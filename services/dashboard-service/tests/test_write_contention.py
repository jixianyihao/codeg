"""Concurrent dashboard writes (R5): same-revision double writes, publish vs
archive, rollback vs patch. Two real HTTP workers on separate connections;
interleaving forced with barriers/events — no sleeps. Asserted outcomes hold
under either serialization order; what is forbidden is both-writers-succeed
or a lost revision bump."""
import threading
import uuid

import sqlalchemy

from dashboard_service import models

from .conftest import publish_html, requires_mysql, service_headers

pytestmark = requires_mysql


def _revision(bundle, dashboard_id: str) -> int:
    with bundle.database.read_only() as connection:
        return connection.execute(
            sqlalchemy.select(models.dashboards.c.revision)
            .where(models.dashboards.c.id == dashboard_id)).scalar_one()


def _status(bundle, dashboard_id: str) -> str:
    with bundle.database.read_only() as connection:
        return connection.execute(
            sqlalchemy.select(models.dashboards.c.status)
            .where(models.dashboards.c.id == dashboard_id)).scalar_one()


def _versions(bundle, dashboard_id: str) -> list:
    with bundle.database.read_only() as connection:
        return connection.execute(
            sqlalchemy.select(models.dashboard_versions.c.number)
            .where(models.dashboard_versions.c.dashboard_id == dashboard_id)
            .order_by(models.dashboard_versions.c.number)).all()


def _patch(client, token, dashboard_id, revision, title, key):
    return client.patch(
        f"/api/v1/dashboards/{dashboard_id}",
        headers={**service_headers(token), "Idempotency-Key": key},
        json={"title": title, "expected_revision": revision})


def test_same_revision_double_patch_exactly_one_wins(client, bundle,
                                                     make_service_account):
    _, token = make_service_account("wc-patch")
    created = publish_html(client, token, b"<html>wc1</html>").json()["result"]
    dashboard_id = created["dashboard_id"]
    assert _revision(bundle, dashboard_id) == 1

    # Both writers read the dashboard row together, then race their updates.
    original = bundle.service.authorizer.dashboard_for_update
    gate = threading.Barrier(3, timeout=30)

    def gated(connection, dashboard_id_value):
        gate.wait()
        return original(connection, dashboard_id_value)

    bundle.service.authorizer.dashboard_for_update = gated
    try:
        results = {}

        def run(name, title):
            from fastapi.testclient import TestClient
            with TestClient(bundle.control) as own:
                results[name] = _patch(own, token, dashboard_id, 1, title,
                                       str(uuid.uuid4()))

        threads = [threading.Thread(target=run, args=(f"t{i}", f"title-{i}"))
                   for i in range(2)]
        for thread in threads:
            thread.start()
        gate.wait()  # release both readers together
        for thread in threads:
            thread.join(timeout=60)
    finally:
        bundle.service.authorizer.dashboard_for_update = original

    statuses = sorted(results[name].status_code for name in results)
    assert statuses == [200, 409], results
    assert _revision(bundle, dashboard_id) == 2, "exactly one bump, no lost update"
    winner = [results[name].json()["result"]["revision"]
              for name in results if results[name].status_code == 200]
    assert winner == [2]


def test_publish_vs_archive_one_wins_consistently(client, bundle,
                                                  make_service_account):
    import io
    _, token = make_service_account("wc-archive", ("read", "write", "manage"))
    created = publish_html(client, token, b"<html>wc-a1</html>").json()["result"]
    dashboard_id = created["dashboard_id"]
    html2 = b"<html>wc-a2</html>"
    import hashlib
    from dashboard_service.errors import ApiError

    ctx = bundle.service.authenticator.authenticate(
        {"authorization": f"Bearer {token}", "x-dashboard-auth-mode": "service"})
    arrived, resume = threading.Event(), threading.Event()

    def gated_reverify():
        arrived.set()
        assert resume.wait(timeout=30)
        return ctx

    publish_out, publish_errors = [], []

    def run_publish():
        try:
            publish_out.append(bundle.service.publisher.publish(
                ctx, gated_reverify, str(uuid.uuid4()), dashboard_id=dashboard_id,
                metadata={"title": "WC", "description": "d",
                          "content_sha256": hashlib.sha256(html2).hexdigest(),
                          "byte_size": len(html2), "expected_revision": 1},
                html_stream=io.BytesIO(html2)))
        except Exception as error:  # noqa: BLE001
            publish_errors.append(error)

    worker = threading.Thread(target=run_publish)
    worker.start()
    assert arrived.wait(timeout=30), "publish never reached pre-commit gate"

    # Archive races the publish's final transaction.
    archived = client.post(
        f"/api/v1/dashboards/{dashboard_id}/archive",
        headers={**service_headers(token), "Idempotency-Key": str(uuid.uuid4())},
        json={"expected_revision": 1})
    resume.set()
    worker.join(timeout=60)

    publish_failed = bool(publish_errors)
    if publish_failed:
        assert isinstance(publish_errors[0], ApiError)
        # Archive won: publish must see the archived state, and no v2 exists.
        assert archived.status_code == 200
        assert _status(bundle, dashboard_id) == "archived"
        assert _versions(bundle, dashboard_id) == [(1,)]
    else:
        # Publish won: archive's expected_revision=1 is stale.
        assert publish_out[0].status == 201
        assert archived.status_code == 409
        assert _status(bundle, dashboard_id) == "published"
        assert _versions(bundle, dashboard_id) == [(1,), (2,)]
        assert _revision(bundle, dashboard_id) == 2


def test_rollback_vs_patch_same_revision_one_wins(client, bundle,
                                                  make_service_account):
    from .conftest import publish_html as publish
    _, token = make_service_account("wc-rollback")
    first = publish(client, token, b"<html>wc-r1</html>").json()["result"]
    dashboard_id = first["dashboard_id"]
    second = publish(client, token, b"<html>wc-r2</html>", dashboard_id=dashboard_id,
                     expected_revision=1).json()["result"]
    assert second["revision"] == 2

    original = bundle.service.authorizer.dashboard_for_update
    gate = threading.Barrier(3, timeout=30)

    def gated(connection, dashboard_id_value):
        gate.wait()
        return original(connection, dashboard_id_value)

    bundle.service.authorizer.dashboard_for_update = gated
    results = {}
    try:
        def run_patch():
            from fastapi.testclient import TestClient
            with TestClient(bundle.control) as own:
                results["patch"] = _patch(own, token, dashboard_id, 2,
                                          "rollback-race", str(uuid.uuid4()))

        def run_rollback():
            from fastapi.testclient import TestClient
            with TestClient(bundle.control) as own:
                results["rollback"] = own.post(
                    f"/api/v1/dashboards/{dashboard_id}/rollback",
                    headers={**service_headers(token),
                             "Idempotency-Key": str(uuid.uuid4())},
                    json={"version_id": first["version_id"],
                          "expected_revision": 2})

        threads = [threading.Thread(target=run_patch),
                   threading.Thread(target=run_rollback)]
        for thread in threads:
            thread.start()
        gate.wait()
        for thread in threads:
            thread.join(timeout=60)
    finally:
        bundle.service.authorizer.dashboard_for_update = original

    statuses = sorted(response.status_code for response in results.values())
    assert statuses == [200, 409], results
    # Exactly one revision bump landed (2 -> 3), never both.
    assert _revision(bundle, dashboard_id) == 3

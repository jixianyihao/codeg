"""Dedicated MySQL/S3 integration regressions; never run on demo resources."""
import hashlib
import io
import threading
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select
from starlette.datastructures import Headers

from dashboard_service import models
from dashboard_service.database import to_db
from dashboard_service.errors import now

from .conftest import human_headers, requires_mysql, service_headers

pytestmark = requires_mysql


def test_deep_health_executes_database_probe(client):
    response = client.get("/api/v1/health/deep")
    assert response.status_code == 200
    assert response.text == "ok"


def begin(bundle, actor, key=None):
    html = b"<html>pending</html>"
    with bundle.database.transaction() as conn:
        with bundle.database.guard(conn, exclusive=False):
            attempt = bundle.service.operations.begin_staged(
                conn, actor, key or str(uuid.uuid4()), action="publish", method="POST",
                path="/api/v1/dashboards", target_id=None,
                payload={"title": "Pending", "content_sha256": hashlib.sha256(html).hexdigest(),
                         "byte_size": len(html)}, new_dashboard=True, byte_size=len(html))
    return attempt, html


def context(bundle, token):
    return bundle.authenticator.authenticate(Headers(service_headers(token)))


def expire(bundle, attempt):
    with bundle.database.transaction() as conn:
        moment = to_db(now() - timedelta(hours=1))
        conn.execute(models.operations.update().where(
            models.operations.c.id == attempt["operation_id"]).values(lease_until=moment))
        conn.execute(models.upload_reservations.update().where(
            models.upload_reservations.c.operation_id == attempt["operation_id"],
            models.upload_reservations.c.attempt_id == attempt["attempt_id"]).values(expires_at=moment))


def test_process_exit_after_s3_put_preserves_tracking_until_cleanup(bundle, make_service_account):
    _, token = make_service_account("i2-crash")
    attempt, html = begin(bundle, context(bundle, token))
    ops = bundle.service.operations
    with bundle.database.transaction() as conn:
        ops.mark_upload_started(conn, attempt["operation_id"], attempt["attempt_id"])
    ref = bundle.service.store.put_verified(attempt["object_key"], html,
                                           hashlib.sha256(html).hexdigest(), len(html))
    # Emulate process loss after PUT and before mark_uploaded/failure handling.
    expire(bundle, attempt)
    ops.recover_stale_operations()
    with bundle.database.read_only() as conn:
        row = ops.reservation(conn, attempt["operation_id"], attempt["attempt_id"])
        assert row is not None
        assert row["reserved_bytes"] == len(html)
        assert row["storage_key"] == ref.key
    assert ops.cleanup_ready_reservations(bundle.service.store) == 1
    assert not bundle.service.store.object_present(ref)


def test_old_recovery_snapshot_cannot_fail_resumed_attempt(bundle, make_service_account):
    _, token = make_service_account("i2-old-snapshot")
    actor = context(bundle, token)
    key = str(uuid.uuid4())
    first, _ = begin(bundle, actor, key)
    expire(bundle, first)
    ops = bundle.service.operations
    with bundle.database.read_only() as conn:
        old = ops.find(conn, actor.principal_id, key)
    second, _ = begin(bundle, actor, key)
    assert second["attempt_id"] != first["attempt_id"]
    with bundle.database.transaction() as conn:
        ops._fail_interrupted(conn, old)
    with bundle.database.read_only() as conn:
        fresh = ops.find(conn, actor.principal_id, key)
        assert fresh["state"] == "accepted"
        assert fresh["attempt_id"] == second["attempt_id"]
        assert ops.reservation(conn, second["operation_id"], second["attempt_id"]) is not None


def test_two_expired_retries_only_start_one_new_attempt(bundle, make_service_account):
    _, token = make_service_account("i2-concurrent-retry")
    actor = context(bundle, token)
    key = str(uuid.uuid4())
    first, _ = begin(bundle, actor, key)
    expire(bundle, first)
    barrier = threading.Barrier(2)
    outcomes, failures = [], []

    def retry():
        try:
            barrier.wait(10)
            outcomes.append(begin(bundle, actor, key)[0])
        except Exception as error:  # noqa: BLE001 - propagate worker failures to the assertion thread
            failures.append(error)

    workers = [threading.Thread(target=retry) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
        assert not worker.is_alive()
    assert not failures
    assert sum("attempt_id" in result for result in outcomes) == 1
    assert sum(result.get("state") == "accepted" for result in outcomes) == 1


def test_full_publisher_process_loss_during_put_keeps_tracking(bundle, make_service_account,
                                                             monkeypatch):
    _, token = make_service_account("i2-publisher-crash")
    actor = context(bundle, token)
    html = b"<html>survives-process-loss</html>"
    ops = bundle.service.operations
    original = bundle.service.store.put_verified
    saved = []

    class ProcessLost(BaseException):
        pass

    def die_after_put(*args):
        saved.append(original(*args))
        raise ProcessLost()

    monkeypatch.setattr(bundle.service.store, "put_verified", die_after_put)
    key = str(uuid.uuid4())
    with pytest.raises(ProcessLost):
        bundle.service.publisher.publish(actor, lambda: actor, key, dashboard_id=None,
                                         metadata={"title": "Crash", "byte_size": len(html),
                                                   "content_sha256": hashlib.sha256(html).hexdigest()},
                                         html_stream=io.BytesIO(html))
    with bundle.database.read_only() as conn:
        operation = ops.find(conn, actor.principal_id, key)
    attempt = {"operation_id": operation["id"], "attempt_id": operation["attempt_id"]}
    expire(bundle, attempt)
    ops.recover_stale_operations()
    with bundle.database.read_only() as conn:
        assert ops.reservation(conn, attempt["operation_id"], attempt["attempt_id"]) is not None
    assert ops.cleanup_ready_reservations(bundle.service.store) == 1
    assert not bundle.service.store.object_present(saved[0])


def test_two_cleanup_workers_release_each_reservation_once(bundle, make_service_account, monkeypatch):
    _, token = make_service_account("i2-cleaners")
    actor = context(bundle, token)
    attempt, html = begin(bundle, actor)
    # Keep another reservation to expose a double deduction rather than
    # having the non-negative quota constraint hide the ledger corruption.
    other, _ = begin(bundle, actor)
    ops = bundle.service.operations
    with bundle.database.transaction() as conn:
        ops.mark_cleanup_pending(conn, attempt["operation_id"], attempt["attempt_id"])
        ops.fail(conn, attempt["operation_id"], attempt["attempt_id"],
                 code="storage_unavailable", message="injected", retryable=True)
    barrier = threading.Barrier(2)
    original_delete = bundle.service.store.delete_orphan

    def gated_delete(ref):
        value = original_delete(ref)
        barrier.wait(timeout=10)
        return value

    monkeypatch.setattr(bundle.service.store, "delete_orphan", gated_delete)
    first_release, let_release, second_release = threading.Event(), threading.Event(), threading.Event()
    original_release = ops._release_quota
    gate_lock = threading.Lock()
    entered = 0

    def gated_release(conn, reservation):
        nonlocal entered
        with gate_lock:
            entered += 1
            index = entered
        if index == 1:
            first_release.set()
            assert let_release.wait(10)
        else:
            second_release.set()
        return original_release(conn, reservation)

    monkeypatch.setattr(ops, "_release_quota", gated_release)
    failures, results = [], []

    def clean():
        try:
            results.append(ops.cleanup_ready_reservations(bundle.service.store))
        except Exception as error:  # noqa: BLE001 - propagate worker failures to the assertion thread
            failures.append(error)

    workers = [threading.Thread(target=clean) for _ in range(2)]
    for worker in workers:
        worker.start()
    assert first_release.wait(10)
    second_release.wait(0.5)
    let_release.set()
    for worker in workers:
        worker.join(10)
        assert not worker.is_alive()
    assert not failures
    assert sum(results) == 1
    with bundle.database.read_only() as conn:
        quota = conn.execute(select(models.quota_usage.c.reserved_bytes).where(
            models.quota_usage.c.scope == "owner",
            models.quota_usage.c.owner_id == actor.principal_id)).scalar_one()
        assert quota == len(html)
        assert ops.reservation(conn, other["operation_id"], other["attempt_id"]) is not None


def test_directory_pages_include_every_type_and_bind_query(bundle, client, make_human,
                                                           make_service_account):
    viewer = make_human("i2-directory-viewer", "Same")
    for index in range(3):
        token = make_human(f"i2-directory-{index}", "Same")
        assert client.get("/api/v1/me", headers=human_headers(token)).status_code == 200
    account, _ = make_service_account("Same")
    headers = human_headers(viewer)
    assert client.get("/api/v1/me", headers=headers).status_code == 200
    actor = bundle.authenticator.authenticate(Headers(headers))
    with bundle.database.transaction() as conn:
        group = bundle.service.groups.create(conn, actor, "Same")
    all_items, cursor = [], None
    for _ in range(10):
        params = {"limit": 2, "q": "Same"}
        if cursor:
            params["cursor"] = cursor
        response = client.get("/api/v1/principals", headers=headers, params=params)
        assert response.status_code == 200
        page = response.json()
        all_items.extend(page["items"])
        next_cursor = page["next_cursor"]
        if not next_cursor:
            break
        assert next_cursor != cursor
        cursor = next_cursor
    assert len(all_items) == 6
    assert len({(item["type"], item["id"]) for item in all_items}) == 6
    assert any(item["id"] == account for item in all_items)
    assert any(item["id"] == group["group_id"] for item in all_items)
    assert cursor
    mismatch = client.get("/api/v1/principals", headers=headers,
                          params={"limit": 2, "q": "changed", "cursor": cursor})
    assert mismatch.status_code == 422

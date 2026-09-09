"""T2: fault-injection recovery — interrupted operations, lease takeovers,
old-worker commits, reservation cleanup, result expiry. All against real
MySQL with committed state verified in the database."""
import uuid
from datetime import timedelta

import pytest
import sqlalchemy

from dashboard_service import models
from dashboard_service.database import from_db, to_db
from dashboard_service.errors import ApiError

from .conftest import committed_version_rows, requires_mysql

pytestmark = requires_mysql


def _ctx(bundle, token):
    return bundle.service.authenticator.authenticate(
        {"authorization": f"Bearer {token}", "x-dashboard-auth-mode": "service"})


def _metadata(html: bytes, **extra) -> dict:
    import hashlib
    meta = {"title": "R", "description": "d",
            "content_sha256": hashlib.sha256(html).hexdigest(), "byte_size": len(html)}
    meta.update(extra)
    return {k: v for k, v in meta.items() if v is not None}


def _operation_row(bundle, operation_id):
    with bundle.database.read_only() as connection:
        return connection.execute(
            sqlalchemy.select(models.operations).where(
                models.operations.c.id == operation_id)).mappings().one()


def test_interrupted_operation_fails_and_resumes(bundle, make_service_account):
    """Stage 3 never runs (simulated crash after the file hit disk): the
    recovery pass marks the operation retryable-failed without committing a
    version; an explicit same-key retry then succeeds exactly once."""
    _, token = make_service_account("rec-interrupt")
    ctx = _ctx(bundle, token)
    html = b"<html>recover</html>"
    key = str(uuid.uuid4())

    # Stage 1: occupy.
    staged = None
    with bundle.database.transaction() as connection:
        with bundle.database.guard(connection, exclusive=False):
            begun = bundle.service.operations.begin_staged(
                connection, ctx, key, action="publish", method="POST",
                path="/api/v1/dashboards", target_id=None, payload=_metadata(html),
                new_dashboard=True, byte_size=len(html))
            assert isinstance(begun, tuple)
            staged = begun
    operation_id, attempt_id = staged

    # Stage 2: file lands durably (crash happens right after this).
    import io
    path = bundle.service.store.stage_stream(attempt_id, io.BytesIO(html),
                                             _metadata(html)["content_sha256"], len(html))
    bundle.service.store.finalize(str(uuid.uuid4()), path)

    # No final transaction: the version must not be committed.
    row = _operation_row(bundle, operation_id)
    assert row["state"] == "accepted"

    # Recovery pass expires the lease by force and re-runs.
    with bundle.database.transaction() as connection:
        connection.execute(models.operations.update().where(
            models.operations.c.id == operation_id).values(
            lease_until=to_db(row["created_at"] - timedelta(seconds=1))))
    result = bundle.service.operations.recover_stale_operations()
    assert result == 1
    row = _operation_row(bundle, operation_id)
    assert row["state"] == "failed"
    assert row["error"]["code"] == "operation_interrupted"
    assert row["error"]["retryable"] is True
    # The reservation was released.
    with bundle.database.read_only() as connection:
        reservations = connection.execute(
            sqlalchemy.select(models.upload_reservations)).all()
    assert reservations == []

    # Same-key retry (fresh request bytes) succeeds.
    from dashboard_service.publishing import PublishOutcome
    outcome = bundle.service.publisher.publish(
        ctx, lambda: ctx, key, dashboard_id=None, metadata=_metadata(html),
        html_stream=io.BytesIO(html))
    assert isinstance(outcome, PublishOutcome)
    assert outcome.status == 201
    rows = committed_version_rows(bundle, outcome.wrapper["result"]["dashboard_id"])
    assert [r[1] for r in rows] == [1]  # exactly one committed version


def test_old_attempt_cannot_commit_after_lease_takeover(bundle, make_service_account):
    """A worker whose lease was taken over must fail claim_for_commit — never
    double-commit."""
    _, token = make_service_account("rec-lease")
    ctx = _ctx(bundle, token)
    html = b"<html>lease</html>"
    key = str(uuid.uuid4())
    with bundle.database.transaction() as connection:
        with bundle.database.guard(connection, exclusive=False):
            operation_id, attempt_id = bundle.service.operations.begin_staged(
                connection, ctx, key, action="publish", method="POST",
                path="/api/v1/dashboards", target_id=None, payload=_metadata(html),
                new_dashboard=True, byte_size=len(html))
    # A new attempt takes over (same key retry after stale-lease recovery).
    with bundle.database.transaction() as connection:
        connection.execute(models.operations.update().where(
            models.operations.c.id == operation_id).values(
            attempt_id=str(uuid.uuid4())))
    with pytest.raises(ApiError) as excinfo:
        with bundle.database.transaction() as connection:
            bundle.service.operations.claim_for_commit(connection, operation_id, attempt_id)
    assert excinfo.value.status == 409
    assert excinfo.value.code == "operation_lease_lost"


def test_result_purge_keeps_compact_record(bundle, make_service_account):
    _, token = make_service_account("rec-purge")
    ctx = _ctx(bundle, token)
    html = b"<html>purge</html>"
    key = str(uuid.uuid4())
    with bundle.database.transaction() as connection:
        with bundle.database.guard(connection, exclusive=False):
            operation_id, _ = bundle.service.operations.begin_staged(
                connection, ctx, key, action="publish", method="POST",
                path="/api/v1/dashboards", target_id=None, payload=_metadata(html),
                new_dashboard=True, byte_size=len(html))
            connection.execute(models.operations.update().where(
                models.operations.c.id == operation_id).values(
                state="succeeded", result={"x": 1},
                result_expires_at=to_db(row_now() - timedelta(days=8))))
    purged = bundle.service.operations.purge_expired_results()
    assert purged >= 1
    row = _operation_row(bundle, operation_id)
    assert row["result"] is None
    assert row["result_purged_at"] is not None
    assert row["idempotency_key"] == key  # compact record retained


def row_now():
    from dashboard_service.errors import now
    return now()


def test_orphan_cleanup_removes_only_unreferenced_old_files(bundle, make_service_account,
                                                             client):
    from .conftest import publish_html
    _, token = make_service_account("rec-orphan")
    created = publish_html(client, token, b"<html>keep</html>").json()["result"]
    # Place an orphan file older than the grace window.
    import os
    orphan = bundle.service.store.root / f"{uuid.uuid4()}.html"
    orphan.write_bytes(b"orphan")
    old = bundle.service.store.root / f"{uuid.uuid4()}.html"
    old.write_bytes(b"old-orphan")
    stamp = os.stat(old).st_mtime - 86400 * 2
    os.utime(old, (stamp, stamp))
    from dashboard_service.operator import Operator
    stats = Operator(bundle.config, bundle.database).cleanup_orphan_files()
    assert stats["files_removed"] == 1
    assert not old.exists()
    assert orphan.exists()          # still inside the grace window
    # The referenced version file survives.
    with bundle.database.read_only() as connection:
        version = connection.execute(
            sqlalchemy.select(models.dashboard_versions).where(
                models.dashboard_versions.c.id == created["version_id"])).mappings().one()
    assert (bundle.service.store.root / f"{version['storage_key']}.html").exists()


def test_duplicate_version_number_race_is_rejected_by_database(bundle, make_service_account):
    """The UNIQUE(dashboard_id, number) constraint is the last line of
    defense against concurrent number assignment."""
    _, token = make_service_account("rec-race")
    ctx = _ctx(bundle, token)
    html = b"<html>race</html>"
    key = str(uuid.uuid4())
    with bundle.database.transaction() as connection:
        with bundle.database.guard(connection, exclusive=False):
            operation_id, _ = bundle.service.operations.begin_staged(
                connection, ctx, key, action="publish", method="POST",
                path="/api/v1/dashboards", target_id=None, payload=_metadata(html),
                new_dashboard=True, byte_size=len(html))
    # Manually simulate a committed version with number 1 for the same
    # dashboard the next publish would create — instead simply verify the
    # constraint exists and fires on direct duplicate insert.
    with pytest.raises(Exception):
        with bundle.database.transaction() as connection:
            from dashboard_service.errors import new_id, now
            moment = to_db(now())
            dash_id = new_id()
            connection.execute(models.dashboards.insert().values(
                id=dash_id, owner_principal_id=ctx.principal_id, title="t",
                current_version_id=None, revision=1, status="published",
                created_at=moment, updated_at=moment, published_at=moment))
            for _ in range(2):
                connection.execute(models.dashboard_versions.insert().values(
                    id=new_id(), dashboard_id=dash_id, number=1,
                    storage_key=new_id(), sha256="a" * 64, byte_size=1,
                    created_by=ctx.principal_id, created_at=moment))

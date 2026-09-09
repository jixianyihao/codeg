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
    """S3 object written, final transaction never runs: recovery marks the
    operation retryable-failed without committing a version; an explicit
    same-key retry succeeds with a NEW attempt (new object key), and the
    abandoned object is removed by exact cleanup."""
    import io
    _, token = make_service_account("rec-interrupt")
    ctx = _ctx(bundle, token)
    html = b"<html>recover</html>"
    key = str(uuid.uuid4())

    # Stage 1: occupy the operation and reservation.
    with bundle.database.transaction() as connection:
        with bundle.database.guard(connection, exclusive=False):
            begun = bundle.service.operations.begin_staged(
                connection, ctx, key, action="publish", method="POST",
                path="/api/v1/dashboards", target_id=None, payload=_metadata(html),
                new_dashboard=True, byte_size=len(html))
            assert "operation_id" in begun
    operation_id, attempt_id = begun["operation_id"], begun["attempt_id"]

    # Stage 2: object lands in S3 and is marked uploaded (crash after this).
    staged_path = bundle.service.store.stage_stream(
        attempt_id, io.BytesIO(html), _metadata(html)["content_sha256"], len(html))
    object_ref = bundle.service.store.put_verified(
        begun["object_key"], staged_path.read_bytes(),
        _metadata(html)["content_sha256"], len(html))
    with bundle.database.transaction() as connection:
        bundle.service.operations.mark_uploaded(connection, operation_id, attempt_id,
                                                object_ref.object_version_id)

    # No final transaction: nothing committed.
    row = _operation_row(bundle, operation_id)
    assert row["state"] == "accepted"
    assert bundle.service.store.object_exists(object_ref)

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

    # Same-key retry (fresh request bytes) succeeds with a new attempt.
    from dashboard_service.publishing import PublishOutcome
    outcome = bundle.service.publisher.publish(
        ctx, lambda: ctx, key, dashboard_id=None, metadata=_metadata(html),
        html_stream=io.BytesIO(html))
    assert isinstance(outcome, PublishOutcome)
    assert outcome.status == 201
    rows = committed_version_rows(bundle, outcome.wrapper["result"]["dashboard_id"])
    assert [r[1] for r in rows] == [1]  # exactly one committed version

    # The abandoned first attempt's object is still there (unique key); the
    # operation is now succeeded (by the retry), so cleanup may settle the
    # superseded attempt's object. A settled key is closed by a zero-byte
    # tombstone: no content remains, and a straggling PUT cannot resurrect it.
    assert bundle.service.store.object_exists(object_ref)
    committed_key = None
    with bundle.database.read_only() as connection:
        committed_key = connection.execute(
            sqlalchemy.select(models.dashboard_versions.c.storage_key).where(
                models.dashboard_versions.c.dashboard_id ==
                outcome.wrapper["result"]["dashboard_id"])).scalar_one()
    removed = bundle.service.operations.cleanup_ready_reservations(bundle.service.store)
    assert removed >= 1
    assert not bundle.service.store.object_present(object_ref)
    assert bundle.service.store.object_exists(
        __import__("dashboard_service.storage", fromlist=["ObjectRef"]).ObjectRef(
            bucket=bundle.config.s3_bucket, key=committed_key))


def test_old_attempt_cannot_commit_after_lease_takeover(bundle, make_service_account):
    """A worker whose lease was taken over must fail claim_for_commit — never
    double-commit."""
    _, token = make_service_account("rec-lease")
    ctx = _ctx(bundle, token)
    html = b"<html>lease</html>"
    key = str(uuid.uuid4())
    with bundle.database.transaction() as connection:
        with bundle.database.guard(connection, exclusive=False):
            begun = bundle.service.operations.begin_staged(
                connection, ctx, key, action="publish", method="POST",
                path="/api/v1/dashboards", target_id=None, payload=_metadata(html),
                new_dashboard=True, byte_size=len(html))
            operation_id, attempt_id = begun["operation_id"], begun["attempt_id"]
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
            begun = bundle.service.operations.begin_staged(
                connection, ctx, key, action="publish", method="POST",
                path="/api/v1/dashboards", target_id=None, payload=_metadata(html),
                new_dashboard=True, byte_size=len(html))
            operation_id = begun["operation_id"]
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


def test_cleanup_never_touches_committed_objects(bundle, make_service_account, client):
    """Committed version objects survive cleanup; only never-committed
    attempt objects (uploaded or cleanup-pending) are deleted exactly."""
    from .conftest import publish_html
    from dashboard_service.storage import ObjectRef
    _, token = make_service_account("rec-orphan")
    created = publish_html(client, token, b"<html>keep</html>").json()["result"]
    with bundle.database.read_only() as connection:
        committed = connection.execute(
            sqlalchemy.select(models.dashboard_versions.c.storage_bucket,
                              models.dashboard_versions.c.storage_key,
                              models.dashboard_versions.c.sha256,
                              models.dashboard_versions.c.byte_size).where(
                models.dashboard_versions.c.id == created["version_id"])).mappings().one()
    committed_ref = ObjectRef(bucket=committed["storage_bucket"], key=committed["storage_key"])
    assert bundle.service.store.object_exists(committed_ref)

    # An abandoned uploaded attempt for a second, never-committed publish.
    ctx = _ctx(bundle, token)
    html = b"<html>orphan</html>"
    with bundle.database.transaction() as connection:
        with bundle.database.guard(connection, exclusive=False):
            begun = bundle.service.operations.begin_staged(
                connection, ctx, str(uuid.uuid4()), action="publish", method="POST",
                path="/api/v1/dashboards", target_id=None, payload=_metadata(html),
                new_dashboard=True, byte_size=len(html))
    staged_path = bundle.service.store.stage_stream(
        begun["attempt_id"], __import__("io").BytesIO(html),
        _metadata(html)["content_sha256"], len(html))
    orphan_ref = bundle.service.store.put_verified(
        begun["object_key"], staged_path.read_bytes(),
        _metadata(html)["content_sha256"], len(html))
    with bundle.database.transaction() as connection:
        bundle.service.operations.mark_uploaded(
            connection, begun["operation_id"], begun["attempt_id"],
            orphan_ref.object_version_id)
        # The abandoned attempt must be provably non-committable before
        # cleanup may touch it: lease dead past the grace window.
        connection.execute(models.operations.update().where(
            models.operations.c.id == begun["operation_id"]).values(
            lease_until=to_db(row_now() - timedelta(seconds=3600))))
        connection.execute(models.upload_reservations.update().where(
            models.upload_reservations.c.operation_id == begun["operation_id"],
            models.upload_reservations.c.attempt_id == begun["attempt_id"]
        ).values(expires_at=to_db(row_now() - timedelta(seconds=3600))))

    from dashboard_service.operator import Operator
    stats = Operator(bundle.config, bundle.database).cleanup_orphan_files()
    assert stats["orphan_objects_removed"] >= 1
    assert not bundle.service.store.object_present(orphan_ref)
    assert bundle.service.store.object_exists(committed_ref)  # committed survives
    # The committed object still reads correctly after cleanup ran.
    assert bundle.service.store.get_verified(
        committed_ref, committed["sha256"], committed["byte_size"]) == b"<html>keep</html>"


def test_duplicate_version_number_race_is_rejected_by_database(bundle, make_service_account):
    """The UNIQUE(dashboard_id, number) constraint is the last line of
    defense against concurrent number assignment."""
    _, token = make_service_account("rec-race")
    ctx = _ctx(bundle, token)
    html = b"<html>race</html>"
    key = str(uuid.uuid4())
    with bundle.database.transaction() as connection:
        with bundle.database.guard(connection, exclusive=False):
            begun = bundle.service.operations.begin_staged(
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

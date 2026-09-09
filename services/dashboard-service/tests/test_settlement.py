"""Settlement lifecycle fixes (review R1-R3): cleanup fencing, unknown PUT
outcomes, attempt-bound failure handling, late-PUT protection.

Everything runs against real MySQL + real S3 (MinIO). Faults are injected by
subclassing the test's own ContentStore so S3 genuinely receives the bytes
before the failure surfaces — no fake stores.
"""
import hashlib
import io
import threading
import uuid
from datetime import timedelta

import pytest
import sqlalchemy

from dashboard_service import models
from dashboard_service.database import from_db, to_db
from dashboard_service.errors import ApiError, now
from dashboard_service.storage import ContentStore, ObjectRef

from .conftest import requires_mysql

pytestmark = requires_mysql


def _metadata(html: bytes, **extra) -> dict:
    meta = {"title": "S", "description": "d",
            "content_sha256": hashlib.sha256(html).hexdigest(),
            "byte_size": len(html)}
    meta.update(extra)
    return {k: v for k, v in meta.items() if v is not None}


def _ctx(bundle, token):
    return bundle.service.authenticator.authenticate(
        {"authorization": f"Bearer {token}", "x-dashboard-auth-mode": "service"})


def _begin(bundle, ctx, html, key=None):
    with bundle.database.transaction() as connection:
        with bundle.database.guard(connection, exclusive=False):
            return bundle.service.operations.begin_staged(
                connection, ctx, key or str(uuid.uuid4()), action="publish",
                method="POST", path="/api/v1/dashboards", target_id=None,
                payload=_metadata(html), new_dashboard=True, byte_size=len(html))


def _reservation_rows(bundle, operation_id: str) -> list:
    with bundle.database.read_only() as connection:
        return connection.execute(
            sqlalchemy.select(models.upload_reservations.c.state,
                              models.upload_reservations.c.attempt_id)
            .where(models.upload_reservations.c.operation_id == operation_id)
        ).mappings().all()


def _operation_state(bundle, operation_id: str) -> dict:
    with bundle.database.read_only() as connection:
        return connection.execute(
            sqlalchemy.select(models.operations.c.state, models.operations.c.attempt_id,
                              models.operations.c.result, models.operations.c.error)
            .where(models.operations.c.id == operation_id)).mappings().one()


def _quota(bundle, owner_id: str) -> dict:
    with bundle.database.read_only() as connection:
        row = connection.execute(
            sqlalchemy.select(models.quota_usage.c.used_bytes,
                              models.quota_usage.c.reserved_bytes)
            .where(models.quota_usage.c.scope == "owner",
                   models.quota_usage.c.owner_id == owner_id)).mappings().one()
        return dict(row)


class _FaultyStore(ContentStore):
    """Real S3 underneath; specific operations fail after their side effect."""

    def __init__(self, config, *, put_raises_after_write=False, readback_raises=False):
        super().__init__(config)
        self.put_raises_after_write = put_raises_after_write
        self.readback_raises = readback_raises

    def put_verified(self, key, data, expected_sha256, expected_size):
        if self.put_raises_after_write:
            # The PUT reaches S3; the response is lost (network blip). The
            # base class would translate this to ApiError(storage_unavailable).
            self._client.put_object(Bucket=self.config.s3_bucket, Key=key, Body=data,
                                    IfNoneMatch="*",
                                    ContentType="text/html; charset=utf-8")
            raise ApiError(503, "storage_unavailable", "response lost (injected)",
                           retryable=True)
        if self.readback_raises:
            self._client.put_object(Bucket=self.config.s3_bucket, Key=key, Body=data,
                                    IfNoneMatch="*",
                                    ContentType="text/html; charset=utf-8")
            raise ApiError(503, "storage_unavailable", "read-back lost (injected)",
                           retryable=True)
        return super().put_verified(key, data, expected_sha256, expected_size)


def _install(bundle, store):
    bundle.service.store = store
    bundle.service.publisher.store = store


def _ref(bundle, key: str) -> ObjectRef:
    return ObjectRef(bucket=bundle.config.s3_bucket, key=key)


def _raw_head(bundle, key):
    return bundle.service.store._client.head_object(
        Bucket=bundle.config.s3_bucket, Key=key)


# --------------------------------------------------------------------- R1

def test_cleanup_never_deletes_object_of_attempt_that_may_still_commit(
        bundle, make_service_account):
    """Barrier scenario: cleanup selects the `uploaded` reservation between
    the S3 upload and the final transaction, while the operation is still
    live (accepted, valid lease). Cleanup must skip; the publish then commits
    and the committed object survives."""
    _, token = make_service_account("st-r1")
    ctx = _ctx(bundle, token)
    html = b"<html>r1-fence</html>"
    key = str(uuid.uuid4())
    arrived, resume = threading.Event(), threading.Event()

    def gated_reverify():
        arrived.set()  # past mark_uploaded, before the final transaction
        assert resume.wait(timeout=30)
        return ctx

    from dashboard_service.publishing import PublishOutcome
    out, errors = [], []

    def run_publish():
        try:
            out.append(bundle.service.publisher.publish(
                ctx, gated_reverify, key, dashboard_id=None,
                metadata=_metadata(html), html_stream=io.BytesIO(html)))
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    worker = threading.Thread(target=run_publish)
    worker.start()
    assert arrived.wait(timeout=30), "publish never reached the pre-commit gate"

    # Cleanup races in while the attempt may still commit.
    removed = bundle.service.operations.cleanup_ready_reservations(
        bundle.service.store)
    assert removed == 0, "live attempt objects must never be cleaned"

    resume.set()
    worker.join(timeout=60)
    assert not errors, [type(e) for e in errors]
    assert out[0].status == 201
    dashboard_id = out[0].wrapper["result"]["dashboard_id"]
    with bundle.database.read_only() as connection:
        committed = connection.execute(
            sqlalchemy.select(models.dashboard_versions.c.storage_key)
            .where(models.dashboard_versions.c.dashboard_id == dashboard_id)
        ).scalar_one()
    assert bundle.service.store.object_exists(_ref(bundle, committed))
    # A later cleanup pass leaves the committed object alone.
    bundle.service.operations.cleanup_ready_reservations(bundle.service.store)
    assert bundle.service.store.object_exists(_ref(bundle, committed))


def test_cleanup_fence_uses_operation_state_not_reservation_state(
        bundle, make_service_account):
    """A `cleanup_pending` row whose operation is live (valid lease, current
    attempt) must also be skipped — state alone must not drive deletion."""
    _, token = make_service_account("st-r1b")
    ctx = _ctx(bundle, token)
    html = b"<html>r1-pending</html>"
    begun = _begin(bundle, ctx, html)
    staged = bundle.service.store.stage_stream(
        begun["attempt_id"], io.BytesIO(html),
        _metadata(html)["content_sha256"], len(html))
    ref = bundle.service.store.put_verified(
        begun["object_key"], staged.read_bytes(),
        _metadata(html)["content_sha256"], len(html))
    with bundle.database.transaction() as connection:
        bundle.service.operations.mark_uploaded(
            connection, begun["operation_id"], begun["attempt_id"],
            ref.object_version_id)
        bundle.service.operations.mark_cleanup_pending(
            connection, begun["operation_id"], begun["attempt_id"])
    assert bundle.service.operations.cleanup_ready_reservations(
        bundle.service.store) == 0
    assert bundle.service.store.object_exists(ref)


# --------------------------------------------------------------------- R2

def test_unknown_put_outcome_keeps_reservation_and_quota(bundle, make_service_account):
    """S3 receives the object but the response is lost: the attempt must be
    settled as tracked-for-cleanup (never quota-released), and cleanup later
    removes the exact object."""
    _, token = make_service_account("st-r2a")
    ctx = _ctx(bundle, token)
    html = b"<html>r2-unknown</html>"
    faulty = _FaultyStore(bundle.config, put_raises_after_write=True)
    _install(bundle, faulty)

    from dashboard_service.publishing import PublishOutcome
    with pytest.raises(ApiError) as excinfo:
        bundle.service.publisher.publish(
            ctx, lambda: ctx, str(uuid.uuid4()), dashboard_id=None,
            metadata=_metadata(html), html_stream=io.BytesIO(html))
    assert excinfo.value.code == "storage_unavailable"

    owner = ctx.principal_id
    with bundle.database.read_only() as connection:
        row = connection.execute(
            sqlalchemy.select(models.upload_reservations.c.state,
                              models.upload_reservations.c.storage_key,
                              models.upload_reservations.c.operation_id)
            .where(models.upload_reservations.c.owner_id == owner)
            .order_by(models.upload_reservations.c.expires_at.desc())
        ).mappings().first()
    assert row is not None, "unknown upload must stay tracked"
    assert row["state"] in ("cleanup_pending", "uploaded")
    quota = _quota(bundle, owner)
    assert quota["reserved_bytes"] == len(html) or quota["used_bytes"] == len(html)
    key = row["storage_key"]

    # The object really is in S3 and remains reachable for exact cleanup.
    assert bundle.service.store.object_exists(_ref(bundle, key))
    # The operation failed (worker recorded the storage failure).
    state = _operation_state(bundle, row["operation_id"])
    assert state["state"] == "failed"

    # Cleanup settles it: object removed, quota released.
    removed = bundle.service.operations.cleanup_ready_reservations(
        bundle.service.store)
    assert removed >= 1
    assert not bundle.service.store.object_present(_ref(bundle, key))
    assert _quota(bundle, owner)["reserved_bytes"] == 0


def test_readback_failure_keeps_reservation(bundle, make_service_account):
    """PUT lands, the read-back is lost: same unknown-outcome settlement."""
    _, token = make_service_account("st-r2b")
    ctx = _ctx(bundle, token)
    html = b"<html>r2-readback</html>"
    faulty = _FaultyStore(bundle.config, readback_raises=True)
    _install(bundle, faulty)
    with pytest.raises(ApiError):
        bundle.service.publisher.publish(
            ctx, lambda: ctx, str(uuid.uuid4()), dashboard_id=None,
            metadata=_metadata(html), html_stream=io.BytesIO(html))
    with bundle.database.read_only() as connection:
        rows = connection.execute(
            sqlalchemy.select(models.upload_reservations.c.state,
                              models.upload_reservations.c.storage_key)
            .where(models.upload_reservations.c.owner_id == ctx.principal_id)
        ).mappings().all()
    assert rows and all(r["state"] in ("cleanup_pending", "uploaded") for r in rows)
    for row in rows:
        assert bundle.service.operations.cleanup_ready_reservations(
            bundle.service.store) >= 0
    # settle and verify objects are gone
    bundle.service.operations.cleanup_ready_reservations(bundle.service.store)
    for row in rows:
        assert not bundle.service.store.object_present(_ref(bundle, row["storage_key"]))


def test_final_tx_rejection_does_not_release_uploaded_quota(
        bundle, make_service_account, client):
    """Upload succeeds but the final transaction rejects (revision conflict):
    the reservation stays tracked; recording the failure must not delete it."""
    from .conftest import publish_html, service_headers
    _, token = make_service_account("st-r2c")
    created = publish_html(client, token, b"<html>v1</html>").json()["result"]
    dashboard_id = created["dashboard_id"]
    revision = client.get(f"/api/v1/dashboards/{dashboard_id}",
                          headers=service_headers(token)).json()["revision"]

    ctx = _ctx(bundle, token)
    html = b"<html>v2-conflict</html>"
    key = str(uuid.uuid4())
    with pytest.raises(ApiError) as excinfo:
        bundle.service.publisher.publish(
            ctx, lambda: ctx, key, dashboard_id=dashboard_id,
            metadata=_metadata(html, expected_revision=revision + 5),
            html_stream=io.BytesIO(html))
    assert excinfo.value.code == "revision_conflict"

    rows = _reservation_rows(bundle, dashboard_id and _op_id(bundle, key, ctx))
    assert rows, "uploaded attempt must remain tracked after final-tx rejection"
    assert all(r["state"] in ("cleanup_pending", "uploaded") for r in rows)
    assert _quota(bundle, ctx.principal_id)["reserved_bytes"] == len(html)
    state = _operation_state(bundle, _op_id(bundle, key, ctx))
    assert state["state"] == "failed"

    # Cleanup settles; the committed v1 object is untouched.
    bundle.service.operations.cleanup_ready_reservations(bundle.service.store)
    assert _quota(bundle, ctx.principal_id)["reserved_bytes"] == 0
    with bundle.database.read_only() as connection:
        committed = connection.execute(
            sqlalchemy.select(models.dashboard_versions.c.storage_key)
            .where(models.dashboard_versions.c.dashboard_id == dashboard_id)).scalar_one()
    assert bundle.service.store.object_exists(_ref(bundle, committed))


def _op_id(bundle, key: str, ctx) -> str:
    with bundle.database.read_only() as connection:
        return bundle.service.operations.find(connection, ctx.principal_id, key)["id"]


def test_late_put_after_cleanup_cannot_resurrect_object(
        bundle, make_service_account):
    """After cleanup settles an attempt key, a straggling PUT (IfNoneMatch,
    same per-attempt key) must fail — the settled key stays closed."""
    import botocore.exceptions
    _, token = make_service_account("st-late")
    ctx = _ctx(bundle, token)
    html = b"<html>late-put</html>"
    begun = _begin(bundle, ctx, html)
    staged = bundle.service.store.stage_stream(
        begun["attempt_id"], io.BytesIO(html),
        _metadata(html)["content_sha256"], len(html))
    ref = bundle.service.store.put_verified(
        begun["object_key"], staged.read_bytes(),
        _metadata(html)["content_sha256"], len(html))
    with bundle.database.transaction() as connection:
        bundle.service.operations.mark_uploaded(
            connection, begun["operation_id"], begun["attempt_id"],
            ref.object_version_id)
        # Worker dies; recovery fails the operation (lease expired).
        connection.execute(models.operations.update().where(
            models.operations.c.id == begun["operation_id"]).values(
            lease_until=to_db(now() - timedelta(seconds=1))))
    bundle.service.operations.recover_stale_operations()

    # Make the attempt grace-eligible for cleanup (own lease far past).
    with bundle.database.transaction() as connection:
        connection.execute(models.operations.update().where(
            models.operations.c.id == begun["operation_id"]).values(
            lease_until=to_db(now() - timedelta(seconds=3600))))
        connection.execute(models.upload_reservations.update().where(
            models.upload_reservations.c.operation_id == begun["operation_id"],
            models.upload_reservations.c.attempt_id == begun["attempt_id"]
        ).values(expires_at=to_db(now() - timedelta(seconds=3600))))
    removed = bundle.service.operations.cleanup_ready_reservations(
        bundle.service.store)
    assert removed >= 1
    assert not bundle.service.store.object_present(ref)

    # The dead worker's PUT finally completes on the same unique key.
    with pytest.raises(botocore.exceptions.ClientError):
        bundle.service.store._client.put_object(
            Bucket=bundle.config.s3_bucket, Key=ref.key, Body=b"<html>zombie</html>",
            IfNoneMatch="*")


# --------------------------------------------------------------------- R3

def test_stale_attempt_failure_cannot_overwrite_new_attempt_success(
        bundle, make_service_account, client):
    """A expires → B takes over the same key and succeeds → A's failure
    handling arrives late. The recorded success must survive untouched."""
    from .conftest import publish_html
    from dashboard_service.publishing import PublishOutcome
    _, token = make_service_account("st-r3")
    ctx = _ctx(bundle, token)
    html = b"<html>r3</html>"
    key = str(uuid.uuid4())
    begun = _begin(bundle, ctx, html, key=key)
    operation_id, attempt_a = begun["operation_id"], begun["attempt_id"]

    # A's lease lapses; the same key retries (B) and commits.
    with bundle.database.transaction() as connection:
        connection.execute(models.operations.update().where(
            models.operations.c.id == operation_id).values(
            lease_until=to_db(now() - timedelta(seconds=1))))
    outcome = bundle.service.publisher.publish(
        ctx, lambda: ctx, key, dashboard_id=None, metadata=_metadata(html),
        html_stream=io.BytesIO(html))
    assert outcome.status == 201
    result = outcome.wrapper["result"]

    # A's worker surfaces its (stale) failure through the full Publisher
    # failure path — settle + record — and must change nothing about B.
    error = ApiError(503, "storage_unavailable", "stale worker saw an error",
                     retryable=True)
    bundle.service.publisher._settle_failed_attempt(
        operation_id, attempt_a, uploaded=True)
    bundle.service.publisher._record_failure(operation_id, error, attempt_id=attempt_a)

    state = _operation_state(bundle, operation_id)
    assert state["state"] == "succeeded"
    assert state["result"] == result
    # B's quota conversion is intact; A's own reservation settled separately.
    rows = _reservation_rows(bundle, operation_id)
    assert [r["state"] for r in rows if r["attempt_id"] != attempt_a] == ["committed"]
    with bundle.database.read_only() as connection:
        versions = connection.execute(
            sqlalchemy.select(models.dashboard_versions.c.id)
            .where(models.dashboard_versions.c.dashboard_id ==
                   result["dashboard_id"])).all()
    assert len(versions) == 1


def test_stale_attempt_failure_cannot_fail_operation_in_progress(
        bundle, make_service_account):
    """A's late failure while B is mid-flight must not flip the operation to
    failed (B's final claim would then abort spuriously). B runs the full
    Publisher path; A's failure lands between B's upload and commit."""
    _, token = make_service_account("st-r3b")
    ctx = _ctx(bundle, token)
    html = b"<html>r3b</html>"
    key = str(uuid.uuid4())
    begun = _begin(bundle, ctx, html, key=key)
    operation_id, attempt_a = begun["operation_id"], begun["attempt_id"]

    # A's lease lapses so B's same-key publish takes over with a new attempt.
    with bundle.database.transaction() as connection:
        connection.execute(models.operations.update().where(
            models.operations.c.id == operation_id).values(
            lease_until=to_db(now() - timedelta(seconds=1))))

    arrived, resume = threading.Event(), threading.Event()

    def gated_reverify():
        arrived.set()
        assert resume.wait(timeout=30)
        return ctx

    out, errors = [], []

    def run_publish():
        try:
            out.append(bundle.service.publisher.publish(
                ctx, gated_reverify, key, dashboard_id=None,
                metadata=_metadata(html), html_stream=io.BytesIO(html)))
        except Exception as error:  # noqa: BLE001
            errors.append(error)

    worker = threading.Thread(target=run_publish)
    worker.start()
    assert arrived.wait(timeout=30), "B never reached the pre-commit gate"

    # A's stale failure arrives while B holds a fresh lease mid-flight.
    bundle.service.publisher._settle_failed_attempt(operation_id, attempt_a,
                                                    uploaded=False)
    bundle.service.publisher._record_failure(
        operation_id, ApiError(503, "storage_unavailable", "late", retryable=True),
        attempt_id=attempt_a)
    state = _operation_state(bundle, operation_id)
    assert state["state"] in ("accepted", "processing"), \
        "B's in-flight operation must stay live"
    assert state["attempt_id"] != attempt_a

    resume.set()
    worker.join(timeout=60)
    assert not errors, [type(e) for e in errors]
    assert out[0].status == 201
    assert _operation_state(bundle, operation_id)["state"] == "succeeded"


def test_release_reservation_never_touches_uploaded_states(bundle, make_service_account):
    """Direct unit check on release semantics: only `reserved` rows release."""
    _, token = make_service_account("st-release")
    ctx = _ctx(bundle, token)
    html = b"<html>release</html>"
    begun = _begin(bundle, ctx, html)
    op, attempt = begun["operation_id"], begun["attempt_id"]
    staged = bundle.service.store.stage_stream(
        attempt, io.BytesIO(html), _metadata(html)["content_sha256"], len(html))
    ref = bundle.service.store.put_verified(
        begun["object_key"], staged.read_bytes(),
        _metadata(html)["content_sha256"], len(html))
    with bundle.database.transaction() as connection:
        bundle.service.operations.mark_uploaded(connection, op, attempt,
                                                ref.object_version_id)
    before = _quota(bundle, ctx.principal_id)
    with bundle.database.transaction() as connection:
        bundle.service.operations.release_reservation(connection, op, attempt)
    rows = _reservation_rows(bundle, op)
    assert rows, "uploaded reservation must survive release_reservation"
    assert _quota(bundle, ctx.principal_id) == before
    # cleanup_pending likewise survives a release call.
    with bundle.database.transaction() as connection:
        bundle.service.operations.mark_cleanup_pending(connection, op, attempt)
    with bundle.database.transaction() as connection:
        bundle.service.operations.release_reservation(connection, op)
    assert _reservation_rows(bundle, op), "cleanup_pending must survive release"
    assert _quota(bundle, ctx.principal_id) == before

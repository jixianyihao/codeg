"""Final-transaction authorization on the CREATE path (R4), staged-key
semantics (R10) and quota-checked-only-for-new-attempts (R11). Real MySQL +
MinIO; interleaving is forced with threading events, never sleeps."""
import hashlib
import io
import threading
import uuid

import pytest
import sqlalchemy

from dashboard_service import models
from dashboard_service.errors import ApiError

from .conftest import requires_mysql

pytestmark = requires_mysql


def _metadata(html: bytes, **extra) -> dict:
    meta = {"title": "G", "description": "d",
            "content_sha256": hashlib.sha256(html).hexdigest(),
            "byte_size": len(html)}
    meta.update(extra)
    return {k: v for k, v in meta.items() if v is not None}


def _ctx(bundle, token):
    return bundle.service.authenticator.authenticate(
        {"authorization": f"Bearer {token}", "x-dashboard-auth-mode": "service"})


def _publish_thread(bundle, ctx, reverify_fn, dashboard_id, html, key, out, errors,
                    expected_revision=None):
    try:
        out.append(bundle.service.publisher.publish(
            ctx, reverify_fn, key, dashboard_id=dashboard_id,
            metadata=_metadata(html, expected_revision=expected_revision),
            html_stream=io.BytesIO(html)))
    except Exception as error:  # noqa: BLE001
        errors.append(error)


def _count_dashboards(bundle, owner_id: str) -> int:
    with bundle.database.read_only() as connection:
        return connection.execute(
            sqlalchemy.select(sqlalchemy.func.count()).select_from(models.dashboards)
            .where(models.dashboards.c.owner_principal_id == owner_id)).scalar_one()


# ---------------------------------------------------------------------- R4

def test_create_rejected_when_account_disabled_during_upload(
        bundle, make_service_account):
    """New-dashboard publishes must re-validate the live account state inside
    the final transaction — the create path has no ACL row to authorize."""
    from dashboard_service.operator import Operator
    principal_id, token = make_service_account("pg-r4-disable", ("read", "write"))
    ctx = _ctx(bundle, token)
    html = b"<html>r4-create</html>"
    arrived, resume = threading.Event(), threading.Event()

    def stale_reverify():
        arrived.set()
        assert resume.wait(timeout=30)
        return ctx  # stale on purpose: the disable lands in this window

    out, errors = [], []
    worker = threading.Thread(target=_publish_thread, args=(
        bundle, ctx, stale_reverify, None, html, str(uuid.uuid4()), out, errors))
    worker.start()
    assert arrived.wait(timeout=30)
    Operator(bundle.config, bundle.database).set_enabled(principal_id, False)
    resume.set()
    worker.join(timeout=60)

    assert len(errors) == 1 and isinstance(errors[0], ApiError)
    assert errors[0].code == "token_revoked"
    assert _count_dashboards(bundle, principal_id) == 0


def test_create_rejected_when_scope_narrowed_during_upload(
        bundle, make_service_account):
    from dashboard_service.operator import Operator
    principal_id, token = make_service_account("pg-r4-scope", ("read", "write"))
    ctx = _ctx(bundle, token)
    html = b"<html>r4-scope</html>"
    arrived, resume = threading.Event(), threading.Event()

    def stale_reverify():
        arrived.set()
        assert resume.wait(timeout=30)
        return ctx

    out, errors = [], []
    worker = threading.Thread(target=_publish_thread, args=(
        bundle, ctx, stale_reverify, None, html, str(uuid.uuid4()), out, errors))
    worker.start()
    assert arrived.wait(timeout=30)
    Operator(bundle.config, bundle.database).set_scopes(principal_id, ["read"])
    resume.set()
    worker.join(timeout=60)

    assert len(errors) == 1 and isinstance(errors[0], ApiError)
    assert errors[0].code == "action_forbidden"
    assert _count_dashboards(bundle, principal_id) == 0


def test_create_rejected_when_credential_expired_by_commit_time(bundle,
                                                                make_service_account):
    _, token = make_service_account("pg-r4-expired", ("read", "write"))
    ctx = _ctx(bundle, token)
    html = b"<html>r4-expired</html>"
    from datetime import timedelta, timezone
    from dashboard_service.errors import now
    expired = now().replace(tzinfo=timezone.utc) - timedelta(seconds=1)
    object.__setattr__(ctx, "valid_until", expired)
    with pytest.raises(ApiError) as excinfo:
        bundle.service.publisher.publish(
            ctx, lambda: ctx, str(uuid.uuid4()), dashboard_id=None,
            metadata=_metadata(html), html_stream=io.BytesIO(html))
    assert excinfo.value.code == "token_expired"
    assert _count_dashboards(bundle, ctx.principal_id) == 0


# --------------------------------------------------------------------- R10

def test_active_lease_different_request_is_conflict_not_pending(
        bundle, make_service_account):
    """An in-flight key must verify the request digest before answering 202:
    same key + different bytes is a 409, not a silent 'in progress'."""
    _, token = make_service_account("pg-r10-hash")
    ctx = _ctx(bundle, token)
    html = b"<html>r10-original</html>"
    key = str(uuid.uuid4())
    arrived, resume = threading.Event(), threading.Event()

    def gated_reverify():
        arrived.set()
        assert resume.wait(timeout=30)
        return ctx

    out, errors = [], []
    worker = threading.Thread(target=_publish_thread, args=(
        bundle, ctx, gated_reverify, None, html, key, out, errors))
    worker.start()
    assert arrived.wait(timeout=30)

    # Same key, different bytes while the first attempt holds the lease.
    other = b"<html>r10-different</html>"
    out2, errors2 = [], []
    _publish_thread(bundle, ctx, lambda: ctx, None, other, key, out2, errors2)
    assert len(errors2) == 1 and isinstance(errors2[0], ApiError)
    assert errors2[0].status == 409
    assert errors2[0].code == "idempotency_conflict"

    # Same key, same bytes: the pending wrapper (202) is correct.
    out3, errors3 = [], []
    _publish_thread(bundle, ctx, lambda: ctx, None, html, key, out3, errors3)
    assert not errors3
    assert out3[0].status == 202
    assert out3[0].wrapper["state"] in ("accepted", "processing")

    resume.set()
    worker.join(timeout=60)
    assert not errors
    assert out[0].status == 201
    assert _count_dashboards(bundle, ctx.principal_id) == 1


def test_stale_lease_same_key_resumes_without_operator_recover(
        bundle, make_service_account):
    """An expired-lease accepted operation resumes inline on the next same-key
    same-bytes request — no operator recover step required."""
    from datetime import timedelta
    from dashboard_service.database import to_db
    from dashboard_service.errors import now
    _, token = make_service_account("pg-r10-resume")
    ctx = _ctx(bundle, token)
    html = b"<html>r10-resume</html>"
    key = str(uuid.uuid4())

    # First attempt occupies the key, then its lease dies (worker lost).
    with bundle.database.transaction() as connection:
        with bundle.database.guard(connection, exclusive=False):
            begun = bundle.service.operations.begin_staged(
                connection, ctx, key, action="publish", method="POST",
                path="/api/v1/dashboards", target_id=None, payload=_metadata(html),
                new_dashboard=True, byte_size=len(html))
        connection.execute(models.operations.update().where(
            models.operations.c.id == begun["operation_id"]).values(
            lease_until=to_db(now() - timedelta(seconds=1))))

    outcome = bundle.service.publisher.publish(
        ctx, lambda: ctx, key, dashboard_id=None, metadata=_metadata(html),
        html_stream=io.BytesIO(html))
    assert outcome.status == 201
    with bundle.database.read_only() as connection:
        states = [row[0] for row in connection.execute(
            sqlalchemy.select(models.upload_reservations.c.state)
            .where(models.upload_reservations.c.operation_id ==
                   begun["operation_id"]))]
    assert states == ["committed"], "one reservation, converted"


# --------------------------------------------------------------------- R11

def test_full_quota_still_replays_finished_result(bundle, client, make_service_account):
    from .conftest import publish_html
    _, token = make_service_account("pg-r11")
    bundle.config.max_owner_dashboards = 1  # single-dashboard owner
    key = str(uuid.uuid4())
    html = b"<html>r11-only</html>"
    created = publish_html(client, token, html, key=key).json()["result"]
    assert created["revision"] == 1

    # The success response was lost; the client retries the SAME key. The
    # owner is now at the dashboard cap — replay must still return the
    # recorded success instead of quota_exceeded (R11).
    replay = publish_html(client, token, html, key=key)
    assert replay.status_code != 507, replay.text
    body = replay.json()
    assert body["state"] == "succeeded"
    assert body["result"]["dashboard_id"] == created["dashboard_id"]
    assert _count_dashboards(bundle, _owner_of(bundle, created["dashboard_id"])) == 1

    # A NEW key at full quota is properly rejected.
    fresh = publish_html(client, token, b"<html>r11-second</html>")
    assert fresh.status_code == 507
    assert fresh.json()["code"] == "quota_exceeded"


def _owner_of(bundle, dashboard_id: str) -> str:
    with bundle.database.read_only() as connection:
        return connection.execute(
            sqlalchemy.select(models.dashboards.c.owner_principal_id)
            .where(models.dashboards.c.id == dashboard_id)).scalar_one()

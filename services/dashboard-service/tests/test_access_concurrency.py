"""T3 concurrency: revocations that land while an upload is in flight must
be honored at the final commit. Coordination uses threading events — never
fixed sleeps pretending to be races. Revocations run on real separate MySQL
connections (operator/service layer), exactly like a hostile or merely fast
concurrent admin."""
import hashlib
import io
import threading
import uuid

import pytest
import sqlalchemy

from dashboard_service import models
from dashboard_service.errors import ApiError
from dashboard_service.operator import Operator

from .conftest import committed_version_rows, requires_mysql

pytestmark = requires_mysql


class _Gate:
    """Blocks the publish between staging and the final transaction while a
    revocation commits on another real connection."""

    def __init__(self, bundle, token, action):
        self.bundle = bundle
        self.token = token
        self.action = action
        self.resume = threading.Event()
        self.arrived = threading.Event()

    def reverify(self):
        self.arrived.set()
        assert self.resume.wait(timeout=30), "test gate timed out"
        return self.bundle.service.authenticator.authenticate(
            {"authorization": f"Bearer {self.token}",
             "x-dashboard-auth-mode": "human"})


def _publish_thread(bundle, ctx, reverify_fn, dashboard_id, html, key, out, errors,
                    expected_revision):
    import json
    metadata = {"title": "Board", "description": "d",
                "content_sha256": hashlib.sha256(html).hexdigest(),
                "byte_size": len(html)}
    if dashboard_id:
        metadata["expected_revision"] = expected_revision
    try:
        outcome = bundle.service.publisher.publish(
            ctx, reverify_fn, key, dashboard_id=dashboard_id, metadata=metadata,
            html_stream=io.BytesIO(html))
        out.append(outcome)
    except Exception as error:  # noqa: BLE001
        errors.append(error)


def _assert_rejected_and_unchanged(bundle, errors, dashboard_id, expected_code):
    assert len(errors) == 1, [type(e) for e in errors]
    error = errors[0]
    assert isinstance(error, ApiError), type(error)
    assert error.code == expected_code
    rows = committed_version_rows(bundle, dashboard_id)
    assert [r[1] for r in rows] == [1], "no second version may be committed"
    with bundle.database.read_only() as connection:
        current = connection.execute(
            sqlalchemy.select(models.dashboards.c.current_version_id)
            .where(models.dashboards.c.id == dashboard_id)).scalar_one()
        version_one = connection.execute(
            sqlalchemy.select(models.dashboard_versions.c.id).where(
                models.dashboard_versions.c.dashboard_id == dashboard_id,
                models.dashboard_versions.c.number == 1)).scalar_one()
    assert current == version_one, "the current-version pointer must not move"
    # The operation row records a real failure with the original principal.
    with bundle.database.read_only() as connection:
        operation = connection.execute(
            sqlalchemy.select(models.operations.c.state, models.operations.c.error)
            .where(models.operations.c.target_id == dashboard_id,
                   models.operations.c.action.in_(("publish", "publish_v2")))
            .order_by(models.operations.c.created_at.desc())).mappings().first()
    assert operation["state"] == "failed"
    assert operation["error"]["code"] == expected_code


def _dashboard_revision(bundle, dashboard_id) -> int:
    with bundle.database.read_only() as connection:
        return connection.execute(
            sqlalchemy.select(models.dashboards.c.revision).where(
                models.dashboards.c.id == dashboard_id)).scalar_one()


def test_editor_revoked_during_upload_final_commit_rejected(bundle, client, make_human,
                                                            operator):
    owner_token = make_human("cc-owner", "CC Owner")
    editor_token = make_human("cc-editor", "CC Editor")
    from .conftest import human_headers, publish_html
    created = publish_html(client, owner_token, auth_mode="human").json()["result"]
    dashboard_id = created["dashboard_id"]
    editor_id = client.get("/api/v1/me", headers=human_headers(editor_token)).json()["principal_id"]
    owner_id = client.get("/api/v1/me", headers=human_headers(owner_token)).json()["principal_id"]
    granted = client.post(
        f"/api/v1/dashboards/{dashboard_id}/grants",
        headers={**human_headers(owner_token), "Idempotency-Key": str(uuid.uuid4())},
        json={"subject_type": "user", "subject_id": editor_id, "role": "editor",
              "expected_revision": 1})
    assert granted.status_code == 200

    editor_ctx = bundle.service.authenticator.authenticate(
        {"authorization": f"Bearer {editor_token}", "x-dashboard-auth-mode": "human"})
    gate = _Gate(bundle, editor_token, "revoke")
    out, errors = [], []
    worker = threading.Thread(
        target=_publish_thread,
        args=(bundle, editor_ctx, gate.reverify, dashboard_id, b"<html>v2</html>",
              str(uuid.uuid4()), out, errors, _dashboard_revision(bundle, dashboard_id)))
    worker.start()
    assert gate.arrived.wait(timeout=30), "upload never reached the gate"

    # Real concurrent revoke on the owner's authority while the upload waits.
    revoked = client.delete(
        f"/api/v1/dashboards/{dashboard_id}/grants/user/{editor_id}?expected_revision=2",
        headers={**human_headers(owner_token), "Idempotency-Key": str(uuid.uuid4())})
    assert revoked.status_code == 200
    gate.resume.set()
    worker.join(timeout=60)
    _assert_rejected_and_unchanged(bundle, errors, dashboard_id, "not_found")


def test_service_account_disabled_during_upload(bundle, client, make_service_account):
    _, token = make_service_account("cc-machine")
    created = None
    from .conftest import publish_html, service_headers
    created = publish_html(client, token).json()["result"]
    dashboard_id = created["dashboard_id"]
    operator_obj = Operator(bundle.config, bundle.database)

    ctx = bundle.service.authenticator.authenticate(
        {"authorization": f"Bearer {token}", "x-dashboard-auth-mode": "service"})
    arrived = threading.Event()
    resume = threading.Event()

    def reverify():
        arrived.set()
        assert resume.wait(timeout=30)
        return bundle.service.authenticator.authenticate(
            {"authorization": f"Bearer {token}", "x-dashboard-auth-mode": "service"})

    out, errors = [], []
    worker = threading.Thread(
        target=_publish_thread, args=(bundle, ctx, reverify, dashboard_id,
                                      b"<html>m2</html>", str(uuid.uuid4()), out, errors,
                                      _dashboard_revision(bundle, dashboard_id)))
    worker.start()
    assert arrived.wait(timeout=30)
    operator_obj.set_enabled("cc-machine", False)  # real second connection
    resume.set()
    worker.join(timeout=60)
    assert errors, "disabled account must not publish"
    assert errors[0].code == "token_revoked"
    assert [r[1] for r in committed_version_rows(bundle, dashboard_id)] == [1]


def test_group_membership_removed_during_upload(bundle, client, make_human):
    from .conftest import human_headers, publish_html
    owner_token = make_human("cg-owner", "CG Owner")
    member_token = make_human("cg-member", "CG Member")
    created = publish_html(client, owner_token, auth_mode="human").json()["result"]
    dashboard_id = created["dashboard_id"]
    member_id = client.get("/api/v1/me", headers=human_headers(member_token)).json()["principal_id"]
    group = client.post("/api/v1/groups",
                        headers={**human_headers(owner_token),
                                 "Idempotency-Key": str(uuid.uuid4())},
                        json={"display_name": "CG"}).json()["result"]
    client.put(f"/api/v1/groups/{group['group_id']}/members",
               headers={**human_headers(owner_token), "Idempotency-Key": str(uuid.uuid4())},
               json={"members": [member_id], "expected_revision": 1})
    granted = client.post(
        f"/api/v1/dashboards/{dashboard_id}/grants",
        headers={**human_headers(owner_token), "Idempotency-Key": str(uuid.uuid4())},
        json={"subject_type": "group", "subject_id": group["group_id"], "role": "editor",
              "expected_revision": 1})
    assert granted.status_code == 200

    member_ctx = bundle.service.authenticator.authenticate(
        {"authorization": f"Bearer {member_token}", "x-dashboard-auth-mode": "human"})
    arrived = threading.Event()
    resume = threading.Event()

    def reverify():
        arrived.set()
        assert resume.wait(timeout=30)
        return member_ctx

    out, errors = [], []
    worker = threading.Thread(
        target=_publish_thread, args=(bundle, member_ctx, reverify, dashboard_id,
                                      b"<html>g2</html>", str(uuid.uuid4()), out, errors,
                                      _dashboard_revision(bundle, dashboard_id)))
    worker.start()
    assert arrived.wait(timeout=30)
    removed = client.put(
        f"/api/v1/groups/{group['group_id']}/members",
        headers={**human_headers(owner_token), "Idempotency-Key": str(uuid.uuid4())},
        json={"members": [], "expected_revision": 2})
    assert removed.status_code == 200
    resume.set()
    worker.join(timeout=60)
    _assert_rejected_and_unchanged(bundle, errors, dashboard_id, "not_found")


def test_scope_revoked_during_upload_by_operator(bundle, client, make_service_account,
                                                 operator):
    principal_id, token = make_service_account("cs-machine", ("read", "write"))
    from .conftest import publish_html
    created = publish_html(client, token).json()["result"]
    dashboard_id = created["dashboard_id"]
    ctx = bundle.service.authenticator.authenticate(
        {"authorization": f"Bearer {token}", "x-dashboard-auth-mode": "service"})
    arrived = threading.Event()
    resume = threading.Event()

    def reverify():
        arrived.set()
        assert resume.wait(timeout=30)
        return ctx

    out, errors = [], []
    worker = threading.Thread(
        target=_publish_thread, args=(bundle, ctx, reverify, dashboard_id,
                                      b"<html>s2</html>", str(uuid.uuid4()), out, errors,
                                      _dashboard_revision(bundle, dashboard_id)))
    worker.start()
    assert arrived.wait(timeout=30)
    operator.set_scopes(principal_id, ["read"])  # write removed mid-flight
    resume.set()
    worker.join(timeout=60)
    assert errors
    assert errors[0].code == "action_forbidden"
    assert [r[1] for r in committed_version_rows(bundle, dashboard_id)] == [1]

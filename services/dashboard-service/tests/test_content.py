"""T4: view capabilities and the isolated content origin."""
import threading
import time
import uuid
from datetime import timedelta

import pytest
import sqlalchemy

from dashboard_service import models

from .conftest import human_headers, publish_html, requires_mysql, service_headers

pytestmark = requires_mysql

CAPABILITY_TTL = 60


@pytest.fixture()
def human_board(client, make_human):
    token = make_human("view-owner", "VOwner")
    created = publish_html(client, token, auth_mode="human",
                           title="Rendered").json()["result"]
    return token, created


def _issue(client, token, dashboard_id, payload=None):
    return client.post(f"/api/v1/dashboards/{dashboard_id}/view-capabilities",
                       headers={**human_headers(token),
                                "Idempotency-Key": str(uuid.uuid4())},
                       json=payload or {})


def test_capability_flow_happy_path(client, content_client, human_board):
    token, created = human_board
    issued = _issue(client, token, created["dashboard_id"])
    assert issued.status_code == 200
    body = issued.json()
    assert body["render_url"].startswith(f"http://127.0.0.1:18081/view/{created['dashboard_id']}#")
    assert body["expires_at"]

    fragment = body["render_url"].split("#", 1)[1]
    content = content_client.get("/content", headers={"Authorization": f"Bearer {fragment}"})
    assert content.status_code == 200
    assert content.content == b"<html><body>ok</body></html>"
    assert content.headers["content-type"].startswith("text/plain")
    assert content.headers["cache-control"] == "no-store"
    assert content.headers["x-content-type-options"] == "nosniff"
    assert content.headers["referrer-policy"] == "no-referrer"


def test_machine_cannot_issue_capabilities(client, make_service_account, human_board):
    _, machine = make_service_account("view-machine", ("read", "write", "manage"))
    _, created = human_board
    response = client.post(
        f"/api/v1/dashboards/{created['dashboard_id']}/view-capabilities",
        headers={**service_headers(machine), "Idempotency-Key": str(uuid.uuid4())},
        json={})
    assert response.status_code == 403


def test_w3_or_service_jwt_rejected_at_content(content_client, client, make_service_account,
                                               human_board):
    _, created = human_board
    _, machine = make_service_account("view-machine2", ("read",))
    response = content_client.get("/content", headers=service_headers(machine))
    assert response.status_code == 401


def test_capability_cannot_call_control_api(client, content_client, human_board):
    token, created = human_board
    body = _issue(client, token, created["dashboard_id"]).json()
    fragment = body["render_url"].split("#", 1)[1]
    # The capability is not a login for the control API.
    listing = client.get("/api/v1/dashboards",
                         headers={"Authorization": f"Bearer {fragment}"})
    assert listing.status_code in (401, 503)


def test_capability_bound_to_one_version(client, content_client, human_board):
    token, created = human_board
    v2 = publish_html(client, token, b"<html>v2</html>",
                      dashboard_id=created["dashboard_id"], expected_revision=1,
                      auth_mode="human").json()["result"]
    # Capability pinned to v1 keeps serving v1; URL tampering cannot switch
    # the version because the URL never carries one.
    body = _issue(client, token, created["dashboard_id"],
                  {"version_id": created["version_id"]}).json()
    fragment = body["render_url"].split("#", 1)[1]
    first = content_client.get("/content", headers={"Authorization": f"Bearer {fragment}"})
    assert first.content == b"<html><body>ok</body></html>"
    tampered = content_client.get("/content",
                                  headers={"Authorization": f"Bearer {fragment}x"})
    assert tampered.status_code == 401


def test_revocation_blocks_capability_reads(client, content_client, bundle, human_board,
                                            make_human):
    owner_token, created = human_board
    reader = make_human("view-reader", "Reader")
    reader_id = client.get("/api/v1/me", headers=human_headers(reader)).json()["principal_id"]
    granted = client.post(
        f"/api/v1/dashboards/{created['dashboard_id']}/grants",
        headers={**human_headers(owner_token), "Idempotency-Key": str(uuid.uuid4())},
        json={"subject_type": "user", "subject_id": reader_id, "role": "viewer",
              "expected_revision": 1})
    assert granted.status_code == 200
    body = _issue(client, reader, created["dashboard_id"]).json()
    fragment = body["render_url"].split("#", 1)[1]
    assert content_client.get("/content",
                              headers={"Authorization": f"Bearer {fragment}"}).status_code == 200
    revoked = client.delete(
        f"/api/v1/dashboards/{created['dashboard_id']}/grants/user/{reader_id}?expected_revision=2",
        headers={**human_headers(owner_token), "Idempotency-Key": str(uuid.uuid4())})
    assert revoked.status_code == 200
    again = content_client.get("/content", headers={"Authorization": f"Bearer {fragment}"})
    assert again.status_code == 401
    assert again.json()["code"] == "invalid_capability"


def test_archived_dashboard_rejects_capabilities(client, content_client, human_board):
    token, created = human_board
    body = _issue(client, token, created["dashboard_id"]).json()
    fragment = body["render_url"].split("#", 1)[1]
    archived = client.post(
        f"/api/v1/dashboards/{created['dashboard_id']}/archive",
        headers={**human_headers(token), "Idempotency-Key": str(uuid.uuid4())},
        json={"expected_revision": 1})
    assert archived.status_code == 200
    assert content_client.get("/content",
                              headers={"Authorization": f"Bearer {fragment}"}).status_code == 401


def test_capability_expiry_capped_at_60s(client, bundle, human_board):
    token, created = human_board
    body = _issue(client, token, created["dashboard_id"]).json()
    from dashboard_service.errors import now as svc_now
    from datetime import datetime
    issued_at = svc_now()
    expiry = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    assert timedelta(seconds=0) < (expiry - issued_at) <= timedelta(seconds=CAPABILITY_TTL + 1)


def test_capability_identity_expiry_caps_lifetime(client, bundle, make_human):
    from dashboard_service.errors import now as svc_now
    from datetime import datetime
    soon = svc_now() + timedelta(seconds=5)
    token = make_human("short-lived", "Short",
                       expires_at=soon)
    created = publish_html(client, token, auth_mode="human").json()["result"]
    body = _issue(client, token, created["dashboard_id"]).json()
    expiry = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
    assert expiry <= soon + timedelta(seconds=2)


def test_no_open_static_path_to_content_files(content_client, human_board, bundle):
    _, created = human_board
    with bundle.database.read_only() as connection:
        version = connection.execute(
            sqlalchemy.select(models.dashboard_versions.c.storage_key).where(
                models.dashboard_versions.c.id == created["version_id"])).scalar_one()
    # Guessing the storage key finds no static route on either app.
    for path in (f"/{version}.html", f"/content/{version}.html",
                 f"/render/{version}.html", f"/static/{version}.html"):
        assert content_client.get(path).status_code == 404


def test_render_pages_served_with_csp(content_client):
    render = content_client.get("/render")
    assert render.status_code == 200
    policy = render.headers.get("content-security-policy", "")
    assert "frame-ancestors 'none'" in policy  # nothing may embed the loader
    assert "script-src 'self' 'unsafe-inline'" in policy
    assert "worker-src 'none'" in policy
    script = content_client.get("/render.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]


def test_content_app_has_no_management_routes(content_client, human_board):
    _, created = human_board
    # No control-API surface on the content origin.
    for path in ("/api/v1/me", "/api/v1/dashboards", "/api/v1/capabilities"):
        response = content_client.get(path)
        assert response.status_code == 404


# ------------------------------------------------- single-layer view flow

def test_view_loader_route_and_csp(client, content_client, human_board):
    """The content origin serves the single-layer trusted loader at
    /view/{id} with the control coordinates injected for the back link; no
    origin may embed it."""
    token, created = human_board
    dashboard_id = created["dashboard_id"]
    issued = _issue(client, token, dashboard_id).json()
    url = issued["render_url"]

    loader = content_client.get(f"/view/{dashboard_id}")
    assert loader.status_code == 200
    policy = loader.headers.get("content-security-policy", "")
    assert "frame-ancestors 'none'" in policy
    assert "frame-src about:" in policy
    assert loader.headers["cache-control"] == "no-store"
    body = loader.text
    assert f'<meta name="x-dashboard-id" content="{dashboard_id}">' in body
    assert ('<meta name="x-dashboard-control-origin" '
            'content="http://127.0.0.1:18080">') in body
    # The capability never appears in the served page.
    assert url.split("#", 1)[1] not in body

    # Legacy /render stays a standalone single-layer entry (no nesting added).
    legacy = content_client.get("/render")
    assert legacy.status_code == 200
    assert "x-dashboard-id" not in legacy.text

    # Malformed ids are rejected, not guessed.
    assert content_client.get("/view/not-a-uuid").status_code == 422


def test_control_routes_serve_launcher_and_manage(client, human_board):
    """/dashboards/{id} is the stable launcher; /manage is a compatibility
    notice; the legacy /view serves the launcher too. Control pages embed
    nothing: frame-src is absent (default-src 'none' denies frames)."""
    _, created = human_board
    dashboard_id = created["dashboard_id"]
    for path in (f"/dashboards/{dashboard_id}",
                 f"/dashboards/{dashboard_id}/view"):
        page = client.get(path)
        assert page.status_code == 200
        policy = page.headers.get("content-security-policy", "")
        assert "default-src 'none'" in policy
        assert "style-src 'self'" in policy
        assert "frame-src" not in policy  # no control-origin iframes at all
        assert 'href="/view.css"' in page.text  # R12: external CSS, no inline

    manage = client.get(f"/dashboards/{dashboard_id}/manage")
    assert manage.status_code == 200
    assert "AresClaw" in manage.text
    assert "<form" not in manage.text
    assert 'src="/app.js"' not in manage.text
    assert 'src="/view.js"' not in manage.text

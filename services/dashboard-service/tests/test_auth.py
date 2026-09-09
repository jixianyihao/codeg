"""T1: service JWT branch, account lifecycle, fixed auth-mode selection."""
import time

import jwt as pyjwt
import pytest

from .conftest import requires_mysql, service_headers

pytestmark = requires_mysql


def test_me_for_service_account(client, make_service_account):
    principal_id, token = make_service_account("ci-basic")
    response = client.get("/api/v1/me", headers=service_headers(token))
    assert response.status_code == 200
    body = response.json()
    assert body["principal_id"] == principal_id
    assert body["principal_type"] == "service"
    assert body["scopes"] == ["read", "write"]


def test_reset_invalidates_old_tokens(client, operator, make_service_account):
    _, old = make_service_account("ci-reset")
    assert client.get("/api/v1/me", headers=service_headers(old)).status_code == 200
    account = operator.list_accounts()[0]
    operator.reset_tokens(account["principal_id"])
    response = client.get("/api/v1/me", headers=service_headers(old))
    assert response.status_code == 401
    assert response.json()["code"] == "token_revoked"


def test_disable_then_enable_does_not_resurrect_tokens(client, operator, make_service_account):
    principal_id, old = make_service_account("ci-disable")
    operator.set_enabled(principal_id, False)
    assert client.get("/api/v1/me", headers=service_headers(old)).status_code == 401
    operator.set_enabled(principal_id, True)
    assert client.get("/api/v1/me", headers=service_headers(old)).status_code == 401
    fresh = operator.issue(principal_id)["token"]
    assert client.get("/api/v1/me", headers=service_headers(fresh)).status_code == 200


def test_scope_reduction_applies_immediately(client, operator, make_service_account):
    principal_id, token = make_service_account("ci-scopes")
    operator.set_scopes(principal_id, ["read"])
    from .conftest import publish_html
    response = publish_html(client, token)
    assert response.status_code == 403
    assert response.json()["code"] == "action_forbidden"


def test_tampered_signature_rejected(client, make_service_account):
    _, token = make_service_account("ci-tamper")
    tampered = token[:-6] + ("AAAAAA" if not token.endswith("AAAAAA") else "BBBBBB")
    assert client.get("/api/v1/me", headers=service_headers(tampered)).status_code == 401


def test_wrong_audience_rejected(client, operator, bundle, make_service_account):
    principal_id, _ = make_service_account("ci-aud")
    account = operator.list_accounts()[0]
    import uuid as _uuid
    wrong = pyjwt.encode(
        {"iss": bundle.config.issuer, "aud": "some-other-service", "sub": principal_id,
         "token_type": "service", "iat": int(time.time()), "exp": int(time.time()) + 600,
         "ver": account["token_version"]},
        bundle.config.jwt_secret, algorithm="HS256")
    response = client.get("/api/v1/me", headers=service_headers(wrong))
    assert response.status_code == 401


def test_expired_token_rejected(client, operator, bundle, make_service_account):
    principal_id, _ = make_service_account("ci-exp")
    account = operator.list_accounts()[0]
    expired = pyjwt.encode(
        {"iss": bundle.config.issuer, "aud": bundle.config.audience, "sub": principal_id,
         "token_type": "service", "iat": int(time.time()) - 7200,
         "exp": int(time.time()) - 3600, "ver": account["token_version"]},
        bundle.config.jwt_secret, algorithm="HS256")
    response = client.get("/api/v1/me", headers=service_headers(expired))
    assert response.status_code == 401
    assert response.json()["code"] == "token_expired"


def test_service_token_rejected_on_human_branch(client, make_service_account):
    """The selected branch decides — a service JWT never passes as human."""
    _, token = make_service_account("ci-branch")
    response = client.get("/api/v1/me", headers={
        "Authorization": f"Bearer {token}", "X-Dashboard-Auth-Mode": "human"})
    assert response.status_code in (401, 503)
    if response.status_code == 401:
        assert response.json()["code"] in ("invalid_token", "identity_unavailable")


def test_unknown_auth_mode_rejected(client, make_service_account):
    _, token = make_service_account("ci-mode")
    response = client.get("/api/v1/me", headers={
        "Authorization": f"Bearer {token}", "X-Dashboard-Auth-Mode": "both"})
    assert response.status_code == 400


def test_missing_bearer_rejected(client):
    assert client.get("/api/v1/me").status_code == 401


def test_human_me_via_fake_w3(client, bundle, make_human):
    token = make_human("u-alice", "Alice")
    response = client.get("/api/v1/me", headers={
        "Authorization": f"Bearer {token}", "X-Dashboard-Auth-Mode": "human"})
    assert response.status_code == 200
    body = response.json()
    assert body["principal_type"] == "human"
    assert body["display_name"] == "Alice"
    assert body["scopes"] == ["read", "write", "manage"]


def test_human_principal_stable_across_tokens(client, bundle, make_human):
    """(issuer, uid) maps to one principal; display-name changes don't
    re-map identity."""
    first = make_human("u-bob", "Bob")
    second = make_human("u-bob", "Bob Renamed")
    ids = set()
    for token in (first, second):
        body = client.get("/api/v1/me", headers={
            "Authorization": f"Bearer {token}", "X-Dashboard-Auth-Mode": "human"}).json()
        ids.add(body["principal_id"])
    assert len(ids) == 1


def test_capabilities_endpoint(client, make_service_account):
    _, token = make_service_account("ci-caps")
    body = client.get("/api/v1/capabilities", headers=service_headers(token)).json()
    assert body["api_major"] == 1
    assert {"publish", "create_draft", "save_draft", "publish_draft"} <= set(body["features"])
    assert body["max_upload_bytes"] == 10 * 1024 * 1024
    assert body["content_origin"].startswith("http://127.0.0.1:18081")
    assert "service_jwt" in body["auth_methods"]
    assert "w3" not in body["auth_methods"]  # not configured in tests

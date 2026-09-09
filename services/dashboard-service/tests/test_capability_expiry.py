"""T4: capability expiry edge cases."""
import uuid
from datetime import timedelta

import pytest
import sqlalchemy

from dashboard_service import models
from dashboard_service.database import to_db
from dashboard_service.errors import now as svc_now

from .conftest import human_headers, publish_html, requires_mysql

pytestmark = requires_mysql


def test_expired_capability_rejected(client, content_client, bundle, make_human):
    token = make_human("exp-owner", "ExpOwner")
    created = publish_html(client, token, auth_mode="human").json()["result"]
    issued = client.post(
        f"/api/v1/dashboards/{created['dashboard_id']}/view-capabilities",
        headers={**human_headers(token), "Idempotency-Key": str(uuid.uuid4())},
        json={}).json()
    fragment = issued["render_url"].split("#", 1)[1]
    import hashlib
    digest = hashlib.sha256(fragment.encode()).hexdigest()
    with bundle.database.transaction() as connection:
        connection.execute(models.view_capabilities.update().where(
            models.view_capabilities.c.token_digest == digest).values(
            expires_at=to_db(svc_now() - timedelta(seconds=1))))
    response = content_client.get("/content", headers={"Authorization": f"Bearer {fragment}"})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_capability"


def test_revocation_source_event_blocks_immediately(client, content_client, bundle,
                                                    make_human):
    """A verified revocation event must block new content reads at once."""
    from dashboard_service.capabilities import CapabilityService, IdentityRevocationSource

    class VerifiedRevocation(IdentityRevocationSource):
        def __init__(self):
            self.revoked_refs: set[str] = set()

        def is_revoked(self, issuer, session_ref, verified_at):
            if session_ref is None:
                return None
            return session_ref in self.revoked_refs

    revocation = VerifiedRevocation()
    bundle.service.capabilities.revocation = revocation
    token = make_human("sess-owner", "SessOwner", session_ref="sess-123")
    created = publish_html(client, token, auth_mode="human").json()["result"]
    issued = client.post(
        f"/api/v1/dashboards/{created['dashboard_id']}/view-capabilities",
        headers={**human_headers(token), "Idempotency-Key": str(uuid.uuid4())},
        json={}).json()
    fragment = issued["render_url"].split("#", 1)[1]
    ok = content_client.get("/content", headers={"Authorization": f"Bearer {fragment}"})
    assert ok.status_code == 200
    revocation.revoked_refs.add("sess-123")
    blocked = content_client.get("/content", headers={"Authorization": f"Bearer {fragment}"})
    assert blocked.status_code == 401


def test_unknown_session_ref_stays_bounded(client, content_client, bundle, make_human):
    """Without a verifiable session source, reads stay allowed for the
    capability's bounded lifetime (default revocation returns None)."""
    token = make_human("nosess-owner", "NoSess")
    created = publish_html(client, token, auth_mode="human").json()["result"]
    issued = client.post(
        f"/api/v1/dashboards/{created['dashboard_id']}/view-capabilities",
        headers={**human_headers(token), "Idempotency-Key": str(uuid.uuid4())},
        json={}).json()
    fragment = issued["render_url"].split("#", 1)[1]
    assert content_client.get(
        "/content", headers={"Authorization": f"Bearer {fragment}"}).status_code == 200
    # Unknown revocation never fabricates a block for humans without sessions.
    assert bundle.service.capabilities.revocation.is_revoked(None, None, None) is None


def test_capability_digest_only_no_token_stored(client, bundle, make_human):
    token = make_human("digest-owner", "Digest")
    created = publish_html(client, token, auth_mode="human").json()["result"]
    issued = client.post(
        f"/api/v1/dashboards/{created['dashboard_id']}/view-capabilities",
        headers={**human_headers(token), "Idempotency-Key": str(uuid.uuid4())},
        json={}).json()
    fragment = issued["render_url"].split("#", 1)[1]
    with bundle.database.read_only() as connection:
        rows = connection.execute(
            sqlalchemy.select(models.view_capabilities.c.token_digest,
                              models.view_capabilities.c.identity_session_ref)).all()
    assert rows
    import hashlib
    digests = {row[0] for row in rows}
    assert hashlib.sha256(fragment.encode()).hexdigest() in digests
    assert fragment not in digests  # the raw capability is never stored

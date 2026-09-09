"""End-to-end closed loop over the real service apps (real MySQL + S3):
machine publish → human grant → capability view → revoke → editor update →
rollback → archive/restore draft → explicit publication → list visibility."""
import hashlib
import uuid

from .conftest import (human_headers, multipart, publish_html, requires_mysql,
                       service_headers)

pytestmark = requires_mysql


def _sha(html: bytes) -> str:
    return hashlib.sha256(html).hexdigest()


def test_full_lifecycle_closed_loop(client, content_client, operator,
                                    make_service_account, make_human):
    # 1. A machine account publishes the first version (integration path).
    _, machine_token = make_service_account("loop-machine",
                                            ("read", "write", "manage"))
    v1 = b"<html><body><h1>v1</h1></body></html>"
    created = publish_html(client, machine_token, v1, title="闭环看板")
    assert created.status_code == 201, created.text
    board = created.json()["result"]
    dashboard_id = board["dashboard_id"]
    assert board["view_url"].endswith(f"/dashboards/{dashboard_id}")

    # The AresClaw card list sees it (service list API).
    listed = client.get("/api/v1/dashboards", headers=service_headers(machine_token))
    assert listed.status_code == 200
    assert any(item["id"] == dashboard_id for item in listed.json()["items"])

    # 2. A human is granted viewer access by... the owner. The machine
    # account owns the board; grant editor-to-human via the machine.
    human_token = make_human("loop-human", "闭环用户")
    human_id = client.get("/api/v1/me", headers=human_headers(human_token)).json()["principal_id"]
    granted = client.post(
        f"/api/v1/dashboards/{dashboard_id}/grants",
        headers={**service_headers(machine_token), "Idempotency-Key": str(uuid.uuid4())},
        json={"subject_type": "user", "subject_id": human_id, "role": "editor",
              "expected_revision": 1})
    assert granted.status_code == 200, granted.text

    # 3. The human views: capability minted on the control origin, redeemed
    # on the content origin, exactly the published bytes come back.
    issued = client.post(
        f"/api/v1/dashboards/{dashboard_id}/view-capabilities",
        headers={**human_headers(human_token), "Idempotency-Key": str(uuid.uuid4())},
        json={})
    assert issued.status_code == 200
    render_url = issued.json()["render_url"]
    assert render_url.startswith("http://127.0.0.1:18081/view/")
    capability = render_url.split("#", 1)[1]
    content = content_client.get("/content",
                                 headers={"Authorization": f"Bearer {capability}"})
    assert content.status_code == 200
    assert content.content == v1

    # The machine credential is not a capability and is refused on /content.
    assert content_client.get(
        "/content", headers={"Authorization": f"Bearer {machine_token}",
                             "X-Dashboard-Auth-Mode": "service"}).status_code == 401

    # 4. Revoke: a fresh capability is refused; the already-granted one from
    # step 3 is short-lived and likewise refused after the ACL drop.
    revoked = client.delete(
        f"/api/v1/dashboards/{dashboard_id}/grants/user/{human_id}?expected_revision=2",
        headers={**service_headers(machine_token), "Idempotency-Key": str(uuid.uuid4())})
    assert revoked.status_code == 200
    denied = client.post(
        f"/api/v1/dashboards/{dashboard_id}/view-capabilities",
        headers={**human_headers(human_token), "Idempotency-Key": str(uuid.uuid4())},
        json={})
    assert denied.status_code == 404
    assert content_client.get(
        "/content", headers={"Authorization": f"Bearer {capability}"}).status_code == 401

    # 5. Re-grant as viewer; the human cannot write.
    assert client.post(
        f"/api/v1/dashboards/{dashboard_id}/grants",
        headers={**service_headers(machine_token), "Idempotency-Key": str(uuid.uuid4())},
        json={"subject_type": "user", "subject_id": human_id, "role": "viewer",
              "expected_revision": 3}).status_code == 200
    v2 = b"<html><body><h1>v2</h1></body></html>"
    human_write = publish_html(client, human_token, v2, dashboard_id=dashboard_id,
                               expected_revision=3, auth_mode="human")
    assert human_write.status_code == 403

    # 6. The owner publishes v2; version history and rollback work.
    revision = client.get(f"/api/v1/dashboards/{dashboard_id}",
                          headers=service_headers(machine_token)).json()["revision"]
    updated = publish_html(client, machine_token, v2, dashboard_id=dashboard_id,
                           expected_revision=revision)
    assert updated.status_code == 201
    version_two = updated.json()["result"]
    versions = client.get(f"/api/v1/dashboards/{dashboard_id}/versions",
                          headers=service_headers(machine_token)).json()["items"]
    assert [v["number"] for v in versions] == [2, 1]

    rolled = client.post(
        f"/api/v1/dashboards/{dashboard_id}/rollback",
        headers={**service_headers(machine_token), "Idempotency-Key": str(uuid.uuid4())},
        json={"version_id": board["version_id"],
              "expected_revision": version_two["revision"]})
    assert rolled.status_code == 200
    assert rolled.json()["result"]["version_number"] == 1

    # 7. Source download returns the exact published bytes of v2.
    source = client.get(
        f"/api/v1/dashboards/{dashboard_id}/versions/{version_two['version_id']}/source",
        headers=service_headers(machine_token))
    assert source.status_code == 200
    assert source.content == v2
    assert source.headers["content-type"].startswith("text/plain")

    # 8. Archive blocks access. Restore prepares a private draft; only an
    # explicit owner publication restores the existing viewer's access.
    revision = client.get(f"/api/v1/dashboards/{dashboard_id}",
                          headers=service_headers(machine_token)).json()["revision"]
    archived = client.post(
        f"/api/v1/dashboards/{dashboard_id}/archive",
        headers={**service_headers(machine_token), "Idempotency-Key": str(uuid.uuid4())},
        json={"expected_revision": revision})
    assert archived.status_code == 200
    assert client.get(f"/api/v1/dashboards/{dashboard_id}",
                      headers=human_headers(human_token)).status_code == 404
    restored = client.post(
        f"/api/v1/dashboards/{dashboard_id}/restore",
        headers={**service_headers(machine_token), "Idempotency-Key": str(uuid.uuid4())},
        json={"expected_revision": archived.json()["result"]["revision"]})
    assert restored.status_code == 200
    restored_result = restored.json()["result"]
    assert restored_result["status"] == "draft"
    assert restored_result["draft_version_id"] == board["version_id"]
    assert client.get(f"/api/v1/dashboards/{dashboard_id}",
                      headers=human_headers(human_token)).status_code == 404
    assert client.post(
        f"/api/v1/dashboards/{dashboard_id}/view-capabilities",
        headers=human_headers(human_token), json={}).status_code == 404
    republished = client.post(
        f"/api/v1/dashboards/{dashboard_id}/publish",
        headers={**service_headers(machine_token), "Idempotency-Key": str(uuid.uuid4())},
        json={"version_id": restored_result["draft_version_id"],
              "expected_revision": restored_result["revision"]})
    assert republished.status_code == 200, republished.text
    assert republished.json()["result"]["status"] == "published"
    assert republished.json()["result"]["draft_version_id"] is None
    assert client.get(f"/api/v1/dashboards/{dashboard_id}",
                      headers=human_headers(human_token)).status_code == 200
    reissued = client.post(
        f"/api/v1/dashboards/{dashboard_id}/view-capabilities",
        headers=human_headers(human_token), json={})
    assert reissued.status_code == 200
    restored_capability = reissued.json()["render_url"].split("#", 1)[1]
    assert content_client.get("/content", headers={
        "Authorization": f"Bearer {restored_capability}"}).content == v1

    # 9. Final integrity: S3 objects behind both versions verify byte-exact.
    from dashboard_service.storage import ObjectRef
    import sqlalchemy
    from dashboard_service import models
    with client.app.state.service.database.read_only() as connection:
        rows = connection.execute(
            sqlalchemy.select(models.dashboard_versions.c.storage_bucket,
                              models.dashboard_versions.c.storage_key,
                              models.dashboard_versions.c.sha256,
                              models.dashboard_versions.c.byte_size)
            .where(models.dashboard_versions.c.dashboard_id == dashboard_id)
            .order_by(models.dashboard_versions.c.number)).all()
    store = client.app.state.service.store
    expected = {1: v1, 2: v2}
    for (bucket, key, sha, size), (_, html) in zip(rows, sorted(expected.items())):
        ref = ObjectRef(bucket=bucket, key=key)
        assert store.object_present(ref)
        assert store.get_verified(ref, sha, size) == html

"""T2: private publish, immutable versions, idempotency, revision checks.

Every assertion about stored state queries the real MySQL database —
responses are never treated as proof of database state.
"""
import json
import uuid

import pytest
import sqlalchemy

from dashboard_service import models

from .conftest import committed_version_rows, publish_html, requires_mysql, service_headers

pytestmark = requires_mysql


def test_publish_creates_private_dashboard(client, bundle, make_service_account):
    _, token = make_service_account("ci-pub")
    response = publish_html(client, token, b"<html>v1</html>", title="Weekly")
    assert response.status_code == 201, response.text
    wrapper = response.json()
    assert wrapper["state"] == "succeeded"
    result = wrapper["result"]
    assert result["version_number"] == 1
    assert result["revision"] == 1
    assert result["view_url"].endswith(f"/dashboards/{result['dashboard_id']}")
    rows = committed_version_rows(bundle, result["dashboard_id"])
    assert [r[1] for r in rows] == [1]
    # The stored immutable file matches the declared bytes.
    with bundle.database.read_only() as connection:
        from dashboard_service import models
        version = connection.execute(
            sqlalchemy.select(models.dashboard_versions).where(
                models.dashboard_versions.c.id == result["version_id"])).mappings().one()
    stored = bundle.service.store.read_version(version["storage_key"], version["sha256"],
                                                version["byte_size"])
    assert stored == b"<html>v1</html>"


def test_same_key_same_bytes_returns_single_version(client, bundle, make_service_account):
    _, token = make_service_account("ci-idem")
    key = str(uuid.uuid4())
    html = b"<html>same</html>"
    first = publish_html(client, token, html, key=key).json()
    second = publish_html(client, token, html, key=key).json()
    assert first["operation_id"] == second["operation_id"]
    assert first["result"]["version_id"] == second["result"]["version_id"]
    assert len(committed_version_rows(bundle, first["result"]["dashboard_id"])) == 1


def test_same_key_different_bytes_conflicts(client, make_service_account):
    _, token = make_service_account("ci-conflict")
    key = str(uuid.uuid4())
    first = publish_html(client, token, b"<html>a</html>", key=key)
    assert first.status_code == 201
    second = publish_html(client, token, b"<html>b</html>", key=key)
    assert second.status_code == 409
    assert second.json()["code"] == "idempotency_conflict"


def test_stale_revision_rejected(client, make_service_account):
    _, token = make_service_account("ci-rev")
    created = publish_html(client, token, b"<html>1</html>").json()["result"]
    dashboard_id = created["dashboard_id"]
    update = publish_html(client, token, b"<html>2</html>",
                          dashboard_id=dashboard_id, expected_revision=1)
    assert update.status_code == 201
    stale = publish_html(client, token, b"<html>3</html>",
                         dashboard_id=dashboard_id, expected_revision=1)
    assert stale.status_code == 409
    assert stale.json()["code"] == "revision_conflict"


def test_other_service_account_cannot_read_private(client, operator, make_service_account):
    _, owner_token = make_service_account("ci-owner")
    _, other_token = make_service_account("ci-other")
    created = publish_html(client, owner_token).json()["result"]
    detail = client.get(f"/api/v1/dashboards/{created['dashboard_id']}",
                        headers=service_headers(other_token))
    assert detail.status_code == 404
    versions = client.get(f"/api/v1/dashboards/{created['dashboard_id']}/versions",
                          headers=service_headers(other_token))
    assert versions.status_code == 404
    source = client.get(
        f"/api/v1/dashboards/{created['dashboard_id']}/versions/{created['version_id']}/source",
        headers=service_headers(other_token))
    assert source.status_code == 404


def test_hash_mismatch_rejected_and_no_version_committed(client, bundle, make_service_account):
    _, token = make_service_account("ci-hash")
    html = b"<html>x</html>"
    metadata = {"title": "Board", "content_sha256": "0" * 64, "byte_size": len(html)}
    response = client.post(
        "/api/v1/dashboards",
        headers={**service_headers(token), "Idempotency-Key": str(uuid.uuid4())},
        data={"metadata": json.dumps(metadata)},
        files={"html": ("d.html", html, "text/html")})
    assert response.status_code == 422
    assert response.json()["code"] == "hash_mismatch"
    with bundle.database.read_only() as connection:
        count = connection.execute(
            sqlalchemy.select(sqlalchemy.func.count()).select_from(models.dashboard_versions)
        ).scalar_one()
    assert count == 0


def test_invalid_utf8_rejected(client, make_service_account):
    _, token = make_service_account("ci-utf8")
    bad = b"<html>\xff\xfe bad</html>"
    response = publish_html(client, token, bad)
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_html"


def test_oversize_upload_rejected(client, make_service_account):
    _, token = make_service_account("ci-size")
    huge = b"<html>" + b"x" * (10 * 1024 * 1024 + 10)
    response = publish_html(client, token, huge)
    assert response.status_code in (413, 422)


def test_update_flow_versions_and_rollback(client, bundle, make_service_account):
    _, token = make_service_account("ci-flow")
    v1 = publish_html(client, token, b"<html>v1</html>", title="T").json()["result"]
    dashboard_id = v1["dashboard_id"]
    v2 = publish_html(client, token, b"<html>v2</html>", dashboard_id=dashboard_id,
                      expected_revision=1).json()["result"]
    assert v2["version_number"] == 2
    assert v2["revision"] == 2
    listing = client.get(f"/api/v1/dashboards/{dashboard_id}/versions",
                         headers=service_headers(token)).json()
    assert [item["number"] for item in listing["items"]] == [2, 1]
    source = client.get(
        f"/api/v1/dashboards/{dashboard_id}/versions/{v1['version_id']}/source",
        headers=service_headers(token))
    assert source.status_code == 200
    assert source.content == b"<html>v1</html>"
    assert source.headers["content-type"].startswith("text/plain")

    rollback = client.post(
        f"/api/v1/dashboards/{dashboard_id}/rollback",
        headers={**service_headers(token), "Idempotency-Key": str(uuid.uuid4())},
        json={"version_id": v1["version_id"], "expected_revision": v2["revision"]})
    assert rollback.status_code == 200
    result = rollback.json()["result"]
    assert result["revision"] == 3
    # Rollback does not create a new version — only moves the pointer.
    rows = committed_version_rows(bundle, dashboard_id)
    assert [r[1] for r in rows] == [1, 2]
    detail = client.get(f"/api/v1/dashboards/{dashboard_id}",
                        headers=service_headers(token)).json()
    assert detail["current_version_id"] == v1["version_id"]
    assert detail["current_version_number"] == 1


def test_non_uuid_idempotency_key_rejected(client, make_service_account):
    _, token = make_service_account("ci-key")
    response = publish_html(client, token, key="not-a-uuid")
    assert response.status_code == 422


def test_missing_expected_revision_on_update(client, make_service_account):
    _, token = make_service_account("ci-upd")
    created = publish_html(client, token).json()["result"]
    response = publish_html(client, token, b"<html>2</html>",
                            dashboard_id=created["dashboard_id"])
    assert response.status_code == 422


def test_patch_title_bumps_revision_only(client, make_service_account):
    _, token = make_service_account("ci-patch")
    created = publish_html(client, token).json()["result"]
    before = client.get(f"/api/v1/dashboards/{created['dashboard_id']}",
                        headers=service_headers(token)).json()
    response = client.patch(
        f"/api/v1/dashboards/{created['dashboard_id']}",
        headers={**service_headers(token), "Idempotency-Key": str(uuid.uuid4())},
        json={"title": "Renamed", "expected_revision": 1})
    assert response.status_code == 200
    assert response.json()["result"]["revision"] == 2
    detail = client.get(f"/api/v1/dashboards/{created['dashboard_id']}",
                        headers=service_headers(token)).json()
    assert detail["title"] == "Renamed"
    # A rename must not change published_at (content publish time).
    assert detail["published_at"] == before["published_at"]


def test_operation_query_by_request_id(client, make_service_account):
    _, token = make_service_account("ci-op")
    key = str(uuid.uuid4())
    created = publish_html(client, token, key=key).json()
    found = client.get(f"/api/v1/operations?request_id={key}",
                       headers=service_headers(token))
    assert found.status_code == 200
    assert found.json()["operation_id"] == created["operation_id"]
    by_id = client.get(f"/api/v1/operations/{created['operation_id']}",
                       headers=service_headers(token))
    assert by_id.status_code == 200
    # Another principal cannot see it.
    _, other = make_service_account("ci-op2")
    assert client.get(f"/api/v1/operations?request_id={key}",
                      headers=service_headers(other)).status_code == 404

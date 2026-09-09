"""Byte-exact version summaries, metadata privacy, and source response identity."""
import hashlib

import pytest

from .conftest import human_headers, publish_html, requires_mysql
from .test_access import _grant
from .test_draft_lifecycle import action, detail, save, write_headers

pytestmark = requires_mysql

SUMMARY_FIELDS = ("current_version_sha256", "current_version_byte_size",
                  "draft_version_sha256", "draft_version_byte_size")
LIVE = "<html>\r\n  <body>本期</body>\r\n</html>\r\n".encode()
DRAFT = "<html>\n <body>下一期</body>\n</html>\n".encode()


def _summary(current=None, draft=None):
    return dict(zip(SUMMARY_FIELDS, (
        hashlib.sha256(current).hexdigest() if current is not None else None,
        len(current) if current is not None else None,
        hashlib.sha256(draft).hexdigest() if draft is not None else None,
        len(draft) if draft is not None else None,
    ), strict=True))


def _assert_summary(board, current=None, draft=None):
    assert {key: board[key] for key in SUMMARY_FIELDS} == _summary(current, draft)


def _listed(client, token, dashboard_id, status="published"):
    response = client.get(f"/api/v1/dashboards?status={status}",
                          headers=human_headers(token))
    assert response.status_code == 200, response.text
    return next(item for item in response.json()["items"] if item["id"] == dashboard_id)


@pytest.mark.parametrize("role", ["owner", "editor", "viewer", None])
def test_list_and_detail_hashes_follow_content_access_without_s3_reads(
        client, bundle, make_human, monkeypatch, role):
    owner = make_human("hash-owner")
    published = publish_html(client, owner, LIVE, auth_mode="human").json()["result"]
    did = published["dashboard_id"]
    saved = save(client, owner, DRAFT, dashboard_id=did, revision=1)
    assert saved.status_code == 201, saved.text
    actor = owner if role == "owner" else make_human("hash-reader")
    if role in ("editor", "viewer"):
        actor_id = client.get("/api/v1/me", headers=human_headers(actor)).json()["principal_id"]
        assert _grant(client, owner, did, "user", actor_id, role,
                      expected_revision=2).status_code == 200

    def unavailable(*args, **kwargs):
        pytest.fail("List/detail must read persisted summaries without accessing S3")

    monkeypatch.setattr(bundle.service.store, "read_version", unavailable)
    monkeypatch.setattr(bundle.service.store, "_get_bytes", unavailable)
    current = LIVE if role is not None else None
    draft = DRAFT if role in ("owner", "editor") else None
    listed = _listed(client, actor, did)
    assert listed["role"] == role
    _assert_summary(listed, current, draft)
    if role is None:
        assert client.get(f"/api/v1/dashboards/{did}",
                          headers=human_headers(actor)).status_code == 404
    else:
        _assert_summary(detail(client, actor, did), current, draft)


def test_blank_draft_and_archived_owner_summaries(client, make_human):
    owner = make_human("hash-states-owner")
    created = client.post("/api/v1/dashboards/drafts", headers=write_headers(owner),
                          json={"title": "Empty"})
    assert created.status_code == 201, created.text
    did = created.json()["result"]["dashboard_id"]
    _assert_summary(detail(client, owner, did))
    _assert_summary(_listed(client, owner, did, "draft"))

    saved = save(client, owner, DRAFT, dashboard_id=did, revision=1)
    assert saved.status_code == 201, saved.text
    _assert_summary(detail(client, owner, did), draft=DRAFT)
    _assert_summary(_listed(client, owner, did, "draft"), draft=DRAFT)

    published = save(client, owner, LIVE, dashboard_id=did, revision=2, disposition="publish")
    assert published.status_code == 201, published.text
    assert action(client, owner, did, "archive", 3).status_code == 200
    _assert_summary(detail(client, owner, did), LIVE, DRAFT)
    _assert_summary(_listed(client, owner, did, "archived"), LIVE, DRAFT)
    for version in (saved.json()["result"]["version_id"], published.json()["result"]["version_id"]):
        source = client.get(f"/api/v1/dashboards/{did}/versions/{version}/source",
                            headers=human_headers(owner))
        assert source.status_code == 404
        assert "x-content-sha256" not in source.headers
        assert "x-dashboard-version-id" not in source.headers


def test_source_identifies_exact_immutable_bytes_after_acl_and_integrity_checks(
        client, bundle, make_human, monkeypatch):
    owner = make_human("source-hash-owner")
    first = publish_html(client, owner, LIVE, auth_mode="human").json()["result"]
    did, version_id = first["dashboard_id"], first["version_id"]
    # A newline-only change is a different version; old source remains byte-exact.
    normalized = LIVE.replace(b"\r\n", b"\n")
    update = publish_html(client, owner, normalized, dashboard_id=did,
                          expected_revision=1, auth_mode="human")
    assert update.status_code == 201, update.text
    _assert_summary(detail(client, owner, did), normalized)
    path = f"/api/v1/dashboards/{did}/versions/{version_id}/source"
    source = client.get(path, headers=human_headers(owner))
    assert source.status_code == 200, source.text
    assert source.content == LIVE
    assert source.headers["x-content-sha256"] == hashlib.sha256(LIVE).hexdigest()
    assert source.headers["x-dashboard-version-id"] == version_id
    assert source.headers["content-type"].startswith("text/plain")
    assert source.headers["content-disposition"].startswith("attachment;")

    stranger = make_human("source-hash-stranger")
    denied = client.get(path, headers=human_headers(stranger))
    assert denied.status_code == 404
    assert "x-content-sha256" not in denied.headers
    assert "x-dashboard-version-id" not in denied.headers
    # Exercise the real read_version/get_verified integrity guard with bad bytes.
    monkeypatch.setattr(bundle.service.store, "_get_bytes", lambda *args: b"x" * len(LIVE))
    corrupt = client.get(path, headers=human_headers(owner))
    assert corrupt.status_code == 503
    assert corrupt.json()["code"] == "storage_unavailable"
    assert "x-content-sha256" not in corrupt.headers
    assert "x-dashboard-version-id" not in corrupt.headers

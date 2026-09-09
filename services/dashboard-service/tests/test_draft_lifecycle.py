"""Draft/publication lifecycle through real HTTP, MySQL and immutable S3 objects."""
import hashlib
import uuid

import pytest
from sqlalchemy import select

from dashboard_service import models

from .conftest import (
    human_headers,
    multipart,
    publish_html,
    requires_mysql,
    service_headers,
)
from .test_access import _grant
from .test_content import _issue

pytestmark = requires_mysql


def write_headers(token, mode="human", key=None):
    return {**(human_headers(token) if mode == "human" else service_headers(token)),
            "Idempotency-Key": key or str(uuid.uuid4())}


def save(client, token, html=b"<html>draft</html>", *, dashboard_id=None,
         revision=None, disposition="save_draft", **extra):
    metadata = {"content_sha256": hashlib.sha256(html).hexdigest(),
                "byte_size": len(html), "disposition": disposition, **extra}
    if dashboard_id is None:
        metadata.setdefault("title", "Draft board")
    else:
        metadata["expected_revision"] = revision
    path = "/api/v1/dashboards" + (f"/{dashboard_id}/versions" if dashboard_id else "")
    return client.post(path, headers=write_headers(token), **multipart(metadata, html))


def detail(client, token, dashboard_id):
    response = client.get(f"/api/v1/dashboards/{dashboard_id}", headers=human_headers(token))
    assert response.status_code == 200, response.text
    return response.json()


def action(client, token, dashboard_id, name, revision, **fields):
    return client.post(f"/api/v1/dashboards/{dashboard_id}/{name}",
                       headers=write_headers(token),
                       json={"expected_revision": revision, **fields})


def test_empty_draft_is_private_idempotent_and_counts_quota(client, bundle, make_human):
    owner, stranger = make_human("empty-owner"), make_human("empty-stranger")
    key = str(uuid.uuid4())
    headers = write_headers(owner, key=key)
    created = client.post("/api/v1/dashboards/drafts", headers=headers,
                          json={"title": "Empty", "description": "Keep me"})
    assert created.status_code == 201, created.text
    result = created.json()["result"]
    did = result["dashboard_id"]
    assert result["status"] == "draft" and result["disposition"] == "save_draft"
    assert client.post("/api/v1/dashboards/drafts", headers=headers,
                       json={"title": "Empty", "description": "Keep me"}).json() == created.json()
    board = detail(client, owner, did)
    assert board["current_version_id"] is None and board["draft_version_id"] is None
    assert board["published_at"] is None and not board["has_draft"]
    assert client.get(f"/api/v1/dashboards/{did}", headers=human_headers(stranger)).status_code == 404
    assert client.get("/api/v1/dashboards?status=draft", headers=human_headers(stranger)).json()["items"] == []
    assert _issue(client, owner, did).status_code == 404
    with bundle.database.read_only() as connection:
        usage = connection.execute(select(models.quota_usage).where(
            models.quota_usage.c.scope == "global")).mappings().one()
        assert usage["dashboard_count"] == 1 and usage["used_bytes"] == 0


def test_live_draft_versions_stay_private_until_explicit_publication(
        client, content_client, bundle, make_human):
    owner, viewer = make_human("live-owner"), make_human("live-viewer")
    original = publish_html(client, owner, auth_mode="human").json()["result"]
    did = original["dashboard_id"]
    viewer_id = client.get("/api/v1/me", headers=human_headers(viewer)).json()["principal_id"]
    assert _grant(client, owner, did, "user", viewer_id).status_code == 200
    before = detail(client, owner, did)
    first = save(client, owner, dashboard_id=did, revision=2)
    assert first.status_code == 201, first.text
    assert first.json()["result"]["draft_version_number"] == 2
    assert first.json()["result"]["current_version_id"] == original["version_id"]
    v2 = first.json()["result"]["version_id"]
    second = save(client, owner, b"<html>draft three</html>", dashboard_id=did, revision=3)
    assert second.status_code == 201, second.text
    v3 = second.json()["result"]["version_id"]
    board = detail(client, owner, did)
    assert board["current_version_id"] == original["version_id"]
    assert board["draft_version_id"] == v3 and board["draft_version_number"] == 3
    assert board["published_at"] == before["published_at"] and board["description"] == "desc"
    viewer_board = detail(client, viewer, did)
    assert viewer_board["draft_version_id"] is None and not viewer_board["has_draft"]
    history = client.get(f"/api/v1/dashboards/{did}/versions", headers=human_headers(viewer)).json()["items"]
    assert [v["id"] for v in history] == [original["version_id"]]
    for version in (v2, v3):
        assert client.get(f"/api/v1/dashboards/{did}/versions/{version}/source",
                          headers=human_headers(viewer)).status_code == 404
        assert _issue(client, viewer, did, {"version_id": version}).status_code == 404
    fragment = _issue(client, viewer, did).json()["render_url"].split("#")[1]
    assert content_client.get("/content", headers={"Authorization": f"Bearer {fragment}"}).content == b"<html><body>ok</body></html>"
    assert action(client, owner, did, "publish", 4, version_id=v2).status_code == 409
    assert action(client, owner, did, "rollback", 4, version_id=v3).status_code == 404
    assert action(client, owner, did, "publish", 4, version_id=v3).status_code == 200
    board = detail(client, owner, did)
    assert board["current_version_id"] == v3 and board["draft_version_id"] is None
    with bundle.database.read_only() as connection:
        rows = connection.execute(select(models.dashboard_versions).where(
            models.dashboard_versions.c.dashboard_id == did)).mappings().all()
        assert {r["id"] for r in rows if r["published_at"] is None} == {v2}


def test_restore_never_reopens_audience_and_editor_cannot_reactivate(client, make_human):
    owner, editor, stranger = (make_human("restore-owner"), make_human("restore-editor"),
                               make_human("restore-stranger"))
    created = publish_html(client, owner, auth_mode="human").json()["result"]
    did = created["dashboard_id"]
    editor_id = client.get("/api/v1/me", headers=human_headers(editor)).json()["principal_id"]
    assert _grant(client, owner, did, "user", editor_id, "editor").status_code == 200
    assert client.put(f"/api/v1/dashboards/{did}/public-access", headers=write_headers(owner),
                      json={"enabled": True, "expected_revision": 2}).status_code == 200
    assert action(client, owner, did, "archive", 3).status_code == 200
    assert _issue(client, owner, did).status_code == 404
    restored = action(client, owner, did, "restore", 4)
    assert restored.status_code == 200 and restored.json()["result"]["status"] == "draft"
    board = detail(client, owner, did)
    assert board["draft_version_id"] == created["version_id"]
    assert client.get(f"/api/v1/dashboards/{did}", headers=human_headers(stranger)).status_code == 404
    assert _issue(client, owner, did).status_code == 200
    assert action(client, editor, did, "publish", 5, version_id=created["version_id"]).status_code == 403
    saved = save(client, editor, dashboard_id=did, revision=5)
    assert saved.status_code == 201, saved.text
    assert save(client, editor, dashboard_id=did, revision=6, disposition="publish").status_code == 403
    assert action(client, owner, did, "publish", 6,
                  version_id=saved.json()["result"]["version_id"]).status_code == 200


def test_direct_publish_and_rollback_preserve_independent_draft(client, make_human):
    owner = make_human("independent-owner")
    created = save(client, owner)
    assert created.status_code == 201, created.text
    result = created.json()["result"]
    did, candidate = result["dashboard_id"], result["version_id"]
    direct = save(client, owner, b"<html>live</html>", dashboard_id=did,
                  revision=1, disposition="publish")
    assert direct.status_code == 201, direct.text
    live = direct.json()["result"]["version_id"]
    assert detail(client, owner, did)["draft_version_id"] == candidate
    newer = save(client, owner, b"<html>newer live</html>", dashboard_id=did,
                 revision=2, disposition="publish")
    assert newer.status_code == 201
    assert action(client, owner, did, "rollback", 3, version_id=live).status_code == 200
    assert detail(client, owner, did)["draft_version_id"] == candidate


def test_shared_human_listing_only_includes_effective_grants(client, make_human):
    owner, reader = make_human("shared-owner"), make_human("shared-reader")
    created = publish_html(client, owner, auth_mode="human").json()["result"]
    assert len(client.get("/api/v1/dashboards?scope=all", headers=human_headers(reader)).json()["items"]) == 1
    assert client.get("/api/v1/dashboards?scope=shared", headers=human_headers(reader)).json()["items"] == []
    reader_id = client.get("/api/v1/me", headers=human_headers(reader)).json()["principal_id"]
    assert _grant(client, owner, created["dashboard_id"], "user", reader_id).status_code == 200
    assert len(client.get("/api/v1/dashboards?scope=shared", headers=human_headers(reader)).json()["items"]) == 1
    assert action(client, owner, created["dashboard_id"], "archive", 2).status_code == 200
    # Archived metadata belongs in the owner's management list, not shared.
    assert len(client.get("/api/v1/dashboards?scope=all&status=archived",
                          headers=human_headers(owner)).json()["items"]) == 1
    assert client.get("/api/v1/dashboards?scope=shared&status=archived",
                      headers=human_headers(owner)).json()["items"] == []


def test_unpublished_capability_rechecks_editor_role_after_downgrade(
        client, content_client, make_human):
    owner, editor = make_human("cap-owner"), make_human("cap-editor")
    created = publish_html(client, owner, auth_mode="human").json()["result"]
    did = created["dashboard_id"]
    editor_id = client.get("/api/v1/me", headers=human_headers(editor)).json()["principal_id"]
    assert _grant(client, owner, did, "user", editor_id, "editor").status_code == 200
    saved = save(client, editor, dashboard_id=did, revision=2).json()["result"]
    issued = _issue(client, editor, did, {"version_id": saved["version_id"]})
    assert issued.status_code == 200, issued.text
    fragment = issued.json()["render_url"].split("#")[1]
    assert content_client.get("/content", headers={"Authorization": f"Bearer {fragment}"}).status_code == 200
    assert _grant(client, owner, did, "user", editor_id, "viewer", expected_revision=3).status_code == 200
    assert content_client.get("/content", headers={"Authorization": f"Bearer {fragment}"}).status_code == 401


def test_blank_draft_quota_and_write_only_service_publication(client, bundle, make_service_account):
    _, token = make_service_account("draft-machine", ("read", "write"))
    from dataclasses import replace
    bundle.service.config = replace(bundle.service.config, max_owner_dashboards=1)
    first = client.post("/api/v1/dashboards/drafts", headers=write_headers(token, "service"),
                        json={"title": "Machine draft"})
    assert first.status_code == 201, first.text
    second = client.post("/api/v1/dashboards/drafts", headers=write_headers(token, "service"),
                         json={"title": "Over quota"})
    assert second.status_code == 507
    did = first.json()["result"]["dashboard_id"]
    uploaded = publish_html(client, token, dashboard_id=did, expected_revision=1)
    assert uploaded.status_code == 201, uploaded.text
    assert uploaded.json()["result"]["status"] == "published"


def test_saved_draft_survives_archive_restore_and_default_preview_is_draft(
        client, content_client, make_human):
    owner = make_human("preserve-restore-owner")
    created = publish_html(client, owner, auth_mode="human").json()["result"]
    did = created["dashboard_id"]
    saved = save(client, owner, dashboard_id=did, revision=1).json()["result"]
    assert action(client, owner, did, "archive", 2).status_code == 200
    assert action(client, owner, did, "restore", 3).status_code == 200
    board = detail(client, owner, did)
    assert board["draft_version_id"] == saved["version_id"]
    fragment = _issue(client, owner, did).json()["render_url"].split("#")[1]
    assert content_client.get("/content", headers={"Authorization": f"Bearer {fragment}"}).content == b"<html>draft</html>"


def test_save_racing_draft_publication_cannot_replace_winning_pointer(client, bundle, make_human):
    import io
    import threading

    from dashboard_service.errors import ApiError
    owner = make_human("race-owner")
    created = save(client, owner).json()["result"]
    did, candidate = created["dashboard_id"], created["version_id"]
    actor = bundle.service.authenticator.authenticate(
        {"authorization": f"Bearer {owner}", "x-dashboard-auth-mode": "human"})
    arrived, resume = threading.Event(), threading.Event()
    errors = []

    def reverify():
        arrived.set()
        assert resume.wait(20)
        return actor

    def pending_save():
        html = b"<html>losing draft</html>"
        try:
            bundle.service.publisher.publish(
                actor, reverify, str(uuid.uuid4()), dashboard_id=did,
                metadata={"disposition": "save_draft", "byte_size": len(html),
                          "content_sha256": hashlib.sha256(html).hexdigest(), "expected_revision": 1},
                html_stream=io.BytesIO(html))
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=pending_save)
    worker.start()
    try:
        assert arrived.wait(20)
        assert action(client, owner, did, "publish", 1, version_id=candidate).status_code == 200
    finally:
        resume.set()
        worker.join(20)
    assert not worker.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], ApiError)
    assert errors[0].code == "revision_conflict"
    board = detail(client, owner, did)
    assert board["current_version_id"] == candidate and board["draft_version_id"] is None
    versions = client.get(f"/api/v1/dashboards/{did}/versions", headers=human_headers(owner)).json()["items"]
    assert [version["id"] for version in versions] == [candidate]


def test_lifecycle_migration_preserves_old_published_and_archived_history(
        client, bundle, make_human):
    from pathlib import Path

    from alembic.config import Config as AlembicConfig

    from alembic import command
    owner = make_human("migration-owner")
    first = publish_html(client, owner, auth_mode="human").json()["result"]
    did = first["dashboard_id"]
    second = publish_html(client, owner, b"<html>history</html>", auth_mode="human",
                          dashboard_id=did, expected_revision=1).json()["result"]
    assert action(client, owner, did, "archive", 2).status_code == 200
    with bundle.database.read_only() as connection:
        before = dict(connection.execute(select(models.dashboards).where(
            models.dashboards.c.id == did)).mappings().one())
    config = AlembicConfig(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    # This uses only the disposable, explicitly configured test database.
    try:
        command.downgrade(config, "b41d7c2f9013")
        with pytest.raises(RuntimeError, match="out of date"):
            bundle.database.verify_schema()
    finally:
        command.upgrade(config, "head")
    with bundle.database.read_only() as connection:
        after = dict(connection.execute(select(models.dashboards).where(
            models.dashboards.c.id == did)).mappings().one())
        versions = connection.execute(select(models.dashboard_versions).where(
            models.dashboard_versions.c.dashboard_id == did)).mappings().all()
    assert after == before
    assert after["status"] == "archived" and after["current_version_id"] == second["version_id"]
    assert len(versions) == 2
    assert all(version["published_at"] == version["created_at"] for version in versions)


def test_draft_pointer_cannot_reference_another_dashboard(client, bundle, make_human):
    from sqlalchemy.exc import IntegrityError
    owner = make_human("draft-fk-owner")
    first = save(client, owner).json()["result"]
    second = save(client, owner).json()["result"]
    with pytest.raises(IntegrityError):
        with bundle.database.transaction() as connection:
            connection.execute(models.dashboards.update().where(
                models.dashboards.c.id == first["dashboard_id"]
            ).values(draft_version_id=second["version_id"]))


def test_operation_queries_hide_unpublished_result_and_redact_draft_ids_after_downgrade(
        client, make_human):
    owner, editor = make_human("operation-owner"), make_human("operation-editor")
    did = publish_html(client, owner, auth_mode="human").json()["result"]["dashboard_id"]
    editor_id = client.get("/api/v1/me", headers=human_headers(editor)).json()["principal_id"]
    assert _grant(client, owner, did, "user", editor_id, "editor").status_code == 200
    saved = save(client, editor, dashboard_id=did, revision=2).json()
    published = save(client, editor, b"<html>published by editor</html>", dashboard_id=did,
                     revision=3, disposition="publish").json()
    assert published["result"]["draft_version_id"] == saved["result"]["version_id"]
    assert _grant(client, owner, did, "user", editor_id, "viewer", expected_revision=4).status_code == 200
    for suffix in (f"/{saved['operation_id']}", f"?request_id={saved['request_id']}"):
        response = client.get(f"/api/v1/operations{suffix}", headers=human_headers(editor))
        assert response.status_code == 404, response.text
    for suffix in (f"/{published['operation_id']}", f"?request_id={published['request_id']}"):
        response = client.get(f"/api/v1/operations{suffix}", headers=human_headers(editor))
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["draft_version_id"] is None and result["draft_version_number"] is None
        assert result.get("draft_version_sha256") is None
        assert result.get("draft_version_byte_size") is None
        assert result["has_draft"] is False
        assert result["version_id"] == published["result"]["version_id"]


def test_downgrade_refuses_unpublished_history_before_altering_schema(client, bundle, make_human):
    from pathlib import Path

    from alembic.config import Config as AlembicConfig

    from alembic import command
    owner = make_human("downgrade-private-owner")
    did = publish_html(client, owner, auth_mode="human").json()["result"]["dashboard_id"]
    draft = save(client, owner, dashboard_id=did, revision=1).json()["result"]["version_id"]
    config = AlembicConfig(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    try:
        with pytest.raises(RuntimeError, match="draft"):
            command.downgrade(config, "b41d7c2f9013")
        bundle.database.verify_schema()
        board = detail(client, owner, did)
        assert board["draft_version_id"] == draft and board["status"] == "published"
        with bundle.database.read_only() as connection:
            version = connection.execute(select(models.dashboard_versions).where(
                models.dashboard_versions.c.id == draft)).mappings().one()
        assert version["published_at"] is None
    finally:
        command.upgrade(config, "head")


def test_legacy_omitted_description_replays_without_aliasing_new_clear_requests(
        client, bundle, make_human):
    from dashboard_service.operations import request_hash
    owner = make_human("legacy-omission-owner")
    did = publish_html(client, owner, auth_mode="human").json()["result"]["dashboard_id"]
    html = b"<html>update</html>"
    metadata = {"content_sha256": hashlib.sha256(html).hexdigest(), "byte_size": len(html),
                "expected_revision": 1}
    path = f"/api/v1/dashboards/{did}/versions"
    headers = write_headers(owner)
    first = client.post(path, headers=headers, **multipart(metadata, html))
    assert first.status_code == 201
    # Model a completed old-server record, whose omitted description was
    # normalized to ''. Its immutable result must remain replayable.
    with bundle.database.transaction() as connection:
        connection.execute(models.operations.update().where(
            models.operations.c.id == first.json()["operation_id"]).values(
            action="publish", request_hash=request_hash("POST", path,
                {"title": None, "description": "", **metadata})))
    replay = client.post(path, headers=headers, **multipart(metadata, html))
    assert replay.status_code == 201 and replay.json() == first.json(), replay.text
    metadata["expected_revision"] = 2
    headers = write_headers(owner)
    second = client.post(path, headers=headers, **multipart(metadata, html))
    assert second.status_code == 201
    cleared = client.post(path, headers=headers, **multipart({**metadata, "description": ""}, html))
    assert cleared.status_code == 409 and cleared.json()["code"] == "idempotency_conflict"


@pytest.mark.parametrize("state", ["accepted", "failed"])
def test_legacy_pending_or_failed_omitted_description_upload_remains_recoverable(
        client, bundle, make_human, state):
    owner = make_human(f"legacy-{state}-owner")
    did = publish_html(client, owner, auth_mode="human").json()["result"]["dashboard_id"]
    actor = bundle.service.authenticator.authenticate(
        {"authorization": f"Bearer {owner}", "x-dashboard-auth-mode": "human"})
    html, key = b"<html>legacy pending</html>", str(uuid.uuid4())
    metadata = {"content_sha256": hashlib.sha256(html).hexdigest(), "byte_size": len(html),
                "expected_revision": 1}
    path = f"/api/v1/dashboards/{did}/versions"
    with bundle.database.transaction() as connection:
        with bundle.database.guard(connection, exclusive=False):
            staged = bundle.service.operations.begin_staged(
                connection, actor, key, action="publish", method="POST", path=path,
                target_id=did, payload={"title": None, "description": "", **metadata},
                new_dashboard=False, byte_size=len(html), quota_owner_id=actor.principal_id)
            if state == "failed":
                connection.execute(models.operations.update().where(
                    models.operations.c.id == staged["operation_id"]).values(
                    state="failed", error={"retryable": True, "code": "interrupted"}))
    response = client.post(path, headers=write_headers(owner, key=key), **multipart(metadata, html))
    assert response.status_code == (202 if state == "accepted" else 201), response.text
    assert response.json()["operation_id"] == staged["operation_id"]
    if state == "failed":
        assert detail(client, owner, did)["description"] == "desc"

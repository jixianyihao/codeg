"""T3: per-dashboard ACL — roles, time windows, groups, public access,
archive/restore, atomic access-changes."""
import uuid
from datetime import timedelta

import pytest

from .conftest import human_headers, publish_html, requires_mysql, service_headers

pytestmark = requires_mysql


@pytest.fixture()
def owner_setup(client, make_human):
    """Human owner with one published dashboard."""
    token = make_human("owner-a", "Owner A")
    created = publish_html(client, token, auth_mode="human", title="Shared Board").json()["result"]
    return token, created


def _grant(client, token, dashboard_id, subject_type, subject_id, role="viewer",
           expected_revision=1, **times):
    payload = {"subject_type": subject_type, "subject_id": subject_id, "role": role,
               "expected_revision": expected_revision}
    payload.update(times)
    return client.post(f"/api/v1/dashboards/{dashboard_id}/grants",
                       headers={**human_headers(token),
                                "Idempotency-Key": str(uuid.uuid4())}, json=payload)


def test_role_matrix_per_action(client, owner_setup, make_human, make_service_account):
    owner_token, created = owner_setup
    dashboard_id = created["dashboard_id"]
    viewer_token = make_human("viewer-b")
    editor_token = make_human("editor-c")

    viewer_id = client.get("/api/v1/me", headers=human_headers(viewer_token)).json()["principal_id"]
    editor_id = client.get("/api/v1/me", headers=human_headers(editor_token)).json()["principal_id"]
    assert _grant(client, owner_token, dashboard_id, "user", viewer_id, "viewer").status_code == 200
    assert _grant(client, owner_token, dashboard_id, "user", editor_id, "editor",
                  expected_revision=2).status_code == 200

    # viewer: read yes, write no, manage no
    assert client.get(f"/api/v1/dashboards/{dashboard_id}",
                      headers=human_headers(viewer_token)).status_code == 200
    denied = publish_html(client, viewer_token, b"<html>v</html>",
                          dashboard_id=dashboard_id, expected_revision=2, auth_mode="human")
    assert denied.status_code == 403
    assert denied.json()["code"] == "action_forbidden"
    grants_view = client.get(f"/api/v1/dashboards/{dashboard_id}/grants",
                             headers=human_headers(viewer_token))
    assert grants_view.status_code == 403

    # editor: write yes, manage no
    update = publish_html(client, editor_token, b"<html>editor</html>",
                          dashboard_id=dashboard_id, expected_revision=3, auth_mode="human")
    assert update.status_code == 201, update.text
    share_attempt = _grant(client, editor_token, dashboard_id, "user", viewer_id, "viewer",
                           expected_revision=4)
    assert share_attempt.status_code == 403


def test_two_dashboards_do_not_leak(client, owner_setup, make_human):
    owner_token, created = owner_setup
    other = publish_html(client, owner_token, b"<html>other</html>",
                         auth_mode="human", title="Other").json()["result"]
    reader = make_human("reader-d")
    reader_id = client.get("/api/v1/me", headers=human_headers(reader)).json()["principal_id"]
    _grant(client, owner_token, created["dashboard_id"], "user", reader_id, "viewer")
    assert client.get(f"/api/v1/dashboards/{created['dashboard_id']}",
                      headers=human_headers(reader)).status_code == 200
    # Grant on dashboard A grants nothing on dashboard B — the DETAIL is
    # where the boundary bites (the listing itself is public metadata).
    assert client.get(f"/api/v1/dashboards/{other['dashboard_id']}",
                      headers=human_headers(reader)).status_code == 404
    listing = client.get("/api/v1/dashboards?scope=all",
                         headers=human_headers(reader)).json()
    listed = {item["id"]: item for item in listing["items"]}
    assert listed[created["dashboard_id"]]["role"] == "viewer"
    assert listed[other["dashboard_id"]]["role"] is None  # visible, not accessible


def test_grant_expiry_boundary(client, owner_setup, make_human):
    from dashboard_service.errors import now as svc_now
    owner_token, created = owner_setup
    dashboard_id = created["dashboard_id"]
    temp = make_human("temp-e")
    temp_id = client.get("/api/v1/me", headers=human_headers(temp)).json()["principal_id"]
    expiry = (svc_now() + timedelta(seconds=2)).isoformat().replace("+00:00", "Z")
    assert _grant(client, owner_token, dashboard_id, "user", temp_id, "viewer",
                  expires_at=expiry).status_code == 200
    assert client.get(f"/api/v1/dashboards/{dashboard_id}",
                      headers=human_headers(temp)).status_code == 200
    import time
    time.sleep(2.5)
    assert client.get(f"/api/v1/dashboards/{dashboard_id}",
                      headers=human_headers(temp)).status_code == 404


def test_expiry_reported_and_multi_source_downgrade(client, owner_setup, make_human):
    from dashboard_service.errors import now as svc_now
    owner_token, created = owner_setup
    dashboard_id = created["dashboard_id"]
    user = make_human("multi-f")
    user_id = client.get("/api/v1/me", headers=human_headers(user)).json()["principal_id"]
    group_creation = client.post("/api/v1/groups",
                                 headers={**human_headers(owner_token),
                                          "Idempotency-Key": str(uuid.uuid4())},
                                 json={"display_name": "Downgrade"}).json()["result"]
    group_id = group_creation["group_id"]
    client.put(f"/api/v1/groups/{group_id}/members",
               headers={**human_headers(owner_token), "Idempotency-Key": str(uuid.uuid4())},
               json={"members": [user_id], "expected_revision": 1})
    soon = (svc_now() + timedelta(hours=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
    later = (svc_now() + timedelta(days=7)).isoformat(timespec="seconds").replace("+00:00", "Z")
    # Direct user grant: editor, lapses in an hour.
    assert _grant(client, owner_token, dashboard_id, "user", user_id, "editor",
                  expires_at=soon).status_code == 200
    # Group grant: viewer, lasts a week.
    assert _grant(client, owner_token, dashboard_id, "group", group_id, "viewer",
                  expected_revision=2, expires_at=later).status_code == 200
    detail = client.get(f"/api/v1/dashboards/{dashboard_id}",
                        headers=human_headers(user)).json()
    assert detail["role"] == "editor"
    # editor lapses in an hour even though the group viewer grant lasts a week
    assert detail["expires_at"] == soon


def test_revoking_group_keeps_public_access(client, owner_setup, make_human):
    owner_token, created = owner_setup
    dashboard_id = created["dashboard_id"]
    group_creation = client.post("/api/v1/groups",
                                 headers={**human_headers(owner_token),
                                          "Idempotency-Key": str(uuid.uuid4())},
                                 json={"display_name": "Team"}).json()["result"]
    group_id = group_creation["group_id"]
    member = make_human("member-g")
    member_id = client.get("/api/v1/me", headers=human_headers(member)).json()["principal_id"]
    client.put(f"/api/v1/groups/{group_id}/members",
               headers={**human_headers(owner_token), "Idempotency-Key": str(uuid.uuid4())},
               json={"members": [member_id], "expected_revision": 1})
    _grant(client, owner_token, dashboard_id, "group", group_id, "viewer")
    enabled = client.put(
        f"/api/v1/dashboards/{dashboard_id}/public-access",
        headers={**human_headers(owner_token), "Idempotency-Key": str(uuid.uuid4())},
        json={"enabled": True, "expected_revision": 2})
    assert enabled.status_code == 200

    # Delete the group grant; public (all_authenticated viewer) still applies.
    delete = client.delete(
        f"/api/v1/dashboards/{dashboard_id}/grants/group/{group_id}?expected_revision=3",
        headers={**human_headers(owner_token), "Idempotency-Key": str(uuid.uuid4())})
    assert delete.status_code == 200
    assert client.get(f"/api/v1/dashboards/{dashboard_id}",
                      headers=human_headers(member)).status_code == 200
    access = client.get(f"/api/v1/dashboards/{dashboard_id}/access",
                        headers=human_headers(member)).json()
    assert access["role"] == "viewer"
    assert any(s["subject_type"] == "all_authenticated" for s in access["sources"])

    # Machines never match public rules.
    machine_headers = {"Authorization": "Bearer anything", "X-Dashboard-Auth-Mode": "service"}
    assert client.get(f"/api/v1/dashboards/{dashboard_id}",
                      headers=machine_headers).status_code == 401


def test_service_grant_only_matches_service_principal(client, owner_setup, make_human,
                                                      make_service_account):
    owner_token, created = owner_setup
    dashboard_id = created["dashboard_id"]
    _, machine_token = make_service_account("granted-machine", ("read",))
    machine_id = client.get("/api/v1/me", headers=service_headers(machine_token)).json()["principal_id"]
    _grant(client, owner_token, dashboard_id, "service", machine_id, "viewer")
    # The machine can read metadata and source.
    assert client.get(f"/api/v1/dashboards/{dashboard_id}",
                      headers=service_headers(machine_token)).status_code == 200
    source = client.get(
        f"/api/v1/dashboards/{dashboard_id}/versions/{created['version_id']}/source",
        headers=service_headers(machine_token))
    assert source.status_code == 200
    # A human never matches a service-subject rule.
    human = make_human("human-h")
    assert client.get(f"/api/v1/dashboards/{dashboard_id}",
                      headers=human_headers(human)).status_code == 404


def test_history_shares_current_acl(client, owner_setup, make_human):
    owner_token, created = owner_setup
    dashboard_id = created["dashboard_id"]
    v2 = publish_html(client, owner_token, b"<html>v2</html>", dashboard_id=dashboard_id,
                      expected_revision=1, auth_mode="human").json()["result"]
    reader = make_human("reader-i")
    reader_id = client.get("/api/v1/me", headers=human_headers(reader)).json()["principal_id"]
    # Before any grant: both versions invisible.
    assert client.get(
        f"/api/v1/dashboards/{dashboard_id}/versions/{created['version_id']}/source",
        headers=human_headers(reader)).status_code == 404
    granted = _grant(client, owner_token, dashboard_id, "user", reader_id, "viewer",
                     expected_revision=2)
    assert granted.status_code == 200
    # After the grant: the older version's source becomes readable too.
    assert client.get(
        f"/api/v1/dashboards/{dashboard_id}/versions/{created['version_id']}/source",
        headers=human_headers(reader)).status_code == 200


def test_archive_blocks_reads_owner_can_restore(client, owner_setup, make_human):
    owner_token, created = owner_setup
    dashboard_id = created["dashboard_id"]
    reader = make_human("reader-j")
    reader_id = client.get("/api/v1/me", headers=human_headers(reader)).json()["principal_id"]
    _grant(client, owner_token, dashboard_id, "user", reader_id, "viewer")
    archived = client.post(
        f"/api/v1/dashboards/{dashboard_id}/archive",
        headers={**human_headers(owner_token), "Idempotency-Key": str(uuid.uuid4())},
        json={"expected_revision": 2})
    assert archived.status_code == 200
    assert archived.json()["result"]["status"] == "archived"
    # Viewer loses everything, including source and capabilities.
    assert client.get(f"/api/v1/dashboards/{dashboard_id}",
                      headers=human_headers(reader)).status_code == 404
    assert client.post(
        f"/api/v1/dashboards/{dashboard_id}/view-capabilities",
        headers={**human_headers(reader), "Idempotency-Key": str(uuid.uuid4())},
        json={}).status_code == 404
    # Owner still sees management info and archived history.
    assert client.get(f"/api/v1/dashboards/{dashboard_id}",
                      headers=human_headers(owner_token)).json()["status"] == "archived"
    assert client.get(f"/api/v1/dashboards/{dashboard_id}/versions",
                      headers=human_headers(owner_token)).status_code == 200
    restored = client.post(
        f"/api/v1/dashboards/{dashboard_id}/restore",
        headers={**human_headers(owner_token), "Idempotency-Key": str(uuid.uuid4())},
        json={"expected_revision": 3})
    assert restored.status_code == 200
    assert client.get(f"/api/v1/dashboards/{dashboard_id}",
                      headers=human_headers(reader)).status_code == 200


def test_access_changes_atomic_all_or_nothing(client, owner_setup, make_human):
    owner_token, created = owner_setup
    dashboard_id = created["dashboard_id"]
    reader = make_human("reader-k")
    reader_id = client.get("/api/v1/me", headers=human_headers(reader)).json()["principal_id"]
    group = client.post("/api/v1/groups",
                        headers={**human_headers(owner_token),
                                 "Idempotency-Key": str(uuid.uuid4())},
                        json={"display_name": "G"}).json()["result"]
    # Batch: disable public + grant group viewer + grant user editor.
    response = client.post(
        f"/api/v1/dashboards/{dashboard_id}/access-changes",
        headers={**human_headers(owner_token), "Idempotency-Key": str(uuid.uuid4())},
        json={"expected_revision": 1, "changes": [
            {"action": "set_public", "enabled": True},
            {"action": "grant", "subject_type": "group", "subject_id": group["group_id"],
             "role": "viewer"},
            {"action": "grant", "subject_type": "user", "subject_id": reader_id,
             "role": "editor"},
        ]})
    assert response.status_code == 200
    revision = response.json()["result"]["revision"]
    assert revision == 2  # the whole batch bumps revision exactly once

    # A batch with one invalid item changes nothing at all.
    broken = client.post(
        f"/api/v1/dashboards/{dashboard_id}/access-changes",
        headers={**human_headers(owner_token), "Idempotency-Key": str(uuid.uuid4())},
        json={"expected_revision": revision, "changes": [
            {"action": "set_public", "enabled": False},
            {"action": "grant", "subject_type": "nonsense", "subject_id": reader_id},
        ]})
    assert broken.status_code == 422
    grants = client.get(f"/api/v1/dashboards/{dashboard_id}/grants",
                        headers=human_headers(owner_token)).json()
    assert grants["revision"] == revision  # unchanged
    assert any(g["subject_type"] == "all_authenticated" for g in grants["items"])


def test_group_member_cas_conflict(client, owner_setup, make_human):
    owner_token, _ = owner_setup
    creation = client.post("/api/v1/groups",
                           headers={**human_headers(owner_token),
                                    "Idempotency-Key": str(uuid.uuid4())},
                           json={"display_name": "CAS"}).json()["result"]
    group_id = creation["group_id"]
    member = make_human("member-l")
    member_id = client.get("/api/v1/me", headers=human_headers(member)).json()["principal_id"]
    first = client.put(
        f"/api/v1/groups/{group_id}/members",
        headers={**human_headers(owner_token), "Idempotency-Key": str(uuid.uuid4())},
        json={"members": [member_id], "expected_revision": 1})
    assert first.status_code == 200
    # Second write based on stale revision must not overwrite.
    stale = client.put(
        f"/api/v1/groups/{group_id}/members",
        headers={**human_headers(owner_token), "Idempotency-Key": str(uuid.uuid4())},
        json={"members": [], "expected_revision": 1})
    assert stale.status_code == 409
    detail = client.get(f"/api/v1/groups/{group_id}",
                        headers=human_headers(owner_token)).json()
    assert detail["members"] == [member_id]


def test_service_cannot_manage_groups(client, make_service_account):
    _, token = make_service_account("ci-groups")
    response = client.post("/api/v1/groups",
                           headers={**service_headers(token),
                                    "Idempotency-Key": str(uuid.uuid4())},
                           json={"display_name": "Nope"})
    assert response.status_code == 403


def test_grant_time_window_validation(client, owner_setup, make_human):
    owner_token, created = owner_setup
    reader = make_human("reader-m")
    reader_id = client.get("/api/v1/me", headers=human_headers(reader)).json()["principal_id"]
    # Naive timestamp without timezone is rejected.
    response = _grant(client, owner_token, created["dashboard_id"], "user", reader_id,
                      "viewer", expires_at="2026-09-16T10:00:00")
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_time"
    # starts_at must be before expires_at.
    from dashboard_service.errors import now as svc_now
    when = (svc_now() + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    response = _grant(client, owner_token, created["dashboard_id"], "user", reader_id,
                      "viewer", starts_at=when, expires_at=when)
    assert response.status_code == 422


def test_owner_role_not_grantable(client, owner_setup, make_human):
    owner_token, created = owner_setup
    reader = make_human("reader-n")
    reader_id = client.get("/api/v1/me", headers=human_headers(reader)).json()["principal_id"]
    response = _grant(client, owner_token, created["dashboard_id"], "user", reader_id, "owner")
    assert response.status_code == 422


def test_public_grant_role_forced_viewer(client, owner_setup):
    owner_token, created = owner_setup
    response = _grant(client, owner_token, created["dashboard_id"], "all_authenticated",
                      "*", "editor")
    assert response.status_code == 422


# ------------------------------------------------------- R7/R8/R9 fixes

def test_timed_grant_role_change_keeps_window(client, owner_setup, make_human):
    """R7: changing only the role of an already-timed grant preserves the
    stored starts_at/expires_at instead of choking on the stored datetime."""
    owner_token, created = owner_setup
    dashboard_id = created["dashboard_id"]
    editor_token = make_human("editor-window")
    editor_id = client.get("/api/v1/me", headers=human_headers(editor_token)).json()["principal_id"]
    expires = "2027-01-01T00:00:00Z"
    granted = _grant(client, owner_token, dashboard_id, "user", editor_id, "viewer",
                     expected_revision=1, expires_at=expires)
    assert granted.status_code == 200, granted.text

    # Role-only change: no time fields in the payload.
    promoted = _grant(client, owner_token, dashboard_id, "user", editor_id, "editor",
                      expected_revision=2)
    assert promoted.status_code == 200, promoted.text
    grants = client.get(f"/api/v1/dashboards/{dashboard_id}/grants",
                        headers=human_headers(owner_token)).json()["items"]
    mine = [g for g in grants if g["subject_id"] == editor_id]
    assert mine and mine[0]["role"] == "editor"
    assert mine[0]["expires_at"] == expires  # window preserved

    # One boundary change only: keep the role, move the expiry.
    later = "2028-06-01T00:00:00Z"
    moved = _grant(client, owner_token, dashboard_id, "user", editor_id, "editor",
                   expected_revision=3, expires_at=later)
    assert moved.status_code == 200, moved.text
    grants = client.get(f"/api/v1/dashboards/{dashboard_id}/grants",
                        headers=human_headers(owner_token)).json()["items"]
    mine = [g for g in grants if g["subject_id"] == editor_id]
    assert mine[0]["expires_at"] == later

    # Explicit null clears the boundary (distinct from omitting it).
    cleared = _grant(client, owner_token, dashboard_id, "user", editor_id, "editor",
                     expected_revision=4, expires_at=None)
    assert cleared.status_code == 200, cleared.text
    grants = client.get(f"/api/v1/dashboards/{dashboard_id}/grants",
                        headers=human_headers(owner_token)).json()["items"]
    mine = [g for g in grants if g["subject_id"] == editor_id]
    assert mine[0]["expires_at"] is None


def test_revoked_editor_cannot_replay_stored_success(client, owner_setup, make_human):
    """R8: a recorded write result is only replayed after re-checking the
    caller's CURRENT authorization."""
    owner_token, created = owner_setup
    dashboard_id = created["dashboard_id"]
    editor_token = make_human("editor-replay")
    editor_id = client.get("/api/v1/me", headers=human_headers(editor_token)).json()["principal_id"]
    assert _grant(client, owner_token, dashboard_id, "user", editor_id, "editor").status_code == 200

    key = str(uuid.uuid4())
    first = client.patch(
        f"/api/v1/dashboards/{dashboard_id}",
        headers={**human_headers(editor_token), "Idempotency-Key": key},
        json={"title": "by editor", "expected_revision": 2})
    assert first.status_code == 200
    assert first.json()["state"] == "succeeded"

    # Same key + same request replays while access is intact.
    replay_ok = client.patch(
        f"/api/v1/dashboards/{dashboard_id}",
        headers={**human_headers(editor_token), "Idempotency-Key": key},
        json={"title": "by editor", "expected_revision": 2})
    assert replay_ok.status_code == 200
    assert replay_ok.json()["state"] == "succeeded"

    # Owner revokes the editor; the identical replay must now be denied —
    # the stored success is NOT handed back.
    assert client.delete(
        f"/api/v1/dashboards/{dashboard_id}/grants/user/{editor_id}?expected_revision=3",
        headers={**human_headers(owner_token), "Idempotency-Key": str(uuid.uuid4())}
    ).status_code == 200
    denied = client.patch(
        f"/api/v1/dashboards/{dashboard_id}",
        headers={**human_headers(editor_token), "Idempotency-Key": key},
        json={"title": "by editor", "expected_revision": 2})
    assert denied.status_code == 404
    assert denied.json().get("state") != "succeeded"


def test_write_only_service_scope_cannot_list_dashboards(client, operator,
                                                         make_service_account):
    """R9: the list endpoint applies the same read-scope gate as details."""
    principal_id, token = make_service_account("scope-list",
                                               scopes=("write", "manage"))
    listed = client.get("/api/v1/dashboards", headers=service_headers(token))
    assert listed.status_code == 403
    assert listed.json()["code"] == "action_forbidden"

    # A read-capable account lists fine (empty is correct: private boards).
    _, reader = make_service_account("scope-list-reader", scopes=("read",))
    ok = client.get("/api/v1/dashboards", headers=service_headers(reader))
    assert ok.status_code == 200
    assert ok.json()["items"] == []


# ------------------------------------------------------- public listing

def test_published_listing_is_public_to_authenticated_humans(
        client, make_human, make_service_account):
    """Product decision 2026-09-09: the LIST shows every published board's
    metadata to any authenticated human; per-board authorization applies at
    detail/manage/capability time. Service accounts still see only granted
    boards."""
    owner_token = make_human("pub-owner", "List Owner")
    created = publish_html(client, owner_token, b"<html>one</html>",
                           auth_mode="human", title="公开列表甲").json()["result"]
    second = publish_html(client, owner_token, b"<html>two</html>",
                          auth_mode="human", title="公开列表乙").json()["result"]

    stranger_token = make_human("pub-stranger", "Stranger")
    listed = client.get("/api/v1/dashboards?scope=all",
                        headers=human_headers(stranger_token))
    assert listed.status_code == 200
    items = {item["id"]: item for item in listed.json()["items"]}
    assert created["dashboard_id"] in items and second["dashboard_id"] in items
    assert items[created["dashboard_id"]]["role"] is None  # visible, no access

    # Clicking in is where authorization happens.
    assert client.get(f"/api/v1/dashboards/{created['dashboard_id']}",
                      headers=human_headers(stranger_token)).status_code == 404
    assert client.post(
        f"/api/v1/dashboards/{created['dashboard_id']}/view-capabilities",
        headers={**human_headers(stranger_token), "Idempotency-Key": str(uuid.uuid4())},
        json={}).status_code == 404

    # Service accounts keep the grant-scoped listing (and the read-scope gate).
    principal_id, reader = make_service_account("pub-reader", scopes=("read",))
    machine_items = client.get("/api/v1/dashboards?scope=all",
                               headers=service_headers(reader)).json()["items"]
    assert machine_items == []
    granted = client.post(
        f"/api/v1/dashboards/{created['dashboard_id']}/grants",
        headers={**human_headers(owner_token), "Idempotency-Key": str(uuid.uuid4())},
        json={"subject_type": "service", "subject_id": principal_id,
              "role": "viewer", "expected_revision": 1})
    assert granted.status_code == 200
    machine_items = client.get("/api/v1/dashboards?scope=all",
                               headers=service_headers(reader)).json()["items"]
    assert [item["id"] for item in machine_items] == [created["dashboard_id"]]


def test_archived_boards_stay_out_of_public_listing(client, make_human):
    owner_token = make_human("arch-owner", "Arch Owner")
    created = publish_html(client, owner_token, b"<html>x</html>",
                           auth_mode="human", title="下架不外列").json()["result"]
    dashboard_id = created["dashboard_id"]
    assert client.post(
        f"/api/v1/dashboards/{dashboard_id}/archive",
        headers={**human_headers(owner_token), "Idempotency-Key": str(uuid.uuid4())},
        json={"expected_revision": 1}).status_code == 200

    stranger_token = make_human("arch-stranger", "Arch Stranger")
    listed = client.get("/api/v1/dashboards?scope=all",
                        headers=human_headers(stranger_token)).json()["items"]
    assert all(item["id"] != dashboard_id for item in listed)
    # Archived listing stays the owner's management view.
    assert client.get("/api/v1/dashboards?status=archived",
                      headers=human_headers(stranger_token)).json()["items"] == []
    own = client.get("/api/v1/dashboards?status=archived",
                     headers=human_headers(owner_token)).json()["items"]
    assert [item["id"] for item in own] == [dashboard_id]

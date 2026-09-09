"""Dashboard resources (contracts.md §3)."""
import json

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from .. import models, quotas
from ..authn import AuthContext
from ..database import record_audit, to_db
from ..errors import ApiError, new_id, now, require, require_uuid
from ..publishing import version_state
from . import Service, dashboard_view, get_actor, get_service, reverify

router = APIRouter(prefix="/api/v1", tags=["dashboards"])

MAX_FORM_FIELDS = 8
ALLOWED_METADATA_FIELDS = {"title", "description", "content_sha256", "byte_size",
                           "expected_revision", "disposition"}


def parse_pagination(limit: int | None, config) -> int:
    if limit is None:
        return config.page_size_default
    require(1 <= limit <= config.page_size_max, 422, "invalid_input",
            f"limit must be 1..{config.page_size_max}")
    return limit


@router.get("/dashboards")
async def list_dashboards(request: Request, scope: str = "all", q: str = "",
                    status: str = "published", cursor: str | None = None,
                    limit: int | None = None,
                    actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)
    require(scope in ("mine", "shared", "all"), 422, "invalid_input", "scope must be mine/shared/all")
    require(status in ("draft", "published", "archived"), 422, "invalid_input",
            "status must be draft/published/archived")
    page_limit = parse_pagination(limit, service.config)

    def run():
        with service.database.read_only() as connection:
            service.authorizer.check_context_validity(connection, actor)
            if actor.principal_type == "service":
                # Same read-scope gate as the detail endpoint: write/manage-
                # only integration accounts must not list metadata (R9).
                actor.requires_scope("read")
            rows, next_cursor = service.authorizer.visible_ids(
                connection, actor, scope=scope, status=status, search=q,
                cursor=cursor, limit=page_limit)
            items = []
            for row in rows:
                access = service.authorizer.effective_access(connection, row, actor)
                items.append(dashboard_view(connection, service, row, access))
            return {"items": items, "next_cursor": next_cursor}

    return await run_in_threadpool(run)


@router.post("/dashboards/drafts", status_code=201)
def create_blank_draft(request: Request, payload: dict,
                       actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)
    from ..operations import validate_idempotency_key
    key = validate_idempotency_key(request.headers.get("idempotency-key"))
    require(set(payload) <= {"title", "description"}, 422, "invalid_input", "Unknown fields")
    title, description = payload.get("title"), payload.get("description", "")
    require(isinstance(title, str) and 1 <= len(title.strip()) <= 200,
            422, "invalid_input", "title is required (1-200 characters)")
    require(isinstance(description, str) and len(description) <= 2000,
            422, "invalid_input", "description must be at most 2000 characters")

    def replay_check(connection):
        service.authorizer.check_context_validity(connection, actor)
        actor.requires_scope("write")

    def unit(connection, operation_id):
        replay_check(connection)
        quotas.ensure_rows(connection, actor.principal_id)
        quotas.check_headroom(connection, service.config, actor.principal_id,
                              extra_bytes=0, new_dashboard=True)
        dashboard_id, moment = new_id(), to_db(now())
        connection.execute(models.dashboards.insert().values(
            id=dashboard_id, owner_principal_id=actor.principal_id,
            title=title, description=description, status="draft", revision=1,
            current_version_id=None, draft_version_id=None, published_at=None,
            created_at=moment, updated_at=moment))
        for scope, owner_id in (("owner", actor.principal_id), ("global", "*")):
            connection.execute(models.quota_usage.update().where(
                models.quota_usage.c.scope == scope, models.quota_usage.c.owner_id == owner_id
            ).values(dashboard_count=models.quota_usage.c.dashboard_count + 1))
        record_audit(connection, actor=actor, action="dashboard.create_draft",
                     target_type="dashboard", target_id=dashboard_id,
                     after={"title": title, "status": "draft"},
                     trace_id=getattr(request.state, "trace_id", "local"), operation_id=operation_id)
        return {"dashboard_id": dashboard_id, "revision": 1, "status": "draft",
                "disposition": "save_draft", "version_id": None,
                "current_version_id": None, "current_version_number": None,
                "draft_version_id": None, "draft_version_number": None,
                "has_draft": False, "published_at": None,
                "view_url": f"{service.config.control_origin}/dashboards/{dashboard_id}"}

    return service.operations.run_sync(
        actor, key, action="create_draft", method="POST", path="/api/v1/dashboards/drafts",
        target_id=None, payload=payload, exclusive_guard=False, unit=unit,
        replay_check=replay_check, trace_id=getattr(request.state, "trace_id", "local"))


@router.get("/dashboards/{dashboard_id}")
async def get_dashboard(dashboard_id: str, request: Request,
                  actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)

    def run():
        with service.database.read_only() as connection:
            row = service.authorizer.dashboard(connection, require_uuid(dashboard_id))
            access = service.authorizer.authorize(connection, row, actor, "read")
            return dashboard_view(connection, service, row, access)

    return await run_in_threadpool(run)


async def _parse_publish_form(request: Request, *, creating: bool):
    form = await request.form(max_files=1, max_fields=MAX_FORM_FIELDS)
    parts = list(form.multi_items())
    require(len(parts) == 2 and parts[0][0] == "metadata" and parts[1][0] == "html"
            and isinstance(parts[0][1], str)
            and hasattr(parts[1][1], "read"), 422, "invalid_input",
            "Upload requires a metadata JSON part followed by exactly one html file part")
    try:
        metadata = json.loads(parts[0][1])
    except ValueError:
        raise ApiError(422, "invalid_input", "metadata part must be valid JSON") from None
    require(isinstance(metadata, dict), 422, "invalid_input", "metadata part must be a JSON object")
    unknown = set(metadata) - ALLOWED_METADATA_FIELDS
    require(not unknown, 422, "invalid_input",
            f"Unknown metadata fields: {sorted(unknown)}")
    if not creating and "title" not in metadata and "description" not in metadata:
        pass  # both optional on update
    return metadata, parts[1][1]


@router.post("/dashboards", status_code=201)
async def create_dashboard(request: Request,
                           actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)
    from ..operations import validate_idempotency_key
    key = validate_idempotency_key(request.headers.get("idempotency-key"))
    metadata, upload = await _parse_publish_form(request, creating=True)

    def run():
        outcome = service.publisher.publish(
            actor, reverify(request), key, dashboard_id=None, metadata=metadata,
            html_stream=upload.file, trace_id=getattr(request.state, "trace_id", "local"))
        return outcome

    outcome = await run_in_threadpool(run)
    return _operation_response(outcome)


@router.post("/dashboards/{dashboard_id}/versions", status_code=201)
async def create_version(dashboard_id: str, request: Request,
                         actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)
    from ..operations import validate_idempotency_key
    key = validate_idempotency_key(request.headers.get("idempotency-key"))
    metadata, upload = await _parse_publish_form(request, creating=False)

    def run():
        return service.publisher.publish(
            actor, reverify(request), key, dashboard_id=require_uuid(dashboard_id),
            metadata=metadata, html_stream=upload.file,
            trace_id=getattr(request.state, "trace_id", "local"))

    outcome = await run_in_threadpool(run)
    return _operation_response(outcome)


def _operation_response(outcome):
    if outcome.status == 202:
        raise PendingOperation(outcome)
    return outcome.wrapper


class PendingOperation(Exception):
    def __init__(self, outcome):
        self.outcome = outcome
        super().__init__("pending")


@router.patch("/dashboards/{dashboard_id}")
def patch_dashboard(dashboard_id: str, request: Request, payload: dict,
                    actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)
    from ..operations import validate_idempotency_key
    key = validate_idempotency_key(request.headers.get("idempotency-key"))
    require(isinstance(payload, dict), 422, "invalid_input", "Body must be an object")
    require(set(payload) <= {"title", "description", "expected_revision"}, 422,
            "invalid_input", "Unknown fields")
    expected_revision = payload.get("expected_revision")
    require(type(expected_revision) is int, 422, "invalid_input",
            "expected_revision is required")
    if "title" in payload:
        require(isinstance(payload["title"], str) and 1 <= len(payload["title"].strip()) <= 200,
                422, "invalid_input", "title must be 1-200 characters")
    if "description" in payload:
        require(isinstance(payload["description"], str) and len(payload["description"]) <= 2000,
                422, "invalid_input", "description must be at most 2000 characters")

    def unit(connection, operation_id):
        row = service.authorizer.dashboard_for_update(connection, dashboard_id)
        require(row is not None, 404, "not_found", "Dashboard is not visible")
        service.authorizer.authorize(connection, row, actor, "write")
        require(row["revision"] == expected_revision, 409, "revision_conflict",
                "Dashboard changed; read it again")
        moment = to_db(now())
        values = {"revision": row["revision"] + 1, "updated_at": moment}
        if "title" in payload:
            values["title"] = payload["title"]
        if "description" in payload:
            values["description"] = payload["description"]
        connection.execute(models.dashboards.update().where(
            models.dashboards.c.id == dashboard_id).values(**values))
        record_audit(connection, actor=actor, action="dashboard.patch",
                     target_type="dashboard", target_id=dashboard_id,
                     before={"title": row["title"], "description": row["description"]},
                     after={"title": values.get("title", row["title"]),
                            "description": values.get("description", row["description"])},
                     trace_id=getattr(request.state, "trace_id", "local"),
                     operation_id=operation_id)
        return {"dashboard_id": dashboard_id, "revision": row["revision"] + 1,
                "status": row["status"], "disposition": "update_metadata",
                **version_state(connection, row)}

    def replay_check(connection):
        current = service.authorizer.dashboard(connection, dashboard_id)
        service.authorizer.authorize(connection, current, actor, "write")

    return service.operations.run_sync(
        actor, key, action="patch", method="PATCH",
        path=f"/api/v1/dashboards/{dashboard_id}", target_id=dashboard_id,
        payload=payload, exclusive_guard=False, unit=unit, replay_check=replay_check,
        trace_id=getattr(request.state, "trace_id", "local"))


@router.get("/dashboards/{dashboard_id}/versions")
async def list_versions(dashboard_id: str, request: Request, cursor: str | None = None,
                  limit: int | None = None, actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)
    page_limit = parse_pagination(limit, service.config)

    def run():
        from ..database import from_db
        from ..errors import to_rfc3339
        with service.database.read_only() as connection:
            row = service.authorizer.dashboard(connection, require_uuid(dashboard_id))
            access = service.authorizer.authorize(connection, row, actor, "read")
            statement = select(models.dashboard_versions.c.id,
                               models.dashboard_versions.c.number,
                               models.dashboard_versions.c.sha256,
                               models.dashboard_versions.c.byte_size,
                               models.dashboard_versions.c.created_at,
                               models.dashboard_versions.c.created_by,
                               models.dashboard_versions.c.published_at)
            statement = (statement.where(models.dashboard_versions.c.dashboard_id == dashboard_id)
                         .order_by(models.dashboard_versions.c.number.desc()))
            if access.rank < 2:
                statement = statement.where(models.dashboard_versions.c.published_at.is_not(None))
            if cursor is not None:
                try:
                    anchor_number = int(cursor)
                except ValueError:
                    raise ApiError(422, "invalid_input", "Malformed pagination cursor") from None
                statement = statement.where(models.dashboard_versions.c.number < anchor_number)
            rows = connection.execute(statement.limit(page_limit + 1)).mappings().all()
            has_more = len(rows) > page_limit
            rows = rows[:page_limit]
            items = [{
                "id": r["id"], "number": r["number"], "sha256": r["sha256"],
                "byte_size": r["byte_size"],
                "created_at": to_rfc3339(from_db(r["created_at"])),
                "created_by": r["created_by"],
                "published_at": to_rfc3339(from_db(r["published_at"])),
                "is_current": r["id"] == row["current_version_id"],
                "is_draft": access.rank >= 2 and r["id"] == row["draft_version_id"],
            } for r in rows]
            next_cursor = str(rows[-1]["number"]) if has_more and rows else None
            return {"items": items, "next_cursor": next_cursor}

    return await run_in_threadpool(run)


@router.get("/dashboards/{dashboard_id}/versions/{version_id}/source")
async def get_source(dashboard_id: str, version_id: str, request: Request,
               actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)

    def run():
        with service.database.read_only() as connection:
            row = service.authorizer.dashboard(connection, require_uuid(dashboard_id))
            access = service.authorizer.authorize(connection, row, actor, "read")
            version = connection.execute(
                select(models.dashboard_versions.c).where(
                    models.dashboard_versions.c.id == require_uuid(version_id),
                    models.dashboard_versions.c.dashboard_id == dashboard_id)
            ).mappings().one_or_none()
            require(service.authorizer.version_visible(row, version, access),
                    404, "not_found", "Version is not visible")
            content = service.store.read_version(version)
            return Response(
                content, media_type="text/plain; charset=utf-8",
                headers={
                    "Content-Disposition": f'attachment; filename="{version_id}.txt"',
                    "X-Content-Type-Options": "nosniff",
                })

    return await run_in_threadpool(run)


@router.post("/dashboards/{dashboard_id}/publish")
def publish_draft(dashboard_id: str, request: Request, payload: dict,
                  actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)
    from ..operations import validate_idempotency_key
    key = validate_idempotency_key(request.headers.get("idempotency-key"))
    dashboard_id = require_uuid(dashboard_id)
    require(set(payload) <= {"version_id", "expected_revision"}, 422, "invalid_input", "Unknown fields")
    version_id = require_uuid(payload.get("version_id"), "version_id")
    expected_revision = payload.get("expected_revision")
    require(type(expected_revision) is int and expected_revision >= 1, 422,
            "invalid_input", "expected_revision is required")

    def replay_check(connection):
        row = service.authorizer.dashboard(connection, dashboard_id)
        service.authorizer.authorize_publication(connection, row, actor)

    def unit(connection, operation_id):
        row = service.authorizer.dashboard_for_update(connection, dashboard_id)
        require(row is not None, 404, "not_found", "Dashboard is not visible")
        service.authorizer.authorize_publication(connection, row, actor)
        require(row["revision"] == expected_revision, 409, "revision_conflict",
                "Dashboard changed; read it again")
        require(row["draft_version_id"] == version_id, 409, "revision_conflict",
                "The selected version is no longer the current draft")
        version = connection.execute(select(models.dashboard_versions).where(
            models.dashboard_versions.c.dashboard_id == dashboard_id,
            models.dashboard_versions.c.id == version_id)).mappings().one()
        moment, revision = to_db(now()), row["revision"] + 1
        connection.execute(models.dashboard_versions.update().where(
            models.dashboard_versions.c.id == version_id,
            models.dashboard_versions.c.published_at.is_(None)).values(published_at=moment))
        connection.execute(models.dashboards.update().where(models.dashboards.c.id == dashboard_id)
                           .values(current_version_id=version_id, draft_version_id=None,
                                   status="published", revision=revision,
                                   updated_at=moment, published_at=moment))
        record_audit(connection, actor=actor, action="dashboard.publish_draft",
                     target_type="dashboard", target_id=dashboard_id,
                     before={"status": row["status"], "current_version_id": row["current_version_id"]},
                     after={"status": "published", "current_version_id": version_id},
                     trace_id=getattr(request.state, "trace_id", "local"), operation_id=operation_id)
        return {"dashboard_id": dashboard_id, "version_id": version_id,
                "version_number": version["number"], "revision": revision,
                "status": "published", "disposition": "publish",
                "current_version_id": version_id, "current_version_number": version["number"],
                "draft_version_id": None, "draft_version_number": None, "has_draft": False,
                **version_state(connection, service.authorizer.dashboard(connection, dashboard_id)),
                "sha256": version["sha256"],
                "view_url": f"{service.config.control_origin}/dashboards/{dashboard_id}"}

    return service.operations.run_sync(
        actor, key, action="publish_draft", method="POST",
        path=f"/api/v1/dashboards/{dashboard_id}/publish", target_id=dashboard_id,
        payload=payload, exclusive_guard=False, unit=unit, replay_check=replay_check,
        trace_id=getattr(request.state, "trace_id", "local"))


@router.post("/dashboards/{dashboard_id}/rollback")
def rollback(dashboard_id: str, request: Request, payload: dict,
             actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)
    from ..operations import validate_idempotency_key
    key = validate_idempotency_key(request.headers.get("idempotency-key"))
    version_id = require_uuid(payload.get("version_id"), "version_id")
    expected_revision = payload.get("expected_revision")
    require(type(expected_revision) is int, 422, "invalid_input",
            "expected_revision is required")

    def unit(connection, operation_id):
        row = service.authorizer.dashboard_for_update(connection, dashboard_id)
        require(row is not None, 404, "not_found", "Dashboard is not visible")
        service.authorizer.authorize(connection, row, actor, "write")
        require(row["status"] == "published", 409, "invalid_input",
                "Restore the dashboard before rolling back")
        require(row["revision"] == expected_revision, 409, "revision_conflict",
                "Dashboard changed; read it again")
        version = connection.execute(
            select(models.dashboard_versions.c).where(
                models.dashboard_versions.c.id == version_id,
                models.dashboard_versions.c.dashboard_id == dashboard_id)
        ).mappings().one_or_none()
        require(version is not None and version["published_at"] is not None,
                404, "not_found", "Only previously published versions can be rolled back")
        # Rollback points at the original immutable version — no copy, no
        # new version number; published_at follows the target version.
        moment = to_db(now())
        revision = row["revision"] + 1
        connection.execute(models.dashboards.update().where(
            models.dashboards.c.id == dashboard_id).values(
            current_version_id=version_id, revision=revision, updated_at=moment,
            published_at=version["published_at"]))
        record_audit(connection, actor=actor, action="dashboard.rollback",
                     target_type="dashboard", target_id=dashboard_id,
                     before={"current_version_id": row["current_version_id"]},
                     after={"current_version_id": version_id, "number": version["number"]},
                     trace_id=getattr(request.state, "trace_id", "local"),
                     operation_id=operation_id)
        return {"dashboard_id": dashboard_id, "version_id": version_id,
                "version_number": version["number"], "revision": revision,
                "status": "published", "disposition": "rollback",
                **version_state(connection, service.authorizer.dashboard(connection, dashboard_id)),
                "sha256": version["sha256"],
                "view_url": f"{service.config.control_origin}/dashboards/{dashboard_id}"}

    def replay_check(connection):
        current = service.authorizer.dashboard(connection, dashboard_id)
        service.authorizer.authorize(connection, current, actor, "write")

    return service.operations.run_sync(
        actor, key, action="rollback", method="POST",
        path=f"/api/v1/dashboards/{dashboard_id}/rollback", target_id=dashboard_id,
        payload=payload, exclusive_guard=False, unit=unit, replay_check=replay_check,
        trace_id=getattr(request.state, "trace_id", "local"))


@router.post("/dashboards/{dashboard_id}/archive")
def archive_dashboard(dashboard_id: str, request: Request, payload: dict,
                      actor: AuthContext = Depends(get_actor)):
    return _status_change(dashboard_id, request, actor, payload, "archived")


@router.post("/dashboards/{dashboard_id}/restore")
def restore_dashboard(dashboard_id: str, request: Request, payload: dict,
                      actor: AuthContext = Depends(get_actor)):
    return _status_change(dashboard_id, request, actor, payload, "draft")


def _status_change(dashboard_id, request, actor, payload, target_status):
    service: Service = get_service(request)
    from ..operations import validate_idempotency_key
    key = validate_idempotency_key(request.headers.get("idempotency-key"))
    expected_revision = payload.get("expected_revision")
    require(type(expected_revision) is int, 422, "invalid_input",
            "expected_revision is required")
    action = "dashboard.archive" if target_status == "archived" else "dashboard.restore"

    def unit(connection, operation_id):
        row = service.authorizer.dashboard_for_update(connection, dashboard_id)
        require(row is not None, 404, "not_found", "Dashboard is not visible")
        service.authorizer.authorize(connection, row, actor, "manage")
        require(row["revision"] == expected_revision, 409, "revision_conflict",
                "Dashboard changed; read it again")
        require(row["status"] != target_status, 409, "invalid_input",
                "Dashboard is already in the requested state")
        if target_status == "draft":
            require(row["status"] == "archived", 409, "invalid_input",
                    "Only an archived dashboard can be restored")
        revision = row["revision"] + 1
        draft_id = row["draft_version_id"]
        if target_status == "draft" and draft_id is None:
            draft_id = row["current_version_id"]
        connection.execute(models.dashboards.update().where(
            models.dashboards.c.id == dashboard_id).values(
            status=target_status, revision=revision, updated_at=to_db(now()),
            draft_version_id=draft_id))
        record_audit(connection, actor=actor, action=action,
                     target_type="dashboard", target_id=dashboard_id,
                     before={"status": row["status"]}, after={"status": target_status},
                     trace_id=getattr(request.state, "trace_id", "local"),
                     operation_id=operation_id)
        return {"dashboard_id": dashboard_id, "revision": revision, "status": target_status,
                **version_state(connection, service.authorizer.dashboard(connection, dashboard_id)),
                "disposition": "archive" if target_status == "archived" else "restore"}

    def replay_check(connection):
        current = service.authorizer.dashboard(connection, dashboard_id)
        service.authorizer.authorize(connection, current, actor, "manage")

    return service.operations.run_sync(
        actor, key, action=action, method="POST",
        path=f"/api/v1/dashboards/{dashboard_id}/{'archive' if target_status == 'archived' else 'restore'}",
        target_id=dashboard_id, payload=payload, exclusive_guard=False, unit=unit,
        replay_check=replay_check,
        trace_id=getattr(request.state, "trace_id", "local"))

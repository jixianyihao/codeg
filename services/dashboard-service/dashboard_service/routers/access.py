"""Per-dashboard ACL (contracts.md §4). All grant writes take the exclusive
authorization guard so they serialize cleanly against publishes."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy import delete, select

from .. import models
from ..authn import AuthContext
from ..database import record_audit, to_db
from ..errors import now, parse_rfc3339, require, require_uuid
from ..operations import validate_idempotency_key
from . import Service, get_actor, get_service, grant_view

router = APIRouter(prefix="/api/v1", tags=["access"])

SUBJECT_TYPES = ("user", "service", "group", "all_authenticated")


def _load_dashboard(connection, service: Service, dashboard_id: str, actor: AuthContext):
    row = service.authorizer.dashboard(connection, dashboard_id)
    service.authorizer.authorize(connection, row, actor, "manage")
    return row


def _validate_subject(connection, subject_type: str, subject_id) -> str:
    require(subject_type in SUBJECT_TYPES, 422, "invalid_input",
            f"subject_type must be one of {SUBJECT_TYPES}")
    if subject_type == "all_authenticated":
        return "*"
    require_uuid(subject_id, "subject_id")
    if subject_type == "user":
        found = connection.execute(
            select(models.principals.c.id).where(
                models.principals.c.id == subject_id,
                models.principals.c.type == "human")).one_or_none()
    elif subject_type == "service":
        found = connection.execute(
            select(models.service_accounts.c.principal_id).where(
                models.service_accounts.c.principal_id == subject_id)).one_or_none()
    else:
        found = connection.execute(
            select(models.groups.c.id).where(models.groups.c.id == subject_id)).one_or_none()
    require(found is not None, 422, "invalid_input",
            "subject_id must reference an existing registered principal of that type")
    return subject_id


def _time_window(current: tuple, payload: dict):
    """Merge semantics: a key omitted from the payload keeps the stored
    value; an explicit null clears it; both endpoints validated together
    (starts_at < expires_at)."""
    stored_start, stored_end = current
    starts = payload.get("starts_at", stored_start)
    expires = payload.get("expires_at", stored_end)
    starts_at = parse_rfc3339(starts, "starts_at") if starts is not None else None
    expires_at = parse_rfc3339(expires, "expires_at") if expires is not None else None
    require(starts_at is None or expires_at is None or starts_at < expires_at, 422,
            "invalid_time", "starts_at must be before expires_at")
    return starts_at, expires_at


@router.get("/dashboards/{dashboard_id}/grants")
async def list_grants(dashboard_id: str, request: Request,
                actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)

    def run():
        with service.database.read_only() as connection:
            row = _load_dashboard(connection, service, require_uuid(dashboard_id), actor)
            grants = connection.execute(
                select(models.dashboard_grants).where(
                    models.dashboard_grants.c.dashboard_id == row["id"])
                .order_by(models.dashboard_grants.c.subject_type,
                          models.dashboard_grants.c.subject_id)).mappings().all()
            return {"items": [grant_view(g) for g in grants], "revision": row["revision"]}

    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(run)


def upsert_grant(connection, service: Service, actor: AuthContext, dashboard_row,
                 payload: dict, *, operation_id, trace_id, bump_revision: bool = True):
    subject_type = payload.get("subject_type")
    role = payload.get("role")
    if subject_type == "all_authenticated":
        require(role in (None, "viewer"), 422, "invalid_input",
                "Public access is always viewer")
        role = "viewer"
    require(role in ("viewer", "editor"), 422, "invalid_input",
            "role must be viewer or editor (owner is not grantable)")
    subject_id = _validate_subject(connection, subject_type, payload.get("subject_id"))
    existing = connection.execute(
        select(models.dashboard_grants).where(
            models.dashboard_grants.c.dashboard_id == dashboard_row["id"],
            models.dashboard_grants.c.subject_type == subject_type,
            models.dashboard_grants.c.subject_id == subject_id)).mappings().one_or_none()
    current = ((existing["starts_at"], existing["expires_at"]) if existing is not None
               else (None, None))
    incoming = {k: payload[k] for k in ("starts_at", "expires_at") if k in payload}
    starts_at, expires_at = _time_window(current, incoming)
    moment = to_db(now())
    if existing is None:
        connection.execute(models.dashboard_grants.insert().values(
            dashboard_id=dashboard_row["id"], subject_type=subject_type,
            subject_id=subject_id, role=role, starts_at=to_db(starts_at),
            expires_at=to_db(expires_at), created_by=actor.principal_id,
            created_at=moment, updated_at=moment))
    else:
        connection.execute(models.dashboard_grants.update().where(
            models.dashboard_grants.c.dashboard_id == dashboard_row["id"],
            models.dashboard_grants.c.subject_type == subject_type,
            models.dashboard_grants.c.subject_id == subject_id).values(
            role=role, starts_at=to_db(starts_at), expires_at=to_db(expires_at),
            updated_at=moment))
    revision = dashboard_row["revision"] + 1 if bump_revision else dashboard_row["revision"]
    if bump_revision:
        connection.execute(models.dashboards.update().where(
            models.dashboards.c.id == dashboard_row["id"]).values(
            revision=revision, updated_at=moment))
    record_audit(connection, actor=actor, action="grant.upsert",
                 target_type="dashboard", target_id=dashboard_row["id"],
                 before=grant_view(existing) if existing is not None else None,
                 after={"subject_type": subject_type, "subject_id": subject_id, "role": role},
                 trace_id=trace_id, operation_id=operation_id)
    return revision


@router.post("/dashboards/{dashboard_id}/grants")
def post_grant(dashboard_id: str, request: Request, payload: dict,
               actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)
    key = validate_idempotency_key(request.headers.get("idempotency-key"))
    expected_revision = payload.get("expected_revision")
    require(type(expected_revision) is int, 422, "invalid_input",
            "expected_revision is required")
    trace_id = getattr(request.state, "trace_id", "local")

    def unit(connection, operation_id):
        row = _load_dashboard(connection, service, dashboard_id, actor)
        require(row["revision"] == expected_revision, 409, "revision_conflict",
                "Dashboard changed; read it again")
        revision = upsert_grant(connection, service, actor, row, payload,
                                operation_id=operation_id, trace_id=trace_id)
        return {"dashboard_id": dashboard_id, "revision": revision}

    return service.operations.run_sync(
        actor, key, action="grant.upsert", method="POST",
        path=f"/api/v1/dashboards/{dashboard_id}/grants", target_id=dashboard_id,
        payload=payload, exclusive_guard=True, unit=unit, trace_id=trace_id)


@router.delete("/dashboards/{dashboard_id}/grants/{subject_type}/{subject_id}")
def delete_grant(dashboard_id: str, subject_type: str, subject_id: str, request: Request,
                 expected_revision: int, actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)
    key = validate_idempotency_key(request.headers.get("idempotency-key"))
    require(type(expected_revision) is int, 422, "invalid_input",
            "expected_revision query parameter is required")
    trace_id = getattr(request.state, "trace_id", "local")

    def unit(connection, operation_id):
        row = _load_dashboard(connection, service, dashboard_id, actor)
        require(row["revision"] == expected_revision, 409, "revision_conflict",
                "Dashboard changed; read it again")
        effective_subject = "*" if subject_type == "all_authenticated" else subject_id
        existing = connection.execute(
            select(models.dashboard_grants).where(
                models.dashboard_grants.c.dashboard_id == dashboard_id,
                models.dashboard_grants.c.subject_type == subject_type,
                models.dashboard_grants.c.subject_id == effective_subject)
        ).mappings().one_or_none()
        require(existing is not None, 404, "not_found", "Grant rule not found")
        connection.execute(delete(models.dashboard_grants).where(
            models.dashboard_grants.c.dashboard_id == dashboard_id,
            models.dashboard_grants.c.subject_type == subject_type,
            models.dashboard_grants.c.subject_id == effective_subject))
        revision = row["revision"] + 1
        moment = to_db(now())
        connection.execute(models.dashboards.update().where(
            models.dashboards.c.id == dashboard_id).values(
            revision=revision, updated_at=moment))
        record_audit(connection, actor=actor, action="grant.delete",
                     target_type="dashboard", target_id=dashboard_id,
                     before=grant_view(existing), trace_id=trace_id,
                     operation_id=operation_id)
        return {"dashboard_id": dashboard_id, "revision": revision}

    return service.operations.run_sync(
        actor, key, action="grant.delete", method="DELETE",
        path=f"/api/v1/dashboards/{dashboard_id}/grants/{subject_type}/{subject_id}",
        target_id=dashboard_id, payload={"expected_revision": expected_revision},
        exclusive_guard=True, unit=unit, trace_id=trace_id)


@router.put("/dashboards/{dashboard_id}/public-access")
def put_public_access(dashboard_id: str, request: Request, payload: dict,
                      actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)
    key = validate_idempotency_key(request.headers.get("idempotency-key"))
    expected_revision = payload.get("expected_revision")
    require(type(expected_revision) is int, 422, "invalid_input",
            "expected_revision is required")
    enabled = payload.get("enabled")
    require(type(enabled) is bool, 422, "invalid_input", "enabled must be a boolean")
    trace_id = getattr(request.state, "trace_id", "local")

    def unit(connection, operation_id):
        row = _load_dashboard(connection, service, dashboard_id, actor)
        require(row["revision"] == expected_revision, 409, "revision_conflict",
                "Dashboard changed; read it again")
        if enabled:
            upsert_grant(connection, service, actor, row,
                         {"subject_type": "all_authenticated", "role": "viewer",
                          "starts_at": payload.get("starts_at"),
                          "expires_at": payload.get("expires_at")},
                         operation_id=operation_id, trace_id=trace_id)
        else:
            connection.execute(delete(models.dashboard_grants).where(
                models.dashboard_grants.c.dashboard_id == dashboard_id,
                models.dashboard_grants.c.subject_type == "all_authenticated",
                models.dashboard_grants.c.subject_id == "*"))
            revision = row["revision"] + 1
            connection.execute(models.dashboards.update().where(
                models.dashboards.c.id == dashboard_id).values(
                revision=revision, updated_at=to_db(now())))
        record_audit(connection, actor=actor,
                     action="public_access.enable" if enabled else "public_access.disable",
                     target_type="dashboard", target_id=dashboard_id,
                     after={"enabled": enabled}, trace_id=trace_id,
                     operation_id=operation_id)
        fresh = service.authorizer.dashboard(connection, dashboard_id)
        return {"dashboard_id": dashboard_id, "revision": fresh["revision"]}

    return service.operations.run_sync(
        actor, key, action="public_access", method="PUT",
        path=f"/api/v1/dashboards/{dashboard_id}/public-access", target_id=dashboard_id,
        payload=payload, exclusive_guard=True, unit=unit, trace_id=trace_id)


@router.post("/dashboards/{dashboard_id}/access-changes")
def access_changes(dashboard_id: str, request: Request, payload: dict,
                   actor: AuthContext = Depends(get_actor)):
    """Atomic batch: validate everything, then apply; revision bumps once."""
    service: Service = get_service(request)
    key = validate_idempotency_key(request.headers.get("idempotency-key"))
    expected_revision = payload.get("expected_revision")
    require(type(expected_revision) is int, 422, "invalid_input",
            "expected_revision is required")
    changes = payload.get("changes")
    require(isinstance(changes, list) and 1 <= len(changes) <= 50, 422, "invalid_input",
            "changes must be a list of 1-50 items")
    trace_id = getattr(request.state, "trace_id", "local")

    def unit(connection, operation_id):
        row = _load_dashboard(connection, service, dashboard_id, actor)
        require(row["revision"] == expected_revision, 409, "revision_conflict",
                "Dashboard changed; read it again")
        for change in changes:
            require(isinstance(change, dict), 422, "invalid_input", "each change is an object")
            action = change.get("action")
            if action == "set_public":
                require(type(change.get("enabled")) is bool, 422, "invalid_input",
                        "set_public requires enabled")
            elif action == "grant":
                require(change.get("subject_type") in SUBJECT_TYPES, 422, "invalid_input",
                        "grant requires subject_type")
                role = change.get("role")
                if change.get("subject_type") == "all_authenticated":
                    require(role in (None, "viewer"), 422, "invalid_input",
                            "Public access is always viewer")
            elif action == "revoke":
                require(change.get("subject_type") in SUBJECT_TYPES, 422, "invalid_input",
                        "revoke requires subject_type")
            else:
                require(False, 422, "invalid_input",
                        "action must be set_public, grant or revoke")
        # Validation passed — apply against a re-read row (revision fixed).
        for change in changes:
            fresh = service.authorizer.dashboard(connection, dashboard_id)
            if change["action"] == "set_public":
                if change["enabled"]:
                    upsert_grant(connection, service, actor, fresh,
                                 {"subject_type": "all_authenticated", "role": "viewer",
                                  "starts_at": change.get("starts_at"),
                                  "expires_at": change.get("expires_at")},
                                 operation_id=operation_id, trace_id=trace_id,
                                 bump_revision=False)
                else:
                    connection.execute(delete(models.dashboard_grants).where(
                        models.dashboard_grants.c.dashboard_id == dashboard_id,
                        models.dashboard_grants.c.subject_type == "all_authenticated",
                        models.dashboard_grants.c.subject_id == "*"))
            elif change["action"] == "grant":
                upsert_grant(connection, service, actor, fresh, change,
                             operation_id=operation_id, trace_id=trace_id,
                             bump_revision=False)
            else:  # revoke
                subject_id = ("*" if change["subject_type"] == "all_authenticated"
                              else change.get("subject_id"))
                connection.execute(delete(models.dashboard_grants).where(
                    models.dashboard_grants.c.dashboard_id == dashboard_id,
                    models.dashboard_grants.c.subject_type == change["subject_type"],
                    models.dashboard_grants.c.subject_id == subject_id))
        # The whole batch bumps revision exactly once (contract §4).
        final_revision = service.authorizer.dashboard(connection, dashboard_id)["revision"] + 1
        connection.execute(models.dashboards.update().where(
            models.dashboards.c.id == dashboard_id).values(
            revision=final_revision, updated_at=to_db(now())))
        record_audit(connection, actor=actor, action="access_changes",
                     target_type="dashboard", target_id=dashboard_id,
                     after={"count": len(changes), "revision": final_revision},
                     trace_id=trace_id,
                     operation_id=operation_id)
        return {"dashboard_id": dashboard_id, "revision": final_revision}

    return service.operations.run_sync(
        actor, key, action="access_changes", method="POST",
        path=f"/api/v1/dashboards/{dashboard_id}/access-changes", target_id=dashboard_id,
        payload=payload, exclusive_guard=True, unit=unit, trace_id=trace_id)


@router.get("/dashboards/{dashboard_id}/access")
async def get_access(dashboard_id: str, request: Request, subject_id: str | None = None,
               actor: AuthContext = Depends(get_actor)):
    """Self-check, or an owner's check of one subject."""
    service: Service = get_service(request)

    def run():
        from ..authorization import ROLE_ACTIONS
        with service.database.read_only() as connection:
            row = service.authorizer.dashboard(connection, require_uuid(dashboard_id))
            if subject_id is not None:
                service.authorizer.authorize(connection, row, actor, "manage")
                from ..errors import is_uuid
                from ..authn import AuthContext as Ctx
                target_type = ("human" if connection.execute(
                    select(models.principals.c.id).where(
                        models.principals.c.id == subject_id,
                        models.principals.c.type == "human")).one_or_none()
                    else "service")
                target = Ctx(principal_id=subject_id, principal_type=target_type,
                             display_name="", scopes=(), valid_until=now(),
                             verified_at=now(), auth_method="lookup")
                access = service.authorizer.effective_access(connection, row, target)
            else:
                service.authorizer.authorize(connection, row, actor, "read")
                access = service.authorizer.effective_access(connection, row, actor)
            return {"role": access.role, "sources": list(access.sources),
                    "allowed_actions": ROLE_ACTIONS.get(access.role or "", []),
                    "as_of": now().isoformat().replace("+00:00", "Z")}

    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(run)

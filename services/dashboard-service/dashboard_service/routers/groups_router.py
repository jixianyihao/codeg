"""Group endpoints (contracts.md §4)."""
from fastapi import APIRouter, Depends, Request

from ..authn import AuthContext
from ..errors import require
from ..operations import validate_idempotency_key
from . import Service, get_actor, get_service

router = APIRouter(prefix="/api/v1", tags=["groups"])


@router.get("/groups")
async def list_groups(request: Request, q: str = "", cursor: str | None = None,
                limit: int | None = None, actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)
    page_limit = limit or service.config.page_size_default
    require(1 <= page_limit <= service.config.page_size_max, 422, "invalid_input",
            f"limit must be 1..{service.config.page_size_max}")

    def run():
        with service.database.read_only() as connection:
            items, next_cursor = service.groups.search(
                connection, actor, query=q, cursor=cursor, limit=page_limit)
            return {"items": items, "next_cursor": next_cursor}

    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(run)


@router.post("/groups")
def create_group(request: Request, payload: dict,
                 actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)
    key = validate_idempotency_key(request.headers.get("idempotency-key"))
    require(isinstance(payload, dict) and isinstance(payload.get("display_name"), str),
            422, "invalid_input", "display_name is required")
    return service.operations.run_sync(
        actor, key, action="group.create", method="POST", path="/api/v1/groups",
        target_id=None, payload=payload, exclusive_guard=True,
        unit=lambda connection, operation_id: service.groups.create(
            connection, actor, payload["display_name"]),
        replay_check=lambda conn: service.authorizer.check_context_validity(conn, actor),
        trace_id=getattr(request.state, "trace_id", "local"))


@router.get("/groups/{group_id}")
async def get_group(group_id: str, request: Request, actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)

    def run():
        with service.database.read_only() as connection:
            return service.groups.get(connection, group_id, actor)

    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(run)


@router.put("/groups/{group_id}/members")
def set_members(group_id: str, request: Request, payload: dict,
                actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)
    key = validate_idempotency_key(request.headers.get("idempotency-key"))
    require(isinstance(payload, dict), 422, "invalid_input", "Body must be an object")
    members = payload.get("members")
    require(isinstance(members, list), 422, "invalid_input", "members must be a list")

    def unit(connection, operation_id):
        return service.groups.set_members(connection, actor, group_id, members,
                                          payload.get("expected_revision"))

    return service.operations.run_sync(
        actor, key, action="group.set_members", method="PUT",
        path=f"/api/v1/groups/{group_id}/members", target_id=group_id,
        payload=payload, exclusive_guard=True, unit=unit,
        replay_check=lambda conn: service.groups.get(conn, group_id, actor),
        trace_id=getattr(request.state, "trace_id", "local"))

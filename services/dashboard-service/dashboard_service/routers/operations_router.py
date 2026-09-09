"""Operation queries (contracts.md §5)."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy import select

from .. import models
from ..authn import AuthContext
from ..errors import ApiError, require, require_uuid
from . import Service, get_actor, get_service

router = APIRouter(prefix="/api/v1", tags=["operations"])


def _visible_wrapper(connection, service: Service, actor: AuthContext, row) -> dict:
    """The stored result is only returned while its target is still visible
    to the caller — holding an operation_id grants nothing."""
    wrapper = service.operations.get_wrapper(row)
    result = wrapper.get("result") or {}
    dashboard_id = result.get("dashboard_id") or row["target_id"]
    if dashboard_id is None:
        return wrapper
    dashboard = service.authorizer.dashboard_or_none(connection, dashboard_id)
    if dashboard is None:
        require(not result.get("dashboard_id"), 404, "not_found", "Operation is not visible")
        return wrapper  # The target may be a group or another resource type.
    access = service.authorizer.effective_access(connection, dashboard, actor)
    require(access.role is not None, 404, "not_found", "Operation is not visible")
    if access.rank < 2 and result:
        if result.get("version_id"):
            version = connection.execute(select(models.dashboard_versions).where(
                models.dashboard_versions.c.id == result["version_id"],
                models.dashboard_versions.c.dashboard_id == dashboard_id)).mappings().one_or_none()
            require(service.authorizer.version_visible(dashboard, version, access),
                    404, "not_found", "Operation is not visible")
        # A direct publication may have retained an unrelated draft. Never
        # replay that stored private pointer to an editor who became a viewer.
        wrapper["result"] = {**result, "draft_version_id": None,
                             "draft_version_number": None, "has_draft": False}
    return wrapper


@router.get("/operations")
async def by_request_id(request: Request, request_id: str,
                  actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)
    if actor.principal_type == "service":
        actor.requires_scope("read")

    def run():
        with service.database.read_only() as connection:
            service.authorizer.check_context_validity(connection, actor)
            row = service.operations.find(connection, actor.principal_id,
                                          require_uuid(request_id, "request_id"))
            require(row is not None, 404, "not_found", "Operation is not visible")
            if row["result_purged_at"] is not None:
                raise ApiError(410, "idempotency_result_expired",
                               "The stored result expired; start a new operation with a new key")
            return _visible_wrapper(connection, service, actor, row)

    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(run)


@router.get("/operations/{operation_id}")
async def by_operation_id(operation_id: str, request: Request,
                    actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)
    if actor.principal_type == "service":
        actor.requires_scope("read")

    def run():
        with service.database.read_only() as connection:
            service.authorizer.check_context_validity(connection, actor)
            row = connection.execute(
                select(models.operations).where(
                    models.operations.c.id == require_uuid(operation_id),
                    models.operations.c.principal_id == actor.principal_id)
            ).mappings().one_or_none()
            require(row is not None, 404, "not_found", "Operation is not visible")
            if row["result_purged_at"] is not None:
                raise ApiError(410, "idempotency_result_expired",
                               "The stored result expired; start a new operation with a new key")
            return _visible_wrapper(connection, service, actor, row)

    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(run)

"""Control-origin view capability issuance (contracts.md §6)."""
from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from ..authn import AuthContext
from ..errors import require, require_uuid
from . import Service, get_actor, get_service

router = APIRouter(prefix="/api/v1", tags=["view"])


@router.post("/dashboards/{dashboard_id}/view-capabilities")
async def issue_capability(dashboard_id: str, request: Request, payload: dict | None = None,
                           actor: AuthContext = Depends(get_actor)):
    """Short-lived credential issuance — deliberately outside the generic
    operation machinery; retries simply mint a new capability."""
    service: Service = get_service(request)
    payload = payload or {}
    require(isinstance(payload, dict), 422, "invalid_input", "Body must be an object")
    require(set(payload) <= {"version_id"}, 422, "invalid_input", "Unknown fields")
    version_id = payload.get("version_id")
    if version_id is not None:
        require_uuid(version_id, "version_id")
    return await run_in_threadpool(
        lambda: service.capabilities.issue(actor, require_uuid(dashboard_id), version_id))

"""Identity and feature discovery (contracts.md §2)."""
from fastapi import APIRouter, Depends, Request
from sqlalchemy import or_, select

from .. import models
from ..authn import AuthContext
from ..errors import now, require
from . import Service, get_actor, get_service

router = APIRouter(prefix="/api/v1", tags=["meta"])


@router.get("/me")
def me(request: Request, actor: AuthContext = Depends(get_actor)):
    return {"principal_id": actor.principal_id,
            "principal_type": actor.principal_type,
            "display_name": actor.display_name,
            "scopes": list(actor.scopes),
            "is_admin": actor.is_admin}


@router.get("/capabilities")
def capabilities(request: Request, actor: AuthContext = Depends(get_actor)):
    service: Service = get_service(request)
    return {
        "api_major": 1,
        "features": ["publish", "versions", "rollback", "grants", "public_access",
                     "access_changes", "groups", "operations", "view_capabilities"],
        "max_upload_bytes": service.config.max_upload_bytes,
        "page_size_max": service.config.page_size_max,
        "content_origin": service.config.content_origin,
        "auth_methods": ["service_jwt"] + (["w3"] if service.config.w3_verify_url else []),
        "server_time": now().isoformat().replace("+00:00", "Z"),
    }


@router.get("/principals")
async def principals(request: Request, type: str | None = None, q: str = "",
               cursor: str | None = None, limit: int | None = None,
               actor: AuthContext = Depends(get_actor)):
    """Verified/trusted directory only: registered humans, service accounts,
    and local groups. Never a full enterprise directory dump."""
    service: Service = get_service(request)
    require(type in (None, "user", "service", "group"), 422, "invalid_input",
            "type must be user, service or group")
    page_limit = limit or service.config.page_size_default
    require(1 <= page_limit <= service.config.page_size_max, 422, "invalid_input",
            f"limit must be 1..{service.config.page_size_max}")

    def run():
        with service.database.read_only() as connection:
            service.authorizer.check_context_validity(connection, actor)
            items: list[dict] = []
            if type in (None, "user"):
                statement = select(models.principals.c.id, models.principals.c.display_name)\
                    .where(models.principals.c.type == "human")
                if q:
                    statement = statement.where(models.principals.c.display_name.like(f"%{q}%"))
                statement = statement.order_by(models.principals.c.display_name,
                                               models.principals.c.id)
                if cursor:
                    statement = statement.where(
                        or_(models.principals.c.display_name > cursor.split("|", 1)[0],
                            models.principals.c.display_name == cursor.split("|", 1)[0]))
                for row in connection.execute(statement.limit(page_limit)):
                    items.append({"id": row[0], "type": "user", "display_name": row[1]})
            if type in (None, "service"):
                statement = select(models.service_accounts.c.principal_id,
                                   models.service_accounts.c.name)
                if q:
                    statement = statement.where(models.service_accounts.c.name.like(f"%{q}%"))
                for row in connection.execute(statement.limit(page_limit)):
                    items.append({"id": row[0], "type": "service", "display_name": row[1]})
            if type in (None, "group"):
                items.extend(service.groups.search(connection, actor, query=q,
                                                   cursor=None, limit=page_limit)[0])
            return {"items": items[:page_limit], "next_cursor": None}

    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(run)

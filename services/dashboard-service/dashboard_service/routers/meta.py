"""Identity and feature discovery (contracts.md §2)."""
import base64
import hashlib
import hmac
import json

from fastapi import APIRouter, Depends, Request
from sqlalchemy import and_, literal, or_, select, union_all

from .. import models
from ..authn import AuthContext
from ..errors import ApiError, now, require
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
        "features": ["publish", "create_draft", "save_draft", "publish_draft",
                     "versions", "rollback", "grants", "public_access",
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
    page_limit = service.config.page_size_default if limit is None else limit
    require(1 <= page_limit <= service.config.page_size_max, 422, "invalid_input",
            f"limit must be 1..{service.config.page_size_max}")
    require(len(q) <= 200, 422, "invalid_input", "Search must be at most 200 characters")
    anchor = (_decode_directory_cursor(cursor, service.config.jwt_secret, actor, type, q)
              if cursor is not None else None)

    def run():
        with service.database.read_only() as connection:
            service.authorizer.check_context_validity(connection, actor)
            # One ordered directory query: per-type LIMIT followed by Python
            # truncation used to permanently hide groups/service accounts.
            # Explicit collation also makes name ties consistent across tables.
            collation = "utf8mb4_unicode_ci"
            directory = union_all(
                select(models.principals.c.id.label("id"), literal("user").label("type"),
                       models.principals.c.display_name.collate(collation).label("display_name"))
                .where(models.principals.c.type == "human"),
                select(models.service_accounts.c.principal_id.label("id"), literal("service"),
                       models.service_accounts.c.name.collate(collation).label("display_name")),
                select(models.groups.c.id.label("id"), literal("group"),
                       models.groups.c.display_name.collate(collation).label("display_name")),
            ).subquery("directory")
            name, kind, identity = (directory.c.display_name, directory.c.type, directory.c.id)
            statement = select(directory)
            if type is not None:
                statement = statement.where(kind == type)
            if q:
                statement = statement.where(name.contains(q, autoescape=True))
            if anchor:
                statement = statement.where(or_(
                    name > anchor["display_name"],
                    and_(name == anchor["display_name"], kind > anchor["type"]),
                    and_(name == anchor["display_name"], kind == anchor["type"],
                         identity > anchor["id"])))
            rows = connection.execute(statement.order_by(name, kind, identity)
                                      .limit(page_limit + 1)).mappings().all()
            items = [dict(row) for row in rows[:page_limit]]
            next_cursor = (_encode_directory_cursor(items[-1], service.config.jwt_secret,
                                                   actor, type, q)
                           if len(rows) > page_limit else None)
            return {"items": items, "next_cursor": next_cursor}

    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(run)


def _encode_directory_cursor(anchor, key, actor, subject_type, query):
    payload = {"principal_id": actor.principal_id, "subject_type": subject_type,
               "query": query, "anchor": anchor, "purpose": "principal-directory"}
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    signature = hmac.new(key, body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature + body).decode()


def _decode_directory_cursor(cursor, key, actor, subject_type, query):
    try:
        if len(cursor) > 4096:
            raise ValueError
        raw = base64.b64decode(cursor, altchars=b"-_", validate=True)
        signature, body = raw[:32], raw[32:]
        if not hmac.compare_digest(signature, hmac.new(key, body, hashlib.sha256).digest()):
            raise ValueError
        payload = json.loads(body)
        if (payload.get("principal_id") != actor.principal_id
                or payload.get("subject_type") != subject_type
                or payload.get("query") != query
                or payload.get("purpose") != "principal-directory"):
            raise ValueError
        anchor = payload["anchor"]
        if not all(isinstance(anchor.get(field), str) for field in ("id", "type", "display_name")):
            raise ValueError
        return anchor
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        raise ApiError(422, "invalid_input", "Cursor does not match this directory query") from None

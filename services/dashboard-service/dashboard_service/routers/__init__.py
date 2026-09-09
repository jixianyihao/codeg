"""Service bundle and shared router dependencies."""
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import select

from .. import models
from ..authn import AuthContext, Authenticator
from ..authorization import Authorizer
from ..capabilities import CapabilityService
from ..config import Config
from ..database import Database, from_db
from ..errors import ApiError, to_rfc3339
from ..groups_service import GroupsService
from ..operations import Operations
from ..publishing import Publisher, version_state
from ..storage import ContentStore


@dataclass
class Service:
    config: Config
    database: Database
    authenticator: Authenticator
    authorizer: Authorizer
    operations: Operations
    store: ContentStore
    publisher: Publisher
    groups: GroupsService
    capabilities: CapabilityService

    def close(self) -> None:
        self.authenticator.close()


def build_service(config: Config, database: Database, authenticator: Authenticator) -> Service:
    authorizer = Authorizer(database)
    operations = Operations(config, database)
    store = ContentStore(config)
    return Service(
        config=config, database=database, authenticator=authenticator,
        authorizer=authorizer, operations=operations, store=store,
        publisher=Publisher(config, database, operations, store, authorizer),
        groups=GroupsService(config, database),
        capabilities=CapabilityService(config, database, authorizer))


def get_service(request: Request) -> Service:
    return request.app.state.service


def get_actor(request: Request) -> AuthContext:
    service: Service = request.app.state.service
    if service.config.recovery_mode:
        raise ApiError(503, "recovery_isolation",
                       "Service is isolated for recovery; writes are rejected")
    return service.authenticator.authenticate(request.headers)


def reverify(request: Request):
    """Closure re-running full authentication with the same credential."""
    service: Service = request.app.state.service
    headers = request.headers
    return lambda: service.authenticator.authenticate(headers)


def dashboard_view(connection, service: Service, row, access) -> dict:
    owner = connection.execute(
        select(models.principals.c.display_name, models.principals.c.type)
        .where(models.principals.c.id == row["owner_principal_id"])).mappings().one()
    current_id = row["current_version_id"] if access.rank >= 1 else None
    draft_id = row["draft_version_id"] if access.rank >= 2 else None
    ids = [version_id for version_id in (current_id, draft_id) if version_id is not None]
    # Content summaries are resource-read metadata, not persisted operation
    # results. Resolve immutable versions from this row's pointers without S3.
    versions = {version["id"]: version for version in connection.execute(
        select(models.dashboard_versions.c.id, models.dashboard_versions.c.sha256,
               models.dashboard_versions.c.byte_size).where(
            models.dashboard_versions.c.dashboard_id == row["id"],
            models.dashboard_versions.c.id.in_(ids))).mappings()} if ids else {}
    current, draft = versions.get(current_id, {}), versions.get(draft_id, {})
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "owner_principal_id": row["owner_principal_id"],
        "owner_name": owner["display_name"],
        "owner_type": owner["type"],
        **version_state(connection, row, include_draft=access.rank >= 2),
        "current_version_sha256": current.get("sha256"),
        "current_version_byte_size": current.get("byte_size"),
        "draft_version_sha256": draft.get("sha256"),
        "draft_version_byte_size": draft.get("byte_size"),
        "revision": row["revision"],
        "status": row["status"],
        "role": access.role,
        "created_at": to_rfc3339(from_db(row["created_at"])),
        "updated_at": to_rfc3339(from_db(row["updated_at"])),
        "expires_at": access.expires_at,
        "view_url": f"{service.config.control_origin}/dashboards/{row['id']}",
    }


def grant_view(row) -> dict:
    return {
        "subject_type": row["subject_type"],
        "subject_id": row["subject_id"],
        "role": row["role"],
        "starts_at": to_rfc3339(from_db(row["starts_at"])),
        "expires_at": to_rfc3339(from_db(row["expires_at"])),
    }

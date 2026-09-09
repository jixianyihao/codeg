"""Service bundle and shared router dependencies."""
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import select

from .. import models
from ..authn import Authenticator, AuthContext
from ..authorization import Authorizer
from ..capabilities import CapabilityService
from ..config import Config
from ..database import Database, from_db
from ..errors import ApiError, to_rfc3339
from ..groups_service import GroupsService
from ..operations import Operations
from ..publishing import Publisher
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
    version_number = None
    if row["current_version_id"]:
        version_number = connection.execute(
            select(models.dashboard_versions.c.number).where(
                models.dashboard_versions.c.id == row["current_version_id"])).scalar_one_or_none()
    return {
        "id": row["id"],
        "title": row["title"],
        "description": row["description"],
        "owner_principal_id": row["owner_principal_id"],
        "owner_name": owner["display_name"],
        "owner_type": owner["type"],
        "current_version_id": row["current_version_id"],
        "current_version_number": version_number,
        "revision": row["revision"],
        "status": row["status"],
        "role": access.role,
        "created_at": to_rfc3339(from_db(row["created_at"])),
        "updated_at": to_rfc3339(from_db(row["updated_at"])),
        "published_at": to_rfc3339(from_db(row["published_at"])),
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

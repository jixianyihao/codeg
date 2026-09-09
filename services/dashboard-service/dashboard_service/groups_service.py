"""Local application groups (design.md §5): flat, human-created, owner-managed.

Groups have no nesting and no enterprise sync. Membership writes use the
exclusive authorization guard — they participate in ACL serialization.
"""
from sqlalchemy import delete, func, select

from . import models
from .authn import AuthContext
from .config import Config
from .database import Database, record_audit, to_db
from .errors import ApiError, new_id, now, require, require_uuid


class GroupsService:
    def __init__(self, config: Config, database: Database):
        self.config = config
        self.database = database

    def create(self, connection, actor: AuthContext, display_name: str) -> dict:
        """Runs inside the caller's transaction (operation guard held)."""
        require(actor.principal_type == "human", 403, "action_forbidden",
                "Only human users can create groups")
        require(isinstance(display_name, str) and 1 <= len(display_name.strip()) <= 200, 422,
                "invalid_input", "display_name must be 1-200 characters")
        group_id = new_id()
        moment = to_db(now())
        connection.execute(models.groups.insert().values(
            id=group_id, display_name=display_name.strip(),
            owner_principal_id=actor.principal_id, source="local",
            revision=1, created_at=moment, updated_at=moment))
        record_audit(connection, actor=actor, action="group.create",
                     target_type="group", target_id=group_id,
                     after={"display_name": display_name.strip()}, trace_id=new_id())
        return {"group_id": group_id, "revision": 1}

    def get(self, connection, group_id: str, actor: AuthContext) -> dict:
        group = connection.execute(
            select(models.groups).where(models.groups.c.id == group_id)).mappings().one_or_none()
        require(group is not None, 404, "not_found", "Group is not visible")
        require(group["owner_principal_id"] == actor.principal_id, 404, "not_found",
                "Group is not visible")
        members = [row[0] for row in connection.execute(
            select(models.group_members.c.human_principal_id)
            .where(models.group_members.c.group_id == group_id)
            .order_by(models.group_members.c.human_principal_id))]
        return {"id": group["id"], "display_name": group["display_name"],
                "owner_principal_id": group["owner_principal_id"],
                "revision": group["revision"], "members": members}

    def search(self, connection, actor: AuthContext, *, query: str, cursor, limit: int):
        """Group metadata is searchable by any authenticated user; member
        detail stays owner-only."""
        require(actor.principal_type in ("human", "service"), 401,
                "authentication_required", "Authentication required")
        statement = select(models.groups.c.id, models.groups.c.display_name,
                           models.groups.c.owner_principal_id, models.groups.c.revision)
        if query:
            statement = statement.where(models.groups.c.display_name.like(f"%{query}%"))
        statement = statement.order_by(models.groups.c.display_name, models.groups.c.id)
        if cursor is not None:
            statement = statement.where(models.groups.c.display_name > cursor)
        rows = connection.execute(statement.limit(limit + 1)).mappings().all()
        has_more = len(rows) > limit
        rows = rows[:limit]
        items = [{"id": r["id"], "display_name": r["display_name"],
                  "owner_principal_id": r["owner_principal_id"], "revision": r["revision"]}
                 for r in rows]
        next_cursor = rows[-1]["display_name"] if has_more and rows else None
        return items, next_cursor

    def set_members(self, connection, actor: AuthContext, group_id: str, members: list,
                    expected_revision: int) -> dict:
        """Full CAS membership replace inside the caller's transaction."""
        require(actor.principal_type == "human", 403, "action_forbidden",
                "Only human users can manage groups")
        require(isinstance(members, list) and len(members) <= self.config.max_group_members,
                422, "invalid_input",
                f"members must be a list of at most {self.config.max_group_members} IDs")
        unique_members = []
        for member in members:
            require_uuid(member, "members[]")
            if member not in unique_members:
                unique_members.append(member)
        group = connection.execute(
            select(models.groups).where(models.groups.c.id == group_id)
        ).mappings().one_or_none()
        require(group is not None, 404, "not_found", "Group is not visible")
        require(group["owner_principal_id"] == actor.principal_id, 404, "not_found",
                "Group is not visible")
        require(type(expected_revision) is int and group["revision"] == expected_revision,
                409, "revision_conflict", "Group changed; read it again")
        if unique_members:
            valid = {row[0] for row in connection.execute(
                select(models.principals.c.id).where(
                    models.principals.c.id.in_(unique_members),
                    models.principals.c.type == "human"))}
            unknown = [m for m in unique_members if m not in valid]
            require(not unknown, 422, "invalid_input",
                    "members must all be registered human principals")
        connection.execute(delete(models.group_members).where(
            models.group_members.c.group_id == group_id))
        moment = to_db(now())
        if unique_members:
            connection.execute(models.group_members.insert(), [
                {"group_id": group_id, "human_principal_id": member, "added_at": moment}
                for member in unique_members])
        revision = group["revision"] + 1
        connection.execute(models.groups.update().where(
            models.groups.c.id == group_id).values(revision=revision, updated_at=moment))
        record_audit(connection, actor=actor, action="group.set_members",
                     target_type="group", target_id=group_id,
                     before={"revision": group["revision"]},
                     after={"revision": revision, "member_count": len(unique_members)},
                     trace_id=new_id())
        return {"group_id": group_id, "revision": revision}

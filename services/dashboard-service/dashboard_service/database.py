"""Engine, transactions, and the authorization_guard lock discipline.

All connections use READ COMMITTED. Every final write transaction takes the
guard row first — FOR SHARE for ordinary content writes (they may proceed in
parallel), FOR UPDATE for authorization-relevant changes (ACL, group
membership, service-account state, ownership). See design.md §8.
"""
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Connection, Engine

from . import models
from .errors import new_id, now


def to_db(value: datetime | None) -> datetime | None:
    """MySQL DATETIME is timezone-less: persist naive UTC. None stays None."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def from_db(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def create_db_engine(database_url: str) -> Engine:
    return create_engine(
        database_url,
        pool_pre_ping=True,
        isolation_level="READ COMMITTED",
        connect_args={"connect_timeout": 5, "read_timeout": 30, "write_timeout": 30},
    )


class Database:
    def __init__(self, engine: Engine):
        self.engine = engine

    @contextmanager
    def transaction(self) -> Iterator[Connection]:
        with self.engine.begin() as connection:
            yield connection

    @contextmanager
    def read_only(self) -> Iterator[Connection]:
        with self.engine.connect() as connection:
            yield connection

    @contextmanager
    def guard(self, connection: Connection, *, exclusive: bool) -> Iterator[None]:
        """Serialize authorization changes against in-flight publishes.

        Lock order (design.md §8): guard → operation → principals/accounts →
        groups → dashboards → quota rows. Callers must not take these locks
        out of order.
        """
        statement = select(models.authorization_guard.c.revision).where(
            models.authorization_guard.c.id == 1)
        statement = statement.with_for_update(nowait=False) if exclusive else statement.with_for_update(
            of=models.authorization_guard, read=True)
        connection.execute(statement).one()
        yield

    def verify_schema(self) -> None:
        """Refuse to serve on an unmigrated or foreign database."""
        required = {"principals", "identity_links", "service_accounts", "groups", "group_members",
                    "dashboards", "dashboard_versions", "dashboard_grants", "operations",
                    "view_capabilities", "authorization_guard", "quota_usage",
                    "upload_reservations", "audit_events", "alembic_version"}
        with self.read_only() as connection:
            found = {
                row[0] for row in connection.execute(
                    text("SELECT table_name FROM information_schema.tables "
                         "WHERE table_schema = DATABASE()"))
            }
        missing = required - found
        if missing:
            raise RuntimeError(
                "database schema is missing tables; run alembic upgrade head first: "
                + ", ".join(sorted(missing)))

    def seed_guard(self) -> None:
        from sqlalchemy.dialects.mysql import insert
        with self.transaction() as connection:
            connection.execute(
                insert(models.authorization_guard).values(id=1, revision=1)
                .prefix_with("IGNORE"))


def record_audit(connection, *, actor, action, target_type, target_id,
                 before=None, after=None, reason=None, trace_id: str, operation_id=None) -> None:
    connection.execute(models.audit_events.insert().values(
        id=new_id(),
        actor_principal_id=actor.principal_id,
        actor_type=actor.principal_type,
        auth_method=actor.auth_method,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        before_summary=before,
        after_summary=after,
        reason=reason,
        trace_id=trace_id,
        operation_id=operation_id,
        created_at=to_db(now()),
    ))

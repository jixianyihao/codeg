"""T1: migration integrity on the real MySQL database."""
import sqlalchemy
import pytest

from dashboard_service import models

from .conftest import TEST_DATABASE_URL, requires_mysql

pytestmark = requires_mysql


def test_all_tables_present(migrated_database):
    with migrated_database.read_only() as connection:
        found = {row[0] for row in connection.exec_driver_sql(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()")}
    expected = {"principals", "identity_links", "service_accounts", "groups", "group_members",
                "dashboards", "dashboard_versions", "dashboard_grants", "operations",
                "view_capabilities", "authorization_guard", "quota_usage",
                "upload_reservations", "audit_events"}
    assert expected <= found


def test_engine_and_collation(migrated_database):
    with migrated_database.read_only() as connection:
        engine = connection.exec_driver_sql(
            "SELECT engine FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = 'dashboards'").scalar_one()
        assert engine == "InnoDB"
        isolation = connection.exec_driver_sql(
            "SELECT @@transaction_isolation").scalar_one()
        assert isolation in ("READ-COMMITTED", "READ COMMITTED")


def test_unique_constraints_enforced_by_database(migrated_database):
    """The database itself rejects duplicates — not only the application."""
    from dashboard_service.database import to_db
    from dashboard_service.errors import new_id, now
    moment = to_db(now())
    with migrated_database.transaction() as connection:
        connection.execute(models.principals.insert().values(
            id=new_id(), type="service", display_name="dup-check", created_at=moment))
        principal_id = connection.execute(
            sqlalchemy.select(models.principals.c.id).where(
                models.principals.c.display_name == "dup-check")).scalar_one()
        connection.execute(models.service_accounts.insert().values(
            principal_id=principal_id, name="dup-account", enabled=True,
            token_version=1, scopes=["read"], revision=1,
            created_at=moment, updated_at=moment))
    with pytest.raises(Exception):
        with migrated_database.transaction() as connection:
            pid = connection.execute(
                sqlalchemy.select(models.principals.c.id).where(
                    models.principals.c.display_name == "dup-check")).scalar_one()
            connection.execute(models.service_accounts.insert().values(
                principal_id=new_id(), name="dup-account", enabled=True,
                token_version=1, scopes=["read"], revision=1,
                created_at=moment, updated_at=moment))


def test_operations_unique_principal_key(migrated_database):
    from dashboard_service.database import to_db
    from dashboard_service.errors import new_id, now
    moment = to_db(now())
    principal = new_id()
    key = new_id()
    with migrated_database.transaction() as connection:
        connection.execute(models.principals.insert().values(
            id=principal, type="service", display_name="op-dup", created_at=moment))
        connection.execute(models.operations.insert().values(
            id=new_id(), principal_id=principal, idempotency_key=key,
            action="x", method="POST", path="/x", request_hash="a" * 64,
            state="succeeded", attempt_id=new_id(), lease_until=None,
            result_expires_at=moment, created_at=moment, updated_at=moment))
    with pytest.raises(Exception):
        with migrated_database.transaction() as connection:
            connection.execute(models.operations.insert().values(
                id=new_id(), principal_id=principal, idempotency_key=key,
                action="x", method="POST", path="/x", request_hash="a" * 64,
                state="succeeded", attempt_id=new_id(), lease_until=None,
                result_expires_at=moment, created_at=moment, updated_at=moment))


def test_verify_schema_rejects_foreign_database(migrated_database):
    """verify_schema must refuse to serve on a database missing tables.

    Uses the pre-granted empty scratch database (CI grants the test user
    rights on it; it stays empty by design).
    """
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from dashboard_service.database import Database
    url = make_url(TEST_DATABASE_URL).set(
        database="aresclaw_dash_scratch_verify").render_as_string(hide_password=False)
    foreign = Database(create_engine(url))
    try:
        foreign.verify_schema()
        raised = False
    except RuntimeError:
        raised = True
    finally:
        foreign.engine.dispose()
    assert raised, "verify_schema must fail on an unmigrated database"

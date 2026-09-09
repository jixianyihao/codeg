"""Normalized MySQL schema (contracts.md §8). InnoDB, UTC DATETIME(6), utf8mb4.

Core (Table) with explicit constraints — enforcement lives in the database,
not only in ORM application checks.
"""
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME as MYSQL_DATETIME

metadata = MetaData()
ID = String(36, collation="utf8mb4_bin").with_variant(String(36, collation="utf8mb4_bin"), "mysql")
DT = DateTime().with_variant(MYSQL_DATETIME(fsp=6), "mysql")


def utc_columns(*names):
    return [Column(name, DT, nullable=False) for name in names]


principals = Table(
    "principals", metadata,
    Column("id", ID, primary_key=True),
    Column("type", String(8, collation="ascii_bin"), nullable=False),
    Column("display_name", String(200, collation="utf8mb4_0900_as_ci"), nullable=False),
    Column("created_at", DT, nullable=False),
    CheckConstraint("type IN ('human','service')", name="ck_principals_type"),
)

identity_links = Table(
    "identity_links", metadata,
    Column("issuer", String(191, collation="utf8mb4_bin"), primary_key=True),
    Column("enterprise_user_id", String(191, collation="utf8mb4_bin"), primary_key=True),
    Column("principal_id", ID, ForeignKey("principals.id"), nullable=False),
    Column("created_at", DT, nullable=False),
)

service_accounts = Table(
    "service_accounts", metadata,
    Column("principal_id", ID, ForeignKey("principals.id"), primary_key=True),
    Column("name", String(200, collation="utf8mb4_bin"), nullable=False, unique=True),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("token_version", Integer, nullable=False, default=1),
    Column("scopes", JSON, nullable=False),
    Column("revision", Integer, nullable=False, default=1),
    *utc_columns("created_at", "updated_at"),
    CheckConstraint("JSON_VALID(scopes)", name="ck_service_accounts_scopes_json"),
)

groups = Table(
    "groups", metadata,
    Column("id", ID, primary_key=True),
    Column("display_name", String(200, collation="utf8mb4_0900_as_ci"), nullable=False),
    Column("owner_principal_id", ID, ForeignKey("principals.id"), nullable=False),
    Column("source", String(16, collation="ascii_bin"), nullable=False, default="local"),
    Column("revision", Integer, nullable=False, default=1),
    *utc_columns("created_at", "updated_at"),
    CheckConstraint("source = 'local'", name="ck_groups_local"),
)

group_members = Table(
    "group_members", metadata,
    Column("group_id", ID, ForeignKey("groups.id"), primary_key=True),
    Column("human_principal_id", ID, ForeignKey("principals.id"), primary_key=True),
    Column("added_at", DT, nullable=False),
)

# current_version_id is added as a two-step composite FK in the initial
# migration (dashboards ⇄ dashboard_versions reference each other).
dashboards = Table(
    "dashboards", metadata,
    Column("id", ID, primary_key=True),
    Column("owner_principal_id", ID, ForeignKey("principals.id", name="fk_dashboards_owner"),
           nullable=False),
    Column("title", String(200, collation="utf8mb4_0900_as_ci"), nullable=False),
    Column("description", String(2000, collation="utf8mb4_0900_as_ci"), nullable=False, default=""),
    Column("current_version_id", ID, nullable=True),
    Column("draft_version_id", ID, nullable=True),
    Column("revision", Integer, nullable=False, default=1),
    Column("status", String(16, collation="ascii_bin"), nullable=False, default="draft"),
    Column("created_at", DT, nullable=False),
    Column("updated_at", DT, nullable=False),
    Column("published_at", DT, nullable=True),
    CheckConstraint("status IN ('draft','published','archived')", name="ck_dashboards_status"),
    CheckConstraint("revision >= 1", name="ck_dashboards_revision"),
    ForeignKeyConstraint(["id", "current_version_id"],
                         ["dashboard_versions.dashboard_id", "dashboard_versions.id"],
                         name="fk_dashboards_current_version", use_alter=True,
                         ondelete="RESTRICT"),
    ForeignKeyConstraint(["id", "draft_version_id"],
                         ["dashboard_versions.dashboard_id", "dashboard_versions.id"],
                         name="fk_dashboards_draft_version", use_alter=True,
                         ondelete="RESTRICT"),
    Index("ix_dashboards_owner_status_updated", "owner_principal_id", "status", "updated_at", "id"),
    Index("ix_dashboards_updated", "updated_at", "id"),
)

dashboard_versions = Table(
    "dashboard_versions", metadata,
    Column("id", ID, primary_key=True),
    Column("dashboard_id", ID, ForeignKey("dashboards.id"), nullable=False),
    Column("number", Integer, nullable=False),
    Column("storage_bucket", String(63, collation="ascii_bin"), nullable=False),
    Column("storage_key", String(512, collation="ascii_bin"), nullable=False),
    Column("object_version_id", String(64, collation="ascii_bin"), nullable=True),
    Column("sha256", String(64, collation="ascii_bin"), nullable=False),
    Column("byte_size", BigInteger, nullable=False),
    Column("created_by", ID, ForeignKey("principals.id"), nullable=False),
    Column("created_at", DT, nullable=False),
    Column("published_at", DT, nullable=True),
    UniqueConstraint("dashboard_id", "number", name="uq_versions_dashboard_number"),
    UniqueConstraint("dashboard_id", "id", name="uq_versions_dashboard_id"),
    UniqueConstraint("storage_bucket", "storage_key", name="uq_versions_storage_object"),
    Index("ix_versions_dashboard_created", "dashboard_id", "created_at"),
)

dashboard_grants = Table(
    "dashboard_grants", metadata,
    Column("dashboard_id", ID, ForeignKey("dashboards.id"), primary_key=True),
    Column("subject_type", String(20, collation="ascii_bin"), primary_key=True),
    Column("subject_id", String(191, collation="utf8mb4_bin"), primary_key=True),
    Column("role", String(8, collation="ascii_bin"), nullable=False),
    Column("starts_at", DT, nullable=True),
    Column("expires_at", DT, nullable=True),
    Column("created_by", ID, ForeignKey("principals.id"), nullable=False),
    Column("updated_at", DT, nullable=False),
    Column("created_at", DT, nullable=False),
    CheckConstraint("role IN ('viewer','editor')", name="ck_grants_role"),
    CheckConstraint("starts_at IS NULL OR expires_at IS NULL OR starts_at < expires_at",
                    name="ck_grants_window"),
    Index("ix_grants_subject", "subject_type", "subject_id"),
)

operations = Table(
    "operations", metadata,
    Column("id", ID, primary_key=True),
    Column("principal_id", ID, ForeignKey("principals.id"), nullable=False),
    Column("idempotency_key", String(36, collation="ascii_bin"), nullable=False),
    Column("action", String(64, collation="ascii_bin"), nullable=False),
    Column("method", String(8, collation="ascii_bin"), nullable=False),
    Column("path", String(256, collation="ascii_bin"), nullable=False),
    Column("target_id", ID, nullable=True),
    Column("request_hash", String(64, collation="ascii_bin"), nullable=False),
    Column("state", String(16, collation="ascii_bin"), nullable=False),
    Column("attempt_id", ID, nullable=False),
    Column("lease_until", DT, nullable=True),
    Column("result", JSON, nullable=True),
    Column("error", JSON, nullable=True),
    Column("result_expires_at", DT, nullable=False),
    Column("result_purged_at", DT, nullable=True),
    Column("created_at", DT, nullable=False),
    Column("updated_at", DT, nullable=False),
    UniqueConstraint("principal_id", "idempotency_key", name="uq_operations_principal_key"),
    CheckConstraint("state IN ('accepted','processing','succeeded','failed')",
                    name="ck_operations_state"),
    Index("ix_operations_principal_created", "principal_id", "created_at"),
    Index("ix_operations_state_lease", "state", "lease_until"),
)

view_capabilities = Table(
    "view_capabilities", metadata,
    Column("token_digest", String(64, collation="ascii_bin"), primary_key=True),
    Column("human_principal_id", ID, ForeignKey("principals.id"), nullable=False),
    Column("identity_session_ref", String(191, collation="utf8mb4_bin"), nullable=True),
    Column("session_version", BigInteger, nullable=True),
    Column("dashboard_id", ID, ForeignKey("dashboards.id"), nullable=False),
    Column("version_id", ID, ForeignKey("dashboard_versions.id"), nullable=False),
    Column("created_at", DT, nullable=False),
    Column("expires_at", DT, nullable=False),
    Index("ix_view_capabilities_expiry", "expires_at"),
)

authorization_guard = Table(
    "authorization_guard", metadata,
    Column("id", Integer, primary_key=True),
    Column("revision", BigInteger, nullable=False, default=1),
)

quota_usage = Table(
    "quota_usage", metadata,
    Column("scope", String(8, collation="ascii_bin"), primary_key=True),
    Column("owner_id", ID, primary_key=True),
    Column("used_bytes", BigInteger, nullable=False, default=0),
    Column("reserved_bytes", BigInteger, nullable=False, default=0),
    Column("dashboard_count", Integer, nullable=False, default=0),
    Column("reserved_count", Integer, nullable=False, default=0),
    CheckConstraint("scope IN ('owner','global')", name="ck_quota_scope"),
    CheckConstraint("used_bytes >= 0 AND reserved_bytes >= 0", name="ck_quota_bytes"),
)

upload_reservations = Table(
    "upload_reservations", metadata,
    Column("operation_id", ID, primary_key=True),
    Column("attempt_id", ID, primary_key=True),
    Column("owner_id", ID, nullable=False),
    # Object coordinates for this attempt; NULL until the S3 PUT succeeds.
    Column("dashboard_id", ID, nullable=False),
    Column("version_id", ID, nullable=False),
    Column("storage_bucket", String(63, collation="ascii_bin"), nullable=True),
    Column("storage_key", String(512, collation="ascii_bin"), nullable=True),
    Column("object_version_id", String(64, collation="ascii_bin"), nullable=True),
    # reserved → uploaded → committed | cleanup_pending → deleted
    Column("state", String(16, collation="ascii_bin"), nullable=False),
    Column("reserved_bytes", BigInteger, nullable=False),
    Column("reserved_count", Integer, nullable=False),
    Column("expires_at", DT, nullable=False),
    CheckConstraint("state IN ('reserved','uploaded','committed','cleanup_pending','deleted')",
                    name="ck_reservations_state"),
    Index("ix_reservations_expiry", "expires_at"),
    Index("ix_reservations_state", "state"),
)

audit_events = Table(
    "audit_events", metadata,
    Column("id", ID, primary_key=True),
    Column("actor_principal_id", ID, nullable=False),
    Column("actor_type", String(8, collation="ascii_bin"), nullable=False),
    Column("auth_method", String(8, collation="ascii_bin"), nullable=False),
    Column("action", String(64, collation="ascii_bin"), nullable=False),
    Column("target_type", String(32, collation="ascii_bin"), nullable=False),
    Column("target_id", String(191, collation="utf8mb4_bin"), nullable=False),
    Column("before_summary", JSON, nullable=True),
    Column("after_summary", JSON, nullable=True),
    Column("reason", String(500, collation="utf8mb4_0900_as_ci"), nullable=True),
    Column("trace_id", ID, nullable=False),
    Column("operation_id", ID, nullable=True),
    Column("created_at", DT, nullable=False),
    Index("ix_audit_target", "target_type", "target_id", "created_at"),
)

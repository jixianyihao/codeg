"""S3 object storage references and attempt-scoped reservations.

Revision ID: b41d7c2f9013
Revises: 4b58c2e3ae59
Create Date: 2026-09-09

Published HTML moves from a local content directory to private S3:
dashboard_versions gains bucket/key/object_version_id references, and
upload_reservations becomes per-attempt with an explicit lifecycle state
so objects uploaded but never committed can be found and deleted exactly.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "b41d7c2f9013"
down_revision = "4b58c2e3ae59"
branch_labels = None
depends_on = None

ID = sa.String(length=36, collation="utf8mb4_bin").with_variant(
    sa.String(length=36, collation="utf8mb4_bin"), "mysql")
DT = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.add_column("dashboard_versions",
                  sa.Column("storage_bucket", sa.String(length=63, collation="ascii_bin"),
                            nullable=False, server_default=""))
    op.add_column("dashboard_versions",
                  sa.Column("object_version_id", sa.String(length=64, collation="ascii_bin"),
                            nullable=True))
    op.alter_column("dashboard_versions", "storage_key",
                    existing_type=sa.String(length=64, collation="ascii_bin"),
                    type_=sa.String(length=512, collation="ascii_bin"),
                    existing_nullable=False)
    op.drop_index("storage_key", table_name="dashboard_versions")
    op.create_unique_constraint("uq_versions_storage_object", "dashboard_versions",
                                ["storage_bucket", "storage_key"])

    # Reservations are transient attempt state: recreate with the new shape.
    op.drop_index("ix_reservations_expiry", table_name="upload_reservations")
    op.drop_table("upload_reservations")
    op.create_table(
        "upload_reservations",
        sa.Column("operation_id", ID, nullable=False),
        sa.Column("attempt_id", ID, nullable=False),
        sa.Column("owner_id", ID, nullable=False),
        sa.Column("dashboard_id", ID, nullable=False),
        sa.Column("version_id", ID, nullable=False),
        sa.Column("storage_bucket", sa.String(length=63, collation="ascii_bin"), nullable=True),
        sa.Column("storage_key", sa.String(length=512, collation="ascii_bin"), nullable=True),
        sa.Column("object_version_id", sa.String(length=64, collation="ascii_bin"), nullable=True),
        sa.Column("state", sa.String(length=16, collation="ascii_bin"), nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("reserved_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", DT, nullable=False),
        sa.PrimaryKeyConstraint("operation_id", "attempt_id"),
        sa.CheckConstraint(
            "state IN ('reserved','uploaded','committed','cleanup_pending','deleted')",
            name="ck_reservations_state"),
    )
    op.create_index("ix_reservations_expiry", "upload_reservations", ["expires_at"])
    op.create_index("ix_reservations_state", "upload_reservations", ["state"])


def downgrade() -> None:
    # Pre-release destructive downgrade: S3 reference keys cannot fit the
    # old VARCHAR(64) column, so the version history is cleared instead of
    # being silently corrupted. Deployed databases must restore from backup
    # rather than downgrade through this revision.
    op.execute("SET FOREIGN_KEY_CHECKS = 0")
    for table in ("upload_reservations", "view_capabilities", "dashboard_grants",
                  "dashboard_versions", "dashboards", "operations", "audit_events"):
        op.execute(f"DELETE FROM {table}")
    op.execute("SET FOREIGN_KEY_CHECKS = 1")
    op.drop_index("ix_reservations_state", table_name="upload_reservations")
    op.drop_index("ix_reservations_expiry", table_name="upload_reservations")
    op.drop_table("upload_reservations")
    op.create_table(
        "upload_reservations",
        sa.Column("operation_id", ID, nullable=False),
        sa.Column("attempt_id", ID, nullable=False),
        sa.Column("owner_id", ID, nullable=False),
        sa.Column("reserved_bytes", sa.BigInteger(), nullable=False),
        sa.Column("reserved_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", DT, nullable=False),
        sa.PrimaryKeyConstraint("operation_id"),
    )
    op.create_index("ix_reservations_expiry", "upload_reservations", ["expires_at"])
    op.drop_constraint("uq_versions_storage_object", "dashboard_versions", type_="unique")
    op.alter_column("dashboard_versions", "storage_key",
                    existing_type=sa.String(length=512, collation="ascii_bin"),
                    type_=sa.String(length=64, collation="ascii_bin"),
                    existing_nullable=False)
    op.create_unique_constraint(None, "dashboard_versions", ["storage_key"])
    op.drop_column("dashboard_versions", "object_version_id")
    op.drop_column("dashboard_versions", "storage_bucket")

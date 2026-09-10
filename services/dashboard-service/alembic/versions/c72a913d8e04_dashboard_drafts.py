"""Independent drafts and first-publication provenance; preserve existing content.

Revision ID: c72a913d8e04
Revises: b41d7c2f9013
"""
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision = "c72a913d8e04"
down_revision = "b41d7c2f9013"
branch_labels = None
depends_on = None
DT = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")

def _check_constraints_enforced() -> bool:
    """MySQL 8.0.16+ enforces CHECK constraints; 5.7 parses and ignores
    them in CREATE TABLE and has no ALTER TABLE ... CHECK syntax at all,
    so the drop/recreate pair must be skipped there (nothing was stored)."""
    version = op.get_bind().dialect.server_version_info
    return version is not None and version >= (8, 0, 16)


def _replace_status_check(states: str) -> None:
    if not _check_constraints_enforced():
        return
    op.drop_constraint("ck_dashboards_status", "dashboards", type_="check")
    op.create_check_constraint("ck_dashboards_status", "dashboards",
                               f"status IN ({states})")




def upgrade() -> None:
    op.add_column("dashboards", sa.Column(
        "draft_version_id", sa.String(36, collation="utf8mb4_bin"), nullable=True))
    op.add_column("dashboard_versions", sa.Column("published_at", DT, nullable=True))
    # Every version in the old model was published when created. The live
    # dashboard pointer/timestamps and archived status remain untouched.
    op.execute("UPDATE dashboard_versions SET published_at = created_at")
    op.alter_column("dashboards", "published_at", existing_type=DT, nullable=True)
    _replace_status_check("'draft','published','archived'")
    op.create_foreign_key("fk_dashboards_draft_version", "dashboards", "dashboard_versions",
                          ["id", "draft_version_id"], ["dashboard_id", "id"], ondelete="RESTRICT")


def downgrade() -> None:
    # MySQL DDL is not transactional. Refuse BEFORE the first mutation: old
    # code treats every version as public history, and cannot retain drafts.
    connection = op.get_bind()
    private_content = connection.execute(sa.text(
        "SELECT EXISTS(SELECT 1 FROM dashboard_versions WHERE published_at IS NULL) "
        "OR EXISTS(SELECT 1 FROM dashboards WHERE status = 'draft' "
        "OR draft_version_id IS NOT NULL OR published_at IS NULL)"
    )).scalar_one()
    if private_content:
        raise RuntimeError("Cannot downgrade while draft state or unpublished history exists")
    op.drop_constraint("fk_dashboards_draft_version", "dashboards", type_="foreignkey")
    op.drop_column("dashboards", "draft_version_id")
    op.drop_column("dashboard_versions", "published_at")
    op.alter_column("dashboards", "published_at", existing_type=DT, nullable=False)
    _replace_status_check("'published','archived'")

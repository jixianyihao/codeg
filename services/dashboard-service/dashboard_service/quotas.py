"""Quota reservations on quota_usage rows (design.md §9).

Owner and global rows are locked in a fixed order (owner, then global) and
checked as used + reserved. Uploads reserve before staging; success converts
the reservation to usage, failure releases it.
"""
from sqlalchemy import func, select

from . import models
from .config import Config
from .errors import ApiError


def ensure_rows(connection, owner_id: str) -> None:
    connection.execute(models.quota_usage.insert().prefix_with("IGNORE").values(
        scope="global", owner_id="*", used_bytes=0, reserved_bytes=0,
        dashboard_count=0, reserved_count=0))
    connection.execute(models.quota_usage.insert().prefix_with("IGNORE").values(
        scope="owner", owner_id=owner_id, used_bytes=0, reserved_bytes=0,
        dashboard_count=0, reserved_count=0))


def check_headroom(connection, config: Config, owner_id: str, *, extra_bytes: int,
                   new_dashboard: bool) -> None:
    """Lock both rows FOR UPDATE (owner first, then global) and verify the
    post-reservation totals stay within the configured limits."""
    rows = {}
    for scope, owner in (("owner", owner_id), ("global", "*")):
        rows[scope] = connection.execute(
            select(models.quota_usage.c.used_bytes, models.quota_usage.c.reserved_bytes,
                   models.quota_usage.c.dashboard_count, models.quota_usage.c.reserved_count)
            .where(models.quota_usage.c.scope == scope,
                   models.quota_usage.c.owner_id == owner)
            .with_for_update()).mappings().one()
    owner_row, global_row = rows["owner"], rows["global"]
    if owner_row["used_bytes"] + owner_row["reserved_bytes"] + extra_bytes > config.max_owner_bytes:
        raise ApiError(507, "quota_exceeded",
                       "Owner content quota exceeded; remove old versions or raise the limit")
    if global_row["used_bytes"] + global_row["reserved_bytes"] + extra_bytes > config.max_total_bytes:
        raise ApiError(507, "quota_exceeded", "Service storage quota exceeded")
    if new_dashboard and (owner_row["dashboard_count"] + owner_row["reserved_count"]
                          >= config.max_owner_dashboards):
        raise ApiError(507, "quota_exceeded", "Owner dashboard quota exceeded")


def count_owner_dashboards(connection, owner_id: str) -> int:
    return connection.execute(
        select(func.count()).select_from(models.dashboards).where(
            models.dashboards.c.owner_principal_id == owner_id)).scalar_one()


def count_dashboard_versions(connection, dashboard_id: str) -> int:
    return connection.execute(
        select(func.count()).select_from(models.dashboard_versions).where(
            models.dashboard_versions.c.dashboard_id == dashboard_id)).scalar_one()

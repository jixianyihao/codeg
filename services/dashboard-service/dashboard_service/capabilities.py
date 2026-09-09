"""Short-lived view capabilities (design.md §7.2, contracts.md §6).

A capability is a 256-bit random token; only its SHA-256 digest is stored.
It pins one human principal, one dashboard, one HTML version, and expires
within min(60s, identity validity, the chosen grant's remaining window).
The W3 token is never stored. Capabilities work only at the content origin.
"""
import hashlib
import secrets
from datetime import timedelta

from sqlalchemy import delete, select

from . import models
from .authn import AuthContext
from .authorization import Authorizer
from .config import Config
from .database import Database, from_db, to_db
from .errors import ApiError, now, require

CAPABILITY_TTL_SECONDS = 60


class IdentityRevocationSource:
    """Shared revocation check between control and content apps.

    `is_revoked` returns True (revoked), False (definitely active) or None
    (unknown). Only evidence the existing W3 integration can verify counts;
    browser self-reported state never does. Unknown falls back to the
    capability's bounded lifetime.
    """

    def is_revoked(self, issuer: str | None, session_ref: str | None,
                   verified_at) -> bool | None:
        return None if session_ref else None


class CapabilityService:
    def __init__(self, config: Config, database: Database, authorizer: Authorizer,
                 revocation: IdentityRevocationSource | None = None):
        self.config = config
        self.database = database
        self.authorizer = authorizer
        self.revocation = revocation or IdentityRevocationSource()

    def issue(self, actor: AuthContext, dashboard_id: str, version_id: str | None) -> dict:
        require(actor.principal_type == "human", 403, "action_forbidden",
                "Rendering capabilities are issued to human users only")
        moment = now()
        with self.database.transaction() as connection:
            dashboard = self.authorizer.dashboard(connection, dashboard_id)
            access = self.authorizer.authorize(connection, dashboard, actor, "read",
                                               moment=moment)
            require(dashboard["status"] == "published", 404, "not_found",
                    "Dashboard is not visible")
            target_version_id = version_id or dashboard["current_version_id"]
            version = connection.execute(
                select(models.dashboard_versions.c.id, models.dashboard_versions.c.created_at)
                .where(models.dashboard_versions.c.id == target_version_id,
                       models.dashboard_versions.c.dashboard_id == dashboard_id)
            ).mappings().one_or_none()
            require(version is not None, 404, "not_found", "Version is not visible")
            # Basis expiry: the longest-lived effective source granting at
            # least viewer, capped by identity validity and the 60s window.
            basis = _latest_expiry(access.sources) or moment + timedelta(days=365)
            expires = min(moment + timedelta(seconds=CAPABILITY_TTL_SECONDS),
                          actor.valid_until, basis)
            token = secrets.token_urlsafe(32)
            digest = hashlib.sha256(token.encode()).hexdigest()
            connection.execute(delete(models.view_capabilities).where(
                models.view_capabilities.c.expires_at < to_db(moment)))
            connection.execute(models.view_capabilities.insert().values(
                token_digest=digest, human_principal_id=actor.principal_id,
                identity_session_ref=actor.identity_session_ref,
                session_version=1, dashboard_id=dashboard_id,
                version_id=target_version_id, created_at=to_db(moment),
                expires_at=to_db(expires)))
        # Stable single-layer flow: the capability rides in the fragment of
        # the content-origin trusted loader /view/{id}; the loader is the
        # only page that redeems it (contracts.md section 6).
        return {"render_url": f"{self.config.content_origin}/view/{dashboard_id}#{token}",
                "expires_at": expires.isoformat().replace("+00:00", "Z")}

    def resolve(self, token: str):
        """Content-origin credential: digest lookup + full re-authorization
        against the current ACL, archived state and revocation source."""
        require(isinstance(token, str) and 0 < len(token) <= 256, 401,
                "invalid_capability", "View capability is invalid")
        digest = hashlib.sha256(token.encode()).hexdigest()
        moment = now()
        with self.database.read_only() as connection:
            row = connection.execute(
                select(models.view_capabilities).where(
                    models.view_capabilities.c.token_digest == digest)
            ).mappings().one_or_none()
            require(row is not None, 401, "invalid_capability",
                    "View capability is invalid")
            require(from_db(row["expires_at"]) > moment, 401, "invalid_capability",
                    "View capability expired")
            dashboard = self.authorizer.dashboard_or_none(connection, row["dashboard_id"])
            # Human principal row drives the ACL re-check.
            from .authn import AuthContext as _Ctx
            human = connection.execute(
                select(models.principals.c.id, models.principals.c.display_name)
                .where(models.principals.c.id == row["human_principal_id"],
                       models.principals.c.type == "human")).mappings().one_or_none()
            require(human is not None and dashboard is not None, 401, "invalid_capability",
                    "View capability is invalid")
            pseudo = _Ctx(principal_id=human["id"], principal_type="human",
                          display_name=human["display_name"], scopes=("read", "write", "manage"),
                          valid_until=from_db(row["expires_at"]), verified_at=moment,
                          auth_method="view_capability")
            access = self.authorizer.effective_access(connection, dashboard, pseudo)
            require(access.role is not None, 401, "invalid_capability",
                    "Access was revoked")
            require(dashboard["status"] == "published", 401, "invalid_capability",
                    "Dashboard is archived")
            revoked = self.revocation.is_revoked(
                None, row["identity_session_ref"], from_db(row["created_at"]))
            require(revoked is not True, 401, "invalid_capability", "Session was revoked")
            version = connection.execute(
                select(models.dashboard_versions).where(
                    models.dashboard_versions.c.id == row["version_id"],
                    models.dashboard_versions.c.dashboard_id == dashboard["id"])
            ).mappings().one_or_none()
            require(version is not None, 401, "invalid_capability",
                    "View capability is invalid")
            return version


def _latest_expiry(sources) -> object | None:
    from datetime import datetime, timezone
    best: datetime | None = None
    for source in sources:
        text = source.get("expires_at")
        if not text:
            return None  # an unbounded source never forces an earlier cap
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
        if best is None or parsed > best:
            best = parsed
    return best

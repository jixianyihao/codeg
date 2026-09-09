"""Per-dashboard authorization shared by every resource entry point.

Role model (design.md §5): viewer < editor < owner. Owner is stored on the
dashboard row and is never a deletable ACL rule. Multiple effective grants
combine to the highest role; losing one source does not necessarily lose
access. History shares the current-version ACL. Archived dashboards are
owner-visible only.
"""
from dataclasses import dataclass

from sqlalchemy import Select, and_, exists, or_, select

from . import models
from .authn import AuthContext
from .database import Database, from_db
from .errors import ApiError, now, require

ROLE_RANK = {"viewer": 1, "editor": 2, "owner": 3}
ROLE_ACTIONS = {
    "viewer": ("read",),
    "editor": ("read", "write"),
    "owner": ("read", "write", "manage"),
}


@dataclass(frozen=True)
class AccessOutcome:
    role: str | None
    sources: tuple[dict, ...]
    # When the caller's current highest role is expected to downgrade or
    # lapse (None = unbounded). Not a dashboard-wide expiry.
    expires_at: str | None

    @property
    def rank(self) -> int:
        return ROLE_RANK.get(self.role or "", 0)


def _source(row) -> dict:
    return {
        "subject_type": row["subject_type"],
        "subject_id": row["subject_id"],
        "role": row["role"],
        "starts_at": _iso(from_db(row["starts_at"])),
        "expires_at": _iso(from_db(row["expires_at"])),
    }


def _iso(value) -> str | None:
    from .errors import to_rfc3339
    return to_rfc3339(value)


def _combine(role_rows: list) -> AccessOutcome:
    if not role_rows:
        return AccessOutcome(None, (), None)
    top = max(ROLE_RANK[r["role"]] for r in role_rows)
    role = next(r for r, rank in ROLE_RANK.items() if rank == top)
    top_sources = [r for r in role_rows if ROLE_RANK[r["role"]] == top]
    if any(r.expires_at is None for r in top_sources):
        expires = None
    else:
        expires = from_db(min(r.expires_at for r in top_sources))
    expires_text = _iso(expires)
    return AccessOutcome(role, tuple(_source(r) for r in role_rows), expires_text)


class Authorizer:
    def __init__(self, database: Database):
        self.database = database

    # ------------------------------------------------------------------ reads

    def dashboard(self, connection, dashboard_id: str):
        row = connection.execute(
            select(models.dashboards).where(models.dashboards.c.id == dashboard_id)
        ).mappings().one_or_none()
        require(row is not None, 404, "not_found", "Dashboard is not visible")
        return row

    def dashboard_or_none(self, connection, dashboard_id: str):
        return connection.execute(
            select(models.dashboards).where(models.dashboards.c.id == dashboard_id)
        ).mappings().one_or_none()

    def dashboard_for_update(self, connection, dashboard_id: str):
        """Write-path read: locks the dashboard row so two concurrent writers
        serialize here — the second one re-reads the post-commit revision and
        fails its expected_revision check instead of overwriting (R5)."""
        return connection.execute(
            select(models.dashboards).where(models.dashboards.c.id == dashboard_id)
            .with_for_update()).mappings().one_or_none()

    def group_ids_of(self, connection, principal_id: str) -> set[str]:
        return {
            row[0] for row in connection.execute(
                select(models.group_members.c.group_id)
                .where(models.group_members.c.human_principal_id == principal_id))
        }

    def effective_access(self, connection, dashboard_row, actor: AuthContext,
                         *, group_ids: set[str] | None = None) -> AccessOutcome:
        moment = now()
        if dashboard_row["owner_principal_id"] == actor.principal_id:
            return AccessOutcome("owner", ({"subject_type": "owner", "subject_id": "*",
                                            "role": "owner", "starts_at": None,
                                            "expires_at": None},), None)
        if dashboard_row["status"] == "archived":
            return AccessOutcome(None, (), None)
        if group_ids is None:
            group_ids = (self.group_ids_of(connection, actor.principal_id)
                         if actor.principal_type == "human" else set())
        grants = connection.execute(
            select(models.dashboard_grants)
            .where(models.dashboard_grants.c.dashboard_id == dashboard_row["id"])
            .order_by(models.dashboard_grants.c.subject_type,
                      models.dashboard_grants.c.subject_id)).mappings().all()
        matched = []
        for grant in grants:
            subject_type = grant["subject_type"]
            applies = (
                (subject_type == "user" and actor.principal_type == "human"
                 and grant["subject_id"] == actor.principal_id)
                or (subject_type == "service" and actor.principal_type == "service"
                    and grant["subject_id"] == actor.principal_id)
                or (subject_type == "group" and actor.principal_type == "human"
                    and grant["subject_id"] in group_ids)
                or (subject_type == "all_authenticated" and actor.principal_type == "human"))
            if not applies:
                continue
            starts = from_db(grant["starts_at"])
            expires = from_db(grant["expires_at"])
            if (starts is None or starts <= moment) and (expires is None or moment < expires):
                matched.append(grant)
        return _combine([g for g in matched])

    def check_context_validity(self, connection, actor: AuthContext, *, moment=None) -> None:
        """Fresh in-transaction revalidation: token expiry plus live account
        state. W3 verification itself stays outside transactions (design §3)."""
        moment = moment or now()
        require(actor.valid_until > moment, 401, "token_expired", "The credential has expired")
        if actor.principal_type == "service":
            account = connection.execute(
                select(models.service_accounts.c.enabled,
                       models.service_accounts.c.token_version,
                       models.service_accounts.c.scopes)
                .where(models.service_accounts.c.principal_id == actor.principal_id)
            ).mappings().one_or_none()
            require(account is not None, 401, "token_revoked", "The service account no longer exists")
            require(account["enabled"], 401, "token_revoked", "The service account is disabled")
            require(account["token_version"] == actor.token_version, 401, "token_revoked",
                    "The service token version was reset")
            current_scopes = tuple(account["scopes"])
            if current_scopes != actor.scopes:
                # Scope reductions apply immediately to in-flight work.
                object.__setattr__(actor, "scopes", current_scopes)

    def authorize(self, connection, dashboard_row, actor: AuthContext, action: str = "read",
                  *, moment=None) -> AccessOutcome:
        """Shared gate: live validity (incl. current account scopes) + ACL.
        404 when no visibility, 403 action_forbidden when visible but
        insufficient."""
        require(action in ("read", "write", "manage"), 500, "internal_error", "Invalid action")
        if actor.principal_type not in ("human", "service"):
            raise ApiError(401, "authentication_required", "Unknown principal type")
        # Refresh live account state first: a scope reduction that landed
        # during the upload must apply before the scope assertion below.
        self.check_context_validity(connection, actor, moment=moment)
        if actor.principal_type == "service":
            actor.requires_scope("read" if action == "read" else
                                 "write" if action == "write" else "manage")
        access = self.effective_access(connection, dashboard_row, actor)
        require(access.role is not None, 404, "not_found", "Dashboard is not visible")
        require(action in ROLE_ACTIONS[access.role], 403, "action_forbidden",
                "This action requires a higher dashboard role")
        return access

    # ----------------------------------------------------------- list queries

    def visible_dashboard_query(self, actor: AuthContext, *, scope: str, status: str,
                                search: str) -> Select:
        """Listing filter. Published metadata is PUBLIC to authenticated
        humans (product decision 2026-09-09): the list shows every published
        dashboard and per-board authorization is enforced when the caller
        opens the detail, the manage page or a view capability — never by
        the listing itself. Service accounts still list only boards they
        are granted on. Archived listing remains the owner's management
        view: no shared/all archived listing. ACL first, then paging; never
        fetch-then-filter in the application."""
        moment = now()
        dash = models.dashboards
        base = select(dash.c.id).where(dash.c.status == status)
        mine = dash.c.owner_principal_id == actor.principal_id
        if status == "archived":
            base = base.where(mine)
        elif scope == "mine":
            base = base.where(mine)
        elif scope == "shared":
            base = base.where(~mine)
        if status == "archived" or scope == "mine":
            pass  # owner-only already applied above
        elif actor.principal_type == "service":
            time_ok = ((models.dashboard_grants.c.starts_at.is_(None)
                        | (models.dashboard_grants.c.starts_at <= moment))
                       & (models.dashboard_grants.c.expires_at.is_(None)
                          | (models.dashboard_grants.c.expires_at > moment)))
            grant_exists = exists(
                select(1).select_from(models.dashboard_grants).where(
                    models.dashboard_grants.c.dashboard_id == dash.c.id, time_ok,
                    self._grant_subject_match(actor)))
            visible = grant_exists if scope == "shared" else or_(mine, grant_exists)
            base = base.where(visible)
        # Human principals: published metadata is public — no grant filter.
        if search:
            base = base.where((dash.c.title.like(f"%{search}%"))
                              | (dash.c.description.like(f"%{search}%")))
        return base

    def _grant_subject_match(self, actor: AuthContext):
        grant = models.dashboard_grants
        if actor.principal_type == "human":
            group_source = exists(
                select(1).select_from(models.group_members).where(
                    models.group_members.c.human_principal_id == actor.principal_id,
                    models.group_members.c.group_id == grant.c.subject_id))
            return or_(
                (grant.c.subject_type == "user") & (grant.c.subject_id == actor.principal_id),
                (grant.c.subject_type == "group") & group_source,
                (grant.c.subject_type == "all_authenticated"),
            )
        return (grant.c.subject_type == "service") & (grant.c.subject_id == actor.principal_id)

    def visible_ids(self, connection, actor: AuthContext, *, scope: str, status: str,
                    search: str, cursor, limit: int) -> tuple[list, str | None]:
        """updated_at DESC, id DESC with a signed opaque cursor."""
        moment = now()
        self.check_context_validity(connection, actor, moment=moment)
        dash = models.dashboards
        query = self.visible_dashboard_query(actor, scope=scope, status=status, search=search)
        query = query.order_by(dash.c.updated_at.desc(), dash.c.id.desc())
        if cursor is not None:
            anchor = decode_cursor(cursor, actor, scope=scope, status=status, search=search)
            query = query.where((dash.c.updated_at < anchor["updated_at"])
                                | ((dash.c.updated_at == anchor["updated_at"])
                                   & (dash.c.id < anchor["id"])))
        query = query.limit(limit + 1)
        ids = [row[0] for row in connection.execute(query)]
        has_more = len(ids) > limit
        ids = ids[:limit]
        rows = connection.execute(
            select(dash).where(dash.c.id.in_(ids))
        ).mappings().all() if ids else []
        by_id = {r["id"]: r for r in rows}
        ordered = [by_id[i] for i in ids if i in by_id]
        next_cursor = None
        if has_more and ordered:
            last = ordered[-1]
            next_cursor = encode_cursor(
                actor, scope=scope, status=status, search=search,
                updated_at=last["updated_at"], dashboard_id=last["id"])
        return ordered, next_cursor


# ------------------------------------------------------------------- cursors

import base64
import hashlib
import hmac
import json


def encode_cursor(actor: AuthContext, **fields) -> str:
    payload = {
        "principal_id": actor.principal_id,
        "scope": fields["scope"], "status": fields["status"], "search": fields["search"],
        "updated_at": fields["updated_at"].isoformat(), "id": fields["dashboard_id"],
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    signature = hmac.new(_cursor_key(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return base64.urlsafe_b64encode(f"{signature}:{body}".encode()).decode()


def decode_cursor(cursor: str, actor: AuthContext, **filters) -> dict:
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
        signature, body = decoded.split(":", 1)
        expected = hmac.new(_cursor_key(), body.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        raise ApiError(422, "invalid_input", "Malformed pagination cursor") from None
    require(payload.get("principal_id") == actor.principal_id
            and payload.get("scope") == filters["scope"]
            and payload.get("status") == filters["status"]
            and payload.get("search") == filters["search"],
            422, "invalid_input", "Cursor does not match the current query")
    from datetime import datetime
    payload["updated_at"] = datetime.fromisoformat(payload["updated_at"])
    return payload


_CURSOR_KEY_CACHE: bytes | None = None


def _cursor_key() -> bytes:
    global _CURSOR_KEY_CACHE
    if _CURSOR_KEY_CACHE is None:
        import os
        # Dev/test fallback: derive from a process secret. Production pins
        # DASHBOARD_CURSOR_KEY (base64) so pages survive restarts.
        pinned = os.environ.get("DASHBOARD_CURSOR_KEY")
        if pinned:
            import base64 as _b64
            _CURSOR_KEY_CACHE = _b64.b64decode(pinned)
        else:
            _CURSOR_KEY_CACHE = os.urandom(32)
    return _CURSOR_KEY_CACHE


def set_cursor_key(key: bytes) -> None:
    """Test hook: pin the cursor key."""
    global _CURSOR_KEY_CACHE
    _CURSOR_KEY_CACHE = key

"""Authentication: one fixed branch per request, never a fallback.

`X-Dashboard-Auth-Mode: human|service` selects the verifier up front
(default human; the integration CLI always sends service). If the selected
branch fails, the request fails — we never retry with the other identity.
"""
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import jwt
from sqlalchemy import select

from . import models
from .config import Config
from .database import Database, to_db
from .errors import ApiError, new_id, now, parse_rfc3339, require

HUMAN_SCOPES = ("read", "write", "manage")
SERVICE_SCOPES = ("read", "write", "manage")


@dataclass(frozen=True)
class AuthContext:
    """Verified caller identity. No credential material is stored here."""
    principal_id: str
    principal_type: str  # human | service
    display_name: str
    scopes: tuple[str, ...]
    valid_until: datetime
    verified_at: datetime
    auth_method: str  # w3 | jwt
    is_admin: bool = False
    token_version: int | None = None
    identity_session_ref: str | None = None
    issuer: str | None = None
    enterprise_user_id: str | None = None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def requires_scope(self, scope: str) -> None:
        require(self.has_scope(scope), 403, "action_forbidden",
                f"This action requires the '{scope}' capability")


@dataclass(frozen=True)
class W3Identity:
    issuer: str
    enterprise_user_id: str
    display_name: str
    expires_at: datetime
    session_ref: str | None = None


class W3IdentityVerifier(Protocol):
    """Adapter boundary to the intranet W3/Uniportal OAuth2 validation.

    Implementations verify the presented token with the enterprise identity
    provider and return the normalized identity above. Field names of the
    native protocol are the adapter's concern, not the service's.
    """

    def verify(self, token: str) -> W3Identity: ...


class UnconfiguredW3Verifier:
    """No W3 configuration: the service still starts (machine-to-machine
    flows must not depend on W3 availability); every human verification
    fails closed with an explicit configuration error."""

    def verify(self, token: str) -> W3Identity:
        raise_for_setup()


def raise_for_setup() -> None:
    raise ApiError(503, "identity_unavailable",
                   "Human identity verification is not configured on this deployment")


class HttpW3Verifier:
    """Configurable introspection endpoint (default adapter).

    Expected normalized JSON response:
      {"active": true, "issuer": "...", "user_id": "...", "display_name": "...",
       "expires_at": "RFC3339", "session_ref": "optional"}
    Replace this adapter when the real intranet contract is available; the
    rest of the service depends only on `W3Identity`.
    """

    def __init__(self, url: str):
        import httpx
        self.url = url
        self.client = httpx.Client(timeout=8, follow_redirects=False, trust_env=False)

    def close(self) -> None:
        self.client.close()

    def verify(self, token: str) -> W3Identity:
        import httpx
        try:
            response = self.client.post(
                self.url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        except httpx.HTTPError:
            raise ApiError(503, "identity_unavailable", "The identity provider is unavailable") from None
        if response.status_code in (401, 403):
            raise ApiError(401, "invalid_token", "The presented human credential was rejected")
        if response.status_code != 200:
            raise ApiError(503, "identity_unavailable", "The identity provider is unavailable")
        if len(response.content) > 65536:
            raise ApiError(503, "identity_unavailable", "Malformed identity provider response")
        try:
            data = response.json()
        except ValueError:
            raise ApiError(503, "identity_unavailable", "Malformed identity provider response") from None
        return normalize_w3_payload(data)

    @staticmethod
    def from_config(config: Config) -> "HttpW3Verifier | UnconfiguredW3Verifier":
        return HttpW3Verifier(config.w3_verify_url) if config.w3_verify_url else UnconfiguredW3Verifier()


def normalize_w3_payload(data: dict) -> W3Identity:
    require(isinstance(data, dict), 503, "identity_unavailable", "Malformed identity response")
    require(data.get("active") is True, 401, "invalid_token", "The credential is not active")
    issuer = data.get("issuer")
    user_id = data.get("user_id")
    name = data.get("display_name")
    require(isinstance(issuer, str) and 0 < len(issuer) <= 191, 503, "identity_unavailable",
            "Malformed identity response")
    require(isinstance(user_id, str) and 0 < len(user_id) <= 191, 503, "identity_unavailable",
            "Malformed identity response")
    require(isinstance(name, str) and 0 < len(name) <= 200, 503, "identity_unavailable",
            "Malformed identity response")
    expires_at = parse_rfc3339(data.get("expires_at"), field="expires_at")
    session_ref = data.get("session_ref")
    if session_ref is not None:
        require(isinstance(session_ref, str) and 0 < len(session_ref) <= 191, 503,
                "identity_unavailable", "Malformed identity response")
    require(expires_at is not None and expires_at > now(), 401, "token_expired",
            "The human credential has expired")
    return W3Identity(issuer=issuer, enterprise_user_id=user_id, display_name=name,
                      expires_at=expires_at, session_ref=session_ref)


def decode_service_jwt(token: str, config: Config) -> dict:
    try:
        claims = jwt.decode(
            token, config.jwt_secret, algorithms=["HS256"], issuer=config.issuer,
            audience=config.audience,
            options={"require": ["iss", "aud", "sub", "token_type", "iat", "exp", "ver"],
                     "strict_aud": True},
            leeway=0)
    except jwt.ExpiredSignatureError:
        raise ApiError(401, "token_expired", "The service token has expired") from None
    except jwt.InvalidAudienceError:
        raise ApiError(401, "invalid_token", "The service token audience is invalid") from None
    except jwt.PyJWTError:
        raise ApiError(401, "invalid_token", "The service token is invalid") from None
    require(claims.get("token_type") == "service", 401, "invalid_token",
            "The token type is invalid for this auth mode")
    require(type(claims.get("ver")) is int and type(claims.get("iat")) is int
            and type(claims.get("exp")) is int, 401, "invalid_token",
            "The service token claims are invalid")
    require(claims["exp"] > claims["iat"] and claims["iat"] <= time.time() + 5, 401,
            "invalid_token", "The service token lifetime is invalid")
    return claims


def issue_service_jwt(config: Config, *, principal_id: str, token_version: int,
                      ttl_seconds: int) -> str:
    issued = int(time.time())
    return jwt.encode(
        {"iss": config.issuer, "aud": config.audience, "sub": principal_id,
         "token_type": "service", "iat": issued, "exp": issued + ttl_seconds,
         "ver": token_version},
        config.jwt_secret, algorithm="HS256")


class Authenticator:
    def __init__(self, config: Config, database: Database, verifier: W3IdentityVerifier):
        self.config = config
        self.database = database
        self.verifier = verifier

    def close(self) -> None:
        closer = getattr(self.verifier, "close", None)
        if callable(closer):
            closer()

    @staticmethod
    def auth_mode(headers) -> str:
        mode = headers.get("x-dashboard-auth-mode") or "human"
        require(mode in ("human", "service"), 400, "invalid_input",
                "X-Dashboard-Auth-Mode must be 'human' or 'service'")
        return mode

    @staticmethod
    def bearer(headers) -> str:
        header = headers.get("authorization")
        require(isinstance(header, str) and header.startswith("Bearer ") and 0 < len(header) <= 16384,
                401, "authentication_required", "A bearer credential is required")
        token = header[7:].strip()
        require(bool(token), 401, "authentication_required", "A bearer credential is required")
        return token

    def authenticate(self, headers) -> AuthContext:
        mode = self.auth_mode(headers)
        token = self.bearer(headers)
        return self.authenticate_service(token) if mode == "service" else self.authenticate_human(token)

    def authenticate_service(self, token: str) -> AuthContext:
        claims = decode_service_jwt(token, self.config)
        with self.database.read_only() as connection:
            account = self._service_account(claims["sub"], connection)
        require(account is not None and account["enabled"], 401, "token_revoked",
                "The service account is disabled")
        require(account["token_version"] == claims["ver"], 401, "token_revoked",
                "The service token version was reset")
        return AuthContext(
            principal_id=account["principal_id"], principal_type="service",
            display_name=account["name"], scopes=tuple(account["scopes"]),
            valid_until=datetime.fromtimestamp(claims["exp"], tz=timezone.utc),
            verified_at=now(),
            auth_method="jwt", token_version=claims["ver"])

    def authenticate_human(self, token: str) -> AuthContext:
        identity = self.verifier.verify(token)
        principal_id = self._link_human_principal(identity)
        return AuthContext(
            principal_id=principal_id, principal_type="human",
            display_name=identity.display_name, scopes=HUMAN_SCOPES,
            valid_until=identity.expires_at, verified_at=now(), auth_method="w3",
            is_admin=identity.enterprise_user_id in self.config.admin_user_ids,
            identity_session_ref=identity.session_ref, issuer=identity.issuer,
            enterprise_user_id=identity.enterprise_user_id)

    def _link_human_principal(self, identity: W3Identity) -> str:
        """Map (issuer, enterprise_user_id) to one stable principal; no
        name/email/sub guessing. Runs on its own short transaction."""
        with self.database.transaction() as connection:
            existing = connection.execute(
                select(models.identity_links.c.principal_id, models.principals.c.display_name)
                .join(models.principals,
                      models.principals.c.id == models.identity_links.c.principal_id)
                .where(models.identity_links.c.issuer == identity.issuer,
                       models.identity_links.c.enterprise_user_id == identity.enterprise_user_id)
            ).mappings().one_or_none()
            if existing is not None:
                # Refresh the display name only; the stable ID never changes.
                if existing["display_name"] != identity.display_name:
                    connection.execute(
                        models.principals.update().where(
                            models.principals.c.id == existing["principal_id"])
                        .values(display_name=identity.display_name))
                return existing["principal_id"]
            principal_id = new_id()
            connection.execute(
                models.principals.insert().values(
                    id=principal_id, type="human", display_name=identity.display_name,
                    created_at=to_db(now())))
            connection.execute(
                models.identity_links.insert().values(
                    issuer=identity.issuer, enterprise_user_id=identity.enterprise_user_id,
                    principal_id=principal_id, created_at=to_db(now())))
            return principal_id

    def _service_account(self, principal_id: str, connection):
        return connection.execute(
            select(models.service_accounts.c.principal_id, models.service_accounts.c.name,
                   models.service_accounts.c.enabled, models.service_accounts.c.token_version,
                   models.service_accounts.c.scopes, models.principals.c.display_name)
            .join(models.principals,
                  models.principals.c.id == models.service_accounts.c.principal_id)
            .where(models.service_accounts.c.principal_id == principal_id)
        ).mappings().one_or_none()

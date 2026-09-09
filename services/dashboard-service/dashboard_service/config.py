"""Explicit deployment configuration; secrets load only from files."""
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@dataclass
class Config:
    database_url: str
    jwt_secret: bytes = field(repr=False)
    storage_dir: Path = Path("data/content")
    control_origin: str = "http://127.0.0.1:8080"
    content_origin: str = "http://127.0.0.1:8081"
    issuer: str = "aresclaw-dashboard"
    audience: str = "aresclaw-dashboard-api"
    w3_verify_url: str | None = None
    admin_user_ids: tuple[str, ...] = ()
    max_upload_bytes: int = MAX_UPLOAD_BYTES
    max_owner_dashboards: int = 100
    max_owner_bytes: int = 500 * 1024 * 1024
    max_total_bytes: int = 10 * 1024 * 1024 * 1024
    max_versions_per_dashboard: int = 50
    max_concurrent_uploads: int = 8
    max_principal_uploads: int = 2
    writes_per_minute: int = 30
    jwt_max_ttl_seconds: int = 90 * 86400
    jwt_default_ttl_seconds: int = 30 * 86400
    operation_result_days: int = 7
    operation_lease_seconds: int = 300
    max_group_members: int = 1000
    page_size_default: int = 20
    page_size_max: int = 50
    recovery_mode: bool = False

    def validate(self) -> "Config":
        if not self.database_url.startswith("mysql+pymysql://"):
            raise ValueError("DASHBOARD_DATABASE_URL must use mysql+pymysql; MySQL is required")
        if len(self.jwt_secret) < 32:
            raise ValueError("JWT key file must contain at least 32 random bytes")
        for name in ("control_origin", "content_origin"):
            url = urlsplit(getattr(self, name))
            if url.scheme not in ("https", "http") or not url.hostname or url.username or url.password \
                    or url.query or url.fragment or url.path not in ("", "/"):
                raise ValueError(f"{name} must be an absolute HTTP(S) origin")
            if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError(f"{name} requires HTTPS off loopback")
        self.control_origin = self.control_origin.rstrip("/")
        self.content_origin = self.content_origin.rstrip("/")
        if self.content_origin == self.control_origin:
            raise ValueError("Content origin must differ from the control origin")
        if self.w3_verify_url:
            url = urlsplit(self.w3_verify_url)
            if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("W3 verifier must be a fixed HTTPS endpoint without credentials")
        if not 0 < self.max_upload_bytes <= MAX_UPLOAD_BYTES:
            raise ValueError("Upload limit must be between 1 byte and 10 MiB")
        return self

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            database_url=os.environ["DASHBOARD_DATABASE_URL"],
            jwt_secret=Path(os.environ["DASHBOARD_JWT_KEY_FILE"]).read_bytes().strip(),
            storage_dir=Path(os.getenv("DASHBOARD_STORAGE_DIR", "data/content")),
            control_origin=os.getenv("DASHBOARD_CONTROL_ORIGIN", "http://127.0.0.1:8080"),
            content_origin=os.getenv("DASHBOARD_CONTENT_ORIGIN", "http://127.0.0.1:8081"),
            issuer=os.getenv("DASHBOARD_JWT_ISSUER", "aresclaw-dashboard"),
            audience=os.getenv("DASHBOARD_JWT_AUDIENCE", "aresclaw-dashboard-api"),
            w3_verify_url=os.getenv("DASHBOARD_W3_VERIFY_URL"),
            admin_user_ids=tuple(filter(None, os.getenv("DASHBOARD_ADMIN_USER_IDS", "").split(","))),
            max_total_bytes=int(os.getenv("DASHBOARD_MAX_TOTAL_BYTES", str(10 * 1024**3))),
            recovery_mode=os.getenv("DASHBOARD_RECOVERY_MODE") == "1",
        ).validate()

    @classmethod
    def for_testing(cls, database_url: str, **overrides):
        base = dict(
            database_url=database_url,
            jwt_secret=b"test-only-key-never-deploy-" * 2,
            control_origin="http://127.0.0.1:18080",
            content_origin="http://127.0.0.1:18081",
        )
        base.update(overrides)
        return cls(**base).validate()

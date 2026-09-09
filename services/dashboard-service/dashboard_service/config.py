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
    # Published HTML lives in a private S3 bucket; the local storage_dir is
    # only a bounded staging buffer for request streams (contracts.md §9).
    s3_endpoint_url: str | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str = ""
    s3_prefix: str = ""
    s3_addressing_style: str = "path"
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
    # DEV-ONLY page login adapter (DASHBOARD_DEV_LOGIN=1). Refused unless
    # the control origin is loopback and a W3 verifier is configured.
    dev_login: bool = False

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
            loopback = url.hostname in ("localhost", "127.0.0.1", "::1")
            scheme_ok = url.scheme == "https" or (url.scheme == "http" and loopback)
            if not scheme_ok or not url.hostname or url.username or url.password                     or url.query or url.fragment:
                raise ValueError("W3 verifier must be a fixed HTTPS endpoint "
                                 "without credentials (loopback HTTP for dev)")
        if not 0 < self.max_upload_bytes <= MAX_UPLOAD_BYTES:
            raise ValueError("Upload limit must be between 1 byte and 10 MiB")
        self._validate_s3()
        if self.dev_login:
            if not self.w3_verify_url:
                raise ValueError("DASHBOARD_DEV_LOGIN requires DASHBOARD_W3_VERIFY_URL")
            control_host = urlsplit(self.control_origin).hostname or ""
            if control_host not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("DASHBOARD_DEV_LOGIN is only allowed on loopback origins")
        return self

    def _validate_s3(self) -> None:
        if not self.s3_bucket:
            raise ValueError("DASHBOARD_S3_BUCKET is required: published HTML must live in S3")
        if not (3 <= len(self.s3_bucket) <= 63) or not all(
                c.islower() or c.isdigit() or c in "-." for c in self.s3_bucket):
            raise ValueError("DASHBOARD_S3_BUCKET must be a valid bucket name")
        if not self.s3_prefix:
            self.s3_prefix = "aresclaw/"
        if len(self.s3_prefix) > 128 or not self.s3_prefix.isascii() \
                or not self.s3_prefix.endswith("/"):
            raise ValueError("DASHBOARD_S3_PREFIX must be a short ASCII prefix ending in /")
        if self.s3_addressing_style not in ("auto", "path", "virtual"):
            raise ValueError("DASHBOARD_S3_ADDRESSING_STYLE must be auto, path or virtual")
        if self.s3_endpoint_url:
            url = urlsplit(self.s3_endpoint_url)
            if url.scheme not in ("https", "http") or not url.hostname or url.query or url.fragment:
                raise ValueError("DASHBOARD_S3_ENDPOINT_URL must be a fixed endpoint URL")
            if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("plain-HTTP S3 endpoints are only allowed on loopback (tests)")

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            database_url=os.environ["DASHBOARD_DATABASE_URL"],
            jwt_secret=Path(os.environ["DASHBOARD_JWT_KEY_FILE"]).read_bytes().strip(),
            storage_dir=Path(os.getenv("DASHBOARD_STORAGE_DIR", "data/staging")),
            control_origin=os.getenv("DASHBOARD_CONTROL_ORIGIN", "http://127.0.0.1:8080"),
            content_origin=os.getenv("DASHBOARD_CONTENT_ORIGIN", "http://127.0.0.1:8081"),
            issuer=os.getenv("DASHBOARD_JWT_ISSUER", "aresclaw-dashboard"),
            audience=os.getenv("DASHBOARD_JWT_AUDIENCE", "aresclaw-dashboard-api"),
            w3_verify_url=os.getenv("DASHBOARD_W3_VERIFY_URL"),
            admin_user_ids=tuple(filter(None, os.getenv("DASHBOARD_ADMIN_USER_IDS", "").split(","))),
            s3_endpoint_url=os.getenv("DASHBOARD_S3_ENDPOINT_URL") or None,
            s3_region=os.getenv("DASHBOARD_S3_REGION", "us-east-1"),
            s3_bucket=os.getenv("DASHBOARD_S3_BUCKET", ""),
            s3_prefix=os.getenv("DASHBOARD_S3_PREFIX", ""),
            s3_addressing_style=os.getenv("DASHBOARD_S3_ADDRESSING_STYLE", "path"),
            max_total_bytes=int(os.getenv("DASHBOARD_MAX_TOTAL_BYTES", str(10 * 1024**3))),
            recovery_mode=os.getenv("DASHBOARD_RECOVERY_MODE") == "1",
            dev_login=os.getenv("DASHBOARD_DEV_LOGIN") == "1",
        ).validate()

    @classmethod
    def for_testing(cls, database_url: str, **overrides):
        base = dict(
            database_url=database_url,
            jwt_secret=b"test-only-key-never-deploy-" * 2,
            control_origin="http://127.0.0.1:18080",
            content_origin="http://127.0.0.1:18081",
            s3_endpoint_url=os.environ.get("TEST_S3_ENDPOINT_URL", "http://127.0.0.1:19000"),
            s3_region="us-east-1",
            s3_bucket=os.environ.get("TEST_S3_BUCKET", "aresclaw-dash-test"),
            s3_prefix=os.environ.get("TEST_S3_PREFIX", "test/"),
        )
        base.update(overrides)
        return cls(**base).validate()

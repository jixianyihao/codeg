"""Integration fixtures: a dedicated real MySQL instance only.

Tests are skipped unless TEST_DATABASE_URL points at a database created for
this purpose (never a default/production database). SQLite or mocks are not
substitutes for these transaction tests.
"""
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy
from fastapi.testclient import TestClient

from dashboard_service import models
from dashboard_service.app import create_control_app
from dashboard_service.authn import Authenticator, W3Identity
from dashboard_service.config import Config
from dashboard_service.content_app import create_content_app
from dashboard_service.database import Database, create_db_engine
from dashboard_service.errors import ApiError
from dashboard_service.operator import Operator
from dashboard_service.routers import build_service

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
TEST_S3_ENDPOINT = os.environ.get("TEST_S3_ENDPOINT_URL", "http://127.0.0.1:19000")
TEST_S3_BUCKET = os.environ.get("TEST_S3_BUCKET", "aresclaw-dash-test")

requires_mysql = pytest.mark.skipif(
    not TEST_DATABASE_URL.startswith("mysql+pymysql://"),
    reason="TEST_DATABASE_URL must point at a dedicated MySQL test database")

# The test S3 endpoint is a real S3-compatible server (MinIO) with fixed
# throwaway credentials — never a production endpoint.
os.environ.setdefault("AWS_ACCESS_KEY_ID", "minioadmin")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "minioadmin")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


class FakeW3:
    """Test double for the intranet W3 adapter. It speaks the normalized
    W3Identity contract only — it is not a claim about the real protocol."""

    def __init__(self):
        self.tokens: dict[str, W3Identity] = {}
        self.revoked: set[str] = set()
        self.calls = 0

    def register(self, uid: str, name: str | None = None,
                 token: str | None = None, **kwargs) -> str:
        token = token or f"w3-token-{uid}"
        self.tokens[token] = W3Identity(
            issuer="w3-test", enterprise_user_id=uid, display_name=name or f"User {uid}",
            expires_at=kwargs.get("expires_at", datetime.now(timezone.utc) + timedelta(hours=8)),
            session_ref=kwargs.get("session_ref"))
        return token

    def verify(self, token: str) -> W3Identity:
        self.calls += 1
        if token in self.revoked:
            raise ApiError(401, "invalid_token", "revoked")
        identity = self.tokens.get(token)
        if identity is None:
            raise ApiError(401, "invalid_token", "unknown test token")
        if identity.expires_at <= datetime.now(timezone.utc):
            raise ApiError(401, "token_expired", "expired")
        return identity


class Bundle:
    def __init__(self, config: Config, database: Database, verifier: FakeW3,
                 authenticator: Authenticator):
        self.config = config
        self.database = database
        self.verifier = verifier
        self.authenticator = authenticator
        self.service = build_service(config, database, authenticator)
        self.control = create_control_app(config, database=database, verifier=verifier)
        self.control.state.service = self.service
        self.content = create_content_app(self.service)


@pytest.fixture(scope="session")
def mysql_url():
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL not configured")
    return TEST_DATABASE_URL


@pytest.fixture(scope="session", autouse=True)
def s3_test_bucket(mysql_url):
    """Ensure the dedicated test bucket exists on the real S3 endpoint."""
    import boto3
    client = boto3.client("s3", endpoint_url=TEST_S3_ENDPOINT)
    try:
        client.head_bucket(Bucket=TEST_S3_BUCKET)
    except Exception:
        try:
            client.create_bucket(Bucket=TEST_S3_BUCKET)
        except Exception as error:
            pytest.skip(f"test S3 endpoint unavailable: {type(error).__name__}")


@pytest.fixture(scope="session")
def migrated_database(mysql_url):
    """One schema migration per session (downgrade → upgrade), real MySQL."""
    database = Database(create_db_engine(mysql_url))
    from alembic import command
    from alembic.config import Config as AlembicConfig
    alembic_cfg = AlembicConfig(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    os.environ["DASHBOARD_DATABASE_URL"] = mysql_url
    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")
    yield database
    database.engine.dispose()


DATA_TABLES = ["audit_events", "upload_reservations", "quota_usage", "view_capabilities",
               "operations", "dashboard_grants", "dashboard_versions", "dashboards",
               "group_members", "groups", "identity_links", "service_accounts",
               "principals"]


@pytest.fixture()
def bundle(migrated_database, tmp_path):
    """Per-test clean data + isolated content directory."""
    with migrated_database.transaction() as connection:
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=0")
        for name in DATA_TABLES:
            connection.execute(sqlalchemy.delete(getattr(models, name)))
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=1")
        connection.execute(models.authorization_guard.insert()
                           .prefix_with("IGNORE").values(id=1, revision=1))
    config = Config.for_testing(
        TEST_DATABASE_URL,
        storage_dir=tmp_path / "staging",
        control_origin="http://127.0.0.1:18080",
        content_origin="http://127.0.0.1:18081",
        s3_endpoint_url=TEST_S3_ENDPOINT,
        s3_bucket=TEST_S3_BUCKET,
        s3_prefix="test/",
    )
    verifier = FakeW3()
    authenticator = Authenticator(config, migrated_database, verifier)
    instance = Bundle(config, migrated_database, verifier, authenticator)
    yield instance
    instance.service.close()


@pytest.fixture()
def client(bundle) -> TestClient:
    return TestClient(bundle.control)


@pytest.fixture()
def content_client(bundle) -> TestClient:
    return TestClient(bundle.content)


@pytest.fixture()
def operator(bundle) -> Operator:
    return Operator(bundle.config, bundle.database)


def service_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Dashboard-Auth-Mode": "service"}


def human_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "X-Dashboard-Auth-Mode": "human"}


@pytest.fixture()
def make_service_account(operator):
    def _make(name: str, scopes=("read", "write")) -> tuple[str, str]:
        account = operator.create_account(name, list(scopes))
        issued = operator.issue(account["principal_id"])
        return account["principal_id"], issued["token"]

    return _make


@pytest.fixture()
def make_human(bundle):
    def _make(uid: str, name: str | None = None, **kwargs) -> str:
        return bundle.verifier.register(uid, name, **kwargs)

    return _make


def multipart(metadata: dict, html: bytes) -> dict:
    return {
        "data": {"metadata": json.dumps(metadata)},
        "files": {"html": ("dashboard.html", html, "text/html")},
    }


def publish_html(client, token: str, html: bytes = b"<html><body>ok</body></html>",
                 *, title: str = "Board", key: str | None = None,
                 dashboard_id: str | None = None,
                 expected_revision: int | None = None,
                 auth_mode: str = "service"):
    """Real multipart HTTP publish; returns the raw response object."""
    metadata = {"title": title, "description": "desc",
                "content_sha256": hashlib.sha256(html).hexdigest(),
                "byte_size": len(html)}
    if expected_revision is not None:
        metadata["expected_revision"] = expected_revision
    headers = {"Authorization": f"Bearer {token}",
               "X-Dashboard-Auth-Mode": auth_mode,
               "Idempotency-Key": key or str(uuid.uuid4())}
    path = ("/api/v1/dashboards" if dashboard_id is None
            else f"/api/v1/dashboards/{dashboard_id}/versions")
    return client.post(path, headers=headers, **multipart(metadata, html))


def committed_version_rows(bundle, dashboard_id: str) -> list:
    """Count committed versions in the real MySQL database — never inferred
    from API responses."""
    with bundle.database.read_only() as connection:
        return connection.execute(
            sqlalchemy.select(models.dashboard_versions.c.id,
                              models.dashboard_versions.c.number)
            .where(models.dashboard_versions.c.dashboard_id == dashboard_id)
            .order_by(models.dashboard_versions.c.number)).all()

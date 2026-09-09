"""T2/S3: object-storage contract tests against a real S3 endpoint
(MinIO in tests; AWS-style S3 in production)."""
import hashlib
import uuid

import pytest

from dashboard_service.storage import ContentStore, ObjectRef, build_object_key

from .conftest import requires_mysql, TEST_S3_BUCKET

pytestmark = requires_mysql


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_key_layout_is_fixed_and_unique_per_attempt():
    key_a = build_object_key("aresclaw/prod/", "d1", "v1", "a1")
    key_b = build_object_key("aresclaw/prod/", "d1", "v1", "a2")
    assert key_a == "aresclaw/prod/dashboards/d1/versions/v1/a1/index.html"
    assert key_a != key_b
    assert len(key_a) < 512


def test_conditional_create_never_overwrites(bundle):
    store: ContentStore = bundle.service.store
    key = build_object_key(bundle.config.s3_prefix, str(uuid.uuid4()), str(uuid.uuid4()),
                           str(uuid.uuid4()))
    data = b"<html>first</html>"
    ref = store.put_verified(key, data, _digest(data), len(data))
    assert ref.bucket == TEST_S3_BUCKET
    # A second write to the same key must be refused, not overwrite.
    other = b"<html>second</html>"
    with pytest.raises(Exception) as excinfo:
        store.put_verified(key, other, _digest(other), len(other))
    assert excinfo.value.status == 503
    assert store.get_verified(ref, _digest(data), len(data)) == data


def test_put_verification_rejects_digest_mismatch_and_cleans_up(bundle):
    store: ContentStore = bundle.service.store
    key = build_object_key(bundle.config.s3_prefix, str(uuid.uuid4()), str(uuid.uuid4()),
                           str(uuid.uuid4()))
    data = b"<html>x</html>"
    with pytest.raises(Exception) as excinfo:
        # Declared digest does not match the bytes: the stored object must
        # not be accepted and is removed again.
        store.put_verified(key, data, "0" * 64, len(data))
    assert excinfo.value.status == 503
    assert not store.object_exists(ObjectRef(bucket=TEST_S3_BUCKET, key=key))


def test_get_verified_detects_corruption(bundle):
    """Corrupt the object behind the store's back; reads must fail closed."""
    import boto3
    from .conftest import TEST_S3_ENDPOINT
    store: ContentStore = bundle.service.store
    key = build_object_key(bundle.config.s3_prefix, str(uuid.uuid4()), str(uuid.uuid4()),
                           str(uuid.uuid4()))
    data = b"<html>good</html>"
    ref = store.put_verified(key, data, _digest(data), len(data))
    client = boto3.client("s3", endpoint_url=TEST_S3_ENDPOINT)
    client.put_object(Bucket=TEST_S3_BUCKET, Key=key, Body=b"<html>tampered</html>")
    with pytest.raises(Exception) as excinfo:
        store.get_verified(ref, _digest(data), len(data))
    assert excinfo.value.status == 503


def test_private_bucket_rejects_anonymous_reads(bundle):
    """S3-02 precondition: the bucket serves nothing without credentials."""
    import boto3
    from botocore import UNSIGNED
    from botocore.config import Config as BotoConfig
    from .conftest import TEST_S3_ENDPOINT
    store: ContentStore = bundle.service.store
    key = build_object_key(bundle.config.s3_prefix, str(uuid.uuid4()), str(uuid.uuid4()),
                           str(uuid.uuid4()))
    data = b"<html>secret-ish</html>"
    ref = store.put_verified(key, data, _digest(data), len(data))
    anonymous = boto3.client("s3", endpoint_url=TEST_S3_ENDPOINT,
                             config=BotoConfig(signature_version=UNSIGNED))
    with pytest.raises(Exception):
        anonymous.get_object(Bucket=TEST_S3_BUCKET, Key=ref.key)


def test_unavailable_endpoint_is_sanitized(tmp_path):
    """Endpoint failures surface as a sanitized 503 — no endpoint URL, key,
    signature or credentials in the message."""
    from dashboard_service.config import Config
    config = Config.for_testing(
        "mysql+pymysql://unused:unused@127.0.0.1/unused",
        storage_dir=tmp_path,
        s3_endpoint_url="http://127.0.0.1:59999",  # nothing listens here
        s3_bucket="aresclaw-dash-test",
        s3_prefix="test/",
    )
    store = ContentStore(config)
    data = b"<html>offline</html>"
    with pytest.raises(Exception) as excinfo:
        store.put_verified("test/offline/index.html", data, _digest(data), len(data))
    error = excinfo.value
    assert error.status == 503
    message = error.message
    assert "59999" not in message and "127.0.0.1" not in message
    assert "aresclaw-dash-test" not in message


def test_publish_stores_object_and_content_reads_it_back(bundle, client,
                                                         make_service_account, make_human):
    """End-to-end: publish → MySQL reference + S3 object; the content origin
    and source endpoint serve exactly the committed bytes."""
    import sqlalchemy
    from dashboard_service import models
    from .conftest import human_headers, publish_html
    token = make_human("s3-e2e", "S3E2E")
    html = b"<html><body>via-s3</body></html>"
    created = publish_html(client, token, html, auth_mode="human").json()["result"]
    with bundle.database.read_only() as connection:
        version = connection.execute(
            sqlalchemy.select(models.dashboard_versions).where(
                models.dashboard_versions.c.id == created["version_id"])).mappings().one()
    assert version["storage_bucket"] == TEST_S3_BUCKET
    assert version["storage_key"].endswith("/index.html")
    assert bundle.service.store.read_version(version) == html
    source = client.get(
        f"/api/v1/dashboards/{created['dashboard_id']}/versions/{created['version_id']}/source",
        headers=human_headers(token))
    assert source.status_code == 200
    assert source.content == html

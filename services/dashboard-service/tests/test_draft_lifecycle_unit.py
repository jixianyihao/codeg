"""Pure validation and access-combination regressions, no external resources."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from dashboard_service.authorization import _combine
from dashboard_service.errors import ApiError
from dashboard_service.publishing import _validate_metadata


@pytest.fixture(scope="session", autouse=True)
def s3_test_bucket():
    yield  # These pure tests do not use MySQL or storage.


class Grant(dict):
    __getattr__ = dict.__getitem__


def test_highest_role_expires_only_after_last_effective_source():
    soon = datetime(2027, 1, 1, tzinfo=timezone.utc)
    later = datetime(2027, 2, 1, tzinfo=timezone.utc)
    grants = [Grant(role="editor", subject_type="user", subject_id="one",
                    starts_at=None, expires_at=soon),
              Grant(role="editor", subject_type="group", subject_id="two",
                    starts_at=None, expires_at=later)]
    assert _combine(grants).expires_at == "2027-02-01T00:00:00Z"


def test_omitted_update_description_stays_unspecified():
    values = _validate_metadata({"content_sha256": "a" * 64, "byte_size": 10,
                                 "expected_revision": 1}, creating=False,
                                config=SimpleNamespace(max_upload_bytes=100))
    assert values[1] is None


def test_disposition_is_validated_before_any_upload():
    with pytest.raises(ApiError) as caught:
        _validate_metadata({"title": "Board", "content_sha256": "a" * 64,
                            "byte_size": 10, "disposition": "publish_typo"},
                           creating=True, config=SimpleNamespace(max_upload_bytes=100))
    assert caught.value.code == "invalid_input"

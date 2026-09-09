"""Immutable HTML object storage on private S3 (contracts.md §9).

Local disk is only a bounded staging buffer for the request stream; the
durable copy of every published version is a uniquely-keyed S3 object.
Publish order: stream+validate to staging → conditional PUT → read-back
verification → only then may the MySQL transaction reference the object.
Orphaned objects (uploaded but never committed) are deleted by the
operator cleanup using the reservation records, never guessed from paths.

No presigned URLs, no browser-facing bucket access, no fallback to local
files when S3 fails.
"""
import codecs
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

import boto3
import botocore.config
import botocore.exceptions

from .config import Config
from .errors import ApiError

CHUNK = 1024 * 1024


@dataclass(frozen=True)
class ObjectRef:
    bucket: str
    key: str
    object_version_id: str | None = None


def build_object_key(prefix: str, dashboard_id: str, version_id: str,
                     attempt_id: str) -> str:
    """Fixed key layout: {prefix}dashboards/{id}/versions/{vid}/{attempt}/index.html"""
    key = f"{prefix}dashboards/{dashboard_id}/versions/{version_id}/{attempt_id}/index.html"
    if len(key) > 512 or not key.isascii():
        raise ApiError(503, "storage_unavailable", "object key is invalid")
    return key


def _storage_unavailable(message: str = "Object storage is unavailable") -> ApiError:
    # Sanitized: never leak endpoints, keys, signatures or bucket internals.
    return ApiError(503, "storage_unavailable", message, retryable=True)


class ContentStore:
    """Staging buffer + private S3 object access for one deployment."""

    def __init__(self, config: Config):
        self.config = config
        self.staging = config.storage_dir.resolve() / "staging"
        self.staging.mkdir(parents=True, exist_ok=True)
        self._client = boto3.client(
            "s3",
            endpoint_url=config.s3_endpoint_url,
            region_name=config.s3_region,
            config=botocore.config.Config(
                s3={"addressing_style": config.s3_addressing_style},
                retries={"max_attempts": 2, "mode": "standard"},
                connect_timeout=5,
                read_timeout=60,
            ),
        )

    # ------------------------------------------------------------- staging

    def staging_path(self, attempt_id: str) -> Path:
        return self.staging / f"{attempt_id}.part"

    def stage_stream(self, attempt_id: str, stream, expected_sha256: str, expected_size: int) -> Path:
        """Stream the upload to a bounded staging file, enforcing the real byte
        count (never trusting Content-Length) and UTF-8 validity."""
        limit = self.config.max_upload_bytes
        digest = hashlib.sha256()
        decoder = codecs.getincrementaldecoder("utf-8")()
        size = 0
        target = self.staging_path(attempt_id)

        def abort(code: str, message: str) -> None:
            output.close()
            target.unlink(missing_ok=True)
            raise ApiError(413 if code == "upload_too_large" else 422, code, message)

        with open(target, "xb") as output:
            while True:
                chunk = stream.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    abort("upload_too_large", "HTML exceeds the 10 MiB limit")
                try:
                    decoder.decode(chunk)
                except UnicodeDecodeError:
                    abort("invalid_html", "HTML must be non-empty UTF-8 text")
                digest.update(chunk)
                output.write(chunk)
            try:
                decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                abort("invalid_html", "HTML must be non-empty UTF-8 text")
            if not size:
                abort("invalid_html", "HTML must be non-empty UTF-8 text")
            if size != expected_size or digest.hexdigest() != expected_sha256:
                abort("hash_mismatch", "HTML bytes or SHA-256 do not match the declared metadata")
            output.flush()
        return target

    # ------------------------------------------------------- S3 operations

    def put_verified(self, key: str, data: bytes, expected_sha256: str,
                     expected_size: int) -> ObjectRef:
        """Conditional create + read-back verification. Returns the committed
        object reference only after the stored bytes match the digest."""
        bucket = self.config.s3_bucket
        try:
            self._client.put_object(
                Bucket=bucket, Key=key, Body=data,
                IfNoneMatch="*",  # unique per attempt key; never overwrite
                ContentType="text/html; charset=utf-8",
                Metadata={"sha256": expected_sha256},
            )
        except botocore.exceptions.ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code in ("PreconditionFailed", "ConditionalRequestConflict"):
                raise _storage_unavailable("Object already exists") from None
            raise _storage_unavailable() from None
        except botocore.exceptions.BotoCoreError:
            raise _storage_unavailable() from None
        # PUT success is not verification: read the object back and digest.
        stored = self._get_bytes(bucket, key, None)
        if len(stored) != expected_size or hashlib.sha256(stored).hexdigest() != expected_sha256:
            self._safe_delete(bucket, key, None)
            raise _storage_unavailable("Object verification failed")
        return ObjectRef(bucket=bucket, key=key)

    def get_verified(self, ref: ObjectRef, expected_sha256: str, expected_size: int) -> bytes:
        data = self._get_bytes(ref.bucket, ref.key, ref.object_version_id)
        if len(data) != expected_size or hashlib.sha256(data).hexdigest() != expected_sha256:
            raise _storage_unavailable("Object integrity check failed")
        return data

    def read_version(self, version_row) -> bytes:
        ref = ObjectRef(bucket=version_row["storage_bucket"], key=version_row["storage_key"],
                        object_version_id=version_row["object_version_id"])
        return self.get_verified(ref, version_row["sha256"], version_row["byte_size"])

    def delete_orphan(self, ref: ObjectRef) -> bool:
        """Precise delete of a never-committed attempt object."""
        return self._safe_delete(ref.bucket, ref.key, ref.object_version_id)

    def object_exists(self, ref: ObjectRef) -> bool:
        try:
            self._client.head_object(
                Bucket=ref.bucket, Key=ref.key,
                **({"VersionId": ref.object_version_id} if ref.object_version_id else {}))
            return True
        except botocore.exceptions.ClientError as error:
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404:
                return False
            raise _storage_unavailable() from None
        except botocore.exceptions.BotoCoreError:
            raise _storage_unavailable() from None

    def probe(self) -> dict:
        """Readiness check for /health/ready."""
        try:
            self._client.head_bucket(Bucket=self.config.s3_bucket)
            return {"s3": "ok", "bucket": self.config.s3_bucket}
        except botocore.exceptions.BotoCoreError:
            return {"s3": "degraded: object storage unreachable", "bucket": self.config.s3_bucket}

    def clear_staging(self, older_than_seconds: int = 3600) -> int:
        removed = 0
        for path in self.staging.glob("*.part"):
            try:
                if path.stat().st_mtime < time.time() - older_than_seconds:
                    path.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                continue
        return removed

    # ------------------------------------------------------------- helpers

    def _get_bytes(self, bucket: str, key: str, version_id: str | None) -> bytes:
        try:
            response = self._client.get_object(
                Bucket=bucket, Key=key,
                **({"VersionId": version_id} if version_id else {}))
            data = response["Body"].read(self.config.max_upload_bytes + 1)
        except botocore.exceptions.ClientError:
            raise _storage_unavailable("Object is unavailable") from None
        except botocore.exceptions.BotoCoreError:
            raise _storage_unavailable() from None
        if len(data) > self.config.max_upload_bytes:
            raise _storage_unavailable("Object exceeds the supported size")
        return data

    def _safe_delete(self, bucket: str, key: str, version_id: str | None) -> bool:
        try:
            self._client.delete_object(
                Bucket=bucket, Key=key,
                **({"VersionId": version_id} if version_id else {}))
            return True
        except botocore.exceptions.BotoCoreError:
            return False

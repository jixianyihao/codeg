"""Immutable HTML file storage.

Publish order (design.md §8): stream to a per-attempt staging file and
validate size/UTF-8/SHA-256 → durably write the unique immutable final path
→ only then may the final database transaction reference it. A file written
before a failed transaction is just a cleanable orphan.
"""
import hashlib
import os
import time
from pathlib import Path

from .config import Config
from .errors import ApiError

CHUNK = 1024 * 1024


class ContentStore:
    def __init__(self, config: Config):
        self.config = config
        self.root = config.storage_dir.resolve()
        self.staging = self.root / "staging"

    def prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging.mkdir(parents=True, exist_ok=True)

    def staging_path(self, attempt_id: str) -> Path:
        return self.staging / f"{attempt_id}.part"

    def stage_stream(self, attempt_id: str, stream, expected_sha256: str, expected_size: int) -> Path:
        """Stream the upload to a staging file, enforcing the real byte
        count (never trusting Content-Length) and UTF-8 validity."""
        import codecs
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
            os.fsync(output.fileno())
        return target

    def finalize(self, storage_key: str, staged: Path) -> Path:
        """Move the staged file to its immutable final path. If a previous
        attempt already placed an identical file, verify and reuse it."""
        final = self.root / f"{storage_key}.html"
        if final.exists():
            self.verify_file(final)
            staged.unlink(missing_ok=True)
            return final
        os.replace(staged, final)
        self._fsync_directory()
        return final

    def _fsync_directory(self) -> None:
        # Directory-handle fsync is a POSIX durability step; Windows refuses
        # it (PermissionError) and NTFS metadata writes are ordered anyway.
        if os.name == "nt":
            return
        directory = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def verify_file(self, path: Path) -> None:
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        expected = path.stem.replace(".html", "")
        if expected and digest != expected:
            raise ApiError(503, "storage_unavailable", "Content integrity check failed")

    def read_version(self, storage_key: str, sha256: str, byte_size: int) -> bytes:
        path = self.root / f"{storage_key}.html"
        try:
            data = path.read_bytes()
        except OSError:
            raise ApiError(503, "storage_unavailable",
                           "Content storage is unavailable") from None
        if len(data) != byte_size or hashlib.sha256(data).hexdigest() != sha256:
            raise ApiError(503, "storage_unavailable", "Content integrity check failed")
        return data

    def cleanup_orphans(self, referenced_keys: set[str], *, older_than_seconds: int = 86400) -> int:
        """Remove staging leftovers and unreferenced files older than the
        grace window. Only UUID-named .html files directly in the content
        root are ever considered."""
        from .errors import is_uuid
        removed = 0
        for path in self.staging.glob("*.part"):
            if path.stat().st_mtime < time.time() - 3600:
                path.unlink(missing_ok=True)
                removed += 1
        for path in self.root.glob("*.html"):
            if not is_uuid(path.stem):
                continue
            if path.stem in referenced_keys:
                continue
            if path.stat().st_mtime < time.time() - older_than_seconds:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

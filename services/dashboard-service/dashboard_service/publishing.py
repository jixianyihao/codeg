"""Publish orchestration (design.md §8 order):

1. TX: validate, occupy the operation key, reserve quota.
2. Filesystem: stream to a per-attempt staging file, validate, durably
   write the immutable final path.
3. TX: re-verify identity/ACL/revision, insert the version, switch
   current_version_id, convert quota, audit, commit the operation result.

Files written before a failed transaction are cleanable orphans; the old
current-version pointer only ever changes in the final committed step.
"""
from collections.abc import Callable

from sqlalchemy.exc import OperationalError

from . import models, quotas
from .authn import AuthContext
from .authorization import Authorizer
from .config import Config
from .database import Database, record_audit, to_db
from .errors import ApiError, new_id, now, require
from .operations import Operations
from .storage import ContentStore

CONTENT_LOCK_RETRIES = 3


class PublishOutcome:
    def __init__(self, wrapper: dict, status: int, retry_after: int | None = None):
        self.wrapper = wrapper
        self.status = status
        self.retry_after = retry_after


def _validate_metadata(metadata: dict, *, creating: bool, config: Config):
    title = metadata.get("title")
    if creating:
        require(isinstance(title, str) and 1 <= len(title.strip()) <= 200, 422,
                "invalid_input", "title is required (1-200 characters)")
    elif title is not None:
        require(isinstance(title, str) and 1 <= len(title.strip()) <= 200, 422,
                "invalid_input", "title must be 1-200 characters when provided")
    description = metadata.get("description", "")
    require(isinstance(description, str) and len(description) <= 2000, 422,
            "invalid_input", "description must be at most 2000 characters")
    content_sha = metadata.get("content_sha256")
    require(isinstance(content_sha, str) and len(content_sha) == 64
            and all(c in "0123456789abcdef" for c in content_sha), 422, "invalid_input",
            "content_sha256 must be a 64-character lowercase hex digest")
    byte_size = metadata.get("byte_size")
    require(type(byte_size) is int and 0 < byte_size <= config.max_upload_bytes, 422,
            "invalid_input", "byte_size must be a positive integer within the limit")
    expected_revision = metadata.get("expected_revision")
    if not creating:
        require(type(expected_revision) is int and expected_revision >= 1, 422,
                "invalid_input", "expected_revision is required when updating a dashboard")
    return title, description, content_sha, byte_size, expected_revision


class Publisher:
    def __init__(self, config: Config, database: Database, operations: Operations,
                 store: ContentStore, authorizer: Authorizer):
        self.config = config
        self.database = database
        self.operations = operations
        self.store = store
        self.authorizer = authorizer

    def publish(self, actor: AuthContext, reverify: Callable[[], AuthContext], key: str,
                *, dashboard_id: str | None, metadata: dict, html_stream,
                trace_id: str = "local") -> PublishOutcome:
        """`reverify` re-runs the full authentication for the presented
        credential (outside any transaction) after the upload finishes."""
        method = "POST"
        path = (f"/api/v1/dashboards/{dashboard_id}/versions" if dashboard_id
                else "/api/v1/dashboards")
        actor.requires_scope("write")
        title, description, content_sha, byte_size, expected_revision = _validate_metadata(
            metadata, creating=dashboard_id is None, config=self.config)
        payload = {"title": title, "description": description, "content_sha256": content_sha,
                   "byte_size": byte_size}
        if expected_revision is not None:
            payload["expected_revision"] = expected_revision

        staged = self._stage_one(actor, key, method, path, dashboard_id, payload, byte_size)
        if isinstance(staged, PublishOutcome):
            return staged
        operation_id, attempt_id = staged
        try:
            staged_path = self.store.stage_stream(attempt_id, html_stream, content_sha, byte_size)
            storage_key = new_id()
            self.store.finalize(storage_key, staged_path)
        except ApiError as error:
            self._record_failure(operation_id, error)
            raise

        # Fresh identity right before the final transaction; the principal
        # must not have changed mid-upload.
        fresh = reverify()
        require(fresh.principal_id == actor.principal_id, 401, "token_revoked",
                "The credential identity changed during the upload")

        last_error: Exception | None = None
        for _ in range(CONTENT_LOCK_RETRIES):
            try:
                return self._stage_three(fresh, operation_id, attempt_id, dashboard_id, title,
                                         description, storage_key, content_sha, byte_size,
                                         expected_revision, trace_id, key)
            except ApiError as error:
                # The final commit legitimately failed (revocation, revision
                # conflict, ...): record the terminal failure and release the
                # reservation instead of waiting for lease recovery.
                self._record_failure(operation_id, error)
                raise
            except OperationalError as error:
                last_error = error
                continue
        contention = ApiError(503, "database_unavailable",
                              "Database contention; retry with the same key", retryable=True)
        self._record_failure(operation_id, contention)
        raise last_error  # type: ignore[misc]

    # --------------------------------------------------------------- stages

    def _stage_one(self, actor: AuthContext, key: str, method: str, path: str,
                   dashboard_id: str | None, payload: dict, byte_size: int):
        for _ in range(CONTENT_LOCK_RETRIES):
            try:
                with self.database.transaction() as connection:
                    with self.database.guard(connection, exclusive=False):
                        if dashboard_id is not None:
                            row = self.authorizer.dashboard_or_none(connection, dashboard_id)
                            require(row is not None, 404, "not_found", "Dashboard is not visible")
                            self.authorizer.authorize(connection, row, actor, "write")
                            require(row["status"] == "published", 409, "invalid_input",
                                    "Restore the dashboard before publishing a new version")
                            quota_owner_id = row["owner_principal_id"]
                        else:
                            quota_owner_id = actor.principal_id
                        quotas.ensure_rows(connection, quota_owner_id)
                        quotas.check_headroom(connection, self.config, quota_owner_id,
                                              extra_bytes=byte_size,
                                              new_dashboard=dashboard_id is None)
                        begun = self.operations.begin_staged(
                            connection, actor, key, action="publish", method=method, path=path,
                            target_id=dashboard_id, payload=payload,
                            new_dashboard=dashboard_id is None, byte_size=byte_size,
                            quota_owner_id=quota_owner_id)
                        if isinstance(begun, dict):
                            if begun.get("state") == "succeeded":
                                return PublishOutcome(begun, 200)  # recorded replay
                            return PublishOutcome(begun, 202, retry_after=2)
                        return begun
            except OperationalError:
                continue
        raise ApiError(503, "database_unavailable",
                       "Database contention; retry with the same key", retryable=True)

    def _stage_three(self, actor: AuthContext, operation_id: str, attempt_id: str,
                     dashboard_id: str | None, title, description, storage_key: str,
                     content_sha: str, byte_size: int, expected_revision, trace_id: str,
                     key: str) -> PublishOutcome:
        with self.database.transaction() as connection:
            with self.database.guard(connection, exclusive=False):
                # This attempt must still own the lease before anything else.
                self.operations.claim_for_commit(connection, operation_id, attempt_id)
                moment = now()
                moment_db = to_db(moment)
                if dashboard_id is None:
                    dashboard_row = None
                else:
                    dashboard_row = self.authorizer.dashboard(connection, dashboard_id)
                    # Upload-period revocation is caught here: authorize()
                    # re-reads the live account state and the current ACL.
                    self.authorizer.authorize(connection, dashboard_row, actor, "write",
                                              moment=moment)
                    require(dashboard_row["status"] == "published", 409, "invalid_input",
                            "Restore the dashboard before publishing a new version")
                    require(dashboard_row["revision"] == expected_revision, 409,
                            "revision_conflict", "Dashboard changed; read it again")
                    title = title if title is not None else dashboard_row["title"]
                owner_id = (actor.principal_id if dashboard_row is None
                            else dashboard_row["owner_principal_id"])
                effective_dashboard_id = dashboard_row["id"] if dashboard_row is not None else ""
                existing_versions = quotas.count_dashboard_versions(
                    connection, effective_dashboard_id)
                if dashboard_row is not None:
                    require(existing_versions < self.config.max_versions_per_dashboard, 507,
                            "quota_exceeded", "Version history limit reached for this dashboard")
                number = existing_versions + 1
                version_id = new_id()
                if dashboard_row is None:
                    # Circular FK (dashboard ⇄ version): the dashboard row is
                    # created with a null pointer, the version is added, then
                    # the pointer is filled — all inside this transaction.
                    dashboard_id = new_id()
                    connection.execute(models.dashboards.insert().values(
                        id=dashboard_id, owner_principal_id=owner_id, title=title,
                        description=description, current_version_id=None,
                        revision=1, status="published", created_at=moment_db,
                        updated_at=moment_db, published_at=moment_db))
                    connection.execute(models.dashboard_versions.insert().values(
                        id=version_id, dashboard_id=dashboard_id, number=number,
                        storage_key=storage_key, sha256=content_sha, byte_size=byte_size,
                        created_by=actor.principal_id, created_at=moment_db))
                    connection.execute(models.dashboards.update().where(
                        models.dashboards.c.id == dashboard_id).values(
                        current_version_id=version_id))
                    revision = 1
                else:
                    connection.execute(models.dashboard_versions.insert().values(
                        id=version_id, dashboard_id=dashboard_id, number=number,
                        storage_key=storage_key, sha256=content_sha, byte_size=byte_size,
                        created_by=actor.principal_id, created_at=moment_db))
                    revision = dashboard_row["revision"] + 1
                    connection.execute(models.dashboards.update().where(
                        models.dashboards.c.id == dashboard_id).values(
                        title=title, description=description,
                        current_version_id=version_id, revision=revision,
                        updated_at=moment_db, published_at=moment_db))
                self.operations.convert_reservation(connection, operation_id, owner_id,
                                                    byte_size=byte_size,
                                                    dashboard_added=dashboard_row is None)
                record_audit(connection, actor=actor, action="publish_version",
                             target_type="dashboard", target_id=dashboard_id,
                             after={"version_id": version_id, "number": number,
                                    "sha256": content_sha, "byte_size": byte_size},
                             trace_id=trace_id, operation_id=operation_id)
                result = {
                    "dashboard_id": dashboard_id,
                    "version_id": version_id,
                    "version_number": number,
                    "revision": revision,
                    "sha256": content_sha,
                    "view_url": f"{self.config.control_origin}/dashboards/{dashboard_id}",
                }
                connection.execute(models.operations.update().where(
                    models.operations.c.id == operation_id).values(
                    target_id=dashboard_id, state="succeeded", lease_until=None,
                    result=result, error=None, updated_at=moment_db))
                wrapper = self.operations.wrap(operation_id, key, "succeeded", result, None)
                return PublishOutcome(wrapper, 201)

    def _record_failure(self, operation_id: str, error: ApiError) -> None:
        # 5xx/timeout/contention failures are retryable with the same key;
        # 4xx input/authorization failures need a new logical request.
        retryable = error.status >= 500 or error.status in (408, 425, 429)
        try:
            with self.database.transaction() as connection:
                with self.database.guard(connection, exclusive=False):
                    self.operations.fail(connection, operation_id, code=error.code,
                                         message=error.message, retryable=retryable)
                    self.operations.release_reservation(connection, operation_id)
        except Exception:  # noqa: BLE001 - failure recording must not mask the real error
            pass

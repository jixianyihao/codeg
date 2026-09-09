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

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from . import models, quotas
from .authn import AuthContext
from .authorization import Authorizer
from .config import Config
from .database import Database, from_db, record_audit, to_db
from .errors import ApiError, now, require, to_rfc3339
from .operations import Operations
from .storage import ContentStore

CONTENT_LOCK_RETRIES = 3


def version_state(connection, row, *, include_draft: bool = True) -> dict:
    """Same pointer/number contract for mutation results and resource reads."""
    current_id = row["current_version_id"]
    draft_id = row["draft_version_id"] if include_draft else None
    ids = [value for value in (current_id, draft_id) if value is not None]
    numbers = dict(connection.execute(select(models.dashboard_versions.c.id,
                                             models.dashboard_versions.c.number).where(
        models.dashboard_versions.c.id.in_(ids))).all()) if ids else {}
    return {"current_version_id": current_id, "current_version_number": numbers.get(current_id),
            "draft_version_id": draft_id, "draft_version_number": numbers.get(draft_id),
            "has_draft": draft_id is not None,
            "published_at": to_rfc3339(from_db(row["published_at"]))}


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
    description = metadata.get("description", "" if creating else None)
    if creating or "description" in metadata:
        require(isinstance(description, str) and len(description) <= 2000, 422,
                "invalid_input", "description must be at most 2000 characters")
    require(metadata.get("disposition", "publish") in ("publish", "save_draft"),
            422, "invalid_input", "disposition must be publish/save_draft")
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
        credential (outside any transaction) after the upload finishes.

        Stage 1 occupies the idempotency key and fixes the S3 object
        coordinates; stage 2 streams, validates and puts the verified
        object; stage 3 is the MySQL commit that references it. An object
        written without a committed reference is an orphan the operator
        cleanup removes (contracts.md section 9)."""
        method = "POST"
        path = (f"/api/v1/dashboards/{dashboard_id}/versions" if dashboard_id
                else "/api/v1/dashboards")
        actor.requires_scope("write")
        title, description, content_sha, byte_size, expected_revision = _validate_metadata(
            metadata, creating=dashboard_id is None, config=self.config)
        disposition = metadata.get("disposition", "publish")
        payload = {"title": title, "description": description, "content_sha256": content_sha,
                   "byte_size": byte_size}
        # Preserve the legacy publish fingerprint while making save a distinct
        # immutable logical request under the same endpoint.
        if disposition != "publish":
            payload["disposition"] = disposition
        if expected_revision is not None:
            payload["expected_revision"] = expected_revision

        staged = self._stage_one(actor, key, method, path, dashboard_id, payload, byte_size)
        if isinstance(staged, PublishOutcome):
            return staged
        operation_id, attempt_id = staged["operation_id"], staged["attempt_id"]
        uploaded = False
        put_unknown = False
        try:
            staged_path = self.store.stage_stream(attempt_id, html_stream, content_sha, byte_size)
            with self.database.transaction() as connection:
                self.operations.mark_upload_started(connection, operation_id, attempt_id)
            put_unknown = True
            try:
                object_ref = self.store.put_verified(staged["object_key"],
                                                     staged_path.read_bytes(),
                                                     content_sha, byte_size)
            except ApiError:
                # The PUT may have reached S3 even though it failed here
                # (lost response / lost read-back): the outcome is unknown,
                # never "nothing uploaded" (R2).
                put_unknown = True
                raise
            staged_path.unlink(missing_ok=True)
            uploaded = True
            with self.database.transaction() as connection:
                self.operations.mark_uploaded(connection, operation_id, attempt_id,
                                              object_ref.object_version_id)
        except ApiError as error:
            self._settle_failed_attempt(operation_id, attempt_id, uploaded, put_unknown)
            self._record_failure(operation_id, error, attempt_id=attempt_id)
            raise

        # Fresh identity right before the final transaction; the principal
        # must not have changed mid-upload.
        fresh = reverify()
        require(fresh.principal_id == actor.principal_id, 401, "token_revoked",
                "The credential identity changed during the upload")

        last_error: Exception | None = None
        for _ in range(CONTENT_LOCK_RETRIES):
            try:
                return self._stage_three(fresh, staged, title, description,
                                         content_sha, byte_size, expected_revision,
                                         trace_id, key, disposition)
            except ApiError as error:
                # The final commit legitimately failed (revocation, revision
                # conflict, ...): settle the attempt and record the failure.
                self._settle_failed_attempt(operation_id, attempt_id, uploaded, put_unknown)
                self._record_failure(operation_id, error, attempt_id=attempt_id)
                raise
            except OperationalError as error:
                last_error = error
                continue
        contention = ApiError(503, "database_unavailable",
                              "Database contention; retry with the same key", retryable=True)
        self._settle_failed_attempt(operation_id, attempt_id, uploaded, put_unknown)
        self._record_failure(operation_id, contention, attempt_id=attempt_id)
        raise last_error  # type: ignore[misc]

    def _settle_failed_attempt(self, operation_id: str, attempt_id: str,
                               uploaded: bool, put_unknown: bool = False) -> None:
        """After a failure: uploaded/unknown-outcome objects stay reserved
        and wait for exact cleanup; attempts that never started an S3 PUT
        release quota now. Fenced to this attempt only."""
        try:
            with self.database.transaction() as connection:
                if uploaded or put_unknown:
                    self.operations.mark_cleanup_pending(connection, operation_id, attempt_id)
                else:
                    self.operations.release_reservation(connection, operation_id, attempt_id)
        except Exception:  # noqa: BLE001 - settlement must not mask the real error
            pass

    # --------------------------------------------------------------- stages

    def _stage_one(self, actor: AuthContext, key: str, method: str, path: str,
                   dashboard_id: str | None, payload: dict, byte_size: int):
        for _ in range(CONTENT_LOCK_RETRIES):
            try:
                with self.database.transaction() as connection:
                    with self.database.guard(connection, exclusive=False):
                        existing = self.operations.find(connection, actor.principal_id, key,
                                                        for_update=True)
                        operation_payload = payload
                        disposition = payload.get("disposition", "publish")
                        # Old publishers normalized an omitted update description
                        # to ''. Only actual legacy records use that fingerprint;
                        # new requests must distinguish preservation from clearing.
                        if (existing is not None and existing["action"] == "publish"
                                and disposition == "publish" and payload["description"] is None):
                            operation_payload = {**payload, "description": ""}
                        if dashboard_id is not None:
                            row = self.authorizer.dashboard_or_none(connection, dashboard_id)
                            require(row is not None, 404, "not_found", "Dashboard is not visible")
                            self.authorizer.authorize(connection, row, actor, "write")
                            require(row["status"] != "archived", 409, "invalid_input",
                                    "Restore the dashboard to draft before changing content")
                            if payload.get("disposition", "publish") == "publish":
                                self.authorizer.authorize_publication(connection, row, actor)
                            quota_owner_id = row["owner_principal_id"]
                        else:
                            self.authorizer.check_context_validity(connection, actor)
                            actor.requires_scope("write")
                            quota_owner_id = actor.principal_id

                        def reserve(reserve_connection):
                            # Headroom is validated only when a NEW attempt is
                            # about to reserve quota — replays of finished or
                            # in-flight operations must not be blocked by a
                            # now-full quota (R11).
                            quotas.ensure_rows(reserve_connection, quota_owner_id)
                            quotas.check_headroom(
                                reserve_connection, self.config, quota_owner_id,
                                extra_bytes=byte_size,
                                new_dashboard=dashboard_id is None)

                        begun = self.operations.begin_staged(
                            connection, actor, key,
                            action="publish_v2" if disposition == "publish" else "save_draft",
                            method=method, path=path,
                            target_id=dashboard_id, payload=operation_payload,
                            new_dashboard=dashboard_id is None, byte_size=byte_size,
                            quota_owner_id=quota_owner_id, reserve=reserve)
                        if "attempt_id" not in begun:
                            # A finished/pending wrapper, not a staged attempt.
                            # Replays re-check CURRENT authorization first —
                            # a revoked editor or narrowed scope must not read
                            # the old result back (R8 semantics for staged ops).
                            if dashboard_id is not None:
                                current = self.authorizer.dashboard_or_none(
                                    connection, dashboard_id)
                                require(current is not None, 404, "not_found",
                                        "Dashboard is not visible")
                                self.authorizer.authorize(connection, current, actor,
                                                          "write")
                            else:
                                self.authorizer.check_context_validity(connection, actor)
                                actor.requires_scope("write")
                            if begun.get("state") == "succeeded":
                                return PublishOutcome(begun, 200)  # recorded replay
                            return PublishOutcome(begun, 202, retry_after=2)
                        return begun
            except OperationalError:
                continue
        raise ApiError(503, "database_unavailable",
                       "Database contention; retry with the same key", retryable=True)

    def _stage_three(self, actor: AuthContext, staged: dict, title, description,
                     content_sha: str, byte_size: int, expected_revision, trace_id: str,
                     key: str, disposition: str = "publish") -> PublishOutcome:
        operation_id = staged["operation_id"]
        attempt_id = staged["attempt_id"]
        dashboard_id = staged["dashboard_id"]
        version_id = staged["version_id"]
        with self.database.transaction() as connection:
            with self.database.guard(connection, exclusive=False):
                # This attempt must still own the lease before anything else.
                self.operations.claim_for_commit(connection, operation_id, attempt_id)
                reservation = self.operations.reservation(connection, operation_id, attempt_id)
                moment = now()
                moment_db = to_db(moment)
                # Re-validate identity and live account state INSIDE the
                # final transaction — both create and update paths. The
                # out-of-transaction reverify cannot cover the window between
                # it and this commit (R4).
                self.authorizer.check_context_validity(connection, actor, moment=moment)
                actor.requires_scope("write")
                if dashboard_id != staged["dashboard_id"]:
                    dashboard_id = staged["dashboard_id"]
                if reservation is None:
                    raise ApiError(409, "operation_lease_lost", "Reservation is missing")
                if dashboard_id not in ("", None) and dashboard_id != reservation["dashboard_id"]:
                    dashboard_id = reservation["dashboard_id"]
                dashboard_row = self.authorizer.dashboard_for_update(
                    connection, reservation["dashboard_id"])
                if dashboard_row is not None:
                    # Update path: the dashboard exists; re-authorize with the
                    # current ACL (upload-period revocation is caught here).
                    self.authorizer.authorize(connection, dashboard_row, actor, "write",
                                              moment=moment)
                    require(dashboard_row["status"] != "archived", 409, "invalid_input",
                            "Restore the dashboard to draft before changing content")
                    if disposition == "publish":
                        self.authorizer.authorize_publication(connection, dashboard_row, actor,
                                                              moment=moment)
                    require(dashboard_row["revision"] == expected_revision, 409,
                            "revision_conflict", "Dashboard changed; read it again")
                    title = title if title is not None else dashboard_row["title"]
                    description = (description if description is not None
                                   else dashboard_row["description"])
                    existing_versions = quotas.count_dashboard_versions(
                        connection, dashboard_row["id"])
                    require(existing_versions < self.config.max_versions_per_dashboard, 507,
                            "quota_exceeded", "Version history limit reached for this dashboard")
                    revision = dashboard_row["revision"] + 1
                    number = existing_versions + 1
                    connection.execute(models.dashboard_versions.insert().values(
                        id=version_id, dashboard_id=dashboard_row["id"], number=number,
                        storage_bucket=reservation["storage_bucket"],
                        storage_key=reservation["storage_key"],
                        object_version_id=reservation["object_version_id"],
                        sha256=content_sha, byte_size=byte_size,
                        created_by=actor.principal_id, created_at=moment_db,
                        published_at=moment_db if disposition == "publish" else None))
                    pointer_values = ({"current_version_id": version_id, "status": "published",
                                       "published_at": moment_db} if disposition == "publish"
                                      else {"draft_version_id": version_id})
                    connection.execute(models.dashboards.update().where(
                        models.dashboards.c.id == dashboard_row["id"]).values(
                        title=title, description=description,
                        revision=revision, updated_at=moment_db, **pointer_values))
                    status = "published" if disposition == "publish" else dashboard_row["status"]
                    draft_id = (version_id if disposition == "save_draft"
                                else dashboard_row["draft_version_id"])
                    effective_id = dashboard_row["id"]
                else:
                    # Create path: no dashboard row exists for these fixed ids.
                    connection.execute(models.dashboards.insert().values(
                        id=dashboard_id, owner_principal_id=actor.principal_id,
                        title=title, description=description, current_version_id=None,
                        revision=1, status="published" if disposition == "publish" else "draft",
                        created_at=moment_db, updated_at=moment_db,
                        published_at=moment_db if disposition == "publish" else None))
                    connection.execute(models.dashboard_versions.insert().values(
                        id=version_id, dashboard_id=dashboard_id, number=1,
                        storage_bucket=reservation["storage_bucket"],
                        storage_key=reservation["storage_key"],
                        object_version_id=reservation["object_version_id"],
                        sha256=content_sha, byte_size=byte_size,
                        created_by=actor.principal_id, created_at=moment_db,
                        published_at=moment_db if disposition == "publish" else None))
                    connection.execute(models.dashboards.update().where(
                        models.dashboards.c.id == dashboard_id).values(
                        **({"current_version_id": version_id} if disposition == "publish"
                           else {"draft_version_id": version_id})))
                    revision = 1
                    number = 1
                    status = "published" if disposition == "publish" else "draft"
                    draft_id = version_id if disposition == "save_draft" else None
                    effective_id = dashboard_id
                self.operations.convert_reservation(connection, operation_id, attempt_id)
                record_audit(connection, actor=actor,
                             action="publish_version" if disposition == "publish" else "save_draft",
                             target_type="dashboard", target_id=effective_id,
                             after={"version_id": version_id, "number": number,
                                    "sha256": content_sha, "byte_size": byte_size},
                             trace_id=trace_id, operation_id=operation_id)
                result = {
                    "dashboard_id": effective_id,
                    "version_id": version_id,
                    "version_number": number,
                    "revision": revision,
                    "status": status,
                    "disposition": disposition,
                    "draft_version_id": draft_id,
                    "has_draft": draft_id is not None,
                    "sha256": content_sha,
                    "view_url": f"{self.config.control_origin}/dashboards/{effective_id}",
                    **version_state(connection, self.authorizer.dashboard(connection, effective_id)),
                }
                connection.execute(models.operations.update().where(
                    models.operations.c.id == operation_id).values(
                    target_id=effective_id, state="succeeded", lease_until=None,
                    result=result, error=None, updated_at=moment_db))
                wrapper = self.operations.wrap(operation_id, key, "succeeded", result, None)
                return PublishOutcome(wrapper, 201)

    def _record_failure(self, operation_id: str, error: ApiError, *, attempt_id: str) -> None:
        """Record the failure BOUND TO THE ATTEMPT: a stale worker whose lease
        was taken over matches zero rows and cannot overwrite the newer
        attempt's in-flight or committed outcome (R3)."""
        # 5xx/timeout/contention failures are retryable with the same key;
        # 4xx input/authorization failures need a new logical request.
        retryable = error.status >= 500 or error.status in (408, 425, 429)
        try:
            with self.database.transaction() as connection:
                with self.database.guard(connection, exclusive=False):
                    self.operations.fail(connection, operation_id, attempt_id,
                                         code=error.code, message=error.message,
                                         retryable=retryable)
                    self.operations.release_reservation(connection, operation_id,
                                                        attempt_id)
        except Exception:  # noqa: BLE001 - failure recording must not mask the real error
            pass

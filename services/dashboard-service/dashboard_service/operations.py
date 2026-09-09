"""Operation lifecycle: idempotency, attempts, leases, recovery.

States: accepted → processing → succeeded/failed. The success result is
committed in the same transaction as the business change. Every business
write carries a UUID Idempotency-Key; same key + same request hash returns
the recorded outcome, a different hash is a 409.
"""
import json
from datetime import timedelta

from sqlalchemy import func, select

from . import models
from .authn import AuthContext
from .config import Config
from .database import Database, from_db, to_db
from .errors import ApiError, is_uuid, new_id, now, require


def request_hash(method: str, path: str, payload) -> str:
    import hashlib
    canonical = json.dumps([method, path, payload], sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_idempotency_key(value) -> str:
    require(isinstance(value, str) and is_uuid(value), 422, "invalid_input",
            "Idempotency-Key must be a UUID generated per logical operation")
    return value.lower()


class OperationOutcome:
    """Either a finished/pending operation record, or None to proceed."""

    def __init__(self, wrapper: dict | None, *, pending: bool = False):
        self.wrapper = wrapper
        self.pending = pending


class Operations:
    def __init__(self, config: Config, database: Database):
        self.config = config
        self.database = database

    # ---------------------------------------------------------------- lookups

    def find(self, connection, principal_id: str, key: str):
        return connection.execute(
            select(models.operations).where(
                models.operations.c.principal_id == principal_id,
                models.operations.c.idempotency_key == key)
        ).mappings().one_or_none()

    def get_wrapper(self, row) -> dict:
        state = row["state"]
        return {
            "operation_id": row["id"],
            "request_id": row["idempotency_key"],
            "state": state,
            "result": row["result"],
            "error": row["error"],
        }

    def check_rate(self, connection, principal_id: str) -> None:
        window_start = to_db(now() - timedelta(seconds=60))
        recent = connection.execute(
            select(func.count()).select_from(models.operations).where(
                models.operations.c.principal_id == principal_id,
                models.operations.c.created_at > window_start)
        ).scalar_one()
        require(recent < self.config.writes_per_minute, 429, "rate_limited",
                "Too many writes; retry after a minute with the same key",
                retryable=True)

    # ----------------------------------------------------- synchronous writes

    def run_sync(self, actor: AuthContext, key: str, *, action: str, method: str, path: str,
                 target_id: str | None, payload, exclusive_guard: bool, unit,
                 trace_id: str = "local") -> dict:
        """Single-transaction write: occupy the key, execute, commit result.

        `unit(connection, operation_id) -> result` performs the business
        change; it must raise ApiError on failure (the whole transaction
        rolls back, including the operation row).
        """
        fingerprint = request_hash(method, path, payload)
        attempt = new_id()
        lease_until = now() + timedelta(seconds=self.config.operation_lease_seconds)
        with self.database.transaction() as connection:
            with self.database.guard(connection, exclusive=exclusive_guard):
                existing = self.find(connection, actor.principal_id, key)
                if existing is not None:
                    return self._existing_outcome(existing, fingerprint)
                self.check_rate(connection, actor.principal_id)
                operation_id = new_id()
                result = unit(connection, operation_id)
                moment = now()
                connection.execute(models.operations.insert().values(
                    id=operation_id, principal_id=actor.principal_id, idempotency_key=key,
                    action=action, method=method, path=path, target_id=target_id,
                    request_hash=fingerprint, state="succeeded", attempt_id=attempt,
                    lease_until=to_db(lease_until), result=result, error=None,
                    result_expires_at=to_db(moment + timedelta(days=self.config.operation_result_days)),
                    created_at=to_db(moment), updated_at=to_db(moment)))
                return self.wrap(operation_id, key, "succeeded", result, None)

    def _existing_outcome(self, row, fingerprint: str) -> dict:
        require(row["request_hash"] == fingerprint, 409, "idempotency_conflict",
                "This idempotency key was used for a different request")
        if row["result_purged_at"] is not None:
            raise ApiError(410, "idempotency_result_expired",
                           "The stored result expired; start a new operation with a new key")
        return self.get_wrapper(row)

    @staticmethod
    def wrap(operation_id: str, request_id: str, state: str, result, error) -> dict:
        return {"operation_id": operation_id, "request_id": request_id,
                "state": state, "result": result, "error": error}

    # --------------------------------------------------- staged (publish) ops

    def begin_staged(self, connection, actor: AuthContext, key: str, *, action: str,
                     method: str, path: str, target_id: str | None, payload,
                     new_dashboard: bool, byte_size: int,
                     quota_owner_id: str | None = None) -> dict:
        """Occupy the key and reserve quota for a staged publish.

        Returns a dict describing the attempt (operation_id, attempt_id,
        dashboard_id, version_id, object_key) to proceed, or a finished
        wrapper dict to return to the caller (202 / replayed result).
        Object coordinates are fixed here so S3 keys are deterministic and
        never collide between attempts.
        """
        from .storage import build_object_key
        fingerprint = request_hash(method, path, payload)
        existing = self.find(connection, actor.principal_id, key)
        if existing is not None:
            if existing["state"] == "succeeded":
                # Same key + same hash replays the recorded result.
                return self._existing_outcome(existing, fingerprint)
            if existing["state"] in ("accepted", "processing"):
                lease = from_db(existing["lease_until"])
                if lease is not None and lease > now():
                    return self.get_wrapper(existing)  # caller turns this into 202
                # Stale lease: recover it now so the retry can proceed.
                self._fail_interrupted(connection, existing)
            # failed state (or freshly interrupted): only a retryable
            # interrupted failure may resume under the same key.
            wrapper = self._existing_outcome(existing, fingerprint)
            require(wrapper["state"] == "failed" and (wrapper["error"] or {}).get("retryable"),
                    409, "idempotency_conflict",
                    "This idempotency key already completed; use a new key for a new operation")
            connection.execute(models.operations.update().where(
                models.operations.c.id == existing["id"]).values(
                state="accepted", request_hash=fingerprint, updated_at=to_db(now())))
            operation_id, attempt = existing["id"], new_id()
            self.release_reservation(connection, operation_id)
        else:
            self.check_rate(connection, actor.principal_id)
            operation_id, attempt = new_id(), new_id()
        quota_owner = quota_owner_id or actor.principal_id
        dashboard_id = target_id or new_id()
        version_id = new_id()
        object_key = build_object_key(self.config.s3_prefix, dashboard_id,
                                      version_id, attempt)
        moment = now()
        lease_until = moment + timedelta(seconds=self.config.operation_lease_seconds)
        for scope, owner in (("global", "*"), ("owner", quota_owner)):
            connection.execute(models.quota_usage.insert().prefix_with("IGNORE").values(
                scope=scope, owner_id=owner, used_bytes=0, reserved_bytes=0,
                dashboard_count=0, reserved_count=0))
        count_delta = 1 if new_dashboard else 0
        for scope, owner in (("owner", quota_owner), ("global", "*")):
            connection.execute(models.quota_usage.update().where(
                models.quota_usage.c.scope == scope,
                models.quota_usage.c.owner_id == owner).values(
                reserved_bytes=models.quota_usage.c.reserved_bytes + byte_size,
                reserved_count=models.quota_usage.c.reserved_count + count_delta))
        if existing is None:
            connection.execute(models.operations.insert().values(
                id=operation_id, principal_id=actor.principal_id, idempotency_key=key,
                action=action, method=method, path=path, target_id=target_id,
                request_hash=fingerprint, state="accepted", attempt_id=attempt,
                lease_until=to_db(lease_until), result=None, error=None,
                result_expires_at=to_db(moment + timedelta(days=self.config.operation_result_days)),
                created_at=to_db(moment), updated_at=to_db(moment)))
        else:
            connection.execute(models.operations.update().where(
                models.operations.c.id == operation_id).values(
                attempt_id=attempt, lease_until=to_db(lease_until), state="accepted",
                updated_at=to_db(moment)))
        connection.execute(models.upload_reservations.insert().values(
            operation_id=operation_id, attempt_id=attempt, owner_id=quota_owner,
            dashboard_id=dashboard_id, version_id=version_id,
            storage_bucket=self.config.s3_bucket, storage_key=object_key,
            object_version_id=None, state="reserved",
            reserved_bytes=byte_size, reserved_count=count_delta,
            expires_at=to_db(lease_until)))
        return {"operation_id": operation_id, "attempt_id": attempt,
                "dashboard_id": dashboard_id, "version_id": version_id,
                "object_key": object_key}

    # ------------------------------------------------- attempt reservations

    def reservation(self, connection, operation_id: str, attempt_id: str):
        return connection.execute(
            select(models.upload_reservations).where(
                models.upload_reservations.c.operation_id == operation_id,
                models.upload_reservations.c.attempt_id == attempt_id)
        ).mappings().one_or_none()

    def mark_uploaded(self, connection, operation_id: str, attempt_id: str,
                      object_version_id: str | None) -> None:
        connection.execute(models.upload_reservations.update().where(
            models.upload_reservations.c.operation_id == operation_id,
            models.upload_reservations.c.attempt_id == attempt_id).values(
            state="uploaded", object_version_id=object_version_id))

    def mark_cleanup_pending(self, connection, operation_id: str,
                             attempt_id: str | None = None) -> None:
        statement = models.upload_reservations.update().where(
            models.upload_reservations.c.operation_id == operation_id,
            models.upload_reservations.c.state != "committed")
        if attempt_id is not None:
            statement = statement.where(
                models.upload_reservations.c.attempt_id == attempt_id)
        connection.execute(statement.values(state="cleanup_pending"))

    def release_reservation(self, connection, operation_id: str,
                            attempt_id: str | None = None) -> None:
        """Release quota for attempts that never uploaded an object.
        Uploaded/unknown attempts stay reserved until the object is verified
        deleted by cleanup (contracts.md section 9)."""
        statement = select(models.upload_reservations).where(
            models.upload_reservations.c.operation_id == operation_id)
        if attempt_id is not None:
            statement = statement.where(
                models.upload_reservations.c.attempt_id == attempt_id)
        for reservation in connection.execute(statement).mappings().all():
            if reservation["state"] == "uploaded":
                continue  # object may exist; only cleanup may release it
            self._release_quota(connection, reservation)
            connection.execute(models.upload_reservations.delete().where(
                models.upload_reservations.c.operation_id == reservation["operation_id"],
                models.upload_reservations.c.attempt_id == reservation["attempt_id"]))

    def _release_quota(self, connection, reservation) -> None:
        for scope, owner in (("owner", reservation["owner_id"]), ("global", "*")):
            connection.execute(models.quota_usage.update().where(
                models.quota_usage.c.scope == scope,
                models.quota_usage.c.owner_id == owner).values(
                reserved_bytes=models.quota_usage.c.reserved_bytes - reservation["reserved_bytes"],
                reserved_count=models.quota_usage.c.reserved_count - reservation["reserved_count"]))

    def convert_reservation(self, connection, operation_id: str, attempt_id: str) -> None:
        reservation = self.reservation(connection, operation_id, attempt_id)
        if reservation is None:
            raise ApiError(409, "operation_lease_lost", "Reservation is missing")
        connection.execute(models.upload_reservations.update().where(
            models.upload_reservations.c.operation_id == operation_id,
            models.upload_reservations.c.attempt_id == attempt_id).values(
            state="committed"))
        count_delta = 1 if reservation["reserved_count"] else 0
        for scope, owner in (("owner", reservation["owner_id"]), ("global", "*")):
            connection.execute(models.quota_usage.update().where(
                models.quota_usage.c.scope == scope,
                models.quota_usage.c.owner_id == owner).values(
                used_bytes=models.quota_usage.c.used_bytes + reservation["reserved_bytes"],
                reserved_bytes=models.quota_usage.c.reserved_bytes - reservation["reserved_bytes"],
                dashboard_count=models.quota_usage.c.dashboard_count + count_delta,
                reserved_count=models.quota_usage.c.reserved_count - count_delta))

    def cleanup_ready_reservations(self, store) -> int:
        """Operator path: delete objects of non-committed attempts, then
        release their quota and drop the rows. Committed objects are never
        touched."""
        cleaned = 0
        with self.database.read_only() as connection:
            rows = connection.execute(
                select(models.upload_reservations).where(
                    models.upload_reservations.c.state.in_(
                        ("cleanup_pending", "uploaded")),
                    models.upload_reservations.c.storage_key.is_not(None))
            ).mappings().all()
        for row in rows:
            from .storage import ObjectRef
            ref = ObjectRef(bucket=row["storage_bucket"], key=row["storage_key"],
                            object_version_id=row["object_version_id"])
            deleted = store.delete_orphan(ref)
            if not deleted:
                continue  # retry next pass; never release unverified storage
            with self.database.transaction() as connection:
                with self.database.guard(connection, exclusive=False):
                    fresh = connection.execute(
                        select(models.upload_reservations).where(
                            models.upload_reservations.c.operation_id == row["operation_id"],
                            models.upload_reservations.c.attempt_id == row["attempt_id"])
                    ).mappings().one_or_none()
                    if fresh is None or fresh["state"] == "committed":
                        continue
                    self._release_quota(connection, fresh)
                    connection.execute(models.upload_reservations.delete().where(
                        models.upload_reservations.c.operation_id == fresh["operation_id"],
                        models.upload_reservations.c.attempt_id == fresh["attempt_id"]))
                    cleaned += 1
        return cleaned

    def _fail_interrupted(self, connection, row) -> None:
        connection.execute(models.operations.update().where(
            models.operations.c.id == row["id"]).values(
            state="failed",
            error={"code": "operation_interrupted", "message":
                   "The operation was interrupted before commit; retry with the same key",
                   "retryable": True, "trace_id": row["id"]},
            updated_at=to_db(now())))
        self.release_reservation(connection, row["id"])

    def claim_for_commit(self, connection, operation_id: str, attempt_id: str) -> None:
        """Final transaction gate: this attempt must still own the lease.
        A lease takeover or stale worker must fail here — never double-commit."""
        row = connection.execute(
            select(models.operations.c.state, models.operations.c.attempt_id,
                   models.operations.c.lease_until)
            .where(models.operations.c.id == operation_id)
            .with_for_update()).mappings().one()
        lease = from_db(row["lease_until"])
        if row["attempt_id"] != attempt_id or row["state"] not in ("accepted", "processing") \
                or lease is None or lease <= now():
            raise ApiError(409, "operation_lease_lost",
                           "Another worker took over this operation")

    def fail(self, connection, operation_id: str, *, code: str, message: str,
             retryable: bool) -> None:
        connection.execute(models.operations.update().where(
            models.operations.c.id == operation_id).values(
            state="failed", lease_until=None,
            error={"code": code, "message": message, "retryable": retryable},
            updated_at=to_db(now())))

    def succeed(self, connection, operation_id: str, attempt_id: str, result: dict) -> None:
        self.claim_for_commit(connection, operation_id, attempt_id)
        connection.execute(models.operations.update().where(
            models.operations.c.id == operation_id).values(
            state="succeeded", lease_until=None, result=result, error=None,
            updated_at=to_db(now())))

    # -------------------------------------------------------------- recovery

    def recover_stale_operations(self) -> int:
        """Lease-expired accepted/processing rows become retryable failures;
        attempts that never uploaded release quota, uploaded ones wait for
        object cleanup. A result committed by the original worker stays
        succeeded (the update targets only non-terminal states)."""
        recovered = 0
        with self.database.transaction() as connection:
            with self.database.guard(connection, exclusive=True):
                stale = connection.execute(
                    select(models.operations.c.id, models.operations.c.state)
                    .where(models.operations.c.state.in_(("accepted", "processing")),
                           models.operations.c.lease_until < to_db(now()))
                    .with_for_update(skip_locked=True)).mappings().all()
                for row in stale:
                    full = connection.execute(
                        select(models.operations).where(models.operations.c.id == row["id"])
                    ).mappings().one()
                    self._fail_interrupted(connection, full)
                    recovered += 1
        return recovered

    def purge_expired_results(self) -> int:
        """Keep the compact key/fingerprint record; drop the payload."""
        purged = 0
        with self.database.transaction() as connection:
            rows = connection.execute(
                select(models.operations.c.id).where(
                    models.operations.c.result_expires_at < to_db(now()),
                    models.operations.c.result_purged_at.is_(None))).all()
            for (operation_id,) in rows:
                connection.execute(models.operations.update().where(
                    models.operations.c.id == operation_id).values(
                    result=None, error=None, result_purged_at=to_db(now())))
                purged += 1
        return purged

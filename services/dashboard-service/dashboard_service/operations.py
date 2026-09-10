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

    def find(self, connection, principal_id: str, key: str, *, for_update=False):
        statement = select(models.operations).where(
            models.operations.c.principal_id == principal_id,
            models.operations.c.idempotency_key == key)
        if for_update:
            statement = statement.with_for_update()
        return connection.execute(statement).mappings().one_or_none()

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
                 trace_id: str = "local", replay_check=None) -> dict:
        """Single-transaction write: occupy the key, execute, commit result.

        `unit(connection, operation_id) -> result` performs the business
        change; it must raise ApiError on failure (the whole transaction
        rolls back, including the operation row).

        `replay_check(connection)` re-validates the caller's CURRENT right to
        perform the action before a recorded result is replayed — a caller
        whose access or scope was revoked after the original success gets the
        authorization error, never the stored result (R8).
        """
        fingerprint = request_hash(method, path, payload)
        attempt = new_id()
        lease_until = now() + timedelta(seconds=self.config.operation_lease_seconds)
        with self.database.transaction() as connection:
            with self.database.guard(connection, exclusive=exclusive_guard):
                existing = self.find(connection, actor.principal_id, key)
                if existing is not None:
                    if replay_check is not None:
                        replay_check(connection)  # raises on revoked access
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
                     quota_owner_id: str | None = None,
                     reserve=None) -> dict:
        """Occupy the key and reserve quota for a staged publish.

        Returns a dict describing the attempt (operation_id, attempt_id,
        dashboard_id, version_id, object_key) to proceed, or a finished
        wrapper dict to return to the caller (202 / replayed result).
        Object coordinates are fixed here so S3 keys are deterministic and
        never collide between attempts.

        `reserve(connection)` validates quota headroom inside this
        transaction and is invoked ONLY when a new attempt is about to
        reserve quota — never on the replay paths, so a full-quota owner can
        still replay a finished result (R11).
        """
        from .storage import build_object_key
        fingerprint = request_hash(method, path, payload)
        # Serialize recovery against both another retry and final commit.
        # A pre-lock snapshot must never decide which attempt to interrupt.
        existing = self.find(connection, actor.principal_id, key, for_update=True)
        if existing is not None:
            # Every branch below first proves the request matches the key.
            require(existing["request_hash"] == fingerprint, 409,
                    "idempotency_conflict",
                    "This idempotency key was used for a different request")
            if existing["state"] in ("accepted", "processing"):
                lease = from_db(existing["lease_until"])
                if lease is not None and lease > now():
                    return self.get_wrapper(existing)  # caller turns this into 202
                # Stale lease: recover it now so the retry can proceed, then
                # branch on the FRESH row (the in-memory snapshot is stale).
                self._fail_interrupted(connection, existing)
                existing = self.find(connection, actor.principal_id, key)
            if existing["state"] != "failed":
                # Succeeded replays and purged results follow the shared path.
                return self._existing_outcome(existing, fingerprint)
            # failed state (or freshly interrupted): only a retryable
            # interrupted failure may resume under the same key.
            error = existing["error"] or {}
            require(error.get("retryable"), 409, "idempotency_conflict",
                    "This idempotency key already completed; use a new key for a new operation")
            operation_id, attempt = existing["id"], new_id()
            moment = now()
            lease_until = moment + timedelta(seconds=self.config.operation_lease_seconds)
            # Fence the resume: only the transaction that flips failed →
            # accepted owns the new attempt. A concurrent same-key retry that
            # loses the race sees the winner's pending state.
            resumed = connection.execute(models.operations.update().where(
                models.operations.c.id == operation_id,
                models.operations.c.state == "failed").values(
                state="accepted", attempt_id=attempt, request_hash=fingerprint,
                lease_until=to_db(lease_until), updated_at=to_db(moment))).rowcount
            if not resumed:
                fresh = self.find(connection, actor.principal_id, key)
                return self.get_wrapper(fresh)  # the winner's 202 wrapper
            self.release_reservation(connection, operation_id, existing["attempt_id"])
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
        if reserve is not None:
            reserve(connection)  # raises quota_exceeded before any reservation
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

    def mark_upload_started(self, connection, operation_id: str, attempt_id: str) -> None:
        """Commit this marker BEFORE starting any S3 PUT.

        cleanup_pending also represents an in-flight/unknown PUT. Cleanup
        still checks the operation lease, so a live worker may safely turn
        the marker into uploaded; crash recovery retains coordinates/quota.
        """
        self.claim_for_commit(connection, operation_id, attempt_id)
        changed = connection.execute(models.upload_reservations.update().where(
            models.upload_reservations.c.operation_id == operation_id,
            models.upload_reservations.c.attempt_id == attempt_id,
            models.upload_reservations.c.state == "reserved").values(
                state="cleanup_pending")).rowcount
        require(changed == 1, 409, "operation_lease_lost",
                "Upload attempt is no longer reserved")

    def mark_uploaded(self, connection, operation_id: str, attempt_id: str,
                      object_version_id: str | None) -> None:
        self.claim_for_commit(connection, operation_id, attempt_id)
        changed = connection.execute(models.upload_reservations.update().where(
            models.upload_reservations.c.operation_id == operation_id,
            models.upload_reservations.c.attempt_id == attempt_id,
            models.upload_reservations.c.state.in_(("reserved", "cleanup_pending"))).values(
            state="uploaded", object_version_id=object_version_id)).rowcount
        require(changed == 1, 409, "operation_lease_lost", "Upload attempt is no longer active")

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
        """Release quota ONLY for attempts that never started an S3 PUT
        (state `reserved`). Uploaded, unknown-outcome and cleanup-pending
        attempts keep their rows and quota until the exact object cleanup
        settles them (contracts.md section 9); committed rows are never
        touched here."""
        statement = select(models.upload_reservations).where(
            models.upload_reservations.c.operation_id == operation_id,
            models.upload_reservations.c.state == "reserved")
        if attempt_id is not None:
            statement = statement.where(
                models.upload_reservations.c.attempt_id == attempt_id)
        for reservation in connection.execute(statement.with_for_update()).mappings().all():
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
        if reservation["state"] != "uploaded":
            # Cleanup fenced this attempt between the upload and the final
            # transaction — the object may already be gone.
            raise ApiError(409, "operation_lease_lost",
                           "The attempt is no longer committable")
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
        """Operator path: settle never-committed attempt objects, then release
        their quota and drop the rows.

        Fencing (R1): a candidate is only settled after a MySQL transaction
        locks the operation row and proves this attempt can no longer commit
        (terminal operation, superseded attempt, or a lease that expired past
        the grace window — a live lease means the worker may still run its
        final transaction). Only then is S3 touched, outside the transaction.
        Committed objects are never addressed.
        """
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
            if not self._attempt_settleable(row):
                continue  # may still commit; never touch its object
            ref = ObjectRef(bucket=row["storage_bucket"], key=row["storage_key"],
                            object_version_id=row["object_version_id"])
            settled = store.delete_orphan(ref)
            if not settled:
                continue  # retry next pass; never release unverified storage
            with self.database.transaction() as connection:
                with self.database.guard(connection, exclusive=False):
                    fresh = connection.execute(
                        select(models.upload_reservations).where(
                            models.upload_reservations.c.operation_id == row["operation_id"],
                            models.upload_reservations.c.attempt_id == row["attempt_id"])
                        .with_for_update()).mappings().one_or_none()
                    if fresh is None or fresh["state"] == "committed":
                        continue
                    self._release_quota(connection, fresh)
                    connection.execute(models.upload_reservations.delete().where(
                        models.upload_reservations.c.operation_id == fresh["operation_id"],
                        models.upload_reservations.c.attempt_id == fresh["attempt_id"]))
                    cleaned += 1
        return cleaned

    def _attempt_settleable(self, row) -> bool:
        """Inside a guarded transaction: True when this attempt provably
        cannot commit anymore AND its dead worker's S3 client has had time to
        finish (grace), so a straggling late PUT cannot resurrect the key."""
        with self.database.transaction() as connection:
            with self.database.guard(connection, exclusive=False):
                operation = connection.execute(
                    select(models.operations.c.state, models.operations.c.attempt_id,
                           models.operations.c.lease_until, models.operations.c.error)
                    .where(models.operations.c.id == row["operation_id"])
                    .with_for_update()).mappings().one_or_none()
                fresh = connection.execute(
                    select(models.upload_reservations.c.state).where(
                        models.upload_reservations.c.operation_id == row["operation_id"],
                        models.upload_reservations.c.attempt_id == row["attempt_id"])
                ).mappings().one_or_none()
                if fresh is None or fresh["state"] == "committed":
                    return False
                if operation is None:
                    # Orphaned reference without an operation row: settle only
                    # after the reservation's own expiry plus grace.
                    return from_db(row["expires_at"]) is not None and \
                        from_db(row["expires_at"]) < now() - timedelta(
                            seconds=self.config.cleanup_grace_seconds)
                if operation["state"] == "succeeded":
                    return True  # worker finished; leftover row is a leftover
                if operation["state"] == "failed":
                    error = operation["error"] or {}
                    if error.get("code") != "operation_interrupted":
                        return True  # worker recorded its own post-S3 failure
                # accepted/processing with a superseded attempt, or an
                # interrupted failure: the dead worker's PUT may still be in
                # flight — wait out the grace window anchored at THIS
                # attempt's own lease (the operation row's lease belongs to
                # whichever attempt currently owns the key).
                own_lease = from_db(row["expires_at"])
                return own_lease is not None and own_lease < now() - timedelta(
                    seconds=self.config.cleanup_grace_seconds)

    def _fail_interrupted(self, connection, row) -> bool:
        # Bind recovery to exactly the attempt/lease that was inspected.
        # In particular, a blocked old retry must not fail a newly resumed
        # attempt after that retry wins the operation-row lock.
        changed = connection.execute(models.operations.update().where(
            models.operations.c.id == row["id"],
            models.operations.c.attempt_id == row["attempt_id"],
            models.operations.c.lease_until == row["lease_until"],
            models.operations.c.lease_until < to_db(now()),
            models.operations.c.state.in_(("accepted", "processing"))).values(
            state="failed",
            error={"code": "operation_interrupted", "message":
                   "The operation was interrupted before commit; retry with the same key",
                   "retryable": True, "trace_id": row["id"]},
            updated_at=to_db(now()))).rowcount
        if changed:
            self.release_reservation(connection, row["id"], row["attempt_id"])
        return bool(changed)

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

    def fail(self, connection, operation_id: str, attempt_id: str, *, code: str,
             message: str, retryable: bool) -> None:
        """Record a failure for ONE attempt. The update is bound to the
        attempt and to non-terminal states, so a stale worker can never
        overwrite a newer attempt's in-flight or already-committed result."""
        connection.execute(models.operations.update().where(
            models.operations.c.id == operation_id,
            models.operations.c.attempt_id == attempt_id,
            models.operations.c.state.in_(("accepted", "processing"))).values(
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
                    .with_for_update(
                        # SKIP LOCKED needs MySQL 8.0+; recovery holds the
                        # exclusive guard, so plain FOR UPDATE is safe on 5.7.
                        skip_locked=self.database.supports_skip_locked
                    )).mappings().all()
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

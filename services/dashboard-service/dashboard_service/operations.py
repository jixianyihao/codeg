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
                     quota_owner_id: str | None = None) -> tuple[str, str] | dict:
        """Occupy the key for a multi-stage publish. Returns (operation_id,
        attempt_id) to proceed, or a finished wrapper dict to return."""
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
            # Explicit same-key retry of an interrupted operation: resume it.
            connection.execute(models.operations.update().where(
                models.operations.c.id == existing["id"]).values(
                state="accepted", request_hash=fingerprint, updated_at=to_db(now())))
            operation_id, attempt = existing["id"], new_id()
            # Drop any leftover reservation so the fresh attempt cannot
            # double-reserve quota (recovery usually did this already).
            self.release_reservation(connection, operation_id)
        else:
            self.check_rate(connection, actor.principal_id)
            operation_id, attempt = new_id(), new_id()
        moment = now()
        lease_until = moment + timedelta(seconds=self.config.operation_lease_seconds)
        # Quota always belongs to the dashboard owner; an editor's upload
        # consumes the owner's allocation, not the editor's.
        quota_owner = quota_owner_id or actor.principal_id
        connection.execute(models.quota_usage.insert().prefix_with("IGNORE").values(
            scope="global", owner_id="*", used_bytes=0, reserved_bytes=0,
            dashboard_count=0, reserved_count=0))
        connection.execute(models.quota_usage.insert().prefix_with("IGNORE").values(
            scope="owner", owner_id=quota_owner, used_bytes=0, reserved_bytes=0,
            dashboard_count=0, reserved_count=0))
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
            reserved_bytes=byte_size, reserved_count=1 if new_dashboard else 0,
            expires_at=to_db(lease_until)))
        # Reflect the reservation on the quota rows themselves so concurrent
        # check_headroom calls see used+reserved correctly.
        count_delta = 1 if new_dashboard else 0
        for scope, owner in (("owner", quota_owner), ("global", "*")):
            connection.execute(models.quota_usage.update().where(
                models.quota_usage.c.scope == scope,
                models.quota_usage.c.owner_id == owner).values(
                reserved_bytes=models.quota_usage.c.reserved_bytes + byte_size,
                reserved_count=models.quota_usage.c.reserved_count + count_delta))
        return operation_id, attempt

    def _fail_interrupted(self, connection, row) -> None:
        connection.execute(models.operations.update().where(
            models.operations.c.id == row["id"]).values(
            state="failed",
            error={"code": "operation_interrupted", "message":
                   "The operation was interrupted before commit; retry with the same key",
                   "retryable": True, "trace_id": row["id"]},
            updated_at=to_db(now())))
        self.release_reservation(connection, row["id"])

    def release_reservation(self, connection, operation_id: str) -> None:
        reservation = connection.execute(
            select(models.upload_reservations.c).where(
                models.upload_reservations.c.operation_id == operation_id)
        ).mappings().one_or_none()
        if reservation is None:
            return
        connection.execute(models.upload_reservations.delete().where(
            models.upload_reservations.c.operation_id == operation_id))
        for scope, owner in (("owner", reservation["owner_id"]), ("global", "*")):
            connection.execute(models.quota_usage.update().where(
                models.quota_usage.c.scope == scope,
                models.quota_usage.c.owner_id == owner).values(
                reserved_bytes=models.quota_usage.c.reserved_bytes - reservation["reserved_bytes"],
                reserved_count=models.quota_usage.c.reserved_count - reservation["reserved_count"]))

    def convert_reservation(self, connection, operation_id: str, owner_id: str,
                            *, byte_size: int, dashboard_added: bool) -> None:
        reservation = connection.execute(
            select(models.upload_reservations.c).where(
                models.upload_reservations.c.operation_id == operation_id)
        ).mappings().one()
        connection.execute(models.upload_reservations.delete().where(
            models.upload_reservations.c.operation_id == operation_id))
        count_delta = 1 if dashboard_added else 0
        for scope, owner in (("owner", owner_id), ("global", "*")):
            connection.execute(models.quota_usage.update().where(
                models.quota_usage.c.scope == scope,
                models.quota_usage.c.owner_id == owner).values(
                used_bytes=models.quota_usage.c.used_bytes + byte_size,
                reserved_bytes=models.quota_usage.c.reserved_bytes - byte_size,
                dashboard_count=models.quota_usage.c.dashboard_count + count_delta,
                reserved_count=models.quota_usage.c.reserved_count
                - (1 if dashboard_added else 0)))

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
        their quota reservations are released. A result committed by the
        original worker stays succeeded (the update targets only
        non-terminal states)."""
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

"""Host-operator commands (design.md §4): account lifecycle, JWT issuance,
recovery, cleanup. These run on the server host with direct database and
key-file access — they are deliberately not exposed as authenticated API.

Issued tokens are printed once to stdout and never written to logs, audit
rows, or operation results.
"""
import argparse
import json
import sys
from datetime import timedelta

from sqlalchemy import delete, func, select

from . import models
from .authn import issue_service_jwt
from .config import Config
from .database import Database, record_audit, to_db
from .errors import ApiError, new_id, now, require
from .operations import Operations
from .storage import ContentStore

VALID_SCOPES = ("read", "write", "manage")


class OperatorError(Exception):
    pass


class Operator:
    def __init__(self, config: Config, database: Database):
        self.config = config
        self.database = database

    # ------------------------------------------------------------- accounts

    def create_account(self, name: str, scopes: list[str]) -> dict:
        self._validate_scopes(scopes)
        require(0 < len(name.strip()) <= 200, 422, "invalid_input",
                "Account name must be 1-200 characters")
        principal_id = new_id()
        with self.database.transaction() as connection:
            with self.database.guard(connection, exclusive=True):
                duplicate = connection.execute(
                    select(models.service_accounts.c.principal_id)
                    .where(models.service_accounts.c.name == name)).one_or_none()
                if duplicate is not None:
                    raise OperatorError(f"account already exists: {name}")
                moment = to_db(now())
                connection.execute(models.principals.insert().values(
                    id=principal_id, type="service", display_name=name, created_at=moment))
                connection.execute(models.service_accounts.insert().values(
                    principal_id=principal_id, name=name, enabled=True, token_version=1,
                    scopes=list(dict.fromkeys(scopes)), revision=1,
                    created_at=moment, updated_at=moment))
                record_audit(connection, actor=_OPERATOR_ACTOR, action="service_account.create",
                             target_type="service_account", target_id=principal_id,
                             after={"name": name, "scopes": sorted(set(scopes))},
                             trace_id=new_id())
        return {"principal_id": principal_id, "name": name, "scopes": sorted(set(scopes)),
                "enabled": True, "token_version": 1}

    def _validate_scopes(self, scopes: list[str]) -> None:
        require(isinstance(scopes, list) and scopes
                and all(scope in VALID_SCOPES for scope in scopes), 422, "invalid_input",
                f"scopes must be a non-empty subset of {list(VALID_SCOPES)}")

    def _account(self, connection, identifier: str):
        row = connection.execute(
            select(models.service_accounts).where(
                (models.service_accounts.c.principal_id == identifier)
                | (models.service_accounts.c.name == identifier))).mappings().one_or_none()
        if row is None:
            raise OperatorError(f"unknown account: {identifier}")
        return row

    def set_enabled(self, identifier: str, enabled: bool) -> dict:
        with self.database.transaction() as connection:
            with self.database.guard(connection, exclusive=True):
                account = self._account(connection, identifier)
                updates: dict = {"enabled": enabled, "updated_at": to_db(now()),
                                 "revision": account["revision"] + 1}
                if enabled != account["enabled"] and not enabled:
                    updates["token_version"] = account["token_version"] + 1
                if enabled and not account["enabled"]:
                    # Re-enabling must not resurrect old tokens.
                    updates["token_version"] = account["token_version"] + 1
                connection.execute(models.service_accounts.update().where(
                    models.service_accounts.c.principal_id == account["principal_id"])
                    .values(**updates))
                record_audit(connection, actor=_OPERATOR_ACTOR,
                             action="service_account.enable" if enabled else "service_account.disable",
                             target_type="service_account", target_id=account["principal_id"],
                             before={"enabled": account["enabled"],
                                     "token_version": account["token_version"]},
                             after={"enabled": enabled,
                                    "token_version": updates.get("token_version",
                                                                 account["token_version"])},
                             trace_id=new_id())
                return self._public_view(self._account(connection, account["principal_id"]))

    def set_scopes(self, identifier: str, scopes: list[str]) -> dict:
        self._validate_scopes(scopes)
        with self.database.transaction() as connection:
            with self.database.guard(connection, exclusive=True):
                account = self._account(connection, identifier)
                connection.execute(models.service_accounts.update().where(
                    models.service_accounts.c.principal_id == account["principal_id"])
                    .values(scopes=list(dict.fromkeys(scopes)),
                            revision=account["revision"] + 1, updated_at=to_db(now())))
                record_audit(connection, actor=_OPERATOR_ACTOR, action="service_account.set_scopes",
                             target_type="service_account", target_id=account["principal_id"],
                             before={"scopes": account["scopes"]},
                             after={"scopes": sorted(set(scopes))}, trace_id=new_id())
                return self._public_view(self._account(connection, account["principal_id"]))

    def reset_tokens(self, identifier: str) -> dict:
        with self.database.transaction() as connection:
            with self.database.guard(connection, exclusive=True):
                account = self._account(connection, identifier)
                connection.execute(models.service_accounts.update().where(
                    models.service_accounts.c.principal_id == account["principal_id"])
                    .values(token_version=account["token_version"] + 1,
                            revision=account["revision"] + 1, updated_at=to_db(now())))
                record_audit(connection, actor=_OPERATOR_ACTOR,
                             action="service_account.reset_token_version",
                             target_type="service_account", target_id=account["principal_id"],
                             before={"token_version": account["token_version"]},
                             after={"token_version": account["token_version"] + 1},
                             trace_id=new_id())
                return self._public_view(self._account(connection, account["principal_id"]))

    def issue(self, identifier: str, ttl_seconds: int | None = None) -> dict:
        ttl = ttl_seconds or self.config.jwt_default_ttl_seconds
        require(1 <= ttl <= self.config.jwt_max_ttl_seconds, 422, "invalid_input",
                f"ttl must be 1..{self.config.jwt_max_ttl_seconds} seconds")
        with self.database.transaction() as connection:
            account = self._account(connection, identifier)
            require(account["enabled"], 409, "invalid_input",
                    "Refusing to issue for a disabled account; enable it first")
            token = issue_service_jwt(self.config, principal_id=account["principal_id"],
                                      token_version=account["token_version"], ttl_seconds=ttl)
        return {"principal_id": account["principal_id"], "name": account["name"],
                "token": token, "expires_in_seconds": ttl}

    def list_accounts(self) -> list[dict]:
        with self.database.read_only() as connection:
            rows = connection.execute(
                select(models.service_accounts).order_by(models.service_accounts.c.name)
            ).mappings().all()
            return [self._public_view(row) for row in rows]

    @staticmethod
    def _public_view(row) -> dict:
        return {"principal_id": row["principal_id"], "name": row["name"],
                "enabled": row["enabled"], "token_version": row["token_version"],
                "scopes": row["scopes"], "revision": row["revision"]}

    # ------------------------------------------------------------- recovery

    def recover_operations(self) -> dict:
        operations = Operations(self.config, self.database)
        return {"interrupted_operations_failed": operations.recover_stale_operations(),
                "results_purged": operations.purge_expired_results()}

    def cleanup_orphan_files(self, *, older_than_seconds: int = 86400) -> dict:
        store = ContentStore(self.config)
        store.prepare()
        with self.database.read_only() as connection:
            keys = {row[0] for row in connection.execute(
                select(models.dashboard_versions.c.storage_key))}
        return {"files_removed": store.cleanup_orphans(keys, older_than_seconds=older_than_seconds)}

    def verify_storage(self) -> dict:
        store = ContentStore(self.config)
        with self.database.read_only() as connection:
            versions = connection.execute(
                select(models.dashboard_versions.c.storage_key,
                       models.dashboard_versions.c.sha256,
                       models.dashboard_versions.c.byte_size)).mappings().all()
        missing = []
        for version in versions:
            try:
                store.read_version(version["storage_key"], version["sha256"],
                                   version["byte_size"])
            except ApiError:
                missing.append(version["storage_key"])
        return {"versions_checked": len(versions), "missing_or_corrupt": missing}

    def expire_capabilities(self) -> int:
        with self.database.transaction() as connection:
            return connection.execute(
                delete(models.view_capabilities).where(
                    models.view_capabilities.c.expires_at < to_db(now()))
            ).rowcount

    def stats(self) -> dict:
        with self.database.read_only() as connection:
            return {
                "principals": connection.execute(
                    select(func.count()).select_from(models.principals)).scalar_one(),
                "service_accounts": connection.execute(
                    select(func.count()).select_from(models.service_accounts)).scalar_one(),
                "dashboards": connection.execute(
                    select(func.count()).select_from(models.dashboards)).scalar_one(),
                "versions": connection.execute(
                    select(func.count()).select_from(models.dashboard_versions)).scalar_one(),
                "operations": connection.execute(
                    select(func.count()).select_from(models.operations)).scalar_one(),
            }


class _OperatorActor:
    principal_id = "operator"
    principal_type = "service"
    auth_method = "operator"


_OPERATOR_ACTOR = _OperatorActor()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dashboard-operator",
                                     description="AresClaw dashboard service operator CLI")
    parser.add_argument("command", choices=[
        "create-account", "disable-account", "enable-account", "set-scopes", "issue",
        "reset-tokens", "list-accounts", "recover-operations", "cleanup-orphan-files",
        "verify-storage", "expire-capabilities", "stats"])
    parser.add_argument("--account", help="principal_id or account name")
    parser.add_argument("--scopes", help="comma list of read/write/manage")
    parser.add_argument("--ttl", type=int, help="token TTL seconds (default 30 days)")
    args = parser.parse_args(argv)

    config = Config.from_env()
    from .database import create_db_engine
    database = Database(create_db_engine(config.database_url))
    database.seed_guard()
    operator = Operator(config, database)
    try:
        if args.command == "create-account":
            require(args.account and args.scopes, 422, "invalid_input",
                    "--account and --scopes are required")
            result = operator.create_account(args.account, args.scopes.split(","))
        elif args.command == "disable-account":
            result = operator.set_enabled(args.account, False)
        elif args.command == "enable-account":
            result = operator.set_enabled(args.account, True)
        elif args.command == "set-scopes":
            require(args.scopes, 422, "invalid_input", "--scopes is required")
            result = operator.set_scopes(args.account, args.scopes.split(","))
        elif args.command == "issue":
            result = operator.issue(args.account, args.ttl)
        elif args.command == "reset-tokens":
            result = operator.reset_tokens(args.account)
        elif args.command == "list-accounts":
            result = operator.list_accounts()
        elif args.command == "recover-operations":
            result = operator.recover_operations()
        elif args.command == "cleanup-orphan-files":
            result = operator.cleanup_orphan_files()
        elif args.command == "verify-storage":
            result = operator.verify_storage()
        elif args.command == "expire-capabilities":
            result = {"capabilities_removed": operator.expire_capabilities()}
        else:
            result = operator.stats()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OperatorError, ApiError) as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

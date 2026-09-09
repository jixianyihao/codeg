#!/usr/bin/env python3
"""Dependency-free CLI for the AresClaw dashboard HTTP contracts."""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path, PurePath
import socket
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid


EXIT_SUCCESS = 0
EXIT_PENDING = 10
EXIT_AUTH = 20
EXIT_FORBIDDEN = 21
EXIT_CONFLICT = 30
EXIT_INPUT = 40
EXIT_UNKNOWN = 50
EXIT_NETWORK = 60
EXIT_REMOTE = 70
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
WRITE_ACTIONS = {
    "dashboard.publish",
    "dashboard.rename",
    "dashboard.rollback",
    "dashboard.share",
    "dashboard.revoke",
    "dashboard.public",
    "dashboard.access_apply",
    "dashboard.archive",
    "dashboard.restore",
    "group.create",
    "group.set_members",
}


class CliError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = EXIT_INPUT):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


class NetworkFailure(Exception):
    pass


class RemoteFailure(Exception):
    def __init__(self, status: int, payload: object):
        super().__init__(str(status))
        self.status = status
        self.payload = payload


class JsonParser(argparse.ArgumentParser):
    def error(self, message):
        raise CliError("invalid_arguments", message)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def parse_json_bytes(data: bytes) -> object:
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliError("invalid_response", "service returned invalid JSON", EXIT_REMOTE) from exc


def validate_uuid(value: str, label: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise CliError("invalid_uuid", f"{label} must be a UUID") from exc
    return str(parsed)


def normalized_date(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise CliError("invalid_datetime", f"{label} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CliError("invalid_datetime", f"{label} must include a UTC offset")
    utc = parsed.astimezone(dt.timezone.utc)
    text = utc.isoformat(timespec="seconds")
    return text.replace("+00:00", "Z")


def relative_path(value: str, label: str) -> str:
    path = PurePath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise CliError("unsafe_path", f"{label} must be a workspace-relative path")
    return path.as_posix()


def is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(attrs & reparse_flag)


def assert_safe_components(root: Path, target: Path, include_target: bool) -> None:
    relative = target.relative_to(root)
    current = root
    parts = relative.parts if include_target else relative.parts[:-1]
    for part in parts:
        current = current / part
        if current.exists() and is_reparse(current):
            raise CliError("unsafe_path", "path contains a symbolic link or reparse point")


def safe_input(root: Path, value: str, label: str = "file") -> Path:
    rel = relative_path(value, label)
    candidate = root.joinpath(*PurePath(rel).parts)
    assert_safe_components(root, candidate, True)
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise CliError("file_not_found", f"{label} does not exist") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CliError("unsafe_path", f"{label} escapes the configured workdir") from exc
    if not resolved.is_file() or is_reparse(candidate):
        raise CliError("unsafe_path", f"{label} must be a regular non-link file")
    return resolved


def safe_output(root: Path, value: str) -> Path:
    rel = relative_path(value, "output")
    candidate = root.joinpath(*PurePath(rel).parts)
    assert_safe_components(root, candidate, True)
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError as exc:
        raise CliError("unsafe_path", "output escapes the configured workdir") from exc
    if candidate.exists():
        raise CliError("output_exists", "output already exists")
    parent = candidate.parent
    parent.mkdir(parents=True, exist_ok=True)
    assert_safe_components(root, candidate, False)
    return candidate


def read_limited(path: Path, maximum: int = MAX_UPLOAD_BYTES) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(maximum + 1)
    if len(data) > maximum:
        raise CliError("file_too_large", f"file exceeds {maximum} bytes")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CliError("invalid_html", "HTML must be valid UTF-8") from exc
    return data


def validated_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CliError("invalid_config", "service_url must be an HTTP(S) origin")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CliError("invalid_config", "service_url must not contain credentials, query, or fragment")
    if parsed.path not in {"", "/"}:
        raise CliError("invalid_config", "service_url must be an origin without a path")
    return value.rstrip("/")


def validated_bridge_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise CliError("invalid_bridge", "conversation bridge must use an HTTP loopback address")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CliError("invalid_bridge", "conversation bridge URL is invalid")
    return value.rstrip("/") + "/invoke"


class HttpClient:
    def __init__(self, base_url: str, timeout: float, token: str | None = None, session: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token
        self.session = session
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, method: str, path: str, *, body: bytes | None = None, headers=None):
        request_headers = {"Accept": "application/json"}
        if self.token is not None:
            request_headers["Authorization"] = f"Bearer {self.token}"
        if self.session is not None:
            request_headers["X-AresClaw-Dashboard-Session"] = self.session
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=request_headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.status, dict(response.headers), response.read(MAX_UPLOAD_BYTES + 1)
        except urllib.error.HTTPError as exc:
            payload = exc.read(1024 * 1024)
            try:
                decoded = parse_json_bytes(payload)
            except CliError:
                decoded = {"code": "http_error", "message": f"service returned HTTP {exc.code}"}
            raise RemoteFailure(exc.code, decoded) from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            raise NetworkFailure(type(exc).__name__) from exc


def multipart(metadata: dict, html: bytes) -> tuple[bytes, str]:
    boundary = "aresclaw-" + uuid.uuid4().hex
    marker = boundary.encode("ascii")
    body = b"".join(
        [
            b"--" + marker + b"\r\n",
            b'Content-Disposition: form-data; name="metadata"\r\n',
            b"Content-Type: application/json\r\n\r\n",
            json_bytes(metadata),
            b"\r\n--" + marker + b"\r\n",
            b'Content-Disposition: form-data; name="html"; filename="dashboard.html"\r\n',
            b"Content-Type: text/html; charset=utf-8\r\n\r\n",
            html,
            b"\r\n--" + marker + b"--\r\n",
        ]
    )
    return body, f"multipart/form-data; boundary={boundary}"


class IntegrationTransport:
    def __init__(self, config_path: str):
        path = Path(config_path).resolve(strict=True)
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CliError("invalid_config", "could not read integration config") from exc
        if not isinstance(config, dict):
            raise CliError("invalid_config", "integration config must be a JSON object")
        allowed = {"service_url", "workdir", "token_file", "timeout_seconds"}
        if set(config) - allowed or not {"service_url", "workdir", "token_file"}.issubset(config):
            raise CliError("invalid_config", "integration config fields are invalid")
        self.workdir = self._config_path(path.parent, config["workdir"], "workdir")
        if not self.workdir.is_dir():
            raise CliError("invalid_config", "workdir must be a directory")
        token_path = self._config_path(path.parent, config["token_file"], "token_file")
        if not token_path.is_file() or is_reparse(token_path):
            raise CliError("invalid_config", "token_file must be a regular non-link file")
        try:
            token = token_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise CliError("invalid_config", "could not read token_file") from exc
        if not token or len(token) > 16384 or "\n" in token or "\r" in token:
            raise CliError("invalid_config", "token_file must contain exactly one token")
        try:
            timeout = float(config.get("timeout_seconds", 30))
        except (TypeError, ValueError) as exc:
            raise CliError("invalid_config", "timeout_seconds must be a number") from exc
        if not 0 < timeout <= 300:
            raise CliError("invalid_config", "timeout_seconds must be between 0 and 300")
        self.secrets = [token]
        self.client = HttpClient(validated_origin(str(config["service_url"])), timeout, token=token)

    @staticmethod
    def _config_path(base: Path, value: object, label: str) -> Path:
        if not isinstance(value, str) or not value:
            raise CliError("invalid_config", f"{label} must be a path")
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = base / candidate
        try:
            return candidate.resolve(strict=True)
        except OSError as exc:
            raise CliError("invalid_config", f"{label} does not exist") from exc

    def freeze_publish(self, params: dict, request_id: str) -> dict:
        source = safe_input(self.workdir, params["path"])
        descriptor = {
            "kind": "publish",
            "path": relative_path(params["path"], "file"),
            "title": params["title"],
            "description": params.get("description", ""),
            "dashboard_id": params.get("dashboard_id"),
            "expected_revision": params.get("expected_revision"),
        }
        state_dir = self.workdir / ".aresclaw-dashboard" / "requests"
        if (self.workdir / ".aresclaw-dashboard").exists() and is_reparse(self.workdir / ".aresclaw-dashboard"):
            raise CliError("unsafe_path", "request state directory is a link")
        state_dir.mkdir(parents=True, exist_ok=True)
        state_path = state_dir / f"publish-{request_id}.json"
        if state_path.exists():
            try:
                frozen = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CliError("request_state_invalid", "frozen request state is unreadable", EXIT_CONFLICT) from exc
            if frozen.get("descriptor") != descriptor:
                raise CliError("idempotency_conflict", "request ID was already used with different publish parameters", EXIT_CONFLICT)
            try:
                html = base64.b64decode(frozen["content_base64"], validate=True)
            except (KeyError, ValueError) as exc:
                raise CliError("request_state_invalid", "frozen request state is invalid", EXIT_CONFLICT) from exc
        else:
            html = read_limited(source)
            frozen = {"descriptor": descriptor, "content_base64": base64.b64encode(html).decode("ascii")}
            try:
                with state_path.open("x", encoding="utf-8") as handle:
                    json.dump(frozen, handle, ensure_ascii=False, separators=(",", ":"))
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.chmod(state_path, 0o600)
                except OSError:
                    pass
            except FileExistsError:
                return self.freeze_publish(params, request_id)
        result = dict(params)
        result["html_bytes"] = html
        return result

    def freeze_access_changes(
        self, path_value: str, dashboard_id: str, expected_revision: int, request_id: str
    ) -> list:
        descriptor = {
            "kind": "access_apply",
            "path": relative_path(path_value, "changes file"),
            "dashboard_id": dashboard_id,
            "expected_revision": expected_revision,
        }
        state_dir = self.workdir / ".aresclaw-dashboard" / "requests"
        if (self.workdir / ".aresclaw-dashboard").exists() and is_reparse(
            self.workdir / ".aresclaw-dashboard"
        ):
            raise CliError("unsafe_path", "request state directory is a link")
        state_dir.mkdir(parents=True, exist_ok=True)
        state_path = state_dir / f"access-{request_id}.json"
        if state_path.exists():
            try:
                frozen = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CliError(
                    "request_state_invalid",
                    "frozen request state is unreadable",
                    EXIT_CONFLICT,
                ) from exc
            if frozen.get("descriptor") != descriptor or not isinstance(
                frozen.get("changes"), list
            ):
                raise CliError(
                    "idempotency_conflict",
                    "request ID was already used with different access parameters",
                    EXIT_CONFLICT,
                )
            return frozen["changes"]
        changes = read_access_changes(self.workdir, path_value)
        try:
            with state_path.open("x", encoding="utf-8") as handle:
                json.dump(
                    {"descriptor": descriptor, "changes": changes},
                    handle,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(state_path, 0o600)
            except OSError:
                pass
        except FileExistsError:
            return self.freeze_access_changes(
                path_value, dashboard_id, expected_revision, request_id
            )
        return changes

    def invoke(self, action: str, params: dict, request_id: str | None):
        quote = lambda value: urllib.parse.quote(str(value), safe="")
        query = lambda values: urllib.parse.urlencode([(k, v) for k, v in values.items() if v is not None])
        headers = {}
        if request_id:
            headers["Idempotency-Key"] = request_id
        method = "GET"
        path = ""
        body = None
        if action == "dashboard.list":
            path = "/api/v1/dashboards?" + query({"scope": params.get("scope"), "q": params.get("query"), "cursor": params.get("cursor"), "status": params.get("status")})
        elif action == "dashboard.show":
            path = f"/api/v1/dashboards/{quote(params['dashboard_id'])}"
        elif action == "dashboard.source":
            path = f"/api/v1/dashboards/{quote(params['dashboard_id'])}/versions/{quote(params['version_id'])}/source"
        elif action == "dashboard.publish":
            method = "POST"
            html = params["html_bytes"]
            metadata = {"title": params["title"], "description": params.get("description", ""), "content_sha256": hashlib.sha256(html).hexdigest(), "byte_size": len(html)}
            if params.get("expected_revision") is not None:
                metadata["expected_revision"] = params["expected_revision"]
            path = "/api/v1/dashboards" if not params.get("dashboard_id") else f"/api/v1/dashboards/{quote(params['dashboard_id'])}/versions"
            body, content_type = multipart(metadata, html)
            headers["Content-Type"] = content_type
        elif action == "dashboard.operation":
            if params.get("operation_id"):
                path = f"/api/v1/operations/{quote(params['operation_id'])}"
            else:
                path = "/api/v1/operations?" + query({"request_id": params["request_id"]})
        elif action == "dashboard.rename":
            method, path = "PATCH", f"/api/v1/dashboards/{quote(params['dashboard_id'])}"
            body = json_bytes({k: params[k] for k in ("title", "description", "expected_revision") if k in params})
        elif action == "dashboard.versions":
            path = f"/api/v1/dashboards/{quote(params['dashboard_id'])}/versions?" + query({"cursor": params.get("cursor")})
        elif action == "dashboard.rollback":
            method, path = "POST", f"/api/v1/dashboards/{quote(params['dashboard_id'])}/rollback"
            body = json_bytes({"version_id": params["version_id"], "expected_revision": params["expected_revision"]})
        elif action == "dashboard.principals":
            path = "/api/v1/principals?" + query({"type": params["type"], "q": params.get("query"), "cursor": params.get("cursor")})
        elif action == "dashboard.grants":
            path = f"/api/v1/dashboards/{quote(params['dashboard_id'])}/grants"
        elif action == "dashboard.share":
            method, path = "POST", f"/api/v1/dashboards/{quote(params['dashboard_id'])}/grants"
            body = json_bytes({k: v for k, v in params.items() if k != "dashboard_id"})
        elif action == "dashboard.revoke":
            method = "DELETE"
            path = f"/api/v1/dashboards/{quote(params['dashboard_id'])}/grants/{quote(params['subject_type'])}/{quote(params['subject_id'])}?" + query({"expected_revision": params["expected_revision"]})
        elif action == "dashboard.public":
            method, path = "PUT", f"/api/v1/dashboards/{quote(params['dashboard_id'])}/public-access"
            body = json_bytes({k: v for k, v in params.items() if k != "dashboard_id"})
        elif action == "dashboard.access":
            path = f"/api/v1/dashboards/{quote(params['dashboard_id'])}/access?" + query({"subject_id": params.get("subject_id")})
        elif action == "dashboard.access_apply":
            method, path = "POST", f"/api/v1/dashboards/{quote(params['dashboard_id'])}/access-changes"
            body = json_bytes({"expected_revision": params["expected_revision"], "changes": params["changes"]})
        elif action in {"dashboard.archive", "dashboard.restore"}:
            method = "POST"
            verb = action.split(".")[1]
            path = f"/api/v1/dashboards/{quote(params['dashboard_id'])}/{verb}"
            body = json_bytes({"expected_revision": params["expected_revision"]})
        elif action == "group.list":
            path = "/api/v1/groups?" + query({"q": params.get("query"), "cursor": params.get("cursor")})
        elif action == "group.create":
            method, path = "POST", "/api/v1/groups"
            body = json_bytes({k: v for k, v in params.items() if k in {"name", "description"}})
        elif action == "group.set_members":
            method, path = "PUT", f"/api/v1/groups/{quote(params['group_id'])}/members"
            body = json_bytes({"members": params["members"], "expected_revision": params["expected_revision"]})
        else:
            raise CliError("unsupported_action", "unsupported dashboard action")
        if body is not None and "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"
        status, response_headers, response = self.client.request(method, path, body=body, headers=headers)
        if action == "dashboard.source":
            if len(response) > MAX_UPLOAD_BYTES:
                raise CliError("file_too_large", "download exceeds the supported size", EXIT_REMOTE)
            output = safe_output(self.workdir, params["output"])
            with output.open("xb") as handle:
                handle.write(response)
            return {"state": "succeeded", "output": relative_path(params["output"], "output"), "byte_size": len(response)}
        return parse_json_bytes(response), status


class ConversationTransport:
    def __init__(self):
        bridge = os.environ.get("ARESCLAW_DASHBOARD_BRIDGE_URL")
        session = os.environ.get("ARESCLAW_DASHBOARD_SESSION_HANDLE")
        if not bridge or not session:
            raise CliError("conversation_unavailable", "dashboard conversation context is unavailable", EXIT_AUTH)
        if len(session) > 4096 or "\n" in session or "\r" in session:
            raise CliError("conversation_unavailable", "dashboard conversation context is invalid", EXIT_AUTH)
        self.secrets = [session]
        invoke_url = validated_bridge_url(bridge)
        parsed = urllib.parse.urlsplit(invoke_url)
        origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        self.path = parsed.path
        self.client = HttpClient(origin, 30, session=session)

    def invoke(self, action: str, params: dict, request_id: str | None):
        effective_request_id = request_id or str(uuid.uuid4())
        body = json_bytes({"action": action, "params": params, "request_id": effective_request_id})
        status, _headers, response = self.client.request("POST", self.path, body=body, headers={"Content-Type": "application/json"})
        return parse_json_bytes(response), status


def read_access_changes(workdir: Path, path_value: str) -> list:
    path = safe_input(workdir, path_value, "changes file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliError("invalid_changes", "changes file must contain valid UTF-8 JSON") from exc
    changes = value.get("changes") if isinstance(value, dict) else value
    if not isinstance(changes, list) or not 1 <= len(changes) <= 50:
        raise CliError("invalid_changes", "changes must contain between 1 and 50 items")
    allowed = {
        "grant": {"action", "subject_type", "subject_id", "role", "starts_at", "expires_at"},
        "revoke": {"action", "subject_type", "subject_id"},
        "set_public": {"action", "enabled", "starts_at", "expires_at"},
    }
    for change in changes:
        if not isinstance(change, dict) or change.get("action") not in allowed:
            raise CliError("invalid_changes", "each access change must have a supported action")
        if set(change) - allowed[change["action"]]:
            raise CliError("invalid_changes", "access change contains unsupported fields")
        for field in ("starts_at", "expires_at"):
            if field in change and change[field] is not None:
                change[field] = normalized_date(change[field], field)
    return changes


def add_common_write(parser, revision=False):
    if revision:
        parser.add_argument("--expected-revision", type=int, required=True)
    parser.add_argument("--request-id", required=True)


def build_parser() -> JsonParser:
    parser = JsonParser(prog="dashboard")
    parser.add_argument("--auth-mode", choices=["conversation", "integration"], default="conversation")
    parser.add_argument("--config")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=JsonParser)
    commands.add_parser("new-request-id")
    item = commands.add_parser("list")
    item.add_argument("--scope", choices=["mine", "shared", "all"], default="mine")
    item.add_argument("--query")
    item.add_argument("--cursor")
    item.add_argument("--status", choices=["published", "archived"])
    item = commands.add_parser("show")
    item.add_argument("dashboard_id")
    item = commands.add_parser("source")
    item.add_argument("dashboard_id")
    item.add_argument("--version-id", required=True)
    item.add_argument("--output", required=True)
    item = commands.add_parser("publish")
    item.add_argument("--file", required=True)
    item.add_argument("--title", required=True)
    item.add_argument("--description", default="")
    item.add_argument("--dashboard-id")
    item.add_argument("--expected-revision", type=int)
    item.add_argument("--request-id", required=True)
    item = commands.add_parser("operation")
    choice = item.add_mutually_exclusive_group(required=True)
    choice.add_argument("--operation-id")
    choice.add_argument("--request-id")
    item = commands.add_parser("rename")
    item.add_argument("dashboard_id")
    item.add_argument("--title")
    item.add_argument("--description")
    add_common_write(item, True)
    item = commands.add_parser("versions")
    item.add_argument("dashboard_id")
    item.add_argument("--cursor")
    item = commands.add_parser("rollback")
    item.add_argument("dashboard_id")
    item.add_argument("--version-id", required=True)
    add_common_write(item, True)
    item = commands.add_parser("principals")
    item.add_argument("--type", choices=["user", "service", "group"], required=True)
    item.add_argument("--query")
    item.add_argument("--cursor")
    item = commands.add_parser("grants")
    item.add_argument("dashboard_id")
    item = commands.add_parser("share")
    item.add_argument("dashboard_id")
    item.add_argument("--subject-type", choices=["user", "service", "group"], required=True)
    item.add_argument("--subject-id", required=True)
    item.add_argument("--role", choices=["viewer", "editor"], required=True)
    item.add_argument("--starts-at")
    item.add_argument("--expires-at")
    item.add_argument("--clear-start", action="store_true")
    item.add_argument("--clear-expiry", action="store_true")
    add_common_write(item, True)
    item = commands.add_parser("revoke")
    item.add_argument("dashboard_id")
    item.add_argument("--subject-type", choices=["user", "service", "group"], required=True)
    item.add_argument("--subject-id", required=True)
    add_common_write(item, True)
    item = commands.add_parser("public")
    item.add_argument("dashboard_id")
    toggle = item.add_mutually_exclusive_group(required=True)
    toggle.add_argument("--enable", action="store_true")
    toggle.add_argument("--disable", action="store_true")
    item.add_argument("--starts-at")
    item.add_argument("--expires-at")
    item.add_argument("--clear-start", action="store_true")
    item.add_argument("--clear-expiry", action="store_true")
    add_common_write(item, True)
    item = commands.add_parser("access")
    item.add_argument("dashboard_id")
    item.add_argument("--subject-id")
    item = commands.add_parser("access-apply")
    item.add_argument("dashboard_id")
    item.add_argument("--file", required=True)
    add_common_write(item, True)
    for name in ("archive", "restore"):
        item = commands.add_parser(name)
        item.add_argument("dashboard_id")
        add_common_write(item, True)
    group = commands.add_parser("group")
    group_commands = group.add_subparsers(dest="group_command", required=True, parser_class=JsonParser)
    item = group_commands.add_parser("list")
    item.add_argument("--query")
    item.add_argument("--cursor")
    item = group_commands.add_parser("create")
    item.add_argument("--name", required=True)
    item.add_argument("--description", default="")
    add_common_write(item)
    member = group_commands.add_parser("member")
    member_commands = member.add_subparsers(dest="member_command", required=True, parser_class=JsonParser)
    for name in ("add", "remove"):
        item = member_commands.add_parser(name)
        item.add_argument("group_id")
        item.add_argument("--user-id", required=True)
        add_common_write(item, True)
    return parser


def command_action(args, transport):
    command = args.command
    values = vars(args)
    if command == "list":
        return "dashboard.list", {k: values[k] for k in ("scope", "query", "cursor", "status") if values[k] is not None}
    if command == "show":
        return "dashboard.show", {"dashboard_id": args.dashboard_id}
    if command == "source":
        relative_path(args.output, "output")
        return "dashboard.source", {"dashboard_id": args.dashboard_id, "version_id": args.version_id, "output": args.output}
    if command == "publish":
        if bool(args.dashboard_id) != (args.expected_revision is not None):
            raise CliError("invalid_publish", "dashboard-id and expected-revision must be provided together for updates")
        params = {"path": relative_path(args.file, "file"), "title": args.title, "description": args.description}
        if args.dashboard_id:
            params.update({"dashboard_id": args.dashboard_id, "expected_revision": args.expected_revision})
        if isinstance(transport, IntegrationTransport):
            params = transport.freeze_publish(params, args.request_id)
        return "dashboard.publish", params
    if command == "operation":
        params = {"operation_id": args.operation_id} if args.operation_id else {"request_id": args.request_id}
        return "dashboard.operation", params
    if command == "rename":
        if args.title is None and args.description is None:
            raise CliError("invalid_rename", "rename requires title or description")
        params = {"dashboard_id": args.dashboard_id, "expected_revision": args.expected_revision}
        if args.title is not None:
            params["title"] = args.title
        if args.description is not None:
            params["description"] = args.description
        return "dashboard.rename", params
    if command == "versions":
        return "dashboard.versions", {"dashboard_id": args.dashboard_id, "cursor": args.cursor}
    if command == "rollback":
        return "dashboard.rollback", {"dashboard_id": args.dashboard_id, "version_id": args.version_id, "expected_revision": args.expected_revision}
    if command == "principals":
        return "dashboard.principals", {"type": args.type, "query": args.query, "cursor": args.cursor}
    if command == "grants":
        return "dashboard.grants", {"dashboard_id": args.dashboard_id}
    if command in {"share", "public"}:
        if args.starts_at and args.clear_start:
            raise CliError("invalid_dates", "starts-at and clear-start are mutually exclusive")
        if args.expires_at and args.clear_expiry:
            raise CliError("invalid_dates", "expires-at and clear-expiry are mutually exclusive")
        params = {"dashboard_id": args.dashboard_id, "expected_revision": args.expected_revision}
        if command == "share":
            params.update({"subject_type": args.subject_type, "subject_id": args.subject_id, "role": args.role})
        else:
            params["enabled"] = args.enable
        if args.starts_at is not None:
            params["starts_at"] = normalized_date(args.starts_at, "starts-at")
        elif args.clear_start:
            params["starts_at"] = None
        if args.expires_at is not None:
            params["expires_at"] = normalized_date(args.expires_at, "expires-at")
        elif args.clear_expiry:
            params["expires_at"] = None
        return f"dashboard.{command}", params
    if command == "revoke":
        return "dashboard.revoke", {"dashboard_id": args.dashboard_id, "subject_type": args.subject_type, "subject_id": args.subject_id, "expected_revision": args.expected_revision}
    if command == "access":
        return "dashboard.access", {"dashboard_id": args.dashboard_id, "subject_id": args.subject_id}
    if command == "access-apply":
        path_value = relative_path(args.file, "changes file")
        if isinstance(transport, ConversationTransport):
            key, changes = "path", path_value
        else:
            key = "changes"
            changes = transport.freeze_access_changes(
                path_value,
                args.dashboard_id,
                args.expected_revision,
                args.request_id,
            )
        return "dashboard.access_apply", {"dashboard_id": args.dashboard_id, "expected_revision": args.expected_revision, key: changes}
    if command in {"archive", "restore"}:
        return f"dashboard.{command}", {"dashboard_id": args.dashboard_id, "expected_revision": args.expected_revision}
    if command == "group":
        if args.group_command == "list":
            return "group.list", {"query": args.query, "cursor": args.cursor}
        if args.group_command == "create":
            return "group.create", {"name": args.name, "description": args.description}
        return "group.member_change", {"group_id": args.group_id, "user_id": args.user_id, "expected_revision": args.expected_revision, "change": args.member_command}
    raise CliError("unsupported_command", "unsupported command")


def group_member_change(transport, params: dict, request_id: str):
    cursor = None
    found = None
    while True:
        response = transport.invoke("group.list", {"cursor": cursor}, None)
        page = response[0] if isinstance(response, tuple) else response
        if not isinstance(page, dict) or not isinstance(page.get("items"), list):
            raise CliError("invalid_response", "group list response is invalid", EXIT_REMOTE)
        found = next((item for item in page["items"] if item.get("id") == params["group_id"]), None)
        if found or not page.get("next_cursor"):
            break
        cursor = page["next_cursor"]
    if found is None:
        raise CliError("group_not_found", "group was not found", EXIT_REMOTE)
    if found.get("revision") != params["expected_revision"] or not isinstance(found.get("members"), list):
        raise CliError("revision_conflict", "group revision changed or members are unavailable", EXIT_CONFLICT)
    members = list(dict.fromkeys(found["members"]))
    if params["change"] == "add" and params["user_id"] not in members:
        members.append(params["user_id"])
    if params["change"] == "remove":
        members = [member for member in members if member != params["user_id"]]
    return transport.invoke("group.set_members", {"group_id": params["group_id"], "members": members, "expected_revision": params["expected_revision"]}, request_id)


def scrub(value, secrets):
    if isinstance(value, dict):
        return {key: scrub(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub(item, secrets) for item in value]
    if isinstance(value, str):
        result = value
        for secret in secrets:
            if secret:
                result = result.replace(secret, "[redacted]")
        return result
    return value


def emit(payload: object, secrets=None) -> None:
    safe = scrub(payload, secrets or [])
    sys.stdout.write(json.dumps(safe, ensure_ascii=False, separators=(",", ":")) + "\n")


def remote_exit(status: int) -> int:
    if status == 401:
        return EXIT_AUTH
    if status in {403, 404}:
        return EXIT_FORBIDDEN
    if status == 409:
        return EXIT_CONFLICT
    if status in {400, 413, 422}:
        return EXIT_INPUT
    return EXIT_REMOTE


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if any(value == "--token" or value.startswith("--token=") for value in argv):
        emit({"state": "error", "code": "raw_token_forbidden", "message": "raw token arguments are forbidden"})
        return EXIT_INPUT
    secrets = []
    request_id = None
    is_write = False
    try:
        args = build_parser().parse_args(argv)
        if args.command == "new-request-id":
            emit({"request_id": str(uuid.uuid4())})
            return EXIT_SUCCESS
        if args.auth_mode == "integration":
            if not args.config:
                raise CliError("config_required", "integration mode requires --config")
            transport = IntegrationTransport(args.config)
        else:
            if args.config:
                raise CliError("invalid_config", "--config is only valid in integration mode")
            transport = ConversationTransport()
        secrets = transport.secrets
        action, params = command_action(args, transport)
        request_id = getattr(args, "request_id", None) if action != "dashboard.operation" else None
        if request_id:
            request_id = validate_uuid(request_id, "request-id")
        is_write = action in WRITE_ACTIONS or action == "group.member_change"
        if action == "group.member_change":
            response = group_member_change(transport, params, request_id)
        else:
            response = transport.invoke(action, params, request_id)
        status = response[1] if isinstance(response, tuple) else 200
        payload = response[0] if isinstance(response, tuple) else response
        if not isinstance(payload, dict):
            raise CliError("invalid_response", "service response must be a JSON object", EXIT_REMOTE)
        if request_id:
            payload.setdefault("idempotency_key", request_id)
        state = payload.get("state")
        emit(payload, secrets)
        return EXIT_PENDING if status == 202 or state in {"pending", "running"} else EXIT_SUCCESS
    except RemoteFailure as exc:
        payload = exc.payload if isinstance(exc.payload, dict) else {"code": "http_error", "message": f"service returned HTTP {exc.status}"}
        payload.setdefault("state", "error")
        if request_id:
            payload.setdefault("idempotency_key", request_id)
        emit(payload, secrets)
        return remote_exit(exc.status)
    except NetworkFailure:
        if is_write and request_id:
            emit({"state": "outcome_unknown", "code": "network_outcome_unknown", "message": "request outcome is unknown; query operation with the same request ID", "idempotency_key": request_id}, secrets)
            return EXIT_UNKNOWN
        emit({"state": "error", "code": "network_error", "message": "dashboard service is unavailable"}, secrets)
        return EXIT_NETWORK
    except CliError as exc:
        payload = {"state": "error", "code": exc.code, "message": exc.message}
        if request_id:
            payload["idempotency_key"] = request_id
        emit(payload, secrets)
        return exc.exit_code
    except (OSError, ValueError) as exc:
        emit({"state": "error", "code": "local_error", "message": "dashboard CLI could not complete the local operation"}, secrets)
        print(type(exc).__name__, file=sys.stderr)
        return EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())

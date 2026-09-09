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


# contracts.md section 7 exit codes.
EXIT_SUCCESS = 0
EXIT_INPUT = 2
EXIT_AUTH = 3
EXIT_FORBIDDEN = 4
EXIT_CONFLICT = 5
EXIT_PENDING = 6
EXIT_UNKNOWN = 7
EXIT_NETWORK = 8
EXIT_REMOTE = 9
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
WRITE_ACTIONS = {
    "dashboard.publish",
    "dashboard.create",
    "dashboard.activate",
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


def same_version_id(left: str, right: str) -> bool:
    # The service canonicalizes UUID spellings before lookup. Legacy opaque
    # identifiers still require an exact match; never fold their casing.
    try:
        return uuid.UUID(left) == uuid.UUID(right)
    except (ValueError, AttributeError):
        return left == right


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
        if is_reparse(current):
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


class HttpClient:
    def __init__(self, base_url: str, timeout: float, token: str | None = None,
                 session: str | None = None, auth_mode: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.token = token
        self.session = session
        self.auth_mode = auth_mode
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, method: str, path: str, *, body: bytes | None = None, headers=None):
        request_headers = {"Accept": "application/json"}
        if self.token is not None:
            request_headers["Authorization"] = f"Bearer {self.token}"
        if self.auth_mode is not None:
            request_headers["X-Dashboard-Auth-Mode"] = self.auth_mode
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


DEFAULT_TOKEN_FILE = "/root/.config/auth_token"
TOKEN_JSON_KEYS = ("access_token", "token", "id_token", "w3_token")


def load_transport_config(config_path: str, *, require_token_file: bool) -> dict:
    """Fixed deployment config shared by both modes: service_url, workdir,
    token_file, optional timeout_seconds. token_file is mandatory for
    integration; human mode falls back to the environment-provided file."""
    path = Path(config_path).resolve(strict=True)
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CliError("invalid_config", "could not read dashboard config") from exc
    if not isinstance(config, dict):
        raise CliError("invalid_config", "dashboard config must be a JSON object")
    allowed = {"service_url", "workdir", "token_file", "timeout_seconds"}
    if set(config) - allowed or "service_url" not in config or "workdir" not in config:
        raise CliError("invalid_config", "dashboard config fields are invalid")
    if require_token_file and "token_file" not in config:
        raise CliError("invalid_config", "integration config must provide its own token_file")
    config["_base"] = path.parent
    return config


def read_credential_file(path: Path) -> str:
    """Load the credential written by the existing environment.

    The producer owns this file; the CLI only reads it fresh on every
    invocation and never echoes it. Accepts a bare single-line UTF-8 token
    or a JSON object carrying the token under a common field.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CliError("auth_file_missing", "credential file does not exist", EXIT_AUTH) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise CliError("auth_file_unreadable", "credential file cannot be read", EXIT_AUTH) from exc
    text = raw.strip()
    if not text:
        raise CliError("auth_file_empty", "credential file is empty", EXIT_AUTH)
    token = text
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CliError("auth_file_invalid", "credential file is not valid JSON", EXIT_AUTH) from exc
        if not isinstance(parsed, dict):
            raise CliError("auth_file_invalid", "credential file must be an object or a bare token", EXIT_AUTH)
        token = ""
        for key in TOKEN_JSON_KEYS:
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                token = value.strip()
                break
        if not token:
            raise CliError("auth_file_invalid", "credential object has no token field", EXIT_AUTH)
    if len(token) > 16384 or any(character.isspace() for character in token):
        raise CliError("auth_file_invalid", "credential is malformed", EXIT_AUTH)
    return token


def _resolve_config_path(base: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise CliError("invalid_config", f"{label} must be a path")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        return candidate.resolve(strict=True)
    except OSError as exc:
        raise CliError("invalid_config", f"{label} does not exist") from exc


class DirectTransport:
    """Direct HTTP transport shared by both credential modes.

    Subclasses only decide where the credential comes from and which fixed
    auth-mode header is sent; command mapping, upload, freezing and error
    handling are identical. A failed branch never falls back to the other.
    """

    auth_mode = "human"

    def __init__(self, service_url: str, workdir: Path, token: str, timeout: float = 30.0):
        self.origin = validated_origin(service_url)
        self.workdir = workdir
        self.secrets = [token]
        self.client = HttpClient(self.origin, timeout, token=token, auth_mode=self.auth_mode)
        self._principal: dict | None = None

    def principal(self) -> dict:
        """Verified stable principal via /me — required before any write or
        snapshot reuse. The token itself (or its digest) is never the user
        identity: the service resolves the stable principal."""
        if self._principal is None:
            _status, _headers, response = self.client.request("GET", "/api/v1/me")
            payload = parse_json_bytes(response)
            if not isinstance(payload, dict) or not payload.get("principal_id"):
                raise CliError("invalid_response", "service identity response is invalid", EXIT_REMOTE)
            expected = "human" if self.auth_mode == "human" else "service"
            if payload.get("principal_type") != expected:
                raise CliError(
                    "principal_type_mismatch",
                    f"credential is not a {expected} identity on this service",
                    EXIT_AUTH,
                )
            self._principal = {
                "principal_id": payload["principal_id"],
                "principal_type": payload["principal_type"],
            }
        return self._principal

    # -------------------------------------------------- frozen request state

    def _state_dir(self) -> Path:
        """Snapshots are isolated per service origin and verified principal;
        the same user with a renewed token keeps the namespace, a different
        user never reuses it."""
        origin_key = hashlib.sha256(self.origin.encode("utf-8")).hexdigest()[:16]
        principal = self.principal()
        principal_id = principal["principal_id"]
        if not isinstance(principal_id, str) or not principal_id or len(principal_id) > 128 \
                or not all(c.isascii() and (c.isalnum() or c in "-_") for c in principal_id):
            raise CliError("invalid_response", "service principal ID is unsafe", EXIT_REMOTE)
        marker = self.workdir / ".aresclaw-dashboard"
        state = marker / "requests" / origin_key / principal_id
        assert_safe_components(self.workdir, state, True)
        state.mkdir(parents=True, exist_ok=True)
        assert_safe_components(self.workdir, state, True)
        return state

    def _snapshot_path(self, kind: str, request_id: str) -> Path:
        path = self._state_dir() / f"{kind}-{validate_uuid(request_id, 'request-id')}.json"
        assert_safe_components(self.workdir, path, True)
        return path

    def freeze_publish(self, params: dict, request_id: str) -> dict:
        descriptor = {
            "kind": "publish",
            "service_origin": self.origin,
            "principal_id": self.principal()["principal_id"],
            "path": relative_path(params["path"], "file"),
            "title": params.get("title"),
            "description": params.get("description"),
            "disposition": params.get("disposition", "publish"),
            "dashboard_id": params.get("dashboard_id"),
            "expected_revision": params.get("expected_revision"),
        }
        state_path = self._snapshot_path("publish", request_id)
        if state_path.exists():
            # Resume: only the frozen snapshot is needed. The original file
            # may have been moved or deleted — it is NOT re-read (R15).
            try:
                frozen = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CliError("request_state_invalid", "frozen request state is unreadable", EXIT_CONFLICT) from exc
            stored_descriptor = frozen.get("descriptor")
            if isinstance(stored_descriptor, dict):
                # Before draft support every upload was an immediate publish.
                legacy = "disposition" not in stored_descriptor
                stored_descriptor.setdefault("disposition", "publish")
                if legacy and "description" not in params and stored_descriptor.get("description") == "":
                    # Old CLI updates implicitly sent an empty description.
                    # Resume that exact request, without changing new omission
                    # semantics or equating new omitted/explicit-clear writes.
                    descriptor["description"] = ""
                    params = {**params, "description": ""}
            if stored_descriptor != descriptor:
                raise CliError("idempotency_conflict", "request ID was already used with different publish parameters", EXIT_CONFLICT)
            try:
                html = base64.b64decode(frozen["content_base64"], validate=True)
            except (KeyError, ValueError) as exc:
                raise CliError("request_state_invalid", "frozen request state is invalid", EXIT_CONFLICT) from exc
        else:
            html = read_limited(safe_input(self.workdir, params["path"]))
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
            "service_origin": self.origin,
            "principal_id": self.principal()["principal_id"],
            "path": relative_path(path_value, "changes file"),
            "dashboard_id": dashboard_id,
            "expected_revision": expected_revision,
        }
        state_path = self._snapshot_path("access", request_id)
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

    def freeze_group_members(self, params: dict, request_id: str) -> dict:
        """Freeze the FINAL membership replacement request for this
        request-id (R13). A retry after a lost response reuses the frozen
        member list and expected_revision — it never re-reads the group and
        never dies on the revision the first success bumped."""
        descriptor = {
            "kind": "group_member_change",
            "service_origin": self.origin,
            "principal_id": self.principal()["principal_id"],
            "group_id": params["group_id"],
            "user_id": params["user_id"],
            "change": params["change"],
            "expected_revision": params["expected_revision"],
        }
        state_path = self._snapshot_path("group", request_id)
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
                frozen.get("members"), list
            ):
                raise CliError(
                    "idempotency_conflict",
                    "request ID was already used with different group parameters",
                    EXIT_CONFLICT,
                )
            return {"members": frozen["members"],
                    "expected_revision": frozen["expected_revision"]}
        frozen = {"descriptor": descriptor,
                  "members": self._compute_members(params),
                  "expected_revision": params["expected_revision"]}
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
            return self.freeze_group_members(params, request_id)
        return {"members": frozen["members"],
                "expected_revision": frozen["expected_revision"]}

    def _compute_members(self, params: dict) -> list:
        # First run only: membership detail is owner-only, so read the full
        # group (group.show) and CAS-replace. The computed list is frozen
        # above; retries never come through this path.
        response = self.invoke("group.show", {"group_id": params["group_id"]}, None)
        detail = response[0] if isinstance(response, tuple) else response
        if not isinstance(detail, dict) or not isinstance(detail.get("members"), list):
            raise CliError("invalid_response", "group detail response is invalid", EXIT_REMOTE)
        if detail.get("revision") != params["expected_revision"]:
            raise CliError("revision_conflict", "group revision changed; read it again", EXIT_CONFLICT)
        members = list(dict.fromkeys(detail["members"]))
        if params["change"] == "add" and params["user_id"] not in members:
            members.append(params["user_id"])
        if params["change"] == "remove":
            members = [member for member in members if member != params["user_id"]]
        return members

    # -------------------------------------------------- REST command mapping

    def download_source(self, params: dict):
        dashboard_id = params["dashboard_id"]
        version_id = params.get("version_id")
        revision = None
        selected_sha256 = None
        selected_byte_size = None
        if version_id is None:
            selection = params.get("version", "current")
            detail, status = self.invoke("dashboard.show", {"dashboard_id": dashboard_id}, None)
            if status != 200 or not isinstance(detail, dict):
                raise CliError("invalid_response", "dashboard detail response is invalid", EXIT_REMOTE)
            version_id = detail.get(f"{selection}_version_id")
            if version_id is None or version_id == "":
                raise CliError("not_found", f"the {selection} version is unavailable or not visible", EXIT_FORBIDDEN)
            revision = detail.get("revision")
            if not isinstance(version_id, str) or type(revision) is not int or revision < 1:
                raise CliError("invalid_response", "dashboard version metadata is invalid", EXIT_REMOTE)
            selected_sha256 = detail.get(f"{selection}_version_sha256")
            selected_byte_size = detail.get(f"{selection}_version_byte_size")

        # Resolve pointers once. Publishing during this GET must not switch the
        # source, digest, byte count or revision to a different snapshot.
        quote = lambda value: urllib.parse.quote(str(value), safe="")
        path = f"/api/v1/dashboards/{quote(dashboard_id)}/versions/{quote(version_id)}/source"
        status, headers, response = self.client.request("GET", path)
        if status != 200:
            raise CliError("invalid_response", "source download did not return HTTP 200", EXIT_REMOTE)
        if len(response) > MAX_UPLOAD_BYTES:
            raise CliError("file_too_large", "download exceeds the supported size", EXIT_REMOTE)
        digest = hashlib.sha256(response).hexdigest()
        headers = {name.lower(): value for name, value in headers.items()}
        expected_digests = [params.get("expected_sha256"), selected_sha256,
                            headers.get("x-content-sha256")]
        mismatch = any(expected is not None and expected != digest for expected in expected_digests)
        if "x-dashboard-version-id" in headers and not same_version_id(headers["x-dashboard-version-id"], version_id):
            mismatch = True
        if selected_byte_size is not None and (type(selected_byte_size) is not int or selected_byte_size != len(response)):
            mismatch = True
        if mismatch:
            raise CliError("source_integrity_mismatch", "source bytes or version do not match the expected snapshot; no output was created", EXIT_REMOTE)

        # Check integrity before creating even the destination's parent, and
        # retain exclusive creation plus the existing link/escape protection.
        output = safe_output(self.workdir, params["output"])
        with output.open("xb") as handle:
            handle.write(response)
        result = {"state": "succeeded", "dashboard_id": dashboard_id, "version_id": version_id,
                  "sha256": digest, "byte_size": len(response),
                  "output": relative_path(params["output"], "output")}
        if revision is not None:
            result["revision"] = revision
        return result

    def invoke(self, action: str, params: dict, request_id: str | None):
        if action == "dashboard.source":
            return self.download_source(params)
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
        elif action == "dashboard.publish":
            method = "POST"
            html = params["html_bytes"]
            metadata = {"content_sha256": hashlib.sha256(html).hexdigest(), "byte_size": len(html),
                        "disposition": params.get("disposition", "publish")}
            for field in ("title", "description"):
                if field in params:
                    metadata[field] = params[field]
            if params.get("expected_revision") is not None:
                metadata["expected_revision"] = params["expected_revision"]
            path = "/api/v1/dashboards" if not params.get("dashboard_id") else f"/api/v1/dashboards/{quote(params['dashboard_id'])}/versions"
            body, content_type = multipart(metadata, html)
            headers["Content-Type"] = content_type
        elif action == "dashboard.create":
            method, path = "POST", "/api/v1/dashboards/drafts"
            body = json_bytes({"title": params["title"], "description": params.get("description", "")})
        elif action == "dashboard.activate":
            method, path = "POST", f"/api/v1/dashboards/{quote(params['dashboard_id'])}/publish"
            body = json_bytes({"version_id": params["version_id"], "expected_revision": params["expected_revision"]})
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
        elif action == "group.show":
            path = f"/api/v1/groups/{quote(params['group_id'])}"
        elif action == "group.create":
            method, path = "POST", "/api/v1/groups"
            body = json_bytes({"display_name": params["display_name"]})
        elif action == "group.set_members":
            method, path = "PUT", f"/api/v1/groups/{quote(params['group_id'])}/members"
            body = json_bytes({"members": params["members"], "expected_revision": params["expected_revision"]})
        else:
            raise CliError("unsupported_action", "unsupported dashboard action")
        if body is not None and "Content-Type" not in headers:
            headers["Content-Type"] = "application/json"
        status, _response_headers, response = self.client.request(method, path, body=body, headers=headers)
        return parse_json_bytes(response), status


class IntegrationTransport(DirectTransport):
    """Machine-to-machine: an operator-issued service JWT loaded from the
    config's own token file; the fixed service verifier branch."""

    auth_mode = "service"

    def __init__(self, config_path: str):
        config = load_transport_config(config_path, require_token_file=True)
        base = config["_base"]
        self.workdir = _resolve_config_path(base, config["workdir"], "workdir")
        if not self.workdir.is_dir():
            raise CliError("invalid_config", "workdir must be a directory")
        token_path = _resolve_config_path(base, config["token_file"], "token_file")
        if not token_path.is_file() or is_reparse(token_path):
            raise CliError("invalid_config", "token_file must be a regular non-link file")
        token = read_credential_file(token_path)
        try:
            timeout = float(config.get("timeout_seconds", 30))
        except (TypeError, ValueError) as exc:
            raise CliError("invalid_config", "timeout_seconds must be a number") from exc
        if not 0 < timeout <= 300:
            raise CliError("invalid_config", "timeout_seconds must be between 0 and 300")
        super().__init__(str(config["service_url"]), self.workdir, token, timeout)


class HumanTransport(DirectTransport):
    """Default mode: the existing environment pre-writes the current user's
    W3 token (default /root/.config/auth_token); each CLI run loads it
    fresh and calls the public API directly as a human."""

    auth_mode = "human"

    def __init__(self, config_path: str | None):
        service_url = ""
        workdir_value = ""
        token_value = ""
        timeout = 30.0
        if config_path:
            config = load_transport_config(config_path, require_token_file=False)
            base = config["_base"]
            service_url = str(config["service_url"])
            workdir_value = str(config["workdir"])
            token_value = str(config.get("token_file", ""))
            try:
                timeout = float(config.get("timeout_seconds", 30))
            except (TypeError, ValueError) as exc:
                raise CliError("invalid_config", "timeout_seconds must be a number") from exc
            if not 0 < timeout <= 300:
                raise CliError("invalid_config", "timeout_seconds must be between 0 and 300")
        if not service_url:
            service_url = os.environ.get("ARESCLAW_DASHBOARD_SERVICE_URL", "").strip()
        if not service_url:
            raise CliError("invalid_config", "human mode needs service_url from config or ARESCLAW_DASHBOARD_SERVICE_URL")
        if not workdir_value:
            workdir_value = os.environ.get("ARESCLAW_DASHBOARD_WORKDIR", "").strip() or str(Path.cwd())
        workdir = Path(workdir_value).resolve(strict=True)
        if not workdir.is_dir():
            raise CliError("invalid_config", "workdir must be a directory")
        token_path_text = (
            token_value
            or os.environ.get("ARESCLAW_DASHBOARD_TOKEN_FILE", "").strip()
            or DEFAULT_TOKEN_FILE
        )
        token_path = Path(token_path_text)
        if not token_path.is_absolute():
            raise CliError("invalid_config", "credential file path must be absolute")
        token = read_credential_file(token_path)
        super().__init__(service_url, workdir, token, timeout)


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
    parser.add_argument("--auth-mode", choices=["human", "integration"], default="human")
    parser.add_argument("--config")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=JsonParser)
    commands.add_parser("new-request-id")
    item = commands.add_parser("list")
    item.add_argument("--scope", choices=["mine", "shared", "all"], default="mine")
    item.add_argument("--query")
    item.add_argument("--cursor")
    item.add_argument("--status", choices=["draft", "published", "archived"])
    item = commands.add_parser("show")
    item.add_argument("dashboard_id")
    item = commands.add_parser("source")
    item.add_argument("dashboard_id")
    choice = item.add_mutually_exclusive_group()
    choice.add_argument("--version", choices=["current", "draft"], help="Resolve once from dashboard detail (default: current)")
    choice.add_argument("--version-id", help="Download this exact immutable version without resolving a pointer")
    item.add_argument("--expected-sha256", help="Require this exact 64-character lowercase SHA-256")
    item.add_argument("--output", required=True)
    item = commands.add_parser("create", help="Create an empty private draft")
    item.add_argument("--title", required=True)
    item.add_argument("--description", default="")
    add_common_write(item)
    for name in ("save", "publish"):
        item = commands.add_parser(name, help="Save a private draft" if name == "save" else "Upload and publish, or publish a named draft")
        item.add_argument("--file", required=name == "save")
        item.add_argument("--title")
        item.add_argument("--description")
        item.add_argument("--dashboard-id")
        item.add_argument("--expected-revision", type=int)
        if name == "publish":
            item.add_argument("--version-id")
        add_common_write(item)
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
    item = group_commands.add_parser("show")
    item.add_argument("group_id")
    item = group_commands.add_parser("create")
    item.add_argument("--name", required=True)
    add_common_write(item)
    member = group_commands.add_parser("member")
    member_commands = member.add_subparsers(dest="member_command", required=True, parser_class=JsonParser)
    for name in ("add", "remove"):
        item = member_commands.add_parser(name)
        item.add_argument("group_id")
        item.add_argument("--user-id", required=True)
        add_common_write(item, True)
    return parser


def validate_command_arguments(args):
    revision = getattr(args, "expected_revision", None)
    if revision is not None and revision < 1:
        raise CliError("invalid_revision", "expected-revision must be a positive integer")
    if args.command == "source":
        digest = args.expected_sha256
        if digest is not None and (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
            raise CliError("invalid_sha256", "expected-sha256 must contain exactly 64 lowercase hexadecimal characters")
        if args.version_id is not None and not args.version_id:
            raise CliError("invalid_source", "version-id must not be empty")
        relative_path(args.output, "output")
    if args.command not in {"save", "publish"}:
        return
    if bool(args.dashboard_id) != (revision is not None):
        raise CliError("invalid_publish", "dashboard-id and expected-revision must be provided together for updates")
    version_id = getattr(args, "version_id", None)
    if args.file:
        if version_id:
            raise CliError("invalid_publish", "file and version-id are mutually exclusive")
        if not args.dashboard_id and not args.title:
            raise CliError("invalid_publish", "new dashboards require title")
        relative_path(args.file, "file")
    elif not (args.dashboard_id and version_id) or args.title is not None or args.description is not None:
        raise CliError("invalid_publish", "publishing a saved draft requires dashboard-id, version-id and expected-revision, without title/description")


def command_action(args, transport):
    command = args.command
    values = vars(args)
    if command == "list":
        return "dashboard.list", {k: values[k] for k in ("scope", "query", "cursor", "status") if values[k] is not None}
    if command == "show":
        return "dashboard.show", {"dashboard_id": args.dashboard_id}
    if command == "source":
        return "dashboard.source", {"dashboard_id": args.dashboard_id, "version_id": args.version_id,
                                    "version": args.version or "current", "expected_sha256": args.expected_sha256,
                                    "output": args.output}
    if command == "create":
        return "dashboard.create", {"title": args.title, "description": args.description}
    if command in {"save", "publish"}:
        if not args.file:
            return "dashboard.activate", {"dashboard_id": args.dashboard_id,
                    "version_id": args.version_id, "expected_revision": args.expected_revision}
        params = {"path": relative_path(args.file, "file"),
                  "disposition": "save_draft" if command == "save" else "publish"}
        if args.title is not None:
            params["title"] = args.title
        if args.description is not None:
            params["description"] = args.description
        elif not args.dashboard_id:
            params["description"] = ""
        if args.dashboard_id:
            params.update({"dashboard_id": args.dashboard_id, "expected_revision": args.expected_revision})
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
        changes = transport.freeze_access_changes(
            path_value,
            args.dashboard_id,
            args.expected_revision,
            args.request_id,
        )
        return "dashboard.access_apply", {"dashboard_id": args.dashboard_id, "expected_revision": args.expected_revision, "changes": changes}
    if command in {"archive", "restore"}:
        return f"dashboard.{command}", {"dashboard_id": args.dashboard_id, "expected_revision": args.expected_revision}
    if command == "group":
        if args.group_command == "list":
            return "group.list", {"query": args.query, "cursor": args.cursor}
        if args.group_command == "show":
            return "group.show", {"group_id": args.group_id}
        if args.group_command == "create":
            return "group.create", {"display_name": args.name}
        return "group.member_change", {"group_id": args.group_id, "user_id": args.user_id, "expected_revision": args.expected_revision, "change": args.member_command}
    raise CliError("unsupported_command", "unsupported command")


def group_member_change(transport, params: dict, request_id: str):
    # The final membership replacement is frozen under the request-id before
    # the write: a lost response retries the identical request bytes and the
    # server replays the recorded outcome (R13).
    frozen = transport.freeze_group_members(params, request_id)
    return transport.invoke(
        "group.set_members",
        {
            "group_id": params["group_id"],
            "members": frozen["members"],
            "expected_revision": frozen["expected_revision"],
        },
        request_id,
    )


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


# Operation payloads can arrive over HTTP 200 with state=failed; the recorded
# error code carries the semantic class (contracts.md section 7 exit codes).
OPERATION_FAILURE_EXITS = {
    "authentication_required": EXIT_AUTH,
    "invalid_token": EXIT_AUTH,
    "token_expired": EXIT_AUTH,
    "token_revoked": EXIT_AUTH,
    "action_forbidden": EXIT_FORBIDDEN,
    "not_found": EXIT_FORBIDDEN,
    "idempotency_conflict": EXIT_CONFLICT,
    "revision_conflict": EXIT_CONFLICT,
    "invalid_input": EXIT_INPUT,
    "invalid_time": EXIT_INPUT,
    "upload_too_large": EXIT_INPUT,
}


def state_exit(payload: dict, status: int) -> int:
    """Exit code from a parsed business payload: failed maps through its
    recorded error, pending stays pending, anything else succeeded."""
    state = payload.get("state")
    if state == "failed":
        code = (payload.get("error") or {}).get("code")
        return OPERATION_FAILURE_EXITS.get(code, EXIT_REMOTE)
    if status == 202 or state in {"accepted", "processing", "pending", "running"}:
        return EXIT_PENDING
    return EXIT_SUCCESS


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
        if getattr(args, "request_id", None):
            args.request_id = validate_uuid(args.request_id, "request-id")
        validate_command_arguments(args)
        # Captured before any network work: an uncertain death during
        # principal resolution or freezing still classifies as a write with
        # unknown outcome.
        request_id = getattr(args, "request_id", None) if args.command != "operation" else None
        if args.auth_mode == "integration":
            if not args.config:
                raise CliError("config_required", "integration mode requires --config")
            transport = IntegrationTransport(args.config)
        else:
            transport = HumanTransport(args.config)
        secrets = transport.secrets
        action, params = command_action(args, transport)
        if request_id and action == "dashboard.operation":
            request_id = None
        is_write = action in WRITE_ACTIONS or action == "group.member_change"
        if is_write:
            # Verified stable principal before any write or snapshot reuse;
            # also enforces the human/service branch up front.
            transport.principal()
        if action == "group.member_change":
            response = group_member_change(transport, params, request_id)
        else:
            response = transport.invoke(action, params, request_id)
        status = response[1] if isinstance(response, tuple) else 200
        payload = response[0] if isinstance(response, tuple) else response
        if not isinstance(payload, dict):
            raise CliError("invalid_response", "service response must be a JSON object", EXIT_REMOTE)
        if (is_write or action == "dashboard.operation") and payload.get("state") not in {
                "accepted", "processing", "pending", "running", "succeeded", "failed"}:
            raise CliError("invalid_response", "service returned an unrecognized operation state; query the original request ID", EXIT_REMOTE)
        if request_id:
            payload.setdefault("idempotency_key", request_id)
        emit(payload, secrets)
        return state_exit(payload, status)
    except RemoteFailure as exc:
        payload = exc.payload if isinstance(exc.payload, dict) else {"code": "http_error", "message": f"service returned HTTP {exc.status}"}
        payload.setdefault("state", "error")
        if request_id:
            payload.setdefault("idempotency_key", request_id)
        emit(payload, secrets)
        return remote_exit(exc.status)
    except NetworkFailure:
        if is_write or request_id:
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
        if os.environ.get("DASHBOARD_CLI_TRACE"):
            import traceback
            traceback.print_exc()
        print(type(exc).__name__, file=sys.stderr)
        return EXIT_INPUT


if __name__ == "__main__":
    raise SystemExit(main())

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "dashboard_cli.py"


class RecordingServer:
    def __init__(self, responder):
        self.requests = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                owner.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": body,
                    }
                )
                status, headers, response = responder(owner.requests[-1])
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(response)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    pass

            do_GET = _handle
            do_POST = _handle
            do_PATCH = _handle
            do_PUT = _handle
            do_DELETE = _handle

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class DashboardCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workdir = Path(self.temp.name) / "workspace"
        self.workdir.mkdir()
        self.token = "header.payload.secret-jwt-value"
        self.token_file = Path(self.temp.name) / "dashboard.jwt"
        self.token_file.write_text(self.token + "\n", encoding="utf-8")
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.close()
        self.temp.cleanup()

    def server(self, responder):
        server = RecordingServer(responder)
        self.servers.append(server)
        return server

    @staticmethod
    def identity_responder(inner, principal_id="svc-1", principal_type="service"):
        def responder(request):
            if request["path"] == "/api/v1/me":
                body = {"principal_id": principal_id, "principal_type": principal_type,
                        "display_name": "CI", "scopes": ["read", "write", "manage"]}
                return 200, {"Content-Type": "application/json"}, json.dumps(body).encode()
            return inner(request)

        return responder

    def write_config(self, service_url, timeout=2):
        path = Path(self.temp.name) / "dashboard-config.json"
        path.write_text(
            json.dumps(
                {
                    "service_url": service_url,
                    "workdir": str(self.workdir),
                    "token_file": str(self.token_file),
                    "timeout_seconds": timeout,
                }
            ),
            encoding="utf-8",
        )
        return path

    def run_cli(self, *args, config=None, env=None):
        command = [sys.executable, str(CLI)]
        if config is not None:
            command.extend(["--auth-mode", "integration", "--config", str(config)])
        command.extend(args)
        clean_env = os.environ.copy()
        for name in list(clean_env):
            if name.lower().endswith("_proxy"):
                clean_env.pop(name)
        if env:
            clean_env.update(env)
        return subprocess.run(
            command,
            cwd=self.workdir,
            env=clean_env,
            capture_output=True,
            text=True,
            timeout=5,
        )

    def test_publish_sends_exact_frozen_bytes_and_metadata(self):
        html = b"<!doctype html><meta charset=utf-8><h1>weekly \xe2\x9c\x93</h1>"
        (self.workdir / "report.html").write_bytes(html)

        def publish_response(_request):
            result = {
                "operation_id": "10000000-0000-4000-8000-000000000001",
                "state": "succeeded",
                "dashboard_id": "20000000-0000-4000-8000-000000000002",
                "version_id": "30000000-0000-4000-8000-000000000003",
                "revision": 1,
                "view_url": "https://dashboards.example/dashboards/2",
                "sha256": hashlib.sha256(html).hexdigest(),
            }
            return 200, {"Content-Type": "application/json"}, json.dumps(result).encode()

        server = self.server(self.identity_responder(publish_response))
        config = self.write_config(server.url)
        request_id = "40000000-0000-4000-8000-000000000004"
        args = (
            "publish",
            "--file",
            "report.html",
            "--title",
            "Weekly",
            "--description",
            "Status",
            "--request-id",
            request_id,
        )

        first = self.run_cli(*args, config=config)
        (self.workdir / "report.html").write_text("changed", encoding="utf-8")
        second = self.run_cli(*args, config=config)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(first.stdout)["idempotency_key"], request_id)
        writes = [request for request in server.requests if request["method"] == "POST"]
        self.assertEqual(len(writes), 2)
        self.assertEqual(
            [request["path"] for request in server.requests if request["method"] == "GET"],
            ["/api/v1/me", "/api/v1/me"],
        )
        for request in writes:
            self.assertEqual(request["method"], "POST")
            self.assertEqual(request["path"], "/api/v1/dashboards")
            self.assertEqual(request["headers"]["Authorization"], f"Bearer {self.token}")
            self.assertEqual(request["headers"]["Idempotency-Key"], request_id)
            body = request["body"]
            metadata_pos = body.index(b'name="metadata"')
            html_pos = body.index(b'name="html"')
            self.assertLess(metadata_pos, html_pos)
            self.assertIn(html, body)
            self.assertIn(hashlib.sha256(html).hexdigest().encode(), body)
            self.assertIn(str(len(html)).encode(), body)
            self.assertNotIn(b"changed", body)

    def test_timed_out_write_reports_unknown_without_leaking_token(self):
        (self.workdir / "report.html").write_text("<h1>slow</h1>", encoding="utf-8")

        def slow_response(_request):
            time.sleep(0.3)
            return 200, {"Content-Type": "application/json"}, b"{}"

        server = self.server(slow_response)
        config = self.write_config(server.url, timeout=0.05)
        request_id = "50000000-0000-4000-8000-000000000005"
        result = self.run_cli(
            "publish",
            "--file",
            "report.html",
            "--title",
            "Slow",
            "--request-id",
            request_id,
            config=config,
        )

        self.assertEqual(result.returncode, 7)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["state"], "outcome_unknown")
        self.assertEqual(payload["idempotency_key"], request_id)
        self.assertNotIn(self.token, result.stdout + result.stderr)

    def test_access_apply_reuses_frozen_changes_for_same_request(self):
        changes_path = self.workdir / "changes.json"
        original = {
            "changes": [
                {
                    "action": "grant",
                    "subject_type": "group",
                    "subject_id": "engineering",
                    "role": "viewer",
                    "expires_at": "2026-09-12T18:00:00+08:00",
                }
            ]
        }
        changes_path.write_text(json.dumps(original), encoding="utf-8")

        def response(_request):
            return 200, {"Content-Type": "application/json"}, b'{"state":"succeeded"}'

        server = self.server(self.identity_responder(response))
        config = self.write_config(server.url)
        request_id = "60000000-0000-4000-8000-000000000006"
        args = (
            "access-apply",
            "dashboard-1",
            "--file",
            "changes.json",
            "--expected-revision",
            "4",
            "--request-id",
            request_id,
        )
        first = self.run_cli(*args, config=config)
        changes_path.write_text(
            json.dumps({"changes": [{"action": "set_public", "enabled": True}]}),
            encoding="utf-8",
        )
        second = self.run_cli(*args, config=config)

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        writes = [request for request in server.requests if request["method"] == "POST"]
        self.assertEqual(len(writes), 2)
        first_body = json.loads(writes[0]["body"])
        second_body = json.loads(writes[1]["body"])
        self.assertEqual(first_body, second_body)
        self.assertEqual(first_body["changes"][0]["expires_at"], "2026-09-12T10:00:00Z")

    def _human_env(self, server, token_file=None):
        return {
            "ARESCLAW_DASHBOARD_SERVICE_URL": server.url,
            "ARESCLAW_DASHBOARD_WORKDIR": str(self.workdir),
            "ARESCLAW_DASHBOARD_TOKEN_FILE": str(token_file or self.auth_file),
        }

    def test_human_mode_reads_token_file_and_sends_human_bearer(self):
        self.auth_file = Path(self.temp.name) / "auth_token"
        self.auth_file.write_text("w3-fake-token-for-alice\n", encoding="utf-8")

        def response(request):
            if request["path"] == "/api/v1/me":
                body = {"principal_id": "human-1", "principal_type": "human",
                        "display_name": "Alice", "scopes": ["read", "write", "manage"]}
                return 200, {"Content-Type": "application/json"}, json.dumps(body).encode()
            return 200, {"Content-Type": "application/json"}, b'{"state":"succeeded"}'

        server = self.server(response)
        request_id = "70000000-0000-4000-8000-000000000007"
        result = self.run_cli(
            "share", "dashboard-1", "--subject-type", "user", "--subject-id", "u-42",
            "--role", "editor", "--expected-revision", "7", "--request-id", request_id,
            env=self._human_env(server),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        paths = [request["path"] for request in server.requests]
        self.assertEqual(paths, ["/api/v1/me", "/api/v1/dashboards/dashboard-1/grants"])
        identity, share = server.requests
        identity_headers = {k.lower(): v for k, v in identity["headers"].items()}
        share_headers = {k.lower(): v for k, v in share["headers"].items()}
        self.assertEqual(identity_headers["authorization"], "Bearer w3-fake-token-for-alice")
        self.assertEqual(identity_headers["x-dashboard-auth-mode"], "human")
        self.assertEqual(share_headers["x-dashboard-auth-mode"], "human")
        self.assertNotIn("w3-fake-token-for-alice", result.stdout + result.stderr)

    def test_human_mode_rereads_token_file_on_each_invocation(self):
        self.auth_file = Path(self.temp.name) / "auth_token"
        self.auth_file.write_text("w3-token-one\n", encoding="utf-8")
        tokens = []

        def response(request):
            tokens.append(request["headers"]["Authorization"])
            if request["path"] == "/api/v1/me":
                body = {"principal_id": "human-1", "principal_type": "human",
                        "display_name": "Alice", "scopes": []}
                return 200, {"Content-Type": "application/json"}, json.dumps(body).encode()
            return 200, {"Content-Type": "application/json"}, b'{"items":[]}'

        server = self.server(response)
        first = self.run_cli("list", env=self._human_env(server))
        self.auth_file.write_text("w3-token-two\n", encoding="utf-8")
        second = self.run_cli("list", env=self._human_env(server))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(tokens, ["Bearer w3-token-one", "Bearer w3-token-two"])

    def test_human_mode_missing_or_empty_token_file_fails_clean(self):
        self.auth_file = Path(self.temp.name) / "auth_token"
        server = self.server(lambda request: (200, {"Content-Type": "application/json"}, b"{}"))
        missing = self.run_cli("list", env=self._human_env(server))
        self.assertEqual(missing.returncode, 3)
        self.assertEqual(json.loads(missing.stdout)["code"], "auth_file_missing")
        self.auth_file.write_text("   \n", encoding="utf-8")
        empty = self.run_cli("list", env=self._human_env(server))
        self.assertEqual(empty.returncode, 3)
        self.assertEqual(json.loads(empty.stdout)["code"], "auth_file_empty")
        self.assertEqual(len(server.requests), 0)

    def test_human_mode_rejects_service_identity_without_fallback(self):
        self.auth_file = Path(self.temp.name) / "auth_token"
        self.auth_file.write_text("w3-fake-token\n", encoding="utf-8")

        def response(request):
            if request["path"] == "/api/v1/me":
                body = {"principal_id": "svc-9", "principal_type": "service",
                        "display_name": "CI", "scopes": []}
                return 200, {"Content-Type": "application/json"}, json.dumps(body).encode()
            raise AssertionError("no further calls expected")

        server = self.server(response)
        request_id = "70000000-0000-4000-8000-00000000000a"
        result = self.run_cli(
            "archive", "dashboard-1", "--expected-revision", "2",
            "--request-id", request_id,
            env=self._human_env(server),
        )
        self.assertEqual(result.returncode, 3)
        self.assertEqual(json.loads(result.stdout)["code"], "principal_type_mismatch")
        # No write was attempted and the mode never switched to service.
        self.assertEqual([r["path"] for r in server.requests], ["/api/v1/me"])
        modes = [{k.lower(): v for k, v in r["headers"].items()}["x-dashboard-auth-mode"]
                 for r in server.requests]
        self.assertEqual(modes, ["human"])

    def test_snapshots_bind_to_principal_not_token(self):
        self.auth_file = Path(self.temp.name) / "auth_token"
        self.auth_file.write_text("w3-token-a\n", encoding="utf-8")
        report = self.workdir / "report.html"
        request_id = "70000000-0000-4000-8000-00000000000b"
        uploaded = []
        tokens_seen = []

        def me_for(token):
            # token A/B are the same employee (renewal); token C is someone else.
            if token in ("w3-token-a", "w3-token-b"):
                return {"principal_id": "human-1", "principal_type": "human"}
            return {"principal_id": "human-2", "principal_type": "human"}

        def response(request):
            if request["path"] == "/api/v1/me":
                token = request["headers"]["Authorization"].removeprefix("Bearer ")
                body = me_for(token) | {"display_name": "X", "scopes": []}
                return 200, {"Content-Type": "application/json"}, json.dumps(body).encode()
            tokens_seen.append(request["headers"]["Authorization"])
            uploaded.append(request["body"])
            return 201, {"Content-Type": "application/json"}, json.dumps(
                {"operation_id": request_id, "state": "succeeded"}).encode()

        server = self.server(response)
        report.write_text("<html>version-1</html>", encoding="utf-8")
        first = self.run_cli(
            "publish", "--file", "report.html", "--title", "T",
            "--request-id", request_id, env=self._human_env(server))
        self.assertEqual(first.returncode, 0, first.stderr)
        # Same user renews the token; the local file changed after freezing.
        self.auth_file.write_text("w3-token-b\n", encoding="utf-8")
        report.write_text("<html>version-2-tampered</html>", encoding="utf-8")
        second = self.run_cli(
            "publish", "--file", "report.html", "--title", "T",
            "--request-id", request_id, env=self._human_env(server))
        self.assertEqual(second.returncode, 0, second.stderr)
        # The frozen bytes are reused: no re-read of the changed file.
        self.assertIn(b"version-1", uploaded[0])
        self.assertIn(b"version-1", uploaded[1])
        self.assertNotIn(b"tampered", uploaded[1])
        # A different user must not continue the first user's operation.
        self.auth_file.write_text("w3-token-c\n", encoding="utf-8")
        third = self.run_cli(
            "publish", "--file", "report.html", "--title", "T",
            "--request-id", request_id, env=self._human_env(server))
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertIn(b"version-2-tampered", uploaded[2])
        self.assertNotEqual(uploaded[2], uploaded[0])

    def test_http_error_is_json_classified_and_redacted(self):
        def response(_request):
            error = {
                "code": "invalid_token",
                "message": f"rejected {self.token}",
                "trace_id": "trace-1",
            }
            return 401, {"Content-Type": "application/json"}, json.dumps(error).encode()

        server = self.server(response)
        result = self.run_cli("list", config=self.write_config(server.url))

        self.assertEqual(result.returncode, 3)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["code"], "invalid_token")
        self.assertEqual(payload["message"], "rejected [redacted]")
        self.assertNotIn(self.token, result.stdout + result.stderr)

    def test_redirect_is_not_followed(self):
        target = self.server(
            lambda _request: (
                200,
                {"Content-Type": "application/json"},
                b'{"items":[],"next_cursor":null}',
            )
        )
        redirect = self.server(
            lambda _request: (
                302,
                {"Location": target.url + "/stolen"},
                b"",
            )
        )

        result = self.run_cli("list", config=self.write_config(redirect.url))

        self.assertEqual(result.returncode, 9)
        self.assertEqual(len(target.requests), 0)

    def test_source_writes_only_a_new_relative_workdir_file(self):
        content = b"<!doctype html><h1>source</h1>"
        server = self.server(
            lambda _request: (
                200,
                {"Content-Type": "text/plain"},
                content,
            )
        )
        config = self.write_config(server.url)
        result = self.run_cli(
            "source",
            "dashboard-1",
            "--version-id",
            "version-2",
            "--output",
            "downloads/source.html",
            config=config,
        )
        escaped = self.run_cli(
            "source",
            "dashboard-1",
            "--version-id",
            "version-2",
            "--output",
            "../escape.html",
            config=config,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.workdir / "downloads" / "source.html").read_bytes(), content)
        self.assertEqual(escaped.returncode, 2)
        self.assertFalse((self.workdir.parent / "escape.html").exists())

    def test_group_member_add_reads_then_updates_with_expected_revision(self):
        def inner(request):
            if request["method"] == "GET":
                body = {
                    "id": "group-1",
                    "display_name": "Engineering",
                    "members": ["u-1"],
                    "revision": 3,
                }
            else:
                body = {"state": "succeeded", "revision": 4}
            return 200, {"Content-Type": "application/json"}, json.dumps(body).encode()

        server = self.server(self.identity_responder(inner))
        request_id = "80000000-0000-4000-8000-000000000008"
        result = self.run_cli(
            "group",
            "member",
            "add",
            "group-1",
            "--user-id",
            "u-2",
            "--expected-revision",
            "3",
            "--request-id",
            request_id,
            config=self.write_config(server.url),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        business = [r for r in server.requests if r["path"] != "/api/v1/me"]
        self.assertEqual([request["method"] for request in business], ["GET", "PUT"])
        self.assertEqual(business[0]["path"], "/api/v1/groups/group-1")
        update = business[1]
        self.assertEqual(update["path"], "/api/v1/groups/group-1/members")
        self.assertEqual(update["headers"]["Idempotency-Key"], request_id)
        self.assertEqual(
            json.loads(update["body"]),
            {"members": ["u-1", "u-2"], "expected_revision": 3},
        )

    def test_raw_token_argument_is_rejected_without_echoing_value(self):
        supplied = "do-not-echo-this-token"
        result = self.run_cli("--token", supplied, "list")

        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["code"], "raw_token_forbidden")
        self.assertNotIn(supplied, result.stdout + result.stderr)

    def test_all_remaining_business_commands_map_to_http_contract(self):
        def response(_request):
            return 200, {"Content-Type": "application/json"}, b'{"state":"succeeded"}'

        server = self.server(self.identity_responder(response))
        config = self.write_config(server.url)
        request_ids = [
            f"90000000-0000-4000-8000-{index:012d}" for index in range(1, 10)
        ]
        cases = [
            (("list", "--scope", "shared", "--query", "week"), "GET", "/api/v1/dashboards?scope=shared&q=week"),
            (("show", "dash"), "GET", "/api/v1/dashboards/dash"),
            (("operation", "--operation-id", "op"), "GET", "/api/v1/operations/op"),
            (("operation", "--request-id", request_ids[0]), "GET", f"/api/v1/operations?request_id={request_ids[0]}"),
            (("rename", "dash", "--title", "New", "--expected-revision", "1", "--request-id", request_ids[1]), "PATCH", "/api/v1/dashboards/dash"),
            (("versions", "dash", "--cursor", "next"), "GET", "/api/v1/dashboards/dash/versions?cursor=next"),
            (("rollback", "dash", "--version-id", "v1", "--expected-revision", "2", "--request-id", request_ids[2]), "POST", "/api/v1/dashboards/dash/rollback"),
            (("principals", "--type", "group", "--query", "eng"), "GET", "/api/v1/principals?type=group&q=eng"),
            (("grants", "dash"), "GET", "/api/v1/dashboards/dash/grants"),
            (("share", "dash", "--subject-type", "group", "--subject-id", "g1", "--role", "viewer", "--expected-revision", "3", "--request-id", request_ids[3]), "POST", "/api/v1/dashboards/dash/grants"),
            (("revoke", "dash", "--subject-type", "group", "--subject-id", "g1", "--expected-revision", "4", "--request-id", request_ids[4]), "DELETE", "/api/v1/dashboards/dash/grants/group/g1?expected_revision=4"),
            (("public", "dash", "--disable", "--expected-revision", "5", "--request-id", request_ids[5]), "PUT", "/api/v1/dashboards/dash/public-access"),
            (("access", "dash", "--subject-id", "u1"), "GET", "/api/v1/dashboards/dash/access?subject_id=u1"),
            (("archive", "dash", "--expected-revision", "6", "--request-id", request_ids[6]), "POST", "/api/v1/dashboards/dash/archive"),
            (("restore", "dash", "--expected-revision", "7", "--request-id", request_ids[7]), "POST", "/api/v1/dashboards/dash/restore"),
            (("group", "list", "--query", "eng"), "GET", "/api/v1/groups?q=eng"),
            (("group", "create", "--name", "Local", "--request-id", request_ids[8]), "POST", "/api/v1/groups"),
        ]

        for args, _method, _path in cases:
            result = self.run_cli(*args, config=config)
            self.assertEqual(result.returncode, 0, (args, result.stdout, result.stderr))

        business = [r for r in server.requests if r["path"] != "/api/v1/me"]
        self.assertEqual(len(business), len(cases))
        for request, (_args, method, path) in zip(business, cases):
            self.assertEqual(request["method"], method)
            self.assertEqual(request["path"], path)

    def test_pending_response_has_distinct_exit_code_and_request_id(self):
        server = self.server(
            self.identity_responder(lambda _request: (
                202,
                {"Content-Type": "application/json"},
                b'{"state":"pending","operation_id":"op-1"}',
            ))
        )
        request_id = "a0000000-0000-4000-8000-000000000001"
        result = self.run_cli(
            "archive",
            "dash",
            "--expected-revision",
            "1",
            "--request-id",
            request_id,
            config=self.write_config(server.url),
        )

        self.assertEqual(result.returncode, 6)
        self.assertEqual(json.loads(result.stdout)["idempotency_key"], request_id)

    # ------------------------------------------------- R13/R14/R15 fixes

    def test_publish_retry_survives_deleted_source_file(self):
        """R15: after freezing, the original HTML file is only needed for the
        FIRST run; retries replay the frozen bytes even if the file is gone."""
        html = b"<h1>snapshot-only</h1>"
        (self.workdir / "gone.html").write_bytes(html)

        def publish_response(_request):
            return 200, {"Content-Type": "application/json"}, json.dumps({
                "operation_id": "10000000-0000-4000-8000-000000000011",
                "state": "succeeded",
                "dashboard_id": "20000000-0000-4000-8000-000000000012",
                "version_id": "30000000-0000-4000-8000-000000000013",
                "revision": 1,
            }).encode()

        server = self.server(self.identity_responder(publish_response))
        config = self.write_config(server.url)
        request_id = "40000000-0000-4000-8000-000000000014"
        args = ("publish", "--file", "gone.html", "--title", "T",
                "--request-id", request_id)
        first = self.run_cli(*args, config=config)
        self.assertEqual(first.returncode, 0, first.stderr)
        (self.workdir / "gone.html").unlink()

        second = self.run_cli(*args, config=config)
        self.assertEqual(second.returncode, 0, second.stderr)
        body = json.loads(second.stdout)
        self.assertEqual(body["state"], "succeeded")
        posts = [r for r in server.requests if r["method"] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertIn(html, posts[-1]["body"])

    def test_group_member_retry_replays_frozen_request(self):
        """R13: a lost-response retry of the same group-member command must
        reuse the frozen member list and revision (same key, same bytes) —
        not re-read the group and die on the bumped revision."""
        group_id = "70000000-0000-4000-8000-000000000001"
        user_id = "80000000-0000-4000-8000-000000000002"
        shows = {"n": 0}

        def responder(request):
            if request["path"] == f"/api/v1/groups/{group_id}":
                shows["n"] += 1
                # First read matches the caller's expected revision; ANY
                # later read (there must be none) would see revision 99.
                revision = 1 if shows["n"] == 1 else 99
                return 200, {"Content-Type": "application/json"}, json.dumps({
                    "id": group_id, "display_name": "G",
                    "owner_principal_id": "svc-1", "revision": revision,
                    "members": [] if shows["n"] == 1 else [user_id],
                }).encode()
            if request["method"] == "PUT":
                return 200, {"Content-Type": "application/json"}, json.dumps({
                    "operation_id": "10000000-0000-4000-8000-000000000021",
                    "state": "succeeded",
                    "group_id": group_id, "revision": 2,
                }).encode()
            raise AssertionError(f"unexpected request {request}")

        server = self.server(self.identity_responder(responder))
        config = self.write_config(server.url)
        request_id = "40000000-0000-4000-8000-000000000022"
        args = ("group", "member", "add", group_id, "--user-id", user_id,
                "--expected-revision", "1", "--request-id", request_id)

        first = self.run_cli(*args, config=config)
        self.assertEqual(first.returncode, 0, first.stderr)
        # The response to the first PUT is "lost" (simulated by the retry);
        # a plain re-run with the SAME command and key must recover.
        second = self.run_cli(*args, config=config)
        self.assertEqual(second.returncode, 0, second.stderr)
        body = json.loads(second.stdout)
        self.assertEqual(body["state"], "succeeded")
        puts = [r for r in server.requests if r["method"] == "PUT"]
        self.assertEqual(len(puts), 2)
        self.assertEqual(puts[0]["headers"]["Idempotency-Key"], request_id)
        self.assertEqual(puts[1]["headers"]["Idempotency-Key"], request_id)
        self.assertEqual(puts[0]["body"], puts[1]["body"],
                         "retry must send the identical frozen request")
        # The retry must not even re-read the group (frozen request, no GET).
        shows = [r for r in server.requests
                 if r["path"] == f"/api/v1/groups/{group_id}"]
        self.assertEqual(len(shows), 1)

    def test_operation_query_failed_maps_to_nonzero_exit(self):
        """R14: HTTP 200 + state=failed is NOT success: the exit code reflects
        the recorded failure."""
        def responder(request):
            if request["path"].startswith("/api/v1/operations"):
                return 200, {"Content-Type": "application/json"}, json.dumps({
                    "operation_id": "10000000-0000-4000-8000-000000000031",
                    "request_id": "40000000-0000-4000-8000-000000000032",
                    "state": "failed",
                    "result": None,
                    "error": {"code": "operation_interrupted",
                              "message": "interrupted", "retryable": True},
                }).encode()
            raise AssertionError(f"unexpected {request}")

        server = self.server(self.identity_responder(responder))
        config = self.write_config(server.url)
        result = self.run_cli("operation", "--request-id",
                              "40000000-0000-4000-8000-000000000032",
                              config=config)
        self.assertEqual(json.loads(result.stdout)["state"], "failed")
        self.assertEqual(result.returncode, 9)

    def test_operation_query_failed_conflict_maps_to_conflict_exit(self):
        def responder(request):
            if request["path"].startswith("/api/v1/operations"):
                return 200, {"Content-Type": "application/json"}, json.dumps({
                    "operation_id": "10000000-0000-4000-8000-000000000041",
                    "request_id": "40000000-0000-4000-8000-000000000042",
                    "state": "failed", "result": None,
                    "error": {"code": "revision_conflict",
                              "message": "changed", "retryable": False},
                }).encode()
            raise AssertionError(f"unexpected {request}")

        server = self.server(self.identity_responder(responder))
        config = self.write_config(server.url)
        result = self.run_cli("operation", "--request-id",
                              "40000000-0000-4000-8000-000000000042",
                              config=config)
        self.assertEqual(result.returncode, 5)

    def test_operation_query_pending_stays_pending_exit(self):
        def responder(request):
            if request["path"].startswith("/api/v1/operations"):
                return 200, {"Content-Type": "application/json"}, json.dumps({
                    "operation_id": "10000000-0000-4000-8000-000000000051",
                    "request_id": "40000000-0000-4000-8000-000000000052",
                    "state": "processing", "result": None, "error": None,
                }).encode()
            raise AssertionError(f"unexpected {request}")

        server = self.server(self.identity_responder(responder))
        config = self.write_config(server.url)
        result = self.run_cli("operation", "--request-id",
                              "40000000-0000-4000-8000-000000000052",
                              config=config)
        self.assertEqual(result.returncode, 6)


if __name__ == "__main__":
    unittest.main()

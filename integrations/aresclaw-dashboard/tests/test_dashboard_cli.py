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
        clean_env.pop("ARESCLAW_DASHBOARD_BRIDGE_URL", None)
        clean_env.pop("ARESCLAW_DASHBOARD_SESSION_HANDLE", None)
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

        server = self.server(publish_response)
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
        self.assertEqual(len(server.requests), 2)
        for request in server.requests:
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

        server = self.server(response)
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
        self.assertEqual(len(server.requests), 2)
        first_body = json.loads(server.requests[0]["body"])
        second_body = json.loads(server.requests[1]["body"])
        self.assertEqual(first_body, second_body)
        self.assertEqual(first_body["changes"][0]["expires_at"], "2026-09-12T10:00:00Z")

    def test_conversation_mode_uses_session_bound_invoke_contract(self):
        session = "opaque-session-handle"

        def response(_request):
            return 200, {"Content-Type": "application/json"}, b'{"state":"succeeded","revision":8}'

        server = self.server(response)
        request_id = "70000000-0000-4000-8000-000000000007"
        result = self.run_cli(
            "share",
            "dashboard-1",
            "--subject-type",
            "user",
            "--subject-id",
            "u-42",
            "--role",
            "editor",
            "--expires-at",
            "2026-09-12T18:00:00+08:00",
            "--expected-revision",
            "7",
            "--request-id",
            request_id,
            env={
                "ARESCLAW_DASHBOARD_BRIDGE_URL": server.url,
                "ARESCLAW_DASHBOARD_SESSION_HANDLE": session,
            },
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(server.requests), 1)
        request = server.requests[0]
        self.assertEqual(request["path"], "/invoke")
        headers = {name.lower(): value for name, value in request["headers"].items()}
        self.assertEqual(headers["x-aresclaw-dashboard-session"], session)
        self.assertNotIn("authorization", headers)
        payload = json.loads(request["body"])
        self.assertEqual(payload["action"], "dashboard.share")
        self.assertEqual(payload["request_id"], request_id)
        self.assertEqual(payload["params"]["expires_at"], "2026-09-12T10:00:00Z")
        self.assertNotIn(session, result.stdout + result.stderr)

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
        def response(request):
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

        server = self.server(response)
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
        self.assertEqual([request["method"] for request in server.requests], ["GET", "PUT"])
        self.assertEqual(server.requests[0]["path"], "/api/v1/groups/group-1")
        update = server.requests[1]
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
            return 200, {"Content-Type": "application/json"}, b"{}"

        server = self.server(response)
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

        self.assertEqual(len(server.requests), len(cases))
        for request, (_args, method, path) in zip(server.requests, cases):
            self.assertEqual(request["method"], method)
            self.assertEqual(request["path"], path)

    def test_pending_response_has_distinct_exit_code_and_request_id(self):
        server = self.server(
            lambda _request: (
                202,
                {"Content-Type": "application/json"},
                b'{"state":"pending","operation_id":"op-1"}',
            )
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


if __name__ == "__main__":
    unittest.main()

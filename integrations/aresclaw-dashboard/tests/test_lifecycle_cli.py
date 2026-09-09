"""Human and integration command contracts for independent draft/live versions."""
from email import message_from_bytes
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest

import test_dashboard_cli as shared


def multipart_parts(request):
    message = message_from_bytes(("Content-Type: " + request["headers"]["Content-Type"]
                                  + "\r\n\r\n").encode() + request["body"])
    return [(part.get_param("name", header="content-disposition"), part.get_payload(decode=True))
            for part in message.get_payload()]


class DashboardLifecycleCliTests(unittest.TestCase):
    setUp = shared.DashboardCliTests.setUp
    tearDown = shared.DashboardCliTests.tearDown
    server = shared.DashboardCliTests.server
    write_config = shared.DashboardCliTests.write_config
    run_cli = shared.DashboardCliTests.run_cli

    def config(self):
        def respond(request):
            return 200, {"Content-Type": "application/json"}, json.dumps({
                "state": "succeeded", "result": {"dashboard_id": "board-1", "revision": 2}
            }).encode()
        server = self.server(shared.DashboardCliTests.identity_responder(respond))
        return server, self.write_config(server.url)

    def test_create_is_metadata_only_and_does_not_upload_or_publish(self):
        server, config = self.config()
        result = self.run_cli("create", "--title", "Draft", "--request-id",
                              "a0000000-0000-4000-8000-000000000001", config=config)
        self.assertEqual(result.returncode, 0, result.stdout)
        writes = [r for r in server.requests if r["method"] == "POST"]
        self.assertEqual(writes[0]["path"], "/api/v1/dashboards/drafts")
        self.assertEqual(json.loads(writes[0]["body"]), {"title": "Draft", "description": ""})

    def test_save_freezes_draft_disposition_and_survives_deleted_source(self):
        server, config = self.config()
        (self.workdir / "report.html").write_text("<h1>draft v2</h1>", encoding="utf-8")
        args = ("save", "--file", "report.html", "--dashboard-id", "board-1",
                "--expected-revision", "4", "--request-id", "a0000000-0000-4000-8000-000000000002")
        first = self.run_cli(*args, config=config)
        (self.workdir / "report.html").unlink()
        second = self.run_cli(*args, config=config)
        self.assertEqual(first.returncode, 0, first.stdout)
        self.assertEqual(second.returncode, 0, second.stdout)
        writes = [r for r in server.requests if r["method"] == "POST"]
        self.assertEqual(len(writes), 2)
        self.assertEqual(multipart_parts(writes[0]), multipart_parts(writes[1]))
        self.assertIn(b'"disposition":"save_draft"', writes[0]["body"])
        self.assertNotIn(b'"description":', writes[0]["body"])
        self.assertEqual(writes[0]["path"], "/api/v1/dashboards/board-1/versions")

    def test_publish_named_draft_is_one_json_operation_without_file(self):
        server, config = self.config()
        result = self.run_cli("publish", "--dashboard-id", "board-1", "--version-id", "draft-2",
                              "--expected-revision", "4", "--request-id",
                              "a0000000-0000-4000-8000-000000000003", config=config)
        self.assertEqual(result.returncode, 0, result.stdout)
        writes = [r for r in server.requests if r["method"] == "POST"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]["path"], "/api/v1/dashboards/board-1/publish")
        self.assertEqual(json.loads(writes[0]["body"]),
                         {"version_id": "draft-2", "expected_revision": 4})

    def test_update_and_publish_preserves_unspecified_metadata(self):
        server, config = self.config()
        (self.workdir / "report.html").write_text("<h1>live v2</h1>", encoding="utf-8")
        result = self.run_cli("publish", "--file", "report.html", "--dashboard-id", "board-1",
                              "--expected-revision", "4", "--request-id",
                              "a0000000-0000-4000-8000-000000000004", config=config)
        self.assertEqual(result.returncode, 0, result.stdout)
        body = next(r["body"] for r in server.requests if r["method"] == "POST")
        self.assertNotIn(b'"title":', body)
        self.assertNotIn(b'"description":', body)
        self.assertIn(b'"disposition":"publish"', body)

    def test_invalid_request_id_precedes_missing_credential_config(self):
        result = self.run_cli("publish", "--file", "missing.html", "--title", "Draft",
                              "--request-id", "../invalid", config=Path(self.temp.name) / "missing.json")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stdout)["code"], "invalid_uuid")
        self.assertFalse((self.workdir / ".aresclaw-dashboard").exists())

    def test_request_uuid_canonicalization_happens_before_snapshot_selection(self):
        server, config = self.config()
        (self.workdir / "report.html").write_text("<h1>original</h1>", encoding="utf-8")
        key = "aabcdef0-0000-4000-8000-000000000001"
        args = ("publish", "--file", "report.html", "--title", "Report", "--request-id")
        first = self.run_cli(*args, "{" + key.upper() + "}", config=config)
        (self.workdir / "report.html").unlink()
        second = self.run_cli(*args, key, config=config)
        self.assertEqual(first.returncode, 0, first.stdout)
        self.assertEqual(second.returncode, 0, second.stdout)
        writes = [r for r in server.requests if r["method"] == "POST"]
        self.assertEqual(multipart_parts(writes[0]), multipart_parts(writes[1]))
        self.assertEqual(writes[0]["headers"]["Idempotency-Key"], key)

    def test_save_then_publish_cannot_reuse_same_upload_key(self):
        server, config = self.config()
        (self.workdir / "report.html").write_text("<h1>candidate</h1>", encoding="utf-8")
        args = ("--file", "report.html", "--title", "Report", "--request-id",
                "a0000000-0000-4000-8000-000000000007")
        first = self.run_cli("save", *args, config=config)
        second = self.run_cli("publish", *args, config=config)
        self.assertEqual(first.returncode, 0, first.stdout)
        self.assertEqual(second.returncode, 5, second.stdout)
        self.assertEqual(json.loads(second.stdout)["code"], "idempotency_conflict")
        self.assertEqual(len([r for r in server.requests if r["method"] == "POST"]), 1)

    def test_snapshot_nested_directory_link_is_rejected(self):
        server, config = self.config()
        marker = self.workdir / ".aresclaw-dashboard"
        marker.mkdir()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        try:
            (marker / "requests").symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            linked = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J",
                                      str(marker / "requests"), str(outside)], capture_output=True)
            self.assertEqual(linked.returncode, 0, linked.stderr.decode(errors="replace"))
        (self.workdir / "report.html").write_text("<h1>private</h1>", encoding="utf-8")
        result = self.run_cli("publish", "--file", "report.html", "--title", "Report",
                              "--request-id", "a0000000-0000-4000-8000-000000000008", config=config)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(json.loads(result.stdout)["code"], "unsafe_path")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse(any(r["method"] == "POST" for r in server.requests))

    def test_invalid_publish_combination_makes_no_network_request(self):
        server, config = self.config()
        result = self.run_cli("publish", "--dashboard-id", "board-1", "--expected-revision", "4",
                              "--request-id", "a0000000-0000-4000-8000-000000000009", config=config)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(server.requests, [])

    def test_write_with_unrecognized_operation_state_is_not_success(self):
        server = self.server(shared.DashboardCliTests.identity_responder(
            lambda _: (200, {"Content-Type": "application/json"}, b'{"state":"unexpected"}')))
        result = self.run_cli("create", "--title", "Draft", "--request-id",
                              "a0000000-0000-4000-8000-000000000010", config=self.write_config(server.url))
        self.assertEqual(result.returncode, 9, result.stdout)
        self.assertEqual(json.loads(result.stdout)["code"], "invalid_response")

    def test_legacy_update_snapshot_preserves_original_implicit_empty_description(self):
        server, config = self.config()
        key = "a0000000-0000-4000-8000-000000000011"
        state = self.workdir / ".aresclaw-dashboard" / "requests" / hashlib.sha256(server.url.encode()).hexdigest()[:16] / "svc-1"
        state.mkdir(parents=True)
        snapshot = {"descriptor": {"kind": "publish", "service_origin": server.url,
                    "principal_id": "svc-1", "path": "deleted.html", "title": "Original",
                    "description": "", "dashboard_id": "board-1", "expected_revision": 4},
                    "content_base64": base64.b64encode(b"<h1>frozen legacy</h1>").decode()}
        (state / f"publish-{key}.json").write_text(json.dumps(snapshot), encoding="utf-8")
        result = self.run_cli("publish", "--file", "deleted.html", "--title", "Original",
                              "--dashboard-id", "board-1", "--expected-revision", "4",
                              "--request-id", key, config=config)
        self.assertEqual(result.returncode, 0, result.stdout)
        write = next(r for r in server.requests if r["method"] == "POST")
        metadata = json.loads(multipart_parts(write)[0][1])
        self.assertEqual(metadata["description"], "")
        self.assertEqual(multipart_parts(write)[1][1], b"<h1>frozen legacy</h1>")

    def test_new_snapshot_omitted_description_is_not_explicit_clear(self):
        server, config = self.config()
        (self.workdir / "report.html").write_text("<h1>candidate</h1>", encoding="utf-8")
        args = ("publish", "--file", "report.html", "--dashboard-id", "board-1",
                "--expected-revision", "4", "--request-id", "a0000000-0000-4000-8000-000000000012")
        first = self.run_cli(*args, config=config)
        second = self.run_cli(*args, "--description", "", config=config)
        self.assertEqual(first.returncode, 0, first.stdout)
        self.assertEqual(second.returncode, 5, second.stdout)


if __name__ == "__main__":
    unittest.main()

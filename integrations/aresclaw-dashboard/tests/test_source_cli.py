"""Pinned source downloads and byte-level integrity against real HTTP responses."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import unittest

import test_dashboard_cli as shared


class DashboardSourceCliTests(unittest.TestCase):
    setUp = shared.DashboardCliTests.setUp
    tearDown = shared.DashboardCliTests.tearDown
    server = shared.DashboardCliTests.server
    write_config = shared.DashboardCliTests.write_config
    run_cli = shared.DashboardCliTests.run_cli

    content = "<!doctype html>\r\n<h1>周报 ✓</h1>\n".encode("utf-8")

    def detail(self):
        return {"id": "board-1", "revision": 7,
                "current_version_id": "live-2", "draft_version_id": "draft-3",
                "current_version_sha256": hashlib.sha256(self.content).hexdigest(),
                "draft_version_sha256": hashlib.sha256(self.content).hexdigest(),
                "current_version_byte_size": len(self.content),
                "draft_version_byte_size": len(self.content)}

    def source_server(self, *, detail=None, headers=None, content=None):
        snapshot = self.detail() if detail is None else detail

        def respond(request):
            if request["path"] == "/api/v1/dashboards/board-1":
                response = json.dumps(snapshot).encode()
                # A publish after this read must not move the download target.
                snapshot["current_version_id"] = "live-4"
                snapshot["revision"] = 8
                return 200, {"Content-Type": "application/json"}, response
            return 200, {"Content-Type": "text/plain", **(headers or {})}, self.content if content is None else content

        server = self.server(respond)
        return server, self.write_config(server.url)

    def test_default_current_reads_once_and_downloads_pinned_version(self):
        server, config = self.source_server()
        result = self.run_cli("source", "board-1", "--output", "downloads/current.html", config=config)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([r["path"] for r in server.requests], [
            "/api/v1/dashboards/board-1", "/api/v1/dashboards/board-1/versions/live-2/source"])
        self.assertEqual(json.loads(result.stdout), {
            "state": "succeeded", "dashboard_id": "board-1", "version_id": "live-2",
            "sha256": hashlib.sha256(self.content).hexdigest(), "byte_size": len(self.content),
            "output": "downloads/current.html", "revision": 7})
        self.assertEqual((self.workdir / "downloads/current.html").read_bytes(), self.content)

    def test_explicit_current_and_draft_select_independent_pointers(self):
        for selection, expected in [("current", "live-2"), ("draft", "draft-3")]:
            with self.subTest(selection=selection):
                server, config = self.source_server()
                result = self.run_cli("source", "board-1", "--version", selection,
                                      "--output", selection + ".html", config=config)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(json.loads(result.stdout)["version_id"], expected)
                self.assertEqual(json.loads(result.stdout)["revision"], 7)
                self.assertEqual(len(server.requests), 2)
                self.assertTrue(server.requests[1]["path"].endswith(f"/{expected}/source"))

    def test_explicit_id_uses_one_get_and_returns_exact_digest_without_new_headers(self):
        server, config = self.source_server()
        result = self.run_cli("source", "board-1", "--version-id", "historical-1",
                              "--expected-sha256", hashlib.sha256(self.content).hexdigest(),
                              "--output", "historical.html", config=config)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([r["path"] for r in server.requests], [
            "/api/v1/dashboards/board-1/versions/historical-1/source"])
        payload = json.loads(result.stdout)
        self.assertEqual(payload["version_id"], "historical-1")
        self.assertEqual(payload["sha256"], hashlib.sha256(self.content).hexdigest())
        self.assertNotIn("revision", payload)
        self.assertEqual((self.workdir / "historical.html").read_bytes(), self.content)

    def test_source_headers_are_case_insensitive(self):
        server, config = self.source_server(headers={
            "x-content-sha256": hashlib.sha256(self.content).hexdigest(),
            "x-DaShBoArD-vErSiOn-Id": "draft-3"})
        result = self.run_cli("source", "board-1", "--version", "draft",
                              "--output", "draft.html", config=config)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout)["version_id"], "draft-3")
        self.assertEqual(len(server.requests), 2)

    def test_version_header_compares_uuid_identity_not_text_representation(self):
        canonical = "abcdef01-2345-4678-890a-bcdef0123456"
        for index, version in enumerate([canonical.upper(), canonical.replace("-", ""),
                                         "{" + canonical.upper() + "}"]):
            with self.subTest(version=version):
                server, config = self.source_server(headers={"X-Dashboard-Version-Id": canonical})
                result = self.run_cli("source", "board-1", "--version-id", version,
                                      "--output", f"uuid-{index}.html", config=config)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertEqual(json.loads(result.stdout)["sha256"], hashlib.sha256(self.content).hexdigest())
                self.assertEqual(len(server.requests), 1)
        server, config = self.source_server(headers={"X-Dashboard-Version-Id": canonical[:-1] + "7"})
        result = self.run_cli("source", "board-1", "--version-id", canonical.upper(),
                              "--output", "never/different.html", config=config)
        self.assertEqual(result.returncode, 9, result.stdout)
        self.assertEqual(json.loads(result.stdout)["code"], "source_integrity_mismatch")
        self.assertFalse((self.workdir / "never").exists())

    def test_unavailable_or_hidden_pointer_never_requests_source(self):
        for selection in ["current", "draft"]:
            for omitted in [False, True]:
                with self.subTest(selection=selection, omitted=omitted):
                    detail = self.detail()
                    key = selection + "_version_id"
                    detail.pop(key) if omitted else detail.update({key: None})
                    server, config = self.source_server(detail=detail)
                    result = self.run_cli("source", "board-1", "--version", selection,
                                          "--output", "missing.html", config=config)
                    self.assertEqual(result.returncode, 4, result.stdout)
                    self.assertEqual(json.loads(result.stdout)["code"], "not_found")
                    self.assertIn(selection, json.loads(result.stdout)["message"])
                    self.assertEqual(len(server.requests), 1)
                    self.assertFalse((self.workdir / "missing.html").exists())

    def test_bad_expected_hash_is_rejected_before_config_or_network(self):
        server, config = self.source_server()
        for value in ["a" * 63, "a" * 65, "g" * 64, "A" * 64, "", "a" * 63 + "\n"]:
            with self.subTest(value=value):
                result = self.run_cli("source", "board-1", "--version-id", "live-2",
                                      "--expected-sha256", value, "--output", "bad.html", config=config)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertEqual(json.loads(result.stdout)["code"], "invalid_sha256")
        result = self.run_cli("source", "board-1", "--expected-sha256", "bad", "--output", "bad.html",
                              config=Path(self.temp.name) / "missing-config.json")
        self.assertEqual(json.loads(result.stdout)["code"], "invalid_sha256")
        self.assertEqual(server.requests, [])

    def test_version_selector_and_id_are_mutually_exclusive_before_network(self):
        server, config = self.source_server()
        result = self.run_cli("source", "board-1", "--version", "current", "--version-id", "live-2",
                              "--output", "bad.html", config=config)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(server.requests, [])

    def test_response_integrity_mismatches_never_create_output_or_parent(self):
        for headers, expected in [
            ({"x-content-sha256": "0" * 64}, None),
            ({"X-Dashboard-Version-Id": "wrong-version"}, None),
            ({"X-Content-SHA256": "invalid"}, None),
            ({}, "0" * 64),
        ]:
            with self.subTest(headers=headers, expected=expected):
                server, config = self.source_server(headers=headers)
                args = ["source", "board-1", "--version-id", "live-2", "--output", "never/source.html"]
                if expected:
                    args.extend(["--expected-sha256", expected])
                result = self.run_cli(*args, config=config)
                self.assertEqual(result.returncode, 9, result.stdout)
                self.assertEqual(json.loads(result.stdout)["code"], "source_integrity_mismatch")
                self.assertEqual(len(server.requests), 1)
                self.assertFalse((self.workdir / "never").exists())

    def test_selected_metadata_hash_and_size_are_both_verified(self):
        for selection in ["current", "draft"]:
            for suffix, value in [("sha256", "0" * 64), ("byte_size", len(self.content) + 1)]:
                with self.subTest(selection=selection, suffix=suffix):
                    detail = self.detail()
                    detail[selection + "_version_" + suffix] = value
                    server, config = self.source_server(detail=detail)
                    result = self.run_cli("source", "board-1", "--version", selection,
                                          "--output", "never/source.html", config=config)
                    self.assertEqual(result.returncode, 9, result.stdout)
                    self.assertEqual(json.loads(result.stdout)["code"], "source_integrity_mismatch")
                    self.assertEqual(len(server.requests), 2)
                    self.assertFalse((self.workdir / "never").exists())

    def test_hash_compares_exact_bytes_including_final_newline(self):
        server, config = self.source_server(content=self.content.rstrip(b"\n"))
        result = self.run_cli("source", "board-1", "--version-id", "live-2",
                              "--expected-sha256", hashlib.sha256(self.content).hexdigest(),
                              "--output", "never/source.html", config=config)
        self.assertEqual(result.returncode, 9, result.stdout)
        self.assertEqual(json.loads(result.stdout)["code"], "source_integrity_mismatch")
        self.assertFalse((self.workdir / "never").exists())

    def test_download_never_overwrites_existing_file(self):
        existing = self.workdir / "existing.html"
        existing.write_bytes(b"keep original")
        server, config = self.source_server()
        result = self.run_cli("source", "board-1", "--version-id", "live-2",
                              "--output", "existing.html", config=config)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(json.loads(result.stdout)["code"], "output_exists")
        self.assertEqual(existing.read_bytes(), b"keep original")

    def test_download_rejects_directory_links(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        link = self.workdir / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            linked = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(outside)],
                                    capture_output=True)
            self.assertEqual(linked.returncode, 0, linked.stderr.decode(errors="replace"))
        server, config = self.source_server()
        result = self.run_cli("source", "board-1", "--version-id", "live-2",
                              "--output", "linked/source.html", config=config)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertEqual(json.loads(result.stdout)["code"], "unsafe_path")
        self.assertEqual(list(outside.iterdir()), [])

    def test_list_returns_version_metadata_without_reading_each_source(self):
        body = {"items": [self.detail()], "next_cursor": None}
        server = self.server(lambda _: (200, {"Content-Type": "application/json"}, json.dumps(body).encode()))
        result = self.run_cli("list", config=self.write_config(server.url))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(json.loads(result.stdout), body)
        self.assertEqual([r["path"] for r in server.requests], ["/api/v1/dashboards?scope=mine"])


if __name__ == "__main__":
    unittest.main()

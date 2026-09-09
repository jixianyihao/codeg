"""No DB/S3 required: real FastAPI body handling and ACL time boundaries."""
import asyncio
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from dashboard_service.app import RequestGuards, create_control_app
from dashboard_service.config import Config
from dashboard_service.routers.access import _time_window


def config(**overrides):
    return Config.for_testing("mysql+pymysql://unused:unused@127.0.0.1:1/unused",
                              **overrides)


class TimeWindowTests(unittest.TestCase):
    def test_update_one_endpoint_preserves_and_compares_stored_utc(self):
        start, end = _time_window(
            (datetime(2026, 9, 9), datetime(2026, 10, 1)),  # noqa: DTZ001 - MySQL returns naive UTC
            {"expires_at": "2026-10-02T08:00:00+08:00"})
        self.assertEqual(start, datetime(2026, 9, 9, tzinfo=UTC))
        self.assertEqual(end, datetime(2026, 10, 2, tzinfo=UTC))


class BodyLimitTests(unittest.TestCase):
    def test_control_app_returns_standard_error_body(self):
        with patch("dashboard_service.app.build_service", return_value=SimpleNamespace()):
            app = create_control_app(config(), database=object(), verifier=object())

        @app.post("/unit-body")
        def write(payload: dict):
            raise AssertionError("oversized request reached handler")

        response = TestClient(app).post("/unit-body", json={"x": "x" * 140000})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["code"], "upload_too_large")
        self.assertTrue(response.json()["trace_id"])

    def test_multipart_parser_limit_returns_one_response(self):
        app = FastAPI()

        @app.post("/form")
        async def write(request: Request):
            await request.form()
            raise AssertionError("oversized form was accepted")

        app.add_middleware(RequestGuards, config=config(max_upload_bytes=1000))
        response = TestClient(app).post("/form", files={"html": ("large.html", b"x" * 300000)})
        self.assertEqual(response.status_code, 413)

    def test_real_fastapi_emits_exactly_one_response_for_oversized_json(self):
        app = FastAPI()

        @app.post("/write")
        def write(payload: dict):
            raise AssertionError("oversized request reached handler")

        app.add_middleware(RequestGuards, config=config())

        async def run():
            messages = []

            async def receive():
                return {"type": "http.request", "body": b'{"x":"' + b"x" * 140000 + b'"}',
                        "more_body": False}

            async def send(message):
                messages.append(message)

            await app({"type": "http", "asgi": {"version": "3.0"},
                       "http_version": "1.1", "method": "POST", "path": "/write",
                       "raw_path": b"/write", "query_string": b"", "scheme": "http",
                       "headers": [(b"content-type", b"application/json")],
                       "client": ("127.0.0.1", 1), "server": ("test", 80)}, receive, send)
            return messages

        messages = asyncio.run(run())
        self.assertEqual([m["status"] for m in messages
                          if m["type"] == "http.response.start"], [413])
        self.assertEqual(sum(m["type"] == "http.response.body" for m in messages), 1)


class ManagementEntryTests(unittest.TestCase):
    def test_management_link_uses_only_configured_origin(self):
        settings = config(aresclaw_origin="https://aresclaw.example.test/")
        with patch("dashboard_service.app.build_service", return_value=SimpleNamespace()):
            app = create_control_app(settings, database=object(), verifier=object())
        response = TestClient(app).get(
            "/dashboards/00000000-0000-4000-8000-000000000001/manage?return_url=https://evil.test")
        self.assertEqual(response.status_code, 200)
        self.assertIn("https://aresclaw.example.test/workspace?view=dashboards&amp;dashboard=", response.text)
        self.assertNotIn("evil.test", response.text)
        self.assertNotIn("<form", response.text)

    def test_management_origin_rejects_paths_and_credentials(self):
        for value in ("https://ares.test/path", "https://user@ares.test", "//ares.test",
                      "https://ares.test/?next=https://evil.test"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config(aresclaw_origin=value)

    def test_unconfigured_management_root_has_no_forms_or_invented_redirect(self):
        with patch("dashboard_service.app.build_service", return_value=SimpleNamespace()):
            app = create_control_app(config(), database=object(), verifier=object())
        response = TestClient(app).get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<form", response.text)
        self.assertNotIn("/workspace?", response.text)
        self.assertNotIn("location", response.headers)


if __name__ == "__main__":
    unittest.main()

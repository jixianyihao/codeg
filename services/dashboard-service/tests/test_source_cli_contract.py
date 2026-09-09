"""Production CLI mapping against the real ASGI service, MySQL and S3."""
import hashlib
import importlib.util
import uuid
from pathlib import Path

import pytest

from .conftest import requires_mysql

pytestmark = requires_mysql


@pytest.fixture(scope="module")
def cli_module():
    path = (Path(__file__).resolve().parents[3] / "integrations"
            / "aresclaw-dashboard" / "scripts" / "dashboard_cli.py")
    spec = importlib.util.spec_from_file_location("dashboard_contract_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("mode", ["human", "service"])
def test_list_compare_source_and_update_keep_the_version_snapshot(
        client, make_human, make_service_account, tmp_path, cli_module, mode):
    token = (make_human("compare-owner") if mode == "human"
             else make_service_account("compare-job")[1])

    class ServiceHttp:
        # Replace only socket delivery; requests use production CLI encoding,
        # authentication, routing, authorization, database and object reads.
        def request(self, method, path, *, body=None, headers=None):
            response = client.request(method, path, content=body, headers={
                "Authorization": f"Bearer {token}",
                "X-Dashboard-Auth-Mode": mode,
                **(headers or {}),
            })
            if response.status_code >= 400:
                raise cli_module.RemoteFailure(response.status_code, response.json())
            return response.status_code, dict(response.headers), response.content

    transport = cli_module.DirectTransport("http://127.0.0.1:18080", tmp_path, token)
    transport.auth_mode = mode
    transport.client = ServiceHttp()
    assert transport.principal()["principal_type"] == mode

    original = "<!doctype html>\r\n<h1>原始报表</h1>\r\n".encode("utf-8")
    first, _ = transport.invoke("dashboard.publish", {
        "html_bytes": original, "title": "Compare report",
    }, str(uuid.uuid4()))
    did = first["result"]["dashboard_id"]
    page, _ = transport.invoke("dashboard.list", {"scope": "mine"}, None)
    snapshot = next(item for item in page["items"] if item["id"] == did)
    assert snapshot["current_version_sha256"] == hashlib.sha256(original).hexdigest()
    assert snapshot["current_version_byte_size"] == len(original)

    # A second writer updates after the first caller read the listing.
    replacement = b"<!doctype html>\n<h1>Revised report</h1>\n"
    second, _ = transport.invoke("dashboard.publish", {
        "dashboard_id": did, "expected_revision": snapshot["revision"],
        "html_bytes": replacement,
    }, str(uuid.uuid4()))
    assert second["result"]["view_url"] == first["result"]["view_url"]

    args = cli_module.build_parser().parse_args([
        "source", did, "--version-id", snapshot["current_version_id"],
        "--expected-sha256", snapshot["current_version_sha256"],
        "--output", "original.html",
    ])
    action, params = cli_module.command_action(args, transport)
    saved = transport.invoke(action, params, None)
    assert saved["dashboard_id"] == did
    assert saved["version_id"] == snapshot["current_version_id"]
    assert saved["sha256"] == snapshot["current_version_sha256"]
    assert (tmp_path / "original.html").read_bytes() == original

    args = cli_module.build_parser().parse_args([
        "source", did, "--output", "current.html",
    ])
    action, params = cli_module.command_action(args, transport)
    current = transport.invoke(action, params, None)
    assert current["version_id"] == second["result"]["version_id"]
    assert current["sha256"] == hashlib.sha256(replacement).hexdigest()
    assert current["revision"] == second["result"]["revision"]
    assert (tmp_path / "current.html").read_bytes() == replacement

    # UUID text casing does not identify a different immutable version. The
    # source API canonicalizes it before looking up the stored version.
    args = cli_module.build_parser().parse_args([
        "source", did, "--version-id", snapshot["current_version_id"].upper(),
        "--expected-sha256", snapshot["current_version_sha256"],
        "--output", "uppercase.html",
    ])
    action, params = cli_module.command_action(args, transport)
    uppercase = transport.invoke(action, params, None)
    assert uppercase["sha256"] == snapshot["current_version_sha256"]
    assert (tmp_path / "uppercase.html").read_bytes() == original

    # A valid digest cannot bypass the stale revision read for this intent.
    with pytest.raises(cli_module.RemoteFailure) as failure:
        transport.invoke("dashboard.publish", {
            "dashboard_id": did, "expected_revision": snapshot["revision"],
            "html_bytes": original,
        }, str(uuid.uuid4()))
    assert failure.value.status == 409
    assert failure.value.payload["code"] == "revision_conflict"
    after, _ = transport.invoke("dashboard.show", {"dashboard_id": did}, None)
    assert after["current_version_id"] == current["version_id"]
    versions, _ = transport.invoke("dashboard.versions", {"dashboard_id": did}, None)
    assert len(versions["items"]) == 2

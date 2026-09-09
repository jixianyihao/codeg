"""DEV-ONLY W3 verification stub for local end-to-end trials.

Implements the normalized verifier contract consumed by
dashboard_service.authn.HttpW3Verifier. It stands in for the intranet W3 /
Uniportal endpoint on a developer machine so the human path (CLI publish,
page login, view capabilities) can be exercised without real credentials.

NOT part of production deployment:
- accepts any token of the form ``dev-<name>`` (name = enterprise_user_id)
- never used unless an operator explicitly points
  DASHBOARD_W3_VERIFY_URL at this loopback listener

Run:  python devtools/dev_w3_stub.py [port]   (default 8090, 127.0.0.1 only)
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def normalized(token: str) -> dict | None:
    if not token.startswith("dev-") or len(token) > 64:
        return None
    name = token[4:]
    if not name.replace("-", "").replace("_", "").replace(".", "").isalnum():
        return None
    expires = (datetime.now(timezone.utc) + timedelta(hours=8)).isoformat().replace("+00:00", "Z")
    return {
        "active": True,
        "issuer": "w3-dev",
        "user_id": name,
        "display_name": f"{name} (dev)",
        "expires_at": expires,
        "session_ref": f"dev-session-{name}",
    }


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/verify":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        identity = normalized(token)
        if identity is None:
            body = json.dumps({"active": False}).encode()
            self.send_response(401)
        else:
            body = json.dumps(identity).encode()
            self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"DEV W3 stub listening on 127.0.0.1:{port} (tokens: dev-<name>)")
    server.serve_forever()

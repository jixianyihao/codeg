"""Hard request-body caps enforced on the bytes actually received (R6):
chunked multipart without Content-Length, oversized JSON, and honest bodies
that must pass through. Drives the real ASGI middleware directly."""
import asyncio
import json

from dashboard_service.app import RequestGuards
from dashboard_service.config import Config


def _config(**overrides) -> Config:
    return Config.for_testing(
        "mysql+pymysql://user:pass@127.0.0.1:1/unused",  # never connected
        max_upload_bytes=1000, **overrides)


def _run(config, content_type: bytes, messages, content_length=None):
    """Feed raw ASGI messages through RequestGuards; return what the app
    consumed and the responses the middleware emitted itself."""
    seen = {"bodies": [], "completed": False}
    middleware_responses = []

    async def app(scope, receive, send):
        while True:
            message = await receive()
            if message["type"] == "http.request":
                seen["bodies"].append(message.get("body", b""))
                if not message.get("more_body"):
                    break
            else:
                break
        seen["completed"] = True

    async def send(message):
        middleware_responses.append(message)

    queue = list(messages)

    async def receive():
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    headers = [(b"content-type", content_type)]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    scope = {"type": "http", "method": "POST", "path": "/", "headers": headers}
    asyncio.run(RequestGuards(app, config)(scope, receive, send))
    return seen, middleware_responses


def _status(responses) -> int:
    for message in responses:
        if message["type"] == "http.response.start":
            return message["status"]
    return 0


def _json_body(responses) -> dict:
    raw = b"".join(message.get("body", b"") for message in responses
                   if message["type"] == "http.response.body")
    return json.loads(raw.decode())


def test_chunked_multipart_over_limit_rejected_before_parsing():
    config = _config()  # limit = 1000 + 262144 multipart overhead
    limit = config.max_upload_bytes + 262144
    chunk = b"x" * 65536
    messages = [{"type": "http.request", "body": chunk, "more_body": True}
                for _ in range((limit // 65536) + 2)]
    messages.append({"type": "http.request", "body": b"", "more_body": False})
    seen, responses = _run(config, b"multipart/form-data; boundary=x", messages)
    assert _status(responses) == 413
    assert _json_body(responses)["code"] == "upload_too_large"
    # The app never saw more than the limit's worth of bytes — no parser
    # buffering of the oversized stream.
    assert sum(len(body) for body in seen["bodies"]) <= limit
    assert not seen["completed"]


def test_lying_small_content_length_still_cut_by_actual_bytes():
    config = _config()
    limit = config.max_upload_bytes + 262144
    messages = [
        {"type": "http.request", "body": b"x" * 65536, "more_body": True},
        {"type": "http.request", "body": b"x" * ((limit // 65536) * 65536),
         "more_body": False},
    ]
    seen, responses = _run(config, b"multipart/form-data; boundary=x", messages,
                           content_length=100)
    assert _status(responses) == 413
    assert sum(len(body) for body in seen["bodies"]) <= limit


def test_json_without_length_over_limit_rejected():
    config = _config()  # non-multipart limit = 131072
    body = b"{" + b'"a": 1,' * 40000 + b"}"
    assert len(body) > 131072
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    seen, responses = _run(config, b"application/json", messages)
    assert _status(responses) == 413
    assert _json_body(responses)["code"] == "upload_too_large"


def test_json_under_limit_flows_through():
    config = _config()
    body = b'{"ok": true}'
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    seen, responses = _run(config, b"application/json", messages,
                           content_length=len(body))
    assert not responses  # middleware added nothing of its own
    assert seen["completed"]
    assert b"".join(seen["bodies"]) == body


def test_exact_limit_multipart_passes():
    config = _config()
    limit = config.max_upload_bytes + 262144
    messages = [
        {"type": "http.request", "body": b"y" * (limit - 10), "more_body": True},
        {"type": "http.request", "body": b"y" * 10, "more_body": False},
    ]
    seen, responses = _run(config, b"multipart/form-data; boundary=x", messages)
    assert not responses
    assert seen["completed"]
    assert sum(len(body) for body in seen["bodies"]) == limit

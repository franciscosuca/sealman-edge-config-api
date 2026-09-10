import httpx
import pytest
from starlette.requests import Request

from extensions.upstreams import http as http_upstream


def _make_request(method="GET", path="/x", query_string=b"", path_params=None, headers=None, body=b"", scheme="http", client=("203.0.113.5", 12345)):
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": method,
        "scheme": scheme,
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "headers": raw_headers,
        "path_params": path_params or {},
        "client": client,
    }
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def test_resolve_upstream_path_fills_placeholders_from_path_params():
    request = _make_request(path_params={"device_name": "abc123", "unused": "z"})
    result = http_upstream._resolve_upstream_path("/devices/{device_name}/status", request)
    assert result == "/devices/abc123/status"


@pytest.mark.asyncio
async def test_dispatch_streams_response_body_without_buffering(monkeypatch):
    request = _make_request(method="GET", path="/foo", query_string=b"a=1&a=2", headers={"connection": "keep-alive"})

    class FakeStreamResponse:
        status_code = 201
        headers = httpx.Headers({"content-type": "text/plain", "transfer-encoding": "chunked"})

        async def aiter_raw(self):
            yield b"chunk-1-"
            yield b"chunk-2"

        async def aclose(self):
            self.closed = True

    fake_resp = FakeStreamResponse()
    captured = {}

    class FakeClient:
        def build_request(self, method, url, params=None, headers=None, content=None):
            captured["method"] = method
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            return {"method": method, "url": url}

        async def send(self, req, stream=False):
            captured["stream"] = stream
            return fake_resp

    monkeypatch.setattr(http_upstream, "_client", FakeClient())

    upstream = {"base_url": "https://upstream.example.com"}
    route = {"upstream_path": "/foo"}

    response = await http_upstream.dispatch(request, upstream, route)

    assert captured["url"] == "https://upstream.example.com/foo"
    assert captured["stream"] is True
    assert ("a", "1") in captured["params"] and ("a", "2") in captured["params"]
    assert "connection" not in captured["headers"]
    assert response.status_code == 201
    assert "transfer-encoding" not in {k.lower() for k in response.headers.keys()}

    chunks = [chunk async for chunk in response.body_iterator]
    assert chunks == [b"chunk-1-", b"chunk-2"]


@pytest.mark.asyncio
async def test_dispatch_returns_502_on_upstream_connection_error(monkeypatch):
    request = _make_request(method="GET", path="/foo")

    class FakeClient:
        def build_request(self, method, url, params=None, headers=None, content=None):
            return {}

        async def send(self, req, stream=False):
            raise httpx.RequestError("boom")

    monkeypatch.setattr(http_upstream, "_client", FakeClient())

    response = await http_upstream.dispatch(request, {"base_url": "https://down.example.com"}, {"upstream_path": "/foo"})
    assert response.status_code == 502


@pytest.mark.asyncio
async def test_dispatch_adds_forwarding_headers_from_scratch(monkeypatch):
    request = _make_request(scheme="https", client=("198.51.100.9", 4711), headers={"host": "gateway.example.com"})

    class FakeStreamResponse:
        status_code = 200
        headers = httpx.Headers({})

        async def aiter_raw(self):
            return
            yield  # pragma: no cover

        async def aclose(self):
            pass

    captured = {}

    class FakeClient:
        def build_request(self, method, url, params=None, headers=None, content=None):
            captured["headers"] = headers
            return {}

        async def send(self, req, stream=False):
            return FakeStreamResponse()

    monkeypatch.setattr(http_upstream, "_client", FakeClient())

    await http_upstream.dispatch(request, {"base_url": "https://upstream.example.com"}, {"upstream_path": "/foo"})

    assert captured["headers"]["x-forwarded-for"] == "198.51.100.9"
    assert captured["headers"]["x-forwarded-host"] == "gateway.example.com"
    assert captured["headers"]["x-forwarded-proto"] == "https"


@pytest.mark.asyncio
async def test_dispatch_appends_to_a_preexisting_x_forwarded_for(monkeypatch):
    request = _make_request(client=("198.51.100.9", 4711), headers={"x-forwarded-for": "9.9.9.9"})

    class FakeStreamResponse:
        status_code = 200
        headers = httpx.Headers({})

        async def aiter_raw(self):
            return
            yield  # pragma: no cover

        async def aclose(self):
            pass

    captured = {}

    class FakeClient:
        def build_request(self, method, url, params=None, headers=None, content=None):
            captured["headers"] = headers
            return {}

        async def send(self, req, stream=False):
            return FakeStreamResponse()

    monkeypatch.setattr(http_upstream, "_client", FakeClient())

    await http_upstream.dispatch(request, {"base_url": "https://upstream.example.com"}, {"upstream_path": "/foo"})

    # The caller's own claimed value is never discarded, but our hop's own observed
    # address is always appended after it, so a downstream consumer can tell them apart.
    assert captured["headers"]["x-forwarded-for"] == "9.9.9.9, 198.51.100.9"

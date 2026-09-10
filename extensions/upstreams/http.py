"""Reverse-proxy dispatch for `http`-type upstreams: a true streaming pass-through,
built on httpx's own documented streaming-proxy pattern so neither direction is ever
fully buffered in memory, for any content type or size.

`request.stream()` in / `resp.aiter_raw()` out: `aiter_raw()` specifically (not
`aiter_bytes()`/`aiter_text()`) skips httpx's automatic content-decoding, so
gzip/br/zstd-compressed bodies, multipart/form-data and text/event-stream (SSE) all
pass through byte-identical and progressively — this dispatch never needs to
understand the upstream's content type to relay it correctly.
"""

import logging
import re
from typing import Any, Dict, Optional

import httpx
from fastapi import Request
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from constants import ALLOW_INSECURE_HTTPS

logger = logging.getLogger("EdgeConfigAPI")

_PLACEHOLDER_RE = re.compile(r"{([^}]+)}")

# RFC 9110 §7.6.1 hop-by-hop headers, stripped on both legs. `Host` is never forwarded —
# the outbound request always targets the upstream's own host. `Content-Length` is
# intentionally NOT in this set: relaying it verbatim is safe only because this path
# never mutates the body (raw bytes in, raw bytes out); a route type that transforms
# the body must never copy it forward and let the ASGI layer compute framing itself.
# Stripping `Upgrade` also means WebSocket handshakes aren't supported yet — planned
# for a future phase, not this one.
_HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection", "te", "transfer-encoding", "upgrade", "host"}

# One shared client (connection pooling), same pattern as async_requests.py's module-level client.
_client = httpx.AsyncClient(verify=not ALLOW_INSECURE_HTTPS)


def _resolve_upstream_path(upstream_path: str, request: Request) -> str:
    """Fills `{name}` placeholders in `upstream_path` from the mounted route's own,
    already-resolved path parameters. Any placeholder the mounted path doesn't provide
    is left as a KeyError-raising `{name}` — an extension manifest bug, not a runtime one."""
    path_params = request.path_params
    used = set(_PLACEHOLDER_RE.findall(upstream_path))
    return upstream_path.format(**{k: v for k, v in path_params.items() if k in used})


def _add_forwarding_headers(request: Request, headers: Dict[str, str]) -> None:
    """RFC 7239: this dispatch is itself a proxy hop, so it must disclose (via the
    X-Forwarded-* headers real upstreams actually consume, rather than the rarely
    implemented `Forwarded` field) what the hop before it destroys — the original
    client address/host/scheme — instead of silently relaying whatever the caller
    already set, which a caller could otherwise forge to spoof its origin."""
    client_host = request.client.host if request.client else None
    if client_host:
        prior = headers.get("x-forwarded-for")
        headers["x-forwarded-for"] = f"{prior}, {client_host}" if prior else client_host
    original_host = request.headers.get("host")
    if original_host:
        headers["x-forwarded-host"] = original_host
    headers["x-forwarded-proto"] = request.url.scheme


async def dispatch(request: Request, upstream: Dict[str, Any], route: Dict[str, Any]) -> StreamingResponse:
    """Forwards the live request to `upstream['base_url'] + route['upstream_path']` and
    streams the response straight back, never materializing either body in memory."""
    upstream_path = _resolve_upstream_path(route["upstream_path"], request)
    target = upstream["base_url"].rstrip("/") + upstream_path

    forward_headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    _add_forwarding_headers(request, forward_headers)

    req = _client.build_request(
        request.method,
        target,
        params=request.query_params.multi_items(),
        headers=forward_headers,
        content=request.stream(),
    )
    try:
        resp = await _client.send(req, stream=True)
    except httpx.RequestError as exc:
        logger.warning(f"Extension http upstream unreachable: {target} ({exc})")
        return StreamingResponse(
            iter([f'{{"detail":"Upstream error calling {target}: {exc}"}}'.encode()]),
            status_code=502,
            media_type="application/json",
        )

    relayed_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}
    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        headers=relayed_headers,
        background=BackgroundTask(resp.aclose),
    )


async def fetch_body_schema(base_url: str, upstream_path: str, method: str) -> Optional[dict]:
    """`upstream_declared`/`unreachable_ref` support: fetches a route's request-body JSON
    Schema from the extension's own declared `{base_url}/openapi.json` — the only origin
    ever queried (an allow-list of exactly one, the upstream's own already-persisted
    `base_url`; never an arbitrary caller-supplied URL, and `follow_redirects` is never
    enabled) so this can never be turned into a fetch of an attacker-chosen location.

    Returns None (→ `unreachable_ref`) for anything short of a fully resolved schema:
    unreachable host, non-2xx, a malformed OpenAPI document, or `upstream_path`/`method`
    simply not declaring a JSON request body — never raises."""
    target = base_url.rstrip("/") + "/openapi.json"
    try:
        resp = await _client.get(target, timeout=10, follow_redirects=False)
        resp.raise_for_status()
        document = resp.json()
        operation = document["paths"][upstream_path][method.lower()]
        schema = operation["requestBody"]["content"]["application/json"]["schema"]
        ref = schema.get("$ref") if isinstance(schema, dict) else None
        if ref and ref.startswith("#/components/schemas/"):
            schema = document["components"]["schemas"][ref.rsplit("/", 1)[-1]]
        return schema
    except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
        logger.info(f"Could not fetch/resolve body schema from {target} ({method} {upstream_path}): {exc}")
        return None


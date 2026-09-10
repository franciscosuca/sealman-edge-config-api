"""Turns persisted `extension_routes` rows into live FastAPI routes and back.

Current scope: route mounting/unmounting mechanics, the two visibility channels'
auth (`public` via the route's own `required_action` through `ABACPermissionCheck`,
`internal` via `X-Internal-Key`), the dynamic per-manifest request signature
(`extensions/signature.py`), `declared`-mode body validation (`extensions/body_validation.py`),
and real upstream dispatch (`http`/`iotedge`, see `extensions/upstreams/`). The `device`
visibility / field-ingress channel is deliberately **not** implemented — skipped until
explicitly picked back up, not just deferred to a later stage.
"""

import logging
import re
from typing import Any, Callable, Dict, List

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from starlette.responses import Response

from authorization.abac_permission_check import ABACPermissionCheck
from db.repos.extension import ExtensionRepository
from db.session import get_repository
from exceptions import APIError

from . import body_validation, signature
from .security import keys_match
from .upstreams import http as http_upstream
from .upstreams import iotedge as iotedge_upstream

logger = logging.getLogger("EdgeConfigAPI")

_ROUTE_NAME_PREFIX = "extension_route__"
_NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9]+")

_DISPATCHERS: Dict[str, Callable] = {"http": http_upstream.dispatch, "iotedge": iotedge_upstream.dispatch}


def _route_name(extension_name: str, route_id: str) -> str:
    return f"{_ROUTE_NAME_PREFIX}{extension_name}__{route_id}"


def _operation_id(extension_name: str, method: str, path: str) -> str:
    """Deterministic, never user-supplied — `{extension_name}_{method}_{path}` slugified
    — so two extensions (or two routes of one extension, which must already differ in
    method/path to both be routable) never collide on one `operation_id`, which FastAPI
    does not itself detect or reject."""
    slug = _NON_ALNUM_RE.sub("_", path).strip("_")
    return f"{extension_name}_{method.lower()}_{slug}"


def _internal_key_dependency(extension_name: str):
    async def _check(
        x_internal_key: str = Header(..., alias="X-Internal-Key"),
        extension_repo: ExtensionRepository = Depends(get_repository(ExtensionRepository)),
    ) -> None:
        row = await extension_repo.get_extension_row(extension_name)
        if row is None or not keys_match(x_internal_key, row.get("internal_key_hash") or ""):
            raise HTTPException(status_code=401, detail="Invalid X-Internal-Key")

    return _check


def _dispatch_handler(extension_name: str, upstream: Dict[str, Any], route: Dict[str, Any]):
    dispatch = _DISPATCHERS.get(upstream["type"])
    if dispatch is None:
        raise ValueError(f"Unknown upstream type '{upstream['type']}'")

    # Only `declared` mode ever validates a body; `upstream_declared`/`unreachable_ref`/
    # `none` all skip straight to dispatch with no JSON parsing at all.
    body_schema = route.get("body") if route.get("validation_mode") == "declared" else None

    async def _handler(request: Request, **_path_and_query: Any) -> Response:
        # `declared` mode always fully buffers the body to validate it against the
        # route's JSON Schema before dispatch — true zero-buffer streaming and
        # request-body validation are mutually exclusive for the same route, by
        # construction, not an oversight. `dispatch()` re-reads the same already-cached
        # bytes afterwards (Starlette's `Request.stream()` replays `.body()`'s cached
        # value once read), so there is still no second read of the ASGI stream.
        if body_schema is not None:
            payload = await body_validation.parse_json_body(request)
            try:
                errors = body_validation.validate_payload(route["id"], body_schema, payload)
            except Exception as exc:  # $ref resolution failing closed, or similar
                logger.error(f"Body schema validation errored for {extension_name} {route.get('path')}: {exc}")
                raise HTTPException(status_code=500, detail="This route's body schema is misconfigured")
            if errors:
                raise HTTPException(status_code=422, detail=errors)
        try:
            return await dispatch(request, upstream, route)
        except APIError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message)

    _handler.__signature__ = signature.build_signature(route)
    return _handler


def _app_for_visibility(visibility: str, public_app: FastAPI, internal_app: FastAPI) -> FastAPI:
    apps = {"public": public_app, "internal": internal_app}
    if visibility not in apps:
        raise ValueError(f"Unknown route visibility '{visibility}'")
    return apps[visibility]


def add_routes_from_specs(
    public_app: FastAPI,
    internal_app: FastAPI,
    extension_name: str,
    upstreams: List[Dict[str, Any]],
    routes: List[Dict[str, Any]],
) -> None:
    """Mounts one live route per persisted `extension_routes` row for `extension_name`,
    on the app matching its `visibility`, dispatching to the upstream (by manifest key)
    each route references. Idempotent — call `remove_live_routes` first if re-mounting
    (enable already does this)."""
    upstream_by_key = {upstream["key"]: upstream for upstream in upstreams}

    for route in routes:
        upstream = upstream_by_key.get(route["upstream"])
        if upstream is None:
            logger.warning(
                f"Skipping extension route {extension_name} {route.get('path')}: "
                f"unknown upstream key '{route['upstream']}'"
            )
            continue

        visibility = route.get("visibility") or "public"
        target_app = _app_for_visibility(visibility, public_app, internal_app)

        dependencies = []
        if visibility == "internal":
            dependencies.append(Depends(_internal_key_dependency(extension_name)))
        elif visibility == "public" and route.get("required_action"):
            dependencies.append(Depends(ABACPermissionCheck(route["required_action"], device_path=None)))

        tags = list(route.get("tags") or []) + [f"Extension: {extension_name}"]
        method = route.get("method") or "GET"

        target_app.add_api_route(
            route["path"],
            _dispatch_handler(extension_name, upstream, route),
            methods=[method],
            name=_route_name(extension_name, route["id"]),
            summary=route.get("summary") or f"{extension_name}: {method} {route['path']}",
            description=route.get("description"),
            tags=tags,
            deprecated=bool(route.get("deprecated")),
            status_code=route.get("status_code") or 200,
            operation_id=_operation_id(extension_name, method, route["path"]),
            dependencies=dependencies,
            include_in_schema=True,
        )
        logger.info(f"Mounted extension route: {extension_name} {method} {route['path']} ({visibility})")

    for app in (public_app, internal_app):
        app.openapi_schema = None  # force /openapi.json to regenerate with the new routes


def remove_live_routes(app: FastAPI, extension_name: str) -> None:
    """Unmounts every live route previously added for `extension_name` on `app`, and
    drops its routes' cached body-schema validators — a re-mount (enable, or a fresh
    register/replace) always gets brand-new route ids, so a stale cache entry would
    otherwise just leak, never being read again."""
    prefix = f"{_ROUTE_NAME_PREFIX}{extension_name}__"
    kept = []
    for route in app.router.routes:
        name = getattr(route, "name", "")
        if name.startswith(prefix):
            body_validation.invalidate(name[len(prefix):])
            continue
        kept.append(route)
    app.router.routes = kept
    app.openapi_schema = None


async def load_all_routes(
    public_app: FastAPI,
    internal_app: FastAPI,
    extension_repo: ExtensionRepository,
) -> None:
    """Re-mounts every persisted, *enabled* extension's routes — called once at startup
    so registrations survive a process restart. A disabled extension stays disabled."""
    for ext in await extension_repo.list_extensions_rows():
        if not ext.get("enabled"):
            continue
        upstreams = await extension_repo.list_upstreams(ext["name"])
        routes = await extension_repo.list_routes(ext["name"])
        add_routes_from_specs(public_app, internal_app, ext["name"], upstreams, routes)

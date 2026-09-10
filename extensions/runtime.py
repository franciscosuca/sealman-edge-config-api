"""Turns persisted `extension_routes` rows into live FastAPI routes and back.

Current scope: route mounting/unmounting mechanics and the two visibility
channels' auth (`public` via the route's own `required_action` through
`ABACPermissionCheck`, `internal` via `X-Internal-Key`) only. The `device`
visibility / field-ingress channel is deliberately **not** implemented — skipped
for the whole effort, not just deferred to a later stage (see IMPLEMENTATION-LOG.md). The
mounted handler is a placeholder — actual upstream dispatch (`http`/`iotedge`)
and the dynamic per-manifest request signature/body validation are not implemented
yet, so every mounted route currently accepts a plain `Request` and always
responds `501`.
"""

import logging
from typing import Any, Dict, List

from fastapi import Depends, FastAPI, Header, HTTPException, Request

from authorization.abac_permission_check import ABACPermissionCheck
from db.repos.extension import ExtensionRepository
from db.session import get_repository

from .security import keys_match

logger = logging.getLogger("EdgeConfigAPI")

_ROUTE_NAME_PREFIX = "extension_route__"


def _route_name(extension_name: str, route_id: str) -> str:
    return f"{_ROUTE_NAME_PREFIX}{extension_name}__{route_id}"


def _internal_key_dependency(extension_name: str):
    async def _check(
        x_internal_key: str = Header(..., alias="X-Internal-Key"),
        extension_repo: ExtensionRepository = Depends(get_repository(ExtensionRepository)),
    ) -> None:
        row = await extension_repo.get_extension_row(extension_name)
        if row is None or not keys_match(x_internal_key, row.get("internal_key_hash") or ""):
            raise HTTPException(status_code=401, detail="Invalid X-Internal-Key")

    return _check


def _placeholder_handler(extension_name: str, path: str):
    async def _handler(request: Request):
        raise HTTPException(
            status_code=501,
            detail=(
                f"Extension '{extension_name}' route '{path}' is mounted but upstream dispatch "
                "is not implemented yet (will be implemented in a later stage)."
            ),
        )

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
    routes: List[Dict[str, Any]],
) -> None:
    """Mounts one live route per persisted `extension_routes` row for `extension_name`,
    on the app matching its `visibility`. Idempotent — call `remove_live_routes` first
    if re-mounting (enable already does this)."""
    for route in routes:
        visibility = route.get("visibility") or "public"
        target_app = _app_for_visibility(visibility, public_app, internal_app)

        dependencies = []
        if visibility == "internal":
            dependencies.append(Depends(_internal_key_dependency(extension_name)))
        elif visibility == "public" and route.get("required_action"):
            dependencies.append(Depends(ABACPermissionCheck(route["required_action"], device_path=None)))

        tags = list(route.get("tags") or []) + [f"Extension: {extension_name}"]

        target_app.add_api_route(
            route["path"],
            _placeholder_handler(extension_name, route["path"]),
            methods=[route.get("method") or "GET"],
            name=_route_name(extension_name, route["id"]),
            summary=route.get("summary") or f"{extension_name}: {route['method']} {route['path']}",
            description=route.get("description"),
            tags=tags,
            deprecated=bool(route.get("deprecated")),
            dependencies=dependencies,
            include_in_schema=True,
        )
        logger.info(f"Mounted extension route: {extension_name} {route.get('method')} {route['path']} ({visibility})")

    for app in (public_app, internal_app):
        app.openapi_schema = None  # force /openapi.json to regenerate with the new routes


def remove_live_routes(app: FastAPI, extension_name: str) -> None:
    """Unmounts every live route previously added for `extension_name` on `app`."""
    prefix = f"{_ROUTE_NAME_PREFIX}{extension_name}__"
    app.router.routes = [route for route in app.router.routes if not getattr(route, "name", "").startswith(prefix)]
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
        routes = await extension_repo.list_routes(ext["name"])
        add_routes_from_specs(public_app, internal_app, ext["name"], routes)

"""Wiring for the extension system: the internal side app and the /extensions
management router.

Business logic lives in `extensions/registry.py`; this module only builds the side
app, mounts the router (with real `db/repos`-backed dependencies, not a module-level
stub), and re-hydrates persisted routes at startup.

The field-ingress side app and its device-key auth channel are deliberately **not**
implemented — skipped until explicitly picked back up, not just deferred to a later
stage.
"""

import asyncio
import logging
from typing import List, Optional

import uvicorn
from fastapi import Depends, FastAPI

from authorization.abac_permission_check import ABACPermissionCheck
from authorization.permission_types import Extension
from constants import (
    EXTENSIONS_ENABLED,
    EXTENSIONS_INTERNAL_API_HOST,
    EXTENSIONS_INTERNAL_API_PORT,
)
from db.repos.extension import ExtensionRepository
from db.repos.role import RoleRepository
from db.session import AsyncSessionLocal, get_repository
from routers.base_api_router import BaseAPIRouter

from . import registry, runtime
from .schemas import (
    ExtensionDetail,
    ExtensionHealthCheckResult,
    ExtensionRegistration,
    InternalKeyRotateResponse,
)

# Built once at import time — model_json_schema() is a pure function of the model
# definition, not per-request state.
_REGISTRATION_SCHEMA = ExtensionRegistration.model_json_schema()

logger = logging.getLogger("EdgeConfigAPI")

# Separate ASGI apps (not app.mount()) so each can run its own uvicorn.Server on its own
# port/network. Bind address is 0.0.0.0 by design — see constants.py and README's
# "Extension system side apps" section for the rationale and the network-restriction
# requirement this places on deployment config.
internal_app = FastAPI(
    title="Edge Configuration API — Internal Extension Router",
    description="Service-to-service router for extension microservices, authenticated via X-Internal-Key. Never expose this port publicly.",
)


@internal_app.get("/", include_in_schema=False)
async def _internal_liveness():
    return {"app": "internal", "status": "ok"}


def _build_management_router() -> BaseAPIRouter:
    """Builds the /extensions management router.

    Kept inline here (not under routers/<feature>/router.py) because it's constructed
    together with, and closes over, the two side apps it also mounts/unmounts routes on.
    """
    router = BaseAPIRouter(prefix="/extensions", tags=["Admin – Extensions"])
    register_dep = Depends(ABACPermissionCheck(Extension.REGISTER, device_path=None))
    deregister_dep = Depends(ABACPermissionCheck(Extension.DEREGISTER, device_path=None))
    read_dep = Depends(ABACPermissionCheck(Extension.READ, device_path=None))

    @router.post(
        "",
        summary="Register a new extension",
        description=(
            "Persists an extension's manifest with enabled=false. A freshly-registered "
            "extension is inert — no routes are mounted until it is enabled."
        ),
        response_model=ExtensionDetail,
        status_code=201,
        dependencies=[register_dep],
    )
    async def register_extension(
        registration: ExtensionRegistration,
        extension_repo: ExtensionRepository = Depends(get_repository(ExtensionRepository)),
        role_repo: RoleRepository = Depends(get_repository(RoleRepository)),
    ) -> ExtensionDetail:
        return await registry.register_extension(extension_repo, role_repo, registration)

    # Registered before "/{name}" — a literal "/schema" path segment must be matched
    # before the parameterized route would otherwise capture it as `name="schema"`.
    @router.get(
        "/schema",
        summary="Get the extension registration manifest's JSON Schema",
        description=(
            "Returns ExtensionRegistration.model_json_schema() verbatim, so extension "
            "authors/tooling can validate a manifest offline before ever calling "
            "POST /extensions. Always in sync with the code, unlike a hand-maintained doc."
        ),
        dependencies=[read_dep],
    )
    async def get_registration_schema() -> dict:
        return _REGISTRATION_SCHEMA

    @router.get(
        "",
        summary="List registered extensions",
        response_model=List[ExtensionDetail],
        dependencies=[read_dep],
    )
    async def list_extensions(
        extension_repo: ExtensionRepository = Depends(get_repository(ExtensionRepository)),
    ) -> List[ExtensionDetail]:
        return await registry.list_extensions(extension_repo)

    @router.get(
        "/{name}",
        summary="Get one registered extension",
        response_model=ExtensionDetail,
        dependencies=[read_dep],
    )
    async def get_extension(
        name: str,
        extension_repo: ExtensionRepository = Depends(get_repository(ExtensionRepository)),
    ) -> ExtensionDetail:
        return await registry.get_extension(extension_repo, name)

    @router.put(
        "/{name}",
        summary="Replace an extension's manifest",
        description=(
            "Atomically replaces the entire manifest; never changes `enabled`. If the "
            "extension is currently enabled, live routes are unmounted and re-mounted from "
            "the new manifest. Refuses (409) to drop an action still granted to a role — "
            "clean up role grants first (DELETE, by contrast, strips grants)."
        ),
        response_model=ExtensionDetail,
        dependencies=[register_dep],
    )
    async def replace_extension(
        name: str,
        registration: ExtensionRegistration,
        extension_repo: ExtensionRepository = Depends(get_repository(ExtensionRepository)),
        role_repo: RoleRepository = Depends(get_repository(RoleRepository)),
    ) -> ExtensionDetail:
        detail = await registry.replace_extension(extension_repo, role_repo, name, registration)
        if detail.enabled:
            for app in (_public_app, internal_app):
                runtime.remove_live_routes(app, name)
            upstreams = await extension_repo.list_upstreams(name)
            routes = await extension_repo.list_routes(name)
            runtime.add_routes_from_specs(_public_app, internal_app, name, upstreams, routes)
        return detail

    @router.delete(
        "/{name}",
        summary="Deregister an extension",
        description=(
            "Unmounts live routes, strips this extension's actions from every role that "
            "holds them, then deletes the extension and leftover Action rows."
        ),
        status_code=204,
        dependencies=[deregister_dep],
    )
    async def delete_extension(
        name: str,
        extension_repo: ExtensionRepository = Depends(get_repository(ExtensionRepository)),
        role_repo: RoleRepository = Depends(get_repository(RoleRepository)),
    ) -> None:
        for app in (_public_app, internal_app):
            runtime.remove_live_routes(app, name)
        await registry.deregister_extension(extension_repo, role_repo, name)

    @router.post(
        "/{name}/enable",
        summary="Enable an extension",
        description="Mounts every persisted route for this extension onto the correct app by visibility.",
        response_model=ExtensionDetail,
        dependencies=[register_dep],
    )
    async def enable_extension(
        name: str,
        extension_repo: ExtensionRepository = Depends(get_repository(ExtensionRepository)),
    ) -> ExtensionDetail:
        # Idempotent: drop any already-live routes first so re-enabling self-heals a
        # partial/stale mount instead of duplicating routes.
        for app in (_public_app, internal_app):
            runtime.remove_live_routes(app, name)
        upstreams = await extension_repo.list_upstreams(name)
        routes = await extension_repo.list_routes(name)
        runtime.add_routes_from_specs(_public_app, internal_app, name, upstreams, routes)
        return await registry.enable_extension(extension_repo, name)

    @router.post(
        "/{name}/disable",
        summary="Disable an extension",
        description="Unmounts every live route for this extension. RBAC grants and issued keys are untouched.",
        response_model=ExtensionDetail,
        dependencies=[deregister_dep],
    )
    async def disable_extension(
        name: str,
        extension_repo: ExtensionRepository = Depends(get_repository(ExtensionRepository)),
    ) -> ExtensionDetail:
        for app in (_public_app, internal_app):
            runtime.remove_live_routes(app, name)
        return await registry.disable_extension(extension_repo, name)

    @router.post(
        "/{name}/health-check",
        summary="Check an extension's upstream health now",
        description=(
            "Runs a fresh health check of every one of this extension's upstreams and "
            "persists the result. A plain GET never triggers a check on its own — it "
            "only ever returns whatever was last persisted here."
        ),
        response_model=ExtensionHealthCheckResult,
        dependencies=[read_dep],
    )
    async def check_extension_health(
        name: str,
        extension_repo: ExtensionRepository = Depends(get_repository(ExtensionRepository)),
    ) -> ExtensionHealthCheckResult:
        return await registry.check_extension_health(extension_repo, name)

    @router.post(
        "/{name}/internal-key/rotate",
        summary="Rotate an extension's internal key",
        description=(
            "Issues a fresh X-Internal-Key and invalidates the previous one immediately. "
            "The raw key is only ever returned here — never re-readable via GET."
        ),
        response_model=InternalKeyRotateResponse,
        dependencies=[register_dep],
    )
    async def rotate_internal_key(
        name: str,
        extension_repo: ExtensionRepository = Depends(get_repository(ExtensionRepository)),
    ) -> InternalKeyRotateResponse:
        raw_key = await registry.rotate_internal_key(extension_repo, name)
        return InternalKeyRotateResponse(internal_key=raw_key)

    return router


_public_app: Optional[FastAPI] = None


def setup_extensions(app: FastAPI) -> None:
    """Mounts the static /extensions management API onto the public app."""
    global _public_app
    _public_app = app
    app.include_router(_build_management_router())


async def hydrate_all_routes() -> None:
    """Re-mounts persisted, enabled extension routes at startup, on both apps. Also
    re-fetches every `body_ref` route's upstream OpenAPI schema first (see
    registry.refresh_ref_schemas), so a route that was `unreachable_ref` when the
    process last started flips to `upstream_declared` (or vice versa) without a manual
    PUT, purely by restarting."""
    if _public_app is None:
        return
    async with AsyncSessionLocal() as session:
        extension_repo = get_repository(ExtensionRepository)(session)
        await registry.refresh_ref_schemas(extension_repo)
        await runtime.load_all_routes(_public_app, internal_app, extension_repo)


def start_side_apps() -> List[asyncio.Task]:
    """Starts the internal side app as a task on the caller's running event loop."""
    if not EXTENSIONS_ENABLED:
        return []

    config = uvicorn.Config(internal_app, host=EXTENSIONS_INTERNAL_API_HOST, port=EXTENSIONS_INTERNAL_API_PORT, log_level="info")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # the public app's uvicorn already owns this

    logger.info(f"Starting extension side app: internal on {EXTENSIONS_INTERNAL_API_HOST}:{EXTENSIONS_INTERNAL_API_PORT}")
    return [asyncio.create_task(server.serve())]


"""Business logic for the extension registry: validates a manifest, enrolls its
actions into the existing RBAC catalog (`actions`/`roles` tables), persists the
extension + its upstreams/routes, and issues/rotates its keys.

Owns *what* a registration means, not *how* a route runs (that's runtime.py) or how
it's bound to a dynamic FastAPI signature (a later stage's scope). Every function here
is a plain async function taking the repositories it needs as parameters — no
module-level state — so it can be exercised the same way from the management router
(DI) or from tests.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from jsonschema.exceptions import SchemaError

from db.repos.extension import ExtensionRepository
from db.repos.role import RoleRepository
from exceptions import APIError

from . import body_validation, health
from .schemas import ExtensionHealthCheckResult, ExtensionRegistration, ExtensionDetail, RouteSpec, UpstreamSpec
from .security import generate_key, hash_key
from .upstreams import http as http_upstream
from .upstreams.iotedge import probe_registration_health

logger = logging.getLogger("EdgeConfigAPI")


def _validate_manifest_cross_references(registration: ExtensionRegistration) -> None:
    """Manifest-wide rules `schemas.py` can't express on its own (it validates each field
    in isolation): a route's `upstream` must exist, the fields it carries must match that
    upstream's type, and a `declared`-mode `body`/`example` pair must actually be a valid
    JSON Schema and a conforming example. No network calls happen here — resolving a
    `body_ref` against its upstream's live OpenAPI doc is `_resolve_route_validation`'s
    job, run separately by `_persist_manifest`."""
    for route in registration.routes:
        upstream = registration.upstreams.get(route.upstream)
        if upstream is None:
            raise APIError(f"Route '{route.path}' references unknown upstream '{route.upstream}'", 422)

        if upstream.type == "http" and not route.upstream_path:
            raise APIError(f"Route '{route.path}' targets an http upstream but has no upstream_path", 422)

        if upstream.type == "iotedge" and route.iotedge is None:
            raise APIError(f"Route '{route.path}' targets an iotedge upstream but has no iotedge call spec", 422)

        if route.body_ref and upstream.type != "http":
            raise APIError(
                f"Route '{route.path}' sets body_ref but its upstream '{route.upstream}' is not an http upstream", 422
            )

        if route.body is not None and route.body_ref:
            raise APIError(f"Route '{route.path}' cannot set both 'body' (declared schema) and 'body_ref'", 422)

        if route.body is not None:
            try:
                body_validation.check_schema(route.body)
            except SchemaError as exc:
                raise APIError(f"Route '{route.path}' body is not a valid JSON Schema: {exc.message}", 422)
            if route.example is not None:
                errors = body_validation.validate_once(route.body, route.example)
                if errors:
                    raise APIError(
                        f"Route '{route.path}' example does not conform to its own body schema: {errors}", 422
                    )


async def _resolve_route_validation(
    route: RouteSpec, upstreams: Dict[str, UpstreamSpec]
) -> Tuple[str, Optional[dict]]:
    """Computes a route's `validation_mode` and the `body` snapshot to persist for it —
    the literal author-supplied schema for `declared`, a fetched-and-resolved snapshot
    (display-only, never used for enforcement) for `upstream_declared`, `None` for
    `unreachable_ref`/`none`. The only step in registration that makes a network call."""
    if route.body is not None:
        return "declared", route.body
    if route.body_ref:
        upstream = upstreams[route.upstream]
        fetched = await http_upstream.fetch_body_schema(upstream.base_url, route.upstream_path, route.method)
        return ("upstream_declared", fetched) if fetched is not None else ("unreachable_ref", None)
    return "none", None


async def _persist_manifest(extension_repo: ExtensionRepository, registration: ExtensionRegistration) -> None:
    upstreams = [
        {"key": key, **spec.model_dump()} for key, spec in registration.upstreams.items()
    ]
    routes = []
    for route in registration.routes:
        route_dict = route.model_dump()
        validation_mode, body_snapshot = await _resolve_route_validation(route, registration.upstreams)
        route_dict["validation_mode"] = validation_mode
        route_dict["body"] = body_snapshot
        routes.append(route_dict)
    await extension_repo.replace_upstreams_and_routes(registration.name, upstreams, routes)


async def refresh_ref_schemas(extension_repo: ExtensionRepository) -> None:
    """Re-fetches every `body_ref` route's upstream OpenAPI schema — called once at
    startup (never per-request, never a background scheduler), flipping
    `unreachable_ref <-> upstream_declared` as the upstream's own reachability changes.
    The fetched schema is stored as a display-only snapshot, never used for request-time
    enforcement (that only ever reads `declared`-mode schemas)."""
    for ext in await extension_repo.list_extensions_rows():
        upstream_by_key = {u["key"]: u for u in await extension_repo.list_upstreams(ext["name"])}
        for route in await extension_repo.list_routes(ext["name"]):
            if not route.get("body_ref"):
                continue
            upstream = upstream_by_key.get(route["upstream"])
            if upstream is None or upstream.get("type") != "http":
                continue
            fetched = await http_upstream.fetch_body_schema(upstream["base_url"], route["upstream_path"], route["method"])
            mode = "upstream_declared" if fetched is not None else "unreachable_ref"
            await extension_repo.update_route_validation(route["id"], mode, fetched)


async def _probe_iotedge_upstreams(registration: ExtensionRegistration) -> None:
    """Best-effort registration-time typo guard: for every `iotedge` upstream with a
    `health_device_query` set, resolve a canary device and confirm `module_name` shows
    up in its reported `$edgeAgent` modules — logged as a warning, never rejects
    registration (see extensions/upstreams/iotedge.py's probe_registration_health)."""
    for key, upstream in registration.upstreams.items():
        if upstream.type != "iotedge" or not upstream.health_device_query:
            continue
        warning = await probe_registration_health(upstream.module_name, upstream.health_device_query)
        if warning:
            logger.warning(f"Extension '{registration.name}' upstream '{key}': {warning}")


async def _enroll_actions(
    extension_repo: ExtensionRepository,
    role_repo: RoleRepository,
    registration: ExtensionRegistration,
) -> None:
    action_names = [action.name for action in registration.actions]

    for action in registration.actions:
        await extension_repo.ensure_action(action.name, action.description, is_global=True)
    await extension_repo.record_extension_actions(registration.name, action_names)

    for role_name in registration.grant_to_roles:
        role = await role_repo.get_by_name(role_name)
        if role is None:
            raise APIError(f"Role '{role_name}' not found", 422)
        if action_names:
            await role_repo.add_actions_to_role(role["id"], action_names)


async def _build_detail(extension_repo: ExtensionRepository, name: str) -> ExtensionDetail:
    ext = await extension_repo.get_extension_row(name)
    if ext is None:
        raise APIError(f"Extension '{name}' not found", 404)

    upstreams_rows = await extension_repo.list_upstreams(name)
    routes_rows = await extension_repo.list_routes(name)
    actions = await extension_repo.list_extension_actions(name)

    upstreams = {row["key"]: {k: v for k, v in row.items() if k not in ("id", "key")} for row in upstreams_rows}
    routes = []
    for row in routes_rows:
        route = {k: v for k, v in row.items() if k != "id"}
        if route.get("iotedge_operation") or route.get("method_name"):
            route["iotedge"] = {
                "operation": route.pop("iotedge_operation") or "direct_method",
                "method_name": route.pop("method_name"),
            }
        else:
            route.pop("iotedge_operation", None)
            route.pop("method_name", None)
            route["iotedge"] = None
        routes.append(route)

    return ExtensionDetail(
        name=ext["name"],
        description=ext["description"],
        schema_version=ext["schema_version"],
        upstreams=upstreams,
        actions=[{"name": a} for a in actions],
        grant_to_roles=[],
        routes=routes,
        enabled=ext["enabled"],
    )


async def register_extension(
    extension_repo: ExtensionRepository,
    role_repo: RoleRepository,
    registration: ExtensionRegistration,
) -> ExtensionDetail:
    """Persists a brand-new extension's manifest with `enabled=false` — a freshly
    registered extension is inert until explicitly enabled."""
    _validate_manifest_cross_references(registration)

    if await extension_repo.extension_exists(registration.name):
        raise APIError(f"Extension '{registration.name}' is already registered", 409)

    await extension_repo.create_extension(registration.name, registration.description, registration.schema_version)
    await _enroll_actions(extension_repo, role_repo, registration)
    await _persist_manifest(extension_repo, registration)
    await _probe_iotedge_upstreams(registration)

    return await _build_detail(extension_repo, registration.name)


async def replace_extension(
    extension_repo: ExtensionRepository,
    role_repo: RoleRepository,
    name: str,
    registration: ExtensionRegistration,
) -> ExtensionDetail:
    """Atomically replaces `name`'s entire manifest; never changes `enabled`. Refuses
    (409) to drop an action that's still granted to any role rather than silently
    revoking access — the extension owner must clean up role grants first."""
    if registration.name != name:
        raise APIError("Cannot change an extension's name via PUT replace", 400)

    existing = await extension_repo.get_extension_row(name)
    if existing is None:
        raise APIError(f"Extension '{name}' not found", 404)

    _validate_manifest_cross_references(registration)

    old_actions = set(await extension_repo.list_extension_actions(name))
    new_actions = {action.name for action in registration.actions}
    for removed in old_actions - new_actions:
        if await extension_repo.is_action_granted_to_any_role(removed):
            raise APIError(
                f"Action '{removed}' is still granted to a role — revoke it before removing it from the manifest",
                409,
            )

    await extension_repo.set_description(name, registration.description)
    await extension_repo.set_schema_version(name, registration.schema_version)

    # A route's required_action FK depends on the action row already existing, so
    # actions must be (re-)enrolled before the new routes referencing them are persisted.
    await extension_repo.clear_extension_actions(name)
    await _enroll_actions(extension_repo, role_repo, registration)
    await _persist_manifest(extension_repo, registration)
    await extension_repo.delete_orphaned_actions(list(old_actions - new_actions))
    await _probe_iotedge_upstreams(registration)

    return await _build_detail(extension_repo, name)


async def get_extension(extension_repo: ExtensionRepository, name: str) -> ExtensionDetail:
    return await _build_detail(extension_repo, name)


async def list_extensions(extension_repo: ExtensionRepository) -> List[ExtensionDetail]:
    rows = await extension_repo.list_extensions_rows()
    return [await _build_detail(extension_repo, row["name"]) for row in rows]


async def check_extension_health(extension_repo: ExtensionRepository, name: str) -> ExtensionHealthCheckResult:
    """Runs a fresh health check of every one of `name`'s upstreams and persists the
    result — the sole trigger for a check (a plain `GET` only ever returns whatever was
    last persisted here). Upstreams are checked concurrently, so this call's latency is
    bounded by the single slowest upstream check, not their sum. Returns only the
    per-upstream health fields, not the full manifest (`ExtensionDetail`) — this
    endpoint's job is reporting health, not re-describing routes/actions/description."""
    if await extension_repo.get_extension_row(name) is None:
        raise APIError(f"Extension '{name}' not found", 404)

    upstreams_rows = await extension_repo.list_upstreams(name)
    if upstreams_rows:
        checked_at = datetime.now(timezone.utc)
        results = await asyncio.gather(*(health.check_upstream_health(row) for row in upstreams_rows))
        for row, (status, detail) in zip(upstreams_rows, results):
            await extension_repo.record_upstream_health(row["id"], status, detail, checked_at)
            row["last_status"] = status
            row["last_detail"] = detail
            row["last_checked_at"] = checked_at

    return ExtensionHealthCheckResult(
        name=name,
        upstreams={
            row["key"]: {
                "last_checked_at": row.get("last_checked_at"),
                "last_status": row["last_status"],
                "last_detail": row.get("last_detail"),
            }
            for row in upstreams_rows
        },
    )


async def deregister_extension(extension_repo: ExtensionRepository, name: str) -> None:
    existing = await extension_repo.get_extension_row(name)
    if existing is None:
        raise APIError(f"Extension '{name}' not found", 404)

    action_names = await extension_repo.list_extension_actions(name)
    await extension_repo.delete_extension(name)  # cascades upstreams/routes/actions
    await extension_repo.delete_orphaned_actions(action_names)


async def enable_extension(extension_repo: ExtensionRepository, name: str) -> ExtensionDetail:
    existing = await extension_repo.get_extension_row(name)
    if existing is None:
        raise APIError(f"Extension '{name}' not found", 404)

    await extension_repo.set_enabled(name, True)
    return await _build_detail(extension_repo, name)


async def disable_extension(extension_repo: ExtensionRepository, name: str) -> ExtensionDetail:
    existing = await extension_repo.get_extension_row(name)
    if existing is None:
        raise APIError(f"Extension '{name}' not found", 404)

    await extension_repo.set_enabled(name, False)
    return await _build_detail(extension_repo, name)


async def rotate_internal_key(extension_repo: ExtensionRepository, name: str) -> str:
    """Generates a fresh internal key, immediately invalidating the previous one — single
    active key, no dual-key grace period. Returns the raw key; only ever visible here,
    never re-readable via GET."""
    existing = await extension_repo.get_extension_row(name)
    if existing is None:
        raise APIError(f"Extension '{name}' not found", 404)

    raw_key = generate_key()
    await extension_repo.set_internal_key_hash(name, hash_key(raw_key))
    return raw_key

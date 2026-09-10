"""Business logic for the extension registry: validates a manifest, enrolls its
actions into the existing RBAC catalog (`actions`/`roles` tables), persists the
extension + its upstreams/routes, and issues/rotates its keys.

Owns *what* a registration means, not *how* a route runs (that's runtime.py) or
how it's dispatched to an upstream or bound to a dynamic FastAPI signature (both
will be implemented in a later stage). Every function here is a plain async function
taking the repositories it needs as parameters — no module-level state — so it can be
exercised the same way from the management router (DI) or from tests.
"""

from typing import List

from db.repos.extension import ExtensionRepository
from db.repos.role import RoleRepository
from exceptions import APIError

from .schemas import ExtensionDetail, ExtensionRegistration
from .security import generate_key, hash_key


def _validate_manifest_cross_references(registration: ExtensionRegistration) -> None:
    """Manifest-wide rules `schemas.py` can't express on its own (it validates each field
    in isolation): a route's `upstream` must exist, and the fields it carries must match
    that upstream's type."""
    for route in registration.routes:
        upstream = registration.upstreams.get(route.upstream)
        if upstream is None:
            raise APIError(f"Route '{route.path}' references unknown upstream '{route.upstream}'", 422)

        if upstream.type == "http" and not route.upstream_path:
            raise APIError(f"Route '{route.path}' targets an http upstream but has no upstream_path", 422)

        if upstream.type == "iotedge" and route.iotedge is None:
            raise APIError(f"Route '{route.path}' targets an iotedge upstream but has no iotedge call spec", 422)


async def _persist_manifest(extension_repo: ExtensionRepository, registration: ExtensionRegistration) -> None:
    upstreams = [
        {"key": key, **spec.model_dump()} for key, spec in registration.upstreams.items()
    ]
    routes = [route.model_dump() for route in registration.routes]
    await extension_repo.replace_upstreams_and_routes(registration.name, upstreams, routes)


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

    await extension_repo.create_extension(registration.name, registration.description)
    await _persist_manifest(extension_repo, registration)
    await _enroll_actions(extension_repo, role_repo, registration)

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
    await _persist_manifest(extension_repo, registration)

    await extension_repo.clear_extension_actions(name)
    await _enroll_actions(extension_repo, role_repo, registration)
    await extension_repo.delete_orphaned_actions(list(old_actions - new_actions))

    return await _build_detail(extension_repo, name)


async def get_extension(extension_repo: ExtensionRepository, name: str) -> ExtensionDetail:
    return await _build_detail(extension_repo, name)


async def list_extensions(extension_repo: ExtensionRepository) -> List[ExtensionDetail]:
    rows = await extension_repo.list_extensions_rows()
    return [await _build_detail(extension_repo, row["name"]) for row in rows]


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

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional


class ExtensionRepository(ABC):
    """Persistence for the dynamic API extension system (extensions, their upstreams and
    contributed routes, RBAC-action provenance).

    Business rules (manifest validation, key generation/hashing, RBAC role grants) live in
    extensions/registry.py, not here — this interface only reads/writes rows.
    """

    # --- extensions ------------------------------------------------------
    @abstractmethod
    async def extension_exists(self, name: str) -> bool:
        pass

    @abstractmethod
    async def create_extension(self, name: str, description: str, schema_version: int) -> Dict[str, Any]:
        """Raises ValueError if an extension with this name already exists."""
        pass

    @abstractmethod
    async def get_extension_row(self, name: str) -> Optional[Dict[str, Any]]:
        pass

    @abstractmethod
    async def list_extensions_rows(self) -> List[Dict[str, Any]]:
        pass

    @abstractmethod
    async def set_description(self, name: str, description: str) -> None:
        pass

    @abstractmethod
    async def set_schema_version(self, name: str, schema_version: int) -> None:
        pass

    @abstractmethod
    async def set_enabled(self, name: str, enabled: bool) -> None:
        pass

    @abstractmethod
    async def set_internal_key_hash(self, name: str, key_hash: str) -> None:
        pass

    @abstractmethod
    async def delete_extension(self, name: str) -> bool:
        pass

    # --- upstreams + routes ------------------------------------------------
    @abstractmethod
    async def replace_upstreams_and_routes(
        self, name: str, upstreams: List[Dict[str, Any]], routes: List[Dict[str, Any]]
    ) -> None:
        """Atomically replaces every persisted upstream/route row for `name` with the given
        ones. `routes` reference their upstream by the manifest key (`upstream` field);
        resolving that key to the newly (re)persisted upstream's id is this method's job."""
        pass

    @abstractmethod
    async def list_upstreams(self, name: str) -> List[Dict[str, Any]]:
        pass

    @abstractmethod
    async def list_routes(self, name: str) -> List[Dict[str, Any]]:
        pass

    @abstractmethod
    async def record_upstream_health(
        self, upstream_id: str, status: str, detail: Optional[str], checked_at: datetime
    ) -> None:
        """Persists one upstream's most recent health-check result. Silently a no-op if
        `upstream_id` no longer exists (e.g. the extension was replaced/deregistered
        concurrently with the check that produced this result)."""
        pass

    @abstractmethod
    async def update_route_validation(self, route_id: str, validation_mode: str, body: Optional[Dict[str, Any]]) -> None:
        """Updates one route's `validation_mode`/`body` snapshot in place — used by the
        startup `upstream_declared`/`unreachable_ref` re-fetch, which must not disturb
        any other persisted route field."""
        pass

    # --- RBAC action provenance -------------------------------------------
    @abstractmethod
    async def ensure_action(self, action_name: str, description: str, is_global: bool = True) -> None:
        """Create the Action row if it doesn't already exist (idempotent)."""
        pass

    @abstractmethod
    async def record_extension_actions(self, extension_name: str, action_names: List[str]) -> None:
        pass

    @abstractmethod
    async def clear_extension_actions(self, extension_name: str) -> None:
        pass

    @abstractmethod
    async def list_extension_actions(self, extension_name: str) -> List[str]:
        pass

    @abstractmethod
    async def is_action_granted_to_any_role(self, action_name: str) -> bool:
        pass

    @abstractmethod
    async def delete_orphaned_actions(self, action_names: List[str]) -> None:
        """Best-effort: deletes each of `action_names` that no longer has any
        `extension_actions` provenance row and isn't granted to any role. Silently
        skips any action that's still referenced or granted."""
        pass

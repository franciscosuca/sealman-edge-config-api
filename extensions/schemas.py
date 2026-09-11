"""Pydantic models for an extension's registration manifest and the /extensions API shapes.

Input validation shapes only — no persistence, no business rules (that's registry.py's
job). Field-level cross-references (e.g. a route's `upstream` key must exist in
`upstreams`, `iotedge` must be present iff the referenced upstream is `type: "iotedge"`,
`body_ref` requiring an `http` upstream) are manifest-wide business rules validated by
registry.py, not enforced here.
"""

from datetime import datetime
from typing import Annotated, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator


class ActionSpec(BaseModel):
    name: str
    description: str = ""


class HttpUpstreamSpec(BaseModel):
    type: Literal["http"]
    base_url: str
    health_path: str = "/health"
    version_field: str = "version"
    expected_version: Optional[str] = None
    # Server-computed (extensions/health.py, run only via POST /extensions/{name}/health-check)
    # — never taken from a client-supplied manifest, present here only so responses surface
    # the last check's persisted result verbatim (Pydantic silently drops dict keys with no
    # matching field). A plain GET never refreshes these; it just returns whatever's stored.
    # Persisted (not ephemeral) deliberately: multiple independent callers (an admin
    # datatable checking on page visit, a future ops dashboard polling on its own
    # schedule) all read/write the same last-known value instead of each needing their
    # own cache. There's no server-side expiry — `last_checked_at`'s age is the staleness
    # signal; how stale is "too stale" is a per-consumer judgment call, not the API's.
    last_checked_at: Optional[datetime] = None
    last_status: Literal["unknown", "healthy", "unhealthy"] = "unknown"
    last_detail: Optional[str] = None


class IotedgeUpstreamSpec(BaseModel):
    type: Literal["iotedge"]
    module_name: str
    # IoT Hub device query/target-condition string (same syntax as deployment
    # targeting, e.g. tags.module='foo') resolved to a canary device dynamically
    # at check time, once implemented in a later stage — not a fixed device ID, so a
    # decommissioned/renamed canary doesn't permanently break health as long as a
    # replacement still matches.
    health_device_query: Optional[str] = None
    # Server-computed, see HttpUpstreamSpec's identical fields above — same caveats apply.
    last_checked_at: Optional[datetime] = None
    last_status: Literal["unknown", "healthy", "unhealthy"] = "unknown"
    last_detail: Optional[str] = None


UpstreamSpec = Annotated[Union[HttpUpstreamSpec, IotedgeUpstreamSpec], Field(discriminator="type")]


class QueryParamSpec(BaseModel):
    name: str
    type: Literal["string", "integer", "number", "boolean"] = "string"
    required: bool = True
    description: Optional[str] = None


class IotEdgeCallSpec(BaseModel):
    operation: Literal["direct_method", "twin_read", "twin_write"] = "direct_method"
    method_name: Optional[str] = None

    @model_validator(mode="after")
    def _check_method_name(self) -> "IotEdgeCallSpec":
        if self.operation == "direct_method" and not self.method_name:
            raise ValueError("method_name is required when operation is 'direct_method'")
        if self.operation != "direct_method" and self.method_name:
            raise ValueError("method_name must be omitted unless operation is 'direct_method'")
        return self


class RouteSpec(BaseModel):
    upstream: str
    path: str
    method: str = "GET"
    summary: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    deprecated: bool = False
    status_code: int = 200
    query_params: List[QueryParamSpec] = Field(default_factory=list)
    # Literal JSON Schema (validated via `jsonschema` at registration + request time,
    # see extensions/body_validation.py) — not an OpenAPI wrapper. Mutually exclusive
    # with `body_ref` on a *registration* payload (registry.py's job to reject both — a
    # persisted route legitimately has both a `body_ref: true` and a `body` snapshot
    # fetched from the upstream, so this can't be a model-level invariant here).
    body: Optional[dict] = None
    example: Optional[dict] = None
    # If set, this route's body schema is fetched from its own `http` upstream's
    # `{base_url}/openapi.json` instead of being declared here (`upstream_declared` /
    # `unreachable_ref` validation_mode, see `validation_mode` below).
    body_ref: bool = False
    # Server-computed (registry.py's `_resolve_route_validation`), never taken from a
    # client-supplied manifest as-is — present here only so GET/list responses surface it
    # verbatim.
    validation_mode: Optional[Literal["declared", "upstream_declared", "unreachable_ref", "none"]] = None
    visibility: Literal["public", "internal"] = "public"
    required_action: Optional[str] = None
    scoped: bool = False
    scope_param: str = "device_name"
    scope_in: Literal["path", "query"] = "query"
    upstream_path: Optional[str] = None  # required for `http` upstreams only
    iotedge: Optional[IotEdgeCallSpec] = None  # required for `iotedge` upstreams only


class ExtensionRegistration(BaseModel):
    # Required, no default: a missing or unrecognized value (anything but the literal
    # `1`) is rejected outright rather than guessed at, mirroring $edgeAgent's own
    # schemaVersion convention. Extend to Literal[1, 2] (not widen to a plain int) the
    # day a second manifest shape actually exists.
    schema_version: Literal[1]
    name: str
    description: str = ""
    upstreams: Dict[str, UpstreamSpec]
    actions: List[ActionSpec] = Field(default_factory=list)
    grant_to_roles: List[str] = Field(default_factory=list)
    routes: List[RouteSpec]


class ExtensionDetail(ExtensionRegistration):
    enabled: bool = False


class UpstreamHealthStatus(BaseModel):
    last_checked_at: Optional[datetime] = None
    last_status: Literal["unknown", "healthy", "unhealthy"] = "unknown"
    last_detail: Optional[str] = None


class ExtensionHealthCheckResult(BaseModel):
    name: str
    upstreams: Dict[str, UpstreamHealthStatus]


class InternalKeyRotateResponse(BaseModel):
    internal_key: str

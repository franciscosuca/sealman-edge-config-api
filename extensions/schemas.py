"""Pydantic models for an extension's registration manifest and the /extensions API shapes.

Input validation shapes only — no persistence, no business rules (that's registry.py's
job). Field-level cross-references (e.g. a route's `upstream` key must exist in
`upstreams`, `iotedge` must be present iff the referenced upstream is `type: "iotedge"`)
are manifest-wide business rules validated by registry.py, not enforced here.
`schema_version`, `validation_mode` and the `body`/`example` pairing check will be
implemented in a later stage and are intentionally not present yet.
"""

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


class IotedgeUpstreamSpec(BaseModel):
    type: Literal["iotedge"]
    module_name: str
    # IoT Hub device query/target-condition string (same syntax as deployment
    # targeting, e.g. tags.module='foo') resolved to a canary device dynamically
    # at check time, once implemented in a later stage — not a fixed device ID, so a
    # decommissioned/renamed canary doesn't permanently break health as long as a
    # replacement still matches.
    health_device_query: Optional[str] = None


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
    # Literal JSON Schema (validated via `jsonschema` in a later stage) — not an OpenAPI wrapper.
    body: Optional[dict] = None
    example: Optional[dict] = None
    visibility: Literal["public", "internal"] = "public"
    required_action: Optional[str] = None
    scoped: bool = False
    scope_param: str = "device_name"
    scope_in: Literal["path", "query"] = "query"
    upstream_path: Optional[str] = None  # required for `http` upstreams only
    iotedge: Optional[IotEdgeCallSpec] = None  # required for `iotedge` upstreams only


class ExtensionRegistration(BaseModel):
    name: str
    description: str = ""
    upstreams: Dict[str, UpstreamSpec]
    actions: List[ActionSpec] = Field(default_factory=list)
    grant_to_roles: List[str] = Field(default_factory=list)
    routes: List[RouteSpec]


class ExtensionDetail(ExtensionRegistration):
    enabled: bool = False


class InternalKeyRotateResponse(BaseModel):
    internal_key: str

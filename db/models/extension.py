"""SQLAlchemy ORM models for the dynamic API extension system.

Persistence shape only — manifest validation lives in extensions/schemas.py,
business rules (RBAC enrollment, key issuance, lifecycle) in extensions/registry.py.
"""

import uuid

from sqlalchemy import Boolean, Column, ForeignKey, Integer, Text, TIMESTAMP, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

from db.base import Base


class Extension(Base):
    __tablename__ = "extensions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(Text, nullable=False, unique=True)
    description = Column(Text, nullable=False, default="")
    enabled = Column(Boolean, nullable=False, default=False)
    internal_key_hash = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ExtensionUpstream(Base):
    """One upstream (http micro-service or iotedge module) contributed by an extension.

    Health-check configuration is split into first-class, nullable columns per upstream
    type instead of a free-form JSON blob — leaving a column unset just opts out of that
    check (the `last_*` columns are read/written once health checks are implemented).
    """

    __tablename__ = "extension_upstreams"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    extension_id = Column(UUID(as_uuid=True), ForeignKey("extensions.id", ondelete="CASCADE"), nullable=False, index=True)
    key = Column(Text, nullable=False)  # the manifest's `upstreams` dict key, e.g. "svc"
    type = Column(Text, nullable=False)  # "http" | "iotedge"

    # http upstreams
    base_url = Column(Text, nullable=True)
    health_path = Column(Text, nullable=True, default="/health")
    version_field = Column(Text, nullable=True, default="version")
    expected_version = Column(Text, nullable=True)

    # iotedge upstreams
    module_name = Column(Text, nullable=True)
    # IoT Hub device query/target-condition string (same syntax as deployment targeting)
    # resolved to a canary device dynamically at check time (once health checks are
    # implemented) — not a fixed device id.
    health_device_query = Column(Text, nullable=True)

    # check-result columns, updated by whatever triggers a check (once implemented)
    last_checked_at = Column(TIMESTAMP(timezone=True), nullable=True)
    last_status = Column(Text, nullable=False, default="unknown")  # unknown | healthy | unhealthy
    last_detail = Column(Text, nullable=True)


class ExtensionRoute(Base):
    """A single route contributed by an extension — enough to mount a live FastAPI route.

    The dynamic per-manifest request signature and the actual upstream dispatch are not
    implemented yet (both land in a later stage); this row is the persistence target both
    build on.
    """

    __tablename__ = "extension_routes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    extension_id = Column(UUID(as_uuid=True), ForeignKey("extensions.id", ondelete="CASCADE"), nullable=False, index=True)
    upstream_id = Column(UUID(as_uuid=True), ForeignKey("extension_upstreams.id", ondelete="CASCADE"), nullable=False)

    path = Column(Text, nullable=False)
    method = Column(Text, nullable=False, default="GET")
    visibility = Column(Text, nullable=False, default="public")  # public | internal

    summary = Column(Text, nullable=True)
    description = Column(Text, nullable=True)
    tags = Column(JSONB, nullable=True)
    deprecated = Column(Boolean, nullable=False, default=False)
    status_code = Column(Integer, nullable=False, default=200)

    query_params = Column(JSONB, nullable=False, default=list)
    body = Column(JSONB, nullable=True)  # literal JSON Schema (validated in a later stage)
    example = Column(JSONB, nullable=True)
    validation_mode = Column(Text, nullable=True)  # declared | upstream_declared | unreachable_ref | none (set in a later stage)

    required_action = Column(Text, ForeignKey("actions.name"), nullable=True)
    scoped = Column(Boolean, nullable=False, default=False)
    scope_param = Column(Text, nullable=False, default="device_name")
    scope_in = Column(Text, nullable=False, default="query")

    # http transport
    upstream_path = Column(Text, nullable=True)

    # iotedge transport
    iotedge_operation = Column(Text, nullable=True)  # direct_method | twin_read | twin_write
    method_name = Column(Text, nullable=True)


class ExtensionAction(Base):
    """Provenance: which extension introduced which RBAC action, so deregistration can
    clean up actions no longer referenced by any extension."""

    __tablename__ = "extension_actions"

    action_name = Column(Text, ForeignKey("actions.name", ondelete="CASCADE"), primary_key=True)
    extension_id = Column(UUID(as_uuid=True), ForeignKey("extensions.id", ondelete="CASCADE"), primary_key=True)

# Architecture Diagram: Component Duplication

This architecture diagram models the code duplication identified between `extensions/iotedge_transport.py` and existing legacy routes across `routers/`.

Interactive standalone HTML diagram: [duplication_architecture.html](duplication_architecture.html)  
Archify specification source: [duplication_architecture.architecture.json](duplication_architecture.architecture.json)

## Architectural Overview

- **Callers & Entrypoints**:
  - `API Client`: External consumer invoking REST endpoints.
  - `Extensions Subsystem`: Health check (`extensions/health.py`) and runtime orchestration (`extensions/runtime.py`).
- **Duplicated Call Sites**:
  - `extensions/iotedge_transport.py`: Newly added adapter for upstream IoT Hub operations.
  - `routers/cmd_proxy`: Device direct method proxy routes (`post_device_cmd_*.py`).
  - `routers/module_config`: Module twin read and patch routes (`get_module_twin_config.py`, `post_module_twin_config.py`).
  - `routers/general`: Module method dispatch and device queries (`post_module_method.py`, `get_device_modules.py`).
- **Shared Infrastructure**:
  - `helper.get_iothub_auth_headers()`: Shared SAS token and Workload Identity credential provider.
  - `async_requests`: Shared httpx async client pool.
- **Upstream Target**:
  - `Azure IoT Hub`: Target REST API endpoints (`/twins/{device}/modules/...`).

## Identified Duplication Points

- **Direct Method Invocation**: Implemented independently in `extensions/iotedge_transport.py`, `routers/general/post_module_method.py`, and `routers/cmd_proxy/post_device_cmd_*.py`.
- **Module Twin Operations**: Implemented independently in `extensions/iotedge_transport.py`, `routers/module_config/get_module_twin_config.py`, `routers/module_config/get_module_twin_identity_reported.py`, and `routers/module_config/post_module_twin_config.py`.
- **Device & Connection Queries**: Implemented in `extensions/iotedge_transport.py`, `routers/general/get_device_modules.py`, and `routers/general/get_device_connection_status.py`.

## Consolidation Target

Consolidate IoT Hub dispatch into a single unified transport service that standardizes:
1. Dynamic and configurable timeout handling.
2. Structured and typed exception handling (`IoTBackendAPIError`).
3. Consistent JSON response parsing.

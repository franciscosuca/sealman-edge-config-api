# Drift Workflow Analysis: IoT Hub Transport Implementation

This workflow diagram illustrates the logical divergence (drift) occurring between `extensions/iotedge_transport.py` and legacy implementations in `routers/`.

Interactive standalone HTML diagram: [drift_workflow.html](drift_workflow.html)  
Archify specification source: [drift_workflow.workflow.json](drift_workflow.workflow.json)

## Workflow Phases & Lanes

1. **Request Intake (Columns 0–1)**:
   - `User Request` enters the `API Router Layer`.
   - The router branches incoming traffic into either the **Extensions Path** or the **Legacy Path**.

2. **Transport Divergence (Columns 2–3)**:
   - **Extensions Path**: Uses `extensions/iotedge_transport.py` with a hardcoded `timeout=45s`.
   - **Legacy Path**: Calculates timeouts dynamically (e.g., 60s for device restarts) and executes ad-hoc timeout logic.
   - Both paths dispatch requests via `async_requests` to `Azure IoT Hub`.

3. **Response Resolution (Columns 4–5)**:
   - **Extensions Path**: Parses HTTP status codes into typed `IoTBackendAPIError` exceptions or returns parsed JSON.
   - **Legacy Path**: Manually checks status codes, returning raw tuples `(text, status_code)` or generic `HTTPException`.
   - Responses return to `User Reply` with diverging schemas and status code contracts.

## Summary of Divergence

| Feature | `extensions/iotedge_transport.py` | `routers/` (Legacy) |
| :--- | :--- | :--- |
| **Timeout Policy** | Hardcoded (45s) | Dynamic calculation (e.g., 60s for restarts) |
| **Error Handling** | Typed `IoTBackendAPIError` | Manual `HTTPException` / Raw tuples |
| **Response Format** | Parsed JSON object | Raw text/status code tuples |
| **Observability** | Standard logging | In-flight AuditTrail logging |

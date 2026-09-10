# Edge Config API

A FastAPI service for managing IoT Edge device module configurations via IoT Hub module twins and Azure Blob Storage. It exposes a REST API consumed by the Edge Config UI and third-party applications to push configuration files to edge devices asynchronously.

## Prerequisites

- Python 3.13+
- Running PostgreSQL instance (see [Database Setup](doc/database.md))
- Authentication provider configured (Entra ID or Keycloak — see [Authentication](doc/authentication.md))

Create a `.env` file and fill in the required values before starting:

```bash
cp .env.example .env   # adjust IOT_HUB_NAME, SAS_TOKEN, POSTGRES_URL, auth variables, …
```

## Run locally

### 1. Create and activate a virtual environment

**Windows**
```powershell
python -m venv .venv
.venv\Scripts\activate
```

**Linux / macOS**
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install dependencies

**For local development** (includes testing tools):
```bash
pip install -r requirements-dev.txt
```

**For production** (runtime dependencies only):
```bash
pip install -r requirements.txt
```

### 3. Start the server

**Option A — uvicorn**
```bash
python -m uvicorn main:app --host localhost --port 5000
```

**Option B — dev server** (host `localhost`, port `5000`)
```bash
python main.py
```

OpenAPI docs: <http://localhost:5000/docs>

## Extension system side apps

Alongside the public app, the extension system starts one additional,
independent ASGI app as an `asyncio` task on the same event loop as the
public app's own `lifespan` — not an `app.mount()` sub-app, not a separate
process:

| App | Purpose | Env vars | Default |
|---|---|---|---|
| Internal | Service-to-service router for an extension's own microservice to call back into, authenticated via `X-Internal-Key` | `EXTENSIONS_INTERNAL_API_HOST` / `EXTENSIONS_INTERNAL_API_PORT` | `0.0.0.0:8500` |

**The field-ingress side app (device → extension-module ingress, `X-Device-Key`
auth) is deliberately skipped for the whole implementation effort for now, not
just deferred to a later stage** — see `.mervin/IMPLEMENTATION-LOG.md`. No
`EXTENSIONS_FIELD_API_*` env vars, no device-key DB schema/routes exist yet.

Set `EXTENSIONS_ENABLED=false` to skip starting the side app (the static
`/extensions` management API on the public app is unaffected either way).

**Never publish this port to a host port mapping, and never add a
reverse-proxy/ingress rule that routes external traffic to it.** It binds
`0.0.0.0` because its real callers are other containers on the same
Docker/Kubernetes network (loopback would make it unreachable by anything,
including its intended callers) — network isolation must come from
deployment config (a dedicated internal network / `NetworkPolicy`), not the
bind address.


## Further documentation

| Topic | File |
|---|---|
| Architecture & module config mechanism | [doc/architecture.md](doc/architecture.md) |
| Authentication (Entra ID / Keycloak / RBAC) | [doc/authentication.md](doc/authentication.md) |
| Database setup & migrations | [doc/database.md](doc/database.md) |
| Running tests & coverage | [doc/testing.md](doc/testing.md) |

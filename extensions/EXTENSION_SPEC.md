# Writing an extension for `sealman-edge-config-api`

This is the extension-**author**-facing reference: what your own service/module must
do to register with, and be called correctly by, this platform's extension mechanism.
It ships next to the code it describes rather than duplicating the design rationale
already written down in `.mervin/extension-mechanism-implementation-plan.md` (this
repo's internal implementation plan) — read that instead if you need the "why", not
just the "what".

## 1. The registration manifest

`POST /extensions` (and `PUT /extensions/{name}` for a full replace) both take an
`ExtensionRegistration` JSON body. Don't hand-copy field names from this document or
from example manifests elsewhere in this repo's history — **the manifest's JSON Schema
is always available live and always in sync with the code**:

```
GET /extensions/schema
```

Validate your manifest against that schema offline (e.g. with any `jsonschema`-
compatible tool) before ever calling `POST /extensions`. Unlike a hand-maintained
reference doc, this can't drift from what the API actually accepts.

### `schema_version`

Every manifest must set `"schema_version": 1`. This is required — a missing or
unrecognized value is rejected outright (`422`) rather than guessed at, the same way
Azure IoT Edge's own `$edgeAgent`/`$edgeHub` module twins require a `schemaVersion`.
There is currently only one version; a future incompatible manifest shape would be
introduced as a new accepted value, not a silent reinterpretation of this one.

## 2. Authenticating on the internal channel

Routes registered with `"visibility": "internal"` are mounted on this API's separate
internal side app (a distinct ASGI app/port from the public one, never reachable from
outside the deployment's internal network — see the main
[README](../README.md#extension-system-side-apps)). Every request your own
microservice sends to one of your `internal` routes must carry:

```
X-Internal-Key: <key issued at rotation>
```

- The key is **never returned at registration time**. Call
  `POST /extensions/{name}/internal-key/rotate` to mint one — the raw key is returned
  **once**, in that call's response body only, and is never re-readable afterwards
  (not via `GET /extensions/{name}`, not anywhere else).
- **Design for rotation, not a fixed key lifetime.** Rotating immediately invalidates
  the previous key — there is no dual-key grace period. Your service's own
  configuration/secret-storage should make swapping this value a routine operation,
  not a rare one requiring a redeploy.
- A missing or non-matching `X-Internal-Key` on an `internal` route is rejected with
  `401` before your upstream is ever called.

Routes registered with `"visibility": "public"` are mounted on the platform's normal
public app and go through this repo's regular ABAC authorization
(`required_action`, if you set one on the route) — no `X-Internal-Key` involved.

## 3. `http` upstream health contract

If your upstream is `"type": "http"`, the platform can poll it for health (via
`POST /extensions/{name}/health-check`, manually triggered — never automatically on a
plain `GET`). To be checked correctly, your service should expose a lightweight route
at the path you declared as `health_path` (default `/health`) that:

- Returns a `2xx` status when healthy — anything else (`4xx`/`5xx`, timeout, refused
  connection) is reported as `unhealthy`.
- If you also set `expected_version` on your upstream's manifest entry, the response
  body must be JSON containing at least:

  ```json
  { "version": "<semver-or-pep440-string>" }
  ```

  (the field name is configurable via `version_field`, default `"version"`) — the
  platform checks this value against your declared `expected_version` and reports
  `unhealthy` on a mismatch, non-JSON body, or missing field.
- Leave `health_path`/`expected_version` unset entirely to opt out — an upstream with
  no health configuration always reports `unknown`, never a false `healthy`/`unhealthy`.

There is currently no equivalent self-description/health convention for `iotedge`
upstreams beyond the canary-device `runtimeStatus` check the platform already performs
using your module's own reported IoT Edge module twin state — nothing extra required
of the module itself.

## 4. Recommended (not required): a future self-description endpoint

Not part of this API today. A convention letting an extension's own `http` service (or
an `iotedge` module, via a `describe` direct method) describe its own capabilities
back to the platform — beyond what `body_ref`'s OpenAPI-schema fetch already covers —
is explicitly aspirational, not decided. If you're building a new extension today,
don't wait on this; register routes with an explicit, hand-written `body`/`example`
(or `body_ref` against your own `http` service's `openapi.json`) as described in the
registration schema above.

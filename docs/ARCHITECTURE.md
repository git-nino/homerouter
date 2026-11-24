# Homerouter architecture

This document describes the **current implementation** of Homerouter / Gateway
Monitor. The source code and tests in this checkout are authoritative; the older
phase audit and hardening reports are historical records and may contain stale
test counts or proposals that were never implemented.

Status labels used throughout the Phase 2 contract documents:

- **Implemented** — present in the live code path and covered by current tests.
- **Partial** — present, but narrower than the target architecture.
- **Planned** — proposed work, not implemented.
- **Not evaluated** — cannot be claimed from source or mocked tests; requires a
  physical device or separate acceptance evidence.

## Purpose and runtime shape

Homerouter is a single-tenant, local Flask application on port `7000`. It detects
a gateway, identifies a matching declarative connector profile without
authenticating, executes that profile after the user presses **Connect**, and
serves one normalized `UniversalModel` snapshot to the dashboard at `/api/data`.

```
Flask routes (app.py)
  ├─ gateway detection (gateway.detect_gateway)
  ├─ unauthenticated discovery via ConnectorExecutor.discover()
  ├─ ConnectorManager: catalog lookup, validation, identification, credential keys
  ├─ ConnectorExecutor: HTTP, auth flows, extraction, transforms, validation
  │    └─ auth_handlers.py: fixed, trusted Python handler registry
  └─ UniversalModel → public JSON → static dashboard (static/app.js)

Local speedtest-cli worker ── injects speed_test metrics into the model at read time
Legacy view builder (legacy_view.py) ── comparison only; not used by app.py
```

The dashboard never selects or displays a connector ID, evidence, or
compatibility details. The browser sends gateway, username, password, and an
optional manufacturer hint; the backend owns connector selection.

## Components

| Component | Status | Current responsibility |
|---|---|---|
| `app.py` | **Implemented** | Flask routes, single active session state, discovery orchestration, connect/data/disconnect, session, manufacturers, login defaults, speedtest worker and public model serialization. |
| `connector_manager.py` | **Implemented** | Loads catalog/credentials/schema, in-house schema validation, cross-checks, lookup, identification scoring, credential-suggestion lookup, compatibility context. |
| `connector_executor.py` | **Implemented** | Endpoint dependency ordering, dynamic value resolution, HTTP GET/POST, JSON parsing, extraction methods, transforms, validation, collections, derived aggregates, endpoint errors, one reauthentication on session expiry, explicit logout. |
| `auth_handlers.py` | **Implemented** | Fixed handler registry: `none`, `http_basic`, `http_digest`, `form`, `session`, `token`, `fiberhome_aes`. No profile text is evaluated as code. |
| `gateway.py` | **Partial** | Active: `detect_gateway()` and `fhencrypt()`. `RouterClient` and its plain/basic/fiberhome modes remain only for legacy comparison and regression; the live app does not call them. |
| `models/` | **Implemented** | Seven domains, 61 stable metric IDs, explicit availability states, source values, collections, connected devices, VoIP lines, safe `tel:` call actions. |
| `config/connectors.json` | **Implemented, partial coverage** | One enabled, observed real profile (`fiberhome.hg6145f1`) plus synthetic test profiles in tests. No Huawei or ZTE profiles. |
| `config/connector.schema.json` | **Implemented, partial validator** | Declarative profile contract. Validation uses an in-house subset of JSON Schema; see `docs/CONNECTOR_PROFILE_SPEC.md`. |
| `config/default_credentials.json` | **Implemented** | FiberHome pre-fill values and provenance; generic suggestion has no defaults. No fallback password is configured. Not stored in profiles or public responses. |
| `config/equipment.json` | **Implemented** | Manufacturer selector catalog (`fiberhome`, `huawei`, `zte`) with `profiles` arrays linking manufacturer IDs to connector IDs. Only FiberHome currently has a link. |
| `tools/connector_builder.py` | **Implemented** | `create`, `validate`, `validate-draft`, `import-draft`, `list`, declarative `configure`, canonical `map`, fixture-only `inspect`/`test`, trusted-handler/metric/manufacturer listings, and explicit `register --approve`. |
| `profile_safety.py` | **Implemented** | Vendor-neutral method/path/scheme/gateway validation, gateway-pinned URL construction, redirect rejection, timeout-safe requests, and bounded response size shared by builder, manager, executor, and API boundary. |
| `templates/index.html`, `static/app.js` | **Implemented** | Restored dashboard presentation with bindings to canonical metric IDs. Speedtest display reset is page-scoped; backend state is unchanged. |
| `legacy_view.py` | **Implemented, comparison only** | Historical normalization retained for physical-equivalence evidence; not imported by `app.py`. |

## Request flows

### 1. Discovery and login defaults

`GET /api/login-defaults` and `GET /api/discovery` call `app.run_discovery()`.
It detects the gateway, then iterates all enabled connector profiles, or only
the enabled profiles linked to an optional manufacturer selector. For each
candidate, `ConnectorExecutor.discover()` executes only endpoints declared
`auth: false` and referenced by `pre_auth` identification rules. Results are
matched with `ConnectorManager.identify()`.

Current behavior:

- No authentication is attempted during discovery.
- A selected manufacturer probes only its linked enabled profiles; an omitted
  selector keeps legacy all-profile discovery. Invalid selections fail closed.
- Pre-auth rule weights select the highest-scoring unambiguous candidate.
- Exact matches are downgraded to partial when multiple profiles match.
- Unknown firmware leaves `selected_profile` as `null`.
- Discovery errors are reported per profile and do not stop other profiles.
- The returned evidence and profile rows are internal API detail; they are not
  shown in the login form.

`/api/login-defaults` exposes only the gateway, a supported flag, editable
username/password defaults, and an unsupported message. It never exposes a
profile ID, confidence, evidence, or discovery errors.

### 2. Connect and authentication

On `POST /api/connect`, the backend validates credentials, accepts and validates
an optional `manufacturer_id`, passes it into discovery, and resolves the
connector internally. A client-supplied `profile_id` is ignored.

`ConnectorExecutor.connect()` tries configured auth flows in `order`. Each flow
starts with reset auth state. The first flow that passes its success check wins;
if none succeed, connection fails without falling back to arbitrary login
methods. Login credentials submitted by the user are used exactly once; no
default substitution or fallback retry is performed.

### 3. Data collection

`GET /api/data` calls `executor.collect()`:

1. Skip auth and logout endpoints; execute the remaining declared endpoints in
   dependency order.
2. Capture each endpoint error independently.
3. Apply extraction rules by metric or collection, with priority ordering and
   fallback rules.
4. Apply transforms, conditions, and validation rules.
5. Derive connected devices/count and the primary VoIP line/call action.
6. Attach compatibility context internally, then remove `profile_id` and
   `compatibility` before public serialization.
7. Inject current local speedtest state into the `speed_test` domain.

If a session-validity field signals expiry, the executor reauthenticates once.
A second expiry raises `SessionExpired`, and `app.py` clears session state and
returns HTTP 401.

### 4. Disconnect

`POST /api/disconnect` clears local state immediately, then calls the selected
connector's explicit logout endpoint when one exists. A router logout failure is
reported in the response but does not prevent local disconnect.

### 5. Speedtest

`POST /api/speedtest` starts a daemon thread running `_speed_worker()` with a
token and a 120-second watchdog. `GET /api/speedtest/status` returns the current
status without the internal token. Results remain in backend state for API and
diagnostic compatibility; a fresh dashboard page does not replay them because
the display reset is page-scoped in `static/app.js`.

The service worker bypasses all `/api/` requests, so neither discovery nor
Speedtest status is served from the PWA cache.

## Trust boundaries and invariants

- Connector profiles are declarative JSON only. Paths, regexes, transforms, and
  operators are interpreted by fixed Python implementations; no profile field
  is executed as code.
- `profile_safety.py` pins every executor request to the configured bare gateway
  host. Unsafe methods, schemes, paths, userinfo, ports, metadata hosts, and
  redirects are rejected; response bodies are size-bounded.
- Profile Builder inspection and testing are fixture-only and credential-free.
  They never instantiate a live session or contact a router.
- Only trusted Python modules in `auth_handlers.py` may implement auth logic.
- Profiles contain no literal secrets. The manager scans for secret-like keys
  and values; credential values live in `config/default_credentials.json` as
  editable suggestions with provenance.
- Structural validity is not compatibility. Compatibility stays
  `NOT_EVALUATED` until a physical ONT comparison is recorded.
- Missing, unsupported, invalid, unloaded, or failed readings are explicit
  availability states with `value: null`; they are never fabricated as `0`,
  `false`, or an empty string.
- `source_value` preserves the original connector value for diagnostics.
- `devices.count` is derived only from a loaded device collection.
- `call_action` is enabled only for a validated phone number and is triggered by
  an explicit user tap that opens the native dialer via a safe `tel:` URI.

## Known limitations and planned work

- **Implemented:** discovery probes every enabled profile only when the request
  omits `manufacturer_id`; a selected manufacturer constrains candidates to its
  linked enabled profiles; see `docs/DISCOVERY_SPEC.md`.
- **Partial:** `SchemaValidator` implements only the JSON Schema keywords used
  by the shipped schema, not full Draft 2020-12.
- **Partial:** no Huawei or ZTE connector profiles exist; only the FiberHome
  profile and synthetic test profiles exist.
- **Partial:** XPath/CSS extraction is explicitly rejected. Retries and
  per-domain refresh policies are not implemented.
- **Implemented:** the Profile Builder validates profiles and runs redacted,
  credential-free dry runs against sanitized offline fixtures; physical
  compatibility is still not evaluated.
- **Not evaluated:** no physical FiberHome old-vs-new normalized-output
  equivalence evidence exists. Mocked HTTP tests are not compatibility proof.
- **Implemented:** M2 adds explicit manufacturer/model/firmware metadata and
  manufacturer-constrained discovery while preserving legacy clients that omit
  `manufacturer_id`.
- **Implemented:** M3 adds offline Profile Builder inspection/testing and
  explicit catalog/manufacturer registration without changing frozen API shapes.

## Related contracts

- `docs/DATA_MODEL.md` — normalized model and `/api/data` contract.
- `docs/CONNECTOR_PROFILE_SPEC.md` — profile structure and validation.
- `docs/AUTHENTICATION_SPEC.md` — trusted auth handlers and FiberHome AES.
- `docs/DISCOVERY_SPEC.md` — identification and discovery APIs.
- `docs/PROFILE_BUILDER_SPEC.md` — profile authoring workflow.
- `docs/MIGRATION_PLAN.md` — compatibility invariants and remaining milestones.

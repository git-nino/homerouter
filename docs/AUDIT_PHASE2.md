# Phase 2 — Universal Runtime Integration

## Goal

Make the JSON connector catalog the **live** data path and make `UniversalModel`
the only contract between collection and the dashboard, while keeping the
previous dashboard's presentation and a simple login experience.

The implemented flow is:

```
Gateway detection
  → Internal unauthenticated identification
  → ConnectorManager
  → config/connectors.json
  → ConnectorExecutor
  → authentication handlers + HTTP requests
  → profile-driven extraction/transforms/validation
  → UniversalModel
  → restored dashboard
```

## Restored UI source

The visual UI was restored from Git commit **`3bd1d55`**, the last commit before
the universal-runtime dashboard rewrite. `templates/index.html` and
`static/style.css` were copied from that commit. The only template removals are
the obsolete `modeInput` authentication-mode selector and the `genericPanel`
raw-data panel. The CSS is byte-for-byte identical to `3bd1d55`.

`static/app.js` keeps the restored DOM, event behavior, speedtest panel, refresh
interval and call-button interaction, but its data binding was replaced with
canonical `UniversalModel` metric IDs and availability states. Connector
profiles, discovery evidence, compatibility messages and manufacturer details
were removed from the user interface.

## Files changed or added

| File | Role in Phase 2 / restoration |
|---|---|
| `app.py` | Discovery, internal connector selection, simple `/api/login-defaults`, profile-driven connect/data/disconnect APIs and serialized `UniversalModel`. Public payload strips `profile_id` and compatibility details. No vendor-specific normalization. |
| `auth_handlers.py` | Controlled auth registry (`none`, `http_basic`, `http_digest`, `form`, `session`, `token`, `fiberhome_aes`). The FiberHome handler delegates AES encryption to `gateway.fhencrypt`; profile text is never evaluated as code. |
| `connector_executor.py` | Loads an enabled profile, orders endpoint dependencies, resolves dynamic values, issues GET/POST with query/header/body, parses JSON, applies extraction and transforms, records endpoint errors, derives collections/counts/call actions, validates values, handles session expiry and logs out explicitly. |
| `legacy_view.py` | Historical FiberHome `build_view()` moved here **only** for regression comparison; it is not imported by `app.py` or the dashboard. |
| `connector_manager.py` | Catalog validation, identification scoring and credential-suggestion lookup. |
| `models/universal.py` | Normalized seven-domain model with explicit availability states, collections, endpoint errors and safe dialer actions. |
| `config/connector.schema.json` | Runtime profile contract, including logout, session validity, collections, transforms and fallbacks. |
| `config/connectors.json` | FiberHome runtime profile. No credential values are stored. |
| `config/default_credentials.json` | Editable pre-fill values retained from the pre-universal UI (`user` / `user1234`) and their provenance. No fallback credential is configured. These are convenience defaults, not stored secrets. |
| `templates/index.html`, `static/style.css` | Restored from `3bd1d55`, minus the obsolete auth-mode selector and generic panel. |
| `static/app.js` | Restored interactions with universal-model data binding, stale refresh messaging and a profile-free login payload. |
| `static/sw.js`, `static/manifest.webmanifest` | Cache bumped to `gwmon-v7`; local PWA icon references. |
| `static/dashboard-icons-router.svg`, `static/icon-*.png`, `static/favicon.png`, `docs/ICON_ATTRIBUTION.md` | Locally served Dashboard Icons `router` icon and attribution. |
| `tests/` | Expanded executor, API, login, restored-UI, icon and second-manufacturer integration coverage. |
| `README.md`, this document | Updated usage and runtime documentation. |

## Behavioral guarantees

- The browser never sends, selects or displays a connector profile ID.
- `/api/login-defaults` performs unauthenticated identification internally and
  returns only gateway address, supported flag and editable credential defaults.
- Authentication happens only after **Connect**. The backend resolves the
  connector, retrieves its credential suggestion and invokes that connector's
  authentication flow.
- Unknown firmware leaves the connector unresolved, returns the simple
  unsupported-gateway message and does not try arbitrary authentication.
- The submitted username and password are used exactly once; no fallback
  credential is substituted or retried.
- The selected connector determines request fields and extraction. A second
  manufacturer profile requires no `app.py` or `static/app.js` change.
- Missing, unsupported, invalid and failed fields never become `0`, `false` or
  an empty string. They are explicit `Availability` states with `value: null`.
- A failed refresh keeps the last rendered values and marks the footer as stale.
- The connected-device count is derived only from a loaded collection.
- The dialer action is generated only from a validated phone number and is
  triggered by an explicit tap. No call is placed automatically.
- No credential values are copied into connector profiles, logs, success
  messages or the public dashboard payload.
- JSON cannot execute code: expressions are parsed only by fixed path, regex and
  transform implementations; unsupported methods fail explicitly.

## Test evidence

Commands run in the writable integration checkout:

```
python -m py_compile app.py connector_executor.py auth_handlers.py \
  connector_manager.py legacy_view.py models/universal.py tests/*.py
node --check static/app.js
python -m unittest discover -s tests -q
python -m tools.connector_builder validate
```

Result: **94 tests OK**; `node --check` clean; catalog structural validity
**PASS**, 1 connector, 0 errors, 0 warnings.

The integration tests prove:

1. profile load/validation and internal identification from a discovery observation;
2. endpoint dependency order, auth, dynamic values, GET/POST, session state and explicit logout;
3. extraction into `UniversalModel`, serialization, collection/device count and validated call action;
4. endpoint failures and missing fields produce error/unsupported states with no fabricated values;
5. one automatic reauthentication after a profile session-expiry signal;
6. profile text cannot execute code;
7. a second synthetic manufacturer profile is served by `/api/data` and rendered by the unchanged UI;
8. gateway/username/password defaults are returned without authentication;
9. a client-supplied profile ID is ignored and connector selection happens in the backend;
10. user-overridden credentials are used exactly as submitted, with no silent fallback;
11. unsupported gateways do not authenticate and return only a simple message;
12. `app.py`, the template and the script contain no legacy vendor normalization or response-key fallbacks;
13. the restored template and stylesheet match the `3bd1d55` presentation contract;
14. PWA icon source, PNG sizes, favicon, manifest and service-worker cache are present.

## Physical-device acceptance — NOT COMPLETE

Unit/integration tests with mocked HTTP responses are **not** proof of physical
ONT compatibility. Before claiming support for the FiberHome HG6145F1, the
physical gateway must be exercised and the normalized output of the new path
compared with the legacy comparison builder for at least:

- identity/firmware/system/optical/WAN fields;
- connected-device list and derived count;
- VoIP number and registration state (redacted in any report);
- explicit logout followed by a fresh login.

Until that comparison is done, the catalog and live compatibility state remain
`NOT_EVALUATED`/`IDENTIFICATION_INCOMPLETE`. The existing profile status remains
`observed`, not `verified`.

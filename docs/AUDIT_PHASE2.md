# Phase 2 — Universal Runtime Integration

## Goal

Make the JSON connector catalog the **live** data path and make `UniversalModel`
the only contract between collection and the dashboard.

The implemented flow is:

```
Gateway detection
  → Device identification (unauthenticated discovery)
  → ConnectorManager
  → config/connectors.json
  → ConnectorExecutor
  → authentication handlers + HTTP requests
  → profile-driven extraction/transforms/validation
  → UniversalModel
  → unified dashboard
```

## Files changed or added

| File | Role in Phase 2 |
|---|---|
| `app.py` | Replaced `RouterClient`/`build_view()` with discovery, profile-driven connect/data/disconnect APIs and serialized `UniversalModel`. Local speedtest results are injected into the `speed_test` domain. |
| `auth_handlers.py` | Controlled auth registry (`none`, `http_basic`, `http_digest`, `form`, `session`, `token`, `fiberhome_aes`). The FiberHome handler delegates encryption to `gateway.fhencrypt`; profile text is never evaluated as code. |
| `connector_executor.py` | Loads an enabled profile, orders endpoint dependencies, resolves literal/user/state/derived/seed/endpoint values, issues GET/POST with query/header/body, parses JSON, applies extraction methods and transforms, records endpoint errors, derives collections/counts/call actions, validates values, handles session expiry and logs out explicitly. |
| `legacy_view.py` | Historical FiberHome `build_view()` moved here **only** for regression comparison; it is not imported by `app.py` or the dashboard. |
| `connector_manager.py` | Identification rules now carry their field/value, supporting discovery display and profile suggestions. |
| `models/universal.py` | Added `device.gpon_sn`, generic `collections`, `endpoint_errors`, `set_collection()`/`get_collection()`/`add_endpoint_error()`, and serialization for all of them. |
| `config/connector.schema.json` | Added runtime profile fields: logout endpoint, session validity, collection extraction, item fields, filters, ranges, transform options, defaults and fallbacks. |
| `config/connectors.json` | FiberHome profile now includes runtime endpoints, AES auth, role-derived logout, collections, system/optical/VoIP extraction and validation. No secret values are stored. |
| `templates/index.html`, `static/app.js`, `static/style.css`, `static/sw.js` | Discovery/profile UI, five normalized dashboard sections, explicit availability states, validated native-dialer call action, removal of auth-mode/generic-panel legacy paths, cache bump to `gwmon-v6`. |
| `tests/fixtures.py`, `tests/test_connector_executor.py`, `tests/test_generic_dashboard.py` | Integration coverage for a second synthetic manufacturer, executor behavior, API rendering, discovery and static-contract checks. |
| `tests/test_app_voice.py`, `tests/test_dashboard_order.py`, `tests/test_ui_polish.py` | Updated for legacy comparison only and the normalized UI/cache contract. |
| `README.md`, this document | Updated usage and runtime documentation. |

## Behavioral guarantees

- Discovery runs only endpoints marked `auth: false`. It does **not** authenticate.
- Unknown firmware leaves the profile selection empty; the user must choose a
  profile explicitly. A matched partial/exact profile still requires the user to
  press **Connect**.
- The selected profile determines auth flow, request fields and extraction.
  Adding a second manufacturer profile requires no `app.py` or `static/app.js`
  changes.
- Missing, unsupported, invalid and failed fields never become `0`, `false` or
  an empty string. They are explicit `Availability` states with `value: null`.
- The connected-device count is derived only from a loaded collection.
- The dialer action is generated only from a validated phone number and is
  triggered by an explicit tap. No call is placed automatically.
- No secret values are stored in profiles, the source tree, logs, success
  messages or the public API.
- JSON cannot execute code: expressions are parsed only by the fixed path,
  regex and transform implementations; unsupported methods fail explicitly.

## Test evidence

Commands run in the writable integration checkout:

```
python -m py_compile app.py connector_executor.py auth_handlers.py \
  connector_manager.py legacy_view.py models/universal.py tests/*.py
node --check static/app.js
python -m unittest discover -s tests -q
python -m tools.connector_builder validate
```

Result: **83 tests OK**; `node --check` clean; catalog structural validity
**PASS**, 1 connector, 0 errors, 0 warnings.

The new integration tests prove:

1. profile load/validation and identification from a discovery observation;
2. endpoint dependency order, auth, dynamic values, GET/POST, session state and
   explicit logout;
3. extraction into `UniversalModel`, serialization, collection/device count and
   validated call action;
4. endpoint failures and missing fields produce explicit error/unsupported
   states with no fabricated values;
5. one automatic reauthentication after a profile session-expiry signal;
6. profile text cannot execute code;
7. a second synthetic manufacturer profile is served by `/api/data` and rendered
   by the unchanged UI;
8. unknown firmware does not auto-select a profile or authenticate;
9. `app.py`, the template and the script contain no legacy vendor normalization
   or response-key fallbacks.

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

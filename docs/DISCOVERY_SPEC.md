# Discovery and identification contract

**Status:** rule-based discovery, deterministic manufacturer-constrained
discovery, and offline manufacturer registration are **implemented** (M2
deployed, M3 implemented); legacy omitted-selector discovery remains unchanged.

Authoritative files: `app.py` (`run_discovery()`, routes), `gateway.py`
(`detect_gateway()`, `probe()`), `connector_manager.py` (`identify()`),
`profile_safety.py` (gateway validation), `config/connectors.json`,
`config/equipment.json`.

## Gateway detection

`gateway.detect_gateway()` probes, in order, the system default gateway,
locally derived subnet candidates, and a short list of common gateway IPs. A
probe counts as a gateway when a model hint or router-like page content is
found. It returns:

```json
{ "gateway": "192.168.1.1", "model": "", "detected": true }
```

If nothing responds, `detected` is `false` and a fallback address is still
returned so a user can type the gateway manually.

## Profile identification

`app.run_discovery()` caches results for the current host, detection flag,
manufacturer selector, and equipment catalog fingerprint unless `force=True`.
A client-specified gateway in `/api/connect` forces a fresh discovery for that
host. That host is validated by `profile_safety.normalize_gateway()`: only a bare
host is accepted. URLs, userinfo, ports, loopback/link-local/unspecified/reserved
addresses, `localhost`, and metadata hostnames are rejected with HTTP 400;
private LAN router addresses remain valid.

For each candidate connector profile (all enabled profiles when the request omits
a manufacturer; otherwise only associated enabled profiles):

1. `ConnectorExecutor.discover()` collects only endpoints that are both
   `auth: false` and referenced by a `pre_auth` identification rule.
2. Endpoint errors are captured as `endpoint_id: error` strings and do not stop
   other profiles.
3. `ConnectorManager.identify()` scores observation rules for that profile.
4. The candidate with the greatest sum of matched rule weights wins.
5. An ambiguous result downgrades `exact` to `partial` and prevents automatic
   selection.
6. With no candidate, `selected_profile` is `null` and the evidence string
   begins with `Unknown firmware`.

Discovery never calls `connect()`, never sends credentials, and never tries
arbitrary auth methods.

Confidence values are `exact`, `partial`, and `unknown`. The shipped FiberHome
pre-auth rule is `partial`, so model name alone is not treated as
firmware-unique.

## `/api/discovery` response

Exact top-level keys:

```text
gateway
detected
gateway_detected
detected_device
selected_profile
identification_confidence
evidence
credential_suggestion
profiles
discovery_errors
physical_device_check_required
```

Each row in `profiles` has:

```text
id, display_name, status, identification_confidence, capabilities,
device_compatibility
```

`device_compatibility` is always `"not_evaluated"` from catalog evidence.

Each `credential_suggestion` is sanitized to:

```text
key (when configured), label, username_source, password_source,
requires_user_confirmation, provenance
```

It never contains a username, password, or fallback value.

`physical_device_check_required` is always `true`.

## `/api/login-defaults` response

Exact keys:

```text
gateway, gateway_detected, supported, username, password, message
```

- `supported` is true only when discovery selected a profile.
- `username` and `password` are editable pre-fill values from the selected
  profile's credential suggestion. Unsupported gateways use the generic
  suggestion, which has no defaults and returns empty strings; no credentials
  are copied from FiberHome or another manufacturer.
- `message` is `null` when supported, otherwise the standard unsupported
  message: "Unsupported or unidentified gateway. This device does not yet have
  a compatible connector profile."

The endpoint must not expose `profile`, `profile_id`, `selected_profile`,
`evidence`, `confidence`, `manufacturer`, or `discovery_errors`.

## Manufacturer selector

`config/equipment.json` supplies `/api/manufacturers`:

```text
ok, manufacturers, message
```

Each manufacturer entry exposed to the UI is `{id, name}`. A missing, empty,
malformed, or unsupported catalog degrades gracefully to
`ok: false`, `manufacturers: []`, and a safe message.

`/api/connect` accepts `manufacturer_id` and the legacy alias `manufacturer`:

- A known ID is validated and stored in the session.
- An unknown non-empty ID is rejected with HTTP 400.
- Requests without either field remain valid; the resulting value is `""`.
- A client-supplied `profile_id` never influences selection.

## Implemented manufacturer-constrained behavior

Connector profiles may declare optional `manufacturer_id`, `model`, and
`firmware` metadata. The `profiles` array in `config/equipment.json` links a
manufacturer ID to connector IDs and is authoritative for candidate membership.
A profile that declares a different `manufacturer_id` is skipped as an invalid
reference; a profile with no metadata remains eligible for legacy catalogs.
`model` and `firmware` are informational only and never expand or narrow
selection.

- A known manufacturer probes only its linked enabled profiles, in the order
  listed in that manufacturer's `profiles` array. No matching candidate produces
  `selected_profile: null`, the standard unsupported login-defaults message, and
  evidence beginning "Unknown firmware".
- An unknown or unvalidatable manufacturer never falls back to all profiles.
  `/api/connect` rejects it with HTTP 400; direct discovery fails closed.
- Omitting `manufacturer_id` preserves legacy behavior: all enabled profiles are
  probed and scored exactly as before.
- The discovery cache is keyed by manufacturer, host, detection state, and a
  deterministic SHA-256 fingerprint of the equipment catalog. A catalog load
  error, an empty/invalid association set, or a changed association set cannot
  reuse a stale result. Omitted-selector discovery is also revalidated when the
  catalog changes, but still probes all enabled profiles.
- Discovery remains unauthenticated, and `/api/discovery` and
  `/api/login-defaults` keep their exact response shapes.
- `static/app.js` passes the current manufacturer selection to
  `/api/login-defaults`, so prefill/messages use the same constrained discovery
  as the subsequent `/api/connect` request.
- Manual model registration is an offline CLI operation that adds a reviewed
  connector ID to the correct `profiles` array; it never adds a runtime route,
  profile ID to the browser, or fallback to all manufacturers.
- `physical_device_check_required` stays `true`; profile rows still report
  `device_compatibility: "not_evaluated"`.

# Connector profile specification

**Status:** foundation, optional manufacturer/model/firmware metadata, and
Profile Builder registration support are **implemented** (M2 deployed, M3
implemented). The shipped catalog is valid and live, but it contains only one
enabled real profile (`fiberhome.hg6145f1`) and synthetic test profiles. No
Huawei or ZTE profiles exist.

Authoritative files:

- `config/connector.schema.json` — declarative profile schema.
- `config/connectors.json` — shipped catalog.
- `config/default_credentials.json` — credential suggestions and provenance.
- `config/equipment.json` — manufacturer selector catalog; its `profiles`
  arrays link manufacturer IDs to connector IDs for constrained discovery and
  manual registration.
- `profile_safety.py` — shared method/path/scheme/gateway/redirect/size rules.

Profiles never contain executable code. Every expression is interpreted by a
fixed Python implementation in `connector_manager.py` or
`connector_executor.py`.

## Catalog shape

```json
{
  "schema_version": 1,
  "connectors": [ /* connector objects */ ]
}
```

`schema_version` must be the integer `1`. Connector IDs must match
`^[a-z0-9]+(?:\.[a-z0-9_-]+)+$`, for example `fiberhome.hg6145f1`.

## Connector object

Required fields:

`id`, `display_name`, `status`, `evidence`, `identification`, `auth`,
`endpoints`, `extraction`, `validation`, `dependencies`, `capabilities`

Optional fields:

`enabled`, `manufacturer_id`, `model`, `firmware`, `scheme`, `generated_by`, `source_har`,
`requires_manual_auth_handler`, `auth_observations`,
`identification_candidates`, `extraction_candidates`,
`metric_mapping_candidates`, `limitations`, `manual_review`

- `status` is one of `draft`, `observed`, `verified`, `deprecated`.
- `enabled` defaults to `true` in runtime lookup. Drafts must be `false`.
- `scheme` is `http` or `https`; `http` is the executor default.
- Unknown connector fields are rejected by `additionalProperties: false`.

Optional `manufacturer_id`, `model`, and `firmware` are declarative strings only.
They contain no executable code, credentials, or compatibility claims.
`manufacturer_id` acts as a safety guard against a conflicting equipment link;
`model` and `firmware` are informational. Legacy profiles may omit all three.

### Evidence

`evidence` requires `source` and `verified`, with optional `notes`. Evidence
describes what was observed; it is not a claim of full device compatibility.
Sensitive values must be redacted.

### Identification

`identification.confidence` is `exact`, `partial`, or `unknown`. Each rule
requires:

`id`, `phase`, `source`, `field`, `operator`, `value`, `confidence`

Optional: `weight`, `evidence`.

- `phase` is `pre_auth` or `post_auth`. Discovery uses only `pre_auth`.
- `source` names a probe observation key, such as `device_name` or `challenge`.
- `field` supports dotted paths and indexed segments.
- `operator` is `equals`, `not_equals`, `contains`, `prefix`, or `regex`.
- Partial evidence may not back an `exact` claim; conflicting exact rules across
  profiles are rejected.

### Authentication

`auth` requires `flows`; optional `credential_suggestion_key` and
`logout_endpoint`. Each flow requires `id`, `type`, `order`, and `success`.
Optional fields are `endpoints`, `fields`, and `session`.

`type` must be one of the trusted handler identifiers:

`none`, `http_basic`, `http_digest`, `form`, `session`, `token`,
`fiberhome_aes`

`endpoints` maps logical names such as `seed`, `login`, `preflight`, or
`challenge` to endpoint IDs. `success` declares a response field and an
`allowed` or `not_equal` expectation. `session` may declare `id_field`,
`store`, `validity_field`, `invalid_values`, and transport hints.

No credential value may be stored in a profile. Profiles reference a suggestion
by key only.

### Endpoints

Each endpoint requires `id`, `method`, `path`, `format`, and
`timeout_seconds`. Optional fields: `scheme`, `auth`, `headers`, `query`,
`body`, `body_format`, `description`.

- `method` is `GET` or `POST`.
- `format` is `json`, `html`, `xml`, or `text`; the executor parses JSON and
  retains raw text for regex extraction.
- `body_format` is `form` or `json`.
- `auth: false` marks an endpoint as unauthenticated. Discovery executes only
  these endpoints.

Dynamic values use an explicit object form:

```json
{ "source": "state", "field": "sessionid" }
```

Supported sources: `literal`, `user_input`, `state`, `derived`, `seed`, and
`endpoint_field`. Dotted shorthand such as `"source": "state.sessionid"` is
normalized by the executor. `endpoint_field` may apply a value `mapping`.

### Extraction and transforms

Each extraction rule requires `id`, `endpoint`, `method`, and `expression`.
Exactly one of `metric` or `collection` should identify the target. Rules may
also declare `priority`, `when`, `when_all`, `transforms`,
`transform_options`, `item_fields`, `range`, `filter`, `source_value`,
`value_type`, and `availability_on_missing`.

Supported extraction methods:

`json_path`, `json_path_multi`, `list_length`, `list_objects`, `keyed_objects`,
`regex`, `header`, `literal`

`xpath` and `css` are explicitly unsupported and fail at runtime.

Supported transforms:

`strip`, `decode_point_encoded_ip`, `enabled_flag`, `integer`, `number`,
`split_before_at`, `voip_status_label`, `used_percent`

Supported condition operators:
`equals`, `not_equals`, `contains`, `prefix`, `regex`.

When no rule produces a value, the target metric becomes
`availability_on_missing` (default `unsupported`), or `error` when the source
endpoint failed. Collections become unloaded with an error when applicable.

### Validation

Each rule requires `metric`, `rule`, and `on_failure`. Supported rules:

`ipv4`, `ipv6`, `number`, `non_empty`, `one_of`, `phone_e164`, `dbm_range`,
`mac`, `duration_seconds`

A failed rule sets the metric value to `null`, preserves `source_value`, and
applies `on_failure` (commonly `invalid` or `unavailable`).

### Dependencies and capabilities

`dependencies` maps an endpoint ID to endpoint IDs that must execute first.
Cycles are rejected. `capabilities` names supported model domains, for example
`device_identity`, `wan`, `optical`, `voip`, `system`, `connected_devices`.

## Validation model

`ConnectorManager.validate()` combines:

1. An **in-house JSON Schema subset** implemented in `SchemaValidator`. It
   supports the keywords used by this schema: `$ref`, `type`, `const`, `enum`,
   `minLength`, `pattern`, `minimum`, `maximum`, `exclusiveMinimum`,
   `minItems`, `uniqueItems`, `items`, `minProperties`, `required`,
   `properties`, and `additionalProperties`. It is **not** a full JSON Schema
   Draft 2020-12 implementation; no external dependency is added.
2. **Cross-checks** for duplicate IDs, evidence quality, identification
   confidence/conflicts, auth handler and credential keys, endpoint runtime
   values, extraction methods/metrics/transforms, validation rules,
   dependency cycles, draft metadata, disabled profiles, secret-like values,
   unsafe endpoint methods/paths, and manufacturer/profile cross-references from
    `config/equipment.json` including dangling references.
3. **Credential checks** requiring explicit user confirmation metadata.

The report separates `structural_valid` from `device_compatibility`. Structural
validity never implies physical compatibility; compatibility remains
`not_evaluated`.

## Draft workflow

Generated drafts are `status: "draft"`, `enabled: false`, `evidence.verified:
false`, contain `__OBSERVE_*__` placeholders, and list `manual_review` steps.
`validate-draft` applies the same checks plus draft-specific rules.
`import-draft` requires explicit `--approve`, refuses duplicate IDs, keeps the
imported profile disabled, never rewrites existing entries, and verifies the
written catalog. See `docs/PROFILE_BUILDER_SPEC.md`.

## Current real profile

`fiberhome.hg6145f1` is enabled with `status: "observed"`. It declares a
pre-auth model rule for `HG6145F1`, a `fiberhome_aes` login flow, explicit
logout, 52 extraction rules across device identity, WAN, optical, system, VoIP,
and collections (`devices`, `lan_ports`, `ssids`), plus validation rules. No
credential values, phone numbers, public IPs, or serial numbers are stored.

Known limitations are recorded in the profile: only one observed device, local
Speedtest rather than router Speedtest, unmapped IPv6/USB fields, and the need
for physical comparison before compatibility can be claimed.

## M2 gap closure

Implemented and deployed: profiles may carry optional explicit
`manufacturer_id`, `model`, and `firmware` metadata; `config/equipment.json`
links manufacturers to connector IDs; and discovery constrains candidates by
the selected manufacturer. Requests that omit the selector keep legacy
all-profile discovery. Structural validity still does not imply physical
compatibility.

## M3 Profile Builder support

The builder can configure declarative endpoints, list response field paths from
sanitized fixtures, map observed fields to canonical metrics, and register the
reviewed connector ID in `config/equipment.json`. Registration requires explicit
approval, checks metadata agreement and duplicate model IDs, rejects dangling
references, writes both catalogs atomically, and disables new profiles unless
verified manual evidence exists. No profile can execute code, open a live
network session, or claim physical compatibility from a mocked test.

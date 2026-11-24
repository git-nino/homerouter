# Profile Builder specification

**Status:** draft authoring, static validation, offline fixture inspection,
credential-free dry runs, and catalog registration are **implemented**. Physical
hardware verification remains **not evaluated**.

Authoritative file: `tools/connector_builder.py`, using `ConnectorManager`,
`profile_safety.py`, `config/connector.schema.json`, `config/connectors.json`,
`config/equipment.json`, and `config/default_credentials.json`. The CLI adds no
dependency beyond the runtime's existing modules.

## Supported workflow

1. Select the manufacturer in the UI or CLI catalog (`/api/manufacturers` or
   `manufacturers`); candidate profiles are constrained by `config/equipment.json`.
2. Run unauthenticated discovery. If no profile matches, manually author a
   declarative draft from sanitized observations; never guess values or create a
   fake model.
3. Configure only the observed endpoint metadata with `configure`.
4. Inspect response field paths with `inspect` using a sanitized fixture.
5. Map observed fields to canonical metric IDs with `map`.
6. Validate the draft and run the offline read-only `test` command.
7. Register the reviewed profile and equipment association with `register
   --approve`, then follow the normal deployment process to reload the service.

The browser continues to send only gateway, credentials, and an optional
manufacturer selector. The backend owns connector selection and ignores any
client-supplied `profile_id`.

## Commands

```bash
python -m tools.connector_builder create \
  --id vendor.model --display-name "Vendor Model"

python -m tools.connector_builder create \
  --id vendor.model --display-name "Vendor Model" \
  --output output/connectors.json

python -m tools.connector_builder validate [--json]
python -m tools.connector_builder validate-draft output/vendor.model.json [--json]
python -m tools.connector_builder list [--json]

python -m tools.connector_builder auth-handlers [--json]
python -m tools.connector_builder metrics [--json]
python -m tools.connector_builder manufacturers [--json]
python -m tools.connector_builder models [--json]

python -m tools.connector_builder configure output/vendor.model.json \
  --manufacturer vendor --model Model \
  --endpoint-id root --path /cgi-bin/ajax --method GET \
  --auth-handler none

python -m tools.connector_builder map output/vendor.model.json \
  --endpoint root --field '$.Status.Model' --metric device.model

python -m tools.connector_builder inspect output/vendor.model.json \
  --fixture output/vendor.model.fixture.json [--json]

python -m tools.connector_builder test output/vendor.model.json \
  --fixture output/vendor.model.fixture.json [--json]

python -m tools.connector_builder register output/vendor.model.json \
  --manufacturer vendor --model Model --approve
```

`create` without `--output` prints an inert draft to stdout. With `--output`, it
appends a draft to a catalog and refuses duplicate IDs. `auth-handlers` lists
the closed trusted identifier set; `metrics` lists canonical metric IDs and
domains; `manufacturers`/`models` expose the registration catalogs locally.

`inspect` and `test` are fixture-only. There is no `--live`, `--network`,
`--host`, or real-credential option. The executor receives a deterministic
in-memory session; no `requests.Session()` is created on this path.

Exit codes: `0` success, `1` validation/execution refusal, `2` missing explicit
approval. Invalid paths, fixtures, metrics, and gateway values fail closed with
`SystemExit`.

## Draft safety rules

A generated draft is deliberately non-functional:

- `status: "draft"`
- `enabled: false`
- `evidence.verified: false`
- `identification.confidence: "unknown"`
- `__OBSERVE_*__` placeholders for model and path
- no extraction rules
- explicit `limitations` and `manual_review` steps

`validate-draft` adds draft-specific errors when status is not `draft`, the
draft is enabled, evidence is verified, identification claims `exact`, or
manual review is missing. It warns when limitations, generator name, or HAR
source are missing.

`import-draft` requires explicit `--approve`. It revalidates the draft, rejects
duplicate IDs, appends a disabled copy, revalidates the combined catalog in
memory, writes atomically with `os.replace()`, and verifies that existing
entries are unchanged.

## Declarative configuration boundaries

`configure` writes only JSON fields already allowed by the schema. Endpoint
methods are restricted to `GET`/`POST`, paths must be single leading-slash paths
without traversal, query text, fragments, encoded separators, backslashes, or
network-path references, and authentication is selected only by a trusted
identifier from `auth-handlers`. No profile field is evaluated as Python code,
dynamically imported, or passed to a shell.

`map` accepts only canonical `UniversalModel` metric IDs and JSON-path-like
field expressions. It records a manually reviewed mapping candidate and adds the
metric's domain to `capabilities`; it never declares hardware compatibility.

## Offline fixture contract

```json
{
  "schema_version": 1,
  "gateway": "192.0.2.1",
  "responses": {
    "/status": {"Model": "ObservedModel"}
  },
  "failed_endpoints": [],
  "status_overrides": {},
  "state": {}
}
```

- `gateway` must be a bare host. URLs, userinfo, ports, loopback/link-local/
  unspecified/reserved addresses, `localhost`, and metadata hostnames are
  rejected. Private LAN router addresses such as `192.168.1.1` remain valid.
- `responses` maps an `ajaxmethod` value (from a declaration's query/body), or
  the URL path when no `ajaxmethod` exists, to a JSON object. Values are used
  only in memory; `inspect` prints field paths and types, never values.
- `failed_endpoints` forces a deterministic failure; `status_overrides` can
  simulate statuses such as `302` to verify redirect rejection; `state` may
  preload synthetic derived values in `test` only.
- Credential-like keys (`password`, `token`, `secret`, `authorization`, `cookie`,
  `bearer`, API/private keys) are rejected before execution. Fixtures are
  capped at 1 MiB and 100 endpoint responses.

## Dry-run output and redaction

`test` validates the profile, executes only unauthenticated pre-auth endpoints
for discovery, then runs collection against the fixture with an offline-only
authenticated flag. It returns:

- `connector_id`, `gateway`, `mode: "offline_fixture"`;
- `discovery_errors` and redacted field-path `observations`;
- `domains` with each metric's normalized `availability` and `value`;
- `endpoint_errors`, `devices_loaded`, `device_count`, `voip_lines`;
- `device_compatibility: "not_evaluated"` and
  `physical_device_check_required: true`.

Sensitive metric IDs such as serial numbers, MACs, PPPoE users, public IPs,
phone numbers, and LOID are printed as `[REDACTED]`. Raw response bodies,
headers, auth state, derived state, credentials, tokens, and
`UniversalModel.to_dict()` are never printed. Successful mocked execution is not
hardware evidence.

## Shared request safety

`profile_safety.py` is a global, vendor-neutral security boundary used by the
runtime executor, manager validation, builder CLI, and API gateway validation.
It rejects unsafe HTTP methods/schemes/paths/gateways, pins requests to the
configured gateway, disables redirect following, rejects 3xx responses, sets
per-endpoint timeouts, and bounds response size. This is a runtime hardening
control, not a per-model dependency: adding a model still requires only JSON
profile changes plus an equipment association, except for a documented trusted
auth handler or parser change.

## Catalog registration

`register` requires `--approve`. It rejects unresolved `__OBSERVE_*__` values,
verifies manufacturer/model metadata, rejects duplicate model registrations and
conflicting existing connector IDs, requires `--manufacturer-name` for a new
manufacturer, and permits `--enable`
only for an `observed` profile with `evidence.verified: true`. It adds the
connector and its equipment association in one validated, atomic multi-file
write, rejects dangling references through combined manager validation, and
verifies both files after writing. A refresh of `gwmon.service` through the
normal deployment process is required before the live process sees new catalogs.

## Validation coverage

Validation is performed by `ConnectorManager.validate()`:

- an in-house JSON Schema subset for structural shape;
- cross-checks for evidence, identification, auth, endpoints, extraction,
  validation rules, dependencies, drafts, secrets, manufacturer links, disabled
  profiles, and dangling references;
- endpoint method/path safety checks shared with the executor;
- credential-suggestion checks requiring explicit user confirmation.

Structural errors and warnings are reported separately. Compatibility remains
`NOT_EVALUATED` unless physical-device evidence is recorded outside this
milestone.

See `docs/CONNECTOR_PROFILE_SPEC.md` for supported schema keywords and
`tests/test_profile_builder_m3.py` for deterministic CLI/registration coverage.

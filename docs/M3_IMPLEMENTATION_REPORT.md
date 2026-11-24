# M3 implementation report — Profile Builder and manual model registration

**Milestone:** M3
**Branch:** `main`
**HEAD:** `9567d24d85d4c0cc7c73143957c70d5ca103c577`
**Status:** implemented, verified locally, independently reviewed, and ready for the normal service reload. Deployment evidence is recorded in `docs/PROJECT_FINAL_REPORT.md`.

## Objective

Extend the existing Profile Builder so an operator can move from a manufacturer-constrained observation to a reviewed declarative profile and register it without changing Python code. Preserve the M1/M2 runtime contracts, the frozen API shapes, the seven-domain `UniversalModel`, FiberHome AES behavior, VoIP dialing, Speedtest behavior, PWA/dashboard presentation, and all pre-existing uncommitted work.

No hardware compatibility is claimed. Physical equivalence remains `NOT_EVALUATED`.

## What changed

### CLI workflow — `tools/connector_builder.py`

The existing CLI was extended, not replaced. Available commands are:

- `create`, `validate`, `validate-draft`, `import-draft`, `list` — retained behavior;
- `configure` — writes only schema-allowed declarative endpoint/manufacturer/model fields;
- `map` — maps an observed field to a canonical metric and adds the required capability;
- `inspect` — returns field paths and types from a sanitized fixture, never values;
- `test` — deterministic, credential-free, offline dry run;
- `auth-handlers`, `metrics`, `manufacturers`, `models` — read-only discovery helpers;
- `register` — explicit, atomic connector plus equipment-catalog registration.

`inspect` and `test` have no live-network mode. They use `_FixtureSession`, do not read real credentials, and never instantiate `requests.Session()`. A test patches `requests.Session` to raise and proves the dry-run path stays offline.

Registration requires `--approve`. It rejects unresolved `__OBSERVE_*__` values, checks duplicate connector IDs and duplicate manufacturer/model pairs, requires `--manufacturer-name` for a new manufacturer, validates metadata agreement, rejects dangling equipment references through combined `ConnectorManager` validation, writes both catalogs with `os.replace()`, and verifies the result. Repeating the same registration is idempotent because registration-managed fields are ignored in the existing-entry comparison. New profiles remain disabled unless verified manual evidence allows `--enable`.

### Shared safety boundary — `profile_safety.py`

A new vendor-neutral module centralizes request safety for the builder, manager, executor, and API boundary:

- methods limited to `GET`/`POST`;
- schemes limited to `http`/`https`;
- endpoint paths must be single leading-slash paths with no traversal, query text, fragments, encoded separators, backslashes, control characters, or network-path references;
- gateways must be bare hosts: URLs, userinfo, ports, metadata hosts, loopback, link-local, unspecified, multicast, reserved addresses, and numeric/hex/octal/binary aliases such as `127.1` and `0x7f000001` are rejected;
- requests are pinned to the configured gateway, redirects are not followed, and 3xx responses are rejected;
- real responses are streamed and bounded to 1 MiB before full buffering; fixture responses use the same bound after materialization.

This is a runtime hardening control, not model-specific Python coupling. Adding a model still normally requires only a declarative JSON profile and an equipment association. A Python change is needed only for a documented trusted auth handler or parser.

### Runtime and API

`connector_executor.py` uses the shared URL/path/method/redirect helpers, sends requests with `stream=True`, reads bounded bodies through `read_bounded_text()`, closes responses in `finally`, and parses only the bounded text. `connector_manager.py` reports `unsafe_endpoint_method` and `unsafe_endpoint_path` validation errors. `app.py` validates client-supplied gateways with the same helper before discovery/connect. `static/app.js` passes the selected manufacturer to `/api/login-defaults` so prefilled discovery and the subsequent `/api/connect` use the same constrained selector.

The browser still sends only gateway, credentials, and an optional manufacturer selector. A client-supplied `profile_id` remains ignored. Requests that omit the selector retain legacy all-profile discovery.

### Fixture contract

Fixtures require `schema_version: 1`, a bare gateway host, and a `responses` map keyed by `ajaxmethod` or URL path. They support `failed_endpoints`, `status_overrides`, and `state` for deterministic branches. Credential-like keys are rejected, fixtures are capped at 1 MiB, and at most 100 endpoint responses are accepted. `status_overrides` can simulate redirects to prove they are rejected.

### Redaction and honesty

`inspect` prints only field paths and types. `test` redacts serial numbers, GPON SNs, MACs, PPPoE users, public/gateway IPs, VoIP numbers, and LOID. Raw bodies, headers, auth state, derived state, credentials, and tokens are never printed. Every dry-run report includes:

```json
{
  "device_compatibility": "not_evaluated",
  "physical_device_check_required": true
}
```

Successful mocked execution is never represented as hardware evidence. The shipped catalog still contains exactly one real enabled profile (`fiberhome.hg6145f1`); no Huawei, ZTE, fake firmware, or invented endpoint behavior was added.

## Documentation updated

- `README.md`
- `docs/PROFILE_BUILDER_SPEC.md`
- `docs/CONNECTOR_PROFILE_SPEC.md`
- `docs/DISCOVERY_SPEC.md`
- `docs/MIGRATION_PLAN.md`
- `docs/ARCHITECTURE.md`
- `docs/AUTHENTICATION_SPEC.md`
- this report

## Deterministic tests added

`tests/test_profile_builder_m3.py` covers:

- gateway/method/path/scheme rejection, including alternate numeric loopback aliases;
- bounded streamed responses and oversize rejection;
- all builder discovery helpers and command wiring;
- fixture-only `inspect` with no raw values;
- offline `test` with `requests.Session` patched to fail;
- redaction of sensitive metrics and explicit `not_evaluated` status;
- fixture rejection of credential-like keys and unsafe gateways;
- redirect rejection;
- unknown-metric rejection;
- approval, metadata agreement, duplicate model, repeated registration, dangling reference, and unresolved-placeholder cases;
- API rejection of bare-URL and numeric-alias gateway values.

`tests/speedtest_reset_browser.test.js` was adjusted to tolerate the new manufacturer query parameter while retaining the Speedtest page-lifecycle regression.

## Verification evidence

All commands were run from `/root/projects/homerouter` with the project virtualenv and bytecode writing disabled.

| Check | Result |
| --- | --- |
| `python -m unittest discover -s tests` | **197 tests OK** |
| `node tests/speedtest_reset_browser.test.js` | **PASS** |
| `python -m tools.connector_builder validate --json` | **PASS**, 1 connector, 0 structural errors, 0 warnings, `not_evaluated` |
| Documentation link/reference check | **OK**, no missing local targets |
| `git diff --check` | **clean** |
| Targeted profile-path scan for `eval`, `exec`, `__import__`, `subprocess`, `os.system`, `shell=True`, `importlib` | **no matches** in `profile_safety.py`, `connector_builder.py`, `connector_manager.py`, `connector_executor.py` |
| Frozen constants | 61 metric IDs, 7 domains, 7 trusted handlers including `fiberhome_aes`, PWA cache `gwmon-v8` |
| Canonical branch/HEAD | `main` / `9567d24d85d4c0cc7c73143957c70d5ca103c577` |

## Independent review

An independent read-only review found one high and one medium issue plus two low contract/documentation caveats:

1. **High:** alternate numeric loopback spellings could bypass the gateway blocklist.
2. **Medium:** the original 1 MiB check occurred after the network body had been fully buffered.
3. **Low:** repeated registration with the original draft was rejected because registration-managed fields differed.
4. **Low:** documentation and tests disagreed about whether scheme-bearing gateway values were accepted.

All four were resolved before deployment: numeric aliases are rejected, real requests are streamed and bounded before buffering, registration comparison ignores managed fields, and bare-host gateway behavior is now consistent across code, docs, and tests. A second read-only pass then identified two residual cases: mixed-radix aliases such as `0x7f.1` and streamed redirect/error responses not being closed. Both were fixed with radix-prefixed label rejection and by moving all status checks inside the response-closing `try/finally`. New deterministic tests cover every review finding. The final independent read-only re-check passed with no critical, high, or medium finding remaining and confirmed the frozen contracts and M3 behavior are intact.

## Preservation and scope

The pre-change backup is `/root/backups/homerouter/m3-pre-20261124-110111/` with verified `full.tar.gz`, SHA-256 sidecar, file hashes, and baseline status hash `f030a2b17b9f4a0599277939e644ccae640dbf56b52d4cb8aad0180ffb31f436`. All Phase 1, Speedtest-reset, M1, and M2 uncommitted work remains in the tree. No commit, push, Phase 4 work, unrelated change, or rollback was performed.

## Gate

M3 is code-complete and locally verified. Deployment is limited to the normal `gwmon.service` reload required for the already-running process to load the new code; production evidence and final regression results are in `docs/PROJECT_FINAL_REPORT.md`.

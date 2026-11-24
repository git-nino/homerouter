# Project final report — Gateway Monitor / Homerouter

**Final status:** complete for the requested scope.
**Branch:** `main`
**HEAD:** `9567d24d85d4c0cc7c73143957c70d5ca103c577`
**Service:** `gwmon.service`, running directly from `/root/projects/homerouter` on `0.0.0.0:7000`.
**Hardware compatibility:** `NOT_EVALUATED`. No physical-device equivalence claim is made.

> This report records the completed M3 state before final publication. The
> subsequent final release publication is documented in the repository history;
> no physical-device authentication was performed as part of that publication.

## 1. Scope and preservation

This run completed M3 — Profile Builder and manual model registration — after preserving all pre-existing uncommitted Phase 1, Speedtest-reset, M1, M2, and deployment work. No `reset`, `clean`, `stash`, overwrite, or discard operation was used.

No commit, push, Phase 4 work, or unrelated change was performed. The canonical branch and HEAD remained unchanged throughout.

The final working-tree status hash is:

```text
1620e1f4aac2aa216c3e7b1a4d30080e5171ba65baaa3f6aace241d73fe6c2cb
```

## 2. Backups

| Backup | Purpose | Archive SHA-256 |
| --- | --- | --- |
| `/root/backups/homerouter/m3-pre-20261124-110111/` | Verified pre-M3 baseline; baseline status hash `f030a2b17b9f4a0599277939e644ccae640dbf56b52d4cb8aad0180ffb31f436` | `e10a4862106bef7b2a53bb2e8df5de40182a830e8fa827e56e6f75ad23236fa6` |
| `/root/backups/homerouter/m3-deploy-pre-20261124-114215/` | Verified final pre-deployment backup; 78 hashed files; captured status hash `6d6d803fadbdf8efcaa1ea3f1134ff1aee155586931516491ff13b0a0a736f56` | `5c90e9582ffcad7a63c057a0fb4d2438a2434de6dc39e845f41043e3b6139ccb` |

Both archives include SHA-256 sidecars, file hash manifests, git status captures, and verification output. The final backup also includes pre-restart journal, service state, pre-deployment verification, restart marker, production checks, journal check, and post-deployment regression results.

## 3. M3 implementation summary

### Profile Builder

`tools/connector_builder.py` now supports the complete manufacturer-constrained workflow:

1. select a manufacturer from the catalog;
2. run constrained unauthenticated discovery;
3. fall back to manual declarative authoring without guessing;
4. configure observed endpoints;
5. inspect sanitized response fields;
6. map fields to canonical metrics;
7. validate the profile;
8. run a redacted offline fixture test;
9. register the profile and equipment association with explicit approval.

`inspect` and `test` are fixture-only. They never instantiate `requests.Session()`, use real credentials, contact a router, or modify device state. Every report states `device_compatibility: "not_evaluated"` and `physical_device_check_required: true`.

### Manual registration

`register --approve` performs combined catalog/manufacturer validation, rejects unresolved placeholders, duplicate models, conflicting IDs, dangling references, and metadata disagreements. It creates new manufacturers only with `--manufacturer-name`, defaults new profiles to disabled, supports `--enable` only with verified observed evidence, writes both JSON catalogs atomically, verifies the result, and is idempotent for repeated registration of unchanged content.

Manual fallback never silently probes all manufacturers. Requests that omit the selector retain the legacy all-profile discovery behavior.

### Shared safety boundary

`profile_safety.py` is a vendor-neutral runtime control shared by the builder, manager, executor, and API boundary. It validates methods, schemes, paths, and bare gateways; rejects metadata hosts, loopback/link-local/unspecified/reserved addresses, URL/userinfo/port forms, numeric and mixed-radix aliases, traversal, query text, fragments, encoded separators, and redirects; pins requests to the configured gateway; and enforces a 1 MiB response bound.

`connector_executor.py` uses `stream=True`, reads bounded bodies through `read_bounded_text()`, closes streamed responses on every path including 3xx/4xx/5xx, and rejects unsafe redirects. `connector_manager.py` surfaces unsafe method/path validation errors. `app.py` rejects invalid client-supplied gateways with HTTP 400. `static/app.js` passes the selected manufacturer to `/api/login-defaults` so prefill and connect use the same constrained selector.

### Frozen contracts preserved

- `/api/discovery`: exact frozen key set and profile-row key set; compatibility remains `not_evaluated`.
- `/api/login-defaults`: exact frozen key set and unsupported message.
- `/api/data`: exact frozen key set when connected; fresh unconnected response is `401 {"error":"Not connected"}`.
- `/api/manufacturers`: unchanged shape.
- Client-supplied `profile_id` remains ignored.
- Seven domains and 61 metric IDs; availability vocabulary unchanged.
- Seven trusted auth handlers, including `fiberhome_aes`; FiberHome AES test vector intact.
- VoIP safe `tel:` dialer, Speedtest worker/APIs, PWA cache `gwmon-v8`, dashboard layout and static response shapes intact.
- Shipped catalog still contains one real enabled profile (`fiberhome.hg6145f1`) and no Huawei, ZTE, invented firmware, fake models, or fabricated endpoint claims.

## 4. Independent review and resolution

An independent read-only review found:

1. **HIGH** — alternate numeric loopback aliases bypassed gateway validation.
2. **MEDIUM** — response size was checked after full buffering.
3. **LOW** — repeated registration was not idempotent.
4. **LOW** — documentation/tests disagreed about scheme-bearing gateway values.

All four were fixed. A second read-only pass found two residual issues:

1. **HIGH** — mixed-radix aliases such as `0x7f.1` and `127.0.0x1` were still accepted.
2. **MEDIUM** — streamed redirect/error responses were raised before the closing `finally`.

Both were fixed with radix-prefixed label rejection and by moving all status checks inside the response-closing `try/finally`. The final independent read-only re-check returned **PASS with no critical, high, or medium finding remaining**, and confirmed the frozen contracts and M3 behavior remained intact.

## 5. Pre-deployment verification

All checks were run from `/root/projects/homerouter` with the project virtualenv.

| Check | Result |
| --- | --- |
| `python -m unittest discover -s tests` | **197 tests OK** |
| `node tests/speedtest_reset_browser.test.js` | **PASS** |
| `python -m tools.connector_builder validate --json` | **PASS** — 1 connector, 0 errors, 0 warnings, `not_evaluated` |
| Documentation link/reference check | **OK**, no missing local targets |
| `git diff --check` | **clean** |
| Targeted profile-path scan for `eval`, `exec`, `__import__`, `subprocess`, `os.system`, `shell=True`, `importlib` | **no matches** in profile safety, builder, manager, or executor |
| Auth handlers | `fiberhome_aes,form,http_basic,http_digest,none,session,token` |
| Metrics/domains | `61` metrics, `7` domains |
| PWA cache | `gwmon-v8` |
| Branch/HEAD | `main` / `9567d24d85d4c0cc7c73143957c70d5ca103c577` |

## 6. Deployment

Deployment was limited to the normal service reload needed for the running process to load the new code. No other service was stopped, restarted, or modified.

| Item | Before | After |
| --- | --- | --- |
| `gwmon.service` PID | `506557` | `527056` |
| Active enter time | `Tue 2026-11-24 10:46:17 WAT` | `Tue 2026-11-24 11:42:24 WAT` |
| State | active | **active/running** |
| Working directory | `/root/projects/homerouter` | `/root/projects/homerouter` |
| Listener | `0.0.0.0:7000` | `0.0.0.0:7000` |

## 7. Production checks

Live HTTP checks against `http://127.0.0.1:7000` all passed:

- `GET /` → `200`, dashboard HTML contains `Gateway Monitor`.
- `GET /static/app.js` → `200`, contains manufacturer-constrained selector wiring.
- `GET /static/sw.js` → `200`, contains `gwmon-v8`.
- `GET /static/manifest.webmanifest` → `200`.
- `GET /api/gateway`, `/api/manufacturers`, `/api/session` → `200`, expected shapes.
- `GET /api/discovery` and `GET /api/discovery?manufacturer_id=fiberhome` → `200`, exact frozen key set; every profile row has the exact frozen row keys and `device_compatibility: "not_evaluated"`.
- `GET /api/login-defaults?manufacturer_id=fiberhome` → `200`, exact frozen key set.
- `GET /api/data` → `401 {"error":"Not connected"}` — **expected for a fresh service; no rollback was performed**.
- `GET /api/speedtest/status` → `200`, idle status shape with no token exposure.
- `POST /api/connect` with gateway `0x7f.1` → `400 Invalid gateway...`, confirming mixed-radix SSRF aliases are rejected in production.

An early check-script attempt requested `/manifest.webmanifest` instead of `/static/manifest.webmanifest`, producing one harmless `404`. The script was corrected; the service was not changed or rolled back.

## 8. Journal check

Journal entries since the restart contain:

```text
error_markers=0
http_500=0
unexpected_4xx=0
expected_data_401=3
expected_connect_400=3
expected_wrong_manifest_path_404_from_check_script=1
```

There are no errors, tracebacks, failed service states, or unexpected HTTP errors.

## 9. Post-deployment regressions

| Check | Result |
| --- | --- |
| `python -m unittest discover -s tests` | **197 tests OK** |
| `node tests/speedtest_reset_browser.test.js` | **PASS** |
| `python -m tools.connector_builder validate --json` | **PASS** — 1 connector, 0 errors, 0 warnings, `not_evaluated` |
| `git diff --check` | **clean** |
| Branch/HEAD/status hash | unchanged / unchanged / `1620e1f4...d73fe6c2cb` |

## 10. Remaining limitations

- Physical FiberHome equivalence and compatibility with any other ONT remain **not evaluated** (M4). Mocked tests and schema validity are not hardware evidence.
- The auth handler registry remains closed. A new proprietary auth handler or parser would require a documented Python change.
- There is no hot-reload or registration API endpoint; registration is an offline CLI operation followed by the normal service reload.

## 11. Final state

M3 is implemented, independently reviewed, backed up, deployed, production-verified, and regression-tested. All requested work is complete. No commit, push, rollback, Phase 4 work, or further service action is pending.

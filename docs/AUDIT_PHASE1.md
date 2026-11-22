# Phase 1 Audit — Homerouter / Gateway Monitor

Date: 2026-11-22  
Repository: `git@github.com:git-nino/homerouter.git`  
Audited commit: `dd3517f` (`README: document API fallback for stale raw installer`)  
Audited checkout: `/root/projects/homerouter` (verified identical to the working copy used for this audit)

This is an audit only. No application architecture, runtime code, installer behaviour, or dashboard behaviour was changed for this phase.

---

## 1. Scope and method

The audit covered the complete tracked repository:

- `app.py` — Flask server, state, routes, view builder, speedtest worker
- `gateway.py` — gateway discovery, FiberHome authentication, raw data collection
- `templates/index.html`, `static/app.js`, `static/style.css` — UI
- `static/manifest.webmanifest`, `static/sw.js`, icons — PWA assets
- `install.sh`, `run.sh`, `requirements*.txt`, `README.md`
- `infos.txt`, `random_topic.txt` — tracked data/notes

Method: static source inspection, endpoint/auth/field mapping from the working implementation, cross-check against the previously captured real-device report in `infos.txt`, git status/history verification, and Python source compilation. The existing deployed services on Ubuntu/WSL and Termux were previously verified and remain the baseline to preserve.

---

## 2. Repository snapshot

### 2.1 Current shape

The project is a small, working, monolithic Flask application:

| Area | Current implementation |
|---|---|
| Entry point | `app.py`, binds `0.0.0.0:7000`, `threaded=True`, `debug=False` |
| Router logic | `gateway.py`: one `RouterClient` class plus module-level discovery/helpers |
| State | Single global `state` dict with one active client/session, guarded by one `threading.Lock` |
| Data flow | Router JSON → hardcoded `build_view(raw)` → JSON API → hardcoded JS render functions |
| Auth | Hardcoded `auto` / `fiberhome` / `plain` / `basic` strategies |
| Profiles | No JSON catalog; only internal auth-mode strings and endpoint-name lists |
| Model | No canonical normalized model, no stable metric IDs, no unit/state vocabulary |
| Tests | None; no test directory, fixtures, CI, or mock-response harness |
| Packaging | No APK code; Termux/Ubuntu installer only |

### 2.2 Git state

- Branch: `main`
- HEAD: `dd3517f`
- Tracking: `main...origin/main`
- Working tree: clean
- All 18 tracked files match the audited copy; no local modifications were found.

### 2.3 Dependencies

`requirements.txt` (Ubuntu/Debian): `flask`, `requests`, `cryptography`, `speedtest-cli`.  
`requirements-termux.txt`: `requests`, `flask`, `speedtest-cli`; `cryptography` is expected from Termux system packages or a wheel install.

The installer creates `~/.venvs/gwmon`, installs dependencies, and registers `gwmon` via systemd on Ubuntu/Debian or termux-services/runit on Termux, with a manual `start.sh` fallback.

---

## 3. Current architecture

### 3.1 Process and request flow

1. `app.py` imports `detect_gateway()` and calls it immediately while building the global `state`.
2. Browser loads `/`, receives `templates/index.html`.
3. `static/app.js` calls:
   - `/api/gateway` to pre-fill the gateway input;
   - `/api/session` to restore an already-connected view;
   - `/api/connect` on explicit **Connect**;
   - `/api/data` every 5 seconds after entering the dashboard;
   - `/api/speedtest` and `/api/speedtest/status` for throughput tests.
4. `/api/connect` constructs `RouterClient(host, username, password, mode)` and calls `login()`.
5. `/api/data` calls `client.collect()`, checks `_meta.session_valid`, optionally performs a silent re-login, then calls `build_view(raw)`.
6. `build_view()` produces the display JSON consumed by `render()` in `app.js`.

### 3.2 API surface

| Route | Method | Purpose |
|---|---|---|
| `/` | GET | Dashboard/login single page |
| `/api/gateway` | GET | Return detected/guessed gateway object |
| `/api/session` | GET | Return connected flag, gateway, auth profile string |
| `/api/connect` | POST | Authenticate and store the active client |
| `/api/data` | GET | Collect and normalize router data |
| `/api/disconnect` | POST | Clear the active client/credentials |
| `/api/speedtest` | POST | Start background speedtest |
| `/api/speedtest/status` | GET | Poll background speedtest state |

### 3.3 Concurrency and state safety

- One lock protects `state` reads/writes and speedtest state transitions.
- Router I/O (`login`, `collect`) happens outside the lock in `/api/data`, which avoids holding the lock during network calls.
- The frontend uses `setInterval(refreshData, 5000)` with no in-flight guard, so slow router responses can overlap with the next poll.
- The speedtest worker has a token guard to prevent a stale worker from overwriting a newer result, plus a 120-second watchdog.

---

## 4. Gateway detection and device identification today

### 4.1 Discovery algorithm (`gateway.py`)

`default_gateway()`:

1. Reads `/proc/net/route` and returns the IPv4 address for destination `00000000`.
2. Falls back to `ip route show default`, parsing `via <IPv4>`.
3. Returns `None` if neither works.

`subnet_candidates()`:

1. Runs `ip -4 -o addr show`.
2. For each non-loopback IPv4 interface with prefix length 8–30, computes the first usable address of that subnet (network address with the last octet set to 1).

`probe(host)`:

1. `GET http://<host>/cgi-bin/ajax?ajaxmethod=get_device_name`
2. `GET http://<host>/`
3. Accepts a host if either response parses to JSON containing `ModelName`, or the page text contains generic router-like keywords (`router`, `gateway`, `gpon`, `onu`, `wan`, `password`, `modelname`, `login`, `administration`).

`detect_gateway()`:

1. Builds candidates in this order: default gateway, subnet candidates, eight hardcoded common gateway addresses.
2. Deduplicates and probes each candidate with a 1.2-second per-request timeout.
3. Returns the first host considered a router, with the observed `ModelName` string.
4. If none pass, returns the default gateway (or the first common gateway) with `detected: false`.

### 4.2 Identification gaps

- There is no separate pre-auth `device_identifier` module.
- Only `ModelName` is captured; manufacturer, firmware, serial, and profile are not assembled into an evidence object before authentication.
- No confidence levels (`exact`, `partial`, `unknown`) and no evidence list exist.
- Keyword-based HTML matching can misidentify a web server as a router.
- `detect_gateway()` is called at import time, so app startup can block while probing up to several candidates.
- The hardcoded common-gateway list goes beyond the default gateway and local subnet. It is not a full LAN scan, but it is broader than the requested “default gateway first, manual fallback” behaviour.
- There is no abstraction layer for future Android/Termux APIs; detection is tied to Linux `/proc` and `ip`.
- The frontend allows manual gateway entry, but there is no backend “manual-only” discovery mode.

---

## 5. FiberHome connector behavior

All current FiberHome behaviour is hardcoded in `gateway.py` and `app.py`. There is no connector JSON profile.

### 5.1 Transport and base URL

- Base: `http://<gateway>/cgi-bin/ajax`
- Data reads: `GET` with query parameters `ajaxmethod=<method>` and `_=<random cache buster>`
- Login: `POST` to `/cgi-bin/ajax` with form-encoded fields.
- Request timeouts: 6 seconds for GET, 8 seconds for POST.
- No retry policy exists.

### 5.2 Authentication

`_login_fiberhome()`:

1. `GET get_acs_random`.
2. Reads `acsRandom` (seed) and `sessionid` from the response.
3. `fhencrypt(password, seed)`:
   - AES-128-CBC with PKCS#7-style padding.
   - Key is `seed[6:][:16]`, padded/truncated to 16 bytes; a hardcoded fallback key is used if the seed is too short.
   - The same 16-byte key is used as the IV.
   - Output is uppercase hex.
4. POST fields: `ajaxmethod=do_login`, `username`, `loginpd=<encrypted password>`, `port=0`, `sessionid`.
5. `login_result` interpretation: `0` and `1` are success, `4` is invalid credentials, any other value is rejection.

`login()`:

- `auto` tries FiberHome AES, then plain form login, then HTTP Basic.
- `_has_data_access()` calls `get_base_info` after a successful strategy and checks `session_valid != 0`; if the payload is empty it returns `True`, which can mask a false-positive login.
- `basic` sets `requests.Session.auth` and checks that the root page is HTTP 200 and does not look like a login form.
- `plain` iterates a hardcoded list of guessed endpoint/field combinations.

`app.py` adds an `auto`-only retry: if login fails with the short prefilled password, it retries once with a different hardcoded fallback password and returns a success message that names that fallback value. This is a credential-disclosure defect for the target design.

### 5.3 Session handling

- The FiberHome session id is captured from `get_acs_random` and reused.
- `collect()` stores `session_valid` in `_meta` based on `get_base_info`.
- `/api/data` detects `session_valid == 0`, silently calls `client.login()` using stored credentials, and retries collection.
- If still invalid, it returns 401 and the frontend returns to the login view.

This silent re-login is useful for the current app but conflicts with the future rule that authentication must follow explicit user action; it should be redesigned as an explicit reconnect or a clearly explained session-refresh policy.

### 5.4 Endpoints used on the real device

Observed/used endpoint methods:

| Method | Use |
|---|---|
| `get_acs_random` | Login seed and session id |
| `get_base_info` | Identity, system, optical/PON, LAN port status |
| `get_allwan_info` | WAN/PPPoE, DNS, Wi-Fi object |
| `get_lan_status` | Connected-device table |
| `get_operator` | Operator name/code |
| `get_device_name` | Model string |
| `get_dongle_info` | USB dongle status |
| `get_ipv6_lan` | Raw IPv6 LAN payload |
| `get_login_user` | Collected but not rendered |
| `do_login` | POST authentication |

`get_pon_info` is not used; optical values come from `get_base_info`.

### 5.5 Field mapping performed by `build_view()`

| Domain | Current source fields / transformations |
|---|---|
| Identity | `Manufacturer`, `ModelName`, `HardwareVersion`, `SoftwareVersion`, `SerialNumber`, `gponsn`, `ManufacturerOUI`, `operator_name`, `operators_code`, `brmac`, `os_version`, `compile_time`, WAN `MACAddress` |
| Internet/WAN | First `wan` entry: `ExternalIPAddress`, `RemoteIPAddress`, `DNSServers`, `ConnectionType`, `ConnectionStatus`, `Name`, `vlanid`, `Username`, `mtu`, `NATEnabled`, `Uptime`, `LastConnectionError` |
| Optical/PON | `WANAccessType`, `txpower`, `rxpower`, `transceivertemperature`, `biascurrent`, `supplyvottage`, `onu_id`, `vendor`, `pon_reg_state`, `authStatus`, `loid_name`, PON byte/packet counters |
| System | `uptime`, `cpu_usage`, `mem_total`/`mem_free` → percentage, `flash_usage`, `lan_port_num`, `uplink_mode` |
| Connected devices | `lan_status.data[]`: `HostName`, `IPAddress`, `MACAddress`, `AccessType`/`InterfaceType`, `AddressSource`, `ClientUpTime`, `rssi`, `Active` |
| LAN ports | `lan_status_1`…`lan_status_N` synthesized from `lan_port_num` |
| Wi-Fi | `wifi_obj_enable.ssid1`…`ssid16` filtered by `ConfigActive1`…`ConfigActive16` |
| IPv6/dongles | Raw passthrough from `get_ipv6_lan` and `get_dongle_info` |
| Dot decoding | `as_ip()` replaces `_point_` with `.` in IP-like values |

### 5.6 Generic fallback behavior

When no FiberHome model is detected, `collect()` also probes a hardcoded list of generic endpoint names (`get_status`, `status`, `get_system_info`, etc.) and three guessed paths (`/status`, `/cgi-bin/status`, `/api/status`). Any non-empty JSON payload is shown in the “Discovered router data” panel.

These generic endpoints and plain-login combinations are speculative, not observed from a physical non-FiberHome device. They should not be carried into the profile-driven engine as if they were verified compatibility.

---

## 6. Dashboard implementation

### 6.1 Structure (`templates/index.html`)

- Login view: gateway address, auth mode selector, username, password, show-password toggle, Connect button.
- Top bar: gateway badge, connection dot, PWA install button, speedtest button, disconnect button.
- Dashboard:
  1. Speedtest panel (currently first once connected)
  2. Eight headline stat cards: public IP, uptime, optical TX/RX, CPU, memory, device count, model/operator
  3. WAN/Internet panel
  4. Optical fiber/PON panel
  5. Connected devices table
  6. System panel and LAN/Wi-Fi panel
  7. Generic discovered-data panel for non-FiberHome payloads
  8. Footer with refresh interval and last update

### 6.2 Rendering (`static/app.js`)

- `render(d)` maps `build_view()` fields directly into DOM nodes and tables.
- HTML is escaped through `esc()` for dynamic text.
- `kvTable()` builds key/value rows with innerHTML.
- Device rows are generated from the normalized `devices` array.
- On transient `/api/data` failure, existing values remain in the DOM and only the footer says `refresh failed: …`.
- On HTTP 401, the UI leaves the dashboard and shows the login view.

### 6.3 Speedtest

- `speedtest-cli` runs in a daemon thread with a 120-second watchdog.
- Status is polled once per second by the browser, with an additional 135-second client-side watchdog.
- A monotonic token prevents stale workers from overwriting newer results.
- Results include download/upload Mbps, ping/jitter ms, and server metadata.

### 6.4 Styling and PWA

- Mobile-first dark theme with teal/orange accents.
- Responsive grids at 600px and 1000px breakpoints.
- `[hidden] { display: none !important; }` fixes previous visibility conflicts.
- PWA manifest, service worker, install prompt, and app shell cache are present.
- `/api/` responses are deliberately excluded from service-worker caching.

### 6.5 Dashboard gaps versus the target

- No VoIP/Telephony section or Call button exists.
- No normalized-device model; the UI consumes FiberHome-shaped `build_view()` output.
- No stable metric IDs, types, units, or provenance/source values.
- No explicit unavailable/unsupported/invalid/not-loaded states.
- No stale marker, last-successful-update timestamp, or per-domain refresh intervals.
- No overlapping-request guard on the 5-second poll.
- No five-card universal structure driven by a universal model.
- No compatibility/status banner using the required state vocabulary.
- No safe `tel:`/ACTION_DIAL integration.

---

## 7. Reusable components

The following pieces are worth carrying forward, usually behind new interfaces:

1. **Gateway route parsing** — `/proc/net/route` and `ip route show default` logic from `default_gateway()`.
2. **HTTP session handling** — `requests.Session`, headers, base URL construction, response parsing that strips a leading `Content-type` line.
3. **FiberHome AES primitive** — `fhencrypt()` and the observed `get_acs_random` / `do_login` sequence, to be expressed as connector operations rather than hardcoded client code.
4. **Dot decoding** — `as_ip()` replacement of `_point_` with `.`; should become a declarative transformation.
5. **Duration formatting** — `human_uptime()`; should be presentation of a normalized seconds value, not the source of truth.
6. **Speedtest worker** — background execution, timeout, token guard, and status endpoint; should become the Speed Test domain provider.
7. **Locking discipline** — avoid holding the state lock during router I/O.
8. **Frontend escaping and visibility fix** — `esc()` and the global `[hidden]` rule.
9. **Termux/Ubuntu installer** — platform detection, service setup, manual fallback, PWA notes. Keep the `app.py` entry point or update the installer during modularization.
10. **Real-device evidence** — `infos.txt` documents observed fields and values; it should be converted into tests/fixtures with sensitive values redacted.

---

## 8. Gaps against the target specification

| Target requirement | Current state | Gap |
|---|---|---|
| Generic engine + universal model + JSON profiles + one dashboard | Hardcoded FiberHome client and view builder | Core architecture still monolithic and device-specific |
| Add devices by adding a JSON profile | No `config/` directory or schema | Connector catalog, loader, and validation are absent |
| Real observed responses only | FiberHome path is observed; generic endpoints/logins are guessed | Speculative generic behavior must be removed or clearly quarantined |
| Auth: Basic, Digest, session/cookie, token, configurable login, dependencies | FiberHome AES, plain guesses, and Basic only | No Digest, token, configurable login flow, or dependency graph |
| Endpoint definitions with method/path/query/headers/body/format/auth/deps/timeouts/retries | Hardcoded methods and fixed timeouts | No declarative endpoint layer, retries, or dependencies |
| Extraction: JSON path, XPath, CSS selectors, regex, headers, transforms | Direct Python dict access | No extraction engine; no safe non-executable rule format |
| Normalized FTTH domains with stable IDs/types/units | Ad-hoc `build_view()` dictionary | No canonical model, IDs, units, or availability states |
| Preserve source values; distinguish unavailable/unsupported/invalid/not-loaded | Missing values become empty strings, zeros, or “Disabled” | Data fabrication and state ambiguity |
| Gateway discovery: default first, no broad scan, manual fallback, Android abstraction | Default first plus subnet guesses and common list | Broader probing, import-time blocking, no abstraction/confidence |
| Pre-auth identification with exact/partial/unknown evidence | Model string from one probe | No evidence/confidence/provenance model |
| Never auto-authenticate after discovery | Explicit Connect exists for first login | Silent re-login on expiry uses stored credentials |
| Credentials secure, no secrets in JSON/logs/source/debug | Hardcoded defaults in source/docs/UI; plaintext in memory; fallback password echoed | Must remove secrets and add credential interface/provenance |
| Compatibility states and diagnostics | None | Required state vocabulary and diagnostic reasons absent |
| Five-card dashboard with Speed Test, WAN, VoIP, System, Devices | Speed/WAN/Optical/System/Devices present; no VoIP | Missing VoIP, universal cards, stale indicators, dialer |
| Connector Builder CLI | Absent | Need `create`, `validate`, `list` and structural reports |
| Automated tests including FiberHome regression | Absent | Need mocks/fixtures and physical-device comparison before removing old logic |

---

## 9. Risks and defects found

### 9.1 Correctness and data integrity

- `build_view()` computes memory percentage as `0` when totals are missing, which fabricates a reading.
- `nat` is rendered as `Disabled` whenever the source value is not exactly `"1"`, including when the field is absent.
- Speedtest ping/jitter use `0` when source attributes are missing.
- Device count is simply `len(devices)`; no distinction between “zero devices” and “table not loaded”.
- `has_fiberhome_data` is inferred from the presence of a model or external IP, so a partial payload can be presented as a complete FiberHome view.
- `get()` swallows all request exceptions and returns `{}`, hiding timeouts/connection failures from diagnostics.
- `_has_data_access()` returns `True` for an empty payload, allowing false-positive Basic/plain logins.
- No request de-duplication or in-flight guard on the 5-second UI poll.

### 9.2 Security and privacy

- Source, template, README, and login retry logic contain hardcoded default credentials; one success message echoes a fallback password to the UI.
- `state["credentials"]` stores the password in plaintext in process memory.
- `infos.txt` is tracked and contains live-deployment identifiers: public IP, serial number, PPPoE account, MAC addresses, SSIDs, and device hostnames.
- Plain-auth guessing can submit entered credentials to multiple guessed local endpoints before compatibility is established.
- Detailed error strings include endpoint paths and codes; while currently low-risk locally, the future diagnostic layer must enforce secret redaction.

### 9.3 Compatibility and migration

- The generic fallback panel exposes raw payloads, but there is no evidence that the guessed endpoint names work on any real second device.
- Basic-auth success is based on “no login form detected”, which is weak evidence.
- The current app is proven on the physical FiberHome HG6145F1; migration must preserve that behavior and compare old/new outputs before old code is removed.
- Installer completeness checks hardcode `app.py` and `gateway.py`; modularization must keep a compatible entry point or update the installer in the same phase.
- Raw GitHub CDN has previously served stale installer content; pushes should be verified via the GitHub API fallback.

### 9.4 Android/Termux future

- Gateway detection is Linux-command based, not behind an abstraction that an Android layer could replace.
- Dialing is not implemented; the future call action must use an OS intent/URI and must never place a call automatically.
- Credential storage must move behind an interface so Android Keystore can replace the in-memory implementation later.

---

## 10. Phased implementation plan

The current app stays runnable after every phase. Each phase ends with tests/checks, a Termux/Ubuntu verification, a report, and a commit/push.

### Phase 2 — Canonical data model and tests

- Add `models/` with universal normalized model structures for all seven domains.
- Define stable IDs, value types, units, availability states, source values, provenance, and timestamps.
- Add `tests/` with unit tests for model construction, units, state handling, and “no fabricated values”.
- Do not change live behavior yet.

### Phase 3 — Connector catalog, credentials catalog, validation, manager

- Add `config/connectors.json` and `config/default_credentials.json` (provenance/suggestions only, no real secrets).
- Define a JSON schema for identities, auth flows, endpoints, extraction rules, validation rules, and dependencies.
- Add `connector_manager.py` and `tools/connector_builder.py` with `create`, `validate`, and `list`.
- Validate unique IDs, endpoint structure, conflicting identification rules, and unknown extraction methods.
- Report structural validity separately from device compatibility.

### Phase 4 — Generic engine and processing pipeline

- Add `http_client.py`, `authentication.py`, `response_parser.py`, `data_extractor.py`, `normalizer.py`, `validator.py`, and `connector_engine.py`.
- Support Basic, Digest, session/cookie, token, and configurable login flows with dependencies.
- Implement declarative extraction: JSON paths, XML XPath, HTML selectors, regex where justified, headers, and controlled transformations.
- Enforce timeouts, retries, dependency ordering, state/status capture, and secret redaction.

### Phase 5 — FiberHome profile migration and regression

- Create the FiberHome JSON profile using only endpoints and fields observed in this audit.
- Keep the legacy path available during comparison.
- Add mocked-response regression tests derived from real observations, with sensitive values redacted.
- Compare old and new normalized output against the same physical HG6145F1 before removing hardcoded FiberHome logic.

### Phase 6 — Discovery and pre-auth identification

- Add `gateway_detector.py` and `device_identifier.py`.
- Prefer default gateway; use manual input as explicit fallback; avoid broad scanning.
- Add exact/partial/unknown confidence with evidence and provenance.
- Show gateway, manufacturer, model, firmware (if observed), profile, confidence, and credential suggestions for user confirmation.
- Require an explicit Connect click; preserve username on failure and clear auth errors correctly.

### Phase 7 — Universal five-card dashboard

- Render from the universal model only.
- Cards in order: Speed Test, WAN/Internet, VoIP/Telephony, System Information, Connected Devices.
- Add configurable per-domain refresh, no overlapping requests, stale/error markers, and last-successful-update display.
- Add VoIP call button only when a validated number exists; use a safely encoded `tel:`/dialer URI and clarify that the call uses phone telephony, not ONT SIP.
- Preserve mobile-first dark styling and PWA behavior.

### Phase 8 — Hardening, docs, and final verification

- Remove hardcoded/default credential values from source, docs, and success messages.
- Redact or replace sensitive values in `infos.txt`; convert evidence to safe fixtures.
- Update README, installer checks, service definition, and PWA cache version as needed.
- Run the full test suite, verify old-vs-new output on the physical ONT, then verify Termux and Ubuntu/WSL services and endpoints.
- Remove legacy code only after physical-device equivalence is confirmed.

---

## 11. Phase 1 conclusion

The existing application is functional and well-suited as a preservation baseline: FiberHome AES login, data collection, speedtest, PWA, and both Termux and Ubuntu service paths are working. It is not yet a generic platform. The main work is to separate the hardcoded FiberHome assumptions into a declarative connector profile, introduce a universal normalized model with explicit availability states, and rebuild the dashboard around that model without losing the proven physical-device behavior.

Recommended next step: approve Phase 2 (canonical model and tests) before any runtime code is restructured.

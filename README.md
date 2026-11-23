# Gateway Monitor

A mobile-first, Netdata-style dashboard for FTTH ONU/gateways. Connector profiles
select the authentication and collection flow; the dashboard only consumes the
normalized `UniversalModel`. Runs locally on **port 7000**.

## Run

```bash
chmod +x run.sh
./run.sh
```

Then open <http://localhost:7000> on your phone or computer.

First run creates `~/.venvs/gwmon` and installs `flask`, `requests`,
`cryptography` and `speedtest-cli` from `requirements.txt`.

## Use

1. On load the app detects the gateway IP, runs **unauthenticated** discovery
   endpoints, and displays the observed device, matching profile, evidence and
   credential provenance. No authentication happens at this stage.
2. Enter your own username and password, confirm or override the suggested
   connector profile, then press **Connect**. The selected profile — not a
   hardcoded branch in `app.py` — determines the authentication flow and
   endpoints. Unknown firmware leaves the profile unselected.
3. The dashboard auto-refreshes every 5 seconds. Summary tiles appear in this
   order: Public IP, Uptime, Optical TX, Optical RX, Devices, Model. The five
   sections are Speed Test, WAN/Internet, VoIP/Telephony, System Information and
   Connected Devices. Missing readings show an explicit state instead of `0`.
4. The Call button appears only when a validated phone number is available and
   opens your phone's dialer after a tap; the app never places a call itself.
5. Tap **Speedtest by Ookla** to run a local download / upload / latency test
   (uses `speedtest-cli`; results are injected into the `speed_test` domain of
   the universal model).
6. **Disconnect** clears the local session and sends the profile's logout
   endpoint when one is defined.

## One-command install (Termux or Ubuntu)

The installer detects the platform and works straight from the repository — no
need to clone manually:

```bash
curl -fsSL https://raw.githubusercontent.com/git-nino/homerouter/main/install.sh | bash
```

Or, from a local checkout: `bash install.sh`.

If `raw.githubusercontent.com` serves a stale copy after an update, fetch the
current file through the GitHub API instead:

```bash
curl -fsSL "https://api.github.com/repos/git-nino/homerouter/contents/install.sh?t=$(date +%s)" \
  | python3 -c "import sys,json,base64;sys.stdout.write(base64.b64decode(json.load(sys.stdin)['content']).decode())" \
  | bash
```

It detects **Termux** (Android) vs **Ubuntu/Debian**, updates/upgrades packages,
creates `~/.venvs/gwmon`, installs dependencies, registers a service, and starts
the app on port 7000. The project lands in `~/projects/homerouter` by default
(`APP_DIR=/some/path` overrides this).

Platform details:

| | Termux (Android) | Ubuntu / Debian |
|---|---|---|
| Packages | `pkg` + native `python-cryptography` | `apt-get` |
| Python deps | `requirements-termux.txt` (system cryptography) | `requirements.txt` |
| Service | `termux-services` (`gwmon`, autostart) | `systemd` (`gwmon.service`, autostart) |
| Controls | `svc-*` or `sv-*` commands, or runit's `sv status/up/down gwmon` + `sv-disable gwmon` (depends on Termux release) | `systemctl status gwmon`, `systemctl stop gwmon`, `systemctl start gwmon`, `systemctl disable gwmon`, `journalctl -u gwmon -f` |
| Logs | `logcat`/`logger -t gwmon` | `journalctl -u gwmon -f` |

If no service manager is available (e.g. WSL without systemd, or a container),
or a Termux release uses the older `sv-*` commands, the installer falls back
automatically to a `start.sh` launcher writing `gwmon.log` in the app directory.

## PWA (install without HTTPS)

Open **http://localhost:7000** in Chrome on the phone and use the browser's
*Add to Home screen* menu. There is no in-app install button. Browsers treat
`localhost` as a secure context, so the service worker and PWA installation
work without a certificate. Opening the app via `http://<phone-ip>:7000` will
show the dashboard, but PWA installation will not be offered — use `localhost`
for that.

## Universal runtime (Phase 2)

The live path is:

```
gateway detection → device identification → ConnectorManager → connectors.json
→ ConnectorExecutor → auth handlers + HTTP → profile extraction
→ UniversalModel → unified dashboard
```

`auth_handlers.py` contains a controlled registry; the FiberHome handler delegates
AES encryption to the existing `gateway.fhencrypt` helper. Profile JSON never
evaluates code. `legacy_view.py` is kept only for regression comparison and is
not imported by `app.py`. See `docs/AUDIT_PHASE2.md` for evidence and the
remaining physical-device check.

## Connector catalog (Phase 3)

Connector definitions live in `config/connectors.json`. They describe only
endpoints, fields, and transformations observed on real devices. The catalog is
validated by `connector_manager.py`, which deliberately reports two separate
results: **structural validity** and **device compatibility**. A structurally
valid profile is not a compatibility claim; physical-device comparison is still
required.

```bash
# Inspect the catalog and its separate compatibility status
python -m tools.connector_builder list
python -m tools.connector_builder validate

# Print a secret-free draft scaffold, or append one to a catalog file
python -m tools.connector_builder create --id vendor.model --display-name "Vendor Model"
python -m tools.connector_builder create --id vendor.model --display-name "Vendor Model" \
  --output config/connectors.json
```

`config/default_credentials.json` contains safe *suggestions* and provenance
only. It never stores credential values, and every suggestion requires explicit
user confirmation. No network authentication happens automatically after
discovery.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Files

- `app.py` — Flask server, discovery/connect/data APIs, speedtest worker; no vendor-specific normalization
- `gateway.py` — gateway detection and the legacy FiberHome client/AES helper used only by the controlled handler and regression tests
- `models/` — universal normalized FTTH model (seven domains, explicit availability states, safe dialer actions)
- `connector_manager.py` — catalog loading, validation, identification scoring, credential suggestions
- `connector_executor.py` — profile-driven endpoint execution, extraction, validation, collections, sessions and errors
- `auth_handlers.py` — controlled auth registry; no profile text is executed as code
- `legacy_view.py` — legacy comparison builder only; never used by the live app
- `config/connectors.json`, `config/connector.schema.json` — connector profiles and their JSON schema
- `config/default_credentials.json` — secret-free credential suggestions with provenance
- `tools/connector_builder.py` — `create`, `validate`, and `list` CLI
- `tests/` — model, phone, connector, executor, API and UI tests, including a second synthetic manufacturer profile
- `templates/index.html`, `static/*` — responsive dashboard UI consuming only `UniversalModel` metric IDs
- `requirements.txt`, `requirements-termux.txt` — dependencies
- `run.sh` — simple desktop launcher
- `install.sh` — universal Termux/Ubuntu installer + service setup (works via `curl | bash`)
- `.gitignore` — keeps caches, venvs and logs out of git
- `static/manifest.webmanifest`, `static/sw.js`, `static/icon-*.png`, `static/favicon.png` — PWA assets derived from `icon-ont.png`
- `docs/AUDIT_PHASE1.md` — Phase 1 architecture audit and phased migration plan
- `docs/AUDIT_PHASE2.md` — Phase 2 integration evidence and physical-device acceptance criteria

# Gateway Monitor

A mobile-first, Netdata-style dashboard for the FiberHome HG6145F1 (and similar)
ONU/gateways. Runs locally on **port 7000**.

## Run

```bash
chmod +x run.sh
./run.sh
```

Then open <http://localhost:7000> on your phone or computer.

First run creates `~/.venvs/gwmon` and installs `flask`, `requests`,
`cryptography` and `speedtest-cli` from `requirements.txt`.

## Use

1. The app detects the gateway IP from your network (`/proc/net/route`, local
   subnets and common router addresses) and pre-fills it.
2. Username `user` and password `user1234` are pre-filled — just press **Enter**.
   Change them if your router uses different credentials (e.g. the verified
   FiberHome default `user123456`).
3. **Authentication mode** is selectable: `Auto-detect` tries FiberHome AES,
   then plain-text login, then HTTP Basic, and keeps the first method that
   actually returns data. Pick a specific mode if you know your router type.
   Non-FiberHome routers show a **Discovered router data** panel with whatever
   their endpoints expose.
4. The dashboard auto-refreshes every 5 seconds and shows public IP, uptime,
   optical TX/RX power, CPU/memory/flash, WAN details, connected devices, LAN
   ports and SSIDs.
5. Tap **⚡ Speedtest** at the top to run a download / upload / latency test
   (uses `speedtest-cli`; the test runs in the background and takes ~30–60 s).

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

Open **http://localhost:7000** in Chrome on the phone and use
*Add to Home screen* (or the **⬇ Install** button). Browsers treat `localhost`
as a secure context, so the service worker and install prompt work without a
certificate. Opening the app via `http://<phone-ip>:7000` will show the
dashboard, but PWA installation will not be offered — use `localhost` for that.

## Files

- `app.py` — Flask server, API routes, speedtest worker
- `gateway.py` — gateway detection, AES login, data collection
- `templates/index.html`, `static/*` — responsive dashboard UI
- `requirements.txt`, `requirements-termux.txt` — dependencies
- `run.sh` — simple desktop launcher
- `install.sh` — universal Termux/Ubuntu installer + service setup (works via `curl | bash`)
- `.gitignore` — keeps caches, venvs and logs out of git
- `static/manifest.webmanifest`, `static/sw.js`, `static/icon-*.png` — PWA assets

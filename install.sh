#!/usr/bin/env bash
# ============================================================================
#  Gateway Monitor (homerouter) — universal installer
#
#  Run straight from GitHub (nothing else needed):
#    curl -fsSL https://raw.githubusercontent.com/git-nino/homerouter/main/install.sh | bash
#
#  Or from a local checkout:
#    ./install.sh
#
#  Detects Termux (Android) vs Ubuntu/Debian, installs dependencies, creates
#  a service (termux-services / systemd) and starts the app on port 7000.
#
#  Optional environment overrides:
#    APP_DIR=~/myrouter  REPO_URL=<git-url>  BRANCH=main  bash install.sh
# ============================================================================
set -euo pipefail

HTTPS_REPO="https://github.com/git-nino/homerouter.git"
SSH_REPO="git@github.com:git-nino/homerouter.git"
BRANCH="${BRANCH:-main}"
APP_DIR="${APP_DIR:-${HOME}/projects/homerouter}"
SERVICE="gwmon"
VENV="${HOME}/.venvs/gwmon"
PORT=7000

# ---------------------------------------------------------------------------
# 1. Detect platform
# ---------------------------------------------------------------------------
if [ -n "${PREFIX:-}" ] && [ -d "/data/data/com.termux" ]; then
  PLATFORM="termux"
  SHEBANG="#!/data/data/com.termux/files/usr/bin/bash"
elif [ -r /etc/os-release ] && grep -qiE '^(ID|ID_LIKE)=.*(ubuntu|debian)' /etc/os-release; then
  PLATFORM="ubuntu"
  SHEBANG="#!/usr/bin/env bash"
elif command -v apt-get >/dev/null 2>&1; then
  PLATFORM="ubuntu"
  SHEBANG="#!/usr/bin/env bash"
else
  echo "ERROR: unsupported platform. This installer supports Termux and Ubuntu/Debian." >&2
  exit 1
fi

echo "==> Platform: ${PLATFORM}"

# WSL and containers often have systemctl installed but no running systemd.
SYSTEMD_OK=0
if [ "$PLATFORM" = "ubuntu" ] && command -v systemctl >/dev/null 2>&1 \
   && [ -d /run/systemd/system ]; then
  SYSTEMD_OK=1
fi

# Termux releases expose different service command families:
#   new: svc-enable / svc-start / svc-stop / svc-status
#   old: sv-enable  / sv-start  / sv-stop  / sv-status
#   bare: sv-enable / sv-disable + runit's "sv up|down|status <service>"
HAS_TERMUX_SVCS=0
if command -v svc-enable >/dev/null 2>&1 || command -v sv-enable >/dev/null 2>&1; then
  HAS_TERMUX_SVCS=1
fi

svc_cmd() {
  action="$1"; shift
  if command -v "svc-${action}" >/dev/null 2>&1; then
    "svc-${action}" "$@"
  elif command -v "sv-${action}" >/dev/null 2>&1; then
    "sv-${action}" "$@"
  elif command -v sv >/dev/null 2>&1; then
    case "$action" in
      start)  sv up "$@" ;;
      stop)   sv down "$@" ;;
      status) sv status "$@" ;;
      *)      return 127 ;;
    esac
  else
    return 127
  fi
}

if [ "$PLATFORM" = "ubuntu" ] && [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO="sudo"
  else
    echo "ERROR: Ubuntu installation needs root or sudo." >&2
    exit 1
  fi
else
  SUDO=""
fi

# ---------------------------------------------------------------------------
# 2. Locate (or fetch) the project files
# ---------------------------------------------------------------------------
fetch_project() {
  echo "==> Fetching project into ${APP_DIR}"
  mkdir -p "$(dirname "$APP_DIR")"

  if command -v git >/dev/null 2>&1; then
    if git clone --depth 1 --branch "$BRANCH" "${REPO_URL:-$HTTPS_REPO}" "$APP_DIR" 2>/dev/null; then
      return 0
    fi
    echo "    HTTPS clone failed — trying SSH remote"
    if GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new' \
       git clone --depth 1 --branch "$BRANCH" "$SSH_REPO" "$APP_DIR" 2>/dev/null; then
      return 0
    fi
    echo "    Git clone failed — trying source archive"
  fi

  # Last resort: download the tarball (works without git on public repos).
  ARCHIVE="https://github.com/git-nino/homerouter/archive/refs/heads/${BRANCH}.tar.gz"
  TMP="$(mktemp -d)"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$ARCHIVE" | tar -xz -C "$TMP"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO- "$ARCHIVE" | tar -xz -C "$TMP"
  else
    echo "ERROR: need git, curl or wget to fetch the project." >&2
    exit 1
  fi
  mv "${TMP}/homerouter-${BRANCH}" "$APP_DIR"
  rm -rf "$TMP"
}

SCRIPT_PATH="${BASH_SOURCE[0]:-}"
if [ -n "$SCRIPT_PATH" ] && [ -f "$SCRIPT_PATH" ]; then
  SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
else
  SCRIPT_DIR=""
fi

if [ -n "$SCRIPT_DIR" ] && [ -f "${SCRIPT_DIR}/app.py" ] && [ -f "${SCRIPT_DIR}/gateway.py" ]; then
  APP_DIR="$SCRIPT_DIR"
  echo "==> Using local checkout: ${APP_DIR}"
elif [ -f "${APP_DIR}/app.py" ]; then
  echo "==> Using existing checkout: ${APP_DIR}"
else
  fetch_project
fi

# When launched from a URL, refresh an existing checkout on re-runs.
if [ -z "$SCRIPT_DIR" ] && [ -d "${APP_DIR}/.git" ]; then
  echo "==> Updating existing checkout"
  git -C "$APP_DIR" pull --ff-only 2>/dev/null || \
    echo "    update skipped (local changes or no network) — continuing"
fi

cd "$APP_DIR"

if [ ! -f app.py ] || [ ! -f gateway.py ] || [ ! -f templates/index.html ]; then
  echo "ERROR: project files are incomplete in ${APP_DIR}." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 3. System packages
# ---------------------------------------------------------------------------
if [ "$PLATFORM" = "termux" ]; then
  echo "==> [1/5] Updating Termux packages"
  pkg update -y
  pkg upgrade -y

  echo "==> [2/5] Installing Termux packages"
  pkg install -y python python-pip termux-services curl git clang make libffi openssl
  # These are optional: some Termux mirrors dropped python-requests, and
  # cryptography can also come from pip when the repo package is missing.
  # The venv below uses --system-site-packages, so repo versions are reused.
  pkg install -y python-cryptography >/dev/null 2>&1 || true
  pkg install -y python-requests >/dev/null 2>&1 || true
  command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock || true

else
  echo "==> [1/5] Updating Ubuntu/Debian packages"
  export DEBIAN_FRONTEND=noninteractive
  ${SUDO} apt-get update -y
  ${SUDO} apt-get upgrade -y

  echo "==> [2/5] Installing Ubuntu/Debian packages"
  ${SUDO} apt-get install -y python3 python3-pip python3-venv python3-requests curl git
fi

# ---------------------------------------------------------------------------
# 4. Python environment
# ---------------------------------------------------------------------------
echo "==> [3/5] Preparing Python environment at ${VENV}"
mkdir -p "$(dirname "$VENV")"

if [ "$PLATFORM" = "termux" ]; then
  # Reuse the native cryptography/requests packages from the Termux repo:
  # building cryptography with pip on Android needs a Rust toolchain.
  PY="python"
  [ -x "${VENV}/bin/python" ] || "$PY" -m venv --system-site-packages "$VENV"
  "${VENV}/bin/pip" install --no-input -r "${APP_DIR}/requirements-termux.txt"
  if ! "${VENV}/bin/python" -c 'import cryptography' >/dev/null 2>&1; then
    echo "    cryptography not found in system packages — trying pip"
    "${VENV}/bin/pip" install --no-input --only-binary :all: cryptography || \
      "${VENV}/bin/pip" install --no-input cryptography
  fi
else
  PY="python3"
  [ -x "${VENV}/bin/python" ] || "$PY" -m venv "$VENV"
  "${VENV}/bin/pip" install --no-input --upgrade pip
  "${VENV}/bin/pip" install --no-input -r "${APP_DIR}/requirements.txt"
fi

echo "==> Verifying dependencies"
"${VENV}/bin/python" - <<'PY'
import flask, requests, cryptography, speedtest
print("    flask / requests / cryptography / speedtest-cli: OK")
PY

# ---------------------------------------------------------------------------
# 5. Service
# ---------------------------------------------------------------------------
echo "==> [4/5] Creating service '${SERVICE}'"

create_manual_launcher() {
  cat > "${APP_DIR}/start.sh" <<EOF
${SHEBANG}
cd "${APP_DIR}"
exec "${VENV}/bin/python" app.py
EOF
  chmod +x "${APP_DIR}/start.sh"
  # Do not start a duplicate if the port is already serving the app.
  if ! "${VENV}/bin/python" -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:${PORT}/',timeout=2)" >/dev/null 2>&1; then
    nohup "${APP_DIR}/start.sh" >"${APP_DIR}/gwmon.log" 2>&1 &
  fi
  echo "    manual launcher created: ${APP_DIR}/start.sh"
}

if [ "$PLATFORM" = "termux" ]; then
  # termux-services 0.13+ stores service directories directly in $SVDIR
  # (normally $PREFIX/var/service); older releases symlink from etc/sv.
  SVDIR="${SVDIR:-${PREFIX}/var/service}"
  export SVDIR
  SOURCE_DIR="${SVDIR}/${SERVICE}"
  mkdir -p "${SOURCE_DIR}/log"
  rm -f "${SOURCE_DIR}/down"
  cat > "${SOURCE_DIR}/run" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
cd "${APP_DIR}"
export PATH="${VENV}/bin:\${PATH}"
exec "${VENV}/bin/python" app.py
EOF
  chmod +x "${SOURCE_DIR}/run"

  if command -v logger >/dev/null 2>&1; then
    LOG_CMD="exec logger -t gwmon"
  else
    mkdir -p "${PREFIX}/var/log"
    LOG_CMD="exec cat > ${PREFIX}/var/log/${SERVICE}.log"
  fi
  cat > "${SOURCE_DIR}/log/run" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
exec 2>&1
${LOG_CMD}
EOF
  chmod +x "${SOURCE_DIR}/log/run"

  if [ "$HAS_TERMUX_SVCS" -eq 1 ]; then
    svc_cmd stop "$SERVICE" >/dev/null 2>&1 || true
    svc_cmd enable "$SERVICE" || true

    # runit creates supervise/ asynchronously for a brand-new service; wait
    # for it before giving up on "sv up".
    for i in 1 2 3 4 5 6 7 8 9 10; do
      [ -e "${SOURCE_DIR}/supervise/ok" ] && break
      sleep 1
    done

    STARTED=0
    for i in 1 2 3 4 5; do
      if svc_cmd start "$SERVICE" >/dev/null 2>&1; then
        STARTED=1
        break
      fi
      sleep 1
    done
    if [ "$STARTED" -eq 1 ]; then
      echo "    termux-service enabled and started (auto-starts after Termux restarts)"
    else
      echo "    WARNING: service could not be started; try: sv up ${SERVICE}"
    fi
  else
    create_manual_launcher
  fi
else
  UNIT="/etc/systemd/system/${SERVICE}.service"
  if [ "$SYSTEMD_OK" -eq 1 ]; then
    ${SUDO} tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Gateway Monitor (router dashboard)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$(id -un)
Group=$(id -gn)
WorkingDirectory=${APP_DIR}
Environment=PATH=${VENV}/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=${VENV}/bin/python ${APP_DIR}/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
    if ${SUDO} systemctl daemon-reload && ${SUDO} systemctl enable --now "$SERVICE"; then
      echo "    systemd service enabled and started"
      # enable --now does not restart an already-active unit, so pull an
      # updated checkout into the running process explicitly.
      if ${SUDO} systemctl is-active --quiet "$SERVICE"; then
        ${SUDO} systemctl restart "$SERVICE"
      fi
    else
      echo "    systemctl could not start the service — using manual launcher"
      create_manual_launcher
    fi
  else
    echo "    systemd not running (WSL/container) — using manual launcher"
    create_manual_launcher
  fi
fi

# ---------------------------------------------------------------------------
# 6. Wait + summary
# ---------------------------------------------------------------------------
echo "==> [5/5] Waiting for the app on port ${PORT}"
OK=0
for i in $(seq 1 15); do
  if "${VENV}/bin/python" -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:${PORT}/',timeout=2)" >/dev/null 2>&1; then
    OK=1
    break
  fi
  sleep 2
done

if [ "$OK" -eq 1 ]; then
  echo "    app is up on port ${PORT}"
else
  echo "WARNING: the app did not respond yet. Check logs and start it manually with ${APP_DIR}/start.sh" >&2
fi

if [ "$PLATFORM" = "termux" ]; then
  if command -v svc-status >/dev/null 2>&1; then
    SERVICE_HELP="  svc-status gwmon      check service
  svc-stop gwmon        stop service
  svc-start gwmon       start service
  svc-disable gwmon     disable autostart"
  elif command -v sv-status >/dev/null 2>&1; then
    SERVICE_HELP="  sv-status gwmon       check service
  sv-stop gwmon         stop service
  sv-start gwmon        start service
  sv-disable gwmon      disable autostart"
  else
    SERVICE_HELP="  sv status gwmon       check service
  sv down gwmon         stop service
  sv up gwmon           start service
  sv-disable gwmon      disable autostart"
  fi
  LOG_HELP="  tail -f ${APP_DIR}/gwmon.log   (manual-launch fallback only)"
  NETWORK_HELP="  Open from a PC     : http://<phone-ip>:${PORT}"
else
  SERVICE_HELP="  systemctl status gwmon      check service
  systemctl stop gwmon        stop service
  systemctl start gwmon       start service
  systemctl disable gwmon     disable autostart
  journalctl -u gwmon -f      follow logs"
  LOG_HELP="  tail -f ${APP_DIR}/gwmon.log   (manual-launch fallback only)"
  NETWORK_HELP="  Open from another device: http://<machine-ip>:${PORT}"
fi

cat <<EOF

=========================================================
 Gateway Monitor installed at: ${APP_DIR}
 Running on port ${PORT}
=========================================================
 Open locally       : http://localhost:${PORT}
 ${NETWORK_HELP}

 Useful commands
${SERVICE_HELP}
${LOG_HELP}

 PWA installation
   Open http://localhost:${PORT} in Chrome, tap the menu
   ("Add to Home screen"). There is no in-app install button.
   localhost is a secure context, so no HTTPS is needed.
   Installing via http://<ip>:${PORT} will NOT offer the
   PWA prompt — use localhost on the device itself.
=========================================================
EOF

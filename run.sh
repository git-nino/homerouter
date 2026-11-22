#!/usr/bin/env bash
# Start the gateway monitor on port 7000.
set -e
cd "$(dirname "$0")"

# Keep the virtualenv off network-mounted drives (slow on WSL /mnt/c).
VENV="${HOME}/.venvs/gwmon"
if [ ! -x "${VENV}/bin/python" ]; then
  echo "Creating virtual environment at ${VENV}…"
  python3 -m venv "${VENV}"
fi
if ! "${VENV}/bin/python" -c 'import flask, speedtest' >/dev/null 2>&1; then
  echo "Installing dependencies…"
  "${VENV}/bin/pip" install -r requirements.txt
fi

echo "Gateway Monitor → http://localhost:7000"
exec "${VENV}/bin/python" app.py

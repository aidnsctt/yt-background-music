#!/bin/bash
# Restart the lofi menu bar player. Self-locating so it works wherever the repo
# is cloned — no hardcoded user/home/pyenv paths.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Prefer a project virtualenv (.venv is gitignored), then any venv on PATH,
# then the system python3.
if [ -x "$SCRIPT_DIR/.venv/bin/python3" ]; then
    PYTHON="$SCRIPT_DIR/.venv/bin/python3"
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "$VIRTUAL_ENV/bin/python3" ]; then
    PYTHON="$VIRTUAL_ENV/bin/python3"
else
    PYTHON="$(command -v python3 || true)"
fi

if [ -z "${PYTHON:-}" ]; then
    echo "error: no python3 found (create a venv with: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt)" >&2
    exit 1
fi

pkill -f "python3.*lofi.py" 2>/dev/null || true
pkill -f "mpv.*lofi-mpv" 2>/dev/null || true
sleep 0.5

nohup "$PYTHON" "$SCRIPT_DIR/lofi.py" > /tmp/lofi-player.log 2>&1 &
echo "Lofi player started (PID: $!) using $PYTHON"

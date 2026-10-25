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

# Match script arguments, not the interpreter name or arbitrary source text.
# Python signal handlers can remain pending inside AppKit's native modal/menu
# loop. Wait for shutdown, then reap survivors before starting another app.
PLAYER_PATTERN='(^|[ /])lofi[.]py([[:space:]]|$)'
OLD_PIDS="$(pgrep -f "$PLAYER_PATTERN" || true)"
if [ -n "$OLD_PIDS" ]; then
    kill $OLD_PIDS 2>/dev/null || true
    for _ in {1..30}; do
        STILL_RUNNING="$(pgrep -f "$PLAYER_PATTERN" || true)"
        [ -z "$STILL_RUNNING" ] && break
        sleep 0.1
    done
    STILL_RUNNING="$(pgrep -f "$PLAYER_PATTERN" || true)"
    if [ -n "$STILL_RUNNING" ]; then
        echo "Stopping unresponsive player instance(s): $STILL_RUNNING"
        kill -KILL $STILL_RUNNING 2>/dev/null || true
    fi
fi
pkill -f "mpv.*lofi-mpv" 2>/dev/null || true
sleep 0.5

# Detach the session and all standard streams. A bare nohup background child
# can be reaped when a terminal/automation command session closes.
"$PYTHON" - "$SCRIPT_DIR/lofi.py" <<'PY'
import subprocess
import sys

with open("/tmp/lofi-player.log", "w") as log:
    process = subprocess.Popen(
        [sys.executable, sys.argv[1]],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True,
    )
print(f"Lofi player started (PID: {process.pid}) using {sys.executable}")
PY

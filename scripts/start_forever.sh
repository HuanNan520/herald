#!/bin/bash
# Supervisor: restart the daemon if it crashes.
#
# The daemon writes its own daemon.log. This supervisor captures stdout/stderr
# to daemon.err separately — don't merge them, or every daemon log line shows
# up twice (daemon writes it, then the supervisor's 2>&1 catches it).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load .env for HERALD_HOME resolution.
if [ -f "$REPO/.env" ]; then
    set -a
    . "$REPO/.env"
    set +a
fi
: "${HERALD_HOME:=$HOME/.herald}"
mkdir -p "$HERALD_HOME"

LOG="$HERALD_HOME/daemon.log"
ERR="$HERALD_HOME/daemon.err"

while true; do
    bash "$SCRIPT_DIR/start.sh" >> "$ERR" 2>&1
    code=$?
    ts="[$(date '+%Y-%m-%d %H:%M:%S')]"
    echo "$ts daemon exited (code=$code), restarting in 10s" >> "$LOG"
    echo "$ts daemon exited (code=$code), restarting in 10s" >> "$ERR"
    sleep 10
done

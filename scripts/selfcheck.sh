#!/bin/bash
# Idempotent health check. Safe to cron every few minutes.
# Output is concise; anomalies are prefixed with WARN / ERR.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

if [ -f "$REPO/.env" ]; then
    set -a
    . "$REPO/.env"
    set +a
fi
: "${HERALD_HOME:=$HOME/.herald}"
: "${TMUX_SESSION:=herald}"

printf "=== selfcheck %s ===\n" "$(date '+%Y-%m-%d %H:%M:%S')"

# 1. tmux session
if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    echo "OK tmux session '$TMUX_SESSION' alive"
else
    echo "ERR tmux session '$TMUX_SESSION' missing, restarting"
    tmux new -d -s "$TMUX_SESSION" "bash $SCRIPT_DIR/start_forever.sh"
    sleep 2
    if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
        echo "FIX tmux restarted"
    else
        echo "ERR tmux restart failed"
    fi
fi

# 2. daemon process (pid file check)
pidfile="$HERALD_HOME/daemon.pid"
if [ -f "$pidfile" ]; then
    pid=$(cat "$pidfile" 2>/dev/null)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "OK daemon alive: pid=$pid"
    else
        echo "ERR pidfile has $pid but no such process (supervisor will restart)"
    fi
else
    echo "WARN no pidfile yet (daemon may be starting)"
    fallback=$(ps -ef | grep -E "daemon\.py" | grep -v grep | wc -l)
    echo "  fallback ps sees $fallback daemon.py processes"
fi

# 3. inbox / pending / processed counts
inbox_n=$(ls "$HERALD_HOME/inbox/" 2>/dev/null | wc -l)
pending_n=$(ls "$HERALD_HOME/pending/" 2>/dev/null | wc -l)
processed_n=$(ls "$HERALD_HOME/processed/" 2>/dev/null | wc -l)
echo "STAT inbox=$inbox_n pending=$pending_n processed=$processed_n"
[ "$inbox_n" -gt 30 ] && echo "WARN inbox backlog — daemon may be stuck"
[ "$pending_n" -gt 50 ] && echo "WARN pending backlog"

# 4. errors in daemon.log within the last 10 minutes (time-filtered, so old
#    fixed errors don't get re-reported)
threshold="[$(date -d '-10 minutes' '+%Y-%m-%d %H:%M:%S')]"
errs=$(awk -v th="$threshold" '$0 > th' "$HERALD_HOME/daemon.log" 2>/dev/null \
    | grep -iE "failed|exception|crash|error|traceback" | tail -3)
if [ -n "$errs" ]; then
    echo "WARN errors in last 10min:"
    echo "$errs" | sed 's/^/  /'
else
    echo "OK no errors in last 10min"
fi

# 5. Telegram API connectivity
if [ -n "${TG_BOT_TOKEN:-}" ]; then
    resp=$(curl -sS --max-time 8 "https://api.telegram.org/bot$TG_BOT_TOKEN/getMe" 2>/dev/null)
    if echo "$resp" | grep -q '"ok":true'; then
        echo "OK Telegram API reachable"
    else
        echo "ERR Telegram API: ${resp:0:150}"
    fi
else
    echo "WARN TG_BOT_TOKEN not in env — skipping API check"
fi

# 6. daemon.log size (truncate if it grows past 2 MB)
if [ -f "$HERALD_HOME/daemon.log" ]; then
    size=$(stat -c%s "$HERALD_HOME/daemon.log")
    size_kb=$((size / 1024))
    echo "STAT daemon.log: ${size_kb} KB"
    if [ "$size" -gt 2097152 ]; then
        tail -500 "$HERALD_HOME/daemon.log" > "$HERALD_HOME/daemon.log.tmp" \
            && mv "$HERALD_HOME/daemon.log.tmp" "$HERALD_HOME/daemon.log"
        echo "FIX truncated daemon.log to last 500 lines"
    fi
fi

# 7. Archive processed/ when it grows past 500 files
if [ "$processed_n" -gt 500 ]; then
    arc="$HERALD_HOME/archive_$(date +%Y%m%d_%H%M).tar.gz"
    tar czf "$arc" -C "$HERALD_HOME/processed" . 2>/dev/null \
        && rm -rf "$HERALD_HOME/processed"/* \
        && echo "FIX archived processed/ → $arc"
fi

printf "=== selfcheck done ===\n"

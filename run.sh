#!/usr/bin/env bash
# Start the mlai shim in the background and keep it running after logout.
set -euo pipefail
export PATH="$HOME/.npm-global/bin:$PATH"
cd "$(dirname "$0")"
LOG="${MLAI_SHIM_LOG:-$HOME/mlai-shim/shim.log}"
PIDFILE="$HOME/mlai-shim/shim.pid"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "already running (pid $(cat "$PIDFILE"))"
  exit 0
fi

nohup python3 shim.py >>"$LOG" 2>&1 &
echo $! >"$PIDFILE"
sleep 1
if curl -s --max-time 5 http://127.0.0.1:8787/v1/models >/dev/null; then
  echo "mlai shim up on http://127.0.0.1:8787/v1 (pid $(cat "$PIDFILE"), log $LOG)"
else
  echo "shim failed to start; see $LOG" >&2
  tail -20 "$LOG" >&2 || true
  exit 1
fi

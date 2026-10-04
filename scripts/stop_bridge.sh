#!/usr/bin/env sh
# Stops the background bridge, if one is running (Linux / macOS). The Windows equivalent is stop_bridge.ps1.
#   ./scripts/stop_bridge.sh [--quiet]
pids=$(pgrep -f ' -m meshllm( |$)' 2>/dev/null)
if [ -n "$pids" ]; then
  kill $pids 2>/dev/null
  sleep 1
  for p in $pids; do kill -0 "$p" 2>/dev/null && kill -9 "$p" 2>/dev/null; done   # still there: force it
  [ "$1" = "--quiet" ] || echo "Stopped bridge (PID $(echo $pids | tr '\n' ' '))."
else
  [ "$1" = "--quiet" ] || echo "Bridge was not running."
fi
rm -f "$(dirname "$0")/../logs/bridge.pid"

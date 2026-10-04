#!/usr/bin/env sh
# Starts (or restarts) the bridge in the background (Linux / macOS). The Windows equivalent is start_bridge.ps1.
#   ./scripts/start_bridge.sh [bridge flags...]      e.g. ./scripts/start_bridge.sh --model qwen2.5:7b
# It finds the radio by itself. Logs (overwritten on each start): logs/bridge.log and logs/bridge.err.log
# It can be run from any folder: it works from the project folder, which is the parent of this scripts/ folder.
here=$(cd "$(dirname "$0")" && pwd)
dir=$(dirname "$here")
cd "$dir" || exit 1
"$here/stop_bridge.sh" --quiet
sleep 0.5
py="$dir/.venv/bin/python"                       # the environment made by setup.sh if there is one
[ -x "$py" ] || py=$(command -v python3 || command -v python)
[ -n "$py" ] || { echo "No Python found. Run ./setup.sh first." >&2; exit 1; }
mkdir -p logs
nohup "$py" -u -m meshllm "$@" > logs/bridge.log 2> logs/bridge.err.log &
pid=$!
echo $pid > logs/bridge.pid
sleep 2
if ! kill -0 "$pid" 2>/dev/null; then            # it died straight away (port already in use, missing package, ...)
  echo "The bridge stopped right after starting. Last lines of logs/bridge.err.log:" >&2
  tail -n 8 logs/bridge.err.log >&2
  rm -f logs/bridge.pid
  exit 1
fi
port=8080
want=""
for a in "$@"; do                                # show the port that was asked for, if any
  if [ -n "$want" ]; then port=$a; want=""; fi
  [ "$a" = "--web-port" ] && want=1
done
echo "Bridge started in the background (PID $pid). Web UI: http://127.0.0.1:$port/   Log: $dir/logs/bridge.log"

#!/bin/bash
# Container entry point: build the bridge's command line from environment variables, then replace this shell with it.
#
#   MESHLLM_TCP         Wi-Fi radio, HOST or HOST:PORT        -> --tcp
#   MESHLLM_PORT        serial port, e.g. /dev/ttyUSB0        -> --port
#   MESHLLM_FALLBACK    failover list, comma-separated      -> one --fallback each, e.g. usb:/dev/ttyUSB1,tcp:192.168.1.50
#   MESHLLM_OLLAMA_URL  where Ollama is (default http://ollama:11434)
#   MESHLLM_MODEL       model name (optional)                 -> --model
#   MESHLLM_EXTRA_ARGS  anything else, split the way a shell would (quotes work, nothing is executed)
#
# Anything given as the container's command (for example `--demo`) is added at the end. Bluetooth (--ble) does not work in a
# container (it needs the host's BlueZ and D-Bus), so there is deliberately no variable for it.
set -eu

args=(--web-host 0.0.0.0 --ollama-url "${MESHLLM_OLLAMA_URL:-http://ollama:11434}")

demo=0
for a in "$@"; do
    if [ "$a" = "--demo" ]; then demo=1; fi
done

# --demo has a simulated radio, a temporary database and its own model, and says so if it is given these, so leave them out then
if [ "$demo" = 0 ]; then
    data="${MESHLLM_DATA_DIR:-/data}"      # (the variable exists so tests can point it elsewhere)
    # a volume or bind mount with the wrong owner would otherwise end in a bare database traceback
    if [ ! -w "$data" ]; then
        echo "$data is not writable by uid $(id -u): fix the volume/bind-mount ownership (see docs/setup.md)" >&2
        exit 1
    fi
    args+=(--db "$data/audit.db")
    if [ -n "${MESHLLM_TCP:-}" ];   then args+=(--tcp "$MESHLLM_TCP"); fi
    if [ -n "${MESHLLM_PORT:-}" ];  then args+=(--port "$MESHLLM_PORT"); fi
    if [ -n "${MESHLLM_FALLBACK:-}" ]; then
        # comma-separated KIND:VALUE entries in priority order; one --fallback each (the bridge validates them and says what is wrong)
        IFS=',' read -r -a fallbacks <<< "$MESHLLM_FALLBACK"
        for f in "${fallbacks[@]}"; do
            if [ -n "$f" ]; then args+=(--fallback "$f"); fi
        done
    fi
    if [ -n "${MESHLLM_MODEL:-}" ]; then args+=(--model "$MESHLLM_MODEL"); fi
fi

if [ -n "${MESHLLM_EXTRA_ARGS:-}" ]; then
    # a typo such as an unclosed quote stops the container with a clear line, rather than starting with half the flags
    python -c 'import os, shlex, sys
try:
    shlex.split(os.environ["MESHLLM_EXTRA_ARGS"])
except ValueError as e:
    sys.exit("MESHLLM_EXTRA_ARGS is not valid (%s): %s" % (e, os.environ["MESHLLM_EXTRA_ARGS"]))' || exit 2
    # shlex does the quote handling; its output is NUL-separated so no word is ever re-split or evaluated by this shell
    while IFS= read -r -d '' word; do
        args+=("$word")
    done < <(python -c 'import os, shlex, sys; sys.stdout.write("".join(w + "\0" for w in shlex.split(os.environ["MESHLLM_EXTRA_ARGS"])))')
fi

# exec: Python becomes process 1 and receives `docker stop`'s SIGTERM itself (the bridge turns it into a clean shutdown)
echo "meshllm: the dashboard listens on 0.0.0.0 in this container; publish it only as 127.0.0.1:PORT:8080 (it has no login)" >&2
exec python -m meshllm "${args[@]}" "$@"

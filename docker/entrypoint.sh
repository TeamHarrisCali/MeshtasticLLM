#!/bin/bash
# Container entry point: build the bridge's command line from environment variables, then replace this shell with it.
#
#   MESHLLM_TCP            Wi-Fi radio, HOST or HOST:PORT        -> --tcp
#   MESHLLM_PORT           serial port, e.g. /dev/ttyUSB0        -> --port
#   MESHLLM_FALLBACK       failover list, comma-separated      -> one --fallback each, e.g. usb:/dev/ttyUSB1,tcp:192.0.2.50
#   MESHLLM_OLLAMA_URL     where Ollama is (default http://ollama:11434)
#   MESHLLM_MODEL          model name (optional)                 -> --model
#   MESHLLM_EXTRA_ARGS     anything else, split the way a shell would (quotes work, nothing is executed)
#   MESHLLM_PUBLISH_ADDR   the host address Compose publishes the dashboard on (set by docker-compose.yml from MESHLLM_WEB_BIND;
#                          default 127.0.0.1). Anything but a 127.x.x.x or ::1 address means "reachable from the network"
#   MESHLLM_ALLOWED_HOSTS  host names / addresses the dashboard may be browsed by, comma-separated      -> one --allowed-host each
#
# THE LOGIN (docker-compose.login.yml). The password hash is a Compose secret: a file mounted at /run/secrets/meshllm_admin_hash
# (never an environment variable, which `docker inspect` shows). A Compose file secret keeps the host file's owner and mode, so the
# mode-600 file `python -m meshllm --set-password` writes is unreadable for the app's user (uid 10001). The login override therefore
# starts this script as root with exactly three capabilities (DAC_OVERRIDE to read the file, SETUID and SETGID to drop root) and the
# first stage below
#   1. copies the file into a 0400 file owned by 10001 on a small tmpfs (/run/meshllm, memory only, gone with the container),
#   2. re-runs this script as 10001 with no supplementary groups but the ones Docker added (group_add), and the second stage
#      (everything below `# ---- stage 2`, including all parsing of the environment) runs as 10001 with no capabilities.
# Root never runs the bridge, Python, or anything built from MESHLLM_EXTRA_ARGS.
#
# LAN RULE. If MESHLLM_PUBLISH_ADDR is not loopback the dashboard is published to the network, and this script then refuses to start
# unless a login is configured AND MESHLLM_ALLOWED_HOSTS is set (and never for --demo, which has no login). It also tells the app
# (MESHLLM_PUBLISH_LAN=1) so that the app's own start-up check refuses a login-less wildcard bind too.
#
# Anything given as the container's command (for example `--demo`) is added at the end. Bluetooth (--ble) does not work in a
# container (it needs the host's BlueZ and D-Bus), so there is deliberately no variable for it.
set -eu -o pipefail

die() { local code="$1"; shift; echo "meshllm: $*" >&2; exit "$code"; }      # die CODE message

APP_UID=10001                                    # the image's user (Dockerfile); also what docker-compose.login.yml's tmpfs is owned by
secret_src="${MESHLLM_SECRETS_DIR:-/run/secrets}/meshllm_admin_hash"      # (the two variables exist so tests can point elsewhere)
secret_copy="${MESHLLM_RUN_DIR:-/run/meshllm}/admin.hash"

# ---- stage 1: only when started as root (docker-compose.login.yml) ----------------------------------------------------------------
if [ "$(id -u)" = 0 ]; then
    [ -f "$secret_src" ] || die 1 "refusing to run the bridge as root. Root is only for copying the dashboard password hash, and $secret_src is not there (is docker-compose.login.yml in use?)"
    # keep the groups Docker added with group_add (the USB serial device's group) but never root's own group 0
    keep_groups="$(id -G | tr ' ' '\n' | { grep -vx 0 || true; } | paste -sd, -)"
    drop=(setpriv --reuid "$APP_UID" --regid "$APP_UID")
    if [ -n "$keep_groups" ]; then drop+=(--groups "$keep_groups"); else drop+=(--clear-groups); fi
    # root reads the secret (DAC_OVERRIDE) but the copy is written by the unprivileged user, so it is born 0400 and owned by it and no
    # chown capability is needed. head: a password hash is one short line; never copy an arbitrary big file into memory
    if ! head -c 4096 -- "$secret_src" | "${drop[@]}" /bin/sh -c 'umask 277 && cat > "$1"' sh "$secret_copy"; then
        die 1 "could not copy the dashboard password hash to $secret_copy (it needs the tmpfs from docker-compose.login.yml and the capability DAC_OVERRIDE to read $secret_src)"
    fi
    [ -s "$secret_copy" ] || die 1 "the dashboard password hash file ($secret_src) is empty"
    export MESHLLM_PASSWORD_HASH_FILE="$secret_copy"
    exec "${drop[@]}" /bin/bash "$0" "$@"
fi

# ---- stage 2: the unprivileged user ----------------------------------------------------------------------------------------------
args=(--web-host 0.0.0.0 --ollama-url "${MESHLLM_OLLAMA_URL:-http://ollama:11434}")

demo=0
for a in "$@"; do
    if [ "$a" = "--demo" ]; then demo=1; fi
done

# where Compose publishes the port: only a 127.x.x.x or ::1 host address keeps it on this computer
publish="${MESHLLM_PUBLISH_ADDR:-127.0.0.1}"
publish="${publish#[}"; publish="${publish%]}"
lan=1
if [[ "$publish" =~ ^127(\.[0-9]{1,3}){3}$ ]] || [ "$publish" = "::1" ]; then lan=0; fi

# the dashboard login: a hash file made by --set-password, from the Compose secret (copied by stage 1) or named by MESHLLM_PASSWORD_HASH_FILE
login=""
if [ "$demo" = 0 ]; then
    if [ -n "${MESHLLM_PASSWORD_HASH_FILE:-}" ]; then
        login="$MESHLLM_PASSWORD_HASH_FILE"
    elif [ -e "$secret_src" ]; then
        login="$secret_src"
    fi
    if [ -n "$login" ] && [ ! -r "$login" ]; then
        die 1 "the dashboard password hash file $login is not readable by uid $(id -u). Start with docker-compose.login.yml (it copies the file for you), or make the host file readable by uid $APP_UID (chown $APP_UID, mode 0400)."
    fi
fi

allowed=()
if [ -n "${MESHLLM_ALLOWED_HOSTS:-}" ]; then
    IFS=',' read -r -a hosts <<< "$MESHLLM_ALLOWED_HOSTS"
    for h in "${hosts[@]}"; do
        h="${h//[[:space:]]/}"
        if [ -n "$h" ]; then allowed+=("$h"); fi
    done
fi

if [ "$lan" = 1 ]; then
    # the dashboard would be reachable from the network (MESHLLM_WEB_BIND): never without a login, never the demo
    if [ "$demo" = 1 ]; then
        die 78 "--demo has no login and must not be published beyond this computer (MESHLLM_PUBLISH_ADDR=$publish)"
    fi
    if [ -z "$login" ]; then
        die 78 "MESHLLM_WEB_BIND=$publish publishes the dashboard to the network, which needs the login: set a password with 'python -m meshllm --set-password', point MESHLLM_ADMIN_HASH_FILE at the file it wrote and add -f docker-compose.login.yml (docs/setup.md, Run with Docker). Refusing to start without it."
    fi
    if [ "${#allowed[@]}" = 0 ]; then
        die 78 "MESHLLM_WEB_BIND=$publish publishes the dashboard to the network, which needs MESHLLM_ALLOWED_HOSTS: the address or name you will type in the browser, comma-separated (for example 192.0.2.10,radio.test). Refusing to start without it."
    fi
    export MESHLLM_PUBLISH_LAN=1
fi

if [ -n "$login" ]; then
    args+=(--password-hash-file "$login")
    # the app wants an allowed-host list with any login on a wildcard bind; loopback names are always accepted, so naming one changes nothing
    if [ "${#allowed[@]}" = 0 ]; then allowed=(127.0.0.1); fi
fi
if [ "$demo" = 0 ]; then
    for h in ${allowed[@]+"${allowed[@]}"}; do args+=(--allowed-host "$h"); done
fi

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
if [ -n "$login" ]; then
    echo "meshllm: the dashboard login is ON (password hash read from a file, never from the environment); it listens on 0.0.0.0 in this container, published on $publish" >&2
else
    echo "meshllm: the dashboard listens on 0.0.0.0 in this container; publish it only as 127.0.0.1:PORT:8080 (it has no login)" >&2
fi
exec python -m meshllm "${args[@]}" "$@"

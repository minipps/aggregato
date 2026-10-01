#!/bin/sh
# The entrypoint supervises the API and scheduler in one container. It restarts the scheduler if it
# exits while keeping the API process available.
set -eu

api_pid=""
worker_pid=""

shutdown() {
    [ -n "$api_pid" ] && kill -TERM "$api_pid" 2>/dev/null || true
    [ -n "$worker_pid" ] && kill -TERM "$worker_pid" 2>/dev/null || true
    wait
    exit 0
}
trap shutdown TERM INT

# AGGREGATO_RELOAD is the development overlay's switch (docker/compose.dev.yml), which bind-mounts
# the source over /app/aggregato. Empty or unset in production: a watcher there is overhead, and a
# code change means a new image.
# Trust forwarded scheme/host headers only from the explicitly configured proxy addresses. When a
# trusted proxy reports HTTPS, the API derives Secure session cookies from that request scheme.
uvicorn aggregato.main:app \
    --loop asyncio \
    --host "${AGGREGATO_HOST:-0.0.0.0}" \
    --port "${AGGREGATO_PORT:-8000}" \
    --proxy-headers \
    --forwarded-allow-ips "${AGGREGATO_FORWARDED_ALLOW_IPS:-127.0.0.1}" \
    ${AGGREGATO_RELOAD:+--reload} &
api_pid=$!

python -m aggregato.worker &
worker_pid=$!

# Whichever exits first decides: the API exiting is fatal, the scheduler exiting is recoverable.
while true; do
    if ! kill -0 "$api_pid" 2>/dev/null; then
        echo "api process exited; stopping" >&2
        shutdown
    fi
    if ! kill -0 "$worker_pid" 2>/dev/null; then
        echo "scheduler exited; restarting" >&2
        python -m aggregato.worker &
        worker_pid=$!
    fi
    sleep 5
done

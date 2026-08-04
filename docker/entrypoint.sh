#!/bin/sh
# Two processes from one image (plan.md Complexity Tracking): the API serves regardless of what the
# scheduler is doing, which is what 's containment requires. The scheduler is restarted on its
# own if it dies; a dead scheduler must never take browsing down with it.
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
uvicorn aggregato.main:app \
    --host "${AGGREGATO_HOST:-0.0.0.0}" \
    --port "${AGGREGATO_PORT:-8000}" \
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

#!/bin/bash
# Voice Clone Pro container entrypoint.
#
# Starts the bgutil PO Token provider server (loopback only) BEFORE
# the app, waits briefly for its port, then execs the app command.
# The provider feeds YouTube proof-of-origin tokens to yt-dlp via the
# bgutil-ytdlp-pot-provider plugin (see app/services/downloader.py).
#
# Failure policy: a dead or slow provider must NEVER take the bot
# down. If the server build is missing, or the port never opens, the
# app still starts — the plugin then warns and extraction continues
# exactly as it did before the sidecar existed. The supervisor loop
# below also restarts the server if it crashes mid-run.
set -u

POT_PORT="${POT_SERVER_PORT:-4416}"
POT_DIR="${POT_SERVER_HOME:-/opt/bgutil-pot-server}"
POT_MAIN="$POT_DIR/build/main.js"

if [ -f "$POT_MAIN" ]; then
    (
        while true; do
            echo "[entrypoint] starting bgutil POT server on 127.0.0.1:${POT_PORT}"
            node "$POT_MAIN" --port "$POT_PORT"
            echo "[entrypoint] bgutil POT server exited (rc=$?) — restarting in 5s"
            sleep 5
        done
    ) &

    pot_up=0
    for _ in $(seq 1 30); do
        if (exec 3<>"/dev/tcp/127.0.0.1/${POT_PORT}") 2>/dev/null; then
            exec 3>&- 3<&-
            pot_up=1
            break
        fi
        sleep 1
    done
    if [ "$pot_up" = "1" ]; then
        echo "[entrypoint] bgutil POT server is up on 127.0.0.1:${POT_PORT}"
    else
        echo "[entrypoint] WARNING: bgutil POT server did not open port ${POT_PORT} within 30s — starting app without it"
    fi
else
    echo "[entrypoint] WARNING: bgutil POT server build not found at ${POT_MAIN} — starting app without it"
fi

exec "$@"

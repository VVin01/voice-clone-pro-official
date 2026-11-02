#!/bin/bash
# Voice Clone Pro container entrypoint.
#
# Starts TWO loopback sidecars BEFORE the app, each with a bounded
# wait, then execs the app command:
#   1. the bgutil PO Token provider server (feeds YouTube
#      proof-of-origin tokens to yt-dlp via the
#      bgutil-ytdlp-pot-provider plugin), and
#   2. the usque Cloudflare WARP egress tunnel (SOCKS5 proxy that
#      YouTube attempts route through, because YouTube walls this
#      host's own egress IP — see app/services/downloader.py).
#
# Failure policy: a dead or slow sidecar must NEVER take the bot
# down. If a sidecar build is missing, registration fails, or a
# port never opens, the app still starts — extraction then behaves
# exactly as it did before the sidecars existed. Supervisor loops
# restart each sidecar if it crashes mid-run.
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

# ── WARP egress sidecar (usque, Cloudflare WARP free tier) ─────────
#
# Device config: registered ONCE, anonymously, at first boot and
# kept on the persistent disk (settings storage_root, /app/storage)
# so the same WARP device survives restarts/deploys. The config
# contains the device private key — it is NEVER committed to the
# repo (public) and NEVER baked into the image. If registration
# fails (no network, Cloudflare API error), log it and continue
# WITHOUT the tunnel; registration is retried on the next boot.
WARP_PORT="${WARP_SOCKS_PORT:-18080}"
WARP_BIN="${WARP_BIN:-/opt/usque/usque}"
WARP_CONFIG="${WARP_CONFIG_PATH:-/app/storage/warp/usque.json}"

if [ -x "$WARP_BIN" ]; then
    if [ ! -f "$WARP_CONFIG" ]; then
        echo "[entrypoint] no WARP device config at ${WARP_CONFIG} — registering a fresh anonymous WARP device"
        mkdir -p "$(dirname "$WARP_CONFIG")"
        if "$WARP_BIN" --config "$WARP_CONFIG" register --accept-tos; then
            echo "[entrypoint] WARP device registered; config stored at ${WARP_CONFIG}"
        else
            echo "[entrypoint] WARNING: WARP device registration failed — starting app without the WARP tunnel"
        fi
    fi
    if [ -f "$WARP_CONFIG" ]; then
        (
            while true; do
                echo "[entrypoint] starting usque WARP SOCKS5 (--http2) on 127.0.0.1:${WARP_PORT}"
                "$WARP_BIN" --config "$WARP_CONFIG" socks --http2 --bind 127.0.0.1 --port "$WARP_PORT"
                echo "[entrypoint] usque WARP exited (rc=$?) — restarting in 5s"
                sleep 5
            done
        ) &

        warp_up=0
        for _ in $(seq 1 30); do
            if (exec 3<>"/dev/tcp/127.0.0.1/${WARP_PORT}") 2>/dev/null; then
                exec 3>&- 3<&-
                warp_up=1
                break
            fi
            sleep 1
        done
        if [ "$warp_up" = "1" ]; then
            echo "[entrypoint] usque WARP SOCKS5 is up on 127.0.0.1:${WARP_PORT}"
        else
            echo "[entrypoint] WARNING: usque WARP SOCKS5 did not open port ${WARP_PORT} within 30s — starting app without it"
        fi
    fi
else
    echo "[entrypoint] WARNING: usque binary not found at ${WARP_BIN} — starting app without the WARP tunnel"
fi

exec "$@"

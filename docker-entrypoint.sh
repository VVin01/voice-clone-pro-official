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
                "$WARP_BIN" --config "$WARP_CONFIG" socks --http2 --bind 127.0.0.1 --port "$WARP_PORT" 2>&1 | grep -v --line-buffered "failed during negotiation: Invalid Version"
                echo "[entrypoint] usque WARP exited (rc=$?) — restarting in 5s"
                sleep 5
            done
        ) &

        warp_up=0
        for _ in $(seq 1 30); do
            if (exec 3<>"/dev/tcp/127.0.0.1/${WARP_PORT}" && printf '\x05\x01\x00' >&3) 2>/dev/null; then
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

# ── Local Telegram Bot API loopback sidecar (lifts 50 MB -> 2,000 MB) ───
# Starts only when TELEGRAM_API_ID and TELEGRAM_API_HASH are configured and
# binds strictly to 127.0.0.1 so raw bot tokens are never exposed publicly.
# Auto-exports TELEGRAM_BOT_API_URL + TELEGRAM_BOT_API_IS_LOCAL=true when up
# (unless the operator explicitly set TELEGRAM_BOT_API_URL).
TBA_PORT="${LOCAL_BOT_API_PORT:-8081}"
TBA_BIN="${LOCAL_BOT_API_BIN:-/opt/telegram-bot-api/run-telegram-bot-api}"
TBA_DIR="${LOCAL_BOT_API_DIR:-/app/storage/telegram-bot-api}"
TBA_TMP="${LOCAL_BOT_API_TEMP:-/tmp/telegram-bot-api}"

if [ -x "$TBA_BIN" ] && [ -n "${TELEGRAM_API_ID:-}" ] && [ -n "${TELEGRAM_API_HASH:-}" ]; then
    mkdir -p "$TBA_DIR" "$TBA_TMP"
    (
        while true; do
            echo "[entrypoint] starting co-located telegram-bot-api (--local) on 127.0.0.1:${TBA_PORT}"
            find "$TBA_DIR" -type f -mmin +120 -not -name '*.binlog*' -delete 2>/dev/null || true
            "$TBA_BIN" --local --http-ip-address=127.0.0.1 --http-port="$TBA_PORT" --dir="$TBA_DIR" --temp-dir="$TBA_TMP" --verbosity=1
            echo "[entrypoint] telegram-bot-api exited (rc=$?) — restarting in 5s"
            sleep 5
        done
    ) &

    tba_up=0
    for _ in $(seq 1 15); do
        if (exec 3<>"/dev/tcp/127.0.0.1/${TBA_PORT}") 2>/dev/null; then
            exec 3>&- 3<&-
            tba_up=1
            break
        fi
        sleep 1
    done
    if [ "$tba_up" = "1" ]; then
        echo "[entrypoint] telegram-bot-api (--local) is up on 127.0.0.1:${TBA_PORT}"
        if [ -z "${TELEGRAM_BOT_API_URL:-}" ]; then
            export TELEGRAM_BOT_API_URL="http://127.0.0.1:${TBA_PORT}"
            export TELEGRAM_BOT_API_IS_LOCAL="true"
        fi
    else
        echo "[entrypoint] WARNING: telegram-bot-api did not open 127.0.0.1:${TBA_PORT} within 15s — app keeps official 50 MB API"
    fi
elif [ -x "$TBA_BIN" ]; then
    echo "[entrypoint] TELEGRAM_API_ID/TELEGRAM_API_HASH unset — skipping co-located telegram-bot-api sidecar"
fi

exec "$@"

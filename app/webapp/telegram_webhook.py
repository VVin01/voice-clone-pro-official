"""
telegram_webhook.py - Telegram webhook intake for the aiogram bot.

The bot can receive updates in one of two modes, selected by ``BOT_MODE``:

* ``polling`` (default) - the long-polling loop started in ``main.py``,
  unchanged from previous releases.
* ``webhook`` - polling is NOT started. Telegram POSTs updates to
  ``{PUBLIC_BASE_URL}{BOT_WEBHOOK_PATH}`` on the existing FastAPI Mini App
  server, and every accepted update is fed into the SAME aiogram
  ``Dispatcher`` / ``Bot`` pair that polling would have used, so handlers,
  the access-control middleware and FSM flows behave identically.

Security: every request must carry the ``X-Telegram-Bot-Api-Secret-Token``
header equal (constant-time compare) to ``TELEGRAM_WEBHOOK_SECRET``.
Anything else gets 403 and the body is never parsed. If the secret is not
configured, ALL requests are rejected. The secret value is never logged.

Registration (``setWebhook``) runs on every boot in webhook mode and is
idempotent: identical url / secret / allowed_updates, and
``drop_pending_updates=False`` so a re-registration (including two
instances briefly coexisting during a deploy overlap) never discards
queued updates. The webhook is NEVER deleted on shutdown.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
from collections import OrderedDict
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from app.config import settings

logger = logging.getLogger(__name__)

BOT_MODE_POLLING = "polling"
BOT_MODE_WEBHOOK = "webhook"
VALID_BOT_MODES = (BOT_MODE_POLLING, BOT_MODE_WEBHOOK)

DEFAULT_WEBHOOK_PATH = "/telegram/webhook"
SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"

# Telegram's documented constraint for secret_token: 1-256 chars of
# A-Z, a-z, 0-9, "_" and "-".
_SECRET_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")

# How many recent update_ids to remember for retry de-duplication.
# Telegram update_ids are sequential per bot; a few thousand covers any
# realistic retry window while staying a few hundred KB at most.
RECENT_UPDATE_IDS_MAX = 4096

# Defence-in-depth body cap for the intake endpoint (hunt #6). Telegram
# updates are small JSON documents (a message with a 4096-char text is
# still only a few KB), so 1 MiB is generous — but the endpoint is
# public, sits in front of the app's rate limiter by design, and the
# ASGI stack applies no size cap of its own to this path: without a cap
# an unbounded (e.g. chunked) body would be buffered whole into memory
# before JSON parsing.
MAX_UPDATE_BODY_BYTES = 1024 * 1024


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def normalize_webhook_path(raw: Optional[str]) -> str:
    """Return a clean absolute path; empty/None falls back to the default."""
    path = (raw or "").strip() or DEFAULT_WEBHOOK_PATH
    if not path.startswith("/"):
        path = "/" + path
    return path


def get_webhook_path() -> str:
    return normalize_webhook_path(getattr(settings, "bot_webhook_path", ""))


def get_configured_bot_mode() -> str:
    """The BOT_MODE as configured; unknown values fall back to polling."""
    raw = (getattr(settings, "bot_mode", "") or "").strip().lower()
    if not raw:
        return BOT_MODE_POLLING
    if raw not in VALID_BOT_MODES:
        logger.warning(
            "Unknown BOT_MODE value %r; falling back to %r.", raw, BOT_MODE_POLLING
        )
        return BOT_MODE_POLLING
    return raw


def build_webhook_url() -> str:
    base = (getattr(settings, "public_base_url", "") or "").strip().rstrip("/")
    return f"{base}{get_webhook_path()}"


def webhook_config_problems() -> list[str]:
    """Human-readable problems that make webhook mode unusable.

    Messages name env vars only and never include secret values.
    """
    problems: list[str] = []
    secret = getattr(settings, "telegram_webhook_secret", "") or ""
    if not secret:
        problems.append("TELEGRAM_WEBHOOK_SECRET is not set")
    elif not _SECRET_TOKEN_RE.match(secret):
        problems.append(
            "TELEGRAM_WEBHOOK_SECRET has an invalid format "
            "(Telegram allows 1-256 chars of A-Z a-z 0-9 _ -)"
        )
    base = (getattr(settings, "public_base_url", "") or "").strip()
    if not base:
        problems.append("PUBLIC_BASE_URL is not set")
    elif not base.lower().startswith("https://"):
        problems.append("PUBLIC_BASE_URL must start with https:// (Telegram requires HTTPS)")
    # A webhook path that collides with the Mini App's own routes would
    # shadow a live API/static route: the intake dispatcher intercepts
    # POSTs to the configured path before routing. Refuse such a path
    # (resolve_bot_mode then falls back to polling, loudly) instead of
    # silently breaking part of the Mini App (hunt #6).
    path = get_webhook_path()
    if path == "/" or path == "/api" or path.startswith("/api/") or path.startswith("/static"):
        problems.append(
            "BOT_WEBHOOK_PATH collides with the Mini App routes; choose a dedicated path"
        )
    return problems


def resolve_bot_mode(*, web_server_in_process: bool) -> str:
    """Effective bot mode for this process.

    Webhook mode is only honoured when it can actually work: the secret
    and public URL are valid AND the FastAPI server runs in this process
    (otherwise nothing would serve the webhook path). A misconfigured
    webhook mode falls back to polling with a loud ERROR so the bot keeps
    working instead of going silent.
    """
    mode = get_configured_bot_mode()
    if mode != BOT_MODE_WEBHOOK:
        return BOT_MODE_POLLING
    problems = webhook_config_problems()
    if not web_server_in_process:
        problems.append("the web server does not run in this process (RUN_MODE has no web component)")
    if problems:
        logger.error(
            "BOT_MODE=webhook requested but unusable (%s); falling back to polling.",
            "; ".join(problems),
        )
        return BOT_MODE_POLLING
    return BOT_MODE_WEBHOOK


# ---------------------------------------------------------------------------
# Runtime state: the shared Bot / Dispatcher, de-dup memory, tasks
# ---------------------------------------------------------------------------


class RecentUpdateIds:
    """Bounded memory of recently accepted update_ids (FIFO eviction).

    Used to skip Telegram retries of an update that was already accepted.
    Single event loop, no awaits between check and insert -> no lock.
    """

    def __init__(self, maxlen: int = RECENT_UPDATE_IDS_MAX) -> None:
        self.maxlen = maxlen
        self._ids: "OrderedDict[int, None]" = OrderedDict()

    def seen_or_add(self, update_id: int) -> bool:
        """Return True if update_id was already seen; otherwise remember it."""
        if update_id in self._ids:
            return True
        self._ids[update_id] = None
        while len(self._ids) > self.maxlen:
            self._ids.popitem(last=False)
        return False

    def clear(self) -> None:
        self._ids.clear()

    def __len__(self) -> int:
        return len(self._ids)


_runtime: dict[str, Any] = {"bot": None, "dispatcher": None}
recent_update_ids = RecentUpdateIds()
_background_tasks: set[asyncio.Task] = set()
_registration_task: Optional[asyncio.Task] = None


def attach_bot(bot: Any, dispatcher: Any) -> None:
    """Hand the process's Bot + Dispatcher to the webhook endpoint.

    Called by main.py ONLY when the effective mode is webhook. Until this
    happens the endpoint answers authenticated requests with 503, so
    Telegram retries later instead of the update being lost.
    """
    _runtime["bot"] = bot
    _runtime["dispatcher"] = dispatcher


def detach_bot() -> None:
    _runtime["bot"] = None
    _runtime["dispatcher"] = None


def get_attached() -> tuple[Any, Any]:
    return _runtime["bot"], _runtime["dispatcher"]


def is_webhook_active() -> bool:
    bot, dp = get_attached()
    return bot is not None and dp is not None


async def drain_background_tasks(timeout: float = 5.0) -> None:
    """Wait (bounded) for in-flight update tasks. Used by tests/shutdown."""
    pending = [t for t in _background_tasks if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout)


# ---------------------------------------------------------------------------
# Update processing
# ---------------------------------------------------------------------------


async def _process_update(bot: Any, dispatcher: Any, update: Any) -> None:
    """Feed one update into the existing Dispatcher.

    Mirrors aiogram's polling path (Dispatcher._process_update with
    call_answer=True): a handler that returns a TelegramMethod gets it
    executed, and exceptions are logged rather than propagated.
    """
    from aiogram.methods import TelegramMethod

    try:
        result = None
        if hasattr(dispatcher, "feed_webhook_update"):
            result = await dispatcher.feed_webhook_update(bot=bot, update=update)
        elif hasattr(dispatcher, "feed_update"):
            result = await dispatcher.feed_update(bot, update, bots=(bot,))
        if isinstance(result, TelegramMethod):
            await dispatcher.silent_call_request(bot=bot, result=result)
    except Exception:
        logger.exception(
            "Error while processing Telegram webhook update id=%s",
            getattr(update, "update_id", "?"),
        )


def _secret_matches(provided: Optional[str]) -> bool:
    expected = getattr(settings, "telegram_webhook_secret", "") or ""
    if not expected:
        # No secret configured -> nothing can authenticate. Never treat an
        # empty expected value as "auth disabled".
        return False
    return hmac.compare_digest(
        (provided or "").encode("utf-8"), expected.encode("utf-8")
    )


async def telegram_webhook_endpoint(request: Request) -> JSONResponse:
    """POST {BOT_WEBHOOK_PATH} - Telegram update intake.

    Order matters: authenticate first (403, body never read), then check
    the bot is attached (503 -> Telegram retries), then parse, de-dup and
    schedule processing in the background so Telegram gets its 200 fast.
    """
    if not _secret_matches(request.headers.get(SECRET_HEADER)):
        logger.warning(
            "Rejected Telegram webhook request: missing or invalid secret token header."
        )
        return JSONResponse({"ok": False}, status_code=403)

    bot, dispatcher = get_attached()
    if bot is None or dispatcher is None:
        logger.warning(
            "Telegram webhook request received but the bot is not attached "
            "(BOT_MODE is not webhook in this process); answering 503."
        )
        return JSONResponse({"ok": False, "error": "bot not ready"}, status_code=503)

    # Bounded body read (hunt #6): reject oversized payloads before they
    # are buffered whole into memory. The Content-Length fast path covers
    # honest clients; the streaming accumulation covers chunked bodies
    # that carry no length header.
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_UPDATE_BODY_BYTES:
                logger.warning(
                    "Rejected oversized Telegram webhook payload (Content-Length %s).",
                    content_length,
                )
                return JSONResponse(
                    {"ok": False, "error": "payload too large"}, status_code=413
                )
        except ValueError:
            pass
    body = bytearray()
    try:
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_UPDATE_BODY_BYTES:
                logger.warning("Rejected oversized Telegram webhook payload (streamed body).")
                return JSONResponse(
                    {"ok": False, "error": "payload too large"}, status_code=413
                )
    except Exception as exc:
        logger.error(
            "Discarding unreadable Telegram webhook body (%s).", type(exc).__name__
        )
        return JSONResponse({"ok": True, "ignored": "malformed"})

    from aiogram.types import Update

    try:
        payload = json.loads(bytes(body))
        update = Update.model_validate(payload, context={"bot": bot})
    except Exception as exc:
        # Authenticated but unparseable: re-delivery can never succeed, so
        # ack with 200 instead of letting Telegram retry it forever.
        logger.error(
            "Discarding malformed Telegram webhook payload (%s).", type(exc).__name__
        )
        return JSONResponse({"ok": True, "ignored": "malformed"})

    if recent_update_ids.seen_or_add(update.update_id):
        logger.info("Skipping duplicate Telegram update id=%s (retry).", update.update_id)
        return JSONResponse({"ok": True, "duplicate": True})

    task = asyncio.create_task(_process_update(bot, dispatcher, update))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    await asyncio.sleep(0)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# Webhook registration / polling rollback safety
# ---------------------------------------------------------------------------


async def register_webhook(
    bot: Any,
    dispatcher: Any,
    *,
    attempts: int = 3,
    retry_delay_seconds: float = 2.0,
) -> bool:
    """Idempotently register the webhook with Telegram (setWebhook).

    Safe on every boot and when two instances overlap during a deploy:
    both send identical parameters, setWebhook simply replaces the
    registration with itself, and drop_pending_updates=False never
    discards queued updates. allowed_updates matches what polling asks
    for (the update types the registered handlers actually use).
    """
    url = build_webhook_url()
    secret = getattr(settings, "telegram_webhook_secret", "") or ""
    try:
        allowed_updates = dispatcher.resolve_used_update_types()
    except Exception:
        allowed_updates = None  # Telegram keeps its previous setting

    for attempt in range(1, max(1, attempts) + 1):
        try:
            await bot.set_webhook(
                url=url,
                secret_token=secret,
                drop_pending_updates=False,
                allowed_updates=allowed_updates,
            )
        except Exception as exc:
            logger.warning(
                "setWebhook attempt %d/%d failed (%s: %s).",
                attempt,
                attempts,
                type(exc).__name__,
                exc,
            )
            if attempt < attempts:
                await asyncio.sleep(retry_delay_seconds * attempt)
            continue

        logger.info("Telegram webhook registered at %s", url)
        try:
            info = await bot.get_webhook_info()
            logger.info(
                "Webhook info: pending_update_count=%s last_error_message=%s",
                getattr(info, "pending_update_count", None),
                getattr(info, "last_error_message", None),
            )
        except Exception:
            pass
        return True

    logger.error(
        "Could not register the Telegram webhook after %d attempts; the bot will "
        "not receive updates until the next successful boot.",
        attempts,
    )
    return False


def schedule_webhook_registration() -> Optional[asyncio.Task]:
    """Called from the FastAPI lifespan. No-op unless webhook mode attached
    a bot in this process. Runs in the background so boot is not delayed."""
    global _registration_task
    bot, dispatcher = get_attached()
    if bot is None or dispatcher is None:
        return None
    _registration_task = asyncio.create_task(register_webhook(bot, dispatcher))
    return _registration_task


def cancel_webhook_registration() -> None:
    """Shutdown hook: stop a still-retrying registration. Deliberately does
    NOT call deleteWebhook - the next instance (or this one after restart)
    keeps receiving updates through the same registration."""
    global _registration_task
    if _registration_task is not None and not _registration_task.done():
        _registration_task.cancel()
    _registration_task = None


async def clear_webhook_for_polling(bot: Any) -> None:
    """Rollback safety for BOT_MODE=polling.

    Telegram refuses getUpdates while a webhook is registered, so after a
    flip back from webhook to polling the bot would silently receive
    nothing. If (and only if) a webhook URL is currently registered, it is
    removed with drop_pending_updates=False so queued updates are handed
    to polling instead. When no webhook is registered (today's state) the
    polling startup is unchanged apart from this one read-only call, and
    any failure here is logged and polling starts exactly as before.
    """
    try:
        info = await bot.get_webhook_info()
    except Exception as exc:
        logger.warning(
            "Could not read Telegram webhook info before polling (%s); "
            "starting polling as before.",
            type(exc).__name__,
        )
        return
    url = getattr(info, "url", "") or ""
    if not isinstance(url, str) or not url:
        return
    logger.warning(
        "BOT_MODE=polling but a Telegram webhook is registered; removing it "
        "(drop_pending_updates=False) so polling can receive updates."
    )
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception as exc:
        logger.error(
            "Failed to remove the Telegram webhook before polling (%s); polling "
            "will receive nothing until it is removed.",
            type(exc).__name__,
        )

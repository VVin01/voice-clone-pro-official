"""Centralized Telegram Bot factory, Local Bot API health probe, dynamic upload cap,
and bounded Telegram ``file_id`` cache.

Why this module exists:
1. Official ``api.telegram.org`` caps bot uploads at **50 MB**. When the deployment
   points ``TELEGRAM_BOT_API_URL`` at a self-hosted Telegram Bot API server
   (e.g. ``https://telegram-bot-api-kydu.onrender.com`` running
   ``aiogram/telegram-bot-api:latest``), Telegram lifts the upload cap to
   **2,000 MB (2 GB)**.
2. Startup health probe (``probe_local_bot_api``) verifies the Local Bot API
   endpoint is reachable and accepts the bot token. If unreachable or not yet
   logged out from official Telegram servers, the service logs an actionable
   warning and falls back safely to official ``api.telegram.org`` (50 MB cap)
   instead of crashing or hanging.
3. Bounded ``file_id`` cache prevents re-uploading identical local media files on
   repeated "Send to Telegram" taps or multi-admin payment-slip fan-out, saving
   significant server egress bandwidth.
"""

from __future__ import annotations

import logging
import os
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger(__name__)

STANDARD_TELEGRAM_UPLOAD_MB = 50
LOCAL_BOT_API_UPLOAD_MB = 2000
STANDARD_TELEGRAM_UPLOAD_BYTES = STANDARD_TELEGRAM_UPLOAD_MB * 1024 * 1024
LOCAL_BOT_API_UPLOAD_BYTES = LOCAL_BOT_API_UPLOAD_MB * 1024 * 1024

# Runtime probe state (keyed to the configured URL so env changes in tests or
# reloads take effect immediately without stale cached flags).
_PROBE_STATE: dict[str, Any] = {
    "url": "",
    "probed": False,
    "active": False,
    "last_error": "",
    "bot_username": "",
    "checked_at": 0.0,
}

# Bounded in-memory Telegram file_id cache keyed by
# (api_namespace, resolved_path, mtime_ns, size_bytes) -> file_id.
# api_namespace distinguishes official api.telegram.org vs local Bot API server
# because file_ids are NOT interchangeable across Bot API servers.
_FILE_ID_CACHE: OrderedDict[tuple[str, str, int, int], str] = OrderedDict()
_FILE_ID_CACHE_MAX = 256


def get_configured_local_bot_api_url() -> str:
    """Return normalized TELEGRAM_BOT_API_URL (empty string when unset)."""
    raw = str(getattr(settings, "telegram_bot_api_url", "") or "").strip()
    return raw.rstrip("/")


def reset_local_bot_api_probe_state() -> None:
    """Reset runtime probe state (mainly for unit tests and manual re-probes)."""
    _PROBE_STATE.update(
        {
            "url": "",
            "probed": False,
            "active": False,
            "last_error": "",
            "bot_username": "",
            "checked_at": 0.0,
        }
    )


def set_local_bot_api_probe_result(
    url: str,
    *,
    active: bool,
    last_error: str = "",
    bot_username: str = "",
) -> None:
    """Record the outcome of a Local Bot API health probe."""
    _PROBE_STATE.update(
        {
            "url": url.strip().rstrip("/"),
            "probed": True,
            "active": bool(active),
            "last_error": str(last_error or ""),
            "bot_username": str(bot_username or ""),
            "checked_at": time.time(),
        }
    )


def is_local_bot_api_active() -> bool:
    """Return True when a Local Telegram Bot API server is configured AND active.

    - If ``TELEGRAM_BOT_API_URL`` is empty -> False (official 50 MB cap).
    - If ``probe_local_bot_api()`` ran for this exact URL -> returns its health result
      (falls back to False / official 50 MB if the local server was unreachable or
      rejected the bot token).
    - If configured but not yet probed -> True (optimistic for configured servers
      and unit tests that toggle ``settings.telegram_bot_api_url`` directly).
    """
    url = get_configured_local_bot_api_url()
    if not url:
        return False
    if _PROBE_STATE.get("probed") and _PROBE_STATE.get("url") == url:
        return bool(_PROBE_STATE.get("active"))
    return True


def get_telegram_max_upload_mb() -> int:
    """Effective Telegram bot upload cap in MB.

    - Explicit ``TELEGRAM_MAX_UPLOAD_MB`` (> 0) overrides the auto cap.
    - Otherwise: 2000 MB when Local Bot API is active, 50 MB on official API.
    """
    explicit = int(getattr(settings, "telegram_max_upload_mb", 0) or 0)
    if explicit > 0:
        return explicit
    return LOCAL_BOT_API_UPLOAD_MB if is_local_bot_api_active() else STANDARD_TELEGRAM_UPLOAD_MB


def get_telegram_max_upload_bytes() -> int:
    """Effective Telegram bot upload cap in bytes (50 MB default, 2000 MB with Local Bot API)."""
    return get_telegram_max_upload_mb() * 1024 * 1024


def get_splitter_max_part_bytes() -> int:
    """Effective per-part size cap for video splitting.

    Stays 48 MB on standard Bot API (leaving 2 MB headroom under Telegram's
    50 MB ceiling) and scales up to ``(max_mb - 20) MB`` (1,980 MB) when
    Local Bot API is active so large recaps deliver whole without forced 48 MB splits.
    """
    max_mb = get_telegram_max_upload_mb()
    if max_mb <= STANDARD_TELEGRAM_UPLOAD_MB:
        return 48 * 1024 * 1024
    return max(48, max_mb - 20) * 1024 * 1024


def get_local_bot_api_status() -> dict[str, Any]:
    """Structured telemetry snapshot for /health and Admin Overview."""
    url = get_configured_local_bot_api_url()
    active = is_local_bot_api_active()
    max_mb = get_telegram_max_upload_mb()
    mode = "local_bot_api" if active else ("fallback_official" if url else "official")
    return {
        "configured": bool(url),
        "active": active,
        "mode": mode,
        "url": url if url else "https://api.telegram.org",
        "is_local_fs": bool(getattr(settings, "telegram_bot_api_is_local", False)),
        "max_upload_mb": max_mb,
        "max_upload_bytes": max_mb * 1024 * 1024,
        "probed": bool(_PROBE_STATE.get("probed") and _PROBE_STATE.get("url") == url),
        "bot_username": _PROBE_STATE.get("bot_username") if _PROBE_STATE.get("url") == url else "",
        "last_error": _PROBE_STATE.get("last_error") if _PROBE_STATE.get("url") == url else "",
    }


def _api_namespace() -> str:
    if is_local_bot_api_active():
        return f"local:{get_configured_local_bot_api_url()}"
    return "official"


def create_telegram_bot(
    token: Optional[str] = None,
    *,
    default: Any = None,
    timeout: Optional[int] = None,
    bot_cls: Any = None,
):
    """Create an Aiogram ``Bot`` wired to Local Bot API when active, else official API.

    Imports ``aiogram.Bot`` dynamically (or uses caller-supplied ``bot_cls``) so
    existing unit tests patching ``aiogram.Bot`` or ``main.Bot`` continue to
    intercept seamlessly, and avoids passing ``session=...`` when using official
    API so lightweight fake-Bot mocks stay 100% compatible.
    """
    if bot_cls is None:
        from aiogram import Bot as bot_cls

    tok = token if token is not None else getattr(settings, "bot_token", "")
    kwargs: dict[str, Any] = {"token": tok}
    if default is not None:
        kwargs["default"] = default

    if is_local_bot_api_active():
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.client.telegram import TelegramAPIServer

        base_url = get_configured_local_bot_api_url()
        is_local_fs = bool(getattr(settings, "telegram_bot_api_is_local", False))
        eff_timeout = int(
            timeout
            if timeout is not None
            else (getattr(settings, "telegram_bot_api_timeout_seconds", 300) or 300)
        )
        api_server = TelegramAPIServer.from_base(base_url, is_local=is_local_fs)
        kwargs["session"] = AiohttpSession(api=api_server, timeout=eff_timeout)

    return bot_cls(**kwargs)


async def probe_local_bot_api(
    token: Optional[str] = None,
    *,
    timeout_seconds: float = 8.0,
) -> dict[str, Any]:
    """Verify the configured Local Bot API server responds to ``getMe``.

    On success: activates Local Bot API mode (lifting upload limit to 2,000 MB).
    On failure: records the error, logs an actionable warning (including Telegram's
    one-time ``logOut`` migration requirement if relevant), and falls back safely
    to official ``api.telegram.org`` (50 MB cap) so bot & Mini App keep working.
    """
    url = get_configured_local_bot_api_url()
    if not url:
        reset_local_bot_api_probe_state()
        return get_local_bot_api_status()

    tok = (token if token is not None else getattr(settings, "bot_token", "")) or ""
    if not tok:
        set_local_bot_api_probe_result(
            url,
            active=False,
            last_error="TELEGRAM_BOT_TOKEN is not set",
        )
        logger.warning(
            "Local Bot API configured (%s) but TELEGRAM_BOT_TOKEN is empty; keeping official 50 MB fallback.",
            url,
        )
        return get_local_bot_api_status()

    try:
        from aiogram import Bot
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.client.telegram import TelegramAPIServer

        api_server = TelegramAPIServer.from_base(
            url,
            is_local=bool(getattr(settings, "telegram_bot_api_is_local", False)),
        )
        session = AiohttpSession(api=api_server, timeout=timeout_seconds)
        probe_bot = Bot(token=tok, session=session)
        try:
            me = await probe_bot.get_me()
            uname = getattr(me, "username", "") or ""
            set_local_bot_api_probe_result(
                url,
                active=True,
                last_error="",
                bot_username=uname,
            )
            logger.info(
                "Local Telegram Bot API ACTIVE at %s (@%s) — upload limit lifted to %d MB!",
                url,
                uname or "bot",
                get_telegram_max_upload_mb(),
            )
            return get_local_bot_api_status()
        finally:
            try:
                await probe_bot.session.close()
            except Exception:
                pass
    except Exception as exc:
        err_msg = str(exc)[:300]
        set_local_bot_api_probe_result(url, active=False, last_error=err_msg)
        hint = ""
        low = err_msg.lower()
        if any(k in low for k in ("logged", "unauthorized", "401", "conflict")):
            hint = (
                " (Hint: Telegram requires calling https://api.telegram.org/bot<TOKEN>/logOut "
                "ONCE before migrating a bot to a self-hosted Local Bot API server.)"
            )
        logger.warning(
            "Local Telegram Bot API probe FAILED for %s (%s)%s — falling back safely to official api.telegram.org (%d MB limit).",
            url,
            err_msg,
            hint,
            STANDARD_TELEGRAM_UPLOAD_MB,
        )
        return get_local_bot_api_status()


# ── Bounded Telegram file_id Cache (Saves Repeat Upload Egress) ─────────────
def _file_cache_key(file_path: str | Path) -> Optional[tuple[str, str, int, int]]:
    try:
        p = Path(file_path).resolve()
        st = os.stat(p)
        return (_api_namespace(), str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def get_cached_telegram_file_id(file_path: str | Path) -> Optional[str]:
    """Return cached Telegram ``file_id`` for an unchanged local file, if any."""
    key = _file_cache_key(file_path)
    if key is None:
        return None
    fid = _FILE_ID_CACHE.get(key)
    if fid:
        _FILE_ID_CACHE.move_to_end(key)
    return fid


def invalidate_cached_telegram_file_id(file_path: str | Path) -> None:
    """Remove any cached ``file_id`` for ``file_path`` (e.g. if Telegram rejects it)."""
    key = _file_cache_key(file_path)
    if key is not None:
        _FILE_ID_CACHE.pop(key, None)


def cache_telegram_file_id(
    file_path: str | Path,
    sent_message: Any,
    media_kind: str = "document",
) -> Optional[str]:
    """Extract and cache ``file_id`` from a Telegram Message returned by send_*."""
    if sent_message is None:
        return None
    fid: Optional[str] = None
    try:
        if media_kind == "photo":
            photos = getattr(sent_message, "photo", None)
            if isinstance(photos, (list, tuple)) and photos:
                fid = getattr(photos[-1], "file_id", None)
        elif media_kind == "video":
            vid = getattr(sent_message, "video", None)
            fid = getattr(vid, "file_id", None) if vid is not None else None
        elif media_kind == "audio":
            aud = getattr(sent_message, "audio", None)
            fid = getattr(aud, "file_id", None) if aud is not None else None
        else:
            doc = getattr(sent_message, "document", None)
            fid = getattr(doc, "file_id", None) if doc is not None else None
    except Exception:
        fid = None

    if not fid or not isinstance(fid, str):
        return None
    key = _file_cache_key(file_path)
    if key is None:
        return None
    _FILE_ID_CACHE[key] = fid
    _FILE_ID_CACHE.move_to_end(key)
    while len(_FILE_ID_CACHE) > _FILE_ID_CACHE_MAX:
        _FILE_ID_CACHE.popitem(last=False)
    return fid


def clear_telegram_file_id_cache() -> None:
    """Clear the in-memory file_id cache (used by tests)."""
    _FILE_ID_CACHE.clear()


async def send_cached_telegram_media(
    bot: Any,
    *,
    media_kind: str,
    chat_id: int,
    file_path: str | Path,
    filename: Optional[str] = None,
    **kwargs: Any,
) -> Any:
    """Send a local media file via ``bot``, reusing a cached Telegram ``file_id``
    when the same file has already been uploaded to the active Bot API server.

    Falls back automatically to a fresh ``FSInputFile`` upload if Telegram rejects
    the cached ``file_id``. Returns the sent Aiogram ``Message``.
    """
    from aiogram.types import FSInputFile

    method_map = {
        "video": ("send_video", "video"),
        "audio": ("send_audio", "audio"),
        "photo": ("send_photo", "photo"),
        "document": ("send_document", "document"),
    }
    method_name, arg_name = method_map.get(media_kind, ("send_document", "document"))
    sender = getattr(bot, method_name)

    cached_fid = get_cached_telegram_file_id(file_path)
    if cached_fid:
        try:
            return await sender(chat_id=chat_id, **{arg_name: cached_fid}, **kwargs)
        except Exception:
            invalidate_cached_telegram_file_id(file_path)

    upload = (
        FSInputFile(str(file_path), filename=filename)
        if filename
        else FSInputFile(str(file_path))
    )
    sent = await sender(chat_id=chat_id, **{arg_name: upload}, **kwargs)
    cache_telegram_file_id(file_path, sent, media_kind=media_kind)
    return sent

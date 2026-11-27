from __future__ import annotations

import json
import logging
import time
from typing import Iterable, Mapping
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.config import settings
from app.models.schema import AuditLog, BotSetting

logger = logging.getLogger(__name__)

# Short-TTL in-process cache for BotSetting reads on persistent DB binds.
# Keyed by (id(sync_engine), str(sync_engine.url)) -> {setting_key: (value_or_sentinel, expires_at)}.
# Automatically invalidated on set_dynamic_setting(), load_dynamic_settings(), and any
# SQLAlchemy Session flush touching a BotSetting row. Bypassed on :memory: SQLite binds
# so isolated unit tests always read live session state directly via a single batched query.
_DYNAMIC_SETTINGS_CACHE_TTL_SECONDS = 15.0
_CACHE_MISS_SENTINEL = object()
_DYNAMIC_SETTINGS_CACHE: dict[tuple[int, str], dict[str, tuple[object, float]]] = {}


def _bind_cache_key(session: AsyncSession) -> tuple[int, str] | None:
    try:
        bind = session.get_bind()
    except Exception:
        return None
    if bind is None:
        return None
    sync_engine = getattr(bind, "engine", bind)
    url_str = str(getattr(sync_engine, "url", "") or "")
    if ":memory:" in url_str:
        return None
    return (id(sync_engine), url_str)


def invalidate_dynamic_settings_cache(key: str | None = None) -> None:
    """Invalidate cached BotSetting entries (one key or all keys across binds)."""
    if key in (None, "gemini_tts_prompt_shape"):
        import sys as _sys

        _gtts_mod = _sys.modules.get("app.services.recap_engine.gemini_tts_engine")
        if _gtts_mod is not None and hasattr(_gtts_mod, "reset_prompt_shape_cache"):
            _gtts_mod.reset_prompt_shape_cache()
    if key is None:
        _DYNAMIC_SETTINGS_CACHE.clear()
        return
    for bucket in _DYNAMIC_SETTINGS_CACHE.values():
        bucket.pop(key, None)


@event.listens_for(Session, "after_flush")
def _invalidate_cache_on_bot_setting_flush(session: Session, flush_context) -> None:
    touched_keys: set[str] = set()
    for obj in (*session.new, *session.dirty, *session.deleted):
        if isinstance(obj, BotSetting) and getattr(obj, "key", None):
            touched_keys.add(str(obj.key))
    for k in touched_keys:
        invalidate_dynamic_settings_cache(k)


def _loggable_value(key: str, value) -> str:
    """A setting value as it may appear in logs.

    payment_* values are bank-account identifiers (KBZPay/Wave
    numbers and account names): the settings loader and updater used
    to spray them into the service logs on every boot/save (hunt L3).
    They are masked in the codebase's usual partial style; every
    other setting logs in full as before.
    """
    if key.startswith("payment_"):
        text = str(value)
        return f"{text[:3]}…{text[-2:]}" if len(text) > 6 else "•••"
    return str(value)


SETTING_TYPES: dict[str, type] = {
    "free_daily_allowance_seconds": int,
    "character_limit_free": int,
    "character_limit_purchased": int,
    "recap_free_max_duration_seconds": int,
    "recap_max_duration_seconds": int,
    "tts_daily_limit_minutes_free": int,
    "tts_daily_limit_minutes_paid": int,
    "free_gemini_taste_minutes": int,
    "paid_gemini_taste_minutes": int,
    "free_gemini_daily_budget_requests": int,
    "required_channel_id": str,
    "required_group_id": str,
    "channel_invite_link": str,
    "group_invite_link": str,
    "maintenance_mode": bool,
    "maintenance_note": str,
    "group_ai_admin_enabled": bool,
    "group_ai_user_cooldown_seconds": int,
    "group_ai_hourly_budget": int,
    "group_ai_daily_budget": int,
    "group_ai_moderation_enabled": bool,
    "group_ai_moderation_confidence_threshold": float,
    "group_ai_moderation_mute_duration_strike2_seconds": int,
    "group_ai_moderation_mute_duration_strike3_seconds": int,
    "group_ai_moderation_whitelist": str,
    "group_ai_voice_enabled": bool,
    "group_ai_voice_daily_cap": int,
    "group_ai_knowledge_sheet": str,
    "group_ai_knowledge_sheet_saved_at": str,
    "group_ai_knowledge_sheet_saved_by": str,
    "private_ai_enabled": bool,
    "private_ai_user_cooldown_seconds": int,
    "private_ai_hourly_budget": int,
    "private_ai_daily_budget": int,
    "tutorial_video_link": str,
    "tutorial_voice_title": str,
    "tutorial_voice_link": str,
    "tutorial_voice_enabled": bool,
    "tutorial_recap_link": str,
    "tutorial_recap_enabled": bool,
    "recap_demo_asset": str,
    "tutorial_index_link": str,
    "tutorial_plans_link": str,
    "tutorial_customvoice_link": str,
    "tutorial_downloader_link": str,
    "tutorial_scriptedit_link": str,
    "tutorial_recapadvanced_link": str,
    "tutorial_refer_link": str,
    "tutorial_support_link": str,
    "payment_kpay_number": str,
    "payment_kpay_name": str,
    "payment_wave_number": str,
    "payment_wave_name": str,
    "payment_accounts_json": str,
    "usd_to_mmk_rate": float,
    "gemini_primary_model": str,
    "gemini_fallback_1": str,
    "gemini_fallback_2": str,
    "gemini_fallback_3": str,
    "gemini_tts_primary_model": str,
    "gemini_tts_fallback_1": str,
    "gemini_tts_lite_model": str,
    "gemini_tts_fallback_2": str,
    "gemini_tts_leak_gate_mode": str,
    "gemini_tts_prompt_shape": str,
    "clone_prosody_phase3_enabled": bool,
    "chunk_balancing_v2_enabled": bool,
    "clone_session_slices_enabled": bool,
    "hf_daily_calibration_enabled": bool,
    "ai_prompt_profile": str,
    "voxcpm_timeout": int,
    "groq_whisper_model": str,
    "recap_on_queue": bool,
    "recap_stale_job_timeout_seconds": int,
    "recap_worker_concurrency": int,
    "worker_laptop_heartbeat": str,
    "worker_laptop_engine": str,
    "worker_laptop_status": str,
    "worker_laptop_host": str,
    "watermark_free_enabled": bool,
    "watermark_paid_enabled": bool,
    "watermark_style": str,
    "watermark_position": str,
    "watermark_opacity": int,
    "watermark_size": int,
}


DEPRECATED_GEMINI_SETTING_DEFAULTS: dict[str, str] = {
    "gemini_primary_model": "gemini-3.6-flash",
    "gemini_fallback_1": "gemini-3.5-flash",
    "gemini_fallback_2": "gemini-3.5-flash-lite",
    "gemini_fallback_3": "gemini-flash-lite-latest",
}

DEPRECATED_OR_SLOW_GEMINI_MODELS: set[str] = {
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
    "gemini-3.8-flash",
    "gemini-3.7-flash",
}


DEPRECATED_GEMINI_TTS_SETTING_DEFAULTS: dict[str, str] = {
    "gemini_tts_primary_model": "gemini-3.8-flash-tts",
    "gemini_tts_fallback_1": "",
    "gemini_tts_lite_model": "gemini-3.8-flash-lite-tts",
    "gemini_tts_fallback_2": "",
}

RETIRED_GEMINI_TTS_PREVIEW_MODELS: set[str] = {
    "gemini-3.1-flash-tts-preview",
    "gemini-2.5-flash-preview-tts",
}


_LAST_LOGGED_SETTINGS: dict[str, object] = {}

# Heartbeat rows are written every 60 s by secondary workers and read only by
# _get_laptop_worker_status(); excluding them from both the config allowlist
# and the watermark probe ensures periodic heartbeats never trigger full reloads.
HEARTBEAT_SETTING_KEYS: frozenset[str] = frozenset(
    {
        "worker_laptop_heartbeat",
        "worker_laptop_engine",
        "worker_laptop_host",
    }
)

# Keys writable through set_dynamic_setting (hunt C5): exactly the
# registered dynamic settings — every one is a real Settings
# attribute (verified), and secrets like bot_token / API keys are
# NOT in SETTING_TYPES, so no admin surface (least of all the
# callback-data-driven preset handler, whose key comes from the
# callback string itself) can write or overwrite them here.
DYNAMIC_SETTING_KEYS: frozenset[str] = frozenset(SETTING_TYPES)

# Strict 80-key config allowlist for load_dynamic_settings() (Design 2 A1):
# excludes state blobs (bn_key_health_v1, aj_hf:*, aj_kh:*, aj_gai:*,
# user_history_cleared_at:*) and the 3 heartbeat keys by construction.
CONFIG_SETTING_KEYS: frozenset[str] = DYNAMIC_SETTING_KEYS - HEARTBEAT_SETTING_KEYS

DEFAULT_WATERMARK_MIN_INTERVAL_SECONDS = 15.0
_WATERMARK_STATE: dict[tuple[int, str], tuple[float, tuple[str | None, int] | None]] = {}


def _watermark_bind_key(session: AsyncSession) -> tuple[int, str]:
    try:
        bind = session.get_bind()
    except Exception:
        bind = None
    if bind is None:
        return (0, "unbound")
    sync_engine = getattr(bind, "engine", bind)
    url_str = str(getattr(sync_engine, "url", "") or "")
    return (id(sync_engine), url_str)


def reset_dynamic_settings_watermark() -> None:
    """Clear per-process watermark check timestamps and tuples (used by tests)."""
    _WATERMARK_STATE.clear()


def _normalize_watermark_ts(val: object) -> str | None:
    if val is None:
        return None
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return str(val)


async def _probe_settings_watermark(session: AsyncSession) -> tuple[str | None, int]:
    stmt = (
        select(func.max(BotSetting.updated_at), func.count())
        .select_from(BotSetting)
        .where(
            ~BotSetting.key.like("aj_%"),
            BotSetting.key != "bn_key_health_v1",
            ~BotSetting.key.like("user_history_cleared_at:%"),
            BotSetting.key.notin_(HEARTBEAT_SETTING_KEYS),
        )
    )
    row = (await session.execute(stmt)).one()
    return (_normalize_watermark_ts(row[0]), int(row[1] or 0))


async def load_dynamic_settings(session: AsyncSession, *, quiet: bool = False) -> dict[str, str]:
    """Load persistent config settings (allowlisted) from database into in-memory settings."""
    stmt = select(BotSetting).where(BotSetting.key.in_(CONFIG_SETTING_KEYS))
    res = await session.execute(stmt)
    rows = res.scalars().all()
    loaded: dict[str, str] = {}
    for row in rows:
        key = row.key
        if key not in CONFIG_SETTING_KEYS:
            continue
        raw_val = row.value
        norm_val = str(raw_val or "").strip().lower()
        if key in DEPRECATED_GEMINI_SETTING_DEFAULTS and norm_val in DEPRECATED_OR_SLOW_GEMINI_MODELS:
            raw_val = DEPRECATED_GEMINI_SETTING_DEFAULTS[key]
        elif key in DEPRECATED_GEMINI_TTS_SETTING_DEFAULTS and norm_val in RETIRED_GEMINI_TTS_PREVIEW_MODELS:
            raw_val = DEPRECATED_GEMINI_TTS_SETTING_DEFAULTS[key]
        loaded[key] = raw_val
        if key in SETTING_TYPES:
            expected_type = SETTING_TYPES[key]
            try:
                if expected_type is bool:
                    parsed_val = raw_val.lower() in ("true", "1", "yes")
                elif expected_type is int:
                    parsed_val = int(raw_val)
                elif expected_type is float:
                    parsed_val = float(raw_val)
                else:
                    parsed_val = str(raw_val)
                if hasattr(settings, key):
                    setattr(settings, key, parsed_val)
                    prev = _LAST_LOGGED_SETTINGS.get(key, object())
                    if not quiet or prev != parsed_val:
                        logger.info("Loaded dynamic setting '%s' = %s", key, _loggable_value(key, parsed_val))
                        _LAST_LOGGED_SETTINGS[key] = parsed_val
            except Exception as e:
                logger.warning("Failed to cast dynamic setting '%s' with value '%s': %s", key, _loggable_value(key, raw_val), e)
    invalidate_dynamic_settings_cache()
    return loaded


async def refresh_dynamic_settings_if_changed(
    session: AsyncSession,
    min_interval_s: float = DEFAULT_WATERMARK_MIN_INTERVAL_SECONDS,
    *,
    quiet: bool = True,
) -> bool:
    """Reload allowlisted dynamic settings only when the DB watermark tuple changes.

    - Per-process time gate: skips SQL if checked within ``min_interval_s`` seconds.
    - First call on a process/bind always loads (watermark tuple starts empty).
    - Subsequent calls run a 1-row ``(MAX(updated_at), COUNT(*))`` watermark probe
      excluding non-config blobs and worker heartbeats, reloading and invalidating
      ``_DYNAMIC_SETTINGS_CACHE`` only when the tuple changed.
    Returns True if ``load_dynamic_settings()`` ran, False otherwise.
    """
    bind_key = _watermark_bind_key(session)
    now = time.monotonic()
    prior = _WATERMARK_STATE.get(bind_key)
    if prior is not None:
        last_checked_at, last_tuple = prior
        if last_tuple is not None and min_interval_s > 0.0 and (now - last_checked_at) < min_interval_s:
            return False
    else:
        last_tuple = None

    probe_tuple = await _probe_settings_watermark(session)
    if last_tuple is None or probe_tuple != last_tuple:
        await load_dynamic_settings(session, quiet=quiet)
        _WATERMARK_STATE[bind_key] = (now, probe_tuple)
        return True

    _WATERMARK_STATE[bind_key] = (now, last_tuple)
    return False


async def set_dynamic_setting(
    session: AsyncSession,
    key: str,
    value: str | int | float | bool,
    admin_id: int | None = None,
) -> None:
    """Save setting to database, apply to in-memory settings, and write an AuditLog entry."""
    if key not in DYNAMIC_SETTING_KEYS:
        raise ValueError(f"Not a writable dynamic setting: {key!r}")
    str_val = str(value).lower() if isinstance(value, bool) else str(value)
    old_val = getattr(settings, key, None)

    # 1. Persist in database FIRST (hunt #4 B2-settings): the old
    # order applied the value to the in-memory settings object before
    # the DB write, so a failed flush/commit left memory and DB
    # permanently diverged until the next restart.
    row = (await session.execute(select(BotSetting).where(BotSetting.key == key))).scalar_one_or_none()
    if row is not None:
        row.value = str_val
    else:
        session.add(BotSetting(key=key, value=str_val))

    # 2. Write AuditLog
    if admin_id is not None:
        session.add(
            AuditLog(
                admin_id=admin_id,
                action="settings.update",
                target_type="setting",
                target_id=key,
                details_json=json.dumps({"old": str(old_val), "new": str_val}),
            )
        )

    await session.flush()
    invalidate_dynamic_settings_cache(key)

    # 3. Only now — persistence succeeded — apply to in-memory settings.
    if key in SETTING_TYPES:
        expected_type = SETTING_TYPES[key]
        if expected_type is bool:
            parsed_val = str_val in ("true", "1", "yes")
        elif expected_type is int:
            parsed_val = int(str_val)
        elif expected_type is float:
            parsed_val = float(str_val)
        else:
            parsed_val = str_val
        if hasattr(settings, key):
            setattr(settings, key, parsed_val)
    elif hasattr(settings, key):
        setattr(settings, key, str_val)

    logger.info("Dynamic setting '%s' updated to '%s' by admin %s", key, _loggable_value(key, str_val), admin_id)


async def get_dynamic_setting(session: AsyncSession, key: str, default: str | None = None) -> str | None:
    """Retrieve setting value via the short-TTL in-process cache or fallback to settings."""
    batch = await get_dynamic_settings_batch(session, (key,), defaults={key: default})
    return batch.get(key, default)


async def get_dynamic_settings_batch(
    session: AsyncSession,
    keys: Iterable[str],
    defaults: Mapping[str, str | None] | None = None,
    *,
    use_settings_fallback: bool = True,
) -> dict[str, str | None]:
    """Fetch multiple BotSetting keys in ONE SQL query (with short-TTL in-process cache).

    Preserves exact ``get_dynamic_setting`` fallback semantics for every key:
    DB ``BotSetting.value`` wins if a row exists, otherwise ``getattr(settings, key, default)``
    (or ``defaults[key]`` directly when ``use_settings_fallback=False``).
    """
    key_list = list(dict.fromkeys(str(k) for k in keys))
    if not key_list:
        return {}

    fallback_map = defaults or {}
    cache_key = _bind_cache_key(session)
    now = time.monotonic()
    db_values: dict[str, object] = {}
    missing_keys: list[str] = []

    if cache_key is not None:
        bucket = _DYNAMIC_SETTINGS_CACHE.setdefault(cache_key, {})
        for k in key_list:
            entry = bucket.get(k)
            if entry is not None and entry[1] > now:
                db_values[k] = entry[0]
            else:
                missing_keys.append(k)
    else:
        missing_keys = key_list

    if missing_keys:
        stmt = select(BotSetting.key, BotSetting.value).where(BotSetting.key.in_(missing_keys))
        rows = (await session.execute(stmt)).all()
        found = {str(k): v for k, v in rows}
        exp = now + _DYNAMIC_SETTINGS_CACHE_TTL_SECONDS
        bucket = _DYNAMIC_SETTINGS_CACHE.setdefault(cache_key, {}) if cache_key is not None else None
        for k in missing_keys:
            val = found.get(k, _CACHE_MISS_SENTINEL)
            db_values[k] = val
            if bucket is not None:
                bucket[k] = (val, exp)

    result: dict[str, str | None] = {}
    for k in key_list:
        raw = db_values.get(k, _CACHE_MISS_SENTINEL)
        if raw is not _CACHE_MISS_SENTINEL and raw is not None:
            result[k] = raw
        elif use_settings_fallback:
            result[k] = getattr(settings, k, fallback_map.get(k))
        else:
            result[k] = fallback_map.get(k)
    return result


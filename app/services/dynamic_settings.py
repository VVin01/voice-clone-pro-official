from __future__ import annotations

import json
import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.schema import AuditLog, BotSetting

logger = logging.getLogger(__name__)


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
    "free_monthly_allowance_seconds": int,
    "character_limit_free": int,
    "character_limit_purchased": int,
    "recap_free_max_duration_seconds": int,
    "recap_max_duration_seconds": int,
    "free_daily_job_limit": int,
    "required_channel_id": str,
    "required_group_id": str,
    "channel_invite_link": str,
    "group_invite_link": str,
    "maintenance_mode": bool,
    "tutorial_video_link": str,
    "tutorial_video_enabled": bool,
    "tutorial_voice_title": str,
    "tutorial_voice_link": str,
    "tutorial_voice_enabled": bool,
    "tutorial_recap_title": str,
    "tutorial_recap_link": str,
    "tutorial_recap_enabled": bool,
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
    "gemini_primary_model": str,
    "gemini_fallback_1": str,
    "gemini_fallback_2": str,
    "gemini_fallback_3": str,
    "voxcpm_timeout": int,
    "groq_whisper_model": str,
    "worker_laptop_heartbeat": str,
    "worker_laptop_engine": str,
    "worker_laptop_status": str,
    "worker_laptop_host": str,
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


async def load_dynamic_settings(session: AsyncSession) -> dict[str, str]:
    """Load persistent settings from database into in-memory settings configuration."""
    stmt = select(BotSetting)
    res = await session.execute(stmt)
    rows = res.scalars().all()
    loaded = {}
    for row in rows:
        key = row.key
        raw_val = row.value
        if key in DEPRECATED_GEMINI_SETTING_DEFAULTS and str(raw_val or "").strip().lower() in DEPRECATED_OR_SLOW_GEMINI_MODELS:
            raw_val = DEPRECATED_GEMINI_SETTING_DEFAULTS[key]
        loaded[key] = raw_val
        if key in SETTING_TYPES:
            expected_type = SETTING_TYPES[key]
            try:
                if expected_type is bool:
                    parsed_val = raw_val.lower() in ("true", "1", "yes")
                elif expected_type is int:
                    parsed_val = int(raw_val)
                else:
                    parsed_val = str(raw_val)
                if hasattr(settings, key):
                    setattr(settings, key, parsed_val)
                    logger.info("Loaded dynamic setting '%s' = %s", key, _loggable_value(key, parsed_val))
            except Exception as e:
                logger.warning("Failed to cast dynamic setting '%s' with value '%s': %s", key, _loggable_value(key, raw_val), e)
    return loaded


# Keys writable through set_dynamic_setting (hunt C5): exactly the
# registered dynamic settings — every one is a real Settings
# attribute (verified), and secrets like bot_token / API keys are
# NOT in SETTING_TYPES, so no admin surface (least of all the
# callback-data-driven preset handler, whose key comes from the
# callback string itself) can write or overwrite them here.
DYNAMIC_SETTING_KEYS = frozenset(SETTING_TYPES)


async def set_dynamic_setting(
    session: AsyncSession,
    key: str,
    value: str | int | bool,
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

    # 3. Only now — persistence succeeded — apply to in-memory settings.
    if key in SETTING_TYPES:
        expected_type = SETTING_TYPES[key]
        if expected_type is bool:
            parsed_val = str_val in ("true", "1", "yes")
        elif expected_type is int:
            parsed_val = int(str_val)
        else:
            parsed_val = str_val
        if hasattr(settings, key):
            setattr(settings, key, parsed_val)
    elif hasattr(settings, key):
        setattr(settings, key, str_val)

    logger.info("Dynamic setting '%s' updated to '%s' by admin %s", key, _loggable_value(key, str_val), admin_id)


async def get_dynamic_setting(session: AsyncSession, key: str, default: str | None = None) -> str | None:
    """Retrieve setting value directly from database or fallback to in-memory setting."""
    row = (await session.execute(select(BotSetting).where(BotSetting.key == key))).scalar_one_or_none()
    if row is not None:
        return row.value
    return getattr(settings, key, default)


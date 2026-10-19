from __future__ import annotations

import logging
from datetime import datetime, timezone
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schema import BotSetting, Job, MinuteLedgerEvent, VoiceModel, utc_now

logger = logging.getLogger(__name__)


def _normalize_dt(dt: datetime | None) -> datetime:
    """Ensure consistent timezone-aware UTC datetime for safe comparisons."""
    if dt is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def get_user_history_cleared_at(session: AsyncSession, user_id: int) -> datetime | None:
    """Return the timestamp when the user last cleared their history, if any."""
    key = f"user_history_cleared_at:{user_id}"
    row = (await session.execute(select(BotSetting).where(BotSetting.key == key))).scalar_one_or_none()
    if row is not None and row.value:
        try:
            return datetime.fromisoformat(row.value)
        except Exception as e:
            logger.warning("Failed to parse history_cleared_at for user %s: %s", user_id, e)
            return None
    return None


async def clear_user_history(session: AsyncSession, user_id: int) -> datetime:
    """Atomically record the history clearance timestamp for the user."""
    key = f"user_history_cleared_at:{user_id}"
    now_dt = utc_now()
    row = (await session.execute(select(BotSetting).where(BotSetting.key == key))).scalar_one_or_none()
    if row is not None:
        row.value = now_dt.isoformat()
    else:
        session.add(BotSetting(key=key, value=now_dt.isoformat()))
    await session.commit()
    logger.info("User %s cleared their personal history display at %s", user_id, now_dt.isoformat())
    return now_dt


async def get_user_history_jobs(
    session: AsyncSession,
    user_id: int,
    limit: int = 15,
) -> list[Job]:
    """Fetch recent voice clone jobs for user, excluding records prior to clearance timestamp."""
    cleared_at = await get_user_history_cleared_at(session, user_id)
    stmt = select(Job).where(Job.user_id == user_id).order_by(Job.created_at.desc()).limit(limit * 2)
    jobs = list((await session.execute(stmt)).scalars().all())
    if cleared_at is not None:
        cl_utc = _normalize_dt(cleared_at)
        jobs = [j for j in jobs if _normalize_dt(j.created_at) > cl_utc]
    return jobs[:limit]


async def get_user_history_ledger_events(
    session: AsyncSession,
    user_id: int,
    limit: int = 15,
) -> list[MinuteLedgerEvent]:
    """Fetch recent minute ledger events for user, excluding records prior to clearance timestamp."""
    cleared_at = await get_user_history_cleared_at(session, user_id)
    stmt = select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == user_id).order_by(
        MinuteLedgerEvent.created_at.desc(), MinuteLedgerEvent.id.desc()
    ).limit(limit * 2)
    events = list((await session.execute(stmt)).scalars().all())
    if cleared_at is not None:
        cl_utc = _normalize_dt(cleared_at)
        events = [ev for ev in events if _normalize_dt(ev.created_at) > cl_utc]
    return events[:limit]


async def get_models_map_for_jobs(session: AsyncSession, jobs: list[Job]) -> dict[int, VoiceModel]:
    """Bulk fetch VoiceModel objects referenced in jobs list."""
    model_ids = {j.model_id for j in jobs if j.model_id}
    if not model_ids:
        return {}
    res = (await session.execute(select(VoiceModel).where(VoiceModel.id.in_(model_ids)))).scalars().all()
    return {m.id: m for m in res}

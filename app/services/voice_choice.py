"""Batch DP: park-and-ask shared machinery.

When a PAID job (recap or voice clone) hits VoxCPM ZeroGPU quota
exhaustion it PARKS instead of silently degrading to the Edge voice:
the job row moves to WAITING_CHOICE with its charge held (no refund,
no failure), the owner gets a bot prompt with inline choice buttons,
and this module owns everything around that state:

* the choice-state JSON on ``jobs.voice_choice_state`` — the DB
  column is the ONE source of truth (the recap checkpoint carries a
  secondary in-job mode mark; the in-memory registry is display-only);
* the prompt keyboard/text (all copy via i18n, my + en);
* owner-only choice application (``apply_user_choice``);
* the periodic resumer sweep (``sweep_parked_jobs``), started from
  the webapp lifespan: edge choices resume immediately, wait/no-choice
  jobs resume when a capacity probe sees a non-cooling pool token,
  and a job still parked after 48 h with no capacity fails through
  the EXISTING failure paths (refund + failure DM), so a dead pool
  can never strand a paid job forever.

Quota-return phrasing: Hugging Face documents ZeroGPU quota as a
rolling 24-hour window per account (no single global reset time),
so the prompt's return time is phrased as an ESTIMATE in both
locales — the next 06:30 Asia/Rangoon is used as the estimate value,
and the resumer itself never trusts the clock: it resumes on the
capacity probe, not on a schedule.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import select, update

from app.config import settings
from app.models.enums import JobStatus
from app.models.schema import Job
from app.services.i18n import t

logger = logging.getLogger(__name__)

WAITING = JobStatus.WAITING_CHOICE.value

CHOICE_WAIT = "wait"
CHOICE_EDGE_ALL = "edge_all"
CHOICE_EDGE_MIX = "edge_mix"
EDGE_CHOICES = (CHOICE_EDGE_ALL, CHOICE_EDGE_MIX)
ALL_CHOICES = (CHOICE_WAIT, CHOICE_EDGE_ALL, CHOICE_EDGE_MIX)

KIND_RECAP = "recap"
KIND_CLONE = "clone"

# Safety valve: a parked job whose pool never recovers fails (with
# the existing refund) after this long instead of parking forever.
PARK_SAFETY_VALVE_SECONDS = 48 * 3600
# Resumer cadence (the lifespan task sleeps this between sweeps).
RESUMER_INTERVAL_SECONDS = 600


# ── Choice state (jobs.voice_choice_state JSON) ─────────────────────

def parse_choice_state(raw: Any) -> Optional[dict]:
    """Parse the persisted choice-state JSON; None when absent/bad."""
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return None
    if not isinstance(data, dict) or "kind" not in data:
        return None
    return data


def dump_choice_state(state: dict) -> str:
    return json.dumps(state, ensure_ascii=False)


def new_choice_state(kind: str) -> dict:
    """A fresh park record: no choice made yet, prompt not sent yet."""
    return {
        "v": 1,
        "kind": kind,
        "choice": None,
        "parked_at": time.time(),
        "prompt_chat_id": None,
        "prompt_message_id": None,
    }


def choice_mode(state: Optional[dict]) -> Optional[str]:
    """The resume mode a choice-state dictates: "edge_all" /
    "edge_mix" once the owner picked an Edge route, else None (a
    wait/no-choice resume re-runs the normal clone-voice path)."""
    if not state:
        return None
    choice = state.get("choice")
    return choice if choice in EDGE_CHOICES else None


# ── Prompt copy / keyboard ──────────────────────────────────────────

def quota_reset_estimate_label() -> str:
    """The estimate value for the prompt's return-time placeholder:
    the next 06:30 Asia/Yangon, formatted HH:MM. An ESTIMATE only —
    ZeroGPU quota is a rolling per-account window (see module
    docstring); the copy in both locales says so."""
    try:
        tz = ZoneInfo("Asia/Yangon")
    except Exception:  # pragma: no cover - tzdata-less environment
        from datetime import timezone as _tz

        tz = _tz(timedelta(hours=6, minutes=30))
    now = datetime.now(tz)
    nxt = now.replace(hour=6, minute=30, second=0, microsecond=0)
    if nxt <= now:
        nxt = nxt + timedelta(days=1)
    return nxt.strftime("%H:%M")


def build_choice_keyboard(job_id: str, kind: str, lang: str):
    """Inline buttons for the park prompt. Recap gets all three
    choices; clone gets wait / Edge-only (per-part substitution is
    forbidden for clone jobs — Batch CH's whole-text rule)."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = [
        [InlineKeyboardButton(
            text=t("dp_btn_wait", lang),
            callback_data=f"dp:wait:{job_id}",
        )],
        [InlineKeyboardButton(
            text=t("dp_btn_edge_all", lang),
            callback_data=f"dp:edgeall:{job_id}",
        )],
    ]
    if kind == KIND_RECAP:
        rows.append([InlineKeyboardButton(
            text=t("dp_btn_edge_mix", lang),
            callback_data=f"dp:edgemix:{job_id}",
        )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def choice_prompt_text(kind: str, lang: str) -> str:
    key = "dp_park_prompt_recap" if kind == KIND_RECAP else "dp_park_prompt_clone"
    return t(key, lang, reset_time=quota_reset_estimate_label())


async def send_choice_prompt(bot, *, chat_id: int, job_id: str, kind: str, lang: str) -> Optional[int]:
    """Send the park prompt; returns the sent message id (or None).
    The caller persists the id into the choice-state."""
    if bot is None or not chat_id:
        return None
    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=choice_prompt_text(kind, lang),
            reply_markup=build_choice_keyboard(job_id, kind, lang),
        )
        return getattr(msg, "message_id", None)
    except Exception:
        logger.warning("DP: could not send park prompt for job %s", job_id, exc_info=True)
        return None


async def deliver_choice_prompt(bot, *, chat_id: int, message_id: Optional[int], job_id: str, kind: str, lang: str) -> Optional[int]:
    """First-park delivery (spec Item 3 step 4): edit the job's
    existing status message when one exists (clone jobs carry a
    standing status card), else send a new message (recap jobs have
    no standing bot message). Returns the prompt message id."""
    if bot is None or not chat_id:
        return None
    if message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=choice_prompt_text(kind, lang),
                reply_markup=build_choice_keyboard(job_id, kind, lang),
            )
            return message_id
        except Exception:
            pass  # fall through to a fresh message
    return await send_choice_prompt(
        bot, chat_id=chat_id, job_id=job_id, kind=kind, lang=lang
    )


async def refresh_choice_prompt(bot, *, chat_id: Optional[int], message_id: Optional[int], job_id: str, kind: str, lang: str) -> None:
    """Re-park: update the EXISTING prompt in place (fresh estimate
    wording, same live buttons) — never a new message, no spam."""
    if bot is None or not chat_id or not message_id:
        return
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=choice_prompt_text(kind, lang),
            reply_markup=build_choice_keyboard(job_id, kind, lang),
        )
    except Exception:
        pass


async def clear_choice_buttons(bot, *, chat_id: Optional[int], message_id: Optional[int]) -> None:
    """Drop the inline buttons from a park prompt (job completed or
    failed after resuming). Best-effort: a stale prompt whose job
    finished keeps harmless dead buttons otherwise — the callback
    handler refuses non-parked jobs."""
    if bot is None or not chat_id or not message_id:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except Exception:
        pass


async def clear_prompt_for_job(bot, job: Job) -> None:
    state = parse_choice_state(getattr(job, "voice_choice_state", None))
    if not state:
        return
    await clear_choice_buttons(
        bot,
        chat_id=state.get("prompt_chat_id") or job.chat_id or job.user_id,
        message_id=state.get("prompt_message_id"),
    )


# ── Capacity probe ──────────────────────────────────────────────────

def voxcpm_capacity_available() -> bool:
    """The resumer's capacity signal: at least one configured caller
    token is plausibly able to serve — not quota-dead (not in
    cooldown) and, when its balance is known, holding at least the
    last-known admission price. Unknown balances count as plausible
    (the token has never refused; after a process restart the learned
    table is empty while the persisted cooldowns still exclude the
    known-dead). Reads the engine's class-level state, restoring the
    persisted cooldown snapshot once, exactly like a synthesis call
    does. An anonymous-only configuration has nothing to wait for."""
    try:
        from app.services.voxcpm_engine import VoxCPMEngine, get_available_hf_tokens

        tokens = [tok for tok in get_available_hf_tokens() if tok]
        if not tokens:
            return True
        VoxCPMEngine._ensure_cooldowns_restored(tokens)
        now = time.time()
        price = VoxCPMEngine._last_admission_price
        for tok in tokens:
            if VoxCPMEngine._token_cooldowns.get(tok, 0) > now:
                continue
            bal = VoxCPMEngine._token_balances.get(str(tok))
            if bal is None or price is None or bal >= price:
                return True
        return False
    except Exception:
        logger.warning("DP: capacity probe failed; staying parked", exc_info=True)
        return False


# ── Choice application (bot callbacks) ──────────────────────────────

async def apply_user_choice(session, job: Optional[Job], choice: str, actor_id: int) -> str:
    """Record the owner's tap and, for an Edge choice, re-queue the
    job atomically. Returns an outcome code the handler maps to a
    localised callback answer:

    ``ok_wait`` / ``ok_resume`` / ``not_owner`` / ``not_parked`` /
    ``bad_choice`` / ``not_found``.
    """
    if job is None:
        return "not_found"
    if choice not in ALL_CHOICES:
        return "bad_choice"
    if job.user_id != actor_id and not settings.is_admin(actor_id):
        return "not_owner"
    state = parse_choice_state(getattr(job, "voice_choice_state", None))
    if job.status != WAITING or state is None:
        return "not_parked"
    if choice == CHOICE_EDGE_MIX and state.get("kind") != KIND_RECAP:
        return "bad_choice"

    state["choice"] = choice
    state["choice_at"] = time.time()
    if choice == CHOICE_WAIT:
        # Silence also means wait; an explicit wait tap just records
        # the preference. The job stays parked; buttons stay live.
        job.voice_choice_state = dump_choice_state(state)
        await session.commit()
        return "ok_wait"

    # Edge choice: resume NOW. The status flip is a guarded update —
    # exactly one resumer (this tap, or the periodic sweep) can win
    # it, so the job can never be dispatched twice.
    res = await session.execute(
        update(Job)
        .where(Job.id == job.id, Job.status == WAITING)
        .values(status=JobStatus.QUEUED.value, voice_choice_state=dump_choice_state(state))
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if res.rowcount != 1:
        return "not_parked"
    # NOTE: the recap registry entry is deliberately left at
    # WAITING_CHOICE — the queue worker skips jobs whose live entry
    # is RUNNING or QUEUED (already-dispatched guard) and proceeds
    # for any other status, claiming via the DB flip below its guard.
    return "ok_resume"


# ── Resumer sweep ───────────────────────────────────────────────────

async def _flip_to_queued(session, job: Job, state: dict) -> bool:
    """Atomically move one parked job back to QUEUED (rowcount==1
    wins; a racing callback/sweep that already flipped it makes this
    a no-op). Stamps the resume on the choice-state, which stays on
    the row: the recap re-dispatch reads the owner's mode from it."""
    state["resumed_at"] = time.time()
    res = await session.execute(
        update(Job)
        .where(Job.id == job.id, Job.status == WAITING)
        .values(status=JobStatus.QUEUED.value, voice_choice_state=dump_choice_state(state))
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return res.rowcount == 1


async def _expire_parked_job(session, job: Job, state: dict, bot) -> None:
    """48 h safety valve: fail a never-recovered parked job through
    the EXISTING failure paths, so the held charge is refunded by the
    same code that refunds any failed job — never a DP-specific
    money path."""
    from app.models.schema import User, utc_now

    user = await session.get(User, job.user_id)
    lang = getattr(user, "language_code", None) or "my"
    reason = t("dp_park_expired_error", lang)
    if state.get("kind") == KIND_RECAP:
        from app.webapp import api as _api

        res = await session.execute(
            update(Job)
            .where(Job.id == job.id, Job.status == WAITING)
            .values(status=JobStatus.FAILED.value, error_message=reason, finished_at=utc_now())
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        if res.rowcount != 1:
            return
        plan = _api._recap_redispatch_plan(job, user, restore_credit_state=True)
        entry = plan["entry"]
        entry["status"] = "FAILED"
        entry["error_message"] = reason
        _api.RECAP_JOBS_REGISTRY[job.id] = entry
        if entry.get("credit_deducted") and not entry.get("credit_refunded"):
            await _api._refund_recap_job_charge(job.user_id, entry)
        try:
            await _api._send_recap_failure_dm(
                job_id=job.id,
                chat_id=job.chat_id or job.user_id,
                title=entry.get("title") or job.input_text or "",
                reason=reason,
                credits_refunded=(
                    int(entry.get("credits_refunded") or 0)
                    if entry.get("credit_refunded")
                    else 0
                ),
                entry=entry,
            )
        except Exception:
            logger.warning("DP: expiry failure DM failed for recap job %s", job.id, exc_info=True)
        try:
            _api._strip_failed_recap_job_media(
                settings.storage_root / "recap_output" / job.id
            )
        except Exception:
            pass
        return

    # Clone: mirror the worker's failure path (curated stored error,
    # net-aware seconds refund, status-card edit).
    res = await session.execute(
        update(Job)
        .where(Job.id == job.id, Job.status == WAITING)
        .values(status=JobStatus.FAILED.value, error_message=reason, finished_at=utc_now())
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        await session.rollback()
        return
    if not settings.is_admin(job.user_id):
        from app.services.ledger import refund_job_seconds

        await refund_job_seconds(session, job.user_id, str(job.id))
    await session.commit()
    if bot is not None and job.status_message_id:
        try:
            from app.worker import safe_edit_status

            await safe_edit_status(
                bot,
                job.chat_id or job.user_id,
                job.status_message_id,
                f"{t('status_failed', lang)}\n<i>({reason})</i>",
            )
        except Exception:
            pass


async def _sweep_one(session, job: Job, bot, counts: dict, capacity: Optional[bool]) -> Optional[bool]:
    """Handle one parked row. Returns the capacity-probe value (the
    probe runs at most once per sweep, lazily)."""
    state = parse_choice_state(getattr(job, "voice_choice_state", None))
    if state is None:
        counts["still_parked"] += 1
        return capacity
    choice = state.get("choice")
    if choice in EDGE_CHOICES:
        if await _flip_to_queued(session, job, state):
            counts["resumed"] += 1
        return capacity
    if capacity is None:
        capacity = voxcpm_capacity_available()
    if capacity:
        if await _flip_to_queued(session, job, state):
            counts["resumed"] += 1
        return capacity
    parked_at = float(state.get("parked_at") or 0)
    if parked_at and (time.time() - parked_at) > PARK_SAFETY_VALVE_SECONDS:
        await _expire_parked_job(session, job, state, bot)
        counts["expired"] += 1
        return capacity
    counts["still_parked"] += 1
    return capacity


async def sweep_parked_jobs(bot=None) -> dict:
    """One resumer pass over every WAITING_CHOICE job. Edge choices
    resume immediately (normally already flipped by the callback —
    this is the backstop), wait/no-choice jobs resume when the
    capacity probe sees room, and a job parked past the 48 h valve
    with no capacity fails through the existing failure paths."""
    from app.db.session import AsyncSessionLocal

    counts = {"resumed": 0, "expired": 0, "still_parked": 0}
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(select(Job).where(Job.status == WAITING))
        ).scalars().all()
        capacity: Optional[bool] = None
        for job in rows:
            try:
                capacity = await _sweep_one(session, job, bot, counts, capacity)
            except Exception:
                # One poisoned row must never block the rest of the
                # sweep (or every later pass): leave it parked.
                try:
                    await session.rollback()
                except Exception:
                    pass
                logger.warning(
                    "DP resumer: sweep failed for parked job %s; leaving it parked",
                    getattr(job, "id", "?"), exc_info=True,
                )
                counts["still_parked"] += 1
    if counts["resumed"] or counts["expired"]:
        logger.info("DP resumer sweep: %s", counts)
    return counts

"""Park-and-ask shared machinery for Paid VoxCPM jobs (HF Pool Phase 1, Part 3).

When a PAID job (recap or standalone voice clone) hits VoxCPM ZeroGPU quota or
congestion exhaustion, it PARKS (`WAITING_CHOICE`) with its reservation held:
- `jobs.voice_choice_state` JSON stores `{completed_units, total_units,
  needed_gpu_s, pinned_space, worker_host, ...}` — zero DB schema migration.
- Choices:
  * `wait` (`dp:wait:<job_id>`) — default if untouched; auto-resumes when the
    shared `aj_hf:pool_state` ledger has `effective_usable_gpu_s >= needed_gpu_s * 1.25`.
  * `edge_all` (`dp:edgeall:<job_id>`, Recap only) — re-renders 100% uniformly in
    Gemini / Studio voice and refunds the held Voice Clone minutes.
  * `cancel` (`dp:cancel:<job_id>`) — flips `WAITING_CHOICE -> CANCELLED` and
    releases/refunds 100% immediately, freeing the user's concurrency slot.
  * Legacy `edge_mix` is retired (Part 2 #2): any persisted `edge_mix` degrades
    to the `Wait` default and `dp:edgemix` is rejected.
- Safety valve: 24 hours (`PARK_SAFETY_VALVE_SECONDS = 24 * 3600`). Unrecovered
  jobs at 24 h auto-expire to `FAILED` + 100% auto-refund + expiry DM.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import socket
import time
from typing import Any, Optional

from sqlalchemy import select, update

from app.config import settings
from app.models.enums import JobStatus
from app.models.schema import Job
from app.services import hf_pool_ledger as _hf_ledger
from app.services.i18n import t

logger = logging.getLogger(__name__)

WAITING = JobStatus.WAITING_CHOICE.value

CHOICE_WAIT = "wait"
CHOICE_EDGE_ALL = "edge_all"
CHOICE_CANCEL = "cancel"
EDGE_CHOICES = (CHOICE_EDGE_ALL,)
ALL_CHOICES = (CHOICE_WAIT, CHOICE_EDGE_ALL, CHOICE_CANCEL)

KIND_RECAP = "recap"
KIND_CLONE = "clone"

# Part 3 (#5): 24 h safety valve (matching ZeroGPU's 24 h rolling window).
PARK_SAFETY_VALVE_SECONDS = 24 * 3600
RESUMER_INTERVAL_SECONDS = 600
WORKER_AFFINITY_SECONDS = 15 * 60  # 15 minutes checkpoint host preference


def current_worker_host() -> str:
    """Stable identifier for the worker host holding local checkpoints."""
    return (
        os.getenv("VPS_PUBLIC_HOST", "").strip()
        or os.getenv("RENDER_INSTANCE_ID", "").strip()
        or socket.gethostname()
        or "primary"
    )


# ── Choice state (jobs.voice_choice_state JSON) ─────────────────────

def parse_choice_state(raw: Any) -> Optional[dict]:
    """Parse the persisted choice-state JSON; None when absent/bad.
    Degrades any retired `edge_mix` choice to None (Wait default — Part 2 #2)."""
    if not raw:
        return None
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception:
        return None
    if not isinstance(data, dict) or "kind" not in data:
        return None
    if data.get("choice") == "edge_mix":
        data = dict(data)
        data["choice"] = CHOICE_WAIT
    return data


def dump_choice_state(state: dict) -> str:
    return json.dumps(state, ensure_ascii=False)


def new_choice_state(
    kind: str,
    *,
    completed_units: int = 0,
    total_units: int = 1,
    needed_gpu_s: float = 0.0,
    pinned_space: Optional[str] = None,
    worker_host: Optional[str] = None,
) -> dict:
    """Create a park record carrying progress, GPU budget, pinned space, and host."""
    return {
        "v": 2,
        "kind": kind,
        "choice": None,
        "parked_at": time.time(),
        "prompt_chat_id": None,
        "prompt_message_id": None,
        "completed_units": max(0, int(completed_units or 0)),
        "total_units": max(1, int(total_units or 1)),
        "needed_gpu_s": round(max(0.0, float(needed_gpu_s or 0.0)), 2),
        "pinned_space": str(pinned_space) if pinned_space else None,
        "worker_host": str(worker_host) if worker_host else current_worker_host(),
    }


def choice_mode(state: Optional[dict]) -> Optional[str]:
    """Resume mode dictated by `state`: `"edge_all"` when explicitly chosen on a
    Recap job, else `None` (Wait / Clone path — including degraded `edge_mix`)."""
    if not state:
        return None
    choice = state.get("choice")
    if choice == CHOICE_EDGE_ALL and state.get("kind") == KIND_RECAP:
        return CHOICE_EDGE_ALL
    return None


def is_host_affinity_blocked(raw_state: Any, candidate_host: Optional[str] = None) -> bool:
    """Part 3 (#3): True when another worker host holds a fresh (<15 min) checkpoint."""
    state = parse_choice_state(raw_state)
    if not state:
        return False
    owner_host = str(state.get("worker_host") or "").strip()
    if not owner_host:
        return False
    here = str(candidate_host or current_worker_host()).strip()
    if not here or owner_host == here:
        return False
    ref_ts = float(state.get("resumed_at") or state.get("parked_at") or 0.0)
    if ref_ts <= 0:
        return False
    return (time.time() - ref_ts) < WORKER_AFFINITY_SECONDS


# ── Prompt copy / keyboard ──────────────────────────────────────────

def quota_reset_estimate_label(needed_gpu_s: float = 0.0) -> str:
    """Part 3 (#6): ETA computed from `aj_hf:pool_state`'s refill slope (`resets_at`)."""
    return _hf_ledger.estimate_refill_eta_label(needed_gpu_s=float(needed_gpu_s or 0.0))


def build_choice_keyboard(job_id: str, kind: str, lang: str):
    """Inline buttons for the park prompt (Part 3 #7):
    - Wait & Auto-Resume (`dp:wait:<job_id>`)
    - Recap only: Redo Whole Video in Gemini / Studio (`dp:edgeall:<job_id>`)
    - Cancel & Instant Full Refund (`dp:cancel:<job_id>`)
    (`dp:edgemix` is permanently retired — Part 2 #2.)
    """
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    rows = [
        [InlineKeyboardButton(
            text=t("dp_btn_wait", lang),
            callback_data=f"dp:wait:{job_id}",
        )],
    ]
    if kind == KIND_RECAP:
        rows.append([InlineKeyboardButton(
            text=t("dp_btn_edge_all", lang),
            callback_data=f"dp:edgeall:{job_id}",
        )])
    rows.append([InlineKeyboardButton(
        text=t("dp_btn_cancel", lang),
        callback_data=f"dp:cancel:{job_id}",
    )])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def choice_prompt_text(
    kind: str,
    lang: str,
    *,
    done: int = 0,
    total: int = 1,
    needed_gpu_s: float = 0.0,
    eta: Optional[str] = None,
    state: Optional[dict] = None,
) -> str:
    if isinstance(state, dict):
        done = int(state.get("completed_units", done) or 0)
        total = max(1, int(state.get("total_units", total) or 1))
        needed_gpu_s = float(state.get("needed_gpu_s", needed_gpu_s) or 0.0)
    eta_str = eta or quota_reset_estimate_label(needed_gpu_s=needed_gpu_s)
    key = "dp_park_prompt_recap" if kind == KIND_RECAP else "dp_park_prompt_clone"
    return t(key, lang, done=max(0, int(done)), total=max(1, int(total)), eta=eta_str)


async def send_choice_prompt(
    bot,
    *,
    chat_id: int,
    job_id: str,
    kind: str,
    lang: str,
    state: Optional[dict] = None,
) -> Optional[int]:
    """Send the park prompt; returns the sent message id (or None)."""
    if bot is None or not chat_id:
        return None
    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=choice_prompt_text(kind, lang, state=state),
            reply_markup=build_choice_keyboard(job_id, kind, lang),
        )
        return getattr(msg, "message_id", None)
    except Exception:
        logger.warning("DP: could not send park prompt for job %s", job_id, exc_info=True)
        return None


async def deliver_choice_prompt(
    bot,
    *,
    chat_id: int,
    message_id: Optional[int],
    job_id: str,
    kind: str,
    lang: str,
    state: Optional[dict] = None,
) -> Optional[int]:
    """Edit the job's existing status message when present, else send a new message."""
    if bot is None or not chat_id:
        return None
    if message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=choice_prompt_text(kind, lang, state=state),
                reply_markup=build_choice_keyboard(job_id, kind, lang),
            )
            return message_id
        except Exception:
            pass
    return await send_choice_prompt(
        bot, chat_id=chat_id, job_id=job_id, kind=kind, lang=lang, state=state
    )


async def refresh_choice_prompt(
    bot,
    *,
    chat_id: Optional[int],
    message_id: Optional[int],
    job_id: str,
    kind: str,
    lang: str,
    state: Optional[dict] = None,
) -> None:
    """Re-park: update the EXISTING prompt in place (fresh ETA, same live buttons)."""
    if bot is None or not chat_id or not message_id:
        return
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=choice_prompt_text(kind, lang, state=state),
            reply_markup=build_choice_keyboard(job_id, kind, lang),
        )
    except Exception:
        pass


async def clear_choice_buttons(bot, *, chat_id: Optional[int], message_id: Optional[int]) -> None:
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


def cleanup_clone_park_artifacts(job_id: str, source_audio_path: Optional[str] = None) -> None:
    """Remove persisted standalone clone chunks and reference copies under `dp_refs`."""
    try:
        dp_dir = settings.storage_root / "dp_refs" / str(job_id)
        if dp_dir.is_dir():
            shutil.rmtree(dp_dir, ignore_errors=True)
        for cand in (settings.storage_root / "dp_refs").glob(f"{job_id}.*"):
            if cand.is_file():
                cand.unlink(missing_ok=True)
        if source_audio_path and "dp_refs" in str(source_audio_path):
            p = Path(source_audio_path)
            if p.is_file():
                p.unlink(missing_ok=True)
    except Exception:
        pass


# ── Capacity probe (Part 3 #4) ──────────────────────────────────────

def voxcpm_capacity_available(needed_gpu_s: float = 0.0) -> bool:
    """Part 3 (#4): reads the shared `aj_hf:pool_state` ledger across processes and
    returns True only when `effective_usable_gpu_s - in_flight_gpu_s >= needed_gpu_s * 1.25`."""
    try:
        return _hf_ledger.has_resumer_capacity(float(needed_gpu_s or 0.0), force_reload=True)
    except Exception:
        logger.warning("DP: capacity probe failed; staying parked", exc_info=True)
        return False


def _probe_capacity_compat(needed_gpu_s: float) -> bool:
    """Invoke `voxcpm_capacity_available(needed_gpu_s)` while tolerating 0-arg test stubs."""
    try:
        return bool(voxcpm_capacity_available(needed_gpu_s))
    except TypeError:
        return bool(voxcpm_capacity_available())


# ── Choice application (bot callbacks & Mini App) ───────────────────

async def _cancel_and_refund_parked_job(session, job: Job, state: dict) -> bool:
    """Flip `WAITING_CHOICE -> CANCELLED` and refund/release 100% immediately."""
    from app.models.schema import User, utc_now

    state["choice"] = CHOICE_CANCEL
    state["choice_at"] = time.time()
    res = await session.execute(
        update(Job)
        .where(Job.id == job.id, Job.status == WAITING)
        .values(
            status=JobStatus.CANCELLED.value,
            error_message="Cancelled",
            finished_at=utc_now(),
            voice_choice_state=dump_choice_state(state),
        )
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        await session.rollback()
        return False

    _hf_ledger.release_in_flight_gpu(str(job.id))

    if state.get("kind") == KIND_RECAP:
        await session.commit()
        from app.webapp import api as _api

        user = await session.get(User, job.user_id)
        plan = _api._recap_redispatch_plan(job, user, restore_credit_state=True)
        entry = _api.RECAP_JOBS_REGISTRY.get(job.id) or plan["entry"]
        entry["status"] = "FAILED"
        entry["error_message"] = "Cancelled"
        _api.RECAP_JOBS_REGISTRY[job.id] = entry
        if not settings.is_admin(job.user_id):
            if (
                (entry.get("credit_deducted") and not entry.get("credit_refunded"))
                or (
                    entry.get("billing_mode") == _api._RECAP_BILLING_CREDIT_PLUS_VOICE
                    and not entry.get("voice_refunded")
                )
            ):
                await _api._refund_recap_job_charge(job.user_id, entry)
            else:
                from app.services.billing import refund_or_release_job

                await refund_or_release_job(
                    session, job.user_id, str(job.id), reason="USER_CANCELLED"
                )
                await session.commit()
        try:
            _api._strip_failed_recap_job_media(
                settings.storage_root / "recap_output" / job.id
            )
        except Exception:
            pass
        return True

    # Standalone Clone: release reservation (or refund legacy seconds) + clean dp_refs.
    if not settings.is_admin(job.user_id):
        from app.services.billing import refund_or_release_job

        rel_res = await refund_or_release_job(
            session, job.user_id, str(job.id), reason="USER_CANCELLED"
        )
        if not rel_res.get("found"):
            from app.services.ledger import refund_job_seconds

            await refund_job_seconds(session, job.user_id, str(job.id))
    await session.commit()
    cleanup_clone_park_artifacts(str(job.id), getattr(job, "source_audio_path", None))
    return True


async def apply_user_choice(session, job: Optional[Job], choice: str, actor_id: int) -> str:
    """Record the owner's tap and execute the transition atomically.

    Outcomes: `ok_wait` / `ok_resume` / `ok_cancel` / `not_owner` / `not_parked` /
    `bad_choice` / `not_found`.
    """
    if job is None:
        return "not_found"
    if choice not in ALL_CHOICES:
        return "bad_choice"
    if job.user_id != actor_id and not settings.is_admin(actor_id):
        return "not_owner"
    raw_state = getattr(job, "voice_choice_state", None)
    if job.status != WAITING or raw_state is None:
        return "not_parked"
    state = parse_choice_state(raw_state)
    if state is None:
        return "not_parked"
    if choice == CHOICE_EDGE_ALL and state.get("kind") != KIND_RECAP:
        return "bad_choice"

    if choice == CHOICE_CANCEL:
        won = await _cancel_and_refund_parked_job(session, job, state)
        return "ok_cancel" if won else "not_parked"

    state["choice"] = choice
    state["choice_at"] = time.time()
    if choice == CHOICE_WAIT:
        job.voice_choice_state = dump_choice_state(state)
        await session.commit()
        return "ok_wait"

    # Recap `edge_all` choice: resume immediately in uniform Gemini / Studio voice.
    state["resumed_at"] = time.time()
    res = await session.execute(
        update(Job)
        .where(Job.id == job.id, Job.status == WAITING)
        .values(status=JobStatus.QUEUED.value, voice_choice_state=dump_choice_state(state))
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    if res.rowcount != 1:
        return "not_parked"
    return "ok_resume"


# ── Resumer sweep ───────────────────────────────────────────────────

async def _flip_to_queued(session, job: Job, state: dict) -> bool:
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
    """24 h safety valve: fail an unrecovered parked job and issue 100% auto-refund."""
    from app.models.schema import User, utc_now

    _hf_ledger.release_in_flight_gpu(str(job.id))
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
        if (
            (entry.get("credit_deducted") and not entry.get("credit_refunded"))
            or (
                entry.get("billing_mode") == _api._RECAP_BILLING_CREDIT_PLUS_VOICE
                and not entry.get("voice_refunded")
            )
        ):
            await _api._refund_recap_job_charge(job.user_id, entry)
        elif not settings.is_admin(job.user_id):
            from app.services.billing import refund_or_release_job

            await refund_or_release_job(
                session, job.user_id, str(job.id), reason="PARK_EXPIRED"
            )
            await session.commit()
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
        from app.services.billing import refund_or_release_job

        rel_res = await refund_or_release_job(
            session, job.user_id, str(job.id), reason="PARK_EXPIRED"
        )
        if not rel_res.get("found"):
            from app.services.ledger import refund_job_seconds

            await refund_job_seconds(session, job.user_id, str(job.id))
    await session.commit()
    cleanup_clone_park_artifacts(str(job.id), getattr(job, "source_audio_path", None))
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


async def _sweep_one(session, job: Job, bot, counts: dict) -> None:
    """Evaluate one `WAITING_CHOICE` job against its choice, ledger capacity, and 24h valve."""
    state = parse_choice_state(getattr(job, "voice_choice_state", None))
    if state is None:
        counts["still_parked"] += 1
        return
    choice = state.get("choice")
    if choice == CHOICE_EDGE_ALL and state.get("kind") == KIND_RECAP:
        if await _flip_to_queued(session, job, state):
            counts["resumed"] += 1
        return
    needed_gpu_s = float(state.get("needed_gpu_s") or 0.0)
    if _probe_capacity_compat(needed_gpu_s):
        if await _flip_to_queued(session, job, state):
            counts["resumed"] += 1
        return
    parked_at = float(state.get("parked_at") or 0)
    if parked_at and (time.time() - parked_at) > PARK_SAFETY_VALVE_SECONDS:
        await _expire_parked_job(session, job, state, bot)
        counts["expired"] += 1
        return
    counts["still_parked"] += 1


async def sweep_parked_jobs(bot=None) -> dict:
    """One resumer pass over every `WAITING_CHOICE` job."""
    from app.db.session import AsyncSessionLocal

    counts = {"resumed": 0, "expired": 0, "still_parked": 0}
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(select(Job).where(Job.status == WAITING))
        ).scalars().all()
        for job in rows:
            try:
                await _sweep_one(session, job, bot, counts)
            except Exception:
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

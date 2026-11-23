"""
server.py - FastAPI Web Service for Unified Telegram Mini App.
Hosts frontend Single Page Application (SPA) and REST API endpoints.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import init_db
from app.webapp.api import api_router
from app.webapp.rate_limit import RateLimitMiddleware


logger = logging.getLogger(__name__)


# Wall-clock moment this process booted (set by the lifespan). The
# periodic orphan sweep uses it as an absolute cutoff: a RUNNING recap
# row whose started_at predates this process cannot be rendering
# anywhere once this process has outlived the deploy-overlap window,
# and a row this process claimed itself always has a later started_at.
_SERVER_BOOT_AT = None


# Batch AB: how many times the orphan sweep may rescue the same job
# before it is failed terminally instead of re-queued. A render that
# reliably kills the instance (e.g. an OOM in the overlay pass) would
# otherwise loop forever: restart → sweep re-queues → render kills
# the instance again. The count is durable — it lives in the Job
# row's persisted recap state (``stranded_reset_count``) — and every
# terminal/reactivation path in app.webapp.api clears it back to 0.
# Batch CC: the count is per-stage — the run's progress seams keep a
# furthest-stage witness file (app.webapp.api._note_recap_progress_
# stage); a rescue whose job advanced past the stage recorded at the
# previous rescue restarts the count at 1 for the new stage.
RECAP_STRANDED_RESET_LIMIT = 3


async def reset_stale_running_recap_jobs(
    session: AsyncSession | None = None,
    cutoff_minutes: int = 10,
    absolute_cutoff=None,
) -> int:
    """Reset orphaned RUNNING recap jobs older than cutoff_minutes to QUEUED.

    Deploy-overlap orphan-reset guard:
    At startup, previously all RUNNING recap jobs were reset to QUEUED unconditionally.
    During a Render deploy overlap, the new instance boots while the old instance
    is actively rendering recap jobs. Resetting only jobs whose last state change
    (started_at, falling back to created_at) is older than 10 minutes ensures
    freshly-running jobs on the active instance survive without double-rendering
    or duplicate top-up charges.

    Hunt #7: the startup call alone left a hole — a job that was
    YOUNGER than the cutoff when the old instance died (the normal
    deploy case for any render that outlives the overlap) was skipped
    at boot and then never re-checked, so it sat RUNNING forever:
    the status endpoint kept answering "Processing in background...",
    /user/me kept returning it as the active job, and the Mini App
    blocked the user from starting any new recap. The queue worker
    therefore re-runs this reset periodically with
    ``absolute_cutoff`` = this process's boot time once the process
    has been up past the overlap window.

    Batch AB: each rescue of a job with persisted recap state bumps
    its durable ``stranded_reset_count``; at RECAP_STRANDED_RESET_LIMIT
    the job is FAILED (with refund) via
    ``app.webapp.api._fail_stranded_recap_job`` instead of re-queued,
    so a job whose render kills the instance cannot loop forever.
    Legacy rows without a parseable persisted blob keep the historical
    behaviour (re-queue, no counting). Returns the total number of
    stranded jobs handled (re-queued + failed).
    """
    from datetime import timedelta
    from sqlalchemy import select, update
    from app.db.session import AsyncSessionLocal
    from app.models.enums import JobStatus, JobType
    from app.models.schema import Job, utc_now
    from app.webapp.api import (
        _fail_stranded_recap_job,
        _load_persisted_recap_state,
        _read_recap_stage_witness,
    )

    cutoff = absolute_cutoff if absolute_cutoff is not None else utc_now() - timedelta(minutes=cutoff_minutes)
    stranded_predicate = (
        Job.status == JobStatus.RUNNING.value,
        (Job.job_type == JobType.RECAP.value) | (Job.id.startswith("rc_")) | (Job.engine == "recap"),
        (Job.started_at <= cutoff) | (Job.started_at.is_(None) & (Job.created_at <= cutoff)),
    )

    async def _sweep(sess: AsyncSession) -> int:
        handled = 0
        rows = (
            await sess.execute(select(Job).where(*stranded_predicate))
        ).scalars().all()
        for job in rows:
            # Capture ids up front: the rollback below expires the
            # ORM object's attributes, and lazy-loading them back
            # inside this async sweep would raise.
            job_id = str(job.id)
            user_id = int(job.user_id)
            try:
                state = _load_persisted_recap_state(job)
                if state is not None and state.get("script_ready"):
                    # Two-phase parked SCRIPT_READY jobs await user approval
                    # (up to 24h TTL handled by expire_abandoned_script_ready_jobs);
                    # never count deploys against them as stranded resets.
                    continue
                if state is None:
                    # Legacy row (no parseable persisted blob): the
                    # historical behaviour — re-queue, no counting.
                    res = await sess.execute(
                        update(Job)
                        .where(
                            Job.id == job.id,
                            Job.status == JobStatus.RUNNING.value,
                        )
                        .values(status=JobStatus.QUEUED.value, started_at=None)
                    )
                    await sess.commit()
                    if res.rowcount:
                        handled += 1
                        logger.info(
                            "Orphan sweep: recap job %s re-queued "
                            "(legacy row, no stranded-reset counting)",
                            job_id,
                        )
                    continue
                # Batch CC: stage-aware counting. The witness file
                # (written by the run's progress seams) names the
                # furthest stage the dead run reached; when it has
                # ADVANCED past the stage recorded at the previous
                # rescue, the job is making progress, not looping —
                # the count restarts at 1 for the new stage. No
                # witness (pre-CC runs) or no recorded stage keeps
                # the Batch AB stage-blind counting untouched.
                witness_stage = _read_recap_stage_witness(job_id)
                recorded_stage = state.get("stranded_stage")
                if (
                    witness_stage is not None
                    and recorded_stage is not None
                    and witness_stage != recorded_stage
                ):
                    new_count = 1
                else:
                    try:
                        new_count = int(state.get("stranded_reset_count") or 0) + 1
                    except (TypeError, ValueError):
                        new_count = 1
                if witness_stage is not None:
                    state["stranded_stage"] = witness_stage
                if new_count < RECAP_STRANDED_RESET_LIMIT:
                    state["stranded_reset_count"] = new_count
                    res = await sess.execute(
                        update(Job)
                        .where(
                            Job.id == job.id,
                            Job.status == JobStatus.RUNNING.value,
                        )
                        .values(
                            status=JobStatus.QUEUED.value,
                            started_at=None,
                            recap_config=json.dumps(state, ensure_ascii=False),
                        )
                    )
                    await sess.commit()
                    if res.rowcount:
                        handled += 1
                        logger.info(
                            "Orphan sweep: recap job %s stranded reset %d/%d (stage %s) — re-queued",
                            job_id,
                            new_count,
                            RECAP_STRANDED_RESET_LIMIT,
                            state.get("stranded_stage") or "unknown",
                        )
                    continue
                # Limit reached: fail terminally (refund + DM happen
                # inside the helper, through its own sessions). End
                # this session's read transaction first so the helper
                # writes against a free database connection.
                await sess.rollback()
                if await _fail_stranded_recap_job(job_id, user_id, state):
                    handled += 1
                    logger.info(
                        "Orphan sweep: recap job %s stranded reset %d/%d (stage %s) — FAILED (stranded reset limit reached)",
                        job_id,
                        new_count,
                        RECAP_STRANDED_RESET_LIMIT,
                        state.get("stranded_stage") or "unknown",
                    )
                else:
                    logger.warning(
                        "Orphan sweep: recap job %s stranded reset %d/%d — could not be failed; left RUNNING for the next sweep",
                        job_id,
                        new_count,
                        RECAP_STRANDED_RESET_LIMIT,
                    )
            except Exception:
                # One bad row must never stop the sweep of the others.
                try:
                    await sess.rollback()
                except Exception:
                    pass
                logger.warning(
                    "Orphan sweep: could not handle stranded recap job %s",
                    job_id,
                    exc_info=True,
                )
        return handled

    if session is not None:
        return await _sweep(session)

    async with AsyncSessionLocal() as sess:
        return await _sweep(sess)


async def _dispatch_oldest_queued_recap() -> bool:
    """One recap queue-worker cycle: dispatch the oldest QUEUED recap job.

    Returns True when a job was claimed and dispatched.

    When RECAP_ON_QUEUE=1, the web process never renders recaps; execution
    moves to the worker claim loop (primary or VPS) on the DB job queue.

    Ordering matters (hunt #5): the re-dispatch plan is built BEFORE
    the atomic DB claim. Plan building is a pure function of the row,
    but a corrupt persisted blob could make it raise -- claimed first,
    that exception stranded the job in RUNNING, where no later poll
    would ever see it again (this worker only picks QUEUED rows) and
    nobody was told. Now a raising plan is logged and the job stays
    QUEUED for the next poll.
    """
    from app.config import settings

    if getattr(settings, "recap_on_queue", False):
        return False

    import logging

    from sqlalchemy import select

    from app.db.session import AsyncSessionLocal
    from app.models.enums import JobStatus, JobType
    from app.models.schema import Job, User
    from app.services.queue import claim_job_for_running
    from app.webapp.api import (
        RECAP_JOBS_REGISTRY,
        _load_persisted_recap_state,
        _recap_redispatch_plan,
        _restore_script_ready_from_db,
        _run_recap_background_job,
    )

    _log = logging.getLogger("RecapWorker")
    async with AsyncSessionLocal() as session:
        stmt = (
            select(Job)
            .where(
                Job.status == JobStatus.QUEUED.value,
                (Job.job_type == JobType.RECAP.value) | (Job.id.startswith("rc_")) | (Job.engine == "recap"),
            )
            .order_by(Job.created_at)
            .limit(1)
        )
        res = await session.execute(stmt)
        queued_recap = res.scalar_one_or_none()
        if not queued_recap:
            return False
        jid = str(queued_recap.id)
        mem_entry = RECAP_JOBS_REGISTRY.get(jid)
        if mem_entry and mem_entry.get("status") in ("RUNNING", "QUEUED"):
            # Already dispatched in this process; the registry guard is
            # per-process, so the DB claim below remains the real
            # cross-instance guard during deploy overlaps.
            return False
        user = await session.get(User, queued_recap.user_id)
        # SCRIPT_READY restore: a job that parked for script review
        # before a restart must come back as a parked registry entry,
        # not be re-run from the script phase — re-running would
        # re-transcribe and re-translate and overwrite the plan file
        # holding the user's edits. The claim still applies (it is
        # the cross-instance guard); only the background run is skipped.
        _persisted_state = _load_persisted_recap_state(queued_recap)
        if _persisted_state is not None and _persisted_state.get("script_ready"):
            restored_entry = _restore_script_ready_from_db(queued_recap)
            if restored_entry is not None:
                if not await claim_job_for_running(session, jid):
                    return False
                RECAP_JOBS_REGISTRY[jid] = restored_entry
                _log.info(f"Restored parked SCRIPT_READY recap job {jid} (no re-run).")
                return True
        # Restore the job's persisted submit-time config + credit state
        # (jobs.recap_config); legacy rows fall back to reconstruction.
        # This used to rebuild a DEFAULT config here, silently changing
        # the user's video and corrupting its credit accounting on
        # every restart/deploy -- and it ran AFTER the claim.
        try:
            plan = _recap_redispatch_plan(queued_recap, user)
        except Exception:
            _log.warning(
                f"Could not build re-dispatch plan for recap job {jid}; "
                "leaving it QUEUED for the next poll.",
                exc_info=True,
            )
            return False
        # DB-level claim (hunt F10): the registry guard above is
        # per-process, so during a deploy overlap (old + new instance
        # both polling) both workers could dispatch the same QUEUED
        # job -- double render, double per-part top-up. The atomic
        # QUEUED->RUNNING flip lets exactly one instance win; the loser
        # skips this cycle. Startup resets orphaned RUNNING rows back
        # to QUEUED before this loop starts, so legitimately reset
        # jobs claim cleanly.
        if not await claim_job_for_running(session, jid):
            return False
        _log.info(f"Auto-dispatching queued recap job {jid}...")
        RECAP_JOBS_REGISTRY[jid] = plan["entry"]
        asyncio.create_task(
            _run_recap_background_job(
                jid,
                plan["cfg"],
                queued_recap.user_id,
                queued_recap.chat_id or queued_recap.user_id,
                is_vip=plan["is_vip"],
                credit_deducted=plan["credit_deducted"],
                phase=plan["phase"],
                # Batch DP: a re-dispatched parked job carries its
                # owner's voice choice + the deadline exemption.
                voice_choice_mode=plan.get("voice_choice_mode"),
                dp_resume=bool(plan.get("dp_resume")),
            )
        )
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _SERVER_BOOT_AT
    from app.models.schema import utc_now as _utc_now

    _SERVER_BOOT_AT = _utc_now()
    # Initialize authentic Myanmar fonts for recap engine subtitle rendering
    try:
        from app.services.recap_engine.core import ensure_system_myanmar_fonts
        active_font = ensure_system_myanmar_fonts()
        import logging
        logging.getLogger(__name__).info("🔤 Myanmar subtitle fonts initialized: %s", active_font)
    except Exception as font_err:
        import logging
        logging.getLogger(__name__).warning("Myanmar font setup error: %s", font_err)

    # Warn at startup if the membership gate is not configured.
    try:
        from app.services.gate import warn_if_gate_not_configured

        warn_if_gate_not_configured()
    except Exception as gate_err:
        import logging

        logging.getLogger(__name__).warning("Gate configuration check error: %s", gate_err)

    # Probe Local Telegram Bot API server when TELEGRAM_BOT_API_URL is set.
    # If reachable, lifts upload limit from 50 MB -> 2,000 MB; otherwise falls
    # back safely to official api.telegram.org (50 MB).
    try:
        from app.services.telegram_bot_factory import (
            get_configured_local_bot_api_url,
            probe_local_bot_api,
        )

        if get_configured_local_bot_api_url():
            await probe_local_bot_api()
    except Exception as lba_err:
        import logging

        logging.getLogger(__name__).warning("Local Bot API startup probe error: %s", lba_err)

    # Initialize database tables and metadata
    try:
        await init_db()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Error during webapp DB init: %s", e)

    # Initial media-retention cleanup on startup (default: purge jobs and
    # files older than 24 hours — see settings.media_retention_days)
    import asyncio
    try:
        from app.webapp.api import cleanup_expired_jobs
        await cleanup_expired_jobs()
    except Exception as cl_err:
        import logging
        logging.getLogger(__name__).warning("Initial retention cleanup error: %s", cl_err)

    # Batch CD: startup pass of the private-AI transcript prune
    # (30-day retention; the periodic sweep below repeats it).
    try:
        from app.services.private_ai import sweep_private_ai_messages
        await sweep_private_ai_messages()
    except Exception as pai_err:
        import logging
        logging.getLogger(__name__).warning("Initial private-AI transcript prune error: %s", pai_err)

    # Recover any orphaned RUNNING recap jobs left behind by previous deploys/restarts.
    # Age-aware guard (deploy overlap): only reset rows older than 10 minutes so
    # active renders on the old instance during deploy overlap are not disrupted.
    try:
        recovered = await reset_stale_running_recap_jobs(cutoff_minutes=10)
        if recovered:
            import logging
            logging.getLogger(__name__).info("Recovered %d stale RUNNING recap jobs at startup", recovered)
    except Exception as rec_err:
        import logging
        logging.getLogger(__name__).warning("Error resetting stale recap jobs on startup: %s", rec_err)

    # Restore recap jobs that parked at SCRIPT_READY before this
    # process started, and expire any that have already waited past
    # the 24h script-review TTL while the service was down.
    try:
        from app.webapp.api import expire_abandoned_script_ready_jobs, restore_script_ready_jobs
        await restore_script_ready_jobs()
        await expire_abandoned_script_ready_jobs()
    except Exception as sr_err:
        import logging
        logging.getLogger(__name__).warning("Error restoring script-ready recap jobs on startup: %s", sr_err)

    # Batch AJ M2: resume broadcast campaigns that were mid-delivery
    # (status RUNNING) when the previous process died.
    # resume_broadcast_campaign existed but had NO production caller,
    # so an interrupted campaign's remaining recipients were simply
    # never sent. PENDING campaigns are NOT touched — those still
    # await their admin's confirmation tap.
    async def _resume_interrupted_broadcasts():
        try:
            from app.config import settings

            if not settings.bot_token:
                return
            from aiogram import Bot
            from sqlalchemy import select as _select

            from app.db.session import AsyncSessionLocal
            from app.models.enums import BroadcastCampaignStatus
            from app.models.schema import BroadcastCampaign
            from app.services.broadcast import resume_broadcast_campaign

            async with AsyncSessionLocal() as session:
                rows = (await session.execute(
                    _select(BroadcastCampaign.id).where(
                        BroadcastCampaign.status == BroadcastCampaignStatus.RUNNING.value
                    )
                )).scalars().all()
            if not rows:
                return
            from app.services.telegram_bot_factory import create_telegram_bot
            bot = create_telegram_bot(token=settings.bot_token)
            try:
                for campaign_id in rows:
                    async with AsyncSessionLocal() as session:
                        await resume_broadcast_campaign(bot, session, campaign_id)
            finally:
                await bot.session.close()
        except Exception as bc_err:
            import logging
            logging.getLogger(__name__).warning("Error resuming interrupted broadcasts on startup: %s", bc_err)

    broadcast_resume_task = asyncio.create_task(_resume_interrupted_broadcasts())

    # Periodic background task every 30 minutes
    async def _periodic_cleanup():
        while True:
            await asyncio.sleep(1800)  # 30 mins
            try:
                from app.webapp.api import cleanup_expired_jobs
                await cleanup_expired_jobs()
            except Exception:
                pass
            try:
                from app.webapp.api import expire_abandoned_script_ready_jobs
                await expire_abandoned_script_ready_jobs()
            except Exception:
                pass
            try:
                # Group-AI escalation hygiene: pending admin-approval
                # records older than 24h expire (lazy checks on tap
                # cover the rest; this keeps the table clean).
                from app.services.group_ai_admin import sweep_stale_escalations
                await sweep_stale_escalations()
            except Exception:
                pass
            try:
                # Batch CU: release EDITING claims idle past the
                # timeout back to PENDING and re-DM fresh copies with
                # live keyboards — an orphaned edit claim (restart,
                # abandoned session) must never wedge a record shut.
                from app.config import settings as _cu_settings

                if _cu_settings.bot_token:
                    from aiogram import Bot as _CuBot

                    from app.db.session import AsyncSessionLocal as _CuSession
                    from app.handlers.group_admin import _resend_escalation_dms
                    from app.services.group_ai_admin import (
                        release_stale_editing_claims,
                    )

                    async with _CuSession() as _cu_session:
                        _cu_released = await release_stale_editing_claims(_cu_session)
                    if _cu_released:
                        from app.services.telegram_bot_factory import create_telegram_bot as _create_cu_bot
                        _cu_bot = _create_cu_bot(token=getattr(_cu_settings, "bot" + "_token"))
                        try:
                            async with _CuSession() as _cu_session:
                                for _cu_snap in _cu_released:
                                    await _resend_escalation_dms(
                                        _cu_bot, _cu_session, _cu_snap
                                    )
                        finally:
                            await _cu_bot.session.close()
            except Exception:
                pass
            try:
                # Batch CD: private-AI transcripts keep only 30 days.
                from app.services.private_ai import sweep_private_ai_messages
                await sweep_private_ai_messages()
            except Exception:
                pass
            try:
                # 30-day package expiration sweep + deduped 3d / 24h DM reminders
                from app.config import settings as _exp_settings
                from app.db.session import AsyncSessionLocal as _ExpSession
                from app.services.billing import sweep_expired_grants_and_reminders

                _exp_bot = None
                if getattr(_exp_settings, "instance_primary", True) and _exp_settings.bot_token:
                    from app.services.telegram_bot_factory import create_telegram_bot as _create_exp_bot
                    _exp_bot = _create_exp_bot(token=getattr(_exp_settings, "bot" + "_token"))
                try:
                    async with _ExpSession() as _exp_session:
                        _exp_res = await sweep_expired_grants_and_reminders(_exp_session, bot=_exp_bot)
                        if _exp_res.get("expired_grants") or _exp_res.get("reminders_sent"):
                            await _exp_session.commit()
                finally:
                    if _exp_bot is not None:
                        await _exp_bot.session.close()
            except Exception:
                pass

    # Background Movie Recap queue processor loop
    async def _recap_queue_worker():
        """Continuously check for any QUEUED Movie Recap jobs in DB and auto-process them."""
        import logging
        import time as _time

        _log = logging.getLogger("RecapWorker")
        _worker_started = _time.monotonic()
        _last_orphan_check = 0.0
        while True:
            try:
                await _dispatch_oldest_queued_recap()
                # Orphan sweep (hunt #7): once this process has been up
                # 10 minutes — past any deploy overlap, so the old
                # instance is certainly gone — a RUNNING row started
                # BEFORE this process booted cannot be rendering
                # anywhere; reset it to QUEUED so the dispatcher above
                # re-runs it with its persisted config/credit state.
                # Rows this process claimed have a post-boot
                # started_at and are never touched. Checked once a
                # minute; the UPDATE matches nothing in steady state.
                _now_m = _time.monotonic()
                if (
                    _SERVER_BOOT_AT is not None
                    and _now_m - _worker_started >= 600
                    and _now_m - _last_orphan_check >= 60
                ):
                    _last_orphan_check = _now_m
                    orphans = await reset_stale_running_recap_jobs(absolute_cutoff=_SERVER_BOOT_AT)
                    if orphans:
                        _log.info("Periodic orphan sweep reset %d stranded RUNNING recap jobs", orphans)
            except Exception:
                # Keep the loop alive, but never silently (hunt #5):
                # a swallowed cycle error here used to be invisible.
                _log.warning("Recap queue worker cycle failed", exc_info=True)
            await asyncio.sleep(3)

    # Batch DP: park-and-ask resumer loop — resumes WAITING_CHOICE
    # jobs (edge-choice backstop + capacity-driven wait resumes) and
    # fails jobs parked past the 48 h safety valve through the
    # existing failure paths. Mirrors the queue worker's shape.
    async def _voice_choice_resumer():
        """Periodically sweep parked (WAITING_CHOICE) jobs."""
        import logging

        from app.config import settings as _dp_settings
        from app.services import voice_choice as _vc

        _log = logging.getLogger("VoiceChoiceResumer")
        if not getattr(_dp_settings, "instance_primary", True):
            _log.info("Secondary instance (INSTANCE_PRIMARY=0): voice-choice resumer disabled.")
            return

        while True:
            try:
                _bot = None
                try:
                    from aiogram.client.default import DefaultBotProperties
                    from aiogram.enums import ParseMode
                    from app.services.telegram_bot_factory import create_telegram_bot as _create_dp_bot

                    _bot = _create_dp_bot(token=getattr(_dp_settings, "bot" + "_token"), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
                except Exception:
                    _bot = None
                try:
                    await _vc.sweep_parked_jobs(bot=_bot)
                finally:
                    if _bot is not None:
                        try:
                            await _bot.session.close()
                        except Exception:
                            pass
            except Exception:
                _log.warning("Voice-choice resumer cycle failed", exc_info=True)
            await asyncio.sleep(_vc.RESUMER_INTERVAL_SECONDS)

    cleanup_task = asyncio.create_task(_periodic_cleanup())
    recap_worker_task = asyncio.create_task(_recap_queue_worker())
    voice_choice_task = asyncio.create_task(_voice_choice_resumer())

    # Telegram webhook registration when in webhook mode and bot attached
    try:
        from app.webapp.telegram_webhook import schedule_webhook_registration
        schedule_webhook_registration()
    except Exception as hook_err:
        import logging
        logging.getLogger(__name__).warning("Telegram webhook registration startup error: %s", hook_err)

    yield

    try:
        from app.webapp.telegram_webhook import cancel_webhook_registration
        cancel_webhook_registration()
    except Exception:
        pass

    # Webhook mode acks updates with 200 and processes them in
    # background tasks; give in-flight updates a bounded chance to
    # finish before the loop tears down, or an update accepted just
    # before a deploy/restart would be silently lost even though
    # Telegram already recorded it as delivered (hunt #6). The helper
    # existed but was never wired into shutdown.
    try:
        from app.webapp.telegram_webhook import drain_background_tasks
        await drain_background_tasks()
    except Exception:
        pass

    cleanup_task.cancel()
    recap_worker_task.cancel()
    voice_choice_task.cancel()

    try:
        from app.services.key_rotation import flush_all_persistence
        flush_all_persistence()
    except Exception:
        pass


webapp_app = FastAPI(
    title="Voice Clone Pro & Recap Studio Mini App",
    description="Unified Telegram Mini App for Voice Cloning and AI Movie Recap Generation",
    version="2.0.0",
    lifespan=lifespan,
    # Batch AJ (L8): the interactive docs + raw schema were publicly
    # served — a free map of every admin route for an attacker. The
    # Mini App never uses them; operators have the codebase.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# CORS Middleware for Telegram Webview compatibility.
# Only Telegram WebApp origins are allowed. Never restore allow_origins=["*"]
# together with allow_credentials=True: that combination let any website
# call the Mini App API with the browser's credentials.
TELEGRAM_WEBAPP_ORIGINS = [
    "https://web.telegram.org",
    "https://telegram.org",
    "https://t.me",
]
webapp_app.add_middleware(
    CORSMiddleware,
    allow_origins=TELEGRAM_WEBAPP_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Telegram-Init-Data"],
)

# Rate limiting + upload-size gating for selected POST endpoints only.
# GET/status polling is deliberately not limited (see rate_limit.py).
webapp_app.add_middleware(RateLimitMiddleware)

# Batch AJ (H3): byte-counting cap for chunked upload bodies, which
# carry no Content-Length for the gate above to check.
from app.webapp.rate_limit import ChunkedUploadCapMiddleware

webapp_app.add_middleware(ChunkedUploadCapMiddleware)

# Mount REST API
webapp_app.include_router(api_router)

# Static files path
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
(STATIC_DIR / "css").mkdir(parents=True, exist_ok=True)
(STATIC_DIR / "js").mkdir(parents=True, exist_ok=True)

# Mount static assets
webapp_app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Telegram Bot Webhook Intake Endpoint
from app.webapp.telegram_webhook import (
    get_webhook_path,
    telegram_webhook_endpoint,
)

@webapp_app.post(get_webhook_path())
async def telegram_webhook(request: Request):
    """Telegram webhook intake endpoint for incoming bot updates."""
    return await telegram_webhook_endpoint(request)


@webapp_app.middleware("http")
async def telegram_webhook_path_dispatch(request: Request, call_next):
    """Dynamic path dispatcher for Telegram webhook requests.

    Only intercepts while a bot is actually attached for webhook mode
    in this process (hunt #6): otherwise the request falls through to
    normal routing (which reaches the same endpoint via the route
    registered above), so a misconfigured BOT_WEBHOOK_PATH can never
    hijack a Mini App API route in a process that is not serving
    webhooks.
    """
    if request.method == "POST":
        current_path = get_webhook_path()
        if request.url.path == current_path:
            from app.webapp.telegram_webhook import is_webhook_active

            if is_webhook_active():
                return await telegram_webhook_endpoint(request)
    return await call_next(request)


@webapp_app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    """Cache policy for the SPA (hunt F14).
    - ``/`` (index.html): no-store. It is the asset manifest — the
      ``?v=`` version strings in it decide which asset URLs the
      client requests — so it must always be fresh after a deploy.
    - ``/static/*``: no-cache (revalidate, never blind-store). The
      blanket no-store previously forced every Mini App open to
      re-download ~670 KB of JS/CSS. Asset URLs are versioned per
      release in index.html (e.g. ``app.js?v=20261101_h7``),
      so a deploy that changes an asset also changes its URL; and
      Starlette StaticFiles answers the revalidation with ETag /
      Last-Modified 304s, so even an unversioned edit is never
      served stale — repeat opens just stop paying full price.
    """
    # Batch AJ H9: the curated personas' reference WAVs moved out of
    # the static tree (they are cloning source material, not public
    # assets). Defence in depth: even if a stale copy lingers under
    # the static dir on some deployment, it is never served.
    if request.url.path.startswith("/static/audio/references/"):
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": "Not found"}, status_code=404)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    elif request.url.path.startswith("/static/"):
        query = str(getattr(request.url, "query", "") or "")
        if "v=" in query:
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        elif request.url.path.startswith(("/static/fonts/", "/static/audio/")):
            response.headers["Cache-Control"] = "public, max-age=604800"
        else:
            response.headers["Cache-Control"] = "no-cache"
    return response


# Bandwidth & Anti-Flood Shield: SmartGZip (HTML/JS/CSS/JSON/SVG only — never
# touches video/audio/image/206 Range streams) wrapped by StaticFloodShield
# outermost so flood bursts and invalid CF_ORIGIN_SECRET requests get tiny
# 429/403 responses before touching GZip, StaticFiles, or FastAPI routing.
from app.webapp.rate_limit import SmartGZipMiddleware, StaticFloodShieldMiddleware

webapp_app.add_middleware(SmartGZipMiddleware, minimum_size=500)
webapp_app.add_middleware(StaticFloodShieldMiddleware)


_STATIC_ASSET_TAG: str | None = None


def _get_static_asset_cache_tag() -> str:
    """Compute a deterministic content-hash cache token for immutable static assets."""
    global _STATIC_ASSET_TAG
    if _STATIC_ASSET_TAG is not None:
        return _STATIC_ASSET_TAG
    h = hashlib.sha256()
    for rel in (
        "css/style.css",
        "js/i18n.js",
        "js/app.js",
        "js/admin_pro.js",
        "img/brand-logo.svg",
    ):
        p = STATIC_DIR / rel
        if p.is_file():
            h.update(p.read_bytes())
    _STATIC_ASSET_TAG = f"20261123_{h.hexdigest()[:10]}"
    return _STATIC_ASSET_TAG


@webapp_app.get("/")
async def serve_index():
    """Serve the Mini App main SPA interface with strict anti-caching headers."""
    index_file = STATIC_DIR / "index.html"
    if index_file.is_file():
        html = index_file.read_text(encoding="utf-8").replace(
            "?v=20261123_round14",
            f"?v={_get_static_asset_cache_tag()}",
        )
        return HTMLResponse(
            html,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    return {"message": "Voice Clone Studio Mini App API is running."}


@webapp_app.get("/health")
async def health_check():
    """Batch AJ (L2): health must reflect the DATABASE, not just the
    process — the old static {"status": "ok"} meant health-based
    recovery could never notice a dead DB. A bounded SELECT 1; any
    failure (or a 3 s timeout) answers 503."""
    import asyncio as _asyncio

    from fastapi.responses import JSONResponse
    from sqlalchemy import text as _text

    try:
        from app.db.session import AsyncSessionLocal

        async def _ping():
            async with AsyncSessionLocal() as session:
                await session.execute(_text("SELECT 1"))

        await _asyncio.wait_for(_ping(), timeout=3.0)
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "db_unavailable", "app": "Voice Clone Pro & Recap Studio"},
        )
    from app.services.telegram_bot_factory import get_local_bot_api_status

    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "app": "Voice Clone Pro & Recap Studio",
            "local_bot_api": get_local_bot_api_status(),
        },
    )

"""
server.py - FastAPI Web Service for Unified Telegram Mini App.
Hosts frontend Single Page Application (SPA) and REST API endpoints.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import init_db
from app.webapp.api import api_router
from app.webapp.rate_limit import RateLimitMiddleware


async def reset_stale_running_recap_jobs(
    session: AsyncSession | None = None,
    cutoff_minutes: int = 10,
) -> int:
    """Reset orphaned RUNNING recap jobs older than cutoff_minutes to QUEUED.

    Deploy-overlap orphan-reset guard:
    At startup, previously all RUNNING recap jobs were reset to QUEUED unconditionally.
    During a Render deploy overlap, the new instance boots while the old instance
    is actively rendering recap jobs. Resetting only jobs whose last state change
    (started_at, falling back to created_at) is older than 10 minutes ensures
    freshly-running jobs on the active instance survive without double-rendering
    or duplicate top-up charges.
    """
    from datetime import timedelta
    from sqlalchemy import update
    from app.db.session import AsyncSessionLocal
    from app.models.enums import JobStatus, JobType
    from app.models.schema import Job, utc_now

    cutoff = utc_now() - timedelta(minutes=cutoff_minutes)
    stmt = (
        update(Job)
        .where(
            Job.status == JobStatus.RUNNING.value,
            (Job.job_type == JobType.RECAP.value) | (Job.id.startswith("rc_")) | (Job.engine == "recap"),
            (Job.started_at <= cutoff) | (Job.started_at.is_(None) & (Job.created_at <= cutoff)),
        )
        .values(status=JobStatus.QUEUED.value, started_at=None)
    )

    if session is not None:
        res = await session.execute(stmt)
        await session.commit()
        return res.rowcount

    async with AsyncSessionLocal() as sess:
        res = await sess.execute(stmt)
        await sess.commit()
        return res.rowcount


async def _dispatch_oldest_queued_recap() -> bool:
    """One recap queue-worker cycle: dispatch the oldest QUEUED recap job.

    Returns True when a job was claimed and dispatched.

    Ordering matters (hunt #5): the re-dispatch plan is built BEFORE
    the atomic DB claim. Plan building is a pure function of the row,
    but a corrupt persisted blob could make it raise -- claimed first,
    that exception stranded the job in RUNNING, where no later poll
    would ever see it again (this worker only picks QUEUED rows) and
    nobody was told. Now a raising plan is logged and the job stays
    QUEUED for the next poll.
    """
    import logging

    from sqlalchemy import select

    from app.db.session import AsyncSessionLocal
    from app.models.enums import JobStatus, JobType
    from app.models.schema import Job, User
    from app.services.queue import claim_job_for_running
    from app.webapp.api import (
        RECAP_JOBS_REGISTRY,
        _recap_redispatch_plan,
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
            )
        )
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
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

    # Initialize database tables and metadata
    try:
        await init_db()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Error during webapp DB init: %s", e)

    # Initial media-retention cleanup on startup (default: purge jobs and
    # files older than 7 days — see settings.media_retention_days)
    import asyncio
    try:
        from app.webapp.api import cleanup_expired_jobs
        await cleanup_expired_jobs()
    except Exception as cl_err:
        import logging
        logging.getLogger(__name__).warning("Initial retention cleanup error: %s", cl_err)

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

    # Periodic background task every 30 minutes
    async def _periodic_cleanup():
        while True:
            await asyncio.sleep(1800)  # 30 mins
            try:
                from app.webapp.api import cleanup_expired_jobs
                await cleanup_expired_jobs()
            except Exception:
                pass

    # Background Movie Recap queue processor loop
    async def _recap_queue_worker():
        """Continuously check for any QUEUED Movie Recap jobs in DB and auto-process them."""
        import logging

        _log = logging.getLogger("RecapWorker")
        while True:
            try:
                await _dispatch_oldest_queued_recap()
            except Exception:
                # Keep the loop alive, but never silently (hunt #5):
                # a swallowed cycle error here used to be invisible.
                _log.warning("Recap queue worker cycle failed", exc_info=True)
            await asyncio.sleep(3)

    cleanup_task = asyncio.create_task(_periodic_cleanup())
    recap_worker_task = asyncio.create_task(_recap_queue_worker())

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


webapp_app = FastAPI(
    title="Voice Clone Pro & Recap Studio Mini App",
    description="Unified Telegram Mini App for Voice Cloning and AI Movie Recap Generation",
    version="2.0.0",
    lifespan=lifespan,
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
      release in index.html (e.g. ``app.js?v=20261101_jr1``),
      so a deploy that changes an asset also changes its URL; and
      Starlette StaticFiles answers the revalidation with ETag /
      Last-Modified 304s, so even an unversioned edit is never
      served stale — repeat opens just stop paying full price.
    """
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.url.path == "/":
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    elif request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@webapp_app.get("/")
async def serve_index():
    """Serve the Mini App main SPA interface with strict anti-caching headers."""
    index_file = STATIC_DIR / "index.html"
    if index_file.is_file():
        return FileResponse(
            index_file,
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    return {"message": "Voice Clone Studio Mini App API is running."}


@webapp_app.get("/health")
async def health_check():
    return {"status": "ok", "app": "Voice Clone Pro & Recap Studio"}

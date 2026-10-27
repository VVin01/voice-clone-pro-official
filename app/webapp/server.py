"""
server.py - FastAPI Web Service for Unified Telegram Mini App.
Hosts frontend Single Page Application (SPA) and REST API endpoints.
"""

from __future__ import annotations

import os
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

from app.db.session import init_db
from app.webapp.api import api_router


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

    # Initialize database tables and metadata
    try:
        await init_db()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Error during webapp DB init: %s", e)

    # Initial 24h cleanup on startup
    import asyncio
    try:
        from app.webapp.api import cleanup_expired_jobs_24h
        await cleanup_expired_jobs_24h()
    except Exception as cl_err:
        import logging
        logging.getLogger(__name__).warning("Initial 24h cleanup error: %s", cl_err)

    # Recover any orphaned RUNNING recap jobs left behind by previous deploys/restarts
    try:
        from app.models.schema import Job
        from app.models.enums import JobStatus, JobType
        from app.db.session import AsyncSessionLocal
        from sqlalchemy import update
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(Job)
                .where(
                    Job.status == JobStatus.RUNNING.value,
                    (Job.job_type == JobType.RECAP.value) | (Job.id.startswith("rc_")) | (Job.engine == "recap")
                )
                .values(status=JobStatus.QUEUED.value, started_at=None)
            )
            await session.commit()
    except Exception as rec_err:
        pass

    # Periodic background task every 30 minutes
    async def _periodic_cleanup():
        while True:
            await asyncio.sleep(1800)  # 30 mins
            try:
                from app.webapp.api import cleanup_expired_jobs_24h
                await cleanup_expired_jobs_24h()
            except Exception:
                pass

    # Background Movie Recap queue processor loop
    async def _recap_queue_worker():
        """Continuously check for any QUEUED Movie Recap jobs in DB and auto-process them."""
        import logging
        from app.models.schema import Job, User
        from app.models.enums import JobStatus, JobType
        from app.db.session import AsyncSessionLocal
        from app.webapp.api import _run_recap_background_job, RecapConfigRequest, RECAP_JOBS_REGISTRY
        from sqlalchemy import select

        _log = logging.getLogger("RecapWorker")
        while True:
            try:
                async with AsyncSessionLocal() as session:
                    stmt = (
                        select(Job)
                        .where(
                            Job.status == JobStatus.QUEUED.value,
                            (Job.job_type == JobType.RECAP.value) | (Job.id.startswith("rc_")) | (Job.engine == "recap")
                        )
                        .order_by(Job.created_at)
                        .limit(1)
                    )
                    res = await session.execute(stmt)
                    queued_recap = res.scalar_one_or_none()
                    if queued_recap:
                        jid = str(queued_recap.id)
                        mem_entry = RECAP_JOBS_REGISTRY.get(jid)
                        if not mem_entry or mem_entry.get("status") not in ("RUNNING", "QUEUED"):
                            _log.info(f"Auto-dispatching queued recap job {jid}...")
                            from app.config import settings
                            from app.models.enums import AccountCategory
                            user = await session.get(User, queued_recap.user_id)
                            is_vip = ((user.account_category == AccountCategory.PURCHASED.value) if user else False) or settings.is_admin(queued_recap.user_id)
                            source_url = getattr(queued_recap, "source_audio_path", None) or queued_recap.input_text or ""
                            cfg = RecapConfigRequest(
                                url=source_url,
                                title=queued_recap.input_text or "Movie Recap Video",
                                voice_slug=queued_recap.preset_voice or "komin",
                            )
                            import time
                            RECAP_JOBS_REGISTRY[jid] = {
                                "id": jid,
                                "user_id": queued_recap.user_id,
                                "title": queued_recap.input_text or "Movie Recap Video",
                                "status": "QUEUED",
                                "progress_percent": 5,
                                "progress_step": "0/4",
                                "progress_label": "⏳ Picking up from queue...",
                                "created_at": time.time(),
                                "video_path": None,
                                "audio_path": None,
                                "srt_path": None,
                                "script_path": None,
                                "download_urls": {},
                                "error_message": None,
                            }
                            asyncio.create_task(
                                _run_recap_background_job(
                                    jid,
                                    cfg,
                                    queued_recap.user_id,
                                    queued_recap.chat_id or queued_recap.user_id,
                                    is_vip=is_vip,
                                )
                            )
            except Exception:
                pass
            await asyncio.sleep(3)

    cleanup_task = asyncio.create_task(_periodic_cleanup())
    recap_worker_task = asyncio.create_task(_recap_queue_worker())
    yield
    cleanup_task.cancel()
    recap_worker_task.cancel()


webapp_app = FastAPI(
    title="Voice Clone Pro & Recap Studio Mini App",
    description="Unified Telegram Mini App for Voice Cloning and AI Movie Recap Generation",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS Middleware for Telegram Webview compatibility
webapp_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount REST API
webapp_app.include_router(api_router)

# Static files path
STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
(STATIC_DIR / "css").mkdir(parents=True, exist_ok=True)
(STATIC_DIR / "js").mkdir(parents=True, exist_ok=True)

# Mount static assets
webapp_app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@webapp_app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    """Ensure Telegram Webview and browsers never serve stale cached SPA assets."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
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

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

    # Periodic background task every 30 minutes
    async def _periodic_cleanup():
        while True:
            await asyncio.sleep(1800)  # 30 mins
            try:
                from app.webapp.api import cleanup_expired_jobs_24h
                await cleanup_expired_jobs_24h()
            except Exception:
                pass

    cleanup_task = asyncio.create_task(_periodic_cleanup())
    yield
    cleanup_task.cancel()


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

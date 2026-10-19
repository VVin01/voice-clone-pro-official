"""
server.py - FastAPI Web Service for Unified Telegram Mini App.
Hosts frontend Single Page Application (SPA) and REST API endpoints.
"""

from __future__ import annotations

import os
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
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
    yield


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


@webapp_app.get("/")
async def serve_index():
    """Serve the Mini App main SPA interface."""
    index_file = STATIC_DIR / "index.html"
    if index_file.is_file():
        return FileResponse(index_file)
    return {"message": "Voice Clone Studio Mini App API is running."}


@webapp_app.get("/health")
async def health_check():
    return {"status": "ok", "app": "Voice Clone Pro & Recap Studio"}

"""
gemini_recap.py - Shared Gemini model resolution for the legacy recap helpers.

Only ``_resolve_primary_model`` remains: the TikTok recap generator
that used to live here (``generate_tiktok_recap_and_srt`` and its
``get_gemini_api_key`` helper) was dead code — its only callers were
MediaDownloader.generate_viral_recap*, removed in batch N — while the
model resolution is still imported by the test suite and kept in step
with the recap planner.
"""

import os
import logging

logger = logging.getLogger(__name__)


def _resolve_primary_model() -> str:
    """The operator-configured primary Gemini model for this module.

    Unified with the recap planner's resolution (hunt #3 A5): this
    module used to read only the GEMINI_MODEL env var — a name
    nothing sets — so the configured primary never applied here.
    Precedence mirrors recap_planner: dynamic setting →
    GEMINI_PRIMARY_MODEL → GEMINI_TEXT_MODEL → legacy GEMINI_MODEL.
    Retired/slow generations (2.5 / 3.8) are rejected, as before.
    """
    raw = ""
    try:
        from app.config import settings

        raw = str(getattr(settings, "gemini_primary_model", "") or "").strip()
    except Exception:
        raw = ""
    if not raw:
        raw = (
            os.environ.get("GEMINI_PRIMARY_MODEL", "").strip()
            or os.environ.get("GEMINI_TEXT_MODEL", "").strip()
            or os.environ.get("GEMINI_MODEL", "").strip()
        )
    if raw and "2.5" not in raw and "3.8" not in raw:
        return raw
    return ""

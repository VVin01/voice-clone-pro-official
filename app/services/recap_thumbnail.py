"""AI Thumbnail Generator for completed Recap jobs (Paid + Admin perk).

Uses the Hugging Face PRO account's included monthly compute credits via
HF Inference Providers (`black-forest-labs/FLUX.1-schnell` by default,
verified warm on `fal-ai` and `wavespeed` at $0.003 / MP -> $0.003 per
720x1280 image), with a hard server-side monthly USD cap (default $1.80),
text-free base image generation, and local Pillow/Padauk/RAQM Burmese
headline + VoiceClonePro brand logo compositing onto a 1080x1920 (9:16)
vertical cover.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, features
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.base import Base
from app.models.enums import AccountCategory
from app.models.schema import RecapThumbnailPoolUsage, User

logger = logging.getLogger(__name__)

DEFAULT_THUMBNAIL_MODEL = "black-forest-labs/FLUX.1-schnell"
DEFAULT_THUMBNAIL_MONTHLY_CAP_USD = 1.80
DEFAULT_THUMBNAIL_COST_PER_IMAGE_USD = 0.003
DEFAULT_THUMBNAIL_PROMPT_TEMPLATE = (
    "Cinematic vertical 9:16 movie poster artwork inspired by {title}, "
    "dramatic lighting, rich atmospheric colors, ultra detailed, high contrast, "
    "expressive composition, strictly no text, no letters, no words, no numbers, "
    "no watermark, no typography, clean text-free illustration"
)
NO_TEXT_SAFETY_SUFFIX = (
    ", strictly no text, no letters, no words, no numbers, "
    "no watermark, no typography, clean text-free illustration"
)
BRAIN_REQUIRED_SUFFIX = "no text, no letters, no words, no logo, no watermark"

BRAIN_PROMPT_TEMPLATE = """You are a master Myanmar movie recap YouTube producer and poster designer.
Write a click-worthy Burmese cover headline and an English visual scene prompt for a 9:16 vertical poster artwork for this story.

Input:
Source Title: {title}
Story Script:
{script}

Output format:
Return ONLY strict JSON with exactly these two keys:
{{
  "headline_my": "line1\\nline2",
  "image_prompt_en": "..."
}}

Rules for "headline_my":
1. Spoken, dramatic Burmese, EXACTLY 2 lines separated by \\n.
2. Line 1: Setup (maximum 18 characters).
3. Line 2: Punchline (maximum 12 characters).
4. Must create a powerful curiosity gap (a shocking secret exposed, an unbelievable moment, a suspenseful question, or an unfinished claim).
5. Approved example:
   "မိန်းမရဲ့ လျှို့ဝှက်ချက်\\nပေါ်သွားပြီ"
6. STRICTLY FORBIDDEN:
   - NO digits (neither 0-9 nor Myanmar 0-9).
   - NO channel names, broadcaster names, or brand watermarks.
   - NO episode numbers or part numbers.
   - NO abstract noun label pairs (e.g. DO NOT write "ကင်ဆာလျှို့ဝှက် / ဖောက်ပြန်မှု").
   - NO copying or transliterating the source title.

Rules for "image_prompt_en":
1. English, describing ONE cinematic vertical 9:16 movie poster scene.
2. Focus on the single most dramatic visual climax from the story with concrete characters, facial expressions, setting, dramatic lighting, rich cinematic mood.
3. Clean text-free prompt: DO NOT include any channel names, actor names, movie titles, or instructions to write text.
4. MUST end with: "no text, no letters, no words, no logo, no watermark".
"""

METALLIC_PALETTES: dict[str, dict[str, tuple[tuple[int, int, int], ...]]] = {
    "warm": {
        "line1": ((255, 244, 190), (247, 197, 66), (176, 118, 10)),   # gold
        "line2": ((255, 214, 170), (232, 64, 40), (128, 8, 4)),       # crimson
    },
    "cold": {
        "line1": ((255, 255, 255), (214, 220, 228), (120, 128, 140)), # silver
        "line2": ((224, 246, 255), (56, 165, 245), (6, 42, 120)),     # ice blue
    },
    "green": {
        "line1": ((255, 246, 200), (240, 205, 90), (160, 120, 20)),   # pale gold
        "line2": ((210, 255, 225), (46, 190, 110), (6, 90, 50)),      # emerald
    },
    "dark_neutral": {
        "line1": ((255, 244, 190), (247, 197, 66), (176, 118, 10)),   # gold
        "line2": ((255, 214, 170), (232, 64, 40), (128, 8, 4)),       # crimson
    },
}

THUMBNAIL_MONTHLY_CAP_MESSAGE_MY = "ဒီလအတွက် AI Thumbnail အရေအတွက် ပြည့်သွားပါပြီ"
THUMBNAIL_MONTHLY_CAP_MESSAGE_EN = (
    "This month's AI Thumbnail pool has been reached (resets on the 1st of next month)."
)

HEADLINE_MAX_CHARS = 120
THUMBNAIL_WIDTH = 1080
THUMBNAIL_HEIGHT = 1920
GEN_WIDTH = 720
GEN_HEIGHT = 1280

# Per-image price lookup (USD) at 720x1280 (0.92 MP <= 1 MP) on HF Inference Providers.
MODEL_PRICE_PER_IMAGE_USD: dict[str, float] = {
    "black-forest-labs/flux.1-schnell": 0.003,
    "black-forest-labs/flux.1-dev": 0.025,
    "black-forest-labs/flux.1-krea-dev": 0.025,
}


class ThumbnailError(Exception):
    """Base error for recap AI thumbnail operations."""


class ThumbnailTierError(ThumbnailError):
    """Raised when a FREE-tier account attempts to generate an AI thumbnail."""


class ThumbnailCapReachedError(ThumbnailError):
    """Raised when the monthly USD cap is reached or HF credits are exhausted."""

    def __init__(
        self,
        message: str = THUMBNAIL_MONTHLY_CAP_MESSAGE_MY,
        *,
        period: str = "",
        spend_usd: float = 0.0,
        cap_usd: float = DEFAULT_THUMBNAIL_MONTHLY_CAP_USD,
    ) -> None:
        super().__init__(message)
        self.period = period
        self.spend_usd = spend_usd
        self.cap_usd = cap_usd


class ThumbnailGenerationError(ThumbnailError):
    """Raised when image generation fails without exhausting the monthly pool."""


def current_utc_period(now: datetime | None = None) -> str:
    dt = now or datetime.now(timezone.utc)
    return dt.strftime("%Y-%m")


def is_user_eligible_for_thumbnail(user: User | None) -> bool:
    """Only PAID (PURCHASED) accounts and admins may generate AI thumbnails."""
    if user is None:
        return False
    if settings.is_admin(user.id):
        return True
    return getattr(user, "account_category", None) == AccountCategory.PURCHASED.value


def get_thumbnail_monthly_cap_usd() -> float:
    raw = getattr(settings, "recap_thumbnail_monthly_cap_usd", DEFAULT_THUMBNAIL_MONTHLY_CAP_USD)
    try:
        val = float(raw)
    except (TypeError, ValueError):
        val = DEFAULT_THUMBNAIL_MONTHLY_CAP_USD
    return max(0.0, round(val, 4))


def get_thumbnail_model() -> str:
    raw = str(getattr(settings, "recap_thumbnail_model", "") or "").strip()
    return raw or DEFAULT_THUMBNAIL_MODEL


def get_thumbnail_prompt_template() -> str:
    raw = str(getattr(settings, "recap_thumbnail_prompt_template", "") or "").strip()
    return raw or DEFAULT_THUMBNAIL_PROMPT_TEMPLATE


def estimate_image_cost_usd(model_name: str | None = None) -> float:
    key = (model_name or get_thumbnail_model()).strip().lower()
    return float(MODEL_PRICE_PER_IMAGE_USD.get(key, DEFAULT_THUMBNAIL_COST_PER_IMAGE_USD))


def sanitize_headline_text(text: str | None, max_chars: int = HEADLINE_MAX_CHARS) -> str:
    """Clean and bound user- or brain-supplied headline text. Empty string = logo-only cover.
    Preserves up to 2 lines separated by newline.
    """
    if text is None:
        return ""
    normalized = unicodedata.normalize("NFC", str(text))
    raw_lines = re.split(r"[\r\n]+", normalized)
    clean_lines: list[str] = []
    for ln in raw_lines:
        c = re.sub(r"[ \t]+", " ", ln).strip()
        if c:
            clean_lines.append(c)
    if not clean_lines:
        return ""
    clean_lines = clean_lines[:2]
    res = "\n".join(clean_lines)
    if max_chars > 0 and len(res) > max_chars:
        res = res[:max_chars].rstrip()
    return res


def sanitize_title_for_brain(title: str | None) -> str:
    """Sanitize source title before brain prompt by stripping channel/brand names,
    bracketed tags, URLs, episode numbers, and watermarks."""
    if not title:
        return "dramatic cinematic story"
    s = str(title).strip()
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"\[.*?\]", "", s)
    s = re.sub(r"\((?i:[^)]*(official|episode|ep|hd|eng\s*sub|sub|part)[^)]*)\)", "", s)
    patterns = [
        r"(?i)\b(ary\s*digital|drama\s*central|netflix|hbo|disney\s*\+|amazon\s*prime|hulu|paramount\s*plus|paramount|sony\s*liv|zee\s*tv|hum\s*tv|geo\s*tv|har\s*pal\s*geo|colors\s*tv|star\s*plus|youtube|channel)\b",
        r"(?i)\b(official\s*(channel|video|hd|trailer|teaser)?|full\s*(movie|episode|video)?|eng\s*sub|hd|4k|1080p|trailer|teaser)\b",
        r"(?i)\b(episode|ep|part|season|အပိုင်း)\s*[\d\u1040-\u1049]+",
    ]
    for p in patterns:
        s = re.sub(p, " ", s)
    s = re.sub(r"\b\d+\b", "", s)
    s = re.sub(r"[\s\|\-\–\—\:\,\.]+$", "", s)
    s = re.sub(r"^[\s\|\-\–\—\:\,\.]+", "", s)
    s = re.sub(r"\s*[\-\|\–\—]\s*", " - ", s)
    s = re.sub(r"[\s\-\|\–\—]+$", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s or s.lower() in ("movie recap video", "recap", "video", "movie", "dramatic story"):
        return "dramatic cinematic story"
    return s[:120].strip()


def validate_brain_response(data: Any, source_title: str = "") -> dict[str, str] | None:
    """Validate structured JSON response from the Brain for recap thumbnail.
    Enforces Burmese 2-line shape, setup/punch length limits, no digits, no abstract slash pairs,
    no channel names, and safety suffix on image prompt.
    Returns sanitized dict with 'headline_my' and 'image_prompt_en', or None on rejection.
    """
    if not isinstance(data, dict):
        return None
    raw_headline = str(data.get("headline_my") or "").strip()
    raw_prompt = str(data.get("image_prompt_en") or "").strip()
    if not raw_headline or not raw_prompt:
        return None

    # 1. Headline validation
    lines = [ln.strip() for ln in re.split(r"[\r\n]+", raw_headline) if ln.strip()]
    if len(lines) != 2:
        return None
    line1, line2 = lines[0], lines[1]

    # No digits (ASCII or Myanmar)
    if re.search(r"[\d\u1040-\u1049]", raw_headline):
        return None

    # Forbidden: abstract noun label pairs (slashes/pipes)
    if any(sep in raw_headline for sep in ("/", "\\", "|")):
        return None

    # Must contain Myanmar characters
    if not re.search(r"[\u1000-\u109f]", raw_headline):
        return None

    # Length limits: line1 <= 24 (setup ~18), line2 <= 18 (punch ~12)
    if len(line1) > 24 or len(line2) > 18:
        return None

    # Channel / brand check
    channel_kw = ("ary digital", "netflix", "hbo", "disney", "amazon prime", "youtube", "channel", "trailer")
    headline_lower = raw_headline.lower()
    if any(k in headline_lower for k in channel_kw):
        return None

    # Must not copy source title directly
    clean_title_lower = source_title.strip().lower()
    if clean_title_lower and clean_title_lower in headline_lower:
        return None

    # 2. Image prompt validation
    if not re.search(r"[A-Za-z]", raw_prompt) or len(raw_prompt) < 15:
        return None
    prompt_clean = raw_prompt.strip().rstrip(" ,.;")
    if BRAIN_REQUIRED_SUFFIX not in prompt_clean.lower():
        prompt_clean = f"{prompt_clean}, {BRAIN_REQUIRED_SUFFIX}"

    return {
        "headline_my": f"{line1}\n{line2}",
        "image_prompt_en": prompt_clean,
    }


def load_recap_narration_script(job_id: str, job_dir: Path | None = None) -> str:
    """Load the final Burmese narration script for a completed recap job.
    Searches smart_sync_plan.json or script text files in the job directory.
    """
    candidate_paths: list[Path] = []
    if job_dir is not None:
        p_dir = Path(job_dir)
        candidate_paths.extend([
            p_dir / "smart_sync_plan.json",
            p_dir / "workspace" / "smart_sync_plan.json",
            p_dir / "narration.txt",
            p_dir / "workspace" / "narration.txt",
        ])
    storage_root = getattr(settings, "storage_root", Path("storage"))
    recap_dir = storage_root / "recap_output" / job_id
    candidate_paths.extend([
        recap_dir / "smart_sync_plan.json",
        recap_dir / "workspace" / "smart_sync_plan.json",
        recap_dir / "narration.txt",
        recap_dir / "workspace" / "narration.txt",
    ])

    for cand in candidate_paths:
        if not cand.is_file():
            continue
        try:
            if cand.suffix.lower() == ".json":
                data = json.loads(cand.read_text(encoding="utf-8"))
                segments = data.get("segments") or []
                lines: list[str] = []
                for seg in segments:
                    txt = str(seg.get("narration") or seg.get("translation") or seg.get("text") or "").strip()
                    if txt:
                        lines.append(txt)
                if lines:
                    return " ".join(lines)
            elif cand.suffix.lower() == ".txt":
                text = cand.read_text(encoding="utf-8").strip()
                if text:
                    return text
        except Exception as exc:
            logger.debug("Failed reading script from %s: %s", cand, exc)
    return ""


def call_brain_for_thumbnail(
    narration_script: str,
    source_title: str,
    *,
    gemini_call: Callable[[str], Any] | None = None,
) -> dict[str, str] | None:
    """Make 1 extra Gemini text call (using existing planner model chain / key pool)
    to write a Burmese headline and English 9:16 image prompt.
    Returns validated dict or None on any failure/empty fallback.
    """
    clean_title = sanitize_title_for_brain(source_title)
    clean_script = str(narration_script or "").strip()
    if not clean_script and not clean_title:
        return None

    # Truncate script to first 5000 chars for prompt economy
    truncated_script = clean_script[:5000].strip() if clean_script else "N/A"
    prompt = BRAIN_PROMPT_TEMPLATE.format(title=clean_title, script=truncated_script)

    try:
        if gemini_call is not None:
            raw_res = gemini_call(prompt)
            if isinstance(raw_res, str):
                try:
                    raw_res = json.loads(raw_res)
                except Exception:
                    m = re.search(r"\{.*\}", raw_res, re.DOTALL)
                    raw_res = json.loads(m.group(0)) if m else {}
        else:
            from app.services.recap_engine.recap_planner import (
                _collect_gemini_key_pool,
                _configure_gemini_key_pool,
                _gemini_json,
            )
            from app.services.recap_engine.services import ai_service as ai_mod

            key_pool = _collect_gemini_key_pool(dict(os.environ))
            if not key_pool:
                logger.warning("No Gemini API keys available for brain thumbnail call")
                return None
            if not os.environ.get("GEMINI_API_KEY"):
                os.environ["GEMINI_API_KEY"] = key_pool[0][1]

            ai = ai_mod.AIService()
            _configure_gemini_key_pool(
                ai,
                key_pool,
                client_factory=ai_mod.genai.Client,
                log=logger.info,
            )
            raw_res = _gemini_json(ai, prompt, retries=1)

        return validate_brain_response(raw_res, source_title=clean_title)
    except Exception as exc:
        logger.warning("Brain thumbnail call failed, falling back to v1 prompt template: %s", exc)
        return None


def _sanitize_prompt_subject(title: str | None, genre: str | None = None) -> str:
    raw = str(title or "").strip()
    # Strip URLs if the raw job input was a link
    raw = re.sub(r"https?://\S+", "", raw).strip()
    raw = re.sub(r"[\r\n\t]+", " ", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw or raw.lower() in ("movie recap video", "recap", "video"):
        raw = "dramatic cinematic story"
    if len(raw) > 160:
        raw = raw[:160].rstrip()
    genre_clean = str(genre or "").strip()
    if genre_clean and genre_clean.lower() not in raw.lower():
        raw = f"{raw} ({genre_clean})"
    return raw


def build_thumbnail_prompt(
    title: str | None,
    genre: str | None = None,
    template: str | None = None,
) -> str:
    """Build the text-to-image prompt from the admin template and recap title/genre.

    Always enforces the text-free guard so the image model never attempts to
    render Burmese or Latin characters into the base artwork.
    """
    subject = _sanitize_prompt_subject(title, genre=genre)
    tpl = (template if template is not None else get_thumbnail_prompt_template()).strip()
    if not tpl:
        tpl = DEFAULT_THUMBNAIL_PROMPT_TEMPLATE

    if "{title}" in tpl:
        prompt = tpl.replace("{title}", subject)
    else:
        prompt = f"{tpl} — {subject}"

    lower = prompt.lower()
    if "no text" not in lower or "no letters" not in lower:
        prompt = prompt.rstrip(" ,.") + NO_TEXT_SAFETY_SUFFIX
    return prompt


def resolve_pro_hf_token() -> str | None:
    """Resolve the existing Hugging Face PRO (reserve) token from env/settings.

    Order of precedence:
    1. First designated reserve token from `HF_RESERVE_TOKENS` (the PRO account
       where the $2/month compute credits live).
    2. `settings.hf_token` / `HF_TOKEN` env var.
    3. Last token in `get_available_hf_tokens()` (positional reserve).
    """
    from app.services.voxcpm_engine import VoxCPMEngine, get_available_hf_tokens

    raw_reserve = os.getenv("HF_RESERVE_TOKENS", "") or ""
    for part in re.split(r"[,;\s\n]+", raw_reserve):
        tok = part.strip().strip("'\"")
        if tok:
            return tok

    designated = VoxCPMEngine._designated_reserve_tokens()
    if designated:
        return sorted(designated)[0]

    direct = (getattr(settings, "hf_token", "") or os.getenv("HF_TOKEN", "") or "").strip().strip("'\"")
    if direct:
        return direct

    pool = [t for t in get_available_hf_tokens() if t]
    if pool:
        return pool[-1]
    return None


def is_hf_insufficient_credit_error(exc: BaseException) -> bool:
    """Return True if an exception from HF Inference Providers indicates
    exhausted credits / HTTP 402 / quota depletion."""
    status_code = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if status_code == 402:
        return True
    msg = str(exc or "").lower()
    credit_markers = (
        "402",
        "payment required",
        "insufficient credit",
        "insufficient_credits",
        "insufficient balance",
        "out of credit",
        "credits have been exhausted",
        "monthly included credits",
        "exceeded your monthly",
        "prepaid credits",
        "quota exceeded",
        "free tier limit",
        "billing",
    )
    return any(marker in msg for marker in credit_markers)


async def ensure_thumbnail_pool_table(session: AsyncSession) -> None:
    """Ensure `recap_thumbnail_pool_usage` table exists (safe idempotent check)."""
    try:
        bind = session.get_bind()
        if bind is not None:
            async with bind.begin() as conn:
                await conn.run_sync(
                    lambda sync_conn: Base.metadata.create_all(
                        sync_conn,
                        tables=[RecapThumbnailPoolUsage.__table__],
                        checkfirst=True,
                    )
                )
    except Exception as exc:
        logger.debug("ensure_thumbnail_pool_table skipped/failed: %s", exc)


async def get_monthly_thumbnail_usage(
    session: AsyncSession,
    period: str | None = None,
) -> dict[str, Any]:
    """Return current month's AI thumbnail counter, spend, cap, and cap-reached status."""
    await ensure_thumbnail_pool_table(session)
    target_period = period or current_utc_period()
    row = (
        await session.execute(
            select(RecapThumbnailPoolUsage).where(RecapThumbnailPoolUsage.period == target_period)
        )
    ).scalar_one_or_none()

    count = int(row.generations_count or 0) if row is not None else 0
    spend = float(row.estimated_spend_usd or 0.0) if row is not None else 0.0
    exhausted = bool(row.exhausted) if row is not None else False

    cap = get_thumbnail_monthly_cap_usd()
    model_name = get_thumbnail_model()
    cost_per_img = estimate_image_cost_usd(model_name)

    cap_reached = (
        exhausted
        or cap <= 0.0
        or round(spend, 6) >= round(cap, 6)
        or round(spend + cost_per_img, 6) > round(cap, 6)
    )
    return {
        "period": target_period,
        "generations_count": count,
        "estimated_spend_usd": round(spend, 4),
        "monthly_cap_usd": round(cap, 2),
        "exhausted": exhausted,
        "cap_reached": bool(cap_reached),
        "model": model_name,
        "prompt_template": get_thumbnail_prompt_template(),
        "cost_per_image_usd": round(cost_per_img, 4),
    }


async def assert_monthly_thumbnail_budget_available(
    session: AsyncSession,
    period: str | None = None,
) -> dict[str, Any]:
    """Verify the monthly cap has not been reached; raise ThumbnailCapReachedError if full."""
    usage = await get_monthly_thumbnail_usage(session, period=period)
    if usage["cap_reached"]:
        raise ThumbnailCapReachedError(
            THUMBNAIL_MONTHLY_CAP_MESSAGE_MY,
            period=usage["period"],
            spend_usd=usage["estimated_spend_usd"],
            cap_usd=usage["monthly_cap_usd"],
        )
    return usage


async def record_thumbnail_generation_success(
    session: AsyncSession,
    cost_usd: float | None = None,
    period: str | None = None,
) -> dict[str, Any]:
    """Increment the current month's generation count and estimated spend after a
    billed HF generation succeeds."""
    await ensure_thumbnail_pool_table(session)
    target_period = period or current_utc_period()
    price = float(cost_usd if cost_usd is not None else estimate_image_cost_usd())

    row = (
        await session.execute(
            select(RecapThumbnailPoolUsage).where(RecapThumbnailPoolUsage.period == target_period)
        )
    ).scalar_one_or_none()
    if row is None:
        row = RecapThumbnailPoolUsage(
            period=target_period,
            generations_count=0,
            estimated_spend_usd=0.0,
            exhausted=False,
        )
        session.add(row)

    row.generations_count = int(row.generations_count or 0) + 1
    row.estimated_spend_usd = round(float(row.estimated_spend_usd or 0.0) + max(0.0, price), 6)
    cap = get_thumbnail_monthly_cap_usd()
    if round(row.estimated_spend_usd + price, 6) > round(cap, 6) or round(row.estimated_spend_usd, 6) >= round(cap, 6):
        # Soft-note: cap_reached will evaluate True on next check
        pass
    await session.flush()
    return await get_monthly_thumbnail_usage(session, period=target_period)


async def mark_thumbnail_pool_exhausted(
    session: AsyncSession,
    period: str | None = None,
) -> dict[str, Any]:
    """Mark the current month's pool as exhausted when HF returns an
    insufficient-credit / 402 error (without adding phantom spend)."""
    await ensure_thumbnail_pool_table(session)
    target_period = period or current_utc_period()
    row = (
        await session.execute(
            select(RecapThumbnailPoolUsage).where(RecapThumbnailPoolUsage.period == target_period)
        )
    ).scalar_one_or_none()
    if row is None:
        row = RecapThumbnailPoolUsage(
            period=target_period,
            generations_count=0,
            estimated_spend_usd=0.0,
            exhausted=True,
        )
        session.add(row)
    else:
        row.exhausted = True
    await session.flush()
    return await get_monthly_thumbnail_usage(session, period=target_period)


def call_hf_text_to_image(
    prompt: str,
    *,
    model_name: str | None = None,
    token: str | None = None,
    width: int = GEN_WIDTH,
    height: int = GEN_HEIGHT,
    client_factory: Callable[..., Any] | None = None,
) -> Image.Image:
    """Call Hugging Face Inference Providers (`provider="auto"`) to generate a
    vertical base image with the PRO reserve token."""
    resolved_token = token or resolve_pro_hf_token()
    if not resolved_token:
        raise ThumbnailGenerationError("HF PRO token is not configured")

    resolved_model = (model_name or get_thumbnail_model()).strip() or DEFAULT_THUMBNAIL_MODEL

    if client_factory is not None:
        client = client_factory(provider="auto", token=resolved_token, timeout=60)
    else:
        from huggingface_hub import InferenceClient

        client = InferenceClient(provider="auto", token=resolved_token, timeout=60)

    img = client.text_to_image(
        prompt,
        model=resolved_model,
        width=int(width),
        height=int(height),
    )
    if not isinstance(img, Image.Image):
        raise ThumbnailGenerationError("HF Inference Provider did not return a valid image")
    return img


# ── Brand Logo & Burmese Headline Compositing ────────────────────────

def _brand_logo_svg_path() -> Path:
    return Path(__file__).resolve().parent.parent / "webapp" / "static" / "img" / "brand-logo.svg"


def load_brand_logo_image(svg_path: Path | None = None) -> Image.Image:
    """Decode the VoiceClonePro brand logo from `app/webapp/static/img/brand-logo.svg`
    (which embeds a 192x192 base64 JPEG raster) into an RGBA PIL Image."""
    target = Path(svg_path) if svg_path is not None else _brand_logo_svg_path()
    if target.is_file():
        if target.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            with Image.open(target) as im:
                return im.convert("RGBA")
        raw_svg = target.read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"data:image/[^;]+;base64,([A-Za-z0-9+/=\s]+)", raw_svg)
        if m:
            raw_bytes = base64.b64decode(re.sub(r"\s+", "", m.group(1)))
            with Image.open(io.BytesIO(raw_bytes)) as im:
                return im.convert("RGBA")

    # Fallback programmatic crest if SVG was moved in an isolated test
    fallback = Image.new("RGBA", (192, 192), (18, 22, 36, 255))
    draw = ImageDraw.Draw(fallback)
    draw.rounded_rectangle([4, 4, 187, 187], radius=36, outline=(255, 215, 0, 255), width=6)
    return fallback


def _composite_corner_logo(canvas: Image.Image, logo_img: Image.Image) -> None:
    """Composite the VoiceClonePro brand logo into the top-right corner of the
    1080x1920 canvas with a rounded mask, dark halo, and subtle gold border."""
    cw, _ch = canvas.size
    logo_size = max(84, int(round(cw * 0.115)))  # ~124 px on 1080w
    margin = max(28, int(round(cw * 0.038)))     # ~41 px margin
    radius = max(16, int(round(logo_size * 0.22)))

    resized_logo = logo_img.convert("RGBA").resize(
        (logo_size, logo_size), Image.Resampling.LANCZOS
    )

    # Rounded corner alpha mask for the logo
    mask = Image.new("L", (logo_size, logo_size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle([0, 0, logo_size - 1, logo_size - 1], radius=radius, fill=255)

    # Create badge container with soft shadow + gold ring
    pad = 12
    badge_w = logo_size + pad * 2
    badge_h = logo_size + pad * 2
    badge = Image.new("RGBA", (badge_w, badge_h), (0, 0, 0, 0))
    b_draw = ImageDraw.Draw(badge)
    b_draw.rounded_rectangle(
        [2, 4, badge_w - 3, badge_h - 1],
        radius=radius + 4,
        fill=(0, 0, 0, 150),
    )
    badge = badge.filter(ImageFilter.GaussianBlur(radius=4))

    badge.paste(resized_logo, (pad, pad), mask)
    ring_draw = ImageDraw.Draw(badge)
    ring_draw.rounded_rectangle(
        [pad, pad, pad + logo_size - 1, pad + logo_size - 1],
        radius=radius,
        outline=(255, 215, 0, 220),
        width=3,
    )

    dest_x = cw - margin - logo_size - pad
    dest_y = margin - pad // 2
    canvas.alpha_composite(badge, (max(0, dest_x), max(0, dest_y)))


def _resolve_noto_serif_black_path() -> Path:
    return Path(__file__).resolve().parent.parent / "assets" / "fonts" / "NotoSerifMyanmar-Black.ttf"


def _get_noto_serif_black_font(size_px: int) -> ImageFont.FreeTypeFont:
    font_path = _resolve_noto_serif_black_path()
    layout = ImageFont.Layout.RAQM if features.check("raqm") else ImageFont.Layout.BASIC
    try:
        return ImageFont.truetype(str(font_path), size=size_px, layout_engine=layout)
    except Exception:
        return ImageFont.truetype(str(font_path), size=size_px)


def _fit_line_font(
    line: str,
    max_w: int = 1035,
    max_px: int = 140,
    min_px: int = 44,
) -> tuple[ImageFont.FreeTypeFont, int]:
    """Auto-fit font size for a single headline line independently to <= max_w px."""
    low = min_px
    high = max_px
    best_size = min_px
    best_font = _get_noto_serif_black_font(min_px)
    dummy = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    draw = ImageDraw.Draw(dummy)

    while low <= high:
        mid = (low + high) // 2
        f = _get_noto_serif_black_font(mid)
        stroke = max(3, round(mid / 16))
        bbox = draw.textbbox((0, 0), line, font=f, stroke_width=stroke)
        w = bbox[2] - bbox[0]
        if w <= max_w:
            best_size = mid
            best_font = f
            low = mid + 1
        else:
            high = mid - 1
    return best_font, best_size


def analyze_palette(image: Image.Image) -> dict[str, Any]:
    """Analyse base image lower-third for dominant hue and mean luminance.
    Returns palette dict with family, line1 stops, line2 stops, mean_luminance, and deepen_contrast.
    """
    w, h = image.size
    crop_box = (0, int(h * 0.60), w, h)
    lower = image.crop(crop_box).convert("RGB")
    if lower.size[0] == 0 or lower.size[1] == 0:
        lower = image.convert("RGB")

    thumb = lower.resize((120, 80), Image.Resampling.BILINEAR)
    arr = np.asarray(thumb, dtype=np.float32)

    lum = 0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1] + 0.114 * arr[:, :, 2]
    mean_lum = float(np.mean(lum))
    deepen_contrast = mean_lum > 150.0

    r = arr[:, :, 0] / 255.0
    g = arr[:, :, 1] / 255.0
    b = arr[:, :, 2] / 255.0
    cmax = np.maximum(np.maximum(r, g), b)
    cmin = np.minimum(np.minimum(r, g), b)
    delta = cmax - cmin

    sat = np.where(cmax > 1e-4, delta / cmax, 0.0)
    mean_sat = float(np.mean(sat))

    if mean_sat < 0.15 or mean_lum < 35.0:
        family = "dark_neutral"
    else:
        valid = (sat > 0.12) & (cmax > 0.12)
        if not np.any(valid):
            family = "dark_neutral"
        else:
            h_deg = np.zeros_like(r)
            mask_r = valid & (cmax == r) & (delta > 1e-5)
            h_deg[mask_r] = (60.0 * ((g[mask_r] - b[mask_r]) / delta[mask_r]) + 360.0) % 360.0
            mask_g = valid & (cmax == g) & (delta > 1e-5)
            h_deg[mask_g] = (60.0 * ((b[mask_g] - r[mask_g]) / delta[mask_g]) + 120.0) % 360.0
            mask_b = valid & (cmax == b) & (delta > 1e-5)
            h_deg[mask_b] = (60.0 * ((r[mask_b] - g[mask_b]) / delta[mask_b]) + 240.0) % 360.0

            valid_hues = h_deg[valid]
            weights = sat[valid] * cmax[valid]
            hist, bin_edges = np.histogram(valid_hues, bins=36, range=(0.0, 360.0), weights=weights)
            dom_bin = int(np.argmax(hist))
            dom_hue = (bin_edges[dom_bin] + bin_edges[dom_bin + 1]) / 2.0

            if (15.0 <= dom_hue < 70.0) or (dom_hue >= 330.0) or (dom_hue < 15.0):
                family = "warm"
            elif 170.0 <= dom_hue <= 260.0:
                family = "cold"
            elif 70.0 <= dom_hue < 170.0:
                family = "green"
            else:
                family = "warm"

    pal = METALLIC_PALETTES[family]
    return {
        "family": family,
        "line1": pal["line1"],
        "line2": pal["line2"],
        "mean_luminance": round(mean_lum, 2),
        "deepen_contrast": deepen_contrast,
    }


def _make_metallic_texture(
    width: int,
    height: int,
    stops: tuple[tuple[int, int, int], ...],
) -> np.ndarray:
    """Generate 3-stop vertical gradient with specular highlight at ~32% and subtle noise."""
    c0 = np.array(stops[0], dtype=np.float32)
    c1 = np.array(stops[1], dtype=np.float32)
    c2 = np.array(stops[2], dtype=np.float32)

    v = np.linspace(0.0, 1.0, max(1, height), dtype=np.float32)[:, None, None]
    grad = np.where(
        v < 0.5,
        c0 + (c1 - c0) * (v / 0.5),
        c1 + (c2 - c1) * ((v - 0.5) / 0.5),
    )
    tex = np.tile(grad, (1, max(1, width), 1))

    # Specular highlight band at ~32% height
    hl_center = 0.32
    hl_width = 0.08
    hl_dist = np.abs(v - hl_center) / hl_width
    hl_mask = np.clip(1.0 - (hl_dist ** 2), 0.0, 1.0)
    specular = np.array([255.0, 255.0, 245.0], dtype=np.float32)
    tex = tex * (1.0 - 0.45 * hl_mask) + specular * (0.45 * hl_mask)

    # Fine deterministic noise
    rng = np.random.RandomState(42)
    noise = rng.uniform(-8.0, 8.0, size=(height, width, 1)).astype(np.float32)
    return np.clip(tex + noise, 0.0, 255.0).astype(np.uint8)


def _render_metallic_text_line(
    line_text: str,
    stops: tuple[tuple[int, int, int], ...],
    max_w: int = 1035,
) -> tuple[Image.Image, int, int, int]:
    """Render a single headline line adhering to the 6-layer metallic text stack:
    1. Drop shadow (Gaussian blur radius 8)
    2. Outer glow in the line's mid colour (Gaussian blur radius 10)
    3. Dark outline silhouette (stroke ≈ size/16, near-black brown)
    4. Bevel shade (+6 px, dark tone of line colour)
    5. Bevel light (−6 px, pale tone)
    6. Metallic texture fill through text mask (3-stop gradient + highlight + noise)
    Returns (layer_image, text_width, text_height, pad).
    """
    f, size = _fit_line_font(line_text, max_w=max_w)
    stroke_px = max(3, round(size / 16))
    dummy = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    d_dummy = ImageDraw.Draw(dummy)
    bbox = d_dummy.textbbox((0, 0), line_text, font=f, stroke_width=stroke_px)
    x_min, y_min, x_max, y_max = bbox
    text_w = x_max - x_min
    text_h = y_max - y_min
    pad = 36
    layer_w = text_w + pad * 2
    layer_h = text_h + pad * 2
    ox = pad - x_min
    oy = pad - y_min

    # 1. Drop shadow
    sh_img = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))
    sh_d = ImageDraw.Draw(sh_img)
    sh_d.text(
        (ox + 2, oy + 8),
        line_text,
        font=f,
        fill=(0, 0, 0, 210),
        stroke_width=stroke_px,
        stroke_fill=(0, 0, 0, 210),
    )
    sh_img = sh_img.filter(ImageFilter.GaussianBlur(radius=8))
    layer = sh_img

    # 2. Outer glow in the line's mid colour
    mr, mg, mb = stops[1]
    glow_img = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))
    g_d = ImageDraw.Draw(glow_img)
    g_d.text(
        (ox, oy),
        line_text,
        font=f,
        fill=(mr, mg, mb, 180),
        stroke_width=stroke_px + 8,
        stroke_fill=(mr, mg, mb, 180),
    )
    glow_img = glow_img.filter(ImageFilter.GaussianBlur(radius=10))
    layer = Image.alpha_composite(layer, glow_img)

    # 3. Dark outline silhouette (stroke ≈ size/16, near-black brown)
    out_img = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))
    o_d = ImageDraw.Draw(out_img)
    o_d.text(
        (ox, oy),
        line_text,
        font=f,
        fill=(20, 12, 10, 255),
        stroke_width=stroke_px,
        stroke_fill=(20, 12, 10, 255),
    )
    layer = Image.alpha_composite(layer, out_img)

    # 4. Bevel shade (+6 px, dark tone of line colour)
    dr, dg, db = stops[2]
    bs_img = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))
    bs_d = ImageDraw.Draw(bs_img)
    bs_d.text((ox + 2, oy + 6), line_text, font=f, fill=(dr, dg, db, 255))
    layer = Image.alpha_composite(layer, bs_img)

    # 5. Bevel light (−6 px, pale tone)
    pr, pg, pb = stops[0]
    bl_img = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))
    bl_d = ImageDraw.Draw(bl_img)
    bl_d.text((ox - 2, oy - 6), line_text, font=f, fill=(pr, pg, pb, 255))
    layer = Image.alpha_composite(layer, bl_img)

    # 6. Metallic texture fill through text mask
    mask = Image.new("L", (layer_w, layer_h), 0)
    m_d = ImageDraw.Draw(mask)
    m_d.text((ox, oy), line_text, font=f, fill=255)
    tex_arr = _make_metallic_texture(layer_w, layer_h, stops)
    tex_img = Image.fromarray(tex_arr, mode="RGB").convert("RGBA")
    fill_img = Image.new("RGBA", (layer_w, layer_h), (0, 0, 0, 0))
    fill_img.paste(tex_img, (0, 0), mask)
    layer = Image.alpha_composite(layer, fill_img)

    return layer, text_w, text_h, pad


def _composite_shade_gradient(canvas: Image.Image, deepen_contrast: bool = False) -> None:
    """Layer 1: soft dark shade gradient behind the lower third headline zone."""
    cw, ch = canvas.size
    start_y = int(ch * 0.58) if deepen_contrast else int(ch * 0.62)
    shade_h = ch - start_y
    scrim = Image.new("RGBA", (cw, shade_h), (0, 0, 0, 0))
    scrim_draw = ImageDraw.Draw(scrim)
    max_alpha = 248 if deepen_contrast else 215
    power = 1.2 if deepen_contrast else 1.5
    for y in range(shade_h):
        t_frac = y / max(1, shade_h - 1)
        alpha = int(round(max_alpha * (t_frac ** power)))
        scrim_draw.line([(0, y), (cw, y)], fill=(6, 8, 14, alpha))
    canvas.alpha_composite(scrim, (0, start_y))


def _composite_burmese_headline_v2(
    canvas: Image.Image,
    headline_text: str,
    palette_info: dict[str, Any] | None = None,
) -> None:
    """Render Burmese headline onto canvas with 7-layer metallic treatment and adaptive palette."""
    cleaned = sanitize_headline_text(headline_text)
    if not cleaned:
        return

    pal = palette_info if palette_info is not None else analyze_palette(canvas)
    deepen = bool(pal.get("deepen_contrast", False))

    # Layer 1: Soft dark shade gradient
    _composite_shade_gradient(canvas, deepen_contrast=deepen)

    raw_lines = [ln.strip() for ln in cleaned.split("\n") if ln.strip()]
    if not raw_lines:
        return
    # Up to 2 lines
    lines = raw_lines[:2]

    line1_stops = pal.get("line1", METALLIC_PALETTES["dark_neutral"]["line1"])
    line2_stops = pal.get("line2", METALLIC_PALETTES["dark_neutral"]["line2"])

    cw, ch = canvas.size
    bottom_margin = 120
    gap = 25

    if len(lines) == 1:
        img, tw, th, p = _render_metallic_text_line(lines[0], line1_stops, max_w=1035)
        dest_y = ch - bottom_margin - th - 50
        dest_x = (cw - tw) // 2
        canvas.alpha_composite(img, (dest_x - p, dest_y - p))
    else:
        img1, tw1, th1, p1 = _render_metallic_text_line(lines[0], line1_stops, max_w=1035)
        img2, tw2, th2, p2 = _render_metallic_text_line(lines[1], line2_stops, max_w=1035)

        dest_y2 = ch - bottom_margin - th2
        dest_y1 = dest_y2 - gap - th1
        dest_x1 = (cw - tw1) // 2
        dest_x2 = (cw - tw2) // 2

        canvas.alpha_composite(img1, (dest_x1 - p1, dest_y1 - p1))
        canvas.alpha_composite(img2, (dest_x2 - p2, dest_y2 - p2))


def _composite_burmese_headline(canvas: Image.Image, headline_text: str) -> None:
    """Backwards-compatible wrapper delegating to v2 renderer."""
    _composite_burmese_headline_v2(canvas, headline_text)


def composite_recap_thumbnail(
    base_image: Image.Image,
    *,
    headline_text: str | None = None,
    output_path: Path | None = None,
    logo_image: Image.Image | None = None,
    palette_info: dict[str, Any] | None = None,
) -> Image.Image:
    """Normalize base image to 1080x1920 (9:16), composite VoiceClonePro corner
    logo, and composite shaped Burmese headline v2 with adaptive palette."""
    canvas = ImageOps.fit(
        base_image.convert("RGBA"),
        (THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT),
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5),
    )

    logo = logo_image if logo_image is not None else load_brand_logo_image()
    _composite_corner_logo(canvas, logo)

    cleaned_headline = sanitize_headline_text(headline_text)
    if cleaned_headline:
        pal = palette_info if palette_info is not None else analyze_palette(canvas)
        _composite_burmese_headline_v2(canvas, cleaned_headline, palette_info=pal)

    final_rgb = canvas.convert("RGB")
    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp.jpg")
        final_rgb.save(str(tmp), format="JPEG", quality=92, optimize=True)
        os.replace(tmp, out)
    return final_rgb


async def generate_recap_ai_thumbnail(
    session: AsyncSession,
    *,
    user: User,
    job_id: str,
    recap_title: str | None,
    headline_text: str | None = None,
    genre: str | None = None,
    output_path: Path,
    narration_script: str | None = None,
    hf_call: Callable[..., Image.Image] | None = None,
    gemini_call: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Full end-to-end AI thumbnail workflow with tier gating, monthly hard cap,
    HF circuit breaker, Brain headline/prompt generation, and local Burmese v2 composite.
    """
    import asyncio

    # 1. Tier gate: PAID + admin only
    if not is_user_eligible_for_thumbnail(user):
        raise ThumbnailTierError("AI Thumbnail is available for VIP / Paid accounts only.")

    # 2. Hard budget check before calling HF
    await assert_monthly_thumbnail_budget_available(session)

    # 3. Brain call: writes headline + image prompt (Part 1)
    script_text = (
        narration_script
        if narration_script is not None
        else load_recap_narration_script(job_id)
    )
    brain_res = None
    try:
        brain_res = await asyncio.to_thread(
            call_brain_for_thumbnail,
            script_text,
            recap_title or "",
            gemini_call=gemini_call,
        )
    except Exception as b_exc:
        logger.warning("Brain call exception for job %s: %s", job_id, b_exc)

    brain_headline = brain_res.get("headline_my") if brain_res else None
    brain_prompt = brain_res.get("image_prompt_en") if brain_res else None

    # Fallback logic for image prompt:
    # If brain gave an image prompt, use it; else fallback to admin template with recap_title
    model_name = get_thumbnail_model()
    cost_usd = estimate_image_cost_usd(model_name)
    if brain_prompt:
        prompt = brain_prompt
    else:
        prompt_tpl = get_thumbnail_prompt_template()
        prompt = build_thumbnail_prompt(recap_title, genre=genre, template=prompt_tpl)

    # Headline selection:
    # User-supplied headline wins. If user field is empty, use brain headline.
    # If both empty, empty string = logo-only cover.
    cleaned_user_headline = sanitize_headline_text(headline_text)
    if cleaned_user_headline:
        final_headline = cleaned_user_headline
    elif brain_headline:
        final_headline = sanitize_headline_text(brain_headline)
    else:
        final_headline = ""

    # 4. HF image generation
    call_fn = hf_call or call_hf_text_to_image
    try:
        base_img = await asyncio.to_thread(
            call_fn,
            prompt,
            model_name=model_name,
        )
    except ThumbnailError:
        raise
    except Exception as exc:
        if is_hf_insufficient_credit_error(exc):
            logger.warning(
                "HF insufficient credits / quota error on thumbnail generation for job %s: %s",
                job_id,
                exc,
            )
            usage = await mark_thumbnail_pool_exhausted(session)
            await session.commit()
            raise ThumbnailCapReachedError(
                THUMBNAIL_MONTHLY_CAP_MESSAGE_MY,
                period=usage["period"],
                spend_usd=usage["estimated_spend_usd"],
                cap_usd=usage["monthly_cap_usd"],
            ) from exc
        logger.error("HF thumbnail generation failed for job %s: %s", job_id, exc)
        raise ThumbnailGenerationError(f"AI Thumbnail generation failed: {exc}") from exc

    # 5. HF succeeded and billed this generation -> record pool usage immediately
    usage = await record_thumbnail_generation_success(session, cost_usd=cost_usd)
    await session.commit()

    # 6. Composite v2: corner logo + Burmese headline v2 (or logo-only if empty)
    palette_info = analyze_palette(base_img)
    palette_family = palette_info["family"]

    await asyncio.to_thread(
        composite_recap_thumbnail,
        base_img,
        headline_text=final_headline,
        output_path=output_path,
        palette_info=palette_info,
    )

    logger.info(
        "AI Thumbnail generated for job %s: palette=%s, headline=%r",
        job_id,
        palette_family,
        final_headline,
    )

    return {
        "job_id": job_id,
        "output_path": str(output_path),
        "headline_text": final_headline,
        "logo_only": not bool(final_headline),
        "palette_family": palette_family,
        "model": model_name,
        "prompt": prompt,
        "cost_usd": cost_usd,
        "pool_usage": usage,
    }

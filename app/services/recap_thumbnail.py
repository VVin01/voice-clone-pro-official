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
import logging
import os
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw, ImageFilter, ImageOps
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
    """Clean and bound user-supplied headline text. Empty string = logo-only cover."""
    if text is None:
        return ""
    cleaned = unicodedata.normalize("NFC", str(text))
    cleaned = re.sub(r"[\r\n\t]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if max_chars > 0 and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip()
    return cleaned


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


def _resolve_padauk_bold_path() -> Path:
    return Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Padauk-Bold.ttf"


def _wrap_burmese_headline_lines(
    text: str,
    font_px: int,
    max_width_px: int,
    font_path: Path,
    stroke_px: int,
) -> list[str]:
    """Wrap Burmese + mixed-script headline text into balanced lines that fit
    within `max_width_px` using our shaped-text width measurer and Burmese
    syllable splitter (never breaking inside a cluster)."""
    from app.services.recap_engine.core import (
        canonicalize_burmese_text,
        split_into_burmese_syllables,
    )
    from app.services.recap_engine.png_subtitles import measure_line_width_px

    canon = canonicalize_burmese_text(text)
    canon = re.sub(r"\s+", " ", canon).strip()
    if not canon:
        return []

    def _w(s: str) -> int:
        return measure_line_width_px(s, font_px, font_path, stroke_px=stroke_px)

    if _w(canon) <= max_width_px:
        return [canon]

    # Tokenize into space-separated words or Burmese syllable atoms
    tokens: list[str] = []
    for word in canon.split(" "):
        if not word:
            continue
        if _w(word) <= max_width_px and " " not in word and len(word) <= 12:
            #Still split Burmese runs into syllables so wrapping is smooth
            syls = split_into_burmese_syllables(word) or [word]
            tokens.extend(syls)
        else:
            syls = split_into_burmese_syllables(word) or list(word)
            tokens.extend(syls)
        tokens.append(" ")
    if tokens and tokens[-1] == " ":
        tokens.pop()

    lines: list[str] = []
    cur = ""
    for tok in tokens:
        cand = cur + tok
        if cur.strip() and _w(cand.strip()) > max_width_px:
            lines.append(cur.strip())
            cur = tok.lstrip()
        else:
            cur = cand
    if cur.strip():
        lines.append(cur.strip())

    return [ln for ln in lines if ln][:4]


def _composite_burmese_headline(canvas: Image.Image, headline_text: str) -> None:
    """Render and composite the user's Burmese headline onto the lower third of
    the 1080x1920 canvas using the Padauk-Bold + split_font_runs shaped-text
    pipeline."""
    from app.services.recap_engine.png_subtitles import (
        _cached_font_for_width,
        _measure_runs_with_loader,
    )

    cleaned = sanitize_headline_text(headline_text)
    if not cleaned:
        return

    cw, ch = canvas.size
    font_path = _resolve_padauk_bold_path()
    max_text_w = int(round(cw * 0.84))  # ~907 px on 1080w

    # Choose font size adaptively based on headline length so it stays punchy (1-3 lines)
    if len(cleaned) <= 28:
        font_px = int(round(cw * 0.064))  # ~69 px
    elif len(cleaned) <= 60:
        font_px = int(round(cw * 0.054))  # ~58 px
    else:
        font_px = int(round(cw * 0.045))  # ~49 px
    stroke_px = max(4, int(round(font_px * 0.11)))

    lines = _wrap_burmese_headline_lines(
        cleaned,
        font_px=font_px,
        max_width_px=max_text_w,
        font_path=font_path,
        stroke_px=stroke_px,
    )
    if not lines:
        return

    # 1. Draw smooth bottom gradient scrim so the headline is readable over any artwork
    scrim_h = int(round(ch * 0.36))
    scrim = Image.new("RGBA", (cw, scrim_h), (0, 0, 0, 0))
    scrim_draw = ImageDraw.Draw(scrim)
    for y in range(scrim_h):
        t_frac = y / max(1, scrim_h - 1)
        alpha = int(round(215 * (t_frac ** 1.6)))
        scrim_draw.line([(0, y), (cw, y)], fill=(6, 8, 16, alpha))
    canvas.alpha_composite(scrim, (0, ch - scrim_h))

    # 2. Measure each line with our shaped-text run measurer (Padauk-Bold + NotoSans fallback)
    dummy = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
    d_dummy = ImageDraw.Draw(dummy)
    measured = [
        _measure_runs_with_loader(_cached_font_for_width, d_dummy, ln, font_px, font_path, stroke_px)
        for ln in lines
    ]

    line_step = max(font_px + 12, int(round(font_px * 1.38)))
    line_widths = [max(1, mb[2] - mb[0]) for _, mb, _ in measured]
    max_lw = max(line_widths)

    ink_x0, ink_y0 = 10**9, 10**9
    ink_x1, ink_y1 = -(10**9), -(10**9)
    line_positions: list[tuple[list[tuple[str, Any, float]], float, float]] = []
    for idx, (placed, mb, _adv) in enumerate(measured):
        lw = max(1, mb[2] - mb[0])
        x_off = (max_lw - lw) / 2.0 - mb[0]
        y_off = float(idx * line_step)
        ink_x0 = min(ink_x0, int(round(x_off + mb[0])))
        ink_x1 = max(ink_x1, int(round(x_off + mb[2])))
        ink_y0 = min(ink_y0, int(round(y_off + mb[1])))
        ink_y1 = max(ink_y1, int(round(y_off + mb[3])))
        line_positions.append((placed, x_off, y_off))

    ink_w = max(1, ink_x1 - ink_x0)
    ink_h = max(1, ink_y1 - ink_y0)
    pad_x = max(36, int(round(font_px * 0.65)))
    pad_y = max(28, int(round(font_px * 0.48)))

    box_w = min(cw - 48, ink_w + pad_x * 2)
    box_h = ink_h + pad_y * 2
    panel = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    p_draw = ImageDraw.Draw(panel)
    radius = max(20, int(round(font_px * 0.42)))

    # Translucent dark glass backing pill with warm gold border
    p_draw.rounded_rectangle(
        [0, 0, box_w - 1, box_h - 1],
        radius=radius,
        fill=(10, 12, 22, 188),
        outline=(255, 210, 60, 210),
        width=3,
    )

    shift_x = (box_w - ink_w) / 2.0 - ink_x0
    shift_y = pad_y - ink_y0

    # Soft shadow layer under text
    sh_layer = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    sh_draw = ImageDraw.Draw(sh_layer)
    sh_col = (0, 0, 0, 210)
    for placed, lx, ly in line_positions:
        for seg, f, rel_x in placed:
            sh_draw.text(
                (lx + shift_x + rel_x + 3, ly + shift_y + 4),
                seg,
                font=f,
                anchor="ls",
                fill=sh_col,
                stroke_width=stroke_px,
                stroke_fill=sh_col,
            )
    sh_layer = sh_layer.filter(ImageFilter.GaussianBlur(radius=4))
    panel = Image.alpha_composite(panel, sh_layer)
    p_draw = ImageDraw.Draw(panel)

    fill_col = (255, 225, 75, 255)    # Cinema gold headline
    stroke_col = (0, 0, 0, 255)       # Crisp black stroke
    for placed, lx, ly in line_positions:
        # Draw all strokes first, then fills so multi-run boundaries stay clean
        for seg, f, rel_x in placed:
            p_draw.text(
                (lx + shift_x + rel_x, ly + shift_y),
                seg,
                font=f,
                anchor="ls",
                fill=stroke_col,
                stroke_width=stroke_px,
                stroke_fill=stroke_col,
            )
        for seg, f, rel_x in placed:
            p_draw.text(
                (lx + shift_x + rel_x, ly + shift_y),
                seg,
                font=f,
                anchor="ls",
                fill=fill_col,
                stroke_width=0,
            )

    dest_x = (cw - box_w) // 2
    dest_y = ch - box_h - int(round(ch * 0.065))
    canvas.alpha_composite(panel, (max(0, dest_x), max(0, dest_y)))


def composite_recap_thumbnail(
    base_image: Image.Image,
    *,
    headline_text: str | None = None,
    output_path: Path | None = None,
    logo_image: Image.Image | None = None,
) -> Image.Image:
    """Normalize base image to 1080x1920 (9:16), composite the VoiceClonePro
    corner logo, and optionally composite the shaped Burmese headline (when
    `headline_text` is non-empty; empty = logo-only cover)."""
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
        _composite_burmese_headline(canvas, cleaned_headline)

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
    hf_call: Callable[..., Image.Image] | None = None,
) -> dict[str, Any]:
    """Full end-to-end AI thumbnail workflow with tier gating, monthly hard cap,
    HF insufficient-credit circuit breaker, and local Burmese + logo compositing.

    NEVER touches `user.recap_credits`, voice balances, or any user ledger.
    Counts against the monthly pool ONLY if HF actually returned an image.
    """
    import asyncio

    # 1. Tier gate: PAID + admin only
    if not is_user_eligible_for_thumbnail(user):
        raise ThumbnailTierError("AI Thumbnail is available for VIP / Paid accounts only.")

    # 2. Hard budget check before calling HF
    await assert_monthly_thumbnail_budget_available(session)

    model_name = get_thumbnail_model()
    prompt_tpl = get_thumbnail_prompt_template()
    prompt = build_thumbnail_prompt(recap_title, genre=genre, template=prompt_tpl)
    cost_usd = estimate_image_cost_usd(model_name)

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

    # 3. HF succeeded and billed this generation -> record pool usage immediately
    usage = await record_thumbnail_generation_success(session, cost_usd=cost_usd)
    await session.commit()

    # 4. Composite Burmese headline (or logo-only if empty) + VoiceClonePro logo
    cleaned_headline = sanitize_headline_text(headline_text)
    await asyncio.to_thread(
        composite_recap_thumbnail,
        base_img,
        headline_text=cleaned_headline,
        output_path=output_path,
    )

    return {
        "job_id": job_id,
        "output_path": str(output_path),
        "headline_text": cleaned_headline,
        "logo_only": not bool(cleaned_headline),
        "model": model_name,
        "prompt": prompt,
        "cost_usd": cost_usd,
        "pool_usage": usage,
    }

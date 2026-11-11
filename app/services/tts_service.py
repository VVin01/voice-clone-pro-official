"""Batch AW — standalone TTS tab service (Parts 2 + 3).

The TTS tab turns arbitrary text into audio with the two engines the
service already runs for recap narration — Gemini TTS and Microsoft
Edge TTS — under its OWN billing rules (owner decisions 2026-11-05):

* A TTS job is ``Job.job_type == "TTS"`` — never VOICE_CLONE — so the
  Batch AN daily voice-minute accounting (which counts VOICE_CLONE
  only) never sees it, and the voice-clone worker never claims it.
* It NEVER deducts purchased voice minutes, monthly/welcome seconds,
  or recap credits. Its cost is charged to the referral bucket FIRST
  (``ledger.draw_tts_referral_seconds`` — the referral bucket is the
  TTS balance since the AW re-scope), and only the remainder counts
  against the tab's own daily allowance
  (``tts_daily_limit_minutes_free`` / ``_paid``, 0 = unlimited).
* FAILED / CANCELLED TTS jobs refund their referral draw in full;
  a SUCCESS settles it against the measured duration, handing any
  over-drawn referral seconds back.
* Admins are exempt from the cap and never draw the referral bucket.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.enums import AccountCategory, JobStatus, JobType, UserStatus
from app.models.schema import Job, User
from app.services.download_names import branded_download_filename
from app.services.ledger import (
    draw_tts_referral_seconds,
    get_minute_balance,
    get_or_create_user,
    refund_tts_referral_seconds,
    tts_referral_net_draw,
)
from app.services.queue import MEASURED_CHARS_PER_SECOND, PRE_CHECK_SAFETY_MARGIN

logger = logging.getLogger(__name__)

TTS_ENGINES = ("gemini", "edge")
# Batch BW: the old flat TTS_MAX_CHARS = 5000 is gone — the per-tier
# cap lives in tts_character_limit() below.
# One pill row drives both engines — the recap flow's rate values
# (Edge: rate=..., Gemini: rate=...), per spec AW-5.
TTS_SPEED_RATES = ("+0%", "+8%", "+12%", "+18%", "+22%")
TTS_DEFAULT_SPEED = "+12%"
EDGE_TTS_VOICES = {
    "thiha": "my-MM-ThihaNeural",
    "nilar": "my-MM-NilarNeural",
}
# Gemini charges one API call per generate_channel99_voice call (the
# recap engine calls it per segment), so long TTS text is chunked at
# sentence boundaries and the WAV parts concatenated.
TTS_CHUNK_CHARS = 400


def _speed_factor(rate: str) -> float:
    try:
        pct = float(str(rate or "+0%").strip().rstrip("%"))
    except ValueError:
        pct = 0.0
    return max(0.5, 1.0 + pct / 100.0)


def estimate_tts_seconds(text: str, rate: str = TTS_DEFAULT_SPEED) -> float:
    """Pre-submit duration estimate — the same chars-per-second model
    the voice pre-check uses (queue.estimate_voice_job_seconds), with
    the speed factor taken from the raw rate string instead of a named
    preset."""
    valid = "".join(ch for ch in (text or "") if ch.isalnum() or ch.isspace())
    return len(valid) / (MEASURED_CHARS_PER_SECOND * _speed_factor(rate)) * PRE_CHECK_SAFETY_MARGIN


def split_tts_text(text: str, max_chars: int = TTS_CHUNK_CHARS) -> list[str]:
    """Split text into <= max_chars chunks on sentence boundaries
    (Myanmar ။, ASCII terminators, newlines), hard-splitting any single
    over-long sentence on whitespace."""
    sentences = [s.strip() for s in re.split(r"(?<=[။.!?])\s*|\n+", text or "") if s.strip()]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        while len(sentence) > max_chars:
            cut = sentence.rfind(" ", 0, max_chars)
            if cut <= 0:
                cut = max_chars
            piece, sentence = sentence[:cut].strip(), sentence[cut:].strip()
            if current:
                chunks.append(current)
                current = ""
            if piece:
                chunks.append(piece)
        if not sentence:
            continue
        if current and len(current) + 1 + len(sentence) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip() if current else sentence
    if current:
        chunks.append(current)
    return chunks


async def tts_cap_consumed_seconds(session: AsyncSession, user_id: int) -> float:
    """Today's TTS minutes already spoken for, in seconds — the AN
    accounting shape restricted to job_type TTS: SUCCESS rows count
    their measured ``seconds_consumed`` (estimate fallback), QUEUED /
    RUNNING rows count their submit estimate, FAILED / CANCELLED rows
    count nothing. Each row's cost is reduced by the referral seconds
    it drew (AW-10: only the remainder counts against the cap)."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    stmt = select(Job).where(
        Job.user_id == user_id,
        Job.job_type == JobType.TTS.value,
        Job.created_at >= today_start,
    )
    rows = (await session.execute(stmt)).scalars().all()
    total = 0.0
    for job in rows:
        if job.status == JobStatus.SUCCESS.value:
            cost = float(job.seconds_consumed or 0)
            if cost <= 0:
                cost = float(job.seconds_estimated or 0) or estimate_tts_seconds(
                    job.input_text or "", job.speed_preset or TTS_DEFAULT_SPEED
                )
        elif job.status in (JobStatus.QUEUED.value, JobStatus.RUNNING.value):
            cost = float(job.seconds_estimated or 0) or estimate_tts_seconds(
                job.input_text or "", job.speed_preset or TTS_DEFAULT_SPEED
            )
        else:
            continue
        net_draw = await tts_referral_net_draw(session, user_id, job.id)
        total += max(0.0, cost - net_draw)
    return total


def tts_daily_limit_minutes(user: User, *, is_admin: bool) -> int:
    if is_admin:
        return 0
    if user.account_category == AccountCategory.PURCHASED:
        return int(getattr(settings, "tts_daily_limit_minutes_paid", 60) or 0)
    return int(getattr(settings, "tts_daily_limit_minutes_free", 15) or 0)


def tts_character_limit(user: User, *, is_admin: bool) -> int:
    """Batch BW: the caller's per-generation character cap for the TTS
    tab — the SAME tier pair the voice-clone paths enforce
    (settings.character_limit_free / _purchased; owner decision
    2026-11-07 unifies them at FREE 1,000 / PAID 5,000 across Voice
    Clone and TTS), additionally bounded by the infrastructure safety
    ceiling. Admins are bound only by that ceiling, mirroring the
    voice choke point's admin branch in queue.py. Before BW this tab
    enforced one flat 5,000-char cap for everyone, so a FREE user
    could submit 5,000 chars here while the voice tab rejected them
    at their tier limit."""
    safety = int(getattr(settings, "infrastructure_safety_character_limit", 5000) or 5000)
    if is_admin:
        return safety
    if user.account_category == AccountCategory.PURCHASED:
        tier = int(getattr(settings, "character_limit_purchased", 5000) or 5000)
    else:
        tier = int(getattr(settings, "character_limit_free", 1000) or 1000)
    return min(tier, safety)


async def create_tts_job(
    session: AsyncSession,
    user_id: int,
    *,
    text: str,
    engine: str,
    voice: str,
    speed_rate: str,
) -> tuple[Job | None, str]:
    """Create a QUEUED TTS job under the AW billing rules.

    Returns (job, "QUEUED") on success or (None, reason) where reason
    is one of EMPTY_TEXT / INVALID_ENGINE / INVALID_VOICE /
    INVALID_SPEED / TEXT_EXCEEDS_CHARACTER_LIMIT /
    CONCURRENT_JOB_LIMIT_EXCEEDED / TTS_DAILY_LIMIT_EXCEEDED /
    USER_BLOCKED. Mirrors create_voice_clone_job's choke-point shape:
    Zawgyi is normalised here so every caller bills and synthesizes
    the same text, and the per-user row lock serialises the
    concurrency + cap + referral-draw checks (the AN/claim discipline).
    """
    from app.services.myanmar_text import convert_zawgyi_to_unicode

    engine = str(engine or "").strip().lower()
    if engine not in TTS_ENGINES:
        return None, "INVALID_ENGINE"
    voice = str(voice or "").strip()
    if engine == "gemini":
        from app.services.recap_engine.gemini_tts_engine import GEMINI_VOICE_NAMES

        if voice not in GEMINI_VOICE_NAMES:
            return None, "INVALID_VOICE"
    else:
        if voice not in EDGE_TTS_VOICES:
            return None, "INVALID_VOICE"
    speed_rate = str(speed_rate or TTS_DEFAULT_SPEED).strip()
    if speed_rate not in TTS_SPEED_RATES:
        return None, "INVALID_SPEED"

    user, _ = await get_or_create_user(session, user_id)
    if user.status == UserStatus.BLOCKED:
        return None, "USER_BLOCKED"
    is_admin = settings.is_admin(user_id)

    converted = convert_zawgyi_to_unicode(text or "")
    if converted != (text or ""):
        logger.info("TTS: Zawgyi input converted to Unicode for user %s", user_id)
    text = converted
    if not text.strip():
        return None, "EMPTY_TEXT"
    if len(text) > tts_character_limit(user, is_admin=is_admin):
        return None, "TEXT_EXCEEDS_CHARACTER_LIMIT"

    estimate = estimate_tts_seconds(text, speed_rate)

    if not is_admin:
        # Per-user row lock — same discipline as the voice choke point:
        # it serialises concurrent submits so the concurrency count,
        # the cap check and the referral draw all see one truth.
        await session.execute(
            select(User).where(User.id == user.id).with_for_update()
        )
        running_count = int(
            (
                await session.execute(
                    select(func.count(Job.id)).where(
                        Job.user_id == user_id,
                        Job.status.in_((JobStatus.QUEUED.value, JobStatus.RUNNING.value)),
                    )
                )
            ).scalar_one()
        )
        if running_count >= settings.max_concurrent_jobs_per_user:
            return None, "CONCURRENT_JOB_LIMIT_EXCEEDED"

        balance = await get_minute_balance(session, user_id)
        draw = min(int(balance.referral_seconds or 0), int(math.ceil(estimate)))
        limit_min = tts_daily_limit_minutes(user, is_admin=False)
        if limit_min > 0:
            consumed = await tts_cap_consumed_seconds(session, user_id)
            remainder = estimate - draw
            if consumed + remainder > limit_min * 60:
                logger.info(
                    "TTS daily cap reject for user %s: consumed %.0fs + remainder %.0fs > %s min",
                    user_id, consumed, remainder, limit_min,
                )
                return None, "TTS_DAILY_LIMIT_EXCEEDED"
    else:
        draw = 0

    job = Job(
        id=str(uuid.uuid4()),
        user_id=user_id,
        chat_id=user_id,
        job_type=JobType.TTS.value,
        engine=engine,
        input_text=text,
        preset_voice=voice if engine == "gemini" else EDGE_TTS_VOICES[voice],
        edge_voice=EDGE_TTS_VOICES[voice] if engine == "edge" else None,
        speed_preset=speed_rate,
        seconds_estimated=int(round(estimate)),
        status=JobStatus.QUEUED.value,
    )
    session.add(job)
    await session.flush()
    if draw > 0:
        await draw_tts_referral_seconds(session, user_id, job.id, draw)
    logger.info(
        "TTS job %s queued for user %s (engine=%s voice=%s est=%.0fs referral_draw=%ss)",
        job.id, user_id, engine, voice, estimate, draw,
    )
    return job, "QUEUED"


def _find_binary(name: str) -> str:
    found = shutil.which(name)
    return found or name


def edge_voice_key_for_gemini_voice(gemini_voice: str | None) -> str:
    """Batch CE: the Edge twin voice KEY for a picked Gemini voice —
    the same gender via GEMINI_VOICE_GENDER (female → nilar, male →
    thiha); an unmapped name defaults to thiha."""
    from app.services.recap_engine.gemini_tts_engine import GEMINI_VOICE_GENDER

    name = str(gemini_voice or "").strip().removeprefix("gemini:")
    return "nilar" if GEMINI_VOICE_GENDER.get(name) == "female" else "thiha"


def _is_gemini_exhaustion(exc: BaseException, engine: object | None = None) -> bool:
    """Batch CE: the fallback trigger set — ONLY exhaustion-class
    Gemini failures: no keys configured, every key dead/exhausted in
    the engine's own accounting, and the engine's exhausted fail-fast
    state. Any other synthesis error keeps the job-fails behaviour."""
    if engine is not None and getattr(engine, "exhausted", False):
        return True
    try:
        from app.services.recap_engine.gemini_tts_engine import GeminiTTSError
    except Exception:  # pragma: no cover - engine module always present
        GeminiTTSError = None  # type: ignore[assignment]
    if GeminiTTSError is not None and isinstance(exc, GeminiTTSError):
        # The one GeminiTTSError that is NOT exhaustion: an unknown
        # voice name — a config error the Edge twin cannot fix.
        return "Unknown Gemini voice" not in str(exc)
    # This service's own no-keys raise (a plain RuntimeError above).
    return "no Gemini API key configured" in str(exc)


def _fallback_reason_class(exc: BaseException) -> str:
    msg = str(exc)
    if "no Gemini API key configured" in msg:
        return "no_keys"
    if "location is not supported" in msg:
        return "location_unsupported"
    return "pool_exhausted"


def _synth_chunks(engine_obj: object, chunks: list[str], voice: str) -> list[Path]:
    parts: list[Path] = []
    for chunk in chunks:
        part = Path(engine_obj.generate_channel99_voice(chunk, voice))
        if not part.is_file() or part.stat().st_size < 256:
            raise RuntimeError("TTS engine returned no usable audio.")
        parts.append(part)
    return parts


def _build_routed_engine(ctx, ffmpeg: str, work_dir: Path, rate: str):
    """Batch CG: build the own-key → pool composite for one job from
    a resolved GeminiRouteContext. Either side may be absent (no
    usable own key / pool closed or unconfigured); returns None when
    neither route exists. The context's key value is handed ONLY to
    the engine constructor — never logged."""
    from app.services.recap_engine.gemini_tts_engine import (
        GeminiTTSEngine,
        RoutedGeminiEngine,
        collect_gemini_keys,
    )

    own_engine = None
    if ctx.has_usable_own_key:
        try:
            own_engine = GeminiTTSEngine(
                ffmpeg=ffmpeg, work_dir=work_dir, rate=rate,
                keys=[ctx.own_key],
                # Batch DN: a user-owned key's quota is theirs alone —
                # its successes must not enter the shared pool ledger.
                record_usage=False,
                log=lambda msg: logger.info("TTS gemini (own key): %s", msg),
            )
        except Exception:
            own_engine = None
    pool_engine = None
    if ctx.pool_allowed:
        keys = collect_gemini_keys()
        if keys:
            try:
                pool_engine = GeminiTTSEngine(
                    ffmpeg=ffmpeg, work_dir=work_dir, rate=rate, keys=keys,
                    log=lambda msg: logger.info("TTS gemini: %s", msg),
                )
            except Exception:
                pool_engine = None
    if own_engine is None and pool_engine is None:
        return None
    return RoutedGeminiEngine(
        own_engine, pool_engine,
        log=lambda msg: logger.info("TTS gemini route: %s", msg),
    )


def _record_route_outcome(job: Job, ctx, routed) -> None:
    """Batch CG: stamp the job's route truth + the transient outcome
    the executor settles (pool debit / invalid marking) after the
    synthesis succeeds."""
    from app.services.gemini_key_service import log_route

    if routed.pool_requests and routed.own_requests:
        job.tts_route = "own_pool_mix"
        route_class = "pool"
    elif routed.pool_requests:
        job.tts_route = "pool"
        route_class = "pool"
    else:
        job.tts_route = "own_key"
        route_class = "own_key"
    if routed.own_key_invalid:
        job.gemini_key_broken = True
    job._cg_outcome = {
        "route": route_class,
        "pool_seconds": float(routed.pool_seconds),
        "pool_requests": int(routed.pool_requests),
        "own_key_invalid": bool(routed.own_key_invalid),
        "taste_blocked": False,
    }
    log_route(route_class, ctx.user_id, ctx.tier)


def _synthesize_tts_audio_sync(job: Job) -> tuple[Path, float]:
    """Blocking synthesis (run via asyncio.to_thread): chunk the text,
    synthesize each chunk with the selected engine, concatenate the
    WAV parts into the job's output file, and probe its duration.

    Batch CE: an exhaustion-class Gemini failure re-synthesises the
    ENTIRE job on the Edge twin of the picked voice (partial Gemini
    chunks are discarded — voices never mix inside one file) and
    stamps ``job.tts_fallback_used`` + ``job.edge_voice`` so the
    delivery caption can tell the truth; the row's engine /
    preset_voice keep the picked values for History."""
    from app.services.recap_engine.core import EdgeTTSEngine, probe_duration

    ffmpeg = _find_binary("ffmpeg")
    ffprobe = _find_binary("ffprobe")
    work_dir = Path(settings.storage_root) / "tts_work" / job.id
    work_dir.mkdir(parents=True, exist_ok=True)
    chunks = split_tts_text(job.input_text or "")
    if not chunks:
        raise ValueError("EMPTY_TEXT")

    rate = job.speed_preset or TTS_DEFAULT_SPEED
    if job.engine == "gemini":
        from app.services.recap_engine.gemini_tts_engine import (
            GeminiTTSEngine,
            collect_gemini_keys,
        )

        ctx = getattr(job, "_cg_route_ctx", None)
        gemini_engine = None
        if ctx is not None and ctx.taste_blocked:
            # Batch CG Item 3b, server side: a FREE user whose taste
            # is spent (and who has no usable own key) is NEVER
            # silently switched — the client prompt offered the key
            # first; a submit that still arrives as Gemini lands
            # truthfully on the Edge twin with the fallback flag, and
            # the caption names the key option (route "edge_taste").
            from app.services.gemini_key_service import log_route

            edge_voice = EDGE_TTS_VOICES[
                edge_voice_key_for_gemini_voice(job.preset_voice)
            ]
            engine_obj = EdgeTTSEngine(
                ffmpeg=ffmpeg, work_dir=work_dir, rate=rate, pitch="+0Hz",
                log=lambda msg: logger.info("TTS edge: %s", msg),
            )
            parts = _synth_chunks(engine_obj, chunks, edge_voice)
            job.tts_fallback_used = True
            job.edge_voice = edge_voice
            job.tts_route = "edge_taste"
            job._cg_outcome = {
                "route": "edge",
                "pool_seconds": 0.0,
                "pool_requests": 0,
                "own_key_invalid": False,
                "taste_blocked": True,
            }
            log_route("edge", ctx.user_id, ctx.tier)
        else:
            try:
                if ctx is not None:
                    routed = _build_routed_engine(ctx, ffmpeg, work_dir, rate)
                    if routed is None:
                        raise RuntimeError(
                            "Gemini TTS: no Gemini API key configured."
                        )
                    gemini_engine = routed
                    job._cg_routed = routed
                    parts = _synth_chunks(routed, chunks, job.preset_voice)
                    _record_route_outcome(job, ctx, routed)
                else:
                    keys = collect_gemini_keys()
                    if not keys:
                        raise RuntimeError(
                            "Gemini TTS: no Gemini API key configured."
                        )
                    gemini_engine = GeminiTTSEngine(
                        ffmpeg=ffmpeg, work_dir=work_dir, rate=rate, keys=keys,
                        log=lambda msg: logger.info("TTS gemini: %s", msg),
                    )
                    parts = _synth_chunks(gemini_engine, chunks, job.preset_voice)
            except Exception as exc:
                if not _is_gemini_exhaustion(exc, gemini_engine):
                    raise
                logger.warning(
                    "TTS fallback: gemini->edge job=%s reason=%s",
                    job.id, _fallback_reason_class(exc),
                )
                edge_voice = EDGE_TTS_VOICES[
                    edge_voice_key_for_gemini_voice(job.preset_voice)
                ]
                engine_obj = EdgeTTSEngine(
                    ffmpeg=ffmpeg, work_dir=work_dir, rate=rate, pitch="+0Hz",
                    log=lambda msg: logger.info("TTS edge: %s", msg),
                )
                parts = _synth_chunks(engine_obj, chunks, edge_voice)
                job.tts_fallback_used = True
                job.edge_voice = edge_voice
                if ctx is not None:
                    # Batch CG: the whole job re-synthesised on Edge —
                    # discarded Gemini chunks debit NOTHING (delivered
                    # seconds only), but a key Google rejected stays
                    # marked so the user is told + the key is flagged.
                    job.tts_route = "edge_exhausted"
                    routed = getattr(job, "_cg_routed", None)
                    job._cg_outcome = {
                        "route": "edge",
                        "pool_seconds": 0.0,
                        "pool_requests": 0,
                        "own_key_invalid": bool(
                            routed is not None and routed.own_key_invalid
                        ),
                        "taste_blocked": False,
                    }
                    if job._cg_outcome["own_key_invalid"]:
                        job.gemini_key_broken = True
                    from app.services.gemini_key_service import log_route

                    log_route("edge", ctx.user_id, ctx.tier)
    else:
        engine_obj = EdgeTTSEngine(
            ffmpeg=ffmpeg, work_dir=work_dir, rate=rate, pitch="+0Hz",
            log=lambda msg: logger.info("TTS edge: %s", msg),
        )
        voice = job.edge_voice or job.preset_voice or EDGE_TTS_VOICES["thiha"]
        parts = _synth_chunks(engine_obj, chunks, voice)

    outputs_dir = Path(settings.storage_root) / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    out_path = outputs_dir / f"{job.id}.wav"
    if len(parts) == 1:
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(parts[0]),
               "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", str(out_path)]
    else:
        list_file = work_dir / "concat.txt"
        list_file.write_text(
            "".join(f"file '{p.resolve()}'\n" for p in parts), encoding="utf-8"
        )
        cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
               "-safe", "0", "-i", str(list_file),
               "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", str(out_path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not out_path.is_file():
        raise RuntimeError(f"TTS concat failed: {(proc.stderr or '')[-300:]}")
    duration = probe_duration(ffprobe, out_path)
    return out_path, float(duration or 0.0)


async def _deliver_tts_to_chat(job: Job, out_path: Path) -> None:
    """Best-effort bot-chat delivery of the finished TTS audio (the
    same send-to-Telegram pattern voice jobs use). A delivery failure
    never fails the job — the Mini App download stays available."""
    try:
        if not settings.bot_token:
            return
        if out_path.stat().st_size > 50 * 1024 * 1024:
            logger.info("TTS job %s output exceeds the Bot API 50MB cap; chat delivery skipped", job.id)
            return
        from aiogram import Bot
        from aiogram.types import FSInputFile

        filename = branded_download_filename("TTS", "wav", job_id=job.id)
        # Batch BX: the caption now names the engine + friendly voice
        # (it said nothing before, and History leaked the raw ids).
        # Language comes from the owner's stored preference, loaded
        # through the same fresh-session pattern the clone delivery
        # sites use; any failure falls back to "my" silently.
        lang = "my"
        try:
            from app.db.session import AsyncSessionLocal
            from app.services.ledger import get_or_create_user

            async with AsyncSessionLocal() as _sess:
                _owner, _ = await get_or_create_user(_sess, job.user_id)
                lang = _owner.language_code or "my"
        except Exception:
            pass
        from app.services.i18n import t as _tts_t
        from app.services.voice_display import (
            tts_engine_display_name,
            tts_voice_display_name,
        )

        caption_engine = tts_engine_display_name(getattr(job, "engine", None))
        caption_voice = tts_voice_display_name(getattr(job, "engine", None), getattr(job, "preset_voice", None))
        # Batch CE: a fallback job's caption names the engine + voice
        # ACTUALLY used (Edge + the twin voice) instead, and gains the
        # explicit fallback note line — the row keeps the picked
        # values, so History still shows the picked name (BK split).
        fell_back = bool(getattr(job, "tts_fallback_used", False))
        if fell_back:
            caption_engine = tts_engine_display_name("edge")
            caption_voice = tts_voice_display_name(
                "edge", getattr(job, "edge_voice", None) or EDGE_TTS_VOICES["thiha"]
            )
        caption = _tts_t(
            "tts_delivery_caption",
            lang,
            engine=caption_engine,
            voice=caption_voice,
        )
        if fell_back:
            caption += "\n" + _tts_t("tts_fallback_note", lang)
            # Batch CG: when the switch happened because the FREE
            # Gemini taste ran out, name the own-key way back to
            # Gemini (the same option the Item 3b prompt offers).
            if getattr(job, "tts_route", None) == "edge_taste":
                caption += "\n" + _tts_t("tts_taste_key_hint", lang)
        # Batch CG: a saved key Google rejected mid-job — the job
        # still delivered (pool/Edge served it); the user is told.
        if getattr(job, "gemini_key_broken", False):
            caption += "\n" + _tts_t("tts_own_key_broken_note", lang)
        bot = Bot(token=settings.bot_token)
        try:
            await bot.send_audio(
                chat_id=job.chat_id or job.user_id,
                audio=FSInputFile(str(out_path), filename=filename),
                caption=caption,
                parse_mode="HTML",
            )
        finally:
            await bot.session.close()
    except Exception:
        logger.warning("TTS job %s chat delivery failed", job.id, exc_info=True)


async def execute_tts_job(job_id: str) -> None:
    """Run one TTS job in-process (spawned as a background task by the
    /tts/generate endpoint, mirroring the recap runner): claim
    QUEUED → RUNNING, synthesize, settle the referral draw against the
    measured duration, deliver to the bot chat. Any failure marks the
    job FAILED and refunds the referral draw in full."""
    from app.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        job = (
            await session.execute(select(Job).where(Job.id == job_id))
        ).scalar_one_or_none()
        if job is None or job.status != JobStatus.QUEUED.value:
            return
        job.status = JobStatus.RUNNING.value
        job.started_at = datetime.now(timezone.utc)
        await session.commit()

    try:
        async with AsyncSessionLocal() as session:
            job = (
                await session.execute(select(Job).where(Job.id == job_id))
            ).scalar_one()
            # Batch CG: resolve the Gemini route (own key → pool →
            # Edge) for Gemini-picked jobs BEFORE synthesis; the
            # context rides the job object as a transient attribute
            # (memory only — the key value is never persisted).
            route_ctx = None
            if job.engine == "gemini":
                try:
                    from app.services.gemini_key_service import (
                        resolve_gemini_route,
                    )

                    _owner, _ = await get_or_create_user(session, job.user_id)
                    route_ctx = await resolve_gemini_route(
                        session, _owner, is_admin=settings.is_admin(job.user_id)
                    )
                    job._cg_route_ctx = route_ctx
                except Exception:
                    logger.warning(
                        "TTS job %s: CG route resolution failed; legacy pool path",
                        job_id, exc_info=True,
                    )
                    route_ctx = None
            out_path, duration = await asyncio.to_thread(_synthesize_tts_audio_sync, job)
            job.result_path = str(out_path)
            job.seconds_consumed = int(round(duration))
            job.status = JobStatus.SUCCESS.value
            job.finished_at = datetime.now(timezone.utc)
            # Batch CG settle: debit the SUCCESSFUL pool draws
            # (taste/budget — FREE only, enforced inside the helper)
            # and flag a key Google rejected mid-job so the user is
            # told (caption note) and routing skips it from now on.
            _cg_outcome = getattr(job, "_cg_outcome", None)
            if _cg_outcome is not None:
                try:
                    from app.services.gemini_key_service import (
                        debit_pool_usage,
                        mark_user_key_invalid,
                    )

                    if _cg_outcome.get("own_key_invalid"):
                        await mark_user_key_invalid(session, job.user_id)
                    if route_ctx is not None and _cg_outcome.get("pool_requests"):
                        await debit_pool_usage(
                            session,
                            job.user_id,
                            tier=route_ctx.tier,
                            seconds=float(_cg_outcome.get("pool_seconds") or 0.0),
                            requests=int(_cg_outcome.get("pool_requests") or 0),
                        )
                except Exception:
                    logger.warning(
                        "TTS job %s: CG settle failed", job_id, exc_info=True
                    )
            # Settle: keep only the referral seconds the audio actually
            # cost; hand any over-draw back (idempotent reconcile).
            net = await tts_referral_net_draw(session, job.user_id, job.id)
            if net > 0:
                await refund_tts_referral_seconds(
                    session, job.user_id, job.id,
                    keep_seconds=min(net, int(job.seconds_consumed)),
                    note=f"TTS job {job_id} settled (Referral)",
                )
            await session.commit()
            user_id = job.user_id
        logger.info("TTS job %s SUCCESS (%.1fs)", job_id, duration)
        async with AsyncSessionLocal() as session:
            job = (
                await session.execute(select(Job).where(Job.id == job_id))
            ).scalar_one()
            await _deliver_tts_to_chat(job, out_path)
        _ = user_id
    except Exception as exc:  # noqa: BLE001 — job failure path
        logger.exception("TTS job %s failed", job_id)
        try:
            async with AsyncSessionLocal() as session:
                job = (
                    await session.execute(select(Job).where(Job.id == job_id))
                ).scalar_one_or_none()
                if job is not None and job.status != JobStatus.SUCCESS.value:
                    job.status = JobStatus.FAILED.value
                    job.error_message = str(exc)[:500]
                    job.finished_at = datetime.now(timezone.utc)
                    await refund_tts_referral_seconds(session, job.user_id, job_id)
                    await session.commit()
        except Exception:
            logger.exception("TTS job %s failure bookkeeping failed", job_id)

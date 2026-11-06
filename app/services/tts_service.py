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
TTS_MAX_CHARS = 5000
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
    if len(text) > TTS_MAX_CHARS:
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


def _synthesize_tts_audio_sync(job: Job) -> tuple[Path, float]:
    """Blocking synthesis (run via asyncio.to_thread): chunk the text,
    synthesize each chunk with the selected engine, concatenate the
    WAV parts into the job's output file, and probe its duration."""
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

        keys = collect_gemini_keys()
        if not keys:
            raise RuntimeError("Gemini TTS: no Gemini API key configured.")
        engine_obj = GeminiTTSEngine(
            ffmpeg=ffmpeg, work_dir=work_dir, rate=rate, keys=keys,
            log=lambda msg: logger.info("TTS gemini: %s", msg),
        )
        voice = job.preset_voice
    else:
        engine_obj = EdgeTTSEngine(
            ffmpeg=ffmpeg, work_dir=work_dir, rate=rate, pitch="+0Hz",
            log=lambda msg: logger.info("TTS edge: %s", msg),
        )
        voice = job.edge_voice or job.preset_voice or EDGE_TTS_VOICES["thiha"]

    parts: list[Path] = []
    for chunk in chunks:
        part = Path(engine_obj.generate_channel99_voice(chunk, voice))
        if not part.is_file() or part.stat().st_size < 256:
            raise RuntimeError("TTS engine returned no usable audio.")
        parts.append(part)

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
        bot = Bot(token=settings.bot_token)
        try:
            await bot.send_audio(
                chat_id=job.chat_id or job.user_id,
                audio=FSInputFile(str(out_path), filename=filename),
                caption=(
                    "🎧 <b>TTS အသံ</b>\n"
                    "✨ <i>Mini App TTS Tab မှ တောင်းဆိုချက်အရ ပေးပို့လိုက်ပါသည်ခင်ဗျာ။</i>"
                ),
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
            out_path, duration = await asyncio.to_thread(_synthesize_tts_audio_sync, job)
            job.result_path = str(out_path)
            job.seconds_consumed = int(round(duration))
            job.status = JobStatus.SUCCESS.value
            job.finished_at = datetime.now(timezone.utc)
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

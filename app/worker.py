from __future__ import annotations

import asyncio
import html
import logging
from pathlib import Path
import re
import shutil
import time
from aiogram import Bot
from aiogram.types import FSInputFile
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.models.enums import AccountCategory, JobStatus
from app.models.schema import AuditLog, Job, User, VoiceModel, utc_now
from app.services.audio import (
    GENDER_PITCH_MAP,
    concat_wav_files,
    generate_edge_tts,
    get_output_filename,
    get_wav_duration_seconds,
    normalize_audio_to_wav,
)
from app.services.download_names import branded_download_filename
from app.services.i18n import t
from app.services.ledger import consume_seconds
from app.services.models import get_authorized_model
from app.services.progress import JobProgressTracker, render_stepper_progress, safe_edit_status
from app.services import voice_choice as _vc
from app.services.speed import DEFAULT_SPEED, DEFAULT_TEMPO, resolve_speed_tempo

logger = logging.getLogger(__name__)

# Telegram Bot API refuses bot uploads above 50 MB. Voice WAVs run
# ~5.8 MB/min, so long generations can cross the cap; delivery must
# check before sending (hunt #3 B1) — the recap pipeline applies the
# same limit to its sends.
TELEGRAM_SEND_LIMIT_BYTES = 50 * 1024 * 1024


def _implausible_audio_reason(text: str, duration_seconds: int, speed_preset) -> str | None:
    """User-facing reason when produced audio is too short for its text.

    Production-measured Burmese narration pace: the primary engine
    runs ≈17.6 chars/s, scaled proportionally by the unified speed tempo
    (1.12x default → 35.0 chars/s threshold; 0.94x slow → 29.4;
    1.20x fast → 37.5; 1.25x ultra → 39.1). Short texts are exempt:
    duration rounding (whole seconds) dominates there.
    """
    chars = len((text or "").strip())
    if chars < 300 or not duration_seconds or duration_seconds <= 0:
        return None
    tempo = resolve_speed_tempo(speed_preset)
    max_rate = 35.0 * (tempo / DEFAULT_TEMPO)
    if duration_seconds < chars / max_rate:
        return (
            "ထုတ်လုပ်လိုက်သော အသံသည် စာသားအားလုံးကို မပါဝင်နိုင်လောက်အောင် "
            f"တိုလွန်းနေပါသည် (စာလုံး {chars} လုံး အတွက် {duration_seconds} စက္ကန့်သာ ရှိပါသည်)။ "
            "ခဏစောင့်ပြီး ပြန်စမ်းကြည့်ပါ ခင်ဗျာ။"
        )
    return None


_INSUFFICIENT_BALANCE_RE = re.compile(
    r"insufficient balance: requested (\d+)s, available (\d+)s", re.IGNORECASE
)


def _user_facing_worker_error(exc: BaseException, lang: str = "my") -> str:
    """Map a raw worker exception to the curated user-facing message.

    job.error_message is surfaced verbatim by the Mini App, and the
    failure path quotes it on the user's Telegram status card — so
    raw str(exc) (provider internals, local file paths, English
    library text like the ledger's "Insufficient balance: requested
    197s, available 180s") must never be stored there (audit fix
    2026-11-03, mirroring _voice_job_error_detail in the webapp).
    The raw text stays in the logs; the caller logs the exception
    before calling this. Messages that are ALREADY curated user copy
    (the truncation gate's Burmese reason) pass through untouched.
    """
    raw = str(exc or "")
    # Already-curated copy: the implausible-audio gate raises with a
    # finished Burmese user message — keep it word for word.
    if "တိုလွန်းနေပါသည်" in raw:
        return raw
    m = _INSUFFICIENT_BALANCE_RE.search(raw)
    if m:
        return t(
            "worker_error_insufficient_balance", lang,
            needed=m.group(1), available=m.group(2),
        )
    low = raw.lower()
    if "legacy trained voice model" in low:
        return t("queue_failed_legacy_model", lang)
    if "model is missing" in low or "not authorized" in low:
        return t("queue_failed_model_not_authorized", lang)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timed out" in low or "timeout" in low:
        return t("worker_error_timeout", lang)
    if any(
        needle in low
        for needle in (
            "voxcpm", "synthes", "edge tts", "tts", "provider",
            "huggingface", "hugging face",
        )
    ):
        return t("worker_error_engine_failure", lang)
    return t("worker_error_generic", lang)


async def sync_output_to_cloud(job_id: str, file_path: Path) -> None:
    """Upload generated audio file from Local Worker to Render Cloud so Mini App & Bot can serve it."""
    import os
    # Hardening (hunt F8): without a dedicated WORKER_TOKEN there is
    # no remote worker to sync for — the co-located worker writes
    # straight into shared storage, so this is a no-op. The bot
    # token is never used as an upload credential anymore.
    worker_token = (getattr(settings, "worker_token", "") or "").strip()
    if not worker_token:
        return
    cloud_url = os.getenv("RENDER_EXTERNAL_URL", "").strip() or os.getenv("MINI_APP_URL", "").strip() or "https://voice-clone-studio-c1er.onrender.com"
    if not file_path or not file_path.is_file():
        return
    try:
        import httpx
        async with httpx.AsyncClient(timeout=90.0) as client:
            with open(file_path, "rb") as f:
                files = {"file": (file_path.name, f, "audio/wav")}
                headers = {"X-Worker-Token": worker_token}
                url = f"{cloud_url}/api/internal/worker/upload-result/{job_id}"
                resp = await client.post(url, files=files, headers=headers)
                if resp.is_success:
                    logger.info("✅ Audio result for job %s successfully uploaded to Render Cloud server!", job_id)
                else:
                    logger.warning("Cloud upload for job %s returned status %s: %s", job_id, resp.status_code, resp.text)
    except Exception as e:
        logger.warning("Failed to sync output to cloud for job %s: %s", job_id, e)


async def _voice_job_cancelled(session: AsyncSession, job: Job) -> bool:
    """Batch AJ (H5): re-read the job's status from the DB.

    The cancel endpoints (user + admin) flip the row to CANCELLED and
    refund while the worker is mid-flight; the worker's in-memory copy
    never sees that write. Before AJ the worker billed the full
    duration anyway and overwrote the row to SUCCESS — the user was
    told "cancelled + refunded" and charged in full. The worker now
    re-reads at the two points where money/delivery commit: before
    billing, and after the billing commit before delivery.
    """
    try:
        await session.refresh(job)
    except Exception:
        return False
    return job.status == JobStatus.CANCELLED.value


def _synthesize_voxcpm_voice_job(
    engine,
    *,
    job_id: str,
    text: str,
    output_wav: Path,
    reference_wav,
    control_instruction,
    preset_voice,
    speed,
    progress_cb,
    reference_transcript: str = "",
    reference_seconds: float = 0.0,
    tier: str = "free",
    allow_fallback: bool = True,
) -> Path:
    """Batch CH: VoxCPM synthesis for a voice-clone job, chunked when
    the text cannot fit the model's 2,096-token input cap in one call.

    A text that fits stays EXACTLY the pre-CH single call (same
    arguments, engine-internal fallback semantics untouched). A longer
    text is split by app.services.voice_chunking into sentence-aligned
    chunks; each chunk is synthesized with the SAME reference,
    control, persona and speed, with allow_fallback=False so a chunk
    can never silently switch voice mid-job — a chunk failure raises
    into the caller's EXISTING whole-job resilient fallback, and the
    parts are concatenated only after every chunk succeeded, so a
    mixed-voice output is never produced. Runs in an executor.

    Batch DP: ``tier`` / ``allow_fallback`` are the caller's tier
    plumbing — paid jobs walk the reserve and run with the engine's
    internal Edge fallback DISABLED end-to-end (a quota exhaustion
    must surface as VoxCPMQuotaExhaustedError so the caller can park
    the job, never as silently-switched persona audio). Free jobs
    keep the defaults and are byte-identical to before.
    """
    from app.services.voice_chunking import plan_text_chunks

    # Only non-default tier/fallback values are passed through: a
    # default (free) job's engine call stays byte-identical to the
    # pre-DP shape (Batch CH pins the single call's argument set).
    _dp_kwargs: dict = {}
    if tier != "free":
        _dp_kwargs["tier"] = tier
    if not allow_fallback:
        _dp_kwargs["allow_fallback"] = False

    chunks = plan_text_chunks(
        text,
        reference_transcript=reference_transcript,
        control_instruction=control_instruction or "",
        reference_seconds=reference_seconds,
    )
    if len(chunks) <= 1:
        return engine.synthesize(
            text=text,
            output_wav=output_wav,
            reference_wav=reference_wav,
            control_instruction=control_instruction,
            preset_voice=preset_voice,
            speed=speed,
            progress_cb=progress_cb,
            **_dp_kwargs,
        )

    logger.info(
        "Voice clone chunked synthesis: job=%s chunks=%d", job_id, len(chunks)
    )
    parts: list[Path] = []
    try:
        for idx, chunk in enumerate(chunks, 1):
            part = output_wav.parent / f"{output_wav.stem}.chunk{idx:02d}.wav"
            engine.synthesize(
                text=chunk,
                output_wav=part,
                reference_wav=reference_wav,
                control_instruction=control_instruction,
                preset_voice=preset_voice,
                speed=speed,
                progress_cb=progress_cb,
                allow_fallback=False,
                **({"tier": tier} if tier != "free" else {}),
            )
            parts.append(part)
        concat_wav_files(parts, output_wav)
    finally:
        for part in parts:
            try:
                part.unlink(missing_ok=True)
            except Exception:
                pass
    return output_wav


async def process_single_job(
    session: AsyncSession,
    job: Job,
    bot: Bot | None = None,
    chat_id: int | None = None,
    status_msg_id: int | None = None,
) -> bool:
    """Execute complete voice clone job lifecycle with safe temp cleanup and idempotency."""
    # Terminal state check: never re-run completed or failed jobs
    if job.status in (JobStatus.SUCCESS.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value):
        logger.warning("Job %s is already in terminal state %s; skipping.", job.id, job.status)
        return False

    temp_dir = settings.storage_root / "temp" / str(job.id)
    temp_dir.mkdir(parents=True, exist_ok=True)

    user = await session.get(User, job.user_id)
    lang = user.language_code if user else "my"
    target_chat_id = chat_id or job.chat_id or job.user_id
    target_msg_id = status_msg_id or job.status_message_id

    # Batch DP: park-and-ask state for THIS run. ``parked`` tells the
    # finally block to preserve the job's reference audio (a parked
    # job resumes later; its inputs must survive). ``_dp_paid`` is
    # the tier: PURCHASED or admin. ``_dp_choice_state`` is the
    # owner's previously recorded choice when this run IS a resume.
    parked = False
    _dp_choice_state = _vc.parse_choice_state(
        getattr(job, "voice_choice_state", None)
    )
    _dp_paid = bool(
        settings.is_admin(job.user_id)
        or (
            user is not None
            and user.account_category == AccountCategory.PURCHASED.value
        )
    )

    tracker: JobProgressTracker | None = None
    try:
        # Stage 1: Validation
        is_voxcpm = (
            getattr(job, "engine", "") == "voxcpm"
            or getattr(job, "preset_voice", None) is not None
        )

        model = None
        if job.model_id is not None:
            model = await get_authorized_model(session, job.user_id, job.model_id)
            if not model:
                model = await session.get(VoiceModel, job.model_id)

        if not model and is_voxcpm:
            stmt_v = select(VoiceModel).where(VoiceModel.slug == "voxcpm-cloud-ai")
            res_v = await session.execute(stmt_v)
            model = res_v.scalar_one_or_none()

        if not model:
            if is_voxcpm:
                from app.models.enums import VoiceModelStatus, VoiceModelType
                model = VoiceModel(
                    name="🎙️ VoxCPM (Cloud AI)",
                    slug="voxcpm-cloud-ai",
                    type=VoiceModelType.SYSTEM.value,
                    status=VoiceModelStatus.ACTIVE.value,
                    allowed_for_free=True,
                    allowed_for_purchased=True,
                    is_default_free=True,
                    pth_path="voxcpm",
                    model_dir="cloud",
                )
                session.add(model)
                await session.flush()
            else:
                model = await session.get(VoiceModel, job.model_id) if job.model_id else None
                if not model:
                    raise RuntimeError("Model is missing or not authorized")

        if not is_voxcpm:
            is_voxcpm = (
                getattr(model, "pth_path", "") == "voxcpm"
                or "voxcpm" in getattr(model, "slug", "").lower()
            )
        if not is_voxcpm:
            raise RuntimeError(
                "Legacy trained voice model is no longer supported. "
                "Please re-clone the voice with a short reference audio using VoxCPM2."
            )

        display_name = model.name or "Standard"
        if getattr(job, "preset_voice", None):
            try:
                from app.services.curated_voices import get_curated_voice
                p_info = get_curated_voice(job.preset_voice)
                if p_info:
                    display_name = f"VoxCPM ({p_info.get('name_my' if lang == 'my' else 'name_en')})"
            except Exception:
                pass

        tracker = JobProgressTracker(
            bot=bot,
            chat_id=target_chat_id,
            message_id=target_msg_id,
            model_name=display_name,
            lang=lang,
        )
        await tracker.update_step(1)

        job.status = JobStatus.RUNNING.value
        job.started_at = utc_now()
        await session.commit()

        # Stage 2: Prepare base audio & Normalize
        await tracker.update_step(2)
        norm_wav_path = temp_dir / "norm.wav"
        if not is_voxcpm:
            if getattr(job, "source_audio_path", None) and Path(job.source_audio_path).is_file():
                logger.info("Direct audio input mode for job %s: normalizing %s", job.id, job.source_audio_path)
                await normalize_audio_to_wav(Path(job.source_audio_path), norm_wav_path)
            else:
                base_tts_path = temp_dir / "base.mp3"
                await generate_edge_tts(
                    text=job.input_text or "",
                    out_path=base_tts_path,
                    voice=job.edge_voice or "my-MM-ThihaNeural",
                    speed=job.speed_preset or DEFAULT_SPEED,
                )
                await normalize_audio_to_wav(base_tts_path, norm_wav_path)

        # Stage 3: Run VoxCPM2 Voice Conversion with Live Ticker & Real-time ETA
        out_name = f"voxcpm_{job.preset_voice}" if getattr(job, "preset_voice", None) else model.name
        out_filename = get_output_filename(out_name)
        converted_wav_path = temp_dir / out_filename

        await tracker.update_step(3)
        await tracker.start_ticker(step=3)
        used_fallback = False
        # Batch BU: same treatment for the reference auto-trim flag —
        # read from the engine after the try/finally below.
        reference_trimmed = False
        # Initialized BEFORE the try (audit fix 2026-11-03): both are
        # read after the try/finally below (the engine fallback flag
        # and the stage-4 truncation gate), so a failure constructing
        # them inside the block must never be able to mask the real
        # error behind a NameError at those reads.
        engine_inst = None
        # Batch CW: rewrite digit runs in the SYNTHESIS text as
        # Myanmar number words, so VoxCPM reads numbers in Burmese
        # instead of English (and the Edge fallback below, which
        # receives this same clean_text, speaks the same words).
        # job.input_text is never mutated: the character-limit
        # validation and billing in create_voice_clone_job already ran
        # on the user's original text, and History/display keep
        # showing it. The normalization happens HERE, upstream of the
        # Batch CH chunker, so chunk planning and its token budget are
        # computed on the normalized (longer) text that is actually
        # synthesized.
        from app.services.number_words import normalize_numbers_for_speech

        clean_text = normalize_numbers_for_speech(
            job.input_text or "မင်္ဂလာပါခင်ဗျာ"
        )
        # Release the pooled connection for the synthesis stage
        # (audit fix 2026-11-03): stage 3 runs for minutes in an
        # executor and touches no DB state, so any transaction still
        # open here would pin one pool connection for the whole
        # generation. The stage-1 commit above already ended the
        # claim/validation transaction on the happy path — this makes
        # the release unconditional instead of incidental.
        await session.commit()
        try:
            if is_voxcpm:
                logger.info("Starting VoxCPM 2 Cloud AI synthesis for job %s", job.id)
                import functools
                from app.services.voxcpm_engine import VoxCPMEngine, VoxCPMQuotaExhaustedError
                engine_inst = VoxCPMEngine(work_dir=temp_dir)

                ref_wav = None
                if getattr(job, "source_audio_path", None) and Path(job.source_audio_path).is_file():
                    ref_wav = Path(job.source_audio_path)
                elif getattr(model, "sample_audio_path", None) and Path(model.sample_audio_path).is_file():
                    ref_wav = Path(model.sample_audio_path)

                control_instruction = None
                if getattr(job, "preset_voice", None):
                    from app.services.curated_voices import get_curated_voice
                    p_info = get_curated_voice(job.preset_voice)
                    if p_info:
                        control_instruction = p_info.get("control_instruction")
                        logger.info("Using curated persona '%s' for VoxCPM 2 job %s", job.preset_voice, job.id)

                if not control_instruction:
                    is_female = (
                        getattr(job, "edge_voice", "") == "my-MM-NilarNeural"
                        or getattr(job, "gender_preset", "") in ("m2f", "f2f")
                    )
                    if is_female:
                        control_instruction = (
                            "A sweet, gentle, natural Burmese female speaker with soft melodious voice, "
                            "clear feminine tone, warm studio acoustics, and fluent authentic native pronunciation."
                        )
                    else:
                        control_instruction = (
                            "A clear, natural, emotionally expressive Burmese male speaker with warm tone, "
                            "deep resonance, velvety cadence, and fluent authentic native pronunciation."
                        )

                # Batch CH: reference overhead for the chunk budget —
                # the persona reference transcript (exact, when this is
                # a persona job) plus the reference audio's duration
                # (the engine trims clone references to 45 s; the
                # chunker caps the estimate there too). Best-effort:
                # any probe failure just means a zero overhead, i.e.
                # the largest chunk budget.
                reference_transcript = ""
                reference_seconds = 0.0
                if getattr(job, "preset_voice", None):
                    try:
                        from app.services.curated_voices import get_persona_reference

                        _p_ref_path, _p_ref_text = get_persona_reference(
                            job.preset_voice
                        )
                        reference_transcript = _p_ref_text or ""
                        if ref_wav is None and _p_ref_path and Path(_p_ref_path).is_file():
                            reference_seconds = float(
                                get_wav_duration_seconds(Path(_p_ref_path))
                            )
                    except Exception:
                        reference_transcript = ""
                        reference_seconds = 0.0
                if ref_wav is not None:
                    try:
                        reference_seconds = float(get_wav_duration_seconds(ref_wav))
                    except Exception:
                        reference_seconds = 0.0

                # Batch DP: an owner who already chose Edge-only on
                # the park prompt skips the clone attempt entirely;
                # a paid job's quota exhaustion PARKS instead of
                # reaching the resilient fallback below.
                _dp_edge_all = (
                    _dp_choice_state is not None
                    and _vc.choice_mode(_dp_choice_state) == _vc.CHOICE_EDGE_ALL
                )
                _vox_error: Exception | None = None
                if _dp_edge_all:
                    _vox_error = RuntimeError(
                        "Owner chose Edge-only delivery at the park prompt (Batch DP)"
                    )
                else:
                    try:
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(
                            None,
                            functools.partial(
                                _synthesize_voxcpm_voice_job,
                                engine_inst,
                                job_id=str(job.id),
                                text=clean_text,
                                output_wav=converted_wav_path,
                                reference_wav=ref_wav,
                                control_instruction=control_instruction,
                                preset_voice=getattr(job, "preset_voice", None),
                                speed=job.speed_preset or DEFAULT_SPEED,
                                progress_cb=tracker.set_custom_note,
                                reference_transcript=reference_transcript,
                                reference_seconds=reference_seconds,
                                tier="paid" if _dp_paid else "free",
                                allow_fallback=not _dp_paid,
                            ),
                        )
                    except VoxCPMQuotaExhaustedError as e_quota:
                        if _dp_paid:
                            # Batch DP (Item 4): park the paid job and
                            # ASK — never silently switch its voice.
                            # No charge has been made (billing is stage
                            # 4), so parking holds everything as-is.
                            # A re-park (this run was itself a resume)
                            # keeps the recorded state + prompt and
                            # refreshes the prompt in place — no new
                            # message.
                            _dp_repark = bool(
                                _dp_choice_state
                                and _dp_choice_state.get("prompt_message_id")
                            )
                            if _dp_repark:
                                _dp_state = _dp_choice_state
                                _dp_state["parked_at"] = time.time()
                                _dp_state.pop("resumed_at", None)
                            else:
                                _dp_state = _vc.new_choice_state(_vc.KIND_CLONE)
                                # Preserve the reference audio: the
                                # finally below deletes temp inputs,
                                # and a parked job must resume later —
                                # move it to the durable park store and
                                # point the row at the copy.
                                _src_path = getattr(job, "source_audio_path", None)
                                if _src_path and Path(_src_path).is_file() and "temp" in str(_src_path):
                                    _dp_ref_dir = settings.storage_root / "dp_refs"
                                    _dp_ref_dir.mkdir(parents=True, exist_ok=True)
                                    _dp_ref_copy = _dp_ref_dir / f"{job.id}{Path(_src_path).suffix or '.wav'}"
                                    shutil.copy2(_src_path, _dp_ref_copy)
                                    job.source_audio_path = str(_dp_ref_copy)
                            job.status = JobStatus.WAITING_CHOICE.value
                            job.voice_choice_state = _vc.dump_choice_state(_dp_state)
                            await session.commit()
                            parked = True
                            if bot is not None:
                                if _dp_repark:
                                    await _vc.refresh_choice_prompt(
                                        bot,
                                        chat_id=_dp_state.get("prompt_chat_id") or target_chat_id,
                                        message_id=_dp_state.get("prompt_message_id"),
                                        job_id=str(job.id),
                                        kind=_vc.KIND_CLONE,
                                        lang=lang,
                                    )
                                else:
                                    _dp_mid = await _vc.deliver_choice_prompt(
                                        bot,
                                        chat_id=target_chat_id,
                                        message_id=target_msg_id,
                                        job_id=str(job.id),
                                        kind=_vc.KIND_CLONE,
                                        lang=lang,
                                    )
                                    if _dp_mid:
                                        _dp_state["prompt_chat_id"] = target_chat_id
                                        _dp_state["prompt_message_id"] = _dp_mid
                                        job.voice_choice_state = _vc.dump_choice_state(_dp_state)
                                        await session.commit()
                            logger.info(
                                "Voice job %s parked (WAITING_CHOICE): VoxCPM quota exhausted",
                                job.id,
                            )
                            return False
                        _vox_error = e_quota
                    except Exception as e_vox:
                        _vox_error = e_vox
                if _vox_error is not None:
                    used_fallback = True
                    logger.warning("VoxCPM 2 primary synthesis failed (%s). Activating resilient High-Fidelity Neural TTS fallback...", _vox_error)
                    try:
                        tracker.set_custom_note("⚠️ မူရင်း အသံ Engine အလုပ်များနေသဖြင့် အရန် အသံဖြင့် ထုတ်ပေးနေပါသည်…")
                    except Exception:
                        pass

                    from app.services.curated_voices import synthesize_curated_persona, get_curated_voice
                    persona_id = getattr(job, "preset_voice", None)
                    if not persona_id or not get_curated_voice(persona_id):
                        is_female = (
                            getattr(job, "edge_voice", "") == "my-MM-NilarNeural"
                            or getattr(job, "gender_preset", "") in ("m2f", "f2f")
                        )
                        persona_id = "mahnin" if is_female else "komin"

                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(
                        None,
                        functools.partial(
                            synthesize_curated_persona,
                            text=clean_text,
                            persona_id=persona_id,
                            output_wav=converted_wav_path,
                            speed=job.speed_preset or DEFAULT_SPEED,
                        )
                    )
                    logger.info("Resilient fallback audio generated successfully for job %s: %s", job.id, converted_wav_path)
            else:
                raise RuntimeError(
                    "Legacy trained voice model is no longer supported. "
                    "Please re-clone the voice with a short reference audio using VoxCPM2."
                )
        finally:
            await tracker.stop_ticker()

        # Transparency (fix 2026-10-29): the engine also falls back to
        # Edge TTS internally without raising, so read its flag — the
        # delivery note below must fire for that common path too, not
        # only when the engine raised into the except block above.
        if getattr(engine_inst, "last_fallback_used", False):
            used_fallback = True
        # Batch BU: the engine cloned from a trimmed reference when
        # the uploaded sample exceeded the 50 s Space cap — the
        # delivery note below must disclose that too.
        if getattr(engine_inst, "last_reference_trimmed", False):
            reference_trimmed = True

        # Stage 4: Preparing Output and Deducting Balance
        await tracker.update_step(4)
        duration_seconds = get_wav_duration_seconds(converted_wav_path)

        # Truncation gate (hunt #6): nothing compared the produced
        # audio against the submitted text, so a silently truncated
        # synthesis was charged and delivered as SUCCESS — the exact
        # 2026-10-29 incident (847 chars shipped in 23 s). Fail the
        # job instead (the failure path refunds any debit and tells
        # the user why) when the pace is physically implausible.
        implausible = _implausible_audio_reason(
            clean_text, duration_seconds, job.speed_preset
        )
        if implausible:
            raise RuntimeError(implausible)

        # Batch AJ (H5): cancel re-check BEFORE billing. A cancel that
        # landed during synthesis has already flipped the row to
        # CANCELLED (and the cancel path owns the refund semantics —
        # nothing has been charged yet, so there is nothing to
        # refund). Honour it: no charge, no delivery, no SUCCESS.
        if await _voice_job_cancelled(session, job):
            logger.info("Voice job %s was cancelled before billing; not charging or delivering.", job.id)
            await session.commit()
            return False

        # Batch AJ (H7): billing comes FIRST now. The old order copied
        # the audio into outputs/, set result_path and uploaded it to
        # the cloud BEFORE consume_seconds ran — a job whose charge
        # then failed (or a cancel that landed mid-flight) still
        # carried a downloadable result. Charge → persist → deliver.
        if not settings.is_admin(job.user_id):
            if used_fallback:
                # Batch CH (Item 2): a job delivered via the Edge
                # fallback is not the clone service the user's minutes
                # pay for — deduct NOTHING from the voice balance
                # (FREE monthly wallet / welcome / purchased), and the
                # seconds_consumed=0 write below keeps it out of the
                # Batch AN daily-cap counting as well (queue.py also
                # skips edge_fallback SUCCESS rows explicitly). The
                # Edge audio, the fallback caption note and the
                # edge_fallback engine marking all stay as they were.
                logger.info(
                    "Voice job %s delivered via Edge fallback; "
                    "skipping voice-balance deduction (Batch CH).",
                    job.id,
                )
            else:
                await consume_seconds(
                    session=session,
                    user_id=job.user_id,
                    seconds_to_consume=duration_seconds,
                    job_id=str(job.id),
                    note=f"Voice clone generation ({model.name}, {duration_seconds}s)",
                )
        else:
            logger.info("Admin voice job %s completed (%ss); skipping balance deduction.", job.id, duration_seconds)
            session.add(
                AuditLog(
                    admin_id=job.user_id,
                    action="admin.voice_clone",
                    target_type="job",
                    target_id=str(job.id),
                    details_json=f'{{"model": "{model.name}", "duration_seconds": {duration_seconds}}}',
                )
            )

        # Batch CH (Item 2): fallback-delivered jobs consumed no
        # voice minutes — record 0, not the delivered duration.
        job.seconds_consumed = 0 if used_fallback else duration_seconds
        if used_fallback:
            # Persist the fallback flag on the job result so the Mini App
            # job status/history can show the fallback notice as well.
            job.engine = "edge_fallback"
        if reference_trimmed:
            # Batch BU: persist the trim flag on the job result so the
            # Mini App job status/history can show the trim note as
            # well (mirrors the fallback flag above).
            job.reference_trimmed = True

        # Preserve the generated audio in permanent outputs storage for Mini App & API downloads.
        # A persistence failure now FAILS the job (the failure path
        # refunds the charge above) instead of pointing result_path at
        # the temp file the finally-block is about to delete.
        outputs_dir = settings.storage_root / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        permanent_wav_path = outputs_dir / f"{job.id}_{out_filename}"
        if converted_wav_path.resolve() != permanent_wav_path.resolve():
            shutil.copy2(converted_wav_path, permanent_wav_path)
        job.result_path = str(permanent_wav_path)

        # Upload audio result to Render Cloud server so Mini App & Bot can stream / download it
        final_audio_path = permanent_wav_path if permanent_wav_path.is_file() else converted_wav_path
        await sync_output_to_cloud(job.id, final_audio_path)

        # Persist the billing + result state and end the transaction
        # BEFORE delivery (audit fix 2026-11-03): the Telegram sends
        # below are network I/O and must not hold the billing
        # transaction's pooled connection open. The SUCCESS status
        # write after delivery gets its own short transaction.
        await session.commit()

        # Batch AJ (H5): cancel re-check AFTER the billing commit,
        # before delivery. A cancel in this window was told the job
        # was cancelled; the charge above is refunded in full and the
        # row stays CANCELLED — never delivered, never SUCCESS.
        if await _voice_job_cancelled(session, job):
            logger.info("Voice job %s was cancelled after billing; refunding and not delivering.", job.id)
            from app.services.ledger import refund_job_seconds

            await refund_job_seconds(session, job.user_id, str(job.id))
            job.result_path = None
            await session.commit()
            return False

        # Send completion audio or notification alert to user via Telegram Bot
        if bot and target_chat_id:
            caption_model_name = display_name or getattr(model, "name", "Standard")
            fallback_note = t("worker_fallback_note", lang) if used_fallback else ""
            trim_note = t("worker_ref_trim_note", lang) if reference_trimmed else ""
            if getattr(job, "auto_deliver", False):
                try:
                    audio_source_path = permanent_wav_path if permanent_wav_path.is_file() else converted_wav_path
                    if audio_source_path.stat().st_size > TELEGRAM_SEND_LIMIT_BYTES:
                        # Over the Bot API send cap (hunt #3 B1): a
                        # send_audio attempt would fail inside Telegram
                        # and the user heard nothing at all. Tell them
                        # plainly and route to the Mini App, where the
                        # file stays downloadable; job still SUCCESS.
                        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
                        app_url = settings.mini_app_url
                        kb = InlineKeyboardMarkup(inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text=t("clone_btn_miniapp_listen", lang),
                                    web_app=WebAppInfo(url=f"{app_url}?tab=tab-voice")
                                )
                            ]
                        ])
                        await bot.send_message(
                            chat_id=target_chat_id,
                            text=t("clone_audio_too_large_text", lang, job_id=job.id) + fallback_note + trim_note,
                            reply_markup=kb,
                            parse_mode="HTML",
                        )
                    else:
                        # Branded delivery name only — the file on disk
                        # keeps out_filename (2026-11-03 naming scheme).
                        audio_file = FSInputFile(
                            str(audio_source_path),
                            filename=branded_download_filename(
                                "Voice",
                                Path(audio_source_path).suffix.lstrip(".") or "wav",
                                title=caption_model_name,
                                job_id=job.id,
                            ),
                        )
                        caption = t(
                            "status_completed_caption",
                            lang,
                            completed_status=t("status_completed", lang),
                            model_name=html.escape(str(caption_model_name)),
                            duration_seconds=duration_seconds,
                        ) + fallback_note + trim_note
                        await bot.send_audio(
                            chat_id=target_chat_id,
                            audio=audio_file,
                            caption=caption,
                            parse_mode="HTML",
                        )
                    if target_msg_id:
                        await safe_edit_status(bot, target_chat_id, target_msg_id, t("status_completed", lang))
                except Exception as send_err:
                    logger.warning("Failed to deliver audio via Telegram for job %s: %s", job.id, send_err)
            else:
                # Mini App Job: Send push notification alert with inline action buttons
                try:
                    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
                    app_url = settings.mini_app_url
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=t("clone_btn_miniapp_listen", lang),
                                web_app=WebAppInfo(url=f"{app_url}?tab=tab-voice")
                            ),
                            InlineKeyboardButton(
                                text=t("clone_btn_send_audio", lang),
                                callback_data=f"send_audio_{job.id}"
                            )
                        ]
                    ])
                    notify_text = t(
                        "worker_notify_audio_text",
                        lang,
                        model_name=html.escape(str(caption_model_name)),
                        duration=duration_seconds,
                        job_id=job.id,
                    ) + fallback_note + trim_note
                    await bot.send_message(
                        chat_id=target_chat_id,
                        text=notify_text,
                        reply_markup=kb,
                        parse_mode="HTML",
                    )
                except Exception as notify_err:
                    logger.warning("Could not send completion alert to user %s: %s", target_chat_id, notify_err)
                # Also flip the job's progress/status card to its completed
                # state. Without this, bot-submitted jobs kept showing the
                # step-4 "in progress" stepper forever, so the chat looked
                # unfinished even though the job had succeeded (the direct
                # delivery branch above already did this edit).
                if target_msg_id:
                    await safe_edit_status(bot, target_chat_id, target_msg_id, t("status_completed", lang))

        # Batch AJ (H5): the SUCCESS write is CONDITIONAL on the row
        # still being RUNNING — a cancel that landed during delivery
        # must never be overwritten back to SUCCESS.
        _succ_res = await session.execute(
            update(Job)
            .where(Job.id == job.id, Job.status == JobStatus.RUNNING.value)
            .values(status=JobStatus.SUCCESS.value, finished_at=utc_now())
            .execution_options(synchronize_session=False)
        )
        if not _succ_res.rowcount:
            await session.rollback()
            if await _voice_job_cancelled(session, job):
                logger.info("Voice job %s was cancelled during delivery; refunding instead of SUCCESS.", job.id)
                if not settings.is_admin(job.user_id):
                    from app.services.ledger import refund_job_seconds

                    await refund_job_seconds(session, job.user_id, str(job.id))
                job.result_path = None
                await session.commit()
                return False
            # Not a cancel (unexpected state): keep the job completable
            # rather than stranding it — fall back to the plain write.
            job.status = JobStatus.SUCCESS.value
            job.finished_at = utc_now()
            await session.commit()
        else:
            await session.commit()
            job.status = JobStatus.SUCCESS.value
            job.finished_at = utc_now()

        # Batch DP: a resumed parked job that has now completed —
        # retire its choice prompt's buttons.
        if _dp_choice_state is not None and bot is not None:
            await _vc.clear_prompt_for_job(bot, job)

        logger.info("Successfully completed voice job %s (duration=%ss)", job.id, duration_seconds)
        return True

    except Exception as exc:
        # Batch AJ (H5): a failure racing a cancel must not overwrite
        # CANCELLED with FAILED either — the cancel path already
        # refunded and told the user; the refund attempt below stays
        # (net-aware: it can only return the unrefunded delta).
        try:
            await session.refresh(job)
        except Exception:
            pass
        if job.status == JobStatus.CANCELLED.value:
            if not settings.is_admin(job.user_id):
                from app.services.ledger import refund_job_seconds

                try:
                    await refund_job_seconds(session, job.user_id, str(job.id))
                    await session.commit()
                except Exception:
                    pass
            return False
        logger.exception("Voice job %s failed: %s", job.id, exc)
        # Store/post only the curated message (audit fix 2026-11-03):
        # the raw exception text is in the log line above; the stored
        # error_message is rendered verbatim by the Mini App and the
        # status card below quotes it to the user.
        user_error = _user_facing_worker_error(exc, lang)
        job.status = JobStatus.FAILED.value
        job.error_message = user_error
        job.finished_at = utc_now()

        # Refund if any debit occurred for this job
        if not settings.is_admin(job.user_id):
            from app.services.ledger import refund_job_seconds
            await refund_job_seconds(session, job.user_id, str(job.id))

        await session.commit()

        safe_exc = html.escape(user_error)
        await safe_edit_status(bot, target_chat_id, target_msg_id, f"{t('status_failed', lang)}\n<i>({safe_exc})</i>")
        return False

    finally:
        if tracker:
            await tracker.stop_ticker()
        # Safe cleanup of intermediate files
        shutil.rmtree(temp_dir, ignore_errors=True)
        if getattr(job, "source_audio_path", None):
            try:
                p = Path(job.source_audio_path)
                # Batch DP: a PARKED job keeps its reference (the
                # resume needs it); a finished job's preserved
                # dp_refs copy is cleaned up like any temp input.
                if p.is_file() and not parked and ("temp" in str(p) or "dp_refs" in str(p)):
                    p.unlink(missing_ok=True)
            except Exception:
                pass


# Batch AJ (M11): a single voice job may run at most this long.
# Synthesis normally takes seconds-to-minutes; a hung engine call
# previously pinned the ONLY worker slot (and the job, and its
# concurrency slot) forever — the worker loop is serial.
VOICE_JOB_DEADLINE_SECONDS = 1800


async def _fail_timed_out_job(job_id: str) -> None:
    """Mark a deadline-killed job FAILED and refund any partial
    charge, in a fresh session (the processing session was cancelled
    mid-flight and cannot be trusted)."""
    try:
        async with AsyncSessionLocal() as session:
            job = await session.get(Job, job_id)
            if job is None or job.status not in (
                JobStatus.QUEUED.value,
                JobStatus.RUNNING.value,
            ):
                return
            job.status = JobStatus.FAILED.value
            job.error_message = "Voice job timed out (worker deadline exceeded)"
            job.finished_at = utc_now()
            if not settings.is_admin(job.user_id):
                from app.services.ledger import refund_job_seconds

                await refund_job_seconds(session, job.user_id, str(job.id))
            await session.commit()
    except Exception as exc:
        logger.warning("Could not fail timed-out job %s: %s", job_id, exc)


async def claim_and_process_next_job(
    session: AsyncSession | None = None,
    bot: Bot | None = None,
    engine_filter: str | None = "voxcpm",
) -> bool:
    """Atomically claim the oldest QUEUED job matching engine_filter and process it.

    Session scoping (audit fix 2026-11-03): with no session passed
    (the worker loop), the claim runs in its own short-lived session
    that closes the moment the job is claimed, and processing runs in
    a FRESH session — the old shape had the poll loop hand ONE
    session through the claim and the entire minutes-long processing.
    A caller that passes a session (direct callers, tests) keeps the
    old single-session behaviour exactly.
    """
    from app.services.queue import claim_next_queued_job

    if session is not None:
        job = await claim_next_queued_job(session, engine_filter=engine_filter)
        if not job:
            return False
        try:
            return await asyncio.wait_for(
                process_single_job(session=session, job=job, bot=bot),
                timeout=VOICE_JOB_DEADLINE_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error("Voice job %s exceeded the %ss worker deadline; failing it.", job.id, VOICE_JOB_DEADLINE_SECONDS)
            await _fail_timed_out_job(str(job.id))
            return False

    async with AsyncSessionLocal() as claim_session:
        job = await claim_next_queued_job(claim_session, engine_filter=engine_filter)
        if not job:
            return False
        job_id = job.id

    async with AsyncSessionLocal() as process_session:
        job = await process_session.get(Job, job_id)
        if job is None:
            return False
        try:
            return await asyncio.wait_for(
                process_single_job(session=process_session, job=job, bot=bot),
                timeout=VOICE_JOB_DEADLINE_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error("Voice job %s exceeded the %ss worker deadline; failing it.", job_id, VOICE_JOB_DEADLINE_SECONDS)
            await _fail_timed_out_job(str(job_id))
            return False


def _boot_cutoff_sweep_due(
    started_monotonic: float,
    last_sweep_monotonic: float,
    now_monotonic: float,
    *,
    grace_seconds: float = 600.0,
    interval_seconds: float = 300.0,
) -> bool:
    """Whether the periodic orphan sweep should run now (hunt #7).

    The sweep must not run during the first `grace_seconds` of
    uptime: during a deploy overlap the previous instance may still
    be legitimately finishing a job it started before this process
    booted. After the grace window the old instance is certainly
    gone, so any RUNNING row started before this process booted is
    orphaned. Thereafter the sweep repeats every `interval_seconds`.
    """
    return (
        now_monotonic - started_monotonic >= grace_seconds
        and now_monotonic - last_sweep_monotonic >= interval_seconds
    )


async def run_worker_loop(bot: Bot | None = None, poll_interval: float = 2.0, engine_filter: str = "voxcpm") -> None:
    """Continuously poll for QUEUED jobs (defaulting to VoxCPM cloud engine) and process them."""
    logger.info("Starting background audio processing worker loop (engine_filter=%s)...", engine_filter)

    # Startup recovery: sweep orphaned RUNNING jobs before starting polling
    from app.services.recovery import recover_stale_running_jobs
    try:
        async with AsyncSessionLocal() as session:
            recovered = await recover_stale_running_jobs(session=session, bot=bot)
            if recovered:
                logger.info("Recovered %d orphaned stale jobs on worker startup: %s", len(recovered), recovered)
    except Exception as e:
        logger.warning("Error during initial stale job recovery sweep: %s", e)

    # Periodic orphan sweep (hunt #7): the startup sweep alone uses
    # the fixed stale timeout, so a job that was mid-flight when the
    # previous process died — younger than the timeout at this boot —
    # was skipped and then never re-checked: it sat RUNNING (and its
    # owner saw "processing" forever) until the NEXT restart. Once
    # this loop has outlived the deploy-overlap grace window, re-run
    # recovery with a boot-relative timeout, whose cutoff is this
    # loop's own start: rows started before it are orphaned, rows
    # this loop claimed have a later started_at and are untouched.
    import time as _time

    _loop_started = _time.monotonic()
    _last_stale_sweep = 0.0

    while True:
        try:
            _now_m = _time.monotonic()
            if _boot_cutoff_sweep_due(_loop_started, _last_stale_sweep, _now_m):
                _last_stale_sweep = _now_m
                try:
                    async with AsyncSessionLocal() as session:
                        recovered = await recover_stale_running_jobs(
                            session=session,
                            bot=bot,
                            stale_timeout_seconds=int(_now_m - _loop_started),
                        )
                        if recovered:
                            logger.info("Periodic sweep recovered %d stranded jobs: %s", len(recovered), recovered)
                except Exception as e:
                    logger.warning("Error during periodic stale job recovery sweep: %s", e)
            # No loop-owned session (audit fix 2026-11-03):
            # claim_and_process_next_job scopes its own short-lived
            # claim session and a fresh processing session.
            processed = await claim_and_process_next_job(bot=bot, engine_filter=engine_filter)
            if not processed:
                await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            logger.info("Worker loop cancelled, shutting down.")
            break
        except Exception as e:
            logger.error("Error in worker loop: %s", e)
            await asyncio.sleep(poll_interval)

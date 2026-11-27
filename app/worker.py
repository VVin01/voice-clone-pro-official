from __future__ import annotations

import asyncio
import html
import json
import logging
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any
from aiogram import Bot
from aiogram.types import FSInputFile
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.models.enums import AccountCategory, JobStatus, JobType
from app.models.schema import AuditLog, Job, User, VoiceModel, utc_now
from app.services.audio import (
    GENDER_PITCH_MAP,
    concat_wav_files,
    generate_edge_tts,
    get_output_filename,
    get_wav_duration_seconds,
    is_wav_edge_clipped,
    normalize_audio_to_wav,
)
from app.services.download_names import branded_download_filename
from app.services.i18n import t
from app.services.billing import (
    record_cost_telemetry,
    refund_or_release_job,
    release_job_reservation,
    settle_job_reservation,
)
from app.services.ledger import consume_seconds, get_minute_balance
from app.services.models import get_authorized_model
from app.services.progress import JobProgressTracker, render_stepper_progress, safe_edit_status
from app.services import hf_pool_ledger as _hf_ledger
from app.services import voice_choice as _vc
from app.services.speed import DEFAULT_SPEED, DEFAULT_TEMPO, resolve_speed_tempo
from app.services.voice_chunking import (
    is_clone_session_slices_enabled,
    plan_session_slices,
    plan_text_chunks,
)

logger = logging.getLogger(__name__)

# Telegram Bot API refuses bot uploads above 50 MB on official api.telegram.org,
# and lifts that cap to 2,000 MB when TELEGRAM_BOT_API_URL (Local Bot API) is
# active. Voice WAVs run ~5.8 MB/min, so delivery checks the live limit before
# sending (hunt #3 B1).
_DEFAULT_TELEGRAM_SEND_LIMIT_BYTES = 50 * 1024 * 1024
TELEGRAM_SEND_LIMIT_BYTES = _DEFAULT_TELEGRAM_SEND_LIMIT_BYTES


def _effective_telegram_send_limit_bytes() -> int:
    if TELEGRAM_SEND_LIMIT_BYTES != _DEFAULT_TELEGRAM_SEND_LIMIT_BYTES:
        return TELEGRAM_SEND_LIMIT_BYTES
    from app.services.telegram_bot_factory import get_telegram_max_upload_bytes

    return get_telegram_max_upload_bytes()


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


def _voice_retention_hours() -> int:
    return max(1, int(getattr(settings, "media_retention_days", 1) or 1)) * 24


def _format_voice_delivery_duration(seconds: float | int | None) -> str:
    try:
        total = int(round(float(seconds or 0)))
    except (TypeError, ValueError):
        total = 0
    if total <= 0:
        return "—"
    hours, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def _format_voice_retention_hours(hours: int, lang: str) -> str:
    raw = str(int(hours))
    if (lang or "my").strip().lower().startswith("my"):
        return raw.translate(str.maketrans("0123456789", "၀၁၂၃၄၅၆၇၈၉"))
    return raw


def build_voice_completion_message(
    lang: str,
    *,
    model_name: str,
    duration_seconds: float | int | None,
    job_id: str,
    retention_hours: int = 24,
    used_fallback: bool = False,
    reference_trimmed: bool = False,
) -> str:
    """Build the compact Voice Clone completion notification text (Part F)."""
    text = t(
        "worker_notify_audio_text",
        lang,
        model_name=html.escape(str(model_name or "")),
        duration=_format_voice_delivery_duration(duration_seconds),
        job_id=job_id,
        retention_hours=_format_voice_retention_hours(retention_hours, lang),
    )
    if used_fallback:
        text += t("worker_fallback_note", lang)
    if reference_trimmed:
        text += t("worker_ref_trim_note", lang)
    return text


def build_voice_direct_caption(
    lang: str,
    *,
    model_name: str,
    duration_seconds: float | int | None,
    job_id: str,
    retention_hours: int = 24,
    used_fallback: bool = False,
    reference_trimmed: bool = False,
) -> str:
    """Build the compact direct-delivery Voice Clone audio caption (Part F)."""
    text = t(
        "status_completed_caption",
        lang,
        model_name=html.escape(str(model_name or "")),
        duration=_format_voice_delivery_duration(duration_seconds),
        job_id=job_id,
        retention_hours=_format_voice_retention_hours(retention_hours, lang),
    )
    if used_fallback:
        text += t("worker_fallback_note", lang)
    if reference_trimmed:
        text += t("worker_ref_trim_note", lang)
    return text


def build_voice_completion_keyboard(lang: str, *, job_id: str, app_url: str):
    """Build the 1-button-per-full-width-row Voice Clone keyboard (Part F)."""
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=t("clone_btn_send_audio", lang),
                callback_data=f"send_audio_{job_id}",
            )
        ],
        [
            InlineKeyboardButton(
                text=t("clone_btn_miniapp_listen", lang),
                web_app=WebAppInfo(url=f"{app_url}?tab=tab-voice"),
            )
        ],
    ])


def build_voice_failure_message(
    lang: str,
    *,
    job_id: str,
    reason: str,
    refunded: bool = True,
) -> str:
    """Build the compact Voice Clone failure message (Part F)."""
    key = "worker_failure_notify_refunded_text" if refunded else "worker_failure_notify_text"
    return t(
        key,
        lang,
        job_id=job_id,
        reason=html.escape(str(reason or "").strip()),
    )


_INSUFFICIENT_BALANCE_RE = re.compile(
    r"insufficient balance: requested (\d+)s, available (\d+)s", re.IGNORECASE
)


def _format_free_balance_label(seconds: int) -> str:
    sec = max(0, int(seconds or 0))
    if sec >= 60 and sec % 60 == 0:
        return f"{sec // 60}m"
    if sec >= 60:
        return f"{sec // 60}m {sec % 60}s"
    return f"{sec}s"


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
    (the truncation gate's Burmese reason or quota fail-fast card) pass
    through untouched.
    """
    raw = str(exc or "")
    # Already-curated copy: the implausible-audio gate or quota fail-fast card
    if "တိုလွန်းနေပါသည်" in raw or raw.startswith("⏸️"):
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
    if (
        isinstance(exc, FileNotFoundError)
        or "reference audio" in low
        or "corrupt" in low
        or "unreadable" in low
        or "cannot read" in low
        or "soundfile" in low
    ):
        return t("worker_error_clone_corrupt_ref", lang)
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timed out" in low or "timeout" in low:
        return t("worker_error_timeout", lang)
    if any(
        needle in low
        for needle in (
            "500", "502", "503", "504", "5xx",
            "bad gateway", "service unavailable", "internal server error",
        )
    ):
        return t("worker_error_clone_server_5xx", lang)
    if any(
        needle in low
        for needle in (
            "voxcpm", "synthes", "edge tts", "tts", "provider",
            "huggingface", "hugging face", "gradio", "upstream",
        )
    ):
        return t("worker_error_engine_failure", lang)
    return t("worker_error_generic", lang)


async def sync_output_to_cloud(job_id: str, file_path: Path) -> bool:
    """Upload generated output file from Local Worker to Render Cloud so Mini App & Bot can serve it."""
    import os
    # Primary web/worker runs co-located on shared persistent storage: no remote upload needed.
    if getattr(settings, "instance_primary", True):
        return True
    # Hardening (hunt F8): without a dedicated WORKER_TOKEN there is
    # no remote worker to sync for — the co-located worker writes
    # straight into shared storage, so this is a no-op. The bot
    # token is never used as an upload credential anymore.
    worker_token = (getattr(settings, "worker_token", "") or "").strip()
    if not worker_token:
        return True
    cloud_url = os.getenv("RENDER_EXTERNAL_URL", "").strip() or os.getenv("MINI_APP_URL", "").strip() or "https://voice-clone-studio-c1er.onrender.com"
    if not file_path or not file_path.is_file():
        return False
    is_mp4 = file_path.suffix.lower() == ".mp4"
    mime_type = "video/mp4" if is_mp4 else "audio/wav"
    upload_timeout = 300.0 if is_mp4 else 90.0
    try:
        import httpx
        async with httpx.AsyncClient(timeout=upload_timeout) as client:
            with open(file_path, "rb") as f:
                files = {"file": (file_path.name, f, mime_type)}
                headers = {
                    "X-Worker-Token": worker_token,
                    "X-Worker-Defer-Status": "1",
                }
                url = f"{cloud_url}/api/internal/worker/upload-result/{job_id}"
                resp = await client.post(url, files=files, headers=headers)
                if resp.is_success:
                    logger.info("✅ Result for job %s successfully uploaded to Render Cloud server!", job_id)
                    return True
                logger.warning("Cloud upload for job %s returned status %s: %s", job_id, resp.status_code, resp.text)
                return False
    except Exception as e:
        logger.warning("Failed to sync output to cloud for job %s: %s", job_id, e)
        return False


_RECAP_BUNDLE_SKIP_DIRS = frozenset({"work", "tts_parts", "tts_parts_short", "video_parts"})
_RECAP_METADATA_MAX_BYTES = 5 * 1024 * 1024
_RECAP_MIME_TYPES = {
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".srt": "text/plain; charset=utf-8",
    ".ass": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json",
}


def collect_recap_bundle_files(
    job_dir: Path,
    *,
    video_path: str | Path | None = None,
    audio_path: str | Path | None = None,
    srt_path: str | Path | None = None,
    ass_path: str | Path | None = None,
    script_path: str | Path | None = None,
    part_paths: list[str | Path] | None = None,
) -> list[tuple[str, Path, str]]:
    """Collect the deliverable files under ``job_dir`` for a finished recap job.

    Returns a deduplicated list of ``(rel_posix_path, abs_path, role)`` strictly
    inside ``job_dir``, omitting intermediate scratch/work directories and raw
    downloaded source media.
    """
    base_dir = Path(job_dir).resolve()
    if not base_dir.is_dir():
        return []

    items: list[tuple[str, Path, str]] = []
    seen_paths: set[Path] = set()

    def _add(candidate: str | Path | None, role: str) -> None:
        if not candidate:
            return
        try:
            p = Path(candidate).resolve()
        except OSError:
            return
        if not p.is_file() or p in seen_paths:
            return
        try:
            rel = p.relative_to(base_dir)
        except ValueError:
            logger.warning("Skipping recap bundle file outside job_dir %s: %s", base_dir, p)
            return
        if any(part.lower() in _RECAP_BUNDLE_SKIP_DIRS for part in rel.parts[:-1]):
            return
        seen_paths.add(p)
        items.append((rel.as_posix(), p, role))

    def _in_work_tree(p: Path) -> bool:
        try:
            rel = p.resolve().relative_to(base_dir)
        except (OSError, ValueError):
            return True
        return any(part.lower() in _RECAP_BUNDLE_SKIP_DIRS for part in rel.parts[:-1])

    _add(video_path, "video")
    _add(audio_path, "audio")
    _add(srt_path, "srt")
    for srt_name in ("final_edge_tts_smart_sync.srt", "recap_myanmar_synced.srt"):
        for cand in sorted(base_dir.rglob(srt_name)):
            if not _in_work_tree(cand):
                _add(cand, "srt")
    _add(ass_path, "ass")
    for ass_name in ("final_edge_tts_smart_sync.ass", "recap_myanmar_synced.ass"):
        for cand in sorted(base_dir.rglob(ass_name)):
            if not _in_work_tree(cand):
                _add(cand, "ass")
    _add(script_path, "script")
    for p_item in (part_paths or []):
        _add(p_item, "part")
    parts_dir = base_dir / "parts"
    if parts_dir.is_dir():
        for pf in sorted(parts_dir.glob("recap_part_*.mp4")):
            _add(pf, "part")
    for plan_cand in sorted(base_dir.rglob("smart_sync_plan_edge_tts.json")):
        if _in_work_tree(plan_cand):
            continue
        try:
            if plan_cand.stat().st_size <= _RECAP_METADATA_MAX_BYTES:
                _add(plan_cand, "metadata")
        except OSError:
            continue
    return items


async def sync_recap_bundle_to_cloud(
    job_id: str,
    job_dir: Path,
    bundle_files: list[tuple[str, Path, str]],
    *,
    max_attempts: int = 3,
    retry_delay: float = 1.5,
) -> dict[str, Any]:
    """Upload a finished recap job's deliverable bundle from a fleet worker to the web service.

    Uses the exact same primary/token gate as ``sync_output_to_cloud`` so
    single-process web runs perform zero HTTP uploads. Returns a structured
    status dict including ``synced`` (bool), ``skipped`` (bool), and
    ``remote_paths`` (role -> web-canonical saved path).
    """
    if getattr(settings, "instance_primary", True):
        return {"synced": True, "skipped": True, "uploaded": [], "remote_paths": {}, "remote_paths_by_rel": {}}
    worker_token = (getattr(settings, "worker_token", "") or "").strip()
    if not worker_token:
        return {"synced": True, "skipped": True, "uploaded": [], "remote_paths": {}, "remote_paths_by_rel": {}}

    cloud_url = (
        os.getenv("RENDER_EXTERNAL_URL", "").strip()
        or os.getenv("MINI_APP_URL", "").strip()
        or (getattr(settings, "mini_app_url", "") or "").strip()
        or "https://voice-clone-studio-c1er.onrender.com"
    ).rstrip("/")

    if not bundle_files:
        msg = f"No deliverable files found in {job_dir} for recap job {job_id}"
        logger.error("Recap cloud sync aborted for %s: %s", job_id, msg)
        return {"synced": False, "skipped": False, "uploaded": [], "error": msg, "remote_paths": {}, "remote_paths_by_rel": {}}

    headers: dict[str, str] = {"X-Worker-Token": worker_token}
    cf_secret = (getattr(settings, "cf_origin_secret", "") or "").strip()
    if cf_secret:
        headers["X-Origin-Secret"] = cf_secret

    attempts = max(1, int(max_attempts))
    uploaded: list[str] = []
    remote_paths: dict[str, str] = {}
    remote_paths_by_rel: dict[str, str] = {}

    import httpx

    url = f"{cloud_url}/api/internal/worker/upload-recap-file/{job_id}"
    for rel_path, local_path, role in bundle_files:
        if not local_path.is_file():
            err_msg = f"Missing recap deliverable file for upload: {local_path}"
            logger.error("Recap cloud sync failed for %s: %s", job_id, err_msg)
            return {
                "synced": False,
                "skipped": False,
                "uploaded": uploaded,
                "failed_file": rel_path,
                "error": err_msg,
                "remote_paths": remote_paths,
                "remote_paths_by_rel": remote_paths_by_rel,
            }

        ext = local_path.suffix.lower()
        mime_type = _RECAP_MIME_TYPES.get(ext, "application/octet-stream")
        upload_timeout = 300.0 if ext == ".mp4" else 90.0
        last_err = ""
        file_ok = False

        for attempt in range(1, attempts + 1):
            try:
                async with httpx.AsyncClient(timeout=upload_timeout) as client:
                    with open(local_path, "rb") as fh:
                        resp = await client.post(
                            url,
                            files={"file": (local_path.name, fh, mime_type)},
                            data={"rel_path": rel_path, "role": role},
                            headers=headers,
                        )
                if resp.is_success:
                    try:
                        payload = resp.json()
                    except Exception:
                        payload = {}
                    saved_path = str(payload.get("saved_path") or "").strip()
                    if saved_path:
                        remote_paths_by_rel[rel_path] = saved_path
                        if role and role not in remote_paths:
                            remote_paths[role] = saved_path
                    uploaded.append(rel_path)
                    file_ok = True
                    break
                last_err = f"HTTP {resp.status_code}: {(resp.text or '')[:300]}"
                if resp.status_code in (400, 403, 404, 409, 413):
                    logger.error(
                        "Recap cloud upload rejected permanently for job %s (%s): %s",
                        job_id, rel_path, last_err,
                    )
                    break
                logger.warning(
                    "Recap cloud upload attempt %d/%d failed for job %s (%s): %s",
                    attempt, attempts, job_id, rel_path, last_err,
                )
            except Exception as exc:
                last_err = str(exc) or exc.__class__.__name__
                logger.warning(
                    "Recap cloud upload attempt %d/%d error for job %s (%s): %s",
                    attempt, attempts, job_id, rel_path, last_err,
                )

            if attempt < attempts and retry_delay > 0:
                await asyncio.sleep(float(retry_delay) * attempt)

        if not file_ok:
            logger.error(
                "Recap cloud upload FAILED after %d attempt(s) for job %s (%s): %s",
                attempts, job_id, rel_path, last_err,
            )
            return {
                "synced": False,
                "skipped": False,
                "uploaded": uploaded,
                "failed_file": rel_path,
                "error": last_err or "upload_failed",
                "remote_paths": remote_paths,
                "remote_paths_by_rel": remote_paths_by_rel,
            }

    logger.info(
        "✅ Recap bundle for job %s uploaded to web (%d files: %s)",
        job_id, len(uploaded), ", ".join(uploaded),
    )
    return {
        "synced": True,
        "skipped": False,
        "uploaded": uploaded,
        "remote_paths": remote_paths,
        "remote_paths_by_rel": remote_paths_by_rel,
    }


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
    allow_fallback: bool = False,
    gender_preset: str = "",
    checkpoint_dir: Path | None = None,
    checkpoint_progress: dict[str, Any] | None = None,
) -> Path:
    """Batch CH + Prosody Phase 1 + HF Pool Phase 1: VoxCPM synthesis for a voice-clone job.

    - Unconditional no-fallback (`allow_fallback=False`) on every chunk and tier (Part 2 #1).
    - Bounded retry: 0 extra walks on quota/congestion; at most 1 retry after 5 s backoff
      on transient transport/5xx while solvent tokens remain (Part 2 #3c).
    - Standalone Paid chunk checkpointing (`storage/dp_refs/<job_id>/`) so resume reuses
      completed `chunk01..chunk(N-1).wav`, `active_ref.wav`, `reference_transcript`, and
      `pinned_space` without re-synthesis (Part 3 #2).
    - Watermark Conservation (`<= 40%`): skips optional GPU re-generations (edge-clip
      re-gens and prosody-outlier retries drop to 0) while local rubberband still runs (Part 7).
    """
    import inspect as _inspect
    import statistics as _statistics
    from app.services import clone_prosody as _cp
    from app.services import voxcpm_engine as _vx_mod
    from app.services.voxcpm_engine import (
        DEFAULT_CLONE_FIDELITY_CONTROL,
        VoxCPMQuotaExhaustedError,
        has_solvent_voxcpm_tokens,
        is_voxcpm_capacity_or_congestion_error,
        master_voxcpm_audio,
    )

    _pool_metrics = _hf_ledger.compute_pool_metrics()
    _allow_optional_regen = bool(_pool_metrics.allow_optional_gpu_regen)
    _max_prosody_retries = _cp.MAX_PROSODY_RETRIES_PER_JOB if _allow_optional_regen else 0

    try:
        _synth_sig = _inspect.signature(engine.synthesize)
        _accepts_var_kw = any(
            p.kind == _inspect.Parameter.VAR_KEYWORD for p in _synth_sig.parameters.values()
        )
        _accepts_allow_fb = _accepts_var_kw or ("allow_fallback" in _synth_sig.parameters)
        _accepts_tier = _accepts_var_kw or ("tier" in _synth_sig.parameters)
        _accepts_session = _accepts_var_kw or ("session_mode" in _synth_sig.parameters)
        _accepts_pin_override = _accepts_var_kw or ("pin_space_override" in _synth_sig.parameters)
    except Exception:
        _accepts_allow_fb = True
        _accepts_tier = True
        _accepts_session = True
        _accepts_pin_override = True

    _dp_kwargs: dict = {}
    if tier != "free" and _accepts_tier:
        _dp_kwargs["tier"] = tier
    if _accepts_allow_fb:
        # Part 2 (#1): Edge/persona fallback is unconditionally forbidden on clone paths.
        _dp_kwargs["allow_fallback"] = False

    def _call_unit_with_bounded_retry(
        call_kw: dict[str, Any], *, req_gate_s: float | None = None
    ) -> Any:
        try:
            return engine.synthesize(**call_kw)
        except Exception as exc:
            if checkpoint_progress is not None:
                checkpoint_progress["pinned_space"] = getattr(engine, "pinned_space", None)
            if (
                isinstance(exc, (VoxCPMQuotaExhaustedError, FileNotFoundError, ValueError, TypeError))
                or is_voxcpm_capacity_or_congestion_error(exc)
            ):
                raise
            if not getattr(engine, "hf_tokens", None):
                raise
            try:
                _has_solvent = (
                    has_solvent_voxcpm_tokens(engine, gate_s=req_gate_s)
                    if req_gate_s is not None
                    else has_solvent_voxcpm_tokens(engine)
                )
            except TypeError:
                _has_solvent = has_solvent_voxcpm_tokens(engine)
            if not _has_solvent:
                raise
            backoff_s = float(getattr(_vx_mod, "TRANSIENT_RETRY_BACKOFF_SECONDS", 5.0) or 0.0)
            if backoff_s > 0:
                time.sleep(backoff_s)
            return engine.synthesize(**call_kw)

    norm_gender = str(gender_preset or "").strip().lower()
    is_gender_flip = norm_gender in ("m2f", "f2m")
    is_custom_clone = bool(reference_wav is not None and not preset_voice)
    _engine_pin_eligible = (
        getattr(engine, "token_bind_index", None) is None
        and bool(getattr(engine, "pin_space_enabled", True))
    )
    use_session_mode = bool(is_clone_session_slices_enabled() and _engine_pin_eligible)

    manifest: dict[str, Any] = {}
    if checkpoint_dir is not None and (checkpoint_dir / "manifest.json").is_file():
        try:
            manifest = json.loads((checkpoint_dir / "manifest.json").read_text(encoding="utf-8"))
        except Exception:
            manifest = {}

    if manifest.get("pinned_space") and getattr(engine, "pinned_space", None) is None:
        try:
            engine.pinned_space = str(manifest["pinned_space"])
        except Exception:
            pass
    if manifest.get("space_family_switched"):
        try:
            engine.space_family_switched = True
        except Exception:
            pass

    active_ref_wav = Path(reference_wav) if reference_wav is not None else None
    active_ref_secs = float(reference_seconds or 0.0)
    ref_prosody: _cp.ProsodyStats | None = None

    ckpt_ref_file = (checkpoint_dir / "active_ref.wav") if checkpoint_dir is not None else None
    if ckpt_ref_file is not None and ckpt_ref_file.is_file() and ckpt_ref_file.stat().st_size > 0:
        active_ref_wav = ckpt_ref_file
        active_ref_secs = float(manifest.get("active_ref_secs") or active_ref_secs or 0.0)
        if not reference_transcript and manifest.get("reference_transcript"):
            reference_transcript = str(manifest["reference_transcript"])
        if is_custom_clone and not is_gender_flip and not reference_transcript and not control_instruction:
            control_instruction = DEFAULT_CLONE_FIDELITY_CONTROL
        if not use_session_mode:
            ref_prosody = _cp.measure_prosody(active_ref_wav, text=reference_transcript)
    elif is_custom_clone and active_ref_wav is not None and active_ref_wav.is_file():
        active_ref_wav, sliced_secs, ref_prosody = _cp.select_steady_reference_slice(
            active_ref_wav,
            output_wav.parent,
            target_sec=_cp.REFERENCE_SLICE_SECONDS,
        )
        if sliced_secs > 0:
            active_ref_secs = sliced_secs
        elif active_ref_secs <= 0:
            active_ref_secs = ref_prosody.duration_sec

        if not is_gender_flip and not reference_transcript:
            reference_transcript = _cp.transcribe_reference_slice_checked(
                active_ref_wav,
                max(active_ref_secs, 0.5),
            )
        if not is_gender_flip and not reference_transcript and not control_instruction:
            control_instruction = DEFAULT_CLONE_FIDELITY_CONTROL
    elif (not use_session_mode) and active_ref_wav is not None and active_ref_wav.is_file():
        ref_prosody = _cp.measure_prosody(active_ref_wav, text=reference_transcript)

    effective_ref_arg = active_ref_wav if active_ref_wav is not None else reference_wav

    # Locked-timbre clones (ref + ref_transcript) ignore control_instruction;
    # avoid subtracting phantom control tokens from chunk capacity.
    effective_planning_control = (
        "" if (effective_ref_arg and reference_transcript) else (control_instruction or "")
    )

    if use_session_mode:
        from app.services.speed import resolve_speed_tempo

        if (
            manifest.get("text") == text
            and manifest.get("session_mode") is True
            and isinstance(manifest.get("session_slices"), list)
            and len(manifest["session_slices"]) >= 1
        ):
            slices = [str(s) for s in manifest["session_slices"]]
            session_fallback_used = bool(manifest.get("session_fallback_used", False))
            saved_slice_status: dict[int, dict[str, Any]] = {
                int(item["index"]): dict(item)
                for item in (manifest.get("slice_status") or [])
                if isinstance(item, dict) and "index" in item
            }
        else:
            slices = plan_session_slices(text)
            session_fallback_used = False
            saved_slice_status = {}

        slice_status_map: dict[int, dict[str, Any]] = dict(saved_slice_status)

        def _rem_session_gpu_s(done_slices: int) -> float:
            rem_sl = slices[max(0, int(done_slices)) :]
            if not rem_sl:
                return 0.0
            raw_sum = sum(_hf_ledger.session_call_gate_s(len(s)) for s in rem_sl)
            return round(raw_sum * _hf_ledger.PRE_JOB_SAFETY_MULTIPLIER, 2)

        def _persist_session_checkpoint(done_slices: int) -> None:
            if checkpoint_dir is None:
                return
            try:
                checkpoint_dir.mkdir(parents=True, exist_ok=True)
                if (
                    active_ref_wav is not None
                    and active_ref_wav.is_file()
                    and ckpt_ref_file is not None
                    and active_ref_wav.resolve() != ckpt_ref_file.resolve()
                ):
                    shutil.copy2(active_ref_wav, ckpt_ref_file)
                ordered_status = [
                    slice_status_map.get(
                        i,
                        {"index": i, "completed": False, "fell_back": False, "sub_chunks": []},
                    )
                    for i in range(1, len(slices) + 1)
                ]
                payload = {
                    "job_id": job_id,
                    "text": text,
                    "session_mode": True,
                    "session_slices": slices,
                    "slice_status": ordered_status,
                    "completed_units": int(done_slices),
                    "total_units": max(1, len(slices)),
                    "session_fallback_used": bool(session_fallback_used),
                    "pinned_space": getattr(engine, "pinned_space", None),
                    "space_family_switched": bool(getattr(engine, "space_family_switched", False)),
                    "reference_transcript": reference_transcript,
                    "active_ref_secs": round(float(active_ref_secs or 0.0), 3),
                }
                (checkpoint_dir / "manifest.json").write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8"
                )
            except Exception as ck_exc:
                logger.debug("Could not persist session clone checkpoint for %s: %s", job_id, ck_exc)

        extra_prompt_kw_sess: dict[str, Any] = {}
        if is_custom_clone and not is_gender_flip:
            extra_prompt_kw_sess["prompt_text"] = reference_transcript
        elif reference_transcript:
            extra_prompt_kw_sess["prompt_text"] = reference_transcript

        if checkpoint_progress is not None:
            checkpoint_progress["completed_units"] = 0
            checkpoint_progress["total_units"] = max(1, len(slices))
            checkpoint_progress["pinned_space"] = getattr(engine, "pinned_space", None)
            checkpoint_progress["session_fallback_used"] = bool(session_fallback_used)
            checkpoint_progress["remaining_gpu_s"] = _rem_session_gpu_s(0)

        logger.info(
            "Voice clone session-slice synthesis: job=%s slices=%d", job_id, len(slices)
        )

        assembled_parts: list[Path] = []
        assembled_texts: list[str] = []
        corr_dir_sess = output_wav.parent / f"{output_wav.stem}_prosody_corr"
        stitched_wav_sess = output_wav.parent / f"{output_wav.stem}.stitched_raw.wav"
        completed_slices = 0

        try:
            for idx, slice_text in enumerate(slices, 1):
                saved_st = saved_slice_status.get(idx)
                if checkpoint_dir is not None:
                    ckpt_slice_file = checkpoint_dir / f"slice{idx:02d}.wav"
                    if (
                        (saved_st is not None and saved_st.get("completed") and not saved_st.get("fell_back"))
                        or (saved_st is None and ckpt_slice_file.is_file() and ckpt_slice_file.stat().st_size > 0)
                    ) and ckpt_slice_file.is_file() and ckpt_slice_file.stat().st_size > 0:
                        part_s = output_wav.parent / f"{output_wav.stem}.slice{idx:02d}.wav"
                        shutil.copy2(ckpt_slice_file, part_s)
                        assembled_parts.append(part_s)
                        assembled_texts.append(slice_text)
                        slice_status_map[idx] = {
                            "index": idx,
                            "completed": True,
                            "fell_back": False,
                            "sub_chunks": [],
                        }
                        completed_slices = idx
                        if checkpoint_progress is not None:
                            checkpoint_progress["completed_units"] = idx
                            checkpoint_progress["pinned_space"] = getattr(engine, "pinned_space", None)
                            checkpoint_progress["session_fallback_used"] = bool(session_fallback_used)
                            checkpoint_progress["remaining_gpu_s"] = _rem_session_gpu_s(idx)
                        logger.info(
                            "Reused checkpointed session slice %d/%d for voice job %s without re-synthesis",
                            idx,
                            len(slices),
                            job_id,
                        )
                        continue

                    if saved_st is not None and saved_st.get("completed") and saved_st.get("fell_back"):
                        sub_list = [str(x) for x in (saved_st.get("sub_chunks") or [])]
                        ckpt_fb_files = [
                            checkpoint_dir / f"slice{idx:02d}_fb{fb_i:02d}.wav"
                            for fb_i in range(1, len(sub_list) + 1)
                        ]
                        if sub_list and all(f.is_file() and f.stat().st_size > 0 for f in ckpt_fb_files):
                            for fb_i, (ckpt_fb, sub_txt) in enumerate(zip(ckpt_fb_files, sub_list), 1):
                                part_fb = output_wav.parent / f"{output_wav.stem}.slice{idx:02d}.fb{fb_i:02d}.wav"
                                shutil.copy2(ckpt_fb, part_fb)
                                assembled_parts.append(part_fb)
                                assembled_texts.append(sub_txt)
                            session_fallback_used = True
                            slice_status_map[idx] = dict(saved_st)
                            completed_slices = idx
                            if checkpoint_progress is not None:
                                checkpoint_progress["completed_units"] = idx
                                checkpoint_progress["pinned_space"] = getattr(engine, "pinned_space", None)
                                checkpoint_progress["session_fallback_used"] = True
                                checkpoint_progress["remaining_gpu_s"] = _rem_session_gpu_s(idx)
                            logger.info(
                                "Reused checkpointed fallback sub-chunks for session slice %d/%d on voice job %s",
                                idx,
                                len(slices),
                                job_id,
                            )
                            continue

                part_s = output_wav.parent / f"{output_wav.stem}.slice{idx:02d}.wav"
                slice_gate_s = _hf_ledger.effective_session_admission_gate_s(
                    len(slice_text), getattr(engine, "pinned_space", None)
                )
                sess_kw: dict[str, Any] = {
                    "text": slice_text,
                    "output_wav": part_s,
                    "reference_wav": effective_ref_arg,
                    "control_instruction": control_instruction,
                    "preset_voice": preset_voice,
                    "speed": speed,
                    "progress_cb": progress_cb,
                    "skip_mastering": True,
                    **extra_prompt_kw_sess,
                    **_dp_kwargs,
                    **({"session_mode": True} if _accepts_session else {}),
                }
                try:
                    _call_unit_with_bounded_retry(sess_kw, req_gate_s=slice_gate_s)
                    if _allow_optional_regen and is_wav_edge_clipped(part_s):
                        logger.warning(
                            "VoxCPM session slice %d/%d clipped at edge for job %s; regenerating once",
                            idx,
                            len(slices),
                            job_id,
                        )
                        try:
                            engine.synthesize(**sess_kw)
                        except Exception as e_regen:
                            logger.warning(
                                "VoxCPM session slice %d retry failed (%s); keeping existing slice",
                                idx,
                                e_regen,
                            )
                    if checkpoint_dir is not None and part_s.is_file():
                        try:
                            checkpoint_dir.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(part_s, checkpoint_dir / f"slice{idx:02d}.wav")
                        except Exception:
                            pass
                    slice_status_map[idx] = {
                        "index": idx,
                        "completed": True,
                        "fell_back": False,
                        "sub_chunks": [],
                    }
                    assembled_parts.append(part_s)
                    assembled_texts.append(slice_text)
                except Exception as exc_sess:
                    part_s.unlink(missing_ok=True)
                    logger.warning(
                        "Session slice %d/%d exhausted retries for job %s (%s); "
                        "re-synthesizing slice via <=385-char legacy /generate chunks",
                        idx,
                        len(slices),
                        job_id,
                        exc_sess,
                    )
                    fb_chunks = plan_text_chunks(
                        slice_text,
                        reference_transcript=reference_transcript,
                        control_instruction=effective_planning_control,
                        reference_seconds=active_ref_secs,
                        max_chunk_chars=385,
                    )
                    fb_parts_for_slice: list[Path] = []
                    try:
                        for fb_i, fb_txt in enumerate(fb_chunks, 1):
                            part_fb = output_wav.parent / f"{output_wav.stem}.slice{idx:02d}.fb{fb_i:02d}.wav"
                            fb_kw: dict[str, Any] = {
                                "text": fb_txt,
                                "output_wav": part_fb,
                                "reference_wav": effective_ref_arg,
                                "control_instruction": control_instruction,
                                "preset_voice": preset_voice,
                                "speed": speed,
                                "progress_cb": progress_cb,
                                "skip_mastering": True,
                                **extra_prompt_kw_sess,
                                **_dp_kwargs,
                                **({"session_mode": False} if _accepts_session else {}),
                                **({"pin_space_override": False} if _accepts_pin_override else {}),
                            }
                            _call_unit_with_bounded_retry(fb_kw)
                            if _allow_optional_regen and is_wav_edge_clipped(part_fb):
                                try:
                                    engine.synthesize(**fb_kw)
                                except Exception:
                                    pass
                            fb_parts_for_slice.append(part_fb)
                    except Exception:
                        for p_tmp in fb_parts_for_slice:
                            p_tmp.unlink(missing_ok=True)
                        _persist_session_checkpoint(completed_slices)
                        if checkpoint_progress is not None:
                            checkpoint_progress["completed_units"] = completed_slices
                            checkpoint_progress["pinned_space"] = getattr(engine, "pinned_space", None)
                            checkpoint_progress["session_fallback_used"] = bool(session_fallback_used)
                            checkpoint_progress["remaining_gpu_s"] = _rem_session_gpu_s(completed_slices)
                        raise

                    session_fallback_used = True

                    if checkpoint_dir is not None:
                        try:
                            checkpoint_dir.mkdir(parents=True, exist_ok=True)
                            for fb_i, p_fb in enumerate(fb_parts_for_slice, 1):
                                if p_fb.is_file():
                                    shutil.copy2(p_fb, checkpoint_dir / f"slice{idx:02d}_fb{fb_i:02d}.wav")
                        except Exception:
                            pass
                    slice_status_map[idx] = {
                        "index": idx,
                        "completed": True,
                        "fell_back": True,
                        "sub_chunks": list(fb_chunks),
                    }
                    assembled_parts.extend(fb_parts_for_slice)
                    assembled_texts.extend(fb_chunks)

                completed_slices = idx
                _persist_session_checkpoint(completed_slices)
                if checkpoint_progress is not None:
                    checkpoint_progress["completed_units"] = idx
                    checkpoint_progress["pinned_space"] = getattr(engine, "pinned_space", None)
                    checkpoint_progress["session_fallback_used"] = bool(session_fallback_used)
                    checkpoint_progress["remaining_gpu_s"] = _rem_session_gpu_s(idx)

            pinned_sp_str = str(getattr(engine, "pinned_space", None) or "none")
            space_fam_switched = bool(getattr(engine, "space_family_switched", False))
            speed_tempo = resolve_speed_tempo(speed)

            if not session_fallback_used:
                # Part 5: NO slice fell back -> SKIP Phase 2 prosody pass entirely (mastering only).
                if len(assembled_parts) == 1:
                    master_voxcpm_audio(
                        assembled_parts[0],
                        output_wav,
                        speed=speed,
                        preset_voice=preset_voice,
                    )
                else:
                    b_specs = _cp.classify_chunk_boundaries(
                        text, assembled_texts, speed_tempo=speed_tempo
                    )
                    slice_pauses = [b.pause_sec for b in b_specs]
                    try:
                        concat_wav_files(
                            assembled_parts,
                            stitched_wav_sess,
                            boundary_pause_sec=_cp.CLONE_BOUNDARY_PAUSE_SEC,
                            boundary_pauses_sec=slice_pauses,
                        )
                    except TypeError as te:
                        if (
                            "boundary_pause" not in str(te)
                            and "unexpected keyword" not in str(te)
                            and "positional" not in str(te)
                        ):
                            raise
                        concat_wav_files(assembled_parts, stitched_wav_sess)
                    master_voxcpm_audio(
                        stitched_wav_sess,
                        output_wav,
                        speed=speed,
                        preset_voice=preset_voice,
                    )

                logger.info(
                    "clone_prosody_summary job=%s chunks=%d retries_used=%d corrections=%d "
                    "desag_corrections=%d tilt_eq_corrections=%d "
                    "envelope_corrections=%d edge_fallbacks=%d anchor_source=%s f0_anchor_hz=%s "
                    "f0_hz=%s syl_rate=%s boundary_steps=%s space_family_switched=%s "
                    "session_mode=%s session_slices=%d session_fallback_used=%s pinned_space=%s "
                    "space_timeouts=%s quota_refusals=%d refusal_wall_s=%.2f ceiling_skips=%d max_fit_jumps=%d "
                    "sub_gate_skips=%d adm_gates_s=%s observed_doors_s=%s refused_tokens=%d refusals_by_space=%s",
                    job_id,
                    len(assembled_parts),
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    "skipped",
                    None,
                    [],
                    [],
                    [],
                    space_fam_switched,
                    True,
                    len(slices),
                    False,
                    pinned_sp_str,
                    dict(getattr(engine, "space_timeouts", None) or {}),
                    int(getattr(engine, "quota_refusals", 0) or 0),
                    float(getattr(engine, "refusal_wall_s", 0.0) or 0.0),
                    int(getattr(engine, "ceiling_skips", 0) or 0),
                    int(getattr(engine, "max_fit_jumps", 0) or 0),
                    int(getattr(engine, "sub_gate_skips", 0) or 0),
                    list(getattr(engine, "adm_gates_s", None) or []),
                    list(getattr(engine, "observed_doors_s", None) or []),
                    int(getattr(engine, "refused_tokens", 0) or 0),
                    dict(getattr(engine, "refusals_by_space", None) or {}),
                )
            else:
                # Part 5: ANY slice fell back -> run prosody pass over assembled audio as legacy path does today.
                if ref_prosody is None and active_ref_wav is not None and active_ref_wav.is_file():
                    ref_prosody = _cp.measure_prosody(active_ref_wav, text=reference_transcript)
                sess_analyses = [
                    _cp.analyze_chunk_prosody(p_item, text=t_item)
                    for p_item, t_item in zip(assembled_parts, assembled_texts)
                ]
                prosody_res = _cp.run_post_loop_prosody_pass(
                    assembled_parts,
                    assembled_texts,
                    full_text=text,
                    out_dir=corr_dir_sess,
                    ref_prosody=ref_prosody,
                    is_custom_clone=is_custom_clone,
                    is_gender_flip=is_gender_flip,
                    preset_voice=preset_voice,
                    speed_tempo=speed_tempo,
                    cached_analyses=sess_analyses,
                    space_family_switched=space_fam_switched,
                )
                try:
                    concat_wav_files(
                        prosody_res.corrected_parts,
                        stitched_wav_sess,
                        boundary_pause_sec=_cp.CLONE_BOUNDARY_PAUSE_SEC,
                        boundary_pauses_sec=prosody_res.pauses_sec,
                    )
                except TypeError as te:
                    if (
                        "boundary_pause" not in str(te)
                        and "unexpected keyword" not in str(te)
                        and "positional" not in str(te)
                    ):
                        raise
                    concat_wav_files(prosody_res.corrected_parts, stitched_wav_sess)

                master_voxcpm_audio(
                    stitched_wav_sess,
                    output_wav,
                    speed=speed,
                    preset_voice=preset_voice,
                )
                post_f0_log = [st.f0_median_hz for st in prosody_res.post_stats]
                post_rate_log = [st.syllable_rate for st in prosody_res.post_stats]
                logger.info(
                    "clone_prosody_summary job=%s chunks=%d retries_used=%d corrections=%d "
                    "desag_corrections=%d tilt_eq_corrections=%d "
                    "envelope_corrections=%d edge_fallbacks=%d anchor_source=%s f0_anchor_hz=%s "
                    "f0_hz=%s syl_rate=%s boundary_steps=%s space_family_switched=%s "
                    "session_mode=%s session_slices=%d session_fallback_used=%s pinned_space=%s "
                    "space_timeouts=%s quota_refusals=%d refusal_wall_s=%.2f ceiling_skips=%d max_fit_jumps=%d "
                    "sub_gate_skips=%d adm_gates_s=%s observed_doors_s=%s refused_tokens=%d refusals_by_space=%s",
                    job_id,
                    len(assembled_parts),
                    0,
                    prosody_res.corrections_applied,
                    prosody_res.desag_corrections,
                    prosody_res.tilt_eq_corrections,
                    prosody_res.envelope_corrections,
                    prosody_res.edge_fallbacks,
                    prosody_res.anchor.anchor_source,
                    prosody_res.anchor.f0_anchor_hz,
                    post_f0_log,
                    post_rate_log,
                    prosody_res.boundary_steps,
                    prosody_res.space_family_switched,
                    True,
                    len(slices),
                    True,
                    pinned_sp_str,
                    dict(getattr(engine, "space_timeouts", None) or {}),
                    int(getattr(engine, "quota_refusals", 0) or 0),
                    float(getattr(engine, "refusal_wall_s", 0.0) or 0.0),
                    int(getattr(engine, "ceiling_skips", 0) or 0),
                    int(getattr(engine, "max_fit_jumps", 0) or 0),
                    int(getattr(engine, "sub_gate_skips", 0) or 0),
                    list(getattr(engine, "adm_gates_s", None) or []),
                    list(getattr(engine, "observed_doors_s", None) or []),
                    int(getattr(engine, "refused_tokens", 0) or 0),
                    dict(getattr(engine, "refusals_by_space", None) or {}),
                )
        finally:
            stitched_wav_sess.unlink(missing_ok=True)
            shutil.rmtree(corr_dir_sess, ignore_errors=True)
            for p_clean in assembled_parts:
                try:
                    p_clean.unlink(missing_ok=True)
                except Exception:
                    pass
        return output_wav
    if (
        manifest.get("text") == text
        and isinstance(manifest.get("chunks"), list)
        and len(manifest["chunks"]) >= 1
    ):
        chunks = [str(c) for c in manifest["chunks"]]
    else:
        chunks = plan_text_chunks(
            text,
            reference_transcript=reference_transcript,
            control_instruction=effective_planning_control,
            reference_seconds=active_ref_secs,
        )

    def _persist_checkpoint(done_units: int) -> None:
        if checkpoint_dir is None:
            return
        try:
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            if (
                active_ref_wav is not None
                and active_ref_wav.is_file()
                and ckpt_ref_file is not None
                and active_ref_wav.resolve() != ckpt_ref_file.resolve()
            ):
                shutil.copy2(active_ref_wav, ckpt_ref_file)
            payload = {
                "job_id": job_id,
                "text": text,
                "chunks": chunks,
                "completed_units": int(done_units),
                "total_units": max(1, len(chunks)),
                "pinned_space": getattr(engine, "pinned_space", None),
                "space_family_switched": bool(getattr(engine, "space_family_switched", False)),
                "reference_transcript": reference_transcript,
                "active_ref_secs": round(float(active_ref_secs or 0.0), 3),
            }
            (checkpoint_dir / "manifest.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except Exception as ck_exc:
            logger.debug("Could not persist standalone clone checkpoint for %s: %s", job_id, ck_exc)

    if checkpoint_progress is not None:
        checkpoint_progress["completed_units"] = 0
        checkpoint_progress["total_units"] = max(1, len(chunks))
        checkpoint_progress["pinned_space"] = getattr(engine, "pinned_space", None)

    extra_prompt_kw: dict = {}
    if is_custom_clone and not is_gender_flip:
        extra_prompt_kw["prompt_text"] = reference_transcript
    elif reference_transcript:
        extra_prompt_kw["prompt_text"] = reference_transcript

    if len(chunks) <= 1:
        single_kw = {
            "text": text,
            "output_wav": output_wav,
            "reference_wav": effective_ref_arg,
            "control_instruction": control_instruction,
            "preset_voice": preset_voice,
            "speed": speed,
            "progress_cb": progress_cb,
            **extra_prompt_kw,
            **_dp_kwargs,
        }
        try:
            _call_unit_with_bounded_retry(single_kw)
        except Exception:
            _persist_checkpoint(0)
            raise
        if _allow_optional_regen and is_wav_edge_clipped(output_wav):
            logger.warning(
                "VoxCPM single-call output clipped at edge for job %s; regenerating once",
                job_id,
            )
            try:
                engine.synthesize(**single_kw)
            except Exception as e_regen:
                logger.warning(
                    "VoxCPM single-call retry failed (%s); keeping existing output",
                    e_regen,
                )
        if checkpoint_progress is not None:
            checkpoint_progress["completed_units"] = 1
            checkpoint_progress["pinned_space"] = getattr(engine, "pinned_space", None)
        _single_sp_tos = dict(getattr(engine, "space_timeouts", None) or {})
        if _single_sp_tos:
            logger.info("clone_space_timeouts job=%s space_timeouts=%s", job_id, _single_sp_tos)
        return output_wav

    logger.info(
        "Voice clone chunked synthesis: job=%s chunks=%d", job_id, len(chunks)
    )
    from app.services.speed import resolve_speed_tempo

    parts: list[Path] = []
    corr_dir = output_wav.parent / f"{output_wav.stem}_prosody_corr"
    stitched_wav = output_wav.parent / f"{output_wav.stem}.stitched_raw.wav"
    job_prosody_retries = 0
    analyses: list[_cp.ChunkProsodyAnalysis] = []
    completed_so_far = 0

    try:
        for idx, chunk in enumerate(chunks, 1):
            part = output_wav.parent / f"{output_wav.stem}.chunk{idx:02d}.wav"
            ckpt_chunk_file = (
                checkpoint_dir / f"chunk{idx:02d}.wav" if checkpoint_dir is not None else None
            )
            if (
                ckpt_chunk_file is not None
                and ckpt_chunk_file.is_file()
                and ckpt_chunk_file.stat().st_size > 0
            ):
                # Part 1 (#1 & #3): checkpointed chunks are RAW (pre-rubberband),
                # so resuming re-derives the post-loop contour pass cleanly with zero double-processing.
                shutil.copy2(ckpt_chunk_file, part)
                analyses.append(_cp.analyze_chunk_prosody(part, text=chunk))
                parts.append(part)
                completed_so_far = idx
                if checkpoint_progress is not None:
                    checkpoint_progress["completed_units"] = idx
                    checkpoint_progress["pinned_space"] = getattr(engine, "pinned_space", None)
                logger.info(
                    "Reused checkpointed raw chunk %d/%d for voice job %s without re-synthesis",
                    idx,
                    len(chunks),
                    job_id,
                )
                continue

            chunk_kw = {
                "text": chunk,
                "output_wav": part,
                "reference_wav": effective_ref_arg,
                "control_instruction": control_instruction,
                "preset_voice": preset_voice,
                "speed": speed,
                "progress_cb": progress_cb,
                "allow_fallback": False,
                "skip_mastering": True,
                **extra_prompt_kw,
                **({"tier": tier} if (tier != "free" and _accepts_tier) else {}),
            }
            try:
                _call_unit_with_bounded_retry(chunk_kw)
            except Exception:
                _persist_checkpoint(completed_so_far)
                if checkpoint_progress is not None:
                    checkpoint_progress["completed_units"] = completed_so_far
                    checkpoint_progress["pinned_space"] = getattr(engine, "pinned_space", None)
                raise

            if _allow_optional_regen and is_wav_edge_clipped(part):
                logger.warning(
                    "VoxCPM segment %d/%d output clipped at edge for job %s; regenerating once",
                    idx,
                    len(chunks),
                    job_id,
                )
                try:
                    engine.synthesize(**chunk_kw)
                except Exception as e_regen:
                    logger.warning(
                        "VoxCPM segment %d retry failed (%s); keeping existing part",
                        idx,
                        e_regen,
                    )

            # Part 1 (#1) + Part 4 (#3): analyze raw chunk ONCE (caching YIN contour) and
            # persist the RAW chunk in dp_refs before any rubberband pass runs.
            analyses.append(_cp.analyze_chunk_prosody(part, text=chunk))
            if ckpt_chunk_file is not None and part.is_file():
                try:
                    checkpoint_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(part, ckpt_chunk_file)
                except Exception:
                    pass
            completed_so_far = idx
            _persist_checkpoint(completed_so_far)
            if checkpoint_progress is not None:
                checkpoint_progress["completed_units"] = idx
                checkpoint_progress["pinned_space"] = getattr(engine, "pinned_space", None)
            parts.append(part)

        # Part 6: Re-scored retry pass across raw chunks (<=2/job, <=1/chunk, Normal band >40% only),
        # ranked worst-scoring first against the global job/reference anchor.
        if _max_prosody_retries > 0 and parts:
            init_anchor = _cp.compute_global_prosody_anchor(
                analyses,
                ref_prosody=ref_prosody,
                is_custom_clone=is_custom_clone,
                is_gender_flip=is_gender_flip,
                preset_voice=preset_voice,
            )
            outlier_ranked: list[tuple[float, int]] = []
            for c_idx, ana in enumerate(analyses):
                tilt_delta = (
                    (ana.voiced_tilt_db - init_anchor.voiced_tilt_anchor_db)
                    if (init_anchor.voiced_tilt_anchor_db is not None and ana.stats.voiced_sec >= 0.15)
                    else 0.0
                )
                sag_for_score = ana.linear_sag_st if (ana.sentence_count >= 2 and ana.stats.duration_sec >= 8.0) else 0.0
                is_out, _df0, _rr = _cp.is_prosody_outlier(
                    ana.stats,
                    anchor_f0_hz=init_anchor.f0_anchor_hz,
                    anchor_rate=init_anchor.rate_anchor,
                    internal_span_st=ana.internal_span_st,
                    voiced_tilt_delta_db=tilt_delta,
                    linear_sag_st=sag_for_score,
                )
                if is_out:
                    sc = _cp.score_prosody_candidate(
                        ana.stats,
                        anchor_f0_hz=init_anchor.f0_anchor_hz,
                        anchor_rate=init_anchor.rate_anchor,
                        internal_span_st=ana.internal_span_st,
                        voiced_tilt_delta_db=tilt_delta,
                        linear_sag_st=sag_for_score,
                    )
                    outlier_ranked.append((round(sc, 6), c_idx))

            outlier_ranked.sort(key=lambda item: (-item[0], item[1]))
            for _orig_score, c_idx in outlier_ranked:
                if job_prosody_retries >= _max_prosody_retries:
                    break
                part = parts[c_idx]
                chunk_text = chunks[c_idx]
                retry_part = output_wav.parent / f"{output_wav.stem}.chunk{c_idx + 1:02d}.retry.wav"
                job_prosody_retries += 1
                try:
                    retry_kw = {
                        "text": chunk_text,
                        "output_wav": retry_part,
                        "reference_wav": effective_ref_arg,
                        "control_instruction": control_instruction,
                        "preset_voice": preset_voice,
                        "speed": speed,
                        "progress_cb": progress_cb,
                        "allow_fallback": False,
                        "skip_mastering": True,
                        **extra_prompt_kw,
                        **({"tier": tier} if (tier != "free" and _accepts_tier) else {}),
                    }
                    engine.synthesize(**retry_kw)
                    if retry_part.is_file():
                        retry_ana = _cp.analyze_chunk_prosody(retry_part, text=chunk_text)
                        orig_ana = analyses[c_idx]
                        o_tilt_d = (
                            (orig_ana.voiced_tilt_db - init_anchor.voiced_tilt_anchor_db)
                            if (init_anchor.voiced_tilt_anchor_db is not None and orig_ana.stats.voiced_sec >= 0.15)
                            else 0.0
                        )
                        r_tilt_d = (
                            (retry_ana.voiced_tilt_db - init_anchor.voiced_tilt_anchor_db)
                            if (init_anchor.voiced_tilt_anchor_db is not None and retry_ana.stats.voiced_sec >= 0.15)
                            else 0.0
                        )
                        o_sag = orig_ana.linear_sag_st if (orig_ana.sentence_count >= 2 and orig_ana.stats.duration_sec >= 8.0) else 0.0
                        r_sag = retry_ana.linear_sag_st if (retry_ana.sentence_count >= 2 and retry_ana.stats.duration_sec >= 8.0) else 0.0
                        s_orig = _cp.score_prosody_candidate(
                            orig_ana.stats,
                            anchor_f0_hz=init_anchor.f0_anchor_hz,
                            anchor_rate=init_anchor.rate_anchor,
                            internal_span_st=orig_ana.internal_span_st,
                            voiced_tilt_delta_db=o_tilt_d,
                            linear_sag_st=o_sag,
                        )
                        s_retry = _cp.score_prosody_candidate(
                            retry_ana.stats,
                            anchor_f0_hz=init_anchor.f0_anchor_hz,
                            anchor_rate=init_anchor.rate_anchor,
                            internal_span_st=retry_ana.internal_span_st,
                            voiced_tilt_delta_db=r_tilt_d,
                            linear_sag_st=r_sag,
                        )
                        if s_retry < s_orig:
                            shutil.move(str(retry_part), str(part))
                            retry_ana.wav_path = part
                            analyses[c_idx] = retry_ana
                            if checkpoint_dir is not None:
                                try:
                                    shutil.copy2(part, checkpoint_dir / f"chunk{c_idx + 1:02d}.wav")
                                except Exception:
                                    pass
                except Exception as e_prosody:
                    logger.warning(
                        "VoxCPM prosody outlier retry failed on chunk %d (%s); keeping original",
                        c_idx + 1,
                        e_prosody,
                    )
                finally:
                    retry_part.unlink(missing_ok=True)

        # Parts 1-8: ONE post-loop contour + de-sag + seam + voiced-tilt EQ + tempo pass
        speed_tempo = resolve_speed_tempo(speed)
        space_fam_switched = bool(getattr(engine, "space_family_switched", False))
        prosody_res = _cp.run_post_loop_prosody_pass(
            parts,
            chunks,
            full_text=text,
            out_dir=corr_dir,
            ref_prosody=ref_prosody,
            is_custom_clone=is_custom_clone,
            is_gender_flip=is_gender_flip,
            preset_voice=preset_voice,
            speed_tempo=speed_tempo,
            cached_analyses=analyses,
            space_family_switched=space_fam_switched,
        )

        # Stitch corrected chunks with Part 5 boundary-class adaptive pauses
        # (graceful fallback for legacy 2-arg test stubs)
        try:
            concat_wav_files(
                prosody_res.corrected_parts,
                stitched_wav,
                boundary_pause_sec=_cp.CLONE_BOUNDARY_PAUSE_SEC,
                boundary_pauses_sec=prosody_res.pauses_sec,
            )
        except TypeError as te:
            if (
                "boundary_pause" not in str(te)
                and "unexpected keyword" not in str(te)
                and "positional" not in str(te)
            ):
                raise
            concat_wav_files(prosody_res.corrected_parts, stitched_wav)

        # Apply ONE post-stitch mastering + speed_preset pass
        master_voxcpm_audio(
            stitched_wav,
            output_wav,
            speed=speed,
            preset_voice=preset_voice,
        )

        post_f0_log = [st.f0_median_hz for st in prosody_res.post_stats]
        post_rate_log = [st.syllable_rate for st in prosody_res.post_stats]
        legacy_pinned_sp = str(getattr(engine, "pinned_space", None) or "none")
        logger.info(
            "clone_prosody_summary job=%s chunks=%d retries_used=%d corrections=%d "
            "desag_corrections=%d tilt_eq_corrections=%d "
            "envelope_corrections=%d edge_fallbacks=%d anchor_source=%s f0_anchor_hz=%s "
            "f0_hz=%s syl_rate=%s boundary_steps=%s space_family_switched=%s "
            "session_mode=%s session_slices=%d session_fallback_used=%s pinned_space=%s "
            "space_timeouts=%s quota_refusals=%d refusal_wall_s=%.2f ceiling_skips=%d max_fit_jumps=%d "
            "sub_gate_skips=%d adm_gates_s=%s observed_doors_s=%s refused_tokens=%d refusals_by_space=%s",
            job_id,
            len(chunks),
            job_prosody_retries,
            prosody_res.corrections_applied,
            prosody_res.desag_corrections,
            prosody_res.tilt_eq_corrections,
            prosody_res.envelope_corrections,
            prosody_res.edge_fallbacks,
            prosody_res.anchor.anchor_source,
            prosody_res.anchor.f0_anchor_hz,
            post_f0_log,
            post_rate_log,
            prosody_res.boundary_steps,
            prosody_res.space_family_switched,
            False,
            0,
            False,
            legacy_pinned_sp,
            dict(getattr(engine, "space_timeouts", None) or {}),
            int(getattr(engine, "quota_refusals", 0) or 0),
            float(getattr(engine, "refusal_wall_s", 0.0) or 0.0),
            int(getattr(engine, "ceiling_skips", 0) or 0),
            int(getattr(engine, "max_fit_jumps", 0) or 0),
            int(getattr(engine, "sub_gate_skips", 0) or 0),
            list(getattr(engine, "adm_gates_s", None) or []),
            list(getattr(engine, "observed_doors_s", None) or []),
            int(getattr(engine, "refused_tokens", 0) or 0),
            dict(getattr(engine, "refusals_by_space", None) or {}),
        )
    finally:
        stitched_wav.unlink(missing_ok=True)
        shutil.rmtree(corr_dir, ignore_errors=True)
        for part in parts:
            try:
                part.unlink(missing_ok=True)
            except Exception:
                pass
    return output_wav


async def process_recap_job(
    session: AsyncSession,
    job: Job,
    bot: Bot | None = None,
) -> bool:
    """Execute a claimed recap job on the worker (primary or VPS).

    Invokes the full recap pipeline (source fetch -> planner -> narration -> render -> delivery)
    using the persisted config and credit contract on the Job row.
    """
    from app.models.schema import User
    from app.webapp.api import (
        RECAP_JOBS_REGISTRY,
        _execute_recap_background_job,
        _load_persisted_recap_state,
        _recap_redispatch_plan,
        _reconcile_recap_registry_with_db_job,
        _restore_script_ready_from_db,
    )

    jid = str(job.id)
    logger.info("Starting worker recap pipeline for job %s...", jid)

    user = await session.get(User, job.user_id)
    persisted_state = _load_persisted_recap_state(job)
    if persisted_state is not None and persisted_state.get("script_ready"):
        restored = _restore_script_ready_from_db(job)
        if restored:
            RECAP_JOBS_REGISTRY[jid] = restored
            logger.info("Restored parked SCRIPT_READY recap job %s on worker (no re-run)", jid)
            return True

    try:
        plan = _recap_redispatch_plan(job, user)
    except Exception as plan_err:
        logger.error("Could not build recap execution plan for job %s: %s", jid, plan_err, exc_info=True)
        from app.webapp.api import _fail_stranded_recap_job
        await _fail_stranded_recap_job(jid, job.user_id, persisted_state or {})
        await session.refresh(job)
        if job.status != JobStatus.FAILED.value:
            job.status = JobStatus.FAILED.value
            job.error_message = f"Failed to build recap execution plan: {plan_err}"
            job.finished_at = utc_now()
            await session.commit()
        _reconcile_recap_registry_with_db_job(jid, job)
        return False

    RECAP_JOBS_REGISTRY[jid] = plan["entry"]

    try:
        await _execute_recap_background_job(
            jid,
            plan["cfg"],
            job.user_id,
            job.chat_id or job.user_id,
            is_vip=plan["is_vip"],
            credit_deducted=plan["credit_deducted"],
            phase=plan["phase"],
            voice_choice_mode=plan.get("voice_choice_mode"),
            dp_resume=plan.get("dp_resume", False),
        )
    except Exception as exec_err:
        logger.error("Recap execution error for job %s: %s", jid, exec_err, exc_info=True)
        await session.refresh(job)
        if job.status not in (
            JobStatus.SUCCESS.value,
            JobStatus.FAILED.value,
            JobStatus.CANCELLED.value,
        ):
            job.status = JobStatus.FAILED.value
            job.error_message = job.error_message or f"Recap execution failed: {exec_err}"[:500]
            job.finished_at = utc_now()
            await session.commit()

    await session.refresh(job)
    _reconcile_recap_registry_with_db_job(jid, job)
    return job.status == JobStatus.SUCCESS.value


async def process_single_job(
    session: AsyncSession,
    job: Job,
    bot: Bot | None = None,
    chat_id: int | None = None,
    status_msg_id: int | None = None,
) -> bool:
    """Execute complete voice clone or recap job lifecycle with safe temp cleanup and idempotency."""
    # Terminal state check: never re-run completed or failed jobs
    if job.status in (JobStatus.SUCCESS.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value):
        logger.warning("Job %s is already in terminal state %s; skipping.", job.id, job.status)
        return False

    is_recap = (
        job.job_type == JobType.RECAP.value
        or job.engine == "recap"
        or str(job.id).startswith("rc_")
    )
    if is_recap:
        return await process_recap_job(session, job, bot=bot)

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
        _vox_runtime_seconds = 0.0
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
                elif (
                    getattr(job, "source_audio_path", None)
                    and not getattr(job, "preset_voice", None)
                ):
                    raise FileNotFoundError(
                        f"Reference audio file not found on worker disk: {job.source_audio_path}"
                    )

                control_instruction = None
                if getattr(job, "preset_voice", None):
                    from app.services.curated_voices import get_curated_voice
                    p_info = get_curated_voice(job.preset_voice)
                    if p_info:
                        control_instruction = p_info.get("control_instruction")
                        logger.info("Using curated persona '%s' for VoxCPM 2 job %s", job.preset_voice, job.id)

                _job_gender = str(getattr(job, "gender_preset", "") or "").strip().lower()
                _is_custom_same_gender_clone = (
                    ref_wav is not None
                    and not getattr(job, "preset_voice", None)
                    and _job_gender not in ("m2f", "f2m")
                )
                if not control_instruction:
                    if _is_custom_same_gender_clone:
                        # Prosody Phase 1A-1 #4: preserve fidelity control on custom same-gender
                        # clones instead of overwriting with generic Voice-Design prose.
                        from app.services.voxcpm_engine import DEFAULT_CLONE_FIDELITY_CONTROL

                        control_instruction = DEFAULT_CLONE_FIDELITY_CONTROL
                    else:
                        is_female = (
                            getattr(job, "edge_voice", "") == "my-MM-NilarNeural"
                            or _job_gender in ("m2f", "f2f")
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

                from app.services.voxcpm_engine import is_voxcpm_capacity_or_congestion_error

                if _dp_choice_state and _dp_choice_state.get("pinned_space"):
                    try:
                        engine_inst.pinned_space = str(_dp_choice_state["pinned_space"])
                    except Exception:
                        pass

                _est_units, _est_gate_s, _est_needed_gpu_s = _hf_ledger.estimate_voxcpm_units_and_budget(
                    kind="clone",
                    text=clean_text,
                    reference_transcript=reference_transcript,
                    control_instruction=control_instruction or "",
                    reference_seconds=reference_seconds,
                )
                _prev_done = int((_dp_choice_state or {}).get("completed_units") or 0)
                _rem_units = max(1, _est_units - _prev_done)
                if is_clone_session_slices_enabled():
                    _sess_slices_est = plan_session_slices(clean_text)
                    _rem_slices_est = _sess_slices_est[_prev_done:] or _sess_slices_est[-1:]
                    _rem_needed_gpu_s = round(
                        sum(_hf_ledger.session_call_gate_s(len(s)) for s in _rem_slices_est)
                        * _hf_ledger.PRE_JOB_SAFETY_MULTIPLIER,
                        2,
                    )
                else:
                    _rem_needed_gpu_s = round(
                        _rem_units * _est_gate_s * _hf_ledger.PRE_JOB_SAFETY_MULTIPLIER, 2
                    )
                _ckpt_dir = (
                    (settings.storage_root / "dp_refs" / str(job.id)) if _dp_paid else None
                )
                _ckpt_progress: dict[str, Any] = {
                    "completed_units": _prev_done,
                    "total_units": _est_units,
                    "pinned_space": getattr(engine_inst, "pinned_space", None),
                }

                _hf_ledger.acquire_in_flight_gpu(str(job.id), _rem_needed_gpu_s)
                _vox_t0 = time.monotonic()
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
                            allow_fallback=False,
                            gender_preset=_job_gender,
                            checkpoint_dir=_ckpt_dir,
                            checkpoint_progress=_ckpt_progress,
                        ),
                    )
                except Exception as e_vox:
                    _hf_ledger.release_in_flight_gpu(str(job.id))
                    _is_cap_err = (
                        isinstance(e_vox, VoxCPMQuotaExhaustedError)
                        or is_voxcpm_capacity_or_congestion_error(e_vox)
                    )
                    if _dp_paid and _is_cap_err:
                        _done_u = int(_ckpt_progress.get("completed_units") or _prev_done or 0)
                        _tot_u = max(1, int(_ckpt_progress.get("total_units") or _est_units or 1))
                        if is_clone_session_slices_enabled() and float(_ckpt_progress.get("remaining_gpu_s") or 0.0) > 0:
                            _rem_gpu_s = round(float(_ckpt_progress["remaining_gpu_s"]), 2)
                        else:
                            _rem_gpu_s = round(
                                max(1, _tot_u - _done_u) * _est_gate_s * _hf_ledger.PRE_JOB_SAFETY_MULTIPLIER,
                                2,
                            )
                        _pinned_sp = _ckpt_progress.get("pinned_space") or getattr(
                            engine_inst, "pinned_space", None
                        )
                        _dp_repark = bool(
                            _dp_choice_state
                            and _dp_choice_state.get("prompt_message_id")
                        )
                        _src_path = getattr(job, "source_audio_path", None)
                        if _src_path and Path(_src_path).is_file() and "temp" in str(_src_path):
                            _dp_ref_dir = settings.storage_root / "dp_refs"
                            _dp_ref_dir.mkdir(parents=True, exist_ok=True)
                            _dp_ref_copy = _dp_ref_dir / f"{job.id}{Path(_src_path).suffix or '.wav'}"
                            shutil.copy2(_src_path, _dp_ref_copy)
                            job.source_audio_path = str(_dp_ref_copy)

                        if _dp_repark and _dp_choice_state is not None:
                            _dp_state = dict(_dp_choice_state)
                            _dp_state["parked_at"] = time.time()
                            _dp_state.pop("resumed_at", None)
                            _dp_state["completed_units"] = _done_u
                            _dp_state["total_units"] = _tot_u
                            _dp_state["needed_gpu_s"] = _rem_gpu_s
                            _dp_state["pinned_space"] = str(_pinned_sp) if _pinned_sp else None
                            _dp_state["worker_host"] = _vc.current_worker_host()
                        else:
                            _dp_state = _vc.new_choice_state(
                                _vc.KIND_CLONE,
                                completed_units=_done_u,
                                total_units=_tot_u,
                                needed_gpu_s=_rem_gpu_s,
                                pinned_space=_pinned_sp,
                                worker_host=_vc.current_worker_host(),
                            )
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
                                    state=_dp_state,
                                )
                            else:
                                _dp_mid = await _vc.deliver_choice_prompt(
                                    bot,
                                    chat_id=target_chat_id,
                                    message_id=target_msg_id,
                                    job_id=str(job.id),
                                    kind=_vc.KIND_CLONE,
                                    lang=lang,
                                    state=_dp_state,
                                )
                                if _dp_mid:
                                    _dp_state["prompt_chat_id"] = target_chat_id
                                    _dp_state["prompt_message_id"] = _dp_mid
                                    job.voice_choice_state = _vc.dump_choice_state(_dp_state)
                                    await session.commit()
                        logger.info(
                            "Voice job %s parked (WAITING_CHOICE) at chunk %d/%d: %s",
                            job.id,
                            _done_u,
                            _tot_u,
                            e_vox,
                        )
                        return False

                    if (not _dp_paid) and _is_cap_err:
                        # Part 2 (#4): Free + quota/congestion exhaustion -> fail-fast + instant restore
                        # (0 s deducted, never park Free jobs, never fall back to Edge/persona).
                        if await _voice_job_cancelled(session, job):
                            if not settings.is_admin(job.user_id):
                                res_ref = await refund_or_release_job(
                                    session, job.user_id, str(job.id), reason="USER_CANCELLED"
                                )
                                if not res_ref.get("found"):
                                    from app.services.ledger import refund_job_seconds
                                    await refund_job_seconds(session, job.user_id, str(job.id))
                                await session.commit()
                            return False
                        if not settings.is_admin(job.user_id):
                            res_ref = await refund_or_release_job(
                                session, job.user_id, str(job.id), reason="JOB_FAILED"
                            )
                            if not res_ref.get("found"):
                                from app.services.ledger import refund_job_seconds
                                await refund_job_seconds(session, job.user_id, str(job.id))
                        mb = await get_minute_balance(session, job.user_id)
                        bal_label = _format_free_balance_label(mb.clone_spendable_seconds)
                        eta_label = _vc.quota_reset_estimate_label(needed_gpu_s=_rem_needed_gpu_s)
                        quota_msg = t(
                            "worker_error_quota_exhausted",
                            lang,
                            eta=eta_label,
                            bal=bal_label,
                        )
                        job.status = JobStatus.FAILED.value
                        job.error_message = quota_msg
                        job.finished_at = utc_now()
                        await session.commit()
                        await safe_edit_status(bot, target_chat_id, target_msg_id, quota_msg)
                        logger.info(
                            "Free voice job %s failed-fast on quota/congestion (0s deducted): %s",
                            job.id,
                            e_vox,
                        )
                        return False

                    raise
                finally:
                    _vox_runtime_seconds = round(max(0.0, time.monotonic() - _vox_t0), 3)
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
            if not settings.is_admin(job.user_id):
                await release_job_reservation(
                    session,
                    job.user_id,
                    str(job.id),
                    reason="USER_CANCELLED",
                    note="Voice job cancelled before billing",
                )
            await session.commit()
            return False

        # Batch AJ (H7): billing comes FIRST now. Charge -> persist -> deliver.
        settled_voice_sec = 0 if used_fallback else duration_seconds
        if not settings.is_admin(job.user_id):
            if used_fallback:
                logger.info(
                    "Voice job %s delivered via Edge fallback; "
                    "releasing reserve with zero voice-balance deduction (Batch CH).",
                    job.id,
                )
                await release_job_reservation(
                    session,
                    job.user_id,
                    str(job.id),
                    reason="EDGE_FALLBACK",
                    note="Delivered via Edge fallback (no voice deduction)",
                )
            else:
                settle_res = await settle_job_reservation(
                    session,
                    user_id=job.user_id,
                    job_id=str(job.id),
                    actual_recap_seconds=0,
                    actual_voice_seconds=duration_seconds,
                    raise_on_voice_shortfall=True,
                    note=f"Voice clone generation ({model.name}, {duration_seconds}s)",
                )
                if settle_res.get("found"):
                    settled_voice_sec = int(settle_res.get("settled_voice_seconds") or 0)
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
                    details_json=json.dumps({
                        "model": getattr(model, "name", "Standard"),
                        "duration_seconds": duration_seconds,
                    }, ensure_ascii=False),
                )
            )

        await record_cost_telemetry(
            session,
            job_id=str(job.id),
            user_id=job.user_id,
            provider="edge_tts" if used_fallback else "voxcpm",
            model_name=str(getattr(model, "name", "VoxCPM") or "VoxCPM"),
            requests_count=1,
            audio_seconds=float(duration_seconds or 0.0),
            runtime_seconds=0.0 if used_fallback else float(_vox_runtime_seconds),
        )

        # Batch CH (Item 2): fallback-delivered jobs consumed no voice minutes.
        job.seconds_consumed = 0 if used_fallback else settled_voice_sec
        job.voice_audio_seconds = 0 if used_fallback else int(duration_seconds or 0)
        job.output_duration_seconds = int(duration_seconds or 0)
        if used_fallback:
            job.engine = "edge_fallback"
        if reference_trimmed:
            job.reference_trimmed = True

        outputs_dir = settings.storage_root / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        permanent_wav_path = outputs_dir / f"{job.id}_{out_filename}"
        if converted_wav_path.resolve() != permanent_wav_path.resolve():
            shutil.copy2(converted_wav_path, permanent_wav_path)
        job.result_path = str(permanent_wav_path)
        out_bytes = int(permanent_wav_path.stat().st_size) if permanent_wav_path.is_file() else 0
        job.output_size_bytes = out_bytes

        # Upload audio result to Render Cloud server so Mini App & Bot can stream / download it
        final_audio_path = permanent_wav_path if permanent_wav_path.is_file() else converted_wav_path
        synced = await sync_output_to_cloud(job.id, final_audio_path)
        if not synced and not getattr(settings, "instance_primary", True) and (getattr(settings, "worker_token", "") or "").strip():
            raise RuntimeError("Failed to upload rendered output to cloud server")
        if synced and out_bytes > 0:
            await record_cost_telemetry(
                session,
                job_id=str(job.id),
                user_id=job.user_id,
                provider="bandwidth",
                bytes_transferred=out_bytes,
            )

        await session.commit()

        # Batch AJ (H5): cancel re-check AFTER the billing commit,
        # before delivery. A cancel in this window was told the job
        # was cancelled; the charge above is refunded in full and the
        # row stays CANCELLED — never delivered, never SUCCESS.
        if await _voice_job_cancelled(session, job):
            logger.info("Voice job %s was cancelled after billing; refunding and not delivering.", job.id)
            res_ref = await refund_or_release_job(
                session, job.user_id, str(job.id), reason="USER_CANCELLED"
            )
            if not res_ref.get("found"):
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
                    if audio_source_path.stat().st_size > _effective_telegram_send_limit_bytes():
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
                        caption = build_voice_direct_caption(
                            lang,
                            model_name=str(caption_model_name),
                            duration_seconds=duration_seconds,
                            job_id=job.id,
                            retention_hours=_voice_retention_hours(),
                            used_fallback=bool(used_fallback),
                            reference_trimmed=bool(reference_trimmed),
                        )
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
                # Mini App Job: Send push notification alert with 1-button-per-row keyboard
                try:
                    app_url = settings.mini_app_url
                    kb = build_voice_completion_keyboard(lang, job_id=job.id, app_url=app_url)
                    notify_text = build_voice_completion_message(
                        lang,
                        model_name=str(caption_model_name),
                        duration_seconds=duration_seconds,
                        job_id=job.id,
                        retention_hours=_voice_retention_hours(),
                        used_fallback=bool(used_fallback),
                        reference_trimmed=bool(reference_trimmed),
                    )
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
                    res_ref = await refund_or_release_job(
                        session, job.user_id, str(job.id), reason="USER_CANCELLED"
                    )
                    if not res_ref.get("found"):
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
        # Clear any failed/PendingRollback transaction state before refreshing or refunding.
        try:
            await session.rollback()
        except Exception:
            pass
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
                try:
                    res_ref = await refund_or_release_job(
                        session, job.user_id, str(job.id), reason="USER_CANCELLED"
                    )
                    if not res_ref.get("found"):
                        from app.services.ledger import refund_job_seconds
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

        # Refund or release reservation if any hold/debit occurred for this job
        if not settings.is_admin(job.user_id):
            res_ref = await refund_or_release_job(
                session, job.user_id, str(job.id), reason="JOB_FAILED"
            )
            if not res_ref.get("found"):
                from app.services.ledger import refund_job_seconds
                await refund_job_seconds(session, job.user_id, str(job.id))

        await session.commit()

        fail_msg = build_voice_failure_message(
            lang,
            job_id=str(job.id),
            reason=user_error,
            refunded=not settings.is_admin(job.user_id),
        )
        await safe_edit_status(bot, target_chat_id, target_msg_id, fail_msg)
        return False

    finally:
        _hf_ledger.release_in_flight_gpu(str(job.id))
        if tracker:
            await tracker.stop_ticker()
        # Safe cleanup of intermediate files
        shutil.rmtree(temp_dir, ignore_errors=True)
        if not parked:
            _vc.cleanup_clone_park_artifacts(str(job.id), getattr(job, "source_audio_path", None))
            if getattr(job, "source_audio_path", None):
                try:
                    p = Path(job.source_audio_path)
                    if p.is_file() and ("temp" in str(p) or "dp_refs" in str(p)):
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
                res_ref = await refund_or_release_job(
                    session, job.user_id, str(job.id), reason="WORKER_TIMEOUT"
                )
                if not res_ref.get("found"):
                    from app.services.ledger import refund_job_seconds
                    await refund_job_seconds(session, job.user_id, str(job.id))
            await session.commit()
    except Exception as exc:
        logger.warning("Could not fail timed-out job %s: %s", job_id, exc)


async def _fail_timed_out_recap_job(job_id: str) -> None:
    """Fail a recap job that exceeded its worker deadline and issue an auto-refund."""
    try:
        from app.webapp.api import _fail_stranded_recap_job, _load_persisted_recap_state
        async with AsyncSessionLocal() as session:
            job = await session.get(Job, job_id)
            if job and job.status in (JobStatus.QUEUED.value, JobStatus.RUNNING.value):
                state = _load_persisted_recap_state(job) or {}
                await _fail_stranded_recap_job(job_id, job.user_id, state)
    except Exception as exc:
        logger.warning("Could not fail timed-out recap job %s: %s", job_id, exc)


async def _run_recap_worker_task(job_id: str, bot: Bot | None = None) -> bool:
    """Execute a claimed recap job in a background worker task with deadline protection."""
    timeout_seconds = getattr(settings, "recap_stale_job_timeout_seconds", 2700)
    try:
        async with AsyncSessionLocal() as session:
            job = await session.get(Job, job_id)
            if not job:
                return False
            return await asyncio.wait_for(
                process_single_job(session=session, job=job, bot=bot),
                timeout=timeout_seconds,
            )
    except asyncio.TimeoutError:
        logger.error("Background recap job %s exceeded %ss deadline; failing it.", job_id, timeout_seconds)
        await _fail_timed_out_recap_job(job_id)
        return False
    except Exception as exc:
        logger.error("Background recap task error for job %s: %s", job_id, exc, exc_info=True)
        return False


async def claim_and_process_next_job(
    session: AsyncSession | None = None,
    bot: Bot | None = None,
    engine_filter: str | None = "voxcpm",
    skip_local_source: bool | None = None,
    allow_recap: bool | None = None,
    background_recap: bool = False,
    active_recap_tasks: set[asyncio.Task] | None = None,
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
    from app.services.dynamic_settings import refresh_dynamic_settings_if_changed
    from app.services.queue import claim_next_queued_job, has_queued_job_probe

    if session is not None:
        if allow_recap is None:
            allow_recap = getattr(settings, "recap_on_queue", False)
        job = await claim_next_queued_job(
            session,
            engine_filter=engine_filter,
            skip_local_source=skip_local_source,
            allow_recap=allow_recap,
        )
        if not job:
            return False
        is_recap = (
            job.job_type == JobType.RECAP.value
            or job.engine == "recap"
            or str(job.id).startswith("rc_")
        )
        timeout_seconds = (
            getattr(settings, "recap_stale_job_timeout_seconds", 2700)
            if is_recap
            else VOICE_JOB_DEADLINE_SECONDS
        )
        try:
            return await asyncio.wait_for(
                process_single_job(session=session, job=job, bot=bot),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            if is_recap:
                logger.error("Recap job %s exceeded the %ss worker deadline; failing it.", job.id, timeout_seconds)
                await _fail_timed_out_recap_job(str(job.id))
            else:
                logger.error("Voice job %s exceeded the %ss worker deadline; failing it.", job.id, timeout_seconds)
                await _fail_timed_out_job(str(job.id))
            return False

    async with AsyncSessionLocal() as claim_session:
        await refresh_dynamic_settings_if_changed(claim_session)
        eff_allow_recap = (
            allow_recap
            if allow_recap is not None
            else (
                bool(getattr(settings, "recap_on_queue", False))
                and (
                    active_recap_tasks is None
                    or len(active_recap_tasks) < getattr(settings, "recap_worker_concurrency", 1)
                )
            )
        )
        if not await has_queued_job_probe(
            claim_session,
            engine_filter=engine_filter,
            skip_local_source=skip_local_source,
            allow_recap=eff_allow_recap,
        ):
            return False
        job = await claim_next_queued_job(
            claim_session,
            engine_filter=engine_filter,
            skip_local_source=skip_local_source,
            allow_recap=eff_allow_recap,
        )
        if not job:
            return False
        await refresh_dynamic_settings_if_changed(claim_session, min_interval_s=0.0)
        _note_worker_job_claimed()
        job_id = str(job.id)
        is_recap = (
            job.job_type == JobType.RECAP.value
            or job.engine == "recap"
            or job_id.startswith("rc_")
        )

    if is_recap and background_recap:
        task = asyncio.create_task(_run_recap_worker_task(job_id, bot=bot))
        if active_recap_tasks is not None:
            active_recap_tasks.add(task)
        return True

    timeout_seconds = (
        getattr(settings, "recap_stale_job_timeout_seconds", 2700)
        if is_recap
        else VOICE_JOB_DEADLINE_SECONDS
    )
    async with AsyncSessionLocal() as process_session:
        job = await process_session.get(Job, job_id)
        if job is None:
            return False
        try:
            return await asyncio.wait_for(
                process_single_job(session=process_session, job=job, bot=bot),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            if is_recap:
                logger.error("Recap job %s exceeded the %ss worker deadline; failing it.", job_id, timeout_seconds)
                await _fail_timed_out_recap_job(job_id)
            else:
                logger.error("Voice job %s exceeded the %ss worker deadline; failing it.", job_id, timeout_seconds)
                await _fail_timed_out_job(job_id)
            return False


WORKER_IDLE_BACKOFF_STEPS: tuple[float, ...] = (2.0, 5.0, 10.0)
WORKER_WARM_WINDOW_SECONDS: float = 20.0
WORKER_DB_ERROR_BACKOFF_STEPS: tuple[float, ...] = (5.0, 15.0, 30.0)
WORKER_HEARTBEAT_INTERVAL_SECONDS: float = 60.0

_LAST_JOB_CLAIMED_FLAG: bool = False
_LAST_WRITTEN_HEARTBEAT_META: dict[str, str] = {}


def _note_worker_job_claimed() -> None:
    global _LAST_JOB_CLAIMED_FLAG
    _LAST_JOB_CLAIMED_FLAG = True


def _consume_worker_job_claimed() -> bool:
    global _LAST_JOB_CLAIMED_FLAG
    claimed = _LAST_JOB_CLAIMED_FLAG
    _LAST_JOB_CLAIMED_FLAG = False
    return claimed


def reset_worker_heartbeat_cache() -> None:
    """Clear cached secondary-worker engine/host values (used by unit tests)."""
    _LAST_WRITTEN_HEARTBEAT_META.clear()


async def _upsert_bot_setting_single_roundtrip(
    session: AsyncSession,
    key: str,
    value: str,
    *,
    now_dt=None,
) -> None:
    """Upsert one ``bot_settings`` row in a single ``INSERT ... ON CONFLICT DO UPDATE``."""
    from app.models.schema import BotSetting
    from app.services.dynamic_settings import invalidate_dynamic_settings_cache

    ts = now_dt if now_dt is not None else utc_now()
    bind = session.get_bind()
    dialect_name = getattr(getattr(bind, "dialect", None), "name", "") if bind is not None else ""
    if dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _dialect_insert
    else:
        from sqlalchemy.dialects.postgresql import insert as _dialect_insert

    stmt = (
        _dialect_insert(BotSetting)
        .values(key=key, value=value, updated_at=ts)
        .on_conflict_do_update(
            index_elements=[BotSetting.key],
            set_={"value": value, "updated_at": ts},
        )
    )
    await session.execute(stmt)
    invalidate_dynamic_settings_cache(key)
    if hasattr(settings, key):
        setattr(settings, key, value)


async def record_secondary_worker_heartbeat(
    session: AsyncSession,
    *,
    engine_filter: str | None = "voxcpm",
    host_override: str | None = None,
    now_dt=None,
) -> list[str]:
    """Write secondary-worker heartbeat (single upsert) + engine/host only on change."""
    import socket

    ts = now_dt if now_dt is not None else utc_now()
    hb_iso = ts.isoformat()
    engine_val = str(engine_filter or "all")
    host_val = (
        (host_override or "").strip()
        or os.getenv("VPS_PUBLIC_HOST", "").strip()
        or socket.gethostname()
        or "vps-worker"
    )

    written_keys: list[str] = ["worker_laptop_heartbeat"]
    await _upsert_bot_setting_single_roundtrip(
        session, "worker_laptop_heartbeat", hb_iso, now_dt=ts
    )
    pending_meta: dict[str, str] = {}
    if _LAST_WRITTEN_HEARTBEAT_META.get("worker_laptop_engine") != engine_val:
        await _upsert_bot_setting_single_roundtrip(
            session, "worker_laptop_engine", engine_val, now_dt=ts
        )
        written_keys.append("worker_laptop_engine")
        pending_meta["worker_laptop_engine"] = engine_val
    if host_val and _LAST_WRITTEN_HEARTBEAT_META.get("worker_laptop_host") != host_val:
        await _upsert_bot_setting_single_roundtrip(
            session, "worker_laptop_host", host_val, now_dt=ts
        )
        written_keys.append("worker_laptop_host")
        pending_meta["worker_laptop_host"] = host_val

    await session.commit()
    _LAST_WRITTEN_HEARTBEAT_META.update(pending_meta)
    return written_keys


class WorkerPollBackoff:
    """Pure stepped idle + warm-window + DB-error backoff state machine (Design 2 A2)."""

    def __init__(
        self,
        *,
        idle_steps: tuple[float, ...] = WORKER_IDLE_BACKOFF_STEPS,
        warm_window_s: float = WORKER_WARM_WINDOW_SECONDS,
        error_steps: tuple[float, ...] = WORKER_DB_ERROR_BACKOFF_STEPS,
    ) -> None:
        self.idle_steps = tuple(float(x) for x in idle_steps) or (2.0,)
        self.warm_window_s = float(warm_window_s)
        self.error_steps = tuple(float(x) for x in error_steps) or (5.0,)
        self.empty_streak: int = 0
        self.error_streak: int = 0
        self.last_job_completed_at: float | None = None

    def on_job_claimed(self) -> None:
        self.empty_streak = 0
        self.error_streak = 0

    def on_job_completed(self, now_monotonic: float) -> None:
        self.empty_streak = 0
        self.error_streak = 0
        self.last_job_completed_at = float(now_monotonic)

    def on_empty_tick(self, now_monotonic: float, *, active: bool = False) -> float:
        self.error_streak = 0
        in_warm = (
            self.last_job_completed_at is not None
            and (float(now_monotonic) - self.last_job_completed_at) < self.warm_window_s
        )
        if active or in_warm:
            self.empty_streak = 0
            return self.idle_steps[0]
        idx = min(self.empty_streak, len(self.idle_steps) - 1)
        delay = self.idle_steps[idx]
        self.empty_streak += 1
        return delay

    def on_db_error(self) -> float:
        idx = min(self.error_streak, len(self.error_steps) - 1)
        delay = self.error_steps[idx]
        self.error_streak += 1
        return delay


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


async def run_worker_loop(
    bot: Bot | None = None,
    poll_interval: float = 2.0,
    engine_filter: str = "voxcpm",
    skip_local_source: bool | None = None,
    allow_recap: bool | None = None,
) -> None:
    """Continuously poll for QUEUED jobs (defaulting to VoxCPM cloud engine) and process them."""
    logger.info("Starting background audio processing worker loop (engine_filter=%s)...", engine_filter)

    # Startup recovery: sweep orphaned RUNNING jobs before starting polling (primary only)
    if settings.instance_primary:
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
    # Primary-only (Phase 1): secondary workers skip this to avoid
    # killing jobs actively rendering on the primary.
    import time as _time

    _loop_started = _time.monotonic()
    _last_stale_sweep = 0.0
    _last_heartbeat = 0.0
    _last_hf_reconcile = 0.0
    _active_recap_tasks: set[asyncio.Task] = set()
    _idle_steps = (
        WORKER_IDLE_BACKOFF_STEPS
        if poll_interval == 2.0
        else (float(poll_interval),)
    )
    _backoff = WorkerPollBackoff(idle_steps=_idle_steps)

    while True:
        try:
            _now_m = _time.monotonic()
            if _last_hf_reconcile == 0.0 or (_now_m - _last_hf_reconcile >= 900.0):
                _last_hf_reconcile = _now_m
                try:
                    from app.services.voxcpm_engine import get_available_hf_tokens

                    _hf_toks = get_available_hf_tokens()
                    if _hf_toks and getattr(settings, "database_url", "") and "sqlite" not in str(getattr(settings, "database_url", "")).lower():
                        await asyncio.to_thread(_hf_ledger.reconcile_if_env_changed, _hf_toks)
                except Exception as hf_exc:
                    logger.debug("HF pool ledger env-change check skipped in worker: %s", hf_exc)

            is_vps_worker = not bool(getattr(settings, "instance_primary", True))
            if is_vps_worker and str(getattr(settings, "worker_laptop_status", "ONLINE")).upper() == "PAUSED":
                from app.services.dynamic_settings import refresh_dynamic_settings_if_changed

                async with AsyncSessionLocal() as paused_session:
                    await refresh_dynamic_settings_if_changed(paused_session)
                if str(getattr(settings, "worker_laptop_status", "ONLINE")).upper() == "PAUSED":
                    if _last_heartbeat == 0.0 or (_now_m - _last_heartbeat >= WORKER_HEARTBEAT_INTERVAL_SECONDS):
                        _last_heartbeat = _now_m
                        try:
                            async with AsyncSessionLocal() as hb_session:
                                await record_secondary_worker_heartbeat(
                                    hb_session, engine_filter=engine_filter
                                )
                        except Exception as hb_exc:
                            logger.debug("Failed to record worker heartbeat while paused: %s", hb_exc)
                    await asyncio.sleep(_backoff.on_empty_tick(_now_m))
                    continue

            # Concurrency: maintain active recap task set (per-instance limit = RECAP_WORKER_CONCURRENCY)
            _active_recap_tasks = {t for t in _active_recap_tasks if not t.done()}
            recap_concurrency = getattr(settings, "recap_worker_concurrency", 1)
            can_claim_recap = (
                (allow_recap and len(_active_recap_tasks) < recap_concurrency)
                if allow_recap is not None
                else None
            )

            # Shared idle-tick session inside claim_and_process_next_job:
            # runs watermark settings refresh (15 s gated) -> SELECT 1 pre-probe -> claim.
            # Any DB exception here short-circuits the tick into 5 -> 15 -> 30 s backoff.
            _consume_worker_job_claimed()
            processed = await claim_and_process_next_job(
                bot=bot,
                engine_filter=engine_filter,
                skip_local_source=skip_local_source,
                allow_recap=can_claim_recap,
                background_recap=True,
                active_recap_tasks=_active_recap_tasks,
            )
            claimed_in_tick = _consume_worker_job_claimed()

            _now_after = _time.monotonic()
            if is_vps_worker and (_last_heartbeat == 0.0 or (_now_after - _last_heartbeat >= WORKER_HEARTBEAT_INTERVAL_SECONDS)):
                _last_heartbeat = _now_after
                try:
                    async with AsyncSessionLocal() as hb_session:
                        await record_secondary_worker_heartbeat(
                            hb_session, engine_filter=engine_filter
                        )
                except Exception as hb_exc:
                    logger.debug("Failed to record worker heartbeat: %s", hb_exc)

            if settings.instance_primary and _boot_cutoff_sweep_due(_loop_started, _last_stale_sweep, _now_after):
                _last_stale_sweep = _now_after
                try:
                    async with AsyncSessionLocal() as session:
                        recovered = await recover_stale_running_jobs(
                            session=session,
                            bot=bot,
                            stale_timeout_seconds=settings.stale_job_timeout_seconds,
                            recap_stale_timeout_seconds=getattr(settings, "recap_stale_job_timeout_seconds", 2700),
                        )
                        if recovered:
                            logger.info("Periodic sweep recovered %d stranded jobs: %s", len(recovered), recovered)
                except Exception as e:
                    logger.warning("Error during periodic stale job recovery sweep: %s", e)

            _active_recap_tasks = {t for t in _active_recap_tasks if not t.done()}
            if processed or claimed_in_tick:
                _backoff.on_job_claimed()
                _backoff.on_job_completed(_now_after)
            else:
                sleep_s = _backoff.on_empty_tick(_now_after, active=bool(_active_recap_tasks))
                await asyncio.sleep(sleep_s)
        except asyncio.CancelledError:
            logger.info("Worker loop cancelled, shutting down.")
            break
        except Exception as e:
            err_sleep = _backoff.on_db_error()
            logger.error("Error in worker loop (backing off %.0fs): %s", err_sleep, e)
            await asyncio.sleep(err_sleep)

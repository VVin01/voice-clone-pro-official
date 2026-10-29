"""
transcriber.py - AI Audio/Video Transcription to SRT Subtitles
Uses faster-whisper (CTranslate2) for fast, lightweight, and offline transcription.
"""

import os
import time
import shutil
import subprocess
import logging
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_whisper_model = None
_current_model_size = None


def format_srt_timestamp(seconds: float) -> str:
    """Convert float seconds to standard SRT timestamp format: HH:MM:SS,mmm"""
    if seconds < 0:
        seconds = 0.0
    millis = int(round(seconds * 1000))
    secs, ms = divmod(millis, 1000)
    mins, s = divmod(secs, 60)
    hrs, m = divmod(mins, 60)
    return f"{hrs:02d}:{m:02d}:{s:02d},{ms:03d}"


def _transcribe_with_groq_srt(
    audio_file_path: str,
    output_srt_path: str,
    language: Optional[str] = None
) -> Optional[Tuple[str, str, float]]:
    """Transcribe audio to SRT via Groq Cloud Whisper API for ultra-fast (sub-3s) processing.

    Falls back to None if no keys configured or if all Groq attempts fail.
    """
    raw_keys = os.getenv("GROQ_API_KEYS", "") or os.getenv("GROQ_API_KEY", "")
    api_keys = [k.strip() for k in raw_keys.split(",") if k.strip()]
    if not api_keys:
        return None

    try:
        import requests
    except ImportError:
        logger.warning("⚠️ 'requests' module not available for Groq API call")
        return None

    path = Path(audio_file_path)
    upload_path = path
    temp_mp3: Optional[Path] = None

    try:
        ff = shutil.which("ffmpeg")
        # Pre-compress to lightweight mono 64k MP3 for rapid network transfer if not already small mp3
        if ff and (path.suffix.lower() != ".mp3" or path.stat().st_size > 8 * 1024 * 1024):
            try:
                temp_mp3 = path.parent / f"_groq_srt_{path.stem}_{int(time.time())}.mp3"
                cmd = [
                    str(ff), "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k",
                    str(temp_mp3)
                ]
                proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
                if proc.returncode == 0 and temp_mp3.is_file() and temp_mp3.stat().st_size > 0:
                    upload_path = temp_mp3
            except Exception as comp_err:
                logger.warning(f"Audio pre-compression skipped: {comp_err}")

        groq_model = os.getenv("GROQ_WHISPER_MODEL", "").strip()
        if not groq_model:
            try:
                from app.config import settings as _settings
                groq_model = getattr(_settings, "groq_whisper_model", "") or "whisper-large-v3-turbo"
            except Exception:
                groq_model = "whisper-large-v3-turbo"

        for idx, key in enumerate(api_keys, 1):
            key_masked = f"{key[:7]}...{key[-4:]}" if len(key) > 12 else "key"
            try:
                logger.info(f"⚡ Attempting Groq Cloud Whisper SRT transcription ({key_masked}, model={groq_model})...")
                t0 = time.time()
                with open(upload_path, "rb") as f:
                    files = {"file": (upload_path.name, f, "audio/mpeg" if upload_path.suffix == ".mp3" else "audio/wav")}
                    data: dict[str, str] = {
                        "model": groq_model,
                        "response_format": "verbose_json",
                        "temperature": "0.0",
                    }
                    if language and language != "auto":
                        data["language"] = language

                    resp = requests.post(
                        "https://api.groq.com/openai/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {key}"},
                        files=files,
                        data=data,
                        timeout=90,
                    )

                if resp.status_code == 200:
                    raw_json = resp.json()
                    segments = raw_json.get("segments") or []
                    detected_lang = raw_json.get("language") or language or "unknown"
                    prob = 1.0

                    srt_entries = []
                    idx_entry = 1
                    for seg in segments:
                        text = str(seg.get("text") or "").strip()
                        if not text:
                            continue
                        start_ts = format_srt_timestamp(float(seg.get("start", 0.0) or 0.0))
                        end_ts = format_srt_timestamp(float(seg.get("end", 0.0) or 0.0))
                        srt_entries.append(f"{idx_entry}\n{start_ts} --> {end_ts}\n{text}\n")
                        idx_entry += 1

                    os.makedirs(os.path.dirname(os.path.abspath(output_srt_path)), exist_ok=True)
                    with open(output_srt_path, "w", encoding="utf-8") as f:
                        if srt_entries:
                            f.write("\n".join(srt_entries) + "\n")
                        else:
                            f.write("1\n00:00:00,000 --> 00:00:05,000\n[No audible dialogue detected]\n")

                    elapsed = time.time() - t0
                    logger.info(f"⚡ Groq Cloud Whisper generated SRT in {elapsed:.2f}s ({len(srt_entries)} cues, lang={detected_lang})")
                    return output_srt_path, detected_lang, prob
                else:
                    logger.warning(f"Groq Whisper key {idx} HTTP {resp.status_code}: {resp.text[:200]}")
            except Exception as exc:
                logger.warning(f"Groq Whisper key {idx} failed: {exc}")
    finally:
        if temp_mp3 and temp_mp3.is_file():
            try:
                temp_mp3.unlink(missing_ok=True)
            except Exception:
                pass

    logger.info("↩️ Groq Whisper unavailable, falling back to local faster-whisper model...")
    return None


def get_whisper_model(model_size: Optional[str] = None):
    """
    Lazy-load and cache the WhisperModel singleton.
    Default model is 'base' (can be overridden by WHISPER_MODEL in .env).
    """
    global _whisper_model, _current_model_size

    target_size = model_size or os.getenv("WHISPER_MODEL", "base").strip()

    if _whisper_model is None or _current_model_size != target_size:
        logger.info(f"Loading faster-whisper model '{target_size}' on CPU (int8)...")
        from faster_whisper import WhisperModel
        _whisper_model = WhisperModel(
            target_size,
            device="cpu",
            compute_type="int8",
            cpu_threads=os.cpu_count() or 4
        )
        _current_model_size = target_size
        logger.info(f"faster-whisper model '{target_size}' loaded successfully.")

    return _whisper_model


def transcribe_to_srt(
    audio_file_path: str,
    output_srt_path: Optional[str] = None,
    model_size: Optional[str] = None,
    language: Optional[str] = None
) -> Tuple[str, str, float]:
    """
    Transcribes an audio or video file and saves as a standard .srt subtitle file.
    
    Returns:
        Tuple[output_srt_path, detected_language, language_probability]
    """
    if not os.path.exists(audio_file_path):
        raise FileNotFoundError(f"Audio file not found: {audio_file_path}")

    if not output_srt_path:
        base, _ = os.path.splitext(audio_file_path)
        output_srt_path = f"{base}_ai.srt"

    # 1. Fast path: Groq Cloud Whisper API (sub-3s zero RAM)
    groq_result = _transcribe_with_groq_srt(audio_file_path, output_srt_path, language=language)
    if groq_result:
        return groq_result

    # 2. Resilient fallback: Local faster-whisper on CPU
    model = get_whisper_model(model_size)

    beam_size = int(os.getenv("WHISPER_BEAM_SIZE", "2"))

    # Transcribe audio with speech-to-text
    # beam_size=2 balances speed and quality on CPU
    segments, info = model.transcribe(
        audio_file_path,
        language=language if language and language != "auto" else None,
        beam_size=beam_size,
        vad_filter=True, # Filter out silent portions
        vad_parameters=dict(min_silence_duration_ms=500)
    )

    detected_lang = info.language or "unknown"
    prob = info.language_probability or 0.0
    logger.info(f"Transcribing audio. Detected language: {detected_lang} (prob: {prob:.2f})")

    srt_entries = []
    index = 1

    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue
        start_ts = format_srt_timestamp(segment.start)
        end_ts = format_srt_timestamp(segment.end)
        srt_entries.append(f"{index}\n{start_ts} --> {end_ts}\n{text}\n")
        index += 1

    # Ensure parent directory exists
    os.makedirs(os.path.dirname(os.path.abspath(output_srt_path)), exist_ok=True)

    with open(output_srt_path, "w", encoding="utf-8") as f:
        if srt_entries:
            f.write("\n".join(srt_entries) + "\n")
        else:
            # Fallback if no speech detected
            f.write("1\n00:00:00,000 --> 00:00:05,000\n[No audible dialogue detected]\n")

    return output_srt_path, detected_lang, prob

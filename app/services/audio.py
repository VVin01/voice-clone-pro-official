from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import re
import shutil
import subprocess
import wave
import edge_tts

from app.config import settings

logger = logging.getLogger(__name__)

# Final-audit batch M: bound the ffmpeg normalization below (short
# voice samples; same 120s scale as recap_engine.core's probe cap).
NORMALIZE_TIMEOUT_SECONDS = 120

# Batch AJ / M11: bound a single Edge TTS save. A stalled
# third-party socket used to pin the voice worker forever; 120 s is
# the same spirit as the VoxCPM budget scale (240 s total) for one
# short voice line.
EDGE_TTS_TIMEOUT_SECONDS = 120

# Batch AJ / L21: ffmpeg/ffprobe may only open local files and
# pipes — never network protocols — for the media handled here.
MEDIA_PROTOCOL_WHITELIST = "file,pipe"


class UnreadableAudioDurationError(RuntimeError):
    """Batch AJ / L4: the produced audio's duration cannot be read.

    Raised instead of returning a fabricated duration: the caller
    (app/worker.py process_single_job) calls the duration probe
    inside its job-level try, whose generic failure path fails and
    refunds the job — an honest failure instead of a made-up 5 s
    charge on an unreadable file.
    """

CURATED_TTS_VOICES = {
    "my-MM-ThihaNeural": {"name": "Thiha (မြန်မာ)", "gender": "male", "lang": "my"},
    "my-MM-NilarNeural": {"name": "Nilar (မြန်မာ)", "gender": "female", "lang": "my"},
    "en-US-GuyNeural": {"name": "Guy (English)", "gender": "male", "lang": "en"},
    "en-US-AriaNeural": {"name": "Aria (English)", "gender": "female", "lang": "en"},
}

SPEED_RATES = {
    "slow": "-15%",
    "normal": "+0%",
    "fast": "+15%",
}

GENDER_PITCH_MAP = {
    "m2m": 0,
    "m2f": 12,
    "f2f": 0,
    "f2m": -12,
}


def sanitize_filename_token(name: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_\u1000-\u109F-]+", "_", name.strip()).strip("_")
    return cleaned or "model"


def get_output_filename(model_name: str) -> str:
    safe_name = sanitize_filename_token(model_name)
    return f"VoiceClonePro({safe_name}).wav"


def get_wav_duration_seconds(wav_path: Path) -> int:
    try:
        with wave.open(str(wav_path), "rb") as wf:
            frames = wf.getnframes()
            rate = wf.getframerate()
            if rate > 0:
                return max(1, int(round(frames / float(rate))))
    except Exception:
        pass

    # Fallback to ffprobe for MP3 or non-standard WAV
    try:
        import subprocess
        import shutil
        ffprobe_bin = shutil.which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"
        cmd = [
            ffprobe_bin, "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST,
            "-v", "error", "-show_entries",
            "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
            str(wav_path)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if res.returncode == 0 and res.stdout.strip():
            val = float(res.stdout.strip())
            return max(1, int(round(val)))
    except Exception as e:
        logger.debug("ffprobe duration inspect fallback: %s", e)

    # Batch AJ / L4: BOTH probes failed — the old code returned a
    # hard-coded 5, so an unreadable output was billed a fabricated
    # 5 seconds and delivered as SUCCESS. Fail honestly instead.
    raise UnreadableAudioDurationError(
        f"Could not determine the duration of the generated audio at {wav_path} "
        "(WAV header and ffprobe both failed) — failing the job instead of "
        "billing a fabricated duration."
    )


async def generate_edge_tts(
    text: str,
    out_path: Path,
    voice: str = "my-MM-ThihaNeural",
    speed: str = "normal",
) -> Path:
    """Synthesize text using Edge TTS."""
    if voice not in CURATED_TTS_VOICES:
        voice = "my-MM-ThihaNeural"

    rate_str = SPEED_RATES.get(speed, "+0%")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    communicate = edge_tts.Communicate(text, voice, rate=rate_str)
    # Batch AJ / M11: a per-call timeout — a stalled Edge socket must
    # fail the job, not pin the voice worker indefinitely.
    await asyncio.wait_for(
        communicate.save(str(out_path)), timeout=EDGE_TTS_TIMEOUT_SECONDS
    )

    if not out_path.is_file() or out_path.stat().st_size == 0:
        raise RuntimeError(f"Edge TTS failed to produce output at {out_path}")

    return out_path


async def normalize_audio_to_wav(
    in_audio: Path,
    out_wav: Path,
) -> Path:
    """Normalize input audio to 40kHz, 16-bit PCM, mono WAV via FFmpeg."""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST, "-y",
        "-i", str(in_audio),
        "-ar", "40000",
        "-ac", "1",
        "-c:a", "pcm_s16le",
        str(out_wav),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=NORMALIZE_TIMEOUT_SECONDS
        )
    except (asyncio.TimeoutError, TimeoutError):
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError(
            f"FFmpeg normalization timed out after {NORMALIZE_TIMEOUT_SECONDS}s"
        )

    if proc.returncode != 0:
        err_msg = stderr.decode("utf-8", errors="replace") if stderr else "Unknown error"
        raise RuntimeError(f"FFmpeg normalization failed (exit {proc.returncode}): {err_msg}")

    return out_wav


def concat_wav_files(parts: list[Path], output_wav: Path) -> Path:
    """Batch CH: join WAV parts into one file, in order.

    Same ffmpeg concat-demuxer pattern as the recap engine's
    concat_audio, but the voice path stays MONO and keeps the first
    part's sample rate, so a chunked voice job's output format matches
    what a single-call job from the same Space would have produced.
    Synchronous: callers run it inside an executor.
    """
    part_paths = [Path(p) for p in parts]
    if not part_paths:
        raise ValueError("concat_wav_files needs at least one part")
    output_wav.parent.mkdir(parents=True, exist_ok=True)
    if len(part_paths) == 1:
        shutil.copy2(part_paths[0], output_wav)
        return output_wav

    rate = 48000
    try:
        with wave.open(str(part_paths[0]), "rb") as wf:
            rate = wf.getframerate() or rate
    except Exception:
        pass

    list_file = output_wav.parent / f"{output_wav.stem}.parts.txt"
    lines = []
    for p in part_paths:
        escaped = str(p.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cmd = [
        "ffmpeg", "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST, "-y",
        "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-ar", str(rate), "-ac", "1", "-c:a", "pcm_s16le",
        str(output_wav),
    ]
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=NORMALIZE_TIMEOUT_SECONDS,
        )
    finally:
        try:
            list_file.unlink(missing_ok=True)
        except Exception:
            pass
    if res.returncode != 0:
        raise RuntimeError(
            f"WAV concat failed (exit {res.returncode}): {(res.stderr or '')[-500:]}"
        )
    if not output_wav.is_file() or output_wav.stat().st_size == 0:
        raise RuntimeError(f"WAV concat produced no output at {output_wav}")
    return output_wav

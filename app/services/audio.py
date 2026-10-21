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
            ffprobe_bin, "-v", "error", "-show_entries",
            "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
            str(wav_path)
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if res.returncode == 0 and res.stdout.strip():
            val = float(res.stdout.strip())
            return max(1, int(round(val)))
    except Exception as e:
        logger.debug("ffprobe duration inspect fallback: %s", e)

    return 5  # Conservative default fallback


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
    await communicate.save(str(out_path))

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
        "ffmpeg", "-y",
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
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        err_msg = stderr.decode("utf-8", errors="replace") if stderr else "Unknown error"
        raise RuntimeError(f"FFmpeg normalization failed (exit {proc.returncode}): {err_msg}")

    return out_wav


def resolve_applio_runner_path() -> Path:
    """Resolve the Applio inference bridge runner script path.

    Returns the explicitly configured APPLIO_RUNNER if set,
    otherwise defaults to `<project_root>/worker/applio_runner.py`.
    """
    if settings.applio_runner:
        return Path(settings.applio_runner)
    return settings.root / "worker" / "applio_runner.py"


def resolve_applio_python_path() -> Path:
    """Resolve the Applio virtualenv Python executable path.

    Returns the explicitly configured APPLIO_PYTHON if set,
    otherwise defaults to `<APPLIO_WORKDIR>/env/python.exe`.
    """
    if settings.applio_python:
        return Path(settings.applio_python)
    return Path(settings.applio_workdir) / "env" / "python.exe"


async def run_applio_conversion(
    model_pth: Path,
    index_path: Path | None,
    input_wav: Path,
    output_wav: Path,
    pitch: int = 0,
    f0_method: str = "rmvpe",
    timeout_seconds: int | None = None,
) -> Path:
    """Run Applio conversion subprocess using the isolated bridge."""
    if timeout_seconds is None:
        timeout_seconds = getattr(settings, "applio_inference_timeout_seconds", 900)

    applio_workdir = Path(settings.applio_workdir)
    applio_python = resolve_applio_python_path()
    bridge_script = resolve_applio_runner_path()

    output_wav.parent.mkdir(parents=True, exist_ok=True)

    # Resilient path resolution for model_pth and index_path
    if not model_pth.is_file():
        candidate = Path(settings.root) / model_pth
        if candidate.is_file():
            model_pth = candidate
        else:
            for found in Path(settings.storage_root).rglob(model_pth.name):
                if found.is_file():
                    model_pth = found
                    break

    if index_path and not index_path.is_file():
        candidate_idx = Path(settings.root) / index_path
        if candidate_idx.is_file():
            index_path = candidate_idx
        else:
            for found in Path(settings.storage_root).rglob(index_path.name):
                if found.is_file():
                    index_path = found
                    break

    if not applio_python.is_file() or not bridge_script.is_file():
        logger.warning(f"Applio not found ({applio_python}). Using direct audio fallback.")
        shutil.copy2(input_wav, output_wav)
        return output_wav

    cmd = [
        str(applio_python), str(bridge_script),
        "--model", str(model_pth),
        "--index", str(index_path or ""),
        "--input", str(input_wav),
        "--output", str(output_wav),
        "--pitch", str(pitch),
        "--f0-method", f0_method,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(applio_workdir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except OSError:
            pass
        raise RuntimeError(f"Applio inference timed out after {timeout_seconds}s")

    if proc.returncode != 0:
        err_msg = stderr.decode("utf-8", errors="replace") if stderr else "Inference failed"
        raise RuntimeError(f"Applio inference failed (exit {proc.returncode}): {err_msg}")

    if not output_wav.is_file():
        raise RuntimeError(f"Applio output not found at {output_wav}")

    return output_wav

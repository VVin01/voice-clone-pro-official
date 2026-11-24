from __future__ import annotations

import asyncio
import logging
import math
from pathlib import Path
import re
import shutil
import struct
import subprocess
import wave
import edge_tts

from app.config import settings
from app.services.speed import DEFAULT_SPEED, SPEED_RATES, resolve_speed_rate

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

# Legacy label map (SPEED_RATES) is imported and re-exported from app.services.speed.

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
    speed: str = DEFAULT_SPEED,
) -> Path:
    """Synthesize text using Edge TTS."""
    if voice not in CURATED_TTS_VOICES:
        voice = "my-MM-ThihaNeural"

    rate_str = resolve_speed_rate(speed)
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


def is_wav_edge_clipped(
    wav_path: Path,
    edge_ms: float = 20.0,
    peak_threshold: float = 0.20,
    rms_threshold: float = 0.06,
) -> bool:
    """Returns True if the WAV starts or ends mid-energy with no natural decay."""
    try:
        p = Path(wav_path)
        if not p.is_file() or p.stat().st_size == 0:
            return False
        with wave.open(str(p), "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            n_frames = wf.getnframes()
            if n_frames == 0 or sampwidth != 2:
                return False

            edge_frames = min(int(framerate * (edge_ms / 1000.0)), n_frames // 4)
            if edge_frames <= 0:
                return False

            boundary_frames = min(int(framerate * 0.005), edge_frames)
            if boundary_frames <= 0:
                return False

            wf.setpos(0)
            start_raw = wf.readframes(boundary_frames)
            wf.setpos(max(0, n_frames - boundary_frames))
            end_raw = wf.readframes(boundary_frames)

            def check_energy(raw_bytes: bytes) -> bool:
                count = len(raw_bytes) // 2
                if count == 0:
                    return False
                samples = struct.unpack(f"<{count}h", raw_bytes)
                peak = max(abs(s) for s in samples) / 32768.0
                rms = math.sqrt(sum(s * s for s in samples) / count) / 32768.0
                return peak > peak_threshold or rms > rms_threshold

            return check_energy(start_raw) or check_energy(end_raw)
    except Exception:
        return False


def concat_wav_files(
    parts: list[Path],
    output_wav: Path,
    crossfade_sec: float = 0.04,
    boundary_pause_sec: float = 0.0,
) -> Path:
    """Batch CH + Prosody Phase 1: join WAV parts into one file, in order.

    When ``boundary_pause_sec > 0`` (long-form voice clone sentence boundaries),
    inserts clean digital silence between parts with a 10 ms anti-click micro-fade.
    Otherwise joins with a short crossfade (acrossfade) when multiple parts exist,
    falling back to the concat-demuxer. Keeps MONO and the first part's sample rate.
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
            rate = wf.getframerate() or 48000
    except Exception:
        pass

    if boundary_pause_sec and float(boundary_pause_sec) > 0.0:
        import numpy as np

        pause_frames = max(1, int(round(rate * float(boundary_pause_sec))))
        silence = np.zeros(pause_frames, dtype="<i2")
        fade_frames = max(1, int(round(rate * 0.010)))  # 10 ms anti-click fade
        rendered_chunks: list[np.ndarray] = []

        for idx, p in enumerate(part_paths):
            arr_i16: np.ndarray | None = None
            try:
                with wave.open(str(p), "rb") as wf:
                    ch = wf.getnchannels()
                    sw = wf.getsampwidth()
                    sr = wf.getframerate() or rate
                    raw = wf.readframes(wf.getnframes())
                if sw == 2 and ch >= 1 and len(raw) >= 2 * ch:
                    s = np.frombuffer(raw, dtype="<i2").astype(np.float32)
                    if ch > 1:
                        s = s[: (s.size // ch) * ch].reshape(-1, ch).mean(axis=1)
                    if sr != rate and s.size > 1:
                        t_len = max(1, int(round(s.size * (float(rate) / float(sr)))))
                        s = np.interp(
                            np.linspace(0.0, float(s.size - 1), num=t_len),
                            np.arange(s.size, dtype=np.float64),
                            s,
                        ).astype(np.float32)
                    arr_i16 = np.clip(s, -32768.0, 32767.0).astype("<i2")
            except Exception:
                arr_i16 = None

            if arr_i16 is None:
                proc = subprocess.run(
                    [
                        "ffmpeg", "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST,
                        "-hide_banner", "-loglevel", "error",
                        "-i", str(p.resolve()), "-ar", str(rate), "-ac", "1", "-f", "s16le", "pipe:1",
                    ],
                    capture_output=True,
                    timeout=NORMALIZE_TIMEOUT_SECONDS,
                    check=False,
                )
                if proc.returncode != 0 or not proc.stdout:
                    raise RuntimeError(f"WAV pause-concat failed reading part {p}")
                arr_i16 = np.frombuffer(proc.stdout, dtype="<i2").copy()
            else:
                arr_i16 = arr_i16.copy()

            if arr_i16.size > 2 * fade_frames:
                ramp = np.linspace(0.0, 1.0, fade_frames, dtype=np.float32)
                if idx > 0:
                    arr_i16[:fade_frames] = (arr_i16[:fade_frames].astype(np.float32) * ramp).astype("<i2")
                if idx < len(part_paths) - 1:
                    arr_i16[-fade_frames:] = (arr_i16[-fade_frames:].astype(np.float32) * ramp[::-1]).astype("<i2")

            rendered_chunks.append(arr_i16)
            if idx < len(part_paths) - 1:
                rendered_chunks.append(silence)

        joined = np.concatenate(rendered_chunks) if rendered_chunks else np.zeros(0, dtype="<i2")
        with wave.open(str(output_wav), "wb") as out_wf:
            out_wf.setnchannels(1)
            out_wf.setsampwidth(2)
            out_wf.setframerate(rate)
            out_wf.writeframes(joined.tobytes())
        return output_wav

    min_dur = 9999.0
    for p in part_paths:
        try:
            with wave.open(str(p), "rb") as wf:
                r = wf.getframerate() or 48000
                dur = wf.getnframes() / float(r)
                if dur < min_dur:
                    min_dur = dur
        except Exception:
            pass

    # Try acrossfade join when crossfade_sec > 0
    actual_xfade = min(crossfade_sec, min_dur / 2.0) if min_dur > 0.02 else 0.0
    if actual_xfade >= 0.005:
        filter_chains = []
        prev_label = "[0:a]"
        for idx in range(1, len(part_paths)):
            next_label = f"[{idx}:a]"
            out_label = "[out]" if idx == len(part_paths) - 1 else f"[a{idx}]"
            filter_chains.append(
                f"{prev_label}{next_label}acrossfade=d={actual_xfade:.3f}:c1=tri:c2=tri{out_label}"
            )
            prev_label = out_label
        filter_complex = ";".join(filter_chains)

        cmd = [
            "ffmpeg", "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST, "-y",
            "-hide_banner", "-loglevel", "error",
        ]
        for p in part_paths:
            cmd.extend(["-i", str(p.resolve())])
        cmd.extend([
            "-filter_complex", filter_complex,
            "-map", "[out]",
            "-ar", str(rate), "-ac", "1", "-c:a", "pcm_s16le",
            str(output_wav),
        ])
        try:
            res = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=NORMALIZE_TIMEOUT_SECONDS,
            )
            if res.returncode == 0 and output_wav.is_file() and output_wav.stat().st_size > 0:
                return output_wav
        except Exception:
            pass

    # Fallback to concat demuxer
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

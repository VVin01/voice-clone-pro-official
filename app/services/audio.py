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


def _decode_wav_part_mono_i16(part_path: Path, target_rate: int):
    """Decode one audio file to mono 16-bit PCM samples at ``target_rate``.

    Fast-paths standard 16-bit PCM WAVs directly via ``wave`` + ``numpy`` (with
    channel downmix and linear resampling when needed) and falls back to a
    single-input ``ffmpeg -f s16le`` decode for non-standard WAV/audio formats.
    """
    import numpy as np

    arr_i16 = None
    try:
        with wave.open(str(part_path), "rb") as wf:
            ch = wf.getnchannels()
            sw = wf.getsampwidth()
            sr = wf.getframerate() or target_rate
            raw = wf.readframes(wf.getnframes())
        if sw == 2 and ch >= 1 and len(raw) >= 2 * ch:
            s = np.frombuffer(raw, dtype="<i2").astype(np.float32)
            if ch > 1:
                s = s[: (s.size // ch) * ch].reshape(-1, ch).mean(axis=1)
            if sr != target_rate and s.size > 1:
                t_len = max(1, int(round(s.size * (float(target_rate) / float(sr)))))
                s = np.interp(
                    np.linspace(0.0, float(s.size - 1), num=t_len),
                    np.arange(s.size, dtype=np.float64),
                    s,
                ).astype(np.float32)
            arr_i16 = np.clip(s, -32768.0, 32767.0).astype("<i2").copy()
    except Exception:
        arr_i16 = None

    if arr_i16 is None:
        proc = subprocess.run(
            [
                "ffmpeg", "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST,
                "-hide_banner", "-loglevel", "error",
                "-i", str(part_path.resolve()),
                "-ar", str(target_rate), "-ac", "1", "-f", "s16le", "pipe:1",
            ],
            capture_output=True,
            timeout=NORMALIZE_TIMEOUT_SECONDS,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout:
            raise RuntimeError(f"WAV concat failed reading part {part_path}")
        arr_i16 = np.frombuffer(proc.stdout, dtype="<i2").copy()

    return arr_i16


ROOM_TONE_TARGET_DBFS: float = -58.0
SEAM_COSINE_FADE_SEC: float = 0.035
_LEGACY_SEAM_LINEAR_FADE_SEC: float = 0.010
_ROOM_TONE_BASE_SEED: int = 20261125


def _is_phase3_seam_bed_enabled() -> bool:
    try:
        val = getattr(settings, "clone_prosody_phase3_enabled", True)
        if isinstance(val, str):
            return val.strip().lower() not in ("0", "false", "no", "off")
        return bool(val)
    except Exception:
        return True


def _estimate_job_voiced_envelope(parts_i16: list, rate: int, n_fft: int = 1024):
    """Estimate smooth speech-band magnitude envelope from decoded chunks for comfort noise."""
    import numpy as np

    freqs = np.fft.rfftfreq(n_fft, d=1.0 / float(rate))
    # Default smooth studio room-tone tilt (band-limited 75..6200 Hz, ~-6 dB/octave speech shape)
    default_env = 1.0 / np.maximum(180.0, freqs)
    default_env[freqs < 75.0] *= np.clip(freqs[freqs < 75.0] / 75.0, 0.0, 1.0) ** 2
    hi_cut = min(6200.0, 0.45 * float(rate))
    if hi_cut > 200.0:
        over = freqs > hi_cut
        default_env[over] *= np.exp(-((freqs[over] - hi_cut) / max(400.0, 0.12 * rate)) ** 2)
    default_env[0] = 0.0

    accum = np.zeros_like(freqs, dtype=np.float64)
    count = 0
    win = np.hanning(n_fft).astype(np.float64)
    for arr in parts_i16:
        if arr.size < n_fft:
            continue
        # Sample up to 24 evenly spaced frames per chunk
        step = max(n_fft // 2, (arr.size - n_fft) // 24)
        for st in range(0, arr.size - n_fft + 1, step):
            seg = arr[st : st + n_fft].astype(np.float64) / 32768.0
            rms = float(np.sqrt(np.mean(seg * seg)))
            if rms < 0.008:
                continue
            spec = np.abs(np.fft.rfft(seg * win))
            accum += spec / max(1e-6, rms)
            count += 1
            if count >= 120:
                break
        if count >= 120:
            break

    if count < 3:
        return default_env

    mean_env = accum / float(count)
    # Smooth envelope over ~250 Hz kernel so no tonal harmonic peaks leak into comfort noise
    k_bins = max(3, int(round(250.0 / max(1.0, float(rate) / n_fft))))
    if k_bins % 2 == 0:
        k_bins += 1
    kernel = np.hanning(k_bins + 2)[1:-1]
    kernel /= np.sum(kernel)
    smooth = np.convolve(np.pad(mean_env, (k_bins // 2, k_bins // 2), mode="edge"), kernel, mode="valid")[: freqs.size]
    smooth[freqs < 75.0] *= np.clip(freqs[freqs < 75.0] / 75.0, 0.0, 1.0) ** 2
    if hi_cut > 200.0:
        over = freqs > hi_cut
        smooth[over] *= np.exp(-((freqs[over] - hi_cut) / max(400.0, 0.12 * rate)) ** 2)
    smooth[0] = 0.0
    if float(np.max(smooth)) <= 1e-9:
        return default_env
    return smooth


def _synthesize_room_tone_bed_i16(
    n_samples: int,
    rate: int,
    spectral_env,
    *,
    seed: int,
    target_dbfs: float = ROOM_TONE_TARGET_DBFS,
):
    """Deterministically generate ``n_samples`` of speech-shaped comfort noise at ``target_dbfs``."""
    import numpy as np

    if n_samples <= 0:
        return np.zeros(0, dtype="<i2")
    rng = np.random.default_rng(int(seed))
    gen_len = max(int(n_samples), 1024)
    white = rng.standard_normal(gen_len)
    w_spec = np.fft.rfft(white)
    env_src_x = np.linspace(0.0, 1.0, spectral_env.size)
    env_dst_x = np.linspace(0.0, 1.0, w_spec.size)
    shaped_mag = np.interp(env_dst_x, env_src_x, spectral_env)
    shaped_mag[0] = 0.0
    shaped = np.fft.irfft(w_spec * shaped_mag, n=gen_len)[:n_samples]

    cur_rms = float(np.sqrt(np.mean(shaped * shaped)))
    target_rms = 10.0 ** (float(target_dbfs) / 20.0)
    if cur_rms > 1e-12:
        shaped = shaped * (target_rms / cur_rms)
    else:
        shaped = rng.standard_normal(n_samples) * target_rms
    return np.clip(np.round(shaped * 32768.0), -32768.0, 32767.0).astype("<i2")


def concat_wav_files(
    parts: list[Path],
    output_wav: Path,
    crossfade_sec: float = 0.04,
    boundary_pause_sec: float = 0.0,
    boundary_pauses_sec: list[float] | tuple[float, ...] | None = None,
) -> Path:
    """Batch CH + Prosody Phase 1–3: join WAV parts into one file, in order.

    When ``boundary_pauses_sec`` (per-seam pause durations in seconds) or
    ``boundary_pause_sec > 0`` is supplied:
    - Phase 3 (``clone_prosody_phase3_enabled=True``): bridges each pause with
      35 ms raised-cosine fades over a deterministic -58 dBFS speech-shaped
      comfort-noise bed (pause durations unchanged).
    - Phase 2 fallback (``clone_prosody_phase3_enabled=False``): inserts digital
      silence between parts with a 10 ms linear anti-click micro-fade.
    Otherwise joins with a deterministic sample-accurate triangular crossfade.
    Keeps MONO and the first part's sample rate.
    """
    import numpy as np

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

    has_per_seam_pauses = bool(boundary_pauses_sec) and any(float(x) > 0.0 for x in boundary_pauses_sec)
    if has_per_seam_pauses or (boundary_pause_sec and float(boundary_pause_sec) > 0.0):
        default_pause_s = max(0.0, float(boundary_pause_sec or 0.0))
        use_p3_bed = _is_phase3_seam_bed_enabled()

        if not use_p3_bed:
            fade_frames = max(1, int(round(rate * _LEGACY_SEAM_LINEAR_FADE_SEC)))
            rendered_chunks: list[np.ndarray] = []
            for idx, p in enumerate(part_paths):
                arr_i16 = _decode_wav_part_mono_i16(p, rate)
                if arr_i16.size > 2 * fade_frames:
                    ramp = np.linspace(0.0, 1.0, fade_frames, dtype=np.float32)
                    if idx > 0:
                        arr_i16[:fade_frames] = (arr_i16[:fade_frames].astype(np.float32) * ramp).astype("<i2")
                    if idx < len(part_paths) - 1:
                        arr_i16[-fade_frames:] = (arr_i16[-fade_frames:].astype(np.float32) * ramp[::-1]).astype("<i2")
                rendered_chunks.append(arr_i16)
                if idx < len(part_paths) - 1:
                    seam_pause_s = (
                        float(boundary_pauses_sec[idx])
                        if (boundary_pauses_sec is not None and idx < len(boundary_pauses_sec))
                        else default_pause_s
                    )
                    pause_frames = max(1, int(round(rate * max(0.0, seam_pause_s))))
                    rendered_chunks.append(np.zeros(pause_frames, dtype="<i2"))

            joined = np.concatenate(rendered_chunks) if rendered_chunks else np.zeros(0, dtype="<i2")
            with wave.open(str(output_wav), "wb") as out_wf:
                out_wf.setnchannels(1)
                out_wf.setsampwidth(2)
                out_wf.setframerate(rate)
                out_wf.writeframes(joined.tobytes())
            return output_wav

        decoded_all = [_decode_wav_part_mono_i16(p, rate) for p in part_paths]
        spec_env = _estimate_job_voiced_envelope(decoded_all, rate)
        target_fade_frames = max(1, int(round(rate * SEAM_COSINE_FADE_SEC)))
        rendered_p3: list[np.ndarray] = []

        for idx, arr_orig in enumerate(decoded_all):
            arr_i16 = arr_orig.copy()
            eff_fade = min(target_fade_frames, max(1, arr_i16.size // 4)) if arr_i16.size >= 8 else 0
            if eff_fade >= 2:
                t_norm = np.linspace(0.0, 1.0, eff_fade, dtype=np.float64)
                w_in = (0.5 * (1.0 - np.cos(np.pi * t_norm))).astype(np.float32)
                w_out = w_in[::-1]
                if idx > 0:
                    bed_head = _synthesize_room_tone_bed_i16(
                        eff_fade, rate, spec_env, seed=_ROOM_TONE_BASE_SEED + idx * 3 + 1
                    ).astype(np.float32)
                    blended_h = arr_i16[:eff_fade].astype(np.float32) * w_in + bed_head * (1.0 - w_in)
                    arr_i16[:eff_fade] = np.clip(np.round(blended_h), -32768.0, 32767.0).astype("<i2")
                if idx < len(decoded_all) - 1:
                    bed_tail = _synthesize_room_tone_bed_i16(
                        eff_fade, rate, spec_env, seed=_ROOM_TONE_BASE_SEED + idx * 3 + 2
                    ).astype(np.float32)
                    blended_t = arr_i16[-eff_fade:].astype(np.float32) * w_out + bed_tail * (1.0 - w_out)
                    arr_i16[-eff_fade:] = np.clip(np.round(blended_t), -32768.0, 32767.0).astype("<i2")

            rendered_p3.append(arr_i16)
            if idx < len(decoded_all) - 1:
                seam_pause_s = (
                    float(boundary_pauses_sec[idx])
                    if (boundary_pauses_sec is not None and idx < len(boundary_pauses_sec))
                    else default_pause_s
                )
                pause_frames = max(1, int(round(rate * max(0.0, seam_pause_s))))
                bed_pause = _synthesize_room_tone_bed_i16(
                    pause_frames, rate, spec_env, seed=_ROOM_TONE_BASE_SEED + idx * 3
                )
                rendered_p3.append(bed_pause)

        joined = np.concatenate(rendered_p3) if rendered_p3 else np.zeros(0, dtype="<i2")
        with wave.open(str(output_wav), "wb") as out_wf:
            out_wf.setnchannels(1)
            out_wf.setsampwidth(2)
            out_wf.setframerate(rate)
            out_wf.writeframes(joined.tobytes())
        return output_wav

    decoded_parts = [_decode_wav_part_mono_i16(p, rate) for p in part_paths]
    min_dur = min((arr.size / float(rate) for arr in decoded_parts), default=0.0)
    req_xfade = float(crossfade_sec or 0.0)
    actual_xfade = min(req_xfade, min_dur / 2.0) if (req_xfade > 0.0 and min_dur > 0.02) else 0.0
    xfade_frames = int(round(rate * actual_xfade)) if actual_xfade >= 0.005 else 0

    if xfade_frames >= 1:
        acc = decoded_parts[0].astype(np.float32)
        for nxt in decoded_parts[1:]:
            nxt_f = nxt.astype(np.float32)
            n_ov = min(xfade_frames, acc.size, nxt_f.size)
            if n_ov >= 1:
                fade_out = np.linspace(1.0, 0.0, n_ov, dtype=np.float32)
                fade_in = np.linspace(0.0, 1.0, n_ov, dtype=np.float32)
                blended = acc[-n_ov:] * fade_out + nxt_f[:n_ov] * fade_in
                acc = np.concatenate([acc[:-n_ov], blended, nxt_f[n_ov:]])
            else:
                acc = np.concatenate([acc, nxt_f])
        joined = np.clip(np.round(acc), -32768.0, 32767.0).astype("<i2")
    else:
        joined = np.concatenate(decoded_parts) if decoded_parts else np.zeros(0, dtype="<i2")

    with wave.open(str(output_wav), "wb") as out_wf:
        out_wf.setnchannels(1)
        out_wf.setsampwidth(2)
        out_wf.setframerate(rate)
        out_wf.writeframes(joined.tobytes())
    if not output_wav.is_file() or output_wav.stat().st_size == 0:
        raise RuntimeError(f"WAV concat produced no output at {output_wav}")
    return output_wav

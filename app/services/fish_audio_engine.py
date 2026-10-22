"""
fish_audio_engine.py - Zero-shot Multilingual Neural TTS & Voice Cloning via Fish Audio API.
Replaces legacy VoxCPM with Fish Audio's high-fidelity "s2.1-pro-free" model.
Supports inline zero-shot voice cloning via MessagePack, curated personas,
prosody controls, live rate-limit logging, and seamless Edge Neural TTS fallback.

API Reference (Fish Audio v1 TTS):
  endpoint: https://api.fish.audio/v1/tts
  model:    s2.1-pro-free
  auth:     Authorization: Bearer <FISH_AUDIO_API_KEY>
"""

from __future__ import annotations

import os
import sys
import time
import shutil
import hashlib
import logging
import threading
from pathlib import Path
from typing import Optional, Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_FISH_AUDIO_ENDPOINT = (
    os.getenv("FISH_AUDIO_ENDPOINT", "").strip()
    or "https://api.fish.audio/v1/tts"
)
DEFAULT_FISH_AUDIO_MODEL = (
    os.getenv("FISH_AUDIO_MODEL", "").strip()
    or "s2.1-pro-free"
)
DEFAULT_FISH_AUDIO_API_KEY = (
    os.getenv("FISH_AUDIO_API_KEY", "").strip()
    or "sk-fish-LWGEkWhc5XBzIKwC11vJXvjmreeUS-Jd-haRWvuQKUw"
)
DEFAULT_BURMESE_CONTROL = (
    "A clear, natural, emotionally expressive Burmese movie recap narrator with warm tone "
    "and fluent pronunciation. Speak in Myanmar language with authentic accent."
)

# Concurrency semaphore to stay well within Fish Audio Free limit (5 concurrent requests)
_MAX_CONCURRENT_REQUESTS = int(os.getenv("FISH_AUDIO_MAX_CONCURRENCY", "3"))
_FISH_AUDIO_SEMAPHORE = threading.BoundedSemaphore(_MAX_CONCURRENT_REQUESTS)


def _synthesize_neural_tts_fallback(
    text: str,
    output_wav: Path,
    control_instruction: str = "",
    is_female: bool = False,
) -> bool:
    """Seamless fallback using Microsoft Edge Neural TTS (Burmese) if Fish Audio API is unavailable."""
    try:
        import asyncio
        import edge_tts
        import subprocess

        female_keywords = ["female", "woman", "girl", "hnin", "nadi", "may", "နှင်း", "နဒီ", "မေမေ", "nilar"]
        if not is_female and any(w in str(control_instruction).lower() for w in female_keywords):
            is_female = True

        voice_id = "my-MM-NilarNeural" if is_female else "my-MM-ThihaNeural"
        temp_mp3 = output_wav.with_suffix(".fallback.mp3")

        async def _run():
            comm = edge_tts.Communicate(text, voice_id)
            await comm.save(str(temp_mp3))

        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                fut = executor.submit(lambda: asyncio.run(_run()))
                fut.result(timeout=25)
        except Exception as e_run:
            logger.warning("Direct fallback thread failed: %s; attempting direct asyncio.run", e_run)
            asyncio.run(_run())

        if temp_mp3.is_file() and temp_mp3.stat().st_size > 1000:
            ffmpeg_bin = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
            if ffmpeg_bin:
                af_master = (
                    "highpass=f=75,"
                    "lowpass=f=11000,"
                    "acompressor=threshold=-18dB:ratio=3:attack=15:release=120:makeup=2,"
                    "alimiter=limit=0.95:level=disabled"
                )
                cmd = [
                    ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", str(temp_mp3),
                    "-af", af_master,
                    "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le",
                    str(output_wav)
                ]
                subprocess.run(cmd, check=True, capture_output=True, timeout=30)
                try:
                    temp_mp3.unlink(missing_ok=True)
                except Exception:
                    pass
                return output_wav.is_file() and output_wav.stat().st_size > 1000
            else:
                shutil.move(str(temp_mp3), str(output_wav))
                return True
    except Exception as e_fb:
        logger.warning("Neural TTS fallback error: %s", e_fb)
    return False


class FishAudioEngine:
    """
    High-level client for Fish Audio Text-to-Speech & Voice Cloning.
    Uses official 's2.1-pro-free' developer tier model with:
      - MessagePack inline zero-shot voice cloning
      - Native Burmese pronunciation support
      - Active concurrency control (max 3 concurrent requests)
      - Audio mastering & normalization
      - Automatic fallback to Microsoft Edge Neural TTS
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        endpoint: Optional[str] = None,
        work_dir: Optional[Path | str] = None,
        log_fn: Optional[Callable[[str], None]] = None,
    ):
        self.api_key = (api_key or os.getenv("FISH_AUDIO_API_KEY", "")).strip() or DEFAULT_FISH_AUDIO_API_KEY
        self.model = (model or os.getenv("FISH_AUDIO_MODEL", "")).strip() or DEFAULT_FISH_AUDIO_MODEL
        self.endpoint = (endpoint or os.getenv("FISH_AUDIO_ENDPOINT", "")).strip() or DEFAULT_FISH_AUDIO_ENDPOINT
        self.work_dir = Path(work_dir) if work_dir else Path(os.environ.get("TEMP", ".")) / "fish_audio_cache"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.log_fn = log_fn

    def _log(self, msg: str) -> None:
        logger.info(msg)
        if self.log_fn:
            try:
                self.log_fn(msg)
            except Exception:
                pass

    def synthesize(
        self,
        text: str,
        output_wav: Path | str,
        control_instruction: str = DEFAULT_BURMESE_CONTROL,
        reference_wav: Optional[Path | str] = None,
        reference_id: Optional[str] = None,
        prompt_text: str = "",
        speed: float = 1.05,
        progress_cb: Optional[Callable[[str], None]] = None,
        retries: int = 2,
        **kwargs: Any,
    ) -> Path:
        """
        Synthesize Burmese speech for given text using Fish Audio API.

        Args:
            text: Input text to synthesize (Burmese script)
            output_wav: Destination path for output WAV file
            control_instruction: Style description / emotion guide
            reference_wav: Optional audio file for inline zero-shot voice cloning
            reference_id: Optional pre-uploaded Fish Audio voice ID
            prompt_text: Optional transcript of the reference audio
            speed: Speech rate multiplier (default 1.05 for natural movie recap pace)
            progress_cb: Callback for status updates
            retries: Number of retry attempts on failure
        Returns:
            Path to the output WAV file
        """
        out_path = Path(output_wav).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)

        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("Fish Audio input text is empty.")

        # Cache check for non-custom-reference generations
        cache_key = None
        cached_file = None
        if not reference_wav:
            cache_key = hashlib.sha256(
                f"{clean_text}_{self.model}_{reference_id}_{speed:.2f}".encode("utf-8")
            ).hexdigest()[:16]
            cached_file = self.work_dir / f"fish_{cache_key}.wav"
            if cached_file.is_file() and cached_file.stat().st_size > 4000:
                shutil.copy2(cached_file, out_path)
                self._log(f"♻️ Fish Audio Cache Hit: {out_path.name}")
                return out_path

        import requests

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "model": self.model,
        }

        # Check if reference audio exists for inline zero-shot cloning
        ref_bytes = None
        if reference_wav:
            ref_p = Path(reference_wav)
            if ref_p.is_file() and ref_p.stat().st_size > 100:
                try:
                    ref_bytes = ref_p.read_bytes()
                    self._log(f"🎵 Fish Audio Reference Audio loaded: {ref_p.name} ({len(ref_bytes)} bytes)")
                except Exception as e_ref:
                    logger.warning("Could not read reference_wav (%s): %s", reference_wav, e_ref)

        use_msgpack = bool(ref_bytes)
        try:
            import msgpack
        except ImportError:
            if use_msgpack:
                logger.warning("msgpack module not found; inline audio cloning cannot serialize binary. Falling back to JSON.")
                use_msgpack = False

        if use_msgpack:
            headers["Content-Type"] = "application/msgpack"
            payload: dict[str, Any] = {
                "text": clean_text,
                "references": [
                    {
                        "audio": ref_bytes,
                        "text": str(prompt_text or "")
                    }
                ],
                "format": "wav",
                "normalize": True,
                "prosody": {
                    "speed": float(speed or 1.05),
                    "volume": 0.0,
                    "normalize_loudness": True,
                },
            }
            body_data = msgpack.packb(payload)
        else:
            headers["Content-Type"] = "application/json"
            json_payload: dict[str, Any] = {
                "text": clean_text,
                "format": "wav",
                "normalize": True,
                "prosody": {
                    "speed": float(speed or 1.05),
                    "volume": 0.0,
                    "normalize_loudness": True,
                },
            }
            if reference_id:
                json_payload["reference_id"] = str(reference_id)
            body_data = None

        last_err: Exception | None = None
        for attempt in range(1, retries + 2):
            try:
                self._log(
                    f"🐟 [Fish Audio API] Attempt {attempt}/{retries+1} | "
                    f"model={self.model} | chars={len(clean_text)} | ref={'audio' if ref_bytes else (reference_id or 'none')}"
                )
                if progress_cb:
                    try:
                        progress_cb(f"🎙️ Fish Audio AI ဖြင့် အသံဖန်တီးနေပါသည် ({attempt}/{retries+1})...")
                    except Exception:
                        pass

                # Acquire concurrency semaphore
                with _FISH_AUDIO_SEMAPHORE:
                    t0 = time.time()
                    if use_msgpack:
                        resp = requests.post(
                            self.endpoint,
                            data=body_data,
                            headers=headers,
                            timeout=60,
                        )
                    else:
                        resp = requests.post(
                            self.endpoint,
                            json=json_payload,
                            headers=headers,
                            timeout=60,
                        )
                    elapsed = time.time() - t0

                # Log rate limits & trace info from headers
                limit_concur = resp.headers.get("ratelimit-limit-concurrency", "N/A")
                curr_concur = resp.headers.get("ratelimit-current-concurrency", "N/A")
                trace_id = resp.headers.get("x-fish-trace-id", "")
                dc = resp.headers.get("x-fishaudio-datacenter", "")
                self._log(
                    f"📊 Fish Audio Response: {resp.status_code} in {elapsed:.2f}s | "
                    f"concurrency={curr_concur}/{limit_concur} | dc={dc} | trace={trace_id[:12]}"
                )

                if resp.status_code == 200 and len(resp.content) > 1000:
                    raw_audio_temp = out_path.with_suffix(".raw.wav")
                    raw_audio_temp.write_bytes(resp.content)

                    # Post-process with broadcast voice mastering via FFmpeg (44.1kHz mono PCM)
                    ffmpeg_bin = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
                    converted_cleanly = False
                    if ffmpeg_bin:
                        try:
                            import subprocess
                            af_master = (
                                "highpass=f=60,"
                                "lowpass=f=12000,"
                                "acompressor=threshold=-18dB:ratio=2.5:attack=15:release=120:makeup=2,"
                                "alimiter=limit=0.95:level=disabled"
                            )
                            cmd = [
                                ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                                "-i", str(raw_audio_temp),
                                "-af", af_master,
                                "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le",
                                str(out_path)
                            ]
                            subprocess.run(cmd, check=True, capture_output=True, timeout=30)
                            if out_path.is_file() and out_path.stat().st_size > 1000:
                                converted_cleanly = True
                        except Exception as e_ffmpeg:
                            logger.debug("FFmpeg mastering fallback: %s", e_ffmpeg)

                    if not converted_cleanly:
                        shutil.copy2(raw_audio_temp, out_path)

                    try:
                        raw_audio_temp.unlink(missing_ok=True)
                    except Exception:
                        pass

                    # Cache result for non-custom reference generations
                    if cached_file and not ref_bytes:
                        try:
                            shutil.copy2(out_path, cached_file)
                        except Exception:
                            pass

                    self._log(f"✅ Fish Audio Success: {out_path.name} ({out_path.stat().st_size} bytes)")
                    return out_path
                else:
                    err_msg = f"HTTP {resp.status_code}: {resp.text[:300]}"
                    raise RuntimeError(f"Fish Audio API returned error: {err_msg}")

            except Exception as e:
                last_err = e
                self._log(f"⚠️ Fish Audio attempt {attempt} failed: {e}")
                if attempt <= retries:
                    # Exponential backoff for rate limits or server saturation
                    sleep_sec = 2.0 * attempt
                    self._log(f"⏳ Backing off for {sleep_sec:.1f}s before retry...")
                    time.sleep(sleep_sec)

        # Resilient fallback to Microsoft Edge Neural TTS
        self._log(f"⚠️ Fish Audio API busy or failed ({last_err}). Invoking High-Fidelity Neural Engine fallback...")
        if progress_cb:
            try:
                progress_cb("🔄 Fish Audio ချိတ်ဆက်မှု အချိန်ကုန်နေသဖြင့် အရန် Neural Audio ဖြင့် ဖန်တီးပေးနေပါသည်...")
            except Exception:
                pass

        is_fem = any(w in str(control_instruction).lower() for w in ["female", "woman", "girl", "hnin", "nadi", "may", "နှင်း", "နဒီ", "မေမေ", "nilar"])
        if _synthesize_neural_tts_fallback(clean_text, out_path, control_instruction, is_female=is_fem):
            self._log(f"✅ Fallback Neural Audio successfully delivered: {out_path.name} ({out_path.stat().st_size} bytes)")
            return out_path

        raise RuntimeError(
            f"Fish Audio speech synthesis failed after {retries + 1} attempts: {last_err}"
        )

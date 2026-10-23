"""
voxcpm_engine.py - Zero-shot Multilingual Neural TTS & Voice Cloning via VoxCPM 2.
Connects seamlessly to OpenBMB's VoxCPM-Demo on Hugging Face Spaces for human-like,
tokenizer-free multilingual speech synthesis supporting Burmese, English, and 30 languages.

Features:
  - Multi-Space Pool & Failover (VOXCPM_SPACES, VOXCPM_SPACE_URL)
  - Hugging Face Token Support (HF_TOKEN) for elevated rate limits & priority
  - Live Queue ETA monitoring & dynamic timeout handling
  - Curated Voice Persona guidance (Voice Design via natural language control prompt)
  - Broadcast-grade audio mastering via FFmpeg (48kHz/44.1kHz PCM 16-bit mono WAV)
  - Resilient Microsoft Edge Neural TTS fallback for zero-downtime reliability
"""

from __future__ import annotations

import os
import re
import sys
import time
import shutil
import hashlib
import logging
from pathlib import Path
from typing import Optional, Any, Callable, List

logger = logging.getLogger(__name__)

DEFAULT_VOXCPM_SPACE = (
    os.getenv("VOXCPM_SPACE_URL", "").strip()
    or os.getenv("VOXCPM_API_URL", "").strip()
    or "openbmb/VoxCPM-Demo"
)
DEFAULT_BURMESE_CONTROL = (
    "A clear, natural, emotionally expressive Burmese speaker with warm tone "
    "and fluent pronunciation. Speak in Myanmar language with authentic accent."
)


def _resolve_result_path(result: Any) -> Optional[str]:
    """Safely extract local file path from gradio_client result."""
    if result is None:
        return None
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        return result.get("path") or result.get("url")
    if isinstance(result, (list, tuple)) and len(result) > 0:
        first = result[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return first.get("path") or first.get("url")
    path_attr = getattr(result, "path", None)
    if path_attr:
        return str(path_attr)
    return None


def get_available_hf_tokens() -> List[str]:
    """Discover configured Hugging Face tokens for increased rate limits and priority queue."""
    tokens: List[str] = []
    for env_var in ("HF_TOKEN", "HUGGINGFACE_TOKEN", "HF_TOKENS"):
        val = os.getenv(env_var, "").strip()
        if val:
            for part in re.split(r"[,;\s\n]+", val):
                tok = part.strip().strip("'\"")
                if tok and tok.startswith("hf_") and tok not in tokens:
                    tokens.append(tok)
    return tokens


def get_configured_spaces() -> List[str]:
    """Get list of candidate Hugging Face spaces for load balancing and redundancy."""
    spaces: List[str] = []
    custom_spaces = os.getenv("VOXCPM_SPACES", "").strip()
    if custom_spaces:
        for part in re.split(r"[,;\s\n]+", custom_spaces):
            sp = part.strip().strip("'\"")
            if sp and sp not in spaces:
                spaces.append(sp)

    single_url = os.getenv("VOXCPM_SPACE_URL", "").strip() or os.getenv("VOXCPM_API_URL", "").strip()
    if single_url and single_url not in spaces:
        spaces.insert(0, single_url)

    if DEFAULT_VOXCPM_SPACE not in spaces:
        spaces.append(DEFAULT_VOXCPM_SPACE)

    return spaces


class VoxCPMEngine:
    """
    High-level client for VoxCPM 2 speech synthesis and voice cloning.
    Connects to Hugging Face Spaces with Multi-Space redundancy, HF token prioritization,
    real-time Queue ETA tracking, and studio broadcast audio mastering.
    """
    _clients: dict[str, Any] = {}
    _last_init_times: dict[str, float] = {}

    def __init__(
        self,
        space_name: Optional[str] = None,
        work_dir: Optional[Path | str] = None,
        log_fn: Optional[Callable[[str], None]] = None,
    ):
        self.spaces = get_configured_spaces()
        if space_name and space_name not in self.spaces:
            self.spaces.insert(0, space_name)
        self.primary_space = self.spaces[0]
        self.hf_tokens = get_available_hf_tokens()
        self.work_dir = Path(work_dir) if work_dir else Path(os.environ.get("TEMP", ".")) / "voxcpm_cache"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.log_fn = log_fn

    def _log(self, msg: str) -> None:
        logger.info(msg)
        if self.log_fn:
            try:
                self.log_fn(msg)
            except Exception:
                pass

    @classmethod
    def get_client(cls, space_name: str, hf_token: Optional[str] = None):
        """Get or initialize cached Gradio client for a specific space with optional HF token."""
        now = time.time()
        last_time = cls._last_init_times.get(space_name, 0.0)
        client = cls._clients.get(space_name)

        if client is None or (now - last_time > 1800):
            try:
                from gradio_client import Client
                token = hf_token or (get_available_hf_tokens()[0] if get_available_hf_tokens() else None)
                tok_info = f" with HF_TOKEN ({token[:8]}...)" if token else " (anonymous)"
                logger.info("Connecting to VoxCPM 2 Space: %s%s...", space_name, tok_info)
                client = Client(space_name, token=token)
                cls._clients[space_name] = client
                cls._last_init_times[space_name] = now
            except Exception as e:
                logger.error("Failed to connect to VoxCPM 2 Space %s: %s", space_name, e)
                cls._clients.pop(space_name, None)
                raise
        return client

    def synthesize(
        self,
        text: str,
        output_wav: Path | str,
        control_instruction: str = DEFAULT_BURMESE_CONTROL,
        reference_wav: Optional[Path | str] = None,
        prompt_text: str = "",
        cfg_value: float = 2.0,
        preset_voice: Optional[str] = None,
        speed: float | str | None = "normal",
        progress_cb: Optional[Callable[[str], None]] = None,
        retries: int = 1,
        **kwargs: Any,
    ) -> Path:
        """
        Synthesize speech using VoxCPM 2.
        Supports zero-shot voice cloning, voice design, live queue ETA tracking,
        and broadcast audio mastering.
        """
        out_path = Path(output_wav).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)

        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("VoxCPM input text is empty.")

        # Check local cache for identical non-reference text
        cache_key = None
        cached_file = None
        if not reference_wav:
            cache_key = hashlib.sha256(
                f"{clean_text}_{control_instruction}_{prompt_text}_{cfg_value:.2f}_{preset_voice}".encode("utf-8")
            ).hexdigest()[:16]
            cached_file = self.work_dir / f"voxcpm_{cache_key}.wav"
            if cached_file.is_file() and cached_file.stat().st_size > 4000:
                shutil.copy2(cached_file, out_path)
                self._log(f"♻️ VoxCPM 2 Cache Hit: {out_path.name}")
                return out_path

        from gradio_client import handle_file

        # Prepare reference audio for zero-shot cloning if provided
        ref_file = None
        if reference_wav:
            ref_path = Path(reference_wav)
            if ref_path.is_file():
                ref_file = handle_file(str(ref_path.resolve()))
                self._log(f"🎵 VoxCPM 2 Reference Audio loaded: {ref_path.name} ({ref_path.stat().st_size} bytes)")
            else:
                logger.warning("VoxCPM reference_wav not found: %s — proceeding with Voice Design", reference_wav)

        # Persona-based control instruction tuning
        if preset_voice:
            from app.services.curated_voices import get_curated_voice
            p_info = get_curated_voice(preset_voice)
            if p_info:
                control_instruction = p_info.get("control_instruction") or control_instruction
                cfg_value = p_info.get("cfg_value") or cfg_value
                self._log(f"🎭 Applied Curated Persona [{preset_voice}] for VoxCPM 2 Voice Design")

        # Try candidate spaces in pool
        last_err: Exception | None = None
        candidate_spaces = self.spaces.copy()

        for attempt in range(1, retries + 2):
            for space_name in candidate_spaces:
                try:
                    self._log(
                        f"🌐 [VoxCPM 2] Attempt {attempt}/{retries+1} on Space '{space_name}' "
                        f"| chars={len(clean_text)} | ref={'yes' if ref_file else 'no'}"
                    )
                    if progress_cb:
                        try:
                            progress_cb(f"🎙️ VoxCPM 2 AI ဖြင့် အသံဖန်တီးနေပါသည် ({space_name})...")
                        except Exception:
                            pass

                    client = self.get_client(space_name)

                    job = client.submit(
                        text_input=clean_text,
                        control_instruction=control_instruction or DEFAULT_BURMESE_CONTROL,
                        reference_wav_path_input=ref_file,
                        use_prompt_text=bool(prompt_text),
                        prompt_text_input=prompt_text or "",
                        cfg_value_input=float(cfg_value or 2.0),
                        do_normalize=True,
                        denoise=False,
                        api_name="/generate",
                    )

                    # Poll status with queue tracking
                    poll_start = time.time()
                    while not job.done():
                        # Fail over if queue exceeds 30 seconds
                        if time.time() - poll_start > 30:
                            raise TimeoutError(f"VoxCPM 2 Space '{space_name}' queue timed out after 30s")

                        try:
                            st = job.status()
                            eta_val = getattr(st, "eta", None)
                            pos_val = getattr(st, "queue_position", None) or getattr(st, "position", None)
                            if eta_val is not None and float(eta_val) > 0:
                                eta_sec = int(round(float(eta_val)))
                                pos_str = f" (အဆင့်: {pos_val})" if pos_val is not None else ""
                                msg = f"⏳ VoxCPM 2 Queue စောင့်ဆိုင်းနေပါသည်{pos_str} — ခန့်မှန်းကြာချိန် ~{eta_sec}s"
                                self._log(msg)
                                if progress_cb:
                                    try:
                                        progress_cb(msg)
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                        time.sleep(2)

                    raw_result = job.result()
                    result_path_str = _resolve_result_path(raw_result)

                    if result_path_str and os.path.isfile(result_path_str) and os.path.getsize(result_path_str) > 1000:
                        # Studio Broadcast Audio Mastering via FFmpeg (44.1kHz mono PCM 16-bit WAV)
                        converted_cleanly = False
                        ffmpeg_bin = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
                        if ffmpeg_bin:
                            try:
                                import subprocess
                                af_master = (
                                    "highpass=f=65,"
                                    "lowpass=f=12000,"
                                    "acompressor=threshold=-16dB:ratio=2.5:attack=15:release=120:makeup=2,"
                                    "alimiter=limit=0.95:level=disabled"
                                )
                                cmd = [
                                    ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                                    "-i", str(result_path_str),
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
                            shutil.copy2(result_path_str, out_path)

                        if cached_file and not ref_file:
                            try:
                                shutil.copy2(out_path, cached_file)
                            except Exception:
                                pass

                        self._log(f"✅ VoxCPM 2 Success: {out_path.name} ({out_path.stat().st_size} bytes)")
                        return out_path
                    else:
                        raise RuntimeError(f"VoxCPM 2 returned empty result from space '{space_name}'")

                except Exception as e_sp:
                    last_err = e_sp
                    self._log(f"⚠️ VoxCPM 2 Space '{space_name}' attempt {attempt} error: {e_sp}")
                    self._clients.pop(space_name, None)
                    time.sleep(1.0)

        # Seamless Fallback to High-Fidelity Burmese Studio Persona Engine
        self._log(f"⚠️ VoxCPM 2 Spaces busy or queue full ({last_err}). Invoking High-Fidelity Burmese Neural Engine fallback...")
        if progress_cb:
            try:
                progress_cb("🔄 VoxCPM Queue အချိန်ကုန်နေသဖြင့် အရန် Studio Neural Audio ဖြင့် ဖန်တီးပေးနေပါသည်...")
            except Exception:
                pass

        try:
            from app.services.curated_voices import synthesize_curated_persona, get_curated_voice
            pid = preset_voice or "komin"
            if not get_curated_voice(pid):
                is_fem = any(w in str(control_instruction).lower() for w in ["female", "woman", "girl", "hnin", "nadi", "may", "နှင်း", "နဒီ", "မေမေ"])
                pid = "mahnin" if is_fem else "komin"

            synthesize_curated_persona(clean_text, pid, out_path, speed=speed, log_fn=self.log_fn)
            if out_path.is_file() and out_path.stat().st_size > 1000:
                self._log(f"✅ Fallback Studio Audio successfully delivered: {out_path.name} ({out_path.stat().st_size} bytes)")
                return out_path
        except Exception as e_fb:
            logger.warning("Neural fallback error: %s", e_fb)

        raise RuntimeError(
            f"VoxCPM 2 speech synthesis failed after retrying all spaces: {last_err}"
        )


__all__ = ["VoxCPMEngine", "DEFAULT_VOXCPM_SPACE", "DEFAULT_BURMESE_CONTROL"]

"""
voxcpm_engine.py - Zero-shot Multilingual Neural TTS & Voice Cloning via VoxCPM 2.
Connects seamlessly to OpenBMB's VoxCPM-Demo on Hugging Face Spaces for human-like,
tokenizer-free multilingual speech synthesis supporting Burmese, English, and 30 languages.

Features:
  - Multi-Token Pool & Round-Robin Rotation (HF_TOKEN, HF_TOKENS)
  - Multi-Space Pool & Failover (VOXCPM_SPACES, VOXCPM_SPACE_URL)
  - Live Queue ETA monitoring & dynamic timeout handling (30s)
  - Curated Voice Persona guidance (Voice Design via natural language control prompt)
  - Broadcast-grade audio mastering via FFmpeg (44.1kHz PCM 16-bit mono WAV)
  - Resilient Microsoft Edge Neural TTS fallback for zero-downtime reliability
"""

from __future__ import annotations

import os
import re
import sys
import time
import shutil
import base64
import hashlib
import logging
import threading
from pathlib import Path
from typing import Optional, Any, Callable, List, Tuple

logger = logging.getLogger(__name__)

DEFAULT_VOXCPM_SPACES = [
    "openbmb/VoxCPM-Demo",
    "yeyee2609/voxcpm2",
    "yeyee2609/voxcpm",
    "soemintheinsmt6/myanmar-voxcpm2",
]
DEFAULT_VOXCPM_SPACE = (
    os.getenv("VOXCPM_SPACE_URL", "").strip()
    or os.getenv("VOXCPM_API_URL", "").strip()
    or DEFAULT_VOXCPM_SPACES[0]
)
DEFAULT_BURMESE_CONTROL = (
    "A clear, natural, emotionally expressive Burmese speaker with warm tone, "
    "smooth studio acoustic, velvety cadence, and pristine pronunciation without harsh sibilance or background noise. "
    "Speak in Myanmar language with authentic accent."
)


def mask_hf_token(tok: Optional[str]) -> str:
    """Safely format HF token for logging."""
    if not tok:
        return "anonymous"
    t = tok.strip()
    if len(t) > 10:
        return f"{t[:7]}...{t[-4:]}"
    return "hf_***"


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
    # 1. Environment variables
    for env_var in ("HF_TOKEN", "HUGGINGFACE_TOKEN", "HF_TOKENS"):
        val = os.getenv(env_var, "").strip()
        if val:
            for part in re.split(r"[,;\s\n]+", val):
                tok = part.strip().strip("'\"")
                if tok and tok.startswith("hf_") and tok not in tokens:
                    tokens.append(tok)

    # 2. Config settings (.env via pydantic)
    try:
        from app.config import settings
        for val in (getattr(settings, "hf_token", None), getattr(settings, "hf_tokens", None)):
            if val:
                for part in re.split(r"[,;\s\n]+", str(val)):
                    tok = part.strip().strip("'\"")
                    if tok and tok.startswith("hf_") and tok not in tokens:
                        tokens.append(tok)
    except Exception:
        pass

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

    for sp in DEFAULT_VOXCPM_SPACES:
        if sp not in spaces:
            spaces.append(sp)

    return spaces


class VoxCPMEngine:
    """
    High-level client for VoxCPM 2 speech synthesis and voice cloning.
    Connects to Hugging Face Spaces with Multi-Token Pool & Rotation, Multi-Space redundancy,
    real-time Queue ETA tracking, and studio broadcast audio mastering.
    """
    _clients: dict[str, Any] = {}
    _last_init_times: dict[str, float] = {}
    _token_cooldowns: dict[str, float] = {}
    _token_rr_counter: int = 0
    _lock = threading.Lock()

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
    def get_client(cls, space_name: str, hf_token: Optional[str] = None) -> Any:
        """Get or initialize cached Gradio client for a specific space and HF token."""
        now = time.time()
        cache_key = f"{space_name}::{hf_token or 'anon'}"

        last_time = cls._last_init_times.get(cache_key, 0.0)
        client = cls._clients.get(cache_key)

        if client is None or (now - last_time > 1800):
            try:
                from gradio_client import Client
                tok_info = f" with HF_TOKEN ({mask_hf_token(hf_token)})" if hf_token else " (anonymous)"
                logger.info("Connecting to VoxCPM 2 Space: %s%s...", space_name, tok_info)
                client = Client(space_name, token=hf_token)
                cls._clients[cache_key] = client
                cls._last_init_times[cache_key] = now
            except Exception as e:
                logger.error("Failed to connect to VoxCPM 2 Space %s: %s", space_name, e)
                cls._clients.pop(cache_key, None)
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
        retries: int = 2,
        allow_fallback: bool = True,
        **kwargs: Any,
    ) -> Path:
        """
        Synthesize speech using VoxCPM 2 with Multi-Token Rotation & Multi-Space redundancy.
        Supports zero-shot voice cloning, voice design, live queue ETA tracking,
        and broadcast audio mastering.

        allow_fallback=False raises instead of silently returning Edge-TTS persona audio, so a
        caller that needs ONE uniform voice for a whole video (Recap) can decide what to do.
        """
        out_path = Path(output_wav).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)

        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("VoxCPM input text is empty.")

        # Resolve brisk narration tempo multiplier from speed & persona
        tempo_val = 1.12
        if isinstance(speed, (int, float)):
            tempo_val = float(speed)
        elif isinstance(speed, str):
            s_low = speed.strip().lower()
            if s_low in ("fast", "1.2x", "1.2"):
                tempo_val = 1.20
            elif s_low in ("slow", "0.8x", "0.8"):
                tempo_val = 0.94
            elif s_low in ("normal", "1.0x", "1.0"):
                tempo_val = 1.12
            elif "%" in s_low:
                m_spd = re.search(r"([+-]?\d+)", s_low)
                if m_spd:
                    tempo_val = 1.0 + int(m_spd.group(1)) / 100.0
        tempo_val = max(0.88, min(1.45, round(tempo_val, 3)))

        # Persona-based control instruction & reference sample tuning
        is_persona_ref = False
        if preset_voice:
            from app.services.curated_voices import get_curated_voice, get_persona_reference
            p_info = get_curated_voice(preset_voice)
            if p_info:
                control_instruction = p_info.get("control_instruction") or control_instruction
                cfg_value = p_info.get("cfg_value") or cfg_value
                if not reference_wav:
                    p_ref_path, p_ref_text = get_persona_reference(preset_voice)
                    if p_ref_path:
                        reference_wav = p_ref_path
                        prompt_text = prompt_text or p_ref_text
                        is_persona_ref = True
                self._log(f"🎭 Applied Curated Persona [{preset_voice}] for VoxCPM 2 (tempo={tempo_val:.2f}x)")

        # Check local cache for identical text + voice configuration
        cache_key = None
        cached_file = None
        if not reference_wav or is_persona_ref:
            cache_key = hashlib.sha256(
                f"{clean_text}_{control_instruction}_{prompt_text}_{cfg_value:.2f}_{preset_voice}_{tempo_val:.3f}".encode("utf-8")
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

        # Build candidate (space, token) pairs with health-aware sorting
        now = time.time()
        raw_tokens: List[Optional[str]] = list(self.hf_tokens) if self.hf_tokens else [None]
        if None not in raw_tokens:
            raw_tokens.append(None)  # include anonymous per-IP ZeroGPU quota as backup
        # Healthy authenticated tokens first, then anonymous, then cooldown tokens
        def _token_sort_key(t: Optional[str]) -> int:
            if t is None:
                return 1
            return 2 if self._token_cooldowns.get(t, 0) > now else 0

        tokens_pool = sorted(raw_tokens, key=_token_sort_key)
        candidate_spaces = self.spaces.copy()
        pairs: List[Tuple[str, Optional[str]]] = []
        for sp in candidate_spaces:
            for tok in tokens_pool:
                pairs.append((sp, tok))

        # Rotate healthy tokens across requests while keeping primary space priority
        healthy_auth = [t for t in tokens_pool if t is not None and self._token_cooldowns.get(t, 0) <= now]
        if len(healthy_auth) > 1:
            with self._lock:
                shift = self._token_rr_counter % len(healthy_auth)
                self._token_rr_counter += 1
            rotated_auth = healthy_auth[shift:] + healthy_auth[:shift]
            other_toks = [t for t in tokens_pool if t not in healthy_auth]
            ordered_toks = rotated_auth + other_toks
            pairs = [(sp, tok) for sp in candidate_spaces for tok in ordered_toks]

        last_err: Exception | None = None
        max_attempts = len(pairs)

        for attempt, (space_name, active_token) in enumerate(pairs, 1):
            tok_masked = mask_hf_token(active_token)
            try:
                self._log(
                    f"🌐 [VoxCPM 2] Attempt {attempt}/{max_attempts} on Space '{space_name}' "
                    f"| Token: {tok_masked} | Pool: {len(tokens_pool)} tokens | chars={len(clean_text)}"
                )
                if progress_cb:
                    try:
                        progress_cb(f"🎙️ VoxCPM 2 AI ဖြင့် အသံဖန်တီးနေပါသည် (Key: {tok_masked})...")
                    except Exception:
                        pass

                client = self.get_client(space_name, hf_token=active_token)
                effective_control = control_instruction or DEFAULT_BURMESE_CONTROL
                if ref_file and prompt_text:
                    # Locked-timbre cloning: the reference clip alone defines the voice.
                    effective_control = ""

                if "soemintheinsmt6" in space_name:
                    if ref_file and not is_persona_ref:
                        self._log(f"ℹ️ Space '{space_name}' specializes in voice design without custom reference file; skipping to next space...")
                        continue
                    job = client.submit(
                        text=clean_text,
                        voice_description=control_instruction or DEFAULT_BURMESE_CONTROL,
                        api_name="/tts",
                    )
                elif "yeyee2609" in space_name:
                    if ref_file:
                        if not prompt_text:
                            self._log(f"ℹ️ Space '{space_name}' cloning needs the reference transcript; skipping to next space...")
                            continue
                        job = client.submit(
                            ref_audio_path=ref_file,
                            ref_text=prompt_text,
                            target_text=clean_text,
                            api_name="/generate_clone",
                        )
                    else:
                        PRESET_MAP_YEYEE = {
                            "mahnin": "👧 Channel 99 (True Crime မိန်းကလေးအသံ)",
                            "manadi": "👧 Channel 99 (True Crime မိန်းကလေးအသံ)",
                            "machaw": "👧 Channel 99 (True Crime မိန်းကလေးအသံ)",
                            "mayoon": "👧 Channel 99 (True Crime မိန်းကလေးအသံ)",
                            "mamay": "👩 သတင်းကြေညာသူ (Professional)",
                            "mathiri": "👩 သတင်းကြေညာသူ (Professional)",
                            "komin": "👨 Knowledge Zone (ဗဟုသုတ ယောက်ျားလေးအသံ)",
                            "kothura": "👨 Knowledge Zone (ဗဟုသုတ ယောက်ျားလေးအသံ)",
                            "koaung": "👨 Knowledge Zone (ဗဟုသုတ ယောက်ျားလေးအသံ)",
                            "kophone": "👨 Knowledge Zone (ဗဟုသုတ ယောက်ျားလေးအသံ)",
                            "kozayar": "👻 သရဲဇာတ်လမ်း (အသံသြသြ)",
                            "komyat": "👻 သရဲဇာတ်လမ်း (အသံသြသြ)",
                        }
                        chosen_preset = PRESET_MAP_YEYEE.get(preset_voice) if preset_voice else None
                        if chosen_preset:
                            job = client.submit(
                                voice_name=chosen_preset,
                                target_text=clean_text,
                                api_name="/generate_preset",
                            )
                        else:
                            job = client.submit(
                                style=effective_control,
                                target_text=clean_text,
                                api_name="/generate_style",
                            )
                elif "chenchenaoyang" in space_name:
                    job = client.submit(
                        text=clean_text,
                        control_instruction=effective_control,
                        ref_wav=ref_file,
                        use_prompt_text=bool(prompt_text),
                        prompt_text_value=prompt_text or "",
                        cfg_value=float(cfg_value or 2.0),
                        do_normalize=True,
                        denoise=True,
                        dit_steps=10.0,
                        api_name="/generate",
                    )
                else:
                    # openbmb/VoxCPM-Demo and standard Gradio spaces
                    job = client.submit(
                        text_input=clean_text,
                        control_instruction=effective_control,
                        reference_wav_path_input=ref_file,
                        use_prompt_text=bool(prompt_text),
                        prompt_text_input=prompt_text or "",
                        cfg_value_input=float(cfg_value or 2.0),
                        do_normalize=True,
                        denoise=True,
                        api_name="/generate",
                    )

                # Poll status with queue tracking
                poll_start = time.time()
                timeout_sec = int(kwargs.get("timeout") or os.getenv("VOXCPM_TIMEOUT", "60"))
                while not job.done():
                    # Fail over if queue exceeds timeout
                    if time.time() - poll_start > timeout_sec:
                        raise TimeoutError(f"VoxCPM 2 Space '{space_name}' queue timed out after {timeout_sec}s")

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
                    # Eliminates harsh sibilance ("စီး" / "ဆူးဆူးအသံ") and normalizes broadcast loudness.
                    converted_cleanly = False
                    ffmpeg_bin = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
                    if ffmpeg_bin:
                        import subprocess
                        atempo_prefix = f"atempo={tempo_val:.3f}," if abs(tempo_val - 1.0) > 0.01 else ""
                        af_master_primary = (
                            f"{atempo_prefix}"
                            "highpass=f=75,"
                            "afftdn=nr=10:nf=-50:tn=1,"
                            "deesser=i=0.5:m=0.5:f=0.5:s=o,"
                            "equalizer=f=7200:width_type=q:w=2:g=-3.5,"
                            "lowpass=f=9500,"
                            "loudnorm=I=-15:TP=-1.0:LRA=7"
                        )
                        af_master_fallback = (
                            f"{atempo_prefix}"
                            "highpass=f=75,"
                            "deesser=i=0.5:m=0.5:f=0.5:s=o,"
                            "equalizer=f=7200:width_type=q:w=2:g=-3.5,"
                            "lowpass=f=9500,"
                            "acompressor=threshold=-18dB:ratio=2.2:attack=20:release=120:makeup=1.2,"
                            "alimiter=limit=0.92"
                        )
                        for af_candidate in (af_master_primary, af_master_fallback):
                            try:
                                cmd = [
                                    ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                                    "-i", str(result_path_str),
                                    "-af", af_candidate,
                                    "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le",
                                    str(out_path)
                                ]
                                subprocess.run(cmd, check=True, capture_output=True, timeout=30)
                                if out_path.is_file() and out_path.stat().st_size > 1000:
                                    converted_cleanly = True
                                    break
                            except Exception as e_ffmpeg:
                                logger.debug("FFmpeg mastering attempt failed: %s", e_ffmpeg)

                    if not converted_cleanly:
                        shutil.copy2(result_path_str, out_path)

                    if cached_file and (not ref_file or is_persona_ref):
                        try:
                            shutil.copy2(out_path, cached_file)
                        except Exception:
                            pass

                    self._log(f"✅ VoxCPM 2 Success [{tok_masked}]: {out_path.name} ({out_path.stat().st_size} bytes)")
                    return out_path
                else:
                    raise RuntimeError(f"VoxCPM 2 returned empty result from space '{space_name}'")

            except Exception as e_pair:
                last_err = e_pair
                err_lower = str(e_pair).lower()
                if active_token and ("zerogpu" in err_lower or "quota" in err_lower or "runs limit" in err_lower):
                    # Only mark token globally on cooldown if it wasn't just a 180s-decorator rejection
                    if "180s requested" not in err_lower:
                        self._token_cooldowns[active_token] = time.time() + 1800
                    self._log(f"⚠️ Token {tok_masked} hit ZeroGPU quota on '{space_name}'; rotating...")
                self._log(f"⚠️ VoxCPM 2 Pair ({space_name}, {tok_masked}) failed: {e_pair}")
                time.sleep(0.5)

        # Seamless Fallback to High-Fidelity Burmese Studio Persona Engine
        if not allow_fallback:
            raise RuntimeError(
                f"VoxCPM 2 speech synthesis failed after retrying all tokens and spaces: {last_err}"
            )
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
            f"VoxCPM 2 speech synthesis failed after retrying all tokens and spaces: {last_err}"
        )


__all__ = ["VoxCPMEngine", "DEFAULT_VOXCPM_SPACE", "DEFAULT_BURMESE_CONTROL", "get_available_hf_tokens"]

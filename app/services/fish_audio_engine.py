"""
fish_audio_engine.py - Zero-shot Multilingual Neural TTS & Voice Cloning via Fish Audio API.
Includes Multi-Key Pool, Smart Load Balancing, Rate-Limit Failover, and Edge Neural TTS fallback.

API Reference (Fish Audio v1 TTS):
  endpoint: https://api.fish.audio/v1/tts
  model:    s2.1-pro-free
  auth:     Authorization: Bearer <FISH_AUDIO_API_KEY>
"""

from __future__ import annotations

import os
import re
import sys
import time
import shutil
import hashlib
import logging
import threading
from pathlib import Path
from typing import Optional, Any, Callable, List, Set

logger = logging.getLogger(__name__)

BUILTIN_FISH_AUDIO_KEY = "sk-fish-LWGEkWhc5XBzIKwC11vJXvjmreeUS-Jd-haRWvuQKUw"

DEFAULT_FISH_AUDIO_ENDPOINT = (
    os.getenv("FISH_AUDIO_ENDPOINT", "").strip()
    or "https://api.fish.audio/v1/tts"
)
DEFAULT_FISH_AUDIO_MODEL = (
    os.getenv("FISH_AUDIO_MODEL", "").strip()
    or "s2.1-pro-free"
)
DEFAULT_BURMESE_CONTROL = (
    "A clear, natural, emotionally expressive Burmese movie recap narrator with warm tone "
    "and fluent pronunciation. Speak in Myanmar language with authentic accent."
)

# Max concurrent requests per individual API key (Fish Audio free tier limit is 5)
_PER_KEY_CONCURRENCY = int(os.getenv("FISH_AUDIO_PER_KEY_CONCURRENCY", "3"))


def mask_api_key(key: str) -> str:
    """Format key safely for logging without leaking secrets."""
    k = (key or "").strip()
    if len(k) > 12:
        return f"{k[:7]}...{k[-4:]}"
    return "sk-***"


def discover_fish_audio_keys(explicit_key: Optional[str] = None) -> List[str]:
    """
    Discover all configured Fish Audio API keys from environment variables and built-in defaults.
    Combines keys from:
      1. Explicit key argument
      2. FISH_AUDIO_API_KEYS (comma/semicolon/newline separated)
      3. FISH_AUDIO_API_KEY (comma/semicolon/newline separated or single)
      4. BUILTIN_FISH_AUDIO_KEY
    Returns deduplicated list of valid keys.
    """
    keys: List[str] = []

    def _add_candidates(val: str) -> None:
        for part in re.split(r"[,;\s\n]+", str(val or "")):
            token = part.strip().strip("'\"")
            if token and token.startswith("sk-") and token not in keys:
                keys.append(token)

    if explicit_key:
        _add_candidates(explicit_key)

    _add_candidates(os.getenv("FISH_AUDIO_API_KEYS", ""))
    _add_candidates(os.getenv("FISH_AUDIO_API_KEY", ""))

    # Always ensure the built-in key is in the pool for zero-downtime reliability
    if BUILTIN_FISH_AUDIO_KEY not in keys:
        keys.append(BUILTIN_FISH_AUDIO_KEY)

    return keys


class FishKeySlot:
    """Represents a single API key with independent concurrency gate and rate-limit tracking."""

    def __init__(self, key: str, max_concurrency: int = _PER_KEY_CONCURRENCY):
        self.key = key
        self.masked = mask_api_key(key)
        self.max_concurrency = max(1, max_concurrency)
        self.semaphore = threading.BoundedSemaphore(self.max_concurrency)
        self.in_flight = 0
        self.cooldown_until = 0.0
        self.total_requests = 0
        self.total_successes = 0
        self.total_failures = 0
        self.last_concurrency_seen: Optional[int] = None
        self._lock = threading.Lock()

    @property
    def is_cooling_down(self) -> bool:
        with self._lock:
            return time.time() < self.cooldown_until

    def mark_cooldown(self, seconds: float = 45.0) -> None:
        with self._lock:
            self.cooldown_until = time.time() + seconds

    def acquire(self, timeout: float = 12.0) -> bool:
        acquired = self.semaphore.acquire(timeout=timeout)
        if acquired:
            with self._lock:
                self.in_flight += 1
                self.total_requests += 1
        return acquired

    def release(self) -> None:
        with self._lock:
            self.in_flight = max(0, self.in_flight - 1)
        try:
            self.semaphore.release()
        except ValueError:
            pass


class FishKeyPool:
    """
    Thread-safe dynamic load balancer across multiple Fish Audio API keys.
    Selects the least-busy, non-cooldown key slot and enables immediate failover
    upon HTTP 429 (Rate Limit) or network errors.
    """

    def __init__(self, keys: List[str], max_concurrency_per_key: int = _PER_KEY_CONCURRENCY):
        self.slots = [FishKeySlot(k, max_concurrency=max_concurrency_per_key) for k in keys]
        self._lock = threading.Lock()
        self._rr_counter = 0

    @property
    def total_capacity(self) -> int:
        return sum(s.max_concurrency for s in self.slots)

    def select_slot(self, exclude_keys: Optional[Set[str]] = None) -> Optional[FishKeySlot]:
        now = time.time()
        excluded = exclude_keys or set()

        with self._lock:
            # 1. Candidate slots that are not excluded and not currently in rate-limit cooldown
            available = [s for s in self.slots if s.key not in excluded and now >= s.cooldown_until]

            if not available:
                # If all available are on cooldown or excluded, pick from non-excluded with earliest cooldown
                non_excluded = [s for s in self.slots if s.key not in excluded]
                if non_excluded:
                    available = non_excluded
                else:
                    available = self.slots

            # 2. Pick the slot with the lowest in-flight load (Least-Busy Load Balancing)
            # Break ties using round-robin index for fair distribution
            self._rr_counter += 1
            available.sort(key=lambda s: (s.in_flight, s.cooldown_until))
            return available[0] if available else None


# Global singleton pool for unified worker & recap concurrency control
_GLOBAL_KEY_POOL: Optional[FishKeyPool] = None
_POOL_INIT_LOCK = threading.Lock()


def get_global_fish_key_pool(explicit_keys: Optional[List[str]] = None) -> FishKeyPool:
    global _GLOBAL_KEY_POOL
    with _POOL_INIT_LOCK:
        if _GLOBAL_KEY_POOL is None or explicit_keys:
            keys = explicit_keys or discover_fish_audio_keys()
            _GLOBAL_KEY_POOL = FishKeyPool(keys, max_concurrency_per_key=_PER_KEY_CONCURRENCY)
            logger.info(
                "🔑 Initialized Fish Audio Key Pool: %d keys | Total concurrency capacity: %d requests",
                len(_GLOBAL_KEY_POOL.slots),
                _GLOBAL_KEY_POOL.total_capacity,
            )
        return _GLOBAL_KEY_POOL


def _synthesize_neural_tts_fallback(
    text: str,
    output_wav: Path,
    control_instruction: str = "",
    persona_id: Optional[str] = None,
    is_female: bool = False,
    speed: float | str = "normal",
) -> bool:
    """Seamless fallback using Microsoft Edge Neural TTS with Curated Personas and broadcast mastering."""
    try:
        from app.services.curated_voices import synthesize_curated_persona, get_curated_voice
        target_pid = persona_id
        if not target_pid or not get_curated_voice(target_pid):
            female_keywords = ["female", "woman", "girl", "hnin", "nadi", "may", "နှင်း", "နဒီ", "မေမေ", "nilar", "machaw", "mathiri"]
            if not is_female and any(w in str(control_instruction).lower() for w in female_keywords):
                is_female = True
            target_pid = "mahnin" if is_female else "komin"

        synthesize_curated_persona(text, target_pid, output_wav, speed=speed)
        return output_wav.is_file() and output_wav.stat().st_size > 1000
    except Exception as e_fb:
        logger.warning("Neural TTS fallback error: %s", e_fb)
    return False


class FishAudioEngine:
    """
    High-level client for Fish Audio Text-to-Speech & Voice Cloning.
    Features:
      - Dynamic Multi-Key Pool with Least-Busy Load Balancing
      - Zero-shot Voice Cloning with MessagePack serialization
      - Per-key Concurrency Gate (max 3 concurrent requests per key, avoiding the 5 cap)
      - Automatic HTTP 429 Cooldown & Instant Failover to Alternative Keys
      - Broadcast audio mastering via FFmpeg
      - Automatic Microsoft Edge Neural TTS fallback
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        endpoint: Optional[str] = None,
        work_dir: Optional[Path | str] = None,
        log_fn: Optional[Callable[[str], None]] = None,
    ):
        self.keys = discover_fish_audio_keys(api_key)
        self.key_pool = get_global_fish_key_pool(self.keys)
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
        speed: float | str = 1.05,
        preset_voice: Optional[str] = None,
        progress_cb: Optional[Callable[[str], None]] = None,
        retries: int = 3,
        **kwargs: Any,
    ) -> Path:
        """
        Synthesize speech using Studio-Mastered Burmese Engine for Myanmar script,
        and Fish Audio Multi-Key Pool for English/foreign text.
        """
        out_path = Path(output_wav).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)

        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("Input text is empty.")

        # 🇲🇲 Burmese Script Detection:
        # Fish Audio S2.1 does NOT support Myanmar Unicode script (which causes token collision
        # and foreign language hallucinations like Vietnamese or Hmong).
        # We seamlessly route Burmese text to the studio-mastered Burmese Neural Engine!
        burmese_match = re.search(r"[\u1000-\u109f\uaa60-\uaa7f]", clean_text)
        if burmese_match:
            from app.services.curated_voices import synthesize_curated_persona, CURATED_VOICES, get_curated_voice
            target_pid = preset_voice or kwargs.get("preset_voice")
            if not target_pid:
                ctrl_lower = str(control_instruction or "").lower()
                for pid, pdata in CURATED_VOICES.items():
                    if pid in ctrl_lower or pdata["name_en"].lower() in ctrl_lower:
                        target_pid = pid
                        break
            if not target_pid or not get_curated_voice(target_pid):
                is_fem = any(w in str(control_instruction).lower() for w in ["female", "woman", "girl", "hnin", "nadi", "may", "နှင်း", "နဒီ", "မေမေ", "nilar", "machaw", "mathiri"])
                target_pid = "mahnin" if is_fem else "komin"

            self._log(f"🇲🇲 Burmese text detected. Routing to studio-mastered Burmese Neural Persona Engine [{target_pid}]...")
            if progress_cb:
                try:
                    p_info = get_curated_voice(target_pid)
                    p_name = p_info["name_my"] if p_info else target_pid
                    progress_cb(f"🎙️ သဘာဝကျသော မြန်မာအသံစနစ်ဖြင့် ဖန်တီးနေပါသည် ({p_name})...")
                except Exception:
                    pass

            return synthesize_curated_persona(
                text=clean_text,
                persona_id=target_pid,
                output_wav=out_path,
                speed=speed,
                log_fn=self.log_fn,
            )

        # Cache check for non-custom-reference generations (English/Foreign text)
        cache_key = None
        cached_file = None
        if not reference_wav:
            speed_str = f"{float(speed):.2f}" if isinstance(speed, (int, float)) else str(speed or "normal")
            cache_key = hashlib.sha256(
                f"{clean_text}_{self.model}_{reference_id}_{speed_str}".encode("utf-8")
            ).hexdigest()[:16]
            cached_file = self.work_dir / f"fish_{cache_key}.wav"
            if cached_file.is_file() and cached_file.stat().st_size > 4000:
                shutil.copy2(cached_file, out_path)
                self._log(f"♻️ Fish Audio Cache Hit: {out_path.name}")
                return out_path

        import requests

        try:
            numeric_speed = float(speed)
        except (ValueError, TypeError):
            numeric_speed = 1.15 if str(speed).lower() in ("fast", "1.2x") else (0.85 if str(speed).lower() in ("slow", "0.8x") else 1.05)

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
                logger.warning("msgpack module not found; falling back to JSON.")
                use_msgpack = False

        if use_msgpack:
            content_type = "application/msgpack"
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
                    "speed": float(numeric_speed),
                    "volume": 0.0,
                    "normalize_loudness": True,
                },
            }
            body_data = msgpack.packb(payload)
            json_payload = None
        else:
            content_type = "application/json"
            json_payload = {
                "text": clean_text,
                "format": "wav",
                "normalize": True,
                "prosody": {
                    "speed": float(numeric_speed),
                    "volume": 0.0,
                    "normalize_loudness": True,
                },
            }
            if reference_id:
                json_payload["reference_id"] = str(reference_id)
            body_data = None

        last_err: Exception | None = None
        tried_keys: Set[str] = set()

        max_attempts = max(retries + 1, len(self.key_pool.slots) + 1)
        for attempt in range(1, max_attempts + 1):
            slot = self.key_pool.select_slot(exclude_keys=tried_keys)
            if not slot:
                # Reset excluded keys if all were tried
                tried_keys.clear()
                slot = self.key_pool.select_slot()

            if not slot:
                break

            slot_acquired = slot.acquire(timeout=10.0)
            if not slot_acquired:
                tried_keys.add(slot.key)
                continue

            try:
                headers = {
                    "Authorization": f"Bearer {slot.key}",
                    "model": self.model,
                    "Content-Type": content_type,
                }

                self._log(
                    f"🐟 [Fish Audio API] Key {slot.masked} (in-flight: {slot.in_flight}/{slot.max_concurrency}) | "
                    f"Attempt {attempt}/{max_attempts} | chars={len(clean_text)} | pool={len(self.key_pool.slots)} keys"
                )
                if progress_cb:
                    try:
                        progress_cb(f"🎙️ Fish Audio AI ဖြင့် အသံဖန်တီးနေပါသည် (Key: {slot.masked})...")
                    except Exception:
                        pass

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

                # Read rate limits & trace info from headers
                limit_concur = resp.headers.get("ratelimit-limit-concurrency", "N/A")
                curr_concur = resp.headers.get("ratelimit-current-concurrency", "N/A")
                trace_id = resp.headers.get("x-fish-trace-id", "")
                dc = resp.headers.get("x-fishaudio-datacenter", "")
                slot.last_concurrency_seen = int(curr_concur) if str(curr_concur).isdigit() else None

                self._log(
                    f"📊 Fish Audio Response [{slot.masked}]: {resp.status_code} in {elapsed:.2f}s | "
                    f"concurrency={curr_concur}/{limit_concur} | dc={dc} | trace={trace_id[:12]}"
                )

                # HTTP 429: Rate limit hit - activate cooldown and failover immediately
                if resp.status_code == 429:
                    slot.mark_cooldown(seconds=45.0)
                    slot.total_failures += 1
                    tried_keys.add(slot.key)
                    self._log(
                        f"⚠️ Key {slot.masked} rate-limited (HTTP 429). Put on 45s cooldown. "
                        f"Failing over to alternative key in pool..."
                    )
                    continue

                if resp.status_code == 200 and len(resp.content) > 1000:
                    slot.total_successes += 1
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

                    self._log(f"✅ Fish Audio Success [{slot.masked}]: {out_path.name} ({out_path.stat().st_size} bytes)")
                    return out_path
                else:
                    slot.total_failures += 1
                    err_msg = f"HTTP {resp.status_code}: {resp.text[:250]}"
                    raise RuntimeError(f"Fish Audio API returned error on key {slot.masked}: {err_msg}")

            except Exception as e:
                last_err = e
                slot.total_failures += 1
                tried_keys.add(slot.key)
                self._log(f"⚠️ Key {slot.masked} attempt {attempt} error: {e}")
                if attempt < max_attempts:
                    time.sleep(1.0)
            finally:
                slot.release()

        # Resilient fallback to Microsoft Edge Neural TTS
        self._log(f"⚠️ All Fish Audio keys exhausted or busy ({last_err}). Invoking High-Fidelity Neural Engine fallback...")
        if progress_cb:
            try:
                progress_cb("🔄 Fish Audio ချိတ်ဆက်မှု အချိန်ကုန်နေသဖြင့် အရန် Neural Audio ဖြင့် ဖန်တီးပေးနေပါသည်...")
            except Exception:
                pass

        is_fem = any(w in str(control_instruction).lower() for w in ["female", "woman", "girl", "hnin", "nadi", "may", "နှင်း", "နဒီ", "မေမေ", "nilar", "machaw", "mathiri"])
        if _synthesize_neural_tts_fallback(clean_text, out_path, control_instruction, persona_id=preset_voice, is_female=is_fem, speed=speed):
            self._log(f"✅ Fallback Neural Audio successfully delivered: {out_path.name} ({out_path.stat().st_size} bytes)")
            return out_path

        raise RuntimeError(
            f"Fish Audio speech synthesis failed across all {len(self.key_pool.slots)} keys in pool: {last_err}"
        )

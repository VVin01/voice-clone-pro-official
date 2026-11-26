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

import json
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

from app.services.speed import DEFAULT_SPEED, resolve_speed_tempo

try:
    from app.services.key_rotation import key_id as _key_id
except Exception:  # pragma: no cover - standalone module load fallback

    def _key_id(key: str) -> str:
        return hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:12]


logger = logging.getLogger(__name__)

DEFAULT_VOXCPM_SPACES = [
    # Third-party VoxCPM2 spaces (yeyee2609/voxcpm2, soemintheinsmt6/myanmar-voxcpm2)
    # were removed on 2026-11-24 because their fixed 120 s declared GPU duration
    # creates a ~180 s admission gate that strands quota dust and produces avoidable
    # refusals; production spaces are supplied via the VOXCPM_SPACES env var.
    # CPU demo kept only as the explicit merged-list tail placeholder (excluded from
    # automated failover).
    "openbmb/VoxCPM-Demo",
]

# Prosody Phase 1 (1A-2 #3): v1 spaces must never enter a VoxCPM2 failover chain.
_V1_SPACE_NAMES = frozenset({"yeyee2609/voxcpm"})
# HF Pool Phase 1 (Part 4 #4): CPU Basic demo is excluded from automated failover.
_CPU_DEMO_SPACE_NAMES = frozenset({"openbmb/voxcpm-demo"})


def is_voxcpm_v1_space(space_name: str) -> bool:
    """True when ``space_name`` identifies a legacy VoxCPM v1 Space (e.g. yeyee2609/voxcpm)."""
    norm = str(space_name or "").strip().lower().rstrip("/")
    if not norm:
        return False
    if norm in _V1_SPACE_NAMES:
        return True
    # Catch full Hugging Face URLs pointing at yeyee2609/voxcpm (without matching voxcpm2)
    return bool(re.search(r"(?:^|/|-)yeyee2609[-/]voxcpm$", norm))


def is_cpu_demo_space(space_name: str) -> bool:
    """True when ``space_name`` identifies the CPU-basic official demo Space."""
    norm = str(space_name or "").strip().lower().rstrip("/")
    if not norm:
        return False
    if norm in _CPU_DEMO_SPACE_NAMES:
        return True
    return bool(re.search(r"(?:^|/|-)openbmb[-/]voxcpm-demo$", norm))


# Per-space capability map: third-party prompt-only spaces (soemintheinsmt6) were
# retired from rotation on 2026-11-24; marker tuple kept empty so no dangling
# special-case remains while preserving the public predicate contract.
_CLONE_INCAPABLE_SPACE_MARKERS: tuple[str, ...] = ()


def space_supports_clone(space_name: str) -> bool:
    """True if the space can take a reference-audio clone request."""
    name = str(space_name or "").lower()
    return not any(marker in name for marker in _CLONE_INCAPABLE_SPACE_MARKERS)


# Batch AT (AT-2): per-space attempt cap inside ONE unpinned synthesis call.
MAX_ATTEMPTS_PER_SPACE = 4
# HF Pool Phase 1 (Part 2 #3): bounded retry ladder for pinned VoxCPM2 spaces.
MAX_PINNED_SPACE_TOKENS = 6
MAX_REPIN_SPACE_TOKENS = 3
SEGMENT_BUDGET_FREE_SECONDS = 120.0
SEGMENT_BUDGET_PAID_SECONDS = 180.0

# Batch AT (AT-3): per-space Gradio endpoint mapping. Every space
# defaults to the openbmb-style "/generate" endpoint.
# yunaisbusy/voxcpm2-fast is a duplicate of the yeyee-family app: its
# app.py (read via the HF API 2026-11-05) exposes
# generate_preset(voice_name, target_text),
# generate_clone(ref_audio_path, ref_text, target_text) and
# generate_style(style, target_text) — there is NO api_name="generate",
# so every pre-AT call to it failed instantly with "Cannot find a
# function with `api_name`: /generate" (production recap job
# rc_1793880623_7150ea: 3 of every line's 6 pair attempts burned
# there). The engine's existing yeyee-family call shape —
# submit(ref_audio_path=…, ref_text=…, target_text=…) — matches
# generate_clone's signature exactly, so the fast space is routed
# through that same family with these endpoint names. The engine
# never calls a preset endpoint (preset voices resolve to a style
# prompt or a reference clip), so "preset" is recorded here for
# completeness only.
_CLONE_STYLE_ENDPOINTS = {
    "clone": "/generate_clone",
    "style": "/generate_style",
    "preset": "/generate_preset",
}
_DEFAULT_SPACE_ENDPOINTS = {
    "clone": "/generate",
    "style": "/generate",
    "preset": "/generate",
}
SPACE_API_ENDPOINTS = {
    "yunaisbusy/voxcpm2-fast": dict(_CLONE_STYLE_ENDPOINTS),
}


# Batch BU: VoxCPM Spaces reject a clone reference longer than 50 s
# ("Reference audio is too long. Please upload audio no longer than
# 50 seconds."). Pre-BU the intake never adapted the reference, so a
# user who uploaded a longer sample failed on EVERY admitting space
# and the job silently fell back to the Edge persona voice (production
# voice job ed0ff7fe, 2026-11-06: the user paid for 15 s of a voice
# that was not their clone). The engine now probes the reference once
# per synthesis and, when it exceeds the cap, clones from its first
# REFERENCE_TRIM_SECONDS instead — see _adapt_reference_for_clone.
REFERENCE_MAX_SECONDS = 50.0
REFERENCE_TRIM_SECONDS = 45.0


def _probe_reference_duration_seconds(path: Path) -> Optional[float]:
    """Float duration of a local audio file via ffprobe, or None.

    Soft-fail by design: any probe problem (no ffprobe, unreadable
    file, unparseable output) returns None and the caller proceeds
    with the original reference — the trim check must never become a
    new way for a working job to fail. The invocation mirrors
    app/services/audio.py (Batch AJ / L21 protocol whitelist: local
    files only). The codebase's other probes do not fit this call
    site: audio.get_wav_duration_seconds raises on failure (billing
    integrity, AJ/L4) and returns whole seconds, and
    downloader.probe_local_media would drag yt_dlp into this
    module's import graph.
    """
    try:
        import subprocess

        ffprobe_bin = shutil.which("ffprobe") or r"C:\ffmpeg\bin\ffprobe.exe"
        res = subprocess.run(
            [
                ffprobe_bin, "-protocol_whitelist", "file,pipe",
                "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            capture_output=True, text=True, timeout=15,
        )
        if res.returncode == 0 and res.stdout.strip():
            value = float(res.stdout.strip())
            return value if value > 0 else None
    except Exception:
        pass
    return None


def _trim_reference_audio(src: Path, dst: Path, seconds: float) -> bool:
    """Write the first ``seconds`` of ``src`` to ``dst`` as WAV.

    Returns True only when a non-empty output file exists. Any ffmpeg
    failure returns False and the caller falls back to the original
    reference (today's behaviour).
    """
    try:
        import subprocess

        ffmpeg_bin = shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
        res = subprocess.run(
            [
                ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                "-protocol_whitelist", "file,pipe",
                "-i", str(src), "-t", f"{seconds:.1f}",
                "-vn", "-c:a", "pcm_s16le", str(dst),
            ],
            capture_output=True, text=True, timeout=120,
        )
        return res.returncode == 0 and dst.is_file() and dst.stat().st_size > 0
    except Exception:
        return False


def space_api_endpoints(space_name: str) -> dict:
    """Gradio endpoint names for one space (Batch AT, AT-3).

    Defaults to "/generate" for every kind; the yeyee family and the
    spaces in SPACE_API_ENDPOINTS use the clone/style endpoint names
    their app.py actually exposes.
    """
    name = str(space_name or "")
    if name in SPACE_API_ENDPOINTS:
        return dict(SPACE_API_ENDPOINTS[name])
    if "yeyee2609" in name.lower():
        return dict(_CLONE_STYLE_ENDPOINTS)
    return dict(_DEFAULT_SPACE_ENDPOINTS)


def space_endpoint_family(space_name: str) -> str:
    """Return canonical endpoint/denoise family for Space-switch prosody telemetry (Phase 3 Part 8)."""
    endpoints = space_api_endpoints(space_name)
    if endpoints.get("clone") == "/generate_clone":
        return "generate_clone"
    if "chenchenaoyang" in str(space_name or "").lower():
        return "chenchenaoyang_generate"
    return "generate"


def _format_pair_error(exc: BaseException) -> str:
    """Batch AT (AT-4): pair-failure log detail — exception TYPE plus
    its message, truncated to 300 chars. Pre-AT the pair line carried
    only the exception's string form, which for the production
    failures was the bare class name with no message attached, making
    the failovers undiagnosable from logs alone (10 bare "ValueError"
    attempts in voice job a670e490)."""
    return f"{type(exc).__name__}: {exc}"[:300]


# yeyee2609 style prompts (fix 2026-10-29): previously every male
# persona resolved to the single "Knowledge Zone" preset and every
# female persona to the single "Channel 99" preset, so all voices came
# out identical whenever the preset path was used. Each persona now has
# its OWN style prompt, derived from that persona's existing
# control_instruction in app/services/curated_voices.py. These are
# prompts only — no new recorded voices are invented.
PRESET_MAP_YEYEE = {
    "komin": "A warm, deep, authoritative Burmese male movie recap storyteller, velvety cadence, emotionally resonant",
    "kothura": "A professional, formal, crisp Burmese male documentary narrator and news anchor, articulate diction",
    "koaung": "An enthusiastic, upbeat, vibrant young Burmese male creator for TikTok and Shorts, conversational rhythm",
    "kozayar": "A deep, bass-rich, resonant Burmese male blockbuster movie trailer narrator with cinematic gravitas",
    "kophone": "An articulate, engaging Burmese male tech and education explainer, clean friendly informative pacing",
    "komyat": "A warm, clear, natural Burmese male human storyteller, conversational narration, natural breathing",
    "mahnin": "A gentle, sweet, emotionally tender Burmese female audiobook narrator, soft melodious tone",
    "manadi": "A dramatic, suspenseful, emotionally intense Burmese female movie recap narrator, dynamic vocal contrast",
    "mamay": "A bright, articulate, friendly Burmese female commercial and guide voice, polite and trustworthy",
    "mayoon": "An ultra-soft, serene, tranquil Burmese female voice for meditation, ASMR and bedtime stories",
    "machaw": "A friendly, clear, natural Burmese female human creator and guide, warm articulate delivery",
    "mathiri": "An elegant, chic, sophisticated Burmese female luxury lifestyle influencer, silky smooth texture",
}
DEFAULT_VOXCPM_SPACE = (
    os.getenv("VOXCPM_SPACE_URL", "").strip()
    or os.getenv("VOXCPM_API_URL", "").strip()
)
DEFAULT_BURMESE_CONTROL = (
    "A clear, natural, emotionally expressive Burmese speaker with warm tone, "
    "smooth studio acoustic, velvety cadence, and pristine pronunciation without harsh sibilance or background noise. "
    "Speak in Myanmar language with authentic accent."
)
DEFAULT_CLONE_FIDELITY_CONTROL = (
    "Match the reference speaker's exact vocal timbre, pitch register, and breathing cadence "
    "with clear, natural Burmese pronunciation and steady studio clarity."
)


# Audit fix E (2026-11-03): markers of an HF/Gradio error that blames
# the TOKEN itself (revoked, invalid, or lacking access) rather than
# the space or the account's quota. Quota-class failures already cool a
# token down for 30 minutes; auth failures previously never did, so a
# dead token stayed in the healthy rotation and burned one failover
# attempt on every synthesis forever.
_HF_TOKEN_AUTH_MARKERS = (
    "unauthorized",
    "invalid token",
    "invalid_token",
    "token_invalid",
    "invalid credentials",
    "authentication failed",
    "not authorized",
    "forbidden",
)


def _is_hf_token_auth_failure(error_text: str) -> bool:
    """True when an error says the HF token itself was rejected (401/403)."""
    text = str(error_text or "").lower()
    if any(marker in text for marker in _HF_TOKEN_AUTH_MARKERS):
        return True
    return bool(re.search(r"\b(?:401|403)\b", text))


class VoxCPMQuotaExhaustedError(RuntimeError):
    """Every reachable (space, token) pair failed quota-class, or the
    job's quota memory has already ruled every pair out.

    Batch AL: callers (the recap per-segment retry) must NOT retry-walk
    on this error — a second walk inside the same job can only re-hit
    the same exhausted accounts. Plain RuntimeError failures (space
    down, bad response) keep the historical retry behaviour.
    """


TRANSIENT_RETRY_BACKOFF_SECONDS = 5.0


def is_voxcpm_capacity_or_congestion_error(exc: BaseException | None) -> bool:
    """True when `exc` represents ZeroGPU quota exhaustion or queue/budget timeout
    congestion (Part 2 #4: Paid -> park; Free -> fail-fast with `worker_error_quota_exhausted`),
    as opposed to deterministic 5xx / bad-input errors (both tiers -> FAILED + 100% refund)."""
    if exc is None:
        return False
    cur: BaseException | None = exc
    seen: set[int] = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, (VoxCPMQuotaExhaustedError, TimeoutError)):
            return True
        low = str(cur).lower()
        if _quota_failure_kind(low) is not None:
            return True
        if (
            "queue timed out" in low
            or "total failover budget" in low
            or "quota exhausted" in low
        ):
            return True
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)
    return False


def has_solvent_voxcpm_tokens(engine: Any) -> bool:
    """True when at least one non-dead token remains solvent for a transient retry (Part 2 #3c)."""
    try:
        from app.services import hf_pool_ledger as _hf_ledger

        st = _hf_ledger.load_pool_state()
        now = time.time()
        tokens = list(getattr(engine, "hf_tokens", None) or get_available_hf_tokens())
        if not tokens:
            return not bool(st.get("tokens"))
        mem = getattr(engine, "job_memory", None)
        pin = getattr(engine, "pinned_space", None)
        gate = VoxCPMEngine._known_space_gate(pin, ledger_state=st)
        for tok in tokens:
            if not tok:
                continue
            if _hf_ledger.is_token_auth_dead(tok, state=st, now_ts=now):
                continue
            if mem is not None and mem.token_dead(tok):
                continue
            bal = VoxCPMEngine._known_token_balance(tok, ledger_state=st)
            if bal is not None and (bal <= 0 or (gate is not None and gate > 0 and bal < gate)):
                continue
            return True
        return False
    except Exception:
        return True


_QUOTA_ERROR_MARKERS = ("zerogpu", "quota", "runs limit", "402", "payment required")
_GATE_REQUEST_RE = re.compile(r"\d+\s*s requested")


def _quota_failure_kind(error_text: str) -> Optional[str]:
    """Classify an error as a quota failure: "gate" for the per-space
    admission rejection (the space asked for more GPU-seconds than the
    token's account has left — space-specific, the account may still
    serve on a space requesting less), "dead" for account-level quota
    exhaustion, or None when the error is not quota-class."""
    text = str(error_text or "").lower()
    if not any(marker in text for marker in _QUOTA_ERROR_MARKERS):
        return None
    if _GATE_REQUEST_RE.search(text):
        return "gate"
    return "dead"


_ADMISSION_REQUESTED_RE = re.compile(r"(\d+(?:\.\d+)?)\s*s requested")
_ADMISSION_LEFT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*s (?:left|remaining)")


def _parse_admission_refusal(error_text: str) -> Optional[tuple[float, float]]:
    """Batch DI: (seconds requested, seconds left) from an HF ZeroGPU
    admission refusal — production text is "You have exceeded your
    free ZeroGPU quota (94s requested vs. 65s left)". Returns None
    when the error carries no figures. The refusal text is the only
    runtime source of exact balances: Hugging Face exposes a
    read-only quota endpoint (the key-health checker polls it via
    huggingface_hub's get_zero_gpu_quota), but the engine cannot
    probe 30 accounts inside a segment's failover budget, so it
    learns from the refusals it already receives."""
    text = str(error_text or "")
    req = _ADMISSION_REQUESTED_RE.search(text)
    if req is None:
        return None
    left = _ADMISSION_LEFT_RE.search(text, req.end())
    if left is None:
        return None
    return float(req.group(1)), float(left.group(1))


def _job_token_key(token: Optional[str]) -> str:
    """Stable, non-reversible key for one caller identity in job memory."""
    if not token:
        return "anonymous"
    return _key_id(str(token))


class JobQuotaMemory:
    """Job-scoped quota memory (Batch AL), shared by every worker
    engine serving ONE job (e.g. the recap TTS pool).

    The pre-AL engine learned quota state only globally: a token whose
    account fell under a space's 180 s admission gate was deliberately
    NOT cooled down (the rejection is space-specific), so EVERY later
    segment re-walked that (space, token) pair and burned failover
    budget re-discovering the same refusal. This memory fixes the
    re-walk for the lifetime of one job:

    - ``record_gate(space, token)`` — the pair was refused at the
      space's admission gate; skip that PAIR for the rest of the job
      (the token may still work on other spaces).
    - ``record_dead_token(token)`` — the token's account quota is gone;
      skip the token on EVERY space for the rest of the job.

    Keys are token digests (never raw tokens). Thread-safe.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._gate_pairs: set[tuple[str, str]] = set()
        self._dead_tokens: set[str] = set()

    def gate_blocked(self, space_name: str, token: Optional[str]) -> bool:
        with self._lock:
            return (str(space_name), _job_token_key(token)) in self._gate_pairs

    def token_dead(self, token: Optional[str]) -> bool:
        with self._lock:
            return _job_token_key(token) in self._dead_tokens

    def record_gate(self, space_name: str, token: Optional[str]) -> None:
        with self._lock:
            self._gate_pairs.add((str(space_name), _job_token_key(token)))

    def record_dead_token(self, token: Optional[str]) -> None:
        with self._lock:
            self._dead_tokens.add(_job_token_key(token))

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "gate_pairs": sorted(f"{sp}::{tok}" for sp, tok in self._gate_pairs),
                "dead_tokens": sorted(self._dead_tokens),
            }


class JobSpacePin:
    """Job-scoped Space pin shared across worker slots (Part 2 #3 & Part 4 #2).

    Pins the first VoxCPM2 Space that succeeds for the job, and allows at most
    ONE controlled re-pin to an alternate v2 Space if every solvent token on the
    pinned Space fails.
    """

    def __init__(self, initial_space: Optional[str] = None) -> None:
        self._lock = threading.Lock()
        self._pinned_space: Optional[str] = initial_space
        self._repin_used: bool = False
        self._space_family_switched: bool = False

    @property
    def pinned_space(self) -> Optional[str]:
        with self._lock:
            return self._pinned_space

    @pinned_space.setter
    def pinned_space(self, value: Optional[str]) -> None:
        with self._lock:
            self._pinned_space = str(value) if value else None

    @property
    def repin_used(self) -> bool:
        with self._lock:
            return self._repin_used

    @repin_used.setter
    def repin_used(self, value: bool) -> None:
        with self._lock:
            self._repin_used = bool(value)

    @property
    def space_family_switched(self) -> bool:
        with self._lock:
            return self._space_family_switched

    @space_family_switched.setter
    def space_family_switched(self, value: bool) -> None:
        with self._lock:
            self._space_family_switched = bool(value)

    def pin_if_unset(self, space_name: str) -> bool:
        with self._lock:
            if self._pinned_space is None and space_name:
                self._pinned_space = str(space_name)
                return True
            return False

    def reset(self) -> None:
        with self._lock:
            self._pinned_space = None
            self._repin_used = False
            self._space_family_switched = False


def _cancel_gradio_job(job: Any) -> bool:
    """Best-effort cancel of a timed-out Gradio job so it cannot later enter the GPU
    and silently burn the caller token while the worker retries elsewhere (Part 4 #1)."""
    cancel_fn = getattr(job, "cancel", None)
    if callable(cancel_fn):
        try:
            cancel_fn()
            return True
        except Exception as exc:
            logger.debug("VoxCPM Gradio job.cancel() error ignored: %s", exc)
    return False


def mask_hf_token(tok: Optional[str]) -> str:
    """Non-reversible HF token identifier for logging.

    Audit fix G: this used to return first-7/last-4 fragments of the
    token itself; it now returns the shared key_rotation digest so no
    part of a real token ever reaches the logs.
    """
    if not tok:
        return "anonymous"
    t = tok.strip()
    if not t:
        return "anonymous"
    return f"hf#{_key_id(t)}"


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
    """Discover configured Hugging Face tokens for the single shared caller pool.

    Round 15 Part F: all configured tokens across HF_TOKEN, HUGGINGFACE_TOKEN,
    HF_TOKENS, and HF_RESERVE_TOKENS are merged and deduplicated into ONE pool
    for every tier (free, purchased, admin). There is no paid-only reserve.
    """
    tokens: List[str] = []
    # 1. Environment variables (includes former HF_RESERVE_TOKENS merged into the general pool)
    for env_var in ("HF_TOKEN", "HUGGINGFACE_TOKEN", "HF_TOKENS", "HF_RESERVE_TOKENS"):
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


def master_voxcpm_audio(
    input_wav: Path | str,
    output_wav: Path | str,
    *,
    speed: float | str | None = DEFAULT_SPEED,
    preset_voice: Optional[str] = None,
    ffmpeg_bin: Optional[str] = None,
) -> Path:
    """Studio Broadcast Audio Mastering via FFmpeg (44.1 kHz mono PCM 16-bit WAV).

    Applies ``atempo`` from ``speed`` plus the broadcast mastering chain once
    (per-chunk in non-chunked mode, or once post-stitch in multi-chunk clone mode).
    """
    src_path = Path(input_wav)
    out_path = Path(output_wav).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tempo_val = resolve_speed_tempo(speed)

    converted_cleanly = False
    ff = ffmpeg_bin or shutil.which("ffmpeg") or r"C:\ffmpeg\bin\ffmpeg.exe"
    if ff and src_path.is_file() and src_path.stat().st_size > 1000:
        import subprocess

        # Safe temporary target when mastering in-place (src_path == out_path)
        same_file = src_path.resolve() == out_path
        target_out = out_path.with_name(f"{out_path.stem}.master_tmp.wav") if same_file else out_path

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
        af_candidates = [af_master_primary, af_master_fallback]
        if preset_voice:
            try:
                from app.services.curated_voices import persona_mastering_filter

                _persona_chain = persona_mastering_filter(preset_voice)
            except Exception:
                _persona_chain = None
            if _persona_chain:
                af_candidates.insert(
                    0,
                    f"aresample=44100,{atempo_prefix}{_persona_chain}",
                )
        try:
            for af_candidate in af_candidates:
                try:
                    cmd = [
                        ff, "-y", "-hide_banner", "-loglevel", "error",
                        "-protocol_whitelist", "file,pipe",
                        "-i", str(src_path),
                        "-af", af_candidate,
                        "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le",
                        str(target_out),
                    ]
                    subprocess.run(cmd, check=True, capture_output=True, timeout=180)
                    if target_out.is_file() and target_out.stat().st_size > 1000:
                        if same_file:
                            shutil.move(str(target_out), str(out_path))
                        converted_cleanly = True
                        break
                except Exception as e_ffmpeg:
                    logger.debug("FFmpeg mastering attempt failed: %s", e_ffmpeg)
        finally:
            if same_file:
                target_out.unlink(missing_ok=True)

    if not converted_cleanly and src_path.resolve() != out_path:
        shutil.copy2(src_path, out_path)
    return out_path


def get_configured_spaces() -> List[str]:
    """Get list of candidate Hugging Face spaces for load balancing and redundancy."""
    spaces: List[str] = []
    custom_spaces = os.getenv("VOXCPM_SPACES", "").strip()
    if custom_spaces:
        for part in re.split(r"[,;\s\n]+", custom_spaces):
            sp = part.strip().strip("'\"")
            if sp and not is_voxcpm_v1_space(sp) and sp not in spaces:
                spaces.append(sp)

    single_url = os.getenv("VOXCPM_SPACE_URL", "").strip() or os.getenv("VOXCPM_API_URL", "").strip()
    if single_url and not is_voxcpm_v1_space(single_url) and single_url not in spaces:
        spaces.insert(0, single_url)

    for sp in DEFAULT_VOXCPM_SPACES:
        if sp not in spaces and not is_voxcpm_v1_space(sp):
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
    # Batch AT (AT-5): per-bind-slot rotation counters. A bound pool
    # worker (recap TTS slot N) used to restart at token N on EVERY
    # call, so the first few pool accounts absorbed nearly all
    # synthesis starts (production Window A: only 3 distinct tokens
    # across the window's attempts). The counter for a slot advances
    # on every call made with that bind index — shared across engine
    # instances and jobs, guarded by _lock — so consecutive calls
    # from the same slot start at different tokens.
    _token_bind_rr_counters: dict[int, int] = {}
    _lock = threading.Lock()
    # Batch DI: last-known ZeroGPU balances (seconds of quota left)
    # per token, plus the admission price last seen per space and
    # overall. Process-level, like the cooldowns: every engine in the
    # process shares what any walk learned. Balances are learned from
    # admission-refusal texts (exact figures) and from successful
    # calls (a success proves the balance covered that space's
    # price); see _parse_admission_refusal. Keyed by raw token, same
    # as _token_cooldowns; never persisted or logged.
    _token_balances: dict[str, float] = {}
    _space_admission_prices: dict[str, float] = {}
    _last_admission_price: Optional[float] = None

    @classmethod
    def _record_admission_refusal(
        cls, space_name: str, token: Optional[str], requested: float, left: float
    ) -> None:
        """Learn from a refusal — exact balance (`left`) and space gate (`requested`),
        persisting through the shared `aj_hf:pool_state` ledger (Part 5)."""
        with cls._lock:
            cls._space_admission_prices[str(space_name)] = float(requested)
            cls._last_admission_price = float(requested)
            if token:
                cls._token_balances[str(token)] = float(left)
        try:
            from app.services import hf_pool_ledger as _hf_ledger

            _hf_ledger.record_admission_refusal(space_name, token, requested, left)
        except Exception:
            pass

    @classmethod
    def _record_success_balance(cls, space_name: str, token: Optional[str]) -> None:
        """Debit the space's gate from the token's balance in memory and `aj_hf:pool_state`."""
        if not token:
            return
        with cls._lock:
            price = cls._space_admission_prices.get(str(space_name))
            if price is not None:
                key = str(token)
                known = cls._token_balances.get(key)
                if known is None:
                    cls._token_balances[key] = float(price)
                else:
                    cls._token_balances[key] = max(0.0, known - float(price))
        try:
            from app.services import hf_pool_ledger as _hf_ledger

            _hf_ledger.record_success_debit(
                space_name,
                token,
                debit_s=price,
            )
        except Exception:
            pass

    @classmethod
    def _known_token_balance(
        cls, token: Optional[str], *, ledger_state: Optional[dict] = None
    ) -> Optional[float]:
        if not token:
            return None
        if ledger_state:
            try:
                from app.services import hf_pool_ledger as _hf_ledger

                rem = _hf_ledger.get_token_remaining_s(token, state=ledger_state)
                if rem is not None:
                    return float(rem)
            except Exception:
                pass
        bal = cls._token_balances.get(str(token))
        return float(bal) if bal is not None else None

    @classmethod
    def _known_space_gate(
        cls, space_name: Optional[str] = None, *, ledger_state: Optional[dict] = None
    ) -> Optional[float]:
        if space_name:
            if ledger_state and isinstance(ledger_state.get("space_gates"), dict):
                gates = ledger_state["space_gates"]
                if str(space_name) in gates:
                    try:
                        g = float(gates[str(space_name)])
                        if g > 0:
                            return g
                    except (TypeError, ValueError):
                        pass
            if str(space_name) in cls._space_admission_prices:
                return float(cls._space_admission_prices[str(space_name)])
            return None
        if ledger_state and isinstance(ledger_state.get("space_gates"), dict):
            gates = ledger_state["space_gates"]
            if gates:
                vals = [float(v) for v in gates.values() if isinstance(v, (int, float)) and float(v) > 0]
                if vals:
                    return min(vals)
        return cls._last_admission_price

    @classmethod
    def _balance_sort_key(
        cls,
        token: Optional[str],
        *,
        gate: Optional[float] = None,
        ledger_state: Optional[dict] = None,
    ) -> tuple[int, float]:
        """Part 6 (#1) Best-Fit token walk order: (tier, +balance).
        Tier 0 = known solvent (`balance >= gate`, SMALLEST sufficient balance first
        so tail balances drain completely while full 300 s tokens stay intact for long units),
        Tier 1 = balance unknown, Tier 2 = known sub-gate (`balance < gate`; filtered out
        before walking so sub-gate tokens are never attempted)."""
        if not token:
            return (1, 0.0)
        bal = cls._known_token_balance(token, ledger_state=ledger_state)
        if bal is None:
            return (1, 0.0)
        eff_gate = gate if gate is not None else cls._known_space_gate(None, ledger_state=ledger_state)
        if bal <= 0 or (eff_gate is not None and bal < eff_gate):
            return (2, -bal)
        return (0, float(bal))

    # Round 15 Part F (owner direction 2026-11-23): SINGLE-POOL RULE.
    # The paid-only HF token reserve (Batch DP / DR) is permanently retired.
    # Every configured token — all tokens in HF_TOKENS plus any token still
    # listed in HF_RESERVE_TOKENS — is merged into ONE general caller pool
    # (get_available_hf_tokens) available equally to all tiers (free,
    # purchased, admin). Do NOT reintroduce tier-based token gating here.
    # Batch AJ (M12): cooldown persistence backstop. The in-memory
    # dict above stays keyed by raw token (existing behaviour/tests),
    # but the PERSISTED form — BotSetting key "aj_hf:cooldowns" via
    # key_rotation's sync store — is keyed by token DIGEST only, so no
    # raw token ever reaches the database. A fresh process maps the
    # digests back onto its currently configured tokens on first use.
    _cooldowns_restored: bool = False
    _cooldown_persist_timer: Any = None
    _HF_COOLDOWN_STORE_KEY = "aj_hf:cooldowns"

    @classmethod
    def export_cooldown_state(cls) -> dict:
        """Digest-keyed snapshot of live token cooldowns (JSON-safe)."""
        now = time.time()
        return {
            "v": 1,
            "cooldowns": {
                _key_id(tok): float(until)
                for tok, until in cls._token_cooldowns.items()
                if tok and until > now
            },
        }

    @classmethod
    def import_cooldown_state(cls, state: Any, tokens: Any = None) -> int:
        """Restore a digest-keyed snapshot onto `tokens`. Returns count."""
        if not isinstance(state, dict):
            return 0
        raw = state.get("cooldowns") or {}
        if not isinstance(raw, dict):
            return 0
        pool = list(tokens) if tokens is not None else get_available_hf_tokens()
        by_digest = {_key_id(tok): tok for tok in pool if tok}
        now = time.time()
        restored = 0
        for digest, until in raw.items():
            tok = by_digest.get(str(digest))
            try:
                expiry = float(until)
            except (TypeError, ValueError):
                continue
            if tok is not None and expiry > now:
                cls._token_cooldowns[tok] = expiry
                restored += 1
        return restored

    @classmethod
    def persist_cooldowns_now(cls) -> bool:
        """Write the cooldown snapshot to the BotSetting store."""
        try:
            from app.services.key_rotation import botsetting_sync_write

            return botsetting_sync_write(
                cls._HF_COOLDOWN_STORE_KEY, json.dumps(cls.export_cooldown_state())
            )
        except Exception:
            return False

    @classmethod
    def load_cooldowns_from_store(cls, tokens: Any = None) -> bool:
        """Read + apply the persisted snapshot. True when one existed."""
        try:
            from app.services.key_rotation import botsetting_sync_read

            raw = botsetting_sync_read(cls._HF_COOLDOWN_STORE_KEY)
            if not raw:
                return False
            cls.import_cooldown_state(json.loads(raw), tokens=tokens)
            return True
        except Exception:
            return False

    @classmethod
    def _ensure_cooldowns_restored(cls, tokens: Any = None) -> dict:
        """Reload shared `aj_hf:pool_state` on its 30 s TTL (replacing the old
        one-shot boot restore that caused cross-process blindness — Part 5 #1)."""
        if not cls._cooldowns_restored:
            cls._cooldowns_restored = True
            cls.load_cooldowns_from_store(tokens=tokens)
        try:
            from app.services import hf_pool_ledger as _hf_ledger

            return _hf_ledger.load_pool_state()
        except Exception:
            return {}

    @classmethod
    def _schedule_cooldown_persist(cls) -> None:
        try:
            timer = cls._cooldown_persist_timer
            if timer is not None:
                timer.cancel()
            timer = threading.Timer(2.0, cls.persist_cooldowns_now)
            timer.daemon = True
            cls._cooldown_persist_timer = timer
            timer.start()
        except Exception:
            pass

    def __init__(
        self,
        space_name: Optional[str] = None,
        work_dir: Optional[Path | str] = None,
        log_fn: Optional[Callable[[str], None]] = None,
        job_memory: Optional[JobQuotaMemory] = None,
        token_bind_index: Optional[int] = None,
        pin_space: Optional[bool] = None,
        space_pin: Optional[JobSpacePin] = None,
        spaces: Optional[List[str]] = None,
        hf_tokens: Optional[List[str]] = None,
    ):
        raw_spaces = list(spaces) if spaces is not None else get_configured_spaces()
        self.spaces = [
            sp
            for sp in raw_spaces
            if sp and not is_voxcpm_v1_space(sp) and not is_cpu_demo_space(sp)
        ]
        if (
            space_name
            and not is_voxcpm_v1_space(space_name)
            and not is_cpu_demo_space(space_name)
            and space_name not in self.spaces
        ):
            self.spaces.insert(0, space_name)
        self.primary_space = self.spaces[0] if self.spaces else ""
        self.hf_tokens = list(hf_tokens) if hf_tokens is not None else get_available_hf_tokens()
        self.work_dir = Path(work_dir) if work_dir else Path(os.environ.get("TEMP", ".")) / "voxcpm_cache"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.log_fn = log_fn
        # Batch AL: quota learning scoped to one job, shared across the
        # job's worker engines; a standalone engine gets a private one.
        self.job_memory = job_memory if job_memory is not None else JobQuotaMemory()
        # Batch AL: parallel-pool workers bind to a starting token so
        # concurrent syntheses do not all queue on the same account.
        self.token_bind_index = token_bind_index
        # Prosody Phase 1 + HF Pool Phase 1: per-job space pinning, optionally shared
        # across worker slots via `space_pin` (Part 4 #2).
        self.space_pin: JobSpacePin = space_pin if space_pin is not None else JobSpacePin()
        self.pin_space_enabled: bool = (
            True if space_pin is not None else ((token_bind_index is None) if pin_space is None else bool(pin_space))
        )

    @property
    def pinned_space(self) -> Optional[str]:
        return self.space_pin.pinned_space

    @pinned_space.setter
    def pinned_space(self, value: Optional[str]) -> None:
        self.space_pin.pinned_space = value

    @property
    def space_family_switched(self) -> bool:
        return self.space_pin.space_family_switched

    @space_family_switched.setter
    def space_family_switched(self, value: bool) -> None:
        self.space_pin.space_family_switched = value

    def reset_pinned_space(self) -> None:
        """Clear per-job pinned Space state between jobs."""
        self.space_pin.reset()

    def _log(self, msg: str) -> None:
        logger.info(msg)
        if self.log_fn:
            try:
                self.log_fn(msg)
            except Exception:
                pass

    def _pair_blocked_by_job(self, space_name: str, token: Optional[str]) -> bool:
        """True when this job's quota memory has ruled the pair out:
        the token is quota-dead (every space) or this exact
        (space, token) pair was refused at the admission gate."""
        mem = self.job_memory
        if mem is None:
            return False
        return mem.token_dead(token) or mem.gate_blocked(space_name, token)

    def _adapt_reference_for_clone(self, ref_path: Path) -> Path:
        """Batch BU: return the reference path this call should use.

        Probes the reference once; when it exceeds the VoxCPM Space
        cap (REFERENCE_MAX_SECONDS) the clone runs from a trimmed
        copy holding its first REFERENCE_TRIM_SECONDS, written into
        the engine work dir — the job-scoped temp dir the caller
        already owns, so the copy dies with the job's existing temp
        lifecycle. The outcome is published on
        ``last_reference_trimmed`` (+ the two duration fields) for
        the caller's honesty surfaces, mirroring last_fallback_used.

        Never raises and never blocks a working job: an unprobeable
        reference, a failed trim, or any unexpected error returns the
        ORIGINAL path and leaves the flags unset — byte-identical to
        pre-BU behaviour. References at/under the cap (including every
        curated persona sample) are returned untouched.
        """
        try:
            duration = _probe_reference_duration_seconds(ref_path)
            if duration is None or duration <= REFERENCE_MAX_SECONDS:
                return ref_path
            stat = ref_path.stat()
            digest = hashlib.sha256(
                f"{ref_path.resolve()}|{stat.st_size}|{int(stat.st_mtime)}".encode("utf-8")
            ).hexdigest()[:16]
            trimmed = self.work_dir / f"ref_trim_{digest}.wav"
            if not (trimmed.is_file() and trimmed.stat().st_size > 0):
                if not _trim_reference_audio(ref_path, trimmed, REFERENCE_TRIM_SECONDS):
                    logger.warning(
                        "VoxCPM reference trim failed for %s (%.1fs) — using the original reference",
                        ref_path.name, duration,
                    )
                    return ref_path
            self.last_reference_trimmed = True
            self.last_reference_original_seconds = round(duration, 1)
            self.last_reference_trimmed_seconds = REFERENCE_TRIM_SECONDS
            self._log(
                f"✂️ VoxCPM 2 Reference Audio trimmed: {duration:.1f}s exceeds the "
                f"{REFERENCE_MAX_SECONDS:.0f}s Space limit — cloning from its first "
                f"{REFERENCE_TRIM_SECONDS:.0f}s ({trimmed.name})"
            )
            return trimmed
        except Exception as exc:
            logger.warning(
                "VoxCPM reference trim check failed (%s) — using the original reference", exc
            )
            return ref_path

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
        speed: float | str | None = DEFAULT_SPEED,
        progress_cb: Optional[Callable[[str], None]] = None,
        retries: int = 2,
        allow_fallback: bool = True,
        fallback_cb: Optional[Callable[[str], None]] = None,
        tier: str = "free",
        skip_mastering: bool = False,
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

        # Transparency state (fix 2026-10-29): callers read these after
        # synthesize() returns to learn whether Edge fallback was used.
        self.last_fallback_used = False
        self.last_fallback_reason: Optional[str] = None
        # Batch BU: same pattern for the reference auto-trim — read by
        # the caller after synthesize() returns (see
        # _adapt_reference_for_clone).
        self.last_reference_trimmed = False
        self.last_reference_original_seconds: Optional[float] = None
        self.last_reference_trimmed_seconds: Optional[float] = None

        clean_text = str(text or "").strip()
        if not clean_text:
            raise ValueError("VoxCPM input text is empty.")

        # Resolve brisk narration tempo multiplier from speed & persona
        tempo_val = resolve_speed_tempo(speed)

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

        # Check local cache for identical text + voice configuration (mastered outputs only)
        cache_key = None
        cached_file = None
        if not skip_mastering and (not reference_wav or is_persona_ref):
            cache_key = hashlib.sha256(
                f"{clean_text}_{control_instruction}_{prompt_text}_{cfg_value:.2f}_{preset_voice}_{tempo_val:.3f}".encode("utf-8")
            ).hexdigest()[:16]
            cached_file = self.work_dir / f"voxcpm_{cache_key}.wav"
            if cached_file.is_file() and cached_file.stat().st_size > 4000:
                shutil.copy2(cached_file, out_path)
                self._log(f"♻️ VoxCPM 2 Cache Hit: {out_path.name}")
                return out_path

        # Phase 1 Part 5: reload shared `aj_hf:pool_state` on its 30 s TTL across processes.
        ledger_state = self._ensure_cooldowns_restored(self.hf_tokens)
        if not isinstance(ledger_state, dict):
            ledger_state = {}
        ledger_seeded = bool(ledger_state.get("tokens"))

        from app.services import hf_pool_ledger as _hf_ledger
        from gradio_client import handle_file

        # Prepare reference audio for zero-shot cloning if provided
        ref_file = None
        if reference_wav:
            ref_path = Path(reference_wav)
            if ref_path.is_file():
                ref_path = self._adapt_reference_for_clone(ref_path)
                ref_file = handle_file(str(ref_path.resolve()))
                self._log(f"🎵 VoxCPM 2 Reference Audio loaded: {ref_path.name} ({ref_path.stat().st_size} bytes)")
            else:
                logger.warning("VoxCPM reference_wav not found: %s — proceeding with Voice Design", reference_wav)

        # Build candidate (space, token) pairs with health + Best-Fit sorting
        now = time.time()
        auth_tokens = [
            tok
            for tok in self.hf_tokens
            if tok and not _hf_ledger.is_token_auth_dead(tok, state=ledger_state, now_ts=now)
        ]
        if not self.hf_tokens:
            raw_tokens: List[Optional[str]] = [None]
        elif ledger_seeded:
            # When the ledger is seeded, walk authenticated tokens only (never blind anonymous).
            raw_tokens = list(auth_tokens)
        else:
            raw_tokens = list(auth_tokens)
            if None not in raw_tokens:
                raw_tokens.append(None)

        def _is_cooling(t: Optional[str]) -> bool:
            if t is None:
                return False
            if self._token_cooldowns.get(t, 0) > now:
                return True
            return _hf_ledger.is_token_cooling(t, state=ledger_state, now_ts=now)

        def _token_sort_key(t: Optional[str]) -> int:
            if t is None:
                return 1
            return 2 if _is_cooling(t) else 0

        tokens_pool = sorted(raw_tokens, key=_token_sort_key)
        # Prosody Phase 1 + HF Pool Phase 1 Part 4 (#4): exclude v1 spaces AND CPU demo.
        candidate_spaces = [
            sp
            for sp in self.spaces
            if not is_voxcpm_v1_space(sp) and not is_cpu_demo_space(sp)
        ]
        if ref_file is not None:
            capable = [sp for sp in candidate_spaces if space_supports_clone(sp)]
            skipped = [sp for sp in candidate_spaces if sp not in capable]
            for sp in skipped:
                self._log(f"ℹ️ Space '{sp}' has no reference-clone endpoint; skipping it for this clone request")
            candidate_spaces = capable

        # Rotate healthy tokens across requests (round-robin tie-breaker across worker slots).
        healthy_auth = [t for t in tokens_pool if t is not None and not _is_cooling(t)]
        ordered_toks: List[Optional[str]] = list(tokens_pool)
        if len(healthy_auth) > 1:
            if self.token_bind_index is not None:
                bind = int(self.token_bind_index)
                with self._lock:
                    advance = self._token_bind_rr_counters.get(bind, 0)
                    self._token_bind_rr_counters[bind] = advance + 1
                shift = (bind + advance) % len(healthy_auth)
            else:
                with self._lock:
                    shift = self._token_rr_counter % len(healthy_auth)
                    self._token_rr_counter += 1
            rotated_auth = healthy_auth[shift:] + healthy_auth[:shift]
            other_toks = [t for t in tokens_pool if t not in healthy_auth]
            ordered_toks = rotated_auth + other_toks

        def _solvent_tokens_for_space(sp: str) -> tuple[List[Optional[str]], int]:
            """Return Best-Fit ordered solvent tokens for `sp` plus count of sub-gate tokens skipped."""
            sp_gate = VoxCPMEngine._known_space_gate(sp, ledger_state=ledger_state)
            kept: List[tuple[int, Optional[str]]] = []
            sub_gate_skipped = 0
            for pos, tok in enumerate(ordered_toks):
                if tok is not None:
                    bal = VoxCPMEngine._known_token_balance(tok, ledger_state=ledger_state)
                    if bal is not None and (bal <= 0 or (sp_gate is not None and sp_gate > 0 and bal < sp_gate)):
                        sub_gate_skipped += 1
                        continue
                kept.append((pos, tok))
            kept.sort(
                key=lambda item: (
                    _token_sort_key(item[1]),
                    *VoxCPMEngine._balance_sort_key(item[1], gate=sp_gate, ledger_state=ledger_state),
                    item[0],
                )
            )
            return [tok for _pos, tok in kept], sub_gate_skipped

        is_pinned_walk = False
        repin_candidate: Optional[str] = None
        pairs: List[Tuple[str, Optional[str]]] = []
        sub_gate_filtered_total = 0

        cur_pin = self.pinned_space
        if self.pin_space_enabled and cur_pin and cur_pin in candidate_spaces:
            is_pinned_walk = True
            pin_toks, skipped_pin = _solvent_tokens_for_space(cur_pin)
            sub_gate_filtered_total += skipped_pin
            for tok in pin_toks[:MAX_PINNED_SPACE_TOKENS]:
                pairs.append((cur_pin, tok))
            # Part 2 (#3b): ONE controlled re-pin to an alternate v2 space (up to 3 solvent tokens).
            if not self.space_pin.repin_used:
                alt_space = next((sp for sp in candidate_spaces if sp != cur_pin), None)
                if alt_space is not None:
                    alt_toks, skipped_alt = _solvent_tokens_for_space(alt_space)
                    sub_gate_filtered_total += skipped_alt
                    repin_candidate = alt_space
                    for tok in alt_toks[:MAX_REPIN_SPACE_TOKENS]:
                        pairs.append((alt_space, tok))
        else:
            per_space_toks: dict[str, List[Optional[str]]] = {}
            max_len = 0
            for sp in candidate_spaces:
                sp_toks, skipped_sp = _solvent_tokens_for_space(sp)
                sub_gate_filtered_total += skipped_sp
                per_space_toks[sp] = sp_toks
                if len(sp_toks) > max_len:
                    max_len = len(sp_toks)
            for round_idx in range(max_len):
                for sp in candidate_spaces:
                    sp_toks = per_space_toks[sp]
                    if round_idx < len(sp_toks):
                        pairs.append((sp, sp_toks[round_idx]))

        total_pairs = len(pairs) + sub_gate_filtered_total
        pairs = [
            (sp, tok) for sp, tok in pairs
            if not self._pair_blocked_by_job(sp, tok)
        ]
        all_pairs_blocked = (
            (total_pairs > 0 and not pairs)
            or (
                not pairs
                and bool(self.hf_tokens)
                and not auth_tokens
            )
        )
        if all_pairs_blocked:
            self._log(
                "🚫 VoxCPM 2: every (space, token) pair is ruled out by quota/ledger memory "
                "— failing fast instead of blind walking."
            )

        last_err: Exception | None = None
        max_attempts = len(pairs)
        attempts_made = 0
        quota_failures = 0
        deadline_cut = False

        _budget_env = os.getenv("VOXCPM_TOTAL_TIMEOUT", "").strip()
        _tier_norm = str(tier or "free").strip().lower()
        _default_budget = (
            SEGMENT_BUDGET_PAID_SECONDS if _tier_norm == "paid" else SEGMENT_BUDGET_FREE_SECONDS
        )
        total_budget = float(
            kwargs.get("total_timeout") or (_budget_env if _budget_env else _default_budget)
        )
        synth_deadline = time.time() + total_budget

        space_attempts: dict[str, int] = {}
        for attempt, (space_name, active_token) in enumerate(pairs, 1):
            if time.time() > synth_deadline:
                deadline_cut = True
                self._log(f"⏱️ VoxCPM total failover budget ({total_budget:.0f}s) exhausted; stopping further space attempts")
                break
            if self._pair_blocked_by_job(space_name, active_token):
                continue
            if not is_pinned_walk:
                if space_attempts.get(space_name, 0) >= MAX_ATTEMPTS_PER_SPACE:
                    continue
            elif repin_candidate is not None and space_name == repin_candidate and not self.space_pin.repin_used:
                self.space_pin.repin_used = True
                self._log(f"🔀 VoxCPM 2 controlled one-time re-pin attempt: '{cur_pin}' -> '{repin_candidate}'")

            space_attempts[space_name] = space_attempts.get(space_name, 0) + 1
            attempts_made += 1
            tok_masked = mask_hf_token(active_token)
            try:
                self._log(
                    f"🌐 [VoxCPM 2] Attempt {attempt}/{max_attempts} on Space '{space_name}' "
                    f"| Token: {tok_masked} | Pool: {len(tokens_pool)} tokens | chars={len(clean_text)}"
                )
                if progress_cb:
                    try:
                        progress_cb("🎙️ အသံ ဖန်တီးနေပါသည်…")
                    except Exception:
                        pass

                client = self.get_client(space_name, hf_token=active_token)
                effective_control = control_instruction or DEFAULT_BURMESE_CONTROL
                if ref_file and prompt_text:
                    effective_control = ""
                elif ref_file and not prompt_text and (
                    not control_instruction or control_instruction == DEFAULT_BURMESE_CONTROL
                ):
                    effective_control = DEFAULT_CLONE_FIDELITY_CONTROL
                endpoints = space_api_endpoints(space_name)

                if endpoints["clone"] == "/generate_clone":
                    if ref_file:
                        if not prompt_text:
                            self._log(f"ℹ️ Space '{space_name}' cloning needs the reference transcript; skipping to next space...")
                            continue
                        job = client.submit(
                            ref_audio_path=ref_file,
                            ref_text=prompt_text,
                            target_text=clean_text,
                            api_name=endpoints["clone"],
                        )
                    else:
                        chosen_style = PRESET_MAP_YEYEE.get(preset_voice) if preset_voice else None
                        job = client.submit(
                            style=chosen_style or effective_control,
                            target_text=clean_text,
                            api_name=endpoints["style"],
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
                    job = client.submit(
                        text_input=clean_text,
                        control_instruction=effective_control,
                        reference_wav_path_input=ref_file,
                        use_prompt_text=bool(prompt_text),
                        prompt_text_input=prompt_text or "",
                        cfg_value_input=float(cfg_value or 2.0),
                        do_normalize=True,
                        denoise=True,
                        api_name=endpoints["clone"],
                    )

                # Poll status with queue tracking
                poll_start = time.time()
                _env_timeout = os.getenv("VOXCPM_TIMEOUT", "").strip()
                if _env_timeout:
                    timeout_sec = int(_env_timeout)
                else:
                    try:
                        from app.config import settings as _settings
                        timeout_sec = int(getattr(_settings, "voxcpm_timeout", 60) or 60)
                    except Exception:
                        timeout_sec = 60
                timeout_sec = int(kwargs.get("timeout") or timeout_sec)
                while not job.done():
                    # Part 4 (#1): cancel orphaned Gradio GPU runs BEFORE raising TimeoutError.
                    if time.time() - poll_start > timeout_sec:
                        _cancel_gradio_job(job)
                        raise TimeoutError(f"VoxCPM 2 Space '{space_name}' queue timed out after {timeout_sec}s")
                    if time.time() > synth_deadline:
                        _cancel_gradio_job(job)
                        raise TimeoutError(f"VoxCPM 2 total failover budget ({total_budget:.0f}s) exhausted on space '{space_name}'")

                    try:
                        st = job.status()
                        eta_val = getattr(st, "eta", None)
                        pos_val = getattr(st, "queue_position", None) or getattr(st, "position", None)
                        if eta_val is not None and float(eta_val) > 0:
                            eta_sec = int(round(float(eta_val)))
                            if pos_val is not None:
                                msg = f"⏳ စောင့်ဆိုင်းစာရင်းတွင် နံပါတ် {pos_val} — ခန့်မှန်းကြာချိန် ~{eta_sec}s"
                            else:
                                msg = f"⏳ စောင့်ဆိုင်းစာရင်းတွင် စောင့်ဆိုင်းနေပါသည် — ခန့်မှန်းကြာချိန် ~{eta_sec}s"
                            self._log(msg)
                            if progress_cb:
                                try:
                                    progress_cb(msg)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    remaining_attempt = timeout_sec - (time.time() - poll_start)
                    time.sleep(max(0.05, min(2.0, remaining_attempt)))

                raw_result = job.result()
                result_path_str = _resolve_result_path(raw_result)

                if result_path_str and os.path.isfile(result_path_str) and os.path.getsize(result_path_str) > 1000:
                    if self.pin_space_enabled:
                        if self.space_pin.pin_if_unset(space_name):
                            self._log(f"📌 VoxCPM 2 Space pinned for job: '{space_name}'")
                        elif repin_candidate is not None and space_name == repin_candidate:
                            prev_fam = space_endpoint_family(cur_pin or "")
                            new_fam = space_endpoint_family(space_name)
                            self.pinned_space = space_name
                            if cur_pin and prev_fam != new_fam:
                                self.space_family_switched = True
                                self._log(
                                    f"📌 VoxCPM 2 Space re-pinned for job: '{space_name}' "
                                    f"(space_family_switched=True: {prev_fam} -> {new_fam})"
                                )
                            else:
                                self._log(f"📌 VoxCPM 2 Space re-pinned for job: '{space_name}'")

                    if skip_mastering:
                        shutil.copy2(result_path_str, out_path)
                    else:
                        master_voxcpm_audio(
                            result_path_str,
                            out_path,
                            speed=speed,
                            preset_voice=preset_voice,
                        )
                        if cached_file and (not ref_file or is_persona_ref):
                            try:
                                shutil.copy2(out_path, cached_file)
                            except Exception:
                                pass

                    VoxCPMEngine._record_success_balance(space_name, active_token)
                    self._log(f"✅ VoxCPM 2 Success [{tok_masked}]: {out_path.name} ({out_path.stat().st_size} bytes)")
                    return out_path
                else:
                    raise RuntimeError(f"VoxCPM 2 returned empty result from space '{space_name}'")

            except Exception as e_pair:
                last_err = e_pair
                err_lower = str(e_pair).lower()
                quota_kind = _quota_failure_kind(err_lower)
                if quota_kind is not None:
                    quota_failures += 1
                if quota_kind == "gate":
                    if self.job_memory is not None:
                        self.job_memory.record_gate(space_name, active_token)
                    _adm = _parse_admission_refusal(str(e_pair))
                    if _adm is not None:
                        VoxCPMEngine._record_admission_refusal(
                            space_name, active_token, _adm[0], _adm[1]
                        )
                        ledger_state = _hf_ledger.load_pool_state()
                    self._log(f"⚠️ Token {tok_masked} refused at '{space_name}' admission gate (GPU-seconds requested > balance); pair memoised for this job, rotating...")
                elif quota_kind == "dead":
                    if self.job_memory is not None:
                        self.job_memory.record_dead_token(active_token)
                    if active_token:
                        self._token_cooldowns[active_token] = time.time() + 1800
                        self._schedule_cooldown_persist()
                        try:
                            _hf_ledger.record_quota_dead(active_token)
                            ledger_state = _hf_ledger.load_pool_state()
                        except Exception:
                            pass
                    self._log(f"⚠️ Token {tok_masked} hit ZeroGPU quota on '{space_name}'; rotating...")
                elif active_token and _is_hf_token_auth_failure(err_lower):
                    # Part 5 (#3): confirm once via whoami-v2; if still failing (401/403),
                    # mark `auth_dead` for 24 h and exclude from all walks.
                    quarantined = False
                    try:
                        quarantined = _hf_ledger.handle_auth_failure(active_token)
                        ledger_state = _hf_ledger.load_pool_state()
                    except Exception:
                        quarantined = False
                    if quarantined:
                        if self.job_memory is not None:
                            self.job_memory.record_dead_token(active_token)
                        self._token_cooldowns[active_token] = time.time() + 86400
                        self._schedule_cooldown_persist()
                        self._log(f"🚫 Token {tok_masked} confirmed auth_dead via whoami-v2; quarantined for 24h...")
                    else:
                        self._token_cooldowns[active_token] = time.time() + 1800
                        self._schedule_cooldown_persist()
                        self._log(f"⚠️ Token {tok_masked} rejected on '{space_name}'; cooling down for 30m...")
                self._log(f"⚠️ VoxCPM 2 Pair ({space_name}, {tok_masked}) failed: {_format_pair_error(e_pair)}")
                time.sleep(0.5)

        # Fallback to the Edge-TTS studio persona engine — ONLY when allow_fallback=True
        # (never allowed on VoxCPM clone jobs — Part 2).
        if not allow_fallback:
            _pure_quota = all_pairs_blocked or (
                attempts_made > 0 and quota_failures >= attempts_made
            )
            _dr_clause = (
                _tier_norm == "paid"
                and deadline_cut
                and attempts_made >= 1
                and quota_failures >= 1
            )
            if _pure_quota or _dr_clause:
                if _dr_clause and not _pure_quota:
                    self._log("🅿️ VoxCPM paid walk cut by the failover budget with quota refusals seen — treating as quota exhaustion (park-eligible)")
                raise VoxCPMQuotaExhaustedError(
                    f"VoxCPM 2 quota exhausted on every space/token available to this job: {last_err}"
                ) from last_err
            raise RuntimeError(
                f"VoxCPM 2 speech synthesis failed after retrying all tokens and spaces: {last_err}"
            ) from last_err
        self.last_fallback_reason = str(last_err)
        logger.warning(
            "EDGE_FALLBACK_USED: all suitable VoxCPM spaces failed (%s); using Edge TTS persona fallback — voice will differ from the VoxCPM preview",
            last_err,
        )
        self._log(f"⚠️ EDGE_FALLBACK_USED: VoxCPM 2 spaces unavailable ({last_err}). Invoking Edge TTS persona fallback...")
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
                self.last_fallback_used = True
                if fallback_cb:
                    try:
                        fallback_cb(str(last_err))
                    except Exception:
                        pass
                self._log(f"✅ Fallback Studio Audio successfully delivered: {out_path.name} ({out_path.stat().st_size} bytes)")
                return out_path
        except Exception as e_fb:
            logger.warning("Neural fallback error: %s", e_fb)

        raise RuntimeError(
            f"VoxCPM 2 speech synthesis failed after retrying all tokens and spaces: {last_err}"
        )


__all__ = ["VoxCPMEngine", "DEFAULT_VOXCPM_SPACE", "DEFAULT_BURMESE_CONTROL", "DEFAULT_CLONE_FIDELITY_CONTROL", "get_available_hf_tokens", "space_supports_clone", "is_voxcpm_v1_space", "is_cpu_demo_space", "master_voxcpm_audio", "space_api_endpoints", "space_endpoint_family", "SPACE_API_ENDPOINTS", "MAX_ATTEMPTS_PER_SPACE", "MAX_PINNED_SPACE_TOKENS", "MAX_REPIN_SPACE_TOKENS", "SEGMENT_BUDGET_FREE_SECONDS", "SEGMENT_BUDGET_PAID_SECONDS", "TRANSIENT_RETRY_BACKOFF_SECONDS", "is_voxcpm_capacity_or_congestion_error", "has_solvent_voxcpm_tokens", "PRESET_MAP_YEYEE", "DEFAULT_VOXCPM_SPACES", "JobQuotaMemory", "JobSpacePin", "VoxCPMQuotaExhaustedError"]

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
    # GPU spaces first (fix 2026-10-29): the official demo below is
    # CPU-basic and almost always timed out, burning the whole attempt
    # budget before a GPU space was ever reached.
    "yeyee2609/voxcpm2",
    "soemintheinsmt6/myanmar-voxcpm2",
    "yeyee2609/voxcpm",
    # CPU demo kept only as the last resort.
    "openbmb/VoxCPM-Demo",
]

# Per-space capability map (fix 2026-10-29): a request that carries a
# reference clip is a CLONE request and may only be routed to a space
# whose API accepts reference audio + reference text. Prompt-only
# spaces may still serve plain TTS (voice design) requests.
#   - yeyee2609/*        : /generate_clone (ref audio + ref text) + style TTS
#   - soemintheinsmt6/*  : prompt /tts only — NO reference-clone endpoint
#   - openbmb demo       : /generate accepts a reference wav
_CLONE_INCAPABLE_SPACE_MARKERS = ("soemintheinsmt6",)


def space_supports_clone(space_name: str) -> bool:
    """True if the space can take a reference-audio clone request."""
    name = str(space_name or "").lower()
    return not any(marker in name for marker in _CLONE_INCAPABLE_SPACE_MARKERS)


# Batch AT (AT-2): per-space attempt cap inside ONE synthesis call.
# The pre-AT walk was space-major, so a single congested/broken space
# could consume the whole global failover budget before a healthy
# space was ever tried (production voice job a670e490: 18 attempts,
# all on space #1, then the 240 s budget expired). Even with the
# round-robin order (AT-1), this cap bounds how much of the budget
# one space may eat per call; the global budget stays the outer bound.
MAX_ATTEMPTS_PER_SPACE = 4

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
    or DEFAULT_VOXCPM_SPACES[0]
)
DEFAULT_BURMESE_CONTROL = (
    "A clear, natural, emotionally expressive Burmese speaker with warm tone, "
    "smooth studio acoustic, velvety cadence, and pristine pronunciation without harsh sibilance or background noise. "
    "Speak in Myanmar language with authentic accent."
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
        """Batch DI: learn from a refusal — the token's exact balance
        (``left``) and the space's admission price (``requested``)."""
        with cls._lock:
            cls._space_admission_prices[str(space_name)] = float(requested)
            cls._last_admission_price = float(requested)
            if token:
                cls._token_balances[str(token)] = float(left)

    @classmethod
    def _record_success_balance(cls, space_name: str, token: Optional[str]) -> None:
        """Batch DI: a successful call proves the token's balance
        covered this space's admission price. When the balance is
        already known exactly (learned from a refusal), debit the
        price just paid; when it is unknown, record the price as the
        lower bound the success guarantees."""
        if not token:
            return
        with cls._lock:
            price = cls._space_admission_prices.get(str(space_name))
            if price is None:
                return
            key = str(token)
            known = cls._token_balances.get(key)
            if known is None:
                cls._token_balances[key] = float(price)
            else:
                cls._token_balances[key] = max(0.0, known - float(price))

    @classmethod
    def _balance_sort_key(cls, token: Optional[str]) -> tuple[int, float]:
        """Batch DI walk order: (tier, -balance). Tier 0 = known
        solvent (balance >= the last-seen admission price, richest
        first), tier 1 = balance unknown, tier 2 = known insolvent
        (balance below the last-seen price; closest to solvent
        first). The anonymous caller has no per-token balance and
        always sorts as unknown."""
        if not token:
            return (1, 0.0)
        bal = cls._token_balances.get(str(token))
        if bal is None:
            return (1, 0.0)
        price = cls._last_admission_price
        if price is not None and bal < price:
            return (2, -bal)
        return (0, -bal)

    # Batch DP (Item 1): the paid reserve. A slice of the caller pool
    # that FREE-tier walks never touch, so an evening of free traffic
    # cannot drain the whole ZeroGPU pool before a paid job arrives.
    # The reserve set is the UNION of three definitions:
    #   * PRIMARY — operator-designated tokens (owner amendment
    #     2026-11-11): token VALUES listed in the HF_RESERVE_TOKENS
    #     env var are reserve from cold start, deterministically —
    #     this is how the PRO account's token (2,400 s/day, by far
    #     the largest balance) is made paid-only;
    #   * the LAST ``reserve_n`` tokens of the configured
    #     (discovery-order) list;
    #   * any token whose learned balance is rich (>= 900 s — the
    #     accounts that carry a paid job through an evening).
    # The anonymous caller (None) is never reserve. With fewer than
    # 8 configured tokens there is no positional reserve at all — a
    # tiny pool cannot afford to strand most of itself.
    _RESERVE_MIN_POOL = 8
    _RESERVE_RICH_BALANCE = 900.0

    @classmethod
    def _designated_reserve_tokens(cls) -> set:
        """Operator-designated reserve token VALUES from the
        HF_RESERVE_TOKENS env var — comma/semicolon/whitespace
        separated, parsed exactly like get_available_hf_tokens
        parses the pool (split + quote strip). Read live on every
        reserve computation; never persisted anywhere."""
        raw = os.getenv("HF_RESERVE_TOKENS", "") or ""
        out: set = set()
        for part in re.split(r"[,;\s\n]+", raw):
            tok = part.strip().strip("'\"")
            if tok:
                out.add(tok)
        return out

    @classmethod
    def _reserve_token_set(cls, discovery_order_tokens) -> set:
        """The reserve set for a pool in DISCOVERY (configured) order:
        designated ∪ positional-last-slice ∪ learned-rich.

        Positional membership must come from the configured order,
        never from a health/balance-sorted walk order: the last
        entries of a sorted list would be the cooling tokens, which
        is the opposite of a reserve. Rich tokens join from the
        class-level learned balances (Batch DI); designated tokens
        join by exact value from HF_RESERVE_TOKENS."""
        toks = [t for t in (discovery_order_tokens or []) if t]
        reserve: set = set()
        designated = cls._designated_reserve_tokens()
        for tok in toks:
            if tok in designated:
                reserve.add(tok)
        n = len(toks)
        if n >= cls._RESERVE_MIN_POOL:
            reserve_n = max(2, min(8, n // 4))
            reserve.update(toks[-reserve_n:])
        for tok in toks:
            bal = cls._token_balances.get(str(tok))
            if bal is not None and bal >= cls._RESERVE_RICH_BALANCE:
                reserve.add(tok)
        return reserve
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
    def _ensure_cooldowns_restored(cls, tokens: Any = None) -> None:
        if cls._cooldowns_restored:
            return
        cls._cooldowns_restored = True
        cls.load_cooldowns_from_store(tokens=tokens)

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
    ):
        self.spaces = get_configured_spaces()
        if space_name and space_name not in self.spaces:
            self.spaces.insert(0, space_name)
        self.primary_space = self.spaces[0]
        self.hf_tokens = get_available_hf_tokens()
        self.work_dir = Path(work_dir) if work_dir else Path(os.environ.get("TEMP", ".")) / "voxcpm_cache"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.log_fn = log_fn
        # Batch AL: quota learning scoped to one job, shared across the
        # job's worker engines; a standalone engine gets a private one.
        self.job_memory = job_memory if job_memory is not None else JobQuotaMemory()
        # Batch AL: parallel-pool workers bind to a starting token so
        # concurrent syntheses do not all queue on the same account.
        self.token_bind_index = token_bind_index

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

        # Batch AJ (M12): resume token cooldowns persisted by the
        # previous process before the pool order is computed, so a
        # quota/auth-dead token is not retried first after every boot.
        self._ensure_cooldowns_restored(self.hf_tokens)

        from gradio_client import handle_file

        # Prepare reference audio for zero-shot cloning if provided
        ref_file = None
        if reference_wav:
            ref_path = Path(reference_wav)
            if ref_path.is_file():
                # Batch BU: an over-cap reference is adapted ONCE here
                # (trimmed to its first 45 s) so every Space attempt in
                # this call uses the trimmed copy instead of failing
                # on the 50 s Space limit.
                ref_path = self._adapt_reference_for_clone(ref_path)
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
        if ref_file is not None:
            # Clone request: only clone-capable spaces may serve it. A
            # prompt-only space would ignore the reference clip and
            # return a random voice that does not match the preview.
            capable = [sp for sp in candidate_spaces if space_supports_clone(sp)]
            skipped = [sp for sp in candidate_spaces if sp not in capable]
            for sp in skipped:
                self._log(f"ℹ️ Space '{sp}' has no reference-clone endpoint; skipping it for this clone request")
            candidate_spaces = capable

        # Rotate healthy tokens across requests (starting offset only;
        # the pair ORDER itself is built round-robin below).
        healthy_auth = [t for t in tokens_pool if t is not None and self._token_cooldowns.get(t, 0) <= now]
        ordered_toks: List[Optional[str]] = list(tokens_pool)
        if len(healthy_auth) > 1:
            if self.token_bind_index is not None:
                # Batch AL: a bound pool worker starts at its OWN token,
                # so N concurrent workers spread over N different
                # accounts instead of contending on one.
                # Batch AT (AT-5): the slot's start ADVANCES by one on
                # every call (per-slot counter shared across engine
                # instances), so the same first few accounts are not
                # re-picked at the head of every segment's walk.
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

        # Batch DI: order the walk by last-known balance, richest
        # first (owner, 2026-11-10: start from the token with the
        # most quota left). Production 2026-11-10: a segment burned
        # its whole 45 s failover budget on instant admission
        # refusals from tokens holding 63–89 s against a 94 s price,
        # while richer tokens waited behind them in rotation order.
        # The sort composes with the health classes above (a cooling
        # token never jumps the queue on balance alone) and with the
        # rotation (equal keys keep their rotated relative order):
        # key = (health class, balance tier, -balance, rotated
        # position). Space priority is untouched — pairs below still
        # iterate the configured space order inside each token round.
        # Batch DP (Item 1): tier-aware walk. The reserve set is
        # computed from the CONFIGURED token order (self.hf_tokens),
        # never from the health-sorted pool below. Free walks have
        # reserve pairs filtered out entirely further down.
        # ``tier`` defaults to "free" — the safe side: an unplumbed
        # caller can never spend the reserve.
        # Batch DR REVERSAL (2026-11-12): DP sorted the reserve class
        # AFTER non-reserve within a health class — drain the shared
        # tokens first, the reserve stays a guaranteed floor. The
        # 2026-11-11 evening evidence killed that design: paid
        # segments burned their whole 45 s failover budget on slow
        # attempts and fragmented shared balances (75–94 s left vs
        # 94–180 s admission asks) and died on the clock before the
        # walk ever REACHED a reserve pair — VIP and admin segments
        # fell back to Edge while the PRO reserve sat untouched.
        # Paid walks therefore lead with the reserve INSIDE each
        # health class: the health class still dominates (a cooling
        # reserve token never jumps healthy shared tokens), and the
        # balance ordering still applies within each class. Free
        # walks are untouched.
        _dp_paid = str(tier or "free").strip().lower() == "paid"
        _dp_reserve = VoxCPMEngine._reserve_token_set(self.hf_tokens)

        def _walk_sort_key(item):
            _pos, _tok = item
            if _dp_paid:
                return (
                    _token_sort_key(_tok),
                    0 if _tok in _dp_reserve else 1,
                    *VoxCPMEngine._balance_sort_key(_tok),
                    _pos,
                )
            return (
                _token_sort_key(_tok),
                *VoxCPMEngine._balance_sort_key(_tok),
                _pos,
            )

        ordered_toks = [
            tok
            for _pos, tok in sorted(
                enumerate(ordered_toks),
                key=_walk_sort_key,
            )
        ]

        # Batch AT (AT-1): space-first round-robin pair order — one
        # pair per space per round (token-major), so every space gets
        # an early attempt before any space gets a second one. The
        # pre-AT space-major order exhausted the failover budget on
        # space #1 alone (production voice job a670e490) and burned
        # half of every recap line's attempts on one broken space
        # before reaching a healthy one (job rc_1793880623_7150ea).
        pairs: List[Tuple[str, Optional[str]]] = [
            (sp, tok) for tok in ordered_toks for sp in candidate_spaces
        ]

        # Batch AL: pairs this job has already ruled out (token quota
        # dead on every space, or the (space, token) pair refused at the
        # space's admission gate) are not walked again — pre-AL, every
        # later segment re-discovered the same refusals at full price.
        total_pairs = len(pairs)
        pairs = [
            (sp, tok) for sp, tok in pairs
            if not self._pair_blocked_by_job(sp, tok)
        ]
        # Batch DP (Item 1): a FREE walk never touches reserve tokens.
        # If the reserve filter (with the job-memory filter above)
        # leaves nothing, the call takes the existing quota-exhausted
        # path below via all_pairs_blocked — free behaviour is
        # otherwise byte-identical to before.
        if not _dp_paid and _dp_reserve:
            _pre_reserve = len(pairs)
            pairs = [
                (sp, tok) for sp, tok in pairs if tok not in _dp_reserve
            ]
            if len(pairs) != _pre_reserve:
                self._log(
                    f"🛡️ VoxCPM reserve: held {len(_dp_reserve)} token(s) "
                    "for the paid tier (free call)"
                )
        all_pairs_blocked = total_pairs > 0 and not pairs
        if all_pairs_blocked:
            self._log(
                "🚫 VoxCPM 2: every (space, token) pair is ruled out by this "
                "job's quota memory — failing fast instead of re-walking."
            )

        last_err: Exception | None = None
        max_attempts = len(pairs)
        attempts_made = 0
        quota_failures = 0
        # Batch DR: set when the walk is cut short by the total
        # failover-budget deadline (as opposed to running out of
        # pairs) — the allow_fallback=False tail below uses it to
        # recognise the mixed evening failure mode for paid walks.
        deadline_cut = False

        # Failover budget (fix 2026-10-29): each attempt keeps the
        # configured per-attempt timeout (VOXCPM_TIMEOUT / settings,
        # default 60s; recap passes 75s), but the WHOLE synthesis is
        # capped by a total budget (default 240s, VOXCPM_TOTAL_TIMEOUT)
        # so a dead first space cannot burn every token x space x 60s
        # before a healthy GPU space is tried.
        _budget_env = os.getenv("VOXCPM_TOTAL_TIMEOUT", "").strip()
        total_budget = float(kwargs.get("total_timeout") or (_budget_env if _budget_env else 240))
        synth_deadline = time.time() + total_budget

        # Batch AT (AT-2): attempts spent per space within THIS call.
        space_attempts: dict[str, int] = {}
        for attempt, (space_name, active_token) in enumerate(pairs, 1):
            if time.time() > synth_deadline:
                deadline_cut = True
                self._log(f"⏱️ VoxCPM total failover budget ({total_budget:.0f}s) exhausted; stopping further space attempts")
                break
            if self._pair_blocked_by_job(space_name, active_token):
                # Ruled out by an earlier failure in THIS walk (e.g. the
                # token died on another space a moment ago).
                continue
            if space_attempts.get(space_name, 0) >= MAX_ATTEMPTS_PER_SPACE:
                # Batch AT (AT-2): this space has had its share of
                # this call — move on even if tokens remain on it, so
                # the global budget is spread across spaces.
                continue
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
                        progress_cb(f"🎙️ အသံ ဖန်တီးနေပါသည်…")
                    except Exception:
                        pass

                client = self.get_client(space_name, hf_token=active_token)
                effective_control = control_instruction or DEFAULT_BURMESE_CONTROL
                if ref_file and prompt_text:
                    # Locked-timbre cloning: the reference clip alone defines the voice.
                    effective_control = ""
                # Batch AT (AT-3): endpoint names for THIS space —
                # "/generate" by default, the clone/style family for
                # spaces whose app exposes those functions instead.
                endpoints = space_api_endpoints(space_name)

                if "soemintheinsmt6" in space_name:
                    # Prompt-only space: clone requests were filtered
                    # out above, so only plain TTS reaches this branch.
                    job = client.submit(
                        text=clean_text,
                        voice_description=control_instruction or DEFAULT_BURMESE_CONTROL,
                        api_name="/tts",
                    )
                elif endpoints["clone"] == "/generate_clone":
                    # yeyee family + mapped duplicates of it (AT-3,
                    # e.g. yunaisbusy/voxcpm2-fast): the clone call
                    # shape below is the one their app.py declares —
                    # generate_clone(ref_audio_path, ref_text,
                    # target_text) — unchanged from the pre-AT yeyee
                    # branch; only the routing now comes from the
                    # endpoint mapping instead of the owner name.
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
                    # Fail over if queue exceeds timeout
                    if time.time() - poll_start > timeout_sec:
                        raise TimeoutError(f"VoxCPM 2 Space '{space_name}' queue timed out after {timeout_sec}s")
                    if time.time() > synth_deadline:
                        raise TimeoutError(f"VoxCPM 2 total failover budget ({total_budget:.0f}s) exhausted on space '{space_name}'")

                    try:
                        st = job.status()
                        eta_val = getattr(st, "eta", None)
                        pos_val = getattr(st, "queue_position", None) or getattr(st, "position", None)
                        if eta_val is not None and float(eta_val) > 0:
                            eta_sec = int(round(float(eta_val)))
                            # R40 deck F-3: no engine name in user-facing
                            # progress text — queue rank only.
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
                        af_candidates = [af_master_primary, af_master_fallback]
                        # Studio-spec persona mastering (2026-10-30): a
                        # non-flagship curated persona gets the full
                        # komin/mahnin pipeline chain (formant + deesser
                        # + body/presence EQ + loudnorm) instead of the
                        # generic chain, so job audio matches the
                        # regenerated previews. Flagships return None
                        # here and keep the generic chain, exactly as
                        # before. The 44100 Hz resample stays first.
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
                        for af_candidate in af_candidates:
                            try:
                                cmd = [
                                    ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error",
                                    "-protocol_whitelist", "file,pipe",
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

                    # Batch DI: the success proves this token covered
                    # the space's admission price — update its
                    # last-known balance for the balance-ordered walk.
                    VoxCPMEngine._record_success_balance(space_name, active_token)
                    self._log(f"✅ VoxCPM 2 Success [{tok_masked}]: {out_path.name} ({out_path.stat().st_size} bytes)")
                    return out_path
                else:
                    raise RuntimeError(f"VoxCPM 2 returned empty result from space '{space_name}'")

            except Exception as e_pair:
                last_err = e_pair
                err_lower = str(e_pair).lower()
                # Quota-class failures for THIS token's account: ZeroGPU
                # quota text, or an HF 402 Payment Required response
                # (fix 2026-10-29: 402s carry none of the quota words, so
                # they previously recorded no cooldown and the exhausted
                # account kept being picked early on later calls).
                quota_kind = _quota_failure_kind(err_lower)
                if quota_kind is not None:
                    quota_failures += 1
                if quota_kind == "gate":
                    # Batch AL: the admission-gate rejection is
                    # space-specific, so the token correctly stays OUT
                    # of the global cooldown (pre-AL behaviour) — but
                    # the (space, token) pair is now remembered for the
                    # rest of this job instead of being re-walked by
                    # every later segment.
                    if self.job_memory is not None:
                        self.job_memory.record_gate(space_name, active_token)
                    # Batch DI: the refusal text carries the exact
                    # figures — learn the token's balance and this
                    # space's admission price so the NEXT segment's
                    # walk starts from the richest token.
                    _adm = _parse_admission_refusal(str(e_pair))
                    if _adm is not None:
                        VoxCPMEngine._record_admission_refusal(
                            space_name, active_token, _adm[0], _adm[1]
                        )
                    self._log(f"⚠️ Token {tok_masked} refused at '{space_name}' admission gate (GPU-seconds requested > balance); pair memoised for this job, rotating...")
                elif quota_kind == "dead":
                    # Account quota is gone: cool the token globally as
                    # before, and mark it dead for this job on ALL
                    # spaces so the walk never tries it again today.
                    if self.job_memory is not None:
                        self.job_memory.record_dead_token(active_token)
                    if active_token:
                        self._token_cooldowns[active_token] = time.time() + 1800
                        self._schedule_cooldown_persist()
                    self._log(f"⚠️ Token {tok_masked} hit ZeroGPU quota on '{space_name}'; rotating...")
                elif active_token and _is_hf_token_auth_failure(err_lower):
                    # Audit fix E: a revoked/invalid token gets the same
                    # 30-minute cooldown as a quota-dead one — otherwise
                    # it stays in rotation and fails one pair-attempt per
                    # synthesis indefinitely.
                    self._token_cooldowns[active_token] = time.time() + 1800
                    self._schedule_cooldown_persist()
                    self._log(f"⚠️ Token {tok_masked} rejected as invalid/unauthorized on '{space_name}'; cooling down for 30m...")
                self._log(f"⚠️ VoxCPM 2 Pair ({space_name}, {tok_masked}) failed: {_format_pair_error(e_pair)}")
                time.sleep(0.5)

        # Fallback to the Edge-TTS studio persona engine — ONLY after
        # every suitable space has been tried (fix 2026-10-29).
        if not allow_fallback:
            _pure_quota = all_pairs_blocked or (
                attempts_made > 0 and quota_failures >= attempts_made
            )
            # Batch DR: a PAID walk cut by the failover-budget
            # deadline after at least one quota-class refusal is
            # park-eligible too. The pure-quota test misses the
            # dominant evening failure mode — mixed slow attempts
            # plus fragmented balances — so those segments silently
            # fell back to Edge instead of parking (2026-11-11
            # evidence; DP parks recorded: zero). Pure congestion
            # with ZERO quota refusals does not match this clause in
            # either tier, and the free tail is unchanged.
            _dr_clause = (
                _dp_paid
                and deadline_cut
                and attempts_made >= 1
                and quota_failures >= 1
            )
            if _pure_quota or _dr_clause:
                if _dr_clause and not _pure_quota:
                    self._log("🅿️ VoxCPM paid walk cut by the failover budget with quota refusals seen — treating as quota exhaustion (park-eligible)")
                # Batch AL: purely quota-class failure — a caller-side
                # retry would re-walk the same exhausted accounts, so
                # this gets its own error type (see the class docstring).
                raise VoxCPMQuotaExhaustedError(
                    f"VoxCPM 2 quota exhausted on every space/token available to this job: {last_err}"
                )
            raise RuntimeError(
                f"VoxCPM 2 speech synthesis failed after retrying all tokens and spaces: {last_err}"
            )
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


__all__ = ["VoxCPMEngine", "DEFAULT_VOXCPM_SPACE", "DEFAULT_BURMESE_CONTROL", "get_available_hf_tokens", "space_supports_clone", "space_api_endpoints", "SPACE_API_ENDPOINTS", "MAX_ATTEMPTS_PER_SPACE", "PRESET_MAP_YEYEE", "DEFAULT_VOXCPM_SPACES", "JobQuotaMemory", "VoxCPMQuotaExhaustedError"]

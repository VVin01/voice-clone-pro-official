from __future__ import annotations

import copy
import gc
import hashlib
import importlib.util
import json
import math
import os
import re
import difflib
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

try:
    from app.services.recap_engine.core import (
        PROBE_TIMEOUT_SECONDS,
        canonicalize_burmese_text,
        ffmpeg_timeout,
        normalize_burmese_orthography,
    )
except ImportError:
    from .core import PROBE_TIMEOUT_SECONDS, canonicalize_burmese_text, ffmpeg_timeout, normalize_burmese_orthography

# Audit fix E: shared Gemini pool construction + cross-job key cooldown
# memory. Defensive import: this planner is also loaded standalone by
# recap_pipeline (its own dir on sys.path), where the app package may
# not resolve — then the pool falls back to the local construction in
# _collect_gemini_key_pool and no cooldown memory is kept.
try:
    from app.services import key_rotation as _key_rotation
except Exception:  # pragma: no cover - standalone load
    try:
        _kr_spec = importlib.util.spec_from_file_location(
            "key_rotation", Path(__file__).resolve().parents[1] / "key_rotation.py"
        )
        if _kr_spec is None or _kr_spec.loader is None:
            raise ImportError("key_rotation spec unavailable")
        _key_rotation = importlib.util.module_from_spec(_kr_spec)
        _kr_spec.loader.exec_module(_key_rotation)
    except Exception:
        _key_rotation = None  # type: ignore[assignment]

try:
    from app.services import speed as _speed_mod
except Exception:  # pragma: no cover - standalone load
    try:
        _sp_spec = importlib.util.spec_from_file_location(
            "speed", Path(__file__).resolve().parents[1] / "speed.py"
        )
        if _sp_spec is None or _sp_spec.loader is None:
            raise ImportError("speed spec unavailable")
        _speed_mod = importlib.util.module_from_spec(_sp_spec)
        _sp_spec.loader.exec_module(_speed_mod)
    except Exception:
        _speed_mod = None  # type: ignore[assignment]

try:
    from app.services.queue import MEASURED_CHARS_PER_SECOND as _MEASURED_CPS
except Exception:  # pragma: no cover - standalone load without SQLAlchemy/FastAPI
    _MEASURED_CPS = 17.6

MEASURED_CHARS_PER_SECOND: float = float(_MEASURED_CPS)
DEFAULT_SPEED: str = str(getattr(_speed_mod, "DEFAULT_SPEED", "+12%") or "+12%")
DEFAULT_MAX_VIDEO_SLOW: float = 1.20

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int, str], None]
# Batch AJ / L21: this module's ffprobe/ffmpeg calls may only open
# local files and pipes — never network protocols (same constant as
# core.MEDIA_PROTOCOL_WHITELIST; duplicated because the planner also
# loads standalone with flat imports).
MEDIA_PROTOCOL_WHITELIST = "file,pipe"
PLANNER_MODE = "timestamp_translation_en_to_mm_narrator_v3"
TRANSLATION_DIRECTION = "en_to_mm"
# Whole-sentence lines like VOXCPM recap (2026-10-12): a line ends at a sentence end (or a clause after 6.5 s,
# a breath after 10 s, at most 24 s). The old 4-6 s lines cut 20-35% of sentences in two, and Gemini then told a
# sentence whole in its first line and said it again (or slid every next line one early). A job started with
# the old lines keeps them (its translation and voices stay).
SEGMENTATION_PROFILE = "v33_sentence_locked_6p5_10_24"
SHORT_SEGMENTATION_PROFILE = "v23_v2_local_sync_4p2_5p6"
SENTENCE_UNIT_TARGET = 6.5      # after this a clause mark (, ; :) may end the line
SENTENCE_UNIT_BREATH = 10.0     # after this a 0.35 s pause may end it
SENTENCE_UNIT_HARD = 24.0       # never longer
SENTENCE_UNIT_GAP = 1.5         # a pause this long always ends a line
# Original V2 visual boundary: first frame to last frame, every gap between two
# narration units split at its midpoint.
BOUNDARY_PROFILE = "v2_full_coverage_midpoint"
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "auto")


def normalize_source_language(value: Any) -> str:
    """Normalise a user-picked source language to a Whisper code.

    Returns "" for auto-detect ("", "auto", "detect", "none", None) so
    callers can fall back to the WHISPER_LANGUAGE env default. Raises
    ValueError on anything that is not a plausible 2-3 letter code —
    failing fast here beats a silent auto-detect surprise mid-job.
    """
    code = str(value or "").strip().lower()
    if code in ("", "auto", "detect", "automatic", "none"):
        return ""
    if not re.fullmatch(r"[a-z]{2,3}", code):
        raise ValueError(
            f"Unsupported source language '{value}': use 'auto' or a Whisper language code (e.g. en, zh, ko, th, ja)"
        )
    return code


GEMINI_MODEL_FALLBACK_CHAIN = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-flash-lite-latest",
    "gemini-3.8-flash",
]

DEPRECATED_GEMINI_MODELS = {
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash",
    "gemini-1.5-pro",
}

# Models deferred to the END of the chain as slow last resorts (see
# _configure_gemini_key_pool). Batch U: rotation never STARTS on one of
# these, and failover reaches them only after every other model.
SLOW_TAIL_GEMINI_MODELS = ("gemini-3.8-flash", "gemini-3.7-flash")

# Batch U (2026-11-04): per-call STARTING-model rotation counters.
# Process-level and in-memory only (the same lifetime as KeyHealth):
# each logical planner call advances its tier's counter once to pick
# the chain position it STARTS from — "full" alternates between the
# two full models (translation + audit calls, whose output quality is
# model-sensitive), "wide" cycles across every non-tail position
# (story-context calls). The admin-configured chain order still
# defines the failover priority AFTER the starting point, and the
# slow tail stays last-resort only. This spreads load across the
# models' separate per-project quota buckets instead of draining the
# primary's bucket first.
_MODEL_ROTATION_LOCK = threading.Lock()
_MODEL_ROTATION_COUNTERS: dict[str, int] = {"full": 0, "wide": 0}


def _next_model_rotation(tier: str) -> int:
    with _MODEL_ROTATION_LOCK:
        value = _MODEL_ROTATION_COUNTERS[tier]
        _MODEL_ROTATION_COUNTERS[tier] = value + 1
        return value


def _reset_model_rotation() -> None:
    """Restart both rotation counters (tests; process state only)."""
    with _MODEL_ROTATION_LOCK:
        for tier in _MODEL_ROTATION_COUNTERS:
            _MODEL_ROTATION_COUNTERS[tier] = 0


class GeminiKeyPoolExhaustedError(RuntimeError):
    """Every configured Gemini model/key fallback was exhausted."""


def _is_gemini_key_rejection(exc: BaseException) -> bool:
    """Errors that should advance to the next key inside the SAME model."""
    text = str(exc).lower()
    return (
        "monthly spending cap" in text
        or "project-spend-caps" in text
        or "resource_exhausted" in text
        or "api_key_invalid" in text
        or "api key not valid" in text
        or "unauthenticated" in text
        or "permission_denied" in text
        or "permission denied" in text
        or bool(re.search(r"\b(?:401|403|429)\b", text))
    )


def _is_gemini_transient_model_error(exc: BaseException) -> bool:
    """Errors that should skip immediately to the NEXT model, restarting at key #1."""
    text = str(exc).lower()
    markers = (
        "503",
        "504",
        "500",
        "404",
        "not_found",
        "not found",
        "no longer available",
        "unavailable",
        "service unavailable",
        "high demand",
        "overloaded",
        "deadline_exceeded",
        "deadline exceeded",
        "timeout",
        "timed out",
        "server disconnected",
        "disconnected",
        "connection reset",
        "remoteprotocolerror",
        "connection error",
        "connection closed",
        "remote protocol",
        "read timed out",
    )
    return any(marker in text for marker in markers)


# Batch AJ / L20: hard bounds for the Gemini worst cases.
# _gemini_json's failover switches (key rotation, model advance)
# deliberately do not consume validation retries, so under a total
# outage one logical call used to walk the whole key pool x the
# whole model chain (~10 keys x ~5 models = ~50 HTTP attempts, each
# with a 25-90 s timeout — a ~30 min worst case). The total-attempt
# budget below caps every logical call; the success path and the
# rotation/failover ORDER are unchanged (the cap only bites once
# 24 attempts have already failed).
GEMINI_JSON_MAX_HTTP_ATTEMPTS = 24
# translate_batch's split-in-half fallback terminates on its own
# (batch sizes halve and <=6-item batches never split), but the
# depth is now explicit: batches are at most 40 items, so depth 3
# (40 -> 20 -> 10 -> 5) already covers every legitimate split.
TRANSLATE_BATCH_MAX_SPLIT_DEPTH = 3


def _gemini_key_pool_message(pool_size: int, model_count: int) -> str:
    return (
        f"Gemini fallback chain exhausted — {model_count} model(s) × up to {pool_size} key(s).\n\n"
        "429/quota/auth errors exhausted the configured key pool and no fallback model remains, "
        "or the final model returned a non-recoverable service error.\n"
        "ပြီးရင် START ကိုပြန်နှိပ်ပါ။ Gemini transcript နဲ့ ပြီးထားတဲ့ translation checkpoint ကို "
        "ပြန်သုံးပြီး ကျန်တဲ့ batch ကနေ ဆက်လုပ်ပါမယ်။"
    )


def _collect_gemini_key_pool(env: dict) -> list[tuple[str, str]]:
    """Gemini key pool for one job: the shared UNION of every source
    (audit fix E — app.services.key_rotation.collect_gemini_key_pool,
    also used by the Gemini TTS engine and the group admin).

    The local fallback below is this planner's original inline
    construction, kept for standalone loads where the shared module is
    unavailable; order and dedupe semantics are identical.
    """
    if _key_rotation is not None:
        return _key_rotation.collect_gemini_key_pool(env)

    key_pool: list[tuple[str, str]] = []
    seen_keys: set[str] = set()

    # 1. Every GEMINI_IMAGE_PROJECT_<N>_KEY in numeric order
    numbers = sorted(
        int(m.group(1)) for m in (re.fullmatch(r"GEMINI_IMAGE_PROJECT_(\d+)_KEY", str(k or "").strip())
                                  for k in env) if m)
    for number in numbers:
        label = f"GEMINI_IMAGE_PROJECT_{number}_KEY"
        key = str(env.get(label) or "").strip()
        if key and key not in seen_keys:
            key_pool.append((label, key))
            seen_keys.add(key)

    # 2. Every GEMINI_API_KEY_<N> in numeric order
    key_numbers = sorted(
        int(m.group(1)) for m in (re.fullmatch(r"GEMINI_API_KEY_(\d+)", str(k or "").strip())
                              for k in env) if m)
    for number in key_numbers:
        label = f"GEMINI_API_KEY_{number}"
        key = str(env.get(label) or "").strip()
        if key and key not in seen_keys:
            key_pool.append((label, key))
            seen_keys.add(key)

    # 3. Comma-separated GEMINI_API_KEYS
    csv_keys = str(env.get("GEMINI_API_KEYS") or "").strip()
    if csv_keys:
        for idx, raw_k in enumerate(csv_keys.split(",")):
            k_clean = raw_k.strip()
            if k_clean and k_clean not in seen_keys:
                key_pool.append((f"GEMINI_API_KEYS[{idx+1}]", k_clean))
                seen_keys.add(k_clean)

    # 4. Standard single GEMINI_API_KEY fallback
    single_key = str(env.get("GEMINI_API_KEY") or "").strip()
    if single_key and single_key not in seen_keys:
        key_pool.append(("GEMINI_API_KEY", single_key))
        seen_keys.add(single_key)

    return key_pool


def _configure_gemini_key_pool(
    ai: Any,
    key_pool: list[tuple[str, str]],
    *,
    client_factory: Callable[..., Any],
    log: LogFn,
    request_timeout_seconds: int = 120,
    model_chain: list[str] | None = None,
) -> None:
    """Attach ordered model-major/key-minor Gemini fallback state.

    Policy:
      * 429/auth/quota -> next key in the current model.
      * After every key is rejected -> next model and restart from key #1.
      * 503/high-demand/timeout -> next model immediately and restart from key #1.
      * Model/key switches retry the exact same request without consuming validation retries.
      * Batch U: each logical call first re-points the chain at a rotated
        STARTING model (ai._recap_begin_gemini_call, driven by _gemini_json's
        rotation_tier); failover then walks the call's visit order — every
        remaining model stays reachable and the slow tail is visited last.
    """
    if not key_pool:
        return

    # The key-pool ordering (audit fix E + Batch O + Batch DN) runs
    # AFTER the model chain below is finalised: Batch DN orders the
    # pool by remaining quota for the chain's head model, so the
    # chain must exist first. See the block following the chain
    # construction.
    try:
        from app.config import settings
        primary = getattr(settings, "gemini_primary_model", None)
        fb1 = getattr(settings, "gemini_fallback_1", None)
        fb2 = getattr(settings, "gemini_fallback_2", None)
        fb3 = getattr(settings, "gemini_fallback_3", None)
    except Exception:
        primary = fb1 = fb2 = fb3 = None

    primary = primary or os.getenv("GEMINI_PRIMARY_MODEL") or os.getenv("GEMINI_TEXT_MODEL") or "gemini-3.6-flash"
    fb1 = fb1 or os.getenv("GEMINI_FALLBACK_1") or "gemini-3.5-flash"
    fb2 = fb2 or os.getenv("GEMINI_FALLBACK_2") or "gemini-3.5-flash-lite"
    fb3 = fb3 or os.getenv("GEMINI_FALLBACK_3") or "gemini-flash-lite-latest"

    # Automatically replace retired 2.5/2.0/1.5 models or slow-hanging default 3.8/3.7 at the head of the chain
    raw_chain = [primary, fb1, fb2, fb3] + (model_chain or GEMINI_MODEL_FALLBACK_CHAIN)
    models = []
    deferred_slow = []
    for x in raw_chain:
        clean = str(x or "").strip()
        if not clean or clean in DEPRECATED_GEMINI_MODELS:
            continue
        if clean in SLOW_TAIL_GEMINI_MODELS:
            if clean not in deferred_slow:
                deferred_slow.append(clean)
            continue
        if clean not in models:
            models.append(clean)
    for slow_m in deferred_slow:
        if slow_m not in models:
            models.append(slow_m)

    if not models:
        models = list(GEMINI_MODEL_FALLBACK_CHAIN)

    # Audit fix E: keys cooled down by quota/auth failures in earlier
    # jobs start LAST for this job. Relative order is preserved and the
    # pool never shrinks, so the model/key fallback chain below (and
    # the env sync in activate()) behaves exactly as before — the
    # cooldown only changes which key a job starts from.
    # Batch O (DECISION backlog item 3): the starting key also ROTATES
    # per job — consecutive jobs begin at different keys instead of
    # every job hammering key #1 first. The ordering omits cooled
    # keys, so any it leaves out are appended at the tail in their
    # original relative order: still last-resort candidates for this
    # job, never silently dropped.
    # Batch DN: the per-job order is now quota-aware for the chain's
    # HEAD model via order_for_model — richest remaining quota first,
    # rotation preserved inside ties. The pool order stays FIXED for
    # the whole job (Batch U pins that every logical call restarts at
    # Key 1 of this order; only the starting MODEL rotates per call),
    # and _gemini_json records each success against the model that
    # actually served, so the NEXT job's order reflects real usage
    # across the whole chain.
    if _key_rotation is not None:
        try:
            _pool_keys = [key for _label, key in key_pool]
            _ordered = _key_rotation.health_for("gemini").order_for_model(
                _pool_keys, models[0] if models else ""
            )
            _seen = set(_ordered)
            _ordered = _ordered + [k for k in _pool_keys if k not in _seen]
            if _ordered != _pool_keys:
                _rank = {key: idx for idx, key in enumerate(_ordered)}
                key_pool = sorted(key_pool, key=lambda item: _rank[item[1]])
        except Exception:
            pass

    model_index = 0
    key_index = 0
    # Batch U: the current logical call's model visit order (indices
    # into `models`). Defaults to the natural chain order; begin_call()
    # rebuilds it per call around the rotated starting model.
    visit_order = list(range(len(models)))
    visit_pos = 0
    # Batch AL-P: per-request timeout trimmed 90 s → 60 s — with
    # parallel translate batches a hung request must fail over sooner.
    timeout_seconds = max(25, min(int(request_timeout_seconds), 60))
    http_options = {
        "timeout": timeout_seconds * 1000,
        "retryOptions": {"attempts": 1},
    }

    def build_client(key: str) -> Any:
        ai._enable_proxy()
        try:
            return client_factory(api_key=key, http_options=http_options)
        finally:
            ai._disable_proxy()

    def activate(mi: int, ki: int, *, reason: str | None = None) -> None:
        nonlocal model_index, key_index
        model_index = mi
        key_index = ki
        label, key = key_pool[key_index]
        model = models[model_index]
        ai.client = build_client(key)
        ai.text_model = model
        os.environ["GEMINI_API_KEY"] = key
        os.environ["GEMINI_TEXT_MODEL"] = model
        ai.api_key = key
        ai._recap_active_key_label = label
        ai._recap_active_model = model
        ai._recap_model_index = model_index
        ai._recap_key_index = key_index
        if reason:
            log(
                f"🔄 Gemini fallback ({reason}) → {model} | {label} "
                f"(model {model_index + 1}/{len(models)}, key {key_index + 1}/{len(key_pool)})"
            )

    # Replace the AIService client before the first planner request.
    activate(0, 0)

    def _cool_current_key() -> None:
        # Audit fix E: remember the key being abandoned for a quota/auth
        # rejection across jobs, so the next job deprioritizes it until
        # the cooldown expires instead of re-trying it first.
        if _key_rotation is not None:
            try:
                _key_rotation.health_for("gemini").mark_failure(key_pool[key_index][1])
            except Exception:
                pass

    def rotate_key() -> bool:
        _cool_current_key()
        next_key = key_index + 1
        if next_key >= len(key_pool):
            return False
        activate(model_index, next_key, reason="next key")
        return True

    def advance_model(reason: str = "next model") -> bool:
        nonlocal visit_pos
        next_pos = visit_pos + 1
        if next_pos >= len(visit_order):
            return False
        visit_pos = next_pos
        # Required behavior: every model starts again at Key 1.
        activate(visit_order[visit_pos], 0, reason=reason)
        return True

    def advance_after_key_pool() -> bool:
        _cool_current_key()
        return advance_model("key pool exhausted")

    # Batch U: the slow tail sits at the end of `models` by
    # construction (deferred above). Rotation never STARTS on the
    # tail; a call's visit order reaches it only after every other
    # position, so it stays last-resort only.
    tail_start = len(models)
    while tail_start > 0 and models[tail_start - 1] in SLOW_TAIL_GEMINI_MODELS:
        tail_start -= 1
    body_positions = list(range(tail_start))
    tail_positions = list(range(tail_start, len(models)))

    def _visit_order_for(tier: str | None) -> list[int]:
        if not body_positions:
            return list(range(len(models)))
        if tier == "full":
            # Rotate the start between the two full models only; the
            # other full model follows, then the lite models in chain
            # order, then the tail.
            head = body_positions[:2]
            start = head[_next_model_rotation("full") % len(head)]
            rest = [p for p in head if p != start] + body_positions[len(head):]
            return [start] + rest + tail_positions
        if tier == "wide":
            # Rotate the start across every non-tail position; the
            # walk wraps around the body and ends at the tail.
            start = body_positions[_next_model_rotation("wide") % len(body_positions)]
            k = body_positions.index(start)
            return body_positions[k:] + body_positions[:k] + tail_positions
        return body_positions + tail_positions

    def begin_call(tier: str | None = None) -> None:
        """Point the chain at this logical call's rotated starting
        model (Batch U) and restart it at Key 1. Failover afterwards
        walks the visit order, so every model remains reachable from
        either start and the slow tail is always last."""
        nonlocal visit_order, visit_pos
        visit_order = _visit_order_for(tier)
        visit_pos = 0
        activate(visit_order[0], 0)

    ai._recap_active_key_label = key_pool[0][0]
    ai._recap_active_model = models[0]
    ai._recap_key_pool_size = len(key_pool)
    ai._recap_model_pool_size = len(models)
    ai._recap_model_chain = list(models)
    ai._recap_begin_gemini_call = begin_call
    ai._recap_rotate_gemini_key = rotate_key
    ai._recap_advance_gemini_model = advance_model
    ai._recap_advance_after_key_pool = advance_after_key_pool
    log(f"🔑 Gemini key pool ready — {len(key_pool)} key(s); active: {key_pool[0][0]}")
    log("🤖 Gemini fallback chain: " + " → ".join(models))
    log(f"⏱️ Gemini request hard timeout: {timeout_seconds}s; planner retries remain enabled")


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Python module load မရပါ: {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@contextmanager
def _pushd(path: Path):
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def _probe_video_duration(ffprobe: str, source: Path) -> float:
    """Use the primary VIDEO stream as the source-of-truth duration.

    Some downloaded/trimmed MP4s contain a valid ~13 minute video stream but an
    accidentally long audio stream (for example ~44 minutes). format.duration then
    follows the long audio tail and would transcribe audio that has no video.
    Recap sync is visual, so v:0 duration is authoritative.
    """
    cmd = [
        ffprobe, "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(source),
    ]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=PROBE_TIMEOUT_SECONDS)
    if p.returncode == 0:
        raw = (p.stdout or "").strip().splitlines()
        for line in raw:
            try:
                value = float(line.strip())
                if value > 0.05:
                    return value
            except Exception:
                pass

    # Fallback for unusual files where stream.duration is unavailable.
    p2 = subprocess.run(
        [ffprobe, "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    if p2.returncode != 0:
        raise RuntimeError("ffprobe duration failed:\n" + (p2.stderr or p.stderr)[-3000:])
    return float((p2.stdout or "0").strip())


def _probe_audio_duration(ffprobe: str, source: Path) -> float | None:
    p = subprocess.run(
        [ffprobe, "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST, "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(source)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    if p.returncode != 0:
        return None
    for line in (p.stdout or "").strip().splitlines():
        try:
            value = float(line.strip())
            if value > 0.0:
                return value
        except Exception:
            pass
    return None


# Real speech peaks far above this; -91 dB was observed on a video's muted end credits.


def _prepare_whisper_audio(ffmpeg: str, source: Path, job_dir: Path, video_duration: float, log: LogFn) -> Path:
    """Decode the video-length audio to a reusable 16 kHz mono WAV for local Whisper."""
    out = job_dir / "_whisper_audio_16k.wav"
    if out.is_file() and out.stat().st_size > 4096:
        log(f"♻️ Whisper trimmed audio reuse: {out.name}")
        return out
    cmd = [
        ffmpeg, "-protocol_whitelist", MEDIA_PROTOCOL_WHITELIST, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source), "-map", "0:a:0", "-vn",
        "-t", f"{video_duration:.6f}",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out),
    ]
    log(f"✂️ Preparing Whisper audio: trim to video duration {video_duration:.3f}s")
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=ffmpeg_timeout(video_duration))
    if proc.returncode != 0 or not out.is_file():
        raise RuntimeError("Whisper audio preparation failed:\n" + (proc.stderr or "")[-3000:])
    return out


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _source_fingerprint_for_mode(source: Path, planner_mode: str) -> str:
    st = source.stat()
    raw = json.dumps({
        "path": str(source.resolve()),
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "planner_mode": planner_mode,
    }, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _fmt_ts(seconds: float) -> str:
    ms = max(0, int(round(float(seconds) * 1000)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def _clean(s: Any) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _has_speakable_content(value: Any) -> bool:
    """Return True when text contains at least one Unicode letter/number.

    This rejects punctuation-only placeholders such as ``...`` or ``…`` before
    they can reach TTS. ``str.isalnum`` treats Burmese letters as content too.
    """
    text = _clean(value)
    return any(ch.isalnum() for ch in text)


MYANMAR_HOMOGLYPHS: dict[str, str] = {
    "\u1293": "\u1014\u102c",  # Ethiopic NAA -> Burmese 'နာ'
    "\u1290": "\u1014",        # Ethiopic NA -> Burmese 'န'
    "\u1260": "\u1018",        # Ethiopic BA -> Burmese 'ဗ'
    "\u1261": "\u1018\u102f",   # Ethiopic BU -> Burmese 'ဗု'
    "\u1220": "\u101e",        # Ethiopic SA -> Burmese 'သ'
    "\u1223": "\u101e\u102c",  # Ethiopic SAA -> Burmese 'သာ'
    "\u12d0": "\u1025",        # Ethiopic A -> Burmese 'ဥ'
    "\u10dd": "\u101d",        # Georgian ON -> Burmese 'ဝ'
    "\u10d8": "\u102d",        # Georgian INI -> Burmese 'ိ'
    "\u10d0": "\u1001",        # Georgian ANI -> Burmese 'ခ'
    "\u10d2": "\u101e",        # Georgian GANI -> Burmese 'သ'
    "\u10e7": "\u101c",        # Georgian QARI -> Burmese 'လ'
    "\u10e3": "\u1015",        # Georgian UNI -> Burmese 'ပ'
    "\u10e9": "\u101b",        # Georgian CHINI -> Burmese 'ရ'
}


def normalize_burmese_homoglyphs(text: Any) -> str:
    """Normalize foreign look-alike homoglyphs (e.g. Ethiopic \u1293) to authentic Myanmar Unicode."""
    s = str(text or "")
    if not s:
        return ""
    for k, v in MYANMAR_HOMOGLYPHS.items():
        if k in s:
            s = s.replace(k, v)
    return s


def _sanitize_translation_text(value: Any) -> str:
    """Strip known Gemini audit/meta wrappers while preserving translated speech.

    V26 exposed a rare audit leak such as::

        [Unused filler adjustment skipped - translated faithfully: Reinvent the pyramid game.]

    The bracketed prefix is tool/audit metadata and must never be spoken by TTS.  When
    that exact wrapper appears, keep only the faithful translation inside it.
    """
    text = _clean(value)
    # Full canonicalization (mark-order repair + orthography), not just
    # spelling: Gemini output reaches TTS and subtitles from here, and a
    # mis-ordered syllable must not survive sanitizing.
    text = canonicalize_burmese_text(text)
    match = re.fullmatch(
        r"\[\s*Unused filler adjustment skipped\s*[-–—:]\s*translated faithfully\s*:\s*(.*?)\s*\]",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        inner = _clean(match.group(1))
        if inner:
            return canonicalize_burmese_text(inner)
    return text


def _foreign_letters(text: Any) -> list[str]:
    """Letters that are neither Myanmar nor Latin outside ( ): Gemini sometimes writes Georgian
    letters inside a Burmese word (ဧည့်ခန်း came back as "მისခန်း"), which the voice cannot read."""
    norm = normalize_burmese_homoglyphs(text)
    bare = re.sub(r"\([^()]*\)", "", norm)
    return list(dict.fromkeys(
        ch for ch in bare
        if ch.isalpha() and not (0x1000 <= ord(ch) <= 0x109F or 0xA9E0 <= ord(ch) <= 0xA9FF
                                 or 0xAA60 <= ord(ch) <= 0xAA7F or ord(ch) <= 0x024F
                                 or 0x1E00 <= ord(ch) <= 0x1EFF)
    ))


def _drop_foreign_letters(items: list[dict[str, Any]], log: LogFn) -> list[dict[str, Any]]:
    """Last resort after the re-asks: normalize homoglyphs, drop stray letters and name the line for review."""
    out = []
    for item in items:
        trans = normalize_burmese_homoglyphs(item.get("translation"))
        letters = _foreign_letters(trans)
        if letters:
            text = _clean("".join(ch for ch in trans if ch not in letters))
            log(f"⚠️ ID {int(item['id']):05d}: removed letters of another script ({' '.join(letters)}); check this line")
            item = {**item, "translation": text}
        else:
            item = {**item, "translation": trans}
        out.append(item)
    return out


# Burmese narration is read by a Burmese voice, so every word is in Burmese script (2026-10-12): English words
# left in the Burmese are read in an English accent, and a line mostly in English can come back from Edge TTS
# with no audio, which stopped the render.
_LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'\u2019-]*[A-Za-z]")


def _latin_words(text: Any) -> list[str]:
    """English (Latin) words of 2+ letters outside ( ) in the Burmese narration."""
    bare = re.sub(r"\([^()]*\)", "", str(text or ""))
    return list(dict.fromkeys(_LATIN_WORD_RE.findall(bare)))


# A sentence split across two IDs was translated whole in the first one and then said again, or every ID after
# it said the NEXT ID's words (2026-10-12; 10-50 lines per video in the full tool). Each check re-asks the batch.
_NOTE_RE = re.compile(r"(?i)\b(?:context\s+carried|continu(?:ing|ed|es)\s+(?:from\s+)?id|carried\s+by\s+id|"
                      r"(?:previous|next|same)\s+id|id\s*\d{2,})\b")


def _note_text(text: Any) -> bool:
    """A translator's note (about IDs / context) instead of narration; also a line with no Burmese word outside ( )
    such as "(ရှေ့စာကြောင်း၏ အဆက်)" ("continuation of the previous sentence"), which the voice would read out."""
    value = str(text or "")
    bare = re.sub(r"\([^()]*\)", "", value)
    return bool(_NOTE_RE.search(value)) or not re.search(r"[\u1000-\u109F]", bare)


def _repeat_words(text: Any) -> list[str]:
    return [w for w in re.split(r"[\s၊။\"'“”‘’,.!?()\-–:]+", str(text or "")) if w]


def _repeated_tail_share(left: Any, right: Any) -> float:
    """How much of `right` the end of `left` already says (0-1). Spaced text (Burmese, English): by words,
    right needs 3+ words. Text with few spaces (Thai): by the longest shared run of letters, 10+ letters."""
    lw, rw = _repeat_words(left), _repeat_words(right)
    r_letters = re.sub(r"\s+", "", str(right or ""))
    if rw and len(r_letters) / len(rw) > 12:          # Thai-like: words are not split by spaces
        if len(r_letters) < 10:
            return 0.0
        tail = re.sub(r"\s+", "", str(left or ""))[-(len(r_letters) + 15):]
        match = difflib.SequenceMatcher(None, tail, r_letters, autojunk=False).find_longest_match(
            0, len(tail), 0, len(r_letters))
        return match.size / len(r_letters) if match.size >= 10 else 0.0
    if len(rw) < 3:
        return 0.0
    tail = set(lw[-(len(rw) + 3):])
    return sum(1 for w in rw if w in tail) / len(rw)


def find_repeated_next_lines(items: list[dict[str, Any]],
                             previous_context: list[dict[str, Any]] | None = None) -> list[tuple[int, int]]:
    """(left id, right id) where the end of the left translation already says the right one, although the
    sources do not repeat (a real repetition in the source is kept)."""
    sequence = list(previous_context[-1:] if previous_context else []) + list(items)
    found = []
    for left, right in zip(sequence, sequence[1:]):
        if (_repeated_tail_share(left.get("translation"), right.get("translation")) >= 0.6
                and _repeated_tail_share(left.get("source_text"), right.get("source_text")) < 0.5):
            found.append((int(left["id"]), int(right["id"])))
    return found


_DUP_STRIP_RE = re.compile(r"[\s၊။\"'“”‘’,.!?()\-–:—…]+")


def _char_grams(text: Any, n: int = 3) -> set[str]:
    value = _DUP_STRIP_RE.sub("", str(text or ""))
    return {value[i:i + n] for i in range(len(value) - n + 1)}


def find_near_duplicates(items: list[dict[str, Any]], previous_context: list[dict[str, Any]] | None = None, *,
                         span: int = 2, min_share: float = 0.6) -> list[tuple[int, int]]:
    """(a, b): lines at most `span` IDs apart whose translations say much the same while their sources do not."""
    prev = list(previous_context[-span:]) if previous_context else []
    seq = prev + list(items)
    found: list[tuple[int, int]] = []
    for i in range(len(seq)):
        ga = _char_grams(seq[i].get("translation"))
        if len(ga) < 12:
            continue
        for k in range(1, span + 1):
            j = i + k
            if j >= len(seq) or j < len(prev):
                continue
            gb = _char_grams(seq[j].get("translation"))
            if len(gb) < 12 or len(ga & gb) / min(len(ga), len(gb)) < min_share:
                continue
            sa, sb = _char_grams(seq[i].get("source_text")), _char_grams(seq[j].get("source_text"))
            if sa and sb and len(sa & sb) / min(len(sa), len(sb)) >= 0.5:
                continue
            found.append((int(seq[i]["id"]), int(seq[j]["id"])))
    return found


def _letter_count(text: Any) -> int:
    """Letters and digits, vowel signs left out: close to a syllable count in Burmese, Hindi and Thai alike
    (counting the signs too missed 2 of 8 slides read by hand)."""
    return max(1, sum(1 for ch in str(text or "") if unicodedata.category(ch)[0] in "LN"))


def _slid_runs(items: list[dict[str, Any]], next_source: Any = None, *, min_run: int = 4,
               min_items: int = 8) -> list[tuple[int, int]]:
    """(first ID, last ID) of runs whose translations carry the next ID's source line."""
    if len(items) < min_items:
        return []
    src = [_letter_count(x.get("source_text")) for x in items]
    src.append(_letter_count(next_source) if _clean(next_source) else 0)
    tr = [_letter_count(x.get("translation")) for x in items]
    ratios = sorted(t / s for t, s in zip(tr, src))
    ratio = ratios[len(ratios) // 2]
    own = [abs(math.log(t / (ratio * s))) for t, s in zip(tr, src)]
    nxt = [abs(math.log(t / (ratio * src[i + 1]))) if src[i + 1] else float("inf") for i, t in enumerate(tr)]
    runs: list[tuple[int, int]] = []
    i = 0
    while i < len(items):
        j = i
        while j < len(items) and nxt[j] < own[j]:
            j += 1
        if j - i >= min_run and sum(nxt[i:j]) < 0.45 * sum(own[i:j]) and sum(own[i:j]) / (j - i) > 0.3:
            runs.append((int(items[i]["id"]), int(items[j - 1]["id"])))
        i = max(j, i + 1)
    return runs


def _assert_speakable_translation_items(items: list[dict[str, Any]]) -> None:
    for item in items:
        source = _clean(item.get("source_text"))
        translation = _sanitize_translation_text(item.get("translation"))
        if _has_speakable_content(source) and not _has_speakable_content(translation):
            raise ValueError(
                f"Translation ID {item.get('id')} contains no speakable words "
                f"(got {translation!r}); placeholders/ellipsis are not allowed"
            )


def _pathological_repetition_segment_ids(transcript: dict[str, Any]) -> set[int]:
    """Detect unmistakable local-Whisper repetition hallucinations.

    This is deliberately conservative: only long raw ASR segments with extreme
    repetition are flagged.  It is mainly a safety net for cases such as a
    20-30 second segment made almost entirely of ``我...我...`` or ``冲冲冲...``.
    Ordinary stutters/repeated dialogue are not long or repetitive enough to trip it.
    """
    backend = str(transcript.get("backend") or "").lower()
    if "whisper" not in backend:
        return set()
    bad: set[int] = set()
    for index, seg in enumerate(transcript.get("segments") or []):
        if not isinstance(seg, dict):
            continue
        try:
            start = float(seg.get("start", 0.0) or 0.0)
            end = float(seg.get("end", start) or start)
        except Exception:
            continue
        duration = max(0.0, end - start)
        if duration < 4.0:
            continue
        text = _clean(seg.get("text"))
        chars = [ch.casefold() for ch in text if ch.isalnum()]
        if len(chars) < 24:
            continue
        counts: dict[str, int] = {}
        for ch in chars:
            counts[ch] = counts.get(ch, 0) + 1
        top_ratio = (max(counts.values()) / len(chars)) if counts else 0.0
        unique_ratio = (len(counts) / len(chars)) if chars else 1.0
        if top_ratio >= 0.72 and unique_ratio <= 0.18:
            try:
                sid = int(seg.get("id", index))
            except Exception:
                sid = index
            bad.add(sid)
    return bad


def _unit_is_low_information_repetition(unit: dict[str, Any]) -> bool:
    text = _clean(unit.get("source_text"))
    chars = [ch.casefold() for ch in text if ch.isalnum()]
    if not chars:
        return True
    if len(set(chars)) <= 1:
        return True
    tokens = []
    for token in text.split():
        cleaned = "".join(ch.casefold() for ch in token if ch.isalnum())
        if cleaned:
            tokens.append(cleaned)
    if len(tokens) >= 4:
        counts: dict[str, int] = {}
        for token in tokens:
            counts[token] = counts.get(token, 0) + 1
        top_ratio = max(counts.values()) / len(tokens)
        unique_ratio = len(counts) / len(tokens)
        if top_ratio >= 0.75 and unique_ratio <= 0.25:
            return True
    return False


# Not English (2026-10-13, a user's second video): the English-only Whisper (small.en) does not stop on Hindi,
# Chinese, … speech - it writes a few English phrases again and again ("I'm not going to do it." ×40, "We are all"
# ×N) and most of the speech is lost, so the video came out with wrong narration. In English transcripts 0-0.1% of
# the lines repeat one of the 5 before them; in two non-English videos 65% and 77% did.
NOT_ENGLISH_REPEAT_SHARE = 0.25
NOT_ENGLISH_MIN_LINES = 10


def _repeated_line_share(transcript: dict[str, Any]) -> tuple[float, int, list[tuple[str, int]]]:
    """(share of lines of 3+ words that say the same as one of the 5 lines before them, lines counted,
    the most repeated lines with their counts)."""
    lines = []
    for seg in transcript.get("segments") or []:
        if isinstance(seg, dict):
            text = re.sub(r"[^a-z0-9 ]", "", _clean(seg.get("text")).lower()).strip()
            text = re.sub(r"\s+", " ", text)
            if len(text.split()) >= 3:
                lines.append(text)
    if not lines:
        return 0.0, 0, []
    repeated = sum(1 for i, text in enumerate(lines) if text in lines[max(0, i - 5):i])
    counts: dict[str, int] = {}
    for text in lines:
        counts[text] = counts.get(text, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
    return repeated / len(lines), len(lines), top


def _check_english_speech(transcript: dict[str, Any], transcript_name: str) -> None:
    """Stop before any Gemini request when Whisper clearly hallucinated repeating lines."""
    detected = str(transcript.get("language") or "").lower().strip()
    if detected and detected not in ("en", "english", "auto", ""):
        # Legitimate non-English speech (e.g. Chinese zh, Korean ko, Japanese ja, Thai th, etc.)
        return
    share, count, top = _repeated_line_share(transcript)
    if count < NOT_ENGLISH_MIN_LINES or share < NOT_ENGLISH_REPEAT_SHARE:
        return
    examples = ", ".join(f'"{text}" ×{n}' for text, n in top if n > 1)
    raise RuntimeError(
        f"Whisper transcript ထဲတွင် ထပ်ခါထပ်ခါ စာကြောင်း {share:.0%} ဖြစ်နေပါသည် ({examples})။ "
        f"နောက်ခံဆူညံသံ သို့မဟုတ် စကားသံမကြည်လင်ခြင်း ဖြစ်နိုင်ပါသည်။"
    )


def _filter_local_whisper_artifact_units(
    units: list[dict[str, Any]],
    transcript: dict[str, Any],
    *,
    log: LogFn | None = None,
) -> list[dict[str, Any]]:
    """Remove only atomic narration rows proven to come from ASR junk.

    IDs are intentionally NOT renumbered.  Keeping stable IDs lets an existing
    timestamp_translations.json reuse every unaffected translated row while obsolete
    hallucination rows are simply ignored.
    """
    pathological_ids = _pathological_repetition_segment_ids(transcript)
    kept: list[dict[str, Any]] = []
    dropped: list[tuple[int, str]] = []
    for unit in units:
        uid = int(unit.get("id", -1))
        source = _clean(unit.get("source_text"))
        if not _has_speakable_content(source):
            dropped.append((uid, "punctuation-only source"))
            continue
        source_ids = {int(value) for value in (unit.get("source_transcript_ids") or [unit.get("source_transcript_id", -1)])}
        if source_ids and source_ids.issubset(pathological_ids) and _unit_is_low_information_repetition(unit):
            dropped.append((uid, "Whisper repetition hallucination"))
            continue
        kept.append(unit)
    if dropped and log:
        preview = ", ".join(f"{uid:05d}" for uid, _reason in dropped[:12])
        suffix = "..." if len(dropped) > 12 else ""
        log(
            f"⚠️ Local Whisper artifact guard removed {len(dropped)} non-narration unit(s): "
            f"IDs {preview}{suffix}. Existing good translation cache remains reusable."
        )
        if pathological_ids:
            seg_preview = ", ".join(str(x) for x in sorted(pathological_ids)[:10])
            log(f"   ↳ pathological raw Whisper segment(s): {seg_preview}")
    return kept


def _plan_has_speakable_narration(plan: Any) -> bool:
    if not isinstance(plan, dict):
        return False
    segments = plan.get("segments")
    if not isinstance(segments, list) or not segments:
        return False
    return all(
        isinstance(seg, dict) and _has_speakable_content(seg.get("narration"))
        for seg in segments
    )


_COMMON_ABBREVIATIONS = {
    "mr.", "mrs.", "ms.", "dr.", "prof.", "sr.", "jr.", "st.",
    "vs.", "etc.", "e.g.", "i.e.",
}


def _word_ends_sentence(text: str) -> bool:
    cleaned = _clean(text).lower()
    if cleaned in _COMMON_ABBREVIATIONS:
        return False
    # ASR may split a person's name as "N." + "Ben".  Treat a
    # lone Latin initial as part of the following name instead of inventing a
    # standalone translation ID.
    if re.fullmatch(r"[a-z]\.['’\"”)]?", cleaned):
        return False
    return bool(re.search(r"[.!?…။।॥。！？][\"'’”)]?$", cleaned))


def _word_ends_soft_clause(text: str) -> bool:
    return bool(re.search(r"[;,:၊；，、][\"'’”)]?$", _clean(text)))


def _fallback_word_tokens(text: str, start: float, end: float, transcript_id: int) -> list[dict[str, Any]]:
    words = re.findall(r"\S+", _clean(text))
    if not words or end <= start:
        return []
    step = (end - start) / len(words)
    return [
        {
            "text": word,
            "start": start + index * step,
            "end": start + (index + 1) * step,
            "source_transcript_id": transcript_id,
        }
        for index, word in enumerate(words)
    ]


def _transcript_word_tokens(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten timestamped transcript words while retaining measured times and source IDs."""
    tokens: list[dict[str, Any]] = []
    for raw_index, raw in enumerate(transcript.get("segments") or []):
        text = _clean(raw.get("text"))
        start = float(raw.get("start", 0.0) or 0.0)
        end = float(raw.get("end", start) or start)
        if not text or end <= start:
            continue
        transcript_id = int(raw.get("id", raw_index))
        measured: list[dict[str, Any]] = []
        for word in raw.get("words") or []:
            wt = _clean(word.get("text"))
            ws = float(word.get("start", start) or start)
            we = float(word.get("end", ws) or ws)
            if wt and we > ws:
                measured.append({
                    "text": wt,
                    "start": ws,
                    "end": we,
                    "source_transcript_id": transcript_id,
                })
        tokens.extend(measured or _fallback_word_tokens(text, start, end, transcript_id))
    tokens.sort(key=lambda item: (float(item["start"]), float(item["end"])))
    return tokens


def atomicize_timestamp_segments(
    transcript: dict[str, Any],
    *,
    max_duration: float = 5.6,
    target_duration: float = 4.2,
    max_words: int = 24,
    sentence_locked: bool = False,
) -> list[dict[str, Any]]:
    """Build V2-style local sync units from measured word timestamps.

    V23 deliberately keeps Smart Sync windows small.  The old sentence-locked planner could
    merge many raw ASR rows into 10-30 second units, so one short/long TTS result could make
    an entire scene feel late.  This profile still prefers real sentence/clause/pause
    boundaries, but it also applies a local ~4.2s target and ~5.6s hard cap.  Translation
    context is still supplied across IDs, so splitting a long sentence does not remove story
    context; it only gives Smart Sync more frequent timestamp anchors.
    """
    tokens = _transcript_word_tokens(transcript)
    units: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    target_duration = max(2.5, float(target_duration))
    max_duration = max(target_duration + 0.5, float(max_duration))
    min_natural_duration = min(1.8, target_duration * 0.4)

    def close_current(boundary: str) -> None:
        nonlocal current
        if not current:
            return
        source_ids = list(dict.fromkeys(int(word["source_transcript_id"]) for word in current))
        units.append({
            "source_transcript_id": source_ids[0],
            "source_transcript_ids": source_ids,
            "speech_start": round(float(current[0]["start"]), 3),
            "speech_end": round(float(current[-1]["end"]), 3),
            "source_text": _clean(" ".join(str(word["text"]) for word in current)),
            "semantic_boundary": boundary,
            "forced_boundary": boundary.startswith("forced"),
        })
        current = []

    for index, token in enumerate(tokens):
        current.append(token)
        duration = float(current[-1]["end"]) - float(current[0]["start"])
        text = str(token["text"])
        next_gap = 0.0
        if index + 1 < len(tokens):
            next_gap = max(0.0, float(tokens[index + 1]["start"]) - float(token["end"]))

        if sentence_locked:
            if next_gap >= SENTENCE_UNIT_GAP:
                close_current("pause")
            elif _word_ends_sentence(text):
                close_current("sentence")
            elif duration >= SENTENCE_UNIT_TARGET and _word_ends_soft_clause(text):
                close_current("clause")
            elif duration >= SENTENCE_UNIT_BREATH and next_gap >= 0.35:
                close_current("pause")
            elif duration >= SENTENCE_UNIT_HARD:
                close_current("forced_long")
            continue
        # A real acoustic gap is always a safe boundary, even when the phrase is short.
        # This prevents a 20-50s silent/dialogue hole from being absorbed by the next unit.
        if next_gap >= 1.0:
            close_current("pause")
        elif _word_ends_sentence(text):
            close_current("sentence")
        elif _word_ends_soft_clause(text) and duration >= min_natural_duration:
            close_current("clause")
        elif duration >= min_natural_duration and next_gap >= 0.50:
            close_current("pause")
        elif duration >= target_duration and next_gap >= 0.16:
            close_current("pause")
        # V2-style local anchor: never allow a normal spoken unit to grow into a long
        # sentence-locked block merely because punctuation is missing.
        elif duration >= max_duration:
            close_current("forced_local")
    close_current("end_of_transcript")

    # A repeated name can also arrive as a measured 40–120 ms sentence of its
    # own (for example "Han." + "Han accidentally ...").  It is not useful as
    # narration and would create a false semantic ID, so fold only these tiny
    # one/two-token artifacts into the following complete unit.
    merged_units: list[dict[str, Any]] = []
    index = 0
    while index < len(units):
        unit = units[index]
        duration = float(unit["speech_end"]) - float(unit["speech_start"])
        token_count = len(re.findall(r"\S+", str(unit["source_text"])))
        following_gap = (
            float(units[index + 1]["speech_start"]) - float(unit["speech_end"])
            if index + 1 < len(units) else 9999.0
        )
        if duration <= 0.5 and token_count <= 2 and index + 1 < len(units) and following_gap <= 0.60:
            following = dict(units[index + 1])
            left_text = _clean(unit["source_text"])
            right_text = _clean(following["source_text"])
            left_word = _comparison_text(left_text.rstrip(".!?…。！？"))
            right_first = _comparison_text(right_text.split(maxsplit=1)[0] if right_text else "")
            combined_text = right_text if left_word and left_word == right_first else _clean(f"{left_text} {right_text}")
            following["speech_start"] = unit["speech_start"]
            following["source_text"] = combined_text
            following["source_transcript_ids"] = list(dict.fromkeys(
                list(unit["source_transcript_ids"]) + list(following["source_transcript_ids"])
            ))
            following["source_transcript_id"] = following["source_transcript_ids"][0]
            units[index + 1] = following
            index += 1
            continue
        merged_units.append(unit)
        index += 1
    units = merged_units

    for i, unit in enumerate(units, 1):
        unit["id"] = i
        unit["timestamp"] = f"{_fmt_ts(unit['speech_start'])} --> {_fmt_ts(unit['speech_end'])}"
    return units


def assign_full_coverage_boundaries(units: list[dict[str, Any]], video_duration: float) -> list[dict[str, Any]]:
    """Give every atomic narration unit a contiguous visual interval anchored tightly around speech.

    Source speech timestamps remain stored as speech_start/speech_end.
    Crucially anchors each video segment around its actual dialogue to prevent dialogue scenes
    from being cut off or displaced by long intros or silent non-speaking gaps.
    """
    if not units:
        return []
    out = [dict(x) for x in units]

    # Cap intro lead-in before the first speech unit so speech is never trimmed away by intro silence!
    first_speech_start = float(out[0]["speech_start"])
    out[0]["video_start"] = max(0.0, first_speech_start - 1.2) if first_speech_start > 2.0 else 0.0

    for i in range(len(out) - 1):
        left = out[i]
        right = out[i + 1]
        le = float(left["speech_end"])
        rs = float(right["speech_start"])
        gap = rs - le
        if gap > 3.0:
            # When there is a notable gap (e.g. action scene, silence), anchor each segment close
            # to its speech (up to 1.5s context padding) rather than stretching midpoint across 10-20s.
            boundary = (le + rs) / 2.0
            left_end = min(le + 1.5, boundary)
            right_start = max(rs - 1.5, boundary)
            left["video_end"] = round(left_end, 3)
            right["video_start"] = round(right_start, 3)
        else:
            boundary = (le + rs) / 2.0 if rs >= le else max(le, rs)
            boundary = max(0.0, min(float(video_duration), boundary))
            left["video_end"] = round(boundary, 3)
            right["video_start"] = round(boundary, 3)

    last_speech_end = float(out[-1]["speech_end"])
    # Cap trailing silence so ending dialogue is not delayed or stretched into dark credits
    if float(video_duration) - last_speech_end > 4.0:
        out[-1]["video_end"] = round(min(float(video_duration), last_speech_end + 2.5), 3)
    else:
        out[-1]["video_end"] = round(float(video_duration), 3)

    for i, unit in enumerate(out):
        unit.setdefault("video_start", 0.0 if i == 0 else out[i - 1]["video_end"])
        unit.setdefault("video_end", round(float(unit["speech_end"]), 3))
    return out


def _batch_by_size(items: list[dict[str, Any]], *, max_items: int, max_chars: int) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    chars = 0
    for item in items:
        cost = len(str(item.get("source_text", ""))) + 96
        if current and (len(current) >= max_items or chars + cost > max_chars):
            batches.append(current)
            current = []
            chars = 0
        current.append(item)
        chars += cost
    if current:
        batches.append(current)
    return batches


def _source_lines(items: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"[ID {int(x['id']):05d}] [{x['timestamp']}] {_clean(x['source_text'])}"
        for x in items
    )


def _translation_lines(items: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"[ID {int(x['id']):05d}] [{x['timestamp']}] SOURCE: {_clean(x['source_text'])}\n"
        f"OUTPUT: {_clean(x.get('translation'))}"
        for x in items
    )


def _comparison_text(value: Any) -> str:
    return re.sub(r"[^\w\u1000-\u109f]+", "", _clean(value).lower(), flags=re.UNICODE)


def find_suspicious_adjacent_duplicates(
    items: list[dict[str, Any]],
    previous_context: list[dict[str, Any]] | None = None,
) -> list[tuple[int, int]]:
    """Find adjacent Burmese rows that repeat despite materially different sources."""
    sequence = list(previous_context[-1:] if previous_context else []) + list(items)
    suspicious: list[tuple[int, int]] = []
    for left, right in zip(sequence, sequence[1:]):
        left_tr = _comparison_text(left.get("translation"))
        right_tr = _comparison_text(right.get("translation"))
        if min(len(left_tr), len(right_tr)) < 12:
            continue
        translation_similarity = difflib.SequenceMatcher(None, left_tr, right_tr).ratio()
        if translation_similarity < 0.94:
            continue
        left_source = _comparison_text(left.get("source_text"))
        right_source = _comparison_text(right.get("source_text"))
        source_similarity = difflib.SequenceMatcher(None, left_source, right_source).ratio()
        if source_similarity < 0.86:
            suspicious.append((int(left["id"]), int(right["id"])))
    return suspicious


def _assert_no_adjacent_translation_duplicates(
    items: list[dict[str, Any]],
    previous_context: list[dict[str, Any]] | None = None,
) -> None:
    pairs = find_suspicious_adjacent_duplicates(items, previous_context)
    if pairs:
        labels = ", ".join(f"{left}/{right}" for left, right in pairs)
        raise ValueError(f"Suspicious adjacent duplicate Burmese translation IDs: {labels}")


def _extract_json_payload(raw_text: str) -> Any:
    raw = (raw_text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        for opener, closer in (("{", "}"), ("[", "]")):
            a, b = raw.find(opener), raw.rfind(closer)
            if a >= 0 and b > a:
                try:
                    return json.loads(raw[a:b + 1])
                except json.JSONDecodeError:
                    pass
        raise


def _gemini_json(ai: Any, prompt: str, *, retries: int = 3, log: LogFn | None = None,
                 rotation_tier: str | None = None) -> Any:
    """Run one logical Gemini request with model/key failover.

    Fallback switches never consume JSON/validation retries, so the exact same
    translation/cleanup/audit batch is resent after each switch.

    Batch U: ``rotation_tier`` picks the call's STARTING model via the
    pool's begin hook — "full" rotates between the two full models,
    "wide" across every non-tail model, None keeps the natural chain
    head. Rotation happens once per logical call, before the retry
    loop, so validation retries keep the call's starting point.
    """
    begin = getattr(ai, "_recap_begin_gemini_call", None)
    if callable(begin):
        begin(rotation_tier)
    last: Exception | None = None
    attempt = 1
    http_attempts = 0
    while attempt <= retries:
        # Batch AJ / L20: total HTTP attempt budget for this logical
        # call — failover switches below `continue` without consuming
        # a validation retry, so without this counter the walk is
        # bounded only by pool x chain size.
        http_attempts += 1
        if http_attempts > GEMINI_JSON_MAX_HTTP_ATTEMPTS:
            pool_size = int(getattr(ai, "_recap_key_pool_size", 1) or 1)
            model_count = int(getattr(ai, "_recap_model_pool_size", 1) or 1)
            if log:
                log(
                    f"⛔ Gemini attempt budget exhausted "
                    f"({GEMINI_JSON_MAX_HTTP_ATTEMPTS} HTTP attempts for one "
                    "logical call) — failing instead of walking the whole pool."
                )
            raise GeminiKeyPoolExhaustedError(
                _gemini_key_pool_message(pool_size, model_count)
            ) from last
        try:
            ai._enable_proxy()
            try:
                response = ai.client.models.generate_content(model=ai.text_model, contents=prompt)
            finally:
                ai._disable_proxy()
            # Batch DN: the request succeeded — count it against the
            # serving (key, model) so the next job's pool order starts
            # from richer keys. The HTTP call consumed quota even if
            # payload extraction below rejects the body, so recording
            # happens before extraction. Best-effort: usage accounting
            # must never break a planner call.
            if _key_rotation is not None:
                try:
                    _key_rotation.health_for("gemini").record_usage(
                        str(getattr(ai, "api_key", "") or ""),
                        str(getattr(ai, "_recap_active_model", "") or getattr(ai, "text_model", "") or ""),
                    )
                except Exception:
                    pass
            return _extract_json_payload(getattr(response, "text", ""))
        except Exception as exc:
            last = exc
            active_key = str(getattr(ai, "_recap_active_key_label", "GEMINI_API_KEY"))
            active_model = str(getattr(ai, "_recap_active_model", getattr(ai, "text_model", "Gemini")))

            if _is_gemini_key_rejection(exc):
                if log:
                    log(f"⚠️ {active_model} | {active_key} quota/auth rejection; trying next key...")
                rotate = getattr(ai, "_recap_rotate_gemini_key", None)
                if callable(rotate):
                    try:
                        if rotate():
                            continue  # exact same prompt; validation retry is untouched
                    except Exception as rotate_exc:
                        raise RuntimeError(f"Gemini key switch failed: {rotate_exc}") from rotate_exc

                # All keys in THIS model were rejected. Move to next model and Key 1.
                next_model = getattr(ai, "_recap_advance_after_key_pool", None)
                if callable(next_model):
                    try:
                        if next_model():
                            continue
                    except Exception as model_exc:
                        raise RuntimeError(f"Gemini model switch failed: {model_exc}") from model_exc

                pool_size = int(getattr(ai, "_recap_key_pool_size", 1) or 1)
                model_count = int(getattr(ai, "_recap_model_pool_size", 1) or 1)
                if log:
                    log(f"⛔ Gemini fallback exhausted at {active_model}; no model/key remains.")
                raise GeminiKeyPoolExhaustedError(_gemini_key_pool_message(pool_size, model_count)) from exc

            if _is_gemini_transient_model_error(exc):
                # Required behavior: 503 / high-demand / timeout skips the rest of
                # the current model's keys and starts the next model from Key 1.
                if log:
                    log(f"⚠️ {active_model} service/timeout error; switching to next model from Key 1...")
                next_model = getattr(ai, "_recap_advance_gemini_model", None)
                if callable(next_model):
                    try:
                        if next_model("503/high-demand/timeout"):
                            continue  # exact same prompt; validation retry is untouched
                    except Exception as model_exc:
                        raise RuntimeError(f"Gemini model switch failed: {model_exc}") from model_exc
                if log:
                    log(f"⛔ Final Gemini model failed with service/timeout error: {exc}")
                raise RuntimeError(f"Gemini fallback chain exhausted after service/timeout error: {exc}") from exc

            if log:
                log(f"⚠️ Gemini JSON attempt {attempt}/{retries} failed: {exc}")
            if attempt < retries:
                time.sleep(1.2 * attempt)
            attempt += 1
    raise RuntimeError(f"Gemini JSON response failed after {retries} attempts: {last}")


def _load_runtime_config(runtime_root: Path) -> dict[str, Any]:
    path = runtime_root / "config" / "whisper.json"  # optional: whisper_backend / whisper_device / whisper_compute_type
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def build_story_context(
    ai: Any,
    units: list[dict[str, Any]],
    *,
    source_title: str = "",
    batch_max_items: int,
    log: LogFn,
    should_stop: Callable[[], bool],
    checkpoint_path: Path | None = None,
    source_fingerprint: str = "",
    reuse: bool = True,
    part_index: int = 1,
    total_parts: int = 1,
    prev_part_summary: str = "",
) -> dict[str, Any]:
    """Create durable context memory in a separate pass before translation, resumably."""
    batches = _batch_by_size(units, max_items=batch_max_items, max_chars=24000)
    memory: dict[str, Any] = {
        "characters": [], "relationships": [], "places": [], "terms": [],
        "plot_state": [], "name_consistency": [], "unresolved": []
    }
    completed = 0
    if reuse and checkpoint_path and checkpoint_path.is_file():
        try:
            saved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if saved.get("planner_mode") == PLANNER_MODE and saved.get("source_fingerprint") == source_fingerprint:
                completed = int(saved.get("completed_batches", 0) or 0)
                saved_memory = saved.get("context")
                if 0 <= completed <= len(batches) and isinstance(saved_memory, dict):
                    memory = saved_memory
                    if completed:
                        log(f"♻️ Story Context resume — {completed}/{len(batches)} batches already done")
                else:
                    completed = 0
        except Exception:
            completed = 0
    part_note = ""
    if int(total_parts or 1) > 1:
        part_note = f"\nMULTI-PART CONTEXT: Part {int(part_index)}/{int(total_parts)}."
        if int(part_index) > 1 and str(prev_part_summary or "").strip():
            part_note += f" Earlier part summary (keep exact names/roles): {str(prev_part_summary).strip()}"
    for i, batch in enumerate(batches, 1):
        if i <= completed:
            continue
        if should_stop():
            raise InterruptedError("STOP requested during story-context pass")
        title_block = f"\nVIDEO TITLE / PREMISE (အဓိက ခေါင်းစဉ်နှင့် ဇာတ်လမ်းနောက်ခံ):\n{source_title.strip()}\n" if source_title.strip() else ""
        prompt = f"""
You are building CONTEXT MEMORY for a later translation task.
Do not translate the transcript now. Do not write narration. Do not summarize away named facts.{title_block}{part_note}
Read this next chronological source chunk and UPDATE the existing compact context memory.
The memory exists only to keep names, relationships, terminology, locations, identities,
and the current story state consistent when later translation requests are sent in separate API calls.

RULES:
- Preserve exact character/place/object names as they appear in the source when known.
- Keep important relationships, aliases, roles, reveals, and terminology.
- Keep plot_state chronological but compact. Do not invent anything.
- If an earlier assumption is corrected by this chunk, update it.
- Return JSON only with these keys:
  characters, relationships, places, terms, plot_state, name_consistency, unresolved
- Each key must contain a compact JSON array. Keep the total memory concise enough to reuse in every later request.

EXISTING MEMORY:
{json.dumps(memory, ensure_ascii=False)}

SOURCE CHUNK {i}/{len(batches)}:
{_source_lines(batch)}
"""
        try:
            data = _gemini_json(ai, prompt, log=log, rotation_tier="wide")
            if not isinstance(data, dict):
                raise RuntimeError("Story context response must be a JSON object")
        except (GeminiKeyPoolExhaustedError, InterruptedError):
            raise
        except Exception as exc:
            # 2026-10-12, a user's run: "Gemini JSON response failed after 3 attempts: Expecting ':' delimiter"
            # stopped the whole job here (Resume then worked). The memory only helps keep names consistent,
            # so one unreadable chunk is left out and the job goes on.
            log(f"⚠️ Story Context {i}/{len(batches)}: Gemini's answer could not be read ({str(exc)[:160]}); "
                "this part is left out of the story memory")
            data = memory
        memory = {
            "characters": list(data.get("characters") or []),
            "relationships": list(data.get("relationships") or []),
            "places": list(data.get("places") or []),
            "terms": list(data.get("terms") or []),
            "plot_state": list(data.get("plot_state") or []),
            "name_consistency": list(data.get("name_consistency") or []),
            "unresolved": list(data.get("unresolved") or []),
        }
        if checkpoint_path:
            _atomic_json(checkpoint_path, {
                "planner_mode": PLANNER_MODE,
                "source_fingerprint": source_fingerprint,
                "completed_batches": i,
                "total_batches": len(batches),
                "context": memory,
            })
        log(f"🧠 Story Context {i}/{len(batches)} updated")
    return memory


def _validate_translation_items(
    source_batch: list[dict[str, Any]],
    payload: Any,
    *,
    log: LogFn | None = None,
) -> list[dict[str, Any]]:
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        raise ValueError("Translation response must contain an items array")
    expected = [int(x["id"]) for x in source_batch]
    got: list[int] = []
    by_id: dict[int, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            uid = int(item.get("id"))
        except Exception:
            continue
        if uid in by_id:
            raise ValueError(f"Duplicate translation ID: {uid}")
        tr = _sanitize_translation_text(item.get("translation"))
        if not tr:
            raise ValueError(f"Empty translation ID: {uid}")
        source_row = next((src for src in source_batch if int(src["id"]) == uid), None)
        if source_row is not None and _has_speakable_content(source_row.get("source_text")) and not _has_speakable_content(tr):
            raise ValueError(
                f"Translation ID {uid} contains no speakable words (got {tr!r}); "
                "do not return ellipsis/placeholders"
            )
        by_id[uid] = item
        got.append(uid)
    if got != expected:
        raise ValueError(f"ID mismatch. expected={expected[:3]}...{expected[-3:]} got={got[:3]}...{got[-3:]}")
    out = []
    for src in source_batch:
        uid = int(src["id"])
        raw = by_id[uid]
        # Gemini does not own timing. Older/cached prompt variants may still return a
        # timestamp and models occasionally typo or copy a neighbouring row's value.
        # Never let that discard an otherwise ID-complete translation batch: the
        # locally measured transcript timestamp remains authoritative in ``src``.
        returned_ts = _clean(raw.get("timestamp"))
        if returned_ts and returned_ts != src["timestamp"] and log:
            log(
                f"ℹ️ Ignored Gemini timestamp for ID {uid}: {returned_ts}; "
                f"using local {src['timestamp']}"
            )
        out.append({**src, "translation": _sanitize_translation_text(raw.get("translation"))})
    return out


def _enforce_style_and_gender_particles(text: str, *, style: str, is_female: bool) -> str:
    """Deterministically strip mismatched gender/narrator particles from Burmese sentences."""
    cleaned = _sanitize_translation_text(text)
    if not cleaned:
        return cleaned
    if is_female or style == "pov":
        # Strip male commentator particles ('ဗျာ', 'ခင်ဗျာ') at sentence/clause endings
        cleaned = re.sub(r"(ပါတော့တယ်|ပါတယ်|တယ်|တာပဲ|တာပေါ့|သေးတယ်|ခဲ့တယ်|နေတယ်|ရတယ်|ပါပဲ)ခင်ဗျာ(?=[။\s]|$)", r"\1", cleaned)
        cleaned = re.sub(r"(ပါတော့တယ်|ပါတယ်|တယ်|တာပဲ|တာပေါ့|သေးတယ်|ခဲ့တယ်|နေတယ်|ရတယ်|ပါပဲ)ဗျာ(?=[။\s]|$)", r"\1", cleaned)
        cleaned = re.sub(r"ခင်ဗျာ(?=[။\s]|$)", "ရှင့်" if (is_female and style != "pov") else "", cleaned)
        cleaned = re.sub(r"ဗျာ(?=[။\s]|$)", "", cleaned)
    if style == "pov":
        pronoun = "ကျွန်မ" if is_female else "ကျွန်တော်"
        possessive = "ကျွန်မရဲ့" if is_female else "ကျွန်တော့်ရဲ့"
        obj_pronoun = "ကျွန်မကို" if is_female else "ကျွန်တော့်ကို"
        # Replace crude first-person 'ငါ' at the start of clauses when clearly the POV narrator
        cleaned = re.sub(r"(^|[။\s])ငါ့ရဲ့", rf"\1{possessive}", cleaned)
        cleaned = re.sub(r"(^|[။\s])ငါ့ကို", rf"\1{obj_pronoun}", cleaned)
        cleaned = re.sub(r"(^|[။\s])ငါကလည်း", rf"\1{pronoun}ကလည်း", cleaned)
        cleaned = re.sub(r"(^|[။\s])ငါလည်း", rf"\1{pronoun}လည်း", cleaned)
        cleaned = re.sub(r"(^|[။\s])ငါကတော့", rf"\1{pronoun}ကတော့", cleaned)
        if is_female:
            cleaned = re.sub(r"ကျွန်တော့်", "ကျွန်မ", cleaned)
            cleaned = re.sub(r"ကျွန်တော်", "ကျွန်မ", cleaned)
        else:
            cleaned = re.sub(r"ကျွန်မ", "ကျွန်တော်", cleaned)
    # Normalize common Myanmar writing errors in TTS text. The full
    # canonicalizer also repairs syllable mark order, so a line that
    # leaves the planner is storage-ordered as well as spell-checked.
    cleaned = re.sub(r'(။)+', '။', cleaned)  # double-period
    cleaned = re.sub(r'(၊)+', '၊', cleaned)  # double-comma
    cleaned = cleaned.replace('\u200B', '').replace('\u200C', '').replace('\u200D', '')
    cleaned = canonicalize_burmese_text(cleaned)
    return _clean(cleaned)


def translate_batch(
    ai: Any,
    *,
    source_title: str = "",
    story_context: dict[str, Any],
    previous_context: list[dict[str, Any]],
    current: list[dict[str, Any]],
    next_context: list[dict[str, Any]],
    log: LogFn,
    narrative_style: str = "narrator",
    is_female: bool = False,
    _split_depth: int = 0,
    previous_context_is_source: bool = False,
    part_index: int = 1,
    total_parts: int = 1,
    prev_part_summary: str = "",
) -> list[dict[str, Any]]:
    style = str(narrative_style or "viral").lower().strip()
    is_very_first_batch = not previous_context and bool(current) and int(current[0].get("id", 0)) == 1
    part_idx_val = max(1, int(part_index or 1))
    total_parts_val = max(1, int(total_parts or 1))
    if is_very_first_batch:
        if part_idx_val > 1:
            intro_rule = (
                f"- PART {part_idx_val}/{total_parts_val} SEAMLESS CONTINUATION (NO INTRO CLICHÉS): Do NOT say "
                "'ဒီနေ့မှာတော့...', 'ဒီဇာတ်လမ်းလေးမှာတော့...', or restart from Episode 1 — pick up directly from "
                "the previous part's momentum and open IDs 1–2 with a punchy immediate hook for THIS part's turn.\n"
                "- OPENING NUMBERS: If THIS part turns on a striking quantity (money, years, counts), surface that number inside the opening hook or this part's first scene, written as Burmese words (e.g. 'သိန်း ငါးရာ', 'အနှစ် နှစ်ဆယ်') — never ASCII digits, never an invented number."
            )
        else:
            intro_rule = (
                "- For ID 1 ONLY (the opening line of the video), you may open with a brief, punchy hook introducing the situation, then immediately narrate ID 1's actual scene event. The hook must establish the story's premise concretely from the source itself — where we are, who matters, and what is going wrong — and it must AGREE with the opening scene: never tease a moment and then restart the story from a different place or contradict the first scene.\n"
                "- MANDATORY 3-SECOND HOOK (IDs 1–2 FOR EVERY STYLE): Open IDs 1–2 within the first ~3 seconds with ONE sharp, concrete curiosity loop or tension trigger from the real story (never spoilers, never generic clichés), then glide straight into the scene. When the presentation style below defines its own hook formula (a premise hook across IDs 1–3), that formula IS this hook — follow it, keep every ID carrying its own source meaning, and continue chronologically without restarting the story.\n"
                "- OPENING NUMBERS: If the story turns on a striking quantity (money, years, counts), surface that number inside the opening hook or the first scene, written as Burmese words (e.g. 'သိန်း ငါးရာ', 'အနှစ် နှစ်ဆယ်') — never ASCII digits, never an invented number."
            )
    else:
        intro_rule = "- DO NOT use opening intro phrases ('ဒီနေ့မှာတော့...', 'ဒီဇာတ်လမ်းလေးမှာတော့...', 'ပရိသတ်ကြီးတို့ရေ...') anywhere in this batch because the story is already in progress!"
    pov_pronoun = "ကျွန်မ" if is_female else "ကျွန်တော်"
    pov_possessive = "ကျွန်မရဲ့" if is_female else "ကျွန်တော့်ရဲ့"
    gender_rule = (
        "- VOICE GENDER RULE (FEMALE VOICE): The selected narrator voice is FEMALE. NEVER use male sentence-ending particles like 'ဗျာ' or 'ခင်ဗျာ' anywhere! End sentences naturally with 'တယ်'၊ 'ပါတယ်'၊ 'ခဲ့တယ်'၊ 'နေတာ'၊ 'ပါပဲ'၊ 'လိုက်တယ်'။"
        if is_female
        else "- VOICE GENDER RULE (MALE VOICE): The selected narrator voice is MALE. Avoid overusing 'ဗျာ' on every line; end sentences naturally with 'တယ်'၊ 'ပါတယ်'၊ 'ခဲ့တယ်'၊ 'နေတာ'၊ 'လိုက်ပါတယ်'။"
    )
    if style == "viral":
        style_prompt = f"""PRESENTATION STYLE: INSTANT HOOK — VIRAL MOVIE RECAP STORYTELLER (🔥 ချက်ချင်းစွဲဆောင် — ဆွဲဆောင်မှုရှိပြီး အချိတ်အဆက်မိသော ရုပ်ရှင်ဇာတ်လမ်းပြော စတိုင်)
- DISTINCT IDENTITY: Fast-paced, high-retention social recap energy — snap straight to the turning point, heighten stakes cleanly with urgent spoken connectives ('အဲဒီမှာတင်...', 'မထင်ထားတဲ့ အချိုးအကွေ့က...', 'တကယ်တော့...'), and keep every beat kinetic.
- SEAMLESS STORY CONTINUITY & CAUSE-AND-EFFECT IS PRIORITY #1: Every segment MUST connect logically from the previous segment into the next segment so the listener clearly understands WHO is doing WHAT, TO WHOM, and WHY (ဇာတ်လမ်း အချိတ်အဆက် မိပြီး အဓိပ္ပာယ်ပြည့်စုံရမည်).
{intro_rule}
{gender_rule}
- NEVER insert random generic filler phrases ('ကြည့်ပါဦးဗျာ...', 'အဆုံးသတ်မှာ ဘာဆက်ဖြစ်မလဲ...', 'မထင်မှတ်ထားတဲ့ အရာတစ်ခု ဖြစ်လာခဲ့တယ်...') that disconnect consecutive sentences or replace actual plot meaning.
- Turn short or fragmented source dialogues into clear, connected third-person Burmese recap storytelling using natural transition words ('အဲဒီအခါမှာတော့...', 'ဒါကိုမြင်တော့...', 'ဒါကြောင့် သူက...', 'အဲဒီနောက်မှာတော့...').
- PREMISE-DUMP OPENING HOOK (IDs 1–3, PRIMARY FORMULA — single video or Part 1 only; a later series part follows the SEAMLESS CONTINUATION rule instead): Across IDs 1–3, compress the story's strange premise into 1–4 concrete sentences — WHO the protagonist is + the ABNORMAL situation they are in + the TURN ('ဒါပေမဲ့ တစ်ရက်မှာတော့…'). Include backstory when the backstory IS the premise. Every one of IDs 1–3 still carries its own source meaning alongside the premise. Then continue CHRONOLOGICALLY from there — NEVER restart the story at scene 1 after the hook, and never invent a premise detail the source does not establish.
- SECONDARY HOOK — SHOCK TEASE (only when the premise itself is ordinary and the story's power is an exceptional mid-story twist): tease that twist in ONE short clause WITHOUT explaining how it ends, then deliver the premise and flow on chronologically from ID 1's actual scene. Never spoil the ending and never invent a moment that is not in the story.
- TURN CADENCE: Land a genuine story turn or reveal roughly every 15–20 seconds of narration, taken from the source's own events — never manufactured by filler.
- DRAMATIC IRONY TOOL: When the source supports it, the narrator may tell the audience what a character does not know ('ဒါပေမဲ့ သူမသိခဲ့တာက…') — only facts the source itself establishes.
- SCENE-BOUNDARY PULL: At a genuine scene change or reveal, you MAY end the segment on the real plot's own unanswered question (what the character is about to do/discover) so viewers keep watching — the pull must come from the actual events, never from generic teaser filler (see the prohibition above).
- VARY SENTENCE OPENINGS: Do not start consecutive segments with the same connective or the same subject name; alternate connectives, pronouns and name mentions so the narration never sounds mechanical.
- Tone: Engaging, clear, dynamic colloquial spoken Burmese (ခေတ်မီဆွဲဆောင်မှုရှိသော ဇာတ်လမ်းပြော စကားပြောဟန်).
- STRICTLY PROHIBITED: Dry, bookish literary writing. NEVER end sentences with formal bookish particles like 'သည်'၊ 'သဖြင့်'၊ 'ပေသည်'၊ 'ခဲ့ပေသည်'၊ 'လျက်ရှိသည်'။ ALWAYS use conversational sentence endings like 'တယ်'၊ 'ခဲ့တယ်'၊ 'နေတာ'၊ 'သွားတယ်'၊ 'လိုက်ပါတယ်'။"""
    elif style == "funny":
        style_prompt = f"""PRESENTATION STYLE: LIGHT COMEDY — WARM SITUATIONAL RECAP (😂 ပေါ့ပါးဟာသ — နွေးထွေးပေါ့ပါးပြီး အချိတ်အဆက်မိသော ဇာတ်လမ်းပြော စတိုင်)
- DISTINCT IDENTITY: Warm situational lightness, NOT stand-up commentary — the humour lives in the misunderstanding or absurd situation itself, told in the narrator's amused, affectionate tone (amused asides such as 'ဖြစ်ချင်တော့...', 'ကံဆိုးချင်တော့...', 'ဒီလူလေးကတော့ အခုထိ ဘာမှ မရိပ်မိသေးပါဘူး'). Never snarky, never sarcastic toward the characters, never stiff or melodramatic on comic scenes.
- SEAMLESS STORY CONTINUITY & CAUSE-AND-EFFECT IS PRIORITY #1: Every segment MUST connect logically from the previous segment into the next segment so the plot is 100% coherent and easy to follow.
{intro_rule}
{gender_rule}
- Keep 100% of the real plot and cause-and-effect intact while narrating characters' actions and reactions in a lively, warm, entertaining spoken Burmese tone.
- HUMOR SOURCE RULE: The comedy must come from the story itself — a misunderstanding, an absurd situation, a character's over-the-top reaction, an ironic contrast. The light, amused TONE may run continuously through the narration; a JOKE (a light comparison or an amused aside) is still Maximum ONE per segment, and the plot sentence always comes first.
- INNER-THOUGHT DEVICE: For comic beats, quote the protagonist's thought in first person inside the narration ('သူ့စိတ်ထဲမှာတော့ \'…\' လို့ တွေးလိုက်ပါတယ်'), clearly attributed and consistent with what the source shows — never a thought that contradicts the character.
- TASTE RULE (အလွန်အရေးကြီး): When the scene is genuinely tragic (death, serious injury, grief, abuse), drop the comedy completely for that segment and narrate it straight and respectfully — never joke over a character's real suffering.
- NEVER replace actual story events with disconnected generic jokes or repetitive filler phrases.
- STRICTLY PROHIBITED: Formal bookish particles like 'သည်'၊ 'သဖြင့်'၊ 'ပေသည်'၊ 'ခဲ့ပေသည်'။ ALWAYS use conversational sentence endings like 'တယ်'၊ 'နေတာ'၊ 'ပါပဲ'၊ 'လိုက်ပါတယ်'။"""
    elif style == "pov":
        style_prompt = f"""PRESENTATION STYLE: FIRST-PERSON POV (👁️ ကိုယ်တွေ့ ရှုထောင့် — အဓိကဇာတ်ကောင် '{pov_pronoun}' ရှုထောင့်မှ ကိုယ်တိုင်ကြုံတွေ့ရသလို အချိတ်အဆက်မိမိ ပြောပြသော စတိုင်)
- DISTINCT IDENTITY: Confessional, intimate inner monologue — the listener is inside '{pov_pronoun}'s head feeling every shock, suspicion, and split-second decision directly from the heart, never sounding like an external YouTube commentator.
- SEAMLESS FIRST-PERSON POV CONTINUITY IS PRIORITY #1: Narrate the entire story consistently from the main character's own first-person perspective using '{pov_pronoun}' ('{pov_possessive}').
- PRONOUN & SPEAKER CLARITY RULE (အလွန်အရေးကြီး):
  1. Always refer to yourself (the protagonist narrator) as '{pov_pronoun}' — NEVER use crude 'ငါ' for narration and NEVER switch to third-person narrator commentary ('သူတို့...', 'ကောင်မလေးက...', '...ကြပါတော့တယ်ဗျာ') in later scenes!
  2. When other characters (antagonists, bosses, coworkers, family) speak or act, ALWAYS frame their words in clear reported/indirect speech or explicitly name the speaker (e.g., 'မန်နေဂျာက {pov_pronoun}ကို ... လို့ မောက်မာစွာ ပြောလာတယ်', 'သူက သူ့အနေနဲ့ ... ဆိုပြီး ခြိမ်းခြောက်လိုက်တယ်') so the listener NEVER confuses another character's 'I/me' with '{pov_pronoun}'!
  3. NATURAL SPOKEN RHYTHM: In POV mode, vary sentence length naturally. Mix short punchy sentences ("ကျွန်တော် ကြောက်သွားတယ်") with slightly longer connecting ones ("ဒါပေမဲ့ ကျွန်တော် ပြန်မပြောဘဲ လှည့်ထွက်ခဲ့တယ်"). Avoid repetitive sentence structures starting every line the same way.
  4. SHORT-SENTENCE MOMENTUM IN ACTION: In action stretches (a chase, a fight, an escape), switch to short first-person sentences carrying one action each ('{pov_pronoun} ပြေးတယ်။ တံခါးကို ဆွဲဖွင့်လိုက်တယ်။') so the momentum never stalls; return to the natural mix once the action ends.
{intro_rule}
- POV OPENING HOOK (IDs 1–3 — single video or Part 1 only): Open with up to THREE short premise beats, in whatever order the story supports — DANGER (what everyone fears), REWARD (the concrete quantity at stake, written as Burmese words), MYSTERY (the one warning or rule that will matter later) — told in first person, then start the action. Use only beats the source actually contains; skip any it lacks.
- CHEKHOV-GUN RULE: Any warning, object or rule planted in the opening must be paid off when the story reaches it — name it again at that moment so the listener connects the two. Never invent a payoff the source lacks.
- LIMITED-KNOWLEDGE RULE: You may only narrate what the protagonist personally sees, hears, or feels in that moment. NEVER state another character's hidden motive or off-screen action as fact — frame it as the protagonist's own guess or later realization ('အဲဒီတုန်းက {pov_pronoun} မသိခဲ့တာက...', 'နောက်မှ {pov_pronoun} သိလိုက်ရတာက...').
- SCENE ANCHORING IN POV (မြင်ကွင်းပြောင်းတိုင်း အခြေချရန်): Whenever the scene changes (new place, new time, new people around you), open the new scene by grounding it through your own eyes — where {pov_pronoun} is now, who is there with {pov_pronoun}, and what is at stake for {pov_pronoun} — so a listener joining mid-story can follow without rewinding. Anchor in first person only; never switch to an outside narrator's summary to set the scene.
- VOICE & PARTICLE RULE: In POV mode you are the main character telling your own story — NEVER use commentator particles like 'ဗျာ' or 'ခင်ဗျာ' or 'ပရိသတ်ကြီးတို့ရေ'. End sentences naturally with 'တယ်'၊ 'ခဲ့တယ်'၊ 'နေတာ'၊ 'ပါပဲ'၊ 'လိုက်တယ်'၊ 'ရတယ်'။
- STRICTLY PROHIBITED: Dry, bookish literary writing ('သည်'၊ 'သဖြင့်'၊ 'ပေသည်'၊ 'ခဲ့ပေသည်'၊ 'လျက်ရှိသည်')."""
    elif style == "dramatic":
        style_prompt = f"""PRESENTATION STYLE: DRAMA PRIME — EMOTIONAL MELODRAMA RECAP (🎭 ဒရာမာ ပရိုင်း — ခံစားချက်ပြည့်ဝပြီး ရင်ခုန်ဖွယ် ဒရာမာ ဇာတ်လမ်းပြော စတိုင်)
- DISTINCT IDENTITY: DEFAULT VOICE = MELODRAMA REGISTER — an emotionally invested storyteller who feels WITH the characters, names the heartbreak, humiliation, injustice and longing in plain words, and lets the audience feel who is wronged. Never bubbly slang, casual giggles, or hype filler.
- SEAMLESS STORY CONTINUITY & CAUSE-AND-EFFECT IS PRIORITY #1: Every segment MUST connect logically from the previous segment into the next segment, clearly explaining the cause-and-effect of every tense moment.
{intro_rule}
{gender_rule}
- Convey the true emotional stakes, motives, and actions of the characters in smooth, gripping spoken Burmese without inserting generic filler hooks.
- MELODRAMA REGISTER (THE DEFAULT for family, romance, revenge, social and everyday-life sources):
  1. EVALUATE & SIDE: The narrator may judge and take sides — label emotions and moral stakes in plain words ('သူမရဲ့ ရင်ထဲမှာတော့ ကြေကွဲမှုတွေ ပြည့်နေပါပြီ', 'ဒါဟာ တကယ့်ကို မတရားတဲ့ စွပ်စွဲမှုပါပဲ').
  2. DRAMATIC IRONY: Tell the audience the truth a character is missing ('ဒါပေမဲ့ သူမသိခဲ့တာက…') whenever the source has already established that truth.
  3. SENTENCE LENGTH: Longer, flowing sentences are allowed in emotional scenes; short staccato sentences are reserved for physical-danger peaks only.
  4. DIALOGUE-FORWARD CONFRONTATIONS: In a confrontation scene, 2–4 consecutive segments may carry near-verbatim key dialogue lines (each clearly attributed to its speaker) instead of paraphrase; paraphrase resumes for the connective tissue. Outside confrontation scenes, shared rule C (dialogue becomes narration) applies as written.
  5. RHETORICAL QUESTIONS: Allowed at climax beats only.
- NOIR / THRILLER SUB-MODE (crime, horror, thriller and mystery sources only): Switch to dark, atmospheric thriller tension — build suspense through sensory details, ominous silence, and sharp staccato peril beats.
- PACING & RHYTHM RULE (NOIR / THRILLER SUB-MODE): Match sentence length to tension — at peak danger or shock, switch to SHORT, punchy, rapid sentences (one action per sentence) so the voice sounds breathless; in the calm aftermath, let sentences run slightly longer and slower. Build dread by stating small concrete details (a sound, a look, a silence) instead of announcing 'ကြောက်စရာကြီး' in the abstract.
- WITHHOLD, DON'T EXPLAIN — PLOT FACTS ONLY: When the source scene hides a motive or identity, narrate only what is visible and let the question hang — never explain a plot twist (identity or motive reveal) before the story itself reveals it. This applies to PLOT FACTS only, NEVER to emotions: always say plainly what the characters feel.
- STRICTLY PROHIBITED: Dry, bookish literary writing. NEVER end sentences with formal bookish particles like 'သည်'၊ 'သဖြင့်'၊ 'ပေသည်'၊ 'ခဲ့ပေသည်'၊ 'လျက်ရှိသည်'။ ALWAYS use conversational sentence endings like 'တယ်'၊ 'ခဲ့တယ်'၊ 'နေတာ'၊ 'သွားတယ်'၊ 'လိုက်ပါတယ်'။"""
    else:  # standard / narrator
        style_prompt = f"""PRESENTATION STYLE: CINEMATIC STORYTELLER — CINEMATIC MOVIE RECAP NARRATOR (📖 ရုပ်ရှင်ဟန် ဇာတ်လမ်းပြော — သဘာဝကျပြီး အချိတ်အဆက်မိသော ရုပ်ရှင်ဇာတ်လမ်းပြော စတိုင် - RECOMMENDED)
- DISTINCT IDENTITY: Classic cinema-grade storyteller — warm, lucid, emotionally grounded chronological flow that lets drama breathe naturally without hyper viral hype or comedy riffs.
- SEAMLESS STORY CONTINUITY & CAUSE-AND-EFFECT IS PRIORITY #1: Every segment MUST connect logically from the previous segment into the next segment so the entire video sounds like ONE continuous, coherent story.
{intro_rule}
{gender_rule}
- Clearly state who is speaking or acting, to whom, and why, using natural Burmese narrative connectives ('အဲဒီနောက်...', 'ဒါကိုကြားတော့...', 'ဒါပေမဲ့လည်း...').
- EMBEDDED-DIALOGUE RULE: Default to reported speech folded into the narration ('…လို့ ပြောပါတယ်', '…လို့ မေးလိုက်ပါတယ်', '…လို့ လျှောက်ထားပါတယ်'). A verbatim quote is reserved for at most the single most important line of a scene.
- SENTENCE PERMISSION: Long, flowing sentences (up to ~110 characters) are welcome in calm scenes, and mid-sentence literary connectors are fine. The anti-bookish ban below targets SENTENCE ENDINGS ('သည်' / 'သဖြင့်') only — never sentence length.
- EPISODIC BEAT PACING: Each scene resolves into ONE clear beat — a small turn, a moment of wonder, or a decision — before the narration moves on to the next scene.
- VARY SENTENCE OPENINGS: Do not start consecutive segments with the same connective or the same subject name; alternate connectives, pronouns and name mentions so the narration never sounds mechanical.
- Use fluent colloquial spoken Burmese (စကားပြောဟန်) popular on top movie recap channels.
- STRICTLY PROHIBITED: Bookish SENTENCE ENDINGS. NEVER end sentences with formal bookish particles like 'သည်'၊ 'သဖြင့်'၊ 'ပေသည်'၊ 'ခဲ့ပေသည်'၊ 'လျက်ရှိသည်'။ ALWAYS use natural conversational endings like 'တယ်'၊ 'ခဲ့တယ်'၊ 'နေတာ'၊ 'သွားတယ်'၊ 'လိုက်ပါတယ်'။"""

    task_rules = f"""
You are an AWARD-WINNING CONTEXT-AWARE BURMESE MOVIE RECAP NARRATOR & STORYTELLING TRANSLATOR.
Convert the CURRENT spoken dialogue/narration items (transcribed from the video in any language: Chinese, English, Korean, Thai, etc.) into a SINGLE COHERENT, MEANINGFUL, CONTINUOUS Burmese movie recap story that sounds natural and clear when read aloud by Burmese Text-to-Speech.

{style_prompt}

CORE PRINCIPLE — LOGICAL STORY COHERENCE & TIMING FIT (ဇာတ်လမ်း အချိတ်အဆက် မိပြီး အချိန်ကိုက်ဖြစ်စေရန်):
1. Source transcripts from movies/dramas often contain short dialogue lines or idiomatic expressions. NEVER translate them as isolated, disconnected, literal fragments that make no sense on their own!
2. Always read STORY CONTEXT MEMORY, PREVIOUS CONTEXT, CURRENT ITEMS, and NEXT CONTEXT together to understand the full scene: WHO is the subject, WHO are they talking to, WHAT just happened in the previous line, and WHY are they saying/doing this.
3. Weave each CURRENT item into a smooth, meaningful Burmese recap sentence that flows naturally from the preceding ID into the next ID.
4. TIMING & LENGTH BALANCE: Look at each item's `[start --> end]` duration window. Write a concise, natural spoken Burmese sentence that fits comfortably within that duration (~10 to 14 Burmese syllables per second of window duration) so the TTS voice speaks at a natural, crisp pace without rushing or dragging the scene!
   DENSITY TARGET (EVERY STYLE): Write to the TOP of that band (~14 syllables per second of window) so the delivered narration runs ≈19–22 characters per second with no dead air. Reach that density with real story content only — repeating an already-narrated fact as filler is FORBIDDEN — and never go past the top of the band: the spoken-duration budget always wins over density.
5. CAUSE-AND-EFFECT CONTINUITY (အဆက်မပြတ် ကြောင်းကျိုးဆက် စပ်ပြောင်းမှု): Read the PREVIOUS context, determine exactly WHO is currently acting, WHAT they just did in the preceding sentence, and WHY the current action logically follows — then narrate the CURRENT item as the natural next step in the story chain. NEVER begin a new scene idea mid-segment without connecting it to the previous segment's outcome.
6. MYANMAR SPELLING & ORTHOGRAPHY RULES (အလွန်အရေးကြီးသော စာလုံးပေါင်းသတ်ပုံ စည်းမျဉ်းများ):
   - ALWAYS use correct standard Myanmar Unicode spelling according to the Myanmar Language Commission (မြန်မာစာအဖွဲ့ သတ်ပုံကျမ်းစံနှုန်း):
   - 'ဖြစ်တယ်' / 'ဖြစ်ပြီး' / 'ဖြစ်လာ' (NEVER 'ဖစ်တယ်' / 'ဖစ်')
   - 'သေချာ' (NEVER 'သေခြာ')
   - 'စဉ်းစား' (NEVER 'စဥ်းစား')
   - 'ရုတ်တရက်' (NEVER 'ရုပ်တရက်')
   - 'ပြဿနာ' (NEVER 'ပြသာနာ' or 'ပြသနာ')
   - 'ဂရုစိုက်' (NEVER 'ဂရုဆိုက်')
   - 'အံ့ဩ' (NEVER 'အံသြ' or 'အံ့သြ')
   - 'ခဏ' (NEVER 'ခန' like in 'ခဏစောင့်', 'ခဏလေး')
   - 'မတတ်နိုင်' / 'တတ်နိုင်သမျှ' (NEVER 'တက်နိုင်')
   - 'ကြိုးစား' (NEVER 'ကျိုးစား' when referring to effort/striving)
   - 'ကျေးဇူး' (NEVER 'ကြေးဇူး')
   - 'လွတ်မြောက်' (NEVER 'လွှတ်မြောက်')
   - 'ဒီအဲန်အေ' for DNA (NEVER 'ဒီအန်အေ' — the letter N is 'အဲန်')
   - 'သံသယဝင်' / 'သံသယဖြစ်' (NEVER 'သံသဝင်')
   - 'လူပုံလယ်' (NEVER 'လူပုံအလယ်')
   - 'အကျပ်အတည်း' (NEVER 'အကြပ်အတည်း')
   - 'အဘွား' for grandmother (NEVER 'ဘွားအေ')
   - 'အံ့ဩစွာ' / 'စိုးရိမ်စွာ' (NEVER the malformed 'တကြီး' adverb family — 'အံ့ဩတကြီး', 'စိုးရိမ်တကြီး': emotion adverbs end in 'စွာ', never in 'တကြီး')
   - FOREIGN PROPER NAMES (နိုင်ငံခြား အမည်နာမများ): Transliterate each foreign name into the natural Burmese form readers actually use, choose ONE spelling for that name, and repeat that exact spelling in EVERY segment of the whole script. NEVER invent stacked-letter cluster forms (NEVER 'လို့စ်' for Lois) and NEVER let one name appear in two different spellings in the same story — an unstable name spelling reads as a spelling error even when each form looks plausible on its own.
   - Standard Unicode characters ONLY: strictly use 'ဉ်' (U+1009) where required; NEVER use Ethiopic, Georgian, or Zawgyi look-alike glyphs.

STORYTELLING STANDARD — SCENE-BY-SCENE NARRATION (မြင်ကွင်းအလိုက် ဇာတ်လမ်းပြော စံချိန်):
The finished script must play like ONE storyteller telling the whole story scene by scene. A viewer who starts watching at ANY point must understand who is on screen, where they are, and what is at stake — without rewinding.
A. DROP-IN COMPREHENSION (အလယ်က စကြည့်လည်း နားလည်စေရန်): Whenever the scene changes (new place, new time, or a different character taking focus), the FIRST sentence of the new scene must re-anchor the listener: name the actor, the place, and the stake in plain terms (e.g. 'တစ်နေရာရာမှာ ပုန်းကွယ်နေတဲ့ လူဆိုးကတော့...', 'ရတနာပြတိုက်ထဲမှာတော့...'). Never let a new scene open mid-thought as a bare continuation of the previous scene's sentence.
B. NAME THE ACTOR — NEVER FLOAT THE SUBJECT: Every sentence must make its actor unmistakable from that sentence alone. After any scene change or focus shift, re-state the character's NAME or ROLE instead of a bare 'သူ' / 'သူက' / 'သူကတော့'. Strictly prohibited failure mode: 'ပြီးတော့ လို့အစ်ကို ဒီမှာပဲ ခဏစောင့်ခိုင်းခဲ့ပါတယ်' (who told her to wait? unnamed) followed by 'သူကတော့ သတင်းပို့ဖို့...' (a different person, still unnamed). USE THE SOURCE'S OWN NAMES: when the source names a character, call them by that name — transliterated into Burmese script and spelled the same way every time it appears — and never replace a known name with a vague coined role paraphrase (never call a named character something like 'စတောရီယူသူ အပေါင်းအဖော်'). Refer to a character by role alone only when the source never names them.
C. DIALOGUE BECOMES NARRATION — NEVER DUBBING: Do NOT transcribe question-and-answer dialogue exchanges. Compress each exchange into ONE narrated beat stating who wanted what, who decided what, or what changed because of it (e.g. 'လို့အစ်က ဒီသတင်းကို လိုက်ယူဖို့ လာတာလို့ ရှင်းပြလိုက်တယ်'). A direct quote is allowed only for a single short exclamation that IS the scene's event (a warning shout, a name called out).
D. CONCRETE FACTS — NO VAGUE FILLER: Carry every scene with the source's own concrete facts — numbers, amounts, objects, places, named actions ('ဒေါ်လာ သန်း ၅၀ တန် ရတနာ', 'သံမဏိလက်တံကြီး', 'ကောင်းကင်ယံကနေ ပျံသန်း'). NEVER pad a segment with vague invented filler such as 'ကပ်ဘေးတစ်ခု ကျရောက်လာပြီး' or 'အန္တရာယ်ဆိုးကြီးဆီ စွမ်းအားထက်ထက်နဲ့' — when the source gives a specific fact, the narration must carry that fact.
E. REAL STANDARD WORDS ONLY: Every Burmese word must be a complete, real standard word. NEVER truncate or coin broken forms (never 'အရူး' — the complete word is 'အရူးအမူး' when someone acts wildly). If you are unsure of a word, use a simpler complete word instead.
F. WRITE 'အဲဒီ' — NEVER 'အဲ့ဒီ': In narration always use the standard written form 'အဲဒီ' ('အဲဒီအချိန်မှာ', 'အဲဒီနောက်', 'အဲဒီသတင်း'). The colloquial spelling 'အဲ့ဒီ' is prohibited anywhere in the output.
G. ENDINGS MUST MATCH THE SOURCE'S ARC: Never announce the whole story's conclusion ('ဇာတ်လမ်း နိဂုံး', 'ဇာတ်လမ်းဆုံးသွားပါပြီ') unless the source itself ends the entire story at that ID. When the source leaves the arc open — a fight about to begin, a rescue still on its way, a villain still at large — close the FINAL ID leaning forward on that real open question instead (e.g. 'ဒီလူဆိုးရဲ့ အစီအစဉ်ကို သူ ဘယ်လိုတားဆီးနိုင်မလဲ။').
H. VARY SENTENCE OPENINGS (ဝါကျအစပုံစံ မထပ်စေရန်): Do not start consecutive segments with the same connective or the same subject name. Alternate naturally among connectives ('အဲဒီအခါမှာတော့', 'ဒါကိုမြင်တော့', 'ဒါကြောင့်', 'အဲဒီနောက်မှာတော့'), name mentions, and pronouns so the narration never sounds mechanical — this applies in EVERY presentation style.
I. CALL THINGS WHAT THEY ARE (အရာဝတ္ထုကို သူ့အမှန် အမည်နဲ့ ခေါ်ရန်): Name every thing by what it truly is in the source. A machine or robot is 'စက်ရုပ်' — NEVER call it 'သရဲ' or 'ဘီလူး' unless the source itself is genuinely supernatural. Use natural Burmese constructions, never broken invented ones: 'အန္တရာယ်ထဲကို ရောက်သွားတယ်' (NEVER 'အန္တရာယ်ကြားဆီ ရောက်သွားတယ်'), 'နင်းခြေခံရတော့မှာ' (NEVER the clumsy split 'နင်းခြေတာခံရတော့မှာ').
J. VARY SENTENCE ENDINGS & RHYTHM — SOUND ALIVE, NOT MECHANICAL (အသက်ဝင်အောင် ဝါကျအဆုံး နဲ့ စည်းချက် ပြောင်းရန်): Do NOT let nearly every sentence end in '…ခဲ့ပါတယ်' — a script whose sentences all close the same way reads flat and mechanical no matter how good the voice is. Mix the endings the way a real storyteller does ('…တယ်', '…နေတယ်', '…လိုက်တယ်', '…သွားတယ်', '…ပါပဲ', a short bare punch ending, an occasional genuine question or exclamation where the story itself asks one) and mix SHORT punchy sentences at action and danger with longer flowing ones at calm moments. Liveliness starts here in the script: the voice reads exactly the rhythm written on this page.
K. NO SOURCE-CHANNEL CREDITS OR SIGN-OFFS (ရင်းမြစ် ချန်နယ် credit/sign-off များ လုံးဝ မထည့်ရ): The narration script must NEVER include the source video's channel credits, sign-off lines, subscribe/end-card text, or "presented by/for <channel>" phrases — in ANY language. Source branding (e.g. "ARY Plus အတွက် စောင့်နေပါတယ်", "presented by XYZ", "subscribe to…", "brought to you by…", channel name credit lines) is NOT story content and must be silently omitted. The script's FINAL sentence must be a real story sentence that ends at or just after the last tension beat; if the source ends in resolution or credits, end on the last tension line before it — NEVER copy a source end-card into the script and NEVER invent new story events to replace dropped credits.

HARD RULES:
1. Do NOT omit meaningful plot events, characters, actions, or key dialogues. Every segment's true meaning must be faithfully narrated inside its own ID.
2. Do NOT translate word-for-word literally. Express the true meaning and cause-and-effect in natural spoken Burmese recap narration.
3. STRICT SPOKEN BURMESE ONLY: Do NOT write formal literary Burmese endings ("သည်", "သဖြင့်", "ပေသည်", "ခဲ့ပေသည်"). Use spoken colloquial Burmese ("တယ်", "ခဲ့တယ်", "နေတာ", "လိုက်တယ်", "သွားတယ်").
4. Write EVERY word in standard Myanmar Unicode script (U+1000 to U+109F) with correct Myanmar spelling — no foreign/Latin letters and no Ethiopic/Georgian homoglyphs.
5. NEVER insert repetitive filler hooks ('ဒီနေ့မှာတော့...', 'ကြည့်ပါဦးဗျာ...', 'အဆုံးသတ်မှာ ဘာဆက်ဖြစ်မလဲ...') on middle segments.
6. Keep every CURRENT ID exactly once, in order. Do not merge or split IDs.
7. Context is READ-ONLY. Use PREVIOUS and NEXT context to make transitions smooth and pronouns/subjects clear, but never shift another ID's event into the wrong ID.
8. Maintain consistent character names/roles, relationships, and pronouns throughout the entire story.
9. Timestamps are local metadata. Return no timestamps.
10. Return JSON only with top-level key "items". Each item must contain integer "id" and non-empty string "translation".
11. NEVER return placeholders such as "...", "…", dashes, or empty text.
12. BURMESE NUMERALS & SUBTITLE PHRASING: ALWAYS write numbers, ages, counts, years, and times in Burmese numerals or Burmese words (e.g. '၁၀', 'ဆယ်', '၂၀၂၆', '၃ နာရီ' — NEVER ASCII digits '0–9' or Latin acronyms, because subtitle fonts show blank boxes for ASCII). Group words naturally with spaces every 10–22 characters (natural '၊'/'။' clause breaks ~20–42 chars) for clean subtitle wrapping.
"""
    title_block = f"\nVIDEO TITLE / PREMISE (အဓိက ခေါင်းစဉ်နှင့် ဇာတ်လမ်းနောက်ခံ):\n{source_title.strip()}\n" if source_title.strip() else ""
    part_block = ""
    if total_parts_val > 1:
        part_block = f"\nSERIES PART CONTEXT: This video is Part {part_idx_val} of {total_parts_val}."
        if part_idx_val > 1 and str(prev_part_summary or "").strip():
            part_block += f"\nPREVIOUS PART RECAP SUMMARY (continue seamlessly from here with identical names): {str(prev_part_summary).strip()}"
        if part_idx_val < total_parts_val:
            part_block += f"\nPART-END CLIFFHANGER: On the very final segment of Part {part_idx_val}, end ON the tension peak — the real open beat teasing what comes next in Part {part_idx_val + 1} — without concluding the overall story. No summary, no moral, no closing greeting."
        part_block += "\n"
    # Batch AL-P: parallel batches (all but the first pending one) are
    # translated before their predecessor's translation exists, so
    # their previous context is the predecessor's SOURCE lines. The
    # header says so plainly and the lines render as source, never as
    # fake SOURCE/OUTPUT pairs.
    if previous_context_is_source:
        prev_header = (
            "PREVIOUS CONTEXT (READ-ONLY — ORIGINAL SOURCE LINES, NOT YET TRANSLATED):\n"
            "These are the lines immediately before CURRENT in the original transcript. "
            "Use them ONLY for scene, names and continuity — do NOT translate them."
        )
        prev_block = _source_lines(previous_context) if previous_context else "(none)"
    else:
        prev_header = "PREVIOUS CONTEXT (READ-ONLY):"
        prev_block = _translation_lines(previous_context) if previous_context else "(none)"
    prompt = f"""
{task_rules}{title_block}{part_block}

STORY CONTEXT MEMORY:
{json.dumps(story_context, ensure_ascii=False)}

{prev_header}
{prev_block}

CURRENT ITEMS — CONVERT ALL OF THESE, ONE-FOR-ONE:
{_source_lines(current)}

NEXT CONTEXT (READ-ONLY LOOK-AHEAD):
{_source_lines(next_context) if next_context else '(none)'}

CRITICAL OUTPUT ID PROTOCOL:
- CURRENT IDs are immutable SOURCE IDs, not row positions.
- Return exactly {len(current)} items.
- Copy this exact ID sequence without dropping, renumbering, or inventing any ID:
{json.dumps([int(x["id"]) for x in current], ensure_ascii=False)}
- Before responding, verify that items[].id exactly equals that sequence.
"""
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            payload = _gemini_json(ai, prompt, retries=1, log=log, rotation_tier="full")
            translated = _validate_translation_items(current, payload, log=log)
            for row in translated:
                row["translation"] = _enforce_style_and_gender_particles(
                    row.get("translation", ""), style=style, is_female=is_female
                )
            _assert_no_adjacent_translation_duplicates(translated, previous_context)
            stray = [f"ID {int(x['id']):05d}: {' '.join(_foreign_letters(x['translation']))}"
                     for x in translated if _foreign_letters(x["translation"])]
            if stray and attempt < 3:  # the last answer is kept; build() drops the letters and logs the line
                raise ValueError("letters of another script inside the Burmese (" + "; ".join(stray) +
                                 "); write every word in Burmese script")
            english = [f"ID {int(x['id']):05d}: {', '.join(_latin_words(x['translation']))}"
                       for x in translated if _latin_words(x["translation"])]
            notes = [f"ID {int(x['id']):05d}" for x in translated if _note_text(x["translation"])]
            repeats = find_repeated_next_lines(translated, previous_context)
            twice = find_near_duplicates(translated, previous_context)
            slid = _slid_runs(translated, next_context[0].get("source_text") if next_context else None)
            if (english or notes or repeats or twice or slid) and attempt < 3:   # the last answer is kept
                problems = []
                if english:
                    problems.append("English words inside the Burmese (" + "; ".join(english[:10]) + "): write them "
                                    "in Burmese script — translate titles and terms, write names by sound")
                if notes:
                    problems.append("a note instead of narration (" + ", ".join(notes) + "): write the narration of "
                                    "that ID's own source words - even when the source is only the end of a sentence, "
                                    "say those words; never a remark such as '(continuation of the previous sentence)' "
                                    "and nothing only inside brackets")
                if repeats:
                    problems.append("the same words twice (" + ", ".join(f"ID {b:05d} repeats the end of ID {a:05d}"
                                                                         for a, b in repeats) + "): a sentence split "
                                    "across two IDs - each ID says only its own source words, never the next ID's")
                if twice:
                    problems.append("the same thing said twice (" + ", ".join(f"IDs {a:05d} and {b:05d}" for a, b in twice)
                                    + " say much the same): each ID says only its own source line")
                if slid:
                    problems.append("lines slid one ID early (" + ", ".join(f"IDs {a:05d}-{b:05d}" for a, b in slid)
                                    + " each say the NEXT ID's source words): every ID translates its own source line")
                raise ValueError("; ".join(problems))
            return translated
        except GeminiKeyPoolExhaustedError:
            raise
        except Exception as exc:
            last = exc
            log(f"⚠️ Translation validation attempt {attempt}/3 failed: {exc}")
            exact_ids = [int(x["id"]) for x in current]
            prompt += (
                f"\n\nCORRECTION REQUIRED: Previous response was invalid because: {exc}. "
                f"Return the full CURRENT batch again with exactly {len(exact_ids)} items. "
                f"The exact items[].id sequence MUST be {json.dumps(exact_ids, ensure_ascii=False)}. "
                "Do not omit the final ID, renumber rows, or merge adjacent IDs."
            )

    # V26 translation-ID resilience: V23's short local-sync segmentation creates many
    # more IDs per long video.  Some Gemini responses occasionally omit the last row
    # of an otherwise-correct 40-item translation batch (for example 401..439 instead
    # of 401..440).  After the normal retries, split only this failed batch into smaller
    # windows and translate those same source IDs.  Completed checkpoints, timestamps,
    # segmentation, narration mode, TTS, and Smart Sync remain unchanged.
    # Batch AJ / L20: the split recursion now has an EXPLICIT depth
    # cap (TRANSLATE_BATCH_MAX_SPLIT_DEPTH) — at the cap the last
    # validation error is raised instead of splitting further.
    if len(current) > 6 and _split_depth >= TRANSLATE_BATCH_MAX_SPLIT_DEPTH:
        log(
            f"⛔ Translation split depth cap reached "
            f"({TRANSLATE_BATCH_MAX_SPLIT_DEPTH}) for a {len(current)}-item "
            "batch — raising the last validation error instead of splitting further."
        )
    if len(current) > 6 and _split_depth < TRANSLATE_BATCH_MAX_SPLIT_DEPTH:
        mid = len(current) // 2
        left_rows = current[:mid]
        right_rows = current[mid:]
        expected_ids = [int(x["id"]) for x in current]
        prefix = "   " * _split_depth
        log(
            f"{prefix}↪️ Translation ID protocol fallback — splitting IDs "
            f"{expected_ids[0]:05d}–{expected_ids[-1]:05d} into "
            f"{len(left_rows)} + {len(right_rows)} smaller translation window(s)"
        )
        context_limit = max(1, len(previous_context) or 6)
        lookahead_limit = max(6, len(next_context) or 0)
        left_next = (right_rows + list(next_context or []))[:lookahead_limit]
        _split_part_kw = (
            {"part_index": part_idx_val, "total_parts": total_parts_val, "prev_part_summary": prev_part_summary}
            if (total_parts_val > 1 or part_idx_val > 1 or bool(prev_part_summary))
            else {}
        )
        translated_left = translate_batch(
            ai,
            source_title=source_title,
            story_context=story_context,
            previous_context=list(previous_context or [])[-context_limit:],
            current=left_rows,
            next_context=left_next,
            log=log,
            narrative_style=narrative_style,
            is_female=is_female,
            _split_depth=_split_depth + 1,
            **_split_part_kw,
        )
        right_prev = (list(previous_context or []) + translated_left)[-context_limit:]
        translated_right = translate_batch(
            ai,
            source_title=source_title,
            story_context=story_context,
            previous_context=right_prev,
            current=right_rows,
            next_context=list(next_context or []),
            log=log,
            narrative_style=narrative_style,
            is_female=is_female,
            _split_depth=_split_depth + 1,
            **_split_part_kw,
        )
        combined = translated_left + translated_right
        _assert_no_adjacent_translation_duplicates(combined, list(previous_context or []))
        _assert_speakable_translation_items(combined)
        return combined

    raise RuntimeError(f"Translation batch could not be validated: {last}")


_TS_PART_RE = re.compile(r"(\d{1,3}):(\d{2}):(\d{2})(?:[.,](\d{1,3}))?")


def _resolve_tempo(voice_speed: str | float | int | None = None) -> float:
    """Resolve the job's voice speed setting to a positive delivered tempo multiplier."""
    if isinstance(voice_speed, (int, float)) and not isinstance(voice_speed, bool):
        return max(0.5, float(voice_speed))
    if _speed_mod is not None:
        return max(0.5, float(_speed_mod.resolve_speed_tempo(voice_speed)))
    raw = str(voice_speed or DEFAULT_SPEED).strip()
    m = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)\s*%", raw)
    if m:
        return max(0.5, 1.0 + float(m.group(1)) / 100.0)
    return 1.12


def _clean_tts_text_for_budget(text: Any) -> str:
    """Mirror gemini_tts_engine._clean_tts_text so char counts reflect spoken Burmese text."""
    cleaned = re.sub(r"\s*\([A-Za-z0-9 ,.'\-]+\)", "", str(text or ""))
    return re.sub(r"\s+", " ", cleaned).strip()


def estimate_raw_speech_seconds(
    text: Any,
    *,
    chars_per_second: float = MEASURED_CHARS_PER_SECOND,
) -> float:
    """Estimate raw (pre-tempo) TTS duration in seconds from MEASURED_CHARS_PER_SECOND (17.6)."""
    cleaned = _clean_tts_text_for_budget(text)
    if not cleaned:
        return 0.0
    return len(cleaned) / max(1e-6, float(chars_per_second))


def _parse_srt_seconds(ts: Any) -> float | None:
    m = _TS_PART_RE.search(str(ts or ""))
    if not m:
        return None
    hours = int(m.group(1))
    minutes = int(m.group(2))
    seconds = int(m.group(3))
    millis = int((m.group(4) or "0").ljust(3, "0")[:3])
    return hours * 3600.0 + minutes * 60.0 + seconds + millis / 1000.0


def segment_window_seconds(row: dict[str, Any]) -> float | None:
    """Return the visual window in seconds, matching assign_full_coverage_boundaries when available."""
    if row.get("video_start") is not None and row.get("video_end") is not None:
        try:
            return max(0.0, float(row["video_end"]) - float(row["video_start"]))
        except (TypeError, ValueError):
            pass
    if row.get("speech_start") is not None and row.get("speech_end") is not None:
        try:
            return max(0.0, float(row["speech_end"]) - float(row["speech_start"]))
        except (TypeError, ValueError):
            pass
    ts = str(row.get("timestamp") or "")
    if "-->" in ts:
        parts = ts.split("-->", 1)
        start_s = _parse_srt_seconds(parts[0])
        end_s = _parse_srt_seconds(parts[1])
        if start_s is not None and end_s is not None:
            return max(0.0, end_s - start_s)
    return None


def segment_raw_duration_budget(
    row: dict[str, Any],
    *,
    voice_speed: str | float | None = DEFAULT_SPEED,
    max_video_slow: float = DEFAULT_MAX_VIDEO_SLOW,
) -> float | None:
    """Max raw (pre-tempo) TTS seconds allowed so post-tempo <= window * max_video_slow."""
    window = segment_window_seconds(row)
    if window is None or window <= 0.0:
        return None
    slow_cap = max(1.0, float(max_video_slow))
    tempo = _resolve_tempo(voice_speed)
    return window * slow_cap * tempo


def _overbudget_rows(
    rows: list[dict[str, Any]],
    *,
    voice_speed: str | float | None = DEFAULT_SPEED,
    max_video_slow: float = DEFAULT_MAX_VIDEO_SLOW,
) -> list[dict[str, Any]]:
    """Collect descriptors for every row whose estimated raw speech exceeds its budget."""
    tempo = _resolve_tempo(voice_speed)
    slow_cap = max(1.0, float(max_video_slow))
    over: list[dict[str, Any]] = []
    for row in rows:
        window = segment_window_seconds(row)
        raw_budget = segment_raw_duration_budget(
            row, voice_speed=voice_speed, max_video_slow=slow_cap
        )
        if window is None or raw_budget is None:
            continue
        cleaned = _clean_tts_text_for_budget(row.get("translation", ""))
        est_raw = (len(cleaned) / MEASURED_CHARS_PER_SECOND) if cleaned else 0.0
        if est_raw <= raw_budget + 1e-6:
            continue
        max_chars = max(1, int(math.floor(raw_budget * MEASURED_CHARS_PER_SECOND)))
        over.append({
            "id": int(row["id"]),
            "window_seconds": window,
            "raw_budget_seconds": raw_budget,
            "delivered_budget_seconds": window * slow_cap,
            "est_raw_seconds": est_raw,
            "est_post_seconds": est_raw / tempo,
            "current_chars": len(cleaned),
            "max_chars": max_chars,
        })
    return over


def _duration_budget_directive(
    overbudget: list[dict[str, Any]],
    *,
    tempo: float,
    max_video_slow: float,
) -> str:
    if not overbudget:
        return ""
    lines = [
        f"- SPOKEN-DURATION BUDGET (HARD CAP: post-tempo <= window * {max_video_slow:.2f}x at tempo {tempo:.2f}x):",
        '  The CURRENT IDs below EXCEED their spoken-duration budget and MUST use status="repair"',
        "  with a shorter spoken Burmese rewrite (trimming filler/repetition while preserving meaning,",
        "  character names, and key plot facts — NEVER drop or merge an ID; keep within-budget IDs unchanged):",
    ]
    for item in overbudget:
        lines.append(
            f"  * ID {item['id']}: window={item['window_seconds']:.2f}s | "
            f"est_raw={item['est_raw_seconds']:.2f}s > budget={item['raw_budget_seconds']:.2f}s "
            f"({item['current_chars']} chars -> MUST be <= {item['max_chars']} spoken chars)"
        )
    return "\n".join(lines)


def _build_compression_prompt(
    *,
    target_rows: list[dict[str, Any]],
    targets_by_id: dict[int, dict[str, Any]],
    story_context: dict[str, Any],
    previous_context: list[dict[str, Any]],
    next_context: list[dict[str, Any]],
    style: str,
    is_female: bool,
    tempo: float,
    max_video_slow: float,
    round_idx: int,
) -> str:
    target_ids = [int(r["id"]) for r in target_rows]
    exact_ids_json = json.dumps(target_ids, ensure_ascii=False)
    sample_json = json.dumps(
        {"items": [{"id": target_ids[0], "translation": f"concise Burmese line for ID {target_ids[0]}"}]},
        ensure_ascii=False,
    )
    budget_lines = []
    for r in target_rows:
        info = targets_by_id[int(r["id"])]
        budget_lines.append(
            f"[ID {info['id']:05d}] window={info['window_seconds']:.2f}s | "
            f"MAX_SPOKEN_CHARS={info['max_chars']} (raw_budget={info['raw_budget_seconds']:.2f}s @ tempo {tempo:.2f}x, "
            f"slow_cap={max_video_slow:.2f}x) | CURRENT={info['current_chars']} chars ({info['est_raw_seconds']:.2f}s)\n"
            f"SOURCE: {_clean(r.get('source_text'))}\n"
            f"CURRENT_TRANSLATION: {_clean(r.get('translation'))}"
        )
    gender_rule = (
        "Female narrator voice: NEVER use 'ဗျာ' or 'ခင်ဗျာ'."
        if is_female
        else "Keep natural spoken Burmese narrator voice."
    )
    return f"""You are an EXPERT BURMESE MOVIE RECAP EDITOR (Compression Round {round_idx}).
Compress ONLY the listed over-budget CURRENT IDs into concise spoken Burmese ({style.upper()} style) so each segment fits its spoken-duration budget.

STRICT RULES:
- Return JSON ONLY matching: {sample_json}
- Copy each ID in exact sequence: {exact_ids_json}. NEVER drop, merge, skip, or renumber any ID.
- EVERY item MUST satisfy len(translation) <= MAX_SPOKEN_CHARS (strictly shorter than CURRENT).
- Preserve the core meaning, character names, numbers, and key plot/action facts of each segment.
- Cut redundant connectives, repetitive modifiers, and conversational padding — write crisp, vivid spoken Burmese (no bookish 'သည်'/'သဖြင့်', no English/Latin words, Burmese numerals only, standard 'အဲဒီ', never '-တကြီး').
- {gender_rule}

STORY CONTEXT:
{json.dumps(story_context, ensure_ascii=False)}

PREVIOUS CONTEXT (READ-ONLY):
{_translation_lines(previous_context[-3:]) if previous_context else '(none)'}

NEXT SOURCE CONTEXT (READ-ONLY):
{_source_lines(next_context[:3]) if next_context else '(none)'}

ITEMS TO COMPRESS:
{chr(10).join(budget_lines)}
"""


def _apply_compressed_candidates(
    working: list[dict[str, Any]],
    items: list[Any],
    targets_by_id: dict[int, dict[str, Any]],
    *,
    style: str,
    is_female: bool,
    prev_rows: list[dict[str, Any]],
) -> int:
    index_by_id = {int(row["id"]): idx for idx, row in enumerate(working)}
    changed = 0
    for item in items:
        if not isinstance(item, dict) or item.get("id") is None:
            continue
        try:
            uid = int(item["id"])
        except (TypeError, ValueError):
            continue
        if uid not in targets_by_id or uid not in index_by_id:
            continue
        idx = index_by_id[uid]
        cand = _enforce_style_and_gender_particles(
            _clean(item.get("translation")), style=style, is_female=is_female
        )
        if not _has_speakable_content(cand):
            continue
        cur_text = str(working[idx].get("translation") or "")
        if len(_clean_tts_text_for_budget(cand)) >= len(_clean_tts_text_for_budget(cur_text)):
            continue
        neighbors: list[dict[str, Any]] = []
        if idx > 0:
            neighbors.append(working[idx - 1])
        elif prev_rows:
            neighbors.append(prev_rows[-1])
        if idx + 1 < len(working):
            neighbors.append(working[idx + 1])
        cand_cmp = _comparison_text(cand)
        src_cmp = _comparison_text(working[idx].get("source_text"))
        if any(
            cand_cmp and cand_cmp == _comparison_text(adj.get("translation")) and src_cmp != _comparison_text(adj.get("source_text"))
            for adj in neighbors
        ):
            continue
        working[idx] = dict(working[idx])
        working[idx]["translation"] = cand
        changed += 1
    return changed


def _compress_overbudget_rows(
    ai: Any,
    rows: list[dict[str, Any]],
    *,
    story_context: dict[str, Any],
    previous_context: list[dict[str, Any]] | None,
    next_context: list[dict[str, Any]] | None,
    log: LogFn,
    narrative_style: str = "narrator",
    is_female: bool = False,
    voice_speed: str | float | None = DEFAULT_SPEED,
    max_video_slow: float = DEFAULT_MAX_VIDEO_SLOW,
    max_rounds: int = 2,
) -> list[dict[str, Any]]:
    """Compress over-budget rows before synthesis while keeping within-budget rows byte-identical."""
    initial_over = _overbudget_rows(
        rows, voice_speed=voice_speed, max_video_slow=max_video_slow
    )
    if not initial_over:
        return list(rows)

    working = [dict(r) for r in rows]
    style = str(narrative_style or "narrator").lower().strip()
    tempo = _resolve_tempo(voice_speed)
    slow_cap = max(1.0, float(max_video_slow))
    prev_list = list(previous_context or [])
    next_list = list(next_context or [])
    compressed_ids: set[int] = set()

    for round_idx in range(1, max(1, int(max_rounds)) + 1):
        overbudget = _overbudget_rows(
            working, voice_speed=voice_speed, max_video_slow=slow_cap
        )
        if not overbudget:
            break
        targets_by_id = {item["id"]: item for item in overbudget}
        target_rows = [r for r in working if int(r["id"]) in targets_by_id]
        compress_prompt = _build_compression_prompt(
            target_rows=target_rows,
            targets_by_id=targets_by_id,
            story_context=story_context,
            previous_context=prev_list,
            next_context=next_list,
            style=style,
            is_female=is_female,
            tempo=tempo,
            max_video_slow=slow_cap,
            round_idx=round_idx,
        )
        try:
            payload = _gemini_json(ai, compress_prompt, retries=2, log=log, rotation_tier="full")
        except GeminiKeyPoolExhaustedError:
            raise
        except Exception as exc:
            log(f"⚠️ Duration budget compression round {round_idx}/{max_rounds} failed: {exc}")
            continue

        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            log(f"⚠️ Duration budget compression round {round_idx}/{max_rounds} returned no items array")
            continue
        before_texts = {int(r["id"]): r.get("translation") for r in target_rows}
        changed = _apply_compressed_candidates(
            working, items, targets_by_id, style=style, is_female=is_female, prev_rows=prev_list
        )
        for r in working:
            uid = int(r["id"])
            if uid in before_texts and r.get("translation") != before_texts[uid]:
                compressed_ids.add(uid)
        if changed == 0 and round_idx >= max_rounds:
            break

    _assert_speakable_translation_items(working)
    remaining = _overbudget_rows(working, voice_speed=voice_speed, max_video_slow=slow_cap)
    if compressed_ids and not remaining:
        log(
            f"✂️ Duration budget compressed {len(compressed_ids)} segment(s) within cap "
            f"({slow_cap:.2f}x slow @ tempo {tempo:.2f}x)"
        )
    elif remaining:
        over_desc = ", ".join(
            f"ID {x['id']}({x['est_raw_seconds']:.1f}s>{x['raw_budget_seconds']:.1f}s)"
            for x in remaining[:5]
        )
        log(
            f"⚠️ Duration budget: {len(remaining)} segment(s) remain over raw budget "
            f"after {max_rounds} compression round(s): {over_desc}"
        )
    return working


def audit_and_repair_batch(
    ai: Any,
    translated: list[dict[str, Any]],
    *,
    story_context: dict[str, Any],
    previous_context: list[dict[str, Any]] | None = None,
    next_context: list[dict[str, Any]] | None = None,
    log: LogFn,
    narrative_style: str = "narrator",
    is_female: bool = False,
    voice_speed: str | float | None = DEFAULT_SPEED,
    max_video_slow: float = DEFAULT_MAX_VIDEO_SLOW,
) -> list[dict[str, Any]]:
    """Mandatory per-ID ownership/completeness audit; only failed rows may be rewritten.

    V24 hardens the audit protocol for V23's larger ID numbers.  Gemini occasionally
    renumbered a late batch (for example IDs 01018-01055) as small local IDs even
    though the translation itself had already passed validation.  We now anchor the
    prompt to the exact current IDs and, if the model still returns the wrong ID set,
    automatically re-audit the *same translated rows* in smaller windows.  Translation,
    timestamps, segmentation, TTS and Smart Sync are not changed by this fallback.
    """
    if not translated:
        return []
    eff_speed = DEFAULT_SPEED if voice_speed is None else voice_speed
    eff_slow = DEFAULT_MAX_VIDEO_SLOW if max_video_slow is None else max(1.0, float(max_video_slow))
    eff_tempo = _resolve_tempo(eff_speed)
    previous_context = list(previous_context or [])
    next_context = list(next_context or [])
    style = str(narrative_style or "narrator").lower().strip()
    pov_pronoun = "ကျွန်မ" if is_female else "ကျွန်တော်"
    target_name = f"Burmese movie recap narration ({style.upper()} style)"
    style_drift_checklist = {
        "viral": "- STYLE CHECK (VIRAL): Needs kinetic pace, sharp curiosity hooks on scene turns, and punchy transitions; when IDs 1–3 of the video are in this batch, the premise hook must be present in IDs 1–3 (who + abnormal situation + the turn) with no generic teaser — repair flat slow documentary phrasing.",
        "funny": "- STYLE CHECK (FUNNY): Needs warm situational lightness; inner-thought or amused aside allowed; no stand-up commentary (while staying respectful in tragedy) — repair humorless dry phrasing.",
        "pov": f"- STRICT POV CONSISTENCY: Every row MUST stay in intimate first-person protagonist POV using '{pov_pronoun}'. NEVER use crude 'ငါ' for the narrator, NEVER let another character's direct quote use 'ငါ' without clear reported-speech framing, and NEVER drift into third-person commentator voice or 'ဗျာ'/'ခင်ဗျာ'!",
        "dramatic": "- STYLE CHECK (DRAMATIC): Needs the melodrama register by default — emotions labelled in plain words, dramatic irony used where the source allows, feelings explained even when plot facts are withheld; tense noir dread, sensory details and short breathless danger beats belong to crime/horror/thriller sources — repair cheerful slang, flat casual summary, or emotionally withheld narration.",
        "narrator": "- STYLE CHECK (NARRATOR): Needs warm, grounded cinema storytelling clarity — repair hyped viral slang, random jokes, or bookish sentence ENDINGS (သည်/သဖြင့်).",
    }
    style_check_line = style_drift_checklist.get(style, style_drift_checklist["narrator"])
    gender_check_line = (
        "- VOICE GENDER CONSISTENCY: The narrator voice is FEMALE — NEVER use male particles 'ဗျာ' or 'ခင်ဗျာ'!"
        if (is_female and style != "pov")
        else "- VOICE GENDER CONSISTENCY: Keep natural spoken Burmese sentence endings."
    )
    audit_style_rule = (
        f"{style_check_line}\n{gender_check_line}"
        if style != "pov"
        else style_check_line
    )

    def _audit_window(
        current_rows: list[dict[str, Any]],
        prev_rows: list[dict[str, Any]],
        next_rows: list[dict[str, Any]],
        *,
        depth: int = 0,
    ) -> list[dict[str, Any]]:
        expected_ids = [int(item["id"]) for item in current_rows]
        if not expected_ids:
            return []
        exact_ids_json = json.dumps(expected_ids, ensure_ascii=False)
        sample_checks = [{"id": expected_ids[0], "status": "pass"}]
        if len(expected_ids) > 1:
            sample_checks.append({
                "id": expected_ids[1],
                "status": "repair",
                "translation": f"complete corrected output for ID {expected_ids[1]} only",
            })
        sample_json = json.dumps({"checks": sample_checks}, ensure_ascii=False)
        overbudget_before = _overbudget_rows(
            current_rows, voice_speed=eff_speed, max_video_slow=eff_slow
        )
        budget_directive = _duration_budget_directive(
            overbudget_before, tempo=eff_tempo, max_video_slow=eff_slow
        )
        budget_block = f"\n{budget_directive}" if budget_directive else ""
        base_prompt = f"""
You are an EXPERT BURMESE MOVIE RECAP NARRATION AUDITOR.
Audit the {target_name}. Every visual event, dialogue, and plot development must remain faithfully narrated inside its corresponding timing ID.
PREVIOUS and NEXT context are READ-ONLY.

CRITICAL ID PROTOCOL:
- CURRENT IDs are SOURCE IDs, not positions and not local row numbers.
- NEVER renumber, restart, shorten, substitute, or invent IDs.
- Copy each CURRENT ID exactly as printed, including large IDs above 999.
- Return exactly {len(expected_ids)} checks.
- The exact required CURRENT ID sequence is: {exact_ids_json}

For EVERY CURRENT ID check:
- faithful scene narration: accurately reflects the events, dialogues, and characters in the segment;
- style adherence: maintain the requested {style.upper()} style and captivating conversational energy;
{audit_style_rule}
- natural spoken colloquial style: strictly avoid formal literary 'သည်'/'သဖြင့်' bookish endings, no English/Latin words;
- cinematic narrative flow: ensure natural, complete spoken sentences instead of awkward word-by-word literal translations or broken dialogue fragments;
- scene anchoring & subject clarity: a row that opens a new scene re-names its actor, place and stake instead of floating a bare pronoun carried over from the previous scene; dialogue is narrated as beats, never transcribed question-and-answer exchanges; no vague invented filler replacing the source's concrete facts; complete standard words only (no truncated coined forms); named characters are called by the source's own names, never by vague coined role paraphrases; the standard form 'အဲဒီ' is used (never 'အဲ့ဒီ');
- spelling & word-choice discipline: standard spelling throughout — never the malformed 'တကြီး' adverb family ('အံ့ဩတကြီး', 'စိုးရိမ်တကြီး' — write 'အံ့ဩစွာ', 'စိုးရိမ်စွာ'); every foreign name keeps ONE consistent natural spelling across all rows (never stacked inventions like 'လို့စ်'); things are called by their true names (a robot is 'စက်ရုပ်', never 'သရဲ'); natural constructions only (never 'အန္တရာယ်ကြားဆီ' or 'နင်းခြေတာခံရတော့မှာ'); sentence endings are varied — a batch where nearly every row closes '…ခဲ့ပါတယ်' needs repair;
- strictly standard Myanmar Unicode characters only (no homoglyphs from Ethiopic or Georgian scripts);
- BURMESE NUMERALS ONLY (font tofu guard): replace any ASCII digits '0–9' with Burmese numerals or words (e.g. '၁၀', '၂၀၂၆');
- no shifted chain where a row narrates another row;
- no adjacent duplicate/near-duplicate outputs for different sources;
- no source-channel credits, sign-offs, subscribe/end-card text, or "presented by/for <channel>" in ANY language — source branding is not story content; the script's FINAL sentence must be a real story sentence, never a credit line;
- consistent names/pronouns throughout the story.{budget_block}

Return JSON ONLY with every CURRENT ID once and in the exact sequence above.
Example shape using ACTUAL IDs from this batch:
{sample_json}
Use status=pass to preserve a good translation byte-for-byte. Use repair only when necessary.

STORY CONTEXT:
{json.dumps(story_context, ensure_ascii=False)}

PREVIOUS APPROVED CONTEXT (READ-ONLY):
{_translation_lines(prev_rows) if prev_rows else '(none)'}

CURRENT ITEMS TO AUDIT:
{_translation_lines(current_rows)}

NEXT SOURCE CONTEXT (READ-ONLY):
{_source_lines(next_rows) if next_rows else '(none)'}

FINAL ID CHECK BEFORE YOU RESPOND:
Your checks[].id list MUST equal exactly: {exact_ids_json}
"""
        prompt = base_prompt
        last_error: Exception | None = None
        for attempt in range(1, 3):
            try:
                payload = _gemini_json(ai, prompt, retries=2, log=log, rotation_tier="full")
                checks = payload.get("checks") if isinstance(payload, dict) else None
                if not isinstance(checks, list):
                    raise ValueError("audit response must contain a checks array")
                got_ids: list[int] = []
                by_id = {int(item["id"]): dict(item) for item in current_rows}
                repaired = 0
                for check in checks:
                    if not isinstance(check, dict):
                        raise ValueError("every audit check must be an object")
                    uid = int(check.get("id"))
                    got_ids.append(uid)
                    status = _clean(check.get("status")).lower()
                    if status == "pass":
                        if uid in by_id:
                            by_id[uid]["translation"] = _enforce_style_and_gender_particles(
                                by_id[uid].get("translation", ""), style=style, is_female=is_female
                            )
                        continue
                    if status != "repair":
                        raise ValueError(f"invalid audit status for ID {uid}: {status}")
                    replacement = _enforce_style_and_gender_particles(
                        _clean(check.get("translation")), style=style, is_female=is_female
                    )
                    if uid not in by_id or not replacement:
                        raise ValueError(f"missing repair translation for ID {uid}")
                    by_id[uid]["translation"] = replacement
                    repaired += 1
                if got_ids != expected_ids:
                    raise ValueError(f"audit ID mismatch expected={expected_ids} got={got_ids}")
                audited = [by_id[uid] for uid in expected_ids]
                _assert_no_adjacent_translation_duplicates(audited, prev_rows)
                _assert_speakable_translation_items(audited)
                audited = _compress_overbudget_rows(
                    ai,
                    audited,
                    story_context=story_context,
                    previous_context=prev_rows,
                    next_context=next_rows,
                    log=log,
                    narrative_style=style,
                    is_female=is_female,
                    voice_speed=eff_speed,
                    max_video_slow=eff_slow,
                )
                prefix = "   " * depth
                log(
                    f"{prefix}🛠️ Sentence-lock audit repaired {repaired} item(s)"
                    if repaired else
                    f"{prefix}✅ Sentence-lock audit: every ID passed"
                )
                return audited
            except GeminiKeyPoolExhaustedError:
                raise
            except Exception as exc:
                last_error = exc
                prefix = "   " * depth
                log(f"{prefix}⚠️ Sentence-lock audit attempt {attempt}/2 failed: {exc}")
                prompt = (
                    base_prompt
                    + f"\n\nCORRECTION REQUIRED: {exc}. "
                      f"Return the full checks array for every CURRENT ID. "
                      f"The exact ID list is {exact_ids_json}; do not renumber it."
                )

        # V23 creates roughly twice as many short local-sync IDs as the older planner.
        # If a large late batch still confuses Gemini's audit response, preserve the
        # already-validated translations and audit them in smaller windows instead of
        # aborting the whole multi-hour job.
        if len(current_rows) > 6:
            mid = len(current_rows) // 2
            left_rows = current_rows[:mid]
            right_rows = current_rows[mid:]
            prefix = "   " * depth
            log(
                f"{prefix}↪️ Audit ID protocol fallback — splitting IDs "
                f"{expected_ids[0]:05d}–{expected_ids[-1]:05d} into "
                f"{len(left_rows)} + {len(right_rows)} smaller audit window(s)"
            )
            context_limit = max(1, len(previous_context) or 6)
            left_next = (right_rows + next_rows)[:max(6, len(next_context) or 0)]
            audited_left = _audit_window(
                left_rows,
                prev_rows[-context_limit:],
                left_next,
                depth=depth + 1,
            )
            right_prev = (prev_rows + audited_left)[-context_limit:]
            audited_right = _audit_window(
                right_rows,
                right_prev,
                next_rows,
                depth=depth + 1,
            )
            combined = audited_left + audited_right
            _assert_no_adjacent_translation_duplicates(combined, prev_rows)
            _assert_speakable_translation_items(combined)
            return combined

        raise RuntimeError(f"Sentence-lock translation audit could not be validated: {last_error}")

    return _audit_window(translated, previous_context, next_context)

def make_translation_plan(units: list[dict[str, Any]], video_duration: float, *, source_title: str, transcript: dict[str, Any], story_context: dict[str, Any]) -> dict[str, Any]:
    # V32 global Smart Sync boundary: the original V2 visual-timing policy is used
    # by all source modes. Speech timestamps stay authoritative; only visual coverage
    # is expanded so gaps are split at midpoint and the source timeline stays contiguous.
    covered = assign_full_coverage_boundaries(units, video_duration)
    segments = []
    for unit in covered:
        tr = _clean(unit.get("translation"))
        if not tr:
            raise RuntimeError(f"Missing translation for ID {unit.get('id')}")
        if _has_speakable_content(unit.get("source_text")) and not _has_speakable_content(tr):
            raise RuntimeError(
                f"Invalid non-speech translation for ID {unit.get('id')}: {tr!r}. "
                "Translation must be regenerated before TTS."
            )
        source_ids = [int(value) for value in (unit.get("source_transcript_ids") or [unit["source_transcript_id"]])]
        segments.append({
            "source_segment_ids": source_ids,
            "atomic_id": int(unit["id"]),
            "gemini_start": round(float(unit["speech_start"]), 3),
            "gemini_end": round(float(unit["speech_end"]), 3),
            "timestamp": unit["timestamp"],
            "video_start": round(float(unit["video_start"]), 3),
            "video_end": round(float(unit["video_end"]), 3),
            "source_text": unit["source_text"],
            "narration": tr,
            "translation": tr,
            "semantic_boundary": str(unit.get("semantic_boundary") or ""),
            "forced_boundary": bool(unit.get("forced_boundary", False)),
            "visual_reason": "V2-style local timestamp translation anchor",
        })
    return {
        "planner_mode": PLANNER_MODE,
        "translation_task": True,
        "summary_task": False,
        "original_title": source_title,
        "source_duration": round(float(video_duration), 3),
        "transcript_backend": transcript.get("backend", ""),
        "transcript_model": transcript.get("model", ""),
        "transcript_language": transcript.get("language", ""),
        "story_context": story_context,
        "segments": segments,
    }


_AUDIT_BOOKISH_RE = re.compile(r"(?:သဖြင့်|ပေသည်|ခဲ့ပေသည်|လျက်ရှိသည်|သည်(?=[။\s]|$))")
_AUDIT_TAGYI_RE = re.compile(r"\S+တကြီး")
_AUDIT_AED_RE = re.compile(r"အဲ့ဒီ")
_AUDIT_ASCII_DIGITS_RE = re.compile(r"[0-9]")

# ── Credit / sign-off leak guard (2026-11-16) ──────────────────────
# Production evidence: recap narrations ending with the SOURCE channel's
# credit line ("ARY Plus အတွက် စောင့်နေပါတယ်", "presented by XYZ",
# "subscribe to…"). These are NOT story content.
_CREDIT_SIGNOFF_RE = re.compile(
    r"(?i)"
    # Burmese channel-credit patterns
    r"(?:အတွက်\s*(?:စောင့်နေပါတယ်|တင်ဆက်ပေးခဲ့တာပါ|တင်ဆက်ပေးပါတယ်|တင်ဆက်ပါတယ်))"
    r"|(?:(?:ချန်နယ်|channel)\s*(?:ကို\s*)?(?:subscribe|လုပ်|နှိပ်|ဖွင့်))"
    r"|(?:(?:subscribe|like|bell|notification)\s*(?:လုပ်|နှိပ်|ဖွင့်|button))"
    # English credit/sign-off patterns
    r"|(?:(?:presented|brought\s+to\s+you|powered)\s+(?:by|for)\b)"
    r"|(?:subscribe\s+(?:to|for|now|and|our|my)\b)"
    r"|(?:(?:thanks?\s+for|thank\s+you\s+for)\s+(?:watching|viewing|listening|tuning))"
    r"|(?:(?:don'?t\s+forget\s+to|make\s+sure\s+to|please)\s+(?:subscribe|like|share|hit|click))"
    r"|(?:(?:see\s+you|catch\s+you|stay\s+tuned)\s+(?:in\s+the\s+next|next\s+time|soon))"
    # Generic end-card / channel mention at sentence end
    r"|(?:(?:(?:ari|ary|ARY)\s*(?:digital|plus|zindagi|news|qtv))\s*(?:အတွက်|for|on|channel)?)"
)


def is_credit_or_signoff(text: str) -> bool:
    """Return True when *text* looks like a source-channel credit / sign-off line.

    Used by the ending-credit auditor to flag the LAST sentence of a
    recap script that accidentally copied the source's end-card.
    """
    cleaned = _clean(text)
    if not cleaned:
        return False
    return bool(_CREDIT_SIGNOFF_RE.search(cleaned))


def audit_script_ending_credit(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop a credit/sign-off LAST sentence — repair by removal only.

    If the final row's translation is a credit/sign-off, drop that
    sentence only (earlier rows are byte-identical).  Returns a NEW
    list; the caller must not assume in-place mutation.
    """
    if not rows:
        return list(rows)
    last = rows[-1]
    text = _clean(last.get("translation") or last.get("narration") or "")
    if not is_credit_or_signoff(text):
        return list(rows)
    # Drop the credit line — use the penultimate row as the new ending.
    return list(rows[:-1])


def _batch_audit_flags(
    translated: list[dict[str, Any]],
    previous_rows: list[dict[str, Any]],
    next_units: list[dict[str, Any]],
) -> list[str]:
    """Batch AL-P + §2B audit-on-flag: free local regex/structural detectors
    over one translated batch. Non-empty -> run paid audit_and_repair pass;
    clean batches skip the LLM audit (after Batch-1 hook spot-check)."""
    flags: list[str] = []
    khae_rows = 0
    for row in translated:
        uid = int(row["id"])
        text = str(row.get("translation") or "")
        if _foreign_letters(text):
            flags.append(f"foreign letters (ID {uid})")
        if _latin_words(text):
            flags.append(f"English words kept (ID {uid})")
        if _note_text(text):
            flags.append(f"note-like text (ID {uid})")
        if _AUDIT_BOOKISH_RE.search(text):
            flags.append(f"bookish 'သည်'/'သဖြင့်' (ID {uid})")
        if _AUDIT_TAGYI_RE.search(text):
            flags.append(f"'-တကြီး' typo (ID {uid})")
        if _AUDIT_AED_RE.search(text):
            flags.append(f"'အဲ့ဒီ' spelling (ID {uid})")
        if _AUDIT_ASCII_DIGITS_RE.search(text):
            flags.append(f"ASCII digits (ID {uid})")
        if "ခဲ့ပါတယ်" in text:
            khae_rows += 1
    if translated and khae_rows >= 3 and (khae_rows * 2) >= len(translated):
        flags.append(f"overused 'ခဲ့ပါတယ်' ({khae_rows}/{len(translated)})")
    if find_repeated_next_lines(translated, previous_rows):
        flags.append("repeated seam line")
    if find_near_duplicates(translated, previous_rows):
        flags.append("near-duplicate seam line")
    if _slid_runs(translated, next_units[0].get("source_text") if next_units else None):
        flags.append("slid translation run")
    # Credit / sign-off leak guard (2026-11-16): flag when the LAST
    # sentence of the batch is a source-channel credit line.
    if translated and is_credit_or_signoff(
        str(translated[-1].get("translation") or "")
    ):
        flags.append(f"credit/sign-off ending (ID {int(translated[-1]['id'])})")
    return flags


def _finalize_batch_translation(
    ai: Any,
    translated: list[dict[str, Any]],
    *,
    bi: int,
    total_batches: int,
    story_context: dict[str, Any],
    prev_committed: list[dict[str, Any]],
    nxt: list[dict[str, Any]],
    log: LogFn,
    translation_audit: bool,
    narrative_style: str,
    is_female: bool,
    voice_speed: str | float | None = None,
    max_video_slow: float | None = None,
) -> list[dict[str, Any]]:
    """Run quality/budget audit (and compress over-budget rows) for one batch."""
    enforce_budget = voice_speed is not None or max_video_slow is not None
    eff_speed = DEFAULT_SPEED if voice_speed is None else voice_speed
    eff_slow = DEFAULT_MAX_VIDEO_SLOW if max_video_slow is None else max(1.0, float(max_video_slow))
    budget_over = (
        _overbudget_rows(translated, voice_speed=eff_speed, max_video_slow=eff_slow)
        if enforce_budget
        else []
    )
    if translation_audit:
        flags = _batch_audit_flags(translated, prev_committed, nxt)
        reasons = list(flags)
        if budget_over:
            reasons.append(f"duration_overbudget ({len(budget_over)} IDs)")
        # §2B Item 7: call the paid LLM audit only when regex/structural
        # flags (or duration budget overflow) fire, keeping the Batch-1
        # (bi == 0) hook spot-check.
        if reasons or bi == 0:
            if reasons:
                log(f"🔎 Translate batch {bi + 1}/{total_batches} audit — flags: {', '.join(reasons)}")
            else:
                log(f"🔎 Translate batch 1/{total_batches} hook spot-check audit")
            audit_kw: dict[str, Any] = {
                "story_context": story_context,
                "previous_context": prev_committed,
                "next_context": nxt,
                "log": log,
                "narrative_style": narrative_style,
                "is_female": is_female,
            }
            if enforce_budget:
                audit_kw["voice_speed"] = eff_speed
                audit_kw["max_video_slow"] = eff_slow
            return audit_and_repair_batch(ai, translated, **audit_kw)
    elif budget_over:
        return _compress_overbudget_rows(
            ai,
            translated,
            story_context=story_context,
            previous_context=prev_committed,
            next_context=nxt,
            log=log,
            narrative_style=narrative_style,
            is_female=is_female,
            voice_speed=eff_speed,
            max_video_slow=eff_slow,
        )
    return translated


def translate_batches_in_parallel(
    *,
    ai: Any,
    worker_ai_factory: Callable[[], Any],
    batches: list[list[dict[str, Any]]],
    pending: list[int],
    units: list[dict[str, Any]],
    unit_index: dict[int, int],
    initial_translated: list[dict[str, Any]],
    story_context: dict[str, Any],
    source_title: str = "",
    log: LogFn,
    should_stop: Callable[[], bool],
    context_overlap: int = 6,
    lookahead: int = 6,
    translation_audit: bool = True,
    narrative_style: str = "narrator",
    is_female: bool = False,
    max_workers: int = 3,
    on_commit: Callable[[list[dict[str, Any]]], None] | None = None,
    part_index: int = 1,
    total_parts: int = 1,
    prev_part_summary: str = "",
    voice_speed: str | float | None = None,
    max_video_slow: float | None = None,
) -> list[dict[str, Any]]:
    """Batch AL-P: translate the pending batches concurrently (max 3),
    then commit them strictly in batch order.

    Phase 1 (parallel): each batch is translated on its OWN ai facade
    from ``worker_ai_factory`` — the shared ``ai`` object carries
    stateful key-pool closures and is not safe for concurrent calls.
    A batch's previous context is the previous PENDING batch's SOURCE
    units (its translation does not exist yet); the story-context
    memory is shared by all batches, and the first pending batch keeps
    the serial context (the resumed prefix's true translations).

    Phase 2 (serial commit): identical semantics to the pre-AL loop —
    audit (on flag, or every 4th batch) against TRUE neighbouring
    translations, foreign-letter drop, seam warnings, then
    ``on_commit`` with the full prefix so the checkpoint on disk is
    always a valid resume prefix. A failure surfaces only after every
    earlier batch has committed, exactly like the serial loop.
    """
    translated_all = list(initial_translated)
    if not pending:
        return translated_all
    workers = max(1, min(int(max_workers or 1), 3, len(pending)))

    def _next_units(batch: list[dict[str, Any]]) -> list[dict[str, Any]]:
        last_index = unit_index[int(batch[-1]["id"])]
        return units[last_index + 1: last_index + 1 + lookahead] if lookahead else []

    results: dict[int, list[dict[str, Any]]] = {}
    errors: dict[int, BaseException] = {}

    _part_kw = (
        {"part_index": int(part_index), "total_parts": int(total_parts), "prev_part_summary": str(prev_part_summary or "")}
        if (int(total_parts or 1) > 1 or int(part_index or 1) > 1 or bool(prev_part_summary))
        else {}
    )

    def _task(pos: int, bi: int) -> None:
        batch = batches[bi]
        if pos == 0:
            prev = translated_all[-context_overlap:] if context_overlap else []
            prev_is_source = False
        else:
            prev = batches[pending[pos - 1]][-context_overlap:] if context_overlap else []
            prev_is_source = True
        try:
            if should_stop():
                raise InterruptedError("STOP requested during timestamp translation")
            log(f"🌐 Translate batch {bi + 1}/{len(batches)} — IDs {batch[0]['id']:05d}–{batch[-1]['id']:05d}")
            results[bi] = translate_batch(
                worker_ai_factory(),
                source_title=source_title,
                story_context=story_context,
                previous_context=prev,
                current=batch,
                next_context=_next_units(batch),
                log=log,
                narrative_style=narrative_style,
                is_female=is_female,
                previous_context_is_source=prev_is_source,
                **_part_kw,
            )
        except BaseException as exc:
            errors[bi] = exc

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="recap-plan") as pool:
        futures = [pool.submit(_task, pos, bi) for pos, bi in enumerate(pending)]
        for fut in futures:
            fut.result()

    for pos, bi in enumerate(pending):
        batch = batches[bi]
        if bi in errors:
            exc = errors[bi]
            if isinstance(exc, GeminiKeyPoolExhaustedError):
                saved = len(translated_all)
                remaining = len(units) - saved
                log(f"💾 Translation checkpoint safe — {saved}/{len(units)} units saved; {remaining} remaining")
                raise GeminiKeyPoolExhaustedError(
                    f"{exc}\n\nSaved progress: {saved}/{len(units)} units; "
                    f"remaining: {remaining}. Resume will start at ID {int(batch[0]['id']):05d}."
                ) from exc
            raise exc
        if should_stop():
            raise InterruptedError("STOP requested during timestamp translation")
        translated = results[bi]
        prev_committed = translated_all[-context_overlap:] if context_overlap else []
        nxt = _next_units(batch)
        translated = _finalize_batch_translation(
            ai,
            translated,
            bi=bi,
            total_batches=len(batches),
            story_context=story_context,
            prev_committed=prev_committed,
            nxt=nxt,
            log=log,
            translation_audit=translation_audit,
            narrative_style=narrative_style,
            is_female=is_female,
            voice_speed=voice_speed,
            max_video_slow=max_video_slow,
        )
        translated = _drop_foreign_letters(translated, log)
        for row in translated:
            if _latin_words(row.get("translation")):
                log(f"⚠️ ID {int(row['id']):05d}: English words kept ({', '.join(_latin_words(row['translation']))}); "
                    "Edit Text can fix the line")
        for a, b in find_repeated_next_lines(translated, translated_all[-1:]) + find_near_duplicates(translated, translated_all[-2:]):
            log(f"⚠️ IDs {a:05d}/{b:05d} may say the same thing twice; Edit Text can fix the lines")
        translated_all.extend(translated)
        if on_commit is not None:
            on_commit(list(translated_all))
    return translated_all


def _job_segmentation_profile(state_file: Path, translations_file: Path) -> str:
    """Whole sentences for a new job; a job whose translation was started with 4-6 s lines keeps them."""
    for path in (translations_file,):
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(saved.get("segmentation_profile") or "") == SHORT_SEGMENTATION_PROFILE:
            return SHORT_SEGMENTATION_PROFILE
    return SEGMENTATION_PROFILE


class RecapPlanBuilder:
    """English narrator video → timestamp-locked Burmese narration plan (narrator-only source)."""

    def __init__(
        self,
        *,
        runtime_root: str,
        gemini_env_path: str = "",
        ffprobe_path: str,
        log: LogFn,
        progress: ProgressFn,
        should_stop: Callable[[], bool],
        translation_batch_max_items: int = 40,
        story_context_batch_max_items: int = 80,
        context_overlap: int = 6,
        lookahead: int = 6,
        translation_audit: bool = True,
        atomic_max_duration: float = 5.6,
        atomic_target_duration: float = 4.2,
        gemini_request_timeout_seconds: int = 60,
        gemini_text_model: str = "gemini-3.6-flash",
        narrative_style: str = "narrator",
        is_female: bool = False,
        source_language: str = "",
        translate_workers: int | None = None,
        part_index: int = 1,
        total_parts: int = 1,
        prev_part_summary: str = "",
        voice_speed: str | float | None = DEFAULT_SPEED,
        max_video_slow: float = DEFAULT_MAX_VIDEO_SLOW,
    ):
        self.runtime_root = Path(runtime_root).resolve()
        self.gemini_env_path = Path(gemini_env_path).expanduser().resolve() if str(gemini_env_path or "").strip() else None
        self.ffprobe = ffprobe_path
        # Prefer ffmpeg next to ffprobe; fallback to PATH.
        fp = Path(ffprobe_path)
        sibling = fp.with_name("ffmpeg.exe" if fp.suffix.lower() == ".exe" else "ffmpeg")
        self.ffmpeg = str(sibling) if sibling.is_file() else (shutil.which("ffmpeg") or "ffmpeg")
        self.log = log
        self.progress = progress
        self.should_stop = should_stop
        self.narrative_style = str(narrative_style or "narrator").lower().strip()
        self.is_female = bool(is_female)
        # User-picked STT language ("" = auto-detect via WHISPER_LANGUAGE).
        self.source_language = normalize_source_language(source_language)
        self.part_index = max(1, int(part_index or 1))
        self.total_parts = max(1, int(total_parts or 1))
        self.prev_part_summary = str(prev_part_summary or "").strip()
        self.translation_batch_max_items = max(5, int(translation_batch_max_items))
        self.story_context_batch_max_items = max(10, int(story_context_batch_max_items))
        self.context_overlap = max(0, int(context_overlap))
        self.lookahead = max(0, int(lookahead))
        self.translation_audit = bool(translation_audit)
        self.atomic_max_duration = max(4.0, float(atomic_max_duration))
        self.atomic_target_duration = max(2.5, min(float(atomic_target_duration), self.atomic_max_duration))
        self.gemini_request_timeout_seconds = max(30, min(int(gemini_request_timeout_seconds), 600))
        self.gemini_text_model = _clean(gemini_text_model) or "gemini-3.6-flash"
        # Batch AL-P: parallel translate batches (hard cap 3 — the
        # Gemini key pool is the real limit). RECAP_PLANNER_TRANSLATE_WORKERS=1
        # restores the exact pre-AL serial loop.
        if translate_workers is None:
            _env_tw = str(os.getenv("RECAP_PLANNER_TRANSLATE_WORKERS", "") or "").strip()
            try:
                translate_workers = int(_env_tw) if _env_tw else 3
            except ValueError:
                translate_workers = 3
        self.translate_workers = max(1, min(int(translate_workers), 3))
        if isinstance(voice_speed, (int, float)) and not isinstance(voice_speed, bool):
            self.voice_speed: str | float = max(0.5, float(voice_speed))
        else:
            self.voice_speed = str(voice_speed or DEFAULT_SPEED).strip() or DEFAULT_SPEED
        self.max_video_slow = max(1.0, float(max_video_slow or DEFAULT_MAX_VIDEO_SLOW))

    def _duration_budget_key(self) -> str:
        return (
            f"tempo:{_resolve_tempo(self.voice_speed):.4f}"
            f"|slow:{self.max_video_slow:.4f}"
            f"|cps:{MEASURED_CHARS_PER_SECOND:.2f}"
        )

    def _validate_runtime(self) -> None:
        required = [
            self.runtime_root / "services" / "whisper_service.py",
            self.runtime_root / "services" / "ai_service.py",
        ]
        missing = [str(p) for p in required if not p.is_file()]
        if missing:
            raise FileNotFoundError("Bundled runtime မပြည့်စုံပါ:\n" + "\n".join(missing))

    @staticmethod
    def _release_whisper_gpu(whisper_obj: Any) -> None:
        try:
            if hasattr(whisper_obj, "_model"):
                whisper_obj._model = None
        except Exception:
            pass
        try:
            del whisper_obj
        except Exception:
            pass
        gc.collect()

    def build(self, *, source: Path, job_dir: Path, source_title: str = "", reuse: bool = True) -> tuple[Path, dict[str, Any]]:
        self._validate_runtime()
        source = source.resolve()
        job_dir.mkdir(parents=True, exist_ok=True)
        whisper_model = WHISPER_MODEL
        whisper_language = self.source_language or WHISPER_LANGUAGE
        transcript_file = job_dir / "whisper_transcript.json"
        transcript_text_file = job_dir / "whisper_transcript.txt"
        atomic_file = job_dir / "atomic_units.json"
        context_file = job_dir / "story_context.json"
        context_checkpoint_file = job_dir / "story_context_checkpoint.json"
        translations_file = job_dir / "timestamp_translations.json"
        plan_file = job_dir / "smart_sync_plan.json"
        state_file = job_dir / "planner_checkpoint.json"
        fp = _source_fingerprint_for_mode(
            source,
            PLANNER_MODE
            + "|"
            + TRANSLATION_DIRECTION
            + f"|whisper:{whisper_model}:{whisper_language}|style:{self.narrative_style}|female:{int(self.is_female)}",
        )
        segmentation_profile = _job_segmentation_profile(state_file, translations_file)
        sentence_lines = segmentation_profile == SEGMENTATION_PROFILE

        if reuse and plan_file.is_file() and transcript_file.is_file() and state_file.is_file():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                plan = json.loads(plan_file.read_text(encoding="utf-8"))
                if (
                    state.get("source_fingerprint") == fp
                    and state.get("plan_ready") is True
                    and state.get("planner_mode") == PLANNER_MODE
                    and plan.get("planner_mode") == PLANNER_MODE
                    and str(state.get("segmentation_profile") or "") == segmentation_profile
                    and str(plan.get("segmentation_profile") or "") == segmentation_profile
                    and str(state.get("boundary_profile") or "") == BOUNDARY_PROFILE
                    and str(plan.get("boundary_profile") or "") == BOUNDARY_PROFILE
                    and str(state.get("duration_budget_key") or "") == self._duration_budget_key()
                ):
                    if _plan_has_speakable_narration(plan):
                        self.log("♻️ Timestamp transcript + Translation plan reuse")
                        self.progress(4, 4, "Plan reused")
                        return plan_file, plan
                    self.log("⚠️ Cached Smart Sync plan contains non-speech/placeholder narration; rebuilding translation from safe checkpoint")
            except Exception:
                pass

        stage_timings: dict[str, float] = {}
        t_transcribe_start = time.monotonic()
        duration = _probe_video_duration(self.ffprobe, source)
        audio_duration = _probe_audio_duration(self.ffprobe, source)
        self.log(f"🎬 Source: {source}")
        self.log(f"⏱️ Video duration (MASTER): {duration:.3f}s")
        if audio_duration is not None:
            self.log(f"🎧 Source audio duration: {audio_duration:.3f}s")
            if audio_duration > duration + 1.0:
                self.log(f"⚠️ Audio tail detected: +{audio_duration-duration:.3f}s beyond video; transcription audio preparation will trim it.")

        whisper_mod = _load_module("timestamp_whisper_service", self.runtime_root / "services" / "whisper_service.py")
        ai_mod = _load_module("timestamp_ai_service", self.runtime_root / "services" / "ai_service.py")

        env_file = self.gemini_env_path
        merged_env: dict[str, str] = {}
        if env_file and env_file.is_file():
            from dotenv import dotenv_values, load_dotenv
            load_dotenv(env_file, override=True)
            self.log(f"🔑 Gemini .env loaded: {env_file}")
            for k, v in dotenv_values(env_file).items():
                if v:
                    merged_env[str(k).strip()] = str(v).strip()

        # Also pull from os.environ (essential for Render Cloud, Docker, and Linux containers)
        for k, v in os.environ.items():
            k_clean = str(k).strip()
            if v and k_clean not in merged_env:
                merged_env[k_clean] = str(v).strip()

        # Audit fix E: the shared union pool (planner/TTS/group admin
        # all resolve the same keys now); semantics identical to the
        # inline construction this replaces.
        key_pool = _collect_gemini_key_pool(merged_env)

        if not key_pool:
            raise RuntimeError(
                "Gemini API key မတွေ့ပါ။ .env သို့မဟုတ် Environment Variables ထဲတွင် GEMINI_API_KEY=... "
                "(သို့) GEMINI_IMAGE_PROJECT_1_KEY, GEMINI_IMAGE_PROJECT_2_KEY ... ထည့်ပေးပါ။"
            )
        os.environ["GEMINI_API_KEY"] = key_pool[0][1]
        os.environ["GEMINI_TEXT_MODEL"] = self.gemini_text_model
        has_groq = bool(os.getenv("GROQ_API_KEYS") or os.getenv("GROQ_API_KEY"))
        whisper_label = "Groq Cloud Whisper Turbo (128x Speed)" if has_groq else f"local Whisper ({whisper_model})"
        self.log(f"🔑 Gemini key pool ready — {len(key_pool)} key(s) | speech engine: {whisper_label}")

        transcript = None
        if reuse and transcript_file.is_file() and state_file.is_file():
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
                if state.get("source_fingerprint") == fp and state.get("transcript_ready") is True:
                    candidate = json.loads(transcript_file.read_text(encoding="utf-8"))
                    c_backend = str(candidate.get("backend") or "")
                    c_model = str(candidate.get("model") or "")
                    backend_valid = c_backend in {"faster-whisper", "openai-whisper", "groq-whisper"}
                    model_valid = (
                        c_model in {whisper_model, "whisper-large-v3-turbo", "whisper-large-v3"}
                        or c_backend == "groq-whisper"
                    )
                    if (
                        backend_valid
                        and model_valid
                        and str(candidate.get("language") or "").lower().startswith(whisper_language.lower())
                    ):
                        transcript = candidate
                        if not transcript_text_file.is_file():
                            transcript_text_file.write_text(str(transcript.get("text") or ""), encoding="utf-8")
                        self.log(f"♻️ Full transcript reuse ({c_backend} / {c_model})")
            except Exception:
                transcript = None

        if transcript is None:
            if self.should_stop():
                raise InterruptedError("STOP requested before local Whisper")
            runtime_cfg = _load_runtime_config(self.runtime_root)
            engine_label = "Groq Cloud Whisper Turbo (128x Speed)" if has_groq else f"Local Whisper ({whisper_model})"
            whisper_lang_arg = None if str(whisper_language).lower().strip() in ("auto", "none", "") else whisper_language
            self.log(f"🌐 Multilingual Audio → Myanmar Recap | Speech Engine: {engine_label} | lang={whisper_language}")
            self.progress(0, 4, "Speech timestamp transcription")
            self.log(f"🎙️ [1/4] {engine_label} — video timestamp transcript...")
            whisper = whisper_mod.WhisperTimestampService(
                model_size=whisper_model,
                backend=str(runtime_cfg.get("whisper_backend", os.getenv("WHISPER_BACKEND", "auto"))),
                device=str(runtime_cfg.get("whisper_device", os.getenv("WHISPER_DEVICE", "cuda"))),
                compute_type=str(runtime_cfg.get("whisper_compute_type", os.getenv("WHISPER_COMPUTE_TYPE", "float16"))),
                language=whisper_lang_arg,
                log=self.log,
            )
            whisper_media = _prepare_whisper_audio(self.ffmpeg, source, job_dir, duration, self.log)
            transcript = whisper.transcribe(str(whisper_media), duration)
            transcript["source_title"] = source_title or source.stem
            transcript["backend"] = transcript.get("backend") or ("groq-whisper" if has_groq else "faster-whisper")
            transcript["model"] = transcript.get("model") or ("whisper-large-v3-turbo" if has_groq else whisper_model)
            detected_lang = transcript.get("language") or "auto"
            self.log(f"🎧 Spoken language detected in video: {detected_lang.upper()}")
            transcript["language"] = detected_lang
            _atomic_json(transcript_file, transcript)
            transcript_text_file.write_text(str(transcript.get("text") or ""), encoding="utf-8")
            self.log(f"✅ Whisper raw segments: {len(transcript.get('segments') or [])}")
            self.log(f"📝 Raw transcript: {transcript_text_file.name}")
            self._release_whisper_gpu(whisper)

        _check_english_speech(transcript, transcript_text_file.name)   # before any Gemini request
        self.log("✅ Narrator source: all transcript speech kept; original V2 Smart Sync boundaries")
        self.log("✂️ Lines: whole sentences (a line ends at a sentence end)" if sentence_lines else
                 "✂️ Lines: 4-6 s (this job was started that way; a new job uses whole sentences)")
        units = atomicize_timestamp_segments(
            transcript,
            max_duration=self.atomic_max_duration,
            target_duration=self.atomic_target_duration,
            sentence_locked=sentence_lines,
        )
        units = _filter_local_whisper_artifact_units(units, transcript, log=self.log)
        if not units:
            raise RuntimeError("Timestamp transcript units မရှိပါ။")
        units = assign_full_coverage_boundaries(units, duration)
        _atomic_json(atomic_file, {
            "planner_mode": PLANNER_MODE,
            "segmentation_profile": segmentation_profile,
            "units": units,
        })
        forced_units = sum(1 for unit in units if unit.get("forced_boundary"))
        durations = [max(0.0, float(unit["speech_end"]) - float(unit["speech_start"])) for unit in units]
        avg_duration = (sum(durations) / len(durations)) if durations else 0.0
        self.log(
            f"🧩 V2-style local sync units: {len(units)} | avg={avg_duration:.2f}s | "
            + (f"whole sentences (max {SENTENCE_UNIT_HARD:.0f}s)" if sentence_lines else
               f"target={self.atomic_target_duration:.1f}s max={self.atomic_max_duration:.1f}s")
            + f" | forced={forced_units}"
        )

        _atomic_json(state_file, {
            "version": 5,
            "planner_mode": PLANNER_MODE,
            "segmentation_profile": segmentation_profile,
            "boundary_profile": BOUNDARY_PROFILE,
            "source_fingerprint": fp,
            "source_path": str(source),
            "translation_direction": TRANSLATION_DIRECTION,
            "transcript_backend": str(transcript.get("backend") or "faster-whisper"),
            "transcript_ready": True,
            "plan_ready": False,
        })

        if self.should_stop():
            raise InterruptedError("STOP requested after timestamp transcription")
        stage_timings["transcribe"] = round(max(0.0, time.monotonic() - t_transcribe_start), 3)

        part_idx_val = max(1, int(self.part_index or 1))
        total_parts_val = max(1, int(self.total_parts or 1))
        part_kw = (
            {"part_index": part_idx_val, "total_parts": total_parts_val, "prev_part_summary": self.prev_part_summary}
            if (total_parts_val > 1 or part_idx_val > 1 or bool(self.prev_part_summary))
            else {}
        )

        with _pushd(self.runtime_root):
            ai = ai_mod.AIService()
            self.log(f"🤖 Gemini primary text model: {ai.text_model}")
            _configure_gemini_key_pool(
                ai,
                key_pool,
                client_factory=ai_mod.genai.Client,
                log=self.log,
                request_timeout_seconds=self.gemini_request_timeout_seconds,
            )

            self.progress(1, 4, "Story context memory")
            self.log("🧠 [2/4] Gemini Story Context Memory pass...")
            t_context_start = time.monotonic()
            context_source_fp = fp + "|seg:" + segmentation_profile
            story_context = build_story_context(
                ai, units,
                source_title=source_title,
                batch_max_items=self.story_context_batch_max_items,
                log=self.log,
                should_stop=self.should_stop,
                checkpoint_path=context_checkpoint_file,
                source_fingerprint=context_source_fp,
                reuse=reuse,
                **part_kw,
            )
            stage_timings["context"] = round(max(0.0, time.monotonic() - t_context_start), 3)
            _atomic_json(context_file, {
                "planner_mode": PLANNER_MODE,
                "source_fingerprint": context_source_fp,
                "context": story_context,
            })

            self.progress(2, 4, "Timestamp translation")
            self.log("🌐 [3/4] Gemini English → Myanmar translation — narrator source | V2 Smart Sync | no summary / no shortening...")
            t_translate_start = time.monotonic()
            batches = _batch_by_size(units, max_items=self.translation_batch_max_items, max_chars=16000)
            translated_all: list[dict[str, Any]] = []
            if reuse and translations_file.is_file():
                try:
                    saved = json.loads(translations_file.read_text(encoding="utf-8"))
                    saved_items = saved.get("items") or []
                    if (saved.get("planner_mode") == PLANNER_MODE and saved.get("source_fingerprint") == fp
                            and str(saved.get("segmentation_profile") or "") == segmentation_profile
                            and isinstance(saved_items, list)):
                        saved_by_id: dict[int, dict[str, Any]] = {}
                        for row in saved_items:
                            if not isinstance(row, dict):
                                continue
                            try:
                                uid = int(row.get("id"))
                            except Exception:
                                continue
                            saved_by_id.setdefault(uid, row)

                        safe_prefix: list[dict[str, Any]] = []
                        bad_id: int | None = None
                        for src in units:
                            uid = int(src["id"])
                            saved_row = saved_by_id.get(uid)
                            if saved_row is None:
                                bad_id = uid
                                break
                            if _clean(saved_row.get("source_text")) != _clean(src.get("source_text")):
                                bad_id = uid
                                break
                            saved_ts = _clean(saved_row.get("timestamp"))
                            if saved_ts and saved_ts != _clean(src.get("timestamp")):
                                bad_id = uid
                                break
                            tr = _sanitize_translation_text(saved_row.get("translation"))
                            if not _has_speakable_content(tr):
                                bad_id = uid
                                break
                            clean_saved = dict(saved_row)
                            clean_saved["translation"] = tr
                            if src.get("video_start") is not None and src.get("video_end") is not None:
                                clean_saved["video_start"] = src["video_start"]
                                clean_saved["video_end"] = src["video_end"]
                            safe_prefix.append(clean_saved)

                        translated_all = safe_prefix
                        ignored_obsolete = max(0, len(saved_items) - len(safe_prefix))
                        if len(safe_prefix) == len(units) and ignored_obsolete:
                            self.log(
                                f"♻️ Translation checkpoint sanitized — reusing all {len(safe_prefix)} valid unit(s); "
                                f"ignoring {ignored_obsolete} obsolete ASR-artifact row(s)"
                            )
                        elif bad_id is not None:
                            self.log(
                                f"⚠️ Translation checkpoint stops before ID {bad_id:05d}; "
                                "resume will regenerate from that batch"
                            )
                        if translated_all:
                            self.log(f"♻️ Timestamp Translation resume — {len(translated_all)}/{len(units)} safe units already done")
                except Exception as exc:
                    self.log(f"⚠️ Translation checkpoint ignored: {exc}")
                    translated_all = []

            unit_index = {int(x["id"]): idx for idx, x in enumerate(units)}

            def _write_translations_checkpoint(rows: list[dict[str, Any]]) -> None:
                _atomic_json(translations_file, {
                    "planner_mode": PLANNER_MODE,
                    "segmentation_profile": segmentation_profile,
                    "source_fingerprint": fp,
                    "translation_direction": TRANSLATION_DIRECTION,
                    "duration_budget_key": self._duration_budget_key(),
                    "completed": len(rows),
                    "total": len(units),
                    "items": list(rows),
                })

            def _make_worker_ai() -> Any:
                # Batch AL-P: a shallow copy of the shared ai carrying
                # its OWN key-pool closures — _gemini_json drives
                # ai.client/ai.text_model through those closures, so
                # concurrent translate batches must never share one
                # ai object.
                facade = copy.copy(ai)
                _configure_gemini_key_pool(
                    facade,
                    key_pool,
                    client_factory=ai_mod.genai.Client,
                    log=self.log,
                    request_timeout_seconds=self.gemini_request_timeout_seconds,
                )
                return facade

            completed_ids = {int(x["id"]) for x in translated_all}
            pending: list[int] = []
            for bi0, batch in enumerate(batches):
                batch_ids = [int(x["id"]) for x in batch]
                if all(uid in completed_ids for uid in batch_ids):
                    continue
                if any(uid in completed_ids for uid in batch_ids):
                    # Checkpoints are written at complete-batch boundaries; a partial batch is unsafe to reuse.
                    translated_all = [x for x in translated_all if int(x["id"]) < batch_ids[0]]
                    completed_ids = {int(x["id"]) for x in translated_all}
                pending.append(bi0)

            if translated_all and _overbudget_rows(
                translated_all, voice_speed=self.voice_speed, max_video_slow=self.max_video_slow
            ):
                row_pos = {int(r["id"]): idx for idx, r in enumerate(translated_all)}
                for bi0, batch in enumerate(batches):
                    batch_ids = [int(x["id"]) for x in batch]
                    if not all(uid in row_pos for uid in batch_ids):
                        continue
                    batch_rows = [translated_all[row_pos[uid]] for uid in batch_ids]
                    if not _overbudget_rows(
                        batch_rows, voice_speed=self.voice_speed, max_video_slow=self.max_video_slow
                    ):
                        continue
                    first_pos = row_pos[batch_ids[0]]
                    last_index = unit_index[batch_ids[-1]]
                    prev_rows = translated_all[max(0, first_pos - self.context_overlap):first_pos] if self.context_overlap else []
                    nxt_rows = units[last_index + 1:last_index + 1 + self.lookahead] if self.lookahead else []
                    repaired_batch = _finalize_batch_translation(
                        ai,
                        batch_rows,
                        bi=bi0,
                        total_batches=len(batches),
                        story_context=story_context,
                        prev_committed=prev_rows,
                        nxt=nxt_rows,
                        log=self.log,
                        translation_audit=self.translation_audit,
                        narrative_style=self.narrative_style,
                        is_female=self.is_female,
                        voice_speed=self.voice_speed,
                        max_video_slow=self.max_video_slow,
                    )
                    for r in repaired_batch:
                        translated_all[row_pos[int(r["id"])]] = r
                    _write_translations_checkpoint(translated_all)

            if self.translate_workers > 1 and len(pending) > 1:
                translated_all = translate_batches_in_parallel(
                    ai=ai,
                    worker_ai_factory=_make_worker_ai,
                    batches=batches,
                    pending=pending,
                    units=units,
                    unit_index=unit_index,
                    initial_translated=translated_all,
                    story_context=story_context,
                    source_title=source_title,
                    log=self.log,
                    should_stop=self.should_stop,
                    context_overlap=self.context_overlap,
                    lookahead=self.lookahead,
                    translation_audit=self.translation_audit,
                    narrative_style=self.narrative_style,
                    is_female=self.is_female,
                    max_workers=self.translate_workers,
                    on_commit=_write_translations_checkpoint,
                    voice_speed=self.voice_speed,
                    max_video_slow=self.max_video_slow,
                    **part_kw,
                )
            else:
                for bi0 in pending:
                    bi = bi0 + 1
                    batch = batches[bi0]
                    batch_ids = [int(x["id"]) for x in batch]
                    if self.should_stop():
                        raise InterruptedError("STOP requested during timestamp translation")
                    last_index = unit_index[int(batch[-1]["id"])]
                    prev = translated_all[-self.context_overlap:] if self.context_overlap else []
                    nxt = units[last_index + 1:last_index + 1 + self.lookahead] if self.lookahead else []
                    self.log(f"🌐 Translate batch {bi}/{len(batches)} — IDs {batch[0]['id']:05d}–{batch[-1]['id']:05d}")
                    try:
                        translated = translate_batch(
                            ai,
                            source_title=source_title,
                            story_context=story_context,
                            previous_context=prev,
                            current=batch,
                            next_context=nxt,
                            log=self.log,
                            narrative_style=self.narrative_style,
                            is_female=self.is_female,
                            **part_kw,
                        )
                    except GeminiKeyPoolExhaustedError as exc:
                        saved = len(translated_all)
                        remaining = len(units) - saved
                        self.log(f"💾 Translation checkpoint safe — {saved}/{len(units)} units saved; {remaining} remaining")
                        raise GeminiKeyPoolExhaustedError(
                            f"{exc}\n\nSaved progress: {saved}/{len(units)} units; "
                            f"remaining: {remaining}. Resume will start at ID {batch_ids[0]:05d}."
                        ) from exc
                    translated = _finalize_batch_translation(
                        ai,
                        translated,
                        bi=bi0,
                        total_batches=len(batches),
                        story_context=story_context,
                        prev_committed=prev,
                        nxt=nxt,
                        log=self.log,
                        translation_audit=self.translation_audit,
                        narrative_style=self.narrative_style,
                        is_female=self.is_female,
                        voice_speed=self.voice_speed,
                        max_video_slow=self.max_video_slow,
                    )
                    translated = _drop_foreign_letters(translated, self.log)
                    for row in translated:
                        if _latin_words(row.get("translation")):
                            self.log(f"⚠️ ID {int(row['id']):05d}: English words kept ({', '.join(_latin_words(row['translation']))}); "
                                     "Edit Text can fix the line")
                    for a, b in find_repeated_next_lines(translated, translated_all[-1:]) + find_near_duplicates(translated, translated_all[-2:]):
                        self.log(f"⚠️ IDs {a:05d}/{b:05d} may say the same thing twice; Edit Text can fix the lines")
                    translated_all.extend(translated)
                    completed_ids.update(int(x["id"]) for x in translated)
                    _write_translations_checkpoint(translated_all)


        if [int(x["id"]) for x in translated_all] != [int(x["id"]) for x in units]:
            raise RuntimeError("Final translation ID sequence mismatch")
        stage_timings["translate"] = round(max(0.0, time.monotonic() - t_translate_start), 3)

        self.progress(3, 4, "Build Smart Sync plan")
        self.log("🧩 [4/4] V2 Smart Sync boundaries → full source coverage + midpoint gap split...")
        t_plan_start = time.monotonic()
        plan = make_translation_plan(
            translated_all,
            duration,
            source_title=source_title or source.stem,
            transcript=transcript,
            story_context=story_context,
        )
        plan["standalone_source_path"] = str(source)
        plan["segmentation_profile"] = segmentation_profile
        plan["boundary_profile"] = BOUNDARY_PROFILE
        plan["translation_direction"] = TRANSLATION_DIRECTION
        plan["narrative_style"] = self.narrative_style
        plan["source_language"] = str(transcript.get("language") or "en")
        plan["target_language"] = "mm"
        plan["translation_batch_count"] = len(batches)
        plan["atomic_unit_count"] = len(translated_all)
        plan["part_index"] = part_idx_val
        plan["total_parts"] = total_parts_val
        _tail_lines = [str(r.get("translation") or "").strip() for r in translated_all[-2:] if str(r.get("translation") or "").strip()]
        _plot_tail = [str(p).strip() for p in (story_context.get("plot_state") or [])[-2:] if str(p).strip()]
        plan["part_summary"] = " | ".join(_plot_tail + _tail_lines)[:380]
        stage_timings["plan"] = round(max(0.0, time.monotonic() - t_plan_start), 3)
        plan["stage_timings"] = stage_timings
        plan["translation_rules"] = {
            "task": "translate",
            "summarize": False,
            "shorten": False,
            "merge_ids": False,
            "delete_ids": False,
            "reorder_ids": False,
            "natural_expansion_allowed": True,
            "new_plot_invention_allowed": False,
            "natural_boundary_preferred": True,
            "local_time_cap_may_split_long_sentence": True,
            "v2_local_sync_anchors": True,
            "sync_target_seconds": self.atomic_target_duration,
            "sync_max_seconds": self.atomic_max_duration,
            "context_read_only_for_each_id": True,
            "adjacent_duplicate_rejected": True,
            "mandatory_per_id_alignment_audit": bool(self.translation_audit),
            "timestamp_authority": "local-whisper",
            "boundary_profile": BOUNDARY_PROFILE,
            "spoken_duration_budget_enforced": True,
            "duration_budget_estimator": "measured_chars_per_second",
            "duration_budget_chars_per_second": MEASURED_CHARS_PER_SECOND,
            "duration_budget_max_video_slow": self.max_video_slow,
            "duration_budget_voice_speed": self.voice_speed,
            "duration_budget_tempo": _resolve_tempo(self.voice_speed),
        }
        _atomic_json(plan_file, plan)
        _atomic_json(state_file, {
            "version": 5,
            "planner_mode": PLANNER_MODE,
            "segmentation_profile": segmentation_profile,
            "boundary_profile": BOUNDARY_PROFILE,
            "duration_budget_key": self._duration_budget_key(),
            "source_fingerprint": fp,
            "source_path": str(source),
            "translation_direction": TRANSLATION_DIRECTION,
            "transcript_ready": True,
            "context_ready": True,
            "translation_ready": True,
            "plan_ready": True,
            "atomic_units": len(translated_all),
        })
        self.progress(4, 4, "Plan ready")
        self.log(f"✅ smart_sync_plan.json READY — V2-style local sync units={len(plan['segments'])}")
        return plan_file, plan

"""Shared Hugging Face ZeroGPU pool ledger (`bot_settings` key `aj_hf:pool_state`).

Phase 1 HF Pool Implementation (2026-11-24):
- Zero DB schema migration: shared state lives in `bot_settings` under key
  `aj_hf:pool_state`, keyed strictly by irreversible SHA-256 hex digests — raw
  token values are NEVER stored, printed, or logged.
- Exact persisted JSON shape:
  {
    "tokens": {
      "<sha256-digest>": {
        "status": "ok|auth_dead",
        "remaining_s": 0.0,
        "base_s": 0.0,
        "resets_at": "...",
        "cooldown_until": "...",
        "updated_at": "..."
      }
    },
    "space_gates": {
      "<space>": 94
    },
    "in_flight_gpu_s": 0
  }
- Both `vcp-web` and `vcp-worker` read/write this ledger with a 30 s TTL reload
  (eliminating cross-process blindness).
- Auth-dead quarantine: on 401/403, confirms once via `/api/whoami-v2`; if still
  rejected, marks `auth_dead` for 24 h and excludes from all walks. Auto-clears
  if the configured token set changes or a subsequent probe returns 200.
- Watermark bands (>40% Normal, 40%->20% Conservation, 20%->8% Short-job priority,
  <8% Dry gate), pre-job quota estimation & soft reservation, and refill slope ETA.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import math
import re
import threading
import time
from typing import Any, Callable, Mapping, Sequence
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

from app.services.key_rotation import (
    botsetting_sync_delete,
    botsetting_sync_mutate,
    botsetting_sync_read,
    botsetting_sync_write,
    key_id as _short_key_id,
)

logger = logging.getLogger(__name__)

POOL_STATE_STORE_KEY = "aj_hf:pool_state"
IN_FLIGHT_STORE_KEY = "aj_hf:in_flight"
ENV_HASH_STORE_KEY = "aj_hf:env_hash"
TRUTH_TS_STORE_KEY = "aj_hf:truth_ts"
TOKEN_META_STORE_KEY = "aj_hf:token_meta"
DRIFT_WINDOW_STORE_KEY = "aj_hf:drift_window"
TRUTH_TS_RETENTION_SECONDS = 172800.0  # 48 hours
TOKEN_META_RETENTION_SECONDS = 604800.0  # 7 days
DRIFT_WINDOW_RETENTION_SECONDS = 86400.0  # 24 hours rolling drift window

# Pro ZeroGPU subscription lapse boundary (2026-12-01T00:00:00Z).
PRO_LAPSE_AT_UTC = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
UNCERTAIN_STALE_DRAINED_REM_S = 45.0
UNCERTAIN_STALE_DRAINED_CEIL_S = 60.0
UNCERTAIN_STALE_DRAINED_AGE_S = 6.0 * 3600.0
UNCERTAIN_AGED_PARTIAL_DELTA_S = 5.0
UNCERTAIN_AGED_PARTIAL_AGE_S = 18.0 * 3600.0
UNCERTAIN_NEVER_VERIFIED_AGE_S = 24.0 * 3600.0
CERTAIN_VERIFIED_MIN_REM_S = 295.0
PRIORITY_HIGH_REM_THRESHOLD_S = 137.0

CACHE_TTL_SECONDS = 30.0
AUTH_DEAD_QUARANTINE_SECONDS = 86400.0  # 24 hours
QUOTA_DEAD_COOLDOWN_SECONDS = 1800.0    # 30 minutes
REFUSAL_CEILING_TTL_SECONDS = 3600.0    # 60 minutes per-token scoped refusal ceiling (Part P1)
REFUSAL_REFILL_CLEAR_DELTA_S = 25.0     # >= 25 s probed increase clears refusal ceiling (Part P1.4)
SESSION_READY_MIN_GPU_S = 145.0         # Admin key-health session-ready threshold (Part Q4.3)
SPACE_TIMEOUT_COOLDOWN_SECONDS = 600.0  # 10 minutes per-Space timeout circuit breaker (Part C1)
IN_FLIGHT_STALE_SECONDS = 7200.0        # 2 hours safety GC
MIN_USABLE_DUST_GATE_S = 25.0           # <25 s "quota dust" never counts as capacity
DEFAULT_SPACE_GATE_S = 60.0             # fallback only when space_gates has no entry yet
DEFAULT_TOKEN_BASE_S = 300.0            # ~5 min free-account base when unprobed
PRE_JOB_SAFETY_MULTIPLIER = 1.20
RESUMER_SAFETY_MULTIPLIER = 1.25
REFILL_ETA_MIN_HORIZON_SECONDS = 900.0    # +15 min floor when deficit <= 0 (D2-A)
REFILL_ETA_MAX_HORIZON_SECONDS = 3600.0   # +60 min horizon cap (D2-A)
CONGESTION_ETA_MIN_HORIZON_SECONDS = 600.0   # +10 min floor for Free congestion parks
CONGESTION_ETA_MAX_HORIZON_SECONDS = 1200.0  # +20 min cap for Free congestion parks
CONGESTION_RESUMER_INTERVAL_SECONDS = 600.0  # matches voice_choice.RESUMER_INTERVAL_SECONDS

# Session-slice `/generate_session` dynamic GPU gate pricing (2026-11-26 / 2026-11-27):
# Runtime / reservation debit: `min(120, 18 + ceil(chars * 0.085))` GPU-seconds (x1.0, unfloored).
# Admission door check (Part P2 + 30 s Space declaration floor):
#   `declared_s = max(30.0, session_call_gate_s(chars))`
#   `min(180.0, ceil(declared_s * 1.5))` GPU-seconds.
SESSION_CALL_BASE_GPU_S = 18
SESSION_CALL_PER_CHAR_GPU_S = 0.085
SESSION_CALL_MAX_GPU_S = 120
SESSION_DECLARED_MIN_GPU_S = 30.0
SESSION_ADMISSION_GATE_MULTIPLIER = 1.5
SESSION_ADMISSION_MAX_GPU_S = 180.0
SESSION_FLOOR_REGIME_MAX_CHARS = 141
SESSION_STATIC_120_MIN_REQUESTED_S = 175.0
SESSION_STATIC_120_MAX_FORMULA_S = 150.0

LEGACY_CALL_BASE_GPU_S = 18
LEGACY_CALL_PER_CHAR_GPU_S = 0.15
LEGACY_DECLARED_MIN_GPU_S = 25.0
LEGACY_DECLARED_MAX_GPU_S = 55.0
GPU_REPORTED_OVERHEAD_PAD_S = 1.0
MIN_REPORTED_GPU_DEBIT_S = 1.0


def session_call_gate_s(chars: int) -> float:
    """Return the GPU-second runtime/debit gate for a `/generate_session` call of `chars` characters:
    `min(120, 18 + ceil(chars * 0.085))`."""
    n = max(0, int(chars or 0))
    added = int(math.ceil(round(n * SESSION_CALL_PER_CHAR_GPU_S, 9)))
    return float(min(SESSION_CALL_MAX_GPU_S, SESSION_CALL_BASE_GPU_S + added))


def session_declared_s(chars: int) -> float:
    """Return the Space's un-multiplied declared GPU duration for `/generate_session`:
    `min(120, max(30, 18 + int(chars * 0.085)))`."""
    n = max(0, int(chars or 0))
    return float(
        min(
            SESSION_CALL_MAX_GPU_S,
            max(int(SESSION_DECLARED_MIN_GPU_S), SESSION_CALL_BASE_GPU_S + int(n * SESSION_CALL_PER_CHAR_GPU_S)),
        )
    )


def legacy_declared_s(chars: int) -> float:
    """Return the Space's un-multiplied declared GPU duration for `/generate`:
    `min(55, max(25, 18 + int(chars * 0.15)))`."""
    n = max(0, int(chars or 0))
    return float(
        min(
            int(LEGACY_DECLARED_MAX_GPU_S),
            max(int(LEGACY_DECLARED_MIN_GPU_S), LEGACY_CALL_BASE_GPU_S + int(n * LEGACY_CALL_PER_CHAR_GPU_S)),
        )
    )


def legacy_declared_door_s(chars: int) -> float:
    """Return the x1.5 Half-Blackwell door duration for `/generate`:
    `round(min(55, max(25, 18 + int(chars * 0.15))) * 1.5, 2)`."""
    return round(legacy_declared_s(chars) * SESSION_ADMISSION_GATE_MULTIPLIER, 2)


def compute_method1_debit_s(gpu_s: float, *, session_mode: bool, chars: int) -> float:
    """Method 1 debit rule: `clamp(gpu_s + 1.0, 1.0, declared_door_s)`."""
    door_s = session_admission_gate_s(chars) if session_mode else legacy_declared_door_s(chars)
    return round(min(float(door_s), max(MIN_REPORTED_GPU_DEBIT_S, float(gpu_s) + GPU_REPORTED_OVERHEAD_PAD_S)), 2)


def _is_cpu_demo_space_name(space_name: str | None) -> bool:
    norm = str(space_name or "").strip().lower().rstrip("/")
    if not norm:
        return False
    if norm == "openbmb/voxcpm-demo":
        return True
    return bool(re.search(r"(?:^|/|-)openbmb[-/]voxcpm-demo$", norm))


_RETRY_HMS_RE = re.compile(
    r"try again in\s+(?:(\d+)\s+days?,\s*)?(\d+):(\d{2}):(\d{2}(?:\.\d+)?)",
    re.IGNORECASE,
)
_RETRY_MS_RE = re.compile(
    r"try again in\s+(\d+):(\d{2}(?:\.\d+)?)(?!\s*:)",
    re.IGNORECASE,
)
_RETRY_UNITS_BLOCK_RE = re.compile(
    r"try again in\s+((?:\d+(?:\.\d+)?\s*(?:days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m|seconds?|secs?|sec|s)\b[\s,]*(?:and\s+)?)+)",
    re.IGNORECASE,
)
_RETRY_UNIT_ITEM_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|hr|h|minutes?|mins?|min|m|seconds?|secs?|sec|s)\b",
    re.IGNORECASE,
)


def parse_refusal_retry_wait_s(error_text: str | None) -> float | None:
    """Parse `Try again in {wait}` from a ZeroGPU quota refusal into positive seconds, or None."""
    text = str(error_text or "")
    if not text or "try again in" not in text.lower():
        return None
    m_hms = _RETRY_HMS_RE.search(text)
    if m_hms is not None:
        days = int(m_hms.group(1) or 0)
        hours = int(m_hms.group(2) or 0)
        mins = int(m_hms.group(3) or 0)
        secs = float(m_hms.group(4) or 0.0)
        tot = days * 86400.0 + hours * 3600.0 + mins * 60.0 + secs
        return round(tot, 2) if tot > 0 else None
    m_ms = _RETRY_MS_RE.search(text)
    if m_ms is not None:
        mins = int(m_ms.group(1) or 0)
        secs = float(m_ms.group(2) or 0.0)
        tot = mins * 60.0 + secs
        return round(tot, 2) if tot > 0 else None
    m_blk = _RETRY_UNITS_BLOCK_RE.search(text)
    if m_blk is not None:
        tot = 0.0
        for val_s, unit_s in _RETRY_UNIT_ITEM_RE.findall(m_blk.group(1)):
            val = float(val_s)
            u = unit_s.lower()
            if u.startswith("d"):
                tot += val * 86400.0
            elif u.startswith("h"):
                tot += val * 3600.0
            elif u.startswith("m"):
                tot += val * 60.0
            else:
                tot += val
        return round(tot, 2) if tot > 0 else None
    return None


def session_admission_gate_s(chars: int) -> float:
    """Part P2 + 2026-11-27 floor fix: return the x1.5 HF ZeroGPU door admission gate
    (reconciling the Space's 30.0 s declared floor, capped at 180.0 s) used strictly
    for `/generate_session` token solvency selection."""
    declared_s = max(SESSION_DECLARED_MIN_GPU_S, session_call_gate_s(chars))
    door_s = int(math.ceil(round(declared_s * SESSION_ADMISSION_GATE_MULTIPLIER, 9)))
    return float(min(SESSION_ADMISSION_MAX_GPU_S, door_s))

WATERMARK_NORMAL_MIN = 0.40             # > 40%
WATERMARK_CONSERVATION_MIN = 0.20       # 40% -> 20%
WATERMARK_SHORT_JOB_MIN = 0.08          # 20% -> 8%; < 8% is Dry gate

BAND_NORMAL = "normal"
BAND_CONSERVATION = "conservation"
BAND_SHORT_JOB = "short_job_priority"
BAND_DRY = "dry"

_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_lock = threading.RLock()
_mem_state: dict[str, Any] | None = None
_mem_loaded_mono: float = 0.0
_mem_in_flight_jobs: dict[str, dict[str, float]] = {}
_last_evicted_stale_gpu_s: float = 0.0
_refusal_rem_baseline: dict[str, float] = {}
_mem_truth_ts: dict[str, float] = {}
_mem_token_meta: dict[str, dict[str, Any]] = {}
_mem_token_meta_loaded: bool = False
_mem_drift_window: dict[str, dict[str, float]] = {}
_last_seen_env_hash: str | None = None
_known_token_digests: set[str] = set()


def token_digest(token: str) -> str:
    """Full SHA-256 hex digest of a raw HF token (never reversible)."""
    return hashlib.sha256(str(token or "").strip().encode("utf-8")).hexdigest()


def compute_env_tokens_hash(tokens: Sequence[str]) -> str:
    """Order-independent digest of the configured HF token set."""
    digests = sorted({token_digest(t) for t in tokens if t and str(t).strip()})
    joined = "|".join(digests)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _iso_utc(ts: float | None = None) -> str:
    val = time.time() if ts is None else float(ts)
    return datetime.fromtimestamp(val, tz=timezone.utc).isoformat()


def _parse_ts(val: Any) -> float | None:
    """Parse an ISO-8601 timestamp or numeric epoch into UTC epoch seconds."""
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        f = float(val)
        return f if f > 0 else None
    s = str(val).strip()
    if not s:
        return None
    try:
        f = float(s)
        return f if f > 0 else None
    except ValueError:
        pass
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _empty_state() -> dict[str, Any]:
    return {
        "tokens": {},
        "space_gates": {},
        "in_flight_gpu_s": 0,
    }


def _normalize_token_row(raw: Any, now_ts: float | None = None) -> dict[str, Any]:
    now = time.time() if now_ts is None else float(now_ts)
    if not isinstance(raw, Mapping):
        raw = {}
    status = str(raw.get("status") or "ok").strip().lower()
    if status not in ("ok", "auth_dead"):
        status = "ok"
    try:
        rem = float(raw.get("remaining_s") if raw.get("remaining_s") is not None else 0.0)
    except (TypeError, ValueError):
        rem = 0.0
    try:
        base = float(raw.get("base_s") if raw.get("base_s") is not None else 0.0)
    except (TypeError, ValueError):
        base = 0.0
    resets_at = raw.get("resets_at")
    resets_str = str(resets_at) if resets_at not in (None, "") else ""
    cooldown_raw = raw.get("cooldown_until")
    if isinstance(cooldown_raw, (int, float)) and float(cooldown_raw) > 0:
        cooldown_str = _iso_utc(float(cooldown_raw))
    elif cooldown_raw not in (None, ""):
        cooldown_str = str(cooldown_raw)
    else:
        cooldown_str = ""

    # Auto-expire 24h auth_dead quarantine once cooldown_until passes.
    cd_ts = _parse_ts(cooldown_str)
    if status == "auth_dead" and cd_ts is not None and cd_ts <= now:
        status = "ok"
        cooldown_str = ""
    elif status == "ok" and cd_ts is not None and cd_ts <= now:
        cooldown_str = ""

    # Part P1.5c: ZeroGPU uses a rolling 24 h window where `resets_at` marks only when
    # the oldest usage ages out — NEVER snap `remaining_s` back to full `base_s` here;
    # leave the measured value intact and let the 15-min probe reconcile true recovery.

    # Part P1.1: optional scoped refusal ceiling (`refused_gate_s`, `refused_until`).
    # Omitted whenever unset, expired, or `auth_dead` so untouched rows keep exact shape.
    rg_val: float | None = None
    ru_str: str = ""
    if status == "ok":
        raw_rg = raw.get("refused_gate_s")
        raw_ru = raw.get("refused_until")
        if raw_rg is not None and raw_ru not in (None, ""):
            try:
                cand_rg = float(raw_rg)
            except (TypeError, ValueError):
                cand_rg = 0.0
            cand_ru = (
                _iso_utc(float(raw_ru))
                if isinstance(raw_ru, (int, float)) and float(raw_ru) > 0
                else str(raw_ru).strip()
            )
            ru_ts = _parse_ts(cand_ru)
            if cand_rg > 0 and ru_ts is not None and ru_ts > now:
                rg_val = round(cand_rg, 2)
                ru_str = cand_ru

    updated_raw = raw.get("updated_at")
    updated_str = str(updated_raw) if updated_raw not in (None, "") else _iso_utc(now)
    row_out: dict[str, Any] = {
        "status": status,
        "remaining_s": round(max(0.0, rem), 2),
        "base_s": round(max(0.0, base), 2),
        "resets_at": resets_str,
        "cooldown_until": cooldown_str,
        "updated_at": updated_str,
    }
    if rg_val is not None and ru_str:
        row_out["refused_gate_s"] = rg_val
        row_out["refused_until"] = ru_str
    return row_out


def normalize_pool_state(raw: Any, *, now_ts: float | None = None) -> dict[str, Any]:
    """Return a clean ledger dict strictly matching the shipped JSON schema."""
    now = time.time() if now_ts is None else float(now_ts)
    if not isinstance(raw, Mapping):
        return _empty_state()
    raw_tokens = raw.get("tokens")
    tokens_out: dict[str, dict[str, Any]] = {}
    if isinstance(raw_tokens, Mapping):
        for digest, entry in raw_tokens.items():
            d_key = str(digest or "").strip()
            if not d_key:
                continue
            tokens_out[d_key] = _normalize_token_row(entry, now_ts=now)

    raw_gates = raw.get("space_gates")
    gates_out: dict[str, int | float] = {}
    if isinstance(raw_gates, Mapping):
        for sp, gate_val in raw_gates.items():
            sp_key = str(sp or "").strip()
            if not sp_key:
                continue
            try:
                g = float(gate_val)
            except (TypeError, ValueError):
                continue
            if g > 0:
                gates_out[sp_key] = int(g) if g.is_integer() else round(g, 2)

    try:
        in_flight = max(0.0, float(raw.get("in_flight_gpu_s") or 0.0))
    except (TypeError, ValueError):
        in_flight = 0.0
    in_flight_val: int | float = int(in_flight) if in_flight.is_integer() else round(in_flight, 2)

    # Part C1 & C4 (additive only): per-Space 10m timeout cooldowns and running timeout counters.
    # Omitted when empty so untouched states keep the exact 3-key base schema with zero migration.
    raw_space_cds = raw.get("space_cooldowns")
    space_cds_out: dict[str, str] = {}
    if isinstance(raw_space_cds, Mapping):
        for sp, cd_val in raw_space_cds.items():
            sp_key = str(sp or "").strip()
            if not sp_key or cd_val in (None, ""):
                continue
            cd_str = _iso_utc(float(cd_val)) if isinstance(cd_val, (int, float)) and float(cd_val) > 0 else str(cd_val).strip()
            cd_ts = _parse_ts(cd_str)
            if cd_ts is not None and cd_ts > now:
                space_cds_out[sp_key] = cd_str

    raw_space_tos = raw.get("space_timeouts")
    space_tos_out: dict[str, int] = {}
    if isinstance(raw_space_tos, Mapping):
        for sp, cnt_val in raw_space_tos.items():
            sp_key = str(sp or "").strip()
            if not sp_key:
                continue
            try:
                cnt = int(cnt_val)
            except (TypeError, ValueError):
                continue
            if cnt > 0:
                space_tos_out[sp_key] = cnt

    raw_sess_floors = raw.get("session_space_floors")
    sess_floors_out: dict[str, int | float] = {}
    if isinstance(raw_sess_floors, Mapping):
        for sp, fl_val in raw_sess_floors.items():
            sp_key = str(sp or "").strip()
            if not sp_key:
                continue
            try:
                fl = float(fl_val)
            except (TypeError, ValueError):
                continue
            if fl > 0:
                sess_floors_out[sp_key] = int(fl) if fl.is_integer() else round(fl, 2)

    raw_drift = raw.get("drift_stats")
    drift_out: dict[str, int | float] | None = None
    if isinstance(raw_drift, Mapping) and raw_drift:
        try:
            pos_d = round(max(0.0, float(raw_drift.get("positive_drift_s") or 0.0)), 2)
            over_cnt = max(0, int(raw_drift.get("overestimated_tokens") or 0))
            max_d = round(max(0.0, float(raw_drift.get("max_token_drift_s") or 0.0)), 2)
            err_cnt = max(0, int(raw_drift.get("quota_probe_errors") or 0))
            drift_out = {
                "positive_drift_s": pos_d,
                "overestimated_tokens": over_cnt,
                "max_token_drift_s": max_d,
                "quota_probe_errors": err_cnt,
            }
        except (TypeError, ValueError):
            drift_out = None

    out: dict[str, Any] = {
        "tokens": tokens_out,
        "space_gates": gates_out,
        "in_flight_gpu_s": in_flight_val,
    }
    if sess_floors_out:
        out["session_space_floors"] = sess_floors_out
    if space_cds_out:
        out["space_cooldowns"] = space_cds_out
    if space_tos_out:
        out["space_timeouts"] = space_tos_out
    if drift_out is not None:
        out["drift_stats"] = drift_out
    return out


def _read_store_state(now_ts: float | None = None) -> dict[str, Any] | None:
    raw = botsetting_sync_read(POOL_STATE_STORE_KEY)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return normalize_pool_state(parsed, now_ts=now_ts)


def _write_store_state(state: dict[str, Any], *, now_ts: float | None = None) -> bool:
    clean = normalize_pool_state(state, now_ts=now_ts)
    try:
        payload = json.dumps(clean, separators=(",", ":"))
    except Exception:
        return False
    return botsetting_sync_write(POOL_STATE_STORE_KEY, payload)


def _load_in_flight_map(now_ts: float) -> dict[str, dict[str, float]]:
    global _mem_in_flight_jobs, _last_evicted_stale_gpu_s
    raw = botsetting_sync_read(IN_FLIGHT_STORE_KEY)
    loaded: dict[str, dict[str, float]] = {}
    evicted_stale_gpu_s = 0.0
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, Mapping):
                for jid, meta in data.items():
                    if not isinstance(meta, Mapping):
                        continue
                    gpu_s = max(0.0, float(meta.get("gpu_s") or 0.0))
                    ts = float(meta.get("ts") or now_ts)
                    if gpu_s > 0:
                        if (now_ts - ts) < IN_FLIGHT_STALE_SECONDS:
                            loaded[str(jid)] = {"gpu_s": round(gpu_s, 2), "ts": ts}
                        else:
                            evicted_stale_gpu_s += round(gpu_s, 2)
        except Exception:
            loaded = {}
            evicted_stale_gpu_s = 0.0
    else:
        for jid, meta in _mem_in_flight_jobs.items():
            gpu_s = max(0.0, float(meta.get("gpu_s") or 0.0))
            ts = float(meta.get("ts") or now_ts)
            if (now_ts - ts) < IN_FLIGHT_STALE_SECONDS:
                loaded[jid] = dict(meta)
            elif gpu_s > 0:
                evicted_stale_gpu_s += round(gpu_s, 2)
    _mem_in_flight_jobs = loaded
    _last_evicted_stale_gpu_s = round(evicted_stale_gpu_s, 2)
    return loaded


def _save_in_flight_map(jobs_map: dict[str, dict[str, float]]) -> None:
    global _mem_in_flight_jobs
    _mem_in_flight_jobs = dict(jobs_map)
    try:
        botsetting_sync_write(IN_FLIGHT_STORE_KEY, json.dumps(jobs_map, separators=(",", ":")))
    except Exception:
        pass


def load_pool_state(*, force: bool = False, now_ts: float | None = None) -> dict[str, Any]:
    """Return the current pool state, reloading from `bot_settings` on 30 s TTL or `force`.

    Non-forced reads within TTL serve the immutable in-memory snapshot via an atomic
    reference read without blocking behind `_lock` or `_SYNC_STORE_LOCK`.
    """
    global _mem_state, _mem_loaded_mono
    now = time.time() if now_ts is None else float(now_ts)
    mono = time.monotonic()
    if not force:
        snap = _mem_state
        loaded_mono = _mem_loaded_mono
        if snap is not None and (mono - loaded_mono) < CACHE_TTL_SECONDS:
            return normalize_pool_state(snap, now_ts=now)
    with _lock:
        expired = (mono - _mem_loaded_mono) >= CACHE_TTL_SECONDS
        if force or _mem_state is None or expired:
            persisted = _read_store_state(now_ts=now)
            if persisted is not None:
                _mem_state = persisted
            elif _mem_state is None:
                _mem_state = _empty_state()
            else:
                _mem_state = normalize_pool_state(_mem_state, now_ts=now)
            _mem_loaded_mono = mono
            return json.loads(json.dumps(_mem_state))
        return normalize_pool_state(_mem_state, now_ts=now)


def save_pool_state(state: Mapping[str, Any], *, now_ts: float | None = None) -> dict[str, Any]:
    """Normalize, cache in memory, and write through to `aj_hf:pool_state`."""
    global _mem_state, _mem_loaded_mono
    now = time.time() if now_ts is None else float(now_ts)
    with _lock:
        clean = normalize_pool_state(state, now_ts=now)
        _mem_state = clean
        _mem_loaded_mono = time.monotonic()
        _write_store_state(clean, now_ts=now)
        return json.loads(json.dumps(clean))


def _mutate_pool_state(
    mutator_fn: Callable[..., tuple[dict[str, Any], bool]],
    *,
    now_ts: float | None = None,
    aux_keys: Sequence[str] = (),
) -> dict[str, Any]:
    """Atomically mutate `aj_hf:pool_state` via `botsetting_sync_mutate` (`SELECT ... FOR UPDATE`)."""
    global _mem_state, _mem_loaded_mono
    now = time.time() if now_ts is None else float(now_ts)
    clean_aux = tuple(str(k) for k in (aux_keys or ()) if str(k))
    has_aux = bool(clean_aux)
    result_box: dict[str, Any] = {}
    with _lock:
        def _resolve_base(raw_str: str | None) -> dict[str, Any]:
            base_state: dict[str, Any] | None = None
            if raw_str:
                try:
                    base_state = normalize_pool_state(json.loads(raw_str), now_ts=now)
                except Exception:
                    base_state = None
            if base_state is None:
                if _mem_state is not None:
                    base_state = normalize_pool_state(_mem_state, now_ts=now)
                else:
                    base_state = _empty_state()
            return base_state

        if has_aux:
            def _apply(raw_str: str | None, aux_map: Mapping[str, str | None]) -> str | None:
                base_state = _resolve_base(raw_str)
                updated_state, should_write = mutator_fn(base_state, aux_map)
                clean = normalize_pool_state(updated_state, now_ts=now)
                result_box["clean"] = clean
                if not should_write:
                    return None
                return json.dumps(clean, separators=(",", ":"))
        else:
            def _apply(raw_str: str | None) -> str | None:
                base_state = _resolve_base(raw_str)
                updated_state, should_write = mutator_fn(base_state)
                clean = normalize_pool_state(updated_state, now_ts=now)
                result_box["clean"] = clean
                if not should_write:
                    return None
                return json.dumps(clean, separators=(",", ":"))

        botsetting_sync_mutate(
            POOL_STATE_STORE_KEY,
            _apply,
            read_fn=botsetting_sync_read,
            write_fn=botsetting_sync_write,
            aux_keys=clean_aux,
        )
        clean_out = result_box.get("clean")
        if clean_out is None:
            clean_out = (
                normalize_pool_state(_mem_state, now_ts=now)
                if _mem_state is not None
                else _empty_state()
            )
        _mem_state = clean_out
        _mem_loaded_mono = time.monotonic()
        return json.loads(json.dumps(clean_out))


def _canonical_digest(token_or_digest: str) -> str:
    raw_tok = str(token_or_digest).strip()
    if len(raw_tok) == 64 and all(c in "0123456789abcdef" for c in raw_tok):
        return raw_tok
    return token_digest(raw_tok)


def _merge_token_meta_entry(existing: Mapping[str, Any] | None, incoming: Mapping[str, Any]) -> dict[str, Any]:
    base = dict(existing or {})
    out: dict[str, Any] = {}
    taint_a = float(base.get("taint_ts") or 0.0)
    taint_b = float(incoming.get("taint_ts") or 0.0)
    if max(taint_a, taint_b) > 0.0:
        if taint_b >= taint_a:
            out["taint_ts"] = round(taint_b, 6)
            if incoming.get("taint_kind"):
                out["taint_kind"] = str(incoming["taint_kind"])
            elif base.get("taint_kind"):
                out["taint_kind"] = str(base["taint_kind"])
        else:
            out["taint_ts"] = round(taint_a, 6)
            if base.get("taint_kind"):
                out["taint_kind"] = str(base["taint_kind"])
    att_a = float(base.get("last_attempt_ts") or 0.0)
    att_b = float(incoming.get("last_attempt_ts") or 0.0)
    if max(att_a, att_b) > 0.0:
        out["last_attempt_ts"] = round(max(att_a, att_b), 6)
    ver_ts_a = float(base.get("verified_ts") or 0.0)
    ver_ts_b = float(incoming.get("verified_ts") or 0.0)
    if max(ver_ts_a, ver_ts_b) > 0.0:
        if ver_ts_b >= ver_ts_a and incoming.get("verified_rem_s") is not None:
            out["verified_ts"] = round(ver_ts_b, 6)
            out["verified_rem_s"] = round(float(incoming["verified_rem_s"]), 2)
        elif base.get("verified_rem_s") is not None:
            out["verified_ts"] = round(ver_ts_a, 6)
            out["verified_rem_s"] = round(float(base["verified_rem_s"]), 2)
    upd_a = float(base.get("updated_ts") or 0.0)
    upd_b = float(incoming.get("updated_ts") or 0.0)
    out["updated_ts"] = round(max(upd_a, upd_b, taint_b, att_b, ver_ts_b), 6)
    return out


def _load_token_meta_map(*, reload_store: bool = False, now_ts: float | None = None) -> dict[str, dict[str, Any]]:
    global _mem_token_meta_loaded
    now = time.time() if now_ts is None else float(now_ts)
    cutoff = now - TOKEN_META_RETENTION_SECONDS
    with _lock:
        if reload_store or not _mem_token_meta_loaded:
            raw = botsetting_sync_read(TOKEN_META_STORE_KEY)
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, Mapping):
                        for k, v in parsed.items():
                            if not isinstance(v, Mapping):
                                continue
                            merged = _merge_token_meta_entry(_mem_token_meta.get(str(k)), v)
                            if float(merged.get("updated_ts") or 0.0) >= cutoff:
                                _mem_token_meta[str(k)] = merged
                except Exception:
                    pass
            _mem_token_meta_loaded = True
        return {k: dict(v) for k, v in _mem_token_meta.items() if float(v.get("updated_ts") or 0.0) >= cutoff}


def _persist_token_meta_update(d_key: str, patch: Mapping[str, Any], *, clear_taint_up_to: float | None = None, now_ts: float | None = None) -> None:
    global _mem_token_meta_loaded
    now = time.time() if now_ts is None else float(now_ts)
    cutoff = now - TOKEN_META_RETENTION_SECONDS
    with _lock:
        cur_entry = _merge_token_meta_entry(_mem_token_meta.get(d_key), {**dict(patch), "updated_ts": now})
        if clear_taint_up_to is not None and float(cur_entry.get("taint_ts") or 0.0) <= float(clear_taint_up_to):
            cur_entry.pop("taint_ts", None)
            cur_entry.pop("taint_kind", None)
        _mem_token_meta[d_key] = cur_entry
        _mem_token_meta_loaded = True

        def _mutate_meta(raw_str: str | None) -> str | None:
            store_map: dict[str, dict[str, Any]] = {}
            if raw_str:
                try:
                    parsed = json.loads(raw_str)
                    if isinstance(parsed, Mapping):
                        for k, v in parsed.items():
                            if isinstance(v, Mapping):
                                m = _merge_token_meta_entry(None, v)
                                if float(m.get("updated_ts") or 0.0) >= cutoff:
                                    store_map[str(k)] = m
                except Exception:
                    store_map = {}
            for k, v in list(_mem_token_meta.items()):
                if float(v.get("updated_ts") or 0.0) < cutoff:
                    _mem_token_meta.pop(k, None)
                else:
                    store_map[k] = _merge_token_meta_entry(store_map.get(k), v)
            merged_target = _merge_token_meta_entry(store_map.get(d_key), cur_entry)
            if clear_taint_up_to is not None and float(merged_target.get("taint_ts") or 0.0) <= float(clear_taint_up_to):
                merged_target.pop("taint_ts", None)
                merged_target.pop("taint_kind", None)
            store_map[d_key] = merged_target
            _mem_token_meta.update(store_map)
            return json.dumps(store_map, separators=(",", ":"))

        botsetting_sync_mutate(
            TOKEN_META_STORE_KEY,
            _mutate_meta,
            read_fn=botsetting_sync_read,
            write_fn=botsetting_sync_write,
        )


def record_token_truth_timestamp(
    token: str | None,
    truth_ts: float | None = None,
    *,
    verified_rem_s: float | None = None,
) -> float | None:
    """Record authoritative HF balance observation timestamp in memory and `aj_hf:truth_ts`."""
    if not token or not str(token).strip():
        return None
    ts = time.time() if truth_ts is None else float(truth_ts)
    d_key = _canonical_digest(str(token).strip())
    with _lock:
        prev = _mem_truth_ts.get(d_key)
        best = round(max(prev, ts), 6) if prev is not None else round(ts, 6)
        _mem_truth_ts[d_key] = best
        cutoff = ts - TRUTH_TS_RETENTION_SECONDS

        def _mutate_truth(raw_str: str | None) -> str | None:
            cur_map: dict[str, float] = {}
            if raw_str:
                try:
                    parsed = json.loads(raw_str)
                    if isinstance(parsed, Mapping):
                        for k, v in parsed.items():
                            fv = float(v)
                            if fv >= cutoff:
                                cur_map[str(k)] = round(fv, 6)
                except Exception:
                    cur_map = {}
            for k, v in list(_mem_truth_ts.items()):
                if v < cutoff:
                    _mem_truth_ts.pop(k, None)
                else:
                    cur_map[k] = round(max(cur_map.get(k, 0.0), v), 6)
            cur_map[d_key] = round(max(cur_map.get(d_key, 0.0), best), 6)
            _mem_truth_ts.update(cur_map)
            return json.dumps(cur_map, separators=(",", ":"))

        botsetting_sync_mutate(
            TRUTH_TS_STORE_KEY,
            _mutate_truth,
            read_fn=botsetting_sync_read,
            write_fn=botsetting_sync_write,
        )
        meta_patch: dict[str, Any] = {}
        if verified_rem_s is not None:
            meta_patch["verified_ts"] = best
            meta_patch["verified_rem_s"] = round(max(0.0, float(verified_rem_s)), 2)
        existing_meta = _mem_token_meta.get(d_key) or {}
        if meta_patch or float(existing_meta.get("taint_ts") or 0.0) > 0.0:
            _persist_token_meta_update(d_key, meta_patch, clear_taint_up_to=best, now_ts=best)
        return _mem_truth_ts.get(d_key, best)


def record_token_taint(
    token: str | None,
    kind: str = "unverified_taint",
    *,
    now_ts: float | None = None,
) -> None:
    """Record a Method 2 failure debit or numberless generic refusal taint since last truth stamp."""
    if not token or not str(token).strip():
        return
    now = time.time() if now_ts is None else float(now_ts)
    d_key = _canonical_digest(str(token).strip())
    with _lock:
        truth_ts = get_token_last_truth_ts(d_key, reload_store=False) or 0.0
        cur = _mem_token_meta.get(d_key) or {}
        already_tainted = float(cur.get("taint_ts") or 0.0) > truth_ts
        patch = {
            "taint_ts": round(now, 6),
            "taint_kind": str(kind or "unverified_taint"),
            "last_attempt_ts": round(max(now, float(cur.get("last_attempt_ts") or 0.0)), 6),
        }
        if already_tainted:
            _mem_token_meta[d_key] = _merge_token_meta_entry(cur, {**patch, "updated_ts": now})
        else:
            _persist_token_meta_update(d_key, patch, now_ts=now)


def record_token_synthesis_attempt(
    token: str | None,
    *,
    now_ts: float | None = None,
) -> None:
    """Mark that `token` participated in a synthesis attempt at `now_ts` (invalidating untouched-certain skip)."""
    if not token or not str(token).strip():
        return
    now = time.time() if now_ts is None else float(now_ts)
    d_key = _canonical_digest(str(token).strip())
    with _lock:
        truth_ts = get_token_last_truth_ts(d_key, reload_store=False) or 0.0
        cur = _mem_token_meta.get(d_key) or {}
        prev_att = float(cur.get("last_attempt_ts") or 0.0)
        patch = {"last_attempt_ts": round(max(now, prev_att), 6)}
        # Persist to DB only on the first attempt after a truth stamp to keep hot synthesis paths zero-SQL.
        if truth_ts > 0.0 and prev_att <= truth_ts:
            _persist_token_meta_update(d_key, patch, now_ts=now)
        else:
            _mem_token_meta[d_key] = _merge_token_meta_entry(cur, {**patch, "updated_ts": now})


def get_token_meta(
    token: str | None,
    *,
    reload_store: bool = False,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Return auxiliary calibration metadata for `token` (or `{}` if none)."""
    if not token or not str(token).strip():
        return {}
    d_key = _canonical_digest(str(token).strip())
    meta_map = _load_token_meta_map(reload_store=reload_store, now_ts=now_ts)
    return dict(meta_map.get(d_key) or {})


def _record_runtime_drift_observation(d_key: str, positive_delta_s: float, now_ts: float) -> None:
    """Record positive pre-overwrite balance drift observed by numeric refusals / calibration sweeps."""
    if positive_delta_s <= 0.0:
        return
    cutoff = now_ts - DRIFT_WINDOW_RETENTION_SECONDS
    with _lock:
        prev = _mem_drift_window.get(d_key)
        prev_d = float((prev or {}).get("delta_s") or 0.0) if (prev and float(prev.get("ts") or 0.0) >= cutoff) else 0.0
        _mem_drift_window[d_key] = {
            "delta_s": round(max(prev_d, float(positive_delta_s)), 2),
            "ts": round(float(now_ts), 3),
        }

        def _mutate_dw(raw_str: str | None) -> str | None:
            cur_dw: dict[str, dict[str, float]] = {}
            if raw_str:
                try:
                    parsed = json.loads(raw_str)
                    if isinstance(parsed, Mapping):
                        for k, v in parsed.items():
                            if isinstance(v, Mapping) and float(v.get("ts") or 0.0) >= cutoff:
                                cur_dw[str(k)] = {
                                    "delta_s": round(max(0.0, float(v.get("delta_s") or 0.0)), 2),
                                    "ts": round(float(v.get("ts") or 0.0), 3),
                                }
                except Exception:
                    cur_dw = {}
            for k, v in list(_mem_drift_window.items()):
                if float(v.get("ts") or 0.0) < cutoff:
                    _mem_drift_window.pop(k, None)
                else:
                    existing_d = float((cur_dw.get(k) or {}).get("delta_s") or 0.0)
                    cur_dw[k] = {
                        "delta_s": round(max(existing_d, float(v.get("delta_s") or 0.0)), 2),
                        "ts": round(max(float((cur_dw.get(k) or {}).get("ts") or 0.0), float(v.get("ts") or 0.0)), 3),
                    }
            _mem_drift_window.update(cur_dw)
            return json.dumps(cur_dw, separators=(",", ":"))

        botsetting_sync_mutate(
            DRIFT_WINDOW_STORE_KEY,
            _mutate_dw,
            read_fn=botsetting_sync_read,
            write_fn=botsetting_sync_write,
        )


def _summarize_runtime_drift_window(now_ts: float) -> tuple[float, int, float]:
    cutoff = now_ts - DRIFT_WINDOW_RETENTION_SECONDS
    with _lock:
        raw = botsetting_sync_read(DRIFT_WINDOW_STORE_KEY)
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, Mapping):
                    for k, v in parsed.items():
                        if isinstance(v, Mapping) and float(v.get("ts") or 0.0) >= cutoff:
                            prev_d = float((_mem_drift_window.get(str(k)) or {}).get("delta_s") or 0.0)
                            _mem_drift_window[str(k)] = {
                                "delta_s": round(max(prev_d, float(v.get("delta_s") or 0.0)), 2),
                                "ts": round(float(v.get("ts") or 0.0), 3),
                            }
            except Exception:
                pass
        pos_sum = 0.0
        over_cnt = 0
        max_d = 0.0
        for k, v in list(_mem_drift_window.items()):
            if float(v.get("ts") or 0.0) < cutoff:
                _mem_drift_window.pop(k, None)
                continue
            d = float(v.get("delta_s") or 0.0)
            if d > 0.0:
                pos_sum += d
                if d > max_d:
                    max_d = d
                if d > 25.0:
                    over_cnt += 1
        return round(pos_sum, 2), int(over_cnt), round(max_d, 2)


def _resolve_truth_ts_from_aux(d_key: str, raw_truth_json: str | None) -> float | None:
    """Merge an auxiliary-read `aj_hf:truth_ts` JSON string into `_mem_truth_ts` in pure CPU."""
    if raw_truth_json:
        try:
            parsed = json.loads(raw_truth_json)
            if isinstance(parsed, Mapping):
                for k, v in parsed.items():
                    fv = float(v)
                    prev = _mem_truth_ts.get(str(k))
                    _mem_truth_ts[str(k)] = max(prev, fv) if prev is not None else fv
        except Exception:
            pass
    return _mem_truth_ts.get(d_key)


def get_token_last_truth_ts(
    token: str | None,
    *,
    reload_store: bool = False,
) -> float | None:
    """Return last authoritative truth epoch timestamp for `token` (or digest), else None."""
    if not token or not str(token).strip():
        return None
    d_key = _canonical_digest(str(token).strip())
    with _lock:
        if reload_store:
            raw = botsetting_sync_read(TRUTH_TS_STORE_KEY)
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, Mapping):
                        for k, v in parsed.items():
                            fv = float(v)
                            prev = _mem_truth_ts.get(str(k))
                            _mem_truth_ts[str(k)] = max(prev, fv) if prev is not None else fv
                except Exception:
                    pass
        return _mem_truth_ts.get(d_key)


def reset_ledger_for_tests(*, clear_store: bool = False) -> None:
    """Reset in-memory cache (and optionally persisted KV keys) between unit tests."""
    global _mem_state, _mem_loaded_mono, _mem_in_flight_jobs, _refusal_rem_baseline, _mem_truth_ts, _mem_token_meta, _mem_token_meta_loaded, _mem_drift_window, _last_seen_env_hash, _known_token_digests
    with _lock:
        _mem_state = None
        _mem_loaded_mono = 0.0
        _mem_in_flight_jobs = {}
        _refusal_rem_baseline = {}
        _mem_truth_ts = {}
        _mem_token_meta = {}
        _mem_token_meta_loaded = False
        _mem_drift_window = {}
        _last_seen_env_hash = None
        _known_token_digests = set()
        if clear_store:
            botsetting_sync_delete(POOL_STATE_STORE_KEY)
            botsetting_sync_delete(IN_FLIGHT_STORE_KEY)
            botsetting_sync_delete(ENV_HASH_STORE_KEY)
            botsetting_sync_delete(TRUTH_TS_STORE_KEY)
            botsetting_sync_delete(TOKEN_META_STORE_KEY)
            botsetting_sync_delete(DRIFT_WINDOW_STORE_KEY)


def lookup_token_entry(
    state: Mapping[str, Any], token: str
) -> tuple[str, dict[str, Any] | None]:
    """Locate a token's entry by full SHA-256 digest (or legacy 12-char digest)."""
    tok = str(token or "").strip()
    if not tok:
        return "", None
    tokens_map = state.get("tokens") if isinstance(state, Mapping) else None
    full_d = token_digest(tok)
    if not isinstance(tokens_map, Mapping):
        return full_d, None
    if full_d in tokens_map:
        return full_d, dict(tokens_map[full_d])
    short_d = _short_key_id(tok)
    if short_d in tokens_map:
        return short_d, dict(tokens_map[short_d])
    return full_d, None


def is_token_auth_dead(token: str, *, state: Mapping[str, Any] | None = None, now_ts: float | None = None) -> bool:
    """True when `token` is currently quarantined as `auth_dead`."""
    if not token:
        return False
    now = time.time() if now_ts is None else float(now_ts)
    st = load_pool_state(now_ts=now) if state is None else normalize_pool_state(state, now_ts=now)
    _d, row = lookup_token_entry(st, token)
    if not row or row.get("status") != "auth_dead":
        return False
    cd_ts = _parse_ts(row.get("cooldown_until"))
    if cd_ts is not None and cd_ts <= now:
        return False
    return True


def is_token_cooling(token: str, *, state: Mapping[str, Any] | None = None, now_ts: float | None = None) -> bool:
    """True when `token` has an active cooldown or `auth_dead` quarantine in the ledger."""
    if not token:
        return False
    now = time.time() if now_ts is None else float(now_ts)
    st = load_pool_state(now_ts=now) if state is None else normalize_pool_state(state, now_ts=now)
    _d, row = lookup_token_entry(st, token)
    if not row:
        return False
    if row.get("status") == "auth_dead":
        return True
    cd_ts = _parse_ts(row.get("cooldown_until"))
    return bool(cd_ts is not None and cd_ts > now)


def get_token_remaining_s(
    token: str, *, state: Mapping[str, Any] | None = None, now_ts: float | None = None
) -> float | None:
    """Known `remaining_s` for `token` from the ledger, or None if unrecorded."""
    if not token:
        return None
    now = time.time() if now_ts is None else float(now_ts)
    st = load_pool_state(now_ts=now) if state is None else normalize_pool_state(state, now_ts=now)
    _d, row = lookup_token_entry(st, token)
    if not row:
        return None
    if row.get("status") == "auth_dead":
        return 0.0
    return float(row.get("remaining_s") or 0.0)


def get_space_gate(
    space_name: str | None = None,
    *,
    state: Mapping[str, Any] | None = None,
    default: float = DEFAULT_SPACE_GATE_S,
    call_type: str = "legacy",
    chars: int = 0,
) -> float:
    """Return the admission gate (seconds) for `space_name`.

    - `call_type="session"`: dynamic `/generate_session` gate `min(120, 18 + ceil(chars * 0.085))`.
    - `call_type="legacy"` (default): looks up `space_gates[space_name]` (falling back to
      the minimum recorded gate across `space_gates` when `space_name` is None, else `default`).
    """
    if str(call_type or "legacy").strip().lower() == "session":
        return session_call_gate_s(chars)
    st = load_pool_state() if state is None else state
    gates = st.get("space_gates") if isinstance(st, Mapping) else None
    if isinstance(gates, Mapping) and gates:
        if space_name:
            if str(space_name) in gates:
                try:
                    val = float(gates[str(space_name)])
                    if val > 0:
                        return val
                except (TypeError, ValueError):
                    pass
            return float(default)
        pos = []
        for v in gates.values():
            try:
                fv = float(v)
                if fv > 0:
                    pos.append(fv)
            except (TypeError, ValueError):
                continue
        if pos:
            return min(pos)
    return float(default)


def _extract_active_ceiling(
    row: Mapping[str, Any] | None, now_ts: float
) -> tuple[float | None, str | None]:
    """Return `(refused_gate_s, refused_until)` if `row` has an active refusal ceiling, else `(None, None)`."""
    if not isinstance(row, Mapping):
        return None, None
    if str(row.get("status") or "ok").strip().lower() == "auth_dead":
        return None, None
    rg_raw = row.get("refused_gate_s")
    ru_raw = row.get("refused_until")
    if rg_raw is None or ru_raw in (None, ""):
        return None, None
    try:
        rg = float(rg_raw)
    except (TypeError, ValueError):
        return None, None
    ru_str = (
        _iso_utc(float(ru_raw))
        if isinstance(ru_raw, (int, float)) and float(ru_raw) > 0
        else str(ru_raw).strip()
    )
    ru_ts = _parse_ts(ru_str)
    if rg > 0 and ru_ts is not None and ru_ts > now_ts:
        return round(rg, 2), ru_str
    return None, None


def _copy_active_ceiling(
    src_row: Mapping[str, Any] | None, dst_row: dict[str, Any], now_ts: float
) -> None:
    rg, ru = _extract_active_ceiling(src_row, now_ts)
    if rg is not None and ru:
        dst_row["refused_gate_s"] = rg
        dst_row["refused_until"] = ru


def get_token_refused_gate_s(
    token_or_row: str | Mapping[str, Any] | None,
    *,
    state: Mapping[str, Any] | None = None,
    now_ts: float | None = None,
) -> float | None:
    """Part P1: return active `refused_gate_s` for `token_or_row` if `refused_until > now`, else None."""
    if not token_or_row:
        return None
    now = time.time() if now_ts is None else float(now_ts)
    if isinstance(token_or_row, Mapping):
        rg, _ru = _extract_active_ceiling(token_or_row, now)
        return rg
    st = load_pool_state(now_ts=now) if state is None else normalize_pool_state(state, now_ts=now)
    _d, row = lookup_token_entry(st, str(token_or_row))
    rg, _ru = _extract_active_ceiling(row, now)
    return rg


def get_token_refused_until(
    token_or_row: str | Mapping[str, Any] | None,
    *,
    state: Mapping[str, Any] | None = None,
    now_ts: float | None = None,
) -> str | None:
    """Part P1: return active ISO `refused_until` for `token_or_row` if `> now`, else None."""
    if not token_or_row:
        return None
    now = time.time() if now_ts is None else float(now_ts)
    if isinstance(token_or_row, Mapping):
        _rg, ru = _extract_active_ceiling(token_or_row, now)
        return ru
    st = load_pool_state(now_ts=now) if state is None else normalize_pool_state(state, now_ts=now)
    _d, row = lookup_token_entry(st, str(token_or_row))
    _rg, ru = _extract_active_ceiling(row, now)
    return ru


def is_token_refusal_capped(
    token: str | None,
    req_gate_s: float,
    *,
    state: Mapping[str, Any] | None = None,
    now_ts: float | None = None,
) -> bool:
    """Part P1.3: True when `token` has an active refusal ceiling and `req_gate_s >= refused_gate_s`."""
    if not token or req_gate_s is None:
        return False
    rg = get_token_refused_gate_s(token, state=state, now_ts=now_ts)
    if rg is None:
        return False
    return float(req_gate_s) >= float(rg)


def get_session_space_floor(
    space_name: str | None,
    *,
    state: Mapping[str, Any] | None = None,
    now_ts: float | None = None,
) -> float:
    """Return learned per-Space session door floor (`session_space_floors[space_name]`) if any, else `0.0`."""
    if not space_name or not str(space_name).strip():
        return 0.0
    now = time.time() if now_ts is None else float(now_ts)
    st = load_pool_state(now_ts=now) if state is None else state
    floors = st.get("session_space_floors") if isinstance(st, Mapping) else None
    if isinstance(floors, Mapping) and str(space_name) in floors:
        try:
            val = float(floors[str(space_name)])
            return val if val > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def effective_session_admission_gate_s(
    chars: int,
    space_name: str | None = None,
    *,
    state: Mapping[str, Any] | None = None,
    now_ts: float | None = None,
) -> float:
    """Return `max(session_admission_gate_s(chars), session_space_floors.get(sp, 0.0))`."""
    base_gate = session_admission_gate_s(chars)
    sp_floor = get_session_space_floor(space_name, state=state, now_ts=now_ts)
    return float(max(base_gate, sp_floor))


def _maybe_apply_session_space_floor(
    st: dict[str, Any],
    space_name: str,
    requested_s: float,
    chars: int,
) -> bool:
    """Wrinkle 5a: learn `session_space_floors[sp]` ONLY when `requested_s` exceeds
    `session_admission_gate_s(chars)` in the <=141-char floor regime OR static-120s regime."""
    sp_key = str(space_name or "").strip()
    if not sp_key or requested_s is None or float(requested_s) <= 0:
        return False
    n_chars = max(0, int(chars or 0))
    req_f = float(requested_s)
    formula_door = session_admission_gate_s(n_chars)
    if req_f <= formula_door:
        return False
    qualifies = (
        n_chars <= SESSION_FLOOR_REGIME_MAX_CHARS
        or (
            n_chars > SESSION_FLOOR_REGIME_MAX_CHARS
            and req_f >= SESSION_STATIC_120_MIN_REQUESTED_S
            and formula_door < SESSION_STATIC_120_MAX_FORMULA_S
        )
    )
    if not qualifies:
        return False
    floors = dict(st.get("session_space_floors") or {})
    floors[sp_key] = int(req_f) if req_f.is_integer() else round(req_f, 2)
    st["session_space_floors"] = floors
    return True


def record_session_space_floor(
    space_name: str,
    requested_s: float,
    chars: int,
    *,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Persist a learned session-only Space door floor (when qualified under Part 5) without touching `space_gates`."""
    now = time.time() if now_ts is None else float(now_ts)

    def _mutate(st: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        changed = _maybe_apply_session_space_floor(st, space_name, requested_s, chars)
        return st, changed

    return _mutate_pool_state(_mutate, now_ts=now)


def record_refusal_ceiling(
    token: str | None,
    gate_s: float,
    *,
    ttl_seconds: float = REFUSAL_CEILING_TTL_SECONDS,
    mark_generic_taint: bool = True,
    now_ts: float | None = None,
) -> dict[str, Any] | None:
    """Part P1.2 + 2026-11-27 2a: record a scoped refusal ceiling (`refused_gate_s = min(existing, gate_s)`),
    extending `refused_until` ONLY when creating or tightening the ceiling and never ratcheting baseline upward."""
    if not token or not str(token).strip() or gate_s is None or float(gate_s) <= 0:
        return None
    now = time.time() if now_ts is None else float(now_ts)
    req_g = round(max(0.01, float(gate_s)), 2)

    def _mutate(st: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        d_key, existing = lookup_token_entry(st, token)
        existing_rem = (existing or {}).get("remaining_s")
        rem_s = float(existing_rem) if existing_rem is not None else DEFAULT_TOKEN_BASE_S
        base_s = float((existing or {}).get("base_s") or max(DEFAULT_TOKEN_BASE_S, rem_s))
        existing_rg, existing_ru = _extract_active_ceiling(existing, now)
        if existing_rg is None or not existing_ru:
            new_rg = req_g
            new_ru = _iso_utc(now + max(0.0, float(ttl_seconds)))
            _refusal_rem_baseline[d_key] = round(max(0.0, rem_s), 2)
        else:
            prev_base = _refusal_rem_baseline.get(d_key)
            if prev_base is None and existing_rem is not None:
                prev_base = float(existing_rem)
            _refusal_rem_baseline[d_key] = (
                round(max(0.0, min(prev_base, rem_s)), 2)
                if prev_base is not None
                else round(max(0.0, rem_s), 2)
            )
            if req_g < existing_rg:
                new_rg = req_g
                new_ru = _iso_utc(now + max(0.0, float(ttl_seconds)))
            else:
                new_rg = existing_rg
                new_ru = existing_ru
        st["tokens"][d_key] = {
            "status": "ok",
            "remaining_s": round(max(0.0, rem_s), 2),
            "base_s": round(max(0.0, base_s), 2),
            "resets_at": (existing or {}).get("resets_at") or _iso_utc(now + 86400.0),
            "cooldown_until": (existing or {}).get("cooldown_until") or "",
            "updated_at": _iso_utc(now),
            "refused_gate_s": new_rg,
            "refused_until": new_ru,
        }
        return st, True

    saved = _mutate_pool_state(_mutate, now_ts=now)
    if mark_generic_taint:
        record_token_taint(token, "generic_refusal", now_ts=now)
    return saved


def get_drift_stats(
    *, state: Mapping[str, Any] | None = None, now_ts: float | None = None
) -> dict[str, int | float] | None:
    """Part Q4.2 + Round 2 Part 1: return 4-key `drift_stats` dict fed by probe reconciliation,
    numeric refusals, and the daily calibration sweep (or None when none recorded)."""
    now = time.time() if now_ts is None else float(now_ts)
    st = load_pool_state(now_ts=now) if state is None else normalize_pool_state(state, now_ts=now)
    ds = st.get("drift_stats")
    rw_pos, rw_over, rw_max = _summarize_runtime_drift_window(now)
    if isinstance(ds, Mapping):
        out = dict(ds)
        if rw_pos > 0.0:
            out["positive_drift_s"] = round(max(float(out.get("positive_drift_s") or 0.0), rw_pos), 2)
            out["overestimated_tokens"] = int(max(int(out.get("overestimated_tokens") or 0), rw_over))
            out["max_token_drift_s"] = round(max(float(out.get("max_token_drift_s") or 0.0), rw_max), 2)
        return out
    if rw_pos > 0.0:
        return {
            "positive_drift_s": rw_pos,
            "overestimated_tokens": rw_over,
            "max_token_drift_s": rw_max,
            "quota_probe_errors": 0,
        }
    return None


def record_admission_refusal(
    space_name: str,
    token: str | None,
    requested_s: float,
    left_s: float,
    *,
    update_space_gate: bool = True,
    session_mode: bool = False,
    session_chars: int | None = None,
    error_text: str | None = None,
    drift_out: list[float] | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Update `space_gates[space_name]` (when `update_space_gate` is True), `session_space_floors[space_name]`
    (when `session_mode` qualifies under Part 5), and `token`'s `remaining_s` from a numeric refusal."""
    now = time.time() if now_ts is None else float(now_ts)
    tok_clean = str(token).strip() if token else ""

    observed_drift: list[float] = []

    def _mutate(st: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        if update_space_gate and space_name and requested_s > 0:
            st["space_gates"][str(space_name)] = (
                int(requested_s) if float(requested_s).is_integer() else round(float(requested_s), 2)
            )
        if session_mode and space_name and requested_s > 0 and session_chars is not None:
            _maybe_apply_session_space_floor(st, space_name, requested_s, session_chars)
        if tok_clean:
            d_key, existing = lookup_token_entry(st, tok_clean)
            clean_left = round(max(0.0, float(left_s)), 2)
            existing_rem = (
                float(existing["remaining_s"])
                if (existing is not None and existing.get("remaining_s") is not None)
                else DEFAULT_TOKEN_BASE_S
            )
            signed_drift = round(existing_rem - clean_left, 2)
            observed_drift.clear()
            observed_drift.append(signed_drift)
            if drift_out is not None:
                drift_out.clear()
                drift_out.append(signed_drift)

            existing_base_s = (
                float(existing.get("base_s") or DEFAULT_TOKEN_BASE_S)
                if existing
                else DEFAULT_TOKEN_BASE_S
            )
            if error_text and "free zerogpu" in str(error_text).lower() and existing_base_s > 300.0:
                eff_base_s = round(max(300.0, clean_left), 2)
            else:
                eff_base_s = round(max(existing_base_s, clean_left), 2)

            wait_s = parse_refusal_retry_wait_s(error_text)
            existing_rst = str((existing or {}).get("resets_at") or "")
            existing_rst_ts = _parse_ts(existing_rst)
            if wait_s is not None and wait_s > 0:
                resets_at = _iso_utc(now + wait_s)
            elif existing_rst_ts is not None and existing_rst_ts > now:
                resets_at = existing_rst
            else:
                resets_at = _iso_utc(now + 86400.0)

            existing_rg, existing_ru = _extract_active_ceiling(existing, now)
            row_out: dict[str, Any] = {
                "status": "ok",
                "remaining_s": clean_left,
                "base_s": eff_base_s,
                "resets_at": resets_at,
                "cooldown_until": str((existing or {}).get("cooldown_until") or ""),
                "updated_at": _iso_utc(now),
            }
            if existing_rg is not None and existing_ru:
                if clean_left >= existing_rg:
                    # Revealed `left_s >= existing_rg` falsifies the tighter stale ceiling.
                    if requested_s is not None and float(requested_s) > 0:
                        row_out["refused_gate_s"] = round(max(0.01, float(requested_s)), 2)
                        row_out["refused_until"] = _iso_utc(now + REFUSAL_CEILING_TTL_SECONDS)
                    _refusal_rem_baseline[d_key] = clean_left
                else:
                    _copy_active_ceiling(existing, row_out, now)
                    prev_base = _refusal_rem_baseline.get(d_key)
                    if prev_base is None and existing is not None and existing.get("remaining_s") is not None:
                        prev_base = float(existing["remaining_s"])
                    _refusal_rem_baseline[d_key] = (
                        round(max(0.0, min(prev_base, clean_left)), 2)
                        if prev_base is not None
                        else clean_left
                    )
            else:
                _refusal_rem_baseline[d_key] = clean_left
            st["tokens"][d_key] = row_out
        return st, True

    # Stamp `aj_hf:truth_ts` BEFORE committing the `aj_hf:pool_state` update so any
    # concurrent debit acquiring `FOR UPDATE` after us is guaranteed to observe it.
    if tok_clean:
        clean_ver = round(max(0.0, float(left_s)), 2)
        record_token_truth_timestamp(tok_clean, now, verified_rem_s=clean_ver)
    saved = _mutate_pool_state(_mutate, now_ts=now)
    if tok_clean and observed_drift and observed_drift[0] > 0.0:
        _record_runtime_drift_observation(token_digest(tok_clean), observed_drift[0], now)
    return saved


def record_success_debit(
    space_name: str,
    token: str | None,
    *,
    debit_s: float | None = None,
    attempt_started_ts: float | None = None,
    skipped_out: list[bool] | None = None,
    now_ts: float | None = None,
) -> dict[str, Any] | None:
    """Debit `space_name`'s gate (or explicit `debit_s`) from `token`'s `remaining_s` after a successful synthesis."""
    if not token or not str(token).strip():
        return None
    now = time.time() if now_ts is None else float(now_ts)
    if _is_cpu_demo_space_name(space_name) or (debit_s is not None and float(debit_s) == 0.0):
        return load_pool_state(now_ts=now)

    tok_clean = str(token).strip()
    full_d = token_digest(tok_clean)

    def _apply_debit_to_state(
        st: dict[str, Any],
        aux_map: Mapping[str, str | None] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if attempt_started_ts is not None:
            raw_truth = aux_map.get(TRUTH_TS_STORE_KEY) if aux_map is not None else None
            truth_ts = _resolve_truth_ts_from_aux(full_d, raw_truth)
            if truth_ts is not None and truth_ts >= float(attempt_started_ts):
                if skipped_out is not None:
                    skipped_out.clear()
                    skipped_out.append(True)
                return st, False

        d_key, existing = lookup_token_entry(st, tok_clean)
        # Part P1.5b: default unknown-Space legacy/recap gate to DEFAULT_SPACE_GATE_S (60.0 s) instead of 0.0.
        known_gate = (
            float(debit_s)
            if (debit_s is not None and debit_s > 0)
            else get_space_gate(space_name, state=st, default=0.0)
        )
        is_fallback_gate = known_gate <= 0.0
        gate = DEFAULT_SPACE_GATE_S if is_fallback_gate else known_gate
        if existing is None and (not st.get("tokens") or gate <= 0):
            return st, False
        if existing is None:
            base_s = max(DEFAULT_TOKEN_BASE_S, gate)
            new_rem = max(0.0, base_s - gate)
            resets_at = _iso_utc(now + 86400.0)
        else:
            base_s = float(existing.get("base_s") or DEFAULT_TOKEN_BASE_S)
            existing_rem = existing.get("remaining_s")
            old_rem = float(existing_rem) if existing_rem is not None else 0.0
            eff_debit = (
                round(old_rem * 0.5, 2)
                if (is_fallback_gate and 0.0 < old_rem < gate)
                else gate
            )
            new_rem = max(0.0, old_rem - eff_debit) if eff_debit > 0 else old_rem
            resets_at = existing.get("resets_at") or _iso_utc(now + 86400.0)
        row_out: dict[str, Any] = {
            "status": "ok",
            "remaining_s": round(new_rem, 2),
            "base_s": round(base_s, 2),
            "resets_at": resets_at,
            "cooldown_until": "",
            "updated_at": _iso_utc(now),
        }
        _copy_active_ceiling(existing, row_out, now)
        st["tokens"][d_key] = row_out
        return st, True

    if attempt_started_ts is not None:
        saved = _mutate_pool_state(
            _apply_debit_to_state,
            now_ts=now,
            aux_keys=(TRUTH_TS_STORE_KEY,),
        )
    else:
        saved = _mutate_pool_state(lambda st: _apply_debit_to_state(st, None), now_ts=now)
    record_token_synthesis_attempt(tok_clean, now_ts=now)
    return saved


def record_failure_debit(
    space_name: str,
    token: str | None,
    debit_s: float,
    *,
    attempt_started_ts: float | None = None,
    skipped_out: list[bool] | None = None,
    now_ts: float | None = None,
) -> dict[str, Any] | None:
    """Method 2: subtract estimated failure `debit_s` from `token`'s `remaining_s` (floor 0.0)
    via atomic mutation without ever touching `_refusal_rem_baseline`, `refused_gate_s`, or `resets_at`."""
    if not token or not str(token).strip() or debit_s is None or float(debit_s) <= 0.0:
        return None
    if _is_cpu_demo_space_name(space_name):
        return None
    now = time.time() if now_ts is None else float(now_ts)
    tok_clean = str(token).strip()
    full_d = token_digest(tok_clean)
    amt = float(debit_s)
    applied: list[bool] = []

    def _apply_fail_to_state(
        st: dict[str, Any],
        aux_map: Mapping[str, str | None] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if attempt_started_ts is not None:
            raw_truth = aux_map.get(TRUTH_TS_STORE_KEY) if aux_map is not None else None
            truth_ts = _resolve_truth_ts_from_aux(full_d, raw_truth)
            if truth_ts is not None and truth_ts >= float(attempt_started_ts):
                if skipped_out is not None:
                    skipped_out.clear()
                    skipped_out.append(True)
                return st, False

        d_key, existing = lookup_token_entry(st, tok_clean)
        if existing is None:
            if not st.get("tokens"):
                return st, False
            base_s = DEFAULT_TOKEN_BASE_S
            new_rem = max(0.0, base_s - amt)
            st["tokens"][d_key] = {
                "status": "ok",
                "remaining_s": round(new_rem, 2),
                "base_s": round(base_s, 2),
                "resets_at": "",
                "cooldown_until": "",
                "updated_at": _iso_utc(now),
            }
            applied.append(True)
            return st, True

        if str(existing.get("status") or "ok").strip().lower() == "auth_dead":
            return st, False

        base_s = float(existing.get("base_s") or DEFAULT_TOKEN_BASE_S)
        existing_rem = existing.get("remaining_s")
        old_rem = float(existing_rem) if existing_rem is not None else 0.0
        new_rem = max(0.0, old_rem - amt)
        row_out: dict[str, Any] = {
            "status": "ok",
            "remaining_s": round(new_rem, 2),
            "base_s": round(base_s, 2),
            "resets_at": str(existing.get("resets_at") or ""),
            "cooldown_until": str(existing.get("cooldown_until") or ""),
            "updated_at": _iso_utc(now),
        }
        _copy_active_ceiling(existing, row_out, now)
        st["tokens"][d_key] = row_out
        applied.append(True)
        return st, True

    if attempt_started_ts is not None:
        saved = _mutate_pool_state(
            _apply_fail_to_state,
            now_ts=now,
            aux_keys=(TRUTH_TS_STORE_KEY,),
        )
    else:
        saved = _mutate_pool_state(lambda st: _apply_fail_to_state(st, None), now_ts=now)
    if applied:
        record_token_taint(tok_clean, "failure_debit", now_ts=now)
    return saved


def record_quota_dead(
    token: str | None,
    *,
    cooldown_seconds: float = QUOTA_DEAD_COOLDOWN_SECONDS,
    now_ts: float | None = None,
) -> dict[str, Any] | None:
    """Mark `token` quota-exhausted (`remaining_s=0`, cooling down)."""
    if not token or not str(token).strip():
        return None
    now = time.time() if now_ts is None else float(now_ts)

    def _mutate(st: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        d_key, existing = lookup_token_entry(st, token)
        base_s = float((existing or {}).get("base_s") or DEFAULT_TOKEN_BASE_S)
        resets_at = (existing or {}).get("resets_at") or _iso_utc(now + 86400.0)
        st["tokens"][d_key] = {
            "status": "ok",
            "remaining_s": 0.0,
            "base_s": round(base_s, 2),
            "resets_at": resets_at,
            "cooldown_until": _iso_utc(now + max(0.0, float(cooldown_seconds))),
            "updated_at": _iso_utc(now),
        }
        return st, True

    return _mutate_pool_state(_mutate, now_ts=now)


def record_space_timeout(
    space_name: str,
    *,
    cooldown_seconds: float = SPACE_TIMEOUT_COOLDOWN_SECONDS,
    now_ts: float | None = None,
) -> dict[str, Any] | None:
    """Part C1 & C4: record a 10-minute Space-level timeout cooldown and increment
    `space_timeouts[space_name]` in `aj_hf:pool_state` (tokens are untouched)."""
    sp = str(space_name or "").strip()
    if not sp:
        return None
    now = time.time() if now_ts is None else float(now_ts)

    def _mutate(st: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        cds = dict(st.get("space_cooldowns") or {})
        if float(cooldown_seconds) > 0:
            cds[sp] = _iso_utc(now + float(cooldown_seconds))
        st["space_cooldowns"] = cds
        tos = dict(st.get("space_timeouts") or {})
        tos[sp] = int(tos.get(sp) or 0) + 1
        st["space_timeouts"] = tos
        return st, True

    return _mutate_pool_state(_mutate, now_ts=now)


def get_space_cooldown_until(
    space_name: str,
    *,
    state: Mapping[str, Any] | None = None,
    now_ts: float | None = None,
) -> str | None:
    """Return active ISO `cooldown_until` for `space_name` if cooling down, else None."""
    sp = str(space_name or "").strip()
    if not sp:
        return None
    now = time.time() if now_ts is None else float(now_ts)
    st = load_pool_state(now_ts=now) if state is None else normalize_pool_state(state, now_ts=now)
    cds = st.get("space_cooldowns")
    if not isinstance(cds, Mapping):
        return None
    cd_str = cds.get(sp)
    if not cd_str:
        return None
    cd_ts = _parse_ts(cd_str)
    if cd_ts is not None and cd_ts > now:
        return str(cd_str)
    return None


def is_space_cooling(
    space_name: str,
    *,
    state: Mapping[str, Any] | None = None,
    now_ts: float | None = None,
) -> bool:
    """Part C1: True when `space_name` has an active timeout circuit-breaker cooldown."""
    return get_space_cooldown_until(space_name, state=state, now_ts=now_ts) is not None


def get_space_timeout_counts(
    *,
    state: Mapping[str, Any] | None = None,
    now_ts: float | None = None,
) -> dict[str, int]:
    """Part C4: running per-Space timeout counts from `aj_hf:pool_state`."""
    now = time.time() if now_ts is None else float(now_ts)
    st = load_pool_state(now_ts=now) if state is None else normalize_pool_state(state, now_ts=now)
    tos = st.get("space_timeouts")
    if not isinstance(tos, Mapping):
        return {}
    return {str(k): int(v) for k, v in tos.items() if int(v or 0) > 0}


def probe_whoami_status(token: str, timeout: float = 8.0) -> tuple[int | None, str | None]:
    """Read-only `GET /api/whoami-v2` probe; NEVER logs or echoes the token."""
    req = urllib.request.Request(
        "https://huggingface.co/api/whoami-v2",
        headers={"User-Agent": _BROWSER_UA, "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                body = json.loads(raw or b"{}")
            except Exception:
                body = {}
            acct = body.get("name") if isinstance(body, dict) else None
            return int(resp.status), (str(acct) if acct else None)
    except urllib.error.HTTPError as exc:
        return int(exc.code), None
    except Exception:
        return None, None


def handle_auth_failure(
    token: str,
    *,
    whoami_fn: Callable[[str], tuple[int | None, str | None]] | None = None,
    now_ts: float | None = None,
) -> bool:
    """On a 401/403 synthesis error, confirm once via `whoami-v2`.

    - If `whoami-v2` returns 401 or 403 -> quarantine `auth_dead` for 24 h (returns True).
    - If `whoami-v2` returns 200 -> token itself is valid (space-level 403); clear any
      false auth mark and return False.
    - If `whoami-v2` is unreachable (status None) -> apply short 30 min cooldown only,
      do not quarantine 24 h (returns False).
    """
    if not token or not str(token).strip():
        return False
    now = time.time() if now_ts is None else float(now_ts)
    probe = whoami_fn or probe_whoami_status
    status_code, _acct = probe(str(token).strip())

    def _mutate(st: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        d_key, existing = lookup_token_entry(st, token)
        base_s = float((existing or {}).get("base_s") or DEFAULT_TOKEN_BASE_S)
        resets_at = (existing or {}).get("resets_at") or ""
        existing_rem = (existing or {}).get("remaining_s")
        if status_code in (401, 403):
            _refusal_rem_baseline.pop(d_key, None)
            st["tokens"][d_key] = {
                "status": "auth_dead",
                "remaining_s": 0.0,
                "base_s": round(base_s, 2),
                "resets_at": resets_at,
                "cooldown_until": _iso_utc(now + AUTH_DEAD_QUARANTINE_SECONDS),
                "updated_at": _iso_utc(now),
            }
            return st, True
        if status_code == 200:
            rem_s = float(existing_rem) if existing_rem is not None else base_s
            row_ok: dict[str, Any] = {
                "status": "ok",
                "remaining_s": round(rem_s, 2),
                "base_s": round(base_s, 2),
                "resets_at": resets_at,
                "cooldown_until": "",
                "updated_at": _iso_utc(now),
            }
            _copy_active_ceiling(existing, row_ok, now)
            st["tokens"][d_key] = row_ok
            return st, True
        # Transport error on confirmation probe: conservative 30m cooldown, status stays ok.
        rem_s = float(existing_rem) if existing_rem is not None else 0.0
        row_cd: dict[str, Any] = {
            "status": "ok",
            "remaining_s": round(rem_s, 2),
            "base_s": round(base_s, 2),
            "resets_at": resets_at,
            "cooldown_until": _iso_utc(now + QUOTA_DEAD_COOLDOWN_SECONDS),
            "updated_at": _iso_utc(now),
        }
        _copy_active_ceiling(existing, row_cd, now)
        st["tokens"][d_key] = row_cd
        return st, True

    _mutate_pool_state(_mutate, now_ts=now)
    return status_code in (401, 403)


def reconcile_from_probe_rows(
    tokens: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    *,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Reconcile exact `remaining_s` / `base_s` / `resets_at` / `auth_dead` from probes,
    preserving active refusal ceilings unless probed balance rose >= 25 s (Part P1.4)
    and recording additive `drift_stats` (Part Q4.2)."""
    now = time.time() if now_ts is None else float(now_ts)
    rw_pos, rw_over, rw_max = _summarize_runtime_drift_window(now)
    summary_box: dict[str, Any] = {}

    def _mutate(st: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        active_digests: set[str] = set()
        positive_drift_s = 0.0
        overestimated_tokens = 0
        max_token_drift_s = 0.0
        quota_probe_errors = 0
        probed_balance_count = 0
        prev_drift = st.get("drift_stats") if isinstance(st.get("drift_stats"), Mapping) else {}

        for idx, tok in enumerate(tokens):
            if not tok or not str(tok).strip():
                continue
            d_key, existing = lookup_token_entry(st, tok)
            # Migrate legacy 12-char digest keys onto canonical SHA-256 hex digest.
            full_d = token_digest(tok)
            if d_key != full_d and d_key in st["tokens"]:
                st["tokens"].pop(d_key, None)
            active_digests.add(full_d)
            row = rows[idx] if idx < len(rows) and isinstance(rows[idx], Mapping) else {}
            probe_status = str(row.get("status") or "").lower()
            existing_rem = (existing or {}).get("remaining_s")

            if probe_status == "dead":
                _refusal_rem_baseline.pop(full_d, None)
                base_s = float(row.get("base_s") or (existing or {}).get("base_s") or 0.0)
                st["tokens"][full_d] = {
                    "status": "auth_dead",
                    "remaining_s": 0.0,
                    "base_s": round(base_s, 2),
                    "resets_at": str(row.get("quota_resets_at") or (existing or {}).get("resets_at") or ""),
                    "cooldown_until": _iso_utc(now + AUTH_DEAD_QUARANTINE_SECONDS),
                    "updated_at": _iso_utc(now),
                }
            elif probe_status == "ok":
                rem_val = row.get("remaining_s")
                base_val = row.get("base_s")
                q_err = row.get("quota_error")
                # Round 2 Part 1: `quota_probe_errors` counts only tokens actually attempted that cycle
                # (tokens skipped via `quota_scope_unsupported` circuit breaker were not attempted).
                if q_err != "quota_scope_unsupported" and (q_err is not None or rem_val is None):
                    quota_probe_errors += 1
                if rem_val is not None:
                    probed_balance_count += 1

                # Part Q4.2: measure pre-overwrite ledger drift against probed ground truth.
                if (
                    rem_val is not None
                    and existing is not None
                    and str(existing.get("status") or "ok") != "auth_dead"
                    and existing_rem is not None
                ):
                    delta = float(existing_rem) - float(rem_val)
                    if delta > 0.0:
                        positive_drift_s += delta
                        if delta > max_token_drift_s:
                            max_token_drift_s = delta
                        if delta > 25.0:
                            overestimated_tokens += 1

                # Part P1.5a: explicit `is not None` check so `0.0` survives `rem_val=None` probe errors!
                if rem_val is not None:
                    rem_s = float(rem_val)
                elif existing_rem is not None:
                    rem_s = float(existing_rem)
                else:
                    rem_s = DEFAULT_TOKEN_BASE_S

                base_s = (
                    float(base_val)
                    if base_val is not None
                    else float((existing or {}).get("base_s") or max(DEFAULT_TOKEN_BASE_S, rem_s))
                )
                resets_at = str(row.get("quota_resets_at") or (existing or {}).get("resets_at") or "")
                cd_until = ""
                if rem_s <= 0:
                    cd_until = (existing or {}).get("cooldown_until") or _iso_utc(now + QUOTA_DEAD_COOLDOWN_SECONDS)
                elif rem_val is None:
                    cd_until = str((existing or {}).get("cooldown_until") or "")

                preserve_updated_at = (
                    rem_val is None
                    and existing is not None
                    and str(existing.get("status") or "ok").strip().lower() == "ok"
                    and bool(str(existing.get("updated_at") or "").strip())
                )
                new_row: dict[str, Any] = {
                    "status": "ok",  # probe 200 auto-clears any prior auth_dead
                    "remaining_s": round(max(0.0, rem_s), 2),
                    "base_s": round(max(0.0, base_s), 2),
                    "resets_at": resets_at,
                    "cooldown_until": cd_until,
                    "updated_at": str(existing["updated_at"]) if preserve_updated_at else _iso_utc(now),
                }

                # Part P1.4: preserve active refusal ceiling UNLESS probed balance rose >= 25 s
                # over the balance recorded at refusal time (genuine rolling refill).
                existing_rg, existing_ru = _extract_active_ceiling(existing, now)
                if existing_rg is not None and existing_ru:
                    baseline_rem = _refusal_rem_baseline.get(full_d)
                    if baseline_rem is None and existing_rem is not None:
                        baseline_rem = float(existing_rem)
                    refilled = (
                        rem_val is not None
                        and baseline_rem is not None
                        and (float(rem_val) - float(baseline_rem)) >= REFUSAL_REFILL_CLEAR_DELTA_S
                    )
                    if refilled:
                        _refusal_rem_baseline.pop(full_d, None)
                    else:
                        new_row["refused_gate_s"] = existing_rg
                        new_row["refused_until"] = existing_ru
                        if baseline_rem is None and rem_val is not None:
                            _refusal_rem_baseline[full_d] = float(rem_val)
                else:
                    _refusal_rem_baseline.pop(full_d, None)

                st["tokens"][full_d] = new_row
            else:
                if existing is not None:
                    st["tokens"][full_d] = existing
        # Prune digests that are no longer in the configured env pool so removed
        # dead tokens auto-clear immediately when the env set changes.
        if active_digests:
            st["tokens"] = {
                k: v for k, v in st["tokens"].items() if k in active_digests
            }

        if probed_balance_count == 0:
            positive_drift_s = max(float(prev_drift.get("positive_drift_s") or 0.0), rw_pos)
            overestimated_tokens = max(int(prev_drift.get("overestimated_tokens") or 0), rw_over)
            max_token_drift_s = max(float(prev_drift.get("max_token_drift_s") or 0.0), rw_max)

        drift_stats = {
            "positive_drift_s": round(positive_drift_s, 2),
            "overestimated_tokens": int(overestimated_tokens),
            "max_token_drift_s": round(max_token_drift_s, 2),
            "quota_probe_errors": int(quota_probe_errors),
        }
        st["drift_stats"] = drift_stats
        summary_box["tokens"] = len(active_digests)
        summary_box["drift_stats"] = drift_stats
        return st, True

    saved = _mutate_pool_state(_mutate, now_ts=now)
    ds_logged = summary_box.get("drift_stats") or saved.get("drift_stats") or {}
    logger.info(
        "hf_pool_reconcile_summary tokens=%d positive_drift_s=%.1f overestimated_tokens=%d "
        "max_token_drift_s=%.1f quota_probe_errors=%d",
        int(summary_box.get("tokens") or 0),
        float(ds_logged.get("positive_drift_s") or 0.0),
        int(ds_logged.get("overestimated_tokens") or 0),
        float(ds_logged.get("max_token_drift_s") or 0.0),
        int(ds_logged.get("quota_probe_errors") or 0),
    )
    return saved


def reconcile_if_env_changed(
    tokens: Sequence[str],
    *,
    probe_fn: Callable[[int, str], dict[str, Any]] | None = None,
    force_probe: bool = False,
    max_workers: int = 8,
    budget_s: float = 30.0,
) -> bool:
    """Seed/reconcile the ledger on boot or whenever the HF env token set changes.

    Returns True when a probe pass executed.
    """
    global _last_seen_env_hash, _known_token_digests
    clean_tokens = [str(t).strip() for t in tokens if t and str(t).strip()]
    if not clean_tokens:
        return False
    cur_hash = compute_env_tokens_hash(clean_tokens)
    stored_hash = botsetting_sync_read(ENV_HASH_STORE_KEY)
    st = load_pool_state()

    with _lock:
        same_mem = (_last_seen_env_hash == cur_hash)
        same_store = (stored_hash == cur_hash) if stored_hash else False
        has_tokens = bool(st.get("tokens"))
        if not force_probe and has_tokens and (same_mem or same_store):
            _last_seen_env_hash = cur_hash
            return False

        # If the env token set changed, immediately clear stale auth_dead marks
        # on newly changed/rotated token sets before probing.
        new_digests = {token_digest(t) for t in clean_tokens}
        if _known_token_digests and new_digests != _known_token_digests:
            for d_key, row in list(st["tokens"].items()):
                if d_key not in new_digests:
                    st["tokens"].pop(d_key, None)
                elif row.get("status") == "auth_dead":
                    row["status"] = "ok"
                    row["cooldown_until"] = ""
            save_pool_state(st)
        _known_token_digests = new_digests
        _last_seen_env_hash = cur_hash

    if probe_fn is None:
        from app.services.key_health import _probe_hf_account as _default_probe
        probe_fn = _default_probe

    rows_by_idx: dict[int, dict[str, Any]] = {}
    workers = max(1, min(int(max_workers), len(clean_tokens)))
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = {ex.submit(probe_fn, i, tok): i for i, tok in enumerate(clean_tokens)}
        done, _pending = wait(futs, timeout=budget_s)
        for fut in done:
            idx = futs[fut]
            try:
                rows_by_idx[idx] = fut.result()
            except Exception as exc:
                rows_by_idx[idx] = {"index": idx, "status": "error", "quota_error": type(exc).__name__}
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    ordered_rows = [
        rows_by_idx.get(i) or {"index": i, "status": "error"}
        for i in range(len(clean_tokens))
    ]
    reconcile_from_probe_rows(clean_tokens, ordered_rows)
    botsetting_sync_write(ENV_HASH_STORE_KEY, cur_hash)
    return True


def acquire_in_flight_gpu(job_id: str, gpu_s: float, *, now_ts: float | None = None) -> float:
    """Increment `in_flight_gpu_s` idempotently for `job_id`. Returns updated total."""
    global _last_evicted_stale_gpu_s
    if not job_id or gpu_s <= 0:
        st = load_pool_state(now_ts=now_ts)
        return float(st.get("in_flight_gpu_s") or 0.0)
    now = time.time() if now_ts is None else float(now_ts)
    with _lock:
        _last_evicted_stale_gpu_s = 0.0
        jobs_map = _load_in_flight_map(now)
        evicted_s = float(_last_evicted_stale_gpu_s or 0.0)
        prev = float((jobs_map.get(str(job_id)) or {}).get("gpu_s") or 0.0)
        target = round(max(0.0, float(gpu_s)), 2)
        jobs_map[str(job_id)] = {"gpu_s": target, "ts": now}
        _save_in_flight_map(jobs_map)
        tracked_sum = sum(float(m.get("gpu_s") or 0.0) for m in jobs_map.values())

        def _mutate(st: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            base_total = max(0.0, float(st.get("in_flight_gpu_s") or 0.0) - evicted_s)
            cur_total = max(0.0, base_total - prev + target)
            st["in_flight_gpu_s"] = round(max(cur_total, tracked_sum), 2)
            return st, True

        saved = _mutate_pool_state(_mutate, now_ts=now)
        return float(saved["in_flight_gpu_s"])


def release_in_flight_gpu(job_id: str, *, now_ts: float | None = None) -> float:
    """Release `job_id`'s reservation from `in_flight_gpu_s` idempotently."""
    global _last_evicted_stale_gpu_s
    if not job_id:
        st = load_pool_state(now_ts=now_ts)
        return float(st.get("in_flight_gpu_s") or 0.0)
    now = time.time() if now_ts is None else float(now_ts)
    with _lock:
        _last_evicted_stale_gpu_s = 0.0
        jobs_map = _load_in_flight_map(now)
        evicted_s = float(_last_evicted_stale_gpu_s or 0.0)
        popped = jobs_map.pop(str(job_id), None)
        _save_in_flight_map(jobs_map)
        if popped is not None or evicted_s > 0.0:
            delta = float((popped or {}).get("gpu_s") or 0.0) + evicted_s

            def _mutate(st: dict[str, Any]) -> tuple[dict[str, Any], bool]:
                new_total = max(0.0, float(st.get("in_flight_gpu_s") or 0.0) - delta)
                st["in_flight_gpu_s"] = round(new_total, 2)
                return st, True

            saved = _mutate_pool_state(_mutate, now_ts=now)
            return float(saved["in_flight_gpu_s"])
        st = load_pool_state(now_ts=now)
        return float(st.get("in_flight_gpu_s") or 0.0)


def reconcile_in_flight_gpu(
    active_running_job_ids: Sequence[str] | set[str] | Any,
    *,
    now_ts: float | None = None,
) -> float:
    """Intersect `aj_hf:in_flight` with live `RUNNING` job IDs, persist the pruned map,
    and set `aj_hf:pool_state.in_flight_gpu_s = round(sum(kept gpu_s), 2)`."""
    global _last_evicted_stale_gpu_s
    now = time.time() if now_ts is None else float(now_ts)
    active_ids = {str(jid) for jid in (active_running_job_ids or ()) if jid}
    with _lock:
        _last_evicted_stale_gpu_s = 0.0
        jobs_map = _load_in_flight_map(now)
        evicted_s = float(_last_evicted_stale_gpu_s or 0.0)
        kept = {jid: dict(meta) for jid, meta in jobs_map.items() if jid in active_ids}
        if kept != jobs_map or evicted_s > 0.0:
            _save_in_flight_map(kept)
        tracked_sum = round(sum(float(m.get("gpu_s") or 0.0) for m in kept.values()), 2)
        cur_st = load_pool_state(force=True, now_ts=now)
        cur_val = round(max(0.0, float(cur_st.get("in_flight_gpu_s") or 0.0)), 2)
        if cur_val == tracked_sum:
            return tracked_sum

        def _mutate(st: dict[str, Any]) -> tuple[dict[str, Any], bool]:
            prev_val = round(max(0.0, float(st.get("in_flight_gpu_s") or 0.0)), 2)
            if prev_val == tracked_sum:
                return st, False
            st["in_flight_gpu_s"] = tracked_sum
            return st, True

        saved = _mutate_pool_state(_mutate, now_ts=now)
        return float(saved.get("in_flight_gpu_s") or 0.0)


@dataclass(frozen=True)
class PoolMetrics:
    seeded: bool
    effective_usable_gpu_s: float
    in_flight_gpu_s: float
    available_gpu_s: float
    total_base_gpu_s: float
    usable_fraction: float
    gate_s: float
    solvent_slots: int
    band: str
    allow_optional_gpu_regen: bool
    high_demand_hint: bool
    auth_dead_count: int


def watermark_band_for_fraction(fraction: float) -> str:
    """Map effective usable fraction of pool base to the owner-approved watermark band."""
    f = float(fraction)
    if f > WATERMARK_NORMAL_MIN:
        return BAND_NORMAL
    if f > WATERMARK_CONSERVATION_MIN:
        return BAND_CONSERVATION
    if f >= WATERMARK_SHORT_JOB_MIN:
        return BAND_SHORT_JOB
    return BAND_DRY


def compute_pool_metrics(
    *,
    gate_s: float | None = None,
    state: Mapping[str, Any] | None = None,
    force_reload: bool = False,
    now_ts: float | None = None,
) -> PoolMetrics:
    """Compute effective usable GPU-seconds, solvent slots, and watermark band."""
    now = time.time() if now_ts is None else float(now_ts)
    st = (
        load_pool_state(force=force_reload, now_ts=now)
        if state is None
        else normalize_pool_state(state, now_ts=now)
    )
    tokens_map = st.get("tokens") or {}
    eff_gate = float(gate_s) if (gate_s is not None and gate_s > 0) else get_space_gate(state=st)
    eff_gate = max(1.0, eff_gate)

    if not tokens_map:
        return PoolMetrics(
            seeded=False,
            effective_usable_gpu_s=0.0,
            in_flight_gpu_s=float(st.get("in_flight_gpu_s") or 0.0),
            available_gpu_s=0.0,
            total_base_gpu_s=0.0,
            usable_fraction=1.0,
            gate_s=eff_gate,
            solvent_slots=0,
            band=BAND_NORMAL,
            allow_optional_gpu_regen=True,
            high_demand_hint=False,
            auth_dead_count=0,
        )

    effective_usable = 0.0
    total_base = 0.0
    solvent_slots = 0
    auth_dead_count = 0

    for row in tokens_map.values():
        status = str(row.get("status") or "ok")
        if status == "auth_dead":
            auth_dead_count += 1
            continue
        rem = max(0.0, float(row.get("remaining_s") or 0.0))
        base = max(rem, float(row.get("base_s") or DEFAULT_TOKEN_BASE_S))
        total_base += base
        cd_ts = _parse_ts(row.get("cooldown_until"))
        if cd_ts is not None and cd_ts > now and rem <= 0.0:
            continue
        if rem >= MIN_USABLE_DUST_GATE_S:
            effective_usable += rem
        if rem >= eff_gate:
            solvent_slots += int(math.floor(rem / eff_gate))

    in_flight = max(0.0, float(st.get("in_flight_gpu_s") or 0.0))
    available = max(0.0, effective_usable - in_flight)
    fraction = (available / total_base) if total_base > 0 else 0.0
    band = watermark_band_for_fraction(fraction)
    return PoolMetrics(
        seeded=True,
        effective_usable_gpu_s=round(effective_usable, 2),
        in_flight_gpu_s=round(in_flight, 2),
        available_gpu_s=round(available, 2),
        total_base_gpu_s=round(total_base, 2),
        usable_fraction=round(fraction, 6),
        gate_s=round(eff_gate, 2),
        solvent_slots=solvent_slots,
        band=band,
        allow_optional_gpu_regen=(band == BAND_NORMAL),
        high_demand_hint=(band != BAND_NORMAL),
        auth_dead_count=auth_dead_count,
    )


def estimate_refill_eta_label(
    needed_gpu_s: float = 0.0,
    *,
    state: Mapping[str, Any] | None = None,
    tz_name: str = "Asia/Yangon",
    force_reload: bool = False,
    now_ts: float | None = None,
) -> str:
    """Earliest Yangon `HH:MM` at which refilling tokens cross `needed_gpu_s * 1.25`."""
    now = time.time() if now_ts is None else float(now_ts)
    st = (
        load_pool_state(force=force_reload, now_ts=now)
        if state is None
        else normalize_pool_state(state, now_ts=now)
    )
    metrics = compute_pool_metrics(state=st, now_ts=now)
    threshold = max(metrics.gate_s, float(needed_gpu_s or 0.0) * RESUMER_SAFETY_MULTIPLIER)
    deficit = max(0.0, threshold - metrics.available_gpu_s)

    refill_events: list[tuple[float, float]] = []
    for row in (st.get("tokens") or {}).values():
        if row.get("status") == "auth_dead":
            continue
        rem = max(0.0, float(row.get("remaining_s") or 0.0))
        base = max(rem, float(row.get("base_s") or DEFAULT_TOKEN_BASE_S))
        gain = max(0.0, base - rem) if rem >= MIN_USABLE_DUST_GATE_S else base
        if gain <= 0:
            continue
        rst_ts = _parse_ts(row.get("resets_at")) or _parse_ts(row.get("cooldown_until"))
        if rst_ts is not None and rst_ts > now:
            refill_events.append((rst_ts, gain))

    target_ts: float | None = None
    if deficit <= 0 and refill_events:
        target_ts = min(ts for ts, _g in refill_events)
    elif refill_events:
        refill_events.sort(key=lambda item: item[0])
        acc = 0.0
        for ts, gain in refill_events:
            acc += gain
            if acc >= deficit:
                target_ts = ts
                break
        if target_ts is None:
            target_ts = refill_events[-1][0]

    if target_ts is None:
        # Fallback rolling-window estimate (~60 min ahead) when no per-token resets_at is recorded.
        target_ts = now + REFILL_ETA_MAX_HORIZON_SECONDS
    elif deficit <= 0 or (target_ts - now) > REFILL_ETA_MAX_HORIZON_SECONDS:
        target_ts = min(
            now + REFILL_ETA_MAX_HORIZON_SECONDS,
            max(now + REFILL_ETA_MIN_HORIZON_SECONDS, target_ts),
        )

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone(timedelta(hours=6, minutes=30))
    dt = datetime.fromtimestamp(target_ts, tz=tz)
    return dt.strftime("%H:%M")


def congestion_resume_eta_label(
    now_ts: float | None = None,
    *,
    state: Mapping[str, Any] | None = None,
    tz_name: str = "Asia/Yangon",
) -> str:
    """Return Yangon `HH:MM` ETA clamped to `[+10m, +20m]` from active `space_cooldowns`
    plus half a resumer sweep interval (`RESUMER_INTERVAL_SECONDS = 600 s`)."""
    now = time.time() if now_ts is None else float(now_ts)
    st = load_pool_state(now_ts=now) if state is None else normalize_pool_state(state, now_ts=now)
    cds = st.get("space_cooldowns")
    active_cd: list[float] = []
    if isinstance(cds, Mapping):
        for cd_str in cds.values():
            cd_ts = _parse_ts(cd_str)
            if cd_ts is not None and cd_ts > now:
                active_cd.append(cd_ts)
    if active_cd:
        raw_target = min(active_cd) + (CONGESTION_RESUMER_INTERVAL_SECONDS * 0.5)
    else:
        raw_target = now + CONGESTION_RESUMER_INTERVAL_SECONDS
    target_ts = min(
        now + CONGESTION_ETA_MAX_HORIZON_SECONDS,
        max(now + CONGESTION_ETA_MIN_HORIZON_SECONDS, raw_target),
    )
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone(timedelta(hours=6, minutes=30))
    dt = datetime.fromtimestamp(target_ts, tz=tz)
    return dt.strftime("%H:%M")


def has_any_non_cooling_space(
    spaces: Sequence[str] | None = None,
    *,
    state: Mapping[str, Any] | None = None,
    now_ts: float | None = None,
) -> bool:
    """True when at least one configured non-CPU, non-v1 VoxCPM2 Space is not in active
    `space_cooldowns` (returns True when no GPU Spaces are configured)."""
    now = time.time() if now_ts is None else float(now_ts)
    st = load_pool_state(now_ts=now) if state is None else normalize_pool_state(state, now_ts=now)
    try:
        from app.services.voxcpm_engine import (
            get_configured_spaces,
            is_cpu_demo_space,
            is_voxcpm_v1_space,
        )

        raw_spaces = list(spaces) if spaces is not None else list(get_configured_spaces())
        gpu_spaces = [
            str(sp).strip()
            for sp in raw_spaces
            if str(sp).strip()
            and not is_voxcpm_v1_space(sp)
            and not is_cpu_demo_space(sp)
        ]
    except Exception:
        gpu_spaces = [str(sp).strip() for sp in (spaces or ()) if str(sp).strip()]
    if not gpu_spaces:
        return True
    return any(not is_space_cooling(sp, state=st, now_ts=now) for sp in gpu_spaces)


def has_solvent_token_at_gate(
    gate_s: float = 45.0,
    *,
    state: Mapping[str, Any] | None = None,
    now_ts: float | None = None,
) -> bool:
    """True when at least one non-dead, non-cooling, uncapped token in `state` has
    `remaining_s >= max(45.0, gate_s)` (returns True on unseeded empty `tokens` map)."""
    now = time.time() if now_ts is None else float(now_ts)
    st = load_pool_state(now_ts=now) if state is None else normalize_pool_state(state, now_ts=now)
    tokens_map = st.get("tokens") or {}
    if not tokens_map:
        return True
    eff_gate = max(45.0, float(gate_s or 45.0))
    for row in tokens_map.values():
        if not isinstance(row, Mapping):
            continue
        if str(row.get("status") or "ok").strip().lower() == "auth_dead":
            continue
        cd_ts = _parse_ts(row.get("cooldown_until"))
        if cd_ts is not None and cd_ts > now:
            continue
        rg, _ru = _extract_active_ceiling(row, now)
        if rg is not None and eff_gate >= rg:
            continue
        rem = max(0.0, float(row.get("remaining_s") or 0.0))
        if rem >= eff_gate:
            return True
    return False


@dataclass(frozen=True)
class AdmissionDecision:
    admitted: bool
    action: str          # "start" | "park" | "nudge_gemini" | "reject_free_capacity"
    reason: str
    band: str
    units: int
    gate_s: float
    needed_gpu_s: float
    available_gpu_s: float
    solvent_slots: int
    eta_label: str


def estimate_voxcpm_units_and_budget(
    *,
    kind: str,
    text: str = "",
    duration_seconds: float = 0.0,
    planned_segments: int | None = None,
    reference_transcript: str = "",
    control_instruction: str = "",
    reference_seconds: float = 0.0,
    gate_s: float | None = None,
    call_type: str | None = None,
    state: Mapping[str, Any] | None = None,
) -> tuple[int, float, float]:
    """Return `(units, gate_s, needed_gpu_s)` per Part 6 (#2) and session gate pricing."""
    st = load_pool_state() if state is None else state
    norm_kind = str(kind).lower()
    if norm_kind == "recap":
        eff_gate = float(gate_s) if (gate_s is not None and gate_s > 0) else get_space_gate(state=st)
        if planned_segments is not None and int(planned_segments) > 0:
            units = int(planned_segments)
        else:
            dur = float(duration_seconds or 0.0)
            if dur <= 0:
                dur = 60.0
            units = max(1, int(math.ceil(dur / 12.0)))
        needed_gpu_s = round(units * eff_gate * PRE_JOB_SAFETY_MULTIPLIER, 2)
        return units, eff_gate, needed_gpu_s

    from app.services.voice_chunking import (
        is_clone_session_slices_enabled,
        plan_session_slices,
        plan_text_chunks,
    )

    use_session = (
        (str(call_type).strip().lower() == "session")
        if call_type is not None
        else is_clone_session_slices_enabled()
    )
    if use_session:
        slices = plan_session_slices(text or "မင်္ဂလာပါ")
        units = max(1, len(slices))
        if gate_s is not None and gate_s > 0:
            slice_gates = [float(gate_s) for _ in slices]
        else:
            slice_gates = [session_call_gate_s(len(s)) for s in slices]
        total_raw_s = sum(slice_gates)
        eff_gate = round(total_raw_s / units, 2)
        needed_gpu_s = round(total_raw_s * PRE_JOB_SAFETY_MULTIPLIER, 2)
        return units, eff_gate, needed_gpu_s

    eff_gate = float(gate_s) if (gate_s is not None and gate_s > 0) else get_space_gate(state=st)
    chunks = plan_text_chunks(
        text or "မင်္ဂလာပါ",
        reference_transcript=reference_transcript,
        control_instruction=control_instruction,
        reference_seconds=reference_seconds,
    )
    units = max(1, len(chunks))
    needed_gpu_s = round(units * eff_gate * PRE_JOB_SAFETY_MULTIPLIER, 2)
    return units, eff_gate, needed_gpu_s


def evaluate_voxcpm_admission(
    *,
    kind: str,
    is_paid: bool,
    units: int,
    est_audio_seconds: float = 0.0,
    gate_s: float | None = None,
    needed_gpu_s: float | None = None,
    state: Mapping[str, Any] | None = None,
    force_reload: bool = False,
    now_ts: float | None = None,
) -> AdmissionDecision:
    """Evaluate Part 6 pre-job capacity + Part 7 watermark bands."""
    now = time.time() if now_ts is None else float(now_ts)
    st = (
        load_pool_state(force=force_reload, now_ts=now)
        if state is None
        else normalize_pool_state(state, now_ts=now)
    )
    metrics = compute_pool_metrics(gate_s=gate_s, state=st, now_ts=now)
    eff_units = max(1, int(units))
    eff_needed_gpu_s = (
        round(max(0.0, float(needed_gpu_s)), 2)
        if (needed_gpu_s is not None and needed_gpu_s > 0)
        else round(eff_units * metrics.gate_s * PRE_JOB_SAFETY_MULTIPLIER, 2)
    )

    if not metrics.seeded:
        return AdmissionDecision(
            admitted=True,
            action="start",
            reason="unseeded_permissive",
            band=BAND_NORMAL,
            units=eff_units,
            gate_s=metrics.gate_s,
            needed_gpu_s=eff_needed_gpu_s,
            available_gpu_s=0.0,
            solvent_slots=0,
            eta_label="",
        )

    eta = estimate_refill_eta_label(eff_needed_gpu_s, state=st, now_ts=now)
    norm_kind = str(kind).lower()
    is_multi_recap = (norm_kind == "recap" and eff_units > 1)

    def _deny(reason: str) -> AdmissionDecision:
        if is_paid:
            act = "park"
        elif norm_kind == "recap":
            act = "nudge_gemini"
        else:
            act = "reject_free_capacity"
        return AdmissionDecision(
            admitted=False,
            action=act,
            reason=reason,
            band=metrics.band,
            units=eff_units,
            gate_s=metrics.gate_s,
            needed_gpu_s=eff_needed_gpu_s,
            available_gpu_s=metrics.available_gpu_s,
            solvent_slots=metrics.solvent_slots,
            eta_label=eta,
        )

    # 1. Part 7 < 8% Dry gate: hold ALL new VoxCPM starts.
    if metrics.band == BAND_DRY:
        return _deny("dry_gate")

    # 2. Part 7 20% -> 8% Short-job priority + soft watermark:
    if metrics.band == BAND_SHORT_JOB:
        if not is_paid and is_multi_recap:
            return _deny("watermark_free_multi_recap_paused")
        if not is_paid and norm_kind == "clone" and float(est_audio_seconds or 0.0) > 60.0:
            return _deny("watermark_free_long_clone_paused")
        if is_paid and is_multi_recap:
            req_slots = int(math.ceil(1.5 * eff_units))
            if metrics.solvent_slots < req_slots:
                return _deny("watermark_heavy_recap_insufficient_slots")

    # 3. Part 6 pre-job capacity + solvent-slot gate:
    if metrics.available_gpu_s < eff_needed_gpu_s or metrics.solvent_slots < eff_units:
        return _deny("insufficient_pool_capacity")

    return AdmissionDecision(
        admitted=True,
        action="start",
        reason="ok",
        band=metrics.band,
        units=eff_units,
        gate_s=metrics.gate_s,
        needed_gpu_s=eff_needed_gpu_s,
        available_gpu_s=metrics.available_gpu_s,
        solvent_slots=metrics.solvent_slots,
        eta_label=eta,
    )


def has_resumer_capacity(
    needed_gpu_s: float,
    *,
    state: Mapping[str, Any] | None = None,
    force_reload: bool = True,
    now_ts: float | None = None,
) -> bool:
    """Part 3 (#4): True iff `effective_usable_gpu_s - in_flight_gpu_s >= needed_gpu_s * 1.25`."""
    now = time.time() if now_ts is None else float(now_ts)
    st = (
        load_pool_state(force=force_reload, now_ts=now)
        if state is None
        else normalize_pool_state(state, now_ts=now)
    )
    metrics = compute_pool_metrics(state=st, now_ts=now)
    if not metrics.seeded:
        return False
    req = float(needed_gpu_s or 0.0)
    if req <= 0:
        req = metrics.gate_s
    return metrics.available_gpu_s >= (req * RESUMER_SAFETY_MULTIPLIER)


# ---------------------------------------------------------------------------
# Round 2 Part 2 & Part 3: Uncertainty Classifier + Calibration Sweep Writes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TokenUncertainty:
    slot: int
    token: str
    token_digest: str
    key_id: str
    is_uncertain: bool
    is_certain_full: bool
    reason: str
    reasons: tuple[str, ...]
    remaining_s: float
    base_s: float
    updated_at_ts: float | None
    last_truth_at: float | None
    priority_key: tuple[int, float, int]


def classify_token_uncertainty(
    token: str,
    *,
    slot: int = 0,
    state: Mapping[str, Any] | None = None,
    meta: Mapping[str, Any] | None = None,
    last_truth_at: float | None = None,
    reload_meta: bool = False,
    now_ts: float | None = None,
) -> TokenUncertainty:
    """Round 2 Part 2: classify one HF token as UNCERTAIN (rules 1–5) or CERTAIN/skipped."""
    now = time.time() if now_ts is None else float(now_ts)
    st = load_pool_state(now_ts=now) if state is None else normalize_pool_state(state, now_ts=now)
    tok_clean = str(token or "").strip()
    full_d = token_digest(tok_clean)
    short_id = _short_key_id(tok_clean)
    _d, row = lookup_token_entry(st, tok_clean)

    if row is None:
        return TokenUncertainty(
            slot=int(slot),
            token=tok_clean,
            token_digest=full_d,
            key_id=short_id,
            is_uncertain=False,
            is_certain_full=False,
            reason="",
            reasons=(),
            remaining_s=DEFAULT_TOKEN_BASE_S,
            base_s=DEFAULT_TOKEN_BASE_S,
            updated_at_ts=None,
            last_truth_at=last_truth_at,
            priority_key=(99, 0.0, int(slot)),
        )

    status = str(row.get("status") or "ok").strip().lower()
    rem_s = round(max(0.0, float(row.get("remaining_s") if row.get("remaining_s") is not None else 0.0)), 2)
    base_s = round(max(0.0, float(row.get("base_s") or DEFAULT_TOKEN_BASE_S)), 2)
    upd_ts = _parse_ts(row.get("updated_at"))
    rst_ts = _parse_ts(row.get("resets_at"))
    active_rg = get_token_refused_gate_s(row, now_ts=now)

    if status == "auth_dead":
        return TokenUncertainty(
            slot=int(slot),
            token=tok_clean,
            token_digest=full_d,
            key_id=short_id,
            is_uncertain=False,
            is_certain_full=False,
            reason="auth_dead",
            reasons=(),
            remaining_s=rem_s,
            base_s=base_s,
            updated_at_ts=upd_ts,
            last_truth_at=last_truth_at,
            priority_key=(99, 0.0, int(slot)),
        )

    truth_ts = (
        last_truth_at
        if last_truth_at is not None
        else get_token_last_truth_ts(full_d, reload_store=reload_meta)
    )
    eff_meta = (
        dict(meta)
        if meta is not None
        else get_token_meta(full_d, reload_store=reload_meta, now_ts=now)
    )
    taint_ts = float(eff_meta["taint_ts"]) if eff_meta.get("taint_ts") is not None else None
    last_attempt_ts = (
        float(eff_meta["last_attempt_ts"]) if eff_meta.get("last_attempt_ts") is not None else None
    )
    verified_rem_s = (
        float(eff_meta["verified_rem_s"]) if eff_meta.get("verified_rem_s") is not None else None
    )

    # Rule 2 (`pro_lapse`) checked first so a lapsed Pro token (>300 s) is never skipped as `>= 295 s` certain.
    is_pro_lapse = (base_s > 300.0 and now >= PRO_LAPSE_AT_UTC) or (rem_s > 300.0)

    # Fast-skip: verified >= 295 s with zero synthesis attempts (and zero taint) since truth stamp.
    has_attempt_since_truth = (
        truth_ts is not None and last_attempt_ts is not None and last_attempt_ts > truth_ts
    )
    has_taint_since_truth = (
        taint_ts is not None and (truth_ts is None or taint_ts > truth_ts)
    )
    eff_verified_rem = verified_rem_s if verified_rem_s is not None else rem_s
    if (
        not is_pro_lapse
        and truth_ts is not None
        and eff_verified_rem >= CERTAIN_VERIFIED_MIN_REM_S
        and rem_s >= CERTAIN_VERIFIED_MIN_REM_S
        and active_rg is None
        and not has_attempt_since_truth
        and not has_taint_since_truth
    ):
        return TokenUncertainty(
            slot=int(slot),
            token=tok_clean,
            token_digest=full_d,
            key_id=short_id,
            is_uncertain=False,
            is_certain_full=True,
            reason="certain",
            reasons=(),
            remaining_s=rem_s,
            base_s=base_s,
            updated_at_ts=upd_ts,
            last_truth_at=truth_ts,
            priority_key=(99, 0.0, int(slot)),
        )

    is_stale_drained = (
        (rem_s < UNCERTAIN_STALE_DRAINED_REM_S or (active_rg is not None and active_rg <= UNCERTAIN_STALE_DRAINED_CEIL_S))
        and (
            (upd_ts is not None and (now - upd_ts) >= UNCERTAIN_STALE_DRAINED_AGE_S)
            or (rst_ts is not None and rst_ts <= now)
        )
    )
    is_unverified_taint = has_taint_since_truth
    age_anchor_ts = truth_ts if truth_ts is not None else upd_ts
    is_aged_partial = (
        rem_s < (base_s - UNCERTAIN_AGED_PARTIAL_DELTA_S)
        and age_anchor_ts is not None
        and (now - age_anchor_ts) >= UNCERTAIN_AGED_PARTIAL_AGE_S
    )
    is_never_verified = (
        truth_ts is None
        and upd_ts is not None
        and (now - upd_ts) >= UNCERTAIN_NEVER_VERIFIED_AGE_S
    )

    matched: list[str] = []
    if is_pro_lapse:
        matched.append("pro_lapse")
    if is_stale_drained:
        matched.append("stale_drained")
    if is_unverified_taint:
        matched.append("unverified_taint")
    if is_aged_partial:
        matched.append("aged_partial")
    if is_never_verified:
        matched.append("never_verified")

    if not matched:
        return TokenUncertainty(
            slot=int(slot),
            token=tok_clean,
            token_digest=full_d,
            key_id=short_id,
            is_uncertain=False,
            is_certain_full=False,
            reason="",
            reasons=(),
            remaining_s=rem_s,
            base_s=base_s,
            updated_at_ts=upd_ts,
            last_truth_at=truth_ts,
            priority_key=(99, 0.0, int(slot)),
        )

    primary = matched[0]
    # Part 3 priority order:
    # (1) pro_lapse -> (2) stale_drained -> (3) remaining_s >= 137.0 tainted/aged descending
    # -> (4) 45.0 <= remaining_s < 137.0 tainted/aged ascending -> (5) never_verified oldest first.
    if is_pro_lapse:
        prio = (1, -rem_s, int(slot))
    elif is_stale_drained:
        prio = (2, float(upd_ts if upd_ts is not None else 0.0), int(slot))
    elif is_unverified_taint or is_aged_partial:
        if rem_s >= PRIORITY_HIGH_REM_THRESHOLD_S:
            prio = (3, -rem_s, int(slot))
        elif rem_s >= UNCERTAIN_STALE_DRAINED_REM_S:
            prio = (4, rem_s, int(slot))
        else:
            prio = (5, rem_s, int(slot))
    else:
        prio = (6, float(upd_ts if upd_ts is not None else 0.0), int(slot))

    return TokenUncertainty(
        slot=int(slot),
        token=tok_clean,
        token_digest=full_d,
        key_id=short_id,
        is_uncertain=True,
        is_certain_full=False,
        reason=primary,
        reasons=tuple(matched),
        remaining_s=rem_s,
        base_s=base_s,
        updated_at_ts=upd_ts,
        last_truth_at=truth_ts,
        priority_key=prio,
    )


def classify_pool_uncertainty(
    tokens: Sequence[str],
    *,
    state: Mapping[str, Any] | None = None,
    now_ts: float | None = None,
) -> tuple[list[TokenUncertainty], list[TokenUncertainty]]:
    """Classify all configured HF tokens, returning `(ordered_uncertain, all_classifications)`."""
    now = time.time() if now_ts is None else float(now_ts)
    st = load_pool_state(force=True, now_ts=now) if state is None else normalize_pool_state(state, now_ts=now)
    # Reload shared truth & meta maps once up front.
    if tokens:
        get_token_last_truth_ts(tokens[0], reload_store=True)
    meta_map = _load_token_meta_map(reload_store=True, now_ts=now)

    all_items: list[TokenUncertainty] = []
    for idx, tok in enumerate(tokens):
        if not tok or not str(tok).strip():
            continue
        d_key = token_digest(tok)
        item = classify_token_uncertainty(
            tok,
            slot=idx,
            state=st,
            meta=meta_map.get(d_key) or {},
            last_truth_at=_mem_truth_ts.get(d_key),
            reload_meta=False,
            now_ts=now,
        )
        all_items.append(item)

    uncertain_ordered = sorted(
        (it for it in all_items if it.is_uncertain),
        key=lambda it: it.priority_key,
    )
    return uncertain_ordered, all_items


def _sync_engine_token_balance(token: str, remaining_s: float, *, clear_cooldown: bool = False) -> None:
    try:
        from app.services.voxcpm_engine import VoxCPMEngine

        with VoxCPMEngine._lock:
            VoxCPMEngine._token_balances[str(token)] = float(remaining_s)
            if clear_cooldown:
                VoxCPMEngine._token_cooldowns.pop(str(token), None)
    except Exception:
        pass


def apply_calibration_numeric_refusal(
    token: str,
    left_s: float,
    *,
    error_text: str | None = None,
    now_ts: float | None = None,
) -> tuple[float, float, dict[str, Any]]:
    """Round 2 Part 3: apply a `/probe_quota` numeric refusal (`297s requested vs. Ys left`).
    Never sets `refused_gate_s = 297`; clears any stale ceiling `existing_rg <= Y`; stamps truth."""
    now = time.time() if now_ts is None else float(now_ts)
    tok_clean = str(token).strip()
    clean_left = round(max(0.0, float(left_s)), 2)
    box: dict[str, float] = {"before": DEFAULT_TOKEN_BASE_S}

    def _mutate(st: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        d_key, existing = lookup_token_entry(st, tok_clean)
        before_s = (
            float(existing["remaining_s"])
            if (existing is not None and existing.get("remaining_s") is not None)
            else DEFAULT_TOKEN_BASE_S
        )
        box["before"] = round(before_s, 2)
        existing_base_s = (
            float(existing.get("base_s") or DEFAULT_TOKEN_BASE_S)
            if existing
            else DEFAULT_TOKEN_BASE_S
        )
        if error_text and "free zerogpu" in str(error_text).lower() and existing_base_s > 300.0:
            eff_base_s = round(max(300.0, clean_left), 2)
        else:
            eff_base_s = round(max(existing_base_s, clean_left), 2)

        wait_s = parse_refusal_retry_wait_s(error_text)
        existing_rst = str((existing or {}).get("resets_at") or "")
        existing_rst_ts = _parse_ts(existing_rst)
        if wait_s is not None and wait_s > 0:
            resets_at = _iso_utc(now + wait_s)
        elif existing_rst_ts is not None and existing_rst_ts > now:
            resets_at = existing_rst
        else:
            resets_at = _iso_utc(now + 86400.0)

        cd_until = "" if clean_left > 0.0 else str((existing or {}).get("cooldown_until") or "")
        row_out: dict[str, Any] = {
            "status": "ok",
            "remaining_s": clean_left,
            "base_s": eff_base_s,
            "resets_at": resets_at,
            "cooldown_until": cd_until,
            "updated_at": _iso_utc(now),
        }
        existing_rg, existing_ru = _extract_active_ceiling(existing, now)
        if existing_rg is not None and existing_ru:
            if existing_rg <= clean_left:
                _refusal_rem_baseline.pop(d_key, None)
            else:
                row_out["refused_gate_s"] = existing_rg
                row_out["refused_until"] = existing_ru
                _refusal_rem_baseline[d_key] = clean_left
        else:
            _refusal_rem_baseline.pop(d_key, None)

        st["tokens"][d_key] = row_out
        return st, True

    record_token_truth_timestamp(tok_clean, now, verified_rem_s=clean_left)
    saved = _mutate_pool_state(_mutate, now_ts=now)
    pos_drift = round(box["before"] - clean_left, 2)
    if pos_drift > 0.0:
        _record_runtime_drift_observation(token_digest(tok_clean), pos_drift, now)
    _sync_engine_token_balance(tok_clean, clean_left, clear_cooldown=(clean_left > 0.0))
    return box["before"], clean_left, saved


def apply_calibration_probe_success(
    token: str,
    gpu_s: float,
    *,
    now_ts: float | None = None,
) -> tuple[float, float, float, dict[str, Any]]:
    """Round 2 Part 3: apply `/probe_quota` success (`debit_s = clamp(gpu_s + 1.0, 1.0, 5.0)`),
    clearing ceiling + `_refusal_rem_baseline` + `cooldown_until`, and stamping truth."""
    now = time.time() if now_ts is None else float(now_ts)
    tok_clean = str(token).strip()
    debit_s = round(min(5.0, max(1.0, float(gpu_s) + 1.0)), 2)
    box: dict[str, float] = {"before": DEFAULT_TOKEN_BASE_S, "after": 0.0}

    def _mutate(st: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        d_key, existing = lookup_token_entry(st, tok_clean)
        before_s = (
            float(existing["remaining_s"])
            if (existing is not None and existing.get("remaining_s") is not None)
            else DEFAULT_TOKEN_BASE_S
        )
        existing_base_s = (
            float(existing.get("base_s") or DEFAULT_TOKEN_BASE_S)
            if existing
            else DEFAULT_TOKEN_BASE_S
        )
        eff_base_s = (
            DEFAULT_TOKEN_BASE_S
            if (existing_base_s > 300.0 and now >= PRO_LAPSE_AT_UTC)
            else existing_base_s
        )
        after_s = round(
            max(0.0, min(eff_base_s - debit_s, max(before_s - debit_s, 297.0 - debit_s))),
            2,
        )
        box["before"] = round(before_s, 2)
        box["after"] = after_s
        _refusal_rem_baseline.pop(d_key, None)
        st["tokens"][d_key] = {
            "status": "ok",
            "remaining_s": after_s,
            "base_s": round(max(eff_base_s, after_s), 2),
            "resets_at": str((existing or {}).get("resets_at") or ""),
            "cooldown_until": "",
            "updated_at": _iso_utc(now),
        }
        return st, True

    best_ts = record_token_truth_timestamp(tok_clean, now) or round(now, 6)
    saved = _mutate_pool_state(_mutate, now_ts=now)
    _persist_token_meta_update(
        _canonical_digest(tok_clean),
        {"verified_ts": best_ts, "verified_rem_s": round(max(0.0, float(box["after"])), 2)},
        clear_taint_up_to=best_ts,
        now_ts=best_ts,
    )
    pos_drift = round((box["before"] - box["after"]) - debit_s, 2)
    if pos_drift > 0.0:
        _record_runtime_drift_observation(token_digest(tok_clean), pos_drift, now)
    _sync_engine_token_balance(tok_clean, box["after"], clear_cooldown=True)
    return box["before"], box["after"], debit_s, saved


def apply_calibration_generic_capped(
    token: str,
    gate_s: float = 45.0,
    *,
    ttl_seconds: float = 3600.0,
    now_ts: float | None = None,
) -> tuple[float, float, dict[str, Any]]:
    """Round 2 Part 3: apply generic refusal at 1-char `/generate_session` door (`G = 45`):
    `remaining_s = min(existing_rem, G - 1.0)` + `record_refusal_ceiling(token, G, ttl_seconds=3600)`."""
    now = time.time() if now_ts is None else float(now_ts)
    tok_clean = str(token).strip()
    g_val = round(max(1.0, float(gate_s)), 2)
    cap_rem = round(max(0.0, g_val - 1.0), 2)
    box: dict[str, float] = {"before": DEFAULT_TOKEN_BASE_S, "after": cap_rem}

    def _mutate(st: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        d_key, existing = lookup_token_entry(st, tok_clean)
        before_s = (
            float(existing["remaining_s"])
            if (existing is not None and existing.get("remaining_s") is not None)
            else DEFAULT_TOKEN_BASE_S
        )
        after_s = round(max(0.0, min(before_s, cap_rem)), 2)
        base_s = (
            float(existing.get("base_s") or max(DEFAULT_TOKEN_BASE_S, before_s))
            if existing
            else DEFAULT_TOKEN_BASE_S
        )
        box["before"] = round(before_s, 2)
        box["after"] = after_s
        row_out: dict[str, Any] = {
            "status": "ok",
            "remaining_s": after_s,
            "base_s": round(max(0.0, base_s), 2),
            "resets_at": str((existing or {}).get("resets_at") or _iso_utc(now + 86400.0)),
            "cooldown_until": str((existing or {}).get("cooldown_until") or ""),
            "updated_at": _iso_utc(now),
        }
        _copy_active_ceiling(existing, row_out, now)
        st["tokens"][d_key] = row_out
        return st, True

    _mutate_pool_state(_mutate, now_ts=now)
    saved = record_refusal_ceiling(
        tok_clean,
        g_val,
        ttl_seconds=ttl_seconds,
        mark_generic_taint=False,
        now_ts=now,
    ) or load_pool_state(now_ts=now)
    pos_drift = round(box["before"] - box["after"], 2)
    if pos_drift > 0.0:
        _record_runtime_drift_observation(token_digest(tok_clean), pos_drift, now)
    _sync_engine_token_balance(tok_clean, box["after"])
    return box["before"], box["after"], saved


def apply_calibration_followup_admitted(
    token: str,
    debit_s: float,
    *,
    floor_s: float = 45.0,
    now_ts: float | None = None,
) -> tuple[float, float, dict[str, Any]]:
    """Round 2 Part 3: apply admitted 1-char `/generate_session` follow-up:
    stamp truth with `remaining_s` reduced by `debit_s` from a `>= 45 s` floor."""
    now = time.time() if now_ts is None else float(now_ts)
    tok_clean = str(token).strip()
    clean_debit = round(max(1.0, float(debit_s)), 2)
    box: dict[str, float] = {"before": DEFAULT_TOKEN_BASE_S, "after": 0.0}

    def _mutate(st: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        d_key, existing = lookup_token_entry(st, tok_clean)
        before_s = (
            float(existing["remaining_s"])
            if (existing is not None and existing.get("remaining_s") is not None)
            else DEFAULT_TOKEN_BASE_S
        )
        base_s = (
            float(existing.get("base_s") or DEFAULT_TOKEN_BASE_S)
            if existing
            else DEFAULT_TOKEN_BASE_S
        )
        eff_base_s = (
            DEFAULT_TOKEN_BASE_S
            if (base_s > 300.0 and now >= PRO_LAPSE_AT_UTC)
            else base_s
        )
        pre_floor_s = max(before_s, float(floor_s))
        after_s = round(max(0.0, min(eff_base_s - clean_debit, pre_floor_s - clean_debit)), 2)
        box["before"] = round(before_s, 2)
        box["after"] = after_s
        row_out: dict[str, Any] = {
            "status": "ok",
            "remaining_s": after_s,
            "base_s": round(max(eff_base_s, after_s), 2),
            "resets_at": str((existing or {}).get("resets_at") or ""),
            "cooldown_until": "",
            "updated_at": _iso_utc(now),
        }
        existing_rg, existing_ru = _extract_active_ceiling(existing, now)
        if existing_rg is not None and existing_ru:
            if existing_rg <= max(float(floor_s), after_s):
                _refusal_rem_baseline.pop(d_key, None)
            else:
                row_out["refused_gate_s"] = existing_rg
                row_out["refused_until"] = existing_ru
                _refusal_rem_baseline[d_key] = after_s
        else:
            _refusal_rem_baseline.pop(d_key, None)
        st["tokens"][d_key] = row_out
        return st, True

    best_ts = record_token_truth_timestamp(tok_clean, now) or round(now, 6)
    saved = _mutate_pool_state(_mutate, now_ts=now)
    _persist_token_meta_update(
        _canonical_digest(tok_clean),
        {"verified_ts": best_ts, "verified_rem_s": round(max(0.0, float(box["after"])), 2)},
        clear_taint_up_to=best_ts,
        now_ts=best_ts,
    )
    pos_drift = round((box["before"] - box["after"]) - clean_debit, 2)
    if pos_drift > 0.0:
        _record_runtime_drift_observation(token_digest(tok_clean), pos_drift, now)
    _sync_engine_token_balance(tok_clean, box["after"], clear_cooldown=True)
    return box["before"], box["after"], saved


def record_calibration_drift_stats(
    positive_drift_s: float,
    overestimated_tokens: int,
    max_token_drift_s: float,
    *,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Update `drift_stats` balance fields in `aj_hf:pool_state` from a calibration sweep
    while keeping `drift_stats`'s exact 4-key schema and preserving `quota_probe_errors`."""
    now = time.time() if now_ts is None else float(now_ts)
    rw_pos, rw_over, rw_max = _summarize_runtime_drift_window(now)

    def _mutate(st: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        prev = st.get("drift_stats") if isinstance(st.get("drift_stats"), Mapping) else {}
        st["drift_stats"] = {
            "positive_drift_s": round(max(0.0, float(positive_drift_s), rw_pos), 2),
            "overestimated_tokens": int(max(0, int(overestimated_tokens), rw_over)),
            "max_token_drift_s": round(max(0.0, float(max_token_drift_s), rw_max), 2),
            "quota_probe_errors": int(prev.get("quota_probe_errors") or 0),
        }
        return st, True

    return _mutate_pool_state(_mutate, now_ts=now)


__all__ = [
    "AUTH_DEAD_QUARANTINE_SECONDS",
    "AdmissionDecision",
    "BAND_CONSERVATION",
    "BAND_DRY",
    "BAND_NORMAL",
    "BAND_SHORT_JOB",
    "CACHE_TTL_SECONDS",
    "CONGESTION_ETA_MAX_HORIZON_SECONDS",
    "CONGESTION_ETA_MIN_HORIZON_SECONDS",
    "CONGESTION_RESUMER_INTERVAL_SECONDS",
    "DEFAULT_SPACE_GATE_S",
    "DEFAULT_TOKEN_BASE_S",
    "GPU_REPORTED_OVERHEAD_PAD_S",
    "LEGACY_DECLARED_MAX_GPU_S",
    "LEGACY_DECLARED_MIN_GPU_S",
    "MIN_REPORTED_GPU_DEBIT_S",
    "MIN_USABLE_DUST_GATE_S",
    "POOL_STATE_STORE_KEY",
    "PRE_JOB_SAFETY_MULTIPLIER",
    "PoolMetrics",
    "REFUSAL_CEILING_TTL_SECONDS",
    "REFUSAL_REFILL_CLEAR_DELTA_S",
    "RESUMER_SAFETY_MULTIPLIER",
    "SESSION_ADMISSION_GATE_MULTIPLIER",
    "SESSION_ADMISSION_MAX_GPU_S",
    "SESSION_CALL_BASE_GPU_S",
    "SESSION_CALL_MAX_GPU_S",
    "SESSION_CALL_PER_CHAR_GPU_S",
    "SESSION_DECLARED_MIN_GPU_S",
    "SESSION_READY_MIN_GPU_S",
    "SPACE_TIMEOUT_COOLDOWN_SECONDS",
    "TOKEN_META_STORE_KEY",
    "TRUTH_TS_STORE_KEY",
    "TokenUncertainty",
    "acquire_in_flight_gpu",
    "apply_calibration_followup_admitted",
    "apply_calibration_generic_capped",
    "apply_calibration_numeric_refusal",
    "apply_calibration_probe_success",
    "classify_pool_uncertainty",
    "classify_token_uncertainty",
    "compute_env_tokens_hash",
    "compute_method1_debit_s",
    "compute_pool_metrics",
    "congestion_resume_eta_label",
    "effective_session_admission_gate_s",
    "estimate_refill_eta_label",
    "estimate_voxcpm_units_and_budget",
    "evaluate_voxcpm_admission",
    "get_drift_stats",
    "get_session_space_floor",
    "get_space_cooldown_until",
    "get_space_gate",
    "get_space_timeout_counts",
    "get_token_last_truth_ts",
    "get_token_meta",
    "get_token_refused_gate_s",
    "get_token_refused_until",
    "get_token_remaining_s",
    "handle_auth_failure",
    "has_any_non_cooling_space",
    "has_resumer_capacity",
    "has_solvent_token_at_gate",
    "is_space_cooling",
    "is_token_auth_dead",
    "is_token_cooling",
    "is_token_refusal_capped",
    "legacy_declared_door_s",
    "legacy_declared_s",
    "load_pool_state",
    "lookup_token_entry",
    "normalize_pool_state",
    "parse_refusal_retry_wait_s",
    "probe_whoami_status",
    "reconcile_from_probe_rows",
    "reconcile_if_env_changed",
    "reconcile_in_flight_gpu",
    "record_admission_refusal",
    "record_calibration_drift_stats",
    "record_failure_debit",
    "record_quota_dead",
    "record_refusal_ceiling",
    "record_session_space_floor",
    "record_space_timeout",
    "record_success_debit",
    "record_token_synthesis_attempt",
    "record_token_taint",
    "record_token_truth_timestamp",
    "release_in_flight_gpu",
    "reset_ledger_for_tests",
    "save_pool_state",
    "session_admission_gate_s",
    "session_call_gate_s",
    "session_declared_s",
    "token_digest",
    "watermark_band_for_fraction",
]

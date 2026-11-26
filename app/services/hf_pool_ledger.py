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
import threading
import time
from typing import Any, Callable, Mapping, Sequence
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

from app.services.key_rotation import (
    botsetting_sync_delete,
    botsetting_sync_read,
    botsetting_sync_write,
    key_id as _short_key_id,
)

logger = logging.getLogger(__name__)

POOL_STATE_STORE_KEY = "aj_hf:pool_state"
IN_FLIGHT_STORE_KEY = "aj_hf:in_flight"
ENV_HASH_STORE_KEY = "aj_hf:env_hash"

CACHE_TTL_SECONDS = 30.0
AUTH_DEAD_QUARANTINE_SECONDS = 86400.0  # 24 hours
QUOTA_DEAD_COOLDOWN_SECONDS = 1800.0    # 30 minutes
SPACE_TIMEOUT_COOLDOWN_SECONDS = 600.0  # 10 minutes per-Space timeout circuit breaker (Part C1)
IN_FLIGHT_STALE_SECONDS = 7200.0        # 2 hours safety GC
MIN_USABLE_DUST_GATE_S = 25.0           # <25 s "quota dust" never counts as capacity
DEFAULT_SPACE_GATE_S = 60.0             # fallback only when space_gates has no entry yet
DEFAULT_TOKEN_BASE_S = 300.0            # ~5 min free-account base when unprobed
PRE_JOB_SAFETY_MULTIPLIER = 1.20
RESUMER_SAFETY_MULTIPLIER = 1.25

# Session-slice `/generate_session` dynamic GPU gate pricing (2026-11-26):
# `min(120, 18 + ceil(chars * 0.085))` GPU-seconds.
SESSION_CALL_BASE_GPU_S = 18
SESSION_CALL_PER_CHAR_GPU_S = 0.085
SESSION_CALL_MAX_GPU_S = 120


def session_call_gate_s(chars: int) -> float:
    """Return the GPU-second gate for a `/generate_session` call of `chars` characters:
    `min(120, 18 + ceil(chars * 0.085))`."""
    n = max(0, int(chars or 0))
    added = int(math.ceil(round(n * SESSION_CALL_PER_CHAR_GPU_S, 9)))
    return float(min(SESSION_CALL_MAX_GPU_S, SESSION_CALL_BASE_GPU_S + added))

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

    # Auto-refill rolling window once resets_at has passed.
    rst_ts = _parse_ts(resets_str)
    if status == "ok" and rst_ts is not None and rst_ts <= now and base > 0 and rem < base:
        rem = base
        resets_str = ""

    updated_raw = raw.get("updated_at")
    updated_str = str(updated_raw) if updated_raw not in (None, "") else _iso_utc(now)
    return {
        "status": status,
        "remaining_s": round(max(0.0, rem), 2),
        "base_s": round(max(0.0, base), 2),
        "resets_at": resets_str,
        "cooldown_until": cooldown_str,
        "updated_at": updated_str,
    }


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

    out: dict[str, Any] = {
        "tokens": tokens_out,
        "space_gates": gates_out,
        "in_flight_gpu_s": in_flight_val,
    }
    if space_cds_out:
        out["space_cooldowns"] = space_cds_out
    if space_tos_out:
        out["space_timeouts"] = space_tos_out
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


def _write_store_state(state: dict[str, Any]) -> bool:
    clean = normalize_pool_state(state)
    try:
        payload = json.dumps(clean, separators=(",", ":"))
    except Exception:
        return False
    return botsetting_sync_write(POOL_STATE_STORE_KEY, payload)


def _load_in_flight_map(now_ts: float) -> dict[str, dict[str, float]]:
    global _mem_in_flight_jobs
    raw = botsetting_sync_read(IN_FLIGHT_STORE_KEY)
    loaded: dict[str, dict[str, float]] = {}
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, Mapping):
                for jid, meta in data.items():
                    if not isinstance(meta, Mapping):
                        continue
                    gpu_s = max(0.0, float(meta.get("gpu_s") or 0.0))
                    ts = float(meta.get("ts") or now_ts)
                    if gpu_s > 0 and (now_ts - ts) < IN_FLIGHT_STALE_SECONDS:
                        loaded[str(jid)] = {"gpu_s": round(gpu_s, 2), "ts": ts}
        except Exception:
            loaded = {}
    else:
        loaded = {
            jid: dict(meta)
            for jid, meta in _mem_in_flight_jobs.items()
            if (now_ts - float(meta.get("ts") or now_ts)) < IN_FLIGHT_STALE_SECONDS
        }
    _mem_in_flight_jobs = loaded
    return loaded


def _save_in_flight_map(jobs_map: dict[str, dict[str, float]]) -> None:
    global _mem_in_flight_jobs
    _mem_in_flight_jobs = dict(jobs_map)
    try:
        botsetting_sync_write(IN_FLIGHT_STORE_KEY, json.dumps(jobs_map, separators=(",", ":")))
    except Exception:
        pass


def load_pool_state(*, force: bool = False, now_ts: float | None = None) -> dict[str, Any]:
    """Return the current pool state, reloading from `bot_settings` on 30 s TTL or `force`."""
    global _mem_state, _mem_loaded_mono
    now = time.time() if now_ts is None else float(now_ts)
    mono = time.monotonic()
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
        else:
            _mem_state = normalize_pool_state(_mem_state, now_ts=now)
        return json.loads(json.dumps(_mem_state))


def save_pool_state(state: Mapping[str, Any], *, now_ts: float | None = None) -> dict[str, Any]:
    """Normalize, cache in memory, and write through to `aj_hf:pool_state`."""
    global _mem_state, _mem_loaded_mono
    now = time.time() if now_ts is None else float(now_ts)
    with _lock:
        clean = normalize_pool_state(state, now_ts=now)
        _mem_state = clean
        _mem_loaded_mono = time.monotonic()
        _write_store_state(clean)
        return json.loads(json.dumps(clean))


def reset_ledger_for_tests(*, clear_store: bool = False) -> None:
    """Reset in-memory cache (and optionally persisted KV keys) between unit tests."""
    global _mem_state, _mem_loaded_mono, _mem_in_flight_jobs, _last_seen_env_hash, _known_token_digests
    with _lock:
        _mem_state = None
        _mem_loaded_mono = 0.0
        _mem_in_flight_jobs = {}
        _last_seen_env_hash = None
        _known_token_digests = set()
        if clear_store:
            botsetting_sync_delete(POOL_STATE_STORE_KEY)
            botsetting_sync_delete(IN_FLIGHT_STORE_KEY)
            botsetting_sync_delete(ENV_HASH_STORE_KEY)


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


def record_admission_refusal(
    space_name: str,
    token: str | None,
    requested_s: float,
    left_s: float,
    *,
    update_space_gate: bool = True,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Update `space_gates[space_name]` (when `update_space_gate` is True) and `token`'s `remaining_s` from a refusal."""
    now = time.time() if now_ts is None else float(now_ts)
    with _lock:
        st = load_pool_state(now_ts=now)
        if update_space_gate and space_name and requested_s > 0:
            st["space_gates"][str(space_name)] = (
                int(requested_s) if float(requested_s).is_integer() else round(float(requested_s), 2)
            )
        if token and str(token).strip():
            d_key, existing = lookup_token_entry(st, token)
            base_s = float(existing.get("base_s") or DEFAULT_TOKEN_BASE_S) if existing else DEFAULT_TOKEN_BASE_S
            st["tokens"][d_key] = {
                "status": "ok",
                "remaining_s": round(max(0.0, float(left_s)), 2),
                "base_s": round(max(base_s, float(left_s)), 2),
                "resets_at": (existing or {}).get("resets_at") or _iso_utc(now + 86400.0),
                "cooldown_until": (existing or {}).get("cooldown_until") or "",
                "updated_at": _iso_utc(now),
            }
        return save_pool_state(st, now_ts=now)


def record_success_debit(
    space_name: str,
    token: str | None,
    *,
    debit_s: float | None = None,
    now_ts: float | None = None,
) -> dict[str, Any] | None:
    """Debit `space_name`'s gate from `token`'s `remaining_s` after a successful synthesis."""
    if not token or not str(token).strip():
        return None
    now = time.time() if now_ts is None else float(now_ts)
    with _lock:
        st = load_pool_state(now_ts=now)
        d_key, existing = lookup_token_entry(st, token)
        gate = float(debit_s) if (debit_s is not None and debit_s > 0) else get_space_gate(space_name, state=st, default=0.0)
        if existing is None and gate <= 0:
            return st
        if existing is None:
            base_s = max(DEFAULT_TOKEN_BASE_S, gate)
            new_rem = max(0.0, base_s - gate)
            resets_at = _iso_utc(now + 86400.0)
        else:
            base_s = float(existing.get("base_s") or DEFAULT_TOKEN_BASE_S)
            old_rem = float(existing.get("remaining_s") or 0.0)
            new_rem = max(0.0, old_rem - gate) if gate > 0 else old_rem
            resets_at = existing.get("resets_at") or _iso_utc(now + 86400.0)
        st["tokens"][d_key] = {
            "status": "ok",
            "remaining_s": round(new_rem, 2),
            "base_s": round(base_s, 2),
            "resets_at": resets_at,
            "cooldown_until": "",
            "updated_at": _iso_utc(now),
        }
        return save_pool_state(st, now_ts=now)


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
    with _lock:
        st = load_pool_state(now_ts=now)
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
        return save_pool_state(st, now_ts=now)


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
    with _lock:
        st = load_pool_state(now_ts=now)
        cds = dict(st.get("space_cooldowns") or {})
        if float(cooldown_seconds) > 0:
            cds[sp] = _iso_utc(now + float(cooldown_seconds))
        st["space_cooldowns"] = cds
        tos = dict(st.get("space_timeouts") or {})
        tos[sp] = int(tos.get(sp) or 0) + 1
        st["space_timeouts"] = tos
        return save_pool_state(st, now_ts=now)


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
    with _lock:
        st = load_pool_state(now_ts=now)
        d_key, existing = lookup_token_entry(st, token)
        base_s = float((existing or {}).get("base_s") or DEFAULT_TOKEN_BASE_S)
        resets_at = (existing or {}).get("resets_at") or ""
        if status_code in (401, 403):
            st["tokens"][d_key] = {
                "status": "auth_dead",
                "remaining_s": 0.0,
                "base_s": round(base_s, 2),
                "resets_at": resets_at,
                "cooldown_until": _iso_utc(now + AUTH_DEAD_QUARANTINE_SECONDS),
                "updated_at": _iso_utc(now),
            }
            save_pool_state(st, now_ts=now)
            return True
        if status_code == 200:
            rem_s = float((existing or {}).get("remaining_s") or base_s)
            st["tokens"][d_key] = {
                "status": "ok",
                "remaining_s": round(rem_s, 2),
                "base_s": round(base_s, 2),
                "resets_at": resets_at,
                "cooldown_until": "",
                "updated_at": _iso_utc(now),
            }
            save_pool_state(st, now_ts=now)
            return False
        # Transport error on confirmation probe: conservative 30m cooldown, status stays ok.
        rem_s = float((existing or {}).get("remaining_s") or 0.0)
        st["tokens"][d_key] = {
            "status": "ok",
            "remaining_s": round(rem_s, 2),
            "base_s": round(base_s, 2),
            "resets_at": resets_at,
            "cooldown_until": _iso_utc(now + QUOTA_DEAD_COOLDOWN_SECONDS),
            "updated_at": _iso_utc(now),
        }
        save_pool_state(st, now_ts=now)
        return False


def reconcile_from_probe_rows(
    tokens: Sequence[str],
    rows: Sequence[Mapping[str, Any]],
    *,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Reconcile exact `remaining_s` / `base_s` / `resets_at` / `auth_dead` from probes."""
    now = time.time() if now_ts is None else float(now_ts)
    with _lock:
        st = load_pool_state(force=True, now_ts=now)
        active_digests: set[str] = set()
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
            if probe_status == "dead":
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
                rem_s = float(rem_val) if rem_val is not None else float((existing or {}).get("remaining_s") or DEFAULT_TOKEN_BASE_S)
                base_s = float(base_val) if base_val is not None else float((existing or {}).get("base_s") or max(DEFAULT_TOKEN_BASE_S, rem_s))
                resets_at = str(row.get("quota_resets_at") or (existing or {}).get("resets_at") or "")
                cd_until = ""
                if rem_s <= 0:
                    cd_until = (existing or {}).get("cooldown_until") or _iso_utc(now + QUOTA_DEAD_COOLDOWN_SECONDS)
                st["tokens"][full_d] = {
                    "status": "ok",  # probe 200 auto-clears any prior auth_dead
                    "remaining_s": round(max(0.0, rem_s), 2),
                    "base_s": round(max(0.0, base_s), 2),
                    "resets_at": resets_at,
                    "cooldown_until": cd_until,
                    "updated_at": _iso_utc(now),
                }
            else:
                if existing is not None:
                    st["tokens"][full_d] = existing
        # Prune digests that are no longer in the configured env pool so removed
        # dead tokens auto-clear immediately when the env set changes.
        if active_digests:
            st["tokens"] = {
                k: v for k, v in st["tokens"].items() if k in active_digests
            }
        return save_pool_state(st, now_ts=now)


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
    if not job_id or gpu_s <= 0:
        st = load_pool_state(now_ts=now_ts)
        return float(st.get("in_flight_gpu_s") or 0.0)
    now = time.time() if now_ts is None else float(now_ts)
    with _lock:
        st = load_pool_state(force=True, now_ts=now)
        jobs_map = _load_in_flight_map(now)
        prev = float((jobs_map.get(str(job_id)) or {}).get("gpu_s") or 0.0)
        target = round(max(0.0, float(gpu_s)), 2)
        jobs_map[str(job_id)] = {"gpu_s": target, "ts": now}
        _save_in_flight_map(jobs_map)
        cur_total = max(0.0, float(st.get("in_flight_gpu_s") or 0.0) - prev + target)
        tracked_sum = sum(float(m.get("gpu_s") or 0.0) for m in jobs_map.values())
        st["in_flight_gpu_s"] = round(max(cur_total, tracked_sum), 2)
        saved = save_pool_state(st, now_ts=now)
        return float(saved["in_flight_gpu_s"])


def release_in_flight_gpu(job_id: str, *, now_ts: float | None = None) -> float:
    """Release `job_id`'s reservation from `in_flight_gpu_s` idempotently."""
    if not job_id:
        st = load_pool_state(now_ts=now_ts)
        return float(st.get("in_flight_gpu_s") or 0.0)
    now = time.time() if now_ts is None else float(now_ts)
    with _lock:
        st = load_pool_state(force=True, now_ts=now)
        jobs_map = _load_in_flight_map(now)
        popped = jobs_map.pop(str(job_id), None)
        _save_in_flight_map(jobs_map)
        if popped is not None:
            delta = float(popped.get("gpu_s") or 0.0)
            new_total = max(0.0, float(st.get("in_flight_gpu_s") or 0.0) - delta)
            st["in_flight_gpu_s"] = round(new_total, 2)
            saved = save_pool_state(st, now_ts=now)
            return float(saved["in_flight_gpu_s"])
        return float(st.get("in_flight_gpu_s") or 0.0)


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
        target_ts = now + 3600.0

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone(timedelta(hours=6, minutes=30))
    dt = datetime.fromtimestamp(target_ts, tz=tz)
    return dt.strftime("%H:%M")


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


__all__ = [
    "AUTH_DEAD_QUARANTINE_SECONDS",
    "AdmissionDecision",
    "BAND_CONSERVATION",
    "BAND_DRY",
    "BAND_NORMAL",
    "BAND_SHORT_JOB",
    "CACHE_TTL_SECONDS",
    "DEFAULT_SPACE_GATE_S",
    "DEFAULT_TOKEN_BASE_S",
    "MIN_USABLE_DUST_GATE_S",
    "POOL_STATE_STORE_KEY",
    "PRE_JOB_SAFETY_MULTIPLIER",
    " PoolMetrics" if False else "PoolMetrics",
    "RESUMER_SAFETY_MULTIPLIER",
    "SESSION_CALL_BASE_GPU_S",
    "SESSION_CALL_MAX_GPU_S",
    "SESSION_CALL_PER_CHAR_GPU_S",
    "SPACE_TIMEOUT_COOLDOWN_SECONDS",
    "acquire_in_flight_gpu",
    "compute_env_tokens_hash",
    "compute_pool_metrics",
    "estimate_refill_eta_label",
    "estimate_voxcpm_units_and_budget",
    "evaluate_voxcpm_admission",
    "get_space_cooldown_until",
    "get_space_gate",
    "get_space_timeout_counts",
    " get_token_remaining_s".strip(),
    "handle_auth_failure",
    "has_resumer_capacity",
    "is_space_cooling",
    "is_token_auth_dead",
    "is_token_cooling",
    "load_pool_state",
    "lookup_token_entry",
    "normalize_pool_state",
    "probe_whoami_status",
    "reconcile_from_probe_rows",
    "reconcile_if_env_changed",
    "record_admission_refusal",
    "record_quota_dead",
    "record_space_timeout",
    "record_success_debit",
    "release_in_flight_gpu",
    "reset_ledger_for_tests",
    "save_pool_state",
    "session_call_gate_s",
    "token_digest",
    "watermark_band_for_fraction",
]

"""Shared API-key rotation health: cooldown memory + rotating start offset.

Audit fix E (2026-11-03). Before this module each provider integration
rolled its own key handling:

* Groq transcription (two implementations) always iterated keys from
  #1, so a rate-limited first key cost a 429 round-trip on EVERY call.
* The Gemini key pool was rebuilt from scratch per job with no memory
  of quota-dead keys, and three different pool constructions existed
  (planner / Gemini TTS / group admin), so a key configured for one
  feature did not necessarily serve the others.
* Only the VoxCPM/HF token pool had health memory (round-robin +
  per-token cooldowns); it keeps its own engine-local state, this
  module is the equivalent for the other providers.

Semantics, mirroring the VoxCPM token pool:

* In-memory first — cooldowns and cursors live in process memory for
  the fast path. Batch AJ (M12): registry instances (``health_for``)
  additionally persist a snapshot — key DIGESTS + expiry timestamps +
  cursor only, never raw key material — to the BotSetting KV store
  (key ``aj_kh:<provider>``) with a debounced write-through, and lazy-
  load it on first use, so a restart resumes where the last process
  stopped instead of retrying dead keys. Direct ``KeyHealth()``
  instances stay memory-only unless ``enable_persistence`` is called.
  All persistence is best-effort: any store failure degrades to the
  old memory-only behaviour and never raises into a caller.
* Thread-safe: one lock guards cooldowns and the rotation cursor.
* Keys are identified by a short SHA-256 digest (``key_id``); raw key
  material is never stored in this module's state and never logged.
* Batch DN (2026-11-10): quota-aware ordering. Google exposes no
  remaining-quota API and a 429 carries no remaining count, so
  KeyHealth keeps a per-(model, key-digest) ledger of OUR OWN
  successful calls, counted against the measured per-model daily caps
  (``MODEL_DAILY_CAPS``) on Google's Pacific quota day.
  ``order_for_model`` starts a call from the key with the most
  remaining quota for that model; rotation survives inside ties so
  the richest keys share the per-minute RPM cap instead of one key
  absorbing every call.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from datetime import datetime
from typing import Callable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Same 30 minutes the VoxCPM token pool uses for quota-class failures.
DEFAULT_COOLDOWN_SECONDS = 1800.0
# Part 2 (§2C Item 1): per-(model, key) cooldowns use short initial backoff
# (25-65 s for RPM / transient 429s) instead of immediately cooling the key
# across ALL models for 30 minutes.
MODEL_INITIAL_COOLDOWN_MIN = 25.0
MODEL_INITIAL_COOLDOWN_MAX = 65.0
MODEL_COOLDOWN_BACKOFF_STEPS = (120.0, 300.0, 900.0, DEFAULT_COOLDOWN_SECONDS)

# Batch DN: per-model DAILY request caps (requests per day per key /
# project), measured firsthand from the AI Studio rate-limit table
# (Free tier, 2026-11-10). Only models the shared pool serves are
# listed; a model missing here carries DEFAULT_MODEL_DAILY_CAP.
MODEL_DAILY_CAPS: dict[str, int] = {
    # Gemini TTS models — 3 RPM / 10K TPM / 10 RPD each.
    "gemini-3.8-flash-tts": 10,
    "gemini-3.1-flash-tts-preview": 10,
    "gemini-3.8-flash-lite-tts": 10,
    "gemini-2.5-flash-preview-tts": 10,
    # Full text models — 5 RPM / 250K TPM / 20 RPD.
    "gemini-3.6-flash": 20,
    "gemini-3.5-flash": 20,
    "gemini-3.8-flash": 20,
    "gemini-2.5-flash": 20,
    "gemini-2.5-flash-lite": 20,
    "gemini-flash-latest": 20,
    "gemini-pro-latest": 20,
    # Lite text models — 15 RPM / 250K TPM / 500 RPD.
    "gemini-3.5-flash-lite": 500,
    "gemini-3.1-flash-lite": 500,
    "gemini-flash-lite-latest": 500,
    # Groq text models (Round 15 Part E — Nov 2026 free tier: 1,000 RPD / 12K TPM / 100K TPD per account).
    "llama-3.3-70b-versatile": 1000,
    "qwen/qwen3.8-27b": 1000,
}

# Conservative daily cap assumed for a model missing from
# MODEL_DAILY_CAPS (the full-text tier's cap).
DEFAULT_MODEL_DAILY_CAP = 20

# Google's quota day is the Pacific date.
_PACIFIC_TZ = ZoneInfo("America/Los_Angeles")

# ---------------------------------------------------------------------------
# BotSetting sync store (Batch AJ / M12 persistence backstop)
#
# The app's async engine cannot be used from the sync call sites that
# own key health (worker threads, ``asyncio.to_thread`` Gemini calls),
# so persistence goes through a small synchronous engine derived from
# the same DATABASE_URL (aiosqlite -> pysqlite, asyncpg -> psycopg2;
# both drivers are pinned dependencies). The store is the existing
# ``bot_settings`` KV table — no schema change. Access is gated: with
# no DATABASE_URL in the environment and no explicit
# ``configure_sync_store()`` call (tests), every operation is a no-op,
# which keeps unit tests and offline tooling completely untouched.
# ---------------------------------------------------------------------------

_SYNC_ENGINE = None
_SYNC_ENGINE_URL: str | None = None
_SYNC_STORE_OVERRIDE: str | None = None
_SYNC_STORE_LOCK = threading.Lock()
_PERSIST_DEBOUNCE_SECONDS = 2.0


def configure_sync_store(url: str | None) -> None:
    """Override the sync-store DATABASE_URL (tests); None restores default."""
    global _SYNC_ENGINE, _SYNC_ENGINE_URL, _SYNC_STORE_OVERRIDE
    with _SYNC_STORE_LOCK:
        _SYNC_STORE_OVERRIDE = url
        if _SYNC_ENGINE is not None:
            try:
                _SYNC_ENGINE.dispose()
            except Exception:
                pass
            _SYNC_ENGINE = None
            _SYNC_ENGINE_URL = None


# Batch DO: asyncpg spells TLS as ?ssl=<mode>; psycopg2 rejects the
# ``ssl`` DSN option outright ("invalid connection option \"ssl\"") —
# it wants ``sslmode``. The Supabase pooler URL carries ?ssl=require,
# which silently killed every sync-store read/write after the
# 2026-11-08 cutover (failures surface only at debug log level).
_ASYNC_PG_SSL_TO_SSLMODE = {
    "require": "require",
    "true": "require",
    "prefer": "prefer",
    "disable": "disable",
    "verify-ca": "verify-ca",
    "verify-full": "verify-full",
}
_ASYNCPG_ONLY_PARAMS = frozenset({
    "statement_cache_size",
    "prepared_statement_cache_size",
    "command_timeout",
    "timeout",
    "max_cached_statement_lifetime",
    "max_cacheable_statement_size",
})
_DEFAULT_SQLITE_URL = "sqlite+aiosqlite:///./data/voice_clone_v2.db"
_URL_CREDS_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@")


def _redact_url_creds(text: object) -> str:
    return _URL_CREDS_RE.sub(r"\1***:***@", str(text or ""))


def _to_sync_url(url: str) -> str:
    """Convert an async or generic DATABASE_URL into its sync-driver equivalent.

    Driver swaps: ``sqlite+aiosqlite:`` -> ``sqlite:``, and PostgreSQL
    schemes (``postgresql+asyncpg:``, ``postgres+asyncpg:``, ``postgres:``,
    ``postgresql:``) -> ``postgresql+psycopg2:``. For psycopg2 URLs, asyncpg-only
    query parameters are stripped and an ``ssl`` param is translated to
    ``sslmode`` (an existing ``sslmode`` wins); valid libpq params remain."""
    raw = str(url or "").strip()
    if raw.startswith("sqlite+aiosqlite:"):
        return "sqlite:" + raw[len("sqlite+aiosqlite:"):]
    sync_url = raw
    for prefix in (
        "postgresql+asyncpg://",
        "postgres+asyncpg://",
        "postgresql://",
        "postgres://",
    ):
        if sync_url.startswith(prefix):
            sync_url = "postgresql+psycopg2://" + sync_url[len(prefix):]
            break
    if "+psycopg2:" not in sync_url:
        return sync_url
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    try:
        parts = urlsplit(sync_url)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
    except Exception:
        return sync_url
    ssl_values = [value for name, value in pairs if name == "ssl"]
    dropped_asyncpg = any(name in _ASYNCPG_ONLY_PARAMS for name, _value in pairs)
    if not ssl_values and not dropped_asyncpg:
        return sync_url
    kept = [
        (name, value)
        for name, value in pairs
        if name != "ssl" and name not in _ASYNCPG_ONLY_PARAMS
    ]
    if ssl_values and not any(name == "sslmode" for name, _value in kept):
        sslmode = _ASYNC_PG_SSL_TO_SSLMODE.get(
            ssl_values[0].strip().lower(), "require")
        kept.append(("sslmode", sslmode))
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(kept), parts.fragment)
    )


def _sync_engine():
    """Lazily build the sync engine, or None when persistence is off."""
    global _SYNC_ENGINE, _SYNC_ENGINE_URL
    url = _SYNC_STORE_OVERRIDE
    if url is None:
        url = os.environ.get("DATABASE_URL") or ""
        if not url:
            try:
                from app.config import settings

                cfg_url = str(getattr(settings, "database_url", "") or "")
                if cfg_url and cfg_url != _DEFAULT_SQLITE_URL:
                    url = cfg_url
            except Exception:
                url = ""
    if not url or ":memory:" in url:
        return None
    sync_url = _to_sync_url(url)
    with _SYNC_STORE_LOCK:
        if _SYNC_ENGINE is not None and _SYNC_ENGINE_URL == sync_url:
            return _SYNC_ENGINE
        try:
            from sqlalchemy import create_engine

            _SYNC_ENGINE = create_engine(
                sync_url,
                pool_pre_ping=True,
                pool_size=1,
                max_overflow=1,
                pool_recycle=1800,
            )
            _SYNC_ENGINE_URL = sync_url
        except Exception as exc:
            logger.warning(
                "key_rotation: sync store unavailable (%s): %s",
                type(exc).__name__, _redact_url_creds(exc),
            )
            _SYNC_ENGINE = None
            _SYNC_ENGINE_URL = None
        return _SYNC_ENGINE


def botsetting_sync_read(key: str) -> str | None:
    """Read one bot_settings value synchronously (None on any failure)."""
    engine = _sync_engine()
    if engine is None:
        return None
    try:
        from sqlalchemy import text

        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT value FROM bot_settings WHERE key = :k"), {"k": str(key)}
            ).first()
        return str(row[0]) if row is not None else None
    except Exception as exc:
        logger.warning(
            "key_rotation: sync store read failed (key=%s, %s): %s",
            key, type(exc).__name__, _redact_url_creds(exc),
        )
        return None


def botsetting_sync_write(key: str, value: str) -> bool:
    """Atomically upsert one bot_settings value synchronously (False on failure)."""
    engine = _sync_engine()
    if engine is None:
        return False
    try:
        from datetime import datetime, timezone

        from sqlalchemy import text

        now = datetime.now(timezone.utc)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO bot_settings (key, value, updated_at) "
                    "VALUES (:k, :v, :t) "
                    "ON CONFLICT (key) DO UPDATE SET "
                    "value = EXCLUDED.value, updated_at = EXCLUDED.updated_at"
                ),
                {"k": str(key), "v": str(value), "t": now},
            )
        return True
    except Exception as exc:
        logger.warning(
            "key_rotation: sync store write failed (key=%s, %s): %s",
            key, type(exc).__name__, _redact_url_creds(exc),
        )
        return False


def botsetting_sync_delete(key: str) -> bool:
    """Delete one bot_settings value synchronously (False on failure)."""
    engine = _sync_engine()
    if engine is None:
        return False
    try:
        from sqlalchemy import text

        with engine.begin() as conn:
            conn.execute(text("DELETE FROM bot_settings WHERE key = :k"), {"k": str(key)})
        return True
    except Exception as exc:
        logger.warning(
            "key_rotation: sync store delete failed (key=%s, %s): %s",
            key, type(exc).__name__, _redact_url_creds(exc),
        )
        return False


def key_id(key: str) -> str:
    """Stable non-secret identifier for a key: 12 hex chars of its SHA-256."""
    return hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:12]


class KeyHealth:
    """Cooldown memory + rotating start offset for ONE provider's keys."""

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        # `clock` is a zero-arg callable returning seconds (time.time by
        # default); tests inject a fake clock to control expiry.
        self._clock = clock or time.time
        self._lock = threading.Lock()
        self._cooldowns: dict[str, float] = {}  # kid or "model:kid" -> expiry
        self._strikes: dict[str, int] = {}  # kid or "model:kid" -> strike count
        self._cursor = 0
        # Batch DN usage ledger: model -> key_id -> [pacific_day, count]
        # of SUCCESSFUL calls. Digests only, like the cooldowns.
        self._usage: dict[str, dict[str, list]] = {}
        # Part 2 (§2C Item 3): per-model TTS accept / DQ-reject counters
        # keyed by model -> [pacific_day, accept, dq_reject] in aj_kh:gemini_tts.
        self._tts_stats: dict[str, list] = {}
        # Phase 1 leak gate (2026-11-24): separate per-model ASR leak-reject counter
        # so accept / dq_reject meanings stay untouched.
        self._tts_asr_stats: dict[str, list] = {}
        # Batch AJ (M12) persistence backstop — off for direct instances.
        self._persist_provider: str | None = None
        self._persist_loaded = False
        self._persist_timer: threading.Timer | None = None

    # -- Batch AJ (M12) persistence -----------------------------------
    # Persisted form: {"v": 1, "cooldowns": {key_id: expiry_epoch},
    # "cursor": int, "usage": {model: {key_id: count}}} under BotSetting
    # key "aj_kh:<provider>". Only digests and timestamps — never raw
    # key material, never text. Batch DN adds the "usage" section;
    # snapshots written before DN have no "usage" key and restore
    # cleanly (restore tolerates its absence).

    def _store_key(self) -> str | None:
        if not self._persist_provider:
            return None
        return f"aj_kh:{self._persist_provider}"

    def enable_persistence(self, provider: str) -> None:
        """Attach this instance to the BotSetting backstop for `provider`."""
        self._persist_provider = str(provider or "").strip().lower() or None

    def snapshot_state(self) -> dict:
        """JSON-serialisable snapshot (digests + expiries + cursor +
        today's usage counts + per-model TTS accept/DQ stats)."""
        with self._lock:
            now = self._clock()
            cooldowns = {
                kid: float(until)
                for kid, until in self._cooldowns.items()
                if until > now
            }
            day = self._pacific_day()
            usage: dict[str, dict[str, int]] = {}
            for model, per_key in self._usage.items():
                rows = {
                    kid: int(entry[1])
                    for kid, entry in per_key.items()
                    if entry and entry[0] == day and int(entry[1]) > 0
                }
                if rows:
                    usage[model] = rows
            tts_stats: dict[str, dict[str, int]] = {}
            for model, entry in self._tts_stats.items():
                if entry and entry[0] == day:
                    acc, rej = int(entry[1]), int(entry[2])
                    if acc > 0 or rej > 0:
                        tts_stats[model] = {"accept": acc, "dq_reject": rej}
            snap = {
                "v": 1,
                "cooldowns": cooldowns,
                "cursor": int(self._cursor),
                "usage": usage,
            }
            if tts_stats:
                snap["tts_stats"] = tts_stats
            return snap

    def restore_state(self, state: object) -> None:
        """Merge a snapshot back in; expired cooldowns are pruned."""
        if not isinstance(state, dict):
            return
        with self._lock:
            now = self._clock()
            raw = state.get("cooldowns") or {}
            if isinstance(raw, dict):
                for kid, until in raw.items():
                    try:
                        expiry = float(until)
                    except (TypeError, ValueError):
                        continue
                    if expiry > now:
                        self._cooldowns[str(kid)] = expiry
            try:
                self._cursor = int(state.get("cursor") or 0)
            except (TypeError, ValueError):
                pass
            # Batch DN: usage section (absent in pre-DN snapshots —
            # tolerated). Restored counts are stamped with the CURRENT
            # Pacific day: snapshots only ever contain same-day counts
            # (snapshot_state prunes stale days), and a count restored
            # just after a Pacific midnight errs conservative (a key
            # looks slightly MORE used than it is), never the reverse.
            raw_usage = state.get("usage") or {}
            if isinstance(raw_usage, dict):
                day = self._pacific_day()
                for model, per_key in raw_usage.items():
                    if not isinstance(per_key, dict):
                        continue
                    target = self._usage.setdefault(str(model), {})
                    for kid, count in per_key.items():
                        try:
                            count = int(count)
                        except (TypeError, ValueError):
                            continue
                        if count <= 0:
                            continue
                        existing = target.get(str(kid))
                        if existing is not None and existing[0] == day:
                            existing[1] = max(int(existing[1]), count)
                        else:
                            target[str(kid)] = [day, count]
            raw_stats = state.get("tts_stats") or {}
            if isinstance(raw_stats, dict):
                day = self._pacific_day()
                for model, row in raw_stats.items():
                    if not isinstance(row, dict):
                        continue
                    try:
                        acc = max(0, int(row.get("accept") or 0))
                        rej = max(0, int(row.get("dq_reject") or 0))
                    except (TypeError, ValueError):
                        continue
                    if acc <= 0 and rej <= 0:
                        continue
                    cur = self._tts_stats.get(str(model))
                    if cur is not None and cur[0] == day:
                        cur[1] = max(int(cur[1]), acc)
                        cur[2] = max(int(cur[2]), rej)
                    else:
                        self._tts_stats[str(model)] = [day, acc, rej]

    def load_persisted(self) -> bool:
        """Lazy one-shot load from the store. True when state was read."""
        if self._persist_loaded or not self._persist_provider:
            return False
        self._persist_loaded = True
        raw = botsetting_sync_read(self._store_key() or "")
        if not raw:
            return False
        try:
            self.restore_state(json.loads(raw))
        except Exception as exc:
            logger.warning(
                "key_rotation: persisted state unreadable (key=%s, %s): %s",
                self._store_key(), type(exc).__name__, _redact_url_creds(exc),
            )
            return False
        return True

    def _maybe_load(self) -> None:
        if not self._persist_loaded and self._persist_provider:
            self.load_persisted()

    def flush_persistence(self) -> bool:
        """Write the current snapshot now (False when persistence is off)."""
        store_key = self._store_key()
        if store_key is None:
            return False
        with self._lock:
            timer = self._persist_timer
            self._persist_timer = None
        if timer is not None and timer is not threading.current_thread():
            timer.cancel()
        try:
            payload = json.dumps(self.snapshot_state())
        except Exception as exc:
            logger.warning(
                "key_rotation: snapshot encode failed (key=%s, %s): %s",
                store_key, type(exc).__name__, _redact_url_creds(exc),
            )
            return False
        return botsetting_sync_write(store_key, payload)

    def _schedule_persist(self) -> None:
        if not self._persist_provider:
            return
        with self._lock:
            if self._persist_timer is not None:
                self._persist_timer.cancel()
            timer = threading.Timer(_PERSIST_DEBOUNCE_SECONDS, self.flush_persistence)
            timer.daemon = True
            self._persist_timer = timer
            timer.start()

    def _is_cooling_locked(self, kid: str, model: str, now: float) -> bool:
        """Check whether `kid` is in cooldown (key-wide OR for `model`). Caller holds `_lock`."""
        if self._cooldowns.get(kid, 0.0) > now:
            return True
        if model:
            return self._cooldowns.get(f"{model}:{kid}", 0.0) > now
        prefix = f":{kid}"
        return any(
            k.endswith(prefix) and until > now
            for k, until in self._cooldowns.items()
        )

    def in_cooldown(self, key: str, model: str = "") -> bool:
        self._maybe_load()
        kid = key_id(key)
        model_norm = str(model or "").strip()
        with self._lock:
            return self._is_cooling_locked(kid, model_norm, self._clock())

    def mark_failure(
        self,
        key: str,
        cooldown_seconds: float | None = None,
        *,
        model: str = "",
    ) -> None:
        """Sideline `key` (per `(model, key)` when `model` is given, else key-wide).

        Part 2 (§2C Item 1): when `model` is supplied and no explicit
        `cooldown_seconds` is forced, strike 1 cools 25–65 s (short RPM/429
        window) with exponential backoff on subsequent consecutive strikes
        for that `(model, key)` instead of locking the key across all
        models for 30 minutes.
        """
        self._maybe_load()
        kid = key_id(key)
        model_norm = str(model or "").strip()
        scope = f"{model_norm}:{kid}" if model_norm else kid
        with self._lock:
            strike = self._strikes.get(scope, 0) + 1
            self._strikes[scope] = strike
            if cooldown_seconds is not None:
                ttl = max(0.0, float(cooldown_seconds))
            elif model_norm:
                if strike <= 1:
                    ttl = random.uniform(
                        MODEL_INITIAL_COOLDOWN_MIN,
                        MODEL_INITIAL_COOLDOWN_MAX,
                    )
                else:
                    idx = min(strike - 2, len(MODEL_COOLDOWN_BACKOFF_STEPS) - 1)
                    ttl = MODEL_COOLDOWN_BACKOFF_STEPS[idx]
            else:
                ttl = DEFAULT_COOLDOWN_SECONDS
            self._cooldowns[scope] = self._clock() + ttl
        self._schedule_persist()

    def mark_success(self, key: str, *, model: str = "") -> None:
        """A key that just worked is healthy again: clear its cooldown/strikes."""
        self._maybe_load()
        kid = key_id(key)
        model_norm = str(model or "").strip()
        with self._lock:
            if model_norm:
                scope = f"{model_norm}:{kid}"
                self._cooldowns.pop(scope, None)
                self._strikes.pop(scope, None)
            else:
                self._cooldowns.pop(kid, None)
                self._strikes.pop(kid, None)
                suffix = f":{kid}"
                for k in [k for k in self._cooldowns if k.endswith(suffix)]:
                    self._cooldowns.pop(k, None)
                for k in [k for k in self._strikes if k.endswith(suffix)]:
                    self._strikes.pop(k, None)
        self._schedule_persist()

    def record_tts_outcome(self, model: str, *, accepted: bool) -> None:
        """Increment today's accept or DQ-reject counter for `model`."""
        self._maybe_load()
        model_norm = str(model or "").strip()
        if not model_norm:
            return
        with self._lock:
            day = self._pacific_day()
            entry = self._tts_stats.get(model_norm)
            if entry is None or entry[0] != day:
                entry = [day, 0, 0]
                self._tts_stats[model_norm] = entry
            if accepted:
                entry[1] = int(entry[1]) + 1
            else:
                entry[2] = int(entry[2]) + 1
        self._schedule_persist()

    def record_tts_accept(self, model: str) -> None:
        self.record_tts_outcome(model, accepted=True)

    def record_tts_dq_reject(self, model: str) -> None:
        self.record_tts_outcome(model, accepted=False)

    def record_tts_asr_reject(self, model: str) -> None:
        """Increment today's ASR leak-reject counter for `model` (separate from dq_reject)."""
        self._maybe_load()
        model_norm = str(model or "").strip()
        if not model_norm:
            return
        with self._lock:
            day = self._pacific_day()
            entry = self._tts_asr_stats.get(model_norm)
            if entry is None or entry[0] != day:
                entry = [day, 0]
                self._tts_asr_stats[model_norm] = entry
            entry[1] = int(entry[1]) + 1
        self._schedule_persist()

    def tts_asr_reject_count(self, model: str) -> int:
        self._maybe_load()
        with self._lock:
            day = self._pacific_day()
            entry = self._tts_asr_stats.get(str(model or "").strip())
            if not entry or entry[0] != day:
                return 0
            return int(entry[1])

    def tts_model_stats(self, model: str | None = None) -> dict:
        """Return today's per-model `{accept, dq_reject, total, dq_reject_rate}` counters."""
        self._maybe_load()
        with self._lock:
            day = self._pacific_day()
            def _fmt(entry: list | None) -> dict[str, float | int]:
                if not entry or entry[0] != day:
                    return {"accept": 0, "dq_reject": 0, "total": 0, "dq_reject_rate": 0.0}
                acc = int(entry[1])
                rej = int(entry[2])
                tot = acc + rej
                rate = round(rej / tot, 4) if tot > 0 else 0.0
                return {"accept": acc, "dq_reject": rej, "total": tot, "dq_reject_rate": rate}
            if model is not None:
                return _fmt(self._tts_stats.get(str(model or "").strip()))
            return {
                m: _fmt(entry)
                for m, entry in self._tts_stats.items()
                if entry and entry[0] == day and (int(entry[1]) > 0 or int(entry[2]) > 0)
            }

    # -- Batch DN: usage ledger + quota-aware ordering -----------------

    def _pacific_day(self) -> str:
        """Today's date (ISO) in America/Los_Angeles — Google's quota
        day. Derived from the injected clock, like cooldown expiry."""
        return datetime.fromtimestamp(self._clock(), tz=_PACIFIC_TZ).date().isoformat()

    def _usage_count_today(self, kid: str, model: str) -> int:
        """Counted successes for (model, kid) on the current Pacific
        day. Caller holds the lock. A stale-day entry reads as 0 —
        the lazy reset: it is only rewritten by the next record."""
        entry = self._usage.get(model, {}).get(kid)
        if not entry or entry[0] != self._pacific_day():
            return 0
        return int(entry[1])

    def record_usage(self, key: str, model: str, n: int = 1) -> None:
        """Count `n` SUCCESSFUL calls of `model` against `key`.

        Success-only by design: a failed call must not consume the
        ledger — a 429 already routes the key into cooldown, so
        counting failures too would double-punish it. Only the key
        digest is ever stored."""
        self._maybe_load()
        model = str(model or "").strip()
        if not model:
            return
        kid = key_id(key)
        step = max(0, int(n))
        with self._lock:
            day = self._pacific_day()
            per_model = self._usage.setdefault(model, {})
            entry = per_model.get(kid)
            if entry is None or entry[0] != day:
                per_model[kid] = [day, step]
            else:
                entry[1] = int(entry[1]) + step
        self._schedule_persist()

    def remaining(self, key: str, model: str) -> Optional[int]:
        """Estimated requests left for (key, model) today: the model's
        daily cap minus our counted successes (stale-day usage counts
        as 0, so a fresh day reads as the full cap).

        None when the model has NO KNOWN CAP — decided by membership
        in MODEL_DAILY_CAPS alone, independent of the ledger. A model
        the cap table does not know carries the conservative
        DEFAULT_MODEL_DAILY_CAP for ordering purposes
        (order_for_model), but reporting a number for it here would
        present an assumption as a measurement, so it stays None."""
        model = str(model or "").strip()
        if not model or model not in MODEL_DAILY_CAPS:
            return None
        self._maybe_load()
        kid = key_id(key)
        with self._lock:
            return MODEL_DAILY_CAPS[model] - self._usage_count_today(kid, model)

    def order_for_model(self, keys: Sequence[str], model: str) -> list[str]:
        """Quota-aware candidate order for ONE model (Batch DN).

        1. Build the rotated list exactly like :meth:`order` (same
           cursor advance) and drop keys in cooldown; if EVERY key is
           cooling, keep the full rotated list — the never-fully-
           offline rule is unchanged.
        2. Stable-sort by today's counted usage for `model`
           ASCENDING — i.e. remaining quota (cap − used) DESCENDING,
           since the cap is per-model. Ties keep the rotated order,
           so consecutive calls start on different keys inside the
           richest group; that rotation is what protects the tight
           per-minute RPM cap (3/min on the TTS models) from one rich
           key absorbing every call.

        An empty/blank model behaves exactly like :meth:`order`. A
        model missing from MODEL_DAILY_CAPS orders with
        DEFAULT_MODEL_DAILY_CAP (it only shifts the sort, never the
        cap arithmetic callers see via :meth:`remaining`)."""
        model = str(model or "").strip()
        if not model:
            return self.order(keys)
        self._maybe_load()
        pool = list(keys)
        if not pool:
            return pool
        with self._lock:
            start = self._cursor % len(pool)
            self._cursor += 1
            now = self._clock()
            cooling = {
                key_id(k)
                for k in pool
                if self._is_cooling_locked(key_id(k), model, now)
            }
            used = {key_id(k): self._usage_count_today(key_id(k), model) for k in pool}
        self._schedule_persist()  # cursor advanced — coalesced write
        rotated = pool[start:] + pool[:start]
        healthy = [k for k in rotated if key_id(k) not in cooling]
        candidates = healthy or rotated
        return sorted(candidates, key=lambda k: used[key_id(k)])

    def order(self, keys: Sequence[str]) -> list[str]:
        """Candidate order for the next call.

        Starts at a rotating offset (advanced once per call, so
        consecutive calls begin at different keys) and skips keys in
        cooldown. If EVERY key is in cooldown the full rotated list is
        returned anyway — a stale cooldown must never take a provider
        fully offline.
        """
        self._maybe_load()
        pool = list(keys)
        if not pool:
            return pool
        with self._lock:
            start = self._cursor % len(pool)
            self._cursor += 1
            now = self._clock()
            cooling = {
                key_id(k)
                for k in pool
                if self._is_cooling_locked(key_id(k), "", now)
            }
        self._schedule_persist()  # cursor advanced — coalesced write
        rotated = pool[start:] + pool[:start]
        healthy = [k for k in rotated if key_id(k) not in cooling]
        return healthy or rotated

    def prioritized(self, keys: Sequence[str]) -> list[str]:
        """Stable partition: healthy keys first, cooled keys last.

        Unlike :meth:`order` this does NOT advance the rotation cursor
        and never drops a key — used where an existing within-run
        rotation must keep its exact semantics and the cooldown may
        only change which key a run STARTS from. (Its last caller, the
        Gemini TTS engine, moved to :meth:`order` in Batch U,
        2026-11-04; the method stays as tested API.)
        """
        self._maybe_load()
        pool = list(keys)
        if not pool:
            return pool
        with self._lock:
            now = self._clock()
            cooling = {
                key_id(k)
                for k in pool
                if self._is_cooling_locked(key_id(k), "", now)
            }
        if not cooling:
            return pool
        healthy = [k for k in pool if key_id(k) not in cooling]
        if not healthy:
            return pool
        return healthy + [k for k in pool if key_id(k) in cooling]

    def reset(self) -> None:
        timer = None
        with self._lock:
            if self._persist_timer is not None:
                timer = self._persist_timer
                self._persist_timer = None
            self._cooldowns.clear()
            self._strikes.clear()
            self._usage.clear()
            self._tts_stats.clear()
            self._tts_asr_stats.clear()
            self._cursor = 0
        if timer is not None:
            timer.cancel()
        # A reset is also a persisted reset: a later process must not
        # resurrect state this process deliberately cleared.
        self._persist_loaded = True
        store_key = self._store_key()
        if store_key is not None:
            botsetting_sync_delete(store_key)


_REGISTRY: dict[str, KeyHealth] = {}
_REGISTRY_LOCK = threading.Lock()


def health_for(provider: str) -> KeyHealth:
    """The process-wide KeyHealth for a provider ("groq", "gemini", ...).

    Shared across call sites so a key cooled by one code path (e.g.
    either Groq transcription implementation) is skipped by the other.
    """
    name = str(provider or "").strip().lower()
    with _REGISTRY_LOCK:
        health = _REGISTRY.get(name)
        if health is None:
            health = KeyHealth()
            # Batch AJ (M12): process-wide instances get the BotSetting
            # backstop; the first real use lazy-loads the snapshot.
            health.enable_persistence(name)
            _REGISTRY[name] = health
        return health


def flush_all_persistence() -> int:
    """Flush every persistent registry KeyHealth snapshot synchronously.

    Safe to call from shutdown hooks and ``atexit`` so debounced updates
    made just before a deploy restart are not discarded with daemon timers.
    """
    with _REGISTRY_LOCK:
        instances = list(_REGISTRY.values())
    flushed = 0
    for health in instances:
        try:
            if health.flush_persistence():
                flushed += 1
        except Exception:
            pass
    return flushed


atexit.register(flush_all_persistence)


def collect_gemini_key_pool(env: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """The ONE shared Gemini key pool: the UNION of every source.

    Resolution order (the recap planner's, now used by the planner,
    the Gemini TTS engine and the group admin alike):

    1. GEMINI_IMAGE_PROJECT_<N>_KEY (numeric order)
    2. GEMINI_API_KEY_<N> (numeric order)
    3. GEMINI_API_KEYS (comma-separated)
    4. GEMINI_API_KEY

    Deduped by value, first occurrence wins. Labels never contain key
    material.
    """
    merged: Mapping[str, str] = os.environ if env is None else env
    pool: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(label: str, value: object) -> None:
        key = str(value or "").strip().strip("'\"")
        if key and key not in seen:
            pool.append((label, key))
            seen.add(key)

    project_nums = sorted(
        int(m.group(1))
        for m in (re.fullmatch(r"GEMINI_IMAGE_PROJECT_(\d+)_KEY", str(k or "").strip()) for k in merged)
        if m
    )
    for n in project_nums:
        add(f"GEMINI_IMAGE_PROJECT_{n}_KEY", merged.get(f"GEMINI_IMAGE_PROJECT_{n}_KEY"))
    key_nums = sorted(
        int(m.group(1))
        for m in (re.fullmatch(r"GEMINI_API_KEY_(\d+)", str(k or "").strip()) for k in merged)
        if m
    )
    for n in key_nums:
        add(f"GEMINI_API_KEY_{n}", merged.get(f"GEMINI_API_KEY_{n}"))
    csv_keys = str(merged.get("GEMINI_API_KEYS") or "").strip()
    if csv_keys:
        for idx, raw_k in enumerate(csv_keys.split(",")):
            add(f"GEMINI_API_KEYS[{idx + 1}]", raw_k)
    add("GEMINI_API_KEY", merged.get("GEMINI_API_KEY"))
    return pool


# ---------------------------------------------------------------------------
# Groq TEXT provider (Round 15 Part E)
#
# Multi-account Groq key pool using existing GROQ_API_KEYS / GROQ_API_KEY env
# vars (deduplicated). Tracked under a separate KeyHealth provider ("groq_text")
# from Whisper ("groq") because Groq enforces independent rate limits for chat
# completions (llama-3.3-70b-versatile: 30 RPM / 1K RPD / 12K TPM / 100K TPD
# per account) versus audio transcription (28,800 audio-seconds/day).
# ---------------------------------------------------------------------------

GROQ_TEXT_PROVIDER = "groq_text"
GROQ_TEXT_MODEL = "qwen/qwen3.8-27b"
GROQ_TEXT_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
GROQ_FREE_TPM_CAP = 12000
_GROQ_TEXT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class GroqPromptTooLargeError(RuntimeError):
    """Raised when a prompt exceeds Groq's per-minute token cap (~12K TPM)."""


def estimate_prompt_tokens(prompt: str) -> int:
    """Estimate token count for a mixed English/Burmese prompt.

    ASCII text runs ~4 chars/token; non-ASCII (e.g. Myanmar script, 3 UTF-8
    bytes per codepoint) runs ~1 token/char in Llama-3 BPE. Used to skip the
    Groq recap fallback gracefully before hitting Groq's 12K TPM free cap.
    """
    text = str(prompt or "")
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    non_ascii_chars = len(text) - ascii_chars
    return int(ascii_chars / 4.0 + non_ascii_chars)


def collect_groq_key_pool(env: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """Collect configured Groq API keys from existing env names, deduplicated.

    Reads GROQ_API_KEYS (comma/semicolon/whitespace separated), any numbered
    GROQ_API_KEY_<N> (numeric order), and GROQ_API_KEY. Labels never contain
    raw key material.
    """
    merged: Mapping[str, str] = os.environ if env is None else env
    pool: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(label: str, value: object) -> None:
        key = str(value or "").strip().strip("'\"")
        if key and key not in seen:
            pool.append((label, key))
            seen.add(key)

    csv_keys = str(merged.get("GROQ_API_KEYS") or "").strip()
    if csv_keys:
        for idx, raw_k in enumerate(re.split(r"[,;\n]+", csv_keys)):
            add(f"GROQ_API_KEYS[{idx + 1}]", raw_k)

    key_nums = sorted(
        int(m.group(1))
        for m in (re.fullmatch(r"GROQ_API_KEY_(\d+)", str(k or "").strip()) for k in merged)
        if m
    )
    for n in key_nums:
        add(f"GROQ_API_KEY_{n}", merged.get(f"GROQ_API_KEY_{n}"))

    add("GROQ_API_KEY", merged.get("GROQ_API_KEY"))
    return pool


def _is_groq_oversize_error(status_code: int, body_text: str) -> bool:
    if status_code == 413:
        return True
    if status_code == 429:
        low = str(body_text or "").lower()
        if "request too large" in low or ("tokens per minute" in low and "requested" in low):
            return True
    return False


def ask_groq_text_sync(
    prompt: str,
    *,
    model: str = GROQ_TEXT_MODEL,
    temperature: float = 0.25,
    max_tokens: int = 4096,
    timeout_seconds: float = 20.0,
    max_attempts: int | None = None,
    json_mode: bool = False,
    max_prompt_tokens: int | None = None,
    deadline_monotonic: float | None = None,
    requester: Callable[..., object] | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """Run one chat-completion call against the health-ordered Groq key pool.

    Rotates across healthy unique keys via ``health_for("groq_text")``.
    Cools dead keys (401/403) key-wide and rate-limited keys (429) per-model.
    Raises ``GroqPromptTooLargeError`` without cooling keys when the prompt
    exceeds ``max_prompt_tokens`` or Groq responds with HTTP 413 / TPM oversize.
    """
    if max_prompt_tokens is not None:
        est_tokens = estimate_prompt_tokens(prompt)
        if est_tokens > int(max_prompt_tokens):
            raise GroqPromptTooLargeError(
                f"groq_text: prompt estimated at ~{est_tokens} tokens exceeds cap ({max_prompt_tokens})"
            )

    pool = collect_groq_key_pool(env)
    if not pool:
        raise RuntimeError("groq_text: no Groq API key configured")

    health = health_for(GROQ_TEXT_PROVIDER)
    pool_keys = [key for _label, key in pool]
    label_by_key = {key: label for label, key in pool}
    ordered_keys = health.order_for_model(pool_keys, model)
    candidate_pool = [(label_by_key[k], k) for k in ordered_keys]

    post_fn = requester
    if post_fn is None:
        import requests

        post_fn = requests.post

    payload: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    est_prompt_tok = estimate_prompt_tokens(prompt)
    last_exc: Exception | None = None
    attempts = 0
    for label, key in candidate_pool:
        if max_attempts is not None and attempts >= int(max_attempts):
            break
        req_timeout = float(timeout_seconds)
        if deadline_monotonic is not None:
            remaining = float(deadline_monotonic) - time.monotonic()
            if remaining <= 0.05:
                last_exc = TimeoutError("groq_text: stage deadline exhausted before attempt")
                break
            req_timeout = min(req_timeout, max(0.5, remaining))
        attempts += 1
        try:
            resp = post_fn(
                GROQ_TEXT_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "User-Agent": _GROQ_TEXT_USER_AGENT,
                },
                json=payload,
                timeout=req_timeout,
            )
            status_code = int(getattr(resp, "status_code", 0) or 0)
            if status_code == 200:
                body = resp.json()
                choices = body.get("choices") if isinstance(body, dict) else None
                content = ""
                if isinstance(choices, list) and choices:
                    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
                    if isinstance(msg, dict):
                        content = str(msg.get("content") or "").strip()
                if content:
                    health.mark_success(key)
                    health.record_usage(key, model)
                    usage = body.get("usage") if isinstance(body, dict) else None
                    real_ptok = usage.get("prompt_tokens") if isinstance(usage, dict) else None
                    comp_tok = usage.get("completion_tokens") if isinstance(usage, dict) else None
                    logger.info(
                        "groq_text: usage model=%s real_prompt_tokens=%s est_prompt_tokens=%d completion_tokens=%s",
                        model,
                        real_ptok,
                        est_prompt_tok,
                        comp_tok,
                    )
                    return content
                last_exc = RuntimeError(f"groq_text: empty response from {model} ({label})")
                continue

            full_body_text = str(getattr(resp, "text", "") or "")
            if _is_groq_oversize_error(status_code, full_body_text):
                logger.warning(
                    "groq_text: prompt oversize HTTP %s on %s (est_prompt_tokens=%d): %s",
                    status_code,
                    model,
                    est_prompt_tok,
                    full_body_text,
                )
                raise GroqPromptTooLargeError(
                    f"groq_text: prompt rejected as oversize (HTTP {status_code}) on {model}: {full_body_text}"
                )
            body_text = full_body_text[:300]
            if status_code in (401, 403):
                health.mark_failure(key)
                last_exc = RuntimeError(f"groq_text: auth failure HTTP {status_code} ({label})")
                logger.warning(
                    "groq_text: key %s (#%s) rejected with HTTP %s; cooled down",
                    label, key_id(key), status_code,
                )
                continue
            if status_code == 429:
                health.mark_failure(key, model=model)
                last_exc = RuntimeError(f"groq_text: rate limit HTTP 429 ({label})")
                logger.warning(
                    "groq_text: key %s (#%s) hit HTTP 429 on %s; trying next key",
                    label, key_id(key), model,
                )
                continue
            last_exc = RuntimeError(f"groq_text: HTTP {status_code} ({label}): {body_text[:120]}")
            logger.warning(
                "groq_text: key %s (#%s) failed HTTP %s; trying next key",
                label, key_id(key), status_code,
            )
        except GroqPromptTooLargeError:
            raise
        except Exception as exc:
            last_exc = exc
            low = str(exc).lower()
            if any(m in low for m in ("401", "403", "invalid_api_key", "invalid api key", "unauthorized")):
                health.mark_failure(key)
            elif any(m in low for m in ("429", "rate_limit", "rate limit", "quota")):
                health.mark_failure(key, model=model)
            logger.warning(
                "groq_text: key %s (#%s) error (%s); trying next key",
                label, key_id(key), type(exc).__name__,
            )

    raise RuntimeError(
        f"groq_text: Groq pool exhausted after {attempts} attempts: {last_exc}"
    )


__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_MODEL_DAILY_CAP",
    "GROQ_FREE_TPM_CAP",
    "GROQ_TEXT_ENDPOINT",
    "GROQ_TEXT_MODEL",
    "GROQ_TEXT_PROVIDER",
    "MODEL_DAILY_CAPS",
    "GroqPromptTooLargeError",
    "KeyHealth",
    "ask_groq_text_sync",
    "botsetting_sync_delete",
    "botsetting_sync_read",
    "botsetting_sync_write",
    "collect_gemini_key_pool",
    "collect_groq_key_pool",
    "configure_sync_store",
    "estimate_prompt_tokens",
    "flush_all_persistence",
    "health_for",
    "key_id",
]

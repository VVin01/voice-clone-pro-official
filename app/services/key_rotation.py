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

import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from typing import Callable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Same 30 minutes the VoxCPM token pool uses for quota-class failures.
DEFAULT_COOLDOWN_SECONDS = 1800.0

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


def _sync_engine():
    """Lazily build the sync engine, or None when persistence is off."""
    global _SYNC_ENGINE, _SYNC_ENGINE_URL
    url = _SYNC_STORE_OVERRIDE
    if url is None:
        if not os.environ.get("DATABASE_URL"):
            return None
        try:
            from app.config import settings

            url = str(settings.database_url)
        except Exception:
            url = os.environ.get("DATABASE_URL", "")
    if not url or ":memory:" in url:
        return None
    sync_url = (
        url.replace("sqlite+aiosqlite:", "sqlite:")
        .replace("postgresql+asyncpg:", "postgresql+psycopg2:")
        .replace("postgres+asyncpg:", "postgresql+psycopg2:")
    )
    with _SYNC_STORE_LOCK:
        if _SYNC_ENGINE is not None and _SYNC_ENGINE_URL == sync_url:
            return _SYNC_ENGINE
        try:
            from sqlalchemy import create_engine

            _SYNC_ENGINE = create_engine(sync_url, pool_pre_ping=True)
            _SYNC_ENGINE_URL = sync_url
        except Exception as exc:
            logger.debug("key_rotation: sync store unavailable: %s", exc)
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
        logger.debug("key_rotation: sync store read failed: %s", exc)
        return None


def botsetting_sync_write(key: str, value: str) -> bool:
    """Upsert one bot_settings value synchronously (False on failure)."""
    engine = _sync_engine()
    if engine is None:
        return False
    try:
        from datetime import datetime, timezone

        from sqlalchemy import text

        now = datetime.now(timezone.utc)
        with engine.begin() as conn:
            result = conn.execute(
                text("UPDATE bot_settings SET value = :v, updated_at = :t WHERE key = :k"),
                {"v": str(value), "t": now, "k": str(key)},
            )
            if not result.rowcount:
                conn.execute(
                    text("INSERT INTO bot_settings (key, value, updated_at) VALUES (:k, :v, :t)"),
                    {"k": str(key), "v": str(value), "t": now},
                )
        return True
    except Exception as exc:
        logger.debug("key_rotation: sync store write failed: %s", exc)
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
        logger.debug("key_rotation: sync store delete failed: %s", exc)
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
        self._cooldowns: dict[str, float] = {}  # key_id -> cooldown expiry
        self._cursor = 0
        # Batch DN usage ledger: model -> key_id -> [pacific_day, count]
        # of SUCCESSFUL calls. Digests only, like the cooldowns.
        self._usage: dict[str, dict[str, list]] = {}
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
        today's usage counts, Batch DN). Usage entries from a stale
        Pacific day are pruned here so a snapshot never carries
        yesterday's counts into a restore."""
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
            return {
                "v": 1,
                "cooldowns": cooldowns,
                "cursor": int(self._cursor),
                "usage": usage,
            }

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
            logger.debug("key_rotation: persisted state unreadable: %s", exc)
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
        try:
            payload = json.dumps(self.snapshot_state())
        except Exception:
            return False
        return botsetting_sync_write(store_key, payload)

    def _schedule_persist(self) -> None:
        if not self._persist_provider:
            return
        timer = self._persist_timer
        if timer is not None:
            timer.cancel()
        timer = threading.Timer(_PERSIST_DEBOUNCE_SECONDS, self.flush_persistence)
        timer.daemon = True
        self._persist_timer = timer
        timer.start()

    def in_cooldown(self, key: str) -> bool:
        self._maybe_load()
        kid = key_id(key)
        with self._lock:
            return self._cooldowns.get(kid, 0.0) > self._clock()

    def mark_failure(self, key: str, cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS) -> None:
        """Sideline `key` for `cooldown_seconds` (default 30 min, for 429/quota)."""
        self._maybe_load()
        kid = key_id(key)
        with self._lock:
            self._cooldowns[kid] = self._clock() + max(0.0, float(cooldown_seconds))
        self._schedule_persist()

    def mark_success(self, key: str) -> None:
        """A key that just worked is healthy again: clear any cooldown."""
        self._maybe_load()
        kid = key_id(key)
        with self._lock:
            self._cooldowns.pop(kid, None)
        self._schedule_persist()

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
            cooling = {kid for kid, until in self._cooldowns.items() if until > now}
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
            cooling = {kid for kid, until in self._cooldowns.items() if until > now}
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
            cooling = {kid for kid, until in self._cooldowns.items() if until > now}
        if not cooling:
            return pool
        healthy = [k for k in pool if key_id(k) not in cooling]
        if not healthy:
            return pool
        return healthy + [k for k in pool if key_id(k) in cooling]

    def reset(self) -> None:
        with self._lock:
            self._cooldowns.clear()
            self._usage.clear()
            self._cursor = 0
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
        key = str(value or "").strip()
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


__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_MODEL_DAILY_CAP",
    "MODEL_DAILY_CAPS",
    "KeyHealth",
    "botsetting_sync_delete",
    "botsetting_sync_read",
    "botsetting_sync_write",
    "collect_gemini_key_pool",
    "configure_sync_store",
    "health_for",
    "key_id",
]

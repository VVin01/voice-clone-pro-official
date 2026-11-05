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
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Callable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

# Same 30 minutes the VoxCPM token pool uses for quota-class failures.
DEFAULT_COOLDOWN_SECONDS = 1800.0

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
        # Batch AJ (M12) persistence backstop — off for direct instances.
        self._persist_provider: str | None = None
        self._persist_loaded = False
        self._persist_timer: threading.Timer | None = None

    # -- Batch AJ (M12) persistence -----------------------------------
    # Persisted form: {"v": 1, "cooldowns": {key_id: expiry_epoch},
    # "cursor": int} under BotSetting key "aj_kh:<provider>". Only
    # digests and timestamps — never raw key material, never text.

    def _store_key(self) -> str | None:
        if not self._persist_provider:
            return None
        return f"aj_kh:{self._persist_provider}"

    def enable_persistence(self, provider: str) -> None:
        """Attach this instance to the BotSetting backstop for `provider`."""
        self._persist_provider = str(provider or "").strip().lower() or None

    def snapshot_state(self) -> dict:
        """JSON-serialisable snapshot (digests + expiries + cursor)."""
        with self._lock:
            now = self._clock()
            cooldowns = {
                kid: float(until)
                for kid, until in self._cooldowns.items()
                if until > now
            }
            return {"v": 1, "cooldowns": cooldowns, "cursor": int(self._cursor)}

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
    "KeyHealth",
    "botsetting_sync_delete",
    "botsetting_sync_read",
    "botsetting_sync_write",
    "collect_gemini_key_pool",
    "configure_sync_store",
    "health_for",
    "key_id",
]

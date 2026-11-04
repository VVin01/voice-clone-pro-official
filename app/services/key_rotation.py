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

* In-memory only — cooldowns and cursors live for the process
  lifetime and reset on restart. No DB, no persistence.
* Thread-safe: one lock guards cooldowns and the rotation cursor.
* Keys are identified by a short SHA-256 digest (``key_id``); raw key
  material is never stored in this module's state and never logged.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from typing import Callable, Mapping, Optional, Sequence

# Same 30 minutes the VoxCPM token pool uses for quota-class failures.
DEFAULT_COOLDOWN_SECONDS = 1800.0


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

    def in_cooldown(self, key: str) -> bool:
        kid = key_id(key)
        with self._lock:
            return self._cooldowns.get(kid, 0.0) > self._clock()

    def mark_failure(self, key: str, cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS) -> None:
        """Sideline `key` for `cooldown_seconds` (default 30 min, for 429/quota)."""
        kid = key_id(key)
        with self._lock:
            self._cooldowns[kid] = self._clock() + max(0.0, float(cooldown_seconds))

    def mark_success(self, key: str) -> None:
        """A key that just worked is healthy again: clear any cooldown."""
        kid = key_id(key)
        with self._lock:
            self._cooldowns.pop(kid, None)

    def order(self, keys: Sequence[str]) -> list[str]:
        """Candidate order for the next call.

        Starts at a rotating offset (advanced once per call, so
        consecutive calls begin at different keys) and skips keys in
        cooldown. If EVERY key is in cooldown the full rotated list is
        returned anyway — a stale cooldown must never take a provider
        fully offline.
        """
        pool = list(keys)
        if not pool:
            return pool
        with self._lock:
            start = self._cursor % len(pool)
            self._cursor += 1
            now = self._clock()
            cooling = {kid for kid, until in self._cooldowns.items() if until > now}
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
            health = _REGISTRY[name] = KeyHealth()
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
    "collect_gemini_key_pool",
    "health_for",
    "key_id",
]

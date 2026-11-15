"""Single source of truth for speech speed across Voice Clone, TTS, and Recap.

Canonical capsule options (identical on all 3 Mini App surfaces):
  -6% (0.94x) · +0% (1.00x) · +12% (1.12x, default) · +20% (1.20x) · +25% (1.25x)

Legacy compatibility:
  The Telegram bot flow (``app/handlers/clone.py``), older saved preferences,
  and historical ``Job`` rows use ``slow`` / ``normal`` / ``fast`` (and ``0.8x`` /
  ``1.0x`` / ``1.2x``). By product guardrail, these always resolve by their
  delivered VoxCPM tempos today:
    slow   -> 0.94 (-6%)
    normal -> 1.12 (+12%)
    fast   -> 1.20 (+20%)
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SPEED_OPTIONS: tuple[str, ...] = ("-6%", "+0%", "+12%", "+20%", "+25%")
DEFAULT_SPEED: str = "+12%"
DEFAULT_TEMPO: float = 1.12
MIN_TEMPO: float = 0.88
MAX_TEMPO: float = 1.45

# Legacy word/multiplier aliases mapped to canonical percent strings by their
# true delivered VoxCPM tempo.
LEGACY_WORD_TO_SPEED: dict[str, str] = {
    "slow": "-6%",
    "0.8x": "-6%",
    "0.8": "-6%",
    "normal": "+12%",
    "1.0x": "+12%",
    "1.0": "+12%",
    "fast": "+20%",
    "1.2x": "+20%",
    "1.2": "+20%",
}

# Retired percent options accepted on API/history for backwards compatibility
# during deploy rollouts and stored-job re-runs.
RETIRED_API_SPEEDS: frozenset[str] = frozenset({"+8%", "+18%", "+22%"})

# Saved user preference migration map -> nearest canonical option without
# altering delivered house speed on legacy Voice ("slow"/"normal"/"fast") or
# drifting user choice on retired Recap/TTS steps.
LEGACY_PREF_MIGRATION: dict[str, str] = {
    **LEGACY_WORD_TO_SPEED,
    "-20%": "-6%",
    "-15%": "-6%",
    "+15%": "+20%",
    "+8%": "+12%",
    "+18%": "+20%",
    "+22%": "+25%",
}

# Compatibility mapping exported for callers importing SPEED_RATES.
SPEED_RATES: dict[str, str] = {
    "slow": "-6%",
    "normal": "+12%",
    "fast": "+20%",
}

_PCT_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)\s*%$")


@dataclass(frozen=True)
class SpeedSpec:
    rate: str
    tempo: float
    pct: int


def _spec_from_pct(pct: int) -> SpeedSpec:
    tempo = max(MIN_TEMPO, min(MAX_TEMPO, round(1.0 + pct / 100.0, 3)))
    clamped_pct = int(round((tempo - 1.0) * 100))
    tempo = round(1.0 + clamped_pct / 100.0, 3)
    return SpeedSpec(rate=f"{clamped_pct:+d}%", tempo=tempo, pct=clamped_pct)


def resolve_speed(raw: float | int | str | None, *, default: str = DEFAULT_SPEED) -> SpeedSpec:
    """Resolve any speed value (canonical, legacy word, float tempo, or %) into
    a unified ``SpeedSpec(rate, tempo, pct)`` guaranteed identical across all
    engines and estimators.
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        tempo = max(MIN_TEMPO, min(MAX_TEMPO, float(raw)))
        return _spec_from_pct(int(round((tempo - 1.0) * 100)))

    if raw is not None:
        s = str(raw).strip().lower()
        if s:
            mapped = LEGACY_WORD_TO_SPEED.get(s)
            if mapped is not None:
                m = _PCT_RE.fullmatch(mapped)
                assert m is not None
                return _spec_from_pct(int(round(float(m.group(1)))))
            m = _PCT_RE.fullmatch(s)
            if m is not None:
                return _spec_from_pct(int(round(float(m.group(1)))))

    if default == raw:
        return _spec_from_pct(12)
    return resolve_speed(default, default=DEFAULT_SPEED)


def resolve_speed_rate(raw: float | int | str | None, *, default: str = DEFAULT_SPEED) -> str:
    return resolve_speed(raw, default=default).rate


def resolve_speed_tempo(raw: float | int | str | None, *, default: str = DEFAULT_SPEED) -> float:
    return resolve_speed(raw, default=default).tempo


def normalize_api_speed(raw: str | None) -> str | None:
    """Validate and canonicalize an API speed input.

    Accepts canonical ``SPEED_OPTIONS``, legacy word presets
    (``slow``/``normal``/``fast`` -> ``-6%``/``+12%``/``+20%``), and retired
    percent options (``+8%``/``+18%``/``+22%``). Returns ``None`` for invalid
    values like ``+99%``.
    """
    if raw is None or not str(raw).strip():
        return DEFAULT_SPEED
    s = str(raw).strip()
    if s in SPEED_OPTIONS or s in RETIRED_API_SPEEDS:
        return s
    low = s.lower()
    if low in LEGACY_WORD_TO_SPEED:
        return LEGACY_WORD_TO_SPEED[low]
    # Normalize unsigned percent like "0%" or "12%" if it matches an accepted value
    m = _PCT_RE.fullmatch(low)
    if m is not None:
        cand = f"{int(round(float(m.group(1)))):+d}%"
        if cand in SPEED_OPTIONS or cand in RETIRED_API_SPEEDS:
            return cand
    return None


def migrate_speed_pref(raw: str | None) -> str | None:
    """Migrate saved ``voice_speed`` / ``recapSpeed`` prefs onto ``SPEED_OPTIONS``
    without losing user intent (or return ``None`` if invalid)."""
    if raw is None or not isinstance(raw, str):
        return None
    s = raw.strip()
    if s in SPEED_OPTIONS:
        return s
    low = s.lower()
    if low in LEGACY_PREF_MIGRATION:
        return LEGACY_PREF_MIGRATION[low]
    m = _PCT_RE.fullmatch(low)
    if m is not None:
        pct = int(round(float(m.group(1))))
        cand = f"{pct:+d}%"
        if cand in SPEED_OPTIONS:
            return cand
        if cand in LEGACY_PREF_MIGRATION:
            return LEGACY_PREF_MIGRATION[cand]
    return None

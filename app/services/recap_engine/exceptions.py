"""Canonical exception classes shared across `app.services.recap_engine` and standalone `core`.

Phase 1 Part 1 (2026-11-24): `recap_pipeline.py` puts `app/services/recap_engine` on
`sys.path` and imports `core` directly, while `app/webapp/api.py` imports from
`app.services.recap_engine.*`. Registering this module under all three alias keys in
`sys.modules` guarantees `RecapParkRequested` is a SINGLE class object across every
import path in the process.
"""

from __future__ import annotations

import sys as _sys

_CANON_MOD_NAME = "app.services.recap_engine.exceptions"
if __name__ != _CANON_MOD_NAME and _CANON_MOD_NAME in _sys.modules:
    _canon = _sys.modules[_CANON_MOD_NAME]
    RecapParkRequested = _canon.RecapParkRequested  # type: ignore[attr-defined]
    RecapFreeQuotaExhaustedError = _canon.RecapFreeQuotaExhaustedError  # type: ignore[attr-defined]
    RecapCloneSynthesisError = _canon.RecapCloneSynthesisError  # type: ignore[attr-defined]
else:

    class RecapParkRequested(Exception):
        """Raised by `EdgeSmartSync` when a PAID recap job hits VoxCPM quota or
        queue-congestion exhaustion so the webapp driver parks it (`WAITING_CHOICE`)
        with its completed segments and held reservation intact."""

        def __init__(
            self,
            message: str,
            *,
            completed_units: int = 0,
            total_units: int = 0,
            pinned_space: str | None = None,
        ) -> None:
            super().__init__(message)
            self.completed_units = int(max(0, completed_units))
            self.total_units = int(max(0, total_units))
            self.pinned_space = str(pinned_space) if pinned_space else None


    class RecapFreeQuotaExhaustedError(RuntimeError):
        """Raised when a FREE VoxCPM recap job hits quota/congestion exhaustion so the
        driver fails fast, restores 100% of reserved credits/minutes immediately, and
        surfaces `worker_error_quota_exhausted` instead of a generic error."""


    class RecapCloneSynthesisError(RuntimeError):
        """Raised when VoxCPM clone synthesis fails due to a deterministic non-quota
        error (e.g. corrupt reference or all v2 spaces returning 5xx with healthy quota)."""


    _self_mod = _sys.modules[__name__]
    for _alias in (_CANON_MOD_NAME, "recap_engine.exceptions", "exceptions"):
        _sys.modules.setdefault(_alias, _self_mod)

__all__ = [
    "RecapParkRequested",
    "RecapFreeQuotaExhaustedError",
    "RecapCloneSynthesisError",
]

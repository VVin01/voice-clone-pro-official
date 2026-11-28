"""
voxcpm_engine.py - Movie Recap Engine adapter for VoxCPM 2.
Directly wraps app.services.voxcpm_engine for zero-shot multilingual TTS and voice cloning.
"""

from __future__ import annotations

from app.services.voxcpm_engine import (
    VoxCPMEngine,
    DEFAULT_VOXCPM_SPACE,
    DEFAULT_VOXCPM_SPACES,
    DEFAULT_BURMESE_CONTROL,
    DEFAULT_CLONE_FIDELITY_CONTROL,
    EXPIRING_RESET_HORIZON_SECONDS,
    JobQuotaMemory,
    JobSpacePin,
    VoxCPMQuotaExhaustedError,
    TRANSIENT_RETRY_BACKOFF_SECONDS,
    _CLONE_INCAPABLE_SPACE_MARKERS,
    get_configured_spaces,
    has_solvent_voxcpm_tokens,
    is_cpu_demo_space,
    is_hf_expiring_quota_first_enabled,
    is_voxcpm_capacity_or_congestion_error,
    is_voxcpm_free_congestion_error,
    is_voxcpm_v1_space,
)

__all__ = [
    "VoxCPMEngine",
    "DEFAULT_VOXCPM_SPACE",
    "DEFAULT_VOXCPM_SPACES",
    "DEFAULT_BURMESE_CONTROL",
    "DEFAULT_CLONE_FIDELITY_CONTROL",
    "EXPIRING_RESET_HORIZON_SECONDS",
    "JobQuotaMemory",
    "JobSpacePin",
    "VoxCPMQuotaExhaustedError",
    "TRANSIENT_RETRY_BACKOFF_SECONDS",
    "_CLONE_INCAPABLE_SPACE_MARKERS",
    "get_configured_spaces",
    "has_solvent_voxcpm_tokens",
    "is_cpu_demo_space",
    "is_hf_expiring_quota_first_enabled",
    "is_voxcpm_capacity_or_congestion_error",
    "is_voxcpm_free_congestion_error",
    "is_voxcpm_v1_space",
]

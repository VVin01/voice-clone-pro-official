"""Batch AJ / L4 — an unreadable output file must not be billed 5 s.

get_wav_duration_seconds returned a hard-coded 5 when both the WAV
header parse and the ffprobe fallback failed, so a voice job whose
output defeated both probes was charged a fabricated 5 seconds and
delivered as SUCCESS. It now raises UnreadableAudioDurationError.

Caller contract (verified read-only in app/worker.py, NOT edited):
process_single_job calls get_wav_duration_seconds at stage 4 inside
the job's main try (worker.py:382, try at :171) whose
``except Exception`` (:551) runs the normal failure/refund path —
so raising fails the job honestly instead of crashing the worker or
billing a made-up duration.
"""
from __future__ import annotations

import struct
import wave
from pathlib import Path

import pytest

from app.services.audio import UnreadableAudioDurationError, get_wav_duration_seconds


def test_unreadable_file_raises_instead_of_fabricated_five_seconds(tmp_path):
    bad = tmp_path / "broken.wav"
    bad.write_bytes(b"this is not audio at all, just text bytes")
    with pytest.raises(UnreadableAudioDurationError):
        get_wav_duration_seconds(bad)


def test_missing_file_raises_instead_of_fabricated_five_seconds(tmp_path):
    with pytest.raises(UnreadableAudioDurationError):
        get_wav_duration_seconds(tmp_path / "nope.wav")


def test_error_is_a_runtime_error_for_worker_failure_handling():
    # The worker's job-failure path catches Exception/RuntimeError —
    # the dedicated type must stay inside that family.
    assert issubclass(UnreadableAudioDurationError, RuntimeError)


def test_valid_wav_duration_still_measured(tmp_path):
    good = tmp_path / "good.wav"
    with wave.open(str(good), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(b"\x00\x00" * 8000 * 4)  # 4 seconds of silence
    assert get_wav_duration_seconds(good) == 4

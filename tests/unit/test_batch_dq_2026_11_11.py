"""Batch DQ — Gemini TTS output-duration gate (prompt-leak guard)
(2026-11-11).

Batch DO's short prompt shape did not fully stop the leak:
gemini-3.8-flash-tts STOCHASTICALLY reads the short instruction
line aloud and repeats the transcript (controlled probe with the
production prompt builder: identical hook text returned 5.96 /
**18.41** / 5.21 / 5.85 s; chunk-ASR of the 18.41 s file heard the
instruction line spoken, then the transcript a second time).
Production victim: recap job rc_1794392082_a0f581 — segments of
21.41 s (4.24 s window), 16.06 s (6.00) and 17.59 s (5.23);
narration total 151.94 s for a 77 s source.

A stochastic model failure cannot be prompt-engineered away, so
``_ask`` now verifies the OUTPUT: audio longer than
``_max_plausible_seconds(cleaned_chars)`` is rejected and the walk
continues to the next key/model. The rejection fires the SAME
bookkeeping as an accept (mark_success + record_usage — the
request really consumed quota and the key is healthy), never
marks the (model, key) dead and never sets busy. If every attempt
is rejected, the existing exhaustion tail raises GeminiTTSError
and the callers' fallbacks engage unchanged.

All clients are fakes — no network, synthetic keys only.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import key_rotation
from app.services.recap_engine import gemini_tts_engine as gte

REPO = Path(__file__).resolve().parents[2]

# A 79-char hook line: cleaned length 79 -> gate bound 12.78 s.
# Clean reads of such a line measure 5.2–6.0 s; the probe's leak
# measured 18.41 s.
HOOK = "က" * 79


@pytest.fixture(autouse=True)
def _reset_state():
    for name in ("gemini", "gemini_tts"):
        key_rotation.health_for(name).reset()
    gte._TTS_MODEL_CURSOR = 0
    yield
    for name in ("gemini", "gemini_tts"):
        key_rotation.health_for(name).reset()
    gte._TTS_MODEL_CURSOR = 0


# ── Cache token ─────────────────────────────────────────────────────

def test_dq_cache_token_is_r140():
    html = (REPO / "app/webapp/static/index.html").read_text(encoding="utf-8")
    assert html.count("20261115_r152") == 4
    assert "r138" not in html


# ── Helper math ─────────────────────────────────────────────────────

def test_pcm_seconds_math():
    assert gte._pcm_seconds(b"") == 0.0
    assert gte._pcm_seconds(b"\x00" * 48000) == 1.0
    assert gte._pcm_seconds(b"\x00" * 24000) == 0.5
    assert gte._pcm_seconds(b"\x00" * 96000) == 2.0


def test_max_plausible_seconds_math():
    # Quality Pass §2C item 3 narrowed bound: min(n/9.0 + 4.0, n/8.5 + 2.2)
    assert gte._max_plausible_seconds(79) == pytest.approx(79 / 8.5 + 2.2)
    assert gte._max_plausible_seconds(150) == pytest.approx(150 / 8.5 + 2.2)
    assert gte._max_plausible_seconds(20) == pytest.approx(20 / 8.5 + 2.2)
    assert gte._max_plausible_seconds(1000) == pytest.approx(1000 / 9.0 + 4.0)


# ── The gate through _ask ───────────────────────────────────────────

def _pcm(seconds: float) -> bytes:
    """Synthetic PCM payload of the given duration (the engine's
    audio is 16-bit mono 24 kHz = 48,000 bytes per second)."""
    return b"\x00\x01" * int(seconds * 24000)


class _Recorder:
    """Fake Gemini client factory: records (key, model) and serves
    PCM payloads from a queue; the last payload repeats."""

    def __init__(self, payloads):
        self.calls: list[tuple[str, str]] = []
        self.payloads = list(payloads)

    def client(self, key):
        recorder = self

        class _Models:
            def generate_content(self, model=None, contents=None, config=None):
                recorder.calls.append((key, model))
                idx = min(len(recorder.calls) - 1, len(recorder.payloads) - 1)
                part = SimpleNamespace(
                    inline_data=SimpleNamespace(data=recorder.payloads[idx]))
                return SimpleNamespace(
                    candidates=[SimpleNamespace(
                        content=SimpleNamespace(parts=[part]))])

        return SimpleNamespace(models=_Models())


def _engine(tmp_path, monkeypatch, recorder, keys=("k1",), logs=None, **kwargs):
    monkeypatch.setattr(gte, "_client", recorder.client)
    return gte.GeminiTTSEngine(
        ffmpeg="ffmpeg", work_dir=tmp_path, keys=list(keys),
        log=(logs.append if logs is not None else (lambda _m: None)),
        **kwargs)


def test_overlong_first_response_rejected_second_returned(
        tmp_path, monkeypatch):
    # The probe's failure shape: a 79-char line read in 18.5 s
    # (bound 12.78 s) — instruction line spoken + transcript repeat.
    overlong, normal = _pcm(18.5), _pcm(5.5)
    recorder = _Recorder([overlong, normal])
    logs: list[str] = []
    eng = _engine(tmp_path, monkeypatch, recorder, logs=logs)
    gte._TTS_MODEL_CURSOR = 0
    data = eng._ask(HOOK, "Charon")
    assert data == normal
    # Exactly two attempts: the rejection walked on to the next model.
    assert len(recorder.calls) == 2
    assert recorder.calls[0][1] != recorder.calls[1][1]
    # The rejection was logged with the shield line.
    assert any(m.startswith("🛡️") and "rejected" in m for m in logs)


def test_all_overlong_responses_raise_gemini_tts_error(
        tmp_path, monkeypatch):
    recorder = _Recorder([_pcm(18.5)])  # every answer overlong
    logs: list[str] = []
    eng = _engine(tmp_path, monkeypatch, recorder, logs=logs)
    with pytest.raises(gte.GeminiTTSError) as excinfo:
        eng._ask(HOOK, "Charon")
    # One rejected try per model in a single round (a rejection sets
    # no busy state, so there is no second round), then the existing
    # exhaustion tail raises carrying the rejection as `last`.
    assert len(recorder.calls) == len(gte.TTS_MODELS)
    assert "output too long" in str(excinfo.value)
    assert sum(m.startswith("🛡️") for m in logs) == len(gte.TTS_MODELS)
    # A rejection never marks the (model, key) dead.
    assert eng._dead == set()


def test_normal_first_response_returned_immediately(tmp_path, monkeypatch):
    normal = _pcm(5.5)
    recorder = _Recorder([normal, _pcm(18.5)])
    logs: list[str] = []
    eng = _engine(tmp_path, monkeypatch, recorder, logs=logs)
    assert eng._ask(HOOK, "Charon") == normal
    assert len(recorder.calls) == 1
    assert not any(m.startswith("🛡️") for m in logs)


def test_audio_exactly_at_the_bound_is_accepted(tmp_path, monkeypatch):
    # 90 chars -> tightened §2C bound 90/8.5 + 2.2 = 12.788235 s; gate rejects only ABOVE it.
    at_bound = _pcm(90 / 8.5 + 2.2)
    recorder = _Recorder([at_bound])
    eng = _engine(tmp_path, monkeypatch, recorder)
    assert eng._ask("က" * 90, "Charon") == at_bound
    assert len(recorder.calls) == 1


def test_long_legit_text_accepted_on_first_attempt(tmp_path, monkeypatch):
    # 1000 chars -> bound 115.1 s; an 80 s read is long but plausible.
    payload = _pcm(80)
    recorder = _Recorder([payload])
    eng = _engine(tmp_path, monkeypatch, recorder)
    assert eng._ask("က" * 1000, "Charon") == payload
    assert len(recorder.calls) == 1


# ── Ledger honesty (§2C item 3): DQ reject tracked separately, not as quota usage ─

def test_rejected_attempt_still_records_usage(tmp_path, monkeypatch):
    recorder = _Recorder([_pcm(18.5), _pcm(5.5)])
    eng = _engine(tmp_path, monkeypatch, recorder)  # pool default
    eng._ask(HOOK, "Charon")
    health = key_rotation.health_for("gemini_tts")
    (key1, model1), (key2, model2) = recorder.calls
    # Quality Pass §2C item 3: stop calling mark_success/record_usage before
    # DQ passes — rejected attempt increments dq_reject without charging usage;
    # the verified second attempt increments accept and charges remaining quota.
    assert health.remaining(key1, model1) == 10
    assert health.tts_model_stats(model1)["dq_reject"] == 1
    assert health.remaining(key2, model2) == 9
    assert health.tts_model_stats(model2)["accept"] == 1


def test_byok_rejection_records_nothing(tmp_path, monkeypatch):
    recorder = _Recorder([_pcm(18.5), _pcm(5.5)])
    eng = _engine(tmp_path, monkeypatch, recorder,
                  keys=["user-own-key"], record_usage=False)
    assert eng._ask(HOOK, "Charon") == _pcm(5.5)
    health = key_rotation.health_for("gemini_tts")
    # The user's own quota never enters the shared pool ledger —
    # not even via a rejected attempt.
    assert health.snapshot_state()["usage"] == {}
    for model in gte.TTS_MODELS:
        assert health.remaining("user-own-key", model) == 10

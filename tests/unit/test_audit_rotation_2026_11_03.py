"""Audit fix E (2026-11-03): API-key rotation hardening.

Covers the shared key-health helper (app.services.key_rotation) and its
wiring:

* Groq transcription iterated keys from #1 on every call — now the
  start rotates across calls and a 429 puts the key into a shared
  cooldown both implementations (transcriber.py, whisper_service.py)
  honour.
* Gemini had THREE pool constructions — now one shared union helper
  serves the recap planner, the Gemini TTS engine and the group admin;
  quota/auth failures in the planner cool the key across jobs.
* The VoxCPM/HF token pool cooled quota-dead tokens but never
  revoked/invalid ones (401/403) — now it cools those too.

All tests run offline: HTTP is faked at the `requests.post` boundary,
Gemini clients are fakes, and VoxCPM clients are the same fakes the
existing HF rotation tests use. Synthetic key values only.
"""
from __future__ import annotations

import time
import wave
from pathlib import Path

import pytest
import requests

from app.services import key_rotation
from app.services.key_rotation import KeyHealth


@pytest.fixture(autouse=True)
def _reset_key_health():
    for name in ("groq", "gemini", "gemini_tts"):
        key_rotation.health_for(name).reset()
    yield
    for name in ("groq", "gemini", "gemini_tts"):
        key_rotation.health_for(name).reset()


class _FakeClock:
    def __init__(self, now: float = 1_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# KeyHealth helper
# ---------------------------------------------------------------------------


def test_key_id_is_stable_and_never_the_key():
    kid = key_rotation.key_id("some-secret-key-value")
    assert kid == key_rotation.key_id("some-secret-key-value")
    assert kid != "some-secret-key-value"
    assert "secret" not in kid
    assert len(kid) == 12


def test_rotation_offset_advances_across_calls():
    health = KeyHealth(clock=_FakeClock())
    keys = ["k1", "k2", "k3"]
    assert health.order(keys) == ["k1", "k2", "k3"]
    assert health.order(keys) == ["k2", "k3", "k1"]
    assert health.order(keys) == ["k3", "k1", "k2"]
    assert health.order(keys) == ["k1", "k2", "k3"]


def test_cooldown_skips_key_and_expiry_restores_it():
    clock = _FakeClock()
    health = KeyHealth(clock=clock)
    health.mark_failure("k2")  # default 30 min cooldown
    assert health.in_cooldown("k2")
    assert not health.in_cooldown("k1")
    assert health.order(["k1", "k2", "k3"]) == ["k1", "k3"]
    clock.advance(1801)
    assert not health.in_cooldown("k2")
    assert "k2" in health.order(["k1", "k2", "k3"])


def test_all_keys_cooled_still_returns_full_rotated_list():
    health = KeyHealth(clock=_FakeClock())
    health.mark_failure("k1")
    health.mark_failure("k2")
    # A stale cooldown must never take the provider fully offline.
    assert sorted(health.order(["k1", "k2"])) == ["k1", "k2"]


def test_mark_success_clears_cooldown():
    health = KeyHealth(clock=_FakeClock())
    health.mark_failure("k1")
    assert health.in_cooldown("k1")
    health.mark_success("k1")
    assert not health.in_cooldown("k1")


def test_prioritized_partitions_without_dropping_or_rotating():
    health = KeyHealth(clock=_FakeClock())
    health.mark_failure("k2")
    assert health.prioritized(["k1", "k2", "k3"]) == ["k1", "k3", "k2"]
    # No cursor advance and no drop: same result on a second call.
    assert health.prioritized(["k1", "k2", "k3"]) == ["k1", "k3", "k2"]
    health.mark_failure("k1")
    health.mark_failure("k3")
    assert health.prioritized(["k1", "k2", "k3"]) == ["k1", "k2", "k3"]


def test_health_registry_is_shared_per_provider():
    assert key_rotation.health_for("groq") is key_rotation.health_for("groq")
    assert key_rotation.health_for("groq") is not key_rotation.health_for("gemini")


# ---------------------------------------------------------------------------
# Gemini: ONE shared union pool
# ---------------------------------------------------------------------------


def test_gemini_pool_helper_returns_deduped_union_of_all_sources():
    env = {
        "GEMINI_IMAGE_PROJECT_2_KEY": "proj2",
        "GEMINI_IMAGE_PROJECT_1_KEY": "proj1",
        "GEMINI_API_KEY_2": "num2",
        "GEMINI_API_KEY_1": "proj1",  # duplicate value: first source wins
        "GEMINI_API_KEYS": "csv1, csv2",
        "GEMINI_API_KEY": "single",
        "UNRELATED": "nope",
    }
    pool = key_rotation.collect_gemini_key_pool(env)
    assert [label for label, _ in pool] == [
        "GEMINI_IMAGE_PROJECT_1_KEY",
        "GEMINI_IMAGE_PROJECT_2_KEY",
        "GEMINI_API_KEY_2",
        "GEMINI_API_KEYS[1]",
        "GEMINI_API_KEYS[2]",
        "GEMINI_API_KEY",
    ]
    assert [key for _, key in pool] == ["proj1", "proj2", "num2", "csv1", "csv2", "single"]


def test_all_three_consumers_use_the_shared_union_pool():
    env = {
        "GEMINI_IMAGE_PROJECT_1_KEY": "proj1",
        "GEMINI_API_KEY": "single",
        "GEMINI_API_KEYS": "csv1",
    }
    expected = key_rotation.collect_gemini_key_pool(env)

    from app.services import group_ai_admin as gai

    assert gai.collect_gemini_key_pool(env) == expected

    from app.services.recap_engine import gemini_tts_engine as gte

    # TTS previously returned ONLY the first non-empty source; the
    # single key and the CSV key were invisible to it.
    assert gte.collect_gemini_keys(env) == ["proj1", "csv1", "single"]

    from app.services.recap_engine import recap_planner as planner

    assert planner._collect_gemini_key_pool(dict(env)) == expected


# ---------------------------------------------------------------------------
# Gemini planner: cooldown memory across jobs, env sync preserved
# ---------------------------------------------------------------------------


class _FakeGeminiAI:
    def __init__(self):
        self.client = None
        self.text_model = ""
        self.api_key = ""

    def _enable_proxy(self):
        pass

    def _disable_proxy(self):
        pass


def _configure_planner_pool(monkeypatch, pool):
    monkeypatch.setenv("GEMINI_API_KEY", "sentinel")
    monkeypatch.setenv("GEMINI_TEXT_MODEL", "sentinel-model")
    ai = _FakeGeminiAI()
    from app.services.recap_engine import recap_planner as planner

    planner._configure_gemini_key_pool(
        ai,
        pool,
        client_factory=lambda api_key, http_options: ("client", api_key),
        log=lambda msg: None,
    )
    return ai


def test_planner_starts_from_healthy_key_and_keeps_env_sync(monkeypatch):
    import os

    pool = [("L1", "key-one"), ("L2", "key-two"), ("L3", "key-three")]
    key_rotation.health_for("gemini").mark_failure("key-one")
    ai = _configure_planner_pool(monkeypatch, pool)
    # The quota-dead key from the earlier job is deprioritized...
    assert ai._recap_active_key_label == "L2"
    assert ai.api_key == "key-two"
    # ...and the deliberate os.environ sync still tracks the ACTIVE key.
    assert os.environ["GEMINI_API_KEY"] == "key-two"
    # Pool size semantics unchanged (cooled keys are reordered, not dropped).
    assert ai._recap_key_pool_size == 3


def test_planner_key_rejection_cools_the_abandoned_key(monkeypatch):
    pool = [("L1", "key-one"), ("L2", "key-two"), ("L3", "key-three")]
    ai = _configure_planner_pool(monkeypatch, pool)
    health = key_rotation.health_for("gemini")
    assert ai._recap_active_key_label == "L1"

    assert ai._recap_rotate_gemini_key() is True
    assert ai._recap_active_key_label == "L2"
    assert health.in_cooldown("key-one")
    assert not health.in_cooldown("key-two")

    assert ai._recap_rotate_gemini_key() is True
    assert ai._recap_active_key_label == "L3"
    assert health.in_cooldown("key-two")

    # Pool exhausted within the model: rotate fails, model advance is
    # the key-pool path and cools the last key too.
    assert ai._recap_rotate_gemini_key() is False
    assert ai._recap_advance_after_key_pool() is True
    assert health.in_cooldown("key-three")
    # Next model restarts from the first (healthiest-ordered) key.
    assert ai._recap_active_key_label == "L1"


# ---------------------------------------------------------------------------
# Groq: rotating start + shared 429 cooldown in BOTH implementations
# ---------------------------------------------------------------------------

_GROQ_KEYS = ["gsk-aaaaaaaaaaaa1", "gsk-bbbbbbbbbbbb2", "gsk-cccccccccccc3"]


class _FakeGroqResponse:
    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = "fake groq response"

    def json(self):
        return self._payload


def _patch_groq(monkeypatch, behaviour: dict, attempts: list):
    """behaviour: key -> HTTP status. Records attempted keys in order."""

    def fake_post(url, headers=None, files=None, data=None, timeout=None):
        key = str((headers or {}).get("Authorization", "")).removeprefix("Bearer ")
        attempts.append(key)
        status = behaviour.get(key, 500)
        payload = {}
        if status == 200:
            payload = {
                "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}],
                "language": "en",
            }
        return _FakeGroqResponse(status, payload)

    monkeypatch.setattr(requests, "post", fake_post)


def _groq_env(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEYS", ",".join(_GROQ_KEYS))
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")


def test_transcriber_groq_start_rotates_across_calls(tmp_path, monkeypatch):
    from app.services import transcriber

    _groq_env(monkeypatch)
    attempts: list = []
    _patch_groq(monkeypatch, {k: 200 for k in _GROQ_KEYS}, attempts)
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\x00" * 64)

    for i in range(3):
        result = transcriber._transcribe_with_groq_srt(str(audio), str(tmp_path / f"out{i}.srt"))
        assert result is not None
    # Each call begins at the NEXT key — no key is always first.
    assert attempts == [_GROQ_KEYS[0], _GROQ_KEYS[1], _GROQ_KEYS[2]]


def test_transcriber_groq_429_cools_key_for_later_calls(tmp_path, monkeypatch):
    from app.services import transcriber

    _groq_env(monkeypatch)
    attempts: list = []
    behaviour = {_GROQ_KEYS[0]: 429, _GROQ_KEYS[1]: 200, _GROQ_KEYS[2]: 200}
    _patch_groq(monkeypatch, behaviour, attempts)
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\x00" * 64)

    result = transcriber._transcribe_with_groq_srt(str(audio), str(tmp_path / "o1.srt"))
    assert result is not None
    assert attempts == [_GROQ_KEYS[0], _GROQ_KEYS[1]]
    assert key_rotation.health_for("groq").in_cooldown(_GROQ_KEYS[0])

    # Second call: rotation starts at key #2, and the cooled key #1 is
    # skipped entirely — even when every other key now fails, the dead
    # key is not retried (avoiding the per-call 429 round-trip).
    attempts.clear()
    behaviour.update({_GROQ_KEYS[1]: 429, _GROQ_KEYS[2]: 429, _GROQ_KEYS[0]: 200})
    result = transcriber._transcribe_with_groq_srt(str(audio), str(tmp_path / "o2.srt"))
    assert result is None
    assert attempts == [_GROQ_KEYS[1], _GROQ_KEYS[2]]


def test_whisper_service_groq_429_cools_key_and_shares_memory(tmp_path, monkeypatch):
    from app.services.recap_engine.services.whisper_service import WhisperTimestampService

    _groq_env(monkeypatch)
    attempts: list = []
    behaviour = {_GROQ_KEYS[0]: 429, _GROQ_KEYS[1]: 200, _GROQ_KEYS[2]: 200}
    _patch_groq(monkeypatch, behaviour, attempts)
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"\x00" * 64)

    svc = WhisperTimestampService(log=lambda msg: None)
    result = svc._transcribe_groq(str(audio))
    assert result is not None and result["backend"] == "groq-whisper"
    assert attempts == [_GROQ_KEYS[0], _GROQ_KEYS[1]]
    # Same shared registry the transcriber uses.
    assert key_rotation.health_for("groq").in_cooldown(_GROQ_KEYS[0])

    attempts.clear()
    behaviour.update({_GROQ_KEYS[1]: 429, _GROQ_KEYS[2]: 429, _GROQ_KEYS[0]: 200})
    svc2 = WhisperTimestampService(log=lambda msg: None)
    assert svc2._transcribe_groq(str(audio)) is None
    assert _GROQ_KEYS[0] not in attempts


# ---------------------------------------------------------------------------
# VoxCPM/HF: auth failures (401/403) now cool the token down too
# ---------------------------------------------------------------------------


def test_voxcpm_auth_failure_classification():
    from app.services import voxcpm_engine as vx

    assert vx._is_hf_token_auth_failure("401 Client Error: Unauthorized for url 'https://huggingface.co/...'")
    assert vx._is_hf_token_auth_failure("Invalid token passed to client")
    assert vx._is_hf_token_auth_failure("403 Forbidden: token lacks access")
    # Generic / quota errors are NOT auth failures.
    assert not vx._is_hf_token_auth_failure("space exploded")
    assert not vx._is_hf_token_auth_failure("ZeroGPU quota exceeded for this account")


def _write_wav(path: Path, seconds: float = 0.1, rate: int = 24000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


class _FakeJob:
    def __init__(self, result_path):
        self._result_path = result_path

    def done(self):
        return True

    def status(self):
        return None

    def result(self):
        return str(self._result_path)


class _FakeClient:
    def __init__(self, result_path=None, error: str | None = None):
        self._result_path = result_path
        self._error = error

    def submit(self, **kwargs):
        if self._error is not None:
            raise RuntimeError(self._error)
        return _FakeJob(self._result_path)


def test_voxcpm_401_rotates_and_cools_down_dead_token(tmp_path, monkeypatch):
    from app.services import voxcpm_engine as vx
    from app.services.voxcpm_engine import VoxCPMEngine

    tok_a = "hf_AAAAAAAAAAAAAAAAAAAAAAAA"
    tok_b = "hf_BBBBBBBBBBBBBBBBBBBBBBBB"
    space = "custom/aaa"

    monkeypatch.setattr(VoxCPMEngine, "_token_cooldowns", {})
    # Batch DI: the balance-ordered walk keeps process-level
    # balance state — reset it with the cooldowns so learned
    # balances never leak between walk tests.
    monkeypatch.setattr(VoxCPMEngine, "_token_balances", {})
    monkeypatch.setattr(VoxCPMEngine, "_space_admission_prices", {})
    monkeypatch.setattr(VoxCPMEngine, "_last_admission_price", None)
    monkeypatch.setattr(VoxCPMEngine, "_token_rr_counter", 0)
    monkeypatch.setattr(vx, "get_available_hf_tokens", lambda: [])

    ok_wav = _write_wav(tmp_path / "ok.wav")
    clients = {
        tok_a: _FakeClient(error="401 Client Error: Unauthorized for url 'https://huggingface.co/api/spaces/custom/aaa/queue/join'"),
        tok_b: _FakeClient(result_path=ok_wav),
        None: _FakeClient(result_path=ok_wav),
    }
    attempts: list = []

    def _get_client(space, hf_token=None):
        attempts.append(hf_token)
        return clients[hf_token]

    monkeypatch.setattr(VoxCPMEngine, "get_client", classmethod(lambda cls, space, hf_token=None: _get_client(space, hf_token)))

    import app.services.curated_voices as cv

    edge_calls: list = []

    def _fake_edge(text, persona_id, output_wav, speed="normal", log_fn=None):
        edge_calls.append(persona_id)
        out = Path(output_wav)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"edge" * 1000)
        return out

    monkeypatch.setattr(cv, "synthesize_curated_persona", _fake_edge)

    engine = VoxCPMEngine(work_dir=tmp_path / "cache")
    engine.spaces = [space]
    engine.hf_tokens = [tok_a, tok_b]

    out = engine.synthesize(
        text="voxcpm auth rotation test",
        output_wav=tmp_path / "out.wav",
        timeout=5,
        total_timeout=30,
    )
    assert Path(out).is_file()
    assert attempts[:2] == [tok_a, tok_b]
    assert edge_calls == []  # failover stayed inside VoxCPM
    # The revoked token is sidelined for later calls, like a quota one.
    assert VoxCPMEngine._token_cooldowns.get(tok_a, 0) > time.time()
    assert tok_b not in VoxCPMEngine._token_cooldowns

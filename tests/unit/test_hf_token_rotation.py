"""Unit tests for HF token rotation on quota failure (2026-10-29).

The live app calls third-party ZeroGPU Spaces with a pool of HF tokens
from the owner's different accounts (HF_TOKENS). ZeroGPU quota is
charged per calling account, so when one account's quota is exhausted
the engine must (a) fail over to a DIFFERENT token within the same
synthesize() call, and (b) record a cooldown for the exhausted token so
later calls stop picking it early. A generic (non-quota) space error
must fail over too, but must NOT cool the token down.

Covers the quota-error classes the engine classifies: ZeroGPU/quota
text and HF HTTP 402 (Payment Required) responses.
"""
from __future__ import annotations

import time
import wave
from pathlib import Path

import pytest

from app.services import voxcpm_engine as vx
from app.services.voxcpm_engine import VoxCPMEngine

# Synthetic fixture values only — not real credentials.
TOK_A = "hf_AAAAAAAAAAAAAAAAAAAAAAAA"
TOK_B = "hf_BBBBBBBBBBBBBBBBBBBBBBBB"
SPACE = "custom/aaa"


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
    """Raises `error` from submit(), or returns a job yielding a wav."""

    def __init__(self, result_path=None, error: str | None = None):
        self._result_path = result_path
        self._error = error

    def submit(self, **kwargs):
        if self._error is not None:
            raise RuntimeError(self._error)
        return _FakeJob(self._result_path)


@pytest.fixture(autouse=True)
def _clean_engine_state(monkeypatch):
    # Cooldowns / round-robin counter are class-level state shared
    # across engine instances; reset them so tests are deterministic.
    monkeypatch.setattr(VoxCPMEngine, "_token_cooldowns", {})
    monkeypatch.setattr(VoxCPMEngine, "_token_rr_counter", 0)


def _make_engine(tmp_path, monkeypatch) -> VoxCPMEngine:
    monkeypatch.setattr(vx, "get_available_hf_tokens", lambda: [])
    engine = VoxCPMEngine(work_dir=tmp_path / "cache")
    engine.spaces = [SPACE]
    engine.hf_tokens = [TOK_A, TOK_B]
    return engine


def _patch_clients(monkeypatch, clients: dict, attempts: list):
    def _get(space, hf_token=None):
        attempts.append(hf_token)
        return clients[hf_token]

    monkeypatch.setattr(
        VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, space, hf_token=None: _get(space, hf_token)),
    )


def _patch_edge(monkeypatch, record: list):
    import app.services.curated_voices as cv

    def _fake_edge(text, persona_id, output_wav, speed="normal", log_fn=None):
        record.append(persona_id)
        out = Path(output_wav)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"edge" * 1000)
        return out

    monkeypatch.setattr(cv, "synthesize_curated_persona", _fake_edge)


def test_zerogpu_quota_error_rotates_to_next_token_and_cools_down(tmp_path, monkeypatch):
    ok_wav = _write_wav(tmp_path / "ok.wav")
    clients = {
        TOK_A: _FakeClient(error="ZeroGPU quota exceeded for this account"),
        TOK_B: _FakeClient(result_path=ok_wav),
        None: _FakeClient(result_path=ok_wav),
    }
    engine = _make_engine(tmp_path, monkeypatch)
    attempts: list = []
    _patch_clients(monkeypatch, clients, attempts)
    edge_calls: list = []
    _patch_edge(monkeypatch, edge_calls)

    out = engine.synthesize(
        text="token rotation quota test",
        output_wav=tmp_path / "out.wav",
        timeout=5,
        total_timeout=30,
    )
    assert Path(out).is_file()
    # First attempt burns token A (quota), second attempt uses token B.
    assert attempts[:2] == [TOK_A, TOK_B]
    # The exhausted account is recorded on cooldown for later calls.
    assert VoxCPMEngine._token_cooldowns.get(TOK_A, 0) > time.time()
    assert TOK_B not in VoxCPMEngine._token_cooldowns
    assert edge_calls == []
    assert engine.last_fallback_used is False


def test_402_payment_required_rotates_and_cools_down(tmp_path, monkeypatch):
    ok_wav = _write_wav(tmp_path / "ok.wav")
    clients = {
        TOK_A: _FakeClient(
            error="Client error '402 Payment Required' for url "
            "'https://huggingface.co/api/spaces/custom/aaa/queue/join'"
        ),
        TOK_B: _FakeClient(result_path=ok_wav),
        None: _FakeClient(result_path=ok_wav),
    }
    engine = _make_engine(tmp_path, monkeypatch)
    attempts: list = []
    _patch_clients(monkeypatch, clients, attempts)
    edge_calls: list = []
    _patch_edge(monkeypatch, edge_calls)

    out = engine.synthesize(
        text="token rotation 402 test",
        output_wav=tmp_path / "out.wav",
        timeout=5,
        total_timeout=30,
    )
    assert Path(out).is_file()
    assert attempts[:2] == [TOK_A, TOK_B]
    assert VoxCPMEngine._token_cooldowns.get(TOK_A, 0) > time.time()
    assert edge_calls == []
    assert engine.last_fallback_used is False


def test_generic_error_rotates_but_does_not_cool_down(tmp_path, monkeypatch):
    ok_wav = _write_wav(tmp_path / "ok.wav")
    clients = {
        TOK_A: _FakeClient(error="space exploded"),
        TOK_B: _FakeClient(result_path=ok_wav),
        None: _FakeClient(result_path=ok_wav),
    }
    engine = _make_engine(tmp_path, monkeypatch)
    attempts: list = []
    _patch_clients(monkeypatch, clients, attempts)
    edge_calls: list = []
    _patch_edge(monkeypatch, edge_calls)

    out = engine.synthesize(
        text="token rotation generic error test",
        output_wav=tmp_path / "out.wav",
        timeout=5,
        total_timeout=30,
    )
    assert Path(out).is_file()
    # Failover still advances to the next token for any error...
    assert attempts[:2] == [TOK_A, TOK_B]
    # ...but a non-quota error must not sideline the token.
    assert TOK_A not in VoxCPMEngine._token_cooldowns
    assert edge_calls == []


def test_cooled_down_token_is_deprioritized_on_next_call(tmp_path, monkeypatch):
    ok_wav = _write_wav(tmp_path / "ok.wav")
    # Token A was marked exhausted by an earlier call.
    monkeypatch.setattr(
        VoxCPMEngine, "_token_cooldowns", {TOK_A: time.time() + 1800}
    )
    clients = {
        TOK_A: _FakeClient(result_path=ok_wav),
        TOK_B: _FakeClient(result_path=ok_wav),
        None: _FakeClient(result_path=ok_wav),
    }
    engine = _make_engine(tmp_path, monkeypatch)
    attempts: list = []
    _patch_clients(monkeypatch, clients, attempts)
    edge_calls: list = []
    _patch_edge(monkeypatch, edge_calls)

    out = engine.synthesize(
        text="cooled token deprioritized test",
        output_wav=tmp_path / "out.wav",
        timeout=5,
        total_timeout=30,
    )
    assert Path(out).is_file()
    # The healthy account is tried first, not the exhausted one.
    assert attempts[0] == TOK_B
    assert edge_calls == []

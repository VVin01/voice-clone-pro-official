"""Unit tests for the VoxCPM failover/voice-quality fix (2026-10-29).

Covers: GPU-first space order, per-space clone capability routing,
unique per-persona yeyee style prompts, space failover before any Edge
fallback, Edge fallback transparency flag/callback, and the recap
per-segment retry seam (one bad line must not flip the whole recap).
"""
from __future__ import annotations

import wave
from pathlib import Path

import pytest

from app.services import voxcpm_engine as vx
from app.services.voxcpm_engine import VoxCPMEngine


def _write_wav(path: Path, seconds: float = 0.1, rate: int = 24000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


class _FakeJob:
    def __init__(self, mode: str, result_path=None):
        self.mode = mode
        self._result_path = result_path

    def done(self):
        return self.mode != "hang"

    def status(self):
        return None

    def result(self):
        return str(self._result_path)


class _FakeClient:
    def __init__(self, behaviour: str, result_path=None, record=None):
        self.behaviour = behaviour
        self._result_path = result_path
        self.record = record if record is not None else []

    def submit(self, **kwargs):
        self.record.append(kwargs.get("api_name"))
        if self.behaviour == "raise":
            raise RuntimeError("space exploded")
        return _FakeJob(self.behaviour, self._result_path)


def _make_engine(tmp_path, monkeypatch, spaces):
    monkeypatch.setattr(vx, "get_available_hf_tokens", lambda: [])
    engine = VoxCPMEngine(work_dir=tmp_path / "cache")
    engine.spaces = list(spaces)
    engine.hf_tokens = []
    return engine


def _patch_clients(monkeypatch, clients: dict):
    monkeypatch.setattr(
        VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, space, hf_token=None: clients[space]),
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


# --- space order & capabilities -------------------------------------

def test_default_space_order_gpu_first_cpu_demo_last():
    spaces = vx.DEFAULT_VOXCPM_SPACES
    assert spaces[0] == "yeyee2609/voxcpm2"
    assert spaces[-1] == "openbmb/VoxCPM-Demo"
    assert spaces.index("soemintheinsmt6/myanmar-voxcpm2") < spaces.index("openbmb/VoxCPM-Demo")


def test_configured_spaces_env_override_comes_first(monkeypatch):
    monkeypatch.setenv("VOXCPM_SPACES", "custom/aaa,custom/bbb")
    monkeypatch.delenv("VOXCPM_SPACE_URL", raising=False)
    monkeypatch.delenv("VOXCPM_API_URL", raising=False)
    spaces = vx.get_configured_spaces()
    assert spaces[:2] == ["custom/aaa", "custom/bbb"]
    assert spaces.index("custom/bbb") < spaces.index("yeyee2609/voxcpm2")


def test_space_supports_clone():
    assert vx.space_supports_clone("soemintheinsmt6/myanmar-voxcpm2") is False
    assert vx.space_supports_clone("yeyee2609/voxcpm2") is True
    assert vx.space_supports_clone("openbmb/VoxCPM-Demo") is True


def test_preset_map_yeyee_unique_per_persona():
    values = list(vx.PRESET_MAP_YEYEE.values())
    assert len(values) == len(set(values)), "two personas share one preset/style"
    for pid in ("komin", "kothura", "koaung", "kophone", "mahnin", "manadi", "machaw", "mayoon"):
        assert pid in vx.PRESET_MAP_YEYEE
    assert vx.PRESET_MAP_YEYEE["komyat"] != vx.PRESET_MAP_YEYEE["komin"]
    assert vx.PRESET_MAP_YEYEE["machaw"] != vx.PRESET_MAP_YEYEE["mamay"]


# --- failover behaviour ----------------------------------------------

def test_failover_timeout_first_space_second_used_edge_not_called(tmp_path, monkeypatch):
    ok_wav = _write_wav(tmp_path / "ok.wav")
    clients = {
        "yeyee2609/voxcpm2": _FakeClient("hang"),
        "soemintheinsmt6/myanmar-voxcpm2": _FakeClient("ok", ok_wav),
    }
    engine = _make_engine(tmp_path, monkeypatch, list(clients))
    _patch_clients(monkeypatch, clients)
    edge_calls: list = []
    _patch_edge(monkeypatch, edge_calls)

    out = engine.synthesize(text="မင်္ဂလာပါ failover test", output_wav=tmp_path / "out.wav", timeout=1, total_timeout=30)
    assert Path(out).is_file()
    assert edge_calls == []
    assert engine.last_fallback_used is False


def test_all_spaces_fail_edge_fallback_with_flag_and_callback(tmp_path, monkeypatch):
    clients = {
        "yeyee2609/voxcpm2": _FakeClient("raise"),
        "soemintheinsmt6/myanmar-voxcpm2": _FakeClient("raise"),
    }
    engine = _make_engine(tmp_path, monkeypatch, list(clients))
    _patch_clients(monkeypatch, clients)
    edge_calls: list = []
    _patch_edge(monkeypatch, edge_calls)
    cb_reasons: list = []

    out = engine.synthesize(
        text="မင်္ဂလာပါ fallback test",
        output_wav=tmp_path / "out.wav",
        timeout=1,
        total_timeout=30,
        fallback_cb=cb_reasons.append,
    )
    assert Path(out).is_file()
    assert len(edge_calls) == 1
    assert engine.last_fallback_used is True
    assert cb_reasons and "exploded" in cb_reasons[0]


def test_no_fallback_mode_raises_instead_of_edge(tmp_path, monkeypatch):
    clients = {"yeyee2609/voxcpm2": _FakeClient("raise")}
    engine = _make_engine(tmp_path, monkeypatch, list(clients))
    _patch_clients(monkeypatch, clients)
    edge_calls: list = []
    _patch_edge(monkeypatch, edge_calls)
    with pytest.raises(RuntimeError):
        engine.synthesize(
            text="no fallback please",
            output_wav=tmp_path / "out.wav",
            timeout=1,
            total_timeout=10,
            allow_fallback=False,
        )
    assert edge_calls == []


# --- clone routing -----------------------------------------------------

def test_clone_request_never_routed_to_prompt_only_space(tmp_path, monkeypatch):
    ref = _write_wav(tmp_path / "ref.wav")
    ok_wav = _write_wav(tmp_path / "ok.wav")
    soemin_record: list = []
    yeyee_record: list = []
    clients = {
        "soemintheinsmt6/myanmar-voxcpm2": _FakeClient("ok", ok_wav, soemin_record),
        "yeyee2609/voxcpm2": _FakeClient("ok", ok_wav, yeyee_record),
    }
    engine = _make_engine(tmp_path, monkeypatch, list(clients))
    _patch_clients(monkeypatch, clients)
    edge_calls: list = []
    _patch_edge(monkeypatch, edge_calls)

    out = engine.synthesize(
        text="clone this voice exactly",
        output_wav=tmp_path / "out.wav",
        reference_wav=ref,
        prompt_text="reference transcript",
        timeout=5,
        total_timeout=30,
    )
    assert Path(out).is_file()
    assert soemin_record == []  # prompt-only space never sees a clone request
    assert yeyee_record == ["/generate_clone"]


def test_plain_tts_may_use_prompt_only_space(tmp_path, monkeypatch):
    ok_wav = _write_wav(tmp_path / "ok.wav")
    record: list = []
    clients = {"soemintheinsmt6/myanmar-voxcpm2": _FakeClient("ok", ok_wav, record)}
    engine = _make_engine(tmp_path, monkeypatch, list(clients))
    _patch_clients(monkeypatch, clients)

    out = engine.synthesize(text="plain tts request", output_wav=tmp_path / "out.wav", timeout=5, total_timeout=30)
    assert Path(out).is_file()
    assert record == ["/tts"]


# --- recap per-segment policy ------------------------------------------

def test_recap_segment_retry_only_failing_segment_falls_back(tmp_path):
    from app.services.recap_engine.core import EdgeSmartSync

    sync = EdgeSmartSync.__new__(EdgeSmartSync)
    sync.log = lambda msg: None

    calls = {"bad": 0, "good": 0}

    class _FakeVox:
        def synthesize(self, *, text, **kwargs):
            if "bad" in text:
                calls["bad"] += 1
                raise RuntimeError("queue timeout")
            calls["good"] += 1
            return kwargs["output_wav"]

    good, err = sync._vox_synthesize_with_retry(
        _FakeVox(), speak="good line", dst=tmp_path / "g.wav", ctrl_prompt="x",
        persona_ref_wav=None, persona_ref_text="", preset_v=None, speed_float=1.1,
    )
    assert err is None and good is not None

    bad, err = sync._vox_synthesize_with_retry(
        _FakeVox(), speak="bad line", dst=tmp_path / "b.wav", ctrl_prompt="x",
        persona_ref_wav=None, persona_ref_text="", preset_v=None, speed_float=1.1,
    )
    assert bad is None and err is not None
    assert calls["bad"] == 2  # retried once at segment level, then only it falls back


def test_recap_core_no_whole_recap_switch_left():
    src = Path("app/services/recap_engine/core.py").read_text(encoding="utf-8")
    assert "switching to consistent Studio Persona engine for entire recap" not in src
    assert "use_voxcpm = False  # Keep whole recap uniform" not in src
    assert "fallback_segments" in src

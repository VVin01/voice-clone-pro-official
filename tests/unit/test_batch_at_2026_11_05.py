"""Batch AT (2026-11-05): VoxCPM failover walk fixes.

Spec: ~/workspace/batch-at-spec-2026-11-05.md (owner-approved
2026-11-05 22:39). Root-cause evidence: production logs for recap job
rc_1793880623_7150ea (Window A) and voice job a670e490 (Window B).

Pins:
- AT-1: the (space, token) walk is space-first round-robin — at most
  one pair per space per round before any space repeats — on the one
  engine both the voice path and the recap path share.
- AT-2: attempts per space inside one synthesis call are capped at
  MAX_ATTEMPTS_PER_SPACE even when tokens remain on that space.
- AT-3: yunaisbusy/voxcpm2-fast is reached through the endpoint names
  its app.py actually exposes (generate_clone / generate_style) with
  the engine's existing yeyee-family call shape; unmapped spaces keep
  the "/generate" default.
- AT-4: pair-failure log lines carry the exception type AND message
  (truncated), never the bare class name alone.
- AT-5: a bound worker's starting token advances per call, and two
  consecutive unbound calls start at different offsets, so parallel
  workers/jobs do not converge on the first pool tokens.
"""
from __future__ import annotations

import wave
from pathlib import Path

import pytest

from app.services import voxcpm_engine as vx
from app.services.voxcpm_engine import VoxCPMEngine


def _write_wav(path: Path, seconds: float = 0.3, rate: int = 24000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


class _FakeJob:
    def __init__(self, result_path=None):
        self._result_path = result_path

    def done(self):
        return True

    def status(self):
        return None

    def result(self):
        return str(self._result_path)


class _RecClient:
    """Fake gradio client: behaviour per (space, token); records the
    pair and the full submit kwargs of every attempt."""

    def __init__(self, behaviour: str, result_path, record: list, key, submit_record: list):
        self.behaviour = behaviour
        self._result_path = result_path
        self.record = record
        self.key = key
        self.submit_record = submit_record

    def submit(self, **kwargs):
        self.record.append(self.key)
        self.submit_record.append(dict(kwargs))
        if self.behaviour == "boom":
            raise RuntimeError("space exploded")
        if self.behaviour == "value_error":
            raise ValueError("backend exploded detail 12345")
        return _FakeJob(self._result_path)


def _vox_engine(tmp_path, monkeypatch, *, tokens, spaces, **kw):
    monkeypatch.setattr(vx, "get_available_hf_tokens", lambda: list(tokens))
    monkeypatch.setattr(VoxCPMEngine, "_token_cooldowns", {})
    # Batch DI: the balance-ordered walk keeps process-level
    # balance state — reset it with the cooldowns so learned
    # balances never leak between walk tests.
    monkeypatch.setattr(VoxCPMEngine, "_token_balances", {})
    monkeypatch.setattr(VoxCPMEngine, "_space_admission_prices", {})
    monkeypatch.setattr(VoxCPMEngine, "_last_admission_price", None)
    monkeypatch.setattr(VoxCPMEngine, "_token_rr_counter", 0)
    monkeypatch.setattr(VoxCPMEngine, "_token_bind_rr_counters", {})
    engine = VoxCPMEngine(work_dir=tmp_path / "cache", **kw)
    engine.spaces = list(spaces)
    engine.hf_tokens = list(tokens)
    return engine


def _patch_vox_clients(monkeypatch, behaviours: dict, result_path, record: list, submit_record: list):
    clients = {
        key: _RecClient(behaviour, result_path, record, key, submit_record)
        for key, behaviour in behaviours.items()
    }
    monkeypatch.setattr(
        VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, space, hf_token=None: clients[(space, hf_token)]),
    )
    return clients


# ── AT-1: space-first round-robin walk ───────────────────────────────

def test_round_robin_one_pair_per_space_per_round(tmp_path, monkeypatch):
    result = _write_wav(tmp_path / "ok.wav")
    record: list = []
    submits: list = []
    spaces = ("spaceA", "spaceB", "spaceC")
    behaviours = {(sp, tok): "boom" for sp in spaces for tok in ("hf_aaa", None)}
    behaviours[("spaceC", None)] = "ok"
    engine = _vox_engine(tmp_path, monkeypatch, tokens=["hf_aaa"], spaces=spaces)
    _patch_vox_clients(monkeypatch, behaviours, result, record, submits)

    out = engine.synthesize(text="round robin line", output_wav=tmp_path / "o.wav", allow_fallback=False)
    assert out.is_file()
    # Token-major rounds: every space is tried once with hf_aaa before
    # any space gets a second (anonymous) attempt. The pre-AT
    # space-major order would have been spaceA×2, spaceB×2, spaceC×2.
    assert record == [
        ("spaceA", "hf_aaa"), ("spaceB", "hf_aaa"), ("spaceC", "hf_aaa"),
        ("spaceA", None), ("spaceB", None), ("spaceC", None),
    ]
    # Round property stated directly: the first len(spaces) attempts
    # are all on distinct spaces.
    first_round = [sp for sp, _tok in record[: len(spaces)]]
    assert len(set(first_round)) == len(spaces)


def test_round_robin_reaches_healthy_space_before_budget_dies(tmp_path, monkeypatch):
    # Window B shape in miniature: space #1 fails generically on every
    # token; the healthy space must be reached in round one, not after
    # space #1's whole token row.
    result = _write_wav(tmp_path / "ok.wav")
    record: list = []
    submits: list = []
    tokens = ["hf_aaa", "hf_bbb", "hf_ccc"]
    behaviours = {(sp, tok): "boom" for sp in ("spaceA", "spaceB") for tok in (*tokens, None)}
    behaviours[("spaceB", "hf_aaa")] = "ok"
    engine = _vox_engine(tmp_path, monkeypatch, tokens=tokens, spaces=("spaceA", "spaceB"))
    _patch_vox_clients(monkeypatch, behaviours, result, record, submits)

    out = engine.synthesize(text="window b shape", output_wav=tmp_path / "o.wav", allow_fallback=False)
    assert out.is_file()
    assert record == [("spaceA", "hf_aaa"), ("spaceB", "hf_aaa")]


# ── AT-2: per-space attempt cap ──────────────────────────────────────

def test_per_space_attempt_cap_spreads_the_walk(tmp_path, monkeypatch):
    result = _write_wav(tmp_path / "ok.wav")
    record: list = []
    submits: list = []
    tokens = [f"hf_t{i}" for i in range(6)]
    spaces = ("spaceA", "spaceB")
    behaviours = {(sp, tok): "boom" for sp in spaces for tok in (*tokens, None)}
    engine = _vox_engine(tmp_path, monkeypatch, tokens=tokens, spaces=spaces)
    _patch_vox_clients(monkeypatch, behaviours, result, record, submits)

    with pytest.raises(RuntimeError):
        engine.synthesize(text="cap line", output_wav=tmp_path / "o.wav", allow_fallback=False)
    # 7 token rows × 2 spaces = 14 candidate pairs, but each space is
    # capped: exactly MAX_ATTEMPTS_PER_SPACE attempts per space, in
    # round-robin order, then the walk ends.
    cap = vx.MAX_ATTEMPTS_PER_SPACE
    assert cap == 4
    assert len(record) == 2 * cap
    assert [sp for sp, _t in record].count("spaceA") == cap
    assert [sp for sp, _t in record].count("spaceB") == cap
    assert record[:4] == [
        ("spaceA", "hf_t0"), ("spaceB", "hf_t0"),
        ("spaceA", "hf_t1"), ("spaceB", "hf_t1"),
    ]


# ── AT-3: voxcpm2-fast endpoint mapping ──────────────────────────────

FAST = "yunaisbusy/voxcpm2-fast"


def test_space_api_endpoints_mapping_and_default():
    fast = vx.space_api_endpoints(FAST)
    assert fast["clone"] == "/generate_clone"
    assert fast["style"] == "/generate_style"
    assert fast["preset"] == "/generate_preset"
    # The yeyee family the fast space duplicates keeps its endpoints.
    assert vx.space_api_endpoints("yeyee2609/voxcpm2")["clone"] == "/generate_clone"
    # Everything else defaults to the openbmb-style "/generate".
    for sp in ("openbmb/VoxCPM-Demo", "yunaisbusy/voxcpm2", "yunaisbusy/voxcpm2-2", "someone/else"):
        assert vx.space_api_endpoints(sp)["clone"] == "/generate"
        assert vx.space_api_endpoints(sp)["style"] == "/generate"


def test_fast_space_clone_uses_generate_clone_with_existing_shape(tmp_path, monkeypatch):
    ref = _write_wav(tmp_path / "ref.wav")
    result = _write_wav(tmp_path / "ok.wav")
    record: list = []
    submits: list = []
    engine = _vox_engine(tmp_path, monkeypatch, tokens=[], spaces=(FAST,))
    _patch_vox_clients(monkeypatch, {(FAST, None): "ok"}, result, record, submits)

    out = engine.synthesize(
        text="clone this please", output_wav=tmp_path / "o.wav",
        reference_wav=ref, prompt_text="reference transcript",
        allow_fallback=False,
    )
    assert out.is_file()
    assert len(submits) == 1
    # The call shape is the engine's pre-existing yeyee-family clone
    # shape — exactly the parameter names generate_clone declares in
    # the fast space's app.py (HF API read 2026-11-05). Nothing guessed.
    assert submits[0]["api_name"] == "/generate_clone"
    assert set(submits[0]) == {"ref_audio_path", "ref_text", "target_text", "api_name"}
    assert submits[0]["ref_text"] == "reference transcript"
    assert submits[0]["target_text"] == "clone this please"


def test_fast_space_plain_tts_uses_generate_style(tmp_path, monkeypatch):
    result = _write_wav(tmp_path / "ok.wav")
    record: list = []
    submits: list = []
    engine = _vox_engine(tmp_path, monkeypatch, tokens=[], spaces=(FAST,))
    _patch_vox_clients(monkeypatch, {(FAST, None): "ok"}, result, record, submits)

    out = engine.synthesize(text="plain fast line", output_wav=tmp_path / "o.wav", allow_fallback=False)
    assert out.is_file()
    assert len(submits) == 1
    assert submits[0]["api_name"] == "/generate_style"
    assert set(submits[0]) == {"style", "target_text", "api_name"}


def test_unmapped_space_still_uses_generate(tmp_path, monkeypatch):
    result = _write_wav(tmp_path / "ok.wav")
    record: list = []
    submits: list = []
    engine = _vox_engine(tmp_path, monkeypatch, tokens=[], spaces=("openbmb/VoxCPM-Demo",))
    _patch_vox_clients(monkeypatch, {("openbmb/VoxCPM-Demo", None): "ok"}, result, record, submits)

    out = engine.synthesize(text="demo line", output_wav=tmp_path / "o.wav", allow_fallback=False)
    assert out.is_file()
    assert submits[0]["api_name"] == "/generate"


# ── AT-4: error detail in pair-failure logs ──────────────────────────

def test_pair_failure_log_carries_type_and_message(tmp_path, monkeypatch):
    result = _write_wav(tmp_path / "ok.wav")
    record: list = []
    submits: list = []
    logs: list = []
    engine = _vox_engine(tmp_path, monkeypatch, tokens=[], spaces=("spaceA",))
    engine.log_fn = logs.append
    _patch_vox_clients(monkeypatch, {("spaceA", None): "value_error"}, result, record, submits)

    with pytest.raises(RuntimeError):
        engine.synthesize(text="log line", output_wav=tmp_path / "o.wav", allow_fallback=False)
    pair_lines = [m for m in logs if "Pair (" in m and "failed:" in m]
    assert pair_lines, "no pair-failure line was logged"
    assert "ValueError" in pair_lines[0]
    assert "backend exploded detail 12345" in pair_lines[0]


def test_format_pair_error_truncates_to_300():
    rendered = vx._format_pair_error(ValueError("x" * 500))
    assert rendered.startswith("ValueError: ")
    assert len(rendered) <= 300
    assert vx._format_pair_error(RuntimeError("boom")) == "RuntimeError: boom"


# ── AT-5: token rotation spread ──────────────────────────────────────

def test_bound_worker_start_advances_per_call(tmp_path, monkeypatch):
    result = _write_wav(tmp_path / "ok.wav")
    record: list = []
    submits: list = []
    tokens = ["hf_aaa", "hf_bbb"]
    behaviours = {(sp, tok): "ok" for sp in ("spaceA",) for tok in (*tokens, None)}
    engine = _vox_engine(
        tmp_path, monkeypatch, tokens=tokens, spaces=("spaceA",), token_bind_index=0,
    )
    _patch_vox_clients(monkeypatch, behaviours, result, record, submits)

    starts = []
    for i in range(3):
        record.clear()
        engine.synthesize(text=f"bound spread line {i}", output_wav=tmp_path / f"o{i}.wav", allow_fallback=False)
        starts.append(record[0][1])
    # First call starts at the worker's own bound token (Batch AL
    # property, preserved); each later call starts one token further
    # round instead of re-picking the head of the pool every time.
    assert starts == ["hf_aaa", "hf_bbb", "hf_aaa"]
    assert starts[0] != starts[1]


def test_unbound_consecutive_calls_start_at_different_offsets(tmp_path, monkeypatch):
    result = _write_wav(tmp_path / "ok.wav")
    record: list = []
    submits: list = []
    tokens = ["hf_aaa", "hf_bbb", "hf_ccc"]
    behaviours = {(sp, tok): "ok" for sp in ("spaceA",) for tok in (*tokens, None)}
    engine = _vox_engine(tmp_path, monkeypatch, tokens=tokens, spaces=("spaceA",))
    _patch_vox_clients(monkeypatch, behaviours, result, record, submits)

    starts = []
    for i in range(3):
        record.clear()
        engine.synthesize(text=f"unbound spread line {i}", output_wav=tmp_path / f"o{i}.wav", allow_fallback=False)
        starts.append(record[0][1])
    # Distribution property: three consecutive calls start on three
    # different pool tokens (the shared round-robin counter advances).
    assert starts == ["hf_aaa", "hf_bbb", "hf_ccc"]
    assert len(set(starts)) == 3

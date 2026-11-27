"""Unit tests for HF Short-Job Gate Fix (2026-11-27).

Covers all 6 owner-mandated verification groups:
1. Part 1 & 2b: 59-char (`4c9c467a`) and ~75-char (`64135cd2`) session calls reconcile the 30 s
   declaration floor (`session_admission_gate_s == 45.0` for 0..141 chars while `session_call_gate_s`
   keeps exact x1.0 debit/reservation math); known 38 s / 40 s tokens across 13 Spaces suffer 0
   refusals (`sub_gate_skips >= 2`); a token drifted 60 -> 38 s mid-walk suffers exactly 1 refusal
   and all remaining Spaces skip it (`ceiling_skips >= 1`).
2. Part 3: Unpinned Slice 1 with 15 drifted tokens + healthy 300 s tokens across 13 Spaces incurs
   at most 2 Best-Fit refusals, lands on a 300 s Max-Fit token on Attempt 3, pins the Space, and
   records `max_fit_jumps == 1`.
3. Part 4: Free walk refusal sub-budget (`FREE_WALK_MAX_QUOTA_REFUSALS == 6`,
   `FREE_WALK_MAX_REFUSAL_WALL_S == 20.0`) raises `VoxCPMQuotaExhaustedError` after <= 6 quota
   refusals and <= 20 s refusal wall; Paid walk in the identical setup is NOT sub-budget-capped.
4. Part 5 (Wrinkle 5a): Session refusal with `requested_s` above the formula door records
   `session_space_floors[sp]` (omitted from `aj_hf:pool_state` when empty) without mutating
   legacy `space_gates[sp]`; a 138 s refusal on an 850-char slice does NOT raise the floor for
   short calls, whereas a static-120s Space (`requested_s >= 175` on <150s formula) IS learned.
5. Part 2a (Wrinkle 5b): Repeated refusals on one token never raise `_refusal_rem_baseline`
   upward and never extend `refused_until` unless the ceiling strictly tightened.
6. Part 6 Telemetry: `clone_prosody_summary` and `recap_voxcpm_tts_summary` emit
   `sub_gate_skips`, `adm_gates_s`, `observed_doors_s`, `refused_tokens`, `refusals_by_space`;
   1-slice jobs with 1 refusal per drifted token report `quota_refusals == refused_tokens`,
   `adm_gates_s[0] >= max(observed_doors_s)`, and mid-walk ceiling skips increment `ceiling_skips`.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import shutil
import subprocess
import time
from types import SimpleNamespace
import wave

import numpy as np
import pytest

from app import worker as worker_mod
from app.config import settings
from app.services import hf_pool_ledger as hf_ledger
from app.services import key_rotation as kr_mod
from app.services import voxcpm_engine as vox_mod
from app.services.recap_engine import core as recap_core
from app.services.recap_engine import voxcpm_engine as recap_vox_mod


def _make_wav(path: Path, *, hz: float = 150.0, duration_s: float = 2.5, sr: int = 24000) -> Path:
    n = max(1, int(round(duration_s * sr)))
    ts = np.arange(n, dtype=np.float64) / sr
    pcm = (0.5 * np.sin(2.0 * np.pi * hz * ts) * 16000.0).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return path


@pytest.fixture(autouse=True)
def _isolated_ledger(monkeypatch):
    kv: dict[str, str] = {}
    monkeypatch.setattr(kr_mod, "botsetting_sync_read", lambda k: kv.get(str(k)))
    monkeypatch.setattr(
        kr_mod,
        "botsetting_sync_write",
        lambda k, v: (kv.__setitem__(str(k), str(v)) is None) or True,
    )
    monkeypatch.setattr(
        kr_mod,
        "botsetting_sync_delete",
        lambda k: (kv.pop(str(k), None) is not None) or True,
    )
    monkeypatch.setattr(hf_ledger, "botsetting_sync_read", lambda k: kv.get(str(k)))
    monkeypatch.setattr(
        hf_ledger,
        "botsetting_sync_write",
        lambda k, v: (kv.__setitem__(str(k), str(v)) is None) or True,
    )
    monkeypatch.setattr(
        hf_ledger,
        "botsetting_sync_delete",
        lambda k: (kv.pop(str(k), None) is not None) or True,
    )
    hf_ledger.reset_ledger_for_tests(clear_store=True)
    vox_mod.VoxCPMEngine._token_cooldowns.clear()
    vox_mod.VoxCPMEngine._token_balances.clear()
    vox_mod.VoxCPMEngine._space_admission_prices.clear()
    vox_mod.VoxCPMEngine._last_admission_price = None
    vox_mod.VoxCPMEngine._token_rr_counter = 0
    vox_mod.VoxCPMEngine._token_bind_rr_counters.clear()
    yield kv
    hf_ledger.reset_ledger_for_tests(clear_store=True)


# ── 1. Part 1 & 2b: 30 s floor reconciliation + 59/75-char pre-walk & mid-walk skips ──


def test_1_short_session_gate_floor_and_38_40s_tokens_across_13_spaces_zero_or_single_refusal(
    tmp_path, monkeypatch
):
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    gen_wav = _make_wav(tmp_path / "ok.wav")

    # Part 1: 0..141 chars -> 45.0 s door; 850 -> 137.0 s; 1250 -> 180.0 s; x1.0 `session_call_gate_s` untouched!
    assert hf_ledger.SESSION_DECLARED_MIN_GPU_S == 30.0
    for short_c in (0, 1, 59, 75, 129, 130, 141):
        assert hf_ledger.session_admission_gate_s(short_c) == 45.0
    assert hf_ledger.session_call_gate_s(59) == 24.0
    assert hf_ledger.session_call_gate_s(75) == 25.0
    assert hf_ledger.session_admission_gate_s(142) == 47.0
    assert hf_ledger.session_admission_gate_s(850) == 137.0
    assert hf_ledger.session_admission_gate_s(1250) == 180.0

    spaces_13 = [f"awints001/voxcpm2-s{i:02d}" for i in range(1, 14)]
    tok_38 = "hf_drained_38s"
    tok_40 = "hf_drained_40s"
    tok_ok = "hf_healthy_300s"
    now = time.time()

    # Case A: Known 38 s and 40 s tokens across 13 Spaces -> skipped pre-walk, 0 refusals, sub_gate_skips >= 2!
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok_38): {"status": "ok", "remaining_s": 38.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(tok_40): {"status": "ok", "remaining_s": 40.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(tok_ok): {"status": "ok", "remaining_s": 300.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
            },
            "space_gates": {sp: 60 for sp in spaces_13},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )

    attempts_a: list[tuple[str, str | None]] = []

    class _ClientA:
        def __init__(self, sp: str, tok: str | None):
            self.sp = sp
            self.tok = tok

        def submit(self, *a, **kw):
            attempts_a.append((self.sp, self.tok))
            if self.tok in (tok_38, tok_40):
                left = 38 if self.tok == tok_38 else 40
                raise RuntimeError(f"You have exceeded your free ZeroGPU quota (45s requested vs. {left}s left)")
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(gen_wav))

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _ClientA(sp, hf_token)),
    )

    for label, n_chars, expected_debit in (("59c", 59, 24.0), ("75c", 75, 25.0)):
        attempts_a.clear()
        eng = vox_mod.VoxCPMEngine(
            work_dir=tmp_path / f"w_{label}",
            spaces=spaces_13,
            hf_tokens=[tok_38, tok_40, tok_ok],
        )
        before_ok = hf_ledger.get_token_remaining_s(tok_ok)
        assert before_ok is not None
        out = eng.synthesize(
            "က" * n_chars,
            tmp_path / f"out_{label}.wav",
            skip_mastering=True,
            allow_fallback=False,
            session_mode=True,
        )
        assert out.is_file()
        assert attempts_a == [(spaces_13[0], tok_ok)]
        assert eng.quota_refusals == 0
        assert eng.sub_gate_skips >= 2
        assert eng.adm_gates_s == [45.0]
        # Post-success debit stays strictly x1.0 (24 s for 59 chars, 25 s for 75 chars — NOT 45 s!)
        assert hf_ledger.get_token_remaining_s(tok_ok) == pytest.approx(before_ok - expected_debit)

    # Case B: Token drifted 60 -> 38 s mid-walk across 13 Spaces -> exactly 1 refusal, remaining 12 Spaces skip!
    tok_drift_60 = "hf_drifted_60_to_38s"
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok_drift_60): {"status": "ok", "remaining_s": 60.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(tok_ok): {"status": "ok", "remaining_s": 300.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
            },
            "space_gates": {sp: 60 for sp in spaces_13},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )

    attempts_b: list[tuple[str, str | None]] = []

    class _ClientB:
        def __init__(self, sp: str, tok: str | None):
            self.sp = sp
            self.tok = tok

        def submit(self, *a, **kw):
            attempts_b.append((self.sp, self.tok))
            if self.tok == tok_drift_60:
                raise RuntimeError("You have exceeded your free ZeroGPU quota (45s requested vs. 38s left)")
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(gen_wav))

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _ClientB(sp, hf_token)),
    )

    eng_b = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w_midwalk",
        spaces=spaces_13,
        hf_tokens=[tok_drift_60, tok_ok],
    )
    out_b = eng_b.synthesize(
        "က" * 59,
        tmp_path / "out_midwalk.wav",
        skip_mastering=True,
        allow_fallback=False,
        session_mode=True,
    )
    assert out_b.is_file()
    # Attempt 1 refused on spaces_13[0]; Spaces 2..13 skipped `tok_drift_60`; Attempt 2 succeeded on `tok_ok`!
    assert attempts_b == [(spaces_13[0], tok_drift_60), (spaces_13[0], tok_ok)]
    assert eng_b.quota_refusals == 1
    assert eng_b.refused_tokens == 1
    assert eng_b.ceiling_skips == 1
    assert eng_b.observed_doors_s == [45.0]
    assert hf_ledger.get_token_remaining_s(tok_drift_60) == pytest.approx(38.0)


# ── 2. Part 3: Unpinned Slice 1 Best-then-Max jump ───────────────────────────


def test_2_unpinned_slice1_15_drifted_tokens_jumps_to_300s_max_fit_on_attempt_3(
    tmp_path, monkeypatch
):
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    gen_wav = _make_wav(tmp_path / "s1_ok.wav")

    spaces_13 = [f"oneclick-org/voxcpm2-s{i:02d}" for i in range(1, 14)]
    drifted_toks = [f"hf_drifted_{i:02d}" for i in range(15)]
    healthy_toks = [f"hf_head300_{i:02d}" for i in range(5)]
    drifted_set = set(drifted_toks)
    healthy_set = set(healthy_toks)

    now = time.time()
    hf_ledger.save_pool_state(
        {
            "tokens": {
                **{
                    hf_ledger.token_digest(t): {"status": "ok", "remaining_s": 60.0 + i, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""}
                    for i, t in enumerate(drifted_toks)
                },
                **{
                    hf_ledger.token_digest(t): {"status": "ok", "remaining_s": 300.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""}
                    for t in healthy_toks
                },
            },
            "space_gates": {sp: 60 for sp in spaces_13},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )

    seen: list[tuple[str, str | None]] = []

    class _Slice1Client:
        def __init__(self, sp: str, tok: str | None):
            self.sp = sp
            self.tok = tok

        def submit(self, *a, **kw):
            seen.append((self.sp, self.tok))
            if self.tok in drifted_set:
                raise RuntimeError("You have exceeded your free ZeroGPU quota (45s requested vs. 35s left)")
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(gen_wav))

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _Slice1Client(sp, hf_token)),
    )

    eng = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w_unpinned_s1",
        spaces=spaces_13,
        hf_tokens=[*drifted_toks, *healthy_toks],
    )
    assert eng.pinned_space is None  # Unpinned Slice 1!

    out = eng.synthesize(
        "က" * 59,
        tmp_path / "slice1.wav",
        skip_mastering=True,
        allow_fallback=False,
        session_mode=True,
    )
    assert out.is_file()
    assert len(seen) == 3
    assert seen[0][1] in drifted_set
    assert seen[1][1] in drifted_set
    assert seen[0][1] != seen[1][1]
    assert seen[2][1] in healthy_set
    assert eng.pinned_space == spaces_13[0]
    assert eng.quota_refusals == 2
    assert eng.refused_tokens == 2
    assert eng.max_fit_jumps == 1


# ── 3. Part 4: Free walk refusal sub-budget vs Paid uncapped ─────────────────


def test_3_free_walk_sub_budget_fails_fast_at_6_refusals_or_20s_wall_while_paid_is_uncapped(
    tmp_path, monkeypatch
):
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    assert vox_mod.FREE_WALK_MAX_QUOTA_REFUSALS == 6
    assert vox_mod.FREE_WALK_MAX_REFUSAL_WALL_S == 20.0

    spaces_4 = [f"awints001/voxcpm2-p{i}" for i in range(1, 5)]
    tokens_12 = [f"hf_drained_pool_{i:02d}" for i in range(12)]
    now = time.time()
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(t): {"status": "ok", "remaining_s": 250.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""}
                for t in tokens_12
            },
            "space_gates": {sp: 60 for sp in spaces_4},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )

    calls: list[tuple[str, str | None]] = []

    class _AllDrainedClient:
        def __init__(self, sp: str, tok: str | None):
            self.sp = sp
            self.tok = tok

        def submit(self, *a, **kw):
            calls.append((self.sp, self.tok))
            raise RuntimeError("You have exceeded your free ZeroGPU quota (45s requested vs. 12s left)")

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _AllDrainedClient(sp, hf_token)),
    )

    # 3A: Free walk stops at <= 6 quota refusals and <= 20 s wall (well under 120 s budget)
    eng_free = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w_free_cap",
        spaces=spaces_4,
        hf_tokens=tokens_12,
    )
    t0 = time.monotonic()
    with pytest.raises(vox_mod.VoxCPMQuotaExhaustedError):
        eng_free.synthesize(
            "က" * 59,
            tmp_path / "free_fail.wav",
            skip_mastering=True,
            allow_fallback=False,
            tier="free",
            session_mode=True,
        )
    elapsed_free = time.monotonic() - t0
    assert len(calls) == vox_mod.FREE_WALK_MAX_QUOTA_REFUSALS == 6
    assert eng_free.quota_refusals <= 6
    assert eng_free.refusal_wall_s <= 20.0
    assert elapsed_free < 15.0  # far under the 120 s Free failover budget

    # 3B: Paid walk in the exact same drained pool is NOT sub-budget-capped (walks all 12 tokens)
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(t): {"status": "ok", "remaining_s": 250.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""}
                for t in tokens_12
            },
            "space_gates": {sp: 60 for sp in spaces_4},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )
    vox_mod.VoxCPMEngine._token_balances.clear()
    calls.clear()
    # Avoid burning wall-clock in unit test on Paid's 12 inter-attempt sleeps while leaving Free's sleep intact above
    monkeypatch.setattr(vox_mod.time, "sleep", lambda _s: None)

    eng_paid = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w_paid_uncapped",
        spaces=spaces_4,
        hf_tokens=tokens_12,
    )
    with pytest.raises(vox_mod.VoxCPMQuotaExhaustedError):
        eng_paid.synthesize(
            "က" * 59,
            tmp_path / "paid_fail.wav",
            skip_mastering=True,
            allow_fallback=False,
            tier="paid",
            session_mode=True,
        )
    assert len(calls) == 12  # > 6: Paid path is NOT sub-budget-capped!
    assert eng_paid.quota_refusals == 12


# ── 4. Part 5 (Wrinkle 5a): `session_space_floors` learning & schema guard ───


def test_4_wrinkle_5a_session_space_floors_isolated_from_space_gates_and_large_slice_safe(
    tmp_path, monkeypatch
):
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    gen_wav = _make_wav(tmp_path / "ok5a.wav")

    sp_rogue = "thirdparty/voxcpm2-highfloor"
    sp_static = "thirdparty/voxcpm2-static120"
    tok_60 = "hf_tok_60s"
    tok_95 = "hf_tok_95s"
    now = time.time()

    # Schema pin: empty `session_space_floors` is omitted from `aj_hf:pool_state`
    base_st = hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok_60): {"status": "ok", "remaining_s": 60.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(tok_95): {"status": "ok", "remaining_s": 95.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
            },
            "space_gates": {sp_rogue: 62, sp_static: 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )
    assert set(base_st.keys()) == {"tokens", "space_gates", "in_flight_gpu_s"}

    # 1. A 138 s refusal on an 850-char slice (formula = 137 s) MUST NOT pollute `session_space_floors` or `space_gates`!
    hf_ledger.record_admission_refusal(
        sp_rogue,
        tok_95,
        138.0,
        95.0,
        update_space_gate=False,
        session_mode=True,
        session_chars=850,
        now_ts=now,
    )
    st_after_850 = hf_ledger.load_pool_state(now_ts=now)
    assert "session_space_floors" not in st_after_850
    assert st_after_850["space_gates"][sp_rogue] == 62
    assert hf_ledger.effective_session_admission_gate_s(59, sp_rogue, state=st_after_850) == 45.0

    # 2. A short-call refusal (`59` chars <= 141) with `requested_s = 90s > 45s` DOES record
    #    `session_space_floors[sp_rogue] == 90` without mutating `space_gates[sp_rogue] == 62`,
    #    and the next short session walk skips `tok_60` (`60 < 90`) pre-walk!
    tried: list[str | None] = []

    class _RogueFloorClient:
        def __init__(self, tok: str | None):
            self.tok = tok

        def submit(self, *a, **kw):
            tried.append(self.tok)
            if self.tok == tok_60:
                raise RuntimeError("You have exceeded your free ZeroGPU quota (90s requested vs. 60s left)")
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(gen_wav))

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _RogueFloorClient(hf_token)),
    )

    eng1 = vox_mod.VoxCPMEngine(work_dir=tmp_path / "w_5a_1", spaces=[sp_rogue], hf_tokens=[tok_60, tok_95])
    eng1.synthesize("က" * 59, tmp_path / "o1.wav", skip_mastering=True, allow_fallback=False, session_mode=True)
    assert tried == [tok_60, tok_95]

    st_after_short = hf_ledger.load_pool_state()
    assert st_after_short["space_gates"][sp_rogue] == 62  # legacy gate untouched!
    assert st_after_short["session_space_floors"][sp_rogue] == 90
    assert hf_ledger.effective_session_admission_gate_s(59, sp_rogue, state=st_after_short) == 90.0

    # Next short session walk on `sp_rogue` prices `known_door_s = 90.0` -> `tok_60` skipped pre-walk!
    # Clear `tok_60`'s refusal ceiling so we isolate Part 5's `session_space_floors` `known_door_s` check:
    st_clear_ceil = hf_ledger.load_pool_state()
    d60 = hf_ledger.token_digest(tok_60)
    st_clear_ceil["tokens"][d60].pop("refused_gate_s", None)
    st_clear_ceil["tokens"][d60].pop("refused_until", None)
    st_clear_ceil["tokens"][d60]["remaining_s"] = 70.0  # 70s >= 45s formula, but < 90s learned floor!
    st_clear_ceil["tokens"][hf_ledger.token_digest(tok_95)]["remaining_s"] = 120.0
    hf_ledger.save_pool_state(st_clear_ceil)

    tried.clear()
    eng2 = vox_mod.VoxCPMEngine(work_dir=tmp_path / "w_5a_2", spaces=[sp_rogue], hf_tokens=[tok_60, tok_95])
    eng2.synthesize("က" * 59, tmp_path / "o2.wav", skip_mastering=True, allow_fallback=False, session_mode=True)
    assert tried == [tok_95]
    assert eng2.sub_gate_skips == 1
    assert eng2.adm_gates_s == [90.0]

    # 3. Static-120s Space (`chars = 300 > 141`, formula = 66 s < 150 s, `requested_s = 180 >= 175`) IS learned!
    hf_ledger.record_admission_refusal(
        sp_static,
        tok_95,
        180.0,
        120.0,
        update_space_gate=False,
        session_mode=True,
        session_chars=300,
        now_ts=now,
    )
    st_static = hf_ledger.load_pool_state(now_ts=now)
    assert st_static["session_space_floors"][sp_static] == 180
    assert st_static["space_gates"][sp_static] == 60


# ── 5. Part 2a (Wrinkle 5b): No baseline ratchet & no TTL re-arm unless tightened ──


def test_5_wrinkle_5b_repeat_refusals_never_raise_baseline_or_extend_ttl_unless_tightened():
    tok = "hf_tok_ratchet_guard"
    d_key = hf_ledger.token_digest(tok)
    t0 = 1_795_000_000.0

    # Initial refusal at t0: 25 s left, 60 s requested -> ceiling = 60.0, baseline = 25.0
    hf_ledger.record_admission_refusal("awints001/voxcpm2-a", tok, 60.0, 25.0, now_ts=t0)
    st1 = hf_ledger.record_refusal_ceiling(tok, 60.0, ttl_seconds=3600.0, now_ts=t0)
    assert st1 is not None
    ru_initial = st1["tokens"][d_key]["refused_until"]
    assert st1["tokens"][d_key]["refused_gate_s"] == 60.0
    assert hf_ledger._refusal_rem_baseline[d_key] == 25.0

    # 10 min later (`t0 + 600`): token trickled up to 38 s left, refused at SAME 60 s (and looser 90 s):
    # - `_refusal_rem_baseline` MUST stay at 25.0 (NEVER overwritten to 38.0!)
    # - `refused_until` MUST stay `ru_initial` (NEVER extended to `t0 + 4200`!)
    hf_ledger.record_admission_refusal("awints001/voxcpm2-b", tok, 60.0, 38.0, now_ts=t0 + 600)
    st2 = hf_ledger.record_refusal_ceiling(tok, 60.0, ttl_seconds=3600.0, now_ts=t0 + 600)
    st3 = hf_ledger.record_refusal_ceiling(tok, 90.0, ttl_seconds=3600.0, now_ts=t0 + 900)
    assert st2 is not None and st3 is not None
    assert hf_ledger._refusal_rem_baseline[d_key] == 25.0
    assert st3["tokens"][d_key]["refused_gate_s"] == 60.0
    assert st3["tokens"][d_key]["refused_until"] == ru_initial

    #Tightening refusal (`45.0 < 60.0`) at `t0 + 1200` DOES tighten `refused_gate_s` and extend `refused_until`,
    # while STILL keeping `_refusal_rem_baseline[d_key] == 25.0`!
    st4 = hf_ledger.record_refusal_ceiling(tok, 45.0, ttl_seconds=3600.0, now_ts=t0 + 1200)
    assert st4 is not None
    assert st4["tokens"][d_key]["refused_gate_s"] == 45.0
    assert st4["tokens"][d_key]["refused_until"] != ru_initial
    assert hf_ledger._refusal_rem_baseline[d_key] == 25.0

    # Because baseline stayed 25.0 (not 38.0), a reconcile probe at 50.0 s (`50 - 25 = 25 >= 25`) clears the ceiling!
    reconciled = hf_ledger.reconcile_from_probe_rows(
        [tok],
        [{"index": 0, "status": "ok", "remaining_s": 50.0, "base_s": 300.0}],
        now_ts=t0 + 1500,
    )
    assert "refused_gate_s" not in reconciled["tokens"][d_key]


# ── 6. Part 6 Telemetry: clone & recap summaries + mid-walk ceiling_skips ────


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_6_telemetry_invariants_in_clone_and_recap_summaries(tmp_path, monkeypatch, caplog):
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    monkeypatch.setattr(settings, "clone_session_slices_enabled", True)
    monkeypatch.setattr(worker_mod, "is_wav_edge_clipped", lambda _p: False)
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **kw: (Path(out).write_bytes(Path(raw).read_bytes()), Path(out))[1],
    )
    gen_wav = _make_wav(tmp_path / "tel.wav", duration_s=2.6)

    spaces = ["awints001/voxcpm2-a", "awints001/voxcpm2-b", "oneclick-org/voxcpm2-a"]
    tok_sub = "hf_tel_sub_38s"
    tok_d1 = "hf_tel_drift1_60s"
    tok_d2 = "hf_tel_drift2_65s"
    tok_ok = "hf_tel_ok_300s"
    now = time.time()

    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok_sub): {"status": "ok", "remaining_s": 38.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(tok_d1): {"status": "ok", "remaining_s": 60.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(tok_d2): {"status": "ok", "remaining_s": 65.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(tok_ok): {"status": "ok", "remaining_s": 300.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
            },
            "space_gates": {sp: 60 for sp in spaces},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )

    class _TelClient:
        def __init__(self, tok: str | None):
            self.tok = tok

        def submit(self, *a, **kw):
            if self.tok in (tok_d1, tok_d2):
                raise RuntimeError("You have exceeded your free ZeroGPU quota (45s requested vs. 39s left)")
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(gen_wav))

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _TelClient(hf_token)),
    )

    eng = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w_tel",
        spaces=spaces,
        hf_tokens=[tok_sub, tok_d1, tok_d2, tok_ok],
    )
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.worker"):
        out = worker_mod._synthesize_voxcpm_voice_job(
            eng,
            job_id="job_tel_1slice",
            text="မင်္ဂလာပါ ခင်ဗျာ ဒီနေ့ စမ်းသပ်မှု ဖြစ်ပါသည်။",
            output_wav=tmp_path / "tel_out.wav",
            reference_wav=None,
            control_instruction="Narrator",
            preset_voice="komin",
            speed="+0%",
            progress_cb=None,
            tier="free",
        )
    assert out.is_file()
    # Key telemetry invariants:
    assert eng.quota_refusals == 2
    assert eng.refused_tokens == 2
    assert eng.quota_refusals == eng.refused_tokens
    assert eng.ceiling_skips == 2  # mid-walk ceiling skips on `tok_d1` and `tok_d2` across spaces[1:]!
    assert eng.sub_gate_skips == len(spaces)  # `tok_sub` (38s < 45s) skipped pre-walk on all 3 Spaces!
    assert eng.max_fit_jumps == 1
    assert eng.adm_gates_s == [45.0]
    assert eng.observed_doors_s == [45.0, 45.0]
    assert all(eng.adm_gates_s[0] >= obs for obs in eng.observed_doors_s)
    assert eng.refusals_by_space == {"awints001/voxcpm2-a": 2}

    clone_lines = [r.getMessage() for r in caplog.records if "clone_prosody_summary" in r.getMessage()]
    assert len(clone_lines) == 1
    assert "quota_refusals=2" in clone_lines[0]
    assert "refused_tokens=2" in clone_lines[0]
    assert "ceiling_skips=2" in clone_lines[0]
    assert "sub_gate_skips=3" in clone_lines[0]
    assert "adm_gates_s=[45.0]" in clone_lines[0]
    assert "observed_doors_s=[45.0, 45.0]" in clone_lines[0]
    assert "refusals_by_space={'awints001/voxcpm2-a': 2}" in clone_lines[0]

    # Verify `recap_voxcpm_tts_summary` also emits all 5 new fields
    src_mp4 = tmp_path / "recap_src.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=duration=2:size=320x180:rate=24",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(src_mp4),
        ],
        check=True,
    )
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(
        json.dumps(
            {"segments": [{"segment_id": 1, "source_index": 0, "video_start": 0.0, "video_end": 2.0, "narration": "စမ်းသပ် စာသား။"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok_sub): {"status": "ok", "remaining_s": 38.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(tok_d1): {"status": "ok", "remaining_s": 70.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(tok_ok): {"status": "ok", "remaining_s": 300.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
            },
            "space_gates": {sp: 60 for sp in spaces[:2]},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )
    vox_mod.VoxCPMEngine._token_balances.clear()
    monkeypatch.setenv("VOXCPM_SPACES", ",".join(spaces[:2]))
    monkeypatch.setenv("HF_TOKENS", ",".join([tok_sub, tok_d1, tok_ok]))

    class _RecapTelClient:
        def __init__(self, tok: str | None):
            self.tok = tok

        def submit(self, *a, **kw):
            if self.tok == tok_d1:
                raise RuntimeError("You have exceeded your free ZeroGPU quota (60s requested vs. 42s left)")
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(gen_wav))

    monkeypatch.setattr(
        recap_vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _RecapTelClient(hf_token)),
    )
    recap_logs: list[str] = []
    sync = recap_core.EdgeSmartSync(
        job_dir=str(tmp_path / "recap_tel"),
        source_path=str(src_mp4),
        plan_path=str(plan_file),
        voice="voxcpm_komin",
        overlays={"tts_engine": "voxcpm"},
        paid_tier=True,
        render_final_video=False,
        tts_workers=1,
        log=recap_logs.append,
    )
    sync.run()
    recap_summaries = [ln for ln in recap_logs if "recap_voxcpm_tts_summary" in ln]
    assert len(recap_summaries) == 1
    assert "quota_refusals=1" in recap_summaries[0]
    assert "refused_tokens=1" in recap_summaries[0]
    assert "ceiling_skips=1" in recap_summaries[0]
    assert "sub_gate_skips=2" in recap_summaries[0]
    assert "adm_gates_s=[60.0]" in recap_summaries[0]
    assert "observed_doors_s=[60.0]" in recap_summaries[0]
    assert "refusals_by_space=" in recap_summaries[0]

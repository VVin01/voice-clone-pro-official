"""Unit tests for HF Ledger Self-Correction Bundle (P1+P2+P3 + Q4 Telemetry, 2026-11-26).

Covers all owner/Hammock-specified pins:
- PART P2:
  * `SESSION_ADMISSION_GATE_MULTIPLIER == 1.5`, `session_admission_gate_s(chars)` capped at 180.0 s.
  * 135 s token skipped for 849-char session slice (door gate 137 s) while 150 s and 300 s tokens
    are selected; post-success debit still deducts the x1.0 `session_call_gate_s` (91 s), NOT 137 s,
    and `estimate_voxcpm_units_and_budget` keeps x1.0 reservation math.
  * Two engines with 141 s / 148 s tokens distribute via 20 s Tier-0 buckets + round-robin instead
    of colliding on the lower float.
- PART P1:
  * Token refused at a session gate gets additive `refused_gate_s` + `refused_until` (without
    hard-zeroing `remaining_s`), is skipped by the next session walk even after `reconcile_from_probe_rows`
    reports the same balance, yet remains usable for a 60 s legacy `/generate` call.
  * Second refusal at 60 s drops `refused_gate_s` to 60 s and blocks all walks (legacy & session).
  * Stored `0.0` balance survives a `rem_val=None` probe error without resurrecting to 300 s.
  * Unprobed Space's legacy success debits `DEFAULT_SPACE_GATE_S` (60.0 s) instead of 0.0 s.
  * Rolling-window `resets_at <= now` in `_normalize_token_row` does NOT snap `remaining_s` to `base_s`.
  * Refusal ceiling clears when probed balance rises >= 25 s over refusal-time balance.
- PART P3:
  * Reproduces Job `884c29c8` shape (15 phantom 140 s tokens + 10 healthy 300 s tokens on pinned Space):
    Slice 2 tries 2 Best-Fit tokens, jumps to Max-Fit 300 s token on Attempt 3 on the SAME pinned Space,
    and finishes with `session_fallback_used=False`.
  * Re-pin candidate tokens are strictly disjoint from the pinned Space's queued tokens; Space 5xx errors
    still enforce the strict `MAX_PINNED_SPACE_TOKENS` cap before jumping to `repin_candidate`.
- PART Q4 + Schema Pin:
  * Untouched pool state keeps exact base keys `{"tokens", "space_gates", "in_flight_gpu_s"}` and
    untouched token rows keep exact 6-key shape; `refused_gate_s`/`refused_until` appear ONLY on tokens
    that earned them.
  * `clone_prosody_summary` and `recap_voxcpm_tts_summary` emit `quota_refusals`, `refusal_wall_s`,
    `ceiling_skips`, and `max_fit_jumps`.
  * `reconcile_from_probe_rows` logs `hf_pool_reconcile_summary` and persists `drift_stats`.
  * `/api/admin/key-health` (fresh + cached) overlays `ledger_effective_s`, `session_ready_tokens`,
    `refusal_capped_tokens`, `quota_probe_errors`, `last_drift_s`, and per-account `ledger_remaining_s`
    + `Gate-capped (<Xs)` UI wiring in `admin.js`.
"""
from __future__ import annotations

from datetime import datetime, timezone
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
from app.services import key_health as kh_mod
from app.services import key_rotation as kr_mod
from app.services import voxcpm_engine as vox_mod
from app.services.recap_engine import core as recap_core
from app.services.recap_engine import voxcpm_engine as recap_vox_mod

ROOT = Path(__file__).resolve().parents[2]
_GENERIC_ZEROGPU_REFUSAL = (
    "AppError: You have exceeded your ZeroGPU runs limit. "
    "Subscribe to Hugging Face PRO to get 40 min of ZeroGPU quota a day"
)


def _make_wav(path: Path, *, hz: float = 145.0, duration_s: float = 2.4, sr: int = 24000) -> Path:
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
def _isolated_ledger_env(monkeypatch):
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
    kh_mod._reset_cache_for_tests()
    vox_mod.VoxCPMEngine._token_cooldowns.clear()
    vox_mod.VoxCPMEngine._token_balances.clear()
    vox_mod.VoxCPMEngine._space_admission_prices.clear()
    vox_mod.VoxCPMEngine._last_admission_price = None
    vox_mod.VoxCPMEngine._token_rr_counter = 0
    vox_mod.VoxCPMEngine._token_bind_rr_counters.clear()
    yield kv
    hf_ledger.reset_ledger_for_tests(clear_store=True)
    kh_mod._reset_cache_for_tests()


def test_p2_session_door_gate_1_5x_preserves_1_0x_debit_and_20s_buckets_spread_workers(
    tmp_path, monkeypatch
):
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    gen_wav = _make_wav(tmp_path / "gen.wav")

    # 1. Exact constants & cap at 180.0 s (<= 300 s all-free account base)
    assert hf_ledger.SESSION_ADMISSION_GATE_MULTIPLIER == 1.5
    assert hf_ledger.session_call_gate_s(849) == 91.0
    assert hf_ledger.session_admission_gate_s(849) == 137.0
    assert hf_ledger.session_call_gate_s(1250) == 120.0
    assert hf_ledger.session_admission_gate_s(1250) == 180.0

    # Pre-job reservation math keeps x1.0 `session_call_gate_s` (91.0 s), never inflated to 137.0 s
    monkeypatch.setattr(settings, "clone_session_slices_enabled", True)
    text_849 = "က" * 849
    units, est_gate, needed_s = hf_ledger.estimate_voxcpm_units_and_budget(kind="clone", text=text_849)
    assert (units, est_gate, needed_s) == (1, 91.0, round(91.0 * hf_ledger.PRE_JOB_SAFETY_MULTIPLIER, 2))

    # 2. 135 s token (< 137 s door gate) is SKIPPED; 150 s & 300 s tokens are selected;
    #    post-success debit deducts 91 s (x1.0), leaving 150 - 91 = 59 s!
    tok_135 = "hf_tok_135s"
    tok_150 = "hf_tok_150s"
    tok_300 = "hf_tok_300s"
    now = time.time()
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok_135): {"status": "ok", "remaining_s": 135.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(tok_150): {"status": "ok", "remaining_s": 150.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(tok_300): {"status": "ok", "remaining_s": 300.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
            },
            "space_gates": {"awints001/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )

    # `has_solvent_voxcpm_tokens` also respects the 137 s session admission gate
    only_135_eng = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "only135",
        spaces=["awints001/voxcpm2-a"],
        hf_tokens=[tok_135],
    )
    assert vox_mod.has_solvent_voxcpm_tokens(only_135_eng, gate_s=hf_ledger.session_admission_gate_s(849)) is False
    assert vox_mod.has_solvent_voxcpm_tokens(only_135_eng, gate_s=60.0) is True

    attempted: list[str | None] = []

    class _FirstFailThenOkClient:
        def __init__(self, tok: str | None):
            self.tok = tok

        def submit(self, *a, **kw):
            attempted.append(self.tok)
            if self.tok == tok_150 and len(attempted) == 1:
                return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(gen_wav))
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(gen_wav))

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _FirstFailThenOkClient(hf_token)),
    )

    eng = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w_p2",
        spaces=["awints001/voxcpm2-a"],
        hf_tokens=[tok_135, tok_300, tok_150],
    )
    out = eng.synthesize(
        text_849,
        tmp_path / "p2_out.wav",
        skip_mastering=True,
        allow_fallback=False,
        session_mode=True,
    )
    assert out.is_file()
    assert tok_135 not in attempted
    assert attempted == [tok_150]
    # Debited by 91.0 s (x1.0), NOT 137.0 s -> 150.0 - 91.0 = 59.0 s!
    assert hf_ledger.get_token_remaining_s(tok_150) == pytest.approx(59.0)

    # Next 849-char session call selects `tok_300` (now that `tok_150` is at 59.0 s < 137.0 s)
    attempted.clear()
    eng.synthesize(
        text_849,
        tmp_path / "p2_out2.wav",
        skip_mastering=True,
        allow_fallback=False,
        session_mode=True,
    )
    assert attempted == [tok_300]
    assert hf_ledger.get_token_remaining_s(tok_300) == pytest.approx(209.0)

    # 3. 20 s Tier-0 buckets (`141 s` & `148 s` both in `[140, 160)` bucket -> `(0, 140.0)`)
    #    distribute two engines across distinct tokens via round-robin instead of colliding!
    tok_141 = "hf_tok_141s"
    tok_148 = "hf_tok_148s"
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok_141): {"status": "ok", "remaining_s": 141.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(tok_148): {"status": "ok", "remaining_s": 148.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
            },
            "space_gates": {"awints001/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )
    st_b = hf_ledger.load_pool_state()
    assert vox_mod.VoxCPMEngine._balance_sort_key(tok_141, gate=60.0, ledger_state=st_b) == (0, 140.0)
    assert vox_mod.VoxCPMEngine._balance_sort_key(tok_148, gate=60.0, ledger_state=st_b) == (0, 140.0)

    vox_mod.VoxCPMEngine._token_rr_counter = 0
    picks: list[str | None] = []

    class _NoDebitCaptureClient:
        def __init__(self, tok):
            self.tok = tok

        def submit(self, *a, **kw):
            picks.append(self.tok)
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(gen_wav))

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _NoDebitCaptureClient(hf_token)),
    )
    # Freeze debit between the two simultaneous starts to simulate concurrent workers starting before either finishes
    monkeypatch.setattr(vox_mod.VoxCPMEngine, "_record_success_balance", classmethod(lambda *a, **kw: None))

    e1 = vox_mod.VoxCPMEngine(work_dir=tmp_path / "e1", spaces=["awints001/voxcpm2-a"], hf_tokens=[tok_141, tok_148])
    e2 = vox_mod.VoxCPMEngine(work_dir=tmp_path / "e2", spaces=["awints001/voxcpm2-a"], hf_tokens=[tok_141, tok_148])
    e1.synthesize("စာကြောင်း ၁။", tmp_path / "b1.wav", skip_mastering=True, allow_fallback=False)
    e2.synthesize("စာကြောင်း ၂။", tmp_path / "b2.wav", skip_mastering=True, allow_fallback=False)
    assert set(picks) == {tok_141, tok_148}
    assert picks[0] != picks[1]


def test_p1_scoped_refusal_ceiling_reconcile_survival_refill_clear_and_ledger_bug_fixes(
    tmp_path, monkeypatch, _isolated_ledger_env
):
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    gen_wav = _make_wav(tmp_path / "ok.wav")

    tok_drift = "hf_tok_drifted_160"
    tok_healthy = "hf_tok_healthy_300"
    tok_zero = "hf_tok_zero_0"
    d_drift = hf_ledger.token_digest(tok_drift)
    d_healthy = hf_ledger.token_digest(tok_healthy)
    d_zero = hf_ledger.token_digest(tok_zero)

    now = time.time()
    past_reset_iso = datetime.fromtimestamp(now - 600, tz=timezone.utc).isoformat()
    initial = hf_ledger.save_pool_state(
        {
            "tokens": {
                d_drift: {"status": "ok", "remaining_s": 160.0, "base_s": 300.0, "resets_at": past_reset_iso, "cooldown_until": "", "updated_at": ""},
                d_healthy: {"status": "ok", "remaining_s": 300.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                d_zero: {"status": "ok", "remaining_s": 0.0, "base_s": 300.0, "resets_at": past_reset_iso, "cooldown_until": "", "updated_at": ""},
            },
            "space_gates": {},  # empty -> unprobed Space!
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )

    # Schema pin + P1.5c: untouched pool state keeps exact 3 base keys, untouched token rows keep
    # exact 6 keys, AND past `resets_at <= now` did NOT snap `160.0` or `0.0` back to `300.0`!
    assert set(initial.keys()) == {"tokens", "space_gates", "in_flight_gpu_s"}
    expected_6_keys = {"status", "remaining_s", "base_s", "resets_at", "cooldown_until", "updated_at"}
    for row in initial["tokens"].values():
        assert set(row.keys()) == expected_6_keys
    assert initial["tokens"][d_drift]["remaining_s"] == 160.0
    assert initial["tokens"][d_zero]["remaining_s"] == 0.0

    # Walk 1 (session 849 chars, door gate = 137 s): `tok_drift` (160s) is tried first and refused
    # with generic ZeroGPU runs-limit error -> `tok_healthy` (300s) succeeds!
    tried: list[tuple[str | None, str]] = []
    refuse_drift_on_legacy = {"flag": False}

    class _RefusalClient:
        def __init__(self, tok: str | None):
            self.tok = tok

        def submit(self, *a, **kw):
            api = kw.get("api_name", "/generate")
            tried.append((self.tok, api))
            if self.tok == tok_drift and (api == "/generate_session" or refuse_drift_on_legacy["flag"]):
                raise RuntimeError(_GENERIC_ZEROGPU_REFUSAL)
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(gen_wav))

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _RefusalClient(hf_token)),
    )

    eng1 = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w1",
        spaces=["awints001/voxcpm2-unprobed"],
        hf_tokens=[tok_drift, tok_healthy, tok_zero],
    )
    eng1.synthesize(
        "က" * 849,
        tmp_path / "s1.wav",
        skip_mastering=True,
        allow_fallback=False,
        session_mode=True,
    )
    assert tried == [(tok_drift, "/generate_session"), (tok_healthy, "/generate_session")]

    # `tok_drift` earned `refused_gate_s == 137.0` and `refused_until` WITHOUT hard-zeroing `remaining_s` (still 160.0)!
    # Untouched `tok_healthy` & `tok_zero` STILL have the exact 6-key shape!
    st_after_refusal = hf_ledger.load_pool_state()
    assert st_after_refusal["tokens"][d_drift]["remaining_s"] == 160.0
    assert st_after_refusal["tokens"][d_drift]["refused_gate_s"] == 137.0
    assert "refused_until" in st_after_refusal["tokens"][d_drift]
    assert set(st_after_refusal["tokens"][d_healthy].keys()) == expected_6_keys
    assert set(st_after_refusal["tokens"][d_zero].keys()) == expected_6_keys

    # Reconcile reports the SAME 160.0 s balance on `tok_drift`, and a `rem_val=None` probe error on `tok_zero`:
    # - `tok_drift` keeps its 137.0 s refusal ceiling!
    # - `tok_zero` stays at `0.0 s` (P1.5a: does NOT resurrect to 300.0 s)!
    hf_ledger.reconcile_from_probe_rows(
        [tok_drift, tok_healthy, tok_zero],
        [
            {"index": 0, "status": "ok", "remaining_s": 160.0, "base_s": 300.0},
            {"index": 1, "status": "ok", "remaining_s": 290.0, "base_s": 300.0},
            {"index": 2, "status": "ok", "remaining_s": None, "base_s": None, "quota_error": "HfHubHTTPError"},
        ],
    )
    assert hf_ledger.get_token_remaining_s(tok_zero) == 0.0
    assert hf_ledger.get_token_refused_gate_s(tok_drift) == 137.0

    # Walk 2 (next session walk, door gate 137 s): `tok_drift` is SKIPPED via ceiling (`ceiling_skips == 1`)!
    tried.clear()
    eng2 = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w2",
        spaces=["awints001/voxcpm2-unprobed"],
        hf_tokens=[tok_drift, tok_healthy, tok_zero],
    )
    eng2.synthesize(
        "က" * 849,
        tmp_path / "s2.wav",
        skip_mastering=True,
        allow_fallback=False,
        session_mode=True,
    )
    assert tried == [(tok_healthy, "/generate_session")]
    assert eng2.ceiling_skips == 1

    # Walk 3 (60 s legacy `/generate` call on unprobed Space):
    # - `tok_drift` (160 s, ceiling 137 s > 60 s) REMAINS USABLE and is picked first!
    # - P1.5b: unprobed Space legacy success debits `DEFAULT_SPACE_GATE_S` (60.0 s): 160.0 -> 100.0 s!
    tried.clear()
    eng3 = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w3",
        spaces=["awints001/voxcpm2-unprobed"],
        hf_tokens=[tok_drift, tok_healthy, tok_zero],
    )
    eng3.synthesize(
        "တိုတို စာကြောင်း။",
        tmp_path / "leg1.wav",
        skip_mastering=True,
        allow_fallback=False,
        session_mode=False,
    )
    assert tried == [(tok_drift, "/generate")]
    assert hf_ledger.get_token_remaining_s(tok_drift) == pytest.approx(100.0)
    assert hf_ledger.get_token_refused_gate_s(tok_drift) == 137.0

    # Walk 4: a SECOND refusal on `tok_drift` at 60 s legacy gate drops `refused_gate_s` to 60.0 s
    # (still without hard-zeroing `remaining_s` == 100.0 s!) and blocks ALL future walks (legacy & session)!
    refuse_drift_on_legacy["flag"] = True
    tried.clear()
    eng4 = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w4",
        spaces=["awints001/voxcpm2-unprobed"],
        hf_tokens=[tok_drift, tok_healthy],
    )
    eng4.synthesize(
        "တိုတို စာကြောင်း ၂။",
        tmp_path / "leg2.wav",
        skip_mastering=True,
        allow_fallback=False,
        session_mode=False,
    )
    assert tried == [(tok_drift, "/generate"), (tok_healthy, "/generate")]
    assert hf_ledger.get_token_remaining_s(tok_drift) == pytest.approx(100.0)
    assert hf_ledger.get_token_refused_gate_s(tok_drift) == 60.0

    # Now `tok_drift` is blocked for BOTH 60 s legacy walks AND session walks!
    tried.clear()
    eng_only_drift = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w_blocked",
        spaces=["awints001/voxcpm2-unprobed"],
        hf_tokens=[tok_drift],
    )
    with pytest.raises(vox_mod.VoxCPMQuotaExhaustedError):
        eng_only_drift.synthesize("တိုတို။", tmp_path / "fail_leg.wav", allow_fallback=False, session_mode=False)
    with pytest.raises(vox_mod.VoxCPMQuotaExhaustedError):
        eng_only_drift.synthesize("က" * 300, tmp_path / "fail_sess.wav", allow_fallback=False, session_mode=True)
    assert tried == []  # 0 network calls!

    # P1.4: genuine rolling refill (probed balance rises >= 25 s over refusal-time balance: 100 -> 130 s) clears ceiling!
    hf_ledger.reconcile_from_probe_rows(
        [tok_drift, tok_healthy, tok_zero],
        [
            {"index": 0, "status": "ok", "remaining_s": 130.0, "base_s": 300.0},
            {"index": 1, "status": "ok", "remaining_s": 149.0, "base_s": 300.0},
            {"index": 2, "status": "ok", "remaining_s": 0.0, "base_s": 300.0},
        ],
    )
    assert hf_ledger.get_token_refused_gate_s(tok_drift) is None
    assert set(hf_ledger.load_pool_state()["tokens"][d_drift].keys()) == expected_6_keys


def test_p3_job_884c29c8_shape_slice2_succeeds_on_attempt3_same_pinned_space_and_disjoint_repin(
    tmp_path, monkeypatch, caplog
):
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    monkeypatch.setattr(settings, "clone_session_slices_enabled", True)
    monkeypatch.setattr(worker_mod, "is_wav_edge_clipped", lambda _p: False)
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **kw: (Path(out).write_bytes(Path(raw).read_bytes()), Path(out))[1],
    )
    gen_wav = _make_wav(tmp_path / "slice.wav", duration_s=3.0)

    # Reproduce Job `884c29c8` pool shape:
    # - `tok_s1_pin` (115 s): solvent for ~520-char Slice 1 (door gate ~95 s), pins `oneclick-org/voxcpm2-a`,
    #   and drains to ~52 s after Slice 1 so Slice 2 (~840 chars, door gate 135 s) faces all 15 untouched
    #   phantom 140 s tokens + 10 healthy 300 s tokens on the pinned Space!
    tok_s1_pin = "hf_s1_pinner_115s"
    phantom_toks = [f"hf_phantom_{i:02d}" for i in range(15)]
    healthy_toks = [f"hf_healthy_{i:02d}" for i in range(10)]
    all_toks = [tok_s1_pin, *phantom_toks, *healthy_toks]
    phantom_set = set(phantom_toks)

    now = time.time()
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok_s1_pin): {"status": "ok", "remaining_s": 115.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                **{
                    hf_ledger.token_digest(t): {"status": "ok", "remaining_s": 140.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""}
                    for t in phantom_toks
                },
                **{
                    hf_ledger.token_digest(t): {"status": "ok", "remaining_s": 300.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""}
                    for t in healthy_toks
                },
            },
            "space_gates": {"oneclick-org/voxcpm2-a": 60, "awints001/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )

    slice_calls: dict[int, list[tuple[str, str | None, str]]] = {}
    cur_call_idx = {"n": 0}

    class _Job884Client:
        def __init__(self, sp: str, tok: str | None):
            self.sp = sp
            self.tok = tok

        def submit(self, *a, **kw):
            idx = cur_call_idx["n"]
            slice_calls.setdefault(idx, []).append((self.sp, self.tok, kw.get("api_name", "/generate")))
            if self.tok in phantom_set:
                raise RuntimeError(_GENERIC_ZEROGPU_REFUSAL)
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(gen_wav))

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _Job884Client(sp, hf_token)),
    )

    # Plan exact 2 session slices: Slice 1 = 520 chars (door gate 95s -> `tok_s1_pin` 115s pins `oneclick-org/voxcpm2-a`),
    # Slice 2 = 840 chars (door gate 135s -> faces all 15 phantom 140s tokens + 10 healthy 300s tokens!)
    s1_txt = ("က" * 519) + "။"
    s2_txt = ("ခ" * 839) + "။"
    full_text = f"{s1_txt} {s2_txt}"
    monkeypatch.setattr(worker_mod, "plan_session_slices", lambda _t: [s1_txt, s2_txt])

    orig_synth = vox_mod.VoxCPMEngine.synthesize

    def _counted_synth(self_eng, *a, **kw):
        cur_call_idx["n"] += 1
        return orig_synth(self_eng, *a, **kw)

    monkeypatch.setattr(vox_mod.VoxCPMEngine, "synthesize", _counted_synth)

    eng = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w_884",
        spaces=["oneclick-org/voxcpm2-a", "awints001/voxcpm2-a"],
        hf_tokens=all_toks,
    )
    with caplog.at_level(logging.INFO, logger="app.worker"):
        out = worker_mod._synthesize_voxcpm_voice_job(
            eng,
            job_id="job_884c29c8_repro",
            text=full_text,
            output_wav=tmp_path / "out_884.wav",
            reference_wav=None,
            control_instruction="Narrator",
            preset_voice="komin",
            speed="+0%",
            progress_cb=None,
            tier="paid",
        )
    assert out.is_file()
    assert cur_call_idx["n"] == 2  # Pure 2 session slices, 0 legacy fallback chunks!

    # Slice 2 (pinned to `oneclick-org/voxcpm2-a`):
    # Attempt 1 & 2 try 2 Best-Fit 140 s tokens (refused), Attempt 3 jumps to a Max-Fit 300 s token
    # on the EXACT SAME pinned Space (`oneclick-org/voxcpm2-a`) and SUCCEEDS!
    s2_attempts = slice_calls[2]
    assert len(s2_attempts) == 3
    assert [sp for sp, _t, _api in s2_attempts] == [
        "oneclick-org/voxcpm2-a",
        "oneclick-org/voxcpm2-a",
        "oneclick-org/voxcpm2-a",
    ]
    assert all(api == "/generate_session" for _sp, _t, api in s2_attempts)
    assert s2_attempts[0][1] in phantom_set
    assert s2_attempts[1][1] in phantom_set
    assert s2_attempts[2][1] in set(healthy_toks)
    assert eng.pinned_space == "oneclick-org/voxcpm2-a"
    assert eng.max_fit_jumps >= 1

    summaries = [r.getMessage() for r in caplog.records if "clone_prosody_summary" in r.getMessage()]
    assert len(summaries) == 1
    assert "session_fallback_used=False" in summaries[0]
    assert "max_fit_jumps=1" in summaries[0]

    # Disjoint re-pin verification + strict `MAX_PINNED_SPACE_TOKENS` cap on non-quota Space 5xx errors:
    # When `cur_pin` fails with 5xx (non-quota), it stops after 6 attempts and `repin_candidate` uses
    # tokens completely DISJOINT from the tokens tried on `cur_pin`!
    seen_5xx: list[tuple[str, str | None]] = []

    class _Pin5xxClient:
        def __init__(self, sp: str, tok: str | None):
            self.sp = sp
            self.tok = tok

        def submit(self, *a, **kw):
            seen_5xx.append((self.sp, self.tok))
            raise RuntimeError("HTTP 502 Bad Gateway")

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _Pin5xxClient(sp, hf_token)),
    )
    eng_disjoint = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w_disjoint",
        spaces=["oneclick-org/voxcpm2-a", "awints001/voxcpm2-a"],
        hf_tokens=healthy_toks,  # 10 clean 300 s tokens
    )
    eng_disjoint.pinned_space = "oneclick-org/voxcpm2-a"
    with pytest.raises(RuntimeError):
        eng_disjoint.synthesize(
            "စမ်းသပ် စာကြောင်း။",
            tmp_path / "disjoint.wav",
            skip_mastering=True,
            allow_fallback=False,
        )
    pin_toks_tried = [tok for sp, tok in seen_5xx if sp == "oneclick-org/voxcpm2-a"]
    repin_toks_tried = [tok for sp, tok in seen_5xx if sp == "awints001/voxcpm2-a"]
    assert len(pin_toks_tried) == vox_mod.MAX_PINNED_SPACE_TOKENS == 6
    assert len(repin_toks_tried) == vox_mod.MAX_REPIN_SPACE_TOKENS == 3
    assert set(pin_toks_tried).isdisjoint(set(repin_toks_tried))


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_q4_telemetry_drift_stats_recap_summary_and_admin_key_health_overlay(
    tmp_path, monkeypatch, caplog
):
    t0 = "hf_q4_tok0"
    t1 = "hf_q4_tok1"
    t2 = "hf_q4_tok2"
    t3 = "hf_q4_tok3"
    tokens = [t0, t1, t2, t3]
    now = time.time()

    # Seed ledger with drifted estimates before 15-min reconcile runs:
    # - t0: ledger=220s vs probed=150s -> +70s drift (>25s overestimated!)
    # - t1: ledger=160s vs probed=150s (and earns a 155s session refusal ceiling!) -> +10s drift
    # - t2: ledger=300s vs probed=300s -> 0s drift (session-ready >=145s & uncapped!)
    # - t3: ledger=90s  vs probed=None (whoami=200, quota_error="HfHubHTTPError") -> probe error!
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(t0): {"status": "ok", "remaining_s": 220.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(t1): {"status": "ok", "remaining_s": 160.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(t2): {"status": "ok", "remaining_s": 300.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(t3): {"status": "ok", "remaining_s": 90.0, "base_s": 300.0, "resets_at": "", "cooldown_until": "", "updated_at": ""},
            },
            "space_gates": {"awints001/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )
    hf_ledger.record_refusal_ceiling(t1, 155.0, now_ts=now)

    monkeypatch.setattr(kh_mod, "_load_pools", lambda: (tokens, [], [], ["awints001/voxcpm2-a"]))

    probe_profiles = {
        0: {"index": 0, "status": "ok", "account": "acct0", "remaining_s": 150.0, "base_s": 300.0, "quota_error": None},
        1: {"index": 1, "status": "ok", "account": "acct1", "remaining_s": 150.0, "base_s": 300.0, "quota_error": None},
        2: {"index": 2, "status": "ok", "account": "acct2", "remaining_s": 300.0, "base_s": 300.0, "quota_error": None},
        3: {"index": 3, "status": "ok", "account": "acct3", "remaining_s": None, "base_s": None, "quota_error": "HfHubHTTPError"},
    }
    monkeypatch.setattr(kh_mod, "_probe_hf_account", lambda idx, _tok: dict(probe_profiles[idx]))
    monkeypatch.setattr(kh_mod, "_probe_space", lambda name: {"name": name, "stage": "RUNNING"})

    with caplog.at_level(logging.INFO, logger="app.services.hf_pool_ledger"):
        fresh = kh_mod.get_key_health(force_refresh=True)

    # 1. Reconciler logged summary + persisted `drift_stats`
    rec_logs = [r.getMessage() for r in caplog.records if "hf_pool_reconcile_summary" in r.getMessage()]
    assert len(rec_logs) == 1
    assert "positive_drift_s=80.0" in rec_logs[0]
    assert "overestimated_tokens=1" in rec_logs[0]
    assert "max_token_drift_s=70.0" in rec_logs[0]
    assert "quota_probe_errors=1" in rec_logs[0]

    ds = hf_ledger.get_drift_stats()
    assert ds == {
        "positive_drift_s": 80.0,
        "overestimated_tokens": 1,
        "max_token_drift_s": 70.0,
        "quota_probe_errors": 1,
    }

    # 2. Fresh Key Health response carries HF overlay metrics
    hf_p = fresh["providers"]["hf"]
    # t0 (150 >= 145, uncapped) + t2 (300 >= 145, uncapped) = 2 session_ready_tokens (t1 is refusal-capped; t3 is 90s)
    assert hf_p["session_ready_tokens"] == 2
    assert hf_p["refusal_capped_tokens"] == 1
    assert hf_p["quota_probe_errors"] == 1
    assert hf_p["last_drift_s"] == 80.0
    assert hf_p["accounts"][1]["refused_gate_s"] == 155.0

    # Simulate post-probe debit on `t0` (150 -> 90s, diverged by 60s > 15s from probed 150s) + new refusal on `t2`:
    # Cached `get_key_health(force_refresh=False)` MUST reflect live ledger changes without re-probing!
    hf_ledger.record_success_debit("awints001/voxcpm2-a", t0, debit_s=60.0)
    hf_ledger.record_refusal_ceiling(t2, 165.0)
    cached = kh_mod.get_key_health(force_refresh=False)
    assert cached["cached"] is True
    hf_c = cached["providers"]["hf"]
    assert hf_c["refusal_capped_tokens"] == 2
    assert hf_c["session_ready_tokens"] == 0  # t0 now 90s, t1 & t2 capped!
    assert hf_c["accounts"][0]["remaining_s"] == 150.0
    assert hf_c["accounts"][0]["ledger_remaining_s"] == 90.0
    assert hf_c["accounts"][2]["refused_gate_s"] == 165.0

    # 3. Frontend `admin.js` display wiring check (`Gate-capped (<Xs)` + diverged >15s ledger_remaining_s)
    admin_js = (ROOT / "app/webapp/static/js/admin.js").read_text(encoding="utf-8")
    assert "Gate-capped (<" in admin_js
    assert "ledger_remaining_s" in admin_js
    assert "session_ready_tokens" in admin_js
    assert "> 15" in admin_js

    # 4. Recap TTS stage summary emits `quota_refusals`, `refusal_wall_s`, `ceiling_skips`, `max_fit_jumps`
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    gen_wav = _make_wav(tmp_path / "recap_seg.wav", duration_s=1.0)
    src = tmp_path / "src.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=duration=2:size=320x180:rate=24",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(src),
        ],
        check=True,
    )
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(
        json.dumps({"segments": [{"segment_id": 1, "source_index": 0, "video_start": 0.0, "video_end": 2.0, "narration": "စမ်းသပ် စာသား။"}]}, ensure_ascii=False),
        encoding="utf-8",
    )

    recap_calls = {"n": 0}

    class _OneRefusalThenOkClient:
        def submit(self, *a, **kw):
            recap_calls["n"] += 1
            if recap_calls["n"] == 1:
                raise RuntimeError(_GENERIC_ZEROGPU_REFUSAL)
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(gen_wav))

    monkeypatch.setattr(
        recap_vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _OneRefusalThenOkClient()),
    )
    monkeypatch.setenv("VOXCPM_SPACES", "awints001/voxcpm2-a")
    monkeypatch.setenv("HF_TOKENS", f"{t0},{t2}")

    recap_logs: list[str] = []
    sync = recap_core.EdgeSmartSync(
        job_dir=str(tmp_path / "recap_q4"),
        source_path=str(src),
        plan_path=str(plan_file),
        voice="voxcpm_komin",
        overlays={"tts_engine": "voxcpm"},
        paid_tier=True,
        render_final_video=False,
        tts_workers=1,
        log=recap_logs.append,
    )
    sync.run()
    tts_summaries = [ln for ln in recap_logs if "recap_voxcpm_tts_summary" in ln]
    assert len(tts_summaries) == 1
    assert "quota_refusals=1" in tts_summaries[0]
    assert "refusal_wall_s=" in tts_summaries[0]
    assert "ceiling_skips=" in tts_summaries[0]
    assert "max_fit_jumps=" in tts_summaries[0]

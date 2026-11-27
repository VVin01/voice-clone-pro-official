"""Unit tests for Session-Slice Clone Synthesis (Engine Round, 2026-11-26).

Covers Parts 1–9:
- Part 1: `clone_session_slices_enabled` dynamic setting (bool, default FALSE, DB reloadable).
- Part 2: `plan_session_slices` sentence-boundary packing (~850 target, 1,050 hard cap,
          <200-char tail merge up to 1,250 hard cap, legacy `plan_text_chunks` untouched).
- Part 3 & 4: Flag OFF = byte-for-byte legacy path; Flag ON = standalone VOICE_CLONE (`token_bind_index=None`)
              calls `/generate_session` with exact `/generate` parameters, Space pinning + 1 re-pin;
              recap and bound engines untouched.
- Part 5 & 8: Per-slice fallback to <=385-char `/generate` chunks across any solvent Space +
              prosody-skip rule (no fallback -> Phase 2 prosody pass skipped; any fallback -> invoked) +
              `clone_prosody_summary` additive telemetry (`session_mode`, `session_slices`,
              `session_fallback_used`, `pinned_space`).
- Part 6: Session quota gate pricing `min(120, 18 + ceil(chars * 0.085))` for solvency checks,
          Best-Fit selection, success debits, and pre-job reservations; legacy gates unchanged.
- Part 7: Mid-session checkpoint & resume at slice granularity (preserving `session_slices`,
          per-slice completion, `session_fallback_used`, and `pinned_space`).
"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
import time
from types import SimpleNamespace
import wave

import numpy as np
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import worker as worker_mod
from app.config import settings
from app.models.enums import AccountCategory, JobStatus, JobType
from app.models.schema import Base, Job, MinuteBalance, User
from app.services import billing as billing_mod
from app.services import clone_prosody as cp_mod
from app.services import dynamic_settings as ds_mod
from app.services import hf_pool_ledger as hf_ledger
from app.services import key_rotation as kr_mod
from app.services import queue as queue_mod
from app.services import voice_choice as vchoice_mod
from app.services import voice_chunking as vc_mod
from app.services import voxcpm_engine as vox_mod


def _make_speech_wav(path: Path, *, hz: float = 135.0, duration_s: float = 3.2, sr: int = 24000) -> Path:
    n = max(1, int(round(duration_s * sr)))
    ts = np.arange(n, dtype=np.float64) / sr
    sig = 0.50 * np.sin(2.0 * np.pi * hz * ts) + 0.18 * np.sin(4.0 * np.pi * hz * ts)
    pcm = np.clip(np.round(sig * 16000.0), -32768, 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return path


@pytest.fixture(autouse=True)
def _isolated_sync_kv(monkeypatch):
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
    vox_mod.VoxCPMEngine._token_rr_counter = 0
    vox_mod.VoxCPMEngine._token_bind_rr_counters.clear()
    queue_mod.reset_fairness_for_tests()
    yield kv
    hf_ledger.reset_ledger_for_tests(clear_store=True)


@pytest_asyncio.fixture
async def session_db_env(tmp_path, monkeypatch):
    import app.db.session as db_session_mod

    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "storage_root", storage)
    monkeypatch.setattr(settings, "admin_ids_raw", "999999")
    monkeypatch.setattr(settings, "bot_token", "123456:TEST_TOKEN")

    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_session_mod, "AsyncSessionLocal", maker)
    monkeypatch.setattr(worker_mod, "AsyncSessionLocal", maker)
    try:
        yield maker, storage
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_part1_dynamic_setting_registered_default_false_and_db_flip(db_session, monkeypatch):
    assert "clone_session_slices_enabled" in ds_mod.SETTING_TYPES
    assert ds_mod.SETTING_TYPES["clone_session_slices_enabled"] is bool
    assert "clone_session_slices_enabled" in ds_mod.DYNAMIC_SETTING_KEYS
    assert "clone_session_slices_enabled" in ds_mod.CONFIG_SETTING_KEYS
    assert settings.clone_session_slices_enabled is False
    assert vc_mod.is_clone_session_slices_enabled() is False

    orig = settings.clone_session_slices_enabled
    try:
        await ds_mod.set_dynamic_setting(db_session, "clone_session_slices_enabled", True, admin_id=1)
        await db_session.commit()
        assert settings.clone_session_slices_enabled is True
        assert vc_mod.is_clone_session_slices_enabled() is True

        settings.clone_session_slices_enabled = False
        await ds_mod.load_dynamic_settings(db_session, quiet=True)
        assert settings.clone_session_slices_enabled is True

        await ds_mod.set_dynamic_setting(db_session, "clone_session_slices_enabled", False, admin_id=1)
        await db_session.commit()
        assert settings.clone_session_slices_enabled is False
        assert vc_mod.is_clone_session_slices_enabled() is False
    finally:
        monkeypatch.setattr(settings, "clone_session_slices_enabled", orig)


def test_part2_session_slicer_boundaries_tail_merge_caps_and_legacy_untouched():
    # 1. ~2,441-char Burmese narration -> 3 balanced session slices (~850 target, <= 1,050 hard cap, zero mid-sentence cuts)
    sent_96 = "ကျောင်းသားလူငယ်လေးများသည် တောင်ပေါ်လမ်းမကြီးအတိုင်း ပျော်ရွှင်ချမ်းမြေ့စွာ လမ်းလျှောက်လာကြပါသည်။"
    long_2441 = " ".join([sent_96] * 25)
    assert 2400 <= len(long_2441) <= 2500

    legacy_before = vc_mod.plan_text_chunks(long_2441)
    slices = vc_mod.plan_session_slices(long_2441)
    legacy_after = vc_mod.plan_text_chunks(long_2441)

    assert legacy_before == legacy_after
    assert len(legacy_before) >= 6
    assert len(slices) == 3
    assert all(len(s) <= 1050 for s in slices)
    assert all(s.endswith("။") for s in slices)
    assert " ".join(slices) == long_2441

    # 2. Hard cap 1,050 enforced on non-tail-merged slices & never splits mid-sentence when sentence <= 1,050
    s_700 = ("မနက်ခင်းမှာ မောင်မောင်နဲ့ သူငယ်ချင်းတွေ မြို့ထဲက ဈေးကြီးဆီ သွားကြပါတယ် " * 9).strip() + "။"
    s_400 = ("ညနေပိုင်းတွင် အိမ်သို့ ပျော်ရွှင်စွာ ပြန်လာခဲ့ကြပါသည် " * 7).strip() + "။"
    assert 600 <= len(s_700) <= 720
    assert 350 <= len(s_400) <= 430
    assert len(s_700) + 1 + len(s_400) > 1050 or len(s_400) >= 200
    two_big = vc_mod.plan_session_slices(f"{s_700} {s_400} {s_400}")
    assert all(len(sl) <= 1050 for sl in two_big)
    assert all(sl.endswith("။") for sl in two_big)

    # 3. Trailing slice < 200 chars merges into predecessor (even over 850 target, up to <= 1,250)
    s_820 = ("မြန်မာ့ရိုးရာ ပွဲတော်ကြီးတွင် လူအများ ပျော်ရွှင်စွာ ဆင်နွှဲနေကြပါသည် " * 13).strip()[:820] + "။"
    s_tail_140 = ("နောက်ဆုံးတွင် အားလုံး အေးချမ်းစွာ အိမ်ပြန်ခဲ့ကြပါသည် " * 3).strip()[:140] + "။"
    assert 800 <= len(s_820) <= 845
    assert 100 <= len(s_tail_140) < 200
    merged = vc_mod.plan_session_slices(f"{s_820} {s_tail_140}")
    assert len(merged) == 1
    assert 900 <= len(merged[0]) <= 1250

    # 4. Trailing slice >= 200 chars does NOT merge when over target
    s_tail_230 = ("နောက်ဆုံးတွင် အားလုံး အေးချမ်းစွာ အိမ်ပြန်ခဲ့ကြပါသည် " * 5).strip()[:229] + "။"
    assert len(s_tail_230) >= 200
    unmerged_200 = vc_mod.plan_session_slices(f"{s_820} {s_tail_230}")
    assert len(unmerged_200) == 2

    # 5. Tail merge respects the 1,250 hard cap (never merges past 1,250)
    s_1045 = ("က" * 1044) + "။"
    s_tail_199 = ("ခ" * 198) + "။"
    # 1045 + 1 + 199 = 1245 <= 1250 -> merges
    merged_near_cap = vc_mod.plan_session_slices(f"{s_1045} {s_tail_199}")
    assert len(merged_near_cap) == 1
    assert len(merged_near_cap[0]) == 1245

    # With tail_merge_hard_cap_chars=1200 (or predecessor 1050 + tail 199 > custom cap), never exceeds cap
    capped = vc_mod.plan_session_slices(
        f"{s_1045} {s_tail_199}", tail_merge_hard_cap_chars=1200
    )
    assert len(capped) == 2
    assert all(len(x) <= 1200 for x in capped)


def test_part3_flag_off_is_legacy_identical(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(settings, "clone_session_slices_enabled", False)
    monkeypatch.setattr(worker_mod, "is_wav_edge_clipped", lambda _p: False)
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **kw: (Path(out).write_bytes(Path(raw).read_bytes()), Path(out))[1],
    )

    sent = "မနက်ခင်းမှာ မောင်မောင်နဲ့ သူငယ်ချင်းတွေ မြို့ထဲက ဈေးကြီးဆီ အတူတူ သွားကြပါတယ်။"
    full_text = " ".join([sent] * 12)

    seen_calls: list[dict] = []
    prosody_calls: list[int] = []
    orig_prosody_pass = cp_mod.run_post_loop_prosody_pass

    def _spy_prosody(*args, **kwargs):
        prosody_calls.append(len(args[0]))
        return orig_prosody_pass(*args, **kwargs)

    monkeypatch.setattr(cp_mod, "run_post_loop_prosody_pass", _spy_prosody)

    def _fake_synth(text, output_wav, **kw):
        seen_calls.append({"text": text, **kw})
        return _make_speech_wav(Path(output_wav), duration_s=2.8)

    eng = SimpleNamespace(synthesize=_fake_synth, pinned_space="awints001/voxcpm2-a")
    with caplog.at_level(logging.INFO, logger="app.worker"):
        out = worker_mod._synthesize_voxcpm_voice_job(
            eng,
            job_id="job_flag_off",
            text=full_text,
            output_wav=tmp_path / "flag_off.wav",
            reference_wav=None,
            control_instruction="Warm Burmese narrator",
            preset_voice="komin",
            speed="+0%",
            progress_cb=None,
        )

    assert out.is_file()
    expected_legacy_chunks = vc_mod.plan_text_chunks(full_text)
    assert [c["text"] for c in seen_calls] == expected_legacy_chunks
    assert all("session_mode" not in c for c in seen_calls)
    assert len(prosody_calls) == 1

    summaries = [r.getMessage() for r in caplog.records if "clone_prosody_summary" in r.getMessage()]
    assert len(summaries) == 1
    assert "session_mode=False" in summaries[0]
    assert "session_slices=0" in summaries[0]
    assert "session_fallback_used=False" in summaries[0]


def test_part3_and_4_flag_on_path_selection_endpoint_params_and_recap_excluded(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "clone_session_slices_enabled", True)
    monkeypatch.setattr(worker_mod, "is_wav_edge_clipped", lambda _p: False)
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **kw: (Path(out).write_bytes(Path(raw).read_bytes()), Path(out))[1],
    )

    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: f"FILE::{Path(p).name}")
    raw_gen = _make_speech_wav(tmp_path / "space_raw.wav", duration_s=3.0)
    ref_wav = _make_speech_wav(tmp_path / "ref.wav", duration_s=4.0)

    submits: list[dict] = []

    class _FakeClient:
        def __init__(self, space_name: str, tok: str | None):
            self.space_name = space_name
            self.tok = tok

        def submit(self, *args, **kwargs):
            submits.append({"space": self.space_name, "token": self.tok, "kwargs": dict(kwargs)})
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(raw_gen))

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _FakeClient(sp, hf_token)),
    )

    sent = "ကိုမင်းက ဒီနေ့မှာ စိတ်ဝင်စားစရာ ဇာတ်လမ်းတစ်ပုဒ်ကို အသေးစိတ် ပြောပြပေးသွားမှာ ဖြစ်ပါတယ်။"
    text_2k = " ".join([sent] * 22)
    expected_slices = vc_mod.plan_session_slices(text_2k)
    assert len(expected_slices) >= 2

    # 1. Standalone clone engine (`token_bind_index=None`) -> session slices via `/generate_session` + Space pin
    eng = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "work",
        spaces=["awints001/voxcpm2-a", "awints001/voxcpm2-b"],
        hf_tokens=["hf_tok_1", "hf_tok_2"],
    )
    monkeypatch.setattr(cp_mod, "select_steady_reference_slice", lambda p, *_a, **_kw: (Path(p), 4.0, cp_mod.measure_prosody(Path(p))))
    monkeypatch.setattr(cp_mod, "transcribe_reference_slice_checked", lambda *_a, **_kw: "မင်္ဂလာပါခင်ဗျာ။")

    out_wav = worker_mod._synthesize_voxcpm_voice_job(
        eng,
        job_id="job_sess_on",
        text=text_2k,
        output_wav=tmp_path / "sess_out.wav",
        reference_wav=ref_wav,
        control_instruction="Custom control",
        preset_voice=None,
        speed="+0%",
        progress_cb=None,
    )
    assert out_wav.is_file()
    assert len(submits) == len(expected_slices)
    assert eng.pinned_space == "awints001/voxcpm2-a"
    for idx, sub in enumerate(submits):
        assert sub["space"] == "awints001/voxcpm2-a"
        kw = sub["kwargs"]
        assert kw["api_name"] == "/generate_session"
        assert kw["text_input"] == expected_slices[idx]
        assert "control_instruction" in kw
        assert kw["reference_wav_path_input"] == "FILE::ref.wav"
        # Updated per Persona + Upload Conditioning Fix v2 Implementation Prompt
        # (2026-11-27, Parts 5 & 6): custom uploads switch to reference-only mode.
        assert kw["use_prompt_text"] is False
        assert kw["prompt_text_input"] == ""
        assert kw["cfg_value_input"] == pytest.approx(2.0)
        assert kw["do_normalize"] is True
        assert kw["denoise"] is True

    # 1b. Curated persona session slices keep `use_prompt_text is True` (2026-11-27 Part 6)
    submits.clear()
    persona_eng = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "work_persona_sess",
        spaces=["awints001/voxcpm2-a"],
        hf_tokens=["hf_tok_1"],
    )
    worker_mod._synthesize_voxcpm_voice_job(
        persona_eng,
        job_id="job_persona_sess_on",
        text=text_2k,
        output_wav=tmp_path / "persona_sess_out.wav",
        reference_wav=None,
        control_instruction="Warm storyteller",
        preset_voice="komin",
        speed="+0%",
        progress_cb=None,
    )
    assert len(submits) == len(expected_slices)
    assert all(s["kwargs"]["api_name"] == "/generate_session" for s in submits)
    assert all(s["kwargs"]["use_prompt_text"] is True for s in submits)
    assert all(bool(s["kwargs"]["prompt_text_input"]) for s in submits)

    # 2. Bound engine (`token_bind_index=0`, recap-style worker) -> excluded from session mode!
    submits.clear()
    bound_eng = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "work_bound",
        spaces=["awints001/voxcpm2-a"],
        hf_tokens=["hf_tok_1"],
        token_bind_index=0,
    )
    worker_mod._synthesize_voxcpm_voice_job(
        bound_eng,
        job_id="job_bound_excluded",
        text=text_2k,
        output_wav=tmp_path / "bound_out.wav",
        reference_wav=None,
        control_instruction="Narrator",
        preset_voice="komin",
        speed="+0%",
        progress_cb=None,
    )
    assert len(submits) == len(vc_mod.plan_text_chunks(text_2k, reference_transcript="x" * 40)) or len(submits) > len(expected_slices)
    assert all(s["kwargs"]["api_name"] == "/generate" for s in submits)

    # 3. Recap budget estimation is completely untouched when flag is ON
    now = time.time()
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest("hf_tok_1"): {
                    "status": "ok",
                    "remaining_s": 300,
                    "base_s": 300,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": "",
                }
            },
            "space_gates": {"awints001/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )
    r_units, r_gate, r_needed = hf_ledger.estimate_voxcpm_units_and_budget(
        kind="recap", planned_segments=8, duration_seconds=96.0
    )
    assert (r_units, r_gate, r_needed) == (8, 60.0, round(8 * 60.0 * 1.20, 2))


def test_part5_and_8_prosody_skip_vs_fallback_prosody_invoked_and_telemetry(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(settings, "clone_session_slices_enabled", True)
    monkeypatch.setattr(worker_mod, "is_wav_edge_clipped", lambda _p: False)
    monkeypatch.setattr(vox_mod, "TRANSIENT_RETRY_BACKOFF_SECONDS", 0.0)
    master_calls: list[Path] = []
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **kw: (
            master_calls.append(Path(out)),
            Path(out).write_bytes(Path(raw).read_bytes()),
            Path(out),
        )[2],
    )

    prosody_invocations: list[int] = []
    orig_prosody_pass = cp_mod.run_post_loop_prosody_pass

    def _spy_prosody(*args, **kwargs):
        prosody_invocations.append(len(args[0]))
        return orig_prosody_pass(*args, **kwargs)

    monkeypatch.setattr(cp_mod, "run_post_loop_prosody_pass", _spy_prosody)

    text_3slices = " ".join(
        f"အမှတ်စဉ် {i + 1:02d} ကျောင်းသားလူငယ်လေးများသည် တောင်ပေါ်လမ်းမကြီးအတိုင်း ပျော်ရွှင်ချမ်းမြေ့စွာ လမ်းလျှောက်လာကြပါသည်။"
        for i in range(22)
    )
    slices = vc_mod.plan_session_slices(text_3slices)
    assert len(slices) == 3
    assert len(set(slices)) == 3

    # Case A: No fallback -> prosody pass SKIPPED (0 calls), mastering runs once, telemetry reports session_fallback_used=False
    class _CleanSessionEngine:
        def __init__(self):
            self.pinned_space = None
            self.space_family_switched = False
            self.calls: list[dict] = []

        def synthesize(self, text, output_wav, *, session_mode=False, pin_space_override=None, **kw):
            self.calls.append(
                {"text": text, "session_mode": session_mode, "pin_space_override": pin_space_override}
            )
            if self.pinned_space is None:
                self.pinned_space = "awints001/voxcpm2-a"
            return _make_speech_wav(Path(output_wav), duration_s=3.4)

    eng_ok = _CleanSessionEngine()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.worker"):
        out_a = worker_mod._synthesize_voxcpm_voice_job(
            eng_ok,
            job_id="job_no_fb",
            text=text_3slices,
            output_wav=tmp_path / "no_fb.wav",
            reference_wav=None,
            control_instruction="Storyteller",
            preset_voice="komin",
            speed="+0%",
            progress_cb=None,
        )
    assert out_a.is_file()
    assert len(eng_ok.calls) == 3
    assert all(c["session_mode"] is True for c in eng_ok.calls)
    assert prosody_invocations == []
    assert len(master_calls) == 1

    lines_a = [r.getMessage() for r in caplog.records if "clone_prosody_summary" in r.getMessage()]
    assert len(lines_a) == 1
    assert "session_mode=True" in lines_a[0]
    assert "session_slices=3" in lines_a[0]
    assert "session_fallback_used=False" in lines_a[0]
    assert "pinned_space=awints001/voxcpm2-a" in lines_a[0]

    # Case B: Slice 2 exhausts session retries -> re-synthesized via <=385-char /generate chunks (unpinned),
    # job succeeds, prosody pass IS invoked over assembled parts, telemetry reports session_fallback_used=True!
    class _Slice2FallbackEngine:
        def __init__(self):
            self.pinned_space = None
            self.space_family_switched = False
            self.hf_tokens = ["hf_tok_1"]
            self.calls: list[dict] = []

        def synthesize(self, text, output_wav, *, session_mode=False, pin_space_override=None, **kw):
            self.calls.append(
                {"text": text, "session_mode": session_mode, "pin_space_override": pin_space_override}
            )
            if session_mode and text == slices[1]:
                raise RuntimeError("session timeout / 503 on slice 2")
            if session_mode and self.pinned_space is None:
                self.pinned_space = "awints001/voxcpm2-a"
            return _make_speech_wav(Path(output_wav), duration_s=2.6)

    eng_fb = _Slice2FallbackEngine()
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.worker"):
        out_b = worker_mod._synthesize_voxcpm_voice_job(
            eng_fb,
            job_id="job_slice2_fb",
            text=text_3slices,
            output_wav=tmp_path / "slice2_fb.wav",
            reference_wav=None,
            control_instruction="Storyteller",
            preset_voice="komin",
            speed="+0%",
            progress_cb=None,
        )
    assert out_b.is_file()
    fb_calls = [c for c in eng_fb.calls if c["session_mode"] is False]
    assert len(fb_calls) >= 2
    assert all(len(c["text"]) <= 385 for c in fb_calls)
    assert all(c["pin_space_override"] is False for c in fb_calls)
    # Prosody pass ran ONCE over `[slice1, *slice2_fb_chunks, slice3]`
    assert len(prosody_invocations) == 1
    assert prosody_invocations[0] == 2 + len(fb_calls)

    lines_b = [r.getMessage() for r in caplog.records if "clone_prosody_summary" in r.getMessage()]
    assert len(lines_b) == 1
    assert "session_mode=True" in lines_b[0]
    assert "session_slices=3" in lines_b[0]
    assert "session_fallback_used=True" in lines_b[0]
    assert "pinned_space=awints001/voxcpm2-a" in lines_b[0]


@pytest.mark.asyncio
async def test_part6_session_gate_pricing_solvency_bestfit_and_reservations(session_db_env, tmp_path, monkeypatch):
    # 1. Exact formula `min(120, 18 + ceil(chars * 0.085))`
    for chars, expected in (
        (0, 18.0),
        (1, 19.0),
        (200, 35.0),
        (500, 61.0),
        (850, 91.0),
        (1050, 108.0),
        (1200, 120.0),
        (1250, 120.0),
    ):
        assert hf_ledger.session_call_gate_s(chars) == expected
        assert hf_ledger.get_space_gate("awints001/voxcpm2-a", call_type="session", chars=chars) == expected

    # 2. Legacy space gate stays untouched; session solvency uses x1.5 door gate (137s for 850 chars)
    #    while success debit still deducts the x1.0 session_call_gate_s (91s)!
    tok_70 = "hf_tok_70s"
    tok_95 = "hf_tok_95s"
    tok_145 = "hf_tok_145s"
    tok_300 = "hf_tok_300s"
    now = time.time()
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok_70): {"status": "ok", "remaining_s": 70, "base_s": 300, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(tok_95): {"status": "ok", "remaining_s": 95, "base_s": 300, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(tok_145): {"status": "ok", "remaining_s": 145, "base_s": 300, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest(tok_300): {"status": "ok", "remaining_s": 300, "base_s": 300, "resets_at": "", "cooldown_until": "", "updated_at": ""},
            },
            # Legacy `/generate` gate recorded as 110s -> legacy space_gate must remain 110!
            "space_gates": {"awints001/voxcpm2-a": 110},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )
    assert hf_ledger.get_space_gate("awints001/voxcpm2-a") == 110.0

    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    src_wav = _make_speech_wav(tmp_path / "g.wav", duration_s=2.0)
    used_tokens: list[str | None] = []

    class _FakeClient:
        def __init__(self, tok):
            self.tok = tok

        def submit(self, *a, **kw):
            used_tokens.append(self.tok)
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(src_wav))

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _FakeClient(hf_token)),
    )

    text_850 = "က" * 850  # runtime gate = 91.0s; Part P2 x1.5 door gate = ceil(91 * 1.5) = 137.0s
    eng = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w",
        spaces=["awints001/voxcpm2-a"],
        hf_tokens=[tok_70, tok_95, tok_300, tok_145],
    )
    eng.synthesize(
        text_850,
        tmp_path / "sess_850.wav",
        skip_mastering=True,
        allow_fallback=False,
        session_mode=True,
    )
    # tok_70 & tok_95 (<137s door gate) skipped; tok_145 (>=137s, smallest sufficient) picked over tok_300;
    # debited by x1.0 runtime gate (91s) -> 54s left; legacy space_gate still 110!
    assert used_tokens == [tok_145]
    st_after = hf_ledger.load_pool_state()
    assert st_after["space_gates"]["awints001/voxcpm2-a"] == 110
    assert hf_ledger.get_token_remaining_s(tok_145, state=st_after) == pytest.approx(54.0)

    # 3. Pre-job estimation & reservation toggle cleanly between Flag OFF (legacy) and Flag ON (session)
    monkeypatch.setattr(settings, "clone_session_slices_enabled", False)
    u_off, g_off, need_off = hf_ledger.estimate_voxcpm_units_and_budget(kind="clone", text=text_850)
    assert g_off == 110.0
    assert need_off == round(u_off * 110.0 * hf_ledger.PRE_JOB_SAFETY_MULTIPLIER, 2)

    monkeypatch.setattr(settings, "clone_session_slices_enabled", True)
    u_on, g_on, need_on = hf_ledger.estimate_voxcpm_units_and_budget(kind="clone", text=text_850)
    assert (u_on, g_on, need_on) == (1, 91.0, round(91.0 * hf_ledger.PRE_JOB_SAFETY_MULTIPLIER, 2))

    # Pre-job queue admission uses session gate (e.g. single 500-char slice -> gate 61s, needed 73.2s;
    # pool with one 90s token admits under session pricing even though legacy 110s gate would reject!)
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest("hf_90s"): {"status": "ok", "remaining_s": 90, "base_s": 150, "resets_at": "", "cooldown_until": "", "updated_at": ""},
            },
            "space_gates": {"awints001/voxcpm2-a": 110},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )
    maker, _storage = session_db_env
    async with maker() as session:
        session.add(User(id=7010, username="u7010", first_name="U", account_category=AccountCategory.FREE.value))
        session.add(MinuteBalance(user_id=7010, welcome_seconds=300))
        await session.commit()
        job_admitted, status_str = await queue_mod.create_voice_clone_job(
            session, user_id=7010, text="မ" * 500, preset_voice="komin"
        )
        assert job_admitted is not None
        assert status_str == JobStatus.QUEUED.value


@pytest.mark.asyncio
async def test_part7_checkpoint_and_resume_mid_session(session_db_env, tmp_path, monkeypatch):
    maker, storage = session_db_env
    monkeypatch.setattr(settings, "clone_session_slices_enabled", True)
    monkeypatch.setattr(worker_mod, "is_wav_edge_clipped", lambda _p: False)
    monkeypatch.setattr(vox_mod, "TRANSIENT_RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **kw: (Path(out).write_bytes(Path(raw).read_bytes()), Path(out))[1],
    )

    from app.services.number_words import normalize_numbers_for_speech

    text_3slices = " ".join(
        f"အမှတ်စဉ် {i + 1:02d} ကျောင်းသားလူငယ်လေးများသည် တောင်ပေါ်လမ်းမကြီးအတိုင်း ပျော်ရွှင်ချမ်းမြေ့စွာ လမ်းလျှောက်လာကြပါသည်။"
        for i in range(22)
    )
    expected_slices = vc_mod.plan_session_slices(normalize_numbers_for_speech(text_3slices))
    assert len(expected_slices) == 3
    assert len(set(expected_slices)) == 3

    uid = 8801
    jid = "vc_sess_resume_1"
    async with maker() as session:
        session.add(
            User(
                id=uid,
                username="paid_sess",
                first_name="PaidSess",
                language_code="my",
                account_category=AccountCategory.PURCHASED.value,
            )
        )
        session.add(MinuteBalance(user_id=uid, purchased_seconds=900))
        await session.commit()
        await billing_mod.reserve_for_job(
            session,
            user_id=uid,
            job_id=jid,
            job_type=JobType.VOICE_CLONE.value,
            billing_mode=billing_mod.BILLING_MODE_VOICE_ONLY,
            reserve_recap_seconds=0,
            reserve_voice_seconds=180,
        )
        session.add(
            Job(
                id=jid,
                user_id=uid,
                chat_id=uid,
                status=JobStatus.QUEUED.value,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                preset_voice="komin",
                input_text=text_3slices,
                seconds_estimated=180,
            )
        )
        await session.commit()

    # Run 1: Slice 1 succeeds on `/generate_session` (pins `awints001/voxcpm2-b`);
    # Slice 2 hits quota exhaustion on both session and fallback -> job parks at slice 1/3!
    run1_calls: list[tuple[bool, str]] = []

    def _run1_synth(self_eng, text, output_wav, *, session_mode=False, **kw):
        run1_calls.append((bool(session_mode), text))
        if session_mode and text == expected_slices[0]:
            self_eng.pinned_space = "awints001/voxcpm2-b"
            return _make_speech_wav(Path(output_wav), duration_s=30.0)
        raise vox_mod.VoxCPMQuotaExhaustedError("Pool exhausted at slice 2")

    monkeypatch.setattr(vox_mod.VoxCPMEngine, "synthesize", _run1_synth)

    async with maker() as session:
        j = await session.get(Job, jid)
        ok1 = await worker_mod.process_single_job(session, j, bot=None)
        await session.refresh(j)

    assert ok1 is False
    assert j.status == JobStatus.WAITING_CHOICE.value
    ckpt_dir = storage / "dp_refs" / jid
    manifest = json.loads((ckpt_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["session_mode"] is True
    assert manifest["session_slices"] == expected_slices
    assert manifest["completed_units"] == 1
    assert manifest["total_units"] == 3
    assert manifest["session_fallback_used"] is False
    assert manifest["pinned_space"] == "awints001/voxcpm2-b"
    assert (ckpt_dir / "slice01.wav").is_file()

    choice_st = vchoice_mod.parse_choice_state(j.voice_choice_state)
    assert choice_st["completed_units"] == 1
    assert choice_st["total_units"] == 3
    assert choice_st["pinned_space"] == "awints001/voxcpm2-b"
    expected_rem_gpu = round(
        sum(hf_ledger.session_call_gate_s(len(s)) for s in expected_slices[1:])
        * hf_ledger.PRE_JOB_SAFETY_MULTIPLIER,
        2,
    )
    assert choice_st["needed_gpu_s"] == pytest.approx(expected_rem_gpu)

    # Run 2 (worker restart / resume): Slice 1 is reused from disk (NOT re-synthesized);
    # Slices 2 & 3 synthesize on pinned `awints001/voxcpm2-b`; prosody pass stays skipped; job completes SUCCESS!
    run2_calls: list[tuple[bool, str, str | None]] = []
    prosody_ran: list[int] = []
    monkeypatch.setattr(cp_mod, "run_post_loop_prosody_pass", lambda *a, **kw: prosody_ran.append(1))

    def _run2_synth(self_eng, text, output_wav, *, session_mode=False, **kw):
        run2_calls.append((bool(session_mode), text, self_eng.pinned_space))
        return _make_speech_wav(Path(output_wav), duration_s=30.0)

    monkeypatch.setattr(vox_mod.VoxCPMEngine, "synthesize", _run2_synth)

    async with maker() as session:
        j = await session.get(Job, jid)
        j.status = JobStatus.QUEUED.value
        await session.commit()
        ok2 = await worker_mod.process_single_job(session, j, bot=None)
        await session.refresh(j)

    assert ok2 is True
    assert j.status == JobStatus.SUCCESS.value
    assert [t for _sm, t, _pin in run2_calls] == expected_slices[1:]
    assert all(sm is True and pin == "awints001/voxcpm2-b" for sm, _t, pin in run2_calls)
    assert prosody_ran == []


def test_part7_checkpoint_resume_preserves_prior_slice_fallback_and_runs_prosody(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "clone_session_slices_enabled", True)
    monkeypatch.setattr(worker_mod, "is_wav_edge_clipped", lambda _p: False)
    monkeypatch.setattr(vox_mod, "TRANSIENT_RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **kw: (Path(out).write_bytes(Path(raw).read_bytes()), Path(out))[1],
    )

    text_2slices = " ".join(
        f"ဝါကျအမှတ် {i + 1:02d} ကျောင်းသားလူငယ်လေးများသည် တောင်ပေါ်လမ်းမကြီးအတိုင်း ပျော်ရွှင်ချမ်းမြေ့စွာ လမ်းလျှောက်လာကြပါသည်။"
        for i in range(14)
    )
    slices = vc_mod.plan_session_slices(text_2slices)
    assert len(slices) == 2
    assert len(set(slices)) == 2

    ckpt_dir = tmp_path / "ckpt_fb"
    run1_texts: list[tuple[bool, str]] = []

    class _Run1Eng:
        pinned_space = None
        space_family_switched = False
        hf_tokens = ["hf_tok_1"]

        def synthesize(self, text, output_wav, *, session_mode=False, **kw):
            run1_texts.append((bool(session_mode), text))
            if session_mode and text == slices[0]:
                raise RuntimeError("slice 1 session failed -> falls back to /generate")
            if text == slices[1] or text in slices[1]:
                raise vox_mod.VoxCPMQuotaExhaustedError("quota exhausted at slice 2")
            return _make_speech_wav(Path(output_wav), duration_s=2.5)

    with pytest.raises(vox_mod.VoxCPMQuotaExhaustedError):
        worker_mod._synthesize_voxcpm_voice_job(
            _Run1Eng(),
            job_id="job_fb_ckpt",
            text=text_2slices,
            output_wav=tmp_path / "out1.wav",
            reference_wav=None,
            control_instruction="Narrator",
            preset_voice="komin",
            speed="+0%",
            progress_cb=None,
            checkpoint_dir=ckpt_dir,
        )

    manifest = json.loads((ckpt_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["completed_units"] == 1
    assert manifest["session_fallback_used"] is True
    assert manifest["slice_status"][0]["fell_back"] is True

    # Resume: Slice 1 fallback sub-chunks reused from checkpoint (0 new calls for Slice 1),
    # Slice 2 succeeds via `/generate_session`, and prosody pass runs because `session_fallback_used=True`!
    run2_texts: list[tuple[bool, str]] = []
    prosody_calls: list[int] = []
    orig_prosody = cp_mod.run_post_loop_prosody_pass

    def _spy_prosody(*args, **kwargs):
        prosody_calls.append(len(args[0]))
        return orig_prosody(*args, **kwargs)

    monkeypatch.setattr(cp_mod, "run_post_loop_prosody_pass", _spy_prosody)

    class _Run2Eng:
        pinned_space = None
        space_family_switched = False
        hf_tokens = ["hf_tok_1"]

        def synthesize(self, text, output_wav, *, session_mode=False, **kw):
            run2_texts.append((bool(session_mode), text))
            self.pinned_space = "awints001/voxcpm2-c"
            return _make_speech_wav(Path(output_wav), duration_s=2.5)

    out_final = worker_mod._synthesize_voxcpm_voice_job(
        _Run2Eng(),
        job_id="job_fb_ckpt",
        text=text_2slices,
        output_wav=tmp_path / "out_final.wav",
        reference_wav=None,
        control_instruction="Narrator",
        preset_voice="komin",
        speed="+0%",
        progress_cb=None,
        checkpoint_dir=ckpt_dir,
    )
    assert out_final.is_file()
    assert run2_texts == [(True, slices[1])]
    assert len(prosody_calls) == 1

"""Unit tests for PART C — VoxCPM Failover Resilience (2026-11-26).

Covers C1–C4:
- C1: Per-Space 10-minute timeout circuit breaker in `aj_hf:pool_state` (additive only;
      tokens remain innocent/uncooled; cooled Spaces skipped across legacy, session, clone,
      and recap walks — including mid-walk & pinned-Space re-pin — with last-resort fallback
      when no other solvent pair exists; auto-expires after 10 minutes).
- C2: Per-attempt budget cap (`PAIR_ATTEMPT_TIMEOUT_CAP_SECONDS == 45.0`) enforced on both
      Gradio queue polling and hung synchronous `Client()` / `submit()` calls; prompt
      first-Space responses keep zero added latency.
- C3: Paid total failover budget raised to 240 s (`SEGMENT_BUDGET_PAID_SECONDS == 240.0`)
      across standalone Paid clone and Paid recap; Free stays at 120 s.
- C4: Per-Space timeout telemetry in per-job logs (`clone_prosody_summary` / `space_timeouts`),
      running `space_timeouts` counter in `aj_hf:pool_state`, and live surfacing in
      `/api/admin/key-health` (`providers.spaces`).
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
from app.services import key_health as kh_mod
from app.services import key_rotation as kr_mod
from app.services import voxcpm_engine as vox_mod
from app.services.recap_engine import core as recap_core
from app.services.recap_engine import voxcpm_engine as recap_vox_mod


def _make_wav(path: Path, *, hz: float = 140.0, duration_s: float = 2.0, sr: int = 24000) -> Path:
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
def _isolated_kv(monkeypatch):
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
    vox_mod.VoxCPMEngine._token_rr_counter = 0
    vox_mod.VoxCPMEngine._token_bind_rr_counters.clear()
    yield kv
    hf_ledger.reset_ledger_for_tests(clear_store=True)
    kh_mod._reset_cache_for_tests()


def test_c1_space_timeout_circuit_breaker_10m_innocent_tokens_skip_and_last_resort(
    tmp_path, monkeypatch, _isolated_kv
):
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    gen_wav = _make_wav(tmp_path / "good.wav", duration_s=1.8)

    # Additive schema: untouched state has exact 3 base keys
    now = time.time()
    base_state = hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest("hf_tok_a"): {"status": "ok", "remaining_s": 300, "base_s": 300, "resets_at": "", "cooldown_until": "", "updated_at": ""},
                hf_ledger.token_digest("hf_tok_b"): {"status": "ok", "remaining_s": 300, "base_s": 300, "resets_at": "", "cooldown_until": "", "updated_at": ""},
            },
            "space_gates": {"yunaisbusy/voxcpm2-2": 60, "awints001/voxcpm2-b": 60, "awints001/voxcpm2-good": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )
    assert set(base_state.keys()) == {"tokens", "space_gates", "in_flight_gpu_s"}
    assert hf_ledger.SPACE_TIMEOUT_COOLDOWN_SECONDS == 600.0

    attempts_seen: list[tuple[str, str | None, str]] = []

    class _SpaceClient:
        def __init__(self, space_name: str, tok: str | None):
            self.space_name = space_name
            self.tok = tok

        def submit(self, *args, **kwargs):
            api_name = kwargs.get("api_name", "/generate")
            attempts_seen.append((self.space_name, self.tok, api_name))
            if self.space_name in ("yunaisbusy/voxcpm2-2", "awints001/voxcpm2-b"):
                # Never finishes -> triggers per-attempt timeout
                return SimpleNamespace(done=lambda: False, status=lambda: None, cancel=lambda: None)
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(gen_wav))

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _SpaceClient(sp, hf_token)),
    )

    eng1 = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w1",
        spaces=["yunaisbusy/voxcpm2-2", "awints001/voxcpm2-b", "awints001/voxcpm2-good"],
        hf_tokens=["hf_tok_a", "hf_tok_b"],
    )
    out1 = eng1.synthesize(
        "ပထမ စာကြောင်း ဖြစ်ပါသည်။",
        tmp_path / "out1.wav",
        timeout=0.15,
        skip_mastering=True,
        allow_fallback=False,
    )
    assert out1.is_file()
    # Both bad Spaces timed out ONCE each and were cooled down for 10m; tokens stayed completely innocent!
    assert [sp for sp, _t, _api in attempts_seen] == [
        "yunaisbusy/voxcpm2-2",
        "awints001/voxcpm2-b",
        "awints001/voxcpm2-good",
    ]
    assert not hf_ledger.is_token_cooling("hf_tok_a")
    assert not hf_ledger.is_token_cooling("hf_tok_b")
    assert vox_mod.VoxCPMEngine._token_cooldowns == {}
    assert hf_ledger.is_space_cooling("yunaisbusy/voxcpm2-2") is True
    assert hf_ledger.is_space_cooling("awints001/voxcpm2-b") is True
    assert hf_ledger.is_space_cooling("awints001/voxcpm2-good") is False
    assert eng1.space_timeouts == {"yunaisbusy/voxcpm2-2": 1, "awints001/voxcpm2-b": 1}

    # Walk 2 (session path `/generate_session` on a fresh engine): cooled Spaces are SKIPPED completely!
    attempts_seen.clear()
    eng2 = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w2",
        spaces=["yunaisbusy/voxcpm2-2", "awints001/voxcpm2-b", "awints001/voxcpm2-good"],
        hf_tokens=["hf_tok_a", "hf_tok_b"],
    )
    out2 = eng2.synthesize(
        "ဒုတိယ စာကြောင်း ဖြစ်ပါသည်။",
        tmp_path / "out2.wav",
        timeout=0.15,
        skip_mastering=True,
        allow_fallback=False,
        session_mode=True,
    )
    assert out2.is_file()
    assert len(attempts_seen) == 1
    assert attempts_seen[0][0] == "awints001/voxcpm2-good"
    assert attempts_seen[0][2] == "/generate_session"

    # Walk 3 (Recap engine adapter): cooled Spaces are ALSO skipped!
    attempts_seen.clear()
    eng_recap = recap_vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w3",
        spaces=["yunaisbusy/voxcpm2-2", "awints001/voxcpm2-good"],
        hf_tokens=["hf_tok_a"],
        token_bind_index=0,
    )
    eng_recap.synthesize(
        "တတိယ စာကြောင်း။",
        tmp_path / "out3.wav",
        timeout=0.15,
        skip_mastering=True,
        allow_fallback=False,
    )
    assert [sp for sp, _t, _api in attempts_seen] == ["awints001/voxcpm2-good"]

    # Walk 4 (Last-resort rule): when ONLY cooled Space(s) exist and recover, last-resort tries once and succeeds!
    attempts_seen.clear()
    recovered = {"ok": True}

    class _RecoveredClient:
        def __init__(self, sp, tok):
            self.sp = sp
            self.tok = tok

        def submit(self, *a, **kw):
            attempts_seen.append((self.sp, self.tok, kw.get("api_name", "/generate")))
            if recovered["ok"]:
                return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(gen_wav))
            return SimpleNamespace(done=lambda: False, status=lambda: None, cancel=lambda: None)

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _RecoveredClient(sp, hf_token)),
    )
    eng_lr = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w_lr",
        spaces=["yunaisbusy/voxcpm2-2"],  # ONLY space in pool, currently cooling!
        hf_tokens=["hf_tok_a", "hf_tok_b"],
    )
    out_lr = eng_lr.synthesize(
        "နောက်ဆုံး အရန် စမ်းသပ်ချက်။",
        tmp_path / "out_lr.wav",
        timeout=0.15,
        skip_mastering=True,
        allow_fallback=False,
    )
    assert out_lr.is_file()
    assert len(attempts_seen) == 1
    assert attempts_seen[0][0] == "yunaisbusy/voxcpm2-2"

    # Cooldown expires cleanly after 10 minutes (600 s)
    after_10m = time.time() + 601.0
    assert hf_ledger.is_space_cooling("yunaisbusy/voxcpm2-2", now_ts=after_10m) is False
    norm_after = hf_ledger.load_pool_state(now_ts=after_10m)
    assert "space_cooldowns" not in norm_after
    # Running timeout counter persists across cooldown expiry!
    assert norm_after["space_timeouts"] == {"yunaisbusy/voxcpm2-2": 1, "awints001/voxcpm2-b": 1}


def test_c1_pinned_space_timeout_skips_remaining_pinned_tokens_and_repins_immediately(
    tmp_path, monkeypatch
):
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    gen_wav = _make_wav(tmp_path / "repin.wav", duration_s=1.8)
    tried: list[tuple[str, str | None]] = []

    class _PinHangClient:
        def __init__(self, sp, tok):
            self.sp = sp
            self.tok = tok

        def submit(self, *a, **kw):
            tried.append((self.sp, self.tok))
            if self.sp == "awints001/voxcpm2-hung-pin":
                return SimpleNamespace(done=lambda: False, status=lambda: None, cancel=lambda: None)
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(gen_wav))

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _PinHangClient(sp, hf_token)),
    )

    eng = vox_mod.VoxCPMEngine(
        work_dir=tmp_path,
        spaces=["awints001/voxcpm2-hung-pin", "awints001/voxcpm2-healthy"],
        hf_tokens=[f"hf_t{i}" for i in range(6)],
    )
    eng.pinned_space = "awints001/voxcpm2-hung-pin"
    out = eng.synthesize(
        "ပင်ထိုးထားသော Space ရပ်သွားသည့်အခါ ချက်ချင်း ပြောင်းလဲခြင်း။",
        tmp_path / "repin_out.wav",
        timeout=0.15,
        skip_mastering=True,
        allow_fallback=False,
    )
    assert out.is_file()
    # Hung pinned Space was tried ONCE (not 6 times!) and immediately jumped to healthy repin Space!
    assert [sp for sp, _tok in tried] == ["awints001/voxcpm2-hung-pin", "awints001/voxcpm2-healthy"]
    assert eng.pinned_space == "awints001/voxcpm2-healthy"


def test_c2_per_attempt_45s_cap_and_client_side_abandonment_on_hung_submit(tmp_path, monkeypatch):
    assert vox_mod.PAIR_ATTEMPT_TIMEOUT_CAP_SECONDS == 45.0

    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    gen_wav = _make_wav(tmp_path / "fast.wav", duration_s=1.5)

    # 1. Even if caller passes `timeout=120` (or settings.voxcpm_timeout=120), attempt cap clamps to PAIR_ATTEMPT_TIMEOUT_CAP_SECONDS!
    monkeypatch.setattr(vox_mod, "PAIR_ATTEMPT_TIMEOUT_CAP_SECONDS", 0.35)
    monkeypatch.setattr(settings, "voxcpm_timeout", 120)

    call_log: list[str] = []

    class _BlockingSubmitClient:
        def __init__(self, sp: str):
            self.sp = sp

        def submit(self, *a, **kw):
            call_log.append(self.sp)
            if self.sp == "bad/blocking-submit":
                # Simulate synchronous HTTP hang inside `client.submit()` itself!
                time.sleep(5.0)
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(gen_wav))

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _BlockingSubmitClient(sp)),
    )

    eng = vox_mod.VoxCPMEngine(
        work_dir=tmp_path,
        spaces=["bad/blocking-submit", "good/fast-space"],
        hf_tokens=["hf_tok_1"],
    )
    t0 = time.monotonic()
    out = eng.synthesize(
        "စမ်းသပ် စာသား။",
        tmp_path / "abandoned.wav",
        timeout=120,  # clamped by PAIR_ATTEMPT_TIMEOUT_CAP_SECONDS (0.35s here)
        skip_mastering=True,
        allow_fallback=False,
    )
    elapsed = time.monotonic() - t0
    assert out.is_file()
    assert call_log == ["bad/blocking-submit", "good/fast-space"]
    # Abandoned `time.sleep(5.0)` in ~0.6s + 0.5s pair backoff (< 2.0s, NOT 5.0s!)
    assert elapsed < 2.2
    assert hf_ledger.is_space_cooling("bad/blocking-submit") is True

    # 2. Prompt first-Space happy path sees zero added delay
    call_log.clear()
    eng_fast = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "fast_w",
        spaces=["good/fast-space"],
        hf_tokens=["hf_tok_1"],
    )
    t_fast0 = time.monotonic()
    out_fast = eng_fast.synthesize(
        "ချက်ချင်း ရရှိသော စာသား။",
        tmp_path / "fast_out.wav",
        skip_mastering=True,
        allow_fallback=False,
    )
    assert out_fast.is_file()
    assert (time.monotonic() - t_fast0) < 0.5


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")
def test_c3_paid_failover_budget_240s_and_free_120s_across_clone_and_recap(tmp_path, monkeypatch):
    # 1. Engine constants: Paid = 240.0 s, Free = 120.0 s
    assert vox_mod.SEGMENT_BUDGET_FREE_SECONDS == 120.0
    assert vox_mod.SEGMENT_BUDGET_PAID_SECONDS == 240.0
    assert recap_core._VOX_SEG_CAP_FREE_S == 120.0
    assert recap_core._VOX_SEG_CAP_PAID_S == 240.0

    # 2. Recap EdgeSmartSync passes 240.0 s `total_timeout` for Paid jobs and <=120.0 s for Free jobs
    src = tmp_path / "src.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=duration=2:size=320x180:rate=24",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(src),
        ],
        check=True,
    )
    plan_file = tmp_path / "smart_sync_plan.json"
    plan_file.write_text(
        json.dumps(
            {"segments": [{"segment_id": 1, "source_index": 0, "video_start": 0.0, "video_end": 2.0, "narration": "စမ်းသပ် စာကြောင်း။"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    seen_budgets: list[tuple[str | None, float | None]] = []

    class _BudgetSpyVox:
        def __init__(self, **kw):
            self.space_pin = kw.get("space_pin")
            self.space_timeouts = {}

        def _adapt_reference_for_clone(self, p):
            return Path(p)

        def synthesize(self, text, output_wav, **kw):
            seen_budgets.append((kw.get("tier"), kw.get("total_timeout")))
            return _make_wav(Path(output_wav), duration_s=1.0)

    monkeypatch.setattr(recap_vox_mod, "VoxCPMEngine", _BudgetSpyVox)

    for is_paid in (False, True):
        sync = recap_core.EdgeSmartSync(
            job_dir=str(tmp_path / f"recap_{is_paid}"),
            source_path=str(src),
            plan_path=str(plan_file),
            voice="voxcpm_komin",
            overlays={"tts_engine": "voxcpm"},
            paid_tier=is_paid,
            render_final_video=False,
            tts_workers=1,
            log=lambda _m: None,
        )
        sync.run()

    assert seen_budgets[0][0] == "free"
    assert seen_budgets[0][1] <= 120.0
    assert seen_budgets[1] == ("paid", 240.0)


def test_c4_per_space_timeout_telemetry_in_job_summary_ledger_and_admin_key_health(
    tmp_path, monkeypatch, caplog
):
    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: p)
    monkeypatch.setattr(worker_mod, "is_wav_edge_clipped", lambda _p: False)
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **kw: (Path(out).write_bytes(Path(raw).read_bytes()), Path(out))[1],
    )
    gen_wav = _make_wav(tmp_path / "ok.wav", duration_s=2.8)

    class _SlowThenGoodClient:
        def __init__(self, sp: str):
            self.sp = sp

        def submit(self, *a, **kw):
            if self.sp == "yunaisbusy/voxcpm2-2":
                return SimpleNamespace(done=lambda: False, status=lambda: None, cancel=lambda: None)
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(gen_wav))

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _SlowThenGoodClient(sp)),
    )
    monkeypatch.setattr(vox_mod, "PAIR_ATTEMPT_TIMEOUT_CAP_SECONDS", 0.15)
    monkeypatch.setattr(settings, "clone_session_slices_enabled", True)

    sent = "ကိုမင်းက ဒီနေ့မှာ စိတ်ဝင်စားစရာ ဇာတ်လမ်းတစ်ပုဒ်ကို အသေးစိတ် ပြောပြပေးသွားမှာ ဖြစ်ပါတယ်။"
    text_2slices = " ".join([sent] * 18)

    eng = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "w",
        spaces=["yunaisbusy/voxcpm2-2", "awints001/voxcpm2-a"],
        hf_tokens=["hf_tok_1", "hf_tok_2"],
    )
    with caplog.at_level(logging.INFO, logger="app.worker"):
        out = worker_mod._synthesize_voxcpm_voice_job(
            eng,
            job_id="job_c4_telemetry",
            text=text_2slices,
            output_wav=tmp_path / "c4_out.wav",
            reference_wav=None,
            control_instruction="Storyteller",
            preset_voice="komin",
            speed="+0%",
            progress_cb=None,
            tier="paid",
        )
    assert out.is_file()

    # 1. Per-job summary log includes `space_timeouts={'yunaisbusy/voxcpm2-2': 1}`
    summaries = [r.getMessage() for r in caplog.records if "clone_prosody_summary" in r.getMessage()]
    assert len(summaries) == 1
    assert "space_timeouts={'yunaisbusy/voxcpm2-2': 1}" in summaries[0]

    # 2. Running counter in `aj_hf:pool_state` increments across jobs
    hf_ledger.record_space_timeout("yunaisbusy/voxcpm2-2")
    assert hf_ledger.get_space_timeout_counts() == {"yunaisbusy/voxcpm2-2": 2}

    # 3. Admin Key Health payload (`run_key_health_check` AND cached `get_key_health`) surfaces slow Space counts + cooldown
    monkeypatch.setattr(
        kh_mod,
        "_load_pools",
        lambda: (["hf_tok_1"], [], [], ["yunaisbusy/voxcpm2-2", "awints001/voxcpm2-a"]),
    )
    monkeypatch.setattr(
        kh_mod,
        "_probe_hf_account",
        lambda idx, _tok: {"index": idx, "status": "ok", "account": "acct1", "remaining_s": 300.0, "base_s": 300.0},
    )
    monkeypatch.setattr(
        kh_mod,
        "_probe_space",
        lambda name: {"name": name, "stage": "RUNNING"},
    )

    kh_data = kh_mod.get_key_health(force_refresh=True)
    sp_prov = kh_data["providers"]["spaces"]
    assert sp_prov["cooling_down"] == 1
    assert sp_prov["space_timeouts"] == {"yunaisbusy/voxcpm2-2": 2}
    by_name = {it["name"]: it for it in sp_prov["items"]}
    assert by_name["yunaisbusy/voxcpm2-2"]["timeouts"] == 2
    assert by_name["yunaisbusy/voxcpm2-2"]["cooling_down"] is True
    assert by_name["yunaisbusy/voxcpm2-2"]["cooldown_until"] is not None
    assert by_name["awints001/voxcpm2-a"]["timeouts"] == 0
    assert by_name["awints001/voxcpm2-a"]["cooling_down"] is False

    # Subsequent timeout updates cached `get_key_health(force_refresh=False)` immediately without re-probing!
    hf_ledger.record_space_timeout("yunaisbusy/voxcpm2-2")
    kh_cached = kh_mod.get_key_health(force_refresh=False)
    assert kh_cached["cached"] is True
    assert kh_cached["providers"]["spaces"]["space_timeouts"] == {"yunaisbusy/voxcpm2-2": 3}
    assert kh_cached["providers"]["spaces"]["items"][0]["timeouts"] == 3

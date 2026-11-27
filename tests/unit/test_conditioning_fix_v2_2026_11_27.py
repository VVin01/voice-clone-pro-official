"""Unit tests for Persona + Upload Conditioning Fix v2 (2026-11-27).

Covers:
- Part 1: 4-layer cross-submode contamination fix (`app.js`, `api.py`, `queue.py`, `worker.py`)
  and the payload-never-both invariant.
- Part 2: `session_space/app.py` `_generate_impl` routing (`ref_continuation` when
  `use_prompt_text=True` & non-empty prompt text; `reference_wav_path` ONLY otherwise),
  preserving `gpu_seconds_used` / `probe_quota` / `@spaces.GPU` durations.
- Part 3: 6 AI-designed persona references (`kothura, kozayar, komyat, kophone, mayoon, machaw`)
  trimmed cleanly of leading `"မင်္ဂလာပါ။"`, byte-identical across both directories, with
  `samples/*.mp3` and corpus personas untouched.
- Part 5:
  - Custom uploads (`is_custom_clone=True`) & own-voice recaps use `use_prompt_text=False`
    (`prompt_text=""`) and never call `transcribe_reference_slice_checked`.
  - `select_steady_reference_slice` snaps endpoints to nearest >=80 ms low-RMS pause within
    +-0.8 s and applies 25 ms cosine fade-in/out.
  - Duration-bloat guard (`worker.py`): >=80-char unit rendering slower than 4.5 chars/s logs
    a warning, retries once, and logs telemetry-only without failing if still slow.
  - Dynamic setting `voxcpm_reference_only_mode` (default False) forces reference-only
    conditioning globally across personas and recaps.
"""
from __future__ import annotations

import io
import json
import logging
import sys
import types
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import numpy as np
import pytest
from fastapi import HTTPException, UploadFile

from app import worker as worker_mod
from app.config import settings
from app.models.enums import AccountCategory, JobStatus, JobType
from app.models.schema import Job, VoiceModel
from app.services import clone_prosody as cp_mod
from app.services import curated_voices as cv_mod
from app.services import dynamic_settings as ds_mod
from app.services import queue as queue_mod
from app.services import voxcpm_engine as vox_mod
from app.services.ledger import get_minute_balance
from app.webapp import api as api_mod

get_or_create_user = queue_mod.get_or_create_user

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")


def _write_pcm_wav(path: Path, samples: np.ndarray, sr: int = 16000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm16 = np.clip(samples * 32767.0, -32768.0, 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm16.tobytes())
    return path


def _make_tone_wav(path: Path, duration_s: float = 3.0, hz: float = 150.0, sr: int = 16000) -> Path:
    n = max(1, int(round(duration_s * sr)))
    t = np.arange(n, dtype=np.float32) / float(sr)
    sig = 0.28 * np.sin(2.0 * np.pi * hz * t) + 0.12 * np.sin(4.0 * np.pi * hz * t)
    return _write_pcm_wav(path, sig, sr=sr)


# ── Part 1: 4-Layer Cross-Submode Contamination Fix ──────────────────────────


def test_part1_frontend_submode_guard_and_payload_never_both():
    # Layer 1a: custom file branch inside `handleVoiceGenerate` gated on `state.voxcpmSubMode === "custom"`
    idx_gen = APP_JS.index("async function handleVoiceGenerate")
    gen_block = APP_JS[idx_gen : idx_gen + 6000]
    assert 'if (state.voxcpmSubMode === "custom" && state.selectedReferenceFile)' in gen_block
    assert "if (state.selectedReferenceFile) {" not in gen_block

    # Layer 1b: custom FormData upload branch never appends `preset_voice`
    idx_branch = gen_block.index('if (state.voxcpmSubMode === "custom" && state.selectedReferenceFile)')
    idx_else = gen_block.index("/api/voices/generate-with-audio", idx_branch)
    form_block = gen_block[idx_branch:idx_else]
    assert 'formData.append("reference_audio", state.selectedReferenceFile)' in form_block
    assert 'formData.append("preset_voice"' not in form_block

    # Layer 1c: switching to curated clears `state.selectedReferenceFile`
    idx_sw = APP_JS.index("function switchVoxCPMSubMode(mode)")
    sw_block = APP_JS[idx_sw : idx_sw + 1400]
    assert 'if (mode === "curated")' in sw_block
    assert "state.selectedReferenceFile = null;" in sw_block


@pytest.mark.asyncio
async def test_part1_queue_and_api_reject_both_preset_voice_and_reference_audio(
    db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(api_mod, "DISK_HEADROOM_MIN_FREE_BYTES", 0)

    user, _ = await get_or_create_user(db_session, telegram_id=998101)
    user.account_category = AccountCategory.PURCHASED.value
    bal = await get_minute_balance(db_session, user.id)
    bal.purchased_seconds = 900
    await db_session.commit()

    # Layer 2a: queue.create_voice_clone_job enforces the hard invariant
    with pytest.raises(ValueError, match="source_audio_path"):
        await queue_mod.create_voice_clone_job(
            db_session,
            user.id,
            text="စမ်းသပ် စာသား ဖြစ်ပါတယ်။",
            engine="voxcpm",
            preset_voice="komin",
            source_audio_path=str(tmp_path / "upload.wav"),
        )

    # Layer 2b: POST /api/voices/generate-with-audio rejects preset_voice + reference_audio (400)
    # without leaving orphan files in storage/temp/voxcpm_refs
    upload = UploadFile(filename="contam.mp3", file=io.BytesIO(b"ID3" + b"\x00" * 1024))
    with pytest.raises(HTTPException) as exc_upload:
        await api_mod.create_voice_job_with_audio(
            text="စမ်းသပ် စာသား ဖြစ်ပါတယ်။",
            model_slug="voxcpm-cloud-ai",
            speed="+12%",
            gender_preset="m2m",
            engine="voxcpm",
            preset_voice="komin",
            reference_audio=upload,
            user=user,
            db=db_session,
            _gate=user,
        )
    assert exc_upload.value.status_code == 400
    ref_dir = tmp_path / "temp" / "voxcpm_refs"
    assert not ref_dir.exists() or list(ref_dir.glob("*")) == []

    # Layer 2c: POST /api/voices/generate rejects preset_voice + voice_ref_id (400)
    ref_dir.mkdir(parents=True, exist_ok=True)
    ref_id = f"webapp_ref_u{user.id}_aabbccddeeff"
    (ref_dir / f"{ref_id}.wav").write_bytes(b"RIFF" + b"\x00" * 512)
    req_both = api_mod.VoiceGenerateRequest(
        text="စမ်းသပ် စာသား ဖြစ်ပါတယ်။",
        engine="voxcpm",
        preset_voice="komin",
        voice_ref_id=ref_id,
    )
    with pytest.raises(HTTPException) as exc_json:
        await api_mod.create_voice_job(req_both, user=user, db=db_session, _gate=user)
    assert exc_json.value.status_code == 400


@pytest.mark.asyncio
async def test_part1_worker_layer_guards_contaminated_job_row(
    db_session, tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    user, _ = await get_or_create_user(db_session, telegram_id=998102)
    user.account_category = AccountCategory.PURCHASED.value
    bal = await get_minute_balance(db_session, user.id)
    bal.purchased_seconds = 900

    model = VoiceModel(
        name="VoxCPM Cloud",
        slug="voxcpm-cloud-ai",
        type="SYSTEM",
        status="ACTIVE",
        allowed_for_free=True,
        allowed_for_purchased=True,
        is_default_free=True,
        pth_path="voxcpm",
        model_dir="cloud",
    )
    db_session.add(model)
    await db_session.flush()

    contam_ref = _make_tone_wav(tmp_path / "contam_upload.wav", duration_s=4.0, hz=200.0)
    # Construct a legacy/contaminated Job row directly in DB carrying BOTH fields
    job = Job(
        id="job-contam-p0",
        user_id=user.id,
        model_id=model.id,
        job_type=JobType.VOICE_CLONE.value,
        status=JobStatus.RUNNING.value,
        engine="voxcpm",
        input_text="ဒါက သန့်ရှင်းတဲ့ စမ်းသပ် စာကြောင်း ဖြစ်ပါတယ်။",
        preset_voice="komin",
        source_audio_path=str(contam_ref),
        speed_preset="+0%",
        gender_preset="m2m",
    )
    db_session.add(job)
    await db_session.commit()

    captured: dict = {}

    def _spy_synth_job(eng, **kwargs):
        captured.update(kwargs)
        return _make_tone_wav(Path(kwargs["output_wav"]), duration_s=2.0, hz=120.0)

    monkeypatch.setattr(worker_mod, "_synthesize_voxcpm_voice_job", _spy_synth_job)
    monkeypatch.setattr(
        worker_mod._hf_ledger,
        "estimate_voxcpm_units_and_budget",
        lambda **kw: (1, 30.0, 30.0),
    )

    with caplog.at_level(logging.WARNING, logger="app.worker"):
        ok = await worker_mod.process_single_job(db_session, job, bot=None)

    assert ok is True
    # Worker stripped contaminated `source_audio_path` so `reference_wav` is None (pure komin)
    assert captured["reference_wav"] is None
    assert captured["preset_voice"] == "komin"
    assert any("P0 mutual-exclusion guard" in r.getMessage() for r in caplog.records)


# ── Part 2: Space App Conditioning Routing (`session_space/app.py`) ──────────


def _load_session_space_module(monkeypatch):
    class _Ctx:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Comp:
        def __init__(self, *a, **kw):
            pass

        def click(self, *a, **kw):
            pass

    fake_spaces = types.ModuleType("spaces")

    def _gpu_decorator(fn=None, *, duration=None, **_kw):
        if fn is None:
            def _wrap(f):
                f._declared_gpu_duration = duration
                return f

            return _wrap
        fn._declared_gpu_duration = duration
        return fn

    fake_spaces.GPU = _gpu_decorator
    monkeypatch.setitem(sys.modules, "spaces", fake_spaces)

    fake_gr = types.ModuleType("gradio")
    fake_gr.Blocks = _Ctx
    fake_gr.Row = _Ctx
    fake_gr.Column = _Ctx
    for name in ("Markdown", "Textbox", "Audio", "Checkbox", "Slider", "Button", "Number"):
        setattr(fake_gr, name, lambda *a, **kw: _Comp(*a, **kw))
    monkeypatch.setitem(sys.modules, "gradio", fake_gr)

    class _FakeTensor:
        def sum(self):
            return self

        def item(self):
            return 1.0

    fake_torch = types.ModuleType("torch")
    fake_torch.compile = lambda m, *a, **k: m
    fake_torch.Tensor = _FakeTensor
    fake_torch.float32 = np.float32
    fake_torch.from_numpy = lambda arr: arr
    fake_torch.inference_mode = _Ctx
    fake_torch.cuda = SimpleNamespace(is_available=lambda: False, empty_cache=lambda: None)
    fake_torch.ones = lambda *_a, **_kw: _FakeTensor()
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    fake_sf = types.ModuleType("soundfile")
    fake_sf.read = lambda _p: (np.zeros(240, dtype=np.float32), 24000)
    monkeypatch.setitem(sys.modules, "soundfile", fake_sf)

    fake_ta = types.ModuleType("torchaudio")
    fake_ta.load = None
    monkeypatch.setitem(sys.modules, "torchaudio", fake_ta)

    fake_voxcpm = types.ModuleType("voxcpm")
    fake_voxcpm.VoxCPM = SimpleNamespace(from_pretrained=lambda *_a, **_kw: None)
    monkeypatch.setitem(sys.modules, "voxcpm", fake_voxcpm)

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "session_space_app_test", ROOT / "session_space" / "app.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_part2_session_space_conditioning_routing_ref_continuation_vs_reference_only(
    tmp_path, monkeypatch
):
    sp_mod = _load_session_space_module(monkeypatch)
    ref_wav = _make_tone_wav(tmp_path / "ref.wav", duration_s=3.0)

    gen_calls: list[dict] = []

    class _FakeModel:
        def __init__(self):
            self.tts_model = SimpleNamespace(sample_rate=44100)

        def generate(self, **kwargs):
            gen_calls.append(dict(kwargs))
            return np.ones(4410, dtype=np.float32) * 0.2

    monkeypatch.setattr(sp_mod, "load_model", lambda: _FakeModel())

    # 1. `use_prompt_text=True` + non-empty prompt_text -> `ref_continuation`
    sr1, audio1 = sp_mod._generate_impl(
        text_input="ပထမ စာကြောင်း။ ဒုတိယ စာကြောင်း။",
        control_instruction="",
        reference_wav_path_input=str(ref_wav),
        use_prompt_text=True,
        prompt_text_input="  နေ့စဉ် ပြောဆိုနေတဲ့ စကားသံ။  ",
        cfg_value_input=2.0,
        do_normalize=True,
        denoise=True,
    )
    assert sr1 == 44100 and audio1.size > 4410
    assert len(gen_calls) == 2
    for call in gen_calls:
        assert call["reference_wav_path"] == str(ref_wav)
        assert call["prompt_wav_path"] == str(ref_wav)
        assert call["prompt_text"] == "နေ့စဉ် ပြောဆိုနေတဲ့ စကားသံ။"

    # 2. `use_prompt_text=False` (or blank prompt_text) -> `reference` mode ONLY
    #    (NO `prompt_wav_path`, NO `prompt_text` — fixes VoxCPM._generate ValueError!)
    gen_calls.clear()
    sr2, audio2 = sp_mod._generate_impl(
        text_input="တတိယ စာကြောင်း။",
        control_instruction="Clone speaker",
        reference_wav_path_input=str(ref_wav),
        use_prompt_text=False,
        prompt_text_input="ignored text",
        cfg_value_input=2.0,
        do_normalize=True,
        denoise=True,
    )
    assert sr2 == 44100 and audio2.size > 0
    assert len(gen_calls) == 1
    assert gen_calls[0]["reference_wav_path"] == str(ref_wav)
    assert "prompt_wav_path" not in gen_calls[0]
    assert "prompt_text" not in gen_calls[0]

    # 3. Round 1 `gpu_seconds_used` return & `probe_quota` & GPU durations untouched
    (sr3, _a3), gpu_s = sp_mod.generate_session(
        "တတိယ စာကြောင်း။", "", str(ref_wav), False, "", 2.0, True, True
    )
    assert sr3 == 44100 and isinstance(gpu_s, float) and gpu_s >= 0.0
    assert hasattr(sp_mod, "probe_quota")
    assert sp_mod.generate._declared_gpu_duration is sp_mod._dyn_duration
    assert sp_mod.generate_session._declared_gpu_duration is sp_mod._session_duration


# ── Part 3: 6 AI Personas Trimmed Cleanly ────────────────────────────────────


def test_part3_six_ai_personas_have_no_greeting_and_clean_zero_crossing():
    pids = ("kothura", "kozayar", "komyat", "kophone", "mayoon", "machaw")
    assets_dir = ROOT / "app/assets/audio/references"
    static_dir = ROOT / "app/webapp/static/audio/references"

    for pid in pids:
        path, transcript = cv_mod.get_persona_reference(pid)
        assert path is not None
        assert "မင်္ဂလာပါ" not in transcript, pid
        assert transcript.startswith("နေ့စဉ် ပြောဆိုနေတဲ့"), pid

        w_a = assets_dir / f"{pid}.wav"
        w_s = static_dir / f"{pid}.wav"
        t_a = assets_dir / f"{pid}.txt"
        t_s = static_dir / f"{pid}.txt"
        assert w_a.read_bytes() == w_s.read_bytes(), pid
        assert t_a.read_bytes() == t_s.read_bytes(), pid

        with wave.open(str(w_a), "rb") as wf:
            assert (wf.getframerate(), wf.getnchannels(), wf.getsampwidth()) == (44100, 1, 2)
            pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype="<i2")
            dur = len(pcm) / 44100.0
        assert 12.0 < dur < 21.0, (pid, dur)
        # Clean zero-crossing start (no mid-waveform click)
        assert abs(int(pcm[0])) <= 2, (pid, int(pcm[0]))


# ── Part 5: Uploads Reference-Only + Silence-Snapped Slice + Bloat Guard + Setting ──


def test_part5_steady_slice_snaps_to_silence_pause_and_applies_25ms_cosine_fades(tmp_path):
    sr = 16000
    t = np.arange(sr * 16, dtype=np.float32) / float(sr)
    sig = 0.30 * np.sin(2.0 * np.pi * 145.0 * t) + 0.10 * np.sin(4.0 * np.pi * 145.0 * t)
    # Make first 2.6 s unsteady F0 so best 10 s window starts at 3.0 s and ends at 13.0 s
    sig[: int(2.6 * sr)] = 0.30 * np.sin(
        2.0 * np.pi * (190.0 + 55.0 * np.sin(2.0 * np.pi * 2.0 * t[: int(2.6 * sr)])) * t[: int(2.6 * sr)]
    )
    # Plant clean 160 ms low-RMS pauses at [3.35s, 3.51s] (+0.43s from 3.0s) and [12.50s, 12.66s] (-0.42s from 13.0s)
    sig[int(3.35 * sr) : int(3.51 * sr)] = 0.0
    sig[int(12.50 * sr) : int(12.66 * sr)] = 0.0

    raw_wav = _write_pcm_wav(tmp_path / "upload_raw.wav", sig, sr=sr)
    out_slice, dur_s, _stats = cp_mod.select_steady_reference_slice(
        raw_wav, tmp_path / "work", target_sec=10.0
    )
    assert out_slice.is_file()
    # Expected snapped span: ~3.43s -> ~12.58s => ~9.15s (distinct from unsnapped 10.00s!)
    assert 9.00 <= dur_s <= 9.30, dur_s

    sliced_samples, out_sr = cp_mod.read_wav_mono_float32(out_slice)
    assert out_sr == sr
    # First & last samples are attenuated by the 25 ms cosine fade (sample[0] == 0, sample[-1] == 0)
    assert abs(float(sliced_samples[0])) == 0.0
    assert abs(float(sliced_samples[-1])) == 0.0
    # Endpoints landed inside the planted low-RMS pauses
    head_rms = float(np.sqrt(np.mean(sliced_samples[: int(0.04 * sr)] ** 2)))
    tail_rms = float(np.sqrt(np.mean(sliced_samples[-int(0.04 * sr) :] ** 2)))
    assert head_rms < 0.01
    assert tail_rms < 0.01


def test_part5_custom_upload_and_own_voice_recap_never_call_asr_and_disable_prompt_text(
    tmp_path, monkeypatch
):
    asr_Boom = AsyncMock(side_effect=AssertionError("ASR must NOT be called on custom uploads"))
    monkeypatch.setattr(cp_mod, "transcribe_reference_slice_checked", asr_Boom)
    monkeypatch.setattr(worker_mod, "is_wav_edge_clipped", lambda _p: False)
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **kw: (Path(out).write_bytes(Path(raw).read_bytes()), Path(out))[1],
    )

    ref_wav = _make_tone_wav(tmp_path / "custom_ref.wav", duration_s=5.0, hz=160.0)
    seen_kw: list[dict] = []

    def _spy_synth(**kwargs):
        seen_kw.append(dict(kwargs))
        return _make_tone_wav(Path(kwargs["output_wav"]), duration_s=2.0, hz=160.0)

    fake_eng = SimpleNamespace(synthesize=_spy_synth, reset_pinned_space=lambda: None)
    out = worker_mod._synthesize_voxcpm_voice_job(
        fake_eng,
        job_id="job_custom_ref_only",
        text="ဒါက ကိုယ်ပိုင်အသံဖြင့် စမ်းသပ်ထုတ်လုပ်ထားသော စာကြောင်းတစ်ကြောင်း ဖြစ်ပါတယ်။",
        output_wav=tmp_path / "custom_out.wav",
        reference_wav=ref_wav,
        control_instruction="",
        preset_voice=None,
        speed="+0%",
        progress_cb=None,
        gender_preset="m2m",
    )
    assert out.is_file()
    assert len(seen_kw) == 1
    assert seen_kw[0]["prompt_text"] == ""
    assert seen_kw[0]["control_instruction"] == vox_mod.DEFAULT_CLONE_FIDELITY_CONTROL

    # Own-voice recap path also never calls `transcribe_reference_slice_checked` and passes `prompt_text=""`
    from app.services.recap_engine import core as pkg_core
    from app.services.recap_engine import voxcpm_engine as recap_vox_mod

    src_vid = tmp_path / "src.mp4"
    src_vid.write_bytes(b"\x00" * 64)
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "segment_id": 1,
                        "source_index": 0,
                        "video_start": 0.0,
                        "video_end": 2.0,
                        "narration": "စမ်းသပ် စာသား။",
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    recap_prompts: list[str] = []

    class _RecapSpyVox:
        def __init__(self, **_kw):
            pass

        def _adapt_reference_for_clone(self, p):
            return Path(p)

        def synthesize(self, text, output_wav, **kw):
            recap_prompts.append(str(kw.get("prompt_text") or ""))
            return _make_tone_wav(Path(output_wav), duration_s=1.0)

    monkeypatch.setattr(recap_vox_mod, "VoxCPMEngine", _RecapSpyVox)
    sync = pkg_core.EdgeSmartSync(
        job_dir=str(tmp_path / "recap_job"),
        source_path=str(src_vid),
        plan_path=str(plan_file),
        voice="voxcpm_komin",
        overlays={"tts_engine": "voxcpm", "voxcpm_ref_audio": str(ref_wav)},
        render_final_video=False,
        tts_workers=1,
        log=lambda _m: None,
    )
    sync.run()
    assert recap_prompts == [""]


def test_part5_duration_bloat_guard_warns_retries_once_and_never_fails_beyond_that(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(settings, "clone_session_slices_enabled", False)
    monkeypatch.setattr(worker_mod, "is_wav_edge_clipped", lambda _p: False)
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **kw: (Path(out).write_bytes(Path(raw).read_bytes()), Path(out))[1],
    )

    # >= 80 chars Burmese unit
    long_unit = (
        "ဒီနေ့မှာတော့ စိတ်ဝင်စားဖွယ်ကောင်းတဲ့ အကြောင်းအရာတစ်ခုကို အသေးစိတ် ရှင်းလင်းပြောပြပေးသွားမှာ ဖြစ်ပါတယ်။"
    )
    assert len(long_unit) >= 80

    # Case 1: First call bloated (30s => ~3.3 chars/s < 4.5), retry clean (7s => ~14 chars/s)
    calls_1 = {"n": 0}

    def _bloat_then_clean(**kw):
        calls_1["n"] += 1
        dur = 30.0 if calls_1["n"] == 1 else 7.0
        return _make_tone_wav(Path(kw["output_wav"]), duration_s=dur)

    with caplog.at_level(logging.WARNING, logger="app.worker"):
        out1 = worker_mod._synthesize_voxcpm_voice_job(
            SimpleNamespace(synthesize=_bloat_then_clean, reset_pinned_space=lambda: None),
            job_id="job_bloat_recover",
            text=long_unit,
            output_wav=tmp_path / "bloat1.wav",
            reference_wav=None,
            control_instruction="Narrator",
            preset_voice="komin",
            speed="+0%",
            progress_cb=None,
        )
    assert out1.is_file()
    assert calls_1["n"] == 2
    assert worker_mod._probe_wav_duration_float(out1) == pytest.approx(7.0, abs=0.1)
    assert any("duration bloat detected" in r.getMessage() for r in caplog.records)

    # Case 2: Still bloated after 1 retry -> stops at 2 total calls (1 retry max), logs telemetry, succeeds!
    caplog.clear()
    calls_2 = {"n": 0}

    def _always_bloated(**kw):
        calls_2["n"] += 1
        return _make_tone_wav(Path(kw["output_wav"]), duration_s=30.0)

    with caplog.at_level(logging.WARNING, logger="app.worker"):
        out2 = worker_mod._synthesize_voxcpm_voice_job(
            SimpleNamespace(synthesize=_always_bloated, reset_pinned_space=lambda: None),
            job_id="job_bloat_persist",
            text=long_unit,
            output_wav=tmp_path / "bloat2.wav",
            reference_wav=None,
            control_instruction="Narrator",
            preset_voice="komin",
            speed="+0%",
            progress_cb=None,
        )
    assert out2.is_file()
    assert calls_2["n"] == 2
    assert any("persisted after 1 retry" in r.getMessage() for r in caplog.records)


def test_part5_dynamic_setting_voxcpm_reference_only_mode_forces_global_ref_only(
    tmp_path, monkeypatch
):
    # Registered in Settings, SETTING_TYPES, DYNAMIC_SETTING_KEYS, CONFIG_SETTING_KEYS; default False
    assert settings.voxcpm_reference_only_mode is False
    assert ds_mod.SETTING_TYPES.get("voxcpm_reference_only_mode") is bool
    assert "voxcpm_reference_only_mode" in ds_mod.DYNAMIC_SETTING_KEYS
    assert "voxcpm_reference_only_mode" in ds_mod.CONFIG_SETTING_KEYS
    assert vox_mod.is_voxcpm_reference_only_mode_enabled() is False

    # Flip True -> curated persona session slices ALSO drop prompt_text (`use_prompt_text=False`)
    monkeypatch.setattr(settings, "voxcpm_reference_only_mode", True)
    monkeypatch.setattr(settings, "clone_session_slices_enabled", True)
    assert vox_mod.is_voxcpm_reference_only_mode_enabled() is True

    import gradio_client

    monkeypatch.setattr(gradio_client, "handle_file", lambda p: f"FILE::{Path(p).name}")
    monkeypatch.setattr(
        vox_mod,
        "master_voxcpm_audio",
        lambda raw, out, **kw: (Path(out).write_bytes(Path(raw).read_bytes()), Path(out))[1],
    )
    raw_gen = _make_tone_wav(tmp_path / "space_raw.wav", duration_s=4.0)
    submits: list[dict] = []

    class _FakeClient:
        def __init__(self, sp: str, tok: str | None):
            self.sp = sp
            self.tok = tok

        def submit(self, **_kwargs):
            submits.append(dict(_kwargs))
            return SimpleNamespace(done=lambda: True, status=lambda: None, result=lambda: str(raw_gen))

    monkeypatch.setattr(
        vox_mod.VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, sp, hf_token=None: _FakeClient(sp, hf_token)),
    )

    eng = vox_mod.VoxCPMEngine(
        work_dir=tmp_path / "work_ref_only",
        spaces=["awints001/voxcpm2-a"],
        hf_tokens=["hf_tok_1"],
    )
    out = worker_mod._synthesize_voxcpm_voice_job(
        eng,
        job_id="job_global_ref_only",
        text="ဒါက စမ်းသပ် စာကြောင်း တစ်ခု ဖြစ်ပါတယ်။ " * 15,
        output_wav=tmp_path / "global_ref_only.wav",
        reference_wav=None,
        control_instruction="Storyteller",
        preset_voice="komin",
        speed="+0%",
        progress_cb=None,
    )
    assert out.is_file()
    assert len(submits) >= 1
    for sub in submits:
        assert sub["use_prompt_text"] is False
        assert sub["prompt_text_input"] == ""


# ── Part 7: Objective Gate Verifier (`scripts/prepare_ear_test_2026_11_27.py`) ──


def test_part7_objective_gate_catches_greetings_openers_sentence_mismatch_and_bad_rate(
    tmp_path, monkeypatch
):
    from scripts import prepare_ear_test_2026_11_27 as ear_mod

    s1_txt = "ဒါက ပထမ စာကြောင်း ဖြစ်ပါတယ်။ ဒါက ဒုတိယ စာကြောင်း ဖြစ်ပါတယ်။"
    w1 = _make_tone_wav(tmp_path / "slice1.wav", duration_s=2.0, hz=150.0)

    # Clean transcript + normal syllable_rate (e.g. 10.5 syl/s) -> PASS
    monkeypatch.setattr(
        cp_mod,
        "measure_prosody",
        lambda _p, text="": cp_mod.ProsodyStats(2.0, 2.0, 2.0, 150.0, 0.4, 21, 10.5),
    )
    monkeypatch.setattr(ear_mod, "_transcribe_wav_gemini", lambda _p: s1_txt)
    res_ok = ear_mod.evaluate_objective_gate(
        input_text=s1_txt, slice_wavs=[w1], slice_texts=[s1_txt]
    )
    assert res_ok["passed"] is True
    assert res_ok["opener_hits"] == []
    assert res_ok["sentence_parity_ok"] is True
    assert res_ok["all_syllable_rates_in_6_18"] is True

    # Greeting / opener leak ("ဟလို" / "မင်္ဂလာပါ") -> FAIL
    monkeypatch.setattr(
        ear_mod,
        "_transcribe_wav_gemini",
        lambda _p: "မင်္ဂလာပါ။ " + s1_txt,
    )
    res_leak = ear_mod.evaluate_objective_gate(
        input_text=s1_txt, slice_wavs=[w1], slice_texts=[s1_txt]
    )
    assert res_leak["passed"] is False
    assert any("မင်္ဂလာပါ" in h for h in res_leak["opener_hits"])
    assert res_leak["sentence_parity_ok"] is False

    # Out-of-bounds syllable_rate (< 6.0 syl/s) -> FAIL
    monkeypatch.setattr(ear_mod, "_transcribe_wav_gemini", lambda _p: s1_txt)
    monkeypatch.setattr(
        cp_mod,
        "measure_prosody",
        lambda _p, text="": cp_mod.ProsodyStats(8.0, 8.0, 8.0, 150.0, 0.4, 21, 3.2),
    )
    res_slow = ear_mod.evaluate_objective_gate(
        input_text=s1_txt, slice_wavs=[w1], slice_texts=[s1_txt]
    )
    assert res_slow["passed"] is False
    assert res_slow["all_syllable_rates_in_6_18"] is False


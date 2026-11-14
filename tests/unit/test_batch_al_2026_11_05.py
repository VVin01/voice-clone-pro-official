"""Batch AL (2026-11-05): recap speedup — parallel TTS pool, job-scoped
quota memory, pipelined part renders, single-pass overlay with RAM
probe, and parallel Gemini planning with audit-on-flag.

Invariants pinned here:
- Parallel TTS changes only WHEN a segment's audio arrives: given the
  same produced WAVs, per-segment durations, subtitle timings, timing
  assembly and fallback bookkeeping are identical to the serial path.
- A quota-dead token / 180 s gate-rejected (space, token) pair is never
  re-walked inside the same job; a quota-class failure never triggers
  the segment-level second retry-walk.
- Part renders start as soon as their own segment's audio exists, but
  encodes stay strictly one-at-a-time and in segment order.
- The overlay single-pass path engages only after a RAM probe passes;
  any probe failure / high RSS falls back to the Batch AB two-step.
- Planner: translation batches run up to 3 concurrent with source-side
  overlap context; audit_and_repair runs on detector flags + every 4th
  batch, against true neighbouring translations; batch commits stay in
  order (checkpoint prefix semantics preserved).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path

import pytest

from app.services import voxcpm_engine as vx
from app.services.voxcpm_engine import VoxCPMEngine
from app.services.recap_engine import core
from app.services.recap_engine import recap_planner as planner


# ── shared helpers ───────────────────────────────────────────────────

def _write_wav(path: Path, seconds: float = 0.3, rate: int = 24000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


def _sine_wav(path: Path, seconds: float, freq: int = 660) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"sine=frequency={freq}:duration={seconds}",
         "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", str(path)],
        check=True,
    )
    return path


class _FakeJob:
    def __init__(self, mode: str, result_path=None):
        self.mode = mode
        self._result_path = result_path

    def done(self):
        return True

    def status(self):
        return None

    def result(self):
        return str(self._result_path)


class _RecClient:
    """Fake gradio client: behaviour per (space, token); records attempts."""

    def __init__(self, behaviour: str, result_path, record: list, key):
        self.behaviour = behaviour
        self._result_path = result_path
        self.record = record
        self.key = key

    def submit(self, **kwargs):
        self.record.append(self.key)
        if self.behaviour == "gate":
            raise RuntimeError(
                "ZeroGPU quota: 180s requested but the account only has 42s remaining"
            )
        if self.behaviour == "quota":
            raise RuntimeError("402 Payment Required: ZeroGPU quota exhausted for this account")
        if self.behaviour == "boom":
            raise RuntimeError("space exploded")
        return _FakeJob("ok", self._result_path)


def _vox_engine(tmp_path, monkeypatch, *, tokens, spaces=("spaceA", "spaceB"), **kw):
    monkeypatch.setattr(vx, "get_available_hf_tokens", lambda: list(tokens))
    monkeypatch.setattr(VoxCPMEngine, "_token_cooldowns", {})
    # Batch DI: the balance-ordered walk keeps process-level
    # balance state — reset it with the cooldowns so learned
    # balances never leak between walk tests.
    monkeypatch.setattr(VoxCPMEngine, "_token_balances", {})
    monkeypatch.setattr(VoxCPMEngine, "_space_admission_prices", {})
    monkeypatch.setattr(VoxCPMEngine, "_last_admission_price", None)
    monkeypatch.setattr(VoxCPMEngine, "_token_rr_counter", 0)
    engine = VoxCPMEngine(work_dir=tmp_path / "cache", **kw)
    engine.spaces = list(spaces)
    engine.hf_tokens = list(tokens)
    return engine


def _patch_vox_clients(monkeypatch, behaviours: dict, result_path, record: list):
    clients = {
        key: _RecClient(behaviour, result_path, record, key)
        for key, behaviour in behaviours.items()
    }
    monkeypatch.setattr(
        VoxCPMEngine,
        "get_client",
        classmethod(lambda cls, space, hf_token=None: clients[(space, hf_token)]),
    )
    return clients


# ── B: job-scoped quota memory ───────────────────────────────────────

def test_job_quota_memory_gate_and_dead():
    mem = vx.JobQuotaMemory()
    assert not mem.gate_blocked("spaceA", "hf_aaa")
    assert not mem.token_dead("hf_aaa")
    mem.record_gate("spaceA", "hf_aaa")
    assert mem.gate_blocked("spaceA", "hf_aaa")
    assert not mem.gate_blocked("spaceB", "hf_aaa")  # gate is per (space, token)
    assert not mem.token_dead("hf_aaa")
    mem.record_dead_token("hf_aaa")
    assert mem.token_dead("hf_aaa")
    # the anonymous caller is tracked separately and never crashes
    mem.record_gate("spaceA", None)
    assert mem.gate_blocked("spaceA", None)


def test_gate_rejection_is_remembered_for_the_job(tmp_path, monkeypatch):
    result = _write_wav(tmp_path / "ok.wav", seconds=0.3)
    record: list = []
    engine = _vox_engine(tmp_path, monkeypatch, tokens=["hf_aaa"])
    _patch_vox_clients(monkeypatch, {
        ("spaceA", "hf_aaa"): "gate",
        ("spaceB", "hf_aaa"): "ok",
        ("spaceA", None): "boom",
        ("spaceB", None): "ok",
    }, result, record)

    out = engine.synthesize(text="first line", output_wav=tmp_path / "o1.wav", allow_fallback=False)
    assert out.is_file()
    # SUPERSEDED by Batch AT (AT-1, spec ~/workspace/batch-at-spec-2026-11-05.md):
    # the pre-AT pin expected the space-major walk
    # [("spaceA", "hf_aaa"), ("spaceA", None), ("spaceB", "hf_aaa")] —
    # spaceA was exhausted before spaceB got its first attempt. The
    # walk is now space-first round-robin, so spaceB serves the line
    # on its first round and the anonymous spaceA pair is never
    # reached. The AL invariant this test exists for (the gate
    # pair is remembered, below) is unchanged.
    assert record == [("spaceA", "hf_aaa"), ("spaceB", "hf_aaa")]

    record.clear()
    out = engine.synthesize(text="second line", output_wav=tmp_path / "o2.wav", allow_fallback=False)
    assert out.is_file()
    # The gate-rejected pair is NOT re-walked in this job; other pairs are.
    assert ("spaceA", "hf_aaa") not in record
    assert ("spaceB", "hf_aaa") in record
    # A gate rejection must NOT put the token on the global 30-min cooldown
    # (that stays reserved for true quota exhaustion, as before AL).
    assert VoxCPMEngine._token_cooldowns.get("hf_aaa", 0) <= time.time()


def test_dead_token_skipped_everywhere_and_quota_error_raised(tmp_path, monkeypatch):
    result = _write_wav(tmp_path / "ok.wav", seconds=0.3)
    record: list = []
    engine = _vox_engine(tmp_path, monkeypatch, tokens=["hf_aaa", "hf_bbb"])
    behaviours = {
        ("spaceA", "hf_aaa"): "quota",
        ("spaceB", "hf_aaa"): "quota",
        ("spaceA", "hf_bbb"): "ok",
        ("spaceB", "hf_bbb"): "ok",
        ("spaceA", None): "quota",
        ("spaceB", None): "quota",
    }
    _patch_vox_clients(monkeypatch, behaviours, result, record)

    out = engine.synthesize(text="line one", output_wav=tmp_path / "o1.wav", allow_fallback=False)
    assert out.is_file()
    # hf_aaa died on spaceA -> never tried on spaceB within the same call.
    assert record.count(("spaceB", "hf_aaa")) == 0
    assert record.count(("spaceA", "hf_aaa")) == 1

    # Second call: everything now fails quota-class. hf_aaa must not be
    # attempted at all (job memory), and the failure must surface as the
    # quota-specific error so callers do not retry-walk.
    behaviours[("spaceA", "hf_bbb")] = "quota"
    behaviours[("spaceB", "hf_bbb")] = "quota"
    _patch_vox_clients(monkeypatch, behaviours, result, record)
    record.clear()
    with pytest.raises(vx.VoxCPMQuotaExhaustedError):
        engine.synthesize(text="line two", output_wav=tmp_path / "o2.wav", allow_fallback=False)
    assert all(tok != "hf_aaa" for _sp, tok in record)

    # Third call: every pair is already memo-blocked -> immediate quota
    # error with zero new attempts.
    record.clear()
    with pytest.raises(vx.VoxCPMQuotaExhaustedError):
        engine.synthesize(text="line three", output_wav=tmp_path / "o3.wav", allow_fallback=False)
    assert record == []


def test_non_quota_failure_is_not_a_quota_error(tmp_path, monkeypatch):
    result = _write_wav(tmp_path / "ok.wav", seconds=0.3)
    record: list = []
    engine = _vox_engine(tmp_path, monkeypatch, tokens=["hf_aaa"])
    _patch_vox_clients(monkeypatch, {
        ("spaceA", "hf_aaa"): "boom",
        ("spaceB", "hf_aaa"): "boom",
        ("spaceA", None): "boom",
        ("spaceB", None): "boom",
    }, result, record)
    with pytest.raises(RuntimeError) as excinfo:
        engine.synthesize(text="line", output_wav=tmp_path / "o.wav", allow_fallback=False)
    assert not isinstance(excinfo.value, vx.VoxCPMQuotaExhaustedError)


def test_worker_engines_bind_to_different_tokens(tmp_path, monkeypatch):
    result = _write_wav(tmp_path / "ok.wav", seconds=0.3)
    record: list = []
    behaviours = {
        (sp, tok): "ok"
        for sp in ("spaceA", "spaceB")
        for tok in ("hf_aaa", "hf_bbb", None)
    }
    _patch_vox_clients(monkeypatch, behaviours, result, record)

    e0 = _vox_engine(tmp_path / "e0", monkeypatch, tokens=["hf_aaa", "hf_bbb"], token_bind_index=0)
    e1 = _vox_engine(tmp_path / "e1", monkeypatch, tokens=["hf_aaa", "hf_bbb"], token_bind_index=1)
    e0.synthesize(text="worker zero line", output_wav=tmp_path / "w0.wav", allow_fallback=False)
    e1.synthesize(text="worker one line", output_wav=tmp_path / "w1.wav", allow_fallback=False)
    first_tokens = [record[0][1], record[1][1]]
    assert first_tokens == ["hf_aaa", "hf_bbb"]


# ── B: failover budget scaling + no quota retry-walk ─────────────────

def test_failover_budget_scales_with_segment_length():
    # Batch CY (2026-11-09) re-sloped the AL formula: the AL slope
    # (20 s per window second, cap 240 s) saturated at a 10.5 s
    # window, so normal lines still got the flat 240 s. CY budgets
    # 25 s + 2.5 s per expected audio second, floored at 45 s and
    # capped at 150 s — the detailed CY pins live in
    # test_batch_cy_2026_11_09.py; the AL-era assertions below were
    # updated to the CY values.
    # ~3.7 s line: short lines sit on the 45 s floor (was 104 s
    # under AL, still far below the old flat 240 s).
    b = core.vox_failover_budget(3.7, 80)
    assert b == 45.0
    # Tiny line hits the floor (unchanged); huge window hits the
    # CY cap of 150 s (was the historical 240 s cap under AL).
    assert core.vox_failover_budget(0.2, 5) == 45.0
    assert core.vox_failover_budget(60.0, 900) == 150.0
    # Monotonic once above the floor (the AL pair 2.0/5.6 s now both
    # land on the shared floor, so the comparison uses longer lines).
    assert core.vox_failover_budget(8.0, 170) < core.vox_failover_budget(20.0, 420)


class _RetryFakeEngine:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def synthesize(self, **kw):
        self.calls.append(kw)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _bare_sync():
    sync = core.EdgeSmartSync.__new__(core.EdgeSmartSync)
    sync.log = lambda msg: None
    return sync


def test_quota_failure_gets_no_second_retry_walk(tmp_path):
    sync = _bare_sync()
    engine = _RetryFakeEngine([vx.VoxCPMQuotaExhaustedError("all quota dead")])
    produced, err = sync._vox_synthesize_with_retry(
        engine, speak="line", dst=tmp_path / "s.wav", ctrl_prompt="",
        persona_ref_wav=None, persona_ref_text="", preset_v=None, speed_float=1.1,
    )
    assert produced is None and isinstance(err, vx.VoxCPMQuotaExhaustedError)
    assert len(engine.calls) == 1  # no second walk for quota-class failures


def test_transient_failure_keeps_segment_retry_and_budget_is_passed(tmp_path):
    sync = _bare_sync()
    ok = _write_wav(tmp_path / "ok.wav")
    engine = _RetryFakeEngine([RuntimeError("boom"), ok])
    produced, err = sync._vox_synthesize_with_retry(
        engine, speak="line", dst=tmp_path / "s.wav", ctrl_prompt="",
        persona_ref_wav=None, persona_ref_text="", preset_v=None, speed_float=1.1,
        budget_seconds=104.0,
    )
    assert err is None and Path(produced) == ok
    assert len(engine.calls) == 2
    assert engine.calls[0].get("total_timeout") == 104.0


# ── A + C: parallel TTS pool, sync integrity, pipelined renders ──────

pytestmark_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")

_ORDINALS = ["ပထမ", "ဒုတိယ", "တတိယ", "စတုတ္ထ", "ပဉ္စမ", "ဆဋ္ဌမ"]
_SEG_TEXTS = [f"{o} စာကြောင်း ဖြစ်ပါတယ်" for o in _ORDINALS]
_SEG_DURATIONS = [0.7, 0.9, 0.8, 1.1, 0.65, 0.95]


def _seg_index(text: str) -> int:
    for i, ordinal in enumerate(_ORDINALS):
        if ordinal in text:
            return i
    raise AssertionError(f"unexpected segment text: {text!r}")


class _PoolFakeVox:
    """Deterministic fake VoxCPM: WAV duration is a pure function of the
    text, so serial and parallel runs must produce identical timings."""

    intervals: list = []
    lock = threading.Lock()
    delay = 0.0
    fail_ordinals: set = set()

    def __init__(self, **kw):
        self.bind = kw.get("token_bind_index")
        self.memory = kw.get("job_memory")

    def synthesize(self, text, output_wav, **kw):
        idx = _seg_index(text)
        start = time.monotonic()
        with _PoolFakeVox.lock:
            _PoolFakeVox.intervals.append([start, None, idx])
        try:
            if _PoolFakeVox.delay:
                time.sleep(_PoolFakeVox.delay)
            if _ORDINALS[idx] in _PoolFakeVox.fail_ordinals:
                raise RuntimeError("fake vox quota wall")
            return _sine_wav(Path(output_wav), _SEG_DURATIONS[idx], freq=440 + 40 * idx)
        finally:
            with _PoolFakeVox.lock:
                for iv in _PoolFakeVox.intervals:
                    if iv[2] == idx and iv[1] is None:
                        iv[1] = time.monotonic()


def _max_overlap(intervals) -> int:
    events = []
    for start, end, _text in intervals:
        if end is None:
            continue
        events.append((start, 1))
        events.append((end, -1))
    events.sort(key=lambda e: (e[0], -e[1]))
    cur = best = 0
    for _t, delta in events:
        cur += delta
        best = max(best, cur)
    return best


def _setup_pool_job(tmp_path: Path, n: int = 6) -> Path:
    job = tmp_path / "job"
    job.mkdir(parents=True, exist_ok=True)
    src = job / "source.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", f"testsrc2=duration={n}:size=320x180:rate=30",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(src)],
        check=True,
    )
    plan = {"segments": [
        {"video_start": float(i), "video_end": float(i + 1), "narration": _SEG_TEXTS[i]}
        for i in range(n)
    ]}
    (job / "smart_sync_plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return job


def _patch_pool(monkeypatch):
    import app.services.curated_voices as cv
    import app.services.recap_engine.voxcpm_engine as vox_mod

    monkeypatch.setattr(vox_mod, "VoxCPMEngine", _PoolFakeVox)

    def _fake_persona(text, persona_id, output_wav, speed=1.1, log_fn=None):
        idx = _seg_index(text)
        return _sine_wav(Path(output_wav), _SEG_DURATIONS[idx], freq=330)

    monkeypatch.setattr(cv, "synthesize_curated_persona", _fake_persona)
    _PoolFakeVox.intervals = []
    _PoolFakeVox.delay = 0.0
    _PoolFakeVox.fail_ordinals = set()


def _run_pool_job(job: Path, workers: int):
    sync = core.EdgeSmartSync(
        job_dir=str(job),
        source_path=str(job / "source.mp4"),
        plan_path=str(job / "smart_sync_plan.json"),
        voice="voxcpm_komin",
        output_ratio="Original",
        mirror_video=False,
        zoom_factor=1.0,
        overlays={},
        log=lambda m: None,
        tts_workers=workers,
    )
    return sync.run()


def _plan_of(job: Path) -> dict:
    return json.loads((job / "edge_tts_smart_sync" / "smart_sync_plan_edge_tts.json").read_text(encoding="utf-8"))


def _checkpoint_of(job: Path) -> dict:
    return json.loads((job / "edge_tts_smart_sync" / "checkpoint.json").read_text(encoding="utf-8"))


@pytestmark_ffmpeg
def test_parallel_tts_sync_integrity_matches_serial(tmp_path, monkeypatch):
    _patch_pool(monkeypatch)
    _PoolFakeVox.delay = 0.3
    job1 = _setup_pool_job(tmp_path / "serial")
    job2 = _setup_pool_job(tmp_path / "parallel")
    _run_pool_job(job1, workers=1)
    serial_intervals = list(_PoolFakeVox.intervals)
    _PoolFakeVox.intervals = []
    _run_pool_job(job2, workers=4)
    parallel_intervals = list(_PoolFakeVox.intervals)

    # Parallelism actually happened (and serial really was serial).
    assert _max_overlap(serial_intervals) == 1
    assert _max_overlap(parallel_intervals) >= 2

    p1, p2 = _plan_of(job1), _plan_of(job2)
    # THE invariant: identical per-segment timings and subtitle timing
    # (audio_file is a per-job-dir path — compare basenames).
    def _timing(rows):
        return [{**r, "audio_file": Path(r["audio_file"]).name} for r in rows]
    assert _timing(p1["tts_timing_segments"]) == _timing(p2["tts_timing_segments"])
    assert p1["subtitle_segments"] == p2["subtitle_segments"]
    assert p1["sync_segments"] == p2["sync_segments"]
    assert p1["actual_audio_duration"] == p2["actual_audio_duration"]
    c1, c2 = _checkpoint_of(job1), _checkpoint_of(job2)
    assert ({k: v["duration"] for k, v in c1["audio"].items()}
            == {k: v["duration"] for k, v in c2["audio"].items()})
    assert c1["fallback_segments"] == c2["fallback_segments"] == []


@pytestmark_ffmpeg
def test_parallel_tts_fallback_segments_stay_ordered(tmp_path, monkeypatch):
    _patch_pool(monkeypatch)
    _PoolFakeVox.fail_ordinals = {_ORDINALS[2]}  # segment 3 falls back
    job = _setup_pool_job(tmp_path / "fb")
    _run_pool_job(job, workers=4)
    plan = _plan_of(job)
    ckpt = _checkpoint_of(job)
    assert plan["fallback_segments"] == [3]
    assert ckpt["fallback_segments"] == [3]
    assert len(plan["tts_timing_segments"]) == 6
    starts = [t["audio_start"] for t in plan["tts_timing_segments"]]
    assert starts == sorted(starts)
    providers = [ckpt["audio"][k]["provider"] for k in sorted(ckpt["audio"])]
    assert providers[2] == "edge_fallback"
    assert providers[0] == "voxcpm"


@pytestmark_ffmpeg
def test_part_renders_pipeline_behind_tts(tmp_path, monkeypatch):
    _patch_pool(monkeypatch)
    _PoolFakeVox.delay = 0.5
    render_starts: list = []
    real_render = core.render_video_part

    def _recording_render(ffmpeg, source, dst, seg, target, encoder, *args, **kw):
        render_starts.append((segment_base(seg), time.monotonic()))
        return real_render(ffmpeg, source, dst, seg, target, encoder, *args, **kw)

    def segment_base(seg):
        return core.segment_basename(seg)

    monkeypatch.setattr(core, "render_video_part", _recording_render)
    job = _setup_pool_job(tmp_path / "pipe")
    _run_pool_job(job, workers=3)

    assert len(render_starts) == 6
    # Renders are issued strictly in segment order, one at a time...
    bases = [core.segment_basename({"video_start": float(i), "video_end": float(i + 1),
                                    "source_index": i, "segment_id": i + 1}) for i in range(6)]
    assert [b for b, _t in render_starts] == bases
    # ...and the FIRST part starts rendering before the LAST segment's
    # synthesis has finished (the pipeline overlap this batch exists for).
    last_synth_end = max(iv[1] for iv in _PoolFakeVox.intervals if iv[1] is not None)
    assert render_starts[0][1] < last_synth_end


# ── D: single-pass overlay with RAM probe ────────────────────────────

_OVERLAYS_AL = {
    "title_text": "Hello Title",
    "subtitle_cues": [[0.5, 2.5, "မြို့ကြီးဆီကို ထွက်ခွာပြီး"]],
    "subtitles_font_size": 16,
    "sub_style": "yellow",
    "sub_lines": 1,
    "sub_y": 0.85,
    "blur_boxes": [{"x": 0.05, "y": 0.85, "w": 0.90, "h": 0.16}],
    "blur_strength": 18,
}


def _capture_overlay_media(monkeypatch):
    calls = []

    def fake_run(cmd, *, timeout, what):
        calls.append(list(cmd))
        out = Path(cmd[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"x" * 6000)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(core, "_run_media", fake_run)
    return calls


def _graph_of(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


def _run_overlay(tmp_path, monkeypatch):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"0" * 100)
    out = tmp_path / "out.mp4"
    work = tmp_path / "w"
    result = core.apply_production_overlays(
        "ffmpeg", src, out, dict(_OVERLAYS_AL), "libx264", work,
        frame_size=(320, 240), layout={"pw": 320, "ph": 240, "ox": 0, "oy": 0},
        duration=4,
    )
    return result, out, work


def test_overlay_single_pass_when_probe_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_run_overlay_probe", lambda cmd, *, timeout: (0, 128 * 1024 * 1024, ""))
    calls = _capture_overlay_media(monkeypatch)
    result, out, work = _run_overlay(tmp_path, monkeypatch)
    assert result == out
    # ONE encode total: blur graph and cues live in the same graph.
    # (Batch DD: the title overlay is removed — no vtitle step.)
    assert len(calls) == 1
    graph = _graph_of(calls[0])
    assert "boxblur" in graph and "vtitle" not in graph and "between(t," in graph
    assert calls[0][-1] == str(out)
    assert not (work / "overlay_step1_blurred.mp4").exists()
    idxs = [i for i, a in enumerate(calls[0]) if a == "-threads"]
    assert calls[0][idxs[-1] + 1] == str(core.OVERLAY_FFMPEG_THREADS)


def test_overlay_falls_back_to_two_step_on_high_probe_rss(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_run_overlay_probe", lambda cmd, *, timeout: (0, core.OVERLAY_SINGLE_PASS_MAX_RSS_BYTES + 1, ""))
    calls = _capture_overlay_media(monkeypatch)
    result, out, work = _run_overlay(tmp_path, monkeypatch)
    assert result == out
    assert len(calls) == 2
    assert calls[0][-1] == str(work / "overlay_step1_blurred.mp4")
    assert "boxblur" in _graph_of(calls[0]) and "vtitle" not in _graph_of(calls[0])


def test_overlay_falls_back_to_two_step_when_probe_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "_run_overlay_probe", lambda cmd, *, timeout: (1, 0, "encode boom"))
    calls = _capture_overlay_media(monkeypatch)
    result, out, work = _run_overlay(tmp_path, monkeypatch)
    assert result == out
    assert len(calls) == 2


def test_overlay_probe_helper_measures_peak_rss(tmp_path):
    # Real helper against a real (tiny) ffmpeg encode: succeeds and
    # reports a plausible peak RSS.
    src = tmp_path / "in.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=duration=1:size=160x90:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(src)],
        check=True,
    )
    out = tmp_path / "probe_out.mp4"
    rc, peak, _err = core._run_overlay_probe(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
        timeout=60,
    )
    assert rc == 0
    assert peak > 1024 * 1024  # at least a few MB for a real encoder process
    assert out.is_file()


# ── E: parallel planning + audit-on-flag ─────────────────────────────

def _units(n: int, per_batch: int = 3):
    units = []
    for i in range(n):
        units.append({
            "id": i + 1,
            "timestamp": f"00:00:{i:02d},000 --> 00:00:{i + 1:02d},000",
            "source_text": f"Source line {i + 1} about the story.",
            "speech_start": float(i),
            "speech_end": float(i + 1),
        })
    batches = [units[i:i + per_batch] for i in range(0, n, per_batch)]
    unit_index = {int(u["id"]): i for i, u in enumerate(units)}
    return units, batches, unit_index


class _PlannerRecorder:
    def __init__(self):
        self.lock = threading.Lock()
        self.translate_calls: list[dict] = []
        self.audit_calls: list[dict] = []
        self.commits: list[int] = []
        self.intervals: list = []


def _fake_translate_factory(rec: _PlannerRecorder, *, flagged_ids=(), fail_at_batch=None, delay=0.05):
    def fake_translate(ai, *, source_title="", story_context, previous_context,
                       current, next_context, log, narrative_style="narrator",
                       is_female=False, _split_depth=0, previous_context_is_source=False):
        start = time.monotonic()
        with rec.lock:
            rec.intervals.append([start, None, int(current[0]["id"])])
            rec.translate_calls.append({
                "first_id": int(current[0]["id"]),
                "prev_ids": [int(x["id"]) for x in previous_context],
                "prev_is_source": previous_context_is_source,
                "ai": ai,
            })
        try:
            if delay:
                time.sleep(delay)
            if fail_at_batch is not None and int(current[0]["id"]) == fail_at_batch:
                raise planner.GeminiKeyPoolExhaustedError("pool dead")
            rows = []
            for row in current:
                _my_id = str(int(row['id'])).translate(str.maketrans("0123456789", "၀၁၂၃၄၅၆၇၈၉"))
                text = f"မြန်မာ ဘာသာပြန် စာကြောင်း {_my_id} ဖြစ်ပါတယ်"
                if int(row["id"]) in flagged_ids:
                    text = "This line kept English words inside"
                rows.append({
                    "id": int(row["id"]),
                    "timestamp": row["timestamp"],
                    "source_text": row["source_text"],
                    "translation": text,
                })
            return rows
        finally:
            with rec.lock:
                for iv in rec.intervals:
                    if iv[2] == int(current[0]["id"]) and iv[1] is None:
                        iv[1] = time.monotonic()
    return fake_translate


def _fake_audit_factory(rec: _PlannerRecorder):
    def fake_audit(ai, translated, *, story_context, previous_context=None,
                   next_context=None, log, narrative_style="narrator", is_female=False):
        with rec.lock:
            rec.audit_calls.append({
                "first_id": int(translated[0]["id"]),
                "prev_ids": [int(x["id"]) for x in (previous_context or [])],
                "prev_translations": [x.get("translation") for x in (previous_context or [])],
            })
        return [dict(r) for r in translated]
    return fake_audit


def _run_planner_parallel(monkeypatch, rec: _PlannerRecorder, *, units, batches, unit_index,
                          initial=None, flagged_ids=(), fail_at_batch=None, audit=True):
    monkeypatch.setattr(planner, "translate_batch", _fake_translate_factory(
        rec, flagged_ids=flagged_ids, fail_at_batch=fail_at_batch))
    monkeypatch.setattr(planner, "audit_and_repair_batch", _fake_audit_factory(rec))
    return planner.translate_batches_in_parallel(
        ai="MAIN_AI",
        worker_ai_factory=lambda: "WORKER_AI",
        batches=batches,
        pending=list(range(len(batches))),
        units=units,
        unit_index=unit_index,
        initial_translated=list(initial or []),
        story_context={},
        source_title="T",
        log=lambda m: None,
        should_stop=lambda: False,
        context_overlap=2,
        lookahead=2,
        translation_audit=audit,
        narrative_style="narrator",
        is_female=False,
        max_workers=3,
        on_commit=lambda rows: rec.commits.append(len(rows)),
    )


def test_parallel_translation_commits_in_order_with_source_overlap(monkeypatch):
    units, batches, unit_index = _units(15, per_batch=3)  # 5 batches
    rec = _PlannerRecorder()
    out = _run_planner_parallel(monkeypatch, rec, units=units, batches=batches, unit_index=unit_index)
    assert [int(r["id"]) for r in out] == list(range(1, 16))
    # Commits grow batch by batch, in order (checkpoint prefix semantics).
    assert rec.commits == [3, 6, 9, 12, 15]
    # Translation really overlapped in time.
    assert _max_overlap(rec.intervals) >= 2
    by_first = {c["first_id"]: c for c in rec.translate_calls}
    # First batch: no previous context. Later batches: previous batch's
    # SOURCE units (flag set), not its translations.
    assert by_first[1]["prev_ids"] == [] and by_first[1]["prev_is_source"] is False
    assert by_first[4]["prev_is_source"] is True
    assert by_first[4]["prev_ids"] == [2, 3]  # overlap tail of batch 1's source units
    assert by_first[7]["prev_ids"] == [5, 6]
    # Audit-on-flag + Batch-1 hook spot-check (§2B).
    assert [c["first_id"] for c in rec.audit_calls] == [1]


def test_parallel_translation_audit_on_flag_and_true_neighbours(monkeypatch):
    units, batches, unit_index = _units(15, per_batch=3)
    rec = _PlannerRecorder()
    # ID 5 lives in batch 2 (IDs 4-6); its English-laced line must flag
    # the batch for audit even though batch 2 is not a sampled batch.
    out = _run_planner_parallel(monkeypatch, rec, units=units, batches=batches,
                                unit_index=unit_index, flagged_ids={5})
    assert [int(r["id"]) for r in out] == list(range(1, 16))
    audited = [c["first_id"] for c in rec.audit_calls]
    assert audited == [1, 4]  # Batch-1 hook spot-check + flagged batch 2 (§2B)
    # The audit saw TRUE neighbouring translations (batch 1's committed
    # rows), not source text.
    audit_b2 = next(c for c in rec.audit_calls if c["first_id"] == 4)
    assert audit_b2["prev_ids"] == [2, 3]
    assert all(t and "မြန်မာ" in t for t in audit_b2["prev_translations"])


def test_parallel_translation_pool_exhaustion_keeps_committed_prefix(monkeypatch):
    units, batches, unit_index = _units(15, per_batch=3)
    rec = _PlannerRecorder()
    with pytest.raises(planner.GeminiKeyPoolExhaustedError) as excinfo:
        _run_planner_parallel(monkeypatch, rec, units=units, batches=batches,
                              unit_index=unit_index, fail_at_batch=7)
    # Batches 1-2 (IDs 1-6) committed before the failure surfaced.
    assert rec.commits == [3, 6]
    assert "Saved progress: 6/15" in str(excinfo.value)


def test_parallel_translation_resume_prefix_is_true_context(monkeypatch):
    units, batches, unit_index = _units(9, per_batch=3)
    initial = [{
        "id": int(u["id"]), "timestamp": u["timestamp"],
        "source_text": u["source_text"],
        "translation": f"ရှိပြီးသား ဘာသာပြန် {int(u['id'])}",
    } for u in units[:3]]
    rec = _PlannerRecorder()
    monkeypatch.setattr(planner, "translate_batch", _fake_translate_factory(rec))
    monkeypatch.setattr(planner, "audit_and_repair_batch", _fake_audit_factory(rec))
    out = planner.translate_batches_in_parallel(
        ai="MAIN_AI", worker_ai_factory=lambda: "WORKER_AI",
        batches=batches, pending=[1, 2], units=units, unit_index=unit_index,
        initial_translated=initial, story_context={}, source_title="T",
        log=lambda m: None, should_stop=lambda: False,
        context_overlap=2, lookahead=2, translation_audit=True,
        narrative_style="narrator", is_female=False, max_workers=3,
        on_commit=lambda rows: rec.commits.append(len(rows)),
    )
    assert [int(r["id"]) for r in out] == list(range(1, 10))
    by_first = {c["first_id"]: c for c in rec.translate_calls}
    # First pending batch keeps the serial context: TRUE translations of
    # the resumed prefix. The next batch switches to source-side overlap.
    assert by_first[4]["prev_is_source"] is False
    assert by_first[4]["prev_ids"] == [2, 3]
    assert by_first[7]["prev_is_source"] is True


def test_seam_detectors_do_not_fire_on_clean_parallel_output(monkeypatch):
    units, batches, unit_index = _units(15, per_batch=3)
    rec = _PlannerRecorder()
    out = _run_planner_parallel(monkeypatch, rec, units=units, batches=batches, unit_index=unit_index)
    # Baseline guard: for clean deterministic translations the seam
    # detectors must stay silent over the assembled parallel output,
    # exactly as they would over a serial assembly of the same rows.
    assert planner.find_repeated_next_lines(out) == []
    assert planner.find_near_duplicates(out) == []


def test_translate_batch_source_side_previous_context_prompt(monkeypatch):
    captured = {}

    _TEXTS = {
        3: "လူကြီးတစ်ယောက် လမ်းလျှောက်ရင်း ဈေးဘက်ကို သွားနေခဲ့ပါတယ်",
        4: "ကောင်မလေးက နောက်ကနေ အပြေးအလွှား လိုက်လာခဲ့ပါတော့တယ်",
    }

    def fake_gemini_json(ai, prompt, retries=1, log=None, rotation_tier=None):
        captured["prompt"] = prompt
        return {"items": [
            {"id": row["id"], "translation": _TEXTS[int(row["id"])]}
            for row in CURRENT_E
        ]}

    monkeypatch.setattr(planner, "_gemini_json", fake_gemini_json)
    prev_source = [
        {"id": 1, "timestamp": "00:00:00,000 --> 00:00:02,000", "source_text": "PREV SOURCE ALPHA"},
        {"id": 2, "timestamp": "00:00:02,000 --> 00:00:04,000", "source_text": "PREV SOURCE BETA"},
    ]
    planner.translate_batch(
        None, source_title="T", story_context={},
        previous_context=prev_source, current=CURRENT_E, next_context=[],
        log=lambda m: None, narrative_style="narrator", is_female=False,
        previous_context_is_source=True,
    )
    prompt = captured["prompt"]
    assert "PREV SOURCE ALPHA" in prompt and "PREV SOURCE BETA" in prompt
    # Source-side context is rendered as source lines, not as fake
    # SOURCE/OUTPUT translation pairs.
    assert "OUTPUT:" not in prompt.split("CURRENT ITEMS")[0].split("PREVIOUS CONTEXT")[-1]


CURRENT_E = [
    {"id": 3, "timestamp": "00:00:04,000 --> 00:00:06,000", "source_text": "He runs."},
    {"id": 4, "timestamp": "00:00:06,000 --> 00:00:08,000", "source_text": "She follows."},
]


def test_planner_request_timeout_trimmed_to_60s(monkeypatch):
    captured = {}

    class _FakeAI:
        def _enable_proxy(self):
            pass

        def _disable_proxy(self):
            pass

    def factory(*, api_key, http_options):
        captured["http_options"] = http_options
        return object()

    ai = _FakeAI()
    planner._configure_gemini_key_pool(
        ai, [("GEMINI_API_KEY", "dummy-key")],
        client_factory=factory, log=lambda m: None,
        request_timeout_seconds=120,
    )
    # Batch AL trim: a hung model fails over sooner — 120 s request
    # configuration now clamps to a 60 s per-request timeout.
    assert captured["http_options"]["timeout"] == 60_000

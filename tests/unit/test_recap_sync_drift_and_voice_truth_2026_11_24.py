"""Tests for Design 1B (no atempo valve) + Design 2B — Recap sync drift + delivery voice truth (2026-11-24).

Covers:
1. Real-FFmpeg uniform_restart regression with distinctly coloured segments (Pass 0 renders >=2
   parts before restart fires -> zero duplicate parts, abs(picture - narration) <= 0.1s,
   pixel colour at every narration timestamp matches its own scene).
2. Hard sync gates (pre-overlay SyncDriftError > 0.30s, stale corrupted concat rejection,
   post-mux truncation SyncDriftError > 0.30s) + planner prevention (pass-1 MAX_SPOKEN_CHARS,
   0.30s breath floor with short-window safety, 3rd compression round + fail-closed > 1.35x,
   1.20x..1.35x absorption band, MEASURED_CHARS_PER_SECOND untouched at 17.6).
3. Canonical voice-presentation resolver matrix: all 5 note states x (my, en) x
   (completion DM, /send-telegram + _send_recap_telegram_delivery, /history), reference job
   exact inputs, healthy explicit-Gemini job (no note), all 8 Round-13 Edge voices,
   segmented multi-part _read_recap_voice_outcome, and 2B-3 cost telemetry attribution.
4. Reference Chocolate Museum 12-segment plan replay (exact 12 windows, 429 on lines 6/8/12
   after Pass 0 renders segs 1..4 -> 12 unique parts, sync gate <= 0.30s, honest Edge caption).
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.recap_engine import core as core_mod
from app.services.recap_engine import recap_planner as planner_mod
from app.services.voice_display import (
    edge_voice_display_name,
    resolve_recap_voice_presentation,
)
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]

pytestmark_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not available",
)


def _write_tone_wav(path: Path, seconds: float = 0.8, freq: int = 440, pad_silence: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if pad_silence > 0.0:
        tone_dur = max(0.15, seconds - 2.0 * pad_silence)
        flt = (
            f"aevalsrc=0:d={pad_silence:.3f}:s=48000[s1];"
            f"sine=frequency={freq}:duration={tone_dur:.3f}:sample_rate=48000[t];"
            f"aevalsrc=0:d={pad_silence:.3f}:s=48000[s2];"
            "[s1][t][s2]concat=n=3:v=0:a=1[a]"
        )
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-filter_complex", flt, "-map", "[a]",
                "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(path),
            ],
            check=True,
        )
    else:
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                "-i", f"sine=frequency={freq}:duration={seconds:.3f}",
                "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(path),
            ],
            check=True,
        )
    return path


def _make_colored_source(path: Path, segments_spec: list[tuple[str, float]], size: str = "160x90") -> Path:
    """Create an MP4 concatenating solid-colour blocks, one per (color, duration_s)."""
    parts_filter = []
    labels = []
    for i, (color, dur) in enumerate(segments_spec):
        parts_filter.append(
            f"color=c={color}:s={size}:r=30:d={dur:.3f},format=yuv420p[c{i}]"
        )
        labels.append(f"[c{i}]")
    graph = ";".join(parts_filter) + f";{''.join(labels)}concat=n={len(segments_spec)}:v=1:a=0[v]"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-filter_complex", graph, "-map", "[v]",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an",
            str(path),
        ],
        check=True,
    )
    return path


def _sample_center_rgb(video_path: Path, timestamp_sec: float) -> tuple[int, int, int]:
    """Decode one frame at timestamp_sec and return average RGB (0..255)."""
    proc = subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-ss", f"{timestamp_sec:.3f}",
            "-i", str(video_path),
            "-vf", "scale=4:4",
            "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    buf = proc.stdout
    assert len(buf) >= 3, f"no video frame decoded at t={timestamp_sec:.3f}s"
    px_count = len(buf) // 3
    r = sum(buf[0::3]) // px_count
    g = sum(buf[1::3]) // px_count
    b = sum(buf[2::3]) // px_count
    return (r, g, b)


# ===========================================================================
# 1. Missing real-FFmpeg uniform_restart regression (distinctly coloured scenes)
# ===========================================================================


@pytestmark_ffmpeg
def test_uniform_restart_colored_scenes_no_duplicate_parts_and_exact_scene_sync(tmp_path, monkeypatch):
    """Pass 0 finishes rendering >=2 video parts BEFORE Gemini 429 triggers uniform_restart.

    Verifies:
    - work/video_parts.txt contains exactly 4 unique parts (zero duplicates from Pass 0).
    - abs(picture - narration) <= 0.10s on both synced_video_noaudio and final.mp4.
    - The pixel colour at EVERY segment's narration midpoint matches that segment's scene
      (Red, Green, Blue, Yellow), proving zero timeline shift.
    - out_plan + voice_outcome truthfully record edge_tts / my-MM-ThihaNeural / uniform_restart=True.
    """
    import app.services.curated_voices as cv
    import app.services.recap_engine.gemini_tts_engine as gem_mod

    job = tmp_path / "colored_job"
    job.mkdir()
    src = _make_colored_source(
        job / "source.mp4",
        [("red", 1.5), ("green", 1.5), ("blue", 1.5), ("yellow", 1.5)],
    )
    plan = {
        "segments": [
            {"video_start": 0.0, "video_end": 1.5, "narration": "အနီရောင် အခန်း ဖြစ်ပါတယ်"},
            {"video_start": 1.5, "video_end": 3.0, "narration": "အစိမ်းရောင် အခန်း ဖြစ်ပါတယ်"},
            {"video_start": 3.0, "video_end": 4.5, "narration": "အပြာရောင် အခန်း ဖြစ်ပါတယ်"},
            {"video_start": 4.5, "video_end": 6.0, "narration": "အဝါရောင် အခန်း ဖြစ်ပါတယ်"},
        ]
    }
    (job / "smart_sync_plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    pass0_parts_rendered = threading.Event()
    render_count = {"n": 0}
    real_render = core_mod.render_video_part

    def _tracking_render(*args, **kwargs):
        res = real_render(*args, **kwargs)
        render_count["n"] += 1
        if render_count["n"] >= 2:
            pass0_parts_rendered.set()
        return res

    monkeypatch.setattr(core_mod, "render_video_part", _tracking_render)

    class _FakeEdge:
        def __init__(self, **kw):
            self.work_dir = Path(kw.get("work_dir") or ".")

        def generate_channel99_voice(self, text, voice):
            return _write_tone_wav(self.work_dir / f"edge_{abs(hash(text)) % 99999}.wav", seconds=1.0, freq=440)

    class _FakeGemini:
        def __init__(self, **kw):
            self.work_dir = Path(kw.get("work_dir") or ".")

        def generate_channel99_voice(self, text, voice):
            if "အဝါရောင်" in text:
                # Hold line 4 until Pass 0's pipelined renderer has actually completed >=2 parts!
                assert pass0_parts_rendered.wait(timeout=30), "Pass 0 renderer never finished >=2 parts"
                raise RuntimeError("429 RESOURCE_EXHAUSTED (gemini-3.8-flash-tts)")
            return _write_tone_wav(self.work_dir / f"gem_{abs(hash(text)) % 99999}.wav", seconds=0.9, freq=660)

    def _fake_persona(text, persona_id, output_wav, speed=1.1, log_fn=None):
        return _write_tone_wav(Path(output_wav), seconds=1.2, freq=520)

    monkeypatch.setattr(core_mod, "EdgeTTSEngine", _FakeEdge)
    monkeypatch.setattr(gem_mod, "GeminiTTSEngine", _FakeGemini)
    monkeypatch.setattr(gem_mod, "collect_gemini_keys", lambda: ["dummy-key"])
    monkeypatch.setattr(cv, "synthesize_curated_persona", _fake_persona)

    logs: list[str] = []
    sync = core_mod.EdgeSmartSync(
        job_dir=str(job),
        source_path=str(src),
        plan_path=str(job / "smart_sync_plan.json"),
        voice="voxcpm_komin",
        output_ratio="Original",
        mirror_video=False,
        zoom_factor=1.0,
        tts_workers=1,
        overlays={"tts_engine": "gemini", "gemini_voice": "Charon", "is_female": False, "burn_subtitles": False},
        log=logs.append,
    )
    result = sync.run()

    assert pass0_parts_rendered.is_set(), "Test precondition failed: Pass 0 must render >=2 parts before restart"

    work_dir = job / "edge_tts_smart_sync" / "work"
    part_lines = [
        ln.strip()
        for ln in (work_dir / "video_parts.txt").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert len(part_lines) == 4, f"Expected 4 unique parts, got {len(part_lines)}: {part_lines}"
    assert len(set(part_lines)) == 4, f"Duplicate parts in video_parts.txt: {part_lines}"

    paths = core_mod.make_paths(job)
    synced_vid = paths.synced_video_noaudio
    voice_wav = paths.voice
    final_mp4 = Path(result["final"])

    pic_dur = core_mod.probe_duration(sync.ffprobe, synced_vid)
    aud_dur = core_mod.probe_duration(sync.ffprobe, voice_wav)
    fin_dur = core_mod.probe_duration(sync.ffprobe, final_mp4)
    assert abs(pic_dur - aud_dur) <= 0.10, f"picture={pic_dur:.3f}s vs narration={aud_dur:.3f}s"
    assert abs(fin_dur - aud_dur) <= 0.10, f"final={fin_dur:.3f}s vs narration={aud_dur:.3f}s"
    assert any("✅ SYNC OK" in line for line in logs)

    out_plan = json.loads((job / "edge_tts_smart_sync" / "smart_sync_plan_edge_tts.json").read_text(encoding="utf-8"))
    assert out_plan["tts_provider"] == "edge_tts"
    assert out_plan["actual_voice"] == "my-MM-ThihaNeural"
    assert out_plan["uniform_restart"] is True
    assert out_plan["total_segments"] == 4
    assert result["voice_outcome"]["actual_tts_provider"] == "edge_tts"
    assert result["voice_outcome"]["actual_voice"] == "my-MM-ThihaNeural"
    assert result["voice_outcome"]["uniform_restart"] is True

    # Pixel-colour proof at every segment's narration midpoint:
    # seg1=Red, seg2=Green, seg3=Blue, seg4=Yellow
    t_segs = out_plan["tts_timing_segments"]
    assert len(t_segs) == 4
    midpoints = [(s["audio_start"] + s["audio_end"]) / 2.0 for s in t_segs]
    r0, g0, b0 = _sample_center_rgb(final_mp4, midpoints[0])
    r1, g1, b1 = _sample_center_rgb(final_mp4, midpoints[1])
    r2, g2, b2 = _sample_center_rgb(final_mp4, midpoints[2])
    r3, g3, b3 = _sample_center_rgb(final_mp4, midpoints[3])

    assert r0 > 180 and g0 < 60 and b0 < 60, f"seg 1 expected RED, got {(r0, g0, b0)}"
    assert g1 > 100 and r1 < 60 and b1 < 60, f"seg 2 expected GREEN, got {(r1, g1, b1)}"
    assert b2 > 180 and r2 < 60 and g2 < 60, f"seg 3 expected BLUE, got {(r2, g2, b2)}"
    assert r3 > 180 and g3 > 180 and b3 < 60, f"seg 4 expected YELLOW, got {(r3, g3, b3)}"


# ===========================================================================
# 2. Hard sync gates (pre-overlay SyncDriftError, stale concat, post-mux)
#    + Planner prevention (pass-1 hints, breath floor, 3rd round & >1.35x fail)
# ===========================================================================


def _setup_mini_edge_job(tmp_path: Path) -> tuple[Path, Path]:
    job = tmp_path / "gate_job"
    job.mkdir()
    src = _make_colored_source(job / "source.mp4", [("red", 1.5), ("blue", 1.5)])
    plan = {
        "segments": [
            {"video_start": 0.0, "video_end": 1.5, "narration": "ပထမ စာကြောင်း ဖြစ်ပါတယ်"},
            {"video_start": 1.5, "video_end": 3.0, "narration": "ဒုတိယ စာကြောင်း ဖြစ်ပါတယ်"},
        ]
    }
    (job / "smart_sync_plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return job, src


@pytestmark_ffmpeg
def test_pre_overlay_sync_drift_gate_raises_before_overlays_and_mux(tmp_path, monkeypatch):
    """Concat drift > 0.30s MUST raise SyncDriftError BEFORE overlays or mux run."""
    import app.services.curated_voices as cv

    job, src = _setup_mini_edge_job(tmp_path)
    monkeypatch.setattr(
        cv,
        "synthesize_curated_persona",
        lambda text, persona_id, output_wav, speed=1.1, log_fn=None: _write_tone_wav(Path(output_wav), seconds=0.8),
    )

    overlay_called = {"n": 0}
    mux_called = {"n": 0}

    def _drifted_concat(ffmpeg, parts, output, work_dir, *, total_duration=None, faststart=True):
        # Deliberately produce a 3.5s video against ~1.6s audio (+1.9s drift > 0.30s)
        _make_colored_source(output, [("red", 3.5)])

    monkeypatch.setattr(core_mod, "concat_video_parts", _drifted_concat)
    monkeypatch.setattr(
        core_mod,
        "apply_production_overlays",
        lambda *a, **kw: overlay_called.__setitem__("n", overlay_called["n"] + 1) or a[1],
    )
    monkeypatch.setattr(
        core_mod,
        "mux_audio",
        lambda *a, **kw: mux_called.__setitem__("n", mux_called["n"] + 1) or False,
    )

    sync = core_mod.EdgeSmartSync(
        job_dir=str(job),
        source_path=str(src),
        plan_path=str(job / "smart_sync_plan.json"),
        voice="voxcpm_komin",
        output_ratio="Original",
        mirror_video=False,
        zoom_factor=1.0,
        overlays={"tts_engine": "edge", "burn_subtitles": False},
        log=lambda _m: None,
    )
    with pytest.raises(core_mod.SyncDriftError, match="drift exceeded tolerance"):
        sync.run()

    assert overlay_called["n"] == 0, "Overlays must NOT run after post-concat SyncDriftError"
    assert mux_called["n"] == 0, "Mux must NOT run after post-concat SyncDriftError"
    ckpt = json.loads((job / "edge_tts_smart_sync" / "checkpoint.json").read_text(encoding="utf-8"))
    assert "concat" not in (ckpt.get("late_stage") or {}), "Drifted concat must NOT be cached in late_stage"


@pytestmark_ffmpeg
def test_post_mux_truncation_gate_raises_sync_drift_error(tmp_path, monkeypatch):
    """If mux_audio produces a final MP4 truncated > 0.30s shorter than narration, raise SyncDriftError."""
    import app.services.curated_voices as cv

    job, src = _setup_mini_edge_job(tmp_path)
    monkeypatch.setattr(
        cv,
        "synthesize_curated_persona",
        lambda text, persona_id, output_wav, speed=1.1, log_fn=None: _write_tone_wav(Path(output_wav), seconds=1.2),
    )

    def _truncating_mux(ffmpeg, video, audio, output, *args, **kwargs):
        # Simulate silent -shortest clipping down to 0.8s (vs ~2.4s narration)
        _make_colored_source(Path(output), [("red", 0.8)])
        return False

    monkeypatch.setattr(core_mod, "mux_audio", _truncating_mux)

    sync = core_mod.EdgeSmartSync(
        job_dir=str(job),
        source_path=str(src),
        plan_path=str(job / "smart_sync_plan.json"),
        voice="voxcpm_komin",
        output_ratio="Original",
        mirror_video=False,
        zoom_factor=1.0,
        overlays={"tts_engine": "edge", "burn_subtitles": False},
        log=lambda _m: None,
    )
    with pytest.raises(core_mod.SyncDriftError, match="Post-mux duration mismatch"):
        sync.run()


@pytestmark_ffmpeg
def test_stale_corrupted_concat_cache_is_rejected_and_rebuilt(tmp_path, monkeypatch):
    """A cached synced_video_noaudio.mp4 whose duration disagrees with narration is never reused."""
    import app.services.curated_voices as cv

    job, src = _setup_mini_edge_job(tmp_path)
    monkeypatch.setattr(
        cv,
        "synthesize_curated_persona",
        lambda text, persona_id, output_wav, speed=1.1, log_fn=None: _write_tone_wav(Path(output_wav), seconds=1.0),
    )

    def _run_once(logs):
        sync = core_mod.EdgeSmartSync(
            job_dir=str(job),
            source_path=str(src),
            plan_path=str(job / "smart_sync_plan.json"),
            voice="voxcpm_komin",
            output_ratio="Original",
            mirror_video=False,
            zoom_factor=1.0,
            overlays={"tts_engine": "edge", "burn_subtitles": False},
            log=logs.append,
        )
        return sync, sync.run()

    logs1: list[str] = []
    sync1, _ = _run_once(logs1)
    # Tamper with cached synced_video_noaudio (5.0s noisy testsrc2 so size > 5000 bytes
    # and the reuse branch is actually entered, then rejected by our duration check!)
    paths = core_mod.make_paths(job)
    synced_vid = paths.synced_video_noaudio
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
            "-i", "testsrc2=duration=5:size=320x180:rate=30",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-an",
            str(synced_vid),
        ],
        check=True,
    )
    assert synced_vid.stat().st_size > 5000

    logs2: list[str] = []
    sync2, res2 = _run_once(logs2)
    assert any("Cached concat rejected" in line for line in logs2)
    assert abs(core_mod.probe_duration(sync2.ffprobe, synced_vid) - core_mod.probe_duration(sync2.ffprobe, paths.voice)) <= 0.10
    assert Path(res2["final"]).is_file()


def test_planner_breath_floor_and_pass1_max_spoken_chars_and_cps_untouched(monkeypatch):
    """Verify 1B-3: MEASURED_CHARS_PER_SECOND == 17.6, breath floor on short windows (safe non-negative),
    and Pass-1 translate_batch receives per-segment [MAX_SPOKEN_CHARS=N] hints."""
    assert planner_mod.MEASURED_CHARS_PER_SECOND == pytest.approx(17.6)
    assert planner_mod.RETAINED_BREATH_SECONDS == pytest.approx(0.30)
    assert planner_mod.HARD_MAX_VIDEO_SLOW_CEILING == pytest.approx(1.35)

    # Short 2.41s window (Chocolate Museum seg 5) @ +12% (1.12x), slow=1.20:
    # (2.41 * 1.20 - 0.30) * 1.12 = 2.90304s (< old 3.23904s)
    short_row = {"id": 5, "video_start": 0.0, "video_end": 2.41, "timestamp": "00:00:00,000 --> 00:00:02,410"}
    b_short = planner_mod.segment_raw_duration_budget(short_row, voice_speed="+12%", max_video_slow=1.20)
    assert b_short == pytest.approx((2.41 * 1.20 - 0.30) * 1.12)

    # Ultra-short windows (0.20s, 0.40s) must floor safely at 50% of delivered cap — never negative/zero
    for win in (0.20, 0.40):
        tiny = {"id": 1, "video_start": 0.0, "video_end": win}
        b_tiny = planner_mod.segment_raw_duration_budget(tiny, voice_speed="+12%", max_video_slow=1.20)
        assert b_tiny > 0.0
        assert b_tiny == pytest.approx((win * 1.20 * 0.5) * 1.12)

    # Pass-1 translate_batch must inject [MAX_SPOKEN_CHARS=N] into CURRENT ITEMS
    prompts: list[str] = []

    def fake_gemini_json(ai, prompt, retries=1, log=None, rotation_tier=None):
        prompts.append(prompt)
        return {"items": [{"id": 1, "translation": "ချောကလက် ပြတိုက်ထဲ ဝင်လာပါတယ်"}]}

    monkeypatch.setattr(planner_mod, "_gemini_json", fake_gemini_json)
    unit = {
        "id": 1,
        "video_start": 0.0,
        "video_end": 4.0,
        "timestamp": "00:00:00,000 --> 00:00:04,000",
        "source_text": "We walk inside the chocolate museum.",
    }
    planner_mod.translate_batch(
        ai=None,
        story_context={},
        previous_context=[],
        current=[unit],
        next_context=[],
        log=lambda _m: None,
        voice_speed="-6%",
        max_video_slow=1.20,
    )
    planner_mod.translate_batch(
        ai=None,
        story_context={},
        previous_context=[],
        current=[unit],
        next_context=[],
        log=lambda _m: None,
        voice_speed="+25%",
        max_video_slow=1.20,
    )
    expected_slow_chars = int((4.0 * 1.20 - 0.30) * 0.94 * 17.6)
    expected_fast_chars = int((4.0 * 1.20 - 0.30) * 1.25 * 17.6)
    assert f"[MAX_SPOKEN_CHARS={expected_slow_chars}]" in prompts[0]
    assert f"[MAX_SPOKEN_CHARS={expected_fast_chars}]" in prompts[1]
    assert expected_fast_chars > expected_slow_chars


def test_planner_compress_runs_3rd_round_and_fails_loudly_above_135_ceiling(monkeypatch):
    """Stubborn rows > window * 1.35 post-tempo after 3 rounds raise DurationBudgetExceededError;
    rows landing between 1.20x target and 1.35x hard ceiling still ship."""
    seed = "သူအိမ်ထဲဝင်လာပြီးပတ်ဝန်းကျင်ကိုလှည့်ကြည့်လိုက်တယ်"

    def _mm(n: int) -> str:
        return (seed * ((n // len(seed)) + 2))[:n]

    # 5.0s window @ +12% (tempo 1.12):
    #   1.20x budget (with 0.30s breath) -> (6.0 - 0.30) * 1.12 * 17.6 = 112 chars
    #   1.35x hard ceiling (post-tempo)  -> 5.0 * 1.35 * 1.12 * 17.6 = 133.05 chars
    # Start with 180 chars (1.82x > 1.35x); LLM refuses to shrink below 160 chars (1.62x > 1.35x)
    stubborn_row = [
        {
            "id": 1,
            "video_start": 0.0,
            "video_end": 5.0,
            "timestamp": "00:00:00,000 --> 00:00:05,000",
            "source_text": "Fast scene.",
            "translation": _mm(180),
        }
    ]
    seen_prompts: list[str] = []

    def _refusing_llm(ai, prompt, retries=1, log=None, rotation_tier=None):
        seen_prompts.append(prompt)
        return {"items": [{"id": 1, "translation": _mm(160)}]}

    monkeypatch.setattr(planner_mod, "_gemini_json", _refusing_llm)
    with pytest.raises(planner_mod.DurationBudgetExceededError, match="hard ceiling"):
        planner_mod._compress_overbudget_rows(
            None,
            stubborn_row,
            story_context={},
            previous_context=[],
            next_context=[],
            log=lambda _m: None,
            voice_speed="+12%",
            max_video_slow=1.20,
        )
    assert len(seen_prompts) == 3, "Must attempt all 3 compression rounds before failing"
    assert "FINAL STRICT COMPRESSION ROUND" in seen_prompts[2]

    # Absorption band check: 125 chars -> post-tempo = 125 / (17.6 * 1.12) = 6.34s = 1.268x
    # (between 1.20 target and 1.35 hard ceiling) -> MUST ship cleanly without raising!
    mid_prompts: list[str] = []

    def _band_llm(ai, prompt, retries=1, log=None, rotation_tier=None):
        mid_prompts.append(prompt)
        return {"items": [{"id": 1, "translation": _mm(125)}]}

    monkeypatch.setattr(planner_mod, "_gemini_json", _band_llm)
    out = planner_mod._compress_overbudget_rows(
        None,
        stubborn_row,
        story_context={},
        previous_context=[],
        next_context=[],
        log=lambda _m: None,
        voice_speed="+12%",
        max_video_slow=1.20,
    )
    assert len(out[0]["translation"]) == 125


# ===========================================================================
# 3. Canonical voice-presentation matrix: 5 note states x (my, en) x 3 surfaces
# ===========================================================================


def test_all_eight_round13_edge_voices_mapped_in_resolver():
    expected = {
        ("thiha", "my-MM-ThihaNeural"): "ကိုသီဟ",
        ("nilar", "my-MM-NilarNeural"): "မနီလာ",
        ("andrew", "en-US-AndrewMultilingualNeural"): "ကိုအန်ဒရူး",
        ("brian", "en-US-BrianMultilingualNeural"): "ကိုဘရိုင်ယန်",
        ("remy", "fr-FR-RemyMultilingualNeural"): "ကိုရီမီ",
        ("ava", "en-US-AvaMultilingualNeural"): "မအေဗာ",
        ("emma", "en-US-EmmaMultilingualNeural"): "မအမ်မာ",
        ("vivienne", "fr-FR-VivienneMultilingualNeural"): "မဗီဗီယန်",
    }
    for (short_key, neural), display in expected.items():
        assert edge_voice_display_name("komin", edge_voice=short_key) == display
        assert edge_voice_display_name("komin", edge_voice=neural) == display
        pres = resolve_recap_voice_presentation(
            requested={"tts_engine": "edge", "edge_voice": neural, "voice_slug": "komin"},
            actual={"actual_tts_provider": "edge_tts", "actual_voice": neural, "uniform_restart": False, "fallback_used": False, "total_segments": 10},
        )
        assert pres["header_voice"] == f"🎧 Edge TTS · {display}"
        assert pres["note_kind"] == "none"
        assert pres["note_params"] is None


@pytest.mark.parametrize("lang", ["my", "en"])
@pytest.mark.parametrize(
    "case_name,requested,actual,expected_header,expected_kind,expected_substrings,forbidden_substrings",
    [
        (
            "none_healthy_explicit_gemini",
            {"tts_engine": "gemini", "gemini_voice": "Charon", "voice_slug": "komin"},
            {"actual_tts_provider": "gemini", "actual_voice": "Charon", "uniform_restart": False, "fallback_used": False, "fallback_count": 0, "total_segments": 12},
            "ကိုသီဟ",
            "none",
            [],
            ["📝", "⚠️", "ကိုမင်း"],
        ),
        (
            "uniform_restart_reference_chocolate_museum",
            {"tts_engine": "gemini", "gemini_voice": None, "voice_slug": "komin"},
            {"actual_tts_provider": "edge_tts", "actual_voice": "my-MM-ThihaNeural", "uniform_restart": True, "fallback_used": False, "fallback_count": 0, "total_segments": 12},
            "🎧 Edge TTS · ကိုသီဟ",
            "uniform_restart",
            ["📝", "Edge TTS", "ကိုသီဟ"],
            ["Gemini ရဲ့ 'ကိုသီဟ'", "Gemini's 'ကိုသီဟ'"],
        ),
        (
            "all_fallback_gemini_100pct",
            {"tts_engine": "gemini", "gemini_voice": "Charon", "voice_slug": "komin"},
            {"actual_tts_provider": "gemini+persona_fallback", "actual_voice": "my-MM-ThihaNeural", "uniform_restart": False, "fallback_used": True, "fallback_count": 12, "total_segments": 12},
            "🎧 Edge TTS · ကိုသီဟ",
            "all_fallback",
            ["📝", "Edge TTS", "ကိုသီဟ"],
            ["Gemini ရဲ့ 'ကိုသီဟ'", "slightly different"],
        ),
        (
            "partial_fallback_voxcpm",
            {"tts_engine": "voxcpm", "gemini_voice": None, "voice_slug": "komin"},
            {"actual_tts_provider": "voxcpm+edge_fallback", "actual_voice": "voxcpm_komin", "uniform_restart": False, "fallback_used": True, "fallback_count": 3, "total_segments": 12},
            "ကိုမင်း",
            "partial_fallback",
            ["⚠️", "3"],
            ["Gemini"],
        ),
        (
            "gemini_voice_mapped_auto_from_persona",
            {"tts_engine": "gemini", "gemini_voice": None, "voice_slug": "komin"},
            {"actual_tts_provider": "gemini", "actual_voice": "Charon", "uniform_restart": False, "fallback_used": False, "fallback_count": 0, "total_segments": 12},
            "ကိုသီဟ",
            "gemini_voice_mapped",
            ["📝", "ကိုမင်း", "Gemini", "ကိုသီဟ"],
            ["Edge TTS"],
        ),
    ],
)
def test_voice_presentation_matrix_across_all_delivery_surfaces(
    tmp_path,
    monkeypatch,
    lang,
    case_name,
    requested,
    actual,
    expected_header,
    expected_kind,
    expected_substrings,
    forbidden_substrings,
):
    """5 note states x (my, en) x (completion DM, _send_recap_telegram_delivery, /history display helper)."""
    pres = resolve_recap_voice_presentation(requested=requested, actual=actual)
    assert pres["header_voice"] == expected_header, case_name
    assert pres["note_kind"] == expected_kind, case_name

    # Surface 1: Bot completion DM (build_recap_completion_message)
    dm = api_mod.build_recap_completion_message(
        lang,
        clean_title="This Chocolate Museum Has Unlimited Chocolate",
        voice_name=pres["header_voice"],
        duration_seconds=64,
        job_id="rc_1795463830_311bc5",
        retention_hours=24,
        voice_note_kind=pres["note_kind"],
        voice_note_params=pres["note_params"],
    )
    assert expected_header in dm
    for token in expected_substrings:
        assert token in dm, f"[{case_name}/{lang}] missing {token!r} in DM: {dm}"
    for bad in forbidden_substrings:
        assert bad not in dm, f"[{case_name}/{lang}] unexpected {bad!r} in DM: {dm}"

    # Surface 2: On-demand Telegram delivery (_send_recap_telegram_delivery)
    sent_captions: list[str] = []
    vid = tmp_path / f"{case_name}_{lang}.mp4"
    vid.write_bytes(b"0" * 1024)

    class _FakeBot:
        class session:
            @staticmethod
            async def close():
                pass

    async def _fake_send_cached(bot, *, media_kind, chat_id, file_path, filename, caption, parse_mode):
        sent_captions.append(caption)

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, cls, uid):
            return SimpleNamespace(language_code=lang)

    monkeypatch.setattr(api_mod.settings, "bot_token", "dummy:token")
    monkeypatch.setattr(api_mod, "create_telegram_bot", lambda **kw: _FakeBot())
    monkeypatch.setattr(api_mod, "send_cached_telegram_media", _fake_send_cached)
    import app.db.session as db_sess
    monkeypatch.setattr(db_sess, "AsyncSessionLocal", lambda: _FakeSession())

    delivered, reason = asyncio.run(
        api_mod._send_recap_telegram_delivery(
            chat_id=8582483248,
            video_path=str(vid),
            title="Chocolate Museum",
            style="Pro Dubbed",
            voice_name=pres["header_voice"],
            duration_sec=64,
            job_id="rc_1795463830_311bc5",
            voice_note_kind=pres["note_kind"],
            voice_note_params=pres["note_params"],
        )
    )
    assert delivered is True and reason is None
    assert len(sent_captions) == 1
    cap = sent_captions[0]
    assert expected_header in cap
    for token in expected_substrings:
        assert token in cap, f"[{case_name}/{lang}] missing {token!r} in delivery caption: {cap}"
    for bad in forbidden_substrings:
        assert bad not in cap, f"[{case_name}/{lang}] unexpected {bad!r} in delivery caption: {cap}"

    # Surface 3: /history serializer (_job_display_names reading persisted Job.recap_config)
    persisted_cfg = {
        "config": dict(requested),
        "actual_tts_provider": actual["actual_tts_provider"],
        "actual_voice": actual["actual_voice"],
        "uniform_restart": actual["uniform_restart"],
        "fallback_used": actual["fallback_used"],
        "fallback_count": actual["fallback_count"],
        "total_segments": actual["total_segments"],
    }
    fake_job = SimpleNamespace(
        job_type="RECAP",
        engine="recap",
        preset_voice=requested.get("voice_slug") or "komin",
        edge_voice=requested.get("edge_voice"),
        recap_ref_audio_path=None,
        recap_config=json.dumps(persisted_cfg, ensure_ascii=False),
    )
    v_disp, _ = api_mod._job_display_names(fake_job, is_recap=True, is_tts=False)
    assert v_disp == expected_header


def test_read_recap_voice_outcome_handles_single_and_segmented_jobs(tmp_path):
    # Single-video job
    single = tmp_path / "single"
    p = single / "workspace" / "edge_tts_smart_sync" / "smart_sync_plan_edge_tts.json"
    p.parent.mkdir(parents=True)
    p.write_text(
        json.dumps({
            "tts_provider": "edge_tts",
            "actual_voice": "my-MM-ThihaNeural",
            "uniform_restart": True,
            "fallback_used": False,
            "fallback_segments": [],
            "total_segments": 12,
        }),
        encoding="utf-8",
    )
    out = api_mod._read_recap_voice_outcome(single)
    assert out["actual_tts_provider"] == "edge_tts"
    assert out["actual_voice"] == "my-MM-ThihaNeural"
    assert out["uniform_restart"] is True
    assert out["total_segments"] == 12

    # Segmented multi-part job (part 1 clean Gemini, part 2 uniform_restart to Edge)
    seg_job = tmp_path / "segmented"
    for idx, payload in (
        (1, {"tts_provider": "gemini", "actual_voice": "Charon", "uniform_restart": False, "fallback_used": False, "fallback_segments": [], "total_segments": 6}),
        (2, {"tts_provider": "edge_tts", "actual_voice": "my-MM-ThihaNeural", "uniform_restart": True, "fallback_used": False, "fallback_segments": [], "total_segments": 6}),
    ):
        pf = seg_job / f"seg{idx:02d}" / "edge_tts_smart_sync" / "smart_sync_plan_edge_tts.json"
        pf.parent.mkdir(parents=True)
        pf.write_text(json.dumps(payload), encoding="utf-8")
    merged = api_mod._read_recap_voice_outcome(seg_job)
    assert merged["uniform_restart"] is True
    assert merged["actual_tts_provider"] == "edge_tts"
    assert merged["actual_voice"] == "my-MM-ThihaNeural"
    assert merged["total_segments"] == 12


def test_2b3_cost_telemetry_uses_actual_provider_and_drops_retired_model():
    api_src = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")
    assert "gemini-2.5-flash-preview-tts" not in api_src
    assert '_tel_provider = str(' in api_src
    assert '_voice_outcome.get("actual_tts_provider")' in api_src


# ===========================================================================
# 4. Reference Chocolate Museum 12-segment plan replay (no live keys)
# ===========================================================================


@pytestmark_ffmpeg
def test_chocolate_museum_12_segment_replay_sync_ok_and_honest_caption(tmp_path, monkeypatch):
    """Replay the exact 12-segment structure of rc_1795463830_311bc5 (windows + raw durations,
    429 on lines 6, 8, 12 after Pass 0 renders segs 1..4) without live keys.

    Asserts:
    - video_parts.txt contains the 12 segment files once each (never 16).
    - Sync gate logs ✅ SYNC OK with abs(picture - narration) <= 0.30s.
    - Final video duration matches narration within <= 0.30s.
    - Delivery caption renders header '🎧 Edge TTS · ကိုသီဟ' + uniform-restart note.
    """
    import app.services.curated_voices as cv
    import app.services.recap_engine.gemini_tts_engine as gem_mod

    # Exact 12 (window_s, raw_audio_s) from rc_1795463830_311bc5 scaled 0.25x so real FFmpeg
    # runs in a few seconds while preserving all 12 segments and exact ratios/topology.
    scale = 0.25
    real_pairs = [
        (4.83, 4.27), (2.93, 3.82), (5.04, 6.60), (7.26, 9.46),
        (2.41, 2.95), (4.79, 5.31), (4.29, 5.26), (7.38, 7.73),
        (5.27, 6.15), (5.30, 6.87), (3.31, 4.13), (7.12, 7.97),
    ]
    colors = [
        "red", "green", "blue", "yellow", "cyan", "magenta",
        "orange", "purple", "pink", "lime", "teal", "white",
    ]
    scaled_specs = [(colors[i], w * scale) for i, (w, _) in enumerate(real_pairs)]
    job = tmp_path / "chocolate_replay"
    job.mkdir()
    src = _make_colored_source(job / "source.mp4", scaled_specs, size="160x90")

    segments = []
    t_cur = 0.0
    for i, (w, _) in enumerate(real_pairs, 1):
        w_s = round(w * scale, 3)
        segments.append({
            "video_start": round(t_cur, 3),
            "video_end": round(t_cur + w_s, 3),
            "narration": f"ချောကလက် ပြတိုက် အပိုင်း {i} ဇာတ်လမ်း ဖြစ်ပါတယ်",
        })
        t_cur += w_s
    (job / "smart_sync_plan.json").write_text(json.dumps({"segments": segments}, ensure_ascii=False), encoding="utf-8")

    # Wait until Pass 0 has finished rendering >=4 parts (reproducing segs 1..4 on the real job!)
    four_rendered = threading.Event()
    p0_done = {"n": 0}
    real_render = core_mod.render_video_part

    def _track_render(*args, **kwargs):
        res = real_render(*args, **kwargs)
        p0_done["n"] += 1
        if p0_done["n"] >= 4:
            four_rendered.set()
        return res

    monkeypatch.setattr(core_mod, "render_video_part", _track_render)

    dur_by_seg = {f"အပိုင်း {i} ": round(a * scale, 3) for i, (_, a) in enumerate(real_pairs, 1)}

    def _seg_dur(text: str) -> float:
        for k, v in dur_by_seg.items():
            if k in text:
                return v
        return 1.0

    class _FakeEdge:
        def __init__(self, **kw):
            self.work_dir = Path(kw.get("work_dir") or ".")

        def generate_channel99_voice(self, text, voice):
            return _write_tone_wav(self.work_dir / f"edge_{abs(hash(text)) % 99999}.wav", seconds=_seg_dur(text), pad_silence=0.12)

    class _FakeGemini:
        def __init__(self, **kw):
            self.work_dir = Path(kw.get("work_dir") or ".")

        def generate_channel99_voice(self, text, voice):
            if any(tok in text for tok in ("အပိုင်း 6 ", "အပိုင်း 8 ", "အပိုင်း 12 ")):
                if "အပိုင်း 6 " in text:
                    assert four_rendered.wait(timeout=30), "Pass 0 did not finish 4 parts before line 6 failed"
                raise RuntimeError("429 RESOURCE_EXHAUSTED (gemini-3.8-flash-tts)")
            return _write_tone_wav(self.work_dir / f"gem_{abs(hash(text)) % 99999}.wav", seconds=_seg_dur(text), pad_silence=0.10)

    def _fake_persona(text, persona_id, output_wav, speed=1.12, log_fn=None):
        return _write_tone_wav(Path(output_wav), seconds=_seg_dur(text), pad_silence=0.14)

    monkeypatch.setattr(core_mod, "EdgeTTSEngine", _FakeEdge)
    monkeypatch.setattr(gem_mod, "GeminiTTSEngine", _FakeGemini)
    monkeypatch.setattr(gem_mod, "collect_gemini_keys", lambda: ["dummy-key"])
    monkeypatch.setattr(cv, "synthesize_curated_persona", _fake_persona)

    logs: list[str] = []
    sync = core_mod.EdgeSmartSync(
        job_dir=str(job),
        source_path=str(src),
        plan_path=str(job / "smart_sync_plan.json"),
        voice="voxcpm_komin",
        edge_tts_rate="+12%",
        output_ratio="Original",
        mirror_video=False,
        zoom_factor=1.0,
        tts_workers=1,
        overlays={"tts_engine": "gemini", "gemini_voice": "Charon", "is_female": False, "burn_subtitles": False},
        log=logs.append,
    )
    res = sync.run()

    assert four_rendered.is_set()
    parts_txt = (job / "edge_tts_smart_sync" / "work" / "video_parts.txt").read_text(encoding="utf-8")
    part_lines = [ln.strip() for ln in parts_txt.splitlines() if ln.strip()]
    assert len(part_lines) == 12
    assert len(set(part_lines)) == 12
    assert any("✅ SYNC OK" in ln for ln in logs)

    paths = core_mod.make_paths(job)
    pic_dur = core_mod.probe_duration(sync.ffprobe, paths.synced_video_noaudio)
    aud_dur = core_mod.probe_duration(sync.ffprobe, paths.voice)
    fin_dur = core_mod.probe_duration(sync.ffprobe, Path(res["final"]))
    assert abs(pic_dur - aud_dur) <= 0.30
    assert abs(fin_dur - aud_dur) <= 0.30

    # End-to-end delivery caption verification from disk plan JSON
    outcome = api_mod._read_recap_voice_outcome(job)
    pres = resolve_recap_voice_presentation(
        requested={"tts_engine": "gemini", "gemini_voice": None, "voice_slug": "komin"},
        actual=outcome,
    )
    assert pres["header_voice"] == "🎧 Edge TTS · ကိုသီဟ"
    assert pres["note_kind"] == "uniform_restart"
    msg_my = api_mod.build_recap_completion_message(
        "my",
        clean_title="This Chocolate Museum Has Unlimited Chocolate",
        voice_name=pres["header_voice"],
        duration_seconds=fin_dur,
        job_id="rc_1795463830_311bc5",
        voice_note_kind=pres["note_kind"],
        voice_note_params=pres["note_params"],
    )
    assert "🎙️ အသံ: 🎧 Edge TTS · ကိုသီဟ" in msg_my
    assert "Edge TTS ရဲ့ 'ကိုသီဟ'" in msg_my
    assert "Gemini ရဲ့ 'ကိုသီဟ'" not in msg_my

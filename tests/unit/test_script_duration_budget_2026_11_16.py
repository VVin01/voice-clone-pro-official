"""Tests for Item 5 Option 1: planner hard spoken-duration budget (2026-11-16)."""
from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app.services import speed as speed_svc
from app.services.queue import MEASURED_CHARS_PER_SECOND
from app.services.recap_engine import recap_pipeline as pipeline_mod
from app.services.recap_engine import recap_planner as planner

_SEED_PHRASES = [
    "သူအိမ်ထဲဝင်လာပြီးပတ်ဝန်းကျင်ကိုလှည့်ကြည့်လိုက်တယ်",
    "မီးဖိုချောင်ထဲမှာထူးဆန်းတဲ့အနံ့တစ်ခုရနေတာသိလိုက်တယ်",
    "တံခါးပေါက်ကနေအပြင်ဘက်ထွက်ပြီးလမ်းမပေါ်လျှောက်သွားတယ်",
    "ရဲတပ်ဖွဲ့ကားတွေလမ်းဆုံနားမှာအမြန်ပိတ်ဆို့နေကြပြီ",
    "ရထားပေါ်ကိုအချိန်မီခုန်တက်ပြီးမြို့ထဲကလွတ်မြောက်သွားတယ်",
]


def _burmese_text(target_chars: int, *, seed: int = 0) -> str:
    """Build valid, distinct, speakable Burmese text of exact character count."""
    rotated = _SEED_PHRASES[seed % len(_SEED_PHRASES):] + _SEED_PHRASES[:seed % len(_SEED_PHRASES)]
    buf = ""
    i = 0
    while len(buf) < target_chars:
        buf += rotated[i % len(rotated)]
        i += 1
    return buf[:target_chars]


def test_constants_derived_from_existing_speed_and_sync_system():
    assert planner.MEASURED_CHARS_PER_SECOND == pytest.approx(MEASURED_CHARS_PER_SECOND)
    assert planner.MEASURED_CHARS_PER_SECOND == pytest.approx(17.6)
    assert planner.DEFAULT_MAX_VIDEO_SLOW == pytest.approx(1.20)
    assert planner.DEFAULT_SPEED == speed_svc.DEFAULT_SPEED


def test_segment_window_and_raw_budget_math():
    row_video = {"id": 1, "video_start": 10.0, "video_end": 41.0, "speech_start": 12.0, "speech_end": 39.0}
    row_speech = {"id": 2, "speech_start": 5.0, "speech_end": 10.0}
    row_ts = {"id": 3, "timestamp": "00:00:01,000 --> 00:00:06,500"}

    assert planner.segment_window_seconds(row_video) == pytest.approx(31.0)
    assert planner.segment_window_seconds(row_speech) == pytest.approx(5.0)
    assert planner.segment_window_seconds(row_ts) == pytest.approx(5.5)

    # At +20% (tempo 1.20) with max_video_slow 1.20 and 0.30s retained breath (1B-3):
    # raw budget = (31 * 1.20 - 0.30) * 1.20 = 44.28 s
    raw_budget = planner.segment_raw_duration_budget(row_video, voice_speed="+20%", max_video_slow=1.20)
    assert raw_budget == pytest.approx(44.28)


def test_real_incident_31s_overbudget_is_compressed_and_rechecked(monkeypatch):
    """Incident shape: 31 s source, ~63.6 s raw narration at +20% (~1119 chars) must compress <= 44.64 s raw (<= 37.2 s post-tempo)."""
    segments = [
        {"id": 1, "video_start": 0.0, "video_end": 10.0, "speech_start": 0.5, "speech_end": 9.5,
         "timestamp": "00:00:00,500 --> 00:00:09,500", "source_text": "He runs across the rooftop.",
         "translation": _burmese_text(360, seed=0)},   # ~20.45 s raw vs 14.40 s budget
        {"id": 2, "video_start": 10.0, "video_end": 21.0, "speech_start": 10.5, "speech_end": 20.5,
         "timestamp": "00:00:10,500 --> 00:00:20,500", "source_text": "Police block the narrow alley.",
         "translation": _burmese_text(399, seed=1)},   # ~22.67 s raw vs 15.84 s budget
        {"id": 3, "video_start": 21.0, "video_end": 31.0, "speech_start": 21.5, "speech_end": 30.5,
         "timestamp": "00:00:21,500 --> 00:00:30,500", "source_text": "He jumps onto the passing train.",
         "translation": _burmese_text(360, seed=2)},   # ~20.45 s raw vs 14.40 s budget
    ]
    total_raw_before = sum(planner.estimate_raw_speech_seconds(r["translation"]) for r in segments)
    assert total_raw_before == pytest.approx(1119 / 17.6)  # ~63.58 s raw (> 2x source window)

    calls: list[str] = []

    def fake_gemini_json(ai, prompt, retries=1, log=None, rotation_tier=None):
        calls.append(prompt)
        if "Compression Round 1" in prompt:
            # Round 1: ID 2 still slightly over budget (290 chars > 278), forcing post-check Round 2
            return {
                "items": [
                    {"id": 1, "translation": _burmese_text(220, seed=0)},
                    {"id": 2, "translation": _burmese_text(290, seed=1)},
                    {"id": 3, "translation": _burmese_text(220, seed=2)},
                ]
            }
        if "Compression Round 2" in prompt:
            # Round 2 receives ONLY still-overbudget ID 2 and compresses within budget
            return {"items": [{"id": 2, "translation": _burmese_text(250, seed=1)}]}
        # Initial audit returns pass without shortening -> _compress_overbudget_rows must handle it
        return {"checks": [{"id": r["id"], "status": "pass"} for r in segments]}

    monkeypatch.setattr(planner, "_gemini_json", fake_gemini_json)
    logs: list[str] = []
    out = planner.audit_and_repair_batch(
        None,
        segments,
        story_context={"title": "Rooftop Escape"},
        previous_context=[],
        next_context=[],
        log=logs.append,
        narrative_style="narrator",
        is_female=False,
        voice_speed="+20%",
        max_video_slow=1.20,
    )

    assert len(calls) == 3  # 1 audit + Compression Round 1 + re-check Compression Round 2
    assert "SPOKEN-DURATION BUDGET" in calls[0]
    assert "ID 00002" in calls[2] and "ID 00001" not in calls[2]
    assert [r["id"] for r in out] == [1, 2, 3]
    for row in out:
        win = planner.segment_window_seconds(row)
        raw_est = planner.estimate_raw_speech_seconds(row["translation"])
        raw_budget = planner.segment_raw_duration_budget(row, voice_speed="+20%", max_video_slow=1.20)
        assert raw_est <= raw_budget
        assert (raw_est / 1.20) <= win * 1.20

    total_post_tempo = sum(
        planner.estimate_raw_speech_seconds(r["translation"]) / speed_svc.resolve_speed_tempo("+20%")
        for r in out
    )
    assert total_post_tempo <= 31.0 * 1.20


def test_within_budget_29s_delivered_passes_byte_identical(monkeypatch):
    """Normal ~29 s delivered / 34.8 s raw on 31 s at +20% passes byte-identical with zero compression rewrites."""
    rows = [
        {"id": 1, "video_start": 0.0, "video_end": 10.0, "timestamp": "00:00:00,000 --> 00:00:10,000",
         "source_text": "Scene one.", "translation": _burmese_text(200, seed=0)},  # 11.36 s <= 14.40 s
        {"id": 2, "video_start": 10.0, "video_end": 21.0, "timestamp": "00:00:10,000 --> 00:00:21,000",
         "source_text": "Scene two.", "translation": _burmese_text(212, seed=1)},  # 12.05 s <= 15.84 s
        {"id": 3, "video_start": 21.0, "video_end": 31.0, "timestamp": "00:00:21,000 --> 00:00:31,000",
         "source_text": "Scene three.", "translation": _burmese_text(200, seed=2)},  # 11.36 s <= 14.40 s
    ]
    originals = [r["translation"] for r in rows]
    calls: list[str] = []

    def fake_gemini_json(ai, prompt, retries=1, log=None, rotation_tier=None):
        calls.append(prompt)
        return {"checks": [{"id": r["id"], "status": "pass"} for r in rows]}

    monkeypatch.setattr(planner, "_gemini_json", fake_gemini_json)
    out = planner.audit_and_repair_batch(
        None,
        rows,
        story_context={},
        previous_context=[],
        next_context=[],
        log=lambda _m: None,
        narrative_style="narrator",
        is_female=False,
        voice_speed="+20%",
        max_video_slow=1.20,
    )

    assert len(calls) == 1
    assert "SPOKEN-DURATION BUDGET" not in calls[0]
    assert [r["translation"] for r in out] == originals


def test_mixed_batch_keeps_within_budget_row_byte_identical(monkeypatch):
    """Within a mixed batch, within-budget IDs stay byte-identical while only over-budget IDs are compressed."""
    rows = [
        {"id": 1, "video_start": 0.0, "video_end": 10.0, "timestamp": "00:00:00,000 --> 00:00:10,000",
         "source_text": "Quiet lobby.", "translation": _burmese_text(120, seed=0)},   # 6.82 s <= 14.40 s
        {"id": 2, "video_start": 10.0, "video_end": 15.0, "timestamp": "00:00:10,000 --> 00:00:15,000",
         "source_text": "Fast chase.", "translation": _burmese_text(240, seed=1)},    # 13.64 s > 7.20 s (126 chars)
    ]
    calls: list[str] = []

    def fake_gemini_json(ai, prompt, retries=1, log=None, rotation_tier=None):
        calls.append(prompt)
        if "Compression Round" in prompt:
            assert "ID 00001" not in prompt and "ID 00002" in prompt
            return {"items": [{"id": 2, "translation": _burmese_text(100, seed=1)}]}
        return {"checks": [{"id": 1, "status": "pass"}, {"id": 2, "status": "pass"}]}

    monkeypatch.setattr(planner, "_gemini_json", fake_gemini_json)
    out = planner.audit_and_repair_batch(
        None, rows, story_context={}, log=lambda _m: None, voice_speed="+20%", max_video_slow=1.20
    )

    assert out[0]["translation"] == rows[0]["translation"]
    assert len(out[1]["translation"]) == 100


def test_tempo_boundary_minus_6_vs_plus_25(monkeypatch):
    """5.0 s window, 115 chars (~6.53 s raw): over-budget at -6% (5.64 s budget) vs within-budget at +25% (7.50 s budget)."""
    text_115 = _burmese_text(115, seed=0)
    base_row = [
        {"id": 1, "video_start": 0.0, "video_end": 5.0, "timestamp": "00:00:00,000 --> 00:00:05,000",
         "source_text": "He opens the heavy iron gate carefully.", "translation": text_115},
    ]

    assert planner._overbudget_rows(base_row, voice_speed="-6%", max_video_slow=1.20)
    assert planner._overbudget_rows(base_row, voice_speed="+25%", max_video_slow=1.20) == []

    # At +25%: passes byte-identical in 1 call
    plus25_calls: list[str] = []
    monkeypatch.setattr(
        planner,
        "_gemini_json",
        lambda ai, prompt, retries=1, log=None, rotation_tier=None: (
            plus25_calls.append(prompt) or {"checks": [{"id": 1, "status": "pass"}]}
        ),
    )
    out_fast = planner.audit_and_repair_batch(
        None, base_row, story_context={}, log=lambda _m: None, voice_speed="+25%", max_video_slow=1.20
    )
    assert len(plus25_calls) == 1
    assert out_fast[0]["translation"] == text_115

    # At -6%: compressed to <= floor(5.0 * 1.20 * 0.94 * 17.6) = 99 chars
    minus6_calls: list[str] = []

    def fake_slow_compress(ai, prompt, retries=1, log=None, rotation_tier=None):
        minus6_calls.append(prompt)
        if "Compression Round" in prompt:
            return {"items": [{"id": 1, "translation": _burmese_text(90, seed=0)}]}
        return {"checks": [{"id": 1, "status": "pass"}]}

    monkeypatch.setattr(planner, "_gemini_json", fake_slow_compress)
    out_slow = planner.audit_and_repair_batch(
        None, base_row, story_context={}, log=lambda _m: None, voice_speed="-6%", max_video_slow=1.20
    )
    assert len(minus6_calls) == 2
    assert len(out_slow[0]["translation"]) <= 99
    post_sec = planner.estimate_raw_speech_seconds(out_slow[0]["translation"]) / speed_svc.resolve_speed_tempo("-6%")
    assert post_sec <= 5.0 * 1.20


def test_parallel_translator_triggers_audit_on_overbudget_later_batch(monkeypatch):
    """Even on batch 2 (bi > 0) with clean Burmese text, duration overflow still triggers audit + compression."""
    units = [
        {"id": 1, "video_start": 0.0, "video_end": 5.0, "timestamp": "00:00:00,000 --> 00:00:05,000", "source_text": "One."},
        {"id": 2, "video_start": 5.0, "video_end": 10.0, "timestamp": "00:00:05,000 --> 00:00:10,000", "source_text": "Two."},
    ]
    batches = [[units[0]], [units[1]]]

    def fake_translate_batch(ai, **kw):
        row = kw["current"][0]
        uid = row["id"]
        # Batch 1 fits nicely; Batch 2 overflows 5s window at +20% (budget 7.20 s -> 126 chars; 200 chars = 11.36 s)
        txt = _burmese_text(70, seed=0) if uid == 1 else _burmese_text(200, seed=1)
        return [{**row, "translation": txt}]

    audit_ids: list[int] = []

    def fake_audit(ai, translated, **kw):
        audit_ids.extend(r["id"] for r in translated)
        assert kw["voice_speed"] == "+20%"
        assert kw["max_video_slow"] == pytest.approx(1.20)
        return [{**r, "translation": _burmese_text(90, seed=1) if r["id"] == 2 else r["translation"]} for r in translated]

    monkeypatch.setattr(planner, "translate_batch", fake_translate_batch)
    monkeypatch.setattr(planner, "audit_and_repair_batch", fake_audit)

    out = planner.translate_batches_in_parallel(
        ai=object(),
        worker_ai_factory=lambda: object(),
        batches=batches,
        pending=[0, 1],
        units=units,
        unit_index={1: 0, 2: 1},
        initial_translated=[],
        story_context={},
        log=lambda _m: None,
        should_stop=lambda: False,
        translation_audit=True,
        voice_speed="+20%",
        max_video_slow=1.20,
    )

    assert audit_ids == [1, 2]
    assert planner._overbudget_rows(out, voice_speed="+20%", max_video_slow=1.20) == []


def test_parenthetical_glosses_excluded_like_tts_engine():
    glossy = _burmese_text(70, seed=0) + " (John Alexander Smith)"
    bare = _burmese_text(70, seed=0)
    assert planner.estimate_raw_speech_seconds(glossy) == pytest.approx(planner.estimate_raw_speech_seconds(bare))


def test_pipeline_and_api_wire_voice_speed_and_slow_cap_into_planner(tmp_path, monkeypatch):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"")
    seen_inits: list[dict] = []

    class RecordingBuilder:
        def __init__(self, **kwargs):
            seen_inits.append(kwargs)

        def build(self, *, source, job_dir, source_title="", reuse=True):
            plan_path = Path(job_dir) / "smart_sync_plan.json"
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text("{}", encoding="utf-8")
            return plan_path, {"segments": []}

    class DummyRunner:
        def __init__(self, **kwargs):
            pass

        def run(self):
            return {"final": "x.mp4", "audio": None, "subtitles_srt": None, "segments": 0, "duration": 0.0}

    monkeypatch.setattr(pipeline_mod, "RecapPlanBuilder", RecordingBuilder)
    monkeypatch.setattr(pipeline_mod, "EdgeSmartSync", DummyRunner)
    monkeypatch.setattr(pipeline_mod, "detect_source_subtitle_bands", lambda *a, **k: [])
    monkeypatch.setattr(pipeline_mod, "find_binary", lambda name: name)
    monkeypatch.setattr(pipeline_mod, "probe_duration", lambda *a: 10.0)

    pipeline_mod.build_recap_script_plan(src, job_dir=tmp_path / "phase1", voice_speed="-6%")
    assert seen_inits[-1]["voice_speed"] == "-6%"
    assert seen_inits[-1]["max_video_slow"] == pytest.approx(1.20)

    pipeline_mod.run_recap_pipeline(src, job_dir=tmp_path / "phase2", voice_speed="+25%")
    assert seen_inits[-1]["voice_speed"] == "+25%"
    assert seen_inits[-1]["max_video_slow"] == pytest.approx(1.20)

    assert "voice_speed" in inspect.signature(pipeline_mod.build_recap_script_plan).parameters
    api_src = Path("app/webapp/api.py").read_text(encoding="utf-8")
    assert "voice_speed=req.voice_speed" in api_src

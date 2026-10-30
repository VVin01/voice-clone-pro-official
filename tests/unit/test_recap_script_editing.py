"""Tests for the recap narration script-editing feature (2026-10-29).

Ported from the desktop Recapter 'Edit Text' tool: users review/edit the
Burmese narration after transcription + translation, before TTS/render.
Pure-function tests over app.services.recap_engine.script_editing.
"""
import copy
import json

import pytest

from app.services.recap_engine.script_editing import (
    MAX_NARRATION_CHARS,
    apply_script_edits,
    atomic_write_json,
    clean_edited_narration,
    list_script_segments,
    narration_problems,
)


def _plan():
    return {
        "planner_mode": "test",
        "segments": [
            {
                "source_segment_ids": [1],
                "atomic_id": 1,
                "timestamp": "00:00:00,000 --> 00:00:04,000",
                "video_start": 0.0,
                "video_end": 4.0,
                "source_text": "He runs into the room.",
                "narration": "သူ အခန်းထဲ ပြေးဝင်လာတယ်။",
                "translation": "သူ အခန်းထဲ ပြေးဝင်လာတယ်။",
            },
            {
                "source_segment_ids": [2],
                "atomic_id": 2,
                "timestamp": "00:00:04,000 --> 00:00:08,500",
                "video_start": 4.0,
                "video_end": 8.5,
                "source_text": "She opens the old letter.",
                "narration": "သူမ စာဟောင်းကို ဖွင့်လိုက်တယ်။",
                "translation": "သူမ စာဟောင်းကို ဖွင့်လိုက်တယ်။",
            },
        ],
    }


def _translations():
    return {
        "items": [
            {"id": 1, "translation": "သူ အခန်းထဲ ပြေးဝင်လာတယ်။"},
            {"id": 2, "translation": "သူမ စာဟောင်းကို ဖွင့်လိုက်တယ်။"},
        ]
    }


def test_list_script_segments_shape_and_order():
    segs = list_script_segments(_plan())
    assert [s["index"] for s in segs] == [1, 2]
    assert segs[0]["source_text"] == "He runs into the room."
    assert segs[0]["narration"].startswith("သူ အခန်း")
    assert segs[1]["start"] == 4.0 and segs[1]["end"] == 8.5


def test_apply_edit_updates_plan_and_translation_checkpoint():
    plan, tr = _plan(), _translations()
    result = apply_script_edits(plan, tr, [{"index": 2, "narration": "သူမ စာဟောင်းကို ဖြည်းဖြည်း ဖွင့်လိုက်တယ်။"}])
    assert result["updated"] == 1
    assert plan["segments"][1]["narration"] == "သူမ စာဟောင်းကို ဖြည်းဖြည်း ဖွင့်လိုက်တယ်။"
    assert plan["segments"][1]["translation"] == "သူမ စာဟောင်းကို ဖြည်းဖြည်း ဖွင့်လိုက်တယ်။"
    # checkpoint mirrored by atomic_id; the untouched row stays as-is
    assert tr["items"][1]["translation"] == "သူမ စာဟောင်းကို ဖြည်းဖြည်း ဖွင့်လိုက်တယ်။"
    assert tr["items"][0]["translation"] == "သူ အခန်းထဲ ပြေးဝင်လာတယ်။"
    # segment 1 in the plan is untouched
    assert plan["segments"][0]["narration"] == "သူ အခန်းထဲ ပြေးဝင်လာတယ်။"


def test_apply_edit_noop_when_text_unchanged():
    plan = _plan()
    result = apply_script_edits(plan, None, [{"index": 1, "narration": plan["segments"][0]["narration"]}])
    assert result["updated"] == 0


def test_apply_edit_rejects_empty_and_unknown_and_too_long():
    plan = _plan()
    with pytest.raises(ValueError):
        apply_script_edits(plan, None, [{"index": 1, "narration": "   "}])
    with pytest.raises(ValueError):
        apply_script_edits(plan, None, [{"index": 99, "narration": "တစ်ခုခု"}])
    with pytest.raises(ValueError):
        apply_script_edits(plan, None, [{"index": 1, "narration": "က" * (MAX_NARRATION_CHARS + 1)}])


def test_apply_edit_validates_whole_batch_before_mutating():
    plan = _plan()
    before = copy.deepcopy(plan)
    with pytest.raises(ValueError):
        apply_script_edits(plan, None, [
            {"index": 1, "narration": "ပြင်ထားသော စာကြောင်း"},
            {"index": 42, "narration": "မရှိသော အပိုင်း"},
        ])
    assert plan == before


def test_narration_problems_flags_latin_words_and_mixed_digits():
    problems = narration_problems("သူက computer ကို သုံးတယ် ၂၀0၁ ခုနှစ်")
    joined = " | ".join(problems)
    assert "English words" in joined
    assert "mixed Myanmar/Latin digits" in joined


def test_narration_problems_clean_burmese_line():
    assert narration_problems("သူ အခန်းထဲ ပြေးဝင်လာတယ်။") == []


def test_clean_edited_narration_collapses_whitespace():
    assert clean_edited_narration("  ပထမ   စာကြောင်း\nဒုတိယ  ") == "ပထမ စာကြောင်း ဒုတိယ"
    assert clean_edited_narration("") == ""


def test_atomic_write_json_roundtrip(tmp_path):
    target = tmp_path / "smart_sync_plan.json"
    payload = {"segments": [{"narration": "မြန်မာ စာသား"}]}
    atomic_write_json(target, payload)
    assert json.loads(target.read_text(encoding="utf-8")) == payload
    assert not target.with_suffix(".json.tmp").exists()

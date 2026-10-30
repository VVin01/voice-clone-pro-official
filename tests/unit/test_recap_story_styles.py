"""Tests for Recap Story Style prompts (recap-quality pass, 2026-10-30).

Each style's prompt is captured with a fake Gemini transport and checked
for its key directives, so a future prompt edit cannot silently drop the
rules that define the style. Shared pipeline rules (spelling, JSON shape)
must be present for every style.
"""
import pytest

from app.services.recap_engine import recap_planner as planner

SENTENCES = [
    "သူဟာ အိမ်ထဲကို ဝင်လာပြီး ပတ်ဝန်းကျင်ကို လှည့်ကြည့်လိုက်တယ်",
    "မီးဖိုချောင်ထဲမှာ ထူးဆန်းတဲ့ အနံ့တစ်ခု ရနေတာကို သတိထားမိတယ်",
    "တံခါးပေါက်ကနေ အပြင်ဘက်ကို ထွက်ပြီး လမ်းမပေါ် လျှောက်သွားခဲ့တယ်",
]


def _capture_prompt(monkeypatch, style: str, is_female: bool = False) -> str:
    captured = {}

    def fake_gemini_json(ai, prompt, retries=1, log=None):
        captured["prompt"] = prompt
        return {"items": [
            {"id": row["id"], "translation": SENTENCES[i]}
            for i, row in enumerate(CURRENT)
        ]}

    monkeypatch.setattr(planner, "_gemini_json", fake_gemini_json)
    planner.translate_batch(
        None,
        source_title="Test Story",
        story_context={},
        previous_context=[],
        current=CURRENT,
        next_context=[],
        log=lambda msg: None,
        narrative_style=style,
        is_female=is_female,
    )
    return captured["prompt"]


CURRENT = [
    {"id": 1, "timestamp": "00:00:01,000 --> 00:00:03,000", "source_text": "He walks in."},
    {"id": 2, "timestamp": "00:00:03,000 --> 00:00:05,000", "source_text": "A strange smell."},
    {"id": 3, "timestamp": "00:00:05,000 --> 00:00:07,000", "source_text": "He leaves."},
]


@pytest.mark.parametrize("style", ["viral", "funny", "pov", "dramatic", "narrator", "standard"])
def test_shared_rules_present_for_all_styles(monkeypatch, style):
    prompt = _capture_prompt(monkeypatch, style)
    # Spelling canon, JSON-only output shape and ID protocol are shared.
    assert "ဖြစ်တယ်" in prompt
    assert 'Return JSON only with top-level key "items"' in prompt
    assert "Return exactly 3 items" in prompt


def test_viral_prompt_has_hook_and_retention_rules(monkeypatch):
    prompt = _capture_prompt(monkeypatch, "viral")
    assert "OPENING HOOK (ID 1 ONLY)" in prompt
    assert "SCENE-BOUNDARY PULL" in prompt
    assert "VARY SENTENCE OPENINGS" in prompt
    # The anti-filler rule must stay — hooks come from the plot, not teasers.
    assert "NEVER insert random generic filler phrases" in prompt


def test_funny_prompt_has_humor_source_and_taste_rules(monkeypatch):
    prompt = _capture_prompt(monkeypatch, "funny")
    assert "HUMOR SOURCE RULE" in prompt
    assert "TASTE RULE" in prompt
    assert "Maximum ONE light beat per segment" in prompt


def test_pov_prompt_has_limited_knowledge_rule(monkeypatch):
    prompt = _capture_prompt(monkeypatch, "pov", is_female=False)
    assert "LIMITED-KNOWLEDGE RULE" in prompt
    assert "ကျွန်တော်" in prompt  # male POV pronoun wired through
    prompt_f = _capture_prompt(monkeypatch, "pov", is_female=True)
    assert "ကျွန်မ" in prompt_f  # female POV pronoun wired through


def test_dramatic_prompt_has_pacing_rules(monkeypatch):
    prompt = _capture_prompt(monkeypatch, "dramatic")
    assert "PACING & RHYTHM RULE" in prompt
    assert "WITHHOLD, DON'T EXPLAIN" in prompt


def test_standard_prompt_has_variety_rule(monkeypatch):
    prompt = _capture_prompt(monkeypatch, "narrator")
    assert "CINEMATIC MOVIE RECAP NARRATOR" in prompt
    assert "VARY SENTENCE OPENINGS" in prompt

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


@pytest.mark.parametrize("style", ["viral", "funny", "pov", "dramatic", "narrator", "standard"])
def test_storytelling_standard_present_for_all_styles(monkeypatch, style):
    # Owner-approved exemplar standard (2026-11-01): scene-by-scene narration a
    # drop-in viewer can follow — shared by every style, not just one.
    prompt = _capture_prompt(monkeypatch, style)
    assert "STORYTELLING STANDARD" in prompt
    assert "DROP-IN COMPREHENSION" in prompt
    assert "NAME THE ACTOR" in prompt
    assert "DIALOGUE BECOMES NARRATION" in prompt
    assert "CONCRETE FACTS" in prompt
    assert "REAL STANDARD WORDS ONLY" in prompt
    assert "ENDINGS MUST MATCH THE SOURCE'S ARC" in prompt
    # The standard form is taught; the colloquial example connectives are gone.
    assert "အဲ့ဒီအခါ" not in prompt
    assert "အဲ့ဒီနောက်" not in prompt


@pytest.mark.parametrize("style", ["viral", "funny", "pov", "dramatic", "narrator", "standard"])
def test_shared_standard_names_and_variety_for_all_styles(monkeypatch, style):
    # All-styles pass (2026-11-01): the shared standard now also carries the
    # name-use rule (from the viral A/B's one remaining miss — a named actor
    # paraphrased into a coined role phrase) and varied sentence openings,
    # which funny/pov/dramatic previously lacked entirely.
    prompt = _capture_prompt(monkeypatch, style)
    assert "USE THE SOURCE'S OWN NAMES" in prompt
    assert "H. VARY SENTENCE OPENINGS" in prompt


def test_pov_prompt_uses_standard_forms_and_scene_anchoring(monkeypatch):
    prompt = _capture_prompt(monkeypatch, "pov", is_female=False)
    # The limited-knowledge example must model the standard form it teaches:
    # the colloquial 'အဲ့တုန်း' example contradicted shared rule F.
    assert "အဲ့တုန်း" not in prompt
    assert "အဲဒီတုန်းက" in prompt
    # Drop-in comprehension, glossed for the first-person frame.
    assert "SCENE ANCHORING IN POV" in prompt


def _capture_audit_prompt(monkeypatch, style: str) -> str:
    captured = {}
    rows = [
        {"id": row["id"], "timestamp": row["timestamp"],
         "source_text": row["source_text"], "translation": SENTENCES[i]}
        for i, row in enumerate(CURRENT)
    ]

    def fake_gemini_json(ai, prompt, retries=1, log=None):
        captured["prompt"] = prompt
        return {"checks": [{"id": row["id"], "status": "pass"} for row in rows]}

    monkeypatch.setattr(planner, "_gemini_json", fake_gemini_json)
    planner.audit_and_repair_batch(
        None,
        rows,
        story_context={},
        previous_context=[],
        next_context=[],
        log=lambda msg: None,
        narrative_style=style,
        is_female=False,
    )
    return captured["prompt"]


@pytest.mark.parametrize("style", ["viral", "funny", "pov", "dramatic", "narrator", "standard"])
def test_audit_checks_source_names_for_all_styles(monkeypatch, style):
    prompt = _capture_audit_prompt(monkeypatch, style)
    assert "called by the source's own names" in prompt
    assert "coined role paraphrases" in prompt


def test_opening_hook_must_establish_premise(monkeypatch):
    # CURRENT starts at ID 1 with no previous context -> first-batch intro rule.
    prompt = _capture_prompt(monkeypatch, "viral")
    assert "must AGREE with the opening scene" in prompt
    assert "premise concretely" in prompt


def test_viral_prompt_has_hook_and_retention_rules(monkeypatch):
    prompt = _capture_prompt(monkeypatch, "viral")
    assert "OPENING HOOK (ID 1 ONLY)" in prompt
    assert "SCENE-BOUNDARY PULL" in prompt
    assert "VARY SENTENCE OPENINGS" in prompt
    # The anti-filler rule must stay — hooks come from the plot, not teasers.
    assert "NEVER insert random generic filler phrases" in prompt
    # Example connectives use the standard form 'အဲဒီ'.
    assert "အဲဒီအခါမှာတော့" in prompt


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
    # Example connectives use the standard form 'အဲဒီ'.
    assert "အဲဒီနောက်" in prompt


@pytest.mark.parametrize("style", ["viral", "funny", "pov", "dramatic", "narrator", "standard"])
def test_spelling_word_choice_discipline_for_all_styles(monkeypatch, style):
    # Owner verdict on jobs rc_1793525723 + rc_1793526555 (2026-11-01): wrong
    # Burmese spelling/word choice and a mechanical, flat-feeling script.
    # The discipline block is shared, so it must ride in every style's prompt.
    prompt = _capture_prompt(monkeypatch, style)
    # The malformed 'တကြီး' adverb family is banned as a spelling rule.
    assert "NEVER the malformed 'တကြီး' adverb family" in prompt
    assert "'အံ့ဩတကြီး'" in prompt and "'စိုးရိမ်တကြီး'" in prompt
    # Foreign names: ONE natural, consistent form; no invented clusters.
    assert "FOREIGN PROPER NAMES" in prompt
    assert "NEVER 'လို့စ်' for Lois" in prompt
    assert "two different spellings in the same story" in prompt
    # Things called by their true names; natural constructions only.
    assert "CALL THINGS WHAT THEY ARE" in prompt
    assert "'အန္တရာယ်ကြားဆီ ရောက်သွားတယ်'" in prompt
    assert "'နင်းခြေတာခံရတော့မှာ'" in prompt
    # Anti-monotony: endings + rhythm vary — liveliness starts in the script.
    assert "VARY SENTENCE ENDINGS & RHYTHM" in prompt
    assert "'…ခဲ့ပါတယ်'" in prompt


@pytest.mark.parametrize("style", ["viral", "funny", "pov", "dramatic", "narrator", "standard"])
def test_audit_checks_spelling_discipline_for_all_styles(monkeypatch, style):
    # The auditor must be able to repair the same defect classes the writer
    # prompt forbids (owner verdict, jobs rc_1793525723 + rc_1793526555).
    prompt = _capture_audit_prompt(monkeypatch, style)
    assert "spelling & word-choice discipline" in prompt
    assert "'လို့စ်'" in prompt
    assert "'စက်ရုပ်', never 'သရဲ'" in prompt


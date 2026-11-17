"""Recap style enrichment + premium renames (Ulfa approved 2026-11-16).

Evidence base: the six-reference style report. These tests pin the NEW
prompt directives per style, the shared density / part-ending / numbers
additions, the aligned audit checklist, and the renamed display labels
(internal style ids unchanged, so saved preferences keep working).
"""
import pathlib
import re

import pytest

from app.services.recap_engine import recap_planner as planner

ROOT = pathlib.Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"
I18N_JS = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")

ALL_STYLES = ["viral", "funny", "pov", "dramatic", "narrator", "standard"]

CURRENT = [
    {"id": 1, "timestamp": "00:00:01,000 --> 00:00:03,000", "source_text": "He walks in."},
    {"id": 2, "timestamp": "00:00:03,000 --> 00:00:05,000", "source_text": "A strange smell."},
    {"id": 3, "timestamp": "00:00:05,000 --> 00:00:07,000", "source_text": "He leaves."},
]
SENTENCES = [
    "သူဟာ အိမ်ထဲကို ဝင်လာပြီး ပတ်ဝန်းကျင်ကို လှည့်ကြည့်လိုက်တယ်",
    "မီးဖိုချောင်ထဲမှာ ထူးဆန်းတဲ့ အနံ့တစ်ခု ရနေတာကို သတိထားမိတယ်",
    "တံခါးပေါက်ကနေ အပြင်ဘက်ကို ထွက်ပြီး လမ်းမပေါ် လျှောက်သွားခဲ့တယ်",
]


def _prompt(monkeypatch, style, *, is_female=False, part_index=1, total_parts=1):
    captured = {}

    def fake_gemini_json(ai, prompt, retries=1, log=None, rotation_tier=None):
        captured["prompt"] = prompt
        return {"items": [{"id": r["id"], "translation": SENTENCES[i]} for i, r in enumerate(CURRENT)]}

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
        part_index=part_index,
        total_parts=total_parts,
    )
    return captured["prompt"]


def _audit_prompt(monkeypatch, style):
    captured = {}
    rows = [
        {"id": r["id"], "timestamp": r["timestamp"], "source_text": r["source_text"], "translation": SENTENCES[i]}
        for i, r in enumerate(CURRENT)
    ]

    def fake_gemini_json(ai, prompt, retries=1, log=None, rotation_tier=None):
        if "AUDITOR" in prompt:
            captured["prompt"] = prompt
        return {"checks": [{"id": r["id"], "status": "pass"} for r in rows]}

    monkeypatch.setattr(planner, "_gemini_json", fake_gemini_json)
    planner.audit_and_repair_batch(
        None, rows, story_context={}, previous_context=[], next_context=[],
        log=lambda msg: None, narrative_style=style, is_female=False,
    )
    return captured["prompt"]


# ── Shared additions (all 5 styles) ──────────────────────────────────

@pytest.mark.parametrize("style", ALL_STYLES)
def test_density_targets_top_of_band_without_filler(monkeypatch, style):
    prompt = _prompt(monkeypatch, style)
    # The band itself is unchanged; density writes to its TOP.
    assert "~10 to 14 Burmese syllables per second" in prompt
    assert "DENSITY TARGET (EVERY STYLE): Write to the TOP of that band (~14 syllables per second of window)" in prompt
    assert "≈19–22 characters per second with no dead air" in prompt
    assert "repeating an already-narrated fact as filler is FORBIDDEN" in prompt
    # Item 5 guardrail: the duration budget beats density.
    assert "the spoken-duration budget always wins over density" in prompt


@pytest.mark.parametrize("style", ALL_STYLES)
def test_part_ending_lands_on_tension_peak(monkeypatch, style):
    prompt = _prompt(monkeypatch, style, part_index=1, total_parts=3)
    assert "PART-END CLIFFHANGER: On the very final segment of Part 1, end ON the tension peak" in prompt
    assert "No summary, no moral, no closing greeting." in prompt
    # Single (non-series) jobs get no part-ending directive.
    single = _prompt(monkeypatch, style)
    assert "PART-END CLIFFHANGER" not in single


@pytest.mark.parametrize("style", ALL_STYLES)
def test_opening_numbers_surface_as_burmese_words(monkeypatch, style):
    prompt = _prompt(monkeypatch, style)
    assert "OPENING NUMBERS: If the story turns on a striking quantity (money, years, counts)" in prompt
    assert "inside the opening hook or the first scene, written as Burmese words" in prompt
    later_part = _prompt(monkeypatch, style, part_index=2, total_parts=3)
    assert "OPENING NUMBERS: If THIS part turns on a striking quantity" in later_part


@pytest.mark.parametrize("style", ALL_STYLES)
def test_shared_hook_defers_to_style_formula_without_restart(monkeypatch, style):
    prompt = _prompt(monkeypatch, style)
    assert "MANDATORY 3-SECOND HOOK (IDs 1–2 FOR EVERY STYLE)" in prompt
    assert "that formula IS this hook" in prompt
    assert "continue chronologically without restarting the story" in prompt
    # The existing premise / agreement guard stays intact.
    assert "must AGREE with the opening scene" in prompt


# ── Viral ────────────────────────────────────────────────────────────

def test_viral_premise_dump_is_primary_and_tease_is_secondary(monkeypatch):
    prompt = _prompt(monkeypatch, "viral")
    assert "INSTANT HOOK — VIRAL MOVIE RECAP STORYTELLER" in prompt
    assert "PREMISE-DUMP OPENING HOOK (IDs 1–3, PRIMARY FORMULA" in prompt
    assert "WHO the protagonist is + the ABNORMAL situation they are in + the TURN ('ဒါပေမဲ့ တစ်ရက်မှာတော့…')" in prompt
    assert "1–4 concrete sentences" in prompt
    assert "Include backstory when the backstory IS the premise" in prompt
    assert "NEVER restart the story at scene 1 after the hook" in prompt
    assert "SECONDARY HOOK — SHOCK TEASE (only when the premise itself is ordinary" in prompt
    assert prompt.index("PREMISE-DUMP OPENING HOOK") < prompt.index("SECONDARY HOOK — SHOCK TEASE")
    # The old ID-1-only tease rule (which conflicted with no-tease-then-restart) is gone.
    assert "OPENING HOOK (ID 1 ONLY)" not in prompt


def test_viral_turn_cadence_and_dramatic_irony(monkeypatch):
    prompt = _prompt(monkeypatch, "viral")
    assert "TURN CADENCE: Land a genuine story turn or reveal roughly every 15–20 seconds of narration" in prompt
    assert "DRAMATIC IRONY TOOL" in prompt
    assert "'ဒါပေမဲ့ သူမသိခဲ့တာက…'" in prompt


def test_viral_generic_filler_ban_unchanged(monkeypatch):
    prompt = _prompt(monkeypatch, "viral")
    assert (
        "- NEVER insert random generic filler phrases ('ကြည့်ပါဦးဗျာ...', 'အဆုံးသတ်မှာ ဘာဆက်ဖြစ်မလဲ...', "
        "'မထင်မှတ်ထားတဲ့ အရာတစ်ခု ဖြစ်လာခဲ့တယ်...') that disconnect consecutive sentences or replace actual plot meaning."
    ) in prompt


# ── Drama ────────────────────────────────────────────────────────────

def test_drama_melodrama_is_default_and_noir_is_sub_mode(monkeypatch):
    prompt = _prompt(monkeypatch, "dramatic")
    assert "DRAMA PRIME — EMOTIONAL MELODRAMA RECAP" in prompt
    assert "DEFAULT VOICE = MELODRAMA REGISTER" in prompt
    assert "MELODRAMA REGISTER (THE DEFAULT" in prompt
    assert "NOIR / THRILLER SUB-MODE (crime, horror, thriller and mystery sources only)" in prompt
    assert prompt.index("MELODRAMA REGISTER (THE DEFAULT") < prompt.index("NOIR / THRILLER SUB-MODE")
    # The old thriller-only identity is gone.
    assert "SUSPENSE / THRILLER DRAMATIC RECAP" not in prompt


def test_drama_melodrama_tools(monkeypatch):
    prompt = _prompt(monkeypatch, "dramatic")
    assert "EVALUATE & SIDE: The narrator may judge and take sides — label emotions and moral stakes in plain words" in prompt
    assert "DRAMATIC IRONY: Tell the audience the truth a character is missing" in prompt
    assert "short staccato sentences are reserved for physical-danger peaks only" in prompt
    assert "2–4 consecutive segments may carry near-verbatim key dialogue lines" in prompt
    assert "paraphrase resumes for the connective tissue" in prompt
    assert "RHETORICAL QUESTIONS: Allowed at climax beats only." in prompt


def test_drama_withhold_scoped_to_plot_facts_never_emotions(monkeypatch):
    prompt = _prompt(monkeypatch, "dramatic")
    assert "WITHHOLD, DON'T EXPLAIN — PLOT FACTS ONLY" in prompt
    assert "This applies to PLOT FACTS only, NEVER to emotions: always say plainly what the characters feel." in prompt


# ── Comedy ───────────────────────────────────────────────────────────

def test_comedy_is_warm_situational_not_stand_up(monkeypatch):
    prompt = _prompt(monkeypatch, "funny")
    assert "LIGHT COMEDY — WARM SITUATIONAL RECAP" in prompt
    assert "Warm situational lightness, NOT stand-up commentary" in prompt
    assert "amused, affectionate tone" in prompt
    # The stand-up example phrase is replaced by amused-aside examples.
    assert "ဟုတ်ကဲ့ပါ တကယ်ကြီး" not in prompt
    assert "'ဖြစ်ချင်တော့...'" in prompt


def test_comedy_inner_thought_and_soft_cap(monkeypatch):
    prompt = _prompt(monkeypatch, "funny")
    assert "INNER-THOUGHT DEVICE" in prompt
    assert "သူ့စိတ်ထဲမှာတော့ '…' လို့ တွေးလိုက်ပါတယ်" in prompt
    assert "The light, amused TONE may run continuously" in prompt
    assert "a JOKE (a light comparison or an amused aside) is still Maximum ONE per segment" in prompt
    # Tragedy taste rule unchanged.
    assert "drop the comedy completely for that segment" in prompt


# ── POV ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("is_female,pronoun", [(False, "ကျွန်တော်"), (True, "ကျွန်မ")])
def test_pov_hook_chekhov_and_momentum(monkeypatch, is_female, pronoun):
    prompt = _prompt(monkeypatch, "pov", is_female=is_female)
    assert "POV OPENING HOOK (IDs 1–3" in prompt
    assert "DANGER (what everyone fears), REWARD (the concrete quantity at stake" in prompt
    assert "MYSTERY (the one warning or rule that will matter later)" in prompt
    assert "CHEKHOV-GUN RULE" in prompt
    assert "Never invent a payoff the source lacks." in prompt
    assert "SHORT-SENTENCE MOMENTUM IN ACTION" in prompt
    assert f"'{pronoun} ပြေးတယ်။" in prompt
    # Unchanged pillars stay.
    for pillar in ("PRONOUN & SPEAKER CLARITY RULE", "LIMITED-KNOWLEDGE RULE", "SCENE ANCHORING IN POV"):
        assert pillar in prompt


# ── Normal / narrator ────────────────────────────────────────────────

@pytest.mark.parametrize("style", ["narrator", "standard"])
def test_narrator_dialogue_sentence_and_beat_rules(monkeypatch, style):
    prompt = _prompt(monkeypatch, style)
    assert "CINEMATIC STORYTELLER — CINEMATIC MOVIE RECAP NARRATOR" in prompt
    assert "EMBEDDED-DIALOGUE RULE: Default to reported speech folded into the narration" in prompt
    assert "'…လို့ လျှောက်ထားပါတယ်'" in prompt
    assert "A verbatim quote is reserved for at most the single most important line of a scene." in prompt
    assert "Long, flowing sentences (up to ~110 characters) are welcome in calm scenes" in prompt
    assert "targets SENTENCE ENDINGS ('သည်' / 'သဖြင့်') only — never sentence length" in prompt
    assert "EPISODIC BEAT PACING: Each scene resolves into ONE clear beat" in prompt


# ── Audit checklist ──────────────────────────────────────────────────

def test_audit_checklist_wording_aligned(monkeypatch):
    viral = _audit_prompt(monkeypatch, "viral")
    assert "the premise hook must be present in IDs 1–3" in viral
    assert "with no generic teaser" in viral
    dramatic = _audit_prompt(monkeypatch, "dramatic")
    assert "melodrama register by default — emotions labelled in plain words" in dramatic
    assert "dramatic irony used where the source allows" in dramatic
    assert "feelings explained even when plot facts are withheld" in dramatic
    funny = _audit_prompt(monkeypatch, "funny")
    assert "warm situational lightness; inner-thought or amused aside allowed; no stand-up commentary" in funny
    assert "playful colloquial irony" not in funny
    narrator = _audit_prompt(monkeypatch, "narrator")
    assert "bookish sentence ENDINGS (သည်/သဖြင့်)" in narrator
    assert "stiff bookish phrasing" not in narrator


# ── Renamed labels (ids unchanged) ───────────────────────────────────

RENAMES = {
    "style_viral": ("ချက်ချင်းစွဲဆောင်", "Instant Hook"),
    "style_dramatic": ("ဒရာမာ ပရိုင်း", "Drama Prime"),
    "style_funny": ("ပေါ့ပါးဟာသ", "Light Comedy"),
    "style_pov": ("ကိုယ်တွေ့ ရှုထောင့်", "First-Person POV"),
    "style_standard": ("ရုပ်ရှင်ဟန် ဇာတ်လမ်းပြော", "Cinematic Storyteller"),
}


@pytest.mark.parametrize("key", sorted(RENAMES))
def test_style_labels_renamed_in_my_and_en(key):
    my, en = RENAMES[key]
    vals = re.findall(rf'\b{key}:\s*"((?:[^"\\]|\\.)*)"', I18N_JS)
    assert vals == [my, en], key
    # The static fallback text matches the Burmese default (no hardcoded drift).
    assert f'data-i18n="{key}">{my}</span>' in INDEX_HTML


def test_internal_style_ids_unchanged():
    for story in ("viral", "dramatic", "funny", "pov", "standard"):
        assert f'data-story="{story}"' in INDEX_HTML
    for old in ("Viral Hook", "Classic Narration", "Character POV", "Dramatic (Thriller)"):
        assert old not in I18N_JS and old not in INDEX_HTML


def test_cache_token_bumped_to_r158():
    assert INDEX_HTML.count("?v=20261116_r159") == 4
    assert "r157" not in INDEX_HTML

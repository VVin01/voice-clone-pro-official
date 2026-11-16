"""Tests for the recap ending-credit-leak fix (2026-11-16).

Production evidence: proof recaps rc_1794837636_f109db (POV) and
rc_1794837982_b5ddab (Drama Prime) of an ARY Digital promo both END
their narration with the SOURCE channel's credit line — the planner
copied the source's end-card into the script.

This module pins:
  1. The shared no-credit rule in the translate prompt for ALL 5 styles.
  2. The auditor's credit/sign-off detection + prompt rule.
  3. The ``is_credit_or_signoff`` detector on both evidence sentences.
  4. The ``audit_script_ending_credit`` repair function (drop only).
  5. Cache token bump to 20261116_r159.
"""
import pytest
from pathlib import Path

from app.services.recap_engine import recap_planner as planner

# ── Fixtures: the two production evidence sentences ────────────────

EVIDENCE_POV = "အေအာရ်ဝိုင် ပလပ်စ်အတွက် စောင့်နေပါတယ်။"
EVIDENCE_DRAMATIC = "အေအာရ်ဝိုင် ပလပ်စ်အတွက် တင်ဆက်ပေးခဲ့တာပါ။"

# ── Shared translate-prompt rule for ALL styles ────────────────────

SENTENCES = [
    "သူဟာ အိမ်ထဲကို ဝင်လာပြီး ပတ်ဝန်းကျင်ကို လှည့်ကြည့်လိုက်တယ်",
    "မီးဖိုချောင်ထဲမှာ ထူးဆန်းတဲ့ အနံ့တစ်ခု ရနေတာကို သတိထားမိတယ်",
    "တံခါးပေါက်ကနေ အပြင်ဘက်ကို ထွက်ပြီး လမ်းမပေါ် လျှောက်သွားခဲ့တယ်",
]

CURRENT = [
    {"id": 1, "timestamp": "00:00:01,000 --> 00:00:03,000", "source_text": "He walks in."},
    {"id": 2, "timestamp": "00:00:03,000 --> 00:00:05,000", "source_text": "A strange smell."},
    {"id": 3, "timestamp": "00:00:05,000 --> 00:00:07,000", "source_text": "He leaves."},
]


def _capture_prompt(monkeypatch, style: str, is_female: bool = False) -> str:
    captured = {}

    def fake_gemini_json(ai, prompt, retries=1, log=None, rotation_tier=None):
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


@pytest.mark.parametrize("style", ["viral", "funny", "pov", "dramatic", "narrator", "standard"])
def test_no_credit_rule_in_translate_prompt_for_all_styles(monkeypatch, style):
    """Shared rule K must appear in the translate prompt for every style."""
    prompt = _capture_prompt(monkeypatch, style)
    assert "NO SOURCE-CHANNEL CREDITS OR SIGN-OFFS" in prompt
    assert "source branding" in prompt.lower() or "source branding" in prompt
    assert "end-card" in prompt


# ── Auditor prompt must also carry the credit rule ─────────────────

def _capture_audit_prompt(monkeypatch, style: str) -> str:
    captured = {}
    rows = [
        {"id": row["id"], "timestamp": row["timestamp"],
         "source_text": row["source_text"], "translation": SENTENCES[i]}
        for i, row in enumerate(CURRENT)
    ]

    def fake_gemini_json(ai, prompt, retries=1, log=None, rotation_tier=None):
        captured.setdefault("prompt", prompt)
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
def test_audit_prompt_carries_credit_rule_for_all_styles(monkeypatch, style):
    """The auditor prompt must explicitly check for credit/sign-off leaks."""
    prompt = _capture_audit_prompt(monkeypatch, style)
    assert "credit" in prompt.lower()
    assert "sign-off" in prompt.lower()


# ── is_credit_or_signoff detector ──────────────────────────────────

def test_evidence_pov_detected():
    """Production evidence POV sentence must be flagged."""
    assert planner.is_credit_or_signoff(EVIDENCE_POV)


def test_evidence_dramatic_detected():
    """Production evidence Drama Prime sentence must be flagged."""
    assert planner.is_credit_or_signoff(EVIDENCE_DRAMATIC)


@pytest.mark.parametrize("text", [
    "ARY Plus အတွက် စောင့်နေပါတယ်",
    "ARY Digital အတွက် တင်ဆက်ပေးခဲ့တာပါ",
    "ချန်နယ် ကို subscribe လုပ်ပေးပါ",
    "Presented by Star Plus",
    "Subscribe to our channel",
    "Thanks for watching",
    "Don't forget to subscribe and like",
    "Brought to you by ZEE TV",
    "See you in the next episode",
])
def test_credit_signoff_patterns_detected(text):
    assert planner.is_credit_or_signoff(text), f"Should flag: {text!r}"


@pytest.mark.parametrize("text", [
    "သူ့ရဲ့ နောက်ဆုံး ဆုံးဖြတ်ချက်ကတော့ အံ့ဩစရာပါပဲ",
    "ဒီလူဆိုးရဲ့ အစီအစဉ်ကို သူ ဘယ်လိုတားဆီးနိုင်မလဲ",
    "He decided to fight back one last time",
    "The villain's plan was about to unfold",
])
def test_story_sentences_not_flagged(text):
    assert not planner.is_credit_or_signoff(text), f"Should NOT flag: {text!r}"


# ── audit_script_ending_credit repair ──────────────────────────────

def test_audit_drops_credit_ending():
    """When the last row is a credit line, it must be dropped."""
    rows = [
        {"id": 1, "translation": "ဇာတ်လမ်းစတင်ပါပြီ"},
        {"id": 2, "translation": "ဒီနေ့ဇာတ်လမ်းက ဆက်လက်ကြည့်ပါ"},
        {"id": 3, "translation": EVIDENCE_POV},
    ]
    result = planner.audit_script_ending_credit(rows)
    assert len(result) == 2
    assert result[-1]["id"] == 2


def test_audit_drops_dramatic_credit_ending():
    """Drama Prime evidence sentence must also be dropped."""
    rows = [
        {"id": 10, "translation": "သူတို့ရဲ့ စစ်ပွဲက ဆက်ရှိနေဆဲပါ"},
        {"id": 11, "translation": EVIDENCE_DRAMATIC},
    ]
    result = planner.audit_script_ending_credit(rows)
    assert len(result) == 1
    assert result[0]["id"] == 10


def test_audit_preserves_clean_ending():
    """Non-credit endings must pass through unchanged."""
    rows = [
        {"id": 1, "translation": "ဇာတ်လမ်းစတင်ပါပြီ"},
        {"id": 2, "translation": "ဒီလူဆိုးရဲ့ အစီအစဉ်ကို သူ ဘယ်လိုတားဆီးနိုင်မလဲ"},
    ]
    result = planner.audit_script_ending_credit(rows)
    assert len(result) == 2
    assert result[-1]["id"] == 2


def test_audit_empty_rows():
    """Empty input returns empty output."""
    assert planner.audit_script_ending_credit([]) == []


def test_audit_does_not_mutate_input():
    """The input list must not be modified in place."""
    rows = [
        {"id": 1, "translation": "ဇာတ်လမ်းစတင်ပါပြီ"},
        {"id": 2, "translation": EVIDENCE_POV},
    ]
    original_len = len(rows)
    planner.audit_script_ending_credit(rows)
    assert len(rows) == original_len


# ── _batch_audit_flags: credit flag integration ───────────────────

def test_batch_audit_flags_detects_credit_ending():
    """_batch_audit_flags must fire when the last row is a credit line."""
    rows = [
        {"id": 1, "translation": "ဇာတ်လမ်းစတင်ပါပြီ"},
        {"id": 2, "translation": EVIDENCE_POV},
    ]
    flags = planner._batch_audit_flags(rows, [], [])
    credit_flags = [f for f in flags if "credit" in f.lower() or "sign-off" in f.lower()]
    assert credit_flags, f"Expected credit flag, got: {flags}"


def test_batch_audit_flags_clean_ending():
    """Clean endings must not produce a credit flag."""
    rows = [
        {"id": 1, "translation": "ဇာတ်လမ်းစတင်ပါပြီ"},
        {"id": 2, "translation": "ဒီလူဆိုးရဲ့ အစီအစဉ်ကို သူ ဘယ်လိုတားဆီးနိုင်မလဲ"},
    ]
    flags = planner._batch_audit_flags(rows, [], [])
    credit_flags = [f for f in flags if "credit" in f.lower() or "sign-off" in f.lower()]
    assert not credit_flags


# ── Integration tests: ending-credit repair wiring before synthesis ─

def test_integration_credit_ending_script_loses_exactly_last_sentence(monkeypatch):
    """Integration test proving a credit-ending script loses exactly its last sentence
    in the audit/repair stage before synthesis without rewriting earlier sentences."""
    rows = [
        {"id": 1, "timestamp": "00:00:01,000 --> 00:00:04,000", "source_text": "He enters the room.", "translation": "သူ အခန်းထဲကို ဝင်လာခဲ့တယ်"},
        {"id": 2, "timestamp": "00:00:04,000 --> 00:00:07,000", "source_text": "He finds the secret document.", "translation": "လျှို့ဝှက်စာရွက်စာတမ်းကို သူ တွေ့ရှိသွားခဲ့တယ်"},
        {"id": 3, "timestamp": "00:00:07,000 --> 00:00:10,000", "source_text": "Presented for ARY Plus.", "translation": EVIDENCE_POV},
    ]

    llm_calls = []

    def fake_gemini_json(ai, prompt, retries=1, log=None, rotation_tier=None):
        llm_calls.append(prompt)
        return {"checks": [{"id": r["id"], "status": "pass"} for r in rows[:2]]}

    monkeypatch.setattr(planner, "_gemini_json", fake_gemini_json)

    logs = []
    result = planner._finalize_batch_translation(
        ai=None,
        translated=list(rows),
        bi=0,
        total_batches=1,
        story_context={"title": "Test Story"},
        prev_committed=[],
        nxt=[],
        log=logs.append,
        translation_audit=True,
        narrative_style="pov",
        is_female=False,
    )

    # Must lose EXACTLY its last sentence (length 3 -> 2)
    assert len(result) == 2
    assert result[0]["id"] == 1 and result[0]["translation"] == rows[0]["translation"]
    assert result[1]["id"] == 2 and result[1]["translation"] == rows[1]["translation"]
    # The credit line was dropped, never rewritten
    assert not any(r["id"] == 3 for r in result)
    assert not any(EVIDENCE_POV in r["translation"] for r in result)


def test_integration_core_normalize_segments_drops_credit_ending():
    """normalize_segments must drop the credit-ending sentence before audio synthesis."""
    from app.services.recap_engine import core

    plan = {
        "segments": [
            {"video_start": 0.0, "video_end": 5.0, "narration": "သူ အိမ်ထဲသို့ ဝင်လာခဲ့သည်"},
            {"video_start": 5.0, "video_end": 10.0, "narration": EVIDENCE_DRAMATIC},
        ]
    }
    segs = core.normalize_segments(plan)
    assert len(segs) == 1
    assert segs[0]["narration"] == "သူ အိမ်ထဲသို့ ဝင်လာခဲ့သည်"
    assert EVIDENCE_DRAMATIC not in segs[0]["narration"]



# ── Cache token pin ────────────────────────────────────────────────

_INDEX_HTML = Path(__file__).resolve().parents[2] / "app" / "webapp" / "static" / "index.html"
INDEX_HTML = _INDEX_HTML.read_text(encoding="utf-8")


def test_cache_token_r159():
    assert INDEX_HTML.count("?v=20261116_r159") == 4


def test_cache_token_no_stale_r157():
    assert "20261116_r157" not in INDEX_HTML

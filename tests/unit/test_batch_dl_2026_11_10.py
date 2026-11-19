"""Batch DL pins (2026-11-10): own-voice recap fact in the AI sheet.

Group AI and Private AI share ONE knowledge sheet —
``group_ai_admin.build_fact_sheet()`` — which both prompt builders
embed verbatim. Ulfa approved adding one fact to the sheet's Recap
section: plan holders can have a recap narrated with their own cloned
voice at no extra voice cost (Recap Credit only), while Free accounts
get Gemini / Edge narration only. Before DL the sheet listed the Recap
engines (Persona + Gemini) but never stated this, so the AIs had to
escalate such questions to admins.

These tests pin the approved line's presence, its exact wording, its
position right after the Recap engine line, and that BOTH prompt
paths (group + private) carry it because they share the one builder.
"""

from app.services import group_ai_admin as gai
from app.services import private_ai as pai

APPROVED_LINE = (
    "• Recap ပြောပြသူ အသံ: Plan ဝယ်ထားသူတွေက Persona အသံ ၁၂ မျိုးတင် မကဘဲ "
    "ကိုယ်တိုင် Clone လုပ်ထားတဲ့ ကိုယ်ပိုင် အသံနဲ့ပါ Recap ကို ပြောခိုင်းလို့ ရပါတယ် — "
    "Persona / Custom Voice Clone (VoxCPM) သုံးရင် Voice Audio Duration အတိုင်း Voice Clone မိနစ်ကို သီးခြားဖြတ်ပြီး "
    "Normal AI Voice (Gemini/Edge) နဲ့ Subtitle-Only မှာ Voice Clone မိနစ် မဖြတ်ပါဘူး။ "
    "Free အကောင့်ကတော့ Gemini / Edge / Subtitle-Only နဲ့ပဲ သုံးနိုင်ပါတယ်။"
)

FRAGMENTS = (
    "ကိုယ်ပိုင် အသံနဲ့ပါ",
    "Voice Clone မိနစ် မဖြတ်ပါဘူး",
    "Gemini / Edge",
)


def test_sheet_contains_own_voice_recap_fragments():
    sheet = gai.build_fact_sheet()
    for fragment in FRAGMENTS:
        assert fragment in sheet


def test_sheet_carries_the_approved_line_verbatim_once():
    sheet = gai.build_fact_sheet()
    assert sheet.count(APPROVED_LINE) == 1
    assert APPROVED_LINE in sheet.splitlines()


def test_approved_line_sits_right_after_the_recap_engine_line():
    lines = gai.build_fact_sheet().splitlines()
    engine_idx = next(
        i for i, line in enumerate(lines) if line.startswith("• အသံ Engine ")
    )
    assert lines[engine_idx + 1] == APPROVED_LINE


def test_group_ai_prompt_carries_the_fact():
    prompt = gai.build_prompt(
        author_name="Member",
        text="Recap ကို ကိုယ့်အသံနဲ့ ပြောခိုင်းလို့ ရလား",
        bot_mentioned=True,
        is_reply_to_bot=False,
    )
    for fragment in FRAGMENTS:
        assert fragment in prompt


def test_private_ai_prompt_carries_the_fact():
    prompt = pai.build_private_prompt(
        text="Recap ကို ကိုယ့်အသံနဲ့ ပြောခိုင်းလို့ ရလား",
        author_name="Aung",
    )
    for fragment in FRAGMENTS:
        assert fragment in prompt

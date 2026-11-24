"""Tests for AI Speech-Style Lock — Always ရှင်/ရှင့်, Never ဗျာ/ခင်ဗျာ (2026-11-24).

Covers:
1. Prompt pins across Group AI SYSTEM_INSTRUCTIONS, Private AI PRIVATE_SYSTEM_INSTRUCTIONS,
   and all three DRAFT_INSTRUCTIONS_* blocks.
2. Deterministic normalizer (`gai.normalize_ai_speech_style`):
   - ခင်ဗျာ -> ရှင်, attached verb-final *ဗျာ -> *ရှင်, standalone clause/sentence-final ဗျာ -> ရှင်
   - Mixed-paragraph normalization
   - Non-corruption of root words (ဗျာပါ, ဗျာဒိ, ကဗျာ, ခင်ဗျား)
   - Preservation of URLs, @mentions, numbers, English text
   - Idempotence
3. Wiring through `ask_group_admin_ai`, `ask_private_ai`, and `draft_escalation_answer`,
   plus confirmation that non-AI male voice samples remain untouched.
"""

from __future__ import annotations

import pytest

from app.services import group_ai_admin as gai
from app.services import private_ai as pai


# ===========================================================================
# 1. Prompt Pins
# ===========================================================================


def test_group_and_private_prompts_lock_female_persona_and_ban_male_endings():
    for label, prompt in (
        ("group", gai.SYSTEM_INSTRUCTIONS),
        ("private", pai.PRIVATE_SYSTEM_INSTRUCTIONS),
    ):
        assert "အစ်မ Senior Admin" in prompt, label
        assert "အစ်မ/အစ်ကို" not in prompt, label
        assert '"ရှင့်"/"ဗျာ"' not in prompt, label
        assert '"ဟုတ်ကဲ့ဗျာ"' not in prompt, label
        assert '"ဟုတ်ကဲ့ရှင်"' in prompt, label
        assert '"ရှင်"/"ရှင့်" ကိုသာ အမြဲ သုံးရမည်' in prompt, label
        assert '"ဗျာ" နှင့် "ခင်ဗျာ" ကို အဖြေ မည်သည့်နေရာတွင်မှ လုံးဝ မသုံးရ' in prompt, label


def test_draft_prompts_ban_khinbya_and_bya():
    for label, prompt in (
        ("sensitive", gai.DRAFT_INSTRUCTIONS_SENSITIVE),
        ("unsure", gai.DRAFT_INSTRUCTIONS_UNSURE),
        ("sensitive_v2", gai.DRAFT_INSTRUCTIONS_SENSITIVE_GROUP_V2),
    ):
        assert 'ခင်ဗျာ" အများဆုံး' not in prompt, label
        assert "ခင်ဗျာ/ဗျာ လုံးဝ မသုံးရ — စကားပိတ်က ရှင်/ရှင့် ပဲ သုံးပါ" in prompt, label


# ===========================================================================
# 2. Normalizer Units
# ===========================================================================


@pytest.mark.parametrize(
    "raw,expected",
    [
        # ခင်ဗျာ -> ရှင် everywhere
        ("မင်္ဂလာပါ ခင်ဗျာ", "မင်္ဂလာပါ ရှင်"),
        ("မင်္ဂလာပါခင်ဗျာ။", "မင်္ဂလာပါရှင်။"),
        ("ခင်ဗျာ၊ စမ်းကြည့်လို့ ရပါတယ်။", "ရှင်၊ စမ်းကြည့်လို့ ရပါတယ်။"),
        ("ကျေးဇူးတင်ပါတယ် ခင်ဗျာ့။", "ကျေးဇူးတင်ပါတယ် ရှင့်။"),
        # Attached verb/response-final forms
        ("စမ်းကြည့်ပါဗျာ", "စမ်းကြည့်ပါရှင်"),
        ("စမ်းကြည့်ပါဗျာ။", "စမ်းကြည့်ပါရှင်။"),
        ("ရပါတယ်ဗျာ။", "ရပါတယ်ရှင်။"),
        ("ကူညီပေးမယ်ဗျာ။", "ကူညီပေးမယ်ရှင်။"),
        ("ဟုတ်လားဗျာ?", "ဟုတ်လားရှင်?"),
        ("ဟုတ်ကဲ့ဗျာ၊ ရပါတယ်။", "ဟုတ်ကဲ့ရှင်၊ ရပါတယ်။"),
        ("စိတ်မပူပါနဲ့နော်ဗျာ", "စိတ်မပူပါနဲ့နော်ရှင်"),
        ("မကုန်ပါဘူးဗျာ။", "မကုန်ပါဘူးရှင်။"),
        ("ပြီးပါပြီဗျာ။", "ပြီးပါပြီရှင်။"),
        # Standalone sentence/clause-final ဗျာ with punctuation preserved
        ("ရပါတယ် ဗျာ။", "ရပါတယ် ရှင်။"),
        ("ဟုတ်ကဲ့ ဗျာ!", "ဟုတ်ကဲ့ ရှင်!"),
        ("ဟုတ်လား ဗျာ?", "ဟုတ်လား ရှင်?"),
        ("ရပါတယ် ဗျာ, စမ်းကြည့်ပါ", "ရပါတယ် ရှင်, စမ်းကြည့်ပါ"),
        ("ရပါတယ် ဗျာ၊ စမ်းကြည့်ပါ", "ရပါတယ် ရှင်၊ စမ်းကြည့်ပါ"),
        ("ရပါတယ် ဗျာ\nနောက်တစ်ကြောင်းပါ", "ရပါတယ် ရှင်\nနောက်တစ်ကြောင်းပါ"),
        ("ရပါတယ် ဗျာ", "ရပါတယ် ရှင်"),
        ("ဗျာ။", "ရှင်။"),
        # Mixed paragraph
        (
            "ရပါတယ်ဗျာ။ စိတ်မပူပါနဲ့ ခင်ဗျာ",
            "ရပါတယ်ရှင်။ စိတ်မပူပါနဲ့ ရှင်",
        ),
        (
            "ဟုတ်ကဲ့ဗျာ၊ @voiceclonepro_bot မှာ စမ်းသုံးလို့ ရပါတယ်ဗျာ။\nအခက်အခဲရှိရင် မေးနိုင်ပါတယ် ခင်ဗျာ!",
            "ဟုတ်ကဲ့ရှင်၊ @voiceclonepro_bot မှာ စမ်းသုံးလို့ ရပါတယ်ရှင်။\nအခက်အခဲရှိရင် မေးနိုင်ပါတယ် ရှင်!",
        ),
    ],
)
def test_normalize_ai_speech_style_conversions(raw, expected):
    out = gai.normalize_ai_speech_style(raw)
    assert out == expected
    assert "ခင်ဗျာ" not in out
    assert "ဗျာ" not in out
    # Idempotent
    assert gai.normalize_ai_speech_style(out) == out


@pytest.mark.parametrize(
    "unchanged",
    [
        # Words where ဗျာ is part of a word, not a final particle
        "ဗျာပါမများပါနဲ့ရှင်။",
        "ဗျာဒိစကား",
        "ဗျာဒိတ်တော်",
        "ကဗျာရေးချင်တယ်ရှင့်။",
        "ဒီကဗျာ။",
        "ကဗျာ",
        # Pronoun ခင်ဗျား ("you") must not corrupt into ရှင်း
        "ခင်ဗျားရဲ့ အသံကို စမ်းကြည့်နိုင်ပါတယ်။",
        # Already female forms (idempotent)
        "မင်္ဂလာပါရှင့်။ ရပါတယ်ရှင်။",
        "စိတ်မပူပါနဲ့နော် — @voiceclonepro_bot မှာ စမ်းကြည့်နိုင်ပါတယ်ရှင့်။",
        # URLs, @mentions, numbers, English text untouched
        "https://t.me/VoiceClonePro @voiceclonepro_bot @VoiceCloneProAdmin 15,000 MMK Voice Clone Pro 30 Days",
        # Empty
        "",
    ],
)
def test_normalize_ai_speech_style_non_corruption_and_idempotence(unchanged):
    assert gai.normalize_ai_speech_style(unchanged) == unchanged
    assert gai.normalize_ai_speech_style(gai.normalize_ai_speech_style(unchanged)) == unchanged


def test_normalize_ai_speech_style_none_safe():
    assert gai.normalize_ai_speech_style(None) == ""


# ===========================================================================
# 3. Wiring Through Group AI, Private AI, and Escalation Draft Paths
# ===========================================================================


@pytest.mark.asyncio
async def test_ask_group_admin_ai_normalizes_reply(monkeypatch):
    monkeypatch.setattr(
        gai,
        "_ask_ai_sync",
        lambda prompt, client_factory=None, groq_requester=None: (
            "MODERATION: category=none confidence=0.0\n"
            "REPLY: ရပါတယ်ဗျာ။ စိတ်မပူပါနဲ့ ခင်ဗျာ — @voiceclonepro_bot မှာ စမ်းကြည့်ပါဗျာ!"
        ),
    )
    decision, reply = await gai.ask_group_admin_ai(text="ဘယ်လိုသုံးရမလဲ", author_name="Aung")
    assert decision == gai.DECISION_REPLY
    assert reply == "ရပါတယ်ရှင်။ စိတ်မပူပါနဲ့ ရှင် — @voiceclonepro_bot မှာ စမ်းကြည့်ပါရှင်!"
    assert "ဗျာ" not in reply
    assert "ခင်ဗျာ" not in reply


@pytest.mark.asyncio
async def test_ask_private_ai_normalizes_reply(monkeypatch):
    monkeypatch.setattr(
        gai,
        "_ask_ai_sync",
        lambda prompt, client_factory=None, groq_requester=None: (
            "REPLY: ဟုတ်ကဲ့ဗျာ၊ Starter Plan က 15,000 MMK ပါ ခင်ဗျာ။ စမ်းကြည့်ပါဗျာ"
        ),
    )
    decision, reply = await pai.ask_private_ai(text="စျေးနှုန်းသိချင်တယ်", author_name="Su")
    assert decision == gai.DECISION_REPLY
    assert reply == "ဟုတ်ကဲ့ရှင်၊ Starter Plan က 15,000 MMK ပါ ရှင်။ စမ်းကြည့်ပါရှင်"
    assert "ဗျာ" not in reply
    assert "ခင်ဗျာ" not in reply


@pytest.mark.asyncio
async def test_draft_escalation_answer_normalizes_draft(monkeypatch):
    monkeypatch.setattr(
        gai,
        "_ask_ai_sync",
        lambda prompt, client_factory=None, groq_requester=None: (
            "Admin က စစ်ဆေးပေးနေပါတယ်ဗျာ။ ခဏစောင့်ပေးပါ ခင်ဗျာ။"
        ),
    )
    draft = await gai.draft_escalation_answer(text="ငွေသွင်းပြီး မဝင်သေး", reason="sensitive")
    assert draft == "Admin က စစ်ဆေးပေးနေပါတယ်ရှင်။ ခဏစောင့်ပေးပါ ရှင်။"
    assert "ဗျာ" not in draft
    assert "ခင်ဗျာ" not in draft


def test_non_ai_male_curated_voice_samples_untouched():
    """Male persona voice samples legitimately keep ခင်ဗျာ and are never normalized."""
    from app.services.curated_voices import CURATED_VOICES

    assert "ခင်ဗျာ" in CURATED_VOICES["komyat"]["sample_text"]

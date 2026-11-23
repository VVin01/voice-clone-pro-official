"""Part H — Group AI & Private AI re-teach (facts audit + human persona, 2026-11-23)."""
from __future__ import annotations

import inspect

from app.config import settings
from app.services import group_ai_admin as gai
from app.services import private_ai as pai


def test_fact_sheet_states_current_product_facts_and_zero_tiered_pricing(monkeypatch):
    # Even if legacy tiered flag were somehow flipped in memory, fact sheet MUST state flat pricing only
    monkeypatch.setattr(settings, "tiered_recap_pricing_enabled", True)
    sheet = gai.build_fact_sheet()

    # No tiered-pricing wording or Batch BE branch anywhere
    src = inspect.getsource(gai.build_fact_sheet)
    assert "tiered_recap_pricing_enabled" not in src
    assert "Top-up Plan" not in sheet
    assert "တစ်ပိုင်းလျှင် 1 Credit" not in sheet

    # Core v3 + Limits + Round 13 ground truth facts
    assert "180 စက္ကန့်" in sheet and "3 မိနစ်" in sheet and "မပေါင်းပါ" in sheet
    assert "60 စက္ကန့်" in sheet and "အကန့်အသတ် မရှိ" in sheet
    assert "3,000" in sheet and "15,000" in sheet
    assert "တစ်နေ့ 15 မိနစ်" in sheet and "တစ်နေ့ 60 မိနစ်" in sheet
    assert "တစ်နေ့ 2 မိနစ်" in sheet and "တစ်နေ့ 10 မိနစ်" in sheet
    assert "1 Credit = Final Recap Output 1 မိနစ်" in sheet
    assert "120 စက္ကန့်" in sheet and "600 စက္ကန့်" in sheet and "Watermark" in sheet
    assert "Dual-Burn" in sheet and "Profile" in sheet
    assert "ခလုတ် ၃ ခုသာ" in sheet and "မိတ်ဆက်ရန်" in sheet
    assert "Buy (ဝယ်ရန်)" in sheet
    assert "ဇာတ်လမ်း စတိုင် ၄ မျိုး" in sheet
    assert "ဇာတ်လမ်း စတိုင် ၆ မျိုး" not in sheet
    assert "Referral ဆု စက္ကန့်ကိုပဲ သုံး" not in sheet


def test_human_persona_and_safety_guardrails_in_group_and_private_ai():
    # Group AI persona & safety guardrails
    sys_g = gai.SYSTEM_INSTRUCTIONS
    assert "ဝါကျ ၁–၃ ကြောင်း" in sys_g
    assert "အဲ့ဒါတော့ မသေချာဘူး" in sys_g
    assert "@VoiceCloneProAdmin" in sys_g
    assert "Emoji လုံးဝ မထည့်ရပါ" in sys_g
    assert "မြန်မာဘာသာ သက်သက်" in sys_g
    assert "၃၀ ရက် (30 Days)" in sys_g
    assert "Grandfathered" in sys_g
    assert "MODERATION:" in sys_g

    # Private AI persona & v3 validity guardrails (no stale blanket 30-day or Top-Up)
    sys_p = pai.PRIVATE_SYSTEM_INSTRUCTIONS
    guard_p = pai.PRIVATE_SYSTEM_INSTRUCTIONS_V2_GUARDRAILS
    assert "ဝါကျ ၁–၃ ကြောင်း" in sys_p
    assert "အဲ့ဒါတော့ မသေချာဘူး" in sys_p
    assert "@VoiceCloneProAdmin" in sys_p
    assert "Top-Up" not in guard_p
    assert "Starter နှင့် Creator Package များသည်" in guard_p
    assert "Pro Package သည် သက်တမ်း ၉၀ ရက် (90 Days)" in guard_p
    assert "သက်တမ်း မကုန် (No Expiry)" in guard_p

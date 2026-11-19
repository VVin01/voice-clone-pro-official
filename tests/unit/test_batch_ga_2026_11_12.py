"""Batch GA pins (2026-11-12): Group AI knowledge completeness + no skips.

Owner report, verified firsthand in the live group on 2026-11-12:

* The knowledge sheet carried NO expiry fact for purchased balances,
  so at 11:48 the AI invented one — "Plan တွေမှာ ပါတဲ့ အသံမိနစ်
  ဒါမှမဟုတ် Recap Credit တွေက တစ်လ သက်တမ်း ရှိပါတယ်ရှင်" — the opposite
  of the product truth (purchased minutes/credits NEVER expire: no
  ledger expiry exists and the product copy states သက်တမ်းမကုန်).
  Ulfa had to correct it in the group by hand.
* In the same 11:41–11:52 burst, roughly half of two members'
  follow-up questions got NO_REPLY; a "မဖြေသေးဘူး" nudge was answered
  by repeating the price list instead of the pending question.
* The FREE trio (voice monthly / recap one-time / TTS daily) was
  scattered across the sheet, so "တခါပဲလား နေ့တိုင်းလား" drew
  partial answers.

GA fixes: an explicit expiry block + a FREE-trio summary line in the
shared fact sheet (Group AI and Private AI both consume it), follow-up
and nudge rules in the decision instructions, and a wider in-memory
context window (10 → 16) so interleaved bursts keep the pending
question in view. Ops-side companions shipped the same day from the
DB (budgets 80/600, seeded FAQ rows) are not covered here.
"""

from app.services import group_ai_admin as gai
from app.services import private_ai as pai  # noqa: F401  (shared-sheet consumer)


def test_sheet_states_purchased_balances_30_day_validity_and_grandfather():
    # Policy updated on 2026-11-19: newly purchased packages carry 30-day
    # validity (FEFO consumption), while legacy pre-migration balances remain
    # grandfathered (no expiry).
    sheet = gai.build_fact_sheet()
    assert "သက်တမ်း (အရေးကြီး)" in sheet
    assert "သက်တမ်း ၃၀ ရက် (30 Days)" in sheet
    assert "FEFO" in sheet
    assert "Grandfathered" in sheet


def test_sheet_never_claims_a_plan_expiry():
    # Must explicitly state the exact 30-day + grandfather rule rather than
    # vague/hallucinated expiry claims.
    sheet = gai.build_fact_sheet()
    assert "တစ်လ သက်တမ်း ရှိပါတယ်" not in sheet
    assert "တစ်လအတွင်း ကုန်သွား" not in sheet


def test_sheet_forbids_monthly_plan_framing():
    sheet = gai.build_fact_sheet()
    assert "သက်တမ်း ၃၀ ရက် (30 Days)" in sheet
    assert "၃ ရက်အလို" in sheet and "၂၄ နာရီအလို" in sheet


def test_sheet_separates_free_reset_from_purchased_balance():
    sheet = gai.build_fact_sheet()
    assert "မရောပါနှင့်" in sheet
    assert "ဝယ်ထားတဲ့ ၃၀ ရက် Package များနှင့် သီးခြားစီ ဖြစ်ပါတယ်" in sheet
    # ...while still stating the FREE allowance itself resets monthly
    # and does not roll over.
    assert "နောက်လ ဆက်မသွားပါ" in sheet


def test_sheet_has_one_free_trio_summary_line():
    sheet = gai.build_fact_sheet()
    assert "အခမဲ့ အကျဉ်းချုပ်" in sheet
    assert "လ စတိုင်း ပြန်ပြည့်" in sheet
    assert "ပထမဆုံး တစ်ကြိမ်သာ ၃ ခု" in sheet
    assert "နေ့တိုင်း ပြန်ပြည့်" in sheet


def test_private_ai_prompt_carries_the_expiry_block():
    # One shared builder: the private-AI prompt embeds the same sheet.
    prompt = pai.build_private_prompt(
        author_name="Member", text="ဝယ်ထားတဲ့ မိနစ် သက်တမ်းကုန်လား"
    )
    assert "သက်တမ်း ၃၀ ရက် (30 Days)" in prompt


def test_decision_prompt_has_followup_rule():
    prompt = gai.build_prompt(
        author_name="Member",
        text="အဲဒီလို ၁၀ မိနစ်စာ ဝယ်ရင် တစ်လလုံး သုံးလို့ရလား",
        bot_mentioned=False,
        is_reply_to_bot=False,
    )
    assert "နောက်ဆက် မေးခွန်းတွေ (Batch GA)" in prompt
    assert "NO_REPLY မထုတ်ပါနှင့်" in prompt


def test_decision_prompt_has_nudge_rule():
    prompt = gai.build_prompt(
        author_name="Member",
        text="အထက်မှာ ကျနော်မေးတာ ဖြေမထားသေးဘူးနော်",
        bot_mentioned=False,
        is_reply_to_bot=False,
    )
    assert "နောက်ဆုံး မဖြေရသေးတဲ့ မေးခွန်းကို ရှာပြီး" in prompt
    assert "အတိအကျ ပြန်ပို့တာ လုံးဝ မလုပ်ပါနှင့်" in prompt


def test_context_buffer_keeps_sixteen():
    assert gai.CONTEXT_BUFFER_MAX == 16
    buf = gai.GroupContextBuffer()
    for i in range(20):
        buf.push(-1001, "Member", f"မေးခွန်း {i}")
    snap = buf.snapshot(-1001)
    assert len(snap) == 16
    assert snap[0].endswith("မေးခွန်း 4")
    assert snap[-1].endswith("မေးခွန်း 19")

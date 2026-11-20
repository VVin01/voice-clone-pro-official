"""Batch DM pins (2026-11-10): troubleshooting + delivery facts in
the AI sheet, and the stale tab count fixed.

Group AI and Private AI share ONE knowledge sheet —
``group_ai_admin.build_fact_sheet()`` — which both prompt builders
embed verbatim. Ulfa asked for the other needed facts to be taught
too ("တခြားလိုမှာတွေပါသင်ပေးထား"): five troubleshooting / delivery
facts that kept reaching admins as escalations, although each is a
settled product behaviour (fresh /start remedy for a dead submit
button — SAKSA KOGAMI incident 2026-11-10; no charge when a job
cannot start + automatic refund on failure; no-audio videos refused
before charging — Batch DF; one recap job at a time; finished files
kept 24 hours only + the 50 MB Telegram send cap). The sheet's tab
line was also stale — it still said 4 tabs although the live nav
gained a TTS tab (and the sheet itself referenced "TTS Tab" later).

These tests pin the five lines' presence (verbatim, exactly once,
as their own sheet lines), the corrected tabs line, and that BOTH
prompt paths (group + private) carry the new facts because they
share the one builder.
"""

from app.services import group_ai_admin as gai
from app.services import private_ai as pai

NEW_FAQ_LINES = (
    "• Recap/Voice စလို့ မရဘူး၊ ခလုတ် နှိပ်လည်း မစဘူး ဆိုရင်: Mini App ကို ပိတ်ပြီး Bot မှာ /start အသစ် ပြန်နှိပ်၊ အဲဒီ မက်ဆေ့ချ်ကနေ Mini App ပြန်ဖွင့်ပြီး ပြန်စမ်းပါ — အဟောင်း App မျက်နှာပြင်ကနေ ဆက်နှိပ်နေရင် မစတတ်ပါ။",
    "• အလုပ် မစနိုင်ရင် Credit လုံးဝ မဖြတ်ပါ။ စပြီးသား အလုပ် ပျက်သွားရင်လည်း ဖြတ်ထားတဲ့ Credit အလိုအလျောက် ပြန်ဝင်ပါတယ်။",
    "• အသံ မပါတဲ့ ဗီဒီယိုနဲ့ Recap လုပ်လို့ မရပါ — အသံ မပါရင် Credit မဖြတ်ခင် ကတည်းက ငြင်းပြီး အသိပေးပါတယ်။ အသံ ပါတဲ့ ဗီဒီယို သုံးပါ။",
    "• Recap အလုပ် တစ်ခုကို တစ်ခါတည်း တစ်ခုသာ လုပ်လို့ ရပါတယ် — လုပ်နေတုန်း နောက်တစ်ခု ထပ်စလို့ မရပါ၊ ပြီးအောင် စောင့်ပါ။",
    "• ပြီးသွားတဲ့ ဖိုင်တွေကို ၂၄ နာရီပဲ သိမ်းထားပေးပါတယ် — အချိန်မီ Download ဆွဲထားပါ။ Local Bot API (2,000 MB / 2 GB) ဖွင့်ထားပြီဖြစ်လို့ ဖိုင်အရွယ်အစား 2,000 MB (2 GB) အထိ Telegram ထဲ တိုက်ရိုက် ပို့ပေးနိုင်ပါတယ် — 2,000 MB ထက် ကြီးမှသာ Mini App ထဲကနေ Download ဆွဲရပါမယ်။",
)

FRAGMENTS = (
    "/start အသစ်",
    "Credit လုံးဝ မဖြတ်ပါ",
    "အသံ မပါတဲ့ ဗီဒီယို",
    "၂၄ နာရီပဲ သိမ်းထား",
    "2,000 MB",
)

NEW_TABS_LINE = (
    "• Mini App ထဲမှာ Tab ၅ ခု ရှိပါတယ် — ပင်မ (Home), Voice Studio, TTS, Recap, VIP။"
)


def test_sheet_contains_troubleshooting_fragments():
    sheet = gai.build_fact_sheet()
    for fragment in FRAGMENTS:
        assert fragment in sheet


def test_sheet_carries_each_new_faq_line_verbatim_once():
    sheet = gai.build_fact_sheet()
    sheet_lines = sheet.splitlines()
    for line in NEW_FAQ_LINES:
        assert sheet.count(line) == 1
        assert line in sheet_lines


def test_tabs_line_states_five_tabs_including_tts():
    sheet = gai.build_fact_sheet()
    assert NEW_TABS_LINE in sheet.splitlines()
    assert "Tab ၅ ခု" in NEW_TABS_LINE
    assert "TTS" in NEW_TABS_LINE


def test_old_four_tab_line_is_gone():
    sheet = gai.build_fact_sheet()
    assert "Tab ၄ ခု" not in sheet


def test_group_ai_prompt_carries_the_new_facts():
    prompt = gai.build_prompt(
        author_name="Member",
        text="Recap စလို့ မရဘူး၊ ခလုတ် နှိပ်လည်း မစဘူး",
        bot_mentioned=True,
        is_reply_to_bot=False,
    )
    for fragment in FRAGMENTS:
        assert fragment in prompt


def test_private_ai_prompt_carries_the_new_facts():
    prompt = pai.build_private_prompt(
        text="အလုပ် ပျက်သွားရင် Credit ပြန်ရလား",
        author_name="Aung",
    )
    for fragment in FRAGMENTS:
        assert fragment in prompt

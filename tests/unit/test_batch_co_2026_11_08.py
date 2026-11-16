"""Batch CO (2026-11-08): too much prose above the TTS "view details"
toggle — move it inside, and make the inside worth opening.

Owner feedback on the real phone (2026-11-08 ~01:47, corrected 01:53):
the complaint was NOT that the expanded detail is thin — it is that the
always-visible stack above the toggle carries too much text. So CO:
1. relocates #tts-taste-note (the Gemini taste remaining/spent prose,
   painted by paintTtsTasteNote) INSIDE #tts-cost-hint-more, so it only
   shows when the user expands "အသေးစိတ် ကြည့်ရန်". Its own hidden-class
   logic (FREE-only, own-key suppression, Edge-mode hiding) is untouched;
   the Item-3b generate-time prompt still guards a spent taste at
   generate time.
2. enriches `tts_cost_hint_more` in BOTH locales with the full accounting
   picture — own-balance-only charging, daily caps (FREE 15 / VIP 60),
   referral seconds spent first, per-generation character limits
   (1,000 / 5,000), the Gemini free taste (2 min/day) and what an own key
   changes.
The live balance line and the collapsed short hint stay visible, as CN
made them. Cache token r110 -> r111 so clients pick up the new strings.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")


def _values(key):
    return re.findall(rf'{re.escape(key)}:\s*"([^"]*)"', I18N_JS)


def test_co_more_text_carries_the_full_picture_in_both_locales():
    my, en = _values("tts_cost_hint_more")
    # Daily caps.
    assert "15 မိနစ်" in my and "60 မိနစ်" in my
    assert "15 free minutes" in en and "60 for VIP" in en
    # Referral seconds spent first.
    assert "Referral ဆု" in my and "အရင် သုံး" in my
    assert "referral reward seconds are spent first" in en
    # Per-generation character limits.
    assert "1,000" in my and "5,000" in my
    assert "1,000 characters" in en and "5,000 for VIP" in en
    # Gemini taste + own-key effect.
    assert "2 မိနစ်" in my and "ကိုယ်ပိုင် Key" in my
    assert "2-minute free taste" in en and "your own key" in en
    # The never-touch guarantee survives in both.
    assert "Voice Clone မိနစ် နဲ့ Recap Credit လုံးဝ မထိပါ" in my
    assert "never touched" in en


def test_co_short_line_unchanged_from_cn():
    assert _values("tts_cost_hint") == [
        "💡 မိတ်ဆက်ဆု အချိန်ကို အရင် သုံးပါတယ်",
        "💡 Referral time is spent first",
    ]


def test_co_inline_default_matches_my_and_token_bumped():
    my = _values("tts_cost_hint_more")[0]
    assert f'data-i18n="tts_cost_hint_more">{my}</p>' in HTML
    assert ("20261104_" + "r110") not in HTML
    assert HTML.count("20261116_r157") == 4


def test_co_taste_note_lives_inside_the_expander():
    # Exactly one taste note, nested inside #tts-cost-hint-more (before
    # the cost paragraph), and no longer a visible-stack sibling above
    # the toggle.
    assert HTML.count('id="tts-taste-note"') == 1
    more = HTML.split('id="tts-cost-hint-more"', 1)[1].split("</div>", 1)[0]
    assert 'id="tts-taste-note"' in more
    assert more.index('id="tts-taste-note"') < more.index("tts_cost_hint_more")
    visible_stack = HTML.split('id="tts-cost-hint-more"', 1)[0]
    toggle_at = visible_stack.index('id="tts-cost-hint-toggle"')
    assert 'id="tts-taste-note"' not in visible_stack[:toggle_at]

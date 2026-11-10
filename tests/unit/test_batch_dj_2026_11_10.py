"""Batch DJ (2026-11-10): recalibrate the recap SRT cue budget to
TRUE rendered capacity.

Batch DI's live matrix (Milo, Lines=1, speech30) showed the burned
on-screen cues landing at roughly HALF the SRT cues: the DD
budget's font-fraction ratio overstated the real Padauk capacity
at the 994 px burn bound by ~1.2–1.35×, so the width-true burn
fit (_cx_split_line, unchanged) re-split most cues. DJ replaces
the vertical budget numbers with MEASURED per-line capacities
(real renderer, real narration corpus; budget = lines × capacity
at ≈95% of the median measured value). Simulated end-to-end
unsplit rate: ~14–28% with the DD budgets → ~97–99% with DJ's.
The horizontal (16:9) budget path is untouched — it never
re-split. _cx_split_line, the DI greedy fill and all timing
logic are unchanged.
"""
from __future__ import annotations

import pytest

import app.services.recap_engine.core as core
import app.services.recap_engine.png_subtitles as pq

CW, CH = 1080, 1920
LB = core.frame_layout("9:16", "blur", 1080, 1920)
BOUND = int(0.92 * 1080)  # 994 — the production fit bound at sub_w 0.92

# Representative narration segments: real planner output sentences
# (the DI eyes-on jobs' SRT text), real spoken-script sentences,
# and the CX fixture — the same corpus the capacity ladders were
# measured on.
_S = [
    "မထင်မှတ်ထားတဲ့ အန္တရာယ်ဆိုးကြီး ကြုံတွေ့လိုက်ရချိန်မှာတော့ သူတို့ရဲ့ ငြိမ်းချမ်းလှတဲ့ ဘဝလေးဟာ ရုတ်တရက်ဆိုသလို ပြောင်းလဲသွားခဲ့ရပါတယ်။",
    "ဒါပေမဲ့ သူတို့အားလုံး ဘေးကင်းအောင် လွတ်မြောက်နိုင်ပါ့မလား။",
    "သာမန်အတိုင်း သွားလာလှုပ်ရှားနေကြတဲ့ လူတွေကြားထဲမှာ မမျှော်လင့်ထားတဲ့ အဖြစ်အပျက်တစ်ခု ရုတ်တရက် စတင်ဖြစ်ပွားလာခဲ့ပြီး ပတ်ဝန်းကျင်တစ်ခုလုံးလည်း ချက်ချင်းဆိုသလို ရုန်းရင်းဆန်းခတ် ဖြစ်သွားခဲ့တာပါ။",
    "ဘာတွေဖြစ်ပျက်နေမှန်း မသိရသေးခင်မှာပဲ လူတိုင်း စိုးရိမ်ထိတ်လန့်စွာနဲ့ လွတ်မြောက်ရာကို ထွက်ပြေးနေကြရပြီး အဲဒီနေရာမှာ ဘယ်လို ဆိုးရွားတဲ့ အခြေအနေမျိုးနဲ့ ရင်ဆိုင်ရတော့မလဲဆိုတာ စောင့်ကြည့်ရမှာ ဖြစ်ပါတယ်။",
    "ဒါကို ရုတ်တရက် ကြုံတွေ့လိုက်ရတဲ့အခါ ဘေးကင်းရာကို ထွက်ပြေးဖို့ ပြင်ဆင်ကြပေမဲ့ ရန်သူတွေက ပတ်ဝန်းကျင်တစ်ခုလုံးကို ဝိုင်းဝန်းပိတ်ဆို့ထားပြီး ဖြစ်နေတာပါ။",
    "အခြေအနေတွေ ဆိုးရွားလာတာကြောင့် ဘာလုပ်လို့ လုပ်ရမှန်း မသိဘဲ အတော်လေး စိုးရိမ်ထိတ်လန့်သွားကြပါတယ်။",
    "အဲဒီအချိန်မှာပဲ အန္တရာယ်ဆိုးက ပိုပြီး နီးကပ်လာခဲ့တာကြောင့် မိသားစုဝင်တွေကို လုံခြုံတဲ့နေရာမှာ ခဏ ပုန်းခိုခိုင်းထားပြီး သူကိုယ်တိုင် ရန်သူတွေကို သွားရောက်ရင်ဆိုင်ဖို့ ဆုံးဖြတ်လိုက်ပါတယ်။",
    "အခြေအနေတွေ ဆိုးရွားမလာခင် မိသားစုကို ဘေးကင်းအောင် ကာကွယ်ဖို့ အစွမ်းကုန် ကြိုးစားရပါတော့တယ်။",
    "အန္တရာယ်ဆိုးကြီးနဲ့ ရုတ်တရက် ရင်ဆိုင်လိုက်ရတဲ့ ကိစ္စတစ်ခုနဲ့အတူ ဇာတ်လမ်းက စတင်ခဲ့ပါတယ်။",
    "မိသားစုကို ကာကွယ်ဖို့ ကြိုးစားနေချိန်မှာပဲ မထင်မှတ်ထားတဲ့ အန္တရာယ်ကြီးနဲ့ စတင်ရင်ဆိုင်လိုက်ရပါတယ်။",
    "ဒီဗီဒီယိုမှာ Voice Clone Pro Bot ရဲ့ Voice Clone၊ Movie Recap နဲ့ TTS သုံးနည်းတွေကို အဆင့်ဆင့် ပြပေးသွားမှာပါ။",
    "Recap ရဲ့ အသေးစိတ် settings ပြင်နည်းတွေ၊ သိထားသင့်တဲ့ ကန့်သတ်ချက်တွေနဲ့ ဆောင်ရန်၊ ရှောင်ရန် အချက်တွေလည်း ပါတာမို့ အဆုံးထိ ကြည့်ပေးပါနော်။",
    "အသင့်သုံးနိုင်တဲ့ persona တစ်ဆယ့်နှစ် မျိုး ရှိပြီး နမူနာ အရင်နားထောင်ပြီး ကြိုက်တဲ့ အသံကို ရွေးနိုင်ပါတယ်။",
    "အသံရွေးပြီးရင် ပြောစေချင်တဲ့ စာသားကို ရိုက်ထည့်ပြီး အမြန်နှုန်း ချိန်လိုက်ရုံပါပဲ။",
    "Persona အသံ တစ်ဆယ့်နှစ် မျိုး ရှိပြီး ဝယ်ထားသူများကတော့ ကိုယ်ပိုင်နမူနာ တင်ပြီး ကိုယ့်အသံနဲ့ပါ ပြောခိုင်းလို့ ရပါတယ်။",
]
_FIXTURE = (
    "မြို့ကြီးဆီကို ထွက်ခွာပြီး သင်္ကြန်ပွဲတော်မှာ ကျောင်းသားတွေ "
    "လွှမ်းမိုးနေတဲ့ ပြဿနာကို ဆုံးဖြတ်ခဲ့ကြပါတယ်"
)
SEGMENTS = [
    _S[0] + " " + _S[1],
    _S[2],
    _S[3],
    _S[4] + " " + _S[5],
    _S[6],
    _S[7] + " " + _S[8] + " " + _S[9],
    _S[10] + " " + _S[11],
    _FIXTURE + " " + _FIXTURE,
    _S[12] + " " + _S[13] + " " + _S[14],
]


def _burn(renderer, text, font_px, visuals, a, b, max_lines=1):
    font = core.overlays_font_path({})
    return core.fit_png_cue_pieces(
        renderer, text, font_px, font, visuals, BOUND, a, b,
        max_lines=max_lines,
    )


# ── 1. The budget IS lines × measured per-line capacity ─────────

def test_dj_budget_anchors_vertical_one_line():
    # Old (DD ratio) → new (DJ measured): 13pt 56→33, 16pt 45→25,
    # 19pt 38→20, 22pt 32→17.
    assert core._subtitle_max_chars(True, 1, 0.92, 13) == 33
    assert core._subtitle_max_chars(True, 1, 0.92, 16) == 25
    assert core._subtitle_max_chars(True, 1, 0.92, 19) == 20
    assert core._subtitle_max_chars(True, 1, 0.92, 22) == 17


def test_dj_budget_is_lines_times_capacity():
    for pt in range(10, 27):
        one = core._SUBTITLE_VERTICAL_LINE_CAPACITY[pt]
        multi = core._SUBTITLE_VERTICAL_LINE_CAPACITY_MULTILINE[pt]
        assert core._subtitle_max_chars(True, 1, 0.92, pt) == one
        assert core._subtitle_max_chars(True, 2, 0.92, pt) == 2 * multi
        assert core._subtitle_max_chars(True, 3, 0.92, pt) == 3 * multi


@pytest.mark.parametrize("lines", [1, 2, 3])
def test_dj_budget_strictly_decreasing_in_pt(lines):
    ladder = [core._subtitle_max_chars(True, lines, 0.92, pt) for pt in range(10, 27)]
    assert all(a > b for a, b in zip(ladder, ladder[1:])), (lines, ladder)


def test_dj_budget_size_clamped_to_measured_range():
    # Outside the 10–26 slider the budget clamps to the measured
    # end values instead of extrapolating past true capacity.
    assert core._subtitle_max_chars(True, 1, 0.92, 8) == core._subtitle_max_chars(True, 1, 0.92, 10)
    assert core._subtitle_max_chars(True, 1, 0.92, 30) == core._subtitle_max_chars(True, 1, 0.92, 26)
    assert core._subtitle_max_chars(True, 1, 0.92, "bogus") == core._subtitle_max_chars(True, 1, 0.92, 16)


def test_dj_horizontal_budget_unchanged():
    # The 16:9 path keeps the legacy table + fraction-ratio
    # scaling byte-identically (it never re-split).
    assert core._subtitle_max_chars(False, 1) == 52
    assert core._subtitle_max_chars(False, 2) == 96
    assert core._subtitle_max_chars(False, 3) == 144
    assert core._subtitle_max_chars(False, 1, font_size=13) == 65
    assert core._subtitle_max_chars(False, 2, font_size=22) == round(96 * 0.040 / 0.056)


# ── 2. Render-level regression: SRT cues survive the burn ───────

@pytest.mark.parametrize("size_pt", [13, 22])
def test_dj_srt_cues_survive_burn_unsplit(tmp_path, size_pt):
    renderer = pq.PngSubtitleRenderer(tmp_path / "dj_cache")
    font_px = core.subtitle_font_px(CW, CH, size_pt, 1, LB)
    visuals = core.build_subtitle_visuals(
        core.SUBTITLE_STYLE_PRESETS["yellow"], None, CH, font_px
    )
    font = core.overlays_font_path({})
    budget = core._subtitle_max_chars(True, 1, 0.92, size_pt)
    total = unsplit = 0
    for seg in SEGMENTS:
        dur = max(8.0, len(seg) / 3.5)
        cues = core.split_burmese_subtitle_cues(
            0.0, dur, seg, max_cue_chars=budget, lines_per_cue=1
        )
        assert cues
        for (a, b, txt) in cues:
            pieces = _burn(renderer, txt, font_px, visuals, a, b)
            assert pieces
            total += 1
            if len(pieces) == 1:
                unsplit += 1
            for (plines, ppx, pvis, _s, _e) in pieces:
                for ln in plines:
                    w = core._png_cue_width(renderer, ln, ppx, font, pvis)
                    if w > BOUND:
                        # Only a single unbreakable token may ship
                        # over the bound (the CX floor rule).
                        toks = [t for t in core.tokenize_burmese_words(ln) if t.strip()]
                        assert len(toks) <= 1, (size_pt, ln, w)
    assert total >= 30
    rate = unsplit / total
    assert rate >= 0.85, f"{size_pt}pt unsplit rate {rate:.3f} ({unsplit}/{total})"

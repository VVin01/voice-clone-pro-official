"""Batch DA (2026-11-09): recap subtitle splitter — never break
a word (CX follow-up).

Owner decision: Ulfa, 2026-11-09 12:02 — "စာလုံးမကွဲအောင် ပြင်ပါ"
(fix it so words don't break).

Batch CX (live) splits overflowing cues into sequential one-line
pieces at the locked size, but its greedy fallback cut at the
overflow point when no meaningful boundary qualified in-span —
and the live eyes-on render (job rc_1794200454_f2d514,
~/workspace/tg-shoot/cx-cz-eyes/) caught it slicing words in two:

- cue 3:  "…အသံတု ပြု" | "လုပ်တဲ့…"   — breaks ပြုလုပ်
- cue 10: "…ဝါကျအထိ တိ" | "ကျတဲ့"     — breaks တိကျ

DA rule (in _cx_split_line): the overflow cut is a LAST resort.
When it would land inside a space-delimited word, the cut
backtracks to the most recent space boundary inside the fitting
span so the whole word moves to the next piece — UNLESS the
backtracked piece would fall below the same ≥40%-of-fitting-width
substantial floor CX uses for meaningful cuts (a stub flashes
worse than a broken word), in which case the overflow cut stands.
No dictionary segmenter: "word boundary" = a space, or the
existing meaningful boundaries. Cues that fit stay byte-identical;
the single-unbreakable-token shrink path is untouched.
"""
from pathlib import Path

import pytest

import app.services.recap_engine.core as core
import app.services.recap_engine.png_subtitles as pq

ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")

CW, CH = 1080, 1920
LB = core.frame_layout("9:16", "blur", 1080, 1920)
BOUND = int(0.92 * 1080)  # default sub_w on a 1080-wide picture

# The REAL 15 cues from the live CX eyes-on job rc_1794200454_f2d514
# (~/workspace/tg-shoot/cx-cz-eyes/VoiceClonePro_Recap_*.srt). The
# live job ran at 19pt — it is the size whose pre-DA split pattern
# matches the live render piece for piece (cues 3 and 10 mid-word).
REAL_CUES = [
    "ကိုယ်ပိုင် အသံနဲ့ ထပ်တူကျတဲ့ အသံတုကို",
    "ဖန်တီးလိုက်တဲ့အခါ ဘာတွေ ဖြစ်လာမလဲ။",
    "အခု ပထမဆုံး အသံတု ပြုလုပ်တဲ့",
    "နည်းပညာအကြောင်းကို မိတ်ဆက် ပြသပေးလိုက်တာပါ။",
    "ဒီဗီဒီယိုထဲမှာတော့ အသံတု",
    "ပြုလုပ်ခြင်းအကြောင်းကို ပြောပြထားပြီး စာသားမှ",
    "အသံပြောင်း စနစ်က ဝါကျတွေကို ဘယ်လို အစဉ်လိုက်",
    "စီစဉ်ဆောင်ရွက်သလဲဆိုတာ ပြသထားတာ ဖြစ်ပါတယ်။",
    "ဝါကျ တစ်ခုချင်းစီကို ပြန်လည်",
    "စိစစ်တဲ့အခါမှာလည်း နောက်ဆုံး ဝါကျအထိ တိကျတဲ့",
    "အဖြေ ရရှိအောင် သေချာ ချိတ်ဆက်",
    "ဆောင်ရွက်ပေးထားပါတယ်။",
    "အဲဒီနောက်မှာတော့ စနစ်ကနေ နမူနာ စကားလုံး",
    "အသံထွက်တွေကို အလိုအလျောက် သရုပ်ပြ",
    "ရွတ်ဆိုသွားတာကို မြင်တွေ့ရမှာပါ။",
]

# Crafted stub-floor cue: a one-syllable first word, then ONE long
# space-free word. The fitting span's only space boundary sits
# right after "သူ" — backtracking there would leave a stub far
# below the 40% floor, so DA must keep the overflow cut inside
# the long word (rule 3).
STUB_CUE = "သူ စာအုပ်စာရွက်မှတ်တမ်းစာရင်းရှည်လျားလှစွာသောအရာဝတ္ထုပစ္စည်းများ"


def _renderer(tmp_path):
    return pq.PngSubtitleRenderer(tmp_path / "da_cache")


def _setup(size_pt):
    font_px = core.subtitle_font_px(CW, CH, size_pt, 1, LB)
    font = core.subtitle_font_file("padauk")
    visuals = pq.preset_visuals(
        core.SUBTITLE_STYLE_PRESETS["yellow"], CH, font_px
    )
    return font_px, font, visuals


def _pieces(renderer, text, font_px, font, visuals, start=0.0, end=2.0):
    out = core.fit_png_cue_pieces(
        renderer, text, font_px, font, visuals, BOUND, start, end
    )
    return [ln[0] for ln, *_ in out], out


def _mid_word_boundaries(cue, texts):
    """Walk the source cue against its pieces; return the internal
    boundaries that fall INSIDE a space-delimited word — i.e. the
    next source character is not a space AND the left piece does
    not end at a meaningful break (particle / clause punctuation).
    Pieces are exact token joins of the source, so the walk must
    stay in sync character for character."""
    bad = []
    pos = 0
    for i, piece in enumerate(texts):
        assert cue.startswith(piece, pos), (
            f"piece desync: {piece!r} not at {pos} in {cue!r}"
        )
        pos += len(piece)
        if i == len(texts) - 1:
            assert cue[pos:].strip() == ""
            break
        at_space = pos < len(cue) and cue[pos] == " "
        toks = [t for t in core.tokenize_burmese_words(piece) if t != ""]
        meaningful = bool(toks) and core._cx_is_break_token(toks[-1])
        if not at_space and not meaningful:
            bad.append(piece)
        while pos < len(cue) and cue[pos] == " ":
            pos += 1
    return bad


# ── Release pin: cache token ─────────────────────────────────────

def test_da_cache_token_is_r129():
    assert INDEX_HTML.count("20261104_r129") == 4
    assert "20261104_r" + "122" not in INDEX_HTML


# ── 1. The two live defects, fixed at the live geometry (19pt) ──

def test_da_cue3_pyuloke_stays_whole(tmp_path):
    r = _renderer(tmp_path)
    font_px, font, visuals = _setup(19)
    texts, _ = _pieces(r, REAL_CUES[2], font_px, font, visuals)
    assert texts == ["အခု ပထမဆုံး အသံတု", "ပြုလုပ်တဲ့"]
    assert any("ပြုလုပ်" in t for t in texts)


def test_da_cue10_teikya_stays_whole(tmp_path):
    r = _renderer(tmp_path)
    font_px, font, visuals = _setup(19)
    texts, _ = _pieces(r, REAL_CUES[9], font_px, font, visuals)
    assert texts == [
        "စိစစ်တဲ့အခါမှာလည်း",
        "နောက်ဆုံး ဝါကျအထိ",
        "တိကျတဲ့",
    ]
    assert any("တိကျ" in t for t in texts)


def test_da_cue11_cheikset_stays_whole_at_16pt(tmp_path):
    # The same overflow defect at 16pt: pre-DA this cue split
    # "ချိတ်" | "ဆက်"; DA backtracks to the space after "သေချာ".
    r = _renderer(tmp_path)
    font_px, font, visuals = _setup(16)
    texts, _ = _pieces(r, REAL_CUES[10], font_px, font, visuals)
    assert texts == ["အဖြေ ရရှိအောင် သေချာ", "ချိတ်ဆက်"]


# ── 2. All 15 real cues: no mid-word boundary where a qualifying
#       in-span boundary exists ───────────────────────────────────

@pytest.mark.parametrize("size_pt", [13, 16])
def test_da_real_cues_never_break_a_word(tmp_path, size_pt):
    r = _renderer(tmp_path)
    font_px, font, visuals = _setup(size_pt)
    for cue in REAL_CUES:
        texts, raw = _pieces(r, cue, font_px, font, visuals)
        assert _mid_word_boundaries(cue, texts) == [], (
            f"mid-word split at {size_pt}pt in {cue!r}: {texts!r}"
        )
        for lines, px, vis, s, e in raw:
            assert px == font_px and len(lines) == 1 and e > s
            assert core._png_cue_fits(r, lines[0], px, font, vis, BOUND)


def test_da_real_cues_19pt_only_the_known_stub_case_remains(tmp_path):
    # At the live 19pt geometry every real cue is clean EXCEPT
    # cue 4's second piece: its fitting span contains exactly one
    # space boundary (after "မိတ်ဆက်", measured at ~34% of the
    # span's width — below the 40% stub floor), so rule 3 keeps
    # the overflow cut there. Pinning the exception by name keeps
    # it a deliberate, visible choice rather than a silent drift.
    r = _renderer(tmp_path)
    font_px, font, visuals = _setup(19)
    offenders = []
    for n, cue in enumerate(REAL_CUES, 1):
        texts, _ = _pieces(r, cue, font_px, font, visuals)
        for piece in _mid_word_boundaries(cue, texts):
            offenders.append((n, piece))
    assert offenders == [(4, "မိတ်ဆက် ပြသပေးလိုက်")]


# ── 3. Stub floor: no backtrack into a sliver piece ─────────────

def test_da_stub_floor_keeps_the_overflow_cut(tmp_path):
    r = _renderer(tmp_path)
    font_px, font, visuals = _setup(19)
    texts, raw = _pieces(r, STUB_CUE, font_px, font, visuals)
    assert texts == [
        "သူ စာအုပ်စာရွက်မှတ်တမ်း",
        "စာရင်းရှည်လျားလှစွာသော",
        "အရာဝတ္ထုပစ္စည်းများ",
    ]
    # The first piece swallowed the short first word AND part of
    # the long word: the cut stayed at the overflow point (inside
    # the long word) instead of backtracking to the stub "သူ".
    assert texts[0] != "သူ" and texts[0].startswith("သူ ")
    for lines, px, vis, s, e in raw:
        assert px == font_px
        assert core._png_cue_fits(r, lines[0], px, font, vis, BOUND)


# ── 4. CX invariants DA must not disturb ─────────────────────────

def test_da_fitting_cue_is_byte_identical(tmp_path):
    # Cue 5 fits whole at 19pt: the fast path is untouched.
    r = _renderer(tmp_path)
    font_px, font, visuals = _setup(19)
    lines, px, vis = core.fit_png_cue_lines(
        r, REAL_CUES[4], 1, font_px, font, visuals, BOUND
    )
    assert px == font_px and vis is visuals
    legacy = r.render_text(REAL_CUES[4], 1, font_px, font, visuals)
    fitted = r.render_lines(lines, px, font, vis)
    assert fitted[0] == legacy[0] and fitted[1] == legacy[1]


def test_da_split_windows_still_partition_and_preserve_text(tmp_path):
    r = _renderer(tmp_path)
    font_px, font, visuals = _setup(19)
    for cue in REAL_CUES:
        _texts, raw = _pieces(r, cue, font_px, font, visuals,
                                start=10.0, end=12.0)
        assert raw[0][3] == pytest.approx(10.0)
        assert raw[-1][4] == pytest.approx(12.0)
        for (_, _, _, _s, e), (_, _, _, ns, _e) in zip(raw, raw[1:]):
            assert ns == pytest.approx(e)
        joined = "".join(ln[0] for ln, *_ in raw).replace(" ", "")
        assert joined == cue.replace(" ", "")

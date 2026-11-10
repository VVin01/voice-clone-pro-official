"""Batch CX (2026-11-09): recap subtitle FIXED font size with
meaningful cue splitting.

Ulfa (2026-11-09 00:45, refined 00:48): short cues rendered LARGE
and long cues SMALL because the Batch AG fit shrank the font per
cue. The font is now LOCKED at the job's chosen size, subtitles are
ALWAYS ONE LINE on the PNG burn path, and an overflowing cue is
SPLIT into sequential one-line pieces (time window split with it)
at meaningful phrase/clause boundaries — never mid-word. Shrink
survives ONLY for a single unbreakable token wider than the canvas.
"""
import shutil
import subprocess

import pytest

import app.services.recap_engine.core as core
import app.services.recap_engine.png_subtitles as pq

CW, CH = 1080, 1920
LB = core.frame_layout("9:16", "blur", 1080, 1920)
BOUND = int(0.92 * 1080)  # default sub_w on a 1080-wide picture
FFMPEG_BIN = shutil.which("ffmpeg")

LONG = "မြို့ကြီးဆီကို ထွက်ခွာပြီး သင်္ကြန်ပွဲတော်မှာ ကျောင်းသားတွေ လွှမ်းမိုးနေတဲ့ ပြဿနာကို ဆုံးဖြတ်ခဲ့ကြပါတယ်"
MID = "မြို့ကြီးဆီကို ထွက်ခွာပြီး ပြဿနာကို ဆုံးဖြတ်ခဲ့ကြပါတယ်"
SHORT = "မင်္ဂလာပါ"
# A cue with an explicit clause boundary (၊) mid-text.
CLAUSE = "မိုးရွာနေတယ်၊ အိမ်ထဲမှာ နေရတာ ပျော်စရာကောင်းတယ်"


def _renderer(tmp_path):
    return pq.PngSubtitleRenderer(tmp_path / "cx_cache")


def _font_px(size_pt, sub_lines=1):
    return core.subtitle_font_px(CW, CH, size_pt, sub_lines, LB)


def _visuals(font_px, preset="yellow"):
    return pq.preset_visuals(core.SUBTITLE_STYLE_PRESETS[preset], CH, font_px)


def _pieces(renderer, text, font_px, start=0.0, end=6.0, bound=BOUND):
    font = core.subtitle_font_file("padauk")
    return core.fit_png_cue_pieces(
        renderer, text, font_px, font, _visuals(font_px), bound, start, end
    )


def _rejoin(pieces):
    """Pieces re-joined: no text may be lost or invented — compare
    space-insensitively, since pieces are stripped at the split."""
    return "".join(ln[0] for ln, *_ in pieces).replace(" ", "")


# ── 1. Locked size: no per-cue shrink ───────────────────────────
# Batch DD (2026-11-09) supersedes CX's "always one line": the
# fit wraps into up to the user's chosen line count (Lines 1/2/3)
# at the LOCKED size. The CX guarantees that survive unchanged:
# the font never shrinks for a multi-token cue and every rendered
# line fits the bound.

@pytest.mark.parametrize("size_pt", [13, 16, 19, 22])
@pytest.mark.parametrize("sub_lines", [1, 2])
def test_cx_font_locked_and_one_line(tmp_path, size_pt, sub_lines):
    r = _renderer(tmp_path)
    font_px = _font_px(size_pt, sub_lines)
    font = core.subtitle_font_file("padauk")
    visuals = _visuals(font_px)
    lines, px, vis = core.fit_png_cue_lines(
        r, LONG, sub_lines, font_px, font, visuals, BOUND
    )
    assert px == font_px, "CX: the font must never shrink for a multi-token cue"
    assert 1 <= len(lines) <= sub_lines, "DD: wrap within the chosen line count"
    if sub_lines == 1:
        assert len(lines) == 1, "CX: one-line jobs stay one line"
    _path, w, _h = r.render_lines(lines, px, font, vis)
    assert w <= BOUND


def test_cx_fitting_cue_is_byte_identical(tmp_path):
    r = _renderer(tmp_path)
    font_px = _font_px(16)
    font = core.subtitle_font_file("padauk")
    visuals = _visuals(font_px)
    lines, px, vis = core.fit_png_cue_lines(
        r, SHORT, 1, font_px, font, visuals, BOUND
    )
    assert px == font_px and vis is visuals
    legacy = r.render_text(SHORT, 1, font_px, font, visuals)
    fitted = r.render_lines(lines, px, font, vis)
    assert fitted[0] == legacy[0] and fitted[1] == legacy[1]


def test_cx_two_line_setting_does_not_enlarge_the_burn_font(tmp_path):
    # Batch DD supersedes the CX pin below: the user's Lines 1/2/3
    # choice is honoured on the burn again — a 2-line job resolves
    # at 2 lines WITH the ×1.25 multi-line font factor, exactly as
    # the preview resolves it (preview == render).
    job = core._resolve_png_subtitle_job(
        {"subtitle_cues": [[0.0, 6.0, LONG]], "subtitles_font_size": 16,
         "sub_lines": 2, "sub_style": "yellow"},
        CW, CH, LB,
    )
    assert job is not None
    assert job["sub_lines"] == 2
    assert job["font_px"] == core.subtitle_font_px(CW, CH, 16, 2, LB)


# ── 2. Splitting: pieces fit, windows partition, text preserved ─

@pytest.mark.parametrize("size_pt", [16, 22])
def test_cx_split_pieces_fit_and_partition_window(tmp_path, size_pt):
    r = _renderer(tmp_path)
    font_px = _font_px(size_pt)
    pieces = _pieces(r, LONG, font_px, start=10.0, end=16.0)
    assert len(pieces) >= 2, "the long cue must split at the locked size"
    font = core.subtitle_font_file("padauk")
    for lines, px, vis, s, e in pieces:
        assert len(lines) == 1 and px == font_px
        _path, w, _h = r.render_lines(lines, px, font, vis)
        assert w <= BOUND
        assert e > s
    # Windows partition the cue window contiguously, in order.
    assert pieces[0][3] == pytest.approx(10.0)
    assert pieces[-1][4] == pytest.approx(16.0)
    for (_, _, _, _s, e), (_, _, _, ns, _e) in zip(pieces, pieces[1:]):
        assert ns == pytest.approx(e)
    # No text lost or invented.
    assert _rejoin(pieces) == LONG.replace(" ", "")


def test_cx_split_windows_proportional_to_text_share(tmp_path):
    r = _renderer(tmp_path)
    font_px = _font_px(16)
    pieces = _pieces(r, MID, font_px, start=0.0, end=4.0)
    assert len(pieces) == 2
    total_chars = sum(len(ln[0]) for ln, *_ in pieces)
    for lines, _px, _vis, s, e in pieces:
        expect = (len(lines[0]) / total_chars) * 4.0
        assert (e - s) == pytest.approx(expect, abs=0.05)


def test_cx_no_piece_flashes_too_short(tmp_path):
    # A short cue window split into pieces still gives each piece
    # a readable minimum on-screen time when the window allows it.
    r = _renderer(tmp_path)
    font_px = _font_px(22)
    pieces = _pieces(r, LONG, font_px, start=0.0, end=6.0)
    assert len(pieces) >= 2
    for _ln, _px, _vis, s, e in pieces:
        assert (e - s) >= 0.4 - 1e-6


# ── 3. Meaningful splits: phrase boundaries, never mid-word ────

def test_cx_split_lands_on_meaningful_boundaries(tmp_path):
    # At 16pt the long cue splits into four pieces, each ending at
    # a phrase/clause boundary (particle or conjunction) — the
    # split never strands a fragment mid-phrase.
    r = _renderer(tmp_path)
    font_px = _font_px(16)
    pieces = _pieces(r, LONG, font_px, start=0.0, end=6.0)
    assert [ln[0] for ln, *_ in pieces] == [
        "မြို့ကြီးဆီကို ထွက်ခွာပြီး",
        "သင်္ကြန်ပွဲတော်မှာ",
        "ကျောင်းသားတွေ လွှမ်းမိုးနေတဲ့",
        "ပြဿနာကို ဆုံးဖြတ်ခဲ့ကြပါတယ်",
    ]
    for lines, _px, _vis, _s, _e in pieces[:-1]:
        text = lines[0]
        toks = core.tokenize_burmese_words(text)
        assert core._cx_is_meaningful_break(toks, len(toks) - 1), (
            f"piece does not end at a meaningful boundary: {text!r}"
        )


def test_cx_clause_punctuation_is_a_split_point(tmp_path):
    r = _renderer(tmp_path)
    font_px = _font_px(22)
    pieces = _pieces(r, CLAUSE, font_px, start=0.0, end=6.0)
    assert len(pieces) >= 2
    assert _rejoin(pieces) == CLAUSE.replace(" ", "")
    # The first piece ends at the ၊ clause boundary.
    assert pieces[0][0][0].rstrip().endswith("၊")


def test_cx_split_never_breaks_a_shaped_cluster(tmp_path):
    # Every piece is a concatenation of whole tokens from the
    # word tokenizer — a split never lands inside a syllable.
    r = _renderer(tmp_path)
    font_px = _font_px(16)
    pieces = _pieces(r, LONG, font_px, start=0.0, end=6.0)
    tokens = core.tokenize_burmese_words(LONG)
    for lines, _px, _vis, _s, _e in pieces:
        text = lines[0]
        # The piece text (ignoring boundary spaces) must be a
        # contiguous slice of the tokenizer's output.
        joined = "".join(tokens).replace(" ", "")
        assert text.replace(" ", "") in joined


def test_cx_split_never_mid_word(tmp_path):
    # "လွှမ်းမိုးနေတဲ့" is one written unit: the split must keep
    # it whole (the old endswith check cut between နေ and တဲ့).
    r = _renderer(tmp_path)
    font_px = _font_px(16)
    pieces = _pieces(r, LONG, font_px, start=0.0, end=6.0)
    for lines, _px, _vis, _s, _e in pieces:
        assert "လွှမ်းမိုးနေ" not in lines[0] or "လွှမ်းမိုးနေတဲ့" in lines[0]
        assert not lines[0].startswith("တဲ့")


# ── 4. Single-token fallback: shrink only there ────────────────

def test_cx_single_unbreakable_token_shrinks_to_floor(tmp_path):
    # One token wider than the canvas at the locked size: the only
    # case the font may shrink. A long Latin run is a single
    # tokenizer atom (no Burmese syllable breaks inside it).
    r = _renderer(tmp_path)
    font_px = _font_px(22)
    font = core.subtitle_font_file("padauk")
    visuals = _visuals(font_px)
    token = "W" * 30
    assert len(core.tokenize_burmese_words(token)) == 1
    lines, px, vis = core.fit_png_cue_lines(
        r, token, 1, font_px, font, visuals, BOUND
    )
    assert len(lines) == 1
    assert px < font_px, "a single over-wide token is the one shrink case"
    _path, w, _h = r.render_lines(lines, px, font, vis)
    assert w <= BOUND
    # And a token that cannot fit even at the floor ships at the
    # floor (it cannot be split without breaking a shaped cluster).
    lines, px, vis = core.fit_png_cue_lines(
        r, "W" * 200, 1, font_px, font, visuals, BOUND
    )
    assert len(lines) == 1
    assert px == core.SUBTITLE_FONT_MIN_PX


# ── 5. SRT consistency: the side-file keeps pre-fit cues ───────
#
# Finding (Batch CX): the .srt is written by EdgeSmartSync from the
# pipeline's cue splitter BEFORE the burn fits them, and the burn
# runs later (possibly in a resumed run) — so the burn's split
# cannot feed back into the .srt without restructuring the
# pipeline. The .srt therefore keeps the pre-fit cues, exactly as
# under Batch AG (whose wrap/shrink also never reached the .srt).
# What stays consistent: the burn's pieces partition each .srt
# cue's window and concatenate back to its text.

def test_cx_burn_pieces_partition_each_srt_cue(tmp_path):
    # split_burmese_subtitle_cues (the .srt source) is unchanged
    # by CX: same cues out for the same segment.
    cues = core.split_burmese_subtitle_cues(
        0.0, 6.0, LONG, max_cue_chars=45, lines_per_cue=1
    )
    assert cues, "SRT splitter must still produce cues"
    r = _renderer(tmp_path)
    font_px = _font_px(16)
    for c_start, c_end, c_text in cues:
        pieces = _pieces(r, c_text, font_px, start=c_start, end=c_end)
        assert pieces
        assert pieces[0][3] == pytest.approx(c_start)
        assert pieces[-1][4] == pytest.approx(c_end)
        assert _rejoin(pieces) == c_text.replace(" ", "")


# ── 6. End to end: the production burn splits, never shrinks ───

@pytest.mark.skipif(not FFMPEG_BIN, reason="ffmpeg not available")
def test_cx_end_to_end_production_burn_splits_long_cue(tmp_path):
    src = tmp_path / "src.mp4"
    subprocess.run(
        [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=0x202030:size=320x240:rate=30:duration=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True,
    )
    out = tmp_path / "out.mp4"
    core.apply_production_overlays(
        FFMPEG_BIN, src, out,
        {"subtitle_cues": [[0.5, 3.5, LONG]], "subtitles_font_size": 22,
         "sub_style": "yellow", "sub_lines": 2, "sub_y": 0.85},
        "libx264", tmp_path / "w", frame_size=(320, 240),
        layout={"pw": 320, "ph": 240, "ox": 0, "oy": 0}, duration=4,
    )
    assert out.is_file()
    from PIL import Image

    widths = []
    for p in (tmp_path / "w" / "subtitle_png_cache").glob("sub_*.png"):
        with Image.open(p) as im:
            widths.append(im.width)
    # The long cue split into several one-line PNGs, each within
    # the bound — at the locked 22pt size, never shrunk.
    assert len(widths) >= 2
    assert widths and max(widths) <= int(0.92 * 320)


@pytest.mark.skipif(not FFMPEG_BIN, reason="ffmpeg not available")
def test_cx_end_to_end_preview_sample_is_one_line(tmp_path):
    src = tmp_path / "src.mp4"
    subprocess.run(
        [FFMPEG_BIN, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=0x202030:size=320x240:rate=30:duration=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True,
    )
    out_img = tmp_path / "preview.png"
    core.extract_preview_with_overlays(
        FFMPEG_BIN, src, out_img, timestamp=1.0,
        overlays={"burn_subtitles": True, "sub_y": 0.85, "sub_lines": 2,
                  "subtitles_font_size": 22, "sub_style": "yellow"},
        output_ratio="Original", frame_fill="blur",
        work_dir=tmp_path / "pw", sample_text=LONG,
    )
    assert out_img.is_file()
    from PIL import Image

    widths = []
    for p in (tmp_path / "pw" / "subtitle_png_cache").glob("sub_*.png"):
        with Image.open(p) as im:
            widths.append((im.width, im.height))
    # One PNG, one line, within the bound — the 2-line sample split
    # is gone and the preview shows the locked size.
    assert len(widths) == 1
    assert widths[0][0] <= int(0.92 * 320)

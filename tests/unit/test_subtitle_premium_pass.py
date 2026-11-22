"""Comprehensive unit tests for Brief v2 — Recap Subtitle Premium Pass (S1–S6, D1–D4)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("BOT_TOKEN", "test_token")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("ADMIN_TELEGRAM_ID", "123456")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.schema import UserPreference  # noqa: E402
from app.services.ledger import get_or_create_user  # noqa: E402
from app.services.recap_engine import core, png_subtitles as pq  # noqa: E402
from app.webapp import api as api_mod  # noqa: E402

APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")


# ── S1: Grapheme-Aware, Meaning-Preserving Line Breaking ───────────

def test_s1_grapheme_clusters_preserve_stacked_and_kinzi():
    words = ["သင်္ကြန်", "ပစ္စည်း", "ပြဿနာ", "ဆုံးဖြတ်", "မြို့ကြီး"]
    for word in words:
        clusters = core.myanmar_grapheme_clusters(word)
        assert "".join(clusters) == word
        assert core.grapheme_count(word) == len(clusters)
    # Every consonant/virama stack stays intact as one visual cluster.
    assert core.myanmar_grapheme_clusters("ပစ္စည်း") == ["ပ", "စ္စ", "ည်း"]
    assert core.myanmar_grapheme_clusters("ပြဿနာ") == ["ပြ", "ဿ", "နာ"]


def test_s1_no_stranded_particles_and_priority_breaks():
    text = (
        "သင်္ကြန်ပွဲတော်ကြီးမှာ ပစ္စည်းတွေကို ဝယ်ပြီးတော့ "
        "AirAsia Flight 123 နဲ့ ရန်ကုန်မြို့ကြီးကို ပြန်လာခဲ့ကြပါတယ်။"
    )
    for font_key in ("padauk", "noto_sans"):
        fpath = core.subtitle_font_file(font_key)
        cues = core.split_burmese_subtitle_cues(
            0.0, 8.0, text, lines_per_cue=1, font_px=54, font_path=fpath, stroke_px=3
        )
        assert len(cues) >= 3
        for _, _, txt in cues:
            assert core.grapheme_count(txt) <= 24
            assert pq.measure_line_width_px(txt, 54, fpath, 3) <= 840
            # Never split a Myanmar stacked cluster or ASCII word.
            first_tok = txt.split()[0]
            assert not core._cx_is_stranded_particle(first_tok), (
                f"Stranded particle opening cue: {txt!r}"
            )
        rejoined = "".join(c[2].replace(" ", "") for c in cues)
        assert rejoined == text.replace(" ", "")


def test_s1_respects_sentence_punctuation_and_multiline_wrapping():
    text = "ပထမစာကြောင်းပြီးပါပြီ။ ဒုတိယစာကြောင်းမှာတော့ ပစ္စည်းတွေကို သေချာစစ်ဆေးပြီးမှ ပြန်ထွက်လာခဲ့ပါတယ်။"
    cues_2 = core.split_burmese_subtitle_cues(0.0, 9.0, text, lines_per_cue=2)
    assert cues_2
    fpath = core.subtitle_font_file("padauk")
    for s, e, txt in cues_2:
        assert e > s
        lines = txt.split("\n")
        assert 1 <= len(lines) <= 2
        for ln in lines:
            assert core.grapheme_count(ln) <= 28
            assert pq.measure_line_width_px(ln, 54, fpath, 2) <= 840
    # Legacy §2G explicit max_cue_chars remains deterministic and boundary-safe.
    legacy = core.split_burmese_subtitle_cues(1.0, 7.0, text, max_cue_chars=28, lines_per_cue=1)
    assert legacy[0][0] == 1.0
    assert legacy[-1][1] == 7.0


# ── S2: Reading Speed, Minimum Display Time, Silence Extension ─────

def test_s2_cps_measurement_and_end_only_silence_extension():
    cues = [
        (0.0, 0.65, "ဟုတ်ကဲ့ပါ"),              # short flash before 1.8s silence gap -> extends to 1.2s
        (2.5, 5.8, "ဒါက စမ်းသပ်စာသားပါ"),    # overhang past speech_end=3.8s -> trims to 4.0s (3.8+0.2)
        (5.85, 6.00, "နောက်တစ်ခု"),           # gap 0.05s from next start? wait let's test gap bridge below
        (6.15, 7.50, "နောက်ဆုံးပါ"),          # 6.00 -> 6.15 is 0.15s < 0.2s -> bridge without moving start!
    ]
    adjusted = core.apply_reading_speed_rules(
        cues,
        speech_ends=[0.65, 3.8, 6.0, 7.5],
        timeline_end=8.0,
    )
    # 1. Starts are strictly frozen (D3: never move onset).
    for (orig_s, _, orig_t), (adj_s, adj_e, adj_t) in zip(cues, adjusted):
        assert adj_s == orig_s
        assert adj_t == orig_t
        assert adj_e >= adj_s

    # 2. Short cue extended into real gap toward min_duration 1.2s.
    assert adjusted[0] == (0.0, 1.2, "ဟုတ်ကဲ့ပါ")
    # 3. Stale overhang trimmed to speech_end + 0.2s.
    assert adjusted[1] == (2.5, 4.0, "ဒါက စမ်းသပ်စာသားပါ")
    # 4. Tiny 0.15s gap (<0.2s) bridged cleanly to next_start without overlap.
    assert adjusted[2][1] == adjusted[3][0] == 6.15
    # 5. Metrics reflect lower or capped CPS on extended cue.
    metrics = core.measure_cue_reading_speed(adjusted)
    assert metrics[0]["duration"] == pytest.approx(1.2, abs=1e-3)
    assert metrics[0]["cps"] == pytest.approx(core.cue_cps(1.2, "ဟုတ်ကဲ့ပါ"), abs=1e-3)


def test_s2_zero_gap_fast_cues_remain_untouched():
    fast = [
        (0.0, 0.55, "မြန်မြန်ပြောတဲ့စာသားတစ်ခုပါ"),
        (0.55, 1.10, "နောက်ထပ်မြန်မြန်ပြောတဲ့စာသားပါ"),
        (1.10, 1.70, "တတိယမြန်မြန်စာသားပါ"),
    ]
    out = core.apply_reading_speed_rules(fast, speech_ends=[0.55, 1.10, 1.70], timeline_end=1.70)
    assert out == fast


def test_s2_pipeline_srt_ass_and_burn_share_exact_timings(tmp_path):
    """D3 + S2 timing parity: SRT, ASS, and burn subtitle_cues share identical post-S2 timings."""
    sync = core.EdgeSmartSync(job_dir=str(tmp_path), voice="my-MM-ThihaNeural", render_final_video=False)
    sync.overlays = {
        "burn_subtitles": True,
        "sub_style": "cinematic",
        "subtitles_font": "padauk",
        "subtitles_font_size": 16,
        "sub_lines": 1,
        "sub_margin": 0.05,
        "output_ratio": "9:16",
        "frame_fill": "blur",
    }
    timing = [
        {"audio_start": 0.0, "audio_end": 0.7, "text": "ဟုတ်ကဲ့ပါ။"},
        {"audio_start": 2.4, "audio_end": 3.5, "text": "သင်္ကြန်ပွဲတော်ကြီးမှာ ပစ္စည်းတွေကို ဝယ်ပြီးတော့ ပြန်လာခဲ့ပါတယ်။"},
    ]
    # Exercise the exact cue-splitting + S2 polish + SRT + ASS + subtitle_cues builder path.
    srt_cues = []
    speech_ends = []
    burn_font_px = core.subtitle_font_px(1080, 1920, 16, 1, None)
    burn_font_path = core.overlays_font_path(sync.overlays)
    for item in timing:
        cs = core.split_burmese_subtitle_cues(
            item["audio_start"], item["audio_end"], item["text"],
            lines_per_cue=1, font_px=burn_font_px, font_path=burn_font_path, stroke_px=3,
        )
        for s, e, t in cs:
            srt_cues.append((s, e, t))
            speech_ends.append(float(e))
    final_cues = core.apply_reading_speed_rules(srt_cues, speech_ends=speech_ends, timeline_end=4.0)
    # First short cue extended to 1.2s in real silence gap.
    assert final_cues[0][0] == 0.0 and final_cues[0][1] == 1.2
    ass_path = tmp_path / "synced.ass"
    core.write_synced_ass_file(final_cues, ass_path, 1080, 1920, raw_font_size=16, sub_style="cinematic")
    parsed_ass = pq.parse_ass_source(ass_path)
    assert parsed_ass is not None
    assert [(round(a, 2), round(b, 2), t) for a, b, t in parsed_ass["cues"]] == [
        (round(a, 2), round(b, 2), core.strip_burmese_subtitle_punctuation(t))
        for a, b, t in final_cues
    ]
    assert all("။" not in t and "၊" not in t for _, _, t in parsed_ass["cues"])


# ── S3: Subtitle Size & Scale Recalibration (D1) ───────────────────

@pytest.mark.parametrize(
    "pt,ff_1,ff_2,lb_1,lb_2,ls_1,ls_2",
    [
        (10, 33, 42, 19, 23, 33, 42),
        (16, 54, 68, 30, 38, 54, 68),
        (26, 90, 113, 51, 63, 90, 113),
    ],
)
def test_s3_pt_to_px_shortest_edge_matrix(pt, ff_1, ff_2, lb_1, lb_2, ls_1, ls_2):
    lb = core.frame_layout("9:16", "blur", 1920, 1080)
    assert core.subtitle_font_px(1080, 1920, pt, 1, None) == ff_1
    assert core.subtitle_font_px(1080, 1920, pt, 2, None) == ff_2
    assert core.subtitle_font_px(1080, 1920, pt, 1, lb) == lb_1
    assert core.subtitle_font_px(1080, 1920, pt, 2, lb) == lb_2
    assert core.subtitle_font_px(1920, 1080, pt, 1, None) == ls_1
    assert core.subtitle_font_px(1920, 1080, pt, 2, None) == ls_2


def test_s3_line_height_multiplier_and_slider_d1_unchanged():
    assert 1.35 <= pq.SUBTITLE_LINE_HEIGHT_MULT <= 1.45
    assert pq.SUBTITLE_LINE_HEIGHT_MULT == pytest.approx(1.44)
    # D1: UI slider stays 10..26pt default 16.
    assert 'id="slider-font-size"' in INDEX_HTML
    assert 'min="10" max="26"' in INDEX_HTML
    assert 'value="16"' in INDEX_HTML
    assert "[[13, 4.0], [16, 5.0], [19, 6.0], [22, 7.0]]" in APP_JS
    assert "Math.min(r.pw * canvasW, r.ph * canvasH)" in APP_JS


# ── S4 + D2: Presets, Cinematic Default, Glass Pill, UI Parity ──────

def test_s4_unified_preset_configs_and_cinematic_glass_specs():
    assert len(core.SUBTITLE_STYLE_PRESETS) == 12
    for key in core.SUBTITLE_STYLE_PRESETS:
        cfg = core.subtitle_style_config(key)
        for field in (
            "font", "font_weight", "text_color", "outline_color",
            "bg_mode", "bg_color", "bg_opacity",
            "primary", "outline_col", "back_col", "border_style", "outline", "shadow",
        ):
            assert field in cfg, f"{key} missing {field}"
        assert f'value="{key}"' in INDEX_HTML
    for ikey in ("cinematic", "glass", "yellow", "white", "boxed", "neon", "rose", "orange", "mint"):
        assert f"sub_style_{ikey}" in I18N_JS
        assert I18N_JS.count(f"sub_style_{ikey}:") == 2

    cin = core.subtitle_style_config("cinematic")
    assert cin["text_color"] == "#FFFFFF"
    assert cin["outline_color"] == "#000000"
    assert cin["shadow_soft"] == {"alpha": 128, "blur_px": 4, "offset_x": 2, "offset_y": 3}
    cin_vis_v = pq.preset_visuals(cin, 1920, 54)
    cin_vis_h = pq.preset_visuals(cin, 1080, 54)
    assert cin_vis_v["stroke_px"] == 3
    assert cin_vis_h["stroke_px"] == 3
    assert cin_vis_v["shadow_soft"] == {"alpha": 128, "blur_px": 4, "offset_x": 2, "offset_y": 3}

    glass = core.subtitle_style_config("glass")
    assert glass["bg_mode"] == "pill"
    assert glass["bg_opacity"] == pytest.approx(0.58, abs=0.01)
    assert glass["outline"] == 1.0
    glass_vis = pq.preset_visuals(glass, 1920, 54)
    assert glass_vis["bg_mode"] == "pill"
    assert glass_vis["band"] == (0, 0, 0, 148)
    assert ".vcp-sub-sample.sample-cinematic" in STYLE_CSS
    assert ".vcp-sub-sample.sample-glass" in STYLE_CSS


@pytest.mark.asyncio
async def test_d2_new_user_receives_cinematic_while_existing_user_keeps_stored(db_session):
    assert api_mod.DEFAULT_USER_PREFS["recapSubStyle"] == "yellow"
    u_new, _ = await get_or_create_user(db_session, telegram_id=910001)
    res_new = await api_mod.get_user_preferences(user=u_new, db=db_session)
    assert res_new["is_new"] is True
    assert res_new["preferences"]["recapSubStyle"] == "cinematic"
    # Read-only on GET: zero DB row created (no write-on-read).
    assert await db_session.get(UserPreference, u_new.id) is None

    # Existing user row with omitted recapSubStyle still defaults to "yellow".
    u_old_omitted, _ = await get_or_create_user(db_session, telegram_id=910002)
    db_session.add(UserPreference(user_id=u_old_omitted.id, prefs_json='{"recapFontSize": 18}'))
    await db_session.flush()
    res_omitted = await api_mod.get_user_preferences(user=u_old_omitted, db=db_session)
    assert res_omitted["is_new"] is False
    assert res_omitted["preferences"]["recapSubStyle"] == "yellow"

    # Existing user with explicit saved style keeps it untouched.
    u_old_saved, _ = await get_or_create_user(db_session, telegram_id=910003)
    db_session.add(UserPreference(user_id=u_old_saved.id, prefs_json='{"recapSubStyle": "neon"}'))
    await db_session.flush()
    res_saved = await api_mod.get_user_preferences(user=u_old_saved, db=db_session)
    assert res_saved["is_new"] is False
    assert res_saved["preferences"]["recapSubStyle"] == "neon"


# ── S5: Placement Verification (D4) ────────────────────────────────

def test_s5_placement_constants_and_letterbox_fullframe_margins(tmp_path):
    assert api_mod.DEFAULT_USER_PREFS["recapSubMargin"] == 5
    assert api_mod.DEFAULT_USER_PREFS["recapSubX"] == 50
    assert api_mod.DEFAULT_USER_PREFS["recapSubW"] == 92

    # 1080x1920 full-frame: 5% bottom margin of picture height = 96px above bottom.
    plate_ff = core.subtitle_plate_box(
        1080, 1920, None, raw_font_size=16, sub_lines=1, sub_margin=0.05
    )
    assert (plate_ff["y"] + plate_ff["h"] / 2.0) * 1920 == pytest.approx(1920 * 0.95, abs=1.5)

    # 16:9->9:16 letterbox: 5% of 608px picture sits strictly inside picture rim (1264px).
    lb = core.frame_layout("9:16", "blur", 1920, 1080)
    plate_lb = core.subtitle_plate_box(
        1080, 1920, lb, raw_font_size=16, sub_lines=1, sub_margin=0.05
    )
    rim_target = lb["oy"] + lb["ph"] - 0.05 * lb["ph"]
    assert (plate_lb["y"] + plate_lb["h"] / 2.0) * 1920 == pytest.approx(rim_target, abs=1.5)

    # ASS side file preserves horizontal margin >= 20 and centred X expression by default.
    ass_out = core.write_synced_ass_file(
        [(0.0, 2.0, "မင်္ဂလာပါ")], tmp_path / "place.ass", 1080, 1920,
        raw_font_size=16, sub_margin=0.05, sub_style="cinematic",
    )
    style_line = next(ln for ln in ass_out.read_text(encoding="utf-8").splitlines() if ln.startswith("Style:"))
    parts = style_line.split(",")
    margin_l, margin_r = int(parts[19]), int(parts[20])
    assert margin_l == margin_r == max(20, int(round(1080 * 0.04)))
    assert core._subtitle_overlay_x_expr(1080, 1920, None, 0.5) == "(W-w)/2"


# ── S6: Unicode Canonicalization & Mixed Latin/Digit Fallback ──────

def test_s6_canonicalize_nfc_dotted_circle_and_medial_reorder():
    # Swapped e-vowel before medial ya / ra reorders cleanly to valid NFC syllable.
    assert core.canonicalize_burmese_text("\u1000\u1031\u103b\u102c\u1004\u103a\u1038") == "ကျောင်း"
    assert core.canonicalize_burmese_text("\u1019\u1031\u103c\u102c\u1004\u103a\u1038") == "မြောင်း"
    # Misordered combining vowel + zero-width space artifacts normalize cleanly; idempotent.
    dirty = "သင်္ကြန်\u200b \u1000\u1031\u103b\u102c\u1004\u103a\u1038 AirAsia 2026"
    cleaned = core.canonicalize_burmese_text(dirty)
    assert cleaned == "သင်္ကြန် ကျောင်း AirAsia 2026"
    assert core.canonicalize_burmese_text(cleaned) == cleaned


def test_s6_font_cmap_fallback_runs_and_scaled_latin():
    noto = core.subtitle_font_file("noto_sans")
    padauk = core.subtitle_font_file("padauk")
    fb = pq.fallback_font_path(noto)
    assert fb is not None and fb == padauk
    assert pq.split_font_runs("ရန်ကုန် AirAsia 2026", padauk) == [
        ("ရန်ကုန် AirAsia 2026", False)
    ]  # Padauk natively covers ASCII + Myanmar without fallback runs

    runs = pq.split_font_runs("ရန်ကုန်မြို့ Air Asia 2026 မှာ", noto)
    assert runs == [("ရန်ကုန်မြို့ ", False), ("Air Asia 2026", True), (" မှာ", False)]
    assert pq.fallback_font_px(54) == round(54 * 0.88)
    w = pq.measure_line_width_px("သင်္ကြန် AirAsia 2026", 54, noto, stroke_px=3)
    assert w > 0


# ── Pixel Render Checks (Linux CI / libraqm hosts) ─────────────────

def test_s4_png_cinematic_glass_and_outline_outside_fill(tmp_path):
    """Requires libraqm (passes on Linux CI; host-gated on Windows without libraqm)."""
    from PIL import Image

    renderer = pq.PngSubtitleRenderer(tmp_path / "png_cache")
    padauk = core.subtitle_font_file("padauk")

    cin_vis = pq.preset_visuals(core.SUBTITLE_STYLE_PRESETS["cinematic"], 1920, 54)
    cin_res = renderer.render_text("သင်္ကြန် ပစ္စည်း", 1, 54, padauk, cin_vis)
    assert cin_res is not None
    cin_img = Image.open(cin_res[0]).convert("RGBA")
    pixels = list(cin_img.getdata())
    # Pure white glyph interior, opaque black stroke pixels, translucent soft shadow.
    assert any(r > 235 and g > 235 and b > 235 and a > 220 for r, g, b, a in pixels)
    assert any(r < 30 and g < 30 and b < 30 and a > 200 for r, g, b, a in pixels)
    assert any(r < 40 and g < 40 and b < 40 and 15 <= a <= 170 for r, g, b, a in pixels)

    glass_vis = pq.preset_visuals(core.SUBTITLE_STYLE_PRESETS["glass"], 1920, 54)
    glass_res = renderer.render_text("ရန်ကုန်မြို့", 1, 54, padauk, glass_vis)
    assert glass_res is not None
    glass_img = Image.open(glass_res[0]).convert("RGBA")
    gpix = list(glass_img.getdata())
    # ~58% translucent black pill background around white glyphs + rounded transparent corners.
    assert gpix[0][3] == 0  # rounded corner transparent
    assert any(r < 15 and g < 15 and b < 15 and 120 <= a <= 175 for r, g, b, a in gpix)

    # Smallest font size (18px) outline-outside-glyph check on dense stacked word: white ink remains vivid.
    min_vis = pq.preset_visuals(core.SUBTITLE_STYLE_PRESETS["cinematic"], 640, 18)
    min_res = renderer.render_text("ပစ္စည်း", 1, 18, padauk, min_vis)
    assert min_res is not None
    min_img = Image.open(min_res[0]).convert("RGBA")
    white_px = sum(1 for r, g, b, a in min_img.getdata() if r > 220 and g > 220 and b > 220 and a > 200)
    assert white_px > 20


def test_s6_png_mixed_script_and_canonicalized_render(tmp_path):
    """Requires libraqm (passes on Linux CI; host-gated on Windows without libraqm)."""
    from PIL import Image

    renderer = pq.PngSubtitleRenderer(tmp_path / "png_cache")
    noto = core.subtitle_font_file("noto_sans")
    vis = pq.preset_visuals(core.SUBTITLE_STYLE_PRESETS["cinematic"], 1920, 54)

    res = renderer.render_text("သင်္ကြန် AirAsia 2026", 1, 54, noto, vis)
    assert res is not None
    img = Image.open(res[0]).convert("RGBA")
    # Right-half Latin/digits render bright white pixels (fallback Padauk active, no tofu collapse).
    right_white = sum(
        1
        for y in range(img.height)
        for x in range(img.width // 2, img.width)
        for r, g, b, a in [img.getpixel((x, y))]
        if r > 220 and g > 220 and b > 220 and a > 200
    )
    assert right_white > 40

    # Canonicalized broken sequence renders identically to canonical NFC.
    fixed_txt = core.canonicalize_burmese_text("\u1000\u1031\u103b\u102c\u1004\u103a\u1038\u200b")
    r1 = renderer.render_text(fixed_txt, 1, 54, noto, vis)
    r2 = renderer.render_text("ကျောင်း", 1, 54, noto, vis)
    assert r1 is not None and r2 is not None and r1[0] == r2[0]

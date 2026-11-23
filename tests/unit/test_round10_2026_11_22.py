"""Round 10 (2026-11-22) regression & feature test suite.

Covers:
- Part A1–A3 (Recap Step 1 simplification + trimmed notes + i18n parity)
- Part B1–B8 (Recap Step 2 simplification + locked Pro defaults on client & server)
- Part C (Card-style token unification for Recap Step 1 and Voice Step 2)
- Part D (Cross-segment full-line width-true Burmese subtitle repack)
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")
MY_MSG = json.loads((ROOT / "locales/my/messages.json").read_text(encoding="utf-8"))
EN_MSG = json.loads((ROOT / "locales/en/messages.json").read_text(encoding="utf-8"))


# ── Part A & B: Static HTML / JS / i18n / CSS verification ───────────────────

def test_part_a_step1_dropzone_header_and_notes():
    # A1: Compact single chooser row inside #recap-video-drop-zone
    dz_match = re.search(
        r'id="recap-video-drop-zone".*?</label>',
        INDEX_HTML,
        re.S,
    )
    assert dz_match, "#recap-video-drop-zone label present"
    dz_html = dz_match.group(0)
    assert 'id="recap-drop-icon"' in dz_html
    assert 'data-i18n="recap_choose_video_file"' in dz_html
    assert 'id="recap-drop-file-state"' in dz_html
    assert 'id="recap-drop-desktop-hint"' in dz_html
    assert 'id="recap-video-file-input"' in INDEX_HTML

    # Old 3-line dropzone text & retired keys are gone everywhere
    for retired in (
        "recap_drop_text",
        "recap_drop_subtext",
        "recap_drop_limits",
        "recap_platforms_note",
        "link_help_recap",
    ):
        assert retired not in INDEX_HTML
        assert retired not in I18N_JS
        assert retired not in MY_MSG
        assert retired not in EN_MSG

    # New key + shortened copyright note exist in both locales (single-language)
    assert 'recap_choose_video_file: "ဗီဒီယိုဖိုင် ရွေးချယ်မည်"' in I18N_JS
    assert 'recap_choose_video_file: "Choose Video File"' in I18N_JS
    assert "မူပိုင်ခွင့် တာဝန်မှာ အသုံးပြုသူ၏ တာဝန်သာ ဖြစ်ပါသည်။" in I18N_JS
    assert "Copyright responsibility rests with the user." in I18N_JS

    # A2: Step 1 in-card header deleted; nav pill keeps recap_step1_title
    step1_block = re.search(
        r'id="recap-step-1".*?id="recap-step-1-body"',
        INDEX_HTML,
        re.S,
    )
    assert step1_block
    assert "recap-step-head" not in step1_block.group(0)
    assert 'data-i18n="recap_step1_title"' in INDEX_HTML

    # A3: Notes expander contains only shortened rights note (Round 15 Part A retired tier pill carousel)
    notes_match = re.search(
        r'class="[^"]*recap-notes-collapse[^"]*".*?</details>',
        INDEX_HTML,
        re.S,
    )
    assert notes_match
    notes_html = notes_match.group(0)
    assert 'data-i18n="recap_rights_note"' in notes_html
    assert 'id="studio-alert-carousel"' not in notes_html
    assert "recap_platforms_note" not in notes_html
    assert "link_help_recap" not in notes_html


def test_part_b_step2_simplified_structure_and_removed_ui():
    # B1: Nav pill 2 text updated in both locales
    assert 'data-i18n="recap_step_nav_preview">Settings ချိန်ရန်<' in INDEX_HTML
    assert 'recap_step_nav_preview: "Settings ချိန်ရန်"' in I18N_JS
    assert 'recap_step_nav_preview: "Settings"' in I18N_JS

    # B2: Step 2 in-card header removed
    step2_head = re.search(
        r'id="recap-step-2".*?id="recap-step-2-body"',
        INDEX_HTML,
        re.S,
    )
    assert step2_head
    assert "recap-step-head" not in step2_head.group(0)

    # B3: Seek section keeps heading + 5 quick chips only
    seek_block = re.search(
        r'id="tb-panel-seek".*?<!-- Row 1: Ratio -->',
        INDEX_HTML,
        re.S,
    )
    assert seek_block
    seek_html = seek_block.group(0)
    for sec in ("0", "5", "15", "30", "60"):
        assert f'data-sec="{sec}"' in seek_html

    # Removed controls across Step 2 & retired i18n keys
    for removed_id in (
        "slider-canvas-seek",
        "canvas-seek-time-badge",
        "btn-seek-back-5",
        "btn-canvas-play-toggle",
        "btn-seek-fwd-5",
        "recap-fill-picker",
        "recap-fill-note",
        "recap-crop-presets-toolbar",
        "recap-crop-presets",
        "slider-crop-top",
        "slider-crop-bottom",
        "slider-crop-left",
        "slider-crop-right",
        "btn-sync-crop",
        "recap-preview-transform-group",
        "btn-toggle-mirror",
        "recap-zoom-picker",
        "recap-protection-picker",
        "recap-cat-preset",
        "recap-master-presets-grid",
        "recap-step2-summary",
        "recap-my-presets-section",
        "btn-reset-canvas-layout",
    ):
        assert removed_id not in INDEX_HTML, f"{removed_id} should be removed from index.html"
        assert removed_id not in APP_JS, f"{removed_id} should be removed from app.js"

    for retired_fn in (
        "RECAP_MASTER_PRESETS",
        "syncFillPickerState",
        "applyCropValues",
        "syncRecapMasterPresetActive",
        "renderMyRecapPresets",
        "toggleRecapCropDetail",
    ):
        assert retired_fn not in APP_JS, f"{retired_fn} should be removed from app.js"

    # B7/B8: Canvas corner reset + Ratio cards + Sub/Blur/Logo accordions kept
    assert 'id="btn-preview-corner-reset"' in INDEX_HTML
    assert 'id="recap-preview-ratio-group"' in INDEX_HTML
    for r in ("9:16", "16:9", "1:1", "Original"):
        assert f'data-ratio="{r}"' in INDEX_HTML
    assert 'id="recap-subtitle-group"' in INDEX_HTML
    assert 'id="recap-blur-group"' in INDEX_HTML
    assert 'id="recap-logo-group"' in INDEX_HTML


def test_part_c_card_style_unification_css():
    assert "#tab-recap #recap-step-1 .source-mode-toggle" in STYLE_CSS
    assert "#tab-recap #recap-step-1 .input-with-btn" in STYLE_CSS
    assert "#tab-recap #recap-video-drop-zone .drop-file-state" in STYLE_CSS
    assert "#tab-voice #curated-personas-card-wrap .segmented-control" in STYLE_CSS
    assert "#tab-voice #curated-personas-card-wrap .ref-notes-collapse" in STYLE_CSS
    assert "#tab-voice #curated-personas-card-wrap .ref-notes-summary" in STYLE_CSS
    # Preserve Round 8 selector invariant
    assert "#tab-voice .ref-sample-card" in STYLE_CSS


# ── Part B4–B6: Locked Pro defaults (Client & Server) ────────────────────────

def test_locked_pro_defaults_client_and_server():
    from app.services.downloader import MediaDownloader
    from app.services.recap_engine import core, recap_pipeline
    from app.webapp import api as api_mod

    # Client JS constants & helper present
    assert 'const RECAP_LOCKED_FILL = "blur";' in APP_JS
    assert 'const RECAP_LOCKED_PROTECTION = "strong";' in APP_JS
    assert "const RECAP_LOCKED_MIRROR = true;" in APP_JS
    assert "const RECAP_LOCKED_ZOOM = 1.1;" in APP_JS
    assert "function recapLockedCropForRatio(ratio)" in APP_JS

    # Server DEFAULT_USER_PREFS
    prefs = api_mod.DEFAULT_USER_PREFS
    assert prefs["recapRatioFill"] == "blur"
    assert prefs["recapCropTop"] == 12
    assert prefs["recapCropBottom"] == 12
    assert prefs["recapCropLeft"] == 0
    assert prefs["recapCropRight"] == 0
    assert prefs["recapProtection"] == "strong"
    assert prefs["recapMirror"] is True
    assert prefs["recapZoom"] == 1.1

    # Server Pydantic request models enforce locked values even against stale payloads
    for req in (
        api_mod.RecapConfigRequest(
            url="https://example.com/v.mp4",
            ratio="9:16",
            ratio_fill="crop",
            crop_top=0,
            crop_bottom=0,
            crop_left=5,
            crop_right=5,
            mirror_video=False,
            zoom_factor=1.0,
            protection_level="off",
        ),
        api_mod.RecapPreviewCompositeRequest(
            ratio="16:9",
            ratio_fill="black",
            crop_top=25,
            crop_bottom=25,
            mirror_video=False,
            zoom_factor=1.0,
            protection_level="standard",
        ),
    ):
        assert req.ratio_fill == "blur"
        assert req.crop_top == 12
        assert req.crop_bottom == 12
        assert req.crop_left == 0
        assert req.crop_right == 0
        assert req.mirror_video is True
        assert req.zoom_factor == 1.1
        assert req.protection_level == "strong"

    # Original ratio zeroes crop while keeping locked fill/protection/mirror/zoom
    for orig_req in (
        api_mod.RecapConfigRequest(
            url="https://example.com/v.mp4",
            ratio="Original",
            ratio_fill="black",
            crop_top=12,
            crop_bottom=12,
            mirror_video=False,
            zoom_factor=1.0,
            protection_level="off",
        ),
        api_mod.RecapPreviewCompositeRequest(
            ratio="Original",
            crop_top=12,
            crop_bottom=12,
            mirror_video=False,
            zoom_factor=1.0,
            protection_level="off",
        ),
    ):
        assert orig_req.crop_top == 0
        assert orig_req.crop_bottom == 0
        assert orig_req.crop_left == 0
        assert orig_req.crop_right == 0
        assert orig_req.ratio_fill == "blur"
        assert orig_req.protection_level == "strong"
        assert orig_req.mirror_video is True
        assert orig_req.zoom_factor == 1.1

    # Backend function signature defaults match locked values
    for fn in (
        MediaDownloader.generate_recap_video,
        MediaDownloader.generate_recap_preview,
        recap_pipeline.run_recap_pipeline,
        recap_pipeline.generate_recap_preview,
    ):
        sig = inspect.signature(fn).parameters
        assert sig["frame_fill"].default == "blur"
        assert sig["crop_top"].default == pytest.approx(0.12)
        assert sig["crop_bottom"].default == pytest.approx(0.12)
        assert sig["crop_left"].default == pytest.approx(0.0)
        assert sig["crop_right"].default == pytest.approx(0.0)
        assert sig["mirror_video"].default is True
        assert sig["zoom_factor"].default == pytest.approx(1.1)
        assert sig["protection_level"].default == "strong"

    pv_sig = inspect.signature(core.extract_preview_with_overlays).parameters
    assert pv_sig["frame_fill"].default == "blur"
    assert pv_sig["crop_top"].default == pytest.approx(0.12)
    assert pv_sig["crop_bottom"].default == pytest.approx(0.12)
    assert pv_sig["mirror_video"].default is True
    assert pv_sig["zoom_factor"].default == pytest.approx(1.1)

    sync_sig = inspect.signature(core.EdgeSmartSync.__init__).parameters
    assert sync_sig["frame_fill"].default == "blur"
    assert sync_sig["crop_top"].default == pytest.approx(0.12)
    assert sync_sig["crop_bottom"].default == pytest.approx(0.12)
    assert sync_sig["mirror_video"].default is True
    assert sync_sig["zoom_factor"].default == pytest.approx(1.1)

    # core.protection_zoom_factor preserves 1.1 under "strong"
    assert core.protection_zoom_factor("strong", 1.1) == pytest.approx(1.1)

    # core.frame_layout applies crop for 9:16 and bypasses crop for Original
    lay_vert = core.frame_layout("9:16", "blur", 1920, 1080, crop_top=0.12, crop_bottom=0.12)
    assert "crop=" in lay_vert["crop_filter"]
    lay_orig = core.frame_layout("Original", "blur", 1920, 1080, crop_top=0.12, crop_bottom=0.12)
    assert lay_orig["crop_filter"] == ""


# ── Part D: Cross-segment width-true Burmese subtitle repack ─────────────────

def _padauk_font_path() -> Path | None:
    from app.services.recap_engine import core

    return core.overlays_font_path({"sub_font": "padauk"})


def test_part_d_full_width_bound_unblocked():
    from app.services.recap_engine import core

    # 0.92 * 1080 = 994 (no longer capped at S1 840)
    assert core._subtitle_fit_width_px({"sub_w": 0.92}, 1080) == 994
    assert core._subtitle_fit_width_px({}, 1080) == 994


def test_part_d_three_short_segments_merge_into_one_full_width_cue():
    from app.services.recap_engine import core

    font_path = _padauk_font_path()
    # Default 1080 canvas with sub_w=92 -> ~994px minus padding -> ~920px ink width
    max_ink_w = 920
    font_px = 42
    segments = [
        {"audio_start": 0.0, "audio_end": 1.2, "speech_duration": 1.2, "text": "မနက်ခင်းမှာ"},
        {"audio_start": 1.25, "audio_end": 2.4, "speech_duration": 1.15, "text": "သူထွက်လာတယ်"},
        {"audio_start": 2.50, "audio_end": 3.8, "speech_duration": 1.3, "text": "အိမ်ပြန်သွားပြီ။"},
    ]
    cues, speech_ends = core.pack_subtitle_cues_from_segments(
        segments,
        lines_per_cue=1,
        max_width_px=max_ink_w,
        font_px=font_px,
        font_path=font_path,
        stroke_px=2,
    )
    assert len(cues) == 1
    assert cues[0][0] == pytest.approx(0.0, abs=0.01)
    assert cues[0][1] == pytest.approx(3.8, abs=0.01)
    assert "မနက်ခင်းမှာ" in cues[0][2]
    assert "သူထွက်လာတယ်" in cues[0][2]
    assert "အိမ်ပြန်သွားပြီ" in cues[0][2]
    assert "။" not in cues[0][2] and "၊" not in cues[0][2]
    assert len(speech_ends) == 1


def test_part_d_pause_gt_035s_blocks_merging():
    from app.services.recap_engine import core

    font_path = _padauk_font_path()
    segments = [
        {"audio_start": 0.0, "audio_end": 1.2, "speech_duration": 1.2, "text": "ပထမစာကြောင်း"},
        {"audio_start": 1.65, "audio_end": 2.8, "speech_duration": 1.15, "text": "ဒုတိယစာကြောင်း။"},
    ]
    # Gap = 1.65 - 1.2 = 0.45s > 0.35s -> must NOT merge
    cues, _ = core.pack_subtitle_cues_from_segments(
        segments,
        lines_per_cue=1,
        max_width_px=920,
        font_px=42,
        font_path=font_path,
        stroke_px=2,
    )
    assert len(cues) == 2
    assert cues[0][1] <= 1.21
    assert cues[1][0] >= 1.64


def test_part_d_long_span_splits_at_burmese_sentence_stop_with_proportional_timing():
    from app.services.recap_engine import core

    font_path = _padauk_font_path()
    s1 = "သူကတစ်နေ့လုံးအလုပ်ကြိုးစားလုပ်ကိုင်ခဲ့ပါတယ်။"
    s2 = "ညနေရောက်တော့မှအိမ်ကိုအေးအေးဆေးဆေးပြန်လာခဲ့ပါတယ်။"
    segments = [
        {"audio_start": 0.0, "audio_end": 3.0, "speech_duration": 3.0, "text": s1},
        {"audio_start": 3.1, "audio_end": 6.1, "speech_duration": 3.0, "text": s2},
    ]
    # Width chosen so s1 fits on one line, while s1 + " " + s2 exceeds it
    s1_w = core._measure_text_width_px(s1, 48, font_path, 2)
    both_w = core._measure_text_width_px(f"{s1} {s2}", 48, font_path, 2)
    bound_w = (s1_w + both_w) // 2
    assert s1_w <= bound_w < both_w

    cues, _ = core.pack_subtitle_cues_from_segments(
        segments,
        lines_per_cue=1,
        max_width_px=bound_w,
        font_px=48,
        font_path=font_path,
        stroke_px=2,
    )
    assert len(cues) == 2
    assert cues[0][2] == core.strip_burmese_subtitle_punctuation(s1)
    assert cues[1][2] == core.strip_burmese_subtitle_punctuation(s2)
    assert all("။" not in c[2] and "၊" not in c[2] for c in cues)
    assert cues[0][0] == pytest.approx(0.0, abs=0.01)
    assert cues[1][1] == pytest.approx(6.1, abs=0.01)
    assert cues[1][0] >= cues[0][1]
    assert (cues[0][1] - cues[0][0]) >= 0.4
    assert (cues[1][1] - cues[1][0]) >= 0.4
    # Sequential and proportional to ~equal halves around ~3.0s
    assert 2.2 <= cues[0][1] <= 3.9


def test_part_d_cps_retiming_caps_reading_speed_after_merge():
    from app.services.recap_engine import core

    font_path = _padauk_font_path()
    # Two rapid short segments followed by 2s silence gap -> extend end cleanly within gap
    segments = [
        {"audio_start": 0.0, "audio_end": 0.4, "speech_duration": 0.4, "text": "ဒီဇာတ်လမ်းဟာ"},
        {"audio_start": 0.45, "audio_end": 0.85, "speech_duration": 0.4, "text": "အရမ်းကောင်းပါတယ်။"},
        {"audio_start": 3.0, "audio_end": 5.0, "speech_duration": 2.0, "text": "နောက်တစ်ခန်းပါ။"},
    ]
    raw_cues, speech_ends = core.pack_subtitle_cues_from_segments(
        segments,
        lines_per_cue=1,
        max_width_px=920,
        font_px=42,
        font_path=font_path,
        stroke_px=2,
    )
    assert len(raw_cues) == 2
    adjusted = core.apply_reading_speed_rules(raw_cues, speech_ends=speech_ends, timeline_end=6.0)
    assert len(adjusted) == 2
    # Extended toward min_duration / lower CPS into the silence without overlapping cue 2
    assert adjusted[0][1] > raw_cues[0][1]
    assert adjusted[0][1] <= adjusted[1][0]


def test_part_d_word_timings_preferred_over_proportional_estimates():
    from app.services.recap_engine import core

    font_path = _padauk_font_path()
    w1 = "ပထမစာကြောင်းတိုလေးပါ။"
    w2 = "ဒုတိယစာကြောင်းလည်းအတူတူပါပဲ။"
    # Skewed word timings: w1 spoken rapidly in [0.0..0.9s], w2 stretched [0.9..5.0s]
    seg_text = f"{w1} {w2}"
    w1_w = core._measure_text_width_px(w1, 48, font_path, 2)
    both_w = core._measure_text_width_px(seg_text, 48, font_path, 2)
    bound_w = (w1_w + both_w) // 2

    skewed_wt = [
        {"start": 0.0, "end": 0.9, "text": w1},
        {"start": 0.9, "end": 5.0, "text": w2},
    ]
    cues_wt, _ = core.pack_subtitle_cues_from_segments(
        [{"audio_start": 0.0, "audio_end": 5.0, "speech_duration": 5.0, "text": seg_text, "word_timings": skewed_wt}],
        lines_per_cue=1,
        max_width_px=bound_w,
        font_px=48,
        font_path=font_path,
        stroke_px=2,
    )
    cues_prop, _ = core.pack_subtitle_cues_from_segments(
        [{"audio_start": 0.0, "audio_end": 5.0, "speech_duration": 5.0, "text": seg_text}],
        lines_per_cue=1,
        max_width_px=bound_w,
        font_px=48,
        font_path=font_path,
        stroke_px=2,
    )
    assert len(cues_wt) == 2 and len(cues_prop) == 2
    # Word-timing cue split follows 0.9s instead of ~2.2s proportional estimate
    assert cues_wt[0][1] == pytest.approx(0.9, abs=0.15)
    assert cues_prop[0][1] > 1.8


def test_part_d_streaming_packer_matches_batch_pack():
    from app.services.recap_engine import core

    font_path = _padauk_font_path()
    segments = [
        {"audio_start": 0.0, "audio_end": 1.0, "speech_duration": 1.0, "text": "ပထမအပိုင်း"},
        {"audio_start": 1.1, "audio_end": 2.2, "speech_duration": 1.1, "text": "ဒုတိယအပိုင်း။"},
        {"audio_start": 2.8, "audio_end": 4.0, "speech_duration": 1.2, "text": "တတိယအပိုင်း။"},
    ]
    packer = core.SubtitleCuePacker(
        lines_per_cue=1, max_width_px=920, font_px=42, font_path=font_path, stroke_px=2
    )
    stream_cues = []
    for seg in segments:
        st, en = seg["audio_start"], seg["audio_end"]
        stream_cues.extend(packer.add_segment(st, en, seg["text"], speech_end=st + seg["speech_duration"]))
    stream_cues.extend(packer.flush())

    batch_cues, _ = core.pack_subtitle_cues_from_segments(
        segments, lines_per_cue=1, max_width_px=920, font_px=42, font_path=font_path, stroke_px=2
    )
    assert [(a, b, c) for a, b, c, _ in stream_cues] == batch_cues

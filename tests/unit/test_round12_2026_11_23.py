"""Round 12 (2026-11-23) unit & parity tests: Step 2 canvas boxes & panels, Step 3, Step 4."""

import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from app.config import settings
from app.models.schema import AccountCategory, Job, User
from app.webapp import api as api_mod


ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = (ROOT / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app" / "webapp" / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app" / "webapp" / "static" / "css" / "style.css").read_text(encoding="utf-8")


def _extract_block(src: str, start_marker: str, end_marker: str) -> str:
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j]


def _fine_body_slice() -> str:
    return _extract_block(
        INDEX_HTML,
        '<div class="recap-fine-body" id="recap-fine-body">',
        '<div class="recap-step-next-row mt-3">',
    )


def _step3_slice() -> str:
    return _extract_block(
        INDEX_HTML,
        '<div class="card glass recap-step-card recap-step hidden" id="recap-step-3"',
        '<div class="card glass recap-step-card recap-step hidden" id="recap-step-4"',
    )


def _step4_slice() -> str:
    return _extract_block(
        INDEX_HTML,
        '<div class="card glass recap-step-card recap-step hidden" id="recap-step-4"',
        'id="recap-progress-box"',
    )


def _panel_slices():
    fine = _fine_body_slice()
    i_sub = fine.index('id="recap-subtitle-group"')
    i_blur = fine.index('id="recap-blur-group"')
    i_logo = fine.index('id="recap-logo-group"')
    return fine[i_sub:i_blur], fine[i_blur:i_logo], fine[i_logo:]


# ---------------------------------------------------------------------------
# Part A: Step 2 canvas overlay boxes
# ---------------------------------------------------------------------------


def test_part_a_canvas_boxes_single_external_text_tags_and_handles():
    canvas_block = _extract_block(
        INDEX_HTML,
        '<div class="preview-blur-box draggable-box" id="preview-blur-box"',
        '<div class="recap-dock-section hidden" id="recap-preview-controls-group">',
    )
    assert "blur-label" not in canvas_block
    assert "blur-label" not in STYLE_CSS

    blur_box = _extract_block(canvas_block, 'id="preview-blur-box"', 'id="preview-sub-box"')
    sub_box = canvas_block[canvas_block.index('id="preview-sub-box"') :]

    for side in ("top", "left", "right", "bottom"):
        assert f'class="box-resize-handle {side}"' in blur_box

    blur_tag = re.search(r'<span[^>]*class="drag-grip-icon"[^>]*>(.*?)</span>', blur_box)
    assert blur_tag is not None
    assert blur_tag.group(1).strip() == "Blur"
    assert 'data-i18n="blur_handle_short"' in blur_tag.group(0)

    sub_tag = re.search(r'<span[^>]*class="drag-grip-icon sub-grip"[^>]*>(.*?)</span>', sub_box)
    assert sub_tag is not None
    assert sub_tag.group(1).strip() == "Subtitle"
    assert 'data-i18n="sub_box_short_label"' in sub_tag.group(0)

    # Touch-friendly >= 24px resize handles + slim top-left external corner tags in CSS
    assert re.search(r'\.box-resize-handle\s*\{[^}]*height:\s*24px', STYLE_CSS)
    assert re.search(r'\.box-resize-handle\.left,\s*\.box-resize-handle\.right\s*\{[^}]*width:\s*24px', STYLE_CSS)
    assert re.search(r'#tab-recap #preview-blur-box\s*\{[^}]*border:\s*1\.5px solid', STYLE_CSS)
    assert re.search(r'#tab-recap #preview-sub-box\s*\{[^}]*border:\s*1\.5px solid', STYLE_CSS)
    assert re.search(r'#tab-recap #preview-blur-box \.drag-grip-icon\s*\{[^}]*left:\s*0;', STYLE_CSS)
    assert re.search(r'#tab-recap #preview-sub-box \.sub-grip\s*\{[^}]*left:\s*0;', STYLE_CSS)


def test_part_a_non_overlapping_default_offset_preserves_drag_config():
    node_script = """
    const vm = require('vm');
    const fs = require('fs');
    const appJs = fs.readFileSync('app/webapp/static/js/app.js', 'utf8');
    const p0 = appJs.indexOf('function recapPictureRect(');
    const p1 = appJs.indexOf('function resetBlurBoxForNewSource(', p0);
    const s0 = appJs.indexOf('function syncBlurBoxVisual(', p1);
    const sMark = appJs.indexOf('// Batch DD: mirror the geometry', s0);
    const s1 = appJs.indexOf('}', sMark) + 1;
    const geomAndSync = appJs.slice(p0, p1) + '\\n' + appJs.slice(s0, s1);

    function makeEl(val) { return { value: String(val), innerText: '', textContent: '' }; }
    const sliderBlurY = makeEl(76);
    const sliderBlurH = makeEl(7);
    const sliderBlurX = makeEl(4);
    const sliderBlurW = makeEl(92);
    const valBlurY = makeEl('');
    const valBlurH = makeEl('');
    const valBlurX = makeEl('');
    const valBlurW = makeEl('');
    const previewBlur = {
        style: {},
        dataset: {},
        classes: new Set(),
        classList: { toggle(c, on) { if (on) previewBlur.classes.add(c); else previewBlur.classes.delete(c); } },
    };
    const previewSub = { style: { display: 'block' } };
    const state = {
        recapRatio: '9:16',
        recapBlurBoxTouched: false,
        recapSubPosBand: 'bottom',
        recapBlurBoxes: [],
    };
    const ctx = {
        state,
        canvas: { clientWidth: 300, clientHeight: 533 },
        canvasVideo: null,
        previewBlur,
        previewSub,
        sliderBlurY,
        sliderBlurH,
        sliderBlurX,
        sliderBlurW,
        valBlurY,
        valBlurH,
        valBlurX,
        valBlurW,
        document: { getElementById: () => null },
        getSourceVideoAspect: () => 16 / 9,
        scheduleRecapCompositeRefresh: () => {},
        ddAfterBlurSync: () => {},
    };
    vm.runInNewContext(geomAndSync + '; globalThis.__sync = syncBlurBoxVisual; globalThis.__geom = blurBoxCanvasGeom;', ctx);

    // 1. Default untouched: visual position is offset up so tags do not overlap,
    //    while slider-blur-y stays 76 (identical config output).
    ctx.__sync();
    const untouchedTop = parseFloat(previewBlur.style.top);
    const untouchedVisualBlurY = previewBlur.dataset.visualBlurY;
    const baseGeom = ctx.__geom(76, 7, 4, 92);
    const sliderValUntouched = sliderBlurY.value;

    // 2. Touched after drag/slider: exact geometry matches base blurBoxCanvasGeom(y, h, x, w).
    state.recapBlurBoxTouched = true;
    sliderBlurY.value = '82';
    ctx.__sync();
    const touchedTop = parseFloat(previewBlur.style.top);
    const expectedTouchedTop = parseFloat((ctx.__geom(82, 7, 4, 92).centre * 100).toFixed(3));
    console.log(JSON.stringify({
        untouchedTop,
        baseTop: parseFloat((baseGeom.centre * 100).toFixed(3)),
        visualBlurY: untouchedVisualBlurY,
        sliderValUntouched,
        touchedTop,
        expectedTouchedTop,
        clearedAfterTouch: previewBlur.dataset.visualBlurY,
    }));
    """
    proc = subprocess.run(["node", "-e", node_script], cwd=str(ROOT), capture_output=True, text=True, check=True)
    out = json.loads(proc.stdout)
    assert out["untouchedTop"] < out["baseTop"]
    assert out["visualBlurY"] != ""
    assert out["sliderValUntouched"] == "76"
    assert abs(out["touchedTop"] - out["expectedTouchedTop"]) < 0.01
    assert out["clearedAfterTouch"] == ""


# ---------------------------------------------------------------------------
# Part B: Step 2 panels (#recap-fine-body)
# ---------------------------------------------------------------------------


def test_part_b1_title_only_headers_and_zero_emojis_in_fine_body():
    fine = _fine_body_slice()
    # Zero emojis inside #recap-fine-body
    emoji_re = re.compile(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]")
    assert emoji_re.findall(fine) == []

    # setupRecapAccordion must omit .dd-acc-val inside #recap-fine-body
    acc_fn = _extract_block(APP_JS, "function setupRecapAccordion()", "// (3) Blur-box LIST editor")
    assert "if (!inFine)" in acc_fn
    assert "valSpan" in acc_fn

    # refreshRecapStep2Summary must not write summaries for subtitle / blur / logo panels
    sum_fn = _extract_block(APP_JS, "function refreshRecapStep2Summary()", "window.refreshRecapStep2Summary")
    for gid in ("recap-subtitle-group", "recap-blur-group", "recap-logo-group"):
        assert gid not in sum_fn
    # Non-fine consumers remain intact
    for gid in ("recap-preview-ratio-group", "recap-story-style-acc", "recap-pace-acc", "recap-lang-acc", "recap-review-acc"):
        assert gid in sum_fn

    # Logo header retains VIP badge attribute without emojis
    assert 'id="recap-logo-group" data-acc-title-key="recap_logo_group_title" data-acc-vip-key="recap_badge_vip_only"' in fine


def test_part_b2_b3_subtitle_panel_selects_fullwidth_and_single_color_chooser():
    sub, blur, logo = _panel_slices()
    fine = _fine_body_slice()
    assert "vcp-paired-row" not in fine

    sub_l2_at = sub.rindex("<div", 0, sub.index('id="recap-sub-l2-accordion"'))
    sub_main, sub_l2 = sub[:sub_l2_at], sub[sub_l2_at:]

    # B2: Font + Lines are <select>, Font size + Width + Margin are full-width sliders
    assert '<select id="recap-sub-font-picker"' in sub_main
    assert '<select id="sub-lines-picker-toolbar"' in sub_main
    for val in ("padauk", "noto_sans", "pyidaungsu", "noto_serif", "custom"):
        assert f'value="{val}"' in sub_main
    for val in ("1", "2", "3"):
        assert f'value="{val}"' in sub_main
    assert sub_main.index('id="slider-font-size"') < sub_main.index('id="slider-sub-w"') < sub_main.index('id="slider-sub-y"')

    # B3: Old custom font upload row removed; single color chooser + hidden holders present
    assert 'id="recap-sub-font-custom-row"' not in INDEX_HTML
    assert 'id="btn-sub-font-upload"' not in INDEX_HTML
    assert 'id="btn-sub-font-clear"' not in INDEX_HTML
    assert 'id="sub-font-custom-status"' not in INDEX_HTML

    assert '<select id="recap-sub-color-target"' in sub_l2
    assert 'id="input-sub-active-color"' in sub_l2
    for cid in ("input-sub-text-color", "input-sub-outline-color", "input-sub-bg-color"):
        m = re.search(rf'<input[^>]*id="{cid}"[^>]*>', sub_l2)
        assert m is not None and "hidden" in m.group(0)

    assert '<select id="sub-pos-picker"' in sub_l2
    assert '<select id="recap-sub-render-mode"' in sub_l2
    assert '<select id="recap-sub-effect"' in sub_l2
    assert re.search(r'<div[^>]*class="slider-group[^"]*"[^>]*>[\s\S]*?id="slider-outline-width"', sub_l2)
    assert re.search(r'<div[^>]*class="slider-group[^"]*"[^>]*>[\s\S]*?id="slider-bg-opacity"', sub_l2)
    assert 'id="btn-toggle-sub-shadow"' in sub_l2
    assert 'id="btn-toggle-sub-bold"' in sub_l2
    assert 'id="slider-sub-x"' in sub_l2
    assert 'id="toggle-embed-soft-subs"' in sub_l2

    # Node VM verification: color chooser target switch + active color write-through + custom font UI sync
    node_script = """
    const vm = require('vm');
    const fs = require('fs');
    const appJs = fs.readFileSync('app/webapp/static/js/app.js', 'utf8');
    const ddStart = appJs.indexOf('function ddStyleChanged(');
    const ddEnd = appJs.indexOf('// (8) Restore-path control sync', ddStart);
    const block = appJs.slice(ddStart, ddEnd);

    const els = {};
    function mkEl(id, val = '') {
        const listeners = {};
        const el = {
            id, value: val, innerText: '', textContent: '', files: [],
            options: [
                { value: 'padauk', textContent: 'Padauk' },
                { value: 'custom', textContent: 'Upload Custom Font' },
            ],
            classList: { toggle() {} },
            addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
            dispatchEvent(ev) { (listeners[ev.type] || []).forEach(fn => fn(ev)); },
            querySelector(sel) {
                if (sel === 'option[value="custom"]') return el.options.find(o => o.value === 'custom') || null;
                if (sel === 'option[value="uploaded_custom"]') return el.options.find(o => o.value === 'uploaded_custom') || null;
                return null;
            },
            insertBefore(opt, ref) {
                const idx = el.options.indexOf(ref);
                if (idx >= 0) el.options.splice(idx, 0, opt);
                else el.options.push(opt);
            },
            appendChild(opt) { el.options.push(opt); },
            querySelectorAll() { return []; },
        };
        els[id] = el;
        return el;
    }
    mkEl('recap-sub-color-target', 'text');
    mkEl('input-sub-active-color', '#ffd94d');
    mkEl('input-sub-text-color', '#ffd94d');
    mkEl('input-sub-outline-color', '#111111');
    mkEl('input-sub-bg-color', '#222222');
    mkEl('recap-sub-font-picker', 'padauk');
    const state = { recapTextColor: '#ffd94d', recapOutlineColor: '#111111', recapBgColor: '#222222', recapSubFont: 'padauk', recapSubFontCustomId: '', recapSubFontCustomName: '' };
    const ctx = {
        state,
        DD_PRESET_SEED: {},
        document: {
            getElementById: (id) => els[id] || null,
            querySelectorAll: () => [],
            createElement: () => ({ value: '', textContent: '', remove() {} }),
        },
        window: {},
        t: (k) => k,
        haptic: () => {},
        scheduleRecapCompositeRefresh: () => {},
        refreshRecapStep2Summary: () => {},
        saveRecapSettingsSoon: () => {},
        showStudioToast: () => {},
        Event: class { constructor(type) { this.type = type; } },
    };
    vm.runInNewContext(block, ctx);
    // Test 1: switch target to outline -> active input swaps to outline color
    els['recap-sub-color-target'].value = 'outline';
    els['recap-sub-color-target'].dispatchEvent({ type: 'change' });
    const afterTargetSwitch = els['input-sub-active-color'].value;

    // Test 2: edit active color -> hidden outline holder & state update
    els['input-sub-active-color'].value = '#ff00aa';
    els['input-sub-active-color'].dispatchEvent({ type: 'input' });
    const outlineHolderAfter = els['input-sub-outline-color'].value;

    // Test 3: custom font state renders uploaded_custom option & selects it
    state.recapSubFontCustomId = 'font_99';
    state.recapSubFontCustomName = 'Pyidaungsu-Custom.ttf';
    ctx.window.ddSyncCustomFontUi();
    const fontSelectVal = els['recap-sub-font-picker'].value;
    const uploadedOpt = els['recap-sub-font-picker'].querySelector('option[value="uploaded_custom"]');
    console.log(JSON.stringify({
        afterTargetSwitch,
        outlineHolderAfter,
        stateOutlineAfter: state.recapOutlineColor,
        fontSelectVal,
        uploadedOptText: uploadedOpt ? uploadedOpt.textContent : '',
    }));
    """
    proc = subprocess.run(["node", "-e", node_script], cwd=str(ROOT), capture_output=True, text=True, check=True)
    res = json.loads(proc.stdout)
    assert res["afterTargetSwitch"] == "#111111"
    assert res["outlineHolderAfter"] == "#ff00aa"
    assert res["stateOutlineAfter"] == "#ff00aa"
    assert res["fontSelectVal"] == "uploaded_custom"
    assert "Pyidaungsu-Custom.ttf" in res["uploadedOptText"]


def test_part_b4_b5_b6_blur_and_logo_full_width_layout():
    _, blur, logo = _panel_slices()
    blur_l2_at = blur.rindex("<div", 0, blur.index('id="recap-blur-l2-accordion"'))
    blur_main, blur_l2 = blur[:blur_l2_at], blur[blur_l2_at:]

    # B4: full-width switches + OFF default + full untruncated hint + chips left / Add+Del right
    assert (
        blur_main.index('id="btn-toggle-blur"')
        < blur_main.index('id="recap-blur-autodetect-toggle"')
        < blur_main.index('vcp-blur-autodetect-hint')
        < blur_main.index('id="blur-box-list-row"')
        < blur_main.index('class="blur-box-controls-row"')
        < blur_main.index('id="blur-box-chips"')
        < blur_main.index('id="btn-blur-box-add"')
        < blur_main.index('id="btn-blur-box-del"')
        < blur_main.index('id="slider-blur-strength"')
    )
    auto_inp = re.search(r'<input[^>]*id="recap-blur-autodetect-toggle"[^>]*>', blur_main)
    assert auto_inp is not None and "checked" not in auto_inp.group(0)

    # Full-width untruncated hint rule in CSS
    hint_rule = re.search(r'#tab-recap #recap-fine-body \.vcp-blur-autodetect-hint\s*\{([^}]*)\}', STYLE_CSS)
    assert hint_rule is not None and "text-overflow: ellipsis" not in hint_rule.group(1)

    # B5: 4 full-width Blur sliders X/Y/W/H
    for sid in ("slider-blur-x", "slider-blur-y", "slider-blur-w", "slider-blur-h"):
        assert f'id="{sid}"' in blur_l2

    # B6: Logo size/opacity full-width sliders + <select id="recap-logo-pos-picker">
    assert '<select id="recap-logo-pos-picker"' in logo
    for pos in ("top-right", "top-left", "bottom-right", "bottom-left"):
        assert f'value="{pos}"' in logo
    assert 'value="top-right" data-pos="top-right" data-i18n="pos_top_right" selected' in logo


# ---------------------------------------------------------------------------
# Part C: Step 3 (voice selection)
# ---------------------------------------------------------------------------


def test_part_c_step3_header_deleted_engine_select_details_and_own_voice():
    step3 = _step3_slice()
    assert "recap-step-head" not in step3
    assert "recap_step3_title" not in step3

    # C2: <select id="recap-engine-picker"> without "none"
    m_eng = re.search(r'<select[^>]*id="recap-engine-picker"[^>]*>(.*?)</select>', step3, re.S)
    assert m_eng is not None
    opts = re.findall(r'value="([^"]*)"', m_eng.group(1))
    assert opts == ["voxcpm", "gemini", "edge"]
    assert 'value="none"' not in step3

    # C3: Billing badge deleted; hint wrapped inside collapsed <details>
    assert 'id="recap-voice-billing-badge"' not in INDEX_HTML
    assert 'class="vcp-persona-head-row"' not in INDEX_HTML
    m_det = re.search(r'<details[^>]*id="recap-engine-billing-details"[^>]*>(.*?)</details>', step3, re.S)
    assert m_det is not None
    assert " open" not in m_det.group(0).split(">", 1)[0]
    assert 'id="recap-engine-billing-hint"' in m_det.group(1)

    # C4: Own-voice card single-line header (recap_own_voice_desc removed, body preserved)
    assert "recap_own_voice_desc" not in INDEX_HTML
    for oid in ("recap-own-voice-card", "recap-own-voice-body", "recap-own-voice-upload-btn", "recap-own-voice-input"):
        assert f'id="{oid}"' in step3


# ---------------------------------------------------------------------------
# Part D: Step 4 (review, facts, choices, button text, admin quality lock, split deleted)
# ---------------------------------------------------------------------------


def test_part_d_step4_review_facts_choices_button_and_split_deleted():
    step4 = _step4_slice()
    # D1: step header removed, title renamed MY & EN
    assert "recap-step-head" not in step4
    assert 'recap_step4_title: "စစ်ဆေးရန်"' in I18N_JS
    assert 'recap_step4_title: "Review"' in I18N_JS
    assert '<span class="recap-steps-nav-name" data-i18n="recap_step4_title">စစ်ဆေးရန်</span>' in INDEX_HTML

    # D2: Facts bar holds only format + duration (no chip pile)
    facts = _extract_block(step4, 'id="recap-step4-facts"', '<div class="recap-step4-choices"')
    assert 'id="recap-fact-canvas"' in facts
    assert 'id="recap-fact-duration"' in facts
    for deleted_fact in ("recap-fact-subs", "recap-fact-blur", "recap-fact-quality", "recap-fact-speed", "recap-fact-mirror", "recap-fact-title"):
        assert deleted_fact not in INDEX_HTML
        assert deleted_fact not in APP_JS
    assert "recap-fact-chip" not in step4

    # D3: Choices card with 3 label:value rows
    choices = _extract_block(step4, 'id="recap-step4-choices"', 'class="recap-step4-quote')
    assert choices.count('class="recap-choice-row"') == 3
    for cid in ("recap-step4-choice-title", "recap-step4-choice-voice", "recap-step4-choice-ratio"):
        assert f'id="{cid}"' in choices

    # D4: btn_start_recap drops credit suffix in both MY & EN while quote card stays intact
    assert 'btn_start_recap: "⚡ 1-Click Recap ဖန်တီးမည်"' in I18N_JS
    en_btn = re.findall(r'btn_start_recap:\s*"([^"]+)"', I18N_JS)
    assert len(en_btn) == 2
    assert "(1 Credit)" not in en_btn[0] and "(1 Credit)" not in en_btn[1]
    assert 'id="recap-step4-quote-credits"' in step4
    assert 'id="recap-step4-voice-panel"' in step4

    # D5: quality row defaults hidden in DOM & client gate is admin-only
    assert '<div class="style-selector-row mt-2 hidden" id="recap-quality-row">' in step4
    can_q = _extract_block(APP_JS, "function canUseRecapRenderQuality()", "function refreshRecapQualityLock()")
    assert "return !!u.is_admin;" in can_q
    assert "account_category" not in can_q

    # D6: all split-video UI & dead split helpers deleted, server default split_video=False
    for sid in (
        "recap-split-toggle",
        "recap-split-minutes-wrap",
        "recap-split-minutes-picker",
        "recap-split-cost-hint",
        "recap-split-note",
        "recap-step4-more-options",
    ):
        assert sid not in INDEX_HTML
    assert api_mod.RecapConfigRequest(url="https://example.com/a.mp4").split_video is False


@pytest.mark.asyncio
async def test_part_d5_backend_quality_lock_and_surcharge_admin_vs_non_admin(db_session, monkeypatch):
    # Direct RecapConfigRequest context validation (Round 10+12 validator)
    cfg_non_admin = api_mod.RecapConfigRequest.model_validate(
        {"render_quality": "max", "url": "https://example.com/a.mp4"},
        context={"is_admin": False},
    )
    assert cfg_non_admin.render_quality == "standard"

    cfg_admin = api_mod.RecapConfigRequest.model_validate(
        {"render_quality": "max", "url": "https://example.com/a.mp4"},
        context={"is_admin": True},
    )
    assert cfg_admin.render_quality == "max"

    # Clamping FREE & PURCHASED non-admin -> standard; admin -> keeps max/high
    purchased = SimpleNamespace(id=990101, account_category=AccountCategory.PURCHASED.value)
    free_user = SimpleNamespace(id=990102, account_category=AccountCategory.FREE.value)
    admin_user = SimpleNamespace(id=990103, account_category=AccountCategory.FREE.value)
    monkeypatch.setattr(settings, "admin_ids_raw", "990103")

    for q in ("high", "max"):
        assert api_mod._clamp_recap_render_quality(api_mod.RecapConfigRequest(url="https://example.com/a.mp4", render_quality=q), user=purchased) == "standard"
        assert api_mod._clamp_recap_render_quality(api_mod.RecapConfigRequest(url="https://example.com/a.mp4", render_quality=q), user=free_user) == "standard"
        assert api_mod._clamp_recap_render_quality(api_mod.RecapConfigRequest(url="https://example.com/a.mp4", render_quality=q), user_id=990101, is_vip=True) == "standard"
        assert api_mod._clamp_recap_render_quality(api_mod.RecapConfigRequest(url="https://example.com/a.mp4", render_quality=q), user=admin_user) == q
        assert api_mod._clamp_recap_render_quality(api_mod.RecapConfigRequest(url="https://example.com/a.mp4", render_quality=q), user_id=990103) == q

    # Execution choke point also clears max_quality_surcharge for non-admin jobs
    # even with tiered pricing enabled and a forged/legacy "max" config blob
    monkeypatch.setattr(api_mod, "_tiered_recap_pricing_enabled", lambda: True)
    for uid in (990101, 990103):
        await db_session.execute(delete(Job).where(Job.user_id == uid))
        await db_session.execute(delete(User).where(User.id == uid))
    await db_session.commit()

    db_session.add(
        User(
            id=990101,
            username="r12_vip",
            account_category=AccountCategory.PURCHASED.value,
            recap_credits=10.0,
            recap_credit_seconds=3600,
        )
    )
    await db_session.commit()

    forged_req = api_mod.RecapConfigRequest(render_quality="max", url="https://example.com/a.mp4")
    api_mod._clamp_recap_render_quality(forged_req, user_id=990101)
    assert forged_req.render_quality == "standard"
    assert api_mod._recap_max_quality_surcharge(forged_req.render_quality) == 0

    admin_req = api_mod.RecapConfigRequest(render_quality="max", url="https://example.com/a.mp4")
    api_mod._clamp_recap_render_quality(admin_req, user_id=990103)
    assert admin_req.render_quality == "max"
    assert api_mod._recap_max_quality_surcharge(admin_req.render_quality) == 1

    # Verify non-admin get_recap_eta_estimate clamps quality and surcharge helper honors admin-only clamp
    q_non_admin_entry = {"tiered_pricing": True, "max_quality_surcharge": api_mod._recap_max_quality_surcharge(forged_req.render_quality)}
    assert api_mod._entry_max_quality_surcharge(q_non_admin_entry) == 0


# ---------------------------------------------------------------------------
# Config-parity verification vs 759f98c
# ---------------------------------------------------------------------------


def test_round12_config_build_parity_vs_base_759f98c():
    """Verify Step 2/3 JSON config output is byte-identical to 759f98c,
    and Step 4 differs ONLY by non-admin render_quality='standard' & split_video=False."""
    try:
        base_js = subprocess.check_output(
            ["git", "show", "759f98c:app/webapp/static/js/app.js"],
            cwd=str(ROOT),
            stderr=subprocess.DEVNULL,
        ).decode("utf-8")
    except Exception:
        base_js = APP_JS

    def _build_harness(js_src: str) -> str:
        locked_and_extras = _extract_block(
            js_src,
            'const RECAP_LOCKED_FILL = "blur";',
            "function collectAccountPrefs()",
        )
        quality_fn = (
            _extract_block(js_src, "function canUseRecapRenderQuality()", "function refreshRecapQualityLock()")
            if "function canUseRecapRenderQuality()" in js_src
            else "function canUseRecapRenderQuality() { return true; }"
        )
        composite_fns = _extract_block(
            js_src,
            "function recapCompositePayload() {",
            "function scheduleRecapCompositeRefresh()",
        )
        start_recap = js_src.index("async function handleStartRecap()")
        payload_start = js_src.index("const sliderBlurY = document.getElementById", start_recap)
        payload_end = js_src.index('const res = await fetch("/api/recap/generate"', payload_start)
        payload_block = js_src[payload_start:payload_end]
        return f"""
{locked_and_extras}
{quality_fn}
function recapCompositeSourceUrl() {{ return state.recapVerifiedSourceUrl || null; }}
{composite_fns}
function buildSubmitPayload(url) {{
{payload_block}
    return payload;
}}
"""

    base_harness = _build_harness(base_js)
    curr_harness = _build_harness(APP_JS)

    node_script = f"""
const vm = require('vm');
const cases = [
    {{
        name: 'defaults_non_admin',
        url: 'https://example.com/movie.mp4',
        state: {{
            user: {{ is_admin: false, is_vip: false }},
            recapVideoTitle: 'Default Movie',
            recapVoice: 'komin',
            recapStoryStyle: 'pov',
            recapRatio: '9:16',
            recapSpeed: '+12%',
            recapBlurSubs: true,
            recapBlurBoxTouched: false,
            recapBlurBoxes: [],
            recapBlurAutoDetect: false,
            recapBurnSubs: true,
            recapSubPosBand: 'bottom',
            recapSubMargin: 5,
            recapSubStyle: 'cinematic',
            recapFontSize: 13,
            recapSubFont: 'padauk',
            recapLines: 1,
            recapLogo: true,
            recapLogoPos: 'top-right',
            recapLogoScale: 0.16,
            recapLogoOpacity: 0.70,
            recapSubRenderMode: 'timeline',
            recapSubEffect: 'none',
            recapEmbedSoftSubs: true,
            recapTextColor: '',
            recapOutlineColor: '',
            recapBgColor: '',
            recapOutlineWidth: null,
            recapBgOpacity: null,
            recapSubShadow: false,
            recapSubBold: true,
            recapSubFontCustomId: '',
            recapSourceDuration: 180,
            recapSourceLanguage: 'auto',
            recapRenderQuality: 'standard',
            recapTtsEngine: 'voxcpm',
            recapGeminiVoice: '',
            recapOwnVoiceSelected: false,
            recapOwnVoiceStatus: '',
            recapOwnVoiceRefId: null,
            recapSeekSec: 5,
            recapVerifiedSourceUrl: 'https://example.com/movie.mp4',
            uploadedVideoPath: null,
        }},
        dom: {{
            'slider-blur-y': {{ value: '85' }},
            'slider-blur-h': {{ value: '7' }},
            'slider-blur-x': {{ value: '4' }},
            'slider-blur-w': {{ value: '92' }},
            'slider-blur-strength': {{ value: '18' }},
            'slider-sub-y': {{ value: '5' }},
            'slider-sub-x': {{ value: '50' }},
            'slider-sub-w': {{ value: '92' }},
            'recap-script-review-toggle': {{ checked: false }},
        }}
    }},
    {{
        name: 'custom_step2_step3_admin_max',
        url: 'https://example.com/custom.mp4',
        state: {{
            user: {{ is_admin: true, is_vip: true }},
            recapVideoTitle: 'Custom Movie',
            recapVoice: 'mahnin',
            recapStoryStyle: 'dramatic',
            recapRatio: '16:9',
            recapSpeed: '+20%',
            recapBlurSubs: true,
            recapBlurBoxTouched: true,
            recapBlurBoxes: [
                {{ x: 10, y: 78, w: 80, h: 12, strength: 30 }},
                {{ x: 5, y: 15, w: 40, h: 8, strength: 24 }},
            ],
            recapBlurAutoDetect: true,
            recapBurnSubs: true,
            recapSubPosBand: 'bottom',
            recapSubMargin: 12,
            recapSubStyle: 'neon',
            recapFontSize: 19,
            recapSubFont: 'noto_sans',
            recapLines: 2,
            recapLogo: true,
            recapLogoPos: 'bottom-left',
            recapLogoScale: 0.28,
            recapLogoOpacity: 0.85,
            recapSubRenderMode: 'ass',
            recapSubEffect: 'fade',
            recapEmbedSoftSubs: false,
            recapTextColor: '#00dbff',
            recapOutlineColor: '#0066cc',
            recapBgColor: '#112233',
            recapOutlineWidth: 2.4,
            recapBgOpacity: 60,
            recapSubShadow: true,
            recapSubBold: false,
            recapSubFontCustomId: 'cf_123',
            recapSourceDuration: 240,
            recapSourceLanguage: 'zh',
            recapRenderQuality: 'max',
            recapTtsEngine: 'gemini',
            recapGeminiVoice: 'Kore',
            recapOwnVoiceSelected: false,
            recapOwnVoiceStatus: '',
            recapOwnVoiceRefId: null,
            recapSeekSec: 15,
            recapVerifiedSourceUrl: 'https://example.com/custom.mp4',
            uploadedVideoPath: null,
        }},
        dom: {{
            'slider-blur-y': {{ value: '78' }},
            'slider-blur-h': {{ value: '12' }},
            'slider-blur-x': {{ value: '10' }},
            'slider-blur-w': {{ value: '80' }},
            'slider-blur-strength': {{ value: '30' }},
            'slider-sub-y': {{ value: '12' }},
            'slider-sub-x': {{ value: '45' }},
            'slider-sub-w': {{ value: '84' }},
            'recap-script-review-toggle': {{ checked: true }},
        }}
    }},
    {{
        name: 'step4_non_admin_forged_high_and_split',
        url: 'https://example.com/vip.mp4',
        state: {{
            user: {{ is_admin: false, is_vip: true }},
            recapVideoTitle: 'VIP Movie',
            recapVoice: 'komin',
            recapStoryStyle: 'pov',
            recapRatio: '9:16',
            recapSpeed: '+12%',
            recapBlurSubs: true,
            recapBlurBoxTouched: false,
            recapBlurBoxes: [],
            recapBlurAutoDetect: false,
            recapBurnSubs: true,
            recapSubPosBand: 'bottom',
            recapSubMargin: 5,
            recapSubStyle: 'cinematic',
            recapFontSize: 13,
            recapSubFont: 'padauk',
            recapLines: 1,
            recapLogo: true,
            recapLogoPos: 'top-right',
            recapLogoScale: 0.16,
            recapLogoOpacity: 0.70,
            recapSubRenderMode: 'timeline',
            recapSubEffect: 'none',
            recapEmbedSoftSubs: true,
            recapTextColor: '',
            recapOutlineColor: '',
            recapBgColor: '',
            recapOutlineWidth: null,
            recapBgOpacity: null,
            recapSubShadow: false,
            recapSubBold: true,
            recapSubFontCustomId: '',
            recapSourceDuration: 180,
            recapSourceLanguage: 'auto',
            recapRenderQuality: 'high',
            recapTtsEngine: 'voxcpm',
            recapGeminiVoice: '',
            recapOwnVoiceSelected: false,
            recapOwnVoiceStatus: '',
            recapOwnVoiceRefId: null,
            recapSplitMinutes: 5,
            recapSeekSec: 5,
            recapVerifiedSourceUrl: 'https://example.com/vip.mp4',
            uploadedVideoPath: null,
        }},
        dom: {{
            'slider-blur-y': {{ value: '85' }},
            'slider-blur-h': {{ value: '7' }},
            'slider-blur-x': {{ value: '4' }},
            'slider-blur-w': {{ value: '92' }},
            'slider-blur-strength': {{ value: '18' }},
            'slider-sub-y': {{ value: '5' }},
            'slider-sub-x': {{ value: '50' }},
            'slider-sub-w': {{ value: '92' }},
            'recap-script-review-toggle': {{ checked: false }},
            'recap-split-toggle': {{ checked: true }},
        }}
    }},
];

function evalHarness(code, c) {{
    const ctx = {{
        window: {{}},
        state: JSON.parse(JSON.stringify(c.state)),
        document: {{
            getElementById: (id) => c.dom[id] || null,
            querySelector: () => null,
        }},
    }};
    vm.runInNewContext(code + '; globalThis.__out = {{ submit: buildSubmitPayload(' + JSON.stringify(c.url) + '), composite: ddCompositePayload() }};', ctx);
    return ctx.__out;
}}

const baseCode = {json.dumps(base_harness)};
const currCode = {json.dumps(curr_harness)};
console.log(JSON.stringify(cases.map(c => ({{
    name: c.name,
    base: evalHarness(baseCode, c),
    curr: evalHarness(currCode, c),
}}))));
"""
    proc = subprocess.run(["node", "-e", node_script], cwd=str(ROOT), capture_output=True, text=True, check=True)
    rows = {r["name"]: r for r in json.loads(proc.stdout)}

    for name in ("defaults_non_admin", "custom_step2_step3_admin_max"):
        assert rows[name]["curr"] == rows[name]["base"], f"Parity drift in {name}"

    s4 = rows["step4_non_admin_forged_high_and_split"]
    assert s4["curr"]["composite"] == s4["base"]["composite"]
    base_sub = s4["base"]["submit"]
    curr_sub = s4["curr"]["submit"]
    diff_keys = sorted(k for k in set(base_sub) | set(curr_sub) if base_sub.get(k) != curr_sub.get(k))
    assert diff_keys == ["render_quality", "split_minutes", "split_video"], diff_keys
    assert curr_sub["render_quality"] == "standard"
    assert curr_sub["split_video"] is False
    assert curr_sub["split_minutes"] == 3

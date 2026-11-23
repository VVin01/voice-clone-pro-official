"""Unit tests for Round 13 (Parts A-K): Recap Step 2/3/4 fixes, Voice/TTS tidy, and Bot /start rebuild."""
from __future__ import annotations

import io
import json
import re
import subprocess
import wave
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from aiogram.filters import CommandObject
from aiogram.types import CallbackQuery, Chat, Message, User as TgUser
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.handlers.menu import (
    handle_change_language,
    handle_menu_refer_earn,
    handle_user_language_toggle,
    render_main_menu,
)
from app.handlers.start import (
    handle_gate_verify,
    handle_set_language,
    handle_start,
)
from app.models.enums import AccountCategory
from app.models.schema import Base, Job, Referral, User
from app.services.ledger import get_or_create_user
from app.services.tts_service import create_tts_job
from app.ui.keyboards import main_menu_kb
from app.webapp.api import (
    RecapConfigRequest,
    _clamp_recap_voice_ref,
    _resolve_recap_edge_voice,
    _resolve_recap_voice_ref,
    upload_recap_voice_ref,
)

ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = (ROOT / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app" / "webapp" / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app" / "webapp" / "static" / "css" / "style.css").read_text(encoding="utf-8")


@pytest_asyncio.fixture
async def r13_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def _make_wav_bytes(duration_s: float = 1.0, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(rate * duration_s))
    return buf.getvalue()


def _node_eval_json(script: str) -> dict:
    proc = subprocess.run(
        ["node", "-e", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(ROOT),
    )
    return json.loads(proc.stdout.strip())


# ── Part A: Preview z-order & Subtitle resize handle ↔ width control parity ──


def test_part_a_preview_z_order_css_and_sub_handles():
    # Sub box must carry the 4 resize handles in DOM
    m = re.search(r'id="preview-sub-box"[\s\S]{0,700}', INDEX_HTML)
    assert m is not None
    sub_block = m.group(0)
    for edge in ("top", "left", "right", "bottom"):
        assert f'class="box-resize-handle {edge}"' in sub_block, f"Missing {edge} resize handle on #preview-sub-box"

    # Explicit recap-scoped CSS z-index: sub > blur
    sub_z = re.findall(r"#preview-sub-box\s*\{[^}]*z-index:\s*(\d+)", STYLE_CSS)
    blur_z = re.findall(r"#preview-blur-box\s*\{[^}]*z-index:\s*(\d+)", STYLE_CSS)
    assert sub_z, "Missing explicit z-index on #preview-sub-box"
    assert blur_z, "Missing explicit z-index on #preview-blur-box"
    assert int(sub_z[-1]) > int(blur_z[-1])


def test_part_a_sub_box_resize_parity_with_width_slider():
    """Handle-resizing the subtitle box must write the exact same state/config as setting slider-sub-w."""
    assert "function applySubBoxWidth(" in APP_JS
    assert "applySubBoxWidth(sliderSubW.value)" in APP_JS
    assert "applySubBoxWidth(startW + deltaPct * deltaScale * 2)" in APP_JS
    assert "sub_w: sliderSubW ? parseInt(sliderSubW.value) : 92" in APP_JS

    # Execute the exact width clamping & payload semantics in Node to prove byte-identical output
    res = _node_eval_json(
        r"""
        const fs = require("fs");
        const src = fs.readFileSync("app/webapp/static/js/app.js", "utf8");
        const m = src.match(/function applySubBoxWidth\(newW\) \{[\s\S]*?\n\s*\}/);
        if (!m) throw new Error("applySubBoxWidth not found");
        let sliderVal = "88";
        let labelVal = "88%";
        const state = { recapSubW: 88 };
        const sliderSubW = { get value() { return sliderVal; }, set value(v) { sliderVal = String(v); } };
        const valSubW = { set textContent(v) { labelVal = String(v); } };
        const document = {
          getElementById: (id) => {
            if (id === "slider-sub-w") return sliderSubW;
            if (id === "val-sub-w") return valSubW;
            return null;
          }
        };
        const syncSubBoxVisual = () => { valSubW.textContent = sliderSubW.value + "%"; };
        const updateSubtitleCharsEstimator = () => {};
        const saveRecapSettingsSoon = () => {};
        eval(m[0]);
        // Path 1: width slider sets 64
        applySubBoxWidth("64");
        const cfgFromSlider = { sub_w: parseInt(sliderSubW.value), sliderVal, labelVal };
        // Reset and Path 2: drag handle sets 64.2
        state.recapSubW = 88; sliderVal = "88"; labelVal = "88%";
        applySubBoxWidth(64.2);
        const cfgFromHandle = { sub_w: parseInt(sliderSubW.value), sliderVal, labelVal };
        console.log(JSON.stringify({ cfgFromSlider, cfgFromHandle }));
        """
    )
    assert res["cfgFromSlider"] == res["cfgFromHandle"] == {
        "sub_w": 64,
        "sliderVal": "64",
        "labelVal": "64%",
    }


# ── Part B & C: Ratio panel & .dd-acc-val suppression ──


def test_parts_b_and_c_ratio_and_accordion_val_suppression():
    for acc_id in ("recap-preview-ratio-group", "recap-story-style-acc", "recap-pace-acc"):
        assert re.search(rf'id="{acc_id}"[^>]*data-no-acc-val="1"', INDEX_HTML), (
            f"{acc_id} must carry data-no-acc-val='1'"
        )
    assert 'getAttribute("data-no-acc-val") === "1"' in APP_JS
    assert "repeat(2, minmax(0, 1fr))" in STYLE_CSS


# ── Part D & E: Engine labels, billing expander deleted, own voice ungated ──


def test_part_d_engine_labels_and_values():
    data = _node_eval_json(
        r"""
        const fs = require("fs");
        const code = fs.readFileSync("app/webapp/static/js/i18n.js", "utf8");
        const fn = new Function("localStorage", "window", code + "\nreturn translations;");
        const tr = fn({ getItem: () => "my", setItem: () => {} }, {});
        console.log(JSON.stringify({
          my: { voxcpm: tr.my.recap_engine_voxcpm, gemini: tr.my.recap_engine_gemini, edge: tr.my.recap_engine_edge },
          en: { voxcpm: tr.en.recap_engine_voxcpm, gemini: tr.en.recap_engine_gemini, edge: tr.en.recap_engine_edge },
          dead_my: [
            "recap_engine_hint_voxcpm", "recap_engine_hint_gemini", "recap_engine_hint_edge",
            "recap_gemini_auto_hint", "recap_persona_note_edge", "recap_step4_edge_voice_note",
            "recap_own_voice_locked_hint", "recap_own_voice_vip_cta", "srv_vip_clone_only",
            "recap_lang_acc_title", "recap_lang_sheet_btn", "recap_lang_sheet_title",
            "recap_review_acc_title", "recap_script_review_hint",
            "telegram_bg_assurance", "btn_start_recap_credits"
          ].filter(k => k in tr.my),
          dead_en: [
            "recap_engine_hint_voxcpm", "recap_engine_hint_gemini", "recap_engine_hint_edge",
            "recap_gemini_auto_hint", "recap_persona_note_edge", "recap_step4_edge_voice_note",
            "recap_own_voice_locked_hint", "recap_own_voice_vip_cta", "srv_vip_clone_only",
            "recap_lang_acc_title", "recap_lang_sheet_btn", "recap_lang_sheet_title",
            "recap_review_acc_title", "recap_script_review_hint",
            "telegram_bg_assurance", "btn_start_recap_credits"
          ].filter(k => k in tr.en),
        }));
        """
    )
    expected = {"voxcpm": "Voice Clone", "gemini": "Gemini TTS", "edge": "Edge TTS"}
    assert data["my"] == expected
    assert data["en"] == expected
    assert data["dead_my"] == []
    assert data["dead_en"] == []

    # Option values preserved in DOM
    assert '<option value="voxcpm"' in INDEX_HTML
    assert '<option value="gemini"' in INDEX_HTML
    assert '<option value="edge"' in INDEX_HTML
    # Billing expander & VIP own-voice locks removed from DOM
    for removed_id in (
        "recap-engine-billing-details",
        "recap-engine-billing-hint",
        "recap-persona-engine-note",
        "recap-own-voice-vip-tag",
        "recap-own-voice-lock-badge",
        "recap-own-voice-locked-hint",
        "btn-recap-own-voice-vip",
    ):
        assert f'id="{removed_id}"' not in INDEX_HTML


@pytest.mark.asyncio
async def test_part_e3_own_voice_ungated_for_free_user_and_ownership_strict(r13_session, tmp_path, monkeypatch):
    # Create FREE user A and user B
    user_a, _ = await get_or_create_user(r13_session, telegram_id=910001, username="free_a", first_name="FreeA")
    user_b, _ = await get_or_create_user(r13_session, telegram_id=910002, username="free_b", first_name="FreeB")
    assert user_a.account_category == AccountCategory.FREE.value
    await r13_session.commit()

    monkeypatch.setattr(settings, "storage_root", tmp_path)

    # Mock UploadFile for FREE user A (chunked stream ending with empty bytes)
    wav_bytes = _make_wav_bytes(1.0)
    fake_file = MagicMock()
    fake_file.filename = "my_voice.wav"
    fake_file.read = AsyncMock(side_effect=[wav_bytes, b""])

    resp = await upload_recap_voice_ref(reference_audio=fake_file, user=user_a, _gate=user_a)
    assert resp["success"] is True
    ref_id = resp["ref_id"]
    assert ref_id.startswith(f"webapp_ref_u{user_a.id}_")

    # Owner A resolves their own ref successfully
    resolved = _resolve_recap_voice_ref(ref_id, user_a)
    assert resolved.is_file()

    # _clamp_recap_voice_ref preserves voice_ref_id & voice_ref_path for FREE user in voxcpm mode, clears in non-voxcpm
    req_vox = RecapConfigRequest(url="https://example.com/v.mp4", tts_engine="voxcpm", voice_ref_id=ref_id, voice_ref_path=str(resolved))
    req_edge = RecapConfigRequest(url="https://example.com/v.mp4", tts_engine="edge", voice_ref_id=ref_id, voice_ref_path=str(resolved))
    req_gem = RecapConfigRequest(url="https://example.com/v.mp4", tts_engine="gemini", voice_ref_id=ref_id, voice_ref_path=str(resolved))
    assert _clamp_recap_voice_ref(req_vox, user=user_a) == str(resolved)
    assert req_vox.voice_ref_id == ref_id
    assert _clamp_recap_voice_ref(req_edge, user=user_a) is None
    assert req_edge.voice_ref_id is None
    assert _clamp_recap_voice_ref(req_gem, user=user_a) is None
    assert req_gem.voice_ref_id is None

    # User B attempting to use User A's ref gets 403 Forbidden
    with pytest.raises(HTTPException) as exc_other:
        _resolve_recap_voice_ref(ref_id, user_b)
    assert exc_other.value.status_code == 403

    # Missing file on disk for User A's own ref pattern gets 404 Not Found
    with pytest.raises(HTTPException) as exc_missing:
        _resolve_recap_voice_ref(f"webapp_ref_u{user_a.id}_deadbeef1234", user_a)
    assert exc_missing.value.status_code == 404


# ── Part F: Edge voice fidelity across TTS & Recap tabs ──


@pytest.mark.asyncio
async def test_part_f_edge_voice_fidelity_tts_and_recap(r13_session):
    user, _ = await get_or_create_user(r13_session, telegram_id=910003, username="edge_user", first_name="Edge")
    await r13_session.commit()

    # 1) TTS tab: short key "andrew" AND full neural name both resolve to en-US-AndrewMultilingualNeural
    job_short, err1 = await create_tts_job(
        r13_session,
        user_id=user.id,
        text="မင်္ဂလာပါ စမ်းသပ်နေပါသည်",
        engine="edge",
        voice="andrew",
        speed_rate="+0%",
    )
    assert err1 == "QUEUED" and job_short is not None
    assert job_short.edge_voice == "en-US-AndrewMultilingualNeural"

    # Mark first job done so concurrent limit doesn't block second
    job_short.status = "SUCCESS"
    await r13_session.commit()

    job_full, err2 = await create_tts_job(
        r13_session,
        user_id=user.id,
        text="မင်္ဂလာပါ ဒုတိယ စမ်းသပ်ချက်",
        engine="edge",
        voice="en-US-AvaMultilingualNeural",
        speed_rate="+0%",
    )
    assert err2 == "QUEUED" and job_full is not None
    assert job_full.edge_voice == "en-US-AvaMultilingualNeural"

    # 2) Recap tab: RecapConfigRequest carries edge_voice and _resolve_recap_edge_voice honors explicit selection
    req_ava = RecapConfigRequest(url="https://example.com/v.mp4", tts_engine="edge", voice_slug="komin", edge_voice="ava")
    assert req_ava.edge_voice == "ava"
    assert _resolve_recap_edge_voice(req_ava.edge_voice, p_info={"edge_voice": "my-MM-ThihaNeural"}) == "en-US-AvaMultilingualNeural"
    # Even when female persona is passed in p_info, explicit male Edge selection "brian" wins!
    req_brian = RecapConfigRequest(url="https://example.com/v.mp4", tts_engine="edge", voice_slug="mahnin", edge_voice="brian")
    assert _resolve_recap_edge_voice(req_brian.edge_voice, p_info={"edge_voice": "my-MM-NilarNeural"}, is_female=True) == "en-US-BrianMultilingualNeural"
    # Fallback to persona only when edge_voice is omitted
    assert _resolve_recap_edge_voice(None, p_info={"edge_voice": "my-MM-NilarNeural"}, is_female=True) == "my-MM-NilarNeural"
    assert _resolve_recap_edge_voice(None, p_info={"edge_voice": "my-MM-ThihaNeural"}, is_female=False) == "my-MM-ThihaNeural"

    # 3) Single-source Edge voice list + Recap Edge picker DOM present
    assert 'id="recap-edge-voice-select"' in INDEX_HTML
    assert 'id="btn-recap-edge-preview"' in INDEX_HTML
    assert "VCP_EDGE_VOICES" in APP_JS
    assert "populateEdgeVoiceSelects" in APP_JS
    assert 'edge_voice: activeEngine === "edge"' in APP_JS


def test_round13_recap_config_payload_locks_and_sanctioned_deltas():
    """Round 10/12 locks stay untouched; buildRecapConfig carries sanctioned Round 13 deltas cleanly."""
    res = _node_eval_json(
        r"""
        const fs = require("fs");
        const src = fs.readFileSync("app/webapp/static/js/app.js", "utf8");
        const start = src.indexOf("function buildRecapConfig(url)");
        const end = src.indexOf('if (typeof window !== "undefined") window.buildRecapConfig', start);
        const body = src.slice(start, end) + "\nreturn buildRecapConfig(url);";
        const RECAP_LOCKED_FILL = "blur";
        const RECAP_LOCKED_MIRROR = true;
        const RECAP_LOCKED_ZOOM = 1.10;
        const RECAP_LOCKED_PROTECTION = "strong";
        const recapLockedCropForRatio = (r) => r === "9:16"
          ? { crop_top: 12, crop_bottom: 12, crop_left: 0, crop_right: 0 }
          : { crop_top: 0, crop_bottom: 0, crop_left: 0, crop_right: 0 };
        const normalizeEdgeVoiceKey = (v) => String(v || "thiha").trim().toLowerCase();
        const ddConfigExtras = () => ({});
        const canUseRecapCloneEngine = () => true;
        const canUseRecapRenderQuality = () => false;
        const run = (overrides) => {
          const state = Object.assign({
            recapVideoTitle: "Sample", recapVoice: "komin", recapStoryStyle: "pov",
            recapRatio: "9:16", recapSpeed: "+12%", recapBlurSubs: true,
            recapBlurBoxTouched: false, recapBlurBoxes: [], recapBlurAutoDetect: true,
            recapBurnSubs: true, recapSubPosBand: "bottom", recapSubMargin: 5,
            recapSubStyle: "cinematic", recapFontSize: 13, recapSubFont: "padauk",
            recapLines: 1, recapLogo: true, recapLogoPos: "top-right",
            recapLogoScale: 0.16, recapLogoOpacity: 0.70, recapVideoDuration: 120,
            recapScriptReview: false, recapRenderQuality: "max",
            recapTtsEngine: "voxcpm", recapGeminiVoice: "", recapEdgeVoice: "ava",
            recapVoiceRefId: null,
          }, overrides);
          const dom = {
            "slider-blur-y": { value: "85" }, "slider-blur-h": { value: "7" },
            "slider-blur-x": { value: "4" }, "slider-blur-w": { value: "92" },
            "slider-blur-strength": { value: "18" }, "slider-sub-y": { value: "5" },
            "slider-sub-x": { value: "50" }, "slider-sub-w": { value: "70" },
            "recap-persona-select": { value: state.recapVoice },
            "recap-edge-voice-select": { value: state.recapEdgeVoice },
            "recap-gemini-voice-select": { value: state.recapGeminiVoice },
          };
          const fn = new Function(
            "url", "state", "document", "RECAP_LOCKED_FILL", "RECAP_LOCKED_MIRROR",
            "RECAP_LOCKED_ZOOM", "RECAP_LOCKED_PROTECTION", "recapLockedCropForRatio",
            "normalizeEdgeVoiceKey", "ddConfigExtras", "canUseRecapCloneEngine",
            "canUseRecapRenderQuality", body
          );
          return fn(
            "https://example.com/v.mp4", state, { getElementById: (id) => dom[id] || null },
            RECAP_LOCKED_FILL, RECAP_LOCKED_MIRROR, RECAP_LOCKED_ZOOM,
            RECAP_LOCKED_PROTECTION, recapLockedCropForRatio, normalizeEdgeVoiceKey,
            ddConfigExtras, canUseRecapCloneEngine, canUseRecapRenderQuality
          );
        };
        console.log(JSON.stringify({
          vox_own: run({ recapTtsEngine: "voxcpm", recapOwnVoiceSelected: true, recapOwnVoiceStatus: "ready", recapOwnVoiceRefId: "webapp_ref_u1_abc" }),
          vox_persona: run({ recapTtsEngine: "voxcpm", recapOwnVoiceSelected: false, recapVoice: "mahnin", recapOwnVoiceStatus: "ready", recapOwnVoiceRefId: "webapp_ref_u1_abc" }),
          edge: run({ recapTtsEngine: "edge", recapEdgeVoice: "ava" }),
        }));
        """
    )
    vox = res["vox_own"]
    vox_persona = res["vox_persona"]
    edge = res["edge"]
    # Round 10 locks intact
    assert vox["ratio_fill"] == "blur"
    assert vox["crop_top"] == 12 and vox["crop_bottom"] == 12
    assert vox["mirror_video"] is True
    assert vox["zoom_factor"] == 1.10
    assert 'RECAP_LOCKED_PROTECTION = "strong"' in APP_JS
    # Round 12 locks intact
    assert vox["render_quality"] == "standard"
    assert vox["split_video"] is False
    # Round 13 sanctioned fields (Part E2 own vs persona parity + Edge fidelity)
    assert vox["source_language"] == "auto"
    assert vox["sub_w"] == 70
    assert vox["voice_ref_id"] == "webapp_ref_u1_abc"
    assert vox["edge_voice"] is None
    assert vox_persona["voice_ref_id"] is None
    assert vox_persona["voice_slug"] == "mahnin"
    assert edge["edge_voice"] == "ava"
    assert edge["voice_ref_id"] is None


# ── Parts G, H, I, J: Style labels, Speed row, Language deleted, Script switch, Step 4 & Label tidy ──


def test_parts_g_h_i_j_ui_and_i18n_contracts():
    tr = _node_eval_json(
        r"""
        const fs = require("fs");
        const code = fs.readFileSync("app/webapp/static/js/i18n.js", "utf8");
        const fn = new Function("localStorage", "window", code + "\nreturn translations;");
        console.log(JSON.stringify(fn({ getItem: () => "my", setItem: () => {} }, {})));
        """
    )

    # Part G: story_style labels renamed, internal values preserved
    assert tr["my"]["style_viral"] == "စွဲဆောင်"
    assert tr["my"]["style_funny"] == "ဟာသ"
    assert tr["my"]["style_standard"] == "ဇာတ်လမ်းပြော"
    assert tr["my"]["style_pov"] == "ကိုယ်တွေ့"
    assert tr["en"]["style_viral"] == "Viral"
    assert tr["en"]["style_funny"] == "Comedy"
    assert tr["en"]["style_standard"] == "Recap"
    assert tr["en"]["style_pov"] == "Dubbing"
    for val in ("viral", "funny", "standard", "pov"):
        assert f'data-story="{val}"' in INDEX_HTML
    assert 'data-story="dramatic"' not in INDEX_HTML

    # Part H1: Speed row emoji-free title + clean 5-col grid without broken sliding capsule
    assert tr["my"]["recap_pace_acc_title"] == "ဇာတ်လမ်းပြောနှုန်း"
    assert tr["en"]["recap_pace_acc_title"] == "Narration Pace"
    assert "recap-speed-grid" in INDEX_HTML
    m_speed = re.search(r'id="recap-speed-picker"[\s\S]{0,900}', INDEX_HTML)
    assert m_speed is not None and "speed-capsule-pill" not in m_speed.group(0)

    # Part H2: Language panel & sheet deleted; config still sends source_language: "auto"
    assert 'id="recap-lang-acc"' not in INDEX_HTML
    assert 'id="recap-lang-sheet-backdrop"' not in INDEX_HTML
    assert "openRecapLangSheet" not in APP_JS
    assert 'source_language: "auto"' in APP_JS

    # Part H3: Script review standalone switch row driving #recap-script-review-toggle
    assert 'id="recap-review-acc"' not in INDEX_HTML
    assert 'id="recap-review-switch-row"' in INDEX_HTML
    assert 'id="recap-script-review-toggle"' in INDEX_HTML
    assert tr["my"]["recap_script_review_label"] == "Script ကို အရင် စစ်မည်"
    assert tr["en"]["recap_script_review_label"] == "Review script first"

    # Part I: Step 4 clean-up
    assert tr["my"]["recap_output_caption"] == "Output နမူနာပါ — မကြိုက်တာ ရှိရင် အဆင့် ၂ မှာ ပြန်ပြင်နိုင်ပါတယ်"
    assert tr["en"]["recap_output_caption"] == "This is a sample of the output — if anything looks off, you can adjust it in Step 2"
    assert 'id="recap-step4-facts"' not in INDEX_HTML
    assert "telegram_bg_assurance" not in INDEX_HTML
    assert tr["my"]["btn_start_recap"] == "ဖန်တီးမည်"
    assert tr["en"]["btn_start_recap"] == "Create"
    assert "recap-output-caption-card" in INDEX_HTML

    # Part I3: _cleanStep4VoiceName strips emojis (via i18n.js stripPanelEmoji) and parentheticals
    step4_check = _node_eval_json(
        r"""
        const fs = require("fs");
        const i18nSrc = fs.readFileSync("app/webapp/static/js/i18n.js", "utf8");
        const mStrip = i18nSrc.match(/function stripPanelEmoji\(str\) \{[\s\S]*?\n\}/);
        if (!mStrip) throw new Error("stripPanelEmoji not found");
        eval(mStrip[0]);
        const src = fs.readFileSync("app/webapp/static/js/app.js", "utf8");
        const m = src.match(/function _cleanStep4VoiceName\(raw\) \{[\s\S]*?\n\}/);
        if (!m) throw new Error("_cleanStep4VoiceName not found");
        eval(m[0]);
        console.log(JSON.stringify({
          komin: _cleanStep4VoiceName("ကိုမင်း (Ko Min) 🔥"),
          thiha: _cleanStep4VoiceName("ကိုသီဟ (ကျား)"),
          own: _cleanStep4VoiceName("🎙️ ကိုယ်ပိုင်အသံ"),
          en_thiha: _cleanStep4VoiceName("Thiha (M)"),
        }));
        """
    )
    assert step4_check == {
        "komin": "ကိုမင်း",
        "thiha": "ကိုသီဟ",
        "own": "ကိုယ်ပိုင်အသံ",
        "en_thiha": "Thiha",
    }

    # Part J: TTS / Voice tab label tidy
    assert tr["my"]["tts_engine_gemini"] == "Gemini"
    assert tr["my"]["tts_engine_edge"] == "Edge"
    assert tr["en"]["tts_engine_gemini"] == "Gemini"
    assert tr["en"]["tts_engine_edge"] == "Edge"
    assert tr["my"]["btn_mode_curated"] == "Persona"
    assert tr["my"]["btn_mode_custom"] == "ကိုယ်ပိုင်အသံ"
    assert tr["en"]["btn_mode_curated"] == "Persona"
    assert tr["en"]["btn_mode_custom"] == "My Voice"
    assert tr["my"]["btn_generate"] == "အသံ ထုတ်မည်"
    assert tr["my"]["tts_generate_btn"] == "အသံ ထုတ်မည်"
    assert tr["en"]["btn_generate"] == "Generate Voice"
    assert tr["en"]["tts_generate_btn"] == "Generate Audio"


# ── Part K: Bot /start rebuild ──


def test_part_k_main_menu_kb_always_exact_3_inline_buttons():
    for lang, expected_labels in (
        ("my", ["🚀 Mini App ဖွင့်မည်", "🎁 မိတ်ဆက်ရန်", "🌐 ဘာသာစကား ပြောင်းမည်"]),
        ("en", ["🚀 Open Mini App", "🎁 Refer Friends", "🌐 Change Language"]),
    ):
        for is_admin in (False, True):
            for pending in (False, True):
                kb = main_menu_kb(is_admin=is_admin, lang=lang, membership_pending=pending)
                assert len(kb.inline_keyboard) == 3
                assert all(len(row) == 1 for row in kb.inline_keyboard)
                assert [row[0].text for row in kb.inline_keyboard] == expected_labels
                assert kb.inline_keyboard[0][0].web_app is not None
                assert kb.inline_keyboard[1][0].callback_data == "menu:refer_earn"
                assert kb.inline_keyboard[2][0].callback_data == "menu:change_lang"


def _make_fake_bot(is_member: bool = True):
    bot = MagicMock()
    member = MagicMock()
    member.status = "member" if is_member else "left"
    member.is_member = is_member
    bot.get_chat_member = AsyncMock(return_value=member)
    bot_me = MagicMock()
    bot_me.username = "voiceclonepro_bot"
    bot.get_me = AsyncMock(return_value=bot_me)
    bot.send_message = AsyncMock()
    return bot


def _make_start_msg(uid: int, bot, first_name: str = "User", username: str = "u"):
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=uid, is_bot=False, first_name=first_name, username=username)
    msg.chat = Chat(id=uid, type="private")
    msg.bot = bot
    msg.answer = AsyncMock()
    msg.delete = AsyncMock()
    return msg


def _make_callback(uid: int, data: str, bot):
    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=uid, is_bot=False, first_name="User", username="u")
    cb.data = data
    cb.bot = bot
    cb.answer = AsyncMock()
    cb.message = MagicMock()
    cb.message.chat = Chat(id=uid, type="private")
    cb.message.answer = AsyncMock()
    cb.message.delete = AsyncMock()
    cb.message.edit_text = AsyncMock()
    return cb


@pytest.mark.asyncio
async def test_part_k_first_run_order_ephemeral_deletion_and_referral_attribution(r13_session, monkeypatch):
    monkeypatch.setattr(settings, "required_channel_id", "-100111")
    monkeypatch.setattr(settings, "required_group_id", "-100222")
    monkeypatch.setattr(settings, "channel_invite_link", "https://t.me/VoiceClonePro")
    monkeypatch.setattr(settings, "group_invite_link", "https://t.me/VoiceCloneProGroup")
    monkeypatch.setattr(settings, "maintenance_mode", False)

    # Create referrer first
    referrer, _ = await get_or_create_user(r13_session, telegram_id=920001, username="referrer", first_name="Ref")
    await r13_session.commit()

    # Step 1: Brand-new user sends /start <referrer_code> -> referral attributed FIRST, language picker shown
    bot_not_joined = _make_fake_bot(is_member=False)
    msg = _make_start_msg(920002, bot_not_joined, first_name="Newbie", username="newbie")
    cmd = CommandObject(prefix="/", command="start", args=referrer.referral_code)
    await handle_start(msg, command=cmd, session=r13_session)

    # Referral must already be recorded before language or gate
    ref_row = (await r13_session.execute(select(Referral).where(Referral.referred_id == 920002))).scalar_one_or_none()
    assert ref_row is not None and ref_row.referrer_id == referrer.id

    # Language picker sent with set_lang: callbacks
    msg.answer.assert_awaited_once()
    picker_kb = msg.answer.await_args.kwargs["reply_markup"]
    picker_cbs = [btn.callback_data for row in picker_kb.inline_keyboard for btn in row]
    assert picker_cbs == ["set_lang:my", "set_lang:en"]

    # Step 2: User taps set_lang:my -> picker message DELETED, join gate shown (2 join links + gate:verify)
    cb_lang = _make_callback(920002, "set_lang:my", bot_not_joined)
    await handle_set_language(cb_lang, session=r13_session)
    cb_lang.message.delete.assert_awaited_once()
    cb_lang.message.answer.assert_awaited_once()
    gate_kb = cb_lang.message.answer.await_args.kwargs["reply_markup"]
    assert gate_kb.inline_keyboard[0][0].url == "https://t.me/VoiceClonePro"
    assert gate_kb.inline_keyboard[1][0].url == "https://t.me/VoiceCloneProGroup"
    assert gate_kb.inline_keyboard[2][0].callback_data == "gate:verify"

    # Step 3: User joins both and taps gate:verify -> gate message DELETED, exact Welcome + 3 buttons shown
    bot_joined = _make_fake_bot(is_member=True)
    cb_gate = _make_callback(920002, "gate:verify", bot_joined)
    await handle_gate_verify(cb_gate, session=r13_session)
    cb_gate.message.delete.assert_awaited_once()
    cb_gate.message.answer.assert_awaited_once()
    welcome_text = cb_gate.message.answer.await_args.args[0]
    welcome_kb = cb_gate.message.answer.await_args.kwargs["reply_markup"]
    assert "Voice Clone Pro မှ ကြိုဆိုပါတယ်" in welcome_text
    assert 'စတင်ရန် "Mini App ဖွင့်မည်" ကို နှိပ်ပါ။' in welcome_text
    assert len(welcome_kb.inline_keyboard) == 3

    # Step 4: Returning member runs /start again -> skips language picker AND skips gate -> Welcome directly!
    msg_ret = _make_start_msg(920002, bot_joined)
    await handle_start(msg_ret, command=CommandObject(prefix="/", command="start"), session=r13_session)
    msg_ret.answer.assert_awaited_once()
    assert "Voice Clone Pro မှ ကြိုဆိုပါတယ်" in msg_ret.answer.await_args.args[0]
    assert len(msg_ret.answer.await_args.kwargs["reply_markup"].inline_keyboard) == 3

    # Step 5: Ex-member leaves channel/group and runs /start -> skips language picker, re-gated!
    msg_left = _make_start_msg(920002, bot_not_joined)
    await handle_start(msg_left, command=CommandObject(prefix="/", command="start"), session=r13_session)
    msg_left.answer.assert_awaited_once()
    re_gate_kb = msg_left.answer.await_args.kwargs["reply_markup"]
    assert re_gate_kb.inline_keyboard[2][0].callback_data == "gate:verify"


@pytest.mark.asyncio
async def test_part_k_refer_button_and_change_language_flow(r13_session, monkeypatch):
    monkeypatch.setattr(settings, "maintenance_mode", False)
    user, _ = await get_or_create_user(r13_session, telegram_id=920010, username="alice", first_name="Alice")
    user.language_code = "my"
    await r13_session.commit()

    bot = _make_fake_bot(is_member=True)

    # 1) Refer button sends a NEW non-ephemeral message (NOT edit, NOT deleted) with user's code + Part I terms + Share URL button
    cb_ref = _make_callback(920010, "menu:refer_earn", bot)
    await handle_menu_refer_earn(cb_ref, session=r13_session)
    cb_ref.message.delete.assert_not_called()
    cb_ref.message.edit_text.assert_not_called()
    cb_ref.message.answer.assert_awaited_once()
    ref_text = cb_ref.message.answer.await_args.args[0]
    ref_kb = cb_ref.message.answer.await_args.kwargs["reply_markup"]
    expected_link = f"https://t.me/voiceclonepro_bot?start={user.referral_code}"
    assert expected_link in ref_text
    assert "Voice Clone ၁ မိနစ် — အကန့်အသတ် မရှိပါ။" in ref_text
    assert "ဖိတ်ထားသူ:" in ref_text
    assert len(ref_kb.inline_keyboard) == 1
    assert len(ref_kb.inline_keyboard[0]) == 1
    assert ref_kb.inline_keyboard[0][0].text == "📤 မျှဝေမည်"
    assert ref_kb.inline_keyboard[0][0].url.startswith("https://t.me/share/url?url=")

    # 2) Change Language button -> opens user_lang: picker -> picking EN deletes picker & sends fresh EN Welcome
    cb_chg = _make_callback(920010, "menu:change_lang", bot)
    await handle_change_language(cb_chg, session=r13_session)
    cb_chg.message.answer.assert_awaited_once()
    chg_kb = cb_chg.message.answer.await_args.kwargs["reply_markup"]
    assert [b.callback_data for row in chg_kb.inline_keyboard for b in row] == ["user_lang:my", "user_lang:en"]

    cb_pick = _make_callback(920010, "user_lang:en", bot)
    await handle_user_language_toggle(cb_pick, session=r13_session)
    cb_pick.message.delete.assert_awaited_once()
    cb_pick.message.answer.assert_awaited_once()
    en_welcome = cb_pick.message.answer.await_args.args[0]
    en_kb = cb_pick.message.answer.await_args.kwargs["reply_markup"]
    assert "Welcome to Voice Clone Pro" in en_welcome
    assert 'Tap "Open Mini App" to start.' in en_welcome
    assert [r[0].text for r in en_kb.inline_keyboard] == [
        "🚀 Open Mini App",
        "🎁 Refer Friends",
        "🌐 Change Language",
    ]

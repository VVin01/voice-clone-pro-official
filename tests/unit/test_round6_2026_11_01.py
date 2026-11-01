"""Cycle round-6 fix batch (branch round6-fixes-2026-11-01).

Round 5 was a findings round over the whole bot; this batch fixes
its defect list, headed by the one HIGH:

P1  In-app playback/download of generated results was broken (401):
    media elements and anchors got raw API URLs, which cannot carry
    the X-Telegram-Init-Data header. All result media now flows
    through authenticated fetch -> blob object URLs.
P3  History never rendered the API's fallback_used flag.
P4  The "voice ready" floating alert squeezed its text into a
    ~100px word-per-line column.
P5  character_limit_free (live override 500) was invisible and
    uneditable in Admin — no GET key, no payload field, no UI field.
P6  Raw-English pipeline status lines leaked into the Burmese UI at
    the ingest seams (no canonical stage -> client showed the raw
    progress_label).
P7  EN mode leaked Burmese on Recap step 1 (hardcoded tab/drop-zone
    strings; analyze chips baked in the analyze-time language).
Copy batch: E1/E2/E3/E4/E9/E10/E11/E16/E17 + V2 (doubled 📱).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")
API_PY = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")
DOWNLOADER_PY = (ROOT / "app/services/downloader.py").read_text(encoding="utf-8")
CURATED_PY = (ROOT / "app/services/curated_voices.py").read_text(encoding="utf-8")


def _translations() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    script = """
global.localStorage = { getItem: () => null, setItem: () => {} };
global.document = { querySelectorAll: () => [], documentElement: {}, body: { classList: { toggle: () => {} } }, getElementById: () => null };
global.window = {};
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const translations = eval(src + '; translations;');
console.log(JSON.stringify(translations));
"""
    out = subprocess.run(
        [node, "-e", script, str(ROOT / "app/webapp/static/js/i18n.js")],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr[-500:]
    return json.loads(out.stdout)


# ── P1: authenticated result media ──────────────────────────────────

def test_p1_auth_media_helpers_exist():
    for marker in (
        "async function fetchAuthBlobUrl(url)",
        "async function setAuthMediaSource(el, url)",
        "function clearAuthMediaSource(el)",
        "async function downloadAuthFile(url, filename)",
        "async function openAuthFile(url)",
        'closest("a[data-dl-url], a[data-open-url]")',
        "URL.revokeObjectURL(prev)",
    ):
        assert marker in APP_JS, marker


def test_p1_no_raw_result_media_urls_remain():
    for bad in (
        "audioPlayer.src = data.output_audio_url",
        "btnDl.href = data.output_audio_url",
        "player.src = data.download_urls.video",
        ".href = data.download_urls.",
        "a.href = p.url",
        'id="admin-modal-video" src="',
        'id="admin-modal-audio" src="',
        'href="${j.deliverables.video}"',
        'href="${j.deliverables.audio}"',
        'href="${j.download_url}"',
        'href="${deliverables.video}"',
        'href="${downloadUrl}"',
        'href="${dlUrl}"',
    ):
        assert bad not in APP_JS, bad


def test_p1_download_buttons_use_data_dl_url():
    # Recap completion buttons + history rows + admin modal chips.
    assert APP_JS.count('setAttribute("data-dl-url", data.download_urls.') == 12
    assert 'data-dl-url="${j.deliverables.video}"' in APP_JS
    assert 'data-dl-url="${j.download_url}"' in APP_JS
    assert 'data-open-url="${j.download_url}"' in APP_JS


def test_p1_blob_flow_behaviour():
    """Run the real helper block under node with stubbed fetch/DOM:
    the fetch must carry the auth header, the player must receive a
    blob: URL, a replaced source must revoke the previous URL, and a
    download must click a temporary anchor with the blob URL."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    start = APP_JS.index("// ── Authenticated result media (round-6 P1)")
    end = APP_JS.index("window.openAuthFile = openAuthFile;")
    helpers = APP_JS[start:end]
    script = """
const calls = { fetch: [], revoked: [], clicks: [] };
let blobSeq = 0;
global.URL.createObjectURL = (blob) => "blob:fake-" + (++blobSeq);
global.URL.revokeObjectURL = (u) => { calls.revoked.push(u); };
global.fetch = async (url, opts) => {
    calls.fetch.push({ url, headers: opts && opts.headers });
    return { ok: true, status: 200, blob: async () => ({ type: "audio/wav" }) };
};
function getAuthHeaders(json) { return { "X-Telegram-Init-Data": "INIT" }; }
function isAuthFailure(res) { return false; }
function haptic() {}
function showErrorToast(e, kind) { throw new Error("toast:" + kind); }
global.document = {
    createElement: (tag) => ({ tag, click() { calls.clicks.push({ href: this.href, download: this.download }); }, remove() {} }),
    body: { appendChild() {} },
    addEventListener() {},
};
const window = {};
""" + helpers + """
(async () => {
    const el = { src: null, removeAttribute() {}, load() {} };
    const ok1 = await setAuthMediaSource(el, "/api/audio/download/job1");
    const first = el.src;
    const ok2 = await setAuthMediaSource(el, "/api/audio/download/job2");
    await downloadAuthFile("/api/audio/download/job1", "voice_clone.wav");
    console.log(JSON.stringify({
        ok1, ok2, first, second: el.src,
        fetchHeaders: calls.fetch.map(c => c.headers),
        revoked: calls.revoked,
        clicks: calls.clicks,
    }));
})().catch(e => { console.error(String(e)); process.exit(1); });
"""
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-800:]
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["ok1"] is True and res["ok2"] is True
    assert res["first"] == "blob:fake-1" and res["second"] == "blob:fake-2"
    assert res["revoked"] == ["blob:fake-1"]  # replaced source revoked
    assert all(h == {"X-Telegram-Init-Data": "INIT"} for h in res["fetchHeaders"])
    assert res["clicks"] and res["clicks"][0]["download"] == "voice_clone.wav"
    assert res["clicks"][0]["href"].startswith("blob:")


# ── P3: History fallback indicator ──────────────────────────────────

def test_p3_history_renders_fallback_indicator():
    assert 't("hist_fallback_badge")' in APP_JS
    assert "hub-fallback-badge" in APP_JS and "hub-fallback-note" in APP_JS
    assert 'tf("recap_fallback_note"' in APP_JS
    assert 't("voice_fallback_notice")' in APP_JS
    assert ".hub-fallback-badge" in STYLE_CSS and ".hub-fallback-note" in STYLE_CSS
    # The note also carries .history-item-body (later dark-text rule);
    # the compound rule must outrank it so the warning reads amber.
    assert ".history-item-body.hub-fallback-note" in STYLE_CSS
    tr = _translations()
    assert tr["my"]["hist_fallback_badge"] and tr["en"]["hist_fallback_badge"]


def test_p3_api_history_still_returns_fallback_fields():
    assert '"fallback_used": _fb_used' in API_PY
    assert '"fallback_segments": _fb_segs' in API_PY


# ── P4: floating alert layout ───────────────────────────────────────

def test_p4_alert_card_wraps_instead_of_squeezing():
    card = STYLE_CSS.index(".floating-alert-card {")
    block = STYLE_CSS[card:card + 700]
    assert "flex-wrap: wrap" in block
    body = STYLE_CSS.index(".floating-alert-body {")
    assert "flex: 1 1 190px" in STYLE_CSS[body:body + 120]


# ── P5: character_limit_free visible + editable in Admin ────────────

def test_p5_settings_payload_accepts_character_limit_free():
    payload = api_mod.AdminSettingsPayload(character_limit_free=750)
    assert payload.character_limit_free == 750


def test_p5_settings_validation_bounds():
    out = api_mod._validate_admin_settings_updates({"character_limit_free": 500})
    assert out["character_limit_free"] == 500
    for bad in (0, 50, 99, 10001, "abc", 3.5):
        with pytest.raises(HTTPException):
            api_mod._validate_admin_settings_updates({"character_limit_free": bad})


def test_p5_get_settings_and_ui_wiring():
    assert '"character_limit_free": int(getattr(settings, "character_limit_free", 1000) or 1000)' in API_PY
    assert 'id="adm-set-char-limit-free"' in INDEX_HTML
    assert 'data-i18n="settings_char_limit_free"' in INDEX_HTML
    assert '"adm-set-char-limit-free"' in APP_JS
    assert "character_limit_free: charLimitFree" in APP_JS
    assert "s.character_limit_free" in APP_JS


# ── P6: pipeline status lines localized ─────────────────────────────

def test_p6_stage_mapping_ingest_seams():
    f = api_mod._recap_stage_progress
    assert f("📥 Downloading source video from URL...", 0)[0] == "download"
    assert f("⚡ [1/4] Groq Cloud Whisper Turbo active (key, model=whisper-large-v3)...", 5)[0] == "transcribe"
    assert f("⚡ [1/4] Groq Cloud Whisper Turbo Audio Ingestion starting...", 5)[0] == "transcribe"
    assert f("✅ Whisper raw segments: 42", 15)[0] == "transcribe"
    assert f("🎙️ [1/4] Groq Cloud Whisper Turbo Transcription...", 5)[0] == "transcribe"
    assert f("🌐 Translate batch 2/5 — IDs 00010–00020", 42)[0] == "translate"
    assert f("🧩 [4/4] V2 Smart Sync boundaries → full source coverage", 54)[0] == "plan"


def test_p6_authored_labels_carry_stages():
    for frag in (
        '"📥 Downloading source video from URL...", stage="download"',
        '"🎙️ [1/4] Transcription + translation (script review)...", stage="transcribe"',
        '"📝 Script ready — review & edit before rendering", stage="script_ready"',
        '"🎉 Recap Video Generation Completed!", stage="finalize"',
        '"✂️ Cutting into parts (smart cut at narration pauses)...", stage="split"',
        '"🗣️ Rendering with your reviewed script...", stage="tts"',
    ):
        assert frag in API_PY, frag


def test_p6_set_entry_label_stage_semantics():
    entry = {"progress_percent": 20}
    api_mod._recap_set_entry_label(entry, "x", stage="download")
    assert entry["progress_stage"] == "download"
    assert entry["progress_step"] == "1/4"
    assert entry["progress_percent"] == 20  # monotonic: never decreases
    entry2 = {}
    api_mod._recap_set_entry_label(entry2, "x", stage="script_ready")
    assert entry2["progress_stage"] == "script_ready"
    assert entry2["progress_percent"] == 60
    entry3 = {"progress_stage": "tts"}
    api_mod._recap_set_entry_label(entry3, "dynamic burmese line")
    assert entry3["progress_stage"] is None  # legacy path unchanged


def test_p6_stage_i18n_keys_both_languages():
    for key in ("recap_stage_download", "recap_stage_script_ready", "recap_stage_split"):
        assert I18N_JS.count(key + ":") == 2, key
    assert "download" in api_mod._RECAP_STAGE_BANDS
    assert "script_ready" in api_mod._RECAP_STAGE_BANDS
    assert "split" in api_mod._RECAP_STAGE_BANDS


# ── P7: EN completeness on Recap step 1 ─────────────────────────────

def _has_myanmar(text: str) -> bool:
    return any("\u1000" <= ch <= "\u109f" for ch in text)


def test_p7_step1_strings_wired_to_i18n():
    for frag in (
        'data-i18n="recap_source_tab_url"',
        'data-i18n="recap_source_tab_file"',
        'data-i18n="recap_drop_text"',
        'data-i18n="recap_drop_subtext"',
    ):
        assert frag in INDEX_HTML, frag
    tr = _translations()
    for key in ("recap_source_tab_url", "recap_source_tab_file", "recap_drop_text",
                "recap_drop_subtext", "recap_sub_ready", "recap_sub_ai"):
        assert tr["my"][key] and tr["en"][key], key
        assert not _has_myanmar(tr["en"][key]), key


def test_p7_info_chips_repaint_on_language_change():
    assert "function paintRecapInfoMeta(data, isOverLimit, maxAllowed)" in APP_JS
    assert "state.lastRecapInfo = data;" in APP_JS
    assert "window.refreshRecapInfoMetaLanguage = refreshRecapInfoMetaLanguage;" in APP_JS
    assert "window.refreshRecapInfoMetaLanguage()" in APP_JS  # toggle hook
    assert '"✅ Subtitle Ready"' not in APP_JS  # hardcoded chip text gone
    assert 't("recap_sub_ready")' in APP_JS and 't("recap_sub_ai")' in APP_JS


# ── Copy batch ──────────────────────────────────────────────────────

def test_e1_open_via_telegram_fallback_codepoints():
    good = "ဖ" + "ွ" + "င" + "့" + "ပ" + "ာ"   # ဖွင့်ပါ
    bad = "ဖ" + "ွ" + "င" + "်" + "္" + "ပ" + "ါ"  # stacked asat+virama, wrong vowel
    assert f"Telegram ထဲမှ {good}" in INDEX_HTML
    assert bad not in INDEX_HTML


def test_e2_price_word_spelling():
    tr = _translations()
    assert "ဈေးနှုန်း" in tr["my"]["plan_how_text2"]
    assert "စျေးနှုန်း" not in tr["my"]["plan_how_text2"]


def test_e3_e4_index_fallbacks():
    assert "စာတန်းဖုံး" not in INDEX_HTML
    assert "စာတန်းထိုး အနေအထားချိန်ရန်" in INDEX_HTML
    assert "စကွဲယား" not in INDEX_HTML
    assert "📐 1:1 (Square)" in INDEX_HTML


def test_e9_persona_display_names_unified():
    from app.services.curated_voices import get_curated_voice
    expect = {
        "komin": "ကိုမင်း", "koaung": "ကိုအောင်", "kophone": "ကိုဘုန်း",
        "mahnin": "မနှင်း", "manadi": "မနဒီ", "mamay": "မမေ",
    }
    for pid, name in expect.items():
        info = get_curated_voice(pid)
        assert info["name_my"].startswith(name), (pid, info["name_my"])
    assert "ကိုဖုန်း" not in INDEX_HTML and "ကိုဖုန်း" not in CURATED_PY
    assert "(ကိုအောင် — Viral Shorts)" in INDEX_HTML
    assert "(ကိုဘုန်း — Tech & Explainer)" in INDEX_HTML


def test_e10_burmese_table_translated():
    tr = _translations()
    keys = (
        "custom_ref_optional", "btn_dl_download", "recap_info_ready",
        "fmt_video_title", "fmt_audio_title", "fmt_sub_title", "dl_title",
        "vip_title", "mirror_on", "mirror_off", "preview_label",
        "recap_engine_label", "recap_sub_band", "recap_sub_heavy",
        "recap_sub_soft", "recap_quality_standard", "recap_quality_high",
        "recap_quality_max", "plan_voice_f4", "plan_recap_f2",
        "plan_recap_f5", "plan_master_f4", "plan_master_f5",
        "plan_recap_starter_f2",
    )
    for key in keys:
        val = tr["my"][key]
        assert _has_myanmar(val), (key, val)


def test_e11_cancel_label_formal():
    tr = _translations()
    assert tr["my"]["confirm_cancel"] == "ပယ်ဖျက်မည်"
    assert 't("confirm_cancel") : "ပယ်ဖျက်မည်"' in APP_JS


def test_e16_laptop_worker_copy_removed():
    assert "worker_auto_complete" not in I18N_JS
    assert "worker_auto_complete" not in APP_JS
    assert "is_waiting_laptop_worker" not in APP_JS
    assert "Laptop Worker" not in I18N_JS


def test_e17_no_ops_text_in_user_errors():
    assert "Render Dashboard" not in DOWNLOADER_PY
    assert "YOUTUBE_COOKIES ထည့်သွင်း" not in DOWNLOADER_PY
    # The user-safe guidance itself is still there.
    assert DOWNLOADER_PY.count("Anti-Bot စစ်ဆေးမှု") == 3
    assert "တိုက်ရိုက် Upload" in DOWNLOADER_PY


def test_v2_send_telegram_single_emoji():
    tr = _translations()
    for lang in ("my", "en"):
        for key in ("btn_send_voice_telegram", "btn_send_recap_telegram"):
            assert not tr[lang][key].startswith("📱"), (lang, key)
    # The icon span in the templates/static HTML is the single 📱 source.
    assert '<span class="btn-icon">📱</span> <span data-i18n="btn_send_voice_telegram">' in INDEX_HTML


def test_cache_buster_bumped():
    # Token supersession chain: r4 -> r6 (round 6) -> gv1 (Gemini
    # voice preview feature, which changed all three statics).
    assert INDEX_HTML.count("?v=20261101_gv1") == 3
    assert "?v=20261101_r6" not in INDEX_HTML
    assert "?v=20261101_r4" not in INDEX_HTML


def test_no_ae_di_anywhere_in_touched_copy():
    for blob in (I18N_JS, INDEX_HTML, APP_JS):
        assert "အဲ့ဒီ" not in blob


# ── P2: Refer & Support in-app surfaces ─────────────────────────────

def test_p2_me_payload_carries_referral_and_support():
    assert '"referral_code": referral_code' in API_PY
    assert '"referral": referral_stats' in API_PY
    assert '"support_telegram": "@VoiceCloneProAdmin"' in API_PY
    assert '"support_link": "https://t.me/VoiceCloneProAdmin"' in API_PY
    assert "https://t.me/voiceclonepro_bot?start={referral_code}" in API_PY
    assert "await _ensure_referral_code(db, user)" in API_PY


@pytest.mark.asyncio
async def test_p2_ensure_referral_code_lazy_generates(db_session):
    from app.models.schema import User
    user = User(id=987654321, first_name="R6", language_code="my")
    db_session.add(user)
    await db_session.commit()
    code1 = await api_mod._ensure_referral_code(db_session, user)
    assert code1.startswith("ref_987654321")
    code2 = await api_mod._ensure_referral_code(db_session, user)
    assert code2 == code1  # stable, generated once


def test_p2_home_card_and_handlers():
    assert 'id="home-refer-card"' in INDEX_HTML
    assert 'id="referral-link-text"' in INDEX_HTML
    assert 'id="referral-stats-line"' in INDEX_HTML
    assert 'onclick="copyReferralLink()"' in INDEX_HTML
    assert 'onclick="shareReferralLink()"' in INDEX_HTML
    assert 'onclick="openSupportContact()"' in INDEX_HTML
    assert "function updateReferUI(data)" in APP_JS
    assert "updateReferUI(data);" in APP_JS  # loadUserProfile hook
    assert "https://t.me/share/url?url=" in APP_JS
    tr = _translations()
    for key in ("ref_card_title", "ref_desc", "ref_stats_line", "ref_share_btn",
                "ref_share_text", "ref_link_pending", "support_contact_title",
                "support_contact_desc"):
        assert tr["my"][key] and tr["en"][key], key


# ── P10: admin sub-nav affordance ───────────────────────────────────

def test_p10_admin_nav_scroll_cue_and_active_centering():
    assert "mask-image: linear-gradient(to right, #000 92%, transparent 100%)" in STYLE_CSS
    assert 'scrollIntoView({ block: "nearest", inline: "center"' in APP_JS

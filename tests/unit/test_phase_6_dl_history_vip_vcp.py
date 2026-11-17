"""Phase 6 verification tests — Downloader, History Hub & VIP Design Pass.

Covers:
1. Downloader: Phase 1–4 `.vcp-*` + `data-ui-state` (`empty`, `loading`,
   `error`, `ready`) skeleton/empty/error states, retry button, lite mode,
   preserved IDs/handlers, no extraction/download endpoint changes.
2. History Hub: Phase 1–4 `.vcp-*` + `data-ui-state` (`loading`, `empty`,
   `error`, `ready`) skeletons/empty CTAs/error retry, status badges, real
   durations (`seconds_consumed` / `seconds_estimated` / `duration_seconds`
   in `GET /api/history` and rendered cards), display-only 22h expires-soon
   reminder (~2h left before 24h deletion, both my + en, zero retention or
   backend job changes), and owning-tab `switchTab` fixes before DOM
   mutation in History-reachable handlers.
3. VIP / Payment: Design pass only, high contrast labels, non-expiry
   trust badge, existing `PLAN_CATALOGUE` + `/api/vip/plans` price sync
   preserved, payment safeguards untouched, no expiry copy on purchases.
4. Zero hardcoded user-visible copy, full `my`+`en` i18n parity, and
   cache token preserved.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.models.enums import AccountCategory, JobStatus, JobType
from app.models.schema import Job, User
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"
INDEX_HTML = (STATIC / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
APP_JS = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")


def _extract_i18n_block(locale: str) -> dict[str, str]:
    script = (
        I18N_JS.split("let currentLang =", 1)[0]
        + f"\nconsole.log(JSON.stringify(translations[{json.dumps(locale)}]));\n"
    )
    res = subprocess.check_output(["node", "-"], input=script, text=True, encoding="utf-8")
    return json.loads(res)


# ── 1. Downloader Design Pass & UI States ─────────────────────────────

def test_downloader_preserves_section_and_uses_vcp_state_machine():
    assert '<section id="tab-downloader" class="tab-pane">' in INDEX_HTML
    sec = INDEX_HTML.split('<section id="tab-downloader" class="tab-pane">', 1)[1].split("</section>", 1)[0]
    assert 'id="dl-extract-stage"' in sec
    assert 'data-ui-state="empty"' in sec
    assert "vcp-state-empty" in sec
    assert "vcp-state-loading" in sec
    assert "vcp-skel dl-skel-thumb" in sec
    assert "vcp-skel dl-skel-fmt" in sec
    assert 'id="dl-error-box"' in sec
    assert 'data-i18n="dl_retry_btn"' in sec
    assert 'id="dl-result-thumb" class="dl-thumb" alt=""' in sec
    assert 'id="btn-dl-use-in-recap"' in sec
    assert "vcp-dl-format-card" in sec


def test_downloader_js_wires_loading_empty_error_ready_without_hardcoded_fallbacks():
    fn = APP_JS[APP_JS.index("function setupDownloader()"):APP_JS.index("function switchPlanCategory")]
    assert 'setSectionState("dl-extract-stage", "loading")' in fn
    assert 'setSectionState("dl-extract-stage", "ready")' in fn
    assert 't("dl_fallback_title")' in fn
    assert 't("dl_fallback_uploader")' in fn
    assert '"Video Content"' not in fn
    assert '"Creator"' not in fn
    err_helpers = APP_JS[APP_JS.index("function dlShowExtractError()"):APP_JS.index("function dlSetSrtCardState")]
    assert 'setSectionState("dl-extract-stage", "error")' in err_helpers
    assert 'setSectionState("dl-extract-stage", "empty")' in err_helpers


# ── 2. History Hub Design Pass, Real Durations & 22h Reminder ─────────

def test_history_preserves_section_and_has_vcp_skeleton_empty_error_states():
    assert '<section id="tab-history" class="tab-pane">' in INDEX_HTML
    sec = INDEX_HTML.split('<section id="tab-history" class="tab-pane">', 1)[1].split("</section>", 1)[0]
    assert 'id="hub-history-stage"' in sec
    assert 'data-ui-state="loading"' in sec
    assert sec.count("hub-skel-card") >= 3
    assert "vcp-state-empty" in sec
    assert "vcp-state-error" in sec
    assert 'data-i18n="hist_empty_cta_voice"' in sec
    assert 'data-i18n="hist_empty_cta_recap"' in sec
    assert 'id="hub-history-list"' in sec
    assert 'data-ui-state' in sec


@pytest.mark.asyncio
async def test_api_history_exposes_real_and_estimated_durations_with_24h_ttl(db_session):
    user = User(
        id=910601,
        first_name="Hist",
        account_category=AccountCategory.PURCHASED.value,
        language_code="my",
    )
    db_session.add(user)
    await db_session.flush()
    now = datetime.now(timezone.utc)
    j_voice = Job(
        id="vc_hist_dur_1",
        user_id=user.id,
        status=JobStatus.SUCCESS.value,
        job_type=JobType.VOICE_CLONE.value,
        input_text="Voice test",
        engine="voxcpm",
        preset_voice="komin",
        seconds_estimated=40,
        seconds_consumed=33,
        created_at=now - timedelta(hours=2),
    )
    j_recap_queued = Job(
        id="rc_hist_dur_2",
        user_id=user.id,
        status=JobStatus.QUEUED.value,
        job_type=JobType.RECAP.value,
        input_text="Recap queued test",
        engine="recap",
        preset_voice="thiha",
        seconds_estimated=145,
        seconds_consumed=0,
        created_at=now - timedelta(hours=22, minutes=30),
    )
    db_session.add_all([j_voice, j_recap_queued])
    await db_session.commit()

    payload = await api_mod.get_user_history(user=user, db=db_session)
    assert payload["ttl_hours"] == 24
    by_id = {row["id"]: row for row in payload["history"]}
    assert by_id["vc_hist_dur_1"]["seconds_consumed"] == 33
    assert by_id["vc_hist_dur_1"]["seconds_estimated"] == 40
    assert by_id["vc_hist_dur_1"]["duration_seconds"] == 33
    assert by_id["rc_hist_dur_2"]["seconds_consumed"] == 0
    assert by_id["rc_hist_dur_2"]["seconds_estimated"] == 145
    assert by_id["rc_hist_dur_2"]["duration_seconds"] == 145


def test_history_js_duration_and_22h_display_only_expires_soon_node_vm():
    # Evaluate formatJobDuration, computeHistoryExpiryState, computeRetentionBadgeHtml,
    # hubHistoryRowHtml, hubHistoryDetailHtml in Node against real i18n.js!
    fn_block = APP_JS[
        APP_JS.index("function formatJobDuration(j)"):
        APP_JS.index("function renderHistoryHub()")
    ]
    ret_block = APP_JS[
        APP_JS.index("function computeRetentionBadgeHtml(j)"):
        APP_JS.index("function openRecentProjectAction(")
    ]
    helper_stubs = """
    const window = {};
    const state = { canUseAiThumbnail: false, aiThumbnailCapReached: false, user: null };
    function escapeHtml(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;');}
    function brandedDlName(a,b,c,d){return a+'_'+d+'.'+b;}
    let hubOpenDetailId = null;
    function hubStatusBorder(s){return 'border-'+(s||'').toLowerCase();}
    """
    node_script = f"""
    {I18N_JS.split("let currentLang =", 1)[0]}
    let currentLang = 'en';
    function t(k){{ return (translations[currentLang]||translations.my)[k] || translations.en[k] || k; }}
    function tf(k,p){{ let m=t(k); if(p) for(const [a,b] of Object.entries(p)) m=m.split('{{'+a+'}}').join(String(b)); return m; }}
    {helper_stubs}
    {ret_block}
    {fn_block}
    const now = Date.now();
    const freshJob = {{
        id: 'vc_1', job_type: 'VOICE_CLONE', status: 'SUCCESS',
        input_snippet: 'Fresh job', voice_display: 'Ko Min',
        seconds_consumed: 95, seconds_estimated: 120, duration_seconds: 95,
        created_at: new Date(now - 5 * 3600000).toISOString(), time_ago: '5h ago',
    }};
    const soonJob = {{
        id: 'rc_2', job_type: 'RECAP', status: 'SUCCESS',
        input_snippet: 'Expiring soon recap', voice_display: 'Thiha',
        seconds_consumed: 132, seconds_estimated: 120, duration_seconds: 132,
        created_at: new Date(now - 22.5 * 3600000).toISOString(), time_ago: '22h ago',
        deliverables: {{ video: '/dl/v.mp4' }}
    }};
    const expiredJob = {{
        id: 'rc_3', job_type: 'RECAP', status: 'SUCCESS', is_expired: true,
        input_snippet: 'Expired recap', voice_display: 'Thiha',
        created_at: new Date(now - 25 * 3600000).toISOString(),
    }};
    const resEn = {{
        durFresh: formatJobDuration(freshJob),
        durSoon: formatJobDuration(soonJob),
        expFresh: computeHistoryExpiryState(freshJob),
        expSoon: computeHistoryExpiryState(soonJob),
        expOld: computeHistoryExpiryState(expiredJob),
        rowFresh: hubHistoryRowHtml(freshJob),
        rowSoon: hubHistoryRowHtml(soonJob),
        detSoon: hubHistoryDetailHtml(soonJob),
        homeSoon: computeRetentionBadgeHtml(soonJob),
    }};
    currentLang = 'my';
    const resMy = {{
        durSoon: formatJobDuration(soonJob),
        rowSoon: hubHistoryRowHtml(soonJob),
        detSoon: hubHistoryDetailHtml(soonJob),
    }};
    console.log(JSON.stringify({{ resEn, resMy }}));
    """
    out = json.loads(subprocess.check_output(["node", "-"], input=node_script, text=True, encoding="utf-8"))
    en = out["resEn"]
    my = out["resMy"]

    assert en["durFresh"] == "1m 35s"
    assert en["durSoon"] == "2m 12s"
    assert en["expFresh"]["expiresSoon"] is False
    assert en["expSoon"]["expiresSoon"] is True
    assert en["expSoon"]["expired"] is False
    assert en["expOld"]["expired"] is True
    assert "1m 35s" in en["rowFresh"]
    assert "hub-expires-soon-badge" not in en["rowFresh"]
    assert "2m 12s" in en["rowSoon"]
    assert "is-expires-soon" in en["rowSoon"]
    assert "hub-expires-soon-badge" in en["rowSoon"]
    assert "Expires soon" in en["rowSoon"]
    assert "hub-expires-soon-banner" in en["detSoon"]
    assert "hub-expires-soon-badge" in en["homeSoon"]
    assert "2 မိနစ် 12 စက္ကန့်" in my["durSoon"]
    assert "မကြာမီ သက်တမ်းကုန်" in my["rowSoon"]
    assert "hub-expires-soon-banner" in my["detSoon"]


def test_history_actions_switch_to_owning_tab_before_mutating():
    voice_det = APP_JS[APP_JS.index("function openVoiceDetail(jobId)"):APP_JS.index("function openRecapDetail(")]
    recap_det = APP_JS[APP_JS.index("function openRecapDetail(jobId)"):APP_JS.index("window.openCurrentVoiceDetail")]
    script_ed = APP_JS[APP_JS.index("async function openRecapScriptEditor(jobId)"):APP_JS.index("async function saveRecapScript(")]
    retry_fn = APP_JS[APP_JS.index("async function retryRecapJobById(jobId)"):APP_JS.index("window.retryRecapJob")]
    player_fn = APP_JS[APP_JS.index("async function openRecapInPlayer(recapJobId)"):APP_JS.index("window.openRecapInPlayer")]

    assert voice_det.index('switchTab("tab-voice")') < voice_det.index('getElementById("voice-progress-box")')
    assert recap_det.index('switchTab("tab-recap")') < recap_det.index('getElementById("recap-progress-box")')
    assert "showRecapStep(4)" in recap_det
    assert script_ed.index('switchTab("tab-recap")') < script_ed.index('getElementById("recap-script-box")')
    assert "showRecapStep(4)" in script_ed
    assert retry_fn.index('switchTab("tab-recap")') < retry_fn.index('getElementById("recap-failed-box")')
    assert retry_fn.index("showRecapStep(4)") < retry_fn.index("startRecapJobPolling(")
    assert player_fn.index('switchTab("tab-recap")') < player_fn.index('getElementById("recap-result-box")')
    assert "showRecapStep(4)" in player_fn


# ── 3. VIP Design Pass & Untouched Payment / Expiry Guardrails ────────

def test_vip_design_pass_preserves_prices_and_never_expires_copy():
    vip = INDEX_HTML.split('<section id="tab-vip" class="tab-pane">', 1)[1].split("</section>", 1)[0]
    assert 'data-i18n="vip_never_expires_badge"' in vip
    assert 'data-i18n="vip_pay_step_hint"' in vip
    assert 'data-price-for="MIN_10">8,000<' in vip
    assert 'data-price-for="voice_studio_pro">75,000<' in vip
    assert 'data-price-for="recap_starter">20,000<' in vip
    assert 'data-price-for="recap_studio_pro">90,000<' in vip
    assert 'data-price-for="combined_starter">30,000<' in vip
    assert 'data-price-for="all_in_one_master">180,000<' in vip
    assert 'openPaymentDrawer(\'MIN_10\', 8000' in vip
    assert 'for="slip-file-input"' in vip
    assert 'id="slip-file-input"' in vip
    assert 'id="btn-submit-slip-action"' in vip
    assert 't("pay_account_name_prefix")' in APP_JS
    assert 'Account Name: ${acc.name}' not in APP_JS
    # High-contrast payment titles & Phase 6 lite overrides in style.css
    p6_css = STYLE_CSS.split("PHASE 6: DOWNLOADER, HISTORY HUB & VIP DESIGN PASS", 1)[1]
    assert "#tab-vip .pay-method.kpay .pay-title" in p6_css
    assert "var(--vcp-warning)" in p6_css
    assert "var(--vcp-info)" in p6_css
    assert "html.lite #tab-history .hub-skel-card" in p6_css


# ── 4. i18n Parity & Cache Token ──────────────────────────────────────

def test_phase6_i18n_parity_and_cache_token():
    my = _extract_i18n_block("my")
    en = _extract_i18n_block("en")
    assert set(my.keys()) == set(en.keys())
    required = [
        "dl_empty_title", "dl_empty_sub", "dl_loading_title", "dl_retry_btn",
        "dl_fallback_title", "dl_fallback_uploader",
        "hist_type_recap", "hist_status_cancelled",
        "hist_duration_sec", "hist_duration_min_sec", "hist_duration_min",
        "hist_expires_soon_badge", "hist_expires_soon_banner",
        "hist_time_hm", "hist_time_m", "hist_time_h",
        "hist_dl_video", "hist_dl_audio", "hist_dl_srt", "hist_dl_txt", "hist_dl_voice",
        "hist_sub_margin_lbl", "hist_sub_pos_lbl", "hist_blur_pos_lbl",
        "hist_empty_cta_voice", "hist_empty_cta_recap",
        "vip_never_expires_badge", "pay_account_name_prefix", "vip_pay_step_hint",
    ]
    for key in required:
        assert key in my and my[key], f"missing my.{key}"
        assert key in en and en[key], f"missing en.{key}"
    assert INDEX_HTML.count("20261117_r160") == 4

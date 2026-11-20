/* ═══════════════════════════════════════════════════════════
   STUDIO TOAST & CONFIRM SYSTEM — Premium Non-Blocking UX
   ═══════════════════════════════════════════════════════════ */

/**
 * showStudioToast(message, type, duration)
 * type: 'success' | 'error' | 'warning' | 'info'
 * Replaces all browser alert() calls
 */
function showStudioToast(message, type = 'info', duration = 3500) {
    // Remove any existing toast
    const existing = document.getElementById('studio-toast-el');
    if (existing) existing.remove();

    const icons = { success: '✅', error: '❌', warning: '⚠️', info: 'ℹ️' };

    const toast = document.createElement('div');
    toast.id = 'studio-toast-el';
    toast.className = `studio-toast ${type}`;
    // Batch AZ-12: the close button's aria-label resolves through
    // i18n like every other UI string (was a hardcoded "Close").
    const closeLabel = typeof t === "function" ? t("toast_close") : "Close";
    toast.innerHTML = `
        <span class="toast-icon">${icons[type] || icons.info}</span>
        <span class="toast-msg"></span>
        <button class="toast-close" onclick="this.parentNode.remove()" aria-label="${closeLabel}">✕</button>
        <span class="toast-progress" aria-hidden="true"></span>
    `;
    // The message is COPY, never markup (hunt X2): it is rendered via
    // textContent so no caller- or server-supplied string can inject
    // HTML into the toast. Caller audit: all ~86 call sites pass plain
    // i18n copy — none relied on markup in the message.
    message = (typeof localizeServerText === "function") ? localizeServerText(message) : message;
    toast.querySelector('.toast-msg').textContent = message;

    document.body.appendChild(toast);
    if (duration > 0) {
        // Batch AZ-7 (presentation only): the progress hairline's
        // lifetime comes from this call's own duration via the
        // --toast-life property, and the exit (fade + sink) plays
        // INSIDE the same budget — the .leaving class lands 200 ms
        // before the unchanged removal timeout, so the total
        // on-screen time is exactly what it was.
        toast.style.setProperty('--toast-life', duration + 'ms');
        if (duration > 200) {
            setTimeout(() => toast && toast.classList.add('leaving'), duration - 200);
        }
        setTimeout(() => toast && toast.remove(), duration);
    }
}

/**
 * showStudioConfirm(message, onConfirm, onCancel)
 * Replaces browser confirm() calls with a premium modal.
 */
function showStudioConfirm(message, onConfirm, onCancel) {
    const existing = document.getElementById('studio-confirm-modal');
    if (existing) existing.remove();

    const modal = document.createElement('div');
    modal.id = 'studio-confirm-modal';
    modal.className = 'studio-confirm-overlay';
    modal.innerHTML = `
        <div class="studio-confirm-card">
            <div class="studio-confirm-icon">⚠️</div>
            <div class="studio-confirm-msg"></div>
            <div class="studio-confirm-actions">
                <button id="studio-confirm-cancel" class="btn secondary-btn">${typeof t === "function" ? t("confirm_cancel") : "Cancel"}</button>
                <button id="studio-confirm-ok" class="btn gold-gradient-btn">${typeof t === "function" ? t("confirm_ok") : "OK"}</button>
            </div>
        </div>
    `;
    // Message via textContent — copy, never markup (hunt X2, as above).
    modal.querySelector('.studio-confirm-msg').textContent = message;

    document.body.appendChild(modal);

    const remove = () => modal.remove();
    document.getElementById('studio-confirm-ok').onclick = () => { remove(); if (onConfirm) onConfirm(); };
    document.getElementById('studio-confirm-cancel').onclick = () => { remove(); if (onCancel) onCancel(); };
    modal.addEventListener('click', (e) => { if (e.target === modal) { remove(); if (onCancel) onCancel(); } });
}

function jobStatusText(status) {
    const st = String(status || "").toUpperCase();
    if (st === "SUCCESS") return t("status_badge_success");
    if (st === "RUNNING") return t("status_badge_running");
    if (st === "QUEUED" || st === "PENDING") return t("status_badge_queued");
    if (st === "FAILED") return t("status_badge_failed");
    if (st === "CANCELLED") return t("status_badge_cancelled");
    if (st === "WAITING_CHOICE") return t("status_badge_waiting_choice");
    return st || t("status_badge_other");
}
window.jobStatusText = jobStatusText;

/**
 * mapErrorToToast(raw, flow) — the ONLY way an error becomes a toast
 * (toast/start fix 2026-10-31).
 *
 * Raw server `detail` strings and stored job `error_message` values are
 * technical text (yt-dlp dumps, Python exceptions, coded prefixes) and
 * must NEVER be interpolated into a user-facing toast. This mapper
 * pattern-matches the known families and returns a safe i18n string;
 * anything unrecognized collapses to the flow's generic message.
 * String MATCHING on the raw text is allowed; DISPLAYING it is not —
 * with ONE narrow exception (E2): the two pass-through families below,
 * which match the closed set of our own localized Burmese details by
 * distinctive marker phrases and return them unchanged.
 *
 * flow: 'voice' | 'recap' | 'upload' | 'download' | 'send' | 'generic'
 */
function mapErrorToToast(raw, flow = 'generic') {
    const rawStr = (raw == null ? '' : String(raw));
    const s = rawStr.toLowerCase();
    const has = (...needles) => needles.some(n => s.includes(n));
    // Pass-through families (E2 fix 2026-10-31): a NARROW exception to
    // the never-display rule, for strings that are verifiably OUR OWN
    // localized user-facing copy. These details are closed-set Burmese
    // sentences produced by api.py itself; collapsing them into the
    // flow generic both destroys information the user needs (the exact
    // recap top-up count) and asserts a refund that never happened
    // (pre-charge rejections charged nothing). Matching is on
    // distinctive marker phrases of that closed set — any raw,
    // foreign or technical string still falls through to the generics
    // below, unchanged.
    //
    // (a) Recap insufficient-credits: the segmented shortfall message
    // (worker _RecapInsufficientCreditsError and the submit-time
    // segmented pre-check share the markers). The exact total matters.
    if (flow === 'recap' && has('စုစုပေါင်း') && has('credits လိုအပ်ပါတယ်')) {
        return typeof localizeServerText === "function" ? localizeServerText(rawStr) : rawStr;
    }
    // (c) Recap SUBMIT-time rejections that happen BEFORE any charge
    // (R40 item 11): the no-credit gate, the duration-cap message and
    // the concurrency rejection are all raised by /recap/generate
    // before the deduction — none of them may wear err_recap_failed's
    // "Credit refunded" copy (a 2026-11-02 session showed exactly
    // that false refund toast with no job and no credit movement).
    if (flow === 'recap' && has(
        'credit မလုံလောက်တော့ပါ',
        'ကန့်သတ်ချက် (အများဆုံး',
        'already running for your account',
    )) {
        return typeof localizeServerText === "function" ? localizeServerText(rawStr) : rawStr;
    }
    // (b) Voice submit rejections that happen BEFORE any charge
    // (_voice_job_error_detail's closed Burmese set: balance, daily
    // limit, concurrency, character limit, model/file problems). None
    // of them may wear err_voice_failed's "balance was refunded" copy.
    // R40 deck M1: the client used to hardcode a COPY of those
    // sentences as match markers — a twin that drifts from the API.
    // Now the API is the single source: pass through any detail that
    // is verifiably our localized copy (Burmese script present, no
    // technical-error markers) and let everything else fall through
    // to the generics below, unchanged. The flow gate still binds:
    // recap-shaped texts (parts/credits phrasing) are NOT voice copy
    // and must never pass on the voice flow.
    if (flow === 'voice'
        && /[\u1000-\u109F]/.test(rawStr)
        && !has('http', 'traceback', '.py', 'yt-dlp', 'exception', 'recap', 'အပိုင်း', 'စုစုပေါင်း')) {
        return typeof localizeServerText === "function" ? localizeServerText(rawStr) : rawStr;
    }
    // Suspended account (api.py require gate raises USER_BANNED: …).
    if (has('user_banned')) return t('err_banned');
    // Membership gate codes: the fetch interceptor already raises the
    // join reminder for MEMBERSHIP_REQUIRED / GATE_NOT_CONFIGURED; keep
    // the same user-facing copy here for non-intercepted paths.
    if (has('membership_required', 'gate_not_configured', 'gate_')) return t('gate_required_toast');
    // File too large (coded detail + the 413 wording, both languages).
    if (has('file_too_large', 'file size exceeds', 'too large', '413')) return t('err_too_large');
    // Voice engine quota / capacity: stored job errors mention ZeroGPU
    // quota or HF 402s when the pool is exhausted.
    if (has('quota', 'zerogpu', '402', 'gpu')) {
        return flow === 'recap' ? t('err_recap_failed') : t('err_engine_busy');
    }
    // Link-block family: the SOURCE site refused the fetch (TikTok /
    // VidMate / Douyin anti-bot walls, YouTube sign-in wall, HTTP 403,
    // "no video formats"). Name the source when the raw error carries
    // one and always give the flow's working alternative — never send
    // the user back to retry the same blocked link (2026-10-31 journey
    // audit: a new user burned 8 attempts on blocked links whose
    // failure copy only said "try again").
    const linkInfo = blockedLinkInfo(rawStr);
    if (linkInfo.blocked && (flow === 'recap' || flow === 'download')) {
        const head = linkInfo.source ? tf('err_link_blocked_named', { source: linkInfo.source }) : t('err_link_blocked');
        return head + ' ' + t(flow === 'download' ? 'err_link_advice_dl' : 'err_link_advice_upload');
    }
    // Platform named without block evidence: previous generic mapping.
    if (has('youtube', 'tiktok', 'douyin', 'extract', 'yt-dlp', 'fresh cookies')) {
        if (flow === 'recap') return t('err_recap_failed');
        if (flow === 'download') return t('err_download_failed');
    }
    switch (flow) {
        case 'voice': return t('err_voice_failed');
        case 'recap': return t('err_recap_failed');
        case 'upload': return t('err_upload_failed');
        case 'download': return t('err_download_failed');
        case 'send': return t('err_send_telegram');
        default: return t('err_generic');
    }
}
window.mapErrorToToast = mapErrorToToast;

// Blocked-link detection shared by the toast mapper above and the
// flows that react to a refused link (e.g. auto-switching Recap to
// the file-upload tab — the path that always works). A link counts as
// blocked when the raw error carries a refusal signal ("no video
// formats", HTTP 403, sign-in wall, unsupported URL…) or names a
// known source next to an extractor failure. Kept directly beside
// the mapper: the predeploy-minifix node harness slices this whole
// region out of app.js and executes it.
const LINK_SOURCE_NAMES = [
    ['tiktok', 'TikTok'], ['douyin', 'Douyin'], ['vidmate', 'VidMate'],
    ['youtube', 'YouTube'], ['youtu.be', 'YouTube'],
    ['instagram', 'Instagram'], ['facebook', 'Facebook'], ['fb.watch', 'Facebook'],
];
function blockedLinkInfo(raw) {
    const s = (raw == null ? '' : String(raw)).toLowerCase();
    const has = (...needles) => needles.some(n => s.includes(n));
    const hit = LINK_SOURCE_NAMES.find(([needle]) => s.includes(needle));
    const blocked = has(
        'no video formats', 'unsupported url', 'unable to extract',
        'sign in to confirm', 'fresh cookies', 'http error 403', 'error 403', 'forbidden',
        // IP blocked (round-3: job rc_1793505308_08cd6b failed with
        // "[TikTok] …: Your IP address is blocked from accessing this
        // post" and fell through to the generic copy because none of
        // the needles above matched it).
        'ip address is blocked', 'ip is blocked', 'blocked from accessing',
        // Our own server-classified blocked-fetch copy (downloader.py
        // classify_source_error, the 403 / IP-block branch): once the
        // server stores that clean Burmese sentence on the job, none
        // of the raw extractor needles above exist in the string any
        // more. This marker keeps the stored form recognised, so the
        // recap poll keeps the named toast + upload-tab auto-switch
        // it already had for the raw form.
        'ဆာဗာမှ ဗီဒီယို ဆွဲယူခြင်းကို ပိတ်ထားပါသည်',
        // Same treatment for the R39 direct-host refusal copy
        // (classify_source_error's host-refusal branch): a media
        // file's host refused our server's fetch of it.
        'ဆွဲယူခြင်းကို ငြင်းပယ်ထားပါသည်',
        // Login walls.
        'login required', 'log in to', 'sign in to', 'please log in',
        // Age gates.
        'age-restricted', 'age restricted', 'confirm your age',
        'inappropriate for some users',
        // Region blocks.
        'in your country', 'in your region', 'not available in your location',
        'uploader has not made',
        // Private / removed content.
        'private video', 'video is private', 'this video is private',
        'account is private', 'has been removed', 'video removed',
        'no longer available', 'video unavailable', 'video is unavailable',
        'content is not available', "content isn't available",
        'post is not available')
        || (hit && has('extract', 'yt-dlp', 'download', 'blocked'));
    return { blocked: !!blocked, source: hit ? hit[1] : null };
}
window.blockedLinkInfo = blockedLinkInfo;

// displayJobError(raw): the ONLY way a STORED job error becomes card
// text (admin queue card, admin history card, retry status box).
// Stored errors are technical text — a raw yt-dlp dump must never
// reach the card (owner screenshot 2026-11-02: two History rows
// showing "ERROR: [TikTok] … Your IP address is blocked from
// accessing this post" verbatim). Three cases:
//   1. our own server-classified copy (⚠️ prefix — downloader.py
//      classify_source_error) is already user-safe: show it, minus
//      the prefix (the cards add their own ⚠️);
//   2. a raw error from the blocked-source family maps to the same
//      named blocked message + upload advice the toast mapper
//      composes for this family;
//   3. anything else stays raw — admin detail keeps the technical
//      text for non-source errors, as before.
// Returns '' for empty input.
function displayJobError(raw) {
    const s = (raw == null ? '' : String(raw)).trim();
    if (!s) return '';
    if (s.startsWith('⚠️')) return localizeServerText(s.replace(/^⚠️\s*/, ''));
    const info = blockedLinkInfo(s);
    if (info.blocked) {
        const head = info.source ? tf('err_link_blocked_named', { source: info.source }) : t('err_link_blocked');
        return head + ' ' + t('err_link_advice_upload');
    }
    return localizeServerText(s);
}
window.displayJobError = displayJobError;

// setCardThumb(img, url): the one way info-card thumbnails are set.
// An <img> left with an empty/removed source renders the browser's
// broken-image glyph plus its alt text (round-3 U2: every fallback
// card, and the uploaded-file card whose flow never set a thumbnail
// at all). With a URL: show the image, and hide it again if the URL
// itself fails to load. Without one: clear the source and hide the
// element so the card simply has no thumbnail.
function setCardThumb(img, url) {
    if (!img) return;
    if (url) {
        img.onerror = () => { img.style.display = "none"; };
        img.src = url;
        img.style.display = "block";
    } else {
        img.onerror = null;
        img.removeAttribute("src");
        img.style.display = "none";
    }
}
window.setCardThumb = setCardThumb;

// showErrorToast(err, flow): console keeps the diagnostic, the user
// only ever sees the mapped premium copy.
function showErrorToast(err, flow = 'generic') {
    try { console.warn('Flow error (' + flow + '):', err); } catch (e) {}
    const raw = err && err.message ? err.message : err;
    showStudioToast(mapErrorToToast(raw, flow), 'error');
}
window.showErrorToast = showErrorToast;

/**
 * showStudioInputModal({title, placeholder, initialValue, confirmLabel,
 * onConfirm}) — premium replacement for the native prompt dialog (used by
 * the slip-rejection flow; mirrors the ban-reason modal pattern).
 */
function showStudioInputModal(opts) {
    const existing = document.getElementById('studio-input-modal');
    if (existing) existing.remove();
    const esc = (v) => String(v == null ? '' : v)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    const modal = document.createElement('div');
    modal.id = 'studio-input-modal';
    modal.className = 'studio-confirm-overlay';
    modal.innerHTML = `
        <div class="studio-confirm-card">
            <div class="studio-confirm-icon">📝</div>
            <div class="studio-confirm-msg">${esc(opts.title || '')}</div>
            <input id="studio-input-field" class="studio-input" type="text"
                   placeholder="${esc(opts.placeholder || '')}" value="${esc(opts.initialValue || '')}">
            <div class="studio-confirm-actions">
                <button id="studio-input-cancel" class="btn secondary-btn">${t('confirm_cancel')}</button>
                <button id="studio-input-ok" class="btn gold-gradient-btn">${esc(opts.confirmLabel || t('confirm_ok'))}</button>
            </div>
        </div>
    `;
    document.body.appendChild(modal);
    const field = document.getElementById('studio-input-field');
    if (field) { field.focus(); try { field.select(); } catch (e) {} }
    const remove = () => modal.remove();
    document.getElementById('studio-input-ok').onclick = () => {
        const value = field ? field.value.trim() : '';
        remove();
        if (opts.onConfirm) opts.onConfirm(value);
    };
    document.getElementById('studio-input-cancel').onclick = remove;
    modal.addEventListener('click', (e) => { if (e.target === modal) remove(); });
}
window.showStudioInputModal = showStudioInputModal;

/**
 * app.js - Unified Telegram Mini App Client Controller
 * Voice Clone Pro & AI Movie Recap Studio
 */

// ── Unified Speed System (Single Source of Truth matching app.services.speed) ──
const VCP_SPEED_OPTIONS = ["-6%", "+0%", "+12%", "+20%", "+25%"];
const VCP_LEGACY_SPEED_MAP = {
    slow: "-6%", "0.8": "-6%", "0.8x": "-6%", "-20%": "-6%", "-15%": "-6%", "-8%": "-6%",
    "1.0": "+0%", "1.0x": "+0%", "0%": "+0%",
    normal: "+12%", "+8%": "+12%",
    fast: "+20%", "1.2": "+20%", "1.2x": "+20%", "+15%": "+20%", "+18%": "+20%",
    "+22%": "+25%",
};
function normalizeSpeedOption(v) {
    if (v == null) return "+12%";
    const raw = String(v).trim();
    if (VCP_SPEED_OPTIONS.includes(raw)) return raw;
    return VCP_LEGACY_SPEED_MAP[raw.toLowerCase()] || "+12%";
}
function speedTempoFactor(v) {
    const k = String(v == null ? "" : v).trim().toLowerCase();
    if (k === "slow" || k === "0.8" || k === "0.8x") return 0.94;
    if (k === "normal") return 1.12;
    if (k === "fast" || k === "1.2" || k === "1.2x") return 1.20;
    if (k.includes("%")) {
        const pct = parseFloat(k.replace("%", ""));
        if (Number.isFinite(pct)) return Math.min(1.45, Math.max(0.88, 1 + pct / 100));
    }
    const num = parseFloat(k);
    if (Number.isFinite(num) && num >= 0.7 && num <= 1.5) return Math.min(1.45, Math.max(0.88, num));
    return 1.12;
}
window.VCP_SPEED_OPTIONS = VCP_SPEED_OPTIONS;
window.normalizeSpeedOption = normalizeSpeedOption;
window.speedTempoFactor = speedTempoFactor;

// ── State ──────────────────────────────────────────────────────────────────
const state = {
    tg: window.Telegram?.WebApp || null,
    user: null,
    models: [],
    curatedVoices: [],
    voxcpmSubMode: "custom",        // "custom" (primary) | "curated" (12 built-in personas)
    selectedPersona: "komin",
    selectedModelSlug: "voxcpm-cloud-ai",
    selectedReferenceFile: null,
    speed: "+12%",
    activeJobId: null,
    pollTimer: null,

    // Recap Configuration
    recapRatio: "9:16",
    recapRatioFill: "blur",
    recapCropTop: 0,
    recapCropBottom: 0,
    recapCropLeft: 0,
    recapCropRight: 0,
    recapSyncCrop: true,
    recapMirror: true,
    recapZoom: 1.0,
    recapSpeed: "+12%",
    recapStoryStyle: "viral",
    recapBurnSubs: true,
    recapBlurSubs: true,
    // Batch BI: source-subtitle auto-detect blur switch — ON by
    // default (the historical behaviour); OFF skips the detector so
    // only the user's own blur box / plate is blurred.
    recapBlurAutoDetect: true,
    recapLogo: true,
    recapLogoPos: "top-right",
    recapLogoScale: 0.16,
    recapLogoOpacity: 0.70,
    // Custom channel logo (paid-only, owner decision 2026-11-02):
    // canUse/has come from /api/user/me; logoUrl (signed preview URL)
    // comes from GET /api/user/logo.
    logoCanUse: false,
    logoHas: false,
    logoUrl: null,
    recapSubStyle: "yellow",
    recapSubFont: "padauk",
    recapFontSize: 16,
    recapLines: 1,
    recapSubRenderMode: "timeline",
    recapSubEffect: "none",
    recapEmbedSoftSubs: true,
    savedCustomVoiceId: "",
    savedCustomVoiceName: "",
    // Batch AA: true once the user DELIBERATELY sets the blur box
    // (Blur Y/H sliders, box drag, resize handles) for the current
    // source. Rides the submit payload as blur_box_explicit: an
    // explicit box is authoritative at render. Reset by
    // resetBlurBoxForNewSource() whenever a NEW source loads.
    recapBlurBoxTouched: false,
    // Batch AG: free 2-axis adjustment defaults — blur box left /
    // width and subtitle centre / wrap width, all percents of the
    // PICTURE width. The sliders are the live source of truth; these
    // mirror them for the snapshot / prefs / payload fallbacks.
    recapBlurX: 4,
    recapBlurW: 92,
    recapSubX: 50,
    recapSubW: 92,
    // Batch AK: the subtitle's vertical anchor is the BOTTOM-EDGE
    // margin (percent of the picture height; 5% default, the
    // Recapter standard). state.recapSubY survives only as the
    // derived centre-% the canvas drag math works in.
    recapSubMargin: 5,
    recapSettingsVersion: 2,
    // Batch DD: Step-2 rebuild state — protection level, free
    // subtitle-style controls, the manual blur-box LIST (percents
    // of the picture: {x: left, y: centre, w, h, strength}), the
    // custom-font handle and the subtitle position band. The title
    // overlay state is GONE with the feature.
    recapProtection: "standard",
    recapTextColor: "",
    recapOutlineColor: "",
    recapBgColor: "",
    recapOutlineWidth: null,
    recapBgOpacity: null,
    recapSubShadow: false,
    recapSubBold: true,
    recapBlurBoxes: [],
    recapBlurBoxIndex: 0,
    recapSubFontCustomId: "",
    recapSubFontCustomName: "",
    recapSubPosBand: "bottom",
    // Batch AA: identity of the last successfully loaded source
    // ("url:<url>" / "file:<server path>") — the new-source reset
    // fires only when a success event brings a DIFFERENT source.
    recapLoadedSourceKey: null,
    recapSplitMinutes: 3,
    recapSourceLanguage: "auto",
    recapRenderQuality: "standard",
    // Batch BX: the pre-profile default is the FREE-safe engine —
    // a FREE user's first paint no longer shows the locked clone
    // engine as if it were selected (the tier lock still re-syncs
    // the picker from the profile once it lands).
    recapTtsEngine: "gemini",
    recapGeminiVoice: "",
    recapVoice: "komin",
    // Batch BV: own-voice narration. Per-job state only — it is
    // deliberately NOT collected into the persisted recap settings
    // or the account prefs: an uploaded sample belongs to one job,
    // never to the account.
    recapOwnVoiceSelected: false,
    recapOwnVoiceRefId: null,
    recapOwnVoiceFile: null,
    recapOwnVoiceFileName: "",
    recapOwnVoiceStatus: "idle", // idle | uploading | ready | error
    recapJobId: null,
    recapPollTimer: null,
    recapScriptReadyJobId: null,
    alertDismissTimer: null,
};

// ── Telegram WebApp Initialization & Native Integrations (Phase 1) ─────────
function initLitePerformanceMode() {
    try {
        const forced = localStorage.getItem("vcp_lite_mode");
        if (forced === "0") {
            document.documentElement.classList.remove("lite");
            document.body?.classList.remove("lite");
            return false;
        }
        const cores = Number(navigator.hardwareConcurrency || 8);
        const mem = Number(navigator.deviceMemory || 8);
        const reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        const isLite = forced === "1" || reducedMotion || cores <= 4 || mem <= 3;
        document.documentElement.classList.toggle("lite", isLite);
        if (document.body) document.body.classList.toggle("lite", isLite);
        syncLiteModeUiState(isLite);
        return isLite;
    } catch (e) {
        return false;
    }
}
window.initLitePerformanceMode = initLitePerformanceMode;

function syncLiteModeUiState(isLite) {
    const enabled = !!isLite;
    const btn = document.getElementById("btn-lite-toggle");
    if (btn) {
        btn.setAttribute("aria-pressed", enabled ? "true" : "false");
        btn.classList.toggle("is-active", enabled);
    }
    return enabled;
}

function toggleLiteMode() {
    haptic("light");
    const nextLite = !document.documentElement.classList.contains("lite");
    try {
        localStorage.setItem("vcp_lite_mode", nextLite ? "1" : "0");
    } catch (e) {}
    document.documentElement.classList.toggle("lite", nextLite);
    if (document.body) document.body.classList.toggle("lite", nextLite);
    syncLiteModeUiState(nextLite);
    try {
        showStudioToast(t(nextLite ? "lite_mode_on" : "lite_mode_off"), "info");
    } catch (e) {}
    return nextLite;
}
window.toggleLiteMode = toggleLiteMode;

function syncThemeUiState(mode) {
    const resolved = mode === "dark" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", resolved);
    if (document.body) {
        document.body.setAttribute("data-theme", resolved);
        document.body.classList.toggle("dark-theme", resolved === "dark");
    }
    const btn = document.getElementById("btn-theme-toggle");
    if (btn) {
        btn.setAttribute("data-theme-state", resolved);
        btn.setAttribute("aria-pressed", resolved === "dark" ? "true" : "false");
    }
    try {
        if (state.tg) {
            const chromeColor = resolved === "dark" ? "#070b16" : "#dcebf7";
            state.tg.setHeaderColor(chromeColor);
            state.tg.setBackgroundColor(chromeColor);
        }
    } catch (e) {}
    return resolved;
}

function setThemeMode(mode) {
    const next = mode === "dark" ? "dark" : "light";
    try {
        localStorage.setItem("vcp_theme_mode", next);
    } catch (e) {}
    return syncThemeUiState(next);
}
window.setThemeMode = setThemeMode;

function initThemeMode() {
    let saved = "light";
    try {
        const attr = document.documentElement.getAttribute("data-theme");
        const stored = localStorage.getItem("vcp_theme_mode");
        if (stored === "dark" || stored === "light") saved = stored;
        else if (attr === "dark" || attr === "light") saved = attr;
    } catch (e) {}
    return syncThemeUiState(saved);
}
window.initThemeMode = initThemeMode;

function toggleThemeMode() {
    haptic("light");
    const current = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
    const next = current === "dark" ? "light" : "dark";
    const resolved = setThemeMode(next);
    try {
        showStudioToast(t(resolved === "dark" ? "theme_dark" : "theme_light"), "info");
    } catch (e) {}
    return resolved;
}
window.toggleThemeMode = toggleThemeMode;

/**
 * setSectionState(sectionOrId, stateName, options)
 * Unified UI state machine helper for data-ui-state="loading|empty|error|ready"
 */
function setSectionState(sectionOrId, stateName = "ready", options = {}) {
    const el = typeof sectionOrId === "string" ? document.getElementById(sectionOrId) : sectionOrId;
    if (!el) return;
    const valid = ["loading", "empty", "error", "ready"];
    const nextState = valid.includes(stateName) ? stateName : "ready";
    el.setAttribute("data-ui-state", nextState);
    if (options.message) {
        const msgEl = el.querySelector(`.vcp-state-${nextState} .vcp-state-msg`);
        if (msgEl) msgEl.textContent = options.message;
    }
    if (typeof options.onRetry === "function") {
        const retryBtn = el.querySelector(`.vcp-state-error .vcp-state-retry-btn`);
        if (retryBtn) retryBtn.onclick = options.onRetry;
    }
}
window.setSectionState = setSectionState;

/**
 * Standard vcp-* Component Helpers (Bottom Sheet, Accordion, Slider Reset, Button Loading)
 */
function openVcpSheet(sheetId) {
    const el = typeof sheetId === "string" ? document.getElementById(sheetId) : sheetId;
    if (!el) return;
    haptic("light");
    el.classList.remove("hidden");
    requestAnimationFrame(() => {
        el.classList.add("is-open");
        syncTelegramBackButton();
    });
}
window.openVcpSheet = openVcpSheet;

function closeVcpSheet(sheetId) {
    const el = typeof sheetId === "string" ? document.getElementById(sheetId) : sheetId;
    if (!el) return;
    el.classList.remove("is-open");
    setTimeout(() => {
        if (!el.classList.contains("is-open")) el.classList.add("hidden");
        syncTelegramBackButton();
    }, 240);
}
window.closeVcpSheet = closeVcpSheet;

function toggleVcpAccordion(btnOrId, force) {
    const el = typeof btnOrId === "string" ? document.getElementById(btnOrId) : btnOrId;
    if (!el) return;
    const acc = el.classList.contains("vcp-accordion") ? el : el.closest(".vcp-accordion");
    if (!acc) return;
    haptic("selection");
    const open = typeof force === "boolean" ? force : !(acc.classList.contains("is-open") || acc.classList.contains("open"));
    acc.classList.toggle("is-open", open);
    acc.classList.toggle("open", open);
    const head = acc.querySelector(".vcp-accordion-head, .vcp-accordion__trigger");
    if (head) head.setAttribute("aria-expanded", open ? "true" : "false");
    const body = acc.querySelector(".vcp-accordion-body, .vcp-accordion__body");
    if (body) body.classList.toggle("hidden", !open);
}
window.toggleVcpAccordion = toggleVcpAccordion;

function resetVcpSlider(sliderId, defaultVal) {
    const slider = typeof sliderId === "string" ? document.getElementById(sliderId) : sliderId;
    if (!slider) return;
    haptic("light");
    const val = defaultVal !== undefined ? defaultVal : slider.getAttribute("data-default");
    if (val === null || val === undefined) return;
    slider.value = val;
    slider.dispatchEvent(new Event("input", { bubbles: true }));
    slider.dispatchEvent(new Event("change", { bubbles: true }));
}
window.resetVcpSlider = resetVcpSlider;

function setVcpButtonLoading(btnOrId, loading) {
    const btn = typeof btnOrId === "string" ? document.getElementById(btnOrId) : btnOrId;
    if (!btn) return;
    btn.classList.toggle("is-loading", !!loading);
    btn.disabled = !!loading;
}
window.setVcpButtonLoading = setVcpButtonLoading;

function closeTopmostOverlay() {
    const confirmModal = document.getElementById("studio-confirm-modal");
    if (confirmModal) { confirmModal.remove(); return true; }
    const inputModal = document.getElementById("studio-input-modal");
    if (inputModal) { inputModal.remove(); return true; }
    const openSheets = document.querySelectorAll(".vcp-sheet-backdrop.is-open");
    if (openSheets.length > 0) {
        closeVcpSheet(openSheets[openSheets.length - 1]);
        return true;
    }
    const overlayIds = [
        ["recap-lang-sheet-backdrop", () => { if (typeof window !== "undefined" && typeof window.closeRecapLangSheet === "function") window.closeRecapLangSheet(); else document.getElementById("recap-lang-sheet-backdrop")?.classList.add("hidden"); }],
        ["open-via-telegram-modal", () => { if (typeof closeOpenViaTelegramModal === "function") closeOpenViaTelegramModal(); else document.getElementById("open-via-telegram-modal")?.classList.add("hidden"); }],
        ["live-jobs-sheet", closeLiveJobsSheet],
        ["income-detail-sheet", closeIncomeDetail],
        ["slip-modal", () => { if (typeof closeSlipModal === "function") closeSlipModal(); else document.getElementById("slip-modal")?.classList.add("hidden"); }],
        ["tts-key-prompt-modal", () => document.getElementById("tts-key-prompt-modal")?.classList.add("hidden")],
        ["admin-media-modal", () => { if (typeof closeAdminMediaModal === "function") closeAdminMediaModal(); else document.getElementById("admin-media-modal")?.classList.add("hidden"); }],
        ["adm-user-detail", () => { if (typeof closeAdminUserDetail === "function") closeAdminUserDetail(); else document.getElementById("adm-user-detail")?.classList.add("hidden"); }],
        ["payment-box", closePaymentDrawer],
    ];
    for (const [id, closer] of overlayIds) {
        const el = document.getElementById(id);
        if (el && !el.classList.contains("hidden")) {
            closer();
            syncTelegramBackButton();
            return true;
        }
    }
    return false;
}
window.closeTopmostOverlay = closeTopmostOverlay;

function hasOpenOverlay() {
    if (document.getElementById("studio-confirm-modal") || document.getElementById("studio-input-modal")) return true;
    if (document.querySelector(".vcp-sheet-backdrop.is-open")) return true;
    const ids = ["recap-lang-sheet-backdrop", "open-via-telegram-modal", "live-jobs-sheet", "income-detail-sheet", "slip-modal", "tts-key-prompt-modal", "admin-media-modal", "adm-user-detail", "payment-box"];
    return ids.some(id => {
        const el = document.getElementById(id);
        return !!(el && !el.classList.contains("hidden"));
    });
}
if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && hasOpenOverlay()) {
            closeTopmostOverlay();
        }
    });
}

function syncTelegramBackButton() {
    try {
        const bb = state.tg?.BackButton;
        if (!bb) return;
        const activePane = document.querySelector(".tab-pane.active");
        const activeTabId = activePane ? activePane.id : "tab-home";
        const shouldShow = hasOpenOverlay()
            || (activeTabId === "tab-recap" && (state.recapDetailOpen || (typeof recapCurrentStep === "number" && recapCurrentStep > 1)))
            || (activeTabId === "tab-voice" && state.voiceDetailOpen)
            || (activeTabId !== "tab-home");
        if (shouldShow) {
            if (typeof bb.show === "function") bb.show();
        } else {
            if (typeof bb.hide === "function") bb.hide();
        }
    } catch (e) {}
}
window.syncTelegramBackButton = syncTelegramBackButton;

function handleTelegramBackButton() {
    haptic("light");
    if (closeTopmostOverlay()) {
        syncTelegramBackButton();
        return;
    }
    const activePane = document.querySelector(".tab-pane.active");
    const activeTabId = activePane ? activePane.id : "tab-home";
    if (activeTabId === "tab-recap") {
        if (state.recapDetailOpen && typeof window.backToStudio === "function") {
            window.backToStudio("recap");
            syncTelegramBackButton();
            return;
        }
        if (typeof recapCurrentStep === "number" && recapCurrentStep > 1 && typeof window.showRecapStep === "function") {
            window.showRecapStep(recapCurrentStep - 1);
            syncTelegramBackButton();
            return;
        }
    }
    if (activeTabId === "tab-voice" && state.voiceDetailOpen && typeof window.backToStudio === "function") {
        window.backToStudio("voice");
        syncTelegramBackButton();
        return;
    }
    if (activeTabId !== "tab-home") {
        switchTab("tab-home");
        syncTelegramBackButton();
    }
}
window.handleTelegramBackButton = handleTelegramBackButton;

function setupViewportAndKeyboardSync() {
    const applyViewportVars = () => {
        try {
            const vh = (state.tg && state.tg.viewportHeight) ? state.tg.viewportHeight : window.innerHeight;
            const sh = (state.tg && state.tg.viewportStableHeight) ? state.tg.viewportStableHeight : window.innerHeight;
            if (vh > 0) document.documentElement.style.setProperty("--tg-viewport-height", `${vh}px`);
            if (sh > 0) document.documentElement.style.setProperty("--tg-viewport-stable-height", `${sh}px`);
        } catch (e) {}
    };
    applyViewportVars();
    window.addEventListener("resize", applyViewportVars);
    if (state.tg && typeof state.tg.onEvent === "function") {
        try { state.tg.onEvent("viewportChanged", applyViewportVars); } catch (e) {}
    }
    document.addEventListener("focusin", (e) => {
        const tEl = e.target;
        if (!tEl || !tEl.matches || !tEl.matches("input:not([type='range']):not([type='file']):not([type='checkbox']), textarea, select")) return;
        document.body.classList.add("vcp-kb-open");
        setTimeout(() => {
            try { tEl.scrollIntoView({ behavior: "smooth", block: "center" }); } catch (err) {}
        }, 260);
    });
    document.addEventListener("focusout", () => {
        setTimeout(() => {
            const active = document.activeElement;
            if (!active || !active.matches || !active.matches("input, textarea, select")) {
                document.body.classList.remove("vcp-kb-open");
            }
        }, 150);
    });
}
window.setupViewportAndKeyboardSync = setupViewportAndKeyboardSync;

function hasUnsavedWork() {
    if (state.recapUploadPending) return true;
    const slipInput = document.getElementById("slip-file-input");
    if (slipInput && slipInput.files && slipInput.files.length > 0) return true;
    const scriptBox = document.getElementById("recap-script-box");
    if (scriptBox && !scriptBox.classList.contains("hidden")) {
        const segs = scriptBox.querySelectorAll(".script-seg-text");
        for (const ta of segs) {
            if (ta.value !== ta.dataset.original) return true;
        }
    }
    return false;
}
window.hasUnsavedWork = hasUnsavedWork;

function syncUnsavedWorkGuard() {
    const dirty = hasUnsavedWork();
    try {
        if (state.tg) {
            if (dirty && typeof state.tg.enableClosingConfirmation === "function") {
                state.tg.enableClosingConfirmation();
            } else if (!dirty && typeof state.tg.disableClosingConfirmation === "function") {
                state.tg.disableClosingConfirmation();
            }
        }
    } catch (e) {}
}
window.syncUnsavedWorkGuard = syncUnsavedWorkGuard;

window.addEventListener("beforeunload", (e) => {
    if (hasUnsavedWork()) {
        e.preventDefault();
        e.returnValue = "";
    }
});

function initTelegram() {
    initLitePerformanceMode();
    initThemeMode();
    if (state.tg) {
        state.tg.ready();
        state.tg.expand();
        try {
            const chromeColor = document.documentElement.getAttribute("data-theme") === "dark" ? "#070b16" : "#dcebf7";
            state.tg.setHeaderColor(chromeColor);
            state.tg.setBackgroundColor(chromeColor);
        } catch (e) {}
        try {
            if (state.tg.BackButton && typeof state.tg.BackButton.onClick === "function") {
                state.tg.BackButton.onClick(handleTelegramBackButton);
            }
        } catch (e) {}
    }
}

function haptic(type = "light") {
    // Route each semantic type to the correct Telegram HapticFeedback API:
    // success/error/warning are NOT impact styles — they belong to
    // notificationOccurred(); "selection" belongs to selectionChanged().
    // Everything is feature-detected so non-Telegram contexts never break.
    try {
        const hf = state.tg?.HapticFeedback;
        if (!hf) return;
        if (type === "success" || type === "error" || type === "warning") {
            if (typeof hf.notificationOccurred === "function") {
                hf.notificationOccurred(type);
                return;
            }
        }
        if (type === "selection") {
            if (typeof hf.selectionChanged === "function") {
                hf.selectionChanged();
                return;
            }
        }
        if (typeof hf.impactOccurred === "function") {
            const style = ["light", "medium", "heavy", "rigid", "soft"].includes(type) ? type : "light";
            hf.impactOccurred(style);
        }
    } catch (e) {}
}
window.haptic = haptic;

// ── Fetch with timeout (premium polish) ────────────────────────────────
// Data loaders use this so a hung connection ends in the loader's error
// state (with Retry) instead of an endless spinner.
const FETCH_TIMEOUT_MS = 12000;
// Telegram sends are different: the server uploads up to 50 MB to
// Telegram before it can answer, so the send-telegram call gets a
// generous-but-finite cap instead of spinning forever (Batch AF).
const TELEGRAM_SEND_TIMEOUT_MS = 180000;
async function fetchWithTimeout(url, options = {}, timeoutMs = FETCH_TIMEOUT_MS) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
        return await fetch(url, { ...options, signal: ctrl.signal });
    } finally {
        clearTimeout(timer);
    }
}
window.fetchWithTimeout = fetchWithTimeout;

// True when a fetch died before ANY response arrived — connection
// closed/reset, DNS failure, or our own timeout abort (AbortError).
// An HTTP error status is NOT a network-level error: fetch resolves
// and the caller throws Error(detail) instead. (Q2, QA cycle
// 2026-11-02: the Recap Analyze extract call for a Short died this
// way — ERR_CONNECTION_CLOSED after ~26s, no response at all.)
function isNetworkLevelError(err) {
    if (!err) return false;
    if (err.name === "AbortError") return true;
    return err instanceof TypeError
        && /failed to fetch|networkerror|load failed/i.test(err.message || "");
}

// ── Home load error strip (premium polish) ─────────────────────────────
function setHomeLoadErrorVisible(visible) {
    const strip = document.getElementById("home-load-error");
    if (strip) strip.classList.toggle("hidden", !visible);
}
function retryHomeLoad() {
    setHomeLoadErrorVisible(false);
    loadUserProfile();
    loadCuratedVoices();
}
window.retryHomeLoad = retryHomeLoad;

// ── Staged progress steppers (premium polish) ──────────────────────────
// Honest stage chips mapped to REAL job states only — no fake progress:
//   Voice:  submit → Script active · QUEUED/RUNNING → Voice active ·
//           SUCCESS → all done.
//   Recap:  server progress_step "N/4" — 0–2 are the script phase
//           (download/transcribe/translate), 3 is voice+video render
//           (script & voice complete, video active), 4/SUCCESS → done.
//           SCRIPT_READY pauses with Script done, Voice next.
function renderStageStepper(el, labels, activeIndex, completeAll) {
    if (!el) return;
    const key = labels.join("|") + "#" + (completeAll ? "done" : activeIndex) + "#" + currentLang;
    if (el.dataset.stageKey === key) return;
    el.dataset.stageKey = key;
    el.innerHTML = labels.map((label, i) => {
        const cls = completeAll || i < activeIndex ? "done" : (i === activeIndex ? "active" : "upcoming");
        const mark = cls === "done" ? "✓ " : "";
        return `<span class="stage-chip ${cls}">${mark}${label}</span>${i < labels.length - 1 ? '<span class="stage-arrow">›</span>' : ""}`;
    }).join("");
}
function voiceStageLabels() { return [t("stage_script"), t("stage_voice"), t("stage_ready")]; }
function recapStageLabels() { return [t("recap_stage_download_lbl"), t("recap_stage_script_lbl"), t("recap_stage_voice_lbl"), t("recap_stage_render_lbl")]; }
function setVoiceStage(mode) {
    const el = document.getElementById("voice-stage-stepper");
    if (!el) return;
    if (mode === "success") renderStageStepper(el, voiceStageLabels(), 2, true);
    else if (mode === "working") renderStageStepper(el, voiceStageLabels(), 1, false);
    else renderStageStepper(el, voiceStageLabels(), 0, false);
}
function setRecapStage(stepStr, status) {
    const el = document.getElementById("recap-stage-stepper");
    if (!el) return;
    if (status === "SUCCESS") { renderStageStepper(el, recapStageLabels(), 3, true); return; }
    if (status === "SCRIPT_READY") { renderStageStepper(el, recapStageLabels(), 1, false); return; }
    if (status === "WAITING_CHOICE") { renderStageStepper(el, recapStageLabels(), 2, false); return; }
    const n = parseInt(String(stepStr || "0").split("/")[0], 10) || 0;
    if (n >= 4) renderStageStepper(el, recapStageLabels(), 3, false);
    else if (n === 3) renderStageStepper(el, recapStageLabels(), 2, false);
    else if (n === 2) renderStageStepper(el, recapStageLabels(), 1, false);
    else renderStageStepper(el, recapStageLabels(), 0, false);
}
window.setVoiceStage = setVoiceStage;
window.setRecapStage = setRecapStage;

// ── Sticky create CTAs (premium polish) ────────────────────────────────
// Mirror bars for the Voice / Recap primary actions. A bar appears only
// while its tab is active AND the real in-flow button is scrolled out of
// view; clicks forward to the real button (no duplicate logic), and the
// disabled state mirrors the real button.
const STICKY_CTA_PAIRS = [
    { tab: "tab-voice", bar: "sticky-cta-voice", src: "btn-generate-voice", btn: "btn-generate-voice-sticky" },
    { tab: "tab-recap", bar: "sticky-cta-recap", src: "btn-start-recap", btn: "btn-start-recap-sticky" },
];
const stickyCtaInView = {};
// Batch AK (Addition C, owner's direct order 2026-11-05): while the
// user has an unfinished recap job, EVERY "1-Click" create CTA is
// hidden — the job-status bar is the progress surface (and the
// backend already allows only one active recap job per user).
function recapJobActive() {
    try {
        if (LiveJobs.activeJobs().some(j => j.kind === "recap")) return true;
        return typeof recapJobIsActive === "function" && recapJobIsActive();
    } catch (e) { return false; }
}
window.recapJobActive = recapJobActive;
function refreshStickyCtas() {
    STICKY_CTA_PAIRS.forEach(p => {
        const bar = document.getElementById(p.bar);
        const src = document.getElementById(p.src);
        const btn = document.getElementById(p.btn);
        if (!bar || !src || !btn) return;
        const pane = document.getElementById(p.tab);
        const tabActive = !!(pane && pane.classList.contains("active"));
        btn.disabled = !!src.disabled;
        const recapBlocked = p.src === "btn-start-recap" && recapJobActive();
        if (p.src === "btn-start-recap") src.classList.toggle("hidden", recapBlocked);
        // Batch BO (owner 2026-11-06 19:56): the recap 1-Click CTA
        // shows ONLY while the wizard's final step (Step 4) is the
        // current step. The source button lives inside the Step 4
        // card, so on Steps 1–3 it sits off-screen and the sticky
        // bar would otherwise float the create action over the
        // earlier steps. The gate COMBINES with the AK job-active
        // hide and the tab/scroll conditions — it replaces none of
        // them — and showRecapStep (the wizard's one door) calls
        // back into this refresh on every step change, so boot,
        // forward nav, back nav and chip taps all obey it.
        const recapStepGate = p.src === "btn-start-recap" && recapCurrentStep !== 4;
        bar.classList.toggle("hidden", recapBlocked || recapStepGate || !(tabActive && stickyCtaInView[p.src] === false));
        if (p.src === "btn-start-recap" && !document.getElementById("recap-result-box")?.classList.contains("hidden")) bar.classList.add("hidden");
    });
}
window.refreshStickyCtas = refreshStickyCtas;
function setupStickyCtas() {
    if (!("IntersectionObserver" in window)) return;
    const io = new IntersectionObserver((entries) => {
        entries.forEach(en => { stickyCtaInView[en.target.id] = en.isIntersecting; });
        refreshStickyCtas();
    }, { threshold: 0.01 });
    STICKY_CTA_PAIRS.forEach(p => {
        const src = document.getElementById(p.src);
        const btn = document.getElementById(p.btn);
        if (src) {
            // Recap tidy-up: the in-flow recap button is a hidden mirror
            // source only — the sticky bar is the one visible CTA. Done
            // here (not in markup/CSS) so a no-IntersectionObserver
            // WebView keeps the working in-flow button instead.
            if (p.src === "btn-start-recap") src.classList.add("cta-source-hidden");
            io.observe(src);
            new MutationObserver(refreshStickyCtas).observe(src, { attributes: true, attributeFilter: ["disabled"] });
        }
        if (btn && src) btn.addEventListener("click", () => { if (!btn.disabled) src.click(); });
    });
    refreshStickyCtas();
}

// Home sample players: lazy-load on first Play tap, one plays at a time
function setupSamplePlayers() {
    const a = document.getElementById("sample-audio-voice");
    const b = document.getElementById("sample-audio-recap");
    if (a && b) {
        a.addEventListener("play", () => { try { b.pause(); } catch (e) {} });
        b.addEventListener("play", () => { try { a.pause(); } catch (e) {} });
    }
    const ensureSampleSrc = async (el) => {
        if (!el || el.src) return !!el?.src;
        const url = el.getAttribute("data-sample-url");
        if (!url) return false;
        const authUrl = await fetchAuthImageUrl(url);
        if (authUrl) el.src = authUrl;
        return !!el.src;
    };
    const wireSampleButton = (el, btnId, fillId) => {
        const btn = document.getElementById(btnId);
        if (!el || !btn) return;
        const icon = btn.querySelector(".sample-play-icon");
        const fill = document.getElementById(fillId);
        const render = () => {
            const playing = !el.paused && !el.ended;
            if (icon) icon.textContent = playing ? "⏸" : "▶";
            btn.classList.toggle("playing", playing);
            btn.setAttribute("aria-label", t(playing ? "sample_pause" : "sample_play"));
            if (el.ended && fill) fill.style.width = "0%";
        };
        btn.addEventListener("click", async () => {
            if (el.paused || el.ended) {
                if (!(await ensureSampleSrc(el))) return;
                const p = el.play();
                if (p && p.catch) p.catch(() => {});
            } else {
                el.pause();
            }
        });
        ["play", "pause", "ended"].forEach((ev) => el.addEventListener(ev, render));
        el.addEventListener("timeupdate", () => {
            if (fill && el.duration > 0) {
                fill.style.width = `${Math.min(100, (el.currentTime / el.duration) * 100)}%`;
            }
        });
        render();
    };
    wireSampleButton(a, "sample-btn-voice", "sample-progress-voice");
    wireSampleButton(b, "sample-btn-recap", "sample-progress-recap");
}

// ── Home tutorials accordion (Batch AY + owner addendum): the
// card's header row is the toggle; the full guide list unfolds in
// place inside the card and collapses again on the next tap. ──────
function toggleHomeTutorials() {
    const more = document.getElementById("home-tut-more");
    const btn = document.getElementById("home-tut-expand");
    if (!more || !btn) return;
    const open = more.classList.toggle("hidden") === false;
    btn.classList.toggle("open", open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
}

function getAuthHeaders(includeContentType = true) {
    const headers = {};
    if (includeContentType) {
        headers["Content-Type"] = "application/json";
    }
    if (state.tg && state.tg.initData) {
        headers["X-Telegram-Init-Data"] = state.tg.initData;
    }
    return headers;
}
window.getAuthHeaders = getAuthHeaders;

// ── Batch AC: LiveJobs manager (global live layer) ─────────────────────
// ONE global transport for job state: a single adaptive poll of
// GET /api/jobs/live carrying every active job plus jobs that turned
// terminal in the last ~10 minutes. It feeds the progress pill, the
// jobs sheet and in-place History updates. The per-screen detail
// pollers keep rendering their rich detail views (download URLs,
// script editor, fallback segments — payloads the bulk endpoint
// deliberately does not carry), but they render through the SAME
// monotonic merge below, so a reconnect can never move a displayed
// stage backwards (the old recap reset-to-1/4 bug).
const LIVE_JOB_TERMINAL_STATUSES = ["SUCCESS", "FAILED", "CANCELLED"];
function liveJobsIsTerminal(status) {
    return LIVE_JOB_TERMINAL_STATUSES.includes(String(status || "").toUpperCase());
}
function liveJobsIsActive(status) {
    return ["PENDING", "QUEUED", "RUNNING", "SCRIPT_READY", "WAITING_CHOICE"].includes(String(status || "").toUpperCase());
}
function liveJobsMergeJob(prev, next) {
    if (!next) return prev || null;
    if (!prev) {
        const fresh = { ...next };
        fresh._terminalRendered = false;
        return fresh;
    }
    const merged = { ...prev, ...next };
    // Terminal state is sticky: once a job is done it never flips back.
    if (liveJobsIsTerminal(prev.status)) {
        merged.status = prev.status;
        merged.progress_percent = prev.progress_percent;
        merged.stage_index = prev.stage_index;
        merged.stage_total = prev.stage_total;
    } else if (!liveJobsIsTerminal(next.status)) {
        // MONOTONIC: displayed progress/stage never go backwards.
        merged.progress_percent = Math.max(prev.progress_percent || 0, next.progress_percent || 0);
        merged.stage_index = Math.max(prev.stage_index || 0, next.stage_index || 0);
        merged.stage_total = next.stage_total || prev.stage_total || 0;
    }
    merged._terminalRendered = !!prev._terminalRendered;
    return merged;
}

let liveJobsFailedPillJob = null;
let liveJobsFailedPillTimer = null;

function setLiveJobsFailedToast(job) {
    if (!job) return;
    liveJobsFailedPillJob = job;
    if (liveJobsFailedPillTimer) {
        clearTimeout(liveJobsFailedPillTimer);
        liveJobsFailedPillTimer = null;
    }
    liveJobsFailedPillTimer = setTimeout(() => {
        clearLiveJobsFailedToast();
    }, 15000);
    renderLiveJobsPill();
}
window.setLiveJobsFailedToast = setLiveJobsFailedToast;

function clearLiveJobsFailedToast() {
    if (liveJobsFailedPillTimer) {
        clearTimeout(liveJobsFailedPillTimer);
        liveJobsFailedPillTimer = null;
    }
    liveJobsFailedPillJob = null;
    renderLiveJobsPill();
}
window.clearLiveJobsFailedToast = clearLiveJobsFailedToast;

const LiveJobs = {
    jobs: new Map(),
    subscribers: [],
    timer: null,
    started: false,
    failStreak: 0,
    reconnecting: false,

    subscribe(fn) {
        if (typeof fn === "function") LiveJobs.subscribers.push(fn);
    },
    notify(job, opts) {
        LiveJobs.subscribers.forEach(fn => { try { fn(job, opts || {}); } catch (e) {} });
        renderLiveJobsPill();
        renderLiveJobsSheet();
    },
    hasActiveJob() {
        for (const j of LiveJobs.jobs.values()) {
            if (liveJobsIsActive(j.status)) return true;
        }
        return false;
    },
    activeJobs() {
        return [...LiveJobs.jobs.values()].filter(j => liveJobsIsActive(j.status));
    },
    track(job) {
        if (!job || !job.id) return;
        const prev = LiveJobs.jobs.get(job.id);
        const merged = liveJobsMergeJob(prev, job);
        const isNewTerminal = liveJobsIsTerminal(merged.status) && !(prev && prev._terminalRendered);
        if (isNewTerminal) merged._terminalRendered = true;
        LiveJobs.jobs.set(job.id, merged);
        if (isNewTerminal && merged.status === "FAILED") {
            setLiveJobsFailedToast(merged);
        } else if (isNewTerminal && merged.status === "SUCCESS") {
            clearLiveJobsFailedToast();
        }
        LiveJobs.notify(merged, { terminal: isNewTerminal });
    },
    applySnapshot(list) {
        const seen = new Set();
        (Array.isArray(list) ? list : []).forEach(raw => {
            if (!raw || !raw.id) return;
            seen.add(raw.id);
            const prev = LiveJobs.jobs.get(raw.id);
            const merged = liveJobsMergeJob(prev, raw);
            const isNewTerminal = liveJobsIsTerminal(merged.status) && !(prev && prev._terminalRendered);
            if (isNewTerminal) merged._terminalRendered = true;
            LiveJobs.jobs.set(raw.id, merged);
            if (isNewTerminal && merged.status === "FAILED") {
                setLiveJobsFailedToast(merged);
            } else if (isNewTerminal && merged.status === "SUCCESS") {
                clearLiveJobsFailedToast();
            }
            LiveJobs.notify(merged, { terminal: isNewTerminal });
            if (isNewTerminal) applyLiveJobToHistory(merged);
        });
        // Active jobs the server no longer reports are gone (purged /
        // never persisted): drop them instead of haunting the pill.
        for (const [id, j] of [...LiveJobs.jobs.entries()]) {
            if (!seen.has(id) && liveJobsIsActive(j.status)) {
                LiveJobs.jobs.delete(id);
                LiveJobs.notify(j, { removed: true });
            }
        }
        syncRecapStateFromLiveJobs();
        renderLiveJobsPill();
        renderLiveJobsSheet();
    },
    async fetchNow() {
        try {
            const res = await fetch("/api/jobs/live", { headers: getAuthHeaders() });
            if (!res.ok) throw new Error("live " + res.status);
            const data = await res.json();
            LiveJobs.failStreak = 0;
            if (LiveJobs.reconnecting) {
                LiveJobs.reconnecting = false;
                renderLiveJobsPill();
            }
            LiveJobs.applySnapshot(data.jobs || []);
        } catch (e) {
            // A failed fetch keeps the last known state — never a reset.
            LiveJobs.failStreak++;
            if (LiveJobs.failStreak >= 3 && !LiveJobs.reconnecting) {
                LiveJobs.reconnecting = true;
                renderLiveJobsPill();
            }
        }
        LiveJobs.schedule();
    },
    schedule() {
        if (LiveJobs.timer) clearTimeout(LiveJobs.timer);
        LiveJobs.timer = null;
        if (typeof document !== "undefined" && document.hidden) return; // PAUSED while hidden
        const active = LiveJobs.hasActiveJob();
        const delay = active ? (LiveJobs.failStreak > 0 ? 5000 : 2000) : 15000;
        LiveJobs.timer = setTimeout(() => { LiveJobs.fetchNow(); }, delay);
    },
    start() {
        if (LiveJobs.started) return;
        LiveJobs.started = true;
        document.addEventListener("visibilitychange", () => {
            if (document.hidden) {
                if (LiveJobs.timer) clearTimeout(LiveJobs.timer);
                LiveJobs.timer = null;
            } else {
                LiveJobs.fetchNow(); // immediate catch-up on return
            }
        });
        window.addEventListener("focus", () => { LiveJobs.fetchNow(); });
        LiveJobs.fetchNow();
    },
};
window.LiveJobs = LiveJobs;

// ── Batch AC: progress pill + jobs sheet ───────────────────────────────
function liveJobStageText(job) {
    if (!job) return "";
    // Batch AK (Addition C): a terminal job reports its OUTCOME, not
    // the last in-progress step — finished rows and the app-top
    // status line used to keep stale step text after completion.
    if (liveJobsIsTerminal(job.status)) {
        if (job.status === "SUCCESS") return t("live_job_status_success");
        if (job.status === "FAILED") return t("live_job_status_failed");
        if (job.status === "CANCELLED") return t("live_job_status_cancelled");
        return job.status || "";
    }
    if (job.status === "QUEUED") {
        return job.queue_position
            ? tf("live_job_queued_pos", { pos: job.queue_position })
            : t("live_job_queued");
    }
    if (job.status === "SCRIPT_READY") return t("hist_script_ready_badge");
    // Batch DP: a parked job is waiting on its owner's voice choice
    // (bot prompt) — name that state instead of a stale stage label.
    if (job.status === "WAITING_CHOICE") return t("live_job_status_waiting_choice");
    if (job.progress_stage) {
        const stageText = t("recap_stage_" + job.progress_stage);
        if (stageText && stageText !== "recap_stage_" + job.progress_stage) return stageText;
    }
    if (job.progress_label) return job.progress_label;
    if (job.kind === "voice") return t("voice_synthesizing");
    return t("recap_processing_msg");
}
// Batch CB Item 4: the overrun status sentence — composed from the
// store job itself (its stage text + its elapsed counter, the same
// values the pill/sheet/form already render) so every surface says
// the identical sentence when the server flags eta_overrun.
function liveJobOverrunText(job) {
    if (!job) return "";
    const stage = liveJobStageText(job);
    const elapsed = (typeof job.elapsed_seconds === "number" && job.elapsed_seconds > 0)
        ? formatRecapQuoteDuration(job.elapsed_seconds) : "";
    return tf("recap_eta_overrun", { stage, elapsed });
}
// Batch AK (Addition C): the status bar can be dismissed; the
// dismissal is keyed to the current active set + lead stage, so
// any state change (a new job, a stage change) shows it again.
let liveJobsPillDismissedSig = "";
function liveJobsPillSignature(active) {
    const lead = [...active].sort((a, b) => (b.progress_percent || 0) - (a.progress_percent || 0))[0];
    return active.map(j => j.id).sort().join(",") + "|" + (lead ? (lead.progress_stage || lead.status || "") : "");
}
function dismissLiveJobsPill() {
    clearLiveJobsFailedToast();
    liveJobsPillDismissedSig = liveJobsPillSignature(LiveJobs.activeJobs());
    const pill = document.getElementById("live-jobs-pill");
    if (pill) pill.classList.add("hidden");
}
window.dismissLiveJobsPill = dismissLiveJobsPill;
function paintProgressBarFill(el, job) {
    if (!el || !job) return;
    const waiting = job.status === "WAITING_CHOICE";
    const pct = Math.min(100, Math.max(0, Number(job.progress_percent) || 0));
    const terminal = liveJobsIsTerminal(job.status);
    const indeterminate = !terminal && (waiting || job.status === "QUEUED" || pct <= 0);
    el.classList.toggle("indeterminate", indeterminate);
    el.classList.toggle("is-waiting", waiting);
    if (terminal) {
        el.style.width = job.status === "SUCCESS" ? "100%" : "0%";
    } else if (indeterminate) {
        el.style.width = "100%";
    } else {
        el.style.width = `${pct}%`;
    }
}
function syncRecapStateFromLiveJobs() {
    try {
        const active = LiveJobs.activeJobs().find(j => j.kind === "recap");
        if (active) {
            if (typeof state !== "undefined" && !state.recapJobId) state.recapJobId = active.id;
            if (typeof state !== "undefined") {
                if (active.status === "SCRIPT_READY") state.recapScriptReadyJobId = active.id;
                else if (state.recapScriptReadyJobId === active.id) state.recapScriptReadyJobId = null;
            }
            if (!recapFormJobId) {
                recapFormJobId = active.id;
                if (typeof ensureRecapFormStoreSubscription === "function") ensureRecapFormStoreSubscription();
            }
            if (recapFormJobId === active.id && typeof renderRecapFormProgress === "function") {
                renderRecapFormProgress(active);
            }
        } else if (typeof state !== "undefined") {
            state.recapScriptReadyJobId = null;
        }
        if (typeof setSubmitButtonState === "function") setSubmitButtonState("recap");
    } catch (e) {}
}
function renderLiveJobsPill() {
    const pill = document.getElementById("live-jobs-pill");
    if (!pill) return;
    const active = LiveJobs.activeJobs();
    // The 1-Click CTA hide (Batch AK) tracks the same active set.
    if (typeof refreshStickyCtas === "function") refreshStickyCtas();

    if (liveJobsFailedPillJob) {
        pill.classList.remove("hidden");
        pill.classList.add("is-failed");
        const countEl = document.getElementById("live-jobs-pill-count");
        const stageEl = document.getElementById("live-jobs-pill-stage");
        const fillEl = document.getElementById("live-jobs-pill-fill");
        if (countEl) countEl.innerText = "❌";
        const title = liveJobsFailedPillJob.title || liveJobsFailedPillJob.input_snippet || (liveJobsFailedPillJob.kind === "recap" ? "Movie Recap" : "Voice Synthesis");
        const failedText = t("live_job_status_failed") || "Failed";
        if (stageEl) stageEl.innerText = `${title} · ${failedText}`;
        if (fillEl) {
            fillEl.classList.remove("indeterminate", "is-waiting");
            fillEl.style.width = "0%";
        }
        return;
    }

    pill.classList.remove("is-failed");
    if (!active.length) {
        pill.classList.add("hidden");
        liveJobsPillDismissedSig = "";
        return;
    }
    if (liveJobsPillSignature(active) === liveJobsPillDismissedSig) {
        pill.classList.add("hidden");
        return;
    }
    pill.classList.remove("hidden");
    const lead = [...active].sort((a, b) => (b.progress_percent || 0) - (a.progress_percent || 0))[0];
    const countEl = document.getElementById("live-jobs-pill-count");
    const stageEl = document.getElementById("live-jobs-pill-stage");
    const fillEl = document.getElementById("live-jobs-pill-fill");
    if (countEl) countEl.innerText = tf("live_jobs_count", { count: active.length });
    // Batch CB Item 4: an overrunning lead job replaces the pill's
    // stage text with the honest overrun sentence.
    if (stageEl) stageEl.innerText = LiveJobs.reconnecting
        ? t("poll_reconnecting")
        : (lead.eta_overrun && !liveJobsIsTerminal(lead.status) ? liveJobOverrunText(lead) : liveJobStageText(lead));
    paintProgressBarFill(fillEl, lead);
}
function liveJobsSheetRowHtml(job) {
    const isRecap = job.kind === "recap";
    const isTts = job.kind === "tts";
    const title = escapeHtml(job.title || (isRecap ? "Movie Recap" : isTts ? "AI TTS" : "Voice Synthesis"));
    const pct = Math.min(100, Math.max(0, job.progress_percent || 0));
    const isWaiting = job.status === "WAITING_CHOICE";
    const isTerminal = liveJobsIsTerminal(job.status);
    const isIndet = !isTerminal && (isWaiting || job.status === "QUEUED" || pct <= 0);
    const barW = isTerminal ? (job.status === "SUCCESS" ? 100 : 0) : (isIndet ? 100 : pct);
    const fillCls = `live-job-row-fill vcp-job-progress-fill${isIndet ? " indeterminate" : ""}${isWaiting ? " is-waiting" : ""}`;
    const elapsed = (typeof job.elapsed_seconds === "number")
        ? tf("live_job_elapsed", { sec: job.elapsed_seconds }) : "";
    let action = "";
    if (liveJobsIsActive(job.status)) {
        action = `<button type="button" class="mini-chip" onclick="liveJobOpen('${job.id}','${job.kind}')">${t("studio_inline_view")}</button>`
            + ` <button type="button" class="mini-chip" onclick="liveJobCancel('${job.id}','${job.kind}')">${t("btn_cancel_job")}</button>`;
    } else if (liveJobsIsTerminal(job.status)) {
        action = `<button type="button" class="mini-chip" onclick="liveJobOpen('${job.id}','${job.kind}')">${t("live_job_view_result")}</button>`;
    }
    // Batch AK (Addition C): terminal rows lead with the outcome
    // label alone (no "SUCCESS · Done" duplication, no stale step).
    // Batch CB Item 4: an active job the server flags eta_overrun
    // leads with the overrun sentence instead (it already carries
    // the stage + elapsed, so the separate elapsed suffix is off).
    const overrun = !!job.eta_overrun && !liveJobsIsTerminal(job.status);
    const metaText = liveJobsIsTerminal(job.status)
        ? escapeHtml(liveJobStageText(job))
        : (isWaiting
            ? escapeHtml(liveJobStageText(job))
            : (overrun
                ? escapeHtml(liveJobOverrunText(job))
                : `${escapeHtml(job.status || "")} · ${escapeHtml(liveJobStageText(job))}`));
    return `<div class="live-job-row vcp-job-card" data-live-job-id="${job.id}">
        <div class="vcp-job-head">
            <span class="hub-type-icon ${isRecap ? "recap" : isTts ? "tts" : "voice"}">${isRecap ? "🎬" : isTts ? "🎧" : "🎙️"}</span>
            <div class="live-job-row-main">
                <div class="live-job-row-title">${title}</div>
                <div class="live-job-row-meta">${metaText}${elapsed && !overrun && !isWaiting ? " · " + escapeHtml(elapsed) : ""}</div>
            </div>
        </div>
        <div class="live-job-row-bar vcp-job-progress"><span class="${fillCls}" style="width:${barW}%"></span></div>
        ${action ? `<div class="live-job-row-actions vcp-job-footer-row">${action}</div>` : ""}
    </div>`;
}
function renderLiveJobsSheet() {
    const list = document.getElementById("live-jobs-sheet-list");
    if (!list) return;
    const all = [...LiveJobs.jobs.values()];
    const active = all.filter(j => liveJobsIsActive(j.status));
    const recent = all.filter(j => liveJobsIsTerminal(j.status));
    if (!active.length && !recent.length) {
        list.innerHTML = `<p class="text-muted">${t("live_jobs_empty")}</p>`;
        return;
    }
    let html = active.map(liveJobsSheetRowHtml).join("");
    if (recent.length) {
        html += `<div class="live-jobs-sheet-head" style="margin-top:10px;"><span>${t("live_job_recent_done")}</span></div>`;
        html += recent.map(liveJobsSheetRowHtml).join("");
    }
    list.innerHTML = html;
}
function openLiveJobsSheet() {
    const sheet = document.getElementById("live-jobs-sheet");
    if (sheet) sheet.classList.remove("hidden");
    renderLiveJobsSheet();
    LiveJobs.fetchNow();
    syncTelegramBackButton();
}
window.openLiveJobsSheet = openLiveJobsSheet;
function closeLiveJobsSheet() {
    const sheet = document.getElementById("live-jobs-sheet");
    if (sheet) sheet.classList.add("hidden");
    syncTelegramBackButton();
}
window.closeLiveJobsSheet = closeLiveJobsSheet;

// ── Batch BC-3: income detail sheet ────────────────────────────────
// Opened from the Admin income card (header chip = all-time, each
// mini-card = its own period). The period total at the top comes from
// the SAME endpoint rows the card sums, so it reconciles by
// construction; test payments are display-only (badged TEST, muted)
// and never included in that total, toggle or not.
let _incomeDetailPeriod = "all";
function openIncomeDetail(period) {
    _incomeDetailPeriod = period || "all";
    const sheet = document.getElementById("income-detail-sheet");
    if (sheet) sheet.classList.remove("hidden");
    loadIncomeDetail();
    syncTelegramBackButton();
}
window.openIncomeDetail = openIncomeDetail;
function closeIncomeDetail() {
    const sheet = document.getElementById("income-detail-sheet");
    if (sheet) sheet.classList.add("hidden");
    syncTelegramBackButton();
}
window.closeIncomeDetail = closeIncomeDetail;
function setIncomeDetailPeriod(period) {
    _incomeDetailPeriod = period;
    loadIncomeDetail();
}
window.setIncomeDetailPeriod = setIncomeDetailPeriod;
async function loadIncomeDetail() {
    const list = document.getElementById("income-detail-list");
    const totalEl = document.getElementById("income-detail-total");
    if (!list) return;
    document.querySelectorAll("#income-detail-periods .filter-chip").forEach(chip => {
        chip.classList.toggle("active", chip.dataset.incomePeriod === _incomeDetailPeriod);
    });
    const showTest = !!document.getElementById("income-detail-show-test")?.checked;
    list.innerHTML = `<p class="text-muted">${t("admin_loading")}</p>`;
    try {
        const res = await fetchWithTimeout(
            `/api/admin/income-detail?period=${encodeURIComponent(_incomeDetailPeriod)}&include_test=${showTest}`,
            { headers: getAuthHeaders() }
        );
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        if (totalEl) {
            totalEl.innerText = (data.total_mmk || 0).toLocaleString() + " MMK · " +
                tf("admin_income_count", { count: data.count || 0 });
        }
        const rows = data.payments || [];
        if (!rows.length) {
            list.innerHTML = `<p class="text-muted">${t("admin_income_empty")}</p>`;
            return;
        }
        list.innerHTML = rows.map(p => `
            <div class="live-job-row income-detail-row${p.is_test ? " is-test" : ""}">
                <div class="live-job-row-main">
                    <div class="live-job-row-title">${escapeHtml(p.user_name || "")}${p.username ? " <span class='income-detail-username'>@" + escapeHtml(p.username) + "</span>" : ""}${p.is_test ? ` <span class="badge-test">${t("admin_income_test_badge")}</span>` : ""}</div>
                    <div class="live-job-row-meta">${escapeHtml(p.package_code || "")} · ${tf("admin_income_minutes", { minutes: p.minutes ?? 0 })} · ${admFmtDate(p.paid_at)}</div>
                </div>
                <div class="income-detail-amount">${(p.amount_mmk || 0).toLocaleString()} MMK</div>
            </div>`).join("");
    } catch (e) {
        list.innerHTML = "";
        if (totalEl) totalEl.innerText = "—";
        showStudioToast(adminErrorText("admin_generic_err", e.message), "error");
    }
}
window.loadIncomeDetail = loadIncomeDetail;
window.liveJobCancel = function(jobId, kind) {
    closeLiveJobsSheet();
    if (kind === "recap") {
        cancelRecapJobById(jobId);
    } else {
        showStudioConfirm(t("voice_cancel_confirm"), async () => {
            try {
                const res = await fetch(`/api/jobs/${jobId}/cancel`, {
                    method: "POST",
                    headers: getAuthHeaders(),
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) throw new Error(data.detail || t("err_generic"));
                showStudioToast(t("voice_cancelled_toast"), "info", 5000);
                if (state.activeJobId === jobId) {
                    if (state.pollTimer) clearInterval(state.pollTimer);
                    state.pollTimer = null;
                    const box = document.getElementById("voice-progress-box");
                    if (box) box.classList.add("hidden");
                    const cancelBtn = document.getElementById("btn-cancel-voice-job");
                    if (cancelBtn) cancelBtn.classList.add("hidden");
                    const btn = document.getElementById("btn-generate-voice");
                    if (btn) btn.disabled = false;
                }
                if (typeof ttsState !== "undefined" && ttsState.jobId === jobId) {
                    if (ttsState.pollTimer) clearInterval(ttsState.pollTimer);
                    ttsState.pollTimer = null;
                    const tBox = document.getElementById("tts-progress-box");
                    if (tBox) tBox.classList.add("hidden");
                    const tCancel = document.getElementById("btn-cancel-tts-job");
                    if (tCancel) tCancel.classList.add("hidden");
                    const tBtn = document.getElementById("btn-generate-tts");
                    if (tBtn) tBtn.disabled = false;
                }
                LiveJobs.fetchNow();
                loadUserHistory();
            } catch (err) {
                showStudioToast((err && err.message) ? err.message : t("err_generic"), "error");
            }
        });
    }
};
window.liveJobOpen = function(jobId, kind) {
    closeLiveJobsSheet();
    const job = LiveJobs.jobs.get(jobId);
    if (kind === "recap") {
        switchTab("tab-recap");
        if (job && job.status === "SUCCESS") {
            openRecapInPlayer(jobId);
        } else if (job && liveJobsIsTerminal(job.status)) {
            openHistoryHub();
        } else {
            openRecapDetail(jobId);
        }
    } else if (kind === "tts") {
        switchTab("tab-tts");
        if (job && job.status === "SUCCESS") {
            openHistoryHub();
        } else {
            const tBox = document.getElementById("tts-progress-box");
            if (tBox) {
                tBox.classList.remove("hidden");
                tBox.scrollIntoView({ behavior: "smooth" });
            }
            if (jobId && typeof ttsState !== "undefined" && !ttsState.pollTimer && (!job || !liveJobsIsTerminal(job.status))) {
                ttsState.jobId = jobId;
                const tCancel = document.getElementById("btn-cancel-tts-job");
                if (tCancel) tCancel.classList.remove("hidden");
                if (typeof pollTtsJob === "function") pollTtsJob(jobId);
            }
        }
    } else {
        switchTab("tab-voice");
        openVoiceDetail(jobId);
    }
};

// ── Batch AC: the studio survives submit ───────────────────────────────
// Submitting NEVER tears the workspace down: no control is reset, the
// canvas is untouched, and nothing scrolls the user away. Progress
// lives on the pill/sheet; the studio keeps only a slim inline status
// line. The detailed progress view opens BY CHOICE (inline line, pill
// sheet, History) and its Back button returns to the untouched studio.
state.voiceDetailOpen = false;
state.recapDetailOpen = false;
function showStudioInlineStatus(kind, text) {
    const line = document.getElementById(kind + "-inline-status");
    const label = document.getElementById(kind + "-inline-status-text");
    if (label && text) label.innerText = text;
    if (line) line.classList.remove("hidden");
}
window.showStudioInlineStatus = showStudioInlineStatus;
function openVoiceDetail(jobId) {
    const voiceTab = document.getElementById("tab-voice");
    if (voiceTab && !voiceTab.classList.contains("active")) switchTab("tab-voice");
    state.voiceDetailOpen = true;
    const box = document.getElementById("voice-progress-box");
    if (box) {
        box.classList.remove("hidden");
        box.scrollIntoView({ behavior: "smooth" });
    }
    if (jobId && !state.pollTimer) startJobPolling(jobId);
    syncTelegramBackButton();
}
window.openVoiceDetail = openVoiceDetail;
function openRecapDetail(jobId) {
    const recapTab = document.getElementById("tab-recap");
    if (recapTab && !recapTab.classList.contains("active")) switchTab("tab-recap");
    if (typeof showRecapStep === "function") showRecapStep(4);
    state.recapDetailOpen = true;
    const box = document.getElementById("recap-progress-box");
    if (box) {
        box.classList.remove("hidden");
        box.scrollIntoView({ behavior: "smooth" });
    }
    if (jobId && !state.recapPollTimer) startRecapJobPolling(jobId);
    syncTelegramBackButton();
}
window.openRecapDetail = openRecapDetail;
window.openCurrentVoiceDetail = function() { openVoiceDetail(state.activeJobId); };
window.openCurrentRecapDetail = function() { openRecapDetail(state.recapJobId); };
window.backToStudio = function(kind) {
    if (kind === "recap") {
        state.recapDetailOpen = false;
        const box = document.getElementById("recap-progress-box");
        if (box) box.classList.add("hidden");
        const tab = document.getElementById("tab-recap");
        if (tab) tab.scrollIntoView({ behavior: "smooth" });
    } else {
        state.voiceDetailOpen = false;
        const box = document.getElementById("voice-progress-box");
        if (box) box.classList.add("hidden");
        const tab = document.getElementById("tab-voice");
        if (tab) tab.scrollIntoView({ behavior: "smooth" });
    }
    if (typeof syncTelegramBackButton === "function") syncTelegramBackButton();
};
function showLiveSubmitLimit(kind) {
    // The server concurrency rule (1 active job per user) explained IN
    // PLACE: the workspace stays exactly as it is, the running job is
    // one tap away in the sheet, and nothing is a dead end.
    showStudioToast(t("live_submit_limit_title") + " — " + t("live_submit_limit_body"), "warning", 6000);
    showStudioInlineStatus(kind, t("live_submit_limit_title"));
    openLiveJobsSheet();
}
window.showLiveSubmitLimit = showLiveSubmitLimit;

// ── Batch AC: duplicate-submit guard + confirmation hand-off ──────────
// Owner-chosen pattern (over a silent stay in the studio): a
// successful submit hands off to a confirmation the user exits by
// choice, and the Create/Generate button cannot re-submit an
// UNCHANGED configuration — it re-arms only when the user changes
// any setting/input (a deliberate variation).
function createSubmitGuard() {
    return { inFlight: false, submittedFingerprint: null, _fp: null };
}
function guardBegin(guard, fingerprint) {
    if (guard.inFlight) return "in-flight";
    if (guard.submittedFingerprint && guard.submittedFingerprint === fingerprint) return "unchanged";
    guard.inFlight = true;
    guard._fp = fingerprint;
    return "ok";
}
function guardSuccess(guard) {
    guard.inFlight = false;
    guard.submittedFingerprint = guard._fp;
    guard._fp = null;
}
function guardFailure(guard) {
    guard.inFlight = false;
    guard._fp = null;
}
function guardRearmCheck(guard, currentFingerprint) {
    if (guard.submittedFingerprint && currentFingerprint !== guard.submittedFingerprint) {
        guard.submittedFingerprint = null;
        return true;
    }
    return false;
}
const voiceSubmitGuard = createSubmitGuard();
const recapSubmitGuard = createSubmitGuard();
const ttsSubmitGuard = createSubmitGuard();
function studioConfigFingerprint(kind) {
    if (kind === "tts") {
        const ti = document.getElementById("tts-text-input");
        return JSON.stringify({ text: ti ? ti.value : "", engine: typeof ttsState !== "undefined" ? ttsState.engine : "gemini", speed: typeof ttsState !== "undefined" ? ttsState.speed : "+12%" });
    }
    if (kind === "voice") {
        const textInput = document.getElementById("voice-text-input");
        return JSON.stringify({
            text: textInput ? textInput.value : "",
            persona: state.selectedPersona,
            speed: state.speed,
            subMode: state.voxcpmSubMode,
            ref: state.selectedReferenceFile
                ? (state.selectedReferenceFile.name + ":" + state.selectedReferenceFile.size)
                : null,
        });
    }
    const urlInput = document.getElementById("recap-url-input");
    const blurY = document.getElementById("slider-blur-y");
    const blurH = document.getElementById("slider-blur-h");
    const sr = document.getElementById("recap-script-review-toggle");
    return JSON.stringify({
        prefs: collectAccountPrefs(),
        url: state.uploadedVideoPath || (urlInput ? urlInput.value.trim() : ""),
        blurY: blurY ? blurY.value : null,
        blurH: blurH ? blurH.value : null,
        scriptReview: !!(sr && sr.checked),
        title: state.recapVideoTitle || "",
        // Batch BV: switching persona <-> own-voice (or a new
        // sample) IS a configuration change for the duplicate guard.
        ownVoice: state.recapOwnVoiceSelected ? (state.recapOwnVoiceRefId || "pending") : null,
    });
}
function setSubmitButtonState(kind) {
    const guard = kind === "voice" ? voiceSubmitGuard : (kind === "tts" ? ttsSubmitGuard : recapSubmitGuard);
    const btn = document.getElementById(kind === "voice" ? "btn-generate-voice" : (kind === "tts" ? "btn-generate-tts" : "btn-start-recap"));
    if (!btn) return;
    const span = btn.querySelector(".btn-text");
    if (span && !span.dataset.acOriginal) span.dataset.acOriginal = span.innerText;
    const activeBlocked = kind === "recap"
        ? (recapJobActive() || (typeof recapJobIsActive === "function" && recapJobIsActive()))
        : false;
    const gateBlocked = kind === "recap" && btn.getAttribute("data-ci-gate-disabled") === "1";
    btn.disabled = !!(guard.inFlight || guard.submittedFingerprint || activeBlocked || gateBlocked);
    if (span) {
        span.innerText = (guard.submittedFingerprint || activeBlocked)
            ? t("submit_already_creating")
            : (span.dataset.acOriginal || span.innerText);
    }
    if (kind === "recap" && typeof refreshStickyCtas === "function") refreshStickyCtas();
}
function studioSubmitBegin(kind) {
    const guard = kind === "voice" ? voiceSubmitGuard : (kind === "tts" ? ttsSubmitGuard : recapSubmitGuard);
    const result = guardBegin(guard, studioConfigFingerprint(kind));
    if (result === "unchanged") {
        showStudioToast(t("submit_already_creating"), "info", 5000);
        return false;
    }
    if (result !== "ok") return false; // in-flight double-tap: exactly one submit
    setSubmitButtonState(kind);
    return true;
}
function studioSubmitSuccess(kind) {
    const guard = kind === "voice" ? voiceSubmitGuard : (kind === "tts" ? ttsSubmitGuard : recapSubmitGuard);
    guardSuccess(guard);
    setSubmitButtonState(kind);
}
function studioSubmitFailure(kind) {
    const guard = kind === "voice" ? voiceSubmitGuard : (kind === "tts" ? ttsSubmitGuard : recapSubmitGuard);
    guardFailure(guard);
    setSubmitButtonState(kind);
}
function setupSubmitGuards() {
    [["tab-voice", "voice"], ["tab-recap", "recap"], ["tab-tts", "tts"]].forEach(([tabId, kind]) => {
        const tab = document.getElementById(tabId);
        if (!tab) return;
        const check = () => {
            const guard = kind === "voice" ? voiceSubmitGuard : (kind === "tts" ? ttsSubmitGuard : recapSubmitGuard);
            if (guardRearmCheck(guard, studioConfigFingerprint(kind))) {
                setSubmitButtonState(kind);
            }
        };
        ["input", "change", "click"].forEach(evt => tab.addEventListener(evt, check));
    });
    // The guard stays authoritative over the detail pollers' legacy
    // button re-enables: any live update re-applies the guard state.
    LiveJobs.subscribe(() => {
        setSubmitButtonState("voice");
        setSubmitButtonState("recap");
        setSubmitButtonState("tts");
    });
}

// The confirmation itself: success indication, the cost actually
// charged stated plainly, and a compact live progress snippet fed by
// LiveJobs. Exactly two actions — Watch the job / Back to studio —
// and nothing auto-navigates.
state.submitConfirmJob = null;
function renderSubmitConfirmSnippet(job) {
    const stageEl = document.getElementById("submit-confirm-stage");
    const fillEl = document.getElementById("submit-confirm-fill");
    if (!job) return;
    // Batch AK: terminal → the outcome label alone (no stale step).
    if (stageEl) stageEl.innerText = liveJobsIsTerminal(job.status)
        ? liveJobStageText(job)
        : (job.status === "WAITING_CHOICE" ? liveJobStageText(job) : `${job.status || ""} · ${liveJobStageText(job)}`);
    paintProgressBarFill(fillEl, job);
}
function showSubmitConfirmation(kind, jobId, costText) {
    state.submitConfirmJob = { kind, id: jobId };
    const card = document.getElementById("submit-confirm");
    const costEl = document.getElementById("submit-confirm-cost");
    if (costEl) costEl.innerText = costText || "";
    const tracked = LiveJobs.jobs.get(jobId);
    renderSubmitConfirmSnippet(tracked || {
        status: "QUEUED", kind, progress_percent: 0,
    });
    if (card) card.classList.remove("hidden");
}
window.showSubmitConfirmation = showSubmitConfirmation;
window.submitConfirmWatch = function() {
    const job = state.submitConfirmJob;
    const card = document.getElementById("submit-confirm");
    if (card) card.classList.add("hidden");
    if (!job) return;
    if (job.kind === "recap") openRecapDetail(job.id);
    else openVoiceDetail(job.id);
};
window.submitConfirmBack = function() {
    // Back to the preserved studio — exactly as the user left it;
    // the workspace underneath was never torn down.
    const card = document.getElementById("submit-confirm");
    if (card) card.classList.add("hidden");
    state.submitConfirmJob = null;
};
LiveJobs.subscribe((job) => {
    const current = state.submitConfirmJob;
    if (current && job && job.id === current.id) renderSubmitConfirmSnippet(job);
});

// History cards for tracked jobs update IN PLACE: the cached row's
// status flips, the row's badge/border/live line follow, and on a
// terminal flip ONE history reload swaps in the real result actions
// (download/send buttons) — never a manual Refresh.
let liveHistoryReloadPending = false;
function applyLiveJobToHistory(job) {
    if (!job || typeof hubHistoryCache === "undefined" || !Array.isArray(hubHistoryCache)) return;
    const item = hubHistoryCache.find(h => h.id === job.id);
    if (!item) {
        if (liveJobsIsTerminal(job.status) && !liveHistoryReloadPending) {
            liveHistoryReloadPending = true;
            loadUserHistory().finally(() => { liveHistoryReloadPending = false; });
        }
        return;
    }
    const changed = item.status !== job.status;
    item.status = job.status;
    const row = document.querySelector(`#hub-history-list .hub-row[data-job-id="${job.id}"]`);
    if (row) {
        const badge = row.querySelector(".history-item-badge");
        if (badge) {
            badge.textContent = job.status || "";
            badge.className = "history-item-badge status-" + String(job.status || "").toLowerCase();
        }
        row.className = row.className.replace(/border-\w+-dim/g, "").trim();
        const border = hubStatusBorder(job.status);
        if (border) row.classList.add(border);
        const live = row.querySelector(".hub-live-meta");
        if (live) {
            live.textContent = liveJobsIsActive(job.status)
                ? ` · ${Math.min(100, Math.max(0, job.progress_percent || 0))}%`
                : "";
        }
    }
    if (changed && liveJobsIsTerminal(job.status) && !liveHistoryReloadPending) {
        liveHistoryReloadPending = true;
        loadUserHistory().finally(() => { liveHistoryReloadPending = false; });
    }
}
LiveJobs.subscribe((job) => { applyLiveJobToHistory(job); });

// ── Batch AC: account-bound preferences ────────────────────────────────
// Server wins over localStorage; localStorage is a write-through cache
// for instant paint. If the server is unreachable the app works from
// cache and syncs on the next launch — the user is never blocked.
const USER_PREFS_CACHE_KEY = "vcp_user_prefs_cache";
let userPrefsSaveTimer = null;
// Batch AK: JS mirror of core.sub_centre_to_bottom_margin —
// converts a legacy centre-% subtitle position (percent of the
// PICTURE height) into the bottom-edge margin (same basis),
// plate-height-aware so the subtitle stays where it was. The
// reference picture height (608) matches core's
// SUB_MARGIN_CONVERT_REF_PICTURE_H.
function subtitleFontFractionOfPicture(sizePt, lines) {
    const pts = [[13, 4.0], [16, 5.0], [19, 6.0], [22, 7.0]];
    let pt = parseFloat(sizePt);
    if (!Number.isFinite(pt)) pt = 16;
    let frac = null;
    if (pt <= pts[0][0]) {
        frac = pts[0][1] * pt / pts[0][0];
    } else if (pt >= pts[pts.length - 1][0]) {
        const a = pts[pts.length - 2], b = pts[pts.length - 1];
        frac = b[1] + (b[1] - a[1]) * ((pt - b[0]) / (b[0] - a[0]));
    } else {
        for (let i = 0; i < pts.length - 1; i++) {
            const x0 = pts[i][0], y0 = pts[i][1], x1 = pts[i + 1][0], y1 = pts[i + 1][1];
            if (pt >= x0 && pt <= x1) { frac = y0 + (y1 - y0) * ((pt - x0) / (x1 - x0)); break; }
        }
    }
    if (frac == null) frac = pts[pts.length - 1][1];
    if ((parseInt(lines) || 1) !== 1) frac *= 1.25;
    return frac / 100;
}
function convertSubCentreToMargin(centrePct, fontSizePt, lines) {
    const PH = 608;
    const nLines = Math.max(1, parseInt(lines) || 1);
    const mult = nLines === 1 ? 1.15 : (nLines === 2 ? 2.30 : 3.65);
    const fp = Math.max(18, Math.round(PH * subtitleFontFractionOfPicture(fontSizePt || 16, nLines)));
    const textH = fp * mult;
    const pad = Math.max(10, fp * 0.40);
    const plateFrac = (textH + 2 * pad) / PH;
    const margin = (100 - centrePct) - (plateFrac * 100) / 2;
    return Math.round(Math.min(95, Math.max(0, margin)));
}
window.convertSubCentreToMargin = convertSubCentreToMargin;
// Batch DD: the Step-2 rebuild's payload fields, shared by the
// submit payload and the preview-composite payload so both send
// the SAME normalised values (preview == render). Fields are
// omitted when unset, so a job with no custom choices parses to
// exactly the pre-DD config.
function ddConfigExtras() {
    const out = {};
    const boxes = Array.isArray(state.recapBlurBoxes) ? state.recapBlurBoxes : [];
    if (boxes.length) {
        out.blur_boxes = boxes.slice(0, 5).map(b => ({
            x: Math.round(b.x), y: Math.round(b.y),
            w: Math.round(b.w), h: Math.round(b.h),
            strength: Math.max(4, Math.min(40, parseInt(b.strength) || 18)),
        }));
    }
    out.protection_level = state.recapProtection || "standard";
    out.sub_render_mode = state.recapSubRenderMode || "timeline";
    out.sub_effect = state.recapSubEffect || "none";
    out.embed_soft_subs = state.recapEmbedSoftSubs !== false;
    if (state.recapTextColor) out.sub_text_color = state.recapTextColor;
    if (state.recapOutlineColor) out.sub_outline_color = state.recapOutlineColor;
    if (state.recapBgColor) out.sub_bg_color = state.recapBgColor;
    if (state.recapOutlineWidth != null) out.sub_outline_width = state.recapOutlineWidth;
    if (state.recapBgOpacity != null) out.sub_bg_opacity = state.recapBgOpacity;
    if (state.recapSubShadow) out.sub_shadow = true;
    out.sub_bold = state.recapSubBold !== false;
    if (state.recapSubFontCustomId) out.sub_font_custom_id = state.recapSubFontCustomId;
    return out;
}
window.ddConfigExtras = ddConfigExtras;
function collectAccountPrefs() {
    const bs = document.getElementById("slider-blur-strength");
    const sy = document.getElementById("slider-sub-y");
    const sxEl = document.getElementById("slider-sub-x");
    const swEl = document.getElementById("slider-sub-w");
    const splitToggle = document.getElementById("recap-split-toggle");
    return {
        voice_persona: state.selectedPersona,
        voice_speed: normalizeSpeedOption(state.speed),
        recapRatio: state.recapRatio,
        recapRatioFill: state.recapRatioFill,
        recapSpeed: normalizeSpeedOption(state.recapSpeed),
        recapStoryStyle: state.recapStoryStyle,
        recapSubStyle: state.recapSubStyle,
        recapSubFont: state.recapSubFont,
        recapLogoPos: state.recapLogoPos,
        recapLogoScale: state.recapLogoScale || 0.16,
        recapLogoOpacity: state.recapLogoOpacity || 0.70,
        recapSourceLanguage: state.recapSourceLanguage,
        recapRenderQuality: state.recapRenderQuality,
        recapTtsEngine: state.recapTtsEngine,
        recapSyncCrop: !!state.recapSyncCrop,
        recapMirror: !!state.recapMirror,
        recapBurnSubs: !!state.recapBurnSubs,
        recapBlurSubs: !!state.recapBlurSubs,
        recapBlurAutoDetect: state.recapBlurAutoDetect !== false,
        recapLogo: !!state.recapLogo,
        recapSplitEnabled: !!(splitToggle && splitToggle.checked),
        recapCropTop: state.recapCropTop || 0,
        recapCropBottom: state.recapCropBottom || 0,
        recapCropLeft: state.recapCropLeft || 0,
        recapCropRight: state.recapCropRight || 0,
        recapFontSize: state.recapFontSize || 16,
        recapLines: state.recapLines || 1,
        recapSplitMinutes: state.recapSplitMinutes || 3,
        // Batch AK: the slider under the historical slider-sub-y id
        // is the BOTTOM-EDGE margin now; recapSubY (centre-%) is no
        // longer collected — the margin + schema version are the
        // stored anchor.
        recapSubMargin: sy ? parseInt(sy.value) : (state.recapSubMargin != null ? state.recapSubMargin : 5),
        recapSettingsVersion: 2,
        // Batch AG: sub_x / sub_w are account preferences like
        // recapSubY (NOT per-source — the blur box's horizontal
        // geometry stays out, exactly like its vertical geometry).
        recapSubX: sxEl ? parseInt(sxEl.value) : (state.recapSubX || 50),
        recapSubW: swEl ? parseInt(swEl.value) : (state.recapSubW || 92),
        // Batch DD: protection + free style controls + blur-box
        // list + custom font are account preferences now.
        recapProtection: state.recapProtection || "standard",
        recapTextColor: state.recapTextColor || "",
        recapOutlineColor: state.recapOutlineColor || "",
        recapBgColor: state.recapBgColor || "",
        recapOutlineWidth: state.recapOutlineWidth != null ? state.recapOutlineWidth : -1,
        recapBgOpacity: state.recapBgOpacity != null ? state.recapBgOpacity : 75,
        recapSubShadow: !!state.recapSubShadow,
        recapSubBold: state.recapSubBold !== false,
        recapBlurBoxesJson: JSON.stringify(state.recapBlurBoxes || []),
        recapSubFontCustomId: state.recapSubFontCustomId || "",
        recapBlurStrength: bs ? parseInt(bs.value) : (state.recapBlurStrength || 18),
        recapZoom: state.recapZoom,
        recapVoice: state.recapVoice,
        recapGeminiVoice: state.recapGeminiVoice || "",
        recapSubRenderMode: state.recapSubRenderMode || "timeline",
        recapSubEffect: state.recapSubEffect || "none",
        recapEmbedSoftSubs: state.recapEmbedSoftSubs !== false,
        savedCustomVoiceId: state.savedCustomVoiceId || "",
        savedCustomVoiceName: state.savedCustomVoiceName || "",
        // Phase 4: up to 3 named Step-2 presets ("My Presets"), JSON.
        recap_presets: serializeMyRecapPresets(),
    };
}
function cacheAccountPrefs(prefs) {
    try {
        localStorage.setItem(USER_PREFS_CACHE_KEY, JSON.stringify(prefs));
        // Write-through into the recap settings snapshot the existing
        // restore path reads at launch (merge: per-source blur
        // geometry keys already in the snapshot are preserved — they
        // are NOT account preferences and are never sent to the server).
        const snap = JSON.parse(localStorage.getItem("vcp_recap_settings") || "{}") || {};
        Object.keys(prefs).forEach(k => { if (k.startsWith("recap")) snap[k] = prefs[k]; });
        localStorage.setItem("vcp_recap_settings", JSON.stringify(snap));
    } catch (e) {}
}
function applyAccountPrefs(prefs) {
    if (!prefs || typeof prefs !== "object") return;
    // Batch AK migration (client mirror of the server rule): a
    // pre-AK payload carries recapSubY (centre-%) with no margin
    // and no version — convert it plate-height-aware so this
    // device's subtitle does not jump on the first launch after
    // the update. Runs once: the converted payload is stamped v2.
    if ((prefs.recapSettingsVersion || 0) < 2 && prefs.recapSubMargin == null
        && typeof prefs.recapSubY === "number") {
        prefs = { ...prefs };
        prefs.recapSubMargin = convertSubCentreToMargin(prefs.recapSubY, prefs.recapFontSize || 16, prefs.recapLines || 1);
        prefs.recapSettingsVersion = 2;
    }
    if (typeof prefs.voice_persona === "string" && prefs.voice_persona) {
        state.selectedPersona = prefs.voice_persona;
        if (typeof renderCuratedVoices === "function") { try { renderCuratedVoices(); } catch (e) {} }
    }
    if (typeof prefs.voice_speed === "string") {
        state.speed = normalizeSpeedOption(prefs.voice_speed);
        document.querySelectorAll("#speed-selector .speed-seg").forEach(p => {
            p.classList.toggle("active", p.getAttribute("data-speed") === state.speed);
        });
        moveSpeedPill(document.getElementById("speed-selector"));
        updateVoicePreflight();
    }
    const recapKeys = [
        "recapRatio", "recapRatioFill", "recapSpeed", "recapStoryStyle",
        "recapSubStyle", "recapSubFont", "recapLogoPos", "recapSourceLanguage",
        "recapRenderQuality", "recapTtsEngine", "recapSyncCrop", "recapMirror",
        "recapBurnSubs", "recapBlurSubs", "recapBlurAutoDetect", "recapLogo", "recapCropTop",
        "recapCropBottom", "recapCropLeft", "recapCropRight", "recapFontSize",
        "recapLines", "recapSplitMinutes", "recapSubY", "recapBlurStrength",
        "recapSubMargin", "recapSettingsVersion",
        "recapSubX", "recapSubW",
        // Batch DD keys (protection, free style controls,
        // blur-box list JSON, custom font handle).
        "recapProtection", "recapTextColor", "recapOutlineColor",
        "recapBgColor", "recapOutlineWidth", "recapBgOpacity",
        "recapSubShadow", "recapSubBold", "recapSubFontCustomId",
        "recapZoom", "recapVoice", "recapGeminiVoice",
        "recapSubRenderMode", "recapSubEffect", "recapEmbedSoftSubs",
        "savedCustomVoiceId", "savedCustomVoiceName",
    ];
    recapKeys.forEach(k => { if (prefs[k] !== undefined) state[k] = prefs[k]; });
    if (typeof syncSavedCustomVoiceUi === "function") syncSavedCustomVoiceUi();
    state.recapSpeed = normalizeSpeedOption(state.recapSpeed);
    // Batch DD: the blur-box list rides prefs as JSON.
    if (typeof prefs.recapBlurBoxesJson === "string" && prefs.recapBlurBoxesJson) {
        try {
            const arr = JSON.parse(prefs.recapBlurBoxesJson);
            if (Array.isArray(arr)) state.recapBlurBoxes = arr.filter(b => b && typeof b === "object").slice(0, 5);
        } catch (e) {}
    }
    if (state.recapOutlineWidth === -1) state.recapOutlineWidth = null;
    if (typeof prefs.recapBgOpacity !== "number") state.recapBgOpacity = null;
    // Phase 4: My Presets ride the account prefs as a JSON string.
    if (typeof prefs.recap_presets === "string") {
        state.recapCustomPresets = parseMyRecapPresets(prefs.recap_presets);
        if (typeof renderMyRecapPresets === "function") { try { renderMyRecapPresets(); } catch (e) {} }
    }
    cacheAccountPrefs(prefs);
    // Batch AM (M8ii): the recap controls were synced at boot from
    // the LOCAL snapshot; the server prefs above may differ (another
    // device, an admin-side change). Re-run the restore's control
    // sync — it re-reads the snapshot cacheAccountPrefs has just
    // rewritten from these same prefs — so toggles, pills and
    // sliders can never display one thing while payloads use
    // another. Guarded + idempotent: local reads only, no fetch,
    // and the hook exists once the recap editor has been set up
    // (boot order: setupRecapVisualEditor before loadAccountPrefs).
    if (typeof window.resyncRecapControlsAfterPrefs === "function") {
        try { window.resyncRecapControlsAfterPrefs(); } catch (e) {}
    }
    // Batch BB-8: seed the saved-snapshot from what was just applied
    // (after the control resync, so DOM-derived fields match state),
    // so the first delegated save attempt after launch — typically a
    // wizard step click — compares equal and stays silent.
    userPrefsLastSavedJson = prefsSnapshotJson(collectAccountPrefs());
}
async function loadAccountPrefs() {
    try {
        const res = await fetch("/api/user/preferences", { headers: getAuthHeaders() });
        if (!res.ok) return; // cache-only mode; sync next launch
        const data = await res.json();
        if (data && data.preferences) applyAccountPrefs(data.preferences);
    } catch (e) {
        // Offline / server unreachable: work from the cache, silently.
    }
}
window.loadAccountPrefs = loadAccountPrefs;
function showPrefsSavedNote() {
    // Batch AZ-10.7: the save confirmation is a TOAST now (AZ-7) —
    // it used to appear in #prefs-saved-note, a line sitting in the
    // wizard's indicator area where it read as step feedback. The
    // indicator area carries step state only. (The #prefs-saved-note
    // element stays in the markup, unused and hidden, so its id and
    // the Batch AC pin on it survive.)
    try {
        if (typeof document !== "undefined" && typeof document.querySelector === "function"
            && document.querySelector("#studio-toast-el.error")) {
            return;
        }
        showStudioToast(t("live_prefs_saved"), "success", 2200);
    } catch (e) {}
}
// Batch BB-8: the prefs saver is triggered by DELEGATED listeners on
// the whole Recap/Voice tabs (setupAccountPrefs), so EVERY click —
// including pure wizard step navigation — used to schedule a save,
// and every successful PUT toasted "settings saved" even when the
// payload was byte-identical to the last save. The toast (and the
// PUT itself) now fires only when a value actually changed: the
// collected payload is compared against the last saved snapshot,
// seeded when server prefs are applied (applyAccountPrefs).
let userPrefsLastSavedJson = null;
function prefsSnapshotJson(prefs) {
    try { return JSON.stringify(prefs); } catch (e) { return null; }
}
async function saveAccountPrefsNow() {
    const prefs = collectAccountPrefs();
    const prefsJson = prefsSnapshotJson(prefs);
    if (prefsJson !== null && prefsJson === userPrefsLastSavedJson) {
        // Nothing changed since the last save (e.g. step navigation):
        // stay silent — no PUT, no toast.
        return;
    }
    cacheAccountPrefs(prefs);
    try {
        const res = await fetch("/api/user/preferences", {
            method: "PUT",
            headers: getAuthHeaders(),
            body: JSON.stringify({ preferences: prefs }),
        });
        if (res.ok) {
            userPrefsLastSavedJson = prefsJson;
            showPrefsSavedNote();
        }
    } catch (e) {
        // Silent: the cache already holds the change; next launch syncs.
    }
}
function saveAccountPrefsSoon() {
    if (userPrefsSaveTimer) clearTimeout(userPrefsSaveTimer);
    userPrefsSaveTimer = setTimeout(saveAccountPrefsNow, 600);
}
window.saveAccountPrefsSoon = saveAccountPrefsSoon;
function setupAccountPrefs() {
    const recapTab = document.getElementById("tab-recap");
    if (recapTab) {
        // Batch BT: pointerup joins the list. The canvas drag
        // helpers (enableVerticalDrag / enableHorizontalDrag) drive
        // the margin / sub_x / sub_w / title-position values through
        // raw mouse + touch events with preventDefault, so a drag
        // ends in a pointerup but NO click/change/input ever reaches
        // the tab. The settings-snapshot saver has always listened
        // for pointerup, so a dragged subtitle margin landed in the
        // local snapshot — but never in the account prefs, and the
        // next launch's prefs apply then overwrote the restored
        // value with the server's stale one (live 2026-11-06: a
        // dragged 20% margin came back as the 5% default, while the
        // click-toggled Blur setting — which does reach this saver —
        // persisted across the same loads). The BB-8 change
        // comparison keeps a pointer-only save silent when nothing
        // actually changed.
        ["click", "change", "input", "pointerup"].forEach(evt => recapTab.addEventListener(evt, saveAccountPrefsSoon));
    }
    const voiceTab = document.getElementById("tab-voice");
    if (voiceTab) {
        ["click", "change", "input"].forEach(evt => voiceTab.addEventListener(evt, saveAccountPrefsSoon));
    }
}


// -- 401 "Open via Telegram" modal (security audit round 2) --
// Previously a 401 from /api/user/me was silently ignored and other
// calls surfaced a raw error or left the UI blank. Now the first 401
// shows a friendly modal instead of breaking the app.
let openViaTelegramShown = false;
function showOpenViaTelegramModal() {
    if (openViaTelegramShown) return;
    openViaTelegramShown = true;
    const modal = document.getElementById("open-via-telegram-modal");
    if (!modal) return;
    const titleEl = modal.querySelector('[data-i18n="open_via_telegram_title"]');
    const descEl = modal.querySelector('[data-i18n="open_via_telegram_desc"]');
    const btnEl = modal.querySelector('[data-i18n="open_via_telegram_btn"]');
    if (titleEl) titleEl.textContent = t("open_via_telegram_title");
    if (descEl) descEl.textContent = t("open_via_telegram_desc");
    if (btnEl) btnEl.textContent = t("open_via_telegram_btn");
    modal.classList.remove("hidden");
}
window.showOpenViaTelegramModal = showOpenViaTelegramModal;
function closeOpenViaTelegramModal() {
    const modal = document.getElementById("open-via-telegram-modal");
    if (modal) modal.classList.add("hidden");
}
window.closeOpenViaTelegramModal = closeOpenViaTelegramModal;
// Session expiry (E1 fix 2026-10-31): the server rejects initData
// whose auth_date is older than 24h (F4). A Mini App left open longer
// than that 401s on every call — but the "Open via Telegram" modal is
// the wrong explanation there (the user IS inside Telegram). Detect
// the stale-auth_date case client-side and show the dedicated
// session-expired message instead; all other 401s keep the modal.
const INIT_DATA_MAX_AGE_SECONDS = 24 * 60 * 60;
function initDataExpired() {
    try {
        let authDate = 0;
        if (state.tg && state.tg.initDataUnsafe) {
            authDate = Number(state.tg.initDataUnsafe.auth_date || 0);
        }
        if (!authDate && state.tg && state.tg.initData) {
            const m = /(?:^|&)auth_date=(\d+)/.exec(state.tg.initData);
            if (m) authDate = Number(m[1]);
        }
        if (!authDate) return false;
        return (Date.now() / 1000 - authDate) > INIT_DATA_MAX_AGE_SECONDS;
    } catch (e) {
        return false;
    }
}
window.initDataExpired = initDataExpired;
let sessionExpiredShown = false;
function isAuthFailure(res) {
    if (res && res.status === 401) {
        if (initDataExpired()) {
            if (!sessionExpiredShown) {
                sessionExpiredShown = true;
                showStudioToast(t("err_session_expired"), 'error', 6000);
            }
            return true;
        }
        showOpenViaTelegramModal();
        return true;
    }
    return false;
}
window.isAuthFailure = isAuthFailure;

// -- 403 membership gate: turn a dead-end API error into the join reminder --
// Gated writes return 403 MEMBERSHIP_REQUIRED (or GATE_NOT_CONFIGURED).
// Before this, app.js had NO handling for them: generate/recap surfaced
// the raw English server detail as an error toast with no way forward.
// Now any such 403 refreshes the gate banner (the server committed fresh
// live-check flags together with the 403, so the per-chat chips are
// accurate), scrolls it into view and points the user at the join +
// re-check actions. The response itself is returned untouched.
let gateNoticeAt = 0;
async function showGateReminder() {
    const now = Date.now();
    if (now - gateNoticeAt < 1500) return; // one nudge per burst of calls
    gateNoticeAt = now;
    try { await loadUserProfile(); } catch (e) { /* banner refresh is best-effort */ }
    // A gated write just 403'd: re-run the LIVE entry check too — if the
    // user left a chat, the whole app re-locks, not just a banner nudge.
    if (window.runEntryGateCheck) runEntryGateCheck("gate-403");
    const banner = document.getElementById("gate-banner");
    if (banner) {
        banner.classList.remove("hidden");
        try { banner.scrollIntoView({ behavior: "smooth", block: "center" }); } catch (e) {}
    }
    showStudioToast(t("gate_required_toast"), "warning");
}
window.showGateReminder = showGateReminder;
(function installGateFetchInterceptor() {
    if (window.__gateFetchInstalled) return;
    window.__gateFetchInstalled = true;
    const origFetch = window.fetch.bind(window);
    window.fetch = async (...args) => {
        const res = await origFetch(...args);
        try {
            const url = typeof args[0] === "string" ? args[0] : ((args[0] && args[0].url) || "");
            // Batch N: 401s from ANY /api/ route get the shared
            // session-expired / open-via-Telegram handling — before
            // this only the call sites that remembered to check
            // isAuthFailure() themselves reacted, and a bare 401
            // elsewhere surfaced as an unexplained failure.
            if (res.status === 401 && url.includes("/api/")) {
                isAuthFailure(res);
            }
            if (res.status === 403 && url.includes("/api/") && !url.includes("/api/user/verify-membership")) {
                res.clone().json().then((data) => {
                    const detail = (data && data.detail) || "";
                    if (detail.includes("MAINTENANCE_MODE")) {
                        // Batch AR: maintenance was switched on while
                        // the app was open — its own calls now refuse.
                        // Swap straight to the maintenance screen; the
                        // join banner would be the wrong way forward.
                        if (window.enterMaintenanceLock) enterMaintenanceLock();
                    } else if (detail.includes("MEMBERSHIP_REQUIRED") || detail.includes("GATE_NOT_CONFIGURED")) {
                        showGateReminder();
                    }
                }).catch(() => {});
            }
        } catch (e) { /* the reminder must never break the app */ }
        return res;
    };
})();

// Auth media fetch with in-memory cache for voice samples
const _authSampleCache = new Map();
async function fetchAuthImageUrl(url) {
    if (!url || !url.startsWith("/api/")) return url;
    if (url.startsWith("/api/voices/sample/") && _authSampleCache.has(url)) return _authSampleCache.get(url);
    try {
        const res = await fetch(url, { headers: getAuthHeaders(false) });
        if (isAuthFailure(res) || !res.ok) return null;
        const objUrl = URL.createObjectURL(await res.blob());
        if (url.startsWith("/api/voices/sample/")) _authSampleCache.set(url, objUrl);
        return objUrl;
    } catch (e) { return null; }
}
window.fetchAuthImageUrl = fetchAuthImageUrl;

// ── Authenticated result media (round-6 P1) ─────────────────────────
const _authMediaObjectUrls = new WeakMap();

async function fetchAuthBlobUrl(url) {
    if (!url) return null;
    if (!url.startsWith("/api/")) return url;
    try {
        const res = await fetch(url, { headers: getAuthHeaders(false) });
        if (isAuthFailure(res) || !res.ok) return null;
        return URL.createObjectURL(await res.blob());
    } catch (e) { return null; }
}

async function fetchAuthBlobUrlWithProgress(url, onProgress) {
    if (!url) return null;
    if (!url.startsWith("/api/")) return url;
    try {
        const res = await fetch(url, { headers: getAuthHeaders(false) });
        if (isAuthFailure(res)) return null;
        if (!res.ok) return null;
        const blob = await _streamedResponseBlob(res, onProgress);
        return URL.createObjectURL(blob);
    } catch (e) { return null; }
}

async function _streamedResponseBlob(res, onProgress) {
    const notify = typeof onProgress === "function" ? onProgress : null;
    const reader = (res.body && typeof res.body.getReader === "function")
        ? res.body.getReader() : null;
    if (!reader) {
        // No streaming available (older WebView): one indeterminate
        // paint, then the same whole-blob read as before.
        if (notify) notify(null);
        return res.blob();
    }
    let total = 0;
    try {
        total = parseInt(res.headers && res.headers.get ? res.headers.get("content-length") : "", 10) || 0;
    } catch (e) { total = 0; }
    if (notify) notify(total > 0 ? 0 : null);
    const chunks = [];
    let received = 0;
    for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value) {
            chunks.push(value);
            received += value.byteLength || value.length || 0;
        }
        if (notify) notify(total > 0 ? Math.min(99, Math.floor((received / total) * 100)) : null);
    }
    let type = "";
    try { type = (res.headers && res.headers.get && res.headers.get("content-type")) || ""; } catch (e) {}
    return new Blob(chunks, { type });
}

async function setAuthMediaSource(el, url) {
    if (!el || !url) return false;
    if (/[?&]t=/.test(url)) {
        clearAuthMediaSource(el);
        el.src = url;
        return true;
    }
    const objUrl = await fetchAuthBlobUrl(url);
    if (!objUrl) return false;
    const prev = _authMediaObjectUrls.get(el);
    el.src = objUrl;
    if (objUrl.startsWith("blob:")) _authMediaObjectUrls.set(el, objUrl);
    if (prev && prev !== objUrl) URL.revokeObjectURL(prev);
    return true;
}

// R40 (ii): recap VIDEO prefers the signed stream URL — the player
// streams it progressively with HTTP Range instead of holding the
// whole render in memory as a blob (the blob path is what produced
// "Unable to play media" in the Telegram WebView on large renders).
// Falls back to the authenticated blob path when no stream URL was
// issued. Returns a promise like setAuthMediaSource.
function setRecapPlayerSource(player, urls) {
    if (!player || !urls) return Promise.resolve(false);
    if (urls.stream_url) {
        clearAuthMediaSource(player);
        player.src = urls.stream_url;
        player.load();
        return Promise.resolve(true);
    }
    return setAuthMediaSource(player, urls.video);
}

function clearAuthMediaSource(el) {
    if (!el) return;
    const prev = _authMediaObjectUrls.get(el);
    if (prev) { URL.revokeObjectURL(prev); _authMediaObjectUrls.delete(el); }
    el.removeAttribute("src");
    try { el.load(); } catch (e) {}
}

// Premium branded download names (2026-11-03): mirrors the server-side
// scheme in app/services/download_names.py —
// VoiceClonePro_<Kind>_<Label>.<ext>, where Label is the sanitized
// content title when the item carries one, else the job tag (last 6
// alphanumeric chars of the job id). Used as the save-as fallback name;
// the server's Content-Disposition carries the same branded name.
function brandedDlName(kind, ext, label, jobId, part) {
    let clean = String(label || "").normalize("NFC").trim();
    if (/^https?:\/\//i.test(clean)) clean = "";
    clean = clean
        .replace(/[^\p{L}\p{M}\p{N}\s_-]/gu, " ")
        .replace(/\s+/g, "_")
        .replace(/_+/g, "_")
        .replace(/^_+|_+$/g, "");
    if (!clean) {
        const tag = String(jobId || "").replace(/[^A-Za-z0-9]/g, "");
        clean = tag ? tag.slice(-6) : "File";
    }
    const partSuffix = part ? `_Part${String(part).padStart(2, "0")}` : "";
    const fixedLen = "VoiceClonePro_".length + kind.length + 1 + partSuffix.length + 1 + ext.length;
    if (clean.length + fixedLen > 90) {
        clean = clean.slice(0, Math.max(0, 90 - fixedLen)).replace(/_+$/g, "") || clean.slice(0, Math.max(0, 90 - fixedLen));
    }
    return `VoiceClonePro_${kind}_${clean}${partSuffix}.${ext}`;
}

// Over-50MB delivery rule (2026-11-03): when the server reports the
// output cannot ride the Telegram bot (telegram_sendable === false in
// the status payload), the result screens do not offer "Send to
// Telegram" at all — the premium Download action stands alone, with a
// short note in the send button's place. Any other value (true, or
// the flag absent) restores the normal two-action layout. For voice,
// the Download button is promoted to the premium gold style while it
// is the only delivery action and restored afterwards.
function applyTelegramSendable(kind, sendable) {
    const isRecap = kind === "recap";
    const btnTg = document.getElementById(isRecap ? "btn-recap-send-telegram" : "btn-voice-send-telegram");
    const note = document.getElementById(isRecap ? "recap-oversize-note" : "voice-oversize-note");
    const oversize = sendable === false;
    if (btnTg) btnTg.classList.toggle("hidden", oversize);
    if (note) {
        note.classList.toggle("hidden", !oversize);
        if (oversize) note.innerText = t("note_over_50mb_download");
    }
    if (!isRecap) {
        const btnDl = document.getElementById("btn-download-audio");
        if (btnDl) {
            btnDl.classList.toggle("gold-gradient-btn", oversize);
            btnDl.classList.toggle("secondary-btn", !oversize);
        }
    }
}
window.applyTelegramSendable = applyTelegramSendable;

function urlCarriesStreamToken(url) {
    // A stream-token URL authenticates itself (a scoped HMAC rides in
    // ?t=), so it can be handed to the device browser as-is. The
    // check is purely syntactic — token VALIDITY is the server's
    // business, decided when the URL is opened.
    if (!url || typeof url !== "string") return false;
    const q = url.split("?", 2)[1];
    if (!q) return false;
    return q.split("&").some(p => p.startsWith("t=") && p.length > 2);
}
window.urlCarriesStreamToken = urlCarriesStreamToken;

async function downloadAuthFile(url, filename, el) {
    if (!url) return;
    // Batch AF: when the tapped element is handed over (the delegated
    // data-dl-url handler passes it), paint the download progress on
    // the element itself and ALWAYS restore its original content when
    // the fetch settles — success or failure — so no button can stay
    // stuck on a percentage. A second tap on an element whose
    // download is already in flight is ignored.
    let onProgress = null;
    let restoreEl = null;
    if (el) {
        if (el.dataset && el.dataset.dlBusy === "1") return;
        const originalHTML = el.innerHTML;
        if (el.dataset) el.dataset.dlBusy = "1";
        el.classList.add("dl-downloading");
        el.setAttribute("aria-busy", "true");
        onProgress = (pct) => {
            el.innerHTML = pct == null
                ? t("dl_downloading")
                : tf("dl_downloading_pct", { pct });
        };
        onProgress(null);
        restoreEl = () => {
            el.innerHTML = originalHTML;
            el.classList.remove("dl-downloading");
            el.removeAttribute("aria-busy");
            if (el.dataset) delete el.dataset.dlBusy;
        };
    }
    try {
        // Skip Telegram.openLink on web/desktop clients where it drops downloads; mobile WebViews only.
        const tgPlat = window.Telegram?.WebApp?.platform || "";
        const allowTgOpen = !/^(unknown|weba|webk|tdesktop|macos)$/i.test(tgPlat);
        if (urlCarriesStreamToken(url) && window.Telegram?.WebApp?.openLink && allowTgOpen) {
            const absUrl = url.startsWith("http") ? url : (window.location.origin + url);
            let opened = false;
            try {
                window.Telegram.WebApp.openLink(absUrl);
                opened = true;
            } catch (e) { /* fall through to the blob path */ }
            if (opened) {
                haptic("success");
                return;
            }
        }
        const objUrl = await fetchAuthBlobUrlWithProgress(url, onProgress);
        if (!objUrl) throw new Error("Download failed");
        const a = document.createElement("a");
        a.href = objUrl;
        if (filename) a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(objUrl), 4000);
        haptic("success");
    } catch (err) {
        showErrorToast(err, 'download');
    } finally {
        if (restoreEl) restoreEl();
    }
}
window.downloadAuthFile = downloadAuthFile;

async function openAuthFile(url) {
    if (!url) return;
    try {
        if (urlCarriesStreamToken(url) && window.Telegram?.WebApp?.openLink && !/^(unknown|weba|webk|tdesktop|macos)$/i.test(window.Telegram.WebApp.platform || "")) {
            const absUrl = url.startsWith("http") ? url : (window.location.origin + url);
            try {
                window.Telegram.WebApp.openLink(absUrl);
                haptic("success");
                return;
            } catch (e) { /* fall through to the blob path */ }
        }
        const objUrl = await fetchAuthBlobUrl(url);
        if (!objUrl) throw new Error("Open failed");
        const a = document.createElement("a");
        a.href = objUrl;
        a.target = "_blank";
        a.rel = "noopener";
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(objUrl), 60000);
    } catch (err) {
        showErrorToast(err, 'download');
    }
}
window.openAuthFile = openAuthFile;

// Delegated handlers: anchors carrying data-dl-url are authenticated
// downloads and anchors carrying data-open-url are authenticated
// view-in-tab links — neither may navigate to the raw API URL.
document.addEventListener("click", (e) => {
    // Batch CK: anchors inside the admin media modal carry their own
    // direct listeners (see openAdminMediaModal) because the modal's
    // content wrapper stops click propagation before it can reach this
    // delegated handler. Those listeners flag the event so that if the
    // wrapper's stopPropagation is ever removed, a click can never fire
    // both paths and download twice.
    if (e.__dlHandled) return;
    const a = e.target && e.target.closest ? e.target.closest("a[data-dl-url], a[data-open-url]") : null;
    if (!a) return;
    e.preventDefault();
    if (a.hasAttribute("data-dl-url")) {
        downloadAuthFile(a.getAttribute("data-dl-url"), a.getAttribute("data-dl-name") || a.getAttribute("download") || undefined, a);
    } else {
        openAuthFile(a.getAttribute("data-open-url"));
    }
});

// ── Tutorial Video Link Controller (Voice Clone & Movie Recap) ───────────────
// Tutorial registry (premium polish): every tutorial lives as a message in
// the @VoiceClonePro channel; msg 38 is the index post linking all 10.
// Resolution is settings-first for EVERY key — all links are editable in
// Admin → Settings: video/voice/recap use their dedicated tutorialConfig
// slots below, the rest use tutorialLinkOverrides (fed by /api/user/me).
// These registry values are the fallback defaults, so no in-app entry can
// point at a dead link.
const TUTORIAL_LINKS = {
    index: "https://t.me/VoiceClonePro/38",
    bot: "https://t.me/VoiceClonePro/28",
    video: "https://t.me/VoiceClonePro/28",
    voice: "https://t.me/VoiceClonePro/29",
    recap: "https://t.me/VoiceClonePro/30",
    plans: "https://t.me/VoiceClonePro/31",
    customvoice: "https://t.me/VoiceClonePro/32",
    downloader: "https://t.me/VoiceClonePro/33",
    scriptedit: "https://t.me/VoiceClonePro/34",
    recapadvanced: "https://t.me/VoiceClonePro/35",
    refer: "https://t.me/VoiceClonePro/36",
    support: "https://t.me/VoiceClonePro/37",
};
window.TUTORIAL_LINKS = TUTORIAL_LINKS;

// Built-in default titles (mirrors of the server-side defaults). When the
// configured title is one of these, the banner label is owned by i18n so
// the EN UI never shows a Myanmar leftover; an admin-customised title is
// always respected as-is.
const TUTORIAL_DEFAULT_TITLES = [
    "Voice Clone ပြုလုပ်နည်း လမ်းညွှန်",
    "🎙️ Voice Clone ပြုလုပ်နည်း လမ်းညွှန်",
    "Movie Recap ပြုလုပ်နည်း လမ်းညွှန်",
    "🎬 Movie Recap ပြုလုပ်နည်း လမ်းညွှန်",
];
function tutorialDisplayTitle(configTitle, i18nKey) {
    if (configTitle && !TUTORIAL_DEFAULT_TITLES.includes(configTitle)) return configTitle;
    return t(i18nKey);
}

// Admin-settings key behind each remaining registry tutorial. "bot"
// shares the main video slot (tutorialConfig.fallbackUrl); voice/recap
// keep their dedicated slots in tutorialConfig.
const TUTORIAL_SETTING_KEYS = {
    index: "tutorial_index_link",
    plans: "tutorial_plans_link",
    customvoice: "tutorial_customvoice_link",
    downloader: "tutorial_downloader_link",
    scriptedit: "tutorial_scriptedit_link",
    recapadvanced: "tutorial_recapadvanced_link",
    refer: "tutorial_refer_link",
    support: "tutorial_support_link",
};
let tutorialLinkOverrides = {};

// Settings-first resolution for every tutorial key; the registry
// default is the fallback when no admin value is set.
function resolveTutorialLink(type) {
    if (type === "voice") return tutorialConfig.voice.url || TUTORIAL_LINKS.voice;
    if (type === "recap") return tutorialConfig.recap.url || TUTORIAL_LINKS.recap;
    if (type === "video" || type === "bot") return tutorialConfig.fallbackUrl || TUTORIAL_LINKS.video;
    return tutorialLinkOverrides[type] || TUTORIAL_LINKS[type] || "";
}

let tutorialConfig = {
    fallbackUrl: TUTORIAL_LINKS.video,
    voice: {
        title: "Voice Clone ပြုလုပ်နည်း လမ်းညွှန်",
        url: TUTORIAL_LINKS.voice,
        enabled: true
    },
    recap: {
        title: "Movie Recap ပြုလုပ်နည်း လမ်းညွှန်",
        url: TUTORIAL_LINKS.recap,
        enabled: true
    }
};

function updateTutorialUI(data) {
    if (!data) return;
    if (data.tutorial_video_link) tutorialConfig.fallbackUrl = data.tutorial_video_link;

    if (data.tutorial_voice_title !== undefined) tutorialConfig.voice.title = data.tutorial_voice_title;
    if (data.tutorial_voice_link !== undefined) tutorialConfig.voice.url = data.tutorial_voice_link;
    if (data.tutorial_voice_enabled !== undefined) {
        tutorialConfig.voice.enabled = String(data.tutorial_voice_enabled).toLowerCase() !== "false" && data.tutorial_voice_enabled !== false;
    }

    if (data.tutorial_recap_title !== undefined) tutorialConfig.recap.title = data.tutorial_recap_title;
    if (data.tutorial_recap_link !== undefined) tutorialConfig.recap.url = data.tutorial_recap_link;
    if (data.tutorial_recap_enabled !== undefined) {
        tutorialConfig.recap.enabled = String(data.tutorial_recap_enabled).toLowerCase() !== "false" && data.tutorial_recap_enabled !== false;
    }

    // Batch L: recap demo slot — visible only while the admin has a
    // demo asset configured (Telegram file_id or URL); the value is
    // kept on state for the watch handler (watchRecapDemo).
    if (data.recap_demo_asset !== undefined) {
        state.recapDemoAsset = data.recap_demo_asset || "";
        const demoCard = document.getElementById("recap-demo-card");
        if (demoCard) demoCard.classList.toggle("hidden", !state.recapDemoAsset);
    }

    // Remaining tutorials: admin value wins over the registry default;
    // an empty value clears the override (back to the default).
    for (const [tutKey, settingKey] of Object.entries(TUTORIAL_SETTING_KEYS)) {
        const v = data[settingKey];
        if (v !== undefined) {
            if (v) tutorialLinkOverrides[tutKey] = v;
            else delete tutorialLinkOverrides[tutKey];
        }
    }

    // Apply visibility & title to Voice banner in Tab 1
    const voiceBanner = document.getElementById("voice-tutorial-banner");
    const voiceTitle = document.getElementById("voice-tut-title");
    if (voiceBanner) {
        if (tutorialConfig.voice.enabled) {
            voiceBanner.classList.remove("hidden");
            if (voiceTitle) voiceTitle.textContent = tutorialDisplayTitle(tutorialConfig.voice.title, "tutorial_banner_title");
        } else {
            voiceBanner.classList.add("hidden");
        }
    }

    // Batch AV: the Recap Step-1 mini tutorial button was removed as a
    // duplicate of the header guide (btn-tutorial-top, which honours
    // the same enabled flags below) — its management block went with it.

    // Apply visibility to Top Nav Button
    const topTutBtn = document.getElementById("btn-tutorial-top");
    if (topTutBtn) {
        if (!tutorialConfig.voice.enabled && !tutorialConfig.recap.enabled) {
            topTutBtn.classList.add("hidden");
        } else {
            topTutBtn.classList.remove("hidden");
        }
    }
}

function openTutorialUrl(url) {
    if (!url) return;
    if (window.Telegram?.WebApp?.openLink) {
        window.Telegram.WebApp.openLink(url);
    } else {
        window.open(url, "_blank");
    }
}

// ── Refer & Support (round-6 P2) ────────────────────────────────────
// The referral programme itself lives in the bot (ledger.py); /me now
// carries the user's own code/link + stats, rendered here on Home.
let _referralLink = "";
let _supportLink = "https://t.me/VoiceCloneProAdmin";

function updateReferUI(data) {
    if (!data) return;
    window._lastReferralStats = data;
    _referralLink = data.referral_link || "";
    if (data.support_link) _supportLink = data.support_link;
    const lt = document.getElementById("referral-link-text");
    if (lt) lt.textContent = _referralLink || t("ref_link_pending");
    const st = document.getElementById("referral-stats-line");
    const r = data.referral;
    if (st) {
        st.textContent = r ? tf("ref_stats_line", {
            rewarded: r.rewarded_referrals || 0,
            max: r.max_referrals || 20,
            earned_min: Math.round((r.earned_minutes || 0) * 10) / 10,
            remaining: r.remaining_slots != null ? r.remaining_slots : 20,
        }) : "";
    }
}
window.updateReferUI = updateReferUI;

function copyReferralLink() {
    haptic("light");
    if (!_referralLink) return;
    const link = _referralLink;
    const done = () => showStudioToast(`${link}\n${t("toast_copied")}`, 'success');
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(link).then(done).catch(() => {
            showStudioInputModal({
                title: t("ref_card_title"),
                initialValue: link,
                confirmLabel: t("btn_copy"),
                onConfirm: () => {
                    if (navigator.clipboard && navigator.clipboard.writeText) {
                        navigator.clipboard.writeText(link).then(done).catch(() => {});
                    }
                },
            });
        });
    }
}
window.copyReferralLink = copyReferralLink;

function shareReferralLink() {
    haptic("light");
    if (!_referralLink) return;
    const u = "https://t.me/share/url?url=" + encodeURIComponent(_referralLink)
        + "&text=" + encodeURIComponent(t("ref_share_text"));
    openTutorialUrl(u);
}
window.shareReferralLink = shareReferralLink;

function openSupportContact() {
    haptic("light");
    // Batch BB-6: the support destination is a Telegram ACCOUNT (the
    // admin), not a web page — openLink() punted users out to a
    // browser tab / t.me landing page. openTelegramLink keeps them
    // inside Telegram, straight in the admin chat. Scoped to this
    // handler only: tutorial links keep the shared openTutorialUrl.
    const url = _supportLink;
    if (!url) return;
    const wa = window.Telegram?.WebApp;
    if (wa && typeof wa.openTelegramLink === "function") {
        wa.openTelegramLink(url);
    } else if (wa && typeof wa.openLink === "function") {
        wa.openLink(url);
    } else {
        window.open(url, "_blank");
    }
}
window.openSupportContact = openSupportContact;

function openTutorialVideo(type = "auto") {
    haptic("light");
    // Registry tutorials (premium polish): index (msg 38), plans,
    // customvoice, downloader, scriptedit, recapadvanced, refer, support,
    // bot — resolved settings-first via resolveTutorialLink().
    if (type && type !== "auto" && type !== "voice" && type !== "recap" && type !== "video"
        && Object.prototype.hasOwnProperty.call(TUTORIAL_LINKS, type)) {
        openTutorialUrl(resolveTutorialLink(type));
        return;
    }
    if (type === "video") {
        openTutorialUrl(tutorialConfig.fallbackUrl || TUTORIAL_LINKS.video);
        return;
    }
    let target = null;
    let fallbackType = type;

    if (type === "voice") {
        target = tutorialConfig.voice;
        fallbackType = "voice";
    } else if (type === "recap") {
        target = tutorialConfig.recap;
        fallbackType = "recap";
    } else {
        // "auto": check which tab is currently active
        const activeTab = document.querySelector(".tab-pane.active")?.id;
        if (activeTab === "tab-recap") {
            target = tutorialConfig.recap;
            fallbackType = "recap";
        } else if (activeTab === "tab-voice") {
            target = tutorialConfig.voice;
            fallbackType = "voice";
        } else {
            // Priority: whichever is enabled
            if (tutorialConfig.voice.enabled) {
                target = tutorialConfig.voice;
                fallbackType = "voice";
            } else if (tutorialConfig.recap.enabled) {
                target = tutorialConfig.recap;
                fallbackType = "recap";
            } else {
                target = null;
            }
        }
    }

    if (!target || !target.enabled) {
        const label = fallbackType === "recap" ? "Movie Recap" : "Voice Clone";
        showStudioToast(tf("tutorial_coming_soon", {label}), 'warning');
        return;
    }

    const url = target.url || tutorialConfig.fallbackUrl || TUTORIAL_LINKS.bot;
    openTutorialUrl(url);
}
window.openTutorialVideo = openTutorialVideo;
window.updateTutorialUI = updateTutorialUI;

// ── Floating Luxury Alert Notification Banner ──────────────────────────────
function showFloatingAlert(title, message, actionCallback, actionText) {
    haptic("heavy");
    const container = document.getElementById("floating-alert-container");
    const titleEl = document.getElementById("floating-alert-title");
    const descEl = document.getElementById("floating-alert-desc");
    const actionBtn = document.getElementById("floating-alert-btn");

    if (!container) return;

    if (titleEl) titleEl.innerText = title;
    if (descEl) descEl.innerText = message;

    if (actionCallback && actionBtn) {
        actionBtn.innerText = actionText || t("alert_btn_view_result");
        actionBtn.style.display = "inline-flex";
        actionBtn.onclick = () => {
            closeFloatingAlert();
            actionCallback();
        };
    } else if (actionBtn) {
        actionBtn.style.display = "none";
    }

    container.classList.remove("hidden");
    void container.offsetWidth;
    container.classList.add("show");

    // Pleasant notification chime using Web Audio API
    try {
        const AudioCtx = window.AudioContext || window.webkitAudioContext;
        if (AudioCtx) {
            const ctx = new AudioCtx();
            const osc = ctx.createOscillator();
            const gain = ctx.createGain();
            osc.type = "sine";
            osc.connect(gain);
            gain.connect(ctx.destination);
            osc.frequency.setValueAtTime(523.25, ctx.currentTime);
            osc.frequency.setValueAtTime(659.25, ctx.currentTime + 0.08);
            osc.frequency.setValueAtTime(783.99, ctx.currentTime + 0.16);
            gain.gain.setValueAtTime(0.08, ctx.currentTime);
            gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.4);
            osc.onended = () => { try { ctx.close(); } catch (e) {} };
            osc.start();
            osc.stop(ctx.currentTime + 0.4);
            setTimeout(() => { try { if (ctx.state !== "closed") ctx.close(); } catch (e) {} }, 700);
        }
    } catch (e) {}

    if (state.alertDismissTimer) clearTimeout(state.alertDismissTimer);
    state.alertDismissTimer = setTimeout(() => {
        closeFloatingAlert();
    }, 8000);
}
window.showFloatingAlert = showFloatingAlert;

function closeFloatingAlert() {
    const container = document.getElementById("floating-alert-container");
    if (!container) return;
    container.classList.remove("show");
    setTimeout(() => {
        container.classList.add("hidden");
    }, 400);
}
window.closeFloatingAlert = closeFloatingAlert;

// ── Batch AZ-4: sliding liquid nav pill + tab-enter ─────────────
// ONE pill sits behind the active nav item; the mover is
// data-driven — it reads whichever .nav-item currently carries
// .active and matches the pill to that item's own box, so a future
// nav item (e.g. AW's TTS tab) joins with no code change. If the
// active item has no layout yet (pre-paint, hidden nav), the pill
// keeps its last position under the previous item.
function moveNavPill() {
    const pill = document.getElementById("nav-active-pill");
    const active = document.querySelector(".bottom-nav .nav-item.active");
    if (!active) return;
    const nav = active.closest(".bottom-nav");
    if (nav && active.dataset.tab) {
        nav.setAttribute("data-active-tab", active.dataset.tab);
    }
    if (nav && !nav._pillRO && typeof ResizeObserver === "function") {
        nav._pillRO = new ResizeObserver(() => requestAnimationFrame(moveNavPill));
        nav._pillRO.observe(nav);
    }
    if (!pill || !active.offsetWidth) return;
    pill.style.width = active.offsetWidth + "px";
    pill.style.height = active.offsetHeight + "px";
    pill.style.transform = "translate(" + active.offsetLeft + "px, " + active.offsetTop + "px)";
}
window.moveNavPill = moveNavPill;

// ── Batch BB-2: liquid segmented capsule (speed selectors) ──────
// Same data-driven measurement as moveNavPill: ONE gradient pill per
// .speed-capsule, matched to whichever .speed-seg currently carries
// .active. Segments do not transition their flex (the pill's spring
// carries the motion), so the target geometry is final the moment
// .active moves and can be measured synchronously. A capsule with no
// layout yet (hidden pane / collapsed group) keeps its last position
// and is re-measured on tab show / resize / language switch / the
// preview surface sync that unhides its group.
function moveSpeedPill(capsule) {
    if (!capsule) return;
    const pill = capsule.querySelector(".speed-capsule-pill");
    const active = capsule.querySelector(".speed-seg.active");
    if (!pill || !active || !active.offsetWidth) return;
    pill.style.width = active.offsetWidth + "px";
    pill.style.height = active.offsetHeight + "px";
    pill.style.transform = "translate(" + active.offsetLeft + "px, " + active.offsetTop + "px)";
    pill.classList.add("on");
}
function moveSpeedPills() {
    document.querySelectorAll(".speed-capsule").forEach(moveSpeedPill);
}
window.moveSpeedPill = moveSpeedPill;
window.moveSpeedPills = moveSpeedPills;
if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => { try { moveSpeedPills(); } catch (e) {} });
}

// ── Batch BD: shared segmented capsule (engine + story pickers) ───
// The BB-2 component generalised (.seg-capsule in style.css): ONE
// gradient pill per capsule, measured onto whichever .capsule-seg
// currently carries .active — the same data-driven measurement as
// moveSpeedPill, including the wrapped story track (offsetTop
// follows the segment's row). Re-measured at the same lifecycle
// points as the speed pills (tab show, resize, language/profile
// repaint, wizard step show, picker clicks, prefs resync).
function moveSegPill(capsule) {
    if (!capsule) return;
    const pill = capsule.querySelector(".seg-capsule-pill");
    const active = capsule.querySelector(".capsule-seg.active");
    if (!pill || !active || !active.offsetWidth) return;
    pill.style.width = active.offsetWidth + "px";
    pill.style.height = active.offsetHeight + "px";
    pill.style.transform = "translate(" + active.offsetLeft + "px, " + active.offsetTop + "px)";
    pill.classList.add("on");
}
function moveSegPills() {
    document.querySelectorAll(".seg-capsule").forEach(moveSegPill);
}
window.moveSegPill = moveSegPill;
window.moveSegPills = moveSegPills;
if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => { try { moveSegPills(); } catch (e) {} });
}

// Tab-enter (AZ-4): the incoming pane carries .tab-enter for one
// crossfade + 6px rise (see style.css); the class is re-armed on
// every switch so the motion replays.
function markPaneEnter(pane) {
    if (!pane) return;
    pane.classList.remove("tab-enter");
    void pane.offsetWidth;
    pane.classList.add("tab-enter");
}
window.addEventListener("resize", () => { try { moveNavPill(); } catch (e) {} try { moveSpeedPills(); } catch (e) {} try { moveSegPills(); } catch (e) {} });

function syncTabAdminLifecycle(tabId) {
    if (tabId === "tab-admin") {
        if (!window._adminProRequested && state.user && state.user.is_admin) {
            window._adminProRequested = true;
            const s = document.createElement("script");
            const _av = (document.querySelector('script[src*="/static/js/app.js"]')?.src.match(/[?&]v=([^&]+)/) || [])[1] || "20261120_p1";
            s.src = "/static/js/admin_pro.js?v=" + _av;
            s.defer = true;
            document.head.appendChild(s);
        }
        loadAdminData();
        if (admCurrentSection === "jobs") loadAdminQueue();
        if (admCurrentSection === "settings") { loadAdminSettings(); loadAdminPricing(); }
        if (window.adminQueueInterval) clearInterval(window.adminQueueInterval);
        window.adminQueueInterval = setInterval(() => {
            if (document.hidden) return;
            const adminPane = document.getElementById("tab-admin");
            if (adminPane && adminPane.classList.contains("active")) {
                if (admCurrentSection === "jobs") loadAdminQueue(true);
            } else {
                clearInterval(window.adminQueueInterval);
            }
        }, 4000);
    } else if (window.adminQueueInterval) {
        clearInterval(window.adminQueueInterval);
        window.adminQueueInterval = null;
    }
}

function pauseInactiveTabMedia(activeTabId) {
    try {
        document.querySelectorAll(".tab-pane").forEach(pane => {
            if (pane.id !== activeTabId && typeof pane.querySelectorAll === "function") {
                pane.querySelectorAll("audio, video").forEach(mediaEl => {
                    try { if (!mediaEl.paused && typeof mediaEl.pause === "function") mediaEl.pause(); } catch (e) {}
                });
            }
        });
    } catch (e) {}
}

function switchTab(tabId) {
    pauseInactiveTabMedia(tabId);
    const navButtons = document.querySelectorAll(".nav-item");
    const targetBtn = document.querySelector(`.nav-item[data-tab="${tabId}"]`);
    navButtons.forEach(b => b.classList.remove("active"));
    if (targetBtn) {
        targetBtn.classList.add("active");
    }
    const topHistBtn = document.getElementById("btn-top-history");
    if (topHistBtn) topHistBtn.classList.toggle("active", tabId === "tab-history");
    document.querySelectorAll(".tab-pane").forEach(pane => {
        pane.classList.remove("active");
    });
    const activePane = document.getElementById(tabId);
    if (activePane) activePane.classList.add("active");
    markPaneEnter(activePane);
    // Tab switches start at the top of the new pane (premium polish).
    window.scrollTo({ top: 0, behavior: "auto" });
    refreshStickyCtas();
    moveNavPill();
    moveSpeedPills();
    moveSegPills();
    if (tabId === "tab-history" && typeof loadUserHistory === "function") {
        loadUserHistory();
    }
    // Batch AW: the TTS tab paints its balance line fresh on entry.
    if (tabId === "tab-tts" && typeof window.refreshTtsQuota === "function") {
        window.refreshTtsQuota();
    }
    // Batch CT: the VIP tab re-syncs plan prices from the API on entry.
    if (tabId === "tab-vip" && typeof refreshPlanPrices === "function") {
        refreshPlanPrices();
    }
    // Batch BZ: first-visit sample prefill rides the same entry hook.
    if (tabId === "tab-tts" && typeof window.maybePrefillTtsSample === "function") {
        window.maybePrefillTtsSample();
    }
    syncTabAdminLifecycle(tabId);
    syncTelegramBackButton();
}
window.switchTab = switchTab;

// ── Language Toggle (i18n) ─────────────────────────────────────────────────
async function toggleLanguage() {
    haptic("medium");
    const nextLang = currentLang === "my" ? "en" : "my";
    setLanguage(nextLang);
    try { await window.__langPersistPromise; } catch (e) {}
    const avTxt = document.getElementById("all-voices-toggle-text");
    const avWrap = document.getElementById("all-voices-dropdown-wrap");
    if (avTxt && avWrap) avTxt.innerText = avWrap.classList.contains("hidden") ? t("voices_show_all") : t("voices_hide_all");
    loadCuratedVoices();
    loadUserProfile();
    updateRecapSplitCostHint();
    // Recap step-1 info chips are painted from the analyze payload,
    // not data-i18n — repaint them in the new language (round-6 P7).
    if (typeof window.refreshRecapInfoMetaLanguage === "function") window.refreshRecapInfoMetaLanguage();
}

// ── Navigation (Tabs) ───────────────────────────────────────────────────────
function setupTabs() {
    document.querySelectorAll(".nav-item").forEach(btn => {
        btn.addEventListener("click", () => {
            const targetTab = btn.getAttribute("data-tab");
            haptic("light");
            switchTab(targetTab);
            requestAnimationFrame(() => { try { moveSpeedPills(); } catch (e) {} try { moveSegPills(); } catch (e) {} });
        });
    });
}

// ── API: Load User Profile ─────────────────────────────────────────────────
async function loadUserProfile() {
    try {
        const res = await fetchWithTimeout("/api/user/me", { headers: getAuthHeaders() });
        if (isAuthFailure(res)) return;
        if (!res.ok) {
            // Failure: surface the Home error + Retry strip. 401 stays
            // with the auth flow above and 403 with the membership
            // gate/ban interceptor; every OTHER failure — sub-500
            // included — must not silently leave the placeholder
            // balances on screen with no error and no retry (hunt #7).
            if (res.status !== 403) setHomeLoadErrorVisible(true);
            return;
        }
        const data = await res.json();
        state.user = data;
        paintPlanHowText();
        const expPill = document.getElementById("home-expiry-pill");
        if (expPill) {
            if (data.earliest_days_left != null && data.earliest_expires_at) {
                expPill.innerText = "⏳ " + tf("vip_grant_days_left", { d: data.earliest_days_left });
                expPill.classList.remove("hidden");
                expPill.classList.toggle("exp-soon", !!data.expiring_soon);
            } else {
                expPill.classList.add("hidden");
                expPill.classList.remove("exp-soon");
            }
        }
        // Custom logo (paid-only): profile flags drive the settings
        // UI; the signed thumbnail URL comes from the logo endpoint.
        state.logoCanUse = !!data.can_use_logo;
        state.logoHas = !!data.has_custom_logo;
        refreshLogoUi();
        refreshRecapQualityLock();
        refreshRecapEngineLock();
        paintVoiceTtsBalanceLine(data);
        paintTtsTodaySurfaces(data);
        // Batch BW: repaint the TTS counter now the per-user limit is
        // known (init painted it before the profile arrived).
        updateTtsCharCount();
        if (data.can_use_logo || data.has_custom_logo) fetchLogoState();
        // Cache the last good profile so the NEXT open paints real
        // balances instantly instead of flashing the gate + 0.0m/0c
        // while revalidating (journey audit 2026-11-02, item 6).
        try { writeCachedUserProfile(data); } catch (e) {}
        // Admins are never entry-gate locked (see entry-gate section).
        if (data.is_admin) unlockEntryGate();
        state.recapInsufMinutesServer = null;
        updateRecapSplitCostHint();

        if (data) {
            setHomeLoadErrorVisible(false);
            updateTutorialUI(data);
            updateReferUI(data);
        }

        // Sync language with user profile if saved
        if (data.language_code && data.language_code !== currentLang) {
            setLanguage(data.language_code);
        }

        const vipBadge = document.getElementById("vip-status-badge");
        const voiceBalChip = document.getElementById("user-voice-bal");
        const recapBalChip = document.getElementById("user-recap-bal");
        
        const catTag = document.getElementById("profile-category-tag");
        const charCount = document.getElementById("char-count");
        const adminNavBtn = document.getElementById("nav-btn-admin");
        const adminTab = document.getElementById("tab-admin");

        if (vipBadge) {
            vipBadge.className = data.is_vip ? "vip-badge vip" : "vip-badge free";
            vipBadge.innerText = data.is_admin ? "👑 ADMIN" : (data.is_vip ? "👑 VIP" : "FREE");
        }
        if (voiceBalChip) {
            voiceBalChip.innerText = data.is_admin ? "🎙️ ∞" : `🎙️ ${data.balance_minutes}m`;
        }
        if (recapBalChip) {
            recapBalChip.innerText = data.is_admin ? "🎬 ∞" : `🎬 ${data.recap_credits || 0}c`;
        }

        if (catTag) {
            catTag.removeAttribute("data-i18n");
            catTag.innerText = data.is_admin ? "ADMIN ⭐" : (data.is_vip ? t("vip_member_pro") : t("vip_member_free"));
            catTag.className = data.is_vip ? "stat-val text-gold" : "stat-val";
        }
        paintVipPlanBalances(data);

        if (charCount) {
            const limit = data.character_limit || (data.is_vip ? (data.character_limit_purchased ?? 5000) : (data.character_limit_free ?? 1000));
            const curLen = (document.getElementById("voice-text-input")?.value || "").length;
            charCount.innerText = `${curLen} / ${limit} ${t("char_counter_unit")}`;
        }

        const recapLimitText = document.getElementById("recap-tier-limit-text");
        if (recapLimitText) {
            if (data.is_vip) {
                recapLimitText.innerText = t("recap_tier_vip_text");
            } else {
                recapLimitText.innerText = t("recap_tier_free_text");
            }
        }

        updateGateBanner(data);
        refreshWelcomeStrip();
        state.canUseAiThumbnail = !!(data.can_use_ai_thumbnail || data.is_vip || data.is_admin);
        state.aiThumbnailCapReached = !!data.ai_thumbnail_cap_reached;
        syncRecapAiThumbnailUI();
        const freeTermsNote = document.getElementById("recap-free-terms-note");
        if (freeTermsNote) {
            // Up-front free terms under the Recap source input
            // (journey audit item 8): FREE users only — VIP/admin
            // never see the 2-minute/watermark note.
            freeTermsNote.classList.toggle("hidden", !!(data.is_admin || data.is_vip));
        }
        if (data.is_banned) { setEntryGateMode("banned", data); lockEntryGate(); }
        updateVoicePreflight();

        // Enable Admin Tab if user is administrator
        if (data.is_admin) {
            if (adminNavBtn) adminNavBtn.classList.remove("hidden");
            // AZ-4: a newly-visible nav item re-flows the dock —
            // re-measure the sliding pill against the active item.
            try { moveNavPill(); } catch (e) {}
            try { moveSpeedPills(); } catch (e) {}
            try { moveSegPills(); } catch (e) {}
            if (adminTab) adminTab.classList.remove("hidden");
        }

        // Auto-resume active background Recap or Voice job if user closed and reopened Mini App
        if (data.active_recap_job_id && !state.recapPollTimer) {
            state.recapJobId = data.active_recap_job_id;
            try { localStorage.setItem("vcp_active_recap_job_id", data.active_recap_job_id); } catch (e) {}
            startRecapJobPolling(data.active_recap_job_id);
        }
        if (data.active_voice_job_id && !state.pollTimer) {
            state.activeJobId = data.active_voice_job_id;
            const vProg = document.getElementById("voice-progress-box");
            if (vProg) vProg.classList.remove("hidden");
            startJobPolling(data.active_voice_job_id);
        }

    } catch (err) {
        console.warn("User profile fetch error:", err);
        // No endless spinner: show a clear error + Retry on Home.
        setHomeLoadErrorVisible(true);
    }
}



// ── API: Curated Voices (12 VoxCPM Personas) ───────────────────────────────
state.personaFilter = "all";

function filterPersonas(filter) {
    haptic("selection");
    state.personaFilter = filter;
    document.querySelectorAll(".filter-chip").forEach(btn => {
        btn.classList.toggle("active", btn.getAttribute("data-filter") === filter);
    });
    renderCuratedVoices();
}

function renderCuratedVoices() {
    const grid = document.getElementById("personas-grid");
    if (!grid) return;
    const filter = state.personaFilter || "all";
    const filtered = (filter === "all")
        ? state.curatedVoices
        : state.curatedVoices.filter(v => v.gender === filter);

    grid.innerHTML = "";
    filtered.forEach(v => {
        const card = document.createElement("div");
        const isSelected = (v.id === state.selectedPersona);
        card.className = `persona-card ${isSelected ? "active" : ""}`;
        card.setAttribute("data-voice-id", v.id);
        // Batch BG (§15): the name renders SHORT (own line) and the
        // parenthetical category becomes its own line under it — the
        // full label used to wrap mid-parenthesis at phone widths.
        const cardCategory = personaCategoryName(v.name);
        card.innerHTML = `
            <div class="persona-header">
                <span class="persona-avatar">${v.avatar}</span>
                <div class="persona-meta">
                    <span class="persona-name">${personaShortName(v.name) || v.name}</span>
                    ${cardCategory ? `<span class="persona-category">${cardCategory}</span>` : ''}
                    <span class="persona-badge">${v.badge}</span>
                </div>
                ${isSelected ? '<span class="selected-indicator">✓</span>' : ''}
            </div>
            <p class="persona-desc">${v.description}</p>
            <div class="persona-footer-row">
                <button type="button" class="voice-preview-btn" onclick="event.stopPropagation(); playVoicePreview('${v.id}', this)">
                    <span class="preview-btn-icon">▶️</span>
                    <span class="preview-btn-label" data-i18n="preview_voice">${t("preview_voice")}</span>
                </button>
            </div>
        `;

        card.addEventListener("click", () => {
            haptic("selection");
            state.selectedPersona = v.id;
            renderCuratedVoices();
            // Compact UX: once a persona is chosen the picker collapses
            // again — the summary chip above the text input is the
            // persistent record of the choice.
            setPersonaPickerOpen(false);
        });

        grid.appendChild(card);
    });

    const countBadge = document.getElementById("personas-count");
    if (countBadge) {
        countBadge.innerText = tf("personas_count_fmt", { n: filtered.length });
    }
    updateSelectedVoiceSummary();
}

// ── Compact Voice UX: selected-voice summary chip + collapsible picker ────
function personaShortName(name) {
    // The picker label carries a parenthetical descriptor
    // ("ကိုမင်း (ရုပ်ရှင်ဇာတ်လမ်းပြောသူ)") that truncates mid-parenthesis at summary
    // chip width; the chip shows the persona's SHORT name only. Since
    // Batch BG the persona CARD also renders the short name on its own
    // line, with the descriptor as a separate category line beneath it
    // (personaCategoryName) — the full label no longer wraps
    // mid-parenthesis inside the card either.
    return String(name || "").replace(/\s*[\(（][^\)）]*[\)）]\s*$/, "").trim();
}

function personaCategoryName(name) {
    // Batch BG (§15): the parenthetical descriptor personaShortName
    // strips — rendered by the persona card as its own smaller line
    // under the name, never wrapped inline inside parentheses again.
    const m = String(name || "").match(/[\(（]([^\)）]*)[\)）]\s*$/);
    return m ? m[1].trim() : "";
}

function updateSelectedVoiceSummary() {
    const nameEl = document.getElementById("voice-selected-name");
    const avatarEl = document.getElementById("voice-selected-avatar");
    if (!nameEl) return;
    const v = (state.curatedVoices || []).find(x => x.id === state.selectedPersona);
    if (v) {
        nameEl.innerText = personaShortName(v.name) || v.name;
        if (avatarEl) avatarEl.innerText = v.avatar || "🎙️";
    } else {
        nameEl.innerText = state.selectedPersona || "—";
        if (avatarEl) avatarEl.innerText = "🎙️";
    }
}

function setPersonaPickerOpen(open) {
    const card = document.getElementById("persona-picker-card");
    if (!card) return;
    card.classList.toggle("picker-collapsed", !open);
    // The picker now expands in place under the summary row, so the
    // row's own Change button is the toggle and carries the aria state
    // (the standalone picker toggle row was removed with the move).
    const toggle = document.getElementById("btn-change-voice");
    if (toggle) toggle.setAttribute("aria-expanded", open ? "true" : "false");
}

function togglePersonaPicker() {
    haptic("selection");
    const card = document.getElementById("persona-picker-card");
    if (!card) return;
    setPersonaPickerOpen(card.classList.contains("picker-collapsed"));
}
window.togglePersonaPicker = togglePersonaPicker;

async function loadCuratedVoices() {
    try {
        const res = await fetchWithTimeout("/api/voices/curated", { headers: getAuthHeaders() });
        if (!res.ok) throw new Error(`curated ${res.status}`);
        const voices = await res.json();
        state.curatedVoices = voices;
        renderCuratedVoices();
    } catch (e) {
        console.warn("Could not load curated voices:", e);
        const grid = document.getElementById("personas-grid");
        if (grid && !(state.curatedVoices || []).length) {
            grid.innerHTML = `<div class="adm-state-error"><span data-i18n="home_load_error">${escapeHtml(t("home_load_error"))}</span> <button type="button" class="mini-chip" onclick="loadCuratedVoices()" data-i18n="admin_retry">${escapeHtml(t("admin_retry"))}</button></div>`;
        }
    }
}

// ── API: Legacy Models Loader (removed) ──
async function loadLegacyModels() {
    // Voice v2: legacy engine removed — VoxCPM2 only. Kept as a no-op stub
    // so existing init calls do not break.
    return;
}

// ── Voice Preview Audio Player Controller ──────────────────────────────────
let currentVoiceAudio = null;
let currentPreviewButton = null;
let previewFetchSeq = 0;

function stopCurrentVoicePreview() {
    previewFetchSeq++; // cancel any in-flight sample fetch
    if (currentVoiceAudio) {
        currentVoiceAudio.pause();
        currentVoiceAudio.currentTime = 0;
        currentVoiceAudio = null;
    }
    if (currentPreviewButton) {
        currentPreviewButton.classList.remove("playing");
        const icon = currentPreviewButton.querySelector(".preview-btn-icon, .preview-icon");
        if (icon) icon.innerText = currentPreviewButton.classList.contains("preview-recap-voice-btn") ? "🔊" : "▶️";
        const label = currentPreviewButton.querySelector(".preview-btn-label, [data-i18n='btn_preview_voice']");
        if (label) {
            label.innerText = currentPreviewButton.classList.contains("mini") ? t("preview_voice") : t("btn_preview_voice");
        }
        currentPreviewButton = null;
    }
}

async function playVoicePreview(voiceId, buttonEl) {
    haptic("light");
    if (!voiceId) return;

    if (currentVoiceAudio && currentPreviewButton === buttonEl) {
        stopCurrentVoicePreview();
        return;
    }

    stopCurrentVoicePreview();

    // /api/voices/sample requires the Telegram auth header (privacy
    // fix, hunt #2 V2), which a plain Audio() load cannot send — fetch
    // the sample with auth and play the resulting blob URL, the same
    // pattern as fetchAuthImageUrl for protected images.
    const seq = ++previewFetchSeq;
    const audioUrl = `/api/voices/sample/${encodeURIComponent(voiceId)}`;
    // Loading state NOW (upload-ux pass): the authenticated blob fetch
    // can take seconds, and the button used to look completely
    // untouched until the audio was ready — taps looked ignored, and a
    // failed fetch ended in total silence. Show the load on the button
    // immediately and surface failures.
    currentPreviewButton = buttonEl;
    if (buttonEl) {
        const icon = buttonEl.querySelector(".preview-btn-icon, .preview-icon");
        if (icon) icon.innerText = "⏳";
        const label = buttonEl.querySelector(".preview-btn-label, [data-i18n='btn_preview_voice']");
        if (label) label.innerText = t("preview_loading");
    }
    const authUrl = await fetchAuthImageUrl(audioUrl);
    if (seq !== previewFetchSeq) return; // a newer tap superseded this one
    if (!authUrl) {
        stopCurrentVoicePreview();
        showStudioToast(t("preview_load_failed"), 'error');
        return;
    }
    const audio = new Audio(authUrl);
    currentVoiceAudio = audio;
    currentPreviewButton = buttonEl;

    if (buttonEl) {
        buttonEl.classList.add("playing");
        const icon = buttonEl.querySelector(".preview-btn-icon, .preview-icon");
        if (icon) icon.innerText = "⏸️";
        const label = buttonEl.querySelector(".preview-btn-label, [data-i18n='btn_preview_voice']");
        if (label) label.innerText = t("preview_playing");
    }

    audio.play().catch(e => {
        console.warn("Voice preview playback error:", e);
        stopCurrentVoicePreview();
    });

    audio.onended = () => {
        stopCurrentVoicePreview();
    };

    audio.onerror = () => {
        console.warn("Audio playback failed for voice:", voiceId);
        stopCurrentVoicePreview();
    };
}

// ── Gemini TTS Voice Previews ──────────────────────────────────────
// One owner-approved static sample per engine voice ships with the
// app (static/audio/gemini/preview_<voice>.mp3). Playback mirrors
// playVoicePreview exactly — the same shared player state
// (currentVoiceAudio / currentPreviewButton / previewFetchSeq) and
// the same fetch→blob helper — so a persona preview and a Gemini
// preview stop each other, and the button runs the same
// loading → playing → reset cycle via stopCurrentVoicePreview.
const GEMINI_VOICE_PREVIEW_DIR = "/static/audio/gemini";

function resolveGeminiPreviewVoice() {
    const sel = document.getElementById("recap-gemini-voice-select");
    const chosen = (sel && sel.value) || state.recapGeminiVoice || "";
    if (chosen) return chosen;
    // Auto: mirror the API (app/webapp/api.py) — the recap persona's
    // gender resolves Kore for a female persona, Charon otherwise.
    const slug = state.recapVoice || "komin";
    const entry = (state.curatedVoices || []).find(v => v.id === slug);
    const isFemale = entry ? entry.gender === "female"
        : ["mahnin", "nilar", "mamay", "manadi", "mayoon", "machaw", "mathiri"].includes(slug);
    return isFemale ? "Kore" : "Charon";
}

async function playGeminiVoicePreview(voiceName, buttonEl) {
    haptic("light");
    if (!voiceName) return;

    if (currentVoiceAudio && currentPreviewButton === buttonEl) {
        stopCurrentVoicePreview();
        return;
    }

    stopCurrentVoicePreview();

    const seq = ++previewFetchSeq;
    const audioUrl = `${GEMINI_VOICE_PREVIEW_DIR}/preview_${voiceName.toLowerCase()}.mp3`;
    // Loading state NOW, same as playVoicePreview: show the tap on
    // the button immediately and surface failures instead of silence.
    currentPreviewButton = buttonEl;
    if (buttonEl) {
        const icon = buttonEl.querySelector(".preview-btn-icon, .preview-icon");
        if (icon) icon.innerText = "⏳";
        const label = buttonEl.querySelector(".preview-btn-label, [data-i18n='btn_preview_voice']");
        if (label) label.innerText = t("preview_loading");
    }
    const authUrl = await fetchAuthImageUrl(audioUrl);
    if (seq !== previewFetchSeq) return; // a newer tap superseded this one
    if (!authUrl) {
        stopCurrentVoicePreview();
        showStudioToast(t("preview_load_failed"), 'error');
        return;
    }
    const audio = new Audio(authUrl);
    currentVoiceAudio = audio;
    currentPreviewButton = buttonEl;

    if (buttonEl) {
        buttonEl.classList.add("playing");
        const icon = buttonEl.querySelector(".preview-btn-icon, .preview-icon");
        if (icon) icon.innerText = "⏸️";
        const label = buttonEl.querySelector(".preview-btn-label, [data-i18n='btn_preview_voice']");
        if (label) label.innerText = t("preview_playing");
    }

    audio.play().catch(e => {
        console.warn("Gemini voice preview playback error:", e);
        stopCurrentVoicePreview();
    });

    audio.onended = () => {
        stopCurrentVoicePreview();
    };

    audio.onerror = () => {
        console.warn("Audio playback failed for Gemini voice:", voiceName);
        stopCurrentVoicePreview();
    };
}

function previewSelectedGeminiVoice() {
    const btn = document.getElementById("btn-preview-gemini-voice");
    playGeminiVoicePreview(resolveGeminiPreviewVoice(), btn);
}

// ── Voice Studio Controls & Sub-Modes ──────────────────────────────────────
function switchVoxCPMSubMode(mode) {
    haptic("selection");
    state.voxcpmSubMode = mode;
    const btnCurated = document.getElementById("btn-submode-curated");
    const btnCustom = document.getElementById("btn-submode-custom");
    const curatedCard = document.getElementById("curated-personas-card");
    const refSection = document.getElementById("ref-audio-section");
    const pickerCard = document.getElementById("persona-picker-card");
    const selectedRow = document.getElementById("voice-selected-row");

    if (mode === "curated") {
        if (btnCurated) btnCurated.classList.add("active");
        if (btnCustom) btnCustom.classList.remove("active");
        if (curatedCard) curatedCard.classList.remove("hidden");
        if (refSection) refSection.classList.add("hidden");
        if (pickerCard) pickerCard.classList.remove("hidden");
        if (selectedRow) selectedRow.classList.remove("hidden");
    } else {
        if (btnCustom) btnCustom.classList.add("active");
        if (btnCurated) btnCurated.classList.remove("active");
        if (curatedCard) curatedCard.classList.add("hidden");
        if (refSection) refSection.classList.remove("hidden");
        // Custom mode has no persona picker / summary chip.
        if (pickerCard) pickerCard.classList.add("hidden");
        if (selectedRow) selectedRow.classList.add("hidden");
    }
}

function toggleResultAudioPlay() {
    const player = document.getElementById("result-audio-player");
    const icon = document.getElementById("play-toggle-icon");
    const text = document.getElementById("play-toggle-text");
    if (!player) return;

    if (player.paused) {
        player.play().then(() => {
            if (icon) icon.innerText = "⏸️";
            if (text) text.innerText = t("pause_audio");
        }).catch(err => {
            console.warn("Audio play error:", err);
            // If autoplay is blocked or user needs to click again
            if (icon) icon.innerText = "▶️";
            if (text) text.innerText = t("play_audio");
        });
    } else {
        player.pause();
        if (icon) icon.innerText = "▶️";
        if (text) text.innerText = t("play_audio");
    }
}
window.toggleResultAudioPlay = toggleResultAudioPlay;

function setupAudioPlayerEvents() {
    const player = document.getElementById("result-audio-player");
    if (!player) return;
    player.addEventListener("play", () => {
        const icon = document.getElementById("play-toggle-icon");
        const text = document.getElementById("play-toggle-text");
        if (icon) icon.innerText = "⏸️";
        if (text) text.innerText = t("pause_audio");
    });
    player.addEventListener("pause", () => {
        const icon = document.getElementById("play-toggle-icon");
        const text = document.getElementById("play-toggle-text");
        if (icon) icon.innerText = "▶️";
        if (text) text.innerText = t("play_audio");
    });
    player.addEventListener("ended", () => {
        const icon = document.getElementById("play-toggle-icon");
        const text = document.getElementById("play-toggle-text");
        if (icon) icon.innerText = "🔄";
        if (text) text.innerText = t("replay_audio");
    });
}

function setupVoiceStudio() {
    setupAudioPlayerEvents();
    const textInput = document.getElementById("voice-text-input");
    const charCount = document.getElementById("char-count");

    if (textInput && charCount) {
        textInput.addEventListener("input", () => {
            const len = textInput.value.length;
            const max = state.user?.character_limit || (state.user?.is_vip ? (state.user?.character_limit_purchased ?? 5000) : (state.user?.character_limit_free ?? 1000));
            charCount.innerText = `${len} / ${max} ${t("char_counter_unit")}`;
            charCount.style.color = len > max ? "#ef4444" : "var(--text-muted)";
            updateVoicePreflight();
        });
    }

    // No engine picker: VoxCPM2 is the only engine (Applio removed in Voice v2).

    // Reference Audio File Input Setup
    const refFileInput = document.getElementById("ref-audio-input");
    const refLabel = document.getElementById("ref-audio-label");
    const btnClearRef = document.getElementById("btn-clear-ref-audio");
    const fileUploadBtn = document.querySelector(".btn-file-upload");
    if (refFileInput) {
        refFileInput.addEventListener("change", (e) => {
            const file = e.target.files && e.target.files[0];
            if (file) {
                state.selectedReferenceFile = file;
                if (refLabel) refLabel.innerText = `🎵 ${file.name} (${Math.round(file.size / 1024)} KB)`;
                if (btnClearRef) btnClearRef.classList.remove("hidden");
                if (fileUploadBtn) fileUploadBtn.classList.add("has-file");
                setCustomCloneStep(2, file);
                haptic("selection");
            }
        });
    }

    // Speed Pill Buttons
    const speedPills = document.querySelectorAll("#speed-selector .speed-seg");
    speedPills.forEach(pill => {
        pill.addEventListener("click", () => {
            haptic("selection");
            speedPills.forEach(p => p.classList.remove("active"));
            pill.classList.add("active");
            state.speed = pill.getAttribute("data-speed") || "+12%";
            moveSpeedPill(document.getElementById("speed-selector"));
            updateVoicePreflight();
            if (typeof saveAccountPrefsSoon === "function") saveAccountPrefsSoon();
        });
    });

    // Generate Voice Button
    const btnGen = document.getElementById("btn-generate-voice");
    if (btnGen) {
        btnGen.addEventListener("click", handleVoiceGenerate);
    }
}

function clearRefAudio() {
    state.selectedReferenceFile = null;
    const refFileInput = document.getElementById("ref-audio-input");
    const refLabel = document.getElementById("ref-audio-label");
    const btnClearRef = document.getElementById("btn-clear-ref-audio");
    const fileUploadBtn = document.querySelector(".btn-file-upload");
    if (refFileInput) refFileInput.value = "";
    if (refLabel) refLabel.innerText = t("ref_file_btn");
    if (btnClearRef) btnClearRef.classList.add("hidden");
    if (fileUploadBtn) fileUploadBtn.classList.remove("has-file");
    setCustomCloneStep(1, null);
    haptic("light");
}

function insertSampleText(sample) {
    const input = document.getElementById("voice-text-input");
    if (input) {
        haptic("light");
        input.value = sample;
        input.dispatchEvent(new Event("input"));
    }
}

function clearVoiceText() {
    const input = document.getElementById("voice-text-input");
    if (input) {
        haptic("medium");
        input.value = "";
        input.dispatchEvent(new Event("input"));
    }
}


// Per-chat status chips inside the gate banner: "📢 Channel · ✓ Joined"
// vs "👥 Group · Not joined yet", so the user can see exactly which one
// they still miss instead of guessing (join-reminder UX, 2026-10-30).
function paintGateChip(el, label, ok) {
    if (!el) return;
    el.innerText = label + " · " + (ok ? t("gate_chip_joined") : t("gate_chip_missing"));
    el.classList.toggle("joined", !!ok);
    el.classList.toggle("missing", !ok);
}

function renderGateChips(channelOk, groupOk) {
    // Same chips, two surfaces: the top banner and the entry-gate lock.
    paintGateChip(document.getElementById("gate-chip-channel"), t("gate_chip_channel"), channelOk);
    paintGateChip(document.getElementById("gate-chip-group"), t("gate_chip_group"), groupOk);
    paintGateChip(document.getElementById("gate-lock-chip-channel"), t("gate_chip_channel"), channelOk);
    paintGateChip(document.getElementById("gate-lock-chip-group"), t("gate_chip_group"), groupOk);
}
window.renderGateChips = renderGateChips;

// ── UI/UX (2026-10-29): gate banner + generate preflight ──────────────────
// Gate banner uses only fields /api/user/me already returns (gate branch);
// it never changes job logic. GATE_NOT_CONFIGURED is a server-config error,
// not a "you have not joined" message, so it is shown differently.
// ── Home welcome strip (one-time, first-run only) ──────────────────
// Shown ONLY when every condition holds: Free plan (not VIP, not
// admin), ZERO completed jobs (server count on /api/user/me), and not
// dismissed on this device (localStorage). Values come from the same
// profile payload the header balance chips render, so the strip can
// never promise more than the configured grants.
function refreshWelcomeStrip() {
    const strip = document.getElementById("home-welcome-strip");
    if (!strip) return;
    const u = state.user;
    let show = false;
    try {
        show = !!u && !u.is_admin && !u.is_vip
            && u.completed_jobs === 0
            && localStorage.getItem("vcp_welcome_dismissed") !== "1";
    } catch (e) { show = false; }
    if (show) {
        const txt = document.getElementById("welcome-strip-text");
        if (txt) {
            txt.innerText = tf("welcome_strip_text", {
                credits: u.recap_credits ?? 0,
                mins: u.balance_minutes ?? 0,
            });
        }
        strip.classList.remove("hidden");
    } else {
        strip.classList.add("hidden");
    }
}

function dismissWelcomeStrip() {
    try { localStorage.setItem("vcp_welcome_dismissed", "1"); } catch (e) {}
    const strip = document.getElementById("home-welcome-strip");
    if (strip) strip.classList.add("hidden");
}
window.dismissWelcomeStrip = dismissWelcomeStrip;

function updateGateBanner(data) {
    const banner = document.getElementById("gate-banner");
    if (!banner || !data) return;
    const title = document.getElementById("gate-banner-title");
    const desc = document.getElementById("gate-banner-desc");
    const status = document.getElementById("gate-banner-status");
    const chLink = document.getElementById("gate-link-channel");
    const grLink = document.getElementById("gate-link-group");
    const isMy = (currentLang === "my");
    if (data.is_admin || data.membership_verified) { banner.classList.add("hidden"); return; }
    if (data.gate_configured === false) {
        if (title) title.innerText = t("gate_not_configured_title");
        if (desc) desc.innerText = t("gate_not_configured_desc");
        if (status) status.innerText = "GATE_NOT_CONFIGURED";
        banner.classList.remove("hidden");
        return;
    }
    if (title) title.innerText = t("gate_banner_title");
    if (desc) desc.innerText = t("gate_banner_desc");
    if (chLink && data.channel_invite_link) chLink.href = data.channel_invite_link;
    if (grLink && data.group_invite_link) grLink.href = data.group_invite_link;
    renderGateChips(data.channel_verified, data.group_verified);
    if (status) status.innerText = "";
    banner.classList.remove("hidden");
}

async function verifyMembershipFromBanner() {
    const status = document.getElementById("gate-banner-status");
    const btn = document.getElementById("gate-btn-verify");
    const isMy = (currentLang === "my");
    if (btn) btn.disabled = true;
    if (status) status.innerText = t("gate_checking");
    try {
        // fetchWithTimeout, not raw fetch (hunt #7): a HUNG (not
        // failed) verify request never settled, leaving this button
        // disabled and the status on "checking" for the whole session.
        const res = await fetchWithTimeout("/api/user/verify-membership", { method: "POST", headers: getAuthHeaders() });
        const data = await res.json();
        // The endpoint returns fresh LIVE per-chat flags: repaint the
        // chips either way so a still-missing chat is obvious at a glance.
        renderGateChips(data.channel_verified, data.group_verified);
        if (data.verified) {
            showStudioToast(t("gate_verified_toast"), "success");
            loadUserProfile();
        } else {
            if (status) status.innerText = t("gate_not_member");
            showStudioToast(t("gate_not_found_toast"), "warning");
        }
    } catch (e) {
        if (status) status.innerText = t("gate_check_failed");
    } finally {
        if (btn) btn.disabled = false;
    }
}

window.verifyMembership = verifyMembershipFromBanner; // compat: old voice2 verify name

// ── Mini App ENTRY GATE LOCK (entry-gate, 2026-10-30) ────────────────────
// The banner above only warns; this lock ENFORCES at entry. The page
// loads already locked (body.gate-locked + #gate-lock visible) and the
// app unlocks only when a LIVE POST /api/user/verify-membership check
// returns verified=true — never from the cached /api/user/me flags.
// The check re-runs when the app regains visibility/focus (returning
// from Telegram after joining — or after leaving) and every 60s while
// open, so join-then-leave re-locks an already-open app. Admins are
// never locked: the verify endpoint treats them as verified, and a
// /api/user/me admin flag force-unlocks as a belt-and-braces guard.
// GATE_NOT_CONFIGURED stays a distinct lock (contact admin), it never
// silently unlocks. A failed check (network/401) also stays locked —
// fail-closed, mirroring the server's gated writes.
// Batch AR (2026-11-05): the lock also renders the MAINTENANCE screen
// (mode "maintenance") — while maintenance mode is on, the server
// refuses every non-admin call with MAINTENANCE_MODE, the boot path
// checks /api/config before any cached unlock, and the screen shows
// only the maintenance title + the admin's note.
const entryGate = { locked: true, checking: false, lastCheckAt: 0, mode: "member" };

function lockEntryGate() {
    const overlay = document.getElementById("gate-lock");
    if (overlay) overlay.classList.remove("hidden");
    document.body.classList.add("gate-locked");
    entryGate.locked = true;
}

function unlockEntryGate() {
    const overlay = document.getElementById("gate-lock");
    if (overlay) overlay.classList.add("hidden");
    document.body.classList.remove("gate-locked");
    entryGate.locked = false;
}
window.unlockEntryGate = unlockEntryGate;

function setEntryGateMode(mode, data) {
    entryGate.mode = mode;
    const title = document.getElementById("gate-lock-title");
    const desc = document.getElementById("gate-lock-desc");
    const emoji = document.getElementById("gate-lock-emoji");
    const chips = document.querySelector(".gate-lock-chips");
    const chLink = document.getElementById("gate-lock-link-channel");
    const grLink = document.getElementById("gate-lock-link-group");
    const verifyBtn = document.getElementById("gate-lock-btn-verify");
    const checking = document.getElementById("gate-lock-checking");
    const actions = document.querySelector(".gate-lock-actions");
    if (mode === "checking") {
        // Batch BX: the boot state BEFORE any membership answer —
        // title/desc/chips/links/verify all hidden, only the checking
        // spinner line shows. Purely a display state: the lock
        // overlay stays up and applyEntryGateState remains the only
        // function that can unlock (fail-closed semantics untouched).
        if (emoji) emoji.innerText = "🔐";
        if (title) title.innerText = "";
        if (desc) desc.innerText = "";
        if (chips) chips.classList.add("hidden");
        if (chLink) chLink.classList.add("hidden");
        if (grLink) grLink.classList.add("hidden");
        if (verifyBtn) verifyBtn.classList.add("hidden");
        if (actions) actions.classList.add("hidden");
        if (checking) checking.classList.remove("hidden");
        return;
    }
    if (checking) checking.classList.add("hidden");
    if (actions) actions.classList.remove("hidden");
    if (mode === "maintenance") {
        // Batch AR (2026-11-05): the maintenance screen. Title + the
        // admin's maintenance_note verbatim (or the default text when
        // no note is set) — no join chips, no links, no verify button:
        // there is nothing for the user to do but wait. The note is
        // admin copy, so while it is shown the desc element must NOT
        // carry a data-i18n attribute (a language re-apply would
        // overwrite the note with the translated default).
        if (emoji) emoji.innerText = "🛠️";
        if (title) { title.setAttribute("data-i18n", "maintenance_title"); title.innerText = t("maintenance_title"); }
        const note = (data && data.maintenance_note) ? String(data.maintenance_note).trim() : "";
        if (desc) {
            if (note) { desc.removeAttribute("data-i18n"); desc.innerText = note; }
            else { desc.setAttribute("data-i18n", "maintenance_default_desc"); desc.innerText = t("maintenance_default_desc"); }
        }
        if (chips) chips.classList.add("hidden");
        if (chLink) chLink.classList.add("hidden");
        if (grLink) grLink.classList.add("hidden");
        if (verifyBtn) verifyBtn.classList.add("hidden");
        return;
    }
    if (emoji) emoji.innerText = "🔐";
    if (mode === "not-configured") {
        if (title) { title.setAttribute("data-i18n", "gate_not_configured_title"); title.innerText = t("gate_not_configured_title"); }
        if (desc) { desc.setAttribute("data-i18n", "gate_not_configured_desc"); desc.innerText = t("gate_not_configured_desc"); }
        if (chips) chips.classList.add("hidden");
        if (chLink) chLink.classList.add("hidden");
        if (grLink) grLink.classList.add("hidden");
        if (verifyBtn) verifyBtn.classList.remove("hidden");
        return;
    }
    if (mode === "banned") {
        // Production ban (2026-10-31): a suspended account gets its own
        // lock — no join buttons, no verify button; support is the way out.
        if (title) { title.setAttribute("data-i18n", "gate_banned_title"); title.innerText = t("gate_banned_title"); }
        if (desc) { desc.setAttribute("data-i18n", "gate_banned_desc"); desc.innerText = t("gate_banned_desc"); }
        if (chips) chips.classList.add("hidden");
        if (chLink) chLink.classList.add("hidden");
        if (grLink) grLink.classList.add("hidden");
        if (verifyBtn) verifyBtn.classList.add("hidden");
        return;
    }
    if (verifyBtn) verifyBtn.classList.remove("hidden");
    if (title) { title.setAttribute("data-i18n", "gate_lock_title"); title.innerText = t("gate_lock_title"); }
    if (desc) { desc.setAttribute("data-i18n", "gate_lock_desc"); desc.innerText = t("gate_lock_desc"); }
    if (chips) chips.classList.remove("hidden");
    if (chLink) { chLink.classList.remove("hidden"); if (data && data.channel_invite_link) chLink.href = data.channel_invite_link; }
    if (grLink) { grLink.classList.remove("hidden"); if (data && data.group_invite_link) grLink.href = data.group_invite_link; }
}

function applyEntryGateVerdict(data, reason) {
    if (!data) return;
    const status = document.getElementById("gate-lock-status");
    // Banned users never unlock, even when their membership verifies —
    // the suspension outranks the join gate (writes also 403 server-side).
    if (state.user && state.user.is_banned && !(state.user && state.user.is_admin)) {
        setEntryGateMode("banned", data);
        if (status) status.innerText = "";
        lockEntryGate();
        return;
    }
    // Admins never stay locked (verify already returns verified for them;
    // the /me flag is the second guard if it landed first).
    if (data.verified === true || (state.user && state.user.is_admin)) {
        const wasLocked = entryGate.locked;
        unlockEntryGate();
        if (wasLocked && reason === "manual") showStudioToast(t("gate_verified_toast"), "success");
        if (wasLocked && reason !== "startup") { try { loadUserProfile(); } catch (e) {} }
        return;
    }
    if (data.gate_configured === false) {
        setEntryGateMode("not-configured", data);
        if (status) status.innerText = "GATE_NOT_CONFIGURED";
        lockEntryGate();
        return;
    }
    setEntryGateMode("member", data);
    renderGateChips(data.channel_verified, data.group_verified);
    lockEntryGate();
    if (reason === "manual") {
        if (status) status.innerText = t("gate_not_member");
        showStudioToast(t("gate_not_found_toast"), "warning");
    } else if (status && entryGate.mode === "member") {
        status.innerText = "";
    }
}

let maintenanceLockFetchAt = 0;
async function enterMaintenanceLock() {
    // Batch AR: swap the entry gate to the maintenance screen and
    // lock. The note is re-read from /api/config (the one endpoint
    // that stays open) so an admin edit to the note reaches
    // already-open apps on their next refused call; a short re-fetch
    // window keeps a burst of refused calls from refetching it.
    const now = Date.now();
    if (entryGate.mode === "maintenance" && now - maintenanceLockFetchAt < 5000) {
        lockEntryGate();
        return;
    }
    maintenanceLockFetchAt = now;
    let note = "";
    try {
        const res = await fetchWithTimeout("/api/config", { headers: getAuthHeaders() });
        if (res && res.ok) {
            const cfg = await res.json();
            if (cfg) note = cfg.maintenance_note || "";
        }
    } catch (e) { /* the lock matters most; the default text covers a missing note */ }
    setEntryGateMode("maintenance", { maintenance_note: note });
    lockEntryGate();
}
window.enterMaintenanceLock = enterMaintenanceLock;

async function runEntryGateCheck(reason) {
    const force = reason === "startup" || reason === "manual" || reason === "gate-403";
    if (entryGate.checking) return;
    if (!force && Date.now() - entryGate.lastCheckAt < 5000) return;
    entryGate.checking = true;
    const checkingEl = document.getElementById("gate-lock-checking");
    const btn = document.getElementById("gate-lock-btn-verify");
    if (entryGate.locked && checkingEl) checkingEl.innerText = t("gate_checking");
    if (reason === "manual" && btn) btn.disabled = true;
    try {
        // fetchWithTimeout, not raw fetch (hunt #7): a HUNG (not failed)
        // verify request never settles, so the finally below never runs
        // and entryGate.checking stays true forever — every later
        // check, including the user's own Verify tap, early-returns
        // and the gate can never recover for the whole session.
        const res = await fetchWithTimeout("/api/user/verify-membership", { method: "POST", headers: getAuthHeaders() });
        if (isAuthFailure(res)) {
            // 401: the "open via Telegram" modal explains; stay locked.
            const status = document.getElementById("gate-lock-status");
            if (status) status.innerText = t("gate_check_failed");
            return;
        }
        if (res.status === 403) {
            // Batch AR: while maintenance is on, the server refuses
            // even this check with MAINTENANCE_MODE — for a non-admin
            // that refusal IS the verdict: show the maintenance
            // screen. (Admins are exempt server-side and receive the
            // normal verified verdict, so a 403 here is never an
            // admin.) Other 403s (gate not configured) fall through
            // to the existing verdict handling untouched.
            const errData = await res.clone().json().catch(() => null);
            if (errData && String(errData.detail || "").includes("MAINTENANCE_MODE")) {
                await enterMaintenanceLock();
                return;
            }
        }
        const data = await res.json();
        applyEntryGateVerdict(data, reason);
    } catch (e) {
        // No live verdict -> no unlock (fail-closed).
        const status = document.getElementById("gate-lock-status");
        if (entryGate.locked && status) status.innerText = t("gate_check_failed");
    } finally {
        entryGate.checking = false;
        entryGate.lastCheckAt = Date.now();
        if (checkingEl) checkingEl.innerText = "";
        if (btn) btn.disabled = false;
    }
}
window.runEntryGateCheck = runEntryGateCheck;

async function verifyEntryGate() {
    await runEntryGateCheck("manual");
}
window.verifyEntryGate = verifyEntryGate;

// ── Cached profile boot paint (journey audit 2026-11-02, item 6) ────
// Every successful /api/user/me is cached (see loadUserProfile). On
// the next open the cached payload paints the header instantly and a
// known verified member starts UNLOCKED — no join-gate flash, no
// 0.0m/0c flash. The live startup verify still runs and locks ONLY
// on a confirmed non-member verdict; a network failure changes
// nothing either way. First-ever opens (no cache) keep the
// fail-closed locked start below.
const USER_ME_CACHE_KEY = "vcp_user_me_cache";

// R40 item 8: the profile cache must be keyed to the Telegram user
// who owns it. Telegram Web keeps ONE localStorage across account
// switches, so an unkeyed cache painted the previous account's
// profile at boot — an admin's cache flashed "∞ / ∞" balances to a
// finite user (and vice versa). The envelope records the owner's id
// from initData; a cache whose owner differs (or a legacy unkeyed
// cache) is ignored, leaving the neutral static placeholders until
// the live profile lands.
function currentTelegramUserId() {
    try {
        const u = window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initDataUnsafe
            ? window.Telegram.WebApp.initDataUnsafe.user : null;
        return u && u.id ? String(u.id) : null;
    } catch (e) { return null; }
}

function writeCachedUserProfile(data) {
    try {
        localStorage.setItem(USER_ME_CACHE_KEY, JSON.stringify({
            owner_id: currentTelegramUserId(),
            profile: data,
        }));
    } catch (e) {}
}

function readCachedUserProfile() {
    try {
        const raw = localStorage.getItem(USER_ME_CACHE_KEY);
        if (!raw) return null;
        const parsed = JSON.parse(raw);
        if (!parsed || typeof parsed !== "object" || !("profile" in parsed)) return null;
        const me = currentTelegramUserId();
        // When initData names a user, the cache must belong to them.
        // When it doesn't (non-Telegram dev context), accept the cache.
        if (me && parsed.owner_id && String(parsed.owner_id) !== me) return null;
        if (me && !parsed.owner_id) return null;
        return parsed.profile;
    } catch (e) { return null; }
}

function _fmtCreditNum(v) {
    const n = Number(v || 0);
    if (!Number.isFinite(n)) return "0";
    return Number.isInteger(n) ? String(n) : String(Math.round(n * 100) / 100);
}

/** Format VIP current-plan balance stats via i18n so Burmese/EN units match Home cards. */
function paintVipPlanBalances(data) {
    if (!data) return;
    const balMin = document.getElementById("profile-balance-min");
    const balCredits = document.getElementById("profile-balance-credits");
    const balTts = document.getElementById("vip-wallet-tts-val");
    const resVoice = document.getElementById("vip-res-voice");
    const resCr = document.getElementById("vip-res-credits");
    const fmt = typeof tf === "function" ? tf : null;
    if (balMin) {
        balMin.innerText = data.is_admin
            ? (typeof t === "function" ? t("unlimited") : "Unlimited")
            : (fmt ? fmt("home_free_value_min", { min: data.balance_minutes ?? 0 }) : `${data.balance_minutes ?? 0} mins`);
    }
    if (balCredits) {
        const cr = _fmtCreditNum(data.available_recap_credits ?? data.recap_credits ?? 0);
        balCredits.innerText = data.is_admin
            ? (typeof t === "function" ? t("unlimited") : "Unlimited")
            : (fmt ? fmt("home_free_value_credits", { count: cr }) : `${cr} credits`);
    }
    if (resVoice) {
        const rv = _fmtCreditNum(data.reserved_voice_minutes ?? 0);
        resVoice.innerText = data.is_admin ? "" : `Reserved: ${rv}m`;
    }
    if (resCr) {
        const rr = _fmtCreditNum(data.reserved_recap_credits ?? 0);
        resCr.innerText = data.is_admin ? "" : `Reserved: ${rr} Cr`;
    }
    if (balTts) {
        const rem = data.tts_today_remaining_seconds;
        balTts.innerText = (data.is_admin || data.tts_daily_limit_seconds === 0)
            ? (typeof t === "function" ? t("unlimited") : "Unlimited")
            : (typeof rem === "number"
                ? (fmt ? fmt("home_free_value_min", { min: (rem / 60).toFixed(1) }) : `${(rem / 60).toFixed(1)} mins`)
                : "—");
    }
}
window.paintVipPlanBalances = paintVipPlanBalances;

// Mirrors the header/profile paint in loadUserProfile for the same
// payload shape — keep the two in step when the header changes.
function paintCachedUserHeader(data) {
    const vipBadge = document.getElementById("vip-status-badge");
    const voiceBalChip = document.getElementById("user-voice-bal");
    const recapBalChip = document.getElementById("user-recap-bal");
    const catTag = document.getElementById("profile-category-tag");
    const charCount = document.getElementById("char-count");
    if (vipBadge) {
        vipBadge.className = data.is_vip ? "vip-badge vip" : "vip-badge free";
        vipBadge.innerText = data.is_admin ? "👑 ADMIN" : (data.is_vip ? "👑 VIP" : "FREE");
    }
    if (voiceBalChip) voiceBalChip.innerText = data.is_admin ? "🎙️ ∞" : `🎙️ ${data.balance_minutes}m`;
    if (recapBalChip) recapBalChip.innerText = data.is_admin ? "🎬 ∞" : `🎬 ${_fmtCreditNum(data.available_recap_credits ?? data.recap_credits)}c`;
    if (catTag) {
        catTag.innerText = data.is_admin ? "ADMIN ⭐" : (data.is_vip ? t("vip_purchased") : t("vip_free"));
        catTag.className = data.is_vip ? "stat-val text-gold" : "stat-val";
    }
    paintVipPlanBalances(data);
    if (charCount) {
        const limit = data.character_limit || (data.is_vip ? (data.character_limit_purchased ?? 5000) : (data.character_limit_free ?? 1000));
        charCount.innerText = `0 / ${limit} ${t("char_counter_unit")}`;
    }
    // Logo flags ride the same cached payload (older caches simply
    // lack the keys → treated as Free until the fresh profile lands).
    state.logoCanUse = !!data.can_use_logo;
    state.logoHas = !!data.has_custom_logo;
    refreshLogoUi();
    refreshRecapQualityLock();
    refreshRecapEngineLock();
    if (typeof renderMyRecapPresets === "function") renderMyRecapPresets();
    paintVoiceTtsBalanceLine(data);
    paintTtsTodaySurfaces(data);
}

// Custom channel logo (paid-only, owner decision 2026-11-02): the
// single painter for every logo UI surface — canvas preview chip,
// toggle/position lock state, upload controls, VIP hint and the
// status line. Free users see the toggle + position picker locked
// with a VIP hint and NO upload controls; paid users get upload /
// replace / remove plus a thumbnail of their stored logo. The chip
// shows only when a logo will actually render (paid + uploaded +
// toggle on), so its state can never promise a stamp that the
// server will not draw. The server enforces the same gate on upload
// and on the generate path regardless of what the client sends.
function refreshLogoUi() {
    const u = state.user || {};
    const canUse = !!(u.is_admin || u.is_vip || state.logoCanUse);
    const has = !!state.logoHas;
    state.logoCanUse = canUse;

    const previewLogo = document.getElementById("preview-logo");
    if (previewLogo) {
        // Batch AK: the canvas watermark placeholder is retired —
        // the composited frame renders the user's real logo, so the
        // placeholder never shows (no more fake "LOGO" text).
        previewLogo.style.display = "none";
    }
    const toggleBtn = document.getElementById("btn-toggle-logo");
    if (toggleBtn) {
        toggleBtn.disabled = !canUse;
        toggleBtn.classList.toggle("logo-locked", !canUse);
    }
    const posPicker = document.getElementById("recap-logo-pos-picker");
    if (posPicker) posPicker.classList.toggle("logo-locked", !canUse);

    const controls = document.getElementById("recap-logo-controls");
    if (controls) controls.classList.toggle("hidden", !canUse);
    const hint = document.getElementById("recap-logo-vip-hint");
    if (hint) hint.classList.toggle("hidden", canUse);

    const thumb = document.getElementById("recap-logo-thumb");
    if (thumb) {
        if (has && state.logoUrl) {
            thumb.src = state.logoUrl;
            thumb.classList.remove("hidden");
        } else {
            thumb.removeAttribute("src");
            thumb.classList.add("hidden");
        }
    }
    const uploadBtn = document.getElementById("btn-logo-upload");
    if (uploadBtn) {
        const span = uploadBtn.querySelector("span");
        const key = has ? "btn_logo_replace" : "btn_logo_upload";
        if (span) {
            span.setAttribute("data-i18n", key);
            span.innerText = t(key);
        }
    }
    const removeBtn = document.getElementById("btn-logo-remove");
    if (removeBtn) removeBtn.classList.toggle("hidden", !has);
    const status = document.getElementById("recap-logo-status");
    if (status) {
        status.innerText = !canUse ? "" : (has ? t("recap_logo_ready") : t("recap_logo_none"));
    }
}

// Batch AU: render quality follows the same purchased-or-admin tier
// as the custom-logo gate. /user/me carries the authoritative
// account_category; while that profile is unknown the picker is left
// untouched (the server clamp is the enforcement), and once a FREE
// profile is known its High/Max pills are disabled and any restored
// high/max preference resolves to Standard.
function canUseRecapRenderQuality() {
    const u = state.user || {};
    return !!(u.is_admin || u.account_category === "PURCHASED");
}
function refreshRecapQualityLock() {
    const u = state.user;
    const tierKnown = !!(
        u && (u.is_admin !== undefined || typeof u.account_category === "string")
    );
    const canUse = canUseRecapRenderQuality();
    if (tierKnown && !canUse && state.recapRenderQuality !== "standard") {
        state.recapRenderQuality = "standard";
    }
    document.querySelectorAll("#recap-quality-picker .pill-btn").forEach(btn => {
        const quality = btn.getAttribute("data-quality");
        if (quality === "high" || quality === "max") {
            const locked = tierKnown && !canUse;
            btn.disabled = locked;
            btn.classList.toggle("quality-locked", locked);
            btn.setAttribute("aria-disabled", locked ? "true" : "false");
        }
        btn.classList.toggle("active", quality === state.recapRenderQuality);
    });
    // Batch BE: if the lock just resolved a saved Max preference to
    // Standard, the quote must drop the surcharge it may have shown.
    if (typeof updateRecapSplitCostHint === "function") updateRecapSplitCostHint();
}
window.refreshRecapQualityLock = refreshRecapQualityLock;

// Batch AW-3: free-tier recap narration is Gemini TTS or Edge TTS
// only — the Persona/Clone engine (voxcpm) is purchased + admin.
// Mirrors the Batch AU quality lock exactly: same tier source
// Phase 5: FREE users CAN pick the VoxCPM Persona Engine (using the 12 Persona
// voices when they hold enough Recap Credits AND Voice Clone Minutes).
// Only "My Own Clone Voice" remains paid/VIP-only — checked via canUseRecapCloneEngine().
function canUseRecapCloneEngine() {
    const u = state.user || {};
    return !!(u.is_admin || u.account_category === "PURCHASED");
}
function syncRecapEngineAreas() {
    const eng = state.recapTtsEngine || "voxcpm";
    const gv = document.getElementById("recap-gemini-voice-wrap");
    if (gv) gv.classList.toggle("hidden", eng !== "gemini");
    const pa = document.getElementById("recap-persona-area");
    if (pa) pa.classList.toggle("hidden", eng === "gemini" || eng === "none");
    const ow = document.getElementById("recap-own-voice-wrap");
    if (ow) ow.classList.toggle("hidden", eng !== "voxcpm");
    const note = document.getElementById("recap-persona-engine-note");
    if (note) note.classList.toggle("hidden", eng !== "edge");
    const billingHint = document.getElementById("recap-engine-billing-hint");
    if (billingHint) {
        const hintKey = eng === "none"
            ? "recap_engine_hint_none"
            : (eng === "edge" ? "recap_engine_hint_edge" : (eng === "gemini" ? "recap_engine_hint_gemini" : "recap_engine_hint_voxcpm"));
        billingHint.setAttribute("data-i18n", hintKey);
        billingHint.textContent = t(hintKey);
    }
    const vBadge = document.getElementById("recap-voice-billing-badge");
    if (vBadge) {
        const isCreditOnly = eng !== "voxcpm";
        const badgeKey = isCreditOnly ? "recap_badge_credit_only" : "recap_badge_credit_plus_min";
        vBadge.setAttribute("data-i18n", badgeKey);
        vBadge.textContent = t(badgeKey);
        vBadge.classList.toggle("is-credit-only", isCreditOnly);
    }
}
function refreshRecapEngineLock() {
    const u = state.user;
    const tierKnown = !!(
        u && (u.is_admin !== undefined || typeof u.account_category === "string")
    );
    const canUse = canUseRecapCloneEngine();
    // Phase 5: VoxCPM Persona engine is unlocked for ALL tiers (FREE + VIP + Admin).
    document.querySelectorAll("#recap-engine-picker .pill-btn").forEach(btn => {
        const engine = btn.getAttribute("data-engine");
        if (engine === "voxcpm") {
            btn.disabled = false;
            btn.classList.remove("engine-locked");
            btn.setAttribute("aria-disabled", "false");
        }
        btn.classList.toggle("active", engine === state.recapTtsEngine);
    });
    moveSegPill(document.getElementById("recap-engine-picker"));
    syncRecapEngineAreas();
    // Batch BV / Phase 5: Own Clone Voice remains VIP + Admin only, so
    // a FREE profile still sees #recap-own-voice-card locked with upsell.
    const owCard = document.getElementById("recap-own-voice-card");
    if (owCard) {
        const owLocked = tierKnown && !canUse;
        owCard.classList.toggle("engine-locked", owLocked);
        owCard.setAttribute("aria-disabled", owLocked ? "true" : "false");
        const badge = document.getElementById("recap-own-voice-lock-badge");
        if (badge) badge.classList.toggle("hidden", !owLocked);
        const hint = document.getElementById("recap-own-voice-locked-hint");
        if (hint) hint.classList.toggle("hidden", !owLocked);
        const vipBtn = document.getElementById("btn-recap-own-voice-vip");
        if (vipBtn) vipBtn.classList.toggle("hidden", !owLocked);
        if (owLocked && state.recapOwnVoiceSelected && typeof deselectRecapOwnVoice === "function") {
            deselectRecapOwnVoice();
        }
    }
    if (typeof syncSavedCustomVoiceUi === "function") syncSavedCustomVoiceUi();
}
window.refreshRecapEngineLock = refreshRecapEngineLock;

// ── Batch BV: own-voice narration card ───────────────────────────────
// The user uploads ONE sample for THIS job (upload-on-pick to
// /api/recap/upload_voice_ref, the recap twin of the voice tab's
// streamed save); the submit then references it by handle. Selecting
// a persona card deselects own-voice and vice versa; the picked
// persona stays meaningful — it is the per-segment fallback voice.
let recapOwnVoiceAudio = null;
let recapOwnVoiceObjectUrl = null;

function stopRecapOwnVoicePreview() {
    if (recapOwnVoiceAudio) {
        recapOwnVoiceAudio.pause();
        recapOwnVoiceAudio = null;
    }
    if (recapOwnVoiceObjectUrl) {
        URL.revokeObjectURL(recapOwnVoiceObjectUrl);
        recapOwnVoiceObjectUrl = null;
    }
    const playBtn = document.getElementById("btn-recap-own-voice-play");
    if (playBtn) playBtn.innerText = "🔊";
}

function syncRecapOwnVoiceCard() {
    const card = document.getElementById("recap-own-voice-card");
    const body = document.getElementById("recap-own-voice-body");
    const label = document.getElementById("recap-own-voice-file-label");
    const status = document.getElementById("recap-own-voice-status");
    const playBtn = document.getElementById("btn-recap-own-voice-play");
    const clearBtn = document.getElementById("btn-clear-recap-own-voice");
    const uploadBtn = document.getElementById("recap-own-voice-upload-btn");
    const selected = !!state.recapOwnVoiceSelected;
    if (card) {
        card.classList.toggle("active", selected);
        card.setAttribute("aria-pressed", selected ? "true" : "false");
    }
    if (body) body.classList.toggle("hidden", !selected);
    const hasFile = !!state.recapOwnVoiceFile;
    if (label) {
        label.innerText = hasFile
            ? `🎵 ${state.recapOwnVoiceFileName} (${Math.round(state.recapOwnVoiceFile.size / 1024)} KB)`
            : t("recap_own_voice_upload_btn");
    }
    if (uploadBtn) uploadBtn.classList.toggle("has-file", hasFile);
    if (playBtn) playBtn.classList.toggle("hidden", !hasFile);
    if (clearBtn) clearBtn.classList.toggle("hidden", !hasFile);
    if (status) {
        let text = "";
        if (state.recapOwnVoiceStatus === "uploading") text = t("recap_own_voice_uploading");
        else if (state.recapOwnVoiceStatus === "ready") text = t("recap_own_voice_ready");
        else if (state.recapOwnVoiceStatus === "error") text = t("recap_own_voice_upload_error");
        status.innerText = text;
        status.classList.toggle("hidden", !text);
    }
}

function selectRecapOwnVoice() {
    if (!canUseRecapCloneEngine() && state.user
        && (state.user.is_admin !== undefined || typeof state.user.account_category === "string")) {
        // Locked for this tier: the lock hint is already on the card.
        haptic("error");
        return;
    }
    haptic("selection");
    state.recapOwnVoiceSelected = true;
    document.querySelectorAll("#recap-top-voices .voice-card, #recap-all-voices-grid .voice-card")
        .forEach(c => c.classList.remove("active"));
    syncRecapOwnVoiceCard();
    if (typeof refreshRecapStep4Choices === "function") refreshRecapStep4Choices();
}

function deselectRecapOwnVoice() {
    if (!state.recapOwnVoiceSelected) return;
    state.recapOwnVoiceSelected = false;
    syncRecapOwnVoiceCard();
    if (typeof refreshRecapStep4Choices === "function") refreshRecapStep4Choices();
}

function clearRecapOwnVoice() {
    stopRecapOwnVoicePreview();
    state.recapOwnVoiceSelected = false;
    state.recapOwnVoiceRefId = null;
    state.recapOwnVoiceFile = null;
    state.recapOwnVoiceFileName = "";
    state.recapOwnVoiceStatus = "idle";
    const input = document.getElementById("recap-own-voice-input");
    if (input) input.value = "";
    syncRecapOwnVoiceCard();
    haptic("light");
}
window.clearRecapOwnVoice = clearRecapOwnVoice;

function previewRecapOwnVoice(e) {
    if (e) e.stopPropagation();
    const file = state.recapOwnVoiceFile;
    if (!file) return;
    haptic("light");
    if (recapOwnVoiceAudio) {
        stopRecapOwnVoicePreview();
        return;
    }
    stopCurrentVoicePreview();
    recapOwnVoiceObjectUrl = URL.createObjectURL(file);
    recapOwnVoiceAudio = new Audio(recapOwnVoiceObjectUrl);
    const playBtn = document.getElementById("btn-recap-own-voice-play");
    if (playBtn) playBtn.innerText = "⏸️";
    recapOwnVoiceAudio.addEventListener("ended", stopRecapOwnVoicePreview);
    recapOwnVoiceAudio.play().catch(() => stopRecapOwnVoicePreview());
}
window.previewRecapOwnVoice = previewRecapOwnVoice;

async function uploadRecapOwnVoiceFile(file) {
    state.recapOwnVoiceFile = file;
    state.recapOwnVoiceFileName = file.name || "";
    state.recapOwnVoiceRefId = null;
    state.recapOwnVoiceStatus = "uploading";
    syncRecapOwnVoiceCard();
    try {
        const formData = new FormData();
        formData.append("reference_audio", file);
        const res = await fetchWithTimeout("/api/recap/upload_voice_ref", {
            method: "POST",
            headers: getAuthHeaders(false),
            body: formData,
        }, 120000);
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ref_id) {
            throw new Error(data.detail || "upload failed");
        }
        state.recapOwnVoiceRefId = data.ref_id;
        state.recapOwnVoiceStatus = "ready";
    } catch (err) {
        state.recapOwnVoiceRefId = null;
        state.recapOwnVoiceStatus = "error";
        showStudioToast(t("recap_own_voice_upload_error"), "error");
    }
    syncRecapOwnVoiceCard();
}

function initRecapOwnVoice() {
    const card = document.getElementById("recap-own-voice-card");
    if (card) {
        card.addEventListener("click", selectRecapOwnVoice);
        card.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                selectRecapOwnVoice();
            }
        });
    }
    const input = document.getElementById("recap-own-voice-input");
    if (input) {
        input.addEventListener("change", (e) => {
            const file = e.target.files && e.target.files[0];
            if (file) uploadRecapOwnVoiceFile(file);
        });
    }
    syncRecapOwnVoiceCard();
}
window.initRecapOwnVoice = initRecapOwnVoice;

// Fetch the authoritative logo state (the profile payload carries
// the flags but not the signed preview URL).
async function fetchLogoState() {
    try {
        const res = await fetchWithTimeout("/api/user/logo", { headers: getAuthHeaders(false) });
        if (!res.ok) return;
        const data = await res.json();
        state.logoCanUse = !!data.can_use_logo;
        state.logoHas = !!data.has_logo;
        state.logoUrl = data.logo_url || null;
        refreshLogoUi();
    } catch (e) { /* non-fatal: profile flags already painted the UI */ }
}
window.refreshLogoUi = refreshLogoUi;

// R40 deck I4: the plan "how it works" line states both character
// limits via placeholders, painted from the profile payload's live
// values (never hardcoded in copy). Repainted on profile load and on
// every language switch (setLanguage calls the window hook).
function paintPlanHowText() {
    const el = document.querySelector('[data-i18n="plan_how_text1"]');
    if (!el) return;
    const u = state.user || {};
    const free = u.character_limit_free ?? 1000;
    const paid = u.character_limit_purchased ?? 5000;
    el.innerText = tf("plan_how_text1", {
        free_chars: Number(free).toLocaleString("en-US"),
        paid_chars: Number(paid).toLocaleString("en-US"),
    });
}
window.paintPlanHowText = paintPlanHowText;

// Returns true when the cached profile is a known verified member
// (or admin) who may start unlocked pending the live check.
function bootFromCachedProfile() {
    const data = readCachedUserProfile();
    if (!data) return false;
    state.user = data;
    paintCachedUserHeader(data);
    if (data.is_banned) return false;
    return !!(data.is_admin || data.membership_verified);
}

async function initEntryGate() {
    // Batch BX: enter the neutral "checking" state first — the
    // statically-shipped lock copy (join text + zero balances) must
    // never paint before the membership answer arrives.
    setEntryGateMode("checking");
    // Batch AR: maintenance is checked BEFORE the cached-profile
    // unlock. While maintenance is on, a non-admin must land on the
    // maintenance screen directly — the page already loads locked,
    // and a cached "verified" paint must not flash the app first.
    // /api/config is unauthenticated, so this works before any
    // identity call; if the fetch fails, boot falls back to the
    // normal path (the startup gate check below still runs, and the
    // server refuses every authenticated call anyway). Admins are
    // recognised by that startup check and unlock as always.
    let maintenanceCfg = null;
    try {
        const cfgRes = await fetchWithTimeout("/api/config", { headers: getAuthHeaders() });
        if (cfgRes && cfgRes.ok) {
            const cfg = await cfgRes.json();
            if (cfg && cfg.maintenance_mode) maintenanceCfg = cfg;
        }
    } catch (e) { /* fall through to the normal boot path */ }
    if (maintenanceCfg) {
        setEntryGateMode("maintenance", maintenanceCfg);
        lockEntryGate();
    } else if (bootFromCachedProfile()) unlockEntryGate(); else lockEntryGate();
    runEntryGateCheck("startup");
    document.addEventListener("visibilitychange", () => {
        if (!document.hidden) runEntryGateCheck("visibility");
    });
    window.addEventListener("focus", () => runEntryGateCheck("focus"));
    setInterval(() => { if (!document.hidden) runEntryGateCheck("interval"); }, 60000);
}
window.initEntryGate = initEntryGate;

// Preflight: show cost/limit BEFORE the user taps Generate, so there is no surprise.
// Uses only /api/user/me fields (balance_minutes, character_limit, is_vip).
function updateVoicePreflight() {
    const box = document.getElementById("voice-preflight");
    const input = document.getElementById("voice-text-input");
    if (!box || !input) return;
    const u = state.user || {};
    const len = input.value.length;
    const max = u.character_limit || (u.is_vip ? (u.character_limit_purchased ?? 5000) : (u.character_limit_free ?? 1000));
    const bal = u.is_admin ? "∞" : `${u.balance_minutes ?? 0}`;
    let line = tf("preflight_line", { len, max, bal });
    if (len > max) line += t("preflight_over_limit");
    else if (!u.is_admin && (u.balance_minutes ?? 0) <= 0) line += t("preflight_balance_empty");
    box.innerText = line;
    box.classList.toggle("preflight-warn", len > max || (!u.is_admin && (u.balance_minutes ?? 0) <= 0));
    syncVoiceExcessAndCost(len, max);
}

// ── Submit Voice Job & Live Polling ─────────────────────────────────────────
async function handleVoiceGenerate() {
    // Batch AC duplicate-submit guard: the button disables on the
    // first tap; an in-flight double-tap is exactly one submit, and
    // an UNCHANGED configuration cannot resubmit after success.
    if (!studioSubmitBegin("voice")) return;
    // Batch AC: a second submit while a job is active is explained IN
    // PLACE (the server gate counts all active jobs for this user) —
    // the studio is never evicted and there is no dead end.
    if (LiveJobs.hasActiveJob() && !(state.user && state.user.is_admin)) {
        studioSubmitFailure("voice");
        showLiveSubmitLimit("voice");
        return;
    }
    const textInput = document.getElementById("voice-text-input");
    const text = textInput ? textInput.value.trim() : "";
    if (!text) {
        studioSubmitFailure("voice");
        showStudioToast(t("toast_empty_text"), 'warning');
        return;
    }

    const maxChars = state.user?.character_limit || (state.user?.is_vip ? (state.user?.character_limit_purchased ?? 5000) : (state.user?.character_limit_free ?? 1000));
    if (text.length > maxChars) {
        studioSubmitFailure("voice");
        showStudioToast(tf("text_too_long", {
            len: text.length,
            tier: state.user?.is_vip ? "VIP" : t("tier_free_label"),
            max: maxChars,
            vip_note: state.user?.is_vip ? "" : t("text_too_long_vip_note"),
        }), 'warning');
        return;
    }

    if (!state._voiceCostConfirmed && !state.user?.is_admin) {
        const estMin = Math.max(0.1, Math.round((((text.length / (17.6 * 1.12)) * 1.15) / 60) * 100) / 100);
        studioSubmitFailure("voice");
        showStudioConfirm(tf("voice_cost_confirm", { min: estMin }), () => {
            state._voiceCostConfirmed = true;
            handleVoiceGenerate();
        });
        return;
    }
    state._voiceCostConfirmed = false;

    haptic("heavy");
    const progressBox = document.getElementById("voice-progress-box");
    const resultBox = document.getElementById("voice-result-box");
    const statusMsg = document.getElementById("voice-status-msg");
    const progressFill = document.getElementById("voice-progress-fill");
    const etaText = document.getElementById("voice-eta-text");
    const btnGen = document.getElementById("btn-generate-voice");

    // Batch AC: the studio survives submit — the detail progress box
    // opens only by choice (state.voiceDetailOpen); the workspace,
    // its text and every control stay exactly as the user left them.
    if (progressBox && state.voiceDetailOpen) progressBox.classList.remove("hidden");
    if (resultBox) resultBox.classList.add("hidden");
    if (btnGen) btnGen.disabled = true;
    setVoiceStage("submit");

    try {
        let res;
        const isVox = true; // Voice v2: VoxCPM2 only
        const presetVoice = (isVox && state.voxcpmSubMode === "curated")
            ? state.selectedPersona
            : null;
        const targetModelSlug = "voxcpm-cloud-ai";

        if (state.selectedReferenceFile) {
            setCustomCloneStep(3);
            const formData = new FormData();
            formData.append("text", text);
            formData.append("model_slug", targetModelSlug);
            formData.append("speed", state.speed);
            formData.append("gender_preset", "m2m");
            formData.append("engine", "voxcpm"); // VoxCPM2 only — backend default
            if (presetVoice) formData.append("preset_voice", presetVoice);
            formData.append("reference_audio", state.selectedReferenceFile);

            res = await fetchWithTimeout("/api/voices/generate-with-audio", {
                method: "POST",
                headers: getAuthHeaders(false),
                body: formData,
            }, 120000);
        } else {
            res = await fetchWithTimeout("/api/voices/generate", {
                method: "POST",
                headers: getAuthHeaders(),
                body: JSON.stringify({
                    text: text,
                    model_slug: targetModelSlug,
                    speed: state.speed,
                    gender_preset: "m2m",
                    engine: "voxcpm", // VoxCPM2 only — backend default
                    preset_voice: presetVoice,
                    voice_ref_id: (state.voxcpmSubMode === "custom" && !state.selectedReferenceFile && state.savedCustomVoiceActiveId)
                        ? state.savedCustomVoiceActiveId
                        : null,
                }),
            }, 30000);
        }

        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.detail || "Voice generation request failed");
        }

        state.activeJobId = data.job_id;
        setVoiceStage("working");
        if (statusMsg) statusMsg.innerText = t("preparing_msg");
        if (progressFill) paintProgressBarFill(progressFill, { status: "QUEUED", progress_percent: 0 });
        if (etaText) etaText.innerText = t("calculating_eta");

        startJobPolling(data.job_id);
        // Batch AC confirmation hand-off: Generate stays in its
        // disabled "already creating" state for THIS configuration
        // (the guard re-arms only when a setting/input changes), a
        // slim inline line carries the job, the pill tracks it, and
        // the confirmation offers Watch / Back to studio.
        studioSubmitSuccess("voice");
        showStudioInlineStatus("voice", t("preparing_msg"));
        LiveJobs.track({ id: data.job_id, kind: "voice", status: "QUEUED", progress_percent: 0, stage_index: 0, stage_total: 1, title: text.slice(0, 70) });
        showSubmitConfirmation("voice", data.job_id, t("confirm_cost_voice"));

    } catch (err) {
        studioSubmitFailure("voice");
        showErrorToast(err, 'voice');
        if (progressBox) progressBox.classList.add("hidden");
        if (btnGen) btnGen.disabled = false;
    }
}

// Batch O: persistent fallback disclosure on the voice result card.
// The completion toast alone was easy to miss; when the status payload
// says the backup engine voiced the job, the note stays on the card.
// Batch BU: the same card also carries the reference auto-trim note
// (payload reference_trimmed) — the two disclosures share the element.
function renderVoiceFallbackNote(data) {
    const noteEl = document.getElementById("voice-fallback-note");
    if (!noteEl) return;
    const notes = [];
    if (data && data.fallback_used) notes.push(t("voice_fallback_notice"));
    if (data && data.reference_trimmed) notes.push(t("voice_ref_trim_notice"));
    noteEl.textContent = notes.join(" ");
    noteEl.classList.toggle("hidden", notes.length === 0);
}

function startJobPolling(jobId) {
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
    state.activeJobId = jobId;
    const failedBoxInit = document.getElementById("voice-failed-box");
    if (failedBoxInit) failedBoxInit.classList.add("hidden");
    const cancelBtnInit = document.getElementById("btn-cancel-voice-job");
    if (cancelBtnInit) cancelBtnInit.classList.remove("hidden");

    let tick = 0;
    let failStreak = 0;
    let voiceMaxRank = 0; // Batch AC: monotonic status rank (never backwards)
    const pollStartedAt = Date.now();
    // Poll-death honesty (upload-ux pass): the old loop swallowed every
    // failure and, at its 4-minute cap, stopped silently — leaving a
    // frozen progress box that looked exactly like a stuck job. Now
    // repeated failures surface a "reconnecting" note, and the cap
    // closes the box with an explanation (the job itself keeps running
    // server-side and lands in History).
    const stopPollingTimedOut = () => {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        const cancelBtn = document.getElementById("btn-cancel-voice-job");
        if (cancelBtn) cancelBtn.classList.add("hidden");
        const box = document.getElementById("voice-progress-box");
        if (box) box.classList.add("hidden");
        const btn = document.getElementById("btn-generate-voice");
        if (btn) btn.disabled = false;
        showStudioToast(t("voice_wait_timeout"), 'info', 6000);
    };
    const registerPollFailure = () => {
        failStreak++;
        if (failStreak === 3) {
            const eta = document.getElementById("voice-eta-text");
            if (eta) eta.innerText = t("voice_reconnecting");
        }
        if (tick > 120) stopPollingTimedOut();
    };
    state.pollTimer = setInterval(async () => {
        tick++;
        try {
            const res = await fetch(`/api/jobs/${jobId}/status`, { headers: getAuthHeaders() });
            if (!res.ok) { registerPollFailure(); return; }
            if (failStreak >= 3) {
                const eta = document.getElementById("voice-eta-text");
                if (eta) eta.innerText = t("calculating_eta");
            }
            failStreak = 0;
            const data = await res.json();

            // Batch AC monotonic guard: a stale/replayed response with
            // an earlier status rank is dropped, never rendered.
            const _voiceRank = { QUEUED: 1, RUNNING: 2, SUCCESS: 3, FAILED: 3, CANCELLED: 3 }[data.status] || 0;
            if (_voiceRank && _voiceRank < voiceMaxRank) return;
            if (_voiceRank) voiceMaxRank = _voiceRank;

            const statusMsg = document.getElementById("voice-status-msg");
            const progressFill = document.getElementById("voice-progress-fill");
            const etaText = document.getElementById("voice-eta-text");
            const progressBox = document.getElementById("voice-progress-box");
            const resultBox = document.getElementById("voice-result-box");
            const audioPlayer = document.getElementById("result-audio-player");
            const btnDl = document.getElementById("btn-download-audio");
            const btnGen = document.getElementById("btn-generate-voice");

            if (data.status === "QUEUED") {
                setVoiceStage("working");
                if (progressFill) paintProgressBarFill(progressFill, { status: "QUEUED", progress_percent: 0 });
                if (statusMsg) statusMsg.innerText = t("cloud_queue_waiting");
                if (etaText) etaText.innerText = tf("voice_elapsed", { sec: Math.max(0, Math.round((Date.now() - pollStartedAt) / 1000)) });
            } else if (data.status === "WAITING_CHOICE") {
                setVoiceStage("working");
                if (progressFill) paintProgressBarFill(progressFill, { status: "WAITING_CHOICE", progress_percent: 0 });
                if (statusMsg) statusMsg.innerText = t("live_job_status_waiting_choice");
                if (etaText) etaText.innerText = t("recap_waiting_choice_bot_hint");
            } else if (data.status === "RUNNING") {
                setVoiceStage("working");
                if (progressFill) paintProgressBarFill(progressFill, { status: "RUNNING", progress_percent: Number(data.progress_percent) || 0 });
                if (statusMsg) statusMsg.innerText = t("voice_synthesizing");
                if (etaText) etaText.innerText = tf("voice_elapsed", { sec: Math.max(0, Math.round((Date.now() - pollStartedAt) / 1000)) });
            } else if (data.status === "SUCCESS") {
                clearInterval(state.pollTimer);
                state.pollTimer = null;
                const cancelBtnDone = document.getElementById("btn-cancel-voice-job");
                if (cancelBtnDone) cancelBtnDone.classList.add("hidden");
                setVoiceStage("success");
                haptic("success");
                if (data.fallback_used) { try { showStudioToast(t("voice_fallback_notice"), 'info'); } catch (e) {} }
                if (data.reference_trimmed) { try { showStudioToast(t("voice_ref_trim_notice"), 'info'); } catch (e) {} }
                renderVoiceFallbackNote(data);
                if (progressBox) progressBox.classList.add("hidden");
                if (resultBox) {
                    resultBox.classList.remove("hidden");
                    // Batch AC: no scroll hijack on completion — the
                    // pill/sheet announce the result; the user opens
                    // it by choice and the studio stays where it was.
                }
                if (btnGen) btnGen.disabled = false;
                if (state.selectedReferenceFile) {
                    setCustomCloneStep(4);
                    const saveBtn = document.getElementById("btn-save-custom-voice-from-result");
                    if (saveBtn) saveBtn.classList.remove("hidden");
                }

                const btnTg = document.getElementById("btn-voice-send-telegram");
                if (btnTg) {
                    btnTg.disabled = false;
                    btnTg.classList.remove("sent");
                    btnTg.innerHTML = `<span class="btn-icon">📱</span> <span>${t("btn_send_voice_telegram")}</span>`;
                }
                applyTelegramSendable("voice", data.telegram_sendable);

                if (audioPlayer && data.output_audio_url) {
                    setAuthMediaSource(audioPlayer, data.output_audio_url).then((ok) => {
                        if (ok) { audioPlayer.load(); audioPlayer.play().catch(() => {}); }
                    });
                }
                if (btnDl && data.output_audio_url) {
                    btnDl.setAttribute("data-dl-url", data.output_audio_url);
                    btnDl.setAttribute("data-dl-name", brandedDlName("Voice", "wav", null, jobId));
                }
                loadUserProfile();
                loadUserHistory();

                showFloatingAlert(
                    t("alert_voice_ready"),
                    t("alert_voice_ready_body1"),
                    () => {
                        if (resultBox) resultBox.scrollIntoView({ behavior: "smooth" });
                    },
                    t("alert_btn_listen")
                );

            } else if (data.status === "FAILED") {
                clearInterval(state.pollTimer);
                state.pollTimer = null;
                const cancelBtnFail = document.getElementById("btn-cancel-voice-job");
                if (cancelBtnFail) cancelBtnFail.classList.add("hidden");
                haptic("error");
                const errText = mapErrorToToast(data.error_message, 'voice');
                showStudioToast(errText, 'error');
                const fMsg = document.getElementById("voice-failed-msg");
                if (fMsg) fMsg.textContent = errText;
                const fBox = document.getElementById("voice-failed-box");
                if (fBox) fBox.classList.remove("hidden");
                if (progressBox) progressBox.classList.add("hidden");
                if (btnGen) btnGen.disabled = false;
                if (state.selectedReferenceFile) setCustomCloneStep(2, state.selectedReferenceFile);
            } else if (data.status === "CANCELLED") {
                // Final-audit batch M: cancelled on another surface
                // (admin console) — stop instead of polling to the
                // timeout with a stale progress card.
                clearInterval(state.pollTimer);
                state.pollTimer = null;
                const cancelBtnStop = document.getElementById("btn-cancel-voice-job");
                if (cancelBtnStop) cancelBtnStop.classList.add("hidden");
                if (progressBox) progressBox.classList.add("hidden");
                if (btnGen) btnGen.disabled = false;
                if (state.selectedReferenceFile) setCustomCloneStep(2, state.selectedReferenceFile);
                showStudioToast(t("voice_cancelled_toast"), 'info', 5000);
                loadUserProfile();
                loadUserHistory();
            }

            if (tick > 120 && data.status !== "SUCCESS" && data.status !== "FAILED") { // 4 minutes timeout
                stopPollingTimedOut();
            }

        } catch (e) {
            console.error(e);
            registerPollFailure();
        }
    }, 2000);
}

window.cancelVoiceJob = function() {
    if (state.activeJobId && typeof window.liveJobCancel === "function") {
        window.liveJobCancel(state.activeJobId, "voice");
    }
};

// ── Recap duration caps ────────────────────────────────────────────────
// The per-job cap mirrors the server and is NEVER raised: free 2 min,
// VIP 10 min (admins exempt). A VIP source over the cap goes through
// the split path instead: the server cuts the source into ≤10-min
// parts and processes each part (see _recap_submit_duration_decision
// in api.py). The client only steers: auto-enable split, keep Start on.
function recapNormalMaxSeconds() {
    return state.user?.is_vip ? 600 : (state.user?.recap_max_duration_seconds || 120);
}
function enableRecapSplitForLongVideo() {
    const toggle = document.getElementById("recap-split-toggle");
    if (toggle && !toggle.checked) {
        toggle.checked = true;
        toggle.dispatchEvent(new Event("change", { bubbles: true }));
    }
}
function notifyRecapSplitAutoEnabled() {
    showStudioToast(t("toast_split_auto"), 'info');
}

// ── Batch CI: duration-cap gate at the EARLIEST point ─────────────
// The server enforces the FREE 120 s / VIP 600 s SOURCE cap only at
// submit (POST /api/recap/generate → HTTP 400), where the reason
// surfaced as a seconds-long toast: a FREE user with a 2:01 source
// walked all four wizard steps, saw a normal quote at Step 4,
// pressed 1-Click and could not tell why nothing happened. The gate
// below evaluates the SAME facts the moment the client learns the
// duration (upload completion / link probe paint through
// updateRecapSplitCostHint, which calls the painter) and keeps the
// explanation on screen:
//   free-blocked — persistent notice on Step 1 (and under the Step-4
//     CTA), advancing past Step 1 refused in showRecapStep, the
//     1-Click button disabled (the sticky CTA mirrors its disabled
//     state), so the user fixes the source instead of the submit.
//   vip-notice — persistent Step-1 notice with the split guidance;
//     never a block (the split path is chosen later in the wizard).
//   clear — in-limit, duration unknown, or admin (server-exempt).
// The server checks stay untouched as the backstop.
function recapDurationGateState(durationSeconds, maxSeconds, isVip, isAdmin) {
    if (isAdmin) return "clear";
    const dur = Number(durationSeconds) || 0;
    // Unknown duration (not learned yet, or a local <video> reporting
    // Infinity for streamed WebM) never gates — the server-measured
    // duration re-evaluates when it lands.
    if (!isFinite(dur) || dur <= 0) return "clear";
    if (dur <= (Number(maxSeconds) || 0)) return "clear";
    return isVip ? "vip-notice" : "free-blocked";
}

function recapGateDurationText(durationSeconds) {
    const total = Math.max(0, Math.floor(Number(durationSeconds) || 0));
    const m = Math.floor(total / 60);
    const s = total % 60;
    return `${m}:${s < 10 ? "0" : ""}${s}`;
}

// The Step-1 / Step-4 notice copy: the same facts the server's
// recap_over_limit_message states (actual duration, tier, max) plus
// the tier's way forward (VIP upsell / split guidance).
function recapOverLimitNoticeText(durationSeconds, maxSeconds, isVip) {
    return tf("recap_over_limit_notice", {
        duration: recapGateDurationText(durationSeconds),
        tier: isVip ? "VIP" : t("tier_free_label"),
        max: Math.floor((Number(maxSeconds) || 0) / 60),
        hint: t(isVip ? "toast_recap_over_hint_vip" : "toast_recap_over_hint_free"),
    });
}

// A server duration rejection the client could not pre-empt (a link
// source whose duration was never learned) is kept here by the
// submit catch and rendered persistently under the Step-4 CTA.
let recapSubmitRejectText = "";

function updateRecapDurationGate() {
    const dur = state.recapSourceDuration || 0;
    const maxSeconds = recapNormalMaxSeconds();
    const isVip = !!state.user?.is_vip;
    const isAdmin = !!state.user?.is_admin;
    const gate = recapDurationGateState(dur, maxSeconds, isVip, isAdmin);
    const step1Note = document.getElementById("recap-overlimit-notice");
    if (step1Note) {
        const show = gate !== "clear";
        step1Note.classList.toggle("hidden", !show);
        if (show) step1Note.innerText = recapOverLimitNoticeText(dur, maxSeconds, isVip);
    }
    const step4Note = document.getElementById("recap-overlimit-notice-step4");
    const btn = document.getElementById("btn-start-recap");
    if (gate === "free-blocked") {
        if (step4Note) {
            step4Note.innerText = recapOverLimitNoticeText(dur, maxSeconds, isVip);
            step4Note.classList.remove("hidden");
        }
        if (btn && !btn.disabled) {
            btn.disabled = true;
            btn.style.opacity = "0.5";
            // Only the gate's OWN disable is ever undone by the gate
            // (see the clear branch) — other owners keep their state.
            btn.setAttribute("data-ci-gate-disabled", "1");
        }
    } else {
        if (step4Note) {
            if (recapSubmitRejectText) {
                const _locFn = (typeof localizeServerText === "function") ? localizeServerText : (x => x);
                step4Note.innerText = _locFn(recapSubmitRejectText);
                step4Note.classList.remove("hidden");
            } else {
                step4Note.classList.add("hidden");
            }
        }
        if (btn && btn.getAttribute("data-ci-gate-disabled")) {
            btn.removeAttribute("data-ci-gate-disabled");
            if (!recapJobIsActive()) {
                btn.disabled = false;
                btn.style.opacity = "";
            }
        }
    }
    if (typeof refreshStickyCtas === "function") refreshStickyCtas();
    return gate;
}
window.updateRecapDurationGate = updateRecapDurationGate;

// Segmented (over-cap) recaps are charged 1 credit PER PART. This
// mirrors the server's planning (_recap_estimate_part_count in api.py
// → plan_parts over the same target): parts of min(split, cap−30s),
// with a tiny tail joining the previous part instead of becoming its
// own. Real cuts move only later (narration pauses), so the actual
// count can be lower than this estimate, never higher.
function estimateRecapSegmentParts(durationSec, splitMinutes) {
    if (!durationSec || durationSec <= 0) return 1;
    const target = Math.min((splitMinutes || 3) * 60, recapNormalMaxSeconds() - 30);
    if (target <= 0) return 1;
    const minTail = Math.min(20, target / 4);
    let parts = 0, start = 0;
    while (start + target < durationSec - 0.05) {
        const cut = start + target;
        if (durationSec - cut < minTail) break;
        parts++;
        start = cut;
    }
    return parts + 1;
}

// Batch AO: the pre-submit quote's duration format ("5:24").
function formatRecapQuoteDuration(durationSec) {
    const totalSec = Math.max(0, Math.round(durationSec || 0));
    return `${Math.floor(totalSec / 60)}:${String(totalSec % 60).padStart(2, "0")}`;
}

function updateRecapSplitCostHint() {
    updateRecapDurationGate();
    refreshRecapStep4Eta();
    const hint = document.getElementById("recap-split-cost-hint");
    if (!hint) return;
    const toggle = document.getElementById("recap-split-toggle");
    const dur = Number(state.recapSourceDuration || 0);
    const segmented = !!(toggle && toggle.checked && dur > recapNormalMaxSeconds() && state.user?.is_vip && !state.user?.is_admin);
    const maxAllowed = recapNormalMaxSeconds();
    const effSec = dur > 0 ? (segmented ? dur : Math.min(dur, maxAllowed || dur)) : 60;
    const estCredits = dur > 0 ? Math.max(0.05, Math.round((effSec / 60) * 100) / 100) : 1;
    const ctaText = document.querySelector("#btn-start-recap .btn-text");
    if (state.user?.is_admin) {
        hint.classList.add("hidden");
        if (ctaText) ctaText.textContent = t("btn_start_recap");
        setRecapStep4Quote(0);
        return;
    }
    if (dur > 0) {
        if (ctaText) ctaText.textContent = tf("btn_start_recap_credits", { credits: _fmtCreditNum(estCredits) });
        const avail = Number(state.user?.available_recap_credits ?? state.user?.recap_credits ?? 0);
        let msg = `${formatRecapQuoteDuration(dur)} ≈ ${_fmtCreditNum(estCredits)} Credit (1 Credit = 1 Min Output)`;
        if (avail < estCredits) msg += " " + t("recap_split_cost_low_balance");
        hint.textContent = msg;
        hint.style.color = avail < estCredits ? "#f87171" : "";
        hint.classList.remove("hidden");
        setRecapStep4Quote(estCredits);
    } else {
        hint.classList.add("hidden");
        hint.textContent = "";
        if (ctaText) ctaText.textContent = t("btn_start_recap");
        setRecapStep4Quote(1);
    }
}

// Batch K: setLanguage's static sweep resets the CTA label — this
// window hook (same pattern as paintPlanHowText) repaints the live
// segmented estimate in the new language.
window.repaintRecapCtaPrice = updateRecapSplitCostHint;

// ── Recap Live Preview visibility state machine ─────────────────────────
// Preview-dependent UI (the canvas mockup, the drag hint, the canvas
// toolbar group inside the settings accordion and the Ratio/Fill, Speed
// and Transform cards) stays hidden until a
// source is actually READY. Before that, the Step-2 card shows a tidy
// empty state — or a loading state while a link is being extracted or a
// file is uploading — so no orphan blur/subtitle controls float over an
// empty canvas. Visibility only: no payloads or defaults change here.
function setRecapPreviewState(mode) {
    state.recapPreviewState = mode;
    // Batch CP: the drop zone's file input is a full-cover overlay hit
    // target; it may only intercept taps while the zone is empty —
    // during loading/ready the progress UI and Cancel button inside
    // the zone must receive the clicks instead.
    const dzFileInput = document.getElementById("recap-video-file-input");
    if (dzFileInput) dzFileInput.style.pointerEvents = mode === "empty" ? "" : "none";
    const ready = mode === "ready";
    const emptyBox = document.getElementById("recap-preview-empty");
    const emptyInner = document.getElementById("recap-preview-empty-inner");
    const loadingInner = document.getElementById("recap-preview-loading-inner");
    const mockupWrap = document.getElementById("recap-mockup-wrap");
    const hint = document.getElementById("preview-hint-tag");
    const controlsGroup = document.getElementById("recap-preview-controls-group");
    const ratioGroup = document.getElementById("recap-preview-ratio-group");
    const speedGroup = document.getElementById("recap-preview-speed-group");
    const transformGroup = document.getElementById("recap-preview-transform-group");
    if (emptyBox) emptyBox.classList.toggle("hidden", ready);
    if (emptyInner) emptyInner.classList.toggle("hidden", mode === "loading");
    if (loadingInner) loadingInner.classList.toggle("hidden", mode !== "loading");
    if (mockupWrap) {
        mockupWrap.classList.toggle("hidden", !ready);
        if (ready) {
            mockupWrap.classList.remove("preview-reveal");
            void mockupWrap.offsetWidth; // restart the reveal animation
            mockupWrap.classList.add("preview-reveal");
        }
    }
    if (hint) hint.classList.toggle("hidden", !ready);
    // R40 item 5 (complaint diagnosis D): the Style/Subtitle/Blur
    // controls group was gated on Analyze SUCCESS, so a user whose
    // link failed verification (or who never pressed Analyze) could
    // not reach the subtitle/blur controls at all — and stale saved
    // positions (e.g. sub_y=67) stayed invisible and unfixable. The
    // controls now follow SOURCE PRESENCE; the canvas mockup above
    // stays preview-gated.
    if (controlsGroup) controlsGroup.classList.toggle("hidden", !(ready || recapHasSource()));
    if (ratioGroup) ratioGroup.classList.toggle("hidden", !ready);
    if (speedGroup) speedGroup.classList.toggle("hidden", !ready);
    if (transformGroup) transformGroup.classList.toggle("hidden", !ready);
    if ((ready || recapHasSource()) && typeof applyActiveRecapStep2Category === "function") {
        applyActiveRecapStep2Category();
    }
    // Batch BB-2: the speed capsule lives inside speedGroup — when it
    // unhides, the sliding pill measures against real layout.
    if (ready) requestAnimationFrame(() => { try { moveSpeedPills(); } catch (e) {} try { moveSegPills(); } catch (e) {} });
    // Batch AV: when a source becomes ready, Step 1 checks off and
    // collapses to its summary strip and Step 2 opens for the user.
    syncRecapStepsWithSource(ready);
}
window.setRecapPreviewState = setRecapPreviewState;

// True when a source is present in any form: a typed URL, a picked or
// uploaded file, a link thumbnail, or a live local <video> preview.
// (R40 item 5: widened from preview-only signals — the controls group
// keys off presence, not off a successful Analyze.)
function recapHasSource() {
    const cv = document.getElementById("canvas-video-player");
    const urlInput = document.getElementById("recap-url-input");
    const fileInput = document.getElementById("recap-video-file-input");
    return Boolean(
        state.recapThumbnailUrl ||
        state.uploadedVideoPath ||
        (urlInput && urlInput.value.trim()) ||
        (fileInput && fileInput.files && fileInput.files.length > 0) ||
        (cv && cv.src && !cv.classList.contains("hidden"))
    );
}
window.recapHasSource = recapHasSource;

// Recap wizard (gated 2026-11-15)
const RECAP_STEP_DONE = { 1: false, 2: false, 3: false, 4: false };
let recapCurrentStep = 1;

function recapStepEls(n) {
    return {
        card: document.getElementById("recap-step-" + n),
    };
}

// Kept name from Batch AV (call sites: setRecapPreviewState, the
// MutationObserver below). In the wizard, "open" means "is the
// current step".
function isRecapStepOpen(n) {
    return recapCurrentStep === n;
}

// Indicator (Batch AZ-9): one liquid segmented capsule. The
// current segment expands into the gradient pill and shows its
// step name (a static data-i18n span in the markup — the i18n
// sweep keeps it in the UI language); completed segments show ✓
// via markRecapStepDone. The AX-2R track/fill/caption are gone:
// progress reads from the tinted ✓ segments + the expanded one.
// This refresh only re-paints the state classes — session-truth
// (AX-3R) lives in RECAP_STEP_DONE and its earners, untouched.
function recapFirstIncompleteBefore(n) {
    for (let m = 1; m < n; m++) { if (!RECAP_STEP_DONE[m]) return m; }
    return null;
}

function refreshRecapStepIndicator() {
    document.querySelectorAll('#recap-steps-nav [data-step-toggle]').forEach(item => {
        const m = parseInt(item.getAttribute("data-step-toggle"), 10);
        const locked = recapFirstIncompleteBefore(m) !== null;
        item.classList.toggle("is-active", m === recapCurrentStep);
        item.classList.toggle("is-done", !!RECAP_STEP_DONE[m]);
        item.classList.toggle("is-locked", locked);
        if (locked) item.setAttribute("aria-disabled", "true");
        else item.removeAttribute("aria-disabled");
    });
}
window.refreshRecapStepIndicator = refreshRecapStepIndicator;

// One door: show step n after enforcing duration cap and step order.
function showRecapStep(n) {
    if (!document.getElementById("recap-step-" + n)) return;
    // Batch CI: a FREE source over the duration cap may not advance
    // past Step 1 — the persistent notice on Step 1 carries the full
    // reason, and this toast repeats it for a tap that went nowhere.
    // VIP (split path) and admins are never blocked here; the server
    // submit check remains the backstop for every tier.
    if (n > 1 && n !== recapCurrentStep
        && recapDurationGateState(state.recapSourceDuration || 0, recapNormalMaxSeconds(), !!state.user?.is_vip, !!state.user?.is_admin) === "free-blocked") {
        showStudioToast(tf("toast_recap_over_limit", {
            tier: t("tier_free_label"),
            m: Math.floor(recapNormalMaxSeconds() / 60),
            hint: t("toast_recap_over_hint_free"),
        }), 'warning', 6000);
        return;
    }
    const missing = recapFirstIncompleteBefore(n);
    if (missing !== null) {
        showStudioToast(tf("recap_step_locked_hint", { n: missing }), "warning", 3000);
        return;
    }
    recapCurrentStep = n;
    [1, 2, 3, 4].forEach(m => {
        const els = recapStepEls(m);
        if (els.card) els.card.classList.toggle("hidden", m !== n);
    });
    // Batch DH: entering Step 2 re-fits the 9:16 preview frame — the
    // boot-time fit runs while the pane is hidden (parent width 0), so
    // the real fit must happen once the step is actually visible.
    if (n === 2 && typeof window.__ddFitPreviewFrame === "function") {
        requestAnimationFrame(() => window.__ddFitPreviewFrame());
    }
    if (n === 2) {
        if (typeof window.scheduleRecapCompositeRefresh === "function") window.scheduleRecapCompositeRefresh();
        if (typeof window.refreshRecapStep2Summary === "function") window.refreshRecapStep2Summary();
    }
    refreshRecapStepIndicator();
    // Batch BO: every step change re-applies the sticky-CTA step
    // gate — the 1-Click CTA bar may show only while Step 4 is the
    // current step (see refreshStickyCtas). This is the one door,
    // so boot (initRecapSteps), Continue buttons, chip taps and
    // the source-flow auto-advance all pass through it.
    refreshStickyCtas();
    // Advance animation (AZ-9): the newly-current segment pops once
    // as it expands into the gradient pill.
    const currentBtn = document.querySelector('#recap-steps-nav [data-step-toggle="' + n + '"]');
    if (currentBtn) {
        currentBtn.classList.remove("pop");
        void currentBtn.offsetWidth;
        currentBtn.classList.add("pop");
    }
    // Batch CF: entering Step 4 also refreshes the honest ETA line —
    // CB wired it only into the cost hint + engine picker, so a
    // navigation/restored-session entry left #recap-step4-eta empty.
    // The refresh is cache-keyed + sequence-guarded, so this extra
    // call is a no-op when the shape has not changed.
    if (n === 4) { refreshRecapStep4Output(); refreshRecapStep4Eta(); }
    if (n === 4) { updateRecapSplitCostHint(); }
    // Batch BB-2: the incoming step card may hold the speed capsule —
    // re-measure its pill now that the card has layout.
    requestAnimationFrame(() => { try { moveSpeedPills(); } catch (e) {} try { moveSegPills(); } catch (e) {} });
    const nav = document.getElementById("recap-steps-nav");
    if (nav) nav.scrollIntoView({ behavior: "smooth", block: "start" });
    syncTelegramBackButton();
}
window.showRecapStep = showRecapStep;

function markRecapStepDone(n, done) {
    // Session-truth (AX-3R): Step 4 never shows ✓, and completion is
    // only ever earned in this session (see the callers).
    if (n === 4) done = false;
    RECAP_STEP_DONE[n] = !!done;
    document.querySelectorAll('[data-step-pill="' + n + '"]')
        .forEach(pill => { pill.textContent = done ? "✓" : String(n); });
    refreshRecapStepIndicator();
}

function syncRecapStepsWithSource(ready) {
    if (!ready || !document.getElementById("recap-step-1")) return;
    if (!RECAP_STEP_DONE[1]) markRecapStepDone(1, true);
    if (!state.recapStepsSourceFlowDone) {
        state.recapStepsSourceFlowDone = true;
        showRecapStep(2);
    }
}

// Step 4 choices line (AX-4): source title · voice name · aspect —
// the summary information the AV strips carried, in one compact,
// never-overflowing line. Purely informational; editing is via the
// indicator chips.
function refreshRecapStep4Choices() {
    const line = document.getElementById("recap-step4-choices");
    if (!line) return;
    const titleEl = document.getElementById("recap-info-title");
    const info = document.getElementById("recap-video-info-box");
    const title = (info && !info.classList.contains("hidden") && titleEl && titleEl.textContent.trim())
        ? titleEl.textContent.trim() : t("recap_summary_no_source");
    const voiceEl = document.getElementById("recap-step4-choice-voice");
    const voiceCard = document.querySelector("#recap-persona-area .voice-card.active .v-name")
        || document.querySelector("#recap-top-voices .voice-card.active .v-name");
    if (voiceEl) {
        let voiceLine = "";
        if (state.recapTtsEngine === "none") {
            voiceLine = t("recap_engine_none");
        } else if (state.recapTtsEngine === "gemini") {
            const gKey = typeof resolveGeminiPreviewVoice === "function"
                ? resolveGeminiPreviewVoice() : (state.recapGeminiVoice || "Charon");
            const gRaw = t("gem_voice_" + String(gKey).toLowerCase());
            const gName = (gRaw && !gRaw.startsWith("gem_voice_") ? gRaw : String(gKey)).replace(/\s*\([^)]*\)\s*$/, "");
            voiceLine = "🎙️ Gemini " + gName;
        } else if (state.recapOwnVoiceSelected && state.recapTtsEngine === "voxcpm") {
            voiceLine = t("recap_own_voice_display");
        } else {
            voiceLine = "🎙️ " + (voiceCard ? voiceCard.textContent.trim() : "");
            if (state.recapTtsEngine === "edge") {
                const activeCard = voiceCard ? voiceCard.closest(".voice-card") : null;
                const slug = activeCard ? activeCard.getAttribute("data-voice") : "";
                const cv = (state.curatedVoices || []).find(v => (v.id || v.slug) === slug);
                const femaleSlugs = ["mahnin", "nilar", "mamay", "manadi", "mayoon", "machaw", "mathiri"];
                const isFemale = cv ? cv.gender === "female" : femaleSlugs.includes(slug || "");
                const edgeVoiceName = t(isFemale ? "tts_edge_nilar" : "tts_edge_thiha").replace(/\s*\([^)]*\)\s*$/, "");
                voiceLine += " " + tf("recap_step4_edge_voice_note", { voice: edgeVoiceName });
            }
        }
        voiceEl.textContent = voiceLine;
    }
    const ratioEl = document.getElementById("recap-step4-choice-ratio");
    const ratioCard = document.querySelector("#recap-preview-ratio-group .ratio-btn.active, #recap-preview-ratio-group .ratio-card.active");
    if (ratioEl) ratioEl.textContent = ratioCard
        ? (ratioCard.getAttribute("data-label") || ratioCard.textContent.trim()) : "";
    const titleSpan = document.getElementById("recap-step4-choice-title");
    if (titleSpan) titleSpan.textContent = "🎬 " + title;
}
window.refreshRecapStep4Choices = refreshRecapStep4Choices;

function getRecapAvailVoiceSeconds() {
    const u = state.user;
    if (!u) return 0;
    if (u.is_admin) return Infinity;
    if (typeof u.balance_seconds === "number") return Math.max(0, u.balance_seconds);
    return Math.max(0, Math.round((Number(u.balance_minutes) || 0) * 60));
}

function getRecapEstVoiceSeconds() {
    const u = state.user;
    if (u && u.is_admin) return 0;
    const eng = state.recapTtsEngine || "voxcpm";
    if (eng !== "voxcpm") return 0;
    const dur = Number(state.recapSourceDuration || 0);
    if (!(dur > 0)) return 60;
    const toggle = document.getElementById("recap-split-toggle");
    const segmented = !!(toggle && toggle.checked && u && u.is_vip);
    const maxAllowed = recapNormalMaxSeconds();
    const eff = segmented ? dur : Math.min(dur, maxAllowed || dur);
    return Math.max(1, Math.ceil(eff * 1.0));
}

function paintRecapDualQuote(creditsCost) {
    const u = state.user;
    const isAdmin = !!(u && u.is_admin);
    const eng = state.recapTtsEngine || "voxcpm";
    const cost = Math.max(0.01, Math.round((Number(creditsCost) || 1) * 100) / 100);

    const creditMathEl = document.getElementById("recap-step4-credit-math");
    const surchargeTag = document.getElementById("recap-quality-max-surcharge-tag");
    if (surchargeTag) surchargeTag.classList.add("hidden");
    if (creditMathEl) {
        if (isAdmin) {
            creditMathEl.textContent = t("recap_step4_credit_admin");
            creditMathEl.classList.remove("hidden");
        } else if (u && typeof u.recap_credits === "number") {
            const before = Math.round(Number(u.available_recap_credits ?? u.recap_credits) * 100) / 100;
            const after = Math.max(0, Math.round((before - cost) * 100) / 100);
            creditMathEl.textContent = tf("recap_step4_credit_math", { before, cost, after });
            creditMathEl.style.color = before < cost ? "#f87171" : "";
            creditMathEl.classList.remove("hidden");
        } else {
            creditMathEl.classList.add("hidden");
        }
    }

    const voicePanel = document.getElementById("recap-step4-voice-panel");
    const creditOnlyEl = document.getElementById("recap-step4-voice-credit-only");
    const voiceEstEl = document.getElementById("recap-step4-voice-est");
    const voiceMathEl = document.getElementById("recap-step4-voice-math");
    const insufBox = document.getElementById("recap-insufficient-minutes-box");
    const insufDesc = document.getElementById("recap-insuf-min-desc");

    if (isAdmin) {
        if (voicePanel) voicePanel.classList.add("hidden");
        if (creditOnlyEl) creditOnlyEl.classList.add("hidden");
        if (insufBox) insufBox.classList.add("hidden");
        return;
    }

    const availSec = getRecapAvailVoiceSeconds();
    const estSec = getRecapEstVoiceSeconds();
    const hasBalKnown = !!(u && (typeof u.balance_seconds === "number" || typeof u.balance_minutes === "number"));

    if (eng === "voxcpm") {
        if (creditOnlyEl) creditOnlyEl.classList.add("hidden");
        if (voicePanel) voicePanel.classList.remove("hidden");
        const estMin = Math.max(0.1, Math.round((estSec / 60) * 100) / 100);
        if (voiceEstEl) voiceEstEl.textContent = tf("recap_step4_voice_value", { min: estMin });
        if (voiceMathEl) {
            if (hasBalKnown) {
                const beforeMin = Math.round((availSec / 60) * 100) / 100;
                const afterMin = Math.max(0, Math.round(((availSec - estSec) / 60) * 100) / 100);
                voiceMathEl.textContent = tf("recap_step4_voice_math", {
                    before: beforeMin,
                    est: estMin,
                    after: afterMin,
                });
                voiceMathEl.style.color = availSec < estSec ? "#f87171" : "";
                voiceMathEl.classList.remove("hidden");
            } else {
                voiceMathEl.classList.add("hidden");
            }
        }
        const srv = state.recapInsufMinutesServer;
        const showInsuf = !!(srv || (hasBalKnown && availSec < estSec));
        if (insufBox) {
            insufBox.classList.toggle("hidden", !showInsuf);
            if (showInsuf && insufDesc) {
                const reqSec = srv ? srv.required_seconds : estSec;
                const avSec = srv ? srv.available_seconds : availSec;
                insufDesc.textContent = tf("recap_insuf_min_desc", {
                    req: Math.max(0.1, Math.round((reqSec / 60) * 100) / 100),
                    avail: Math.max(0, Math.round((avSec / 60) * 100) / 100),
                });
            }
        }
    } else {
        if (voicePanel) voicePanel.classList.add("hidden");
        if (insufBox) insufBox.classList.add("hidden");
        if (creditOnlyEl) {
            const engLabel = eng === "none" ? "No Voice" : (eng === "edge" ? "Edge TTS" : "Gemini AI");
            creditOnlyEl.textContent = tf("recap_step4_voice_credit_only_badge", { engine: engLabel });
            creditOnlyEl.classList.remove("hidden");
        }
    }
}

window.switchRecapTtsEngine = function(engine) {
    const next = engine || "voxcpm";
    state.recapTtsEngine = next;
    state.recapInsufMinutesServer = null;
    document.querySelectorAll("#recap-engine-picker .pill-btn").forEach(b => {
        b.classList.toggle("active", b.getAttribute("data-engine") === next);
    });
    moveSegPills();
    syncRecapEngineAreas();
    refreshRecapStep4Choices();
    updateRecapSplitCostHint();
    const engLabel = next === "none" ? "No Voice" : (next === "edge" ? "Edge TTS" : (next === "gemini" ? "Gemini AI" : "Persona Voice"));
    showStudioToast(tf("recap_fallback_switched_toast", { engine: engLabel }), "success");
};

function setRecapStep4Quote(credits, breakdownText) {
    const cost = Math.max(0, Math.round((Number(credits) || 0) * 100) / 100);
    state._lastStep4QuoteCredits = cost || 1;
    const el = document.getElementById("recap-step4-quote-credits");
    if (el) el.textContent = _fmtCreditNum(cost);
    // Batch BE: the optional breakdown line under the Step-4 total
    // (band + Max surcharge) — shown only when a breakdown applies.
    const bd = document.getElementById("recap-step4-quote-breakdown");
    if (bd) {
        bd.textContent = breakdownText || "";
        bd.classList.toggle("hidden", !breakdownText);
    }
    paintRecapDualQuote(cost);
    syncRecapEngineAreas();
    refreshRecapStep4Choices();
}
window.setRecapStep4Quote = setRecapStep4Quote;

// ── Batch CB: honest ETA text + the Step-4 estimate ─────────────
// ONE formatter for every ETA surface: a RANGE in whole minutes
// (low floored, high ceiled), the under-a-minute wording ONLY when
// the range's high bound is genuinely < 90 s, and NO 1-minute floor
// anywhere — the pre-CB Math.max(1, ...) paint is what let a job
// 50 minutes in claim "~1 min" for tens of minutes.
function formatEtaRangeText(range, mode) {
    if (!Array.isArray(range) || range.length < 2) return "";
    const low = Number(range[0]), high = Number(range[1]);
    if (!isFinite(low) || !isFinite(high) || high <= 0) return "";
    if (high < 90) return t("recap_eta_under_minute");
    const lowMin = Math.max(1, Math.floor(low / 60));
    const highMin = Math.max(lowMin, Math.ceil(high / 60));
    if (lowMin === highMin) return tf("recap_eta_dynamic", { min: lowMin });
    return tf(mode === "estimate" ? "recap_eta_estimate_range" : "recap_eta_range", { low: lowMin, high: highMin });
}

// The Step-4 estimate: the wizard knows duration + engine + quality
// before submit, so it asks the server for THAT shape's total range
// (GET /api/recap/eta-estimate) whenever the shape changes — the
// quote refresh hub (updateRecapSplitCostHint) and the engine
// picker both land here. A null range hides the line: no number is
// ever invented client-side.
let recapStep4EtaSeq = 0;
let recapStep4EtaKey = "";
async function refreshRecapStep4Eta() {
    const el = document.getElementById("recap-step4-eta");
    if (!el) return;
    const dur = state.recapSourceDuration || 0;
    if (!dur) {
        el.textContent = "";
        el.classList.add("hidden");
        recapStep4EtaKey = "";
        return;
    }
    const engine = state.recapTtsEngine || "voxcpm";
    const quality = state.recapRenderQuality || "standard";
    const key = `${dur}|${engine}|${quality}|${currentLang}`;
    if (key === recapStep4EtaKey) return;
    recapStep4EtaKey = key;
    const seq = ++recapStep4EtaSeq;
    try {
        const res = await fetch(`/api/recap/eta-estimate?duration_seconds=${encodeURIComponent(dur)}&tts_engine=${encodeURIComponent(engine)}&render_quality=${encodeURIComponent(quality)}`, { headers: getAuthHeaders() });
        if (!res.ok) throw new Error("eta " + res.status);
        const data = await res.json();
        if (seq !== recapStep4EtaSeq) return; // a newer shape won
        const text = formatEtaRangeText(data.eta_range_seconds, "estimate");
        el.textContent = text;
        el.classList.toggle("hidden", !text);
    } catch (e) {
        if (seq === recapStep4EtaSeq) {
            el.textContent = "";
            el.classList.add("hidden");
        }
    }
}
window.refreshRecapStep4Eta = refreshRecapStep4Eta;

function refreshRecapStep4Thumb() {
    // AX-4R: the Step 4 frame is the SAME engine composite the
    // Step 2 stage shows (state.recapCompositeUrl), at full card
    // width in the chosen aspect — a fixed aspect box means the
    // swap never jumps the layout, and the previous frame stays
    // until the new one is assigned (no blank flash).
    const thumb = document.getElementById("recap-step4-thumb");
    if (!thumb) return;
    const empty = document.getElementById("recap-step4-output-empty");
    const frame = document.getElementById("recap-step4-output-frame");
    const img = document.getElementById("canvas-composite-img");
    const src = state.recapCompositeUrl || (img && img.src) || state.recapThumbnailUrl || "";
    if (frame) {
        const r = state.recapRatio || "9:16";
        let aspect = "9 / 16";
        if (r === "16:9") aspect = "16 / 9";
        else if (r === "1:1") aspect = "1 / 1";
        else if (String(r).toLowerCase() === "original" && thumb.naturalWidth > 0 && thumb.naturalHeight > 0) {
            aspect = thumb.naturalWidth + " / " + thumb.naturalHeight;
        }
        frame.style.aspectRatio = aspect;
        // Portrait frames cap their width so the large preview
        // stays a preview, not a full-screen poster.
        frame.style.maxWidth = (r === "16:9" || r === "1:1") ? "100%" : "320px";
    }
    if (src) {
        if (thumb.getAttribute("src") !== src) thumb.src = src;
        thumb.classList.remove("hidden");
        if (empty) empty.classList.add("hidden");
    } else {
        thumb.classList.add("hidden");
        if (empty) empty.classList.remove("hidden");
    }
}
window.refreshRecapStep4Thumb = refreshRecapStep4Thumb;

// Output facts chips (AX-4R): every value is derived from the live
// config/state at refresh time — nothing is hard-coded per job.
function refreshRecapStep4Facts() {
    const set = (id, text) => {
        const el = document.getElementById(id);
        if (!el) return;
        el.textContent = text || "";
        el.classList.toggle("hidden", !text);
    };
    const ratio = state.recapRatio || "9:16";
    const canvasMap = { "9:16": "1080×1920 · 9:16", "16:9": "1920×1080 · 16:9", "1:1": "1080×1080 · 1:1" };
    const canvas = canvasMap[ratio] ||
        ((state.recapSourceWidth && state.recapSourceHeight)
            ? state.recapSourceWidth + "×" + state.recapSourceHeight + " · Original" : "Original");
    set("recap-fact-canvas", "📐 " + canvas);
    const dur = state.recapSourceDuration || 0;
    set("recap-fact-duration", dur > 0 ? "⏱️ " + formatRecapQuoteDuration(dur) : "");
    if (state.recapBurnSubs !== false) {
        const styleKey = "sub_style_" + (state.recapSubStyle || "yellow");
        const styleName = t(styleKey);
        const mKey = "sub_mode_" + (state.recapSubRenderMode || "timeline");
        const mName = t(mKey);
        set("recap-fact-subs", "📝 " + (styleName && styleName !== styleKey ? styleName : (state.recapSubStyle || "yellow")) + ` · ${state.recapFontSize || 16}pt` + (mName && mName !== mKey ? ` · ${mName}` : ""));
    } else {
        set("recap-fact-subs", "📝 " + t("recap_fact_off"));
    }
    set("recap-fact-title", "🛡️ " + t("recap_protection_" + (state.recapProtection || "standard")));
    set("recap-fact-blur", "🌫️ " + t(state.recapBlurSubs !== false ? "recap_fact_on" : "recap_fact_off"));
    const qBtn = document.querySelector("#recap-quality-picker .pill-btn.active");
    set("recap-fact-quality", "🎞️ " + (qBtn ? qBtn.textContent.trim() : (state.recapRenderQuality || "standard")));
    set("recap-fact-speed", "⚡ " + (state.recapSpeed || "+12%"));
    set("recap-fact-mirror", state.recapMirror !== false ? "🪞 " + t("recap_fact_mirror_on") : "");
}
window.refreshRecapStep4Facts = refreshRecapStep4Facts;

// Step 4 entry (AX-4R): re-request the composite with the CURRENT
// full config and refresh every summary surface. The composite
// helper hash-guards its fetch — an unchanged config is already
// the frame on record; a changed one fetches fresh and the success
// path swaps the Step 4 frame in when it lands.
function refreshRecapStep4Output() {
    if (recapHasSource() && typeof window.refreshRecapCompositePreview === "function") {
        try { window.refreshRecapCompositePreview(); } catch (e) { /* keep the current frame */ }
    }
    refreshRecapStep4Thumb();
    refreshRecapStep4Facts();
    refreshRecapStep4Choices();
}
window.refreshRecapStep4Output = refreshRecapStep4Output;

function toggleRecapFineSettings(force) {
    const body = document.getElementById("recap-fine-body");
    if (!body) return;
    const open = typeof force === "boolean" ? force : body.classList.contains("hidden");
    body.classList.toggle("hidden", !open);
    const chevron = document.getElementById("recap-fine-chevron");
    if (chevron) chevron.textContent = open ? "▲" : "▼";
    const btn = document.getElementById("btn-toggle-recap-fine");
    if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
    // The transform group syncs its own visibility from the preview
    // state machine; expanding the fine panel is the user asking for
    // it — reveal the group while the panel is open (Batch AK gating
    // for the collapsed state is unchanged).
    if (open) {
        const tg = document.getElementById("recap-preview-transform-group");
        if (tg && recapHasSource()) tg.classList.remove("hidden");
    }
    // F1 (Batch AV rewire): the blur box's label is an editing
    // affordance — it paints while the detail panel holding the blur
    // group is open (previously: while the Mask toolbar tab was
    // active; the tabs are gone, the behaviour is preserved).
    const blurBox = document.getElementById("preview-blur-box");
    if (blurBox) blurBox.classList.toggle("label-on", open);
}
window.toggleRecapFineSettings = toggleRecapFineSettings;

function initRecapSteps() {
    if (state.recapStepsInitialised) return;
    if (!document.getElementById("recap-steps-nav")) return;
    state.recapStepsInitialised = true;
    // Gated navigation (2026-11-15)
    document.querySelectorAll("[data-step-toggle]").forEach(el => {
        el.addEventListener("click", () => showRecapStep(parseInt(el.getAttribute("data-step-toggle"), 10)));
    });
    document.querySelectorAll("[data-step-next]").forEach(el => {
        el.addEventListener("click", () => {
            const target = parseInt(el.getAttribute("data-step-next"), 10);
            const from = target - 1;
            // Session-truth (AX-3R): a Continue earns its step's ✓
            // only in this session — and Step 2's Continue counts
            // only while a source is present.
            if (from === 2 && recapHasSource() && !RECAP_STEP_DONE[2]) markRecapStepDone(2, true);
            if (from === 3 && !RECAP_STEP_DONE[3]) markRecapStepDone(3, true);
            showRecapStep(target);
        });
    });
    const img = document.getElementById("canvas-composite-img");
    if (img && "MutationObserver" in window) {
        new MutationObserver(() => { if (isRecapStepOpen(4)) refreshRecapStep4Thumb(); })
            .observe(img, { attributes: true, attributeFilter: ["src"] });
    }
    // Session-truth (AX-3R, supersedes AX-3): saved prefs pre-fill
    // control VALUES only — they NEVER mark a step complete. On load
    // no step shows ✓, fresh or returning; every ✓ is earned in this
    // session (Step 1 on resolve, Steps 2/3 on their Continue).
    showRecapStep(1);
}
window.initRecapSteps = initRecapSteps;

// ── TAB 2: Recap Studio Configuration & Visual Mockup ──────────────────────
function setupRecapVisualEditor() {
    // Batch DD: blur-chip / extras render signatures. Declared at the
    // TOP of this scope: the composite-refresh path (defined and first
    // invoked far above the blur-list editor section) reads them, and
    // a `let` declared mid-function throws a TDZ error on that early
    // call (seen live 2026-11-10: "Cannot access 'ddChipsSig' before
    // initialization" on page load).
    let ddChipsSig = "";
    let ddExtrasSig = "";
    const sliderBlurY = document.getElementById("slider-blur-y");
    const sliderBlurH = document.getElementById("slider-blur-h");
    const sliderBlurX = document.getElementById("slider-blur-x");
    const sliderBlurW = document.getElementById("slider-blur-w");
    const sliderBlurStrength = document.getElementById("slider-blur-strength");
    const sliderSubY = document.getElementById("slider-sub-y");
    const sliderSubX = document.getElementById("slider-sub-x");
    const sliderSubW = document.getElementById("slider-sub-w");

    const valBlurY = document.getElementById("val-blur-y");
    const valBlurH = document.getElementById("val-blur-h");
    const valBlurX = document.getElementById("val-blur-x");
    const valBlurW = document.getElementById("val-blur-w");
    const valBlurStrength = document.getElementById("val-blur-strength");
    const valSubY = document.getElementById("val-sub-y");
    const valSubX = document.getElementById("val-sub-x");
    const valSubW = document.getElementById("val-sub-w");

    const previewBlur = document.getElementById("preview-blur-box");
    // Batch AK: the CSS plate element is gone — the composited
    // frame carries the engine's real plate (see S2).
    const previewSub = document.getElementById("preview-sub-box");
    const previewLogo = document.getElementById("preview-logo");
    const canvas = document.getElementById("video-bg-canvas");
    const mockupFrame = document.getElementById("recap-mockup-frame");
    const canvasVideo = document.getElementById("canvas-video-player");

    // Preview starts in the tidy empty state; controls reveal on ready.
    setRecapPreviewState("empty");

    // ── 0. Dual Mode Video Source Switcher (URL vs Direct File Upload) ──
    const btnModeUrl = document.getElementById("btn-source-mode-url");
    const btnModeFile = document.getElementById("btn-source-mode-file");
    const panelUrl = document.getElementById("panel-source-url");
    const panelFile = document.getElementById("panel-source-file");
    const fileInput = document.getElementById("recap-video-file-input");
    const dropZone = document.getElementById("recap-video-drop-zone");

    if (btnModeUrl && btnModeFile) {
        btnModeUrl.addEventListener("click", () => {
            haptic("selection");
            // Batch DF: the no-audio verdict belongs to the uploaded
            // FILE — switching to a link source clears it.
            state.recapNoAudio = false;
            btnModeUrl.classList.add("active");
            btnModeFile.classList.remove("active");
            if (panelUrl) panelUrl.classList.remove("hidden");
            if (panelFile) panelFile.classList.add("hidden");
        });
        btnModeFile.addEventListener("click", () => {
            haptic("selection");
            btnModeFile.classList.add("active");
            btnModeUrl.classList.remove("active");
            if (panelFile) panelFile.classList.remove("hidden");
            if (panelUrl) panelUrl.classList.add("hidden");
        });
    }

    let recapCanvasVideoObjectUrl = null;
    async function handleVideoFileSelect(file) {
        if (!file) return;
        haptic("medium");
        setRecapPreviewState("loading");

        // Instant client-side video preview via Object URL
        if (recapCanvasVideoObjectUrl) {
            try { URL.revokeObjectURL(recapCanvasVideoObjectUrl); } catch (e) {}
            recapCanvasVideoObjectUrl = null;
        }
        const objectUrl = URL.createObjectURL(file);
        recapCanvasVideoObjectUrl = objectUrl;
        const innerMovie = document.getElementById("mockup-inner-movie");
        if (innerMovie) innerMovie.style.backgroundImage = "none";
        // "" (not "none") so the canvas keeps its CSS gradient surface
        // until the blurred backdrop / thumbnail takes over.
        if (canvas) canvas.style.backgroundImage = "";
        if (canvasVideo) {
            canvasVideo.src = objectUrl;
            canvasVideo.classList.remove("hidden");
            canvasVideo.style.display = "block";
            canvasVideo.style.opacity = "1";
            canvasVideo.load();
            canvasVideo.onloadedmetadata = () => {
                const dur = canvasVideo.duration || 60;
                state.recapSourceDuration = dur;
                updateRecapSplitCostHint();
                if (canvasVideo.videoWidth && canvasVideo.videoHeight) {
                    state.recapSourceWidth = canvasVideo.videoWidth;
                    state.recapSourceHeight = canvasVideo.videoHeight;
                    updateCanvasMockupVisuals();
                }
                if (sliderSeek) {
                    sliderSeek.max = Math.min(600, Math.floor(dur));
                    sliderSeek.value = Math.min(5, Math.floor(dur));
                }
                updateSeekTimeDisplay(sliderSeek ? parseFloat(sliderSeek.value) : 5, dur);
                seekCanvasFrame(Math.min(5, Math.floor(dur * 0.2)));
            };
        }

        // Show info card with file name
        const recapInfoBox = document.getElementById("recap-video-info-box");
        const titleSpan = document.getElementById("recap-info-title");
        const platformSpan = document.getElementById("recap-info-platform");
        const fillBadge = document.getElementById("mockup-fill-badge");
        if (fillBadge) fillBadge.style.display = "none";
        if (recapInfoBox) recapInfoBox.classList.remove("hidden");
        // The previous source's thumbnail must not ride along on this
        // pick (round-4 F-D): hide the card image until THIS file's
        // own frame arrives with the upload success.
        setCardThumb(document.getElementById("recap-info-thumb"), null);
        if (titleSpan) {
            titleSpan.removeAttribute("data-i18n");
            titleSpan.innerText = file.name;
        }
        if (platformSpan) {
            platformSpan.dataset.sourceType = "direct_upload";
            platformSpan.innerText = t("recap_source_direct_mp4");
        }
        // A new pick replaces any earlier source: the previous file's
        // confirmed server path and thumbnail must not leak onto this
        // pick — a failed upload of the NEW file must never be able to
        // submit, or claim Ready for, the OLD one.
        state.uploadedVideoPath = null;
        state.recapThumbnailUrl = null;
        state.recapUploadPending = true;

        // Upload with REAL progress (XHR upload events), honest phases,
        // cancel + inline retry: startRecapUpload owns the whole flow,
        // including the success continuation (onRecapUploadSuccess).
        startRecapUpload(file);
    }

    // ── Recap file upload (upload-ux pass) ─────────────────────────────
    // XMLHttpRequest is used on purpose: fetch() exposes no upload
    // progress events, which is how the old UI ended up frozen on a
    // hardcoded "40%" for the whole transfer. Phases stay honest: real
    // byte % while sending, then an indeterminate "server is preparing"
    // phase once the bytes are gone but the response (media probe +
    // thumbnail) is still pending.
    let recapUploadXhr = null;
    let recapUploadTimer = null;

    function recapUploadEls() {
        return {
            wrap: document.getElementById("recap-upload-progress"),
            bar: document.getElementById("recap-upload-bar"),
            label: document.getElementById("recap-upload-label"),
            elapsed: document.getElementById("recap-upload-elapsed"),
            hint: document.getElementById("recap-upload-hint"),
            cancelBtn: document.getElementById("recap-upload-cancel"),
            errorBox: document.getElementById("recap-upload-error"),
            errorText: document.getElementById("recap-upload-error-text"),
            errorHint: document.getElementById("recap-upload-error-hint"),
        };
    }

    // The info card must never claim a server-side Ready the server
    // has not confirmed for the CURRENT pick. States: "pending" (file
    // picked / uploading — neutral duration, not-on-server label),
    // "failed" (upload error — duration cleared, failure label), and
    // "ready" (ONLY from onRecapUploadSuccess; restores the Ready
    // line — the duration chip itself stays owned by the
    // success/analyze writers, which hold the real server metadata).
    // Paint the analyze info card's duration/subtitle chips from an
    // extract payload. Split out (round-6 P7) so a language toggle can
    // repaint the SAME chips in the new language — they used to be
    // baked once, in whichever language was active at analyze time,
    // and the subtitle chip was hardcoded English in both modes.
    function paintRecapInfoMeta(data, isOverLimit, maxAllowed) {
        const durSpan = document.getElementById("recap-info-dur");
        const subSpan = document.getElementById("recap-info-sub");
        if (durSpan) {
            if (data.metadata_verified === false) {
                // Graceful-fallback metadata: no invented duration,
                // no green "Ready" claim (journey-audit honesty rule).
                durSpan.className = "mini-tag";
                durSpan.innerHTML = `⏱️ — <span style="color:#d97706; font-weight:700; margin-left:4px;">(${t("recap_unverified_note")})</span>`;
            } else if (isOverLimit) {
                durSpan.className = "mini-tag text-danger";
                const maxM = Math.floor(maxAllowed / 60);
                const msg = state.user?.is_vip ? tf("recap_over_max_vip", { max: maxM }) : t("recap_over_max_free");
                durSpan.innerHTML = `⏱️ ${escapeHtml(data.estimated_duration || "00:00")} <span style="color:#ef4444; font-weight:700; margin-left:4px;">(${escapeHtml(msg)})</span>`;
            } else {
                durSpan.className = "mini-tag";
                durSpan.innerHTML = `⏱️ ${escapeHtml(data.estimated_duration || "00:00")} <span style="color:#10b981; font-weight:700; margin-left:4px;">(${escapeHtml(t("recap_duration_valid") || "Ready")})</span>`;
            }
        }
        if (subSpan) subSpan.innerText = data.has_subtitles ? t("recap_sub_ready") : t("recap_sub_ai");
    }

    // R39 (Analyze honesty, mirroring the R38 Downloader fix): when
    // the current URL source's duration could NOT be verified, a
    // warning line sits by the 1-Click CTA — the job may fail or
    // turn out to exceed the duration cap, and direct upload is the
    // reliable route. Advisory only: the CTA is never blocked by it.
    // Verified durations (and uploaded files, whose duration the
    // server measured itself) never show it.
    function paintRecapUnverifiedWarning() {
        const el = document.getElementById("recap-unverified-warning");
        if (!el) return;
        const data = state.lastRecapInfo;
        const show = !!data && data.metadata_verified === false && !state.uploadedVideoPath;
        el.classList.toggle("hidden", !show);
        if (show) el.innerText = t("recap_unverified_warning");
    }
    window.paintRecapUnverifiedWarning = paintRecapUnverifiedWarning;

    // R39: clear the analyze info card + cached payload at Analyze
    // start, so the PREVIOUS source's title/duration never stands in
    // for the new one while the new Analyze is still running (the
    // stale-card half of the same honesty fix). The settle paths
    // repaint from the new payload only.
    function clearRecapInfoCard() {
        const box = document.getElementById("recap-video-info-box");
        if (box) box.classList.add("hidden");
        const tSpan = document.getElementById("recap-info-title");
        if (tSpan) tSpan.innerText = "";
        const dSpan = document.getElementById("recap-info-dur");
        if (dSpan) { dSpan.className = "mini-tag"; dSpan.innerText = "⏱️ —"; }
        const pSpan = document.getElementById("recap-info-platform");
        if (pSpan) pSpan.innerText = "";
        const sSpan = document.getElementById("recap-info-sub");
        if (sSpan) sSpan.innerText = "";
        setCardThumb(document.getElementById("recap-info-thumb"), null);
        state.lastRecapInfo = null;
        state.recapSourceDuration = 0;
        state.recapVideoTitle = "";
        // Batch CI: a new source retires any stored server duration
        // rejection — the gate repaint below hides its Step-4 note.
        recapSubmitRejectText = "";
        updateRecapSplitCostHint();
        paintRecapUnverifiedWarning();
    }
    window.clearRecapInfoCard = clearRecapInfoCard;

    // Repaint the cached analyze result after a language change.
    function refreshRecapInfoMetaLanguage() {
        const data = state.lastRecapInfo;
        if (!data) return;
        const box = document.getElementById("recap-video-info-box");
        if (!box || box.classList.contains("hidden")) return;
        const maxAllowed = recapNormalMaxSeconds();
        let isOverLimit = !!(data.duration_seconds && data.duration_seconds > maxAllowed);
        if (isOverLimit && (state.user?.is_admin || state.user?.is_vip)) isOverLimit = false;
        paintRecapInfoMeta(data, isOverLimit, maxAllowed);
        paintRecapUnverifiedWarning();
        // Batch CI: the gate notices are painted imperatively (not
        // via data-i18n), so a language switch re-renders them too.
        updateRecapDurationGate();
    }
    window.refreshRecapInfoMetaLanguage = refreshRecapInfoMetaLanguage;

    function setRecapInfoUploadState(mode) {
        const durSpan = document.getElementById("recap-info-dur");
        const subSpan = document.getElementById("recap-info-sub");
        if (mode === "pending") {
            if (durSpan) { durSpan.className = "mini-tag"; durSpan.innerText = "⏱️ 00:00"; }
            if (subSpan) { subSpan.className = "mini-tag"; subSpan.innerText = t("recap_info_uploading"); }
        } else if (mode === "processing") {
            if (subSpan) { subSpan.className = "mini-tag"; subSpan.innerText = t("recap_info_processing"); }
        } else if (mode === "failed") {
            if (durSpan) { durSpan.className = "mini-tag"; durSpan.innerText = "⏱️ —"; }
            if (subSpan) { subSpan.className = "mini-tag text-danger"; subSpan.innerText = t("recap_info_upload_failed"); }
        } else if (mode === "ready") {
            if (subSpan) { subSpan.className = "mini-tag text-green"; subSpan.innerText = t("recap_info_ready"); }
        }
    }

    function setRecapStartEnabled(enabled) {
        const b = document.getElementById("btn-start-recap");
        if (!b) return;
        b.disabled = !enabled;
        b.style.opacity = enabled ? "" : "0.5";
    }

    function formatUploadClock(ms) {
        const s = Math.max(0, Math.floor(ms / 1000));
        return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    }

    function formatUploadSize(bytes) {
        if (bytes == null) return "";
        if (bytes >= 1024 * 1024 * 1024) return (bytes / (1024 * 1024 * 1024)).toFixed(2) + " GB";
        if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MB";
        if (bytes >= 1024) return Math.round(bytes / 1024) + " KB";
        return bytes + " B";
    }

    function stopRecapUploadChrome() {
        if (recapUploadTimer) { clearInterval(recapUploadTimer); recapUploadTimer = null; }
        recapUploadXhr = null;
    }

    // Drop the local preview after a failed/cancelled upload and fall
    // back to the empty state (unless an earlier source is usable).
    // On upload error the info card itself stays visible
    // (keepInfoCard) so it can show its not-uploaded state next to
    // the error box instead of vanishing.
    function resetRecapUploadPreview(keepInfoCard = false) {
        if (!state.uploadedVideoPath) {
            if (canvasVideo) {
                try { canvasVideo.pause(); } catch (e) { /* noop */ }
                canvasVideo.removeAttribute("src");
                canvasVideo.load();
                canvasVideo.classList.add("hidden");
            }
            if (recapCanvasVideoObjectUrl) {
                try { URL.revokeObjectURL(recapCanvasVideoObjectUrl); } catch (e) {}
                recapCanvasVideoObjectUrl = null;
            }
            const infoBox = document.getElementById("recap-video-info-box");
            if (infoBox && !keepInfoCard) infoBox.classList.add("hidden");
        }
        setRecapPreviewState(recapHasSource() ? "ready" : "empty");
    }

    function startRecapUpload(file) {
        const els = recapUploadEls();
        if (recapUploadXhr) { try { recapUploadXhr.abort(); } catch (e) { /* noop */ } }
        // Client-side size cap (2026-11-03): the server aborts uploads
        // over MAX_RECAP_UPLOAD_BYTES (1 GB) mid-transfer with a 413 —
        // don't burn the user's bandwidth finding that out. Surface the
        // same upload error box the server-failure path uses and never
        // start the XHR.
        if (file && file.size > 1024 * 1024 * 1024) {
            stopRecapUploadChrome();
            state.recapUploadFile = file;
            setRecapInfoUploadState("pending");
            setRecapStartEnabled(false);
            if (els.wrap) els.wrap.classList.remove("hidden");
            onRecapUploadError(t("upload_too_large_1gb"));
            return;
        }
        stopRecapUploadChrome();
        state.recapUploadFile = file;
        // Every (re)start drops the card to the honest pending state
        // and locks the Recap CTA until the server confirms THIS file.
        setRecapInfoUploadState("pending");
        setRecapStartEnabled(false);

        if (els.wrap) els.wrap.classList.remove("hidden");
        if (els.errorBox) els.errorBox.classList.add("hidden");
        if (els.cancelBtn) els.cancelBtn.classList.remove("hidden");
        if (els.bar) {
            els.bar.style.width = "0%";
            els.bar.classList.remove("error", "indeterminate");
            els.bar.classList.add("busy");
        }
        if (els.label) els.label.innerText = tf("upload_progress_label", { pct: 0 });
        if (els.elapsed) els.elapsed.innerText = "⏱ 0:00";
        if (els.hint) {
            els.hint.innerText = tf("upload_expect_hint", { size: formatUploadSize(file.size) });
            els.hint.classList.remove("hidden");
        }

        const startedAt = Date.now();
        let lastProgressAt = Date.now();
        let phase = "uploading";
        let slowHintShown = false;
        recapUploadTimer = setInterval(() => {
            if (els.elapsed) els.elapsed.innerText = "⏱ " + formatUploadClock(Date.now() - startedAt);
            if (phase === "uploading" && !slowHintShown && Date.now() - lastProgressAt > 45000) {
                slowHintShown = true;
                if (els.hint) els.hint.innerText = t("upload_slow_hint");
            }
        }, 500);

        const xhr = new XMLHttpRequest();
        recapUploadXhr = xhr;
        xhr.open("POST", "/api/recap/upload_video");
        const authHeaders = getAuthHeaders(false);
        Object.keys(authHeaders).forEach(k => xhr.setRequestHeader(k, authHeaders[k]));

        xhr.upload.onprogress = (ev) => {
            lastProgressAt = Date.now();
            if (!ev.lengthComputable) return;
            const pct = Math.min(100, Math.round((ev.loaded / ev.total) * 100));
            if (pct >= 100) {
                if (phase !== "processing") { phase = "processing"; setRecapInfoUploadState("processing"); }
                if (els.bar) { els.bar.style.width = "100%"; els.bar.classList.remove("busy"); els.bar.classList.add("indeterminate"); }
                if (els.label) els.label.innerText = t("upload_processing_label");
            } else {
                if (els.bar) els.bar.style.width = pct + "%";
                if (els.label) els.label.innerText = tf("upload_progress_label", { pct });
            }
        };

        xhr.onload = () => {
            stopRecapUploadChrome();
            let data = {};
            try { data = JSON.parse(xhr.responseText || "{}"); } catch (e) { /* keep {} */ }
            if (xhr.status >= 200 && xhr.status < 300) {
                onRecapUploadSuccess(data, file);
            } else {
                onRecapUploadError(mapErrorToToast(data.detail || tf("upload_failed_code", { code: xhr.status }), "upload"));
            }
        };
        xhr.onerror = () => { stopRecapUploadChrome(); onRecapUploadError(t("err_upload_failed")); };
        xhr.onabort = () => {
            if (recapUploadXhr && recapUploadXhr !== xhr) return; // superseded by a newer upload
            stopRecapUploadChrome();
            // The upload is over: nothing is pending any more
            // (round-3 U5). Leaving the flag set locked the Start
            // button's guard even for a pasted URL, with no on-screen
            // explanation, until an analyze happened to clear it.
            state.recapUploadPending = false;
            if (els.bar) els.bar.classList.remove("busy", "indeterminate");
            if (els.label) els.label.innerText = t("upload_cancelled_label");
            if (els.cancelBtn) els.cancelBtn.classList.add("hidden");
            if (els.hint) els.hint.classList.add("hidden");
            setTimeout(() => { if (els.wrap) els.wrap.classList.add("hidden"); }, 1200);
            resetRecapUploadPreview();
        };

        const formData = new FormData();
        formData.append("video_file", file);
        xhr.send(formData);
    }

    function onRecapUploadError(message) {
        const els = recapUploadEls();
        // Same terminal-state invariant as cancel (round-4 F-F): the
        // upload is finished, so nothing is pending. The file-mode
        // guard does not depend on this flag — the failed file has no
        // server path (uploadedVideoPath stays null) and the CTA is
        // disabled below; the flag only gates the URL path.
        state.recapUploadPending = false;
        haptic("error");
        if (els.bar) { els.bar.classList.remove("busy", "indeterminate"); els.bar.classList.add("error"); }
        if (els.label) els.label.innerText = "";
        if (els.cancelBtn) els.cancelBtn.classList.add("hidden");
        if (els.hint) els.hint.classList.add("hidden");
        if (els.errorText) els.errorText.innerText = message;
        if (els.errorHint) els.errorHint.innerText = t("upload_error_hint");
        if (els.errorBox) els.errorBox.classList.remove("hidden");
        // The error box and the info card must tell the same story:
        // drop the local preview, keep the card visible, and strip
        // every Ready claim — this file never reached the server.
        resetRecapUploadPreview(true);
        setRecapInfoUploadState("failed");
        setRecapStartEnabled(false);
    }

    async function onRecapUploadSuccess(data, file) {
        state.uploadedVideoPath = data.local_path;
        // R40 item 9: an upload supersedes any link source — drop the
        // link's verified URL and its server frame (the local <video>
        // element is the frame source for uploads).
        state.recapVerifiedSourceUrl = null;
        if (state.recapFrameUrl) { URL.revokeObjectURL(state.recapFrameUrl); state.recapFrameUrl = null; }
        // Batch AK: the old source's composite must not linger.
        if (state.recapCompositeUrl) { URL.revokeObjectURL(state.recapCompositeUrl); state.recapCompositeUrl = null; }
        recapCompositeHash = "";
        state.recapUploadPending = false;
        // R39: an uploaded file's duration is server-measured — the
        // unverified-link warning by the CTA never applies to it.
        paintRecapUnverifiedWarning();
        state.recapVideoTitle = data.title || file.name;
        state.recapSourceDuration = data.duration_seconds;
        // Batch DF: remember a no-audio verdict for THIS file — the
        // ready-paint below and handleStartRecap both key off it.
        state.recapNoAudio = data.has_audio === false;
        updateRecapSplitCostHint();
        if (data.width && data.height) {
            state.recapSourceWidth = data.width;
            state.recapSourceHeight = data.height;
        }
        // Batch AA: an upload success is a NEW source (every upload
        // gets a fresh server path) — reset the blur box / subtitle
        // Y to the tight defaults for this picture.
        const _aaUploadKey = "file:" + (data.local_path || "");
        if (state.recapLoadedSourceKey !== _aaUploadKey) {
            state.recapLoadedSourceKey = _aaUploadKey;
            resetBlurBoxForNewSource();
        }
        if (data.thumbnail) {
            // /api/recap/frame needs the auth header: fetch it as a
            // blob instead of assigning the raw URL to an <img>.
            const authThumb = data.thumbnail.startsWith("/api/recap/frame")
                ? (await fetchAuthImageUrl(data.thumbnail))
                : data.thumbnail;
            state.recapThumbnailUrl = sanitizeCssMediaUrl(authThumb || data.thumbnail);
            const probeImg = new Image();
            probeImg.onload = () => {
                if (probeImg.naturalWidth && probeImg.naturalHeight && (!state.recapSourceWidth || !state.recapSourceHeight)) {
                    state.recapSourceWidth = probeImg.naturalWidth;
                    state.recapSourceHeight = probeImg.naturalHeight;
                    updateCanvasMockupVisuals();
                }
            };
            probeImg.src = state.recapThumbnailUrl;
            updateCanvasMockupVisuals();
            // The info card shows this file's own frame too (round-4
            // F-D: the upload flow never populated the card image, so
            // it sat on the empty-src broken glyph at the ready moment).
            setCardThumb(document.getElementById("recap-info-thumb"), state.recapThumbnailUrl);
        }

        const maxAllowed = recapNormalMaxSeconds();
        let isOverLimit = (data.duration_seconds && data.duration_seconds > maxAllowed);
        let splitAutoEnabled = false;
        if (isOverLimit && state.user?.is_admin) {
            isOverLimit = false; // admins are exempt from the cap
        } else if (isOverLimit && state.user?.is_vip) {
            // Split path: the server segments the source into
            // <=10-min parts and processes each part.
            enableRecapSplitForLongVideo();
            splitAutoEnabled = true;
            isOverLimit = false;
        }
        const btnStartRecap = document.getElementById("btn-start-recap");
        const durSpan = document.getElementById("recap-info-dur");
        const esc = escapeHtml;

        if (durSpan) {
            if (isOverLimit) {
                durSpan.className = "mini-tag text-danger";
                const tierLabel = state.user?.is_vip ? "VIP" : t("tier_free_label");
                durSpan.innerHTML = `⏱️ ${esc(data.estimated_duration)} <span style="color:#ef4444; font-weight:700; margin-left:4px;">(${esc(tf("recap_over_limit_label", { tier: tierLabel }))})</span>`;
                if (btnStartRecap) {
                    btnStartRecap.disabled = true;
                    btnStartRecap.style.opacity = "0.5";
                }
                showStudioToast(t("toast_duration_over"), 'warning');
            } else {
                durSpan.className = "mini-tag";
                durSpan.innerHTML = `⏱️ ${esc(data.estimated_duration)} <span class="recap-ready-badge" style="color:#10b981; font-weight:700; margin-left:4px;">(${esc(t("recap_upload_ready_badge"))})</span>`;
                if (btnStartRecap && !recapJobIsActive()) {
                    btnStartRecap.disabled = false;
                    btnStartRecap.style.opacity = "";
                }
                if (splitAutoEnabled) notifyRecapSplitAutoEnabled();
            }
        }

        const els = recapUploadEls();
        if (els.bar) { els.bar.classList.remove("busy", "indeterminate"); els.bar.style.width = "100%"; }
        // Batch DF: a no-audio file can never be recapped — say so
        // NOW, on the card, and lock the CTA (see recapNoAudio).
        if (state.recapNoAudio) {
            if (durSpan) {
                durSpan.className = "mini-tag text-danger";
                durSpan.innerHTML = `⏱️ ${esc(data.estimated_duration)} <span style="color:#ef4444; font-weight:700; margin-left:4px;">(${esc(t("recap_no_audio_tag"))})</span>`;
            }
            setRecapStartEnabled(false);
            showStudioToast(t("recap_no_audio"), 'warning');
        }
        setRecapInfoUploadState("ready");
        if (els.label) els.label.innerText = t("upload_ready_label");
        setRecapPreviewState("ready");
        // Batch AK: a fresh source re-composites the preview.
        scheduleRecapCompositeRefresh();
        if (els.cancelBtn) els.cancelBtn.classList.add("hidden");
        if (els.hint) els.hint.classList.add("hidden");
        setTimeout(() => { if (els.wrap) els.wrap.classList.add("hidden"); }, 1500);

        haptic("success");
    }

    if (dropZone) {
        dropZone.addEventListener("dragover", (e) => {
            e.preventDefault();
            dropZone.classList.add("dragover");
        });
        dropZone.addEventListener("dragleave", () => {
            dropZone.classList.remove("dragover");
        });
        dropZone.addEventListener("drop", (e) => {
            e.preventDefault();
            dropZone.classList.remove("dragover");
            if (e.dataTransfer && e.dataTransfer.files.length > 0) {
                handleVideoFileSelect(e.dataTransfer.files[0]);
            }
        });
    }

    if (fileInput) {
        fileInput.addEventListener("change", (e) => {
            if (e.target.files && e.target.files.length > 0) {
                const picked = e.target.files[0];
                // Reset the input so re-picking the SAME file after a
                // failure fires "change" again (several WebViews swallow
                // a same-file selection otherwise — retry was impossible).
                e.target.value = "";
                handleVideoFileSelect(picked);
            }
        });
    }

    const uploadCancelBtn = document.getElementById("recap-upload-cancel");
    if (uploadCancelBtn) {
        uploadCancelBtn.addEventListener("click", () => {
            if (recapUploadXhr) { try { recapUploadXhr.abort(); } catch (e) { /* noop */ } }
        });
    }
    const uploadRetryBtn = document.getElementById("recap-upload-retry");
    if (uploadRetryBtn) {
        uploadRetryBtn.addEventListener("click", () => {
            // Re-send the already-picked file — no re-picking needed.
            if (state.recapUploadFile) startRecapUpload(state.recapUploadFile);
        });
    }

    // ── 0.5. Timeline Frame Scrubber & Second-Skipping (Like in the Bot) ──
    const sliderSeek = document.getElementById("slider-canvas-seek");
    const seekBadge = document.getElementById("canvas-seek-time-badge");
    const btnPrev5 = document.getElementById("btn-seek-prev-5");
    const btnNext5 = document.getElementById("btn-seek-next-5");
    const btnPlayToggle = document.getElementById("btn-canvas-play-toggle");
    const seekPills = document.querySelectorAll(".seek-pill-btn");

    function formatSecToMMSS(sec) {
        const s = Math.max(0, Math.floor(sec || 0));
        const m = Math.floor(s / 60);
        const remS = s % 60;
        return `${m < 10 ? '0' : ''}${m}:${remS < 10 ? '0' : ''}${remS}`;
    }

    function updateSeekTimeDisplay(currentSec, totalSec) {
        if (!seekBadge) return;
        const curStr = formatSecToMMSS(currentSec);
        // state.recapSourceDuration is only ever set from REAL metadata
        // (verified extract, upload probe, or the <video> element). When
        // it is 0 the source's length is unknown — show "--:--" instead
        // of the old fabricated 60s default total (RS-4).
        const knownTotal = state.recapSourceDuration > 0 ? (totalSec || state.recapSourceDuration) : 0;
        const totStr = knownTotal > 0 ? formatSecToMMSS(knownTotal) : "--:--";
        seekBadge.innerText = `⏱️ ${curStr}s / ${totStr}s`;
        seekPills.forEach(pill => {
            const pSec = parseFloat(pill.getAttribute("data-sec") || 0);
            pill.disabled = knownTotal > 0 && pSec > knownTotal;
        });
    }

    function seekCanvasFrame(targetSec) {
        const total = state.recapSourceDuration || (canvasVideo?.duration) || 60;
        const clamped = Math.max(0, Math.min(total, targetSec));
        if (sliderSeek) sliderSeek.value = clamped;
        updateSeekTimeDisplay(clamped, total);
        // Batch AK: the composited preview follows the seek (its
        // payload reads the seek slider position).
        scheduleRecapCompositeRefresh();

        if (canvasVideo && canvasVideo.src && !canvasVideo.classList.contains("hidden")) {
            canvasVideo.currentTime = clamped;
            if (canvasVideo.paused) {
                const playPromise = canvasVideo.play();
                if (playPromise !== undefined) {
                    playPromise.then(() => {
                        canvasVideo.pause();
                    }).catch(() => {});
                }
            }
        } else {
            // R40 item 9: no local video (link source) — ask the
            // server for the REAL frame at this position instead of
            // leaving the static thumbnail on screen.
            scheduleRecapFrameRefresh(clamped);
        }

        seekPills.forEach(pill => {
            const pSec = parseFloat(pill.getAttribute("data-sec"));
            pill.classList.toggle("active", Math.abs(pSec - clamped) < 1.0);
        });
    }

    let recapFrameTimer = null;
    let recapFrameSeq = 0;
    function scheduleRecapFrameRefresh(sec) {
        if (!state.recapVerifiedSourceUrl || state.uploadedVideoPath) return;
        if (recapFrameTimer) clearTimeout(recapFrameTimer);
        recapFrameTimer = setTimeout(() => refreshRecapFramePreview(sec), 450);
    }
    window.scheduleRecapFrameRefresh = scheduleRecapFrameRefresh;

    async function refreshRecapFramePreview(sec) {
        const url = state.recapVerifiedSourceUrl;
        if (!url || state.uploadedVideoPath) return;
        const mySeq = ++recapFrameSeq;
        try {
            const res = await fetch("/api/recap/preview-frame", {
                method: "POST",
                headers: getAuthHeaders(),
                body: JSON.stringify({ url, t: Math.max(0.5, sec || 5) }),
            });
            if (!res.ok) return;
            const blob = await res.blob();
            if (mySeq !== recapFrameSeq) return; // a newer seek won
            const objUrl = URL.createObjectURL(blob);
            if (state.recapFrameUrl) URL.revokeObjectURL(state.recapFrameUrl);
            state.recapFrameUrl = objUrl;
            updateCanvasMockupVisuals();
        } catch (e) { /* keep the existing canvas visuals */ }
    }
    window.refreshRecapFramePreview = refreshRecapFramePreview;

    // ── Batch AK: engine-composited preview ─────────────────────
    // The canvas's picture is the ENGINE's own composite (POST
    // /api/recap/preview-composite) for the CURRENT settings — all
    // seven subtitle styles with engine plate sizing, the title
    // badge, and (once a script exists) the real first cue as the
    // sample. The CSS boxes over it are placement handles only;
    // the hand-mirrored JS renderer no longer draws subtitles.
    let recapCompositeSeq = 0;
    let recapCompositeTimer = null;
    let recapCompositeHash = "";
    function recapCompositeSourceUrl() {
        if (state.uploadedVideoPath) return null;
        if (state.recapVerifiedSourceUrl) return state.recapVerifiedSourceUrl;
        const m = ((document.getElementById("recap-url-input")?.value || "").trim()).match(/https?:\/\/[^\s]+/i);
        return m ? m[0] : null;
    }
    function recapCompositePayload() {
        const g = (id) => document.getElementById(id);
        const iv = (el, dflt) => el ? parseInt(el.value) : dflt;
        const seek = g("slider-canvas-seek");
        // N3: the real first cue once the script editor has one.
        let sample = "";
        const firstSeg = document.querySelector("#recap-script-list .script-seg-text");
        if (firstSeg && firstSeg.value) sample = firstSeg.value.trim().slice(0, 120);
        return {
            url: recapCompositeSourceUrl(),
            uploaded_path: state.uploadedVideoPath || null,
            t: seek ? (parseFloat(seek.value) || 5) : 5,
            ratio: state.recapRatio || "9:16",
            ratio_fill: state.recapRatioFill || "blur",
            crop_top: state.recapCropTop || 0,
            crop_bottom: state.recapCropBottom || 0,
            crop_left: state.recapCropLeft || 0,
            crop_right: state.recapCropRight || 0,
            mirror_video: state.recapMirror !== false,
            zoom_factor: state.recapZoom || 1.0,
            blur_subtitles: state.recapBlurSubs !== false,
            blur_y: iv(g("slider-blur-y"), 85),
            blur_h: iv(g("slider-blur-h"), 7),
            blur_x: iv(g("slider-blur-x"), 4),
            blur_w: iv(g("slider-blur-w"), 92),
            blur_box_explicit: !!state.recapBlurBoxTouched,
            blur_strength: iv(g("slider-blur-strength"), 18),
            blur_auto_detect: state.recapBlurAutoDetect !== false,
            burn_subtitles: state.recapBurnSubs !== false,
            sub_bottom_margin: iv(g("slider-sub-y"), state.recapSubMargin != null ? state.recapSubMargin : 5),
            sub_x: iv(g("slider-sub-x"), 50),
            sub_w: iv(g("slider-sub-w"), 92),
            sub_lines: state.recapLines || 1,
            sub_font_size: state.recapFontSize || 16,
            sub_style: state.recapSubStyle || "yellow",
            sub_font: state.recapSubFont || "padauk",
            sample_text: sample || null,
            // Batch DD: protection, free style controls, custom
            // font and the blur-box list — the composite consumes
            // the same normalised fields as the submit payload.
            ...ddConfigExtras(),
            include_logo: state.recapLogo !== false,
            logo_pos: state.recapLogoPos || "top-right",
            logo_scale: Number(state.recapLogoScale || 0.16),
            logo_opacity: Number(state.recapLogoOpacity || 0.70),
        };
    }
    // Batch DD: with a blur-box list present, the legacy
    // single-box flag must not ride along (the list supersedes it).
    function ddCompositePayload() {
        const p = recapCompositePayload();
        if (Array.isArray(p.blur_boxes) && p.blur_boxes.length) p.blur_box_explicit = false;
        // Batch DD: position band — Top/Centre ride the centre-%
        // sub_y (and suppress the bottom margin); Bottom is the
        // margin path, exactly as before.
        if (state.recapSubPosBand === "top") { p.sub_y = 15; p.sub_bottom_margin = null; }
        else if (state.recapSubPosBand === "center") { p.sub_y = 50; p.sub_bottom_margin = null; }
        return p;
    }
    function scheduleRecapCompositeRefresh() {
        if (!recapCompositeSourceUrl() && !state.uploadedVideoPath) return;
        if (recapCompositeTimer) clearTimeout(recapCompositeTimer);
        recapCompositeTimer = setTimeout(refreshRecapCompositePreview, 600);
    }
    window.scheduleRecapCompositeRefresh = scheduleRecapCompositeRefresh;
    async function refreshRecapCompositePreview() {
        if (!recapCompositeSourceUrl() && !state.uploadedVideoPath) return;
        const payload = ddCompositePayload();
        const hash = JSON.stringify(payload);
        if (hash === recapCompositeHash) return; // nothing changed
        const mySeq = ++recapCompositeSeq;
        // Batch AM (M3 residual): until the FIRST composite lands,
        // the canvas would otherwise sit on its bare backdrop with
        // no hint that a render is in flight — show the explicit
        // preparing state for exactly that window. Later refreshes
        // keep the stale frame (Batch AK), so no indicator then.
        const showLoading = !state.recapCompositeUrl;
        if (showLoading) setRecapPreviewLoading(true);
        try {
            const res = await fetch("/api/recap/preview-composite", {
                method: "POST",
                headers: getAuthHeaders(),
                body: hash,
            });
            if (!res.ok) {
                if (showLoading && mySeq === recapCompositeSeq) setRecapPreviewLoading(false);
                return;
            }
            const blob = await res.blob();
            if (mySeq !== recapCompositeSeq) return; // a newer refresh won
            recapCompositeHash = hash;
            const objUrl = URL.createObjectURL(blob);
            if (state.recapCompositeUrl) URL.revokeObjectURL(state.recapCompositeUrl);
            state.recapCompositeUrl = objUrl;
            updateCanvasMockupVisuals();
            // AX-4R: if Step 4 is on screen waiting for this frame,
            // swap it in now (the old frame stayed up meanwhile).
            if (typeof isRecapStepOpen === "function" && isRecapStepOpen(4)
                && typeof window.refreshRecapStep4Thumb === "function") {
                window.refreshRecapStep4Thumb();
            }
            if (showLoading) setRecapPreviewLoading(false);
        } catch (e) { /* keep the existing canvas visuals */
            if (showLoading && mySeq === recapCompositeSeq) setRecapPreviewLoading(false);
        }
    }
    // Batch AM (M3 residual): the canvas's explicit preparing state,
    // shown only while the first engine composite is in flight.
    function setRecapPreviewLoading(on) {
        const el = document.getElementById("canvas-preview-loading");
        if (el) el.classList.toggle("hidden", !on);
    }
    window.refreshRecapCompositePreview = refreshRecapCompositePreview;

    if (sliderSeek) {
        sliderSeek.addEventListener("input", (e) => {
            seekCanvasFrame(parseFloat(e.target.value));
        });
    }

    if (btnPrev5) {
        btnPrev5.addEventListener("click", () => {
            haptic("selection");
            const cur = sliderSeek ? parseFloat(sliderSeek.value) : 5;
            seekCanvasFrame(cur - 5);
        });
    }

    if (btnNext5) {
        btnNext5.addEventListener("click", () => {
            haptic("selection");
            const cur = sliderSeek ? parseFloat(sliderSeek.value) : 5;
            seekCanvasFrame(cur + 5);
        });
    }

    if (btnPlayToggle && canvasVideo) {
        btnPlayToggle.addEventListener("click", () => {
            haptic("selection");
            if (canvasVideo.src && !canvasVideo.classList.contains("hidden")) {
                if (canvasVideo.paused) {
                    canvasVideo.play().then(() => {
                        btnPlayToggle.innerText = `⏸️ ${t("pause_audio")}`;
                        btnPlayToggle.classList.add("active");
                    }).catch(err => {
                        console.warn("Play error:", err);
                    });
                } else {
                    canvasVideo.pause();
                    btnPlayToggle.innerText = `⏯️ ${t("play_audio")}`;
                    btnPlayToggle.classList.remove("active");
                }
            } else {
                showStudioToast(t("toast_scrub_hint"), 'info');
            }
        });

        canvasVideo.addEventListener("timeupdate", () => {
            if (!canvasVideo.paused && sliderSeek) {
                sliderSeek.value = canvasVideo.currentTime;
                updateSeekTimeDisplay(canvasVideo.currentTime, canvasVideo.duration);
            }
        });
        canvasVideo.addEventListener("ended", () => {
            btnPlayToggle.innerText = `⏯️ ${t("play_audio")}`;
            btnPlayToggle.classList.remove("active");
        });
    }

    seekPills.forEach(pill => {
        pill.addEventListener("click", () => {
            haptic("selection");
            const pSec = parseFloat(pill.getAttribute("data-sec") || 0);
            seekCanvasFrame(pSec);
        });
    });

    // Batch P: ONE geometry for preview and render (recapter
    // standard) — mirrors core.frame_layout + subtitle_centre_px in
    // core.py. The picture rect is computed from the same inputs the
    // render uses (source aspect, chosen ratio, crop, fill), and
    // sub_y is interpreted picture-relatively on letterboxed
    // layouts, exactly as the render burns it.
    function recapPictureRect() {
        let rCanvas = 9 / 16;
        if (state.recapRatio === "16:9") rCanvas = 16 / 9;
        else if (state.recapRatio === "1:1") rCanvas = 1.0;
        let rSource = null;
        if (state.recapSourceWidth && state.recapSourceHeight && state.recapSourceHeight > 0) {
            rSource = state.recapSourceWidth / state.recapSourceHeight;
        } else if (canvasVideo && canvasVideo.videoWidth && canvasVideo.videoHeight) {
            rSource = canvasVideo.videoWidth / canvasVideo.videoHeight;
        }
        if (state.recapRatio === "Original" && rSource) rCanvas = rSource;
        if (!rSource) {
            // Batch AK: no opposite-orientation guess — with the
            // source aspect unknown, treat the picture as the whole
            // canvas; the composited frame corrects it on arrival.
            rSource = rCanvas;
        }
        const ct = Math.max(0, Math.min(35, state.recapCropTop || 0)) / 100.0;
        const cb = Math.max(0, Math.min(35, state.recapCropBottom || 0)) / 100.0;
        const cl = Math.max(0, Math.min(35, state.recapCropLeft || 0)) / 100.0;
        const cr = Math.max(0, Math.min(35, state.recapCropRight || 0)) / 100.0;
        const keepW = Math.max(0.10, 1.0 - cl - cr);
        const keepH = Math.max(0.10, 1.0 - ct - cb);
        const hasCrop = (ct > 0.001 || cb > 0.001 || cl > 0.001 || cr > 0.001);
        const rCropped = rSource * (keepW / keepH);
        const fillMode = state.recapRatioFill || "blur";
        if (fillMode === "crop" || (!hasCrop && Math.abs(rSource - rCanvas) < 0.01)) {
            return { ox: 0, oy: 0, pw: 1, ph: 1 };
        }
        let pw = 1, ph = 1;
        if (rCropped >= rCanvas) { ph = rCanvas / rCropped; }
        else { pw = rCropped / rCanvas; }
        return { ox: (1 - pw) / 2, oy: (1 - ph) / 2, pw, ph };
    }

    // Subtitle centre as a fraction of the canvas height — the same
    // anchor the render burns (subtitle_centre_px in core.py):
    // oy + sub_y × ph on letterboxed layouts, sub_y full-frame.
    function subtitleAnchorCanvasFrac(subYPercent) {
        const r = recapPictureRect();
        const sy = Math.max(0, Math.min(100, subYPercent)) / 100.0;
        if ((r.oy + r.ph) < 1 - 0.002) return r.oy + sy * r.ph;
        return sy;
    }

    // S3: plate height fraction matching core._plate_height_px with font sized
    // off the shorter picture side min(pw, ph) relative to picture height.
    function subtitlePlateHeightFracOfPicture() {
        const PH = 608;
        const r = recapPictureRect();
        const cw = (canvas ? canvas.clientWidth : 0) || 270;
        const ch = (canvas ? canvas.clientHeight : 0) || 480;
        const picH = Math.max(120, r.ph * ch);
        const basis = Math.max(60, Math.min(r.pw * cw, r.ph * ch));
        const scaledBasis = PH * (basis / picH);
        const fp = Math.max(18, scaledBasis * subtitleFontFraction(state.recapFontSize || 16, state.recapLines || 1));
        const nLines = Math.max(1, state.recapLines || 1);
        const textH = fp * (nLines === 1 ? 1.15 : (nLines === 2 ? 2.30 : 3.65));
        const pad = Math.max(10, fp * 0.40);
        return (textH + 2 * pad) / PH;
    }
    // The subtitle centre-% (picture basis) implied by a bottom
    // margin — the value the canvas drag math works in. The slider
    // under the historical slider-sub-y id IS the margin.
    function subCentrePercentFromMargin(marginPct) {
        return 100 - marginPct - subtitlePlateHeightFracOfPicture() * 50;
    }
    function subMarginFromCentrePercent(centrePct) {
        return Math.round(Math.min(95, Math.max(0, 100 - centrePct - subtitlePlateHeightFracOfPicture() * 50)));
    }
    function currentSubMarginPercent() {
        return sliderSubY ? parseFloat(sliderSubY.value) : (state.recapSubMargin != null ? state.recapSubMargin : 5);
    }

    // Batch AG: subtitle centre X as a fraction of the canvas width
    // — the mirror of subtitle_centre_x_px in core.py:
    // ox + sub_x × pw on pillarboxed layouts, sub_x full-frame.
    function subtitleCentreXCanvasFrac(subXPercent) {
        const r = recapPictureRect();
        const sx = Math.max(0, Math.min(100, subXPercent)) / 100.0;
        if (r.pw < 1 - 0.002) return r.ox + sx * r.pw;
        return sx;
    }

    // Batch AG: subtitle wrap width (sub_w, percent of the PICTURE
    // width) as a fraction of the canvas width.
    function subtitleWidthCanvasFrac(subWPercent) {
        const r = recapPictureRect();
        const sw = Math.max(0, Math.min(100, subWPercent)) / 100.0;
        return sw * r.pw;
    }

    // Batch AA/AG: the blur box's canvas geometry from its
    // PICTURE-relative percents — the exact mirror of
    // core.user_blur_box_to_canvas_box (centre = oy + y × ph,
    // left = ox + x × pw), so the box the user drags is the box
    // the render blurs. x/w default to the legacy centred box
    // (4% / 92%) when not supplied.
    function blurBoxCanvasGeom(yPercent, hPercent, xPercent, wPercent) {
        const r = recapPictureRect();
        const y = Math.max(0, Math.min(100, parseFloat(yPercent) || 0)) / 100.0;
        const h = Math.max(0, Math.min(100, parseFloat(hPercent) || 0)) / 100.0;
        const x = (xPercent == null ? 4 : Math.max(0, Math.min(100, parseFloat(xPercent) || 0))) / 100.0;
        const w = (wPercent == null ? 92 : Math.max(0, Math.min(100, parseFloat(wPercent) || 0))) / 100.0;
        return {
            centre: r.oy + y * r.ph,
            height: h * r.ph,
            left: r.ox + x * r.pw,
            width: w * r.pw,
        };
    }

    // History re-run prefill does NOT go through here (it restores a
    // job's own settings on purpose), and a page reload restoring
    // the same session's snapshot is untouched: this is called only
    // from the new-source success hooks (Analyze success for a
    // different URL, upload success for a new file).
    function resetBlurBoxForNewSource() {
        state.recapBlurY = 85;
        state.recapBlurH = 7;
        // Batch AK: the subtitle position resets to the shipped
        // default — now the 5% bottom margin (syncSubBoxVisual
        // re-derives the centre mirror from it).
        state.recapSubMargin = 5;
        // Batch AG: the horizontal blur geometry is per-source like
        // Y/H and resets with it; sub_x follows sub_y's reset rule.
        // sub_w does NOT reset — like the font size, it is a style
        // choice that carries across sources.
        state.recapBlurX = 4;
        state.recapBlurW = 92;
        state.recapSubX = 50;
        state.recapBlurBoxTouched = false;
        if (sliderBlurY) sliderBlurY.value = 85;
        if (sliderBlurH) sliderBlurH.value = 7;
        if (sliderSubY) sliderSubY.value = 5;
        if (sliderBlurX) sliderBlurX.value = 4;
        if (sliderBlurW) sliderBlurW.value = 92;
        if (sliderSubX) sliderSubX.value = 50;
        syncBlurBoxVisual();
        syncSubBoxVisual();
        saveRecapSettings();
    }

    // Picker size → fraction of the shorter picture side min(pw, ph);
    // mirrors SUBTITLE_FONT_HEIGHT_FRACTIONS in core.py (×1.25 two-line).
    function subtitleFontFraction(sizePt, lines) {
        const pts = [[13, 4.0], [16, 5.0], [19, 6.0], [22, 7.0]];
        const v = parseFloat(sizePt) || 16;
        let f;
        if (v <= pts[0][0]) {
            f = pts[0][1] * v / pts[0][0];
        } else {
            f = null;
            for (let i = 1; i < pts.length; i++) {
                if (v <= pts[i][0]) {
                    f = pts[i - 1][1] + (pts[i][1] - pts[i - 1][1]) * (v - pts[i - 1][0]) / (pts[i][0] - pts[i - 1][0]);
                    break;
                }
            }
            if (f === null) {
                const a = pts[pts.length - 2], b = pts[pts.length - 1];
                f = b[1] + (b[1] - a[1]) * (v - b[0]) / (b[0] - a[0]);
            }
        }
        if ((parseInt(lines) || 1) !== 1) f *= 1.25;
        return f / 100.0;
    }

    function subtitlePreviewFontPx() {
        const canvasW = canvas ? canvas.clientWidth : 0;
        const canvasH = canvas ? canvas.clientHeight : 0;
        const frac = subtitleFontFraction(state.recapFontSize || 16, state.recapLines || 1);
        // S3: the fraction applies to min(pw * canvasW, ph * canvasH),
        // mirroring core.subtitle_font_px across all aspect ratios.
        const r = recapPictureRect();
        const basis = Math.min(r.pw * canvasW, r.ph * canvasH);
        return basis > 0 ? Math.max(8, Math.round(frac * basis)) : 13;
    }

    // 1. Slider Controls for Original Blur and Subtitle Y (100% synced with backend FFmpeg coordinates)
    function syncBlurBoxVisual() {
        if (!previewBlur) return;
        const y = sliderBlurY ? parseFloat(sliderBlurY.value) : 85;
        const h = sliderBlurH ? parseFloat(sliderBlurH.value) : 7;
        const x = sliderBlurX ? parseFloat(sliderBlurX.value) : 4;
        const w = sliderBlurW ? parseFloat(sliderBlurW.value) : 92;
        if (valBlurY) valBlurY.innerText = `${Math.round(y)}%`;
        if (valBlurH) valBlurH.innerText = `${Math.round(h)}%`;
        if (valBlurX) valBlurX.innerText = `${Math.round(x)}%`;
        if (valBlurW) valBlurW.innerText = `${Math.round(w)}%`;
        // Batch AA: the box is painted PICTURE-relative (Batch P
        // machinery): with translateY(-50%) the centre lands at
        // oy + blur_y × ph of the canvas and the height is
        // blur_h × ph — the same conversion the render applies
        // (core.user_blur_box_to_canvas_box), so the drawn box is
        // the rendered box.
        // Batch AG: left/width are painted EXPLICITLY from the same
        // conversion (ox + blur_x × pw, blur_w × pw) instead of the
        // CSS pin (left/right 4%) the box used to rely on.
        const geom = blurBoxCanvasGeom(y, h, x, w);
        previewBlur.style.top = `${(geom.centre * 100).toFixed(3)}%`;
        previewBlur.style.height = `${(geom.height * 100).toFixed(3)}%`;
        previewBlur.style.left = `${(geom.left * 100).toFixed(3)}%`;
        previewBlur.style.width = `${(geom.width * 100).toFixed(3)}%`;
        previewBlur.style.right = "auto";
        previewBlur.style.bottom = "auto";
        previewBlur.classList.toggle("is-near-top", (geom.centre - geom.height * 0.5) < 0.14);
        // Batch AK: the blur geometry feeds the composited frame —
        // a blur change re-renders it (debounced + hash-guarded).
        scheduleRecapCompositeRefresh();
        // Batch DD: mirror the geometry into the selected list box.
        if (typeof ddAfterBlurSync === "function") ddAfterBlurSync();
    }

    // Batch AK: fitBlurPlateToCaption and its plate element are
    // GONE — the engine-composited frame carries the real plate at
    // the engine's own size, so there is no CSS plate left to fit.

    function syncSubBoxVisual() {
        if (!previewSub) return;
        // Batch AK: the slider is the BOTTOM-EDGE margin. The drag
        // handle (outline-only over the composited frame) is placed
        // by the margin-derived CENTRE through the shared anchor,
        // with the plate's real height — so the handle outlines the
        // plate the composite shows.
        const margin = currentSubMarginPercent();
        const centre = subCentrePercentFromMargin(margin);
        const x = sliderSubX ? parseFloat(sliderSubX.value) : 50;
        const w = sliderSubW ? parseFloat(sliderSubW.value) : 92;
        if (valSubY) valSubY.innerText = `${Math.round(margin)}%`;
        if (valSubX) valSubX.innerText = `${Math.round(x)}%`;
        if (valSubW) valSubW.innerText = `${Math.round(w)}%`;
        state.recapSubMargin = Math.round(margin);
        state.recapSubY = Math.round(centre);
        previewSub.style.top = `${(subtitleAnchorCanvasFrac(centre) * 100).toFixed(3)}%`;
        previewSub.style.height = `${(subtitlePlateHeightFracOfPicture() * recapPictureRect().ph * 100).toFixed(3)}%`;
        // Batch AG: the handle is centred on sub_x (the shared
        // centre-X resolver) with its width set from sub_w, exactly
        // as the render places and wraps the caption.
        previewSub.style.left = `${(subtitleCentreXCanvasFrac(x) * 100).toFixed(3)}%`;
        previewSub.style.width = `${(subtitleWidthCanvasFrac(w) * 100).toFixed(3)}%`;
        previewSub.style.right = "auto";
        previewSub.style.transform = "translate(-50%, -50%)";
        previewSub.style.bottom = "auto";
        previewSub.classList.toggle("is-near-top", subtitleAnchorCanvasFrac(centre) < 0.18);
        syncBlurBoxVisual();
        scheduleRecapCompositeRefresh();
    }

    if (sliderBlurY && previewBlur && valBlurY) {
        sliderBlurY.addEventListener("input", () => {
            // Batch AA: a deliberate slider set marks the box
            // explicit — it will rule the render's source band.
            state.recapBlurBoxTouched = true;
            syncBlurBoxVisual();
        });
    }

    if (sliderBlurH && previewBlur && valBlurH) {
        sliderBlurH.addEventListener("input", () => {
            state.recapBlurBoxTouched = true;
            syncBlurBoxVisual();
        });
    }

    // Batch AG: the horizontal blur sliders are deliberate sets too
    // (same explicit marker as Y/H). X and W clamp against each
    // other so the box always stays inside the picture: widening W
    // past the right edge pulls X back, and X stops at 100 − W.
    if (sliderBlurX && previewBlur && valBlurX) {
        sliderBlurX.addEventListener("input", () => {
            state.recapBlurBoxTouched = true;
            const w = sliderBlurW ? parseFloat(sliderBlurW.value) : 92;
            const maxX = Math.max(0, 100 - w);
            if (parseFloat(sliderBlurX.value) > maxX) sliderBlurX.value = maxX;
            syncBlurBoxVisual();
        });
    }

    if (sliderBlurW && previewBlur && valBlurW) {
        sliderBlurW.addEventListener("input", () => {
            state.recapBlurBoxTouched = true;
            const w = parseFloat(sliderBlurW.value);
            if (sliderBlurX && parseFloat(sliderBlurX.value) + w > 100) {
                sliderBlurX.value = Math.max(0, 100 - w);
            }
            syncBlurBoxVisual();
        });
    }

    // Batch O: blur strength slider — the value rides the submit
    // payload into the engine's boxblur radius; the canvas mockup's
    // backdrop blur scales with it so the preview hints at the
    // strength the render will use (default 18 ≈ the shipped 6px).
    function syncBlurStrengthVisual() {
        const v = sliderBlurStrength ? parseInt(sliderBlurStrength.value) : 18;
        if (valBlurStrength) valBlurStrength.textContent = String(Number.isFinite(v) ? v : 18);
        // Batch CA: this used to scale a CSS backdrop frosting on
        // the blur box (an inline backdrop filter) — retired with
        // the frosting itself, which washed the composited subtitle
        // under the box. Strength is payload geometry: the settled
        // engine-composited frame carries the true blur, so a
        // strength change schedules it like every other geometry
        // control (this slider previously never refreshed the
        // composite at all — the frosting was its only feedback).
        scheduleRecapCompositeRefresh();
    }
    if (sliderBlurStrength) {
        sliderBlurStrength.addEventListener("input", syncBlurStrengthVisual);
        syncBlurStrengthVisual();
    }

    if (sliderSubY && previewSub && valSubY) {
        sliderSubY.addEventListener("input", () => {
            // Batch DD: touching the margin slider returns the
            // position band to Bottom (the margin's own band).
            if (state.recapSubPosBand !== "bottom") {
                state.recapSubPosBand = "bottom";
                if (typeof ddSyncPosBandPills === "function") ddSyncPosBandPills();
            }
            syncSubBoxVisual();
        });
    }

    if (sliderSubX && previewSub && valSubX) {
        sliderSubX.addEventListener("input", () => {
            syncSubBoxVisual();
        });
    }

    if (sliderSubW && previewSub && valSubW) {
        sliderSubW.addEventListener("input", () => {
            syncSubBoxVisual();
            if (typeof updateSubtitleCharsEstimator === "function") updateSubtitleCharsEstimator();
        });
    }

    // ── Interactive Drag & Drop / Manual Move on Canvas ──────────────────
    function enableVerticalDrag(boxElement, onUpdateY, getInitialY, getDeltaScale, getClampRange) {
        if (!boxElement || !canvas) return;

        let isDragging = false;
        let startClientY = 0;
        let startPercent = 85;

        function startDrag(clientY) {
            isDragging = true;
            startClientY = clientY;
            startPercent = getInitialY ? getInitialY() : 85;
            boxElement.classList.add("is-dragging");
            haptic("light");
        }

        function moveDrag(clientY) {
            if (!isDragging) return;
            const canvasRect = canvas.getBoundingClientRect();
            if (canvasRect.height <= 0) return;

            // Calculate percentage delta from initial touch/click to prevent sudden jumping
            const deltaPx = clientY - startClientY;
            const deltaPercent = (deltaPx / canvasRect.height) * 100;
            const deltaScale = getDeltaScale ? (getDeltaScale() || 1) : 1;
            // Batch AH: the clamp range is a parameter now — the
            // subtitle/blur default stays [10, 95] (their slider
            // range); the title box passes [0, 100].
            const range = getClampRange ? getClampRange() : [10, 95];
            const clampedY = Math.max(range[0], Math.min(range[1], Math.round(startPercent + deltaPercent * deltaScale)));
            onUpdateY(clampedY);
        }

        function endDrag() {
            if (!isDragging) return;
            isDragging = false;
            boxElement.classList.remove("is-dragging");
        }

        // Desktop Mouse Drag
        boxElement.addEventListener("mousedown", (e) => {
            e.preventDefault();
            e.stopPropagation();
            startDrag(e.clientY);

            const onMouseMove = (ev) => {
                ev.preventDefault();
                moveDrag(ev.clientY);
            };
            const onMouseUp = () => {
                endDrag();
                window.removeEventListener("mousemove", onMouseMove);
                window.removeEventListener("mouseup", onMouseUp);
            };
            window.addEventListener("mousemove", onMouseMove);
            window.addEventListener("mouseup", onMouseUp);
        });

        // Mobile Touch Drag — R40 item 5: DELIBERATE grabs only. The
        // drag used to start on ANY touch landing on the (very wide)
        // box: a page scroll that happened to begin on the subtitle
        // silently dragged sub_y with it, and the change was then
        // persisted by the settings snapshot — the exact mechanism
        // behind a real user's sub_y=67 mystery. Now a touch drag can
        // only start on the box's ⠿ grip handle; touches elsewhere on
        // the box body fall through to normal page scrolling (the CSS
        // gives the box touch-action: pan-y and the grip none).
        const grip = boxElement.querySelector(".drag-grip-icon");
        const touchTarget = grip || boxElement;
        touchTarget.addEventListener("touchstart", (e) => {
            if (e.touches && e.touches.length > 0) {
                e.stopPropagation();
                startDrag(e.touches[0].clientY);
            }
        }, { passive: true });

        boxElement.addEventListener("touchmove", (e) => {
            if (isDragging && e.touches && e.touches.length > 0) {
                e.preventDefault();
                e.stopPropagation();
                moveDrag(e.touches[0].clientY);
            }
        }, { passive: false });

        boxElement.addEventListener("touchend", endDrag);
        boxElement.addEventListener("touchcancel", endDrag);
    }

    // Batch AG: horizontal twin of enableVerticalDrag — same
    // deliberate-grab rules (mouse anywhere on the box, touch only
    // via the ⠿ grip), with the delta measured against the canvas
    // WIDTH and scaled by 1/pw on pillarboxed layouts so the box
    // follows the finger 1:1, mirroring the vertical 1/ph rule.
    // Attached alongside the vertical drag, a diagonal drag moves
    // both axes at once. getClampRange returns [min, max] percents
    // for the axis value (blur_x is a left edge clamped by the
    // width; sub_x is a centre clamped to [10, 90]).
    function enableHorizontalDrag(boxElement, onUpdateX, getInitialX, getDeltaScaleX, getClampRange) {
        if (!boxElement || !canvas) return;

        let isDragging = false;
        let startClientX = 0;
        let startPercent = 50;

        function startDrag(clientX) {
            isDragging = true;
            startClientX = clientX;
            startPercent = getInitialX ? getInitialX() : 50;
            boxElement.classList.add("is-dragging");
            haptic("light");
        }

        function moveDrag(clientX) {
            if (!isDragging) return;
            const canvasRect = canvas.getBoundingClientRect();
            if (canvasRect.width <= 0) return;
            const deltaPx = clientX - startClientX;
            const deltaPercent = (deltaPx / canvasRect.width) * 100;
            const deltaScale = getDeltaScaleX ? (getDeltaScaleX() || 1) : 1;
            const range = getClampRange ? getClampRange() : [0, 100];
            const clampedX = Math.max(range[0], Math.min(range[1], Math.round(startPercent + deltaPercent * deltaScale)));
            onUpdateX(clampedX);
        }

        function endDrag() {
            if (!isDragging) return;
            isDragging = false;
            boxElement.classList.remove("is-dragging");
        }

        boxElement.addEventListener("mousedown", (e) => {
            e.preventDefault();
            e.stopPropagation();
            startDrag(e.clientX);

            const onMouseMove = (ev) => {
                ev.preventDefault();
                moveDrag(ev.clientX);
            };
            const onMouseUp = () => {
                endDrag();
                window.removeEventListener("mousemove", onMouseMove);
                window.removeEventListener("mouseup", onMouseUp);
            };
            window.addEventListener("mousemove", onMouseMove);
            window.addEventListener("mouseup", onMouseUp);
        });

        const grip = boxElement.querySelector(".drag-grip-icon");
        const touchTarget = grip || boxElement;
        touchTarget.addEventListener("touchstart", (e) => {
            if (e.touches && e.touches.length > 0) {
                e.stopPropagation();
                startDrag(e.touches[0].clientX);
            }
        }, { passive: true });

        boxElement.addEventListener("touchmove", (e) => {
            if (isDragging && e.touches && e.touches.length > 0) {
                e.preventDefault();
                e.stopPropagation();
                moveDrag(e.touches[0].clientX);
            }
        }, { passive: false });

        boxElement.addEventListener("touchend", endDrag);
        boxElement.addEventListener("touchcancel", endDrag);
    }

    // Attach dragging to Blur Box
    enableVerticalDrag(previewBlur, (y) => {
        // Batch AA: dragging the box is a deliberate set.
        state.recapBlurBoxTouched = true;
        if (sliderBlurY) sliderBlurY.value = y;
        syncBlurBoxVisual();
    }, () => sliderBlurY ? parseFloat(sliderBlurY.value) : 85, () => {
        // Batch AA: mirror Batch P's subtitle drag — on letterboxed
        // layouts the box's canvas position moves ph× slower than
        // blur_y (picture-relative anchor), so scale the drag delta
        // by 1/ph and the box follows the finger 1:1.
        const r = recapPictureRect();
        return ((r.oy + r.ph) < 1 - 0.002 && r.ph > 0) ? 1 / r.ph : 1;
    });

    // Attach dragging to Subtitle Box
    // Batch AK: the slider is the bottom MARGIN now — dragging the
    // box DOWN must DECREASE the margin, so the delta scale is
    // negative (the box still follows the finger 1:1 through the
    // margin-derived centre); the clamp is the margin range.
    enableVerticalDrag(previewSub, (y) => {
        if (sliderSubY) sliderSubY.value = y;
        syncSubBoxVisual();
    }, () => sliderSubY ? parseFloat(sliderSubY.value) : 5, () => {
        // Batch P: on letterboxed layouts the caption's canvas
        // position moves ph× slower than the picture-% value
        // (picture-relative anchor) — scale the drag delta so the
        // box follows the finger 1:1 instead of lagging behind it.
        const r = recapPictureRect();
        return -(((r.oy + r.ph) < 1 - 0.002 && r.ph > 0) ? 1 / r.ph : 1);
    }, () => [0, 60]);

    // Batch AG: horizontal drag for both boxes (2-axis with the
    // vertical drags above). The blur box's left edge clamps
    // against its width; the subtitle centre clamps to [10, 90] —
    // the same ranges the API enforces.
    enableHorizontalDrag(previewBlur, (x) => {
        // Dragging the box sideways is a deliberate set.
        state.recapBlurBoxTouched = true;
        if (sliderBlurX) sliderBlurX.value = x;
        syncBlurBoxVisual();
    }, () => sliderBlurX ? parseFloat(sliderBlurX.value) : 4, () => {
        const r = recapPictureRect();
        return (r.pw < 1 - 0.002 && r.pw > 0) ? 1 / r.pw : 1;
    }, () => [0, Math.max(0, 100 - (sliderBlurW ? parseFloat(sliderBlurW.value) : 92))]);

    enableHorizontalDrag(previewSub, (x) => {
        if (sliderSubX) sliderSubX.value = x;
        syncSubBoxVisual();
    }, () => sliderSubX ? parseFloat(sliderSubX.value) : 50, () => {
        const r = recapPictureRect();
        return (r.pw < 1 - 0.002 && r.pw > 0) ? 1 / r.pw : 1;
    }, () => [10, 90]);

    // ── Resize Handles for Blur Box Height ──
    const blurResizeTop = previewBlur ? previewBlur.querySelector(".box-resize-handle.top") : null;
    const blurResizeBottom = previewBlur ? previewBlur.querySelector(".box-resize-handle.bottom") : null;

    function attachBlurResize(handle, isTop) {
        if (!handle) return;
        let startY = 0;
        let startH = 7;
        let isResizing = false;

        function startResize(y) {
            isResizing = true;
            startY = y;
            startH = sliderBlurH ? parseFloat(sliderBlurH.value) : 7;
            // F1: resizing counts as interacting with the box — its
            // label paints for the duration (same class as dragging).
            if (previewBlur) previewBlur.classList.add("is-dragging");
            haptic("light");
        }
        function moveResize(y) {
            if (!isResizing) return;
            const canvasRect = canvas ? canvas.getBoundingClientRect() : null;
            const canvasH = (canvasRect && canvasRect.height > 0) ? canvasRect.height : 360;
            // Dragging top handle up (y < startY) expands height; dragging down shrinks height
            // Dragging bottom handle down (y > startY) expands height; dragging up shrinks height
            const deltaPx = isTop ? (startY - y) : (y - startY);
            const deltaPct = (deltaPx / canvasH) * 100;
            // Batch AA: blur_h is a % of the PICTURE height, so a
            // canvas-% finger delta converts by 1/ph on letterboxed
            // layouts (same conversion as the box drag above).
            const r = recapPictureRect();
            const deltaScale = ((r.oy + r.ph) < 1 - 0.002 && r.ph > 0) ? 1 / r.ph : 1;
            const newH = Math.max(3, Math.min(22, Math.round(startH + deltaPct * deltaScale)));
            // Batch AA: resizing is a deliberate set.
            state.recapBlurBoxTouched = true;
            if (sliderBlurH) sliderBlurH.value = newH;
            syncBlurBoxVisual();
        }
        function endResize() {
            isResizing = false;
            if (previewBlur) previewBlur.classList.remove("is-dragging");
        }

        handle.addEventListener("mousedown", (e) => {
            e.stopPropagation();
            startResize(e.clientY);
            const mm = (ev) => { ev.preventDefault(); moveResize(ev.clientY); };
            const mu = () => { endResize(); window.removeEventListener("mousemove", mm); window.removeEventListener("mouseup", mu); };
            window.addEventListener("mousemove", mm);
            window.addEventListener("mouseup", mu);
        });

        handle.addEventListener("touchstart", (e) => {
            if (e.touches && e.touches.length > 0) {
                e.stopPropagation();
                startResize(e.touches[0].clientY);
            }
        }, { passive: true });

        handle.addEventListener("touchmove", (e) => {
            if (isResizing && e.touches && e.touches.length > 0) {
                e.preventDefault();
                e.stopPropagation();
                moveResize(e.touches[0].clientY);
            }
        }, { passive: false });

        handle.addEventListener("touchend", endResize);
        handle.addEventListener("touchcancel", endResize);
    }
    attachBlurResize(blurResizeTop, true);
    attachBlurResize(blurResizeBottom, false);

    // ── Resize Handles for Blur Box Width (Batch AG) ──
    const blurResizeLeft = previewBlur ? previewBlur.querySelector(".box-resize-handle.left") : null;
    const blurResizeRight = previewBlur ? previewBlur.querySelector(".box-resize-handle.right") : null;

    function attachBlurResizeX(handle, isLeft) {
        if (!handle) return;
        let startX = 0;
        let startLeft = 4;
        let startW = 92;
        let isResizing = false;

        function startResize(x) {
            isResizing = true;
            startX = x;
            startLeft = sliderBlurX ? parseFloat(sliderBlurX.value) : 4;
            startW = sliderBlurW ? parseFloat(sliderBlurW.value) : 92;
            if (previewBlur) previewBlur.classList.add("is-dragging");
            haptic("light");
        }
        function moveResize(x) {
            if (!isResizing) return;
            const canvasRect = canvas ? canvas.getBoundingClientRect() : null;
            const canvasW = (canvasRect && canvasRect.width > 0) ? canvasRect.width : 360;
            const deltaPct = ((x - startX) / canvasW) * 100;
            // blur_x / blur_w are % of the PICTURE width, so a
            // canvas-% finger delta converts by 1/pw on pillarboxed
            // layouts (the horizontal twin of the 1/ph rule above).
            const r = recapPictureRect();
            const deltaScale = (r.pw < 1 - 0.002 && r.pw > 0) ? 1 / r.pw : 1;
            const delta = deltaPct * deltaScale;
            // Resizing is a deliberate set.
            state.recapBlurBoxTouched = true;
            if (isLeft) {
                // The right edge stays put: X and W move together.
                const newLeft = Math.max(0, Math.min(startLeft + startW - 10, Math.round(startLeft + delta)));
                if (sliderBlurX) sliderBlurX.value = newLeft;
                if (sliderBlurW) sliderBlurW.value = startW + (startLeft - newLeft);
            } else {
                const newW = Math.max(10, Math.min(100 - startLeft, Math.round(startW + delta)));
                if (sliderBlurW) sliderBlurW.value = newW;
            }
            syncBlurBoxVisual();
        }
        function endResize() {
            isResizing = false;
            if (previewBlur) previewBlur.classList.remove("is-dragging");
        }

        handle.addEventListener("mousedown", (e) => {
            e.stopPropagation();
            startResize(e.clientX);
            const mm = (ev) => { ev.preventDefault(); moveResize(ev.clientX); };
            const mu = () => { endResize(); window.removeEventListener("mousemove", mm); window.removeEventListener("mouseup", mu); };
            window.addEventListener("mousemove", mm);
            window.addEventListener("mouseup", mu);
        });

        handle.addEventListener("touchstart", (e) => {
            if (e.touches && e.touches.length > 0) {
                e.stopPropagation();
                startResize(e.touches[0].clientX);
            }
        }, { passive: true });

        handle.addEventListener("touchmove", (e) => {
            if (isResizing && e.touches && e.touches.length > 0) {
                e.preventDefault();
                e.stopPropagation();
                moveResize(e.touches[0].clientX);
            }
        }, { passive: false });

        handle.addEventListener("touchend", endResize);
        handle.addEventListener("touchcancel", endResize);
    }
    attachBlurResizeX(blurResizeLeft, true);
    attachBlurResizeX(blurResizeRight, false);
    syncBlurBoxVisual();
    syncSubBoxVisual();

    // Batch AV: the compact-toolbar tab binding was retired with the
    // tab nav itself — the seek panel is always visible, and the
    // mask/style controls live in the fine expandable. The F1
    // blur-label behaviour moved to toggleRecapFineSettings: the
    // label paints while the detail panel (which holds the blur
    // group) is open.

    // ── Helper: Real-time Live Canvas Mockup Visual Updater ──
    // Exposed on window for the top-level callers (Batch AV retired
    // the accordion caller; the exposure stays for the others).
    window.updateCanvasMockupVisuals = updateCanvasMockupVisuals;

    // Batch AS (AS-2): ONE stage-shape authority. The preview stage
    // adopts the SELECTED output ratio — 9:16 portrait, 16:9 wide,
    // 1:1 square — and for "Original" it adopts the SOURCE's own
    // ratio: the probed source dimensions when known, otherwise the
    // composited frame's natural aspect (for Original the engine
    // canvas IS the source shape, so the composite is ground truth).
    // Before AS, "Original" depended on a bucket re-derivation in
    // step 4 that never ran when the source dimensions were missing,
    // so the stage stayed at the 9:16 default while the picture
    // inside it changed — the stage lied about the output.
    function recapStageAspect() {
        if (state.recapRatio === "16:9") return { cls: "ratio-16-9", css: "16 / 9" };
        if (state.recapRatio === "1:1") return { cls: "ratio-1-1", css: "1 / 1" };
        if (state.recapRatio === "Original") {
            let w = state.recapSourceWidth, h = state.recapSourceHeight;
            if (!(w && h)) {
                const ci = document.getElementById("canvas-composite-img");
                if (ci && state.recapCompositeUrl && ci.naturalWidth > 0 && ci.naturalHeight > 0) {
                    w = ci.naturalWidth; h = ci.naturalHeight;
                }
            }
            if (w && h) {
                const r = w / h;
                const cls = r > 1.3 ? "ratio-16-9" : (r >= 0.8 ? "ratio-1-1" : "ratio-9-16");
                return { cls, css: `${w} / ${h}` };
            }
            // Source shape not known yet: keep the neutral default;
            // the composite's load handler re-runs the visuals and
            // this resolves to the real shape as soon as it lands.
            return { cls: "ratio-9-16", css: "" };
        }
        return { cls: "ratio-9-16", css: "9 / 16" };
    }

    function updateCanvasMockupVisuals() {
        if (!canvas) return;

        const zoom = state.recapZoom || 1.0;

        // 1. Aspect Ratio Frame Class — Batch AS (AS-2): the stage
        // shape comes from the single authority above, for every
        // selection, so class and inline aspect can never disagree.
        if (mockupFrame) {
            const stage = recapStageAspect();
            mockupFrame.className = `mockup-screen ${stage.cls}`;
            mockupFrame.style.aspectRatio = stage.css;
            const stickyWrap = document.getElementById("recap-step2-sticky-preview");
            if (stickyWrap) {
                const ratioVal = state.recapRatio || "9:16";
                const parts = String(stage.css || "9 / 16").split("/");
                const ratioNum = (parseFloat(parts[0]) || 9) / (parseFloat(parts[1]) || 16);
                stickyWrap.classList.toggle("is-ratio-9-16", ratioVal === "9:16");
                stickyWrap.classList.toggle("is-ratio-16-9", ratioVal === "16:9");
                stickyWrap.classList.toggle("is-ratio-1-1", ratioVal === "1:1");
                stickyWrap.classList.toggle("is-ratio-orig", String(ratioVal).toLowerCase().startsWith("orig"));
                stickyWrap.classList.toggle("is-portrait-stage", ratioNum < 0.85 || stage.cls === "ratio-9-16");
            }
            const topNav = document.querySelector(".top-nav");
            if (topNav && document.documentElement) {
                const navH = Math.ceil(topNav.getBoundingClientRect().height || 0);
                if (navH > 40) {
                    document.documentElement.style.setProperty("--vcp-hdr-sticky-top", `${navH + 14}px`);
                }
            }
        }

        // 2. Canvas Base Classes & Fill Mode
        const fillMode = state.recapRatioFill || "blur";
        canvas.className = `video-bg-placeholder fill-${fillMode} ${state.recapMirror ? 'mirrored' : ''}`;

        // 3. Keep thumbnail on Canvas & Inner Movie
        const innerMovie = document.getElementById("mockup-inner-movie");
        const fillBadge = document.getElementById("mockup-fill-badge");
        const origSub = document.getElementById("mockup-orig-sub");
        const hasActiveMedia = Boolean(state.recapThumbnailUrl || state.recapFrameUrl || state.uploadedVideoPath || (canvasVideo && !canvasVideo.classList.contains("hidden")));
        
        // Hide mock English subtitles if user has loaded a video/thumbnail
        if (origSub) {
            origSub.style.display = hasActiveMedia ? "none" : "";
        }

        const cTop = Math.max(0, Math.min(35, state.recapCropTop || 0));
        const cBottom = Math.max(0, Math.min(35, state.recapCropBottom || 0));
        const cLeft = Math.max(0, Math.min(35, state.recapCropLeft || 0));
        const cRight = Math.max(0, Math.min(35, state.recapCropRight || 0));

        const ct = cTop / 100.0;
        const cb = cBottom / 100.0;
        const cl = cLeft / 100.0;
        const cr = cRight / 100.0;
        const keepW = Math.max(0.10, 1.0 - cl - cr);
        const keepH = Math.max(0.10, 1.0 - ct - cb);

        // Hide crop-shade overlays because innerMovie + media positioning already applies the exact FFmpeg crop
        const shadeTop = document.getElementById("crop-shade-top");
        const shadeBottom = document.getElementById("crop-shade-bottom");
        const shadeLeft = document.getElementById("crop-shade-left");
        const shadeRight = document.getElementById("crop-shade-right");
        if (shadeTop) shadeTop.style.height = "0%";
        if (shadeBottom) shadeBottom.style.height = "0%";
        if (shadeLeft) shadeLeft.style.width = "0%";
        if (shadeRight) shadeRight.style.width = "0%";

        // 4. Calculate Dynamic Source vs Canvas Aspect Ratio Fit (matching FFmpeg frame_layout + _visual_filters)
        if (innerMovie) {
            // Determine Canvas Aspect Ratio
            let rCanvas = 9 / 16; // default 0.5625
            if (state.recapRatio === "16:9") rCanvas = 16 / 9;
            else if (state.recapRatio === "1:1") rCanvas = 1.0;

            // Determine Source Media Aspect Ratio
            let rSource = null;
            if (state.recapSourceWidth && state.recapSourceHeight && state.recapSourceHeight > 0) {
                rSource = state.recapSourceWidth / state.recapSourceHeight;
            } else if (canvasVideo && canvasVideo.videoWidth && canvasVideo.videoHeight) {
                rSource = canvasVideo.videoWidth / canvasVideo.videoHeight;
            }

            // If user selected "Original", canvas dynamically matches the source aspect ratio
            if (state.recapRatio === "Original" && rSource) {
                rCanvas = rSource;
                // Batch AS (AS-2): the stage's own shape is set once
                // in step 1 via recapStageAspect() — it is no longer
                // re-derived here, so the frame and the fit math can
                // never claim different shapes.
                // No source dimensions known: rCanvas keeps its neutral
                // 9/16 default rather than asserting a landscape canvas
                // we cannot know — the unverified state is the info
                // card's job.
            }

            // Fallback source ratio if media not yet analyzed
            // (Batch AK: no opposite-orientation guess — the canvas
            // ratio stands until the real aspect is known).
            if (!rSource) {
                rSource = rCanvas;
            }

            // Fit aspect for the inset: when neither the extract nor a
            // <video> reported the source's real dimensions but the
            // preview thumbnail has loaded, fit to the picture actually
            // on screen (its natural aspect). The fallback guess above
            // assumes the opposite orientation of the canvas, which
            // squeezed 16:9 into a narrow strip flanked by disjoint
            // backdrop slices. Presentation only — reported state
            // dimensions still win, and the "Original" frame shape is
            // still driven by rSource alone.
            let rFit = rSource;
            if (!state.recapSourceWidth && !(canvasVideo && canvasVideo.videoWidth)) {
                const loadedThumb = document.getElementById("canvas-thumb-img");
                if (loadedThumb && loadedThumb.naturalWidth > 0 && loadedThumb.naturalHeight > 0 &&
                    state.recapThumbnailUrl && loadedThumb.getAttribute("src") === state.recapThumbnailUrl) {
                    rFit = loadedThumb.naturalWidth / loadedThumb.naturalHeight;
                }
            }

            // Cropped source aspect ratio matching FFmpeg's crop=c_sw:c_sh:c_x:c_y
            const rCropped = rFit * (keepW / keepH);
            const hasCrop = (ct > 0.001 || cb > 0.001 || cl > 0.001 || cr > 0.001);
            const ratioDiff = Math.abs(rSource - rCanvas);

            let cropCoverW = 100.0 * zoom;
            let cropCoverH = 100.0 * zoom;

            if (fillMode === "crop" || (!hasCrop && ratioDiff < 0.01)) {
                innerMovie.style.width = "100%";
                innerMovie.style.height = "100%";
                innerMovie.style.top = "0%";
                innerMovie.style.left = "0%";
                innerMovie.style.boxShadow = "none";
                if (rCropped >= rCanvas) {
                    cropCoverH = 100.0 * zoom;
                    cropCoverW = 100.0 * zoom * (rCropped / rCanvas);
                } else {
                    cropCoverW = 100.0 * zoom;
                    cropCoverH = 100.0 * zoom * (rCanvas / rCropped);
                }
            } else if (rCropped >= rCanvas) {
                // Cropped picture is wider than canvas (letterbox bars on top & bottom).
                // The strip MUST be vertically centered: .mockup-inner-movie is
                // inset:0, so without an explicit top it glues to the top edge
                // and leaves one giant void below (owner screenshot 2026-10-30).
                const effectiveHeight = Math.max(12, Math.min(100, (rCanvas / rCropped) * 100.0));
                innerMovie.style.width = "100%";
                innerMovie.style.height = `${effectiveHeight.toFixed(3)}%`;
                innerMovie.style.top = `${((100 - effectiveHeight) / 2).toFixed(3)}%`;
                innerMovie.style.left = "0%";
                innerMovie.style.boxShadow = "0 0 35px rgba(0, 0, 0, 0.9), 0 0 15px rgba(245, 158, 11, 0.25)";
            } else {
                // Cropped picture is narrower/taller than canvas (pillarbox bars on left & right)
                const effectiveWidth = Math.max(12, Math.min(100, (rCropped / rCanvas) * 100.0));
                innerMovie.style.height = "100%";
                innerMovie.style.width = `${effectiveWidth.toFixed(3)}%`;
                innerMovie.style.left = `${((100 - effectiveWidth) / 2).toFixed(3)}%`;
                innerMovie.style.top = "0%";
                innerMovie.style.boxShadow = "0 0 35px rgba(0, 0, 0, 0.9), 0 0 15px rgba(245, 158, 11, 0.25)";
            }

            // Exact asymmetric crop + center zoom coordinates inside innerMovie
            const vidW = cropCoverW / keepW;
            const vidH = cropCoverH / keepH;
            const vidLeft = 50.0 - (cl + 0.5 * keepW) * vidW;
            const vidTop = 50.0 - (ct + 0.5 * keepH) * vidH;

            let thumbImg = document.getElementById("canvas-thumb-img");
            if (!thumbImg && innerMovie) {
                thumbImg = document.createElement("img");
                thumbImg.id = "canvas-thumb-img";
                thumbImg.className = "canvas-video-player hidden";
                thumbImg.alt = "";                // A dead thumbnail URL must hide the element, never show
                // the browser's broken-image glyph inside the canvas.
                thumbImg.onerror = () => {
                    thumbImg.dataset.failed = "1";
                    thumbImg.classList.add("hidden");
                    updateCanvasMockupVisuals();
                };
                thumbImg.onload = () => {
                    delete thumbImg.dataset.failed;
                    // F2: once the picture (and its real aspect) has
                    // loaded, refit the inset — the first paint used
                    // the fallback aspect guess. Keyed on the natural
                    // size so each new image refits exactly once.
                    const aspectKey = `${thumbImg.naturalWidth}x${thumbImg.naturalHeight}`;
                    if (thumbImg.dataset.aspect !== aspectKey) {
                        thumbImg.dataset.aspect = aspectKey;
                        updateCanvasMockupVisuals();
                    }
                };
                innerMovie.insertBefore(thumbImg, innerMovie.firstChild);
            }

            // R40 item 9: the server-grabbed real-frame element,
            // created the same lazy way as the thumbnail image.
            let frameImg = document.getElementById("canvas-frame-img");
            if (!frameImg && innerMovie) {
                frameImg = document.createElement("img");
                frameImg.id = "canvas-frame-img";
                frameImg.className = "canvas-video-player hidden";
                frameImg.alt = "";
                innerMovie.insertBefore(frameImg, innerMovie.firstChild);
            }

            const backdropEl = document.getElementById("video-backdrop-blur");
            const noThumbEl = document.getElementById("canvas-no-thumb");
            const compositeImg = document.getElementById("canvas-composite-img");
            // Batch AM (M4): the raw <video> surface letterboxes flat
            // black by design and is NOT the styled preview — label it
            // as the source original for exactly the branch that shows
            // it; every other surface keeps the label hidden.
            const sourceLabel = document.getElementById("canvas-source-label");
            if (sourceLabel) sourceLabel.classList.add("hidden");
            let mediaVisible = false;
            if (compositeImg && state.recapCompositeUrl) {
                // Batch AK: the engine composite IS the canvas — it
                // already carries the ratio, fill, crop, zoom,
                // mirror, blur, subtitles and title exactly as the
                // render burns them, so it fills the frame plainly
                // (no CSS fit math) and every other media layer
                // hides. The CSS boxes stay on top as drag handles.
                // Batch AS (AS-3a): the composite ALREADY contains
                // the engine's mirror flip — the CSS .mirrored flip
                // (which exists to mirror the RAW layers: video /
                // thumbnail / frame grab) must not flip it a second
                // time. While the double flip stood, the picture
                // silently un-mirrored and every overlay the engine
                // had burned READABLY — the title above all — showed
                // MIRRORED in the preview while the real output was
                // correct. Step 2 rebuilds the class from state on
                // every call, so stripping it here is deterministic:
                // raw layers (other branches) keep the flip.
                canvas.classList.remove("mirrored");
                mediaVisible = true;
                if (noThumbEl) noThumbEl.classList.add("hidden");
                if (backdropEl) backdropEl.classList.remove("on");
                canvas.style.backgroundImage = "";
                innerMovie.style.backgroundImage = "none";
                innerMovie.style.width = "100%";
                innerMovie.style.height = "100%";
                innerMovie.style.top = "0%";
                innerMovie.style.left = "0%";
                innerMovie.style.boxShadow = "none";
                if (thumbImg) thumbImg.classList.add("hidden");
                if (frameImg) frameImg.classList.add("hidden");
                if (canvasVideo) { canvasVideo.classList.add("hidden"); canvasVideo.style.display = "none"; }
                if (compositeImg.getAttribute("src") !== state.recapCompositeUrl) compositeImg.src = state.recapCompositeUrl;
                // Batch AS (AS-2): once the composite has actually
                // loaded, its natural aspect is ground truth for an
                // "Original" stage whose source dimensions are still
                // unknown — re-run the visuals so step 1 can adopt
                // it (and backfill the drag geometry's source dims:
                // for Original the canvas shape IS the source shape).
                compositeImg.onload = () => {
                    if (state.recapRatio === "Original" && !state.recapSourceWidth &&
                        compositeImg.naturalWidth > 0 && compositeImg.naturalHeight > 0) {
                        state.recapSourceWidth = compositeImg.naturalWidth;
                        state.recapSourceHeight = compositeImg.naturalHeight;
                    }
                    updateCanvasMockupVisuals();
                };
                compositeImg.classList.remove("hidden");
                compositeImg.style.display = "block";
                if (fillBadge) fillBadge.style.display = "none";
            } else if (canvasVideo && !canvasVideo.classList.contains("hidden") && canvasVideo.src) {
                mediaVisible = true;
                if (sourceLabel) sourceLabel.classList.remove("hidden");
                if (noThumbEl) noThumbEl.classList.add("hidden");
                innerMovie.style.backgroundImage = "none";
                // Fill the letterbox area with a blurred copy of the source
                // frame, like the real render's blurred fill. Without this
                // the canvas background stays "none" (set in
                // handleVideoFileSelect) and the area renders as a void.
                if (backdropEl) {
                    if (fillMode === "blur" && state.recapThumbnailUrl) {
                        backdropEl.style.backgroundImage = `url('${state.recapThumbnailUrl}')`;
                        backdropEl.classList.add("on");
                    } else {
                        backdropEl.classList.remove("on");
                    }
                }
                if (thumbImg) thumbImg.classList.add("hidden");
                if (frameImg) frameImg.classList.add("hidden");
                if (compositeImg) compositeImg.classList.add("hidden");
                canvasVideo.style.display = "block";
                canvasVideo.style.opacity = "1";
                canvasVideo.style.objectFit = "fill";
                canvasVideo.style.transform = "none";
                canvasVideo.style.width = `${vidW.toFixed(3)}%`;
                canvasVideo.style.height = `${vidH.toFixed(3)}%`;
                canvasVideo.style.left = `${vidLeft.toFixed(3)}%`;
                canvasVideo.style.top = `${vidTop.toFixed(3)}%`;
                if (fillBadge) fillBadge.style.display = "none";
            } else if (state.recapFrameUrl) {
                // R40 item 9: a REAL server-grabbed frame at the seek
                // position wins over the static thumbnail. Same fit
                // geometry + backdrop treatment as the thumbnail
                // branch; the CSS blur/subtitle boxes stay on top, so
                // Mirror/Ratio/Style/Blur edits keep reflecting live.
                mediaVisible = true;
                if (noThumbEl) noThumbEl.classList.add("hidden");
                if (backdropEl) {
                    if (fillMode === "blur") {
                        backdropEl.style.backgroundImage = `url('${state.recapFrameUrl}')`;
                        backdropEl.classList.add("on");
                    } else {
                        backdropEl.style.backgroundImage = "";
                        backdropEl.classList.remove("on");
                    }
                }
                canvas.style.backgroundImage = "";
                canvas.style.backgroundSize = "";
                canvas.style.backgroundPosition = "";
                innerMovie.style.backgroundImage = "none";
                if (thumbImg) thumbImg.classList.add("hidden");
                if (compositeImg) compositeImg.classList.add("hidden");
                if (frameImg) {
                    if (frameImg.getAttribute("src") !== state.recapFrameUrl) frameImg.src = state.recapFrameUrl;
                    frameImg.classList.remove("hidden");
                    frameImg.style.display = "block";
                    frameImg.style.objectFit = "fill";
                    frameImg.style.width = `${vidW.toFixed(3)}%`;
                    frameImg.style.height = `${vidH.toFixed(3)}%`;
                    frameImg.style.left = `${vidLeft.toFixed(3)}%`;
                    frameImg.style.top = `${vidTop.toFixed(3)}%`;
                }
                if (fillBadge) fillBadge.style.display = "none";
            } else if (state.recapThumbnailUrl && !(thumbImg && thumbImg.dataset.failed === "1")) {
                mediaVisible = true;
                if (noThumbEl) noThumbEl.classList.add("hidden");
                // F2/F3: the letterbox backdrop is the SAME blurred
                // layer the video branch uses — one centred inset over
                // a blurred cover backdrop in every ratio (never the
                // sharp canvas-cover slices), and the layer the Mirror
                // flip covers, so backdrop + inset flip together,
                // exactly like the render's hflip-before-split.
                if (backdropEl) {
                    if (fillMode === "blur") {
                        backdropEl.style.backgroundImage = `url('${state.recapThumbnailUrl}')`;
                        backdropEl.classList.add("on");
                    } else {
                        backdropEl.style.backgroundImage = "";
                        backdropEl.classList.remove("on");
                    }
                }
                canvas.style.backgroundImage = "";
                canvas.style.backgroundSize = "";
                canvas.style.backgroundPosition = "";
                innerMovie.style.backgroundImage = "none";
                if (frameImg) frameImg.classList.add("hidden");
                if (compositeImg) compositeImg.classList.add("hidden");
                if (thumbImg) {
                    if (thumbImg.getAttribute("src") !== state.recapThumbnailUrl) delete thumbImg.dataset.failed;
                    thumbImg.src = state.recapThumbnailUrl;
                    thumbImg.classList.remove("hidden");
                    thumbImg.style.display = "block";
                    thumbImg.style.objectFit = "fill";
                    thumbImg.style.width = `${vidW.toFixed(3)}%`;
                    thumbImg.style.height = `${vidH.toFixed(3)}%`;
                    thumbImg.style.left = `${vidLeft.toFixed(3)}%`;
                    thumbImg.style.top = `${vidTop.toFixed(3)}%`;
                }
                if (fillBadge) fillBadge.style.display = "none";
            } else {
                if (backdropEl) backdropEl.classList.remove("on");
                canvas.style.backgroundImage = "";
                innerMovie.style.backgroundImage = "";
                if (thumbImg) thumbImg.classList.add("hidden");
                if (frameImg) frameImg.classList.add("hidden");
                if (compositeImg) compositeImg.classList.add("hidden");
                // No media at all: collapse the letterbox strip geometry
                // (its box-shadow drew seam/edge lines on the empty
                // canvas in every ratio) and show the designed
                // no-thumbnail placeholder over the gradient surface.
                innerMovie.style.width = "100%";
                innerMovie.style.height = "100%";
                innerMovie.style.top = "0%";
                innerMovie.style.left = "0%";
                innerMovie.style.boxShadow = "none";
                if (fillBadge) fillBadge.style.display = "none";
                const noThumb = document.getElementById("canvas-no-thumb");
                if (noThumb) noThumb.classList.remove("hidden");
            }
            // No-media states (empty preview / dead thumbnail): both drag
            // boxes share the default Y=85%, so the blur box's label text
            // stacks onto the subtitle sample text unreadably. Render the
            // blur box label-less there (slim outline only, still
            // draggable). Presentation only — no state values, defaults,
            // ids or handlers change; with real media the label returns.
            const blurBoxEl = document.getElementById("preview-blur-box");
            if (blurBoxEl) blurBoxEl.classList.toggle("no-media", !mediaVisible);
        }
        syncBlurBoxVisual();
        syncSubBoxVisual();
        syncFillPickerState();
        scheduleRecapCompositeRefresh();
    }

    // Batch AK (S4/S5/N5): the fill picker's honesty layer — the
    // picker greys out when the fill cannot change anything (ratio
    // "Original", or the picture already covers the canvas), shows
    // "the whole picture is kept" for Blur/Black, and shows the
    // cropped-away share for Center Crop.
    function syncFillPickerState() {
        const note = document.getElementById("recap-fill-note");
        const picker = document.getElementById("recap-fill-picker");
        if (!note || !picker) return;
        const r = recapPictureRect();
        const fill = state.recapRatioFill || "blur";
        const fullFrame = (r.pw > 0.999 && r.ph > 0.999);
        const disabled = state.recapRatio === "Original" || fullFrame;
        picker.classList.toggle("fill-picker-disabled", disabled);
        picker.querySelectorAll(".pill-btn").forEach(b => { b.disabled = disabled; });
        if (disabled) {
            note.innerText = t("recap_fill_disabled_note");
            note.classList.remove("hidden");
            return;
        }
        if (fill === "crop") {
            let rCanvas = 9 / 16;
            if (state.recapRatio === "16:9") rCanvas = 16 / 9;
            else if (state.recapRatio === "1:1") rCanvas = 1.0;
            let rSource = null;
            if (state.recapSourceWidth && state.recapSourceHeight && state.recapSourceHeight > 0) {
                rSource = state.recapSourceWidth / state.recapSourceHeight;
            }
            if (!rSource) rSource = rCanvas;
            const keepW = Math.max(0.10, 1.0 - (Math.min(35, state.recapCropLeft || 0) + Math.min(35, state.recapCropRight || 0)) / 100.0);
            const keepH = Math.max(0.10, 1.0 - (Math.min(35, state.recapCropTop || 0) + Math.min(35, state.recapCropBottom || 0)) / 100.0);
            const rCropped = rSource * (keepW / keepH);
            const loss = rCropped >= rCanvas ? (1 - rCanvas / rCropped) : (1 - rCropped / rCanvas);
            note.innerText = tf("fill_crop_loss", { pct: Math.max(0, Math.round(loss * 100)) });
        } else {
            note.innerText = t("fill_keep_picture_note");
        }
        note.classList.remove("hidden");
    }

    // Batch AK (S5): in Center Crop mode, dragging the canvas pans
    // the crop window — the left/right (or top/bottom) crop split
    // moves with the drag while each axis's TOTAL crop stays, which
    // is exactly the render's crop-offset freedom. Sliders remain
    // the precise control; this is the direct-manipulation path.
    (function setupCropPanDrag() {
        if (!canvas) return;
        let panning = null;
        canvas.addEventListener("pointerdown", (e) => {
            if ((state.recapRatioFill || "blur") !== "crop") return;
            if (e.target.closest(".draggable-box")) return;
            panning = {
                x: e.clientX, y: e.clientY,
                cl: state.recapCropLeft || 0, cr: state.recapCropRight || 0,
                ct: state.recapCropTop || 0, cb: state.recapCropBottom || 0,
            };
        });
        window.addEventListener("pointermove", (e) => {
            if (!panning) return;
            const rect = canvas.getBoundingClientRect();
            if (!rect.width || !rect.height) return;
            const dxPct = ((e.clientX - panning.x) / rect.width) * 100;
            const dyPct = ((e.clientY - panning.y) / rect.height) * 100;
            let cl = panning.cl, cr = panning.cr, ct = panning.ct, cb = panning.cb;
            const totX = cl + cr, totY = ct + cb;
            if (totX > 0) {
                // Dragging right reveals more of the left side.
                const keepW = Math.max(0.10, 1.0 - totX / 100.0);
                cl = Math.round(Math.max(0, Math.min(totX, panning.cl - dxPct * keepW)));
                cr = totX - cl;
            }
            if (totY > 0) {
                const keepH = Math.max(0.10, 1.0 - totY / 100.0);
                ct = Math.round(Math.max(0, Math.min(totY, panning.ct - dyPct * keepH)));
                cb = totY - ct;
            }
            if (cl !== (state.recapCropLeft || 0) || cr !== (state.recapCropRight || 0)
                || ct !== (state.recapCropTop || 0) || cb !== (state.recapCropBottom || 0)) {
                applyCropValues(ct, cb, cl, cr);
            }
        });
        window.addEventListener("pointerup", () => { panning = null; });
    })();

    // 2. Aspect Ratio Selector (9:16, 16:9, 1:1, Original)
    const ratioBtns = document.querySelectorAll("#recap-ratio-picker .ratio-btn");
    ratioBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            ratioBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            state.recapRatio = btn.getAttribute("data-ratio") || "9:16";
            updateCanvasMockupVisuals();
        });
    });

    // 3. Aspect Ratio Fill Mode (blur, crop, black)
    const fillBtns = document.querySelectorAll("#recap-fill-picker .pill-btn");
    fillBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            fillBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            state.recapRatioFill = btn.getAttribute("data-fill") || "blur";
            updateCanvasMockupVisuals();
        });
    });

    // ── 3.5. Custom Video Crop: Sliders, Presets & Sync Mode ──
    const sliderCropTop = document.getElementById("slider-crop-top");
    const sliderCropBottom = document.getElementById("slider-crop-bottom");
    const sliderCropLeft = document.getElementById("slider-crop-left");
    const sliderCropRight = document.getElementById("slider-crop-right");

    const valCropTop = document.getElementById("val-crop-top");
    const valCropBottom = document.getElementById("val-crop-bottom");
    const valCropLeft = document.getElementById("val-crop-left");
    const valCropRight = document.getElementById("val-crop-right");

    const btnSyncCrop = document.getElementById("btn-sync-crop");

    function setCropPresetActive(presetName) {
        document.querySelectorAll("#recap-crop-presets .pill-btn, #recap-crop-presets-toolbar .pill-btn").forEach(btn => {
            btn.classList.toggle("active", btn.getAttribute("data-crop") === presetName);
        });
    }

    function applyCropValues(top, bottom, left, right, preset = null) {
        state.recapCropTop = top;
        state.recapCropBottom = bottom;
        state.recapCropLeft = left;
        state.recapCropRight = right;

        if (sliderCropTop) sliderCropTop.value = top;
        if (sliderCropBottom) sliderCropBottom.value = bottom;
        if (sliderCropLeft) sliderCropLeft.value = left;
        if (sliderCropRight) sliderCropRight.value = right;

        if (valCropTop) valCropTop.innerText = `${top}%`;
        if (valCropBottom) valCropBottom.innerText = `${bottom}%`;
        if (valCropLeft) valCropLeft.innerText = `${left}%`;
        if (valCropRight) valCropRight.innerText = `${right}%`;

        if (preset) {
            setCropPresetActive(preset);
        } else {
            // Check matches
            if (top === 0 && bottom === 0 && left === 0 && right === 0) setCropPresetActive("full");
            else if (top === 12 && bottom === 12 && left === 0 && right === 0) setCropPresetActive("cinema");
            else if (top === 0 && bottom === 0 && left === 10 && right === 10) setCropPresetActive("side");
            else setCropPresetActive("custom");
        }

        updateCanvasMockupVisuals();
    }

    if (btnSyncCrop) {
        btnSyncCrop.addEventListener("click", () => {
            haptic("selection");
            state.recapSyncCrop = !state.recapSyncCrop;
            btnSyncCrop.classList.toggle("active", state.recapSyncCrop);
            if (state.recapSyncCrop && sliderCropTop) {
                applyCropValues(parseInt(sliderCropTop.value) || 0, parseInt(sliderCropTop.value) || 0, state.recapCropLeft, state.recapCropRight);
            }
        });
    }

    if (sliderCropTop) {
        sliderCropTop.addEventListener("input", (e) => {
            const val = parseInt(e.target.value) || 0;
            const bottomVal = state.recapSyncCrop ? val : state.recapCropBottom;
            applyCropValues(val, bottomVal, state.recapCropLeft, state.recapCropRight);
        });
    }

    if (sliderCropBottom) {
        sliderCropBottom.addEventListener("input", (e) => {
            const val = parseInt(e.target.value) || 0;
            const topVal = state.recapSyncCrop ? val : state.recapCropTop;
            applyCropValues(topVal, val, state.recapCropLeft, state.recapCropRight);
        });
    }

    if (sliderCropLeft) {
        sliderCropLeft.addEventListener("input", (e) => {
            const val = parseInt(e.target.value) || 0;
            applyCropValues(state.recapCropTop, state.recapCropBottom, val, state.recapCropRight);
        });
    }

    if (sliderCropRight) {
        sliderCropRight.addEventListener("input", (e) => {
            const val = parseInt(e.target.value) || 0;
            applyCropValues(state.recapCropTop, state.recapCropBottom, state.recapCropLeft, val);
        });
    }

    document.querySelectorAll("#recap-crop-presets .pill-btn, #recap-crop-presets-toolbar .pill-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            const mode = btn.getAttribute("data-crop");
            if (mode === "full") {
                applyCropValues(0, 0, 0, 0, "full");
            } else if (mode === "cinema") {
                applyCropValues(12, 12, 0, 0, "cinema");
            } else if (mode === "side") {
                applyCropValues(0, 0, 10, 10, "side");
            } else if (mode === "custom") {
                setCropPresetActive("custom");
            }
        });
    });

    // 4. Video Transformation: Mirror Toggle
    const btnMirror = document.getElementById("btn-toggle-mirror");
    if (btnMirror) {
        btnMirror.addEventListener("click", () => {
            haptic("selection");
            state.recapMirror = !state.recapMirror;
            btnMirror.classList.toggle("active", state.recapMirror);
            const statusSpan = btnMirror.querySelector(".toggle-status");
            if (statusSpan) {
                statusSpan.innerText = state.recapMirror ? t("mirror_on") : t("mirror_off");
            }
            updateCanvasMockupVisuals();
            if (canvas) {
                canvas.classList.toggle("mirrored", state.recapMirror);
            }
            refreshRecapStep2Summary();
            if (typeof refreshRecapStep4Facts === "function") refreshRecapStep4Facts();
        });
    }

    // 5. Video Transformation: Micro-Zoom (1.00, 1.06, 1.10)
    const zoomBtns = document.querySelectorAll("#recap-zoom-picker .pill-btn");
    zoomBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            zoomBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            state.recapZoom = parseFloat(btn.getAttribute("data-zoom") || "1.00");
            updateCanvasMockupVisuals();
        });
    });

    // 6. Narration Speed & Pacing (-6%, +0%, +12%, +20%, +25%)
    const speedBtns = document.querySelectorAll("#recap-speed-picker .speed-seg");
    speedBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            speedBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            state.recapSpeed = btn.getAttribute("data-speed") || "+12%";
            moveSpeedPill(document.getElementById("recap-speed-picker"));
            if (typeof refreshRecapStep4Facts === "function") refreshRecapStep4Facts();
        });
    });

    // 6b. Video Splitter: part length pills + toggle shows/hides them
    const splitMinBtns = document.querySelectorAll("#recap-split-minutes-picker .pill-btn");
    splitMinBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            splitMinBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            state.recapSplitMinutes = parseInt(btn.getAttribute("data-split-min") || "3", 10) || 3;
            updateRecapSplitCostHint();
        });
    });
    const splitToggle = document.getElementById("recap-split-toggle");
    if (splitToggle) {
        splitToggle.addEventListener("change", () => {
            const wrap = document.getElementById("recap-split-minutes-wrap");
            if (wrap) wrap.classList.toggle("hidden", !splitToggle.checked);
            updateRecapSplitCostHint();
        });
    }

    // Batch AH: the Batch K twin-toggle sync is gone — there is
    // exactly ONE script-review checkbox (#recap-script-review-toggle,
    // the main-area row with the hint text), and every read site
    // (fingerprint, submit payload, History prefill) reads it
    // directly. No mirroring remains to disagree.

    // 6c. Source language (auto = detect; else a Whisper code).
    // Batch AZ-11: ONE compact dropdown lists all 18 values — the
    // AX-5 frequent pills are retired. The write path is unchanged:
    // ONE state (state.recapSourceLanguage), written only through
    // setRecapSourceLanguage; the sync paints the one control, so a
    // saved value (any of the 18) displays correctly on load.
    const langSel = document.getElementById("recap-lang-more");
    function renderRecapLangSheetGrid() {
        const grid = document.getElementById("recap-lang-sheet-grid");
        if (!grid || !langSel) return;
        const activeCode = state.recapSourceLanguage || "auto";
        grid.innerHTML = "";
        Array.from(langSel.options).forEach(opt => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "vcp-lang-chip" + (opt.value === activeCode ? " active" : "");
            btn.textContent = opt.textContent || opt.value;
            btn.addEventListener("click", () => {
                haptic("selection");
                setRecapSourceLanguage(opt.value);
                closeRecapLangSheet();
            });
            grid.appendChild(btn);
        });
    }
    window.openRecapLangSheet = function() {
        renderRecapLangSheetGrid();
        const backdrop = document.getElementById("recap-lang-sheet-backdrop");
        if (backdrop) backdrop.classList.remove("hidden");
    };
    window.closeRecapLangSheet = function() {
        const backdrop = document.getElementById("recap-lang-sheet-backdrop");
        if (backdrop) backdrop.classList.add("hidden");
    };
    document.addEventListener("keydown", (ev) => {
        if (ev.key === "Escape") {
            const backdrop = document.getElementById("recap-lang-sheet-backdrop");
            if (backdrop && !backdrop.classList.contains("hidden")) window.closeRecapLangSheet();
        }
    });
    function syncRecapLangViews() {
        if (langSel) {
            const has = Array.from(langSel.options).some(o => o.value === state.recapSourceLanguage);
            langSel.value = has ? state.recapSourceLanguage : "auto";
        }
        const backdrop = document.getElementById("recap-lang-sheet-backdrop");
        if (backdrop && !backdrop.classList.contains("hidden")) {
            renderRecapLangSheetGrid();
        }
    }
    window.syncRecapLangViews = syncRecapLangViews;
    function setRecapSourceLanguage(code) {
        state.recapSourceLanguage = code || "auto";
        syncRecapLangViews();
    }
    window.setRecapSourceLanguage = setRecapSourceLanguage;
    if (langSel) {
        langSel.addEventListener("change", () => {
            if (!langSel.value) return;
            haptic("selection");
            setRecapSourceLanguage(langSel.value);
        });
    }

    // 6d. Render quality pills (standard = the historical default encode)
    const qualityBtns = document.querySelectorAll("#recap-quality-picker .pill-btn");
    qualityBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            if (btn.disabled) return;
            haptic("selection");
            qualityBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            state.recapRenderQuality = btn.getAttribute("data-quality") || "standard";
            if (typeof window.refreshRecapStep4Facts === "function") window.refreshRecapStep4Facts();
            // Batch BE: the quote depends on the quality (Max carries
            // the +1 surcharge while tiered pricing is armed).
            updateRecapSplitCostHint();
        });
    });
    refreshRecapQualityLock();

    // 6e. Narration engine pills (VoxCPM persona / Gemini TTS / Edge
    // TTS) + Gemini voice select. Batch AW: Edge keeps the persona
    // area visible (the persona pick supplies the Edge base voice —
    // no separate Edge picker); the voxcpm pill is locked for free
    // profiles by refreshRecapEngineLock and disabled pills never
    // fire (guard below, same as the quality pills).
    const engineBtns = document.querySelectorAll("#recap-engine-picker .pill-btn");
    engineBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            if (btn.disabled) return;
            haptic("selection");
            engineBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            moveSegPill(document.getElementById("recap-engine-picker"));
            state.recapTtsEngine = btn.getAttribute("data-engine") || "voxcpm";
            state.recapInsufMinutesServer = null;
            syncRecapEngineAreas();
            updateRecapSplitCostHint();
        });
    });
    refreshRecapEngineLock();
    initRecapOwnVoice();
    const geminiVoiceSel = document.getElementById("recap-gemini-voice-select");
    if (geminiVoiceSel) {
        geminiVoiceSel.addEventListener("change", () => {
            state.recapGeminiVoice = geminiVoiceSel.value || "";
            if (typeof refreshRecapStep4Choices === "function") refreshRecapStep4Choices();
            // Stop a Gemini preview still playing the previous pick.
            if (currentPreviewButton && currentPreviewButton.id === "btn-preview-gemini-voice") {
                stopCurrentVoicePreview();
            }
        });
    }

    // 7. Narration Story Style Picker (viral, dramatic, funny, pov, standard) — synced across Step 3 & Advanced
    const storyPills = document.querySelectorAll("#story-style-picker-quick .style-select-pill");
    storyPills.forEach(pill => {
        pill.addEventListener("click", () => {
            haptic("selection");
            const chosenStory = pill.getAttribute("data-story") || "viral";
            state.recapStoryStyle = chosenStory;
            storyPills.forEach(p => p.classList.toggle("active", p.getAttribute("data-story") === chosenStory));
            moveSegPill(document.getElementById("story-style-picker-quick"));
        });
    });

    // Reset Layout Helper (Blur Y/H, Sub Y, Crop, Zoom)
    function resetRecapCanvasLayout() {
        haptic("medium");
        if (sliderBlurY) sliderBlurY.value = "85";
        if (sliderBlurH) sliderBlurH.value = "7";
        // Batch AG: the layout reset also returns the horizontal
        // geometry to the centred defaults (sub_w is a style width,
        // not a position — it is NOT reset, like the font size).
        if (sliderBlurX) sliderBlurX.value = "4";
        if (sliderBlurW) sliderBlurW.value = "92";
        if (sliderSubX) sliderSubX.value = "50";
        if (sliderBlurStrength) { sliderBlurStrength.value = "18"; syncBlurStrengthVisual(); }
        // Batch AK: the sub slider resets to the 5% bottom margin.
        if (sliderSubY) sliderSubY.value = "5";
        // Batch AM (M8i): match the per-source reset — a layout
        // reset returns the box to the DEFAULT geometry, so the
        // stale user-touched flag must go too; otherwise the default
        // box still submits as blur_box_explicit and keeps
        // suppressing the engine's source-band detection.
        state.recapBlurBoxTouched = false;
        syncBlurBoxVisual();
        syncSubBoxVisual();

        state.recapCropTop = 0;
        state.recapCropBottom = 0;
        state.recapCropLeft = 0;
        state.recapCropRight = 0;
        const sTop = document.getElementById("slider-crop-top");
        const sBot = document.getElementById("slider-crop-bottom");
        const sLeft = document.getElementById("slider-crop-left");
        const sRight = document.getElementById("slider-crop-right");
        if (sTop) sTop.value = "0";
        if (sBot) sBot.value = "0";
        if (sLeft) sLeft.value = "0";
        if (sRight) sRight.value = "0";
        document.querySelectorAll("#recap-crop-presets .pill-btn, #recap-crop-presets-toolbar .pill-btn").forEach(b => {
            b.classList.toggle("active", b.getAttribute("data-crop") === "full");
        });

        state.recapZoom = 1.0;
        document.querySelectorAll("#recap-zoom-picker .pill-btn").forEach(b => {
            b.classList.toggle("active", b.getAttribute("data-zoom") === "1.00");
        });
        updateCanvasMockupVisuals();
    }
    window.resetRecapCanvasLayout = resetRecapCanvasLayout;

    // 8. Overlay Toggles: Subtitles, Blur, Logo
    const btnToggleSubs = document.getElementById("btn-toggle-subs");
    if (btnToggleSubs) {
        btnToggleSubs.addEventListener("click", () => {
            haptic("selection");
            state.recapBurnSubs = !state.recapBurnSubs;
            btnToggleSubs.classList.toggle("active", state.recapBurnSubs);
            const tVal = btnToggleSubs.querySelector(".t-val");
            if (tVal) tVal.innerText = state.recapBurnSubs ? t("toggle_on") : t("toggle_off");
            if (previewSub) previewSub.style.display = state.recapBurnSubs ? "block" : "none";
            // The band plate tracks the caption (Q1): grow when the
            // caption returns, relax to the slider height when hidden.
            syncBlurBoxVisual();
            scheduleRecapCompositeRefresh();
            refreshRecapStep2Summary();
        });
    }

    const btnToggleBlur = document.getElementById("btn-toggle-blur");
    if (btnToggleBlur) {
        btnToggleBlur.addEventListener("click", () => {
            haptic("selection");
            state.recapBlurSubs = !state.recapBlurSubs;
            btnToggleBlur.classList.toggle("active", state.recapBlurSubs);
            const tVal = btnToggleBlur.querySelector(".t-val");
            if (tVal) tVal.innerText = state.recapBlurSubs ? t("toggle_on") : t("toggle_off");
            if (previewBlur) previewBlur.style.display = state.recapBlurSubs ? "block" : "none";
            // Batch AM (M7): the blur lives in the composited frame
            // now — re-render it, like every other payload-affecting
            // control does, or the picture keeps the old blur until
            // an unrelated change refreshes it.
            scheduleRecapCompositeRefresh();
            // Batch AM (S9 cosmetic): grey the inert sliders while OFF.
            syncBlurSlidersEnabled();
        });
    }

    const btnToggleLogo = document.getElementById("btn-toggle-logo");
    if (btnToggleLogo) {
        btnToggleLogo.addEventListener("click", () => {
            haptic("selection");
            state.recapLogo = !state.recapLogo;
            btnToggleLogo.classList.toggle("active", state.recapLogo);
            const tVal = btnToggleLogo.querySelector(".t-val");
            if (tVal) tVal.innerText = state.recapLogo ? t("toggle_on") : t("toggle_off");
            refreshLogoUi();
            // Batch AM (M7): include_logo rides the composite
            // payload — re-render so the picture follows the toggle
            // immediately (refreshLogoUi only moves the chip).
            scheduleRecapCompositeRefresh();
        });
    }

    // 9. Subtitle Style Presets (Yellow, White, Boxed, Neon, …)
    // Batch AZ-11: the 7 chips are ONE compact dropdown now (a
    // <select> carrying the picker's id). The write path is the
    // same: state.recapSubStyle + a composite refresh.
    const styleSel = document.getElementById("sub-style-picker");
    if (styleSel) {
        styleSel.addEventListener("change", () => {
            haptic("selection");
            state.recapSubStyle = styleSel.value || "yellow";
            // Batch DD: a preset SEEDS the free controls (the user
            // can still tweak them afterwards).
            if (typeof ddSeedStyleControls === "function") ddSeedStyleControls(state.recapSubStyle);
            // Phase 4: keep the 10 visual sample cards in step with
            // the dropdown (both write the SAME state.recapSubStyle).
            if (typeof syncRecapSubStyleCards === "function") syncRecapSubStyleCards();
            // Batch AK: the style renders in the composited frame —
            // refresh it; the drag handle keeps a neutral outline
            // (no per-style CSS classes to keep in sync any more).
            scheduleRecapCompositeRefresh();
        });
    }

    // 10. Subtitle Live Preview Renderer
    function updateSubtitleVisualPreview() {
        // Batch AK: retired — the CSS sample renderer is gone. The
        // subtitle (style, font, size, sample text) renders inside
        // the engine-composited frame; nothing here draws it.
        return;
    }
    // 11. Font Size — Batch DD: ONE full-width slider (10–26pt)
    // with a numeric readout + live cluster capacity indicator.
    function estimateSubtitleCharsPerLine(fontSize, lines, subW) {
        const fs = parseFloat(fontSize) || 16;
        const nLines = Math.max(1, parseInt(lines) || 1);
        let base = 24;
        if (Math.abs(fs - 16) > 0.05) {
            const f16 = subtitleFontFraction(16, 1);
            const fCur = Math.max(0.001, subtitleFontFraction(fs, 1));
            base = Math.round(base * (f16 / fCur));
        }
        if (nLines >= 2) base = Math.round(base / 1.18);
        const swEl = document.getElementById("slider-sub-w");
        const wVal = subW != null ? parseFloat(subW) : (swEl ? parseFloat(swEl.value) : 92);
        const wFrac = Math.max(0.10, Math.min(1.0, (wVal || 92) / 100.0));
        if (Math.abs(wFrac - 0.77) > 0.01) base = Math.round(base * (wFrac / 0.77));
        return Math.max(8, Math.min(46, base));
    }
    function updateSubtitleCharsEstimator() {
        const el = document.getElementById("val-font-chars-est");
        if (!el) return;
        const n = estimateSubtitleCharsPerLine(state.recapFontSize || 16, state.recapLines || 1);
        el.innerText = tf("sub_chars_est", { n });
    }
    window.estimateSubtitleCharsPerLine = estimateSubtitleCharsPerLine;
    window.updateSubtitleCharsEstimator = updateSubtitleCharsEstimator;
    function syncSubtitleFontSize(sizeVal, snapThumb = true) {
        let f = parseFloat(sizeVal);
        if (!Number.isFinite(f)) f = 16;
        for (const s of [13, 16, 19, 22]) {
            if (Math.abs(f - s) <= 0.5) { f = s; break; }
        }
        state.recapFontSize = Math.max(10, Math.min(30, Math.round(f)));
        const fsSlider = document.getElementById("slider-font-size");
        if (fsSlider && snapThumb && parseFloat(fsSlider.value) !== state.recapFontSize) fsSlider.value = state.recapFontSize;
        const fsVal = document.getElementById("val-font-size");
        if (fsVal) fsVal.innerText = `${state.recapFontSize}pt`;
        if (typeof updateSubtitleCharsEstimator === "function") updateSubtitleCharsEstimator();
        updateSubtitleVisualPreview();
        scheduleRecapCompositeRefresh();
        if (typeof refreshRecapStep2Summary === "function") refreshRecapStep2Summary();
    }
    const fontSizeSliderEl = document.getElementById("slider-font-size");
    if (fontSizeSliderEl) {
        fontSizeSliderEl.addEventListener("input", () => syncSubtitleFontSize(fontSizeSliderEl.value, false));
        fontSizeSliderEl.addEventListener("change", () => syncSubtitleFontSize(fontSizeSliderEl.value, true));
        fontSizeSliderEl.addEventListener("keydown", (e) => {
            if (e.key === "ArrowRight" || e.key === "ArrowUp" || e.key === "ArrowLeft" || e.key === "ArrowDown") {
                e.preventDefault();
                const step = (e.key === "ArrowRight" || e.key === "ArrowUp") ? 1 : -1;
                syncSubtitleFontSize((state.recapFontSize || 16) + step, true);
            }
        });
    }

    // 12. Subtitle Lines Picker (Synchronized across toolbar & advanced settings)
    function syncSubtitleLines(linesVal) {
        state.recapLines = parseInt(linesVal || "1");
        document.querySelectorAll("#sub-lines-picker-toolbar .pill-btn").forEach(p => {
            p.classList.toggle("active", parseInt(p.getAttribute("data-lines")) === state.recapLines);
        });
        if (typeof updateSubtitleCharsEstimator === "function") updateSubtitleCharsEstimator();
        updateSubtitleVisualPreview();
        scheduleRecapCompositeRefresh();
        if (typeof window.refreshRecapStep2Summary === "function") window.refreshRecapStep2Summary();
    }
    document.querySelectorAll("#sub-lines-picker-toolbar .pill-btn").forEach(pill => {
        pill.addEventListener("click", () => {
            haptic("selection");
            syncSubtitleLines(pill.getAttribute("data-lines"));
        });
    });

    function syncSubtitleModeUi() {
        const mode = state.recapSubRenderMode || "timeline";
        document.querySelectorAll("#recap-sub-render-mode .pill-btn").forEach(b => {
            b.classList.toggle("active", b.getAttribute("data-sub-mode") === mode);
        });
        const fxRow = document.getElementById("recap-sub-effect-row");
        if (fxRow) fxRow.classList.toggle("hidden", mode === "soft");
        document.querySelectorAll("#recap-sub-effect .pill-btn").forEach(b => {
            b.classList.toggle("active", b.getAttribute("data-sub-effect") === (state.recapSubEffect || "none"));
        });
        const softCb = document.getElementById("toggle-embed-soft-subs");
        if (softCb) softCb.checked = state.recapEmbedSoftSubs !== false;
    }
    document.querySelectorAll("#recap-sub-render-mode .pill-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            state.recapSubRenderMode = btn.getAttribute("data-sub-mode") || "timeline";
            if (state.recapSubRenderMode === "karaoke") state.recapSubEffect = "karaoke";
            else if (state.recapSubRenderMode === "ass" && state.recapSubEffect === "none") state.recapSubEffect = "fade";
            syncSubtitleModeUi();
            scheduleRecapCompositeRefresh();
            if (typeof refreshRecapStep2Summary === "function") refreshRecapStep2Summary();
            if (typeof saveRecapSettingsSoon === "function") saveRecapSettingsSoon();
        });
    });
    document.querySelectorAll("#recap-sub-effect .pill-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            state.recapSubEffect = btn.getAttribute("data-sub-effect") || "none";
            if (state.recapSubEffect === "karaoke" && state.recapSubRenderMode === "timeline") state.recapSubRenderMode = "karaoke";
            syncSubtitleModeUi();
            if (typeof refreshRecapStep2Summary === "function") refreshRecapStep2Summary();
            if (typeof saveRecapSettingsSoon === "function") saveRecapSettingsSoon();
        });
    });
    const embedSoftEl = document.getElementById("toggle-embed-soft-subs");
    if (embedSoftEl) {
        embedSoftEl.addEventListener("change", () => {
            state.recapEmbedSoftSubs = !!embedSoftEl.checked;
            if (typeof saveRecapSettingsSoon === "function") saveRecapSettingsSoon();
        });
    }

    // 12b. Subtitle Font Picker (padauk / noto_sans / pyidaungsu / noto_serif)
    const subFontBtns = document.querySelectorAll("#recap-sub-font-picker .pill-btn");
    subFontBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            subFontBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            state.recapSubFont = btn.getAttribute("data-font") || "padauk";
            updateSubtitleVisualPreview();
            // Batch AS (AS-5): the font renders inside the
            // engine-composited frame, exactly like size/lines —
            // without this refresh the composite kept the PREVIOUS
            // font forever, so the picker looked dead in preview.
            scheduleRecapCompositeRefresh();
        });
    });

    // ── Batch DD wiring (Step-2 rebuild) ─────────────────────────
    // (0) Blur auto-detect re-bind — its original binding sat in
    // the block removed with the title controls.
    const ddAutoDetectEl = document.getElementById("recap-blur-autodetect-toggle");
    if (ddAutoDetectEl) {
        ddAutoDetectEl.addEventListener("change", () => {
            state.recapBlurAutoDetect = !!ddAutoDetectEl.checked;
            scheduleRecapCompositeRefresh();
            refreshRecapStep2Summary();
        });
    }

    // (1) Summary strip + accordion header values.
    function refreshRecapStep2Summary() {
        const styleName = t("sub_style_" + (state.recapSubStyle || "yellow"));
        const prot = t("recap_protection_" + (state.recapProtection || "standard"));
        const strip = document.getElementById("recap-step2-summary");
        const mTag = state.recapMirror !== false ? "🪞 " + t("recap_fact_mirror_on") : "";
        if (strip) {
            strip.innerText = [
                state.recapRatio || "9:16",
                state.recapRatioFill || "blur",
                `${state.recapFontSize || 16}pt`,
                styleName,
                `🛡️ ${prot}`,
                mTag,
            ].filter(Boolean).join(" · ");
        }
        const setVal = (id, txt) => {
            const gEl = document.getElementById(id);
            const v = gEl ? gEl.querySelector(".dd-acc-val") : null;
            if (v) v.innerText = txt;
        };
        setVal("recap-preview-ratio-group", `${state.recapRatio || "9:16"} · ${state.recapRatioFill || "blur"}`);
        setVal("recap-subtitle-group", `${state.recapFontSize || 16}pt · ${styleName} · ${state.recapLines || 1} · ${state.recapSubRenderMode || "timeline"}`);
        const nBoxes = (state.recapBlurBoxes || []).length;
        setVal("recap-blur-group", `${state.recapBlurAutoDetect !== false ? t("toggle_on") : t("toggle_off")} · ×${nBoxes}`);
        setVal("recap-logo-group", `${state.recapLogo !== false ? t("toggle_on") : t("toggle_off")} · ${state.recapLogoPos || "top-right"}`);
        setVal("recap-preview-transform-group", [`🛡️ ${prot}`, `${state.recapZoom || 1}x`, mTag].filter(Boolean).join(" · "));
    }
    window.refreshRecapStep2Summary = refreshRecapStep2Summary;

    // (2) Accordion: every .dd-acc-group gets a header (title +
    // current values + chevron) and a collapsible body; ALL groups
    // start collapsed, subtitle included.
    function setupRecapAccordion() {
        document.querySelectorAll(".dd-acc-group").forEach(group => {
            if (group.dataset.accReady) return;
            group.dataset.accReady = "1";
            const titleKey = group.getAttribute("data-acc-title-key") || "";
            const head = document.createElement("button");
            head.type = "button";
            head.className = "dd-acc-head vcp-acc-head";
            head.setAttribute("aria-expanded", "false");
            const titleSpan = document.createElement("span");
            titleSpan.className = "dd-acc-title";
            if (titleKey) titleSpan.setAttribute("data-i18n", titleKey);
            titleSpan.textContent = titleKey ? t(titleKey) : "";
            const valSpan = document.createElement("span");
            valSpan.className = "dd-acc-val";
            const chev = document.createElement("span");
            chev.className = "dd-acc-chevron";
            chev.textContent = "▸";
            head.appendChild(titleSpan);
            head.appendChild(valSpan);
            head.appendChild(chev);
            const body = document.createElement("div");
            body.className = "dd-acc-body hidden";
            while (group.firstChild) body.appendChild(group.firstChild);
            group.appendChild(head);
            group.appendChild(body);
            group.classList.add("dd-acc");
            body.querySelectorAll(".recap-detail-group-title, .card-label").forEach(el => {
                el.classList.add("dd-acc-orig-title");
            });
            head.addEventListener("click", () => {
                const open = group.classList.toggle("open");
                body.classList.toggle("hidden", !open);
                head.setAttribute("aria-expanded", open ? "true" : "false");
            });
        });
    }

    // (3) Blur-box LIST editor: sliders + the drag box edit the
    // SELECTED box; chips select; add/delete manage the list (≤5).
    // Other boxes paint as outline overlays (click selects).
    // (ddChipsSig / ddExtrasSig are declared at the top of
    // setupRecapVisualEditor — see the note there.)
    function ddBoxes() {
        if (!Array.isArray(state.recapBlurBoxes)) state.recapBlurBoxes = [];
        return state.recapBlurBoxes;
    }
    function ddReadBoxFromSliders() {
        return {
            x: sliderBlurX ? parseInt(sliderBlurX.value) : 4,
            y: sliderBlurY ? parseInt(sliderBlurY.value) : 85,
            w: sliderBlurW ? parseInt(sliderBlurW.value) : 92,
            h: sliderBlurH ? parseInt(sliderBlurH.value) : 7,
            strength: sliderBlurStrength ? parseInt(sliderBlurStrength.value) : 18,
        };
    }
    function ddWriteSelectedBox() {
        const boxes = ddBoxes();
        if (!boxes.length) return;
        const i = Math.max(0, Math.min(boxes.length - 1, state.recapBlurBoxIndex || 0));
        boxes[i] = ddReadBoxFromSliders();
    }
    function ddLoadBoxToSliders(i) {
        const boxes = ddBoxes();
        if (!boxes.length) return;
        state.recapBlurBoxIndex = Math.max(0, Math.min(boxes.length - 1, i));
        const b = boxes[state.recapBlurBoxIndex];
        if (sliderBlurY) sliderBlurY.value = b.y;
        if (sliderBlurH) sliderBlurH.value = b.h;
        if (sliderBlurX) sliderBlurX.value = b.x;
        if (sliderBlurW) sliderBlurW.value = b.w;
        if (sliderBlurStrength) sliderBlurStrength.value = b.strength || 18;
        syncBlurBoxVisual();
        if (typeof syncBlurStrengthVisual === "function") syncBlurStrengthVisual();
        ddRenderBlurChips();
    }
    function ddRenderBlurChips() {
        const boxes = ddBoxes();
        const idx = Math.max(0, Math.min(Math.max(0, boxes.length - 1), state.recapBlurBoxIndex || 0));
        const chipsSig = `${boxes.length}:${idx}`;
        if (chipsSig !== ddChipsSig) {
            ddChipsSig = chipsSig;
            const wrap = document.getElementById("blur-box-chips");
            if (wrap) {
                wrap.innerHTML = "";
                boxes.forEach((b, i) => {
                    const chip = document.createElement("button");
                    chip.type = "button";
                    chip.className = "pill-btn sm" + (i === idx ? " active" : "");
                    chip.textContent = tf("recap_blur_box_n", { n: i + 1 });
                    chip.addEventListener("click", () => ddLoadBoxToSliders(i));
                    wrap.appendChild(chip);
                });
            }
            const addBtn = document.getElementById("btn-blur-box-add");
            if (addBtn) addBtn.classList.toggle("hidden", boxes.length >= 5);
        }
        const extrasSig = JSON.stringify(boxes.filter((b, i) => i !== idx));
        if (extrasSig !== ddExtrasSig && canvas) {
            ddExtrasSig = extrasSig;
            canvas.querySelectorAll(".preview-blur-box-extra").forEach(el => el.remove());
            boxes.forEach((b, i) => {
                if (i === idx) return;
                const g = blurBoxCanvasGeom(b.y, b.h, b.x, b.w);
                const d = document.createElement("div");
                d.className = "preview-blur-box-extra";
                d.style.top = `${(g.centre * 100).toFixed(3)}%`;
                d.style.height = `${(g.height * 100).toFixed(3)}%`;
                d.style.left = `${(g.left * 100).toFixed(3)}%`;
                d.style.width = `${(g.width * 100).toFixed(3)}%`;
                d.addEventListener("click", () => ddLoadBoxToSliders(i));
                canvas.appendChild(d);
            });
        }
    }
    function ddAfterBlurSync() {
        ddWriteSelectedBox();
        ddRenderBlurChips();
        refreshRecapStep2Summary();
    }
    const ddAddBoxBtn = document.getElementById("btn-blur-box-add");
    if (ddAddBoxBtn) ddAddBoxBtn.addEventListener("click", () => {
        const boxes = ddBoxes();
        if (boxes.length >= 5) return;
        haptic("selection");
        if (!boxes.length) boxes.push(ddReadBoxFromSliders());
        else { ddWriteSelectedBox(); boxes.push({ ...boxes[boxes.length - 1] }); }
        state.recapBlurBoxTouched = true;
        ddLoadBoxToSliders(boxes.length - 1);
        scheduleRecapCompositeRefresh();
        if (typeof saveRecapSettingsSoon === "function") saveRecapSettingsSoon();
    });
    const ddDelBoxBtn = document.getElementById("btn-blur-box-del");
    if (ddDelBoxBtn) ddDelBoxBtn.addEventListener("click", () => {
        const boxes = ddBoxes();
        if (!boxes.length) return;
        haptic("selection");
        boxes.splice(Math.max(0, Math.min(boxes.length - 1, state.recapBlurBoxIndex || 0)), 1);
        state.recapBlurBoxIndex = 0;
        if (boxes.length) ddLoadBoxToSliders(0);
        else { ddChipsSig = ""; ddExtrasSig = ""; ddRenderBlurChips(); }
        scheduleRecapCompositeRefresh();
        if (typeof saveRecapSettingsSoon === "function") saveRecapSettingsSoon();
    });

    // (4) Protection level pills.
    document.querySelectorAll("#recap-protection-picker .pill-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            state.recapProtection = btn.getAttribute("data-protection") || "standard";
            document.querySelectorAll("#recap-protection-picker .pill-btn").forEach(b => {
                b.classList.toggle("active", b === btn);
            });
            scheduleRecapCompositeRefresh();
            refreshRecapStep2Summary();
            if (typeof saveRecapSettingsSoon === "function") saveRecapSettingsSoon();
        });
    });

    // (5) Free style controls. A preset SEEDS them (ddSeedStyleControls,
    // called from the style dropdown); the user can still tweak.
    const DD_PRESET_SEED = {
        cinematic: { text: "#ffffff", outline: "#000000", ow: 2.5, bg: null, bgOp: null, shadow: true },
        glass: { text: "#ffffff", outline: "#000000", ow: 1.0, bg: "#000000", bgOp: 58, shadow: false },
        yellow: { text: "#ffd700", outline: "#000000", ow: 2.2, bg: null, bgOp: null },
        white: { text: "#ffffff", outline: "#1a1a1a", ow: 1.8, bg: null, bgOp: null },
        boxed: { text: "#ffffff", outline: "#000000", ow: 1.0, bg: "#000000", bgOp: 35 },
        neon: { text: "#00dbff", outline: "#0066cc", ow: 2.4, bg: null, bgOp: null },
        band: { text: "#ffffff", outline: "#000000", ow: 1.5, bg: "#000000", bgOp: 40 },
        heavy: { text: "#ffd94d", outline: "#000000", ow: 3.0, bg: "#000000", bgOp: 20 },
        soft: { text: "#ffffff", outline: "#222222", ow: 1.2, bg: null, bgOp: null },
        rose: { text: "#ff69b4", outline: "#000000", ow: 1.6, bg: null, bgOp: null },
        orange: { text: "#ff8c00", outline: "#000000", ow: 1.6, bg: null, bgOp: null },
        mint: { text: "#00ff9d", outline: "#151515", ow: 1.8, bg: null, bgOp: null },
    };
    function ddStyleChanged() {
        scheduleRecapCompositeRefresh();
        refreshRecapStep2Summary();
        if (typeof saveRecapSettingsSoon === "function") saveRecapSettingsSoon();
    }
    function ddSeedStyleControls(styleKey) {
        const seed = DD_PRESET_SEED[styleKey];
        if (!seed) return;
        state.recapTextColor = seed.text;
        state.recapOutlineColor = seed.outline;
        state.recapOutlineWidth = seed.ow;
        state.recapBgColor = seed.bg || "";
        state.recapBgOpacity = seed.bgOp;
        const tc = document.getElementById("input-sub-text-color");
        if (tc) tc.value = seed.text;
        const oc = document.getElementById("input-sub-outline-color");
        if (oc) oc.value = seed.outline;
        const bc = document.getElementById("input-sub-bg-color");
        if (bc && seed.bg) bc.value = seed.bg;
        const ow = document.getElementById("slider-outline-width");
        if (ow) ow.value = Math.round(seed.ow * 10);
        const owVal = document.getElementById("val-outline-width");
        if (owVal) owVal.innerText = seed.ow.toFixed(1);
        if (seed.bgOp != null) {
            const bo = document.getElementById("slider-bg-opacity");
            if (bo) bo.value = seed.bgOp;
            const boVal = document.getElementById("val-bg-opacity");
            if (boVal) boVal.innerText = `${seed.bgOp}%`;
        }
        if (typeof seed.shadow === "boolean") {
            state.recapSubShadow = seed.shadow;
            const shBtn = document.getElementById("btn-toggle-sub-shadow");
            if (shBtn) {
                shBtn.classList.toggle("active", seed.shadow);
                const tv = shBtn.querySelector(".t-val");
                if (tv) tv.innerText = seed.shadow ? t("toggle_on") : t("toggle_off");
            }
        }
        ddStyleChanged();
    }
    [["input-sub-text-color", "recapTextColor"], ["input-sub-outline-color", "recapOutlineColor"], ["input-sub-bg-color", "recapBgColor"]].forEach(([id, key]) => {
        const el = document.getElementById(id);
        if (el) el.addEventListener("input", () => { state[key] = el.value; ddStyleChanged(); });
    });
    const ddOwSlider = document.getElementById("slider-outline-width");
    if (ddOwSlider) ddOwSlider.addEventListener("input", () => {
        state.recapOutlineWidth = parseInt(ddOwSlider.value) / 10;
        const v = document.getElementById("val-outline-width");
        if (v) v.innerText = state.recapOutlineWidth.toFixed(1);
        ddStyleChanged();
    });
    const ddBgOpSlider = document.getElementById("slider-bg-opacity");
    if (ddBgOpSlider) ddBgOpSlider.addEventListener("input", () => {
        state.recapBgOpacity = parseInt(ddBgOpSlider.value);
        if (!state.recapBgColor) {
            state.recapBgColor = "#000000";
            const bc = document.getElementById("input-sub-bg-color");
            if (bc) bc.value = "#000000";
        }
        const v = document.getElementById("val-bg-opacity");
        if (v) v.innerText = `${state.recapBgOpacity}%`;
        ddStyleChanged();
    });
    function ddBindMiniToggle(id, get, set) {
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener("click", () => {
            haptic("selection");
            const nv = !get();
            set(nv);
            el.classList.toggle("active", nv);
            const tv = el.querySelector(".t-val");
            if (tv) tv.innerText = nv ? t("toggle_on") : t("toggle_off");
            ddStyleChanged();
        });
    }
    ddBindMiniToggle("btn-toggle-sub-shadow", () => !!state.recapSubShadow, v => { state.recapSubShadow = v; });
    ddBindMiniToggle("btn-toggle-sub-bold", () => state.recapSubBold !== false, v => { state.recapSubBold = v; });

    // (6) Subtitle position band pills (Top / Centre / Bottom).
    function ddSyncPosBandPills() {
        document.querySelectorAll("#sub-pos-picker .pill-btn").forEach(b => {
            b.classList.toggle("active", b.getAttribute("data-pos-band") === (state.recapSubPosBand || "bottom"));
        });
    }
    document.querySelectorAll("#sub-pos-picker .pill-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            state.recapSubPosBand = btn.getAttribute("data-pos-band") || "bottom";
            ddSyncPosBandPills();
            scheduleRecapCompositeRefresh();
            refreshRecapStep2Summary();
            if (typeof saveRecapSettingsSoon === "function") saveRecapSettingsSoon();
        });
    });

    // (7) Custom subtitle font: upload -> handle in state; the
    // engine-composited preview renders with the uploaded file
    // server-side; FontFace registration covers CSS-side samples.
    function ddSyncCustomFontUi() {
        const status = document.getElementById("sub-font-custom-status");
        const has = !!state.recapSubFontCustomId;
        if (status) status.innerText = has ? `${t("recap_font_custom_active")}: ${state.recapSubFontCustomName || ""}` : "";
        const clr = document.getElementById("btn-sub-font-clear");
        if (clr) clr.classList.toggle("hidden", !has);
    }
    const ddFontInput = document.getElementById("input-sub-font-file");
    if (ddFontInput) ddFontInput.addEventListener("change", async () => {
        const f = ddFontInput.files && ddFontInput.files[0];
        if (!f) return;
        const status = document.getElementById("sub-font-custom-status");
        if (status) status.innerText = t("recap_font_uploading");
        try {
            const fd = new FormData();
            fd.append("font_file", f);
            const res = await fetch("/api/recap/upload_sub_font", {
                method: "POST", headers: getAuthHeaders(false), body: fd,
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || "upload failed");
            state.recapSubFontCustomId = data.font_id;
            state.recapSubFontCustomName = data.filename || f.name;
            try {
                const ff = new FontFace("DDCustomFont", await f.arrayBuffer());
                await ff.load();
                document.fonts.add(ff);
            } catch (e) {}
            ddSyncCustomFontUi();
            scheduleRecapCompositeRefresh();
            if (typeof saveRecapSettingsSoon === "function") saveRecapSettingsSoon();
        } catch (e) {
            if (status) status.innerText = t("recap_font_upload_error");
        } finally {
            ddFontInput.value = "";
        }
    });
    const ddFontClear = document.getElementById("btn-sub-font-clear");
    if (ddFontClear) ddFontClear.addEventListener("click", () => {
        state.recapSubFontCustomId = "";
        state.recapSubFontCustomName = "";
        ddSyncCustomFontUi();
        scheduleRecapCompositeRefresh();
        if (typeof saveRecapSettingsSoon === "function") saveRecapSettingsSoon();
    });

    // (8) Restore-path control sync (called from restoreRecapSettings
    // and applyAccountPrefs' resync).
    window.ddSyncControlsFromState = function () {
        document.querySelectorAll("#recap-protection-picker .pill-btn").forEach(b => {
            b.classList.toggle("active", b.getAttribute("data-protection") === (state.recapProtection || "standard"));
        });
        const setColor = (id, v) => { const el = document.getElementById(id); if (el && v) el.value = v; };
        setColor("input-sub-text-color", state.recapTextColor);
        setColor("input-sub-outline-color", state.recapOutlineColor);
        setColor("input-sub-bg-color", state.recapBgColor);
        if (state.recapOutlineWidth != null) {
            const ow = document.getElementById("slider-outline-width");
            if (ow) ow.value = Math.round(state.recapOutlineWidth * 10);
            const v = document.getElementById("val-outline-width");
            if (v) v.innerText = Number(state.recapOutlineWidth).toFixed(1);
        }
        if (state.recapBgOpacity != null) {
            const bo = document.getElementById("slider-bg-opacity");
            if (bo) bo.value = state.recapBgOpacity;
            const v = document.getElementById("val-bg-opacity");
            if (v) v.innerText = `${state.recapBgOpacity}%`;
        }
        const sh = document.getElementById("btn-toggle-sub-shadow");
        if (sh) {
            sh.classList.toggle("active", !!state.recapSubShadow);
            const tv = sh.querySelector(".t-val");
            if (tv) tv.innerText = state.recapSubShadow ? t("toggle_on") : t("toggle_off");
        }
        const bd = document.getElementById("btn-toggle-sub-bold");
        if (bd) {
            const on = state.recapSubBold !== false;
            bd.classList.toggle("active", on);
            const tv = bd.querySelector(".t-val");
            if (tv) tv.innerText = on ? t("toggle_on") : t("toggle_off");
        }
        ddSyncPosBandPills();
        ddSyncCustomFontUi();
        if (typeof syncSubtitleModeUi === "function") syncSubtitleModeUi();
        if (typeof updateSubtitleCharsEstimator === "function") updateSubtitleCharsEstimator();
        if (ddBoxes().length) ddLoadBoxToSliders(state.recapBlurBoxIndex || 0);
        ddRenderBlurChips();
        refreshRecapStep2Summary();
    };

    // (9) Batch DG: the 9:16 preview is sized WIDTH-first — its
    // natural design width (300px max, or the card width) with the
    // height following the aspect. The old DD rule capped it by a
    // percentage of the screen HEIGHT (36%, shrinking to 22% on
    // scroll), which on tall/desktop webviews squeezed the frame
    // to ~110px wide — too small to read subtitles or place blur
    // boxes, and it shrank while the user was working. Now only a
    // SHORT viewport (<640px) gets a height guard (44%); there is
    // no scroll shrink.
    function ddFitPreviewFrame() {
        const frame = document.getElementById("recap-mockup-frame");
        if (!frame) return;
        if ((state.recapRatio || "9:16") !== "9:16") {
            frame.style.height = "";
            frame.style.width = "";
            return;
        }
        const parentW = frame.parentElement ? frame.parentElement.clientWidth : 300;
        if (parentW <= 0) {
            // Batch DH: the editor setup runs at boot while the Recap
            // pane is hidden — a 0 parent width here must NOT be written
            // as an inline 0px size (it bricks the preview until a
            // resize). Clear and let the CSS default size the frame;
            // showRecapStep re-fits when Step 2 actually opens.
            frame.style.height = "";
            frame.style.width = "";
            return;
        }
        let w = Math.min(300, parentW);
        if (window.innerHeight < 640) {
            w = Math.min(w, window.innerHeight * 0.44 * 9 / 16);
        } else {
            const capH = window.innerHeight - 240;
            if (capH > 200 && (w * 16 / 9) > capH) w = Math.min(w, capH * 9 / 16);
        }
        frame.style.width = `${Math.round(w)}px`;
        frame.style.height = `${Math.round(w * 16 / 9)}px`;
    }
    window.addEventListener("resize", ddFitPreviewFrame);
    window.__ddFitPreviewFrame = ddFitPreviewFrame;
    ["recap-ratio-picker", "recap-fill-picker"].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.addEventListener("click", () => setTimeout(() => {
            refreshRecapStep2Summary();
            ddFitPreviewFrame();
        }, 0));
    });

    setupRecapAccordion();
    ddRenderBlurChips();
    refreshRecapStep2Summary();
    ddFitPreviewFrame();

    // 12. Logo Position, Size & Opacity
    const applyLogoPreviewStyle = () => {
        if (!previewLogo) return;
        const pct = Math.round((state.recapLogoScale || 0.16) * 100);
        previewLogo.style.width = `${Math.max(4, Math.min(40, pct))}%`;
        previewLogo.style.opacity = String(Math.max(0.1, Math.min(1, state.recapLogoOpacity || 0.7)));
    };
    const logoPosBtns = document.querySelectorAll("#recap-logo-pos-picker .pill-btn");
    logoPosBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            logoPosBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            state.recapLogoPos = btn.getAttribute("data-pos") || "top-right";
            if (previewLogo) {
                previewLogo.className = `preview-logo-watermark ${state.recapLogoPos}`;
                applyLogoPreviewStyle();
            }
            scheduleRecapCompositeRefresh();
        });
    });
    const slLogoScale = document.getElementById("slider-logo-scale");
    const slLogoOp = document.getElementById("slider-logo-opacity");
    if (slLogoScale) {
        slLogoScale.addEventListener("input", () => {
            const v = Math.max(4, Math.min(40, parseInt(slLogoScale.value, 10) || 16));
            state.recapLogoScale = +(v / 100).toFixed(2);
            const lbl = document.getElementById("val-logo-scale");
            if (lbl) lbl.innerText = `${v}%`;
            applyLogoPreviewStyle();
            scheduleRecapCompositeRefresh();
        });
    }
    if (slLogoOp) {
        slLogoOp.addEventListener("input", () => {
            const v = Math.max(10, Math.min(100, parseInt(slLogoOp.value, 10) || 70));
            state.recapLogoOpacity = +(v / 100).toFixed(2);
            const lbl = document.getElementById("val-logo-opacity");
            if (lbl) lbl.innerText = `${v}%`;
            applyLogoPreviewStyle();
            scheduleRecapCompositeRefresh();
        });
    }
    applyLogoPreviewStyle();

    // 12b. Custom Logo Upload / Remove (paid-only — the server
    // enforces the tier gate; these handlers only wire the controls
    // that refreshLogoUi shows to paid users).
    const btnLogoUpload = document.getElementById("btn-logo-upload");
    const inputLogoFile = document.getElementById("input-logo-file");
    if (btnLogoUpload && inputLogoFile) {
        // Batch CL: the upload control is a <label for="input-logo-file">
        // — native label activation opens the picker (desktop WebViews
        // drop .click() chains on hidden file inputs), so there is no
        // click listener to wire. A label has no `disabled`: the busy
        // flag + inert styling below preserve the no-double-submit
        // behaviour the old btnLogoUpload.disabled toggling provided.
        let logoUploadBusy = false;
        inputLogoFile.addEventListener("change", async () => {
            const file = inputLogoFile.files ? inputLogoFile.files[0] : null;
            inputLogoFile.value = "";
            if (!file) return;
            if (logoUploadBusy) return;
            logoUploadBusy = true;
            btnLogoUpload.style.pointerEvents = "none";
            btnLogoUpload.setAttribute("aria-disabled", "true");
            try {
                const formData = new FormData();
                formData.append("logo_file", file);
                const res = await fetchWithTimeout("/api/user/logo", {
                    method: "POST",
                    headers: getAuthHeaders(false),
                    body: formData,
                }, 60000);
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail || "Upload failed");
                state.logoCanUse = !!data.can_use_logo;
                state.logoHas = !!data.has_logo;
                state.logoUrl = data.logo_url || null;
                refreshLogoUi();
                showStudioToast(t("toast_logo_uploaded"), 'success');
            } catch (e) {
                showErrorToast(e, 'generic');
            } finally {
                logoUploadBusy = false;
                btnLogoUpload.style.pointerEvents = "";
                btnLogoUpload.removeAttribute("aria-disabled");
            }
        });
    }
    const btnLogoRemove = document.getElementById("btn-logo-remove");
    if (btnLogoRemove) {
        btnLogoRemove.addEventListener("click", async () => {
            btnLogoRemove.disabled = true;
            try {
                const res = await fetchWithTimeout("/api/user/logo", {
                    method: "DELETE",
                    headers: getAuthHeaders(false),
                }, 30000);
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail || "Remove failed");
                state.logoHas = !!data.has_logo;
                state.logoUrl = null;
                refreshLogoUi();
                showStudioToast(t("toast_logo_removed"), 'success');
            } catch (e) {
                showErrorToast(e, 'generic');
            } finally {
                btnLogoRemove.disabled = false;
            }
        });
    }

    // 14. Analyze / Video Info Check Button
    const btnAnalyze = document.getElementById("btn-fetch-recap-info");
    const recapInfoBox = document.getElementById("recap-video-info-box");
    if (btnAnalyze) {
        btnAnalyze.addEventListener("click", async () => {
            const urlInput = document.getElementById("recap-url-input");
            let url = urlInput ? urlInput.value.trim() : "";
            if (!url) {
                showStudioToast(t("toast_empty_url"), 'warning');
                return;
            }
            // Auto extract clean URL from Douyin/TikTok share text
            const urlMatch = url.match(/https?:\/\/[^\s]+/i);
            if (urlMatch) {
                url = urlMatch[0];
                if (urlInput) urlInput.value = url;
            }
            haptic("medium");
            btnAnalyze.disabled = true;
            const originalText = btnAnalyze.innerText;
            btnAnalyze.innerText = "⏳...";
            setRecapPreviewState("loading");
            // R39: the previous source's card must not stand in for
            // this one while the new Analyze runs.
            clearRecapInfoCard();

            try {
                // Phase 4: a Downloader "Use in Recap" hand-off carries
                // the SAME /api/downloader/extract payload the Downloader
                // already verified for this exact URL — reuse it instead
                // of re-probing (no re-upload, no second extract). Every
                // paint / gate / composite step below runs unchanged.
                let data = null;
                const handoff = state.recapExtractHandoff;
                state.recapExtractHandoff = null;
                if (handoff && handoff.data && (handoff.url === url || handoff.data.url === url)) {
                    data = handoff.data;
                } else {
                    const res = await fetchWithTimeout("/api/downloader/extract", {
                        method: "POST",
                        headers: getAuthHeaders(),
                        body: JSON.stringify({ url: url }),
                    }, 60000);
                    data = await res.json();
                    if (!res.ok) throw new Error(data.detail || "Analysis failed");
                }

                if (recapInfoBox) recapInfoBox.classList.remove("hidden");
                const thumbImg = document.getElementById("recap-info-thumb");
                const titleSpan = document.getElementById("recap-info-title");
                const durSpan = document.getElementById("recap-info-dur");
                const platformSpan = document.getElementById("recap-info-platform");
                const subSpan = document.getElementById("recap-info-sub");

                setCardThumb(thumbImg, data.thumbnail || null);
                if (titleSpan) titleSpan.innerText = data.title || "Video";
                state.recapVideoTitle = data.title || "Movie Recap Video";

                state.recapSourceDuration = data.duration_seconds || 0;
                state.lastRecapInfo = data;
                updateRecapSplitCostHint();
                const maxAllowed = recapNormalMaxSeconds();
                let isOverLimit = (data.duration_seconds && data.duration_seconds > maxAllowed);
                let splitAutoEnabled = false;
                if (isOverLimit && state.user?.is_admin) {
                    isOverLimit = false; // admins are exempt from the cap
                } else if (isOverLimit && state.user?.is_vip) {
                    // Split path: the server segments the source into
                    // <=10-min parts and processes each part.
                    enableRecapSplitForLongVideo();
                    splitAutoEnabled = true;
                    isOverLimit = false;
                }
                const btnStartRecap = document.getElementById("btn-start-recap");
                paintRecapInfoMeta(data, isOverLimit, maxAllowed);
                paintRecapUnverifiedWarning();
                if (isOverLimit) {
                    if (btnStartRecap) {
                        btnStartRecap.disabled = true;
                        btnStartRecap.style.opacity = "0.5";
                    }
                    showStudioToast(t("toast_duration_over"), 'warning');
                } else {
                    if (btnStartRecap && !recapJobIsActive()) {
                        btnStartRecap.disabled = false;
                        btnStartRecap.style.opacity = "";
                    }
                    if (splitAutoEnabled) notifyRecapSplitAutoEnabled();
                }

                if (platformSpan) platformSpan.innerText = data.platform || "Video";

                if (data.width && data.height) {
                    state.recapSourceWidth = data.width;
                    state.recapSourceHeight = data.height;
                }
                // Reflect thumbnail on live preview canvas & state
                if (data.thumbnail) {
                    const safeThumb = sanitizeCssMediaUrl(data.thumbnail);
                    state.recapThumbnailUrl = safeThumb;
                    const probeImg = new Image();
                    probeImg.onload = () => {
                        if (probeImg.naturalWidth && probeImg.naturalHeight && (!state.recapSourceWidth || !state.recapSourceHeight)) {
                            state.recapSourceWidth = probeImg.naturalWidth;
                            state.recapSourceHeight = probeImg.naturalHeight;
                            updateCanvasMockupVisuals();
                        }
                    };
                    probeImg.src = data.thumbnail;
                    updateCanvasMockupVisuals();
                }

                state.uploadedVideoPath = null;
                state.recapUploadPending = false;
                // Batch AA: a successful Check for a NEW url is a
                // new source — reset the blur box / subtitle Y to
                // the tight defaults (re-checking the SAME url keeps
                // the user's box).
                const _aaSourceKey = "url:" + (data.url || url);
                if (state.recapLoadedSourceKey !== _aaSourceKey) {
                    state.recapLoadedSourceKey = _aaSourceKey;
                    resetBlurBoxForNewSource();
                }
                // R40 item 9: remember the VERIFIED source URL so the
                // canvas can pull real frames from /recap/preview-frame
                // at the seek position. Unverified payloads get none —
                // the server could not read the source either.
                if (data.metadata_verified && data.url) {
                    state.recapVerifiedSourceUrl = data.url;
                    if (state.recapFrameUrl) { URL.revokeObjectURL(state.recapFrameUrl); state.recapFrameUrl = null; }
                    if (window.scheduleRecapFrameRefresh) window.scheduleRecapFrameRefresh(Math.min(5, data.duration_seconds || 5));
                    // Batch AK: composite for the newly verified source.
                    if (window.scheduleRecapCompositeRefresh) window.scheduleRecapCompositeRefresh();
                } else {
                    state.recapVerifiedSourceUrl = null;
                }
                const sliderSeek = document.getElementById("slider-canvas-seek");
                const seekBadge = document.getElementById("canvas-seek-time-badge");
                if (sliderSeek) {
                    sliderSeek.max = Math.min(600, data.duration_seconds || 60);
                    sliderSeek.value = Math.min(5, data.duration_seconds || 5);
                }
                if (seekBadge) {
                    // Unverified payloads carry an invented placeholder
                    // duration ("03:30") — never paint it as the source's
                    // length; the total stays unknown until verified.
                    seekBadge.innerText = data.duration_seconds
                        ? `⏱️ 00:05s / ${data.estimated_duration || "00:00"}s`
                        : "⏱️ 00:05s / --:--";
                }
                setRecapPreviewState("ready");
            } catch (err) {
                setRecapPreviewState(recapHasSource() ? "ready" : "empty");
                // The extract returned nothing at all (timeout / network):
                // a source URL is present but its metadata is unverified.
                // Show the info card in the same honest unverified state
                // a fallback payload gets, instead of leaving it hidden
                // behind its static placeholders (RS-4).
                if (url) {
                    if (recapInfoBox) recapInfoBox.classList.remove("hidden");
                    const tSpan = document.getElementById("recap-info-title");
                    if (tSpan) tSpan.innerText = "Video";
                    const pSpan = document.getElementById("recap-info-platform");
                    if (pSpan) {
                        pSpan.innerText = /tiktok\.com/i.test(url) ? "TikTok"
                            : /youtu/i.test(url) ? "YouTube"
                            : /facebook\.com|fb\.watch/i.test(url) ? "Facebook" : "Video";
                    }
                    state.recapVideoTitle = "Movie Recap Video";
                    state.recapSourceDuration = 0;
                    state.lastRecapInfo = { metadata_verified: false, duration_seconds: 0, has_subtitles: false, estimated_duration: "00:00" };
                    paintRecapInfoMeta(state.lastRecapInfo, false, recapNormalMaxSeconds());
                    paintRecapUnverifiedWarning();
                    // Batch CI: duration is back to UNKNOWN here —
                    // repaint the gate so no stale notice survives.
                    updateRecapDurationGate();
                    const sb = document.getElementById("canvas-seek-time-badge");
                    if (sb) sb.innerText = "⏱️ 00:05s / --:--";
                }
                const msg = err.message || "";
                if (msg.includes("Douyin") || msg.includes("Fresh cookies")) {
                    showStudioToast(t("toast_douyin_blocked"), 'warning', 6000);
                    const btnFileTab = document.getElementById("btn-source-mode-file");
                    if (btnFileTab) btnFileTab.click();
                } else if (blockedLinkInfo(msg).blocked) {
                    // Blocked source (TikTok / VidMate / a 403 wall):
                    // the mapper's specific copy names the source and
                    // the fix, and the user lands on the upload tab —
                    // the path that always works. Same treatment the
                    // Douyin branch above already had; previously every
                    // other blocked source fell into the bare generic.
                    showErrorToast(err, 'recap');
                    const btnFileTab = document.getElementById("btn-source-mode-file");
                    if (btnFileTab) btnFileTab.click();
                } else if (isNetworkLevelError(err) && recapHasSource()) {
                    // Q2 (QA cycle 2026-11-02): the extract request
                    // died at the network level (connection closed /
                    // timeout — no response at all) while a preview
                    // frame is already on the canvas (the paste-time
                    // source thumbnail). The info card painted just
                    // above already shows the honest "metadata
                    // unverified" state; a generic error toast on top
                    // reads as "the frame you can see failed", which
                    // is false. Failures with no preview on the canvas
                    // still toast in the branch below.
                    console.warn('Analyze: extract unreachable, preview-only state kept:', err);
                } else {
                    showErrorToast(err, 'generic');
                }
            } finally {
                btnAnalyze.disabled = false;
                btnAnalyze.innerText = originalText;
                // Batch AK: the settled source re-composites.
                scheduleRecapCompositeRefresh();
            }
        });
    }

    // Auto-detect on input / paste with live YouTube thumbnail & debounce
    let recapUrlTimer = null;
    const urlInput = document.getElementById("recap-url-input");
    if (urlInput) {
        urlInput.addEventListener("input", () => {
            const val = urlInput.value.trim();
            // R40 item 5: a typed URL is a present source — reveal the
            // Style/Subtitle/Blur controls immediately, without waiting
            // for Analyze to succeed (recompute visibility only; the
            // preview mode itself is untouched).
            if (val && state.recapPreviewState !== "ready") {
                setRecapPreviewState(state.recapPreviewState || "empty");
            }
            if (!val) {
                state.recapThumbnailUrl = null;
                state.recapVerifiedSourceUrl = null;
                if (state.recapFrameUrl) { URL.revokeObjectURL(state.recapFrameUrl); state.recapFrameUrl = null; }
                if (state.recapCompositeUrl) { URL.revokeObjectURL(state.recapCompositeUrl); state.recapCompositeUrl = null; }
                recapCompositeHash = "";
                updateCanvasMockupVisuals();
                if (recapInfoBox) recapInfoBox.classList.add("hidden");
                const warnEl = document.getElementById("recap-unverified-warning");
                if (warnEl) warnEl.classList.add("hidden");
                setRecapPreviewState(recapHasSource() ? "ready" : "empty");
                return;
            }

            // Real-time instant YouTube thumbnail extraction
            const ytMatch = val.match(/(?:youtu\.be\/|youtube\.com\/(?:embed\/|v\/|watch\?v=|watch\?.+&v=|shorts\/))([\w-]{11})/);
            if (ytMatch && ytMatch[1]) {
                state.recapThumbnailUrl = `https://img.youtube.com/vi/${ytMatch[1]}/hqdefault.jpg`;
                updateCanvasMockupVisuals();
            }

            // Auto-fetch video info after 700ms pause
            clearTimeout(recapUrlTimer);
            recapUrlTimer = setTimeout(() => {
                if (val && btnAnalyze && !btnAnalyze.disabled) {
                    btnAnalyze.click();
                }
            }, 700);
        });
    }

    // Wire up Voice Cards (top-4 grid + the all-12 grid in the expander
    // render the SAME 12 voices as cards; one handler keeps both grids'
    // active states in sync — there is no second <select> picker).
    const voiceCards = document.querySelectorAll("#recap-top-voices .voice-card, #recap-all-voices-grid .voice-card");
    voiceCards.forEach(card => {
        card.addEventListener("click", () => {
            haptic("selection");
            const voice = card.getAttribute("data-voice");
            state.recapVoice = voice;
            // Batch BV: picking a persona deselects own-voice (the
            // persona becomes the narration voice again, not merely
            // the fallback).
            deselectRecapOwnVoice();
            voiceCards.forEach(c => c.classList.toggle("active", c.getAttribute("data-voice") === voice));
        });
    });

    // 15. 1-Click Recap Action
    const btnRecap = document.getElementById("btn-start-recap");
    if (btnRecap) {
        btnRecap.addEventListener("click", handleStartRecap);
    }


    // ── Recap settings persistence (overnight hands-on fix, 2026-11-02) ──
    // Every Recap setting used to live only in `state`, so closing and
    // reopening the Mini App silently reset the whole panel (ratio, fill,
    // subtitle style, voice, engine, split…) to defaults — verified live:
    // ratio 16:9 + fill crop reverted to 9:16 + blur after one reload.
    // Persist the settings slice to localStorage (same pattern as
    // vcp_active_recap_job_id / vcp_welcome_dismissed) and restore it
    // here, after every control handler above has been registered.
    // Restore is defensive: only known enum values, booleans and ranged
    // integers are accepted; anything else keeps the shipped default.
    const RECAP_SETTINGS_KEY = "vcp_recap_settings";
    const RECAP_SETTING_ENUMS = {
        recapRatio: ["9:16", "16:9", "1:1", "Original"],
        recapRatioFill: ["blur", "crop", "black"],
        recapSpeed: VCP_SPEED_OPTIONS,
        recapStoryStyle: ["viral", "dramatic", "funny", "pov", "standard", "narrator"],
        recapSubStyle: ["yellow", "white", "boxed", "neon", "band", "heavy", "soft", "rose", "orange", "mint", "cinematic", "glass"],
        recapProtection: ["off", "standard", "strong"],
        recapSubFont: ["padauk", "noto_sans", "pyidaungsu", "noto_serif"],
        recapLogoPos: ["top-right", "top-left", "bottom-right", "bottom-left", "top-center"],
        recapSourceLanguage: ["auto", "en", "zh", "ko", "th", "ja", "hi", "my", "es", "fr", "de", "id", "vi", "ru", "ar", "it", "pt", "tr"],
        recapRenderQuality: ["standard", "high", "max"],
        recapTtsEngine: ["voxcpm", "gemini", "edge", "none"],
        recapSubRenderMode: ["png", "timeline", "ass", "karaoke", "soft"],
        recapSubEffect: ["none", "fade", "pop", "karaoke"],
    };
    // Batch DD: the title keys are gone with the feature — legacy
    // snapshots carrying them are simply never applied.
    const RECAP_SETTING_BOOLS = ["recapSyncCrop", "recapMirror", "recapBurnSubs", "recapBlurSubs", "recapBlurAutoDetect", "recapLogo", "recapBlurBoxTouched", "recapSubShadow", "recapSubBold", "recapEmbedSoftSubs"];
    const RECAP_SETTING_INTS = {
        recapCropTop: [0, 35], recapCropBottom: [0, 35],
        recapCropLeft: [0, 35], recapCropRight: [0, 35],
        recapFontSize: [10, 30], recapLines: [1, 3], recapSplitMinutes: [1, 10],
        recapBlurY: [0, 100], recapBlurH: [1, 50], recapSubY: [0, 100],
        recapSubMargin: [0, 95], recapSettingsVersion: [0, 10],
        recapBlurX: [0, 100], recapBlurW: [10, 100],
        recapSubX: [10, 90], recapSubW: [10, 100],
        recapBgOpacity: [0, 100],
        recapBlurStrength: [4, 40],
    };

    function snapshotRecapSettings() {
        const snap = {};
        Object.keys(RECAP_SETTING_ENUMS).forEach(k => { snap[k] = state[k]; });
        RECAP_SETTING_BOOLS.forEach(k => { snap[k] = !!state[k]; });
        Object.keys(RECAP_SETTING_INTS).forEach(k => { snap[k] = state[k]; });
        snap.recapZoom = state.recapZoom;
        snap.recapVoice = state.recapVoice;
        snap.recapGeminiVoice = state.recapGeminiVoice || "";
        const st = document.getElementById("recap-split-toggle");
        snap.recapSplitEnabled = !!(st && st.checked);
        const by = document.getElementById("slider-blur-y");
        const bh = document.getElementById("slider-blur-h");
        const bs = document.getElementById("slider-blur-strength");
        const sy = document.getElementById("slider-sub-y");
        snap.recapBlurY = by ? parseInt(by.value) : 85;
        snap.recapBlurH = bh ? parseInt(bh.value) : 7;
        snap.recapBlurStrength = bs ? parseInt(bs.value) : 18;
        // Batch AK: the slider is the bottom margin now — the
        // snapshot stores the margin + the schema version.
        snap.recapSubMargin = sy ? parseInt(sy.value) : 5;
        snap.recapSettingsVersion = 2;
        const bx = document.getElementById("slider-blur-x");
        const bw = document.getElementById("slider-blur-w");
        const sx = document.getElementById("slider-sub-x");
        const sw = document.getElementById("slider-sub-w");
        snap.recapBlurX = bx ? parseInt(bx.value) : 4;
        snap.recapBlurW = bw ? parseInt(bw.value) : 92;
        snap.recapSubX = sx ? parseInt(sx.value) : 50;
        snap.recapSubW = sw ? parseInt(sw.value) : 92;
        // Batch DD: free style controls + blur-box list + custom
        // font handle ride the snapshot (protection rides the enums).
        snap.recapTextColor = state.recapTextColor || "";
        snap.recapOutlineColor = state.recapOutlineColor || "";
        snap.recapBgColor = state.recapBgColor || "";
        snap.recapOutlineWidth = state.recapOutlineWidth;
        snap.recapSubFontCustomId = state.recapSubFontCustomId || "";
        snap.recapSubFontCustomName = state.recapSubFontCustomName || "";
        snap.recapBlurBoxes = state.recapBlurBoxes || [];
        snap.recapSubPosBand = state.recapSubPosBand || "bottom";
        return snap;
    }

    function saveRecapSettings() {
        try { localStorage.setItem(RECAP_SETTINGS_KEY, JSON.stringify(snapshotRecapSettings())); } catch (e) {}
    }
    // Phase 4: My Presets read the live settings slice through the
    // SAME snapshot the persistence path writes (no forked key list).
    window.__snapshotRecapSettings = snapshotRecapSettings;
    window.__saveRecapSettingsNow = saveRecapSettings;
    let recapSaveTimer = null;
    function saveRecapSettingsSoon() {
        if (recapSaveTimer) clearTimeout(recapSaveTimer);
        recapSaveTimer = setTimeout(saveRecapSettings, 250);
    }

    function syncPillGroup(selector, attr, value) {
        document.querySelectorAll(selector).forEach(b => {
            b.classList.toggle("active", b.getAttribute(attr) === String(value));
        });
    }
    function syncToggleBtn(id, on) {
        const btn = document.getElementById(id);
        if (!btn) return;
        btn.classList.toggle("active", !!on);
        const val = btn.querySelector(".t-val");
        if (val) val.innerText = on ? t("toggle_on") : t("toggle_off");
    }
    // Batch AM (S9 cosmetic): while the source-subtitle Blur toggle
    // is OFF, the geometry/strength sliders cannot affect anything —
    // the submit payload sends blur_subtitles=false and the engine's
    // master gate turns that into ZERO boxes (recap_pipeline) — so
    // grey them out instead of letting the panel assert live values.
    // Display-only: slider values, state and payloads are untouched,
    // and the sliders keep their values for when the toggle returns.
    function syncBlurSlidersEnabled() {
        const off = state.recapBlurSubs === false;
        ["slider-blur-y", "slider-blur-h", "slider-blur-x",
         "slider-blur-w", "slider-blur-strength"].forEach(id => {
            const el = document.getElementById(id);
            const group = el && el.closest ? el.closest(".slider-group") : null;
            if (group) group.classList.toggle("blur-off", off);
        });
    }

    function restoreRecapSettings() {
        let saved = null;
        try { saved = JSON.parse(localStorage.getItem(RECAP_SETTINGS_KEY) || "null"); } catch (e) {}
        if (!saved || typeof saved !== "object") return;
        // Batch Q font choice: legacy snapshot values from before the
        // four-key registry are migrated on restore ("Serif" was
        // Noto Serif Myanmar and keeps that meaning; "Default" was
        // the old factory value — Batch T, Ulfa 2026-11-04: those
        // savers move to the new premium default, Padauk).
        const LEGACY_SUB_FONTS = { "default": "padauk", "serif": "noto_serif" };
        if (LEGACY_SUB_FONTS[saved.recapSubFont]) saved.recapSubFont = LEGACY_SUB_FONTS[saved.recapSubFont];
        // Batch AK settings migration — runs once, versioned. A
        // pre-AK snapshot stores the subtitle anchor as recapSubY
        // (centre-% of the picture height); convert it
        // plate-height-aware to the bottom-edge margin so the
        // restored subtitle lands where it always was, and stamp
        // version 2 so it never re-runs.
        if ((parseInt(saved.recapSettingsVersion) || 0) < 2 && saved.recapSubMargin == null
            && Number.isFinite(parseInt(saved.recapSubY))) {
            saved.recapSubMargin = convertSubCentreToMargin(
                parseInt(saved.recapSubY),
                parseInt(saved.recapFontSize) || 16,
                parseInt(saved.recapLines) || 1,
            );
            saved.recapSettingsVersion = 2;
        }
        if (saved.recapSpeed != null) saved.recapSpeed = normalizeSpeedOption(saved.recapSpeed);
        Object.entries(RECAP_SETTING_ENUMS).forEach(([k, allowed]) => {
            if (allowed.includes(saved[k])) state[k] = saved[k];
        });
        RECAP_SETTING_BOOLS.forEach(k => { if (typeof saved[k] === "boolean") state[k] = saved[k]; });
        Object.entries(RECAP_SETTING_INTS).forEach(([k, range]) => {
            const v = parseInt(saved[k]);
            if (Number.isFinite(v) && v >= range[0] && v <= range[1]) state[k] = v;
        });
        const zoom = parseFloat(saved.recapZoom);
        if (Number.isFinite(zoom) && zoom >= 1 && zoom <= 1.2) state.recapZoom = zoom;
        if (typeof saved.recapVoice === "string" && saved.recapVoice) state.recapVoice = saved.recapVoice;
        if (typeof saved.recapGeminiVoice === "string") state.recapGeminiVoice = saved.recapGeminiVoice;
        // Batch DD restores (strings / list / band).
        if (typeof saved.recapTextColor === "string") state.recapTextColor = saved.recapTextColor;
        if (typeof saved.recapOutlineColor === "string") state.recapOutlineColor = saved.recapOutlineColor;
        if (typeof saved.recapBgColor === "string") state.recapBgColor = saved.recapBgColor;
        if (typeof saved.recapOutlineWidth === "number") state.recapOutlineWidth = saved.recapOutlineWidth;
        if (typeof saved.recapSubFontCustomId === "string") state.recapSubFontCustomId = saved.recapSubFontCustomId;
        if (typeof saved.recapSubFontCustomName === "string") state.recapSubFontCustomName = saved.recapSubFontCustomName;
        if (Array.isArray(saved.recapBlurBoxes)) state.recapBlurBoxes = saved.recapBlurBoxes.filter(b => b && typeof b === "object").slice(0, 5);
        if (["top", "center", "bottom"].includes(saved.recapSubPosBand)) state.recapSubPosBand = saved.recapSubPosBand;
        // Batch DD migration-on-read: a legacy snapshot with a
        // deliberately-set single box (recapBlurBoxTouched) and NO
        // list becomes a one-element list.
        if ((!state.recapBlurBoxes || !state.recapBlurBoxes.length) && saved.recapBlurBoxTouched) {
            state.recapBlurBoxes = [{
                x: Number.isFinite(parseInt(saved.recapBlurX)) ? parseInt(saved.recapBlurX) : 4,
                y: Number.isFinite(parseInt(saved.recapBlurY)) ? parseInt(saved.recapBlurY) : 85,
                w: Number.isFinite(parseInt(saved.recapBlurW)) ? parseInt(saved.recapBlurW) : 92,
                h: Number.isFinite(parseInt(saved.recapBlurH)) ? parseInt(saved.recapBlurH) : 7,
                strength: Number.isFinite(parseInt(saved.recapBlurStrength)) ? parseInt(saved.recapBlurStrength) : 18,
            }];
        }

        // ── Re-sync every control so the UI shows the restored state ──
        syncPillGroup("#recap-ratio-picker .ratio-btn", "data-ratio", state.recapRatio);
        syncPillGroup("#recap-fill-picker .pill-btn", "data-fill", state.recapRatioFill);
        applyCropValues(state.recapCropTop || 0, state.recapCropBottom || 0,
                        state.recapCropLeft || 0, state.recapCropRight || 0);
        if (btnSyncCrop) btnSyncCrop.classList.toggle("active", !!state.recapSyncCrop);
        const btnMirrorEl = document.getElementById("btn-toggle-mirror");
        if (btnMirrorEl) {
            btnMirrorEl.classList.toggle("active", !!state.recapMirror);
            const ms = btnMirrorEl.querySelector(".toggle-status");
            if (ms) ms.innerText = state.recapMirror ? t("mirror_on") : t("mirror_off");
        }
        if (canvas) canvas.classList.toggle("mirrored", !!state.recapMirror);
        document.querySelectorAll("#recap-zoom-picker .pill-btn").forEach(b => {
            b.classList.toggle("active", parseFloat(b.getAttribute("data-zoom")) === state.recapZoom);
        });
        syncPillGroup("#recap-speed-picker .speed-seg", "data-speed", state.recapSpeed);
        moveSpeedPill(document.getElementById("recap-speed-picker"));
        syncPillGroup("#recap-split-minutes-picker .pill-btn", "data-split-min", state.recapSplitMinutes);
        const splitToggleEl = document.getElementById("recap-split-toggle");
        if (splitToggleEl) {
            splitToggleEl.checked = !!saved.recapSplitEnabled;
            const wrap = document.getElementById("recap-split-minutes-wrap");
            if (wrap) wrap.classList.toggle("hidden", !splitToggleEl.checked);
        }
        // AZ-11: the language pills are retired — the one dropdown
        // is painted by syncRecapLangViews (called just below).
        if (window.syncRecapLangViews) syncRecapLangViews();
        refreshRecapQualityLock();
        syncPillGroup("#recap-quality-picker .pill-btn", "data-quality", state.recapRenderQuality);
        // Batch AW: a saved "voxcpm" resolves to "gemini" for a free
        // profile BEFORE the engine pills paint (the lock re-syncs the
        // pill actives + the persona/Gemini areas from resolved state).
        refreshRecapEngineLock();
        syncPillGroup("#recap-engine-picker .pill-btn", "data-engine", state.recapTtsEngine);
        const gv = document.getElementById("recap-gemini-voice-wrap");
        if (gv) gv.classList.toggle("hidden", state.recapTtsEngine !== "gemini");
        const pa = document.getElementById("recap-persona-area");
        if (pa) pa.classList.toggle("hidden", state.recapTtsEngine === "gemini" || state.recapTtsEngine === "none");
        if (geminiVoiceSel) geminiVoiceSel.value = state.recapGeminiVoice || "";
        document.querySelectorAll("#story-style-picker-quick .style-select-pill").forEach(p => {
            p.classList.toggle("active", p.getAttribute("data-story") === state.recapStoryStyle);
        });
        moveSegPills();
        document.querySelectorAll("#recap-top-voices .voice-card, #recap-all-voices-grid .voice-card").forEach(c => {
            c.classList.toggle("active", c.getAttribute("data-voice") === state.recapVoice);
        });
        syncToggleBtn("btn-toggle-subs", state.recapBurnSubs);
        syncToggleBtn("btn-toggle-blur", state.recapBlurSubs);
        syncToggleBtn("btn-toggle-logo", state.recapLogo);
        // Batch AM (S9 cosmetic): the greyed-slider state follows
        // the restored Blur toggle, here and on every re-sync.
        syncBlurSlidersEnabled();
        if (previewSub) previewSub.style.display = state.recapBurnSubs ? "block" : "none";
        if (previewBlur) previewBlur.style.display = state.recapBlurSubs ? "block" : "none";
        // The plate exists only where the render draws one: blur on
        // AND burn on (blur safefix 2026-11-03).
        // AZ-11: the subtitle-style picker is a <select> now — sync
        // its value directly (same state field as before).
        const subStyleSel = document.getElementById("sub-style-picker");
        if (subStyleSel) subStyleSel.value = state.recapSubStyle;
        if (typeof syncRecapSubStyleCards === "function") syncRecapSubStyleCards();
        // Batch AK: the handle's classes are fixed (the style lives
        // in the composited frame, not in CSS classes).
        if (previewSub) previewSub.className = "preview-sub-box draggable-box";
        syncPillGroup("#recap-sub-font-picker .pill-btn", "data-font", state.recapSubFont);
        syncPillGroup("#recap-logo-pos-picker .pill-btn", "data-pos", state.recapLogoPos);
        if (previewLogo) previewLogo.className = `preview-logo-watermark ${state.recapLogoPos}`;
        // Logo chip visibility follows the central rule (paid +
        // uploaded + toggle on), not the persisted toggle alone.
        refreshLogoUi();
        const by = document.getElementById("slider-blur-y");
        const bh = document.getElementById("slider-blur-h");
        const bs = document.getElementById("slider-blur-strength");
        const sy = document.getElementById("slider-sub-y");
        if (by && Number.isFinite(state.recapBlurY)) by.value = state.recapBlurY;
        if (bh && Number.isFinite(state.recapBlurH)) bh.value = state.recapBlurH;
        if (bs && Number.isFinite(state.recapBlurStrength)) {
            bs.value = state.recapBlurStrength;
            bs.dispatchEvent(new Event("input"));
        }
        if (sy && Number.isFinite(state.recapSubMargin)) sy.value = state.recapSubMargin;
        const bx = document.getElementById("slider-blur-x");
        const bw = document.getElementById("slider-blur-w");
        const sx = document.getElementById("slider-sub-x");
        const sw = document.getElementById("slider-sub-w");
        if (bx && Number.isFinite(state.recapBlurX)) bx.value = state.recapBlurX;
        if (bw && Number.isFinite(state.recapBlurW)) bw.value = state.recapBlurW;
        if (sx && Number.isFinite(state.recapSubX)) sx.value = state.recapSubX;
        if (sw && Number.isFinite(state.recapSubW)) sw.value = state.recapSubW;
        // Batch BI: the auto-detect blur switch re-syncs like the
        // other toggles (default ON when nothing was ever saved).
        const blurAutoDetectEl = document.getElementById("recap-blur-autodetect-toggle");
        if (blurAutoDetectEl) blurAutoDetectEl.checked = state.recapBlurAutoDetect !== false;
        // Batch DD: re-sync the new controls from state.
        if (typeof window.ddSyncControlsFromState === "function") {
            try { window.ddSyncControlsFromState(); } catch (e) {}
        }
        syncSubtitleFontSize(state.recapFontSize || 16);
        syncSubtitleLines(state.recapLines || 1);
        syncBlurBoxVisual();
        syncSubBoxVisual();
        updateCanvasMockupVisuals();
        if (typeof ddFitPreviewFrame === "function") ddFitPreviewFrame();
        if (typeof refreshRecapStep2Summary === "function") refreshRecapStep2Summary();
        if (typeof refreshRecapStep4Thumb === "function") refreshRecapStep4Thumb();
        if (typeof refreshRecapStep4Facts === "function") refreshRecapStep4Facts();
        if (typeof updateRecapSplitCostHint === "function") updateRecapSplitCostHint();
        if (typeof syncRecapMasterPresetActive === "function") syncRecapMasterPresetActive();
    }

    const recapTabEl = document.getElementById("tab-recap");
    if (recapTabEl) {
        ["click", "change", "input", "pointerup"].forEach(evt =>
            recapTabEl.addEventListener(evt, saveRecapSettingsSoon));
    }
    restoreRecapSettings();

    // Batch AM (M8ii): account prefs land AFTER this boot restore
    // (loadAccountPrefs resolves async). applyAccountPrefs calls
    // this hook after overwriting state from the server and writing
    // the same values through into the snapshot, so re-running the
    // restore re-syncs every recap control from the merged result.
    // Idempotent by construction: it performs exactly the reads and
    // control syncs the boot restore already performed — no fetch,
    // no submit, no state writes beyond the restore's own.
    window.__RECAP_SETTINGS_KEY = RECAP_SETTINGS_KEY;
    window.restoreRecapSettings = restoreRecapSettings;
    window.__ddFitPreviewFrame = typeof ddFitPreviewFrame === "function" ? ddFitPreviewFrame : null;
    window.refreshRecapStep2Summary = typeof refreshRecapStep2Summary === "function" ? refreshRecapStep2Summary : null;
    window.refreshRecapStep4Output = typeof refreshRecapStep4Output === "function" ? refreshRecapStep4Output : null;
    window.resyncRecapControlsAfterPrefs = function () {
        restoreRecapSettings();
    };

    // Batch L: "Re-run with these settings" (History detail) — map a
    // stored RecapConfigRequest back into the settings-snapshot space
    // and run it through the SAME restore path as the persisted panel
    // state (so validation + control sync are shared, not forked),
    // then restore the source: the URL field for link jobs, or the
    // uploaded-path state for upload jobs whose file is still on the
    // server. PREFILL ONLY — nothing is submitted until the user
    // taps Start themselves.
    window.prefillRecapFromHistory = function(cfg, item) {
        if (!cfg || typeof cfg !== "object") return;
        const snap = {
            recapRatio: cfg.ratio, recapRatioFill: cfg.ratio_fill,
            recapSpeed: normalizeSpeedOption(cfg.voice_speed), recapStoryStyle: cfg.story_style,
            recapSubStyle: cfg.sub_style, recapSubFont: cfg.sub_font,
            recapLogoPos: cfg.logo_pos, recapSourceLanguage: cfg.source_language,
            recapRenderQuality: cfg.render_quality, recapTtsEngine: cfg.tts_engine,
            recapMirror: cfg.mirror_video !== false,
            recapBurnSubs: cfg.burn_subtitles !== false,
            recapBlurSubs: cfg.blur_subtitles !== false,
            recapLogo: cfg.include_logo !== false,
            recapCropTop: cfg.crop_top, recapCropBottom: cfg.crop_bottom,
            recapCropLeft: cfg.crop_left, recapCropRight: cfg.crop_right,
            recapFontSize: cfg.sub_font_size, recapLines: cfg.sub_lines,
            recapSplitMinutes: cfg.split_minutes,
            recapBlurY: cfg.blur_y, recapBlurH: cfg.blur_h, recapSubY: cfg.sub_y,
            // Batch AK: the anchor round-trips as the bottom margin
            // — the job's own margin when it has one, else converted
            // from its centre-% sub_y with the job's own font/lines,
            // stamped v2 so restore takes it as-is.
            recapSubMargin: (cfg.sub_bottom_margin != null) ? cfg.sub_bottom_margin
                : convertSubCentreToMargin(cfg.sub_y || 85, cfg.sub_font_size || 16, cfg.sub_lines || 1),
            recapSettingsVersion: 2,
            // Batch AG: horizontal geometry round-trips too — a job
            // rendered with an off-centre box / subtitle re-runs with
            // the same placement (pre-AG configs carry none of these
            // keys and restore keeps the current values).
            recapBlurX: cfg.blur_x, recapBlurW: cfg.blur_w,
            recapSubX: cfg.sub_x, recapSubW: cfg.sub_w,
            // Batch DD: protection / style controls / box list /
            // custom font round-trip (pre-DD configs carry none of
            // these keys and restore keeps the current values; a
            // legacy explicit single box migrates in restore).
            recapProtection: cfg.protection_level,
            recapTextColor: cfg.sub_text_color || "",
            recapOutlineColor: cfg.sub_outline_color || "",
            recapBgColor: cfg.sub_bg_color || "",
            recapOutlineWidth: (cfg.sub_outline_width != null) ? cfg.sub_outline_width : null,
            recapBgOpacity: cfg.sub_bg_opacity,
            recapSubShadow: cfg.sub_shadow === true,
            recapSubBold: cfg.sub_bold !== false,
            recapBlurBoxes: Array.isArray(cfg.blur_boxes) ? cfg.blur_boxes : undefined,
            recapSubFontCustomId: cfg.sub_font_custom_id || "",
            // Batch AA: a job whose blur box was explicit re-runs
            // with an explicit box — authoritativeness is preserved
            // through the prefill (pre-AA configs have no marker and
            // stay on the legacy path).
            recapBlurBoxTouched: cfg.blur_box_explicit === true,
            recapBlurStrength: cfg.blur_strength,
            // Batch BI: the auto-detect switch round-trips too
            // (pre-BI configs carry no key and restore keeps ON).
            recapBlurAutoDetect: cfg.blur_auto_detect !== false,
            recapZoom: cfg.zoom_factor, recapVoice: cfg.voice_slug,
            recapGeminiVoice: cfg.gemini_voice || "",
            recapSubRenderMode: cfg.sub_render_mode || "timeline",
            recapSubEffect: cfg.sub_effect || "none",
            recapEmbedSoftSubs: cfg.embed_soft_subs !== false,
            recapSplitEnabled: !!cfg.split_video,
        };
        try { localStorage.setItem(RECAP_SETTINGS_KEY, JSON.stringify(snap)); } catch (e) {}
        restoreRecapSettings();
        // Batch AH: exactly one script-review toggle exists now.
        const srToggle = document.getElementById("recap-script-review-toggle");
        if (srToggle) srToggle.checked = !!cfg.script_review;
        if (cfg.title) state.recapVideoTitle = cfg.title;
        state.recapSourceDuration = cfg.duration_seconds || null;
        // Batch CI: a prefilled source's duration becomes known
        // HERE (Step-4 context) — the gate must evaluate it too.
        updateRecapDurationGate();
        const srcUrl = (cfg.url || "").trim();
        if (/^https?:\/\//i.test(srcUrl)) {
            state.uploadedVideoPath = null;
            const urlInput = document.getElementById("recap-url-input");
            if (urlInput) urlInput.value = srcUrl;
            const btnUrlMode = document.getElementById("btn-source-mode-url");
            if (btnUrlMode) btnUrlMode.click();
        } else if (srcUrl && item && item.source_available) {
            state.uploadedVideoPath = srcUrl;
            const btnFileMode = document.getElementById("btn-source-mode-file");
            if (btnFileMode) btnFileMode.click();
        } else {
            // Upload source is gone: settings are filled, the user
            // picks a source again (the toast explains prefill).
            state.uploadedVideoPath = null;
        }
        // Batch AA: the prefilled source IS the loaded source — a
        // later Analyze of THIS url must not reset the prefilled
        // box; only a different source does.
        if (/^https?:\/\//i.test(srcUrl)) state.recapLoadedSourceKey = "url:" + srcUrl;
        else if (srcUrl) state.recapLoadedSourceKey = "file:" + srcUrl;
        const ctaBtn = document.getElementById("btn-start-recap");
        if (ctaBtn) ctaBtn.scrollIntoView({ behavior: "smooth", block: "center" });
    };

    // Initial canvas mockup sync
    updateCanvasMockupVisuals();
    updateSubtitleVisualPreview();
}

// ── Streamlined Studio Global Window Helpers ─────────────────────────────────
window.previewVoiceSlug = function(slug, e) {
    if (e) e.stopPropagation();
    const btn = e ? e.currentTarget : null;
    playVoicePreview(slug, btn);
};

window.toggleAllRecapVoices = function() {
    const dropdown = document.getElementById("all-voices-dropdown-wrap");
    const txt = document.getElementById("all-voices-toggle-text");
    if (!dropdown) return;
    const isHidden = dropdown.classList.toggle("hidden");
    if (txt) {
        txt.innerText = isHidden ? t("voices_show_all") : t("voices_hide_all");
    }
};

window.toggleSectionAccordion = function(btn) {
    const wrap = btn.closest(".advanced-studio-accordion");
    if (!wrap) return;
    const body = wrap.querySelector(".advanced-accordion-body");
    const chevron = btn.querySelector(".accordion-chevron");
    if (!body) return;
    const isHidden = body.classList.toggle("hidden");
    if (chevron) chevron.innerText = isHidden ? "▼" : "▲";
};

window.toggleRecapCropDetail = function() {
    const body = document.getElementById("recap-crop-detail");
    const chevron = document.getElementById("crop-detail-chevron");
    if (!body) return;
    const isHidden = body.classList.toggle("hidden");
    if (chevron) chevron.innerText = isHidden ? "▼" : "▲";
};

// Batch AV: window.toggleAdvancedRecapStudio was retired with the
// advanced accordion — its sections now live inside Steps 2–4 and the
// accordion's only caller (its header button) is gone from the markup.

// A recap job is active while its status-polling timer runs: the
// timer is created in startRecapJobPolling and cleared on every
// terminal state (SCRIPT_READY / SUCCESS / FAILED). Used to stop the
// Analyze/Upload success paths from re-enabling the Start button
// mid-job and to refuse a parallel submit (double-submit fix, hunt
// #2 R1 — a second submit charged a second credit and left the first
// job untracked).
//
// SCRIPT_READY counts as active too (hunt #3 E8): polling stops while
// the script awaits review, but the job still owns the user's recap
// slot (the server counts it as active), so the poller records it in
// state.recapScriptReadyJobId and it keeps blocking a parallel submit
// until the user renders it.
function recapJobIsActive() {
    return !!state.recapPollTimer || !!state.recapScriptReadyJobId;
}

async function handleStartRecap() {
    // Batch AC: a parallel submit is explained IN PLACE (limit text +
    // the jobs sheet with the running job) — never a silent dead end,
    // and the workspace is never evicted.
    if (recapJobIsActive()) showLiveSubmitLimit("recap");
    if (recapJobIsActive()) return; // a recap job is already running
    // Hard guard: a picked file whose upload has not succeeded is NOT
    // a recap source — never submit on the strength of a local pick.
    if (state.recapUploadPending) {
        showStudioToast(t("toast_recap_not_uploaded"), 'warning');
        return;
    }
    // Batch DF: an uploaded file with no audio track can never be
    // recapped — block here with the exact reason (the CTA is already
    // disabled; this covers every other path to this handler, and the
    // server refuses it too, before any credit moves).
    if (state.recapNoAudio) {
        showStudioToast(t("recap_no_audio"), 'warning');
        return;
    }
    // Batch BV: same rule for the own-voice sample — a selected
    // own-voice card whose upload is still in flight (or failed)
    // must not silently submit as a persona job.
    if (state.recapOwnVoiceSelected && state.recapTtsEngine === "voxcpm"
        && state.recapOwnVoiceStatus !== "ready") {
        showStudioToast(t("recap_own_voice_pending_error"), 'warning');
        return;
    }
    const urlInput = document.getElementById("recap-url-input");
    let url = state.uploadedVideoPath || (urlInput ? urlInput.value.trim() : "");
    if (!state.uploadedVideoPath && url) {
        const urlMatch = url.match(/https?:\/\/[^\s]+/i);
        if (urlMatch) url = urlMatch[0];
    }
    if (!url) {
        showStudioToast(t("toast_empty_url"), 'warning');
        return;
    }

    haptic("heavy");
    const btn = document.getElementById("btn-start-recap");

    // Enforce the per-job cap: free 2 min, VIP 10 min, admins exempt.
    // A >10 min VIP source is NOT blocked: it takes the split path
    // (auto-enable split; the server segments the source into parts).
    const maxRecapSec = recapNormalMaxSeconds();
    if (state.recapSourceDuration && state.recapSourceDuration > maxRecapSec
        && state.user?.is_vip && !state.user?.is_admin) {
        enableRecapSplitForLongVideo();
        notifyRecapSplitAutoEnabled();
    }
    if (state.recapSourceDuration && state.recapSourceDuration > maxRecapSec
        && !state.user?.is_vip && !state.user?.is_admin) {
        const m = Math.floor(maxRecapSec / 60);
        showStudioToast(tf("toast_recap_over_limit", {
            tier: state.user?.is_vip ? "VIP" : t("tier_free_label"),
            m,
            hint: t(state.user?.is_vip ? "toast_recap_over_hint_vip" : "toast_recap_over_hint_free"),
        }), 'warning', 6000);
        return;
    }

    // R40 item 10: a segmented recap charges 1 credit PER PART — show
    // the estimated parts × cost in a pre-start confirmation so the
    // total is never a surprise (the same estimate as the split-cost
    // hint by the toggle). One-shot flag: the confirm's callback
    // re-enters this function and proceeds.
    if (!state._recapCostConfirmed && !state.user?.is_admin) {
        const estCr = _fmtCreditNum(state._lastStep4QuoteCredits || 1);
        const voiceNote = (state.recapTtsEngine === "voxcpm")
            ? ` + ~${_fmtCreditNum(Math.max(0.1, Math.round((getRecapEstVoiceSeconds() / 60) * 100) / 100))}m Voice`
            : "";
        showStudioConfirm(tf("recap_cost_confirm", { credits: estCr, voiceNote }), () => {
            state._recapCostConfirmed = true;
            handleStartRecap();
        });
        return;
    }
    state._recapCostConfirmed = false;

    if (btn) btn.disabled = true;

    const progressBox = document.getElementById("recap-progress-box");
    const resultBox = document.getElementById("recap-result-box");
    const progressFill = document.getElementById("recap-progress-fill");
    const statusMsg = document.getElementById("recap-status-msg");
    const stepText = document.getElementById("recap-step-text");

    if (resultBox) resultBox.classList.add("hidden");
    const failedBoxOnStart = document.getElementById("recap-failed-box");
    if (failedBoxOnStart) failedBoxOnStart.classList.add("hidden");
    // Batch AC: the studio survives submit — no scroll-away and the
    // detail progress box stays closed; a slim inline status line in
    // the studio plus the global pill carry the job from here. The
    // canvas, preview and every control value are left untouched.
    showStudioInlineStatus("recap", t("recap_stage1_status"));
    if (progressFill) paintProgressBarFill(progressFill, { status: "QUEUED", progress_percent: 0 });
    setRecapStage("1/4", null);
    // R40 deck F-1/F-2: the first status/ETA lines named the vendor
    // engine ("Groq Cloud Whisper Turbo", "Groq Turbo Engine + 1080p
    // Cinema Compositing") — vendor names out, structure kept.
    if (statusMsg) statusMsg.innerText = t("recap_stage1_status");

    // Batch CB Item 3: the hardcoded submit-time estimate
    // ("1.5 - 2.5" / "3 - 4" minutes for every job) is GONE — it was
    // fiction unrelated to the job's shape. The honest range the
    // wizard fetched for Step 4 rides the submit response
    // (eta_range_seconds); the store renderer + detail poller paint
    // the live lines from real server estimates from here on, and
    // when no estimate exists the line simply carries no number.

    // Batch AC duplicate-submit guard, engaged at the last moment
    // before the request (after every auto-adjustment, e.g. the
    // split auto-enable, has settled the real configuration): an
    // in-flight double-tap is one submit, and an UNCHANGED
    // configuration cannot resubmit after a successful submit.
    if (!studioSubmitBegin("recap")) {
        if (btn) btn.disabled = false;
        return;
    }

    try {
        const sliderBlurY = document.getElementById("slider-blur-y");
        const sliderBlurH = document.getElementById("slider-blur-h");
        const sliderBlurX = document.getElementById("slider-blur-x");
        const sliderBlurW = document.getElementById("slider-blur-w");
        const sliderBlurStrength = document.getElementById("slider-blur-strength");
        const sliderSubY = document.getElementById("slider-sub-y");
        const sliderSubX = document.getElementById("slider-sub-x");
        const sliderSubW = document.getElementById("slider-sub-w");

        const payload = {
            url: url,
            title: state.recapVideoTitle || null,
            voice_slug: state.recapVoice || "komin",
            story_style: state.recapStoryStyle || "viral",
            ratio: state.recapRatio || "9:16",
            ratio_fill: state.recapRatioFill || "blur",
            crop_top: state.recapCropTop || 0,
            crop_bottom: state.recapCropBottom || 0,
            crop_left: state.recapCropLeft || 0,
            crop_right: state.recapCropRight || 0,
            mirror_video: state.recapMirror !== false,
            zoom_factor: state.recapZoom || 1.0,
            voice_speed: state.recapSpeed || "+12%",
            blur_subtitles: state.recapBlurSubs !== false,
            blur_y: sliderBlurY ? parseInt(sliderBlurY.value) : 85,
            blur_h: sliderBlurH ? parseInt(sliderBlurH.value) : 7,
            // Batch AG: horizontal blur geometry, percents of the
            // picture width — the engine converts them through the
            // same single conversion point as blur_y/blur_h.
            blur_x: sliderBlurX ? parseInt(sliderBlurX.value) : 4,
            blur_w: sliderBlurW ? parseInt(sliderBlurW.value) : 92,
            // Batch AA: the explicit-set marker rides along — the
            // engine treats a marked box as authoritative. Batch
            // DD: a blur-box LIST supersedes it (list sent below).
            blur_box_explicit: !!state.recapBlurBoxTouched && !(Array.isArray(state.recapBlurBoxes) && state.recapBlurBoxes.length),
            blur_strength: sliderBlurStrength ? parseInt(sliderBlurStrength.value) : 18,
            blur_auto_detect: state.recapBlurAutoDetect !== false,
            burn_subtitles: state.recapBurnSubs !== false,
            // Batch AK: the slider is the bottom-edge margin; sub_y
            // is no longer sent (the server default stands and the
            // margin wins in the engine when present).
            sub_bottom_margin: (state.recapSubPosBand === "top" || state.recapSubPosBand === "center") ? null : (sliderSubY ? parseInt(sliderSubY.value) : (state.recapSubMargin != null ? state.recapSubMargin : 5)),
            // Batch DD: position band Top/Centre ride sub_y
            // (undefined = omitted = the Bottom margin path).
            sub_y: (state.recapSubPosBand === "top") ? 15 : (state.recapSubPosBand === "center") ? 50 : undefined,
            // Batch AG: subtitle centre X + wrap width (picture
            // width percents), mirroring sub_y's centre semantics.
            sub_x: sliderSubX ? parseInt(sliderSubX.value) : 50,
            sub_w: sliderSubW ? parseInt(sliderSubW.value) : 92,
            sub_style: state.recapSubStyle || "yellow",
            sub_font_size: state.recapFontSize || 16,
            sub_font: state.recapSubFont || "padauk",
            sub_lines: state.recapLines || 1,
            include_logo: state.recapLogo !== false,
            logo_pos: state.recapLogoPos || "top-right",
            logo_scale: Number(state.recapLogoScale || 0.16),
            logo_opacity: Number(state.recapLogoOpacity || 0.70),
            // Batch DD: protection level, free style controls,
            // custom font handle and the blur-box list.
            ...ddConfigExtras(),
            duration_seconds: state.recapSourceDuration || null,
            script_review: !!((document.getElementById("recap-script-review-toggle") || {}).checked),
            source_language: state.recapSourceLanguage || "auto",
            render_quality: state.recapRenderQuality || "standard",
            tts_engine: state.recapTtsEngine || "voxcpm",
            gemini_voice: (state.recapTtsEngine === "gemini" && state.recapGeminiVoice) ? state.recapGeminiVoice : null,
            // Batch BV: own-voice handle — only on the clone engine
            // and only once the upload has completed (the submit
            // guard above blocks an in-flight upload).
            voice_ref_id: (state.recapTtsEngine === "voxcpm" && state.recapOwnVoiceSelected && state.recapOwnVoiceStatus === "ready")
                ? state.recapOwnVoiceRefId
                : null,
            split_video: !!((document.getElementById("recap-split-toggle") || {}).checked),
            split_minutes: state.recapSplitMinutes || 3,
        };

        const res = await fetch("/api/recap/generate", {
            method: "POST",
            headers: getAuthHeaders(),
            body: JSON.stringify(payload),
        });

        const data = await res.json();
        if (!res.ok) {
            if (res.status === 403 && data && data.code === "INSUFFICIENT_VOICE_MINUTES") {
                state.recapInsufMinutesServer = {
                    required_seconds: Number(data.required_seconds) || 60,
                    available_seconds: Number(data.available_seconds) || 0,
                };
                paintRecapDualQuote(state._lastStep4QuoteCredits || 1);
                studioSubmitFailure("recap");
                if (progressBox) progressBox.classList.add("hidden");
                if (btn) btn.disabled = false;
                updateRecapDurationGate();
                showStudioToast(data.detail || t("recap_insuf_min_title"), "error", 6500);
                const insufEl = document.getElementById("recap-insufficient-minutes-box");
                if (insufEl) insufEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
                return;
            }
            throw new Error(data.detail || "Recap creation failed");
        }

        state.recapInsufMinutesServer = null;
        state.recapJobId = data.recap_id;
        try { localStorage.setItem("vcp_active_recap_job_id", data.recap_id); } catch (e) {}
        loadUserProfile();
        startRecapJobPolling(data.recap_id);
        // Batch AC: Start is free again immediately — the workspace
        // survives, the pill tracks this job, and a parallel submit
        // is explained in place (server concurrency rule unchanged).
        // The button stays in its disabled "already creating" state
        // for THIS configuration (guard) — it is NOT re-enabled here.
        studioSubmitSuccess("recap");
        LiveJobs.track({ id: data.recap_id, kind: "recap", status: "QUEUED", progress_percent: 0, stage_index: 0, stage_total: 4, title: state.recapVideoTitle || "" });
        // Batch AO: while tiered pricing is armed, the response's
        // credits_total (the band total the job will be charged) is
        // the quoted cost. The key is absent in the legacy world,
        // where the chain below is exactly the historical one.
        let confirmCostText = data.credits_total
            ? tf("confirm_cost_recap", { credits: data.credits_total })
            : ((data.estimated_parts && data.estimated_parts > 1)
                ? tf("confirm_cost_recap_segmented", { parts: data.estimated_parts })
                : (data.credits_charged
                    ? tf("confirm_cost_recap", { credits: data.credits_charged })
                    : t("confirm_cost_none")));
        if (data.billing_mode === "credit_plus_voice_minutes" && Number(data.voice_seconds_held || 0) > 0) {
            const heldMin = Math.max(0.1, Math.round((Number(data.voice_seconds_held) / 60) * 10) / 10);
            confirmCostText = tf("recap_confirm_voice_hold", { credit_text: confirmCostText, min: heldMin });
        }
        // Batch CI: an accepted submit retires any stored duration
        // rejection note from an earlier attempt on this source.
        recapSubmitRejectText = "";
        updateRecapDurationGate();
        showSubmitConfirmation("recap", data.recap_id, confirmCostText);

    } catch (err) {
        studioSubmitFailure("recap");
        // Batch CI: a server DURATION-cap rejection — possible when
        // the client never learned this source's duration (some link
        // sources) — ALSO lands as a persistent note under the Step-4
        // CTA via the gate painter; the toast alone disappears in
        // seconds and leaves the user guessing. Identified by the
        // same marker mapErrorToToast's recap pass-through family
        // uses. Any other outcome clears a stale stored rejection.
        const rawErr = err && err.message ? String(err.message) : "";
        recapSubmitRejectText = rawErr.includes('ကန့်သတ်ချက် (အများဆုံး') ? rawErr : "";
        // R40 item 11: this catch only sees SUBMIT-time failures — the
        // server rejected /recap/generate before any charge (or the
        // request never landed), so the generic recap failure copy
        // (which speaks of a refund) would be false. Keep the mapper's
        // specific pass-through messages; swap only its generic
        // fallthrough for the pre-charge text.
        const mapped = mapErrorToToast(err && err.message ? err.message : err, 'recap');
        showStudioToast(mapped === t('err_recap_failed') ? t('err_recap_rejected') : mapped, 'error');
        if (progressBox) progressBox.classList.add("hidden");
        if (btn) btn.disabled = false;
        updateRecapDurationGate();
    }
}

// ── Batch BR: the recap form's progress lines render from the
// LiveJobs store — the SAME monotonic-merged job object the progress
// pill renders (renderLiveJobsPill via liveJobStageText) — so the
// form and the pill can never show different step text for the same
// job again. (The detail poller's private response used to paint
// these lines on its own cadence, with its own clamp, and the two
// surfaces routinely sat one step apart.) The detail poller keeps
// only what the bulk endpoint does not carry: result/download URLs,
// the script editor, failure/refund handling — plus the ETA extra,
// which it caches below for this renderer. Monotonicity comes from
// liveJobsMergeJob, the one merge both surfaces share.
let recapFormJobId = null;      // the job the detail form tracks
let recapFormEtaRange = null;   // ETA range extra, fed by the detail poller (Batch CB)
let recapFormEtaOverrun = false; // overrun flag, fed by the detail poller (Batch CB)
let recapFormNotice = null;     // a poller health notice owning the status line
let recapFormStoreSubscribed = false;

function renderRecapFormProgress(job) {
    if (!job || job.id !== recapFormJobId) return;
    if (recapFormNotice) return; // a poller notice owns the status line
    const statusMsg = document.getElementById("recap-status-msg");
    const stepText = document.getElementById("recap-step-text");
    const progressFill = document.getElementById("recap-progress-fill");
    // Batch CB Item 4: while the job overruns its initial estimate,
    // the status line (here AND the inline line below) switches to
    // the honest overrun copy — the stage + elapsed come from this
    // same store job, the flag from the detail poller's ETA extra.
    const formStatusText = (recapFormEtaOverrun && !liveJobsIsTerminal(job.status))
        ? liveJobOverrunText(job)
        : liveJobStageText(job);
    if (statusMsg) statusMsg.innerText = formStatusText;
    // Batch BT: the studio INLINE status line is the surface the
    // user actually watches after a 1-Click submit (Batch AC keeps
    // the detail box closed) — but showStudioInlineStatus wrote it
    // exactly once at submit (the stage-1 text) and nothing ever
    // updated it again, so it sat on "[1/4] …" for the whole run
    // and even after the job finished. Paint it from this same
    // store job with the same text source, and keep the line
    // visible while a job is tracked. Terminal jobs paint their
    // outcome text exactly like the pill; the result/failed boxes
    // stay the detail poller's business. (The recapFormNotice
    // early-return above covers this line too — a parked poller
    // health notice owns the status text, inline included.)
    const inlineLine = document.getElementById("recap-inline-status");
    const inlineText = document.getElementById("recap-inline-status-text");
    if (inlineText) inlineText.innerText = formStatusText;
    if (inlineLine) inlineLine.classList.remove("hidden");
    if (progressFill) {
        paintProgressBarFill(progressFill, job);
    }
    if (stepText) {
        if (liveJobsIsTerminal(job.status)) {
            stepText.innerText = "";
        } else if (job.status === "WAITING_CHOICE") {
            stepText.innerText = `${t("recap_waiting_choice_detail")} · ${t("recap_waiting_choice_bot_hint")}`;
        } else {
            const stepBits = [`${t("recap_step_label")} ${job.stage_index || 0}/${job.stage_total || 4}`];
            if (typeof job.queue_position === "number" && job.queue_position > 0) {
                stepBits.push(tf("recap_queue_ahead", { count: job.queue_position }));
            }
            // Batch CB: the ETA bit is the remaining RANGE (no
            // 1-minute floor); during an overrun the status line
            // carries the honest copy and the bit stays off rather
            // than contradicting it.
            const etaText = recapFormEtaOverrun ? "" : formatEtaRangeText(recapFormEtaRange, "remaining");
            if (etaText) {
                stepBits.push(etaText);
            }
            if (stepBits.length === 1) stepBits.push(t("queue_notice_recap"));
            stepText.innerText = stepBits.join(" • ");
        }
    }
    setRecapStage(`${job.stage_index || 0}/${job.stage_total || 4}`, job.status);
    if (job.progress_stage === "sync" || job.progress_stage === "png") setRecapStage("4/4", job.status);
}

function ensureRecapFormStoreSubscription() {
    if (recapFormStoreSubscribed) return;
    recapFormStoreSubscribed = true;
    LiveJobs.subscribe((job, opts) => {
        if (!job || job.kind !== "recap") return;
        if (opts && opts.removed) {
            // The server no longer reports the tracked job at all:
            // the form lines go empty (the detail poller's own
            // job-gone path owns the toast + box from here).
            if (job.id === recapFormJobId) {
                const statusMsg = document.getElementById("recap-status-msg");
                const stepText = document.getElementById("recap-step-text");
                if (statusMsg) statusMsg.innerText = "";
                if (stepText) stepText.innerText = "";
                // Batch BT: the inline line mirrors the detail
                // lines here too — the store no longer reports the
                // job, so no surface may keep a stale stage text
                // frozen on it (that frozen stage text is the very
                // defect this batch fixes). The line element itself
                // stays in place, emptied like the detail lines;
                // the poller's job-gone path owns the toast + box.
                const inlineText = document.getElementById("recap-inline-status-text");
                if (inlineText) inlineText.innerText = "";
            }
            return;
        }
        renderRecapFormProgress(job);
    });
}

function startRecapJobPolling(jobId) {
    if (state.recapPollTimer) clearInterval(state.recapPollTimer);

    const progressBox = document.getElementById("recap-progress-box");
    const resultBox = document.getElementById("recap-result-box");
    // Batch BR: no progressFill handle here any more — the fill is
    // painted by the store renderer (renderRecapFormProgress).
    const statusMsg = document.getElementById("recap-status-msg");
    const stepText = document.getElementById("recap-step-text");
    const btn = document.getElementById("btn-start-recap");

    // Batch AC: the detail view opens BY CHOICE only — polling never
    // evicts the studio, and the poller never locks Start (a parallel
    // submit is explained in place by handleStartRecap).
    if (progressBox && state.recapDetailOpen) progressBox.classList.remove("hidden");
    const failedBoxOnPoll = document.getElementById("recap-failed-box");
    if (failedBoxOnPoll) failedBoxOnPoll.classList.add("hidden");

    // Batch BR: this poller's job is the form's tracked job — the
    // store subscription paints its progress lines from here on
    // (an already-known store job paints immediately).
    recapFormJobId = jobId;
    recapFormEtaRange = null;
    recapFormEtaOverrun = false;
    recapFormNotice = null;
    ensureRecapFormStoreSubscription();
    const storeJobOnStart = LiveJobs.jobs.get(jobId);
    if (storeJobOnStart) renderRecapFormProgress(storeJobOnStart);

    // Poll health bookkeeping (hunt #7): this loop used to swallow
    // every failure silently (non-OK -> return, throw -> console.warn)
    // and had NO cap, so a dead connection or a long server outage
    // left a frozen progress box for the whole session — and via
    // recapJobIsActive() it blocked every future recap submit too.
    // Now: consecutive failures surface a "reconnecting" cue on the
    // status line after 3 strikes; a job the server no longer knows
    // (404 three polls in a row — purged, or never persisted) stops
    // the loop and releases the UI; and a hard cap stops the loop
    // after 60 minutes while the job itself keeps running
    // server-side (the bot notifies on completion, /user/me resumes
    // this poller on the next profile load, History has the result).
    let recapPollFailures = 0;
    let recapPollNotFound = 0;
    let recapPollTicks = 0;
    // Batch BR: the Batch AC monotonic guard that used to live here
    // (recapPollMaxStep / recapPollMaxPercent clamping this poller's
    // private response) is gone — the displayed stage/percent now
    // come from the LiveJobs store, whose merge (liveJobsMergeJob)
    // is monotonic by construction for every surface at once.
    // Tick budget in 2s units: 1800 = the 60-minute overall cap. After
    // the backoff below each 5s tick counts 2.5 units, so the cap (and
    // every stop condition) is unchanged.
    const RECAP_POLL_MAX_TICKS = 1800;

    function noteRecapPollFailure() {
        recapPollFailures++;
        if (recapPollFailures === 3 && statusMsg) {
            // Batch BR: the notice also flags the store renderer to
            // leave the status line alone until a tick succeeds
            // again (the success path clears recapFormNotice).
            recapFormNotice = t("poll_reconnecting");
            statusMsg.innerText = recapFormNotice;
        }
    }

    function stopRecapPollingJobGone() {
        clearInterval(state.recapPollTimer);
        state.recapPollTimer = null;
        try { localStorage.removeItem("vcp_active_recap_job_id"); } catch (e) {}
        if (btn) btn.disabled = false;
        if (progressBox) progressBox.classList.add("hidden");
        showStudioToast(t("poll_job_gone"), 'error', 6000);
    }

    // Polling efficiency (audit fix 2026-11-03): ticks are skipped
    // while the tab is hidden, and after the first 60s of polling the
    // interval backs off from 2s to 5s (the callback is a named const
    // so it can be re-armed at the slower interval).
    let recapPollBackedOff = false;
    const recapPollTick = async () => {
        if (document.hidden) return;
        recapPollTicks += recapPollBackedOff ? 2.5 : 1;
        if (!recapPollBackedOff && recapPollTicks >= 30) {
            recapPollBackedOff = true;
            clearInterval(state.recapPollTimer);
            state.recapPollTimer = setInterval(recapPollTick, 5000);
        }
        if (recapPollTicks > RECAP_POLL_MAX_TICKS) {
            clearInterval(state.recapPollTimer);
            state.recapPollTimer = null;
            // Batch BR: the notice keeps owning the status line —
            // the store renderer must not paint over it now that
            // this poller has stopped.
            recapFormNotice = t("poll_background_notice");
            if (statusMsg) statusMsg.innerText = recapFormNotice;
            showStudioToast(t("poll_background_notice"), 'info', 6000);
            return;
        }
        try {
            const res = await fetch(`/api/recap/${jobId}/status`, {
                headers: getAuthHeaders(),
            });
            if (res.status === 404) {
                recapPollNotFound++;
                if (recapPollNotFound >= 3) stopRecapPollingJobGone();
                return;
            }
            if (!res.ok) { noteRecapPollFailure(); return; }

            const data = await res.json();
            recapPollFailures = 0;
            recapPollNotFound = 0;
            // Batch BR: a successful tick hands the progress lines
            // back to the store renderer (clearing any health notice
            // a failure streak had parked on the status line). The
            // detail payload's only remaining contribution to those
            // lines is the ETA extra the bulk endpoint does not
            // carry (Batch L) — cache it, then repaint FROM THE
            // STORE JOB, never from this response, so the step/stage
            // text is the pill's own merged values by construction.
            recapFormNotice = null;
            // Batch CB: the ETA extra is now the remaining RANGE +
            // the overrun flag (the bulk endpoint carries its own
            // copy for the pill/sheet; the detail payload's richer
            // estimator feeds this form).
            recapFormEtaRange = Array.isArray(data.eta_range_seconds) ? data.eta_range_seconds : null;
            recapFormEtaOverrun = !!data.eta_overrun;
            const storeJobForPaint = LiveJobs.jobs.get(jobId);
            if (storeJobForPaint) renderRecapFormProgress(storeJobForPaint);
            // Batch AK (Addition C): on a terminal status the
            // progress lines must not keep the last in-progress
            // step — replace them with the outcome label before the
            // branch handlers run. (Batch BR: this write stays, and
            // stays honest, because it uses the identical text
            // source as the store renderer — liveJobStageText.)
            if (["SUCCESS", "FAILED", "CANCELLED"].includes(data.status)) {
                if (statusMsg) statusMsg.innerText = liveJobStageText({ status: data.status, kind: "recap" });
                if (stepText) stepText.innerText = "";
            }

            if (data.status !== "SCRIPT_READY") {
                if (state.recapScriptReadyJobId === jobId) state.recapScriptReadyJobId = null;
            }
            if (data.status === "WAITING_CHOICE") {
                setRecapStage(null, "WAITING_CHOICE");
                if (statusMsg) statusMsg.innerText = t("live_job_status_waiting_choice");
                if (stepText) stepText.innerText = `${t("recap_waiting_choice_detail")} · ${t("recap_waiting_choice_bot_hint")}`;
                const cancelBtn = document.getElementById("btn-recap-cancel");
                if (cancelBtn) {
                    cancelBtn.classList.remove("hidden");
                    cancelBtn.disabled = false;
                }
            } else if (data.status === "SCRIPT_READY") {
                clearInterval(state.recapPollTimer);
                state.recapPollTimer = null;
                // The job is NOT done: it waits for script review and
                // still counts as active (E8) — Start stays disabled
                // until the user renders it.
                state.recapScriptReadyJobId = jobId;
                setRecapStage(null, "SCRIPT_READY");
                if (progressBox) progressBox.classList.add("hidden");
                showStudioToast(t("toast_script_ready"), 'info');
                openRecapScriptEditor(jobId);
            } else if (data.status === "SUCCESS") {
                if (data.fallback_used) { try { showStudioToast(tf("recap_fallback_note", { count: (data.fallback_segments || []).length || 1 }), 'info'); } catch (e) {} }
                clearInterval(state.recapPollTimer);
                state.recapPollTimer = null;
                try { localStorage.removeItem("vcp_active_recap_job_id"); } catch (e) {}
                if (btn) btn.disabled = false;
                if (progressBox) progressBox.classList.add("hidden");
                if (resultBox) {
                    resultBox.classList.remove("hidden");
                    refreshStickyCtas();
                }

                const btnTg = document.getElementById("btn-recap-send-telegram");
                if (btnTg) {
                    btnTg.disabled = false;
                    btnTg.classList.remove("sent");
                    btnTg.innerHTML = `<span class="btn-icon">📱</span> <span>${t("btn_send_recap_telegram")}</span>`;
                }
                applyTelegramSendable("recap", data.telegram_sendable);

                // Setup video player (round-6 P1: blob URL — a raw
                // API src cannot carry the init-data header and 401s;
                // R40: the signed stream URL solves that properly for
                // video — see setRecapPlayerSource)
                const player = document.getElementById("recap-result-player");
                if (player && data.download_urls?.video) {
                    setRecapPlayerSource(player, data.download_urls);
                    player.classList.remove("hidden");
                }

                // Setup deliverable buttons
                const btnVideo = document.getElementById("btn-recap-dl-video");
                const btnAudio = document.getElementById("btn-recap-dl-audio");
                const btnSrt = document.getElementById("btn-recap-dl-srt");
                const btnScript = document.getElementById("btn-recap-dl-script");

                if (btnVideo && data.download_urls?.video) {
                    btnVideo.setAttribute("data-dl-url", data.download_urls.video);
                    btnVideo.setAttribute("data-dl-name", brandedDlName("Recap", "mp4", null, jobId));
                }
                if (btnAudio && data.download_urls?.audio) {
                    btnAudio.setAttribute("data-dl-url", data.download_urls.audio);
                    btnAudio.setAttribute("data-dl-name", brandedDlName("RecapAudio", "mp3", null, jobId));
                }
                if (btnSrt && data.download_urls?.srt) {
                    btnSrt.setAttribute("data-dl-url", data.download_urls.srt);
                    btnSrt.setAttribute("data-dl-name", brandedDlName("Subtitles", "srt", null, jobId));
                }
                if (btnScript && data.download_urls?.script) {
                    btnScript.setAttribute("data-dl-url", data.download_urls.script);
                    btnScript.setAttribute("data-dl-name", brandedDlName("Script", "txt", null, jobId));
                }
                syncRecapAiThumbnailUI(data, jobId);

                // Split parts (optional Video Splitter deliverables)
                const partsWrap = document.getElementById("recap-parts-wrap");
                const partsList = document.getElementById("recap-parts-list");
                if (partsWrap && partsList) {
                    const parts = (Array.isArray(data.parts) && data.parts.length)
                        ? data.parts
                        : (Array.isArray(data.download_urls?.parts) ? data.download_urls.parts : []);
                    partsList.innerHTML = "";
                    if (parts.length) {
                        partsWrap.classList.remove("hidden");
                        parts.forEach(p => {
                            const a = document.createElement("a");
                            a.href = "#";
                            a.setAttribute("data-dl-url", p.url);
                            if (p.n) a.setAttribute("data-dl-name", brandedDlName("Recap", "mp4", null, jobId, p.n));
                            a.className = "btn mini-btn secondary-btn";
                            a.setAttribute("download", "");
                            a.textContent = "🎬 " + (p.label || ("Part " + p.n));
                            partsList.appendChild(a);
                        });
                    } else {
                        partsWrap.classList.add("hidden");
                    }
                    const noteEl = document.getElementById("recap-split-note");
                    if (noteEl) {
                        let note = "";
                        if (data.split_error) note = t("recap_split_failed");
                        else if (data.split_note) note = data.split_note;
                        // Segmented jobs: show the exact parts count and
                        // the credits the job actually used (1 per part).
                        if (!data.split_error && data.parts_count > 1 && data.credits_charged) {
                            const costLine = tf("recap_segmented_cost", { parts: data.parts_count, credits: data.credits_charged });
                            note = note ? (note + " " + costLine) : costLine;
                        }
                        noteEl.textContent = note;
                        noteEl.classList.toggle("hidden", !note);
                    }
                    // Blur no-coverage warning (blur safefix 2026-11-03):
                    // the job finished, but the engine found no source
                    // subtitle band to blur — tell the user to check
                    // the blur box position.
                    const blurWarnEl = document.getElementById("recap-blur-warning");
                    if (blurWarnEl) {
                        const hasBlurWarn = Array.isArray(data.warnings) && data.warnings.includes("blur_no_source_band");
                        blurWarnEl.textContent = hasBlurWarn ? t("warn_blur_no_source_band") : "";
                        blurWarnEl.classList.toggle("hidden", !hasBlurWarn);
                    }
                    // Batch O: persistent fallback disclosure on the
                    // result card (the completion toast alone was easy
                    // to miss). The status payload carries the total
                    // segment count, so a job whose EVERY segment fell
                    // back gets the full-fallback wording.
                    const fbNoteEl = document.getElementById("recap-fallback-note");
                    if (fbNoteEl) {
                        const fbCount = (data.fallback_segments || []).length;
                        const fbTotal = typeof data.segments_count === "number" ? data.segments_count : 0;
                        let fbText = "";
                        if (data.fallback_used && fbTotal > 0 && fbCount >= fbTotal) {
                            fbText = t("recap_fallback_note_all");
                        } else if (data.fallback_used) {
                            fbText = tf("recap_fallback_note", { count: fbCount || 1 });
                        }
                        fbNoteEl.textContent = fbText;
                        fbNoteEl.classList.toggle("hidden", !fbText);
                    }
                    // Batch K: every completed recap shows its receipt —
                    // the credits this job was actually charged — not
                    // only segmented jobs (whose split note above
                    // already carries the cost line).
                    const costEl = document.getElementById("recap-cost-line");
                    if (costEl) {
                        const charged = data.credits_charged;
                        const costInNote = !data.split_error && data.parts_count > 1 && charged;
                        if (typeof charged === "number" && charged > 0 && !costInNote) {
                            costEl.textContent = tf("recap_cost_charged_line", { credits: charged });
                            costEl.classList.remove("hidden");
                        } else {
                            costEl.textContent = "";
                            costEl.classList.add("hidden");
                        }
                    }
                    const vsEl = document.getElementById("recap-voice-settlement-line");
                    if (vsEl) {
                        const held = Number(data.voice_seconds_held || 0);
                        const settled = data.voice_seconds_settled;
                        const refSec = Number(data.voice_seconds_refunded || 0);
                        if (data.billing_mode === "credit_plus_voice_minutes" && typeof settled === "number") {
                            const sMin = (settled / 60).toFixed(1);
                            if (held > 0 && refSec > 0) {
                                vsEl.textContent = tf("recap_voice_settlement_used_line", {
                                    settled: sMin,
                                    held: (held / 60).toFixed(1),
                                    refunded: (refSec / 60).toFixed(1),
                                });
                            } else {
                                vsEl.textContent = tf("recap_voice_settlement_exact_line", { settled: sMin });
                            }
                            vsEl.classList.remove("hidden");
                        } else {
                            vsEl.textContent = "";
                            vsEl.classList.add("hidden");
                        }
                    }
                }

                haptic("success");
                loadUserProfile();
                loadUserHistory();

                // ONE completion surface (round-3 U3): the result box
                // above — player + download buttons, already scrolled
                // into view — is the completion surface. The floating
                // alert that used to stack a near-identical second
                // card directly below it is gone.
            } else if (data.status === "FAILED") {
                clearInterval(state.recapPollTimer);
                state.recapPollTimer = null;
                try { localStorage.removeItem("vcp_active_recap_job_id"); } catch (e) {}
                if (btn) btn.disabled = false;
                haptic("error");
                if (progressBox) progressBox.classList.add("hidden");

                const errMsg = data.error_message || "";
                // The stored error is technical text (e.g. a raw yt-dlp
                // dump): it is only pattern-matched by the mapper, never
                // displayed. Type is 'error' (this branch used to toast
                // with 'success' styling for a failure).
                // Batch K: when the server proves a refund was issued
                // for this job, the failure surface says so.
                let failToast = mapErrorToToast(errMsg, 'recap');
                if (data.credit_refunded) {
                    failToast += " " + tf("recap_refunded_line", { credits: data.credits_refunded || data.credits_charged || 1 });
                }
                const failVoiceRefSec = Number(data.voice_seconds_refunded || 0);
                if (data.voice_refunded && failVoiceRefSec > 0) {
                    failToast += " " + tf("recap_failed_voice_refund_badge", {
                        min: (failVoiceRefSec / 60).toFixed(1),
                        sec: failVoiceRefSec,
                    });
                }
                showStudioToast(failToast, 'error', 6000);
                // Batch L: the failed card is the retry surface —
                // the same curated error text as the toast (mapped,
                // never raw), the refund confirmation when proven,
                // and a one-tap retry (a NEW charged job built from
                // the stored config; the server enforces it). When
                // the server reports the source is gone
                // (can_retry === false — an upload whose file was
                // purged), the button is disabled and the honest
                // re-upload note shows instead of the charge note.
                const failedBox = document.getElementById("recap-failed-box");
                if (failedBox) {
                    failedBox.classList.remove("hidden");
                    const ferrEl = document.getElementById("recap-failed-error");
                    if (ferrEl) ferrEl.innerText = mapErrorToToast(errMsg, 'recap');
                    const frefEl = document.getElementById("recap-failed-refund");
                    if (frefEl) {
                        if (data.credit_refunded) {
                            frefEl.innerText = tf("recap_refunded_line", { credits: data.credits_refunded || data.credits_charged || 1 });
                            frefEl.classList.remove("hidden");
                        } else {
                            frefEl.classList.add("hidden");
                        }
                    }
                    const fvRefEl = document.getElementById("recap-failed-voice-refund");
                    if (fvRefEl) {
                        if (data.voice_refunded && failVoiceRefSec > 0) {
                            fvRefEl.innerText = tf("recap_failed_voice_refund_badge", {
                                min: (failVoiceRefSec / 60).toFixed(1),
                                sec: failVoiceRefSec,
                            });
                            fvRefEl.classList.remove("hidden");
                        } else {
                            fvRefEl.classList.add("hidden");
                        }
                    }
                    const retryBtn = document.getElementById("btn-recap-retry");
                    const missNote = document.getElementById("recap-retry-upload-missing");
                    const chargeNote = document.getElementById("recap-retry-note");
                    if (data.can_retry === false) {
                        if (retryBtn) retryBtn.disabled = true;
                        if (missNote) missNote.classList.remove("hidden");
                        if (chargeNote) chargeNote.classList.add("hidden");
                    } else {
                        if (retryBtn) retryBtn.disabled = false;
                        if (missNote) missNote.classList.add("hidden");
                        if (chargeNote) chargeNote.classList.remove("hidden");
                    }
                    state.recapFailedJobId = jobId;
                }
                if (errMsg.includes("Douyin") || errMsg.includes("Fresh cookies") || blockedLinkInfo(errMsg).blocked) {
                    const btnFileTab = document.getElementById("btn-source-mode-file");
                    if (btnFileTab) btnFileTab.click();
                }
                loadUserProfile();
                loadUserHistory();
            } else if (data.status === "CANCELLED") {
                // Final-audit batch M: the job was cancelled on
                // another surface (admin console, or the user in a
                // second session) — this client never learns it from
                // its own cancel helper, so the poller must stop here
                // instead of polling a dead job to the 60-minute cap.
                // The refund line is shown only when the payload
                // proves the refund.
                clearInterval(state.recapPollTimer);
                state.recapPollTimer = null;
                try { localStorage.removeItem("vcp_active_recap_job_id"); } catch (e) {}
                if (btn) btn.disabled = false;
                if (progressBox) progressBox.classList.add("hidden");
                let cancelToast = t("recap_cancelled_toast");
                if (data.credit_refunded) {
                    cancelToast += " " + tf("recap_refunded_line", { credits: data.credits_refunded || data.credits_charged || 1 });
                }
                if (data.voice_refunded && Number(data.voice_seconds_refunded || 0) > 0) {
                    const vSec = Number(data.voice_seconds_refunded);
                    cancelToast += " " + tf("recap_failed_voice_refund_badge", { min: (vSec / 60).toFixed(1), sec: vSec });
                }
                showStudioToast(cancelToast, 'info', 5000);
                loadUserProfile();
                loadUserHistory();
            }
        } catch (err) {
            console.warn("Recap polling error:", err);
            noteRecapPollFailure();
        }
    };
    state.recapPollTimer = setInterval(recapPollTick, 2000);
}

// ── Recap job actions (batch L): cancel / retry / demo ───────────────
// All three are thin clients over owner-only server endpoints; the
// server re-checks ownership, state and source availability, so these
// helpers only confirm, call, and refresh the surfaces.
async function cancelRecapJobById(jobId) {
    if (!jobId) return;
    showStudioConfirm(t("recap_cancel_confirm"), async () => {
        try {
            const res = await fetch(`/api/recap/${jobId}/cancel`, {
                method: "POST",
                headers: getAuthHeaders(),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || t("err_generic"));
            if (state.recapPollTimer) {
                clearInterval(state.recapPollTimer);
                state.recapPollTimer = null;
            }
            if (state.recapJobId === jobId) {
                try { localStorage.removeItem("vcp_active_recap_job_id"); } catch (e) {}
            }
            if (state.recapScriptReadyJobId === jobId) state.recapScriptReadyJobId = null;
            const pBox = document.getElementById("recap-progress-box");
            if (pBox) pBox.classList.add("hidden");
            const sBox = document.getElementById("recap-script-box");
            if (sBox) sBox.classList.add("hidden");
            const startBtn = document.getElementById("btn-start-recap");
            if (startBtn) startBtn.disabled = false;
            let msg = t("recap_cancelled_toast");
            if (data.credit_refunded) {
                msg += " " + tf("recap_refunded_line", { credits: data.credits_refunded || 1 });
            }
            if (data.voice_refunded && Number(data.voice_seconds_refunded || 0) > 0) {
                const vSec = Number(data.voice_seconds_refunded);
                msg += " " + tf("recap_failed_voice_refund_badge", { min: (vSec / 60).toFixed(1), sec: vSec });
            }
            showStudioToast(msg, 'info', 5000);
            loadUserProfile();
            loadUserHistory();
        } catch (err) {
            showStudioToast((err && err.message) ? err.message : t("err_recap_failed"), 'error');
        }
    });
}
window.cancelRecapJob = function(jobId) { cancelRecapJobById(jobId); };
window.cancelCurrentRecapJob = function() {
    cancelRecapJobById(state.recapScriptReadyJobId || state.recapJobId);
};

let recapRetryInFlight = false;
async function retryRecapJobById(jobId) {
    if (!jobId || recapRetryInFlight) return;
    const item = (hubHistoryCache || []).find(h => h.id === jobId);
    const isAdmin = !!(state.user?.is_admin || state.user?.role === "admin");
    const credits = (item && (item.parts_count || item.credits_charged)) || 1;
    const confirmMsg = isAdmin
        ? t("recap_retry_confirm_admin")
        : tf("recap_retry_confirm_cost", { credits });

    showStudioConfirm(confirmMsg, async () => {
        if (recapRetryInFlight) return;
        recapRetryInFlight = true;
        try {
            const res = await fetch(`/api/recap/${jobId}/retry`, {
                method: "POST",
                headers: getAuthHeaders(),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || t("err_generic"));
            switchTab("tab-recap");
            if (typeof showRecapStep === "function") showRecapStep(4);
            const failedBox = document.getElementById("recap-failed-box");
            if (failedBox) failedBox.classList.add("hidden");
            state.recapJobId = data.recap_id;
            try { localStorage.setItem("vcp_active_recap_job_id", data.recap_id); } catch (e) {}
            showStudioToast(t("recap_retry_started_toast"), 'success');
            loadUserProfile();
            startRecapJobPolling(data.recap_id);
        } catch (err) {
            showStudioToast((err && err.message) ? err.message : t("err_recap_failed"), 'error');
        } finally {
            recapRetryInFlight = false;
        }
    });
}
window.retryRecapJob = function(jobId) { retryRecapJobById(jobId); };
window.retryCurrentRecapJob = function() {
    retryRecapJobById(state.recapFailedJobId || state.recapJobId);
};

window.watchRecapDemo = async function() {
    const asset = state.recapDemoAsset || "";
    if (!asset) return;
    haptic("light");
    if (/^https?:\/\//i.test(asset)) {
        if (window.Telegram?.WebApp?.openLink) window.Telegram.WebApp.openLink(asset);
        else window.open(asset, "_blank");
        return;
    }
    // A Telegram file_id cannot play inline in the Mini App — the
    // server asks the bot to send it to the user's own chat instead.
    try {
        const res = await fetch("/api/recap/demo/send", {
            method: "POST",
            headers: getAuthHeaders(),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || t("err_generic"));
        if (data.url) {
            if (window.Telegram?.WebApp?.openLink) window.Telegram.WebApp.openLink(data.url);
            else window.open(data.url, "_blank");
        } else {
            showStudioToast(t("recap_demo_sent_toast"), 'info', 5000);
        }
    } catch (err) {
        showStudioToast((err && err.message) ? err.message : t("err_recap_failed"), 'error');
    }
};

// ── Recap Script Review / Edit (two-phase flow) ─────────────────────────
async function openRecapScriptEditor(jobId) {
    if (typeof switchTab === "function") {
        const recapTab = document.getElementById("tab-recap");
        if (!recapTab || !recapTab.classList.contains("active")) switchTab("tab-recap");
    }
    if (typeof showRecapStep === "function") showRecapStep(4);
    const box = document.getElementById("recap-script-box");
    const list = document.getElementById("recap-script-list");
    if (!box || !list) return;
    box.classList.remove("hidden");
    box.scrollIntoView({ behavior: "smooth" });
    list.innerHTML = "";
    try {
        const res = await fetch(`/api/recap/${jobId}/script`, { headers: getAuthHeaders() });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Script load failed");
        (data.segments || []).forEach((seg) => {
            const wrap = document.createElement("div");
            wrap.className = "script-seg";
            const meta = document.createElement("div");
            meta.className = "script-seg-meta";
            meta.innerText = `#${seg.index} • ${(seg.timestamp || "").toString()} • ${Number(seg.start || 0).toFixed(1)}s – ${Number(seg.end || 0).toFixed(1)}s`;
            wrap.appendChild(meta);
            if (seg.source_text) {
                const src = document.createElement("div");
                src.className = "script-seg-source";
                src.innerText = `${t("recap_script_source")}: ${seg.source_text}`;
                wrap.appendChild(src);
            }
            const ta = document.createElement("textarea");
            ta.className = "script-seg-text";
            ta.value = seg.narration || "";
            ta.dataset.index = seg.index;
            ta.dataset.original = seg.narration || "";
            wrap.appendChild(ta);
            list.appendChild(wrap);
        });
        const btnSave = document.getElementById("btn-script-save");
        if (btnSave) btnSave.onclick = () => saveRecapScript(jobId);
        const btnRender = document.getElementById("btn-script-render");
        if (btnRender) btnRender.onclick = () => renderRecapScript(jobId);
    } catch (err) {
        showErrorToast(err, 'generic');
    }
}

async function saveRecapScript(jobId) {
    const list = document.getElementById("recap-script-list");
    if (!list) return true;
    const edits = [];
    list.querySelectorAll(".script-seg-text").forEach((ta) => {
        if (ta.value !== ta.dataset.original) {
            edits.push({ index: parseInt(ta.dataset.index, 10), narration: ta.value });
        }
    });
    if (!edits.length) return true;
    try {
        const res = await fetch(`/api/recap/${jobId}/script`, {
            method: "PUT",
            headers: getAuthHeaders(),
            body: JSON.stringify({ segments: edits }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Script save failed");
        list.querySelectorAll(".script-seg-text").forEach((ta) => { ta.dataset.original = ta.value; });
        showStudioToast(t("toast_script_saved"), 'success');
        if (data.warnings && Object.keys(data.warnings).length) {
            showStudioToast(t("toast_script_warnings"), 'warning');
        }
        return true;
    } catch (err) {
        showErrorToast(err, 'generic');
        return false;
    }
}

async function renderRecapScript(jobId) {
    const saved = await saveRecapScript(jobId);
    if (!saved) return;
    try {
        const res = await fetch(`/api/recap/${jobId}/render`, {
            method: "POST",
            headers: getAuthHeaders(),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Render start failed");
        const box = document.getElementById("recap-script-box");
        if (box) box.classList.add("hidden");
        // Review is over: the job is back to polling, and the poll
        // timer alone governs "active" from here (E8).
        state.recapScriptReadyJobId = null;
        showStudioToast(t("recap_processing_msg") || "Processing...", 'info');
        startRecapJobPolling(jobId);
    } catch (err) {
        showErrorToast(err, 'generic');
    }
}

// ── Downloader Tab ─────────────────────────────────────────────────────────
let dlCurrent = null; // { url, has_subtitles } from the last extract

// P1 (premium round 2026-11-02): the Downloader extract failure
// state is PERSISTENT — the error toast fades after a few seconds
// and the tab used to settle back to a silent idle that reads as
// "still working". The box stays until the next attempt or success.
function dlShowExtractError() {
    const box = document.getElementById("dl-error-box");
    if (box) box.classList.remove("hidden");
    if (typeof setSectionState === "function") setSectionState("dl-extract-stage", "error");
}

function dlHideExtractError() {
    const box = document.getElementById("dl-error-box");
    if (box) box.classList.add("hidden");
    const stage = document.getElementById("dl-extract-stage");
    if (stage && stage.getAttribute("data-ui-state") === "error" && typeof setSectionState === "function") {
        setSectionState(stage, "empty");
    }
}

// Journey audit 2026-11-02: the result card outlived its link —
// clearing the input left a stale card (with live download buttons)
// on screen. One helper clears card + delivery rows + state; it runs
// when a new extraction starts and whenever the input is emptied.
function dlClearResult() {
    const card = document.getElementById("dl-result-card");
    if (card) card.classList.add("hidden");
    const actions = document.getElementById("dl-delivery-actions");
    if (actions) actions.innerHTML = "";
    dlCurrent = null;
    if (typeof setSectionState === "function") setSectionState("dl-extract-stage", "empty");
}

function dlSetSrtCardState(hasSubtitles) {
    const card = document.getElementById("btn-dl-fmt-srt");
    const desc = document.getElementById("dl-fmt-srt-desc");
    if (!card || !desc) return;
    if (hasSubtitles === false) {
        card.classList.add("format-card-unavailable");
        desc.innerText = t("dl_no_subtitles");
    } else {
        card.classList.remove("format-card-unavailable");
        desc.innerText = t("fmt_sub_desc");
    }
}

async function dlDownloadFile(downloadUrl, filename, btn) {
    if (!downloadUrl) return;
    const original = btn ? btn.innerHTML : "";
    if (btn) { btn.disabled = true; btn.innerHTML = `<span>${t("dl_preparing")}</span>`; }
    try {
        if (typeof urlCarriesStreamToken === "function" && urlCarriesStreamToken(downloadUrl) && window.Telegram?.WebApp?.openLink) {
            const absUrl = downloadUrl.startsWith("http") ? downloadUrl : (window.location.origin + downloadUrl);
            let opened = false;
            try {
                window.Telegram.WebApp.openLink(absUrl);
                opened = true;
            } catch (e) { /* fall through to blob fetch */ }
            if (opened) {
                haptic("success");
                return;
            }
        }
        const fetcher = typeof fetchWithTimeout === "function" ? fetchWithTimeout : fetch;
        const res = await fetcher(downloadUrl, { headers: getAuthHeaders() }, 120000);
        if (!res.ok) throw new Error("Download failed");
        const blob = await res.blob();
        const objUrl = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = objUrl;
        a.download = filename || "download";
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(objUrl), 4000);
        haptic("success");
    } catch (err) {
        showErrorToast(err, 'download');
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = original; }
    }
}

async function dlSendTelegram(downloadId, btn) {
    const original = btn ? btn.innerHTML : "";
    if (btn) { btn.disabled = true; btn.innerHTML = `<span>${t("dl_preparing")}</span>`; }
    try {
        const res = await fetch(`/api/downloader/${downloadId}/send-telegram`, {
            method: "POST",
            headers: getAuthHeaders(),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            if (data.detail === "file_too_large_telegram") throw new Error(t("dl_too_large_telegram"));
            throw new Error(data.detail || "Failed to send to Telegram");
        }
        haptic("success");
        showStudioToast(t("toast_telegram_sent"), 'success');
    } catch (err) {
        showErrorToast(err, 'send');
    } finally {
        if (btn) { btn.disabled = false; btn.innerHTML = original; }
    }
}

function formatDlSize(bytes) {
    // R39: prepared-file cards showed "0.0 MB" for small files
    // (32768 B rendered through (b/1048576).toFixed(1)). Proper
    // units: <1 MB in whole KB, MB/GB with 1–2 decimals above that.
    if (bytes == null || isNaN(bytes)) return "?";
    if (bytes >= 1024 * 1024 * 1024) return (bytes / (1024 * 1024 * 1024)).toFixed(2) + " GB";
    if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MB";
    if (bytes >= 1024) return Math.round(bytes / 1024) + " KB";
    return bytes + " B";
}

function dlRenderDeliveryRow(data) {
    const box = document.getElementById("dl-delivery-actions");
    if (!box) return;
    const rowId = `dl-delivery-${data.format}`;
    const old = document.getElementById(rowId);
    if (old) old.remove();
    const icon = data.format === "video" ? "🎬" : (data.format === "mp3" ? "🎵" : "📝");
    const row = document.createElement("div");
    row.className = "dl-delivery-row";
    row.id = rowId;
    const label = document.createElement("span");
    label.className = "dl-delivery-label";
    label.innerText = `${icon} ${data.filename} · ${formatDlSize(data.size_bytes)}`;
    const btnDlFile = document.createElement("button");
    btnDlFile.type = "button";
    btnDlFile.className = "btn mini-btn secondary-btn";
    btnDlFile.innerText = t("btn_dl_download");
    btnDlFile.addEventListener("click", () => dlDownloadFile(data.download_url, data.filename, btnDlFile));
    row.appendChild(label);
    row.appendChild(btnDlFile);
    if (data.telegram_sendable) {
        const btnTg = document.createElement("button");
        btnTg.type = "button";
        btnTg.className = "btn mini-btn secondary-btn";
        btnTg.innerText = t("btn_dl_send_telegram");
        btnTg.addEventListener("click", () => dlSendTelegram(data.download_id, btnTg));
        row.appendChild(btnTg);
    } else {
        const note = document.createElement("span");
        note.className = "dl-delivery-note";
        note.innerText = t("dl_too_large_telegram");
        row.appendChild(note);
    }
    box.appendChild(row);
}

async function dlFetchFormat(fmt, cardBtn) {
    if (!dlCurrent || !dlCurrent.url) {
        showStudioToast(t("dl_fetch_first"), 'warning');
        return;
    }
    if (fmt === "srt" && dlCurrent.has_subtitles === false) {
        showStudioToast(t("dl_no_subtitles"), 'warning');
        return;
    }
    haptic("medium");
    const descEl = cardBtn ? cardBtn.querySelector(".fmt-desc") : null;
    const originalDesc = descEl ? descEl.innerText : "";
    if (cardBtn) cardBtn.disabled = true;
    if (descEl) descEl.innerText = t("dl_preparing");
    try {
        const res = await fetchWithTimeout("/api/downloader/fetch", {
            method: "POST",
            headers: getAuthHeaders(),
            body: JSON.stringify({ url: dlCurrent.url, format: fmt }),
        }, 240000);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            if (data.detail === "no_subtitles_available") {
                dlCurrent.has_subtitles = false;
                dlSetSrtCardState(false);
                throw new Error(t("dl_no_subtitles"));
            }
            throw new Error(data.detail || "Download failed");
        }
        dlRenderDeliveryRow(data);
        haptic("success");
        showStudioToast(t("dl_ready"), 'success');
    } catch (err) {
        showErrorToast(err, 'download');
    } finally {
        if (cardBtn) cardBtn.disabled = false;
        if (descEl && !(fmt === "srt" && dlCurrent && dlCurrent.has_subtitles === false)) {
            descEl.innerText = originalDesc;
        }
    }
}

function setupDownloader() {
    const btnDl = document.getElementById("btn-extract-dl");
    const resultCard = document.getElementById("dl-result-card");
    if (btnDl && resultCard) {
        btnDl.addEventListener("click", async () => {
            const urlInput = document.getElementById("dl-url-input");
            const url = urlInput ? urlInput.value.trim() : "";
            if (!url) {
                showStudioToast(t("toast_empty_url"), 'warning');
                return;
            }
            haptic("medium");
            btnDl.disabled = true;
            dlHideExtractError();
            dlClearResult();
            if (typeof setSectionState === "function") setSectionState("dl-extract-stage", "loading");
            const dlBtnSpan = btnDl.querySelector("span");
            const dlBtnOriginal = dlBtnSpan ? dlBtnSpan.innerText : "";
            if (dlBtnSpan) dlBtnSpan.innerText = t("recap_preview_loading");
            try {
                const res = await fetchWithTimeout("/api/downloader/extract", {
                    method: "POST",
                    headers: getAuthHeaders(),
                    body: JSON.stringify({ url: url }),
                }, 60000);
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail || t("dl_extract_failed_title"));

                // Honesty gate (journey audit 2026-11-02): an
                // unverified payload — or one with NO real formats —
                // must never render a result card with download
                // buttons (a generic web page used to get a plausible
                // "Web Media" card + working buttons). Route it to the
                // persistent error box, exactly like a failed extract.
                if (data.metadata_verified === false || !(data.formats_available || []).length) {
                    dlClearResult();
                    dlShowExtractError();
                    showStudioToast(t("dl_extract_failed_title"), 'error');
                    return;
                }

                // Populate result info header
                const dlThumb = document.getElementById("dl-result-thumb");
                const dlTitle = document.getElementById("dl-result-title");
                const dlPlatform = document.getElementById("dl-result-platform");
                const dlUploader = document.getElementById("dl-result-uploader");
                const dlDuration = document.getElementById("dl-result-duration");

                if (dlTitle) dlTitle.innerText = data.title || t("dl_fallback_title");
                if (dlPlatform) dlPlatform.innerText = data.platform || "";
                if (dlUploader) dlUploader.innerText = data.uploader || t("dl_fallback_uploader");
                if (dlDuration) {
                    if (data.metadata_verified === false) {
                        // Same honesty treatment as the Recap card
                        // (round-4 F-B): the extract fallback invents
                        // the duration ("03:30") and the title/uploader
                        // are placeholders — never render the fabricated
                        // duration as plain fact.
                        dlDuration.innerHTML = `⏱️ — <span style="color:#d97706; font-weight:700;">(${t("recap_unverified_note")})</span>`;
                    } else {
                        dlDuration.innerText = data.estimated_duration || "";
                    }
                }
                setCardThumb(dlThumb, data.thumbnail || null);

                dlCurrent = { url: data.url || url, has_subtitles: data.has_subtitles, extractData: data };
                dlSetSrtCardState(data.has_subtitles);
                const dlActions = document.getElementById("dl-delivery-actions");
                if (dlActions) dlActions.innerHTML = "";

                if (typeof setSectionState === "function") setSectionState("dl-extract-stage", "ready");
                resultCard.classList.remove("hidden");
                dlHideExtractError();
            } catch (err) {
                // Includes the client-side timeout (fetchWithTimeout
                // abort): the extract did not complete — say so, and
                // keep saying it after the toast fades (P1).
                dlClearResult();
                dlShowExtractError();
                showErrorToast(err, 'download');
            } finally {
                btnDl.disabled = false;
                if (dlBtnSpan) dlBtnSpan.innerText = dlBtnOriginal;
            }
        });

        const dlUrlInput = document.getElementById("dl-url-input");
        if (dlUrlInput && !dlUrlInput.dataset.dlClearBound) {
            dlUrlInput.dataset.dlClearBound = "1";
            dlUrlInput.addEventListener("input", () => {
                if (!dlUrlInput.value.trim()) dlClearResult();
            });
        }

        const fmtButtons = { video: "btn-dl-fmt-video", mp3: "btn-dl-fmt-mp3", srt: "btn-dl-fmt-srt" };
        Object.entries(fmtButtons).forEach(([fmt, id]) => {
            const card = document.getElementById(id);
            if (card) card.addEventListener("click", () => dlFetchFormat(fmt, card));
        });

        const btnUseInRecap = document.getElementById("btn-dl-use-in-recap");
        if (btnUseInRecap) {
            btnUseInRecap.addEventListener("click", () => {
                const urlInput = document.getElementById("dl-url-input");
                const url = urlInput ? urlInput.value.trim() : "";
                if (!url) return;

                haptic("success");
                const handoffUrl = (dlCurrent && dlCurrent.url) || url;
                if (dlCurrent && dlCurrent.extractData) {
                    state.recapExtractHandoff = { url: handoffUrl, data: dlCurrent.extractData };
                    if (typeof showStudioToast === "function" && typeof t === "function") {
                        showStudioToast(t("dl_use_in_recap_ready_toast"), "success");
                    }
                } else {
                    state.recapExtractHandoff = null;
                }

                // 1. Switch to tab-recap
                const tabRecapBtn = document.querySelector('[data-tab="tab-recap"]');
                if (tabRecapBtn) tabRecapBtn.click();

                // 2. Set recap URL and apply hand-off immediately without re-upload/re-probe
                const recapInput = document.getElementById("recap-url-input");
                if (recapInput) {
                    recapInput.value = handoffUrl;
                    const btnModeUrl = document.getElementById("btn-source-mode-url");
                    if (btnModeUrl) btnModeUrl.click();

                    const btnExtract = document.getElementById("btn-fetch-recap-info");
                    if (btnExtract) {
                        if (state.recapExtractHandoff) {
                            btnExtract.click();
                        } else {
                            setTimeout(() => btnExtract.click(), 300);
                        }
                    }
                }
            });
        }
    }
}

function switchVipSubView(view) {
    haptic("selection");
    const v = view === "wallet" ? "wallet" : "plans";
    document.getElementById("vip-subbtn-plans")?.classList.toggle("active", v === "plans");
    document.getElementById("vip-subbtn-wallet")?.classList.toggle("active", v === "wallet");
    document.getElementById("vip-sub-plans")?.classList.toggle("hidden", v !== "plans");
    document.getElementById("vip-sub-wallet")?.classList.toggle("hidden", v !== "wallet");
    if (v === "wallet") loadVipWalletHistory(true);
}
window.switchVipSubView = switchVipSubView;

// ── Plan Category Tabs (one category at a time — no long scroll) ──────────
function switchPlanCategory(cat) {
    haptic("selection");
    const target = cat === "combined" ? "combo" : cat;
    document.querySelectorAll(".plan-tab-btn").forEach(btn => {
        btn.classList.toggle("active", btn.getAttribute("data-plan-cat") === target);
    });
    ["voice", "recap", "combo", "topup"].forEach(name => {
        const panel = document.getElementById(`plan-panel-${name}`);
        if (panel) panel.classList.toggle("hidden", name !== target);
    });
}

// ── Payment Drawer & Slip Submission ───────────────────────────────────────
let activePackageCode = "voice_creator";
let activeAmount = 10000;
let planPriceByCode = {};
let _vipPlansCache = [];

function renderDynamicVipPlans(data) {
    const plans = (data && data.plans) || [];
    _vipPlansCache = plans;
    const buckets = { voice: [], recap: [], combo: [], topup: [] };
    plans.forEach(p => {
        const c = p.is_topup ? "topup" : (p.category === "combined" ? "combo" : (p.category || "voice"));
        (buckets[c] || buckets.voice).push(p);
    });
    Object.keys(buckets).forEach(cat => {
        const grid = document.getElementById(`vip-grid-${cat}`);
        if (!grid) return;
        const list = buckets[cat];
        if (!list.length) { grid.innerHTML = `<p class="text-muted text-sm">${escapeHtml(t("admin_empty"))}</p>`; return; }
        grid.innerHTML = list.map(p => {
            const code = escapeHtml(p.code || p.id);
            const name = escapeHtml(p.name || code);
            const alloc = escapeHtml(p.allocation || "");
            const priceMm = Number(p.price_mmk || parseInt(String(p.price || "0").replace(/[^0-9]/g, ""), 10) || 0);
            const badgeHtml = p.badge
                ? `<span class="vcp-plan-badge ${p.badge_key === "best_value" ? "vcp-badge-best" : ""}">${escapeHtml(p.badge)}</span>`
                : "";
            const feats = (Array.isArray(p.features) ? p.features.slice(0, 3) : [])
                .map(f => `<li>✓ ${escapeHtml(f)}</li>`).join("");
            const btnCls = (p.badge_key === "popular" || p.badge_key === "best_value")
                ? "btn gold-gradient-btn vcp-btn vcp-btn--gold mt-3"
                : "btn secondary-gold-btn vcp-btn mt-3";
            const valBadge = p.validity_label
                ? `<div class="vcp-plan-validity">⏳ ${escapeHtml(p.validity_label)}</div>`
                : "";
            return `
                <div class="card glass vcp-card pricing-card ${p.badge_key === "best_value" ? "vcp-card--gold" : ""}" id="card-plan-${code}">
                    ${badgeHtml}
                    <div class="plan-name">${name}</div>
                    <div class="plan-allocation">${alloc}</div>
                    ${valBadge}
                    <div class="plan-price">
                        <span class="price-val" data-price-for="${code}">${priceMm.toLocaleString("en-US")}</span>
                        <span class="currency">MMK</span>
                    </div>
                    ${feats ? `<ul class="vcp-features-mini">${feats}</ul>` : ""}
                    <button type="button" class="${btnCls}" onclick="openPaymentDrawer('${code}', ${priceMm}, '${escapeHtml(String(p.name || code)).replace(/'/g, "\\'")}')">💳 ${priceMm.toLocaleString("en-US")} MMK</button>
                </div>`;
        }).join("");
    });
}

function syncPlanPrices(data) {
    const plans = (data && data.plans) || [];
    plans.forEach(p => {
        const numeric = Number(p.price_mmk || parseInt(String(p.price || "").replace(/[^0-9]/g, ""), 10));
        if ((p.id || p.code) && Number.isFinite(numeric) && numeric > 0) {
            planPriceByCode[p.id || p.code] = numeric;
        }
    });
    renderDynamicVipPlans(data);
    document.querySelectorAll("[data-price-for]").forEach(el => {
        const v = planPriceByCode[el.getAttribute("data-price-for")];
        if (v) el.innerText = v.toLocaleString("en-US");
    });
}

async function refreshPlanPrices() {
    try {
        const res = await fetchWithTimeout("/api/vip/plans", { headers: getAuthHeaders() });
        if (res.ok) {
            const data = await res.json();
            syncPlanPrices(data);
            if (Array.isArray(data.payment_accounts)) paintPaymentAccountNames(data.payment_accounts);
        }
    } catch (err) {}
}

let _vipOrdersLoaded = false;
function renderGrantsRowsHtml(grants) {
    const list = Array.isArray(grants) ? grants : [];
    if (!list.length) return `<p class="text-muted text-xs">${escapeHtml(t("vip_grants_empty"))}</p>`;
    return list.map(g => {
        const rem = [];
        if (g.remaining_voice_minutes > 0 || g.initial_voice_minutes > 0) rem.push(`🎙️ ${_fmtCreditNum(g.remaining_voice_minutes)}m`);
        if (g.remaining_recap_credits > 0 || g.initial_recap_credits > 0) rem.push(`🎬 ${_fmtCreditNum(g.remaining_recap_credits)} Cr`);
        let expTxt = g.never_expires ? t("vip_grant_legacy")
            : (g.status === "EXPIRED" ? t("vip_grant_expired")
            : `${(g.expires_at || "").slice(0, 10)} (${tf("vip_grant_days_left", { d: g.days_left ?? 0 })})`);
        const cls = g.expiring_soon ? "vcp-grant-exp-soon" : "text-muted";
        return `<div class="vcp-grant-row"><div><b>${escapeHtml(g.package_name || g.package_code || g.source_type)}</b><br><span class="text-xs ${cls}">${escapeHtml(expTxt)}</span></div><div style="text-align:right"><b>${escapeHtml(rem.join(" · ") || "0")}</b><br><span class="badge-status ${String(g.status || "").toLowerCase()}">${escapeHtml(g.status)}</span></div></div>`;
    }).join("");
}
async function loadVipWalletHistory(force) {
    if (_vipOrdersLoaded && !force) return;
    _vipOrdersLoaded = true;
    const gEl = document.getElementById("vip-grants-list");
    if (gEl) {
        try {
            const rw = await fetchWithTimeout("/api/wallet/summary", { headers: getAuthHeaders() });
            if (rw.ok) {
                const wd = await rw.json();
                gEl.innerHTML = renderGrantsRowsHtml(wd.grants || []);
            }
        } catch (e) { gEl.innerHTML = `<p class="text-muted text-xs">—</p>`; }
    }
    const ordEl = document.getElementById("vip-orders-list");
    if (ordEl) {
        try {
            const r = await fetchWithTimeout("/api/vip/orders", { headers: getAuthHeaders() });
            if (r.ok) {
                const d = await r.json();
                const list = d.orders || [];
                ordEl.innerHTML = list.length ? list.map(o => `
                    <div class="vcp-tx-row">
                        <div><b>${escapeHtml(o.package_name || o.package_code)}</b><br><span class="text-xs text-muted">${admFmtDate(o.created_at)} • ${(o.amount_mmk || 0).toLocaleString()} MMK</span></div>
                        <span class="badge-status ${String(o.status || "").toLowerCase()}">${escapeHtml(o.status)}</span>
                    </div>`).join("") : `<p class="text-muted text-xs">${escapeHtml(t("admin_empty"))}</p>`;
            }
        } catch (e) { ordEl.innerHTML = `<p class="text-muted text-xs">—</p>`; }
    }
    loadVipWalletTx("");
}
window.loadVipWalletHistory = loadVipWalletHistory;

async function loadVipWalletTx(kind) {
    document.querySelectorAll("#vip-sub-wallet [data-tx-kind]").forEach(b => {
        b.classList.toggle("active", b.getAttribute("data-tx-kind") === (kind || ""));
    });
    const txEl = document.getElementById("vip-tx-list");
    if (!txEl) return;
    try {
        const qs = kind ? `?wallet_kind=${encodeURIComponent(kind)}` : "";
        const r = await fetchWithTimeout("/api/wallet/transactions" + qs, { headers: getAuthHeaders() });
        if (!r.ok) return;
        const d = await r.json();
        const rows = d.transactions || [];
        txEl.innerHTML = rows.length ? rows.map(tx => {
            const parts = [];
            if (tx.recap_credits_delta) parts.push(`${tx.recap_credits_delta > 0 ? "+" : ""}${_fmtCreditNum(tx.recap_credits_delta)} Cr`);
            if (tx.voice_minutes_delta) parts.push(`${tx.voice_minutes_delta > 0 ? "+" : ""}${_fmtCreditNum(tx.voice_minutes_delta)}m`);
            return `<div class="vcp-tx-row">
                <div><b>${escapeHtml(tx.tx_type)}</b> (${escapeHtml(tx.wallet_kind)})<br><span class="text-xs text-muted">${escapeHtml(tx.note || tx.reference_id || "")} • ${admFmtDate(tx.created_at)}</span></div>
                <div style="text-align:right"><b>${escapeHtml(parts.join(" · ") || "0")}</b><br><span class="text-xs text-muted">Recap ${_fmtCreditNum(tx.balance_recap_credits_after)} | Voice ${_fmtCreditNum(tx.balance_voice_minutes_after)}m</span></div>
            </div>`;
        }).join("") : `<p class="text-muted text-xs">${escapeHtml(t("admin_empty"))}</p>`;
    } catch (e) { txEl.innerHTML = `<p class="text-muted text-xs">—</p>`; }
}
window.loadVipWalletTx = loadVipWalletTx;

function paintPaymentAccountNames(accounts) {
    const list = Array.isArray(accounts) ? accounts : [];
    const prefix = t("pay_account_name_prefix");
    const extraBox = document.getElementById("extra-payment-accounts");
    if (list.length) {
        let seenK = false, seenW = false;
        const extras = [];
        const _setQr = (card, qr) => {
            if (!card) return;
            const old = card.querySelector(".pay-qr-btn");
            if (old) old.remove();
            if (!qr) return;
            const b = document.createElement("button");
            b.type = "button";
            b.className = "mini-copy-btn pay-qr-btn";
            b.style.marginTop = "6px";
            b.textContent = "📷 QR";
            b.onclick = () => { if (typeof openSlipModal === "function") openSlipModal(qr); else window.open(qr, "_blank"); };
            card.appendChild(b);
        };
        list.forEach(acc => {
            const prov = (acc.provider || "").toLowerCase();
            if (!seenK && (acc.id === "kpay_primary" || prov.includes("kbzpay") || prov.includes("kpay"))) {
                seenK = true;
                state.kpayNumber = acc.number || "";
                const elNum = document.getElementById("kpay-num");
                const elName = document.getElementById("kpay-name");
                const card = elNum ? elNum.closest(".pay-method") : null;
                if (card) card.classList.remove("hidden");
                if (elNum) elNum.innerText = acc.number || "—";
                if (elName) { elName.dataset.accountOwner = acc.name || ""; elName.innerText = acc.name ? `${prefix} ${acc.name}` : "—"; }
                _setQr(card, acc.qr_url || "");
            } else if (!seenW && (acc.id === "wave_primary" || prov.includes("wave"))) {
                seenW = true;
                state.waveNumber = acc.number || "";
                const elNum = document.getElementById("wave-num");
                const elName = document.getElementById("wave-name");
                const card = elNum ? elNum.closest(".pay-method") : null;
                if (card) card.classList.remove("hidden");
                if (elNum) elNum.innerText = acc.number || "—";
                if (elName) { elName.dataset.accountOwner = acc.name || ""; elName.innerText = acc.name ? `${prefix} ${acc.name}` : "—"; }
                _setQr(card, acc.qr_url || "");
            } else {
                extras.push(acc);
            }
        });
        const kCard = document.getElementById("kpay-num")?.closest(".pay-method");
        const wCard = document.getElementById("wave-num")?.closest(".pay-method");
        if (kCard) kCard.classList.toggle("hidden", !seenK && extras.length > 0);
        if (wCard) wCard.classList.toggle("hidden", !seenW && extras.length > 0);
        if (extraBox) {
            const copyLbl = escapeHtml(t("copy_btn") || "Copy");
            extraBox.innerHTML = extras.map(acc => {
                const col = /^#[0-9a-fA-F]{3,6}$/.test(acc.color || "") ? acc.color : "#0047BA";
                const ic = acc.account_type === "bank" ? "🏦" : (acc.account_type === "crypto" ? "🪙" : "💳");
                const sub = [acc.name ? `${prefix} ${acc.name}` : "", acc.bank_branch || "", acc.note || ""].filter(Boolean).join(" · ");
                const qrBtn = acc.qr_url ? `<button type="button" class="mini-copy-btn pay-qr-btn" style="margin-top:6px" onclick="typeof openSlipModal==='function'?openSlipModal('${escapeHtml(acc.qr_url)}'):window.open('${escapeHtml(acc.qr_url)}','_blank')">📷 QR</button>` : "";
                return `<div class="pay-method vcp-pay-method" style="border-left:4px solid ${col};margin-top:8px"><div class="pay-title">${ic} ${escapeHtml(acc.provider || "")}</div><div class="pay-account"><span class="acc-num">${escapeHtml(acc.number || "—")}</span><button type="button" class="mini-copy-btn" onclick="copyAccount('')">${copyLbl}</button></div><span class="acc-name">${escapeHtml(sub || "—")}</span>${qrBtn}</div>`;
            }).join("");
        }
        return;
    }
    ["kpay-name", "wave-name"].forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        const owner = el.dataset.accountOwner || "";
        el.dataset.accountOwner = owner;
        el.innerText = owner ? `${prefix} ${owner}` : "—";
    });
}
window.paintPaymentAccountNames = paintPaymentAccountNames;

async function openPaymentDrawer(packageCode, amount, title) {
    haptic("light");
    // A previously synced API price wins over the inline literal.
    if (planPriceByCode[packageCode]) {
        amount = planPriceByCode[packageCode];
    }
    activePackageCode = packageCode;
    activeAmount = amount;

    const titleEl = document.getElementById("drawer-package-title");
    if (titleEl && title) {
        titleEl.removeAttribute("data-i18n");
        titleEl.innerText = `${title} — ${amount.toLocaleString()} MMK`;
    }

    const drawer = document.getElementById("payment-box");
    if (drawer) {
        drawer.classList.remove("hidden");
        drawer.scrollIntoView({ behavior: "smooth" });
    }
    syncTelegramBackButton();

    // Dynamic sync of payment account numbers from API
    try {
        const res = await fetchWithTimeout("/api/vip/plans", { headers: getAuthHeaders() });
        if (res.ok) {
            const data = await res.json();
            // Batch CT: sync plan prices from the same payload; if the
            // open drawer's plan was repriced, repaint amount + title.
            syncPlanPrices(data);
            const fresh = planPriceByCode[activePackageCode];
            if (fresh && fresh !== activeAmount) {
                activeAmount = fresh;
                const titleEl2 = document.getElementById("drawer-package-title");
                if (titleEl2 && title) {
                    titleEl2.removeAttribute("data-i18n");
                    titleEl2.innerText = `${title} — ${fresh.toLocaleString()} MMK`;
                }
            }
            const accounts = data.payment_accounts || [];
            window._lastPaymentAccounts = accounts;
            paintPaymentAccountNames(accounts);
        }
    } catch (err) {
        console.warn("Could not sync payment accounts from API:", err);
    }
}

function closePaymentDrawer() {
    const drawer = document.getElementById("payment-box");
    if (drawer) drawer.classList.add("hidden");
    syncTelegramBackButton();
}

function copyAccount(accNum) {
    haptic("medium");
    let liveNum = "";
    try {
        const evTarget = typeof window !== "undefined" && window.event && (window.event.currentTarget || window.event.target);
        const row = evTarget && typeof evTarget.closest === "function" ? evTarget.closest(".pay-account") : null;
        const numEl = row && typeof row.querySelector === "function" ? row.querySelector(".acc-num") : null;
        if (numEl && numEl.textContent) liveNum = numEl.textContent.trim();
    } catch (e) {}
    const numToCopy = (liveNum && liveNum !== "—") ? liveNum : (accNum ? String(accNum).trim() : "");
    if (!numToCopy || numToCopy === "—") {
        showStudioToast(t("copy_unavailable_placeholder"), "warning");
        return;
    }
    // No native prompt dialog anywhere in the Mini App: when the Clipboard
    // API is unavailable, show the number in the premium modal with a
    // copy button instead.
    const showNumberModal = () => showStudioInputModal({
        title: t("account_number_title"),
        initialValue: numToCopy,
        confirmLabel: t("btn_copy"),
        onConfirm: () => {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(numToCopy).then(() => {
                    showStudioToast(`${numToCopy}\n${t("toast_copied")}`, 'success');
                }).catch(() => {});
            }
        },
    });
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(numToCopy).then(() => {
            showStudioToast(`${numToCopy}\n${t("toast_copied")}`, 'success');
        }).catch(showNumberModal);
    } else {
        showNumberModal();
    }
}

// ── On-Demand Telegram Delivery Actions ─────────────────────────────────────
async function sendVoiceToTelegram(targetJobId) {
    const jobId = targetJobId || state.activeJobId;
    if (!jobId) {
        showStudioToast(t("toast_job_id_missing"), 'warning');
        return;
    }
    haptic("medium");
    const btn = document.getElementById("btn-voice-send-telegram");
    const originalContent = btn ? btn.innerHTML : "";
    if (btn && !targetJobId) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-inline">⏳</span> <span>${t("toast_sending_telegram")}</span>`;
    }
    try {
        const res = await fetch(`/api/voices/${jobId}/send-telegram`, {
            method: "POST",
            headers: getAuthHeaders(),
        });
        const data = await res.json();
        if (!res.ok && data.detail === "file_too_large_telegram") {
            // Over Telegram's 50 MB send cap (B1): the file is fine and
            // stays downloadable here — say exactly that instead of a
            // generic send failure (same coded detail as the downloader).
            showStudioToast(t("dl_too_large_telegram"), 'warning', 6000);
            if (btn && !targetJobId) {
                btn.disabled = false;
                btn.innerHTML = originalContent;
            }
            return;
        }
        if (!res.ok) throw new Error(data.detail || "Failed to send to Telegram");
        haptic("success");
        if (btn && !targetJobId) {
            btn.classList.add("sent");
            btn.innerHTML = `<span>${t("btn_telegram_sent")}</span>`;
        }
        showStudioToast(t("toast_telegram_sent"), 'success');
    } catch (err) {
        showErrorToast(err, 'send');
        if (btn && !targetJobId) {
            btn.disabled = false;
            btn.innerHTML = originalContent;
        }
    }
}
window.sendVoiceToTelegram = sendVoiceToTelegram;

// ── Batch AW: standalone TTS tab ─────────────────────────────────────
// Gemini AI or Edge voices under the tab's OWN billing (referral
// bucket first, then the daily TTS allowance — never voice minutes or
// recap credits; the server enforces it in tts_service.py). The flow
// mirrors the voice flow: submit → poll /jobs/{id}/status → result
// card with player, branded download and bot-chat delivery.
const ttsState = { engine: "gemini", speed: "+12%", jobId: null, pollTimer: null };
window.ttsState = ttsState;

// Batch BZ: the two "today's TTS" surfaces — the header 🎧 chip and
// the Home "Free today" card — painted from the /api/me BZ fields,
// which the server computes through the same helpers the TTS submit
// path enforces with, so the numbers can never disagree with the
// gate. Admin → ∞ (the other chips' treatment). A null remaining
// with a 0 daily limit means the tier's allowance is unlimited; a
// null remaining for any other reason (quota read failed) falls back
// to the referral figure, the most the client can state truthfully.
function formatTtsTabBalanceLine(opts) {
    const isAdmin = !!(opts && opts.isAdmin);
    const isVip = !!(opts && opts.isVip);
    const unlimited = !!(opts && opts.unlimited);
    const refSec = Math.max(0, Number(opts && opts.refSec) || 0);
    const remainSec = Math.max(0, Number(opts && opts.remainSec) || 0);
    const refMin = (refSec / 60).toFixed(1);
    const remainMin = (remainSec / 60).toFixed(1);
    if (isAdmin) return t("tts_balance_line_admin");
    if (unlimited) {
        return (!isVip && refSec > 0)
            ? tf("tts_balance_line_unlimited", { ref_min: refMin })
            : t("tts_balance_line_unlimited_clean");
    }
    if (isVip) return tf("tts_balance_line_paid", { remain_min: remainMin });
    if (refSec > 0) return tf("tts_balance_line", { ref_min: refMin, remain_min: remainMin });
    return tf("tts_balance_line_free_daily", { remain_min: remainMin });
}

function paintHomeTilePeriods(data, quota) {
    const u = data || (state && state.user) || {};
    const q = quota || window._lastTtsQuota || null;
    const isAdmin = Boolean((q && q.is_admin) || u.is_admin);
    const isVip = Boolean(u.is_vip);
    const isUnlimited = Boolean(isAdmin || (q && q.unlimited));
    const dailyCapMin = (q && typeof q.daily_limit_minutes === "number" && q.daily_limit_minutes > 0)
        ? Math.round(q.daily_limit_minutes)
        : (typeof u.tts_daily_limit_seconds === "number" && u.tts_daily_limit_seconds > 0
            ? Math.round(u.tts_daily_limit_seconds / 60)
            : (isVip ? 60 : 15));

    const ttsPeriod = document.querySelector('#home-free-card [data-i18n="home_free_tts_period"]');
    if (ttsPeriod) {
        ttsPeriod.textContent = isUnlimited
            ? t("home_tile_period_unlimited")
            : tf("home_tile_tts_period_cap", { min: dailyCapMin });
    }
    const voicePeriod = document.querySelector('#home-free-card [data-i18n="home_free_voice_period"]');
    if (voicePeriod) {
        voicePeriod.textContent = isAdmin
            ? t("home_tile_period_unlimited")
            : (isVip ? t("home_tile_voice_period_paid") : t("home_free_voice_period"));
    }
    const recapPeriod = document.querySelector('#home-free-card [data-i18n="home_free_recap_period"]');
    if (recapPeriod) {
        recapPeriod.textContent = isAdmin
            ? t("home_tile_period_unlimited")
            : (isVip ? t("home_tile_recap_period_paid") : t("home_free_recap_period"));
    }
}

function paintTtsTodaySurfaces(data) {
    if (!data) return;
    const rem = data.tts_today_remaining_seconds;
    const chip = document.getElementById("user-tts-bal");
    if (chip) {
        if (data.is_admin) chip.innerText = "🎧 ∞";
        else if (typeof rem === "number") chip.innerText = `🎧 ${(rem / 60).toFixed(1)}m`;
        else if (data.tts_daily_limit_seconds === 0) chip.innerText = "🎧 ∞";
        else if (typeof data.tts_balance_seconds === "number") chip.innerText = `🎧 ${(data.tts_balance_seconds / 60).toFixed(1)}m`;
    }
    const ttsVal = document.getElementById("home-free-tts-value");
    if (ttsVal) {
        if (data.is_admin) ttsVal.innerText = "∞";
        else if (typeof rem === "number") ttsVal.innerText = tf("home_free_value_min", { min: (rem / 60).toFixed(1) });
        else if (data.tts_daily_limit_seconds === 0) ttsVal.innerText = t("unlimited");
        else if (typeof data.tts_balance_seconds === "number") ttsVal.innerText = tf("home_free_value_min", { min: (data.tts_balance_seconds / 60).toFixed(1) });
    }
    const note = document.getElementById("home-free-tts-note");
    if (note) {
        const ref = (data.tts_referral_seconds != null) ? data.tts_referral_seconds : data.tts_balance_seconds;
        if (!data.is_admin && !data.is_vip && typeof ref === "number" && ref > 0) {
            note.innerText = tf("home_free_tts_referral_note", { min: (ref / 60).toFixed(1) });
            note.classList.remove("hidden");
        } else {
            note.classList.add("hidden");
        }
    }
    if (typeof paintHomeTilePeriods === "function") paintHomeTilePeriods(data, window._lastTtsQuota || null);
    const voiceVal = document.getElementById("home-free-voice-value");
    if (voiceVal) voiceVal.innerText = data.is_admin ? "∞" : tf("home_free_value_min", { min: data.balance_minutes });
    const vipTtsVal = document.getElementById("vip-wallet-tts-val");
    if (vipTtsVal) {
        if (data.is_admin || data.tts_daily_limit_seconds === 0) vipTtsVal.innerText = t("unlimited");
        else if (typeof rem === "number") vipTtsVal.innerText = tf("home_free_value_min", { min: (rem / 60).toFixed(1) });
    }
    const recapVal = document.getElementById("home-free-recap-value");
    if (recapVal) recapVal.innerText = data.is_admin ? "∞" : tf("home_free_value_credits", { count: data.recap_credits || 0 });
    paintHomeStatusStrip(data);
    if (typeof paintTtsCostHintTiered === "function") paintTtsCostHintTiered(window._lastTtsQuota || null, data);
}

// The same two surfaces refreshed from a /api/tts/quota response —
// the post-job and tab-entry refresh path (refreshTtsQuota), so the
// chip/card follow a finished TTS job without a full profile reload.
function paintTtsChipFromQuota(q) {
    if (!q) return;
    const chip = document.getElementById("user-tts-bal");
    const ttsVal = document.getElementById("home-free-tts-value");
    if (q.is_admin || q.unlimited) {
        if (chip) chip.innerText = "🎧 ∞";
        if (ttsVal) ttsVal.innerText = q.is_admin ? "∞" : t("unlimited");
    } else {
        const total = (q.referral_seconds || 0) + (q.remaining_seconds || 0);
        if (chip) chip.innerText = `🎧 ${(total / 60).toFixed(1)}m`;
        if (ttsVal) ttsVal.innerText = tf("home_free_value_min", { min: (total / 60).toFixed(1) });
    }
    window._lastTtsQuota = q;
    const note = document.getElementById("home-free-tts-note");
    if (note) {
        const isVipUser = Boolean(state && state.user && state.user.is_vip);
        if (!q.is_admin && !isVipUser && (q.referral_seconds || 0) > 0) {
            note.innerText = tf("home_free_tts_referral_note", { min: ((q.referral_seconds || 0) / 60).toFixed(1) });
            note.classList.remove("hidden");
        } else {
            note.classList.add("hidden");
        }
    }
    paintHomeTilePeriods((state && state.user) || {}, q);
    paintHomeStatusStripFromQuota(q);
    if (typeof paintTtsCostHintTiered === "function") paintTtsCostHintTiered(q, (state && state.user) || null);
}

function paintVoiceTtsBalanceLine(profile) {
    // AW-11: the Voice tab labels the referral bucket as TTS-only —
    // it is NOT part of the clone balance anymore. Admins sit outside
    // referral/gift framing so the line stays hidden for them.
    const el = document.getElementById("voice-tts-balance-line");
    if (!el) return;
    if (profile && profile.is_admin) {
        el.textContent = "";
        el.classList.add("hidden");
        return;
    }
    const secs = profile && typeof profile.tts_balance_seconds === "number"
        ? profile.tts_balance_seconds : 0;
    if (secs > 0) {
        el.textContent = tf("voice_tts_balance_line", { min: (secs / 60).toFixed(1) });
        el.classList.remove("hidden");
    } else {
        el.textContent = "";
        el.classList.add("hidden");
    }
}
window.paintVoiceTtsBalanceLine = paintVoiceTtsBalanceLine;

function paintTtsCostHintTiered(q, profile) {
    const u = profile || (typeof state !== "undefined" && state && state.user) || {};
    const isAdmin = Boolean((q && q.is_admin) || u.is_admin);
    const isVip = Boolean(u.is_vip) && !isAdmin;
    const refSec = (q && typeof q.referral_seconds === "number")
        ? q.referral_seconds
        : (typeof u.tts_referral_seconds === "number" ? u.tts_referral_seconds : (u.tts_balance_seconds || 0));
    const hasRef = refSec > 0;
    const maxChars = (u.character_limit || (isAdmin || isVip ? (u.character_limit_purchased ?? 5000) : (u.character_limit_free ?? 1000))).toLocaleString();

    const shortEl = document.querySelector('#tab-tts [data-i18n="tts_cost_hint"]');
    if (shortEl) {
        if (isAdmin) shortEl.textContent = t("tts_cost_hint_admin");
        else if (isVip) shortEl.textContent = t(hasRef ? "tts_cost_hint_vip_ref" : "tts_cost_hint_vip");
        else shortEl.textContent = t(hasRef ? "tts_cost_hint" : "tts_cost_hint_free_noref");
        if (shortEl.classList && typeof shortEl.classList.toggle === "function") {
            shortEl.classList.toggle("hidden", Boolean(isAdmin));
        }
    }

    const moreEl = document.querySelector('#tts-cost-hint-more [data-i18n="tts_cost_hint_more"]');
    if (moreEl) {
        if (isAdmin) moreEl.textContent = tf("tts_cost_hint_more_admin", { max_chars: maxChars });
        else if (isVip) moreEl.textContent = tf(hasRef ? "tts_cost_hint_more_vip_ref" : "tts_cost_hint_more_vip", { max_chars: maxChars });
        else moreEl.textContent = t("tts_cost_hint_more");
    }
}
window.paintTtsCostHintTiered = paintTtsCostHintTiered;

// Batch BB-4 (diagnosed): this loader was the ONLY balance paint in
// the app that ignored the fetchWithTimeout standard — ONE bare fetch
// per tab entry, no timeout, no retry, and a catch that swallowed
// every failure (network error / non-200 / bad JSON) without painting
// anything, so the static "checking…" placeholder became the
// permanent state after ANY single failed attempt. Both live
// observations sat in deploy-restart windows of the single-process
// app (r73 go-live ~07:00, r74 deploy mid-audit ~07:20), where one
// request can hang on a draining instance or fail outright; the
// first pass recovered only because the job flow re-invoked this
// loader after each job, the audit ran no jobs and never recovered.
// The endpoint itself reproduces at ≤0.05 s locally under this exact
// request shape — the defect is this loader's fragility, fixed here:
// timeout via fetchWithTimeout, bounded retries, an in-flight guard,
// and a tap-to-retry fallback paint instead of eternal "checking…".
// Batch BH (production bug, owner-confirmed on a real phone
// 2026-11-06): despite the BB-4 bounds above, the line still sat on
// "checking…" forever in production — three times, incl. a fresh
// profile and the owner's phone — while the endpoint answers in
// ~0.5 s. Two defects are provable from this code alone:
// (1) every paint wrote into the node captured at loader start and
//     left its data-i18n="tts_balance_loading" attribute in place,
//     so ANY later setLanguage() pass (the profile language sync in
//     loadUserProfile, or a manual language toggle) re-stamped the
//     loading string over the painted balance — and nothing
//     re-triggered this loader, so the line stayed on "checking…"
//     for the rest of the session;
// (2) every paint — including the BB error fallback — sat behind
//     the fetch await-chain, so a chain that never settles inside
//     the Telegram webview paints nothing at all and leaves
//     ttsQuotaInFlight latched true, silently blocking every later
//     retry (tab re-entries included).
// BH makes the stuck state impossible by construction instead of
// betting on which stall fired: an instant paint from the profile
// /user/me already carries (BH-1), fresh-node paints that strip
// data-i18n so no i18n sweep can re-stamp the placeholder (BH-2),
// and a run-keyed 6 s watchdog that force-paints the tap-to-retry
// state and releases the latch (BH-3).
let ttsQuotaInFlight = false;
async function refreshTtsQuota() {
    // BH-1: paint from the already-loaded profile first — this runs
    // even while a previous (hung) chain holds the latch, because
    // profile data needs no network.
    paintTtsQuotaInstant();
    if (ttsQuotaInFlight) return;
    if (!document.getElementById("tts-balance-line")) return;
    ttsQuotaInFlight = true;
    const runId = ++ttsQuotaRunCounter;
    // BH-3: hard watchdog for this run (disarmed by any paint from
    // this run — see paintTtsBalanceLine).
    ttsQuotaWatchdogTimer = setTimeout(() => {
        if (runId !== ttsQuotaRunCounter) return;
        ttsQuotaWatchdogTimer = null;
        const line = document.getElementById("tts-balance-line");
        if (!line) return;
        const stillLoading = line.getAttribute("data-i18n") === "tts_balance_loading"
            || line.textContent === t("tts_balance_loading");
        if (stillLoading) {
            paintTtsQuotaError(runId);
            ttsQuotaInFlight = false;
        }
    }, 6000);
    try {
        const retryDelaysMs = [0, 700, 1800];
        for (let attempt = 0; attempt < retryDelaysMs.length; attempt++) {
            if (retryDelaysMs[attempt]) {
                await new Promise(r => setTimeout(r, retryDelaysMs[attempt]));
            }
            try {
                const res = await fetchWithTimeout("/api/tts/quota", { headers: getAuthHeaders() });
                if (!res.ok) throw new Error("tts quota " + res.status);
                const q = await res.json();
                _cgQuotaCache = q;
                const refMin = ((q.referral_seconds || 0) / 60).toFixed(1);
                paintTtsChipFromQuota(q);
                paintTtsGeminiKeyState(q);
                paintTtsTasteNote(q);
                paintTtsBalanceLine(runId, (line) => {
                    line.classList.remove("tts-balance-retry");
                    delete line.dataset.quotaError;
                    line.textContent = formatTtsTabBalanceLine({
                        isAdmin: Boolean(q.is_admin || (state && state.user && state.user.is_admin)),
                        isVip: Boolean(state && state.user && state.user.is_vip),
                        unlimited: Boolean(q.unlimited),
                        refSec: q.referral_seconds || 0,
                        remainSec: q.remaining_seconds || 0,
                    });
                });
                return;
            } catch (e) { /* retry, then the fallback paint below */ }
        }
        paintTtsQuotaError(runId);
    } finally {
        ttsQuotaInFlight = false;
    }
}
window.refreshTtsQuota = refreshTtsQuota;

// Batch BH machinery (declared after refreshTtsQuota; the function
// declarations hoist and the counters initialise at script load,
// before any tab entry can call the loader).
let ttsQuotaRunCounter = 0;
let ttsQuotaWatchdogTimer = null;

function paintTtsBalanceLine(runId, mutate) {
    // BH-2: re-query the node at paint time (never a captured node)
    // and strip the i18n hook BEFORE writing, so no later
    // setLanguage sweep can re-stamp the loading string over
    // painted data; dataset.dynamic marks the line loader-owned
    // (setLanguage re-triggers a refresh for dynamic lines).
    const line = document.getElementById("tts-balance-line");
    if (!line) return;
    line.removeAttribute("data-i18n");
    line.dataset.dynamic = "1";
    mutate(line);
    // BH-3: a paint from the CURRENT run disarms its watchdog; a
    // late paint from a stale run must not disarm a newer run's.
    if (runId === ttsQuotaRunCounter && ttsQuotaWatchdogTimer) {
        clearTimeout(ttsQuotaWatchdogTimer);
        ttsQuotaWatchdogTimer = null;
    }
}

function paintTtsQuotaError(runId) {
    paintTtsBalanceLine(runId, (line) => {
        line.textContent = t("tts_balance_error");
        line.dataset.quotaError = "1";
        line.classList.add("tts-balance-retry");
    });
}

function paintTtsQuotaInstant() {
    // BH-1: /user/me already carries tts_balance_seconds (the Voice
    // tab paints its own line from it in paintVoiceTtsBalanceLine);
    // paint the referral picture from it immediately — the quota
    // response then overwrites with the full picture (daily
    // remaining included). Admins sit outside the daily cap, so
    // their instant paint uses the unlimited format. The run key is
    // -1: the instant paint is not a fetch run and must never
    // disarm a watchdog.
    const profile = state.user;
    if (!profile || typeof profile.tts_balance_seconds !== "number") return;
    const refMin = (profile.tts_balance_seconds / 60).toFixed(1);
    paintTtsCostHintTiered(null, profile);
    paintTtsBalanceLine(-1, (line) => {
        line.classList.remove("tts-balance-retry");
        delete line.dataset.quotaError;
        const remSec = (typeof profile.tts_today_remaining_seconds === "number")
            ? profile.tts_today_remaining_seconds
            : (typeof profile.tts_daily_limit_seconds === "number" ? profile.tts_daily_limit_seconds : (profile.is_vip ? 3600 : 900));
        line.textContent = formatTtsTabBalanceLine({
            isAdmin: Boolean(profile.is_admin),
            isVip: Boolean(profile.is_vip),
            unlimited: Boolean(profile.is_admin),
            refSec: profile.tts_referral_seconds ?? profile.tts_balance_seconds ?? 0,
            remainSec: remSec,
        });
    });
}

// Batch BZ (Item 7): first-visit sample prefill — a brand-new user
// landing on the TTS tab can press Generate immediately instead of
// facing an empty box. Session-scoped: once the user types, edits,
// clears, uses a sample chip, or simply already has text in the box,
// the prefill never fires again this session, and it NEVER
// overwrites existing text.
let ttsSampleSettled = false;
function maybePrefillTtsSample() {
    if (ttsSampleSettled) return;
    const input = document.getElementById("tts-text-input");
    if (!input) return;
    ttsSampleSettled = true;
    if ((input.value || "").trim() !== "") return;
    input.value = t("tts_sample_text");
    updateTtsCharCount();
}
window.maybePrefillTtsSample = maybePrefillTtsSample;

function updateTtsCharCount() {
    const input = document.getElementById("tts-text-input");
    const counter = document.getElementById("tts-char-count");
    if (input && counter) {
        // Batch BW: the caller's OWN tier limit (same expression the
        // voice counter uses) — before BW this was hardcoded 5,000 for
        // everyone while the server now enforces per-tier.
        const len = (input.value || "").length;
        const max = state.user?.character_limit || (state.user?.is_vip ? (state.user?.character_limit_purchased ?? 5000) : (state.user?.character_limit_free ?? 1000));
        counter.textContent = `${len} / ${max} ${t("char_counter_unit")}`;
        counter.style.color = len > max ? "#ef4444" : "var(--text-muted)";
        syncTtsExcessAndCost(len, max);
    }
}
window.updateTtsCharCount = updateTtsCharCount;
function insertTtsSampleText(txt) {
    const input = document.getElementById("tts-text-input");
    ttsSampleSettled = true;
    if (input) { input.value = txt; updateTtsCharCount(); input.focus(); }
}
window.insertTtsSampleText = insertTtsSampleText;
function clearTtsText() {
    const input = document.getElementById("tts-text-input");
    ttsSampleSettled = true;
    if (input) { input.value = ""; updateTtsCharCount(); }
}
window.clearTtsText = clearTtsText;

// ── Batch CG: BYOK Gemini key card, taste note, Item 3b prompt ──
// The key VALUE is never painted anywhere: the card shows only the
// server's masked view (•••• + last 4) from /api/tts/quota, and the
// typed value is cleared from the input as soon as the save returns.
let _cgQuotaCache = null;

function paintTtsGeminiKeyState(q) {
    const stateEl = document.getElementById("tts-key-state");
    const delBtn = document.getElementById("btn-tts-key-delete");
    const activeBadge = document.getElementById("tts-key-active-badge");
    if (activeBadge) {
        activeBadge.classList.toggle("hidden", !(q && q.has_own_key && q.own_key_state !== "invalid"));
    }
    if (!stateEl) return;
    if (q && q.has_own_key) {
        stateEl.removeAttribute("data-i18n");
        stateEl.textContent = q.own_key_state === "invalid"
            ? t("tts_key_state_invalid")
            : tf("tts_key_state_ok", { masked: q.own_key_masked || "" });
        if (delBtn) delBtn.classList.remove("hidden");
    } else {
        stateEl.textContent = t("tts_key_state_none");
        if (delBtn) delBtn.classList.add("hidden");
    }
}

function paintTtsTasteNote(q) {
    const note = document.getElementById("tts-taste-note");
    if (!note) return;
    const remaining = q ? q.gemini_taste_remaining_seconds : null;
    const u = (typeof state !== "undefined" && state && state.user) ? state.user : null;
    const isPrivileged = Boolean((q && q.is_admin) || (u && (u.is_admin || u.is_vip)));
    // Taste lines are FREE-only and pointless once an own key routes
    // first — for everyone else the note stays hidden.
    if (isPrivileged || remaining === null || remaining === undefined || (q && q.has_own_key)) {
        note.classList.add("hidden");
        note.textContent = "";
        return;
    }
    note.classList.remove("hidden");
    note.textContent = remaining > 0
        ? tf("tts_taste_remaining", { min: Math.ceil(remaining / 60) })
        : t("tts_taste_spent");
}

async function saveTtsGeminiKey() {
    const input = document.getElementById("tts-key-input");
    const btn = document.getElementById("btn-tts-key-save");
    const candidate = ((input && input.value) || "").trim();
    if (!candidate) {
        showStudioToast(t("tts_key_save_failed"), 'warning');
        return;
    }
    if (btn) { btn.disabled = true; btn.textContent = t("tts_key_saving"); }
    try {
        const res = await fetchWithTimeout("/api/tts/gemini-key", {
            method: "POST",
            headers: getAuthHeaders(),
            body: JSON.stringify({ key: candidate }),
        }, 90000);
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || t("tts_key_save_failed"));
        if (input) input.value = "";
        showStudioToast(t("tts_key_saved"), 'success');
        refreshTtsQuota();
    } catch (err) {
        showStudioToast((err && err.message) || t("tts_key_save_failed"), 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = t("tts_key_save_btn"); }
    }
}

async function deleteTtsGeminiKey() {
    const btn = document.getElementById("btn-tts-key-delete");
    if (btn) btn.disabled = true;
    try {
        const res = await fetchWithTimeout("/api/tts/gemini-key", {
            method: "DELETE",
            headers: getAuthHeaders(),
        }, 30000);
        if (!res.ok) throw new Error("delete failed");
        const input = document.getElementById("tts-key-input");
        if (input) input.value = "";
        showStudioToast(t("tts_key_deleted"), 'success');
        refreshTtsQuota();
    } catch (err) {
        showStudioToast(t("tts_key_save_failed"), 'error');
    } finally {
        if (btn) btn.disabled = false;
    }
}

async function _cgTasteBlocked() {
    // Item 3b gate, checked on a FRESH quota read at generate time
    // (not the cached paint): FREE + no own key + taste spent.
    try {
        const res = await fetchWithTimeout("/api/tts/quota", { headers: getAuthHeaders() });
        if (!res.ok) return false;
        const q = await res.json();
        _cgQuotaCache = q;
        paintTtsGeminiKeyState(q);
        paintTtsTasteNote(q);
        return !q.is_admin && q.tier === "FREE" && !q.has_own_key
            && q.gemini_taste_remaining_seconds !== null
            && q.gemini_taste_remaining_seconds !== undefined
            && q.gemini_taste_remaining_seconds <= 0;
    } catch (e) {
        return false;
    }
}

function _cgPromptAddKey() {
    const modal = document.getElementById("tts-key-prompt-modal");
    if (modal) modal.classList.add("hidden");
    toggleTtsByokAdvanced(true);
    const card = document.getElementById("tts-gemini-key-card");
    if (card) card.scrollIntoView({ behavior: "smooth", block: "center" });
    const input = document.getElementById("tts-key-input");
    if (input) setTimeout(() => input.focus(), 350);
}

function _cgPromptUseEdge() {
    const modal = document.getElementById("tts-key-prompt-modal");
    if (modal) modal.classList.add("hidden");
    const edgeBtn = document.querySelector('#tts-engine-picker .seg-btn[data-tts-engine="edge"]');
    if (edgeBtn) edgeBtn.click();
    generateTTS();
}

// ── Batch CN: tap-to-expand detail blocks on the TTS tab ────────
// One reusable toggle for the tab's long explanations (the cost
// hint's billing detail, the key card's how-to + privacy notes):
// the button carries data-detail-target="<id>"; the target starts
// .hidden and unfolds in place — the toggleHomeTutorials idiom
// (hidden flip + .open chevron rotation + aria-expanded). State is
// not persisted; a fresh visit starts collapsed.
function toggleTtsDetail(btn) {
    if (!btn) return;
    const target = document.getElementById(btn.getAttribute("data-detail-target") || "");
    if (!target) return;
    const open = target.classList.toggle("hidden") === false;
    btn.classList.toggle("open", open);
    btn.setAttribute("aria-expanded", open ? "true" : "false");
}
window.toggleTtsDetail = toggleTtsDetail;

// Batch CN: engine-driven visibility for the whole TTS tab. The
// Gemini-only surfaces (taste note + BYOK key card) exist only in
// Gemini mode; the Edge voice hint explains the 10 → 2 voice list
// in Edge mode. The taste note's own quota rules still decide its
// Gemini-mode visibility (paintTtsTasteNote re-run on the cached
// quota), so the two never fight.
function applyTtsEngineVisibility() {
    const engine = ttsState.engine || "gemini";
    const gRow = document.getElementById("tts-gemini-voice-row");
    const eRow = document.getElementById("tts-edge-voice-row");
    if (gRow) gRow.classList.toggle("hidden", engine !== "gemini");
    if (eRow) eRow.classList.toggle("hidden", engine !== "edge");
    const keyCard = document.getElementById("tts-gemini-key-card");
    if (keyCard) keyCard.classList.toggle("hidden", engine !== "gemini");
    const edgeHint = document.getElementById("tts-edge-voice-hint");
    if (edgeHint) edgeHint.classList.toggle("hidden", engine !== "edge");
    const tasteNote = document.getElementById("tts-taste-note");
    if (tasteNote) {
        if (engine !== "gemini") tasteNote.classList.add("hidden");
        else paintTtsTasteNote(_cgQuotaCache);
    }
}

function initTtsTab() {
    document.querySelectorAll("#tts-engine-picker .seg-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            ttsState.engine = btn.getAttribute("data-tts-engine") || "gemini";
            document.querySelectorAll("#tts-engine-picker .seg-btn").forEach(
                b => b.classList.toggle("active", b === btn)
            );
            applyTtsEngineVisibility();
        });
    });
    document.querySelectorAll("#tts-speed-picker .speed-seg").forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            ttsState.speed = btn.getAttribute("data-tts-speed") || "+12%";
            document.querySelectorAll("#tts-speed-picker .speed-seg").forEach(
                b => b.classList.toggle("active", b === btn)
            );
            moveSpeedPill(document.getElementById("tts-speed-picker"));
            const inp = document.getElementById("tts-text-input");
            if (inp) {
                const maxC = state.user?.character_limit || (state.user?.is_vip ? (state.user?.character_limit_purchased ?? 5000) : (state.user?.character_limit_free ?? 1000));
                syncTtsExcessAndCost((inp.value || "").length, maxC);
            }
        });
    });
    const input = document.getElementById("tts-text-input");
    if (input) {
        try {
            const savedDraft = localStorage.getItem("vcp_tts_draft");
            if (savedDraft && !input.value) {
                input.value = savedDraft;
                ttsSampleSettled = true;
            }
        } catch (e) {}
        input.addEventListener("input", () => {
            // Any real edit settles the BZ prefill for this session.
            ttsSampleSettled = true;
            try { localStorage.setItem("vcp_tts_draft", input.value); } catch (e) {}
            updateTtsCharCount();
        });
    }
    // Batch CG: key card + Item 3b prompt wiring.
    const keySaveBtn = document.getElementById("btn-tts-key-save");
    if (keySaveBtn) keySaveBtn.addEventListener("click", saveTtsGeminiKey);
    const keyDelBtn = document.getElementById("btn-tts-key-delete");
    if (keyDelBtn) keyDelBtn.addEventListener("click", deleteTtsGeminiKey);
    const promptAddBtn = document.getElementById("btn-tts-prompt-add-key");
    if (promptAddBtn) promptAddBtn.addEventListener("click", _cgPromptAddKey);
    const promptEdgeBtn = document.getElementById("btn-tts-prompt-edge");
    if (promptEdgeBtn) promptEdgeBtn.addEventListener("click", _cgPromptUseEdge);
    updateTtsCharCount();
    // Batch BB-4: the balance line's fallback state is tap-to-retry.
    const balLine = document.getElementById("tts-balance-line");
    if (balLine) balLine.addEventListener("click", () => {
        if (balLine.dataset.quotaError === "1") refreshTtsQuota();
    });
    // Batch CN: paint the engine-driven surfaces once at init so
    // the tab opens consistent with the selected engine.
    applyTtsEngineVisibility();
}
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initTtsTab);
} else {
    initTtsTab();
}

async function generateTTS() {
    if (!studioSubmitBegin("tts")) return;
    const input = document.getElementById("tts-text-input");
    const text = ((input && input.value) || "").trim();
    if (!text) {
        studioSubmitFailure("tts");
        showStudioToast(t("tts_empty_error"), 'warning');
        return;
    }
    // Batch BW: client-side tier guard mirroring the voice submit
    // guard — the server enforces the same per-tier limit, this only
    // spares the round trip.
    const maxChars = state.user?.character_limit || (state.user?.is_vip ? (state.user?.character_limit_purchased ?? 5000) : (state.user?.character_limit_free ?? 1000));
    if (text.length > maxChars) {
        studioSubmitFailure("tts");
        showStudioToast(tf("text_too_long", {
            len: text.length,
            tier: state.user?.is_vip ? "VIP" : t("tier_free_label"),
            max: maxChars,
            vip_note: state.user?.is_vip ? "" : t("text_too_long_vip_note"),
        }), 'warning');
        return;
    }
    // Batch CG Item 3b: a FREE user who picked Gemini with the
    // taste spent is NEVER silently switched to Edge — the prompt
    // offers the own-key way to keep Gemini, or an explicit Edge
    // continue (which re-enters here with Edge selected).
    if (ttsState.engine === "gemini" && await _cgTasteBlocked()) {
        studioSubmitFailure("tts");
        const modal = document.getElementById("tts-key-prompt-modal");
        if (modal) modal.classList.remove("hidden");
        return;
    }
    const engine = ttsState.engine;
    const voice = engine === "gemini"
        ? ((document.getElementById("tts-gemini-voice-select") || {}).value || "Charon")
        : ((document.getElementById("tts-edge-voice-select") || {}).value || "thiha");
    const btn = document.getElementById("btn-generate-tts");
    const progressBox = document.getElementById("tts-progress-box");
    const resultBox = document.getElementById("tts-result-box");
    const failedBox = document.getElementById("tts-failed-box");
    const cancelBtn = document.getElementById("btn-cancel-tts-job");
    haptic("heavy");
    if (btn) btn.disabled = true;
    if (resultBox) resultBox.classList.add("hidden");
    if (failedBox) failedBox.classList.add("hidden");
    if (cancelBtn) cancelBtn.classList.remove("hidden");
    if (progressBox) progressBox.classList.remove("hidden");
    try {
        const res = await fetchWithTimeout("/api/tts/generate", {
            method: "POST",
            headers: getAuthHeaders(),
            body: JSON.stringify({ text, engine, voice, speed: ttsState.speed }),
        }, 30000);
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "TTS request failed");
        studioSubmitSuccess("tts");
        ttsState.jobId = data.job_id;
        try { localStorage.setItem("vcp_active_tts_job_id", data.job_id); } catch (e) {}
        if (typeof LiveJobs !== "undefined" && typeof LiveJobs.track === "function") {
            LiveJobs.track({ id: data.job_id, kind: "tts", status: "QUEUED", progress_percent: 0, stage_index: 0, stage_total: 1, title: text.slice(0, 70) });
        }
        pollTtsJob(data.job_id);
    } catch (err) {
        studioSubmitFailure("tts");
        showErrorToast(err, 'voice');
        if (cancelBtn) cancelBtn.classList.add("hidden");
        if (progressBox) progressBox.classList.add("hidden");
        if (btn) btn.disabled = false;
    }
}
window.generateTTS = generateTTS;

function pollTtsJob(jobId) {
    if (ttsState.pollTimer) { clearInterval(ttsState.pollTimer); ttsState.pollTimer = null; }
    let tick = 0;
    const startedAt = Date.now();
    ttsState.pollTimer = setInterval(async () => {
        tick++;
        try {
            const res = await fetch(`/api/jobs/${jobId}/status`, { headers: getAuthHeaders() });
            if (!res.ok) return;
            const data = await res.json();
            const progressBox = document.getElementById("tts-progress-box");
            const resultBox = document.getElementById("tts-result-box");
            const fill = document.getElementById("tts-progress-fill");
            const btn = document.getElementById("btn-generate-tts");
            const cancelBtn = document.getElementById("btn-cancel-tts-job");
            if (data.status === "QUEUED" || data.status === "RUNNING") {
                if (fill) paintProgressBarFill(fill, { status: data.status, progress_percent: 0 });
            } else if (data.status === "SUCCESS") {
                try { localStorage.removeItem("vcp_active_tts_job_id"); } catch (e) {}
                clearInterval(ttsState.pollTimer);
                ttsState.pollTimer = null;
                if (cancelBtn) cancelBtn.classList.add("hidden");
                haptic("success");
                if (progressBox) progressBox.classList.add("hidden");
                if (btn) btn.disabled = false;
                if (resultBox) resultBox.classList.remove("hidden");
                const player = document.getElementById("tts-audio-player");
                if (player && data.output_audio_url) {
                    setAuthMediaSource(player, data.output_audio_url).then((ok) => {
                        if (ok) { player.load(); player.play().catch(() => {}); }
                    });
                }
                const btnDl = document.getElementById("btn-download-tts");
                if (btnDl && data.output_audio_url) {
                    btnDl.setAttribute("data-dl-url", data.output_audio_url);
                    btnDl.setAttribute("data-dl-name", brandedDlName("TTS", "wav", null, jobId));
                }
                const oversize = data.telegram_sendable === false;
                const btnTg = document.getElementById("btn-tts-send-telegram");
                const note = document.getElementById("tts-oversize-note");
                if (btnTg) btnTg.classList.toggle("hidden", oversize);
                if (note) note.classList.toggle("hidden", !oversize);
                refreshTtsQuota();
                loadUserProfile();
                loadUserHistory();
            } else if (data.status === "FAILED" || data.status === "CANCELLED") {
                try { localStorage.removeItem("vcp_active_tts_job_id"); } catch (e) {}
                clearInterval(ttsState.pollTimer);
                ttsState.pollTimer = null;
                if (cancelBtn) cancelBtn.classList.add("hidden");
                if (progressBox) progressBox.classList.add("hidden");
                if (btn) btn.disabled = false;
                if (data.status !== "FAILED") {
                    haptic("light");
                    showStudioToast(t("voice_cancelled_toast"), 'info', 5000);
                } else {
                    haptic("error");
                    const errText = data.error_message ? mapErrorToToast(data.error_message, 'voice') : t("tts_failed");
                    showStudioToast(t("tts_failed"), 'error');
                    const tFailMsg = document.getElementById("tts-failed-msg");
                    if (tFailMsg) tFailMsg.textContent = errText;
                    const tFailBox = document.getElementById("tts-failed-box");
                    if (tFailBox) tFailBox.classList.remove("hidden");
                }
                refreshTtsQuota();
            }
            if (tick > 120 && !["SUCCESS", "FAILED", "CANCELLED"].includes(data.status)) {
                clearInterval(ttsState.pollTimer);
                ttsState.pollTimer = null;
                if (cancelBtn) cancelBtn.classList.add("hidden");
                if (progressBox) progressBox.classList.add("hidden");
                if (btn) btn.disabled = false;
                showStudioToast(t("voice_wait_timeout"), 'info', 6000);
            }
        } catch (e) {
            if (tick > 120) {
                clearInterval(ttsState.pollTimer);
                ttsState.pollTimer = null;
                const cancelBtn = document.getElementById("btn-cancel-tts-job");
                if (cancelBtn) cancelBtn.classList.add("hidden");
                const progressBox = document.getElementById("tts-progress-box");
                if (progressBox) progressBox.classList.add("hidden");
                const btn = document.getElementById("btn-generate-tts");
                if (btn) btn.disabled = false;
            }
        }
    }, 2000);
}

window.cancelTtsJob = function() {
    if (ttsState.jobId && typeof window.liveJobCancel === "function") {
        window.liveJobCancel(ttsState.jobId, "tts");
    }
};

async function sendTtsToTelegram() {
    const jobId = ttsState.jobId;
    if (!jobId) {
        showStudioToast(t("toast_job_id_missing"), 'warning');
        return;
    }
    haptic("medium");
    const btn = document.getElementById("btn-tts-send-telegram");
    const originalContent = btn ? btn.innerHTML : "";
    if (btn) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-inline">⏳</span> <span>${t("toast_sending_telegram")}</span>`;
    }
    try {
        const res = await fetch(`/api/voices/${jobId}/send-telegram`, {
            method: "POST",
            headers: getAuthHeaders(),
        });
        const data = await res.json();
        if (!res.ok && data.detail === "file_too_large_telegram") {
            showStudioToast(t("dl_too_large_telegram"), 'warning', 6000);
            if (btn) { btn.disabled = false; btn.innerHTML = originalContent; }
            return;
        }
        if (!res.ok) throw new Error(data.detail || "Failed to send to Telegram");
        haptic("success");
        if (btn) {
            btn.classList.add("sent");
            btn.innerHTML = `<span>${t("btn_telegram_sent")}</span>`;
        }
        showStudioToast(t("toast_telegram_sent"), 'success');
    } catch (err) {
        showErrorToast(err, 'send');
        if (btn) { btn.disabled = false; btn.innerHTML = originalContent; }
    }
}
window.sendTtsToTelegram = sendTtsToTelegram;

async function sendRecapToTelegram(targetJobId) {
    const jobId = targetJobId || state.recapJobId;
    if (!jobId) {
        showStudioToast(t("toast_job_id_missing"), 'warning');
        return;
    }
    haptic("medium");
    const btn = document.getElementById("btn-recap-send-telegram");
    const originalContent = btn ? btn.innerHTML : "";
    if (btn && !targetJobId) {
        btn.disabled = true;
        btn.innerHTML = `<span class="spinner-inline">⏳</span> <span>${t("toast_sending_telegram")}</span>`;
    }
    try {
        // Batch AF: route through fetchWithTimeout — the plain fetch
        // had NO timeout, so a stuck server-side send spun the button
        // forever. The cap is generous because the server uploads up
        // to 50 MB to Telegram before it can answer.
        const res = await fetchWithTimeout(`/api/recap/${jobId}/send-telegram`, {
            method: "POST",
            headers: getAuthHeaders(),
        }, TELEGRAM_SEND_TIMEOUT_MS);
        const data = await res.json();
        if (!res.ok && data.detail === "telegram_delivery_failed") {
            // Batch AF: the server now reports a failed delivery
            // honestly (it used to answer success unconditionally, so
            // this toast claimed ✅ for sends that never happened).
            // Say exactly that and point at the Download action that
            // still works (same coded-detail pattern as the voice
            // flow's file_too_large_telegram).
            showStudioToast(t("toast_telegram_send_failed"), 'error', 6000);
            if (btn && !targetJobId) {
                btn.disabled = false;
                btn.innerHTML = originalContent;
            }
            return;
        }
        if (!res.ok) throw new Error(data.detail || "Failed to send to Telegram");
        haptic("success");
        if (btn && !targetJobId) {
            btn.classList.add("sent");
            btn.innerHTML = `<span>${t("btn_telegram_sent")}</span>`;
        }
        showStudioToast(t("toast_telegram_sent"), 'success');
    } catch (err) {
        showErrorToast(err, 'send');
        if (btn && !targetJobId) {
            btn.disabled = false;
            btn.innerHTML = originalContent;
        }
    }
}
window.sendRecapToTelegram = sendRecapToTelegram;

function setupSlipPreview() {
    const fileInput = document.getElementById("slip-file-input");
    const previewBox = document.getElementById("slip-preview-box");
    const previewImg = document.getElementById("slip-preview-img");

    if (fileInput && previewBox && previewImg) {
        fileInput.addEventListener("change", (e) => {
            const file = e.target.files && e.target.files[0];
            const nameEl = document.getElementById("slip-file-name");
            if (file) {
                if (nameEl) nameEl.textContent = file.name;
                const reader = new FileReader();
                reader.onload = (ev) => {
                    previewImg.src = ev.target.result;
                    previewBox.classList.remove("hidden");
                };
                reader.readAsDataURL(file);
            } else {
                if (nameEl) nameEl.textContent = t("slip_file_none");
                previewBox.classList.add("hidden");
            }
        });
    }
}

async function submitPaymentSlip() {
    const fileInput = document.getElementById("slip-file-input");
    const file = fileInput && fileInput.files ? fileInput.files[0] : null;
    if (!file) {
        showStudioToast(t("toast_slip_no_file"), 'info');
        return;
    }

    haptic("heavy");
    const btn = document.getElementById("btn-submit-slip-action");
    if (btn) btn.disabled = true;
    const slipBtnSpan = btn ? btn.querySelector("span") : null;
    const slipBtnOriginal = slipBtnSpan ? slipBtnSpan.innerText : "";
    if (slipBtnSpan) slipBtnSpan.innerText = t("slip_sending");

    try {
        const formData = new FormData();
        formData.append("package_code", activePackageCode);
        formData.append("amount_mmk", activeAmount);
        formData.append("slip_file", file);
        const payRefEl = document.getElementById("slip-pay-ref");
        if (payRefEl && payRefEl.value.trim()) formData.append("payment_reference", payRefEl.value.trim());

        const res = await fetchWithTimeout("/api/vip/submit-slip", {
            method: "POST",
            headers: getAuthHeaders(false),
            body: formData,
        }, 60000);

        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Submission failed");

        showStudioToast(data.message || t("toast_success_slip"), 'success', 7000);
        closePaymentDrawer();
        if (fileInput) fileInput.value = "";
        if (payRefEl) payRefEl.value = "";
        const previewBox = document.getElementById("slip-preview-box");
        if (previewBox) previewBox.classList.add("hidden");
        loadVipWalletHistory(true);
    } catch (e) {
        showErrorToast(e, 'generic');
    } finally {
        if (btn) btn.disabled = false;
        if (slipBtnSpan) slipBtnSpan.innerText = slipBtnOriginal;
    }
}

// ── TAB 5: Role-Gated Admin Console ───────────────────────────────────────
// ══════════════════════════════════════════════════════════════════════
// ADMIN CONSOLE (rebuilt 2026-10-31)
// Hub + one-topic screens: Overview / Payments / Jobs / Users /
// Broadcast / Activity / Settings. Every list has loading, empty and
// error states. Destructive actions live in danger zones and are
// confirmation-guarded; balance changes always require a reason and go
// through the audited /api/admin/adjust-user path (unchanged backend).
// ══════════════════════════════════════════════════════════════════════

const ADM_SECTIONS = ["overview", "payments", "jobs", "users", "broadcast", "activity", "keys", "settings"];
let admCurrentSection = "overview";
let admCurrentUserId = null;
// Batch AJ M5: one idempotency key per adjust intent — generated
// when the user-detail dialog opens, reused if the apply request is
// retried after a network failure, cleared after a successful apply
// (a further apply is a NEW intent and must not be deduped).
let _admAdjustIdemKey = null;
function _newAdjustIdemKey() {
    try { return crypto.randomUUID(); } catch (e) { return "adj-" + Date.now() + "-" + Math.floor(Math.random() * 1e9); }
}
let admLoadedSections = {};

function admFmtBytes(n) {
    if (n == null) return "—";
    if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB";
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
    return Math.round(n / 1024) + " KB";
}

function admFmtDate(iso) {
    if (!iso) return "—";
    return String(iso).replace("T", " ").slice(0, 16);
}

function admStateLoading(el) {
    if (el) el.innerHTML = `<div class="adm-state"><span class="spinner"></span><span data-i18n="admin_loading">${t("admin_loading")}</span></div>`;
}

function admStateEmpty(el, msgKey) {
    const k = msgKey || "admin_empty";
    if (el) el.innerHTML = `<div class="adm-state adm-state-empty"><span class="adm-state-icon">📭</span><span data-i18n="${escapeHtml(k)}">${t(k)}</span></div>`;
}

function admStateError(el, retryJs) {
    if (el) el.innerHTML = `<div class="adm-state adm-state-error"><span class="adm-state-icon">⚠️</span><span data-i18n="admin_load_error">${t("admin_load_error")}</span>${retryJs ? `<button class="mini-chip" onclick="${retryJs}" data-i18n="admin_retry">${t("admin_retry")}</button>` : ""}</div>`;
}

function showAdminSection(name) {
    if (!ADM_SECTIONS.includes(name)) name = "overview";
    admCurrentSection = name;
    haptic("light");
    document.querySelectorAll("#adm-nav .adm-nav-btn").forEach(b => {
        const active = b.getAttribute("data-adm-section") === name;
        b.classList.toggle("active", active);
        // Round-6 P10: the sub-nav overflows on narrow screens and the
        // last tabs sat off-screen with no cue — keep the active tab
        // visible inside the scrollable pill row.
        if (active) b.scrollIntoView({ block: "nearest", inline: "center", behavior: "smooth" });
    });
    ADM_SECTIONS.forEach(s => {
        const el = document.getElementById("adm-screen-" + s);
        if (el) el.classList.toggle("hidden", s !== name);
    });
    if (typeof updateAdminSettingsSaveBar === "function") {
        updateAdminSettingsSaveBar(name !== "settings");
    }
    const pane = document.getElementById("tab-admin");
    if (pane) pane.scrollTop = 0;
    window.scrollTo({ top: 0, behavior: "smooth" });

    // Lazy-load each screen the first time it is opened.
    if (name === "overview") {
        if (!admStageTimingsData) loadAdminStageTimings(false);
        loadAdminAnalytics(30);
    }
    if (name === "payments") {
        if (!admLoadedSections.payments) { loadAdminData(); admLoadedSections.payments = true; }
        loadAdminOrders("");
        loadAdminRefunds();
    }
    if (name === "jobs") {
        loadAdminQueue();
        if (typeof loadAdminFleetStatus === "function") loadAdminFleetStatus();
    }
    if (name === "users" && !admLoadedSections.users) { loadAdminUsersRecent(); admLoadedSections.users = true; }
    if (name === "activity" && !admLoadedSections.activity) { loadAdminAudit(); admLoadedSections.activity = true; }
    if (name === "keys") loadAdminKeyHealth(false);
    if (name === "settings") { loadAdminSettings(); loadAdminPricing(); }
    if (name === "broadcast") {
        const ta = document.getElementById("adm-broadcast-text");
        if (ta && !ta.dataset.admCountBound) {
            ta.dataset.admCountBound = "1";
            ta.addEventListener("input", () => {
                const c = document.getElementById("adm-broadcast-count");
                if (c) c.innerText = ta.value.length;
            });
        }
    }
}
window.showAdminSection = showAdminSection;

function showAdminJobsTab(tab) {
    const live = document.getElementById("adm-jobs-live");
    const hist = document.getElementById("adm-jobs-history");
    const bLive = document.getElementById("adm-jobs-tab-live");
    const bHist = document.getElementById("adm-jobs-tab-history");
    if (live) live.classList.toggle("hidden", tab !== "live");
    if (hist) hist.classList.toggle("hidden", tab !== "history");
    if (bLive) bLive.classList.toggle("active", tab === "live");
    if (bHist) bHist.classList.toggle("active", tab === "history");
    if (tab === "history") {
        loadAdminHistory();
    } else {
        loadAdminQueue();
        if (typeof loadAdminFleetStatus === "function") loadAdminFleetStatus();
    }
}
window.showAdminJobsTab = showAdminJobsTab;

// ── Key Health (Batch BN) ────────────────────────────────────────────
// The app checks its own provider pools server-side (GET
// /api/admin/key-health, cached ~15 min; the Refresh button forces a
// fresh round with ?refresh=1). The payload never carries key
// material — indexes, labels, account names, statuses only.

let admKeyHealthData = null;
let admKeyHealthAccountsOpen = false;

async function loadAdminKeyHealth(force) {
    if (!state.user?.is_admin) return;
    const summary = document.getElementById("adm-keys-summary");
    const btn = document.getElementById("adm-keys-refresh-btn");
    if (force) {
        if (btn) btn.disabled = true;
        const checked = document.getElementById("adm-keys-checked-at");
        if (checked && admKeyHealthData) checked.innerText = t("adm_keys_checking");
    } else if (!admKeyHealthData && summary) {
        admStateLoading(summary);
    }
    try {
        const res = await fetchWithTimeout("/api/admin/key-health" + (force ? "?refresh=1" : ""), { headers: getAuthHeaders() });
        if (!res.ok) throw new Error("key-health " + res.status);
        admKeyHealthData = await res.json();
        renderAdminKeyHealth();
    } catch (e) {
        if (summary) admStateError(summary, "loadAdminKeyHealth(true)");
    } finally {
        if (btn) btn.disabled = false;
    }
}
window.loadAdminKeyHealth = loadAdminKeyHealth;

function admKeyPill(status, text) {
    const cls = status === "ok" ? "success" : (status === "limit" ? "running" : "failed");
    return `<span class="mini-tag adm-status-tag ${cls}"><span class="adm-key-dot" aria-hidden="true"></span>${escapeHtml(text)}</span>`;
}

function admKeyProviderRow(nameKey, pills, subText) {
    return `<div class="adm-key-provider-row">` +
        `<div class="adm-key-provider-head"><span class="adm-key-provider-name" title="${escapeHtml(t(nameKey))}">${escapeHtml(t(nameKey))}</span><span class="adm-key-provider-pills">${pills}</span></div>` +
        (subText ? `<p class="text-muted text-xs adm-key-provider-sub">${escapeHtml(subText)}</p>` : "") +
        `</div>`;
}

function renderAdminKeyHealth() {
    const data = admKeyHealthData;
    if (!data) return;
    const checked = document.getElementById("adm-keys-checked-at");
    if (checked) checked.innerText = admFmtDate(data.checked_at);
    const cached = document.getElementById("adm-keys-cached");
    if (cached) cached.classList.toggle("hidden", !data.cached);
    const p = data.providers || {};
    const rows = [];
    if (p.hf) {
        let pills = admKeyPill(p.hf.ok === p.hf.total ? "ok" : "dead", `${p.hf.ok}/${p.hf.total} ${t("adm_keys_working")}`);
        if (p.hf.dead > 0) pills += " " + admKeyPill("dead", `${p.hf.dead} ${t("adm_keys_dead")}`);
        if (p.hf.error > 0) pills += " " + admKeyPill("dead", `${p.hf.error} ${t("adm_keys_error")}`);
        const sub = `${t("adm_keys_quota_left")}: ${Math.round(p.hf.quota_remaining_s || 0).toLocaleString()} ${t("adm_keys_unit_s")}` +
            (p.hf.quota_exhausted > 0 ? ` · ${p.hf.quota_exhausted} ${t("adm_keys_exhausted")}` : "");
        rows.push(admKeyProviderRow("adm_keys_hf", pills, sub));
    }
    const keyRow = (prov, nameKey) => {
        if (!prov) return;
        let pills = admKeyPill(prov.ok === prov.total ? "ok" : "dead", `${prov.ok}/${prov.total} ${t("adm_keys_working")}`);
        if (prov.limit > 0) pills += " " + admKeyPill("limit", `${prov.limit} ${t("adm_keys_limit")}`);
        if (prov.dead > 0) pills += " " + admKeyPill("dead", `${prov.dead} ${t("adm_keys_dead")}`);
        rows.push(admKeyProviderRow(nameKey, pills, ""));
    };
    keyRow(p.gemini, "adm_keys_gemini");
    keyRow(p.groq, "adm_keys_groq");
    if (p.spaces) {
        const pills = admKeyPill(p.spaces.running === p.spaces.total ? "ok" : "dead", `${p.spaces.running}/${p.spaces.total} ${t("adm_keys_running")}`);
        rows.push(admKeyProviderRow("adm_keys_spaces", pills, ""));
    }
    const summary = document.getElementById("adm-keys-summary");
    if (summary) summary.innerHTML = rows.join("");
    const accBtn = document.getElementById("adm-keys-accounts-btn");
    if (accBtn) accBtn.classList.toggle("hidden", !(p.hf && p.hf.accounts && p.hf.accounts.length));
    renderAdminKeyHealthAccounts();
}

function admKeyRefillText(row) {
    if (row.remaining_s == null) return "—";
    if (!row.refill) return t("adm_keys_full");
    if (row.refill.method === "slope" && row.refill.refill_at) {
        return `${t("adm_keys_refill")}: ${admFmtDate(row.refill.refill_at)} (${t("adm_keys_est")})`;
    }
    return `${t("adm_keys_refill")}: ${t("adm_keys_refill_24h")}`;
}

function renderAdminKeyHealthAccounts() {
    const host = document.getElementById("adm-keys-accounts");
    if (!host) return;
    host.classList.toggle("hidden", !admKeyHealthAccountsOpen);
    if (!admKeyHealthAccountsOpen || !admKeyHealthData) return;
    const accounts = (admKeyHealthData.providers?.hf?.accounts) || [];
    host.innerHTML = accounts.map(row => {
        const name = row.account || ("#" + row.index);
        const remain = row.remaining_s == null ? "—"
            : `${Math.round(row.remaining_s).toLocaleString()} / ${Math.round(row.base_s || 0).toLocaleString()} ${t("adm_keys_unit_s")}`;
        const pill = admKeyPill(row.status === "ok" ? "ok" : "dead", row.status === "ok" ? t("adm_keys_working") : (row.status === "dead" ? t("adm_keys_dead") : t("adm_keys_error")));
        return `<div class="adm-key-row">` +
            `<div class="adm-key-main">` +
            `<span class="adm-key-name" title="${escapeHtml(name)}" onclick="this.classList.toggle('is-expanded')">${escapeHtml(name)}</span>` +
            `<div class="adm-key-meta"><span class="adm-key-quota">${escapeHtml(remain)}</span><span class="text-muted adm-key-refill">· ${escapeHtml(admKeyRefillText(row))}</span></div>` +
            `</div>` +
            `<div class="adm-key-status">${pill}</div>` +
            `</div>`;
    }).join("");
}

function toggleAdminKeyHealthAccounts() {
    admKeyHealthAccountsOpen = !admKeyHealthAccountsOpen;
    const btn = document.getElementById("adm-keys-accounts-btn");
    const span = btn ? btn.querySelector("span") : null;
    if (span) {
        const key = admKeyHealthAccountsOpen ? "adm_keys_accounts_hide" : "adm_keys_accounts_show";
        span.setAttribute("data-i18n", key);
        span.innerText = t(key);
    }
    renderAdminKeyHealthAccounts();
}
window.toggleAdminKeyHealthAccounts = toggleAdminKeyHealthAccounts;

// ── Overview + Payments ──────────────────────────────────────────────

function renderAdminOverview(stats) {
    if (!stats) return;
    window._lastAdminOverview = stats;
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.innerText = v; };
    set("adm-total-users", stats.total_users ?? 0);
    set("adm-total-users-split", tf("admin_stat_users_split", { real: stats.real_users ?? 0, never: stats.never_used_users ?? 0 }));
    set("adm-vip-users", stats.vip_users ?? 0);
    set("adm-pending-slips", stats.pending_slips ?? 0);
    set("adm-queued-jobs", stats.queued_jobs ?? 0);
    set("adm-failed-24h", stats.failed_jobs_24h ?? 0);
    set("adm-banned-users", stats.banned_users ?? 0);
    set("adm-banned-split", tf("admin_stat_banned_split", { admin: stats.banned_by_admin ?? 0, bot: stats.bot_blocked_users ?? 0 }));
    set("adm-new-24h", stats.new_users_24h ?? 0);
    const triageCard = document.getElementById("adm-triage-card");
    if (triageCard) {
        const pend = Number(stats.pending_slips || 0);
        const fail = Number(stats.failed_jobs_24h || 0);
        const isClear = pend === 0 && fail === 0;
        triageCard.classList.toggle("is-clear", isClear);
        const triageTitleEl = document.getElementById("adm-triage-title");
        if (triageTitleEl) {
            triageTitleEl.removeAttribute("data-i18n");
            triageTitleEl.innerText = isClear
                ? t("adm_triage_clear")
                : tf("adm_triage_action_needed", { slips: pend, failed: fail });
        }
        set("adm-triage-sub", tf("adm_triage_sub", { queued: stats.queued_jobs ?? 0, new24: stats.new_users_24h ?? 0 }));
    }
    const inc = stats.income || {};
    const setIncome = (id, mmk, count) => {
        set(id, (mmk || 0).toLocaleString() + " MMK");
        set(id + "-count", tf("admin_income_count", { count: count || 0 }));
    };
    setIncome("adm-income-today", inc.today_mmk, inc.today_count);
    setIncome("adm-income-7d", inc.last_7d_mmk, inc.last_7d_count);
    setIncome("adm-income-30d", inc.last_30d_mmk, inc.last_30d_count);
    setIncome("adm-income-all", inc.all_time_mmk, inc.all_time_count);
    const elSize = document.getElementById("adm-storage-size");
    if (elSize) elSize.innerText = admFmtBytes(stats.db_size_bytes) + " / " + admFmtBytes(stats.storage_bytes);
    const badge = document.getElementById("adm-nav-slips-badge");
    if (badge) {
        badge.innerText = stats.pending_slips ?? 0;
        badge.classList.toggle("hidden", !(stats.pending_slips > 0));
    }
    const chips = document.getElementById("adm-jobs-by-status");
    if (chips && stats.jobs_by_status) {
        const order = ["QUEUED", "RUNNING", "SUCCESS", "FAILED", "CANCELLED"];
        chips.innerHTML = order
            .filter(k => stats.jobs_by_status[k] !== undefined)
            .map(k => `<span class="mini-tag adm-status-tag ${k.toLowerCase()}">${escapeHtml(jobStatusText(k))}: <strong>${stats.jobs_by_status[k]}</strong></span>`)
            .join("");
    }
    const worker = document.getElementById("adm-worker-line");
    if (worker) {
        const rawWorker = stats.cloud_worker_status || "";
        const workerText = (!rawWorker || /online.*render cloud/i.test(rawWorker))
            ? t("adm_worker_online")
            : localizeServerText(rawWorker);
        worker.innerText = "☁️ " + workerText;
    }
    const actionsBox = document.getElementById("admin-recent-actions");
    if (actionsBox) {
        const acts = stats.recent_admin_actions || [];
        if (!acts.length) {
            actionsBox.innerHTML = `<p class="text-muted">—</p>`;
        } else {
            actionsBox.innerHTML = acts.map(a => `
                <div class="adm-action-row-line">
                    <span>• <b>${escapeHtml(a.action)}</b> → ${escapeHtml(a.target_type || "")} ${escapeHtml(String(a.target_id || ""))}</span>
                    <span class="text-muted adm-line-time">${admFmtDate(a.created_at)}</span>
                </div>`).join("");
        }
    }
}
window.renderAdminOverview = renderAdminOverview;
let admStageTimingsData = null;
let admStageTimingsInFlight = false;

function admFmtStageDuration(sec) {
    if (typeof sec !== "number" || !Number.isFinite(sec) || sec < 0) return "—";
    if (sec >= 60) {
        const m = Math.floor(sec / 60);
        const s = Math.round(sec - m * 60);
        return `${m}m ${s}s`;
    }
    return `${sec.toFixed(sec >= 10 ? 1 : 2)}s`;
}

async function loadAdminStageTimings(force) {
    if (!state.user?.is_admin) return;
    if (admStageTimingsInFlight && !force) return;
    admStageTimingsInFlight = true;
    const body = document.getElementById("adm-stage-timings-body");
    const btn = document.getElementById("adm-stage-timings-refresh-btn");
    if (force && btn) btn.disabled = true;
    else if (!admStageTimingsData && body) admStateLoading(body);
    try {
        const res = await fetchWithTimeout(
            "/api/admin/stage-timings" + (force ? "?refresh=1" : ""),
            { headers: getAuthHeaders() }
        );
        if (!res.ok) throw new Error("stage-timings " + res.status);
        admStageTimingsData = await res.json();
        renderAdminStageTimings();
    } catch (e) {
        if (body) admStateError(body, "loadAdminStageTimings(true)");
    } finally {
        admStageTimingsInFlight = false;
        if (btn) btn.disabled = false;
    }
}
window.loadAdminStageTimings = loadAdminStageTimings;

function renderAdminStageTimings() {
    const data = admStageTimingsData;
    const meta = document.getElementById("adm-stage-timings-meta");
    const body = document.getElementById("adm-stage-timings-body");
    if (!data || !body) return;
    if (meta) {
        const metaTxt = tf("adm_stage_timings_meta", {
            timed: data.jobs_with_timings ?? 0,
            scanned: data.jobs_scanned ?? 0,
            limit: data.sample_limit ?? 100,
        });
        const tsTxt = data.generated_at ? ` · ${admFmtDate(data.generated_at)}` : "";
        const cachedTxt = data.cached ? ` (${t("adm_stage_timings_cached")})` : "";
        meta.innerText = metaTxt + tsTxt + cachedTxt;
    }
    const tot = data.total || {};
    const stages = data.stages || {};
    const stageOrder = ["download", "transcribe", "translate", "tts", "sync", "png", "overlay", "mux"];
    const hasAnySample = (data.jobs_with_timings || 0) > 0 || (tot.count || 0) > 0;
    if (!hasAnySample) {
        admStateEmpty(body, "adm_stage_timings_empty");
        return;
    }
    const totStrip = `
        <div class="adm-stage-total-row">
            <span class="adm-stage-total-lbl">${escapeHtml(t("adm_stage_timings_total"))}</span>
            <span class="adm-stage-total-vals">${escapeHtml(t("adm_stage_timings_col_p50"))}: <b>${escapeHtml(admFmtStageDuration(tot.p50))}</b> · ${escapeHtml(t("adm_stage_timings_col_p95"))}: <b>${escapeHtml(admFmtStageDuration(tot.p95))}</b></span>
        </div>`;
    const rowsHtml = stageOrder.map(key => {
        const s = stages[key] || {};
        const lbl = t("adm_stage_lbl_" + key);
        const cntBadge = s.count ? ` <span class="text-muted">(${s.count})</span>` : "";
        return `
            <tr>
                <td>${escapeHtml(lbl)}${cntBadge}</td>
                <td class="adm-num">${escapeHtml(admFmtStageDuration(s.p50))}</td>
                <td class="adm-num">${escapeHtml(admFmtStageDuration(s.p95))}</td>
                <td class="adm-num">${escapeHtml(admFmtStageDuration(s.max))}</td>
            </tr>`;
    }).join("");
    body.innerHTML = `${totStrip}
        <div class="adm-stage-table-wrap">
            <table class="adm-stage-table">
                <thead>
                    <tr>
                        <th>${escapeHtml(t("adm_stage_timings_col_stage"))}</th>
                        <th class="adm-num">${escapeHtml(t("adm_stage_timings_col_p50"))}</th>
                        <th class="adm-num">${escapeHtml(t("adm_stage_timings_col_p95"))}</th>
                        <th class="adm-num">${escapeHtml(t("adm_stage_timings_col_max"))}</th>
                    </tr>
                </thead>
                <tbody>${rowsHtml}</tbody>
            </table>
        </div>`;
}
window.renderAdminStageTimings = renderAdminStageTimings;

async function loadAdminData() {
    if (!state.user?.is_admin) return;
    // Journey audit 2026-11-02: the stat cards ship "—" placeholders,
    // and a failed/slow overview fetch must say so — the old code left
    // literal "0" defaults standing, so the live Overview read as an
    // empty product (0 users / 0 VIP / 0 queue) while the bot-chat
    // dashboard showed 199 users / 146 jobs.
    const admOverviewError = () => {
        const host = document.getElementById("adm-overview-error");
        if (host) admStateError(host, "loadAdminData()");
    };
    const admOverviewClear = () => {
        const host = document.getElementById("adm-overview-error");
        if (host) host.innerHTML = "";
    };
    // B0: cached stage timings card on Overview (parallel, non-blocking).
    loadAdminStageTimings(false);
    loadAdminAnalytics(30);
    try {
        const resStats = await fetchWithTimeout("/api/admin/overview", { headers: getAuthHeaders() });
        if (resStats.ok) {
            admOverviewClear();
            renderAdminOverview(await resStats.json());
        } else {
            admOverviewError();
        }
    } catch (e) {
        console.warn("Error loading admin overview:", e);
        admOverviewError();
        // Not silent: the recent-actions card shows error + Retry.
        const actionsBox = document.getElementById("admin-recent-actions");
        if (actionsBox && !actionsBox.children.length) admStateError(actionsBox, loadAdminData);
    }

    // Pending payment slips
    const slipsContainer = document.getElementById("admin-slips-list");
    try {
        const resSlips = await fetchWithTimeout("/api/admin/pending-slips", { headers: getAuthHeaders() });
        if (resSlips.ok && slipsContainer) {
            renderAdminSlips(await resSlips.json());
        } else if (slipsContainer) {
            admStateError(slipsContainer, "loadAdminData()");
        }
    } catch (e) {
        if (slipsContainer) admStateError(slipsContainer, "loadAdminData()");
    }
}

function renderAdminSlips(slips) {
    window._lastAdminSlips = Array.isArray(slips) ? slips : [];
    const slipsContainer = document.getElementById("admin-slips-list");
    if (!slipsContainer) return;
    if (!window._lastAdminSlips.length) {
        admStateEmpty(slipsContainer, "admin_no_slips");
        return;
    }
    slipsContainer.innerHTML = "";
    window._lastAdminSlips.forEach(s => {
        const item = document.createElement("div");
        item.className = "slip-item-card";
        const slipThumb = s.slip_url
            ? `<img src="${escapeHtml(s.slip_url)}" class="slip-thumb" onclick="openSlipModal('${escapeHtml(s.slip_url)}')" title="${escapeHtml(t("adm_slip_img_title"))}">`
            : `<div class="slip-thumb" style="display:flex;align-items:center;justify-content:center;font-size:20px;opacity:.75;">🧾</div>`;
        item.innerHTML = `
            <div class="slip-item-row">
                ${slipThumb}
                <div class="slip-info">
                    <div class="slip-user adm-tap" onclick="openAdminUserDetail(${s.user_id})">${escapeHtml(s.user_name)}${s.username ? ` (@${escapeHtml(s.username)})` : ` (ID: ${s.user_id})`}</div>
                    <div class="slip-amount">${(s.amount_mmk || 0).toLocaleString()} MMK • ${escapeHtml(s.package_code)}</div>
                    <div class="slip-date">${escapeHtml(s.created_at)}</div>
                </div>
            </div>
            <div class="slip-actions">
                <button class="btn-approve" onclick="adminApproveSlip('${s.id}')">${t("btn_approve")}</button>
                <button class="btn-reject" onclick="adminRejectSlip('${s.id}')">${t("btn_reject")}</button>
            </div>
        `;
        slipsContainer.appendChild(item);
    });
    slipsContainer.querySelectorAll(".slip-thumb").forEach(async (imgEl) => {
        const authUrl = await fetchAuthImageUrl(imgEl.getAttribute("src"));
        if (authUrl) imgEl.src = authUrl;
    });
}

async function openSlipModal(url) {
    const modal = document.getElementById("slip-modal");
    const img = document.getElementById("modal-slip-img");
    if (modal && img) {
        const authUrl = await fetchAuthImageUrl(url);
        img.src = authUrl || url;
        modal.classList.remove("hidden");
    }
}

function closeSlipModal() {
    const modal = document.getElementById("slip-modal");
    if (modal) modal.classList.add("hidden");
}

function signedAdminHref(u) { return u && urlCarriesStreamToken(u) ? escapeHtml(u) : "#"; }

function openAdminMediaModal(jobId, jobType, downloadUrl, deliverablesJson, title) {
    haptic("light");
    const modal = document.getElementById("admin-media-modal");
    const badge = document.getElementById("admin-media-badge");
    const titleEl = document.getElementById("admin-media-title");
    const bodyEl = document.getElementById("admin-media-body");
    const delivEl = document.getElementById("admin-media-deliverables");
    if (!modal) return;

    let deliverables = {};
    try {
        deliverables = typeof deliverablesJson === "string" ? JSON.parse(deliverablesJson) : (deliverablesJson || {});
    } catch (e) {
        deliverables = {};
    }

    if (badge) {
        badge.className = `history-item-badge ${jobType === 'RECAP' ? 'badge-recap' : jobType === 'TTS' ? 'badge-tts' : 'badge-voice'}`;
        badge.innerText = jobType === 'RECAP' ? t("adm_badge_recap") : jobType === 'TTS' ? t("job_badge_tts") : t("adm_badge_voice");
    }
    if (titleEl) {
        titleEl.removeAttribute("data-i18n");
        titleEl.innerText = title || `${t("adm_job_id_prefix")}${jobId.slice(0, 10)}`;
    }

    // ONE availability decision for the whole card: the API only hands out
    // media URLs when the output file is actually on the server, so "no
    // URLs" means the media is gone — every control below follows this flag.
    const mediaAvailable = jobType === "RECAP"
        ? !!(deliverables.video || downloadUrl)
        : !!(deliverables.download || deliverables.audio || downloadUrl);

    if (bodyEl) {
        if (!mediaAvailable) {
            bodyEl.innerHTML = `
                <div class="modal-video-wrapper">
                    ${mediaGonePanelHtml(jobId)}
                </div>
            `;
        } else if (jobType === "RECAP") {
            // Batch CK: deliverables.video now points at the DOWNLOAD
            // route (attachment) for the modal's save button; the player
            // must keep a stream URL, so prefer downloadUrl (the signed
            // stream form) and fall back to deliverables.video.
            const videoUrl = downloadUrl || deliverables.video;
            bodyEl.innerHTML = `
                <div class="modal-video-wrapper">
                    <video id="admin-modal-video" data-auth-src="${videoUrl}" controls autoplay playsinline class="admin-modal-player-video"
                           onerror="handleAdminMediaError(this, '${jobId}', '${jobType}')"
                           onloadedmetadata="handleAdminMediaLoaded(this)">
                        ${escapeHtml(t("adm_media_no_video"))}
                    </video>
                </div>
            `;
        } else {
            const audioUrl = deliverables.audio || downloadUrl;
            bodyEl.innerHTML = `
                <div class="modal-audio-wrapper">
                    <div class="modal-audio-icon">🎧</div>
                    <audio id="admin-modal-audio" data-auth-src="${audioUrl}" controls autoplay class="admin-modal-player-audio"
                           onerror="handleAdminMediaError(this, '${jobId}', '${jobType}')"
                           onloadedmetadata="handleAdminMediaLoaded(this)">
                        ${escapeHtml(t("adm_media_no_audio"))}
                    </audio>
                </div>
            `;
        }
    }

    const modalMediaEl = bodyEl ? bodyEl.querySelector("video[data-auth-src], audio[data-auth-src]") : null;
    if (modalMediaEl) {
        const mediaElRef = modalMediaEl;
        setAuthMediaSource(mediaElRef, mediaElRef.getAttribute("data-auth-src")).then((ok) => {
            if (!ok) handleAdminMediaError(mediaElRef, jobId, jobType);
        });
    }

    if (delivEl) {
        let buttons = '';
        if (!mediaAvailable) {
            // Media gone: ALL download controls disabled together, so the
            // card can never show one live button next to a dead one.
            const dis = (label) => `<span class="mini-chip" style="opacity: 0.45; pointer-events: none;" aria-disabled="true">${escapeHtml(label)}</span>`;
            if (jobType === "RECAP") {
                buttons += dis(t("adm_media_dl_video"));
                buttons += dis(t("adm_media_dl_audio"));
                buttons += dis(t("adm_media_dl_srt"));
                buttons += dis(t("adm_media_dl_script"));
            } else {
                buttons += dis(t("adm_media_dl_voice"));
            }
            buttons += dis(t("dl_open_in_browser_tab"));
        } else if (jobType === "RECAP") {
            if (deliverables.video) {
                buttons += `<a href="${signedAdminHref(deliverables.video)}" download="${brandedDlName("Recap", "mp4", title, jobId)}" data-dl-url="${deliverables.video}" data-dl-name="${brandedDlName("Recap", "mp4", title, jobId)}" class="mini-chip gold">${escapeHtml(t("adm_media_dl_video"))}</a>`;
            }
            if (deliverables.audio) {
                buttons += `<a href="${signedAdminHref(deliverables.audio)}" download="${brandedDlName("RecapAudio", "mp3", title, jobId)}" data-dl-url="${deliverables.audio}" data-dl-name="${brandedDlName("RecapAudio", "mp3", title, jobId)}" class="mini-chip">${escapeHtml(t("adm_media_dl_audio"))}</a>`;
            }
            if (deliverables.srt) {
                buttons += `<a href="${signedAdminHref(deliverables.srt)}" download="${brandedDlName("Subtitles", "srt", title, jobId)}" data-dl-url="${deliverables.srt}" data-dl-name="${brandedDlName("Subtitles", "srt", title, jobId)}" class="mini-chip">${escapeHtml(t("adm_media_dl_srt"))}</a>`;
            }
            if (deliverables.script) {
                buttons += `<a href="${signedAdminHref(deliverables.script)}" download="${brandedDlName("Script", "txt", title, jobId)}" data-dl-url="${deliverables.script}" data-dl-name="${brandedDlName("Script", "txt", title, jobId)}" class="mini-chip">${escapeHtml(t("adm_media_dl_script"))}</a>`;
            }
        } else {
            const dlUrl = deliverables.download || downloadUrl;
            if (dlUrl) {
                buttons += `<a href="${signedAdminHref(dlUrl)}" download="${brandedDlName("Voice", "wav", null, jobId)}" data-dl-url="${dlUrl}" data-dl-name="${brandedDlName("Voice", "wav", null, jobId)}" class="mini-chip gold">${escapeHtml(t("adm_media_dl_voice"))}</a>`;
            }
        }
        if (mediaAvailable && downloadUrl) {
            buttons += `<a href="${signedAdminHref(downloadUrl)}" target="_blank" rel="noopener" data-open-url="${downloadUrl}" class="mini-chip" style="opacity: 0.85;">${t("dl_open_in_browser_tab")}</a>`;
        }
        delivEl.innerHTML = buttons;
        // Batch CK: wire these anchors DIRECTLY. The modal's content
        // wrapper carries onclick="event.stopPropagation()", so clicks
        // inside the modal never bubble to the document-level delegated
        // download handler — the buttons were dead on every device even
        // with correctly signed URLs (Batch CJ). A listener attached to
        // the anchor itself fires at the target regardless of the
        // ancestor's stopPropagation. Each handler flags the event with
        // __dlHandled so the delegated handler can never double-fire if
        // the wrapper's stopPropagation is ever removed. The wrapper's
        // stopPropagation itself stays: it keeps content clicks from
        // closing the modal via the backdrop handler.
        delivEl.querySelectorAll("a[data-dl-url]").forEach((a) => {
            a.addEventListener("click", (e) => {
                e.preventDefault();
                e.__dlHandled = true;
                downloadAuthFile(a.getAttribute("data-dl-url"), a.getAttribute("data-dl-name") || undefined, a);
            });
        });
        delivEl.querySelectorAll("a[data-open-url]").forEach((a) => {
            a.addEventListener("click", (e) => {
                e.preventDefault();
                e.__dlHandled = true;
                openAuthFile(a.getAttribute("data-open-url"));
            });
        });
    }

    modal.classList.remove("hidden");
}

// Admin Play/Preview buttons carry their job data in data-* attributes
// and are wired here after each render (stored-XSS fix, hunt #2 X1):
// job fields such as preset_voice are user-controlled, so they must
// never be interpolated into an inline onclick JS string — the old
// handlers escaped only double quotes, and a payload like
// x');alert(1);// broke straight out of the single-quoted argument.
function wireAdminMediaButtons(root) {
    if (!root) return;
    root.querySelectorAll(".adm-media-open-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            openAdminMediaModal(
                btn.dataset.jobId,
                btn.dataset.jobType,
                btn.dataset.downloadUrl,
                btn.dataset.deliverables,
                btn.dataset.title
            );
        });
    });
}

function closeAdminMediaModal() {
    const modal = document.getElementById("admin-media-modal");
    if (!modal) return;
    const bodyEl = document.getElementById("admin-media-body");
    if (bodyEl) {
        const vid = bodyEl.querySelector("video");
        if (vid) { vid.pause(); clearAuthMediaSource(vid); }
        const aud = bodyEl.querySelector("audio");
        if (aud) { aud.pause(); clearAuthMediaSource(aud); }
        bodyEl.innerHTML = "";
    }
    modal.classList.add("hidden");
}

// Shared "media gone" panel for the admin media modal. The copy states the
// real retention rule (files are kept on the server for 24 hours, then the
// retention sweep removes them) — it must never blame deploys or disk
// wipes: outputs live on the persistent disk and survive restarts.
// Regenerate is the single primary action here.
function mediaGonePanelHtml(jobId) {
    return `
        <div class="admin-media-err-banner mt-3 p-3 text-center" style="border-radius: 12px; border: 1px solid rgba(239, 68, 68, 0.35); background: rgba(239, 68, 68, 0.08);">
            <div style="font-size: 1.6rem; margin-bottom: 4px;">⌛</div>
            <strong style="color: #fca5a5; font-size: 13px;">${t("admin_media_gone_title")}</strong>
            <p class="text-xs text-muted mt-1" style="line-height: 1.5;">
                ${t("admin_media_gone_desc")}
            </p>
            <div class="mt-2" style="display: flex; gap: 8px; justify-content: center;">
                <button class="btn mini-btn secondary-btn" onclick="closeAdminMediaModal(); adminRetryJob('${jobId}');">
                    ${t("admin_media_gone_regen")}
                </button>
            </div>
        </div>
    `;
}

function handleAdminMediaError(mediaEl, jobId, jobType) {
    console.warn("Admin media failed to load (missing on server):", jobId);
    const wrapper = mediaEl.parentElement;
    if (wrapper) {
        mediaEl.style.display = "none";
        const oldErr = wrapper.querySelector(".admin-media-err-banner");
        if (!oldErr) {
            const errDiv = document.createElement("div");
            errDiv.innerHTML = mediaGonePanelHtml(jobId);
            wrapper.appendChild(errDiv.firstElementChild);
        }
    }
}

function handleAdminMediaLoaded(mediaEl) {
    const wrapper = mediaEl.parentElement;
    if (wrapper && mediaEl.duration && !isNaN(mediaEl.duration)) {
        let durDiv = wrapper.querySelector(".admin-media-dur-badge");
        if (!durDiv) {
            durDiv = document.createElement("div");
            durDiv.className = "admin-media-dur-badge text-xs text-muted mt-2 text-center";
            wrapper.appendChild(durDiv);
        }
        const m = Math.floor(mediaEl.duration / 60);
        const s = Math.floor(mediaEl.duration % 60);
        durDiv.innerText = tf("admin_media_duration_badge", { time: `${m}:${s < 10 ? '0' : ''}${s}`, sec: Math.round(mediaEl.duration) });
    }
}

// Admin error toasts: localized premium prefix first; the raw server
// detail is appended after " — " only when it adds diagnostic value
// (audit A4). Generic placeholder details ("Failed"/"Error") are noise
// and dropped.
function adminErrorText(prefixKey, raw) {
    const prefix = t(prefixKey);
    const detail = (typeof localizeServerText === "function" ? localizeServerText(raw == null ? "" : String(raw)) : (raw == null ? "" : String(raw))).trim();
    if (!detail || detail === "Failed" || detail === "Error" || detail === prefix) return prefix;
    return `${prefix} — ${detail}`;
}
window.adminErrorText = adminErrorText;

async function adminApproveSlip(reqId) {
    haptic("medium");
    showStudioConfirm(t("admin_confirm_approve"), async () => {
        try {
            const res = await fetch("/api/admin/approve-slip", {
                method: "POST",
                headers: getAuthHeaders(),
                body: JSON.stringify({ request_id: reqId }),
            });
            const data = await res.json();
            if (res.ok) {
                showStudioToast(data.message || t("admin_done"), 'success');
            } else {
                showStudioToast(adminErrorText("admin_generic_err", data.detail || data.message), 'error');
            }
            admLoadedSections.payments = false;
            loadAdminData();
        } catch (e) {
            showStudioToast(adminErrorText("admin_generic_err", e.message), 'error');
        }
    });
}

async function adminRejectSlip(reqId) {
    haptic("medium");
    // Premium modal-with-input (mirrors the ban-reason pattern) — the
    // native prompt dialog is gone from the Mini App. Same {request_id,
    // reason} payload as before.
    showStudioInputModal({
        title: t("admin_reject_title"),
        placeholder: t("admin_reject_reason_ph"),
        initialValue: t("admin_reject_default"),
        confirmLabel: t("admin_reject_btn"),
        onConfirm: async (reason) => {
            if (!reason) return;
            try {
                const res = await fetch("/api/admin/reject-slip", {
                    method: "POST",
                    headers: getAuthHeaders(),
                    body: JSON.stringify({ request_id: reqId, reason: reason }),
                });
                const data = await res.json();
                if (res.ok) {
                    showStudioToast(data.message || t("admin_done"), 'success');
                } else {
                    showStudioToast(adminErrorText("admin_generic_err", data.detail || data.message), 'error');
                }
                loadAdminData();
            } catch (e) {
                showStudioToast(adminErrorText("admin_generic_err", e.message), 'error');
            }
        },
    });
}

// ── Users: search, recent list, detail ───────────────────────────────

function _adminReason() {
    const el = document.getElementById("adm-adjust-reason");
    return el ? el.value.trim() : "";
}

function _adminRequireReason() {
    const reason = _adminReason();
    if (reason.length < 3) {
        showStudioToast(t("admin_reason_required"), "warning");
        const el = document.getElementById("adm-adjust-reason");
        if (el) { el.focus(); el.scrollIntoView({ behavior: "smooth", block: "center" }); }
        return null;
    }
    return reason;
}

// Dedicated Danger-Zone ban reason (premium polish P12): the ban flow no
// longer borrows the Adjust card's reason field. Same {reason} payload
// and AuditLog behaviour server-side.
function _adminBanReason() {
    const el = document.getElementById("adm-ban-reason");
    return el ? el.value.trim() : "";
}

function _adminRequireBanReason() {
    const reason = _adminBanReason();
    if (reason.length < 3) {
        showStudioToast(t("admin_reason_required"), "warning");
        const el = document.getElementById("adm-ban-reason");
        if (el) { el.focus(); el.scrollIntoView({ behavior: "smooth", block: "center" }); }
        return null;
    }
    return reason;
}

function admUserRowHtml(u) {
    // First grapheme, not charAt(0): names starting with an emoji or a
    // combining stack otherwise render a blank/broken avatar initial.
    const initial = escapeHtml((Array.from((u.first_name || "?").trim())[0] || "?").toUpperCase());
    const balFmt = Math.floor((u.balance_seconds || 0) / 60) + "m";
    // Sub-line as segments (same pattern as D-1): the "•" separators
    // are CSS-generated between segments, so a wrapped line never
    // strands one at a line end.
    const segs = [];
    if (u.username) segs.push(`@${escapeHtml(u.username)}`);
    segs.push(`ID: ${u.id}`);
    segs.push(escapeHtml(u.account_category || "FREE"));
    segs.push(`\u23f1 ${balFmt}`);
    segs.push(`\ud83c\udfac ${u.recap_credits ?? 0}`);
    if (u.is_banned) segs.push("\ud83d\udeab");
    const subHtml = segs.map(s => `<span class="adm-sub-seg">${s}</span>`).join("");
    // Two-line row (full-app audit A-3/A-4/A-6): line 1 is the name
    // (the only part allowed to ellipsize) beside badges that always
    // render in full; line 2 carries @username + the ID/usage sub-line.
    return `<div class="adm-user-row adm-tap" onclick="openAdminUserDetail(${u.id})">
        <div class="adm-avatar">${initial}</div>
        <div class="adm-user-main">
            <div class="adm-user-line1">
                <span class="adm-user-name">${escapeHtml(u.first_name || "-")}</span>
                ${u.account_category === "PURCHASED" ? `<span class="badge-vip">👑 VIP</span>` : ""}
                <span class="badge-status ${u.is_banned ? "blocked" : (u.bot_blocked ? "botblocked" : "active")}">${escapeHtml(u.is_banned ? t("admin_status_banned") : (u.bot_blocked ? t("admin_status_bot_blocked") : t("admin_status_active")))}</span>
            </div>
            <div class="adm-user-sub">${subHtml}</div>
        </div>
        <span class="adm-row-chev">\u203a</span>
    </div>`;
}

// ── Admin Users list state (A-6): category filter + offset paging ──
// The filter is applied SERVER-side via the endpoint's category param
// (187+ users — filtering one fetched page client-side would lie).
let admUsersCategory = "";   // "" | "FREE" | "PURCHASED"
let admUsersOffset = 0;
const ADM_USERS_PAGE = 20;

function admUsersCategoryParam() {
    return admUsersCategory ? `&category=${encodeURIComponent(admUsersCategory)}` : "";
}

function syncAdminUsersChips() {
    document.querySelectorAll(".adm-users-filter-chip").forEach(c => {
        c.classList.toggle("active", (c.getAttribute("data-cat") || "") === admUsersCategory);
    });
}

function setAdminUsersCategory(cat) {
    admUsersCategory = cat || "";
    admUsersOffset = 0;
    syncAdminUsersChips();
    loadAdminUsersRecent();
    // An on-screen search re-runs under the same filter.
    const qEl = document.getElementById("adm-user-search-q");
    if (qEl && qEl.value.trim().length >= 2) adminSearchUsers();
}
window.setAdminUsersCategory = setAdminUsersCategory;

function loadMoreAdminUsers() {
    admUsersOffset += ADM_USERS_PAGE;
    loadAdminUsersRecent(true);
}
window.loadMoreAdminUsers = loadMoreAdminUsers;

// Overview's VIP stat card: land on Users with the Paid filter active.
function openAdminUsersPaid() {
    admUsersCategory = "PURCHASED";
    admUsersOffset = 0;
    syncAdminUsersChips();
    showAdminSection("users");
    loadAdminUsersRecent();
}
window.openAdminUsersPaid = openAdminUsersPaid;

async function adminSearchUsers() {
    const qEl = document.getElementById("adm-user-search-q");
    const box = document.getElementById("admin-user-search-results");
    const q = qEl ? qEl.value.trim() : "";
    if (q.length < 2) { showStudioToast(t("admin_search_short"), "warning"); return; }
    admStateLoading(box);
    try {
        const res = await fetchWithTimeout(`/api/admin/users/search?q=${encodeURIComponent(q)}${admUsersCategoryParam()}`, { headers: getAuthHeaders() });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Search failed");
        if (!box) return;
        if (!data.users.length) {
            window._lastAdminUsersSearch = [];
            admStateEmpty(box, "admin_search_none");
            return;
        }
        window._lastAdminUsersSearch = data.users;
        box.innerHTML = data.users.map(admUserRowHtml).join("");
    } catch (e) {
        admStateError(box, "adminSearchUsers()");
    }
}
window.adminSearchUsers = adminSearchUsers;

async function loadAdminUsersRecent(append = false) {
    const box = document.getElementById("admin-users-recent");
    if (!box) return;
    const moreBtn = document.getElementById("adm-users-load-more");
    if (!append) {
        admUsersOffset = 0;
        admStateLoading(box);
        if (moreBtn) moreBtn.classList.add("hidden");
    }
    try {
        const res = await fetchWithTimeout(`/api/admin/users?limit=${ADM_USERS_PAGE}&offset=${admUsersOffset}${admUsersCategoryParam()}`, { headers: getAuthHeaders() });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        if (!data.users.length) {
            if (append) { if (moreBtn) moreBtn.classList.add("hidden"); return; }
            window._lastAdminUsersList = [];
            admStateEmpty(box, "admin_empty");
            return;
        }
        window._lastAdminUsersList = append && Array.isArray(window._lastAdminUsersList)
            ? window._lastAdminUsersList.concat(data.users)
            : data.users;
        const rowsHtml = data.users.map(admUserRowHtml).join("");
        if (append) box.insertAdjacentHTML("beforeend", rowsHtml);
        else box.innerHTML = rowsHtml;
        // More pages exist while the server reports un-fetched rows.
        const total = typeof data.total === "number" ? data.total : admUsersOffset + data.users.length;
        if (moreBtn) moreBtn.classList.toggle("hidden", admUsersOffset + data.users.length >= total);
    } catch (e) {
        if (append) { showStudioToast(t("admin_load_error"), "error"); return; }
        admStateError(box, "loadAdminUsersRecent()");
    }
}
window.loadAdminUsersRecent = loadAdminUsersRecent;

async function openAdminUserDetail(uid) {
    admCurrentUserId = uid;
    _admAdjustIdemKey = _newAdjustIdemKey();
    haptic("light");
    const main = document.getElementById("adm-users-main");
    const detail = document.getElementById("adm-user-detail");
    if (main) main.classList.add("hidden");
    if (detail) detail.classList.remove("hidden");
    window.scrollTo({ top: 0, behavior: "smooth" });
    const nameEl = document.getElementById("adm-detail-name");
    if (nameEl) nameEl.innerText = t("admin_loading");
    try {
        const res = await fetchWithTimeout(`/api/admin/users/${uid}`, { headers: getAuthHeaders() });
        const u = await res.json();
        if (!res.ok) throw new Error(u.detail || "Failed");
        renderAdminUserDetail(u);
    } catch (e) {
        showStudioToast(adminErrorText("admin_generic_err", e.message), "error");
        closeAdminUserDetail();
        return;
    }
    loadAdminUserJobs(uid);
}
window.openAdminUserDetail = openAdminUserDetail;

function renderAdminUserDetail(u) {
    if (!u) return;
    window._lastAdminUserDetail = u;
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.innerText = v; };
    set("adm-detail-avatar", (u.first_name || "U").slice(0, 1).toUpperCase());
    set("adm-detail-name", u.first_name || "User");
    set("adm-detail-sub", (u.username ? "@" + u.username + " • " : "") + "ID: " + u.id);
    const st = document.getElementById("adm-detail-status");
    if (st) {
        st.className = "badge-status " + (u.is_banned ? "failed" : (u.bot_blocked ? "botblocked" : "success"));
        st.innerText = u.is_banned ? t("admin_status_banned") : (u.bot_blocked ? t("admin_status_bot_blocked") : t("admin_status_active"));
    }
    const meta = document.getElementById("adm-detail-meta");
    if (meta) {
        const activeJobs = Number(u.jobs_active || 0);
        meta.innerHTML = `
            <span class="mini-tag">${u.account_category === "PURCHASED" ? "👑 VIP" : "FREE"}</span>
            <span class="mini-tag">${t("admin_joined")}: ${admFmtDate(u.created_at)}</span>
            ${u.language_code ? `<span class="mini-tag">${escapeHtml(u.language_code.toUpperCase())}</span>` : ""}
            ${u.is_test_account ? `<span class="mini-tag">${t("admin_badge_test_acct")}</span>` : ""}
            ${activeJobs > 0 ? `<span class="mini-tag">${t("admin_badge_active_jobs")}: ${activeJobs}</span>` : ""}`;
    }
    const mins = Math.floor((u.balance_seconds || 0) / 60);
    const secs = (u.balance_seconds || 0) % 60;
    set("adm-detail-balance", mins + "m " + secs + "s");
    set("adm-detail-credits", String(u.recap_credits ?? 0));
    set("adm-detail-jobs", `${u.jobs_total ?? 0} (${u.jobs_success ?? 0}✓ / ${u.jobs_failed ?? 0}✗)`);
    set("adm-detail-lastjob", admFmtDate(u.last_job_at));
    // Reset adjust form
    ["adm-adj-minutes", "adm-adj-seconds", "adm-adj-credits"].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = 0;
    });
    const tier = document.getElementById("adm-adj-tier");
    if (tier) tier.value = "";
    // Batch BC-1: reflect the test-account flag on its toggle.
    const testToggle = document.getElementById("adm-test-flag-toggle");
    if (testToggle) testToggle.checked = !!u.is_test_account;
    renderAdminBanZone(u);
    // Batch CD: the completed detail sections + a fresh message box.
    renderAdminDetailExtras(u);
    const msgText = document.getElementById("adm-msg-text");
    if (msgText) msgText.value = "";
    const msgNote = document.getElementById("adm-msg-note");
    if (msgNote) {
        if (u.is_banned) {
            msgNote.innerText = t("admin_msg_banned_note");
            msgNote.classList.remove("hidden");
        } else if (u.bot_blocked) {
            msgNote.innerText = t("admin_msg_blocked_note");
            msgNote.classList.remove("hidden");
        } else {
            msgNote.innerText = "";
            msgNote.classList.add("hidden");
        }
    }
}

function admFmtSecs(total) {
    const s = Math.max(0, Math.round(total || 0));
    return Math.floor(s / 60) + "m " + (s % 60) + "s";
}

// Batch CD Item 1: render the completed-detail sections (balance
// buckets, TTS today, verification, referrals, recent jobs,
// payments, ledger, recorded private-AI chats) from the payload.
function renderAdminDetailExtras(u) {
    const row = (label, val) => `<div class="adm-bc-row"><span>${label}</span><strong>${val}</strong></div>`;
    const bb = u.balance_breakdown || {};
    const bd = document.getElementById("adm-detail-breakdown");
    if (bd) bd.innerHTML =
        row(t("admin_bal_monthly"), admFmtSecs(bb.monthly_free_seconds)) +
        row(t("admin_bal_welcome"), admFmtSecs(bb.welcome_seconds)) +
        row(t("admin_bal_referral"), admFmtSecs(bb.referral_seconds)) +
        row(t("admin_bal_purchased"), admFmtSecs(bb.purchased_seconds)) +
        row(t("admin_bal_consumed"), admFmtSecs(bb.consumed_seconds)) +
        row(t("admin_custom_voices"), String(u.custom_voices_count ?? 0));
    const gEl = document.getElementById("adm-detail-grants");
    if (gEl) gEl.innerHTML = `<div class="text-xs font-bold mt-2">${escapeHtml(t("adm_grants_lbl"))}</div>${renderGrantsRowsHtml(u.wallet_grants || [])}`;
    const tt = u.tts_today || {};
    const ttsEl = document.getElementById("adm-detail-tts");
    if (ttsEl) {
        ttsEl.innerHTML = (tt.consumed_seconds == null)
            ? row(t("admin_tts_today"), "∞")
            : row(t("admin_tts_today"), `${t("admin_tts_used")} ${admFmtSecs(tt.consumed_seconds)} · ${t("admin_tts_left")} ${admFmtSecs(tt.remaining_seconds)}`);
    }
    // Batch CV: BYOK Gemini key presence (masked last4 only).
    const gk = u.gemini_key || {};
    const gkEl = document.getElementById("adm-detail-gemini-key");
    if (gkEl) {
        let gkVal;
        if (!gk.has_key) {
            gkVal = t("admin_gemini_key_none");
        } else if (gk.state === "invalid") {
            gkVal = `••••${escapeHtml(gk.last4 || "")} · ${t("admin_gemini_key_invalid")}`;
        } else {
            gkVal = `••••${escapeHtml(gk.last4 || "")} · ${t("admin_gemini_key_ok")}`;
        }
        gkEl.innerHTML = row(t("admin_gemini_key"), gkVal);
    }
    const vf = u.verification || {};
    const vEl = document.getElementById("adm-detail-verify");
    if (vEl) vEl.innerHTML =
        `<span class="mini-tag">${t("admin_verified_channel")}: ${vf.channel_verified ? t("admin_verified_yes") : t("admin_verified_no")}</span>` +
        `<span class="mini-tag">${t("admin_verified_group")}: ${vf.group_verified ? t("admin_verified_yes") : t("admin_verified_no")}</span>`;
    const rf = u.referrals || {};
    const rEl = document.getElementById("adm-detail-referrals");
    if (rEl) {
        let html = row(t("admin_referrals_made"), String(rf.referred_count ?? 0)) +
            row(t("admin_referral_earned"), admFmtSecs(rf.referral_earned_seconds));
        if (rf.referred_by) {
            html += row(t("admin_referred_by"), escapeHtml((rf.referred_by.first_name || "") + (rf.referred_by.username ? " (@" + rf.referred_by.username + ")" : "") + " · ID " + rf.referred_by.id));
        }
        rEl.innerHTML = html;
    }
    const jobs = u.recent_jobs || [];
    const jEl = document.getElementById("adm-detail-recent-jobs");
    if (jEl) jEl.innerHTML = jobs.length ? jobs.map(j => `
            <div class="adm-job-mini">
                <span class="adm-job-mini-main">${escapeHtml(j.job_type || "")} · ${escapeHtml(j.status || "")}</span>
                <span class="text-muted text-xs">${admFmtDate(j.created_at)} · ${admFmtSecs(j.seconds_consumed || j.seconds_estimated || 0)}</span>
            </div>`).join("") : `<p class="text-muted">${t("admin_user_jobs_empty")}</p>`;
    const pays = u.payments || [];
    const pEl = document.getElementById("adm-detail-payments");
    if (pEl) pEl.innerHTML = pays.length ? pays.map(p => `
            <div class="adm-job-mini">
                <span class="adm-job-mini-main">${escapeHtml(p.plan || "")}</span>
                <span class="history-item-badge status-${(p.status || "").toLowerCase()}">${escapeHtml(p.status || "")}</span>
                <span class="text-muted text-xs">${Number(p.amount_mmk || 0).toLocaleString("en-US")} MMK · ${admFmtDate(p.created_at)}</span>
            </div>`).join("") : `<p class="text-muted">${t("admin_payments_empty")}</p>`;
    const ptEl = document.getElementById("adm-detail-payments-total");
    if (ptEl) ptEl.innerHTML = row(t("admin_payments_total"), Number(u.payments_total_mmk || 0).toLocaleString("en-US") + " MMK");
    const led = u.ledger_recent || [];
    const lEl = document.getElementById("adm-detail-ledger");
    if (lEl) lEl.innerHTML = led.length ? led.map(e => `
            <div class="adm-job-mini">
                <span class="adm-job-mini-main">${escapeHtml(e.kind || "")}${e.reason ? " · " + escapeHtml(e.reason) : ""}</span>
                <span class="text-muted text-xs">${(e.delta >= 0 ? "+" : "-") + admFmtSecs(Math.abs(e.delta || 0))} · ${admFmtDate(e.created_at)}</span>
            </div>`).join("") : `<p class="text-muted">${t("admin_ledger_empty")}</p>`;
    // Batch CU: recap-credit movements (their own ledger table).
    const rLed = u.recap_ledger_recent || [];
    const rlEl = document.getElementById("adm-detail-recap-ledger");
    if (rlEl) rlEl.innerHTML = rLed.length ? rLed.map(e => `
            <div class="adm-job-mini">
                <span class="adm-job-mini-main">${escapeHtml(e.kind || "")}${e.reason ? " · " + escapeHtml(e.reason) : ""}</span>
                <span class="text-muted text-xs">${(e.delta >= 0 ? "+" : "-") + Math.abs(e.delta || 0)} cr · ${admFmtDate(e.created_at)}</span>
            </div>`).join("") : `<p class="text-muted">${t("admin_recap_ledger_empty")}</p>`;
    const chats = u.ai_chats || [];
    const cEl = document.getElementById("adm-detail-ai-chats");
    if (cEl) cEl.innerHTML = chats.length ? chats.map(m => `
            <div class="adm-chat-row ${m.direction === "user" ? "adm-chat-user" : "adm-chat-ai"}">
                <span class="adm-chat-who">${m.direction === "user" ? t("admin_ai_chat_user") : t("admin_ai_chat_ai")}</span>
                <span class="adm-chat-text">${escapeHtml(m.text || "")}</span>
                <span class="adm-chat-time">${admFmtDate(m.created_at)}</span>
            </div>`).join("") : `<p class="text-muted">${t("admin_ai_chats_empty")}</p>`;
}

// Batch CD Item 2: admin → this user direct message (inline confirm,
// same grammar as the broadcast send).
async function adminSendUserMessage() {
    const uid = admCurrentUserId;
    if (!uid) { showStudioToast(t("admin_no_user"), "warning"); return; }
    const textEl = document.getElementById("adm-msg-text");
    const msg = textEl ? textEl.value.trim() : "";
    if (!msg) { showStudioToast(t("admin_msg_empty"), "warning"); return; }
    haptic("medium");
    showStudioConfirm(t("admin_msg_confirm"), async () => {
        const btn = document.getElementById("adm-msg-send-btn");
        if (btn) btn.disabled = true;
        try {
            const res = await fetch(`/api/admin/users/${uid}/message`, {
                method: "POST",
                headers: getAuthHeaders(),
                body: JSON.stringify({ text: msg }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                if (data.detail === "bot_blocked") {
                    showStudioToast(t("admin_msg_blocked"), "error");
                    return;
                }
                if (data.detail === "chat_not_found") {
                    showStudioToast(t("admin_msg_unreachable"), "error");
                    return;
                }
                throw new Error(data.detail || t("admin_msg_send_failed"));
            }
            showStudioToast(t("admin_msg_sent"), "success");
            if (textEl) textEl.value = "";
        } catch (e) {
            showStudioToast(adminErrorText("admin_generic_err", e.message), "error");
        } finally {
            if (btn) btn.disabled = false;
        }
    });
}
window.adminSendUserMessage = adminSendUserMessage;

// Batch BC-1: set/unset the test-account flag (audited server-side).
// On failure the toggle snaps back so the UI never claims a state the
// server did not record.
async function adminToggleTestFlag(checked) {
    const uid = admCurrentUserId;
    const toggle = document.getElementById("adm-test-flag-toggle");
    if (!uid) { if (toggle) toggle.checked = !checked; return; }
    haptic("light");
    try {
        const res = await fetch(`/api/admin/users/${uid}/test-flag`, {
            method: "POST",
            headers: getAuthHeaders(),
            body: JSON.stringify({ is_test_account: !!checked }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        showStudioToast(t(checked ? "admin_test_flag_on" : "admin_test_flag_off"), "success");
        loadAdminData();
    } catch (e) {
        if (toggle) toggle.checked = !checked;
        showStudioToast(adminErrorText("admin_generic_err", e.message), "error");
    }
}
window.adminToggleTestFlag = adminToggleTestFlag;

function renderAdminBanZone(u) {
    const zone = document.getElementById("adm-detail-ban-zone");
    if (!zone) return;
    zone.innerHTML = u.is_banned
        ? `<button class="btn secondary-btn adm-wide-btn" onclick="adminUnbanUser(${u.id})">${t("btn_unban_user")}</button>`
        : `<button class="btn adm-danger-btn adm-wide-btn" onclick="adminBanUser(${u.id})">${t("btn_ban_user")}</button>`;
}

function closeAdminUserDetail() {
    admCurrentUserId = null;
    const main = document.getElementById("adm-users-main");
    const detail = document.getElementById("adm-user-detail");
    if (detail) detail.classList.add("hidden");
    if (main) main.classList.remove("hidden");
}
window.closeAdminUserDetail = closeAdminUserDetail;

function adminPickUser(uid) { openAdminUserDetail(uid); }
window.adminPickUser = adminPickUser;

function admFillUser(userId) {
    showAdminSection("users");
    openAdminUserDetail(userId);
}
window.admFillUser = admFillUser;

function adminAdjustPreset(minutes, seconds, credits, tier) {
    const m = document.getElementById("adm-adj-minutes");
    const s = document.getElementById("adm-adj-seconds");
    const c = document.getElementById("adm-adj-credits");
    const tEl = document.getElementById("adm-adj-tier");
    if (m) m.value = minutes;
    if (s) s.value = seconds;
    if (c) c.value = credits;
    if (tEl && tier !== undefined) tEl.value = tier;
    haptic("light");
}
window.adminAdjustPreset = adminAdjustPreset;

async function adminApplyAdjust() {
    const uid = admCurrentUserId;
    if (!uid) { showStudioToast(t("admin_no_user"), "warning"); return; }
    const minutes = parseFloat(document.getElementById("adm-adj-minutes")?.value || "0") || 0;
    const seconds = parseInt(document.getElementById("adm-adj-seconds")?.value || "0", 10) || 0;
    const credits = parseFloat(document.getElementById("adm-adj-credits")?.value || "0") || 0;
    const tier = document.getElementById("adm-adj-tier")?.value || "";
    if (!tier && minutes === 0 && seconds === 0 && credits === 0) {
        showStudioToast(t("admin_nothing_to_adjust"), "warning");
        return;
    }
    const reason = _adminRequireReason();
    if (reason === null) return;
    const isNegative = (minutes * 60 + seconds) < 0 || credits < 0;
    const doApply = async () => {
        haptic("medium");
        // In-flight guard (hunt #2 R4): a double-tap on Apply would
        // otherwise credit/debit twice — disable the button for the
        // duration of the request, mirroring submitPaymentSlip.
        const applyBtn = document.getElementById("adm-adjust-apply-btn");
        if (applyBtn) applyBtn.disabled = true;
        try {
            const vdaysEl = document.getElementById("adm-adj-vdays");
            const validity_days = vdaysEl ? (parseInt(vdaysEl.value || "30", 10) || 0) : 30;
            const payload = { user_id: uid, add_minutes: minutes, add_seconds: seconds, add_recap_credits: credits, validity_days, reason };
            if (!_admAdjustIdemKey) _admAdjustIdemKey = _newAdjustIdemKey();
            payload.idempotency_key = _admAdjustIdemKey;
            if (tier) payload.account_category = tier;
            const res = await fetch("/api/admin/adjust-user", {
                method: "POST",
                headers: getAuthHeaders(),
                body: JSON.stringify(payload),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || "Failed");
            _admAdjustIdemKey = null; // applied — the next apply is a new intent
            showStudioToast(data.message, "success");
            const reasonEl = document.getElementById("adm-adjust-reason");
            if (reasonEl) reasonEl.value = "";
            openAdminUserDetail(uid);
            loadAdminData();
        } catch (e) {
            showStudioToast(adminErrorText("admin_generic_err", e.message), "error");
        } finally {
            if (applyBtn) applyBtn.disabled = false;
        }
    };
    if (isNegative) {
        showStudioConfirm(t("admin_confirm_negative_adjust"), doApply);
    } else {
        await doApply();
    }
}
window.adminApplyAdjust = adminApplyAdjust;

async function _adminBanCall(uid, ban) {
    const reason = ban ? _adminRequireBanReason() : _adminBanReason();
    if (ban && reason === null) return;
    const doCall = async () => {
        haptic("medium");
        try {
            const res = await fetch(`/api/admin/users/${uid}/${ban ? "ban" : "unban"}`, {
                method: "POST",
                headers: getAuthHeaders(),
                body: JSON.stringify({ reason: reason || "" }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || "Failed");
            showStudioToast(data.message, "success");
            if (ban) {
                const reasonEl = document.getElementById("adm-ban-reason");
                if (reasonEl) reasonEl.value = "";
            }
            if (admCurrentUserId === uid) openAdminUserDetail(uid);
            loadAdminUsersRecent();
            loadAdminData();
        } catch (e) {
            showStudioToast(adminErrorText("admin_generic_err", e.message), "error");
        }
    };
    if (ban) {
        showStudioConfirm(t("admin_confirm_ban"), doCall);
    } else {
        await doCall();
    }
}

async function adminBanUser(uid) { await _adminBanCall(uid, true); }
window.adminBanUser = adminBanUser;
async function adminUnbanUser(uid) { await _adminBanCall(uid, false); }
window.adminUnbanUser = adminUnbanUser;

function renderAdminUserJobsList(jobs) {
    const box = document.getElementById("adm-user-jobs");
    if (!box) return;
    window._lastAdminUserJobs = Array.isArray(jobs) ? jobs : [];
    if (!window._lastAdminUserJobs.length) { admStateEmpty(box, "admin_user_jobs_empty"); return; }
    box.innerHTML = window._lastAdminUserJobs.map(j => `
        <div class="adm-job-mini">
            <span class="history-item-badge ${j.job_type === 'RECAP' ? 'badge-recap' : j.job_type === 'TTS' ? 'badge-tts' : 'badge-voice'}">${j.job_type === 'RECAP' ? '🎬' : j.job_type === 'TTS' ? '🎧' : '🎙️'}</span>
            <span class="adm-job-mini-main">${escapeHtml(j.input_snippet || j.preset_voice || "—")}</span>
            <span class="history-item-badge status-${(j.status || "").toLowerCase()}">${escapeHtml(jobStatusText(j.status))}</span>
            <span class="text-muted text-xs">${escapeHtml(j.time_ago || "")}</span>
        </div>`).join("");
}

async function loadAdminUserJobs(uid) {
    const box = document.getElementById("adm-user-jobs");
    if (!box) return;
    admStateLoading(box);
    try {
        const params = new URLSearchParams({ timeframe: "all", status: "all", job_type: "all", limit: "20", user_id: String(uid) });
        const res = await fetchWithTimeout(`/api/admin/history?${params.toString()}`, { headers: getAuthHeaders() });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        renderAdminUserJobsList(data.history || []);
    } catch (e) {
        admStateError(box, `loadAdminUserJobs(${uid})`);
    }
}
window.loadAdminUserJobs = loadAdminUserJobs;


// ── Broadcast ────────────────────────────────────────────────────────

async function adminSendBroadcast() {
    const textEl = document.getElementById("adm-broadcast-text");
    const btn = document.getElementById("adm-broadcast-btn");
    const btnText = document.getElementById("adm-broadcast-btn-text");
    const reportBox = document.getElementById("adm-broadcast-report");

    const msg = textEl ? textEl.value.trim() : "";
    if (!msg) {
        showStudioToast(t("admin_broadcast_empty"), 'info');
        return;
    }
    haptic("heavy");
    showStudioConfirm(t("admin_confirm_broadcast"), async () => {
        if (btn) btn.disabled = true;
        if (btnText) btnText.textContent = t("admin_broadcast_sending");
        if (reportBox) {
            reportBox.classList.remove("hidden");
            reportBox.innerHTML = `<div class="adm-state"><span class="spinner"></span><span>${t("admin_broadcast_sending")}</span></div>`;
        }
        try {
            const res = await fetch("/api/admin/broadcast", {
                method: "POST",
                headers: getAuthHeaders(),
                body: JSON.stringify({ message: msg }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || "Broadcast request failed");

            const bd = data.breakdown || {};
            const row = (label, val) => `<div class="adm-bc-row"><span>${label}</span><strong>${val}</strong></div>`;
            reportBox.innerHTML = `
                <div class="adm-bc-title">📢 ${t("admin_broadcast_report")}</div>
                <div class="adm-bc-grid">
                    <div class="adm-bc-cell"><span>${t("admin_bc_total")}</span><strong>${data.total_users_in_db || 0}</strong></div>
                    <div class="adm-bc-cell"><span>${t("admin_bc_targeted")}</span><strong>${data.total_targeted || 0}</strong></div>
                    <div class="adm-bc-cell ok"><span>${t("admin_bc_delivered")}</span><strong>${data.sent_count || 0}</strong></div>
                    <div class="adm-bc-cell bad"><span>${t("admin_bc_failed")}</span><strong>${data.failed_count || 0}</strong></div>
                </div>
                <div class="adm-bc-detail">
                    ${row(t("admin_bc_blocked"), bd.blocked_by_user || 0)}
                    ${row(t("admin_bc_deactivated"), bd.deactivated_or_not_found || 0)}
                    ${row(t("admin_bc_testids"), bd.invalid_test_id || 0)}
                    ${bd.other_errors ? row(t("admin_bc_other"), bd.other_errors) : ""}
                </div>
                ${bd.pre_existing_blocked_in_db ? `<div class="adm-bc-note">${row(t("admin_bc_preblocked"), bd.pre_existing_blocked_in_db)}</div>` : ""}`;
            showStudioToast(`${t("admin_bc_delivered")}: ${data.sent_count} / ${data.total_users_in_db}`, 'success');
            if (textEl) {
                textEl.value = "";
                const c = document.getElementById("adm-broadcast-count");
                if (c) c.innerText = "0";
            }
        } catch (e) {
            if (reportBox) reportBox.innerHTML = `<div class="adm-state adm-state-error"><span>❌ ${escapeHtml(e.message)}</span></div>`;
            showStudioToast(adminErrorText("admin_generic_err", e.message), 'error');
        } finally {
            if (btn) btn.disabled = false;
            if (btnText) btnText.textContent = t("btn_send_broadcast");
        }
    });
}
window.adminSendBroadcast = adminSendBroadcast;

// ── Live queue ───────────────────────────────────────────────────────

let adminQueueStatusFilter = "all";
let adminQueueEngineFilter = "all";

function setQueueStatusFilter(status) {
    adminQueueStatusFilter = status;
    document.querySelectorAll("#queue-status-filters .filter-pill").forEach(btn => {
        btn.classList.toggle("active", btn.getAttribute("data-status") === status);
    });
    loadAdminQueue();
}
window.setQueueStatusFilter = setQueueStatusFilter;

function setQueueEngineFilter(engine) {
    adminQueueEngineFilter = engine;
    loadAdminQueue();
}
window.setQueueEngineFilter = setQueueEngineFilter;

function renderAdminQueueData(data) {
    if (!data) return;
    window._lastAdminQueue = data;
    if (data.worker && typeof renderAdminFleetUI === "function") {
        renderAdminFleetUI({ vps_worker: data.worker });
    }
    const container = document.getElementById("admin-queue-list");
    const bannerEl = document.getElementById("admin-queue-status-banner");
    if (!container) return;

    const c = data.counts || { total_listed: 0, queued: 0, running: 0, success: 0, failed: 0 };
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.innerText = v; };
    set("cnt-total", c.total_listed);
    set("cnt-queued", c.queued);
    set("cnt-running", c.running);
    set("cnt-waiting-choice", c.waiting_choice ?? 0);
    set("cnt-success", c.success);
    set("cnt-failed", c.failed);
    set("adm-queued-jobs", c.queued);

    if (bannerEl) {
        if (c.queued === 0 && c.running === 0) {
            bannerEl.innerHTML = `
                <div class="active-queue-status-banner idle">
                    <div class="status-banner-icon">🟢</div>
                    <div class="status-banner-info">
                        <div class="status-banner-title">${t("admin_queue_idle_title")}</div>
                        <div class="status-banner-desc">${t("admin_queue_idle_desc")}</div>
                    </div>
                </div>`;
        } else if (c.running > 0) {
            bannerEl.innerHTML = `
                <div class="active-queue-status-banner running">
                    <div class="status-banner-icon pulsing">⚡</div>
                    <div class="status-banner-info">
                        <div class="status-banner-title">${t("admin_queue_running_title")}</div>
                        <div class="status-banner-desc">${tf("admin_queue_running_desc", { running: c.running, queued: c.queued })}</div>
                    </div>
                </div>`;
        } else {
            bannerEl.innerHTML = `
                <div class="active-queue-status-banner queued">
                    <div class="status-banner-icon">⏳</div>
                    <div class="status-banner-info">
                        <div class="status-banner-title">${t("admin_queue_queued_title")}</div>
                        <div class="status-banner-desc">${tf("admin_queue_queued_desc", { queued: c.queued })}</div>
                    </div>
                </div>`;
        }
    }

    const jobs = data.jobs || [];
    if (jobs.length === 0) {
        admStateEmpty(container, "admin_queue_empty");
        return;
    }

    const renderJobCard = (j) => {
        const statusLower = (j.status || "queued").toLowerCase();
        const engineLower = (j.engine || "voxcpm").toLowerCase();
        const engineLabel = engineLower === "recap" ? t("adm_badge_recap") : t("adm_opt_engine_voxcpm");

        let statusBadgeText = jobStatusText(j.status);
        let durationTxt = `⏳ ${j.created_ago}`;
        if (statusLower === "success") {
            statusBadgeText = t("badge_completed");
            durationTxt = `⏱️ ${j.duration_seconds || '15'}s • ${j.created_ago}`;
        } else if (statusLower === "queued") {
            statusBadgeText = t("badge_queued");
            durationTxt = `⏳ ${j.created_ago}`;
        } else if (statusLower === "running") {
            statusBadgeText = t("badge_running");
            durationTxt = `⚡ ${j.created_ago}`;
        } else if (statusLower === "failed") {
            statusBadgeText = t("badge_failed");
        } else if (statusLower === "cancelled") {
            statusBadgeText = t("badge_cancelled");
        }

        let actionsHtml = "";
        if (statusLower === "failed" || statusLower === "cancelled") {
            actionsHtml += `<button class="queue-act-btn retry" onclick="adminRetryJob('${j.id}')">🔄 ${t("admin_retry")}</button>`;
        } else if (statusLower === "queued" || statusLower === "running") {
            actionsHtml += `<button class="queue-act-btn cancel" onclick="adminCancelJob('${j.id}')">⏹️ ${t("admin_cancel_job")}</button>`;
        }
        if (statusLower === "success" || statusLower === "failed") {
            actionsHtml += `<button class="queue-act-btn" onclick="adminManualRefundJob('${escapeHtml(j.id)}')">↩️</button>`;
        }
        if (j.is_expired) {
            actionsHtml += `<span class="badge-expired text-xs">⌛ ${t("admin_expired")}</span>`;
            actionsHtml += `<button class="queue-act-btn retry" onclick="adminRetryJob('${j.id}')">🔄 ${t("admin_regen")}</button>`;
        } else if (j.download_url) {
            const qTitle = `${j.username ? '@' + j.username : 'ID: ' + j.user_id} • ${j.voice_display || j.preset_voice || j.engine}`;
            actionsHtml += `<button class="queue-act-btn uid adm-media-open-btn" data-job-id="${escapeHtml(j.id)}" data-job-type="${escapeHtml(j.job_type)}" data-download-url="${escapeHtml(j.download_url)}" data-deliverables="${escapeHtml(JSON.stringify(j.deliverables || {}))}" data-title="${escapeHtml(qTitle)}">${escapeHtml(t("adm_btn_play"))}</button>`;
            actionsHtml += `<a href="${signedAdminHref(j.download_url)}" target="_blank" rel="noopener" data-open-url="${j.download_url}" class="queue-act-btn uid" title="${escapeHtml(t("adm_open_stream_title"))}">↗️</a>`;
        }

        const pct = Math.min(100, Math.max(8, Number(j.progress_percent) || (statusLower === "running" ? 58 : 22)));
        const progressHtml = (statusLower === "running" || statusLower === "queued")
            ? `<div class="vcp-job-progress"><span class="vcp-job-progress-fill" style="width:${pct}%"></span></div>`
            : "";

        return `
            <div class="queue-item vcp-job-card" id="queue-item-${j.id}">
                <div class="queue-item-header vcp-job-badges">
                    <div class="queue-badges-left">
                        <span class="badge-status ${statusLower}">${statusBadgeText}</span>
                        <span class="badge-engine ${engineLower}">${engineLabel}</span>
                        <span class="mini-tag">${escapeHtml(j.voice_display || j.preset_voice)}</span>
                        ${j.job_type === 'TTS' ? `<span class="mini-tag">${escapeHtml(t("job_badge_tts"))}</span>` : ""}
                    </div>
                    <span class="queue-timing">${durationTxt}</span>
                </div>
                <div class="queue-item-meta">
                    <span class="queue-user-pill" onclick="admFillUser(${j.user_id})">👤 ${escapeHtml(j.user_name)}${j.username ? ` (@${escapeHtml(j.username)})` : ""} • ID: ${j.user_id}</span>
                    <span>#${j.id.slice(0, 8)}</span>
                </div>
                <div class="queue-item-body">${escapeHtml(j.input_snippet || t("adm_task_content_fallback"))}</div>
                ${progressHtml}
                ${j.error_message ? `<div class="queue-item-error">⚠️ ${escapeHtml(displayJobError(j.error_message))}</div>` : ""}
                <div class="queue-item-footer vcp-job-footer-row">
                    <span class="queue-timing">${escapeHtml(j.created_ago)}</span>
                    <div class="queue-actions-row">${actionsHtml}</div>
                </div>
            </div>`;
    };

    if (adminQueueStatusFilter === "all") {
        const activeJobs = jobs.filter(j => j.status === "QUEUED" || j.status === "RUNNING");
        const finishedJobs = jobs.filter(j => j.status !== "QUEUED" && j.status !== "RUNNING");
        let fullHtml = "";
        if (activeJobs.length > 0) {
            fullHtml += `<div class="queue-section-header active">${t("admin_active_section_title")} (${activeJobs.length})</div>`;
            fullHtml += activeJobs.map(renderJobCard).join("");
        }
        if (finishedJobs.length > 0) {
            fullHtml += `<div class="queue-section-header history">${t("admin_history_section_title")} (${finishedJobs.length})</div>`;
            fullHtml += finishedJobs.map(renderJobCard).join("");
        }
        container.innerHTML = fullHtml || "";
        if (!fullHtml) admStateEmpty(container, "admin_queue_empty");
    } else {
        container.innerHTML = jobs.map(renderJobCard).join("");
    }
    wireAdminMediaButtons(container);
}

async function loadAdminQueue(isSilent = false) {
    if (!state.user?.is_admin) return;
    const container = document.getElementById("admin-queue-list");
    if (!container) return;

    if (!isSilent && container.children.length === 0) admStateLoading(container);

    try {
        const url = `/api/admin/queue?status=${encodeURIComponent(adminQueueStatusFilter)}&engine=${encodeURIComponent(adminQueueEngineFilter)}&limit=50`;
        const res = await fetchWithTimeout(url, { headers: getAuthHeaders() });
        if (!res.ok) {
            if (!isSilent) admStateError(container, "loadAdminQueue()");
            return;
        }
        renderAdminQueueData(await res.json());
        loadAdminFleetStatus();
    } catch (e) {
        console.warn("Could not load admin queue:", e);
        if (!isSilent) admStateError(container, "loadAdminQueue()");
    }
}
window.loadAdminQueue = loadAdminQueue;

let _adminFleetData = null;

async function loadAdminFleetStatus() {
    if (!state.user?.is_admin) return;
    try {
        const res = await fetch("/api/admin/fleet/status", { headers: getAuthHeaders() });
        if (!res.ok) return;
        const data = await res.json();
        _adminFleetData = data;
        renderAdminFleetUI(data);
    } catch (e) {
        console.warn("Could not load fleet status:", e);
    }
}

function renderAdminFleetUI(data) {
    if (!data) return;
    const vps = data.vps_worker || {};
    const badge = document.getElementById("adm-vps-status-badge");
    const hostLbl = document.getElementById("adm-vps-host-label");
    const engLbl = document.getElementById("adm-vps-engine");
    const seenLbl = document.getElementById("adm-vps-last-seen");
    const btnToggleIcon = document.getElementById("adm-vps-toggle-icon");
    const btnToggleLabel = document.getElementById("adm-vps-toggle-label");
    const recapQueueIcon = document.getElementById("adm-recap-queue-icon");
    const recapQueueLabel = document.getElementById("adm-recap-queue-label");

    if (badge) {
        badge.textContent = vps.status_code || "OFFLINE";
        badge.className = "badge-mini-gold";
        if (vps.status_code === "ONLINE") badge.classList.add("text-green");
        else if (vps.status_code === "PAUSED") badge.classList.add("text-gold");
        else if (vps.status_code === "STANDBY") badge.classList.add("text-cyan");
        else badge.classList.add("text-danger");
    }
    if (hostLbl) hostLbl.textContent = `(${vps.host || t("adm_fleet_vps_host_default")})`;
    if (engLbl) engLbl.textContent = vps.engine || "VOXCPM";
    if (seenLbl) seenLbl.textContent = vps.last_seen_ago || "Never";

    const isPaused = vps.status_code === "PAUSED";
    if (btnToggleIcon) btnToggleIcon.textContent = isPaused ? "▶️" : "⏸️";
    if (btnToggleLabel) btnToggleLabel.textContent = isPaused ? t("adm_vps_resume") : t("adm_vps_pause");

    const recapOn = !!vps.recap_on_queue;
    if (recapQueueIcon) recapQueueIcon.textContent = recapOn ? "🎬" : "⏸️";
    if (recapQueueLabel) recapQueueLabel.textContent = recapOn ? t("adm_recap_queue_enabled") : t("adm_recap_queue_disabled");
}

async function toggleVpsWorker() {
    haptic("medium");
    try {
        const currPaused = _adminFleetData?.vps_worker?.status_code === "PAUSED";
        const nextStatus = currPaused ? "ONLINE" : "PAUSED";
        const res = await fetch("/api/admin/fleet/worker-toggle", {
            method: "POST",
            headers: {
                ...getAuthHeaders(),
                "Content-Type": "application/json",
            },
            body: JSON.stringify({ status: nextStatus }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        showStudioToast(nextStatus === "PAUSED" ? t("adm_vps_paused_toast") : t("adm_vps_resumed_toast"), "success");
        await loadAdminFleetStatus();
    } catch (e) {
        showStudioToast(tf("adm_vps_toggle_failed", { error: e.message }), "error");
    }
}

async function toggleRecapQueueRouting() {
    haptic("medium");
    try {
        const currOn = !!_adminFleetData?.vps_worker?.recap_on_queue;
        const nextOn = !currOn;
        const res = await fetch("/api/admin/fleet/worker-toggle", {
            method: "POST",
            headers: {
                ...getAuthHeaders(),
                "Content-Type": "application/json",
            },
            body: JSON.stringify({ recap_on_queue: nextOn }),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        showStudioToast(nextOn ? t("adm_recap_queue_on_toast") : t("adm_recap_queue_off_toast"), "success");
        await loadAdminFleetStatus();
    } catch (e) {
        showStudioToast(tf("adm_recap_queue_failed", { error: e.message }), "error");
    }
}

window.loadAdminFleetStatus = loadAdminFleetStatus;
window.renderAdminFleetUI = renderAdminFleetUI;
window.toggleVpsWorker = toggleVpsWorker;
window.toggleRecapQueueRouting = toggleRecapQueueRouting;

async function adminCancelJob(jobId) {
    showStudioConfirm(t("admin_confirm_cancel_job"), async () => {
        haptic("medium");
        try {
            const res = await fetch(`/api/admin/queue/${jobId}/cancel`, {
                method: "POST",
                headers: getAuthHeaders(),
            });
            const data = await res.json();
            if (data.success) {
                showStudioToast(data.refund && data.refund.refunded ? t("admin_cancel_refunded") : t("admin_cancel_done"), "success");
                loadAdminQueue();
                loadAdminHistory();
            } else {
                showStudioToast(adminErrorText("admin_cancel_err", data.detail || data.message), 'error');
            }
        } catch (e) {
            showStudioToast(adminErrorText("admin_cancel_err", e.message), 'error');
        }
    });
}
window.adminCancelJob = adminCancelJob;

// ── History ──────────────────────────────────────────────────────────

function sanitizeCssMediaUrl(raw) {
    const s = String(raw || "").trim();
    if (!s || /["'()\\\r\n]/.test(s)) return "";
    if (/^(https?:\/\/|\/|blob:|data:image\/)/i.test(s)) return s;
    return "";
}

function escapeHtml(str) {
    if (!str) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}


let adminHistFilter = {
    timeframe: "24h",
    status: "all",
    job_type: "all",
    job_id: ""
};

function setAdminHistFilter(key, val) {
    if (key) adminHistFilter[key] = val;
    const chips = document.querySelectorAll('.filter-chip[data-filter-type="timeframe"]');
    chips.forEach(c => c.classList.toggle('active', c.getAttribute('data-val') === adminHistFilter.timeframe));
    const selStatus = document.getElementById("adm-hist-status-sel");
    if (selStatus && selStatus.value !== adminHistFilter.status) selStatus.value = adminHistFilter.status;
    const selType = document.getElementById("adm-hist-type-sel");
    if (selType && selType.value !== adminHistFilter.job_type) selType.value = adminHistFilter.job_type;
    loadAdminHistory();
}
window.setAdminHistFilter = setAdminHistFilter;

function adminSearchJobId() {
    const el = document.getElementById("adm-hist-jobid");
    adminHistFilter.job_id = el ? el.value.trim() : "";
    loadAdminHistory();
}
window.adminSearchJobId = adminSearchJobId;

function renderAdminHistoryData(data) {
    if (!data) return;
    window._lastAdminHistory = data;
    const container = document.getElementById("admin-global-history-list");
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.innerText = v; };
    set("adm-hist-total", data.total_count ?? (data.history?.length || 0));
    set("adm-hist-failed", data.total_failed || 0);
    set("adm-hist-success", data.total_success || 0);
    set("adm-hist-recap", data.total_recap || 0);
    set("adm-hist-voice", data.total_voice || 0);

    const history = data.history || [];
    if (!container) return;
    if (history.length === 0) {
        admStateEmpty(container, "admin_history_empty");
        return;
    }
    container.innerHTML = history.map(j => {
        const titleStr = `${j.username ? '@' + j.username : 'ID: ' + j.user_id} • ${j.voice_display || j.preset_voice || j.engine}`;
        const errText = displayJobError(j.error_message);
        const isFailed = (j.status === "FAILED");
        const isSuccess = (j.status === "SUCCESS");
        const isRunning = (j.status === "RUNNING" || j.status === "QUEUED");
        const borderCls = isFailed ? 'border-danger-dim' : (isSuccess ? (j.is_expired ? 'border-warning-dim' : 'border-success-dim') : (isRunning ? 'border-running-dim' : ''));
        return `
        <div class="history-item ${borderCls}" id="adm-job-item-${j.id}">
            <div class="history-item-header">
                <div>
                    <span class="history-item-badge ${j.job_type === 'RECAP' ? 'badge-recap' : j.job_type === 'TTS' ? 'badge-tts' : 'badge-voice'}">${j.job_type === 'RECAP' ? escapeHtml(t("adm_badge_recap")) : j.job_type === 'TTS' ? escapeHtml(t("job_badge_tts")) : escapeHtml(t("adm_badge_voice"))}</span>
                    <span class="mini-tag adm-tap" style="margin-left: 4px;" onclick="admFillUser(${j.user_id})">👤 ${escapeHtml(j.username ? '@' + j.username : 'ID: ' + j.user_id)}</span>
                </div>
                <div>
                    <span class="history-item-badge status-${j.status.toLowerCase()}">${escapeHtml(jobStatusText(j.status))}</span>
                    ${j.is_expired ? `<span class="badge-expired" style="margin-left: 4px;">⌛ ${t("admin_expired")}</span>` : ''}
                </div>
            </div>
            <div class="history-item-body">
                <strong>${escapeHtml(j.voice_display || j.preset_voice || j.engine)}:</strong> ${escapeHtml(j.input_snippet || t("adm_task_fallback"))}
                ${errText ? `
                    <div class="history-error-snippet text-xs text-danger mt-1 p-2 glass-inset">
                        ⚠️ <strong>${escapeHtml(t("adm_error_prefix"))}</strong> ${escapeHtml(errText.slice(0, 180))}${errText.length > 180 ? '...' : ''}
                    </div>` : ''}
                <div id="retry-status-box-${j.id}"></div>
            </div>
            <div class="history-item-footer">
                <span>⏱️ ${j.time_ago} <code class="adm-job-code">(${j.id.slice(0, 8)})</code></span>
                <div class="history-btn-row" id="adm-actions-row-${j.id}">
                    ${j.is_expired ? `
                        <button class="btn mini-btn secondary-btn btn-retry-job" id="btn-retry-${j.id}" onclick="adminRetryJob('${j.id}')">🔄 ${t("admin_regen")}</button>
                    ` : (j.download_url ? `
                        <button class="btn-play-preview adm-media-open-btn" data-job-id="${escapeHtml(j.id)}" data-job-type="${escapeHtml(j.job_type)}" data-download-url="${escapeHtml(j.download_url)}" data-deliverables="${escapeHtml(JSON.stringify(j.deliverables || {}))}" data-title="${escapeHtml(titleStr)}">${escapeHtml(t("adm_btn_preview"))}</button>
                        <a href="${signedAdminHref(j.download_url)}" target="_blank" rel="noopener" data-open-url="${j.download_url}" class="history-dl-link" title="${escapeHtml(t("adm_open_stream_title"))}">↗️</a>
                    ` : '')}
                    ${isFailed ? `
                        <button class="btn mini-btn secondary-btn btn-retry-job" id="btn-retry-${j.id}" onclick="adminRetryJob('${j.id}')">🔄 ${t("admin_retry")}</button>
                    ` : ''}
                    <button class="btn mini-btn danger-btn" onclick="adminDeleteSingleJob('${j.id}')">🗑️ ${t("admin_delete")}</button>
                </div>
            </div>
        </div>`;
    }).join("");
    wireAdminMediaButtons(container);
}

async function loadAdminHistory() {
    const container = document.getElementById("admin-global-history-list");
    try {
        const params = new URLSearchParams({
            timeframe: adminHistFilter.timeframe || "24h",
            status: adminHistFilter.status || "all",
            job_type: adminHistFilter.job_type || "all",
            limit: "100"
        });
        if (adminHistFilter.job_id) params.set("job_id", adminHistFilter.job_id);
        const res = await fetchWithTimeout(`/api/admin/history?${params.toString()}`, { headers: getAuthHeaders() });
        if (!res.ok) {
            if (container) admStateError(container, "loadAdminHistory()");
            return;
        }
        renderAdminHistoryData(await res.json());
    } catch (e) {
        console.warn("Could not load admin history:", e);
        if (container) admStateError(container, "loadAdminHistory()");
    }
}
window.loadAdminHistory = loadAdminHistory;

async function adminRetryJob(jobId) {
    haptic("medium");
    const itemEl = document.getElementById(`adm-job-item-${jobId}`);
    const retryBtn = document.getElementById(`btn-retry-${jobId}`);
    const statusBox = document.getElementById(`retry-status-box-${jobId}`);

    if (retryBtn) {
        retryBtn.disabled = true;
        retryBtn.innerText = t("admin_retry_starting");
    }
    if (itemEl) {
        itemEl.className = "history-item border-running-dim";
        const badge = itemEl.querySelector('.history-item-badge[class*="status-"]');
        if (badge) {
            badge.className = "history-item-badge status-running";
            badge.innerText = "QUEUED";
        }
    }
    if (statusBox) {
        statusBox.innerHTML = `
            <div class="text-xs mt-1 p-2" style="background: rgba(56, 189, 248, 0.1); border: 1px solid rgba(56, 189, 248, 0.3); border-radius: 8px; color: #38bdf8;">
                ⏳ <strong>${t("admin_retry_queued_note")}</strong>
            </div>
        `;
    }

    try {
        const res = await fetch(`/api/admin/queue/${jobId}/retry`, {
            method: "POST",
            headers: getAuthHeaders(),
        });
        const data = await res.json();
        if (res.ok) {
            loadAdminQueue(true);
            pollAdminJobProgress(jobId);
        } else {
            showStudioToast(adminErrorText("admin_retry_err", data.detail || data.message), 'error');
            if (retryBtn) {
                retryBtn.disabled = false;
                retryBtn.innerText = `🔄 ${t("admin_retry")}`;
            }
        }
    } catch (e) {
        showStudioToast(adminErrorText("admin_retry_err", e.message), 'error');
        if (retryBtn) {
            retryBtn.disabled = false;
            retryBtn.innerText = `🔄 ${t("admin_retry")}`;
        }
    }
}

let activeRetryPollers = {};

function pollAdminJobProgress(jobId) {
    // Shape guard (audit fix 2026-11-03): never poll for an id that
    // cannot be a job — production logs showed this poller requesting
    // /api/recap/final_edge_tts_smart_sync/status (an engine name, not
    // a job id) repeatedly. Real job ids are rc_* (recap) or UUIDs
    // (voice clone).
    if (!/^rc_/.test(jobId) && !/^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(jobId)) {
        console.warn("pollAdminJobProgress skipped for non-job id:", jobId);
        return;
    }
    if (activeRetryPollers[jobId]) {
        clearInterval(activeRetryPollers[jobId]);
    }

    let pollCount = 0;
    const maxPolls = 80; // Up to ~3.5 mins
    let notFoundCount = 0;

    activeRetryPollers[jobId] = setInterval(async () => {
        pollCount++;
        if (pollCount > maxPolls) {
            clearInterval(activeRetryPollers[jobId]);
            delete activeRetryPollers[jobId];
            return;
        }

        try {
            const itemEl = document.getElementById(`adm-job-item-${jobId}`);
            const isRecap = jobId.startsWith("rc_") || (itemEl && itemEl.querySelector('.badge-recap'));
            const statusUrl = isRecap ? `/api/recap/${jobId}/status` : `/api/jobs/${jobId}/status`;

            const res = await fetch(statusUrl, { headers: getAuthHeaders() });
            if (res.status === 404) {
                // Mirror the user-side recap poller: three 404s in a
                // row mean the job is gone (purged, or never persisted)
                // — stop polling and surface the same "job gone" state
                // instead of silently spinning until the poll cap.
                notFoundCount++;
                if (notFoundCount >= 3) {
                    clearInterval(activeRetryPollers[jobId]);
                    delete activeRetryPollers[jobId];
                    const goneBox = document.getElementById(`retry-status-box-${jobId}`);
                    if (goneBox) {
                        goneBox.innerHTML = `
                            <div class="text-xs mt-1 p-2" style="background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 8px; color: #fca5a5;">
                                ⚠️ <strong>${escapeHtml(t("poll_job_gone"))}</strong>
                            </div>
                        `;
                    }
                    const goneRetryBtn = document.getElementById(`btn-retry-${jobId}`);
                    if (goneRetryBtn) {
                        goneRetryBtn.disabled = false;
                        goneRetryBtn.innerText = "🔄 Retry";
                    }
                    showStudioToast(t("poll_job_gone"), 'error', 6000);
                }
                return;
            }
            if (!res.ok) return;
            notFoundCount = 0;

            const data = await res.json();
            const statusBox = document.getElementById(`retry-status-box-${jobId}`);
            const badge = itemEl ? itemEl.querySelector('.history-item-badge[class*="status-"]') : null;
            const actionsRow = document.getElementById(`adm-actions-row-${jobId}`);
            const retryBtn = document.getElementById(`btn-retry-${jobId}`);

            if (data.status === "RUNNING" || data.status === "QUEUED") {
                if (badge) {
                    badge.className = "history-item-badge status-running";
                    badge.innerText = data.status;
                }
                if (statusBox) {
                    let label = data.progress_label || (data.status === "QUEUED" ? t("admin_retry_status_waiting") : t("admin_retry_status_running"));
                    const step = data.progress_step ? `[${data.progress_step}] ` : '';
                    const pct = data.progress_percent || (data.status === "RUNNING" ? 50 : 10);
                    statusBox.innerHTML = `
                        <div class="text-xs mt-1 p-2" style="background: rgba(56, 189, 248, 0.1); border: 1px solid rgba(56, 189, 248, 0.3); border-radius: 8px; color: #38bdf8;">
                            ⏳ <strong>${step}${escapeHtml(label)}</strong> (${pct}%)
                        </div>
                    `;
                }
            } else if (data.status === "SUCCESS") {
                clearInterval(activeRetryPollers[jobId]);
                delete activeRetryPollers[jobId];

                if (itemEl) itemEl.className = "history-item border-success-dim";
                if (badge) {
                    badge.className = "history-item-badge status-success";
                    badge.innerText = "SUCCESS";
                }
                if (statusBox) {
                    statusBox.innerHTML = `
                        <div class="text-xs mt-1 p-2" style="background: rgba(16, 185, 129, 0.12); border: 1px solid rgba(16, 185, 129, 0.35); border-radius: 8px; color: #34d399;">
                            🎉 <strong>${t("admin_retry_status_done")}</strong>
                        </div>
                    `;
                }
                if (retryBtn) retryBtn.remove();
                const expiredBadge = itemEl ? itemEl.querySelector('.badge-expired') : null;
                if (expiredBadge) expiredBadge.remove();

                if (actionsRow && !actionsRow.querySelector('.btn-play-preview')) {
                    const mediaUrl = isRecap
                        ? (data.download_urls?.video || `/api/recap/stream/${jobId}/video`)
                        : (data.output_audio_url || `/api/audio/stream/${jobId}`);
                    const previewBtn = document.createElement('button');
                    previewBtn.className = 'btn-play-preview';
                    previewBtn.innerText = t("adm_btn_preview");
                    previewBtn.onclick = () => openAdminMediaModal(
                        jobId,
                        isRecap ? 'RECAP' : 'VOICE_CLONE',
                        mediaUrl,
                        JSON.stringify(data.download_urls || {}),
                        `${t("adm_job_id_prefix")}${jobId.slice(0, 8)}`
                    );
                    actionsRow.prepend(previewBtn);
                }
                // Refresh global history counters
                setTimeout(loadAdminHistory, 2500);
            } else if (data.status === "FAILED") {
                clearInterval(activeRetryPollers[jobId]);
                delete activeRetryPollers[jobId];

                if (itemEl) itemEl.className = "history-item border-danger-dim";
                if (badge) {
                    badge.className = "history-item-badge status-failed";
                    badge.innerText = jobStatusText("FAILED");
                }
                if (statusBox) {
                    statusBox.innerHTML = `
                        <div class="text-xs mt-1 p-2" style="background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 8px; color: #fca5a5;">
                            ⚠️ <strong>${escapeHtml(t("adm_error_prefix"))}</strong> ${escapeHtml(displayJobError(data.error_message) || t("adm_exec_failed"))}
                        </div>
                    `;
                }
                if (retryBtn) {
                    retryBtn.disabled = false;
                    retryBtn.innerText = `🔄 ${t("admin_retry")}`;
                }
            }
        } catch (pollErr) {
            console.debug("Admin poller tick error:", pollErr);
        }
    }, 2500);
}

async function adminDeleteSingleJob(jobId) {
    showStudioConfirm(tf("admin_confirm_delete_job", { id: jobId.slice(0, 8) }), async () => {
        haptic("heavy");
        try {
            const res = await fetch("/api/admin/history/purge", {
                method: "POST",
                headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
                body: JSON.stringify({ mode: "single", job_id: jobId }),
            });
            const data = await res.json();
            if (res.ok) {
                const el = document.getElementById(`adm-job-item-${jobId}`);
                if (el) el.remove();
                loadAdminHistory();
            } else {
                showStudioToast(adminErrorText("admin_delete_err", data.detail || data.message), 'error');
            }
        } catch (e) {
            showStudioToast(adminErrorText("admin_delete_err", e.message), 'error');
        }
    });
}
window.adminDeleteSingleJob = adminDeleteSingleJob;

async function adminPurgeHistory(mode) {
    let confirmMsg = "";
    if (mode === "failed") confirmMsg = t("admin_confirm_purge_failed");
    else if (mode === "older_retention") confirmMsg = t("admin_confirm_purge_older");
    else if (mode === "matching") confirmMsg = tf("admin_confirm_purge_matching", { timeframe: adminHistFilter.timeframe, status: adminHistFilter.status, job_type: adminHistFilter.job_type });
    else confirmMsg = t("admin_confirm_purge_generic");

    showStudioConfirm(confirmMsg, async () => {
        haptic("heavy");
        try {
            const res = await fetch("/api/admin/history/purge", {
                method: "POST",
                headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
                body: JSON.stringify({
                    mode: mode,
                    timeframe: adminHistFilter.timeframe,
                    status: adminHistFilter.status,
                    job_type: adminHistFilter.job_type,
                }),
            });
            const data = await res.json();
            if (res.ok) {
                showStudioToast(data.message || tf("admin_purged", { count: data.purged_count || 0 }), 'success');
                loadAdminHistory();
            } else {
                showStudioToast(adminErrorText("admin_purge_err", data.detail || data.message), 'error');
            }
        } catch (e) {
            showStudioToast(adminErrorText("admin_purge_err", e.message), 'error');
        }
    });
}
window.adminPurgeHistory = adminPurgeHistory;

async function adminTriggerPurge24h() {
    // Name kept for compatibility; the purge boundary is the media
    // retention window (settings.media_retention_days, default 1 day),
    // matching the automatic sweep.
    return adminPurgeHistory("older_retention");
}
window.adminTriggerPurge24h = adminTriggerPurge24h;

// ── Activity (audit log) ─────────────────────────────────────────────

async function loadAdminAudit() {
    const box = document.getElementById("admin-audit-list");
    if (!box) return;
    admStateLoading(box);
    try {
        const res = await fetchWithTimeout("/api/admin/audit-log?limit=50", { headers: getAuthHeaders() });
        const rows = await res.json();
        if (!res.ok) throw new Error("Failed");
        if (!rows.length) { admStateEmpty(box, "admin_audit_empty"); return; }
        box.innerHTML = rows.map(r => {
            let detailTxt = "";
            try {
                const d = typeof r.details === "string" ? JSON.parse(r.details) : (r.details || {});
                const parts = Object.entries(d).map(([k, v]) => `${k}: ${v && typeof v === "object" ? JSON.stringify(v) : v}`);
                detailTxt = parts.join(" • ");
            } catch (e) { detailTxt = ""; }
            return `
            <div class="adm-audit-row">
                <div class="adm-audit-main">
                    <b>${escapeHtml(r.action)}</b>
                    <span class="text-muted">${escapeHtml(r.target_type || "")} ${escapeHtml(String(r.target_id || ""))}</span>
                </div>
                ${detailTxt ? `<div class="adm-audit-detail">${escapeHtml(detailTxt)}</div>` : ""}
                <div class="adm-audit-time">${admFmtDate(r.created_at)} • Admin ${r.admin_id}</div>
            </div>`;
        }).join("");
    } catch (e) {
        admStateError(box, "loadAdminAudit()");
    }
}
window.loadAdminAudit = loadAdminAudit;

// ── Pricing, Analytics, Orders & Refunds ─────────────────────────────
let _admPricingFilter = "all";
function filterAdminPricing(cat) {
    _admPricingFilter = cat || "all";
    document.querySelectorAll("#adm-pricing-cat-btns [data-pcat]").forEach(b => {
        b.classList.toggle("active", b.getAttribute("data-pcat") === _admPricingFilter);
    });
    if (Array.isArray(window._lastAdminPricingPlans)) renderAdminPricingList(window._lastAdminPricingPlans);
}
window.filterAdminPricing = filterAdminPricing;

function renderAdminPricingList(plans) {
    const box = document.getElementById("admin-pricing-list");
    if (!box) return;
    if (!Array.isArray(plans) || !plans.length) { admStateEmpty(box, "admin_empty"); return; }
    window._lastAdminPricingPlans = plans;
    const filtered = _admPricingFilter === "all" ? plans : plans.filter(p => {
        const g = p.is_topup ? "topup" : String(p.category || "voice").toLowerCase();
        return g === _admPricingFilter;
    });
    if (!filtered.length) { admStateEmpty(box, "admin_empty"); return; }
    box.innerHTML = filtered.map(p => {
        const code = String(p.code || "");
        const nKey = `admin_plan_name_${code}`;
        const titleText = (t(nKey) !== nKey) ? t(nKey) : (p.name || code);
        const catGroup = p.is_topup ? "topup" : String(p.category || "voice").toLowerCase();
        const catKey = `adm_plan_cat_${catGroup}`;
        const catText = (t(catKey) !== catKey) ? t(catKey) : catGroup;
        const safeCode = escapeHtml(code);
        return `
        <div class="adm-price-row adm-price-card">
            <div class="adm-price-main">
                <div class="adm-price-head">
                    <div class="adm-price-title">${escapeHtml(titleText)}</div>
                    <span class="adm-price-cat-badge">${escapeHtml(catText)}</span>
                </div>
                <div class="adm-price-meta">
                    <span class="adm-price-code">${safeCode}</span>
                </div>
                <div class="adm-price-specs">
                    <label class="text-xs">Min <input type="number" class="studio-input" id="adm-min-${safeCode}" value="${p.minutes || 0}" min="0"></label>
                    <label class="text-xs">Cr <input type="number" class="studio-input" id="adm-cr-${safeCode}" value="${p.recap_credits || 0}" min="0"></label>
                    <label class="text-xs">Days <input type="number" class="studio-input" id="adm-vdays-${safeCode}" value="${p.validity_days ?? 30}" min="0" max="3650"></label>
                    <label class="text-xs"><input type="checkbox" id="adm-act-${safeCode}" ${p.is_active ? "checked" : ""}> ${escapeHtml(t("adm_plan_active_lbl"))}</label>
                </div>
            </div>
            <div class="adm-price-controls">
                <div class="adm-price-input-wrap">
                    <input type="number" class="studio-input adm-price-input" id="adm-price-${safeCode}" value="${p.price_mmk}" min="100" step="100" aria-label="${escapeHtml(titleText)}">
                    <span class="adm-price-cur">MMK</span>
                </div>
                <button type="button" class="btn mini-btn gold-btn adm-price-save-btn" onclick="adminSavePrice('${safeCode}')">${escapeHtml(t("admin_pricing_save"))}</button>
            </div>
        </div>`;
    }).join("");
}

async function loadAdminPricing() {
    const box = document.getElementById("admin-pricing-list");
    if (!box) return;
    admStateLoading(box);
    try {
        const res = await fetchWithTimeout("/api/admin/pricing", { headers: getAuthHeaders() });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        renderAdminPricingList(data.plans || []);
    } catch (e) {
        admStateError(box, "loadAdminPricing()");
    }
}
window.loadAdminPricing = loadAdminPricing;

async function adminSavePrice(code) {
    const price = parseInt(document.getElementById("adm-price-" + code)?.value || "0", 10) || 0;
    const minutes = parseInt(document.getElementById("adm-min-" + code)?.value || "0", 10) || 0;
    const recap_credits = parseInt(document.getElementById("adm-cr-" + code)?.value || "0", 10) || 0;
    const validity_days = parseInt(document.getElementById("adm-vdays-" + code)?.value || "30", 10) || 0;
    const is_active = !!(document.getElementById("adm-act-" + code)?.checked);
    showStudioConfirm(tf("admin_confirm_price", { code, price: price.toLocaleString() }), async () => {
        haptic("medium");
        try {
            const res = await fetch(`/api/admin/pricing/${encodeURIComponent(code)}`, {
                method: "POST",
                headers: getAuthHeaders(),
                body: JSON.stringify({ price_mmk: price, minutes, recap_credits, validity_days, is_active }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || "Failed");
            showStudioToast(data.message, "success");
            loadAdminPricing();
            if (typeof refreshPlanPrices === "function") refreshPlanPrices();
        } catch (e) {
            showStudioToast(adminErrorText("admin_generic_err", e.message), "error");
        }
    });
}
window.adminSavePrice = adminSavePrice;

function openAdminPricingShortcut() {
    showAdminSection("settings");
    const acc = document.getElementById("adm-acc-pricing");
    if (acc) {
        acc.open = true;
        setTimeout(() => acc.scrollIntoView({ behavior: "smooth", block: "start" }), 120);
    }
}
window.openAdminPricingShortcut = openAdminPricingShortcut;

async function loadAdminAnalytics(days = 30) {
    document.querySelectorAll("#adm-analytics-window-btns [data-days]").forEach(b => {
        b.classList.toggle("active", Number(b.getAttribute("data-days")) === Number(days));
    });
    const body = document.getElementById("adm-analytics-body");
    if (!body || !state.user?.is_admin) return;
    try {
        const res = await fetchWithTimeout(`/api/admin/analytics?days=${encodeURIComponent(days)}`, { headers: getAuthHeaders() });
        if (!res.ok) { admStateError(body, `loadAdminAnalytics(${days})`); return; }
        const d = await res.json();
        const rev = d.revenue || {}, usg = d.usage || {}, c = d.costs || {}, tel = c.telemetry_summary || {}, exp = d.expiration || {};
        const top = rev.top_package;
        const topPkgsHtml = (rev.package_sales || []).slice(0, 4).map(p =>
            `<div class="adm-cost-row"><span>${escapeHtml(p.name)} (${p.orders}x)</span><b>${(p.revenue_mmk || 0).toLocaleString()} MMK</b></div>`
        ).join("");
        body.innerHTML = `
            <div class="adm-analytics-grid">
                <div class="card glass stat-mini-card"><span class="stat-lbl">${escapeHtml(tf("adm_rev_lbl", { n: rev.approved_orders || 0 }))}</span><span class="stat-num text-gold">${(rev.total_approved_mmk || 0).toLocaleString()} MMK</span></div>
                <div class="card glass stat-mini-card"><span class="stat-lbl">${escapeHtml(t("adm_profit_lbl"))}</span><span class="stat-num ${(c.estimated_gross_profit_mmk || 0) >= 0 ? "text-green" : "text-danger"}">${(c.estimated_gross_profit_mmk || 0).toLocaleString()} MMK</span></div>
                <div class="card glass stat-mini-card"><span class="stat-lbl">${escapeHtml(t("adm_cost_lbl"))}</span><span class="stat-num">$${(c.total_cost_usd || 0).toFixed(3)} (${(c.total_cost_mmk || 0).toLocaleString()} MMK)</span></div>
                <div class="card glass stat-mini-card"><span class="stat-lbl">${escapeHtml(t("adm_arpu_lbl"))}</span><span class="stat-num" style="font-size:13px;">${(c.arpu_mmk || 0).toLocaleString()} / ${(c.avg_cost_job_mmk ?? c.avg_cost_per_job_mmk ?? 0).toLocaleString()} MMK</span></div>
            </div>
            <div class="mt-2 text-xs text-muted">Jobs: ${usg.total_jobs || 0} (✓${usg.success_jobs || 0} / ✗${usg.failed_jobs || 0}) • ${_fmtCreditNum(usg.total_recap_credits_consumed || 0)} Cr, ${_fmtCreditNum(usg.total_voice_minutes_consumed || 0)}m • Refunds: ${_fmtCreditNum(usg.refunded_recap_credits || 0)} Cr</div>
            <div class="adm-cost-row mt-2"><span>${escapeHtml(t("adm_exp_lbl"))} (≤7d: <b>${exp.expiring_7d_count || 0}</b> lots)</span><b>-${_fmtCreditNum(exp.expired_recap_credits || 0)} Cr · -${_fmtCreditNum(exp.expired_voice_minutes || 0)}m</b></div>
            ${top ? `<div class="mt-2"><div class="text-xs font-bold">${escapeHtml(t("adm_top_pkg_lbl"))}</div>${topPkgsHtml}</div>` : ""}
            <div class="mt-2">
                <div class="text-xs font-bold">${escapeHtml(t("adm_infra_lbl"))}</div>
                <div class="adm-cost-row"><span>Gemini Text (${(tel.gemini_input_tokens || 0) + (tel.gemini_output_tokens || 0)} tok)</span><b>$${(c.gemini_text_usd || 0).toFixed(4)}</b></div>
                <div class="adm-cost-row"><span>Gemini TTS</span><b>$${(c.gemini_tts_usd || 0).toFixed(4)}</b></div>
                <div class="adm-cost-row"><span>GPU / VoxCPM (${Math.round(tel.gpu_runtime_seconds || 0)}s)</span><b>$${(c.gpu_usd || 0).toFixed(4)}</b></div>
                <div class="adm-cost-row"><span>Storage (${(tel.storage_gb || 0).toFixed(3)} GB) + BW</span><b>$${((c.storage_usd || 0) + (c.bandwidth_usd || 0)).toFixed(4)}</b></div>
            </div>`;
    } catch (e) { admStateError(body, `loadAdminAnalytics(${days})`); }
}
window.loadAdminAnalytics = loadAdminAnalytics;

async function loadAdminOrders(status = "") {
    document.querySelectorAll("#adm-screen-payments [data-ord-st]").forEach(b => {
        b.classList.toggle("active", (b.getAttribute("data-ord-st") || "") === status);
    });
    const el = document.getElementById("adm-orders-list");
    if (!el || !state.user?.is_admin) return;
    try {
        const qs = status ? `?status=${encodeURIComponent(status)}` : "";
        const r = await fetchWithTimeout("/api/admin/orders" + qs, { headers: getAuthHeaders() });
        if (!r.ok) { admStateError(el, `loadAdminOrders('${status}')`); return; }
        const d = await r.json();
        const rows = d.orders || [];
        el.innerHTML = rows.length ? rows.slice(0, 30).map(o => `
            <div class="vcp-tx-row">
                <div><b>${escapeHtml(o.user_name)}</b> • ${escapeHtml(o.package_name || o.package_code)}<br><span class="text-xs text-muted">${(o.amount_mmk || 0).toLocaleString()} MMK • ${admFmtDate(o.created_at)}</span></div>
                <span class="badge-status ${String(o.status || "").toLowerCase()}">${escapeHtml(o.status)}</span>
            </div>`).join("") : `<p class="text-muted text-xs">${escapeHtml(t("admin_empty"))}</p>`;
    } catch (e) { admStateError(el, `loadAdminOrders('${status}')`); }
}
window.loadAdminOrders = loadAdminOrders;

async function loadAdminRefunds() {
    const el = document.getElementById("adm-refunds-list");
    if (!el || !state.user?.is_admin) return;
    try {
        const r = await fetchWithTimeout("/api/admin/refunds", { headers: getAuthHeaders() });
        if (!r.ok) { admStateError(el, "loadAdminRefunds()"); return; }
        const d = await r.json();
        const rows = d.refunds || [];
        el.innerHTML = rows.length ? rows.slice(0, 20).map(rf => `
            <div class="vcp-tx-row">
                <div><b>User ${rf.user_id}</b> • ${escapeHtml(rf.reference_id || "")}<br><span class="text-xs text-muted">${escapeHtml(rf.note || "")} • ${admFmtDate(rf.created_at)}</span></div>
                <b>+${_fmtCreditNum(rf.recap_credits_refunded)} Cr · +${_fmtCreditNum(rf.voice_minutes_refunded)}m</b>
            </div>`).join("") : `<p class="text-muted text-xs">${escapeHtml(t("admin_empty"))}</p>`;
    } catch (e) { admStateError(el, "loadAdminRefunds()"); }
}
window.loadAdminRefunds = loadAdminRefunds;

async function adminManualRefundJob(jobIdArg) {
    const jid = (jobIdArg || document.getElementById("adm-refund-job-id")?.value || "").trim();
    if (!jid) return;
    showStudioConfirm(tf("adm_confirm_refund_job", { id: jid }), async () => {
        try {
            const res = await fetch(`/api/admin/refunds/job/${encodeURIComponent(jid)}`, {
                method: "POST",
                headers: getAuthHeaders(),
                body: JSON.stringify({ reason: "Admin console manual refund" }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || "Failed");
            showStudioToast(data.message || "Refunded", "success");
            loadAdminRefunds();
            loadAdminQueue(true);
            loadAdminAnalytics(30);
        } catch (e) {
            showStudioToast(adminErrorText("admin_generic_err", e.message), "error");
        }
    });
}
window.adminManualRefundJob = adminManualRefundJob;

// ── Settings (loaders/savers preserved from the previous console) ────

// [input element id, settings key] for the tutorial links beyond the
// original video/voice/recap slots — one tidy block in the Tutorials
// card of Admin → Settings; all ride the same settings save flow.
const ADMIN_TUTORIAL_LINK_FIELDS = [
    ["adm-tut-index-link", "tutorial_index_link"],
    ["adm-tut-plans-link", "tutorial_plans_link"],
    ["adm-tut-customvoice-link", "tutorial_customvoice_link"],
    ["adm-tut-downloader-link", "tutorial_downloader_link"],
    ["adm-tut-scriptedit-link", "tutorial_scriptedit_link"],
    ["adm-tut-recapadvanced-link", "tutorial_recapadvanced_link"],
    ["adm-tut-refer-link", "tutorial_refer_link"],
    ["adm-tut-support-link", "tutorial_support_link"],
];

// Batch BY: admin-settings edit-clobber protection. loadAdminSettings()
// repaints EVERY settings field from the server and runs on admin-tab
// entry and on settings sub-tab entry, so a repaint landing between an
// admin's edit and their Save silently reverted the field to the old
// server value (the known repaint clobber vector). Fields the admin
// has touched since the last load are tracked here and skipped by the
// paint helpers below; the flags clear ONLY after a successful
// saveAdminSettings(), whose post-save reload then paints server
// truth. Programmatic paints never fire input/change events, so
// painting itself never marks a field dirty.
const adminSettingsDirty = new Set();
let _adminSettingsDirtyBound = false;
let _adminSettingsSavedSnapshot = {};

function _snapshotAdminSettingsValues() {
    const pane = document.getElementById("adm-screen-settings");
    if (!pane) return;
    const snap = {};
    pane.querySelectorAll("input, select, textarea").forEach(el => {
        if (!el.id || el.closest("#adm-settings-save-bar")) return;
        if (el.type === "checkbox" || el.type === "radio") {
            snap[el.id] = el.checked;
        } else {
            snap[el.id] = el.value;
        }
    });
    _adminSettingsSavedSnapshot = snap;
}

function _checkAdminSettingsDirty() {
    const pane = document.getElementById("adm-screen-settings");
    if (!pane || !Object.keys(_adminSettingsSavedSnapshot).length) return false;
    const els = pane.querySelectorAll("input, select, textarea");
    for (const el of els) {
        if (!el.id || el.closest("#adm-settings-save-bar")) continue;
        if (!(el.id in _adminSettingsSavedSnapshot)) continue;
        const saved = _adminSettingsSavedSnapshot[el.id];
        if (el.type === "checkbox" || el.type === "radio") {
            if (el.checked !== saved) return true;
        } else {
            if (el.value !== saved) return true;
        }
    }
    return false;
}

function updateAdminSettingsSaveBar(forceHide = false) {
    const bar = document.getElementById("adm-settings-save-bar");
    if (!bar) return;
    if (forceHide || (typeof admCurrentSection !== "undefined" && admCurrentSection !== "settings")) {
        bar.classList.add("hidden");
        return;
    }
    const isDirty = _checkAdminSettingsDirty();
    bar.classList.toggle("hidden", !isDirty);
}

function _bindAdminSettingsDirtyTracking() {
    if (_adminSettingsDirtyBound) return;
    const pane = document.getElementById("adm-screen-settings");
    if (!pane) return;
    _adminSettingsDirtyBound = true;
    const mark = (e) => {
        if (e.target && e.target.id && !e.target.closest("#adm-settings-save-bar")) {
            adminSettingsDirty.add(e.target.id);
        }
        updateAdminSettingsSaveBar();
    };
    pane.addEventListener("input", mark);
    pane.addEventListener("change", mark);
}

function _admPaintValue(el, value) {
    if (el && !adminSettingsDirty.has(el.id)) el.value = value;
}

function _admPaintChecked(el, checked) {
    if (el && !adminSettingsDirty.has(el.id)) el.checked = checked;
}

window._snapshotAdminSettingsValues = _snapshotAdminSettingsValues;
window._checkAdminSettingsDirty = _checkAdminSettingsDirty;
window.updateAdminSettingsSaveBar = updateAdminSettingsSaveBar;

async function loadAdminSettings() {
    if (!state.user?.is_admin) return;
    _bindAdminSettingsDirtyTracking();
    try {
        const res = await fetch("/api/admin/settings", { headers: getAuthHeaders() });
        if (!res.ok) return;
        const s = await res.json();

        const elKpayNum = document.getElementById("adm-set-kpay-num");
        const elKpayName = document.getElementById("adm-set-kpay-name");
        const elWaveNum = document.getElementById("adm-set-wave-num");
        const elWaveName = document.getElementById("adm-set-wave-name");
        const elTutLink = document.getElementById("adm-set-tutorial-link");
        const elMaint = document.getElementById("adm-set-maintenance");
        const elMaintNote = document.getElementById("adm-set-maintenance-note");
        const elGroupAi = document.getElementById("adm-set-group-ai");
        const elCharLimitFree = document.getElementById("adm-set-char-limit-free");
        const elCharLimitPurchased = document.getElementById("adm-set-char-limit-purchased");
        const elFreeAllowance = document.getElementById("adm-set-free-allowance");
        const elDailyJobLimit = document.getElementById("adm-set-daily-job-limit");
        const elRecapFreeMax = document.getElementById("adm-set-recap-free-max");
        const elRecapMax = document.getElementById("adm-set-recap-max");
        const elVoiceDailyFree = document.getElementById("adm-set-voice-daily-free");
        const elVoiceDailyPaid = document.getElementById("adm-set-voice-daily-paid");
        const elTtsDailyFree = document.getElementById("adm-set-tts-daily-free");
        const elTtsDailyPaid = document.getElementById("adm-set-tts-daily-paid");
        const elVoxcpmTimeout = document.getElementById("adm-set-voxcpm-timeout");
        const elTutVideoEn = document.getElementById("adm-set-tutorial-video-enabled");
        const elTieredPricing = document.getElementById("adm-set-tiered-pricing");
        const elReqChannelId = document.getElementById("adm-set-required-channel-id");
        const elReqGroupId = document.getElementById("adm-set-required-group-id");
        const elChannelInvite = document.getElementById("adm-set-channel-invite-link");
        const elGroupInvite = document.getElementById("adm-set-group-invite-link");

        const elTutVoiceEn = document.getElementById("adm-tut-voice-enabled");
        const elTutVoiceTitle = document.getElementById("adm-tut-voice-title");
        const elTutVoiceLink = document.getElementById("adm-tut-voice-link");

        const elTutRecapEn = document.getElementById("adm-tut-recap-enabled");
        const elTutRecapTitle = document.getElementById("adm-tut-recap-title");
        const elTutRecapLink = document.getElementById("adm-tut-recap-link");
        const elRecapDemoAsset = document.getElementById("adm-recap-demo-asset");

        _admPaintValue(elKpayNum, s.payment_kpay_number || "");
        _admPaintValue(elKpayName, s.payment_kpay_name || "");
        _admPaintValue(elWaveNum, s.payment_wave_number || "");
        _admPaintValue(elWaveName, s.payment_wave_name || "");
        _admPaintValue(elTutLink, s.tutorial_video_link || "");
        _admPaintChecked(elMaint, !!s.maintenance_mode);
        _admPaintValue(elMaintNote, s.maintenance_note || "");
        _admPaintChecked(elGroupAi, !!s.group_ai_admin_enabled);
        const elGroupMaster = document.getElementById("adm-group-ai-master-toggle");
        _admPaintChecked(elGroupMaster, !!s.group_ai_admin_enabled);
        if (elGroupAi && elGroupMaster && !elGroupMaster._synced) {
            elGroupMaster._synced = true;
            elGroupMaster.addEventListener("change", () => {
                elGroupAi.checked = elGroupMaster.checked;
            });
            elGroupAi.addEventListener("change", () => {
                elGroupMaster.checked = elGroupAi.checked;
            });
        }
        const elModToggle = document.getElementById("adm-group-ai-moderation-toggle");
        _admPaintChecked(elModToggle, s.group_ai_moderation_enabled === undefined || s.group_ai_moderation_enabled === null ? true : !!s.group_ai_moderation_enabled);
        const elModThreshold = document.getElementById("adm-group-ai-mod-threshold");
        if (s.group_ai_moderation_confidence_threshold != null) _admPaintValue(elModThreshold, s.group_ai_moderation_confidence_threshold);
        const elMuteS2 = document.getElementById("adm-group-ai-mute-s2");
        if (s.group_ai_moderation_mute_duration_strike2_seconds != null) _admPaintValue(elMuteS2, s.group_ai_moderation_mute_duration_strike2_seconds);
        const elMuteS3 = document.getElementById("adm-group-ai-mute-s3");
        if (s.group_ai_moderation_mute_duration_strike3_seconds != null) _admPaintValue(elMuteS3, s.group_ai_moderation_mute_duration_strike3_seconds);
        const elWhitelist = document.getElementById("adm-group-ai-whitelist");
        if (s.group_ai_moderation_whitelist != null) _admPaintValue(elWhitelist, s.group_ai_moderation_whitelist);
        const elVoiceToggle = document.getElementById("adm-group-ai-voice-toggle");
        _admPaintChecked(elVoiceToggle, s.group_ai_voice_enabled === undefined || s.group_ai_voice_enabled === null ? true : !!s.group_ai_voice_enabled);
        const elVoiceCap = document.getElementById("adm-group-ai-voice-cap");
        if (s.group_ai_voice_daily_cap != null) _admPaintValue(elVoiceCap, s.group_ai_voice_daily_cap);
        const elGroupAiCooldown = document.getElementById("adm-set-group-ai-cooldown");
        const elGroupAiHourly = document.getElementById("adm-set-group-ai-hourly");
        const elGroupAiDaily = document.getElementById("adm-set-group-ai-daily");
        if (s.group_ai_user_cooldown_seconds != null) _admPaintValue(elGroupAiCooldown, s.group_ai_user_cooldown_seconds);
        if (s.group_ai_hourly_budget != null) _admPaintValue(elGroupAiHourly, s.group_ai_hourly_budget);
        if (s.group_ai_daily_budget != null) _admPaintValue(elGroupAiDaily, s.group_ai_daily_budget);
        // Batch BX: Private AI — the toggle defaults ON (owner
        // order), so an absent key in the response still renders
        // checked; the number fields follow the group's pattern.
        const elPrivateAi = document.getElementById("adm-set-private-ai");
        _admPaintChecked(elPrivateAi, s.private_ai_enabled === undefined || s.private_ai_enabled === null ? true : !!s.private_ai_enabled);
        const elPrivateAiCooldown = document.getElementById("adm-set-private-ai-cooldown");
        const elPrivateAiHourly = document.getElementById("adm-set-private-ai-hourly");
        const elPrivateAiDaily = document.getElementById("adm-set-private-ai-daily");
        if (s.private_ai_user_cooldown_seconds != null) _admPaintValue(elPrivateAiCooldown, s.private_ai_user_cooldown_seconds);
        if (s.private_ai_hourly_budget != null) _admPaintValue(elPrivateAiHourly, s.private_ai_hourly_budget);
        if (s.private_ai_daily_budget != null) _admPaintValue(elPrivateAiDaily, s.private_ai_daily_budget);
        if (s.character_limit_free != null) _admPaintValue(elCharLimitFree, s.character_limit_free);
        if (s.character_limit_purchased != null) _admPaintValue(elCharLimitPurchased, s.character_limit_purchased);
        if (s.free_monthly_allowance_seconds != null) _admPaintValue(elFreeAllowance, s.free_monthly_allowance_seconds);
        if (s.free_daily_job_limit != null) _admPaintValue(elDailyJobLimit, s.free_daily_job_limit);
        // Batch CG: FREE Gemini taste + global FREE budget fields.
        const elFreeGeminiTaste = document.getElementById("adm-set-free-gemini-taste");
        const elFreeGeminiBudget = document.getElementById("adm-set-free-gemini-budget");
        if (s.free_gemini_taste_minutes != null) _admPaintValue(elFreeGeminiTaste, s.free_gemini_taste_minutes);
        if (s.free_gemini_daily_budget_requests != null) _admPaintValue(elFreeGeminiBudget, s.free_gemini_daily_budget_requests);
        if (s.recap_free_max_duration_seconds != null) _admPaintValue(elRecapFreeMax, s.recap_free_max_duration_seconds);
        if (s.recap_max_duration_seconds != null) _admPaintValue(elRecapMax, s.recap_max_duration_seconds);
        if (s.voice_daily_limit_minutes_free != null) _admPaintValue(elVoiceDailyFree, s.voice_daily_limit_minutes_free);
        if (s.voice_daily_limit_minutes_paid != null) _admPaintValue(elVoiceDailyPaid, s.voice_daily_limit_minutes_paid);
        if (s.tts_daily_limit_minutes_free != null) _admPaintValue(elTtsDailyFree, s.tts_daily_limit_minutes_free);
        if (s.tts_daily_limit_minutes_paid != null) _admPaintValue(elTtsDailyPaid, s.tts_daily_limit_minutes_paid);
        if (s.voxcpm_timeout != null) _admPaintValue(elVoxcpmTimeout, s.voxcpm_timeout);
        _admPaintChecked(elTutVideoEn, String(s.tutorial_video_enabled).toLowerCase() !== "false" && s.tutorial_video_enabled !== false);
        // Batch BE: the tiered-pricing arming switch (AO shipped the
        // setting + API dark; this toggle is the arming lever).
        _admPaintChecked(elTieredPricing, !!s.tiered_recap_pricing_enabled);
        _admPaintValue(elReqChannelId, s.required_channel_id || "");
        _admPaintValue(elReqGroupId, s.required_group_id || "");
        _admPaintValue(elChannelInvite, s.channel_invite_link || "");
        _admPaintValue(elGroupInvite, s.group_invite_link || "");

        _admPaintChecked(elTutVoiceEn, String(s.tutorial_voice_enabled).toLowerCase() !== "false" && s.tutorial_voice_enabled !== false);
        _admPaintValue(elTutVoiceTitle, tutorialDisplayTitle(s.tutorial_voice_title, "tutorial_default_voice_title"));
        _admPaintValue(elTutVoiceLink, s.tutorial_voice_link || s.tutorial_video_link || "");

        _admPaintChecked(elTutRecapEn, String(s.tutorial_recap_enabled).toLowerCase() !== "false" && s.tutorial_recap_enabled !== false);
        _admPaintValue(elTutRecapTitle, tutorialDisplayTitle(s.tutorial_recap_title, "tutorial_default_recap_title"));
        _admPaintValue(elTutRecapLink, s.tutorial_recap_link || s.tutorial_video_link || "");
        _admPaintValue(elRecapDemoAsset, s.recap_demo_asset || "");

        for (const [elId, settingKey] of ADMIN_TUTORIAL_LINK_FIELDS) {
            const el = document.getElementById(elId);
            _admPaintValue(el, s[settingKey] || "");
        }

        setSelectOrCustom("adm-gemini-primary", "adm-gemini-primary-custom", s.gemini_primary_model || "gemini-3.6-flash");
        setSelectOrCustom("adm-gemini-fb1", "adm-gemini-fb1-custom", s.gemini_fallback_1 || "gemini-3.5-flash");
        setSelectOrCustom("adm-gemini-fb2", "adm-gemini-fb2-custom", s.gemini_fallback_2 || "gemini-3.5-flash-lite");
        setSelectOrCustom("adm-gemini-fb3", "adm-gemini-fb3-custom", s.gemini_fallback_3 || "gemini-flash-lite-latest");
        setSelectOrCustom("adm-tts-primary", "adm-tts-primary-custom", s.gemini_tts_primary_model || "gemini-3.8-flash-tts");
        setSelectOrCustom("adm-tts-fb1", "adm-tts-fb1-custom", s.gemini_tts_fallback_1 || "");
        setSelectOrCustom("adm-tts-lite", "adm-tts-lite-custom", s.gemini_tts_lite_model || "gemini-3.8-flash-lite-tts");
        setSelectOrCustom("adm-tts-fb2", "adm-tts-fb2-custom", s.gemini_tts_fallback_2 || "");
        // Batch BS: the Groq Whisper (STT) model field — the GET
        // pre-fills it with the current effective model.
        const elGroqWhisper = document.getElementById("adm-groq-whisper-model");
        _admPaintValue(elGroqWhisper, s.groq_whisper_model || "");

        // Admin Watermark Controls
        const elWmFree = document.getElementById("adm-wm-free-enabled");
        const elWmPaid = document.getElementById("adm-wm-paid-enabled");
        const elWmStyle = document.getElementById("adm-wm-style");
        const elWmPos = document.getElementById("adm-wm-position");
        const elWmOpacity = document.getElementById("adm-wm-opacity");
        const elWmSize = document.getElementById("adm-wm-size");
        _admPaintChecked(elWmFree, s.watermark_free_enabled === undefined || s.watermark_free_enabled === null ? true : !!s.watermark_free_enabled);
        _admPaintChecked(elWmPaid, !!s.watermark_paid_enabled);
        _admPaintValue(elWmStyle, s.watermark_style || "tiled");
        _admPaintValue(elWmPos, s.watermark_position || "bottom-right");
        if (s.watermark_opacity != null) _admPaintValue(elWmOpacity, s.watermark_opacity);
        if (s.watermark_size != null) _admPaintValue(elWmSize, s.watermark_size);
        const elWmOpacityVal = document.getElementById("adm-wm-opacity-val");
        if (elWmOpacityVal && elWmOpacity) elWmOpacityVal.textContent = `${elWmOpacity.value || 20}%`;
        const elWmSizeVal = document.getElementById("adm-wm-size-val");
        if (elWmSizeVal && elWmSize) elWmSizeVal.textContent = `${elWmSize.value || 16}%`;

        const elThumbCap = document.getElementById("adm-set-thumb-cap");
        const elThumbModel = document.getElementById("adm-set-thumb-model");
        const elThumbPrompt = document.getElementById("adm-set-thumb-prompt");
        if (s.recap_thumbnail_monthly_cap_usd != null) {
            _admPaintValue(elThumbCap, Number(s.recap_thumbnail_monthly_cap_usd).toFixed(2));
        }
        if (s.recap_thumbnail_model != null) {
            _admPaintValue(elThumbModel, s.recap_thumbnail_model);
        }
        if (s.recap_thumbnail_prompt_template != null) {
            _admPaintValue(elThumbPrompt, s.recap_thumbnail_prompt_template);
        }
        const elThumbUsageTitle = document.getElementById("adm-thumb-usage-title");
        const elThumbUsageStats = document.getElementById("adm-thumb-usage-stats");
        const elThumbStatusBadge = document.getElementById("adm-thumb-status-badge");
        if (elThumbUsageTitle) {
            elThumbUsageTitle.textContent = tf("adm_thumb_usage_title", {
                period: s.recap_thumbnail_month_period || "—",
            });
        }
        if (elThumbUsageStats) {
            const cnt = Number(s.recap_thumbnail_month_count || 0);
            const sp = Number(s.recap_thumbnail_month_spend_usd || 0).toFixed(3);
            const cp = Number(s.recap_thumbnail_monthly_cap_usd ?? 1.8).toFixed(2);
            elThumbUsageStats.textContent = tf("adm_thumb_usage_stats", {
                count: cnt,
                spend: sp,
                cap: cp,
            });
        }
        if (elThumbStatusBadge) {
            const capped = !!s.recap_thumbnail_cap_reached;
            elThumbStatusBadge.textContent = capped ? t("adm_thumb_status_capped") : t("adm_thumb_status_ok");
        }

        updateTutorialUI(s);
        await loadAdminGroupAiConsole();
        _snapshotAdminSettingsValues();
        updateAdminSettingsSaveBar(true);
    } catch (e) {
        console.warn("Could not load dynamic settings:", e);
    }
}

function setSelectOrCustom(selectId, customInputId, val) {
    // Batch BY: same dirty guard as the rest of the settings pane —
    // never repaint a model select (or its custom-value input) that
    // the admin has edited since the last load.
    if (adminSettingsDirty.has(selectId) || adminSettingsDirty.has(customInputId)) return;
    const sel = document.getElementById(selectId);
    const custom = document.getElementById(customInputId);
    if (!sel) return;
    val = (val || "").trim();
    let found = false;
    for (let i = 0; i < sel.options.length; i++) {
        if (sel.options[i].value === val) {
            sel.selectedIndex = i;
            found = true;
            break;
        }
    }
    if (!found && val) {
        sel.value = "custom";
        if (custom) {
            custom.value = val;
            custom.classList.remove("hidden");
        }
    } else {
        if (custom) custom.classList.add("hidden");
    }
}

function getSelectOrCustom(selectId, customInputId, defaultVal) {
    const sel = document.getElementById(selectId);
    const custom = document.getElementById(customInputId);
    if (!sel) return defaultVal;
    if (sel.value === "custom") {
        return (custom?.value?.trim()) || defaultVal;
    }
    return sel.value || defaultVal;
}

function checkCustomModel(selectId) {
    const sel = document.getElementById(selectId);
    const custom = document.getElementById(selectId + "-custom");
    if (sel && custom) {
        if (sel.value === "custom") {
            custom.classList.remove("hidden");
            custom.focus();
        } else {
            custom.classList.add("hidden");
        }
    }
}

async function saveGeminiModelSettings() {
    haptic("medium");
    const primary = getSelectOrCustom("adm-gemini-primary", "adm-gemini-primary-custom", "gemini-3.6-flash");
    const fb1 = getSelectOrCustom("adm-gemini-fb1", "adm-gemini-fb1-custom", "gemini-3.5-flash");
    const fb2 = getSelectOrCustom("adm-gemini-fb2", "adm-gemini-fb2-custom", "gemini-3.5-flash-lite");
    const fb3 = getSelectOrCustom("adm-gemini-fb3", "adm-gemini-fb3-custom", "gemini-flash-lite-latest");

    const payload = {
        gemini_primary_model: primary,
        gemini_fallback_1: fb1,
        gemini_fallback_2: fb2,
        gemini_fallback_3: fb3,
        // Batch BS: the Groq Whisper (STT) model rides the same
        // model-settings save as the Gemini chain.
        groq_whisper_model: document.getElementById("adm-groq-whisper-model")?.value?.trim() || "",
    };

    try {
        const res = await fetch("/api/admin/settings", {
            method: "POST",
            headers: {
                ...getAuthHeaders(),
                "Content-Type": "application/json",
            },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        showStudioToast(t("admin_gemini_saved"), 'success');
        loadAdminSettings();
    } catch (e) {
        showStudioToast(adminErrorText("admin_gemini_save_err", e.message), 'error');
    }
}

async function saveAdminSettings() {
    haptic("medium");
    const kpayNum = document.getElementById("adm-set-kpay-num")?.value?.trim();
    const kpayName = document.getElementById("adm-set-kpay-name")?.value?.trim();
    const waveNum = document.getElementById("adm-set-wave-num")?.value?.trim();
    const waveName = document.getElementById("adm-set-wave-name")?.value?.trim();
    const tutLink = document.getElementById("adm-set-tutorial-link")?.value?.trim();
    const maint = !!document.getElementById("adm-set-maintenance")?.checked;
    const maintNote = document.getElementById("adm-set-maintenance-note")?.value?.trim() || "";
    const groupAi = !!(document.getElementById("adm-group-ai-master-toggle")?.checked || document.getElementById("adm-set-group-ai")?.checked);
    const groupAiCooldownRaw = document.getElementById("adm-set-group-ai-cooldown")?.value?.trim();
    const groupAiCooldown = groupAiCooldownRaw === "" || groupAiCooldownRaw == null ? null : parseInt(groupAiCooldownRaw, 10);
    const groupAiHourlyRaw = document.getElementById("adm-set-group-ai-hourly")?.value?.trim();
    const groupAiHourly = groupAiHourlyRaw === "" || groupAiHourlyRaw == null ? null : parseInt(groupAiHourlyRaw, 10);
    const groupAiDailyRaw = document.getElementById("adm-set-group-ai-daily")?.value?.trim();
    const groupAiDaily = groupAiDailyRaw === "" || groupAiDailyRaw == null ? null : parseInt(groupAiDailyRaw, 10);
    const groupAiModToggle = document.getElementById("adm-group-ai-moderation-toggle");
    const groupAiModEnabled = groupAiModToggle ? !!groupAiModToggle.checked : true;
    const groupAiModThresholdRaw = document.getElementById("adm-group-ai-mod-threshold")?.value?.trim();
    const groupAiModThreshold = groupAiModThresholdRaw === "" || groupAiModThresholdRaw == null ? null : parseFloat(groupAiModThresholdRaw);
    const groupAiMuteS2Raw = document.getElementById("adm-group-ai-mute-s2")?.value?.trim();
    const groupAiMuteS2 = groupAiMuteS2Raw === "" || groupAiMuteS2Raw == null ? null : parseInt(groupAiMuteS2Raw, 10);
    const groupAiMuteS3Raw = document.getElementById("adm-group-ai-mute-s3")?.value?.trim();
    const groupAiMuteS3 = groupAiMuteS3Raw === "" || groupAiMuteS3Raw == null ? null : parseInt(groupAiMuteS3Raw, 10);
    const groupAiWhitelist = (document.getElementById("adm-group-ai-whitelist")?.value || "").trim();
    const groupAiVoiceToggle = document.getElementById("adm-group-ai-voice-toggle");
    const groupAiVoiceEnabled = groupAiVoiceToggle ? !!groupAiVoiceToggle.checked : true;
    const groupAiVoiceCapRaw = document.getElementById("adm-group-ai-voice-cap")?.value?.trim();
    const groupAiVoiceCap = groupAiVoiceCapRaw === "" || groupAiVoiceCapRaw == null ? null : parseInt(groupAiVoiceCapRaw, 10);
    const privateAi = !!document.getElementById("adm-set-private-ai")?.checked;
    const privateAiCooldownRaw = document.getElementById("adm-set-private-ai-cooldown")?.value?.trim();
    const privateAiCooldown = privateAiCooldownRaw === "" || privateAiCooldownRaw == null ? null : parseInt(privateAiCooldownRaw, 10);
    const privateAiHourlyRaw = document.getElementById("adm-set-private-ai-hourly")?.value?.trim();
    const privateAiHourly = privateAiHourlyRaw === "" || privateAiHourlyRaw == null ? null : parseInt(privateAiHourlyRaw, 10);
    const privateAiDailyRaw = document.getElementById("adm-set-private-ai-daily")?.value?.trim();
    const privateAiDaily = privateAiDailyRaw === "" || privateAiDailyRaw == null ? null : parseInt(privateAiDailyRaw, 10);
    const charLimitFreeRaw = document.getElementById("adm-set-char-limit-free")?.value?.trim();
    const charLimitFree = charLimitFreeRaw === "" || charLimitFreeRaw == null ? null : parseInt(charLimitFreeRaw, 10);
    const charLimitPurchasedRaw = document.getElementById("adm-set-char-limit-purchased")?.value?.trim();
    const charLimitPurchased = charLimitPurchasedRaw === "" || charLimitPurchasedRaw == null ? null : parseInt(charLimitPurchasedRaw, 10);
    const freeAllowanceRaw = document.getElementById("adm-set-free-allowance")?.value?.trim();
    const freeAllowance = freeAllowanceRaw === "" || freeAllowanceRaw == null ? null : parseInt(freeAllowanceRaw, 10);
    const dailyJobLimitRaw = document.getElementById("adm-set-daily-job-limit")?.value?.trim();
    const dailyJobLimit = dailyJobLimitRaw === "" || dailyJobLimitRaw == null ? null : parseInt(dailyJobLimitRaw, 10);
    const freeGeminiTasteRaw = document.getElementById("adm-set-free-gemini-taste")?.value?.trim();
    const freeGeminiTaste = freeGeminiTasteRaw === "" || freeGeminiTasteRaw == null ? null : parseInt(freeGeminiTasteRaw, 10);
    const freeGeminiBudgetRaw = document.getElementById("adm-set-free-gemini-budget")?.value?.trim();
    const freeGeminiBudget = freeGeminiBudgetRaw === "" || freeGeminiBudgetRaw == null ? null : parseInt(freeGeminiBudgetRaw, 10);
    const recapFreeMaxRaw = document.getElementById("adm-set-recap-free-max")?.value?.trim();
    const recapFreeMax = recapFreeMaxRaw === "" || recapFreeMaxRaw == null ? null : parseInt(recapFreeMaxRaw, 10);
    const recapMaxRaw = document.getElementById("adm-set-recap-max")?.value?.trim();
    const recapMax = recapMaxRaw === "" || recapMaxRaw == null ? null : parseInt(recapMaxRaw, 10);
    const voiceDailyFreeRaw = document.getElementById("adm-set-voice-daily-free")?.value?.trim();
    const voiceDailyFree = voiceDailyFreeRaw === "" || voiceDailyFreeRaw == null ? null : parseInt(voiceDailyFreeRaw, 10);
    const voiceDailyPaidRaw = document.getElementById("adm-set-voice-daily-paid")?.value?.trim();
    const voiceDailyPaid = voiceDailyPaidRaw === "" || voiceDailyPaidRaw == null ? null : parseInt(voiceDailyPaidRaw, 10);
    const ttsDailyFreeRaw = document.getElementById("adm-set-tts-daily-free")?.value?.trim();
    const ttsDailyFree = ttsDailyFreeRaw === "" || ttsDailyFreeRaw == null ? null : parseInt(ttsDailyFreeRaw, 10);
    const ttsDailyPaidRaw = document.getElementById("adm-set-tts-daily-paid")?.value?.trim();
    const ttsDailyPaid = ttsDailyPaidRaw === "" || ttsDailyPaidRaw == null ? null : parseInt(ttsDailyPaidRaw, 10);
    const voxcpmTimeoutRaw = document.getElementById("adm-set-voxcpm-timeout")?.value?.trim();
    const voxcpmTimeout = voxcpmTimeoutRaw === "" || voxcpmTimeoutRaw == null ? null : parseInt(voxcpmTimeoutRaw, 10);
    const tutVideoEnEl = document.getElementById("adm-set-tutorial-video-enabled");
    const tutVideoEn = tutVideoEnEl ? !!tutVideoEnEl.checked : true;
    const tieredPricing = !!document.getElementById("adm-set-tiered-pricing")?.checked;
    const reqChannelId = document.getElementById("adm-set-required-channel-id")?.value?.trim();
    const reqGroupId = document.getElementById("adm-set-required-group-id")?.value?.trim();
    const channelInvite = document.getElementById("adm-set-channel-invite-link")?.value?.trim();
    const groupInvite = document.getElementById("adm-set-group-invite-link")?.value?.trim();

    const tutVoiceEn = !!document.getElementById("adm-tut-voice-enabled")?.checked;
    const tutVoiceTitle = document.getElementById("adm-tut-voice-title")?.value?.trim() || t("tutorial_default_voice_title");
    const tutVoiceLink = document.getElementById("adm-tut-voice-link")?.value?.trim() || "";

    const tutRecapEn = !!document.getElementById("adm-tut-recap-enabled")?.checked;
    const tutRecapTitle = document.getElementById("adm-tut-recap-title")?.value?.trim() || t("tutorial_default_recap_title");
    const tutRecapLink = document.getElementById("adm-tut-recap-link")?.value?.trim() || "";
    const recapDemoAsset = document.getElementById("adm-recap-demo-asset")?.value?.trim() || "";

    const tutExtraLinks = {};
    for (const [elId, settingKey] of ADMIN_TUTORIAL_LINK_FIELDS) {
        tutExtraLinks[settingKey] = document.getElementById(elId)?.value?.trim() || "";
    }
    const thumbCapRaw = document.getElementById("adm-set-thumb-cap")?.value?.trim();
    const thumbCap = thumbCapRaw === "" || thumbCapRaw == null ? null : parseFloat(thumbCapRaw);
    const thumbModel = document.getElementById("adm-set-thumb-model")?.value?.trim();
    const thumbPrompt = document.getElementById("adm-set-thumb-prompt")?.value?.trim();

    const payload = {
        payment_kpay_number: kpayNum,
        payment_kpay_name: kpayName,
        payment_wave_number: waveNum,
        payment_wave_name: waveName,
        tutorial_video_link: tutLink,
        tutorial_voice_enabled: tutVoiceEn,
        tutorial_voice_title: tutVoiceTitle,
        tutorial_voice_link: tutVoiceLink,
        tutorial_recap_enabled: tutRecapEn,
        tutorial_recap_title: tutRecapTitle,
        tutorial_recap_link: tutRecapLink,
        // Batch L demo slot asset (file_id or URL; "" hides the slot).
        recap_demo_asset: recapDemoAsset,
        ...tutExtraLinks,
        maintenance_mode: maint,
        maintenance_note: maintNote,
        group_ai_admin_enabled: groupAi,
        ...(Number.isFinite(groupAiCooldown) ? { group_ai_user_cooldown_seconds: groupAiCooldown } : {}),
        ...(Number.isFinite(groupAiHourly) ? { group_ai_hourly_budget: groupAiHourly } : {}),
        ...(Number.isFinite(groupAiDaily) ? { group_ai_daily_budget: groupAiDaily } : {}),
        group_ai_moderation_enabled: groupAiModEnabled,
        ...(Number.isFinite(groupAiModThreshold) ? { group_ai_moderation_confidence_threshold: groupAiModThreshold } : {}),
        ...(Number.isFinite(groupAiMuteS2) ? { group_ai_moderation_mute_duration_strike2_seconds: groupAiMuteS2 } : {}),
        ...(Number.isFinite(groupAiMuteS3) ? { group_ai_moderation_mute_duration_strike3_seconds: groupAiMuteS3 } : {}),
        group_ai_moderation_whitelist: groupAiWhitelist,
        group_ai_voice_enabled: groupAiVoiceEnabled,
        ...(Number.isFinite(groupAiVoiceCap) ? { group_ai_voice_daily_cap: groupAiVoiceCap } : {}),
        private_ai_enabled: privateAi,
        ...(Number.isFinite(privateAiCooldown) ? { private_ai_user_cooldown_seconds: privateAiCooldown } : {}),
        ...(Number.isFinite(privateAiHourly) ? { private_ai_hourly_budget: privateAiHourly } : {}),
        ...(Number.isFinite(privateAiDaily) ? { private_ai_daily_budget: privateAiDaily } : {}),
        ...(Number.isFinite(charLimitFree) ? { character_limit_free: charLimitFree } : {}),
        ...(Number.isFinite(charLimitPurchased) ? { character_limit_purchased: charLimitPurchased } : {}),
        ...(Number.isFinite(freeAllowance) ? { free_monthly_allowance_seconds: freeAllowance } : {}),
        ...(Number.isFinite(dailyJobLimit) ? { free_daily_job_limit: dailyJobLimit } : {}),
        ...(Number.isFinite(freeGeminiTaste) ? { free_gemini_taste_minutes: freeGeminiTaste } : {}),
        ...(Number.isFinite(freeGeminiBudget) ? { free_gemini_daily_budget_requests: freeGeminiBudget } : {}),
        ...(Number.isFinite(recapFreeMax) ? { recap_free_max_duration_seconds: recapFreeMax } : {}),
        ...(Number.isFinite(recapMax) ? { recap_max_duration_seconds: recapMax } : {}),
        ...(Number.isFinite(voiceDailyFree) ? { voice_daily_limit_minutes_free: voiceDailyFree } : {}),
        ...(Number.isFinite(voiceDailyPaid) ? { voice_daily_limit_minutes_paid: voiceDailyPaid } : {}),
        ...(Number.isFinite(ttsDailyFree) ? { tts_daily_limit_minutes_free: ttsDailyFree } : {}),
        ...(Number.isFinite(ttsDailyPaid) ? { tts_daily_limit_minutes_paid: ttsDailyPaid } : {}),
        ...(Number.isFinite(voxcpmTimeout) ? { voxcpm_timeout: voxcpmTimeout } : {}),
        tutorial_video_enabled: tutVideoEn,
        tiered_recap_pricing_enabled: tieredPricing,
        ...(reqChannelId ? { required_channel_id: reqChannelId } : {}),
        ...(reqGroupId ? { required_group_id: reqGroupId } : {}),
        ...(channelInvite ? { channel_invite_link: channelInvite } : {}),
        ...(groupInvite ? { group_invite_link: groupInvite } : {}),
        ..._collectWatermarkPayload(),
        ...(Number.isFinite(thumbCap) ? { recap_thumbnail_monthly_cap_usd: thumbCap } : {}),
        ...(thumbModel ? { recap_thumbnail_model: thumbModel } : {}),
        ...(thumbPrompt ? { recap_thumbnail_prompt_template: thumbPrompt } : {}),
    };
    updateTutorialUI(payload);

    try {
        const res = await fetch("/api/admin/settings", {
            method: "POST",
            headers: {
                ...getAuthHeaders(),
                "Content-Type": "application/json",
            },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        // Batch BY: the save succeeded — clear the edit-clobber guard
        // so the post-save reload below paints fresh server truth
        // over every field again.
        adminSettingsDirty.clear();
        _snapshotAdminSettingsValues();
        updateAdminSettingsSaveBar(true);
        showStudioToast(data.message || t("admin_settings_saved"), 'success');
        loadAdminSettings();
    } catch (e) {
        showStudioToast(adminErrorText("admin_settings_save_err", e.message), 'error');
    }
}

async function saveGeminiTtsModelSettings() {
    haptic("medium");
    const primary = getSelectOrCustom("adm-tts-primary", "adm-tts-primary-custom", "gemini-3.8-flash-tts");
    const fb1 = getSelectOrCustom("adm-tts-fb1", "adm-tts-fb1-custom", "");
    const lite = getSelectOrCustom("adm-tts-lite", "adm-tts-lite-custom", "gemini-3.8-flash-lite-tts");
    const fb2 = getSelectOrCustom("adm-tts-fb2", "adm-tts-fb2-custom", "");

    const payload = {
        gemini_tts_primary_model: primary,
        gemini_tts_fallback_1: fb1,
        gemini_tts_lite_model: lite,
        gemini_tts_fallback_2: fb2,
    };

    try {
        const res = await fetch("/api/admin/settings", {
            method: "POST",
            headers: {
                ...getAuthHeaders(),
                "Content-Type": "application/json",
            },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        [
            "adm-tts-primary", "adm-tts-primary-custom",
            "adm-tts-fb1", "adm-tts-fb1-custom",
            "adm-tts-lite", "adm-tts-lite-custom",
            "adm-tts-fb2", "adm-tts-fb2-custom",
        ].forEach(id => adminSettingsDirty.delete(id));
        showStudioToast(t("admin_tts_chain_saved"), "success");
        loadAdminSettings();
    } catch (e) {
        showStudioToast(adminErrorText("admin_tts_chain_save_err", e.message), "error");
    }
}
window.saveGeminiTtsModelSettings = saveGeminiTtsModelSettings;

function _collectWatermarkPayload() {
    const elWmFree = document.getElementById("adm-wm-free-enabled");
    const elWmPaid = document.getElementById("adm-wm-paid-enabled");
    const elWmStyle = document.getElementById("adm-wm-style");
    const elWmPos = document.getElementById("adm-wm-position");
    const elWmOpacity = document.getElementById("adm-wm-opacity");
    const elWmSize = document.getElementById("adm-wm-size");
    const out = {};
    if (elWmFree) out.watermark_free_enabled = !!elWmFree.checked;
    if (elWmPaid) out.watermark_paid_enabled = !!elWmPaid.checked;
    if (elWmStyle && elWmStyle.value) out.watermark_style = elWmStyle.value.trim().toLowerCase();
    if (elWmPos && elWmPos.value) out.watermark_position = elWmPos.value.trim().toLowerCase();
    if (elWmOpacity) {
        const op = parseInt(elWmOpacity.value, 10);
        if (Number.isFinite(op)) out.watermark_opacity = op;
    }
    if (elWmSize) {
        const sz = parseInt(elWmSize.value, 10);
        if (Number.isFinite(sz)) out.watermark_size = sz;
    }
    return out;
}

async function saveWatermarkSettings() {
    haptic("medium");
    const payload = _collectWatermarkPayload();
    try {
        const res = await fetch("/api/admin/settings", {
            method: "POST",
            headers: {
                ...getAuthHeaders(),
                "Content-Type": "application/json",
            },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        [
            "adm-wm-free-enabled",
            "adm-wm-paid-enabled",
            "adm-wm-style",
            "adm-wm-position",
            "adm-wm-opacity",
            "adm-wm-size",
        ].forEach(id => adminSettingsDirty.delete(id));
        showStudioToast(t("adm_wm_saved"), "success");
        loadAdminSettings();
    } catch (e) {
        showStudioToast(adminErrorText("adm_wm_save_err", e.message), "error");
    }
}
window.saveWatermarkSettings = saveWatermarkSettings;

async function saveAiThumbnailSettings() {
    haptic("medium");
    const capRaw = document.getElementById("adm-set-thumb-cap")?.value?.trim();
    const capVal = capRaw === "" || capRaw == null ? 1.80 : parseFloat(capRaw);
    const modelVal = document.getElementById("adm-set-thumb-model")?.value?.trim() || "black-forest-labs/FLUX.1-schnell";
    const promptVal = document.getElementById("adm-set-thumb-prompt")?.value?.trim() || "";

    const payload = {
        ...(Number.isFinite(capVal) ? { recap_thumbnail_monthly_cap_usd: capVal } : {}),
        recap_thumbnail_model: modelVal,
        ...(promptVal ? { recap_thumbnail_prompt_template: promptVal } : {}),
    };

    try {
        const res = await fetch("/api/admin/settings", {
            method: "POST",
            headers: {
                ...getAuthHeaders(),
                "Content-Type": "application/json",
            },
            body: JSON.stringify(payload),
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        ["adm-set-thumb-cap", "adm-set-thumb-model", "adm-set-thumb-prompt"].forEach(id => adminSettingsDirty.delete(id));
        showStudioToast(t("adm_thumb_saved_toast"), "success");
        loadAdminSettings();
    } catch (e) {
        showStudioToast(adminErrorText("admin_settings_save_err", e.message), "error");
    }
}
window.saveAiThumbnailSettings = saveAiThumbnailSettings;

function formatIsoTime(isoStr) {
    if (!isoStr) return "";
    try {
        const d = new Date(isoStr);
        if (isNaN(d.getTime())) return isoStr;
        return d.toLocaleDateString() + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    } catch (e) {
        return isoStr;
    }
}

async function loadAdminGroupAiConsole() {
    try {
        const res = await fetch("/api/admin/group-ai", {
            headers: getAuthHeaders(),
        });
        if (!res.ok) return;
        const data = await res.json();

        const sheetEl = document.getElementById("adm-group-ai-sheet");
        const charsEl = document.getElementById("adm-group-ai-sheet-chars");
        if (sheetEl) {
            sheetEl.value = data.knowledge_sheet || "";
            if (charsEl) charsEl.textContent = String(sheetEl.value.length);
        }

        const metaEl = document.getElementById("adm-group-ai-sheet-meta");
        if (metaEl) {
            if (data.is_custom && data.last_saved_at) {
                const timeStr = formatIsoTime(data.last_saved_at);
                metaEl.textContent = tf("adm_group_ai_sheet_last_saved", {
                    time: timeStr,
                    user: data.last_saved_by || "Admin",
                });
            } else {
                metaEl.textContent = t("adm_group_ai_sheet_default");
            }
        }

        const b = data.budgets || {};
        const bReplies = document.getElementById("adm-group-ai-budget-replies");
        if (bReplies) bReplies.textContent = `${b.today_replies ?? 0} / ${b.daily_reply_cap ?? 300}`;
        const bHourly = document.getElementById("adm-group-ai-budget-hourly");
        if (bHourly) bHourly.textContent = `${b.hourly_replies ?? 0} / ${b.hourly_budget ?? 40}`;
        const bVoice = document.getElementById("adm-group-ai-budget-voice");
        if (bVoice) bVoice.textContent = `${b.today_voice ?? 0} / ${b.daily_voice_cap ?? 60}`;

        const recentEl = document.getElementById("adm-group-ai-recent-actions");
        if (recentEl) {
            const actions = data.recent_actions || [];
            if (actions.length === 0) {
                recentEl.innerHTML = `<p class="text-muted text-xs p-2">${escapeHtml(t("adm_group_ai_no_actions"))}</p>`;
            } else {
                let html = '<div class="table-responsive"><table class="table-glass text-xs w-full">';
                html += `<thead><tr>
                    <th>${escapeHtml(t("adm_group_ai_col_user"))}</th>
                    <th>${escapeHtml(t("adm_group_ai_col_category"))}</th>
                    <th>${escapeHtml(t("adm_group_ai_col_action"))}</th>
                    <th>${escapeHtml(t("adm_group_ai_col_time"))}</th>
                </tr></thead><tbody>`;
                for (const act of actions) {
                    const actionBadge = act.action === "warn"
                        ? '<span class="vcp-badge vcp-badge-info">Strike 1 (warn)</span>'
                        : act.action === "mute_1h"
                        ? '<span class="vcp-badge vcp-badge-warning">Strike 2 (mute 1h)</span>'
                        : act.action === "mute_24h"
                        ? '<span class="vcp-badge vcp-badge-danger">Strike 3 (mute 24h)</span>'
                        : `<span class="vcp-badge">${escapeHtml(act.action)}</span>`;
                    const banBadge = act.banned ? ' <span class="vcp-badge vcp-badge-danger">BANNED</span>' : "";
                    const timeStr = formatIsoTime(act.created_at);
                    html += `<tr>
                        <td><code>${escapeHtml(String(act.user_id))}</code></td>
                        <td>${escapeHtml(act.category)}</td>
                        <td>${actionBadge}${banBadge}</td>
                        <td class="text-muted">${escapeHtml(timeStr)}</td>
                    </tr>`;
                }
                html += "</tbody></table></div>";
                recentEl.innerHTML = html;
            }
        }
    } catch (err) {
        console.warn("Could not load group ai console:", err);
    }
}

async function saveGroupAiKnowledgeSheet() {
    haptic("medium");
    const sheetEl = document.getElementById("adm-group-ai-sheet");
    if (!sheetEl) return;
    const sheet = sheetEl.value.trim();
    if (!sheet) {
        showStudioToast(t("err_generic"), "error");
        return;
    }
    if (sheet.length > 20000) {
        showStudioToast("Knowledge sheet exceeds 20,000 characters", "error");
        return;
    }
    try {
        const res = await fetch("/api/admin/group-ai/knowledge-sheet", {
            method: "POST",
            headers: {
                ...getAuthHeaders(),
                "Content-Type": "application/json",
            },
            body: JSON.stringify({ sheet }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            showStudioToast(data.detail || t("err_generic"), "error");
            return;
        }
        showStudioToast(t("adm_group_ai_sheet_saved"), "success");
        await loadAdminGroupAiConsole();
    } catch (err) {
        showStudioToast((err && err.message) ? err.message : t("err_generic"), "error");
    }
}

async function resetGroupAiKnowledgeSheet() {
    haptic("medium");
    showStudioConfirm(t("adm_group_ai_reset_sheet") + "?", async () => {
        try {
            const res = await fetch("/api/admin/group-ai/knowledge-sheet/reset", {
                method: "POST",
                headers: getAuthHeaders(),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                showStudioToast(data.detail || t("err_generic"), "error");
                return;
            }
            showStudioToast(t("adm_group_ai_sheet_reset"), "success");
            await loadAdminGroupAiConsole();
        } catch (err) {
            showStudioToast((err && err.message) ? err.message : t("err_generic"), "error");
        }
    });
}

window.loadAdminGroupAiConsole = loadAdminGroupAiConsole;
window.saveGroupAiKnowledgeSheet = saveGroupAiKnowledgeSheet;
window.resetGroupAiKnowledgeSheet = resetGroupAiKnowledgeSheet;

// ── Dedicated History Hub ────────────────────────────────────────────────
// One unified, newest-first list of ALL the user's jobs (voice + recap),
// served by the existing GET /api/history endpoint (payload unchanged).
// The per-studio history lists were removed; studios link here instead.
let hubHistoryFilter = "all";
let hubHistoryCache = null;      // last fetched history array (null = not loaded yet)
let hubHistoryError = false;     // last fetch failed
let hubOpenDetailId = null;      // job id whose detail block is expanded

function openHistoryHub() {
    switchTab("tab-history");
    loadUserHistory();
}
window.openHistoryHub = openHistoryHub;

function setHubHistoryFilter(filter) {
    hubHistoryFilter = filter || "all";
    document.querySelectorAll(".hub-filter-chip").forEach(c => {
        c.classList.toggle("active", c.getAttribute("data-hub-filter") === hubHistoryFilter);
    });
    renderHistoryHub();
}
window.setHubHistoryFilter = setHubHistoryFilter;

function toggleHistoryDetail(jobId) {
    haptic("light");
    hubOpenDetailId = (hubOpenDetailId === jobId) ? null : jobId;
    document.querySelectorAll("#hub-history-list .hub-row").forEach(row => {
        const open = row.getAttribute("data-job-id") === hubOpenDetailId;
        row.classList.toggle("open", open);
        const det = row.querySelector(".hub-detail");
        if (det) det.classList.toggle("hidden", !open);
        const chev = row.querySelector(".hub-chevron");
        if (chev) chev.textContent = open ? "⌃" : "›";
        if (open) wireHubDetailAudio(row);
    });
}
window.toggleHistoryDetail = toggleHistoryDetail;

// Lazy-load History audio only when interacted with (saves multi-MB egress on row expand)
function wireHubDetailAudio(scope) {
    if (!scope) return;
    scope.querySelectorAll("audio[data-auth-src]").forEach(audio => {
        if (audio.getAttribute("src") || audio.dataset.lazyWired) return;
        audio.dataset.lazyWired = "1";
        const url = audio.getAttribute("data-auth-src");
        if (/[?&]t=/.test(url || "")) { audio.preload = "none"; audio.src = url; return; }
        const load = async () => {
            if (audio.getAttribute("src") || audio.dataset.loading) return;
            audio.dataset.loading = "1";
            if (await setAuthMediaSource(audio, url)) { audio.play().catch(() => {}); }
            delete audio.dataset.loading;
        };
        audio.addEventListener("pointerdown", load, { once: true });
        audio.addEventListener("focus", load, { once: true });
    });
}

function hubStatusBorder(status) {
    switch ((status || "").toUpperCase()) {
        case "SUCCESS": return "border-success-dim";
        case "FAILED": return "border-failed-dim";
        case "RUNNING": return "border-running-dim";
        case "QUEUED": return "border-warning-dim";
        default: return "";
    }
}

function formatJobDuration(j) {
    if (!j || typeof j !== "object") return "";
    const realSec = Number(j.seconds_consumed || 0);
    const dispSec = Number(j.duration_seconds || 0);
    const estSec = Number(j.seconds_estimated || 0);
    const sec = realSec > 0 ? realSec : (dispSec > 0 ? dispSec : estSec);
    if (!Number.isFinite(sec) || sec <= 0) return "";
    const whole = Math.round(sec);
    if (whole < 60) return tf("hist_duration_sec", { sec: whole });
    const min = Math.floor(whole / 60);
    const rem = whole % 60;
    return rem > 0 ? tf("hist_duration_min_sec", { min, sec: rem }) : tf("hist_duration_min", { min });
}
window.formatJobDuration = formatJobDuration;

// Display-only 22h expiry reminder helper (media retention remains 24h).
function computeHistoryExpiryState(j) {
    if (!j || j.is_expired) {
        return { expired: true, expiresSoon: false, hoursLeft: 0, minsLeft: 0, timeText: "" };
    }
    if (!j.created_at) {
        return { expired: false, expiresSoon: false, hoursLeft: null, minsLeft: null, timeText: "" };
    }
    const createdMs = Date.parse(j.created_at);
    if (Number.isNaN(createdMs)) {
        return { expired: false, expiresSoon: false, hoursLeft: null, minsLeft: null, timeText: "" };
    }
    const ageMs = Date.now() - createdMs;
    const remainingMs = Math.max(0, 24 * 3600000 - ageMs);
    if (remainingMs <= 0) {
        return { expired: true, expiresSoon: false, hoursLeft: 0, minsLeft: 0, timeText: "" };
    }
    const hoursLeft = Math.max(0, Math.ceil(remainingMs / 3600000));
    const minsLeft = Math.max(1, Math.ceil(remainingMs / 60000));
    const expiresSoon = ageMs >= 22 * 3600000;
    let timeText = "";
    if (expiresSoon) {
        if (minsLeft < 60) {
            timeText = tf("hist_time_m", { m: minsLeft });
        } else {
            const h = Math.floor(minsLeft / 60);
            const m = minsLeft % 60;
            timeText = m > 0 ? tf("hist_time_hm", { h, m }) : tf("hist_time_h", { h });
        }
    }
    return { expired: false, expiresSoon, hoursLeft, minsLeft, timeText };
}
window.computeHistoryExpiryState = computeHistoryExpiryState;

// Detail block shown when a hub row is tapped. It reuses the exact same
// actions/handlers the old per-studio history cards offered (download,
// send-to-Telegram, in-player view, live tracking) — nothing new invented.
function hubHistoryDetailHtml(j) {
    const isRecap = j.job_type === "RECAP";
    const exp = computeHistoryExpiryState(j);
    let inner = "";
    if (exp.expiresSoon && !exp.expired && j.status === "SUCCESS") {
        inner += `<div class="history-item-body hub-expires-soon-banner">${escapeHtml(tf("hist_expires_soon_banner", { time: exp.timeText }))}</div>`;
    }
    if (j.fallback_used) {
        // Round-6 P3: the API has carried fallback_used all along
        // (round-4 F-G) but History never rendered it — a degraded
        // result looked identical to a genuine one.
        const fbNote = isRecap
            ? tf("recap_fallback_note", { count: (j.fallback_segments || []).length || 1 })
            : t("voice_fallback_notice");
        inner += `<div class="history-item-body hub-fallback-note">⚠️ ${escapeHtml(fbNote)}</div>`;
    }
    if (!isRecap && j.reference_trimmed) {
        // Batch BU: the persisted reference auto-trim disclosure,
        // mirroring the fallback note above (voice jobs only).
        inner += `<div class="history-item-body hub-fallback-note">✂️ ${escapeHtml(t("voice_ref_trim_notice"))}</div>`;
    }
    if (isRecap && j.parts_count && j.parts_count > 1) {
        inner += `<div class="history-item-body">${escapeHtml(tf("recap_segmented_cost", { parts: j.parts_count, credits: (j.credits_charged ?? j.parts_count) }))}</div>`;
    }
    // Batch K: the per-job receipt — every completed recap shows the
    // credits it was charged (segmented jobs carry it in the line
    // above), and a failed job shows its refund marker when — and
    // only when — the server record proves a refund was issued.
    if (isRecap && j.status === "SUCCESS" && !(j.parts_count > 1)
        && typeof j.credits_charged === "number" && j.credits_charged > 0) {
        inner += `<div class="history-item-body">${escapeHtml(tf("recap_cost_charged_line", { credits: j.credits_charged }))}</div>`;
    }
    if (isRecap && j.status === "FAILED" && j.credit_refunded) {
        inner += `<div class="history-item-body">${escapeHtml(tf("recap_refunded_line", { credits: j.credits_refunded || j.credits_charged || 1 }))}</div>`;
    }
    if (j.status === "FAILED") {
        inner += `<div class="history-item-body hub-error-snippet text-danger">⚠️ ${escapeHtml(j.error_message || t("recap_failed_title"))}</div>`;
    }
    if (isRecap && j.recap_settings) {
        // R40 item 13: compact settings summary from the persisted
        // recap_config (engine / voice / subtitle position / style /
        // font) so the user — and support — can see what a render used.
        const s = j.recap_settings;
        const parts = [];
        if (s.engine) parts.push(s.engine === "gemini" ? "Gemini" : s.engine === "edge" ? "Edge" : "Smart Sync");
        if (s.gemini_voice || s.voice) parts.push(s.gemini_voice || s.voice);
        if (s.story_style) parts.push(s.story_style);
        if (s.sub_style) parts.push(s.sub_style);
        if (s.sub_font) parts.push(s.sub_font);
        // Final-audit batch M: the persisted config stores sub_y /
        // blur_y as INTEGER percents (85 — the recap form's sliders
        // are 0-100), so multiplying by 100 rendered "8500%". Show
        // the stored percent as-is; a legacy fraction (0.85) is
        // still handled defensively.
        const pctOf = (v) => Math.round(v <= 1 ? v * 100 : v);
        // Batch AK: AK jobs carry the bottom margin — label it as
        // what it is; pre-AK jobs keep the centre-% line.
        if (typeof s.sub_bottom_margin === "number") parts.push(tf("hist_sub_margin_lbl", { pct: pctOf(s.sub_bottom_margin) }));
        else if (typeof s.sub_y === "number") parts.push(tf("hist_sub_pos_lbl", { pct: pctOf(s.sub_y) }));
        if (typeof s.blur_y === "number") parts.push(tf("hist_blur_pos_lbl", { pct: pctOf(s.blur_y) }));
        if (parts.length) {
            inner += `<div class="history-item-body hub-settings-summary">⚙️ ${escapeHtml(parts.join(" · "))}</div>`;
        }
    }
    let actions = "";
    if (isRecap && j.script_ready) {
        // Batch N: a job PARKED at script review — its row reads
        // RUNNING but nothing is rendering; it waits for the user's
        // review and auto-refunds if left unreviewed for 24h. Offer
        // the same review action the Track poller opens, plus
        // cancel / re-run prefill / delete like the other states
        // (delete answers the server's terminal-only 409 as a toast
        // while the job is still parked — cancel is the way out).
        let parkedNote = t("hist_script_ready_badge");
        if (j.script_ready_at) {
            const parkedMs = Date.parse(j.script_ready_at);
            if (!Number.isNaN(parkedMs)) {
                const hoursLeft = Math.max(0, Math.ceil(24 - (Date.now() - parkedMs) / 3600000));
                parkedNote += " · " + tf("hist_script_ready_expires", { hours: hoursLeft });
            }
        }
        actions = `<span class="vcp-badge vcp-badge-warning">📝 ${escapeHtml(parkedNote)}</span>`;
        actions += ` <button type="button" onclick="openRecapScriptEditor('${j.id}')" class="history-dl-link">${t("btn_review_script")}</button>`;
        actions += ` <button type="button" onclick="cancelRecapJob('${j.id}')" class="history-dl-link">${t("btn_cancel_job")}</button>`;
        if (j.recap_config) {
            actions += ` <button type="button" onclick="rerunRecapFromHistory('${j.id}')" class="history-dl-link">${t("btn_rerun_recap")}</button>`;
        }
        actions += ` <button type="button" onclick="deleteHistoryItem('${j.id}')" class="history-dl-link">${t("btn_delete_history")}</button>`;
    } else if (j.status === "FAILED") {
        actions = `<span class="vcp-badge vcp-badge-danger">${isRecap ? t("hist_failed_recap") : t("hist_failed")}</span>`;
        // Batch L: one-tap retry (hidden when the server says the
        // source is gone — the note explains instead), re-run prefill
        // and delete for recap items; delete for every terminal item.
        if (isRecap) {
            if (j.can_retry !== false) {
                actions += ` <button type="button" onclick="retryRecapJob('${j.id}')" class="history-dl-link">${t("btn_retry_job")}</button>`;
            } else {
                actions += ` <button type="button" class="history-dl-link is-disabled" disabled title="${escapeHtml(t("recap_retry_upload_missing"))}">${escapeHtml(t("btn_retry_job"))}</button>`;
                inner += `<div class="history-item-body text-xs text-muted mt-1">${escapeHtml(t("recap_retry_upload_missing"))}</div>`;
            }
            if (j.recap_config) {
                actions += ` <button type="button" onclick="rerunRecapFromHistory('${j.id}')" class="history-dl-link">${t("btn_rerun_recap")}</button>`;
            }
        }
        actions += ` <button type="button" onclick="deleteHistoryItem('${j.id}')" class="history-dl-link">${t("btn_delete_history")}</button>`;
    } else if (j.status === "RUNNING" || j.status === "QUEUED") {
        actions = isRecap
            ? `<button type="button" onclick="openRecapDetail('${j.id}')" class="history-dl-link">${t("hist_track_recap")}</button> <button type="button" onclick="cancelRecapJob('${j.id}')" class="history-dl-link">${t("btn_cancel_job")}</button>`
            : `<button type="button" onclick="openVoiceDetail('${j.id}')" class="history-dl-link">${t("hist_track_voice")}</button>`;
    } else if (j.is_expired) {
        actions = `<span class="badge-expired vcp-badge vcp-badge-danger" title="">${t("hist_expired")}</span>`;
    } else if (isRecap) {
        if (j.deliverables?.video) {
            actions += `<button type="button" onclick="openRecapInPlayer('${j.id}')" class="history-dl-link">${t("hist_watch")}</button> <a href="#" data-dl-url="${j.deliverables.video}" data-dl-name="${brandedDlName("Recap", "mp4", j.input_snippet, j.id)}" class="history-dl-link">${escapeHtml(t("hist_dl_video"))}</a>`;
            // Over-50MB rule: no Telegram action at all for outputs the
            // bot cannot send — Download is the only delivery action.
            if (j.telegram_sendable !== false) {
                actions += ` <button type="button" onclick="sendRecapToTelegram('${j.id}')" class="history-dl-link tg-btn">${t("hist_send_telegram")}</button>`;
            }
        }
        if (j.deliverables?.audio) actions += ` <a href="#" data-dl-url="${j.deliverables.audio}" data-dl-name="${brandedDlName("RecapAudio", "mp3", j.input_snippet, j.id)}" class="history-dl-link">${escapeHtml(t("hist_dl_audio"))}</a>`;
        if (j.deliverables?.srt) actions += ` <a href="#" data-dl-url="${j.deliverables.srt}" data-dl-name="${brandedDlName("Subtitles", "srt", j.input_snippet, j.id)}" class="history-dl-link">${escapeHtml(t("hist_dl_srt"))}</a>`;
        if (j.deliverables?.script) actions += ` <a href="#" data-dl-url="${j.deliverables.script}" data-dl-name="${brandedDlName("Script", "txt", j.input_snippet, j.id)}" class="history-dl-link">${escapeHtml(t("hist_dl_txt"))}</a>`;
        if (j.deliverables?.thumbnail) actions += ` <a href="#" data-dl-url="${j.deliverables.thumbnail}" data-dl-name="${brandedDlName("Thumbnail", "jpg", j.input_snippet, j.id)}" class="history-dl-link">${escapeHtml(t("hist_dl_thumbnail"))}</a>`;
        const canThumb = !!(state.canUseAiThumbnail || state.user?.is_vip || state.user?.is_admin);
        const capFull = !!state.aiThumbnailCapReached;
        const prefillHeadline = escapeHtml(j.input_snippet || "");
        const thumbNote = !canThumb
            ? escapeHtml(t("recap_thumb_vip_locked_note"))
            : (capFull ? escapeHtml(t("recap_thumb_cap_reached")) : escapeHtml(t("recap_thumb_free_perk_note")));
        inner += `
        <div class="history-item-body recap-hist-thumb-box mt-2">
            <label class="text-xs" for="hist-thumb-headline-${j.id}">${escapeHtml(t("recap_thumb_headline_lbl"))}</label>
            <div class="recap-hist-thumb-row mt-1">
                <input type="text" id="hist-thumb-headline-${j.id}" class="studio-input vcp-input" maxlength="120" value="${prefillHeadline}" placeholder="${escapeHtml(t("recap_thumb_headline_ph"))}" ${(!canThumb || capFull) ? "disabled" : ""}>
                <button type="button" id="btn-hist-thumb-${j.id}" class="history-dl-link" onclick="generateRecapAiThumbnail('${j.id}', 'hist-thumb-headline-${j.id}')" ${capFull ? "disabled" : ""}>${escapeHtml(t("btn_recap_ai_thumbnail"))}</button>
            </div>
            <div class="text-xs text-muted mt-1">${thumbNote}</div>
        </div>`;
    } else if (j.download_url) {
        actions = `<a href="#" data-dl-url="${j.download_url}" data-dl-name="${brandedDlName("Voice", "wav", null, j.id)}" class="history-dl-link">${escapeHtml(t("hist_dl_voice"))}</a>`;
        if (j.telegram_sendable !== false) {
            actions += ` <button type="button" onclick="sendVoiceToTelegram('${j.id}')" class="history-dl-link tg-btn">${t("hist_send_telegram")}</button>`;
        }
    }
    if (!isRecap && j.status === "SUCCESS" && !j.is_expired && j.download_url) {
        // Journey audit 2026-11-02: a finished voice job opened from
        // History offered only Download/Telegram — the player existed
        // solely on the Voice result view. Same auth-blob player
        // pattern (data-auth-src + setAuthMediaSource), wired when
        // the detail opens (see wireHubDetailAudio).
        inner += `<div class="hub-detail-player"><audio class="hub-detail-audio" controls preload="none" data-auth-src="${j.download_url}"></audio></div>`;
    }
    // Batch L: terminal SUCCESS/CANCELLED items also get re-run
    // prefill (recap, when the stored config survived) and delete.
    if (j.status === "SUCCESS" || j.status === "CANCELLED") {
        if (isRecap && j.recap_config) {
            actions += ` <button type="button" onclick="rerunRecapFromHistory('${j.id}')" class="history-dl-link">${t("btn_rerun_recap")}</button>`;
        }
        actions += ` <button type="button" onclick="deleteHistoryItem('${j.id}')" class="history-dl-link">${t("btn_delete_history")}</button>`;
    }
    if (actions) inner += `<div class="history-btn-row hub-detail-actions">${actions}</div>`;
    return inner;
}

// Batch L history actions: re-run prefill + owner delete. Both are
// thin clients — the server re-checks ownership on delete, and the
// prefill only fills the form (never auto-submits).
window.rerunRecapFromHistory = function(jobId) {
    const item = (hubHistoryCache || []).find(h => h.id === jobId);
    if (!item || !item.recap_config) {
        showStudioToast(t("err_recap_failed"), 'error');
        return;
    }
    haptic("light");
    switchTab("tab-recap");
    if (typeof window.prefillRecapFromHistory === "function") {
        window.prefillRecapFromHistory(item.recap_config, item);
    }
    showStudioToast(t("recap_rerun_prefilled_toast"), 'info', 5000);
};

window.deleteHistoryItem = function(jobId) {
    showStudioConfirm(t("history_delete_confirm"), async () => {
        try {
            const res = await fetch(`/api/history/${jobId}`, {
                method: "DELETE",
                headers: getAuthHeaders(),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || t("err_generic"));
            if (Array.isArray(hubHistoryCache)) {
                hubHistoryCache = hubHistoryCache.filter(h => h.id !== jobId);
            }
            if (hubOpenDetailId === jobId) hubOpenDetailId = null;
            renderHistoryHub();
            showStudioToast(t("history_deleted_toast"), 'info');
        } catch (err) {
            showStudioToast((err && err.message) ? err.message : t("err_recap_failed"), 'error');
        }
    });
};

function hubStatusBadgeHtml(j) {
    const st = String(j.status || "").toUpperCase();
    const stCls = `status-${(j.status || "").toLowerCase()}`;
    if (st === "SUCCESS") return `<span class="vcp-badge vcp-badge-success ${stCls}">✅ ${escapeHtml(t("status_badge_success"))}</span>`;
    if (st === "RUNNING") return `<span class="vcp-badge vcp-badge-info ${stCls}">⏳ ${escapeHtml(t("status_badge_running"))}</span>`;
    if (st === "QUEUED") return `<span class="vcp-badge vcp-badge-warning ${stCls}">🕒 ${escapeHtml(t("status_badge_queued"))}</span>`;
    if (st === "FAILED") return `<span class="vcp-badge vcp-badge-danger ${stCls}">❌ ${escapeHtml(t("status_badge_failed"))}</span>`;
    if (st === "CANCELLED") return `<span class="vcp-badge vcp-badge-warning ${stCls}">🚫 ${escapeHtml(t("hist_status_cancelled"))}</span>`;
    return `<span class="vcp-badge vcp-badge-info ${stCls}">${escapeHtml(j.status || t("status_badge_other"))}</span>`;
}

function hubHistoryRowHtml(j) {
    const isRecap = j.job_type === "RECAP";
    // Batch BB-3: TTS jobs get their own label — they used to render
    // as ordinary voice jobs (🎙️, no type tag) and were only
    // recognisable by voice name.
    const isTts = j.job_type === "TTS";
    const open = hubOpenDetailId === j.id;
    const snippet = j.input_snippet || (isRecap ? t("home_recent_fallback_recap") : isTts ? t("home_recent_fallback_tts") : t("home_recent_fallback_voice"));
    const title = isRecap ? escapeHtml(snippet) : `&quot;${escapeHtml(snippet)}&quot;`;
    const durText = formatJobDuration(j);
    const exp = computeHistoryExpiryState(j);
    const expiryPillHtml = (typeof computeRetentionBadgeHtml === "function")
        ? computeRetentionBadgeHtml(j)
        : (exp.expiresSoon && !exp.expired && j.status === "SUCCESS"
            ? `<span class="vcp-badge vcp-badge-warning hub-expires-soon-badge">${escapeHtml(tf("hist_expires_soon_badge", { time: exp.timeText }))}</span>`
            : "");
    const isRunningOrQueued = j.status === "RUNNING" || j.status === "QUEUED";
    const pct = Math.min(100, Math.max(8, Number(j.progress_percent) || (j.status === "RUNNING" ? 55 : 22)));
    const progressHtml = isRunningOrQueued
        ? `<span class="vcp-job-progress"><span class="vcp-job-progress-fill" style="width:${pct}%"></span></span>`
        : "";
    let quickAction = "";
    if (isRecap && j.script_ready) {
        quickAction = `<button type="button" class="vcp-chip active" onclick="openRecapScriptEditor('${j.id}')">${escapeHtml(t("btn_review_script"))}</button>`;
    } else if (isRunningOrQueued) {
        quickAction = isRecap
            ? `<button type="button" class="vcp-chip active" onclick="openRecapDetail('${j.id}')">${escapeHtml(t("hist_track_recap"))}</button>`
            : `<button type="button" class="vcp-chip active" onclick="openVoiceDetail('${j.id}')">${escapeHtml(t("hist_track_voice"))}</button>`;
    } else if (j.status === "SUCCESS" && !j.is_expired) {
        if (isRecap && j.deliverables && j.deliverables.video) {
            quickAction = `<a href="#" data-dl-url="${j.deliverables.video}" data-dl-name="${brandedDlName("Recap", "mp4", j.input_snippet, j.id)}" class="vcp-chip active">${escapeHtml(t("home_recent_action_dl"))}</a>`;
        } else if (!isRecap && j.download_url) {
            quickAction = `<a href="#" data-dl-url="${j.download_url}" data-dl-name="${brandedDlName("Voice", "wav", null, j.id)}" class="vcp-chip active">${escapeHtml(t("home_recent_action_dl"))}</a>`;
        }
    } else if (j.status === "FAILED" && isRecap) {
        if (j.can_retry === false) {
            quickAction = `<button type="button" class="vcp-chip is-disabled" disabled title="${escapeHtml(t("recap_retry_upload_missing"))}">${escapeHtml(t("btn_retry_job"))}</button>`;
        } else {
            quickAction = `<button type="button" class="vcp-chip" onclick="retryRecapJob('${j.id}')">${escapeHtml(t("btn_retry_job"))}</button>`;
        }
    }
    return `
    <div class="hub-row vcp-card vcp-card--compact vcp-job-card ${hubStatusBorder(j.status)} ${exp.expiresSoon ? "is-expires-soon" : ""} ${open ? "open" : ""}" data-job-id="${j.id}">
        <button type="button" class="hub-row-main" onclick="toggleHistoryDetail('${j.id}')" aria-expanded="${open}">
            <span class="vcp-job-head">
                <span class="hub-type-icon ${isRecap ? "recap" : isTts ? "tts" : "voice"}">${isRecap ? "🎬" : isTts ? "🎧" : "🎙️"}</span>
                <span class="hub-row-text">
                    <span class="hub-row-title">${title}</span>
                    <span class="hub-row-meta">${isRecap ? escapeHtml(t("hist_type_recap")) + " • " : isTts ? escapeHtml(t("job_badge_tts")) + " • " : ""}${escapeHtml(j.voice_display || j.preset_voice || "")}${durText ? ` <span class="hub-duration-pill">⏱️ ${escapeHtml(durText)}</span>` : ""} <span class="hub-time-ago">🕒 ${escapeHtml(j.time_ago || "")}</span><span class="hub-live-meta"></span></span>
                    ${(j.status === "FAILED" && j.error_message) ? `<span class="hub-row-error text-danger text-xs">${escapeHtml(displayJobError(j.error_message))}</span>` : ""}
                </span>
                <span class="hub-chevron">${open ? "⌃" : "›"}</span>
            </span>
            <span class="hub-row-badges vcp-job-badges">
                ${hubStatusBadgeHtml(j)}
                ${expiryPillHtml}
                ${j.fallback_used ? `<span class="history-item-badge hub-fallback-badge">${t("hist_fallback_badge")}</span>` : ""}
                ${!isRecap && j.reference_trimmed ? `<span class="history-item-badge hub-fallback-badge">${t("hist_ref_trim_badge")}</span>` : ""}
            </span>
            ${progressHtml}
        </button>
        ${quickAction ? `<div class="vcp-job-footer-row hub-quick-footer">${quickAction}</div>` : ""}
        <div class="hub-detail ${open ? "" : "hidden"}">${hubHistoryDetailHtml(j)}</div>
    </div>`;
}

function renderHistoryHub() {
    renderHomeRecentProjects();
    const container = document.getElementById("hub-history-list");
    const stage = document.getElementById("hub-history-stage");
    if (!container) return;

    if (hubHistoryCache === null && !hubHistoryError) {
        if (stage && typeof setSectionState === "function") {
            setSectionState(stage, "loading");
            container.innerHTML = "";
        } else {
            container.innerHTML = `<div class="hub-state"><p class="text-muted">${t("history_loading")}</p></div>`;
        }
        return;
    }
    if (hubHistoryError) {
        if (stage && typeof setSectionState === "function") {
            setSectionState(stage, "error", { onRetry: loadUserHistory });
            container.innerHTML = "";
        } else {
            container.innerHTML = `
                <div class="hub-state">
                    <div class="hub-state-emoji">⚠️</div>
                    <p class="hub-state-text" data-i18n="history_error">${t("history_error")}</p>
                    <button type="button" class="mini-chip" onclick="loadUserHistory()" data-i18n="history_retry">${t("history_retry")}</button>
                </div>`;
        }
        return;
    }

    const all = hubHistoryCache || [];
    if (all.length === 0) {
        if (stage && typeof setSectionState === "function") {
            setSectionState(stage, "empty");
            container.innerHTML = "";
        } else {
            container.innerHTML = `
                <div class="hub-state">
                    <div class="hub-state-emoji">📜</div>
                    <p class="hub-state-text" data-i18n="history_empty">${t("history_empty")}</p>
                    <p class="text-muted hub-state-sub">${t("history_empty_hint")}</p>
                </div>`;
        }
        return;
    }

    if (stage && typeof setSectionState === "function") {
        setSectionState(stage, "ready");
    }

    const jobs = hubHistoryFilter === "all" ? all : all.filter(h => h.job_type === hubHistoryFilter);
    if (jobs.length === 0) {
        container.innerHTML = `
            <div class="hub-state vcp-card vcp-card--compact">
                <div class="hub-state-emoji">🔍</div>
                <p class="hub-state-text">${t("history_filter_empty")}</p>
                <button type="button" class="vcp-btn vcp-btn--glass vcp-btn--sm" onclick="setHubHistoryFilter('all')">${t("history_show_all")}</button>
            </div>`;
        return;
    }

    container.innerHTML = jobs.map(hubHistoryRowHtml).join("");
    // A re-render can restore an open detail (hubOpenDetailId) — wire
    // its player too, not just fresh toggle clicks.
    const openRow = container.querySelector(".hub-row.open");
    if (openRow) wireHubDetailAudio(openRow);
}

async function loadUserHistory() {
    const container = document.getElementById("hub-history-list");
    if (container && hubHistoryCache === null && !hubHistoryError) {
        renderHistoryHub(); // loading state
    }
    try {
        const res = await fetchWithTimeout("/api/history", {
            headers: getAuthHeaders(),
        });
        if (res.status === 403) { hubHistoryCache = []; hubHistoryError = false; renderHistoryHub(); return; }
        if (!res.ok) throw new Error(`history ${res.status}`);

        const data = await res.json();
        const history = data.history || [];
        // Unified merge: the endpoint already returns both job types; sort
        // newest-first client-side by created_at so the hub order never
        // depends on server grouping.
        history.sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
        hubHistoryCache = history;
        window.__historyLoadedOnce = true;
        hubHistoryError = false;
        renderHistoryHub();
    } catch (e) {
        console.warn("Could not load user history:", e);
        hubHistoryError = true;
        renderHistoryHub();
    }
}

async function openRecapInPlayer(recapJobId) {
    if (!recapJobId) return;
    haptic("light");
    switchTab("tab-recap");
    if (typeof showRecapStep === "function") showRecapStep(4);
    try {
        const res = await fetch(`/api/recap/${recapJobId}/status`, { headers: getAuthHeaders() });
        if (!res.ok) return;
        const data = await res.json();
        if (data.status === "SUCCESS") {
            state.recapJobId = recapJobId;
            const resultBox = document.getElementById("recap-result-box");
            const player = document.getElementById("recap-result-player");
            const btnVideo = document.getElementById("btn-recap-dl-video");
            const btnAudio = document.getElementById("btn-recap-dl-audio");
            const btnSrt = document.getElementById("btn-recap-dl-srt");
            const btnScript = document.getElementById("btn-recap-dl-script");
            const btnTg = document.getElementById("btn-recap-send-telegram");

            if (resultBox) {
                resultBox.classList.remove("hidden");
                refreshStickyCtas();
                resultBox.scrollIntoView({ behavior: "smooth" });
            }
            if (player && data.download_urls?.video) {
                setRecapPlayerSource(player, data.download_urls).then((ok) => {
                    if (ok) player.play().catch(() => {});
                });
                player.classList.remove("hidden");
            }
            if (btnVideo && data.download_urls?.video) {
                btnVideo.setAttribute("data-dl-url", data.download_urls.video);
                btnVideo.setAttribute("data-dl-name", brandedDlName("Recap", "mp4", null, recapJobId));
            }
            if (btnAudio && data.download_urls?.audio) {
                btnAudio.setAttribute("data-dl-url", data.download_urls.audio);
                btnAudio.setAttribute("data-dl-name", brandedDlName("RecapAudio", "mp3", null, recapJobId));
            }
            if (btnSrt && data.download_urls?.srt) {
                btnSrt.setAttribute("data-dl-url", data.download_urls.srt);
                btnSrt.setAttribute("data-dl-name", brandedDlName("Subtitles", "srt", null, recapJobId));
            }
            if (btnScript && data.download_urls?.script) {
                btnScript.setAttribute("data-dl-url", data.download_urls.script);
                btnScript.setAttribute("data-dl-name", brandedDlName("Script", "txt", null, recapJobId));
            }
            syncRecapAiThumbnailUI(data, recapJobId);
            if (btnTg) {
                btnTg.disabled = false;
                btnTg.classList.remove("sent");
                btnTg.innerHTML = `<span class="btn-icon">📱</span> <span>${t("btn_send_recap_telegram")}</span>`;
            }
            applyTelegramSendable("recap", data.telegram_sendable);
        } else if (data.status === "RUNNING" || data.status === "QUEUED") {
            state.recapJobId = recapJobId;
            startRecapJobPolling(recapJobId);
        }
    } catch (e) {
        console.warn("Could not open recap in player:", e);
    }
}
window.openRecapInPlayer = openRecapInPlayer;

function syncRecapAiThumbnailUI(statusData, jobId) {
    const inputEl = document.getElementById("recap-thumb-headline-input");
    const btnGen = document.getElementById("btn-recap-ai-thumbnail");
    const btnDl = document.getElementById("btn-recap-dl-thumbnail");
    const noteEl = document.getElementById("recap-thumb-status-note");
    const previewImg = document.getElementById("recap-thumb-preview-img");

    const canUse = !!(state.canUseAiThumbnail || state.user?.is_vip || state.user?.is_admin || state.user?.account_category === "PURCHASED");
    const capReached = !!state.aiThumbnailCapReached;

    if (inputEl && statusData) {
        const defaultTitle = statusData.title || document.getElementById("recap-video-title")?.value?.trim() || "";
        if (!inputEl.dataset.userEdited || inputEl.dataset.forJobId !== String(jobId || "")) {
            inputEl.value = defaultTitle;
            inputEl.dataset.forJobId = String(jobId || "");
            delete inputEl.dataset.userEdited;
        }
        if (!inputEl.dataset.boundEditListener) {
            inputEl.addEventListener("input", () => {
                inputEl.dataset.userEdited = "1";
            });
            inputEl.dataset.boundEditListener = "1";
        }
    }
    if (inputEl) {
        inputEl.disabled = !canUse || capReached;
    }
    if (btnGen) {
        btnGen.disabled = capReached;
        btnGen.innerHTML = `<span data-i18n="btn_recap_ai_thumbnail">${escapeHtml(t("btn_recap_ai_thumbnail"))}</span>`;
    }
    if (noteEl) {
        if (!canUse) {
            noteEl.textContent = t("recap_thumb_vip_locked_note");
        } else if (capReached) {
            noteEl.textContent = t("recap_thumb_cap_reached");
        } else {
            noteEl.textContent = t("recap_thumb_free_perk_note");
        }
    }
    if (statusData) {
        const thumbDlUrl = statusData.download_urls?.thumbnail;
        const thumbStreamUrl = statusData.download_urls?.thumbnail_stream_url || thumbDlUrl;
        if (btnDl) {
            if (thumbDlUrl) {
                btnDl.setAttribute("data-dl-url", thumbDlUrl);
                btnDl.setAttribute("data-dl-name", brandedDlName("Thumbnail", "jpg", statusData.title || null, jobId || state.recapJobId));
                btnDl.classList.remove("hidden");
            } else {
                btnDl.classList.add("hidden");
            }
        }
        if (previewImg) {
            if (thumbStreamUrl) {
                previewImg.src = thumbStreamUrl;
                previewImg.classList.remove("hidden");
            } else {
                previewImg.removeAttribute("src");
                previewImg.classList.add("hidden");
            }
        }
    }
}
window.syncRecapAiThumbnailUI = syncRecapAiThumbnailUI;

async function generateRecapAiThumbnail(jobIdArg, headlineInputId) {
    haptic("medium");
    const canUse = !!(state.canUseAiThumbnail || state.user?.is_vip || state.user?.is_admin || state.user?.account_category === "PURCHASED");
    if (!canUse) {
        showStudioToast(t("recap_thumb_vip_locked_note"), "info", 5000);
        const noteEl = document.getElementById("recap-thumb-status-note");
        if (noteEl) noteEl.textContent = t("recap_thumb_vip_locked_note");
        return;
    }
    if (state.aiThumbnailCapReached) {
        showStudioToast(t("recap_thumb_cap_reached"), "warning", 5000);
        syncRecapAiThumbnailUI();
        return;
    }

    const targetJobId = jobIdArg || state.recapJobId;
    if (!targetJobId) return;

    const inputEl = document.getElementById(headlineInputId || "recap-thumb-headline-input");
    const headlineText = inputEl ? String(inputEl.value ?? "") : "";
    const btnEl = jobIdArg
        ? document.getElementById(`btn-hist-thumb-${jobIdArg}`)
        : document.getElementById("btn-recap-ai-thumbnail");

    if (btnEl) {
        btnEl.disabled = true;
        btnEl.textContent = t("btn_recap_ai_thumbnail_generating");
    }

    try {
        const res = await fetch(`/api/recap/${targetJobId}/thumbnail`, {
            method: "POST",
            headers: {
                ...getAuthHeaders(),
                "Content-Type": "application/json",
            },
            body: JSON.stringify({ headline_text: headlineText }),
        });
        const data = await res.json().catch(() => ({}));
        if (res.status === 403) {
            showStudioToast(t("recap_thumb_vip_locked_note"), "info", 5000);
            return;
        }
        if (res.status === 429) {
            state.aiThumbnailCapReached = true;
            syncRecapAiThumbnailUI();
            if (typeof renderHistoryHub === "function" && Array.isArray(hubHistoryCache)) {
                renderHistoryHub();
            }
            showStudioToast(t("recap_thumb_cap_reached"), "warning", 6000);
            return;
        }
        if (!res.ok) {
            throw new Error(data.detail || t("recap_thumb_failed"));
        }

        if (data.pool_usage && data.pool_usage.cap_reached) {
            state.aiThumbnailCapReached = true;
        }
        if (inputEl && !headlineText && data.headline_text) {
            inputEl.value = data.headline_text;
        }
        if (!jobIdArg || targetJobId === state.recapJobId) {
            syncRecapAiThumbnailUI(
                {
                    title: data.headline_text || headlineText,
                    download_urls: {
                        thumbnail: data.thumbnail_url,
                        thumbnail_stream_url: data.thumbnail_stream_url || data.thumbnail_url,
                    },
                },
                targetJobId
            );
        }
        if (Array.isArray(hubHistoryCache)) {
            const item = hubHistoryCache.find(h => h.id === targetJobId);
            if (item) {
                item.deliverables = item.deliverables || {};
                item.deliverables.thumbnail = data.thumbnail_url;
                renderHistoryHub();
            }
        }
        showStudioToast(t("recap_thumb_ready_toast"), "success", 5000);
    } catch (err) {
        showStudioToast(t("recap_thumb_failed"), "error", 5000);
    } finally {
        if (btnEl && !state.aiThumbnailCapReached) {
            btnEl.disabled = false;
            btnEl.textContent = t("btn_recap_ai_thumbnail");
        }
    }
}
window.generateRecapAiThumbnail = generateRecapAiThumbnail;

// ── Deep Link Handling (Telegram WebApp & URL Query) ──────────────────────
async function checkStartupDeepLinks() {
    try {
        const urlParams = new URLSearchParams(window.location.search);
        const tabParam = urlParams.get("tab");
        let jobId = urlParams.get("job_id");
        let recapJobId = urlParams.get("recap_job_id");

        if (tabParam) {
            switchTab(tabParam);
        }

        if (state.tg?.initDataUnsafe?.start_param) {
            const sp = state.tg.initDataUnsafe.start_param;
            if (sp.startsWith("job_")) jobId = sp.replace("job_", "");
            if (sp.startsWith("recap_")) recapJobId = sp.replace("recap_", "");
        }

        if (!recapJobId) {
            try {
                const savedRecap = localStorage.getItem("vcp_active_recap_job_id");
                if (savedRecap) recapJobId = savedRecap;
            } catch (e) {}
        }

        if (jobId) {
            switchTab("tab-voice");
            const res = await fetch(`/api/jobs/${jobId}/status`, { headers: getAuthHeaders() });
            if (res.ok) {
                const data = await res.json();
                if (data.status === "SUCCESS") {
                    if (data.fallback_used) { try { showStudioToast(t("voice_fallback_notice"), 'info'); } catch (e) {} }
                    if (data.reference_trimmed) { try { showStudioToast(t("voice_ref_trim_notice"), 'info'); } catch (e) {} }
                    renderVoiceFallbackNote(data);
                    state.activeJobId = jobId;
                    const resultBox = document.getElementById("voice-result-box");
                    const audioPlayer = document.getElementById("result-audio-player");
                    const btnDl = document.getElementById("btn-download-audio");
                    const btnTg = document.getElementById("btn-voice-send-telegram");

                    if (resultBox) {
                        resultBox.classList.remove("hidden");
                        resultBox.scrollIntoView({ behavior: "smooth" });
                    }
                    if (audioPlayer && data.output_audio_url) {
                        setAuthMediaSource(audioPlayer, data.output_audio_url).then((ok) => {
                            if (ok) { audioPlayer.load(); audioPlayer.play().catch(() => {}); }
                        });
                    }
                    if (btnDl && data.output_audio_url) {
                        btnDl.setAttribute("data-dl-url", data.output_audio_url);
                        btnDl.setAttribute("data-dl-name", brandedDlName("Voice", "wav", null, jobId));
                    }
                    if (btnTg) {
                        btnTg.disabled = false;
                        btnTg.classList.remove("sent");
                        btnTg.innerHTML = `<span class="btn-icon">📱</span> <span>${t("btn_send_voice_telegram")}</span>`;
                    }
                    applyTelegramSendable("voice", data.telegram_sendable);
                    showFloatingAlert(
                        t("alert_voice_ready"),
                        t("alert_voice_ready_body2"),
                        () => {
                            if (resultBox) resultBox.scrollIntoView({ behavior: "smooth" });
                        },
                        t("alert_btn_listen")
                    );
                } else if (data.status === "RUNNING" || data.status === "QUEUED") {
                    const progressBox = document.getElementById("voice-progress-box");
                    if (progressBox) progressBox.classList.remove("hidden");
                    startJobPolling(jobId);
                }
            }
        } else if (recapJobId) {
            if (urlParams.get("recap_job_id") || tabParam === "tab-recap") {
                switchTab("tab-recap");
            }
            const res = await fetch(`/api/recap/${recapJobId}/status`, { headers: getAuthHeaders() });
            if (res.ok) {
                const data = await res.json();
                if (data.status === "SUCCESS") {
                    try { localStorage.removeItem("vcp_active_recap_job_id"); } catch (e) {}
                    state.recapJobId = recapJobId;
                    const resultBox = document.getElementById("recap-result-box");
                    const player = document.getElementById("recap-result-player");
                    const btnVideo = document.getElementById("btn-recap-dl-video");
                    const btnAudio = document.getElementById("btn-recap-dl-audio");
                    const btnSrt = document.getElementById("btn-recap-dl-srt");
                    const btnScript = document.getElementById("btn-recap-dl-script");
                    const btnTg = document.getElementById("btn-recap-send-telegram");

                    if (resultBox) {
                        resultBox.classList.remove("hidden");
                        if (urlParams.get("recap_job_id")) {
                            resultBox.scrollIntoView({ behavior: "smooth" });
                        }
                    }
                    if (player && data.download_urls?.video) {
                        setRecapPlayerSource(player, data.download_urls);
                        player.classList.remove("hidden");
                    }
                    if (btnVideo && data.download_urls?.video) {
                        btnVideo.setAttribute("data-dl-url", data.download_urls.video);
                        btnVideo.setAttribute("data-dl-name", brandedDlName("Recap", "mp4", null, recapJobId));
                    }
                    if (btnAudio && data.download_urls?.audio) {
                        btnAudio.setAttribute("data-dl-url", data.download_urls.audio);
                        btnAudio.setAttribute("data-dl-name", brandedDlName("RecapAudio", "mp3", null, recapJobId));
                    }
                    if (btnSrt && data.download_urls?.srt) {
                        btnSrt.setAttribute("data-dl-url", data.download_urls.srt);
                        btnSrt.setAttribute("data-dl-name", brandedDlName("Subtitles", "srt", null, recapJobId));
                    }
                    if (btnScript && data.download_urls?.script) {
                        btnScript.setAttribute("data-dl-url", data.download_urls.script);
                        btnScript.setAttribute("data-dl-name", brandedDlName("Script", "txt", null, recapJobId));
                    }
                    syncRecapAiThumbnailUI(data, recapJobId);
                    if (btnTg) {
                        btnTg.disabled = false;
                        btnTg.classList.remove("sent");
                        btnTg.innerHTML = `<span class="btn-icon">📱</span> <span>${t("btn_send_recap_telegram")}</span>`;
                    }
                    applyTelegramSendable("recap", data.telegram_sendable);
                    // Cold-start restore: this alert is the ONLY
                    // completion signal here (the user may be on any
                    // tab when a previous session's job is restored),
                    // so it stays — unlike the live poller's duplicate
                    // (round-4 F-E), where the user is already watching
                    // the result card appear.
                    showFloatingAlert(
                        t("alert_recap_ready"),
                        t("alert_recap_ready_body"),
                        () => {
                            switchTab("tab-recap");
                            if (resultBox) resultBox.scrollIntoView({ behavior: "smooth" });
                        },
                        t("alert_btn_watch")
                    );
                } else if (data.status === "RUNNING" || data.status === "QUEUED" || data.status === "WAITING_CHOICE") {
                    state.recapJobId = recapJobId;
                    startRecapJobPolling(recapJobId);
                } else if (data.status === "SCRIPT_READY") {
                    state.recapJobId = recapJobId;
                    state.recapScriptReadyJobId = recapJobId;
                    openRecapScriptEditor(recapJobId);
                } else if (data.status === "FAILED" || data.status === "CANCELLED") {
                    try { localStorage.removeItem("vcp_active_recap_job_id"); } catch (e) {}
                }
            }
        }
    } catch (e) {
        console.warn("Deep link handling error:", e);
    }
}

// ── Deliverable Downloads (Telegram Mini App & Mobile Browser Compatible) ──
function downloadDeliverable(url, defaultFilename) {
    if (!url || url === "#") return;
    const absUrl = url.startsWith("http") ? url : (window.location.origin + url);

    // If running in Telegram WebApp
    if (window.Telegram?.WebApp?.openLink) {
        try {
            window.Telegram.WebApp.openLink(absUrl);
            return;
        } catch (e) {
            console.warn("Telegram openLink fallback:", e);
        }
    }

    try {
        const link = document.createElement("a");
        link.href = absUrl;
        link.download = defaultFilename || "file";
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    } catch (e) {
        window.open(absUrl, "_blank");
    }
}

// NOTE (audit fix 2026-11-03): the four recap result-screen download
// anchors (btn-recap-dl-video/audio/srt/script) used to ALSO get
// element-level click listeners here (setupDeliverablesDownloads)
// that fired downloadDeliverable()/Telegram openLink on the very same
// tap the delegated document handler (a[data-dl-url] →
// downloadAuthFile) already handled — one tap, two downloads with two
// different code paths. The delegated handler is now the single owner
// of these buttons, exactly like every other download anchor in the
// app; the render sites set both data-dl-url (tokenized server-side)
// and data-dl-name (brandedDlName) for them. downloadDeliverable()
// above remains available for genuine non-anchor callers.

// ── Alert Boxes Auto-Sliding Carousel ───────────────────────────────────────
function setupAlertCarousel() {
    const carousel = document.getElementById("studio-alert-carousel");
    if (!carousel) return;
    const slides = carousel.querySelectorAll(".carousel-slide");
    const dots = carousel.querySelectorAll(".carousel-dot");
    const btnPrev = document.getElementById("btn-carousel-prev");
    const btnNext = document.getElementById("btn-carousel-next");
    if (!slides.length) return;

    let currentIndex = 0;
    let timer = null;
    let isPaused = false;

    function goToSlide(idx, direction = "next") {
        slides.forEach((s, i) => {
            s.classList.remove("active", "slide-out-left");
            if (i === currentIndex && direction === "next") {
                s.classList.add("slide-out-left");
            }
        });
        dots.forEach(d => d.classList.remove("active"));

        currentIndex = (idx + slides.length) % slides.length;
        slides[currentIndex].classList.add("active");
        if (dots[currentIndex]) dots[currentIndex].classList.add("active");
    }

    function nextSlide() {
        goToSlide(currentIndex + 1, "next");
    }

    function prevSlide() {
        goToSlide(currentIndex - 1, "prev");
    }

    function startAutoSlide() {
        if (timer) clearInterval(timer);
        timer = setInterval(() => {
            if (!isPaused && !document.hidden) nextSlide();
        }, 3500); // 3.5 seconds auto rotation (paused while the tab is hidden)
    }

    if (btnNext) {
        btnNext.addEventListener("click", () => {
            haptic("selection");
            nextSlide();
            startAutoSlide();
        });
    }

    if (btnPrev) {
        btnPrev.addEventListener("click", () => {
            haptic("selection");
            prevSlide();
            startAutoSlide();
        });
    }

    dots.forEach((dot, idx) => {
        dot.addEventListener("click", () => {
            haptic("selection");
            goToSlide(idx);
            startAutoSlide();
        });
    });

    // Touch Swipe Gestures for Mobile
    let touchStartX = 0;
    let touchEndX = 0;

    carousel.addEventListener("touchstart", (e) => {
        isPaused = true;
        if (e.touches && e.touches.length > 0) {
            touchStartX = e.touches[0].clientX;
        }
    }, { passive: true });

    carousel.addEventListener("touchend", (e) => {
        if (e.changedTouches && e.changedTouches.length > 0) {
            touchEndX = e.changedTouches[0].clientX;
            const diffX = touchEndX - touchStartX;
            if (diffX > 40) {
                haptic("selection");
                prevSlide();
            } else if (diffX < -40) {
                haptic("selection");
                nextSlide();
            }
        }
        setTimeout(() => { isPaused = false; }, 2000);
    }, { passive: true });

    carousel.addEventListener("mouseenter", () => { isPaused = true; });
    carousel.addEventListener("mouseleave", () => { isPaused = false; });

    startAutoSlide();
}

// ── App Startup ────────────────────────────────────────────────────────────
// CQ: on Telegram Desktop the OS file picker never opens from a tap in
// the webview (CL/CP), while drag-and-drop works. Show the drag hint in
// the Recap drop zone only on that platform; hidden everywhere else.
function applyDesktopDropHint() {
    try {
        var platform = (window.Telegram && Telegram.WebApp && Telegram.WebApp.platform) || "";
        if (platform !== "tdesktop") return;
        var hint = document.getElementById("recap-drop-desktop-hint");
        if (hint) hint.hidden = false;
    } catch (e) { /* hint stays hidden */ }
}

// ── Phase 2: Home Dashboard, Status Strip, Recent Projects & Help Hub ──────
function paintHomeStatusStrip(data) {
    if (!data) return;
    const tier = data.is_admin ? "admin" : (data.is_vip ? "vip" : "free");
    const homePane = document.getElementById("tab-home");
    if (homePane) homePane.setAttribute("data-tier", tier);
    const heroCard = document.getElementById("home-hero-card");
    if (heroCard) heroCard.setAttribute("data-tier", tier);
    const greetEl = document.getElementById("home-greeting-text");
    if (greetEl) {
        const tgFirst = state.tg && state.tg.initDataUnsafe && state.tg.initDataUnsafe.user && state.tg.initDataUnsafe.user.first_name;
        const displayName = data.first_name || data.username || tgFirst || "";
        greetEl.textContent = displayName
            ? tf("home_greeting_named", { name: displayName })
            : t("home_greeting_default");
    }
    const tierEl = document.getElementById("home-tier-badge");
    if (tierEl) {
        if (data.is_admin) {
            tierEl.className = "vcp-badge vcp-badge-gold";
            tierEl.textContent = t("tier_badge_admin");
        } else if (data.is_vip) {
            tierEl.className = "vcp-badge vcp-badge-gold";
            tierEl.textContent = t("badge_vip");
        } else {
            tierEl.className = "vcp-badge vcp-badge-info";
            tierEl.textContent = t("vip_free");
        }
    }
    const sumEl = document.getElementById("home-bal-summary");
    if (sumEl) {
        const mUnit = t("unit_min_short");
        const cUnit = t("unit_credit_short");
        const vPart = data.is_admin ? "🎙️ ∞" : `🎙️ ${data.balance_minutes ?? 0}${mUnit}`;
        const rPart = data.is_admin ? "🎬 ∞" : `🎬 ${data.recap_credits ?? 0}${cUnit}`;
        let tPart = "🎧 —";
        const rem = data.tts_today_remaining_seconds;
        if (data.is_admin || data.tts_daily_limit_seconds === 0) tPart = "🎧 ∞";
        else if (typeof rem === "number") tPart = `🎧 ${(rem / 60).toFixed(1)}${mUnit}`;
        else if (typeof data.tts_balance_seconds === "number") tPart = `🎧 ${(data.tts_balance_seconds / 60).toFixed(1)}${mUnit}`;
        sumEl.textContent = `${vPart} · ${rPart} · ${tPart}`;
    }
}
window.paintHomeStatusStrip = paintHomeStatusStrip;

function paintHomeStatusStripFromQuota(q) {
    if (!q || !state.user) return;
    const sumEl = document.getElementById("home-bal-summary");
    if (!sumEl) return;
    const u = state.user;
    const mUnit = t("unit_min_short");
    const cUnit = t("unit_credit_short");
    const vPart = u.is_admin ? "🎙️ ∞" : `🎙️ ${u.balance_minutes ?? 0}${mUnit}`;
    const rPart = u.is_admin ? "🎬 ∞" : `🎬 ${u.recap_credits ?? 0}${cUnit}`;
    let tPart = "🎧 —";
    if (q.is_admin || q.unlimited) tPart = "🎧 ∞";
    else {
        const total = (q.referral_seconds || 0) + (q.remaining_seconds || 0);
        tPart = `🎧 ${(total / 60).toFixed(1)}${mUnit}`;
    }
    sumEl.textContent = `${vPart} · ${rPart} · ${tPart}`;
}

// Plays the pre-generated static sample audio (/api/voices/sample/komin)
// via the existing #sample-btn-voice / #sample-audio-voice element.
// Never calls any generation API or consumes user quota.
function playWelcomeTrySample() {
    haptic("light");
    const sampleBtn = document.getElementById("sample-btn-voice");
    const audio = document.getElementById("sample-audio-voice");
    const tryBtn = document.getElementById("welcome-try-sample-btn");
    if (sampleBtn) {
        sampleBtn.click();
    } else if (audio) {
        if (audio.paused) audio.play().catch(() => {});
        else audio.pause();
    }
    if (audio && tryBtn && !audio.dataset.welcomeTryWired) {
        audio.dataset.welcomeTryWired = "1";
        const syncTryLabel = () => {
            const playing = !audio.paused && !audio.ended;
            tryBtn.textContent = t(playing ? "welcome_try_sample_playing" : "welcome_try_sample_btn");
        };
        audio.addEventListener("play", syncTryLabel);
        audio.addEventListener("pause", syncTryLabel);
        audio.addEventListener("ended", syncTryLabel);
    }
}
window.playWelcomeTrySample = playWelcomeTrySample;

function computeRetentionBadgeHtml(j) {
    if (j.status !== "SUCCESS") return "";
    const exp = computeHistoryExpiryState(j);
    if (exp.expired) {
        return `<span class="vcp-badge vcp-badge-danger">${escapeHtml(t("hist_expired"))}</span>`;
    }
    if (exp.hoursLeft == null) return "";
    if (exp.expiresSoon) {
        return `<span class="vcp-badge vcp-badge-warning hub-expires-soon-badge">${escapeHtml(tf("hist_expires_soon_badge", { time: exp.timeText }))}</span>`;
    }
    const cls = exp.hoursLeft <= 4 ? "vcp-badge-warning" : "vcp-badge-info";
    return `<span class="vcp-badge ${cls}">${escapeHtml(tf("home_recent_time_left_hours", { hours: exp.hoursLeft }))}</span>`;
}

function computeHomeRecentStatusBadgeHtml(j) {
    const st = String(j.status || "").toUpperCase();
    if (st === "SUCCESS") return `<span class="vcp-badge vcp-badge-success">✅ ${escapeHtml(t("status_badge_success"))}</span>`;
    if (st === "RUNNING") return `<span class="vcp-badge vcp-badge-info">⏳ ${escapeHtml(t("status_badge_running"))}</span>`;
    if (st === "QUEUED") return `<span class="vcp-badge vcp-badge-warning">🕒 ${escapeHtml(t("status_badge_queued"))}</span>`;
    if (st === "FAILED") return `<span class="vcp-badge vcp-badge-danger">❌ ${escapeHtml(t("status_badge_failed"))}</span>`;
    if (st === "CANCELLED") return `<span class="vcp-badge vcp-badge-warning">🚫 ${escapeHtml(t("hist_status_cancelled"))}</span>`;
    return `<span class="vcp-badge vcp-badge-info">ℹ️ ${escapeHtml(t("status_badge_other"))}</span>`;
}

function openRecentProjectAction(jobId, mode) {
    haptic("light");
    if (mode === "review") {
        switchTab("tab-recap");
        if (typeof showRecapStep === "function") showRecapStep(4);
        if (typeof openRecapScriptEditor === "function") openRecapScriptEditor(jobId);
        return;
    }
    if (mode === "recap") {
        switchTab("tab-recap");
        if (typeof showRecapStep === "function") showRecapStep(4);
        if (typeof openRecapDetail === "function") openRecapDetail(jobId);
        return;
    }
    if (mode === "tts") {
        switchTab("tab-tts");
        return;
    }
    if (mode === "voice") {
        switchTab("tab-voice");
        if (typeof openVoiceDetail === "function") openVoiceDetail(jobId);
        return;
    }
    if (mode === "retry") {
        switchTab("tab-recap");
        if (typeof retryRecapJob === "function") retryRecapJob(jobId);
    }
}
window.openRecentProjectAction = openRecentProjectAction;

function computeHomeRecentActionHtml(j) {
    const isRecap = j.job_type === "RECAP";
    const isTts = j.job_type === "TTS";
    const safeId = escapeHtml(j.id);
    if (isRecap && j.script_ready) {
        return `<button type="button" class="vcp-chip active" onclick="openRecentProjectAction('${safeId}','review')">${escapeHtml(t("home_recent_action_review"))}</button>`;
    }
    if (j.status === "RUNNING" || j.status === "QUEUED") {
        const mode = isRecap ? "recap" : (isTts ? "tts" : "voice");
        return `<button type="button" class="vcp-chip active" onclick="openRecentProjectAction('${safeId}','${mode}')">${escapeHtml(t("home_recent_action_continue"))}</button>`;
    }
    if (j.status === "SUCCESS" && !j.is_expired) {
        if (isRecap && j.deliverables && j.deliverables.video) {
            return `<a href="#" data-dl-url="${j.deliverables.video}" data-dl-name="${brandedDlName("Recap", "mp4", j.input_snippet, j.id)}" class="vcp-chip active">${escapeHtml(t("home_recent_action_dl"))}</a>`;
        }
        if (!isRecap && j.download_url) {
            return `<a href="#" data-dl-url="${j.download_url}" data-dl-name="${brandedDlName("Voice", "wav", null, j.id)}" class="vcp-chip active">${escapeHtml(t("home_recent_action_dl"))}</a>`;
        }
    }
    if (j.status === "FAILED" && isRecap) {
        if (j.can_retry === false) {
            return `<button type="button" class="vcp-chip is-disabled" disabled title="${escapeHtml(t("recap_retry_upload_missing"))}">${escapeHtml(t("home_recent_action_retry"))}</button>`;
        }
        return `<button type="button" class="vcp-chip" onclick="openRecentProjectAction('${safeId}','retry')">${escapeHtml(t("home_recent_action_retry"))}</button>`;
    }
    return `<button type="button" class="vcp-chip" onclick="openHistoryHub()" aria-label="${escapeHtml(t("home_recent_action_open_history"))}">›</button>`;
}

function renderHomeRecentProjects() {
    const card = document.getElementById("home-recent-projects-card");
    const list = document.getElementById("home-recent-projects-list");
    if (!card || !list) return;

    if (hubHistoryCache === null && !hubHistoryError) {
        setSectionState(card, "loading");
        return;
    }
    if (hubHistoryError) {
        setSectionState(card, "error");
        return;
    }
    const all = hubHistoryCache || [];
    if (all.length === 0) {
        setSectionState(card, "empty");
        return;
    }
    setSectionState(card, "ready");
    const recent = all.slice(0, 5);
    list.innerHTML = recent.map(j => {
        const isRecap = j.job_type === "RECAP";
        const isTts = j.job_type === "TTS";
        const kind = isRecap ? "recap" : isTts ? "tts" : "voice";
        const icon = isRecap
            ? '<svg width="16" height="16" viewBox="0 0 24 24" class="vcp-ico" aria-hidden="true"><rect x="2" y="4" width="20" height="16" rx="3"/><path d="m10 9 5 3-5 3z"/></svg>'
            : isTts
                ? '<svg width="16" height="16" viewBox="0 0 24 24" class="vcp-ico" aria-hidden="true"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3z"/><path d="M3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg>'
                : '<svg width="16" height="16" viewBox="0 0 24 24" class="vcp-ico" aria-hidden="true"><path d="M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3Z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/></svg>';
        const snippet = j.input_snippet || (isRecap ? t("home_recent_fallback_recap") : isTts ? t("home_recent_fallback_tts") : t("home_recent_fallback_voice"));
        const isRunningOrQueued = j.status === "RUNNING" || j.status === "QUEUED";
        const pct = Math.min(100, Math.max(8, Number(j.progress_percent) || (j.status === "RUNNING" ? 55 : 22)));
        const progressHtml = isRunningOrQueued
            ? `<div class="vcp-job-progress"><span class="vcp-job-progress-fill" style="width:${pct}%"></span></div>`
            : "";
        return `
        <div class="home-recent-row vcp-job-card" data-recent-job-id="${j.id}" data-job-kind="${kind}">
            <div class="home-recent-main vcp-job-head">
                <span class="home-recent-icon">${icon}</span>
                <div class="home-recent-meta">
                    <span class="home-recent-name">${escapeHtml(snippet)}</span>
                </div>
            </div>
            ${progressHtml}
            <div class="home-recent-footer">
                <div class="home-recent-badges">
                    ${computeHomeRecentStatusBadgeHtml(j)}
                    ${computeRetentionBadgeHtml(j)}
                </div>
                <div class="home-recent-action vcp-job-footer-row">
                    ${computeHomeRecentActionHtml(j)}
                </div>
            </div>
        </div>`;
    }).join("");
}
window.renderHomeRecentProjects = renderHomeRecentProjects;

function toggleVcpHelpHub(forceOpen) {
    haptic("light");
    const homeTab = document.getElementById("tab-home");
    const btn = document.getElementById("btn-home-help-hub");
    if (!homeTab) return;
    const isOpen = typeof forceOpen === "boolean"
        ? forceOpen
        : !homeTab.classList.contains("vcp-help-hub-open");
    homeTab.classList.toggle("vcp-help-hub-open", isOpen);
    if (btn) btn.setAttribute("aria-expanded", isOpen ? "true" : "false");
    if (isOpen) {
        const tutMore = document.getElementById("home-tut-more");
        const tutHead = document.getElementById("home-tut-expand");
        if (tutMore && tutMore.classList.contains("hidden")) {
            tutMore.classList.remove("hidden");
            if (tutHead) tutHead.setAttribute("aria-expanded", "true");
        }
    }
}
window.toggleVcpHelpHub = toggleVcpHelpHub;

// ── Phase 3: Voice & TTS Helpers (Excess Banner, Cost Estimate, Custom Clone Steps & BYOK) ──
function estimateSpeechMinutesFromText(charCount, speedModeOrRate) {
    if (!charCount || charCount <= 0) return null;
    // Aligned with backend MEASURED_CHARS_PER_SECOND (17.6 * 60 = 1056 chars/min at 1.00x)
    const tempo = speedTempoFactor(speedModeOrRate);
    const mins = Math.max(0.1, charCount / (1056 * tempo));
    return mins.toFixed(1);
}
window.estimateSpeechMinutesFromText = estimateSpeechMinutesFromText;

function syncVoiceExcessAndCost(len, max) {
    const banner = document.getElementById("voice-char-excess-banner");
    const txt = document.getElementById("voice-char-excess-text");
    const vipBtn = document.getElementById("btn-voice-excess-vip");
    if (banner && txt) {
        if (len > max) {
            txt.textContent = tf("char_excess_warning", { excess: len - max });
            banner.classList.remove("hidden");
            if (vipBtn) vipBtn.classList.toggle("hidden", !!(state.user && (state.user.is_vip || state.user.is_admin)));
        } else {
            banner.classList.add("hidden");
        }
    }
    const costBadge = document.getElementById("voice-btn-est-cost");
    if (costBadge) {
        const estMin = estimateSpeechMinutesFromText(len, state.speed || "+12%");
        costBadge.textContent = estMin ? " " + tf("est_cost_min_badge", { min: estMin }) : "";
    }
}
window.syncVoiceExcessAndCost = syncVoiceExcessAndCost;

function trimVoiceTextToLimit() {
    haptic("light");
    const input = document.getElementById("voice-text-input");
    if (!input) return;
    const u = state.user || {};
    const max = u.character_limit || (u.is_vip ? (u.character_limit_purchased ?? 5000) : (u.character_limit_free ?? 1000));
    if (input.value.length > max) {
        input.value = input.value.slice(0, max);
        input.dispatchEvent(new Event("input"));
    }
}
window.trimVoiceTextToLimit = trimVoiceTextToLimit;

function setCustomCloneStep(stepNum, file) {
    state.customCloneStep = stepNum;
    const stepper = document.getElementById("custom-clone-stepper");
    if (stepper) {
        stepper.querySelectorAll("[data-clone-step]").forEach(el => {
            const n = parseInt(el.getAttribute("data-clone-step") || "1", 10);
            el.classList.toggle("is-active", n === stepNum);
            el.classList.toggle("is-done", n < stepNum);
            el.classList.toggle("is-locked", n > stepNum);
        });
    }
    const checkEl = document.getElementById("custom-clone-check-status");
    if (checkEl) {
        const refFile = file || state.selectedReferenceFile;
        if (refFile && stepNum >= 2) {
            checkEl.textContent = tf("clone_check_ok", { kb: Math.max(1, Math.round(refFile.size / 1024)) });
            checkEl.classList.remove("hidden");
        } else if (!file) {
            checkEl.textContent = "";
            checkEl.classList.add("hidden");
        }
    }
}
window.setCustomCloneStep = setCustomCloneStep;

function syncSavedCustomVoiceUi() {
    const id = state.savedCustomVoiceId || "";
    const name = state.savedCustomVoiceName || "Custom Voice";
    const recapAllowed = typeof canUseRecapCloneEngine === "function"
        ? canUseRecapCloneEngine()
        : Boolean(state.user && (state.user.is_vip || state.user.is_admin));
    [["saved-custom-voice-bar", "saved-custom-voice-label", true], ["recap-saved-voice-bar", "recap-saved-voice-label", recapAllowed]].forEach(([bId, lId, allow]) => {
        const bar = document.getElementById(bId);
        const lbl = document.getElementById(lId);
        if (bar) bar.classList.toggle("hidden", !id || !allow);
        if (lbl && id) lbl.innerText = tf("saved_custom_voice_use", { name });
    });
}
window.syncSavedCustomVoiceUi = syncSavedCustomVoiceUi;

function applySavedCustomVoiceToVoice() {
    if (!state.savedCustomVoiceId) return;
    if (typeof switchVoxCPMSubMode === "function") switchVoxCPMSubMode("custom");
    state.selectedReferenceFile = null;
    state.savedCustomVoiceActiveId = state.savedCustomVoiceId;
    const lbl = document.getElementById("ref-audio-label");
    if (lbl) lbl.innerText = `💾 ${state.savedCustomVoiceName || "Saved Custom Voice"}`;
    const checkEl = document.getElementById("custom-clone-check-status");
    if (checkEl) {
        checkEl.innerText = `✅ ${state.savedCustomVoiceName || "Saved Custom Voice"}`;
        checkEl.classList.remove("hidden");
    }
    showStudioToast(`✅ ${state.savedCustomVoiceName || "Saved Custom Voice"}`, "success");
}
window.applySavedCustomVoiceToVoice = applySavedCustomVoiceToVoice;

function applySavedCustomVoiceToRecap() {
    if (!state.savedCustomVoiceId) return;
    state.recapOwnVoiceRefId = state.savedCustomVoiceId;
    state.recapOwnVoiceFile = null;
    state.recapOwnVoiceFileName = state.savedCustomVoiceName || "Saved Custom Voice";
    state.recapOwnVoiceStatus = "ready";
    state.recapOwnVoiceSelected = true;
    const label = document.getElementById("recap-own-voice-file-label");
    if (label) label.innerText = `💾 ${state.recapOwnVoiceFileName}`;
    syncRecapOwnVoiceCard();
    if (typeof refreshRecapStep4Choices === "function") refreshRecapStep4Choices();
    showStudioToast(t("recap_own_voice_saved_toast"), "success");
}
window.applySavedCustomVoiceToRecap = applySavedCustomVoiceToRecap;

async function saveCurrentRefAsCustomVoice() {
    haptic("light");
    const file = state.selectedReferenceFile;
    if (!file) return;
    const allowed = typeof canUseRecapCloneEngine === "function"
        ? canUseRecapCloneEngine()
        : Boolean(state.user && (state.user.is_vip || state.user.is_admin));
    if (!allowed) {
        showStudioToast(t("recap_own_voice_locked_hint"), "warning");
        switchTab("tab-vip");
        return;
    }
    const saveBtn = document.getElementById("btn-save-custom-voice-from-result");
    if (typeof setVcpButtonLoading === "function") setVcpButtonLoading(saveBtn, true);
    try {
        const fd = new FormData();
        fd.append("reference_audio", file);
        const res = await fetchWithTimeout("/api/recap/upload_voice_ref", {
            method: "POST",
            headers: getAuthHeaders(false),
            body: fd,
        }, 120000);
        const data = await res.json().catch(() => ({}));
        if (!res.ok || !data.ref_id) throw new Error(data.detail || t("err_generic"));
        state.recapOwnVoiceRefId = data.ref_id;
        state.recapOwnVoiceFile = file;
        state.recapOwnVoiceFileName = data.filename || file.name || "";
        state.recapOwnVoiceStatus = "ready";
        state.recapOwnVoiceSelected = true;
        state.savedCustomVoiceId = data.ref_id;
        state.savedCustomVoiceName = data.filename || file.name || "Custom Voice";
        syncSavedCustomVoiceUi();
        if (typeof saveAccountPrefsSoon === "function") saveAccountPrefsSoon();
        if (typeof syncRecapOwnVoiceCard === "function") syncRecapOwnVoiceCard();
        setCustomCloneStep(5, file);
        showStudioToast(t("recap_own_voice_saved_toast"), "success");
    } catch (err) {
        showStudioToast((err && err.message) || t("err_generic"), "error");
    } finally {
        if (typeof setVcpButtonLoading === "function") setVcpButtonLoading(saveBtn, false);
    }
}
window.saveCurrentRefAsCustomVoice = saveCurrentRefAsCustomVoice;

function syncTtsExcessAndCost(len, max) {
    const banner = document.getElementById("tts-char-excess-banner");
    const txt = document.getElementById("tts-char-excess-text");
    const vipBtn = document.getElementById("btn-tts-excess-vip");
    if (banner && txt) {
        if (len > max) {
            txt.textContent = tf("char_excess_warning", { excess: len - max });
            banner.classList.remove("hidden");
            if (vipBtn) vipBtn.classList.toggle("hidden", !!(state.user && (state.user.is_vip || state.user.is_admin)));
        } else {
            banner.classList.add("hidden");
        }
    }
    const costBadge = document.getElementById("tts-btn-est-cost");
    if (costBadge) {
        const estMin = estimateSpeechMinutesFromText(len, (typeof ttsState !== "undefined" && ttsState.speed) || "+12%");
        costBadge.textContent = estMin ? " " + tf("est_cost_min_badge", { min: estMin }) : "";
    }
}
window.syncTtsExcessAndCost = syncTtsExcessAndCost;

function trimTtsTextToLimit() {
    haptic("light");
    const input = document.getElementById("tts-text-input");
    if (!input) return;
    const u = state.user || {};
    const max = u.character_limit || (u.is_vip ? (u.character_limit_purchased ?? 5000) : (u.character_limit_free ?? 1000));
    if (input.value.length > max) {
        input.value = input.value.slice(0, max);
        input.dispatchEvent(new Event("input"));
    }
}
window.trimTtsTextToLimit = trimTtsTextToLimit;

function toggleTtsByokAdvanced(forceOpen) {
    haptic("light");
    const body = document.getElementById("tts-byok-advanced-body");
    const head = document.getElementById("tts-byok-advanced-toggle");
    if (!body) return;
    const isOpen = typeof forceOpen === "boolean"
        ? forceOpen
        : body.classList.contains("hidden");
    body.classList.toggle("hidden", !isOpen);
    if (head) {
        head.classList.toggle("open", isOpen);
        head.setAttribute("aria-expanded", isOpen ? "true" : "false");
    }
}
window.toggleTtsByokAdvanced = toggleTtsByokAdvanced;

document.addEventListener("DOMContentLoaded", () => {
    initTelegram();
    setupViewportAndKeyboardSync();
    applyDesktopDropHint();
    initEntryGate();
    setLanguage(currentLang);
    setupTabs();
    // AZ-4: park the sliding nav pill under the initially-active
    // item once first layout exists.
    requestAnimationFrame(() => { try { moveNavPill(); } catch (e) {} try { moveSpeedPills(); } catch (e) {} try { moveSegPills(); } catch (e) {} });
    setupVoiceStudio();
    setupRecapVisualEditor();
    initRecapSteps();
    switchRecapStep2Category(state.recapStep2Cat || "preset", true);
    syncRecapMasterPresetActive();
    syncRecapSubStyleCards();
    renderMyRecapPresets();
    const recapStep2El = document.getElementById("recap-step-2");
    if (recapStep2El) {
        recapStep2El.addEventListener("click", () => {
            setTimeout(() => {
                if (typeof syncRecapMasterPresetActive === "function") syncRecapMasterPresetActive();
                if (typeof syncRecapSubStyleCards === "function") syncRecapSubStyleCards();
            }, 0);
        });
    }
    setupAlertCarousel();
    setupDownloader();
    setupSlipPreview();
    setupStickyCtas();
    setupSamplePlayers();
    syncTelegramBackButton();

    loadUserProfile();
    loadCuratedVoices();
    loadLegacyModels();
    loadUserHistory();
    checkStartupDeepLinks();
    // Batch AC: global live-jobs transport + account-bound prefs.
    LiveJobs.start();
    setupAccountPrefs();
    setupSubmitGuards();
    loadAccountPrefs();
});

// ==================== PHASE 4: RECAP STEP 2 SPLIT-SCREEN 5-TAB EDITOR ====================

const RECAP_MASTER_PRESETS = {
    tiktok: {
        recapRatio: "9:16",
        recapRatioFill: "blur",
        recapSubStyle: "yellow",
        recapLines: 1,
        recapProtection: "standard",
        recapBurnSubs: true,
        recapBlurSubs: true,
        recapMirror: true,
        recapZoom: 1.0,
        recapTextColor: "",
        recapOutlineColor: "",
        recapBgColor: "",
    },
    fb_story: {
        recapRatio: "9:16",
        recapRatioFill: "crop",
        recapSubStyle: "boxed",
        recapLines: 2,
        recapProtection: "standard",
        recapBurnSubs: true,
        recapBlurSubs: true,
        recapMirror: true,
        recapZoom: 1.0,
        recapTextColor: "",
        recapOutlineColor: "",
        recapBgColor: "",
    },
    yt_16_9: {
        recapRatio: "16:9",
        recapRatioFill: "blur",
        recapSubStyle: "white",
        recapLines: 1,
        recapProtection: "standard",
        recapBurnSubs: true,
        recapBlurSubs: true,
        recapMirror: true,
        recapZoom: 1.0,
        recapTextColor: "",
        recapOutlineColor: "",
        recapBgColor: "",
    },
    strict_safe: {
        recapRatio: "9:16",
        recapRatioFill: "blur",
        recapSubStyle: "band",
        recapLines: 1,
        recapProtection: "strong",
        recapBurnSubs: true,
        recapBlurSubs: true,
        recapBlurAutoDetect: true,
        recapMirror: true,
        recapZoom: 1.06,
        recapTextColor: "",
        recapOutlineColor: "",
        recapBgColor: "",
    },
};

const PORTABLE_RECAP_PRESET_KEYS = [
    "recapRatio", "recapRatioFill",
    "recapCropTop", "recapCropBottom", "recapCropLeft", "recapCropRight", "recapSyncCrop",
    "recapMirror", "recapZoom", "recapProtection",
    "recapBurnSubs", "recapSubStyle", "recapSubFont", "recapFontSize", "recapLines",
    "recapSubRenderMode", "recapSubEffect", "recapEmbedSoftSubs",
    "recapSubMargin", "recapSubX", "recapSubW",
    "recapTextColor", "recapOutlineColor", "recapBgColor",
    "recapOutlineWidth", "recapBgOpacity", "recapSubShadow", "recapSubBold", "recapSubPosBand",
    "recapBlurSubs", "recapBlurAutoDetect",
];

function applyActiveRecapStep2Category() {
    const valid = ["subtitle", "blur", "protection", "preset", "frame"];
    const cat = valid.includes(state.recapStep2Cat) ? state.recapStep2Cat : "subtitle";
    state.recapStep2Cat = cat;

    document.querySelectorAll("#recap-step2-cat-tabs [data-step2-cat]").forEach(btn => {
        const match = btn.getAttribute("data-step2-cat") === cat;
        btn.classList.toggle("active", match);
        btn.setAttribute("aria-selected", match ? "true" : "false");
    });

    const setAccOpen = (id, open) => {
        const grp = document.getElementById(id);
        if (!grp) return;
        grp.classList.toggle("open", open);
        const body = grp.querySelector(":scope > .dd-acc-body");
        if (body) body.classList.toggle("hidden", !open);
        const hd = grp.querySelector(":scope > .dd-acc-hd");
        if (hd) hd.setAttribute("aria-expanded", open ? "true" : "false");
    };

    // Always keep Aspect Ratio picker visible directly under Preview (user essential control!)
    const ratioGroup = document.getElementById("recap-preview-ratio-group");
    if (ratioGroup) {
        ratioGroup.classList.remove("hidden");
        setAccOpen("recap-preview-ratio-group", true);
    }

    const presetPanel = document.getElementById("recap-cat-preset");
    if (presetPanel) presetPanel.classList.toggle("hidden", cat !== "preset");

    const inFine = cat === "subtitle" || cat === "blur" || cat === "protection" || cat === "preset";
    const fineWrap = document.querySelector("#recap-step-2 .recap-fine");
    const fineHeader = document.querySelector("#recap-step-2 .recap-fine-header");
    if (fineWrap) fineWrap.classList.toggle("hidden", !inFine);
    if (fineHeader) fineHeader.classList.toggle("vcp-tab-mode-hidden", inFine);

    if (typeof toggleRecapFineSettings === "function") {
        toggleRecapFineSettings(inFine);
    }

    // Core 3 tabs: Subtitle, Blur, Logo (protection) — Anti-Copyright Transform lives in Advanced (preset)
    const groupCatMap = {
        "recap-subtitle-group": "subtitle",
        "recap-blur-group": "blur",
        "recap-logo-group": "protection",
        "recap-preview-transform-group": "preset",
    };
    Object.keys(groupCatMap).forEach(id => {
        const grp = document.getElementById(id);
        if (!grp) return;
        const show = groupCatMap[id] === cat;
        grp.classList.toggle("hidden", !show);
        if (show) setAccOpen(id, true);
    });

    const blurBox = document.getElementById("preview-blur-box");
    if (blurBox) blurBox.classList.toggle("label-on", cat === "blur");

    if (typeof moveSegPills === "function" || typeof window.__ddFitPreviewFrame === "function") {
        requestAnimationFrame(() => {
            try { moveSpeedPills(); } catch (e) {}
            try { moveSegPills(); } catch (e) {}
            try { if (window.__ddFitPreviewFrame) window.__ddFitPreviewFrame(); } catch (e) {}
        });
    }
}
window.applyActiveRecapStep2Category = applyActiveRecapStep2Category;

function switchRecapStep2Category(cat, silent) {
    if (!silent && typeof haptic === "function") haptic("light");
    const valid = ["subtitle", "blur", "protection", "preset", "frame"];
    state.recapStep2Cat = valid.includes(cat) ? cat : "subtitle";
    applyActiveRecapStep2Category();
}
window.switchRecapStep2Category = switchRecapStep2Category;

function applyRecapPresetObject(patch) {
    if (!patch || typeof patch !== "object") return;
    const base = typeof window.__snapshotRecapSettings === "function"
        ? window.__snapshotRecapSettings()
        : {};
    const next = Object.assign({}, base, patch);
    if (next.recapSpeed != null) next.recapSpeed = normalizeSpeedOption(next.recapSpeed);
    const settingsKey = window.__RECAP_SETTINGS_KEY || "vcp_recap_settings";
    try { localStorage.setItem(settingsKey, JSON.stringify(next)); } catch (e) {}
    if (typeof state === "object" && state) {
        Object.assign(state, patch);
        state.recapSpeed = normalizeSpeedOption(state.recapSpeed);
    }
    const restoreFn = typeof window.restoreRecapSettings === "function"
        ? window.restoreRecapSettings
        : (typeof window.resyncRecapControlsAfterPrefs === "function" ? window.resyncRecapControlsAfterPrefs : null);
    if (restoreFn) restoreFn();
    if (typeof window.updateCanvasMockupVisuals === "function") window.updateCanvasMockupVisuals();
    if (typeof window.__ddFitPreviewFrame === "function") window.__ddFitPreviewFrame();
    if (typeof window.refreshRecapStep2Summary === "function") window.refreshRecapStep2Summary();
    if (typeof window.refreshRecapStep4Output === "function") window.refreshRecapStep4Output();
    if (typeof window.__saveRecapSettingsNow === "function") window.__saveRecapSettingsNow();
    if (typeof saveAccountPrefsSoon === "function") saveAccountPrefsSoon();
    if (typeof applyActiveRecapStep2Category === "function") applyActiveRecapStep2Category();
    syncRecapSubStyleCards();
    syncRecapMasterPresetActive();
}

function applyRecapMasterPreset(presetKey) {
    const patch = RECAP_MASTER_PRESETS[presetKey];
    if (!patch) return;
    if (typeof haptic === "function") haptic("medium");
    applyRecapPresetObject(patch);
    if (typeof showStudioToast === "function" && typeof t === "function") {
        const presetLabel = t(`preset_${presetKey}_name`);
        const msg = typeof tf === "function"
            ? tf("recap_master_preset_applied_toast", { name: presetLabel })
            : t("recap_master_preset_applied_toast");
        showStudioToast(msg, "success");
    }
}
window.applyRecapMasterPreset = applyRecapMasterPreset;

function syncRecapMasterPresetActive() {
    const cards = document.querySelectorAll("#recap-master-presets-grid [data-master-preset]");
    if (!cards.length) return;
    const curRatio = state.recapRatio || "9:16";
    const curFill = state.recapRatioFill || "blur";
    const curStyle = state.recapSubStyle || "yellow";
    const curLines = Number(state.recapLines || 1);
    const curProt = state.recapProtection || "standard";
    cards.forEach(btn => {
        const key = btn.getAttribute("data-master-preset");
        const p = RECAP_MASTER_PRESETS[key];
        const match = Boolean(p)
            && p.recapRatio === curRatio
            && p.recapRatioFill === curFill
            && p.recapSubStyle === curStyle
            && Number(p.recapLines) === curLines
            && p.recapProtection === curProt;
        btn.classList.toggle("active", match);
        btn.setAttribute("aria-pressed", match ? "true" : "false");
    });
}
window.syncRecapMasterPresetActive = syncRecapMasterPresetActive;

function extractPortablePresetSettings() {
    const snap = typeof window.__snapshotRecapSettings === "function"
        ? window.__snapshotRecapSettings()
        : state;
    const out = {};
    PORTABLE_RECAP_PRESET_KEYS.forEach(k => {
        if (snap && Object.prototype.hasOwnProperty.call(snap, k) && snap[k] !== undefined) {
            out[k] = snap[k];
        }
    });
    return out;
}

function parseMyRecapPresets(raw) {
    let arr = [];
    if (Array.isArray(raw)) {
        arr = raw;
    } else if (typeof raw === "string" && raw.trim()) {
        try {
            const parsed = JSON.parse(raw);
            if (Array.isArray(parsed)) arr = parsed;
        } catch (e) {
            return [];
        }
    }
    const clean = [];
    arr.slice(0, 3).forEach(item => {
        if (!item || typeof item !== "object") return;
        const name = String(item.name || "").trim().slice(0, 32);
        const settings = item.settings && typeof item.settings === "object" && !Array.isArray(item.settings)
            ? item.settings
            : null;
        if (name && settings) clean.push({ name, settings });
    });
    return clean;
}
window.parseMyRecapPresets = parseMyRecapPresets;

function serializeMyRecapPresets() {
    const list = Array.isArray(state.recapCustomPresets) ? state.recapCustomPresets : [];
    try {
        return JSON.stringify(list.slice(0, 3));
    } catch (e) {
        return "[]";
    }
}
window.serializeMyRecapPresets = serializeMyRecapPresets;

function renderMyRecapPresets() {
    const isVip = Boolean(state.user && state.user.is_vip);
    const badge = document.getElementById("recap-my-presets-vip-badge");
    if (badge) badge.classList.toggle("hidden", isVip);

    const listEl = document.getElementById("recap-my-presets-list");
    if (!listEl) return;

    const presets = Array.isArray(state.recapCustomPresets) ? state.recapCustomPresets : [];
    if (!presets.length) {
        const emptyText = typeof t === "function" ? t("recap_my_preset_empty") : "";
        listEl.innerHTML = `<div class="vcp-my-preset-empty" data-i18n="recap_my_preset_empty">${escapeHtml(emptyText)}</div>`;
        return;
    }

    listEl.innerHTML = presets.map((p, idx) => {
        const metaParts = [
            p.settings && p.settings.recapRatio ? p.settings.recapRatio : "9:16",
            p.settings && p.settings.recapSubStyle ? p.settings.recapSubStyle : "yellow",
        ];
        const applyLabel = typeof t === "function" ? t("recap_my_preset_apply") : "Apply";
        const delLabel = typeof t === "function" ? t("recap_my_preset_delete") : "Delete";
        return `
            <div class="vcp-my-preset-item">
                <button type="button" class="vcp-my-preset-apply" onclick="applyMyRecapPreset(${idx})" title="${escapeHtml(applyLabel)}">
                    <span class="vcp-my-preset-name">${escapeHtml(p.name)}</span>
                    <span class="vcp-my-preset-meta">${escapeHtml(metaParts.join(" · "))}</span>
                </button>
                <button type="button" class="vcp-my-preset-del" onclick="deleteMyRecapPreset(${idx})" aria-label="${escapeHtml(delLabel)} ${escapeHtml(p.name)}" title="${escapeHtml(delLabel)}">✕</button>
            </div>
        `;
    }).join("");
}
window.renderMyRecapPresets = renderMyRecapPresets;

function saveCurrentAsMyRecapPreset() {
    if (typeof haptic === "function") haptic("light");
    const isVip = Boolean(state.user && state.user.is_vip);
    if (!isVip) {
        if (typeof showStudioToast === "function" && typeof t === "function") {
            showStudioToast(t("recap_my_preset_vip_only"), "warning");
        }
        return;
    }
    const list = Array.isArray(state.recapCustomPresets) ? state.recapCustomPresets.slice() : [];
    if (list.length >= 3) {
        if (typeof showStudioToast === "function" && typeof t === "function") {
            showStudioToast(t("recap_my_preset_max_toast"), "warning");
        }
        return;
    }
    const input = document.getElementById("input-my-preset-name");
    const rawName = input ? input.value.trim() : "";
    const defTpl = (typeof t === "function" && t("recap_my_preset_default_name")) || "My Preset {n}";
    const name = (rawName || defTpl.replace("{n}", String(list.length + 1))).slice(0, 32);
    list.push({ name, settings: extractPortablePresetSettings() });
    if (JSON.stringify(list).length > 3900) {
        if (typeof showStudioToast === "function" && typeof t === "function") {
            showStudioToast(t("recap_my_preset_too_large"), "warning");
        }
        return;
    }
    state.recapCustomPresets = list;
    if (input) input.value = "";
    renderMyRecapPresets();
    if (typeof saveAccountPrefsNow === "function") saveAccountPrefsNow();
    if (typeof showStudioToast === "function" && typeof t === "function") {
        showStudioToast(t("recap_my_preset_saved_toast"), "success");
    }
}
window.saveCurrentAsMyRecapPreset = saveCurrentAsMyRecapPreset;

function applyMyRecapPreset(idx) {
    const list = Array.isArray(state.recapCustomPresets) ? state.recapCustomPresets : [];
    const item = list[idx];
    if (!item || !item.settings) return;
    if (typeof haptic === "function") haptic("medium");
    applyRecapPresetObject(item.settings);
    if (typeof showStudioToast === "function" && typeof t === "function") {
        showStudioToast(t("recap_my_preset_applied_toast"), "success");
    }
}
window.applyMyRecapPreset = applyMyRecapPreset;

function deleteMyRecapPreset(idx) {
    const list = Array.isArray(state.recapCustomPresets) ? state.recapCustomPresets.slice() : [];
    if (idx < 0 || idx >= list.length) return;
    if (typeof haptic === "function") haptic("light");
    list.splice(idx, 1);
    state.recapCustomPresets = list;
    renderMyRecapPresets();
    if (typeof saveAccountPrefsNow === "function") saveAccountPrefsNow();
    if (typeof showStudioToast === "function" && typeof t === "function") {
        showStudioToast(t("recap_my_preset_deleted_toast"), "info");
    }
}
window.deleteMyRecapPreset = deleteMyRecapPreset;

function syncRecapSubStyleCards() {
    const sel = document.getElementById("sub-style-picker");
    const cur = state.recapSubStyle || (sel ? sel.value : "yellow") || "yellow";
    document.querySelectorAll("#sub-style-cards-grid [data-style-card]").forEach(btn => {
        const match = btn.getAttribute("data-style-card") === cur;
        btn.classList.toggle("active", match);
        btn.setAttribute("aria-pressed", match ? "true" : "false");
    });
}
window.syncRecapSubStyleCards = syncRecapSubStyleCards;

function selectRecapSubStyleCard(styleKey) {
    if (typeof haptic === "function") haptic("light");
    const sel = document.getElementById("sub-style-picker");
    if (sel) {
        sel.value = styleKey;
        sel.dispatchEvent(new Event("change", { bubbles: true }));
    } else {
        state.recapSubStyle = styleKey;
        syncRecapSubStyleCards();
    }
    syncRecapMasterPresetActive();
}
window.selectRecapSubStyleCard = selectRecapSubStyleCard;

function toggleRecapSubL2Detail(forceOpen) {
    if (typeof haptic === "function") haptic("light");
    const body = document.getElementById("recap-sub-l2-body");
    const btn = document.getElementById("btn-toggle-sub-l2-detail");
    const chev = document.getElementById("sub-l2-chevron");
    if (!body) return;
    const isOpen = typeof forceOpen === "boolean" ? forceOpen : body.classList.contains("hidden");
    body.classList.toggle("hidden", !isOpen);
    if (btn) btn.setAttribute("aria-expanded", isOpen ? "true" : "false");
    if (chev) chev.textContent = "▼";
    if (isOpen && typeof moveSegPills === "function") {
        requestAnimationFrame(() => { try { moveSegPills(); } catch (e) {} });
    }
}
window.toggleRecapSubL2Detail = toggleRecapSubL2Detail;

function toggleRecapBlurL2Detail(forceOpen) {
    if (typeof haptic === "function") haptic("light");
    const body = document.getElementById("recap-blur-l2-body");
    const btn = document.getElementById("btn-toggle-blur-l2-detail");
    const chev = document.getElementById("blur-l2-chevron");
    if (!body) return;
    const isOpen = typeof forceOpen === "boolean" ? forceOpen : body.classList.contains("hidden");
    body.classList.toggle("hidden", !isOpen);
    if (btn) btn.setAttribute("aria-expanded", isOpen ? "true" : "false");
    if (chev) chev.textContent = "▼";
}
window.toggleRecapBlurL2Detail = toggleRecapBlurL2Detail;

function refreshAdminLanguageSurfaces() {
    if (window._lastAdminOverview && typeof renderAdminOverview === "function") {
        renderAdminOverview(window._lastAdminOverview);
    }
    if (Array.isArray(window._lastAdminSlips) && typeof renderAdminSlips === "function") {
        renderAdminSlips(window._lastAdminSlips);
    }
    if (typeof admKeyHealthData !== "undefined" && admKeyHealthData && typeof renderAdminKeyHealth === "function") {
        renderAdminKeyHealth();
        const accBtnSpan = document.querySelector("#adm-keys-accounts-btn span");
        if (accBtnSpan) {
            const key = admKeyHealthAccountsOpen ? "adm_keys_accounts_hide" : "adm_keys_accounts_show";
            accBtnSpan.setAttribute("data-i18n", key);
            accBtnSpan.innerText = t(key);
        }
    }
    if (window._lastAdminQueue && typeof renderAdminQueueData === "function") {
        renderAdminQueueData(window._lastAdminQueue);
    }
    if (window._lastAdminHistory && typeof renderAdminHistoryData === "function") {
        renderAdminHistoryData(window._lastAdminHistory);
    }
    const recentUsersBox = document.getElementById("admin-users-recent");
    if (recentUsersBox && Array.isArray(window._lastAdminUsersList)) {
        if (!window._lastAdminUsersList.length) admStateEmpty(recentUsersBox, "admin_empty");
        else recentUsersBox.innerHTML = window._lastAdminUsersList.map(admUserRowHtml).join("");
    }
    const searchUsersBox = document.getElementById("admin-user-search-results");
    if (searchUsersBox && Array.isArray(window._lastAdminUsersSearch)) {
        if (!window._lastAdminUsersSearch.length) admStateEmpty(searchUsersBox, "admin_search_none");
        else searchUsersBox.innerHTML = window._lastAdminUsersSearch.map(admUserRowHtml).join("");
    }
    if (window._lastAdminUserDetail && typeof renderAdminUserDetail === "function") {
        renderAdminUserDetail(window._lastAdminUserDetail);
    }
    if (Array.isArray(window._lastAdminUserJobs) && typeof renderAdminUserJobsList === "function") {
        renderAdminUserJobsList(window._lastAdminUserJobs);
    }
}
window.refreshAdminLanguageSurfaces = refreshAdminLanguageSurfaces;

function refreshPhase1to4LanguageSurfaces() {
    try {
        if (state && state.user && typeof paintCachedUserHeader === "function") {
            paintCachedUserHeader(state.user);
        }
        if (state && state.user) {
            const catTag = document.getElementById("profile-category-tag");
            if (catTag) {
                catTag.removeAttribute("data-i18n");
                catTag.innerText = state.user.is_admin ? "ADMIN ⭐" : (state.user.is_vip ? t("vip_member_pro") : t("vip_member_free"));
            }
            const recapLimitText = document.getElementById("recap-tier-limit-text");
            if (recapLimitText) {
                recapLimitText.innerText = t(state.user.is_vip ? "recap_tier_vip_text" : "recap_tier_free_text");
            }
        }
        if (state && state.user && typeof paintHomeStatusStrip === "function") {
            paintHomeStatusStrip(state.user);
        }
        if (state && state.user && typeof paintVipPlanBalances === "function") {
            paintVipPlanBalances(state.user);
        }
        if (state && state.user && typeof paintTtsTodaySurfaces === "function") {
            paintTtsTodaySurfaces(state.user);
        }
        if (typeof paintTtsCostHintTiered === "function") {
            paintTtsCostHintTiered(window._lastTtsQuota || null, (state && state.user) || null);
        }
        if (window._lastTtsQuota && typeof paintTtsChipFromQuota === "function") {
            paintTtsChipFromQuota(window._lastTtsQuota);
        }
        if (typeof paintTtsQuotaInstant === "function") {
            paintTtsQuotaInstant();
        }
        if (Array.isArray(window._lastAdminPricingPlans) && typeof renderAdminPricingList === "function") {
            renderAdminPricingList(window._lastAdminPricingPlans);
        }
        const personaBadge = document.getElementById("personas-count");
        if (personaBadge && typeof CURATED_VOICES !== "undefined") {
            const activeChip = document.querySelector("#curated-personas-card .filter-chip.active");
            const gf = activeChip ? (activeChip.getAttribute("data-filter") || "all") : "all";
            const n = gf === "all" ? CURATED_VOICES.length : CURATED_VOICES.filter(v => v.gender === gf).length;
            personaBadge.innerText = tf("personas_count_fmt", { n });
        }
        if (typeof paintPaymentAccountNames === "function") {
            paintPaymentAccountNames(window._lastPaymentAccounts || []);
        }
        const platformEl = document.getElementById("recap-info-platform");
        if (platformEl && platformEl.dataset.sourceType === "direct_upload") {
            platformEl.innerText = t("recap_source_direct_mp4");
        }
        document.querySelectorAll("#recap-info-dur .recap-ready-badge").forEach(el => {
            el.innerText = `(${t("recap_upload_ready_badge")})`;
        });
        if (typeof updateRecapDurationGate === "function") {
            updateRecapDurationGate();
        }
        refreshAdminLanguageSurfaces();
        if (window._lastReferralStats && typeof updateReferUI === "function") {
            updateReferUI(window._lastReferralStats);
        }
        if (typeof renderHomeRecentProjects === "function") {
            renderHomeRecentProjects();
        }
        const sampleAudio = document.getElementById("sample-audio-voice");
        const tryBtn = document.getElementById("welcome-try-sample-btn");
        if (sampleAudio && tryBtn && typeof t === "function") {
            const playing = !sampleAudio.paused && !sampleAudio.ended;
            tryBtn.textContent = t(playing ? "welcome_try_sample_playing" : "welcome_try_sample_btn");
        }
        const u = (state && state.user) || {};
        const max = u.character_limit || (u.is_vip ? (u.character_limit_purchased ?? 5000) : (u.character_limit_free ?? 1000));
        const vInp = document.getElementById("voice-text-input");
        const vCount = document.getElementById("char-count");
        if (vCount) {
            vCount.innerText = `${(vInp?.value || "").length} / ${max} ${t("char_counter_unit")}`;
        }
        if (vInp && typeof syncVoiceExcessAndCost === "function") {
            syncVoiceExcessAndCost((vInp.value || "").length, max);
        }
        if (typeof window.updateTtsCharCount === "function") {
            const _repaintTtsCount = window.updateTtsCharCount;
            _repaintTtsCount();
        }
        const tInp = document.getElementById("tts-text-input");
        if (tInp && typeof syncTtsExcessAndCost === "function") {
            syncTtsExcessAndCost((tInp.value || "").length, max);
        }
        if (state && state.selectedReferenceFile && typeof setCustomCloneStep === "function") {
            setCustomCloneStep(state.customCloneStep || 2, state.selectedReferenceFile);
        }
        if (typeof renderMyRecapPresets === "function") {
            renderMyRecapPresets();
        }
        if (typeof window.refreshRecapStep2Summary === "function") {
            window.refreshRecapStep2Summary();
        }
        if (typeof window.syncSavedCustomVoiceUi === "function") {
            window.syncSavedCustomVoiceUi();
        }
        if (typeof renderCuratedVoices === "function" && Array.isArray(state?.curatedVoices) && state.curatedVoices.length) {
            renderCuratedVoices();
        }
        if (typeof renderHistoryHub === "function" && Array.isArray(hubHistoryCache)) {
            renderHistoryHub();
        }
    } catch (e) {}
}
window.refreshPhase1to4LanguageSurfaces = refreshPhase1to4LanguageSurfaces;

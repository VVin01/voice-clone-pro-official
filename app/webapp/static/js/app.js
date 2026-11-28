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
    const closeLabel = typeof t === "function" ? t("toast_close") : "Close";
    toast.innerHTML = `
        <span class="toast-icon">${icons[type] || icons.info}</span>
        <span class="toast-msg"></span>
        <button class="toast-close" onclick="this.parentNode.remove()" aria-label="${closeLabel}">✕</button>
        <span class="toast-progress" aria-hidden="true"></span>
    `;
    // The message is COPY, never markup (hunt X2): it is rendered via
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
    if (flow === 'recap' && has('စုစုပေါင်း') && has('credits လိုအပ်ပါတယ်')) {
        return typeof localizeServerText === "function" ? localizeServerText(rawStr) : rawStr;
    }
    // (c) Recap SUBMIT-time rejections that happen BEFORE any charge
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
    if (has('membership_required', 'gate_not_configured', 'gate_')) return t('gate_required_toast');
    // File too large (coded detail + the 413 wording, both languages).
    if (has('file_too_large', 'file size exceeds', 'too large', '413')) return t('err_too_large');
    // Voice engine quota / capacity: stored job errors mention ZeroGPU
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

    // Recap Configuration (Round 10 locked pro defaults: fill=blur, crop=CinemaScope 12% non-Original,
    // protection=strong, mirror=true, zoom=1.10)
    recapRatio: "9:16",
    recapRatioFill: "blur",
    recapCropTop: 12,
    recapCropBottom: 12,
    recapCropLeft: 0,
    recapCropRight: 0,
    recapSyncCrop: true,
    recapMirror: true,
    recapZoom: 1.1,
    recapSeekSec: 5,
    recapSpeed: "+12%",
    recapStoryStyle: "pov",
    recapBurnSubs: true,
    recapBlurSubs: true,
    // Batch BI: source-subtitle auto-detect blur switch.
    recapBlurAutoDetect: false,
    recapLogo: true,
    recapLogoPos: "top-right",
    recapLogoScale: 0.16,
    recapLogoOpacity: 0.70,
    // Custom channel logo (paid-only, owner decision 2026-11-02):
    logoCanUse: false,
    logoHas: false,
    logoUrl: null,
    recapSubStyle: "cinematic",
    recapSubFont: "padauk",
    recapFontSize: 13,
    recapLines: 1,
    recapSubRenderMode: "timeline",
    recapSubEffect: "none",
    recapEmbedSoftSubs: true,
    savedCustomVoiceId: "",
    savedCustomVoiceName: "",
    // Batch AA: true once the user DELIBERATELY sets the blur box
    recapBlurBoxTouched: false,
    // Batch AG: free 2-axis adjustment defaults — blur box left /
    recapBlurX: 4,
    recapBlurW: 92,
    recapSubX: 50,
    recapSubW: 92,
    // Batch AK: the subtitle's vertical anchor is the BOTTOM-EDGE
    recapSubMargin: 5,
    recapSettingsVersion: 3,
    // Batch DD: Step-2 rebuild state — protection level, free
    recapProtection: "strong",
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
    recapLoadedSourceKey: null,
    recapSourceLanguage: "auto",
    recapRenderQuality: "standard",
    recapTtsEngine: "gemini",
    recapGeminiVoice: "",
    recapEdgeVoice: "thiha",
    recapVoice: "komin",
    // Batch BV: own-voice narration. Per-job state only — it is
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
state.recapTtsEngine = "voxcpm";

// ── Part A: Cold-Open Splash Controller (tap + locale audio + strict READY gate) ──
const __vcpSplashState = {
    mountedAt: Date.now(),
    minOnScreenMs: 400,
    fadeMs: 300,
    postTapSafetyMs: 10000,
    clipFailFadeDelayMs: 350,
    clipStallSafetyMs: 15000,
    profileApplied: false,
    initialTabRendered: false,
    bootReady: false,
    tapped: false,
    clipDone: false,
    safetyFired: false,
    hiding: false,
    hidden: false,
    preloadedLocale: null,
    preloadedAudio: null,
    activeAudio: null,
    safetyTimer: null,
    failTimer: null,
    clipTimeoutTimer: null,
    minDelayTimer: null,
    fadeTimer: null,
    wired: false,
};

function resolveVcpSplashLocale() {
    try {
        if (typeof currentLang !== "undefined" && (currentLang === "en" || currentLang === "my")) {
            return currentLang;
        }
        if (typeof localStorage !== "undefined") {
            return localStorage.getItem("studio_lang") === "en" ? "en" : "my";
        }
    } catch (e) {}
    return "my";
}

function preloadVcpSplashGreeting(locale) {
    const loc = locale === "en" ? "en" : "my";
    try {
        if (typeof document !== "undefined" && document.documentElement) {
            document.documentElement.setAttribute("data-lang", loc);
        }
    } catch (e) {}
    if (__vcpSplashState.tapped || __vcpSplashState.hidden) return;
    if (__vcpSplashState.preloadedLocale === loc && __vcpSplashState.preloadedAudio) return;
    __vcpSplashState.preloadedLocale = loc;
    const src = `/static/audio/welcome_${loc}.mp3`;
    try {
        if (typeof Audio !== "undefined") {
            const a = new Audio(src);
            a.preload = "auto";
            if (typeof a.load === "function") a.load();
            __vcpSplashState.preloadedAudio = a;
        }
    } catch (e) {
        __vcpSplashState.preloadedAudio = null;
    }
}

function flushDeferredGateAndBanner() {
    try {
        if (typeof entryGate !== "undefined" && entryGate.locked) {
            const overlay = document.getElementById("gate-lock");
            if (overlay) overlay.classList.remove("hidden");
            document.body?.classList.add("gate-locked");
        }
        if (state._pendingGateBannerData && typeof updateGateBanner === "function") {
            const pending = state._pendingGateBannerData;
            state._pendingGateBannerData = null;
            updateGateBanner(pending);
        }
        if (state._pendingGateReminder && typeof showGateReminder === "function") {
            state._pendingGateReminder = false;
            showGateReminder();
        }
        if (state._pendingOpenViaTelegram && typeof showOpenViaTelegramModal === "function") {
            state._pendingOpenViaTelegram = false;
            showOpenViaTelegramModal();
        }
    } catch (e) {}
}

function isSplashBlockingOverlays() {
    if (typeof document === "undefined") return false;
    const el = document.getElementById("vcp-splash");
    if (!el && !document.body?.hasAttribute("data-splash-active")) return false;
    return !(__vcpSplashState.hidden && __vcpSplashState.bootReady);
}

function attemptVcpSplashHide() {
    const s = __vcpSplashState;
    if (s.hidden || s.hiding) return;
    // Strict rule: untapped splash NEVER hides, even after READY.
    if (!s.tapped || !s.clipDone) return;
    if (!s.bootReady && !s.safetyFired) return;

    const elapsed = Date.now() - s.mountedAt;
    const waitMs = Math.max(0, s.minOnScreenMs - elapsed);
    if (waitMs > 0) {
        if (s.minDelayTimer) clearTimeout(s.minDelayTimer);
        s.minDelayTimer = setTimeout(() => {
            s.minDelayTimer = null;
            attemptVcpSplashHide();
        }, waitMs);
        return;
    }

    s.hiding = true;
    if (s.safetyTimer) { clearTimeout(s.safetyTimer); s.safetyTimer = null; }
    if (s.clipTimeoutTimer) { clearTimeout(s.clipTimeoutTimer); s.clipTimeoutTimer = null; }
    if (s.failTimer) { clearTimeout(s.failTimer); s.failTimer = null; }

    const el = typeof document !== "undefined" ? document.getElementById("vcp-splash") : null;
    if (el) el.classList.add("vcp-splash-hiding");

    s.fadeTimer = setTimeout(() => {
        s.fadeTimer = null;
        s.hidden = true;
        if (el && typeof el.remove === "function") el.remove();
        else if (el && el.parentNode) el.parentNode.removeChild(el);
        try { document.body?.removeAttribute("data-splash-active"); } catch (e) {}
        flushDeferredGateAndBanner();
    }, s.fadeMs);
}

function onVcpSplashClipFinished() {
    const s = __vcpSplashState;
    if (s.clipDone) return;
    s.clipDone = true;
    if (s.clipTimeoutTimer) { clearTimeout(s.clipTimeoutTimer); s.clipTimeoutTimer = null; }
    if (s.failTimer) { clearTimeout(s.failTimer); s.failTimer = null; }
    if (s.bootReady) {
        attemptVcpSplashHide();
    } else if (!s.safetyTimer) {
        // Post-tap safety valve ONLY: if READY still has not landed 10s after the greeting ends, fade anyway.
        s.safetyTimer = setTimeout(() => {
            s.safetyTimer = null;
            s.safetyFired = true;
            attemptVcpSplashHide();
        }, s.postTapSafetyMs);
    }
}

function handleVcpSplashTap() {
    const s = __vcpSplashState;
    if (s.tapped || s.hidden || s.hiding) return;
    s.tapped = true;
    const el = typeof document !== "undefined" ? document.getElementById("vcp-splash") : null;
    if (el) el.classList.add("vcp-splash-starting");

    const loc = resolveVcpSplashLocale();
    const src = `/static/audio/welcome_${loc}.mp3`;
    let audio = (s.preloadedAudio && s.preloadedLocale === loc) ? s.preloadedAudio : null;
    if (!audio && typeof Audio !== "undefined") {
        try { audio = new Audio(src); } catch (e) { audio = null; }
    }
    s.activeAudio = audio;

    if (!audio || typeof audio.play !== "function") {
        s.failTimer = setTimeout(onVcpSplashClipFinished, s.clipFailFadeDelayMs);
        return;
    }

    const triggerFailFade = () => {
        if (!s.clipDone && !s.failTimer) {
            if (s.clipTimeoutTimer) { clearTimeout(s.clipTimeoutTimer); s.clipTimeoutTimer = null; }
            s.failTimer = setTimeout(onVcpSplashClipFinished, s.clipFailFadeDelayMs);
        }
    };

    audio.onended = () => onVcpSplashClipFinished();
    audio.onerror = () => triggerFailFade();
    s.clipTimeoutTimer = setTimeout(onVcpSplashClipFinished, s.clipStallSafetyMs);

    try {
        const playRes = audio.play();
        if (playRes && typeof playRes.catch === "function") {
            playRes.catch(() => triggerFailFade());
        }
    } catch (err) {
        triggerFailFade();
    }
}

function syncVcpSplashReady() {
    const s = __vcpSplashState;
    if (s.profileApplied && s.initialTabRendered) {
        s.bootReady = true;
        if (s.tapped && s.clipDone) {
            attemptVcpSplashHide();
        }
    }
}

function initVcpSplash() {
    const s = __vcpSplashState;
    const el = typeof document !== "undefined" ? document.getElementById("vcp-splash") : null;
    if (!el) {
        s.hidden = true;
        return;
    }
    if (!s.wired) {
        s.wired = true;
        el.addEventListener("click", handleVcpSplashTap);
        el.addEventListener("keydown", (e) => {
            if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                handleVcpSplashTap();
            }
        });
    }
    preloadVcpSplashGreeting(resolveVcpSplashLocale());
}

window.__vcpSplash = {
    state: __vcpSplashState,
    init: initVcpSplash,
    tap: handleVcpSplashTap,
    onLocaleChanged: preloadVcpSplashGreeting,
    notifyProfileApplied() {
        __vcpSplashState.profileApplied = true;
        syncVcpSplashReady();
    },
    notifyInitialTabRendered() {
        __vcpSplashState.initialTabRendered = true;
        syncVcpSplashReady();
    },
    markBootReady() {
        __vcpSplashState.profileApplied = true;
        __vcpSplashState.initialTabRendered = true;
        syncVcpSplashReady();
    },
    isReady() {
        return __vcpSplashState.bootReady;
    },
    isHidden() {
        return __vcpSplashState.hidden;
    },
    isReadyAndHidden() {
        return __vcpSplashState.bootReady && __vcpSplashState.hidden;
    },
    isBlockingOverlays: isSplashBlockingOverlays,
};
if (typeof document !== "undefined" && document.getElementById("vcp-splash")) {
    initVcpSplash();
}

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

function closeHeaderPopovers() {
    const moreMenu = document.getElementById("header-more-menu");
    const moreBtn = document.getElementById("btn-header-more");
    if (moreMenu) moreMenu.classList.add("hidden");
    if (moreBtn) moreBtn.setAttribute("aria-expanded", "false");
}
function toggleHeaderMoreMenu(ev) {
    if (ev) ev.stopPropagation();
    haptic("light");
    const menu = document.getElementById("header-more-menu");
    const btn = document.getElementById("btn-header-more");
    if (!menu) return;
    const isOpen = !menu.classList.toggle("hidden");
    if (btn) btn.setAttribute("aria-expanded", isOpen ? "true" : "false");
}
window.closeHeaderPopovers = closeHeaderPopovers;
window.toggleHeaderMoreMenu = toggleHeaderMoreMenu;
if (typeof document !== "undefined" && !window.__vcpHeaderOutsideWired) {
    window.__vcpHeaderOutsideWired = true;
    document.addEventListener("click", (e) => {
        const tgt = e && e.target;
        if (!tgt) return;
        if (tgt.closest && tgt.closest("#btn-top-history, #btn-tutorial-top, #btn-lang-toggle")) {
            closeHeaderPopovers();
            return;
        }
        if (tgt.closest && !tgt.closest(".top-actions")) {
            closeHeaderPopovers();
        }
    });
}

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
    if (activeTabId === "tab-recap-job") {
        if (typeof window.backToStudio === "function") {
            window.backToStudio("recap");
            syncTelegramBackButton();
            return;
        }
    }
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
const FETCH_TIMEOUT_MS = 12000;
// Telegram sends are different: the server uploads up to 50 MB to
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
        const cls = completeAll || (activeIndex >= 0 && i < activeIndex)
            ? "done"
            : (i === activeIndex ? "active" : "upcoming");
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
const RECAP_STAGE_KEY_TO_STEP = {
    download: 1,
    transcribe: 2,
    context: 2,
    translate: 2,
    plan: 2,
    script_ready: 2,
    tts: 3,
    sync: 4,
    png: 4,
    overlay: 4,
    mux: 4,
    finalize: 4,
    split: 4,
};
function resolveRecapStageIndex(job) {
    if (!job) return 0;
    if (job.status === "SUCCESS") return 4;
    const stageKey = String(job.progress_stage || "").trim().toLowerCase();
    if (stageKey && Object.prototype.hasOwnProperty.call(RECAP_STAGE_KEY_TO_STEP, stageKey)) {
        return RECAP_STAGE_KEY_TO_STEP[stageKey];
    }
    if (job.status === "SCRIPT_READY") return 2;
    if (job.status === "WAITING_CHOICE") return 3;
    const rawIdx = parseInt(String(job.stage_index || 0), 10) || 0;
    if (rawIdx >= 1) return Math.min(4, rawIdx);
    return 0;
}
window.resolveRecapStageIndex = resolveRecapStageIndex;
function setRecapStage(stepStr, status, progressStage) {
    const el = document.getElementById("recap-stage-stepper");
    if (!el) return;
    if (status === "SUCCESS") { renderStageStepper(el, recapStageLabels(), 3, true); return; }
    const stageKey = String(progressStage || "").trim().toLowerCase();
    let n = (stageKey && Object.prototype.hasOwnProperty.call(RECAP_STAGE_KEY_TO_STEP, stageKey))
        ? RECAP_STAGE_KEY_TO_STEP[stageKey]
        : 0;
    if (!n) {
        if (status === "SCRIPT_READY") n = 2;
        else if (status === "WAITING_CHOICE") n = 3;
        else n = parseInt(String(stepStr || "0").split("/")[0], 10) || 0;
    }
    if (n >= 4) renderStageStepper(el, recapStageLabels(), 3, false);
    else if (n === 3) renderStageStepper(el, recapStageLabels(), 2, false);
    else if (n === 2) renderStageStepper(el, recapStageLabels(), 1, false);
    else if (n === 1) renderStageStepper(el, recapStageLabels(), 0, false);
    else renderStageStepper(el, recapStageLabels(), -1, false);
}
window.setVoiceStage = setVoiceStage;
window.setRecapStage = setRecapStage;

// ── Sticky create CTAs (premium polish) ────────────────────────────────
const STICKY_CTA_PAIRS = [
    { tab: "tab-recap", bar: "sticky-cta-recap", src: "btn-start-recap", btn: "btn-start-recap-sticky" },
];
const stickyCtaInView = {};
// Batch AK (Addition C, owner's direct order 2026-11-05): while the
function recapJobActive() {
    try {
        if (LiveJobs.activeJobs().some(j => j.kind === "recap")) return true;
        if (typeof state !== "undefined" && state && state.recapJobId && LiveJobs.jobs) {
            const tracked = LiveJobs.jobs.get(state.recapJobId);
            if (tracked && liveJobsIsTerminal(tracked.status)) return false;
        }
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
        if (p.src === "btn-start-recap") {
            src.classList.toggle("hidden", recapBlocked);
            const srcSpan = src.querySelector(".btn-text");
            const stickySpan = btn.querySelector(".btn-text");
            if (srcSpan && stickySpan) stickySpan.innerText = srcSpan.innerText;
        }
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
        if (LiveJobs.started && liveJobsIsActive(merged.status) && !prev) {
            LiveJobs.schedule();
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
        if (typeof syncVoiceStateFromLiveJobs === "function") syncVoiceStateFromLiveJobs();
        if (typeof syncTtsStateFromLiveJobs === "function") syncTtsStateFromLiveJobs();
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
    if (job.status === "WAITING_CHOICE") {
        return job.choice === "wait"
            ? t("live_job_status_waiting_capacity")
            : t("live_job_status_waiting_choice");
    }
    if (job.progress_stage) {
        const stageText = t("recap_stage_" + job.progress_stage);
        if (stageText && stageText !== "recap_stage_" + job.progress_stage) return stageText;
    }
    if (job.progress_label) return job.progress_label;
    if (job.kind === "voice") return t("voice_synthesizing");
    return t("recap_processing_msg");
}
// Batch CB Item 4: the overrun status sentence — composed from the
function liveJobOverrunText(job) {
    if (!job) return "";
    const stage = liveJobStageText(job);
    const elapsed = (typeof job.elapsed_seconds === "number" && job.elapsed_seconds > 0)
        ? formatRecapQuoteDuration(job.elapsed_seconds) : "";
    return tf("recap_eta_overrun", { stage, elapsed });
}
// Batch AK (Addition C): the status bar can be dismissed; the
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
            const tracked = state.recapJobId ? LiveJobs.jobs.get(state.recapJobId) : null;
            if (tracked && liveJobsIsTerminal(tracked.status)) {
                try {
                    if (localStorage.getItem("vcp_active_recap_job_id") === state.recapJobId) {
                        localStorage.removeItem("vcp_active_recap_job_id");
                    }
                } catch (e) {}
                if (tracked.status === "FAILED" || tracked.status === "CANCELLED") {
                    if (typeof recapSubmitGuard !== "undefined") recapSubmitGuard.submittedFingerprint = null;
                }
            }
        }
        if (typeof setSubmitButtonState === "function") setSubmitButtonState("recap");
        if (typeof refreshStickyCtas === "function") refreshStickyCtas();
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
    if (isWaiting) {
        const cancelLbl = job.tier === "free" ? t("dp_btn_cancel_free") : t("dp_btn_cancel");
        if (job.choice !== "wait") {
            action = `<button type="button" class="mini-chip" onclick="submitLiveJobVoiceChoice('${job.id}','wait')">${t("dp_btn_wait")}</button> `;
        }
        if (isRecap) {
            action += `<button type="button" class="mini-chip" onclick="submitLiveJobVoiceChoice('${job.id}','edge_all')">${t("dp_btn_edge_all")}</button> `;
        }
        action += `<button type="button" class="mini-chip" onclick="submitLiveJobVoiceChoice('${job.id}','cancel')">${cancelLbl}</button>`;
    } else if (liveJobsIsActive(job.status)) {
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
async function submitLiveJobVoiceChoice(jobId, choice) {
    if (!jobId || !choice) return;
    haptic("selection");
    try {
        const res = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/voice-choice`, {
            method: "POST",
            headers: getAuthHeaders(),
            body: JSON.stringify({ choice }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            showStudioToast(data.detail || t("dp_choice_not_parked"), "error", 4500);
            return;
        }
        showStudioToast(data.message || t("dp_choice_wait_answer"), "info", 4000);
        if (typeof loadUserProfile === "function") loadUserProfile();
        LiveJobs.fetchNow();
    } catch (e) {
        showStudioToast(t("dp_choice_not_parked"), "error", 4000);
    }
}
window.submitLiveJobVoiceChoice = submitLiveJobVoiceChoice;

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
                if (!res.ok) {
                    if (res.status === 409) {
                        // Round 7 Item 2: job already ended on backend — reconcile
                        // LiveJobs immediately so stale detail/dialog/pill finalize.
                        LiveJobs.fetchNow();
                    }
                    throw new Error(data.detail || t("err_generic"));
                }
                showStudioToast(t("voice_cancelled_toast"), "info", 5000);
                if (kind !== "tts") {
                    state._voiceTerminalHandledJobId = jobId;
                    if (typeof voiceSubmitGuard !== "undefined") voiceSubmitGuard.submittedFingerprint = null;
                    if (typeof syncVoiceDetailTerminalState === "function") {
                        syncVoiceDetailTerminalState({ id: jobId, kind: "voice", status: "CANCELLED", progress_percent: 0 });
                    }
                    LiveJobs.track({ id: jobId, kind: "voice", status: "CANCELLED", progress_percent: 0 });
                }
                if (state.activeJobId === jobId) {
                    if (state.pollTimer) clearInterval(state.pollTimer);
                    state.pollTimer = null;
                    state["voiceDetailOpen"] = false;
                    const box = document.getElementById("voice-progress-box");
                    if (box) box.classList.add("hidden");
                    const cancelBtn = document.getElementById("btn-cancel-voice-job");
                    if (cancelBtn) { cancelBtn.classList.add("hidden"); cancelBtn.disabled = true; }
                    const btn = document.getElementById("btn-generate-voice");
                    if (btn) btn.disabled = false;
                    if (typeof setSubmitButtonState === "function") setSubmitButtonState("voice");
                }
                if (kind === "tts" || (typeof ttsState !== "undefined" && ttsState.jobId === jobId)) {
                    const cancelledTts = { id: jobId, kind: "tts", status: "CANCELLED", progress_percent: 0 };
                    if (typeof syncTtsTerminalState === "function") syncTtsTerminalState(cancelledTts);
                    LiveJobs.track(cancelledTts);
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
        if (job && job.status === "SUCCESS") {
            switchTab("tab-recap");
            openRecapInPlayer(jobId);
        } else if (job && liveJobsIsTerminal(job.status)) {
            openHistoryHub();
        } else {
            openCurrentRecapDetail(jobId);
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
state.voiceDetailOpen = false;
state.recapDetailOpen = false;
function showStudioInlineStatus(kind, text) {
    // Round 7 Item 2 + Round 15 Part B: Voice and Recap progress live in their
    // dedicated detail views and Active Jobs sheet only — never render an inline
    // status strip inside either studio.
    if (kind === "voice" || kind === "recap") return;
    const line = document.getElementById(kind + "-inline-status");
    const label = document.getElementById(kind + "-inline-status-text");
    if (label && text) label.innerText = text;
    if (line) line.classList.remove("hidden");
}
window.showStudioInlineStatus = showStudioInlineStatus;
function openVoiceDetail(jobId) {
    const voiceTab = document.getElementById("tab-voice");
    if (voiceTab && !voiceTab.classList.contains("active")) switchTab("tab-voice");
    const targetId = jobId || state.activeJobId;
    const tracked = (targetId && typeof LiveJobs !== "undefined" && LiveJobs.jobs)
        ? LiveJobs.jobs.get(targetId)
        : null;
    if (tracked && typeof liveJobsIsTerminal === "function" && liveJobsIsTerminal(tracked.status)) {
        if (typeof syncVoiceDetailTerminalState === "function") syncVoiceDetailTerminalState(tracked);
        if (tracked.status === "SUCCESS") {
            state.voiceDetailOpen = false;
            const box = document.getElementById("voice-progress-box");
            if (box) box.classList.add("hidden");
            const resultBox = document.getElementById("voice-result-box");
            if (state._voiceResultRenderedJobId === targetId && resultBox && !resultBox.classList.contains("hidden")) {
                resultBox.scrollIntoView({ behavior: "smooth" });
            } else if (targetId && typeof loadCompletedVoiceJobResult === "function") {
                loadCompletedVoiceJobResult(targetId, { scroll: true });
            }
            if (typeof syncTelegramBackButton === "function") syncTelegramBackButton();
            return;
        }
        state.voiceDetailOpen = true;
        const box = document.getElementById("voice-progress-box");
        if (box) {
            box.classList.remove("hidden");
            box.scrollIntoView({ behavior: "smooth" });
        }
        if (typeof syncTelegramBackButton === "function") syncTelegramBackButton();
        return;
    }
    state.voiceDetailOpen = true;
    const box = document.getElementById("voice-progress-box");
    if (box) {
        box.classList.remove("hidden");
        box.scrollIntoView({ behavior: "smooth" });
    }
    if (targetId && !state.pollTimer) startJobPolling(targetId);
    if (typeof syncTelegramBackButton === "function") syncTelegramBackButton();
}
window.openVoiceDetail = openVoiceDetail;
function openRecapDetail(jobId) {
    const activePane = (typeof document !== "undefined" && typeof document.querySelector === "function")
        ? document.querySelector(".tab-pane.active")
        : null;
    if (activePane && activePane.id && activePane.id !== "tab-recap-job") {
        state.recapJobReturnTab = activePane.id;
    } else if (!state.recapJobReturnTab || state.recapJobReturnTab === "tab-recap-job") {
        state.recapJobReturnTab = "tab-home";
    }
    const fallbackActive = (typeof LiveJobs !== "undefined" && LiveJobs && typeof LiveJobs.activeJobs === "function")
        ? LiveJobs.activeJobs().find(j => j.kind === "recap")
        : null;
    const targetId = jobId || state.recapJobId || (fallbackActive && fallbackActive.id) || null;
    if (targetId) {
        state.recapJobId = targetId;
        if (typeof recapFormJobId !== "undefined") recapFormJobId = targetId;
    }
    if (typeof recapFormNotice !== "undefined") recapFormNotice = null;
    state.recapDetailOpen = true;
    if (typeof switchTab === "function") {
        switchTab("tab-recap-job");
    }
    const box = document.getElementById("recap-progress-box");
    if (box) {
        box.classList.remove("hidden");
    }
    if (typeof ensureRecapFormStoreSubscription === "function") {
        ensureRecapFormStoreSubscription();
    }
    const tracked = (targetId && typeof LiveJobs !== "undefined" && LiveJobs && LiveJobs.jobs)
        ? LiveJobs.jobs.get(targetId)
        : null;
    if (typeof renderRecapFormProgress === "function") {
        if (tracked) {
            renderRecapFormProgress(tracked);
        } else {
            renderRecapFormProgress({
                id: targetId || recapFormJobId || "",
                kind: "recap",
                status: "QUEUED",
                stage_index: 0,
                stage_total: 4,
                progress_percent: 0,
            });
        }
    }
    if (targetId && !state.recapPollTimer && typeof startRecapJobPolling === "function") {
        startRecapJobPolling(targetId);
    }
    if (typeof syncTelegramBackButton === "function") syncTelegramBackButton();
}
window.openRecapDetail = openRecapDetail;
window.openCurrentVoiceDetail = function() { openVoiceDetail(state.activeJobId); };
window.openCurrentRecapDetail = function(jobId) {
    const fallbackActive = (typeof LiveJobs !== "undefined" && LiveJobs && typeof LiveJobs.activeJobs === "function")
        ? LiveJobs.activeJobs().find(j => j.kind === "recap")
        : null;
    openRecapDetail(jobId || state.recapJobId || (fallbackActive && fallbackActive.id) || null);
};
window.backToStudio = function(kind) {
    if (kind === "recap") {
        state.recapDetailOpen = false;
        const box = document.getElementById("recap-progress-box");
        if (box) box.classList.add("hidden");
        const returnTab = (state.recapJobReturnTab && state.recapJobReturnTab !== "tab-recap-job")
            ? state.recapJobReturnTab
            : "tab-recap";
        if (typeof switchTab === "function") {
            switchTab(returnTab);
        } else {
            const tab = document.getElementById(returnTab) || document.getElementById("tab-recap");
            if (tab && typeof tab.scrollIntoView === "function") tab.scrollIntoView({ behavior: "smooth" });
        }
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
    showStudioToast(t("live_submit_limit_title") + " — " + t("live_submit_limit_body"), "warning", 6000);
    showStudioInlineStatus(kind, t("live_submit_limit_title"));
    openLiveJobsSheet();
}
window.showLiveSubmitLimit = showLiveSubmitLimit;

// ── Batch AC: duplicate-submit guard + confirmation hand-off ──────────
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
            savedVoice: state.savedCustomVoiceActiveId || null,
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
    const isCreating = !!(guard.submittedFingerprint || activeBlocked);
    btn.disabled = !!(guard.inFlight || isCreating || gateBlocked);
    if (span) {
        if (isCreating) {
            span.innerText = t("submit_already_creating");
        } else {
            const key = span.getAttribute ? span.getAttribute("data-i18n") : null;
            span.innerText = (key && typeof t === "function") ? t(key) : (span.dataset.acOriginal || span.innerText);
        }
    }
    // Round 7 Item 1: when the button shows the busy creating label, suppress
    // the pre-submit minute-estimate badge so the rendered label is solely
    // t("submit_already_creating").
    const costBadge = btn.querySelector ? btn.querySelector(".vcp-btn-est-cost") : null;
    if (costBadge) {
        if (isCreating) {
            if (costBadge.textContent && costBadge.dataset && !costBadge.dataset.estText) {
                costBadge.dataset.estText = costBadge.textContent;
            }
            costBadge.textContent = "";
            if (costBadge.classList && costBadge.classList.toggle) costBadge.classList.toggle("hidden", true);
        } else {
            if (costBadge.dataset && typeof costBadge.dataset.estText === "string") {
                costBadge.textContent = costBadge.dataset.estText;
            }
            if (costBadge.classList && costBadge.classList.toggle) costBadge.classList.toggle("hidden", false);
        }
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
    LiveJobs.subscribe(() => {
        setSubmitButtonState("voice");
        setSubmitButtonState("recap");
        setSubmitButtonState("tts");
    });
}

// The confirmation itself: success indication, the cost actually
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
    const card = document.getElementById("submit-confirm");
    if (card) card.classList.add("hidden");
    state.submitConfirmJob = null;
};
LiveJobs.subscribe((job) => {
    const current = state.submitConfirmJob;
    if (current && job && job.id === current.id) renderSubmitConfirmSnippet(job);
    if (job && job.kind === "voice" && liveJobsIsTerminal(job.status)) {
        syncVoiceStateFromLiveJobs(job);
    }
    if (job && job.kind === "tts" && liveJobsIsTerminal(job.status)) {
        syncTtsStateFromLiveJobs(job);
    }
});

// ── Round 7 Item 2: Voice detail view terminal-state sync (reusing LiveJobs) ──
function syncVoiceDetailTerminalState(job) {
    if (!job || !liveJobsIsTerminal(job.status)) return;
    const isCurrent = !state.activeJobId || state.activeJobId === job.id;
    if (isCurrent && state.pollTimer) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
    }
    if (!isCurrent) return;
    const spinner = document.getElementById("voice-spinner");
    const statusMsg = document.getElementById("voice-status-msg");
    const progressFill = document.getElementById("voice-progress-fill");
    const etaText = document.getElementById("voice-eta-text");
    const cancelBtn = document.getElementById("btn-cancel-voice-job");
    if (spinner) {
        spinner.innerText = job.status === "SUCCESS" ? "✅" : (job.status === "FAILED" ? "❌" : "⏹️");
        if (spinner.classList) spinner.classList.remove("spinner-wave");
    }
    if (statusMsg) statusMsg.innerText = liveJobStageText(job);
    if (job.status === "SUCCESS" && typeof setVoiceStage === "function") setVoiceStage("success");
    if (progressFill) paintProgressBarFill(progressFill, job);
    if (etaText) etaText.innerText = "";
    if (cancelBtn) {
        cancelBtn.classList.add("hidden");
        cancelBtn.disabled = true;
    }
    if (typeof voiceSubmitGuard !== "undefined") voiceSubmitGuard.submittedFingerprint = null;
    if (typeof setSubmitButtonState === "function") setSubmitButtonState("voice");
}
window.syncVoiceDetailTerminalState = syncVoiceDetailTerminalState;

function renderVoiceJobResultCard(jobId, resData, opts) {
    const resultBox = document.getElementById("voice-result-box");
    const audioPlayer = document.getElementById("result-audio-player");
    const btnDl = document.getElementById("btn-download-audio");
    const btnTg = document.getElementById("btn-voice-send-telegram");
    state.activeJobId = jobId;
    state._voiceResultRenderedJobId = jobId;
    if (typeof renderVoiceFallbackNote === "function") renderVoiceFallbackNote(resData);
    if (resultBox) {
        resultBox.classList.remove("hidden");
        if (opts && opts.scroll) resultBox.scrollIntoView({ behavior: "smooth" });
    }
    if (state.selectedReferenceFile) {
        if (typeof setCustomCloneStep === "function") setCustomCloneStep(4);
        const saveBtn = document.getElementById("btn-save-custom-voice-from-result");
        if (saveBtn) saveBtn.classList.remove("hidden");
    }
    if (btnTg) {
        btnTg.disabled = false;
        btnTg.classList.remove("sent");
        btnTg.innerHTML = `<span class="btn-icon">📱</span> <span>${t("btn_send_voice_telegram")}</span>`;
    }
    if (typeof applyTelegramSendable === "function") applyTelegramSendable("voice", resData && resData.telegram_sendable);
    if (audioPlayer && resData && resData.output_audio_url && typeof setAuthMediaSource === "function") {
        setAuthMediaSource(audioPlayer, resData.output_audio_url).then((ok) => {
            if (ok) { audioPlayer.load(); audioPlayer.play().catch(() => {}); }
        });
    }
    if (btnDl && resData && resData.output_audio_url) {
        btnDl.setAttribute("data-dl-url", resData.output_audio_url);
        btnDl.setAttribute("data-dl-name", brandedDlName("Voice", "wav", null, jobId));
    }
}

async function loadCompletedVoiceJobResult(jobId, opts) {
    if (!jobId) return;
    try {
        const res = await fetch(`/api/jobs/${jobId}/status`, { headers: getAuthHeaders() });
        if (!res.ok) return;
        const resData = await res.json();
        if (resData.status === "SUCCESS") {
            const box = document.getElementById("voice-progress-box");
            if (box) box.classList.add("hidden");
            state.voiceDetailOpen = false;
            renderVoiceJobResultCard(jobId, resData, opts);
        }
    } catch (e) {}
}

function syncVoiceStateFromLiveJobs(changedJob) {
    try {
        const targetId = state.activeJobId || (changedJob && changedJob.kind === "voice" ? changedJob.id : null);
        if (!targetId || typeof LiveJobs === "undefined" || !LiveJobs.jobs) return;
        const tracked = LiveJobs.jobs.get(targetId);
        if (!tracked || tracked.kind !== "voice" || !liveJobsIsTerminal(tracked.status)) return;
        syncVoiceDetailTerminalState(tracked);
        if (state._voiceTerminalHandledJobId !== tracked.id) {
            state._voiceTerminalHandledJobId = tracked.id;
            const box = document.getElementById("voice-progress-box");
            if (box) box.classList.add("hidden");
            state.voiceDetailOpen = false;
            if (tracked.status === "SUCCESS") {
                if (state._voiceResultRenderedJobId !== tracked.id) {
                    loadCompletedVoiceJobResult(tracked.id, { scroll: false });
                }
            } else if (tracked.status === "FAILED") {
                fetch(`/api/jobs/${tracked.id}/status`, { headers: getAuthHeaders() })
                    .then(r => r.ok ? r.json() : null)
                    .then(resData => {
                        if (!resData) return;
                        const errText = mapErrorToToast(resData.error_message, "voice");
                        const fMsg = document.getElementById("voice-failed-msg");
                        if (fMsg) fMsg.textContent = errText;
                        const fBox = document.getElementById("voice-failed-box");
                        if (fBox) fBox.classList.remove("hidden");
                    })
                    .catch(() => {});
            }
            if (typeof loadUserProfile === "function") loadUserProfile();
            if (typeof loadUserHistory === "function") loadUserHistory();
        }
    } catch (e) {}
}
window.syncVoiceStateFromLiveJobs = syncVoiceStateFromLiveJobs;

function _resetTtsSubmitControls() {
    if (typeof ttsSubmitGuard !== "undefined") {
        ttsSubmitGuard.inFlight = false;
        ttsSubmitGuard.submittedFingerprint = null;
        ttsSubmitGuard._fp = null;
    }
    const btn = document.getElementById("btn-generate-tts");
    if (btn) btn.disabled = false;
    const tInp = document.getElementById("tts-text-input");
    if (tInp && typeof syncTtsExcessAndCost === "function") {
        const u = (typeof state !== "undefined" && state && state.user) || {};
        const maxC = u.character_limit || (u.is_vip ? (u.character_limit_purchased ?? 15000) : (u.character_limit_free ?? 3000));
        syncTtsExcessAndCost((tInp.value || "").length, maxC);
    }
    if (typeof setSubmitButtonState === "function") setSubmitButtonState("tts");
}

function syncTtsTerminalState(job) {
    if (!job || !liveJobsIsTerminal(job.status)) return;
    const isCurrent = typeof ttsState === "undefined" || !ttsState.jobId || ttsState.jobId === job.id;
    if (isCurrent && typeof ttsState !== "undefined" && ttsState.pollTimer) {
        clearInterval(ttsState.pollTimer);
        ttsState.pollTimer = null;
    }
    if (!isCurrent) return;
    try { localStorage.removeItem("vcp_active_tts_job_id"); } catch (e) {}
    const progressBox = document.getElementById("tts-progress-box");
    if (progressBox) progressBox.classList.add("hidden");
    const cancelBtn = document.getElementById("btn-cancel-tts-job");
    if (cancelBtn) cancelBtn.classList.add("hidden");
    _resetTtsSubmitControls();
}
window.syncTtsTerminalState = syncTtsTerminalState;

function syncTtsStateFromLiveJobs(changedJob) {
    try {
        const targetId = (typeof ttsState !== "undefined" && ttsState.jobId) || (changedJob && changedJob.kind === "tts" ? changedJob.id : null);
        if (!targetId || typeof LiveJobs === "undefined" || !LiveJobs.jobs) return;
        const tracked = LiveJobs.jobs.get(targetId);
        if (!tracked || tracked.kind !== "tts" || !liveJobsIsTerminal(tracked.status)) return;
        _resetTtsSubmitControls();
    } catch (e) {}
}
window.syncTtsStateFromLiveJobs = syncTtsStateFromLiveJobs;

// History cards for tracked jobs update IN PLACE: the cached row's
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
const USER_PREFS_CACHE_KEY = "vcp_user_prefs_cache";
let userPrefsSaveTimer = null;
// Batch AK: JS mirror of core.sub_centre_to_bottom_margin —
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
const RECAP_LOCKED_FILL = "blur";
const RECAP_LOCKED_PROTECTION = "strong";
const RECAP_LOCKED_MIRROR = true;
const RECAP_LOCKED_ZOOM = 1.1;
function recapLockedCropForRatio(ratio) {
    if (String(ratio || "9:16").trim().toLowerCase() === "original") {
        return { crop_top: 0, crop_bottom: 0, crop_left: 0, crop_right: 0 };
    }
    return { crop_top: 12, crop_bottom: 12, crop_left: 0, crop_right: 0 };
}
function syncLockedRecapDefaultsToState() {
    state.recapRatioFill = RECAP_LOCKED_FILL;
    state.recapProtection = RECAP_LOCKED_PROTECTION;
    state.recapMirror = RECAP_LOCKED_MIRROR;
    state.recapZoom = RECAP_LOCKED_ZOOM;
    const c = recapLockedCropForRatio(state.recapRatio);
    state.recapCropTop = c.crop_top;
    state.recapCropBottom = c.crop_bottom;
    state.recapCropLeft = c.crop_left;
    state.recapCropRight = c.crop_right;
}
window.recapLockedCropForRatio = recapLockedCropForRatio;

// Batch DD: the Step-2 rebuild's payload fields, shared by the
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
    out.protection_level = RECAP_LOCKED_PROTECTION;
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
    const lockedCrop = recapLockedCropForRatio(state.recapRatio);
    return {
        voice_persona: state.selectedPersona,
        voice_speed: normalizeSpeedOption(state.speed),
        recapRatio: state.recapRatio,
        recapRatioFill: RECAP_LOCKED_FILL,
        recapSpeed: normalizeSpeedOption(state.recapSpeed),
        recapStoryStyle: state.recapStoryStyle,
        recapSubStyle: state.recapSubStyle,
        recapSubFont: state.recapSubFont,
        recapLogoPos: state.recapLogoPos,
        recapLogoScale: state.recapLogoScale || 0.16,
        recapLogoOpacity: state.recapLogoOpacity || 0.70,
        recapSourceLanguage: state.recapSourceLanguage,
        recapRenderQuality: (state.user && state.user.is_admin) ? state.recapRenderQuality : "standard",
        recapTtsEngine: state.recapTtsEngine,
        recapSyncCrop: !!state.recapSyncCrop,
        recapMirror: RECAP_LOCKED_MIRROR,
        recapBurnSubs: !!state.recapBurnSubs,
        recapBlurSubs: !!state.recapBlurSubs,
        recapBlurAutoDetect: state.recapBlurAutoDetect !== false,
        recapLogo: !!state.recapLogo,
        recapSplitEnabled: false,
        recapCropTop: lockedCrop.crop_top,
        recapCropBottom: lockedCrop.crop_bottom,
        recapCropLeft: lockedCrop.crop_left,
        recapCropRight: lockedCrop.crop_right,
        recapFontSize: state.recapFontSize || 13,
        recapLines: state.recapLines || 1,
        recapSplitMinutes: 3,
        recapSubMargin: sy ? parseInt(sy.value) : (state.recapSubMargin != null ? state.recapSubMargin : 5),
        recapSettingsVersion: 3,
        recapSubX: sxEl ? parseInt(sxEl.value) : (state.recapSubX || 50),
        recapSubW: swEl ? parseInt(swEl.value) : (state.recapSubW || 92),
        recapProtection: RECAP_LOCKED_PROTECTION,
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
        recapZoom: RECAP_LOCKED_ZOOM,
        recapVoice: state.recapVoice,
        recapGeminiVoice: state.recapGeminiVoice || "",
        recapSubRenderMode: state.recapSubRenderMode || "timeline",
        recapSubEffect: state.recapSubEffect || "none",
        recapEmbedSoftSubs: state.recapEmbedSoftSubs !== false,
        savedCustomVoiceId: state.savedCustomVoiceId || "",
        savedCustomVoiceName: state.savedCustomVoiceName || "",
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
        "recapLines", "recapSubY", "recapBlurStrength",
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
    if (!(state.user && state.user.is_admin)) state.recapRenderQuality = "standard";
    syncLockedRecapDefaultsToState();
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
    cacheAccountPrefs(prefs);
    // Batch AM (M8ii): the recap controls were synced at boot from
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
let openViaTelegramShown = false;
function showOpenViaTelegramModal() {
    if (typeof isSplashBlockingOverlays === "function" && isSplashBlockingOverlays()) {
        state._pendingOpenViaTelegram = true;
        return;
    }
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
let gateNoticeAt = 0;
async function showGateReminder() {
    if (typeof isSplashBlockingOverlays === "function" && isSplashBlockingOverlays()) {
        state._pendingGateReminder = true;
        return;
    }
    const now = Date.now();
    if (now - gateNoticeAt < 1500) return; // one nudge per burst of calls
    gateNoticeAt = now;
    try { await loadUserProfile(); } catch (e) { /* banner refresh is best-effort */ }
    // A gated write just 403'd: re-run the LIVE entry check too — if the
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

// Auth media fetch with in-memory cache for voice samples (Persona + Edge)
const _authSampleCache = new Map();
function isCacheableVoiceSampleUrl(url) {
    return url.startsWith("/api/voices/sample/") || url.startsWith("/api/voices/edge-sample/");
}
async function fetchAuthImageUrl(url) {
    if (!url || !url.startsWith("/api/")) return url;
    if (isCacheableVoiceSampleUrl(url) && _authSampleCache.has(url)) return _authSampleCache.get(url);
    try {
        const res = await fetch(url, { headers: getAuthHeaders(false) });
        if (isAuthFailure(res) || !res.ok) return null;
        const objUrl = URL.createObjectURL(await res.blob());
        if (isCacheableVoiceSampleUrl(url)) _authSampleCache.set(url, objUrl);
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
document.addEventListener("click", (e) => {
    // Batch CK: anchors inside the admin media modal carry their own
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

// ── Refer & Support (round-6 P2; Refer card lives in Profile since Round 4) ─
// The referral programme itself lives in the bot (ledger.py); /me carries the
// user's own code/link + stats, rendered in #profile-refer-card.
let _referralLink = "";
let _supportLink = "https://t.me/VoiceCloneProAdmin";

function updateProfileReferSummary(data) {
    const src = data || window._lastReferralStats || (typeof state !== "undefined" && state && state.user);
    if (!src) return;
    const r = src.referral;
    const friendsEl = document.getElementById("profile-ref-friends");
    const minEl = document.getElementById("profile-ref-minutes");
    const linkBlock = document.getElementById("profile-ref-link-block");
    const linkText = document.getElementById("profile-referral-link-text");
    if (friendsEl) {
        const rewarded = (r && r.rewarded_referrals) || 0;
        const max = r ? r.max_referrals : null;
        friendsEl.textContent = (max && max > 0) ? `${rewarded} / ${max}` : String(rewarded);
    }
    if (minEl) {
        const earnedMin = r ? Math.round((r.earned_minutes || 0) * 10) / 10 : 0;
        minEl.textContent = typeof tf === "function"
            ? tf("home_free_value_min", { min: earnedMin })
            : String(earnedMin);
    }
    const link = src.referral_link || _referralLink || "";
    if (linkBlock && linkText) {
        if (link) {
            linkText.textContent = link;
            linkBlock.classList.remove("hidden");
        } else {
            linkText.textContent = "";
            linkBlock.classList.add("hidden");
        }
    }
}
window.updateProfileReferSummary = updateProfileReferSummary;

function updateReferUI(data) {
    if (!data) return;
    window._lastReferralStats = data;
    _referralLink = data.referral_link || "";
    if (data.support_link) _supportLink = data.support_link;
    updateProfileReferSummary(data);
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

function openSupportContact() {
    haptic("light");
    // Batch BB-6: the support destination is a Telegram ACCOUNT (the
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
    if (typeof closeHeaderPopovers === "function") closeHeaderPopovers();
    haptic("light");
    // Registry tutorials (premium polish): index (msg 38), plans,
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


// Shared helpers exposed for lazy-loaded admin.js
window.state = state;
window.escapeHtml = escapeHtml;
window.sanitizeCssMediaUrl = sanitizeCssMediaUrl;
window.admFmtYangon = admFmtYangon;
window.admFmtDate = admFmtDate;
window.openSlipModal = openSlipModal;
window.closeSlipModal = closeSlipModal;
window.setAuthMediaSource = setAuthMediaSource;
window.clearAuthMediaSource = clearAuthMediaSource;
window.brandedDlName = brandedDlName;
window.tutorialDisplayTitle = tutorialDisplayTitle;
window.refreshPlanPrices = refreshPlanPrices;
window.renderGrantsRowsHtml = renderGrantsRowsHtml;

function ensureAdminConsoleScript() {
    if (!(state && state.user && state.user.is_admin)) return Promise.resolve(false);
    if (window.__adminJsLoaded) return Promise.resolve(true);
    if (window.__adminJsPromise) return window.__adminJsPromise;
    window.__adminJsPromise = new Promise((resolve) => {
        const s = document.createElement("script");
        const appSrc = document.querySelector('script[src*="/static/js/app.js"]')?.src || "";
        const _av = (appSrc.match(/[?&]v=([^&]+)/) || [])[1] || "20261125_admin_r23";
        s.src = "/static/js/admin.js?v=" + _av;
        s.defer = true;
        s.onload = () => { window.__adminJsLoaded = true; resolve(true); };
        s.onerror = () => { window.__adminJsPromise = null; resolve(false); };
        document.head.appendChild(s);
    });
    return window.__adminJsPromise;
}
window.ensureAdminConsoleScript = ensureAdminConsoleScript;

function syncTabAdminLifecycle(tabId) {
    if (tabId === "tab-admin") {
        if (!(state && state.user && state.user.is_admin)) return;
        ensureAdminConsoleScript().then((ok) => {
            if (ok && typeof window.onAdminTabEntered === "function") {
                window.onAdminTabEntered();
            }
        });
    } else if (typeof window._clearAdminQueuePoll === "function") {
        window._clearAdminQueuePoll();
    }
}

function pauseInactiveTabMedia(activeTabId) {
    try {
        if (typeof stopCurrentVoicePreview === "function") stopCurrentVoicePreview();
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
    if (typeof closeHeaderPopovers === "function") closeHeaderPopovers();
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
    if (tabId === "tab-profile") {
        if (typeof paintProfileView === "function") paintProfileView(state.user);
        if (typeof paintTtsGeminiKeyState === "function" && typeof _cgQuotaCache !== "undefined" && _cgQuotaCache) {
            paintTtsGeminiKeyState(_cgQuotaCache);
        }
        if (typeof window.refreshTtsQuota === "function") {
            window.refreshTtsQuota();
        }
        if (typeof loadVipWalletHistory === "function") {
            loadVipWalletHistory(true);
        }
    }
    // Batch AW: the TTS tab paints its balance line fresh on entry.
    if (tabId === "tab-tts" && typeof window.refreshTtsQuota === "function") {
        window.refreshTtsQuota();
    }
    // Batch CT: the VIP tab re-syncs plan prices from the API on entry.
    if (tabId === "tab-vip" && typeof refreshPlanPrices === "function") {
        refreshPlanPrices();
    }
    syncTabAdminLifecycle(tabId);
    syncTelegramBackButton();
}
window.switchTab = switchTab;

function openProfileView() {
    haptic("light");
    switchTab("tab-profile");
}
window.openProfileView = openProfileView;

let _profilePhotoState = { userId: null, url: null, status: "idle" };

function _paintProfileAvatarImg(avatarEl, objUrl, fallbackChar) {
    if (!avatarEl || !objUrl) return;
    const existing = avatarEl.querySelector("img.profile-avatar-img");
    if (existing && existing.getAttribute("src") === objUrl) return;
    avatarEl.textContent = "";
    const img = document.createElement("img");
    img.className = "profile-avatar-img";
    img.src = objUrl;
    img.alt = "";
    img.decoding = "async";
    img.onerror = () => {
        if (avatarEl.contains(img)) avatarEl.textContent = fallbackChar || "👤";
    };
    avatarEl.appendChild(img);
}

function _maybeLoadProfilePhoto(avatarEl, userId, fallbackChar) {
    if (!avatarEl || !userId) return;
    const profilePane = document.getElementById("tab-profile");
    if (!profilePane || !profilePane.classList.contains("active")) return;
    if (_profilePhotoState.userId !== userId) {
        if (_profilePhotoState.url && typeof URL !== "undefined" && URL.revokeObjectURL) {
            try { URL.revokeObjectURL(_profilePhotoState.url); } catch (_) {}
        }
        _profilePhotoState = { userId: userId, url: null, status: "idle" };
    }
    if (_profilePhotoState.status === "loaded" && _profilePhotoState.url) {
        _paintProfileAvatarImg(avatarEl, _profilePhotoState.url, fallbackChar);
        return;
    }
    if (_profilePhotoState.status === "loading" || _profilePhotoState.status === "none") {
        return;
    }
    _profilePhotoState.status = "loading";
    Promise.resolve()
        .then(() => fetchAuthImageUrl("/api/user/me/photo"))
        .then((objUrl) => {
            if (_profilePhotoState.userId !== userId) return;
            if (!objUrl) {
                _profilePhotoState.status = "none";
                return;
            }
            _profilePhotoState.url = objUrl;
            _profilePhotoState.status = "loaded";
            const currentAvatar = document.getElementById("profile-ident-avatar");
            if (currentAvatar) _paintProfileAvatarImg(currentAvatar, objUrl, fallbackChar);
        })
        .catch(() => {
            if (_profilePhotoState.userId === userId) {
                _profilePhotoState.status = "none";
            }
        });
}

function paintProfileView(data) {
    const u = data || (typeof state !== "undefined" && state && state.user) || null;
    const tgUser = typeof state !== "undefined" && state && state.tg && state.tg.initDataUnsafe && state.tg.initDataUnsafe.user;
    const cleanName = (n) => (n && !/^voice\s*clone\s*pro$/i.test(String(n).trim())) ? String(n).trim() : "";
    const first = cleanName(u && u.first_name) || cleanName(tgUser && tgUser.first_name);
    const last = cleanName(tgUser && tgUser.last_name);
    const fullName = first ? (last && !first.includes(last) ? `${first} ${last}` : first) : "";
    const rawUsername = (u && u.username) || (tgUser && tgUser.username) || "";
    const cleanUsername = String(rawUsername).trim().replace(/^@+/, "");
    const rawId = (u && u.id != null) ? u.id : (tgUser && tgUser.id != null ? tgUser.id : null);
    const cleanId = (rawId !== null && rawId !== undefined && String(rawId).trim() !== "") ? String(rawId).trim() : "";

    const identCard = document.getElementById("profile-identity-card");
    const avatarEl = document.getElementById("profile-ident-avatar");
    const nameEl = document.getElementById("profile-ident-name");
    const userEl = document.getElementById("profile-ident-username");
    const idWrap = document.getElementById("profile-ident-id-wrap");
    const idEl = document.getElementById("profile-ident-id");

    const hasAnyIdent = Boolean(fullName || cleanUsername || cleanId);
    if (identCard) identCard.classList.toggle("hidden", !hasAnyIdent);
    if (avatarEl) {
        const ch = fullName
            ? Array.from(fullName)[0].toUpperCase()
            : (cleanUsername ? Array.from(cleanUsername)[0].toUpperCase() : "");
        const fallbackChar = ch || "👤";
        if (_profilePhotoState.userId === cleanId && _profilePhotoState.status === "loaded" && _profilePhotoState.url) {
            _paintProfileAvatarImg(avatarEl, _profilePhotoState.url, fallbackChar);
        } else {
            avatarEl.textContent = fallbackChar;
        }
        _maybeLoadProfilePhoto(avatarEl, cleanId, fallbackChar);
    }
    if (nameEl) {
        nameEl.textContent = fullName;
        nameEl.classList.toggle("hidden", !fullName);
    }
    if (userEl) {
        userEl.textContent = cleanUsername ? `@${cleanUsername}` : "";
        userEl.classList.toggle("hidden", !cleanUsername);
    }
    if (idWrap && idEl) {
        idEl.textContent = cleanId;
        idWrap.classList.toggle("hidden", !cleanId);
    }

    const expLine = document.getElementById("profile-expiry-line");
    if (expLine) {
        if (u && u.earliest_days_left != null && u.earliest_expires_at) {
            const dateStr = String(u.earliest_expires_at).slice(0, 10);
            const leftStr = tf("vip_grant_days_left", { d: u.earliest_days_left });
            expLine.textContent = "⏳ " + tf("profile_plan_expiry_line", { date: dateStr, left: leftStr });
            expLine.classList.remove("hidden");
            expLine.classList.toggle("exp-soon", !!u.expiring_soon);
        } else {
            expLine.textContent = "";
            expLine.classList.add("hidden");
            expLine.classList.remove("exp-soon");
        }
    }

    if (u) {
        paintVipPlanBalances(u);
        updateProfileReferSummary(u);
    }
}
window.paintProfileView = paintProfileView;

// ── Language Toggle (i18n) ─────────────────────────────────────────────────
async function toggleLanguage() {
    if (typeof closeHeaderPopovers === "function") closeHeaderPopovers();
    haptic("medium");
    const nextLang = currentLang === "my" ? "en" : "my";
    setLanguage(nextLang);
    try { await window.__langPersistPromise; } catch (e) {}
    const avTxt = document.getElementById("all-voices-toggle-text");
    const avWrap = document.getElementById("all-voices-dropdown-wrap");
    if (avTxt && avWrap) avTxt.innerText = avWrap.classList.contains("hidden") ? t("voices_show_all") : t("voices_hide_all");
    loadCuratedVoices();
    loadUserProfile();
    if (state && state.user && typeof paintHomeTilePeriods === "function") {
        paintHomeTilePeriods(state.user, window._lastTtsQuota || null);
    }
    if (typeof refreshPlanPrices === "function") refreshPlanPrices();
    updateRecapSplitCostHint();
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
            if (res.status === 403) {
                const errData = await res.clone().json().catch(() => null);
                if (errData && String(errData.detail || "").includes("MAINTENANCE_MODE") && typeof enterMaintenanceLock === "function") {
                    await enterMaintenanceLock();
                }
            } else {
                setHomeLoadErrorVisible(true);
            }
            return;
        }
        const data = await res.json();
        state.user = data;
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
        paintTtsTodaySurfaces(data);
        if (document.body) { document.body.removeAttribute("data-profile-loading"); document.body.classList.remove("profile-loading"); }
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

        const catTag = document.getElementById("profile-category-tag");
        const charCount = document.getElementById("char-count");
        const adminNavBtn = document.getElementById("nav-btn-admin");
        const adminTab = document.getElementById("tab-admin");

        if (vipBadge) {
            vipBadge.className = data.is_vip ? "vip-badge vip" : "vip-badge free";
            vipBadge.innerText = data.is_admin ? "👑 ADMIN" : (data.is_vip ? "👑 VIP" : "FREE");
        }

        if (catTag) {
            catTag.removeAttribute("data-i18n");
            catTag.innerText = data.is_admin ? "ADMIN ⭐" : (data.is_vip ? t("vip_member_pro") : t("vip_member_free"));
            catTag.className = data.is_vip ? "stat-val text-gold" : "stat-val";
        }
        paintVipPlanBalances(data);
        paintProfileView(data);

        if (charCount) {
            const limit = data.character_limit || (data.is_vip ? (data.character_limit_purchased ?? 15000) : (data.character_limit_free ?? 3000));
            const curLen = (document.getElementById("voice-text-input")?.value || "").length;
            charCount.innerText = `${curLen} / ${limit} ${t("char_counter_unit")}`;
        }

        updateGateBanner(data);
        refreshWelcomeStrip();
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
            ensureAdminConsoleScript();
        }

        // Auto-resume active background Recap or Voice job if user closed and reopened Mini App
        if (data.active_recap_job_id && !state.recapPollTimer) {
            state.recapJobId = data.active_recap_job_id;
            try { localStorage.setItem("vcp_active_recap_job_id", data.active_recap_job_id); } catch (e) {}
            startRecapJobPolling(data.active_recap_job_id);
        }
        if (data.active_voice_job_id && !state.pollTimer) {
            const trackedVoice = (typeof LiveJobs !== "undefined" && LiveJobs.jobs)
                ? LiveJobs.jobs.get(data.active_voice_job_id)
                : null;
            const voiceAlreadyDone = (trackedVoice && typeof liveJobsIsTerminal === "function" && liveJobsIsTerminal(trackedVoice.status))
                || state._voiceTerminalHandledJobId === data.active_voice_job_id;
            if (!voiceAlreadyDone) {
                state.activeJobId = data.active_voice_job_id;
                const vProg = document.getElementById("voice-progress-box");
                if (vProg) vProg.classList.remove("hidden");
                startJobPolling(data.active_voice_job_id);
            }
        }

    } catch (err) {
        console.warn("User profile fetch error:", err);
        setHomeLoadErrorVisible(true);
    } finally {
        if (document.body) { document.body.removeAttribute("data-profile-loading"); document.body.classList.remove("profile-loading"); }
        if (window.__vcpSplash && typeof window.__vcpSplash.notifyProfileApplied === "function") {
            window.__vcpSplash.notifyProfileApplied();
        }
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

function formatPersonaOptionLabel(v) {
    const shortNm = personaShortName(v && v.name) || (v && v.name) || "";
    const g = String((v && v.gender) || "").toLowerCase();
    const suffix = g === "female"
        ? ` (${t("persona_gender_female")})`
        : (g === "male" ? ` (${t("persona_gender_male")})` : "");
    return `${shortNm}${suffix}`;
}

function renderCuratedVoices() {
    const voices = Array.isArray(state.curatedVoices) ? state.curatedVoices : [];
    const filter = state.personaFilter || "all";
    const filtered = (filter === "all")
        ? voices
        : voices.filter(v => v.gender === filter);

    const errBox = document.getElementById("voice-persona-load-error");
    if (errBox && voices.length) {
        errBox.innerHTML = "";
        errBox.classList.add("hidden");
    }

    const voiceSel = document.getElementById("voice-persona-select");
    if (voiceSel) {
        voiceSel.innerHTML = "";
        const list = filtered.length ? filtered : voices;
        list.forEach(v => {
            const opt = document.createElement("option");
            opt.value = v.id;
            opt.textContent = formatPersonaOptionLabel(v);
            voiceSel.appendChild(opt);
        });
        const hasVal = Array.from(voiceSel.options).some(o => o.value === state.selectedPersona);
        if (hasVal) {
            voiceSel.value = state.selectedPersona;
        } else if (list.length) {
            state.selectedPersona = list[0].id;
            voiceSel.value = list[0].id;
        }
        if (!voiceSel.dataset.vcpWired) {
            voiceSel.dataset.vcpWired = "1";
            voiceSel.addEventListener("change", () => {
                haptic("selection");
                state.selectedPersona = voiceSel.value || "komin";
                updateSelectedVoiceSummary();
                setPersonaPickerOpen(false);
                if (typeof saveAccountPrefsSoon === "function") saveAccountPrefsSoon();
            });
        }
    }

    const recapSel = document.getElementById("recap-persona-select");
    if (recapSel && voices.length) {
        const byId = new Map(voices.map(v => [v.id, v]));
        if (recapSel.options.length) {
            Array.from(recapSel.options).forEach(opt => {
                const v = byId.get(opt.value);
                if (v) opt.textContent = formatPersonaOptionLabel(v);
            });
        } else {
            voices.forEach(v => {
                const opt = document.createElement("option");
                opt.value = v.id;
                opt.textContent = formatPersonaOptionLabel(v);
                recapSel.appendChild(opt);
            });
        }
        const chosenRecap = state.recapVoice || "komin";
        if (Array.from(recapSel.options).some(o => o.value === chosenRecap)) {
            recapSel.value = chosenRecap;
        }
        if (typeof resyncRecapPersonaSelect === "function") resyncRecapPersonaSelect();
        else if (typeof refreshRecapStep4Choices === "function") refreshRecapStep4Choices();
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
        const errBox = document.getElementById("voice-persona-load-error");
        if (errBox && !(state.curatedVoices || []).length) {
            errBox.innerHTML = `<div class="adm-state-error"><span data-i18n="home_load_error">${escapeHtml(t("home_load_error"))}</span> <button type="button" class="mini-chip" onclick="loadCuratedVoices()" data-i18n="admin_retry">${escapeHtml(t("admin_retry"))}</button></div>`;
            errBox.classList.remove("hidden");
        }
    }
}

// ── API: Legacy Models Loader (removed) ──
async function loadLegacyModels() {
    // Voice v2: legacy engine removed — VoxCPM2 only. Kept as a no-op stub
    return;
}

// ── Voice Preview Audio Player Controller (Part D: shared gesture-primed Audio) ──
const VCP_SILENT_WAV_DATA_URI =
    "data:audio/wav;base64,UklGRrQBAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YZABAACAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgICA";
let sharedPreviewAudio = null;
let currentVoiceAudio = null;
let currentPreviewButton = null;
let previewFetchSeq = 0;

function getSharedPreviewAudio() {
    if (!sharedPreviewAudio) {
        sharedPreviewAudio = new Audio();
        sharedPreviewAudio.preload = "auto";
        sharedPreviewAudio.playsInline = true;
    }
    return sharedPreviewAudio;
}

function primeSharedPreviewAudio() {
    const audio = getSharedPreviewAudio();
    try {
        audio.onended = null;
        audio.onerror = null;
        audio.pause();
        audio.muted = true;
        audio.src = VCP_SILENT_WAV_DATA_URI;
        const p = audio.play();
        if (p && typeof p.then === "function") {
            p.then(() => {
                if (audio.src === VCP_SILENT_WAV_DATA_URI) {
                    try { audio.pause(); } catch (_) {}
                }
            }).catch(() => {});
        }
    } catch (e) {
        console.warn("Shared preview audio prime warning:", e);
    }
    return audio;
}
window.getSharedPreviewAudio = getSharedPreviewAudio;
window.primeSharedPreviewAudio = primeSharedPreviewAudio;

function setPreviewButtonState(buttonEl, mode) {
    if (!buttonEl) return;
    buttonEl.classList.toggle("playing", mode === "playing");
    buttonEl.classList.toggle("loading", mode === "loading");
    const icon = buttonEl.querySelector(".preview-btn-icon, .preview-icon");
    const label = buttonEl.querySelector(".preview-btn-label, [data-i18n='btn_preview_voice'], [data-i18n='btn_preview_short']");
    if (mode === "loading") {
        if (icon) icon.innerText = "⏳";
        if (label) label.innerText = t("preview_loading");
    } else if (mode === "playing") {
        if (icon) icon.innerText = "⏸️";
        if (label) label.innerText = t("preview_playing");
    } else {
        if (icon) icon.innerText = buttonEl.classList.contains("mini") ? "▶️" : "🔊";
        if (label) {
            const k = label.getAttribute("data-i18n");
            label.innerText = k === "btn_preview_short"
                ? t("btn_preview_short")
                : (buttonEl.classList.contains("mini") ? t("preview_voice") : t("btn_preview_voice"));
        }
    }
}

function stopCurrentVoicePreview() {
    previewFetchSeq++; // cancel any in-flight sample fetch
    if (currentVoiceAudio) {
        currentVoiceAudio.onended = null;
        currentVoiceAudio.onerror = null;
        try { currentVoiceAudio.pause(); } catch (_) {}
        try { currentVoiceAudio.currentTime = 0; } catch (_) {}
        currentVoiceAudio = null;
    }
    if (currentPreviewButton) {
        setPreviewButtonState(currentPreviewButton, "idle");
        currentPreviewButton = null;
    }
}

async function runSharedVoicePreview(audioUrl, buttonEl, labelTag) {
    const seq = ++previewFetchSeq;
    // Synchronously prime the shared Audio element INSIDE the user gesture
    // before any await, so mobile Telegram WebViews / strict autoplay keep
    // playback unlocked even when the authenticated fetch takes 2+ seconds.
    const audio = primeSharedPreviewAudio();
    currentPreviewButton = buttonEl;
    setPreviewButtonState(buttonEl, "loading");
    try {
        const authUrl = await fetchAuthImageUrl(audioUrl);
        if (seq !== previewFetchSeq) return;
        if (!authUrl) {
            stopCurrentVoicePreview();
            showStudioToast(t("preview_load_failed"), "error");
            return;
        }
        audio.onended = () => {
            if (seq !== previewFetchSeq) return;
            stopCurrentVoicePreview();
        };
        audio.onerror = () => {
            if (seq !== previewFetchSeq) return;
            console.warn("Audio playback failed for", labelTag);
            stopCurrentVoicePreview();
            showStudioToast(t("preview_load_failed"), "error");
        };
        audio.muted = false;
        audio.src = authUrl;
        try { audio.currentTime = 0; } catch (_) {}
        audio.load();
        currentVoiceAudio = audio;
        currentPreviewButton = buttonEl;
        setPreviewButtonState(buttonEl, "playing");

        const playPromise = audio.play();
        if (playPromise && typeof playPromise.catch === "function") {
            playPromise.catch(e => {
                if (seq !== previewFetchSeq) return;
                console.warn(`${labelTag} preview playback error:`, e);
                stopCurrentVoicePreview();
                showStudioToast(t("preview_load_failed"), "error");
            });
        }
    } catch (e) {
        if (seq !== previewFetchSeq) return;
        console.warn(`${labelTag} preview handler error:`, e);
        stopCurrentVoicePreview();
        showStudioToast(t("preview_load_failed"), "error");
    }
}

async function playVoicePreview(voiceId, buttonEl) {
    try {
        haptic("light");
        if (!voiceId) {
            showStudioToast(t("preview_load_failed"), "error");
            return;
        }
        if (currentPreviewButton && currentPreviewButton === buttonEl) {
            stopCurrentVoicePreview();
            return;
        }
        stopCurrentVoicePreview();
        const audioUrl = `/api/voices/sample/${encodeURIComponent(voiceId)}`;
        await runSharedVoicePreview(audioUrl, buttonEl, `persona:${voiceId}`);
    } catch (e) {
        console.warn("Voice preview outer error:", e);
        stopCurrentVoicePreview();
        showStudioToast(t("preview_load_failed"), "error");
    }
}

// ── Gemini TTS Voice Previews ──────────────────────────────────────
const GEMINI_VOICE_PREVIEW_DIR = "/static/audio/gemini";

function resolveGeminiPreviewVoice() {
    const sel = document.getElementById("recap-gemini-voice-select");
    const chosen = (sel && sel.value) || state.recapGeminiVoice || "";
    if (chosen) return chosen;
    // Auto: mirror the API (app/webapp/api.py) — the recap persona's
    const slug = state.recapVoice || "komin";
    const entry = (state.curatedVoices || []).find(v => v.id === slug);
    const isFemale = entry ? entry.gender === "female"
        : ["mahnin", "nilar", "mamay", "manadi", "mayoon", "machaw", "mathiri"].includes(slug);
    return isFemale ? "Kore" : "Charon";
}

async function playGeminiVoicePreview(voiceName, buttonEl) {
    try {
        haptic("light");
        if (!voiceName) {
            showStudioToast(t("preview_load_failed"), "error");
            return;
        }
        if (currentPreviewButton && currentPreviewButton === buttonEl) {
            stopCurrentVoicePreview();
            return;
        }
        stopCurrentVoicePreview();
        const audioUrl = `${GEMINI_VOICE_PREVIEW_DIR}/preview_${voiceName.toLowerCase()}.mp3`;
        await runSharedVoicePreview(audioUrl, buttonEl, `gemini:${voiceName}`);
    } catch (e) {
        console.warn("Gemini voice preview outer error:", e);
        stopCurrentVoicePreview();
        showStudioToast(t("preview_load_failed"), "error");
    }
}

function previewSelectedGeminiVoice(e) {
    try {
        if (e) e.stopPropagation();
        const btn = document.getElementById("btn-preview-gemini-voice") || (e && e.currentTarget) || null;
        playGeminiVoicePreview(resolveGeminiPreviewVoice(), btn);
    } catch (err) {
        console.warn("previewSelectedGeminiVoice error:", err);
        stopCurrentVoicePreview();
        showStudioToast(t("preview_load_failed"), "error");
    }
}

window.previewSelectedVoiceTabPersona = function(e) {
    try {
        if (e) e.stopPropagation();
        const sel = document.getElementById("voice-persona-select");
        const id = (sel && sel.value) || state.selectedPersona || "komin";
        const btn = document.getElementById("btn-voice-persona-preview") || (e && e.currentTarget) || null;
        playVoicePreview(id, btn);
    } catch (err) {
        console.warn("previewSelectedVoiceTabPersona error:", err);
        stopCurrentVoicePreview();
        showStudioToast(t("preview_load_failed"), "error");
    }
};

window.previewSelectedRecapPersona = function(e) {
    try {
        if (e) e.stopPropagation();
        const sel = document.getElementById("recap-persona-select");
        const id = (sel && sel.value) || state.recapVoice || "komin";
        const btn = document.getElementById("btn-recap-persona-preview") || (e && e.currentTarget) || null;
        playVoicePreview(id, btn);
    } catch (err) {
        console.warn("previewSelectedRecapPersona error:", err);
        stopCurrentVoicePreview();
        showStudioToast(t("preview_load_failed"), "error");
    }
};

// Single source of truth for all 8 Microsoft Edge TTS voices (shared by TTS tab + Recap Step 3)
const VCP_EDGE_VOICES = [
    { key: "thiha", neural: "my-MM-ThihaNeural", i18nKey: "tts_edge_thiha" },
    { key: "nilar", neural: "my-MM-NilarNeural", i18nKey: "tts_edge_nilar" },
    { key: "andrew", neural: "en-US-AndrewMultilingualNeural", i18nKey: "tts_edge_andrew" },
    { key: "brian", neural: "en-US-BrianMultilingualNeural", i18nKey: "tts_edge_brian" },
    { key: "remy", neural: "fr-FR-RemyMultilingualNeural", i18nKey: "tts_edge_remy" },
    { key: "ava", neural: "en-US-AvaMultilingualNeural", i18nKey: "tts_edge_ava" },
    { key: "emma", neural: "en-US-EmmaMultilingualNeural", i18nKey: "tts_edge_emma" },
    { key: "vivienne", neural: "fr-FR-VivienneMultilingualNeural", i18nKey: "tts_edge_vivienne" },
];
window.VCP_EDGE_VOICES = VCP_EDGE_VOICES;

function normalizeEdgeVoiceKey(raw) {
    const val = String(raw || "").trim().toLowerCase();
    if (!val) return "thiha";
    const found = VCP_EDGE_VOICES.find(v => v.key === val || v.neural.toLowerCase() === val);
    return found ? found.key : "thiha";
}

function populateEdgeVoiceSelects() {
    ["tts-edge-voice-select", "recap-edge-voice-select"].forEach(id => {
        const sel = document.getElementById(id);
        if (!sel) return;
        const prev = id === "recap-edge-voice-select"
            ? normalizeEdgeVoiceKey(state.recapEdgeVoice || sel.value || "thiha")
            : normalizeEdgeVoiceKey(sel.value || "thiha");
        sel.innerHTML = "";
        VCP_EDGE_VOICES.forEach(v => {
            const opt = document.createElement("option");
            opt.value = v.key;
            opt.setAttribute("data-i18n", v.i18nKey);
            opt.textContent = t(v.i18nKey);
            sel.appendChild(opt);
        });
        sel.value = prev;
    });
}
window.populateEdgeVoiceSelects = populateEdgeVoiceSelects;

async function playEdgeVoicePreview(voiceKey, buttonEl) {
    try {
        haptic("light");
        const key = normalizeEdgeVoiceKey(voiceKey);
        if (currentPreviewButton && currentPreviewButton === buttonEl) {
            stopCurrentVoicePreview();
            return;
        }
        stopCurrentVoicePreview();
        const audioUrl = `/api/voices/edge-sample/${encodeURIComponent(key)}`;
        await runSharedVoicePreview(audioUrl, buttonEl, `edge:${key}`);
    } catch (e) {
        console.warn("Edge voice preview outer error:", e);
        stopCurrentVoicePreview();
        showStudioToast(t("preview_load_failed"), "error");
    }
}
window.playEdgeVoicePreview = playEdgeVoicePreview;

window.previewSelectedRecapEdgeVoice = function(e) {
    try {
        if (e) e.stopPropagation();
        const sel = document.getElementById("recap-edge-voice-select");
        const key = normalizeEdgeVoiceKey((sel && sel.value) || state.recapEdgeVoice || "thiha");
        const btn = document.getElementById("btn-recap-edge-preview") || (e && e.currentTarget) || null;
        playEdgeVoicePreview(key, btn);
    } catch (err) {
        console.warn("previewSelectedRecapEdgeVoice error:", err);
        stopCurrentVoicePreview();
        showStudioToast(t("preview_load_failed"), "error");
    }
};

window.previewSelectedTtsVoice = function(e) {
    try {
        if (e) e.stopPropagation();
        const eng = (typeof ttsState !== "undefined" && ttsState.engine) || "gemini";
        if (eng === "edge") {
            const eSel = document.getElementById("tts-edge-voice-select");
            const eVal = normalizeEdgeVoiceKey((eSel && eSel.value) || "thiha");
            const btn = document.getElementById("btn-tts-edge-preview") || (e && e.currentTarget) || null;
            playEdgeVoicePreview(eVal, btn);
            return;
        }
        const gSel = document.getElementById("tts-gemini-voice-select");
        const gVal = (gSel && gSel.value) || "Charon";
        const btn = document.getElementById("btn-tts-gemini-preview") || (e && e.currentTarget) || null;
        playGeminiVoicePreview(gVal, btn);
    } catch (err) {
        console.warn("previewSelectedTtsVoice error:", err);
        stopCurrentVoicePreview();
        showStudioToast(t("preview_load_failed"), "error");
    }
};

window.previewSavedCustomVoice = function(e) {
    try {
        if (e) e.stopPropagation();
        const file = state.recapOwnVoiceFile || state.selectedReferenceFile;
        if (!file) {
            showStudioToast(t("preview_load_failed"), "error");
            return;
        }
        haptic("light");
        if (recapOwnVoiceAudio) {
            stopRecapOwnVoicePreview();
            return;
        }
        stopCurrentVoicePreview();
        recapOwnVoiceObjectUrl = URL.createObjectURL(file);
        const audio = new Audio(recapOwnVoiceObjectUrl);
        recapOwnVoiceAudio = audio;
        const btn = document.getElementById("btn-saved-custom-voice-preview");
        setPreviewButtonState(btn, "playing");
        const resetBtn = (hadErr) => {
            stopRecapOwnVoicePreview();
            setPreviewButtonState(btn, "idle");
            if (hadErr) showStudioToast(t("preview_load_failed"), "error");
        };
        audio.onended = () => resetBtn(false);
        audio.onerror = () => resetBtn(true);
        audio.play().catch(err => {
            console.warn("Saved custom voice preview error:", err);
            resetBtn(true);
        });
    } catch (err) {
        console.warn("previewSavedCustomVoice error:", err);
        stopRecapOwnVoicePreview();
        showStudioToast(t("preview_load_failed"), "error");
    }
};

window.deleteSavedCustomVoice = function() {
    haptic("light");
    const oldId = state.savedCustomVoiceId;
    state.savedCustomVoiceId = "";
    state.savedCustomVoiceName = "";
    state.savedCustomVoiceActiveId = "";
    if (oldId && state.recapOwnVoiceRefId === oldId && typeof clearRecapOwnVoice === "function") {
        clearRecapOwnVoice();
    }
    if (typeof syncSavedCustomVoiceUi === "function") syncSavedCustomVoiceUi();
    if (typeof saveAccountPrefsNow === "function") saveAccountPrefsNow();
    else if (typeof saveAccountPrefsSoon === "function") saveAccountPrefsSoon();
    showStudioToast(t("saved_custom_voice_deleted_toast"), "info");
};

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
        if (state.selectedReferenceFile || state.savedCustomVoiceActiveId) {
            state.selectedReferenceFile = null;
            state.savedCustomVoiceActiveId = "";
            const refFileInput = document.getElementById("ref-audio-input");
            const refLabel = document.getElementById("ref-audio-label");
            const btnClearRef = document.getElementById("btn-clear-ref-audio");
            const fileUploadBtn = document.querySelector(".btn-file-upload");
            if (refFileInput) refFileInput.value = "";
            if (refLabel) refLabel.innerText = t("ref_file_btn");
            if (btnClearRef) btnClearRef.classList.add("hidden");
            if (fileUploadBtn) fileUploadBtn.classList.remove("has-file");
            if (typeof setCustomCloneStep === "function") setCustomCloneStep(1, null);
        }
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
            const max = state.user?.character_limit || (state.user?.is_vip ? (state.user?.character_limit_purchased ?? 15000) : (state.user?.character_limit_free ?? 3000));
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
                state.savedCustomVoiceActiveId = "";
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
    state.savedCustomVoiceActiveId = "";
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
// ── Home welcome strip (one-time, first-run only) ──────────────────
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
    if (typeof isSplashBlockingOverlays === "function" && isSplashBlockingOverlays()) {
        state._pendingGateBannerData = data;
        banner.classList.add("hidden");
        return;
    }
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
// Batch AR (2026-11-05): the lock also renders the MAINTENANCE screen
const entryGate = { locked: true, checking: false, lastCheckAt: 0, mode: "member" };

function lockEntryGate() {
    const overlay = document.getElementById("gate-lock");
    const blockedBySplash = typeof isSplashBlockingOverlays === "function" && isSplashBlockingOverlays();
    if (overlay) {
        if (blockedBySplash) overlay.classList.add("hidden");
        else overlay.classList.remove("hidden");
    }
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
    if (state.user && state.user.is_banned && !(state.user && state.user.is_admin)) {
        setEntryGateMode("banned", data);
        if (status) status.innerText = "";
        lockEntryGate();
        return;
    }
    // Admins never stay locked (verify already returns verified for them;
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
// 0.0m/0c flash. The live startup verify still runs and locks ONLY
const USER_ME_CACHE_KEY = "vcp_user_me_cache";

// R40 item 8: the profile cache must be keyed to the Telegram user
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
function paintCachedUserHeader(data) {
    const vipBadge = document.getElementById("vip-status-badge");
    const catTag = document.getElementById("profile-category-tag");
    const charCount = document.getElementById("char-count");
    if (vipBadge) {
        vipBadge.className = data.is_vip ? "vip-badge vip" : "vip-badge free";
        vipBadge.innerText = data.is_admin ? "👑 ADMIN" : (data.is_vip ? "👑 VIP" : "FREE");
    }
    if (catTag) {
        catTag.removeAttribute("data-i18n");
        catTag.innerText = data.is_admin ? "ADMIN ⭐" : (data.is_vip ? t("vip_member_pro") : t("vip_member_free"));
        catTag.className = data.is_vip ? "stat-val text-gold" : "stat-val";
    }
    paintVipPlanBalances(data);
    if (typeof paintProfileView === "function") paintProfileView(data);
    if (charCount) {
        const limit = data.character_limit || (data.is_vip ? (data.character_limit_purchased ?? 15000) : (data.character_limit_free ?? 3000));
        charCount.innerText = `0 / ${limit} ${t("char_counter_unit")}`;
    }
    // Logo flags ride the same cached payload (older caches simply
    state.logoCanUse = !!data.can_use_logo;
    state.logoHas = !!data.has_custom_logo;
    refreshLogoUi();
    refreshRecapQualityLock();
    refreshRecapEngineLock();
    paintTtsTodaySurfaces(data);
    if (document.body) { document.body.removeAttribute("data-profile-loading"); document.body.classList.remove("profile-loading"); }
}

// Custom channel logo (paid-only, owner decision 2026-11-02): the
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
    if (posPicker) {
        posPicker.classList.toggle("logo-locked", !canUse);
        posPicker.disabled = !canUse;
    }

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

// Round 12 D5: render quality chooser is admin-only; non-admins are locked to "standard".
function canUseRecapRenderQuality() {
    const u = state.user || {};
    return !!u.is_admin;
}
function refreshRecapQualityLock() {
    const isAdmin = canUseRecapRenderQuality();
    if (!isAdmin && state.recapRenderQuality !== "standard") {
        state.recapRenderQuality = "standard";
    }
    const qRow = document.getElementById("recap-quality-row");
    if (qRow) qRow.classList.toggle("hidden", !isAdmin);
    const surchargeTag = document.getElementById("recap-quality-max-surcharge-tag");
    if (surchargeTag) surchargeTag.classList.toggle("hidden", !isAdmin);
    document.querySelectorAll("#recap-quality-picker .pill-btn").forEach(btn => {
        const quality = btn.getAttribute("data-quality");
        if (quality === "high" || quality === "max") {
            const locked = !isAdmin;
            btn.disabled = locked;
            btn.classList.toggle("quality-locked", locked);
            btn.setAttribute("aria-disabled", locked ? "true" : "false");
        }
        btn.classList.toggle("active", quality === state.recapRenderQuality);
    });
    if (typeof updateRecapSplitCostHint === "function") updateRecapSplitCostHint();
}
window.refreshRecapQualityLock = refreshRecapQualityLock;

// Batch AW-3: free-tier recap narration is Gemini TTS or Edge TTS
// only — the Persona/Clone engine (voxcpm) is purchased + admin.
// Mirrors the Batch AU quality lock exactly: same tier source
// Phase 5: FREE users CAN pick the VoxCPM Persona Engine (using the 12 Persona
// voices when they hold enough Recap Credits AND Voice Clone Minutes).
// Only "My Own Clone Voice" remains paid/VIP-only — checked via canUseRecapCloneEngine().
// Round 13 E3: Own Clone Voice is open to ALL authenticated users (VIP gate removed).
function canUseRecapCloneEngine() {
    return true;
}
let _lastPoolHintFetchAt = 0;
let _lastPoolHighDemand = false;
async function refreshVoxcpmPoolDemandHint(force = false) {
    const hintEl = document.getElementById("recap-clone-demand-hint");
    const eng = state.recapTtsEngine || "voxcpm";
    if (!hintEl) return;
    if (eng !== "voxcpm") {
        hintEl.classList.add("hidden");
        return;
    }
    const now = Date.now();
    if (!force && _lastPoolHintFetchAt && (now - _lastPoolHintFetchAt) < 20000) {
        hintEl.classList.toggle("hidden", !_lastPoolHighDemand);
        return;
    }
    _lastPoolHintFetchAt = now;
    try {
        const res = await fetch("/api/voxcpm/pool-status", { headers: getAuthHeaders() });
        if (!res.ok) return;
        const data = await res.json();
        _lastPoolHighDemand = !!(data && data.high_demand);
        if (data && data.hint_text) hintEl.textContent = data.hint_text;
        hintEl.classList.toggle("hidden", !(_lastPoolHighDemand && (state.recapTtsEngine || "voxcpm") === "voxcpm"));
    } catch (_e) {}
}
window.refreshVoxcpmPoolDemandHint = refreshVoxcpmPoolDemandHint;

function syncRecapEngineAreas() {
    const eng = state.recapTtsEngine || "voxcpm";
    const gv = document.getElementById("recap-gemini-voice-wrap");
    if (gv) gv.classList.toggle("hidden", eng !== "gemini");
    const ev = document.getElementById("recap-edge-voice-wrap");
    if (ev) ev.classList.toggle("hidden", eng !== "edge");
    const pa = document.getElementById("recap-persona-area");
    if (pa) pa.classList.toggle("hidden", eng !== "voxcpm");
    const dh = document.getElementById("recap-clone-demand-hint");
    if (dh) dh.classList.toggle("hidden", !(eng === "voxcpm" && _lastPoolHighDemand));
    if (eng === "voxcpm" && typeof fetch === "function") {
        refreshVoxcpmPoolDemandHint(false);
    }
    // Voice Clone chooser: show ONE sub-block at a time (Own Voice vs Persona)
    const ownActive = !!state.recapOwnVoiceSelected;
    const btnOwn = document.getElementById("btn-recap-clone-own");
    const btnPersona = document.getElementById("btn-recap-clone-persona");
    if (btnOwn) btnOwn.classList.toggle("active", ownActive);
    if (btnPersona) btnPersona.classList.toggle("active", !ownActive);
    const ow = document.getElementById("recap-own-voice-wrap");
    if (ow) ow.classList.toggle("hidden", eng !== "voxcpm" || !ownActive);
    const personaRow = document.getElementById("recap-persona-select-row");
    if (personaRow) personaRow.classList.toggle("hidden", eng !== "voxcpm" || ownActive);
}
function switchRecapCloneMode(mode) {
    haptic("selection");
    const isOwn = mode === "own";
    state.recapOwnVoiceSelected = isOwn;
    syncRecapEngineAreas();
    syncRecapOwnVoiceCard();
    if (typeof refreshRecapStep4Choices === "function") refreshRecapStep4Choices();
}
window.switchRecapCloneMode = switchRecapCloneMode;

function refreshRecapEngineLock() {
    // Round 13 E3: Voice Clone (both Persona & Own Voice) is unlocked for ALL users.
    const engineSel = document.getElementById("recap-engine-picker");
    if (engineSel && ["voxcpm", "gemini", "edge"].includes(state.recapTtsEngine)) {
        engineSel.value = state.recapTtsEngine;
    }
    syncRecapEngineAreas();
    if (typeof syncSavedCustomVoiceUi === "function") syncSavedCustomVoiceUi();
}
window.refreshRecapEngineLock = refreshRecapEngineLock;

// ── Batch BV: own-voice narration card ───────────────────────────────
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
            : (state.recapOwnVoiceRefId && state.recapOwnVoiceFileName
                ? `💾 ${state.recapOwnVoiceFileName}`
                : t("recap_own_voice_upload_btn"));
    }
    if (uploadBtn) uploadBtn.classList.toggle("has-file", hasFile || !!state.recapOwnVoiceRefId);
    if (playBtn) playBtn.classList.toggle("hidden", !hasFile);
    if (clearBtn) clearBtn.classList.toggle("hidden", !hasFile && !state.recapOwnVoiceRefId);
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
    haptic("selection");
    state.recapOwnVoiceSelected = true;
    document.querySelectorAll("#recap-top-voices .voice-card, #recap-all-voices-grid .voice-card")
        .forEach(c => c.classList.remove("active"));
    syncRecapEngineAreas();
    syncRecapOwnVoiceCard();
    if (typeof refreshRecapStep4Choices === "function") refreshRecapStep4Choices();
}

function deselectRecapOwnVoice() {
    if (!state.recapOwnVoiceSelected) return;
    state.recapOwnVoiceSelected = false;
    syncRecapEngineAreas();
    syncRecapOwnVoiceCard();
    if (typeof refreshRecapStep4Choices === "function") refreshRecapStep4Choices();
}

function clearRecapOwnVoice() {
    stopRecapOwnVoicePreview();
    state.recapOwnVoiceRefId = null;
    state.recapOwnVoiceFile = null;
    state.recapOwnVoiceFileName = "";
    state.recapOwnVoiceStatus = "idle";
    const input = document.getElementById("recap-own-voice-input");
    if (input) input.value = "";
    syncRecapOwnVoiceCard();
    if (typeof refreshRecapStep4Choices === "function") refreshRecapStep4Choices();
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

// (Removed obsolete paintPlanHowText — Buy tab uses static Terms card)

// Returns true when the cached profile is a known verified member
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
    setEntryGateMode("checking");
    // Batch AR: maintenance is checked BEFORE the cached-profile
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

// Preflight: surface only dynamic validation warnings (over-limit / empty balance);
// static char count and balance already live in Step 1 (#char-count) and the Home hero.
function updateVoicePreflight() {
    const box = document.getElementById("voice-preflight");
    const input = document.getElementById("voice-text-input");
    if (!input) return;
    const u = state.user || {};
    const len = input.value.length;
    const max = u.character_limit || (u.is_vip ? (u.character_limit_purchased ?? 15000) : (u.character_limit_free ?? 3000));
    let warn = "";
    if (len > max) {
        warn = t("preflight_over_limit").replace(/^\s*•\s*/, "");
    } else if (state.user && !u.is_admin && (u.balance_minutes ?? 0) <= 0) {
        warn = t("preflight_balance_empty").replace(/^\s*•\s*/, "");
    }
    if (box) {
        box.innerText = warn;
        box.classList.toggle("preflight-warn", Boolean(warn));
    }
    syncVoiceExcessAndCost(len, max);
}

// ── Submit Voice Job & Live Polling ─────────────────────────────────────────
async function handleVoiceGenerate() {
    // Batch AC duplicate-submit guard: the button disables on the
    if (!studioSubmitBegin("voice")) return;
    // Batch AC: a second submit while a job is active is explained IN
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

    // Round 7 Item 3: My Voice (custom) mode requires either an uploaded sample
    // or an applied saved voice. Applies to admins too (missing sample is a
    // data error, not a billing gate).
    if (
        state.voxcpmSubMode === "custom"
        && !state.selectedReferenceFile
        && !state.savedCustomVoiceActiveId
    ) {
        studioSubmitFailure("voice");
        showStudioToast(t("toast_custom_voice_needs_sample"), 'warning');
        return;
    }

    const maxChars = state.user?.character_limit || (state.user?.is_vip ? (state.user?.character_limit_purchased ?? 15000) : (state.user?.character_limit_free ?? 3000));
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

        if (state.voxcpmSubMode === "custom" && state.selectedReferenceFile) {
            setCustomCloneStep(3);
            const formData = new FormData();
            formData.append("text", text);
            formData.append("model_slug", targetModelSlug);
            formData.append("speed", state.speed);
            formData.append("gender_preset", "m2m");
            formData.append("engine", "voxcpm"); // VoxCPM2 only — backend default
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
        state._voiceResultRenderedJobId = null;
        state._voiceTerminalHandledJobId = null;
        setVoiceStage("working");
        const spinnerInit = document.getElementById("voice-spinner");
        if (spinnerInit) {
            spinnerInit.innerText = "⚡";
            if (spinnerInit.classList) spinnerInit.classList.add("spinner-wave");
        }
        const cancelBtnStart = document.getElementById("btn-cancel-voice-job");
        if (cancelBtnStart) {
            cancelBtnStart.disabled = false;
            cancelBtnStart.classList.remove("hidden");
        }
        if (statusMsg) statusMsg.innerText = t("preparing_msg");
        if (progressFill) paintProgressBarFill(progressFill, { status: "QUEUED", progress_percent: 0 });
        if (etaText) etaText.innerText = t("calculating_eta");

        // Round 7 Item 2: track in LiveJobs first so polling & confirmation
        // dialog share the exact same live state; no inline status strip in
        // Voice Studio (details live in the dedicated detail view & bar).
        LiveJobs.track({ id: data.job_id, kind: "voice", status: "QUEUED", progress_percent: 0, stage_index: 0, stage_total: 1, title: text.slice(0, 70) });
        startJobPolling(data.job_id);
        studioSubmitSuccess("voice");
        showSubmitConfirmation("voice", data.job_id, t("confirm_cost_voice"));

    } catch (err) {
        studioSubmitFailure("voice");
        showErrorToast(err, 'voice');
        if (progressBox) progressBox.classList.add("hidden");
        if (btnGen) btnGen.disabled = false;
    }
}

// Batch O: persistent fallback disclosure on the voice result card.
// Batch BU: the same card also carries the reference auto-trim note
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
    const trackedInit = (typeof LiveJobs !== "undefined" && LiveJobs.jobs)
        ? LiveJobs.jobs.get(jobId)
        : null;
    if (trackedInit && typeof liveJobsIsTerminal === "function" && liveJobsIsTerminal(trackedInit.status)) {
        if (typeof syncVoiceDetailTerminalState === "function") syncVoiceDetailTerminalState(trackedInit);
        if (trackedInit.status === "SUCCESS" && state._voiceResultRenderedJobId !== jobId && typeof loadCompletedVoiceJobResult === "function") {
            loadCompletedVoiceJobResult(jobId, { scroll: false });
        }
        return;
    }
    const failedBoxInit = document.getElementById("voice-failed-box");
    if (failedBoxInit) failedBoxInit.classList.add("hidden");
    const cancelBtnInit = document.getElementById("btn-cancel-voice-job");
    if (cancelBtnInit) {
        if (trackedInit && typeof liveJobsIsActive === "function" && liveJobsIsActive(trackedInit.status)) {
            cancelBtnInit.disabled = false;
            cancelBtnInit.classList.remove("hidden");
        } else {
            cancelBtnInit.classList.add("hidden");
            cancelBtnInit.disabled = true;
        }
    }

    let tick = 0;
    let failStreak = 0;
    let voiceMaxRank = 0; // Batch AC: monotonic status rank (never backwards)
    let stopped = false;
    let inFlight = false;
    const pollStartedAt = Date.now();
    const stopTimer = () => {
        stopped = true;
        if (state.pollTimer) {
            clearInterval(state.pollTimer);
            state.pollTimer = null;
        }
    };
    // Poll-death honesty (upload-ux pass): the old loop swallowed every
    const stopPollingTimedOut = () => {
        stopTimer();
        const cancelBtn = document.getElementById("btn-cancel-voice-job");
        if (cancelBtn) { cancelBtn.classList.add("hidden"); cancelBtn.disabled = true; }
        const box = document.getElementById("voice-progress-box");
        if (box) box.classList.add("hidden");
        state.voiceDetailOpen = false;
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
    const voicePollTick = async () => {
        if (stopped || inFlight || state.activeJobId !== jobId) return;
        inFlight = true;
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
            if (stopped || state.activeJobId !== jobId) return;

            // Batch AC monotonic guard: a stale/replayed response with
            // an earlier status rank is dropped, never rendered.
            const _voiceRank = { QUEUED: 1, RUNNING: 2, SUCCESS: 3, FAILED: 3, CANCELLED: 3 }[data.status] || 0;
            if (_voiceRank && _voiceRank < voiceMaxRank) return;
            if (_voiceRank) voiceMaxRank = _voiceRank;

            const spinner = document.getElementById("voice-spinner");
            const statusMsg = document.getElementById("voice-status-msg");
            const progressFill = document.getElementById("voice-progress-fill");
            const etaText = document.getElementById("voice-eta-text");
            const progressBox = document.getElementById("voice-progress-box");
            const resultBox = document.getElementById("voice-result-box");
            const cancelBtn = document.getElementById("btn-cancel-voice-job");
            const btnGen = document.getElementById("btn-generate-voice");

            if (data.status === "QUEUED") {
                setVoiceStage("working");
                if (spinner) { spinner.innerText = "⚡"; if (spinner.classList) spinner.classList.add("spinner-wave"); }
                if (cancelBtn) { cancelBtn.disabled = false; cancelBtn.classList.remove("hidden"); }
                if (progressFill) paintProgressBarFill(progressFill, { status: "QUEUED", progress_percent: 0 });
                if (statusMsg) statusMsg.innerText = t("cloud_queue_waiting");
                if (etaText) etaText.innerText = tf("voice_elapsed", { sec: Math.max(0, Math.round((Date.now() - pollStartedAt) / 1000)) });
                LiveJobs.track({ id: jobId, kind: "voice", status: "QUEUED", progress_percent: 0, stage_index: 0, stage_total: 1 });
            } else if (data.status === "WAITING_CHOICE") {
                setVoiceStage("working");
                const vcChoice = data.voice_choice && data.voice_choice.choice ? data.voice_choice.choice : null;
                const vcTier = data.voice_choice && data.voice_choice.tier ? data.voice_choice.tier : null;
                if (progressFill) paintProgressBarFill(progressFill, { status: "WAITING_CHOICE", progress_percent: 0 });
                if (statusMsg) statusMsg.innerText = vcChoice === "wait" ? t("live_job_status_waiting_capacity") : t("live_job_status_waiting_choice");
                if (etaText) etaText.innerText = t("recap_waiting_choice_bot_hint");
                LiveJobs.track({ id: jobId, kind: "voice", status: "WAITING_CHOICE", choice: vcChoice, tier: vcTier, progress_percent: 0, stage_index: 0, stage_total: 1 });
            } else if (data.status === "RUNNING") {
                setVoiceStage("working");
                if (spinner) { spinner.innerText = "⚡"; if (spinner.classList) spinner.classList.add("spinner-wave"); }
                if (cancelBtn) { cancelBtn.disabled = false; cancelBtn.classList.remove("hidden"); }
                if (progressFill) paintProgressBarFill(progressFill, { status: "RUNNING", progress_percent: Number(data.progress_percent) || 0 });
                if (statusMsg) statusMsg.innerText = t("voice_synthesizing");
                if (etaText) etaText.innerText = tf("voice_elapsed", { sec: Math.max(0, Math.round((Date.now() - pollStartedAt) / 1000)) });
                LiveJobs.track({ id: jobId, kind: "voice", status: "RUNNING", progress_percent: Number(data.progress_percent) || 0, stage_index: 0, stage_total: 1 });
            } else if (data.status === "SUCCESS") {
                stopTimer();
                state._voiceTerminalHandledJobId = jobId;
                const terminalJob = { id: jobId, kind: "voice", status: "SUCCESS", progress_percent: 100, stage_index: 1, stage_total: 1 };
                syncVoiceDetailTerminalState(terminalJob);
                LiveJobs.track(terminalJob);
                haptic("success");
                if (data.fallback_used) { try { showStudioToast(t("voice_fallback_notice"), 'info'); } catch (e) {} }
                if (data.reference_trimmed) { try { showStudioToast(t("voice_ref_trim_notice"), 'info'); } catch (e) {} }
                if (progressBox) progressBox.classList.add("hidden");
                state.voiceDetailOpen = false;
                if (resultBox) {
                    resultBox.classList.remove("hidden");
                    // Batch AC: no scroll hijack on completion — the
                    // pill/sheet announce the result; the user opens
                    // it by choice and the studio stays where it was.
                }
                renderVoiceJobResultCard(jobId, data, { scroll: false });
                if (btnGen) btnGen.disabled = false;
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
                stopTimer();
                state._voiceTerminalHandledJobId = jobId;
                const terminalJob = { id: jobId, kind: "voice", status: "FAILED", progress_percent: 0 };
                syncVoiceDetailTerminalState(terminalJob);
                LiveJobs.track(terminalJob);
                haptic("error");
                const errText = mapErrorToToast(data.error_message, 'voice');
                showStudioToast(errText, 'error');
                const fMsg = document.getElementById("voice-failed-msg");
                if (fMsg) fMsg.textContent = errText;
                const fBox = document.getElementById("voice-failed-box");
                if (fBox) fBox.classList.remove("hidden");
                if (progressBox) progressBox.classList.add("hidden");
                state.voiceDetailOpen = false;
                if (btnGen) btnGen.disabled = false;
                if (state.selectedReferenceFile) setCustomCloneStep(2, state.selectedReferenceFile);
            } else if (data.status === "CANCELLED") {
                // Final-audit batch M: cancelled on another surface
                // (admin console) — stop instead of polling to the
                // timeout with a stale progress card.
                stopTimer();
                state._voiceTerminalHandledJobId = jobId;
                const terminalJob = { id: jobId, kind: "voice", status: "CANCELLED", progress_percent: 0 };
                syncVoiceDetailTerminalState(terminalJob);
                LiveJobs.track(terminalJob);
                if (progressBox) progressBox.classList.add("hidden");
                state.voiceDetailOpen = false;
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
        } finally {
            inFlight = false;
        }
    };
    state.pollTimer = setInterval(voicePollTick, 2000);
    voicePollTick();
}

window.cancelVoiceJob = function() {
    if (state.activeJobId && typeof window.liveJobCancel === "function") {
        window.liveJobCancel(state.activeJobId, "voice");
    }
};

// ── Recap duration caps ────────────────────────────────────────────────
function recapNormalMaxSeconds() {
    return state.user?.is_vip ? 600 : (state.user?.recap_max_duration_seconds || 120);
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
function recapOverLimitNoticeText(durationSeconds, maxSeconds, isVip) {
    return tf("recap_over_limit_notice", {
        duration: recapGateDurationText(durationSeconds),
        tier: isVip ? "VIP" : t("tier_free_label"),
        max: Math.floor((Number(maxSeconds) || 0) / 60),
        hint: t(isVip ? "toast_recap_over_hint_vip" : "toast_recap_over_hint_free"),
    });
}

// A server duration rejection the client could not pre-empt (a link
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
    const dur = Number(state.recapSourceDuration || 0);
    const maxAllowed = recapNormalMaxSeconds();
    const effSec = dur > 0 ? Math.min(dur, maxAllowed || dur) : 60;
    const estCredits = dur > 0 ? Math.max(0.05, Math.round((effSec / 60) * 100) / 100) : 1;
    const ctaText = document.querySelector("#btn-start-recap .btn-text");
    const stickyCtaText = document.querySelector("#btn-start-recap-sticky .btn-text");
    const btnLabel = t("btn_start_recap");
    if (ctaText) ctaText.textContent = btnLabel;
    if (stickyCtaText) stickyCtaText.textContent = btnLabel;
    if (state.user?.is_admin) {
        setRecapStep4Quote(0);
        return;
    }
    setRecapStep4Quote(dur > 0 ? estCredits : 1);
}

// Batch K: setLanguage's static sweep resets the CTA label — this
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
    if (controlsGroup) controlsGroup.classList.toggle("hidden", !(ready || recapHasSource()));
    if (ratioGroup) ratioGroup.classList.toggle("hidden", !ready);
    if (speedGroup) speedGroup.classList.toggle("hidden", !ready);
    if ((ready || recapHasSource()) && typeof applyActiveRecapStep2Category === "function") {
        applyActiveRecapStep2Category();
    }
    // Batch BB-2: the speed capsule lives inside speedGroup — when it
    if (ready) requestAnimationFrame(() => { try { moveSpeedPills(); } catch (e) {} try { moveSegPills(); } catch (e) {} });
    // Batch AV: when a source becomes ready, Step 1 checks off and
    syncRecapStepsWithSource(ready);
}
window.setRecapPreviewState = setRecapPreviewState;

// Part C: strict URL syntax + verified metadata gate helpers.
const RECAP_GENERIC_VIDEO_TITLES = new Set([
    "", "media", "video", "video content", "web media", "web media content",
    "online content", "youtube content", "tiktok content", "facebook content",
    "douyin content", "instagram content", "twitter content", "x content",
    "bilibili content", "vimeo content", "reddit content",
    "untitled", "untitled video", "unknown", "none",
]);

function extractRecapHttpUrl(raw) {
    const s = String(raw || "").trim();
    if (!s) return "";
    const m = s.match(/https?:\/\/[^\s]+/i);
    return m ? m[0].replace(/[),.;!?]+$/, "") : "";
}

function isValidRecapHttpUrl(raw) {
    const candidate = extractRecapHttpUrl(raw);
    if (!candidate) return false;
    try {
        const parsed = new URL(candidate);
        if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return false;
        const host = (parsed.hostname || "").trim().toLowerCase();
        if (!host || !host.includes(".") || host.startsWith(".") || host.endsWith(".")) return false;
        return true;
    } catch (e) {
        return false;
    }
}

function isGenericRecapVideoTitle(title, platform) {
    const norm = String(title || "").replace(/\s+/g, " ").trim().toLowerCase();
    if (!norm || RECAP_GENERIC_VIDEO_TITLES.has(norm)) return true;
    const plat = String(platform || "").replace(/\s+/g, " ").trim().toLowerCase();
    if (plat && (norm === plat || norm === `${plat} content` || norm === `${plat} video`)) return true;
    return false;
}

function isVerifiedRecapInfoPayload(data) {
    if (!data || typeof data !== "object") return false;
    if (data.metadata_verified !== true) return false;
    const title = String(data.title || "").trim();
    if (!title || isGenericRecapVideoTitle(title, data.platform)) return false;
    const dur = Number(data.duration_seconds);
    if (!Number.isFinite(dur) || dur <= 0) return false;
    return true;
}

function hideRecapUrlError() {
    const card = document.getElementById("recap-url-error-card");
    if (card) card.classList.add("hidden");
}

function showRecapUrlError() {
    const infoBox = document.getElementById("recap-video-info-box");
    if (infoBox) infoBox.classList.add("hidden");
    const card = document.getElementById("recap-url-error-card");
    const titleEl = document.getElementById("recap-url-error-title");
    const bodyEl = document.getElementById("recap-url-error-body");
    if (titleEl) titleEl.textContent = t("recap_url_verify_err_title");
    if (bodyEl) bodyEl.textContent = t("recap_url_verify_err_body");
    if (card) card.classList.remove("hidden");
}

function resetRecapUrlVerification(opts) {
    const options = opts || {};
    state.recapVerifiedSourceUrl = null;
    state.recapThumbnailUrl = null;
    state.recapSourceWidth = null;
    state.recapSourceHeight = null;
    state.recapStepsSourceFlowDone = false;
    if (state.recapFrameUrl) {
        try { URL.revokeObjectURL(state.recapFrameUrl); } catch (e) {}
        state.recapFrameUrl = null;
    }
    if (state.recapCompositeUrl) {
        try { URL.revokeObjectURL(state.recapCompositeUrl); } catch (e) {}
        state.recapCompositeUrl = null;
    }
    recapCompositeHash = "";
    if (options.clearInfo !== false && typeof clearRecapInfoCard === "function") {
        clearRecapInfoCard();
    }
    if (options.hideError) {
        hideRecapUrlError();
    }
    if (!state.uploadedVideoPath) {
        [1, 2, 3].forEach(s => markRecapStepDone(s, false));
        if (recapCurrentStep > 1) showRecapStep(1);
    }
    setRecapPreviewState((state.uploadedVideoPath && !state.recapUploadPending) ? "ready" : "empty");
    if (typeof updateCanvasMockupVisuals === "function") updateCanvasMockupVisuals();
    if (typeof updateRecapDurationGate === "function") updateRecapDurationGate();
}

window.isValidRecapHttpUrl = isValidRecapHttpUrl;
window.isVerifiedRecapInfoPayload = isVerifiedRecapInfoPayload;
window.showRecapUrlError = showRecapUrlError;
window.hideRecapUrlError = hideRecapUrlError;
window.resetRecapUrlVerification = resetRecapUrlVerification;

// Strict Part C source check: true ONLY when a direct upload succeeded OR
// a URL passed full server metadata verification (never on raw typed text).
function recapHasSource() {
    if (state.uploadedVideoPath && !state.recapUploadPending) return true;
    return Boolean(
        state.recapVerifiedSourceUrl &&
        isVerifiedRecapInfoPayload(state.lastRecapInfo)
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
function isRecapStepOpen(n) {
    return recapCurrentStep === n;
}

// Indicator (Batch AZ-9): one liquid segmented capsule. The
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
    if (n === 2 && typeof window.__ddFitPreviewFrame === "function") {
        requestAnimationFrame(() => window.__ddFitPreviewFrame());
    }
    if (n === 2) {
        if (typeof window.scheduleRecapCompositeRefresh === "function") window.scheduleRecapCompositeRefresh();
        if (typeof window.refreshRecapStep2Summary === "function") window.refreshRecapStep2Summary();
    }
    if (n === 3) {
        const spdGrp = document.getElementById("recap-preview-speed-group");
        if (spdGrp) spdGrp.classList.remove("hidden");
        if (typeof window.refreshRecapStep2Summary === "function") window.refreshRecapStep2Summary();
    }
    refreshRecapStepIndicator();
    refreshStickyCtas();
    const currentBtn = document.querySelector('#recap-steps-nav [data-step-toggle="' + n + '"]');
    if (currentBtn) {
        currentBtn.classList.remove("pop");
        void currentBtn.offsetWidth;
        currentBtn.classList.add("pop");
    }
    if (n === 4) { refreshRecapStep4Output(); refreshRecapStep4Eta(); }
    if (n === 4) { refreshRecapStep4Choices(); updateRecapSplitCostHint(); }
    requestAnimationFrame(() => { try { moveSpeedPills(); } catch (e) {} try { moveSegPills(); } catch (e) {} });
    const nav = document.getElementById("recap-steps-nav");
    if (nav) nav.scrollIntoView({ behavior: "smooth", block: "start" });
    syncTelegramBackButton();
}
window.showRecapStep = showRecapStep;

window.skipRecapToGenerate = function() {
    haptic("medium");
    if (typeof recapHasSource === "function" && recapHasSource()) {
        [1, 2, 3].forEach(s => markRecapStepDone(s, true));
    }
    showRecapStep(4);
};

window.customizeRecapFromStep1 = function() {
    haptic("light");
    if (typeof recapHasSource === "function" && recapHasSource()) {
        [1].forEach(s => markRecapStepDone(s, true));
    }
    showRecapStep(2);
};

function markRecapStepDone(n, done) {
    // Session-truth (AX-3R): Step 4 never shows ✓, and completion is
    if (n === 4) done = false;
    RECAP_STEP_DONE[n] = !!done;
    document.querySelectorAll('[data-step-pill="' + n + '"]')
        .forEach(pill => { pill.textContent = done ? "✓" : String(n); });
    refreshRecapStepIndicator();
}

function syncRecapStepsWithSource(ready) {
    if (!document.getElementById("recap-step-1")) return;
    if (!ready || !recapHasSource()) {
        [1, 2, 3].forEach(s => {
            if (RECAP_STEP_DONE[s]) markRecapStepDone(s, false);
        });
        state.recapStepsSourceFlowDone = false;
        return;
    }
    if (!RECAP_STEP_DONE[1]) markRecapStepDone(1, true);
    if (!state.recapStepsSourceFlowDone) {
        state.recapStepsSourceFlowDone = true;
        showRecapStep(2);
    }
}

function _cleanStep4VoiceName(raw) {
    let s = String(raw || "").trim();
    if (typeof stripPanelEmoji === "function") s = stripPanelEmoji(s);
    // Drop trailing parenthetical descriptor or English/gender tag (e.g. "(Ko Min)", "(ကျား)", "(Male)")
    s = s.replace(/\s*[\(（][^\)）]*[\)）]\s*/g, " ").replace(/\s{2,}/g, " ").trim();
    return s;
}

// Step 4 choices rows (Round 13 Part I): plain title · plain voice name · bare ratio (no emojis, no suffixes)
function refreshRecapStep4Choices() {
    const line = document.getElementById("recap-step4-choices");
    if (!line) return;
    const titleEl = document.getElementById("recap-info-title");
    const info = document.getElementById("recap-video-info-box");
    const title = (info && !info.classList.contains("hidden") && titleEl && titleEl.textContent.trim())
        ? titleEl.textContent.trim() : t("recap_summary_no_source");
    const voiceEl = document.getElementById("recap-step4-choice-voice");
    if (voiceEl) {
        const eng = state.recapTtsEngine || "voxcpm";
        let voiceLine = "";
        if (eng === "none") {
            voiceLine = _cleanStep4VoiceName(t("recap_engine_none"));
        } else if (eng === "gemini") {
            const gKey = typeof resolveGeminiPreviewVoice === "function"
                ? resolveGeminiPreviewVoice() : (state.recapGeminiVoice || "Charon");
            const gRaw = t("gem_voice_" + String(gKey).toLowerCase());
            voiceLine = _cleanStep4VoiceName(gRaw && !gRaw.startsWith("gem_voice_") ? gRaw : String(gKey));
        } else if (eng === "edge") {
            const eSel = document.getElementById("recap-edge-voice-select");
            const eKey = normalizeEdgeVoiceKey((eSel && eSel.value) || state.recapEdgeVoice || "thiha");
            const eRaw = t("tts_edge_" + eKey);
            voiceLine = _cleanStep4VoiceName(eRaw && !eRaw.startsWith("tts_edge_") ? eRaw : eKey);
        } else if (state.recapOwnVoiceSelected) {
            voiceLine = _cleanStep4VoiceName(t("recap_own_voice_display"));
        } else {
            const selEl = document.getElementById("recap-persona-select");
            const slug = (selEl && selEl.value) || state.recapVoice || "komin";
            const selOpt = selEl && selEl.selectedOptions && selEl.selectedOptions[0]
                ? selEl.selectedOptions[0].textContent.trim() : "";
            const pKey = "persona_opt_" + slug;
            const pTrans = t(pKey);
            const rawName = selOpt || (pTrans !== pKey ? pTrans : slug);
            voiceLine = _cleanStep4VoiceName(rawName);
        }
        voiceEl.textContent = voiceLine;
    }
    const ratioEl = document.getElementById("recap-step4-choice-ratio");
    if (ratioEl) {
        const activeBtn = document.querySelector("#recap-preview-ratio-group .ratio-btn.active");
        const bareRatio = (activeBtn && activeBtn.getAttribute("data-ratio")) || state.recapRatio || "9:16";
        ratioEl.textContent = bareRatio;
    }
    const titleSpan = document.getElementById("recap-step4-choice-title");
    if (titleSpan) titleSpan.textContent = title;
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
    const maxAllowed = recapNormalMaxSeconds();
    const eff = Math.min(dur, maxAllowed || dur);
    return Math.max(1, Math.ceil(eff * 1.0));
}

function paintRecapDualQuote(creditsCost) {
    const u = state.user;
    const isAdmin = !!(u && u.is_admin);
    const eng = state.recapTtsEngine || "voxcpm";
    const cost = Math.max(0.01, Math.round((Number(creditsCost) || 1) * 100) / 100);

    const creditMathEl = document.getElementById("recap-step4-credit-math");
    const surchargeTag = document.getElementById("recap-quality-max-surcharge-tag");
    if (surchargeTag) surchargeTag.classList.toggle("hidden", !isAdmin);
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
    const engineSel = document.getElementById("recap-engine-picker");
    if (engineSel && ["voxcpm", "gemini", "edge"].includes(next)) {
        engineSel.value = next;
    }
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
    const quality = (state.user && state.user.is_admin) ? (state.recapRenderQuality || "standard") : "standard";
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

// Step 4 entry (AX-4R): re-request the composite with the CURRENT
function refreshRecapStep4Output() {
    if (recapHasSource() && typeof window.refreshRecapCompositePreview === "function") {
        try { window.refreshRecapCompositePreview(); } catch (e) { /* keep the current frame */ }
    }
    refreshRecapStep4Thumb();
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
    // F1 (Batch AV rewire): the blur box's label is an editing
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
    showRecapStep(1);
}
window.initRecapSteps = initRecapSteps;

// ── TAB 2: Recap Studio Configuration & Visual Mockup ──────────────────────
function setupRecapVisualEditor() {
    // Batch DD: blur-chip / extras render signatures. Declared at the
    // a `let` declared mid-function throws a TDZ error on that early
    // call (seen live 2026-11-10: "Cannot access 'ddChipsSig' before
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

    function setRecapDropFileState(file) {
        const iconEl = document.getElementById("recap-drop-icon");
        const textEl = document.getElementById("recap-drop-text");
        const stateEl = document.getElementById("recap-drop-file-state");
        if (file && file.name) {
            if (dropZone) dropZone.classList.add("has-file");
            if (iconEl) iconEl.textContent = "🎬";
            if (textEl) textEl.classList.remove("hidden");
            if (stateEl) {
                const sz = formatUploadSize(file.size);
                stateEl.innerText = sz ? `${file.name} (${sz})` : file.name;
                stateEl.classList.remove("hidden");
            }
        } else {
            if (dropZone) dropZone.classList.remove("has-file");
            if (iconEl) iconEl.textContent = "📁";
            if (textEl) textEl.classList.remove("hidden");
            if (stateEl) {
                stateEl.innerText = "";
                stateEl.classList.add("hidden");
            }
        }
    }

    let recapCanvasVideoObjectUrl = null;
    async function handleVideoFileSelect(file) {
        if (!file) return;
        haptic("medium");
        setRecapDropFileState(file);
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
                const initSeek = Math.min(5, Math.floor(dur));
                state.recapSeekSec = initSeek;
                updateSeekChipsAvailability(dur);
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

    // Paint verified metadata tags on the Recap info card.
    function paintRecapInfoMeta(data, isOverLimit, maxAllowed) {
        const durSpan = document.getElementById("recap-info-dur");
        const subSpan = document.getElementById("recap-info-sub");
        if (durSpan) {
            if (isOverLimit) {
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

    // R39: clear the analyze info card + cached payload at Analyze
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
    }
    window.clearRecapInfoCard = clearRecapInfoCard;

    // Repaint the cached analyze result after a language change.
    function refreshRecapInfoMetaLanguage() {
        const errCard = document.getElementById("recap-url-error-card");
        if (errCard && !errCard.classList.contains("hidden")) {
            const titleEl = document.getElementById("recap-url-error-title");
            const bodyEl = document.getElementById("recap-url-error-body");
            if (titleEl) titleEl.textContent = t("recap_url_verify_err_title");
            if (bodyEl) bodyEl.textContent = t("recap_url_verify_err_body");
        }
        const data = state.lastRecapInfo;
        if (!data) return;
        const box = document.getElementById("recap-video-info-box");
        if (!box || box.classList.contains("hidden")) return;
        const maxAllowed = recapNormalMaxSeconds();
        let isOverLimit = !!(data.duration_seconds && data.duration_seconds > maxAllowed);
        if (isOverLimit && (state.user?.is_admin || state.user?.is_vip)) isOverLimit = false;
        paintRecapInfoMeta(data, isOverLimit, maxAllowed);
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
            if (!state.uploadedVideoPath) setRecapDropFileState(null);
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
        hideRecapUrlError();
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
        if (isOverLimit && state.user?.is_admin) {
            isOverLimit = false; // admins are exempt from the cap
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

    // ── 0.5. Timeline Frame Scrubber (Quick-Jump Chips) ──
    const seekPills = document.querySelectorAll(".seek-pill-btn");

    function updateSeekChipsAvailability(totalSec) {
        const knownTotal = state.recapSourceDuration > 0 ? (totalSec || state.recapSourceDuration) : 0;
        seekPills.forEach(pill => {
            const pSec = parseFloat(pill.getAttribute("data-sec") || 0);
            pill.disabled = knownTotal > 0 && pSec > knownTotal;
        });
    }

    function seekCanvasFrame(targetSec) {
        const total = state.recapSourceDuration || (canvasVideo?.duration) || 60;
        const clamped = Math.max(0, Math.min(total, targetSec));
        state.recapSeekSec = clamped;
        updateSeekChipsAvailability(total);
        // Batch AK: the composited preview follows the seek (its
        // payload reads state.recapSeekSec).
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
        // N3: the real first cue once the script editor has one.
        let sample = "";
        const firstSeg = document.querySelector("#recap-script-list .script-seg-text");
        if (firstSeg && firstSeg.value) sample = firstSeg.value.trim().slice(0, 120);
        const lockedCrop = recapLockedCropForRatio(state.recapRatio);
        return {
            url: recapCompositeSourceUrl(),
            uploaded_path: state.uploadedVideoPath || null,
            t: Number.isFinite(state.recapSeekSec) ? state.recapSeekSec : 5,
            ratio: state.recapRatio || "9:16",
            ratio_fill: RECAP_LOCKED_FILL,
            crop_top: lockedCrop.crop_top,
            crop_bottom: lockedCrop.crop_bottom,
            crop_left: lockedCrop.crop_left,
            crop_right: lockedCrop.crop_right,
            mirror_video: RECAP_LOCKED_MIRROR,
            zoom_factor: RECAP_LOCKED_ZOOM,
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
    function setRecapPreviewLoading(on) {
        const el = document.getElementById("canvas-preview-loading");
        if (el) el.classList.toggle("hidden", !on);
    }
    window.refreshRecapCompositePreview = refreshRecapCompositePreview;

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
    // render uses (source aspect, chosen ratio, locked crop, locked fill), and
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
        const isOrig = state.recapRatio === "Original";
        const ct = isOrig ? 0 : Math.max(0, Math.min(35, state.recapCropTop || 0)) / 100.0;
        const cb = isOrig ? 0 : Math.max(0, Math.min(35, state.recapCropBottom || 0)) / 100.0;
        const cl = isOrig ? 0 : Math.max(0, Math.min(35, state.recapCropLeft || 0)) / 100.0;
        const cr = isOrig ? 0 : Math.max(0, Math.min(35, state.recapCropRight || 0)) / 100.0;
        const keepW = Math.max(0.10, 1.0 - cl - cr);
        const keepH = Math.max(0.10, 1.0 - ct - cb);
        const hasCrop = (ct > 0.001 || cb > 0.001 || cl > 0.001 || cr > 0.001);
        const rCropped = rSource * (keepW / keepH);
        const fillMode = (typeof RECAP_LOCKED_FILL !== "undefined") ? RECAP_LOCKED_FILL : (state.recapRatioFill || "blur");
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
    function subtitleWidthCanvasFrac(subWPercent) {
        const r = recapPictureRect();
        const sw = Math.max(0, Math.min(100, subWPercent)) / 100.0;
        return sw * r.pw;
    }

    // Batch AA/AG: the blur box's canvas geometry from its
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
        let visualBlurY = "";
        if (!state.recapBlurBoxTouched && previewSub && previewSub.style.display !== "none" && state.recapSubPosBand !== "top" && y >= 55) {
                const offsetY = Math.max(12, y - 34);
                const offsetGeom = blurBoxCanvasGeom(offsetY, h, x, w);
                previewBlur.style.top = `${(offsetGeom.centre * 100).toFixed(3)}%`;
                previewBlur.classList.toggle("is-near-top", (offsetGeom.centre - offsetGeom.height * 0.5) < 0.14);
                visualBlurY = String(offsetY);
        }
        previewBlur.dataset.visualBlurY = visualBlurY;
        // Batch AK: the blur geometry feeds the composited frame —
        // a blur change re-renders it (debounced + hash-guarded).
        scheduleRecapCompositeRefresh();
        // Batch DD: mirror the geometry into the selected list box.
        if (typeof ddAfterBlurSync === "function") ddAfterBlurSync();
    }

    // Batch AK: fitBlurPlateToCaption and its plate element are

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

    function applySubBoxWidth(newW) {
        const clamped = Math.max(10, Math.min(100, Math.round(Number(newW) || 92)));
        if (sliderSubW) sliderSubW.value = String(clamped);
        state.recapSubW = clamped;
        syncSubBoxVisual();
        if (typeof updateSubtitleCharsEstimator === "function") updateSubtitleCharsEstimator();
        if (typeof saveRecapSettingsSoon === "function") saveRecapSettingsSoon();
        return clamped;
    }
    window.applySubBoxWidth = applySubBoxWidth;

    if (sliderSubW && previewSub && valSubW) {
        sliderSubW.addEventListener("input", () => {
            applySubBoxWidth(sliderSubW.value);
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
    // Attached alongside the vertical drag, a diagonal drag moves
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
    }, () => {
        if (!state.recapBlurBoxTouched && previewBlur.dataset.visualBlurY) {
            const vy = parseFloat(previewBlur.dataset.visualBlurY);
            if (Number.isFinite(vy)) return vy;
        }
        return sliderBlurY ? parseFloat(sliderBlurY.value) : 85;
    }, () => {
        // Batch AA: mirror Batch P's subtitle drag — on letterboxed
        // layouts the box's canvas position moves ph× slower than
        // blur_y (picture-relative anchor), so scale the drag delta
        // by 1/ph and the box follows the finger 1:1.
        const r = recapPictureRect();
        return ((r.oy + r.ph) < 1 - 0.002 && r.ph > 0) ? 1 / r.ph : 1;
    });

    // Attach dragging to Subtitle Box
    // Batch AK: the slider is the bottom MARGIN now — dragging the
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

    // ── Resize Handles for Subtitle Box (Round 13 Part A) ──
    const subResizeLeft = previewSub ? previewSub.querySelector(".box-resize-handle.left") : null;
    const subResizeRight = previewSub ? previewSub.querySelector(".box-resize-handle.right") : null;
    const subResizeTop = previewSub ? previewSub.querySelector(".box-resize-handle.top") : null;
    const subResizeBottom = previewSub ? previewSub.querySelector(".box-resize-handle.bottom") : null;

    function attachSubResizeX(handle, isLeft) {
        if (!handle) return;
        let startX = 0;
        let startW = 92;
        let isResizing = false;

        function startResize(x) {
            isResizing = true;
            startX = x;
            startW = sliderSubW ? parseFloat(sliderSubW.value) : (state.recapSubW || 92);
            if (previewSub) previewSub.classList.add("is-dragging");
            haptic("light");
        }
        function moveResize(x) {
            if (!isResizing) return;
            const canvasRect = canvas ? canvas.getBoundingClientRect() : null;
            const canvasW = (canvasRect && canvasRect.width > 0) ? canvasRect.width : 360;
            const deltaPx = isLeft ? (startX - x) : (x - startX);
            const deltaPct = (deltaPx / canvasW) * 100;
            const r = recapPictureRect();
            const deltaScale = (r.pw < 1 - 0.002 && r.pw > 0) ? 1 / r.pw : 1;
            // Center-anchored subtitle box: dragging one edge outward by d expands total width by 2*d.
            applySubBoxWidth(startW + deltaPct * deltaScale * 2);
        }
        function endResize() {
            isResizing = false;
            if (previewSub) previewSub.classList.remove("is-dragging");
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
    function attachSubResizeY(handle) {
        if (!handle) return;
        let startY = 0;
        let startMargin = 5;
        let isResizing = false;
        function startResize(y) {
            isResizing = true;
            startY = y;
            startMargin = sliderSubY ? parseFloat(sliderSubY.value) : (state.recapSubMargin ?? 5);
            if (previewSub) previewSub.classList.add("is-dragging");
            haptic("light");
        }
        function moveResize(y) {
            if (!isResizing) return;
            const canvasRect = canvas ? canvas.getBoundingClientRect() : null;
            const canvasH = (canvasRect && canvasRect.height > 0) ? canvasRect.height : 360;
            const deltaPct = ((startY - y) / canvasH) * 100;
            const r = recapPictureRect();
            const deltaScale = ((r.oy + r.ph) < 1 - 0.002 && r.ph > 0) ? 1 / r.ph : 1;
            const newM = Math.max(0, Math.min(95, Math.round(startMargin + deltaPct * deltaScale)));
            if (state.recapSubPosBand !== "bottom") {
                state.recapSubPosBand = "bottom";
                if (typeof ddSyncPosBandPills === "function") ddSyncPosBandPills();
            }
            if (sliderSubY) sliderSubY.value = String(newM);
            syncSubBoxVisual();
        }
        function endResize() {
            isResizing = false;
            if (previewSub) previewSub.classList.remove("is-dragging");
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
    attachSubResizeX(subResizeLeft, true);
    attachSubResizeX(subResizeRight, false);
    attachSubResizeY(subResizeTop);
    attachSubResizeY(subResizeBottom);

    syncBlurBoxVisual();
    syncSubBoxVisual();

    // Batch AV: the compact-toolbar tab binding was retired with the

    // ── Helper: Real-time Live Canvas Mockup Visual Updater ──
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

        syncLockedRecapDefaultsToState();
        const zoom = RECAP_LOCKED_ZOOM;

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

        // 2. Canvas Base Classes & Fill Mode (locked to blur + mirror)
        const fillMode = RECAP_LOCKED_FILL;
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

        const lockedCrop = recapLockedCropForRatio(state.recapRatio);
        const cTop = Math.max(0, Math.min(35, lockedCrop.crop_top || 0));
        const cBottom = Math.max(0, Math.min(35, lockedCrop.crop_bottom || 0));
        const cLeft = Math.max(0, Math.min(35, lockedCrop.crop_left || 0));
        const cRight = Math.max(0, Math.min(35, lockedCrop.crop_right || 0));

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
        scheduleRecapCompositeRefresh();
    }

    // 2. Aspect Ratio Selector (9:16, 16:9, 1:1, Original)
    const ratioBtns = document.querySelectorAll("#recap-ratio-picker .ratio-btn");
    ratioBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            ratioBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            state.recapRatio = btn.getAttribute("data-ratio") || "9:16";
            syncLockedRecapDefaultsToState();
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
            if (typeof refreshRecapStep2Summary === "function") refreshRecapStep2Summary();
        });
    });

    // Round 13 Part H: Source language panel & sheet removed; pipeline stays on Auto.
    state.recapSourceLanguage = "auto";

    // 6d. Render quality pills (standard = the historical default encode)
    const qualityBtns = document.querySelectorAll("#recap-quality-picker .pill-btn");
    qualityBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            if (btn.disabled || !canUseRecapRenderQuality()) return;
            haptic("selection");
            qualityBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            state.recapRenderQuality = btn.getAttribute("data-quality") || "standard";
            // Batch BE: the quote depends on the quality (Max carries
            // the +1 surcharge while tiered pricing is armed).
            updateRecapSplitCostHint();
        });
    });
    refreshRecapQualityLock();

    // 6e. Narration engine select (Voice Clone / Gemini TTS / Edge TTS) + voice selects.
    const engineSel = document.getElementById("recap-engine-picker");
    if (engineSel) {
        engineSel.addEventListener("change", () => {
            const opt = engineSel.options[engineSel.selectedIndex];
            if (opt && opt.disabled) {
                engineSel.value = state.recapTtsEngine || "gemini";
                return;
            }
            haptic("selection");
            state.recapTtsEngine = engineSel.value || "voxcpm";
            state.recapInsufMinutesServer = null;
            syncRecapEngineAreas();
            updateRecapSplitCostHint();
            if (typeof refreshRecapStep4Choices === "function") refreshRecapStep4Choices();
        });
    }
    refreshRecapEngineLock();
    initRecapOwnVoice();
    populateEdgeVoiceSelects();
    const geminiVoiceSel = document.getElementById("recap-gemini-voice-select");
    if (geminiVoiceSel) {
        geminiVoiceSel.addEventListener("change", () => {
            state.recapGeminiVoice = geminiVoiceSel.value || "";
            if (typeof refreshRecapStep4Choices === "function") refreshRecapStep4Choices();
            if (currentPreviewButton && currentPreviewButton.id === "btn-preview-gemini-voice") {
                stopCurrentVoicePreview();
            }
        });
    }
    const edgeVoiceSel = document.getElementById("recap-edge-voice-select");
    if (edgeVoiceSel) {
        edgeVoiceSel.addEventListener("change", () => {
            haptic("selection");
            state.recapEdgeVoice = normalizeEdgeVoiceKey(edgeVoiceSel.value || "thiha");
            if (typeof refreshRecapStep4Choices === "function") refreshRecapStep4Choices();
            if (currentPreviewButton && currentPreviewButton.id === "btn-recap-edge-preview") {
                stopCurrentVoicePreview();
            }
            if (typeof saveRecapSettingsSoon === "function") saveRecapSettingsSoon();
        });
    }

    // 7. Narration Story Style Picker (viral, funny, standard, pov) — 2x2 grid
    const storyPills = document.querySelectorAll("#story-style-picker-quick .style-select-pill");
    storyPills.forEach(pill => {
        pill.addEventListener("click", () => {
            haptic("selection");
            const chosenStory = pill.getAttribute("data-story") || "pov";
            state.recapStoryStyle = chosenStory;
            storyPills.forEach(p => p.classList.toggle("active", p.getAttribute("data-story") === chosenStory));
            if (typeof refreshRecapStep2Summary === "function") refreshRecapStep2Summary();
        });
    });

    // Reset Layout Helper (Blur Y/H, Sub Y, Crop, Zoom)
    function resetRecapCanvasLayout() {
        haptic("medium");
        if (sliderBlurY) sliderBlurY.value = "76";
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

        syncLockedRecapDefaultsToState();
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
    const styleSel = document.getElementById("sub-style-picker");
    if (styleSel) {
        styleSel.addEventListener("change", () => {
            haptic("selection");
            state.recapSubStyle = styleSel.value || "yellow";
            // Batch DD: a preset SEEDS the free controls (the user
            // can still tweak them afterwards).
            if (typeof ddSeedStyleControls === "function") ddSeedStyleControls(state.recapSubStyle);
            updateSubtitleVisualPreview();
            if (typeof refreshRecapStep2Summary === "function") refreshRecapStep2Summary();
            // Batch AK: the style renders in the composited frame —
            // refresh it; the drag handle keeps a neutral outline
            // (no per-style CSS classes to keep in sync any more).
            scheduleRecapCompositeRefresh();
            if (typeof saveRecapSettingsSoon === "function") saveRecapSettingsSoon();
        });
    }

    // 10. Subtitle Live Preview Renderer
    function updateSubtitleVisualPreview() {
        const badge = document.getElementById("recap-sub-style-live-preview");
        if (badge) {
            const st = state.recapSubStyle || "cinematic";
            badge.className = "vcp-sub-sample recap-sub-live-badge sample-" + st;
        }
        return;
    }
    window.updateSubtitleVisualPreview = updateSubtitleVisualPreview;
    // 11. Font Size — Batch DD: ONE full-width slider (10–26pt)
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
        const linesSel = document.getElementById("sub-lines-picker-toolbar");
        if (linesSel) linesSel.value = String(state.recapLines);
        if (typeof updateSubtitleCharsEstimator === "function") updateSubtitleCharsEstimator();
        updateSubtitleVisualPreview();
        scheduleRecapCompositeRefresh();
        if (typeof window.refreshRecapStep2Summary === "function") window.refreshRecapStep2Summary();
    }
    const subLinesSel = document.getElementById("sub-lines-picker-toolbar");
    if (subLinesSel) {
        subLinesSel.addEventListener("change", () => {
            haptic("selection");
            syncSubtitleLines(subLinesSel.value);
        });
    }

    function syncSubtitleModeUi() {
        const mode = state.recapSubRenderMode || "timeline";
        const modeSel = document.getElementById("recap-sub-render-mode");
        if (modeSel) modeSel.value = mode;
        const fxRow = document.getElementById("recap-sub-effect-row");
        if (fxRow) fxRow.classList.toggle("hidden", mode === "soft");
        const fxSel = document.getElementById("recap-sub-effect");
        if (fxSel) fxSel.value = state.recapSubEffect || "none";
        const softCb = document.getElementById("toggle-embed-soft-subs");
        if (softCb) softCb.checked = state.recapEmbedSoftSubs !== false;
    }
    const subModeSel = document.getElementById("recap-sub-render-mode");
    if (subModeSel) {
        subModeSel.addEventListener("change", () => {
            haptic("selection");
            state.recapSubRenderMode = subModeSel.value || "timeline";
            if (state.recapSubRenderMode === "karaoke") state.recapSubEffect = "karaoke";
            else if (state.recapSubRenderMode === "ass" && state.recapSubEffect === "none") state.recapSubEffect = "fade";
            syncSubtitleModeUi();
            scheduleRecapCompositeRefresh();
            if (typeof refreshRecapStep2Summary === "function") refreshRecapStep2Summary();
            if (typeof saveRecapSettingsSoon === "function") saveRecapSettingsSoon();
        });
    }
    const subFxSel = document.getElementById("recap-sub-effect");
    if (subFxSel) {
        subFxSel.addEventListener("change", () => {
            haptic("selection");
            state.recapSubEffect = subFxSel.value || "none";
            if (state.recapSubEffect === "karaoke" && state.recapSubRenderMode === "timeline") state.recapSubRenderMode = "karaoke";
            syncSubtitleModeUi();
            if (typeof refreshRecapStep2Summary === "function") refreshRecapStep2Summary();
            if (typeof saveRecapSettingsSoon === "function") saveRecapSettingsSoon();
        });
    }
    const embedSoftEl = document.getElementById("toggle-embed-soft-subs");
    if (embedSoftEl) {
        embedSoftEl.addEventListener("change", () => {
            state.recapEmbedSoftSubs = !!embedSoftEl.checked;
            if (typeof saveRecapSettingsSoon === "function") saveRecapSettingsSoon();
        });
    }

    // 12b. Subtitle Font Picker (padauk / noto_sans / pyidaungsu / noto_serif / custom)
    const subFontSel = document.getElementById("recap-sub-font-picker");
    if (subFontSel) {
        subFontSel.addEventListener("change", () => {
            const val = subFontSel.value || "padauk";
            if (val === "custom") {
                subFontSel.value = state.recapSubFontCustomId ? "uploaded_custom" : (state.recapSubFont || "padauk");
                const fontInp = document.getElementById("input-sub-font-file");
                if (fontInp) fontInp.click();
                return;
            }
            if (val === "uploaded_custom") return;
            haptic("selection");
            state.recapSubFontCustomId = "";
            state.recapSubFontCustomName = "";
            state.recapSubFont = val;
            if (typeof ddSyncCustomFontUi === "function") ddSyncCustomFontUi();
            updateSubtitleVisualPreview();
            scheduleRecapCompositeRefresh();
            if (typeof saveRecapSettingsSoon === "function") saveRecapSettingsSoon();
        });
    }

    // ── Batch DD wiring (Step-2 rebuild) ─────────────────────────
    const ddAutoDetectEl = document.getElementById("recap-blur-autodetect-toggle");
    if (ddAutoDetectEl) {
        ddAutoDetectEl.addEventListener("change", () => {
            state.recapBlurAutoDetect = !!ddAutoDetectEl.checked;
            scheduleRecapCompositeRefresh();
            refreshRecapStep2Summary();
        });
    }

    // (1) Accordion header values (for any accordion that opts into .dd-acc-val).
    function refreshRecapStep2Summary() {
        const setVal = (id, txt) => {
            const gEl = document.getElementById(id);
            const v = gEl ? gEl.querySelector(".dd-acc-val") : null;
            if (v) v.innerText = txt;
        };
        setVal("recap-preview-ratio-group", `${state.recapRatio || "9:16"} · ${RECAP_LOCKED_FILL}`);
        const storyKey = state.recapStoryStyle || "pov";
        const storyPill = document.querySelector(`#story-style-picker-quick [data-story="${storyKey}"]`);
        setVal("recap-story-style-acc", storyPill ? storyPill.textContent.trim() : storyKey);
        setVal("recap-pace-acc", state.recapSpeed || "+12%");
    }
    window.refreshRecapStep2Summary = refreshRecapStep2Summary;

    const srBoxEl = document.getElementById("recap-script-review-toggle");
    if (srBoxEl && !srBoxEl.dataset.vcpSummaryWired) {
        srBoxEl.dataset.vcpSummaryWired = "1";
        srBoxEl.addEventListener("change", refreshRecapStep2Summary);
    }

    // (2) Accordion: every .dd-acc-group gets a header (title + optional .dd-acc-val + chevron)
    function setupRecapAccordion() {
        document.querySelectorAll(".dd-acc-group").forEach(group => {
            if (group.dataset.accReady) return;
            group.dataset.accReady = "1";
            const titleKey = group.getAttribute("data-acc-title-key") || "";
            const vipKey = group.getAttribute("data-acc-vip-key") || "";
            const head = document.createElement("button");
            head.type = "button";
            head.className = "dd-acc-head vcp-acc-head";
            head.setAttribute("aria-expanded", "false");
            const inFine = !!(group.closest && group.closest("#recap-fine-body"));
            const hideVal = inFine || group.getAttribute("data-no-acc-val") === "1";
            const plainTxt = (raw) => (inFine && typeof stripPanelEmoji === "function") ? stripPanelEmoji(raw) : raw;
            const titleSpan = document.createElement("span");
            titleSpan.className = "dd-acc-title";
            if (titleKey) titleSpan.setAttribute("data-i18n", titleKey);
            titleSpan.textContent = titleKey ? plainTxt(t(titleKey)) : "";
            head.appendChild(titleSpan);
            if (vipKey) {
                const vipSpan = document.createElement("span");
                vipSpan.className = "dd-acc-vip-tag";
                vipSpan.setAttribute("data-i18n", vipKey);
                vipSpan.textContent = plainTxt(t(vipKey));
                head.appendChild(vipSpan);
            }
            if (!hideVal) {
                const valSpan = document.createElement("span");
                valSpan.className = "dd-acc-val";
                head.appendChild(valSpan);
            }
            const chev = document.createElement("span");
            chev.className = "dd-acc-chevron";
            chev.textContent = "▸";
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

    // (5) Free style controls. A preset SEEDS them (ddSeedStyleControls,
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
    function ddColorHolderForTarget(target) {
        if (target === "outline") return document.getElementById("input-sub-outline-color");
        if (target === "bg") return document.getElementById("input-sub-bg-color");
        return document.getElementById("input-sub-text-color");
    }
    function ddSyncActiveColorInput() {
        const targetSel = document.getElementById("recap-sub-color-target");
        const activeInp = document.getElementById("input-sub-active-color");
        if (!activeInp) return;
        const target = targetSel ? targetSel.value : "text";
        const holder = ddColorHolderForTarget(target);
        if (holder && /^#[0-9a-fA-F]{6}$/.test(holder.value || "")) {
            activeInp.value = holder.value;
        }
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
        ddSyncActiveColorInput();
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
        if (el) el.addEventListener("input", () => {
            state[key] = el.value;
            ddSyncActiveColorInput();
            ddStyleChanged();
        });
    });
    const ddColorTargetSel = document.getElementById("recap-sub-color-target");
    const ddActiveColorInp = document.getElementById("input-sub-active-color");
    if (ddColorTargetSel) ddColorTargetSel.addEventListener("change", ddSyncActiveColorInput);
    if (ddActiveColorInp) {
        const pushActiveColor = (evName) => {
            const target = ddColorTargetSel ? ddColorTargetSel.value : "text";
            const holder = ddColorHolderForTarget(target);
            if (!holder) return;
            holder.value = ddActiveColorInp.value;
            holder.dispatchEvent(new Event(evName, { bubbles: true }));
        };
        ddActiveColorInp.addEventListener("input", () => pushActiveColor("input"));
        ddActiveColorInp.addEventListener("change", () => pushActiveColor("change"));
    }
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
            ddSyncActiveColorInput();
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

    // (6) Subtitle position band select (Top / Centre / Bottom).
    function ddSyncPosBandPills() {
        const sel = document.getElementById("sub-pos-picker");
        if (sel) sel.value = state.recapSubPosBand || "bottom";
    }
    const subPosSel = document.getElementById("sub-pos-picker");
    if (subPosSel) {
        subPosSel.addEventListener("change", () => {
            haptic("selection");
            state.recapSubPosBand = subPosSel.value || "bottom";
            ddSyncPosBandPills();
            scheduleRecapCompositeRefresh();
            refreshRecapStep2Summary();
            if (typeof saveRecapSettingsSoon === "function") saveRecapSettingsSoon();
        });
    }

    // (7) Custom subtitle font: upload -> handle in state; option lives inside #recap-sub-font-picker.
    function ddSyncCustomFontUi() {
        const fontSel = document.getElementById("recap-sub-font-picker");
        if (!fontSel) return;
        const customOpt = fontSel.querySelector('option[value="custom"]');
        if (customOpt && typeof t === "function") {
            customOpt.textContent = t("recap_sub_font_custom_upload");
        }
        let uploadedOpt = fontSel.querySelector('option[value="uploaded_custom"]');
        const has = !!state.recapSubFontCustomId;
        if (has) {
            const fname = state.recapSubFontCustomName || state.recapSubFontCustomId;
            if (!uploadedOpt) {
                uploadedOpt = document.createElement("option");
                uploadedOpt.value = "uploaded_custom";
                if (customOpt) fontSel.insertBefore(uploadedOpt, customOpt);
                else fontSel.appendChild(uploadedOpt);
            }
            uploadedOpt.textContent = `${t("recap_font_custom_active")}: ${fname}`;
            fontSel.value = "uploaded_custom";
        } else {
            if (uploadedOpt) uploadedOpt.remove();
            fontSel.value = state.recapSubFont || "padauk";
        }
    }
    window.ddSyncCustomFontUi = ddSyncCustomFontUi;
    const ddFontInput = document.getElementById("input-sub-font-file");
    if (ddFontInput) ddFontInput.addEventListener("change", async () => {
        const f = ddFontInput.files && ddFontInput.files[0];
        if (!f) return;
        showStudioToast(t("recap_font_uploading"), "info");
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
            ddSyncCustomFontUi();
            showStudioToast(t("recap_font_upload_error"), "warning");
        } finally {
            ddFontInput.value = "";
        }
    });

    // (8) Restore-path control sync (called from restoreRecapSettings
    window.ddSyncControlsFromState = function () {
        const setColor = (id, v) => { const el = document.getElementById(id); if (el && v) el.value = v; };
        setColor("input-sub-text-color", state.recapTextColor);
        setColor("input-sub-outline-color", state.recapOutlineColor);
        setColor("input-sub-bg-color", state.recapBgColor);
        ddSyncActiveColorInput();
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
    ["recap-ratio-picker"].forEach(id => {
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
    const logoPosSel = document.getElementById("recap-logo-pos-picker");
    if (logoPosSel) {
        logoPosSel.addEventListener("change", () => {
            haptic("selection");
            state.recapLogoPos = logoPosSel.value || "top-right";
            if (previewLogo) {
                previewLogo.className = `preview-logo-watermark ${state.recapLogoPos}`;
                applyLogoPreviewStyle();
            }
            scheduleRecapCompositeRefresh();
        });
    }
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

    // 14. Analyze / Video Info Check Button (Part C strict URL gate)
    const btnAnalyze = document.getElementById("btn-fetch-recap-info");
    const recapInfoBox = document.getElementById("recap-video-info-box");
    let recapUrlTimer = null;
    if (btnAnalyze) {
        btnAnalyze.addEventListener("click", async () => {
            if (recapUrlTimer) { clearTimeout(recapUrlTimer); recapUrlTimer = null; }
            const urlInput = document.getElementById("recap-url-input");
            const rawInput = urlInput ? urlInput.value.trim() : "";
            if (!rawInput) {
                resetRecapUrlVerification({ clearInfo: true, hideError: false });
                showRecapUrlError();
                showStudioToast(t("toast_empty_url"), 'warning');
                return;
            }
            // Client-side strict URL gate: reject plain text / malformed URLs
            // immediately on Step 1 with ZERO server call.
            if (!isValidRecapHttpUrl(rawInput)) {
                resetRecapUrlVerification({ clearInfo: true, hideError: false });
                showRecapUrlError();
                return;
            }
            // Auto extract clean URL from Douyin/TikTok share text
            const url = extractRecapHttpUrl(rawInput);
            if (urlInput && urlInput.value.trim() !== url) {
                urlInput.value = url;
            }
            haptic("medium");
            btnAnalyze.disabled = true;
            const originalText = btnAnalyze.innerText;
            btnAnalyze.innerText = "⏳...";
            hideRecapUrlError();
            state.uploadedVideoPath = null;
            state.recapUploadPending = false;
            state.recapVerifiedSourceUrl = null;
            state.recapThumbnailUrl = null;
            state.recapStepsSourceFlowDone = false;
            [1, 2, 3].forEach(s => markRecapStepDone(s, false));
            setRecapPreviewState("loading");
            clearRecapInfoCard();

            try {
                // Phase 4: a Downloader "Use in Recap" hand-off carries
                // the SAME /api/downloader/extract payload the Downloader
                // already verified for this exact URL — reuse it only if
                // it passes the strict verified payload rule.
                let data = null;
                const handoff = state.recapExtractHandoff;
                state.recapExtractHandoff = null;
                if (
                    handoff &&
                    handoff.data &&
                    (handoff.url === url || handoff.data.url === url) &&
                    isVerifiedRecapInfoPayload(handoff.data)
                ) {
                    data = handoff.data;
                } else {
                    const res = await fetchWithTimeout("/api/downloader/extract", {
                        method: "POST",
                        headers: getAuthHeaders(),
                        body: JSON.stringify({ url: url }),
                    }, 60000);
                    let parsed = null;
                    try { parsed = await res.json(); } catch (_) { parsed = null; }
                    if (!res.ok || !isVerifiedRecapInfoPayload(parsed)) {
                        throw new Error((parsed && parsed.detail) || t("recap_url_verify_err_title"));
                    }
                    data = parsed;
                }

                // If the user edited the URL while the check was in flight,
                // discard the stale result so Steps 2/3 never unlock for an
                // edited/unverified link.
                const currentInputUrl = urlInput ? extractRecapHttpUrl(urlInput.value.trim()) : url;
                if (currentInputUrl && currentInputUrl !== url && currentInputUrl !== data.url) {
                    return;
                }

                hideRecapUrlError();
                if (recapInfoBox) recapInfoBox.classList.remove("hidden");
                const thumbImg = document.getElementById("recap-info-thumb");
                const titleSpan = document.getElementById("recap-info-title");
                const platformSpan = document.getElementById("recap-info-platform");

                setCardThumb(thumbImg, data.thumbnail || null);
                if (titleSpan) titleSpan.innerText = data.title;
                state.recapVideoTitle = data.title;

                state.recapSourceDuration = Number(data.duration_seconds) || 0;
                state.lastRecapInfo = data;
                state.recapVerifiedSourceUrl = data.url || url;
                updateRecapSplitCostHint();
                const maxAllowed = recapNormalMaxSeconds();
                let isOverLimit = (state.recapSourceDuration > maxAllowed);
                if (isOverLimit && state.user?.is_admin) {
                    isOverLimit = false; // admins are exempt from the cap
                }
                const btnStartRecap = document.getElementById("btn-start-recap");
                paintRecapInfoMeta(data, isOverLimit, maxAllowed);
                updateRecapDurationGate();
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
                }

                if (platformSpan) platformSpan.innerText = data.platform || "Video";

                if (data.width && data.height) {
                    state.recapSourceWidth = data.width;
                    state.recapSourceHeight = data.height;
                }
                // Reflect thumbnail on live preview canvas & state ONLY after verification
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

                const _aaSourceKey = "url:" + (data.url || url);
                if (state.recapLoadedSourceKey !== _aaSourceKey) {
                    state.recapLoadedSourceKey = _aaSourceKey;
                    resetBlurBoxForNewSource();
                }
                if (state.recapFrameUrl) { URL.revokeObjectURL(state.recapFrameUrl); state.recapFrameUrl = null; }
                if (window.scheduleRecapFrameRefresh) window.scheduleRecapFrameRefresh(Math.min(5, state.recapSourceDuration || 5));
                if (window.scheduleRecapCompositeRefresh) window.scheduleRecapCompositeRefresh();
                state.recapSeekSec = Math.min(5, state.recapSourceDuration || 5);
                if (typeof updateSeekChipsAvailability === "function") {
                    updateSeekChipsAvailability(state.recapSourceDuration || 0);
                }
                setRecapPreviewState("ready");
            } catch (err) {
                // Part C strict gate: stay on Step 1, keep Steps 2/3 locked,
                // never fabricate a shallow card, never unlock or skip.
                resetRecapUrlVerification({ clearInfo: true, hideError: false });
                showRecapUrlError();
                state.recapSeekSec = 5;
                if (typeof updateSeekChipsAvailability === "function") {
                    updateSeekChipsAvailability(0);
                }
                console.warn("Recap URL verification failed:", err);
            } finally {
                btnAnalyze.disabled = false;
                btnAnalyze.innerText = originalText;
                if (recapHasSource()) {
                    scheduleRecapCompositeRefresh();
                }
            }
        });
    }

    // URL input edit resets verified state immediately and re-locks Steps 2/3
    const urlInput = document.getElementById("recap-url-input");
    if (urlInput) {
        urlInput.addEventListener("input", () => {
            clearTimeout(recapUrlTimer);
            const val = urlInput.value.trim();
            // Editing URL text ALWAYS clears verified state and re-locks Steps 2/3
            // until a new check passes (Part C requirement 4).
            if (!state.uploadedVideoPath) {
                resetRecapUrlVerification({ clearInfo: true, hideError: true });
            } else {
                hideRecapUrlError();
            }
            if (!val) return;

            if (isValidRecapHttpUrl(val)) {
                recapUrlTimer = setTimeout(() => {
                    if (urlInput.value.trim() === val && btnAnalyze && !btnAnalyze.disabled) {
                        btnAnalyze.click();
                    }
                }, 700);
            } else {
                // Reject obvious non-URL text client-side after pause with ZERO
                // network call (skip while user is still typing an http/https prefix).
                const typingHttpPrefix = /^https?:?\/?\/?$/i.test(val) || "https://".startsWith(val.toLowerCase()) || "http://".startsWith(val.toLowerCase());
                if (!typingHttpPrefix) {
                    recapUrlTimer = setTimeout(() => {
                        const cur = urlInput.value.trim();
                        if (cur && !isValidRecapHttpUrl(cur)) {
                            resetRecapUrlVerification({ clearInfo: true, hideError: false });
                            showRecapUrlError();
                        }
                    }, 500);
                }
            }
        });
        urlInput.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && btnAnalyze && !btnAnalyze.disabled) {
                e.preventDefault();
                btnAnalyze.click();
            }
        });
    }

    // Wire up Voice Cards (top-4 grid + the all-12 grid in the expander
    const voiceCards = document.querySelectorAll("#recap-top-voices .voice-card, #recap-all-voices-grid .voice-card");
    const recapPersonaSel = document.getElementById("recap-persona-select");
    function resyncRecapPersonaSelect() {
        const chosen = state.recapVoice || "komin";
        if (recapPersonaSel) {
            const has = Array.from(recapPersonaSel.options).some(o => o.value === chosen);
            if (has) recapPersonaSel.value = chosen;
        }
        voiceCards.forEach(c => c.classList.toggle("active", c.getAttribute("data-voice") === chosen));
        if (typeof refreshRecapStep4Choices === "function") refreshRecapStep4Choices();
    }
    window.resyncRecapPersonaSelect = resyncRecapPersonaSelect;
    if (recapPersonaSel && !recapPersonaSel.dataset.vcpWired) {
        recapPersonaSel.dataset.vcpWired = "1";
        recapPersonaSel.addEventListener("change", () => {
            haptic("selection");
            state.recapVoice = recapPersonaSel.value || "komin";
            deselectRecapOwnVoice();
            resyncRecapPersonaSelect();
        });
    }
    voiceCards.forEach(card => {
        card.addEventListener("click", () => {
            haptic("selection");
            const voice = card.getAttribute("data-voice");
            state.recapVoice = voice;
            deselectRecapOwnVoice();
            resyncRecapPersonaSelect();
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
    const RECAP_SETTING_BOOLS = ["recapSyncCrop", "recapMirror", "recapBurnSubs", "recapBlurSubs", "recapBlurAutoDetect", "recapLogo", "recapBlurBoxTouched", "recapSubShadow", "recapSubBold", "recapEmbedSoftSubs"];
    const RECAP_SETTING_INTS = {
        recapCropTop: [0, 35], recapCropBottom: [0, 35],
        recapCropLeft: [0, 35], recapCropRight: [0, 35],
        recapFontSize: [10, 30], recapLines: [1, 3],
        recapBlurY: [0, 100], recapBlurH: [1, 50], recapSubY: [0, 100],
        recapSubMargin: [0, 95], recapSettingsVersion: [0, 10],
        recapBlurX: [0, 100], recapBlurW: [10, 100],
        recapSubX: [10, 90], recapSubW: [10, 100],
        recapBgOpacity: [0, 100],
        recapBlurStrength: [4, 40],
    };

    function snapshotRecapSettings() {
        syncLockedRecapDefaultsToState();
        const snap = {};
        Object.keys(RECAP_SETTING_ENUMS).forEach(k => { snap[k] = state[k]; });
        RECAP_SETTING_BOOLS.forEach(k => { snap[k] = !!state[k]; });
        Object.keys(RECAP_SETTING_INTS).forEach(k => { snap[k] = state[k]; });
        snap.recapZoom = state.recapZoom;
        snap.recapVoice = state.recapVoice;
        snap.recapGeminiVoice = state.recapGeminiVoice || "";
        snap.recapEdgeVoice = normalizeEdgeVoiceKey(state.recapEdgeVoice || "thiha");
        snap.recapSplitEnabled = false;
        const by = document.getElementById("slider-blur-y");
        const bh = document.getElementById("slider-blur-h");
        const bs = document.getElementById("slider-blur-strength");
        const sy = document.getElementById("slider-sub-y");
        snap.recapBlurY = by ? parseInt(by.value) : 76;
        snap.recapBlurH = bh ? parseInt(bh.value) : 7;
        snap.recapBlurStrength = bs ? parseInt(bs.value) : 18;
        snap.recapSubMargin = sy ? parseInt(sy.value) : 5;
        snap.recapSettingsVersion = 3;
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
        if ((parseInt(saved.recapSettingsVersion) || 0) < 3) {
            if (saved.recapSubStyle === "yellow") saved.recapSubStyle = "cinematic";
            if (parseInt(saved.recapFontSize) === 16) saved.recapFontSize = 13;
            if (saved.recapStoryStyle === "viral") saved.recapStoryStyle = "pov";
            if (saved.recapProtection === "standard") saved.recapProtection = "strong";
            if (parseFloat(saved.recapZoom) === 1 || parseFloat(saved.recapZoom) === 1.0) saved.recapZoom = 1.1;
            if (saved.recapTtsEngine === "gemini") saved.recapTtsEngine = "voxcpm";
            saved.recapBlurAutoDetect = false;
            saved.recapSettingsVersion = 3;
        }
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
        if (typeof saved.recapEdgeVoice === "string" && saved.recapEdgeVoice) {
            state.recapEdgeVoice = normalizeEdgeVoiceKey(saved.recapEdgeVoice);
        }
        state.recapSourceLanguage = "auto";
        // Batch DD restores (strings / list / band).
        if (typeof saved.recapTextColor === "string") state.recapTextColor = saved.recapTextColor;
        if (typeof saved.recapOutlineColor === "string") state.recapOutlineColor = saved.recapOutlineColor;
        if (typeof saved.recapBgColor === "string") state.recapBgColor = saved.recapBgColor;
        if (typeof saved.recapOutlineWidth === "number") state.recapOutlineWidth = saved.recapOutlineWidth;
        if (typeof saved.recapSubFontCustomId === "string") state.recapSubFontCustomId = saved.recapSubFontCustomId;
        if (typeof saved.recapSubFontCustomName === "string") state.recapSubFontCustomName = saved.recapSubFontCustomName;
        if (Array.isArray(saved.recapBlurBoxes)) state.recapBlurBoxes = saved.recapBlurBoxes.filter(b => b && typeof b === "object").slice(0, 5);
        if (["top", "center", "bottom"].includes(saved.recapSubPosBand)) state.recapSubPosBand = saved.recapSubPosBand;
        syncLockedRecapDefaultsToState();
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
        if (canvas) canvas.classList.toggle("mirrored", !!state.recapMirror);
        syncPillGroup("#recap-speed-picker .speed-seg", "data-speed", state.recapSpeed);
        refreshRecapQualityLock();
        syncPillGroup("#recap-quality-picker .pill-btn", "data-quality", state.recapRenderQuality);
        refreshRecapEngineLock();
        const engineSelEl = document.getElementById("recap-engine-picker");
        if (engineSelEl) engineSelEl.value = state.recapTtsEngine || "gemini";
        if (geminiVoiceSel) geminiVoiceSel.value = state.recapGeminiVoice || "";
        if (edgeVoiceSel) edgeVoiceSel.value = normalizeEdgeVoiceKey(state.recapEdgeVoice || "thiha");
        syncRecapEngineAreas();
        document.querySelectorAll("#story-style-picker-quick .style-select-pill").forEach(p => {
            p.classList.toggle("active", p.getAttribute("data-story") === state.recapStoryStyle);
        });
        if (typeof resyncRecapPersonaSelect === "function") resyncRecapPersonaSelect();
        else document.querySelectorAll("#recap-top-voices .voice-card, #recap-all-voices-grid .voice-card").forEach(c => {
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
        if (typeof updateSubtitleVisualPreview === "function") updateSubtitleVisualPreview();
        // Batch AK: the handle's classes are fixed (the style lives
        // in the composited frame, not in CSS classes).
        if (previewSub) previewSub.className = "preview-sub-box draggable-box";
        if (typeof ddSyncCustomFontUi === "function") ddSyncCustomFontUi();
        const logoPosSelEl = document.getElementById("recap-logo-pos-picker");
        if (logoPosSelEl) logoPosSelEl.value = state.recapLogoPos || "top-right";
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
        if (typeof refreshRecapStep4Choices === "function") refreshRecapStep4Choices();
        if (typeof updateRecapSplitCostHint === "function") updateRecapSplitCostHint();
    }

    const recapTabEl = document.getElementById("tab-recap");
    if (recapTabEl) {
        ["click", "change", "input", "pointerup"].forEach(evt =>
            recapTabEl.addEventListener(evt, saveRecapSettingsSoon));
    }
    restoreRecapSettings();
    (function syncInitialRecapDefaultsUi() {
        const initEngineSel = document.getElementById("recap-engine-picker");
        if (initEngineSel) initEngineSel.value = state.recapTtsEngine || "gemini";
        if (typeof syncRecapEngineAreas === "function") syncRecapEngineAreas();
        const subStyleSel = document.getElementById("sub-style-picker");
        if (subStyleSel && state.recapSubStyle) subStyleSel.value = state.recapSubStyle;
        if (typeof updateSubtitleVisualPreview === "function") updateSubtitleVisualPreview();
        syncSubtitleFontSize(state.recapFontSize || 13);
        syncLockedRecapDefaultsToState();
        document.querySelectorAll("#story-style-picker-quick .style-select-pill").forEach(p => {
            p.classList.toggle("active", p.getAttribute("data-story") === state.recapStoryStyle);
        });
        moveSegPills();
        const autoEl = document.getElementById("recap-blur-autodetect-toggle");
        if (autoEl) autoEl.checked = state.recapBlurAutoDetect !== false;
        if (!state.recapBlurBoxTouched) {
            const by = document.getElementById("slider-blur-y");
            if (by && !localStorage.getItem(RECAP_SETTINGS_KEY)) {
                by.value = 76;
                syncBlurBoxVisual();
            }
        }
        if (typeof ddSyncControlsFromState === "function") ddSyncControlsFromState();
        if (typeof resyncRecapPersonaSelect === "function") resyncRecapPersonaSelect();
        if (typeof refreshRecapStep2Summary === "function") refreshRecapStep2Summary();
    })();

    // Batch AM (M8ii): account prefs land AFTER this boot restore
    window.__RECAP_SETTINGS_KEY = RECAP_SETTINGS_KEY;
    window.restoreRecapSettings = restoreRecapSettings;
    window.__ddFitPreviewFrame = typeof ddFitPreviewFrame === "function" ? ddFitPreviewFrame : null;
    window.refreshRecapStep2Summary = typeof refreshRecapStep2Summary === "function" ? refreshRecapStep2Summary : null;
    window.refreshRecapStep4Output = typeof refreshRecapStep4Output === "function" ? refreshRecapStep4Output : null;
    window.resyncRecapControlsAfterPrefs = function () {
        restoreRecapSettings();
    };

    // Batch L: "Re-run with these settings" (History detail) — map a
    window.prefillRecapFromHistory = function(cfg, item) {
        if (!cfg || typeof cfg !== "object") return;
        const lockedCrop = recapLockedCropForRatio(cfg.ratio);
        const snap = {
            recapRatio: cfg.ratio, recapRatioFill: RECAP_LOCKED_FILL,
            recapSpeed: normalizeSpeedOption(cfg.voice_speed), recapStoryStyle: cfg.story_style,
            recapSubStyle: cfg.sub_style, recapSubFont: cfg.sub_font,
            recapLogoPos: cfg.logo_pos, recapSourceLanguage: cfg.source_language,
            recapRenderQuality: (state.user && state.user.is_admin) ? cfg.render_quality : "standard",
            recapTtsEngine: cfg.tts_engine,
            recapMirror: RECAP_LOCKED_MIRROR,
            recapBurnSubs: cfg.burn_subtitles !== false,
            recapBlurSubs: cfg.blur_subtitles !== false,
            recapLogo: cfg.include_logo !== false,
            recapCropTop: lockedCrop.crop_top, recapCropBottom: lockedCrop.crop_bottom,
            recapCropLeft: lockedCrop.crop_left, recapCropRight: lockedCrop.crop_right,
            recapFontSize: cfg.sub_font_size, recapLines: cfg.sub_lines,
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
            recapProtection: RECAP_LOCKED_PROTECTION,
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
            recapZoom: RECAP_LOCKED_ZOOM, recapVoice: cfg.voice_slug,
            recapGeminiVoice: cfg.gemini_voice || "",
            recapSubRenderMode: cfg.sub_render_mode || "timeline",
            recapSubEffect: cfg.sub_effect || "none",
            recapEmbedSoftSubs: cfg.embed_soft_subs !== false,
            recapSplitEnabled: false,
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
            state.recapLoadedSourceKey = "url:" + srcUrl;
            const urlInput = document.getElementById("recap-url-input");
            if (urlInput) urlInput.value = srcUrl;
            const btnUrlMode = document.getElementById("btn-source-mode-url");
            if (btnUrlMode) btnUrlMode.click();
            resetRecapUrlVerification({ clearInfo: true, hideError: true });
            const btnCheck = document.getElementById("btn-fetch-recap-info");
            if (btnCheck && !btnCheck.disabled) btnCheck.click();
        } else if (srcUrl && item && item.source_available) {
            state.uploadedVideoPath = srcUrl;
            state.recapUploadPending = false;
            state.recapLoadedSourceKey = "file:" + srcUrl;
            const btnFileMode = document.getElementById("btn-source-mode-file");
            if (btnFileMode) btnFileMode.click();
            hideRecapUrlError();
            setRecapPreviewState("ready");
        } else {
            // Upload source is gone: settings are filled, the user
            // picks a source again (the toast explains prefill).
            state.uploadedVideoPath = null;
            resetRecapUrlVerification({ clearInfo: true, hideError: true });
        }
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

// Batch AV: window.toggleAdvancedRecapStudio was retired with the

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

function buildRecapConfig(url) {
    const sliderBlurY = document.getElementById("slider-blur-y");
    const sliderBlurH = document.getElementById("slider-blur-h");
    const sliderBlurX = document.getElementById("slider-blur-x");
    const sliderBlurW = document.getElementById("slider-blur-w");
    const sliderBlurStrength = document.getElementById("slider-blur-strength");
    const sliderSubY = document.getElementById("slider-sub-y");
    const sliderSubX = document.getElementById("slider-sub-x");
    const sliderSubW = document.getElementById("slider-sub-w");
    const personaSel = document.getElementById("recap-persona-select");
    const edgeSel = document.getElementById("recap-edge-voice-select");
    const geminiSel = document.getElementById("recap-gemini-voice-select");

    const lockedCrop = recapLockedCropForRatio(state.recapRatio);
    const activeEngine = state.recapTtsEngine || "voxcpm";
    const activePersona = (personaSel && personaSel.value) || state.recapVoice || "komin";
    const activeGemini = (geminiSel ? geminiSel.value : state.recapGeminiVoice) || "";
    const activeEdge = normalizeEdgeVoiceKey((edgeSel && edgeSel.value) || state.recapEdgeVoice || "thiha");

    return {
        url: url,
        title: state.recapVideoTitle || null,
        voice_slug: activePersona,
        story_style: state.recapStoryStyle || "pov",
        ratio: state.recapRatio || "9:16",
        ratio_fill: RECAP_LOCKED_FILL,
        crop_top: lockedCrop.crop_top,
        crop_bottom: lockedCrop.crop_bottom,
        crop_left: lockedCrop.crop_left,
        crop_right: lockedCrop.crop_right,
        mirror_video: RECAP_LOCKED_MIRROR,
        zoom_factor: RECAP_LOCKED_ZOOM,
        voice_speed: state.recapSpeed || "+12%",
        blur_subtitles: state.recapBlurSubs !== false,
        blur_y: sliderBlurY ? parseInt(sliderBlurY.value) : 85,
        blur_h: sliderBlurH ? parseInt(sliderBlurH.value) : 7,
        blur_x: sliderBlurX ? parseInt(sliderBlurX.value) : 4,
        blur_w: sliderBlurW ? parseInt(sliderBlurW.value) : 92,
        blur_box_explicit: !!state.recapBlurBoxTouched && !(Array.isArray(state.recapBlurBoxes) && state.recapBlurBoxes.length),
        blur_strength: sliderBlurStrength ? parseInt(sliderBlurStrength.value) : 18,
        blur_auto_detect: state.recapBlurAutoDetect !== false,
        burn_subtitles: state.recapBurnSubs !== false,
        sub_bottom_margin: (state.recapSubPosBand === "top" || state.recapSubPosBand === "center") ? null : (sliderSubY ? parseInt(sliderSubY.value) : (state.recapSubMargin != null ? state.recapSubMargin : 5)),
        sub_y: (state.recapSubPosBand === "top") ? 15 : (state.recapSubPosBand === "center") ? 50 : undefined,
        sub_x: sliderSubX ? parseInt(sliderSubX.value) : 50,
        sub_w: sliderSubW ? parseInt(sliderSubW.value) : 92,
        sub_style: state.recapSubStyle || "cinematic",
        sub_font_size: state.recapFontSize || 13,
        sub_font: state.recapSubFont || "padauk",
        sub_lines: state.recapLines || 1,
        include_logo: state.recapLogo !== false,
        logo_pos: state.recapLogoPos || "top-right",
        logo_scale: Number(state.recapLogoScale || 0.16),
        logo_opacity: Number(state.recapLogoOpacity || 0.70),
        ...ddConfigExtras(),
        duration_seconds: state.recapSourceDuration || null,
        script_review: !!((document.getElementById("recap-script-review-toggle") || {}).checked),
        source_language: "auto",
        render_quality: canUseRecapRenderQuality() ? (state.recapRenderQuality || "standard") : "standard",
        tts_engine: activeEngine,
        gemini_voice: (activeEngine === "gemini" && activeGemini) ? activeGemini : null,
        edge_voice: activeEngine === "edge" ? activeEdge : null,
        voice_ref_id: (activeEngine === "voxcpm" && state.recapOwnVoiceSelected && state.recapOwnVoiceStatus === "ready")
            ? state.recapOwnVoiceRefId
            : null,
        split_video: false,
        split_minutes: 3,
    };
}
if (typeof window !== "undefined") window.buildRecapConfig = buildRecapConfig;

async function handleStartRecap() {
    // Batch AC: a parallel submit is explained IN PLACE (limit text +
    if (recapJobIsActive()) showLiveSubmitLimit("recap");
    if (recapJobIsActive()) return; // a recap job is already running
    // Hard guard: a picked file whose upload has not succeeded is NOT
    // a recap source — never submit on the strength of a local pick.
    if (state.recapUploadPending) {
        showStudioToast(t("toast_recap_not_uploaded"), 'warning');
        return;
    }
    // Batch DF: an uploaded file with no audio track can never be
    if (state.recapNoAudio) {
        showStudioToast(t("recap_no_audio"), 'warning');
        return;
    }
    // Batch BV: same rule for the own-voice sample — a selected
    if (state.recapOwnVoiceSelected && state.recapTtsEngine === "voxcpm"
        && state.recapOwnVoiceStatus !== "ready") {
        showStudioToast(t("recap_own_voice_pending_error"), 'warning');
        return;
    }
    const urlInput = document.getElementById("recap-url-input");
    let url = state.uploadedVideoPath || (urlInput ? urlInput.value.trim() : "");
    if (!state.uploadedVideoPath) {
        if (!url) {
            showStudioToast(t("toast_empty_url"), 'warning');
            return;
        }
        const extractedUrl = extractRecapHttpUrl(url);
        if (
            !isValidRecapHttpUrl(url) ||
            !state.recapVerifiedSourceUrl ||
            !isVerifiedRecapInfoPayload(state.lastRecapInfo)
        ) {
            showRecapUrlError();
            showStudioToast(t("recap_url_verify_err_title"), 'warning');
            showRecapStep(1);
            return;
        }
        url = state.recapVerifiedSourceUrl || extractedUrl;
    }

    haptic("heavy");
    const btn = document.getElementById("btn-start-recap");

    // Enforce the per-job cap: free 2 min, VIP 10 min, admins exempt.
    const maxRecapSec = recapNormalMaxSeconds();
    if (state.recapSourceDuration && state.recapSourceDuration > maxRecapSec
        && !state.user?.is_admin) {
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
    // Round 15 Parts B & C: no inline status strip on Step 1; progress renders
    // honestly from the LiveJobs store inside the dedicated Recap Job sub-view.
    if (progressFill) paintProgressBarFill(progressFill, { status: "QUEUED", progress_percent: 0 });
    setRecapStage("0/4", "QUEUED");
    if (statusMsg) statusMsg.innerText = t("live_job_queued");
    if (stepText) stepText.innerText = "";

    // Batch CB Item 3: the hardcoded submit-time estimate

    // Batch AC duplicate-submit guard, engaged at the last moment
    if (!studioSubmitBegin("recap")) {
        if (btn) btn.disabled = false;
        return;
    }

    try {
        const payload = buildRecapConfig(url);

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
            if (data && data.code === "VOXCPM_GEMINI_SWITCH_REQUIRED") {
                studioSubmitFailure("recap");
                if (progressBox) progressBox.classList.add("hidden");
                if (btn) btn.disabled = false;
                _lastPoolHighDemand = true;
                syncRecapEngineAreas();
                showStudioToast(data.detail || t("recap_clone_high_demand_hint"), "warning", 7500);
                if (typeof switchRecapTtsEngine === "function") {
                    switchRecapTtsEngine("gemini");
                } else {
                    state.recapTtsEngine = "gemini";
                    syncRecapEngineAreas();
                }
                return;
            }
            throw new Error(data.detail || "Recap creation failed");
        }

        state.recapInsufMinutesServer = null;
        state.recapJobId = data.recap_id;
        try { localStorage.setItem("vcp_active_recap_job_id", data.recap_id); } catch (e) {}
        loadUserProfile();
        startRecapJobPolling(data.recap_id);
        studioSubmitSuccess("recap");
        LiveJobs.track({ id: data.recap_id, kind: "recap", status: data.status || "QUEUED", progress_percent: 0, stage_index: 0, stage_total: 4, title: state.recapVideoTitle || "" });
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
let recapFormJobId = null;      // the job the detail form tracks
let recapFormEtaRange = null;   // ETA range extra, fed by the detail poller (Batch CB)
let recapFormEtaOverrun = false; // overrun flag, fed by the detail poller (Batch CB)
let recapFormNotice = null;     // a poller health notice owning the status line
let recapFormStoreSubscribed = false;

function renderRecapFormProgress(job) {
    if (!job) return;
    if (recapFormJobId && job.id && job.id !== recapFormJobId) return;
    if (recapFormNotice) return; // a poller notice owns the status line
    const titleEl = document.getElementById("recap-job-title");
    const statusMsg = document.getElementById("recap-status-msg");
    const stepText = document.getElementById("recap-step-text");
    const progressFill = document.getElementById("recap-progress-fill");
    const cancelBtn = document.getElementById("btn-recap-cancel");
    if (titleEl) {
        const cleanTitle = String(job.title || "").trim();
        titleEl.innerText = cleanTitle;
        titleEl.classList.toggle("hidden", !cleanTitle);
    }
    const formStatusText = (recapFormEtaOverrun && !liveJobsIsTerminal(job.status))
        ? liveJobOverrunText(job)
        : liveJobStageText(job);
    if (statusMsg) statusMsg.innerText = formStatusText;
    if (progressFill) {
        paintProgressBarFill(progressFill, job);
    }
    const resolvedStage = resolveRecapStageIndex(job);
    const stageTotal = Number(job.stage_total) || 4;
    if (cancelBtn) {
        const isTerminal = liveJobsIsTerminal(job.status);
        cancelBtn.classList.toggle("hidden", isTerminal);
        cancelBtn.disabled = isTerminal;
    }
    if (stepText) {
        if (liveJobsIsTerminal(job.status)) {
            stepText.innerText = "";
        } else if (job.status === "WAITING_CHOICE") {
            stepText.innerText = `${t("recap_waiting_choice_detail")} · ${t("recap_waiting_choice_bot_hint")}`;
        } else {
            const stepBits = [];
            if (resolvedStage >= 1) {
                stepBits.push(`${t("recap_step_label")} ${resolvedStage}/${stageTotal}`);
            }
            if (typeof job.queue_position === "number" && job.queue_position > 0) {
                stepBits.push(tf("recap_queue_ahead", { count: job.queue_position }));
            }
            if (!recapFormEtaOverrun && typeof job.elapsed_seconds === "number" && job.elapsed_seconds > 0) {
                stepBits.push(tf("live_job_elapsed", { sec: job.elapsed_seconds }));
            }
            const etaText = recapFormEtaOverrun ? "" : formatEtaRangeText(recapFormEtaRange, "remaining");
            if (etaText) {
                stepBits.push(etaText);
            }
            if (stepBits.length === 0) stepBits.push(t("queue_notice_recap"));
            stepText.innerText = stepBits.join(" • ");
        }
    }
    setRecapStage(`${resolvedStage}/${stageTotal}`, job.status, job.progress_stage);
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
    const statusMsg = document.getElementById("recap-status-msg");
    const stepText = document.getElementById("recap-step-text");
    const btn = document.getElementById("btn-start-recap");

    // Batch AC: the detail view opens BY CHOICE only — polling never
    if (progressBox && state.recapDetailOpen) progressBox.classList.remove("hidden");
    const failedBoxOnPoll = document.getElementById("recap-failed-box");
    if (failedBoxOnPoll) failedBoxOnPoll.classList.add("hidden");

    // Batch BR: this poller's job is the form's tracked job — the
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
        const jobPane = document.getElementById("tab-recap-job");
        if (jobPane && jobPane.classList.contains("active") && typeof window.backToStudio === "function") {
            window.backToStudio("recap");
        }
        showStudioToast(t("poll_job_gone"), 'error', 6000);
    }

    // Polling efficiency (audit fix 2026-11-03): ticks are skipped
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
                const vcChoice = data.voice_choice && data.voice_choice.choice ? data.voice_choice.choice : null;
                if (statusMsg) statusMsg.innerText = vcChoice === "wait" ? t("live_job_status_waiting_capacity") : t("live_job_status_waiting_choice");
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
                LiveJobs.track({ id: jobId, kind: "recap", status: "SUCCESS", progress_percent: 100, stage_index: 4, stage_total: 4 });
                if (btn) btn.disabled = false;
                if (progressBox) progressBox.classList.add("hidden");
                const jobPaneDone = document.getElementById("tab-recap-job");
                if (jobPaneDone && jobPaneDone.classList.contains("active")) {
                    state.recapDetailOpen = false;
                    if (typeof switchTab === "function") switchTab("tab-recap");
                    if (typeof showRecapStep === "function") showRecapStep(4);
                }
                if (resultBox) {
                    resultBox.classList.remove("hidden");
                }
                if (typeof setSubmitButtonState === "function") setSubmitButtonState("recap");
                refreshStickyCtas();

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
                    // the credits this job was actually charged.
                    const costEl = document.getElementById("recap-cost-line");
                    if (costEl) {
                        const charged = data.credits_charged;
                        if (typeof charged === "number" && charged > 0) {
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
                if (typeof recapSubmitGuard !== "undefined") recapSubmitGuard.submittedFingerprint = null;
                LiveJobs.track({ id: jobId, kind: "recap", status: "FAILED", progress_percent: 0 });
                if (btn) btn.disabled = false;
                if (typeof setSubmitButtonState === "function") setSubmitButtonState("recap");
                refreshStickyCtas();
                haptic("error");
                if (progressBox) progressBox.classList.add("hidden");
                const jobPaneFailed = document.getElementById("tab-recap-job");
                if (jobPaneFailed && jobPaneFailed.classList.contains("active")) {
                    state.recapDetailOpen = false;
                    if (typeof switchTab === "function") switchTab("tab-recap");
                    if (typeof showRecapStep === "function") showRecapStep(4);
                }

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
                if (typeof recapSubmitGuard !== "undefined") recapSubmitGuard.submittedFingerprint = null;
                LiveJobs.track({ id: jobId, kind: "recap", status: "CANCELLED", progress_percent: 0 });
                if (btn) btn.disabled = false;
                if (typeof setSubmitButtonState === "function") setSubmitButtonState("recap");
                refreshStickyCtas();
                if (progressBox) progressBox.classList.add("hidden");
                const jobPaneCancelled = document.getElementById("tab-recap-job");
                if (jobPaneCancelled && jobPaneCancelled.classList.contains("active") && typeof window.backToStudio === "function") {
                    window.backToStudio("recap");
                }
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
async function cancelRecapJobById(jobId) {
    if (!jobId) return;
    showStudioConfirm(t("recap_cancel_confirm"), async () => {
        try {
            const res = await fetch(`/api/recap/${jobId}/cancel`, {
                method: "POST",
                headers: getAuthHeaders(),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                if (res.status === 409) {
                    // Round 6 Item 5: job already ended on backend — immediately
                    // reconcile LiveJobs so stale pill/sheet/Step 4 clear.
                    LiveJobs.fetchNow();
                }
                throw new Error(data.detail || t("err_generic"));
            }
            if (state.recapPollTimer) {
                clearInterval(state.recapPollTimer);
                state.recapPollTimer = null;
            }
            if (state.recapJobId === jobId) {
                try { localStorage.removeItem("vcp_active_recap_job_id"); } catch (e) {}
            }
            if (state.recapScriptReadyJobId === jobId) state.recapScriptReadyJobId = null;
            if (typeof recapSubmitGuard !== "undefined") recapSubmitGuard.submittedFingerprint = null;
            LiveJobs.track({ id: jobId, kind: "recap", status: "CANCELLED", progress_percent: 0 });
            const pBox = document.getElementById("recap-progress-box");
            if (pBox) pBox.classList.add("hidden");
            const jobPaneOnCancel = document.getElementById("tab-recap-job");
            if (jobPaneOnCancel && jobPaneOnCancel.classList.contains("active") && typeof window.backToStudio === "function") {
                window.backToStudio("recap");
            }
            const sBox = document.getElementById("recap-script-box");
            if (sBox) sBox.classList.add("hidden");
            const startBtn = document.getElementById("btn-start-recap");
            if (startBtn) startBtn.disabled = false;
            if (typeof setSubmitButtonState === "function") setSubmitButtonState("recap");
            refreshStickyCtas();
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
                    dlDuration.innerText = data.estimated_duration || "";
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

// ── Plan Category Tabs (Package / Voice / Recap — one category at a time) ─
function switchPlanCategory(cat) {
    haptic("selection");
    const target = (cat === "combined" || cat === "package") ? "combo" : (["combo", "voice", "recap"].includes(cat) ? cat : "combo");
    document.querySelectorAll(".plan-tab-btn").forEach(btn => {
        btn.classList.toggle("active", btn.getAttribute("data-plan-cat") === target);
    });
    ["combo", "voice", "recap"].forEach(name => {
        const panel = document.getElementById(`plan-panel-${name}`);
        if (panel) panel.classList.toggle("hidden", name !== target);
    });
}
window.switchPlanCategory = switchPlanCategory;

// ── Payment Drawer & Slip Submission ───────────────────────────────────────
let activePackageCode = "pkg_starter";
let activeAmount = 0;
let planPriceByCode = {};
let _vipPlansCache = [];

function renderDynamicVipPlans(data) {
    const plans = (data && data.plans) || [];
    _vipPlansCache = plans;
    const buckets = { combo: [], voice: [], recap: [] };
    plans.forEach(p => {
        if (p.is_topup) return;
        const c = (p.category === "combined" || p.category === "combo") ? "combo" : (p.category === "recap" ? "recap" : "voice");
        (buckets[c] || buckets.combo).push(p);
    });
    const buyBtnText = escapeHtml(t("buy_btn_label"));
    const bonusBadgeText = escapeHtml(t("buy_badge_bonus_200"));
    Object.keys(buckets).forEach(cat => {
        const grid = document.getElementById(`vip-grid-${cat}`);
        if (!grid) return;
        const list = buckets[cat];
        if (!list.length) { grid.innerHTML = `<p class="text-muted text-sm">${escapeHtml(t("admin_empty"))}</p>`; return; }
        grid.innerHTML = list.map(p => {
            const code = escapeHtml(p.code || p.id);
            const mins = Number(p.minutes || 0);
            const dispMin = (p.display_minutes != null) ? Number(p.display_minutes) : mins;
            const credits = Number(p.recap_credits || 0);
            const priceMm = Number(p.price_mmk || parseInt(String(p.price || "0").replace(/[^0-9]/g, ""), 10) || 0);
            const vdays = (p.validity_days != null) ? Number(p.validity_days) : 30;
            const valText = vdays > 0 ? tf("buy_validity_days", { n: vdays }) : t("buy_validity_no_expiry");

            let titleStr = String(p.name || code);
            let amountHtml = "";
            if (cat === "combo") {
                amountHtml = `<div class="plan-allocation">${escapeHtml(tf("buy_pkg_amount", { minutes: mins, credits }))}</div>`;
            } else if (cat === "voice") {
                titleStr = tf("buy_voice_title", { minutes: dispMin });
            } else if (cat === "recap") {
                titleStr = `${credits} Credits`;
            }

            const bonusHtml = p.badge_key === "bonus_200"
                ? `<span class="vcp-plan-bonus-tag">${bonusBadgeText}</span>`
                : "";
            const safeDrawerTitle = escapeHtml(titleStr).replace(/'/g, "\\'");
            return `
                <div class="card glass vcp-card pricing-card${p.badge_key === "bonus_200" ? " has-bonus-tag" : ""}" id="card-plan-${code}">
                    ${bonusHtml}
                    <div class="plan-name">${escapeHtml(titleStr)}</div>
                    ${amountHtml}
                    <div class="vcp-plan-validity">${escapeHtml(valText)}</div>
                    <div class="plan-price">
                        <span class="price-val" data-price-for="${code}">${priceMm.toLocaleString("en-US")}</span>
                        <span class="currency">MMK</span>
                    </div>
                    <button type="button" class="btn gold-gradient-btn vcp-btn vcp-btn--gold mt-3" onclick="openPaymentDrawer('${code}', ${priceMm}, '${safeDrawerTitle}')">${buyBtnText}</button>
                </div>`;
        }).join("");
    });
}
window.rerenderVipPlans = function rerenderVipPlans() {
    if (Array.isArray(_vipPlansCache) && _vipPlansCache.length) {
        renderDynamicVipPlans({ plans: _vipPlansCache });
    }
};

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
        let expTxt = g.never_expires
            ? (g.source_type === "LEGACY_GRANDFATHER" ? t("vip_grant_legacy") : t("vip_grant_no_expiry"))
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
    document.querySelectorAll("#profile-wallet-orders-section [data-tx-kind]").forEach(b => {
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
const ttsState = { engine: "gemini", speed: "+12%", jobId: null, pollTimer: null };
window.ttsState = ttsState;

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
function paintTtsChipFromQuota(q) {
    if (!q) return;
    const ttsVal = document.getElementById("home-free-tts-value");
    const vipTtsVal = document.getElementById("vip-wallet-tts-val");
    if (q.is_admin || q.unlimited) {
        if (ttsVal) ttsVal.innerText = q.is_admin ? "∞" : t("unlimited");
        if (vipTtsVal) vipTtsVal.innerText = t("unlimited");
    } else {
        const total = (q.referral_seconds || 0) + (q.remaining_seconds || 0);
        const formatted = tf("home_free_value_min", { min: (total / 60).toFixed(1) });
        if (ttsVal) ttsVal.innerText = formatted;
        if (vipTtsVal) vipTtsVal.innerText = formatted;
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

function paintTtsCostHintTiered(q, profile) {
    const u = profile || (typeof state !== "undefined" && state && state.user) || {};
    const isAdmin = Boolean((q && q.is_admin) || u.is_admin);
    const isVip = Boolean(u.is_vip) && !isAdmin;
    const refSec = (q && typeof q.referral_seconds === "number")
        ? q.referral_seconds
        : (typeof u.tts_referral_seconds === "number" ? u.tts_referral_seconds : (u.tts_balance_seconds || 0));
    const hasRef = refSec > 0;
    const maxChars = (u.character_limit || (isAdmin || isVip ? (u.character_limit_purchased ?? 15000) : (u.character_limit_free ?? 3000))).toLocaleString();

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

// Batch BB-4 / Round 8: resilient /api/tts/quota loader (fetchWithTimeout + bounded retries + in-flight latch).
let ttsQuotaInFlight = false;
async function refreshTtsQuota() {
    if (typeof state !== "undefined" && state && state.user) {
        paintTtsCostHintTiered(null, state.user);
    }
    if (ttsQuotaInFlight) return;
    ttsQuotaInFlight = true;
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
                paintTtsChipFromQuota(q);
                paintTtsGeminiKeyState(q);
                paintTtsTasteNote(q);
                return;
            } catch (e) { /* retry, then stop cleanly */ }
        }
    } finally {
        ttsQuotaInFlight = false;
    }
}
window.refreshTtsQuota = refreshTtsQuota;

function updateTtsCharCount() {
    const input = document.getElementById("tts-text-input");
    const counter = document.getElementById("tts-char-count");
    if (input && counter) {
        // Batch BW: the caller's OWN tier limit (same expression the
        // voice counter uses) — before BW this was hardcoded 5,000 for
        // everyone while the server now enforces per-tier.
        const len = (input.value || "").length;
        const max = state.user?.character_limit || (state.user?.is_vip ? (state.user?.character_limit_purchased ?? 15000) : (state.user?.character_limit_free ?? 3000));
        counter.textContent = `${len} / ${max} ${t("char_counter_unit")}`;
        counter.style.color = len > max ? "#ef4444" : "var(--text-muted)";
        syncTtsExcessAndCost(len, max);
    }
}
window.updateTtsCharCount = updateTtsCharCount;
function insertTtsSampleText(txt) {
    const input = document.getElementById("tts-text-input");
    if (input) { input.value = txt; updateTtsCharCount(); input.focus(); }
}
window.insertTtsSampleText = insertTtsSampleText;
function clearTtsText() {
    const input = document.getElementById("tts-text-input");
    if (input) { input.value = ""; updateTtsCharCount(); }
}
window.clearTtsText = clearTtsText;

// ── Batch CG: BYOK Gemini key card (Profile), taste note, Item 3b prompt ──
let _cgQuotaCache = null;

function paintTtsGeminiKeyState(q) {
    const targets = [
        { stateId: "profile-key-state", delId: "btn-profile-key-delete", badgeId: "profile-key-active-badge" },
    ];
    const hasActiveKey = Boolean(q && q.has_own_key && q.own_key_state !== "invalid");
    targets.forEach(({ stateId, delId, badgeId }) => {
        const stateEl = document.getElementById(stateId);
        const delBtn = document.getElementById(delId);
        const activeBadge = document.getElementById(badgeId);
        if (activeBadge) {
            activeBadge.classList.toggle("hidden", !hasActiveKey);
        }
        if (!stateEl) return;
        if (q && q.has_own_key) {
            stateEl.removeAttribute("data-i18n");
            stateEl.textContent = q.own_key_state === "invalid"
                ? t("tts_key_state_invalid")
                : tf("tts_key_state_ok", { masked: q.own_key_masked || "" });
            if (delBtn) delBtn.classList.remove("hidden");
        } else {
            stateEl.setAttribute("data-i18n", "tts_key_state_none");
            stateEl.textContent = t("tts_key_state_none");
            if (delBtn) delBtn.classList.add("hidden");
        }
    });
}

function paintTtsTasteNote(q) {
    const note = document.getElementById("tts-taste-note");
    if (!note) return;
    const remaining = q ? q.gemini_taste_remaining_seconds : null;
    const u = (typeof state !== "undefined" && state && state.user) ? state.user : null;
    const isAdmin = Boolean((q && q.is_admin) || (u && u.is_admin));
    const isPaid = Boolean((q && q.tier === "PURCHASED") || (u && u.is_vip && !isAdmin));
    // Admins and own-key users are never limited by pool taste
    if (isAdmin || remaining === null || remaining === undefined || (q && q.has_own_key)) {
        note.classList.add("hidden");
        note.textContent = "";
        return;
    }
    note.classList.remove("hidden");
    const remKey = isPaid ? "tts_taste_remaining_paid" : "tts_taste_remaining";
    const spentKey = isPaid ? "tts_taste_spent_paid" : "tts_taste_spent";
    note.textContent = remaining > 0
        ? tf(remKey, { min: Math.ceil(remaining / 60) })
        : t(spentKey);
}

function _applyGeminiKeyMutationView(data) {
    if (!data || typeof data !== "object") return;
    const view = Object.assign({}, _cgQuotaCache || {}, {
        has_own_key: Boolean(data.has_key),
        own_key_masked: data.masked || "",
        own_key_state: data.state || "",
    });
    _cgQuotaCache = view;
    paintTtsGeminiKeyState(view);
}

async function saveGeminiKeyFrom(inputId, btnId) {
    const input = document.getElementById(inputId);
    const btn = document.getElementById(btnId);
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
        const profileInput = document.getElementById("profile-key-input");
        if (profileInput) profileInput.value = "";
        _applyGeminiKeyMutationView(data);
        showStudioToast(t("tts_key_saved"), 'success');
        refreshTtsQuota();
    } catch (err) {
        showStudioToast((err && err.message) || t("tts_key_save_failed"), 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = t("tts_key_save_btn"); }
    }
}

async function deleteGeminiKeyFrom(btnId) {
    const btn = document.getElementById(btnId);
    if (btn) btn.disabled = true;
    try {
        const res = await fetchWithTimeout("/api/tts/gemini-key", {
            method: "DELETE",
            headers: getAuthHeaders(),
        }, 30000);
        const data = await res.json().catch(() => ({ has_key: false, masked: null, state: null }));
        if (!res.ok) throw new Error("delete failed");
        const profileInput = document.getElementById("profile-key-input");
        if (profileInput) profileInput.value = "";
        _applyGeminiKeyMutationView(data);
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
    try {
        const res = await fetchWithTimeout("/api/tts/quota", { headers: getAuthHeaders() });
        if (!res.ok) return false;
        const q = await res.json();
        _cgQuotaCache = q;
        paintTtsGeminiKeyState(q);
        paintTtsTasteNote(q);
        const blocked = !q.is_admin && !q.has_own_key
            && q.gemini_taste_remaining_seconds !== null
            && q.gemini_taste_remaining_seconds !== undefined
            && q.gemini_taste_remaining_seconds <= 0;
        if (blocked) {
            const modal = document.getElementById("tts-key-prompt-modal");
            const isPaid = q.tier === "PURCHASED";
            const h3 = modal ? modal.querySelector("h3") : null;
            const p = modal ? modal.querySelector("p.modal-desc") : null;
            if (h3) h3.textContent = t(isPaid ? "tts_key_prompt_title_paid" : "tts_key_prompt_title");
            if (p) p.textContent = t(isPaid ? "tts_key_prompt_body_paid" : "tts_key_prompt_body");
        }
        return blocked;
    } catch (e) {
        return false;
    }
}

function _cgPromptAddKey() {
    const modal = document.getElementById("tts-key-prompt-modal");
    if (modal) modal.classList.add("hidden");
    if (typeof openProfileView === "function") openProfileView();
    else if (typeof switchTab === "function") switchTab("tab-profile");
    const card = document.getElementById("profile-gemini-key-card");
    if (card && typeof card.scrollIntoView === "function") {
        card.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    const input = document.getElementById("profile-key-input");
    if (input && typeof input.focus === "function") {
        setTimeout(() => input.focus(), 350);
    }
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
function applyTtsEngineVisibility() {
    const engine = ttsState.engine || "gemini";
    const gRow = document.getElementById("tts-gemini-voice-row");
    const eRow = document.getElementById("tts-edge-voice-row");
    if (gRow) gRow.classList.toggle("hidden", engine !== "gemini");
    if (eRow) eRow.classList.toggle("hidden", engine !== "edge");
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
                const maxC = state.user?.character_limit || (state.user?.is_vip ? (state.user?.character_limit_purchased ?? 15000) : (state.user?.character_limit_free ?? 3000));
                syncTtsExcessAndCost((inp.value || "").length, maxC);
            }
        });
    });
    const input = document.getElementById("tts-text-input");
    if (input) {
        input.addEventListener("input", () => {
            updateTtsCharCount();
        });
    }
    // Batch CG: Profile Gemini key wiring + Item 3b prompt.
    const profileKeySaveBtn = document.getElementById("btn-profile-key-save");
    if (profileKeySaveBtn) profileKeySaveBtn.addEventListener("click", () => saveGeminiKeyFrom("profile-key-input", "btn-profile-key-save"));
    const profileKeyDelBtn = document.getElementById("btn-profile-key-delete");
    if (profileKeyDelBtn) profileKeyDelBtn.addEventListener("click", () => deleteGeminiKeyFrom("btn-profile-key-delete"));
    const promptAddBtn = document.getElementById("btn-tts-prompt-add-key");
    if (promptAddBtn) promptAddBtn.addEventListener("click", _cgPromptAddKey);
    const promptEdgeBtn = document.getElementById("btn-tts-prompt-edge");
    if (promptEdgeBtn) promptEdgeBtn.addEventListener("click", _cgPromptUseEdge);
    updateTtsCharCount();
    // Batch CN: paint the engine-driven surfaces once at init so
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
    const maxChars = state.user?.character_limit || (state.user?.is_vip ? (state.user?.character_limit_purchased ?? 15000) : (state.user?.character_limit_free ?? 3000));
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
                const terminalJob = { id: jobId, kind: "tts", status: "SUCCESS", progress_percent: 100, stage_index: 1, stage_total: 1 };
                syncTtsTerminalState(terminalJob);
                if (typeof LiveJobs !== "undefined" && typeof LiveJobs.track === "function") LiveJobs.track(terminalJob);
                haptic("success");
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
                const terminalJob = { id: jobId, kind: "tts", status: data.status, progress_percent: 0 };
                syncTtsTerminalState(terminalJob);
                if (typeof LiveJobs !== "undefined" && typeof LiveJobs.track === "function") LiveJobs.track(terminalJob);
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
                _resetTtsSubmitControls();
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
                _resetTtsSubmitControls();
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


// ── Shared Utilities (Dates, Sanitization & Slip Image Modal) ───────────
const _ADM_MONTHS_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function admFmtYangon(iso) {
    if (!iso) return "—";
    let raw = String(iso).trim();
    if (!raw) return "—";
    if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}/.test(raw) && !/(Z|[+-]\d{2}:?\d{2})$/i.test(raw)) {
        raw = raw.replace(" ", "T") + "Z";
    }
    const d = new Date(raw);
    if (isNaN(d.getTime())) return String(iso);
    try {
        const toParts = (dt) => {
            const map = {};
            new Intl.DateTimeFormat("en-GB", {
                timeZone: "Asia/Yangon",
                year: "numeric",
                month: "numeric",
                day: "numeric",
                hour: "numeric",
                minute: "2-digit",
                hour12: true,
            }).formatToParts(dt).forEach(p => { map[p.type] = p.value; });
            return map;
        };
        const p = toParts(d);
        const nowP = toParts(new Date());
        const mer = String(p.dayPeriod || "").toUpperCase();
        const timeStr = `${p.hour}:${p.minute}${mer ? " " + mer : ""}`;
        if (p.year === nowP.year && p.month === nowP.month && p.day === nowP.day) {
            const todayWord = (typeof currentLang !== "undefined" && currentLang === "en")
                ? "Today"
                : (typeof t === "function" ? t("adm_today_word") : "Today");
            return `${todayWord} · ${timeStr}`;
        }
        const mIdx = Math.max(0, Math.min(11, parseInt(p.month, 10) - 1));
        return `${parseInt(p.day, 10)} ${_ADM_MONTHS_SHORT[mIdx]} ${p.year} · ${timeStr}`;
    } catch (e) {
        return raw;
    }
}
window.admFmtYangon = admFmtYangon;

function admFmtDate(iso) {
    return admFmtYangon(iso);
}
window.admFmtDate = admFmtDate;
window.admFmtYangon = admFmtYangon;
window.admFmtDate = admFmtDate;
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
window.openSlipModal = openSlipModal;
window.closeSlipModal = closeSlipModal;
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
window.sanitizeCssMediaUrl = sanitizeCssMediaUrl;
window.escapeHtml = escapeHtml;

// ── Dedicated History Hub ────────────────────────────────────────────────
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
    const rawTs = String(j.created_at).trim();
    const isoTs = /Z$|[+-]\d{2}:?\d{2}$/i.test(rawTs) ? rawTs : (rawTs + "Z");
    const createdMs = Date.parse(isoTs);
    if (Number.isNaN(createdMs)) {
        return { expired: false, expiresSoon: false, hoursLeft: null, minsLeft: null, timeText: "" };
    }
    const ageMs = Date.now() - createdMs;
    const remainingMs = Math.max(0, 24 * 3600000 - ageMs);
    if (remainingMs <= 0) {
        return { expired: true, expiresSoon: false, hoursLeft: 0, minsLeft: 0, timeText: "" };
    }
    const hoursLeft = remainingMs >= 3600000 ? Math.floor(remainingMs / 3600000) : Math.max(0, Math.ceil(remainingMs / 3600000));
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
    if (isRecap && j.status === "SUCCESS" && !(j.parts_count > 1)
        && typeof j.credits_charged === "number" && j.credits_charged > 0) {
        inner += `<div class="history-item-body">${escapeHtml(tf("recap_cost_charged_line", { credits: j.credits_charged }))}</div>`;
    }
    if (isRecap && j.status === "FAILED" && j.credit_refunded) {
        inner += `<div class="history-item-body">${escapeHtml(tf("recap_refunded_line", { credits: j.credits_refunded || j.credits_charged || 1 }))}</div>`;
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
                    <span class="hub-row-meta">${isRecap ? escapeHtml(t("hist_type_recap")) + " • " : isTts ? escapeHtml(t("job_badge_tts")) + " • " : ""}${escapeHtml(j.voice_display || j.preset_voice || "")}${durText ? ` <span class="hub-duration-pill">⏱️ ${escapeHtml(durText)}</span>` : ""} <span class="hub-time-ago">🕒 ${escapeHtml((() => { try { if (!j.created_at) return j.time_ago || ""; const r = String(j.created_at).trim(); const ms = Date.parse(/Z$|[+-]\d{2}:?\d{2}$/i.test(r) ? r : r + "Z"); if (Number.isNaN(ms)) return j.time_ago || ""; const dSec = Math.max(0, Math.floor((Date.now() - ms) / 1000)); if (dSec < 60) return t("time_ago_just_now"); if (dSec < 3600) return tf("time_ago_m", { n: Math.floor(dSec / 60) }); if (dSec < 86400) return tf("time_ago_h", { n: Math.floor(dSec / 3600) }); return tf("time_ago_d", { n: Math.floor(dSec / 86400) }); } catch (e) { return j.time_ago || ""; } })())}</span><span class="hub-live-meta"></span></span>
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
        const cleanName = (n) => (n && !/^voice\s*clone\s*pro$/i.test(String(n).trim())) ? String(n).trim() : "";
        const displayName = cleanName(data.first_name) || cleanName(data.username) || cleanName(tgFirst) || "";
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
let _welcomeTryAudio = null;
let _welcomeTryLoading = false;
async function playWelcomeTrySample() {
    haptic("light");
    if (_welcomeTryLoading) return;
    const syncTryLabel = () => {
        const tryBtn = document.getElementById("welcome-try-sample-btn");
        if (!tryBtn || typeof t !== "function") return;
        const playing = !!(_welcomeTryAudio && !_welcomeTryAudio.paused && !_welcomeTryAudio.ended);
        tryBtn.textContent = t(playing ? "welcome_try_sample_playing" : "welcome_try_sample_btn");
    };
    if (!_welcomeTryAudio) {
        _welcomeTryAudio = new Audio();
        _welcomeTryAudio.addEventListener("play", syncTryLabel);
        _welcomeTryAudio.addEventListener("pause", syncTryLabel);
        _welcomeTryAudio.addEventListener("ended", syncTryLabel);
    }
    if (_welcomeTryAudio.paused || _welcomeTryAudio.ended) {
        if (!_welcomeTryAudio.src) {
            _welcomeTryLoading = true;
            try {
                const authUrl = await fetchAuthImageUrl("/api/voices/sample/komin");
                if (!authUrl) return;
                _welcomeTryAudio.src = authUrl;
            } finally {
                _welcomeTryLoading = false;
            }
        }
        const p = _welcomeTryAudio.play();
        if (p && p.catch) p.catch(() => {});
    } else {
        _welcomeTryAudio.pause();
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
    const tutCard = document.getElementById("home-tutorials-card");
    if (tutCard) tutCard.setAttribute("aria-hidden", isOpen ? "false" : "true");
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
        const estText = estMin ? " " + tf("est_cost_min_badge", { min: estMin }) : "";
        if (costBadge.dataset) costBadge.dataset.estText = estText;
        const busy = typeof voiceSubmitGuard !== "undefined" && !!voiceSubmitGuard.submittedFingerprint;
        costBadge.textContent = busy ? "" : estText;
        if (costBadge.classList && costBadge.classList.toggle) costBadge.classList.toggle("hidden", busy);
    }
}
window.syncVoiceExcessAndCost = syncVoiceExcessAndCost;

function trimVoiceTextToLimit() {
    haptic("light");
    const input = document.getElementById("voice-text-input");
    if (!input) return;
    const u = state.user || {};
    const max = u.character_limit || (u.is_vip ? (u.character_limit_purchased ?? 15000) : (u.character_limit_free ?? 3000));
    if (input.value.length > max) {
        input.value = input.value.slice(0, max);
        input.dispatchEvent(new Event("input"));
    }
}
window.trimVoiceTextToLimit = trimVoiceTextToLimit;

function setCustomCloneStep(stepNum, file) {
    state.customCloneStep = stepNum;
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
    [["saved-custom-voice-bar", "saved-custom-voice-label"], ["recap-saved-voice-bar", "recap-saved-voice-label"]].forEach(([bId, lId]) => {
        const bar = document.getElementById(bId);
        const lbl = document.getElementById(lId);
        if (bar) bar.classList.toggle("hidden", !id);
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
    syncRecapEngineAreas();
    syncRecapOwnVoiceCard();
    if (typeof refreshRecapStep4Choices === "function") refreshRecapStep4Choices();
    showStudioToast(t("recap_own_voice_saved_toast"), "success");
}
window.applySavedCustomVoiceToRecap = applySavedCustomVoiceToRecap;

async function saveCurrentRefAsCustomVoice() {
    haptic("light");
    const file = state.selectedReferenceFile;
    if (!file) return;
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
        const estText = estMin ? " " + tf("est_cost_min_badge", { min: estMin }) : "";
        if (costBadge.dataset) costBadge.dataset.estText = estText;
        const busy = typeof ttsSubmitGuard !== "undefined" && !!ttsSubmitGuard.submittedFingerprint;
        costBadge.textContent = busy ? "" : estText;
        if (costBadge.classList && costBadge.classList.toggle) costBadge.classList.toggle("hidden", busy);
    }
}
window.syncTtsExcessAndCost = syncTtsExcessAndCost;

function trimTtsTextToLimit() {
    haptic("light");
    const input = document.getElementById("tts-text-input");
    if (!input) return;
    const u = state.user || {};
    const max = u.character_limit || (u.is_vip ? (u.character_limit_purchased ?? 15000) : (u.character_limit_free ?? 3000));
    if (input.value.length > max) {
        input.value = input.value.slice(0, max);
        input.dispatchEvent(new Event("input"));
    }
}
window.trimTtsTextToLimit = trimTtsTextToLimit;

document.addEventListener("DOMContentLoaded", () => {
    initTelegram();
    setupViewportAndKeyboardSync();
    applyDesktopDropHint();
    initEntryGate();
    setLanguage(currentLang);
    setupTabs();
    // AZ-4: park the sliding nav pill under the initially-active
    requestAnimationFrame(() => { try { moveNavPill(); } catch (e) {} try { moveSpeedPills(); } catch (e) {} try { moveSegPills(); } catch (e) {} });
    setupVoiceStudio();
    setupRecapVisualEditor();
    initRecapSteps();
    switchRecapStep2Category(state.recapStep2Cat || "subtitle", true);
    if (typeof window.updateSubtitleVisualPreview === "function") window.updateSubtitleVisualPreview();
    setupAlertCarousel();
    setupDownloader();
    setupSlipPreview();
    setupStickyCtas();
    syncTelegramBackButton();

    loadUserProfile();
    loadCuratedVoices();
    loadLegacyModels();
    loadUserHistory();
    checkStartupDeepLinks();
    if (window.__vcpSplash) {
        if (typeof window.__vcpSplash.init === "function") window.__vcpSplash.init();
        if (typeof window.__vcpSplash.notifyInitialTabRendered === "function") window.__vcpSplash.notifyInitialTabRendered();
    }
    // Batch AC: global live-jobs transport + account-bound prefs.
    LiveJobs.start();
    setupAccountPrefs();
    setupSubmitGuards();
    loadAccountPrefs();
});

// ==================== PHASE 4: RECAP STEP 2 SETTINGS GROUPS ====================

function applyActiveRecapStep2Category() {
    const valid = ["subtitle", "blur", "protection", "preset", "frame"];
    const cat = valid.includes(state.recapStep2Cat) ? state.recapStep2Cat : "subtitle";
    state.recapStep2Cat = cat;

    document.querySelectorAll("#recap-step2-cat-tabs [data-step2-cat]").forEach(btn => {
        const match = btn.getAttribute("data-step2-cat") === cat;
        btn.classList.toggle("active", match);
        btn.setAttribute("aria-selected", match ? "true" : "false");
    });

    // Ratio picker stays visible directly under seek chips (collapsed by default)
    const ratioGroup = document.getElementById("recap-preview-ratio-group");
    if (ratioGroup) {
        ratioGroup.classList.remove("hidden");
    }

    const fineWrap = document.querySelector("#recap-step-2 .recap-fine");
    const fineHeader = document.querySelector("#recap-step-2 .recap-fine-header");
    if (fineWrap) fineWrap.classList.remove("hidden");
    if (fineHeader) fineHeader.classList.add("vcp-tab-mode-hidden");

    if (typeof toggleRecapFineSettings === "function") {
        toggleRecapFineSettings(true);
    }

    [
        "recap-preview-ratio-group",
        "recap-subtitle-group",
        "recap-blur-group",
        "recap-logo-group",
    ].forEach(id => {
        const grp = document.getElementById(id);
        if (grp) grp.classList.remove("hidden");
    });

    const blurBox = document.getElementById("preview-blur-box");
    if (blurBox) blurBox.classList.add("label-on");

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
        if (state && state.user && typeof paintHomeTilePeriods === "function") {
            paintHomeTilePeriods(state.user, window._lastTtsQuota || null);
        }
        if (typeof refreshPlanPrices === "function") {
            refreshPlanPrices();
        }
        if (typeof paintTtsCostHintTiered === "function") {
            paintTtsCostHintTiered(window._lastTtsQuota || null, (state && state.user) || null);
        }
        if (window._lastTtsQuota && typeof paintTtsChipFromQuota === "function") {
            paintTtsChipFromQuota(window._lastTtsQuota);
        }
        if (typeof _cgQuotaCache !== "undefined" && _cgQuotaCache && typeof paintTtsTasteNote === "function") {
            paintTtsTasteNote(_cgQuotaCache);
        }
        if (Array.isArray(window._lastAdminPricingPlans) && typeof window.renderAdminPricingList === "function") {
            window.renderAdminPricingList(window._lastAdminPricingPlans);
        }
        if (typeof recapFormJobId !== "undefined" && recapFormJobId && typeof LiveJobs !== "undefined" && LiveJobs && LiveJobs.jobs) {
            const trackedRecap = LiveJobs.jobs.get(recapFormJobId);
            if (trackedRecap && typeof renderRecapFormProgress === "function") {
                renderRecapFormProgress(trackedRecap);
            }
        }
        if (typeof updateVoicePreflight === "function") {
            updateVoicePreflight();
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
        if (typeof window.refreshRecapStep2Summary === "function") {
            window.refreshRecapStep2Summary();
        }
        if (typeof window.refreshAdminLanguageSurfaces === "function") window.refreshAdminLanguageSurfaces();
        if (window._lastReferralStats && typeof updateReferUI === "function") {
            updateReferUI(window._lastReferralStats);
        }
        if (state && state.user && typeof paintProfileView === "function") {
            paintProfileView(state.user);
        }
        if (typeof _cgQuotaCache !== "undefined" && _cgQuotaCache && typeof paintTtsGeminiKeyState === "function") {
            paintTtsGeminiKeyState(_cgQuotaCache);
        }
        const tryBtn = document.getElementById("welcome-try-sample-btn");
        if (tryBtn && typeof t === "function") {
            const playing = !!(_welcomeTryAudio && !_welcomeTryAudio.paused && !_welcomeTryAudio.ended);
            tryBtn.textContent = t(playing ? "welcome_try_sample_playing" : "welcome_try_sample_btn");
        }
        const u = (state && state.user) || {};
        const max = u.character_limit || (u.is_vip ? (u.character_limit_purchased ?? 15000) : (u.character_limit_free ?? 3000));
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
        if (typeof window.refreshRecapStep2Summary === "function") {
            window.refreshRecapStep2Summary();
        }
        if (typeof window.ddSyncCustomFontUi === "function") {
            window.ddSyncCustomFontUi();
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
        if (typeof setSubmitButtonState === "function") {
            setSubmitButtonState("voice");
            setSubmitButtonState("recap");
            setSubmitButtonState("tts");
        }
        if (state && state.submitConfirmJob && typeof renderSubmitConfirmSnippet === "function" && typeof LiveJobs !== "undefined" && LiveJobs.jobs) {
            const scJob = LiveJobs.jobs.get(state.submitConfirmJob.id);
            if (scJob) renderSubmitConfirmSnippet(scJob);
        }
        if (state && state.activeJobId && typeof syncVoiceDetailTerminalState === "function" && typeof LiveJobs !== "undefined" && LiveJobs.jobs) {
            const vJob = LiveJobs.jobs.get(state.activeJobId);
            if (vJob && typeof liveJobsIsTerminal === "function" && liveJobsIsTerminal(vJob.status)) {
                syncVoiceDetailTerminalState(vJob);
            }
        }
    } catch (e) {}
}
window.refreshPhase1to4LanguageSurfaces = refreshPhase1to4LanguageSurfaces;

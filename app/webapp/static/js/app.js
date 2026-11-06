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
                <button id="studio-confirm-cancel" class="btn secondary-btn">${typeof t === "function" ? t("confirm_cancel") : "ပယ်ဖျက်မည်"}</button>
                <button id="studio-confirm-ok" class="btn gold-gradient-btn">${typeof t === "function" ? t("confirm_ok") : "သဘောတူ"}</button>
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
        return rawStr;
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
        return rawStr;
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
        return rawStr;
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
    if (s.startsWith('⚠️')) return s.replace(/^⚠️\s*/, '');
    const info = blockedLinkInfo(s);
    if (info.blocked) {
        const head = info.source ? tf('err_link_blocked_named', { source: info.source }) : t('err_link_blocked');
        return head + ' ' + t('err_link_advice_upload');
    }
    return s;
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

// ── State ──────────────────────────────────────────────────────────────────
const state = {
    tg: window.Telegram?.WebApp || null,
    user: null,
    models: [],
    curatedVoices: [],
    voxcpmSubMode: "curated",       // "curated" | "custom"
    selectedPersona: "komin",
    selectedModelSlug: "voxcpm-cloud-ai",
    selectedReferenceFile: null,
    speed: "normal",
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
    // Batch AH: recap title overlay — off by default (the historical
    // Mini App behaviour: no title badge was rendered); the position
    // is the title CENTRE as percents of the picture width/height,
    // the engine's title_position basis. The sliders are the live
    // source of truth while the studio is open.
    recapTitleEnabled: false,
    recapTitleText: "",
    recapTitleX: 50,
    recapTitleY: 12,
    // Batch AA: identity of the last successfully loaded source
    // ("url:<url>" / "file:<server path>") — the new-source reset
    // fires only when a success event brings a DIFFERENT source.
    recapLoadedSourceKey: null,
    recapSplitMinutes: 3,
    recapSourceLanguage: "auto",
    recapRenderQuality: "standard",
    recapTtsEngine: "voxcpm",
    recapGeminiVoice: "",
    recapVoice: "komin",
    recapJobId: null,
    recapPollTimer: null,
    recapScriptReadyJobId: null,
    alertDismissTimer: null,
};

// ── Telegram WebApp Initialization ─────────────────────────────────────────
function initTelegram() {
    if (state.tg) {
        state.tg.ready();
        state.tg.expand();
        try {
            state.tg.setHeaderColor("#dcebf7");
            state.tg.setBackgroundColor("#dcebf7");
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
function recapStageLabels() { return [t("stage_script"), t("stage_voice"), t("stage_video"), t("stage_ready")]; }
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
    const n = parseInt(String(stepStr || "0").split("/")[0], 10) || 0;
    if (n >= 4) renderStageStepper(el, recapStageLabels(), 3, true);
    else if (n === 3) renderStageStepper(el, recapStageLabels(), 2, false);
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
    try { return LiveJobs.activeJobs().some(j => j.kind === "recap"); } catch (e) { return false; }
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

// ── Home sample players: only one plays at a time ──────────────────────
function setupSamplePlayers() {
    const a = document.getElementById("sample-audio-voice");
    const b = document.getElementById("sample-audio-recap");
    if (a && b) {
        a.addEventListener("play", () => { try { b.pause(); } catch (e) {} });
        b.addEventListener("play", () => { try { a.pause(); } catch (e) {} });
    }
    // /api/voices/sample requires the Telegram auth header (privacy
    // fix, hunt #2 V2), which an <audio src> load cannot send — fetch
    // each sample with auth and hand the player a blob URL instead.
    [a, b].forEach(async (el) => {
        if (!el) return;
        const url = el.getAttribute("data-sample-url");
        if (!url) return;
        const authUrl = await fetchAuthImageUrl(url);
        if (authUrl) el.src = authUrl;
    });
    // Batch AY: branded mini-player buttons drive the SAME elements
    // (native controls are gone). Button state is rendered FROM the
    // media events, never from the click, so the one-at-a-time pause
    // above also resets the other row's button + progress.
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
        btn.addEventListener("click", () => {
            if (el.paused || el.ended) {
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
    return ["PENDING", "QUEUED", "RUNNING", "SCRIPT_READY"].includes(String(status || "").toUpperCase());
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
        LiveJobs.jobs.set(job.id, merged);
        LiveJobs.notify(merged, { terminal: liveJobsIsTerminal(merged.status) && !merged._terminalRendered });
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
    if (job.progress_stage) {
        const stageText = t("recap_stage_" + job.progress_stage);
        if (stageText && stageText !== "recap_stage_" + job.progress_stage) return stageText;
    }
    if (job.progress_label) return job.progress_label;
    if (job.kind === "voice") return t("voice_synthesizing");
    return t("recap_processing_msg");
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
    liveJobsPillDismissedSig = liveJobsPillSignature(LiveJobs.activeJobs());
    const pill = document.getElementById("live-jobs-pill");
    if (pill) pill.classList.add("hidden");
}
window.dismissLiveJobsPill = dismissLiveJobsPill;
function renderLiveJobsPill() {
    const pill = document.getElementById("live-jobs-pill");
    if (!pill) return;
    const active = LiveJobs.activeJobs();
    // The 1-Click CTA hide (Batch AK) tracks the same active set.
    if (typeof refreshStickyCtas === "function") refreshStickyCtas();
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
    if (stageEl) stageEl.innerText = LiveJobs.reconnecting ? t("poll_reconnecting") : liveJobStageText(lead);
    if (fillEl) fillEl.style.width = `${Math.min(100, Math.max(4, lead.progress_percent || 0))}%`;
}
function liveJobsSheetRowHtml(job) {
    const isRecap = job.kind === "recap";
    const title = escapeHtml(job.title || (isRecap ? "Movie Recap" : "Voice Synthesis"));
    const pct = Math.min(100, Math.max(0, job.progress_percent || 0));
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
    const metaText = liveJobsIsTerminal(job.status)
        ? escapeHtml(liveJobStageText(job))
        : `${escapeHtml(job.status || "")} · ${escapeHtml(liveJobStageText(job))}`;
    return `<div class="live-job-row" data-live-job-id="${job.id}">
        <span class="hub-type-icon ${isRecap ? "recap" : "voice"}">${isRecap ? "🎬" : "🎙️"}</span>
        <div class="live-job-row-main">
            <div class="live-job-row-title">${title}</div>
            <div class="live-job-row-meta">${metaText}${elapsed ? " · " + escapeHtml(elapsed) : ""}</div>
            <div class="live-job-row-bar"><span class="live-job-row-fill" style="width:${pct}%"></span></div>
        </div>
        <div class="live-job-row-actions">${action}</div>
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
}
window.openLiveJobsSheet = openLiveJobsSheet;
function closeLiveJobsSheet() {
    const sheet = document.getElementById("live-jobs-sheet");
    if (sheet) sheet.classList.add("hidden");
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
}
window.openIncomeDetail = openIncomeDetail;
function closeIncomeDetail() {
    const sheet = document.getElementById("income-detail-sheet");
    if (sheet) sheet.classList.add("hidden");
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
                    const btn = document.getElementById("btn-generate-voice");
                    if (btn) btn.disabled = false;
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
    state.voiceDetailOpen = true;
    const box = document.getElementById("voice-progress-box");
    if (box) {
        box.classList.remove("hidden");
        box.scrollIntoView({ behavior: "smooth" });
    }
    if (jobId && !state.pollTimer) startJobPolling(jobId);
}
window.openVoiceDetail = openVoiceDetail;
function openRecapDetail(jobId) {
    state.recapDetailOpen = true;
    const box = document.getElementById("recap-progress-box");
    if (box) {
        box.classList.remove("hidden");
        box.scrollIntoView({ behavior: "smooth" });
    }
    if (jobId && !state.recapPollTimer) startRecapJobPolling(jobId);
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
function studioConfigFingerprint(kind) {
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
    });
}
function setSubmitButtonState(kind) {
    const guard = kind === "voice" ? voiceSubmitGuard : recapSubmitGuard;
    const btn = document.getElementById(kind === "voice" ? "btn-generate-voice" : "btn-start-recap");
    if (!btn) return;
    const span = btn.querySelector(".btn-text");
    if (span && !span.dataset.acOriginal) span.dataset.acOriginal = span.innerText;
    btn.disabled = !!(guard.inFlight || guard.submittedFingerprint);
    if (span) {
        span.innerText = guard.submittedFingerprint
            ? t("submit_already_creating")
            : (span.dataset.acOriginal || span.innerText);
    }
}
function studioSubmitBegin(kind) {
    const guard = kind === "voice" ? voiceSubmitGuard : recapSubmitGuard;
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
    const guard = kind === "voice" ? voiceSubmitGuard : recapSubmitGuard;
    guardSuccess(guard);
    setSubmitButtonState(kind);
}
function studioSubmitFailure(kind) {
    const guard = kind === "voice" ? voiceSubmitGuard : recapSubmitGuard;
    guardFailure(guard);
    setSubmitButtonState(kind);
}
function setupSubmitGuards() {
    [["tab-voice", "voice"], ["tab-recap", "recap"]].forEach(([tabId, kind]) => {
        const tab = document.getElementById(tabId);
        if (!tab) return;
        const check = () => {
            const guard = kind === "voice" ? voiceSubmitGuard : recapSubmitGuard;
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
        : `${job.status || ""} · ${liveJobStageText(job)}`;
    if (fillEl) fillEl.style.width = `${Math.min(100, Math.max(4, job.progress_percent || 0))}%`;
}
function showSubmitConfirmation(kind, jobId, costText) {
    state.submitConfirmJob = { kind, id: jobId };
    const card = document.getElementById("submit-confirm");
    const costEl = document.getElementById("submit-confirm-cost");
    if (costEl) costEl.innerText = costText || "";
    const tracked = LiveJobs.jobs.get(jobId);
    renderSubmitConfirmSnippet(tracked || {
        status: "QUEUED", kind, progress_percent: kind === "voice" ? 45 : 5,
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
    const pts = [[13, 3.2], [16, 4.0], [19, 4.8], [22, 5.6]];
    let pt = parseFloat(sizePt);
    if (!Number.isFinite(pt)) pt = 16;
    pt = Math.min(22, Math.max(13, pt));
    let frac = null;
    for (let i = 0; i < pts.length - 1; i++) {
        const x0 = pts[i][0], y0 = pts[i][1], x1 = pts[i + 1][0], y1 = pts[i + 1][1];
        if (pt >= x0 && pt <= x1) { frac = y0 + (y1 - y0) * ((pt - x0) / (x1 - x0)); break; }
    }
    if (frac == null) frac = pts[pts.length - 1][1];
    if (parseInt(lines) === 2) frac *= 1.25;
    return frac / 100;
}
function convertSubCentreToMargin(centrePct, fontSizePt, lines) {
    const PH = 608;
    const fp = Math.max(18, Math.round(PH * subtitleFontFractionOfPicture(fontSizePt || 16, lines || 1)));
    const textH = fp * ((parseInt(lines) === 2) ? 2.30 : 1.15);
    const pad = Math.max(10, fp * 0.40);
    const plateFrac = (textH + 2 * pad) / PH;
    const margin = (100 - centrePct) - (plateFrac * 100) / 2;
    return Math.round(Math.min(95, Math.max(0, margin)));
}
window.convertSubCentreToMargin = convertSubCentreToMargin;
function collectAccountPrefs() {
    const bs = document.getElementById("slider-blur-strength");
    const sy = document.getElementById("slider-sub-y");
    const sxEl = document.getElementById("slider-sub-x");
    const swEl = document.getElementById("slider-sub-w");
    const splitToggle = document.getElementById("recap-split-toggle");
    return {
        voice_persona: state.selectedPersona,
        voice_speed: state.speed,
        recapRatio: state.recapRatio,
        recapRatioFill: state.recapRatioFill,
        recapSpeed: state.recapSpeed,
        recapStoryStyle: state.recapStoryStyle,
        recapSubStyle: state.recapSubStyle,
        recapSubFont: state.recapSubFont,
        recapLogoPos: state.recapLogoPos,
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
        // Batch AH: title overlay settings are account preferences
        // (toggle, custom text, centre position).
        recapTitleEnabled: !!state.recapTitleEnabled,
        recapTitleText: state.recapTitleText || "",
        recapTitleX: state.recapTitleX != null ? state.recapTitleX : 50,
        recapTitleY: state.recapTitleY != null ? state.recapTitleY : 12,
        recapBlurStrength: bs ? parseInt(bs.value) : (state.recapBlurStrength || 18),
        recapZoom: state.recapZoom,
        recapVoice: state.recapVoice,
        recapGeminiVoice: state.recapGeminiVoice || "",
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
        state.speed = prefs.voice_speed;
        document.querySelectorAll("#speed-selector .speed-seg").forEach(p => {
            p.classList.toggle("active", p.getAttribute("data-speed") === state.speed);
        });
        moveSpeedPill(document.getElementById("speed-selector"));
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
        // Batch BR: recapTitleEnabled is session-truth — account
        // prefs must not resurrect the title toggle on load either
        // (restore's checkbox re-sync reads state, so applying the
        // key here would silently turn the toggle back ON). The
        // title text/position still restore as before.
        "recapTitleText", "recapTitleX", "recapTitleY",
        "recapZoom", "recapVoice", "recapGeminiVoice",
    ];
    recapKeys.forEach(k => { if (prefs[k] !== undefined) state[k] = prefs[k]; });
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
    try { showStudioToast(t("live_prefs_saved"), "success", 2200); } catch (e) {}
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

// Authenticated image loading: /api/recap/frame and /api/receipts/*
// now require the Telegram header, which a plain <img src> cannot send.
// External thumbnails (YouTube etc.) are returned untouched.
async function fetchAuthImageUrl(url) {
    if (!url || !url.startsWith("/api/")) return url;
    try {
        const res = await fetch(url, { headers: getAuthHeaders(false) });
        if (isAuthFailure(res)) return null;
        if (!res.ok) return null;
        const blob = await res.blob();
        return URL.createObjectURL(blob);
    } catch (e) { return null; }
}
window.fetchAuthImageUrl = fetchAuthImageUrl;

// ── Authenticated result media (round-6 P1) ─────────────────────────
// Generated results live behind the Telegram init-data header, which
// <audio>/<video> elements and plain <a href> clicks cannot send —
// raw API URLs answered 401, so the voice result player sat at
// 0:00/0:00, the WAV download failed, and History's recap player
// never loaded. Fetch the bytes WITH the header and hand over a
// blob: object URL instead (the pattern dlDownloadFile proved for
// the downloader). Per-element object URLs are revoked on replace.
const _authMediaObjectUrls = new WeakMap();

async function fetchAuthBlobUrl(url) {
    if (!url) return null;
    if (!url.startsWith("/api/")) return url;
    try {
        const res = await fetch(url, { headers: getAuthHeaders(false) });
        if (isAuthFailure(res)) return null;
        if (!res.ok) return null;
        const blob = await res.blob();
        return URL.createObjectURL(blob);
    } catch (e) { return null; }
}

// Batch AF: progress-reporting variant for the DOWNLOAD path only.
// The blob design holds the whole file in memory before the save
// prompt appears, so a large render used to be a long, silent,
// apparently-dead wait with no feedback at all. When the response
// can be streamed, report real percentages computed from
// Content-Length; when no total is known, report null and the
// caller paints an indeterminate "downloading…" state. Playback
// keeps using fetchAuthBlobUrl / the signed stream URL — untouched.
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
        // Batch BJ: inside the Telegram WebApp, a URL carrying its
        // own scoped stream token is self-authenticating — hand it
        // to Telegram openLink so the device browser performs a real
        // download. The blob path below cannot save inside mobile
        // WebViews: the programmatic <a download> click on an object
        // URL is silently dropped there (no blob-download handler;
        // iOS WKWebView worst), which left the result and History
        // download buttons dead on phones even though the fetch and
        // its progress paint succeeded. openLink throwing or being
        // unavailable falls through to the blob path unchanged, as
        // do tokenless URLs and plain-browser contexts. The finally
        // below restores the button's AF progress state immediately.
        if (urlCarriesStreamToken(url) && window.Telegram?.WebApp?.openLink) {
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
            osc.start();
            osc.stop(ctx.currentTime + 0.4);
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
    if (!pill || !active || !active.offsetWidth) return;
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

function switchTab(tabId) {
    const navButtons = document.querySelectorAll(".nav-item");
    const targetBtn = document.querySelector(`.nav-item[data-tab="${tabId}"]`);
    if (targetBtn) {
        navButtons.forEach(b => b.classList.remove("active"));
        targetBtn.classList.add("active");
    } else {
        // Panes without a dock button of their own (Downloader / History
        // live under Home's Tools / Library section now) keep Home lit,
        // so the dock always shows where the user is.
        const homeBtn = document.querySelector('.nav-item[data-tab="tab-home"]');
        if (homeBtn) {
            navButtons.forEach(b => b.classList.remove("active"));
            homeBtn.classList.add("active");
        }
    }
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
    // Batch AW: the TTS tab paints its balance line fresh on entry.
    if (tabId === "tab-tts" && typeof window.refreshTtsQuota === "function") {
        window.refreshTtsQuota();
    }
}
window.switchTab = switchTab;

// ── Language Toggle (i18n) ─────────────────────────────────────────────────
async function toggleLanguage() {
    haptic("medium");
    const nextLang = currentLang === "my" ? "en" : "my";
    setLanguage(nextLang);
    // Persist BEFORE the profile re-read below: /api/user/me echoes
    // the stored language_code and loadUserProfile adopts it, so an
    // un-awaited POST lets the stale value win the race and the
    // toggle silently reverts (journey audit 2026-10-31: the toggle
    // appeared completely dead while the server kept the new value,
    // leaving a mixed-language UI on the next cold start).
    try { await window.__langPersistPromise; } catch (e) {}
    // The voices expander label is state-dependent (show vs hide), so
    // re-apply the correct variant after the generic i18n pass.
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
    const navButtons = document.querySelectorAll(".nav-item");
    navButtons.forEach(btn => {
        btn.addEventListener("click", () => {
            const targetTab = btn.getAttribute("data-tab");
            haptic("light");

            navButtons.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");

            document.querySelectorAll(".tab-pane").forEach(pane => {
                pane.classList.remove("active");
            });
            const activePane = document.getElementById(targetTab);
            if (activePane) activePane.classList.add("active");
            markPaneEnter(activePane);
            // Tab switches start at the top of the new pane (premium polish).
            window.scrollTo({ top: 0, behavior: "auto" });
            refreshStickyCtas();
            moveNavPill();

            if (targetTab === "tab-history") {
                loadUserHistory();
            }

            // Batch BI (BI-4): the TTS balance line must paint fresh
            // on nav entry. This hand-rolled handler duplicates
            // switchTab's pane switching but never carried the hook —
            // the AW-era refreshTtsQuota call lived only inside
            // switchTab, which the dock never calls — so tapping the
            // TTS nav button never ran the quota loader at all and
            // the line sat on its placeholder (the real root cause
            // behind the BB/BH "stuck line" reports).
            if (targetTab === "tab-tts" && typeof window.refreshTtsQuota === "function") {
                window.refreshTtsQuota();
            }

            if (targetTab === "tab-admin") {
                loadAdminData();
                loadAdminQueue();
                loadAdminSettings();
                if (window.adminQueueInterval) clearInterval(window.adminQueueInterval);
                window.adminQueueInterval = setInterval(() => {
                    if (document.hidden) return; // audit fix: no re-fetch/re-render while hidden
                    const adminPane = document.getElementById("tab-admin");
                    if (adminPane && adminPane.classList.contains("active")) {
                        loadAdminQueue(true);
                    } else {
                        clearInterval(window.adminQueueInterval);
                    }
                }, 4000);
            } else {
                if (window.adminQueueInterval) {
                    clearInterval(window.adminQueueInterval);
                    window.adminQueueInterval = null;
                }
            }
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
        // Custom logo (paid-only): profile flags drive the settings
        // UI; the signed thumbnail URL comes from the logo endpoint.
        state.logoCanUse = !!data.can_use_logo;
        state.logoHas = !!data.has_custom_logo;
        refreshLogoUi();
        refreshRecapQualityLock();
        refreshRecapEngineLock();
        paintVoiceTtsBalanceLine(data);
        if (data.can_use_logo || data.has_custom_logo) fetchLogoState();
        // Cache the last good profile so the NEXT open paints real
        // balances instantly instead of flashing the gate + 0.0m/0c
        // while revalidating (journey audit 2026-11-02, item 6).
        try { writeCachedUserProfile(data); } catch (e) {}
        // Admins are never entry-gate locked (see entry-gate section).
        if (data.is_admin) unlockEntryGate();
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
        const balMin = document.getElementById("profile-balance-min");
        const balCredits = document.getElementById("profile-balance-credits");
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
            catTag.innerText = data.is_admin ? "ADMIN ⭐" : (data.is_vip ? t("vip_purchased") : t("vip_free"));
            catTag.className = data.is_vip ? "stat-val text-gold" : "stat-val";
        }
        if (balMin) {
            balMin.innerText = data.is_admin ? t("unlimited") : `${data.balance_minutes} mins`;
        }
        if (balCredits) {
            balCredits.innerText = data.is_admin ? t("unlimited") : `${data.recap_credits || 0} credits`;
        }

        if (charCount) {
            const limit = data.character_limit || (data.is_vip ? (data.character_limit_purchased ?? 5000) : (data.character_limit_free ?? 1000));
            charCount.innerText = `0 / ${limit} ${t("char_counter_unit")}`;
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
                    <span class="preview-btn-label">${t("preview_voice")}</span>
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
        countBadge.innerText = `${filtered.length} Personas`;
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
            grid.innerHTML = `<div class="adm-state-error">${escapeHtml(t("home_load_error"))} <button type="button" class="mini-chip" onclick="loadCuratedVoices()">${escapeHtml(t("admin_retry"))}</button></div>`;
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
            state.speed = pill.getAttribute("data-speed");
            moveSpeedPill(document.getElementById("speed-selector"));
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

// Mirrors the header/profile paint in loadUserProfile for the same
// payload shape — keep the two in step when the header changes.
function paintCachedUserHeader(data) {
    const vipBadge = document.getElementById("vip-status-badge");
    const voiceBalChip = document.getElementById("user-voice-bal");
    const recapBalChip = document.getElementById("user-recap-bal");
    const catTag = document.getElementById("profile-category-tag");
    const balMin = document.getElementById("profile-balance-min");
    const balCredits = document.getElementById("profile-balance-credits");
    const charCount = document.getElementById("char-count");
    if (vipBadge) {
        vipBadge.className = data.is_vip ? "vip-badge vip" : "vip-badge free";
        vipBadge.innerText = data.is_admin ? "👑 ADMIN" : (data.is_vip ? "👑 VIP" : "FREE");
    }
    if (voiceBalChip) voiceBalChip.innerText = data.is_admin ? "🎙️ ∞" : `🎙️ ${data.balance_minutes}m`;
    if (recapBalChip) recapBalChip.innerText = data.is_admin ? "🎬 ∞" : `🎬 ${data.recap_credits || 0}c`;
    if (catTag) {
        catTag.innerText = data.is_admin ? "ADMIN ⭐" : (data.is_vip ? t("vip_purchased") : t("vip_free"));
        catTag.className = data.is_vip ? "stat-val text-gold" : "stat-val";
    }
    if (balMin) balMin.innerText = data.is_admin ? t("unlimited") : `${data.balance_minutes} mins`;
    if (balCredits) balCredits.innerText = data.is_admin ? t("unlimited") : `${data.recap_credits || 0} credits`;
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
    paintVoiceTtsBalanceLine(data);
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
// (state.user), the voxcpm pill is disabled with the lock affordance
// while a FREE profile is known, and a saved "voxcpm" preference
// resolves to "gemini" on load for a free profile (the server clamp
// enforces the same rule at submit + execution regardless).
function canUseRecapCloneEngine() {
    const u = state.user || {};
    return !!(u.is_admin || u.account_category === "PURCHASED");
}
function syncRecapEngineAreas() {
    const gv = document.getElementById("recap-gemini-voice-wrap");
    if (gv) gv.classList.toggle("hidden", state.recapTtsEngine !== "gemini");
    const pa = document.getElementById("recap-persona-area");
    if (pa) pa.classList.toggle("hidden", state.recapTtsEngine === "gemini");
}
function refreshRecapEngineLock() {
    const u = state.user;
    const tierKnown = !!(
        u && (u.is_admin !== undefined || typeof u.account_category === "string")
    );
    const canUse = canUseRecapCloneEngine();
    if (tierKnown && !canUse && state.recapTtsEngine === "voxcpm") {
        state.recapTtsEngine = "gemini";
    }
    document.querySelectorAll("#recap-engine-picker .pill-btn").forEach(btn => {
        const engine = btn.getAttribute("data-engine");
        if (engine === "voxcpm") {
            const locked = tierKnown && !canUse;
            btn.disabled = locked;
            btn.classList.toggle("engine-locked", locked);
            btn.setAttribute("aria-disabled", locked ? "true" : "false");
        }
        btn.classList.toggle("active", engine === state.recapTtsEngine);
    });
    moveSegPill(document.getElementById("recap-engine-picker"));
    syncRecapEngineAreas();
}
window.refreshRecapEngineLock = refreshRecapEngineLock;

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
            const formData = new FormData();
            formData.append("text", text);
            formData.append("model_slug", targetModelSlug);
            formData.append("speed", state.speed);
            formData.append("gender_preset", "m2m");
            formData.append("engine", "voxcpm"); // VoxCPM2 only — backend default
            if (presetVoice) formData.append("preset_voice", presetVoice);
            formData.append("reference_audio", state.selectedReferenceFile);

            const headers = {};
            if (state.tg && state.tg.initData) {
                headers["X-Telegram-Init-Data"] = state.tg.initData;
            }
            res = await fetchWithTimeout("/api/voices/generate-with-audio", {
                method: "POST",
                headers: headers,
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
        if (progressFill) progressFill.style.width = "30%";
        if (etaText) etaText.innerText = t("calculating_eta");

        startJobPolling(data.job_id);
        // Batch AC confirmation hand-off: Generate stays in its
        // disabled "already creating" state for THIS configuration
        // (the guard re-arms only when a setting/input changes), a
        // slim inline line carries the job, the pill tracks it, and
        // the confirmation offers Watch / Back to studio.
        studioSubmitSuccess("voice");
        showStudioInlineStatus("voice", t("preparing_msg"));
        LiveJobs.track({ id: data.job_id, kind: "voice", status: "QUEUED", progress_percent: 45, stage_index: 0, stage_total: 1, title: text.slice(0, 70) });
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
    if (state.pollTimer) clearInterval(state.pollTimer);

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
                if (progressFill) progressFill.style.width = "45%";
                if (statusMsg) statusMsg.innerText = t("cloud_queue_waiting");
                if (etaText) etaText.innerText = tf("voice_elapsed", { sec: Math.max(0, Math.round((Date.now() - pollStartedAt) / 1000)) });
            } else if (data.status === "RUNNING") {
                setVoiceStage("working");
                if (progressFill) progressFill.style.width = "80%";
                if (statusMsg) statusMsg.innerText = t("voice_synthesizing");
                if (etaText) etaText.innerText = tf("voice_elapsed", { sec: Math.max(0, Math.round((Date.now() - pollStartedAt) / 1000)) });
            } else if (data.status === "SUCCESS") {
                clearInterval(state.pollTimer);
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
                haptic("error");
                showStudioToast(mapErrorToToast(data.error_message, 'voice'), 'error');
                if (progressBox) progressBox.classList.add("hidden");
                if (btnGen) btnGen.disabled = false;
            } else if (data.status === "CANCELLED") {
                // Final-audit batch M: cancelled on another surface
                // (admin console) — stop instead of polling to the
                // timeout with a stale progress card.
                clearInterval(state.pollTimer);
                if (progressBox) progressBox.classList.add("hidden");
                if (btnGen) btnGen.disabled = false;
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
    const hint = document.getElementById("recap-split-cost-hint");
    if (!hint) return;
    const toggle = document.getElementById("recap-split-toggle");
    const dur = state.recapSourceDuration || 0;
    // The per-part estimate applies only to the segmented path: split
    // on, source over the per-job cap, VIP, non-admin (admins are
    // exempt and never charged). Splitting a ≤cap video afterwards
    // stays inside its single credit, so no estimate is shown there.
    const show = !!(toggle && toggle.checked && dur > recapNormalMaxSeconds()
        && state.user?.is_vip && !state.user?.is_admin);
    // Batch K: while a segmented estimate is known, the start CTA
    // carries the estimated total instead of the static "(1 Credit)";
    // otherwise it falls back to the plain label. Display-only — the
    // submit/charging path never reads the CTA text.
    const ctaText = document.querySelector("#btn-start-recap .btn-text");
    // Batch AO: while tiered pricing is armed (server switch, mirrored
    // on /user/me) the per-part estimate is replaced wholesale by the
    // duration-band quote: ceil(duration / 180) over the probed
    // duration — the same formula and the same duration the server
    // charges from, and the submit response's credits_total confirms
    // the identical number after submit. Shown whenever a duration
    // is known, for every non-admin (band 1 at ≤3 min, the free
    // lane's ≤2 min included, so free users see "→ 1 Credit").
    if (state.user?.tiered_pricing_enabled && dur > 0 && !state.user?.is_admin) {
        const band = Math.max(1, Math.ceil(dur / 180));
        // Batch BE: Max render quality adds +1 credit on top of the
        // band — the quote shows the breakdown (band + surcharge +
        // total), mirroring the server's submit-time computation
        // from the same clamped quality selection.
        const surcharge = state.recapRenderQuality === "max" ? 1 : 0;
        const total = band + surcharge;
        if (ctaText) ctaText.textContent = total > 1
            ? tf("btn_start_recap_credits", { credits: total })
            : t("btn_start_recap");
        let text = surcharge
            ? tf("recap_tiered_cost_estimate_max", { duration: formatRecapQuoteDuration(dur), credits: band, total })
            : tf("recap_tiered_cost_estimate", { duration: formatRecapQuoteDuration(dur), credits: total });
        const lowBalance = (state.user?.recap_credits ?? 0) < total;
        if (lowBalance) text += " " + t("recap_split_cost_low_balance");
        hint.textContent = text;
        hint.style.color = lowBalance ? "#f87171" : "";
        hint.classList.remove("hidden");
        setRecapStep4Quote(total, surcharge ? tf("recap_step4_quote_breakdown", { credits: band }) : null);
        return;
    }
    if (!show) {
        hint.classList.add("hidden");
        hint.textContent = "";
        if (ctaText) ctaText.textContent = t("btn_start_recap");
        setRecapStep4Quote(1);
        return;
    }
    const est = estimateRecapSegmentParts(dur, state.recapSplitMinutes || 3);
    if (ctaText) ctaText.textContent = est > 1
        ? tf("btn_start_recap_credits", { credits: est })
        : t("btn_start_recap");
    let text = tf("recap_split_cost_estimate", { parts: est, credits: est });
    const lowBalance = (state.user?.recap_credits ?? 0) < est;
    if (lowBalance) text += " " + t("recap_split_cost_low_balance");
    hint.textContent = text;
    hint.style.color = lowBalance ? "#f87171" : "";
    hint.classList.remove("hidden");
    setRecapStep4Quote(est);
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

// ── Batch AX: Recap wizard (owner direction 2026-11-06) ──────────
// One step at a time: exactly ONE step card is visible; completed
// steps disappear and their state lives in the 1-2-3-4 indicator
// chips (number, or ✓ when completed). Navigation is free — tapping
// any chip shows that step — and no control value is ever touched by
// navigation. The AV summary strips are gone; their information role
// moved to the chips and the Step 4 choices line (refreshRecapStep4Choices).
// DOM visibility only — no state, payload or prefs keys change here.
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
function refreshRecapStepIndicator() {
    document.querySelectorAll('#recap-steps-nav [data-step-toggle]')
        .forEach(item => {
            const m = parseInt(item.getAttribute("data-step-toggle"), 10);
            item.classList.toggle("is-active", m === recapCurrentStep);
            item.classList.toggle("is-done", !!RECAP_STEP_DONE[m]);
        });
}
window.refreshRecapStepIndicator = refreshRecapStepIndicator;

// The one door (AX-1): hides every step card, shows step n, updates
// the indicator, scrolls the tab back to the top of the wizard.
function showRecapStep(n) {
    if (!document.getElementById("recap-step-" + n)) return;
    recapCurrentStep = n;
    [1, 2, 3, 4].forEach(m => {
        const els = recapStepEls(m);
        if (els.card) els.card.classList.toggle("hidden", m !== n);
    });
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
    if (n === 4) { refreshRecapStep4Output(); }
    // Batch BB-2: the incoming step card may hold the speed capsule —
    // re-measure its pill now that the card has layout.
    requestAnimationFrame(() => { try { moveSpeedPills(); } catch (e) {} try { moveSegPills(); } catch (e) {} });
    const nav = document.getElementById("recap-steps-nav");
    if (nav) nav.scrollIntoView({ behavior: "smooth", block: "start" });
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
    if (voiceEl) voiceEl.textContent = "🎙️ " + (voiceCard ? voiceCard.textContent.trim() : "");
    const ratioEl = document.getElementById("recap-step4-choice-ratio");
    const ratioCard = document.querySelector("#recap-preview-ratio-group .ratio-card.active");
    if (ratioEl) ratioEl.textContent = ratioCard
        ? (ratioCard.getAttribute("data-label") || ratioCard.textContent.trim()) : "";
    const titleSpan = document.getElementById("recap-step4-choice-title");
    if (titleSpan) titleSpan.textContent = "🎬 " + title;
}
window.refreshRecapStep4Choices = refreshRecapStep4Choices;

function setRecapStep4Quote(credits, breakdownText) {
    const el = document.getElementById("recap-step4-quote-credits");
    if (el) el.textContent = String(Math.max(1, parseInt(credits, 10) || 1));
    // Batch BE: the optional breakdown line under the Step-4 total
    // (band + Max surcharge) — shown only when a breakdown applies.
    const bd = document.getElementById("recap-step4-quote-breakdown");
    if (bd) {
        bd.textContent = breakdownText || "";
        bd.classList.toggle("hidden", !breakdownText);
    }
    refreshRecapStep4Choices();
}
window.setRecapStep4Quote = setRecapStep4Quote;

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
        else if (r === "original" && thumb.naturalWidth > 0 && thumb.naturalHeight > 0) {
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
        set("recap-fact-subs", "📝 " + (styleName && styleName !== styleKey ? styleName : (state.recapSubStyle || "yellow")));
    } else {
        set("recap-fact-subs", "📝 " + t("recap_fact_off"));
    }
    set("recap-fact-title", "🏷️ " + t(state.recapTitleEnabled ? "recap_fact_on" : "recap_fact_off"));
    set("recap-fact-blur", "🌫️ " + t(state.recapBlurSubs !== false ? "recap_fact_on" : "recap_fact_off"));
    const qBtn = document.querySelector("#recap-quality-picker .pill-btn.active");
    set("recap-fact-quality", "🎞️ " + (qBtn ? qBtn.textContent.trim() : (state.recapRenderQuality || "standard")));
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
    // Free navigation (AX-1): tapping any indicator chip shows that
    // step — no locking, no forced order, no state touched.
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
    const sliderBlurY = document.getElementById("slider-blur-y");
    const sliderBlurH = document.getElementById("slider-blur-h");
    const sliderBlurX = document.getElementById("slider-blur-x");
    const sliderBlurW = document.getElementById("slider-blur-w");
    const sliderBlurStrength = document.getElementById("slider-blur-strength");
    const sliderSubY = document.getElementById("slider-sub-y");
    const sliderSubX = document.getElementById("slider-sub-x");
    const sliderSubW = document.getElementById("slider-sub-w");
    const sliderTitleX = document.getElementById("slider-title-x");
    const sliderTitleY = document.getElementById("slider-title-y");

    const valBlurY = document.getElementById("val-blur-y");
    const valBlurH = document.getElementById("val-blur-h");
    const valBlurX = document.getElementById("val-blur-x");
    const valBlurW = document.getElementById("val-blur-w");
    const valBlurStrength = document.getElementById("val-blur-strength");
    const valSubY = document.getElementById("val-sub-y");
    const valSubX = document.getElementById("val-sub-x");
    const valSubW = document.getElementById("val-sub-w");
    const valTitleX = document.getElementById("val-title-x");
    const valTitleY = document.getElementById("val-title-y");

    const previewBlur = document.getElementById("preview-blur-box");
    // Batch AK: the CSS plate element is gone — the composited
    // frame carries the engine's real plate (see S2).
    const previewSub = document.getElementById("preview-sub-box");
    const previewTitle = document.getElementById("preview-title-box");
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

    async function handleVideoFileSelect(file) {
        if (!file) return;
        haptic("medium");
        setRecapPreviewState("loading");

        // Instant client-side video preview via Object URL
        const objectUrl = URL.createObjectURL(file);
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
        if (titleSpan) titleSpan.innerText = file.name;
        if (platformSpan) platformSpan.innerText = "Direct Upload (MP4)";
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
                durSpan.innerHTML = `⏱️ ${data.estimated_duration || "00:00"} <span style="color:#ef4444; font-weight:700; margin-left:4px;">(${msg})</span>`;
            } else {
                durSpan.className = "mini-tag";
                durSpan.innerHTML = `⏱️ ${data.estimated_duration || "00:00"} <span style="color:#10b981; font-weight:700; margin-left:4px;">(${t("recap_duration_valid") || "Ready"})</span>`;
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
    }
    window.refreshRecapInfoMetaLanguage = refreshRecapInfoMetaLanguage;

    function setRecapInfoUploadState(mode) {
        const durSpan = document.getElementById("recap-info-dur");
        const subSpan = document.getElementById("recap-info-sub");
        if (mode === "pending") {
            if (durSpan) { durSpan.className = "mini-tag"; durSpan.innerText = "⏱️ 00:00"; }
            if (subSpan) { subSpan.className = "mini-tag"; subSpan.innerText = t("recap_info_uploading"); }
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
                phase = "processing";
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
                onRecapUploadError(mapErrorToToast(data.detail || `Upload failed (${xhr.status})`, "upload"));
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
            state.recapThumbnailUrl = authThumb || data.thumbnail;
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

        if (durSpan) {
            if (isOverLimit) {
                durSpan.className = "mini-tag text-danger";
                const maxM = Math.floor(maxAllowed / 60);
                const tierLabel = state.user?.is_vip ? "VIP" : t("tier_free_label");
                const durM = Math.floor(data.duration_seconds / 60);
                const durS = Math.floor(data.duration_seconds % 60);
                const durStr = `${durM}:${durS < 10 ? '0' : ''}${durS}`;
                durSpan.innerHTML = `⏱️ ${data.estimated_duration} <span style="color:#ef4444; font-weight:700; margin-left:4px;">(${tf("recap_over_limit_label", { tier: tierLabel })})</span>`;
                if (btnStartRecap) {
                    btnStartRecap.disabled = true;
                    btnStartRecap.style.opacity = "0.5";
                }
                showStudioToast(t("toast_duration_over"), 'warning');
            } else {
                durSpan.className = "mini-tag";
                durSpan.innerHTML = `⏱️ ${data.estimated_duration} <span style="color:#10b981; font-weight:700; margin-left:4px;">(Ready)</span>`;
                if (btnStartRecap && !recapJobIsActive()) {
                    btnStartRecap.disabled = false;
                    btnStartRecap.style.opacity = "";
                }
                if (splitAutoEnabled) notifyRecapSplitAutoEnabled();
            }
        }

        const els = recapUploadEls();
        if (els.bar) { els.bar.classList.remove("busy", "indeterminate"); els.bar.style.width = "100%"; }
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

    // ── R40 item 9: server-grabbed preview frames for link sources ──
    // The endpoint downloads the verified source once (server-side
    // cache) and returns a real frame at the requested second. The
    // frame then flows through the SAME canvas treatment as the
    // thumbnail (ratio fit, mirror class, backdrop blur, CSS blur /
    // subtitle boxes on top), so Mirror/Ratio/Style/Blur changes keep
    // reflecting live. Failures are silent on purpose: the thumbnail
    // or mockup stays, and we do not retry in a loop.
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
    function recapCompositePayload() {
        const g = (id) => document.getElementById(id);
        const iv = (el, dflt) => el ? parseInt(el.value) : dflt;
        const seek = g("slider-canvas-seek");
        const titleOn = !!state.recapTitleEnabled;
        const customTitle = (state.recapTitleText || "").trim();
        // N3: the real first cue once the script editor has one.
        let sample = "";
        const firstSeg = document.querySelector("#recap-script-list .script-seg-text");
        if (firstSeg && firstSeg.value) sample = firstSeg.value.trim().slice(0, 120);
        return {
            url: state.uploadedVideoPath ? null : (state.recapVerifiedSourceUrl || null),
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
            // The client resolves the Addition-B fallback (custom
            // text, else the video's own title) — the preview burns
            // exactly the text the render will.
            title_enabled: titleOn,
            title_text: titleOn ? (customTitle || (state.recapVideoTitle || "") || null) : null,
            title_x: iv(g("slider-title-x"), state.recapTitleX || 50),
            title_y: iv(g("slider-title-y"), state.recapTitleY != null ? state.recapTitleY : 12),
            include_logo: state.recapLogo !== false,
            logo_pos: state.recapLogoPos || "top-right",
        };
    }
    function scheduleRecapCompositeRefresh() {
        if (!state.recapVerifiedSourceUrl && !state.uploadedVideoPath) return;
        if (recapCompositeTimer) clearTimeout(recapCompositeTimer);
        recapCompositeTimer = setTimeout(refreshRecapCompositePreview, 600);
    }
    window.scheduleRecapCompositeRefresh = scheduleRecapCompositeRefresh;
    async function refreshRecapCompositePreview() {
        if (!state.recapVerifiedSourceUrl && !state.uploadedVideoPath) return;
        const payload = recapCompositePayload();
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

    // Batch AK: the subtitle PLATE height as a fraction of the
    // PICTURE height — core._plate_height_px's formula evaluated at
    // the 608px reference picture height (the fraction is stable
    // across picture heights to ~0.1pt).
    function subtitlePlateHeightFracOfPicture() {
        const PH = 608;
        const fp = Math.max(18, PH * subtitleFontFraction(state.recapFontSize || 16, state.recapLines || 1));
        const textH = fp * ((state.recapLines || 1) === 2 ? 2.30 : 1.15);
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

    // Batch AH: title centre as canvas fractions on BOTH axes — the
    // engine's title_position basis ([tx, ty] are the title CENTRE
    // as fractions of the PICTURE; core.py overlays the title PNG at
    // ox + pw*tx − w/2, oy + ph*ty − h/2), so the dragged preview
    // position is the rendered position.
    function titleCentreCanvasFrac(xPercent, yPercent) {
        const r = recapPictureRect();
        const tx = Math.max(0, Math.min(100, xPercent)) / 100.0;
        const ty = Math.max(0, Math.min(100, yPercent)) / 100.0;
        return {
            x: (r.pw < 1 - 0.002) ? r.ox + tx * r.pw : tx,
            y: ((r.oy + r.ph) < 1 - 0.002) ? r.oy + ty * r.ph : ty,
        };
    }

    // Batch AH: paint the title box from the sliders/state — shown
    // only while the title toggle is on. The text is the user's
    // custom text, else the source video's own title (the same
    // fallback the API applies), else the sample label; the box is
    // capped at the render's 78%-of-picture title width.
    function syncTitleBoxVisual() {
        if (!previewTitle) return;
        const enabled = !!state.recapTitleEnabled;
        previewTitle.classList.toggle("hidden", !enabled);
        const x = sliderTitleX ? parseFloat(sliderTitleX.value) : (state.recapTitleX || 50);
        const y = sliderTitleY ? parseFloat(sliderTitleY.value) : (state.recapTitleY != null ? state.recapTitleY : 12);
        state.recapTitleX = Math.round(x);
        state.recapTitleY = Math.round(y);
        if (valTitleX) valTitleX.innerText = `${Math.round(x)}%`;
        if (valTitleY) valTitleY.innerText = `${Math.round(y)}%`;
        syncTitleFallbackHint();
        // Batch AS (AS-3b): the refresh must ALSO fire when the
        // title is switched OFF — the composited frame burns the
        // title, so skipping the refresh on the disabled path left
        // the burned badge in the preview indefinitely (the DOM
        // handle hid, the picture kept the title). The scheduler is
        // hash-guarded and source-guarded, so calling it on every
        // sync is safe.
        scheduleRecapCompositeRefresh();
        if (!enabled) return;
        const c = titleCentreCanvasFrac(x, y);
        const r = recapPictureRect();
        previewTitle.style.left = `${(c.x * 100).toFixed(3)}%`;
        previewTitle.style.top = `${(c.y * 100).toFixed(3)}%`;
        previewTitle.style.right = "auto";
        previewTitle.style.bottom = "auto";
        previewTitle.style.transform = "translate(-50%, -50%)";
        previewTitle.style.maxWidth = `${(0.78 * r.pw * 100).toFixed(3)}%`;
        // Batch AK: the box is an outline handle — the title itself
        // renders inside the composited frame (the composite refresh
        // is scheduled above, for BOTH the on and off paths).
    }

    // Batch AK (Addition B): the fallback hint — visible exactly
    // when the title toggle is ON and the custom text is empty, the
    // state where the video's OWN title gets burned for the whole
    // video. The toggle itself lives in the main form flow (never
    // buried), so its state is always visible.
    function syncTitleFallbackHint() {
        const hint = document.getElementById("recap-title-fallback-hint");
        if (!hint) return;
        const show = !!state.recapTitleEnabled && !(state.recapTitleText || "").trim();
        hint.classList.toggle("hidden", !show);
    }

    // Batch AA: the blur box is drawn on the PICTURE, exactly like
    // the subtitle (recapter standard) — blur_y is the box centre as
    // a fraction of the PICTURE height and blur_h its height as a
    // fraction of the picture height, end-to-end (sliders → payload
    // → engine; core.user_blur_box_to_canvas_box converts with this
    // same layout math at render). Returns the box geometry as
    // fractions of the CANVAS height for painting: centre =
    // oy + blur_y × ph, height = blur_h × ph on letterboxed layouts;
    // identity on full-frame layouts. Width stays 92% centred (CSS).
    // Batch AG: with xPercent / wPercent given, the horizontal axis
    // joins the same picture basis — left = ox + blur_x × pw,
    // width = blur_w × pw (canvas fractions) — and is returned as
    // left/width for explicit painting; without them the legacy
    // centred 92% paint is returned (left 0.04, width 0.92).
    function blurBoxCanvasGeom(yPercent, hPercent, xPercent, wPercent) {
        const r = recapPictureRect();
        const y = Math.max(0, Math.min(100, yPercent)) / 100.0;
        const h = Math.max(0, Math.min(100, hPercent)) / 100.0;
        let left = 0.04, width = 0.92;
        if (xPercent !== undefined || wPercent !== undefined) {
            const wF = (wPercent === undefined ? 92 : Math.max(0, Math.min(100, wPercent))) / 100.0;
            const xF = (xPercent === undefined ? (1 - wF) * 50 : Math.max(0, Math.min(100, xPercent))) / 100.0;
            left = r.ox + xF * r.pw;
            width = wF * r.pw;
        }
        if ((r.oy + r.ph) < 1 - 0.002) {
            return { centre: r.oy + y * r.ph, height: h * r.ph, left, width };
        }
        return { centre: y, height: h, left, width };
    }

    // Batch AA: a NEW source starts from the tight defaults — the
    // previous video's blur box / subtitle position must not ride
    // onto a different picture. Resets the sliders, the state, the
    // persisted snapshot and the explicit-box marker, and repaints.
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

    // Picker size → fraction of the PICTURE height (Batch AK —
    // picture-relative, the Recapter standard); mirrors
    // SUBTITLE_FONT_HEIGHT_FRACTIONS in core.py (×1.25 two-line).
    function subtitleFontFraction(sizePt, lines) {
        const pts = [[13, 3.2], [16, 4.0], [19, 4.8], [22, 5.6]];
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
        const canvasH = canvas ? canvas.clientHeight : 0;
        const frac = subtitleFontFraction(state.recapFontSize || 16, state.recapLines || 1);
        // Batch AK: the fraction applies to the PICTURE's height on
        // canvas (ph × canvasH), mirroring core.subtitle_font_px.
        const r = recapPictureRect();
        return canvasH > 0 ? Math.max(8, Math.round(frac * r.ph * canvasH)) : 13;
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
        // Batch AK: the blur geometry feeds the composited frame —
        // a blur change re-renders it (debounced + hash-guarded).
        scheduleRecapCompositeRefresh();
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
        if (previewBlur) {
            const px = 2 + ((Number.isFinite(v) ? v : 18) - 4) / 3;
            previewBlur.style.backdropFilter = `blur(${px.toFixed(1)}px)`;
            previewBlur.style.webkitBackdropFilter = `blur(${px.toFixed(1)}px)`;
        }
    }
    if (sliderBlurStrength) {
        sliderBlurStrength.addEventListener("input", syncBlurStrengthVisual);
        syncBlurStrengthVisual();
    }

    if (sliderSubY && previewSub && valSubY) {
        sliderSubY.addEventListener("input", () => {
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
        });
    }

    // Batch AH: title position sliders mirror the canvas drag.
    if (sliderTitleX && previewTitle && valTitleX) {
        sliderTitleX.addEventListener("input", () => {
            syncTitleBoxVisual();
        });
    }

    if (sliderTitleY && previewTitle && valTitleY) {
        sliderTitleY.addEventListener("input", () => {
            syncTitleBoxVisual();
        });
    }

    // Batch BI: auto-detect blur switch — the change lands in state;
    // the tab-level delegated saver snapshots it like every other
    // recap setting, and collectAccountPrefs carries it server-side.
    const blurAutoDetectToggleEl = document.getElementById("recap-blur-autodetect-toggle");
    if (blurAutoDetectToggleEl) {
        blurAutoDetectToggleEl.addEventListener("change", () => {
            state.recapBlurAutoDetect = !!blurAutoDetectToggleEl.checked;
        });
    }

    // Batch AH: title enable toggle + custom text (the controls live
    // in the main form area above the CTA; the box/sliders follow).
    const titleToggleEl = document.getElementById("recap-title-toggle");
    const titleTextEl = document.getElementById("recap-title-text");
    if (titleToggleEl) {
        titleToggleEl.addEventListener("change", () => {
            state.recapTitleEnabled = !!titleToggleEl.checked;
            if (titleTextEl) titleTextEl.classList.toggle("hidden", !state.recapTitleEnabled);
            syncTitleBoxVisual();
        });
    }
    if (titleTextEl) {
        titleTextEl.addEventListener("input", () => {
            state.recapTitleText = titleTextEl.value || "";
            syncTitleBoxVisual();
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

    // Batch AH: the title box drags on BOTH axes over the full
    // picture (its centre is the engine's title_position), reusing
    // the same two drag helpers and picture-scaled deltas.
    enableVerticalDrag(previewTitle, (y) => {
        if (sliderTitleY) sliderTitleY.value = y;
        syncTitleBoxVisual();
    }, () => sliderTitleY ? parseFloat(sliderTitleY.value) : 12, () => {
        const r = recapPictureRect();
        return ((r.oy + r.ph) < 1 - 0.002 && r.ph > 0) ? 1 / r.ph : 1;
    }, () => [0, 100]);

    enableHorizontalDrag(previewTitle, (x) => {
        if (sliderTitleX) sliderTitleX.value = x;
        syncTitleBoxVisual();
    }, () => sliderTitleX ? parseFloat(sliderTitleX.value) : 50, () => {
        const r = recapPictureRect();
        return (r.pw < 1 - 0.002 && r.pw > 0) ? 1 / r.pw : 1;
    }, () => [0, 100]);

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
    syncTitleBoxVisual();

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
        syncTitleBoxVisual();
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
            note.classList.add("hidden");
            note.innerText = "";
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

    // 6. Narration Speed & Pacing (+0%, +8%, +12%, +18%, +22%)
    const speedBtns = document.querySelectorAll("#recap-speed-picker .speed-seg");
    speedBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            speedBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            state.recapSpeed = btn.getAttribute("data-speed") || "+12%";
            moveSpeedPill(document.getElementById("recap-speed-picker"));
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
    function syncRecapLangViews() {
        if (langSel) {
            const has = Array.from(langSel.options).some(o => o.value === state.recapSourceLanguage);
            langSel.value = has ? state.recapSourceLanguage : "auto";
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
            syncRecapEngineAreas();
        });
    });
    refreshRecapEngineLock();
    const geminiVoiceSel = document.getElementById("recap-gemini-voice-select");
    if (geminiVoiceSel) {
        geminiVoiceSel.addEventListener("change", () => {
            state.recapGeminiVoice = geminiVoiceSel.value || "";
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
        // Batch AH: the title position resets with the layout too
        // (the toggle/text are content choices, not layout — kept).
        if (sliderTitleX) sliderTitleX.value = "50";
        if (sliderTitleY) sliderTitleY.value = "12";
        syncTitleBoxVisual();
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
    // 11. Font Size Picker (Synchronized across toolbar & advanced settings)
    function syncSubtitleFontSize(sizeVal) {
        state.recapFontSize = parseInt(sizeVal || "16");
        document.querySelectorAll("#font-size-picker-toolbar .pill-btn").forEach(p => {
            p.classList.toggle("active", parseInt(p.getAttribute("data-size")) === state.recapFontSize);
        });
        updateSubtitleVisualPreview();
        scheduleRecapCompositeRefresh();
    }
    document.querySelectorAll("#font-size-picker-toolbar .pill-btn").forEach(pill => {
        pill.addEventListener("click", () => {
            haptic("selection");
            syncSubtitleFontSize(pill.getAttribute("data-size"));
        });
    });

    // 12. Subtitle Lines Picker (Synchronized across toolbar & advanced settings)
    function syncSubtitleLines(linesVal) {
        state.recapLines = parseInt(linesVal || "1");
        document.querySelectorAll("#sub-lines-picker-toolbar .pill-btn").forEach(p => {
            p.classList.toggle("active", parseInt(p.getAttribute("data-lines")) === state.recapLines);
        });
        updateSubtitleVisualPreview();
        scheduleRecapCompositeRefresh();
    }
    document.querySelectorAll("#sub-lines-picker-toolbar .pill-btn").forEach(pill => {
        pill.addEventListener("click", () => {
            haptic("selection");
            syncSubtitleLines(pill.getAttribute("data-lines"));
        });
    });

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

    // 12. Logo Position Picker
    const logoPosBtns = document.querySelectorAll("#recap-logo-pos-picker .pill-btn");
    logoPosBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            logoPosBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            state.recapLogoPos = btn.getAttribute("data-pos") || "top-right";
            if (previewLogo) {
                previewLogo.className = `preview-logo-watermark ${state.recapLogoPos}`;
            }
        });
    });

    // 12b. Custom Logo Upload / Remove (paid-only — the server
    // enforces the tier gate; these handlers only wire the controls
    // that refreshLogoUi shows to paid users).
    const btnLogoUpload = document.getElementById("btn-logo-upload");
    const inputLogoFile = document.getElementById("input-logo-file");
    if (btnLogoUpload && inputLogoFile) {
        btnLogoUpload.addEventListener("click", () => inputLogoFile.click());
        inputLogoFile.addEventListener("change", async () => {
            const file = inputLogoFile.files ? inputLogoFile.files[0] : null;
            inputLogoFile.value = "";
            if (!file) return;
            btnLogoUpload.disabled = true;
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
                btnLogoUpload.disabled = false;
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
                const res = await fetchWithTimeout("/api/downloader/extract", {
                    method: "POST",
                    headers: getAuthHeaders(),
                    body: JSON.stringify({ url: url }),
                }, 60000);
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail || "Analysis failed");

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
                    state.recapThumbnailUrl = data.thumbnail;
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
        recapSpeed: ["+0%", "+8%", "+12%", "+18%", "+22%"],
        recapStoryStyle: ["viral", "dramatic", "funny", "pov", "standard", "narrator"],
        recapSubStyle: ["yellow", "white", "boxed", "neon", "band", "heavy", "soft"],
        recapSubFont: ["padauk", "noto_sans", "pyidaungsu", "noto_serif"],
        recapLogoPos: ["top-right", "top-left", "bottom-right", "bottom-left", "top-center"],
        recapSourceLanguage: ["auto", "en", "zh", "ko", "th", "ja", "hi", "my", "es", "fr", "de", "id", "vi", "ru", "ar", "it", "pt", "tr"],
        recapRenderQuality: ["standard", "high", "max"],
        recapTtsEngine: ["voxcpm", "gemini", "edge"],
    };
    // Batch BR: recapTitleEnabled is deliberately NOT in this list —
    // the title toggle is session-truth (the Batch AX principle): it
    // always starts OFF on a fresh form load, whatever any saved
    // snapshot says. Stored snapshots may still carry the key (older
    // saves, the account-prefs write-through); restore simply never
    // applies it. History prefill — an explicit re-create action —
    // sets it directly from the job's config instead (see below).
    const RECAP_SETTING_BOOLS = ["recapSyncCrop", "recapMirror", "recapBurnSubs", "recapBlurSubs", "recapBlurAutoDetect", "recapLogo", "recapBlurBoxTouched"];
    const RECAP_SETTING_INTS = {
        recapCropTop: [0, 35], recapCropBottom: [0, 35],
        recapCropLeft: [0, 35], recapCropRight: [0, 35],
        recapFontSize: [12, 30], recapLines: [1, 2], recapSplitMinutes: [1, 10],
        recapBlurY: [0, 100], recapBlurH: [1, 50], recapSubY: [0, 100],
        recapSubMargin: [0, 95], recapSettingsVersion: [0, 10],
        recapBlurX: [0, 100], recapBlurW: [10, 100],
        recapSubX: [10, 90], recapSubW: [10, 100],
        recapTitleX: [0, 100], recapTitleY: [0, 100],
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
        // Batch AH: title placement + custom text ride the snapshot
        // like the AG horizontal geometry (enabled rides the bools).
        const tx = document.getElementById("slider-title-x");
        const ty = document.getElementById("slider-title-y");
        snap.recapTitleX = tx ? parseInt(tx.value) : 50;
        snap.recapTitleY = ty ? parseInt(ty.value) : 12;
        snap.recapTitleText = state.recapTitleText || "";
        return snap;
    }

    function saveRecapSettings() {
        try { localStorage.setItem(RECAP_SETTINGS_KEY, JSON.stringify(snapshotRecapSettings())); } catch (e) {}
    }
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
        if (typeof saved.recapTitleText === "string") state.recapTitleText = saved.recapTitleText;

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
        if (pa) pa.classList.toggle("hidden", state.recapTtsEngine === "gemini");
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
        // Batch AH: title controls re-sync (toggle, text, sliders).
        const titleToggleEl = document.getElementById("recap-title-toggle");
        if (titleToggleEl) titleToggleEl.checked = !!state.recapTitleEnabled;
        const titleTextEl = document.getElementById("recap-title-text");
        if (titleTextEl) {
            titleTextEl.value = state.recapTitleText || "";
            titleTextEl.classList.toggle("hidden", !state.recapTitleEnabled);
        }
        const tix = document.getElementById("slider-title-x");
        const tiy = document.getElementById("slider-title-y");
        if (tix && Number.isFinite(state.recapTitleX)) tix.value = state.recapTitleX;
        if (tiy && Number.isFinite(state.recapTitleY)) tiy.value = state.recapTitleY;
        syncSubtitleFontSize(state.recapFontSize || 16);
        syncSubtitleLines(state.recapLines || 1);
        syncBlurBoxVisual();
        syncSubBoxVisual();
        syncTitleBoxVisual();
        if (typeof updateRecapSplitCostHint === "function") updateRecapSplitCostHint();
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
            recapSpeed: cfg.voice_speed, recapStoryStyle: cfg.story_style,
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
            // Batch AH: title settings round-trip like the rest
            // (pre-AH configs carry none of these keys and restore
            // keeps the current values).
            recapTitleEnabled: cfg.title_enabled === true,
            recapTitleText: cfg.title_text || "",
            recapTitleX: cfg.title_x, recapTitleY: cfg.title_y,
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
            recapSplitEnabled: !!cfg.split_video,
        };
        try { localStorage.setItem(RECAP_SETTINGS_KEY, JSON.stringify(snap)); } catch (e) {}
        restoreRecapSettings();
        // Batch BR: the title toggle is session-truth — restore no
        // longer applies recapTitleEnabled from any snapshot — so
        // this explicit re-create action sets it directly from the
        // job's own config (the same post-restore pattern as the
        // script-review toggle below). This is the one path still
        // allowed to turn the toggle ON.
        state.recapTitleEnabled = cfg.title_enabled === true;
        const titleTogglePf = document.getElementById("recap-title-toggle");
        if (titleTogglePf) titleTogglePf.checked = state.recapTitleEnabled;
        const titleTextPf = document.getElementById("recap-title-text");
        if (titleTextPf) titleTextPf.classList.toggle("hidden", !state.recapTitleEnabled);
        syncTitleBoxVisual();
        // Batch AH: exactly one script-review toggle exists now.
        const srToggle = document.getElementById("recap-script-review-toggle");
        if (srToggle) srToggle.checked = !!cfg.script_review;
        if (cfg.title) state.recapVideoTitle = cfg.title;
        state.recapSourceDuration = cfg.duration_seconds || null;
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
    const splitToggleEl = document.getElementById("recap-split-toggle");
    if (!state._splitCostConfirmed
        && splitToggleEl && splitToggleEl.checked
        && state.recapSourceDuration && state.recapSourceDuration > maxRecapSec
        && state.user?.is_vip && !state.user?.is_admin) {
        const est = estimateRecapSegmentParts(state.recapSourceDuration, state.recapSplitMinutes || 3);
        showStudioConfirm(tf("recap_split_cost_estimate", { parts: est, credits: est }), () => {
            state._splitCostConfirmed = true;
            handleStartRecap();
        });
        return;
    }
    state._splitCostConfirmed = false;

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
    if (progressFill) progressFill.style.width = "10%";
    setRecapStage("1/4", null);
    // R40 deck F-1/F-2: the first status/ETA lines named the vendor
    // engine ("Groq Cloud Whisper Turbo", "Groq Turbo Engine + 1080p
    // Cinema Compositing") — vendor names out, structure kept.
    if (statusMsg) statusMsg.innerText = t("recap_stage1_status");

    let estMinutes = "၁.၅ - ၂.၅";
    if (state.recapSourceDuration && state.recapSourceDuration > 180) {
        estMinutes = "၃ - ၄";
    }
    if (stepText) {
        stepText.innerText = tf("recap_eta_estimate", {
            min: currentLang === "my" ? estMinutes : estMinutes.replace("၁.၅ - ၂.၅", "1.5 - 2.5").replace("၃ - ၄", "3 - 4"),
        });
    }

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
        const sliderTitleXSubmit = document.getElementById("slider-title-x");
        const sliderTitleYSubmit = document.getElementById("slider-title-y");

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
            // engine treats a marked box as authoritative.
            blur_box_explicit: !!state.recapBlurBoxTouched,
            blur_strength: sliderBlurStrength ? parseInt(sliderBlurStrength.value) : 18,
            blur_auto_detect: state.recapBlurAutoDetect !== false,
            burn_subtitles: state.recapBurnSubs !== false,
            // Batch AK: the slider is the bottom-edge margin; sub_y
            // is no longer sent (the server default stands and the
            // margin wins in the engine when present).
            sub_bottom_margin: sliderSubY ? parseInt(sliderSubY.value) : (state.recapSubMargin != null ? state.recapSubMargin : 5),
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
            // Batch AH: title overlay — enabled flag, custom text
            // (null = fall back to the video's own title), and the
            // centre position in picture percents.
            title_enabled: !!state.recapTitleEnabled,
            title_text: (state.recapTitleText || "").trim() || null,
            title_x: sliderTitleXSubmit ? parseInt(sliderTitleXSubmit.value) : (state.recapTitleX || 50),
            title_y: sliderTitleYSubmit ? parseInt(sliderTitleYSubmit.value) : (state.recapTitleY != null ? state.recapTitleY : 12),
            duration_seconds: state.recapSourceDuration || null,
            script_review: !!((document.getElementById("recap-script-review-toggle") || {}).checked),
            source_language: state.recapSourceLanguage || "auto",
            render_quality: state.recapRenderQuality || "standard",
            tts_engine: state.recapTtsEngine || "voxcpm",
            gemini_voice: (state.recapTtsEngine === "gemini" && state.recapGeminiVoice) ? state.recapGeminiVoice : null,
            split_video: !!((document.getElementById("recap-split-toggle") || {}).checked),
            split_minutes: state.recapSplitMinutes || 3,
        };

        const res = await fetch("/api/recap/generate", {
            method: "POST",
            headers: getAuthHeaders(),
            body: JSON.stringify(payload),
        });

        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Recap creation failed");

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
        LiveJobs.track({ id: data.recap_id, kind: "recap", status: "QUEUED", progress_percent: 5, stage_index: 0, stage_total: 4, title: state.recapVideoTitle || "" });
        // Batch AO: while tiered pricing is armed, the response's
        // credits_total (the band total the job will be charged) is
        // the quoted cost. The key is absent in the legacy world,
        // where the chain below is exactly the historical one.
        const confirmCostText = data.credits_total
            ? tf("confirm_cost_recap", { credits: data.credits_total })
            : ((data.estimated_parts && data.estimated_parts > 1)
                ? tf("confirm_cost_recap_segmented", { parts: data.estimated_parts })
                : (data.credits_charged
                    ? tf("confirm_cost_recap", { credits: data.credits_charged })
                    : t("confirm_cost_none")));
        showSubmitConfirmation("recap", data.recap_id, confirmCostText);

    } catch (err) {
        studioSubmitFailure("recap");
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
let recapFormEtaSeconds = 0;    // ETA extra, fed by the detail poller
let recapFormNotice = null;     // a poller health notice owning the status line
let recapFormStoreSubscribed = false;

function renderRecapFormProgress(job) {
    if (!job || job.id !== recapFormJobId) return;
    if (recapFormNotice) return; // a poller notice owns the status line
    const statusMsg = document.getElementById("recap-status-msg");
    const stepText = document.getElementById("recap-step-text");
    const progressFill = document.getElementById("recap-progress-fill");
    if (statusMsg) statusMsg.innerText = liveJobStageText(job);
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
    if (inlineText) inlineText.innerText = liveJobStageText(job);
    if (inlineLine) inlineLine.classList.remove("hidden");
    if (progressFill && job.progress_percent) {
        progressFill.style.width = `${Math.min(100, Math.max(10, job.progress_percent))}%`;
    }
    if (stepText) {
        if (liveJobsIsTerminal(job.status)) {
            stepText.innerText = "";
        } else {
            const stepBits = [`${t("recap_step_label")} ${job.stage_index || 0}/${job.stage_total || 4}`];
            if (typeof job.queue_position === "number" && job.queue_position > 0) {
                stepBits.push(tf("recap_queue_ahead", { count: job.queue_position }));
            }
            if (recapFormEtaSeconds > 0) {
                stepBits.push(tf("recap_eta_dynamic", { min: Math.max(1, Math.round(recapFormEtaSeconds / 60)) }));
            }
            if (stepBits.length === 1) stepBits.push(t("queue_notice_recap"));
            stepText.innerText = stepBits.join(" • ");
        }
    }
    setRecapStage(`${job.stage_index || 0}/${job.stage_total || 4}`, job.status);
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
    recapFormEtaSeconds = 0;
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
            recapFormEtaSeconds = (typeof data.eta_seconds === "number" && data.eta_seconds > 0)
                ? data.eta_seconds : 0;
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

            if (data.status === "SCRIPT_READY") {
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
                    // Batch AC: no scroll hijack on completion — the
                    // pill/sheet announce the result and the user
                    // opens it by choice; the studio stays put.
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

async function retryRecapJobById(jobId) {
    if (!jobId) return;
    showStudioConfirm(t("recap_retry_confirm"), async () => {
        try {
            const res = await fetch(`/api/recap/${jobId}/retry`, {
                method: "POST",
                headers: getAuthHeaders(),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || t("err_generic"));
            const failedBox = document.getElementById("recap-failed-box");
            if (failedBox) failedBox.classList.add("hidden");
            state.recapJobId = data.recap_id;
            try { localStorage.setItem("vcp_active_recap_job_id", data.recap_id); } catch (e) {}
            showStudioToast(t("recap_retry_started_toast"), 'success');
            loadUserProfile();
            switchTab("tab-recap");
            startRecapJobPolling(data.recap_id);
        } catch (err) {
            showStudioToast((err && err.message) ? err.message : t("err_recap_failed"), 'error');
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
}

function dlHideExtractError() {
    const box = document.getElementById("dl-error-box");
    if (box) box.classList.add("hidden");
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
    const original = btn ? btn.innerHTML : "";
    if (btn) { btn.disabled = true; btn.innerHTML = `<span>${t("dl_preparing")}</span>`; }
    try {
        const res = await fetch(downloadUrl, { headers: getAuthHeaders() });
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
                if (!res.ok) throw new Error(data.detail || "Extraction failed");

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

                if (dlTitle) dlTitle.innerText = data.title || "Video Content";
                if (dlPlatform) dlPlatform.innerText = data.platform || "";
                if (dlUploader) dlUploader.innerText = data.uploader || "Creator";
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

                dlCurrent = { url: data.url || url, has_subtitles: data.has_subtitles };
                dlSetSrtCardState(data.has_subtitles);
                const dlActions = document.getElementById("dl-delivery-actions");
                if (dlActions) dlActions.innerHTML = "";

                resultCard.classList.remove("hidden");
                dlHideExtractError();
            } catch (err) {
                // Includes the client-side timeout (fetchWithTimeout
                // abort): the extract did not complete — say so, and
                // keep saying it after the toast fades (P1).
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
                // 1. Switch to tab-recap
                const tabRecapBtn = document.querySelector('[data-tab="tab-recap"]');
                if (tabRecapBtn) tabRecapBtn.click();

                // 2. Set recap URL and trigger inspection
                const recapInput = document.getElementById("recap-url-input");
                if (recapInput) {
                    recapInput.value = url;
                    // Trigger mode URL if currently on File mode
                    const btnModeUrl = document.getElementById("btn-source-mode-url");
                    if (btnModeUrl) btnModeUrl.click();

                    const btnExtract = document.getElementById("btn-fetch-recap-info");
                    if (btnExtract) {
                        setTimeout(() => btnExtract.click(), 300);
                    }
                }
            });
        }
    }
}

// ── Plan Category Tabs (one category at a time — no long scroll) ──────────
function switchPlanCategory(cat) {
    haptic("selection");
    document.querySelectorAll(".plan-tab-btn").forEach(btn => {
        btn.classList.toggle("active", btn.getAttribute("data-plan-cat") === cat);
    });
    ["voice", "recap", "combined"].forEach(name => {
        const panel = document.getElementById(`plan-panel-${name}`);
        if (panel) panel.classList.toggle("hidden", name !== cat);
    });
}

// ── Payment Drawer & Slip Submission ───────────────────────────────────────
let activePackageCode = "voice_studio_pro";
let activeAmount = 25000;

async function openPaymentDrawer(packageCode, amount, title) {
    haptic("light");
    activePackageCode = packageCode;
    activeAmount = amount;

    const titleEl = document.getElementById("drawer-package-title");
    if (titleEl && title) {
        titleEl.innerText = `${title} — ${amount.toLocaleString()} MMK`;
    }

    const drawer = document.getElementById("payment-box");
    if (drawer) {
        drawer.classList.remove("hidden");
        drawer.scrollIntoView({ behavior: "smooth" });
    }

    // Dynamic sync of payment account numbers from API
    try {
        const res = await fetchWithTimeout("/api/vip/plans", { headers: getAuthHeaders() });
        if (res.ok) {
            const data = await res.json();
            const accounts = data.payment_accounts || [];
            accounts.forEach(acc => {
                const prov = (acc.provider || "").toLowerCase();
                if (prov.includes("kbz") || prov.includes("kpay")) {
                    const elNum = document.getElementById("kpay-num");
                    const elName = document.getElementById("kpay-name");
                    if (elNum) elNum.innerText = acc.number;
                    if (elName) elName.innerText = `Account Name: ${acc.name}`;
                } else if (prov.includes("wave")) {
                    const elNum = document.getElementById("wave-num");
                    const elName = document.getElementById("wave-name");
                    if (elNum) elNum.innerText = acc.number;
                    if (elName) elName.innerText = `Account Name: ${acc.name}`;
                }
            });
        }
    } catch (err) {
        console.warn("Could not sync payment accounts from API:", err);
    }
}

function closePaymentDrawer() {
    const drawer = document.getElementById("payment-box");
    if (drawer) drawer.classList.add("hidden");
}

function copyAccount(accNum) {
    haptic("medium");
    const numToCopy = accNum || "09790760003";
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

function paintVoiceTtsBalanceLine(profile) {
    // AW-11: the Voice tab labels the referral bucket as TTS-only —
    // it is NOT part of the clone balance anymore.
    const el = document.getElementById("voice-tts-balance-line");
    if (!el) return;
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
                const refMin = ((q.referral_seconds || 0) / 60).toFixed(1);
                paintTtsBalanceLine(runId, (line) => {
                    line.classList.remove("tts-balance-retry");
                    delete line.dataset.quotaError;
                    if (q.unlimited) {
                        line.textContent = tf("tts_balance_line_unlimited", { ref_min: refMin });
                    } else {
                        line.textContent = tf("tts_balance_line", {
                            ref_min: refMin,
                            remain_min: (((q.remaining_seconds || 0)) / 60).toFixed(1),
                        });
                    }
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
    paintTtsBalanceLine(-1, (line) => {
        line.classList.remove("tts-balance-retry");
        delete line.dataset.quotaError;
        line.textContent = profile.is_admin
            ? tf("tts_balance_line_unlimited", { ref_min: refMin })
            : tf("voice_tts_balance_line", { min: refMin });
    });
}

function updateTtsCharCount() {
    const input = document.getElementById("tts-text-input");
    const counter = document.getElementById("tts-char-count");
    if (input && counter) counter.textContent = `${(input.value || "").length} / 5,000`;
}
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

function initTtsTab() {
    document.querySelectorAll("#tts-engine-picker .seg-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            ttsState.engine = btn.getAttribute("data-tts-engine") || "gemini";
            document.querySelectorAll("#tts-engine-picker .seg-btn").forEach(
                b => b.classList.toggle("active", b === btn)
            );
            const gRow = document.getElementById("tts-gemini-voice-row");
            const eRow = document.getElementById("tts-edge-voice-row");
            if (gRow) gRow.classList.toggle("hidden", ttsState.engine !== "gemini");
            if (eRow) eRow.classList.toggle("hidden", ttsState.engine !== "edge");
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
        });
    });
    const input = document.getElementById("tts-text-input");
    if (input) input.addEventListener("input", updateTtsCharCount);
    updateTtsCharCount();
    // Batch BB-4: the balance line's fallback state is tap-to-retry.
    const balLine = document.getElementById("tts-balance-line");
    if (balLine) balLine.addEventListener("click", () => {
        if (balLine.dataset.quotaError === "1") refreshTtsQuota();
    });
}
if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initTtsTab);
} else {
    initTtsTab();
}

async function generateTTS() {
    const input = document.getElementById("tts-text-input");
    const text = ((input && input.value) || "").trim();
    if (!text) {
        showStudioToast(t("tts_empty_error"), 'warning');
        return;
    }
    const engine = ttsState.engine;
    const voice = engine === "gemini"
        ? ((document.getElementById("tts-gemini-voice-select") || {}).value || "Charon")
        : ((document.getElementById("tts-edge-voice-select") || {}).value || "thiha");
    const btn = document.getElementById("btn-generate-tts");
    const progressBox = document.getElementById("tts-progress-box");
    const resultBox = document.getElementById("tts-result-box");
    haptic("heavy");
    if (btn) btn.disabled = true;
    if (resultBox) resultBox.classList.add("hidden");
    if (progressBox) progressBox.classList.remove("hidden");
    try {
        const res = await fetchWithTimeout("/api/tts/generate", {
            method: "POST",
            headers: getAuthHeaders(),
            body: JSON.stringify({ text, engine, voice, speed: ttsState.speed }),
        }, 30000);
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "TTS request failed");
        ttsState.jobId = data.job_id;
        pollTtsJob(data.job_id);
    } catch (err) {
        showErrorToast(err, 'voice');
        if (progressBox) progressBox.classList.add("hidden");
        if (btn) btn.disabled = false;
    }
}
window.generateTTS = generateTTS;

function pollTtsJob(jobId) {
    if (ttsState.pollTimer) clearInterval(ttsState.pollTimer);
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
            if (data.status === "QUEUED") {
                if (fill) fill.style.width = "45%";
            } else if (data.status === "RUNNING") {
                if (fill) fill.style.width = "80%";
            } else if (data.status === "SUCCESS") {
                clearInterval(ttsState.pollTimer);
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
                clearInterval(ttsState.pollTimer);
                haptic("error");
                if (progressBox) progressBox.classList.add("hidden");
                if (btn) btn.disabled = false;
                showStudioToast(t("tts_failed"), 'error');
                refreshTtsQuota();
            }
            if (tick > 120 && !["SUCCESS", "FAILED", "CANCELLED"].includes(data.status)) {
                clearInterval(ttsState.pollTimer);
                if (progressBox) progressBox.classList.add("hidden");
                if (btn) btn.disabled = false;
                showStudioToast(t("voice_wait_timeout"), 'info', 6000);
            }
        } catch (e) {
            if (tick > 120) {
                clearInterval(ttsState.pollTimer);
                const progressBox = document.getElementById("tts-progress-box");
                if (progressBox) progressBox.classList.add("hidden");
                const btn = document.getElementById("btn-generate-tts");
                if (btn) btn.disabled = false;
            }
        }
    }, 2000);
}

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

        const headers = {};
        if (state.tg && state.tg.initData) {
            headers["X-Telegram-Init-Data"] = state.tg.initData;
        }

        const res = await fetchWithTimeout("/api/vip/submit-slip", {
            method: "POST",
            headers: headers,
            body: formData,
        }, 60000);

        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Submission failed");

        // R40 item 6 / deck A10: the confirmation must carry the
        // request ID + pending status — the API's message already
        // includes both ("တောင်းဆိုမှု နံပါတ်: #… — Admin မှ
        // စစ်ဆေးပြီးပါက Bot မှ အသိပေးပါမည်"); the old static toast
        // showed neither, so buyers had no reference at all.
        showStudioToast(data.message || t("toast_success_slip"), 'success', 7000);
        closePaymentDrawer();
        if (fileInput) fileInput.value = "";
        const previewBox = document.getElementById("slip-preview-box");
        if (previewBox) previewBox.classList.add("hidden");
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
    if (el) el.innerHTML = `<div class="adm-state"><span class="spinner"></span><span>${t("admin_loading")}</span></div>`;
}

function admStateEmpty(el, msgKey) {
    if (el) el.innerHTML = `<div class="adm-state adm-state-empty"><span class="adm-state-icon">📭</span><span>${t(msgKey || "admin_empty")}</span></div>`;
}

function admStateError(el, retryJs) {
    if (el) el.innerHTML = `<div class="adm-state adm-state-error"><span class="adm-state-icon">⚠️</span><span>${t("admin_load_error")}</span>${retryJs ? `<button class="mini-chip" onclick="${retryJs}">${t("admin_retry")}</button>` : ""}</div>`;
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
    const pane = document.getElementById("tab-admin");
    if (pane) pane.scrollTop = 0;
    window.scrollTo({ top: 0, behavior: "smooth" });

    // Lazy-load each screen the first time it is opened.
    if (name === "payments" && !admLoadedSections.payments) { loadAdminData(); admLoadedSections.payments = true; }
    if (name === "jobs") loadAdminQueue();
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
    if (tab === "history") loadAdminHistory(); else loadAdminQueue();
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
    return `<span class="mini-tag adm-status-tag ${cls}">${escapeHtml(text)}</span>`;
}

function admKeyProviderRow(nameKey, pills, subText) {
    return `<div class="adm-action-row-line"><span><b>${escapeHtml(t(nameKey))}</b></span><span>${pills}</span></div>` +
        (subText ? `<p class="text-muted text-xs">${escapeHtml(subText)}</p>` : "");
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
        const pills = admKeyPill(p.spaces.running === p.spaces.total ? "ok" : "dead", `${p.spaces.running}/${p.spaces.total} RUNNING`);
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
        return `<div class="adm-action-row-line"><span>${escapeHtml(name)} ${pill}</span><span class="text-muted adm-line-time">${escapeHtml(remain)} · ${escapeHtml(admKeyRefillText(row))}</span></div>`;
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
    try {
        const resStats = await fetchWithTimeout("/api/admin/overview", { headers: getAuthHeaders() });
        if (resStats.ok) {
            admOverviewClear();
            const stats = await resStats.json();
            const set = (id, v) => { const el = document.getElementById(id); if (el) el.innerText = v; };
            // Batch X: headline is REAL users (accounts that have run
            // a job / bought / been granted credit); the sub-line shows
            // the registered total and the never-used remainder.
            set("adm-total-users", stats.real_users ?? stats.total_users ?? 0);
            set("adm-total-users-split", tf("admin_stat_users_split", { total: stats.total_users ?? 0, never: stats.never_used_users ?? 0 }));
            set("adm-vip-users", stats.vip_users ?? 0);
            set("adm-pending-slips", stats.pending_slips ?? 0);
            set("adm-queued-jobs", stats.queued_jobs ?? 0);
            set("adm-failed-24h", stats.failed_jobs_24h ?? 0);
            set("adm-banned-users", stats.banned_users ?? 0);
            // Batch R: headline is admin-bans + bot-blockers combined;
            // the sub-line shows the split so both numbers stay visible.
            set("adm-banned-split", tf("admin_stat_banned_split", { admin: stats.banned_by_admin ?? 0, bot: stats.bot_blocked_users ?? 0 }));
            set("adm-new-24h", stats.new_users_24h ?? 0);
            // Batch R: income card (approved payment requests only).
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
                    .map(k => `<span class="mini-tag adm-status-tag ${k.toLowerCase()}">${k}: <strong>${stats.jobs_by_status[k]}</strong></span>`)
                    .join("");
            }
            const worker = document.getElementById("adm-worker-line");
            if (worker) {
                worker.innerText = "☁️ " + (stats.cloud_worker_status || "Render Cloud");
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
    const slipsContainer = document.getElementById("admin-slips-list");
    if (!slipsContainer) return;
    if (!slips.length) {
        admStateEmpty(slipsContainer, "admin_no_slips");
        return;
    }
    slipsContainer.innerHTML = "";
    slips.forEach(s => {
        const item = document.createElement("div");
        item.className = "slip-item-card";
        item.innerHTML = `
            <div class="slip-item-row">
                <img src="${s.slip_url}" class="slip-thumb" onclick="openSlipModal('${s.slip_url}')" title="Click to view full image">
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
        badge.innerText = jobType === 'RECAP' ? '🎬 RECAP' : jobType === 'TTS' ? t("job_badge_tts") : '🎙️ VOICE';
    }
    if (titleEl) {
        titleEl.innerText = title || `Job #${jobId.slice(0, 10)}`;
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
            const videoUrl = deliverables.video || downloadUrl;
            bodyEl.innerHTML = `
                <div class="modal-video-wrapper">
                    <video id="admin-modal-video" data-auth-src="${videoUrl}" controls autoplay playsinline class="admin-modal-player-video"
                           onerror="handleAdminMediaError(this, '${jobId}', '${jobType}')"
                           onloadedmetadata="handleAdminMediaLoaded(this)">
                        Browser does not support HTML5 video.
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
                        Browser does not support HTML5 audio.
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
            const dis = (label) => `<span class="mini-chip" style="opacity: 0.45; pointer-events: none;" aria-disabled="true">${label}</span>`;
            if (jobType === "RECAP") {
                buttons += dis("🎬 Download Video");
                buttons += dis("🎙️ Download Audio");
                buttons += dis("📝 Subtitles (SRT)");
                buttons += dis("📄 Script (TXT)");
            } else {
                buttons += dis("📥 Download WAV / MP3");
            }
            buttons += dis(t("dl_open_in_browser_tab"));
        } else if (jobType === "RECAP") {
            if (deliverables.video) {
                buttons += `<a href="#" data-dl-url="${deliverables.video}" data-dl-name="${brandedDlName("Recap", "mp4", title, jobId)}" class="mini-chip gold">🎬 Download Video</a>`;
            }
            if (deliverables.audio) {
                buttons += `<a href="#" data-dl-url="${deliverables.audio}" data-dl-name="${brandedDlName("RecapAudio", "mp3", title, jobId)}" class="mini-chip">🎙️ Download Audio</a>`;
            }
            if (deliverables.srt) {
                buttons += `<a href="#" data-dl-url="${deliverables.srt}" data-dl-name="${brandedDlName("Subtitles", "srt", title, jobId)}" class="mini-chip">📝 Subtitles (SRT)</a>`;
            }
            if (deliverables.script) {
                buttons += `<a href="#" data-dl-url="${deliverables.script}" data-dl-name="${brandedDlName("Script", "txt", title, jobId)}" class="mini-chip">📄 Script (TXT)</a>`;
            }
        } else {
            const dlUrl = deliverables.download || downloadUrl;
            if (dlUrl) {
                buttons += `<a href="#" data-dl-url="${dlUrl}" data-dl-name="${brandedDlName("Voice", "wav", null, jobId)}" class="mini-chip gold">📥 Download WAV / MP3</a>`;
            }
        }
        if (mediaAvailable && downloadUrl) {
            buttons += `<a href="#" data-open-url="${downloadUrl}" class="mini-chip" style="opacity: 0.85;">${t("dl_open_in_browser_tab")}</a>`;
        }
        delivEl.innerHTML = buttons;
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
    const detail = (raw == null ? "" : String(raw)).trim();
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
                <span class="badge-status ${u.is_banned ? "blocked" : (u.bot_blocked ? "botblocked" : "active")}">${u.is_banned ? "BLOCKED" : (u.bot_blocked ? "BOT BLOCKED" : "ACTIVE")}</span>
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
        if (!data.users.length) { admStateEmpty(box, "admin_search_none"); return; }
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
            admStateEmpty(box, "admin_empty");
            return;
        }
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
        meta.innerHTML = `
            <span class="mini-tag">${u.account_category === "PURCHASED" ? "👑 VIP" : "FREE"}</span>
            <span class="mini-tag">${t("admin_joined")}: ${admFmtDate(u.created_at)}</span>
            ${u.language_code ? `<span class="mini-tag">${escapeHtml(u.language_code.toUpperCase())}</span>` : ""}`;
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
}

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
    const minutes = parseInt(document.getElementById("adm-adj-minutes")?.value || "0", 10) || 0;
    const seconds = parseInt(document.getElementById("adm-adj-seconds")?.value || "0", 10) || 0;
    const credits = parseInt(document.getElementById("adm-adj-credits")?.value || "0", 10) || 0;
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
            const payload = { user_id: uid, add_minutes: minutes, add_seconds: seconds, add_recap_credits: credits, reason };
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

async function loadAdminUserJobs(uid) {
    const box = document.getElementById("adm-user-jobs");
    if (!box) return;
    admStateLoading(box);
    try {
        const params = new URLSearchParams({ timeframe: "all", status: "all", job_type: "all", limit: "20", user_id: String(uid) });
        const res = await fetchWithTimeout(`/api/admin/history?${params.toString()}`, { headers: getAuthHeaders() });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        const jobs = data.history || [];
        if (!jobs.length) { admStateEmpty(box, "admin_user_jobs_empty"); return; }
        box.innerHTML = jobs.map(j => `
            <div class="adm-job-mini">
                <span class="history-item-badge ${j.job_type === 'RECAP' ? 'badge-recap' : j.job_type === 'TTS' ? 'badge-tts' : 'badge-voice'}">${j.job_type === 'RECAP' ? '🎬' : j.job_type === 'TTS' ? '🎧' : '🎙️'}</span>
                <span class="adm-job-mini-main">${escapeHtml(j.input_snippet || j.preset_voice || "—")}</span>
                <span class="history-item-badge status-${(j.status || "").toLowerCase()}">${escapeHtml(j.status || "")}</span>
                <span class="text-muted text-xs">${escapeHtml(j.time_ago || "")}</span>
            </div>`).join("");
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

async function loadAdminQueue(isSilent = false) {
    if (!state.user?.is_admin) return;
    const container = document.getElementById("admin-queue-list");
    const bannerEl = document.getElementById("admin-queue-status-banner");
    if (!container) return;

    if (!isSilent && container.children.length === 0) admStateLoading(container);

    try {
        const url = `/api/admin/queue?status=${encodeURIComponent(adminQueueStatusFilter)}&engine=${encodeURIComponent(adminQueueEngineFilter)}&limit=50`;
        const res = await fetchWithTimeout(url, { headers: getAuthHeaders() });
        if (!res.ok) {
            if (!isSilent) admStateError(container, "loadAdminQueue()");
            return;
        }
        const data = await res.json();

        const c = data.counts || { total_listed: 0, queued: 0, running: 0, success: 0, failed: 0 };
        const set = (id, v) => { const el = document.getElementById(id); if (el) el.innerText = v; };
        set("cnt-total", c.total_listed);
        set("cnt-queued", c.queued);
        set("cnt-running", c.running);
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
            const engineLabel = engineLower === "recap" ? "🎬 RECAP" : "☁️ VOXCPM AI";

            let statusBadgeText = j.status;
            let durationTxt = `⏳ ${j.created_ago}`;
            if (statusLower === "success") {
                statusBadgeText = t("badge_completed") || "✅ COMPLETED";
                durationTxt = `⏱️ ${j.duration_seconds || '15'}s • ${j.created_ago}`;
            } else if (statusLower === "queued") {
                statusBadgeText = t("badge_queued") || "⏳ WAITING";
                durationTxt = `⏳ ${j.created_ago}`;
            } else if (statusLower === "running") {
                statusBadgeText = t("badge_running") || "⚡ PROCESSING";
                durationTxt = `⚡ ${j.created_ago}`;
            } else if (statusLower === "failed") {
                statusBadgeText = t("badge_failed") || "❌ FAILED";
            } else if (statusLower === "cancelled") {
                statusBadgeText = t("badge_cancelled") || "⏹ CANCELLED";
            }

            let actionsHtml = "";
            if (statusLower === "failed" || statusLower === "cancelled") {
                actionsHtml += `<button class="queue-act-btn retry" onclick="adminRetryJob('${j.id}')">🔄 ${t("admin_retry")}</button>`;
            } else if (statusLower === "queued" || statusLower === "running") {
                actionsHtml += `<button class="queue-act-btn cancel" onclick="adminCancelJob('${j.id}')">⏹️ ${t("admin_cancel_job")}</button>`;
            }
            if (j.is_expired) {
                actionsHtml += `<span class="badge-expired text-xs">⌛ ${t("admin_expired")}</span>`;
                actionsHtml += `<button class="queue-act-btn retry" onclick="adminRetryJob('${j.id}')">🔄 ${t("admin_regen")}</button>`;
            } else if (j.download_url) {
                const qTitle = `${j.username ? '@' + j.username : 'ID: ' + j.user_id} • ${j.preset_voice || j.engine}`;
                actionsHtml += `<button class="queue-act-btn uid adm-media-open-btn" data-job-id="${escapeHtml(j.id)}" data-job-type="${escapeHtml(j.job_type)}" data-download-url="${escapeHtml(j.download_url)}" data-deliverables="${escapeHtml(JSON.stringify(j.deliverables || {}))}" data-title="${escapeHtml(qTitle)}">▶️ Play</button>`;
                actionsHtml += `<a href="#" data-open-url="${j.download_url}" class="queue-act-btn uid" title="Open Stream">↗️</a>`;
            }

            return `
                <div class="queue-item" id="queue-item-${j.id}">
                    <div class="queue-item-header">
                        <div class="queue-badges-left">
                            <span class="badge-status ${statusLower}">${statusBadgeText}</span>
                            <span class="badge-engine ${engineLower}">${engineLabel}</span>
                            <span class="mini-tag">${escapeHtml(j.preset_voice)}</span>
                            ${j.job_type === 'TTS' ? `<span class="mini-tag">${escapeHtml(t("job_badge_tts"))}</span>` : ""}
                        </div>
                        <span class="queue-timing">${durationTxt}</span>
                    </div>
                    <div class="queue-item-meta">
                        <span class="queue-user-pill" onclick="admFillUser(${j.user_id})">👤 ${escapeHtml(j.user_name)}${j.username ? ` (@${escapeHtml(j.username)})` : ""} • ID: ${j.user_id}</span>
                        <span>#${j.id.slice(0, 8)}</span>
                    </div>
                    <div class="queue-item-body">${escapeHtml(j.input_snippet || "Task Content")}</div>
                    ${j.error_message ? `<div class="queue-item-error">⚠️ ${escapeHtml(displayJobError(j.error_message))}</div>` : ""}
                    <div class="queue-item-footer">
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
    } catch (e) {
        console.warn("Could not load admin queue:", e);
        if (!isSilent) admStateError(container, "loadAdminQueue()");
    }
}
window.loadAdminQueue = loadAdminQueue;

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
        const data = await res.json();
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
            const titleStr = `${j.username ? '@' + j.username : 'ID: ' + j.user_id} • ${j.preset_voice || j.engine}`;
            const errText = displayJobError(j.error_message);
            const isFailed = (j.status === "FAILED");
            const isSuccess = (j.status === "SUCCESS");
            const isRunning = (j.status === "RUNNING" || j.status === "QUEUED");
            const borderCls = isFailed ? 'border-danger-dim' : (isSuccess ? (j.is_expired ? 'border-warning-dim' : 'border-success-dim') : (isRunning ? 'border-running-dim' : ''));
            return `
            <div class="history-item ${borderCls}" id="adm-job-item-${j.id}">
                <div class="history-item-header">
                    <div>
                        <span class="history-item-badge ${j.job_type === 'RECAP' ? 'badge-recap' : j.job_type === 'TTS' ? 'badge-tts' : 'badge-voice'}">${j.job_type === 'RECAP' ? '🎬 RECAP' : j.job_type === 'TTS' ? escapeHtml(t("job_badge_tts")) : '🎙️ VOICE'}</span>
                        <span class="mini-tag adm-tap" style="margin-left: 4px;" onclick="admFillUser(${j.user_id})">👤 ${escapeHtml(j.username ? '@' + j.username : 'ID: ' + j.user_id)}</span>
                    </div>
                    <div>
                        <span class="history-item-badge status-${j.status.toLowerCase()}">${j.status}</span>
                        ${j.is_expired ? `<span class="badge-expired" style="margin-left: 4px;">⌛ ${t("admin_expired")}</span>` : ''}
                    </div>
                </div>
                <div class="history-item-body">
                    <strong>${escapeHtml(j.preset_voice || j.engine)}:</strong> ${escapeHtml(j.input_snippet || "Task")}
                    ${errText ? `
                        <div class="history-error-snippet text-xs text-danger mt-1 p-2 glass-inset">
                            ⚠️ <strong>Error:</strong> ${escapeHtml(errText.slice(0, 180))}${errText.length > 180 ? '...' : ''}
                        </div>` : ''}
                    <div id="retry-status-box-${j.id}"></div>
                </div>
                <div class="history-item-footer">
                    <span>⏱️ ${j.time_ago} <code class="adm-job-code">(${j.id.slice(0, 8)})</code></span>
                    <div class="history-btn-row" id="adm-actions-row-${j.id}">
                        ${j.is_expired ? `
                            <button class="btn mini-btn secondary-btn btn-retry-job" id="btn-retry-${j.id}" onclick="adminRetryJob('${j.id}')">🔄 ${t("admin_regen")}</button>
                        ` : (j.download_url ? `
                            <button class="btn-play-preview adm-media-open-btn" data-job-id="${escapeHtml(j.id)}" data-job-type="${escapeHtml(j.job_type)}" data-download-url="${escapeHtml(j.download_url)}" data-deliverables="${escapeHtml(JSON.stringify(j.deliverables || {}))}" data-title="${escapeHtml(titleStr)}">▶️ Preview</button>
                            <a href="#" data-open-url="${j.download_url}" class="history-dl-link" title="Open Stream">↗️</a>
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
                retryBtn.innerText = "🔄 Retry";
            }
        }
    } catch (e) {
        showStudioToast(adminErrorText("admin_retry_err", e.message), 'error');
        if (retryBtn) {
            retryBtn.disabled = false;
            retryBtn.innerText = "🔄 Retry";
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
                    previewBtn.innerText = '▶️ Preview';
                    previewBtn.onclick = () => openAdminMediaModal(
                        jobId,
                        isRecap ? 'RECAP' : 'VOICE_CLONE',
                        mediaUrl,
                        JSON.stringify(data.download_urls || {}),
                        `Job #${jobId.slice(0, 8)}`
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
                    badge.innerText = "FAILED";
                }
                if (statusBox) {
                    statusBox.innerHTML = `
                        <div class="text-xs mt-1 p-2" style="background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 8px; color: #fca5a5;">
                            ⚠️ <strong>Error:</strong> ${escapeHtml(displayJobError(data.error_message) || "Execution failed")}
                        </div>
                    `;
                }
                if (retryBtn) {
                    retryBtn.disabled = false;
                    retryBtn.innerText = "🔄 Retry";
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

// ── Pricing ──────────────────────────────────────────────────────────

async function loadAdminPricing() {
    const box = document.getElementById("admin-pricing-list");
    if (!box) return;
    admStateLoading(box);
    try {
        const res = await fetchWithTimeout("/api/admin/pricing", { headers: getAuthHeaders() });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        const plans = data.plans || [];
        if (!plans.length) { admStateEmpty(box, "admin_empty"); return; }
        box.innerHTML = plans.map(p => {
            const alloc = p.recap_credits > 0 && p.minutes > 0
                ? `${p.minutes}m + ${p.recap_credits} cr`
                : (p.recap_credits > 0 ? `${p.recap_credits} credits` : `${p.minutes} min`);
            return `
            <div class="adm-price-row">
                <div class="adm-price-main">
                    <div class="adm-price-code">${escapeHtml(p.code)}</div>
                    <div class="adm-price-alloc">${escapeHtml(p.category)} • ${alloc}</div>
                </div>
                <input type="number" class="studio-input adm-price-input" id="adm-price-${escapeHtml(p.code)}" value="${p.price_mmk}" min="100" step="100">
                <span class="adm-price-cur">MMK</span>
                <button class="btn mini-btn gold-btn" onclick="adminSavePrice('${escapeHtml(p.code)}')">${t("admin_pricing_save")}</button>
            </div>`;
        }).join("");
    } catch (e) {
        admStateError(box, "loadAdminPricing()");
    }
}
window.loadAdminPricing = loadAdminPricing;

async function adminSavePrice(code) {
    const input = document.getElementById("adm-price-" + code);
    const price = parseInt(input?.value || "0", 10) || 0;
    showStudioConfirm(tf("admin_confirm_price", { code, price: price.toLocaleString() }), async () => {
        haptic("medium");
        try {
            const res = await fetch(`/api/admin/pricing/${encodeURIComponent(code)}`, {
                method: "POST",
                headers: getAuthHeaders(),
                body: JSON.stringify({ price_mmk: price }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || "Failed");
            showStudioToast(data.message, "success");
            loadAdminPricing();
        } catch (e) {
            showStudioToast(adminErrorText("admin_generic_err", e.message), "error");
        }
    });
}
window.adminSavePrice = adminSavePrice;

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

async function loadAdminSettings() {
    if (!state.user?.is_admin) return;
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

        if (elKpayNum) elKpayNum.value = s.payment_kpay_number || "";
        if (elKpayName) elKpayName.value = s.payment_kpay_name || "";
        if (elWaveNum) elWaveNum.value = s.payment_wave_number || "";
        if (elWaveName) elWaveName.value = s.payment_wave_name || "";
        if (elTutLink) elTutLink.value = s.tutorial_video_link || "";
        if (elMaint) elMaint.checked = !!s.maintenance_mode;
        if (elMaintNote) elMaintNote.value = s.maintenance_note || "";
        if (elGroupAi) elGroupAi.checked = !!s.group_ai_admin_enabled;
        const elGroupAiCooldown = document.getElementById("adm-set-group-ai-cooldown");
        const elGroupAiHourly = document.getElementById("adm-set-group-ai-hourly");
        const elGroupAiDaily = document.getElementById("adm-set-group-ai-daily");
        if (elGroupAiCooldown && s.group_ai_user_cooldown_seconds != null) elGroupAiCooldown.value = s.group_ai_user_cooldown_seconds;
        if (elGroupAiHourly && s.group_ai_hourly_budget != null) elGroupAiHourly.value = s.group_ai_hourly_budget;
        if (elGroupAiDaily && s.group_ai_daily_budget != null) elGroupAiDaily.value = s.group_ai_daily_budget;
        if (elCharLimitFree && s.character_limit_free != null) elCharLimitFree.value = s.character_limit_free;
        if (elCharLimitPurchased && s.character_limit_purchased != null) elCharLimitPurchased.value = s.character_limit_purchased;
        if (elFreeAllowance && s.free_monthly_allowance_seconds != null) elFreeAllowance.value = s.free_monthly_allowance_seconds;
        if (elDailyJobLimit && s.free_daily_job_limit != null) elDailyJobLimit.value = s.free_daily_job_limit;
        if (elRecapFreeMax && s.recap_free_max_duration_seconds != null) elRecapFreeMax.value = s.recap_free_max_duration_seconds;
        if (elRecapMax && s.recap_max_duration_seconds != null) elRecapMax.value = s.recap_max_duration_seconds;
        // Batch BE: the tiered-pricing arming switch (AO shipped the
        // setting + API dark; this toggle is the arming lever).
        if (elTieredPricing) elTieredPricing.checked = !!s.tiered_recap_pricing_enabled;
        if (elReqChannelId) elReqChannelId.value = s.required_channel_id || "";
        if (elReqGroupId) elReqGroupId.value = s.required_group_id || "";
        if (elChannelInvite) elChannelInvite.value = s.channel_invite_link || "";
        if (elGroupInvite) elGroupInvite.value = s.group_invite_link || "";

        if (elTutVoiceEn) elTutVoiceEn.checked = String(s.tutorial_voice_enabled).toLowerCase() !== "false" && s.tutorial_voice_enabled !== false;
        if (elTutVoiceTitle) elTutVoiceTitle.value = s.tutorial_voice_title || "Voice Clone ပြုလုပ်နည်း လမ်းညွှန်";
        if (elTutVoiceLink) elTutVoiceLink.value = s.tutorial_voice_link || s.tutorial_video_link || "";

        if (elTutRecapEn) elTutRecapEn.checked = String(s.tutorial_recap_enabled).toLowerCase() !== "false" && s.tutorial_recap_enabled !== false;
        if (elTutRecapTitle) elTutRecapTitle.value = s.tutorial_recap_title || "Movie Recap ပြုလုပ်နည်း လမ်းညွှန်";
        if (elTutRecapLink) elTutRecapLink.value = s.tutorial_recap_link || s.tutorial_video_link || "";
        if (elRecapDemoAsset) elRecapDemoAsset.value = s.recap_demo_asset || "";

        for (const [elId, settingKey] of ADMIN_TUTORIAL_LINK_FIELDS) {
            const el = document.getElementById(elId);
            if (el) el.value = s[settingKey] || "";
        }

        setSelectOrCustom("adm-gemini-primary", "adm-gemini-primary-custom", s.gemini_primary_model || "gemini-3.6-flash");
        setSelectOrCustom("adm-gemini-fb1", "adm-gemini-fb1-custom", s.gemini_fallback_1 || "gemini-3.5-flash");
        setSelectOrCustom("adm-gemini-fb2", "adm-gemini-fb2-custom", s.gemini_fallback_2 || "gemini-3.5-flash-lite");
        setSelectOrCustom("adm-gemini-fb3", "adm-gemini-fb3-custom", s.gemini_fallback_3 || "gemini-flash-lite-latest");
        // Batch BS: the Groq Whisper (STT) model field — the GET
        // pre-fills it with the current effective model.
        const elGroqWhisper = document.getElementById("adm-groq-whisper-model");
        if (elGroqWhisper) elGroqWhisper.value = s.groq_whisper_model || "";

        updateTutorialUI(s);
    } catch (e) {
        console.warn("Could not load dynamic settings:", e);
    }
}

function setSelectOrCustom(selectId, customInputId, val) {
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
    const groupAi = !!document.getElementById("adm-set-group-ai")?.checked;
    const groupAiCooldownRaw = document.getElementById("adm-set-group-ai-cooldown")?.value?.trim();
    const groupAiCooldown = groupAiCooldownRaw === "" || groupAiCooldownRaw == null ? null : parseInt(groupAiCooldownRaw, 10);
    const groupAiHourlyRaw = document.getElementById("adm-set-group-ai-hourly")?.value?.trim();
    const groupAiHourly = groupAiHourlyRaw === "" || groupAiHourlyRaw == null ? null : parseInt(groupAiHourlyRaw, 10);
    const groupAiDailyRaw = document.getElementById("adm-set-group-ai-daily")?.value?.trim();
    const groupAiDaily = groupAiDailyRaw === "" || groupAiDailyRaw == null ? null : parseInt(groupAiDailyRaw, 10);
    const charLimitFreeRaw = document.getElementById("adm-set-char-limit-free")?.value?.trim();
    const charLimitFree = charLimitFreeRaw === "" || charLimitFreeRaw == null ? null : parseInt(charLimitFreeRaw, 10);
    const charLimitPurchasedRaw = document.getElementById("adm-set-char-limit-purchased")?.value?.trim();
    const charLimitPurchased = charLimitPurchasedRaw === "" || charLimitPurchasedRaw == null ? null : parseInt(charLimitPurchasedRaw, 10);
    const freeAllowanceRaw = document.getElementById("adm-set-free-allowance")?.value?.trim();
    const freeAllowance = freeAllowanceRaw === "" || freeAllowanceRaw == null ? null : parseInt(freeAllowanceRaw, 10);
    const dailyJobLimitRaw = document.getElementById("adm-set-daily-job-limit")?.value?.trim();
    const dailyJobLimit = dailyJobLimitRaw === "" || dailyJobLimitRaw == null ? null : parseInt(dailyJobLimitRaw, 10);
    const recapFreeMaxRaw = document.getElementById("adm-set-recap-free-max")?.value?.trim();
    const recapFreeMax = recapFreeMaxRaw === "" || recapFreeMaxRaw == null ? null : parseInt(recapFreeMaxRaw, 10);
    const recapMaxRaw = document.getElementById("adm-set-recap-max")?.value?.trim();
    const recapMax = recapMaxRaw === "" || recapMaxRaw == null ? null : parseInt(recapMaxRaw, 10);
    const tieredPricing = !!document.getElementById("adm-set-tiered-pricing")?.checked;
    const reqChannelId = document.getElementById("adm-set-required-channel-id")?.value?.trim();
    const reqGroupId = document.getElementById("adm-set-required-group-id")?.value?.trim();
    const channelInvite = document.getElementById("adm-set-channel-invite-link")?.value?.trim();
    const groupInvite = document.getElementById("adm-set-group-invite-link")?.value?.trim();

    const tutVoiceEn = !!document.getElementById("adm-tut-voice-enabled")?.checked;
    const tutVoiceTitle = document.getElementById("adm-tut-voice-title")?.value?.trim() || "Voice Clone ပြုလုပ်နည်း လမ်းညွှန်";
    const tutVoiceLink = document.getElementById("adm-tut-voice-link")?.value?.trim() || "";

    const tutRecapEn = !!document.getElementById("adm-tut-recap-enabled")?.checked;
    const tutRecapTitle = document.getElementById("adm-tut-recap-title")?.value?.trim() || "Movie Recap ပြုလုပ်နည်း လမ်းညွှန်";
    const tutRecapLink = document.getElementById("adm-tut-recap-link")?.value?.trim() || "";
    const recapDemoAsset = document.getElementById("adm-recap-demo-asset")?.value?.trim() || "";

    const tutExtraLinks = {};
    for (const [elId, settingKey] of ADMIN_TUTORIAL_LINK_FIELDS) {
        tutExtraLinks[settingKey] = document.getElementById(elId)?.value?.trim() || "";
    }

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
        ...(Number.isFinite(charLimitFree) ? { character_limit_free: charLimitFree } : {}),
        ...(Number.isFinite(charLimitPurchased) ? { character_limit_purchased: charLimitPurchased } : {}),
        ...(Number.isFinite(freeAllowance) ? { free_monthly_allowance_seconds: freeAllowance } : {}),
        ...(Number.isFinite(dailyJobLimit) ? { free_daily_job_limit: dailyJobLimit } : {}),
        ...(Number.isFinite(recapFreeMax) ? { recap_free_max_duration_seconds: recapFreeMax } : {}),
        ...(Number.isFinite(recapMax) ? { recap_max_duration_seconds: recapMax } : {}),
        tiered_recap_pricing_enabled: tieredPricing,
        ...(reqChannelId ? { required_channel_id: reqChannelId } : {}),
        ...(reqGroupId ? { required_group_id: reqGroupId } : {}),
        ...(channelInvite ? { channel_invite_link: channelInvite } : {}),
        ...(groupInvite ? { group_invite_link: groupInvite } : {}),
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
        showStudioToast(data.message || t("admin_settings_saved"), 'success');
        loadAdminSettings();
    } catch (e) {
        showStudioToast(adminErrorText("admin_settings_save_err", e.message), 'error');
    }
}

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

// Wire the History detail's inline voice player (item 7): the audio
// element renders with data-auth-src and no src; the authenticated
// blob URL is attached lazily, only for the row being opened, so a
// long history list never fetches every job's audio up front.
function wireHubDetailAudio(scope) {
    if (!scope) return;
    scope.querySelectorAll("audio[data-auth-src]").forEach(audio => {
        if (!audio.getAttribute("src")) {
            setAuthMediaSource(audio, audio.getAttribute("data-auth-src"));
        }
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

// Detail block shown when a hub row is tapped. It reuses the exact same
// actions/handlers the old per-studio history cards offered (download,
// send-to-Telegram, in-player view, live tracking) — nothing new invented.
function hubHistoryDetailHtml(j) {
    const isRecap = j.job_type === "RECAP";
    let inner = "";
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
    if (isRecap && j.recap_settings) {
        // R40 item 13: compact settings summary from the persisted
        // recap_config (engine / voice / subtitle position / style /
        // font) so the user — and support — can see what a render used.
        const s = j.recap_settings;
        const parts = [];
        if (s.engine) parts.push(s.engine === "gemini" ? "Gemini" : "Smart Sync");
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
        if (typeof s.sub_bottom_margin === "number") parts.push(`Sub margin ${pctOf(s.sub_bottom_margin)}%`);
        else if (typeof s.sub_y === "number") parts.push(`Sub ${pctOf(s.sub_y)}%`);
        if (typeof s.blur_y === "number") parts.push(`Blur ${pctOf(s.blur_y)}%`);
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
        actions = `<span class="badge-expired text-xs" style="color:#fbbf24;">📝 ${escapeHtml(parkedNote)}</span>`;
        actions += ` <button type="button" onclick="openRecapScriptEditor('${j.id}')" class="history-dl-link">${t("btn_review_script")}</button>`;
        actions += ` <button type="button" onclick="cancelRecapJob('${j.id}')" class="history-dl-link">${t("btn_cancel_job")}</button>`;
        if (j.recap_config) {
            actions += ` <button type="button" onclick="rerunRecapFromHistory('${j.id}')" class="history-dl-link">${t("btn_rerun_recap")}</button>`;
        }
        actions += ` <button type="button" onclick="deleteHistoryItem('${j.id}')" class="history-dl-link">${t("btn_delete_history")}</button>`;
    } else if (j.status === "FAILED") {
        actions = `<span class="badge-expired text-xs" style="color:#f87171;">${isRecap ? t("hist_failed_recap") : t("hist_failed")}</span>`;
        // Batch L: one-tap retry (hidden when the server says the
        // source is gone — the note explains instead), re-run prefill
        // and delete for recap items; delete for every terminal item.
        if (isRecap) {
            if (j.can_retry !== false) {
                actions += ` <button type="button" onclick="retryRecapJob('${j.id}')" class="history-dl-link">${t("btn_retry_job")}</button>`;
            } else {
                inner += `<div class="history-item-body">${escapeHtml(t("recap_retry_upload_missing"))}</div>`;
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
        actions = `<span class="badge-expired text-xs" title="">${t("hist_expired")}</span>`;
    } else if (isRecap) {
        if (j.deliverables?.video) {
            actions += `<button type="button" onclick="openRecapInPlayer('${j.id}')" class="history-dl-link">${t("hist_watch")}</button> <a href="#" data-dl-url="${j.deliverables.video}" data-dl-name="${brandedDlName("Recap", "mp4", j.input_snippet, j.id)}" class="history-dl-link">🎬 Video</a>`;
            // Over-50MB rule: no Telegram action at all for outputs the
            // bot cannot send — Download is the only delivery action.
            if (j.telegram_sendable !== false) {
                actions += ` <button type="button" onclick="sendRecapToTelegram('${j.id}')" class="history-dl-link tg-btn">${t("hist_send_telegram")}</button>`;
            }
        }
        if (j.deliverables?.audio) actions += ` <a href="#" data-dl-url="${j.deliverables.audio}" data-dl-name="${brandedDlName("RecapAudio", "mp3", j.input_snippet, j.id)}" class="history-dl-link">🎙️ Audio</a>`;
        if (j.deliverables?.srt) actions += ` <a href="#" data-dl-url="${j.deliverables.srt}" data-dl-name="${brandedDlName("Subtitles", "srt", j.input_snippet, j.id)}" class="history-dl-link">📝 SRT</a>`;
        if (j.deliverables?.script) actions += ` <a href="#" data-dl-url="${j.deliverables.script}" data-dl-name="${brandedDlName("Script", "txt", j.input_snippet, j.id)}" class="history-dl-link">📜 TXT</a>`;
    } else if (j.download_url) {
        actions = `<a href="#" data-dl-url="${j.download_url}" data-dl-name="${brandedDlName("Voice", "wav", null, j.id)}" class="history-dl-link">📥 Download</a>`;
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

function hubHistoryRowHtml(j) {
    const isRecap = j.job_type === "RECAP";
    // Batch BB-3: TTS jobs get their own label — they used to render
    // as ordinary voice jobs (🎙️, no type tag) and were only
    // recognisable by voice name.
    const isTts = j.job_type === "TTS";
    const open = hubOpenDetailId === j.id;
    const snippet = j.input_snippet || (isRecap ? "Movie Recap" : isTts ? "TTS" : "Voice Synthesis");
    const title = isRecap ? escapeHtml(snippet) : `&quot;${escapeHtml(snippet)}&quot;`;
    return `
    <div class="hub-row ${hubStatusBorder(j.status)} ${open ? "open" : ""}" data-job-id="${j.id}">
        <button type="button" class="hub-row-main" onclick="toggleHistoryDetail('${j.id}')" aria-expanded="${open}">
            <span class="hub-type-icon ${isRecap ? "recap" : isTts ? "tts" : "voice"}">${isRecap ? "🎬" : isTts ? "🎧" : "🎙️"}</span>
            <span class="hub-row-text">
                <span class="hub-row-title">${title}</span>
                <span class="hub-row-meta">${isRecap ? "🎬 RECAP • " : isTts ? escapeHtml(t("job_badge_tts")) + " • " : ""}${escapeHtml(j.preset_voice || "")} · <span class="hub-time-ago">⏱️ ${escapeHtml(j.time_ago || "")}</span><span class="hub-live-meta"></span></span>
            </span>
            ${j.fallback_used ? `<span class="history-item-badge hub-fallback-badge">${t("hist_fallback_badge")}</span>` : ""}
            ${!isRecap && j.reference_trimmed ? `<span class="history-item-badge hub-fallback-badge">${t("hist_ref_trim_badge")}</span>` : ""}
            <span class="history-item-badge status-${(j.status || "").toLowerCase()}">${escapeHtml(j.status || "")}</span>
            <span class="hub-chevron">${open ? "⌃" : "›"}</span>
        </button>
        <div class="hub-detail ${open ? "" : "hidden"}">${hubHistoryDetailHtml(j)}</div>
    </div>`;
}

function renderHistoryHub() {
    const container = document.getElementById("hub-history-list");
    if (!container) return;

    if (hubHistoryCache === null && !hubHistoryError) {
        container.innerHTML = `<div class="hub-state"><p class="text-muted">${t("history_loading")}</p></div>`;
        return;
    }
    if (hubHistoryError) {
        container.innerHTML = `
            <div class="hub-state">
                <div class="hub-state-emoji">⚠️</div>
                <p class="hub-state-text">${t("history_error")}</p>
                <button type="button" class="mini-chip" onclick="loadUserHistory()">${t("history_retry")}</button>
            </div>`;
        return;
    }

    const all = hubHistoryCache || [];
    if (all.length === 0) {
        container.innerHTML = `
            <div class="hub-state">
                <div class="hub-state-emoji">📜</div>
                <p class="hub-state-text" data-i18n="history_empty">${t("history_empty")}</p>
                <p class="text-muted hub-state-sub">${t("history_empty_hint")}</p>
            </div>`;
        return;
    }

    const jobs = hubHistoryFilter === "all" ? all : all.filter(h => h.job_type === hubHistoryFilter);
    if (jobs.length === 0) {
        container.innerHTML = `
            <div class="hub-state">
                <div class="hub-state-emoji">🔍</div>
                <p class="hub-state-text">${t("history_filter_empty")}</p>
                <button type="button" class="mini-chip" onclick="setHubHistoryFilter('all')">${t("history_show_all")}</button>
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
        if (!res.ok) throw new Error(`history ${res.status}`);

        const data = await res.json();
        const history = data.history || [];
        // Unified merge: the endpoint already returns both job types; sort
        // newest-first client-side by created_at so the hub order never
        // depends on server grouping.
        history.sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
        hubHistoryCache = history;
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
                } else if (data.status === "RUNNING" || data.status === "QUEUED") {
                    state.recapJobId = recapJobId;
                    startRecapJobPolling(recapJobId);
                } else if (data.status === "FAILED") {
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
document.addEventListener("DOMContentLoaded", () => {
    initTelegram();
    initEntryGate();
    setLanguage(currentLang);
    setupTabs();
    // AZ-4: park the sliding nav pill under the initially-active
    // item once first layout exists.
    requestAnimationFrame(() => { try { moveNavPill(); } catch (e) {} try { moveSpeedPills(); } catch (e) {} try { moveSegPills(); } catch (e) {} });
    setupVoiceStudio();
    setupRecapVisualEditor();
    initRecapSteps();
    setupAlertCarousel();
    setupDownloader();
    setupSlipPreview();
    setupStickyCtas();
    setupSamplePlayers();

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

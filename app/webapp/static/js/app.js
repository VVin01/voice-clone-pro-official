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
    toast.innerHTML = `
        <span class="toast-icon">${icons[type] || icons.info}</span>
        <span class="toast-msg"></span>
        <button class="toast-close" onclick="this.parentNode.remove()" aria-label="Close">✕</button>
    `;
    // The message is COPY, never markup (hunt X2): it is rendered via
    // textContent so no caller- or server-supplied string can inject
    // HTML into the toast. Caller audit: all ~86 call sites pass plain
    // i18n copy — none relied on markup in the message.
    toast.querySelector('.toast-msg').textContent = message;

    document.body.appendChild(toast);
    if (duration > 0) setTimeout(() => toast && toast.remove(), duration);
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
    // (b) Voice submit rejections that happen BEFORE any charge
    // (_voice_job_error_detail's closed Burmese set: balance, daily
    // limit, concurrency, character limit, model/file problems). None
    // of them may wear err_voice_failed's "balance was refunded" copy.
    if (flow === 'voice' && has(
        'လက်ကျန်မိနစ် မလုံလောက်ပါ',
        'ယနေ့ အခမဲ့ ကန့်သတ်ချက် ပြည့်သွားပါပြီ',
        'တစ်ပြိုင်တည်း လုပ်ဆောင်နေသော အလုပ်များနေပါသည်',
        'စာသား စာလုံးရေ ကန့်သတ်ချက်ထက် ကျော်လွန်နေပါသည်',
        'အသံမော်ဒယ် မတွေ့ရှိပါ',
        'အသံဖိုင် မတွေ့ရှိပါ',
        'ဒီအသံ မော်ဒယ် အဟောင်းကို မသုံးနိုင်တော့ပါ',
        'အလုပ်ဖန်တီးရာတွင် အမှားဖြစ်ပေါ်ပါသည်',
    )) {
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
 * Voice Clone Pro & AI Movie Recap Studio — Studio Ultra Suite
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
    recapZoom: 1.06,
    recapSpeed: "+12%",
    recapStoryStyle: "viral",
    recapBurnSubs: true,
    recapBlurSubs: true,
    recapLogo: true,
    recapLogoPos: "top-right",
    recapSubStyle: "yellow",
    recapSubFont: "default",
    recapFontSize: 16,
    recapLines: 1,
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
function refreshStickyCtas() {
    STICKY_CTA_PAIRS.forEach(p => {
        const bar = document.getElementById(p.bar);
        const src = document.getElementById(p.src);
        const btn = document.getElementById(p.btn);
        if (!bar || !src || !btn) return;
        const pane = document.getElementById(p.tab);
        const tabActive = !!(pane && pane.classList.contains("active"));
        btn.disabled = !!src.disabled;
        bar.classList.toggle("hidden", !(tabActive && stickyCtaInView[p.src] === false));
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
            if (res.status === 403 && url.includes("/api/") && !url.includes("/api/user/verify-membership")) {
                res.clone().json().then((data) => {
                    const detail = (data && data.detail) || "";
                    if (detail.includes("MEMBERSHIP_REQUIRED") || detail.includes("GATE_NOT_CONFIGURED")) {
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

function clearAuthMediaSource(el) {
    if (!el) return;
    const prev = _authMediaObjectUrls.get(el);
    if (prev) { URL.revokeObjectURL(prev); _authMediaObjectUrls.delete(el); }
    el.removeAttribute("src");
    try { el.load(); } catch (e) {}
}

async function downloadAuthFile(url, filename) {
    if (!url) return;
    try {
        const objUrl = await fetchAuthBlobUrl(url);
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
        downloadAuthFile(a.getAttribute("data-dl-url"), a.getAttribute("data-dl-name") || a.getAttribute("download") || undefined);
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

    // Recap Step-1 mini button: uses the configured recap title and
    // respects tutorial_recap_enabled (hidden when disabled, like the
    // Voice banner). (The old dead block that managed a non-existent
    // recap banner element has been removed.)
    const recapStepBtn = document.getElementById("recap-step1-tut-btn");
    if (recapStepBtn) {
        if (tutorialConfig.recap.enabled) {
            recapStepBtn.classList.remove("hidden");
            recapStepBtn.textContent = tutorialDisplayTitle(tutorialConfig.recap.title, "recap_tutorial_title");
            recapStepBtn.title = tutorialDisplayTitle(tutorialConfig.recap.title, "recap_tutorial_title");
        } else {
            recapStepBtn.classList.add("hidden");
        }
    }

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
    openTutorialUrl(_supportLink);
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
        actionBtn.innerText = actionText || (currentLang === "my" ? "ကြည့်ရှုမည်" : "View Result");
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
    // Tab switches start at the top of the new pane (premium polish).
    window.scrollTo({ top: 0, behavior: "auto" });
    refreshStickyCtas();
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
            // Tab switches start at the top of the new pane (premium polish).
            window.scrollTo({ top: 0, behavior: "auto" });
            refreshStickyCtas();

            if (targetTab === "tab-history") {
                loadUserHistory();
            }

            if (targetTab === "tab-admin") {
                loadAdminData();
                loadAdminQueue();
                loadAdminSettings();
                if (window.adminQueueInterval) clearInterval(window.adminQueueInterval);
                window.adminQueueInterval = setInterval(() => {
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
            vipBadge.innerText = data.is_admin ? "👑 ADMIN" : (data.is_vip ? "👑 PRO" : "FREE");
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
            const limit = data.character_limit || (data.is_vip ? 5000 : 1000);
            charCount.innerText = `0 / ${limit} ${t("char_counter_unit")}`;
        }

        const recapLimitText = document.getElementById("recap-tier-limit-text");
        if (recapLimitText) {
            if (data.is_vip) {
                recapLimitText.innerText = (currentLang === "my")
                    ? "🎬 VIP အဖွဲ့ဝင်: ဗီဒီယိုကြာချိန် ၁၀ မိနစ်အထိ • Watermark ကင်းလွတ်ခွင့် ရရှိထားပါသည် ✨"
                    : "🎬 VIP Member: Up to 10 Minutes per Recap • Watermark-Free Output ✨";
            } else {
                recapLimitText.innerText = (currentLang === "my")
                    ? "🎬 အခမဲ့အသုံးပြုသူ: အများဆုံး ၂ မိနစ် (Watermark ပါဝင်မည်) • VIP ရယူပါက Watermark မပါဝင်ပါ"
                    : "🎬 Free Creator: Max 2 Minutes (Watermark Included) • Upgrade to VIP for Watermark-Free";
            }
        }

        updateGateBanner(data);
        refreshWelcomeStrip();
        if (data.is_banned) { setEntryGateMode("banned", data); lockEntryGate(); }
        updateVoicePreflight();

        // Enable Admin Tab if user is administrator
        if (data.is_admin) {
            if (adminNavBtn) adminNavBtn.classList.remove("hidden");
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
        card.innerHTML = `
            <div class="persona-header">
                <span class="persona-avatar">${v.avatar}</span>
                <div class="persona-meta">
                    <span class="persona-name">${v.name}</span>
                    <span class="persona-badge">${v.badge}</span>
                </div>
                ${isSelected ? '<span class="selected-indicator">✓</span>' : ''}
            </div>
            <p class="persona-desc">${v.description}</p>
            <div class="persona-footer-row">
                <button type="button" class="voice-preview-btn" onclick="event.stopPropagation(); playVoicePreview('${v.id}', this)">
                    <span class="preview-btn-icon">▶️</span>
                    <span class="preview-btn-label">${t("preview_voice") || "နမူနာ နားထောင်မည်"}</span>
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
    // ("မင်းမင်း (ရုပ်ရှင်ဇာတ်လမ်းပြောသူ)") that truncates mid-parenthesis at summary
    // chip width; the chip shows the persona's SHORT name only. The
    // picker itself keeps the full label.
    return String(name || "").replace(/\s*[\(（][^\)）]*[\)）]\s*$/, "").trim();
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
    const toggle = document.getElementById("btn-toggle-picker");
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
            label.innerText = currentPreviewButton.classList.contains("mini") ? (t("preview_voice") || "နမူနာ") : (t("btn_preview_voice") || "နမူနာ နားထောင်မည်");
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
        if (label) label.innerText = t("preview_playing") || "ဖွင့်နေသည်...";
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

function previewSelectedRecapVoice() {
    // The all-voices <select> was removed (all 12 voices are cards now);
    // kept as a thin wrapper over the current card selection.
    playVoicePreview(state.recapVoice || "komin", null);
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
        if (label) label.innerText = t("preview_playing") || "ဖွင့်နေသည်...";
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
            const max = state.user?.character_limit || (state.user?.is_vip ? 5000 : 1000);
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
    const speedPills = document.querySelectorAll("#speed-selector .pill-btn");
    speedPills.forEach(pill => {
        pill.addEventListener("click", () => {
            haptic("selection");
            speedPills.forEach(p => p.classList.remove("active"));
            pill.classList.add("active");
            state.speed = pill.getAttribute("data-speed");
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
    paintGateChip(document.getElementById("gate-chip-channel"), "📢 Channel", channelOk);
    paintGateChip(document.getElementById("gate-chip-group"), "👥 Group", groupOk);
    paintGateChip(document.getElementById("gate-lock-chip-channel"), "📢 Channel", channelOk);
    paintGateChip(document.getElementById("gate-lock-chip-group"), "👥 Group", groupOk);
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
    const chips = document.querySelector(".gate-lock-chips");
    const chLink = document.getElementById("gate-lock-link-channel");
    const grLink = document.getElementById("gate-lock-link-group");
    const verifyBtn = document.getElementById("gate-lock-btn-verify");
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

function initEntryGate() {
    lockEntryGate();
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
    const max = u.character_limit || (u.is_vip ? 5000 : 1000);
    const bal = u.is_admin ? "∞" : `${u.balance_minutes ?? 0}`;
    let line = tf("preflight_line", { len, max, bal });
    if (len > max) line += t("preflight_over_limit");
    else if (!u.is_admin && (u.balance_minutes ?? 0) <= 0) line += t("preflight_balance_empty");
    box.innerText = line;
    box.classList.toggle("preflight-warn", len > max || (!u.is_admin && (u.balance_minutes ?? 0) <= 0));
}

// ── Submit Voice Job & Live Polling ─────────────────────────────────────────
async function handleVoiceGenerate() {
    const textInput = document.getElementById("voice-text-input");
    const text = textInput ? textInput.value.trim() : "";
    if (!text) {
        showStudioToast(t("toast_empty_text"), 'warning');
        return;
    }

    const maxChars = state.user?.character_limit || (state.user?.is_vip ? 5000 : 1000);
    if (text.length > maxChars) {
        showStudioToast(tf("text_too_long", {
            len: text.length,
            tier: state.user?.is_vip ? "VIP" : (currentLang === "my" ? "အခမဲ့ (Free)" : "Free"),
            max: maxChars,
            vip_note: state.user?.is_vip ? "" : (currentLang === "my" ? " ကန့်သတ်ချက်မရှိ (စာလုံးရေ ၅၀၀၀ အထိ) အသုံးပြုလိုပါက VIP သို့ အဆင့်မြှင့်တင်ပါခင်ဗျာ။" : " Upgrade to VIP for up to 5000 characters with no limit."),
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

    if (progressBox) progressBox.classList.remove("hidden");
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

    } catch (err) {
        showErrorToast(err, 'voice');
        if (progressBox) progressBox.classList.add("hidden");
        if (btnGen) btnGen.disabled = false;
    }
}

function startJobPolling(jobId) {
    if (state.pollTimer) clearInterval(state.pollTimer);

    let tick = 0;
    let failStreak = 0;
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
            } else if (data.status === "RUNNING") {
                setVoiceStage("working");
                if (progressFill) progressFill.style.width = "80%";
                if (statusMsg) statusMsg.innerText = t("voice_synthesizing");
            } else if (data.status === "SUCCESS") {
                clearInterval(state.pollTimer);
                setVoiceStage("success");
                haptic("success");
                if (data.fallback_used) { try { showStudioToast(t("voice_fallback_notice"), 'info'); } catch (e) {} }
                if (progressBox) progressBox.classList.add("hidden");
                if (resultBox) {
                    resultBox.classList.remove("hidden");
                    resultBox.scrollIntoView({ behavior: "smooth" });
                }
                if (btnGen) btnGen.disabled = false;

                const btnTg = document.getElementById("btn-voice-send-telegram");
                if (btnTg) {
                    btnTg.disabled = false;
                    btnTg.classList.remove("sent");
                    btnTg.innerHTML = `<span class="btn-icon">📱</span> <span>${t("btn_send_voice_telegram")}</span>`;
                }

                if (audioPlayer && data.output_audio_url) {
                    setAuthMediaSource(audioPlayer, data.output_audio_url).then((ok) => {
                        if (ok) { audioPlayer.load(); audioPlayer.play().catch(() => {}); }
                    });
                }
                if (btnDl && data.output_audio_url) {
                    btnDl.setAttribute("data-dl-url", data.output_audio_url);
                    btnDl.setAttribute("data-dl-name", "voice_clone.wav");
                }
                loadUserProfile();
                loadUserHistory();

                showFloatingAlert(
                    t("alert_voice_ready") || "🎉 AI Voice ဖန်တီးမှု ပြီးစီးပါပြီ!",
                    currentLang === "my" ? "အသံဖိုင် ပြင်ဆင်ပြီးစီးပါပြီ။ ဖွင့်နားထောင်နိုင်ပါပြီ။" : "Your AI voice is ready to listen or send to Telegram.",
                    () => {
                        if (resultBox) resultBox.scrollIntoView({ behavior: "smooth" });
                    },
                    currentLang === "my" ? "🎧 နားထောင်မည်" : "🎧 Listen"
                );

            } else if (data.status === "FAILED") {
                clearInterval(state.pollTimer);
                haptic("error");
                showStudioToast(mapErrorToToast(data.error_message, 'voice'), 'error');
                if (progressBox) progressBox.classList.add("hidden");
                if (btnGen) btnGen.disabled = false;
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
    if (!show) {
        hint.classList.add("hidden");
        hint.textContent = "";
        return;
    }
    const est = estimateRecapSegmentParts(dur, state.recapSplitMinutes || 3);
    let text = tf("recap_split_cost_estimate", { parts: est, credits: est });
    const lowBalance = (state.user?.recap_credits ?? 0) < est;
    if (lowBalance) text += " " + t("recap_split_cost_low_balance");
    hint.textContent = text;
    hint.style.color = lowBalance ? "#f87171" : "";
    hint.classList.remove("hidden");
}

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
    if (controlsGroup) controlsGroup.classList.toggle("hidden", !ready);
    if (ratioGroup) ratioGroup.classList.toggle("hidden", !ready);
    if (speedGroup) speedGroup.classList.toggle("hidden", !ready);
    if (transformGroup) transformGroup.classList.toggle("hidden", !ready);
}
window.setRecapPreviewState = setRecapPreviewState;

// True when some usable source preview exists (link thumbnail, an
// uploaded file, or a live local <video> preview in the canvas).
function recapHasSource() {
    const cv = document.getElementById("canvas-video-player");
    return Boolean(
        state.recapThumbnailUrl ||
        state.uploadedVideoPath ||
        (cv && cv.src && !cv.classList.contains("hidden"))
    );
}
window.recapHasSource = recapHasSource;

// ── TAB 2: Recap Studio Configuration & Visual Mockup ──────────────────────
function setupRecapVisualEditor() {
    const sliderBlurY = document.getElementById("slider-blur-y");
    const sliderBlurH = document.getElementById("slider-blur-h");
    const sliderSubY = document.getElementById("slider-sub-y");

    const valBlurY = document.getElementById("val-blur-y");
    const valBlurH = document.getElementById("val-blur-h");
    const valSubY = document.getElementById("val-sub-y");

    const previewBlur = document.getElementById("preview-blur-box");
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
                const msg = (currentLang === "my")
                    ? (state.user?.is_vip ? `အများဆုံး ${maxM} မိနစ်ထက် ကျော်လွန်နေပါသည်` : `အများဆုံး ၂ မိနစ်ထက် ကျော်လွန်နေပါသည် (VIP ရယူပါ)`)
                    : (state.user?.is_vip ? `Exceeds max ${maxM}m` : `Exceeds Free 2m limit (Upgrade to VIP)`);
                durSpan.innerHTML = `⏱️ ${data.estimated_duration || "00:00"} <span style="color:#ef4444; font-weight:700; margin-left:4px;">(${msg})</span>`;
            } else {
                durSpan.className = "mini-tag";
                durSpan.innerHTML = `⏱️ ${data.estimated_duration || "00:00"} <span style="color:#10b981; font-weight:700; margin-left:4px;">(${t("recap_duration_valid") || "Ready"})</span>`;
            }
        }
        if (subSpan) subSpan.innerText = data.has_subtitles ? t("recap_sub_ready") : t("recap_sub_ai");
    }

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
        state.recapUploadPending = false;
        state.recapVideoTitle = data.title || file.name;
        state.recapSourceDuration = data.duration_seconds;
        updateRecapSplitCostHint();
        if (data.width && data.height) {
            state.recapSourceWidth = data.width;
            state.recapSourceHeight = data.height;
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
                const tierLabel = state.user?.is_vip ? "VIP" : "အခမဲ့ (Free)";
                const durM = Math.floor(data.duration_seconds / 60);
                const durS = Math.floor(data.duration_seconds % 60);
                const durStr = `${durM}:${durS < 10 ? '0' : ''}${durS}`;
                durSpan.innerHTML = `⏱️ ${data.estimated_duration} <span style="color:#ef4444; font-weight:700; margin-left:4px;">(${tierLabel} ကန့်သတ်ချက်ထက် ကျော်လွန်နေပါသည်)</span>`;
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
        const totStr = formatSecToMMSS(totalSec || state.recapSourceDuration || 60);
        seekBadge.innerText = `⏱️ ${curStr}s / ${totStr}s`;
    }

    function seekCanvasFrame(targetSec) {
        const total = state.recapSourceDuration || (canvasVideo?.duration) || 60;
        const clamped = Math.max(0, Math.min(total, targetSec));
        if (sliderSeek) sliderSeek.value = clamped;
        updateSeekTimeDisplay(clamped, total);

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
        }

        seekPills.forEach(pill => {
            const pSec = parseFloat(pill.getAttribute("data-sec"));
            pill.classList.toggle("active", Math.abs(pSec - clamped) < 1.0);
        });
    }

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
                        btnPlayToggle.innerText = "⏸️ ရပ်မည် (Pause)";
                        btnPlayToggle.classList.add("active");
                    }).catch(err => {
                        console.warn("Play error:", err);
                    });
                } else {
                    canvasVideo.pause();
                    btnPlayToggle.innerText = "⏯️ နမူနာကြည့် (Play)";
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
            btnPlayToggle.innerText = "⏯️ နမူနာကြည့် (Play)";
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

    // 1. Slider Controls for Original Blur and Subtitle Y (100% synced with backend FFmpeg coordinates)
    function syncBlurBoxVisual() {
        if (!previewBlur) return;
        const y = sliderBlurY ? parseFloat(sliderBlurY.value) : 85;
        const h = sliderBlurH ? parseFloat(sliderBlurH.value) : 7;
        if (valBlurY) valBlurY.innerText = `${Math.round(y)}%`;
        if (valBlurH) valBlurH.innerText = `${Math.round(h)}%`;
        // Anchored at top: y% with translateY(-50%) places blur center exactly at y% (1:1 with FFmpeg y_center)
        previewBlur.style.top = `${y}%`;
        previewBlur.style.height = `${h}%`;
        previewBlur.style.bottom = "auto";
    }

    function syncSubBoxVisual() {
        if (!previewSub) return;
        const y = sliderSubY ? parseFloat(sliderSubY.value) : 85;
        if (valSubY) valSubY.innerText = `${Math.round(y)}%`;
        // Anchored at top: y% with translateY(-50%) places subtitle center exactly at y% (1:1 with ASS subtitle baseline offset)
        previewSub.style.top = `${y}%`;
        previewSub.style.bottom = "auto";
    }

    if (sliderBlurY && previewBlur && valBlurY) {
        sliderBlurY.addEventListener("input", () => {
            syncBlurBoxVisual();
        });
    }

    if (sliderBlurH && previewBlur && valBlurH) {
        sliderBlurH.addEventListener("input", () => {
            syncBlurBoxVisual();
        });
    }

    if (sliderSubY && previewSub && valSubY) {
        sliderSubY.addEventListener("input", () => {
            syncSubBoxVisual();
        });
    }

    // ── Interactive Drag & Drop / Manual Move on Canvas ──────────────────
    function enableVerticalDrag(boxElement, onUpdateY, getInitialY) {
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
            const clampedY = Math.max(10, Math.min(95, Math.round(startPercent + deltaPercent)));
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

        // Mobile Touch Drag
        boxElement.addEventListener("touchstart", (e) => {
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

    // Attach dragging to Blur Box
    enableVerticalDrag(previewBlur, (y) => {
        if (sliderBlurY) sliderBlurY.value = y;
        syncBlurBoxVisual();
    }, () => sliderBlurY ? parseFloat(sliderBlurY.value) : 85);

    // Attach dragging to Subtitle Box
    enableVerticalDrag(previewSub, (y) => {
        if (sliderSubY) sliderSubY.value = y;
        syncSubBoxVisual();
    }, () => sliderSubY ? parseFloat(sliderSubY.value) : 85);

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
            const newH = Math.max(3, Math.min(22, Math.round(startH + deltaPct)));
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
    syncBlurBoxVisual();
    syncSubBoxVisual();

    // ── Setup Compact Live Canvas Toolbar Tabs ──
    const tbTabBtns = document.querySelectorAll(".tb-tab-btn");
    const tbPanels = {
        seek: document.getElementById("tb-panel-seek"),
        mask: document.getElementById("tb-panel-mask"),
        style: document.getElementById("tb-panel-style"),
    };

    tbTabBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            const target = btn.getAttribute("data-tb-tab");
            if (!target) return;
            haptic("selection");
            tbTabBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");

            // F1: the blur box's label is an editing affordance — it
            // paints only while its own toolbar tab (Mask) is active.
            if (previewBlur) previewBlur.classList.toggle("label-on", target === "mask");

            Object.entries(tbPanels).forEach(([key, panel]) => {
                if (panel) {
                    if (key === target) panel.classList.remove("hidden");
                    else panel.classList.add("hidden");
                }
            });
        });
    });

    // ── Helper: Real-time Live Canvas Mockup Visual Updater ──
    // Exposed on window: toggleAdvancedRecapStudio() (top-level) calls it
    // when the settings accordion opens; without this it throws ReferenceError.
    window.updateCanvasMockupVisuals = updateCanvasMockupVisuals;
    function updateCanvasMockupVisuals() {
        if (!canvas) return;

        const zoom = state.recapZoom || 1.06;

        // 1. Aspect Ratio Frame Class
        if (mockupFrame) {
            let ratioClass = "ratio-9-16";
            if (state.recapRatio === "16:9") ratioClass = "ratio-16-9";
            else if (state.recapRatio === "1:1") ratioClass = "ratio-1-1";
            else if (state.recapRatio === "Original") ratioClass = "ratio-16-9";
            mockupFrame.className = `mockup-screen ${ratioClass}`;
            mockupFrame.style.aspectRatio = "";
        }

        // 2. Canvas Base Classes & Fill Mode
        const fillMode = state.recapRatioFill || "blur";
        canvas.className = `video-bg-placeholder fill-${fillMode} ${state.recapMirror ? 'mirrored' : ''}`;

        // 3. Keep thumbnail on Canvas & Inner Movie
        const innerMovie = document.getElementById("mockup-inner-movie");
        const fillBadge = document.getElementById("mockup-fill-badge");
        const origSub = document.getElementById("mockup-orig-sub");
        const hasActiveMedia = Boolean(state.recapThumbnailUrl || state.uploadedVideoPath || (canvasVideo && !canvasVideo.classList.contains("hidden")));
        
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
            if (state.recapRatio === "Original") {
                if (rSource) {
                    rCanvas = rSource;
                    if (mockupFrame) {
                        let ratioClass = "ratio-9-16";
                        if (rSource > 1.3) ratioClass = "ratio-16-9";
                        else if (rSource >= 0.8 && rSource <= 1.3) ratioClass = "ratio-1-1";
                        mockupFrame.className = `mockup-screen ${ratioClass}`;
                        if (state.recapSourceWidth && state.recapSourceHeight) {
                            mockupFrame.style.aspectRatio = `${state.recapSourceWidth} / ${state.recapSourceHeight}`;
                        }
                    }
                } else {
                    rCanvas = 16 / 9;
                }
            }

            // Fallback source ratio if media not yet analyzed
            if (!rSource) {
                rSource = (state.recapRatio === "9:16" || state.recapRatio === "1:1") ? (16 / 9) : (9 / 16);
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
                thumbImg.alt = "";
                // A dead thumbnail URL must hide the element, never show
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

            const backdropEl = document.getElementById("video-backdrop-blur");
            const noThumbEl = document.getElementById("canvas-no-thumb");
            let mediaVisible = false;
            if (canvasVideo && !canvasVideo.classList.contains("hidden") && canvasVideo.src) {
                mediaVisible = true;
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
                canvasVideo.style.display = "block";
                canvasVideo.style.opacity = "1";
                canvasVideo.style.objectFit = "fill";
                canvasVideo.style.transform = "none";
                canvasVideo.style.width = `${vidW.toFixed(3)}%`;
                canvasVideo.style.height = `${vidH.toFixed(3)}%`;
                canvasVideo.style.left = `${vidLeft.toFixed(3)}%`;
                canvasVideo.style.top = `${vidTop.toFixed(3)}%`;
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
    }

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
            state.recapZoom = parseFloat(btn.getAttribute("data-zoom") || "1.06");
            updateCanvasMockupVisuals();
        });
    });

    // 6. Narration Speed & Pacing (+0%, +8%, +12%, +18%, +22%)
    const speedBtns = document.querySelectorAll("#recap-speed-picker .pill-btn");
    speedBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            speedBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            state.recapSpeed = btn.getAttribute("data-speed") || "+12%";
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

    // 6c. Source language pills (auto = detect; else a Whisper code)
    const langBtns = document.querySelectorAll("#recap-lang-picker .pill-btn");
    langBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            langBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            state.recapSourceLanguage = btn.getAttribute("data-lang") || "auto";
        });
    });

    // 6d. Render quality pills (standard = the historical default encode)
    const qualityBtns = document.querySelectorAll("#recap-quality-picker .pill-btn");
    qualityBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            qualityBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            state.recapRenderQuality = btn.getAttribute("data-quality") || "standard";
        });
    });

    // 6e. Narration engine pills (VoxCPM persona / Gemini TTS) + Gemini voice select
    const engineBtns = document.querySelectorAll("#recap-engine-picker .pill-btn");
    engineBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            engineBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            state.recapTtsEngine = btn.getAttribute("data-engine") || "voxcpm";
            const gv = document.getElementById("recap-gemini-voice-wrap");
            if (gv) gv.classList.toggle("hidden", state.recapTtsEngine !== "gemini");
            const pa = document.getElementById("recap-persona-area");
            if (pa) pa.classList.toggle("hidden", state.recapTtsEngine === "gemini");
        });
    });
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
        });
    });

    // Reset Layout Helper (Blur Y/H, Sub Y, Crop, Zoom)
    function resetRecapCanvasLayout() {
        haptic("medium");
        if (sliderBlurY) sliderBlurY.value = "85";
        if (sliderBlurH) sliderBlurH.value = "7";
        if (sliderSubY) sliderSubY.value = "85";
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

        state.recapZoom = 1.06;
        document.querySelectorAll("#recap-zoom-picker .pill-btn").forEach(b => {
            b.classList.toggle("active", b.getAttribute("data-zoom") === "1.06");
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
            if (previewLogo) previewLogo.style.display = state.recapLogo ? "block" : "none";
        });
    }

    // 9. Subtitle Style Presets (Yellow, White, Boxed, Neon)
    const styleChips = document.querySelectorAll("#sub-style-picker .style-chip");
    styleChips.forEach(chip => {
        chip.addEventListener("click", () => {
            haptic("selection");
            styleChips.forEach(c => c.classList.remove("active"));
            chip.classList.add("active");
            const style = chip.getAttribute("data-style") || "yellow";
            state.recapSubStyle = style;
            if (previewSub) {
                previewSub.className = `preview-sub-box ${style}`;
            }
        });
    });

    // 10. Subtitle Live Preview Renderer
    function updateSubtitleVisualPreview() {
        const previewText = document.getElementById("preview-sub-text-el");
        const previewBox = document.getElementById("preview-sub-box");
        if (!previewText || !previewBox) return;

        const lines = state.recapLines || 1;
        const size = state.recapFontSize || 16;

        // Font scaling for miniature live canvas mockup
        const fontScaleMap = { 13: 11, 16: 13, 19: 15, 22: 17 };
        const px = fontScaleMap[size] || 13;
        previewText.style.fontSize = `${px}px`;

        const fontMap = {
            "default": "'Padauk', sans-serif",
            "serif": "'Noto Serif Myanmar', serif",
            "padauk": "'Padauk', sans-serif"
        };
        previewText.style.fontFamily = fontMap[state.recapSubFont || "default"] || fontMap["default"];

        if (lines === 1) {
            previewBox.classList.remove("two-lines");
            previewBox.classList.add("one-line");
            previewText.innerHTML = t("sub_sample_text");
            // NOT nowrap: with nowrap the one-line line-clamp can never
            // engage, and a long sample (e.g. the i18n subtitle sample
            // applied by setLanguage) overflows the bar and hard-clips at
            // both ends. Normal flow + the CSS line-clamp gives a clean
            // single line with an ellipsis instead.
            previewText.style.whiteSpace = "normal";
            previewText.style.lineHeight = "1.8";
        } else {
            previewBox.classList.remove("one-line");
            previewBox.classList.add("two-lines");
            previewText.innerHTML = t("sub_sample_text");
            previewText.style.whiteSpace = "normal";
            previewText.style.lineHeight = "1.8";
        }
    }

    // 11. Font Size Picker (Synchronized across toolbar & advanced settings)
    function syncSubtitleFontSize(sizeVal) {
        state.recapFontSize = parseInt(sizeVal || "16");
        document.querySelectorAll("#font-size-picker-toolbar .pill-btn").forEach(p => {
            p.classList.toggle("active", parseInt(p.getAttribute("data-size")) === state.recapFontSize);
        });
        updateSubtitleVisualPreview();
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
    }
    document.querySelectorAll("#sub-lines-picker-toolbar .pill-btn").forEach(pill => {
        pill.addEventListener("click", () => {
            haptic("selection");
            syncSubtitleLines(pill.getAttribute("data-lines"));
        });
    });

    // 12b. Subtitle Font Picker (default / serif / padauk)
    const subFontBtns = document.querySelectorAll("#recap-sub-font-picker .pill-btn");
    subFontBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            subFontBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            state.recapSubFont = btn.getAttribute("data-font") || "default";
            updateSubtitleVisualPreview();
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
                const sliderSeek = document.getElementById("slider-canvas-seek");
                const seekBadge = document.getElementById("canvas-seek-time-badge");
                if (sliderSeek) {
                    sliderSeek.max = Math.min(600, data.duration_seconds || 60);
                    sliderSeek.value = Math.min(5, data.duration_seconds || 5);
                }
                if (seekBadge) {
                    seekBadge.innerText = `⏱️ 00:05s / ${data.estimated_duration || "00:00"}s`;
                }
                setRecapPreviewState("ready");
            } catch (err) {
                setRecapPreviewState(recapHasSource() ? "ready" : "empty");
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
                } else {
                    showErrorToast(err, 'generic');
                }
            } finally {
                btnAnalyze.disabled = false;
                btnAnalyze.innerText = originalText;
            }
        });
    }

    // Auto-detect on input / paste with live YouTube thumbnail & debounce
    let recapUrlTimer = null;
    const urlInput = document.getElementById("recap-url-input");
    if (urlInput) {
        urlInput.addEventListener("input", () => {
            const val = urlInput.value.trim();
            if (!val) {
                state.recapThumbnailUrl = null;
                updateCanvasMockupVisuals();
                if (recapInfoBox) recapInfoBox.classList.add("hidden");
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

    // ── Interactive Touch / Mouse Crop Guide Handles ──
    function setupInteractiveCropHandles() {
        const handleT = document.getElementById("crop-handle-t");
        const handleB = document.getElementById("crop-handle-b");
        const handleL = document.getElementById("crop-handle-l");
        const handleR = document.getElementById("crop-handle-r");
        const innerMovie = document.getElementById("mockup-inner-movie");
        if (!innerMovie || (!handleT && !handleB && !handleL && !handleR)) return;

        function attachHandleDrag(handle, onDelta) {
            if (!handle) return;
            let startCoord = 0;
            let isDragging = false;

            function onStart(coord) {
                isDragging = true;
                startCoord = coord;
                haptic("light");
            }
            function onMove(coord) {
                if (!isDragging) return;
                const delta = coord - startCoord;
                if (Math.abs(delta) >= 3) {
                    startCoord = coord;
                    onDelta(delta > 0 ? 1 : -1);
                    updateCanvasMockupVisuals();
                }
            }
            function onEnd() {
                isDragging = false;
            }

            // Desktop Mouse Drag
            handle.addEventListener("mousedown", (e) => {
                e.preventDefault();
                e.stopPropagation();
                const isHoriz = (handle === handleL || handle === handleR);
                onStart(isHoriz ? e.clientX : e.clientY);
                const mm = (ev) => {
                    ev.preventDefault();
                    onMove(isHoriz ? ev.clientX : ev.clientY);
                };
                const mu = () => {
                    onEnd();
                    window.removeEventListener("mousemove", mm);
                    window.removeEventListener("mouseup", mu);
                };
                window.addEventListener("mousemove", mm);
                window.addEventListener("mouseup", mu);
            });

            // Mobile Touch Drag
            handle.addEventListener("touchstart", (e) => {
                if (e.touches && e.touches.length > 0) {
                    e.stopPropagation();
                    const isHoriz = (handle === handleL || handle === handleR);
                    onStart(isHoriz ? e.touches[0].clientX : e.touches[0].clientY);
                }
            }, { passive: true });

            handle.addEventListener("touchmove", (e) => {
                if (isDragging && e.touches && e.touches.length > 0) {
                    e.preventDefault();
                    e.stopPropagation();
                    const isHoriz = (handle === handleL || handle === handleR);
                    onMove(isHoriz ? e.touches[0].clientX : e.touches[0].clientY);
                }
            }, { passive: false });

            handle.addEventListener("touchend", onEnd);
            handle.addEventListener("touchcancel", onEnd);
        }

        attachHandleDrag(handleT, (dir) => {
            state.recapCropTop = Math.max(0, Math.min(30, (state.recapCropTop || 0) + dir));
            if (sliderCropTop) sliderCropTop.value = state.recapCropTop;
            if (valCropTop) valCropTop.innerText = `${state.recapCropTop}%`;
        });
        attachHandleDrag(handleB, (dir) => {
            state.recapCropBottom = Math.max(0, Math.min(30, (state.recapCropBottom || 0) - dir));
            if (sliderCropBottom) sliderCropBottom.value = state.recapCropBottom;
            if (valCropBottom) valCropBottom.innerText = `${state.recapCropBottom}%`;
        });
        attachHandleDrag(handleL, (dir) => {
            state.recapCropLeft = Math.max(0, Math.min(30, (state.recapCropLeft || 0) + dir));
            if (sliderCropLeft) sliderCropLeft.value = state.recapCropLeft;
            if (valCropLeft) valCropLeft.innerText = `${state.recapCropLeft}%`;
        });
        attachHandleDrag(handleR, (dir) => {
            state.recapCropRight = Math.max(0, Math.min(30, (state.recapCropRight || 0) - dir));
            if (sliderCropRight) sliderCropRight.value = state.recapCropRight;
            if (valCropRight) valCropRight.innerText = `${state.recapCropRight}%`;
        });

        // Touch Pinch-to-Zoom on innerMovie
        let initDistance = 0;
        let initCrop = 0;

        innerMovie.addEventListener("touchstart", (e) => {
            if (e.touches && e.touches.length === 2) {
                const dx = e.touches[0].clientX - e.touches[1].clientX;
                const dy = e.touches[0].clientY - e.touches[1].clientY;
                initDistance = Math.hypot(dx, dy);
                initCrop = (state.recapCropTop || 0);
            }
        }, { passive: true });

        innerMovie.addEventListener("touchmove", (e) => {
            if (e.touches && e.touches.length === 2 && initDistance > 0) {
                e.preventDefault();
                const dx = e.touches[0].clientX - e.touches[1].clientX;
                const dy = e.touches[0].clientY - e.touches[1].clientY;
                const dist = Math.hypot(dx, dy);
                const scaleDiff = (initDistance - dist) * 0.08;
                const newCrop = Math.max(0, Math.min(25, Math.round(initCrop + scaleDiff)));
                state.recapCropTop = newCrop;
                state.recapCropBottom = newCrop;
                if (sliderCropTop) sliderCropTop.value = newCrop;
                if (sliderCropBottom) sliderCropBottom.value = newCrop;
                if (valCropTop) valCropTop.innerText = `${newCrop}%`;
                if (valCropBottom) valCropBottom.innerText = `${newCrop}%`;
                updateCanvasMockupVisuals();
            }
        }, { passive: false });

        innerMovie.addEventListener("touchend", () => { initDistance = 0; });
    }
    setupInteractiveCropHandles();

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

window.toggleAdvancedRecapStudio = function() {
    const body = document.getElementById("recap-advanced-body");
    const chevron = document.getElementById("accordion-chevron");
    if (!body) return;
    const isHidden = body.classList.toggle("hidden");
    if (chevron) chevron.innerText = isHidden ? "▼" : "▲";
    if (!isHidden) {
        updateCanvasMockupVisuals();
    }
};

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
            tier: state.user?.is_vip ? "VIP" : (currentLang === "my" ? "အခမဲ့ (Free)" : "Free"),
            m,
            hint: t(state.user?.is_vip ? "toast_recap_over_hint_vip" : "toast_recap_over_hint_free"),
        }), 'warning', 6000);
        return;
    }

    if (btn) btn.disabled = true;

    const progressBox = document.getElementById("recap-progress-box");
    const resultBox = document.getElementById("recap-result-box");
    const progressFill = document.getElementById("recap-progress-fill");
    const statusMsg = document.getElementById("recap-status-msg");
    const stepText = document.getElementById("recap-step-text");

    if (resultBox) resultBox.classList.add("hidden");
    if (progressBox) {
        progressBox.classList.remove("hidden");
        progressBox.scrollIntoView({ behavior: "smooth" });
    }
    if (progressFill) progressFill.style.width = "10%";
    setRecapStage("1/4", null);
    if (statusMsg) statusMsg.innerText = (state.lang === "my"
        ? "⚡ [1/4] Groq Cloud Whisper Turbo Audio Ingestion စတင်နေပါသည်..."
        : "⚡ [1/4] Groq Cloud Whisper Turbo Audio Ingestion starting...");

    let estMinutes = "၁.၅ - ၂.၅ မိနစ်";
    if (state.recapSourceDuration && state.recapSourceDuration > 180) {
        estMinutes = "၃ - ၄ မိနစ်";
    }
    if (stepText) {
        stepText.innerText = (state.lang === "my"
            ? `⚡ ခန့်မှန်းကြာချိန်: ~${estMinutes} (Groq Turbo Engine + 1080p Cinema Compositing)`
            : `⚡ Estimated time: ~${estMinutes} (Groq Turbo Engine + 1080p Cinema Compositing)`);
    }

    try {
        const sliderBlurY = document.getElementById("slider-blur-y");
        const sliderBlurH = document.getElementById("slider-blur-h");
        const sliderSubY = document.getElementById("slider-sub-y");

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
            zoom_factor: state.recapZoom || 1.06,
            voice_speed: state.recapSpeed || "+12%",
            blur_subtitles: state.recapBlurSubs !== false,
            blur_y: sliderBlurY ? parseInt(sliderBlurY.value) : 85,
            blur_h: sliderBlurH ? parseInt(sliderBlurH.value) : 7,
            burn_subtitles: state.recapBurnSubs !== false,
            sub_y: sliderSubY ? parseInt(sliderSubY.value) : 85,
            sub_style: state.recapSubStyle || "yellow",
            sub_font_size: state.recapFontSize || 16,
            sub_font: state.recapSubFont || "default",
            sub_lines: state.recapLines || 1,
            include_logo: state.recapLogo !== false,
            logo_pos: state.recapLogoPos || "top-right",
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

    } catch (err) {
        showErrorToast(err, 'recap');
        if (progressBox) progressBox.classList.add("hidden");
        if (btn) btn.disabled = false;
    }
}

function startRecapJobPolling(jobId) {
    if (state.recapPollTimer) clearInterval(state.recapPollTimer);

    const progressBox = document.getElementById("recap-progress-box");
    const resultBox = document.getElementById("recap-result-box");
    const progressFill = document.getElementById("recap-progress-fill");
    const statusMsg = document.getElementById("recap-status-msg");
    const stepText = document.getElementById("recap-step-text");
    const btn = document.getElementById("btn-start-recap");

    if (progressBox) progressBox.classList.remove("hidden");
    if (btn) btn.disabled = true;

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
    const RECAP_POLL_MAX_TICKS = 1800; // 60 min at the 2s interval

    function noteRecapPollFailure() {
        recapPollFailures++;
        if (recapPollFailures === 3 && statusMsg) {
            statusMsg.innerText = t("poll_reconnecting");
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

    state.recapPollTimer = setInterval(async () => {
        recapPollTicks++;
        if (recapPollTicks > RECAP_POLL_MAX_TICKS) {
            clearInterval(state.recapPollTimer);
            state.recapPollTimer = null;
            if (statusMsg) statusMsg.innerText = t("poll_background_notice");
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
            if (progressFill && data.progress_percent) {
                progressFill.style.width = `${Math.min(100, Math.max(10, data.progress_percent))}%`;
            }
            if (statusMsg) {
                if (data.progress_stage) {
                    // Round-4 F-C: canonical stage from the server,
                    // rendered in the UI language — the raw engine
                    // labels ("Smart Sync: VoxCPM 2 3/10", "Timestamp
                    // translation") never reach the user any more.
                    const stageText = t("recap_stage_" + data.progress_stage);
                    statusMsg.innerText = data.progress_sub ? `${stageText} ${data.progress_sub}` : stageText;
                } else if (data.progress_label) {
                    statusMsg.innerText = data.progress_label;
                }
            }
            if (stepText && data.progress_step) {
                stepText.innerText = `Step ${data.progress_step} • ${t("queue_notice_recap")}`;
            }
            setRecapStage(data.progress_step, data.status);

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
                    resultBox.scrollIntoView({ behavior: "smooth" });
                }

                const btnTg = document.getElementById("btn-recap-send-telegram");
                if (btnTg) {
                    btnTg.disabled = false;
                    btnTg.classList.remove("sent");
                    btnTg.innerHTML = `<span class="btn-icon">📱</span> <span>${t("btn_send_recap_telegram")}</span>`;
                }

                // Setup video player (round-6 P1: blob URL — a raw
                // API src cannot carry the init-data header and 401s)
                const player = document.getElementById("recap-result-player");
                if (player && data.download_urls?.video) {
                    setAuthMediaSource(player, data.download_urls.video);
                    player.classList.remove("hidden");
                }

                // Setup deliverable buttons
                const btnVideo = document.getElementById("btn-recap-dl-video");
                const btnAudio = document.getElementById("btn-recap-dl-audio");
                const btnSrt = document.getElementById("btn-recap-dl-srt");
                const btnScript = document.getElementById("btn-recap-dl-script");

                if (btnVideo && data.download_urls?.video) {
                    btnVideo.setAttribute("data-dl-url", data.download_urls.video);
                }
                if (btnAudio && data.download_urls?.audio) {
                    btnAudio.setAttribute("data-dl-url", data.download_urls.audio);
                }
                if (btnSrt && data.download_urls?.srt) {
                    btnSrt.setAttribute("data-dl-url", data.download_urls.srt);
                }
                if (btnScript && data.download_urls?.script) {
                    btnScript.setAttribute("data-dl-url", data.download_urls.script);
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
                            if (p.n) a.setAttribute("data-dl-name", `recap_part_${p.n}.mp4`);
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
                        if (data.split_error) note = (t("recap_split_failed") || "Part ခွဲတာ မအောင်မြင်ပါ — အပြည့်အစုံ ဗီဒီယို အပေါ်မှာ ရပါတယ်။");
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
                showStudioToast(mapErrorToToast(errMsg, 'recap'), 'error', 6000);
                if (errMsg.includes("Douyin") || errMsg.includes("Fresh cookies") || blockedLinkInfo(errMsg).blocked) {
                    const btnFileTab = document.getElementById("btn-source-mode-file");
                    if (btnFileTab) btnFileTab.click();
                }
                loadUserProfile();
                loadUserHistory();
            }
        } catch (err) {
            console.warn("Recap polling error:", err);
            noteRecapPollFailure();
        }
    }, 2000);
}

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

function dlRenderDeliveryRow(data) {
    const box = document.getElementById("dl-delivery-actions");
    if (!box) return;
    const rowId = `dl-delivery-${data.format}`;
    const old = document.getElementById(rowId);
    if (old) old.remove();
    const sizeMb = data.size_bytes ? (data.size_bytes / (1024 * 1024)).toFixed(1) : "?";
    const icon = data.format === "video" ? "🎬" : (data.format === "mp3" ? "🎵" : "📝");
    const row = document.createElement("div");
    row.className = "dl-delivery-row";
    row.id = rowId;
    const label = document.createElement("span");
    label.className = "dl-delivery-label";
    label.innerText = `${icon} ${data.filename} · ${sizeMb} MB`;
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
            } catch (err) {
                showErrorToast(err, 'download');
            } finally {
                btnDl.disabled = false;
                if (dlBtnSpan) dlBtnSpan.innerText = dlBtnOriginal;
            }
        });

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
        const res = await fetch(`/api/recap/${jobId}/send-telegram`, {
            method: "POST",
            headers: getAuthHeaders(),
        });
        const data = await res.json();
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

        showStudioToast(t("toast_success_slip"), 'success');
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

const ADM_SECTIONS = ["overview", "payments", "jobs", "users", "broadcast", "activity", "settings"];
let admCurrentSection = "overview";
let admCurrentUserId = null;
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

// ── Overview + Payments ──────────────────────────────────────────────

async function loadAdminData() {
    if (!state.user?.is_admin) return;
    try {
        const resStats = await fetchWithTimeout("/api/admin/overview", { headers: getAuthHeaders() });
        if (resStats.ok) {
            const stats = await resStats.json();
            const set = (id, v) => { const el = document.getElementById(id); if (el) el.innerText = v; };
            set("adm-total-users", stats.total_users ?? 0);
            set("adm-vip-users", stats.vip_users ?? 0);
            set("adm-pending-slips", stats.pending_slips ?? 0);
            set("adm-queued-jobs", stats.queued_jobs ?? 0);
            set("adm-failed-24h", stats.failed_jobs_24h ?? 0);
            set("adm-banned-users", stats.banned_users ?? 0);
            set("adm-new-24h", stats.new_users_24h ?? 0);
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
        }
    } catch (e) {
        console.warn("Error loading admin overview:", e);
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
        badge.className = `history-item-badge ${jobType === 'RECAP' ? 'badge-recap' : 'badge-voice'}`;
        badge.innerText = jobType === 'RECAP' ? '🎬 RECAP' : '🎙️ VOICE';
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
            buttons += dis("↗️ Browser Tab တွင်ဖွင့်မည်");
        } else if (jobType === "RECAP") {
            if (deliverables.video) {
                buttons += `<a href="#" data-dl-url="${deliverables.video}" data-dl-name="recap_video.mp4" class="mini-chip gold">🎬 Download Video</a>`;
            }
            if (deliverables.audio) {
                buttons += `<a href="#" data-dl-url="${deliverables.audio}" data-dl-name="voiceover.mp3" class="mini-chip">🎙️ Download Audio</a>`;
            }
            if (deliverables.srt) {
                buttons += `<a href="#" data-dl-url="${deliverables.srt}" data-dl-name="subtitles.srt" class="mini-chip">📝 Subtitles (SRT)</a>`;
            }
            if (deliverables.script) {
                buttons += `<a href="#" data-dl-url="${deliverables.script}" data-dl-name="script.txt" class="mini-chip">📄 Script (TXT)</a>`;
            }
        } else {
            const dlUrl = deliverables.download || downloadUrl;
            if (dlUrl) {
                buttons += `<a href="#" data-dl-url="${dlUrl}" data-dl-name="voice.wav" class="mini-chip gold">📥 Download WAV / MP3</a>`;
            }
        }
        if (mediaAvailable && downloadUrl) {
            buttons += `<a href="#" data-open-url="${downloadUrl}" class="mini-chip" style="opacity: 0.85;">↗️ Browser Tab တွင်ဖွင့်မည်</a>`;
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
// real retention rule (files are kept on the server for 7 days, then the
// retention sweep removes them) — it must never blame deploys or disk
// wipes: outputs live on the persistent disk and survive restarts.
// Regenerate is the single primary action here.
function mediaGonePanelHtml(jobId) {
    return `
        <div class="admin-media-err-banner mt-3 p-3 text-center" style="border-radius: 12px; border: 1px solid rgba(239, 68, 68, 0.35); background: rgba(239, 68, 68, 0.08);">
            <div style="font-size: 1.6rem; margin-bottom: 4px;">⌛</div>
            <strong style="color: #fca5a5; font-size: 13px;">အသံ/ဗီဒီယို ဖိုင် မတွေ့တော့ပါ (File No Longer Available)</strong>
            <p class="text-xs text-muted mt-1" style="line-height: 1.5;">
                ထုတ်လုပ်ထားသော ဖိုင်များကို ဆာဗာပေါ်တွင် ၇ ရက်အထိ သိမ်းဆည်းပေးထားပြီး၊ သက်တမ်းကျော်လွန်ပါက အလိုအလျောက် ဖျက်သိမ်းပါသည်။ ဤဖိုင်ကို ပြန်လိုချင်ပါက Regenerate နှိပ်ပါ — အလုပ်ကို အသစ် ပြန်လည် ထုတ်လုပ်ပေးပါမည်။
            </p>
            <div class="mt-2" style="display: flex; gap: 8px; justify-content: center;">
                <button class="btn mini-btn secondary-btn" onclick="closeAdminMediaModal(); adminRetryJob('${jobId}');">
                    🔄 ပြန်လည် ထုတ်လုပ်မည် (Regenerate / Retry)
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
        durDiv.innerText = `⏱️ အသံဖိုင် ကြာချိန်: ${m}:${s < 10 ? '0' : ''}${s} (${Math.round(mediaEl.duration)} စက္ကန့်)`;
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
    // Two-line row (full-app audit A-3/A-4/A-6): line 1 is the name
    // (the only part allowed to ellipsize) beside badges that always
    // render in full; line 2 carries @username + the ID/usage sub-line.
    return `<div class="adm-user-row adm-tap" onclick="openAdminUserDetail(${u.id})">
        <div class="adm-avatar">${initial}</div>
        <div class="adm-user-main">
            <div class="adm-user-line1">
                <span class="adm-user-name">${escapeHtml(u.first_name || "-")}</span>
                ${u.account_category === "PURCHASED" ? `<span class="badge-vip">👑 VIP</span>` : ""}
                <span class="badge-status ${u.is_banned ? "blocked" : "active"}">${u.is_banned ? "BLOCKED" : "ACTIVE"}</span>
            </div>
            <div class="adm-user-sub">${u.username ? `@${escapeHtml(u.username)} \u2022 ` : ""}ID: ${u.id} \u2022 ${escapeHtml(u.account_category || "FREE")} \u2022 \u23f1 ${balFmt} \u2022 \ud83c\udfac ${u.recap_credits ?? 0}${u.is_banned ? " \u2022 \ud83d\udeab" : ""}</div>
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
        st.className = "badge-status " + (u.is_banned ? "failed" : "success");
        st.innerText = u.is_banned ? t("admin_status_banned") : t("admin_status_active");
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
    renderAdminBanZone(u);
}

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
            if (tier) payload.account_category = tier;
            const res = await fetch("/api/admin/adjust-user", {
                method: "POST",
                headers: getAuthHeaders(),
                body: JSON.stringify(payload),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || "Failed");
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
                <span class="history-item-badge ${j.job_type === 'RECAP' ? 'badge-recap' : 'badge-voice'}">${j.job_type === 'RECAP' ? '🎬' : '🎙️'}</span>
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
                    ${bd.pre_existing_blocked_in_db ? row(t("admin_bc_preblocked"), bd.pre_existing_blocked_in_db) : ""}
                    ${bd.other_errors ? row(t("admin_bc_other"), bd.other_errors) : ""}
                </div>`;
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
                        </div>
                        <span class="queue-timing">${durationTxt}</span>
                    </div>
                    <div class="queue-item-meta">
                        <span class="queue-user-pill" onclick="admFillUser(${j.user_id})">👤 ${escapeHtml(j.user_name)}${j.username ? ` (@${escapeHtml(j.username)})` : ""} • ID: ${j.user_id}</span>
                        <span>#${j.id.slice(0, 8)}</span>
                    </div>
                    <div class="queue-item-body">${escapeHtml(j.input_snippet || "Task Content")}</div>
                    ${j.error_message ? `<div class="queue-item-error">⚠️ ${escapeHtml(j.error_message)}</div>` : ""}
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
            const isFailed = (j.status === "FAILED");
            const isSuccess = (j.status === "SUCCESS");
            const isRunning = (j.status === "RUNNING" || j.status === "QUEUED");
            const borderCls = isFailed ? 'border-danger-dim' : (isSuccess ? (j.is_expired ? 'border-warning-dim' : 'border-success-dim') : (isRunning ? 'border-running-dim' : ''));
            return `
            <div class="history-item ${borderCls}" id="adm-job-item-${j.id}">
                <div class="history-item-header">
                    <div>
                        <span class="history-item-badge ${j.job_type === 'RECAP' ? 'badge-recap' : 'badge-voice'}">${j.job_type === 'RECAP' ? '🎬 RECAP' : '🎙️ VOICE'}</span>
                        <span class="mini-tag adm-tap" style="margin-left: 4px;" onclick="admFillUser(${j.user_id})">👤 ${escapeHtml(j.username ? '@' + j.username : 'ID: ' + j.user_id)}</span>
                    </div>
                    <div>
                        <span class="history-item-badge status-${j.status.toLowerCase()}">${j.status}</span>
                        ${j.is_expired ? `<span class="badge-expired" style="margin-left: 4px;">⌛ ${t("admin_expired")}</span>` : ''}
                    </div>
                </div>
                <div class="history-item-body">
                    <strong>${escapeHtml(j.preset_voice || j.engine)}:</strong> ${escapeHtml(j.input_snippet || "Task")}
                    ${j.error_message ? `
                        <div class="history-error-snippet text-xs text-danger mt-1 p-2 glass-inset">
                            ⚠️ <strong>Error:</strong> ${escapeHtml(j.error_message.slice(0, 180))}${j.error_message.length > 180 ? '...' : ''}
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
        retryBtn.innerText = "⏳ စတင်နေဆဲ...";
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
                ⏳ <strong>Queue ထဲသို့ ထည့်သွင်းပြီး စတင်လုပ်ဆောင်နေပါပြီ...</strong>
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
    if (activeRetryPollers[jobId]) {
        clearInterval(activeRetryPollers[jobId]);
    }

    let pollCount = 0;
    const maxPolls = 80; // Up to ~3.5 mins

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
            if (!res.ok) return;

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
                    let label = data.progress_label || (data.status === "QUEUED" ? "တန်းစီဇယားထဲတွင် စောင့်ဆိုင်းနေပါသည်..." : "လုပ်ဆောင်နေဆဲ...");
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
                            🎉 <strong>အလုပ်ပြီးမြောက်ပါပြီ! (100%)</strong>
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
                            ⚠️ <strong>Error:</strong> ${escapeHtml(data.error_message || "Execution failed")}
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
    // retention window (settings.media_retention_days, default 7 days),
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
        const elCharLimitFree = document.getElementById("adm-set-char-limit-free");

        const elTutVoiceEn = document.getElementById("adm-tut-voice-enabled");
        const elTutVoiceTitle = document.getElementById("adm-tut-voice-title");
        const elTutVoiceLink = document.getElementById("adm-tut-voice-link");

        const elTutRecapEn = document.getElementById("adm-tut-recap-enabled");
        const elTutRecapTitle = document.getElementById("adm-tut-recap-title");
        const elTutRecapLink = document.getElementById("adm-tut-recap-link");

        if (elKpayNum) elKpayNum.value = s.payment_kpay_number || "";
        if (elKpayName) elKpayName.value = s.payment_kpay_name || "";
        if (elWaveNum) elWaveNum.value = s.payment_wave_number || "";
        if (elWaveName) elWaveName.value = s.payment_wave_name || "";
        if (elTutLink) elTutLink.value = s.tutorial_video_link || "";
        if (elMaint) elMaint.checked = !!s.maintenance_mode;
        if (elCharLimitFree && s.character_limit_free != null) elCharLimitFree.value = s.character_limit_free;

        if (elTutVoiceEn) elTutVoiceEn.checked = String(s.tutorial_voice_enabled).toLowerCase() !== "false" && s.tutorial_voice_enabled !== false;
        if (elTutVoiceTitle) elTutVoiceTitle.value = s.tutorial_voice_title || "Voice Clone ပြုလုပ်နည်း လမ်းညွှန်";
        if (elTutVoiceLink) elTutVoiceLink.value = s.tutorial_voice_link || s.tutorial_video_link || "";

        if (elTutRecapEn) elTutRecapEn.checked = String(s.tutorial_recap_enabled).toLowerCase() !== "false" && s.tutorial_recap_enabled !== false;
        if (elTutRecapTitle) elTutRecapTitle.value = s.tutorial_recap_title || "Movie Recap ပြုလုပ်နည်း လမ်းညွှန်";
        if (elTutRecapLink) elTutRecapLink.value = s.tutorial_recap_link || s.tutorial_video_link || "";

        for (const [elId, settingKey] of ADMIN_TUTORIAL_LINK_FIELDS) {
            const el = document.getElementById(elId);
            if (el) el.value = s[settingKey] || "";
        }

        setSelectOrCustom("adm-gemini-primary", "adm-gemini-primary-custom", s.gemini_primary_model || "gemini-3.6-flash");
        setSelectOrCustom("adm-gemini-fb1", "adm-gemini-fb1-custom", s.gemini_fallback_1 || "gemini-3.5-flash");
        setSelectOrCustom("adm-gemini-fb2", "adm-gemini-fb2-custom", s.gemini_fallback_2 || "gemini-3.5-flash-lite");
        setSelectOrCustom("adm-gemini-fb3", "adm-gemini-fb3-custom", s.gemini_fallback_3 || "gemini-flash-lite-latest");

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
    const charLimitFreeRaw = document.getElementById("adm-set-char-limit-free")?.value?.trim();
    const charLimitFree = charLimitFreeRaw === "" || charLimitFreeRaw == null ? null : parseInt(charLimitFreeRaw, 10);

    const tutVoiceEn = !!document.getElementById("adm-tut-voice-enabled")?.checked;
    const tutVoiceTitle = document.getElementById("adm-tut-voice-title")?.value?.trim() || "Voice Clone ပြုလုပ်နည်း လမ်းညွှန်";
    const tutVoiceLink = document.getElementById("adm-tut-voice-link")?.value?.trim() || "";

    const tutRecapEn = !!document.getElementById("adm-tut-recap-enabled")?.checked;
    const tutRecapTitle = document.getElementById("adm-tut-recap-title")?.value?.trim() || "Movie Recap ပြုလုပ်နည်း လမ်းညွှန်";
    const tutRecapLink = document.getElementById("adm-tut-recap-link")?.value?.trim() || "";

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
        ...tutExtraLinks,
        maintenance_mode: maint,
        ...(Number.isFinite(charLimitFree) ? { character_limit_free: charLimitFree } : {}),
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
    });
}
window.toggleHistoryDetail = toggleHistoryDetail;

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
    if (isRecap && j.parts_count && j.parts_count > 1) {
        inner += `<div class="history-item-body">${escapeHtml(tf("recap_segmented_cost", { parts: j.parts_count, credits: (j.credits_charged ?? j.parts_count) }))}</div>`;
    }
    let actions = "";
    if (j.status === "FAILED") {
        actions = `<span class="badge-expired text-xs" style="color:#f87171;">${isRecap ? t("hist_failed_recap") : t("hist_failed")}</span>`;
    } else if (j.status === "RUNNING" || j.status === "QUEUED") {
        actions = isRecap
            ? `<button type="button" onclick="startRecapJobPolling('${j.id}')" class="history-dl-link">${t("hist_track_recap")}</button>`
            : `<button type="button" onclick="startJobPolling('${j.id}')" class="history-dl-link">${t("hist_track_voice")}</button>`;
    } else if (j.is_expired) {
        actions = `<span class="badge-expired text-xs" title="">${t("hist_expired")}</span>`;
    } else if (isRecap) {
        if (j.deliverables?.video) {
            actions += `<button type="button" onclick="openRecapInPlayer('${j.id}')" class="history-dl-link">${t("hist_watch")}</button> <a href="#" data-dl-url="${j.deliverables.video}" data-dl-name="recap_${j.id.slice(0,8)}.mp4" class="history-dl-link">🎬 Video</a> <button type="button" onclick="sendRecapToTelegram('${j.id}')" class="history-dl-link tg-btn">${t("hist_send_telegram")}</button>`;
        }
        if (j.deliverables?.audio) actions += ` <a href="#" data-dl-url="${j.deliverables.audio}" data-dl-name="audio_${j.id.slice(0,8)}.wav" class="history-dl-link">🎙️ Audio</a>`;
        if (j.deliverables?.srt) actions += ` <a href="#" data-dl-url="${j.deliverables.srt}" data-dl-name="sub_${j.id.slice(0,8)}.srt" class="history-dl-link">📝 SRT</a>`;
        if (j.deliverables?.script) actions += ` <a href="#" data-dl-url="${j.deliverables.script}" data-dl-name="script_${j.id.slice(0,8)}.txt" class="history-dl-link">📜 TXT</a>`;
    } else if (j.download_url) {
        actions = `<a href="#" data-dl-url="${j.download_url}" data-dl-name="voice_${j.id.slice(0,8)}.wav" class="history-dl-link">📥 Download</a> <button type="button" onclick="sendVoiceToTelegram('${j.id}')" class="history-dl-link tg-btn">${t("hist_send_telegram")}</button>`;
    }
    if (actions) inner += `<div class="history-btn-row hub-detail-actions">${actions}</div>`;
    return inner;
}

function hubHistoryRowHtml(j) {
    const isRecap = j.job_type === "RECAP";
    const open = hubOpenDetailId === j.id;
    const snippet = j.input_snippet || (isRecap ? "Movie Recap" : "Voice Synthesis");
    const title = isRecap ? escapeHtml(snippet) : `&quot;${escapeHtml(snippet)}&quot;`;
    return `
    <div class="hub-row ${hubStatusBorder(j.status)} ${open ? "open" : ""}" data-job-id="${j.id}">
        <button type="button" class="hub-row-main" onclick="toggleHistoryDetail('${j.id}')" aria-expanded="${open}">
            <span class="hub-type-icon ${isRecap ? "recap" : "voice"}">${isRecap ? "🎬" : "🎙️"}</span>
            <span class="hub-row-text">
                <span class="hub-row-title">${title}</span>
                <span class="hub-row-meta">${isRecap ? "🎬 RECAP • " : ""}${escapeHtml(j.preset_voice || "")} · ⏱️ ${escapeHtml(j.time_ago || "")}</span>
            </span>
            ${j.fallback_used ? `<span class="history-item-badge hub-fallback-badge">${t("hist_fallback_badge")}</span>` : ""}
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
                setAuthMediaSource(player, data.download_urls.video).then((ok) => {
                    if (ok) player.play().catch(() => {});
                });
                player.classList.remove("hidden");
            }
            if (btnVideo && data.download_urls?.video) {
                btnVideo.setAttribute("data-dl-url", data.download_urls.video);
            }
            if (btnAudio && data.download_urls?.audio) {
                btnAudio.setAttribute("data-dl-url", data.download_urls.audio);
            }
            if (btnSrt && data.download_urls?.srt) {
                btnSrt.setAttribute("data-dl-url", data.download_urls.srt);
            }
            if (btnScript && data.download_urls?.script) {
                btnScript.setAttribute("data-dl-url", data.download_urls.script);
            }
            if (btnTg) {
                btnTg.disabled = false;
                btnTg.classList.remove("sent");
                btnTg.innerHTML = `<span class="btn-icon">📱</span> <span>${t("btn_send_recap_telegram")}</span>`;
            }
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
                        btnDl.setAttribute("data-dl-name", "voice_clone.wav");
                    }
                    if (btnTg) {
                        btnTg.disabled = false;
                        btnTg.classList.remove("sent");
                        btnTg.innerHTML = `<span class="btn-icon">📱</span> <span>${t("btn_send_voice_telegram")}</span>`;
                    }
                    showFloatingAlert(
                        t("alert_voice_ready") || "🎉 AI Voice အဆင်သင့်ဖြစ်ပါပြီ!",
                        currentLang === "my" ? "အသံဖိုင်ကို တိုက်ရိုက်ဖွင့်နားထောင်နိုင်ပါပြီ။" : "Your generated voice is ready to play.",
                        () => {
                            if (resultBox) resultBox.scrollIntoView({ behavior: "smooth" });
                        },
                        currentLang === "my" ? "🎧 နားထောင်မည်" : "🎧 Listen"
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
                        setAuthMediaSource(player, data.download_urls.video);
                        player.classList.remove("hidden");
                    }
                    if (btnVideo && data.download_urls?.video) {
                        btnVideo.setAttribute("data-dl-url", data.download_urls.video);
                    }
                    if (btnAudio && data.download_urls?.audio) {
                        btnAudio.setAttribute("data-dl-url", data.download_urls.audio);
                    }
                    if (btnSrt && data.download_urls?.srt) {
                        btnSrt.setAttribute("data-dl-url", data.download_urls.srt);
                    }
                    if (btnScript && data.download_urls?.script) {
                        btnScript.setAttribute("data-dl-url", data.download_urls.script);
                    }
                    if (btnTg) {
                        btnTg.disabled = false;
                        btnTg.classList.remove("sent");
                        btnTg.innerHTML = `<span class="btn-icon">📱</span> <span>${t("btn_send_recap_telegram")}</span>`;
                    }
                    // Cold-start restore: this alert is the ONLY
                    // completion signal here (the user may be on any
                    // tab when a previous session's job is restored),
                    // so it stays — unlike the live poller's duplicate
                    // (round-4 F-E), where the user is already watching
                    // the result card appear.
                    showFloatingAlert(
                        t("alert_recap_ready") || "🎉 Pro Movie Recap အဆင်သင့်ဖြစ်ပါပြီ!",
                        currentLang === "my" ? "Full HD Video ကို တိုက်ရိုက်ဖွင့်ကြည့်နိုင်ပါပြီ။" : "Your recap video is ready to watch.",
                        () => {
                            switchTab("tab-recap");
                            if (resultBox) resultBox.scrollIntoView({ behavior: "smooth" });
                        },
                        currentLang === "my" ? "🎬 ကြည့်ရှုမည်" : "🎬 View Video"
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

function setupDeliverablesDownloads() {
    const btnVideo = document.getElementById("btn-recap-dl-video");
    const btnAudio = document.getElementById("btn-recap-dl-audio");
    const btnSrt = document.getElementById("btn-recap-dl-srt");
    const btnScript = document.getElementById("btn-recap-dl-script");

    if (btnVideo) {
        btnVideo.addEventListener("click", (e) => {
            const url = btnVideo.getAttribute("data-dl-url") || btnVideo.href;
            if (url && url !== "#") {
                e.preventDefault();
                downloadDeliverable(url, "recap_video.mp4");
            }
        });
    }
    if (btnAudio) {
        btnAudio.addEventListener("click", (e) => {
            const url = btnAudio.getAttribute("data-dl-url") || btnAudio.href;
            if (url && url !== "#") {
                e.preventDefault();
                downloadDeliverable(url, "voiceover.mp3");
            }
        });
    }
    if (btnSrt) {
        btnSrt.addEventListener("click", (e) => {
            const url = btnSrt.getAttribute("data-dl-url") || btnSrt.href;
            if (url && url !== "#") {
                e.preventDefault();
                downloadDeliverable(url, "subtitles.srt");
            }
        });
    }
    if (btnScript) {
        btnScript.addEventListener("click", (e) => {
            const url = btnScript.getAttribute("data-dl-url") || btnScript.href;
            if (url && url !== "#") {
                e.preventDefault();
                downloadDeliverable(url, "script.txt");
            }
        });
    }
}

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
            if (!isPaused) nextSlide();
        }, 3500); // 3.5 seconds auto rotation
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
    setupVoiceStudio();
    setupRecapVisualEditor();
    setupAlertCarousel();
    setupDeliverablesDownloads();
    setupDownloader();
    setupSlipPreview();
    setupStickyCtas();
    setupSamplePlayers();

    loadUserProfile();
    loadCuratedVoices();
    loadLegacyModels();
    loadUserHistory();
    checkStartupDeepLinks();
});

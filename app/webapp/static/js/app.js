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
        <span class="toast-msg">${message}</span>
        <button class="toast-close" onclick="this.parentNode.remove()" aria-label="Close">✕</button>
    `;

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
            <div class="studio-confirm-msg">${message}</div>
            <div class="studio-confirm-actions">
                <button id="studio-confirm-cancel" class="btn secondary-btn">မဟုတ်ဘူး / Cancel</button>
                <button id="studio-confirm-ok" class="btn gold-gradient-btn">သဘောတူ / OK</button>
            </div>
        </div>
    `;

    document.body.appendChild(modal);

    const remove = () => modal.remove();
    document.getElementById('studio-confirm-ok').onclick = () => { remove(); if (onConfirm) onConfirm(); };
    document.getElementById('studio-confirm-cancel').onclick = () => { remove(); if (onCancel) onCancel(); };
    modal.addEventListener('click', (e) => { if (e.target === modal) { remove(); if (onCancel) onCancel(); } });
}

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
    try {
        if (state.tg?.HapticFeedback) {
            state.tg.HapticFeedback.impactOccurred(type);
        }
    } catch (e) {}
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
function isAuthFailure(res) {
    if (res && res.status === 401) { showOpenViaTelegramModal(); return true; }
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

// ── Tutorial Video Link Controller (Voice Clone & Movie Recap) ───────────────
let tutorialConfig = {
    fallbackUrl: "https://t.me/VoiceClonePro/16",
    voice: {
        title: "Voice Clone ပြုလုပ်နည်း လမ်းညွှန်",
        url: "https://t.me/VoiceClonePro/16",
        enabled: true
    },
    recap: {
        title: "Movie Recap ပြုလုပ်နည်း လမ်းညွှန်",
        url: "https://t.me/VoiceClonePro/16",
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

    // Apply visibility & title to Voice banner in Tab 1
    const voiceBanner = document.getElementById("voice-tutorial-banner");
    const voiceTitle = document.getElementById("voice-tut-title");
    if (voiceBanner) {
        if (tutorialConfig.voice.enabled) {
            voiceBanner.classList.remove("hidden");
            if (voiceTitle && tutorialConfig.voice.title) voiceTitle.textContent = tutorialConfig.voice.title;
        } else {
            voiceBanner.classList.add("hidden");
        }
    }

    // Apply visibility & title to Recap banner in Tab 2
    const recapBanner = document.getElementById("recap-tutorial-banner");
    const recapTitle = document.getElementById("recap-tut-title");
    if (recapBanner) {
        if (tutorialConfig.recap.enabled) {
            recapBanner.classList.remove("hidden");
            if (recapTitle && tutorialConfig.recap.title) recapTitle.textContent = tutorialConfig.recap.title;
        } else {
            recapBanner.classList.add("hidden");
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

function openTutorialVideo(type = "auto") {
    haptic("light");
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

    const url = target.url || tutorialConfig.fallbackUrl || "https://t.me/VoiceClonePro/16";
    if (window.Telegram?.WebApp?.openLink) {
        window.Telegram.WebApp.openLink(url);
    } else {
        window.open(url, "_blank");
    }
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
    }
    document.querySelectorAll(".tab-pane").forEach(pane => {
        pane.classList.remove("active");
    });
    const activePane = document.getElementById(tabId);
    if (activePane) activePane.classList.add("active");
}
window.switchTab = switchTab;

// ── Language Toggle (i18n) ─────────────────────────────────────────────────
function toggleLanguage() {
    haptic("medium");
    const nextLang = currentLang === "my" ? "en" : "my";
    setLanguage(nextLang);
    loadCuratedVoices();
    loadUserProfile();
    updateRecapSplitCostHint();
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
        const res = await fetch("/api/user/me", { headers: getAuthHeaders() });
        if (isAuthFailure(res)) return;
        if (!res.ok) return;
        const data = await res.json();
        state.user = data;
        // Admins are never entry-gate locked (see entry-gate section).
        if (data.is_admin) unlockEntryGate();
        updateRecapSplitCostHint();

        if (data) {
            updateTutorialUI(data);
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
            charCount.innerText = `0 / ${limit} chars`;
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
        });

        grid.appendChild(card);
    });

    const countBadge = document.getElementById("personas-count");
    if (countBadge) {
        countBadge.innerText = `${filtered.length} Personas`;
    }
}

async function loadCuratedVoices() {
    try {
        const res = await fetch("/api/voices/curated", { headers: getAuthHeaders() });
        if (!res.ok) return;
        const voices = await res.json();
        state.curatedVoices = voices;
        renderCuratedVoices();
    } catch (e) {
        console.warn("Could not load curated voices:", e);
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

function stopCurrentVoicePreview() {
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

function playVoicePreview(voiceId, buttonEl) {
    haptic("light");
    if (!voiceId) return;

    if (currentVoiceAudio && currentPreviewButton === buttonEl) {
        stopCurrentVoicePreview();
        return;
    }

    stopCurrentVoicePreview();

    const audioUrl = `/api/voices/sample/${encodeURIComponent(voiceId)}`;
    const audio = new Audio(audioUrl);
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
    const sel = document.getElementById("recap-voice-select");
    const btn = document.getElementById("btn-preview-recap-voice");
    const voiceId = sel ? sel.value : "komin";
    playVoicePreview(voiceId, btn);
}

// ── Voice Studio Controls & Sub-Modes ──────────────────────────────────────
function switchVoxCPMSubMode(mode) {
    haptic("selection");
    state.voxcpmSubMode = mode;
    const btnCurated = document.getElementById("btn-submode-curated");
    const btnCustom = document.getElementById("btn-submode-custom");
    const curatedCard = document.getElementById("curated-personas-card");
    const refSection = document.getElementById("ref-audio-section");

    if (mode === "curated") {
        if (btnCurated) btnCurated.classList.add("active");
        if (btnCustom) btnCustom.classList.remove("active");
        if (curatedCard) curatedCard.classList.remove("hidden");
        if (refSection) refSection.classList.add("hidden");
    } else {
        if (btnCustom) btnCustom.classList.add("active");
        if (btnCurated) btnCurated.classList.remove("active");
        if (curatedCard) curatedCard.classList.add("hidden");
        if (refSection) refSection.classList.remove("hidden");
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
            charCount.innerText = `${len} / ${max} chars`;
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
        const res = await fetch("/api/user/verify-membership", { method: "POST", headers: getAuthHeaders() });
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
    if (mode === "not-configured") {
        if (title) { title.setAttribute("data-i18n", "gate_not_configured_title"); title.innerText = t("gate_not_configured_title"); }
        if (desc) { desc.setAttribute("data-i18n", "gate_not_configured_desc"); desc.innerText = t("gate_not_configured_desc"); }
        if (chips) chips.classList.add("hidden");
        if (chLink) chLink.classList.add("hidden");
        if (grLink) grLink.classList.add("hidden");
        return;
    }
    if (title) { title.setAttribute("data-i18n", "gate_lock_title"); title.innerText = t("gate_lock_title"); }
    if (desc) { desc.setAttribute("data-i18n", "gate_lock_desc"); desc.innerText = t("gate_lock_desc"); }
    if (chips) chips.classList.remove("hidden");
    if (chLink) { chLink.classList.remove("hidden"); if (data && data.channel_invite_link) chLink.href = data.channel_invite_link; }
    if (grLink) { grLink.classList.remove("hidden"); if (data && data.group_invite_link) grLink.href = data.group_invite_link; }
}

function applyEntryGateVerdict(data, reason) {
    if (!data) return;
    const status = document.getElementById("gate-lock-status");
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
        const res = await fetch("/api/user/verify-membership", { method: "POST", headers: getAuthHeaders() });
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
            res = await fetch("/api/voices/generate-with-audio", {
                method: "POST",
                headers: headers,
                body: formData,
            });
        } else {
            res = await fetch("/api/voices/generate", {
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
            });
        }

        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.detail || "Voice generation request failed");
        }

        state.activeJobId = data.job_id;
        if (statusMsg) statusMsg.innerText = t("preparing_msg");
        if (progressFill) progressFill.style.width = "30%";
        if (etaText) etaText.innerText = t("calculating_eta");

        startJobPolling(data.job_id);

    } catch (err) {
        showStudioToast(err.message, 'info');
        if (progressBox) progressBox.classList.add("hidden");
        if (btnGen) btnGen.disabled = false;
    }
}

function startJobPolling(jobId) {
    if (state.pollTimer) clearInterval(state.pollTimer);

    let tick = 0;
    state.pollTimer = setInterval(async () => {
        tick++;
        try {
            const res = await fetch(`/api/jobs/${jobId}/status`, { headers: getAuthHeaders() });
            if (!res.ok) return;
            const data = await res.json();

            const statusMsg = document.getElementById("voice-status-msg");
            const progressFill = document.getElementById("voice-progress-fill");
            const etaText = document.getElementById("voice-eta-text");
            const progressBox = document.getElementById("voice-progress-box");
            const resultBox = document.getElementById("voice-result-box");
            const audioPlayer = document.getElementById("result-audio-player");
            const btnDl = document.getElementById("btn-download-audio");
            const btnGen = document.getElementById("btn-generate-voice");

            if (data.is_waiting_laptop_worker) {
                if (statusMsg) statusMsg.innerText = t("voxcpm_status_waiting");
                if (etaText) etaText.innerText = t("worker_auto_complete");
                if (progressFill) progressFill.style.width = "35%";
            } else if (data.status === "QUEUED") {
                if (progressFill) progressFill.style.width = "45%";
                if (statusMsg) statusMsg.innerText = t("cloud_queue_waiting");
            } else if (data.status === "RUNNING") {
                if (progressFill) progressFill.style.width = "80%";
                if (statusMsg) statusMsg.innerText = "⚡ အသံထွက်ပေါ်အောင် ပေါင်းစပ်နေပါသည်...";
            } else if (data.status === "SUCCESS") {
                clearInterval(state.pollTimer);
                haptic("heavy");
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
                    audioPlayer.src = data.output_audio_url;
                    audioPlayer.load();
                    audioPlayer.play().catch(() => {});
                }
                if (btnDl && data.output_audio_url) {
                    btnDl.href = data.output_audio_url;
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
                showStudioToast("Generation failed: " + (data.error_message || "Unknown error"), 'error');
                if (progressBox) progressBox.classList.add("hidden");
                if (btnGen) btnGen.disabled = false;
            }

            if (tick > 120) { // 4 minutes timeout
                clearInterval(state.pollTimer);
                if (btnGen) btnGen.disabled = false;
            }

        } catch (e) {
            console.error(e);
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
    showStudioToast(currentLang === "my"
        ? "✂️ ဗီဒီယိုက ၁၀ မိနစ်ထက် ရှည်လို့ အပိုင်းခွဲပြီး လုပ်ပေးပါမယ် — 'အပိုင်းခွဲ ထုတ်မယ်' ကို အလိုအလျောက် ဖွင့်ပေးထားပါတယ်။ ပြီးသွားရင် အပိုင်းအားလုံး ရပါမယ်။"
        : "✂️ This video is longer than 10 minutes, so it will be processed in parts — split output was enabled automatically, and you will receive every part.", 'info');
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
        if (titleSpan) titleSpan.innerText = file.name;
        if (platformSpan) platformSpan.innerText = "Direct Upload (MP4)";

        // Upload asynchronously
        const progressWrap = document.getElementById("recap-upload-progress");
        const progressBar = document.getElementById("recap-upload-bar");
        const progressLabel = document.getElementById("recap-upload-label");
        if (progressWrap) progressWrap.classList.remove("hidden");
        if (progressBar) progressBar.style.width = "40%";
        if (progressLabel) progressLabel.innerText = "Uploading to Cloud Studio... 40%";

        try {
            const formData = new FormData();
            formData.append("video_file", file);
            const res = await fetch("/api/recap/upload_video", {
                method: "POST",
                headers: getAuthHeaders(false),
                body: formData
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || "Upload failed");

            state.uploadedVideoPath = data.local_path;
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
                    showStudioToast(currentLang === "my"
                        ? `⚠️ သတိပေးချက်: တင်သွင်းထားသော ဗီဒီယိုဖိုင် ကြာချိန် (${durStr}) သည် ${tierLabel} ကန့်သတ်ချက် (အများဆုံး ${maxM} မိနစ်) ထက် ကျော်လွန်နေပါသည်ခင်ဗျာ။\n\nကျေးဇူးပြု၍ မိနစ်ပိုတိုသော ဗီဒီယိုကို ရွေးချယ်ပါ သို့မဟုတ် ၁၀ မိနစ်အထိ ပြုလုပ်နိုင်သော VIP သို့ အဆင့်မြှင့်တင်ပါရန်။`
                        : `⚠️ Warning: Video duration (${durStr}) exceeds ${tierLabel} limit (${maxM}m). Please select a shorter video or upgrade to VIP.`, 'warning');
                } else {
                    durSpan.className = "mini-tag";
                    durSpan.innerHTML = `⏱️ ${data.estimated_duration} <span style="color:#10b981; font-weight:700; margin-left:4px;">(Ready)</span>`;
                    if (btnStartRecap) {
                        btnStartRecap.disabled = false;
                        btnStartRecap.style.opacity = "";
                    }
                    if (splitAutoEnabled) notifyRecapSplitAutoEnabled();
                }
            }

            if (progressBar) progressBar.style.width = "100%";
            if (progressLabel) progressLabel.innerText = "✅ Video Ready for 1080p Recap!";
            setRecapPreviewState("ready");
            setTimeout(() => {
                if (progressWrap) progressWrap.classList.add("hidden");
            }, 1500);

            haptic("success");
        } catch (err) {
            showStudioToast("Video upload error: " + err.message, 'error');
            if (progressWrap) progressWrap.classList.add("hidden");
            // Upload failed: drop the local preview and fall back to the
            // empty state (unless an earlier uploaded source is usable).
            if (!state.uploadedVideoPath) {
                if (canvasVideo) {
                    try { canvasVideo.pause(); } catch (e) { /* noop */ }
                    canvasVideo.removeAttribute("src");
                    canvasVideo.load();
                    canvasVideo.classList.add("hidden");
                }
                if (recapInfoBox) recapInfoBox.classList.add("hidden");
            }
            setRecapPreviewState(recapHasSource() ? "ready" : "empty");
        }
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
                handleVideoFileSelect(e.target.files[0]);
            }
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
                showStudioToast(currentLang === "my"
                    ? "💡 တိုက်ရိုက်ဗီဒီယို (Live Video Scrubbing) ကို Frame အလိုက် ကြည့်ရှုလိုပါက အဆင့် (၁) ရှိ '📁 Video Upload' ခလုတ်ဖြင့် MP4 ဗီဒီယိုကို တိုက်ရိုက်တင်သွင်း၍ အသုံးပြုနိုင်ပါသည်ခင်ဗျာ။"
                    : "💡 Live real-time video scrubbing is active when you upload an MP4 video file.", 'info');
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

            // Cropped source aspect ratio matching FFmpeg's crop=c_sw:c_sh:c_x:c_y
            const rCropped = rSource * (keepW / keepH);
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
                innerMovie.insertBefore(thumbImg, innerMovie.firstChild);
            }

            const backdropEl = document.getElementById("video-backdrop-blur");
            if (canvasVideo && !canvasVideo.classList.contains("hidden") && canvasVideo.src) {
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
            } else if (state.recapThumbnailUrl) {
                if (backdropEl) backdropEl.classList.remove("on");
                canvas.style.backgroundImage = `url('${state.recapThumbnailUrl}')`;
                canvas.style.backgroundSize = "cover";
                canvas.style.backgroundPosition = "center";
                innerMovie.style.backgroundImage = "none";
                if (thumbImg) {
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
                if (fillBadge) fillBadge.style.display = "";
            }
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
        });
    });
    const geminiVoiceSel = document.getElementById("recap-gemini-voice-select");
    if (geminiVoiceSel) {
        geminiVoiceSel.addEventListener("change", () => {
            state.recapGeminiVoice = geminiVoiceSel.value || "";
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
            previewText.innerHTML = '"အခုတော့ အစီအစဉ် စတင်ပါပြီ..."';
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
            previewText.innerHTML = '"အခုတော့ ကျွန်တော်တို့ရဲ့<br>အစီအစဉ် စတင်ပါပြီ..."';
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
                const res = await fetch("/api/downloader/extract", {
                    method: "POST",
                    headers: getAuthHeaders(),
                    body: JSON.stringify({ url: url }),
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail || "Analysis failed");

                if (recapInfoBox) recapInfoBox.classList.remove("hidden");
                const thumbImg = document.getElementById("recap-info-thumb");
                const titleSpan = document.getElementById("recap-info-title");
                const durSpan = document.getElementById("recap-info-dur");
                const platformSpan = document.getElementById("recap-info-platform");
                const subSpan = document.getElementById("recap-info-sub");

                if (thumbImg && data.thumbnail) thumbImg.src = data.thumbnail;
                if (titleSpan) titleSpan.innerText = data.title || "Video";
                state.recapVideoTitle = data.title || "Movie Recap Video";

                state.recapSourceDuration = data.duration_seconds || 0;
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
                if (durSpan) {
                    if (isOverLimit) {
                        durSpan.className = "mini-tag text-danger";
                        const maxM = Math.floor(maxAllowed / 60);
                        const tierLabel = state.user?.is_vip ? "VIP" : "အခမဲ့ (Free)";
                        const durM = Math.floor(data.duration_seconds / 60);
                        const durS = Math.floor(data.duration_seconds % 60);
                        const durStr = `${durM}:${durS < 10 ? '0' : ''}${durS}`;
                        const msg = (currentLang === "my")
                            ? (state.user?.is_vip ? `အများဆုံး ${maxM} မိနစ်ထက် ကျော်လွန်နေပါသည်` : `အများဆုံး ၂ မိနစ်ထက် ကျော်လွန်နေပါသည် (VIP ရယူပါ)`)
                            : (state.user?.is_vip ? `Exceeds max ${maxM}m` : `Exceeds Free 2m limit (Upgrade to VIP)`);
                        durSpan.innerHTML = `⏱️ ${data.estimated_duration || "00:00"} <span style="color:#ef4444; font-weight:700; margin-left:4px;">(${msg})</span>`;
                        if (btnStartRecap) {
                            btnStartRecap.disabled = true;
                            btnStartRecap.style.opacity = "0.5";
                        }
                        showStudioToast(currentLang === "my"
                            ? `⚠️ သတိပေးချက်: သင်ထည့်သွင်းထားသော ဗီဒီယို ကြာချိန် (${durStr}) သည် ${tierLabel} ကန့်သတ်ချက် (အများဆုံး ${maxM} မိနစ်) ထက် ကျော်လွန်နေပါသည်ခင်ဗျာ။\n\nကျေးဇူးပြု၍ မိနစ်ပိုတိုသော ဗီဒီယိုကို ရွေးချယ်ပါ သို့မဟုတ် ၁၀ မိနစ်အထိ ပြုလုပ်နိုင်သော VIP သို့ အဆင့်မြှင့်တင်ပါရန်။`
                            : `⚠️ Warning: Video duration (${durStr}) exceeds your ${tierLabel} limit (${maxM}m). Please select a shorter video or upgrade to VIP.`, 'warning');
                    } else {
                        durSpan.className = "mini-tag";
                        durSpan.innerHTML = `⏱️ ${data.estimated_duration || "00:00"} <span style="color:#10b981; font-weight:700; margin-left:4px;">(${t("recap_duration_valid") || "Ready"})</span>`;
                        if (btnStartRecap) {
                            btnStartRecap.disabled = false;
                            btnStartRecap.style.opacity = "";
                        }
                        if (splitAutoEnabled) notifyRecapSplitAutoEnabled();
                    }
                }

                if (platformSpan) platformSpan.innerText = data.platform || "Video";
                if (subSpan) subSpan.innerText = data.has_subtitles ? "✅ Subtitle Ready" : "⚠️ AI Auto Sub";

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
                    showStudioToast(`⚠️ Douyin Anti-Bot အကာအကွယ်ကြောင့် လင့်ခ်ဖြင့် တိုက်ရိုက်စစ်ဆေးမရပါခင်ဗျာ။\n\nဖုန်းထဲရှိ ဒေါင်းလုဒ်ဆွဲထားသော Douyin ဗီဒီယိုဖိုင်ကို Step 1 ရှိ '📁 Upload MP4' ဖြင့် စက္ကန့်ပိုင်းအတွင်း တိုက်ရိုက်တင်သွင်း၍ အလွယ်တကူ ဆက်လက်အသုံးပြုနိုင်ပါသည်ခင်ဗျာ။`, 'warning');
                    const btnFileTab = document.getElementById("btn-source-mode-file");
                    if (btnFileTab) btnFileTab.click();
                } else {
                    showStudioToast(err.message || "Failed to inspect video link", 'error');
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

    // Wire up Voice Cards in Step 2
    const voiceCards = document.querySelectorAll("#recap-top-voices .voice-card");
    const voiceSelect = document.getElementById("recap-voice-select");
    voiceCards.forEach(card => {
        card.addEventListener("click", () => {
            haptic("selection");
            const voice = card.getAttribute("data-voice");
            voiceCards.forEach(c => c.classList.remove("active"));
            card.classList.add("active");
            state.recapVoice = voice;
            if (voiceSelect) voiceSelect.value = voice;
        });
    });

    if (voiceSelect) {
        voiceSelect.addEventListener("change", (e) => {
            const v = e.target.value;
            state.recapVoice = v;
            voiceCards.forEach(c => {
                if (c.getAttribute("data-voice") === v) c.classList.add("active");
                else c.classList.remove("active");
            });
        });
    }

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
        txt.innerText = isHidden ? "အခြားအသံများ ရွေးချယ်ရန် (Show all 12 voices) ▼" : "အသံစာရင်း ပိတ်မည် (Hide) ▲";
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

async function handleStartRecap() {
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
        showStudioToast(currentLang === "my"
            ? `ဗီဒီယို ကြာချိန်သည် ${state.user?.is_vip ? "VIP" : "အခမဲ့ (Free)"} အသုံးပြုသူ ကန့်သတ်ချက် (အများဆုံး ${m} မိနစ်) ထက် ကျော်လွန်နေပါသည်။ ` + (state.user?.is_vip ? "✂️ ၁၀ မိနစ်ထက် ရှည်လျှင် 'အပိုင်းခွဲ ထုတ်မယ်' ကို ဖွင့်ပြီး အပိုင်းလိုက် ထုတ်နိုင်ပါသည်ခင်ဗျာ။" : "ပိုရှည်သော ဗီဒီယိုများ (၁၀ မိနစ်အထိ) ပြုလုပ်လိုပါက VIP သို့ အဆင့်မြှင့်တင်ပါခင်ဗျာ။")
            : `Video duration exceeds your ${state.user?.is_vip ? "VIP" : "Free"} limit of ${m} minutes.` + (state.user?.is_vip ? " Enable split output to receive longer videos as parts." : " Upgrade to VIP for up to 10 minutes."), 'warning');
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
        showStudioToast(err.message, 'info');
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

    state.recapPollTimer = setInterval(async () => {
        try {
            const res = await fetch(`/api/recap/${jobId}/status`, {
                headers: getAuthHeaders(),
            });
            if (!res.ok) return;

            const data = await res.json();
            if (progressFill && data.progress_percent) {
                progressFill.style.width = `${Math.min(100, Math.max(10, data.progress_percent))}%`;
            }
            if (statusMsg && data.progress_label) {
                statusMsg.innerText = data.progress_label;
            }
            if (stepText && data.progress_step) {
                stepText.innerText = `Step ${data.progress_step} • ${t("queue_notice_recap")}`;
            }

            if (data.status === "SCRIPT_READY") {
                clearInterval(state.recapPollTimer);
                state.recapPollTimer = null;
                if (btn) btn.disabled = false;
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

                // Setup video player
                const player = document.getElementById("recap-result-player");
                if (player && data.download_urls?.video) {
                    player.src = data.download_urls.video;
                    player.classList.remove("hidden");
                }

                // Setup deliverable buttons
                const btnVideo = document.getElementById("btn-recap-dl-video");
                const btnAudio = document.getElementById("btn-recap-dl-audio");
                const btnSrt = document.getElementById("btn-recap-dl-srt");
                const btnScript = document.getElementById("btn-recap-dl-script");

                if (btnVideo && data.download_urls?.video) {
                    btnVideo.href = data.download_urls.video;
                    btnVideo.setAttribute("data-dl-url", data.download_urls.video);
                }
                if (btnAudio && data.download_urls?.audio) {
                    btnAudio.href = data.download_urls.audio;
                    btnAudio.setAttribute("data-dl-url", data.download_urls.audio);
                }
                if (btnSrt && data.download_urls?.srt) {
                    btnSrt.href = data.download_urls.srt;
                    btnSrt.setAttribute("data-dl-url", data.download_urls.srt);
                }
                if (btnScript && data.download_urls?.script) {
                    btnScript.href = data.download_urls.script;
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
                            a.href = p.url;
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

                showFloatingAlert(
                    t("alert_recap_ready") || "🎉 Pro Movie Recap ဖန်တီးမှု ပြီးစီးပါပြီ!",
                    currentLang === "my" ? "Full HD Video ပြင်ဆင်ပြီးစီးပါပြီ။ ကြည့်ရှုနိုင်ပါပြီ။" : "Your Movie Recap video is ready to watch or download.",
                    () => {
                        if (resultBox) resultBox.scrollIntoView({ behavior: "smooth" });
                    },
                    currentLang === "my" ? "🎬 ကြည့်ရှုမည်" : "🎬 View Video"
                );
            } else if (data.status === "FAILED") {
                clearInterval(state.recapPollTimer);
                state.recapPollTimer = null;
                try { localStorage.removeItem("vcp_active_recap_job_id"); } catch (e) {}
                if (btn) btn.disabled = false;
                if (progressBox) progressBox.classList.add("hidden");

                const errMsg = data.error_message || "Processing error";
                if (errMsg.includes("Douyin") || errMsg.includes("Fresh cookies")) {
                    showStudioToast(`⚠️ Douyin Anti-Bot အကာအကွယ်ကြောင့် တိုက်ရိုက်ဒေါင်းလုဒ် မအောင်မြင်ပါခင်ဗျာ။\n\nCredit ပြန်လည်ဖြည့်သွင်းပေးထားပါသည်။ ဖုန်းထဲရှိ ဒေါင်းလုဒ်ဆွဲထားသော Douyin ဗီဒီယိုဖိုင်ကို Step 1 ရှိ '📁 Upload MP4' ဖြင့် စက္ကန့်ပိုင်းအတွင်း တိုက်ရိုက်တင်သွင်း၍ အလွယ်တကူ ဆက်လက်ပြုလုပ်နိုင်ပါသည်ခင်ဗျာ။`, 'error');
                    const btnFileTab = document.getElementById("btn-source-mode-file");
                    if (btnFileTab) btnFileTab.click();
                } else {
                    showStudioToast(`⚠️ Movie Recap ဖန်တီးမှု အဆင်မပြေဖြစ်သွားပါသည်:\n${errMsg}\n\n✅ သင့် Recap Credit ကို အကောင့်ထဲသို့ အလိုအလျောက် ပြန်လည်ဖြည့်သွင်းပေးထားပါသည်ခင်ဗျာ။`, 'success');
                }
                loadUserProfile();
                loadUserHistory();
            }
        } catch (err) {
            console.warn("Recap polling error:", err);
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
        showStudioToast(err.message, 'error');
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
        showStudioToast(err.message, 'error');
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
        showStudioToast(t("recap_processing_msg") || "Processing...", 'info');
        startRecapJobPolling(jobId);
    } catch (err) {
        showStudioToast(err.message, 'error');
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
        showStudioToast(err.message, 'error');
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
        showStudioToast(err.message, 'error');
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
        const res = await fetch("/api/downloader/fetch", {
            method: "POST",
            headers: getAuthHeaders(),
            body: JSON.stringify({ url: dlCurrent.url, format: fmt }),
        });
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
        showStudioToast(err.message, 'error');
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
            try {
                const res = await fetch("/api/downloader/extract", {
                    method: "POST",
                    headers: getAuthHeaders(),
                    body: JSON.stringify({ url: url }),
                });
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
                if (dlDuration) dlDuration.innerText = data.estimated_duration || "";
                if (dlThumb) {
                    if (data.thumbnail) {
                        dlThumb.src = data.thumbnail;
                        dlThumb.style.display = "block";
                    } else {
                        dlThumb.style.display = "none";
                    }
                }

                dlCurrent = { url: data.url || url, has_subtitles: data.has_subtitles };
                dlSetSrtCardState(data.has_subtitles);
                const dlActions = document.getElementById("dl-delivery-actions");
                if (dlActions) dlActions.innerHTML = "";

                resultCard.classList.remove("hidden");
            } catch (err) {
                showStudioToast(err.message, 'info');
            } finally {
                btnDl.disabled = false;
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
        const res = await fetch("/api/vip/plans", { headers: getAuthHeaders() });
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
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(numToCopy).then(() => {
            showStudioToast(`${numToCopy}\n${t("toast_copied")}`, 'success');
        }).catch(() => {
            prompt("Account Number:", numToCopy);
        });
    } else {
        prompt("Account Number:", numToCopy);
    }
}

// ── On-Demand Telegram Delivery Actions ─────────────────────────────────────
async function sendVoiceToTelegram(targetJobId) {
    const jobId = targetJobId || state.activeJobId;
    if (!jobId) {
        showStudioToast(t("toast_empty_text") || "Voice Job ID not found", 'warning');
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
        if (!res.ok) throw new Error(data.detail || "Failed to send to Telegram");
        haptic("success");
        if (btn && !targetJobId) {
            btn.classList.add("sent");
            btn.innerHTML = `<span>${t("btn_telegram_sent")}</span>`;
        }
        showStudioToast(t("toast_telegram_sent"), 'success');
    } catch (err) {
        showStudioToast(err.message || "Failed to send to Telegram", 'error');
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
        showStudioToast(t("toast_empty_url") || "Recap Job ID not found", 'warning');
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
        showStudioToast(err.message || "Failed to send to Telegram", 'error');
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
            if (file) {
                const reader = new FileReader();
                reader.onload = (ev) => {
                    previewImg.src = ev.target.result;
                    previewBox.classList.remove("hidden");
                };
                reader.readAsDataURL(file);
            } else {
                previewBox.classList.add("hidden");
            }
        });
    }
}

async function submitPaymentSlip() {
    const fileInput = document.getElementById("slip-file-input");
    const file = fileInput && fileInput.files ? fileInput.files[0] : null;
    if (!file) {
        showStudioToast("ကျေးဇူးပြု၍ ငွေလွှဲပြေစာ ပုံကို ရွေးချယ်ပေးပါခင်ဗျာ။", 'info');
        return;
    }

    haptic("heavy");
    const btn = document.getElementById("btn-submit-slip-action");
    if (btn) btn.disabled = true;

    try {
        const formData = new FormData();
        formData.append("package_code", activePackageCode);
        formData.append("amount_mmk", activeAmount);
        formData.append("slip_file", file);

        const headers = {};
        if (state.tg && state.tg.initData) {
            headers["X-Telegram-Init-Data"] = state.tg.initData;
        }

        const res = await fetch("/api/vip/submit-slip", {
            method: "POST",
            headers: headers,
            body: formData,
        });

        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Submission failed");

        showStudioToast(t("toast_success_slip"), 'success');
        closePaymentDrawer();
        if (fileInput) fileInput.value = "";
        const previewBox = document.getElementById("slip-preview-box");
        if (previewBox) previewBox.classList.add("hidden");
    } catch (e) {
        showStudioToast(e.message, 'error');
    } finally {
        if (btn) btn.disabled = false;
    }
}

// ── TAB 5: Role-Gated Admin Console ───────────────────────────────────────
async function loadAdminData() {
    if (!state.user?.is_admin) return;
    try {
        // Load overview stats
        const resStats = await fetch("/api/admin/overview", { headers: getAuthHeaders() });
        if (resStats.ok) {
            const stats = await resStats.json();
            const elUsers = document.getElementById("adm-total-users");
            const elVip = document.getElementById("adm-vip-users");
            const elSlips = document.getElementById("adm-pending-slips");
            const elQueue = document.getElementById("adm-queued-jobs");
            if (elUsers) elUsers.innerText = stats.total_users;
            if (elVip) elVip.innerText = stats.vip_users;
            if (elSlips) elSlips.innerText = stats.pending_slips;
            if (elQueue) elQueue.innerText = stats.queued_jobs !== undefined ? stats.queued_jobs : 0;
        }

        // Load live processing queue & dynamic settings
        loadAdminQueue();
        loadAdminSettings();

        // Load pending payment slips
        const resSlips = await fetch("/api/admin/pending-slips", { headers: getAuthHeaders() });
        const slipsContainer = document.getElementById("admin-slips-list");
        if (resSlips.ok && slipsContainer) {
            const slips = await resSlips.json();
            if (slips.length === 0) {
                slipsContainer.innerHTML = `<p class="text-muted">${t("admin_no_slips")}</p>`;
            } else {
                slipsContainer.innerHTML = "";
                slips.forEach(s => {
                    const item = document.createElement("div");
                    item.className = "slip-item-card";
                    item.innerHTML = `
                        <div class="slip-item-row">
                            <img src="${s.slip_url}" class="slip-thumb" onclick="openSlipModal('${s.slip_url}')" title="Click to view full image">
                            <div class="slip-info">
                                <div class="slip-user">${s.user_name} (@${s.username || s.user_id})</div>
                                <div class="slip-amount">${s.amount_mmk.toLocaleString()} MMK • Package: ${s.package_code}</div>
                                <div class="slip-date">${s.created_at}</div>
                            </div>
                        </div>
                        <div class="slip-actions">
                            <button class="btn-approve" onclick="adminApproveSlip('${s.id}')">${t("btn_approve")}</button>
                            <button class="btn-reject" onclick="adminRejectSlip('${s.id}')">${t("btn_reject")}</button>
                        </div>
                    `;
                    slipsContainer.appendChild(item);
                });
                // Receipts now require auth: hydrate thumbnails via fetch.
                slipsContainer.querySelectorAll(".slip-thumb").forEach(async (imgEl) => {
                    const authUrl = await fetchAuthImageUrl(imgEl.getAttribute("src"));
                    if (authUrl) imgEl.src = authUrl;
                });
            }
        }
        // Load 24h global history
        loadAdminHistory();
    } catch (e) {
        console.warn("Error loading admin data:", e);
    }
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

    if (bodyEl) {
        if (jobType === "RECAP") {
            const videoUrl = deliverables.video || downloadUrl;
            bodyEl.innerHTML = `
                <div class="modal-video-wrapper">
                    <video id="admin-modal-video" src="${videoUrl}" controls autoplay playsinline class="admin-modal-player-video"
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
                    <audio id="admin-modal-audio" src="${audioUrl}" controls autoplay class="admin-modal-player-audio"
                           onerror="handleAdminMediaError(this, '${jobId}', '${jobType}')"
                           onloadedmetadata="handleAdminMediaLoaded(this)">
                        Browser does not support HTML5 audio.
                    </audio>
                </div>
            `;
        }
    }

    if (delivEl) {
        let buttons = '';
        if (jobType === "RECAP") {
            if (deliverables.video) {
                buttons += `<a href="${deliverables.video}" download class="mini-chip gold" target="_blank">🎬 Download Video</a>`;
            }
            if (deliverables.audio) {
                buttons += `<a href="${deliverables.audio}" download class="mini-chip" target="_blank">🎙️ Download Audio</a>`;
            }
            if (deliverables.srt) {
                buttons += `<a href="${deliverables.srt}" download class="mini-chip" target="_blank">📝 Subtitles (SRT)</a>`;
            }
            if (deliverables.script) {
                buttons += `<a href="${deliverables.script}" download class="mini-chip" target="_blank">📄 Script (TXT)</a>`;
            }
        } else {
            const dlUrl = deliverables.download || downloadUrl;
            if (dlUrl) {
                buttons += `<a href="${dlUrl}" download class="mini-chip gold" target="_blank">📥 Download WAV / MP3</a>`;
            }
        }
        if (downloadUrl) {
            buttons += `<a href="${downloadUrl}" target="_blank" class="mini-chip" style="opacity: 0.85;">↗️ Browser Tab တွင်ဖွင့်မည်</a>`;
        }
        delivEl.innerHTML = buttons;
    }

    modal.classList.remove("hidden");
}

function closeAdminMediaModal() {
    const modal = document.getElementById("admin-media-modal");
    if (!modal) return;
    const bodyEl = document.getElementById("admin-media-body");
    if (bodyEl) {
        const vid = bodyEl.querySelector("video");
        if (vid) { vid.pause(); vid.src = ""; }
        const aud = bodyEl.querySelector("audio");
        if (aud) { aud.pause(); aud.src = ""; }
        bodyEl.innerHTML = "";
    }
    modal.classList.add("hidden");
}

function handleAdminMediaError(mediaEl, jobId, jobType) {
    console.warn("Admin media failed to load (expired/missing on server):", jobId);
    const wrapper = mediaEl.parentElement;
    if (wrapper) {
        mediaEl.style.display = "none";
        const oldErr = wrapper.querySelector(".admin-media-err-banner");
        if (!oldErr) {
            const errDiv = document.createElement("div");
            errDiv.className = "admin-media-err-banner mt-3 p-3 text-center";
            errDiv.style.borderRadius = "12px";
            errDiv.style.border = "1px solid rgba(239, 68, 68, 0.35)";
            errDiv.style.background = "rgba(239, 68, 68, 0.08)";
            errDiv.innerHTML = `
                <div style="font-size: 1.6rem; margin-bottom: 4px;">⌛</div>
                <strong style="color: #fca5a5; font-size: 13px;">အသံ/ဗီဒီယို ဖိုင် သက်တမ်းကုန်ဆုံးသွားပါပြီ (Media Expired / 404)</strong>
                <p class="text-xs text-muted mt-1" style="line-height: 1.5;">
                    ဤအလုပ်သည် ၂၄ နာရီကျော်လွန်သွားပြီဖြစ်၍ (သို့မဟုတ် Cloud Server အသစ် Deploy တင်ချိန်တွင် ယာယီ Disk Storage ဖိုင် ရှင်းလင်းခံရသဖြင့်) အသံဖိုင် မရှိတော့ပါ။
                </p>
                <div class="mt-2" style="display: flex; gap: 8px; justify-content: center;">
                    <button class="btn mini-btn secondary-btn" onclick="closeAdminMediaModal(); adminRetryJob('${jobId}');">
                        🔄 ပြန်လည် ထုတ်လုပ်မည် (Regenerate / Retry)
                    </button>
                </div>
            `;
            wrapper.appendChild(errDiv);
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

async function adminApproveSlip(reqId) {
    haptic("medium");
    showStudioConfirm("အတည်ပြုပြီး VIP သို့ မြှင့်တင်ပေးမည်လား?", async () => {
        try {
            const res = await fetch("/api/admin/approve-slip", {
                method: "POST",
                headers: getAuthHeaders(),
                body: JSON.stringify({ request_id: reqId }),
            });
            const data = await res.json();
            showStudioToast(data.message, 'info');
            loadAdminData();
        } catch (e) {
            showStudioToast(e.message, 'error');
        }
    });
}

async function adminRejectSlip(reqId) {
    haptic("medium");
    const reason = prompt("ပယ်ဖျက်ရသည့် အကြောင်းပြချက် ထည့်ပါ:", "Invalid payment receipt");
    if (!reason) return;
    try {
        const res = await fetch("/api/admin/reject-slip", {
            method: "POST",
            headers: getAuthHeaders(),
            body: JSON.stringify({ request_id: reqId, reason: reason }),
        });
        const data = await res.json();
        showStudioToast(data.message, 'info');
        loadAdminData();
    } catch (e) {
        showStudioToast(e.message, 'error');
    }
}

async function adminGrantVip() {
    const input = document.getElementById("adm-target-user-id");
    const uid = input ? parseInt(input.value) : 0;
    if (!uid) {
        showStudioToast("Telegram ID ထည့်ပါ", 'info');
        return;
    }
    haptic("medium");
    try {
        const res = await fetch("/api/admin/adjust-user", {
            method: "POST",
            headers: getAuthHeaders(),
            body: JSON.stringify({ user_id: uid, account_category: "PURCHASED" }),
        });
        const data = await res.json();
        showStudioToast(data.message, 'info');
        loadAdminData();
    } catch (e) {
        showStudioToast(e.message, 'error');
    }
}

async function adminAddMinutes() {
    const input = document.getElementById("adm-target-user-id");
    const uid = input ? parseInt(input.value) : 0;
    if (!uid) {
        showStudioToast("Telegram ID ထည့်ပါ", 'info');
        return;
    }
    haptic("medium");
    try {
        const res = await fetch("/api/admin/adjust-user", {
            method: "POST",
            headers: getAuthHeaders(),
            body: JSON.stringify({ user_id: uid, add_minutes: 60 }),
        });
        const data = await res.json();
        showStudioToast(data.message, 'info');
        loadAdminData();
    } catch (e) {
        showStudioToast(e.message, 'error');
    }
}

async function adminAddRecapCredits() {
    const input = document.getElementById("adm-target-user-id");
    const uid = input ? parseInt(input.value) : 0;
    if (!uid) {
        showStudioToast("Telegram ID ထည့်ပါ", 'info');
        return;
    }
    haptic("medium");
    try {
        const res = await fetch("/api/admin/adjust-user", {
            method: "POST",
            headers: getAuthHeaders(),
            body: JSON.stringify({ user_id: uid, add_recap_credits: 10 }),
        });
        const data = await res.json();
        showStudioToast(data.message, 'info');
        loadAdminData();
    } catch (e) {
        showStudioToast(e.message, 'error');
    }
}

async function adminSendBroadcast() {
    const textEl = document.getElementById("adm-broadcast-text");
    const btn = document.getElementById("adm-broadcast-btn");
    const btnText = document.getElementById("adm-broadcast-btn-text");
    const reportBox = document.getElementById("adm-broadcast-report");

    const msg = textEl ? textEl.value.trim() : "";
    if (!msg) {
        showStudioToast(currentLang === "my" ? "မက်ဆေ့ခ်ျ စာသား ရိုက်ထည့်ပေးပါ" : "Please enter broadcast message text", 'info');
        return;
    }
    haptic("heavy");
    showStudioConfirm(currentLang === "my"
        ? "အသုံးပြုသူ အားလုံးထံသို့ ဤ Broadcast မက်ဆေ့ခ်ျ ပို့မှာ သေချာပါသလား?"
        : "Are you sure you want to broadcast this message to all users?",
        async () => {
            if (btn) btn.disabled = true;
    if (btnText) btnText.textContent = currentLang === "my" ? "⏳ ပို့ဆောင်နေပါသည်..." : "⏳ Sending broadcast...";
    if (reportBox) {
        reportBox.classList.remove("hidden");
        reportBox.innerHTML = `<div style="text-align: center; color: var(--gold-primary); padding: 10px;">
            <span class="spinner" style="display:inline-block; margin-right:6px;"></span>
            ${currentLang === "my" ? "Telegram Bot မှတစ်ဆင့် အသုံးပြုသူများထံ စစ်ဆေး ပို့ဆောင်နေပါသည်..." : "Delivering messages via Telegram Bot..."}
        </div>`;
    }

    try {
        const res = await fetch("/api/admin/broadcast", {
            method: "POST",
            headers: getAuthHeaders(),
            body: JSON.stringify({ message: msg }),
        });
        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.detail || "Broadcast request failed");
        }

        const bd = data.breakdown || {};
        const isMy = (currentLang === "my");
        
        let reportHtml = `
            <div style="border-left: 3px solid #10b981; padding-left: 10px; margin-bottom: 12px;">
                <strong style="color: #10b981; font-size: 14px;">
                    ${isMy ? "📢 Broadcast ပို့ဆောင်မှု အစီရင်ခံစာ" : "📢 Broadcast Delivery Telemetry Report"}
                </strong>
            </div>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-bottom: 12px;">
                <div style="background: rgba(255,255,255,0.03); padding: 8px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.06);">
                    <div style="color: #94a3b8; font-size: 11px;">${isMy ? "👥 DB စုစုပေါင်း" : "Total DB Users"}</div>
                    <div style="color: #fff; font-size: 16px; font-weight: 700;">${data.total_users_in_db || 0} ဦး</div>
                </div>
                <div style="background: rgba(255,255,255,0.03); padding: 8px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.06);">
                    <div style="color: #94a3b8; font-size: 11px;">${isMy ? "🎯 ပို့ရန် စစ်ဆေးခဲ့သူ" : "Targeted Users"}</div>
                    <div style="color: #38bdf8; font-size: 16px; font-weight: 700;">${data.total_targeted || 0} ဦး</div>
                </div>
                <div style="background: rgba(16, 185, 129, 0.12); border: 1px solid rgba(16, 185, 129, 0.35); padding: 8px; border-radius: 8px;">
                    <div style="color: #10b981; font-size: 11px;">${isMy ? "✅ အောင်မြင်စွာ ပို့ဆောင်ပြီး" : "Delivered"}</div>
                    <div style="color: #10b981; font-size: 16px; font-weight: 700;">${data.sent_count || 0} ဦး</div>
                </div>
                <div style="background: rgba(239, 68, 68, 0.12); border: 1px solid rgba(239, 68, 68, 0.35); padding: 8px; border-radius: 8px;">
                    <div style="color: #ef4444; font-size: 11px;">${isMy ? "❌ မရောက်ရှိခဲ့သူ" : "Undelivered"}</div>
                    <div style="color: #ef4444; font-size: 16px; font-weight: 700;">${data.failed_count || 0} ဦး</div>
                </div>
            </div>
            
            <div style="background: rgba(0,0,0,0.3); border-radius: 8px; padding: 10px; font-size: 12px; line-height: 1.6;">
                <div style="color: #f59e0b; font-weight: 600; margin-bottom: 6px;">
                    ${isMy ? "🔍 မရောက်ရှိရသည့် အသေးစိတ် အကြောင်းအရင်းများ:" : "🔍 Undelivered Breakdown & Reasons:"}
                </div>
                <div style="color: #cbd5e1;">• 🚫 <b>${bd.blocked_by_user || 0} ဦး</b>: ${isMy ? "Bot ကို Block ထားသူများ (DB တွင် BLOCKED အဖြစ် အလိုအလျောက် သတ်မှတ်ပေးခဲ့သည်)" : "Blocked bot (Auto-marked as BLOCKED in DB)"}</div>
                <div style="color: #cbd5e1;">• ⚠️ <b>${bd.deactivated_or_not_found || 0} ဦး</b>: ${isMy ? "Telegram Account ဖျက်ထားသူ / ရှာမတွေ့သူများ" : "Telegram account deleted / not found"}</div>
                <div style="color: #cbd5e1;">• 🧪 <b>${bd.invalid_test_id || 0} ဦး</b>: ${isMy ? "Test / စမ်းသပ် ID အဟောင်းများ (< ၈ လုံး)" : "Legacy Test IDs (< 8 digits)"}</div>
                ${bd.pre_existing_blocked_in_db ? `<div style="color: #cbd5e1;">• 🛡️ <b>${bd.pre_existing_blocked_in_db} ဦး</b>: ${isMy ? "ယခင်ကတည်းက DB တွင် Blocked ဖြစ်နေသူများ" : "Pre-existing blocked users in DB"}</div>` : ""}
                ${bd.other_errors ? `<div style="color: #cbd5e1;">• ❓ <b>${bd.other_errors} ဦး</b>: ${isMy ? "အခြား ကွန်ရက်/ချိတ်ဆက်မှု အမှား" : "Network/Other errors"}</div>` : ""}
            </div>
        `;

        if (reportBox) {
            reportBox.innerHTML = reportHtml;
        }

        showStudioToast(isMy 
            ? `📢 Broadcast ပို့ဆောင်မှု ပြီးဆုံးပါပြီ! (အောင်မြင်: ${data.sent_count} ဦး / စုစုပေါင်း: ${data.total_users_in_db} ဦး)` 
            : `📢 Broadcast Completed! (Delivered: ${data.sent_count} / Total: ${data.total_users_in_db})`, 'success');

        if (textEl) textEl.value = "";
    } catch (e) {
        if (reportBox) {
            reportBox.innerHTML = `<div style="color: #ef4444; padding: 10px;">❌ ${e.message}</div>`;
        }
        showStudioToast(e.message, 'error');
    } finally {
        if (btn) btn.disabled = false;
        if (btnText) btnText.textContent = currentLang === "my" ? "Broadcast ပို့မည်" : "Send Broadcast";
    }
        }
    );
}

// ── Admin Queue & Engine Monitor Controller ────────────────────────────────
let adminQueueStatusFilter = "all";
let adminQueueEngineFilter = "all";

function setQueueStatusFilter(status) {
    adminQueueStatusFilter = status;
    document.querySelectorAll("#queue-status-filters .filter-pill").forEach(btn => {
        btn.classList.toggle("active", btn.getAttribute("data-status") === status);
    });
    loadAdminQueue();
}

function setQueueEngineFilter(engine) {
    adminQueueEngineFilter = engine;
    document.querySelectorAll("#queue-engine-filters .engine-pill").forEach(btn => {
        btn.classList.toggle("active", btn.getAttribute("data-engine") === engine);
    });
    loadAdminQueue();
}

async function loadAdminQueue(isSilent = false) {
    if (!state.user?.is_admin) return;
    const container = document.getElementById("admin-queue-list");
    const bannerEl = document.getElementById("admin-queue-status-banner");
    if (!container) return;

    if (!isSilent && container.innerHTML.trim() === "") {
        container.innerHTML = `<p class="text-muted">${t("admin_queue_loading")}</p>`;
    }

    try {
        const url = `/api/admin/queue?status=${encodeURIComponent(adminQueueStatusFilter)}&engine=${encodeURIComponent(adminQueueEngineFilter)}&limit=50`;
        const res = await fetch(url, { headers: getAuthHeaders() });
        if (!res.ok) return;

        const data = await res.json();

        // 1. Update Counts
        const c = data.counts || { total_listed: 0, queued: 0, running: 0, success: 0, failed: 0 };
        const elTotal = document.getElementById("cnt-total");
        const elQueued = document.getElementById("cnt-queued");
        const elRunning = document.getElementById("cnt-running");
        const elSuccess = document.getElementById("cnt-success");
        const elFailed = document.getElementById("cnt-failed");

        if (elTotal) elTotal.innerText = c.total_listed;
        if (elQueued) elQueued.innerText = c.queued;
        if (elRunning) elRunning.innerText = c.running;
        if (elSuccess) elSuccess.innerText = c.success;
        if (elFailed) elFailed.innerText = c.failed;

        const elTopQueue = document.getElementById("adm-queued-jobs");
        if (elTopQueue) elTopQueue.innerText = c.queued;

        // 2. Render High-Visibility Active Queue Status Banner (cloud pipeline)
        if (bannerEl) {
            if (c.queued === 0 && c.running === 0) {
                bannerEl.innerHTML = `
                    <div class="active-queue-status-banner idle">
                        <div class="status-banner-icon">🟢</div>
                        <div class="status-banner-info">
                            <div class="status-banner-title">${t("admin_queue_idle_title")}</div>
                            <div class="status-banner-desc">${t("admin_queue_idle_desc")}</div>
                            <div class="status-banner-meta">
                                <span class="meta-pill">☁️ Engine: <strong>Cloud AI</strong></span>
                                <span class="meta-pill">⏳ Active Queue: <strong>0 Pending</strong></span>
                            </div>
                        </div>
                    </div>
                `;
            } else if (c.running > 0) {
                bannerEl.innerHTML = `
                    <div class="active-queue-status-banner running">
                        <div class="status-banner-icon pulsing">⚡</div>
                        <div class="status-banner-info">
                            <div class="status-banner-title">${t("admin_queue_running_title")}</div>
                            <div class="status-banner-desc">Cloud engine is actively processing <strong>${c.running}</strong> job(s) right now.</div>
                            <div class="status-banner-meta">
                                <span class="meta-pill">⚡ Running: <strong>${c.running}</strong></span>
                                <span class="meta-pill">⏳ Waiting: <strong>${c.queued}</strong></span>
                            </div>
                        </div>
                    </div>
                `;
            } else {
                bannerEl.innerHTML = `
                    <div class="active-queue-status-banner queued">
                        <div class="status-banner-icon">⏳</div>
                        <div class="status-banner-info">
                            <div class="status-banner-title">${t("admin_queue_queued_title")}</div>
                            <div class="status-banner-desc">${c.queued} job(s) waiting in queue. The cloud engine picks them up automatically.</div>
                        </div>
                    </div>
                `;
            }
        }

        // 3. Render Jobs with Clear Visual Grouping
        const jobs = data.jobs || [];
        if (jobs.length === 0) {
            container.innerHTML = `<p class="text-muted" style="padding: 12px 0;">${t("admin_queue_empty")}</p>`;
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
                durationTxt = `⏳ In Queue: ${j.created_ago}`;
            } else if (statusLower === "running") {
                statusBadgeText = t("badge_running") || "⚡ PROCESSING";
                durationTxt = `⚡ Processing since: ${j.created_ago}`;
            } else if (statusLower === "failed") {
                statusBadgeText = t("badge_failed") || "❌ FAILED";
            }

            let actionsHtml = "";
            if (statusLower === "failed" || statusLower === "cancelled") {
                actionsHtml += `<button class="queue-act-btn retry" onclick="adminRetryJob('${j.id}')">🔄 Retry</button>`;
            } else if (statusLower === "queued" || statusLower === "running") {
                actionsHtml += `<button class="queue-act-btn cancel" onclick="adminCancelJob('${j.id}')">⏹️ Cancel</button>`;
            }

            if (j.is_expired) {
                actionsHtml += `<span class="badge-expired text-xs" title="ဖိုင်သက်တမ်းကုန်သွားပါပြီ (Expired)">⌛ သက်တမ်းကုန်</span>`;
                actionsHtml += `<button class="queue-act-btn retry" onclick="adminRetryJob('${j.id}')" title="ပြန်လည်ထုတ်လုပ်မည်">🔄 Re-gen</button>`;
            } else if (j.download_url) {
                const qDelivStr = JSON.stringify(j.deliverables || {}).replace(/"/g, '&quot;');
                const qTitleStr = `${j.username ? '@' + j.username : 'ID: ' + j.user_id} • ${j.preset_voice || j.engine}`.replace(/"/g, '&quot;');
                actionsHtml += `<button class="queue-act-btn uid" onclick="openAdminMediaModal('${j.id}', '${j.job_type}', '${escapeHtml(j.download_url)}', '${qDelivStr}', '${qTitleStr}')">▶️ Play</button>`;
                actionsHtml += `<a href="${j.download_url}" target="_blank" class="queue-act-btn uid" title="Open Stream">↗️</a>`;
            }

            actionsHtml += `<button class="queue-act-btn uid" onclick="admFillUser(${j.user_id})" title="Copy User ID to management input">👤 Target</button>`;

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
                        <span class="queue-user-pill" onclick="admFillUser(${j.user_id})">👤 ${escapeHtml(j.user_name)} (@${escapeHtml(j.username || 'none')}) • ID: ${j.user_id}</span>
                        <span>#${j.id.slice(0, 8)}</span>
                    </div>

                    <div class="queue-item-body">
                        ${escapeHtml(j.input_snippet || "Task Content")}
                    </div>

                    ${j.error_message ? `<div class="queue-item-error">⚠️ ${escapeHtml(j.error_message)}</div>` : ""}

                    <div class="queue-item-footer">
                        <span class="queue-timing">Created: ${escapeHtml(j.created_ago)}</span>
                        <div class="queue-actions-row">
                            ${actionsHtml}
                        </div>
                    </div>
                </div>
            `;
        };

        if (adminQueueStatusFilter === "all") {
            const activeJobs = jobs.filter(j => j.status === "QUEUED" || j.status === "RUNNING");
            const finishedJobs = jobs.filter(j => j.status !== "QUEUED" && j.status !== "RUNNING");

            let fullHtml = "";
            if (activeJobs.length > 0) {
                fullHtml += `<div class="queue-section-header active" style="grid-column: 1 / -1;">🔥 ${t("admin_active_section_title")} (${activeJobs.length})</div>`;
                fullHtml += activeJobs.map(renderJobCard).join("");
            }

            if (finishedJobs.length > 0) {
                fullHtml += `<div class="queue-section-header history" style="grid-column: 1 / -1;">📜 ${t("admin_history_section_title")} (${finishedJobs.length})</div>`;
                fullHtml += finishedJobs.map(renderJobCard).join("");
            }

            container.innerHTML = fullHtml || `<p class="text-muted" style="padding: 12px 0;">${t("admin_queue_empty")}</p>`;
        } else {
            container.innerHTML = jobs.map(renderJobCard).join("");
        }

    } catch (e) {
        console.warn("Could not load admin queue:", e);
    }
}

async function adminCancelJob(jobId) {
    showStudioConfirm("Are you sure you want to cancel this job?", async () => {
        haptic("medium");
        try {
            const res = await fetch(`/api/admin/queue/${jobId}/cancel`, {
                method: "POST",
                headers: getAuthHeaders(),
            });
            const data = await res.json();
            if (data.success) {
                loadAdminQueue();
            } else {
                showStudioToast(data.message || "Failed to cancel job", 'error');
            }
        } catch (e) {
            showStudioToast("Cancel error: " + e.message, 'error');
        }
    });
}

function admFillUser(userId) {
    const input = document.getElementById("adm-target-user-id");
    if (input) {
        input.value = userId;
        input.scrollIntoView({ behavior: "smooth", block: "center" });
        input.focus();
        haptic("light");
    }
}

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

        if (elTutVoiceEn) elTutVoiceEn.checked = String(s.tutorial_voice_enabled).toLowerCase() !== "false" && s.tutorial_voice_enabled !== false;
        if (elTutVoiceTitle) elTutVoiceTitle.value = s.tutorial_voice_title || "Voice Clone ပြုလုပ်နည်း လမ်းညွှန်";
        if (elTutVoiceLink) elTutVoiceLink.value = s.tutorial_voice_link || s.tutorial_video_link || "";

        if (elTutRecapEn) elTutRecapEn.checked = String(s.tutorial_recap_enabled).toLowerCase() !== "false" && s.tutorial_recap_enabled !== false;
        if (elTutRecapTitle) elTutRecapTitle.value = s.tutorial_recap_title || "Movie Recap ပြုလုပ်နည်း လမ်းညွှန်";
        if (elTutRecapLink) elTutRecapLink.value = s.tutorial_recap_link || s.tutorial_video_link || "";

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
        showStudioToast("✅ Gemini Model Chain အောင်မြင်စွာ ပြောင်းလဲသိမ်းဆည်းပြီးပါပြီ!\n\n" +
              `Primary: ${primary}\nFallback 1: ${fb1}\nFallback 2: ${fb2}\nFallback 3: ${fb3}`, 'success');
        loadAdminSettings();
    } catch (e) {
        showStudioToast("Failed to save Gemini models: " + e.message, 'error');
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

    const tutVoiceEn = !!document.getElementById("adm-tut-voice-enabled")?.checked;
    const tutVoiceTitle = document.getElementById("adm-tut-voice-title")?.value?.trim() || "Voice Clone ပြုလုပ်နည်း လမ်းညွှန်";
    const tutVoiceLink = document.getElementById("adm-tut-voice-link")?.value?.trim() || "";

    const tutRecapEn = !!document.getElementById("adm-tut-recap-enabled")?.checked;
    const tutRecapTitle = document.getElementById("adm-tut-recap-title")?.value?.trim() || "Movie Recap ပြုလုပ်နည်း လမ်းညွှန်";
    const tutRecapLink = document.getElementById("adm-tut-recap-link")?.value?.trim() || "";

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
        maintenance_mode: maint,
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
        showStudioToast(data.message || "Settings updated successfully!", 'success');
        loadAdminSettings();
    } catch (e) {
        showStudioToast("Failed to save settings: " + e.message, 'error');
    }
}

// ── 24-Hour Job History & Lifecycle Controller ─────────────────────────────
function escapeHtml(str) {
    if (!str) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

async function loadUserHistory() {
    try {
        const res = await fetch("/api/history", {
            headers: getAuthHeaders(),
        });
        if (!res.ok) return;

        const data = await res.json();
        const history = data.history || [];

        const voiceContainer = document.getElementById("voice-user-history-list");
        const recapContainer = document.getElementById("recap-user-history-list");

        const voiceJobs = history.filter(h => h.job_type === "VOICE_CLONE");
        const recapJobs = history.filter(h => h.job_type === "RECAP");

        // Render Voice History (24h)
        if (voiceContainer) {
            if (voiceJobs.length === 0) {
                voiceContainer.innerHTML = `<p class="text-muted" data-i18n="history_empty">${t("history_empty")}</p>`;
            } else {
                voiceContainer.innerHTML = voiceJobs.map(j => `
                    <div class="history-item">
                        <div class="history-item-header">
                            <span class="history-item-badge badge-voice">🎙️ ${escapeHtml(j.preset_voice)}</span>
                            <span class="history-item-badge status-${j.status.toLowerCase()}">${j.status}</span>
                        </div>
                        <div class="history-item-body">"${escapeHtml(j.input_snippet || "Voice Synthesis")}"</div>
                        <div class="history-item-footer">
                            <span>⏱️ ${j.time_ago}</span>
                            <div class="history-btn-row">
                                ${j.status === "FAILED" ? `
                                    <span class="badge-expired text-xs" style="color:#f87171;">❌ မအောင်မြင်ပါ</span>
                                ` : (j.status === "RUNNING" || j.status === "QUEUED") ? `
                                    <button type="button" onclick="startJobPolling('${j.id}')" class="history-dl-link">⏳ ထုတ်လုပ်နေဆဲ (ကြည့်မည်)</button>
                                ` : j.is_expired ? `
                                    <span class="badge-expired text-xs" title="ဖိုင်သက်တမ်းကုန်သွားပါပြီ">⌛ သက်တမ်းကုန်</span>
                                ` : (j.download_url ? `<a href="${j.download_url}" download="voice_${j.id.slice(0,8)}.wav" class="history-dl-link">📥 Download</a> <button type="button" onclick="sendVoiceToTelegram('${j.id}')" class="history-dl-link tg-btn" title="Telegram သို့ ပို့မည်">📱 Telegram</button>` : '')}
                            </div>
                        </div>
                    </div>
                `).join("");
            }
        }

        // Render Recap History (24h)
        if (recapContainer) {
            if (recapJobs.length === 0) {
                recapContainer.innerHTML = `<p class="text-muted" data-i18n="history_empty">${t("history_empty")}</p>`;
            } else {
                recapContainer.innerHTML = recapJobs.map(j => `
                    <div class="history-item">
                        <div class="history-item-header">
                            <span class="history-item-badge badge-recap">🎬 RECAP • ${escapeHtml(j.preset_voice)}</span>
                            <span class="history-item-badge status-${j.status.toLowerCase()}">${j.status}</span>
                        </div>
                        <div class="history-item-body">🔗 ${escapeHtml(j.input_snippet || "Movie Recap")}</div>
                        ${(j.parts_count && j.parts_count > 1) ? `<div class="history-item-body">${escapeHtml(tf("recap_segmented_cost", { parts: j.parts_count, credits: (j.credits_charged ?? j.parts_count) }))}</div>` : ''}
                        <div class="history-item-footer">
                            <span>⏱️ ${j.time_ago}</span>
                            <div class="history-btn-row">
                                ${j.status === "FAILED" ? `
                                    <span class="badge-expired text-xs" style="color:#f87171;">❌ မအောင်မြင်ပါ (Credit ပြန်အမ်းပြီး)</span>
                                ` : (j.status === "RUNNING" || j.status === "QUEUED") ? `
                                    <button type="button" onclick="startRecapJobPolling('${j.id}')" class="history-dl-link">⏳ ထုတ်လုပ်နေဆဲ (ခြေရာခံမည်)</button>
                                ` : j.is_expired ? `
                                    <span class="badge-expired text-xs" title="ဗီဒီယိုဖိုင်သက်တမ်းကုန်သွားပါပြီ">⌛ သက်တမ်းကုန်</span>
                                ` : `
                                    ${j.deliverables?.video ? `<button type="button" onclick="openRecapInPlayer('${j.id}')" class="history-dl-link" title="Mini App တွင် တိုက်ရိုက်ကြည့်မည်">▶️ ကြည့်မည်</button> <a href="${j.deliverables.video}" download="recap_${j.id.slice(0,8)}.mp4" class="history-dl-link">🎬 Video</a> <button type="button" onclick="sendRecapToTelegram('${j.id}')" class="history-dl-link tg-btn" title="Telegram သို့ ပို့မည်">📱 Telegram</button>` : ''}
                                    ${j.deliverables?.audio ? `<a href="${j.deliverables.audio}" download="audio_${j.id.slice(0,8)}.wav" class="history-dl-link">🎙️ Audio</a>` : ''}
                                    ${j.deliverables?.srt ? `<a href="${j.deliverables.srt}" download="sub_${j.id.slice(0,8)}.srt" class="history-dl-link">📝 SRT</a>` : ''}
                                    ${j.deliverables?.script ? `<a href="${j.deliverables.script}" download="script_${j.id.slice(0,8)}.txt" class="history-dl-link">📜 TXT</a>` : ''}
                                `}
                            </div>
                        </div>
                    </div>
                `).join("");
            }
        }
    } catch (e) {
        console.warn("Could not load user history:", e);
    }
}

let adminHistFilter = {
    timeframe: "24h",
    status: "all",
    job_type: "all"
};

function setAdminHistFilter(key, val) {
    if (key) {
        adminHistFilter[key] = val;
    }
    // Update active state on timeframe chips
    const chips = document.querySelectorAll('.filter-chip[data-filter-type="timeframe"]');
    chips.forEach(c => {
        if (c.getAttribute('data-val') === adminHistFilter.timeframe) {
            c.classList.add('active');
        } else {
            c.classList.remove('active');
        }
    });

    const selStatus = document.getElementById("adm-hist-status-sel");
    if (selStatus && selStatus.value !== adminHistFilter.status) {
        selStatus.value = adminHistFilter.status;
    }

    const selType = document.getElementById("adm-hist-type-sel");
    if (selType && selType.value !== adminHistFilter.job_type) {
        selType.value = adminHistFilter.job_type;
    }

    loadAdminHistory();
}

async function loadAdminHistory() {
    try {
        const params = new URLSearchParams({
            timeframe: adminHistFilter.timeframe || "24h",
            status: adminHistFilter.status || "all",
            job_type: adminHistFilter.job_type || "all",
            limit: "100"
        });
        const res = await fetch(`/api/admin/history?${params.toString()}`, {
            headers: getAuthHeaders(),
        });
        if (!res.ok) return;

        const data = await res.json();
        const elTotal = document.getElementById("adm-hist-total");
        const elFailed = document.getElementById("adm-hist-failed");
        const elSuccess = document.getElementById("adm-hist-success");
        const elRecap = document.getElementById("adm-hist-recap");
        const elVoice = document.getElementById("adm-hist-voice");
        const container = document.getElementById("admin-global-history-list");

        if (elTotal) elTotal.innerText = data.total_count ?? (data.history?.length || 0);
        if (elFailed) elFailed.innerText = data.total_failed || 0;
        if (elSuccess) elSuccess.innerText = data.total_success || 0;
        if (elRecap) elRecap.innerText = data.total_recap || 0;
        if (elVoice) elVoice.innerText = data.total_voice || 0;

        const history = data.history || [];
        if (container) {
            if (history.length === 0) {
                container.innerHTML = `<div class="p-4 text-center text-muted" style="border: 1px dashed rgba(255,255,255,0.1); border-radius: 12px;">
                    <p style="font-size: 1.1rem; margin-bottom: 4px;">📭 မှတ်တမ်းမရှိပါ</p>
                    <span class="text-xs">ရွေးချယ်ထားသော Filter နှင့် ကိုက်ညီသည့် အလုပ်မှတ်တမ်း ရှာမတွေ့ပါ။</span>
                </div>`;
            } else {
                container.innerHTML = history.map(j => {
                    const delivStr = JSON.stringify(j.deliverables || {}).replace(/"/g, '&quot;');
                    const titleStr = `${j.username ? '@' + j.username : 'ID: ' + j.user_id} • ${j.preset_voice || j.engine}`.replace(/"/g, '&quot;');
                    const isFailed = (j.status === "FAILED");
                    const isSuccess = (j.status === "SUCCESS");
                    const isRunning = (j.status === "RUNNING" || j.status === "QUEUED");
                    const borderCls = isFailed ? 'border-danger-dim' : (isSuccess ? (j.is_expired ? 'border-warning-dim' : 'border-success-dim') : (isRunning ? 'border-running-dim' : ''));

                    return `
                    <div class="history-item ${borderCls}" id="adm-job-item-${j.id}">
                        <div class="history-item-header">
                            <div>
                                <span class="history-item-badge ${j.job_type === 'RECAP' ? 'badge-recap' : 'badge-voice'}">
                                    ${j.job_type === 'RECAP' ? '🎬 RECAP' : '🎙️ VOICE'}
                                </span>
                                <span class="mini-tag" style="margin-left: 4px;">👤 ${escapeHtml(j.username ? '@' + j.username : 'ID: ' + j.user_id)}</span>
                            </div>
                            <div>
                                <span class="history-item-badge status-${j.status.toLowerCase()}">${j.status}</span>
                                ${j.is_expired ? `<span class="badge-expired" style="margin-left: 4px;">⌛ သက်တမ်းကုန်</span>` : ''}
                            </div>
                        </div>
                        <div class="history-item-body">
                            <strong>${escapeHtml(j.preset_voice || j.engine)}:</strong> ${escapeHtml(j.input_snippet || "Task")}
                            ${j.error_message ? `
                                <div class="history-error-snippet text-xs text-danger mt-1 p-2 glass-inset" style="border-radius: 8px; word-break: break-all; color: #fca5a5; background: rgba(239,68,68,0.08); border: 1px solid rgba(239,68,68,0.25);">
                                    ⚠️ <strong>Error:</strong> ${escapeHtml(j.error_message.slice(0, 180))}${j.error_message.length > 180 ? '...' : ''}
                                </div>
                            ` : ''}
                            <div id="retry-status-box-${j.id}"></div>
                        </div>
                        <div class="history-item-footer">
                            <span>⏱️ ${j.time_ago} <code style="opacity: 0.7; font-size: 10px;">(${j.id.slice(0,8)})</code></span>
                            <div class="history-btn-row" id="adm-actions-row-${j.id}">
                                ${j.is_expired ? `
                                    <button class="btn mini-btn secondary-btn btn-retry-job" id="btn-retry-${j.id}" onclick="adminRetryJob('${j.id}')" title="ဖိုင်သက်တမ်းကုန်သွားသဖြင့် အသစ်ပြန်လည်ထုတ်ယူမည်">
                                        🔄 ပြန်ထုတ်မည်
                                    </button>
                                ` : (j.download_url ? `
                                    <button class="btn-play-preview" onclick="openAdminMediaModal('${j.id}', '${j.job_type}', '${escapeHtml(j.download_url)}', '${delivStr}', '${titleStr}')">
                                        ▶️ Preview
                                    </button>
                                    <a href="${j.download_url}" target="_blank" class="history-dl-link" title="Open Stream">↗️</a>
                                ` : '')}
                                ${isFailed ? `
                                    <button class="btn mini-btn secondary-btn btn-retry-job" id="btn-retry-${j.id}" onclick="adminRetryJob('${j.id}')" title="Re-queue this failed job">
                                        🔄 Retry
                                    </button>
                                ` : ''}
                                <button class="btn mini-btn danger-btn" onclick="adminDeleteSingleJob('${j.id}')" title="Delete record and media files">
                                    🗑️ ဖျက်မည်
                                </button>
                            </div>
                        </div>
                    </div>
                `;
                }).join("");
            }
        }
    } catch (e) {
        console.warn("Could not load admin history:", e);
    }
}

let activeRetryPollers = {};

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
            showStudioToast("Retry failed: " + (data.detail || data.message || "Unknown error"), 'error');
            if (retryBtn) {
                retryBtn.disabled = false;
                retryBtn.innerText = "🔄 Retry";
            }
        }
    } catch (e) {
        showStudioToast("Retry error: " + e.message, 'error');
        if (retryBtn) {
            retryBtn.disabled = false;
            retryBtn.innerText = "🔄 Retry";
        }
    }
}

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
                    if (data.is_waiting_laptop_worker) {
                        label = "VoxCPM2 Cloud တွင် စောင့်ဆိုင်းနေပါသည်...";
                    }
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
    showStudioConfirm(`ဤအလုပ် (Job: ${jobId.slice(0,8)}) နှင့် သက်ဆိုင်ရာ အသံ/ဗီဒီယို ဖိုင်များကို Database နှင့် Disk မှ လုံးဝ ဖျက်ပစ်မည်မှာ သေချာပါသလား?`, async () => {
        haptic("heavy");
        try {
            const res = await fetch("/api/admin/history/purge", {
                method: "POST",
                headers: {
                    ...getAuthHeaders(),
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({
                    mode: "single",
                    job_id: jobId,
                }),
            });
            const data = await res.json();
            if (res.ok) {
                const el = document.getElementById(`adm-job-item-${jobId}`);
                if (el) el.remove();
                loadAdminHistory();
            } else {
                showStudioToast("Delete failed: " + (data.detail || data.message || "Unknown error"), 'error');
            }
        } catch (e) {
            showStudioToast("Delete error: " + e.message, 'error');
        }
    });
}

async function adminPurgeHistory(mode) {
    let confirmMsg = "";
    if (mode === "failed") {
        confirmMsg = "❌ မအောင်မြင်ဘဲ ရပ်တန့်နေသော Error ဖြစ်ထားသည့် အလုပ်များ (FAILED Jobs) အားလုံးကို ဖိုင်များနှင့်တကွ လုံးဝရှင်းလင်းမည်မှာ သေချာပါသလား?";
    } else if (mode === "older_24h") {
        confirmMsg = "⏱️ ၂၄ နာရီကျော်လွန်နေသော အလုပ်များနှင့် ဖိုင်များအားလုံးကို ရှင်းလင်းဖျက်သိမ်းမည်မှာ သေချာပါသလား?";
    } else if (mode === "matching") {
        confirmMsg = `🎯 လက်ရှိ Filter သတ်မှတ်ချက် (အချိန်: ${adminHistFilter.timeframe}, အခြေအနေ: ${adminHistFilter.status}, အမျိုးအစား: ${adminHistFilter.job_type}) နှင့် ကိုက်ညီသည့် အလုပ်များအားလုံးကို ရှင်းလင်းမည်မှာ သေချာပါသလား?`;
    } else {
        confirmMsg = "အလုပ်မှတ်တမ်းများနှင့် ဖိုင်များကို ရှင်းလင်းမည်မှာ သေချာပါသလား?";
    }

    showStudioConfirm(confirmMsg, async () => {
        haptic("heavy");
        try {
            const res = await fetch("/api/admin/history/purge", {
                method: "POST",
                headers: {
                    ...getAuthHeaders(),
                    "Content-Type": "application/json",
                },
                body: JSON.stringify({
                    mode: mode,
                    timeframe: adminHistFilter.timeframe,
                    status: adminHistFilter.status,
                    job_type: adminHistFilter.job_type,
                }),
            });
            const data = await res.json();
            if (res.ok) {
                showStudioToast(`✅ ${data.message || `အောင်မြင်စွာ ရှင်းလင်းပြီးပါပြီ (ဖျက်ပြီးသမျှ: ${data.purged_count})`}`, 'success');
                loadAdminHistory();
            } else {
                showStudioToast("Purge failed: " + (data.detail || data.message || "Unknown error"), 'error');
            }
        } catch (e) {
            showStudioToast("Purge error: " + e.message, 'error');
        }
    });
}

async function adminTriggerPurge24h() {
    return adminPurgeHistory("older_24h");
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
                player.src = data.download_urls.video;
                player.classList.remove("hidden");
                player.play().catch(() => {});
            }
            if (btnVideo && data.download_urls?.video) {
                btnVideo.href = data.download_urls.video;
                btnVideo.setAttribute("data-dl-url", data.download_urls.video);
            }
            if (btnAudio && data.download_urls?.audio) {
                btnAudio.href = data.download_urls.audio;
                btnAudio.setAttribute("data-dl-url", data.download_urls.audio);
            }
            if (btnSrt && data.download_urls?.srt) {
                btnSrt.href = data.download_urls.srt;
                btnSrt.setAttribute("data-dl-url", data.download_urls.srt);
            }
            if (btnScript && data.download_urls?.script) {
                btnScript.href = data.download_urls.script;
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
                        audioPlayer.src = data.output_audio_url;
                        audioPlayer.load();
                        audioPlayer.play().catch(() => {});
                    }
                    if (btnDl && data.output_audio_url) {
                        btnDl.href = data.output_audio_url;
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
                        player.src = data.download_urls.video;
                        player.classList.remove("hidden");
                    }
                    if (btnVideo && data.download_urls?.video) {
                        btnVideo.href = data.download_urls.video;
                        btnVideo.setAttribute("data-dl-url", data.download_urls.video);
                    }
                    if (btnAudio && data.download_urls?.audio) {
                        btnAudio.href = data.download_urls.audio;
                        btnAudio.setAttribute("data-dl-url", data.download_urls.audio);
                    }
                    if (btnSrt && data.download_urls?.srt) {
                        btnSrt.href = data.download_urls.srt;
                        btnSrt.setAttribute("data-dl-url", data.download_urls.srt);
                    }
                    if (btnScript && data.download_urls?.script) {
                        btnScript.href = data.download_urls.script;
                        btnScript.setAttribute("data-dl-url", data.download_urls.script);
                    }
                    if (btnTg) {
                        btnTg.disabled = false;
                        btnTg.classList.remove("sent");
                        btnTg.innerHTML = `<span class="btn-icon">📱</span> <span>${t("btn_send_recap_telegram")}</span>`;
                    }
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

    loadUserProfile();
    loadCuratedVoices();
    loadLegacyModels();
    loadUserHistory();
    checkStartupDeepLinks();
});

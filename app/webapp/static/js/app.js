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
    selectedEngine: "voxcpm",       // "voxcpm" | "applio"
    voxcpmSubMode: "curated",       // "curated" | "custom"
    selectedPersona: "komin",
    selectedModelSlug: "voxcpm-cloud-ai",
    selectedApplioSlug: null,
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
    recapFontSize: 16,
    recapLines: 1,
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
            state.tg.setHeaderColor("#07090e");
            state.tg.setBackgroundColor("#07090e");
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

function getAuthHeaders() {
    const headers = { "Content-Type": "application/json" };
    if (state.tg && state.tg.initData) {
        headers["X-Telegram-Init-Data"] = state.tg.initData;
    }
    return headers;
}
window.getAuthHeaders = getAuthHeaders;

// ── Tutorial Video Link Controller ──────────────────────────────────────────
let currentTutorialUrl = "https://t.me/VoiceClonePro/16";

function openTutorialVideo() {
    haptic("light");
    const url = currentTutorialUrl || "https://t.me/VoiceClonePro/16";
    if (window.Telegram?.WebApp?.openLink) {
        window.Telegram.WebApp.openLink(url);
    } else {
        window.open(url, "_blank");
    }
}
window.openTutorialVideo = openTutorialVideo;

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
        if (!res.ok) return;
        const data = await res.json();
        state.user = data;

        if (data.tutorial_video_link) {
            currentTutorialUrl = data.tutorial_video_link;
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

        // Enable Admin Tab if user is administrator
        if (data.is_admin) {
            if (adminNavBtn) adminNavBtn.classList.remove("hidden");
            if (adminTab) adminTab.classList.remove("hidden");
        }

    } catch (err) {
        console.warn("User profile fetch error:", err);
    }
}

// ── API: Curated Voices (12 Gemini Personas) ───────────────────────────────
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

// ── API: Applio Models Loader ──────────────────────────────────────────────
async function loadApplioModels() {
    const container = document.getElementById("models-list");
    if (!container) return;

    try {
        const res = await fetch("/api/voices/models", { headers: getAuthHeaders() });
        if (!res.ok) return;
        const models = await res.json();
        state.models = models;

        const applioModels = models.filter(m => !m.is_voxcpm);
        container.innerHTML = "";

        if (applioModels.length === 0) {
            container.innerHTML = `<p class="text-muted" style="grid-column: 1 / -1; padding: 12px; text-align: center;">No Applio models found in database.</p>`;
            return;
        }

        // Preserve and assign default Applio model selection without corrupting VoxCPM
        if (!state.selectedApplioSlug && applioModels.length > 0) {
            state.selectedApplioSlug = applioModels[0].slug;
        }
        if (state.selectedEngine === "applio") {
            state.selectedModelSlug = state.selectedApplioSlug || applioModels[0].slug;
        }

        const avatarMap = {
            "kartoon": "🎭",
            "myanmar-voice-1": "🎙️",
            "the-light": "💡",
            "ant-movie": "🎬",
        };

        applioModels.forEach(m => {
            const card = document.createElement("div");
            const isSelected = (m.slug === (state.selectedApplioSlug || applioModels[0].slug));
            card.className = `applio-model-card ${isSelected ? "active" : ""}`;
            card.setAttribute("data-slug", m.slug);

            const avatar = avatarMap[m.slug] || "🎙️";
            card.innerHTML = `
                <span class="applio-model-avatar">${avatar}</span>
                <div class="applio-model-details">
                    <span class="applio-model-name">${m.name}</span>
                    <span class="applio-model-tag">RVC v2 • Local GPU</span>
                </div>
                <button type="button" class="voice-preview-btn mini" onclick="event.stopPropagation(); playVoicePreview('${m.slug}', this)">
                    <span class="preview-btn-icon">▶️</span>
                    <span class="preview-btn-label">${t("preview_voice") || "နမူနာ"}</span>
                </button>
            `;

            card.addEventListener("click", () => {
                haptic("selection");
                document.querySelectorAll(".applio-model-card").forEach(c => c.classList.remove("active"));
                card.classList.add("active");
                state.selectedApplioSlug = m.slug;
                if (state.selectedEngine === "applio") {
                    state.selectedModelSlug = m.slug;
                }
            });

            container.appendChild(card);
        });

    } catch (err) {
        console.warn("Could not load Applio models:", err);
    }
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
            if (text) text.innerText = currentLang === "my" ? "ခေတ္တရပ်မည် (Pause Audio)" : "Pause Audio";
        }).catch(err => {
            console.warn("Audio play error:", err);
            // If autoplay is blocked or user needs to click again
            if (icon) icon.innerText = "▶️";
            if (text) text.innerText = currentLang === "my" ? "အသံဖွင့်နားထောင်မည် (Play Audio)" : "Play Audio";
        });
    } else {
        player.pause();
        if (icon) icon.innerText = "▶️";
        if (text) text.innerText = currentLang === "my" ? "အသံဖွင့်နားထောင်မည် (Play Audio)" : "Play Audio";
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
        if (text) text.innerText = currentLang === "my" ? "ခေတ္တရပ်မည် (Pause Audio)" : "Pause Audio";
    });
    player.addEventListener("pause", () => {
        const icon = document.getElementById("play-toggle-icon");
        const text = document.getElementById("play-toggle-text");
        if (icon) icon.innerText = "▶️";
        if (text) text.innerText = currentLang === "my" ? "အသံဖွင့်နားထောင်မည် (Play Audio)" : "Play Audio";
    });
    player.addEventListener("ended", () => {
        const icon = document.getElementById("play-toggle-icon");
        const text = document.getElementById("play-toggle-text");
        if (icon) icon.innerText = "🔄";
        if (text) text.innerText = currentLang === "my" ? "ပြန်လည်နားထောင်မည် (Replay)" : "Replay Audio";
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
        });
    }

    // Engine Selector Buttons (VoxCPM vs Applio)
    const btnVox = document.getElementById("btn-engine-voxcpm");
    const btnApplio = document.getElementById("btn-engine-applio");
    const voxModeSection = document.getElementById("voxcpm-mode-section");
    const applioSection = document.getElementById("applio-models-section");
    const voiceQueueNotice = document.getElementById("voice-queue-notice");

    if (btnVox && btnApplio) {
        btnVox.addEventListener("click", () => {
            haptic("selection");
            btnVox.classList.add("active");
            btnApplio.classList.remove("active");
            state.selectedEngine = "voxcpm";
            state.selectedModelSlug = "voxcpm-cloud-ai";
            if (voxModeSection) voxModeSection.classList.remove("hidden");
            if (applioSection) applioSection.classList.add("hidden");
            if (voiceQueueNotice) {
                voiceQueueNotice.className = "queue-notice-banner";
                voiceQueueNotice.querySelector(".notice-text").innerText = t("queue_notice_voxcpm");
            }
        });

        btnApplio.addEventListener("click", () => {
            haptic("selection");
            btnApplio.classList.add("active");
            btnVox.classList.remove("active");
            state.selectedEngine = "applio";
            state.selectedModelSlug = state.selectedApplioSlug || "kartoon";
            if (voxModeSection) voxModeSection.classList.add("hidden");
            if (applioSection) applioSection.classList.remove("hidden");
            if (voiceQueueNotice) {
                voiceQueueNotice.className = "queue-notice-banner applio-notice";
                voiceQueueNotice.querySelector(".notice-text").innerText = t("queue_notice_applio");
            }
            loadApplioModels();
        });
    }

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

// ── Submit Voice Job & Live Polling ─────────────────────────────────────────
async function handleVoiceGenerate() {
    const textInput = document.getElementById("voice-text-input");
    const text = textInput ? textInput.value.trim() : "";
    if (!text) {
        alert(t("toast_empty_text"));
        return;
    }

    const maxChars = state.user?.character_limit || (state.user?.is_vip ? 5000 : 1000);
    if (text.length > maxChars) {
        alert(currentLang === "my"
            ? `စာသား စာလုံးရေ (${text.length}) သည် ${state.user?.is_vip ? "VIP" : "အခမဲ့ (Free)"} အသုံးပြုသူ ကန့်သတ်ချက် (${maxChars} လုံး) ထက် ကျော်လွန်နေပါသည်။ ` + (state.user?.is_vip ? "" : "ကန့်သတ်ချက်မရှိ (စာလုံးရေ ၅၀၀၀ အထိ) အသုံးပြုလိုပါက VIP သို့ အဆင့်မြှင့်တင်ပါခင်ဗျာ။")
            : `Text length (${text.length}) exceeds your ${state.user?.is_vip ? "VIP" : "Free"} limit of ${maxChars} characters.`);
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
        const isVox = (state.selectedEngine === "voxcpm");
        const presetVoice = (isVox && state.voxcpmSubMode === "curated")
            ? state.selectedPersona
            : null;
        const targetModelSlug = isVox
            ? "voxcpm-cloud-ai"
            : (state.selectedApplioSlug || state.selectedModelSlug || "kartoon");

        if (state.selectedReferenceFile) {
            const formData = new FormData();
            formData.append("text", text);
            formData.append("model_slug", targetModelSlug);
            formData.append("speed", state.speed);
            formData.append("gender_preset", "m2m");
            formData.append("engine", state.selectedEngine);
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
                    engine: state.selectedEngine,
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
        alert(err.message);
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
                if (statusMsg) statusMsg.innerText = t("applio_status_waiting");
                if (etaText) etaText.innerText = "Laptop Worker ဖွင့်ထားပါက အလိုအလျောက် ပြီးစီးပါမည်";
                if (progressFill) progressFill.style.width = "35%";
            } else if (data.status === "QUEUED") {
                if (progressFill) progressFill.style.width = "45%";
                if (statusMsg) statusMsg.innerText = "⏳ Cloud Queue စောင့်ဆိုင်းနေပါသည်...";
            } else if (data.status === "RUNNING") {
                if (progressFill) progressFill.style.width = "80%";
                if (statusMsg) statusMsg.innerText = "⚡ အသံထွက်ပေါ်အောင် ပေါင်းစပ်နေပါသည်...";
            } else if (data.status === "SUCCESS") {
                clearInterval(state.pollTimer);
                haptic("heavy");
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
                alert("Generation failed: " + (data.error_message || "Unknown error"));
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

    // 1. Slider Controls for Original Blur and Subtitle Y
    if (sliderBlurY && previewBlur && valBlurY) {
        sliderBlurY.addEventListener("input", (e) => {
            const val = parseInt(e.target.value);
            valBlurY.innerText = `${val}%`;
            const bottomPct = Math.max(2, 100 - val - (sliderBlurH ? parseInt(sliderBlurH.value)/2 : 7));
            previewBlur.style.bottom = `${bottomPct}%`;
        });
    }

    if (sliderBlurH && previewBlur && valBlurH) {
        sliderBlurH.addEventListener("input", (e) => {
            const val = parseInt(e.target.value);
            valBlurH.innerText = `${val}%`;
            previewBlur.style.height = `${val}%`;
        });
    }

    if (sliderSubY && previewSub && valSubY) {
        sliderSubY.addEventListener("input", (e) => {
            const val = parseInt(e.target.value);
            valSubY.innerText = `${val}%`;
            const bottomPct = Math.max(4, 100 - val);
            previewSub.style.bottom = `${bottomPct}%`;
        });
    }

    // ── Interactive Drag & Drop / Manual Move on Canvas ──────────────────
    function enableVerticalDrag(boxElement, onUpdateY) {
        if (!boxElement || !canvas) return;

        let isDragging = false;

        function startDrag(clientY) {
            isDragging = true;
            boxElement.classList.add("is-dragging");
            haptic("light");
        }

        function moveDrag(clientY) {
            if (!isDragging) return;
            const canvasRect = canvas.getBoundingClientRect();
            if (canvasRect.height <= 0) return;

            // Calculate percentage from canvas top (0% top to 100% bottom)
            const relativeY = clientY - canvasRect.top;
            const rawPercent = (relativeY / canvasRect.height) * 100;
            const clampedY = Math.max(50, Math.min(95, Math.round(rawPercent)));
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
        if (valBlurY) valBlurY.innerText = `${y}%`;
        const bottomPct = Math.max(2, 100 - y - (sliderBlurH ? parseInt(sliderBlurH.value)/2 : 7));
        if (previewBlur) previewBlur.style.bottom = `${bottomPct}%`;
    });

    // Attach dragging to Subtitle Box
    enableVerticalDrag(previewSub, (y) => {
        if (sliderSubY) sliderSubY.value = y;
        if (valSubY) valSubY.innerText = `${y}%`;
        const bottomPct = Math.max(4, 100 - y);
        if (previewSub) previewSub.style.bottom = `${bottomPct}%`;
    });

    // ── Helper: Real-time Live Canvas Mockup Visual Updater ──
    function updateCanvasMockupVisuals() {
        if (!canvas) return;

        // 1. Aspect Ratio Frame Class
        if (mockupFrame) {
            let ratioClass = "ratio-9-16";
            if (state.recapRatio === "16:9") ratioClass = "ratio-16-9";
            else if (state.recapRatio === "1:1") ratioClass = "ratio-1-1";
            else if (state.recapRatio === "Original") ratioClass = "ratio-16-9";
            mockupFrame.className = `mockup-screen ${ratioClass}`;
        }

        // 2. Canvas Base Classes & Fill Mode
        const fillMode = state.recapRatioFill || "blur";
        canvas.className = `video-bg-placeholder fill-${fillMode} ${state.recapMirror ? 'mirrored' : ''}`;

        // 3. Fill Badge Status
        const fillBadge = document.getElementById("mockup-fill-badge");
        if (fillBadge) {
            if (fillMode === "blur") fillBadge.innerText = "🌫️ Blur Background";
            else if (fillMode === "crop") fillBadge.innerText = "✂️ Center Crop (Full Fill)";
            else if (fillMode === "black") fillBadge.innerText = "⬛ Black Bars";
        }

        // 4. Update Crop Shades & Inner Movie Height
        const shadeTop = document.getElementById("crop-shade-top");
        const shadeBottom = document.getElementById("crop-shade-bottom");
        const shadeLeft = document.getElementById("crop-shade-left");
        const shadeRight = document.getElementById("crop-shade-right");
        const innerMovie = document.getElementById("mockup-inner-movie");

        const cTop = state.recapCropTop || 0;
        const cBottom = state.recapCropBottom || 0;
        const cLeft = state.recapCropLeft || 0;
        const cRight = state.recapCropRight || 0;

        if (shadeTop) shadeTop.style.height = `${cTop}%`;
        if (shadeBottom) shadeBottom.style.height = `${cBottom}%`;
        if (shadeLeft) shadeLeft.style.width = `${cLeft}%`;
        if (shadeRight) shadeRight.style.width = `${cRight}%`;

        if (innerMovie && (fillMode === "blur" || fillMode === "black")) {
            if (state.recapRatio === "9:16") {
                // Base 16:9 height inside 9:16 canvas is 56.25%
                const totalVerticalCrop = Math.min(60, cTop + cBottom);
                const effectivePct = 56.25 * (1.0 - totalVerticalCrop / 100.0);
                innerMovie.style.height = `${Math.max(28, effectivePct)}%`;
            } else if (state.recapRatio === "1:1") {
                const totalVerticalCrop = Math.min(60, cTop + cBottom);
                const effectivePct = 68.0 * (1.0 - totalVerticalCrop / 100.0);
                innerMovie.style.height = `${Math.max(34, effectivePct)}%`;
            } else {
                innerMovie.style.height = "100%";
            }
        } else if (innerMovie) {
            innerMovie.style.height = "100%";
        }
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
    const cropPresetBtns = document.querySelectorAll("#recap-crop-presets .pill-btn");

    function setCropPresetActive(presetName) {
        cropPresetBtns.forEach(btn => {
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

    cropPresetBtns.forEach(btn => {
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

    // 4. Anti-Copyright Protection: Mirror Toggle
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

    // 5. Anti-Copyright Protection: Micro-Zoom (1.00, 1.06, 1.10)
    const zoomBtns = document.querySelectorAll("#recap-zoom-picker .pill-btn");
    zoomBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            zoomBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            state.recapZoom = parseFloat(btn.getAttribute("data-zoom") || "1.06");
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

    // 7. Narration Story Style Picker (viral, dramatic, funny, pov, standard)
    const storyPills = document.querySelectorAll("#story-style-picker .style-select-pill");
    storyPills.forEach(pill => {
        pill.addEventListener("click", () => {
            haptic("selection");
            storyPills.forEach(p => p.classList.remove("active"));
            pill.classList.add("active");
            state.recapStoryStyle = pill.getAttribute("data-story") || "viral";
        });
    });

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

    // 10. Font Size Picker
    const sizePills = document.querySelectorAll("#font-size-picker .pill-btn");
    sizePills.forEach(pill => {
        pill.addEventListener("click", () => {
            haptic("selection");
            sizePills.forEach(p => p.classList.remove("active"));
            pill.classList.add("active");
            state.recapFontSize = parseInt(pill.getAttribute("data-size") || "16");
            const previewText = document.getElementById("preview-sub-text-el");
            if (previewText) {
                previewText.style.fontSize = `${state.recapFontSize * 0.85}px`;
            }
        });
    });

    // 11. Subtitle Lines Picker
    const linePills = document.querySelectorAll("#sub-lines-picker .pill-btn");
    linePills.forEach(pill => {
        pill.addEventListener("click", () => {
            haptic("selection");
            linePills.forEach(p => p.classList.remove("active"));
            pill.classList.add("active");
            state.recapLines = parseInt(pill.getAttribute("data-lines") || "1");
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

    // 13. Dub Voice Selector
    const voiceSelect = document.getElementById("recap-voice-select");
    if (voiceSelect) {
        voiceSelect.addEventListener("change", (e) => {
            state.recapVoice = e.target.value;
        });
    }

    // 14. Analyze / Video Info Check Button
    const btnAnalyze = document.getElementById("btn-fetch-recap-info");
    const recapInfoBox = document.getElementById("recap-video-info-box");
    if (btnAnalyze) {
        btnAnalyze.addEventListener("click", async () => {
            const urlInput = document.getElementById("recap-url-input");
            const url = urlInput ? urlInput.value.trim() : "";
            if (!url) {
                alert(t("toast_empty_url"));
                return;
            }
            haptic("medium");
            btnAnalyze.disabled = true;
            const originalText = btnAnalyze.innerText;
            btnAnalyze.innerText = "⏳...";

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

                state.recapSourceDuration = data.duration_seconds || 0;
                const maxAllowed = state.user?.is_vip ? 600 : (state.user?.recap_max_duration_seconds || 120);
                const isOverLimit = (data.duration_seconds && data.duration_seconds > maxAllowed);
                if (durSpan) {
                    if (isOverLimit) {
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

                if (platformSpan) platformSpan.innerText = data.platform || "Video";
                if (subSpan) subSpan.innerText = data.has_subtitles ? "✅ Subtitle Ready" : "⚠️ AI Auto Sub";

                // Reflect thumbnail on live preview canvas
                if (canvas && data.thumbnail) {
                    canvas.style.backgroundImage = `url('${data.thumbnail}')`;
                    canvas.style.backgroundSize = "cover";
                    canvas.style.backgroundPosition = "center";
                }
            } catch (err) {
                alert(err.message || "Failed to inspect video link");
            } finally {
                btnAnalyze.disabled = false;
                btnAnalyze.innerText = originalText;
            }
        });
    }

    // 15. 1-Click Recap Action
    const btnRecap = document.getElementById("btn-start-recap");
    if (btnRecap) {
        btnRecap.addEventListener("click", handleStartRecap);
    }

    // Initial canvas mockup sync
    updateCanvasMockupVisuals();
}

async function handleStartRecap() {
    const urlInput = document.getElementById("recap-url-input");
    const url = urlInput ? urlInput.value.trim() : "";
    if (!url) {
        alert(t("toast_empty_url"));
        return;
    }

    haptic("heavy");
    const btn = document.getElementById("btn-start-recap");

    // Strictly enforce maximum duration: Free = 2 minutes (120s), VIP = 10 minutes (600s)
    const maxRecapSec = state.user?.is_vip ? 600 : (state.user?.recap_max_duration_seconds || 120);
    if (state.recapSourceDuration && state.recapSourceDuration > maxRecapSec) {
        const m = Math.floor(maxRecapSec / 60);
        alert(currentLang === "my"
            ? `ဗီဒီယို ကြာချိန်သည် ${state.user?.is_vip ? "VIP" : "အခမဲ့ (Free)"} အသုံးပြုသူ ကန့်သတ်ချက် (အများဆုံး ${m} မိနစ်) ထက် ကျော်လွန်နေပါသည်။ ` + (state.user?.is_vip ? "" : "ပိုရှည်သော ဗီဒီယိုများ (၁၀ မိနစ်အထိ) ပြုလုပ်လိုပါက VIP သို့ အဆင့်မြှင့်တင်ပါခင်ဗျာ။")
            : `Video duration exceeds your ${state.user?.is_vip ? "VIP" : "Free"} limit of ${m} minutes. Upgrade to VIP for up to 10 minutes.`);
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
    if (statusMsg) statusMsg.innerText = t("recap_processing_msg");
    if (stepText) stepText.innerText = t("queue_notice_recap");

    try {
        const sliderBlurY = document.getElementById("slider-blur-y");
        const sliderBlurH = document.getElementById("slider-blur-h");
        const sliderSubY = document.getElementById("slider-sub-y");

        const payload = {
            url: url,
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
            blur_y: sliderBlurY ? parseInt(sliderBlurY.value) : 81,
            blur_h: sliderBlurH ? parseInt(sliderBlurH.value) : 13,
            burn_subtitles: state.recapBurnSubs !== false,
            sub_y: sliderSubY ? parseInt(sliderSubY.value) : 85,
            sub_style: state.recapSubStyle || "yellow",
            sub_font_size: state.recapFontSize || 16,
            sub_lines: state.recapLines || 1,
            include_logo: state.recapLogo !== false,
            logo_pos: state.recapLogoPos || "top-right",
            duration_seconds: state.recapSourceDuration || null,
        };

        const res = await fetch("/api/recap/generate", {
            method: "POST",
            headers: getAuthHeaders(),
            body: JSON.stringify(payload),
        });

        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Recap creation failed");

        state.recapJobId = data.recap_id;
        loadUserProfile();
        startRecapJobPolling(data.recap_id);

    } catch (err) {
        alert(err.message);
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

            if (data.status === "SUCCESS") {
                clearInterval(state.recapPollTimer);
                state.recapPollTimer = null;
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

                if (btnVideo && data.download_urls?.video) btnVideo.href = data.download_urls.video;
                if (btnAudio && data.download_urls?.audio) btnAudio.href = data.download_urls.audio;
                if (btnSrt && data.download_urls?.srt) btnSrt.href = data.download_urls.srt;
                if (btnScript && data.download_urls?.script) btnScript.href = data.download_urls.script;

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
                if (btn) btn.disabled = false;
                if (progressBox) progressBox.classList.add("hidden");
                alert(`❌ Recap Generation Failed: ${data.error_message || "Processing error"}\n\nCredit has been refunded to your account.`);
                loadUserProfile();
            }
        } catch (err) {
            console.warn("Recap polling error:", err);
        }
    }, 2000);
}

// ── Downloader Tab ─────────────────────────────────────────────────────────
function setupDownloader() {
    const btnDl = document.getElementById("btn-extract-dl");
    const resultCard = document.getElementById("dl-result-card");
    if (btnDl && resultCard) {
        btnDl.addEventListener("click", async () => {
            const urlInput = document.getElementById("dl-url-input");
            const url = urlInput ? urlInput.value.trim() : "";
            if (!url) {
                alert(t("toast_empty_url"));
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
                resultCard.classList.remove("hidden");
            } catch (err) {
                alert(err.message);
            } finally {
                btnDl.disabled = false;
            }
        });
    }
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
            alert(`${numToCopy}\n${t("toast_copied")}`);
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
        alert(t("toast_empty_text") || "Voice Job ID not found");
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
        alert(t("toast_telegram_sent"));
    } catch (err) {
        alert(err.message || "Failed to send to Telegram");
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
        alert(t("toast_empty_url") || "Recap Job ID not found");
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
        alert(t("toast_telegram_sent"));
    } catch (err) {
        alert(err.message || "Failed to send to Telegram");
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
        alert("ကျေးဇူးပြု၍ ငွေလွှဲပြေစာ ပုံကို ရွေးချယ်ပေးပါခင်ဗျာ။");
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

        alert(t("toast_success_slip"));
        closePaymentDrawer();
        if (fileInput) fileInput.value = "";
        const previewBox = document.getElementById("slip-preview-box");
        if (previewBox) previewBox.classList.add("hidden");
    } catch (e) {
        alert(e.message);
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
            if (elQueue) elQueue.innerText = stats.queued_jobs !== undefined ? stats.queued_jobs : stats.queued_applio_jobs;
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
            }
        }
        // Load 24h global history
        loadAdminHistory();
    } catch (e) {
        console.warn("Error loading admin data:", e);
    }
}

function openSlipModal(url) {
    const modal = document.getElementById("slip-modal");
    const img = document.getElementById("modal-slip-img");
    if (modal && img) {
        img.src = url;
        modal.classList.remove("hidden");
    }
}

function closeSlipModal() {
    const modal = document.getElementById("slip-modal");
    if (modal) modal.classList.add("hidden");
}

async function adminApproveSlip(reqId) {
    haptic("medium");
    if (!confirm("အတည်ပြုပြီး VIP သို့ မြှင့်တင်ပေးမည်လား?")) return;
    try {
        const res = await fetch("/api/admin/approve-slip", {
            method: "POST",
            headers: getAuthHeaders(),
            body: JSON.stringify({ request_id: reqId }),
        });
        const data = await res.json();
        alert(data.message);
        loadAdminData();
    } catch (e) {
        alert(e.message);
    }
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
        alert(data.message);
        loadAdminData();
    } catch (e) {
        alert(e.message);
    }
}

async function adminGrantVip() {
    const input = document.getElementById("adm-target-user-id");
    const uid = input ? parseInt(input.value) : 0;
    if (!uid) {
        alert("Telegram ID ထည့်ပါ");
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
        alert(data.message);
        loadAdminData();
    } catch (e) {
        alert(e.message);
    }
}

async function adminAddMinutes() {
    const input = document.getElementById("adm-target-user-id");
    const uid = input ? parseInt(input.value) : 0;
    if (!uid) {
        alert("Telegram ID ထည့်ပါ");
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
        alert(data.message);
        loadAdminData();
    } catch (e) {
        alert(e.message);
    }
}

async function adminAddRecapCredits() {
    const input = document.getElementById("adm-target-user-id");
    const uid = input ? parseInt(input.value) : 0;
    if (!uid) {
        alert("Telegram ID ထည့်ပါ");
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
        alert(data.message);
        loadAdminData();
    } catch (e) {
        alert(e.message);
    }
}

async function adminSendBroadcast() {
    const textEl = document.getElementById("adm-broadcast-text");
    const btn = document.getElementById("adm-broadcast-btn");
    const btnText = document.getElementById("adm-broadcast-btn-text");
    const reportBox = document.getElementById("adm-broadcast-report");

    const msg = textEl ? textEl.value.trim() : "";
    if (!msg) {
        alert(currentLang === "my" ? "မက်ဆေ့ခ်ျ စာသား ရိုက်ထည့်ပေးပါ" : "Please enter broadcast message text");
        return;
    }
    haptic("heavy");
    if (!confirm(currentLang === "my" 
        ? "အသုံးပြုသူ အားလုံးထံသို့ ဤ Broadcast မက်ဆေ့ခ်ျ ပို့မှာ သေချာပါသလား?" 
        : "Are you sure you want to broadcast this message to all users?")) return;

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

        alert(isMy 
            ? `📢 Broadcast ပို့ဆောင်မှု ပြီးဆုံးပါပြီ!\n\n• စုစုပေါင်း: ${data.total_users_in_db} ဦး\n• အောင်မြင်: ${data.sent_count} ဦး\n• မရောက်ရှိ: ${data.failed_count} ဦး\n\nအသေးစိတ် အချက်အလက်များကို Admin Panel ရှိ Report တွင် ဖော်ပြထားပါသည်။` 
            : `📢 Broadcast Completed!\n\n• Total Users: ${data.total_users_in_db}\n• Delivered: ${data.sent_count}\n• Failed: ${data.failed_count}\n\nDetailed breakdown displayed in report box.`);

        if (textEl) textEl.value = "";
    } catch (e) {
        if (reportBox) {
            reportBox.innerHTML = `<div style="color: #ef4444; padding: 10px;">❌ ${e.message}</div>`;
        }
        alert(e.message);
    } finally {
        if (btn) btn.disabled = false;
        if (btnText) btnText.textContent = currentLang === "my" ? "Broadcast ပို့မည်" : "Send Broadcast";
    }
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

        // 1. Update Worker Heartbeat Pill
        const workerDot = document.getElementById("adm-worker-dot");
        const workerText = document.getElementById("adm-worker-status-text");

        if (data.worker) {
            const w = data.worker;
            if (workerDot) {
                workerDot.className = "pulsing-dot " + (w.status_code ? w.status_code.toLowerCase() : "offline");
            }
            if (workerText) {
                workerText.innerText = `Local Worker: ${w.status_text} (Engine: ${w.engine})`;
            }
        }

        // 2. Update Counts
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

        // 3. Render High-Visibility Active Queue Status Banner
        if (bannerEl) {
            const hostName = data.worker?.host || "Awin";
            const engineMode = (data.worker?.engine || "ALL").toUpperCase();
            const isOnline = data.worker?.status_code === "ONLINE";

            if (c.queued === 0 && c.running === 0) {
                bannerEl.innerHTML = `
                    <div class="active-queue-status-banner idle">
                        <div class="status-banner-icon">🟢</div>
                        <div class="status-banner-info">
                            <div class="status-banner-title">${t("admin_queue_idle_title")}</div>
                            <div class="status-banner-desc">${t("admin_queue_idle_desc")}</div>
                            <div class="status-banner-meta">
                                <span class="meta-pill">💻 Worker Host: <strong>${escapeHtml(hostName)}</strong></span>
                                <span class="meta-pill">⚙️ Engine: <strong>${escapeHtml(engineMode)}</strong></span>
                                <span class="meta-pill">📡 Status: <strong class="${isOnline ? 'text-green' : 'text-danger'}">${isOnline ? 'ONLINE (STANDBY)' : 'OFFLINE'}</strong></span>
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
                            <div class="status-banner-desc">Local Worker (${escapeHtml(hostName)}) is actively processing <strong>${c.running}</strong> job(s) right now.</div>
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
                            <div class="status-banner-desc">${c.queued} job(s) waiting in queue. Worker is polling and claiming atomically.</div>
                        </div>
                    </div>
                `;
            }
        }

        // 4. Render Jobs with Clear Visual Grouping
        const jobs = data.jobs || [];
        if (jobs.length === 0) {
            container.innerHTML = `<p class="text-muted" style="padding: 12px 0;">${t("admin_queue_empty")}</p>`;
            return;
        }

        const renderJobCard = (j) => {
            const statusLower = (j.status || "queued").toLowerCase();
            const engineLower = (j.engine || "voxcpm").toLowerCase();
            const engineLabel = engineLower === "applio" ? "🎙️ APPLIO GPU" : (engineLower === "recap" ? "🎬 RECAP" : "☁️ VOXCPM AI");
            
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

            if (j.download_url) {
                actionsHtml += `<a href="${j.download_url}" target="_blank" class="queue-act-btn uid">📥 View</a>`;
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

async function adminRetryJob(jobId) {
    haptic("medium");
    try {
        const res = await fetch(`/api/admin/queue/${jobId}/retry`, {
            method: "POST",
            headers: getAuthHeaders(),
        });
        const data = await res.json();
        if (data.success) {
            loadAdminQueue();
        } else {
            alert(data.message || "Failed to retry job");
        }
    } catch (e) {
        alert("Retry error: " + e.message);
    }
}

async function adminCancelJob(jobId) {
    if (!confirm("Are you sure you want to cancel this job?")) return;
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
            alert(data.message || "Failed to cancel job");
        }
    } catch (e) {
        alert("Cancel error: " + e.message);
    }
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

        if (elKpayNum) elKpayNum.value = s.payment_kpay_number || "";
        if (elKpayName) elKpayName.value = s.payment_kpay_name || "";
        if (elWaveNum) elWaveNum.value = s.payment_wave_number || "";
        if (elWaveName) elWaveName.value = s.payment_wave_name || "";
        if (elTutLink) elTutLink.value = s.tutorial_video_link || "";
        if (s.tutorial_video_link) currentTutorialUrl = s.tutorial_video_link;
        if (elMaint) elMaint.checked = !!s.maintenance_mode;
    } catch (e) {
        console.warn("Could not load dynamic settings:", e);
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

    const payload = {
        payment_kpay_number: kpayNum,
        payment_kpay_name: kpayName,
        payment_wave_number: waveNum,
        payment_wave_name: waveName,
        tutorial_video_link: tutLink,
        maintenance_mode: maint,
    };
    if (tutLink) currentTutorialUrl = tutLink;

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
        alert(data.message || "Settings updated successfully!");
        loadAdminSettings();
    } catch (e) {
        alert("Failed to save settings: " + e.message);
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
                            <span class="history-item-badge badge-voice">🎙️ ${escapeHtml(j.engine.toUpperCase())} • ${escapeHtml(j.preset_voice)}</span>
                            <span class="history-item-badge status-${j.status.toLowerCase()}">${j.status}</span>
                        </div>
                        <div class="history-item-body">"${escapeHtml(j.input_snippet || "Voice Synthesis")}"</div>
                        <div class="history-item-footer">
                            <span>⏱️ ${j.time_ago}</span>
                            <div class="history-btn-row">
                                ${j.download_url ? `<a href="${j.download_url}" download="voice_${j.id.slice(0,8)}.wav" class="history-dl-link">📥 Download</a> <button type="button" onclick="sendVoiceToTelegram('${j.id}')" class="history-dl-link tg-btn" title="Telegram သို့ ပို့မည်">📱 Telegram</button>` : ''}
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
                        <div class="history-item-footer">
                            <span>⏱️ ${j.time_ago}</span>
                            <div class="history-btn-row">
                                ${j.deliverables?.video ? `<a href="${j.deliverables.video}" download="recap_${j.id.slice(0,8)}.mp4" class="history-dl-link">🎬 Video</a> <button type="button" onclick="sendRecapToTelegram('${j.id}')" class="history-dl-link tg-btn" title="Telegram သို့ ပို့မည်">📱 Telegram</button>` : ''}
                                ${j.deliverables?.audio ? `<a href="${j.deliverables.audio}" download="audio_${j.id.slice(0,8)}.mp3" class="history-dl-link">🎙️ Audio</a>` : ''}
                                ${j.deliverables?.srt ? `<a href="${j.deliverables.srt}" download="sub_${j.id.slice(0,8)}.srt" class="history-dl-link">📝 SRT</a>` : ''}
                                ${j.deliverables?.script ? `<a href="${j.deliverables.script}" download="script_${j.id.slice(0,8)}.txt" class="history-dl-link">📜 TXT</a>` : ''}
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

async function loadAdminHistory() {
    try {
        const res = await fetch("/api/admin/history", {
            headers: getAuthHeaders(),
        });
        if (!res.ok) return;

        const data = await res.json();
        const elTotal = document.getElementById("adm-hist-total");
        const elRecap = document.getElementById("adm-hist-recap");
        const elVoice = document.getElementById("adm-hist-voice");
        const container = document.getElementById("admin-global-history-list");

        if (elTotal) elTotal.innerText = data.total_24h || 0;
        if (elRecap) elRecap.innerText = data.total_recap || 0;
        if (elVoice) elVoice.innerText = data.total_voice || 0;

        const history = data.history || [];
        if (container) {
            if (history.length === 0) {
                container.innerHTML = `<p class="text-muted" data-i18n="history_empty">${t("history_empty")}</p>`;
            } else {
                container.innerHTML = history.map(j => `
                    <div class="history-item">
                        <div class="history-item-header">
                            <div>
                                <span class="history-item-badge ${j.job_type === 'RECAP' ? 'badge-recap' : 'badge-voice'}">
                                    ${j.job_type === 'RECAP' ? '🎬 RECAP' : '🎙️ VOICE'}
                                </span>
                                <span class="mini-tag" style="margin-left: 4px;">👤 ${escapeHtml(j.username ? '@' + j.username : 'ID: ' + j.user_id)}</span>
                            </div>
                            <span class="history-item-badge status-${j.status.toLowerCase()}">${j.status}</span>
                        </div>
                        <div class="history-item-body">
                            <strong>${escapeHtml(j.preset_voice || j.engine)}:</strong> ${escapeHtml(j.input_snippet || "Task")}
                        </div>
                        <div class="history-item-footer">
                            <span>⏱️ ${j.time_ago} (${j.id.slice(0,10)})</span>
                            <div class="history-btn-row">
                                ${j.download_url ? `<a href="${j.download_url}" target="_blank" class="history-dl-link">🎬 Preview / File</a>` : ''}
                            </div>
                        </div>
                    </div>
                `).join("");
            }
        }
    } catch (e) {
        console.warn("Could not load admin history:", e);
    }
}

async function adminTriggerPurge24h() {
    if (!confirm("၂၄ နာရီကျော်လွန်နေသော အသံနှင့် ဗီဒီယို ဖိုင်များအားလုံးကို ရှင်းလင်းဖျက်သိမ်းမည်မှာ သေချာပါသလား?")) return;
    haptic("heavy");
    try {
        const res = await fetch("/api/admin/cleanup-24h", {
            method: "POST",
            headers: getAuthHeaders(),
        });
        const data = await res.json();
        alert(data.message || `Cleaned ${data.purged_count} files`);
        loadAdminHistory();
    } catch (e) {
        alert("Cleanup error: " + e.message);
    }
}

// ── Deep Link Handling (Telegram WebApp & URL Query) ──────────────────────
async function checkStartupDeepLinks() {
    try {
        const urlParams = new URLSearchParams(window.location.search);
        let jobId = urlParams.get("job_id");
        let recapJobId = urlParams.get("recap_job_id");

        if (state.tg?.initDataUnsafe?.start_param) {
            const sp = state.tg.initDataUnsafe.start_param;
            if (sp.startsWith("job_")) jobId = sp.replace("job_", "");
            if (sp.startsWith("recap_")) recapJobId = sp.replace("recap_", "");
        }

        if (jobId) {
            switchTab("tab-voice");
            const res = await fetch(`/api/jobs/${jobId}/status`, { headers: getAuthHeaders() });
            if (res.ok) {
                const data = await res.json();
                if (data.status === "SUCCESS") {
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
            switchTab("tab-recap");
            const res = await fetch(`/api/recap/${recapJobId}/status`, { headers: getAuthHeaders() });
            if (res.ok) {
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
                    }
                    if (btnVideo && data.download_urls?.video) btnVideo.href = data.download_urls.video;
                    if (btnAudio && data.download_urls?.audio) btnAudio.href = data.download_urls.audio;
                    if (btnSrt && data.download_urls?.srt) btnSrt.href = data.download_urls.srt;
                    if (btnScript && data.download_urls?.script) btnScript.href = data.download_urls.script;
                    if (btnTg) {
                        btnTg.disabled = false;
                        btnTg.classList.remove("sent");
                        btnTg.innerHTML = `<span class="btn-icon">📱</span> <span>${t("btn_send_recap_telegram")}</span>`;
                    }
                    showFloatingAlert(
                        t("alert_recap_ready") || "🎉 Pro Movie Recap အဆင်သင့်ဖြစ်ပါပြီ!",
                        currentLang === "my" ? "Full HD Video ကို တိုက်ရိုက်ဖွင့်ကြည့်နိုင်ပါပြီ။" : "Your recap video is ready to watch.",
                        () => {
                            if (resultBox) resultBox.scrollIntoView({ behavior: "smooth" });
                        },
                        currentLang === "my" ? "🎬 ကြည့်ရှုမည်" : "🎬 View Video"
                    );
                }
            }
        }
    } catch (e) {
        console.warn("Deep link handling error:", e);
    }
}

// ── App Startup ────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
    initTelegram();
    setLanguage(currentLang);
    setupTabs();
    setupVoiceStudio();
    setupRecapVisualEditor();
    setupDownloader();
    setupSlipPreview();

    loadUserProfile();
    loadCuratedVoices();
    loadApplioModels();
    loadUserHistory();
    checkStartupDeepLinks();
});

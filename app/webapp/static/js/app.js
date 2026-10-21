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
            charCount.innerText = `0 / ${data.character_limit} chars`;
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

function setupVoiceStudio() {
    const textInput = document.getElementById("voice-text-input");
    const charCount = document.getElementById("char-count");

    if (textInput && charCount) {
        textInput.addEventListener("input", () => {
            const len = textInput.value.length;
            const max = state.user?.character_limit || 500;
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
                if (resultBox) resultBox.classList.remove("hidden");
                if (btnGen) btnGen.disabled = false;

                if (audioPlayer && data.output_audio_url) {
                    audioPlayer.src = data.output_audio_url;
                    audioPlayer.play().catch(() => {});
                }
                if (btnDl && data.output_audio_url) {
                    btnDl.href = data.output_audio_url;
                }
                loadUserProfile();
                loadUserHistory();

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

    // 2. Aspect Ratio Selector (9:16, 16:9, 1:1, Original)
    const ratioBtns = document.querySelectorAll("#recap-ratio-picker .ratio-btn");
    ratioBtns.forEach(btn => {
        btn.addEventListener("click", () => {
            haptic("selection");
            ratioBtns.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            state.recapRatio = btn.getAttribute("data-ratio") || "9:16";

            if (mockupFrame) {
                let ratioClass = "ratio-9-16";
                if (state.recapRatio === "16:9") ratioClass = "ratio-16-9";
                else if (state.recapRatio === "1:1") ratioClass = "ratio-1-1";
                else if (state.recapRatio === "Original") ratioClass = "ratio-16-9";
                mockupFrame.className = `mockup-screen ${ratioClass}`;
            }
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
                const isOverLimit = (data.duration_seconds && data.duration_seconds > 600);
                if (durSpan) {
                    if (isOverLimit) {
                        durSpan.className = "mini-tag text-danger";
                        durSpan.innerHTML = `⏱️ ${data.estimated_duration || "00:00"} <span style="color:#ef4444; font-weight:700; margin-left:4px;">(${t("recap_duration_warning")})</span>`;
                    } else {
                        durSpan.className = "mini-tag";
                        durSpan.innerHTML = `⏱️ ${data.estimated_duration || "00:00"} <span style="color:#10b981; font-weight:700; margin-left:4px;">(${t("recap_duration_valid")})</span>`;
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

    // Strictly enforce 10-minute maximum duration per recap credit
    if (state.recapSourceDuration && state.recapSourceDuration > 600) {
        alert(t("recap_duration_warning"));
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

function openPaymentDrawer(packageCode, amount, title) {
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
}

function closePaymentDrawer() {
    const drawer = document.getElementById("payment-box");
    if (drawer) drawer.classList.add("hidden");
}

function copyAccount(accNum) {
    haptic("medium");
    navigator.clipboard.writeText(accNum).then(() => {
        alert(t("toast_copied"));
    }).catch(() => {
        prompt("Account:", accNum);
    });
}

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
            if (elQueue) elQueue.innerText = stats.queued_applio_jobs;
        }

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
    const msg = textEl ? textEl.value.trim() : "";
    if (!msg) {
        alert("စာသား ရိုက်ထည့်ပါ");
        return;
    }
    haptic("heavy");
    if (!confirm("အသုံးပြုသူ အားလုံးထံ ဤ Broadcast စာကို ပေးပို့မည်လား?")) return;
    try {
        const res = await fetch("/api/admin/broadcast", {
            method: "POST",
            headers: getAuthHeaders(),
            body: JSON.stringify({ message: msg }),
        });
        const data = await res.json();
        alert(`Broadcast ပို့ဆောင်ပြီးပါပြီ! (အောင်မြင်သူ: ${data.sent_count || 0})`);
        if (textEl) textEl.value = "";
    } catch (e) {
        alert(e.message);
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
                                ${j.download_url ? `<a href="${j.download_url}" download="voice_${j.id.slice(0,8)}.wav" class="history-dl-link">📥 Play / Download</a>` : ''}
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
                                ${j.deliverables?.video ? `<a href="${j.deliverables.video}" download="recap_${j.id.slice(0,8)}.mp4" class="history-dl-link">🎬 Video</a>` : ''}
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
});

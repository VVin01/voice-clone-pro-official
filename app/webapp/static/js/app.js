/**
 * app.js - Unified Telegram Mini App Client Logic
 * Voice Clone Pro & AI Movie Recap Studio
 */

// ── State ──────────────────────────────────────────────────────────────────
const state = {
    tg: window.Telegram?.WebApp || null,
    user: null,
    models: [],
    selectedModelSlug: "voxcpm-cloud-ai",
    selectedEngine: "voxcpm",
    speed: "normal",
    activeJobId: null,
    pollTimer: null,
};

// ── Telegram WebApp Initialization ─────────────────────────────────────────
function initTelegram() {
    if (state.tg) {
        state.tg.ready();
        state.tg.expand();
        // Set header color to dark
        try {
            state.tg.setHeaderColor("#0a0d14");
            state.tg.setBackgroundColor("#0a0d14");
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

// ── Navigation (Tabs) ───────────────────────────────────────────────────────
function setupTabs() {
    const navButtons = document.querySelectorAll(".nav-item");
    navButtons.forEach(btn => {
        btn.addEventListener("click", () => {
            const targetTab = btn.getAttribute("data-tab");
            haptic("light");

            // Update bottom bar active state
            navButtons.forEach(b => b.classList.remove("active"));
            btn.classList.add("active");

            // Switch visible view pane
            document.querySelectorAll(".tab-pane").forEach(pane => {
                pane.classList.remove("active");
            });
            const activePane = document.getElementById(targetTab);
            if (activePane) activePane.classList.add("active");
        });
    });
}

// ── API: Load User Profile ─────────────────────────────────────────────────
async function loadUserProfile() {
    try {
        const res = await fetch("/api/user/me", { headers: getAuthHeaders() });
        if (!res.ok) throw new Error("Could not load user profile");
        const data = await res.json();
        state.user = data;

        // Update UI Badges
        const vipBadge = document.getElementById("vip-status-badge");
        const balText = document.getElementById("user-balance-text");
        const catTag = document.getElementById("profile-category-tag");
        const balMin = document.getElementById("profile-balance-min");
        const charCount = document.getElementById("char-count");

        if (vipBadge) {
            vipBadge.className = data.is_vip ? "vip-badge vip" : "vip-badge free";
            vipBadge.innerText = data.is_vip ? "👑 VIP" : "FREE";
        }
        if (balText) {
            balText.innerText = data.is_vip ? "⏱️ Unlimited" : `⏱️ ${data.balance_minutes}m`;
        }
        if (catTag) {
            catTag.innerText = data.is_vip ? "👑 PURCHASED VIP" : "FREE MEMBER";
            catTag.className = data.is_vip ? "stat-val text-gold" : "stat-val";
        }
        if (balMin) {
            balMin.innerText = data.is_vip ? "Unlimited" : `${data.balance_minutes} mins`;
        }
        if (charCount) {
            charCount.innerText = `0 / ${data.character_limit} chars`;
        }

    } catch (err) {
        console.warn("User profile fetch error:", err);
    }
}

// ── API: Load Voice Models ─────────────────────────────────────────────────
async function loadVoiceModels() {
    const listEl = document.getElementById("models-list");
    if (!listEl) return;

    try {
        const res = await fetch("/api/voices/models", { headers: getAuthHeaders() });
        if (!res.ok) return;
        const models = await res.json();
        state.models = models;

        const countTag = document.getElementById("models-count");
        if (countTag) countTag.innerText = `${models.length} Models`;

        listEl.innerHTML = "";
        models.forEach((m, idx) => {
            const chip = document.createElement("div");
            chip.className = `model-chip ${m.slug === state.selectedModelSlug ? "active" : ""}`;
            chip.setAttribute("data-slug", m.slug);
            chip.innerHTML = `
                <span class="chip-avatar">${m.is_voxcpm ? "🎙️" : "🎭"}</span>
                <div class="chip-info">
                    <span class="chip-name">${m.name}</span>
                    <span class="chip-type">${m.is_voxcpm ? "Cloud AI" : "Clone Model"}</span>
                </div>
                <span class="chip-check">✓</span>
            `;

            chip.addEventListener("click", () => {
                haptic("selection");
                document.querySelectorAll(".model-chip").forEach(c => c.classList.remove("active"));
                chip.classList.add("active");
                state.selectedModelSlug = m.slug;
            });

            listEl.appendChild(chip);
        });

    } catch (err) {
        console.warn("Could not load models:", err);
    }
}

// ── Voice Studio Controls ──────────────────────────────────────────────────
function setupVoiceStudio() {
    const textInput = document.getElementById("voice-text-input");
    const charCount = document.getElementById("char-count");

    if (textInput && charCount) {
        textInput.addEventListener("input", () => {
            const len = textInput.value.length;
            const max = state.user?.character_limit || 500;
            charCount.innerText = `${len} / ${max} chars`;
            if (len > max) {
                charCount.style.color = "#ef4444";
            } else {
                charCount.style.color = "var(--text-muted)";
            }
        });
    }

    // Engine Selector Buttons
    const btnVox = document.getElementById("btn-engine-voxcpm");
    const btnApplio = document.getElementById("btn-engine-applio");
    if (btnVox && btnApplio) {
        btnVox.addEventListener("click", () => {
            haptic("selection");
            btnVox.classList.add("active");
            btnApplio.classList.remove("active");
            state.selectedEngine = "voxcpm";
            state.selectedModelSlug = "voxcpm-cloud-ai";
            updateActiveModelChip("voxcpm-cloud-ai");
        });
        btnApplio.addEventListener("click", () => {
            haptic("selection");
            btnApplio.classList.add("active");
            btnVox.classList.remove("active");
            state.selectedEngine = "applio";
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

function updateActiveModelChip(slug) {
    document.querySelectorAll(".model-chip").forEach(chip => {
        if (chip.getAttribute("data-slug") === slug) {
            chip.classList.add("active");
        } else {
            chip.classList.remove("active");
        }
    });
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
        alert("ကျေးဇူးပြု၍ စာသား ရိုက်ထည့်ပေးပါခင်ဗျာ။");
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
        const res = await fetch("/api/voices/generate", {
            method: "POST",
            headers: getAuthHeaders(),
            body: JSON.stringify({
                text: text,
                model_slug: state.selectedModelSlug,
                speed: state.speed,
                engine: state.selectedEngine,
            }),
        });

        const data = await res.json();
        if (!res.ok) {
            throw new Error(data.detail || "Voice generation request failed");
        }

        state.activeJobId = data.job_id;
        if (statusMsg) statusMsg.innerText = "⏳ စတင်လုပ်ဆောင်နေပါသည်...";
        if (progressFill) progressFill.style.width = "40%";
        if (etaText) etaText.innerText = data.is_voxcpm ? "VoxCPM Cloud Queue စစ်ဆေးနေပါသည်..." : "AI ပြင်ဆင်နေပါသည်...";

        // Start Polling for Status
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

            if (data.status === "RUNNING") {
                if (progressFill) progressFill.style.width = "75%";
                if (statusMsg) statusMsg.innerText = "⚡ အသံထွက်ပေါ်အောင် ပေါင်းစပ်နေပါသည်...";
                if (etaText) etaText.innerText = "ခန့်မှန်းကြာချိန် ~5s - 15s";
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
                // Refresh balance
                loadUserProfile();

            } else if (data.status === "FAILED") {
                clearInterval(state.pollTimer);
                alert("အသံထုတ်ယူခြင်း မအောင်မြင်ပါ: " + (data.error_message || "Unknown error"));
                if (progressBox) progressBox.classList.add("hidden");
                if (btnGen) btnGen.disabled = false;
            }

            if (tick > 60) { // 2 minutes timeout
                clearInterval(state.pollTimer);
                if (btnGen) btnGen.disabled = false;
            }

        } catch (e) {
            console.error(e);
        }
    }, 2000);
}

// ── TAB 2: Visual Mockup Slider Synchronizer ───────────────────────────────
function setupRecapVisualEditor() {
    const sliderBlurY = document.getElementById("slider-blur-y");
    const sliderBlurH = document.getElementById("slider-blur-h");
    const sliderSubY = document.getElementById("slider-sub-y");

    const valBlurY = document.getElementById("val-blur-y");
    const valBlurH = document.getElementById("val-blur-h");
    const valSubY = document.getElementById("val-sub-y");

    const previewBlur = document.getElementById("preview-blur-box");
    const previewSub = document.getElementById("preview-sub-box");

    if (sliderBlurY && previewBlur && valBlurY) {
        sliderBlurY.addEventListener("input", (e) => {
            const val = parseInt(e.target.value);
            valBlurY.innerText = `${val}%`;
            // Calculate bottom coordinate: Y=81 means 100-81 = 19% from bottom
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

    // Subtitle Style Picker
    const styleChips = document.querySelectorAll("#sub-style-picker .style-chip");
    styleChips.forEach(chip => {
        chip.addEventListener("click", () => {
            haptic("selection");
            styleChips.forEach(c => c.classList.remove("active"));
            chip.classList.add("active");
            const style = chip.getAttribute("data-style");
            if (previewSub) {
                previewSub.className = `preview-sub-box ${style}`;
            }
        });
    });

    // 1-Click Recap Action
    const btnRecap = document.getElementById("btn-start-recap");
    if (btnRecap) {
        btnRecap.addEventListener("click", () => {
            const urlInput = document.getElementById("recap-url-input");
            const url = urlInput ? urlInput.value.trim() : "";
            if (!url) {
                alert("YouTube သို့မဟုတ် TikTok Link ထည့်သွင်းပေးပါခင်ဗျာ။");
                return;
            }
            haptic("heavy");
            alert("🚀 1-Click Pro Recap အမိန့်ကို လက်ခံရရှိပါပြီ!\nBot မှ ဗီဒီယိုကို ရယူပြီး အချိန်ကိုက် စာတန်းထိုးနှင့် အသံသွင်းခြင်း စတင်နေပါပြီခင်ဗျာ။");
        });
    }
}

// ── Downloader Tab ─────────────────────────────────────────────────────────
function setupDownloader() {
    const btnDl = document.getElementById("btn-extract-dl");
    const resultCard = document.getElementById("dl-result-card");
    if (btnDl && resultCard) {
        btnDl.addEventListener("click", () => {
            const urlInput = document.getElementById("dl-url-input");
            const url = urlInput ? urlInput.value.trim() : "";
            if (!url) {
                alert("ဒေါင်းလုဒ် ရယူလိုသော Link ကို ထည့်သွင်းပေးပါခင်ဗျာ။");
                return;
            }
            haptic("medium");
            resultCard.classList.remove("hidden");
        });
    }
}

// ── Payment Drawer Helpers ─────────────────────────────────────────────────
function openPaymentDrawer(planName, price) {
    haptic("light");
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
        alert(`Account Number ${accNum} ကို ကူးယူပြီးပါပြီခင်ဗျာ!`);
    }).catch(() => {
        prompt("Account Number:", accNum);
    });
}

// ── App Startup ────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
    initTelegram();
    setupTabs();
    setupVoiceStudio();
    setupRecapVisualEditor();
    setupDownloader();

    loadUserProfile();
    loadVoiceModels();
});

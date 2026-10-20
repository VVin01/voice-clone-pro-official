/**
 * app.js - Unified Telegram Mini App Client Logic
 * Voice Clone Pro & AI Movie Recap Studio
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
    selectedReferenceFile: null,
    speed: "normal",
    activeJobId: null,
    pollTimer: null,
};

// ── Telegram WebApp Initialization ─────────────────────────────────────────
function initTelegram() {
    if (state.tg) {
        state.tg.ready();
        state.tg.expand();
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
window.getAuthHeaders = getAuthHeaders;

// ── Language Toggle (i18n) ─────────────────────────────────────────────────
function toggleLanguage() {
    haptic("medium");
    const nextLang = currentLang === "my" ? "en" : "my";
    setLanguage(nextLang);
    loadCuratedVoices();
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
        const balText = document.getElementById("user-balance-text");
        const catTag = document.getElementById("profile-category-tag");
        const balMin = document.getElementById("profile-balance-min");
        const charCount = document.getElementById("char-count");
        const adminNavBtn = document.getElementById("nav-btn-admin");
        const adminTab = document.getElementById("tab-admin");

        if (vipBadge) {
            vipBadge.className = data.is_vip ? "vip-badge vip" : "vip-badge free";
            vipBadge.innerText = data.is_vip ? t("vip_purchased") : t("vip_free");
        }
        if (balText) {
            balText.innerText = data.is_vip ? `⏱️ ${t("unlimited")}` : `⏱️ ${data.balance_minutes}m`;
        }
        if (catTag) {
            catTag.innerText = data.is_vip ? t("vip_purchased") : t("vip_free");
            catTag.className = data.is_vip ? "stat-val text-gold" : "stat-val";
        }
        if (balMin) {
            balMin.innerText = data.is_vip ? t("unlimited") : `${data.balance_minutes} mins`;
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

// ── API: Curated Voices (Gemini Personas) ──────────────────────────────────
async function loadCuratedVoices() {
    const grid = document.getElementById("personas-grid");
    if (!grid) return;

    try {
        const res = await fetch("/api/voices/curated", { headers: getAuthHeaders() });
        if (!res.ok) return;
        const voices = await res.json();
        state.curatedVoices = voices;

        grid.innerHTML = "";
        voices.forEach(v => {
            const card = document.createElement("div");
            card.className = `persona-card ${v.id === state.selectedPersona ? "active" : ""}`;
            card.setAttribute("data-voice-id", v.id);
            card.innerHTML = `
                <div class="persona-header">
                    <span class="persona-avatar">${v.avatar}</span>
                    <span class="persona-name">${v.name}</span>
                </div>
                <span class="persona-badge">${v.badge}</span>
                <span class="persona-desc">${v.description}</span>
            `;

            card.addEventListener("click", () => {
                haptic("selection");
                document.querySelectorAll(".persona-card").forEach(c => c.classList.remove("active"));
                card.classList.add("active");
                state.selectedPersona = v.id;
            });

            grid.appendChild(card);
        });
    } catch (e) {
        console.warn("Could not load curated voices:", e);
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

    if (btnVox && btnApplio) {
        btnVox.addEventListener("click", () => {
            haptic("selection");
            btnVox.classList.add("active");
            btnApplio.classList.remove("active");
            state.selectedEngine = "voxcpm";
            state.selectedModelSlug = "voxcpm-cloud-ai";
            if (voxModeSection) voxModeSection.classList.remove("hidden");
            if (applioSection) applioSection.classList.add("hidden");
        });
        btnApplio.addEventListener("click", () => {
            haptic("selection");
            btnApplio.classList.add("active");
            btnVox.classList.remove("active");
            state.selectedEngine = "applio";
            if (voxModeSection) voxModeSection.classList.add("hidden");
            if (applioSection) applioSection.classList.remove("hidden");
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
        const presetVoice = (state.selectedEngine === "voxcpm" && state.voxcpmSubMode === "curated")
            ? state.selectedPersona
            : null;

        if (state.selectedReferenceFile) {
            const formData = new FormData();
            formData.append("text", text);
            formData.append("model_slug", state.selectedModelSlug || "voxcpm-cloud-ai");
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
                    model_slug: state.selectedModelSlug || "voxcpm-cloud-ai",
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
                if (etaText) etaText.innerText = "Laptop Worker တွင် စတင်လိုက်ပါက အလိုအလျောက် ပြီးစီးပါမည်";
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
                alert(t("toast_empty_url"));
                return;
            }
            haptic("heavy");
            alert("🚀 1-Click Pro Recap အမိန့်ကို လက်ခံရရှိပါပြီ!\nBot မှ ဗီဒီယိုကို ရယူပြီး စာတန်းထိုးနှင့် အသံသွင်းခြင်း စတင်နေပါပြီခင်ဗျာ။");
        });
    }
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
let activePackageCode = "vip_1m";
let activeAmount = 15000;

function openPaymentDrawer(packageCode, priceStr) {
    haptic("light");
    activePackageCode = packageCode;
    activeAmount = packageCode === "vip_1m" ? 15000 : 35000;
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

async function submitPaymentSlip() {
    const fileInput = document.getElementById("slip-file-input");
    const file = fileInput && fileInput.files ? fileInput.files[0] : null;
    if (!file) {
        alert("ငွေလွှဲပြေစာ ပုံကို ရွေးချယ်ပေးပါခင်ဗျာ။");
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
                            <img src="${s.slip_url}" class="slip-thumb" onclick="window.open('${s.slip_url}')" title="Click to view full image">
                            <div class="slip-info">
                                <div class="slip-user">${s.user_name} (@${s.username || s.user_id})</div>
                                <div class="slip-amount">${s.amount_mmk.toLocaleString()} MMK (${s.minutes} mins)</div>
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
    } catch (e) {
        console.warn("Error loading admin data:", e);
    }
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

// ── App Startup ────────────────────────────────────────────────────────────
document.addEventListener("DOMContentLoaded", () => {
    initTelegram();
    setLanguage(currentLang);
    setupTabs();
    setupVoiceStudio();
    setupRecapVisualEditor();
    setupDownloader();

    loadUserProfile();
    loadCuratedVoices();
});

/* ═══════════════════════════════════════════════════════════════════════════
   ADMIN CONSOLE MODULE (Lazy-loaded ONLY when state.user.is_admin === true)
   Combined Round Part A: 6-tab pure-Burmese IA, native admin tools,
   merged Jobs (Queue + History), consolidated Users + User Detail.
   ═══════════════════════════════════════════════════════════════════════════ */

const ADM_SECTIONS = ["overview", "payments", "jobs", "users", "broadcast", "settings"];
let admCurrentSection = "overview";
let admCurrentUserId = null;
let _admAdjustIdemKey = null;
function _newAdjustIdemKey() {
    try { return crypto.randomUUID(); } catch (e) { return "adj-" + Date.now() + "-" + Math.floor(Math.random() * 1e9); }
}
let admLoadedSections = {};

const _admIsMy = () => (typeof currentLang === "string" ? currentLang !== "en" : (localStorage.getItem("studio_lang") || localStorage.getItem("vcp_lang") || "my") !== "en");
const _admL = (my, en) => (_admIsMy() ? my : en);
const _admAskConfirm = (msg, onOk, onCancel) => {
    if (typeof showStudioConfirm === "function") showStudioConfirm(msg, onOk, onCancel);
    else if (onOk) onOk();
};

const _admProState = {
    accounts: [],
    presets: [],
    editingAccId: "",
    orderStatus: "",
    orderSearch: "",
    orderDays: 0,
    expDays: 7,
    broadcastAudience: "all",
    broadcastDeliverable: null,
    auditPrefix: "",
    auditQ: "",
    auditOffset: 0,
};

let _admQueueActiveCount = 0;

function _clearAdminQueuePoll() {
    if (window.adminQueueInterval) {
        clearTimeout(window.adminQueueInterval);
        clearInterval(window.adminQueueInterval);
        window.adminQueueInterval = null;
    }
}
window._clearAdminQueuePoll = _clearAdminQueuePoll;

function scheduleAdminQueuePoll() {
    _clearAdminQueuePoll();
    if (typeof document !== "undefined" && document.hidden) return;
    const adminPane = document.getElementById("tab-admin");
    if (!adminPane || !adminPane.classList.contains("active")) return;
    if (admCurrentSection !== "jobs") return;
    if (admJobsFilter.status !== "active" && admJobsFilter.status !== "all") return;
    const delayMs = _admQueueActiveCount > 0 ? 4000 : 15000;
    window.adminQueueInterval = setTimeout(() => {
        window.adminQueueInterval = null;
        if (typeof document !== "undefined" && document.hidden) return;
        const paneNow = document.getElementById("tab-admin");
        if (!paneNow || !paneNow.classList.contains("active") || admCurrentSection !== "jobs") return;
        if (admJobsFilter.status !== "active" && admJobsFilter.status !== "all") return;
        loadAdminJobs(true);
    }, delayMs);
}
window.scheduleAdminQueuePoll = scheduleAdminQueuePoll;

if (typeof document !== "undefined" && !window._admQueueVisBound) {
    window._admQueueVisBound = true;
    document.addEventListener("visibilitychange", () => {
        if (document.hidden) _clearAdminQueuePoll();
        else scheduleAdminQueuePoll();
    });
}

function admFmtBytes(n) {
    if (n == null) return "—";
    if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB";
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
    return Math.round(n / 1024) + " KB";
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

const _diagState = {
    keys: { loaded: false, inFlight: false },
    timings: { loaded: false, inFlight: false },
    ops: { loaded: false, inFlight: false },
    audit: { loaded: false, inFlight: false },
};

function loadDiagPanel(which) {
    const st = _diagState[which];
    if (!st || st.loaded || st.inFlight) return;
    if (which === "keys") return loadAdminKeyHealth(false);
    if (which === "timings") return loadAdminStageTimings(false);
    if (which === "ops") return loadDiagOpsHealth(false);
    if (which === "audit") return loadAdminAudit(false);
}
window.loadDiagPanel = loadDiagPanel;

function showAdminSection(name) {
    if (name === "activity" || name === "keys") {
        const panel = name === "keys" ? "keys" : "audit";
        const targetId = name === "keys" ? "adm-settings-keys-details" : "adm-settings-activity-details";
        showAdminSection("settings");
        const diag = document.getElementById("adm-acc-diagnostics");
        if (diag) diag.open = true;
        const det = document.getElementById(targetId);
        if (det) {
            const wasOpen = det.open;
            det.open = true;
            if (wasOpen) loadDiagPanel(panel);
            setTimeout(() => det.scrollIntoView({ behavior: "smooth" }), 60);
        }
        return;
    }
    if (!ADM_SECTIONS.includes(name)) name = "overview";
    admCurrentSection = name;
    haptic("light");
    document.querySelectorAll("#adm-nav .adm-nav-btn").forEach(b => {
        const active = b.getAttribute("data-adm-section") === name;
        b.classList.toggle("active", active);
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

    if (name === "overview") {
        if (!window._lastAdminOverview) {
            loadAdminData();
        } else {
            const aDet = document.getElementById("adm-analytics-card");
            if (aDet && aDet.open) loadAdminAnalytics(30);
        }
    }
    if (name === "payments") {
        loadAdminPendingSlips();
        const oDet = document.getElementById("adm-orders-details");
        if (oDet && oDet.open) loadAdminOrders(_admProState.orderStatus);
        const eDet = document.getElementById("admp-expiring-card");
        if (eDet && eDet.open) loadExpiringWatchlist();
        const rDet = document.getElementById("adm-refunds-details");
        if (rDet && rDet.open) loadAdminRefunds();
    }
    if (name === "jobs") {
        loadAdminJobs();
    }
    if (name === "users" && !admLoadedSections.users) {
        loadAdminUsersRecent();
        admLoadedSections.users = true;
    }
    if (name === "settings") {
        mountSettingsAuxUi();
        loadAdminSettings();
        const bDet = document.getElementById("admp-banks-card");
        if (bDet && bDet.open && !window._banksLoaded) loadBankAccounts();
        const pDet = document.getElementById("adm-acc-pricing");
        if (pDet && pDet.open && !window._adminPricingLoaded) loadAdminPricing();
        const aiDet = document.getElementById("adm-acc-ai-bots");
        if (aiDet && aiDet.open && !window._groupAiLoaded) loadAdminGroupAiConsole();
    }
    if (name === "broadcast") {
        const ta = document.getElementById("adm-broadcast-text");
        if (ta && !ta.dataset.admCountBound) {
            ta.dataset.admCountBound = "1";
            ta.addEventListener("input", () => {
                const c = document.getElementById("adm-broadcast-count");
                if (c) c.innerText = ta.value.length;
            });
        }
        loadBroadcastPreview();
        loadBroadcastHistory();
    }
    scheduleAdminQueuePoll();
}
window.showAdminSection = showAdminSection;

function openAdminAuditShortcut() {
    showAdminSection("activity");
}
window.openAdminAuditShortcut = openAdminAuditShortcut;

function onAdminTabEntered() {
    loadAdminData();
    if (admCurrentSection === "payments") loadAdminPendingSlips();
    if (admCurrentSection === "jobs") loadAdminJobs();
    if (admCurrentSection === "users" && !admLoadedSections.users) {
        loadAdminUsersRecent();
        admLoadedSections.users = true;
    }
    if (admCurrentSection === "broadcast") {
        loadBroadcastPreview();
        loadBroadcastHistory();
    }
    if (admCurrentSection === "settings") {
        mountSettingsAuxUi();
        loadAdminSettings();
        const pDet = document.getElementById("adm-acc-pricing");
        if (pDet && pDet.open && !window._adminPricingLoaded) loadAdminPricing();
        const aiDet = document.getElementById("adm-acc-ai-bots");
        if (aiDet && aiDet.open && !window._groupAiLoaded) loadAdminGroupAiConsole();
    }
    scheduleAdminQueuePoll();
}
window.onAdminTabEntered = onAdminTabEntered;
// ── Batch BC-3: income detail sheet ────────────────────────────────
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
            totalEl.innerText = admFmtMmk(data.total_mmk || 0) + " · " +
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
                    <div class="live-job-row-meta">${escapeHtml(p.package_code || "")} · ${tf("admin_income_minutes", { minutes: p.minutes ?? 0 })} · ${admFmtYangon(p.paid_at)}</div>
                </div>
                <div class="income-detail-amount">${admFmtMmk(p.amount_mmk || 0)}</div>
            </div>`).join("");
    } catch (e) {
        list.innerHTML = "";
        if (totalEl) totalEl.innerText = "—";
        showStudioToast(adminErrorText("admin_generic_err", e.message), "error");
    }
}
window.loadIncomeDetail = loadIncomeDetail;
// ── Key Health (Batch BN) ────────────────────────────────────────────
// The app checks its own provider pools server-side (GET
// /api/admin/key-health, cached ~15 min; the Refresh button forces a
// fresh round with ?refresh=1). The payload never carries key
// material — indexes, labels, account names, statuses only.

let admKeyHealthData = null;
let admKeyHealthAccountsOpen = false;

async function loadAdminKeyHealth(force) {
    if (!state.user?.is_admin) return;
    if (!force && (_diagState.keys.inFlight || _diagState.keys.loaded)) return;
    _diagState.keys.inFlight = true;
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
        const res = await fetchWithTimeout("/api/admin/key-health" + (force ? "?refresh=true" : ""), { headers: getAuthHeaders() });
        if (!res.ok) throw new Error("key-health " + res.status);
        admKeyHealthData = await res.json();
        _diagState.keys.loaded = true;
        renderAdminKeyHealth();
    } catch (e) {
        if (summary) admStateError(summary, "loadAdminKeyHealth(true)");
    } finally {
        _diagState.keys.inFlight = false;
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
        if (p.hf.refusal_capped_tokens > 0) pills += " " + admKeyPill("limit", `${p.hf.refusal_capped_tokens} ${t("adm_keys_capped")}`);
        if (p.hf.quota_probe_errors > 0) pills += " " + admKeyPill("limit", `${p.hf.quota_probe_errors} ${t("adm_keys_probe_err")}`);
        let sub = `${t("adm_keys_quota_left")}: ${Math.round(p.hf.quota_remaining_s || 0).toLocaleString()} ${t("adm_keys_unit_s")}`;
        if (p.hf.ledger_effective_s != null) {
            sub += ` · ${t("adm_keys_ledger_eff")}: ${Math.round(p.hf.ledger_effective_s).toLocaleString()} ${t("adm_keys_unit_s")}`;
        }
        if (p.hf.session_ready_tokens != null) {
            sub += ` · ${t("adm_keys_session_ready")}: ${p.hf.session_ready_tokens}`;
        }
        if (p.hf.quota_exhausted > 0) {
            sub += ` · ${p.hf.quota_exhausted} ${t("adm_keys_exhausted")}`;
        }
        if (p.hf.last_drift_s > 0) {
            sub += ` · ${t("adm_keys_drift")}: ${Math.round(p.hf.last_drift_s).toLocaleString()} ${t("adm_keys_unit_s")}`;
        }
        if (p.hf.calibration && p.hf.calibration.totals) {
            const ct = p.hf.calibration.totals;
            sub += ` · ${t("adm_keys_calibrated")}: ${ct.probed_count || 0}/${ct.uncertain_count || 0} (+${Math.round(ct.negative_drift_recovered_s || 0)}/-${Math.round(ct.positive_drift_corrected_s || 0)} ${t("adm_keys_unit_s")})`;
        }
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
        let remain = row.remaining_s == null ? "—"
            : `${Math.round(row.remaining_s).toLocaleString()} / ${Math.round(row.base_s || 0).toLocaleString()} ${t("adm_keys_unit_s")}`;
        if (row.ledger_remaining_s != null && row.remaining_s != null && Math.abs(row.ledger_remaining_s - row.remaining_s) > 15) {
            remain += ` (${t("adm_keys_ledger_short")}: ${Math.round(row.ledger_remaining_s).toLocaleString()} ${t("adm_keys_unit_s")})`;
        }
        let pill = admKeyPill(row.status === "ok" ? "ok" : "dead", row.status === "ok" ? t("adm_keys_working") : (row.status === "dead" ? t("adm_keys_dead") : t("adm_keys_error")));
        if (row.refused_gate_s != null) {
            pill = admKeyPill("limit", `Gate-capped (<${Math.round(row.refused_gate_s)}s)`) + " " + pill;
        }
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
    set("adm-total-users", stats.real_users ?? 0);
    set("adm-total-users-split", tf("admin_stat_users_split", { total: stats.total_users ?? 0, real: stats.real_users ?? 0, never: stats.never_used_users ?? 0 }));
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
        set(id, admFmtMmk(mmk || 0));
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
        const wObj = stats.worker || null;
        if (wObj && wObj.status_code) {
            const seenBit = wObj.last_seen_ago ? ` · ${wObj.last_seen_ago}` : "";
            const stLabel = wObj.status_code === "ONLINE"
                ? t("adm_worker_online")
                : localizeServerText(wObj.status_text || wObj.status_code);
            worker.innerText = "☁️ " + stLabel + seenBit;
        } else {
            const rawWorker = stats.worker_status || stats.cloud_worker_status || "";
            const workerText = (!rawWorker || /online/i.test(rawWorker))
                ? t("adm_worker_online")
                : localizeServerText(rawWorker);
            worker.innerText = "☁️ " + workerText;
        }
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
                    <span class="text-muted adm-line-time">${admFmtYangon(a.created_at)}</span>
                </div>`).join("");
        }
    }
    if (typeof window._admpRenderOverviewFromStats === "function") {
        window._admpRenderOverviewFromStats(stats);
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
    if (!force && (_diagState.timings.inFlight || _diagState.timings.loaded || admStageTimingsInFlight)) return;
    admStageTimingsInFlight = true;
    _diagState.timings.inFlight = true;
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
        _diagState.timings.loaded = true;
        renderAdminStageTimings();
    } catch (e) {
        if (body) admStateError(body, "loadAdminStageTimings(true)");
    } finally {
        admStageTimingsInFlight = false;
        _diagState.timings.inFlight = false;
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
    const aDet = document.getElementById("adm-analytics-card");
    if (aDet && aDet.open) loadAdminAnalytics(30);
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
        const actionsBox = document.getElementById("admin-recent-actions");
        if (actionsBox && !actionsBox.children.length) admStateError(actionsBox, "loadAdminData()");
    }

    // Pending payment slips — only fetch when Payments screen is active (Overview already gets pending_slips count).
    if (admCurrentSection === "payments") {
        await loadAdminPendingSlips();
    }
}
function paintOverviewOpsFromStats(ov) {
    const diskEl = document.getElementById("admp-disk-body");
    const infraEl = document.getElementById("admp-infra-body");
    if (!diskEl || !ov) return;
    const titleEl = document.getElementById("admp-ops-health-title");
    if (titleEl) titleEl.textContent = "🩺 " + _admL("ဆာဗာ သိုလှောင်မှုနှင့် Bot API", "Server Storage & Bot API");
    const probeBtn = document.getElementById("admp-lba-probe");
    if (probeBtn) probeBtn.textContent = "🔌 " + _admL("API စမ်းမည်", "Probe API");
    const d = ov.disk || {};
    if (d.total_bytes) {
        const pct = Number(d.used_percent || 0).toFixed(1);
        const freeGb = (Number(d.free_bytes || 0) / (1024 ** 3)).toFixed(2);
        const totGb = (Number(d.total_bytes || 0) / (1024 ** 3)).toFixed(1);
        const badge = d.low
            ? `<span class="admp-badge admp-badge-err">⚠️ ${escapeHtml(_admL("နေရာနည်းနေသည်", "LOW DISK"))}</span>`
            : `<span class="admp-badge admp-badge-ok">✅ ${escapeHtml(_admL("ကောင်းမွန်သည်", "Healthy"))}</span>`;
        diskEl.innerHTML = `<div class="admp-between"><span>💾 Disk: <b>${pct}%</b> · <b>${freeGb} GB</b> ${escapeHtml(_admL("လွတ်သည်", "free"))} / ${totGb} GB</span>${badge}</div><div class="admp-bar-bg"><div class="admp-bar-fill" style="width:${Math.min(100, Number(pct))}%"></div></div>`;
    } else {
        diskEl.textContent = _admL("💾 Disk အချက်အလက် မရနိုင်ပါ", "💾 Disk telemetry unavailable");
    }
    if (infraEl) {
        const lba = ov.local_bot_api || {};
        const sh = ov.bandwidth_shield || {};
        const lbaCls = lba.active ? "admp-badge-ok" : (lba.configured ? "admp-badge-warn" : "");
        const lbaTxt = lba.active
            ? `🚀 Local Bot API: ${escapeHtml(String(lba.max_upload_mb || 2000))} MB`
            : (lba.configured
                ? `⚠️ Bot API (${escapeHtml(String(lba.max_upload_mb || 50))} MB)`
                : `📡 Official Bot API (${escapeHtml(String(lba.max_upload_mb || 50))} MB)`);
        const shTxt = `🛡️ GZip · 429:${escapeHtml(String(sh.blocked_static_flood_429 || 0))}`;
        infraEl.innerHTML = `<div class="admp-row">
            <span class="admp-badge ${lbaCls}" title="${escapeHtml(lba.last_error || lba.url || "")}">${lbaTxt}</span>
            <span class="admp-badge admp-badge-ok">${shTxt}</span>
        </div>`;
        if (lba.configured && !lba.active) {
            const actBtn = document.createElement("button");
            actBtn.type = "button";
            actBtn.className = "btn mini-btn secondary-btn mt-2";
            actBtn.style.width = "100%";
            actBtn.id = "admp-lba-activate";
            actBtn.textContent = "🚀 " + _admL("Local Bot API (2,000 MB) ပြောင်းဖွင့်မည်", "Activate Local Bot API (2,000 MB)");
            actBtn.onclick = () => {
                const confirmMsg = _admL(
                    "⚠️ Local Bot API ပြောင်းဖွင့်ရန် အတည်ပြုချက်\n\n• Official Telegram API မှ logOut ပြုလုပ်ပြီး Local Bot API (2,000 MB upload) သို့ ပြောင်းလဲချိတ်ဆက်ပါမည်။\n• Local Bot API ဆာဗာ အဆင်သင့်ဖြစ်နေမှသာ အတည်ပြုပါ။",
                    "Activate Local Bot API? This runs official Telegram logOut and switches bot upload transport."
                );
                _admAskConfirm(confirmMsg, async () => {
                    actBtn.disabled = true;
                    try {
                        const aRes = await fetchWithTimeout("/api/admin/local-bot-api/activate", {
                            method: "POST",
                            headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
                            body: JSON.stringify({}),
                        });
                        const aData = aRes.ok ? await aRes.json() : null;
                        showStudioToast(
                            aData?.success
                                ? _admL("Local Bot API ချိတ်ဆက်ပြီးပါပြီ", "Local Bot API activated")
                                : _admL("Local Bot API ချိတ်ဆက်၍ မရသေးပါ", "Local Bot API unavailable"),
                            aData?.success ? "success" : "warning"
                        );
                        loadDiagOpsHealth(true);
                    } finally {
                        actBtn.disabled = false;
                    }
                });
            };
            infraEl.appendChild(actBtn);
        }
    }
}

async function loadDiagOpsHealth(force = false) {
    if (!state.user?.is_admin) return;
    if (!force && (_diagState.ops.inFlight || _diagState.ops.loaded)) return;
    _diagState.ops.inFlight = true;
    const btn = document.getElementById("admp-ops-ref");
    const diskEl = document.getElementById("admp-disk-body");
    if (force && btn) btn.disabled = true;
    if (diskEl && !_diagState.ops.loaded) admStateLoading(diskEl);
    try {
        const res = await fetchWithTimeout("/api/admin/overview", { headers: getAuthHeaders() });
        if (!res.ok) throw new Error("overview " + res.status);
        const ov = await res.json();
        window._lastAdminOverview = ov;
        paintOverviewOpsFromStats(ov);
        _diagState.ops.loaded = true;
    } catch (e) {
        if (diskEl) admStateError(diskEl, "loadDiagOpsHealth(true)");
    } finally {
        _diagState.ops.inFlight = false;
        if (btn) btn.disabled = false;
    }
}
window.loadDiagOpsHealth = loadDiagOpsHealth;

async function probeAdminLocalBotApi() {
    const btn = document.getElementById("admp-lba-probe");
    if (btn) btn.disabled = true;
    try {
        const res = await fetchWithTimeout("/api/admin/local-bot-api/probe", { method: "POST", headers: getAuthHeaders() });
        const data = res.ok ? await res.json() : null;
        if (data && data.local_bot_api) {
            const lba = data.local_bot_api;
            showStudioToast(
                lba.active
                    ? `Local Bot API (${lba.max_upload_mb} MB)`
                    : `Bot API: ${lba.mode} (${lba.max_upload_mb} MB)`,
                lba.active ? "success" : "info"
            );
            if (window._lastAdminOverview) {
                window._lastAdminOverview.local_bot_api = lba;
                paintOverviewOpsFromStats(window._lastAdminOverview);
            }
        }
    } catch (e) {
        showStudioToast(_admL("Bot API စစ်ဆေးမှု မအောင်မြင်ပါ", "Local Bot API probe failed"), "error");
    } finally {
        if (btn) btn.disabled = false;
    }
}
window.probeAdminLocalBotApi = probeAdminLocalBotApi;
async function loadAdminPendingSlips() {
    if (!state.user?.is_admin) return;
    const slipsContainer = document.getElementById("admin-slips-list");
    try {
        const resSlips = await fetchWithTimeout("/api/admin/pending-slips", { headers: getAuthHeaders() });
        if (resSlips.ok && slipsContainer) {
            renderAdminSlips(await resSlips.json());
        } else if (slipsContainer) {
            admStateError(slipsContainer, "loadAdminPendingSlips()");
        }
    } catch (e) {
        if (slipsContainer) admStateError(slipsContainer, "loadAdminPendingSlips()");
    }
}
window.loadAdminPendingSlips = loadAdminPendingSlips;

function renderAdminSlips(slips) {
    window._lastAdminSlips = Array.isArray(slips) ? slips : [];
    const listEl = document.getElementById("admin-slips-list");
    if (!listEl) return;
    const arr = window._lastAdminSlips;
    if (!arr.length) {
        admStateEmpty(listEl, "admin_no_slips");
        return;
    }
    listEl.innerHTML = arr.map(s => {
        const refLine = s.payment_reference ? `<div class="text-xs" style="color:#38bdf8">🧾 Ref: <code>${escapeHtml(s.payment_reference)}</code></div>` : "";
        const lockLine = s.review_claimed_by ? `<div class="text-xs" style="color:#f59e0b">🔒 ${escapeHtml(_admL("Admin စစ်ဆေးနေဆဲ", "Claimed by Admin"))} ${escapeHtml(s.review_claimed_by)}</div>` : "";
        const vTxt = (s.validity_days === 0) ? _admL("သက်တမ်းမကုန်", "No Expiry") : `${s.validity_days ?? 30} ${_admL("ရက်", "d")}`;
        const alloc = [
            Number(s.minutes || 0) > 0 ? `${admFmtSecs(Number(s.minutes) * 60)} Voice` : "",
            Number(s.recap_credits || 0) > 0 ? `${admFmtCredits(s.recap_credits)} Credit` : "",
            vTxt,
        ].filter(Boolean).join(" · ");
        const imgHtml = s.slip_url
            ? `<img data-auth-slip="${escapeHtml(s.slip_url)}" alt="slip" style="width:64px;height:64px;object-fit:cover;border-radius:8px;cursor:pointer;flex-shrink:0;background:rgba(255,255,255,0.06)" onclick="typeof openSlipModal==='function'&&openSlipModal('${escapeHtml(s.slip_url)}')">`
            : `<div style="width:64px;height:64px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:22px;background:rgba(255,255,255,0.06);flex-shrink:0">🧾</div>`;
        return `<div class="admp-card-item">
            <div class="admp-row" style="align-items:flex-start;flex-wrap:nowrap">
                ${imgHtml}
                <div style="flex:1;min-width:0">
                    <div class="admp-between">
                        <strong class="text-xs">${escapeHtml(s.package_name || s.package_code)} — ${escapeHtml(admFmtMmk(s.amount_mmk || 0))}</strong>
                        <span class="text-xs text-muted">${escapeHtml(admFmtYangon(s.created_at))}</span>
                    </div>
                    <div class="text-xs mt-1"><a href="javascript:void(0)" onclick="openAdminUserDetail(${Number(s.user_id)})" style="color:#d4af37;font-weight:600">${escapeHtml(s.user_name)} ${s.username ? `(@${escapeHtml(s.username)})` : ""}</a> · ID ${escapeHtml(String(s.user_id))}</div>
                    <div class="text-xs text-muted">${escapeHtml(alloc)} · <code>${escapeHtml(s.id)}</code></div>
                    ${refLine}${lockLine}
                </div>
            </div>
            <div class="admp-row mt-2">
                <button type="button" class="btn mini-btn gold-btn" onclick="adminApproveSlip('${escapeHtml(s.id)}')">✅ ${escapeHtml(_admL("အတည်ပြုမည်", "Approve"))}</button>
                <button type="button" class="mini-chip" data-edit-slip="${escapeHtml(s.id)}">✏️ ${escapeHtml(_admL("ပြင်ပြီး အတည်ပြုမည်", "Edit & Approve"))}</button>
                <button type="button" class="mini-chip" style="color:#ef4444" onclick="adminRejectSlip('${escapeHtml(s.id)}')">❌ ${escapeHtml(_admL("ငြင်းပယ်မည်", "Reject"))}</button>
            </div>
        </div>`;
    }).join("");

    listEl.querySelectorAll("img[data-auth-slip]").forEach(async (imgEl) => {
        const rawUrl = imgEl.getAttribute("data-auth-slip");
        if (rawUrl && typeof fetchAuthImageUrl === "function") {
            const blobUrl = await fetchAuthImageUrl(rawUrl);
            if (blobUrl) imgEl.src = blobUrl;
        }
    });
    listEl.querySelectorAll("button[data-edit-slip]").forEach(btn => {
        btn.onclick = () => {
            const slip = arr.find(x => x.id === btn.dataset.editSlip);
            if (slip) openEditApproveModal(slip);
        };
    });
}
window.renderAdminSlips = renderAdminSlips;

function openEditApproveModal(slip) {
    const old = document.getElementById("admp-edit-slip-modal");
    if (old) old.remove();
    const baseMins = Number(slip.minutes || 0);
    const baseCredits = Number(slip.recap_credits || 0);
    const presets = [
        { mult: 1.0, label: "မူလအတိုင်း" },
        { mult: 1.1, label: "+10%" },
        { mult: 1.2, label: "+20% ပရိုမို" },
        { mult: 1.5, label: "+50%" },
    ];
    const bd = document.createElement("div");
    bd.id = "admp-edit-slip-modal";
    bd.className = "admp-modal-backdrop";
    bd.innerHTML = `<div class="admp-modal">
        <div class="admp-between mb-2"><strong>✏️ ${escapeHtml(_admL("ပမာဏ ပြင်ဆင်ပြီး အတည်ပြုရန်", "Edit & Approve Slip"))}</strong><button type="button" class="mini-chip" id="admp-es-close">✕</button></div>
        <p class="text-xs text-muted">${escapeHtml(slip.user_name)} · ${escapeHtml(slip.package_name || slip.package_code)} (${escapeHtml(admFmtMmk(slip.amount_mmk || 0))})</p>
        <div class="admp-field mt-2">
            <label class="input-label text-xs">${escapeHtml(_admL("အပိုဆု ပရိုမို အမြန်ရွေးရန်", "Promo Bonus Presets"))}</label>
            <div class="admp-row" id="admp-es-presets">
                ${presets.map((p, idx) => `<button type="button" class="admp-preset-chip ${idx === 0 ? "active" : ""}" data-mult="${p.mult}">${escapeHtml(p.label)}</button>`).join("")}
            </div>
        </div>
        <div class="admp-stack mt-2">
            <div class="admp-field">
                <label class="input-label text-xs">🎙️ ${escapeHtml(_admL("ပေးမည့် Voice မိနစ်", "Voice Minutes"))}</label>
                <input type="number" id="admp-es-mins" class="studio-input text-xs" min="0" max="100000" step="1" value="${Math.round(baseMins)}">
            </div>
            <div class="admp-field">
                <label class="input-label text-xs">🎬 ${escapeHtml(_admL("ပေးမည့် Recap Credit", "Recap Credits"))}</label>
                <input type="number" id="admp-es-rc" class="studio-input text-xs" min="0" max="1000" step="0.01" value="${admFmtCredits(baseCredits)}">
            </div>
            <div class="admp-field">
                <label class="input-label text-xs">⏳ ${escapeHtml(_admL("သက်တမ်း (ရက်) — 0 = သက်တမ်းမကုန်", "Validity Days (0 = No Expiry)"))}</label>
                <input type="number" id="admp-es-days" class="studio-input text-xs" min="0" max="3650" value="${Number(slip.validity_days ?? 30)}">
            </div>
            <button type="button" class="btn gold-btn" style="width:100%" id="admp-es-submit">✅ ${escapeHtml(_admL("ပြင်ပြီး အတည်ပြုမည်", "Confirm Edit & Approve"))}</button>
            <button type="button" class="btn secondary-btn" style="width:100%" id="admp-es-cancel">${escapeHtml(_admL("ပယ်ဖျက်မည်", "Cancel"))}</button>
        </div>
    </div>`;
    document.body.appendChild(bd);

    const minsInput = document.getElementById("admp-es-mins");
    const rcInput = document.getElementById("admp-es-rc");
    bd.querySelectorAll("#admp-es-presets .admp-preset-chip").forEach(chip => {
        chip.onclick = () => {
            bd.querySelectorAll("#admp-es-presets .admp-preset-chip").forEach(c => c.classList.remove("active"));
            chip.classList.add("active");
            const mult = Number(chip.dataset.mult || 1);
            minsInput.value = String(Math.round(baseMins * mult));
            rcInput.value = String(Number((baseCredits * mult).toFixed(2)));
        };
    });
    const closeModal = () => bd.remove();
    document.getElementById("admp-es-close").onclick = closeModal;
    document.getElementById("admp-es-cancel").onclick = closeModal;
    document.getElementById("admp-es-submit").onclick = async () => {
        const payload = {
            request_id: slip.id,
            minutes: Number(minsInput.value || 0),
            recap_credits: Number(rcInput.value || 0),
            validity_days: Number(document.getElementById("admp-es-days").value ?? 30),
        };
        const btn = document.getElementById("admp-es-submit");
        if (btn) btn.disabled = true;
        try {
            const res = await fetchWithTimeout("/api/admin/approve-slip", {
                method: "POST",
                headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) { showStudioToast(data.detail || "Approval failed", "error"); return; }
            showStudioToast(data.message || _admL("အတည်ပြုပြီးပါပြီ", "Approved"), "success");
            closeModal();
            await loadAdminPendingSlips();
            loadAdminData();
        } finally {
            if (btn) btn.disabled = false;
        }
    };
}

async function loadAdminOrders(status) {
    if (typeof status === "string") _admProState.orderStatus = status;
    document.querySelectorAll("#adm-screen-payments [data-ord-st]").forEach(b => {
        b.classList.toggle("active", (b.getAttribute("data-ord-st") || "") === _admProState.orderStatus);
    });
    const listEl = document.getElementById("adm-orders-list");
    if (!listEl || !state.user?.is_admin) return;
    _admProState.orderSearch = (document.getElementById("admp-ord-q")?.value || "").trim();
    _admProState.orderDays = Number(document.getElementById("admp-ord-days")?.value || 0);
    const qs = new URLSearchParams({ limit: "60" });
    if (_admProState.orderStatus) qs.set("status", _admProState.orderStatus);
    if (_admProState.orderSearch) qs.set("search", _admProState.orderSearch);
    if (_admProState.orderDays > 0) qs.set("days", String(_admProState.orderDays));
    try {
        const res = await fetchWithTimeout(`/api/admin/orders?${qs.toString()}`, { headers: getAuthHeaders() });
        if (!res.ok) { admStateError(listEl, "loadAdminOrders()"); return; }
        const data = await res.json();
        const rows = data.orders || [];
        if (!rows.length) {
            listEl.innerHTML = `<p class="text-muted text-xs">${escapeHtml(_admL("မှတ်တမ်း မရှိပါ", "No orders match filter"))}</p>`;
            return;
        }
        listEl.innerHTML = rows.map(o => {
            const bCls = o.status === "APPROVED" ? "admp-badge-ok" : (o.status === "REJECTED" ? "admp-badge-err" : "admp-badge-warn");
            const slipBtn = (o.status === "PENDING" && o.slip_url)
                ? `<button type="button" class="mini-chip" onclick="typeof openSlipModal==='function'&&openSlipModal('${escapeHtml(o.slip_url)}')">🖼️ Slip</button>`
                : "";
            const refTxt = o.payment_reference ? ` · Ref: <code>${escapeHtml(o.payment_reference)}</code>` : "";
            const rejTxt = o.rejection_reason ? `<div class="text-xs text-danger">❌ ${escapeHtml(o.rejection_reason)}</div>` : "";
            const dt = admFmtYangon(o.created_at);
            const vTxt = (o.validity_days === 0) ? _admL("သက်တမ်းမကုန်", "No Expiry") : `${o.validity_days ?? 30} ${_admL("ရက်", "d")}`;
            return `<div class="admp-card-item">
                <div class="admp-between">
                    <div><strong class="text-xs">${escapeHtml(o.package_name || o.package_code)}</strong> · <b>${escapeHtml(admFmtMmk(o.amount_mmk || 0))}</b></div>
                    <span class="admp-badge ${bCls}">${escapeHtml(o.status)}</span>
                </div>
                <div class="admp-between text-xs text-muted mt-1">
                    <span><a href="javascript:void(0)" onclick="openAdminUserDetail(${Number(o.user_id)})" style="color:#d4af37">${escapeHtml(o.user_name)}</a> (${escapeHtml(String(o.user_id))}) · ${admFmtSecs(Number(o.minutes || 0) * 60)} / ${admFmtCredits(o.recap_credits)} Credit / ${escapeHtml(vTxt)}${refTxt}</span>
                    <div class="admp-row">${slipBtn}<span>${escapeHtml(dt)}</span></div>
                </div>
                ${rejTxt}
            </div>`;
        }).join("");
    } catch (err) {
        admStateError(listEl, "loadAdminOrders()");
    }
}
window.loadAdminOrders = loadAdminOrders;

async function exportAdminOrdersCsv() {
    const csvBtn = document.getElementById("admp-ord-csv");
    if (csvBtn) csvBtn.disabled = true;
    try {
        const res = await fetchWithTimeout("/api/admin/orders/export-dm", {
            method: "POST",
            headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
            body: JSON.stringify({
                status: _admProState.orderStatus || null,
                search: _admProState.orderSearch || "",
                days: Number(_admProState.orderDays || 30),
            }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || "Export HTTP " + res.status);
        showStudioToast(_admL("DM သို့ ပို့ပြီးပါပြီ", "CSV sent to your Telegram DM"), "success");
    } catch (err) {
        showStudioToast(err.message, "error");
    } finally {
        if (csvBtn) csvBtn.disabled = false;
    }
}
window.exportAdminOrdersCsv = exportAdminOrdersCsv;

function bindGrantActionButtons(root, onDone) {
    if (!root) return;
    root.querySelectorAll("button[data-g-ext]").forEach(btn => {
        btn.onclick = () => {
            openGrantExtendModal({
                grantId: btn.dataset.gExt,
                userName: btn.dataset.gUser || "",
                pkgLabel: btn.dataset.gPkg || "Package",
                expiresAt: btn.dataset.gExp || "",
            }, Number(btn.dataset.days || 7), onDone);
        };
    });
    root.querySelectorAll("button[data-g-rev]").forEach(btn => {
        btn.onclick = () => confirmGrantRevoke({
            grantId: btn.dataset.gRev,
            userName: btn.dataset.gUser || "",
            pkgLabel: btn.dataset.gPkg || "Package",
            remTxt: btn.dataset.gRem || "",
        }, onDone);
    });
}

async function loadExpiringWatchlist() {
    const box = document.getElementById("admp-exp-list");
    if (!box) return;
    _admProState.expDays = Number(document.getElementById("admp-exp-days")?.value || _admProState.expDays || 7);
    try {
        const res = await fetchWithTimeout(`/api/admin/expiring-grants?days=${_admProState.expDays}`, { headers: getAuthHeaders() });
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();
        const rows = data.grants || [];
        if (!rows.length) {
            box.innerHTML = `<p class="text-muted text-xs">✅ ${escapeHtml(_admL(`${_admProState.expDays} ရက်အတွင်း သက်တမ်းကုန်မည့် Package မရှိပါ`, `No paid lots expiring within ${_admProState.expDays} days`))}</p>`;
            return;
        }
        box.innerHTML = rows.map(g => {
            const parts = [];
            if (Number(g.remaining_recap_credits || 0) > 0) parts.push(`${admFmtCredits(g.remaining_recap_credits)} Credit`);
            if (Number(g.remaining_voice_minutes || 0) > 0) parts.push(admFmtSecs(Number(g.remaining_voice_minutes) * 60));
            const remTxt = parts.join(" + ") || "0m";
            const warnCls = g.days_left <= 1 ? "admp-badge-err" : "admp-badge-warn";
            const pkgLabel = g.package_name || g.package_code || g.source_type || "Package";
            return `<div class="admp-card-item">
                <div class="admp-between">
                    <div><a href="javascript:void(0)" onclick="openAdminUserDetail(${Number(g.user_id)})" style="color:#d4af37;font-weight:600" class="text-xs">${escapeHtml(g.user_name)} ${g.username ? `(@${escapeHtml(g.username)})` : ""}</a> · <span class="text-xs">${escapeHtml(remTxt)}</span></div>
                    <span class="admp-badge ${warnCls}">⏳ ${escapeHtml(String(g.days_left))} ${_admL("ရက်ကျန်", "d")}</span>
                </div>
                <div class="admp-between text-xs text-muted mt-1">
                    <span>${escapeHtml(pkgLabel)} · ${escapeHtml(admFmtYangon(g.expires_at))}</span>
                    <div class="admp-row">
                        <button type="button" class="mini-chip" data-g-ext="${escapeHtml(String(g.grant_id))}" data-days="7" data-g-user="${escapeHtml(g.user_name)}" data-g-pkg="${escapeHtml(pkgLabel)}" data-g-exp="${escapeHtml(g.expires_at || "")}">+7d</button>
                        <button type="button" class="mini-chip" data-g-ext="${escapeHtml(String(g.grant_id))}" data-days="30" data-g-user="${escapeHtml(g.user_name)}" data-g-pkg="${escapeHtml(pkgLabel)}" data-g-exp="${escapeHtml(g.expires_at || "")}">+30d</button>
                        <button type="button" class="mini-chip" style="color:#ef4444" data-g-rev="${escapeHtml(String(g.grant_id))}" data-g-user="${escapeHtml(g.user_name)}" data-g-pkg="${escapeHtml(pkgLabel)}" data-g-rem="${escapeHtml(remTxt)}">🚫</button>
                    </div>
                </div>
            </div>`;
        }).join("");
        bindGrantActionButtons(box, () => loadExpiringWatchlist());
    } catch (err) {
        box.innerHTML = `<p class="text-xs text-danger">${escapeHtml(err.message)}</p>`;
    }
}
window.loadExpiringWatchlist = loadExpiringWatchlist;

function openGrantExtendModal(meta, addDays, onDone) {
    const old = document.getElementById("admp-grant-ext-modal");
    if (old) old.remove();
    let baseMs = Date.now();
    if (meta.expiresAt) {
        const parsed = new Date(meta.expiresAt).getTime();
        if (!isNaN(parsed) && parsed > baseMs) baseMs = parsed;
    }
    const newExpIso = new Date(baseMs + addDays * 86400 * 1000).toISOString();
    const bd = document.createElement("div");
    bd.id = "admp-grant-ext-modal";
    bd.className = "admp-modal-backdrop";
    bd.innerHTML = `<div class="admp-modal">
        <div class="admp-between mb-2"><strong>⏳ ${escapeHtml(_admL("သက်တမ်း တိုးရန် အတည်ပြုချက်", "Confirm Grant Extension"))}</strong><button type="button" class="mini-chip" id="admp-ge-close">✕</button></div>
        <p class="text-xs"><b>${escapeHtml(meta.userName || "")}</b> · ${escapeHtml(meta.pkgLabel || "")}</p>
        <p class="text-xs text-muted mt-1">+${addDays} ${_admL("ရက်", "d")} → <b>${escapeHtml(admFmtYangon(newExpIso))}</b></p>
        <label class="text-xs mt-2" style="display:flex;align-items:center;gap:8px"><input type="checkbox" id="admp-ge-notify" checked> ${escapeHtml(_admL("သုံးစွဲသူထံ Telegram စာ ပို့ အသိပေးမည်", "Notify user via Telegram"))}</label>
        <div class="admp-stack mt-3">
            <button type="button" class="btn gold-btn" style="width:100%" id="admp-ge-ok">⏳ ${escapeHtml(_admL(`+${addDays} ရက် သက်တမ်းတိုးမည်`, `Extend +${addDays}d`))}</button>
            <button type="button" class="btn secondary-btn" style="width:100%" id="admp-ge-cancel">${escapeHtml(_admL("ပယ်ဖျက်မည်", "Cancel"))}</button>
        </div>
    </div>`;
    document.body.appendChild(bd);
    const closeModal = () => bd.remove();
    document.getElementById("admp-ge-close").onclick = closeModal;
    document.getElementById("admp-ge-cancel").onclick = closeModal;
    document.getElementById("admp-ge-ok").onclick = async () => {
        const notify_user = !!document.getElementById("admp-ge-notify").checked;
        const okBtn = document.getElementById("admp-ge-ok");
        if (okBtn) okBtn.disabled = true;
        try {
            const res = await fetchWithTimeout(`/api/admin/grants/${encodeURIComponent(meta.grantId)}/extend`, {
                method: "POST",
                headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
                body: JSON.stringify({ add_days: addDays, notify_user }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) { showStudioToast(data.detail || "Extend failed", "error"); return; }
            showStudioToast(data.message || _admL(`+${addDays} ရက် တိုးပြီးပါပြီ`, `Extended +${addDays}d`), "success");
            closeModal();
            if (onDone) onDone();
        } finally {
            if (okBtn) okBtn.disabled = false;
        }
    };
}

function confirmGrantRevoke(meta, onDone) {
    const msg = _admL(
        `🚫 Grant ရုပ်သိမ်းရန် အတည်ပြုချက်\n\n• အသုံးပြုသူ: ${meta.userName}\n• Package: ${meta.pkgLabel}\n• ကျန်ရှိပမာဏ: ${meta.remTxt}\n\nရုပ်သိမ်းပါက ကျန်ရှိသော လက်ကျန်များ ချက်ချင်း ဖြတ်တောက်ခံရပါမည်။`,
        `Revoke ${meta.pkgLabel} from ${meta.userName} (${meta.remTxt})? Remaining quota will be deducted immediately.`
    );
    _admAskConfirm(msg, async () => {
        const res = await fetchWithTimeout(`/api/admin/grants/${encodeURIComponent(meta.grantId)}/revoke`, {
            method: "POST",
            headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
            body: JSON.stringify({ reason: "Admin console revoke", notify: false }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { showStudioToast(data.detail || "Revoke failed", "error"); return; }
        showStudioToast(data.message || _admL("Grant ကို ရုပ်သိမ်းပြီးပါပြီ", "Revoked"), "success");
        if (onDone) onDone();
    });
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
            loadAdminPendingSlips();
            loadAdminOrders("");
            loadAdminData();
        } catch (e) {
            showStudioToast(adminErrorText("admin_generic_err", e.message), 'error');
        }
    });
}
window.adminApproveSlip = adminApproveSlip;

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
                loadAdminPendingSlips();
                loadAdminOrders("");
                loadAdminData();
            } catch (e) {
                showStudioToast(adminErrorText("admin_generic_err", e.message), 'error');
            }
        },
    });
}
window.adminRejectSlip = adminRejectSlip;
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
    const initial = escapeHtml((Array.from((u.first_name || "?").trim())[0] || "?").toUpperCase());
    const balFmt = admFmtSecs(u.balance_seconds || 0);
    const pendingBadge = Number(u.daily_free_pending_seconds || 0) > 0
        ? ` <span class="mini-tag text-muted">${escapeHtml(t("admin_daily_free_pending"))}</span>`
        : "";
    // Sub-line as segments (same pattern as D-1): the "•" separators
    const segs = [];
    if (u.username) segs.push(`@${escapeHtml(u.username)}`);
    segs.push(`ID: ${u.id}`);
    segs.push(escapeHtml(u.account_category || "FREE"));
    segs.push(`\u23f1 ${balFmt}${pendingBadge}`);
    segs.push(`\ud83c\udfac ${admFmtCredits(u.recap_credits ?? 0)}`);
    if (u.is_banned) segs.push("\ud83d\udeab");
    const subHtml = segs.map(s => `<span class="adm-sub-seg">${s}</span>`).join("");
    // Two-line row (full-app audit A-3/A-4/A-6): line 1 is the name
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
window.admUserRowHtml = admUserRowHtml;


let admUsersCategory = "";
let admUsersFilter = "";
let admUsersSort = "recent";
let admUsersOffset = 0;
const ADM_USERS_PAGE = 20;

function admUsersCategoryParam() {
    return admUsersCategory ? `&category=${encodeURIComponent(admUsersCategory)}` : "";
}

function syncAdminUsersChips() {
    document.querySelectorAll("#adm-users-filter-chips .adm-users-filter-chip").forEach(btn => {
        btn.classList.toggle("active", (btn.getAttribute("data-cat") || "") === admUsersCategory);
    });
}

function setAdminUsersCategory(cat) {
    admUsersCategory = cat || "";
    admUsersOffset = 0;
    syncAdminUsersChips();
    loadAdminUsersRecent(false);
}
window.setAdminUsersCategory = setAdminUsersCategory;

function toggleAdminUserFlag(flag, btn) {
    const next = admUsersFilter === flag ? "" : flag;
    admUsersFilter = next;
    document.querySelectorAll("#admp-users-extra-row [data-u-flt]").forEach(b => {
        b.classList.toggle("active", b.dataset.uFlt === next);
    });
    admUsersOffset = 0;
    loadAdminUsersRecent(false);
}
window.toggleAdminUserFlag = toggleAdminUserFlag;

function setAdminUsersSort(sortVal) {
    admUsersSort = sortVal || "recent";
    admUsersOffset = 0;
    loadAdminUsersRecent(false);
}
window.setAdminUsersSort = setAdminUsersSort;

function loadMoreAdminUsers() {
    admUsersOffset += ADM_USERS_PAGE;
    loadAdminUsersRecent(true);
}
window.loadMoreAdminUsers = loadMoreAdminUsers;

function openAdminUsersPaid() {
    admUsersCategory = "PURCHASED";
    admUsersOffset = 0;
    admLoadedSections.users = true;
    showAdminSection("users");
    syncAdminUsersChips();
    loadAdminUsersRecent(false);
}
window.openAdminUsersPaid = openAdminUsersPaid;

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
        const qs = new URLSearchParams({
            limit: String(ADM_USERS_PAGE),
            offset: String(admUsersOffset),
            sort: admUsersSort || "recent",
        });
        if (admUsersCategory) qs.set("category", admUsersCategory);
        if (admUsersFilter) qs.set("filter", admUsersFilter);
        const q = (document.getElementById("adm-user-search-q")?.value || "").trim();
        if (q) qs.set("search", q);
        const res = await fetchWithTimeout(`/api/admin/users?${qs.toString()}`, { headers: getAuthHeaders() });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        if (!data.users.length) {
            if (append) { if (moreBtn) moreBtn.classList.add("hidden"); return; }
            window._lastAdminUsersList = [];
            admStateEmpty(box, q ? "admin_search_none" : "admin_empty");
            return;
        }
        window._lastAdminUsersList = append && Array.isArray(window._lastAdminUsersList)
            ? window._lastAdminUsersList.concat(data.users)
            : data.users;
        const rowsHtml = data.users.map(admUserRowHtml).join("");
        if (append) box.insertAdjacentHTML("beforeend", rowsHtml);
        else box.innerHTML = rowsHtml;
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
    }
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
    set("adm-detail-balance", admFmtSecs(u.balance_seconds || 0));
    set("adm-detail-credits", admFmtCredits(u.recap_credits ?? 0));
    set("adm-detail-jobs", `${u.jobs_total ?? 0} (${u.jobs_success ?? 0}✓ / ${u.jobs_failed ?? 0}✗)`);
    set("adm-detail-lastjob", admFmtDate(u.last_job_at));
    ["adm-adj-minutes", "adm-adj-seconds", "adm-adj-credits"].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = 0;
    });
    const tier = document.getElementById("adm-adj-tier");
    if (tier) tier.value = "";
    const testToggle = document.getElementById("adm-test-flag-toggle");
    if (testToggle) testToggle.checked = !!u.is_test_account;
    renderAdminBanZone(u);
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
    const compact = arguments.length > 1 ? !!arguments[1] : true;
    const s = Math.max(0, Math.round(Number(total) || 0));
    const m = Math.floor(s / 60);
    const rem = s % 60;
    if (!compact) return m + "m " + rem + "s";
    if (s === 0) return "0m";
    if (m === 0) return rem + "s";
    if (rem === 0) return m + "m";
    return m + "m " + rem + "s";
}
window.admFmtSecs = admFmtSecs;

function admFmtCredits(val) {
    const n = Number(val);
    if (!Number.isFinite(n) || n === 0) return "0";
    return String(Number(n.toFixed(2)));
}
window.admFmtCredits = admFmtCredits;

function admFmtMmk(val) {
    const num = Math.round(Number(val) || 0);
    if (typeof currentLang !== "undefined" && currentLang === "en") {
        return num.toLocaleString() + " MMK";
    }
    const suffix = typeof t === "function" ? t("adm_mmk_suffix") : " MMK";
    return num.toLocaleString("en-US") + suffix;
}
window.admFmtMmk = admFmtMmk;

// Batch CD Item 1: render the completed-detail sections (balance

function renderUserDetailGrantsCard(u) {
    const gEl = document.getElementById("adm-detail-grants");
    if (!gEl) return;
    const grants = Array.isArray(u.wallet_grants) ? u.wallet_grants : [];
    if (!grants.length) {
        gEl.innerHTML = `<p class="text-muted text-xs">${escapeHtml(t("vip_grants_empty"))}</p>`;
        return;
    }
    const uid = u.id;
    const uName = u.first_name || ("ID " + uid);
    gEl.innerHTML = grants.map(g => {
        const parts = [];
        if (Number(g.remaining_voice_minutes || 0) > 0 || Number(g.initial_voice_minutes || 0) > 0) parts.push(`🎙️ ${admFmtSecs(Number(g.remaining_voice_minutes || 0) * 60)}`);
        if (Number(g.remaining_recap_credits || 0) > 0 || Number(g.initial_recap_credits || 0) > 0) parts.push(`🎬 ${admFmtCredits(g.remaining_recap_credits)} Cr`);
        const remTxt = parts.join(" · ") || "0m";
        const pkgLabel = g.package_name || g.package_code || g.source_type || "Grant";
        const expTxt = g.never_expires
            ? (g.source_type === "LEGACY_GRANDFATHER" ? t("vip_grant_legacy") : t("vip_grant_no_expiry"))
            : (g.status === "EXPIRED" ? t("vip_grant_expired") : `${admFmtDate(g.expires_at)} (${g.days_left ?? 0}${_admL(" ရက်", "d")})`);
        const isActive = String(g.status || "").toUpperCase() === "ACTIVE";
        const actions = (isActive && g.id) ? `<div class="admp-row mt-1">
            <button type="button" class="mini-chip" data-g-ext="${escapeHtml(String(g.id))}" data-days="7" data-g-user="${escapeHtml(uName)}" data-g-pkg="${escapeHtml(pkgLabel)}" data-g-exp="${escapeHtml(g.expires_at || "")}">+7d</button>
            <button type="button" class="mini-chip" data-g-ext="${escapeHtml(String(g.id))}" data-days="30" data-g-user="${escapeHtml(uName)}" data-g-pkg="${escapeHtml(pkgLabel)}" data-g-exp="${escapeHtml(g.expires_at || "")}">+30d</button>
            <button type="button" class="mini-chip" style="color:#ef4444" data-g-rev="${escapeHtml(String(g.id))}" data-g-user="${escapeHtml(uName)}" data-g-pkg="${escapeHtml(pkgLabel)}" data-g-rem="${escapeHtml(remTxt)}">🚫</button>
        </div>` : "";
        return `<div class="admp-card-item">
            <div class="admp-between">
                <div><b>${escapeHtml(pkgLabel)}</b><br><span class="text-xs text-muted">${escapeHtml(expTxt)}</span></div>
                <div style="text-align:right"><b>${escapeHtml(remTxt)}</b><br><span class="badge-status ${String(g.status || "").toLowerCase()}">${escapeHtml(g.status || "")}</span></div>
            </div>${actions}
        </div>`;
    }).join("");
    bindGrantActionButtons(gEl, () => openAdminUserDetail(uid));
}

function renderAdminDetailExtras(u) {
    const row = (label, val) => `<div class="adm-bc-row"><span>${label}</span><strong>${val}</strong></div>`;
    const bb = u.balance_breakdown || {};
    const dailyPending = Number(bb.daily_free_pending_seconds || 0);
    const dailyVal = dailyPending > 0
        ? `${admFmtSecs(bb.daily_free_seconds, false)} · ${admFmtSecs(dailyPending, false)} (${escapeHtml(t("admin_daily_free_pending"))})`
        : admFmtSecs(bb.daily_free_seconds, false);
    const bd = document.getElementById("adm-detail-breakdown");
    if (bd) bd.innerHTML =
        row(t("admin_bal_daily"), dailyVal) +
        row(t("admin_bal_welcome"), admFmtSecs(bb.welcome_seconds, false)) +
        row(t("admin_bal_referral_voice"), admFmtSecs(bb.referral_voice_seconds, false)) +
        row(t("admin_bal_referral"), admFmtSecs(bb.referral_seconds, false)) +
        row(t("admin_bal_purchased"), admFmtSecs(bb.purchased_seconds, false)) +
        row(t("admin_bal_consumed"), admFmtSecs(bb.consumed_seconds, false));
    renderUserDetailGrantsCard(u);
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
    // 1 Jobs list (#adm-user-jobs)
    const jobs = u.recent_jobs || [];
    const jEl = document.getElementById("adm-user-jobs");
    if (jEl) {
        window._lastAdminUserJobs = jobs;
        jEl.innerHTML = jobs.length ? jobs.map(j => `
            <div class="adm-job-mini">
                <span class="history-item-badge ${j.job_type === 'RECAP' ? 'badge-recap' : j.job_type === 'TTS' ? 'badge-tts' : 'badge-voice'}">${j.job_type === 'RECAP' ? '🎬' : j.job_type === 'TTS' ? '🎧' : '🎙️'}</span>
                <span class="adm-job-mini-main">${escapeHtml(j.input_snippet || j.preset_voice || j.job_type || "—")} · ${escapeHtml(j.status || "")}</span>
                <span class="text-muted text-xs">${admFmtDate(j.created_at)} · ${admFmtSecs(j.seconds_consumed || j.seconds_estimated || 0)}</span>
            </div>`).join("") : `<p class="text-muted">${t("admin_user_jobs_empty")}</p>`;
    }

    // Payments
    const pays = u.payments || [];
    const pEl = document.getElementById("adm-detail-payments");
    if (pEl) pEl.innerHTML = pays.length ? pays.map(p => `
        <div class="adm-job-mini">
            <span class="adm-job-mini-main">${escapeHtml(p.plan || "")}</span>
            <span class="history-item-badge status-${(p.status || "").toLowerCase()}">${escapeHtml(p.status || "")}</span>
            <span class="text-muted text-xs">${admFmtMmk(p.amount_mmk || 0)} · ${admFmtDate(p.created_at)}</span>
        </div>`).join("") : `<p class="text-muted">${t("admin_payments_empty")}</p>`;
    const ptEl = document.getElementById("adm-detail-payments-total");
    if (ptEl) ptEl.innerHTML = row(t("admin_payments_total"), admFmtMmk(u.payments_total_mmk || 0));

    // 1 Unified Wallet Ledger (#adm-detail-ledger)
    const lEl = document.getElementById("adm-detail-ledger");
    if (lEl) {
        const wtx = u.wallet_transactions_recent || [];
        if (wtx.length) {
            lEl.innerHTML = wtx.map(tx => {
                const bits = [];
                if (tx.voice_minutes_delta) bits.push(`${tx.voice_minutes_delta > 0 ? "+" : ""}${tx.voice_minutes_delta}m`);
                if (tx.recap_credits_delta) bits.push(`${tx.recap_credits_delta > 0 ? "+" : ""}${tx.recap_credits_delta} Cr`);
                return `<div class="adm-job-mini">
                    <span class="adm-job-mini-main"><b>${escapeHtml(tx.tx_type)}</b> (${escapeHtml(tx.service_type || "")})${tx.note ? " · " + escapeHtml(tx.note) : ""}</span>
                    <span class="text-muted text-xs">${escapeHtml(bits.join(" ") || "0")} · ${admFmtDate(tx.created_at)}</span>
                </div>`;
            }).join("");
        } else {
            const unified = [];
            (u.ledger_recent || []).forEach(e => unified.push({
                label: (e.kind || "Voice") + (e.reason ? " · " + e.reason : ""),
                deltaTxt: (e.delta >= 0 ? "+" : "-") + admFmtSecs(Math.abs(e.delta || 0)),
                ts: e.created_at || "",
            }));
            (u.recap_ledger_recent || []).forEach(e => unified.push({
                label: "🎬 " + (e.kind || "Recap") + (e.reason ? " · " + e.reason : ""),
                deltaTxt: (e.delta >= 0 ? "+" : "-") + admFmtCredits(Math.abs(e.delta || 0)) + " Cr",
                ts: e.created_at || "",
            }));
            unified.sort((a, b) => String(b.ts).localeCompare(String(a.ts)));
            lEl.innerHTML = unified.length ? unified.slice(0, 20).map(e => `
                <div class="adm-job-mini">
                    <span class="adm-job-mini-main">${escapeHtml(e.label)}</span>
                    <span class="text-muted text-xs">${escapeHtml(e.deltaTxt)} · ${admFmtDate(e.ts)}</span>
                </div>`).join("") : `<p class="text-muted">${t("admin_ledger_empty")}</p>`;
        }
    }

    const chats = u.ai_chats || [];
    const cEl = document.getElementById("adm-detail-ai-chats");
    if (cEl) cEl.innerHTML = chats.length ? chats.map(m => `
        <div class="adm-chat-row ${m.direction === "user" ? "adm-chat-user" : "adm-chat-ai"}">
            <span class="adm-chat-who">${m.direction === "user" ? t("admin_ai_chat_user") : t("admin_ai_chat_ai")}</span>
            <span class="adm-chat-text">${escapeHtml(m.text || "")}</span>
            <span class="adm-chat-time">${admFmtDate(m.created_at)}</span>
        </div>`).join("") : `<p class="text-muted">${t("admin_ai_chats_empty")}</p>`;
}

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

async function adminToggleTestFlag(checked) {
    const uid = admCurrentUserId;
    const toggle = document.getElementById("adm-test-flag-toggle");
    if (!uid) { if (toggle) toggle.checked = !checked; return; }
    haptic("light");
    const confirmKey = checked ? "adm_confirm_test_flag_on" : "adm_confirm_test_flag_off";
    showStudioConfirm(t(confirmKey), async () => {
        if (toggle) toggle.disabled = true;
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
        } finally {
            if (toggle) toggle.disabled = false;
        }
    }, () => {
        if (toggle) toggle.checked = !checked;
    });
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
    const uSnap = window._lastAdminUserDetail || {};
    const curVoiceSec = Number(uSnap.balance_seconds || 0);
    const curPurchasedSec = Number(uSnap.balance_breakdown?.purchased_seconds || 0);
    const curCredits = Number(uSnap.recap_credits || 0);
    const deltaVoiceSec = Math.round(minutes * 60 + seconds);
    const nextVoiceSec = (tier === "FREE")
        ? Math.max(0, curVoiceSec - curPurchasedSec + deltaVoiceSec)
        : Math.max(0, curVoiceSec + deltaVoiceSec);
    const nextCredits = Math.max(0, curCredits + credits);
    const curTier = String(uSnap.account_category || "FREE");
    const nextTier = tier || curTier;
    const userLabel = `${uSnap.first_name || "User"} (ID: ${uid})`;
    const freeWipeWarning = (tier === "FREE")
        ? tf("adm_confirm_adjust_free_warn", { mins: admFmtSecs(curPurchasedSec) })
        : "";
    const confirmAdjustMsg = tf("adm_confirm_adjust_msg", {
        user: userLabel,
        curTier,
        nextTier,
        curVoice: admFmtSecs(curVoiceSec),
        nextVoice: admFmtSecs(nextVoiceSec),
        curCr: admFmtCredits(curCredits),
        nextCr: admFmtCredits(nextCredits),
        reason,
        warn: freeWipeWarning,
    });

    const doApply = async () => {
        haptic("medium");
        const applyBtn = document.getElementById("adm-adjust-apply-btn");
        if (applyBtn) applyBtn.disabled = true;
        try {
            const vdaysEl = document.getElementById("adm-adj-vdays");
            const rawVdays = vdaysEl ? String(vdaysEl.value ?? "").trim() : "";
            const parsedVdays = rawVdays === "" ? 30 : parseInt(rawVdays, 10);
            const validity_days = Number.isFinite(parsedVdays) ? parsedVdays : 30;
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
            _admAdjustIdemKey = null;
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
    showStudioConfirm(confirmAdjustMsg, doApply);
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
    showStudioConfirm(t(ban ? "admin_confirm_ban" : "adm_confirm_unban_user"), doCall);
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
            <span class="adm-job-mini-main">${escapeHtml(j.input_snippet || j.preset_voice || j.job_type || "—")}</span>
            <span class="history-item-badge status-${(j.status || "").toLowerCase()}">${escapeHtml(jobStatusText(j.status))}</span>
            <span class="text-muted text-xs">${admFmtDate(j.created_at)}</span>
        </div>`).join("");
}
// ── Broadcast ────────────────────────────────────────────────────────
function setBroadcastAudience(aud) {
    _admProState.broadcastAudience = aud || "all";
    loadBroadcastPreview();
}
window.setBroadcastAudience = setBroadcastAudience;

async function loadBroadcastPreview() {
    const el = document.getElementById("admp-bc-count");
    if (!el) return;
    try {
        const res = await fetchWithTimeout(`/api/admin/broadcast/preview?audience=${encodeURIComponent(_admProState.broadcastAudience)}`, { headers: getAuthHeaders() });
        if (!res.ok) return;
        const d = await res.json();
        _admProState.broadcastDeliverable = Number(d.deliverable || 0);
        el.textContent = `👥 ${_admProState.broadcastDeliverable} ${_admL("ဦး ပို့မည်", "recipients")}`;
    } catch (e) {}
}
window.loadBroadcastPreview = loadBroadcastPreview;

async function sendTestBroadcast() {
    const testBtn = document.getElementById("admp-bc-test-btn");
    const msg = (document.getElementById("adm-broadcast-text")?.value || "").trim();
    if (msg.length < 4) { showStudioToast(_admL("စာသား အနည်းဆုံး ၄ လုံး ရိုက်ပါ", "Minimum 4 characters"), "warning"); return; }
    if (testBtn) testBtn.disabled = true;
    try {
        const res = await fetchWithTimeout("/api/admin/broadcast", {
            method: "POST",
            headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
            body: JSON.stringify({ message: msg, audience: _admProState.broadcastAudience, test_only: true }),
        });
        const d = await res.json().catch(() => ({}));
        if (!res.ok) { showStudioToast(d.detail || "Test DM failed", "error"); return; }
        showStudioToast(d.message || _admL("မိမိ DM သို့ အစမ်းပို့ပြီးပါပြီ", "Test DM sent"), "success");
    } finally {
        if (testBtn) testBtn.disabled = false;
    }
}
window.sendTestBroadcast = sendTestBroadcast;

async function loadBroadcastHistory() {
    const box = document.getElementById("admp-bc-history");
    if (!box) return;
    try {
        const res = await fetchWithTimeout("/api/admin/broadcast/history?limit=15", { headers: getAuthHeaders() });
        if (!res.ok) return;
        const data = await res.json();
        const list = data.campaigns || [];
        if (!list.length) {
            box.innerHTML = `<p class="text-muted text-xs">${escapeHtml(_admL("မှတ်တမ်း မရှိသေးပါ", "No past campaigns yet"))}</p>`;
            return;
        }
        box.innerHTML = list.map(c => `<div class="admp-card-item">
            <div class="admp-between text-xs">
                <span class="admp-badge admp-badge-ok">✅ ${c.sent_count}/${c.total_recipients}</span>
                <span class="text-muted">${escapeHtml(admFmtYangon(c.created_at))}</span>
            </div>
            <div class="text-xs mt-1">${escapeHtml(c.snippet)}</div>
        </div>`).join("");
    } catch (e) {}
}
window.loadBroadcastHistory = loadBroadcastHistory;

async function adminSendBroadcast() {
    const textEl = document.getElementById("adm-broadcast-text");
    const btn = document.getElementById("adm-broadcast-btn");
    const btnText = document.getElementById("adm-broadcast-btn-text");
    const reportBox = document.getElementById("adm-broadcast-report");
    const msg = textEl ? textEl.value.trim() : "";
    if (!msg) { showStudioToast(t("admin_broadcast_empty"), "info"); return; }
    const audLabel = document.getElementById("admp-bc-aud")?.selectedOptions?.[0]?.textContent || _admProState.broadcastAudience;
    const nTxt = _admProState.broadcastDeliverable != null ? `${_admProState.broadcastDeliverable}` : "?";
    const confirmMsg = _admL(
        `📢 Broadcast ပို့ရန် အတည်ပြုချက်\n\n• ပစ်မှတ်: ${audLabel}\n• ပို့မည့်သူ: ${nTxt} ဦး\n• စာသား: "${msg.slice(0, 80)}${msg.length > 80 ? "…" : ""}"`,
        `Send broadcast to ${nTxt} recipients (${audLabel})?`
    );
    haptic("heavy");
    showStudioConfirm(confirmMsg, async () => {
        loadBroadcastPreview();
        if (btn) btn.disabled = true;
        if (btnText) btnText.textContent = t("admin_broadcast_sending");
        if (reportBox) {
            reportBox.classList.remove("hidden");
            reportBox.innerHTML = `<div class="adm-state"><span class="spinner"></span><span>${t("admin_broadcast_sending")}</span></div>`;
        }
        try {
            const res = await fetchWithTimeout("/api/admin/broadcast", {
                method: "POST",
                headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
                body: JSON.stringify({ message: msg, audience: _admProState.broadcastAudience, test_only: false }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || "Broadcast request failed");
            if (reportBox) {
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
            }
            showStudioToast(`${t("admin_bc_delivered")}: ${data.sent_count || 0} / ${data.total_targeted || 0}`, "success");
            if (textEl) {
                textEl.value = "";
                const c = document.getElementById("adm-broadcast-count");
                if (c) c.innerText = "0";
            }
            loadBroadcastHistory();
        } catch (e) {
            if (reportBox) reportBox.innerHTML = `<div class="adm-state adm-state-error"><span>❌ ${escapeHtml(e.message)}</span></div>`;
            showStudioToast(adminErrorText("admin_generic_err", e.message), "error");
        } finally {
            if (btn) btn.disabled = false;
            if (btnText) btnText.textContent = t("btn_send_broadcast");
        }
    });
}
window.adminSendBroadcast = adminSendBroadcast;
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
    const currPaused = _adminFleetData?.vps_worker?.status_code === "PAUSED";
    const nextStatus = currPaused ? "ONLINE" : "PAUSED";
    const confirmKey = nextStatus === "PAUSED" ? "adm_confirm_worker_pause" : "adm_confirm_worker_resume";
    showStudioConfirm(t(confirmKey), async () => {
        const btn = document.getElementById("adm-btn-toggle-vps");
        if (btn) btn.disabled = true;
        try {
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
            if (data.worker) {
                _adminFleetData = { vps_worker: data.worker };
                renderAdminFleetUI(_adminFleetData);
            }
        } catch (e) {
            showStudioToast(tf("adm_vps_toggle_failed", { error: e.message }), "error");
        } finally {
            if (btn) btn.disabled = false;
        }
    });
}

async function toggleRecapQueueRouting() {
    haptic("medium");
    const currOn = !!_adminFleetData?.vps_worker?.recap_on_queue;
    const nextOn = !currOn;
    const confirmKey = nextOn ? "adm_confirm_recap_route_on" : "adm_confirm_recap_route_off";
    showStudioConfirm(t(confirmKey), async () => {
        const btn = document.getElementById("adm-btn-toggle-recap-queue");
        if (btn) btn.disabled = true;
        try {
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
            if (data.worker) {
                _adminFleetData = { vps_worker: data.worker };
                renderAdminFleetUI(_adminFleetData);
            }
        } catch (e) {
            showStudioToast(tf("adm_recap_queue_failed", { error: e.message }), "error");
        } finally {
            if (btn) btn.disabled = false;
        }
    });
}

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
                loadAdminJobs();
            } else {
                showStudioToast(adminErrorText("admin_cancel_err", data.detail || data.message), 'error');
            }
        } catch (e) {
            showStudioToast(adminErrorText("admin_cancel_err", e.message), 'error');
        }
    });
}
window.adminCancelJob = adminCancelJob;
// ── Merged Jobs (Worker + Active Queue + History + Purge) ─────────────
let _adminFleetData = null;
const admJobsFilter = {
    status: "active",   // "active" | "SUCCESS" | "FAILED" | "all"
    timeframe: "24h",   // "24h" | "7d" | "all"
    job_type: "all",    // "all" | "RECAP" | "VOICE_CLONE" | "TTS"
    job_id: "",
};

function setAdminJobsStatus(status) {
    admJobsFilter.status = status || "active";
    document.querySelectorAll("#adm-jobs-status-filters .filter-pill").forEach(btn => {
        btn.classList.toggle("active", btn.getAttribute("data-jstatus") === admJobsFilter.status);
    });
    loadAdminJobs();
}
window.setAdminJobsStatus = setAdminJobsStatus;

function setAdminHistFilter(key, val) {
    if (key) admJobsFilter[key] = val;
    document.querySelectorAll('.filter-chip[data-filter-type="timeframe"]').forEach(c => {
        c.classList.toggle("active", c.getAttribute("data-val") === admJobsFilter.timeframe);
    });
    const selType = document.getElementById("adm-hist-type-sel");
    if (selType && selType.value !== admJobsFilter.job_type) selType.value = admJobsFilter.job_type;
    loadAdminJobs();
}
window.setAdminHistFilter = setAdminHistFilter;

function adminSearchJobId() {
    const el = document.getElementById("adm-hist-jobid");
    admJobsFilter.job_id = el ? el.value.trim() : "";
    loadAdminJobs();
}
window.adminSearchJobId = adminSearchJobId;

function renderAdminJobsData(data) {
    if (!data) return;
    const isQueueShape = Array.isArray(data.jobs);
    if (isQueueShape) window._lastAdminQueue = data;
    else window._lastAdminHistory = data;
    const list = isQueueShape ? data.jobs : (data.history || []);
    if (data.worker) {
        _adminFleetData = { vps_worker: data.worker };
        renderAdminFleetUI(_adminFleetData);
    }
    const c = data.counts || {};
    const activeCnt = c.active != null ? Number(c.active) : (Number(c.queued || 0) + Number(c.running || 0) + Number(c.waiting_choice || 0));
    _admQueueActiveCount = activeCnt;
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.innerText = v; };
    set("cnt-active", activeCnt);
    set("cnt-success", c.success ?? data.total_success ?? 0);
    set("cnt-failed", c.failed ?? data.total_failed ?? 0);
    set("cnt-total", c.window_total ?? c.total_listed ?? data.total_count ?? list.length);
    set("adm-queued-jobs", c.queued ?? activeCnt);

    const bannerEl = document.getElementById("admin-queue-status-banner");
    if (bannerEl) {
        const runningCnt = Number(c.running || 0);
        const queuedCnt = Number(c.queued || 0);
        if (queuedCnt === 0 && runningCnt === 0) {
            bannerEl.innerHTML = `<div class="active-queue-status-banner idle"><div class="status-banner-icon">🟢</div><div class="status-banner-info"><div class="status-banner-title">${t("admin_queue_idle_title")}</div><div class="status-banner-desc">${t("admin_queue_idle_desc")}</div></div></div>`;
        } else if (runningCnt > 0) {
            bannerEl.innerHTML = `<div class="active-queue-status-banner running"><div class="status-banner-icon pulsing">⚡</div><div class="status-banner-info"><div class="status-banner-title">${t("admin_queue_running_title")}</div><div class="status-banner-desc">${tf("admin_queue_running_desc", { running: runningCnt, queued: queuedCnt })}</div></div></div>`;
        } else {
            bannerEl.innerHTML = `<div class="active-queue-status-banner queued"><div class="status-banner-icon">⏳</div><div class="status-banner-info"><div class="status-banner-title">${t("admin_queue_queued_title")}</div><div class="status-banner-desc">${tf("admin_queue_queued_desc", { queued: queuedCnt })}</div></div></div>`;
        }
    }

    const container = document.getElementById("admin-queue-list");
    if (!container) return;
    if (!list.length) {
        admStateEmpty(container, admJobsFilter.status === "active" ? "admin_queue_empty" : "admin_history_empty");
        return;
    }

    container.innerHTML = list.map(j => {
        const stLower = (j.status || "queued").toLowerCase();
        const isRecap = (j.job_type === "RECAP" || j.engine === "recap");
        const engineLower = isRecap ? "recap" : "voxcpm";
        const engineLabel = isRecap ? t("adm_badge_recap") : t("adm_opt_engine_voxcpm");
        const hasDur = j.duration_seconds != null && Number.isFinite(Number(j.duration_seconds)) && Number(j.duration_seconds) > 0;
        const durBit = hasDur ? `⏱️ ${admFmtSecs(j.duration_seconds)} • ` : "";
        const createdLabel = admFmtYangon(j.created_at) || j.created_ago || j.time_ago || "—";
        const errText = j.error_message ? displayJobError(j.error_message) : "";
        const titleStr = `${j.username && j.username !== "Unknown" ? "@" + j.username : "ID: " + j.user_id} • ${j.voice_display || j.preset_voice || j.engine}`;

        let actionsHtml = "";
        if (stLower === "failed" || stLower === "cancelled") {
            actionsHtml += `<button class="queue-act-btn retry" id="btn-retry-${j.id}" onclick="adminRetryJob('${j.id}')">🔄 ${t("admin_retry")}</button>`;
        } else if (stLower === "queued" || stLower === "running" || stLower === "waiting_choice") {
            actionsHtml += `<button class="queue-act-btn cancel" onclick="adminCancelJob('${j.id}')">⏹️ ${t("admin_cancel_job")}</button>`;
        }
        if (stLower === "success" || stLower === "failed") {
            actionsHtml += `<button class="queue-act-btn" onclick="adminManualRefundJob('${escapeHtml(j.id)}')">↩️</button>`;
        }
        if (j.is_expired) {
            actionsHtml += `<span class="badge-expired text-xs">⌛ ${t("admin_expired")}</span>`;
            actionsHtml += `<button class="queue-act-btn retry" id="btn-retry-${j.id}" onclick="adminRetryJob('${j.id}')">🔄 ${t("admin_regen")}</button>`;
        } else if (j.download_url) {
            actionsHtml += `<button class="queue-act-btn uid adm-media-open-btn" data-job-id="${escapeHtml(j.id)}" data-job-type="${escapeHtml(j.job_type)}" data-download-url="${escapeHtml(j.download_url)}" data-deliverables="${escapeHtml(JSON.stringify(j.deliverables || {}))}" data-title="${escapeHtml(titleStr)}">${escapeHtml(t("adm_btn_play"))}</button>`;
            actionsHtml += `<a href="${signedAdminHref(j.download_url)}" target="_blank" rel="noopener" data-open-url="${escapeHtml(j.download_url)}" class="queue-act-btn uid" title="${escapeHtml(t("adm_open_stream_title"))}">↗️</a>`;
        }
        if (stLower !== "queued" && stLower !== "running" && stLower !== "waiting_choice") {
            actionsHtml += `<button class="queue-act-btn" onclick="adminDeleteSingleJob('${j.id}')">🗑️</button>`;
        }
        const hasRealPct = typeof j.progress_percent === "number" && Number.isFinite(j.progress_percent) && j.progress_percent > 0;
        const realProgressHtml = (stLower === "running" && hasRealPct)
            ? `<div class="vcp-job-progress mt-1"><div class="vcp-job-progress-fill" style="width:${Math.max(0, Math.min(100, Math.round(j.progress_percent)))}%"></div></div>`
            : "";
        const stageLine = (stLower === "running" || stLower === "queued" || stLower === "waiting_choice")
            ? `<div class="text-xs text-muted mt-1">${escapeHtml(j.progress_label || jobStatusText(j.status))}</div>${realProgressHtml}`
            : "";
        const userLabel = j.user_name || (j.first_name || (j.username && j.username !== "Unknown" ? "@" + j.username : "User " + j.user_id));
        return `<div class="queue-item vcp-job-card" id="adm-job-item-${j.id}">
            <div class="queue-item-header vcp-job-badges">
                <div class="queue-badges-left">
                    <span class="badge-status ${stLower}">${escapeHtml(jobStatusText(j.status))}</span>
                    <span class="badge-engine ${engineLower}">${escapeHtml(engineLabel)}</span>
                    <span class="mini-tag">${escapeHtml(j.voice_display || j.preset_voice || j.engine)}</span>
                    ${j.job_type === "TTS" ? `<span class="mini-tag">${escapeHtml(t("job_badge_tts"))}</span>` : ""}
                </div>
                <span class="queue-timing">${escapeHtml(durBit + createdLabel)}</span>
            </div>
            <div class="queue-item-meta">
                <span class="queue-user-pill" onclick="admFillUser(${j.user_id})">👤 ${escapeHtml(userLabel)}${j.username && j.username !== "Unknown" ? ` (@${escapeHtml(j.username)})` : ""} • ID: ${j.user_id}</span>
                <span>#${escapeHtml(String(j.id).slice(0, 8))}</span>
            </div>
            <div class="queue-item-body">${escapeHtml(j.input_snippet || t("adm_task_content_fallback"))}</div>
            ${stageLine}
            <div id="retry-status-box-${j.id}"></div>
            ${errText ? `<div class="queue-item-error">⚠️ ${escapeHtml(errText.slice(0, 200))}</div>` : ""}
            <div class="queue-item-footer vcp-job-footer-row">
                <span class="queue-timing">${escapeHtml(j.created_ago || j.time_ago || "")}</span>
                <div class="queue-actions-row" id="adm-actions-row-${j.id}">${actionsHtml}</div>
            </div>
        </div>`;
    }).join("");
    wireAdminMediaButtons(container);
}
window.renderAdminJobsData = renderAdminJobsData;
window.renderAdminQueueData = renderAdminJobsData;
window.renderAdminHistoryData = renderAdminJobsData;

async function loadAdminJobs(isSilent = false) {
    if (!state.user?.is_admin) return;
    const container = document.getElementById("admin-queue-list");
    if (!container) return;
    if (!isSilent && container.children.length === 0) admStateLoading(container);
    try {
        const tfVal = admJobsFilter.status === "active" ? "all" : (admJobsFilter.timeframe || "24h");
        const params = new URLSearchParams({
            timeframe: tfVal,
            status: admJobsFilter.status || "active",
            job_type: admJobsFilter.job_type || "all",
            limit: "100",
        });
        if (admJobsFilter.job_id) params.set("job_id", admJobsFilter.job_id);
        const res = await fetchWithTimeout(`/api/admin/history?${params.toString()}`, { headers: getAuthHeaders() });
        if (!res.ok) {
            if (!isSilent) admStateError(container, "loadAdminJobs()");
            return;
        }
        renderAdminJobsData(await res.json());
        scheduleAdminQueuePoll();
    } catch (e) {
        if (!isSilent) admStateError(container, "loadAdminJobs()");
    }
}
window.loadAdminJobs = loadAdminJobs;
window.loadAdminQueue = loadAdminJobs;
window.loadAdminHistory = loadAdminJobs;
async function adminRetryJob(jobId) {
    haptic("medium");
    showStudioConfirm(tf("adm_confirm_retry_job", { id: String(jobId || "").slice(0, 10) }), async () => {
        const itemEl = document.getElementById(`adm-job-item-${jobId}`);
        const retryBtn = document.getElementById(`btn-retry-${jobId}`) || document.getElementById(`btn-q-retry-${jobId}`) || document.getElementById(`btn-q-regen-${jobId}`);
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
                if (admCurrentSection !== "jobs" || admJobsFilter.status !== "active") {
                    pollAdminJobProgress(jobId);
                }
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
    });
}
window.adminRetryJob = adminRetryJob;

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
    else if (mode === "matching") confirmMsg = tf("admin_confirm_purge_matching", { timeframe: admJobsFilter.timeframe, status: admJobsFilter.status, job_type: admJobsFilter.job_type });
    else confirmMsg = t("admin_confirm_purge_generic");

    showStudioConfirm(confirmMsg, async () => {
        haptic("heavy");
        try {
            const res = await fetch("/api/admin/history/purge", {
                method: "POST",
                headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
                body: JSON.stringify({
                    mode: mode,
                    timeframe: admJobsFilter.timeframe,
                    status: admJobsFilter.status,
                    job_type: admJobsFilter.job_type,
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
// ── Activity (Audit Log) + Bank Accounts + Plan Creator (Settings) ─────
function setAdminAuditFilter(prefix) {
    _admProState.auditPrefix = prefix || "";
    _admProState.auditOffset = 0;
    loadAdminAudit(false);
}
window.setAdminAuditFilter = setAdminAuditFilter;

async function loadAdminAudit(append = false) {
    const box = document.getElementById("admin-audit-list");
    if (!box) return;
    if (!append && _diagState.audit.inFlight) return;
    _diagState.audit.inFlight = true;
    if (!append) {
        _admProState.auditOffset = 0;
        admStateLoading(box);
    }
    const sel = document.getElementById("admp-aud-prefix");
    if (sel) _admProState.auditPrefix = sel.value || "";
    const qEl = document.getElementById("admp-aud-q");
    if (qEl) _admProState.auditQ = (qEl.value || "").trim();
    const qs = new URLSearchParams({ limit: "40", offset: String(_admProState.auditOffset) });
    if (_admProState.auditPrefix) qs.set("action_prefix", _admProState.auditPrefix);
    if (_admProState.auditQ) qs.set("q", _admProState.auditQ);
    try {
        const res = await fetchWithTimeout(`/api/admin/audit-log?${qs.toString()}`, { headers: getAuthHeaders() });
        const rows = await res.json();
        if (!res.ok) throw new Error(rows.detail || "Failed");
        _diagState.audit.loaded = true;
        const arr = Array.isArray(rows) ? rows : [];
        const html = arr.map(a => `
            <div class="adm-audit-row">
                <div class="adm-audit-main">
                    <b>${escapeHtml(a.action)}</b>
                    <span class="text-muted">${escapeHtml(a.target_type || "")} ${escapeHtml(String(a.target_id || ""))}</span>
                </div>
                ${a.details && Object.keys(a.details).length ? `<div class="adm-audit-detail">${escapeHtml(JSON.stringify(a.details))}</div>` : ""}
                <div class="adm-audit-time">${admFmtDate(a.created_at)}</div>
            </div>`).join("");
        const prevBtn = document.getElementById("admp-audit-more");
        if (prevBtn) prevBtn.remove();
        if (append) {
            box.insertAdjacentHTML("beforeend", html);
        } else if (!arr.length) {
            admStateEmpty(box, "admin_audit_empty");
        } else {
            box.innerHTML = html;
        }
        if (arr.length >= 40) {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.id = "admp-audit-more";
            btn.className = "btn secondary-btn mt-2";
            btn.textContent = _admL("နောက်ထပ် ၄၀ ခု ပြမည်", "Load 40 More");
            btn.onclick = () => { _admProState.auditOffset += 40; loadAdminAudit(true); };
            box.appendChild(btn);
        }
    } catch (e) {
        admStateError(box, "loadAdminAudit()");
    } finally {
        _diagState.audit.inFlight = false;
    }
}
window.loadAdminAudit = loadAdminAudit;

function mountSettingsAuxUi() {
    const banksBody = document.getElementById("admp-banks-body");
    if (banksBody && !document.getElementById("admp-banks-list")) {
        banksBody.innerHTML = `
            <div class="admp-stack">
                <button type="button" class="btn secondary-btn" id="admp-btn-add-bank">➕ ${escapeHtml(_admL("အကောင့်အသစ်ထည့်", "Add Account"))}</button>
                <button type="button" class="mini-chip" id="admp-btn-refresh-banks">🔄 ${escapeHtml(t("btn_refresh"))}</button>
            </div>
            <p class="text-muted text-xs mt-1">${escapeHtml(_admL("KBZPay, WavePay အပြင် AYA, CB, Yoma, KBZ Bank, USDT စသည့် အကောင့်များ ထည့်သွင်း၊ ပိတ်ဖွင့်၊ QR တင်နိုင်ပါသည်။", "Add, toggle, or upload QR for unlimited Bank/Wallet accounts."))}</p>
            <div id="admp-bank-form-wrap" class="hidden admp-card-item mt-2">
                <div class="admp-between mb-2">
                    <strong class="text-xs" id="admp-bank-form-title">➕ ${escapeHtml(_admL("ငွေလက်ခံ အကောင့်အသစ်", "New Payment Account"))}</strong>
                    <button type="button" class="mini-chip" id="admp-bank-form-close">✕</button>
                </div>
                <div class="admp-row mb-2" id="admp-bank-presets"></div>
                <div class="admp-stack">
                    <div class="admp-field"><label class="input-label text-xs">${escapeHtml(_admL("ဘဏ် / Wallet အမည် *", "Provider / Bank *"))}</label><input type="text" id="admp-b-provider" class="studio-input text-xs" placeholder="KBZPay / WavePay / AYA Bank"></div>
                    <div class="admp-field"><label class="input-label text-xs">${escapeHtml(_admL("အမျိုးအစား", "Type"))}</label><select id="admp-b-type" class="studio-input text-xs"><option value="bank">🏦 ${escapeHtml(_admL("ဘဏ်အကောင့်", "Bank Account"))}</option><option value="mobile_wallet">💳 ${escapeHtml(_admL("မိုဘိုင်း Wallet", "Mobile Wallet"))}</option><option value="crypto">🪙 USDT / Crypto</option></select></div>
                    <div class="admp-field"><label class="input-label text-xs">${escapeHtml(_admL("အကောင့် / ဖုန်းနံပါတ် *", "Account / Phone No *"))}</label><input type="text" id="admp-b-number" class="studio-input text-xs" placeholder="09xxx / 0012-3456-..."></div>
                    <div class="admp-field"><label class="input-label text-xs">${escapeHtml(_admL("အကောင့်ပိုင်ရှင် အမည်", "Account Holder Name"))}</label><input type="text" id="admp-b-name" class="studio-input text-xs" placeholder="U / Daw ..."></div>
                    <div class="admp-field"><label class="input-label text-xs">${escapeHtml(_admL("ဘဏ်ခွဲ · မထည့်လည်းရ", "Branch · Optional"))}</label><input type="text" id="admp-b-branch" class="studio-input text-xs" placeholder="Yangon Main"></div>
                    <div class="admp-field"><label class="input-label text-xs">${escapeHtml(_admL("အစဉ်လိုက် နံပါတ်", "Display Order"))}</label><input type="number" id="admp-b-order" class="studio-input text-xs" value="30" min="1" max="999"></div>
                    <div class="admp-field"><label class="input-label text-xs">${escapeHtml(_admL("ကတ် အရောင်", "Card Accent Color"))}</label><input type="color" id="admp-b-color" value="#1D4ED8" style="width:100%;height:36px;border:none;background:none;cursor:pointer"></div>
                    <div class="admp-field"><label class="input-label text-xs">${escapeHtml(_admL("မှတ်ချက် · မထည့်လည်းရ", "Payment Note · Optional"))}</label><input type="text" id="admp-b-note" class="studio-input text-xs" placeholder="${escapeHtml(_admL("ဥပမာ - ငွေလွှဲမှတ်ချက်တွင် Telegram ID ထည့်ပါ", "e.g. Put Telegram ID in remark"))}"></div>
                    <div class="admp-toggle-row">
                        <span class="input-label text-xs">${escapeHtml(_admL("အသုံးပြုသူများ မြင်အောင် ဖွင့်ထားမည်", "Active · Visible to Users"))}</span>
                        <label class="switch-luxury"><input type="checkbox" id="admp-b-active" checked><span class="slider round"></span></label>
                    </div>
                    <button type="button" class="btn gold-btn" style="width:100%" id="admp-b-save">💾 ${escapeHtml(_admL("အကောင့် သိမ်းဆည်းမည်", "Save Account"))}</button>
                </div>
            </div>
            <div id="admp-banks-list" class="mt-2"><p class="text-muted text-xs">${escapeHtml(_admL("ရယူနေသည်…", "Loading…"))}</p></div>
        `;
        document.getElementById("admp-btn-add-bank").onclick = () => openBankForm(null);
        document.getElementById("admp-btn-refresh-banks").onclick = () => loadBankAccounts();
        document.getElementById("admp-bank-form-close").onclick = () => document.getElementById("admp-bank-form-wrap").classList.add("hidden");
        document.getElementById("admp-b-save").onclick = () => saveBankAccountForm();
    }

    const npWrap = document.getElementById("admp-new-plan-wrap");
    if (npWrap && !document.getElementById("admp-np-toggle")) {
        npWrap.innerHTML = `
            <div class="admp-between">
                <strong class="text-xs">➕ ${escapeHtml(_admL("ပရိုမို / Package အသစ် ဖန်တီးရန်", "Create New Promo Plan"))}</strong>
                <button type="button" class="mini-chip" id="admp-np-toggle">▾</button>
            </div>
            <div id="admp-np-body" class="hidden admp-stack mt-2">
                <div class="admp-field"><label class="input-label text-xs">${escapeHtml(_admL("Package ကုဒ် · အင်္ဂလိပ်စာလုံးအသေး", "Plan Code"))}</label><input type="text" id="admp-np-code" class="studio-input text-xs" placeholder="promo_newyear"></div>
                <div class="admp-field"><label class="input-label text-xs">${escapeHtml(_admL("Package အမည်", "Plan Name"))}</label><input type="text" id="admp-np-name" class="studio-input text-xs" placeholder="New Year Combo"></div>
                <div class="admp-field"><label class="input-label text-xs">${escapeHtml(_admL("အမျိုးအစား", "Category"))}</label><select id="admp-np-cat" class="studio-input text-xs"><option value="combo">Combo</option><option value="voice">Voice Clone</option><option value="recap">Recap Credits</option></select></div>
                <div class="admp-field"><label class="input-label text-xs">${escapeHtml(_admL("ဈေးနှုန်း · MMK", "Price · MMK"))}</label><input type="number" id="admp-np-price" class="studio-input text-xs" placeholder="15000"></div>
                <div class="admp-field"><label class="input-label text-xs">${escapeHtml(_admL("Voice မိနစ်", "Voice Minutes"))}</label><input type="number" id="admp-np-mins" class="studio-input text-xs" value="0"></div>
                <div class="admp-field"><label class="input-label text-xs">${escapeHtml(_admL("Recap Credit", "Recap Credits"))}</label><input type="number" id="admp-np-rc" class="studio-input text-xs" value="0"></div>
                <div class="admp-field"><label class="input-label text-xs">${escapeHtml(_admL("သက်တမ်း · ရက်", "Validity · Days"))}</label><input type="number" id="admp-np-days" class="studio-input text-xs" value="30"></div>
                <div class="admp-field"><label class="input-label text-xs">${escapeHtml(_admL("တံဆိပ် · မထည့်လည်းရ", "Badge · Optional"))}</label><input type="text" id="admp-np-badge" class="studio-input text-xs" placeholder="Promo 🔥"></div>
                <button type="button" class="btn gold-btn" style="width:100%" id="admp-np-save">➕ ${escapeHtml(_admL("Package အသစ် ထုတ်ဝေမည်", "Publish New Plan"))}</button>
            </div>`;
        document.getElementById("admp-np-toggle").onclick = () => document.getElementById("admp-np-body").classList.toggle("hidden");
        document.getElementById("admp-np-save").onclick = () => {
            const payload = {
                code: (document.getElementById("admp-np-code").value || "").trim(),
                name: (document.getElementById("admp-np-name").value || "").trim(),
                category: document.getElementById("admp-np-cat").value,
                price_mmk: Number(document.getElementById("admp-np-price").value || 0),
                minutes: Number(document.getElementById("admp-np-mins").value || 0),
                recap_credits: Number(document.getElementById("admp-np-rc").value || 0),
                validity_days: Number(document.getElementById("admp-np-days").value ?? 30),
                badge: (document.getElementById("admp-np-badge").value || "").trim(),
                is_active: true,
            };
            if (!payload.code || !payload.name || payload.price_mmk < 100) {
                showStudioToast(_admL("ကုဒ်၊ အမည် နှင့် ဈေးနှုန်း မှန်ကန်စွာ ဖြည့်ပါ", "Enter valid code, name, and price"), "warning");
                return;
            }
            const confirmMsg = _admL(
                `💰 Package အသစ် ထုတ်ဝေရန် အတည်ပြုချက်\n\n• အမည်: ${payload.name} (${payload.code})\n• ဈေးနှုန်း: ${admFmtMmk(payload.price_mmk)}\n• ပမာဏ: Voice ${payload.minutes}m · Recap ${payload.recap_credits} Credit`,
                `Publish live plan ${payload.name} (${payload.code}) at ${admFmtMmk(payload.price_mmk)}?`
            );
            _admAskConfirm(confirmMsg, async () => {
                const saveBtn = document.getElementById("admp-np-save");
                if (saveBtn) saveBtn.disabled = true;
                try {
                    const res = await fetchWithTimeout("/api/admin/pricing", {
                        method: "POST",
                        headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
                        body: JSON.stringify(payload),
                    });
                    const data = await res.json().catch(() => ({}));
                    if (!res.ok) { showStudioToast(data.detail || "Create plan failed", "error"); return; }
                    showStudioToast(data.message || _admL("ဖန်တီးပြီးပါပြီ", "Created"), "success");
                    document.getElementById("admp-np-body").classList.add("hidden");
                    loadAdminPricing();
                } finally {
                    if (saveBtn) saveBtn.disabled = false;
                }
            });
        };
    }

    const ordQ = document.getElementById("admp-ord-q");
    if (ordQ && !ordQ.dataset.admBound) {
        ordQ.dataset.admBound = "1";
        let timer = null;
        ordQ.addEventListener("input", () => {
            clearTimeout(timer);
            timer = setTimeout(() => loadAdminOrders(), 350);
        });
    }
    const ordDays = document.getElementById("admp-ord-days");
    if (ordDays && !ordDays.dataset.admBound) {
        ordDays.dataset.admBound = "1";
        ordDays.addEventListener("change", () => loadAdminOrders());
    }
    const audQ = document.getElementById("admp-aud-q");
    if (audQ && !audQ.dataset.admBound) {
        audQ.dataset.admBound = "1";
        let timer = null;
        audQ.addEventListener("input", () => {
            clearTimeout(timer);
            timer = setTimeout(() => loadAdminAudit(false), 350);
        });
    }
    const uSearch = document.getElementById("adm-user-search-q");
    if (uSearch && !uSearch.dataset.admBound) {
        uSearch.dataset.admBound = "1";
        uSearch.addEventListener("keydown", (e) => {
            if (e.key === "Enter") loadAdminUsersRecent(false);
        });
    }
}

function openBankForm(acc) {
    _admProState.editingAccId = acc ? acc.id : "";
    const wrap = document.getElementById("admp-bank-form-wrap");
    if (!wrap) return;
    wrap.classList.remove("hidden");
    document.getElementById("admp-bank-form-title").textContent = acc
        ? `✏️ ${_admL("အကောင့် ပြင်ဆင်ရန်", "Edit Account")}: ${acc.provider}`
        : `➕ ${_admL("ငွေလက်ခံ အကောင့်အသစ်", "New Payment Account")}`;
    document.getElementById("admp-b-provider").value = acc ? acc.provider : "";
    document.getElementById("admp-b-type").value = acc ? (acc.account_type || "bank") : "bank";
    document.getElementById("admp-b-number").value = acc ? acc.number : "";
    document.getElementById("admp-b-name").value = acc ? (acc.name || "") : "";
    document.getElementById("admp-b-branch").value = acc ? (acc.bank_branch || "") : "";
    document.getElementById("admp-b-color").value = (acc && /^#[0-9a-fA-F]{6}$/.test(acc.color || "")) ? acc.color : "#1D4ED8";
    document.getElementById("admp-b-order").value = acc ? (acc.sort_order ?? 30) : ((_admProState.accounts.length + 1) * 10);
    document.getElementById("admp-b-note").value = acc ? (acc.note || "") : "";
    document.getElementById("admp-b-active").checked = acc ? !!acc.is_active : true;
    const box = document.getElementById("admp-bank-presets");
    if (box) {
        const list = _admProState.presets.length ? _admProState.presets : [
            { provider: "KBZPay", account_type: "mobile_wallet", color: "#0047BA" },
            { provider: "WavePay", account_type: "mobile_wallet", color: "#EAB308" },
            { provider: "AYA Pay", account_type: "mobile_wallet", color: "#D92D20" },
            { provider: "CB Pay", account_type: "mobile_wallet", color: "#0284C7" },
            { provider: "KBZ Bank", account_type: "bank", color: "#1D4ED8" },
            { provider: "AYA Bank", account_type: "bank", color: "#B91C1C" },
            { provider: "CB Bank", account_type: "bank", color: "#0369A1" },
            { provider: "Yoma Bank", account_type: "bank", color: "#EA580C" },
            { provider: "USDT (TRC20)", account_type: "crypto", color: "#16A34A" },
        ];
        box.innerHTML = list.map((p, i) => `<button type="button" class="admp-preset-chip" data-idx="${i}" style="border-left:3px solid ${escapeHtml(p.color)}">${escapeHtml(p.provider)}</button>`).join("");
        box.querySelectorAll(".admp-preset-chip").forEach(btn => {
            btn.onclick = () => {
                const p = list[Number(btn.dataset.idx)];
                if (!p) return;
                document.getElementById("admp-b-provider").value = p.provider;
                document.getElementById("admp-b-type").value = p.account_type || "bank";
                document.getElementById("admp-b-color").value = p.color || "#1D4ED8";
            };
        });
    }
}
window.openBankForm = openBankForm;

async function loadBankAccounts() {
    mountSettingsAuxUi();
    const box = document.getElementById("admp-banks-list");
    if (!box) return;
    try {
        const res = await fetchWithTimeout("/api/admin/payment-accounts", { headers: getAuthHeaders() });
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();
        window._banksLoaded = true;
        _admProState.accounts = data.accounts || [];
        _admProState.presets = data.presets || [];
        if (!_admProState.accounts.length) {
            box.innerHTML = `<p class="text-muted text-xs">${escapeHtml(_admL("အကောင့် မရှိသေးပါ", "No accounts configured"))}</p>`;
            return;
        }
        box.innerHTML = _admProState.accounts.map(acc => {
            const col = /^#[0-9a-fA-F]{3,6}$/.test(acc.color || "") ? acc.color : "#0047BA";
            const ic = acc.account_type === "bank" ? "🏦" : (acc.account_type === "crypto" ? "🪙" : "💳");
            const stBadge = acc.is_active
                ? `<span class="admp-badge admp-badge-ok">● ${escapeHtml(_admL("ဖွင့်ထားသည်", "Active"))}</span>`
                : `<span class="admp-badge admp-badge-err">● ${escapeHtml(_admL("ပိတ်ထားသည်", "Disabled"))}</span>`;
            const br = acc.bank_branch ? ` · ${escapeHtml(acc.bank_branch)}` : "";
            const qrBadge = acc.has_qr ? `<span class="admp-badge admp-badge-ok">📷 QR ✓</span>` : "";
            return `<div class="admp-card-item admp-bank-item" style="border-left-color:${col}">
                <div class="admp-between">
                    <div><b>${ic} ${escapeHtml(acc.provider)}</b> <span class="text-xs text-muted">(${escapeHtml(acc.account_type || "bank")})</span></div>
                    <div class="admp-row">${qrBadge}${stBadge}</div>
                </div>
                <div class="text-xs mt-1"><b>${escapeHtml(acc.number)}</b> · ${escapeHtml(acc.name || "—")}${br}</div>
                ${acc.note ? `<div class="text-xs text-muted mt-1">💬 ${escapeHtml(acc.note)}</div>` : ""}
                <div class="admp-row mt-2">
                    <button type="button" class="mini-chip" data-bank-edit="${escapeHtml(acc.id)}">✏️ ${escapeHtml(_admL("ပြင်ဆင်မည်", "Edit"))}</button>
                    <button type="button" class="mini-chip" data-bank-qr="${escapeHtml(acc.id)}">📷 QR</button>
                    <button type="button" class="mini-chip" data-bank-toggle="${escapeHtml(acc.id)}">${acc.is_active ? `⏸️ ${_admL("ပိတ်မည်", "Disable")}` : `▶️ ${_admL("ဖွင့်မည်", "Enable")}`}</button>
                    <button type="button" class="mini-chip" style="color:#ef4444" data-bank-del="${escapeHtml(acc.id)}">🗑️</button>
                </div>
            </div>`;
        }).join("");
        box.querySelectorAll("[data-bank-edit]").forEach(b => {
            b.onclick = () => {
                const a = _admProState.accounts.find(x => x.id === b.dataset.bankEdit);
                if (a) openBankForm(a);
            };
        });
        box.querySelectorAll("[data-bank-toggle]").forEach(b => {
            b.onclick = async () => {
                const a = _admProState.accounts.find(x => x.id === b.dataset.bankToggle);
                if (!a) return;
                b.disabled = true;
                try {
                    const r = await fetchWithTimeout(`/api/admin/payment-accounts/${encodeURIComponent(a.id)}`, {
                        method: "PUT",
                        headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
                        body: JSON.stringify({ is_active: !a.is_active }),
                    });
                    if (!r.ok) { showStudioToast("Toggle failed", "error"); return; }
                    await loadBankAccounts();
                    if (typeof refreshPlanPrices === "function") refreshPlanPrices();
                } finally {
                    b.disabled = false;
                }
            };
        });
        box.querySelectorAll("[data-bank-qr]").forEach(b => {
            b.onclick = () => triggerQrUpload(b.dataset.bankQr);
        });
        box.querySelectorAll("[data-bank-del]").forEach(b => {
            b.onclick = () => {
                const a = _admProState.accounts.find(x => x.id === b.dataset.bankDel);
                if (a) confirmDeleteBank(a, b);
            };
        });
    } catch (err) {
        box.innerHTML = `<p class="text-xs text-danger">${escapeHtml(err.message)}</p>`;
    }
}
window.loadBankAccounts = loadBankAccounts;

async function saveBankAccountForm() {
    const provider = (document.getElementById("admp-b-provider")?.value || "").trim();
    const number = (document.getElementById("admp-b-number")?.value || "").trim();
    if (!provider || !number) {
        showStudioToast(_admL("ဘဏ်အမည်နှင့် အကောင့်နံပါတ် ထည့်ပါ", "Provider & Account Number required"), "warning");
        return;
    }
    const payload = {
        provider,
        account_type: document.getElementById("admp-b-type").value,
        number,
        name: (document.getElementById("admp-b-name").value || "").trim(),
        bank_branch: (document.getElementById("admp-b-branch").value || "").trim(),
        color: document.getElementById("admp-b-color").value || "#1D4ED8",
        sort_order: Number(document.getElementById("admp-b-order").value || 30),
        note: (document.getElementById("admp-b-note").value || "").trim(),
        is_active: !!document.getElementById("admp-b-active").checked,
    };
    const isEdit = !!_admProState.editingAccId;
    const url = isEdit ? `/api/admin/payment-accounts/${encodeURIComponent(_admProState.editingAccId)}` : "/api/admin/payment-accounts";
    const method = isEdit ? "PUT" : "POST";
    const saveBtn = document.getElementById("admp-b-save");
    if (saveBtn) saveBtn.disabled = true;
    try {
        const res = await fetchWithTimeout(url, {
            method,
            headers: { ...getAuthHeaders(), "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { showStudioToast(data.detail || "Save failed", "error"); return; }
        showStudioToast(data.message || _admL("သိမ်းဆည်းပြီးပါပြီ", "Saved"), "success");
        document.getElementById("admp-bank-form-wrap").classList.add("hidden");
        await loadBankAccounts();
        if (typeof refreshPlanPrices === "function") refreshPlanPrices();
    } finally {
        if (saveBtn) saveBtn.disabled = false;
    }
}

function confirmDeleteBank(acc, btnEl) {
    const msg = _admL(
        `🗑️ ${acc.provider} (${acc.number}) ကို ဖျက်ရန် အတည်ပြုပါသလား?\n\nဖျက်ပြီးပါက အသုံးပြုသူများ ငွေလွှဲရန် မြင်ရတော့မည် မဟုတ်ပါ။`,
        `Delete payment account ${acc.provider} (${acc.number})?`
    );
    _admAskConfirm(msg, async () => {
        if (btnEl) btnEl.disabled = true;
        try {
            const res = await fetchWithTimeout(`/api/admin/payment-accounts/${encodeURIComponent(acc.id)}`, {
                method: "DELETE",
                headers: getAuthHeaders(),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) { showStudioToast(data.detail || "Cannot delete", "error"); return; }
            showStudioToast(data.message || _admL("ဖျက်ပြီးပါပြီ", "Deleted"), "success");
            await loadBankAccounts();
            if (typeof refreshPlanPrices === "function") refreshPlanPrices();
        } finally {
            if (btnEl) btnEl.disabled = false;
        }
    });
}

function triggerQrUpload(accId) {
    const inp = document.createElement("input");
    inp.type = "file";
    inp.accept = "image/png,image/jpeg,image/webp";
    inp.onchange = async () => {
        const f = inp.files && inp.files[0];
        if (!f) return;
        const fd = new FormData();
        fd.append("file", f);
        const res = await fetchWithTimeout(`/api/admin/payment-accounts/${encodeURIComponent(accId)}/qr`, {
            method: "POST",
            headers: getAuthHeaders(false),
            body: fd,
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { showStudioToast(data.detail || "QR upload failed", "error"); return; }
        showStudioToast(_admL("QR ပုံ တင်ပြီးပါပြီ", "QR image uploaded"), "success");
        await loadBankAccounts();
    };
    inp.click();
}
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
        <div class="adm-price-row adm-price-card admp-stack">
            <div class="adm-price-main">
                <div class="adm-price-head">
                    <div class="adm-price-title">${escapeHtml(titleText)}</div>
                    <span class="adm-price-cat-badge">${escapeHtml(catText)}</span>
                </div>
                <div class="adm-price-meta">
                    <span class="adm-price-code">${safeCode}</span>
                </div>
                <div class="admp-stack mt-2">
                    <div class="admp-field">
                        <label class="input-label text-xs" for="adm-min-${safeCode}">${escapeHtml(t("adm_price_fld_min"))}</label>
                        <input type="number" class="studio-input" id="adm-min-${safeCode}" value="${p.minutes || 0}" min="0">
                    </div>
                    <div class="admp-field">
                        <label class="input-label text-xs" for="adm-cr-${safeCode}">${escapeHtml(t("adm_price_fld_cr"))}</label>
                        <input type="number" class="studio-input" id="adm-cr-${safeCode}" value="${p.recap_credits || 0}" min="0">
                    </div>
                    <div class="admp-field">
                        <label class="input-label text-xs" for="adm-vdays-${safeCode}">${escapeHtml(t("adm_price_fld_vdays"))}</label>
                        <input type="number" class="studio-input" id="adm-vdays-${safeCode}" value="${p.validity_days ?? 30}" min="0" max="3650">
                    </div>
                    <div class="admp-toggle-row">
                        <span class="input-label text-xs">${escapeHtml(t("adm_price_fld_active"))}</span>
                        <label class="switch-luxury">
                            <input type="checkbox" id="adm-act-${safeCode}" ${p.is_active ? "checked" : ""}>
                            <span class="slider round"></span>
                        </label>
                    </div>
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
    if (!box || window._adminPricingLoading) return;
    window._adminPricingLoading = true;
    admStateLoading(box);
    try {
        const res = await fetchWithTimeout("/api/admin/pricing", { headers: getAuthHeaders() });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Failed");
        window._adminPricingLoaded = true;
        renderAdminPricingList(data.plans || []);
    } catch (e) {
        admStateError(box, "loadAdminPricing()");
    } finally {
        window._adminPricingLoading = false;
    }
}
window.loadAdminPricing = loadAdminPricing;

async function adminSavePrice(code) {
    const price = parseInt(document.getElementById("adm-price-" + code)?.value || "0", 10) || 0;
    const minutes = parseInt(document.getElementById("adm-min-" + code)?.value || "0", 10) || 0;
    const recap_credits = parseInt(document.getElementById("adm-cr-" + code)?.value || "0", 10) || 0;
    const rawVdays = String(document.getElementById("adm-vdays-" + code)?.value ?? "").trim();
    const parsedVdays = rawVdays === "" ? 30 : parseInt(rawVdays, 10);
    const validity_days = Number.isFinite(parsedVdays) ? parsedVdays : 30;
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
        const wasOpen = acc.open;
        acc.open = true;
        if (wasOpen && !window._adminPricingLoaded && typeof loadAdminPricing === "function") {
            loadAdminPricing();
        }
        setTimeout(() => acc.scrollIntoView({ behavior: "smooth", block: "start" }), 120);
    }
}
window.openAdminPricingShortcut = openAdminPricingShortcut;

async function loadAdminAnalytics(days = 30) {
    window._admAnalyticsLoadedAt = Date.now();
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
                <div class="card glass stat-mini-card"><span class="stat-lbl">${escapeHtml(t("adm_profit_lbl"))}</span><span class="stat-num ${(c.estimated_gross_profit_mmk || 0) >= 0 ? "text-green" : "text-danger"}">${(c.estimated_gross_profit_mmk || 0).toLocaleString()} MMK</span></div>
                <div class="card glass stat-mini-card"><span class="stat-lbl">${escapeHtml(t("adm_cost_lbl"))}</span><span class="stat-num">$${(c.total_cost_usd || 0).toFixed(2)} (${(c.total_cost_mmk || 0).toLocaleString()} MMK)</span></div>
                <div class="card glass stat-mini-card"><span class="stat-lbl">${escapeHtml(t("adm_arpu_lbl"))}</span><span class="stat-num" style="font-size:13px;">${(c.arpu_mmk || 0).toLocaleString()} / ${(c.avg_cost_job_mmk ?? c.avg_cost_per_job_mmk ?? 0).toLocaleString()} MMK</span></div>
            </div>
            <div class="mt-2 text-xs text-muted">Jobs: ${usg.total_jobs || 0} (✓${usg.success_jobs || 0} / ✗${usg.failed_jobs || 0}) • ${admFmtCredits(usg.total_recap_credits_consumed || 0)} Cr, ${admFmtCredits(usg.total_voice_minutes_consumed || 0)}m • Refunds: ${admFmtCredits(usg.refunded_recap_credits || 0)} Cr</div>
            <div class="adm-cost-row mt-2"><span>${escapeHtml(t("adm_exp_lbl"))} (≤7d: <b>${exp.expiring_7d_count || 0}</b> lots)</span><b>-${admFmtCredits(exp.expired_recap_credits || 0)} Cr · -${admFmtCredits(exp.expired_voice_minutes || 0)}m</b></div>
            ${top ? `<div class="mt-2"><div class="text-xs font-bold">${escapeHtml(t("adm_top_pkg_lbl"))}</div>${topPkgsHtml}</div>` : ""}
            <div class="mt-2">
                <div class="text-xs font-bold">${escapeHtml(t("adm_infra_lbl"))}</div>
                <div class="adm-cost-row"><span>Gemini Text (${(tel.gemini_input_tokens || 0) + (tel.gemini_output_tokens || 0)} tok)</span><b>$${(c.gemini_text_usd || 0).toFixed(2)}</b></div>
                <div class="adm-cost-row"><span>Gemini TTS</span><b>$${(c.gemini_tts_usd || 0).toFixed(2)}</b></div>
                <div class="adm-cost-row"><span>GPU / VoxCPM (${Math.round(tel.gpu_runtime_seconds || 0)}s)</span><b>$${(c.gpu_usd || 0).toFixed(2)}</b></div>
                <div class="adm-cost-row"><span>Storage (${(tel.storage_gb || 0).toFixed(2)} GB) + BW</span><b>$${((c.storage_usd || 0) + (c.bandwidth_usd || 0)).toFixed(2)}</b></div>
            </div>`;
    } catch (e) { admStateError(body, `loadAdminAnalytics(${days})`); }
}
window.loadAdminAnalytics = loadAdminAnalytics;
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
                <b>+${admFmtCredits(rf.recap_credits_refunded)} Cr · +${admFmtCredits(rf.voice_minutes_refunded)}m</b>
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

        const elTutLink = document.getElementById("adm-set-tutorial-link");
        const elMaint = document.getElementById("adm-set-maintenance");
        const elMaintNote = document.getElementById("adm-set-maintenance-note");
        const elCharLimitFree = document.getElementById("adm-set-char-limit-free");
        const elCharLimitPurchased = document.getElementById("adm-set-char-limit-purchased");
        const elFreeAllowance = document.getElementById("adm-set-free-allowance");
        const elRecapFreeMax = document.getElementById("adm-set-recap-free-max");
        const elRecapMax = document.getElementById("adm-set-recap-max");
        const elTtsDailyFree = document.getElementById("adm-set-tts-daily-free");
        const elTtsDailyPaid = document.getElementById("adm-set-tts-daily-paid");
        const elVoxcpmTimeout = document.getElementById("adm-set-voxcpm-timeout");
        const elReqChannelId = document.getElementById("adm-set-required-channel-id");
        const elReqGroupId = document.getElementById("adm-set-required-group-id");
        const elChannelInvite = document.getElementById("adm-set-channel-invite-link");
        const elGroupInvite = document.getElementById("adm-set-group-invite-link");

        const elTutVoiceEn = document.getElementById("adm-tut-voice-enabled");
        const elTutVoiceTitle = document.getElementById("adm-tut-voice-title");
        const elTutVoiceLink = document.getElementById("adm-tut-voice-link");

        const elTutRecapEn = document.getElementById("adm-tut-recap-enabled");
        const elTutRecapLink = document.getElementById("adm-tut-recap-link");
        const elRecapDemoAsset = document.getElementById("adm-recap-demo-asset");

        _admPaintValue(elTutLink, s.tutorial_video_link || "");
        _admPaintChecked(elMaint, !!s.maintenance_mode);
        _admPaintValue(elMaintNote, s.maintenance_note || "");
        const elGroupMaster = document.getElementById("adm-group-ai-master-toggle");
        _admPaintChecked(elGroupMaster, !!s.group_ai_admin_enabled);
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
        if (s.free_daily_allowance_seconds != null) _admPaintValue(elFreeAllowance, s.free_daily_allowance_seconds);
        // Gemini pool taste (Free + Paid) + global FREE budget fields.
        const elFreeGeminiTaste = document.getElementById("adm-set-free-gemini-taste");
        const elPaidGeminiTaste = document.getElementById("adm-set-paid-gemini-taste");
        const elFreeGeminiBudget = document.getElementById("adm-set-free-gemini-budget");
        if (s.free_gemini_taste_minutes != null) _admPaintValue(elFreeGeminiTaste, s.free_gemini_taste_minutes);
        if (s.paid_gemini_taste_minutes != null) _admPaintValue(elPaidGeminiTaste, s.paid_gemini_taste_minutes);
        if (s.free_gemini_daily_budget_requests != null) _admPaintValue(elFreeGeminiBudget, s.free_gemini_daily_budget_requests);
        if (s.recap_free_max_duration_seconds != null) _admPaintValue(elRecapFreeMax, s.recap_free_max_duration_seconds);
        if (s.recap_max_duration_seconds != null) _admPaintValue(elRecapMax, s.recap_max_duration_seconds);
        if (s.tts_daily_limit_minutes_free != null) _admPaintValue(elTtsDailyFree, s.tts_daily_limit_minutes_free);
        if (s.tts_daily_limit_minutes_paid != null) _admPaintValue(elTtsDailyPaid, s.tts_daily_limit_minutes_paid);
        if (s.voxcpm_timeout != null) _admPaintValue(elVoxcpmTimeout, s.voxcpm_timeout);
        _admPaintValue(elReqChannelId, s.required_channel_id || "");
        _admPaintValue(elReqGroupId, s.required_group_id || "");
        _admPaintValue(elChannelInvite, s.channel_invite_link || "");
        _admPaintValue(elGroupInvite, s.group_invite_link || "");

        _admPaintChecked(elTutVoiceEn, String(s.tutorial_voice_enabled).toLowerCase() !== "false" && s.tutorial_voice_enabled !== false);
        _admPaintValue(elTutVoiceTitle, tutorialDisplayTitle(s.tutorial_voice_title, "tutorial_default_voice_title"));
        _admPaintValue(elTutVoiceLink, s.tutorial_voice_link || s.tutorial_video_link || "");

        _admPaintChecked(elTutRecapEn, String(s.tutorial_recap_enabled).toLowerCase() !== "false" && s.tutorial_recap_enabled !== false);
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

        // Advanced Runtime Ops (#admp-runtime-card) painted from the same single GET /api/admin/settings payload
        const elRtConc = document.getElementById("admp-rt-conc");
        const elRtTimeout = document.getElementById("admp-rt-timeout");
        const elRtProfile = document.getElementById("admp-rt-profile");
        const elRtFx = document.getElementById("admp-rt-fx");
        if (s.recap_worker_concurrency != null) _admPaintValue(elRtConc, s.recap_worker_concurrency);
        if (s.recap_stale_job_timeout_seconds != null) _admPaintValue(elRtTimeout, s.recap_stale_job_timeout_seconds);
        if (s.ai_prompt_profile != null) _admPaintValue(elRtProfile, s.ai_prompt_profile);
        if (s.usd_to_mmk_rate != null) _admPaintValue(elRtFx, s.usd_to_mmk_rate);

        updateTutorialUI(s);
        if (document.getElementById("adm-acc-ai-bots")?.open) {
            await loadAdminGroupAiConsole();
        }
        _snapshotAdminSettingsValues();
        updateAdminSettingsSaveBar(true);
    } catch (e) {
        console.warn("Could not load dynamic settings:", e);
    }
}

function setSelectOrCustom(selectId, customInputId, val) {
    // Batch BY: same dirty guard as the rest of the settings pane —
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

function _isSettingsElDirty(elId) {
    const el = document.getElementById(elId);
    if (!el) return false;
    if (adminSettingsDirty.has(elId)) {
        if (!(elId in _adminSettingsSavedSnapshot)) return true;
    }
    if (!(elId in _adminSettingsSavedSnapshot)) return false;
    const saved = _adminSettingsSavedSnapshot[elId];
    return (el.type === "checkbox" || el.type === "radio") ? (el.checked !== saved) : (el.value !== saved);
}

async function saveAdminSettings() {
    haptic("medium");
    const tutLink = document.getElementById("adm-set-tutorial-link")?.value?.trim();
    const maint = !!document.getElementById("adm-set-maintenance")?.checked;
    const maintNote = document.getElementById("adm-set-maintenance-note")?.value?.trim() || "";
    const groupAi = !!document.getElementById("adm-group-ai-master-toggle")?.checked;
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
    const freeGeminiTasteRaw = document.getElementById("adm-set-free-gemini-taste")?.value?.trim();
    const freeGeminiTaste = freeGeminiTasteRaw === "" || freeGeminiTasteRaw == null ? null : parseInt(freeGeminiTasteRaw, 10);
    const paidGeminiTasteRaw = document.getElementById("adm-set-paid-gemini-taste")?.value?.trim();
    const paidGeminiTaste = paidGeminiTasteRaw === "" || paidGeminiTasteRaw == null ? null : parseInt(paidGeminiTasteRaw, 10);
    const freeGeminiBudgetRaw = document.getElementById("adm-set-free-gemini-budget")?.value?.trim();
    const freeGeminiBudget = freeGeminiBudgetRaw === "" || freeGeminiBudgetRaw == null ? null : parseInt(freeGeminiBudgetRaw, 10);
    const recapFreeMaxRaw = document.getElementById("adm-set-recap-free-max")?.value?.trim();
    const recapFreeMax = recapFreeMaxRaw === "" || recapFreeMaxRaw == null ? null : parseInt(recapFreeMaxRaw, 10);
    const recapMaxRaw = document.getElementById("adm-set-recap-max")?.value?.trim();
    const recapMax = recapMaxRaw === "" || recapMaxRaw == null ? null : parseInt(recapMaxRaw, 10);
    const ttsDailyFreeRaw = document.getElementById("adm-set-tts-daily-free")?.value?.trim();
    const ttsDailyFree = ttsDailyFreeRaw === "" || ttsDailyFreeRaw == null ? null : parseInt(ttsDailyFreeRaw, 10);
    const ttsDailyPaidRaw = document.getElementById("adm-set-tts-daily-paid")?.value?.trim();
    const ttsDailyPaid = ttsDailyPaidRaw === "" || ttsDailyPaidRaw == null ? null : parseInt(ttsDailyPaidRaw, 10);
    const voxcpmTimeoutRaw = document.getElementById("adm-set-voxcpm-timeout")?.value?.trim();
    const voxcpmTimeout = voxcpmTimeoutRaw === "" || voxcpmTimeoutRaw == null ? null : parseInt(voxcpmTimeoutRaw, 10);
    const reqChannelId = document.getElementById("adm-set-required-channel-id")?.value?.trim() || "";
    const reqGroupId = document.getElementById("adm-set-required-group-id")?.value?.trim() || "";
    const channelInvite = document.getElementById("adm-set-channel-invite-link")?.value?.trim() || "";
    const groupInvite = document.getElementById("adm-set-group-invite-link")?.value?.trim() || "";

    const tutVoiceEn = !!document.getElementById("adm-tut-voice-enabled")?.checked;
    const tutVoiceTitle = document.getElementById("adm-tut-voice-title")?.value?.trim() || t("tutorial_default_voice_title");
    const tutVoiceLink = document.getElementById("adm-tut-voice-link")?.value?.trim() || "";

    const tutRecapEn = !!document.getElementById("adm-tut-recap-enabled")?.checked;
    const tutRecapLink = document.getElementById("adm-tut-recap-link")?.value?.trim() || "";
    const recapDemoAsset = document.getElementById("adm-recap-demo-asset")?.value?.trim() || "";

    const rtConcRaw = document.getElementById("admp-rt-conc")?.value?.trim();
    const rtConc = rtConcRaw === "" || rtConcRaw == null ? null : parseInt(rtConcRaw, 10);
    const rtTimeoutRaw = document.getElementById("admp-rt-timeout")?.value?.trim();
    const rtTimeout = rtTimeoutRaw === "" || rtTimeoutRaw == null ? null : parseInt(rtTimeoutRaw, 10);
    const rtProfile = document.getElementById("admp-rt-profile")?.value?.trim() || "";
    const rtFxRaw = document.getElementById("admp-rt-fx")?.value?.trim();
    const rtFx = rtFxRaw === "" || rtFxRaw == null ? null : parseFloat(rtFxRaw);

    const tutExtraLinks = {};
    for (const [elId, settingKey] of ADMIN_TUTORIAL_LINK_FIELDS) {
        tutExtraLinks[settingKey] = document.getElementById(elId)?.value?.trim() || "";
    }

    const fullPayload = {
        tutorial_video_link: tutLink,
        tutorial_voice_enabled: tutVoiceEn,
        tutorial_voice_title: tutVoiceTitle,
        tutorial_voice_link: tutVoiceLink,
        tutorial_recap_enabled: tutRecapEn,
        tutorial_recap_link: tutRecapLink,
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
        ...(Number.isFinite(freeAllowance) ? { free_daily_allowance_seconds: freeAllowance } : {}),
        ...(Number.isFinite(freeGeminiTaste) ? { free_gemini_taste_minutes: freeGeminiTaste } : {}),
        ...(Number.isFinite(paidGeminiTaste) ? { paid_gemini_taste_minutes: paidGeminiTaste } : {}),
        ...(Number.isFinite(freeGeminiBudget) ? { free_gemini_daily_budget_requests: freeGeminiBudget } : {}),
        ...(Number.isFinite(recapFreeMax) ? { recap_free_max_duration_seconds: recapFreeMax } : {}),
        ...(Number.isFinite(recapMax) ? { recap_max_duration_seconds: recapMax } : {}),
        ...(Number.isFinite(ttsDailyFree) ? { tts_daily_limit_minutes_free: ttsDailyFree } : {}),
        ...(Number.isFinite(ttsDailyPaid) ? { tts_daily_limit_minutes_paid: ttsDailyPaid } : {}),
        ...(Number.isFinite(voxcpmTimeout) ? { voxcpm_timeout: voxcpmTimeout } : {}),
        required_channel_id: reqChannelId,
        required_group_id: reqGroupId,
        channel_invite_link: channelInvite,
        group_invite_link: groupInvite,
        ..._collectWatermarkPayload(),
        ...(Number.isFinite(rtConc) ? { recap_worker_concurrency: rtConc } : {}),
        ...(Number.isFinite(rtTimeout) ? { recap_stale_job_timeout_seconds: rtTimeout } : {}),
        ...(rtProfile ? { ai_prompt_profile: rtProfile } : {}),
        ...(Number.isFinite(rtFx) ? { usd_to_mmk_rate: rtFx } : {}),
        gemini_primary_model: getSelectOrCustom("adm-gemini-primary", "adm-gemini-primary-custom", "gemini-3.6-flash"),
        gemini_fallback_1: getSelectOrCustom("adm-gemini-fb1", "adm-gemini-fb1-custom", "gemini-3.5-flash"),
        gemini_fallback_2: getSelectOrCustom("adm-gemini-fb2", "adm-gemini-fb2-custom", "gemini-3.5-flash-lite"),
        gemini_fallback_3: getSelectOrCustom("adm-gemini-fb3", "adm-gemini-fb3-custom", "gemini-flash-lite-latest"),
        groq_whisper_model: document.getElementById("adm-groq-whisper-model")?.value?.trim() || "",
        gemini_tts_primary_model: getSelectOrCustom("adm-tts-primary", "adm-tts-primary-custom", "gemini-3.8-flash-tts"),
        gemini_tts_fallback_1: getSelectOrCustom("adm-tts-fb1", "adm-tts-fb1-custom", ""),
        gemini_tts_lite_model: getSelectOrCustom("adm-tts-lite", "adm-tts-lite-custom", "gemini-3.8-flash-lite-tts"),
        gemini_tts_fallback_2: getSelectOrCustom("adm-tts-fb2", "adm-tts-fb2-custom", ""),
    };
    const keyToIds = {
        tutorial_video_link: ["adm-set-tutorial-link"],
        tutorial_voice_enabled: ["adm-tut-voice-enabled"],
        tutorial_voice_title: ["adm-tut-voice-title"],
        tutorial_voice_link: ["adm-tut-voice-link"],
        tutorial_recap_enabled: ["adm-tut-recap-enabled"],
        tutorial_recap_link: ["adm-tut-recap-link"],
        recap_demo_asset: ["adm-recap-demo-asset"],
        maintenance_mode: ["adm-set-maintenance"],
        maintenance_note: ["adm-set-maintenance-note"],
        group_ai_admin_enabled: ["adm-group-ai-master-toggle"],
        group_ai_user_cooldown_seconds: ["adm-set-group-ai-cooldown"],
        group_ai_hourly_budget: ["adm-set-group-ai-hourly"],
        group_ai_daily_budget: ["adm-set-group-ai-daily"],
        group_ai_moderation_enabled: ["adm-group-ai-moderation-toggle"],
        group_ai_moderation_confidence_threshold: ["adm-group-ai-mod-threshold"],
        group_ai_moderation_mute_duration_strike2_seconds: ["adm-group-ai-mute-s2"],
        group_ai_moderation_mute_duration_strike3_seconds: ["adm-group-ai-mute-s3"],
        group_ai_moderation_whitelist: ["adm-group-ai-whitelist"],
        group_ai_voice_enabled: ["adm-group-ai-voice-toggle"],
        group_ai_voice_daily_cap: ["adm-group-ai-voice-cap"],
        private_ai_enabled: ["adm-set-private-ai"],
        private_ai_user_cooldown_seconds: ["adm-set-private-ai-cooldown"],
        private_ai_hourly_budget: ["adm-set-private-ai-hourly"],
        private_ai_daily_budget: ["adm-set-private-ai-daily"],
        character_limit_free: ["adm-set-char-limit-free"],
        character_limit_purchased: ["adm-set-char-limit-purchased"],
        free_daily_allowance_seconds: ["adm-set-free-allowance"],
        free_gemini_taste_minutes: ["adm-set-free-gemini-taste"],
        paid_gemini_taste_minutes: ["adm-set-paid-gemini-taste"],
        free_gemini_daily_budget_requests: ["adm-set-free-gemini-budget"],
        recap_free_max_duration_seconds: ["adm-set-recap-free-max"],
        recap_max_duration_seconds: ["adm-set-recap-max"],
        tts_daily_limit_minutes_free: ["adm-set-tts-daily-free"],
        tts_daily_limit_minutes_paid: ["adm-set-tts-daily-paid"],
        voxcpm_timeout: ["adm-set-voxcpm-timeout"],
        required_channel_id: ["adm-set-required-channel-id"],
        required_group_id: ["adm-set-required-group-id"],
        channel_invite_link: ["adm-set-channel-invite-link"],
        group_invite_link: ["adm-set-group-invite-link"],
        watermark_free_enabled: ["adm-wm-free-enabled"],
        watermark_paid_enabled: ["adm-wm-paid-enabled"],
        watermark_style: ["adm-wm-style"],
        watermark_position: ["adm-wm-position"],
        watermark_opacity: ["adm-wm-opacity"],
        watermark_size: ["adm-wm-size"],
        recap_worker_concurrency: ["admp-rt-conc"],
        recap_stale_job_timeout_seconds: ["admp-rt-timeout"],
        ai_prompt_profile: ["admp-rt-profile"],
        usd_to_mmk_rate: ["admp-rt-fx"],
        gemini_primary_model: ["adm-gemini-primary", "adm-gemini-primary-custom"],
        gemini_fallback_1: ["adm-gemini-fb1", "adm-gemini-fb1-custom"],
        gemini_fallback_2: ["adm-gemini-fb2", "adm-gemini-fb2-custom"],
        gemini_fallback_3: ["adm-gemini-fb3", "adm-gemini-fb3-custom"],
        groq_whisper_model: ["adm-groq-whisper-model"],
        gemini_tts_primary_model: ["adm-tts-primary", "adm-tts-primary-custom"],
        gemini_tts_fallback_1: ["adm-tts-fb1", "adm-tts-fb1-custom"],
        gemini_tts_lite_model: ["adm-tts-lite", "adm-tts-lite-custom"],
        gemini_tts_fallback_2: ["adm-tts-fb2", "adm-tts-fb2-custom"],
    };
    for (const [elId, settingKey] of ADMIN_TUTORIAL_LINK_FIELDS) {
        keyToIds[settingKey] = [elId];
    }

    const hasSnapshot = Object.keys(_adminSettingsSavedSnapshot).length > 0;
    const payload = {};
    for (const [key, val] of Object.entries(fullPayload)) {
        if (val === undefined) continue;
        const ids = keyToIds[key] || [];
        const dirty = !hasSnapshot || !ids.length || ids.some(id => _isSettingsElDirty(id));
        if (dirty) payload[key] = val;
    }

    if (!Object.keys(payload).length) {
        showStudioToast(t("adm_settings_no_changes"), "info");
        updateAdminSettingsSaveBar(true);
        return;
    }

    updateTutorialUI(fullPayload);

    const doSaveSettings = async () => {
        const saveBtn = document.getElementById("btn-save-admin-settings");
        const rtSaveBtn = document.getElementById("admp-rt-save");
        if (saveBtn) saveBtn.disabled = true;
        if (rtSaveBtn) rtSaveBtn.disabled = true;
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
            adminSettingsDirty.clear();
            _snapshotAdminSettingsValues();
            updateAdminSettingsSaveBar(true);
            showStudioToast(data.message || t("admin_settings_saved"), 'success');
            loadAdminSettings();
        } catch (e) {
            showStudioToast(adminErrorText("admin_settings_save_err", e.message), 'error');
        } finally {
            if (saveBtn) saveBtn.disabled = false;
            if (rtSaveBtn) rtSaveBtn.disabled = false;
        }
    };

    const emptyLbl = t("adm_empty_val");
    const warnLines = [];
    if (payload.maintenance_mode === true) {
        warnLines.push(t("adm_warn_maint_on"));
    }
    if ("required_channel_id" in payload) {
        warnLines.push(tf("adm_warn_chan_change", {
            oldVal: _adminSettingsSavedSnapshot["adm-set-required-channel-id"] || emptyLbl,
            newVal: payload.required_channel_id || emptyLbl,
        }));
    }
    if ("required_group_id" in payload) {
        warnLines.push(tf("adm_warn_group_change", {
            oldVal: _adminSettingsSavedSnapshot["adm-set-required-group-id"] || emptyLbl,
            newVal: payload.required_group_id || emptyLbl,
        }));
    }

    if (warnLines.length) {
        showStudioConfirm(
            tf("adm_confirm_critical_settings", { lines: warnLines.join("\n") }),
            doSaveSettings
        );
    } else {
        await doSaveSettings();
    }
}
window.saveAdminSettings = saveAdminSettings;

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

function formatIsoTime(isoStr) {
    return admFmtYangon(isoStr);
}

async function loadAdminGroupAiConsole(force = false) {
    if (!force && (window._groupAiLoading || window._groupAiLoaded)) return;
    window._groupAiLoading = true;
    try {
        const res = await fetch("/api/admin/group-ai", {
            headers: getAuthHeaders(),
        });
        if (!res.ok) return;
        const data = await res.json();
        window._groupAiLoaded = true;

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
                const _fmtManualDur = (sec) => {
                    const n = Number(sec);
                    if (!Number.isFinite(n) || n <= 0) return "";
                    if (n % 86400 === 0) return `${n / 86400}d`;
                    if (n % 3600 === 0) return `${n / 3600}h`;
                    if (n % 60 === 0) {
                        const h = Math.floor(n / 3600);
                        const m = Math.floor((n % 3600) / 60);
                        return h ? `${h}h${m}m` : `${m}m`;
                    }
                    return `${n}s`;
                };
                for (const act of actions) {
                    const isManual = act.category === "manual" || String(act.action || "").startsWith("manual_");
                    const durLabel = _fmtManualDur(act.duration_seconds);
                    const actionBadge = act.action === "warn"
                        ? '<span class="vcp-badge vcp-badge-info">Strike 1 (warn)</span>'
                        : act.action === "mute_1h"
                        ? '<span class="vcp-badge vcp-badge-warning">Strike 2 (mute 1h)</span>'
                        : act.action === "mute_24h"
                        ? '<span class="vcp-badge vcp-badge-danger">Strike 3 (mute 24h)</span>'
                        : act.action === "manual_mute"
                        ? `<span class="vcp-badge vcp-badge-warning">Manual Mute${durLabel ? ` (${escapeHtml(durLabel)})` : ""}</span>`
                        : act.action === "manual_unmute"
                        ? '<span class="vcp-badge vcp-badge-info">Manual Unmute</span>'
                        : act.action === "manual_ban"
                        ? '<span class="vcp-badge vcp-badge-danger">Manual Ban</span>'
                        : act.action === "manual_unban"
                        ? '<span class="vcp-badge vcp-badge-info">Manual Unban</span>'
                        : `<span class="vcp-badge">${escapeHtml(act.action)}</span>`;
                    const banBadge = (!isManual && act.banned) ? ' <span class="vcp-badge vcp-badge-danger">BANNED</span>' : "";
                    const actorMeta = (isManual && act.admin_id)
                        ? ` <span class="text-muted">Admin ${escapeHtml(String(act.admin_id))}</span>`
                        : "";
                    const timeStr = formatIsoTime(act.created_at);
                    html += `<tr>
                        <td><code>${escapeHtml(String(act.user_id))}</code></td>
                        <td>${escapeHtml(act.category)}</td>
                        <td>${actionBadge}${banBadge}${actorMeta}</td>
                        <td class="text-muted">${escapeHtml(timeStr)}</td>
                    </tr>`;
                }
                html += "</tbody></table></div>";
                recentEl.innerHTML = html;
            }
        }
    } catch (err) {
        console.warn("Could not load group ai console:", err);
    } finally {
        window._groupAiLoading = false;
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
    showStudioConfirm(t("adm_confirm_sheet_save"), async () => {
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
            await loadAdminGroupAiConsole(true);
        } catch (err) {
            showStudioToast((err && err.message) ? err.message : t("err_generic"), "error");
        }
    });
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
            await loadAdminGroupAiConsole(true);
        } catch (err) {
            showStudioToast((err && err.message) ? err.message : t("err_generic"), "error");
        }
    });
}

window.loadAdminGroupAiConsole = loadAdminGroupAiConsole;
window.saveGroupAiKnowledgeSheet = saveGroupAiKnowledgeSheet;
window.resetGroupAiKnowledgeSheet = resetGroupAiKnowledgeSheet;
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
    if (window._lastAdminQueue && typeof renderAdminJobsData === "function") {
        renderAdminJobsData(window._lastAdminQueue);
    } else if (window._lastAdminHistory && typeof renderAdminJobsData === "function") {
        renderAdminJobsData(window._lastAdminHistory);
    }
    const recentUsersBox = document.getElementById("admin-users-recent");
    if (recentUsersBox && Array.isArray(window._lastAdminUsersList)) {
        if (!window._lastAdminUsersList.length) admStateEmpty(recentUsersBox, "admin_empty");
        else recentUsersBox.innerHTML = window._lastAdminUsersList.map(admUserRowHtml).join("");
    }
    if (window._lastAdminUserDetail && typeof renderAdminUserDetail === "function") {
        renderAdminUserDetail(window._lastAdminUserDetail);
    }
    if (Array.isArray(window._lastAdminUserJobs) && typeof renderAdminUserJobsList === "function") {
        renderAdminUserJobsList(window._lastAdminUserJobs);
    }
    mountSettingsAuxUi();
}
window.refreshAdminLanguageSurfaces = refreshAdminLanguageSurfaces;
window.admFmtSecs = admFmtSecs;
window.admFmtCredits = admFmtCredits;
window.admFmtMmk = admFmtMmk;
window.adminErrorText = adminErrorText;
window.loadAdminData = loadAdminData;
window.renderAdminOverview = renderAdminOverview;
window.checkCustomModel = checkCustomModel;
window.saveGeminiModelSettings = saveGeminiModelSettings;
window.updateAdminSettingsSaveBar = updateAdminSettingsSaveBar;
window.renderAdminPricingList = renderAdminPricingList;
window.loadAdminPricing = loadAdminPricing;
window.loadAdminSettings = loadAdminSettings;

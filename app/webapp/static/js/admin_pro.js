/* ═══════════════════════════════════════════════════════════════════════════
   ADMIN PRO POWER-TOOLS MODULE (Lazy-loaded for Admins only)
   Modules:
   1. Dynamic Multi-Bank / Wallet Payment Accounts Manager (+ QR Upload)
   2. Mini App Edit-and-Approve Slips + Rich Slip Metadata
   3. Order History Search, Period Filter, Slip Preview & CSV Export
   4. 30-Day Expiring-Soon Watchlist + 1-Tap Grant Extend / Revoke
   5. Targeted Broadcast (Audience Segments + Live Count + Test DM + History)
   6. Enhanced Users Filters (Banned/BotBlocked/Test/BYOK/Expiring) + Unified Wallet Ledger
   7. Audit Log Filter/Search/Pagination + Disk Health + Ops Queues + Create Plan
   8. Advanced Runtime Settings (Concurrency, Timeout, AI Profile, USD/MMK FX)
   ═══════════════════════════════════════════════════════════════════════════ */
(function initAdminPro() {
    if (window._adminProMounted) return;
    window._adminProMounted = true;

    const isMy = () => (localStorage.getItem("vcp_lang") || "my") !== "en";
    const L = (my, en) => (isMy() ? my : en);
    const esc = (s) => (typeof escapeHtml === "function" ? escapeHtml(String(s ?? "")) : String(s ?? "").replace(/[&<>"']/g, m => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[m])));
    const toast = (msg, kind = "info") => { if (typeof showStudioToast === "function") showStudioToast(msg, kind); };
    const hdrs = () => (typeof getAuthHeaders === "function" ? getAuthHeaders() : {});
    const reqFetch = (url, opts) => (typeof fetchWithTimeout === "function" ? fetchWithTimeout(url, opts) : fetch(url, opts));

    // Inject compact scoped styles once (zero impact on main style.css bundle budget)
    if (!document.getElementById("adm-pro-styles")) {
        const st = document.createElement("style");
        st.id = "adm-pro-styles";
        st.textContent = `
.admp-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.admp-row-nowrap{display:flex;flex-wrap:nowrap;gap:6px;align-items:center;flex-shrink:0}
.admp-between{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.admp-card-item{padding:10px 12px;border-radius:12px;background:rgba(15,23,42,0.04);border:1px solid rgba(15,23,42,0.10);margin-bottom:8px}
.admp-bank-item{border-left:4px solid #0047BA}
.admp-badge{display:inline-flex;align-items:center;gap:4px;font-size:11px;padding:3px 9px;border-radius:999px;background:rgba(15,23,42,0.07);color:inherit;font-weight:600;white-space:nowrap}
.admp-badge-ok{background:rgba(16,185,129,0.15);color:#059669}
.admp-badge-warn{background:rgba(245,158,11,0.18);color:#b45309}
.admp-badge-err{background:rgba(239,68,68,0.16);color:#dc2626}
.admp-preset-chip{font-size:11px;padding:4px 9px;border-radius:999px;border:1px solid rgba(15,23,42,0.16);background:rgba(15,23,42,0.04);color:inherit;cursor:pointer;white-space:nowrap}
.admp-preset-chip:hover{border-color:#d4af37}
.admp-grid-2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.admp-bar-bg{height:8px;width:100%;border-radius:999px;background:rgba(15,23,42,0.11);overflow:hidden;margin-top:6px}
.admp-bar-fill{height:100%;border-radius:999px;background:linear-gradient(90deg,#10b981,#f59e0b)}
.admp-modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,0.68);z-index:9999;display:flex;align-items:center;justify-content:center;padding:16px}
.admp-modal{width:100%;max-width:420px;border-radius:18px;padding:18px;background:var(--surface-elevated,#141824);border:1px solid rgba(255,255,255,0.14);color:var(--text-primary,#fff);max-height:88vh;overflow-y:auto}
html[data-theme="dark"] .admp-card-item{background:rgba(255,255,255,0.04);border-color:rgba(255,255,255,0.09)}
html[data-theme="dark"] .admp-badge{background:rgba(255,255,255,0.09)}
html[data-theme="dark"] .admp-badge-ok{color:#34d399}
html[data-theme="dark"] .admp-badge-warn{color:#fbbf24}
html[data-theme="dark"] .admp-badge-err{color:#f87171}
html[data-theme="dark"] .admp-preset-chip{background:rgba(255,255,255,0.06);border-color:rgba(255,255,255,0.16)}
html[data-theme="dark"] .admp-bar-bg{background:rgba(255,255,255,0.12)}
@media(max-width:480px){.admp-grid-2{grid-template-columns:1fr}}
`;
        document.head.appendChild(st);
    }

    // State
    const P = {
        accounts: [],
        presets: [],
        editingAccId: "",
        orderStatus: "",
        orderSearch: "",
        orderDays: 0,
        expDays: 7,
        broadcastAudience: "all",
        userFilter: "",
        userSort: "recent",
        auditPrefix: "",
        auditQ: "",
        auditOffset: 0,
    };

    // ═══════════════════════════════════════════════════════════════════════
    // MODULE 1 — Multi-Bank & Payment Accounts Manager (+ QR Upload)
    // ═══════════════════════════════════════════════════════════════════════
    function mountBankAccountsCard() {
        const payScreen = document.getElementById("adm-screen-payments");
        if (!payScreen || document.getElementById("admp-banks-card")) return;

        const card = document.createElement("div");
        card.className = "card glass mt-3";
        card.id = "admp-banks-card";
        card.innerHTML = `
            <div class="card-header-row">
                <div class="card-label">🏦 ${esc(L("ငွေလက်ခံ Bank & Wallet အကောင့်များ", "Bank & Wallet Payment Accounts"))}</div>
                <div class="admp-row">
                    <button type="button" class="mini-chip" id="admp-btn-add-bank">➕ ${esc(L("အကောင့်အသစ်ထည့်", "Add Account"))}</button>
                    <button type="button" class="mini-chip" id="admp-btn-refresh-banks">🔄</button>
                </div>
            </div>
            <p class="text-muted text-xs mt-1">${esc(L("KBZPay, WavePay အပြင် AYA, CB, Yoma, KBZ Bank, USDT စသည့် အကောင့်များ အကန့်အသတ်မရှိ ထည့်သွင်း/ပိတ်ဖွင့်/QR တင်နိုင်ပြီး Mini App & Bot နှစ်ခုလုံးတွင် ချက်ချင်းပြသပေးပါသည်။", "Add, toggle, or upload QR for unlimited Bank/Wallet accounts. Syncs instantly to both Mini App VIP Drawer and Telegram Bot invoices."))}</p>
            <div id="admp-bank-form-wrap" class="hidden admp-card-item mt-2">
                <div class="admp-between mb-2">
                    <strong class="text-xs" id="admp-bank-form-title">➕ ${esc(L("Bank / Wallet အကောင့်အသစ်", "New Payment Account"))}</strong>
                    <button type="button" class="mini-chip" id="admp-bank-form-close">✕</button>
                </div>
                <div class="admp-row mb-2" id="admp-bank-presets"></div>
                <div class="admp-grid-2">
                    <div><label class="input-label text-xs">${esc(L("ဘဏ် / Wallet အမည် *", "Provider / Bank *"))}</label><input type="text" id="admp-b-provider" class="studio-input text-xs" placeholder="AYA Bank / CB Pay / KBZ Bank"></div>
                    <div><label class="input-label text-xs">${esc(L("အမျိုးအစား", "Type"))}</label><select id="admp-b-type" class="studio-input text-xs"><option value="bank">🏦 Bank Account</option><option value="mobile_wallet">💳 Mobile Wallet</option><option value="crypto">🪙 Crypto / USDT</option></select></div>
                </div>
                <div class="admp-grid-2 mt-2">
                    <div><label class="input-label text-xs">${esc(L("အကောင့် / ဖုန်းနံပါတ် *", "Account / Phone No *"))}</label><input type="text" id="admp-b-number" class="studio-input text-xs" placeholder="09xxx / 0012-3456-..."></div>
                    <div><label class="input-label text-xs">${esc(L("အကောင့်ပိုင်ရှင်အမည်", "Account Holder Name"))}</label><input type="text" id="admp-b-name" class="studio-input text-xs" placeholder="U / Daw ..."></div>
                </div>
                <div class="admp-grid-2 mt-2">
                    <div><label class="input-label text-xs">${esc(L("ဘဏ်ခွဲ (Optional)", "Branch (Optional)"))}</label><input type="text" id="admp-b-branch" class="studio-input text-xs" placeholder="Yangon Main / Special"></div>
                    <div><label class="input-label text-xs">${esc(L("အရောင် & စီစဉ်မှု", "Color & Order"))}</label><div class="admp-row"><input type="color" id="admp-b-color" value="#1D4ED8" style="width:40px;height:34px;border:none;background:none;cursor:pointer"><input type="number" id="admp-b-order" class="studio-input text-xs" style="width:74px" value="30" min="1" max="999"></div></div>
                </div>
                <div class="mt-2">
                    <label class="input-label text-xs">${esc(L("မှတ်ချက် (Optional)", "Payment Note (Optional)"))}</label>
                    <input type="text" id="admp-b-note" class="studio-input text-xs" placeholder="${esc(L("ဥပမာ- ငွေလွှဲမှတ်ချက်တွင် Telegram ID ထည့်ပါ", "e.g. Put Telegram ID in remark"))}">
                </div>
                <div class="admp-between mt-2">
                    <label class="text-xs" style="display:flex;align-items:center;gap:6px"><input type="checkbox" id="admp-b-active" checked> ${esc(L("အသုံးပြုသူများမြင်အောင် ဖွင့်ထားမည် (Active)", "Active (Visible to Users)"))}</label>
                    <button type="button" class="btn mini-btn gold-btn" id="admp-b-save">💾 ${esc(L("သိမ်းမည်", "Save"))}</button>
                </div>
            </div>
            <div id="admp-banks-list" class="mt-2"><p class="text-muted text-xs">${esc(L("ရယူနေသည်…", "Loading…"))}</p></div>
        `;
        payScreen.appendChild(card);

        document.getElementById("admp-btn-add-bank").onclick = () => openBankForm(null);
        document.getElementById("admp-btn-refresh-banks").onclick = () => loadBankAccounts();
        document.getElementById("admp-bank-form-close").onclick = () => document.getElementById("admp-bank-form-wrap").classList.add("hidden");
        document.getElementById("admp-b-save").onclick = () => saveBankAccountForm();

        // Also add quick jump button inside Settings -> Payment & Maintenance
        const payAcc = document.querySelector("#adm-acc-pay-maint .adm-acc-body");
        if (payAcc && !document.getElementById("admp-settings-banks-jump")) {
            const jump = document.createElement("div");
            jump.id = "admp-settings-banks-jump";
            jump.className = "mt-2 admp-card-item admp-between";
            jump.innerHTML = `<span class="text-xs">🏦 ${esc(L("ထပ်တိုး Bank Account များ (AYA / CB / KBZ Bank / Yoma / QR)", "Additional Bank Accounts & QR Manager"))}</span><button type="button" class="mini-chip">🏦 ${esc(L("စီမံရန် ဖွင့်မည်", "Manage Banks"))}</button>`;
            jump.querySelector("button").onclick = () => {
                if (typeof showAdminSection === "function") showAdminSection("payments");
                setTimeout(() => document.getElementById("admp-banks-card")?.scrollIntoView({ behavior: "smooth" }), 120);
            };
            payAcc.appendChild(jump);
        }
    }

    function openBankForm(acc) {
        P.editingAccId = acc ? acc.id : "";
        const wrap = document.getElementById("admp-bank-form-wrap");
        if (!wrap) return;
        wrap.classList.remove("hidden");
        document.getElementById("admp-bank-form-title").textContent = acc
            ? `✏️ ${L("အကောင့် ပြင်ဆင်ရန်", "Edit Account")}: ${acc.provider}`
            : `➕ ${L("Bank / Wallet အကောင့်အသစ်", "New Payment Account")}`;
        document.getElementById("admp-b-provider").value = acc ? acc.provider : "";
        document.getElementById("admp-b-type").value = acc ? (acc.account_type || "bank") : "bank";
        document.getElementById("admp-b-number").value = acc ? acc.number : "";
        document.getElementById("admp-b-name").value = acc ? (acc.name || "") : "";
        document.getElementById("admp-b-branch").value = acc ? (acc.bank_branch || "") : "";
        document.getElementById("admp-b-color").value = (acc && /^#[0-9a-fA-F]{6}$/.test(acc.color || "")) ? acc.color : "#1D4ED8";
        document.getElementById("admp-b-order").value = acc ? (acc.sort_order || 30) : ((P.accounts.length + 1) * 10);
        document.getElementById("admp-b-note").value = acc ? (acc.note || "") : "";
        document.getElementById("admp-b-active").checked = acc ? !!acc.is_active : true;
        renderBankPresets();
    }

    function renderBankPresets() {
        const box = document.getElementById("admp-bank-presets");
        if (!box) return;
        const list = P.presets.length ? P.presets : [
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
        box.innerHTML = list.map((p, i) => `<button type="button" class="admp-preset-chip" data-idx="${i}" style="border-left:3px solid ${esc(p.color)}">${esc(p.provider)}</button>`).join("");
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

    async function loadBankAccounts() {
        const box = document.getElementById("admp-banks-list");
        if (!box) return;
        try {
            const res = await reqFetch("/api/admin/payment-accounts", { headers: hdrs() });
            if (!res.ok) throw new Error("HTTP " + res.status);
            const data = await res.json();
            P.accounts = data.accounts || [];
            P.presets = data.presets || [];
            if (!P.accounts.length) {
                box.innerHTML = `<p class="text-muted text-xs">${esc(L("အကောင့် မရှိသေးပါ", "No accounts configured"))}</p>`;
                return;
            }
            box.innerHTML = P.accounts.map(acc => {
                const col = /^#[0-9a-fA-F]{3,6}$/.test(acc.color || "") ? acc.color : "#0047BA";
                const ic = acc.account_type === "bank" ? "🏦" : (acc.account_type === "crypto" ? "🪙" : "💳");
                const stBadge = acc.is_active
                    ? `<span class="admp-badge admp-badge-ok">● Active</span>`
                    : `<span class="admp-badge admp-badge-err">○ Off</span>`;
                const qrHtml = acc.qr_url
                    ? `<button type="button" class="mini-chip" data-act="qr-view" data-url="${esc(acc.qr_url)}">📷 QR</button>`
                    : "";
                return `<div class="admp-card-item admp-bank-item" style="border-left-color:${col}">
                    <div class="admp-between">
                        <div><strong>${ic} ${esc(acc.provider)}</strong> ${stBadge}</div>
                        <div class="admp-row">
                            ${qrHtml}
                            <button type="button" class="mini-chip" data-act="qr-up" data-id="${esc(acc.id)}">⬆️ QR</button>
                            <button type="button" class="mini-chip" data-act="toggle" data-id="${esc(acc.id)}">${acc.is_active ? "⏸" : "▶"}</button>
                            <button type="button" class="mini-chip" data-act="edit" data-id="${esc(acc.id)}">✏️</button>
                            <button type="button" class="mini-chip" data-act="del" data-id="${esc(acc.id)}">🗑️</button>
                        </div>
                    </div>
                    <div class="text-xs mt-1"><b>${esc(acc.number)}</b>${acc.name ? ` · ${esc(acc.name)}` : ""}${acc.bank_branch ? ` · ${esc(acc.bank_branch)}` : ""}</div>
                    ${acc.note ? `<div class="text-xs text-muted mt-1">💬 ${esc(acc.note)}</div>` : ""}
                </div>`;
            }).join("");

            box.querySelectorAll("button[data-act]").forEach(btn => {
                const id = btn.dataset.id;
                const act = btn.dataset.act;
                btn.onclick = async () => {
                    const acc = P.accounts.find(a => a.id === id);
                    if (act === "qr-view") {
                        if (typeof openSlipModal === "function") openSlipModal(btn.dataset.url);
                        else window.open(btn.dataset.url, "_blank");
                    } else if (act === "edit" && acc) {
                        openBankForm(acc);
                    } else if (act === "toggle" && acc) {
                        await toggleBankAccount(acc);
                    } else if (act === "del" && acc) {
                        await deleteBankAccount(acc);
                    } else if (act === "qr-up" && acc) {
                        triggerQrUpload(acc.id);
                    }
                };
            });
        } catch (err) {
            box.innerHTML = `<p class="text-xs text-danger">Failed to load accounts: ${esc(err.message)}</p>`;
        }
    }

    async function saveBankAccountForm() {
        const provider = (document.getElementById("admp-b-provider")?.value || "").trim();
        const number = (document.getElementById("admp-b-number")?.value || "").trim();
        if (!provider || !number) {
            toast(L("ဘဏ်အမည်နှင့် အကောင့်နံပါတ် ထည့်ပေးပါ", "Provider and account number are required"), "warning");
            return;
        }
        const payload = {
            account: {
                id: P.editingAccId || undefined,
                provider,
                account_type: document.getElementById("admp-b-type")?.value || "bank",
                number,
                name: (document.getElementById("admp-b-name")?.value || "").trim(),
                bank_branch: (document.getElementById("admp-b-branch")?.value || "").trim(),
                note: (document.getElementById("admp-b-note")?.value || "").trim(),
                color: document.getElementById("admp-b-color")?.value || "#1D4ED8",
                sort_order: Number(document.getElementById("admp-b-order")?.value || 30),
                is_active: !!document.getElementById("admp-b-active")?.checked,
            }
        };
        const res = await reqFetch("/api/admin/payment-accounts", {
            method: "POST",
            headers: { ...hdrs(), "Content-Type": "application/json" },
            body: JSON.stringify(payload),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { toast(data.detail || "Save failed", "error"); return; }
        toast(data.message || L("သိမ်းပြီးပါပြီ", "Saved"), "success");
        document.getElementById("admp-bank-form-wrap")?.classList.add("hidden");
        await loadBankAccounts();
        if (typeof refreshPlanPrices === "function") refreshPlanPrices();
    }

    async function toggleBankAccount(acc) {
        const res = await reqFetch("/api/admin/payment-accounts", {
            method: "POST",
            headers: { ...hdrs(), "Content-Type": "application/json" },
            body: JSON.stringify({ account: { ...acc, is_active: !acc.is_active } }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { toast(data.detail || "Failed", "error"); return; }
        toast(acc.is_active ? L("ပိတ်ထားလိုက်ပါပြီ", "Account deactivated") : L("ပြန်ဖွင့်ပြီးပါပြီ", "Account activated"), "success");
        await loadBankAccounts();
        if (typeof refreshPlanPrices === "function") refreshPlanPrices();
    }

    async function deleteBankAccount(acc) {
        const res = await reqFetch(`/api/admin/payment-accounts/${encodeURIComponent(acc.id)}`, {
            method: "DELETE",
            headers: hdrs(),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) { toast(data.detail || "Cannot delete", "error"); return; }
        toast(data.message || L("ဖျက်ပြီးပါပြီ", "Deleted"), "success");
        await loadBankAccounts();
        if (typeof refreshPlanPrices === "function") refreshPlanPrices();
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
            const res = await reqFetch(`/api/admin/payment-accounts/${encodeURIComponent(accId)}/qr`, {
                method: "POST",
                headers: hdrs(),
                body: fd,
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) { toast(data.detail || "QR upload failed", "error"); return; }
            toast(L("QR ပုံ တင်ပြီးပါပြီ", "QR image uploaded"), "success");
            await loadBankAccounts();
        };
        inp.click();
    }

    // ═══════════════════════════════════════════════════════════════════════
    // MODULE 2 — Edit-and-Approve Slips + Rich Slip Metadata
    // ═══════════════════════════════════════════════════════════════════════
    async function renderEnhancedSlips() {
        const listEl = document.getElementById("admin-slips-list");
        if (!listEl) return;
        try {
            const res = await reqFetch("/api/admin/pending-slips", { headers: hdrs() });
            if (!res.ok) return;
            const slips = await res.json();
            if (!Array.isArray(slips) || !slips.length) return;
            listEl.innerHTML = slips.map(s => {
                const refLine = s.payment_reference ? `<div class="text-xs" style="color:#38bdf8">🧾 Ref: <code>${esc(s.payment_reference)}</code></div>` : "";
                const lockLine = s.review_claimed_by ? `<div class="text-xs" style="color:#f59e0b">🔒 Claimed by Admin ${esc(s.review_claimed_by)}</div>` : "";
                const alloc = [s.minutes > 0 ? `${s.minutes}m Voice` : "", s.recap_credits > 0 ? `${s.recap_credits} Recap` : "", `${s.validity_days || 30}d`].filter(Boolean).join(" · ");
                const imgHtml = s.slip_url ? `<img src="${esc(s.slip_url)}" alt="slip" style="width:64px;height:64px;object-fit:cover;border-radius:8px;cursor:pointer;flex-shrink:0" onclick="typeof openSlipModal==='function'&&openSlipModal('${esc(s.slip_url)}')">` : "";
                return `<div class="admp-card-item">
                    <div class="admp-row" style="align-items:flex-start;flex-wrap:nowrap">
                        ${imgHtml}
                        <div style="flex:1;min-width:0">
                            <div class="admp-between">
                                <strong class="text-xs">${esc(s.package_name || s.package_code)} — ${Number(s.amount_mmk || 0).toLocaleString()} MMK</strong>
                                <span class="text-xs text-muted">${esc(s.created_at)}</span>
                            </div>
                            <div class="text-xs mt-1"><a href="javascript:void(0)" onclick="openAdminUserDetail(${Number(s.user_id)})" style="color:#d4af37;font-weight:600">${esc(s.user_name)} ${s.username ? `(@${esc(s.username)})` : ""}</a> · ID ${esc(s.user_id)}</div>
                            <div class="text-xs text-muted">${esc(alloc)} · <code>${esc(s.id)}</code></div>
                            ${refLine}${lockLine}
                        </div>
                    </div>
                    <div class="admp-row mt-2">
                        <button type="button" class="btn mini-btn gold-btn" onclick="approveSlip('${esc(s.id)}')">✅ ${esc(L("အတည်ပြု", "Approve"))}</button>
                        <button type="button" class="mini-chip" data-edit-slip="${esc(s.id)}">✏️ ${esc(L("ပြင်ပြီး အတည်ပြု", "Edit & Approve"))}</button>
                        <button type="button" class="mini-chip" style="color:#ef4444" onclick="rejectSlip('${esc(s.id)}')">❌ ${esc(L("ငြင်းပယ်", "Reject"))}</button>
                    </div>
                </div>`;
            }).join("");
            listEl.querySelectorAll("button[data-edit-slip]").forEach(btn => {
                btn.onclick = () => {
                    const slip = slips.find(x => x.id === btn.dataset.editSlip);
                    if (slip) openEditApproveModal(slip);
                };
            });
        } catch (e) {}
    }

    function openEditApproveModal(slip) {
        const old = document.getElementById("admp-edit-slip-modal");
        if (old) old.remove();
        const bd = document.createElement("div");
        bd.id = "admp-edit-slip-modal";
        bd.className = "admp-modal-backdrop";
        bd.innerHTML = `<div class="admp-modal">
            <div class="admp-between mb-2"><strong>✏️ ${esc(L("ပမာဏ ပြင်ဆင်ပြီး အတည်ပြုရန်", "Edit & Approve Slip"))}</strong><button type="button" class="mini-chip" id="admp-es-close">✕</button></div>
            <p class="text-xs text-muted">${esc(slip.user_name)} · ${esc(slip.package_name || slip.package_code)} (${Number(slip.amount_mmk || 0).toLocaleString()} MMK)</p>
            <div class="admp-grid-2 mt-2">
                <div><label class="input-label text-xs">🎙️ Voice Minutes</label><input type="number" id="admp-es-mins" class="studio-input text-xs" min="0" max="100000" value="${Number(slip.minutes || 0)}"></div>
                <div><label class="input-label text-xs">🎬 Recap Credits</label><input type="number" id="admp-es-rc" class="studio-input text-xs" min="0" max="1000" value="${Number(slip.recap_credits || 0)}"></div>
            </div>
            <div class="mt-2">
                <label class="input-label text-xs">⏳ ${esc(L("သက်တမ်း (ရက်) — 0 = သက်တမ်းမကုန်", "Validity Days (0 = No Expiry)"))}</label>
                <input type="number" id="admp-es-days" class="studio-input text-xs" min="0" max="3650" value="${Number(slip.validity_days ?? 30)}">
            </div>
            <div class="admp-between mt-3">
                <button type="button" class="mini-chip" id="admp-es-cancel">${esc(L("ပယ်ဖျက်", "Cancel"))}</button>
                <button type="button" class="btn mini-btn gold-btn" id="admp-es-submit">✅ ${esc(L("ပြင်ပြီး အတည်ပြုမည်", "Confirm Edit & Approve"))}</button>
            </div>
        </div>`;
        document.body.appendChild(bd);
        const close = () => bd.remove();
        document.getElementById("admp-es-close").onclick = close;
        document.getElementById("admp-es-cancel").onclick = close;
        document.getElementById("admp-es-submit").onclick = async () => {
            const body = {
                request_id: slip.id,
                corrected_minutes: Number(document.getElementById("admp-es-mins").value || 0),
                corrected_recap_credits: Number(document.getElementById("admp-es-rc").value || 0),
                validity_days: Number(document.getElementById("admp-es-days").value ?? 30),
            };
            const res = await reqFetch("/api/admin/edit-approve-slip", {
                method: "POST",
                headers: { ...hdrs(), "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) { toast(data.detail || "Edit-Approve failed", "error"); return; }
            toast(data.message || L("အတည်ပြုပြီးပါပြီ", "Approved"), "success");
            close();
            if (typeof loadAdminData === "function") loadAdminData();
            renderEnhancedSlips();
            loadEnhancedOrders();
        };
    }

    // ═══════════════════════════════════════════════════════════════════════
    // MODULE 3 — Order History Search, Period, Slip Preview & CSV Export
    // ═══════════════════════════════════════════════════════════════════════
    function mountOrdersToolbar() {
        const listEl = document.getElementById("adm-orders-list");
        if (!listEl || document.getElementById("admp-orders-bar")) return;
        const bar = document.createElement("div");
        bar.id = "admp-orders-bar";
        bar.className = "admp-row mt-2";
        bar.innerHTML = `
            <input type="text" id="admp-ord-q" class="studio-input text-xs" style="flex:1 1 160px;min-width:130px" placeholder="${esc(L("ID / @user / Ref ရှာရန်…", "Search ID / @user / Ref…"))}">
            <select id="admp-ord-days" class="studio-input text-xs" style="width:94px">
                <option value="0">${esc(L("အားလုံး", "All"))}</option>
                <option value="7">7D</option>
                <option value="30">30D</option>
                <option value="90">90D</option>
            </select>
            <button type="button" class="mini-chip" id="admp-ord-go">🔎</button>
            <button type="button" class="mini-chip" id="admp-ord-csv">⬇️ CSV</button>
        `;
        listEl.parentElement.insertBefore(bar, listEl);

        document.getElementById("admp-ord-go").onclick = () => {
            P.orderSearch = (document.getElementById("admp-ord-q")?.value || "").trim();
            P.orderDays = Number(document.getElementById("admp-ord-days")?.value || 0);
            loadEnhancedOrders(P.orderStatus);
        };
        document.getElementById("admp-ord-days").onchange = () => document.getElementById("admp-ord-go").click();
        document.getElementById("admp-ord-q").onkeydown = (e) => { if (e.key === "Enter") document.getElementById("admp-ord-go").click(); };
        document.getElementById("admp-ord-csv").onclick = () => exportOrdersCsv();

        // Hook status chips
        document.querySelectorAll("[data-ord-st]").forEach(btn => {
            btn.onclick = () => {
                document.querySelectorAll("[data-ord-st]").forEach(b => b.classList.remove("active"));
                btn.classList.add("active");
                P.orderStatus = btn.dataset.ordSt || "";
                loadEnhancedOrders(P.orderStatus);
            };
        });
    }

    async function loadEnhancedOrders(statusFilter = P.orderStatus) {
        P.orderStatus = statusFilter || "";
        const listEl = document.getElementById("adm-orders-list");
        if (!listEl) return;
        const qs = new URLSearchParams({ limit: "100" });
        if (P.orderStatus) qs.set("status", P.orderStatus);
        if (P.orderSearch) qs.set("search", P.orderSearch);
        if (P.orderDays > 0) qs.set("days", String(P.orderDays));
        try {
            const res = await reqFetch(`/api/admin/orders?${qs.toString()}`, { headers: hdrs() });
            if (!res.ok) throw new Error("HTTP " + res.status);
            const data = await res.json();
            const rows = data.orders || [];
            if (!rows.length) {
                listEl.innerHTML = `<p class="text-muted text-xs">${esc(L("မှတ်တမ်း မရှိပါ", "No orders match filter"))}</p>`;
                return;
            }
            listEl.innerHTML = rows.map(o => {
                const bCls = o.status === "APPROVED" ? "admp-badge-ok" : (o.status === "REJECTED" ? "admp-badge-err" : "admp-badge-warn");
                const slipBtn = o.slip_url ? `<button type="button" class="mini-chip" onclick="typeof openSlipModal==='function'&&openSlipModal('${esc(o.slip_url)}')">🖼️ Slip</button>` : "";
                const refTxt = o.payment_reference ? ` · Ref: <code>${esc(o.payment_reference)}</code>` : "";
                const rejTxt = o.rejection_reason ? `<div class="text-xs text-danger">❌ ${esc(o.rejection_reason)}</div>` : "";
                const dt = (o.created_at || "").replace("T", " ").slice(0, 16);
                return `<div class="admp-card-item">
                    <div class="admp-between">
                        <div><strong class="text-xs">${esc(o.package_name || o.package_code)}</strong> · <b>${Number(o.amount_mmk || 0).toLocaleString()} MMK</b></div>
                        <span class="admp-badge ${bCls}">${esc(o.status)}</span>
                    </div>
                    <div class="admp-between text-xs text-muted mt-1">
                        <span><a href="javascript:void(0)" onclick="openAdminUserDetail(${Number(o.user_id)})" style="color:#d4af37">${esc(o.user_name)}</a> (${esc(o.user_id)}) · ${o.minutes}m / ${o.recap_credits}c / ${o.validity_days || 30}d${refTxt}</span>
                        <div class="admp-row">${slipBtn}<span>${esc(dt)}</span></div>
                    </div>
                    ${rejTxt}
                </div>`;
            }).join("");
        } catch (err) {
            listEl.innerHTML = `<p class="text-xs text-danger">${esc(err.message)}</p>`;
        }
    }
    window.loadAdminOrders = loadEnhancedOrders;

    async function exportOrdersCsv() {
        const qs = new URLSearchParams();
        if (P.orderStatus) qs.set("status", P.orderStatus);
        if (P.orderSearch) qs.set("search", P.orderSearch);
        qs.set("days", String(P.orderDays || 30));
        try {
            const res = await reqFetch(`/api/admin/orders/export?${qs.toString()}`, { headers: hdrs() });
            if (!res.ok) throw new Error("Export HTTP " + res.status);
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `vcp_orders_${new Date().toISOString().slice(0, 10)}.csv`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(url), 1500);
            toast(L("Orders CSV Export အောင်မြင်ပါပြီ", "Orders CSV exported"), "success");
        } catch (err) {
            toast(err.message, "error");
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // MODULE 4 — Global 30-Day Expiring-Soon Watchlist & Grant Extend/Revoke
    // ═══════════════════════════════════════════════════════════════════════
    function mountExpiringWatchlistCard() {
        const payScreen = document.getElementById("adm-screen-payments");
        if (!payScreen || document.getElementById("admp-expiring-card")) return;
        const card = document.createElement("div");
        card.id = "admp-expiring-card";
        card.className = "card glass mt-3";
        card.innerHTML = `
            <div class="card-header-row">
                <div class="card-label">⏳ ${esc(L("သက်တမ်းကုန်ခါနီး Package စာရင်း (Expiring Watchlist)", "Expiring Grants Watchlist"))}</div>
                <div class="admp-row">
                    <select id="admp-exp-days" class="studio-input text-xs" style="width:82px">
                        <option value="3">≤ 3d</option>
                        <option value="7" selected>≤ 7d</option>
                        <option value="14">≤ 14d</option>
                        <option value="30">≤ 30d</option>
                    </select>
                    <button type="button" class="mini-chip" id="admp-exp-refresh">🔄</button>
                </div>
            </div>
            <div id="admp-exp-list" class="mt-2"><p class="text-muted text-xs">${esc(L("ရယူနေသည်…", "Loading…"))}</p></div>
        `;
        payScreen.appendChild(card);
        document.getElementById("admp-exp-days").onchange = (e) => { P.expDays = Number(e.target.value || 7); loadExpiringWatchlist(); };
        document.getElementById("admp-exp-refresh").onclick = () => loadExpiringWatchlist();
    }

    async function loadExpiringWatchlist() {
        const box = document.getElementById("admp-exp-list");
        if (!box) return;
        try {
            const res = await reqFetch(`/api/admin/expiring-grants?days=${P.expDays}`, { headers: hdrs() });
            if (!res.ok) throw new Error("HTTP " + res.status);
            const data = await res.json();
            const rows = data.grants || [];
            if (!rows.length) {
                box.innerHTML = `<p class="text-muted text-xs">✅ ${esc(L(`${P.expDays} ရက်အတွင်း သက်တမ်းကုန်မည့် Package မရှိပါ`, `No paid lots expiring within ${P.expDays} days`))}</p>`;
                return;
            }
            box.innerHTML = rows.map(g => {
                const parts = [];
                if (Number(g.remaining_recap_credits || 0) > 0) parts.push(`${g.remaining_recap_credits}c`);
                if (Number(g.remaining_voice_minutes || 0) > 0) parts.push(`${g.remaining_voice_minutes}m`);
                const remTxt = parts.join(" + ") || "0";
                const warnCls = g.days_left <= 1 ? "admp-badge-err" : "admp-badge-warn";
                return `<div class="admp-card-item">
                    <div class="admp-between">
                        <div><a href="javascript:void(0)" onclick="openAdminUserDetail(${Number(g.user_id)})" style="color:#d4af37;font-weight:600" class="text-xs">${esc(g.user_name)} ${g.username ? `(@${esc(g.username)})` : ""}</a> · <span class="text-xs">${esc(remTxt)}</span></div>
                        <span class="admp-badge ${warnCls}">⏳ ${esc(g.days_left)}d</span>
                    </div>
                    <div class="admp-between text-xs text-muted mt-1">
                        <span>${esc(g.package_name || g.package_code || g.source_type)} · exp ${(g.expires_at || "").slice(0, 10)}</span>
                        <div class="admp-row">
                            <button type="button" class="mini-chip" data-g-ext="${esc(g.grant_id)}" data-days="7">+7d</button>
                            <button type="button" class="mini-chip" data-g-ext="${esc(g.grant_id)}" data-days="30">+30d</button>
                            <button type="button" class="mini-chip" style="color:#ef4444" data-g-rev="${esc(g.grant_id)}">🚫</button>
                        </div>
                    </div>
                </div>`;
            }).join("");
            bindGrantActionButtons(box, () => loadExpiringWatchlist());
        } catch (err) {
            box.innerHTML = `<p class="text-xs text-danger">${esc(err.message)}</p>`;
        }
    }

    function bindGrantActionButtons(root, onDone) {
        root.querySelectorAll("button[data-g-ext]").forEach(btn => {
            btn.onclick = async () => {
                const gid = btn.dataset.gExt;
                const days = Number(btn.dataset.days || 7);
                const res = await reqFetch(`/api/admin/grants/${encodeURIComponent(gid)}/extend`, {
                    method: "POST",
                    headers: { ...hdrs(), "Content-Type": "application/json" },
                    body: JSON.stringify({ add_days: days, reason: `Admin +${days}d extend`, notify_user: true }),
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) { toast(data.detail || "Extend failed", "error"); return; }
                toast(data.message || `+${days}d extended`, "success");
                if (onDone) onDone();
            };
        });
        root.querySelectorAll("button[data-g-rev]").forEach(btn => {
            btn.onclick = async () => {
                const gid = btn.dataset.gRev;
                const res = await reqFetch(`/api/admin/grants/${encodeURIComponent(gid)}/revoke`, {
                    method: "POST",
                    headers: { ...hdrs(), "Content-Type": "application/json" },
                    body: JSON.stringify({ reason: "Admin revoked grant from console" }),
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) { toast(data.detail || "Revoke failed", "error"); return; }
                toast(data.message || "Revoked", "success");
                if (onDone) onDone();
            };
        });
    }

    // ═══════════════════════════════════════════════════════════════════════
    // MODULE 5 — Targeted Broadcast + Live Count + Test DM + Campaign History
    // ═══════════════════════════════════════════════════════════════════════
    function mountBroadcastUpgrades() {
        const scr = document.getElementById("adm-screen-broadcast");
        if (!scr || document.getElementById("admp-bc-aud")) return;
        const textArea = document.getElementById("adm-broadcast-text");
        if (textArea) {
            const audWrap = document.createElement("div");
            audWrap.id = "admp-bc-aud";
            audWrap.className = "mt-2";
            const segs = [
                ["all", L("အားလုံး (All Active)", "All Active")],
                ["vip", "👑 VIP"],
                ["free", "🆓 Free"],
                ["expiring_7d", L("⏳ ၇ ရက်အတွင်းကုန်မည့်သူ", "⏳ Expiring ≤7d")],
                ["active_7d", L("🔥 7D Active", "🔥 Active 7d")],
                ["active_30d", L("📅 30D Active", "📅 Active 30d")],
            ];
            audWrap.innerHTML = `
                <div class="admp-between mb-1">
                    <span class="text-xs text-muted">${esc(L("ပို့မည့်အုပ်စု ရွေးရန် (Audience)", "Target Audience"))}</span>
                    <span class="admp-badge admp-badge-ok" id="admp-bc-count">…</span>
                </div>
                <div class="admp-row">${segs.map(([k, lbl]) => `<button type="button" class="mini-chip ${k === "all" ? "active" : ""}" data-bc-aud="${k}">${esc(lbl)}</button>`).join("")}</div>
            `;
            textArea.parentElement.insertBefore(audWrap, textArea);
            audWrap.querySelectorAll("[data-bc-aud]").forEach(btn => {
                btn.onclick = () => {
                    audWrap.querySelectorAll("[data-bc-aud]").forEach(b => b.classList.remove("active"));
                    btn.classList.add("active");
                    P.broadcastAudience = btn.dataset.bcAud || "all";
                    loadBroadcastPreview();
                };
            });
        }
        const sendBtn = document.getElementById("adm-broadcast-btn");
        if (sendBtn && !document.getElementById("admp-bc-test-btn")) {
            const testBtn = document.createElement("button");
            testBtn.type = "button";
            testBtn.id = "admp-bc-test-btn";
            testBtn.className = "btn secondary-btn mt-2";
            testBtn.textContent = `🧪 ${L("မိမိ DM သို့ Test အရင်ပို့ကြည့်မည်", "Test Send to My DM First")}`;
            testBtn.onclick = () => sendTestBroadcast();
            sendBtn.parentElement.insertBefore(testBtn, sendBtn);
        }
        const histCard = document.createElement("div");
        histCard.className = "card glass mt-3";
        histCard.id = "admp-bc-history-card";
        histCard.innerHTML = `
            <div class="card-header-row">
                <div class="card-label">📜 ${esc(L("Broadcast ပို့ထားပြီးသော မှတ်တမ်း", "Past Broadcast Campaigns"))}</div>
                <button type="button" class="mini-chip" id="admp-bc-hist-ref">🔄</button>
            </div>
            <div id="admp-bc-history" class="mt-2"><p class="text-muted text-xs">${esc(L("ရယူနေသည်…", "Loading…"))}</p></div>
        `;
        scr.appendChild(histCard);
        document.getElementById("admp-bc-hist-ref").onclick = () => loadBroadcastHistory();

        // Override adminSendBroadcast to pass selected audience & refresh history
        const origBroadcast = window.adminSendBroadcast;
        window.adminSendBroadcast = async function () {
            if (P.broadcastAudience === "all" && typeof origBroadcast === "function") {
                await origBroadcast();
                setTimeout(loadBroadcastHistory, 500);
                return;
            }
            const msg = (document.getElementById("adm-broadcast-text")?.value || "").trim();
            if (msg.length < 4) { toast(L("စာသား အနည်းဆုံး ၄ လုံး ရိုက်ပါ", "Minimum 4 characters"), "warning"); return; }
            const res = await reqFetch("/api/admin/broadcast", {
                method: "POST",
                headers: { ...hdrs(), "Content-Type": "application/json" },
                body: JSON.stringify({ message: msg, audience: P.broadcastAudience, test_only: false }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) { toast(data.detail || "Broadcast failed", "error"); return; }
            toast(`✅ Sent: ${data.sent_count || 0} / ${data.total_targeted || 0}`, "success");
            loadBroadcastHistory();
        };
    }

    async function loadBroadcastPreview() {
        const el = document.getElementById("admp-bc-count");
        if (!el) return;
        try {
            const res = await reqFetch(`/api/admin/broadcast/preview?audience=${encodeURIComponent(P.broadcastAudience)}`, { headers: hdrs() });
            if (!res.ok) return;
            const d = await res.json();
            el.textContent = `👥 ${d.deliverable} ${L("ဦး ပို့မည်", "recipients")}`;
        } catch (e) {}
    }

    async function sendTestBroadcast() {
        const msg = (document.getElementById("adm-broadcast-text")?.value || "").trim();
        if (msg.length < 4) { toast(L("စာသား အနည်းဆုံး ၄ လုံး ရိုက်ပါ", "Minimum 4 characters"), "warning"); return; }
        const res = await reqFetch("/api/admin/broadcast", {
            method: "POST",
            headers: { ...hdrs(), "Content-Type": "application/json" },
            body: JSON.stringify({ message: msg, audience: P.broadcastAudience, test_only: true }),
        });
        const d = await res.json().catch(() => ({}));
        if (!res.ok) { toast(d.detail || "Test DM failed", "error"); return; }
        toast(d.message || L("Test DM ပို့ပြီးပါပြီ", "Test DM sent"), "success");
    }

    async function loadBroadcastHistory() {
        const box = document.getElementById("admp-bc-history");
        if (!box) return;
        try {
            const res = await reqFetch("/api/admin/broadcast/history?limit=15", { headers: hdrs() });
            if (!res.ok) return;
            const data = await res.json();
            const list = data.campaigns || [];
            if (!list.length) {
                box.innerHTML = `<p class="text-muted text-xs">${esc(L("Broadcast မှတ်တမ်း မရှိသေးပါ", "No past campaigns yet"))}</p>`;
                return;
            }
            box.innerHTML = list.map(c => `<div class="admp-card-item">
                <div class="admp-between text-xs">
                    <span class="admp-badge admp-badge-ok">✅ ${c.sent_count}/${c.total_recipients}</span>
                    <span class="text-muted">${esc((c.created_at || "").replace("T", " ").slice(0, 16))}</span>
                </div>
                <div class="text-xs mt-1">${esc(c.snippet)}</div>
            </div>`).join("");
        } catch (e) {}
    }

    // ═══════════════════════════════════════════════════════════════════════
    // MODULE 6 — Enhanced Users Filters + User Detail Unified Ledger & Grant Ops
    // ═══════════════════════════════════════════════════════════════════════
    function mountUsersEnhancements() {
        const row = document.getElementById("adm-users-filter-chips");
        if (row && !document.getElementById("admp-users-extra-row")) {
            const ex = document.createElement("div");
            ex.id = "admp-users-extra-row";
            ex.className = "admp-row mt-2";
            const flts = [
                ["", L("အားလုံး", "All Flags")],
                ["EXPIRING_7D", "⏳ ≤7d"],
                ["BANNED", "🚫 Banned"],
                ["BOT_BLOCKED", "🔕 Bot Blocked"],
                ["BYOK", "🔑 BYOK"],
                ["TEST", "🧪 Test"],
            ];
            ex.innerHTML = flts.map(([k, lbl]) => `<button type="button" class="mini-chip ${k === "" ? "active" : ""}" data-u-flt="${k}">${esc(lbl)}</button>`).join("")
                + `<select id="admp-u-sort" class="studio-input text-xs" style="min-width:134px;width:auto;margin-left:auto"><option value="recent">${esc(L("အသစ်ဆုံး", "Newest"))}</option><option value="balance">${esc(L("Voice အများဆုံး", "Top Voice"))}</option><option value="recap">${esc(L("Recap အများဆုံး", "Top Recap"))}</option></select>`;
            row.parentElement.insertBefore(ex, row.nextSibling);
            ex.querySelectorAll("[data-u-flt]").forEach(btn => {
                btn.onclick = () => {
                    ex.querySelectorAll("[data-u-flt]").forEach(b => b.classList.remove("active"));
                    btn.classList.add("active");
                    P.userFilter = btn.dataset.uFlt || "";
                    loadFilteredUsers();
                };
            });
            document.getElementById("admp-u-sort").onchange = (e) => {
                P.userSort = e.target.value || "recent";
                loadFilteredUsers();
            };
        }

        // Hide redundant duplicate #adm-detail-recent-jobs card inside User Detail
        const dupJobs = document.getElementById("adm-detail-recent-jobs");
        if (dupJobs && dupJobs.closest(".card")) {
            dupJobs.closest(".card").classList.add("hidden");
        }
    }

    async function loadFilteredUsers() {
        const box = document.getElementById("admin-users-recent");
        if (!box) return;
        const qs = new URLSearchParams({ limit: "30", offset: "0", sort: P.userSort });
        if (P.userFilter) qs.set("filter", P.userFilter);
        const q = (document.getElementById("adm-user-search-q")?.value || "").trim();
        if (q) qs.set("search", q);
        try {
            const res = await reqFetch(`/api/admin/users?${qs.toString()}`, { headers: hdrs() });
            if (!res.ok) return;
            const d = await res.json();
            const users = d.users || [];
            if (!users.length) {
                box.innerHTML = `<p class="text-muted text-xs">${esc(L("ကိုက်ညီသော အသုံးပြုသူ မတွေ့ပါ", "No matching users"))}</p>`;
                return;
            }
            box.innerHTML = users.map(u => `<div class="admp-card-item admp-between" style="cursor:pointer" onclick="openAdminUserDetail(${Number(u.id)})">
                <div>
                    <strong class="text-xs">${esc(u.first_name || ("User " + u.id))}</strong>
                    <span class="text-xs text-muted">${u.username ? ` @${esc(u.username)}` : ""} · ${esc(u.id)}</span>
                    <div class="text-xs text-muted">🎙️ ${Number(u.balance_minutes || 0).toFixed(1)}m · 🎬 ${Number(u.recap_credits || 0).toFixed(1)}c</div>
                </div>
                <span class="admp-badge ${u.account_category === "PURCHASED" ? "admp-badge-ok" : ""}">${esc(u.account_category || "FREE")}</span>
            </div>`).join("");
        } catch (e) {}
    }

    // Wrap openAdminUserDetail to inject inline Grant Extend/Revoke + Unified Wallet Transactions
    const _origOpenUser = window.openAdminUserDetail;
    if (typeof _origOpenUser === "function") {
        window.openAdminUserDetail = async function (userId) {
            await _origOpenUser(userId);
            enhanceUserDetailPanel(userId);
        };
    }

    async function enhanceUserDetailPanel(userId) {
        try {
            const res = await reqFetch(`/api/admin/users/${encodeURIComponent(userId)}`, { headers: hdrs() });
            if (!res.ok) return;
            const u = await res.json();

            // 1. Add interactive +7d / +30d / Revoke buttons to #adm-detail-grants
            const grantsEl = document.getElementById("adm-detail-grants");
            const grants = u.wallet_grants || [];
            if (grantsEl && grants.length) {
                grantsEl.innerHTML = `<div class="text-xs font-semibold mb-1">⏳ ${esc(L("Package Lots & သက်တမ်း (တိုက်ရိုက်တိုး/ရုပ်သိမ်း)", "Wallet Lots & Expiry Actions"))}</div>`
                    + grants.map(g => {
                        const balBits = [];
                        if (Number(g.initial_recap_credits || 0) > 0 || Number(g.remaining_recap_credits || 0) > 0) {
                            balBits.push(`${Number(g.remaining_recap_credits || 0).toFixed(1)}/${Number(g.initial_recap_credits || 0).toFixed(1)}c`);
                        }
                        if (Number(g.initial_voice_minutes || 0) > 0 || Number(g.remaining_voice_minutes || 0) > 0) {
                            balBits.push(`${Number(g.remaining_voice_minutes || 0).toFixed(1)}/${Number(g.initial_voice_minutes || 0).toFixed(1)}m`);
                        }
                        const expTxt = g.expires_at ? `exp ${g.expires_at.slice(0, 10)}` : L("သက်တမ်းမကုန်", "No Expiry");
                        const acts = g.status === "ACTIVE"
                            ? `<div class="admp-row"><button type="button" class="mini-chip" data-g-ext="${esc(g.id)}" data-days="7">+7d</button><button type="button" class="mini-chip" data-g-ext="${esc(g.id)}" data-days="30">+30d</button><button type="button" class="mini-chip" style="color:#ef4444" data-g-rev="${esc(g.id)}">🚫</button></div>`
                            : `<span class="admp-badge admp-badge-err">${esc(g.status)}</span>`;
                        return `<div class="admp-card-item admp-between text-xs">
                            <div><b>${esc(g.package_name || g.package_code || g.source_type)}</b> · ${esc(balBits.join(" + ") || "0")}<br><span class="text-muted">${esc(expTxt)}</span></div>
                            ${acts}
                        </div>`;
                    }).join("");
                bindGrantActionButtons(grantsEl, () => window.openAdminUserDetail(userId));
            }

            // 2. Render Unified WalletTransaction Ledger inside User Detail
            const ledgerWrap = document.getElementById("adm-detail-recap-ledger")?.parentElement;
            if (ledgerWrap) {
                let wBox = document.getElementById("admp-detail-wallet-tx");
                if (!wBox) {
                    const lbl = document.createElement("div");
                    lbl.className = "card-label mt-3";
                    lbl.textContent = `💳 ${L("Unified Dual-Wallet Transactions", "Unified Dual-Wallet Transactions")}`;
                    wBox = document.createElement("div");
                    wBox.id = "admp-detail-wallet-tx";
                    wBox.className = "mt-2";
                    ledgerWrap.appendChild(lbl);
                    ledgerWrap.appendChild(wBox);
                }
                const wtx = u.wallet_transactions_recent || [];
                wBox.innerHTML = wtx.length ? wtx.map(tx => `<div class="admp-card-item admp-between text-xs">
                    <div><b>${esc(tx.tx_type)}</b> · ${esc(tx.service_type)}<br><span class="text-muted">${esc(tx.note || (tx.created_at || "").slice(0, 16))}</span></div>
                    <div style="text-align:right"><b>${tx.recap_credits_delta ? `${tx.recap_credits_delta > 0 ? "+" : ""}${tx.recap_credits_delta}c ` : ""}${tx.voice_minutes_delta ? `${tx.voice_minutes_delta > 0 ? "+" : ""}${tx.voice_minutes_delta}m` : ""}</b></div>
                </div>`).join("") : `<p class="text-muted text-xs">—</p>`;
            }
        } catch (e) {}
    }

    // ═══════════════════════════════════════════════════════════════════════
    // MODULE 7 — Audit Filter/Search, Overview Disk Health & Ops Queues, Create Plan
    // ═══════════════════════════════════════════════════════════════════════
    function mountAuditToolbar() {
        const listEl = document.getElementById("admin-audit-list");
        if (!listEl || document.getElementById("admp-audit-bar")) return;
        const bar = document.createElement("div");
        bar.id = "admp-audit-bar";
        bar.className = "admp-row mt-2";
        bar.innerHTML = `
            <select id="admp-audit-prefix" class="studio-input text-xs" style="width:118px">
                <option value="">All Actions</option>
                <option value="payment">💳 payment.*</option>
                <option value="grant">⏳ grant.*</option>
                <option value="user">👤 user.*</option>
                <option value="settings">⚙️ settings.*</option>
                <option value="pricing">💰 pricing.*</option>
                <option value="job">🎬 job.*</option>
                <option value="broadcast">📢 broadcast.*</option>
            </select>
            <input type="text" id="admp-audit-q" class="studio-input text-xs" style="flex:1;min-width:120px" placeholder="${esc(L("Target / Admin ID / Keyword ရှာရန်…", "Filter target / admin / details…"))}">
            <button type="button" class="mini-chip" id="admp-audit-go">🔎</button>
        `;
        listEl.parentElement.insertBefore(bar, listEl);
        document.getElementById("admp-audit-go").onclick = () => {
            P.auditPrefix = document.getElementById("admp-audit-prefix").value || "";
            P.auditQ = (document.getElementById("admp-audit-q").value || "").trim();
            P.auditOffset = 0;
            loadFilteredAudit(false);
        };
        document.getElementById("admp-audit-prefix").onchange = () => document.getElementById("admp-audit-go").click();
    }

    async function loadFilteredAudit(append = false) {
        const listEl = document.getElementById("admin-audit-list");
        if (!listEl) return;
        const qs = new URLSearchParams({ limit: "40", offset: String(P.auditOffset) });
        if (P.auditPrefix) qs.set("action_prefix", P.auditPrefix);
        if (P.auditQ) qs.set("q", P.auditQ);
        try {
            const res = await reqFetch(`/api/admin/audit-log?${qs.toString()}`, { headers: hdrs() });
            if (!res.ok) return;
            const rows = await res.json();
            const html = rows.map(r => `<div class="admp-card-item text-xs">
                <div class="admp-between"><b>${esc(r.action)}</b><span class="text-muted">${esc((r.created_at || "").replace("T", " ").slice(0, 16))}</span></div>
                <div class="text-muted mt-1">Admin ${esc(r.admin_id)} → ${esc(r.target_type)}:${esc(r.target_id)}</div>
                ${r.details ? `<div class="text-muted" style="word-break:break-word">${esc(String(r.details).slice(0, 180))}</div>` : ""}
            </div>`).join("");
            if (append) {
                const moreBtn = document.getElementById("admp-audit-more");
                if (moreBtn) moreBtn.remove();
                listEl.insertAdjacentHTML("beforeend", html);
            } else {
                listEl.innerHTML = html || `<p class="text-muted text-xs">${esc(L("မှတ်တမ်း မရှိပါ", "No matching audit entries"))}</p>`;
            }
            if (rows.length >= 40) {
                const btn = document.createElement("button");
                btn.type = "button";
                btn.id = "admp-audit-more";
                btn.className = "btn secondary-btn mt-2";
                btn.textContent = L("နောက်ထပ် ၄၀ ခု ပြမည်", "Load 40 More");
                btn.onclick = () => { P.auditOffset += 40; loadFilteredAudit(true); };
                listEl.appendChild(btn);
            }
        } catch (e) {}
    }

    function mountOverviewOpsHealth() {
        const ov = document.getElementById("adm-screen-overview");
        if (!ov || document.getElementById("admp-ops-health-card")) return;
        const card = document.createElement("div");
        card.id = "admp-ops-health-card";
        card.className = "card glass mt-3";
        card.innerHTML = `
            <div class="card-header-row" style="flex-wrap:nowrap;gap:8px">
                <div class="card-label" style="min-width:0">🩺 ${esc(L("Infra, Bot API & Shield", "Infra, Bot API & Shield"))}</div>
                <div class="admp-row-nowrap">
                    <button type="button" class="mini-chip" id="admp-lba-probe" style="white-space:nowrap">🔌 ${esc(L("Test API", "Test API"))}</button>
                    <button type="button" class="mini-chip" id="admp-ops-ref">🔄</button>
                </div>
            </div>
            <div id="admp-disk-body" class="mt-2 text-xs text-muted">${esc(L("စစ်ဆေးနေသည်…", "Checking…"))}</div>
            <div id="admp-infra-body" class="mt-2 text-xs"></div>
            <div id="admp-queues-body" class="mt-2 text-xs"></div>
        `;
        const anchor = document.getElementById("admin-recent-card");
        if (anchor) ov.insertBefore(card, anchor);
        else ov.appendChild(card);
        document.getElementById("admp-ops-ref").onclick = () => loadOverviewOpsHealth();
        document.getElementById("admp-lba-probe").onclick = async () => {
            const btn = document.getElementById("admp-lba-probe");
            if (btn) btn.disabled = true;
            try {
                const res = await reqFetch("/api/admin/local-bot-api/probe", { method: "POST", headers: hdrs() });
                const data = res.ok ? await res.json() : null;
                if (data && data.local_bot_api) {
                    const lba = data.local_bot_api;
                    toast(
                        lba.active
                            ? `Local Bot API Active (${lba.max_upload_mb} MB limit)`
                            : `Bot API: ${lba.mode} (${lba.max_upload_mb} MB${lba.last_error ? " — " + lba.last_error.slice(0, 80) : ""})`,
                        lba.active ? "success" : "info"
                    );
                }
                await loadOverviewOpsHealth();
            } catch (e) {
                toast("Local Bot API probe failed", "error");
            } finally {
                if (btn) btn.disabled = false;
            }
        };
    }

    async function loadOverviewOpsHealth() {
        const diskEl = document.getElementById("admp-disk-body");
        const infraEl = document.getElementById("admp-infra-body");
        const qEl = document.getElementById("admp-queues-body");
        if (!diskEl || !qEl) return;
        try {
            const [ovRes, qRes] = await Promise.all([
                reqFetch("/api/admin/overview", { headers: hdrs() }),
                reqFetch("/api/admin/ops-queues", { headers: hdrs() }),
            ]);
            if (ovRes.ok) {
                const ov = await ovRes.json();
                const d = ov.disk || {};
                if (d.total_bytes) {
                    const pct = Number(d.used_percent || 0).toFixed(1);
                    const freeGb = (Number(d.free_bytes || 0) / (1024 ** 3)).toFixed(2);
                    const totGb = (Number(d.total_bytes || 0) / (1024 ** 3)).toFixed(1);
                    const badge = d.low ? `<span class="admp-badge admp-badge-err">⚠️ LOW DISK</span>` : `<span class="admp-badge admp-badge-ok">✅ Healthy</span>`;
                    diskEl.innerHTML = `<div class="admp-between"><span>💾 Disk: <b>${pct}%</b> used · <b>${freeGb} GB</b> free / ${totGb} GB</span>${badge}</div><div class="admp-bar-bg"><div class="admp-bar-fill" style="width:${Math.min(100, Number(pct))}%"></div></div>`;
                } else {
                    diskEl.textContent = "💾 Disk telemetry unavailable";
                }
                if (infraEl) {
                    const lba = ov.local_bot_api || {};
                    const sh = ov.bandwidth_shield || {};
                    const lbaCls = lba.active ? "admp-badge-ok" : (lba.configured ? "admp-badge-warn" : "");
                    const lbaTxt = lba.active
                        ? `🚀 Local Bot API: ${esc(lba.max_upload_mb || 2000)} MB`
                        : (lba.configured
                            ? `⚠️ Bot API Fallback (${esc(lba.max_upload_mb || 50)} MB)`
                            : `📡 Official Bot API (${esc(lba.max_upload_mb || 50)} MB)`);
                    const shTxt = `🛡️ Shield: GZip + Flood(${esc(sh.static_flood_max_requests || 180)}/m, 429:${esc(sh.blocked_static_flood_429 || 0)})${sh.origin_lock_enabled ? " + OriginLock" : ""}`;
                    infraEl.innerHTML = `<div class="admp-row">
                        <span class="admp-badge ${lbaCls}" title="${esc(lba.last_error || lba.url || "")}">${lbaTxt}</span>
                        <span class="admp-badge admp-badge-ok">${shTxt}</span>
                    </div>`;
                }
            }
            if (qRes.ok) {
                const q = await qRes.json();
                const cvCnt = Number(q.custom_voice_pending_count || 0);
                const escCnt = Number(q.group_ai_escalations_pending_count || 0);
                qEl.innerHTML = `<div class="admp-row">
                    <span class="admp-badge ${cvCnt > 0 ? "admp-badge-warn" : "admp-badge-ok"}">🎤 Custom Voice Pending: ${cvCnt}</span>
                    <span class="admp-badge ${escCnt > 0 ? "admp-badge-warn" : "admp-badge-ok"}">🤖 Group AI Escalations: ${escCnt}</span>
                </div>`;
            }
        } catch (e) {}
    }

    function mountCreatePlanForm() {
        const pricingAcc = document.querySelector("#adm-acc-pricing .adm-acc-body");
        if (!pricingAcc || document.getElementById("admp-new-plan-wrap")) return;
        const wrap = document.createElement("div");
        wrap.id = "admp-new-plan-wrap";
        wrap.className = "admp-card-item mt-3";
        wrap.innerHTML = `
            <div class="admp-between">
                <strong class="text-xs">➕ ${esc(L("Custom / Promo Package အသစ်ဖန်တီးရန်", "Create New Custom / Promo Plan"))}</strong>
                <button type="button" class="mini-chip" id="admp-np-toggle">▾</button>
            </div>
            <div id="admp-np-body" class="hidden mt-2">
                <div class="admp-grid-2">
                    <input type="text" id="admp-np-code" class="studio-input text-xs" placeholder="code (e.g. promo_newyear)">
                    <input type="text" id="admp-np-name" class="studio-input text-xs" placeholder="Name (e.g. New Year Combo)">
                </div>
                <div class="admp-grid-2 mt-2">
                    <select id="admp-np-cat" class="studio-input text-xs"><option value="combo">Combo</option><option value="voice">Voice Clone</option><option value="recap">Recap Credits</option><option value="topup">Top-Up</option></select>
                    <input type="number" id="admp-np-price" class="studio-input text-xs" placeholder="Price MMK (e.g. 9900)">
                </div>
                <div class="admp-grid-2 mt-2">
                    <input type="number" id="admp-np-mins" class="studio-input text-xs" placeholder="Voice Mins" value="0">
                    <input type="number" id="admp-np-rc" class="studio-input text-xs" placeholder="Recap Credits" value="0">
                </div>
                <div class="admp-grid-2 mt-2">
                    <input type="number" id="admp-np-days" class="studio-input text-xs" placeholder="Validity Days" value="30">
                    <input type="text" id="admp-np-badge" class="studio-input text-xs" placeholder="Badge (e.g. Promo 🔥)">
                </div>
                <button type="button" class="btn mini-btn gold-btn mt-2" id="admp-np-save">➕ ${esc(L("Package အသစ်ထည့်မည်", "Create Plan"))}</button>
            </div>
        `;
        pricingAcc.appendChild(wrap);
        document.getElementById("admp-np-toggle").onclick = () => document.getElementById("admp-np-body").classList.toggle("hidden");
        document.getElementById("admp-np-save").onclick = async () => {
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
            const res = await reqFetch("/api/admin/pricing", {
                method: "POST",
                headers: { ...hdrs(), "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) { toast(data.detail || "Create plan failed", "error"); return; }
            toast(data.message || "Created", "success");
            document.getElementById("admp-np-body").classList.add("hidden");
            if (typeof loadAdminPricing === "function") loadAdminPricing();
        };
    }

    // ═══════════════════════════════════════════════════════════════════════
    // MODULE 8 — Advanced Runtime Settings (Concurrency, Timeout, AI Profile, FX)
    // ═══════════════════════════════════════════════════════════════════════
    function mountRuntimeSettingsCard() {
        const scr = document.getElementById("adm-screen-settings");
        if (!scr || document.getElementById("admp-runtime-card")) return;
        const det = document.createElement("details");
        det.id = "admp-runtime-card";
        det.className = "card glass vcp-card adm-accordion mt-3";
        det.innerHTML = `
            <summary class="adm-acc-head">
                <span class="adm-acc-title">⚡ ${esc(L("Worker, AI Profile & FX နှုန်းထား (Runtime Ops)", "Worker Concurrency, AI Profile & USD/MMK FX"))}</span>
                <span class="adm-acc-meta"><span class="adm-acc-pill">Ops</span><span class="adm-acc-chev">▾</span></span>
            </summary>
            <div class="adm-acc-body">
                <div class="admp-grid-2">
                    <div><label class="input-label text-xs">Recap Worker Concurrency (1–10)</label><input type="number" id="admp-rt-conc" class="studio-input text-xs" min="1" max="10" value="1"></div>
                    <div><label class="input-label text-xs">Stale Job Timeout (sec)</label><input type="number" id="admp-rt-timeout" class="studio-input text-xs" min="60" max="86400" value="2700"></div>
                </div>
                <div class="admp-grid-2 mt-2">
                    <div><label class="input-label text-xs">AI Prompt Profile</label><select id="admp-rt-profile" class="studio-input text-xs"><option value="v2">v2 (Production XML Hardened)</option><option value="v1">v1 (Legacy)</option></select></div>
                    <div><label class="input-label text-xs">USD → MMK FX Rate (Analytics)</label><input type="number" id="admp-rt-fx" class="studio-input text-xs" min="500" max="50000" step="50" value="4500"></div>
                </div>
                <button type="button" class="btn mini-btn gold-btn mt-2" id="admp-rt-save">💾 ${esc(L("Runtime ဆက်တင် သိမ်းမည်", "Save Runtime Settings"))}</button>
            </div>
        `;
        scr.appendChild(det);
        document.getElementById("admp-rt-save").onclick = async () => {
            const payload = {
                recap_worker_concurrency: Number(document.getElementById("admp-rt-conc").value || 1),
                recap_stale_job_timeout_seconds: Number(document.getElementById("admp-rt-timeout").value || 2700),
                ai_prompt_profile: document.getElementById("admp-rt-profile").value || "v2",
                usd_to_mmk_rate: Number(document.getElementById("admp-rt-fx").value || 4500),
            };
            const res = await reqFetch("/api/admin/settings", {
                method: "POST",
                headers: { ...hdrs(), "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) { toast(data.detail || "Save failed", "error"); return; }
            toast(data.message || L("သိမ်းပြီးပါပြီ", "Saved"), "success");
        };
    }

    async function loadRuntimeSettingsValues() {
        try {
            const res = await reqFetch("/api/admin/settings", { headers: hdrs() });
            if (!res.ok) return;
            const s = await res.json();
            const setVal = (id, v) => { const el = document.getElementById(id); if (el && v !== undefined && v !== null) el.value = String(v); };
            setVal("admp-rt-conc", s.recap_worker_concurrency ?? 1);
            setVal("admp-rt-timeout", s.recap_stale_job_timeout_seconds ?? 2700);
            setVal("admp-rt-profile", s.ai_prompt_profile || "v2");
            setVal("admp-rt-fx", s.usd_to_mmk_rate ?? 4500);
        } catch (e) {}
    }

    // ═══════════════════════════════════════════════════════════════════════
    // BOOTSTRAP & SECTION ROUTING HOOKS
    // ═══════════════════════════════════════════════════════════════════════
    function mountAll() {
        mountOverviewOpsHealth();
        mountBankAccountsCard();
        mountOrdersToolbar();
        mountExpiringWatchlistCard();
        mountBroadcastUpgrades();
        mountUsersEnhancements();
        mountAuditToolbar();
        mountCreatePlanForm();
        mountRuntimeSettingsCard();
    }

    function refreshForSection(sec) {
        if (sec === "overview") loadOverviewOpsHealth();
        if (sec === "payments") { renderEnhancedSlips(); loadBankAccounts(); loadEnhancedOrders(P.orderStatus); loadExpiringWatchlist(); }
        if (sec === "broadcast") { loadBroadcastPreview(); loadBroadcastHistory(); }
        if (sec === "activity") loadFilteredAudit(false);
        if (sec === "settings") loadRuntimeSettingsValues();
    }

    mountAll();

    const _origShow = window.showAdminSection;
    if (typeof _origShow === "function") {
        window.showAdminSection = function (section) {
            const res = _origShow(section);
            mountAll();
            setTimeout(() => refreshForSection(section), 60);
            return res;
        };
    }

    // Refresh currently visible section immediately upon lazy-load
    const cur = typeof admCurrentSection === "string" ? admCurrentSection : "overview";
    refreshForSection(cur);
    window.adminPro = { loadBankAccounts, loadEnhancedOrders, loadExpiringWatchlist, loadOverviewOpsHealth, loadBroadcastPreview, loadBroadcastHistory };
})();

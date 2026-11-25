/* ═══════════════════════════════════════════════════════════════════════════
   ADMIN PRO POWER-TOOLS MODULE (Lazy-loaded for Admins only — Round 1 Hardened)
   ═══════════════════════════════════════════════════════════════════════════ */
(function initAdminPro() {
    if (window._adminProMounted) return;
    window._adminProMounted = true;

    const isMy = () => {
        if (typeof currentLang === "string") return currentLang !== "en";
        return (localStorage.getItem("studio_lang") || localStorage.getItem("vcp_lang") || "my") !== "en";
    };
    const L = (my, en) => (isMy() ? my : en);
    const esc = (s) => (typeof escapeHtml === "function" ? escapeHtml(String(s ?? "")) : String(s ?? "").replace(/[&<>"']/g, m => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[m])));
    const toast = (msg, kind = "info") => { if (typeof showStudioToast === "function") showStudioToast(msg, kind); };
    const hdrs = () => (typeof getAuthHeaders === "function" ? getAuthHeaders() : {});
    const reqFetch = (url, opts) => (typeof fetchWithTimeout === "function" ? fetchWithTimeout(url, opts) : fetch(url, opts));
    const fmtYangon = (iso) => (typeof window.admFmtYangon === "function" ? window.admFmtYangon(iso) : String(iso || "—"));
    const fmtSecs = (sec) => (typeof window.admFmtSecs === "function" ? window.admFmtSecs(sec) : `${Math.round(Number(sec || 0) / 60)}m`);
    const fmtCredits = (cr) => (typeof window.admFmtCredits === "function" ? window.admFmtCredits(cr) : String(Number(Number(cr || 0).toFixed(2))));
    const fmtMmk = (mmk) => (typeof window.admFmtMmk === "function" ? window.admFmtMmk(mmk) : `${Math.round(Number(mmk || 0)).toLocaleString("en-US")} ကျပ်`);
    const askConfirm = (msg, onOk, onCancel) => {
        if (typeof showStudioConfirm === "function") showStudioConfirm(msg, onOk, onCancel);
        else if (onOk) onOk();
    };

    if (!document.getElementById("adm-pro-styles")) {
        const st = document.createElement("style");
        st.id = "adm-pro-styles";
        st.textContent = `
.admp-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.admp-row-nowrap{display:flex;flex-wrap:nowrap;gap:6px;align-items:center;flex-shrink:0}
.admp-between{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.admp-stack{display:flex;flex-direction:column;gap:10px}
.admp-field{display:flex;flex-direction:column;gap:4px;width:100%}
.admp-card-item{padding:10px 12px;border-radius:12px;background:rgba(15,23,42,0.04);border:1px solid rgba(15,23,42,0.10);margin-bottom:8px}
.admp-bank-item{border-left:4px solid #0047BA}
.admp-badge{display:inline-flex;align-items:center;gap:4px;font-size:11px;padding:3px 9px;border-radius:999px;background:rgba(15,23,42,0.07);color:inherit;font-weight:600;white-space:nowrap}
.admp-badge-ok{background:rgba(16,185,129,0.15);color:#059669}
.admp-badge-warn{background:rgba(245,158,11,0.18);color:#b45309}
.admp-badge-err{background:rgba(239,68,68,0.16);color:#dc2626}
.admp-preset-chip{font-size:12px;padding:5px 10px;border-radius:999px;border:1px solid rgba(15,23,42,0.18);background:rgba(15,23,42,0.04);color:inherit;cursor:pointer;white-space:nowrap;font-weight:600}
.admp-preset-chip.active,.admp-preset-chip:hover{border-color:#d4af37;background:rgba(212,175,55,0.16)}
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
`;
        document.head.appendChild(st);
    }

    const P = {
        accounts: [],
        presets: [],
        editingAccId: "",
        orderStatus: "",
        orderSearch: "",
        orderDays: 0,
        expDays: 7,
        broadcastAudience: "all",
        broadcastDeliverable: null,
        userFilter: "",
        userSort: "recent",
        auditPrefix: "",
        auditQ: "",
        auditOffset: 0,
        paymentsAuxLoaded: false,
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
                <div class="card-label">🏦 ${esc(L("ငွေလက်ခံ ဘဏ်နှင့် Wallet အကောင့်များ", "Bank & Wallet Payment Accounts"))}</div>
                <div class="admp-row">
                    <button type="button" class="mini-chip" id="admp-btn-add-bank">➕ ${esc(L("အကောင့်အသစ်ထည့်", "Add Account"))}</button>
                    <button type="button" class="mini-chip" id="admp-btn-refresh-banks">🔄</button>
                </div>
            </div>
            <p class="text-muted text-xs mt-1">${esc(L("KBZPay, WavePay အပြင် AYA, CB, Yoma, KBZ Bank, USDT စသည့် အကောင့်များ ထည့်သွင်း၊ ပိတ်ဖွင့်၊ QR တင်နိုင်ပါသည်။", "Add, toggle, or upload QR for unlimited Bank/Wallet accounts."))}</p>
            <div id="admp-bank-form-wrap" class="hidden admp-card-item mt-2">
                <div class="admp-between mb-2">
                    <strong class="text-xs" id="admp-bank-form-title">➕ ${esc(L("ငွေလက်ခံ အကောင့်အသစ်", "New Payment Account"))}</strong>
                    <button type="button" class="mini-chip" id="admp-bank-form-close">✕</button>
                </div>
                <div class="admp-row mb-2" id="admp-bank-presets"></div>
                <div class="admp-stack">
                    <div class="admp-field"><label class="input-label text-xs">${esc(L("ဘဏ် / Wallet အမည် *", "Provider / Bank *"))}</label><input type="text" id="admp-b-provider" class="studio-input text-xs" placeholder="KBZPay / WavePay / AYA Bank"></div>
                    <div class="admp-field"><label class="input-label text-xs">${esc(L("အမျိုးအစား", "Type"))}</label><select id="admp-b-type" class="studio-input text-xs"><option value="bank">🏦 ${esc(L("ဘဏ်အကောင့်", "Bank Account"))}</option><option value="mobile_wallet">💳 ${esc(L("မိုဘိုင်း Wallet", "Mobile Wallet"))}</option><option value="crypto">🪙 USDT / Crypto</option></select></div>
                    <div class="admp-field"><label class="input-label text-xs">${esc(L("အကောင့် / ဖုန်းနံပါတ် *", "Account / Phone No *"))}</label><input type="text" id="admp-b-number" class="studio-input text-xs" placeholder="09xxx / 0012-3456-..."></div>
                    <div class="admp-field"><label class="input-label text-xs">${esc(L("အကောင့်ပိုင်ရှင် အမည်", "Account Holder Name"))}</label><input type="text" id="admp-b-name" class="studio-input text-xs" placeholder="U / Daw ..."></div>
                    <div class="admp-field"><label class="input-label text-xs">${esc(L("ဘဏ်ခွဲ (မထည့်လည်းရ)", "Branch (Optional)"))}</label><input type="text" id="admp-b-branch" class="studio-input text-xs" placeholder="Yangon Main"></div>
                    <div class="admp-field"><label class="input-label text-xs">${esc(L("အစဉ်လိုက် နံပါတ်", "Display Order"))}</label><input type="number" id="admp-b-order" class="studio-input text-xs" value="30" min="1" max="999"></div>
                    <div class="admp-field"><label class="input-label text-xs">${esc(L("ကတ် အရောင်", "Card Accent Color"))}</label><input type="color" id="admp-b-color" value="#1D4ED8" style="width:100%;height:36px;border:none;background:none;cursor:pointer"></div>
                    <div class="admp-field"><label class="input-label text-xs">${esc(L("မှတ်ချက် (မထည့်လည်းရ)", "Payment Note (Optional)"))}</label><input type="text" id="admp-b-note" class="studio-input text-xs" placeholder="${esc(L("ဥပမာ - ငွေလွှဲမှတ်ချက်တွင် Telegram ID ထည့်ပါ", "e.g. Put Telegram ID in remark"))}"></div>
                    <label class="text-xs" style="display:flex;align-items:center;gap:8px"><input type="checkbox" id="admp-b-active" checked> ${esc(L("အသုံးပြုသူများ မြင်အောင် ဖွင့်ထားမည်", "Active (Visible to Users)"))}</label>
                    <button type="button" class="btn gold-btn" style="width:100%" id="admp-b-save">💾 ${esc(L("အကောင့် သိမ်းဆည်းမည်", "Save Account"))}</button>
                </div>
            </div>
            <div id="admp-banks-list" class="mt-2"><p class="text-muted text-xs">${esc(L("ရယူနေသည်…", "Loading…"))}</p></div>
        `;
        payScreen.appendChild(card);

        document.getElementById("admp-btn-add-bank").onclick = () => openBankForm(null);
        document.getElementById("admp-btn-refresh-banks").onclick = () => loadBankAccounts();
        document.getElementById("admp-bank-form-close").onclick = () => document.getElementById("admp-bank-form-wrap").classList.add("hidden");
        document.getElementById("admp-b-save").onclick = () => saveBankAccountForm();

        const payAcc = document.querySelector("#adm-acc-pay-maint .adm-acc-body");
        if (payAcc && !document.getElementById("admp-settings-banks-jump")) {
            const jump = document.createElement("div");
            jump.id = "admp-settings-banks-jump";
            jump.className = "mt-2 admp-card-item admp-between";
            jump.innerHTML = `<span class="text-xs">🏦 ${esc(L("ထပ်တိုး ဘဏ်အကောင့်များ နှင့် QR စီမံရန်", "Additional Bank Accounts & QR Manager"))}</span><button type="button" class="mini-chip">🏦 ${esc(L("စီမံရန် ဖွင့်မည်", "Manage Banks"))}</button>`;
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
            : `➕ ${L("ငွေလက်ခံ အကောင့်အသစ်", "New Payment Account")}`;
        document.getElementById("admp-b-provider").value = acc ? acc.provider : "";
        document.getElementById("admp-b-type").value = acc ? (acc.account_type || "bank") : "bank";
        document.getElementById("admp-b-number").value = acc ? acc.number : "";
        document.getElementById("admp-b-name").value = acc ? (acc.name || "") : "";
        document.getElementById("admp-b-branch").value = acc ? (acc.bank_branch || "") : "";
        document.getElementById("admp-b-color").value = (acc && /^#[0-9a-fA-F]{6}$/.test(acc.color || "")) ? acc.color : "#1D4ED8";
        document.getElementById("admp-b-order").value = acc ? (acc.sort_order ?? 30) : ((P.accounts.length + 1) * 10);
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
                    ? `<span class="admp-badge admp-badge-ok">● ${esc(L("ဖွင့်ထားသည်", "Active"))}</span>`
                    : `<span class="admp-badge admp-badge-err">○ ${esc(L("ပိတ်ထားသည်", "Off"))}</span>`;
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
                    } else if (act === "edit" && acc) {
                        openBankForm(acc);
                    } else if (act === "toggle" && acc) {
                        toggleBankAccount(acc, btn);
                    } else if (act === "del" && acc) {
                        deleteBankAccount(acc, btn);
                    } else if (act === "qr-up" && acc) {
                        triggerQrUpload(acc.id);
                    }
                };
            });
        } catch (err) {
            box.innerHTML = `<p class="text-xs text-danger">${esc(err.message)}</p>`;
        }
    }

    async function saveBankAccountForm() {
        const provider = (document.getElementById("admp-b-provider")?.value || "").trim();
        const number = (document.getElementById("admp-b-number")?.value || "").trim();
        if (!provider || !number) {
            toast(L("ဘဏ်အမည်နှင့် အကောင့်နံပါတ် ထည့်ပေးပါ", "Provider and account number are required"), "warning");
            return;
        }
        const saveBtn = document.getElementById("admp-b-save");
        if (saveBtn) saveBtn.disabled = true;
        try {
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
                    sort_order: Number(document.getElementById("admp-b-order")?.value ?? 30),
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
        } finally {
            if (saveBtn) saveBtn.disabled = false;
        }
    }

    function toggleBankAccount(acc, btnEl) {
        const nextActive = !acc.is_active;
        const msg = nextActive
            ? L(`${acc.provider} (${acc.number}) ကို အသုံးပြုသူများ ပြန်မြင်ရအောင် ဖွင့်မည်လား?`, `Activate ${acc.provider} (${acc.number}) for users?`)
            : L(`${acc.provider} (${acc.number}) ကို ငွေလွှဲစာရင်းမှ ခေတ္တပိတ်ထားမည်လား?`, `Deactivate ${acc.provider} (${acc.number})?`);
        askConfirm(msg, async () => {
            if (btnEl) btnEl.disabled = true;
            try {
                const res = await reqFetch("/api/admin/payment-accounts", {
                    method: "POST",
                    headers: { ...hdrs(), "Content-Type": "application/json" },
                    body: JSON.stringify({ account: { ...acc, is_active: nextActive } }),
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) { toast(data.detail || "Failed", "error"); return; }
                toast(nextActive ? L("ပြန်ဖွင့်ပြီးပါပြီ", "Account activated") : L("ပိတ်ထားလိုက်ပါပြီ", "Account deactivated"), "success");
                await loadBankAccounts();
                if (typeof refreshPlanPrices === "function") refreshPlanPrices();
            } finally {
                if (btnEl) btnEl.disabled = false;
            }
        });
    }

    function deleteBankAccount(acc, btnEl) {
        const msg = L(
            `${acc.provider} (${acc.number}) အကောင့်ကို အပြီးတိုင် ဖျက်သိမ်းမည်မှာ သေချာပါသလား?`,
            `Permanently delete ${acc.provider} (${acc.number})?`
        );
        askConfirm(msg, async () => {
            if (btnEl) btnEl.disabled = true;
            try {
                const res = await reqFetch(`/api/admin/payment-accounts/${encodeURIComponent(acc.id)}`, {
                    method: "DELETE",
                    headers: hdrs(),
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) { toast(data.detail || "Cannot delete", "error"); return; }
                toast(data.message || L("ဖျက်ပြီးပါပြီ", "Deleted"), "success");
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
    // MODULE 2 — Edit-and-Approve Slips + Rich Slip Metadata + Promo Presets
    // ═══════════════════════════════════════════════════════════════════════
    function renderSlipsDom(slips) {
        const listEl = document.getElementById("admin-slips-list");
        if (!listEl) return;
        const arr = Array.isArray(slips) ? slips : [];
        if (!arr.length) {
            if (typeof admStateEmpty === "function") admStateEmpty(listEl, "admin_no_slips");
            else listEl.innerHTML = `<p class="text-muted text-xs">${esc(L("စစ်ဆေးရန် ပြေစာ မရှိပါ", "No pending slips"))}</p>`;
            return;
        }
        listEl.innerHTML = arr.map(s => {
            const refLine = s.payment_reference ? `<div class="text-xs" style="color:#38bdf8">🧾 Ref: <code>${esc(s.payment_reference)}</code></div>` : "";
            const lockLine = s.review_claimed_by ? `<div class="text-xs" style="color:#f59e0b">🔒 ${esc(L("Admin စစ်ဆေးနေဆဲ", "Claimed by Admin"))} ${esc(s.review_claimed_by)}</div>` : "";
            const vTxt = (s.validity_days === 0) ? L("သက်တမ်းမကုန်", "No Expiry") : `${s.validity_days ?? 30} ${L("ရက်", "d")}`;
            const alloc = [
                Number(s.minutes || 0) > 0 ? `${fmtSecs(Number(s.minutes) * 60)} Voice` : "",
                Number(s.recap_credits || 0) > 0 ? `${fmtCredits(s.recap_credits)} Credit` : "",
                vTxt,
            ].filter(Boolean).join(" · ");
            const imgHtml = s.slip_url
                ? `<img data-auth-slip="${esc(s.slip_url)}" alt="slip" style="width:64px;height:64px;object-fit:cover;border-radius:8px;cursor:pointer;flex-shrink:0;background:rgba(255,255,255,0.06)" onclick="typeof openSlipModal==='function'&&openSlipModal('${esc(s.slip_url)}')">`
                : `<div style="width:64px;height:64px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:22px;background:rgba(255,255,255,0.06);flex-shrink:0">🧾</div>`;
            return `<div class="admp-card-item">
                <div class="admp-row" style="align-items:flex-start;flex-wrap:nowrap">
                    ${imgHtml}
                    <div style="flex:1;min-width:0">
                        <div class="admp-between">
                            <strong class="text-xs">${esc(s.package_name || s.package_code)} — ${esc(fmtMmk(s.amount_mmk || 0))}</strong>
                            <span class="text-xs text-muted">${esc(fmtYangon(s.created_at))}</span>
                        </div>
                        <div class="text-xs mt-1"><a href="javascript:void(0)" onclick="openAdminUserDetail(${Number(s.user_id)})" style="color:#d4af37;font-weight:600">${esc(s.user_name)} ${s.username ? `(@${esc(s.username)})` : ""}</a> · ID ${esc(s.user_id)}</div>
                        <div class="text-xs text-muted">${esc(alloc)} · <code>${esc(s.id)}</code></div>
                        ${refLine}${lockLine}
                    </div>
                </div>
                <div class="admp-row mt-2">
                    <button type="button" class="btn mini-btn gold-btn" onclick="adminApproveSlip('${esc(s.id)}')">✅ ${esc(L("အတည်ပြုမည်", "Approve"))}</button>
                    <button type="button" class="mini-chip" data-edit-slip="${esc(s.id)}">✏️ ${esc(L("ပြင်ပြီး အတည်ပြုမည်", "Edit & Approve"))}</button>
                    <button type="button" class="mini-chip" style="color:#ef4444" onclick="adminRejectSlip('${esc(s.id)}')">❌ ${esc(L("ငြင်းပယ်မည်", "Reject"))}</button>
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
    window._admpRenderSlipsFromData = renderSlipsDom;

    async function renderEnhancedSlips() {
        try {
            const res = await reqFetch("/api/admin/pending-slips", { headers: hdrs() });
            if (!res.ok) return;
            const slips = await res.json();
            window._lastAdminSlips = Array.isArray(slips) ? slips : [];
            renderSlipsDom(window._lastAdminSlips);
        } catch (e) {}
    }

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
            <div class="admp-between mb-2"><strong>✏️ ${esc(L("ပမာဏ ပြင်ဆင်ပြီး အတည်ပြုရန်", "Edit & Approve Slip"))}</strong><button type="button" class="mini-chip" id="admp-es-close">✕</button></div>
            <p class="text-xs text-muted">${esc(slip.user_name)} · ${esc(slip.package_name || slip.package_code)} (${esc(fmtMmk(slip.amount_mmk || 0))})</p>
            <div class="admp-field mt-2">
                <label class="input-label text-xs">${esc(L("အပိုဆု ပရိုမို အမြန်ရွေးရန်", "Promo Bonus Presets"))}</label>
                <div class="admp-row" id="admp-es-presets">
                    ${presets.map((p, idx) => `<button type="button" class="admp-preset-chip ${idx === 0 ? "active" : ""}" data-mult="${p.mult}">${esc(p.label)}</button>`).join("")}
                </div>
            </div>
            <div class="admp-stack mt-2">
                <div class="admp-field">
                    <label class="input-label text-xs">🎙️ ${esc(L("ပေးမည့် Voice မိနစ်", "Voice Minutes"))}</label>
                    <input type="number" id="admp-es-mins" class="studio-input text-xs" min="0" max="100000" step="1" value="${Math.round(baseMins)}">
                </div>
                <div class="admp-field">
                    <label class="input-label text-xs">🎬 ${esc(L("ပေးမည့် Recap Credit", "Recap Credits"))}</label>
                    <input type="number" id="admp-es-rc" class="studio-input text-xs" min="0" max="1000" step="0.01" value="${fmtCredits(baseCredits)}">
                </div>
                <div class="admp-field">
                    <label class="input-label text-xs">⏳ ${esc(L("သက်တမ်း (ရက်) — 0 = သက်တမ်းမကုန်", "Validity Days (0 = No Expiry)"))}</label>
                    <input type="number" id="admp-es-days" class="studio-input text-xs" min="0" max="3650" value="${Number(slip.validity_days ?? 30)}">
                </div>
                <button type="button" class="btn gold-btn" style="width:100%" id="admp-es-submit">✅ ${esc(L("ပြင်ပြီး အတည်ပြုမည်", "Confirm Edit & Approve"))}</button>
                <button type="button" class="btn secondary-btn" style="width:100%" id="admp-es-cancel">${esc(L("ပယ်ဖျက်မည်", "Cancel"))}</button>
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

        const close = () => bd.remove();
        document.getElementById("admp-es-close").onclick = close;
        document.getElementById("admp-es-cancel").onclick = close;
        document.getElementById("admp-es-submit").onclick = async () => {
            const submitBtn = document.getElementById("admp-es-submit");
            if (submitBtn) submitBtn.disabled = true;
            try {
                const body = {
                    request_id: slip.id,
                    corrected_minutes: Math.round(Number(minsInput.value || 0)),
                    corrected_recap_credits: Number(Number(rcInput.value || 0).toFixed(2)),
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
                if (typeof loadAdminPendingSlips === "function") loadAdminPendingSlips();
                else renderEnhancedSlips();
                loadEnhancedOrders(P.orderStatus);
                if (typeof loadAdminData === "function") loadAdminData();
            } finally {
                if (submitBtn) submitBtn.disabled = false;
            }
        };
    }

    // ═══════════════════════════════════════════════════════════════════════
    // MODULE 3 — Order History Search, Period & CSV Export via Telegram DM
    // ═══════════════════════════════════════════════════════════════════════
    function mountOrdersToolbar() {
        const listEl = document.getElementById("adm-orders-list");
        if (!listEl || document.getElementById("admp-orders-bar")) return;
        const bar = document.createElement("div");
        bar.id = "admp-orders-bar";
        bar.className = "admp-stack mt-2";
        bar.innerHTML = `
            <div class="admp-row">
                <input type="text" id="admp-ord-q" class="studio-input text-xs" style="flex:1 1 160px;min-width:130px" placeholder="${esc(L("ID / @user / Ref ရှာရန်…", "Search ID / @user / Ref…"))}">
                <select id="admp-ord-days" class="studio-input text-xs" style="width:104px">
                    <option value="0">${esc(L("အားလုံး", "All"))}</option>
                    <option value="7">${esc(L("၇ ရက်", "7D"))}</option>
                    <option value="30">${esc(L("၃၀ ရက်", "30D"))}</option>
                    <option value="90">${esc(L("၉၀ ရက်", "90D"))}</option>
                </select>
                <button type="button" class="mini-chip" id="admp-ord-go">🔎</button>
            </div>
            <button type="button" class="btn mini-btn secondary-btn" style="width:100%" id="admp-ord-csv">📥 ${esc(L("CSV စာရင်း Telegram DM သို့ ပို့မည်", "Send Orders CSV to Telegram DM"))}</button>
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
                // Receipt files are deleted on decision; only PENDING slips have live preview images
                const slipBtn = (o.status === "PENDING" && o.slip_url)
                    ? `<button type="button" class="mini-chip" onclick="typeof openSlipModal==='function'&&openSlipModal('${esc(o.slip_url)}')">🖼️ Slip</button>`
                    : "";
                const refTxt = o.payment_reference ? ` · Ref: <code>${esc(o.payment_reference)}</code>` : "";
                const rejTxt = o.rejection_reason ? `<div class="text-xs text-danger">❌ ${esc(o.rejection_reason)}</div>` : "";
                const dt = fmtYangon(o.created_at);
                const vTxt = (o.validity_days === 0) ? L("သက်တမ်းမကုန်", "No Expiry") : `${o.validity_days ?? 30} ${L("ရက်", "d")}`;
                return `<div class="admp-card-item">
                    <div class="admp-between">
                        <div><strong class="text-xs">${esc(o.package_name || o.package_code)}</strong> · <b>${esc(fmtMmk(o.amount_mmk || 0))}</b></div>
                        <span class="admp-badge ${bCls}">${esc(o.status)}</span>
                    </div>
                    <div class="admp-between text-xs text-muted mt-1">
                        <span><a href="javascript:void(0)" onclick="openAdminUserDetail(${Number(o.user_id)})" style="color:#d4af37">${esc(o.user_name)}</a> (${esc(o.user_id)}) · ${fmtSecs(Number(o.minutes || 0) * 60)} / ${fmtCredits(o.recap_credits)} Credit / ${esc(vTxt)}${refTxt}</span>
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
        const csvBtn = document.getElementById("admp-ord-csv");
        if (csvBtn) csvBtn.disabled = true;
        try {
            const res = await reqFetch("/api/admin/orders/export-dm", {
                method: "POST",
                headers: { ...hdrs(), "Content-Type": "application/json" },
                body: JSON.stringify({
                    status: P.orderStatus || null,
                    search: P.orderSearch || "",
                    days: Number(P.orderDays || 30),
                }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || "Export HTTP " + res.status);
            toast(L("DM သို့ ပို့ပြီးပါပြီ", "CSV sent to your Telegram DM"), "success");
        } catch (err) {
            toast(err.message, "error");
        } finally {
            if (csvBtn) csvBtn.disabled = false;
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // MODULE 4 — Global Expiring Watchlist & Confirmed Grant Extend / Revoke
    // ═══════════════════════════════════════════════════════════════════════
    function mountExpiringWatchlistCard() {
        const payScreen = document.getElementById("adm-screen-payments");
        if (!payScreen || document.getElementById("admp-expiring-card")) return;
        const card = document.createElement("div");
        card.id = "admp-expiring-card";
        card.className = "card glass mt-3";
        card.innerHTML = `
            <div class="card-header-row">
                <div class="card-label">⏳ ${esc(L("သက်တမ်းကုန်ခါနီး စာရင်း", "Expiring Grants Watchlist"))}</div>
                <div class="admp-row">
                    <select id="admp-exp-days" class="studio-input text-xs" style="width:94px">
                        <option value="3">${esc(L("၃ ရက်အတွင်း", "≤ 3d"))}</option>
                        <option value="7" selected>${esc(L("၇ ရက်အတွင်း", "≤ 7d"))}</option>
                        <option value="14">${esc(L("၁၄ ရက်အတွင်း", "≤ 14d"))}</option>
                        <option value="30">${esc(L("၃၀ ရက်အတွင်း", "≤ 30d"))}</option>
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
                if (Number(g.remaining_recap_credits || 0) > 0) parts.push(`${fmtCredits(g.remaining_recap_credits)} Credit`);
                if (Number(g.remaining_voice_minutes || 0) > 0) parts.push(fmtSecs(Number(g.remaining_voice_minutes) * 60));
                const remTxt = parts.join(" + ") || "0m";
                const warnCls = g.days_left <= 1 ? "admp-badge-err" : "admp-badge-warn";
                const pkgLabel = g.package_name || g.package_code || g.source_type || "Package";
                return `<div class="admp-card-item">
                    <div class="admp-between">
                        <div><a href="javascript:void(0)" onclick="openAdminUserDetail(${Number(g.user_id)})" style="color:#d4af37;font-weight:600" class="text-xs">${esc(g.user_name)} ${g.username ? `(@${esc(g.username)})` : ""}</a> · <span class="text-xs">${esc(remTxt)}</span></div>
                        <span class="admp-badge ${warnCls}">⏳ ${esc(g.days_left)} ${esc(L("ရက်ကျန်", "d"))}</span>
                    </div>
                    <div class="admp-between text-xs text-muted mt-1">
                        <span>${esc(pkgLabel)} · ${esc(fmtYangon(g.expires_at))}</span>
                        <div class="admp-row">
                            <button type="button" class="mini-chip" data-g-ext="${esc(g.grant_id)}" data-days="7" data-g-user="${esc(g.user_name)}" data-g-pkg="${esc(pkgLabel)}" data-g-exp="${esc(g.expires_at || "")}">+7d</button>
                            <button type="button" class="mini-chip" data-g-ext="${esc(g.grant_id)}" data-days="30" data-g-user="${esc(g.user_name)}" data-g-pkg="${esc(pkgLabel)}" data-g-exp="${esc(g.expires_at || "")}">+30d</button>
                            <button type="button" class="mini-chip" style="color:#ef4444" data-g-rev="${esc(g.grant_id)}" data-g-user="${esc(g.user_name)}" data-g-pkg="${esc(pkgLabel)}" data-g-rem="${esc(remTxt)}">🚫</button>
                        </div>
                    </div>
                </div>`;
            }).join("");
            bindGrantActionButtons(box, () => loadExpiringWatchlist());
        } catch (err) {
            box.innerHTML = `<p class="text-xs text-danger">${esc(err.message)}</p>`;
        }
    }

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
            <div class="admp-between mb-2"><strong>📅 ${esc(L(`သက်တမ်း +${addDays} ရက် တိုးပေးရန် အတည်ပြုချက်`, `Confirm +${addDays}d Grant Extension`))}</strong><button type="button" class="mini-chip" id="admp-ge-close">✕</button></div>
            <div class="admp-stack text-xs">
                <div>• ${esc(L("အသုံးပြုသူ", "User"))}: <b>${esc(meta.userName || "-")}</b></div>
                <div>• Package: <b>${esc(meta.pkgLabel || "-")}</b></div>
                <div>• ${esc(L("လက်ရှိ သက်တမ်းကုန်ချိန်", "Current Expiry"))}: <b>${esc(fmtYangon(meta.expiresAt))}</b></div>
                <div>• ${esc(L("သက်တမ်းကုန်ချိန် အသစ်", "New Expiry"))}: <b style="color:#34d399">${esc(fmtYangon(newExpIso))}</b></div>
                <label class="admp-card-item" style="display:flex;align-items:center;gap:8px;cursor:pointer;margin:4px 0 0">
                    <input type="checkbox" id="admp-ge-notify" checked>
                    <span>${esc(L("သုံးစွဲသူထံ Telegram စာ ပို့ အသိပေးမည်", "Send Telegram notification DM to user"))}</span>
                </label>
                <button type="button" class="btn gold-btn" style="width:100%" id="admp-ge-submit">✅ ${esc(L(`+${addDays} ရက် တိုးပေးမည်`, `Confirm +${addDays}d`))} </button>
                <button type="button" class="btn secondary-btn" style="width:100%" id="admp-ge-cancel">${esc(L("ပယ်ဖျက်မည်", "Cancel"))}</button>
            </div>
        </div>`;
        document.body.appendChild(bd);
        const close = () => bd.remove();
        document.getElementById("admp-ge-close").onclick = close;
        document.getElementById("admp-ge-cancel").onclick = close;
        document.getElementById("admp-ge-submit").onclick = async () => {
            const subBtn = document.getElementById("admp-ge-submit");
            const notifyUser = !!document.getElementById("admp-ge-notify")?.checked;
            if (subBtn) subBtn.disabled = true;
            try {
                const res = await reqFetch(`/api/admin/grants/${encodeURIComponent(meta.grantId)}/extend`, {
                    method: "POST",
                    headers: { ...hdrs(), "Content-Type": "application/json" },
                    body: JSON.stringify({
                        add_days: addDays,
                        reason: `Admin +${addDays}d extend`,
                        notify_user: notifyUser,
                    }),
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) { toast(data.detail || "Extend failed", "error"); return; }
                toast(data.message || `+${addDays}d extended`, "success");
                close();
                if (onDone) onDone();
            } finally {
                if (subBtn) subBtn.disabled = false;
            }
        };
    }

    function bindGrantActionButtons(root, onDone) {
        root.querySelectorAll("button[data-g-ext]").forEach(btn => {
            btn.onclick = () => {
                const gid = btn.dataset.gExt;
                const days = Number(btn.dataset.days || 7);
                openGrantExtendModal({
                    grantId: gid,
                    userName: btn.dataset.gUser || "User",
                    pkgLabel: btn.dataset.gPkg || "Package",
                    expiresAt: btn.dataset.gExp || "",
                }, days, onDone);
            };
        });
        root.querySelectorAll("button[data-g-rev]").forEach(btn => {
            btn.onclick = () => {
                const gid = btn.dataset.gRev;
                const uName = btn.dataset.gUser || "User";
                const pkg = btn.dataset.gPkg || "Package";
                const rem = btn.dataset.gRem || "0m";
                const msg = L(
                    `⚠️ Package ရုပ်သိမ်းမှု အတည်ပြုရန်\n\n• အသုံးပြုသူ: ${uName}\n• Package: ${pkg}\n• ရုပ်သိမ်းမည့် လက်ကျန်: ${rem} → 0m\n• အခြား Active Package မရှိပါက VIP မှ FREE သို့ ပြောင်းသွားပါမည်။\n\nအတည်ပြုပါသလား?`,
                    `Revoke ${pkg} for ${uName}? Remaining (${rem}) will become 0.`
                );
                askConfirm(msg, async () => {
                    btn.disabled = true;
                    try {
                        const res = await reqFetch(`/api/admin/grants/${encodeURIComponent(gid)}/revoke`, {
                            method: "POST",
                            headers: { ...hdrs(), "Content-Type": "application/json" },
                            body: JSON.stringify({ reason: "Admin revoked grant from console" }),
                        });
                        const data = await res.json().catch(() => ({}));
                        if (!res.ok) { toast(data.detail || "Revoke failed", "error"); return; }
                        toast(data.message || L("ရုပ်သိမ်းပြီးပါပြီ", "Revoked"), "success");
                        if (onDone) onDone();
                    } finally {
                        btn.disabled = false;
                    }
                });
            };
        });
    }

    // ═══════════════════════════════════════════════════════════════════════
    // MODULE 5 — Targeted Broadcast + Mandatory Confirm Across ALL Segments
    // ═══════════════════════════════════════════════════════════════════════
    const BC_AUDIENCE_LABELS = {
        all: () => L("အသုံးပြုသူ အားလုံး", "All Active Users"),
        vip: () => L("👑 VIP သုံးစွဲသူများ", "👑 VIP Users"),
        free: () => L("အခမဲ့ သုံးစွဲသူများ", "Free Users"),
        expiring_7d: () => L("⏳ ၇ ရက်အတွင်း သက်တမ်းကုန်မည့်သူ", "⏳ Expiring ≤7d"),
        active_7d: () => L("🔥 ၇ ရက်အတွင်း သုံးစွဲသူ", "🔥 Active 7d"),
        active_30d: () => L("📅 ၃၀ ရက်အတွင်း သုံးစွဲသူ", "📅 Active 30d"),
    };

    function mountBroadcastUpgrades() {
        const scr = document.getElementById("adm-screen-broadcast");
        if (!scr || document.getElementById("admp-bc-aud")) return;
        const textArea = document.getElementById("adm-broadcast-text");
        if (textArea) {
            const audWrap = document.createElement("div");
            audWrap.id = "admp-bc-aud";
            audWrap.className = "mt-2";
            const segKeys = ["all", "vip", "free", "expiring_7d", "active_7d", "active_30d"];
            audWrap.innerHTML = `
                <div class="admp-between mb-1">
                    <span class="text-xs text-muted">${esc(L("ပို့မည့် အုပ်စု ရွေးရန်", "Target Audience"))}</span>
                    <span class="admp-badge admp-badge-ok" id="admp-bc-count">…</span>
                </div>
                <div class="admp-row">${segKeys.map(k => `<button type="button" class="mini-chip ${k === "all" ? "active" : ""}" data-bc-aud="${k}">${esc(BC_AUDIENCE_LABELS[k]())}</button>`).join("")}</div>
            `;
            textArea.parentElement.insertBefore(audWrap, textArea);
            audWrap.querySelectorAll("[data-bc-aud]").forEach(btn => {
                btn.onclick = () => {
                    audWrap.querySelectorAll("[data-bc-aud]").forEach(b => b.classList.remove("active"));
                    btn.classList.add("active");
                    P.broadcastAudience = btn.dataset.bcAud || "all";
                    P.broadcastDeliverable = null;
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
            testBtn.style.width = "100%";
            testBtn.textContent = `🧪 ${L("မိမိ Telegram သို့ အစမ်းပို့ကြည့်မည်", "Test Send to My DM First")}`;
            testBtn.onclick = () => sendTestBroadcast();
            sendBtn.parentElement.insertBefore(testBtn, sendBtn);
        }
        const histCard = document.createElement("div");
        histCard.className = "card glass mt-3";
        histCard.id = "admp-bc-history-card";
        histCard.innerHTML = `
            <div class="card-header-row">
                <div class="card-label">📜 ${esc(L("ပို့ခဲ့ပြီးသော ကြေညာချက် မှတ်တမ်း", "Past Broadcast Campaigns"))}</div>
                <button type="button" class="mini-chip" id="admp-bc-hist-ref">🔄</button>
            </div>
            <div id="admp-bc-history" class="mt-2"><p class="text-muted text-xs">${esc(L("ရယူနေသည်…", "Loading…"))}</p></div>
        `;
        scr.appendChild(histCard);
        document.getElementById("admp-bc-hist-ref").onclick = () => loadBroadcastHistory();

        // Unified confirmed Broadcast sender for EVERY audience segment (all, vip, free, expiring_7d, active_7d, active_30d)
        window.adminSendBroadcast = async function () {
            const textEl = document.getElementById("adm-broadcast-text");
            const btn = document.getElementById("adm-broadcast-btn");
            const btnText = document.getElementById("adm-broadcast-btn-text");
            const reportBox = document.getElementById("adm-broadcast-report");
            const msg = (textEl?.value || "").trim();
            if (msg.length < 4) {
                toast(L("စာသား အနည်းဆုံး ၄ လုံး ရိုက်ပါ", "Minimum 4 characters"), "warning");
                return;
            }
            if (P.broadcastDeliverable == null) {
                await loadBroadcastPreview();
            }
            const segLabel = (BC_AUDIENCE_LABELS[P.broadcastAudience] || BC_AUDIENCE_LABELS.all)();
            const targetCnt = P.broadcastDeliverable != null ? P.broadcastDeliverable : "…";
            const previewSnippet = msg.length > 140 ? (msg.slice(0, 140) + "…") : msg;
            const confirmMsg = L(
                `📢 Broadcast ပို့ရန် အတည်ပြုချက်\n\n• လက်ခံမည့် အုပ်စု: ${segLabel}\n• ပို့ဆောင်မည့် ဦးရေ: ${targetCnt} ဦး\n• စာသား အစမ်းကြည့်:\n"${previewSnippet}"\n\nTelegram DM များ တိုက်ရိုက်ပို့ပါမည် (ပြန်ဖျက်၍ မရပါ)။ သေချာပါသလား?`,
                `Send live broadcast to ${segLabel} (${targetCnt} users)?\n\nPreview:\n"${previewSnippet}"`
            );
            askConfirm(confirmMsg, async () => {
                if (btn) btn.disabled = true;
                if (btnText) btnText.textContent = typeof t === "function" ? t("admin_broadcast_sending") : "Sending…";
                if (reportBox) {
                    reportBox.classList.remove("hidden");
                    reportBox.innerHTML = `<div class="adm-state"><span class="spinner"></span><span>${esc(L("ပို့ဆောင်နေပါသည်…", "Sending…"))}</span></div>`;
                }
                try {
                    const res = await reqFetch("/api/admin/broadcast", {
                        method: "POST",
                        headers: { ...hdrs(), "Content-Type": "application/json" },
                        body: JSON.stringify({ message: msg, audience: P.broadcastAudience, test_only: false }),
                    });
                    const data = await res.json().catch(() => ({}));
                    if (!res.ok) throw new Error(data.detail || "Broadcast failed");
                    if (reportBox) {
                        reportBox.innerHTML = `<div class="admp-card-item text-xs"><b>✅ ${esc(L("ပို့ဆောင်ပြီးပါပြီ", "Delivered"))}: ${data.sent_count || 0} / ${data.total_targeted || 0}</b></div>`;
                    }
                    toast(`✅ ${L("ပို့ပြီးပါပြီ", "Sent")}: ${data.sent_count || 0} / ${data.total_targeted || 0}`, "success");
                    if (textEl) {
                        textEl.value = "";
                        const c = document.getElementById("adm-broadcast-count");
                        if (c) c.innerText = "0";
                    }
                    loadBroadcastHistory();
                } catch (err) {
                    if (reportBox) reportBox.innerHTML = `<div class="adm-state adm-state-error"><span>❌ ${esc(err.message)}</span></div>`;
                    toast(err.message, "error");
                } finally {
                    if (btn) btn.disabled = false;
                    if (btnText && typeof t === "function") btnText.textContent = t("btn_send_broadcast");
                }
            });
        };
    }

    async function loadBroadcastPreview() {
        const el = document.getElementById("admp-bc-count");
        try {
            const res = await reqFetch(`/api/admin/broadcast/preview?audience=${encodeURIComponent(P.broadcastAudience)}`, { headers: hdrs() });
            if (!res.ok) return;
            const d = await res.json();
            P.broadcastDeliverable = Number(d.deliverable || 0);
            if (el) el.textContent = `👥 ${P.broadcastDeliverable} ${L("ဦး ပို့မည်", "recipients")}`;
        } catch (e) {}
    }

    async function sendTestBroadcast() {
        const testBtn = document.getElementById("admp-bc-test-btn");
        const msg = (document.getElementById("adm-broadcast-text")?.value || "").trim();
        if (msg.length < 4) { toast(L("စာသား အနည်းဆုံး ၄ လုံး ရိုက်ပါ", "Minimum 4 characters"), "warning"); return; }
        if (testBtn) testBtn.disabled = true;
        try {
            const res = await reqFetch("/api/admin/broadcast", {
                method: "POST",
                headers: { ...hdrs(), "Content-Type": "application/json" },
                body: JSON.stringify({ message: msg, audience: P.broadcastAudience, test_only: true }),
            });
            const d = await res.json().catch(() => ({}));
            if (!res.ok) { toast(d.detail || "Test DM failed", "error"); return; }
            toast(d.message || L("မိမိ DM သို့ အစမ်းပို့ပြီးပါပြီ", "Test DM sent"), "success");
        } finally {
            if (testBtn) testBtn.disabled = false;
        }
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
                box.innerHTML = `<p class="text-muted text-xs">${esc(L("မှတ်တမ်း မရှိသေးပါ", "No past campaigns yet"))}</p>`;
                return;
            }
            box.innerHTML = list.map(c => `<div class="admp-card-item">
                <div class="admp-between text-xs">
                    <span class="admp-badge admp-badge-ok">✅ ${c.sent_count}/${c.total_recipients}</span>
                    <span class="text-muted">${esc(fmtYangon(c.created_at))}</span>
                </div>
                <div class="text-xs mt-1">${esc(c.snippet)}</div>
            </div>`).join("");
        } catch (e) {}
    }

    // ═══════════════════════════════════════════════════════════════════════
    // MODULE 6 — Enhanced Users Filters + Zero-Duplicate User Detail Render
    // ═══════════════════════════════════════════════════════════════════════
    function mountUsersEnhancements() {
        const row = document.getElementById("adm-users-filter-chips");
        if (row && !document.getElementById("admp-users-extra-row")) {
            const ex = document.createElement("div");
            ex.id = "admp-users-extra-row";
            ex.className = "admp-stack mt-2";
            const flts = [
                ["EXPIRING_7D", L("⏳ ၇ ရက်အတွင်း ကုန်မည်", "⏳ ≤7d")],
                ["BANNED", L("🚫 Ban ထားသူ", "🚫 Banned")],
                ["BOT_BLOCKED", L("🔕 Bot ပိတ်ထားသူ", "🔕 Bot Blocked")],
                ["BYOK", "🔑 BYOK"],
                ["TEST", L("🧪 Test အကောင့်", "🧪 Test")],
            ];
            ex.innerHTML = `<div class="admp-row">${flts.map(([k, lbl]) => `<button type="button" class="mini-chip" data-u-flt="${k}">${esc(lbl)}</button>`).join("")}</div>`
                + `<select id="admp-u-sort" class="studio-input text-xs" style="width:100%"><option value="recent">${esc(L("အသစ်ဆုံး စာရင်းဝင်", "Newest"))}</option><option value="balance_desc">${esc(L("Voice လက်ကျန် အများဆုံး", "Top Voice Balance"))}</option><option value="recap">${esc(L("Recap Credit အများဆုံး", "Top Recap Credits"))}</option></select>`;
            row.parentElement.insertBefore(ex, row.nextSibling);
            ex.querySelectorAll("[data-u-flt]").forEach(btn => {
                btn.onclick = () => {
                    const wasActive = btn.classList.contains("active");
                    ex.querySelectorAll("[data-u-flt]").forEach(b => b.classList.remove("active"));
                    P.userFilter = wasActive ? "" : (btn.dataset.uFlt || "");
                    if (!wasActive) btn.classList.add("active");
                    loadFilteredUsers();
                };
            });
            row.addEventListener("click", (ev) => {
                const chip = ev.target && ev.target.closest ? ev.target.closest("[data-cat]") : null;
                if (!chip) return;
                P.userFilter = "";
                ex.querySelectorAll("[data-u-flt]").forEach(b => b.classList.remove("active"));
            });
            document.getElementById("admp-u-sort").onchange = (e) => {
                P.userSort = e.target.value || "recent";
                loadFilteredUsers();
            };
        }

        const dupJobs = document.getElementById("adm-detail-recent-jobs");
        if (dupJobs && dupJobs.closest(".card")) {
            dupJobs.closest(".card").classList.add("hidden");
        }
    }

    async function loadFilteredUsers() {
        const box = document.getElementById("admin-users-recent");
        if (!box) return;
        const qs = new URLSearchParams({ limit: "30", offset: "0", sort: P.userSort });
        if (typeof admUsersCategory === "string" && admUsersCategory) {
            qs.set("category", admUsersCategory);
        }
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
            window._lastAdminUsersList = users;
            // Shared renderer reading real balance_seconds + daily_free_pending_seconds + formatted credits
            if (typeof window.admUserRowHtml === "function") {
                box.innerHTML = users.map(window.admUserRowHtml).join("");
            } else {
                box.innerHTML = users.map(u => `<div class="admp-card-item" onclick="openAdminUserDetail(${Number(u.id)})"><b>${esc(u.first_name || "User")}</b> · ${esc(fmtSecs(u.balance_seconds || 0))} · ${esc(fmtCredits(u.recap_credits))} Credit</div>`).join("");
            }
        } catch (e) {}
    }

    function renderUserDetailEnhancements(u) {
        if (!u) return;
        const userId = u.id;
        const userName = u.first_name || ("User " + userId);

        const grantsEl = document.getElementById("adm-detail-grants");
        const grants = u.wallet_grants || [];
        if (grantsEl && grants.length) {
            grantsEl.innerHTML = `<div class="text-xs font-semibold mb-1">⏳ ${esc(L("ဝယ်ယူထားသော Package များနှင့် သက်တမ်း", "Wallet Lots & Expiry Actions"))}</div>`
                + grants.map(g => {
                    const balBits = [];
                    const remBits = [];
                    if (Number(g.initial_recap_credits || 0) > 0 || Number(g.remaining_recap_credits || 0) > 0) {
                        balBits.push(`${fmtCredits(g.remaining_recap_credits)}/${fmtCredits(g.initial_recap_credits)} Credit`);
                        if (Number(g.remaining_recap_credits || 0) > 0) remBits.push(`${fmtCredits(g.remaining_recap_credits)} Credit`);
                    }
                    if (Number(g.initial_voice_minutes || 0) > 0 || Number(g.remaining_voice_minutes || 0) > 0) {
                        balBits.push(`${fmtSecs(Number(g.remaining_voice_minutes || 0) * 60)}/${fmtSecs(Number(g.initial_voice_minutes || 0) * 60)}`);
                        if (Number(g.remaining_voice_minutes || 0) > 0) remBits.push(fmtSecs(Number(g.remaining_voice_minutes) * 60));
                    }
                    const pkgLabel = g.package_name || g.package_code || g.source_type || "Package";
                    const expTxt = g.expires_at ? fmtYangon(g.expires_at) : L("သက်တမ်းမကုန်", "No Expiry");
                    const acts = g.status === "ACTIVE"
                        ? `<div class="admp-row"><button type="button" class="mini-chip" data-g-ext="${esc(g.id)}" data-days="7" data-g-user="${esc(userName)}" data-g-pkg="${esc(pkgLabel)}" data-g-exp="${esc(g.expires_at || "")}">+7d</button><button type="button" class="mini-chip" data-g-ext="${esc(g.id)}" data-days="30" data-g-user="${esc(userName)}" data-g-pkg="${esc(pkgLabel)}" data-g-exp="${esc(g.expires_at || "")}">+30d</button><button type="button" class="mini-chip" style="color:#ef4444" data-g-rev="${esc(g.id)}" data-g-user="${esc(userName)}" data-g-pkg="${esc(pkgLabel)}" data-g-rem="${esc(remBits.join(" + ") || "0m")}">🚫</button></div>`
                        : `<span class="admp-badge admp-badge-err">${esc(g.status)}</span>`;
                    return `<div class="admp-card-item admp-between text-xs">
                        <div><b>${esc(pkgLabel)}</b> · ${esc(balBits.join(" + ") || "0m")}<br><span class="text-muted">${esc(expTxt)}</span></div>
                        ${acts}
                    </div>`;
                }).join("");
            bindGrantActionButtons(grantsEl, () => window.openAdminUserDetail(userId));
        }

        const ledgerWrap = document.getElementById("adm-detail-recap-ledger")?.parentElement;
        if (ledgerWrap) {
            let wBox = document.getElementById("admp-detail-wallet-tx");
            if (!wBox) {
                const lbl = document.createElement("div");
                lbl.className = "card-label mt-3";
                lbl.textContent = `💳 ${L("ပေါင်းစပ် Wallet အဝင်အထွက် မှတ်တမ်း", "Unified Wallet Transactions")}`;
                wBox = document.createElement("div");
                wBox.id = "admp-detail-wallet-tx";
                wBox.className = "mt-2";
                ledgerWrap.appendChild(lbl);
                ledgerWrap.appendChild(wBox);
            }
            const wtx = u.wallet_transactions_recent || [];
            wBox.innerHTML = wtx.length ? wtx.map(tx => {
                const crDelta = Number(tx.recap_credits_delta || 0);
                const vmDelta = Number(tx.voice_minutes_delta || 0);
                const deltaParts = [];
                if (crDelta !== 0) deltaParts.push(`${crDelta > 0 ? "+" : "-"}${fmtCredits(Math.abs(crDelta))} Credit`);
                if (vmDelta !== 0) deltaParts.push(`${vmDelta > 0 ? "+" : "-"}${fmtSecs(Math.abs(vmDelta) * 60)}`);
                return `<div class="admp-card-item admp-between text-xs">
                    <div><b>${esc(tx.tx_type)}</b> · ${esc(tx.service_type)}<br><span class="text-muted">${esc(tx.note ? tx.note + " · " : "")}${esc(fmtYangon(tx.created_at))}</span></div>
                    <div style="text-align:right"><b>${esc(deltaParts.join(" · ") || "0")}</b></div>
                </div>`;
            }).join("") : `<p class="text-muted text-xs">—</p>`;
        }
    }
    window._admpEnhanceUserDetailFromData = renderUserDetailEnhancements;

    // ═══════════════════════════════════════════════════════════════════════
    // MODULE 7 — Audit Filter, Overview Ops Health (No Double Fetch), Create Plan
    // ═══════════════════════════════════════════════════════════════════════
    function mountAuditToolbar() {
        const listEl = document.getElementById("admin-audit-list");
        if (!listEl || document.getElementById("admp-audit-bar")) return;
        const bar = document.createElement("div");
        bar.id = "admp-audit-bar";
        bar.className = "admp-row mt-2";
        bar.innerHTML = `
            <select id="admp-audit-prefix" class="studio-input text-xs" style="width:128px">
                <option value="">${esc(L("အားလုံး", "All Actions"))}</option>
                <option value="payment">💳 payment.*</option>
                <option value="orders">📥 orders.*</option>
                <option value="grant">⏳ grant.*</option>
                <option value="user">👤 user.*</option>
                <option value="settings">⚙️ settings.*</option>
                <option value="pricing">💰 pricing.*</option>
                <option value="job">🎬 job.*</option>
                <option value="history">🗑️ history.*</option>
                <option value="ops">🔌 ops.*</option>
                <option value="broadcast">📢 broadcast.*</option>
                <option value="group_mod">group_mod.*</option>
            </select>
            <input type="text" id="admp-audit-q" class="studio-input text-xs" style="flex:1;min-width:120px" placeholder="${esc(L("Target / Admin ID / စာသား ရှာရန်…", "Filter target / admin / details…"))}">
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
        if (!append) P.auditOffset = 0;
        const qs = new URLSearchParams({ limit: "40", offset: String(P.auditOffset) });
        if (P.auditPrefix) qs.set("action_prefix", P.auditPrefix);
        if (P.auditQ) qs.set("q", P.auditQ);
        try {
            const res = await reqFetch(`/api/admin/audit-log?${qs.toString()}`, { headers: hdrs() });
            if (!res.ok) return;
            const rows = await res.json();
            const html = rows.map(r => `<div class="admp-card-item text-xs">
                <div class="admp-between"><b>${esc(r.action)}</b><span class="text-muted">${esc(fmtYangon(r.created_at))}</span></div>
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
    window.loadAdminAudit = () => loadFilteredAudit(false);

    function mountOverviewOpsHealth() {
        const ov = document.getElementById("adm-screen-overview");
        if (!ov || document.getElementById("admp-ops-health-card")) return;
        const card = document.createElement("div");
        card.id = "admp-ops-health-card";
        card.className = "card glass mt-3";
        card.innerHTML = `
            <div class="card-header-row" style="flex-wrap:nowrap;gap:8px">
                <div class="card-label" style="min-width:0">🩺 ${esc(L("ဆာဗာ သိုလှောင်မှုနှင့် Bot API", "Server Storage & Bot API"))}</div>
                <div class="admp-row-nowrap">
                    <button type="button" class="mini-chip" id="admp-lba-probe" style="white-space:nowrap">🔌 ${esc(L("API စမ်းမည်", "Probe API"))}</button>
                    <button type="button" class="mini-chip" id="admp-ops-ref">🔄</button>
                </div>
            </div>
            <div id="admp-disk-body" class="mt-2 text-xs text-muted">${esc(L("စစ်ဆေးနေသည်…", "Checking…"))}</div>
            <div id="admp-infra-body" class="mt-2 text-xs"></div>
        `;
        const anchor = document.getElementById("admin-recent-card");
        if (anchor) ov.insertBefore(card, anchor);
        else ov.appendChild(card);

        document.getElementById("admp-ops-ref").onclick = () => {
            if (typeof loadAdminData === "function") loadAdminData();
        };
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
                toast(L("Bot API စစ်ဆေးမှု မအောင်မြင်ပါ", "Local Bot API probe failed"), "error");
            } finally {
                if (btn) btn.disabled = false;
            }
        };
        if (window._lastAdminOverview) {
            paintOverviewOpsFromStats(window._lastAdminOverview);
        }
    }

    function paintOverviewOpsFromStats(ov) {
        const diskEl = document.getElementById("admp-disk-body");
        const infraEl = document.getElementById("admp-infra-body");
        if (!diskEl || !ov) return;
        const d = ov.disk || {};
        if (d.total_bytes) {
            const pct = Number(d.used_percent || 0).toFixed(1);
            const freeGb = (Number(d.free_bytes || 0) / (1024 ** 3)).toFixed(2);
            const totGb = (Number(d.total_bytes || 0) / (1024 ** 3)).toFixed(1);
            const badge = d.low
                ? `<span class="admp-badge admp-badge-err">⚠️ ${esc(L("နေရာနည်းနေသည်", "LOW DISK"))}</span>`
                : `<span class="admp-badge admp-badge-ok">✅ ${esc(L("ကောင်းမွန်သည်", "Healthy"))}</span>`;
            diskEl.innerHTML = `<div class="admp-between"><span>💾 Disk: <b>${pct}%</b> · <b>${freeGb} GB</b> ${esc(L("လွတ်သည်", "free"))} / ${totGb} GB</span>${badge}</div><div class="admp-bar-bg"><div class="admp-bar-fill" style="width:${Math.min(100, Number(pct))}%"></div></div>`;
        } else {
            diskEl.textContent = L("💾 Disk အချက်အလက် မရနိုင်ပါ", "💾 Disk telemetry unavailable");
        }
        if (infraEl) {
            const lba = ov.local_bot_api || {};
            const sh = ov.bandwidth_shield || {};
            const lbaCls = lba.active ? "admp-badge-ok" : (lba.configured ? "admp-badge-warn" : "");
            const lbaTxt = lba.active
                ? `🚀 Local Bot API: ${esc(lba.max_upload_mb || 2000)} MB`
                : (lba.configured
                    ? `⚠️ Bot API (${esc(lba.max_upload_mb || 50)} MB)`
                    : `📡 Official Bot API (${esc(lba.max_upload_mb || 50)} MB)`);
            const shTxt = `🛡️ GZip · 429:${esc(sh.blocked_static_flood_429 || 0)}`;
            infraEl.innerHTML = `<div class="admp-row">
                <span class="admp-badge ${lbaCls}" title="${esc(lba.last_error || lba.url || "")}">${lbaTxt}</span>
                <span class="admp-badge admp-badge-ok">${shTxt}</span>
            </div>`;
            if (lba.configured && !lba.active) {
                const actBtn = document.createElement("button");
                actBtn.type = "button";
                actBtn.className = "btn mini-btn secondary-btn mt-2";
                actBtn.style.width = "100%";
                actBtn.id = "admp-lba-activate";
                actBtn.textContent = "🚀 " + L("Local Bot API (2,000 MB) ပြောင်းဖွင့်မည်", "Activate Local Bot API (2,000 MB)");
                actBtn.onclick = () => {
                    const confirmMsg = L(
                        "⚠️ Local Bot API ပြောင်းဖွင့်ရန် အတည်ပြုချက်\n\n• Official Telegram API မှ logOut ပြုလုပ်ပြီး Local Bot API (2,000 MB upload) သို့ ပြောင်းလဲချိတ်ဆက်ပါမည်။\n• Local Bot API ဆာဗာ အဆင်သင့်ဖြစ်နေမှသာ အတည်ပြုပါ။",
                        "Activate Local Bot API? This runs official Telegram logOut and switches bot upload transport."
                    );
                    askConfirm(confirmMsg, async () => {
                        actBtn.disabled = true;
                        try {
                            const aRes = await reqFetch("/api/admin/local-bot-api/activate", {
                                method: "POST",
                                headers: { ...hdrs(), "Content-Type": "application/json" },
                                body: JSON.stringify({}),
                            });
                            const aData = aRes.ok ? await aRes.json() : null;
                            toast(
                                aData?.success
                                    ? L("Local Bot API ချိတ်ဆက်ပြီးပါပြီ", "Local Bot API activated")
                                    : L("Local Bot API ချိတ်ဆက်၍ မရသေးပါ", "Local Bot API unavailable"),
                                aData?.success ? "success" : "warning"
                            );
                            if (typeof loadAdminData === "function") loadAdminData();
                        } finally {
                            actBtn.disabled = false;
                        }
                    });
                };
                infraEl.appendChild(actBtn);
            }
        }
    }
    window._admpRenderOverviewFromStats = paintOverviewOpsFromStats;

    function mountCreatePlanForm() {
        const pricingAcc = document.querySelector("#adm-acc-pricing .adm-acc-body");
        if (!pricingAcc || document.getElementById("admp-new-plan-wrap")) return;
        const wrap = document.createElement("div");
        wrap.id = "admp-new-plan-wrap";
        wrap.className = "admp-card-item mt-3";
        wrap.innerHTML = `
            <div class="admp-between">
                <strong class="text-xs">➕ ${esc(L("ပရိုမို / Package အသစ် ဖန်တီးရန်", "Create New Promo Plan"))}</strong>
                <button type="button" class="mini-chip" id="admp-np-toggle">▾</button>
            </div>
            <div id="admp-np-body" class="hidden admp-stack mt-2">
                <div class="admp-field"><label class="input-label text-xs">${esc(L("Package ကုဒ် (အင်္ဂလိပ်စာလုံးအသေး)", "Plan Code"))}</label><input type="text" id="admp-np-code" class="studio-input text-xs" placeholder="promo_newyear"></div>
                <div class="admp-field"><label class="input-label text-xs">${esc(L("Package အမည်", "Plan Name"))}</label><input type="text" id="admp-np-name" class="studio-input text-xs" placeholder="New Year Combo"></div>
                <div class="admp-field"><label class="input-label text-xs">${esc(L("အမျိုးအစား", "Category"))}</label><select id="admp-np-cat" class="studio-input text-xs"><option value="combo">Combo</option><option value="voice">Voice Clone</option><option value="recap">Recap Credits</option></select></div>
                <div class="admp-field"><label class="input-label text-xs">${esc(L("ဈေးနှုန်း (ကျပ်)", "Price (MMK)"))}</label><input type="number" id="admp-np-price" class="studio-input text-xs" placeholder="15000"></div>
                <div class="admp-field"><label class="input-label text-xs">${esc(L("Voice မိနစ်", "Voice Minutes"))}</label><input type="number" id="admp-np-mins" class="studio-input text-xs" value="0"></div>
                <div class="admp-field"><label class="input-label text-xs">${esc(L("Recap Credit", "Recap Credits"))}</label><input type="number" id="admp-np-rc" class="studio-input text-xs" value="0"></div>
                <div class="admp-field"><label class="input-label text-xs">${esc(L("သက်တမ်း (ရက်)", "Validity Days"))}</label><input type="number" id="admp-np-days" class="studio-input text-xs" value="30"></div>
                <div class="admp-field"><label class="input-label text-xs">${esc(L("တံဆိပ် (မထည့်လည်းရ)", "Badge (Optional)"))}</label><input type="text" id="admp-np-badge" class="studio-input text-xs" placeholder="Promo 🔥"></div>
                <button type="button" class="btn gold-btn" style="width:100%" id="admp-np-save">➕ ${esc(L("Package အသစ် ထုတ်ဝေမည်", "Publish New Plan"))}</button>
            </div>
        `;
        pricingAcc.appendChild(wrap);
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
                toast(L("ကုဒ်၊ အမည် နှင့် ဈေးနှုန်း မှန်ကန်စွာ ဖြည့်ပါ", "Enter valid code, name, and price"), "warning");
                return;
            }
            const confirmMsg = L(
                `💰 Package အသစ် ထုတ်ဝေရန် အတည်ပြုချက်\n\n• အမည်: ${payload.name} (${payload.code})\n• ဈေးနှုန်း: ${fmtMmk(payload.price_mmk)}\n• ပမာဏ: Voice ${payload.minutes}m · Recap ${payload.recap_credits} Credit\n• သက်တမ်း: ${payload.validity_days === 0 ? "သက်တမ်းမကုန်" : payload.validity_days + " ရက်"}\n\nသုံးစွဲသူအားလုံး ဝယ်ယူနိုင်သော စာရင်းတွင် ချက်ချင်း ပေါ်လာပါမည်။ အတည်ပြုပါသလား?`,
                `Publish live plan ${payload.name} (${payload.code}) at ${fmtMmk(payload.price_mmk)}?`
            );
            askConfirm(confirmMsg, async () => {
                const saveBtn = document.getElementById("admp-np-save");
                if (saveBtn) saveBtn.disabled = true;
                try {
                    const res = await reqFetch("/api/admin/pricing", {
                        method: "POST",
                        headers: { ...hdrs(), "Content-Type": "application/json" },
                        body: JSON.stringify(payload),
                    });
                    const data = await res.json().catch(() => ({}));
                    if (!res.ok) { toast(data.detail || "Create plan failed", "error"); return; }
                    toast(data.message || L("ဖန်တီးပြီးပါပြီ", "Created"), "success");
                    document.getElementById("admp-np-body").classList.add("hidden");
                    if (typeof loadAdminPricing === "function") loadAdminPricing();
                } finally {
                    if (saveBtn) saveBtn.disabled = false;
                }
            });
        };
    }

    // ═══════════════════════════════════════════════════════════════════════
    // MODULE 8 — Advanced Runtime Settings (Integrated into Unified Dirty Save)
    // ═══════════════════════════════════════════════════════════════════════
    function mountRuntimeSettingsCard() {
        const scr = document.getElementById("adm-screen-settings");
        if (!scr || document.getElementById("admp-runtime-card")) return;
        const det = document.createElement("details");
        det.id = "admp-runtime-card";
        det.className = "card glass vcp-card adm-accordion mt-3";
        det.innerHTML = `
            <summary class="adm-acc-head">
                <span class="adm-acc-title">⚡ ${esc(L("ဆာဗာ စွမ်းဆောင်ရည်နှင့် ငွေလဲနှုန်း ချိန်ညှိချက်", "Worker Concurrency, AI Profile & FX"))}</span>
                <span class="adm-acc-meta"><span class="adm-acc-pill">Ops</span><span class="adm-acc-chev">▾</span></span>
            </summary>
            <div class="adm-acc-body admp-stack">
                <div class="admp-field"><label class="input-label text-xs">${esc(L("Recap တစ်ပြိုင်နက် လုပ်ဆောင်နိုင်မှု (1–10)", "Recap Worker Concurrency (1–10)"))}</label><input type="number" id="admp-rt-conc" class="studio-input text-xs" min="1" max="10" value="1"></div>
                <div class="admp-field"><label class="input-label text-xs">${esc(L("အလုပ် ရပ်တန့်ချိန် ကန့်သတ်ချက် (စက္ကန့်)", "Stale Job Timeout (sec)"))}</label><input type="number" id="admp-rt-timeout" class="studio-input text-xs" min="60" max="86400" value="2700"></div>
                <div class="admp-field"><label class="input-label text-xs">${esc(L("AI Prompt ပရိုဖိုင်", "AI Prompt Profile"))}</label><select id="admp-rt-profile" class="studio-input text-xs"><option value="v2">v2 (Production)</option><option value="v1">v1 (Legacy)</option></select></div>
                <div class="admp-field"><label class="input-label text-xs">${esc(L("USD မှ ကျပ် တွက်ချက်နှုန်း (Analytics အတွက်)", "USD to MMK FX Rate"))}</label><input type="number" id="admp-rt-fx" class="studio-input text-xs" min="500" max="50000" step="50" value="4500"></div>
                <button type="button" class="btn gold-btn" style="width:100%" id="admp-rt-save">💾 ${esc(L("ပြောင်းလဲမှုများ သိမ်းဆည်းမည်", "Save Settings"))}</button>
            </div>
        `;
        const saveBar = document.getElementById("adm-settings-save-bar");
        if (saveBar && saveBar.parentElement === scr) scr.insertBefore(det, saveBar);
        else scr.appendChild(det);

        document.getElementById("admp-rt-save").onclick = () => {
            if (typeof window.saveAdminSettings === "function") window.saveAdminSettings();
        };
    }

    // ═══════════════════════════════════════════════════════════════════════
    // BOOTSTRAP & ZERO-DUPLICATE SECTION HOOKS
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

    function onSectionActivated(sec) {
        if (sec === "payments" && !P.paymentsAuxLoaded) {
            P.paymentsAuxLoaded = true;
            loadBankAccounts();
            loadExpiringWatchlist();
        }
        if (sec === "broadcast") {
            loadBroadcastPreview();
            loadBroadcastHistory();
        }
    }

    mountAll();
    if (Array.isArray(window._lastAdminSlips)) renderSlipsDom(window._lastAdminSlips);
    if (window._lastAdminOverview) paintOverviewOpsFromStats(window._lastAdminOverview);
    if (window._lastAdminUserDetail) renderUserDetailEnhancements(window._lastAdminUserDetail);

    const _origShow = window.showAdminSection;
    if (typeof _origShow === "function") {
        window.showAdminSection = function (section) {
            mountAll();
            const res = _origShow(section);
            onSectionActivated(section);
            return res;
        };
    }

    const cur = typeof admCurrentSection === "string" ? admCurrentSection : "overview";
    onSectionActivated(cur);
    window.adminPro = {
        mountAll,
        loadBankAccounts,
        loadEnhancedOrders,
        loadExpiringWatchlist,
        loadBroadcastPreview,
        loadBroadcastHistory,
    };
})();

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from app.services.i18n import t


def language_selection_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇲🇲 မြန်မာစာ", callback_data="set_lang:my"),
                InlineKeyboardButton(text="🇬🇧 English", callback_data="set_lang:en"),
            ]
        ]
    )


def membership_gate_kb(channel_link: str, group_link: str, lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_join_channel", lang), url=channel_link)],
            [InlineKeyboardButton(text=t("btn_join_group", lang), url=group_link)],
            [InlineKeyboardButton(text=t("btn_verify_membership", lang), callback_data="gate:verify")],
        ]
    )


def main_menu_kb(is_admin: bool = False, lang: str = "my") -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=t("btn_menu_voice_clone", lang), callback_data="menu:voice_clone")],
        [
            InlineKeyboardButton(text=t("btn_menu_buy_minutes", lang), callback_data="menu:buy_minutes"),
            InlineKeyboardButton(text=t("btn_menu_refer_earn", lang), callback_data="menu:refer_earn"),
        ],
        [
            InlineKeyboardButton(text=t("btn_menu_custom_voice", lang), callback_data="menu:custom_voice"),
            InlineKeyboardButton(text=t("btn_menu_help_settings", lang), callback_data="menu:help_settings"),
        ],
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton(text=t("btn_admin_dashboard", lang), callback_data="admin:dashboard")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def help_settings_kb(
    video_link: str | None = None,
    is_video_enabled: bool = True,
    lang: str = "my",
) -> InlineKeyboardMarkup:
    buttons = []
    if is_video_enabled and video_link:
        buttons.append([
            InlineKeyboardButton(text=t("btn_watch_tutorial", lang), url=video_link)
        ])

    my_badge = "✅ 🇲🇲 မြန်မာစာ" if lang == "my" else "🇲🇲 မြန်မာစာ"
    en_badge = "✅ 🇬🇧 English" if lang == "en" else "🇬🇧 English"
    buttons.append([
        InlineKeyboardButton(text=t("btn_menu_voice_samples", lang), callback_data="menu:voice_samples"),
        InlineKeyboardButton(text=t("btn_menu_history", lang), callback_data="menu:history"),
    ])
    buttons.append([
        InlineKeyboardButton(text=my_badge, callback_data="user_lang:my"),
        InlineKeyboardButton(text=en_badge, callback_data="user_lang:en"),
    ])
    buttons.append([
        InlineKeyboardButton(text=t("btn_main_menu", lang), callback_data="menu:home")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def back_to_menu_kb(lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_main_menu", lang), callback_data="menu:home")]
        ]
    )


def admin_dashboard_kb(lang: str = "my", pending_payments: int = 0, pending_cvr: int = 0) -> InlineKeyboardMarkup:
    pay_badge = f" ({pending_payments})" if pending_payments > 0 else ""
    cvr_badge = f" ({pending_cvr})" if pending_cvr > 0 else ""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("admin_btn_voice_mgmt", lang), callback_data="admin:voices"),
                InlineKeyboardButton(text=f"{t('admin_btn_minutes_payments', lang)}{pay_badge}", callback_data="admin:payments"),
            ],
            [
                InlineKeyboardButton(text=t("admin_btn_users_referrals", lang), callback_data="admin:users"),
                InlineKeyboardButton(text=t("admin_btn_queue_monitor", lang), callback_data="admin:queue"),
            ],
            [
                InlineKeyboardButton(text=f"👑 Custom Voice{cvr_badge}", callback_data="admin:cvr"),
                InlineKeyboardButton(text=t("admin_btn_bot_settings", lang), callback_data="admin:settings"),
            ],
            [
                InlineKeyboardButton(text="📜 Audit Logs", callback_data="admin:audit"),
                InlineKeyboardButton(text=t("admin_btn_tutorial_mgr", lang), callback_data="admin:tutorial"),
            ],
            [
                InlineKeyboardButton(text=t("admin_btn_broadcast", lang), callback_data="admin:broadcast"),
                InlineKeyboardButton(text=t("admin_btn_refresh", lang), callback_data="admin:refresh"),
            ],
            [
                InlineKeyboardButton(text=t("btn_main_menu", lang), callback_data="menu:home"),
            ],
        ]
    )


def admin_broadcast_audience_kb(
    registered_count: int,
    imported_all_count: int,
    imported_unregistered_count: int,
    lang: str = "my",
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"👥 Registered All ({registered_count})",
                    callback_data="admin:bc_group:REGISTERED_ALL",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"📥 Imported All ({imported_all_count})",
                    callback_data="admin:bc_group:IMPORTED_ALL",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"⏳ Imported Unregistered ({imported_unregistered_count})",
                    callback_data="admin:bc_group:IMPORTED_UNREGISTERED",
                )
            ],
            [
                InlineKeyboardButton(text="❌ Cancel", callback_data="admin:broadcast_cancel")
            ],
        ]
    )


def admin_broadcast_prompt_kb(lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("admin_btn_broadcast_cancel", lang), callback_data="admin:broadcast_cancel")]
        ]
    )


def admin_broadcast_preview_kb(token: str, lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("admin_btn_broadcast_confirm", lang), callback_data=f"admin:broadcast_send:{token}")],
            [InlineKeyboardButton(text=t("admin_btn_broadcast_cancel", lang), callback_data="admin:broadcast_cancel")],
        ]
    )


def admin_users_list_kb(users: list, page: int, total_pages: int, lang: str = "my") -> InlineKeyboardMarkup:
    buttons = []
    for u in users:
        uname = f"@{u.username}" if u.username else "no_username"
        btn_text = f"#{u.id} · {uname} ({u.account_category})"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"admin:user_detail:{u.id}:{page}")])

    # Navigation pagination row
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton(text="◀ Prev", callback_data=f"admin:users_page:{page - 1}"))
    nav_row.append(InlineKeyboardButton(text=f"📄 {page}/{total_pages}", callback_data=f"admin:users_page:{page}"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton(text="Next ▶", callback_data=f"admin:users_page:{page + 1}"))
    buttons.append(nav_row)

    buttons.append([
        InlineKeyboardButton(text=t("admin_btn_search_user", lang), callback_data="admin:user_search"),
        InlineKeyboardButton(text=t("btn_back", lang), callback_data="admin:dashboard"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_user_card_kb(
    target_user_id: int,
    return_page: int = 1,
    lang: str = "my",
    user_status: str = "ACTIVE",
    account_category: str = "FREE",
) -> InlineKeyboardMarkup:
    status_btn_text = t("admin_btn_unblock_user", lang) if user_status == "BLOCKED" else t("admin_btn_block_user", lang)
    status_action = "UNBLOCK" if user_status == "BLOCKED" else "BLOCK"
    tier_btn_text = "🔄 Switch to FREE" if account_category == "PURCHASED" else "⭐ Switch to PURCHASED"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=tier_btn_text, callback_data=f"admin:user_tier:{target_user_id}:{return_page}"),
            ],
            [
                InlineKeyboardButton(text="⚡ +10m Bonus", callback_data=f"admin:user_quick_credit:{target_user_id}:10:{return_page}"),
                InlineKeyboardButton(text="⚡ +30m Bonus", callback_data=f"admin:user_quick_credit:{target_user_id}:30:{return_page}"),
            ],
            [
                InlineKeyboardButton(text="➕ Custom Credit", callback_data=f"admin:adjust_start:{target_user_id}:CREDIT:{return_page}"),
                InlineKeyboardButton(text="➖ Debit Minutes", callback_data=f"admin:adjust_start:{target_user_id}:DEBIT:{return_page}"),
            ],
            [
                InlineKeyboardButton(text=t("admin_btn_view_ledger", lang), callback_data=f"admin:user_ledger:{target_user_id}:{return_page}"),
                InlineKeyboardButton(text=status_btn_text, callback_data=f"admin:user_status:{target_user_id}:{status_action}:{return_page}"),
            ],
            [
                InlineKeyboardButton(text="🔄 Refresh", callback_data=f"admin:user_detail:{target_user_id}:{return_page}"),
                InlineKeyboardButton(text="🔙 Back to Users", callback_data=f"admin:users_page:{return_page}"),
            ],
        ]
    )


def admin_user_ledger_kb(target_user_id: int, return_page: int = 1, lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Refresh", callback_data=f"admin:user_ledger:{target_user_id}:{return_page}")],
            [InlineKeyboardButton(text="🔙 Back to User Detail", callback_data=f"admin:user_detail:{target_user_id}:{return_page}")],
        ]
    )


def admin_pay_approve_confirm_kb(pay_id: str, lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("admin_btn_confirm_approve", lang), callback_data=f"admin:pay_approve_confirm:{pay_id}")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data=f"admin:pay_detail:{pay_id}")],
        ]
    )


def admin_pay_reject_confirm_kb(pay_id: str, lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("admin_btn_confirm_reject", lang), callback_data=f"admin:pay_reject_confirm:{pay_id}")],
            [InlineKeyboardButton(text="🔙 Cancel", callback_data=f"admin:pay_detail:{pay_id}")],
        ]
    )


def admin_tutorial_mgmt_kb(
    video_link: str | None = None,
    is_enabled: bool = True,
    lang: str = "my",
) -> InlineKeyboardMarkup:
    toggle_text = t("admin_btn_tutorial_disable", lang) if is_enabled else t("admin_btn_tutorial_enable", lang)
    buttons = [
        [
            InlineKeyboardButton(text=t("admin_btn_tutorial_edit", lang), callback_data="admin:tutorial_edit_url"),
            InlineKeyboardButton(text=toggle_text, callback_data="admin:tutorial_toggle"),
        ],
    ]
    if video_link:
        buttons.append([
            InlineKeyboardButton(text=t("admin_btn_test_tutorial", lang), url=video_link),
            InlineKeyboardButton(text=t("admin_btn_tutorial_remove", lang), callback_data="admin:tutorial_remove_prompt"),
        ])
    buttons.append([InlineKeyboardButton(text=t("btn_back", lang), callback_data="admin:dashboard")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_tutorial_confirm_remove_kb(lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗑️ Confirm Remove", callback_data="admin:tutorial_remove_confirm")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="admin:tutorial")],
        ]
    )


def admin_tutorial_cancel_kb(lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel", callback_data="admin:tutorial")]
        ]
    )


def admin_user_search_cancel_kb(lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("admin_btn_search_cancel", lang), callback_data="admin:user_search_cancel")]
        ]
    )


def admin_adjust_cancel_kb(lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel Adjustment", callback_data="admin:adjust_cancel")]
        ]
    )


def admin_adjust_preview_kb(token: str, lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Confirm & Apply", callback_data=f"admin:adjust_confirm:{token}")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="admin:adjust_cancel")],
        ]
    )


def admin_queue_kb(
    lang: str = "my",
    failed_count: int = 0,
    running: int = 0,
    queued: int = 0,
    success: int = 0,
) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text=t("admin_btn_queue_running", lang, count=running), callback_data="admin:queue_view:running"),
            InlineKeyboardButton(text=t("admin_btn_queue_queued", lang, count=queued), callback_data="admin:queue_view:queued"),
        ],
        [
            InlineKeyboardButton(text=t("admin_btn_queue_success", lang, count=success), callback_data="admin:queue_view:success"),
            InlineKeyboardButton(text=t("admin_btn_queue_failed", lang, count=failed_count), callback_data="admin:queue_failures"),
        ],
        [
            InlineKeyboardButton(text="🔄 " + ("Refresh Queue" if lang == "en" else "အသစ်ပြန်ဖွင့်ရန်"), callback_data="admin:queue"),
        ],
        [
            InlineKeyboardButton(text=t("btn_back", lang), callback_data="admin:dashboard"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_queue_list_kb(jobs: list | None = None, category: str = "running", lang: str = "my") -> InlineKeyboardMarkup:
    rows = []
    if jobs:
        for j in jobs:
            j_short = str(j.id)[:8]
            st = str(getattr(j, "status", "")).upper()
            st_icon = "⚙️" if st == "RUNNING" else ("⏳" if st == "QUEUED" else ("✅" if st == "SUCCESS" else "❌"))
            btn_label = f"{st_icon} #{j_short} · User {j.user_id}"
            rows.append([InlineKeyboardButton(text=btn_label, callback_data=f"admin:job_detail:{j.id}")])

    refresh_cb = f"admin:queue_view:{category}" if category in ("running", "queued", "success") else "admin:queue"
    rows.append([
        InlineKeyboardButton(text="🔄 " + ("Refresh" if lang == "en" else "အသစ်ဖွင့်ရန်"), callback_data=refresh_cb),
        InlineKeyboardButton(text=t("admin_btn_back_to_queue", lang), callback_data="admin:queue"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_queue_failures_kb(jobs: list | None = None, lang: str = "my") -> InlineKeyboardMarkup:
    buttons = []
    if jobs:
        for j in jobs:
            j_short = str(j.id)[:8]
            btn_label = f"❌ #{j_short} · User {j.user_id}"
            buttons.append([InlineKeyboardButton(text=btn_label, callback_data=f"admin:job_detail:{j.id}")])
    buttons.append([
        InlineKeyboardButton(text="🔄 " + ("Refresh" if lang == "en" else "အသစ်ဖွင့်ရန်"), callback_data="admin:queue_failures"),
        InlineKeyboardButton(text=t("admin_btn_back_to_queue", lang), callback_data="admin:queue"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_job_detail_kb(job_id: str, is_retryable: bool = False, lang: str = "my", status: str = "FAILED") -> InlineKeyboardMarkup:
    buttons = []
    if is_retryable:
        buttons.append([InlineKeyboardButton(text=t("admin_btn_retry_job", lang), callback_data=f"admin:job_retry_prompt:{job_id}")])
    buttons.append([
        InlineKeyboardButton(text="🔄 Refresh", callback_data=f"admin:job_detail:{job_id}"),
        InlineKeyboardButton(text=t("admin_btn_back_to_queue", lang), callback_data="admin:queue"),
    ])
    st_upper = str(status).upper()
    if st_upper == "FAILED":
        back_text = "🔙 " + ("Failed Jobs" if lang == "en" else "မအောင်မြင်သောစာရင်းသို့")
        buttons.append([InlineKeyboardButton(text=back_text, callback_data="admin:queue_failures")])
    elif st_upper in ("RUNNING", "QUEUED", "SUCCESS"):
        back_text = f"🔙 {st_upper.capitalize()} Jobs"
        buttons.append([InlineKeyboardButton(text=back_text, callback_data=f"admin:queue_view:{st_upper.lower()}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_job_retry_confirm_kb(job_id: str, lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("admin_btn_confirm_retry", lang), callback_data=f"admin:job_retry_confirm:{job_id}")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data=f"admin:job_detail:{job_id}")],
        ]
    )


def admin_settings_interactive_kb(lang: str = "my", maintenance_mode: bool = False) -> InlineKeyboardMarkup:
    if lang == "my":
        maint_text = "🔴 ပြုပြင်ဆဲ" if maintenance_mode else "🟢 ပုံမှန်ဖွင့်"
    else:
        maint_text = "🔴 Maintenance" if maintenance_mode else "🟢 Online"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("admin_btn_edit_allowance", lang), callback_data="admin:settings:allowance"),
                InlineKeyboardButton(text=t("admin_btn_edit_free_chars", lang), callback_data="admin:settings:free_chars"),
            ],
            [
                InlineKeyboardButton(text=t("admin_btn_edit_purchased_chars", lang), callback_data="admin:settings:purchased_chars"),
                InlineKeyboardButton(text=t("admin_btn_edit_daily_limit", lang), callback_data="admin:settings:daily_limit"),
            ],
            [
                InlineKeyboardButton(text=t("admin_btn_edit_gates", lang), callback_data="admin:settings:gates"),
                InlineKeyboardButton(text=f"🛠️ {maint_text}", callback_data="admin:settings:toggle_maintenance"),
            ],
            [
                InlineKeyboardButton(text=t("admin_btn_refresh", lang), callback_data="admin:settings"),
                InlineKeyboardButton(text=t("btn_back", lang), callback_data="admin:dashboard"),
            ],
        ]
    )


def admin_setting_presets_kb(
    setting_key: str,
    presets: list[tuple[str, str]],
    allow_custom: bool = True,
    lang: str = "my",
) -> InlineKeyboardMarkup:
    rows = []
    # 2 buttons per row for presets
    current_row = []
    for label, val in presets:
        current_row.append(InlineKeyboardButton(text=label, callback_data=f"admin:set:{setting_key}:{val}"))
        if len(current_row) == 2:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)

    if allow_custom:
        rows.append([InlineKeyboardButton(text=t("admin_btn_custom_input", lang), callback_data=f"admin:settings_input:{setting_key}")])

    rows.append([InlineKeyboardButton(text=t("btn_back", lang), callback_data="admin:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_settings_cancel_kb(lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_cancel", lang), callback_data="admin:settings")]
        ]
    )


def admin_pricing_plan_edit_kb(code: str, lang: str = "my") -> InlineKeyboardMarkup:
    presets = [
        [1500, 2000, 2500],
        [3000, 5000, 8000, 10000],
    ]
    rows = []
    for r in presets:
        rows.append([
            InlineKeyboardButton(
                text=f"{val:,} Ks",
                callback_data=f"admin:price_set:{code}:{val}",
            )
            for val in r
        ])
    rows.append([
        InlineKeyboardButton(text="✍️ Custom Price", callback_data=f"admin:price_custom:{code}"),
        InlineKeyboardButton(text="🔙 Back to Pricing", callback_data="admin:pricing"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_payment_accounts_edit_kb(lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📱 KBZPay Number", callback_data="admin:pay_field:payment_kpay_number"),
                InlineKeyboardButton(text="👤 KBZPay Name", callback_data="admin:pay_field:payment_kpay_name"),
            ],
            [
                InlineKeyboardButton(text="📱 WavePay Number", callback_data="admin:pay_field:payment_wave_number"),
                InlineKeyboardButton(text="👤 WavePay Name", callback_data="admin:pay_field:payment_wave_name"),
            ],
            [
                InlineKeyboardButton(text="🔙 Back to Pricing", callback_data="admin:pricing"),
            ],
        ]
    )


def admin_model_rename_cancel_kb(model_id: int, lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Cancel", callback_data=f"admin:model:{model_id}")]
        ]
    )


def user_history_kb(
    job_count: int = 0,
    ledger_count: int = 0,
    lang: str = "my",
) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("history_btn_jobs", lang, count=job_count), callback_data="history:jobs"),
                InlineKeyboardButton(text=t("history_btn_ledger", lang, count=ledger_count), callback_data="history:ledger"),
            ],
            [
                InlineKeyboardButton(text=t("history_btn_clear", lang), callback_data="history:clear_prompt"),
                InlineKeyboardButton(text="🔄 " + ("Refresh" if lang == "en" else "အသစ်ဖွင့်ရန်"), callback_data="menu:history"),
            ],
            [
                InlineKeyboardButton(text="🔙 " + ("Help & Settings" if lang == "en" else "အကူအညီ & ဆက်တင်"), callback_data="menu:help_settings"),
                InlineKeyboardButton(text=t("btn_main_menu", lang), callback_data="menu:home"),
            ],
        ]
    )


def user_history_sub_kb(
    current_view: str = "jobs",
    lang: str = "my",
) -> InlineKeyboardMarkup:
    other_btn = (
        InlineKeyboardButton(text="💳 " + ("Minute Ledger" if lang == "en" else "မိနစ်အဝင်/အထွက်"), callback_data="history:ledger")
        if current_view == "jobs"
        else InlineKeyboardButton(text="🎙 " + ("Voice Jobs" if lang == "en" else "အသံထုတ်လုပ်မှုများ"), callback_data="history:jobs")
    )
    refresh_cb = f"history:{current_view}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                other_btn,
                InlineKeyboardButton(text=t("history_btn_clear", lang), callback_data="history:clear_prompt"),
            ],
            [
                InlineKeyboardButton(text="🔄 " + ("Refresh" if lang == "en" else "အသစ်ဖွင့်ရန်"), callback_data=refresh_cb),
                InlineKeyboardButton(text="🔙 " + ("History Overview" if lang == "en" else "မှတ်တမ်းပင်မသို့"), callback_data="menu:history"),
            ],
            [
                InlineKeyboardButton(text=t("btn_main_menu", lang), callback_data="menu:home"),
            ],
        ]
    )


def user_history_clear_confirm_kb(lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("history_btn_clear_confirm", lang), callback_data="history:clear_confirm")],
            [InlineKeyboardButton(text=t("btn_cancel", lang), callback_data="menu:history")],
        ]
    )


def custom_voice_hub_kb(can_request: bool, lang: str = "my") -> InlineKeyboardMarkup:
    buttons = []
    if can_request:
        buttons.append([InlineKeyboardButton(text=t("custom_voice_btn_request_new", lang), callback_data="cvr:request_start")])
    buttons.append([
        InlineKeyboardButton(text="🔄 " + ("Refresh" if lang == "en" else "အသစ်ပြန်ဖွင့်ရန်"), callback_data="menu:custom_voice"),
        InlineKeyboardButton(text=t("btn_main_menu", lang), callback_data="menu:home"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def custom_voice_vip_promo_kb(lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("custom_voice_btn_upgrade", lang), callback_data="menu:buy_minutes")],
            [InlineKeyboardButton(text=t("btn_main_menu", lang), callback_data="menu:home")],
        ]
    )


def custom_voice_cancel_kb(lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_cancel", lang), callback_data="cvr:cancel")]
        ]
    )


def admin_cvr_list_kb(requests: list, status_filter: str = "PENDING", lang: str = "my") -> InlineKeyboardMarkup:
    buttons = []
    for req in requests:
        st_badge = "⏳" if req.status == "PENDING" else ("✅" if req.status == "APPROVED" else "❌")
        m_name = req.model_name[:14] if req.model_name else "Unnamed"
        btn_text = f"{st_badge} {req.id} · {m_name}"
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"admin:cvr:detail:{req.id}")])

    filter_btn = (
        InlineKeyboardButton(text="📋 All Requests", callback_data="admin:cvr:filter:ALL")
        if status_filter == "PENDING"
        else InlineKeyboardButton(text="⏳ Pending Only", callback_data="admin:cvr:filter:PENDING")
    )
    buttons.append([
        filter_btn,
        InlineKeyboardButton(text="🔄 Refresh", callback_data="admin:cvr"),
    ])
    buttons.append([
        InlineKeyboardButton(text=t("btn_back", lang), callback_data="admin:dashboard")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_cvr_detail_kb(req_id: str, status: str = "PENDING", lang: str = "my") -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🎧 နမူနာအသံနားဆင်မည်", callback_data=f"admin:cvr:play:{req_id}")],
    ]
    if status == "PENDING":
        buttons.append([
            InlineKeyboardButton(text=t("admin_btn_cvr_assign_existing", lang), callback_data=f"admin:cvr:assign_exist:{req_id}"),
            InlineKeyboardButton(text=t("admin_btn_cvr_scan_assign", lang), callback_data=f"admin:cvr:scan_assign:{req_id}"),
        ])
        buttons.append([
            InlineKeyboardButton(text=t("admin_btn_cvr_reject", lang), callback_data=f"admin:cvr:reject_prompt:{req_id}")
        ])
    buttons.append([
        InlineKeyboardButton(text=t("admin_btn_back_to_cvr", lang), callback_data="admin:cvr")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_cvr_select_model_kb(req_id: str, models: list, lang: str = "my") -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for m in models:
        row.append(InlineKeyboardButton(text=f"🎙️ {m.name[:16]}", callback_data=f"admin:cvr:do_assign:{req_id}:{m.id}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([
        InlineKeyboardButton(text=t("btn_cancel", lang), callback_data=f"admin:cvr:detail:{req_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_cvr_reject_presets_kb(req_id: str, lang: str = "my") -> InlineKeyboardMarkup:
    presets = [
        ("🔇 အသံဖိုင် ဆူညံ/မကြည်လင်ပါ", "0"),
        ("🎵 နောက်ခံတေးဂီတ ပါဝင်နေပါသည်", "1"),
        ("⏱️ ကြာချိန် တိုတောင်းလွန်းပါသည် (<30s)", "2"),
    ]
    buttons = [
        [InlineKeyboardButton(text=label, callback_data=f"admin:cvr:do_reject:{req_id}:{idx}")]
        for label, idx in presets
    ]
    buttons.append([
        InlineKeyboardButton(text="✍️ Custom Reason", callback_data=f"admin:cvr:reject_custom:{req_id}")
    ])
    buttons.append([
        InlineKeyboardButton(text=t("btn_cancel", lang), callback_data=f"admin:cvr:detail:{req_id}")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def voice_samples_catalog_kb(
    models: list[Any],
    lang: str = "my",
    current_user_id: int | None = None,
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for m in models:
        prefix = (
            "👑"
            if getattr(m, "type", None) == "PERSONAL"
            or getattr(m, "owner_user_id", None) == current_user_id
            else ("⭐" if getattr(m, "is_default_free", False) else "🎙️")
        )
        has_sample = bool(getattr(m, "sample_audio_path", None))
        status_icon = "▶️" if has_sample else "⏳"
        row.append(
            InlineKeyboardButton(
                text=f"{status_icon} {prefix} {m.name}",
                callback_data=f"sample:play:{m.id}",
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([
        InlineKeyboardButton(text=t("btn_menu_voice_clone", lang), callback_data="menu:voice_clone"),
        InlineKeyboardButton(text=t("btn_main_menu", lang), callback_data="menu:home"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_model_sample_cancel_kb(model_id: int, lang: str = "my") -> InlineKeyboardMarkup:
    cancel_text = t("btn_cancel", lang)
    if not cancel_text or cancel_text == "btn_cancel":
        cancel_text = "❌ ပယ်ဖျက်မည်" if lang == "my" else "❌ Cancel"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=cancel_text, callback_data=f"admin:model:{model_id}")]
        ]
    )





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
        [InlineKeyboardButton(text=t("btn_menu_buy_minutes", lang), callback_data="menu:buy_minutes")],
        [InlineKeyboardButton(text=t("btn_menu_refer_earn", lang), callback_data="menu:refer_earn")],
        [InlineKeyboardButton(text=t("btn_menu_tutorial", lang), callback_data="menu:tutorial")],
    ]
    if is_admin:
        buttons.append([InlineKeyboardButton(text=t("btn_admin_dashboard", lang), callback_data="admin:dashboard")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def back_to_menu_kb(lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("btn_main_menu", lang), callback_data="menu:home")]
        ]
    )


def admin_dashboard_kb(lang: str = "my", pending_payments: int = 0) -> InlineKeyboardMarkup:
    pay_badge = f" ({pending_payments})" if pending_payments > 0 else ""
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
                InlineKeyboardButton(text=t("admin_btn_bot_settings", lang), callback_data="admin:settings"),
                InlineKeyboardButton(text=t("admin_btn_tutorial_mgr", lang), callback_data="admin:tutorial"),
            ],
            [
                InlineKeyboardButton(text=t("admin_btn_broadcast", lang), callback_data="admin:broadcast"),
            ],
            [
                InlineKeyboardButton(text=t("admin_btn_refresh", lang), callback_data="admin:refresh"),
                InlineKeyboardButton(text=t("btn_main_menu", lang), callback_data="menu:home"),
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
) -> InlineKeyboardMarkup:
    status_btn_text = t("admin_btn_unblock_user", lang) if user_status == "BLOCKED" else t("admin_btn_block_user", lang)
    status_action = "UNBLOCK" if user_status == "BLOCKED" else "BLOCK"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Credit Minutes", callback_data=f"admin:adjust_start:{target_user_id}:CREDIT:{return_page}"),
                InlineKeyboardButton(text="➖ Debit Minutes", callback_data=f"admin:adjust_start:{target_user_id}:DEBIT:{return_page}"),
            ],
            [
                InlineKeyboardButton(text=status_btn_text, callback_data=f"admin:user_status:{target_user_id}:{status_action}:{return_page}"),
            ],
            [
                InlineKeyboardButton(text="🔄 Refresh", callback_data=f"admin:user_detail:{target_user_id}:{return_page}"),
                InlineKeyboardButton(text="🔙 Back to Users", callback_data=f"admin:users_page:{return_page}"),
            ],
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


def admin_queue_kb(lang: str = "my", failed_count: int = 0) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="🔄 Refresh Queue", callback_data="admin:queue")],
    ]
    if failed_count > 0:
        buttons.append([InlineKeyboardButton(text=t("admin_btn_queue_failures", lang), callback_data="admin:queue_failures")])
    buttons.append([InlineKeyboardButton(text=t("btn_back", lang), callback_data="admin:dashboard")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_queue_failures_kb(lang: str = "my") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Refresh", callback_data="admin:queue_failures")],
            [InlineKeyboardButton(text=t("admin_btn_back_to_queue", lang), callback_data="admin:queue")],
        ]
    )


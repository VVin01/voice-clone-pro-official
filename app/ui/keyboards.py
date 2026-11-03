from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from app.services.i18n import t


def language_selection_kb(
    active_lang: str | None = None,
    show_back: bool = False,
    lang: str = "my",
) -> InlineKeyboardMarkup:
    # The active language carries a ✅ marker (same convention as
    # help_settings_kb) so the current choice is visible at a glance;
    # callers that re-open this screen for an existing user pass
    # show_back=True so the screen is never a dead end (bot cleanup
    # 2026-11-03). The brand-new-user /start call passes neither: there
    # is no chosen language and nothing to go back to yet.
    my_text = "✅ 🇲🇲 မြန်မာစာ" if active_lang == "my" else "🇲🇲 မြန်မာစာ"
    en_text = "✅ 🇬🇧 English" if active_lang == "en" else "🇬🇧 English"
    rows = [
        [
            InlineKeyboardButton(text=my_text, callback_data="set_lang:my"),
            InlineKeyboardButton(text=en_text, callback_data="set_lang:en"),
        ]
    ]
    if show_back:
        rows.append([InlineKeyboardButton(text=t("btn_back", lang), callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def membership_gate_kb(
    channel_link: str,
    group_link: str,
    lang: str = "my",
    channel_ok: bool = False,
    group_ok: bool = False,
) -> InlineKeyboardMarkup:
    # Status-card keyboard: a Join button is offered only for a chat
    # the user is still missing (bot cleanup 2026-11-03); the verify
    # ("check again") button is always present. Callers that do not
    # pass the flags get the historical both-buttons behaviour.
    rows = []
    if not channel_ok:
        rows.append([InlineKeyboardButton(text=t("btn_join_channel", lang), url=channel_link)])
    if not group_ok:
        rows.append([InlineKeyboardButton(text=t("btn_join_group", lang), url=group_link)])
    rows.append([InlineKeyboardButton(text=t("btn_verify_membership", lang), callback_data="gate:verify")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _mini_app_base_url() -> str:
    import os

    return (
        os.getenv("MINI_APP_URL", "").strip()
        or os.getenv("RENDER_EXTERNAL_URL", "").strip()
        or "https://voice-clone-studio-c1er.onrender.com"
    )


def main_menu_kb(
    is_admin: bool = False,
    lang: str = "my",
    membership_pending: bool = False,
) -> InlineKeyboardMarkup:
    from aiogram.types import WebAppInfo

    web_app_url = _mini_app_base_url()

    # Bot cleanup 2026-11-03: the chat menu is a lean launcher — the
    # Mini App is the primary surface. Four core buttons (hero WebApp,
    # Plans, Refer, Language/Help) + the membership button ONLY while
    # the user is unverified + admin entries for admins. The separate
    # Recap/Downloader WebApp deep-links and the channel Tutorials row
    # retired: Mini App tabs and the Help screen cover them.
    buttons = [
        [InlineKeyboardButton(text=t("menu_launch_text", lang), web_app=WebAppInfo(url=web_app_url))],
        [
            InlineKeyboardButton(text=t("menu_buy_minutes_text", lang), callback_data="menu:buy_minutes"),
        ],
        [
            InlineKeyboardButton(text=t("btn_menu_refer_earn", lang), callback_data="menu:refer_earn"),
        ],
        [
            InlineKeyboardButton(text=t("menu_language_help_text", lang), callback_data="menu:help_settings"),
        ],
    ]
    if membership_pending:
        buttons.append([
            InlineKeyboardButton(text=t("menu_verify_gate", lang), callback_data="menu:membership"),
        ])
    if is_admin:
        buttons.append([InlineKeyboardButton(text=t("menu_admin_panel_text", lang), callback_data="admin:dashboard")])
        buttons.append([InlineKeyboardButton(text=t("menu_admin_broadcast", lang), callback_data="admin:broadcast")])

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


def admin_menu_kb(lang: str = "my") -> InlineKeyboardMarkup:
    # Bot cleanup 2026-11-03: the chat-side admin surface is a compact
    # landing — a read-only pulse (rendered as the message text), the
    # Mini App Admin Suite as the real work surface, Broadcast, Home.
    # The old 11-button dashboard (voice models / users / queue /
    # custom voice / settings / audit / pricing screens) retired: all
    # of it lives in the Mini App Admin tab now.
    from aiogram.types import WebAppInfo

    web_app_url = _mini_app_base_url()
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("admin_menu_open_suite", lang),
                    web_app=WebAppInfo(url=f"{web_app_url}?tab=tab-admin"),
                )
            ],
            [
                InlineKeyboardButton(text=t("admin_btn_broadcast", lang), callback_data="admin:broadcast"),
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
    # Bot cleanup 2026-11-03: audiences with 0 recipients are hidden —
    # offering a broadcast segment that reaches nobody was pure
    # mis-tap risk.
    rows = []
    if registered_count > 0:
        rows.append([
            InlineKeyboardButton(
                text=f"👥 Registered All ({registered_count})",
                callback_data="admin:bc_group:REGISTERED_ALL",
            )
        ])
    if imported_all_count > 0:
        rows.append([
            InlineKeyboardButton(
                text=f"📥 Imported All ({imported_all_count})",
                callback_data="admin:bc_group:IMPORTED_ALL",
            )
        ])
    if imported_unregistered_count > 0:
        rows.append([
            InlineKeyboardButton(
                text=f"⏳ Imported Unregistered ({imported_unregistered_count})",
                callback_data="admin:bc_group:IMPORTED_UNREGISTERED",
            )
        ])
    rows.append([
        InlineKeyboardButton(text="❌ Cancel", callback_data="admin:broadcast_cancel")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
            [InlineKeyboardButton(text=t("admin_btn_broadcast_test", lang), callback_data=f"admin:broadcast_test:{token}")],
            [InlineKeyboardButton(text=t("admin_btn_broadcast_cancel", lang), callback_data="admin:broadcast_cancel")],
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
    voice_enabled: bool = True,
    voice_link: str | None = None,
    recap_enabled: bool = True,
    recap_link: str | None = None,
) -> InlineKeyboardMarkup:
    v_status = "🟢 ON" if voice_enabled else "🔴 OFF"
    r_status = "🟢 ON" if recap_enabled else "🔴 OFF"

    buttons = [
        [
            InlineKeyboardButton(text=f"🎙️ Voice Tut: {v_status}", callback_data="admin:tutorial:voice_toggle"),
            InlineKeyboardButton(text="✏️ Voice Link", callback_data="admin:tutorial:voice_edit"),
        ],
        [
            InlineKeyboardButton(text=f"🎬 Recap Tut: {r_status}", callback_data="admin:tutorial:recap_toggle"),
            InlineKeyboardButton(text="✏️ Recap Link", callback_data="admin:tutorial:recap_edit"),
        ],
    ]
    test_btns = []
    if voice_link:
        test_btns.append(InlineKeyboardButton(text="🎙️ Test Voice", url=voice_link))
    if recap_link:
        test_btns.append(InlineKeyboardButton(text="🎬 Test Recap", url=recap_link))
    if test_btns:
        buttons.append(test_btns)

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
                InlineKeyboardButton(text=t("kb_refresh", lang), callback_data="menu:history"),
            ],
            [
                InlineKeyboardButton(text=t("kb_back_help", lang), callback_data="menu:help_settings"),
                InlineKeyboardButton(text=t("btn_main_menu", lang), callback_data="menu:home"),
            ],
        ]
    )


def user_history_sub_kb(
    current_view: str = "jobs",
    lang: str = "my",
) -> InlineKeyboardMarkup:
    other_btn = (
        InlineKeyboardButton(text=t("kb_ledger", lang), callback_data="history:ledger")
        if current_view == "jobs"
        else InlineKeyboardButton(text=t("kb_voice_jobs", lang), callback_data="history:jobs")
    )
    refresh_cb = f"history:{current_view}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                other_btn,
                InlineKeyboardButton(text=t("history_btn_clear", lang), callback_data="history:clear_prompt"),
            ],
            [
                InlineKeyboardButton(text=t("kb_refresh", lang), callback_data=refresh_cb),
                InlineKeyboardButton(text=t("kb_back_history", lang), callback_data="menu:history"),
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
        InlineKeyboardButton(text=t("kb_refresh_queue", lang), callback_data="menu:custom_voice"),
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
        # R40 item 15f: ⏳ read as "still loading" on a static catalogue
        # button. ▶️ = tap to play the sample; 🎙️ = voice with no
        # sample uploaded yet (no time/loading semantics).
        status_icon = "▶️" if has_sample else "🎙️"
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



from __future__ import annotations

from typing import Any
from urllib.parse import quote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from app.services.i18n import t


def language_selection_kb(
    active_lang: str | None = None,
    show_back: bool = False,
    lang: str = "my",
    callback_prefix: str = "set_lang",
) -> InlineKeyboardMarkup:
    my_text = "✅ 🇲🇲 မြန်မာစာ" if active_lang == "my" else "🇲🇲 မြန်မာစာ"
    en_text = "✅ 🇬🇧 English" if active_lang == "en" else "🇬🇧 English"
    rows = [
        [
            InlineKeyboardButton(text=my_text, callback_data=f"{callback_prefix}:my"),
            InlineKeyboardButton(text=en_text, callback_data=f"{callback_prefix}:en"),
        ]
    ]
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
    """Round 13 Part K: Welcome inline keyboard — ALWAYS exactly 3 buttons, one per row.

    No reply keyboard anywhere in DM. Every other user/admin function lives in the Mini App.
    """
    from aiogram.types import WebAppInfo

    web_app_url = _mini_app_base_url()
    buttons = [
        [InlineKeyboardButton(text=t("menu_launch_text", lang), web_app=WebAppInfo(url=web_app_url))],
        [InlineKeyboardButton(text=t("menu_refer_btn", lang), callback_data="menu:refer_earn")],
        [InlineKeyboardButton(text=t("menu_change_lang_btn", lang), callback_data="menu:change_lang")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def referral_share_kb(
    ref_link: str,
    invite_text: str,
    lang: str = "my",
) -> InlineKeyboardMarkup:
    """Round 13 Part K3b: Single inline Share button under the referral message."""
    share_url = (
        f"https://t.me/share/url?url={quote(ref_link, safe='')}"
        f"&text={quote(invite_text, safe='')}"
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("refer_share_btn", lang), url=share_url)]
        ]
    )


def help_settings_kb(
    video_link: str | None = None,
    is_video_enabled: bool = True,
    lang: str = "my",
    active_section: str | None = None,
) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(text=t("help_btn_voice", lang), callback_data="help_sec:voice"),
            InlineKeyboardButton(text=t("help_btn_recap", lang), callback_data="help_sec:recap"),
            InlineKeyboardButton(text=t("help_btn_downloader", lang), callback_data="help_sec:downloader"),
        ],
        [
            InlineKeyboardButton(text=t("help_btn_plans", lang), callback_data="help_sec:plans"),
            InlineKeyboardButton(text=t("help_btn_refer", lang), callback_data="help_sec:refer"),
            InlineKeyboardButton(text=t("help_btn_lang", lang), callback_data="help_sec:lang"),
        ],
    ]
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
    if active_section:
        buttons.append([
            InlineKeyboardButton(text=t("help_btn_back_guide", lang), callback_data="menu:help_settings"),
            InlineKeyboardButton(text=t("btn_main_menu", lang), callback_data="menu:home"),
        ])
    else:
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


def admin_pay_review_kb(pay_id: str) -> InlineKeyboardMarkup:
    """The three-action keyboard on every admin's Telegram copy of a
    payment review message (batch I): approve / edit-and-approve /
    reject. Burmese labels, matching the owner-specified pattern; the
    approve/reject callback data are the long-standing ones, so all
    existing prompt/confirm handlers keep working unchanged."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ အတည်ပြုမယ်", callback_data=f"admin:pay_approve:{pay_id}"),
                InlineKeyboardButton(text="🚫 ငြင်းမယ်", callback_data=f"admin:pay_reject:{pay_id}"),
            ],
            [
                InlineKeyboardButton(text="✏️ ပြင်ပြီးအတည်ပြုမယ်", callback_data=f"admin:pay_edit:{pay_id}"),
            ],
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



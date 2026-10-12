import html
import json
from pathlib import Path
from app.config import settings
from app.services.i18n import load_locales, t
from app.services.progress import render_stepper_progress


def test_i18n_myanmar_and_english():
    # Test Myanmar default
    my_btn = t("btn_menu_voice_clone", lang="my")
    assert "Voice Clone" in my_btn

    # Test English
    en_btn = t("btn_menu_voice_clone", lang="en")
    assert "Text → Voice → Clone" in en_btn

    # Test formatting with variables
    formatted_my = t("referral_desc", lang="my", referral_link="https://t.me/bot?start=ref_123", rewarded=5, max_referrals=20, earned_min=2.5, earned_sec=150, remaining_slots=15, remaining_min=7.5)
    assert "ref_123" in formatted_my
    assert "5/20" in formatted_my

    formatted_en = t("referral_desc", lang="en", referral_link="https://t.me/bot?start=ref_123", rewarded=5, max_referrals=20, earned_min=2.5, earned_sec=150, remaining_slots=15, remaining_min=7.5)
    assert "ref_123" in formatted_en
    assert "5/20" in formatted_en

    # Test new Start and Help keys
    assert "Start" in t("btn_menu_start", lang="en")
    assert "စတင်ရန်" in t("btn_menu_start", lang="my")
    assert "Voice Studio Help" in t("help_title", lang="en")
    assert "Voice Studio အသုံးပြုနည်း လမ်းညွှန်" in t("help_title", lang="my")
    assert "/help" in t("help_desc", lang="en")
    assert "/help" in t("help_desc", lang="my")

    # Fallback to key if unknown
    assert t("non_existent_key_xyz", lang="my") == "non_existent_key_xyz"


def test_locale_key_parity():
    """Verify that Burmese and English locale dictionaries contain identical sets of keys."""
    my_path = settings.locales_root / "my" / "messages.json"
    en_path = settings.locales_root / "en" / "messages.json"

    assert my_path.is_file(), "Burmese locale file must exist"
    assert en_path.is_file(), "English locale file must exist"

    with open(my_path, "r", encoding="utf-8") as f:
        my_keys = set(json.load(f).keys())

    with open(en_path, "r", encoding="utf-8") as f:
        en_keys = set(json.load(f).keys())

    missing_in_en = my_keys - en_keys
    missing_in_my = en_keys - my_keys

    assert not missing_in_en, f"Keys in Burmese missing from English: {missing_in_en}"
    assert not missing_in_my, f"Keys in English missing from Burmese: {missing_in_my}"
    assert my_keys == en_keys


def test_progress_keys_and_stepper_rendering():
    """Verify all progress keys render properly through render_stepper_progress."""
    # Burmese rendering
    burmese_out = render_stepper_progress(step=3, model_name="<TestVoice>", elapsed_seconds=65, spinner="⠹", lang="my")
    assert "Voice Studio — အသံဖန်တီးနေသည်" in burmese_out
    assert "&lt;TestVoice&gt;" in burmese_out
    assert "01:05" in burmese_out
    assert "✅ <b>[1/4] စစ်ဆေးခြင်းနှင့် Queue စာရင်း:</b> <i>ပြီးစီးပါပြီ</i>" in burmese_out
    assert "✅ <b>[2/4] မူရင်းအသံ:</b> <i>ပြီးစီးပါပြီ</i>" in burmese_out
    assert "🔄 <b>[3/4] AI အသံတုပြောင်းလဲခြင်း:</b> <b>ပြောင်းလဲနေသည် ⠹</b>" in burmese_out
    assert "⏳ <b>[4/4] စစ်ဆေးခြင်းနှင့် ပေးပို့ခြင်း:</b> <i>စောင့်ဆိုင်းနေသည်</i>" in burmese_out

    # English rendering
    english_out = render_stepper_progress(step=1, model_name="Standard", elapsed_seconds=5, spinner="⏳", lang="en")
    assert "Voice Studio — Processing" in english_out
    assert "Model: <b>Standard</b>" in english_out
    assert "00:05" in english_out
    assert "🔄 <b>[1/4] Validation & Queue:</b> <b>Validating</b>" in english_out
    assert "⏳ <b>[2/4] Base Audio:</b> <i>Waiting</i>" in english_out


def test_audio_completed_caption_localization():
    """Verify delivery audio caption renders properly with HTML escaping."""
    caption_my = t(
        "status_completed_caption",
        "my",
        completed_status=t("status_completed", "my"),
        model_name=html.escape("<Model & Sound>"),
        duration_seconds=42,
    )
    assert "အသံဖိုင် အောင်မြင်စွာ ဖန်တီးပြီးပါပြီ" in caption_my
    assert "&lt;Model &amp; Sound&gt;" in caption_my
    assert "42s" in caption_my

    caption_en = t(
        "status_completed_caption",
        "en",
        completed_status=t("status_completed", "en"),
        model_name=html.escape("Guy & Girl"),
        duration_seconds=15,
    )
    assert "Audio generated successfully" in caption_en
    assert "Guy &amp; Girl" in caption_en
    assert "15s" in caption_en


def test_admin_and_recovery_keys_localization():
    """Verify admin and recovery prompt keys format correctly."""
    # Admin text prompt
    adm_text_en = t("tts_admin_enter_text", "en", max_chars=5000)
    assert "Admin Unlimited Access" in adm_text_en
    assert "5,000 characters" in adm_text_en

    adm_text_my = t("tts_admin_enter_text", "my", max_chars=5000)
    assert "Admin အကန့်အသတ်မဲ့ အခွင့်အရေး" in adm_text_my
    assert "5,000 လုံး" in adm_text_my

    # Recovery fail notice
    rec_refund_en = t("recovery_fail_notice_refunded", "en")
    assert "refunded to your account balance" in rec_refund_en

    rec_refund_my = t("recovery_fail_notice_refunded", "my")
    assert "ပြန်လည်ထည့်သွင်းပေးပြီးပါပြီ။" in rec_refund_my


def test_admin_html_escaping_keys():
    """Verify admin_model_detail and payment_rejected_user_notify handle HTML escaping correctly."""
    # Test model detail with special characters in path
    unsafe_path = "C:\\Models\\Rock & Roll <v1>\\model.pth"
    safe_path = html.escape(unsafe_path)

    detail_en = t("admin_model_detail", "en", name="RockModel", status="ACTIVE", type="SYSTEM", free_default="Yes", pth_status="🟢 Ready", idx_status="⚪ Optional", pth_path=safe_path)
    assert "Rock &amp; Roll &lt;v1&gt;" in detail_en
    assert unsafe_path not in detail_en
    assert "<code>" in detail_en and "</code>" in detail_en
    assert "<b>Model: RockModel</b>" in detail_en

    detail_my = t("admin_model_detail", "my", name="RockModel", status="ACTIVE", type="SYSTEM", free_default="Yes", pth_status="🟢 Ready", idx_status="⚪ Optional", pth_path=safe_path)
    assert "Rock &amp; Roll &lt;v1&gt;" in detail_my
    assert unsafe_path not in detail_my
    assert "<code>" in detail_my and "</code>" in detail_my

    # Test payment rejection notice with special characters in reason
    unsafe_reason = "Unverified <slip> & fake ID"
    safe_reason = html.escape(unsafe_reason)

    reject_en = t("payment_rejected_user_notify", "en", pay_id="PAY-88", reason=safe_reason)
    assert "Unverified &lt;slip&gt; &amp; fake ID" in reject_en
    assert unsafe_reason not in reject_en
    assert "<b>Payment Request (#PAY-88) has been rejected.</b>" in reject_en

    reject_my = t("payment_rejected_user_notify", "my", pay_id="PAY-88", reason=safe_reason)
    assert "Unverified &lt;slip&gt; &amp; fake ID" in reject_my
    assert unsafe_reason not in reject_my
    assert "<b>ငွေပေးချေမှု တောင်းဆိုချက် (#PAY-88) ကို ပယ်ချလိုက်ပါသည်။</b>" in reject_my


def test_admin_dashboard_status_localization():
    """Verify admin_dashboard_status and new admin keys render with all placeholders in EN and MY."""
    # Test English
    dash_en = t(
        "admin_dashboard_status",
        "en",
        admin_title="👑 Admin Dashboard",
        admin_id=12345,
        total_users=10,
        free_users=8,
        purchased_users=2,
        total_jobs=15,
        queued_jobs=1,
        running_jobs=2,
        success_jobs=11,
        failed_jobs=1,
        purchased_minutes=50.5,
        consumed_minutes=12.3,
        pending_payments=3,
        active_models=2,
        recent_issues=t("admin_issues_failed", "en", count=1, error="Timeout"),
    )
    assert "Admin Dashboard" in dash_en
    assert "12345" in dash_en
    assert "Total: <b>10</b> (Free: 8 · Purchased: 2)" in dash_en
    assert "Total Jobs: <b>15</b>" in dash_en
    assert "Purchased: <b>50.5m</b>" in dash_en
    assert "Pending Payments: <b>3</b>" in dash_en
    assert "1 failed jobs (Latest: Timeout)" in dash_en

    # Test Burmese
    dash_my = t(
        "admin_dashboard_status",
        "my",
        admin_title="👑 Admin Dashboard",
        admin_id=12345,
        total_users=10,
        free_users=8,
        purchased_users=2,
        total_jobs=15,
        queued_jobs=1,
        running_jobs=2,
        success_jobs=11,
        failed_jobs=1,
        purchased_minutes=50.5,
        consumed_minutes=12.3,
        pending_payments=3,
        active_models=2,
        recent_issues=t("admin_issues_clean", "my"),
    )
    assert "ဆာဗာအခြေအနေ: 🟢 Online" in dash_my
    assert "12345" in dash_my
    assert "စုစုပေါင်း: <b>10</b>" in dash_my
    assert "အလုပ်စုစုပေါင်း: <b>15</b>" in dash_my
    assert "ပုံမှန်လည်ပတ်နေပါသည်" in dash_my


def test_admin_broadcast_keys_localization():
    """Verify all 10 broadcast keys render properly in English and Burmese with placeholders."""
    # Button texts
    assert "Broadcast" in t("admin_btn_broadcast", "en")
    assert "ကြေညာရန်" in t("admin_btn_broadcast", "my")
    assert "Confirm & Send" in t("admin_btn_broadcast_confirm", "en")
    assert "အတည်ပြုပြီး ပေးပို့မည်" in t("admin_btn_broadcast_confirm", "my")
    assert "Cancel Broadcast" in t("admin_btn_broadcast_cancel", "en")
    assert "မပို့တော့ပါ" in t("admin_btn_broadcast_cancel", "my")

    # Prompt
    prompt_en = t("admin_broadcast_prompt", "en")
    assert "Admin Broadcast Message" in prompt_en
    prompt_my = t("admin_broadcast_prompt", "my")
    assert "Admin သတင်းစကား ကြေညာခြင်း" in prompt_my

    # Preview
    preview_en = t("admin_broadcast_preview", "en", recipient_count=42, preview_text="Hello &lt;World&gt;")
    assert "42" in preview_en
    assert "Hello &lt;World&gt;" in preview_en
    assert "Target Recipients:" in preview_en

    preview_my = t("admin_broadcast_preview", "my", recipient_count=42, preview_text="Hello &lt;World&gt;")
    assert "42" in preview_my
    assert "Hello &lt;World&gt;" in preview_my
    assert "လက်ခံမည့်သူ စုစုပေါင်း:" in preview_my

    # Empty text warning
    assert "cannot be empty" in t("admin_broadcast_empty_text", "en")
    assert "အလွတ်မဖြစ်ရပါ" in t("admin_broadcast_empty_text", "my")

    # Alerts
    assert "cancelled" in t("admin_alert_broadcast_cancelled", "en")
    assert "ပယ်ဖျက်လိုက်ပါသည်" in t("admin_alert_broadcast_cancelled", "my")
    assert "expired" in t("admin_alert_broadcast_invalid_or_expired", "en")
    assert "သက်တမ်းကုန်ဆုံး" in t("admin_alert_broadcast_invalid_or_expired", "my")

    # In progress
    prog_en = t("admin_broadcast_in_progress", "en", sent=10, total=50, failed=2)
    assert "10/50" in prog_en
    assert "Failed: 2" in prog_en
    prog_my = t("admin_broadcast_in_progress", "my", sent=10, total=50, failed=2)
    assert "10/50" in prog_my
    assert "မအောင်မြင်: 2" in prog_my

    # Completed
    done_en = t("admin_broadcast_completed", "en", total=50, sent=45, failed=2, skipped=3, duration=1.5)
    assert "Total Targets:</b> <code>50</code>" in done_en
    assert "Successfully Sent:</b> <code>45</code>" in done_en
    assert "Failed:</b> <code>2</code>" in done_en
    assert "Skipped / Blocked:</b> <code>3</code>" in done_en
    assert "1.5s" in done_en

    done_my = t("admin_broadcast_completed", "my", total=50, sent=45, failed=2, skipped=3, duration=1.5)
    assert "စုစုပေါင်း ပစ်မှတ်:</b> <code>50</code>" in done_my
    assert "အောင်မြင်စွာ ပေးပို့ပြီး:</b> <code>45</code>" in done_my
    assert "မအောင်မြင်:</b> <code>2</code>" in done_my
    assert "ကျော်သွား / ပိတ်ပင်ထား:</b> <code>3</code>" in done_my
    assert "1.5s" in done_my



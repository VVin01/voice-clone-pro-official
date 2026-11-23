"""Part F — Bot job-completion & failure delivery messages + 1-per-row keyboards (2026-11-23)."""
from __future__ import annotations

from app.services import tts_service
from app.webapp import api as api_mod
from app import worker as worker_mod


def test_recap_completion_message_and_one_per_row_keyboard_my_and_en():
    msg_my = api_mod.build_recap_completion_message(
        "my",
        clean_title="Inception",
        voice_name="ကိုမင်း",
        duration_seconds=185,
        job_id="rc_12345",
        retention_hours=24,
    )
    assert "🎉 <b>Recap ပြီးပါပြီ!</b>" in msg_my
    assert "🎬 Inception" in msg_my
    assert "🎙️ အသံ: ကိုမင်း" in msg_my
    assert "⏱ 03:05 · MP4 (မြန်မာ အသံ + စာတန်းထိုး)" in msg_my
    assert "🆔 Task: <code>#rc_12345</code>" in msg_my
    assert "ဖိုင်များကို ၂၄ နာရီ သိမ်းထားပေးပါတယ် — အချိန်မီ Download ရယူထားပါ။" in msg_my
    assert "━━━━" not in msg_my

    msg_en = api_mod.build_recap_completion_message(
        "en",
        clean_title="Inception",
        voice_name="Ko Min",
        duration_seconds=185,
        job_id="rc_12345",
        retention_hours=24,
    )
    assert "🎉 <b>Your Recap is ready!</b>" in msg_en
    assert "🎬 Inception" in msg_en
    assert "🎙️ Voice: Ko Min" in msg_en
    assert "⏱ 03:05 · MP4 (Myanmar narration + subtitles)" in msg_en
    assert "🆔 Task: <code>#rc_12345</code>" in msg_en
    assert "Files are kept for 24 hours — please download in time." in msg_en

    # Full 5-file keyboard: strictly 1 button per full-width row in exact order
    kb = api_mod.build_recap_completion_keyboard(
        "my",
        job_id="rc_12345",
        app_url="https://app.example.com",
        has_video=True,
        has_audio=True,
        has_srt=True,
        has_script=True,
    )
    assert len(kb.inline_keyboard) == 5
    assert all(len(row) == 1 for row in kb.inline_keyboard)
    assert [row[0].text for row in kb.inline_keyboard] == [
        "🎬 Video ဖိုင် ပို့ပေးပါ",
        "🎧 Recap အသံ ပို့ပေးပါ",
        "📝 SRT ဖိုင် ပို့ပေးပါ",
        "📄 Script ဖိုင် ပို့ပေးပါ",
        "📱 Mini App တွင် ကြည့်မည်",
    ]
    assert [row[0].callback_data for row in kb.inline_keyboard[:4]] == [
        "send_recap_rc_12345",
        "send_recap_audio_rc_12345",
        "send_recap_srt_rc_12345",
        "send_recap_script_rc_12345",
    ]
    assert kb.inline_keyboard[4][0].web_app.url == "https://app.example.com?tab=tab-recap&recap_job_id=rc_12345"

    # Missing companion files are omitted while keeping existing files + Mini App button
    kb_partial = api_mod.build_recap_completion_keyboard(
        "en",
        job_id="rc_12345",
        app_url="https://app.example.com",
        has_video=True,
        has_audio=False,
        has_srt=False,
        has_script=True,
    )
    assert len(kb_partial.inline_keyboard) == 3
    assert all(len(row) == 1 for row in kb_partial.inline_keyboard)
    assert [row[0].callback_data for row in kb_partial.inline_keyboard[:2]] == [
        "send_recap_rc_12345",
        "send_recap_script_rc_12345",
    ]


def test_voice_clone_completion_and_failure_builders():
    msg_my = worker_mod.build_voice_completion_message(
        "my",
        model_name="ကိုမင်း",
        duration_seconds=42,
        job_id="vc_999",
        retention_hours=24,
    )
    assert "🎉 <b>အသံ ဖန်တီးပြီးပါပြီ!</b>" in msg_my
    assert "🎙️ အသံ: ကိုမင်း" in msg_my
    assert "⏱ 00:42" in msg_my
    assert "🆔 Task: <code>#vc_999</code>" in msg_my
    assert "ဖိုင်များကို ၂၄ နာရီ သိမ်းထားပေးပါတယ်" in msg_my
    assert "━━━━" not in msg_my

    cap_en = worker_mod.build_voice_direct_caption(
        "en",
        model_name="Ko Min",
        duration_seconds=42,
        job_id="vc_999",
        retention_hours=24,
    )
    assert "🎉 <b>Your audio is ready!</b>" in cap_en
    assert "🎙️ Voice: Ko Min" in cap_en
    assert "⏱ 00:42" in cap_en
    assert "🆔 Task: <code>#vc_999</code>" in cap_en

    kb = worker_mod.build_voice_completion_keyboard(
        "my", job_id="vc_999", app_url="https://app.example.com"
    )
    assert len(kb.inline_keyboard) == 2
    assert all(len(row) == 1 for row in kb.inline_keyboard)
    assert kb.inline_keyboard[0][0].callback_data == "send_audio_vc_999"
    assert kb.inline_keyboard[1][0].web_app.url == "https://app.example.com?tab=tab-voice"

    fail_my = worker_mod.build_voice_failure_message(
        "my", job_id="vc_999", reason="Timeout", refunded=True
    )
    assert "❌ <b>အသံ ဖန်တီးမှု မအောင်မြင်ပါ</b>" in fail_my
    assert "⚠️ အကြောင်းရင်း: Timeout" in fail_my
    assert "မိနစ်ကို ပြန်ဖြည့်ပေးပြီးပါပြီ" in fail_my


def test_tts_completion_caption_keyboard_and_profile_key_hint():
    cap_my = tts_service.build_tts_completion_caption(
        "my",
        engine_name="Gemini TTS",
        voice_name="ကိုသီဟ",
        duration_seconds=75,
        job_id="tts_456",
        retention_hours=24,
        fell_back=True,
        taste_blocked=True,
    )
    assert "🎉 <b>အသံ ဖန်တီးပြီးပါပြီ!</b>" in cap_my
    assert "🎙️ အသံ: Gemini TTS · ကိုသီဟ" in cap_my
    assert "⏱ 01:15" in cap_my
    assert "🆔 Task: <code>#tts_456</code>" in cap_my
    assert "ဖိုင်များကို ၂၄ နာရီ သိမ်းထားပေးပါတယ်" in cap_my
    assert "Profile" in cap_my
    assert "TTS Tab" not in cap_my

    cap_en = tts_service.build_tts_completion_caption(
        "en",
        engine_name="Edge TTS",
        voice_name="Ko Thiha",
        duration_seconds=30,
        job_id="tts_457",
        retention_hours=24,
        key_broken=True,
    )
    assert "🎉 <b>Your audio is ready!</b>" in cap_en
    assert "🎙️ Voice: Edge TTS · Ko Thiha" in cap_en
    assert "⏱ 00:30" in cap_en
    assert "in Profile" in cap_en
    assert "TTS tab" not in cap_en

    kb = tts_service.build_tts_completion_keyboard("en", app_url="https://app.example.com")
    assert len(kb.inline_keyboard) == 1
    assert len(kb.inline_keyboard[0]) == 1
    assert kb.inline_keyboard[0][0].web_app.url == "https://app.example.com?tab=tab-tts"


def test_recap_failure_dm_aligned_pattern_no_heavy_divider():
    dm_my = api_mod._build_recap_failure_dm_text(
        "my",
        title="Test Film",
        job_id="rc_fail_1",
        reason="Source blocked",
        credits_refunded=2,
    )
    assert "❌ <b>Recap ဖန်တီးမှု မအောင်မြင်ပါ</b>" in dm_my
    assert "🎬 Test Film" in dm_my
    assert "🆔 Task: <code>#rc_fail_1</code>" in dm_my
    assert "⚠️ အကြောင်းရင်း: Source blocked" in dm_my
    assert "2 Credit" in dm_my
    assert "Mini App တွင် ပြန်စမ်းကြည့်နိုင်ပါသည်" in dm_my
    assert "━━━━" not in dm_my

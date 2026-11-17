"""Unit tests for the Recap AI Thumbnail Generator (feat/recap-ai-thumbnail).

Covers:
1. Monthly cap hard-stop (no HF call after the cap is reached)
2. Tier gating (FREE blocked with upgrade note; PAID and Admin allowed)
3. HF insufficient-credit / 402 error -> graceful monthly disable with
   i18n message "ဒီလအတွက် AI Thumbnail အရေအတွက် ပြည့်သွားပါပြီ"
4. Zero user-balance deduction (recap_credits + minute/credit ledgers) on
   both success and failure, and no pool spend on non-billed failures
5. VoiceClonePro corner logo + Padauk Burmese headline compositing on
   1080x1920 (9:16) output, and empty headline = logo-only cover
6. Admin Settings persistence for monthly cap, model name, and prompt
   template ({title} placeholder + text-free base image guard)
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.db.base import Base
from app.models.enums import AccountCategory
from app.models.schema import (
    MinuteLedgerEvent,
    RecapCreditLedgerEvent,
    RecapThumbnailPoolUsage,
    User,
)
from app.services.dynamic_settings import load_dynamic_settings, set_dynamic_setting
from app.services.recap_thumbnail import (
    DEFAULT_THUMBNAIL_COST_PER_IMAGE_USD,
    DEFAULT_THUMBNAIL_MODEL,
    DEFAULT_THUMBNAIL_MONTHLY_CAP_USD,
    DEFAULT_THUMBNAIL_PROMPT_TEMPLATE,
    THUMBNAIL_HEIGHT,
    THUMBNAIL_MONTHLY_CAP_MESSAGE_MY,
    THUMBNAIL_WIDTH,
    ThumbnailCapReachedError,
    ThumbnailGenerationError,
    ThumbnailTierError,
    build_thumbnail_prompt,
    composite_recap_thumbnail,
    generate_recap_ai_thumbnail,
    get_monthly_thumbnail_usage,
    resolve_pro_hf_token,
)

ROOT = Path(__file__).resolve().parents[2]


def _run(coro):
    return asyncio.run(coro)


async def _make_session_factory(tmp_path: Path):
    db_file = tmp_path / "thumb_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _restore_thumbnail_settings(monkeypatch):
    monkeypatch.setattr(
        settings, "recap_thumbnail_monthly_cap_usd", DEFAULT_THUMBNAIL_MONTHLY_CAP_USD
    )
    monkeypatch.setattr(settings, "recap_thumbnail_model", DEFAULT_THUMBNAIL_MODEL)
    monkeypatch.setattr(
        settings, "recap_thumbnail_prompt_template", DEFAULT_THUMBNAIL_PROMPT_TEMPLATE
    )


def test_tier_gating_free_blocked_paid_and_admin_allowed(tmp_path, monkeypatch):
    _restore_thumbnail_settings(monkeypatch)
    monkeypatch.setattr(settings, "admin_ids_raw", "999001")

    async def _scenario():
        engine, Session = await _make_session_factory(tmp_path)
        try:
            async with Session() as session:
                free_user = User(
                    id=1001,
                    username="free_u",
                    account_category=AccountCategory.FREE.value,
                    recap_credits=3,
                )
                paid_user = User(
                    id=1002,
                    username="paid_u",
                    account_category=AccountCategory.PURCHASED.value,
                    recap_credits=5,
                )
                admin_free_cat = User(
                    id=999001,
                    username="admin_u",
                    account_category=AccountCategory.FREE.value,
                    recap_credits=2,
                )
                session.add_all([free_user, paid_user, admin_free_cat])
                await session.commit()

                hf_calls = []

                def fake_hf(prompt: str, *, model_name: str | None = None):
                    hf_calls.append((prompt, model_name))
                    return Image.new("RGB", (720, 1280), (40, 60, 90))

                # 1. FREE user must be blocked before any HF call
                with pytest.raises(ThumbnailTierError):
                    await generate_recap_ai_thumbnail(
                        session,
                        user=free_user,
                        job_id="rc_free_1",
                        recap_title="သရဲကား အကျဉ်းချုပ်",
                        headline_text="သရဲကား အကျဉ်းချုပ်",
                        output_path=tmp_path / "free_thumb.jpg",
                        hf_call=fake_hf,
                    )
                assert len(hf_calls) == 0

                # 2. PAID user succeeds
                res_paid = await generate_recap_ai_thumbnail(
                    session,
                    user=paid_user,
                    job_id="rc_paid_1",
                    recap_title="သရဲကား အကျဉ်းချုပ်",
                    headline_text="သရဲကား အကျဉ်းချုပ်",
                    output_path=tmp_path / "paid_thumb.jpg",
                    hf_call=fake_hf,
                )
                assert len(hf_calls) == 1
                assert Path(res_paid["output_path"]).is_file()

                # 3. Admin user (even with FREE account_category) succeeds
                res_admin = await generate_recap_ai_thumbnail(
                    session,
                    user=admin_free_cat,
                    job_id="rc_admin_1",
                    recap_title="အက်ရှင်ဇာတ်ကြမ်း",
                    headline_text="အက်ရှင်ဇာတ်ကြမ်း",
                    output_path=tmp_path / "admin_thumb.jpg",
                    hf_call=fake_hf,
                )
                assert len(hf_calls) == 2
                assert Path(res_admin["output_path"]).is_file()
        finally:
            await engine.dispose()

    _run(_scenario())


def test_monthly_cap_hard_stop_prevents_hf_call(tmp_path, monkeypatch):
    _restore_thumbnail_settings(monkeypatch)
    # Set cap to $0.006 -> exactly 2 images at $0.003 each; 3rd must hard-stop
    monkeypatch.setattr(settings, "recap_thumbnail_monthly_cap_usd", 0.006)

    async def _scenario():
        engine, Session = await _make_session_factory(tmp_path)
        try:
            async with Session() as session:
                paid_user = User(
                    id=2001,
                    username="paid_cap",
                    account_category=AccountCategory.PURCHASED.value,
                    recap_credits=10,
                )
                session.add(paid_user)
                await session.commit()

                hf_calls = 0

                def fake_hf(prompt: str, *, model_name: str | None = None):
                    nonlocal hf_calls
                    hf_calls += 1
                    return Image.new("RGB", (720, 1280), (30, 50, 70))

                # Generation 1 ($0.003 / $0.006)
                await generate_recap_ai_thumbnail(
                    session,
                    user=paid_user,
                    job_id="rc_cap_1",
                    recap_title="Movie 1",
                    headline_text="Movie 1",
                    output_path=tmp_path / "c1.jpg",
                    hf_call=fake_hf,
                )
                # Generation 2 ($0.006 / $0.006 -> hits cap)
                res2 = await generate_recap_ai_thumbnail(
                    session,
                    user=paid_user,
                    job_id="rc_cap_2",
                    recap_title="Movie 2",
                    headline_text="Movie 2",
                    output_path=tmp_path / "c2.jpg",
                    hf_call=fake_hf,
                )
                assert hf_calls == 2
                assert res2["pool_usage"]["generations_count"] == 2
                assert res2["pool_usage"]["estimated_spend_usd"] == pytest.approx(0.006)
                assert res2["pool_usage"]["cap_reached"] is True

                # Generation 3 -> MUST hard-stop BEFORE calling HF
                with pytest.raises(ThumbnailCapReachedError) as exc_info:
                    await generate_recap_ai_thumbnail(
                        session,
                        user=paid_user,
                        job_id="rc_cap_3",
                        recap_title="Movie 3",
                        headline_text="Movie 3",
                        output_path=tmp_path / "c3.jpg",
                        hf_call=fake_hf,
                    )
                assert str(exc_info.value) == THUMBNAIL_MONTHLY_CAP_MESSAGE_MY
                assert hf_calls == 2  # No 3rd HF call was made
        finally:
            await engine.dispose()

    _run(_scenario())


def test_hf_insufficient_credit_error_triggers_graceful_monthly_disable(tmp_path, monkeypatch):
    _restore_thumbnail_settings(monkeypatch)

    async def _scenario():
        engine, Session = await _make_session_factory(tmp_path)
        try:
            async with Session() as session:
                paid_user = User(
                    id=3001,
                    username="paid_402",
                    account_category=AccountCategory.PURCHASED.value,
                    recap_credits=7,
                )
                session.add(paid_user)
                await session.commit()

                hf_calls = 0

                def fake_hf_402(prompt: str, *, model_name: str | None = None):
                    nonlocal hf_calls
                    hf_calls += 1
                    raise RuntimeError(
                        "402 Client Error: Payment Required — You have exceeded your monthly included credits"
                    )

                with pytest.raises(ThumbnailCapReachedError) as exc_info:
                    await generate_recap_ai_thumbnail(
                        session,
                        user=paid_user,
                        job_id="rc_402_1",
                        recap_title="Credit Exhausted Movie",
                        headline_text="Credit Exhausted Movie",
                        output_path=tmp_path / "err402.jpg",
                        hf_call=fake_hf_402,
                    )
                assert str(exc_info.value) == THUMBNAIL_MONTHLY_CAP_MESSAGE_MY
                assert hf_calls == 1

                # Pool must now be marked exhausted with 0 billed spend
                usage = await get_monthly_thumbnail_usage(session)
                assert usage["exhausted"] is True
                assert usage["cap_reached"] is True
                assert usage["generations_count"] == 0
                assert usage["estimated_spend_usd"] == 0.0

                # Next call must hard-stop without even calling HF
                with pytest.raises(ThumbnailCapReachedError):
                    await generate_recap_ai_thumbnail(
                        session,
                        user=paid_user,
                        job_id="rc_402_2",
                        recap_title="Second Attempt",
                        headline_text="Second Attempt",
                        output_path=tmp_path / "err402_2.jpg",
                        hf_call=fake_hf_402,
                    )
                assert hf_calls == 1
        finally:
            await engine.dispose()

    _run(_scenario())


def test_zero_user_balance_deduction_on_success_and_failure(tmp_path, monkeypatch):
    _restore_thumbnail_settings(monkeypatch)

    async def _scenario():
        engine, Session = await _make_session_factory(tmp_path)
        try:
            async with Session() as session:
                paid_user = User(
                    id=4001,
                    username="paid_balance",
                    account_category=AccountCategory.PURCHASED.value,
                    recap_credits=9,
                )
                session.add(paid_user)
                await session.commit()

                # Seed 300 voice seconds in MinuteLedgerEvent
                session.add(
                    MinuteLedgerEvent(
                        user_id=paid_user.id,
                        source="PURCHASE",
                        direction="CREDIT",
                        seconds=300,
                    )
                )
                await session.commit()

                # 1. Successful generation -> zero balance change
                await generate_recap_ai_thumbnail(
                    session,
                    user=paid_user,
                    job_id="rc_bal_ok",
                    recap_title="Balance Test",
                    headline_text="လက်ကျန် စစ်ဆေးမှု",
                    output_path=tmp_path / "bal_ok.jpg",
                    hf_call=lambda prompt, *, model_name=None: Image.new(
                        "RGB", (720, 1280), (20, 40, 60)
                    ),
                )
                await session.refresh(paid_user)
                assert paid_user.recap_credits == 9

                # 2. Transient failure -> zero balance change AND no pool spend added
                def failing_hf(prompt: str, *, model_name: str | None = None):
                    raise ConnectionError("Gateway timeout")

                with pytest.raises(ThumbnailGenerationError):
                    await generate_recap_ai_thumbnail(
                        session,
                        user=paid_user,
                        job_id="rc_bal_fail",
                        recap_title="Balance Fail Test",
                        headline_text="လက်ကျန် စစ်ဆေးမှု",
                        output_path=tmp_path / "bal_fail.jpg",
                        hf_call=failing_hf,
                    )
                await session.refresh(paid_user)
                assert paid_user.recap_credits == 9

                min_rows = (
                    await session.execute(
                        select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == paid_user.id)
                    )
                ).scalars().all()
                recap_rows = (
                    await session.execute(
                        select(RecapCreditLedgerEvent).where(
                            RecapCreditLedgerEvent.user_id == paid_user.id
                        )
                    )
                ).scalars().all()
                assert len(min_rows) == 1 and min_rows[0].direction == "CREDIT"
                assert len(recap_rows) == 0

                # Pool usage counted only the 1 successful generation, not the failed one
                usage = await get_monthly_thumbnail_usage(session)
                assert usage["generations_count"] == 1
                assert usage["estimated_spend_usd"] == pytest.approx(
                    DEFAULT_THUMBNAIL_COST_PER_IMAGE_USD
                )
        finally:
            await engine.dispose()

    _run(_scenario())


def test_logo_and_burmese_headline_compositing_and_empty_logo_only(tmp_path):
    # Create a uniform mid-blue base image (720x1280)
    base = Image.new("RGB", (720, 1280), (45, 75, 120))

    # Case A: With Burmese headline -> 1080x1920, top-right has logo, lower third has Burmese headline
    out_with_text = tmp_path / "with_burmese.jpg"
    img_with_text = composite_recap_thumbnail(
        base,
        headline_text="မုန်တိုင်းထဲက သူရဲကောင်း — အပိုင်း (၁)",
        output_path=out_with_text,
    )
    assert img_with_text.size == (THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT)
    assert out_with_text.is_file()

    arr_text = np.asarray(img_with_text, dtype=np.int16)
    base_1080 = np.full((THUMBNAIL_HEIGHT, THUMBNAIL_WIDTH, 3), (45, 75, 120), dtype=np.int16)

    # Top-right corner region (where the VoiceClonePro logo sits) must differ from base
    top_right_diff = np.abs(arr_text[20:200, 880:1060] - base_1080[20:200, 880:1060]).mean()
    assert top_right_diff > 15.0

    # Lower-third region (where the shaped Burmese headline + scrim sit) must differ from base
    lower_third_diff = np.abs(arr_text[1350:1850, 100:980] - base_1080[1350:1850, 100:980]).mean()
    assert lower_third_diff > 15.0

    # Case B: Empty headline ("") -> logo-only cover (top-right has logo, lower third untouched!)
    out_logo_only = tmp_path / "logo_only.jpg"
    img_logo_only = composite_recap_thumbnail(
        base,
        headline_text="   ",
        output_path=out_logo_only,
    )
    assert img_logo_only.size == (THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT)
    arr_logo_only = np.asarray(img_logo_only, dtype=np.int16)

    top_right_logo_diff = np.abs(
        arr_logo_only[20:200, 880:1060] - base_1080[20:200, 880:1060]
    ).mean()
    assert top_right_logo_diff > 15.0

    lower_third_logo_only_diff = np.abs(
        arr_logo_only[1350:1850, 100:980] - base_1080[1350:1850, 100:980]
    ).mean()
    assert lower_third_logo_only_diff < 2.0  # Untouched base color (JPEG compression tolerance < 2)


def test_admin_settings_model_prompt_cap_and_reserve_token(tmp_path, monkeypatch):
    _restore_thumbnail_settings(monkeypatch)
    monkeypatch.setenv("HF_RESERVE_TOKENS", "hf_pro_reserve_test_token_123")
    assert resolve_pro_hf_token() == "hf_pro_reserve_test_token_123"

    async def _scenario():
        engine, Session = await _make_session_factory(tmp_path)
        try:
            async with Session() as session:
                await set_dynamic_setting(
                    session, "recap_thumbnail_monthly_cap_usd", 1.50, admin_id=999
                )
                await set_dynamic_setting(
                    session,
                    "recap_thumbnail_model",
                    "black-forest-labs/FLUX.1-dev",
                    admin_id=999,
                )
                await set_dynamic_setting(
                    session,
                    "recap_thumbnail_prompt_template",
                    "Dark thriller vertical poster for {title}, neon rain",
                    admin_id=999,
                )
                await session.commit()

                # Reset in-memory settings and reload from DB to prove stored values win
                _restore_thumbnail_settings(monkeypatch)
                await load_dynamic_settings(session)

                usage = await get_monthly_thumbnail_usage(session)
                assert usage["monthly_cap_usd"] == pytest.approx(1.50)
                assert usage["model"] == "black-forest-labs/FLUX.1-dev"
                assert "Dark thriller vertical poster for {title}" in usage["prompt_template"]

                built = build_thumbnail_prompt("John Wick", genre="action")
                assert "Dark thriller vertical poster for John Wick (action)" in built
                # Text-free guard is always enforced even if admin template omitted it
                assert "no text" in built.lower()
                assert "no letters" in built.lower()
        finally:
            await engine.dispose()

    _run(_scenario())


def test_frontend_and_cache_token_wiring():
    index_html = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
    i18n_js = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")

    assert index_html.count("?v=20261117_r161") == 4
    assert 'id="recap-ai-thumbnail-box"' in index_html
    assert 'id="recap-thumb-headline-input"' in index_html
    assert 'id="btn-recap-ai-thumbnail"' in index_html
    assert 'id="adm-ai-thumbnail-card"' in index_html
    assert 'id="adm-set-thumb-cap"' in index_html
    assert 'id="adm-set-thumb-model"' in index_html
    assert 'id="adm-set-thumb-prompt"' in index_html

    assert "generateRecapAiThumbnail" in app_js
    assert "saveAiThumbnailSettings" in app_js
    assert THUMBNAIL_MONTHLY_CAP_MESSAGE_MY in i18n_js


def test_send_recap_thumbnail_telegram_parsemode_html(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.webapp.api import _send_recap_thumbnail_telegram

    monkeypatch.setattr(settings, "bot_token", "test_bot_token_123")
    fake_photo = tmp_path / "thumb.jpg"
    fake_photo.write_bytes(b"dummy_jpeg_bytes")

    sent_kwargs = {}
    mock_bot = MagicMock()
    mock_bot.send_photo = AsyncMock(side_effect=lambda **kw: sent_kwargs.update(kw))
    mock_bot.session = MagicMock()
    mock_bot.session.close = AsyncMock()

    with patch("aiogram.Bot", return_value=mock_bot):
        ok = _run(
            _send_recap_thumbnail_telegram(
                chat_id=12345,
                photo_path=fake_photo,
                title="Test Movie",
                lang="en",
            )
        )
        assert ok is True
        assert sent_kwargs.get("parse_mode") == "HTML"
        assert "<b>AI Thumbnail Ready</b>" in sent_kwargs.get("caption", "")


def test_sanitize_title_for_brain_strips_channels_and_tags():
    from app.services.recap_thumbnail import sanitize_title_for_brain

    assert sanitize_title_for_brain("Mere Paas Tum Ho - Episode 1 | ARY Digital [Eng Sub]") == "Mere Paas Tum Ho"
    assert sanitize_title_for_brain("ARY Digital - Besharam Official Full Episode 12") == "Besharam"
    assert sanitize_title_for_brain("Money Heist (Official Trailer) - Netflix") == "Money Heist"
    assert sanitize_title_for_brain("https://youtube.com/watch?v=12345 Movie Recap Video") == "dramatic cinematic story"
    assert sanitize_title_for_brain("Inception") == "Inception"


def test_validate_brain_response_headline_rules():
    from app.services.recap_thumbnail import BRAIN_REQUIRED_SUFFIX, validate_brain_response

    # 1. Approved 2-line dramatic Burmese headline
    approved = {
        "headline_my": "မိန်းမရဲ့ လျှို့ဝှက်ချက်\nပေါ်သွားပြီ",
        "image_prompt_en": "Cinematic vertical 9:16 poster of a shocked woman uncovering a hidden dark secret",
    }
    validated = validate_brain_response(approved, source_title="Besharam")
    assert validated is not None
    assert validated["headline_my"] == "မိန်းမရဲ့ လျှို့ဝှက်ချက်\nပေါ်သွားပြီ"
    assert BRAIN_REQUIRED_SUFFIX in validated["image_prompt_en"]

    # 2. Rejection: contains digits (ASCII or Myanmar)
    bad_ascii_digit = {
        "headline_my": "မိန်းမရဲ့ လျှို့ဝှက်ချက် 1\nပေါ်သွားပြီ",
        "image_prompt_en": "Cinematic vertical 9:16 poster",
    }
    assert validate_brain_response(bad_ascii_digit) is None

    bad_mm_digit = {
        "headline_my": "မိန်းမရဲ့ လျှို့ဝှက်ချက် ၁\nပေါ်သွားပြီ",
        "image_prompt_en": "Cinematic vertical 9:16 poster",
    }
    assert validate_brain_response(bad_mm_digit) is None

    # 3. Rejection: abstract noun slash pairs
    bad_slash_pair = {
        "headline_my": "ကင်ဆာလျှို့ဝှက် / ဖောက်ပြန်မှု",
        "image_prompt_en": "Cinematic vertical 9:16 poster",
    }
    assert validate_brain_response(bad_slash_pair) is None

    # 4. Rejection: channel names in headline
    bad_channel = {
        "headline_my": "ARY Digital ဇာတ်လမ်း\nပေါ်သွားပြီ",
        "image_prompt_en": "Cinematic vertical 9:16 poster",
    }
    assert validate_brain_response(bad_channel) is None

    # 5. Rejection: single line or 3 lines
    bad_single_line = {
        "headline_my": "တစ်ခုတည်းသောခေါင်းစဉ်အရှည်ကြီး",
        "image_prompt_en": "Cinematic vertical 9:16 poster",
    }
    assert validate_brain_response(bad_single_line) is None

    bad_three_lines = {
        "headline_my": "လိုင်း ၁\nလိုင်း ၂\nလိုင်း ၃",
        "image_prompt_en": "Cinematic vertical 9:16 poster",
    }
    assert validate_brain_response(bad_three_lines) is None

    # 6. Rejection: source title copied directly
    bad_title_copied = {
        "headline_my": "Besharam\nBesharam",
        "image_prompt_en": "Cinematic vertical 9:16 poster",
    }
    assert validate_brain_response(bad_title_copied, source_title="Besharam") is None


def test_call_brain_for_thumbnail_fallback_on_error_or_bad_json():
    from app.services.recap_thumbnail import call_brain_for_thumbnail

    # 1. Invalid JSON string
    def fake_bad_json(prompt: str):
        return "Not a valid JSON response from Gemini"

    res_bad = call_brain_for_thumbnail("ဇာတ်လမ်းအကျဉ်း", "Besharam", gemini_call=fake_bad_json)
    assert res_bad is None

    # 2. Exception raised during Gemini call
    def fake_error(prompt: str):
        raise ConnectionResetError("Gemini network timeout")

    res_err = call_brain_for_thumbnail("ဇာတ်လမ်းအကျဉ်း", "Besharam", gemini_call=fake_error)
    assert res_err is None

    # 3. Valid JSON string parsed correctly
    def fake_good(prompt: str):
        return '{"headline_my": "မိန်းမရဲ့ လျှို့ဝှက်ချက်\\nပေါ်သွားပြီ", "image_prompt_en": "Cinematic vertical 9:16 poster of a shocked woman"}'

    res_good = call_brain_for_thumbnail("ဇာတ်လမ်းအကျဉ်း", "Besharam", gemini_call=fake_good)
    assert res_good is not None
    assert res_good["headline_my"] == "မိန်းမရဲ့ လျှို့ဝှက်ချက်\nပေါ်သွားပြီ"


def test_generate_thumbnail_brain_headline_and_user_override(tmp_path, monkeypatch):
    _restore_thumbnail_settings(monkeypatch)

    async def _scenario():
        engine, Session = await _make_session_factory(tmp_path)
        try:
            async with Session() as session:
                paid_user = User(
                    id=5001,
                    username="paid_v2",
                    account_category=AccountCategory.PURCHASED.value,
                    recap_credits=10,
                )
                session.add(paid_user)
                await session.commit()

                def fake_brain(prompt: str):
                    return {
                        "headline_my": "မိန်းမရဲ့ လျှို့ဝှက်ချက်\nပေါ်သွားပြီ",
                        "image_prompt_en": "Dramatic scene of a shocked woman in vertical 9:16 poster",
                    }

                hf_prompts = []

                def fake_hf(prompt: str, *, model_name: str | None = None):
                    hf_prompts.append(prompt)
                    return Image.new("RGB", (720, 1280), (180, 50, 30))  # Warm base

                # Case 1: Empty user headline -> uses brain headline + brain image prompt
                res1 = await generate_recap_ai_thumbnail(
                    session,
                    user=paid_user,
                    job_id="rc_brain_1",
                    recap_title="Besharam - Episode 1 | ARY Digital",
                    headline_text="",
                    output_path=tmp_path / "brain1.jpg",
                    hf_call=fake_hf,
                    gemini_call=fake_brain,
                )
                assert res1["headline_text"] == "မိန်းမရဲ့ လျှို့ဝှက်ချက်\nပေါ်သွားပြီ"
                assert "Dramatic scene of a shocked woman" in res1["prompt"]
                assert res1["palette_family"] == "warm"

                # Case 2: User provides explicit headline -> overrides brain headline
                res2 = await generate_recap_ai_thumbnail(
                    session,
                    user=paid_user,
                    job_id="rc_brain_2",
                    recap_title="Besharam - Episode 1 | ARY Digital",
                    headline_text="အသုံးပြုသူ စိတ်ကြိုက်\nခေါင်းစဉ်",
                    output_path=tmp_path / "brain2.jpg",
                    hf_call=fake_hf,
                    gemini_call=fake_brain,
                )
                assert res2["headline_text"] == "အသုံးပြုသူ စိတ်ကြိုက်\nခေါင်းစဉ်"

                # Case 3: Brain call fails -> falls back to v1 prompt template and logo-only / user headline
                def failing_brain(prompt: str):
                    raise RuntimeError("Brain 503 unavailable")

                res3 = await generate_recap_ai_thumbnail(
                    session,
                    user=paid_user,
                    job_id="rc_brain_3",
                    recap_title="Fallback Movie",
                    headline_text="",
                    output_path=tmp_path / "brain3.jpg",
                    hf_call=fake_hf,
                    gemini_call=failing_brain,
                )
                assert res3["headline_text"] == ""
                assert res3["logo_only"] is True
                assert "Cinematic vertical 9:16 movie poster artwork inspired by Fallback Movie" in res3["prompt"]
        finally:
            await engine.dispose()

    _run(_scenario())


def test_palette_analysis_on_synthetic_swatches_and_contrast_rule():
    from app.services.recap_thumbnail import analyze_palette

    # 1. Warm swatch (red/orange)
    warm_img = Image.new("RGB", (720, 1280), (180, 50, 20))
    p_warm = analyze_palette(warm_img)
    assert p_warm["family"] == "warm"
    assert p_warm["deepen_contrast"] is False

    # 2. Cold swatch (blue/cyan)
    cold_img = Image.new("RGB", (720, 1280), (20, 60, 180))
    p_cold = analyze_palette(cold_img)
    assert p_cold["family"] == "cold"
    assert p_cold["deepen_contrast"] is False

    # 3. Green swatch
    green_img = Image.new("RGB", (720, 1280), (20, 150, 50))
    p_green = analyze_palette(green_img)
    assert p_green["family"] == "green"
    assert p_green["deepen_contrast"] is False

    # 4. Dark / neutral swatch
    dark_img = Image.new("RGB", (720, 1280), (15, 15, 15))
    p_dark = analyze_palette(dark_img)
    assert p_dark["family"] == "dark_neutral"
    assert p_dark["deepen_contrast"] is False

    # 5. High luminance swatch (> 150 mean luminance) -> deepen_contrast must be True
    bright_img = Image.new("RGB", (720, 1280), (230, 230, 230))
    p_bright = analyze_palette(bright_img)
    assert p_bright["mean_luminance"] > 150.0
    assert p_bright["deepen_contrast"] is True


def test_composite_noto_serif_black_compact_shaping_and_autofit(tmp_path):
    from app.services.recap_thumbnail import (
        _fit_line_font,
        _resolve_noto_serif_black_path,
        composite_recap_thumbnail,
    )

    # 1. Font file is NotoSerifMyanmar-Black
    font_path = _resolve_noto_serif_black_path()
    assert font_path.is_file()
    assert "NotoSerifMyanmar-Black" in font_path.name

    # 2. Auto-fit test: punch line renders larger font size than setup line
    line_setup = "မိန်းမရဲ့ လျှို့ဝှက်ချက်"
    line_punch = "ပေါ်သွားပြီ"
    _f1, s1 = _fit_line_font(line_setup)
    _f2, s2 = _fit_line_font(line_punch)
    assert s2 >= s1  # Shorter punch line renders at >= font size of setup line

    # 3. Syllable test: render words containing ု/ူ syllables ("သူ", "ပြု")
    base = Image.new("RGB", (720, 1280), (30, 40, 60))
    out_img = composite_recap_thumbnail(
        base,
        headline_text="သူရဲကောင်း၏\nပြုပြင်မှု",
        output_path=tmp_path / "shaping_test.jpg",
    )
    assert out_img.size == (1080, 1920)
    arr = np.asarray(out_img, dtype=np.int16)
    # The headline area (lower third, 1400:1850) must have rendered text pixels differing from base
    base_1080 = np.full((1920, 1080, 3), (30, 40, 60), dtype=np.int16)
    diff = np.abs(arr[1400:1850, 100:980] - base_1080[1400:1850, 100:980]).mean()
    assert diff > 15.0




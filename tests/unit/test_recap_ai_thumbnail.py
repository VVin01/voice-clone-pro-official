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

    assert index_html.count("?v=20261116_r159") == 4
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



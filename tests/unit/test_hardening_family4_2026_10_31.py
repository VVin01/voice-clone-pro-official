"""Hardening batch — Family 4 (honesty & enforcement).

V1  custom-voice truth: assign refuses models the production engine
    cannot run; the queue's legacy remap is logged, not silent.
C1  maintenance mode is enforced: Mini App writes via
    require_gate_verified (admins exempt), /config reports it, and
    the bot buy/refer entry handlers alert instead of opening.
C2  bot Tutorial Manager writes go through set_dynamic_setting.
C5  set_dynamic_setting enforces the dynamic-key allowlist (secrets
    are not writable through it).
B2s set_dynamic_setting persists BEFORE applying to memory, so a
    failed write cannot diverge memory from DB.
X2  showStudioToast/showStudioConfirm render messages via textContent.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message
from aiogram.types import User as TgUser
from fastapi import HTTPException
from sqlalchemy import select

import app.handlers.admin as admin_mod
import app.handlers.menu as menu_mod
import app.handlers.payment as payment_mod
import app.webapp.api as api_mod
from app.config import settings
from app.models.enums import AccountCategory
from app.models.schema import BotSetting, User, VoiceModel
from app.services.custom_voice import assign_model_to_request, create_custom_voice_request
from app.services.dynamic_settings import set_dynamic_setting
from app.services.ledger import get_or_create_user


def _patch_sessions(monkeypatch, module, db_session):
    @asynccontextmanager
    async def mock_session_ctx():
        yield db_session

    monkeypatch.setattr(module, "AsyncSessionLocal", lambda: mock_session_ctx())


async def _purchased(db_session, uid):
    user, _ = await get_or_create_user(db_session, telegram_id=uid)
    user.account_category = AccountCategory.PURCHASED.value
    await db_session.commit()
    return user


async def _model(db_session, mid, pth_path):
    m = VoiceModel(
        id=mid, name=f"Model {mid}", slug=f"model-{mid}",
        model_dir="cloud" if pth_path == "voxcpm" else "storage/models/x",
        pth_path=pth_path, type="SYSTEM", status="ACTIVE",
    )
    db_session.add(m)
    await db_session.commit()
    return m


# ── V1 ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_v1_assign_refuses_non_runnable_model(db_session):
    user = await _purchased(db_session, 7901)
    ok_r, req, _ = await create_custom_voice_request(
        session=db_session, user_id=user.id, model_name="Legacy",
        sample_file_id="s1",
    )
    assert ok_r and req
    legacy = await _model(db_session, 7951, "storage/models/legacy.pth")

    ok, req2, err = await assign_model_to_request(
        session=db_session, request_id=req.id, model_id=legacy.id, admin_id=1
    )
    assert ok is False
    assert err.startswith("MODEL_NOT_RUNNABLE")
    await db_session.refresh(req)
    assert req.status == "PENDING"  # untouched — no silently-dead approval


@pytest.mark.asyncio
async def test_v1_assign_accepts_runnable_model(db_session):
    user = await _purchased(db_session, 7902)
    ok_r, req, _ = await create_custom_voice_request(
        session=db_session, user_id=user.id, model_name="Modern",
        sample_file_id="s2",
    )
    assert ok_r and req
    modern = await _model(db_session, 7952, "voxcpm")
    ok, req2, err = await assign_model_to_request(
        session=db_session, request_id=req.id, model_id=modern.id, admin_id=1
    )
    assert ok is True, err
    assert req2.status == "APPROVED"


@pytest.mark.asyncio
async def test_v1_queue_remap_is_logged_not_silent(db_session, caplog):
    from app.models.schema import UserVoiceModelGrant
    from app.services.queue import create_voice_clone_job

    user = await _purchased(db_session, 7903)
    legacy = await _model(db_session, 7953, "storage/models/legacy.pth")
    cloud = await _model(db_session, 7954, "voxcpm")
    cloud.slug = "voxcpm-cloud-ai"
    db_session.add(UserVoiceModelGrant(user_id=user.id, model_id=legacy.id, granted_by=1))
    await db_session.commit()

    with caplog.at_level(logging.WARNING, logger="app.services.queue"):
        job, reason = await create_voice_clone_job(
            db_session, user_id=user.id, text="မင်္ဂလာပါ", model_id=legacy.id
        )
    assert job is not None
    assert job.model_id == cloud.id  # behaviour unchanged: fell back
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("not runnable" in r.getMessage() and str(user.id) in r.getMessage() for r in warnings)


# ── C1: maintenance enforcement ───────────────────────────────────────

@pytest.mark.asyncio
async def test_c1_gate_dependency_blocks_writes_during_maintenance(db_session, monkeypatch):
    user, _ = await get_or_create_user(db_session, telegram_id=7910)
    admin = User(id=999333, username="adm", first_name="Admin", language_code="my")
    db_session.add(admin)
    await db_session.commit()

    monkeypatch.setattr(settings, "maintenance_mode", True)
    monkeypatch.setattr(settings, "admin_ids_raw", "999333")
    with pytest.raises(HTTPException) as exc:
        await api_mod.require_gate_verified(user=user, db=db_session)
    assert exc.value.status_code == 403
    assert exc.value.detail.startswith("MAINTENANCE_MODE")
    # Admins are exempt.
    assert await api_mod.require_gate_verified(user=admin, db=db_session) is admin


@pytest.mark.asyncio
async def test_c1_maintenance_checked_before_gate(db_session, monkeypatch):
    # With maintenance OFF the very same call proceeds to the gate
    # check (which fails closed in the test env) — proving order.
    user, _ = await get_or_create_user(db_session, telegram_id=7911)
    await db_session.commit()
    monkeypatch.setattr(settings, "maintenance_mode", False)
    with pytest.raises(HTTPException) as exc:
        await api_mod.require_gate_verified(user=user, db=db_session)
    assert not exc.value.detail.startswith("MAINTENANCE_MODE")


@pytest.mark.asyncio
async def test_c1_public_config_reports_maintenance(db_session, monkeypatch):
    monkeypatch.setattr(settings, "maintenance_mode", True)
    cfg = await api_mod.get_public_app_config(db_session)
    assert cfg["maintenance_mode"] is True
    monkeypatch.setattr(settings, "maintenance_mode", False)
    cfg = await api_mod.get_public_app_config(db_session)
    assert cfg["maintenance_mode"] is False


@pytest.mark.asyncio
async def test_c1_bot_buy_menu_blocked_during_maintenance(db_session, monkeypatch):
    from app.services.payments import get_active_pricing_plans

    await get_active_pricing_plans(db_session)
    user, _ = await get_or_create_user(db_session, telegram_id=7912)
    await db_session.commit()
    _patch_sessions(monkeypatch, payment_mod, db_session)
    monkeypatch.setattr(settings, "maintenance_mode", True)

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=7912, is_bot=False, first_name="Buyer")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    state = SimpleNamespace(clear=AsyncMock())
    await payment_mod.handle_buy_minutes_menu(cb, state=state)

    cb.answer.assert_awaited_once()
    assert cb.answer.call_args[1].get("show_alert") is True
    cb.message.edit_text.assert_not_called()


@pytest.mark.asyncio
async def test_c1_bot_refer_blocked_during_maintenance(db_session, monkeypatch):
    user, _ = await get_or_create_user(db_session, telegram_id=7913)
    await db_session.commit()
    _patch_sessions(monkeypatch, menu_mod, db_session)
    monkeypatch.setattr(settings, "maintenance_mode", True)

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=7913, is_bot=False, first_name="Fan")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    cb.bot = SimpleNamespace(get_me=AsyncMock())
    await menu_mod.handle_menu_refer_earn(cb)

    cb.answer.assert_awaited_once()
    assert cb.answer.call_args[1].get("show_alert") is True
    cb.bot.get_me.assert_not_called()  # stopped before building the link


# ── C2/C5 + B2-settings ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_c5_allowlist_rejects_secrets_and_unknown_keys(db_session):
    for bad in ("bot_token", "gemini_api_key", "totally_made_up", "admin_ids_raw"):
        with pytest.raises(ValueError):
            await set_dynamic_setting(db_session, bad, "x", admin_id=1)


@pytest.mark.asyncio
async def test_c5_allowlisted_key_round_trips(db_session):
    original = settings.tutorial_voice_link
    try:
        await set_dynamic_setting(db_session, "tutorial_voice_link", "https://t.me/VoiceClonePro/29", admin_id=1)
        await db_session.commit()
        row = (await db_session.execute(
            select(BotSetting).where(BotSetting.key == "tutorial_voice_link")
        )).scalar_one()
        assert row.value == "https://t.me/VoiceClonePro/29"
        assert settings.tutorial_voice_link == "https://t.me/VoiceClonePro/29"
    finally:
        settings.tutorial_voice_link = original


@pytest.mark.asyncio
async def test_b2s_failed_persistence_leaves_memory_untouched(db_session):
    """A session whose flush fails must NOT leave the new value in the
    in-memory settings (the old code applied memory first)."""
    original = settings.tutorial_recap_link

    class _FailingSession:
        async def execute(self, stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

        def add(self, obj):
            pass

        async def flush(self):
            raise RuntimeError("DB write failed")

    try:
        with pytest.raises(RuntimeError):
            await set_dynamic_setting(_FailingSession(), "tutorial_recap_link", "https://t.me/evil/1", admin_id=1)
        assert settings.tutorial_recap_link == original
    finally:
        settings.tutorial_recap_link = original


@pytest.mark.asyncio
async def test_c2_tutorial_toggle_writes_through_service(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", "888111")
    _patch_sessions(monkeypatch, admin_mod, db_session)
    original = settings.tutorial_voice_enabled
    try:
        cb = MagicMock(spec=CallbackQuery)
        cb.from_user = TgUser(id=888111, is_bot=False, first_name="BossAdmin")
        cb.message = MagicMock(spec=Message)
        cb.message.edit_text = AsyncMock()
        cb.answer = AsyncMock()

        await admin_mod.handle_admin_tutorial_voice_toggle(cb)

        row = (await db_session.execute(
            select(BotSetting).where(BotSetting.key == "tutorial_voice_enabled")
        )).scalar_one_or_none()
        assert row is not None and row.value == "false"  # default True → toggled off
        # …and the in-memory value moved WITH the write (the C2 point).
        assert settings.tutorial_voice_enabled is False
    finally:
        settings.tutorial_voice_enabled = original


# ── X2 ────────────────────────────────────────────────────────────────

def test_x2_toast_and_confirm_use_textcontent():
    src = open(api_mod.__file__.replace("api.py", "static/js/app.js"), encoding="utf-8").read()
    assert src.count("textContent = message") == 2
    toast_block = src[src.index("function showStudioToast"):src.index("function showStudioConfirm")]
    assert "${message}" not in toast_block
    confirm_block = src[src.index("function showStudioConfirm"):src.index("function mapErrorToToast")]
    assert "${message}" not in confirm_block

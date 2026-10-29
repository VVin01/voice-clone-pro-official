from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock
import pytest
from sqlalchemy import select
from aiogram.types import CallbackQuery, Message, User as TgUser

from app.config import settings
from app.handlers.clone import CloneFSM, handle_clone_text_input, handle_voice_clone_start
from app.models.enums import AccountCategory, JobStatus, LedgerDirection, LedgerSource
from app.models.schema import AuditLog, Job, MinuteLedgerEvent, VoiceModel
from app.services.gate import verify_user_membership
from app.services.ledger import (
    award_welcome_bonus,
    consume_seconds,
    get_minute_balance,
    get_or_create_user,
    grant_monthly_free_allowance,
    refund_job_seconds,
)
from app.services.queue import create_voice_clone_job
from app.worker import process_single_job


ADMIN_TELEGRAM_ID = 999999999
NORMAL_TELEGRAM_ID = 100000001
ATTACKER_TELEGRAM_ID = 100000002


@pytest.fixture(autouse=True)
def mock_admin_ids(monkeypatch):
    """Ensure ADMIN_TELEGRAM_ID is strictly in settings.admin_ids for testing."""
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_TELEGRAM_ID))


@pytest.mark.asyncio
async def test_admin_welcome_bonus_exemption(db_session):
    """Admins must not receive or require the 300-second welcome bonus."""
    admin_user, _ = await get_or_create_user(db_session, telegram_id=ADMIN_TELEGRAM_ID)
    assert settings.is_admin(admin_user.id) is True

    # 1. Direct call to award_welcome_bonus
    awarded, bal = await award_welcome_bonus(db_session, admin_user.id)
    assert awarded is False
    assert bal.welcome_seconds == 0
    assert bal.total_usable_seconds == 0
    assert admin_user.welcome_bonus_awarded is False

    # Verify no ledger event was created
    events = (
        await db_session.execute(
            select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == admin_user.id)
        )
    ).scalars().all()
    assert len(events) == 0

    # 2. Gate verification flow (bot=None bypasses external network check)
    passed, reason = await verify_user_membership(db_session, admin_user, bot=None)
    assert passed is True
    assert admin_user.channel_verified is True
    assert admin_user.group_verified is True
    # Admin bonus must still be NOT awarded
    assert admin_user.welcome_bonus_awarded is False

    # Verify balance is still 0 and no bonus awarded
    bal_after_gate = await get_minute_balance(db_session, admin_user.id)
    assert bal_after_gate.welcome_seconds == 0
    assert bal_after_gate.total_usable_seconds == 0


@pytest.mark.asyncio
async def test_admin_zero_balance_job_creation(db_session, tmp_path):
    """Admins can create voice clone jobs with 0 minute balance."""
    admin_user, _ = await get_or_create_user(db_session, telegram_id=ADMIN_TELEGRAM_ID)
    bal = await get_minute_balance(db_session, admin_user.id)
    assert bal.total_usable_seconds == 0

    # Create dummy active model
    pth_file = tmp_path / "ModelAdmin.pth"
    pth_file.write_bytes(b"pth_content")
    model = VoiceModel(
        name="AdminModel",
        slug="system-adminmodel",
        type="SYSTEM",
        model_dir=str(tmp_path),
        pth_path=str(pth_file),
        status="ACTIVE",
        is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()

    # Admin should succeed creating a job with 0 balance
    job, status_str = await create_voice_clone_job(
        session=db_session,
        user_id=admin_user.id,
        text="Admin testing voice clone without balance",
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
    )
    assert status_str == "QUEUED"
    assert job.status == JobStatus.QUEUED.value
    assert job.user_id == admin_user.id


@pytest.mark.asyncio
async def test_non_admin_zero_balance_blocked(db_session, tmp_path):
    """Normal users cannot create voice clone jobs with 0 minute balance."""
    user, _ = await get_or_create_user(db_session, telegram_id=NORMAL_TELEGRAM_ID)
    await grant_monthly_free_allowance(db_session, user.id)
    bal = await get_minute_balance(db_session, user.id)
    bal.monthly_free_seconds = 0
    await db_session.flush()
    assert bal.total_usable_seconds == 0

    # Create dummy active model
    pth_file = tmp_path / "ModelNormal.pth"
    pth_file.write_bytes(b"pth_content")
    model = VoiceModel(
        name="NormalModel",
        slug="system-normalmodel",
        type="SYSTEM",
        model_dir=str(tmp_path),
        pth_path=str(pth_file),
        status="ACTIVE",
        is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()

    job, res = await create_voice_clone_job(
        session=db_session,
        user_id=user.id,
        text="Normal user attempting without balance",
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
    )
    assert job is None
    assert res == "INSUFFICIENT_BALANCE"


@pytest.mark.asyncio
async def test_admin_voice_clone_start_handler(db_session):
    """Admin entering Voice Studio with 0 balance is granted access and notified of exempt status."""
    admin_user, _ = await get_or_create_user(db_session, telegram_id=ADMIN_TELEGRAM_ID)
    admin_user.language_code = "en"
    await db_session.flush()
    bal = await get_minute_balance(db_session, admin_user.id)
    assert bal.total_usable_seconds == 0

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=ADMIN_TELEGRAM_ID, is_bot=False, first_name="BossAdmin")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    state = AsyncMock()

    @asynccontextmanager
    async def mock_session():
        yield db_session

    with patch("app.handlers.clone.AsyncSessionLocal", side_effect=mock_session):
        await handle_voice_clone_start(cb, state)

    # Admin should set state to waiting for text
    state.set_state.assert_called_once_with(CloneFSM.waiting_for_text)
    # Edit text should have been called welcoming to enter text with admin unlimited message
    cb.message.edit_text.assert_called_once()
    call_args = cb.message.edit_text.call_args[0][0]
    assert "Admin Unlimited Access" in call_args
    assert f"{settings.infrastructure_safety_character_limit:,}" in call_args
    cb.answer.assert_called_once()


@pytest.mark.asyncio
async def test_non_admin_voice_clone_start_handler_blocked(db_session):
    """Non-admin entering Voice Studio with 0 balance is blocked and prompted with alert."""
    user, _ = await get_or_create_user(db_session, telegram_id=NORMAL_TELEGRAM_ID)
    bal = await get_minute_balance(db_session, user.id)
    bal.monthly_free_seconds = 0
    await db_session.flush()
    assert bal.total_usable_seconds == 0

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=NORMAL_TELEGRAM_ID, is_bot=False, first_name="NormalUser")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    state = AsyncMock()

    @asynccontextmanager
    async def mock_session():
        yield db_session

    with patch("app.handlers.clone.AsyncSessionLocal", side_effect=mock_session):
        await handle_voice_clone_start(cb, state)

    # Normal user should NOT set state
    state.set_state.assert_not_called()
    cb.message.edit_text.assert_not_called()
    cb.answer.assert_called_once()
    assert cb.answer.call_args[1].get("show_alert") is True


@pytest.mark.asyncio
async def test_admin_submits_text_longer_than_purchased_limit_allowed(db_session, tmp_path, monkeypatch):
    """Admin submitting text longer than PURCHASED limit is ALLOWED.

    The current defaults set character_limit_purchased == the 5000-char
    infrastructure safety limit, so no real text can sit between them.
    The behaviour under test is the ADMIN EXEMPTION (admins are checked
    only against the infrastructure limit), so this test pins the
    purchased limit to 2000 for its own scenario via monkeypatch.
    """
    monkeypatch.setattr(settings, "character_limit_purchased", 2000)
    admin_user, _ = await get_or_create_user(db_session, telegram_id=ADMIN_TELEGRAM_ID)
    long_text = "မြန်မာစာ" * 320  # 320 * 8 = 2560 characters, well above the 2000-char purchased limit
    assert len(long_text) > settings.character_limit_purchased
    assert len(long_text) < settings.infrastructure_safety_character_limit

    # 1. Handler validation
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=ADMIN_TELEGRAM_ID, is_bot=False, first_name="BossAdmin")
    msg.text = long_text
    msg.answer = AsyncMock()
    state = AsyncMock()
    state.get_data.return_value = {"lang": "my", "max_chars": settings.infrastructure_safety_character_limit}

    await handle_clone_text_input(msg, state)

    # Admin successfully passed text validation and advanced to voice selection
    state.update_data.assert_called_once_with(input_text=long_text)
    state.set_state.assert_called_once_with(CloneFSM.waiting_for_voice)
    msg.answer.assert_called_once()
    assert "tts_text_too_long" not in msg.answer.call_args[0][0]

    # 2. Queue service validation
    pth_file = tmp_path / "AdminLongModel.pth"
    pth_file.write_bytes(b"pth_content")
    model = VoiceModel(
        name="AdminLongModel",
        slug="system-adminlongmodel",
        type="SYSTEM",
        model_dir=str(tmp_path),
        pth_path=str(pth_file),
        status="ACTIVE",
        is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()

    job, status_str = await create_voice_clone_job(
        session=db_session,
        user_id=admin_user.id,
        text=long_text,
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
    )
    assert status_str == "QUEUED"
    assert job is not None
    assert job.input_text == long_text


@pytest.mark.asyncio
async def test_ordinary_purchased_user_blocked_above_purchased_limit(db_session, tmp_path):
    """Ordinary PURCHASED user submitting text longer than 5000 chars is BLOCKED."""
    user, _ = await get_or_create_user(db_session, telegram_id=NORMAL_TELEGRAM_ID)
    user.account_category = AccountCategory.PURCHASED.value
    await db_session.flush()

    long_text = "A" * 6000  # 6000 characters > current 5000-char purchased limit
    assert len(long_text) > settings.character_limit_purchased

    # 1. Handler validation
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=NORMAL_TELEGRAM_ID, is_bot=False, first_name="NormalPurchasedUser")
    msg.text = long_text
    msg.answer = AsyncMock()
    state = AsyncMock()
    state.get_data.return_value = {"lang": "en", "max_chars": settings.character_limit_purchased}

    await handle_clone_text_input(msg, state)

    # State should NOT advance to voice
    state.set_state.assert_not_called()
    msg.answer.assert_called_once()
    assert "Text is too long" in msg.answer.call_args[0][0]

    # 2. Queue service validation
    pth_file = tmp_path / "PurchasedModel.pth"
    pth_file.write_bytes(b"pth_content")
    model = VoiceModel(
        name="PurchasedModel",
        slug="system-purchasedmodel",
        type="SYSTEM",
        model_dir=str(tmp_path),
        pth_path=str(pth_file),
        status="ACTIVE",
        is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()

    job, reason = await create_voice_clone_job(
        session=db_session,
        user_id=user.id,
        text=long_text,
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
    )
    assert job is None
    assert reason == "TEXT_EXCEEDS_CHARACTER_LIMIT"


@pytest.mark.asyncio
async def test_ordinary_free_user_blocked_above_free_limit(db_session, tmp_path):
    """Ordinary FREE user submitting text longer than 1000 chars is BLOCKED."""
    user, _ = await get_or_create_user(db_session, telegram_id=NORMAL_TELEGRAM_ID + 5)
    user.account_category = AccountCategory.FREE.value
    await db_session.flush()

    medium_text = "B" * 1200  # 1200 characters > current 1000-char free limit
    assert len(medium_text) > settings.character_limit_free

    # 1. Handler validation
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=user.id, is_bot=False, first_name="NormalFreeUser")
    msg.text = medium_text
    msg.answer = AsyncMock()
    state = AsyncMock()
    state.get_data.return_value = {"lang": "en", "max_chars": settings.character_limit_free}

    await handle_clone_text_input(msg, state)

    state.set_state.assert_not_called()
    msg.answer.assert_called_once()
    assert "Text is too long" in msg.answer.call_args[0][0]

    # 2. Queue service validation
    pth_file = tmp_path / "FreeModel.pth"
    pth_file.write_bytes(b"pth_content")
    model = VoiceModel(
        name="FreeModel",
        slug="system-freemodel",
        type="SYSTEM",
        model_dir=str(tmp_path),
        pth_path=str(pth_file),
        status="ACTIVE",
        is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()

    job, reason = await create_voice_clone_job(
        session=db_session,
        user_id=user.id,
        text=medium_text,
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
    )
    assert job is None
    assert reason == "TEXT_EXCEEDS_CHARACTER_LIMIT"


@pytest.mark.asyncio
async def test_infrastructure_safety_limit_guard_blocks_excessive_payload(db_session, tmp_path):
    """Payloads exceeding infrastructure safety limit (5000 chars) are blocked even for admins."""
    admin_user, _ = await get_or_create_user(db_session, telegram_id=ADMIN_TELEGRAM_ID)
    huge_text = "X" * (settings.infrastructure_safety_character_limit + 100)

    # 1. Handler check
    msg = MagicMock(spec=Message)
    msg.from_user = TgUser(id=ADMIN_TELEGRAM_ID, is_bot=False, first_name="BossAdmin")
    msg.text = huge_text
    msg.answer = AsyncMock()
    state = AsyncMock()
    state.get_data.return_value = {"lang": "en", "max_chars": settings.infrastructure_safety_character_limit}

    await handle_clone_text_input(msg, state)

    state.set_state.assert_not_called()
    msg.answer.assert_called_once()
    assert "infrastructure safety limit" in msg.answer.call_args[0][0]

    # 2. Queue service check
    pth_file = tmp_path / "SafetyModel.pth"
    pth_file.write_bytes(b"pth_content")
    model = VoiceModel(
        name="SafetyModel",
        slug="system-safetymodel",
        type="SYSTEM",
        model_dir=str(tmp_path),
        pth_path=str(pth_file),
        status="ACTIVE",
        is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()

    job, reason = await create_voice_clone_job(
        session=db_session,
        user_id=admin_user.id,
        text=huge_text,
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
    )
    assert job is None
    assert reason == "TEXT_EXCEEDS_INFRASTRUCTURE_LIMIT"


@pytest.mark.asyncio
async def test_admin_worker_execution_no_ledger_debit_with_audit_log(db_session, tmp_path):
    """Admin voice job completes without debiting buckets and logs to AuditLog."""
    admin_user, _ = await get_or_create_user(db_session, telegram_id=ADMIN_TELEGRAM_ID)
    bal = await get_minute_balance(db_session, admin_user.id)
    assert bal.total_usable_seconds == 0

    pth_file = tmp_path / "ModelWorkerAdmin.pth"
    pth_file.write_bytes(b"pth_content")
    model = VoiceModel(
        name="AdminWorkerModel",
        slug="system-adminworkermodel",
        type="SYSTEM",
        model_dir=str(tmp_path),
        pth_path=str(pth_file),
        status="ACTIVE",
        is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()

    job, _ = await create_voice_clone_job(
        session=db_session,
        user_id=admin_user.id,
        text="Admin voice generation execution test",
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
    )

    async def mock_tts(text, out_path, voice, speed):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake_mp3_audio")
        return out_path

    async def mock_norm(in_audio, out_wav):
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        out_wav.write_bytes(b"fake_norm_wav")
        return out_wav

    def mock_voxcpm(text, output_wav, **kwargs):  # patched via side_effect: MagicMock calls it WITHOUT self (worker passes all-keyword args)
        output_wav = Path(output_wav)
        output_wav.parent.mkdir(parents=True, exist_ok=True)
        output_wav.write_bytes(b"fake_voxcpm_wav")
        return output_wav

    with (
        patch("app.worker.generate_edge_tts", side_effect=mock_tts),
        patch("app.worker.normalize_audio_to_wav", side_effect=mock_norm),
        patch("app.services.voxcpm_engine.VoxCPMEngine.synthesize", side_effect=mock_voxcpm),
        patch("app.worker.get_wav_duration_seconds", return_value=25),
    ):
        success = await process_single_job(session=db_session, job=job)
        assert success is True

    # Job record updated with duration consumed
    assert job.status == JobStatus.SUCCESS.value
    assert job.seconds_consumed == 25
    assert job.finished_at is not None

    # Balance remains exactly 0
    bal_after = await get_minute_balance(db_session, admin_user.id)
    assert bal_after.total_usable_seconds == 0
    assert bal_after.welcome_seconds == 0
    assert bal_after.purchased_seconds == 0

    # NO ledger events written
    events = (
        await db_session.execute(
            select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == admin_user.id)
        )
    ).scalars().all()
    assert len(events) == 0

    # AuditLog entry written
    audit_logs = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.admin_id == admin_user.id)
        )
    ).scalars().all()
    assert len(audit_logs) == 1
    audit = audit_logs[0]
    assert audit.action == "admin.voice_clone"
    assert audit.target_type == "job"
    assert audit.target_id == str(job.id)
    assert "25" in audit.details_json
    assert "AdminWorkerModel" in audit.details_json


@pytest.mark.asyncio
async def test_admin_worker_failure_no_refund_event(db_session, tmp_path):
    """Admin failed voice job marks job FAILED without writing misleading refund ledger events."""
    admin_user, _ = await get_or_create_user(db_session, telegram_id=ADMIN_TELEGRAM_ID)

    pth_file = tmp_path / "ModelFail.pth"
    pth_file.write_bytes(b"pth_content")
    model = VoiceModel(
        name="FailModel",
        slug="system-failmodel",
        type="SYSTEM",
        model_dir=str(tmp_path),
        pth_path=str(pth_file),
        status="ACTIVE",
        is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()

    job, _ = await create_voice_clone_job(
        session=db_session,
        user_id=admin_user.id,
        text="Admin voice generation failure test",
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
    )

    # A VoxCPM job never calls generate_edge_tts (that was the legacy
    # pipeline). Fail BOTH the VoxCPM2 engine and the curated-persona
    # fallback so the job genuinely fails end-to-end.
    def mock_voxcpm_fail(text, output_wav, **kwargs):
        raise RuntimeError("TTS engine crashed")

    def mock_persona_fail(text, persona_id, output_wav, **kwargs):
        raise RuntimeError("TTS engine crashed")

    with (
        patch("app.services.voxcpm_engine.VoxCPMEngine.synthesize", side_effect=mock_voxcpm_fail),
        patch("app.services.curated_voices.synthesize_curated_persona", side_effect=mock_persona_fail),
    ):
        success = await process_single_job(session=db_session, job=job)
        assert success is False

    assert job.status == JobStatus.FAILED.value
    assert "TTS engine crashed" in job.error_message

    # Ensure no ledger events whatsoever
    events = (
        await db_session.execute(
            select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == admin_user.id)
        )
    ).scalars().all()
    assert len(events) == 0


@pytest.mark.asyncio
async def test_identity_spoofing_prevention(db_session):
    """Non-admin user cannot gain admin unlimited access even if username is 'admin'."""
    attacker, _ = await get_or_create_user(
        db_session,
        telegram_id=ATTACKER_TELEGRAM_ID,
        username="admin",
        first_name="Admin",
    )
    assert settings.is_admin(attacker.id) is False

    # Attempt to consume seconds for attacker
    # If balance is 0, normal user cannot consume
    bal = await get_minute_balance(db_session, attacker.id)
    bal.monthly_free_seconds = 0
    await db_session.flush()
    assert bal.total_usable_seconds == 0

    with pytest.raises(ValueError):
        await consume_seconds(
            session=db_session,
            user_id=attacker.id,
            seconds_to_consume=10,
            job_id="fake-job",
        )


@pytest.mark.asyncio
async def test_normal_user_regression(db_session, tmp_path):
    """Verify normal user behavior is strictly preserved (welcome bonus, deduction, ledger)."""
    user, _ = await get_or_create_user(db_session, telegram_id=NORMAL_TELEGRAM_ID + 99)
    awarded, bal = await award_welcome_bonus(db_session, user.id)
    assert awarded is True
    assert bal.welcome_seconds == 300
    await grant_monthly_free_allowance(db_session, user.id)
    bal = await get_minute_balance(db_session, user.id)
    assert bal.monthly_free_seconds == 180
    assert bal.total_usable_seconds == 480

    pth_file = tmp_path / "ModelReg.pth"
    pth_file.write_bytes(b"pth_content")
    model = VoiceModel(
        name="RegModel",
        slug="system-regmodel",
        type="SYSTEM",
        model_dir=str(tmp_path),
        pth_path=str(pth_file),
        status="ACTIVE",
        is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()

    job, _ = await create_voice_clone_job(
        session=db_session,
        user_id=user.id,
        text="Normal user regression test",
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
    )

    async def mock_tts(text, out_path, voice, speed):
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"fake_mp3_audio")
        return out_path

    async def mock_norm(in_audio, out_wav):
        out_wav.parent.mkdir(parents=True, exist_ok=True)
        out_wav.write_bytes(b"fake_norm_wav")
        return out_wav

    def mock_voxcpm(text, output_wav, **kwargs):  # patched via side_effect: MagicMock calls it WITHOUT self (worker passes all-keyword args)
        output_wav = Path(output_wav)
        output_wav.parent.mkdir(parents=True, exist_ok=True)
        output_wav.write_bytes(b"fake_voxcpm_wav")
        return output_wav

    with (
        patch("app.worker.generate_edge_tts", side_effect=mock_tts),
        patch("app.worker.normalize_audio_to_wav", side_effect=mock_norm),
        patch("app.services.voxcpm_engine.VoxCPMEngine.synthesize", side_effect=mock_voxcpm),
        patch("app.worker.get_wav_duration_seconds", return_value=12),
    ):
        success = await process_single_job(session=db_session, job=job)
        assert success is True

    # User balance debited (monthly free deducted first)
    bal_after = await get_minute_balance(db_session, user.id)
    assert bal_after.welcome_seconds == 300
    assert bal_after.monthly_free_seconds == 168  # 180 - 12 = 168
    assert bal_after.total_usable_seconds == 468

    # Ledger events created: MONTHLY_FREE credit, WELCOME credit, MONTHLY_FREE debit
    events = (
        await db_session.execute(
            select(MinuteLedgerEvent).where(MinuteLedgerEvent.user_id == user.id)
        )
    ).scalars().all()
    assert len(events) == 3
    debit_events = [e for e in events if e.direction == LedgerDirection.DEBIT.value]
    assert len(debit_events) == 1
    assert debit_events[0].source == LedgerSource.MONTHLY_FREE.value
    assert debit_events[0].seconds == 12

    # Normal user must NOT have any admin audit logs
    audit_logs = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.admin_id == user.id)
        )
    ).scalars().all()
    assert len(audit_logs) == 0

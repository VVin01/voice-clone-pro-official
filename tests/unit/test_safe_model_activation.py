from __future__ import annotations

from pathlib import Path
import pytest
from sqlalchemy import select

from app.config import settings
from app.models.enums import VoiceModelStatus, VoiceModelType
from app.models.schema import AuditLog, VoiceModel
from app.services.models import (
    activate_model,
    disable_model,
    get_available_models_for_user,
    scan_root,
)


@pytest.mark.asyncio
async def test_safe_activation_lifecycle_and_persistence(db_session, tmp_path: Path):
    """Verify that scan discovers model as READY, activation sets ACTIVE with AuditLog, and persists."""
    sys_dir = tmp_path / "system_models"
    model_folder = sys_dir / "TestVoice"
    model_folder.mkdir(parents=True)
    pth_file = model_folder / "TestVoice.pth"
    pth_file.write_bytes(b"dummy_pth_content")

    # 1. Scan root
    scanned = await scan_root(db_session, sys_dir, VoiceModelType.SYSTEM)
    assert len(scanned) == 1
    model = scanned[0]
    assert model.name == "TestVoice"
    assert model.status == VoiceModelStatus.READY.value
    await db_session.flush()

    # 2. Activate model with real admin ID
    admin_id = next(iter(settings.admin_ids)) if settings.admin_ids else 9999
    ok, msg = await activate_model(db_session, model.id, admin_id=admin_id)
    assert ok is True
    assert msg == "ACTIVATED"
    assert model.status == VoiceModelStatus.ACTIVE.value
    await db_session.flush()

    # Verify audit log
    audit_stmt = select(AuditLog).where(
        AuditLog.target_id == str(model.id),
        AuditLog.action == "voice_model.activate",
    )
    audit = (await db_session.execute(audit_stmt)).scalar_one_or_none()
    assert audit is not None
    assert audit.admin_id == admin_id

    # 3. Idempotent re-activation
    ok_again, msg_again = await activate_model(db_session, model.id, admin_id=admin_id)
    assert ok_again is True
    assert model.status == VoiceModelStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_activation_fails_if_pth_missing(db_session, tmp_path: Path):
    """Activation must reject models whose physical .pth file is missing."""
    ghost_pth = tmp_path / "ghost.pth"

    ghost_model = VoiceModel(
        name="GhostVoice",
        slug="system-ghost",
        type=VoiceModelType.SYSTEM.value,
        model_dir=str(tmp_path),
        pth_path=str(ghost_pth),
        status=VoiceModelStatus.READY.value,
    )
    db_session.add(ghost_model)
    await db_session.flush()

    admin_id = next(iter(settings.admin_ids)) if settings.admin_ids else 9999
    ok, msg = await activate_model(db_session, ghost_model.id, admin_id=admin_id)
    assert ok is False
    assert msg == "PTH_FILE_MISSING"
    assert ghost_model.status == VoiceModelStatus.BROKEN.value


@pytest.mark.asyncio
async def test_activation_non_existent_model(db_session):
    """Activation must return False when model_id does not exist."""
    admin_id = next(iter(settings.admin_ids)) if settings.admin_ids else 9999
    ok, msg = await activate_model(db_session, model_id=999999, admin_id=admin_id)
    assert ok is False
    assert msg == "MODEL_NOT_FOUND"


@pytest.mark.asyncio
async def test_scan_duplicate_idempotency(db_session, tmp_path: Path):
    """Scanning twice must update the existing record without creating duplicate rows."""
    sys_dir = tmp_path / "system_models"
    model_folder = sys_dir / "VoiceDupe"
    model_folder.mkdir(parents=True)
    pth_file = model_folder / "VoiceDupe.pth"
    pth_file.write_bytes(b"dupe_test")

    # Scan run 1
    res1 = await scan_root(db_session, sys_dir, VoiceModelType.SYSTEM)
    assert len(res1) == 1
    first_id = res1[0].id
    await db_session.flush()

    # Scan run 2
    res2 = await scan_root(db_session, sys_dir, VoiceModelType.SYSTEM)
    assert len(res2) == 1
    assert res2[0].id == first_id

    # Verify database has exactly 1 row
    all_models = (await db_session.execute(select(VoiceModel))).scalars().all()
    assert len(all_models) == 1

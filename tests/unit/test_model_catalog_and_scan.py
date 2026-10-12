from pathlib import Path
import pytest
from sqlalchemy import select

from app.models.enums import AccountCategory, VoiceModelStatus, VoiceModelType
from app.models.schema import AuditLog, VoiceModel
from app.services.ledger import credit_purchased_minutes, get_or_create_user
from app.services.models import (
    _artifact_pairs,
    activate_model,
    disable_model,
    get_authorized_model,
    get_available_models_for_user,
    scan_root,
    set_free_default_model,
)


def test_artifact_pairs_matching_logic(tmp_path: Path):
    root = tmp_path / "models"
    root.mkdir()

    # Case 1: same stem pairing
    folder1 = root / "VoiceA"
    folder1.mkdir()
    pth1 = folder1 / "VoiceA.pth"
    idx1 = folder1 / "VoiceA.index"
    pth1.write_bytes(b"pth1")
    idx1.write_bytes(b"idx1")

    # Case 2: different stem, but only one index in folder
    folder2 = root / "တွတ်ပီ"
    folder2.mkdir()
    pth2 = folder2 / "cartoon_100e.pth"
    idx2 = folder2 = folder2 / "trained.index"
    pth2.write_bytes(b"pth2")
    idx2.write_bytes(b"idx2")

    # Case 3: model without index
    folder3 = root / "VoiceC"
    folder3.mkdir()
    pth3 = folder3 / "VoiceC.pth"
    pth3.write_bytes(b"pth3")

    pairs = _artifact_pairs(root)
    assert len(pairs) == 3

    pair_dict = {p.name: idx.name if idx else None for p, idx in pairs}
    assert pair_dict["VoiceA.pth"] == "VoiceA.index"
    assert pair_dict["cartoon_100e.pth"] == "trained.index"
    assert pair_dict["VoiceC.pth"] is None


@pytest.mark.asyncio
async def test_scan_and_admin_review_workflow(tmp_path: Path, db_session):
    root = tmp_path / "system_models"
    root.mkdir()

    # Create 2 model folders
    m1_dir = root / "Zawgyi"
    m1_dir.mkdir()
    (m1_dir / "Zawgyi.pth").write_bytes(b"model1")
    (m1_dir / "Zawgyi.index").write_bytes(b"index1")

    m2_dir = root / "Nilar"
    m2_dir.mkdir()
    (m2_dir / "Nilar.pth").write_bytes(b"model2")

    # 1. Run local scan
    scanned = await scan_root(db_session, root, VoiceModelType.SYSTEM)
    assert len(scanned) == 2

    # Verify models are READY, NOT automatically ACTIVE!
    for m in scanned:
        assert m.status == VoiceModelStatus.READY.value
        assert m.is_default_free is False

    zawgyi = next(m for m in scanned if m.name == "Zawgyi")
    nilar = next(m for m in scanned if m.name == "Nilar")

    # 2. Admin reviews and activates Zawgyi
    ok, status = await activate_model(db_session, zawgyi.id, admin_id=999)
    assert ok is True
    assert zawgyi.status == VoiceModelStatus.ACTIVE.value

    # Verify audit log recorded
    logs = (await db_session.execute(
        select(AuditLog).where(AuditLog.target_id == str(zawgyi.id))
    )).scalars().all()
    assert len(logs) == 1
    assert logs[0].action == "voice_model.activate"

    # 3. Admin activates Nilar and sets Nilar as Free Default
    await activate_model(db_session, nilar.id, admin_id=999)
    ok_free, _ = await set_free_default_model(db_session, nilar.id, admin_id=999)
    assert ok_free is True
    assert nilar.is_default_free is True

    # 4. Check model catalog visibility for FREE user vs PURCHASED user (P1-1 alignment)
    free_user, _ = await get_or_create_user(db_session, telegram_id=111)
    purchased_user, _ = await get_or_create_user(db_session, telegram_id=222)
    await credit_purchased_minutes(db_session, purchased_user.id, minutes=5)

    # Free user sees all active admin-approved models
    free_models = await get_available_models_for_user(db_session, free_user.id)
    assert len(free_models) == 2
    assert {m.name for m in free_models} == {"Nilar", "Zawgyi"}

    # Purchased user also sees all active models identically
    purchased_models = await get_available_models_for_user(db_session, purchased_user.id)
    assert len(purchased_models) == 2
    assert {m.name for m in purchased_models} == {"Nilar", "Zawgyi"}

    # Authorized model check (both authorized for active system models)
    assert (await get_authorized_model(db_session, free_user.id, zawgyi.id)) is not None
    assert (await get_authorized_model(db_session, free_user.id, nilar.id)) is not None
    assert (await get_authorized_model(db_session, purchased_user.id, zawgyi.id)) is not None

    # 5. Admin disables Zawgyi
    await disable_model(db_session, zawgyi.id, admin_id=999)
    assert zawgyi.status == VoiceModelStatus.DISABLED.value
    # Disabled model no longer visible to anyone
    p_models_after = await get_available_models_for_user(db_session, purchased_user.id)
    assert len(p_models_after) == 1
    assert p_models_after[0].name == "Nilar"

    # 6. Physical file removal triggers BROKEN status on re-scan
    (nilar_dir := root / "Nilar" / "Nilar.pth").unlink()
    rescan = await scan_root(db_session, root, VoiceModelType.SYSTEM)
    assert nilar.status == VoiceModelStatus.BROKEN.value
    assert nilar.is_default_free is False

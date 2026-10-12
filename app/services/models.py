from __future__ import annotations

import logging
import re
from pathlib import Path
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.enums import AccountCategory, VoiceModelStatus, VoiceModelType
from app.models.schema import AuditLog, User, VoiceModel, utc_now

logger = logging.getLogger(__name__)


def safe_model_slug(name: str) -> str:
    """Sanitize model name or artifact path into a filesystem/slug-safe token."""
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower()
    return value or "model"


def _artifact_pairs(root: Path) -> list[tuple[Path, Path | None]]:
    """Discover every .pth artifact and pair with matching .index if present.

    Matching policy (matching legacy proven behavior):
    1. First tries same stem: <name>.pth -> <name>.index
    2. If not found, checks parent folder for exactly one sibling .index file.
    3. Otherwise index is None.
    """
    if not root.exists():
        return []

    pairs: list[tuple[Path, Path | None]] = []
    for pth in sorted(root.rglob("*.pth")):
        if not pth.is_file():
            continue
        same_stem = pth.with_suffix(".index")
        idx = same_stem if same_stem.is_file() else None
        if idx is None:
            siblings = sorted(x for x in pth.parent.glob("*.index") if x.is_file())
            if len(siblings) == 1:
                idx = siblings[0]
        pairs.append((pth, idx))
    return pairs


async def scan_root(
    session: AsyncSession,
    root: Path,
    model_type: VoiceModelType = VoiceModelType.SYSTEM,
    owner_user_id: int | None = None,
    display_name: str | None = None,
) -> list[VoiceModel]:
    """Scan directory for models, reconcile with database, and update status.

    Newly discovered system models receive READY status (requiring admin review).
    """
    root.mkdir(parents=True, exist_ok=True)
    found_models: list[VoiceModel] = []
    seen_slugs: set[str] = set()

    for pth, idx in _artifact_pairs(root):
        rel_parent = pth.parent.relative_to(root)
        # Logical UI name is folder name, or file stem if placed at root
        logical_name = display_name or (pth.parent.name if rel_parent != Path(".") else pth.stem)

        # Internal stable slug
        relative_artifact = pth.relative_to(settings.storage_root).as_posix() if settings.storage_root in pth.parents else pth.name
        slug = safe_model_slug(f"{model_type.value}-{relative_artifact}")
        seen_slugs.add(slug)

        # Match by physical path first
        stmt = select(VoiceModel).where(VoiceModel.pth_path == str(pth))
        res = await session.execute(stmt)
        model = res.scalar_one_or_none()

        if model is None:
            # Check by slug
            slug_stmt = select(VoiceModel).where(VoiceModel.slug == slug)
            model = (await session.execute(slug_stmt)).scalar_one_or_none()

        if model is None:
            # Brand new model discovered -> READY status (Admin must review before activation)
            model = VoiceModel(
                name=logical_name,
                slug=slug,
                type=model_type.value,
                owner_user_id=owner_user_id,
                model_dir=str(pth.parent),
                pth_path=str(pth),
                index_path=str(idx) if idx else None,
                status=VoiceModelStatus.ACTIVE.value if model_type == VoiceModelType.PERSONAL else VoiceModelStatus.READY.value,
                is_default_free=False,
            )
            session.add(model)
            await session.flush()
        else:
            # Existing row update
            model.name = logical_name
            model.model_dir = str(pth.parent)
            model.pth_path = str(pth)
            model.index_path = str(idx) if idx else None
            model.type = model_type.value
            model.owner_user_id = owner_user_id

            if model.status in (VoiceModelStatus.BROKEN.value, VoiceModelStatus.DISCOVERED.value):
                model.status = VoiceModelStatus.READY.value
            model.updated_at = utc_now()

        found_models.append(model)

    # Detect broken models (in DB under this root, but file missing on disk)
    all_existing = (await session.execute(
        select(VoiceModel).where(
            VoiceModel.type == model_type.value,
            VoiceModel.model_dir.like(str(root) + "%"),
        )
    )).scalars().all()

    for m in all_existing:
        if m.slug not in seen_slugs and not Path(m.pth_path).is_file():
            m.status = VoiceModelStatus.BROKEN.value
            m.is_default_free = False
            m.updated_at = utc_now()

    await session.flush()
    return found_models


async def scan_all_models(session: AsyncSession) -> list[VoiceModel]:
    """Scan system and personal model directories."""
    sys_root = settings.storage_root / "models" / "system"
    models = await scan_root(session, sys_root, VoiceModelType.SYSTEM)

    personal_root = settings.storage_root / "models" / "personal"
    personal_root.mkdir(parents=True, exist_ok=True)
    for folder in sorted(personal_root.iterdir()):
        if folder.is_dir():
            owner = int(folder.name) if folder.name.isdigit() else None
            p_models = await scan_root(
                session, folder, VoiceModelType.PERSONAL, owner,
                display_name=None if owner else folder.name,
            )
            models.extend(p_models)

    return models


async def activate_model(session: AsyncSession, model_id: int, admin_id: int) -> tuple[bool, str]:
    """Admin activates a READY/DISCOVERED model after review."""
    model = await session.get(VoiceModel, model_id)
    if not model:
        return False, "MODEL_NOT_FOUND"

    if not Path(model.pth_path).is_file():
        model.status = VoiceModelStatus.BROKEN.value
        await session.flush()
        return False, "PTH_FILE_MISSING"

    model.status = VoiceModelStatus.ACTIVE.value
    model.updated_at = utc_now()

    session.add(AuditLog(
        admin_id=admin_id,
        action="voice_model.activate",
        target_type="voice_model",
        target_id=str(model_id),
        details_json=f'{{"name": "{model.name}", "status": "{model.status}"}}',
    ))
    await session.flush()
    return True, "ACTIVATED"


async def disable_model(session: AsyncSession, model_id: int, admin_id: int) -> tuple[bool, str]:
    """Admin disables an ACTIVE model."""
    model = await session.get(VoiceModel, model_id)
    if not model:
        return False, "MODEL_NOT_FOUND"

    model.status = VoiceModelStatus.DISABLED.value
    model.is_default_free = False
    model.updated_at = utc_now()

    session.add(AuditLog(
        admin_id=admin_id,
        action="voice_model.disable",
        target_type="voice_model",
        target_id=str(model_id),
        details_json=f'{{"name": "{model.name}", "status": "{model.status}"}}',
    ))
    await session.flush()
    return True, "DISABLED"


async def set_free_default_model(session: AsyncSession, model_id: int, admin_id: int) -> tuple[bool, str]:
    """Set an active system model as the free default. Unsets previous defaults."""
    model = await session.get(VoiceModel, model_id)
    if not model or model.status != VoiceModelStatus.ACTIVE.value or model.type != VoiceModelType.SYSTEM.value:
        return False, "ACTIVE_SYSTEM_MODEL_REQUIRED"

    # Clear existing free default
    await session.execute(
        update(VoiceModel)
        .where(VoiceModel.type == VoiceModelType.SYSTEM.value, VoiceModel.is_default_free.is_(True))
        .values(is_default_free=False, updated_at=utc_now())
    )

    model.is_default_free = True
    model.updated_at = utc_now()

    session.add(AuditLog(
        admin_id=admin_id,
        action="voice_model.set_free_default",
        target_type="voice_model",
        target_id=str(model_id),
        details_json=f'{{"name": "{model.name}"}}',
    ))
    await session.flush()
    return True, "SET_FREE_DEFAULT"


async def get_available_models_for_user(session: AsyncSession, user_id: int) -> list[VoiceModel]:
    """Single authoritative source of truth for models a user is allowed to use.

    Per product directive, FREE and PURCHASED users have identical model access
    privileges. Both see all active admin-approved SYSTEM models plus any active
    PERSONAL models owned by the user.
    """
    stmt = (
        select(VoiceModel)
        .where(
            VoiceModel.status == VoiceModelStatus.ACTIVE.value,
            (VoiceModel.type == VoiceModelType.SYSTEM.value)
            | ((VoiceModel.type == VoiceModelType.PERSONAL.value) & (VoiceModel.owner_user_id == user_id)),
        )
        .order_by(VoiceModel.name)
    )

    res = await session.execute(stmt)
    return list(res.scalars().all())


async def get_authorized_model(session: AsyncSession, user_id: int, model_id: int | None) -> VoiceModel | None:
    """Validate that model_id is authorized for user. Fail closed."""
    if model_id is None:
        return None
    available = await get_available_models_for_user(session, user_id)
    for m in available:
        if m.id == model_id:
            return m
    return None

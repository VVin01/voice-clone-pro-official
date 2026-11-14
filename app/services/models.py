from __future__ import annotations

import logging
import re
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.enums import AccountCategory, VoiceModelStatus, VoiceModelType
from app.models.schema import User, UserVoiceModelGrant, VoiceModel

logger = logging.getLogger(__name__)


def is_legacy_local_model(model) -> bool:
    """True for legacy pre-VoxCPM2 SYSTEM model rows (the old trained
    .pth voices — e.g. the rows the admin screen showed as "The Light",
    "Kartoon", "Myanmar Voice 1", "Ant").

    Ground truth (bot cleanup 2026-11-03): these rows exist only in the
    VoiceModel table — nothing in code seeds or defines them any more
    (session.init_db seeds only the VoxCPM row, slug "voxcpm-cloud-ai",
    pth_path "voxcpm"). Their artifact files are gone and queue.py maps
    every job for a non-"voxcpm" model onto the VoxCPM row, so the rows
    are display-only zombies: ACTIVE in the DB yet unusable, which is
    exactly what made the old admin screen show 🔴 "(ACTIVE)".

    They are HIDDEN from display surfaces, never deleted: history
    rendering (services.history.get_models_map_for_jobs) and old job
    records still resolve model names/ids through the rows. PERSONAL
    models are user property and are never legacy by this test.
    """
    if getattr(model, "type", None) != VoiceModelType.SYSTEM.value:
        return False
    pth = (getattr(model, "pth_path", "") or "").strip().lower()
    slug = (getattr(model, "slug", "") or "").lower()
    return pth != "voxcpm" and "voxcpm" not in slug


def is_displayable_model(model) -> bool:
    """Catalogue visibility: everything except legacy local models."""
    return not is_legacy_local_model(model)


def safe_model_slug(name: str) -> str:
    """Sanitize model name or artifact path into a filesystem/slug-safe token."""
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower()
    return value or "model"


def find_model_sample_audio(model_dir: Path) -> Path | None:
    """Find a voice sample audio file in the model directory if present.

    Searches for:
    1. sample.mp3, sample.wav, sample.ogg, sample.m4a
    2. preview.mp3, preview.wav, preview.ogg, preview.m4a
    3. Any file containing 'sample' or 'preview' in stem
    """
    if not model_dir.exists() or not model_dir.is_dir():
        return None

    # Exact matches first
    for base in ("sample", "preview", "demo"):
        for ext in (".mp3", ".wav", ".ogg", ".m4a"):
            cand = model_dir / f"{base}{ext}"
            if cand.is_file():
                return cand

    # Pattern matches
    for ext in (".mp3", ".wav", ".ogg", ".m4a"):
        for f in model_dir.glob(f"*{ext}"):
            lower_stem = f.stem.lower()
            if "sample" in lower_stem or "preview" in lower_stem or "demo" in lower_stem:
                return f

    return None


async def get_available_models_for_user(session: AsyncSession, user_id: int) -> list[VoiceModel]:
    """Single authoritative source of truth for models a user is allowed to use.

    - Admins: see all active models.
    - PURCHASED users: see all active system models explicitly allowed for Purchased,
      plus active personal models owned by the user.
    - FREE users: see all active system models explicitly allowed for Free.
    """
    is_admin = settings.is_admin(user_id)
    if is_admin:
        stmt = (
            select(VoiceModel)
            .where(VoiceModel.status == VoiceModelStatus.ACTIVE.value)
            .order_by(VoiceModel.name)
        )
        res = await session.execute(stmt)
        return list(res.scalars().all())

    user_stmt = select(User).where(User.id == user_id)
    user_res = await session.execute(user_stmt)
    user = user_res.scalar_one_or_none()
    is_purchased = (user is not None and user.account_category == AccountCategory.PURCHASED.value)

    if is_purchased:
        granted_subq = (
            select(UserVoiceModelGrant.model_id)
            .where(UserVoiceModelGrant.user_id == user_id)
            .scalar_subquery()
        )
        stmt = (
            select(VoiceModel)
            .where(
                VoiceModel.status == VoiceModelStatus.ACTIVE.value,
                (
                    (VoiceModel.type == VoiceModelType.SYSTEM.value)
                    & (VoiceModel.allowed_for_purchased.is_(True))
                )
                | (
                    (VoiceModel.type == VoiceModelType.PERSONAL.value)
                    & (VoiceModel.owner_user_id == user_id)
                )
                | (
                    VoiceModel.id.in_(granted_subq)
                ),
            )
            .order_by(VoiceModel.name)
        )
    else:
        stmt = (
            select(VoiceModel)
            .where(
                VoiceModel.status == VoiceModelStatus.ACTIVE.value,
                VoiceModel.type == VoiceModelType.SYSTEM.value,
                VoiceModel.allowed_for_free.is_(True),
            )
            .order_by(VoiceModel.name)
        )

    res = await session.execute(stmt)
    return list(res.scalars().all())


async def get_displayable_models_for_user(session: AsyncSession, user_id: int) -> list[VoiceModel]:
    """Catalogue-facing variant of get_available_models_for_user.

    Bot cleanup 2026-11-03: display surfaces (bot samples catalogue,
    bot clone model picker, Mini App /voices/models) must not list
    the legacy .pth SYSTEM rows. The filter lives HERE, not inside
    get_available_models_for_user, because that function is also the
    authorization source for job creation (get_authorized_model) —
    and the generation path must keep its existing remap semantics
    for granted/legacy rows (queue.py logs and falls back to VoxCPM).
    """
    models = await get_available_models_for_user(session, user_id)
    return [m for m in models if is_displayable_model(m)]


async def get_authorized_model(session: AsyncSession, user_id: int, model_id: int | None) -> VoiceModel | None:
    """Validate that model_id is authorized for user. Fail closed."""
    if model_id is None:
        return None
    available = await get_available_models_for_user(session, user_id)
    for m in available:
        if m.id == model_id:
            return m
    return None


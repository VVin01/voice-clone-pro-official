from __future__ import annotations

import logging
import uuid
from typing import Any
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.enums import AccountCategory, CustomVoiceRequestStatus, VoiceModelStatus, VoiceModelType
from app.models.schema import CustomVoiceRequest, User, UserVoiceModelGrant, VoiceModel

logger = logging.getLogger(__name__)

MAX_CUSTOM_VOICE_MODELS_PER_USER = 2


async def get_user_custom_voice_quota(session: AsyncSession, user_id: int) -> dict[str, Any]:
    """Retrieve custom voice quota and usage details for a user."""
    is_admin = settings.is_admin(user_id)
    user_stmt = select(User).where(User.id == user_id)
    user = (await session.execute(user_stmt)).scalar_one_or_none()
    is_purchased = (user is not None and user.account_category == AccountCategory.PURCHASED.value)

    # 1. Fetch active custom models (granted via UserVoiceModelGrant or directly owned personal models)
    granted_subq = (
        select(UserVoiceModelGrant.model_id)
        .where(UserVoiceModelGrant.user_id == user_id)
    )
    models_stmt = (
        select(VoiceModel)
        .where(
            VoiceModel.status == VoiceModelStatus.ACTIVE.value,
            (
                (VoiceModel.id.in_(granted_subq))
                | (
                    (VoiceModel.type == VoiceModelType.PERSONAL.value)
                    & (VoiceModel.owner_user_id == user_id)
                )
            ),
        )
        .order_by(VoiceModel.name)
    )
    active_models = list((await session.execute(models_stmt)).scalars().all())

    # 2. Fetch pending requests
    pending_stmt = (
        select(CustomVoiceRequest)
        .where(
            CustomVoiceRequest.user_id == user_id,
            CustomVoiceRequest.status == CustomVoiceRequestStatus.PENDING.value,
        )
        .order_by(CustomVoiceRequest.created_at.desc())
    )
    pending_requests = list((await session.execute(pending_stmt)).scalars().all())

    # 3. Calculate quota slots
    max_allowed = MAX_CUSTOM_VOICE_MODELS_PER_USER
    slots_used = len(active_models) + len(pending_requests)
    remaining_slots = max(0, max_allowed - slots_used)
    can_request = (is_purchased or is_admin) and remaining_slots > 0

    return {
        "user_id": user_id,
        "is_purchased": is_purchased,
        "is_admin": is_admin,
        "max_allowed": max_allowed,
        "slots_used": slots_used,
        "remaining_slots": remaining_slots,
        "can_request": can_request,
        "active_models": active_models,
        "pending_requests": pending_requests,
    }


async def create_custom_voice_request(
    session: AsyncSession,
    user_id: int,
    model_name: str,
    sample_file_id: str,
    sample_file_name: str | None = None,
    sample_duration_seconds: int | None = None,
    notes: str | None = None,
) -> tuple[bool, CustomVoiceRequest | None, str]:
    """Submit a new custom voice request for a Purchased user or Admin."""
    quota = await get_user_custom_voice_quota(session, user_id)
    if not (quota["is_purchased"] or quota["is_admin"]):
        return False, None, "PURCHASED_TIER_REQUIRED"

    if quota["remaining_slots"] <= 0:
        return False, None, "QUOTA_LIMIT_REACHED"

    req_id = f"CVR-{uuid.uuid4().hex[:8].upper()}"
    req = CustomVoiceRequest(
        id=req_id,
        user_id=user_id,
        model_name=model_name.strip()[:255],
        sample_file_id=sample_file_id,
        sample_file_name=sample_file_name[:255] if sample_file_name else None,
        sample_duration_seconds=sample_duration_seconds,
        notes=notes,
        status=CustomVoiceRequestStatus.PENDING.value,
    )
    session.add(req)
    await session.flush()

    logger.info("Created CustomVoiceRequest %s for user %s (model: %s)", req_id, user_id, model_name)
    return True, req, "REQUEST_CREATED"

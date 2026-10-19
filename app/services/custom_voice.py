from __future__ import annotations

import logging
import uuid
from typing import Any
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.enums import AccountCategory, CustomVoiceRequestStatus, VoiceModelStatus, VoiceModelType
from app.models.schema import AuditLog, CustomVoiceRequest, User, UserVoiceModelGrant, VoiceModel, utc_now

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


async def get_custom_voice_requests(
    session: AsyncSession,
    status: str | None = None,
    user_id: int | None = None,
) -> list[CustomVoiceRequest]:
    """List custom voice requests with optional filters."""
    stmt = select(CustomVoiceRequest)
    if status is not None:
        stmt = stmt.where(CustomVoiceRequest.status == status)
    if user_id is not None:
        stmt = stmt.where(CustomVoiceRequest.user_id == user_id)
    stmt = stmt.order_by(CustomVoiceRequest.created_at.desc())
    res = await session.execute(stmt)
    return list(res.scalars().all())


async def get_custom_voice_request_by_id(
    session: AsyncSession,
    request_id: str,
) -> CustomVoiceRequest | None:
    """Fetch single request by ID."""
    stmt = select(CustomVoiceRequest).where(CustomVoiceRequest.id == request_id)
    res = await session.execute(stmt)
    return res.scalar_one_or_none()


async def assign_model_to_request(
    session: AsyncSession,
    request_id: str,
    model_id: int,
    admin_id: int,
    notes: str | None = None,
) -> tuple[bool, CustomVoiceRequest | None, str]:
    """Admin fulfills request by assigning a voice model (newly trained or existing)."""
    req = await get_custom_voice_request_by_id(session, request_id)
    if not req:
        return False, None, "REQUEST_NOT_FOUND"

    model = await session.get(VoiceModel, model_id)
    if not model:
        return False, None, "MODEL_NOT_FOUND"

    # 1. Grant user access to this model if not already granted
    grant_stmt = select(UserVoiceModelGrant).where(
        UserVoiceModelGrant.user_id == req.user_id,
        UserVoiceModelGrant.model_id == model_id,
    )
    grant = (await session.execute(grant_stmt)).scalar_one_or_none()
    if not grant:
        new_grant = UserVoiceModelGrant(
            user_id=req.user_id,
            model_id=model_id,
            granted_by=admin_id,
            notes=f"Assigned via request {request_id}",
        )
        session.add(new_grant)

    # 2. Update request status to APPROVED
    req.status = CustomVoiceRequestStatus.APPROVED.value
    req.assigned_model_id = model_id
    req.admin_notes = notes
    req.reviewed_by = admin_id
    req.reviewed_at = utc_now()
    req.updated_at = utc_now()

    # 3. Log audit event
    session.add(
        AuditLog(
            admin_id=admin_id,
            action="custom_voice.assign",
            target_type="custom_voice_request",
            target_id=request_id,
            details_json=f'{{"user_id": {req.user_id}, "model_id": {model_id}, "model_name": "{model.name}"}}',
        )
    )
    await session.flush()
    logger.info("Admin %s assigned model %s to request %s (user %s)", admin_id, model_id, request_id, req.user_id)
    return True, req, "MODEL_ASSIGNED"


async def reject_custom_voice_request(
    session: AsyncSession,
    request_id: str,
    admin_id: int,
    reason: str,
) -> tuple[bool, CustomVoiceRequest | None, str]:
    """Admin rejects a request with an explanation, freeing user's slot."""
    req = await get_custom_voice_request_by_id(session, request_id)
    if not req:
        return False, None, "REQUEST_NOT_FOUND"

    req.status = CustomVoiceRequestStatus.REJECTED.value
    req.admin_notes = reason.strip()
    req.reviewed_by = admin_id
    req.reviewed_at = utc_now()
    req.updated_at = utc_now()

    session.add(
        AuditLog(
            admin_id=admin_id,
            action="custom_voice.reject",
            target_type="custom_voice_request",
            target_id=request_id,
            details_json=f'{{"user_id": {req.user_id}, "reason": "{reason.strip()}"}}',
        )
    )
    await session.flush()
    logger.info("Admin %s rejected request %s (user %s, reason: %s)", admin_id, request_id, req.user_id, reason)
    return True, req, "REQUEST_REJECTED"


async def grant_user_model_access(
    session: AsyncSession,
    user_id: int,
    model_id: int,
    admin_id: int,
    notes: str | None = None,
) -> tuple[bool, UserVoiceModelGrant | None, str]:
    """Directly grant a user access to any voice model."""
    user = await session.get(User, user_id)
    if not user:
        return False, None, "USER_NOT_FOUND"

    model = await session.get(VoiceModel, model_id)
    if not model:
        return False, None, "MODEL_NOT_FOUND"

    grant_stmt = select(UserVoiceModelGrant).where(
        UserVoiceModelGrant.user_id == user_id,
        UserVoiceModelGrant.model_id == model_id,
    )
    existing = (await session.execute(grant_stmt)).scalar_one_or_none()
    if existing:
        return True, existing, "ALREADY_GRANTED"

    grant = UserVoiceModelGrant(
        user_id=user_id,
        model_id=model_id,
        granted_by=admin_id,
        notes=notes,
    )
    session.add(grant)
    session.add(
        AuditLog(
            admin_id=admin_id,
            action="custom_voice.direct_grant",
            target_type="user_voice_model_grant",
            target_id=str(model_id),
            details_json=f'{{"user_id": {user_id}, "model_id": {model_id}}}',
        )
    )
    await session.flush()
    return True, grant, "ACCESS_GRANTED"


async def revoke_user_model_access(
    session: AsyncSession,
    user_id: int,
    model_id: int,
    admin_id: int,
) -> tuple[bool, str]:
    """Revoke a user's access to a granted voice model."""
    grant_stmt = select(UserVoiceModelGrant).where(
        UserVoiceModelGrant.user_id == user_id,
        UserVoiceModelGrant.model_id == model_id,
    )
    grant = (await session.execute(grant_stmt)).scalar_one_or_none()
    if not grant:
        return False, "GRANT_NOT_FOUND"

    await session.delete(grant)
    session.add(
        AuditLog(
            admin_id=admin_id,
            action="custom_voice.revoke_grant",
            target_type="user_voice_model_grant",
            target_id=str(model_id),
            details_json=f'{{"user_id": {user_id}, "model_id": {model_id}}}',
        )
    )
    await session.flush()
    return True, "ACCESS_REVOKED"

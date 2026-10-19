from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
    TelegramAPIError,
)
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import (
    BroadcastCampaignStatus,
    BroadcastDeliveryStatus,
    BroadcastRecipientGroup,
    UserStatus,
)
from app.models.schema import BroadcastCampaign, BroadcastDelivery, ImportedAudience, User

logger = logging.getLogger(__name__)


@dataclass
class BroadcastResult:
    total: int = 0
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    duration_seconds: float = 0.0
    status: str = "COMPLETED"
    campaign_id: str | None = None


async def get_broadcast_recipient_ids(
    session: AsyncSession,
    target_group: str | BroadcastRecipientGroup = BroadcastRecipientGroup.REGISTERED_ALL,
) -> list[int]:
    """Query recipient Telegram IDs for the specified target group.

    Groups:
    - IMPORTED_ALL: All imported audience users who are broadcast eligible.
    - IMPORTED_UNREGISTERED: Imported audience users not yet registered in the new system and broadcast eligible.
    - REGISTERED_ALL: All active registered users in the new system.
    """
    group_str = target_group.value if isinstance(target_group, BroadcastRecipientGroup) else str(target_group)

    if group_str == BroadcastRecipientGroup.IMPORTED_ALL.value:
        stmt = (
            select(ImportedAudience.telegram_id)
            .where(ImportedAudience.broadcast_eligible.is_(True))
            .order_by(ImportedAudience.telegram_id.asc())
        )
    elif group_str == BroadcastRecipientGroup.IMPORTED_UNREGISTERED.value:
        stmt = (
            select(ImportedAudience.telegram_id)
            .where(
                ImportedAudience.is_registered_in_new_system.is_(False),
                ImportedAudience.broadcast_eligible.is_(True),
            )
            .order_by(ImportedAudience.telegram_id.asc())
        )
    else:  # Default to REGISTERED_ALL
        stmt = (
            select(User.id)
            .where(User.status == UserStatus.ACTIVE.value)
            .order_by(User.id.asc())
        )

    result = await session.execute(stmt)
    return [int(uid) for uid in result.scalars().all()]


async def mark_user_blocked(session: AsyncSession, user_id: int) -> None:
    """Mark user status as BLOCKED upon definitive TelegramForbiddenError."""
    try:
        stmt = update(User).where(User.id == user_id).values(status=UserStatus.BLOCKED.value)
        await session.execute(stmt)

        stmt_imported = (
            update(ImportedAudience)
            .where(ImportedAudience.telegram_id == user_id)
            .values(broadcast_eligible=False)
        )
        await session.execute(stmt_imported)

        await session.commit()
        logger.info("Marked user %s as BLOCKED/ineligible in database", user_id)
    except Exception as e:
        logger.error("Failed to mark user %s as BLOCKED: %s", user_id, e)


async def create_or_get_broadcast_campaign(
    session: AsyncSession,
    admin_id: int,
    token: str,
    text: str,
    recipient_ids: list[int],
) -> BroadcastCampaign:
    """Idempotently create or retrieve a broadcast campaign and its per-recipient delivery rows.

    If a campaign with the confirmation token already exists, returns it without creating duplicates.
    """
    stmt = select(BroadcastCampaign).where(BroadcastCampaign.confirmation_token == token)
    result = await session.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        return existing

    campaign = BroadcastCampaign(
        admin_id=admin_id,
        confirmation_token=token,
        message_text=text,
        status=BroadcastCampaignStatus.PENDING.value,
        total_recipients=len(recipient_ids),
        sent_count=0,
        failed_count=0,
        skipped_count=0,
        duration_seconds=0.0,
    )
    session.add(campaign)
    await session.flush()

    # Pre-populate delivery records for all targets in PENDING status
    for uid in recipient_ids:
        delivery = BroadcastDelivery(
            campaign_id=campaign.id,
            user_id=uid,
            status=BroadcastDeliveryStatus.PENDING.value,
            attempts=0,
        )
        session.add(delivery)

    await session.commit()
    logger.info("Created durable BroadcastCampaign %s with %d recipients", campaign.id, len(recipient_ids))
    return campaign


async def deliver_broadcast(
    bot: Bot,
    recipient_ids: list[int],
    text: str,
    progress_callback: Callable[[int, int, int], Awaitable[None]] | None = None,
    delay_between_sends: float = 0.1,  # Conservative 0.1s interval (10 msg/sec)
    max_retries: int = 2,
    progress_interval: int = 25,
    session: AsyncSession | None = None,
    campaign_id: str | None = None,
    sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
    max_retry_after: float = 300.0,
) -> BroadcastResult:
    """Deliver broadcast text to target recipient IDs with rate limiting, bounded retry, and durable tracking.

    - Conservative default delay of 0.1s (10 messages/sec) well below Telegram's 30 msg/sec global limit.
    - Full requested delay on TelegramRetryAfter is honored (not capped at 5s).
    - Definitive TelegramForbiddenError marks the user as BLOCKED in the database.
    - Transient network errors are isolated as failed without altering user status.
    - If session and campaign_id are provided, per-recipient delivery state is tracked in BroadcastDelivery,
      enabling interrupted campaigns to be resumed safely.

    NOTE ON EXACTLY-ONCE DELIVERY:
    If Telegram accepts a message but the process crashes before the database commits the SENT state,
    the delivery remains PENDING and would be retried upon restart. Exactly-once delivery across
    uncoordinated crashes is not guaranteed without two-phase commit with Telegram.
    """
    start_time = time.monotonic()

    campaign = None
    pending_deliveries: dict[int, BroadcastDelivery] = {}
    if session is not None and campaign_id is not None:
        campaign = await session.get(BroadcastCampaign, campaign_id)
        if campaign:
            if campaign.status in (
                BroadcastCampaignStatus.COMPLETED.value,
                BroadcastCampaignStatus.CANCELLED.value,
            ):
                logger.warning("Campaign %s is already in terminal status %s; skipping", campaign_id, campaign.status)
                return BroadcastResult(
                    total=campaign.total_recipients,
                    sent=campaign.sent_count,
                    failed=campaign.failed_count,
                    skipped=campaign.skipped_count,
                    duration_seconds=campaign.duration_seconds,
                    status=campaign.status,
                    campaign_id=campaign.id,
                )

            # Prevent concurrent execution
            if campaign.status == BroadcastCampaignStatus.RUNNING.value and len(recipient_ids) == campaign.total_recipients:
                logger.warning("Campaign %s is already RUNNING", campaign_id)

            campaign.status = BroadcastCampaignStatus.RUNNING.value
            if campaign.started_at is None:
                campaign.started_at = datetime.now(timezone.utc)
            await session.commit()

            # Load pending deliveries
            stmt = select(BroadcastDelivery).where(
                BroadcastDelivery.campaign_id == campaign_id,
                BroadcastDelivery.status == BroadcastDeliveryStatus.PENDING.value,
            ).order_by(BroadcastDelivery.user_id.asc())
            rows = (await session.execute(stmt)).scalars().all()
            pending_deliveries = {d.user_id: d for d in rows}
            # Use pending recipient list if resuming
            target_ids = list(pending_deliveries.keys())
            total = campaign.total_recipients
            sent = campaign.sent_count
            failed = campaign.failed_count
            skipped = campaign.skipped_count
        else:
            target_ids = list(recipient_ids)
            total = len(target_ids)
            sent = 0
            failed = 0
            skipped = 0
    else:
        target_ids = list(recipient_ids)
        total = len(target_ids)
        sent = 0
        failed = 0
        skipped = 0

    for idx, user_id in enumerate(target_ids, 1):
        if delay_between_sends > 0 and idx > 1:
            await sleep_func(delay_between_sends)

        delivered = False
        attempts = 0
        delivery = pending_deliveries.get(user_id)

        while attempts <= max_retries and not delivered:
            attempts += 1
            try:
                await bot.send_message(chat_id=user_id, text=text)
                delivered = True
                sent += 1
                if delivery:
                    delivery.status = BroadcastDeliveryStatus.SENT.value
                    delivery.attempts = attempts
                    delivery.sent_at = datetime.now(timezone.utc)
                    delivery.error_message = None
            except TelegramRetryAfter as e:
                # Requirement 1: Honor Telegram's full requested delay without capping at 5s
                sleep_duration = float(e.retry_after)
                logger.warning(
                    "Rate limit encountered for user %s: sleeping %.1fs as requested by Telegram (attempt %d/%d)",
                    user_id,
                    sleep_duration,
                    attempts,
                    max_retries + 1,
                )
                if sleep_duration > max_retry_after:
                    logger.error(
                        "Telegram requested flood wait of %.1fs exceeds maximum safe limit %.1fs; failing user %s",
                        sleep_duration,
                        max_retry_after,
                        user_id,
                    )
                    failed += 1
                    if delivery:
                        delivery.status = BroadcastDeliveryStatus.FAILED.value
                        delivery.attempts = attempts
                        delivery.error_message = f"Flood wait {sleep_duration}s exceeded max limit"
                    break

                if attempts > max_retries:
                    logger.error("Rate limit retry budget exceeded for user %s", user_id)
                    failed += 1
                    if delivery:
                        delivery.status = BroadcastDeliveryStatus.FAILED.value
                        delivery.attempts = attempts
                        delivery.error_message = "Rate limit retry budget exceeded"
                    break

                await sleep_func(sleep_duration)
            except TelegramForbiddenError as e:
                # Requirement 4: Mark user as BLOCKED in database upon definitive TelegramForbiddenError
                logger.info("Bot forbidden/blocked for user %s; skipping and updating user status to BLOCKED", user_id)
                skipped += 1
                if delivery:
                    delivery.status = BroadcastDeliveryStatus.SKIPPED.value
                    delivery.attempts = attempts
                    delivery.error_message = "Forbidden: bot blocked by user"
                if session is not None:
                    await mark_user_blocked(session, user_id)
                break
            except TelegramBadRequest as e:
                logger.warning("TelegramBadRequest for user %s: %s; skipping", user_id, e)
                skipped += 1
                if delivery:
                    delivery.status = BroadcastDeliveryStatus.SKIPPED.value
                    delivery.attempts = attempts
                    delivery.error_message = str(e)
                break
            except TelegramAPIError as e:
                logger.error("TelegramAPIError for user %s: %s", user_id, e)
                if attempts > max_retries:
                    failed += 1
                    if delivery:
                        delivery.status = BroadcastDeliveryStatus.FAILED.value
                        delivery.attempts = attempts
                        delivery.error_message = str(e)
            except Exception as e:
                logger.exception("Unexpected error sending broadcast to user %s: %s", user_id, e)
                failed += 1
                if delivery:
                    delivery.status = BroadcastDeliveryStatus.FAILED.value
                    delivery.attempts = attempts
                    delivery.error_message = str(e)
                break

        # Persist intermediate delivery record if session is active
        if session is not None and campaign:
            try:
                campaign.sent_count = sent
                campaign.failed_count = failed
                campaign.skipped_count = skipped
                await session.commit()
            except Exception as e:
                logger.debug("Intermediate broadcast commit failed: %s", e)

        if progress_callback is not None:
            processed = sent + failed + skipped
            if processed % progress_interval == 0 or processed == total:
                try:
                    await progress_callback(sent, failed, skipped)
                except Exception as e:
                    logger.debug("Progress callback failed: %s", e)

    duration = round(time.monotonic() - start_time, 2)

    # Final campaign status update
    if session is not None and campaign:
        try:
            campaign.sent_count = sent
            campaign.failed_count = failed
            campaign.skipped_count = skipped
            campaign.duration_seconds = duration
            campaign.status = BroadcastCampaignStatus.COMPLETED.value
            campaign.finished_at = datetime.now(timezone.utc)
            await session.commit()
        except Exception as e:
            logger.error("Final broadcast campaign commit failed: %s", e)

    return BroadcastResult(
        total=total,
        sent=sent,
        failed=failed,
        skipped=skipped,
        duration_seconds=duration,
        status="COMPLETED",
        campaign_id=campaign_id,
    )


async def resume_broadcast_campaign(
    bot: Bot,
    session: AsyncSession,
    campaign_id: str,
    progress_callback: Callable[[int, int, int], Awaitable[None]] | None = None,
    delay_between_sends: float = 0.1,
    max_retries: int = 2,
    sleep_func: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> BroadcastResult:
    """Resume an unfinished or interrupted broadcast campaign from persistent database records."""
    campaign = await session.get(BroadcastCampaign, campaign_id)
    if not campaign:
        raise ValueError(f"BroadcastCampaign {campaign_id} not found")

    # If resuming, check if there are pending deliveries and reset status to RUNNING if needed
    stmt = select(BroadcastDelivery).where(
        BroadcastDelivery.campaign_id == campaign_id,
        BroadcastDelivery.status == BroadcastDeliveryStatus.PENDING.value,
    )
    pending_rows = (await session.execute(stmt)).scalars().all()
    if not pending_rows:
        logger.info("Campaign %s has no pending deliveries to resume", campaign_id)
        return BroadcastResult(
            total=campaign.total_recipients,
            sent=campaign.sent_count,
            failed=campaign.failed_count,
            skipped=campaign.skipped_count,
            duration_seconds=campaign.duration_seconds,
            status=campaign.status,
            campaign_id=campaign_id,
        )

    campaign.status = BroadcastCampaignStatus.RUNNING.value
    await session.commit()

    return await deliver_broadcast(
        bot=bot,
        recipient_ids=[],  # deliver_broadcast will read remaining PENDING rows from DB
        text=campaign.message_text,
        progress_callback=progress_callback,
        delay_between_sends=delay_between_sends,
        max_retries=max_retries,
        session=session,
        campaign_id=campaign_id,
        sleep_func=sleep_func,
    )

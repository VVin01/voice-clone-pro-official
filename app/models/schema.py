from __future__ import annotations

import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import (
    AccountCategory,
    BroadcastCampaignStatus,
    BroadcastDeliveryStatus,
    CustomVoiceRequestStatus,
    JobStatus,
    JobType,
    PaymentStatus,
    UserStatus,
    VoiceModelStatus,
    VoiceModelType,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


PK_INT = BigInteger().with_variant(Integer, "sqlite")


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram ID
    username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    language_code: Mapped[str] = mapped_column(String(10), nullable=False, default="my")
    account_category: Mapped[str] = mapped_column(
        String(20), nullable=False, default=AccountCategory.FREE.value
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=UserStatus.ACTIVE.value
    )

    channel_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    group_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    welcome_bonus_awarded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    referral_code: Mapped[str | None] = mapped_column(
        String(32), unique=True, index=True, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        onupdate=utc_now,
    )

    # Relationships
    balance: Mapped["MinuteBalance | None"] = relationship(
        "MinuteBalance", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(
            "account_category IN ('FREE', 'PURCHASED')",
            name="chk_users_account_category",
        ),
    )


class MinuteBalance(Base):
    __tablename__ = "minute_balances"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    monthly_free_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    last_monthly_grant_period: Mapped[str | None] = mapped_column(String(7), nullable=True)  # YYYY-MM
    welcome_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    referral_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    purchased_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    consumed_seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        onupdate=utc_now,
    )

    user: Mapped["User"] = relationship("User", back_populates="balance")

    @property
    def total_usable_seconds(self) -> int:
        return max(
            0,
            self.monthly_free_seconds
            + self.purchased_seconds
            + self.welcome_seconds
            + self.referral_seconds,
        )

    @property
    def total_usable_minutes(self) -> float:
        return self.total_usable_seconds / 60.0

    @property
    def monthly_free_minutes(self) -> float:
        return max(0, self.monthly_free_seconds) / 60.0

    @property
    def purchased_minutes(self) -> float:
        return max(0, self.purchased_seconds) / 60.0

    @property
    def welcome_minutes(self) -> float:
        return max(0, self.welcome_seconds) / 60.0

    @property
    def referral_minutes(self) -> float:
        return max(0, self.referral_seconds) / 60.0

    __table_args__ = (
        CheckConstraint("monthly_free_seconds >= 0", name="chk_balance_monthly_free_non_neg"),
        CheckConstraint("welcome_seconds >= 0", name="chk_balance_welcome_non_neg"),
        CheckConstraint("referral_seconds >= 0", name="chk_balance_referral_non_neg"),
        CheckConstraint("purchased_seconds >= 0", name="chk_balance_purchased_non_neg"),
        CheckConstraint("consumed_seconds >= 0", name="chk_balance_consumed_non_neg"),
    )


class WelcomeBonusAward(Base):
    __tablename__ = "welcome_bonus_awards"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=300)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )


class MinuteLedgerEvent(Base):
    __tablename__ = "minute_ledger_events"

    id: Mapped[int] = mapped_column(PK_INT, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reference_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "source IN ('MONTHLY_FREE', 'WELCOME', 'REFERRAL', 'PURCHASE', 'ADJUST', 'CONSUME')",
            name="chk_ledger_source",
        ),
        CheckConstraint(
            "direction IN ('CREDIT', 'DEBIT')",
            name="chk_ledger_direction",
        ),
        CheckConstraint("seconds > 0", name="chk_ledger_seconds_positive"),
    )


class AdjustmentIdempotencyRecord(Base):
    __tablename__ = "adjustment_idempotency_records"

    idempotency_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    admin_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    seconds: Mapped[int] = mapped_column(BigInteger, nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )


class Referral(Base):
    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(PK_INT, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    referred_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    referral_code: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )
    rewarded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint("referrer_id != referred_id", name="chk_referral_no_self"),
        CheckConstraint("status IN ('PENDING', 'REWARDED')", name="chk_referral_status"),
    )


class ReferralReward(Base):
    __tablename__ = "referral_rewards"

    id: Mapped[int] = mapped_column(PK_INT, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    referral_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("referrals.id", ondelete="CASCADE"), nullable=False
    )
    reward_index: Mapped[int] = mapped_column(Integer, nullable=False)  # 1 to 20
    seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=30)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("referrer_id", "reward_index", name="uq_referral_reward_user_index"),
        CheckConstraint(
            "reward_index >= 1 AND reward_index <= 20",
            name="chk_referral_reward_index_range",
        ),
        CheckConstraint("seconds > 0", name="chk_referral_reward_seconds_pos"),
    )


class PricingPlan(Base):
    __tablename__ = "pricing_plans"

    id: Mapped[int] = mapped_column(PK_INT, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    price_mmk: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PaymentRequest(Base):
    __tablename__ = "payment_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    package_code: Mapped[str] = mapped_column(String(50), nullable=False)
    minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_mmk: Mapped[int] = mapped_column(Integer, nullable=False)
    receipt_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PaymentStatus.PENDING.value
    )
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')",
            name="chk_payment_status",
        ),
    )


class VoiceModel(Base):
    __tablename__ = "voice_models"

    id: Mapped[int] = mapped_column(PK_INT, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(
        String(20), nullable=False, default=VoiceModelType.SYSTEM.value
    )
    owner_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    model_dir: Mapped[str] = mapped_column(Text, nullable=False)
    pth_path: Mapped[str] = mapped_column(Text, nullable=False)
    index_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    sample_audio_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    gender: Mapped[str] = mapped_column(String(20), nullable=False, default="unspecified")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=VoiceModelStatus.READY.value
    )
    is_default_free: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allowed_for_free: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allowed_for_purchased: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        onupdate=utc_now,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('DISCOVERED', 'READY', 'ACTIVE', 'DISABLED', 'BROKEN')",
            name="chk_voice_model_status",
        ),
        CheckConstraint("type IN ('SYSTEM', 'PERSONAL')", name="chk_voice_model_type"),
    )


class CustomVoiceRequest(Base):
    __tablename__ = "custom_voice_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: f"CVR-{uuid.uuid4().hex[:8].upper()}")
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    model_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sample_file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    sample_file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sample_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CustomVoiceRequestStatus.PENDING.value
    )
    assigned_model_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("voice_models.id", ondelete="SET NULL"), nullable=True
    )
    admin_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        onupdate=utc_now,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')",
            name="chk_custom_voice_request_status",
        ),
    )


class UserVoiceModelGrant(Base):
    __tablename__ = "user_voice_model_grants"

    id: Mapped[int] = mapped_column(PK_INT, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    model_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("voice_models.id", ondelete="CASCADE"), index=True, nullable=False
    )
    granted_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "model_id", name="uq_user_voice_model_grant"),
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=JobStatus.PENDING.value
    )
    job_type: Mapped[str] = mapped_column(
        String(30), nullable=False, default=JobType.VOICE_CLONE.value
    )
    input_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    edge_voice: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    gender_preset: Mapped[str | None] = mapped_column(String(20), nullable=True)  # m2m, m2f, f2f, f2m
    speed_preset: Mapped[str | None] = mapped_column(String(20), nullable=True)   # slow, normal, fast
    seconds_estimated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    seconds_consumed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    result_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_audio_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'QUEUED', 'RUNNING', 'SUCCESS', 'FAILED', 'CANCELLED')",
            name="chk_job_status",
        ),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(PK_INT, primary_key=True, autoincrement=True)
    admin_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[str] = mapped_column(String(100), nullable=False)
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )


class BotSetting(Base):
    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        onupdate=utc_now,
    )


class BroadcastCampaign(Base):
    __tablename__ = "broadcast_campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    admin_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    confirmation_token: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BroadcastCampaignStatus.PENDING.value
    )
    total_recipients: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    deliveries: Mapped[list[BroadcastDelivery]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )


class BroadcastDelivery(Base):
    __tablename__ = "broadcast_deliveries"

    id: Mapped[int] = mapped_column(PK_INT, primary_key=True, autoincrement=True)
    campaign_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("broadcast_campaigns.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, index=True, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=BroadcastDeliveryStatus.PENDING.value
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    campaign: Mapped[BroadcastCampaign] = relationship(back_populates="deliveries")

    __table_args__ = (
        UniqueConstraint("campaign_id", "user_id", name="uq_broadcast_delivery_campaign_user"),
    )


class DatabaseMetadata(Base):
    __tablename__ = "database_metadata"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        server_default=func.now(),
        onupdate=utc_now,
    )


class ImportedAudience(Base):
    __tablename__ = "imported_audience"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_batch: Mapped[str] = mapped_column(
        String(100), nullable=False, default="legacy_migration_v1"
    )
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )
    is_registered_in_new_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    broadcast_eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class MonthlyFreeAllowanceGrant(Base):
    __tablename__ = "monthly_free_allowance_grants"

    id: Mapped[int] = mapped_column(PK_INT, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)  # Format: "YYYY-MM"
    seconds: Mapped[int] = mapped_column(BigInteger, nullable=False, default=180)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("user_id", "period", name="uq_user_monthly_grant_period"),
    )

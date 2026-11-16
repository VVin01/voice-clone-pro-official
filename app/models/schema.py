from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
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

    recap_credits: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    channel_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    group_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    welcome_bonus_awarded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    # True when Telegram reports the user has blocked the bot
    # (TelegramForbiddenError on a send). This is NOT an admin ban:
    # status stays untouched, broadcasts skip flagged users, and the
    # flag clears on the user's next successful inbound interaction.
    # Admin bans are expressed ONLY via status == BLOCKED.
    bot_blocked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # Batch BC-1: admin-set test-account marker. Payments from flagged
    # users are excluded from the Admin income totals (read-side filter
    # only — payment records are never altered) and can be surfaced in
    # the income detail view badged TEST. Set/unset from the Admin user
    # detail view; every change is audit-logged. No user IDs are ever
    # hardcoded — the flag is the only signal.
    is_test_account: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # Batch CG: the user's OWN Gemini API key (bring-your-own-key).
    # The value is a secret: it is never logged, never echoed back to
    # any client (API/UI expose only •••• + last 4 via
    # app.services.gemini_key_service.masked_key_view), never written
    # to an audit row, and is sent ONLY to Google's API for this
    # user's own Gemini TTS calls. It serves this user alone — never
    # pooled, never shared (owner decision 2026-11-07 14:27).
    # gemini_key_state: "" (no key) / "ok" (validated, working) /
    # "invalid" (Google rejected it as an auth-class failure; routing
    # skips it until the user saves a fresh one).
    gemini_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    gemini_key_state: Mapped[str] = mapped_column(
        String(10), nullable=False, default=""
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
    def clone_spendable_seconds(self) -> int:
        """Seconds a VOICE CLONE job may spend (Batch AW re-scope).

        Referral-earned seconds are TTS-only since Batch AW: they are
        excluded here, and every clone balance check / deduction uses
        this figure instead of the grand total.
        """
        return max(
            0,
            self.monthly_free_seconds
            + self.purchased_seconds
            + self.welcome_seconds,
        )

    @property
    def clone_spendable_minutes(self) -> float:
        return self.clone_spendable_seconds / 60.0

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


class RecapCreditLedgerEvent(Base):
    """Audit trail for recap-credit movements (Batch O, billing option C).

    Mirrors MinuteLedgerEvent's conventions for the recap-credit
    currency (whole credits on users.recap_credits, which the minute
    ledger cannot represent): a DEBIT row is written in the same
    transaction as every charge (submit + segment top-up) and a
    CREDIT row — reference_id ``REFUND:<job_id>``, the voice path's
    refund convention — in the same transaction as every refund, so
    the refund row doubles as a durable, checkable refund record.
    """

    __tablename__ = "recap_credit_ledger_events"

    id: Mapped[int] = mapped_column(PK_INT, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    credits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reference_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "source IN ('RECAP')",
            name="chk_recap_ledger_source",
        ),
        CheckConstraint(
            "direction IN ('CREDIT', 'DEBIT')",
            name="chk_recap_ledger_direction",
        ),
        CheckConstraint("credits > 0", name="chk_recap_ledger_credits_positive"),
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
    slip_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PaymentStatus.PENDING.value
    )
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Batch I (payment admin review): every admin receives a Telegram
    # copy of the review message; the copies are stored here as JSON
    # [{"chat_id", "message_id", "kind", "text"}] so a terminal action
    # by ANY admin (Telegram or Mini App) can clear the keyboards and
    # append a status line on ALL copies, not just the actor's own.
    admin_notify_copies: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Edit-and-approve claim: while one admin edits the credit amount,
    # the request is claimed by them; other admins' review keyboards
    # are cleared and their taps answer "already being handled".
    review_claimed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    review_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Edit-and-approve audit: the minutes BEFORE the admin's correction
    # and the admin who made it (the corrected value lands in `minutes`
    # and is what the approval credits).
    original_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    edited_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Batch AJ (H1): durable identity of the payment slip itself —
    # "tg:<telegram file_id>" for bot submissions, "sha256:<hex of the
    # image bytes>" for Mini App uploads. One slip = one credit,
    # globally: creation refuses a fingerprint that any live (PENDING)
    # or APPROVED request already carries, and a defensive partial
    # unique index (app.db.session.ensure_batch_aj_unique_indexes)
    # backstops the check against races. The fingerprint survives the
    # slip file's deletion at approval, which is the point: replay
    # detection must not depend on the image still being on disk.
    slip_fingerprint: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Batch AJ (M4): edit-and-approve may also correct the plan's recap
    # credits; when set, approval grants THIS value instead of the
    # catalogue amount (the recap twin of edited minutes).
    edited_recap_credits: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'APPROVED', 'REJECTED', 'CANCELLED')",
            name="chk_payment_status",
        ),
    )


class PaymentIntent(Base):
    """A buy-flow package selection that survives a bot restart (B2).

    The aiogram FSM is in-memory: a deploy/restart between tapping a
    package and sending the slip photo silently lost the flow — the
    photo arrived with no state and met silence. The selection is
    persisted here at tap time; a photo/document arriving with no
    live FSM state but a recent (<24h) intent resumes the slip flow
    from this row. One row per user (re-selection overwrites); the
    row is cleared when a slip is created or the buy menu is
    re-entered (flow abandoned).
    """

    __tablename__ = "payment_intents"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    package_code: Mapped[str] = mapped_column(String(50), nullable=False)
    chosen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now(), nullable=False
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
    engine: Mapped[str] = mapped_column(String(20), nullable=False, default="voxcpm")  # voxcpm only in Voice v2; legacy rows may still carry an old value
    preset_voice: Mapped[str | None] = mapped_column(String(50), nullable=True)        # komin, kothura, etc.
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
    # Batch BU: True when the job's clone reference exceeded the
    # VoxCPM Space 50 s cap and the engine cloned from its first 45 s
    # instead. The persisted honesty flag behind the delivery note and
    # the Mini App result/history trim note — the same role
    # engine == "edge_fallback" plays for the fallback disclosure.
    reference_trimmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Batch CE: True when a Gemini TTS-tab job hit an exhaustion-class
    # Gemini failure and was re-synthesised WHOLE on the Edge twin
    # voice. The persisted honesty flag behind the delivery caption's
    # used-engine truth + fallback note — the same role
    # reference_trimmed plays for the BU trim disclosure. The row's
    # engine / preset_voice keep the PICKED values, so app/History
    # keep showing the picked name (settled BK split).
    tts_fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Batch CG: how a Gemini-picked job was actually routed —
    # "own_key" (the user's own key served it), "pool" (the service
    # pool), "own_pool_mix" (both), "edge_taste" (FREE taste spent:
    # the whole job went to Edge before synthesis), "edge_exhausted"
    # (Batch CE pool-exhaustion fallback). NULL for pre-CG rows and
    # non-Gemini jobs. Delivery-caption truth + CE-watch observability
    # only; History keeps the picked engine (BK split, unchanged).
    tts_route: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # Batch CG: True when the user's saved Gemini key was rejected
    # as invalid mid-job (the job continued on the pool/Edge — a bad
    # key never fails a job). Drives the plain-copy caption note that
    # tells the user their key stopped working.
    gemini_key_broken: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Batch BV: for RECAP jobs, the resolved server path of the
    # user's own-voice narration sample (uploaded via
    # /api/recap/upload_voice_ref). Nullable: only own-voice recap
    # jobs carry one. NOT source_audio_path — on recap rows that
    # column holds the source VIDEO (URL or upload path).
    recap_ref_audio_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # JSON blob for recap jobs: the full submit-time RecapConfigRequest
    # plus credit state (credits_charged / credit_deducted / phase /
    # is_vip), so a restart/deploy re-dispatch restores the job exactly
    # instead of rebuilding it with default settings. Nullable: rows
    # created before this column existed simply have no persisted state.
    recap_config: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Batch DP: park-and-ask state (JSON) — written when a paid job
    # parks on VoxCPM quota exhaustion: the owner's choice
    # (none/wait/edge_all/edge_mix), the prompt message id and the
    # park timestamp. The DB column is the resumer's one source of
    # truth; NULL for every job that never parked.
    voice_choice_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Second Worker Phase 2 (2026-11-16): final narration script text
    # persisted on the job row upon completion so AI Thumbnail v2 and support
    # lookups can read it regardless of which worker executed the render.
    final_script: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'QUEUED', 'RUNNING', 'WAITING_CHOICE', 'SUCCESS', 'FAILED', 'CANCELLED')",
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


class GroupAiEscalation(Base):
    """A group-AI question an admin must approve before it is answered.

    Created when the group AI escalates (AI ESCALATE outcome or the
    sensitive-topic hard rule): the question text is exactly what the
    admins already receive by DM, plus one AI-drafted suggested answer.
    An admin's [Post answer] tap flips the row out of PENDING
    (first tap wins, enforced by a conditional UPDATE); [Dismiss]
    closes it without posting. Rows left PENDING for 24h are expired
    by the periodic sweep. No other group message is ever persisted.
    """

    __tablename__ = "group_ai_escalations"

    id: Mapped[int] = mapped_column(PK_INT, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    author_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    author_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    author_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(20), nullable=True)
    draft_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The AI's original draft, kept for audit when an admin edits the
    # draft before approval (draft_text then holds the edited text,
    # which is also what gets posted and FAQ-cached).
    original_draft_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    # While status == 'EDITING', handled_by is the admin who claimed
    # the record for editing; on APPROVED/DISMISSED it is the admin
    # whose tap closed the record.
    handled_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The escalation DM as delivered: one identical body per admin,
    # stored verbatim (dm_text) with every copy's [chat_id,
    # message_id] (dm_copies, JSON) so state changes can update ALL
    # admin copies — append a status line and/or clear the keyboard.
    dm_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    dm_copies: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING', 'EDITING', 'APPROVED', 'DISMISSED', 'EXPIRED')",
            name="chk_group_ai_escalation_status",
        ),
    )


class GroupAiFaq(Base):
    """An admin-approved (question, answer) pair for the group AI.

    Rows enter ONLY when an admin taps [Post answer] on an escalation
    draft — never from raw AI replies — so every cached answer is one
    a human admin has already approved and seen posted in the group.
    question_norm is the normalised form used for deterministic
    matching (see app/services/group_ai_admin.py).
    """

    __tablename__ = "group_ai_faqs"

    id: Mapped[int] = mapped_column(PK_INT, primary_key=True, autoincrement=True)
    question_norm: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    answer_text: Mapped[str] = mapped_column(Text, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )


class PrivateAiMessage(Base):
    """One recorded private-chat AI exchange row (Batch CD, 2026-11-07).

    Owner decision (2026-11-07 11:58): private (1:1) chats handled by
    the private-AI chain are recorded so admins can read the
    conversation in the Mini App user-detail screen. Two rows per
    exchange — the user's text (direction 'user', kind 'user') and
    the bot's sent reply (direction 'ai', kind 'fast_path' /
    'ai_reply' / 'escalation'). Messages consumed by other chains
    (commands, FSM flows, the payment-intent hint) are NEVER recorded,
    and GROUP messages are never recorded anywhere (the standing
    group storage ban is untouched by this table). Rows older than
    30 days are pruned by the periodic sweep (see
    app/services/private_ai.py). Created via
    Base.metadata.create_all like the other Batch H tables — no
    migration framework.
    """

    __tablename__ = "private_ai_messages"

    id: Mapped[int] = mapped_column(PK_INT, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "direction IN ('user', 'ai')",
            name="chk_private_ai_message_direction",
        ),
        CheckConstraint(
            "kind IN ('user', 'fast_path', 'ai_reply', 'escalation')",
            name="chk_private_ai_message_kind",
        ),
    )


class GeminiPoolUsage(Base):
    """One row per (UTC day, user) of SUCCESSFUL service-pool Gemini
    TTS draws (Batch CG, 2026-11-07).

    The accounting behind the FREE Gemini taste (delivered pool
    seconds per user per day) and the global FREE daily budget
    (pool requests across FREE users per day). Rows are written
    ONLY after a job actually delivered audio the pool produced —
    failed attempts consume nothing, mirroring the
    tts_cap_consumed_seconds rule that FAILED/CANCELLED count zero.
    ``tier`` is the user's tier AT DEBIT TIME so the global FREE
    aggregate stays honest if an account upgrades mid-day. Only
    FREE rows are ever written (PURCHASED pool use is not
    taste-governed); admins are exempt and never recorded.
    Created via Base.metadata.create_all (Batch H pattern).
    """

    __tablename__ = "gemini_pool_usage"

    id: Mapped[int] = mapped_column(PK_INT, primary_key=True, autoincrement=True)
    day: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    tier: Mapped[str] = mapped_column(String(20), nullable=False, default="FREE")
    pool_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    pool_requests: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("day", "user_id", name="uq_gemini_pool_usage_day_user"),
    )


class UserPreference(Base):
    """Account-bound Mini App preferences (Batch AC, 2026-11-04).

    One row per user holding a JSON blob of PREFERENCE values only
    (voice choice, recap style/subtitle/blur-strength defaults — see
    USER_PREF_FIELDS in app/webapp/api.py for the exact allowlist).
    Per-source geometry (a recap job's blur_y/blur_h box and the
    blur_box_explicit marker) is deliberately NOT stored here: it
    belongs to one source video, not to the account. The table is
    created at startup via Base.metadata.create_all, like the
    Batch H group-AI tables — no migration framework.
    """

    __tablename__ = "user_preferences"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    prefs_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, server_default=func.now()
    )


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


class RecapThumbnailPoolUsage(Base):
    """Per-calendar-month (UTC 'YYYY-MM') counter for AI Thumbnail generations.

    Tracks total billed generations, estimated USD spend, and whether HF
    returned an insufficient-credit / quota error for that month so the
    feature hard-stops until the 1st of next month. Created via
    Base.metadata.create_all (Batch H pattern).
    """

    __tablename__ = "recap_thumbnail_pool_usage"

    period: Mapped[str] = mapped_column(String(7), primary_key=True)  # Format: "YYYY-MM"
    generations_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_spend_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    exhausted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, server_default=func.now()
    )


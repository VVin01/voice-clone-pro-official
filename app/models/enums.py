from __future__ import annotations
from enum import Enum


class UserStatus(str, Enum):
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"


class AccountCategory(str, Enum):
    FREE = "FREE"
    PURCHASED = "PURCHASED"


class LedgerSource(str, Enum):
    WELCOME = "WELCOME"
    REFERRAL = "REFERRAL"
    PURCHASE = "PURCHASE"
    ADJUST = "ADJUST"


class LedgerDirection(str, Enum):
    CREDIT = "CREDIT"
    DEBIT = "DEBIT"


class VoiceModelType(str, Enum):
    SYSTEM = "SYSTEM"
    PERSONAL = "PERSONAL"


class VoiceModelStatus(str, Enum):
    DISCOVERED = "DISCOVERED"
    READY = "READY"
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    BROKEN = "BROKEN"


class PaymentStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class JobStatus(str, Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobType(str, Enum):
    VOICE_CLONE = "VOICE_CLONE"


class BroadcastCampaignStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class BroadcastDeliveryStatus(str, Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"

"""Batch CG — bring-your-own Gemini key + FREE Gemini taste accounting.

Owner design (2026-11-07 14:02–14:27, spec batch-cg): Gemini TTS is
routed **user's own key → service pool → Edge**. A user's key serves
ONLY that user — never pooled, never shared, never logged, never
echoed back (clients see •••• + last 4 only). FREE users get a small
pool-funded "taste" (``free_gemini_taste_minutes`` of delivered
Gemini audio per day) bounded additionally by a global FREE daily
request budget (``free_gemini_daily_budget_requests``); PURCHASED
pool use is not taste-governed; admins are exempt from both.

Accounting lives in the ``gemini_pool_usage`` table and counts
SUCCESSFUL pool draws only — a debit happens after a job delivered
audio the pool actually produced, so failed attempts consume
nothing (the tts_cap_consumed_seconds rule, applied to the pool).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.models.enums import AccountCategory
from app.models.schema import GeminiPoolUsage, User

logger = logging.getLogger(__name__)

KEY_STATE_OK = "ok"
KEY_STATE_INVALID = "invalid"

# The one minimal real-shaped validation utterance (see
# validate_gemini_key): a single short Burmese greeting through the
# engine's own client path — one generate call, nothing stored.
_VALIDATION_TEXT = "မင်္ဂလာပါ။"
_VALIDATION_VOICE = "Charon"


def _utc_day():
    return datetime.now(timezone.utc).date()


# ── BYOK Encryption at Rest (Fernet) ─────────────────────────────────
_ENC_PREFIX = "enc:v1:"


def _byok_fernet():
    from cryptography.fernet import Fernet

    raw_secret = (
        os.getenv("BYOK_ENCRYPTION_KEY")
        or getattr(settings, "bot_token", "")
        or getattr(settings, "expected_db_identity", "")
        or "voice-clone-pro-byok-key"
    )
    digest = hashlib.sha256(f"byok-v1:{raw_secret}".encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_byok_secret(value: str | None) -> str | None:
    """Encrypt a plaintext BYOK secret for database storage; idempotent."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s.startswith(_ENC_PREFIX):
        return s
    try:
        token = _byok_fernet().encrypt(s.encode("utf-8")).decode("ascii")
        return f"{_ENC_PREFIX}{token}"
    except Exception:
        return s


def decrypt_byok_secret(value: str | None) -> str | None:
    """Decrypt stored BYOK ciphertext, passing legacy plaintext values through."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if not s.startswith(_ENC_PREFIX):
        return s
    token = s[len(_ENC_PREFIX):]
    try:
        return _byok_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except Exception:
        # Fallback: if key material changed cleanly return None rather than ciphertext
        return None


# ── Storage ──────────────────────────────────────────────────────────

def masked_key_view(user: User) -> dict:
    """The ONLY client-facing shape of a saved key: presence, state,
    and •••• + last 4. The value itself never leaves the server."""
    key = (decrypt_byok_secret(getattr(user, "gemini_api_key", None)) or "").strip()
    if not key:
        return {"has_key": False, "masked": None, "state": None}
    return {
        "has_key": True,
        "masked": f"••••{key[-4:]}",
        "state": getattr(user, "gemini_key_state", "") or KEY_STATE_OK,
    }


async def save_user_key(session: AsyncSession, user: User, key: str) -> None:
    """Store a VALIDATED key (validation happens in validate_gemini_key
    before this is called — an invalid key is never stored)."""
    user.gemini_api_key = key.strip()
    user.gemini_key_state = KEY_STATE_OK
    await session.flush()


async def delete_user_key(session: AsyncSession, user: User) -> None:
    """One-tap delete: the row's key material is cleared immediately."""
    user.gemini_api_key = None
    user.gemini_key_state = ""
    await session.flush()


async def mark_user_key_invalid(session: AsyncSession, user_id: int) -> None:
    """Record that Google rejected the saved key as invalid (mid-job
    auth-class failure). The key material stays (the user sees the
    invalid state and can replace or delete it) but routing skips it
    until a fresh key validates."""
    user = (
        await session.execute(select(User).where(User.id == user_id))
    ).scalar_one_or_none()
    if user is not None and (user.gemini_api_key or "").strip():
        user.gemini_key_state = KEY_STATE_INVALID
        await session.flush()
        logger.info("Gemini key marked invalid for user %s", user_id)


# ── Validation ───────────────────────────────────────────────────────

def validate_gemini_key(candidate: str) -> tuple[bool, str]:
    """Validate a candidate key with exactly ONE minimal real TTS
    call through the engine's own client path (GeminiTTSEngine).
    Returns (accepted, reason): reason "ok" (works), "spent"
    (genuine but out of quota today — still accepted and stored:
    it will serve tomorrow, and routing treats a spent key as a
    pool step, never a failure), "invalid" (auth-class rejection —
    never stored), "unverifiable" (network/other — never stored).
    Blocking: callers run it via asyncio.to_thread. The candidate
    is never logged."""
    from app.services.recap_engine.gemini_tts_engine import (
        GeminiTTSEngine,
        GeminiTTSError,
        classify_gemini_key_failure,
    )

    candidate = (candidate or "").strip()
    if not candidate or any(ch.isspace() for ch in candidate):
        return False, "invalid"
    work_dir = (
        Path(settings.storage_root) / "gemini_key_check" / uuid.uuid4().hex
    )
    engine = None
    try:
        engine = GeminiTTSEngine(
            ffmpeg=shutil.which("ffmpeg") or "ffmpeg",
            work_dir=work_dir,
            keys=[candidate],
            # Batch DN: the candidate is a user's own key — validating
            # it must not write into the shared pool usage ledger.
            record_usage=False,
            log=lambda msg: None,
        )
        engine.generate_channel99_voice(_VALIDATION_TEXT, _VALIDATION_VOICE)
        return True, "ok"
    except GeminiTTSError as exc:
        cls = classify_gemini_key_failure(engine, exc) if engine is not None else "error"
        if cls == "spent":
            return True, "spent"
        if cls == "invalid":
            return False, "invalid"
        return False, "unverifiable"
    except Exception:
        return False, "unverifiable"
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ── Taste / budget accounting ────────────────────────────────────────

def taste_limit_seconds(tier: str = AccountCategory.FREE.value) -> float:
    if tier == AccountCategory.PURCHASED.value:
        return paid_taste_limit_seconds()
    return max(0, int(getattr(settings, "free_gemini_taste_minutes", 2) or 0)) * 60.0


def paid_taste_limit_seconds() -> float:
    return max(0, int(getattr(settings, "paid_gemini_taste_minutes", 10) or 0)) * 60.0


def free_budget_limit_requests() -> int:
    return max(0, int(getattr(settings, "free_gemini_daily_budget_requests", 100) or 0))


async def taste_used_seconds(session: AsyncSession, user_id: int) -> float:
    row = (
        await session.execute(
            select(GeminiPoolUsage).where(
                GeminiPoolUsage.day == _utc_day(),
                GeminiPoolUsage.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    return float(row.pool_seconds or 0.0) if row is not None else 0.0


async def free_budget_used_requests(session: AsyncSession) -> int:
    # Summed in Python, not SQL — the codebase keeps aggregates out
    # of SQL (D1 sweep); the row set is one row per FREE user who
    # drew from the pool today, which stays small.
    rows = (
        await session.execute(
            select(GeminiPoolUsage.pool_requests).where(
                GeminiPoolUsage.day == _utc_day(),
                GeminiPoolUsage.tier == AccountCategory.FREE.value,
            )
        )
    ).scalars().all()
    return int(sum(int(r or 0) for r in rows))


async def debit_pool_usage(
    session: AsyncSession,
    user_id: int,
    *,
    tier: str,
    seconds: float,
    requests: int,
) -> None:
    """Record SUCCESSFUL pool draws for FREE and PURCHASED users.

    Admins are exempt (never written). Global FREE request budget queries
    filter by ``tier == FREE``, so PURCHASED rows do not count against
    the global FREE budget. Zero-draw calls are no-ops.
    """
    if settings.is_admin(user_id):
        return
    if tier not in (AccountCategory.FREE.value, AccountCategory.PURCHASED.value):
        return
    if seconds <= 0 and requests <= 0:
        return
    day = _utc_day()
    row = (
        await session.execute(
            select(GeminiPoolUsage).where(
                GeminiPoolUsage.day == day,
                GeminiPoolUsage.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = GeminiPoolUsage(day=day, user_id=user_id, tier=tier)
        session.add(row)
    else:
        row.tier = tier
    row.pool_seconds = float(row.pool_seconds or 0.0) + float(seconds)
    row.pool_requests = int(row.pool_requests or 0) + int(requests)
    await session.flush()


# ── Route resolution ─────────────────────────────────────────────────

@dataclass
class GeminiRouteContext:
    """Everything a Gemini TTS call site needs to route one user's
    request. Holds the raw key in memory ONLY (repr-disabled, never
    logged, never serialised into job rows, checkpoints or
    overlays)."""

    user_id: int
    tier: str
    is_admin: bool
    own_key: str | None = field(default=None, repr=False)
    own_key_state: str = ""
    taste_limit_seconds: float = 0.0
    taste_used_seconds: float = 0.0
    budget_limit_requests: int = 0
    budget_used_requests: int = 0

    @property
    def has_usable_own_key(self) -> bool:
        return bool(self.own_key) and self.own_key_state != KEY_STATE_INVALID

    @property
    def pool_allowed(self) -> bool:
        if self.is_admin:
            return True
        if self.tier == AccountCategory.PURCHASED.value:
            return self.taste_used_seconds < self.taste_limit_seconds
        return (
            self.taste_used_seconds < self.taste_limit_seconds
            and self.budget_used_requests < self.budget_limit_requests
        )

    @property
    def taste_blocked(self) -> bool:
        """Non-admin, no usable own key, and pool taste/budget closed."""
        return (
            not self.is_admin
            and not self.has_usable_own_key
            and not self.pool_allowed
        )

    @property
    def taste_remaining_seconds(self) -> float | None:
        if self.is_admin:
            return None
        return max(0.0, self.taste_limit_seconds - self.taste_used_seconds)


async def resolve_gemini_route(
    session: AsyncSession, user: User, *, is_admin: bool
) -> GeminiRouteContext:
    tier = (
        AccountCategory.PURCHASED.value
        if user.account_category == AccountCategory.PURCHASED.value
        else AccountCategory.FREE.value
    )
    ctx = GeminiRouteContext(
        user_id=user.id,
        tier=tier,
        is_admin=is_admin,
        own_key=(user.gemini_api_key or "").strip() or None,
        own_key_state=getattr(user, "gemini_key_state", "") or "",
        taste_limit_seconds=taste_limit_seconds(tier),
        budget_limit_requests=free_budget_limit_requests(),
    )
    if not is_admin:
        ctx.taste_used_seconds = await taste_used_seconds(session, user.id)
        if tier == AccountCategory.FREE.value:
            ctx.budget_used_requests = await free_budget_used_requests(session)
    return ctx


def log_route(route: str, user_id: int, tier: str) -> None:
    """The structured per-decision route line (spec marker). Carries
    ids and tier only — never key material."""
    logger.info("TTS route: %s user=%s tier=%s", route, user_id, tier)

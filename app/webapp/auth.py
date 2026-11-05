"""
auth.py - Telegram WebApp initData HMAC-SHA256 Cryptographic Verification.
Validates authentic Telegram users seamlessly without requiring login/passwords.
"""

from __future__ import annotations

import hmac
import hashlib
import json
import time
import urllib.parse
from typing import Optional, Dict, Any
from app.config import settings

# initData is a bearer credential: whoever holds the string can act as
# the user. Telegram support's own guidance is to reject stale
# auth_date values; without an age limit a leaked initData string
# (logs, screenshots, shared links) stays valid forever.
INIT_DATA_MAX_AGE_SECONDS = 24 * 60 * 60

# Batch AJ (M8): the future direction was unchecked — an auth_date
# far ahead of the server clock verified fine. A small skew is
# legitimate (Telegram/Render clock drift); anything beyond it is
# not a real client.
INIT_DATA_FUTURE_SKEW_SECONDS = 300

# Batch AJ (M8): admin actions demand fresher initData than user
# reads. The 24 h bearer window is a usability trade-off for normal
# Mini App use; an admin session that old is a leaked-session risk,
# and reopening the Mini App mints fresh initData anyway.
ADMIN_INIT_DATA_MAX_AGE_SECONDS = 12 * 60 * 60


def init_data_auth_date(init_data: str) -> Optional[int]:
    """The auth_date carried by an initData string, or None.

    UNVERIFIED parse — only meaningful on a string that has already
    passed parse_and_verify_init_data (the value is HMAC-covered, so
    once the signature checks out, the date is trustworthy).
    """
    if not init_data:
        return None
    try:
        parsed = urllib.parse.parse_qs(init_data, keep_blank_values=True)
        raw = parsed.get("auth_date", [None])[0]
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def parse_and_verify_init_data(init_data: str, bot_token: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Cryptographically verify Telegram WebApp initData string using Bot Token.
    Returns parsed user dict if valid, or None if invalid/tampered.
    """
    token = bot_token or settings.bot_token
    if not init_data:
        return None

    # Batch AJ (H10): fail closed on an empty token. With no bot
    # token configured, the HMAC secret is derivable by anyone
    # (HMAC-SHA256 of the public "WebAppData" label under an empty
    # key), so initData for ANY user — admins included — would be
    # forgeable. The webhook and stream-token paths already fail
    # closed the same way; auth now matches them.
    if not token:
        return None

    try:
        parsed_params = urllib.parse.parse_qs(init_data, keep_blank_values=True)
        data_dict = {k: v[0] for k, v in parsed_params.items()}

        received_hash = data_dict.pop("hash", None)
        if not received_hash:
            return None

        # Build data-check-string (keys sorted alphabetically, joined with \n)
        check_items = [f"{k}={v}" for k, v in sorted(data_dict.items())]
        data_check_string = "\n".join(check_items)

        # 1. secret_key = HMAC_SHA256("WebAppData", bot_token)
        secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()

        # 2. calculated_hash = HMAC_SHA256(secret_key, data_check_string)
        calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        # Freshness check (security fix 2026-10-31): auth_date is part
        # of the HMAC-signed payload, so now that the signature has
        # verified it can be trusted. Reject initData that carries no
        # auth_date or one older than INIT_DATA_MAX_AGE_SECONDS.
        auth_date_raw = data_dict.get("auth_date")
        if not auth_date_raw:
            return None
        try:
            auth_date = int(auth_date_raw)
        except (TypeError, ValueError):
            return None
        if time.time() - auth_date > INIT_DATA_MAX_AGE_SECONDS:
            return None
        # Batch AJ (M8): reject auth_dates implausibly far in the
        # FUTURE too (beyond a small clock-skew allowance) — the
        # stale-only check left a pre-minting window wide open.
        if auth_date - time.time() > INIT_DATA_FUTURE_SKEW_SECONDS:
            return None

        # Extract authenticated user information. Batch AJ (L9): a
        # validly-signed payload WITHOUT a user field used to fall
        # through and return the raw params dict, which callers then
        # indexed ["id"] on — a 500 instead of a clean rejection.
        # No user field means no authenticated user: reject.
        user_str = data_dict.get("user")
        if not user_str:
            return None
        return json.loads(user_str)

    except Exception:
        return None

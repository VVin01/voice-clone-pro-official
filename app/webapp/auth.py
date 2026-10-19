"""
auth.py - Telegram WebApp initData HMAC-SHA256 Cryptographic Verification.
Validates authentic Telegram users seamlessly without requiring login/passwords.
"""

from __future__ import annotations

import hmac
import hashlib
import json
import urllib.parse
from typing import Optional, Dict, Any
from app.config import settings


def parse_and_verify_init_data(init_data: str, bot_token: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Cryptographically verify Telegram WebApp initData string using Bot Token.
    Returns parsed user dict if valid, or None if invalid/tampered.
    """
    token = bot_token or settings.bot_token
    if not init_data:
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

        # Extract authenticated user information
        user_str = data_dict.get("user")
        if user_str:
            return json.loads(user_str)
        return data_dict

    except Exception:
        return None

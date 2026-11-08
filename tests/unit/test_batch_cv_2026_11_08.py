"""Batch CV — admin user detail shows BYOK Gemini key presence.

Ulfa's request (2026-11-08): from Admin → user info, he wants to see
whether a user has added their own Gemini key. Only presence + state
+ masked last4 may be exposed — never the key itself.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import settings
from app.models.schema import User

ROOT = Path(__file__).resolve().parents[2]
ADMIN_ID = 999000


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_cv_detail_exposes_key_presence_masked(db_session, monkeypatch):
    from app.webapp import api as api_mod

    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    admin = User(id=ADMIN_ID, first_name="Boss")
    with_key = User(
        id=700501, first_name="KeyUser",
    )
    with_key.gemini_api_key = "AIzaSyFAKE-KEY-VALUE-9xyz"
    with_key.gemini_key_state = "ok"
    no_key = User(id=700502, first_name="NoKey")
    bad_key = User(id=700503, first_name="BadKey")
    bad_key.gemini_api_key = "AIzaSyFAKE-DEAD-KEY-77ab"
    bad_key.gemini_key_state = "invalid"
    db_session.add_all([admin, with_key, no_key, bad_key])
    await db_session.commit()

    d1 = await api_mod.get_admin_user_detail(700501, admin, db_session)
    assert d1["gemini_key"] == {"has_key": True, "state": "ok", "last4": "9xyz"}
    # The full key must never appear anywhere in the payload.
    assert "AIzaSyFAKE-KEY-VALUE" not in str(d1)

    d2 = await api_mod.get_admin_user_detail(700502, admin, db_session)
    assert d2["gemini_key"] == {"has_key": False, "state": "", "last4": None}

    d3 = await api_mod.get_admin_user_detail(700503, admin, db_session)
    assert d3["gemini_key"]["has_key"] is True
    assert d3["gemini_key"]["state"] == "invalid"
    assert d3["gemini_key"]["last4"] == "77ab"


def test_cv_ui_wired():
    js = _read("app/webapp/static/js/app.js")
    html = _read("app/webapp/static/index.html")
    i18n = _read("app/webapp/static/js/i18n.js")
    assert 'id="adm-detail-gemini-key"' in html
    assert "gemini_key" in js and "adm-detail-gemini-key" in js
    for key in ("admin_gemini_key", "admin_gemini_key_none",
                "admin_gemini_key_ok", "admin_gemini_key_invalid"):
        assert i18n.count(key) >= 2, key  # my + en

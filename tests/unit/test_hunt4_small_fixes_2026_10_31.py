"""Hunt-#4 small fixes (branch fix-toast-start-2026-10-31).

B1  Admin settings saves are validated ALL-OR-NOTHING before any
    field is applied: tutorial links must be https://t.me/… links
    (name / name+post / +invite shapes — a malformed one previously
    broke the bot's main menu for every user, because Telegram
    rejects a message whose URL button is invalid) and the payment
    numbers must be non-empty (a blanked number leaves invoices with
    no account to pay into).
A6  Mini App slip submission stores the CATALOGUE price as
    amount_mmk — the same get_plan_by_code source the minutes
    already came from, mirroring the bot flow — never the
    client-typed amount, which is the number the admin is shown.
D1  The synchronous yt-dlp paths that ran on the single shared
    event loop (downloader extract, recap submit's duration
    pre-check, and the redirect-following unshorten calls in the
    same handlers) now go through asyncio.to_thread, exactly like
    the neighbouring download endpoints already did.
D2  A module-level semaphore caps concurrent recap executions at
    2 across every dispatch path; extra dispatched jobs wait with
    their registry entry still QUEUED — never failed or dropped.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select

import app.services.downloader as dl_mod
import app.services.url_safety as url_safety_mod
from app.config import settings
from app.models.schema import BotSetting, PaymentRequest, User
from app.services.ledger import get_or_create_user
from app.services.payments import get_plan_by_code
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
API_PY = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")

ADMIN_ID = 999101


def _admin() -> User:
    return User(id=ADMIN_ID, first_name="Admin", username=None,
                account_category="FREE", language_code="my")


async def _make_user(db_session, telegram_id, **fields):
    user, _ = await get_or_create_user(db_session, telegram_id=telegram_id)
    for key, value in fields.items():
        setattr(user, key, value)
    await db_session.commit()
    return user


# ── B1: settings validation helpers (shape matrix) ──────────────────

@pytest.mark.parametrize("bad", [
    "javascript:alert(1)",
    "data:text/html;base64,AAAA",
    "http://t.me/VoiceClonePro",          # http, not https
    "https://t.me.evil.com/VoiceClonePro",
    "https://t.me/",                      # no target at all
    "t.me/VoiceClonePro",                 # missing scheme
    "https://t.me/Voice Clone/28",        # space inside
    "https://youtube.com/watch?v=1",      # not a Telegram link
    "",
])
def test_settings_helper_rejects_non_telegram_links(bad):
    with pytest.raises(HTTPException) as exc:
        api_mod._validate_admin_settings_updates({"tutorial_voice_link": bad})
    assert exc.value.status_code == 400


@pytest.mark.parametrize("good", [
    "https://t.me/VoiceClonePro",           # name only
    "https://t.me/VoiceClonePro/28",        # name + post id
    "https://t.me/+AbC123xYz-987",          # invite hash
])
def test_settings_helper_accepts_telegram_link_shapes(good):
    out = api_mod._validate_admin_settings_updates({"tutorial_voice_link": good})
    assert out["tutorial_voice_link"] == good


def test_settings_helper_strips_link_whitespace():
    out = api_mod._validate_admin_settings_updates(
        {"tutorial_voice_link": "  https://t.me/VoiceClonePro/28  "})
    assert out["tutorial_voice_link"] == "https://t.me/VoiceClonePro/28"


@pytest.mark.parametrize("key", ["payment_kpay_number", "payment_wave_number"])
def test_settings_helper_rejects_empty_payment_number(key):
    with pytest.raises(HTTPException) as exc:
        api_mod._validate_admin_settings_updates({key: "   "})
    assert exc.value.status_code == 400


# ── B1: endpoint is all-or-nothing ──────────────────────────────────

@pytest.mark.asyncio
async def test_settings_bad_link_rejects_wholesale(db_session):
    admin = _admin()
    db_session.add(admin)
    await db_session.commit()
    keys = ("tutorial_voice_link", "tutorial_plans_link")
    originals = {k: getattr(settings, k) for k in keys}
    try:
        payload = api_mod.AdminSettingsPayload(
            tutorial_plans_link="https://t.me/VoiceClonePro/31",   # valid
            tutorial_voice_link="https://example.com/not-telegram",  # invalid
        )
        with pytest.raises(HTTPException) as exc:
            await api_mod.update_admin_settings(payload, admin, db_session)
        assert exc.value.status_code == 400
        # NOTHING was applied: no rows for either key, memory untouched —
        # not even the valid plans link.
        rows = (await db_session.execute(
            select(BotSetting).where(BotSetting.key.in_(keys)))).scalars().all()
        assert rows == []
        for k in keys:
            assert getattr(settings, k) == originals[k]
    finally:
        for k, v in originals.items():
            setattr(settings, k, v)


@pytest.mark.asyncio
async def test_settings_empty_payment_number_rejects_wholesale(db_session):
    admin = _admin()
    db_session.add(admin)
    await db_session.commit()
    keys = ("tutorial_voice_link", "payment_kpay_number")
    originals = {k: getattr(settings, k) for k in keys}
    try:
        payload = api_mod.AdminSettingsPayload(
            tutorial_voice_link="https://t.me/VoiceClonePro/29",  # valid
            payment_kpay_number="",                                # invalid
        )
        with pytest.raises(HTTPException) as exc:
            await api_mod.update_admin_settings(payload, admin, db_session)
        assert exc.value.status_code == 400
        rows = (await db_session.execute(
            select(BotSetting).where(BotSetting.key.in_(keys)))).scalars().all()
        assert rows == []
        for k in keys:
            assert getattr(settings, k) == originals[k]
    finally:
        for k, v in originals.items():
            setattr(settings, k, v)


@pytest.mark.asyncio
async def test_settings_valid_payload_applies_all_shapes(db_session):
    admin = _admin()
    db_session.add(admin)
    await db_session.commit()
    updates = {
        "tutorial_voice_link": "https://t.me/VoiceClonePro",
        "tutorial_recap_link": "https://t.me/VoiceClonePro/30",
        "tutorial_plans_link": "https://t.me/+AbC123xYz-987",
        "payment_kpay_number": "09123456789",
    }
    originals = {k: getattr(settings, k) for k in updates}
    try:
        payload = api_mod.AdminSettingsPayload(**updates)
        res = await api_mod.update_admin_settings(payload, admin, db_session)
        assert res["success"] is True
        rows = (await db_session.execute(
            select(BotSetting).where(BotSetting.key.in_(list(updates))))).scalars().all()
        stored = {r.key: r.value for r in rows}
        assert stored == updates
        for k, v in updates.items():
            assert getattr(settings, k) == v
    finally:
        for k, v in originals.items():
            setattr(settings, k, v)


# ── A6: slip amount comes from the catalogue ────────────────────────

class _FakeUpload:
    def __init__(self, filename="slip.png", content=b"\x89PNG-fake-bytes"):
        self.filename = filename
        self._content = content
        self._sent = False

    async def read(self, size=-1):
        if self._sent:
            return b""
        self._sent = True
        return self._content


@pytest.mark.asyncio
async def test_slip_amount_ignores_client_typed_value(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "bot_token", "")
    user = await _make_user(db_session, 880801)
    plan = get_plan_by_code("MIN_10")
    assert plan is not None

    res = await api_mod.submit_payment_slip(
        package_code="MIN_10",
        amount_mmk=1,  # the lie: client claims they paid 1 MMK
        slip_file=_FakeUpload(),
        user=user,
        db=db_session,
        _gate=user,
    )
    assert res["success"] is True
    req = (await db_session.execute(select(PaymentRequest))).scalar_one()
    # stored amount + minutes are BOTH the catalogue's, not the form's
    assert req.amount_mmk == plan["price_mmk"] == 8000
    assert req.minutes == plan["minutes"] == 10
    assert req.package_code == "MIN_10"


@pytest.mark.asyncio
async def test_slip_unknown_plan_still_errors(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "bot_token", "")
    user = await _make_user(db_session, 880802)
    with pytest.raises(HTTPException) as exc:
        await api_mod.submit_payment_slip(
            package_code="NO_SUCH_PLAN",
            amount_mmk=5000,
            slip_file=_FakeUpload(),
            user=user,
            db=db_session,
            _gate=user,
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == "Unknown plan code"
    rows = (await db_session.execute(select(PaymentRequest))).scalars().all()
    assert rows == []


# ── D1: blocking calls run off the event loop ───────────────────────

def test_to_thread_source_pins():
    # downloader extract: get_info + unshorten wrapped
    assert "info = await asyncio.to_thread(downloader.get_info, canonical_url)" in API_PY
    assert "canonical_url = await asyncio.to_thread(unshorten_url, url)" in API_PY
    # recap submit pre-check: get_info + unshorten wrapped
    assert "info = await asyncio.to_thread(MediaDownloader().get_info, can_url)" in API_PY
    # the old direct (loop-blocking) statements are gone
    assert "info = downloader.get_info(canonical_url)" not in API_PY
    assert "info = MediaDownloader().get_info(can_url)" not in API_PY
    assert "can_url = unshorten_url(extract_first_url(req.url) or req.url)" not in API_PY
    assert "canonical_url = unshorten_url(url)" not in API_PY


@pytest.mark.asyncio
async def test_extract_get_info_runs_off_event_loop(monkeypatch):
    loop_ident = threading.get_ident()
    seen = {}

    def fake_unshorten(u):
        seen["unshorten_ident"] = threading.get_ident()
        return u

    def fake_get_info(self, url):
        seen["get_info_ident"] = threading.get_ident()
        return {"duration": 125, "title": "Sample", "platform": "YouTube",
                "uploader": "Chan", "thumbnail": "", "width": 1920,
                "height": 1080, "has_subtitles": False}

    monkeypatch.setattr(dl_mod, "unshorten_url", fake_unshorten)
    monkeypatch.setattr(url_safety_mod, "validate_public_url", lambda u: u)
    monkeypatch.setattr(dl_mod.MediaDownloader, "get_info", fake_get_info)

    user = User(id=880811, first_name="T", username=None,
                account_category="FREE", language_code="my")
    req = api_mod.ExtractMediaRequest(url="https://www.youtube.com/watch?v=abc123")
    res = await api_mod.extract_media_info(req, user, user)

    assert res["success"] is True
    assert res["duration_seconds"] == 125
    assert seen["get_info_ident"] != loop_ident
    assert seen["unshorten_ident"] != loop_ident


@pytest.mark.asyncio
async def test_recap_precheck_get_info_runs_off_event_loop(db_session, monkeypatch):
    loop_ident = threading.get_ident()
    seen = {}

    def fake_unshorten(u):
        seen["unshorten_ident"] = threading.get_ident()
        return u

    def fake_get_info(self, url):
        seen["get_info_ident"] = threading.get_ident()
        return {"duration": 90}

    dispatched = []

    async def _fake_runner(job_id, req, user_id, chat_id, **kwargs):
        dispatched.append(job_id)

    monkeypatch.setattr(dl_mod, "unshorten_url", fake_unshorten)
    monkeypatch.setattr(url_safety_mod, "validate_public_url", lambda u: u)
    monkeypatch.setattr(dl_mod.MediaDownloader, "get_info", fake_get_info)
    monkeypatch.setattr(api_mod, "_run_recap_background_job", _fake_runner)
    try:
        user = await _make_user(
            db_session, 880812,
            account_category="FREE", recap_credits=1,
        )
        req = api_mod.RecapConfigRequest(
            url="https://www.youtube.com/watch?v=abc123",
            title="Test Movie",
            voice_slug="komin",
            duration_seconds=0,  # unknown → forces the yt-dlp pre-check
        )
        resp = await api_mod.create_recap_job(req, user=user, db=db_session, _gate=user)
        await asyncio.sleep(0)
        assert resp["success"] is True
        assert len(dispatched) == 1
        assert seen["get_info_ident"] != loop_ident
        assert seen["unshorten_ident"] != loop_ident
    finally:
        api_mod.RECAP_JOBS_REGISTRY.clear()


# ── D2: global recap execution cap ──────────────────────────────────

def test_recap_semaphore_source_pins():
    assert "RECAP_EXECUTION_SEMAPHORE = asyncio.Semaphore(2)" in API_PY
    assert "async with RECAP_EXECUTION_SEMAPHORE:" in API_PY


@pytest.mark.asyncio
async def test_recap_execution_cap_two_concurrent(monkeypatch):
    state = {"current": 0, "peak": 0, "finished": []}

    async def fake_execute(job_id, req, user_id, chat_id, **kwargs):
        state["current"] += 1
        state["peak"] = max(state["peak"], state["current"])
        await asyncio.sleep(0.1)
        state["current"] -= 1
        state["finished"].append(job_id)

    monkeypatch.setattr(api_mod, "_execute_recap_background_job", fake_execute)
    job_ids = [f"rc_cap_{i}" for i in range(3)]
    await asyncio.gather(*(
        api_mod._run_recap_background_job(jid, None, 1, 1) for jid in job_ids
    ))
    # at most 2 executions overlapped, and the third was NOT dropped —
    # it waited for a slot and ran to completion
    assert state["peak"] == 2
    assert sorted(state["finished"]) == job_ids

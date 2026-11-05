"""Batch AJ — adversarial weakness-hunt fixes: AUTH / PLATFORM cluster.

Findings covered: H10 (empty bot token must fail closed), H12
(polling must not drop pending updates; redeliveries deduplicated),
M8 (future auth_dates rejected; admin surface needs a fresher
initData than the 24 h user window), L2 (/health must notice a dead
DB), L8 (FastAPI docs surface disabled), L9 (valid-HMAC initData
WITHOUT a user field must not authenticate), L10 (rate-limit gaps +
X-Forwarded-For trust), H3 middleware slice (chunked upload bodies
bounded even without Content-Length).

Written against the UNFIXED tree first.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from types import SimpleNamespace

import pytest

TEST_TOKEN = "aj" + "-test-token-" + "0" * 8


def _signed_init_data(fields, token=None):
    token = TEST_TOKEN if token is None else token
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode({**fields, "hash": digest})


def _fields(auth_date_offset=0, with_user=True):
    fields = {"query_id": "AAE", "auth_date": str(int(time.time()) + auth_date_offset)}
    if with_user:
        fields["user"] = json.dumps({"id": 880601, "first_name": "T"})
    return fields


# ── H10: no bot token configured → nothing verifies ──────────────────


def test_batch_aj_h10_empty_bot_token_fails_closed(monkeypatch):
    """Unfixed hole: with an empty bot token, the HMAC secret is
    derivable by ANYONE (HMAC of the well-known "WebAppData" label
    with an empty key), so initData for any user — including admins —
    was forgeable whenever the token was unset/misconfigured."""
    from app.config import settings
    from app.webapp.auth import parse_and_verify_init_data

    monkeypatch.setattr(settings, "bot_token", "")
    forged = _signed_init_data(_fields(), token="")
    assert parse_and_verify_init_data(forged, bot_token="") is None
    assert parse_and_verify_init_data(forged) is None


# ── M8: future auth_date + shorter admin window ──────────────────────


def test_batch_aj_m8_future_auth_date_rejected():
    """Unfixed: only the stale direction was checked — an auth_date
    an hour in the future verified fine (clock-skew games and a
    pre-minted long-lived forgery window)."""
    from app.webapp.auth import parse_and_verify_init_data

    data = _signed_init_data(_fields(auth_date_offset=3600))
    assert parse_and_verify_init_data(data, bot_token=TEST_TOKEN) is None
    # Small skew (2 minutes) still verifies — Telegram/Render clocks
    # are not perfectly synchronised.
    data = _signed_init_data(_fields(auth_date_offset=120))
    assert parse_and_verify_init_data(data, bot_token=TEST_TOKEN) is not None


def test_batch_aj_m8_admin_requires_fresh_init_data(monkeypatch):
    """Unfixed: require_admin accepted the same 24 h-old initData as
    any user read — a day-old leaked admin initData could still drive
    admin actions. Admin actions now need initData ≤ 12 h old."""
    from fastapi import HTTPException

    from app.config import settings
    from app.models.schema import User
    from app.webapp import api as api_mod

    monkeypatch.setattr(settings, "bot_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "admin_ids_raw", "880601")
    admin_user = User(id=880601, first_name="Admin", language_code="my")

    fresh = _signed_init_data(_fields(auth_date_offset=-3600))
    assert api_mod.require_admin(fresh, admin_user).id == 880601

    stale_for_admin = _signed_init_data(_fields(auth_date_offset=-13 * 3600))
    with pytest.raises(HTTPException) as excinfo:
        api_mod.require_admin(stale_for_admin, admin_user)
    assert excinfo.value.status_code == 403


# ── L9: initData without a user field ────────────────────────────────


def test_batch_aj_l9_init_data_without_user_field_rejected():
    """Unfixed: a validly-signed payload with NO user field returned
    the raw params dict, and get_current_user then KeyError'd on
    ["id"] — a 500 instead of a clean 401."""
    from app.webapp.auth import parse_and_verify_init_data

    data = _signed_init_data(_fields(with_user=False))
    assert parse_and_verify_init_data(data, bot_token=TEST_TOKEN) is None


# ── L2: /health reflects the DB ──────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_aj_l2_health_reports_db_failure(monkeypatch):
    """Unfixed: /health returned a static {"status": "ok"} — Render's
    health-based recovery could never notice a dead database."""
    from app.db import session as session_mod
    from app.webapp import server as server_mod

    class _DeadSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **k):
            raise RuntimeError("db is down")

    monkeypatch.setattr(session_mod, "AsyncSessionLocal", lambda: _DeadSession())
    resp = await server_mod.health_check()
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_batch_aj_l2_health_ok_when_db_answers(monkeypatch):
    from app.db import session as session_mod
    from app.webapp import server as server_mod

    class _OkSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def execute(self, *a, **k):
            return None

    monkeypatch.setattr(session_mod, "AsyncSessionLocal", lambda: _OkSession())
    resp = await server_mod.health_check()
    assert resp.status_code == 200


# ── L8: interactive docs / schema are not served ─────────────────────


def test_batch_aj_l8_docs_disabled():
    from app.webapp.server import webapp_app

    assert webapp_app.docs_url is None
    assert webapp_app.redoc_url is None
    assert webapp_app.openapi_url is None


# ── L10: rate-limit coverage + XFF handling ──────────────────────────


def test_batch_aj_l10_new_endpoints_are_limited():
    from app.webapp.rate_limit import GET_RATE_LIMITS, RATE_LIMITS

    assert "/api/user/verify-membership" in RATE_LIMITS
    assert "/api/user/logo" in RATE_LIMITS
    assert "/api/jobs/{job_id}/cancel" in RATE_LIMITS
    assert "/api/recap/frame" in GET_RATE_LIMITS


def test_batch_aj_l10_client_ip_uses_last_forwarded_hop():
    """Unfixed: the FIRST X-Forwarded-For value was trusted — fully
    client-suppliable, so an attacker rotated fake first hops to
    escape IP-keyed buckets. The platform proxy APPENDS the observed
    address, so the LAST hop is the trustworthy one."""
    from app.webapp.rate_limit import _client_ip

    request = SimpleNamespace(
        headers={"x-forwarded-for": "1.1.1.1, 9.9.9.9, 203.0.113.7"},
        client=SimpleNamespace(host="10.0.0.1"),
    )
    assert _client_ip(request) == "203.0.113.7"


# ── H3 (middleware slice): chunked bodies are capped too ─────────────


@pytest.mark.asyncio
async def test_batch_aj_h3_chunked_upload_body_is_capped():
    """Unfixed gap: MAX_CONTENT_LENGTH only fired when the client
    sent a Content-Length header — a chunked (or lying) upload sailed
    past the middleware and only the endpoint's own streaming cap
    (where present) stood in the way. The ASGI guard counts actual
    bytes for capped paths and refuses over-cap bodies itself."""
    from app.webapp.rate_limit import ChunkedUploadCapMiddleware

    app_calls = []

    async def dummy_app(scope, receive, send):
        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body += message.get("body", b"")
                if not message.get("more_body"):
                    break
        app_calls.append(body)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = ChunkedUploadCapMiddleware(dummy_app, {"/api/vip/submit-slip": 1000})

    def _scope():
        return {
            "type": "http",
            "method": "POST",
            "path": "/api/vip/submit-slip",
            "headers": [(b"transfer-encoding", b"chunked")],
        }

    # 3 chunks × 600 bytes = 1800 > the 1000 cap → 413, app untouched.
    chunks = [
        {"type": "http.request", "body": b"x" * 600, "more_body": True},
        {"type": "http.request", "body": b"x" * 600, "more_body": True},
        {"type": "http.request", "body": b"x" * 600, "more_body": False},
    ]

    async def receive_over():
        return chunks.pop(0)

    sent = []

    async def send_over(message):
        sent.append(message)

    await middleware(_scope(), receive_over, send_over)
    assert sent and sent[0]["status"] == 413
    assert app_calls == []

    # Under the cap → passes through byte-identical.
    chunks2 = [
        {"type": "http.request", "body": b"y" * 400, "more_body": True},
        {"type": "http.request", "body": b"y" * 400, "more_body": False},
    ]

    async def receive_under():
        return chunks2.pop(0)

    sent2 = []

    async def send_under(message):
        sent2.append(message)

    await middleware(_scope(), receive_under, send_under)
    assert sent2 and sent2[0]["status"] == 200
    assert app_calls == [b"y" * 800]


# ── H12: polling keeps pending updates; redelivery is deduplicated ──


def test_batch_aj_h12_polling_does_not_drop_pending_updates():
    import main as main_mod

    src = open(main_mod.__file__, encoding="utf-8").read()
    assert "drop_pending_updates=False" in src
    assert "drop_pending_updates=True" not in src


@pytest.mark.asyncio
async def test_batch_aj_h12_update_dedup_middleware_drops_redeliveries():
    """With drop_pending_updates=False, Telegram redelivers updates
    it thinks were lost (exactly the restart window H12 is about) —
    the polling path needs the webhook path's dedup memory so a
    redelivered slip is processed exactly once."""
    from app.webapp.telegram_webhook import UpdateDedupMiddleware, recent_update_ids

    recent_update_ids.clear()
    middleware = UpdateDedupMiddleware()
    calls = []

    async def handler(event, data):
        calls.append(event.update_id)
        return "handled"

    event = SimpleNamespace(update_id=424242)
    assert await middleware(handler, event, {}) == "handled"
    assert await middleware(handler, event, {}) is None  # redelivery dropped
    assert calls == [424242]
    recent_update_ids.clear()

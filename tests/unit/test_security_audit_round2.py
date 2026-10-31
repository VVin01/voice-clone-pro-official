"""Security audit round 2 (2026-10-29) on security-fixes-audit-2026-10-28.

Covers the six gaps without a live server: SSRF helper, streaming
uploads, recap concurrency union count, credit refund, and the auth
dependency on the frame/receipt endpoints.
"""

from __future__ import annotations

import inspect
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.services.url_safety import (
    SSRFBlockedError,
    follow_redirects_safely,
    is_ip_blocked,
    validate_public_url,
)


# ── 4. SSRF ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.0.0.5", "172.16.0.1", "192.168.1.1", "169.254.169.254",
     "100.64.0.1", "0.0.0.0", "::1", "fe80::1", "224.0.0.1"],
)
def test_ssrf_private_ips_blocked(ip):
    assert is_ip_blocked(ip) is True


def test_ssrf_public_ip_literal_allowed():
    assert validate_public_url("http://93.184.216.34/video") == "http://93.184.216.34/video"


def test_ssrf_private_ip_literal_blocked():
    with pytest.raises(SSRFBlockedError):
        validate_public_url("http://127.0.0.1:8080/admin")


@pytest.mark.parametrize("url", ["ftp://example.com/x", "file:///etc/passwd", "gopher://example.com/"])
def test_ssrf_non_http_schemes_blocked(url):
    with pytest.raises(SSRFBlockedError):
        validate_public_url(url)


def test_ssrf_embedded_credentials_blocked():
    with pytest.raises(SSRFBlockedError):
        validate_public_url("https://user:pass@example.com/video")


def test_ssrf_hostname_resolving_to_private_blocked(monkeypatch):
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, None, None, None, ("10.1.2.3", 443))],
    )
    with pytest.raises(SSRFBlockedError):
        validate_public_url("https://internal.example.com/video")


def test_ssrf_redirect_to_private_blocked(monkeypatch):
    class Resp:
        status_code = 302
        headers = {"Location": "http://169.254.169.254/latest/meta-data/"}

    class Session:
        def head(self, *a, **k):
            return Resp()

    monkeypatch.setattr("app.services.url_safety.requests.Session", lambda: Session())
    with pytest.raises(SSRFBlockedError):
        follow_redirects_safely("https://short.example.com/abc")


# ── 3. Streaming uploads ───────────────────────────────────────────────────
class _FakeUpload:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, size=-1):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


@pytest.mark.asyncio
async def test_save_upload_streaming_writes_chunks(tmp_path):
    from app.webapp.api import _save_upload_streaming

    dest = tmp_path / "slip.jpg"
    total = await _save_upload_streaming(_FakeUpload([b"a" * 10, b"b" * 5]), dest, 100)
    assert total == 15
    assert dest.read_bytes() == b"a" * 10 + b"b" * 5


@pytest.mark.asyncio
async def test_save_upload_streaming_413_deletes_partial(tmp_path):
    from app.webapp.api import _save_upload_streaming

    dest = tmp_path / "big.mp4"
    with pytest.raises(HTTPException) as exc:
        await _save_upload_streaming(_FakeUpload([b"x" * 10, b"y" * 10]), dest, 15)
    assert exc.value.status_code == 413
    assert not dest.exists()


# ── 5. Recap concurrency (union, not sum) ──────────────────────────────────
@pytest.mark.asyncio
async def test_recap_active_count_union_not_sum():
    from app.webapp import api as api_mod

    api_mod.RECAP_JOBS_REGISTRY["rc_shared"] = {"user_id": 42, "status": "RUNNING"}
    api_mod.RECAP_JOBS_REGISTRY["rc_other"] = {"user_id": 99, "status": "RUNNING"}
    try:
        class Result:
            def all(self):
                return [("rc_shared",), ("rc_db_only",)]

        class FakeDB:
            async def execute(self, *a, **k):
                return Result()

        assert await api_mod._recap_active_job_count(FakeDB(), 42) == 2
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop("rc_shared", None)
        api_mod.RECAP_JOBS_REGISTRY.pop("rc_other", None)


# ── 5. Credit refund ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_refund_recap_credit_adds_one(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.config import settings
    from app.db.base import Base
    from app.models.schema import User
    from app.webapp import api as api_mod

    monkeypatch.setattr(settings, "admin_ids_raw", "999001")

    # Hardening F5 made the refund an atomic conditional UPDATE, which
    # a get()/commit() dict fake cannot execute — this harness now
    # points AsyncSessionLocal at a real file-backed SQLite DB (one
    # short-lived engine per session). Behaviour asserted is unchanged.
    db_file = str(tmp_path / "refund.db")
    sync_engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(sync_engine)
    sync_engine.dispose()

    def make_session():
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", future=True)
        maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
        return maker()

    async with make_session() as s:
        s.add(User(id=123, recap_credits=4))
        await s.commit()

    monkeypatch.setattr("app.db.session.AsyncSessionLocal", make_session)
    assert await api_mod._refund_recap_credit(123) is True

    async with make_session() as s:
        stored = await s.get(User, 123)
        assert stored.recap_credits == 5


# ── 1 & 2. Auth dependencies on receipts / frame ───────────────────────────
def test_frame_endpoint_requires_get_current_user():
    from app.webapp.api import get_current_user, get_recap_frame

    param = inspect.signature(get_recap_frame).parameters["user"]
    assert param.default.dependency is get_current_user


def test_receipt_endpoint_requires_get_current_user():
    from app.webapp.api import get_current_user, get_receipt_image

    param = inspect.signature(get_receipt_image).parameters["user"]
    assert param.default.dependency is get_current_user


@pytest.mark.asyncio
async def test_receipt_owner_and_admin(tmp_path, monkeypatch):
    from app.config import settings
    from app.webapp import api as api_mod

    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "admin_ids_raw", "999001")
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "slip_7_ab12.jpg").write_bytes(b"fake-slip")

    owner = MagicMock()
    owner.id = 7

    class Found:
        def scalar_one_or_none(self):
            return MagicMock()

    class FakeDB:
        async def execute(self, *a, **k):
            return Found()

    resp = await api_mod.get_receipt_image("slip_7_ab12.jpg", user=owner, db=FakeDB())
    assert resp.path.endswith("slip_7_ab12.jpg")

    # Traversal + not-owned both collapse to the same 404.
    with pytest.raises(HTTPException) as exc:
        await api_mod.get_receipt_image("../secret.jpg", user=owner, db=FakeDB())
    assert exc.value.status_code == 404

    class Empty:
        def scalar_one_or_none(self):
            return None

    class EmptyDB:
        async def execute(self, *a, **k):
            return Empty()

    stranger = MagicMock()
    stranger.id = 8
    with pytest.raises(HTTPException) as exc:
        await api_mod.get_receipt_image("slip_7_ab12.jpg", user=stranger, db=EmptyDB())
    assert exc.value.status_code == 404

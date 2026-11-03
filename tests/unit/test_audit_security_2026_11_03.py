"""Audit fix batch D — security hardening pins (2026-11-03).

Pins four hardening changes from the whole-codebase security audit:

1. ``mini_app_url`` is a real ``Settings`` field. It used to exist
   only as ``getattr(settings, "mini_app_url", <hardcoded>)``
   fallbacks in api.py / clone.py / worker.py — and since the
   Settings model ignores unknown env names, setting MINI_APP_URL in
   the environment silently did nothing and every WebApp button was
   pinned to the hardcoded default forever.

2. The Mini App rate limiter keys on client IP + parsed Telegram id
   combined. It used to key on the UNVERIFIED parsed id alone, so
   anyone who knew a victim's Telegram id could spend that victim's
   quota from any address.

3. Stream tokens (the ?t= download tokens from PR #59) are signed
   with a dedicated subkey — HMAC-SHA256(key=bot_token,
   msg="stream-token-v1") — never with the raw bot token itself.
   Tokens minted under the old raw-key scheme must not verify.

4. The internal worker upload endpoint
   (POST /internal/worker/upload-result/{job_id}) previously had no
   test coverage at all. Pinned here: unset worker token -> 404
   (fails closed), wrong token -> 403, traversal filenames are
   sanitized into the outputs dir, oversized uploads -> 413 with the
   partial file removed.
"""

import hashlib
import hmac
import io
import json
import time
import urllib.parse
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile
from starlette.requests import Request
from starlette.responses import Response

from app.config import settings
from app.config.settings import Settings
from app.models.enums import JobStatus, JobType
from app.models.schema import Job
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod
from app.webapp import rate_limit as rl_mod

TEST_BOT_TOKEN = "123456:unit-test-dummy-token"
TEST_WORKER_TOKEN = "unit-test-worker-token"
JOB_ID = "job_worker_upload_1"
OWNER_TG = 880101


# ── 1. mini_app_url is a real, env-overridable Settings field ───────

def test_mini_app_url_field_exists_with_production_default():
    assert "mini_app_url" in Settings.model_fields
    assert (
        Settings.model_fields["mini_app_url"].default
        == "https://voice-clone-studio-c1er.onrender.com"
    )
    # The live settings object exposes it directly (no getattr games).
    assert settings.mini_app_url == "https://voice-clone-studio-c1er.onrender.com"


def test_mini_app_url_is_env_overridable(monkeypatch):
    monkeypatch.setenv("MINI_APP_URL", "https://example.test/miniapp")
    fresh = Settings()
    assert fresh.mini_app_url == "https://example.test/miniapp"


# ── 2. Rate limiter keys on IP + parsed id combined ─────────────────

def _init_data(user_id: int) -> str:
    return urllib.parse.urlencode({"user": json.dumps({"id": user_id})})


def _request(ip: str | None = None, user_id: int | None = None,
             peer: str = "10.0.0.9", path: str = "/api/recap/generate",
             raw_init: bytes | None = None) -> Request:
    headers = []
    if ip is not None:
        headers.append((b"x-forwarded-for", ip.encode()))
    if raw_init is not None:
        headers.append((b"x-telegram-init-data", raw_init))
    elif user_id is not None:
        headers.append((b"x-telegram-init-data", _init_data(user_id).encode()))
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": headers,
        "client": (peer, 4321),
    }
    return Request(scope)


def test_rate_key_combines_ip_and_parsed_id():
    key = rl_mod._rate_key(_request(ip="203.0.113.7, 10.0.0.1", user_id=4242))
    # First forwarded hop is the client address Render reports.
    assert key == "ipuser:203.0.113.7:4242"


def test_rate_key_same_forged_id_from_other_ip_is_a_different_bucket():
    # The pre-fix key was "user:<id>" alone: a forged id spent the
    # victim's bucket from anywhere. Now the IP namespaces the id.
    victim = rl_mod._rate_key(_request(ip="198.51.100.2", user_id=4242))
    attacker = rl_mod._rate_key(_request(ip="203.0.113.7", user_id=4242))
    assert victim != attacker
    assert attacker == "ipuser:203.0.113.7:4242"


def test_rate_key_without_init_data_is_ip_only():
    assert rl_mod._rate_key(_request(ip="203.0.113.7")) == "ip:203.0.113.7"
    # No forwarded header -> direct peer address.
    assert rl_mod._rate_key(_request(peer="192.0.2.5")) == "ip:192.0.2.5"
    # Garbage init data falls back to the IP, never crashes.
    req = _request(ip="203.0.113.7", raw_init=b"not-valid-init-data")
    assert rl_mod._rate_key(req) == "ip:203.0.113.7"


@pytest.mark.asyncio
async def test_rate_limiter_buckets_are_per_ip_and_id_pair():
    mw = rl_mod.RateLimitMiddleware(
        app=lambda scope, receive, send: None,
        limits={"/api/recap/generate": (2, 60)},
    )

    async def _ok(_request):
        return Response("ok", status_code=200)

    ip_a = _request(ip="203.0.113.7", user_id=4242)
    ip_b = _request(ip="198.51.100.2", user_id=4242)
    for _ in range(2):
        res = await mw.dispatch(ip_a, _ok)
        assert res.status_code == 200
    # Third hit from IP A with that id is limited…
    res = await mw.dispatch(ip_a, _ok)
    assert res.status_code == 429
    # …but the same Telegram id arriving from IP B has its own bucket.
    res = await mw.dispatch(ip_b, _ok)
    assert res.status_code == 200


# ── 3. Stream tokens use a derived subkey, not the raw bot token ────

@pytest.fixture(autouse=True)
def _bot_token(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", TEST_BOT_TOKEN)


def test_stream_signing_key_is_derived_subkey():
    expected = hmac.new(
        TEST_BOT_TOKEN.encode("utf-8"), b"stream-token-v1", hashlib.sha256
    ).digest()
    assert api_mod._stream_signing_key() == expected
    # The raw bot token is never itself the signing key.
    assert api_mod._stream_signing_key() != TEST_BOT_TOKEN.encode("utf-8")


def test_stream_token_round_trip_with_subkey():
    token = api_mod._make_stream_token(JOB_ID, 4242, "video")
    assert token is not None
    assert api_mod._verify_stream_token(token, JOB_ID, "video") == 4242


def test_stream_token_signed_with_raw_bot_token_is_rejected():
    # A token minted the pre-fix way (raw bot token as the HMAC key)
    # must NOT verify under the subkey scheme.
    exp = int(time.time()) + 600
    msg = f"stream:{JOB_ID}:4242:video:{exp}".encode("utf-8")
    legacy_sig = hmac.new(
        TEST_BOT_TOKEN.encode("utf-8"), msg, hashlib.sha256
    ).hexdigest()
    legacy_token = f"4242.{exp}.{legacy_sig}"
    assert api_mod._verify_stream_token(legacy_token, JOB_ID, "video") is None


def test_stream_signing_off_without_bot_token(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "")
    assert api_mod._stream_signing_key() == b""
    assert api_mod._make_stream_token(JOB_ID, 4242, "video") is None


# ── 4. Internal worker upload endpoint ──────────────────────────────

async def _seed_job(db_session, storage_root: Path):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    job = Job(
        id=JOB_ID, user_id=owner.id, status=JobStatus.RUNNING.value,
        job_type=JobType.VOICE_CLONE.value, engine="voxcpm",
    )
    db_session.add(job)
    await db_session.commit()
    return job


def _upload(filename: str, data: bytes = b"RIFF-fake-audio") -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO(data))


@pytest.mark.asyncio
async def test_worker_upload_404_when_token_unset(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "worker_token", "")
    await _seed_job(db_session, tmp_path)
    # Even presenting some token cannot open the endpoint: unset
    # WORKER_TOKEN means the endpoint is effectively absent.
    with pytest.raises(HTTPException) as exc:
        await api_mod.upload_worker_job_result(
            JOB_ID, file=_upload("result.wav"),
            auth_token=TEST_WORKER_TOKEN, db=db_session,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_worker_upload_403_on_wrong_or_missing_token(
    db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "worker_token", TEST_WORKER_TOKEN)
    await _seed_job(db_session, tmp_path)
    for bad in ("wrong-token", "", None):
        with pytest.raises(HTTPException) as exc:
            await api_mod.upload_worker_job_result(
                JOB_ID, file=_upload("result.wav"), auth_token=bad, db=db_session
            )
        assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_worker_upload_sanitizes_traversal_filename(
    db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "worker_token", TEST_WORKER_TOKEN)
    job = await _seed_job(db_session, tmp_path)

    res = await api_mod.upload_worker_job_result(
        JOB_ID,
        file=_upload("../../../etc/passwd.wav"),
        auth_token=TEST_WORKER_TOKEN, db=db_session,
    )
    assert res["status"] == "success"
    saved = Path(res["saved_path"])
    outputs_dir = tmp_path / "outputs"
    # The file landed inside the outputs dir under a server-built
    # name — nothing of the attacker-controlled path survives.
    assert saved.parent == outputs_dir
    assert saved.is_file()
    assert saved.name.startswith(f"{JOB_ID}_")
    assert saved.suffix == ".wav"
    assert ".." not in saved.name and "/" not in saved.name
    assert saved.read_bytes() == b"RIFF-fake-audio"
    assert not (tmp_path / "etc").exists()
    # The job was marked successful with the sanitized path recorded.
    assert job.result_path == str(saved)
    assert job.status == JobStatus.SUCCESS.value


@pytest.mark.asyncio
async def test_worker_upload_unknown_extension_falls_back_to_wav(
    db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "worker_token", TEST_WORKER_TOKEN)
    await _seed_job(db_session, tmp_path)
    res = await api_mod.upload_worker_job_result(
        JOB_ID,
        file=_upload("payload.exe"),
        auth_token=TEST_WORKER_TOKEN, db=db_session,
    )
    assert Path(res["saved_path"]).suffix == ".wav"


class _ChunkStream:
    """File-like that yields ``total`` zero bytes without allocating
    them all at once (the endpoint reads it in 1 MB chunks)."""

    def __init__(self, total: int):
        self._remaining = total

    def read(self, n: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        take = min(n if n and n > 0 else 1024 * 1024, self._remaining)
        self._remaining -= take
        return b"\x00" * take


@pytest.mark.asyncio
async def test_worker_upload_rejects_oversized_and_removes_partial(
    db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "worker_token", TEST_WORKER_TOKEN)
    job = await _seed_job(db_session, tmp_path)

    oversized = UploadFile(
        filename="big.wav", file=_ChunkStream(101 * 1024 * 1024)
    )
    with pytest.raises(HTTPException) as exc:
        await api_mod.upload_worker_job_result(
            JOB_ID, file=oversized,
            auth_token=TEST_WORKER_TOKEN, db=db_session,
        )
    assert exc.value.status_code == 413
    # The partial file was removed and the job was not marked success.
    outputs_dir = tmp_path / "outputs"
    assert list(outputs_dir.iterdir()) == []
    assert job.status == JobStatus.RUNNING.value
    assert not job.result_path

"""Hardening batch — Family 6 (endpoint/auth tests, hunt #2 §5).

The suite has NO HTTP-level test harness; like the rest of the
suite, these call the endpoint functions directly with real DB
sessions and real auth primitives. What they pin:

- parse_and_verify_init_data tamper cases (F4 covered freshness):
  a modified payload carrying the original hash, a garbage hash,
  and a hash minted with a different bot token are all rejected;
  the untampered control verifies.
- _require_job_owner behaviour through its real callers: job
  status, voice audio download, recap file download — owner OK,
  stranger 404 (also for a nonexistent job: no probing).
- Recap script GET/PUT ownership (registry-backed).
- purge-history: non-admin 403; admin purge actually deletes the
  matched rows and leaves the rest.
- Pending slips: the require_admin dependency rejects non-admins
  even with a valid self-signed initData, accepts a real admin;
  the listing returns the seeded pending slip.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.responses import FileResponse

import app.webapp.api as api_mod
from app.config import settings
from app.models.enums import JobStatus, JobType
from app.models.schema import Job, PaymentRequest, User
from app.services.ledger import get_or_create_user
from app.webapp.auth import parse_and_verify_init_data

TOKEN = "family6-test-token"


def _signed_init_data(fields, token=TOKEN):
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode({**fields, "hash": digest})


def _fields(uid=880601):
    return {
        "user": json.dumps({"id": uid, "first_name": "T"}),
        "query_id": "AAE",
        "auth_date": str(int(time.time())),
    }


# ── initData tamper cases ─────────────────────────────────────────────

def test_initdata_control_verifies():
    out = parse_and_verify_init_data(_signed_init_data(_fields()), bot_token=TOKEN)
    assert out is not None and out["id"] == 880601


def test_initdata_modified_payload_with_old_hash_rejected():
    good = _signed_init_data(_fields())
    # Attacker swaps in their own user id but keeps the original hash.
    tampered_fields = _fields(uid=999999)
    tampered = urllib.parse.urlencode({**tampered_fields, "hash": urllib.parse.parse_qs(good)["hash"][0]})
    assert parse_and_verify_init_data(tampered, bot_token=TOKEN) is None


def test_initdata_garbage_hash_rejected():
    fields = _fields()
    forged = urllib.parse.urlencode({**fields, "hash": "deadbeef" * 8})
    assert parse_and_verify_init_data(forged, bot_token=TOKEN) is None
    forged2 = urllib.parse.urlencode({**fields, "hash": "not-even-hex"})
    assert parse_and_verify_init_data(forged2, bot_token=TOKEN) is None


def test_initdata_hash_from_different_token_rejected():
    forged = _signed_init_data(_fields(), token="a-different-bot-token")
    assert parse_and_verify_init_data(forged, bot_token=TOKEN) is None


# ── Job ownership via real callers ────────────────────────────────────

async def _seed_voice_job(db_session, owner_id, job_id="job-own-1", result_path=None):
    job = Job(
        id=job_id, user_id=owner_id, status=JobStatus.SUCCESS.value,
        job_type=JobType.VOICE_CLONE.value, engine="voxcpm",
        result_path=result_path,
    )
    db_session.add(job)
    await db_session.commit()
    return job


@pytest.mark.asyncio
async def test_job_status_owner_ok_stranger_404(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=8001)
    stranger, _ = await get_or_create_user(db_session, telegram_id=8002)
    await _seed_voice_job(db_session, owner.id)

    res = await api_mod.get_job_status("job-own-1", user=owner, db=db_session)
    assert res["id"] == "job-own-1" and res["status"] == "SUCCESS"

    with pytest.raises(HTTPException) as exc:
        await api_mod.get_job_status("job-own-1", user=stranger, db=db_session)
    assert exc.value.status_code == 404
    # Nonexistent job: same 404 (no ownership probing).
    with pytest.raises(HTTPException) as exc:
        await api_mod.get_job_status("job-nope", user=owner, db=db_session)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_audio_download_owner_ok_stranger_404(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    owner, _ = await get_or_create_user(db_session, telegram_id=8011)
    stranger, _ = await get_or_create_user(db_session, telegram_id=8012)
    audio = tmp_path / "out.wav"
    audio.write_bytes(b"RIFF-fake")
    await _seed_voice_job(db_session, owner.id, job_id="job-audio-1", result_path=str(audio))

    request = SimpleNamespace(url=SimpleNamespace(path="/api/audio/download/job-audio-1"))
    res = await api_mod.download_job_audio("job-audio-1", request=request, user=owner, db=db_session)
    assert isinstance(res, FileResponse)

    with pytest.raises(HTTPException) as exc:
        await api_mod.download_job_audio("job-audio-1", request=request, user=stranger, db=db_session)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_recap_download_owner_ok_stranger_404(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    owner, _ = await get_or_create_user(db_session, telegram_id=8021)
    stranger, _ = await get_or_create_user(db_session, telegram_id=8022)
    job = Job(
        id="rc_dl1", user_id=owner.id, status=JobStatus.SUCCESS.value,
        job_type=JobType.RECAP.value, engine="recap",
    )
    db_session.add(job)
    await db_session.commit()

    video = tmp_path / "final.mp4"
    video.write_bytes(b"\x00" * 2048)
    api_mod.RECAP_JOBS_REGISTRY["rc_dl1"] = {
        "user_id": owner.id, "status": "SUCCESS", "video_path": str(video),
    }
    try:
        request = SimpleNamespace(url=SimpleNamespace(path="/api/recap/download/rc_dl1/video"))
        res = await api_mod.download_recap_file("rc_dl1", "video", request=request, user=owner, db=db_session)
        assert isinstance(res, FileResponse)

        with pytest.raises(HTTPException) as exc:
            await api_mod.download_recap_file("rc_dl1", "video", request=request, user=stranger, db=db_session)
        assert exc.value.status_code == 404
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop("rc_dl1", None)


# ── Recap script GET/PUT ownership ────────────────────────────────────

def _plan():
    return {
        "planner_mode": "test",
        "segments": [
            {
                "source_segment_ids": [1], "atomic_id": 1,
                "timestamp": "00:00:00,000 --> 00:00:04,000",
                "video_start": 0.0, "video_end": 4.0,
                "source_text": "He runs into the room.",
                "narration": "သူ အခန်းထဲ ပြေးဝင်လာတယ်။",
                "translation": "သူ အခန်းထဲ ပြေးဝင်လာတယ်။",
            },
        ],
    }


@pytest.fixture
def script_job(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    job_id = "rc_script1"
    ws = tmp_path / "recap_output" / job_id / "workspace"
    ws.mkdir(parents=True)
    (ws / "smart_sync_plan.json").write_text(json.dumps(_plan(), ensure_ascii=False), encoding="utf-8")
    api_mod.RECAP_JOBS_REGISTRY[job_id] = {"user_id": 8031, "status": "SCRIPT_READY"}
    yield job_id
    api_mod.RECAP_JOBS_REGISTRY.pop(job_id, None)


@pytest.mark.asyncio
async def test_recap_script_get_ownership(db_session, script_job):
    owner = User(id=8031, first_name="Owner", language_code="my")
    stranger = User(id=8032, first_name="Stranger", language_code="my")

    res = await api_mod.get_recap_script(script_job, user=owner)
    assert res["status"] == "SCRIPT_READY"
    assert len(res["segments"]) == 1
    assert res["segments"][0]["source_text"] == "He runs into the room."

    with pytest.raises(HTTPException) as exc:
        await api_mod.get_recap_script(script_job, user=stranger)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_recap_script_put_ownership(db_session, script_job):
    owner = User(id=8031, first_name="Owner", language_code="my")
    stranger = User(id=8032, first_name="Stranger", language_code="my")
    empty = api_mod.RecapScriptUpdateRequest(segments=[])

    with pytest.raises(HTTPException) as exc:
        await api_mod.update_recap_script(script_job, empty, user=stranger, _gate=stranger)
    assert exc.value.status_code == 404

    res = await api_mod.update_recap_script(script_job, empty, user=owner, _gate=owner)
    assert res == {"success": True, "updated": 0, "warnings": {}}

    edit = api_mod.RecapScriptUpdateRequest(segments=[
        api_mod.RecapScriptEditItem(index=1, narration="သူ အခန်းထဲသို့ အပြေးအလွား ဝင်လာခဲ့တယ်။")
    ])
    res = await api_mod.update_recap_script(script_job, edit, user=owner, _gate=owner)
    assert res["success"] is True and res["updated"] == 1


# ── Purge history ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_purge_history_admin_only_and_actually_purges(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", "8041")
    admin, _ = await get_or_create_user(db_session, telegram_id=8041)
    plain, _ = await get_or_create_user(db_session, telegram_id=8042)
    for jid, st in (("job-f1", JobStatus.FAILED.value), ("job-s1", JobStatus.SUCCESS.value)):
        db_session.add(Job(id=jid, user_id=plain.id, status=st, engine="voxcpm"))
    await db_session.commit()

    req = api_mod.AdminPurgeHistoryRequest(mode="failed")
    with pytest.raises(HTTPException) as exc:
        await api_mod.purge_admin_history(req, user=plain, db=db_session)
    assert exc.value.status_code == 403
    # Nothing was purged by the rejected call.
    assert await db_session.get(Job, "job-f1") is not None

    res = await api_mod.purge_admin_history(req, user=admin, db=db_session)
    assert res["success"] is True and res["purged_count"] == 1
    assert await db_session.get(Job, "job-f1") is None
    assert await db_session.get(Job, "job-s1") is not None


# ── Pending slips: admin-only ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_pending_slips_require_real_admin(db_session, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", TOKEN)
    monkeypatch.setattr(settings, "admin_ids_raw", "8051")
    admin, _ = await get_or_create_user(db_session, telegram_id=8051)
    plain, _ = await get_or_create_user(db_session, telegram_id=8052)
    db_session.add(PaymentRequest(
        id="PAY-F6-1", user_id=plain.id, package_code="MIN_10",
        minutes=10, amount_mmk=8000, status="PENDING",
    ))
    await db_session.commit()

    # A non-admin with a perfectly valid self-signed initData is
    # still rejected by require_admin (the dependency the endpoint
    # hangs off).
    plain_init = _signed_init_data(_fields(uid=8052))
    with pytest.raises(HTTPException) as exc:
        api_mod.require_admin(x_telegram_init_data=plain_init, user=plain)
    assert exc.value.status_code == 403

    # The admin's initData must match the admin user, and does.
    admin_init = _signed_init_data(_fields(uid=8051))
    assert api_mod.require_admin(x_telegram_init_data=admin_init, user=admin) is admin

    slips = await api_mod.get_pending_payment_slips(admin=admin, db=db_session)
    assert [s["id"] for s in slips] == ["PAY-F6-1"]

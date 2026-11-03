"""Over-50MB download-only rule pins (2026-11-03).

Ulfa's requirement: when a job's output exceeds Telegram's 50 MB bot
send cap, the Mini App must not offer "Send to Telegram" at all — the
premium Download action stands alone. The downloader already exposed
``telegram_sendable`` for exactly this; the recap and voice serializers
now do the same, in BOTH the status payloads the result screens poll
and the /api/history list items:

- recap: sendable when the full video fits the cap, or the job is
  segmented and EVERY resolved part fits (parts delivery is how
  segmented jobs reach Telegram); otherwise False.
- voice: sendable when the result audio fits the cap.
- unknown size (file missing / not a SUCCESS row) keeps the button:
  telegram_sendable True, output_size_bytes None.

The send endpoints themselves are unchanged (recap keeps its graceful
parts/Mini-App handling; voice keeps its coded 413) — these tests pin
only the serializer flags the UI now keys on.
"""

import datetime
from pathlib import Path

import pytest

from app.config import settings
from app.models.enums import JobStatus, JobType
from app.models.schema import Job
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod

MB = 1024 * 1024
CAP = api_mod._DL_TELEGRAM_LIMIT
OWNER_TG = 880101


def _make_file(path: Path, size: int) -> Path:
    """A file whose stat() size is exactly ``size`` (sparse — fast)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)
    return path


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    for jid in [k for k in api_mod.RECAP_JOBS_REGISTRY if k.startswith("rc_50mb")]:
        api_mod.RECAP_JOBS_REGISTRY.pop(jid, None)


# ── Recap helper: the rule itself ─────────────────────────────────────

def test_recap_helper_small_video_sendable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    video = _make_file(tmp_path / "v.mp4", 10 * MB)
    size, sendable = api_mod._recap_output_size_and_sendable("rc_50mb_a", str(video))
    assert size == 10 * MB
    assert sendable is True


def test_recap_helper_big_video_without_parts_not_sendable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    video = _make_file(tmp_path / "v.mp4", 60 * MB)
    size, sendable = api_mod._recap_output_size_and_sendable("rc_50mb_b", str(video))
    assert size == 60 * MB
    assert sendable is False


def test_recap_helper_segmented_all_parts_fit_sendable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    jid = "rc_50mb_c"
    video = _make_file(tmp_path / "full.mp4", 80 * MB)
    p1 = _make_file(tmp_path / "p1.mp4", 20 * MB)
    p2 = _make_file(tmp_path / "p2.mp4", CAP)  # exactly at the cap still sends
    api_mod.RECAP_JOBS_REGISTRY[jid] = {
        "user_id": 1, "status": "SUCCESS", "segmented": True,
        "video_path": str(video), "part1_path": str(p1), "part2_path": str(p2),
    }
    size, sendable = api_mod._recap_output_size_and_sendable(jid, str(video))
    assert size == 80 * MB  # main output size is the FULL video's
    assert sendable is True


def test_recap_helper_segmented_one_part_over_cap_not_sendable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    jid = "rc_50mb_d"
    video = _make_file(tmp_path / "full.mp4", 90 * MB)
    p1 = _make_file(tmp_path / "p1.mp4", 20 * MB)
    p2 = _make_file(tmp_path / "p2.mp4", 55 * MB)
    api_mod.RECAP_JOBS_REGISTRY[jid] = {
        "user_id": 1, "status": "SUCCESS", "segmented": True,
        "video_path": str(video), "part1_path": str(p1), "part2_path": str(p2),
    }
    size, sendable = api_mod._recap_output_size_and_sendable(jid, str(video))
    assert size == 90 * MB
    assert sendable is False


def test_recap_helper_missing_file_keeps_button(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    size, sendable = api_mod._recap_output_size_and_sendable(
        "rc_50mb_e", str(tmp_path / "gone.mp4")
    )
    assert size is None
    assert sendable is True


# ── Voice helper ──────────────────────────────────────────────────────

def test_voice_helper_big_audio_not_sendable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    audio = _make_file(tmp_path / "voice.wav", 60 * MB)
    job = Job(id="voice_50mb_a", result_path=str(audio))
    size, sendable = api_mod._voice_output_size_and_sendable(job)
    assert size == 60 * MB
    assert sendable is False


def test_voice_helper_small_audio_sendable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    audio = _make_file(tmp_path / "voice.wav", 5 * MB)
    job = Job(id="voice_50mb_b", result_path=str(audio))
    assert api_mod._voice_output_size_and_sendable(job) == (5 * MB, True)


def test_voice_helper_missing_file_keeps_button(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    job = Job(id="voice_50mb_c", result_path=str(tmp_path / "gone.wav"))
    assert api_mod._voice_output_size_and_sendable(job) == (None, True)


# ── Recap status payloads (registry + DB branches) ────────────────────

@pytest.mark.asyncio
async def test_recap_status_registry_big_video_not_sendable(db_session, tmp_path):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    jid = "rc_50mb_status_reg"
    video = _make_file(tmp_path / "final.mp4", 60 * MB)
    api_mod.RECAP_JOBS_REGISTRY[jid] = {
        "user_id": owner.id, "status": "SUCCESS", "video_path": str(video),
        "download_urls": {"video": f"/api/recap/download/{jid}/video"},
    }
    res = await api_mod.get_recap_status(jid, user=owner, db=db_session)
    assert res["output_size_bytes"] == 60 * MB
    assert res["telegram_sendable"] is False


@pytest.mark.asyncio
async def test_recap_status_registry_small_video_sendable(db_session, tmp_path):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    jid = "rc_50mb_status_small"
    video = _make_file(tmp_path / "final.mp4", 12 * MB)
    api_mod.RECAP_JOBS_REGISTRY[jid] = {
        "user_id": owner.id, "status": "SUCCESS", "video_path": str(video),
        "download_urls": {"video": f"/api/recap/download/{jid}/video"},
    }
    res = await api_mod.get_recap_status(jid, user=owner, db=db_session)
    assert res["output_size_bytes"] == 12 * MB
    assert res["telegram_sendable"] is True


@pytest.mark.asyncio
async def test_recap_status_registry_segmented_sendable(db_session, tmp_path):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    jid = "rc_50mb_status_seg"
    video = _make_file(tmp_path / "full.mp4", 80 * MB)
    p1 = _make_file(tmp_path / "p1.mp4", 30 * MB)
    p2 = _make_file(tmp_path / "p2.mp4", 30 * MB)
    api_mod.RECAP_JOBS_REGISTRY[jid] = {
        "user_id": owner.id, "status": "SUCCESS", "segmented": True,
        "video_path": str(video), "part1_path": str(p1), "part2_path": str(p2),
        "download_urls": {"video": f"/api/recap/download/{jid}/video"},
    }
    res = await api_mod.get_recap_status(jid, user=owner, db=db_session)
    assert res["output_size_bytes"] == 80 * MB
    assert res["telegram_sendable"] is True


@pytest.mark.asyncio
async def test_recap_status_db_branch_big_video_not_sendable(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    jid = "rc_50mb_status_db"
    video = _make_file(tmp_path / "final.mp4", 60 * MB)
    db_session.add(Job(
        id=jid, user_id=owner.id, status=JobStatus.SUCCESS.value,
        job_type=JobType.RECAP.value, engine="recap", result_path=str(video),
    ))
    await db_session.commit()
    res = await api_mod.get_recap_status(jid, user=owner, db=db_session)
    assert res["output_size_bytes"] == 60 * MB
    assert res["telegram_sendable"] is False


# ── Voice status payload ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_voice_status_big_audio_not_sendable(db_session, tmp_path):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    audio = _make_file(tmp_path / "voice.wav", 60 * MB)
    db_session.add(Job(
        id="voice_50mb_status_big", user_id=owner.id,
        status=JobStatus.SUCCESS.value, job_type=JobType.VOICE_CLONE.value,
        engine="voxcpm", result_path=str(audio),
    ))
    await db_session.commit()
    res = await api_mod.get_job_status("voice_50mb_status_big", user=owner, db=db_session)
    assert res["output_size_bytes"] == 60 * MB
    assert res["telegram_sendable"] is False


@pytest.mark.asyncio
async def test_voice_status_small_audio_sendable(db_session, tmp_path):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    audio = _make_file(tmp_path / "voice.wav", 3 * MB)
    db_session.add(Job(
        id="voice_50mb_status_small", user_id=owner.id,
        status=JobStatus.SUCCESS.value, job_type=JobType.VOICE_CLONE.value,
        engine="voxcpm", result_path=str(audio),
    ))
    await db_session.commit()
    res = await api_mod.get_job_status("voice_50mb_status_small", user=owner, db=db_session)
    assert res["output_size_bytes"] == 3 * MB
    assert res["telegram_sendable"] is True


# ── History items ─────────────────────────────────────────────────────

async def _history_by_id(db_session, owner):
    hist = await api_mod.get_user_history(user=owner, db=db_session)
    return {x["id"]: x for x in hist["history"]}


@pytest.mark.asyncio
async def test_history_recap_and_voice_flags(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    now = datetime.datetime.now(datetime.timezone.utc)

    big_video = _make_file(tmp_path / "big.mp4", 60 * MB)
    db_session.add(Job(
        id="rc_50mb_hist_big", user_id=owner.id, status=JobStatus.SUCCESS.value,
        job_type=JobType.RECAP.value, engine="recap",
        input_text="https://example.com/x", result_path=str(big_video),
        created_at=now,
    ))
    small_video = _make_file(tmp_path / "small.mp4", 8 * MB)
    db_session.add(Job(
        id="rc_50mb_hist_small", user_id=owner.id, status=JobStatus.SUCCESS.value,
        job_type=JobType.RECAP.value, engine="recap",
        input_text="https://example.com/y", result_path=str(small_video),
        created_at=now,
    ))
    big_wav = _make_file(tmp_path / "big.wav", 60 * MB)
    db_session.add(Job(
        id="voice_50mb_hist_big", user_id=owner.id, status=JobStatus.SUCCESS.value,
        job_type=JobType.VOICE_CLONE.value, engine="voxcpm",
        input_text="hello", result_path=str(big_wav), created_at=now,
    ))
    small_wav = _make_file(tmp_path / "small.wav", 4 * MB)
    db_session.add(Job(
        id="voice_50mb_hist_small", user_id=owner.id, status=JobStatus.SUCCESS.value,
        job_type=JobType.VOICE_CLONE.value, engine="voxcpm",
        input_text="hi", result_path=str(small_wav), created_at=now,
    ))
    # SUCCESS row whose file is gone: size unknown -> button kept.
    db_session.add(Job(
        id="voice_50mb_hist_gone", user_id=owner.id, status=JobStatus.SUCCESS.value,
        job_type=JobType.VOICE_CLONE.value, engine="voxcpm",
        input_text="gone", result_path=str(tmp_path / "nope.wav"), created_at=now,
    ))
    await db_session.commit()

    by_id = await _history_by_id(db_session, owner)

    big = by_id["rc_50mb_hist_big"]
    assert big["output_size_bytes"] == 60 * MB
    assert big["telegram_sendable"] is False

    small = by_id["rc_50mb_hist_small"]
    assert small["output_size_bytes"] == 8 * MB
    assert small["telegram_sendable"] is True

    vbig = by_id["voice_50mb_hist_big"]
    assert vbig["output_size_bytes"] == 60 * MB
    assert vbig["telegram_sendable"] is False

    vsmall = by_id["voice_50mb_hist_small"]
    assert vsmall["output_size_bytes"] == 4 * MB
    assert vsmall["telegram_sendable"] is True

    gone = by_id["voice_50mb_hist_gone"]
    assert gone["output_size_bytes"] is None
    assert gone["telegram_sendable"] is True


@pytest.mark.asyncio
async def test_history_segmented_recap_sendable(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    now = datetime.datetime.now(datetime.timezone.utc)
    jid = "rc_50mb_hist_seg"
    video = _make_file(tmp_path / "full.mp4", 80 * MB)
    p1 = _make_file(tmp_path / "p1.mp4", 25 * MB)
    p2 = _make_file(tmp_path / "p2.mp4", 25 * MB)
    api_mod.RECAP_JOBS_REGISTRY[jid] = {
        "user_id": owner.id, "status": "SUCCESS", "segmented": True,
        "video_path": str(video), "part1_path": str(p1), "part2_path": str(p2),
        "download_urls": {"video": f"/api/recap/stream/{jid}/video"},
    }
    db_session.add(Job(
        id=jid, user_id=owner.id, status=JobStatus.SUCCESS.value,
        job_type=JobType.RECAP.value, engine="recap",
        input_text="https://example.com/seg", result_path=str(video),
        created_at=now,
    ))
    await db_session.commit()

    by_id = await _history_by_id(db_session, owner)
    item = by_id[jid]
    assert item["output_size_bytes"] == 80 * MB
    assert item["telegram_sendable"] is True

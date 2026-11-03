"""Recap download URL signing pins (fix 2026-11-03).

Background: users reported recap downloads failing. The recap result
screen's main download buttons open the download URL in the device
browser via Telegram openLink, where no initData header exists — and
the URLs handed out by the status/history serializers were PLAIN, so
the download endpoint answered 401 "Telegram authentication required"
(9x video + 1x srt in the 2026-11-03 production logs, all from
Chrome/Safari browsers).

The signed stream-token machinery already existed, but
``_with_stream_url`` only added a separate ``stream_url`` for the
<video> player and left every download URL unsigned. The fix extends
it to append each file's own scoped token (?t=) to the video, audio,
srt and script URLs and to every split-part URL, and routes the
/api/history recap deliverables through the same signing wrapper.

These tests pin:
- every signed URL's token verifies for exactly its (job, file, user);
- tokens fail across file types, across jobs, when tampered, and when
  expired;
- stream_url behaviour is unchanged;
- with no bot token configured, URLs pass through unsigned (the
  pre-fix behaviour — signing is simply unavailable);
- signing never mutates the stored registry URLs and never stacks
  tokens when re-applied;
- the download endpoint accepts a valid token with no header user,
  and still rejects wrong-scope / stranger / missing tokens;
- /api/history signs recap deliverables for their owner and leaves
  voice audio URLs unsigned (that endpoint verifies headers only).
"""

import datetime
import hashlib
import hmac
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from starlette.responses import FileResponse

from app.config import settings
from app.models.enums import JobStatus, JobType
from app.models.schema import Job
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod

TEST_SECRET = "123456:unit-test-dummy-token"
JOB_ID = "rc_dl_token_1"
OWNER_TG = 880001
STRANGER_TG = 880002


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", TEST_SECRET)


def _token_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["t"][0]


def _sample_urls(job_id: str = JOB_ID):
    return {
        "video": f"/api/recap/download/{job_id}/video",
        "audio": f"/api/recap/download/{job_id}/audio",
        "srt": f"/api/recap/download/{job_id}/srt",
        "script": f"/api/recap/download/{job_id}/script",
        "parts": [
            {"n": 1, "label": "Part 1", "url": f"/api/recap/download/{job_id}/part1"},
            {"n": 2, "label": "", "url": f"/api/recap/download/{job_id}/part2"},
        ],
    }


# ── (a) Every download URL gets a verifiable, correctly-scoped token ──

def test_with_stream_url_signs_every_download_url():
    out = api_mod._with_stream_url(_sample_urls(), JOB_ID, 4242)

    for key in ("video", "audio", "srt", "script"):
        url = out[key]
        assert url.startswith(f"/api/recap/download/{JOB_ID}/{key}?t=")
        assert api_mod._verify_stream_token(_token_of(url), JOB_ID, key) == 4242

    assert len(out["parts"]) == 2
    for part in out["parts"]:
        url = part["url"]
        assert url.startswith(f"/api/recap/download/{JOB_ID}/part{part['n']}?t=")
        assert api_mod._verify_stream_token(
            _token_of(url), JOB_ID, f"part{part['n']}"
        ) == 4242
        # Non-URL fields of a part entry survive signing untouched.
        assert part["label"] in ("Part 1", "")


def test_with_stream_url_signs_stream_style_urls_and_existing_queries():
    # History deliverables use /stream/ paths for video+audio; the
    # token must land there too, and a URL that already has a query
    # string takes &t= instead of a second '?'.
    out = api_mod._with_stream_url(
        {
            "video": f"/api/recap/stream/{JOB_ID}/video",
            "audio": f"/api/recap/stream/{JOB_ID}/audio?x=1",
        },
        JOB_ID,
        4242,
    )
    assert api_mod._verify_stream_token(_token_of(out["video"]), JOB_ID, "video") == 4242
    assert "&t=" in out["audio"]
    assert api_mod._verify_stream_token(_token_of(out["audio"]), JOB_ID, "audio") == 4242


# ── (b) Scope: wrong file, wrong job, tampered, expired all fail ──────

def test_signed_tokens_fail_out_of_scope():
    out = api_mod._with_stream_url(_sample_urls(), JOB_ID, 4242)
    video_tok = _token_of(out["video"])

    # A video token must not open the audio/SRT/part files…
    assert api_mod._verify_stream_token(video_tok, JOB_ID, "audio") is None
    assert api_mod._verify_stream_token(video_tok, JOB_ID, "srt") is None
    assert api_mod._verify_stream_token(video_tok, JOB_ID, "part1") is None
    # …nor another job's video…
    assert api_mod._verify_stream_token(video_tok, "rc_other_job", "video") is None
    # …and a part token must not open a different part.
    part1_tok = _token_of(out["parts"][0]["url"])
    assert api_mod._verify_stream_token(part1_tok, JOB_ID, "part2") is None

    # Tampered signature.
    uid, exp, sig = video_tok.split(".")
    forged = f"{uid}.{exp}." + ("0" if sig[0] != "0" else "1") + sig[1:]
    assert api_mod._verify_stream_token(forged, JOB_ID, "video") is None
    # Tampered user id segment.
    forged_uid = f"999999.{exp}.{sig}"
    assert api_mod._verify_stream_token(forged_uid, JOB_ID, "video") is None


def test_signed_token_expires():
    # Hand-mint a correctly-signed token whose expiry is in the past.
    exp = int(time.time()) - 10
    msg = f"stream:{JOB_ID}:4242:video:{exp}".encode("utf-8")
    sig = hmac.new(TEST_SECRET.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    expired = f"4242.{exp}.{sig}"
    assert api_mod._verify_stream_token(expired, JOB_ID, "video") is None


# ── (c) stream_url behaviour unchanged ────────────────────────────────

def test_stream_url_unchanged():
    out = api_mod._with_stream_url(_sample_urls(), JOB_ID, 4242)
    assert out["stream_url"].startswith(f"/api/recap/stream/{JOB_ID}/video?t=")
    assert api_mod._verify_stream_token(
        _token_of(out["stream_url"]), JOB_ID, "video"
    ) == 4242
    # No video -> no stream_url (as before).
    out2 = api_mod._with_stream_url({"audio": f"/api/recap/download/{JOB_ID}/audio"}, JOB_ID, 4242)
    assert "stream_url" not in out2


# ── (d) No signing secret -> URLs pass through unchanged ──────────────

def test_no_secret_leaves_urls_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "")
    urls = _sample_urls()
    out = api_mod._with_stream_url(urls, JOB_ID, 4242)
    assert out == urls
    assert "stream_url" not in out


# ── Signing hygiene: no mutation of stored URLs, no token stacking ───

def test_signing_does_not_mutate_input_or_stack_tokens():
    urls = _sample_urls()
    out = api_mod._with_stream_url(urls, JOB_ID, 4242)
    # The registry's stored dict (the input) keeps its plain URLs.
    assert urls["video"] == f"/api/recap/download/{JOB_ID}/video"
    assert urls["parts"][0]["url"] == f"/api/recap/download/{JOB_ID}/part1"
    assert "stream_url" not in urls
    # Re-signing an already-signed payload must not stack ?t= on ?t=.
    out2 = api_mod._with_stream_url(out, JOB_ID, 4242)
    assert out2["video"].count("t=") == 1
    assert out2["parts"][0]["url"].count("t=") == 1
    assert api_mod._verify_stream_token(_token_of(out2["video"]), JOB_ID, "video") == 4242


def test_none_and_odd_entries_pass_through():
    out = api_mod._with_stream_url(
        {
            "video": None,
            "audio": f"/api/recap/download/{JOB_ID}/audio",
            "parts": [{"label": "no n"}, {"n": 3}, "junk"],
        },
        JOB_ID,
        4242,
    )
    assert out["video"] is None
    assert "stream_url" not in out
    assert out["parts"][0] == {"label": "no n"}
    assert out["parts"][1] == {"n": 3}
    assert out["parts"][2] == "junk"


# ── (e) Endpoint level: the token alone authenticates a download ────

def _seed_recap_download(db_session, owner_id, job_id, video_path):
    job = Job(
        id=job_id, user_id=owner_id, status=JobStatus.SUCCESS.value,
        job_type=JobType.RECAP.value, engine="recap",
    )
    db_session.add(job)
    api_mod.RECAP_JOBS_REGISTRY[job_id] = {
        "user_id": owner_id, "status": "SUCCESS", "video_path": str(video_path),
    }
    return job


@pytest.mark.asyncio
async def test_download_endpoint_accepts_valid_token_without_header(db_session, tmp_path):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    video = tmp_path / "final.mp4"
    video.write_bytes(b"\x00" * 2048)
    _seed_recap_download(db_session, owner.id, JOB_ID, video)
    await db_session.commit()
    try:
        token = api_mod._make_stream_token(JOB_ID, owner.id, "video")
        request = SimpleNamespace(url=SimpleNamespace(path=f"/api/recap/download/{JOB_ID}/video"))
        res = await api_mod.download_recap_file(
            JOB_ID, "video", request=request, user=None, t=token, db=db_session
        )
        assert isinstance(res, FileResponse)
        assert res.status_code == 200
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop(JOB_ID, None)


@pytest.mark.asyncio
async def test_download_endpoint_rejects_wrong_scope_and_strangers(db_session, tmp_path):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    stranger, _ = await get_or_create_user(db_session, telegram_id=STRANGER_TG)
    video = tmp_path / "final.mp4"
    video.write_bytes(b"\x00" * 2048)
    _seed_recap_download(db_session, owner.id, JOB_ID, video)
    await db_session.commit()
    request = SimpleNamespace(url=SimpleNamespace(path=f"/api/recap/download/{JOB_ID}/video"))
    try:
        # No token at all -> 401 (the production failure mode).
        with pytest.raises(HTTPException) as exc:
            await api_mod.download_recap_file(
                JOB_ID, "video", request=request, user=None, t=None, db=db_session
            )
        assert exc.value.status_code == 401

        # Token minted for the audio file must not open the video -> 401.
        audio_token = api_mod._make_stream_token(JOB_ID, owner.id, "audio")
        with pytest.raises(HTTPException) as exc:
            await api_mod.download_recap_file(
                JOB_ID, "video", request=request, user=None, t=audio_token, db=db_session
            )
        assert exc.value.status_code == 401

        # A stranger's own validly-scoped token verifies as the stranger,
        # and the ownership check still refuses them -> 404.
        stranger_token = api_mod._make_stream_token(JOB_ID, stranger.id, "video")
        with pytest.raises(HTTPException) as exc:
            await api_mod.download_recap_file(
                JOB_ID, "video", request=request, user=None, t=stranger_token, db=db_session
            )
        assert exc.value.status_code == 404
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop(JOB_ID, None)


# ── Serializers: status payload + history hand out signed URLs ───────

@pytest.mark.asyncio
async def test_status_payload_signs_download_urls_without_mutating_registry(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    api_mod.RECAP_JOBS_REGISTRY[JOB_ID] = {
        "user_id": owner.id,
        "status": "SUCCESS",
        "download_urls": _sample_urls(JOB_ID),
        "parts": _sample_urls(JOB_ID)["parts"],
    }
    try:
        res = await api_mod.get_recap_status(JOB_ID, user=owner, db=db_session)
        urls = res["download_urls"]
        assert api_mod._verify_stream_token(_token_of(urls["video"]), JOB_ID, "video") == owner.id
        assert api_mod._verify_stream_token(_token_of(urls["srt"]), JOB_ID, "srt") == owner.id
        assert api_mod._verify_stream_token(
            _token_of(urls["parts"][1]["url"]), JOB_ID, "part2"
        ) == owner.id
        # The stored registry entry keeps its plain URLs.
        stored = api_mod.RECAP_JOBS_REGISTRY[JOB_ID]["download_urls"]
        assert stored["video"] == f"/api/recap/download/{JOB_ID}/video"
        assert stored["parts"][0]["url"] == f"/api/recap/download/{JOB_ID}/part1"
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop(JOB_ID, None)


def _make_output(storage_root: Path, job_id: str) -> Path:
    final = (
        storage_root / "recap_output" / job_id / "workspace"
        / "edge_tts_smart_sync" / "final_edge_tts_smart_sync.mp4"
    )
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"\x00" * 5000)
    (final.parent / "voice_edge_tts.wav").write_bytes(b"\x00" * 5000)
    return final


@pytest.mark.asyncio
async def test_history_signs_recap_but_not_voice_urls(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    now = datetime.datetime.now(datetime.timezone.utc)

    final = _make_output(tmp_path, JOB_ID)
    db_session.add(Job(
        id=JOB_ID, user_id=owner.id, status=JobStatus.SUCCESS.value,
        job_type=JobType.RECAP.value, engine="recap", preset_voice="komin",
        input_text="https://example.com/source", result_path=str(final),
        created_at=now,
    ))
    voice_file = tmp_path / "voice_out.wav"
    voice_file.write_bytes(b"RIFF-fake")
    db_session.add(Job(
        id="job_voice_dl_1", user_id=owner.id, status=JobStatus.SUCCESS.value,
        job_type=JobType.VOICE_CLONE.value, engine="voxcpm",
        input_text="hello", result_path=str(voice_file), created_at=now,
    ))
    await db_session.commit()

    hist = await api_mod.get_user_history(user=owner, db=db_session)
    by_id = {x["id"]: x for x in hist["history"]}

    recap = by_id[JOB_ID]
    assert recap["is_expired"] is False
    for key in ("video", "audio", "srt", "script"):
        url = recap["deliverables"][key]
        assert api_mod._verify_stream_token(_token_of(url), JOB_ID, key) == owner.id
    assert recap["download_url"] == recap["deliverables"]["video"]

    voice = by_id["job_voice_dl_1"]
    assert voice["deliverables"]["audio"] == "/api/audio/stream/job_voice_dl_1"
    assert voice["deliverables"]["download"] == "/api/audio/download/job_voice_dl_1"
    assert voice["download_url"] == "/api/audio/stream/job_voice_dl_1"


# ── Premium branded download filenames (2026-11-03 naming scheme) ───

from app.services.download_names import (  # noqa: E402
    branded_download_filename,
    job_tag,
    sanitize_filename_label,
)


def test_branded_names_per_kind():
    assert branded_download_filename(
        "Recap", "mp4", title="Some Movie", job_id="rc_x"
    ) == "VoiceClonePro_Recap_Some_Movie.mp4"
    assert branded_download_filename(
        "Recap", "mp4", title="Some Movie", job_id="rc_x", part=2
    ) == "VoiceClonePro_Recap_Some_Movie_Part02.mp4"
    assert branded_download_filename(
        "RecapAudio", ".mp3", title="Some Movie", job_id="rc_x"
    ) == "VoiceClonePro_RecapAudio_Some_Movie.mp3"
    assert branded_download_filename(
        "Subtitles", "srt", title="Some Movie", job_id="rc_x"
    ) == "VoiceClonePro_Subtitles_Some_Movie.srt"
    assert branded_download_filename(
        "Script", "txt", title="Some Movie", job_id="rc_x"
    ) == "VoiceClonePro_Script_Some_Movie.txt"
    assert branded_download_filename(
        "Voice", "wav", title="komin", job_id="job_x"
    ) == "VoiceClonePro_Voice_komin.wav"


def test_branded_name_sanitizes_label():
    # Path separators, punctuation and control chars are dropped;
    # whitespace collapses to single underscores.
    assert sanitize_filename_label('a/b\\c:d*e?f"g<h>i|j') == "abcdefghij"
    assert sanitize_filename_label("  My   Movie:\tPart 2! \x00") == "My_Movie_Part_2"
    assert sanitize_filename_label("../../etc/passwd") == "etcpasswd"
    assert sanitize_filename_label("") == ""
    assert sanitize_filename_label(None) == ""
    # Burmese labels keep their letters and combining marks.
    label = sanitize_filename_label("မြန်မာဇာတ်ကား အပိုင်း ၁")
    assert label.startswith("မြန်မာ") and "၁" in label


def test_branded_name_fallback_and_cap():
    # No usable title -> job tag (last 6 alphanumerics of the job id).
    assert job_tag("rc_1793abcd1234") == "cd1234"
    assert branded_download_filename(
        "Recap", "mp4", title=None, job_id="rc_1793abcd1234"
    ) == "VoiceClonePro_Recap_cd1234.mp4"
    assert branded_download_filename(
        "Recap", "mp4", title="!!!", job_id="rc_1793abcd1234"
    ) == "VoiceClonePro_Recap_cd1234.mp4"
    # Nothing at all -> neutral placeholder, still branded.
    assert branded_download_filename("Voice", "wav") == "VoiceClonePro_Voice_File.wav"
    # Length cap: whole filename never exceeds ~90 chars, ext preserved.
    name = branded_download_filename(
        "Recap", "mp4", title="Long " * 60, job_id="rc_x", part=12
    )
    assert len(name) <= 90 and name.endswith("_Part12.mp4")
    assert name.startswith("VoiceClonePro_Recap_Long")


@pytest.mark.asyncio
async def test_download_endpoint_uses_branded_content_disposition(db_session, tmp_path):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    video = tmp_path / "final.mp4"
    video.write_bytes(b"\x00" * 2048)
    _seed_recap_download(db_session, owner.id, JOB_ID, video)
    await db_session.commit()
    request = SimpleNamespace(url=SimpleNamespace(path=f"/api/recap/download/{JOB_ID}/video"))
    token = api_mod._make_stream_token(JOB_ID, owner.id, "video")
    try:
        res = await api_mod.download_recap_file(
            JOB_ID, "video", request=request, user=None, t=token, db=db_session
        )
        assert res.status_code == 200
        # No title anywhere for this synthetic job -> job-tag fallback
        # (JOB_ID "rc_dl_token_1" -> tag "token1").
        assert 'filename="VoiceClonePro_Recap_token1.mp4"' in (
            res.headers["content-disposition"]
        )

        # With a registry title, the label is the sanitized title.
        api_mod.RECAP_JOBS_REGISTRY[JOB_ID]["title"] = "Khin Wint Wah: Love Story!"
        res2 = await api_mod.download_recap_file(
            JOB_ID, "video", request=request, user=None, t=token, db=db_session
        )
        assert 'filename="VoiceClonePro_Recap_Khin_Wint_Wah_Love_Story.mp4"' in (
            res2.headers["content-disposition"]
        )
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop(JOB_ID, None)

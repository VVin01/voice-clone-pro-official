"""Batch CJ — Admin job downloads dead on phones (2026-11-07).

Owner report: in the Admin console's job media modal on a real phone,
Play works but Download Video / Audio / SRT / Script and the "Browser
Tab" button do nothing — the buttons paint progress and no file lands.

Diagnosis: the user history serializer signs every deliverable URL
with a scoped stream token (Batch BJ), and downloadAuthFile only
escapes to the device browser via Telegram openLink when the URL
carries that ?t= token; tokenless URLs take the blob path, whose
programmatic <a download> click mobile WebViews silently drop. Both
ADMIN serializers (global history + queue) handed out the RAW
_job_deliverables URLs unsigned, so admin downloads took the dead
path every time. The modal player worked because it fetches with the
auth header (setAuthMediaSource) — a different path entirely.

Batch CJ:
- CJ-1 (server): the admin history and admin queue serializers sign
  deliverables exactly like the user serializer — recap via
  _with_stream_url(..., admin.id), voice/TTS via the Batch BJ
  audio-scope token minted for the admin. Endpoint authorization is
  unchanged: a token's uid is loaded as the user and the ownership
  checks accept owner OR admin, so an admin-scoped token validates
  end-to-end while a stranger's still 404s.
- CJ-2 (client): openAuthFile (the "Browser Tab" button) prefers
  Telegram openLink for token-carrying URLs, same guard shape as
  downloadAuthFile; tokenless URLs and plain browsers keep the blob
  path unchanged.

These tests pin:
- both admin serializers' signed recap + voice/TTS URLs (every
  deliverable entry + download_url), each token verifying for its
  (job, scope) with the ADMIN's uid;
- expired/missing-file rows still handing out no URLs;
- the recap stream + srt download endpoints serving an admin-uid
  token with no header user, and still refusing a stranger's;
- the audio endpoint serving an admin-uid token;
- the client openLink preference + blob fallbacks (node harness);
- the r107 cache-token release pin.
"""

import datetime
import json
import shutil
import subprocess
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
ADMIN_TG = 880201
OWNER_TG = 880202
STRANGER_TG = 880203
RECAP_JOB = "rc_cj_recap_1"
VOICE_JOB = "job_cj_voice_1"
TTS_JOB = "job_cj_tts_1"
GONE_JOB = "rc_cj_gone_1"
GONE_VOICE_JOB = "job_cj_gone_voice_1"

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
API_PY = (ROOT / "app" / "webapp" / "api.py").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", TEST_SECRET)


@pytest.fixture(autouse=True)
def _admin_ids(monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_TG))


def _token_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["t"][0]


async def _admin_and_owner(db_session):
    admin, _ = await get_or_create_user(db_session, telegram_id=ADMIN_TG)
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    assert settings.is_admin(admin.id)
    return admin, owner


def _seed_jobs(db_session, owner_id, tmp_path):
    now = datetime.datetime.now(datetime.timezone.utc)
    video = tmp_path / "final.mp4"
    video.write_bytes(b"\x00" * 2048)
    wav = tmp_path / "voice.wav"
    wav.write_bytes(b"RIFF-fake-audio")
    tts_wav = tmp_path / "tts.wav"
    tts_wav.write_bytes(b"RIFF-fake-audio")
    rows = [
        Job(id=RECAP_JOB, user_id=owner_id, status=JobStatus.SUCCESS.value,
            job_type=JobType.RECAP.value, engine="recap",
            input_text="a recap", result_path=str(video), created_at=now),
        Job(id=VOICE_JOB, user_id=owner_id, status=JobStatus.SUCCESS.value,
            job_type=JobType.VOICE_CLONE.value, engine="voxcpm",
            preset_voice="komin", input_text="hello",
            result_path=str(wav), created_at=now),
        Job(id=TTS_JOB, user_id=owner_id, status=JobStatus.SUCCESS.value,
            job_type=JobType.TTS.value, engine="edge",
            preset_voice="thiha", input_text="hello tts",
            result_path=str(tts_wav), created_at=now),
        # SUCCESS rows whose media is gone: no URLs may be handed out.
        Job(id=GONE_JOB, user_id=owner_id, status=JobStatus.SUCCESS.value,
            job_type=JobType.RECAP.value, engine="recap",
            input_text="gone", result_path=str(tmp_path / "missing.mp4"),
            created_at=now),
        Job(id=GONE_VOICE_JOB, user_id=owner_id, status=JobStatus.SUCCESS.value,
            job_type=JobType.VOICE_CLONE.value, engine="voxcpm",
            preset_voice="komin", input_text="gone",
            result_path=str(tmp_path / "missing.wav"), created_at=now),
    ]
    for row in rows:
        db_session.add(row)
    return rows


def _assert_signed_recap_row(row, admin_id):
    assert row["download_url"].startswith(f"/api/recap/stream/{RECAP_JOB}/video?t=")
    assert api_mod._verify_stream_token(
        _token_of(row["download_url"]), RECAP_JOB, "video"
    ) == admin_id
    deliverables = row["deliverables"]
    for key, base in (
        # Batch CK: the admin video/audio deliverables move to the
        # download route (save, not play); the CJ signing itself is
        # unchanged — same (job, scope) token under the admin's uid.
        ("video", f"/api/recap/download/{RECAP_JOB}/video"),
        ("audio", f"/api/recap/download/{RECAP_JOB}/audio"),
        ("srt", f"/api/recap/download/{RECAP_JOB}/srt"),
        ("script", f"/api/recap/download/{RECAP_JOB}/script"),
    ):
        url = deliverables[key]
        assert url.startswith(base + "?t="), (key, url)
        assert api_mod._verify_stream_token(
            _token_of(url), RECAP_JOB, key
        ) == admin_id, key
    # _with_stream_url also adds the signed player URL.
    assert deliverables["stream_url"].startswith(
        f"/api/recap/stream/{RECAP_JOB}/video?t="
    )


def _assert_signed_audio_row(row, job_id, admin_id):
    assert row["download_url"].startswith(f"/api/audio/stream/{job_id}?t=")
    assert api_mod._verify_stream_token(
        _token_of(row["download_url"]), job_id, "audio"
    ) == admin_id
    assert row["deliverables"]["audio"] == row["download_url"]
    dl = row["deliverables"]["download"]
    assert dl.startswith(f"/api/audio/download/{job_id}?t=")
    assert api_mod._verify_stream_token(_token_of(dl), job_id, "audio") == admin_id


def _assert_unsigned_gone_row(row):
    assert row["download_url"] is None
    assert row["deliverables"] == {}


# ── Source pins: the CJ shapes exist where they belong ──────────────

def test_cj_source_pins():
    # Server: both admin serializers sign (history uses job/user,
    # queue uses j/admin); the user serializer's shapes are untouched.
    assert "_with_stream_url(deliverables, job.id, user.id)" in API_PY
    assert "_with_stream_url(deliverables, j.id, admin.id)" in API_PY
    assert '_make_stream_token(job.id, user.id, "audio")' in API_PY
    assert '_make_stream_token(j.id, admin.id, "audio")' in API_PY
    # Client: the openLink preference lives in openAuthFile, before
    # the blob fetch (downloadAuthFile's BJ branch is separate).
    start = APP_JS.index("async function openAuthFile(url)")
    body = APP_JS[start:start + 2500]
    assert "urlCarriesStreamToken(url) && window.Telegram?.WebApp?.openLink" in body
    assert body.index("window.Telegram.WebApp.openLink(absUrl)") < body.index(
        "fetchAuthBlobUrl(url)"
    )


# ── Server: admin history serializer signs ──────────────────────────

@pytest.mark.asyncio
async def test_cj_admin_history_signs_recap_and_voice_urls(db_session, tmp_path):
    admin, owner = await _admin_and_owner(db_session)
    _seed_jobs(db_session, owner.id, tmp_path)
    await db_session.commit()

    res = await api_mod.get_admin_global_history(user=admin, db=db_session)
    by_id = {x["id"]: x for x in res["history"]}
    _assert_signed_recap_row(by_id[RECAP_JOB], admin.id)
    _assert_signed_audio_row(by_id[VOICE_JOB], VOICE_JOB, admin.id)
    _assert_signed_audio_row(by_id[TTS_JOB], TTS_JOB, admin.id)
    _assert_unsigned_gone_row(by_id[GONE_JOB])
    _assert_unsigned_gone_row(by_id[GONE_VOICE_JOB])


# ── Server: admin queue serializer signs ────────────────────────────

@pytest.mark.asyncio
async def test_cj_admin_queue_signs_recap_and_voice_urls(db_session, tmp_path):
    admin, owner = await _admin_and_owner(db_session)
    _seed_jobs(db_session, owner.id, tmp_path)
    await db_session.commit()

    res = await api_mod.get_admin_queue(admin=admin, db=db_session)
    by_id = {x["id"]: x for x in res["jobs"]}
    _assert_signed_recap_row(by_id[RECAP_JOB], admin.id)
    _assert_signed_audio_row(by_id[VOICE_JOB], VOICE_JOB, admin.id)
    _assert_signed_audio_row(by_id[TTS_JOB], TTS_JOB, admin.id)
    _assert_unsigned_gone_row(by_id[GONE_JOB])
    _assert_unsigned_gone_row(by_id[GONE_VOICE_JOB])


# ── Server: endpoints serve admin-uid tokens, refuse strangers ─────

def _seed_recap_download(db_session, owner_id, job_id, video_path, srt_path):
    job = Job(
        id=job_id, user_id=owner_id, status=JobStatus.SUCCESS.value,
        job_type=JobType.RECAP.value, engine="recap",
    )
    db_session.add(job)
    api_mod.RECAP_JOBS_REGISTRY[job_id] = {
        "user_id": owner_id, "status": "SUCCESS",
        "video_path": str(video_path), "srt_path": str(srt_path),
    }
    return job


@pytest.mark.asyncio
async def test_cj_recap_endpoints_accept_admin_token(db_session, tmp_path):
    admin, owner = await _admin_and_owner(db_session)
    video = tmp_path / "final.mp4"
    video.write_bytes(b"\x00" * 2048)
    srt = tmp_path / "subs.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
    _seed_recap_download(db_session, owner.id, RECAP_JOB, video, srt)
    await db_session.commit()
    try:
        # Stream form (the modal's Download Video URL shape).
        req = SimpleNamespace(
            url=SimpleNamespace(path=f"/api/recap/stream/{RECAP_JOB}/video")
        )
        token = api_mod._make_stream_token(RECAP_JOB, admin.id, "video")
        res = await api_mod.download_recap_file(
            RECAP_JOB, "video", request=req, user=None, t=token, db=db_session
        )
        assert isinstance(res, FileResponse)
        assert res.status_code == 200

        # Download form for the SRT deliverable.
        req2 = SimpleNamespace(
            url=SimpleNamespace(path=f"/api/recap/download/{RECAP_JOB}/srt")
        )
        srt_token = api_mod._make_stream_token(RECAP_JOB, admin.id, "srt")
        res2 = await api_mod.download_recap_file(
            RECAP_JOB, "srt", request=req2, user=None, t=srt_token, db=db_session
        )
        assert isinstance(res2, FileResponse)
        assert res2.status_code == 200
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop(RECAP_JOB, None)


@pytest.mark.asyncio
async def test_cj_recap_endpoint_stranger_token_still_refused(db_session, tmp_path):
    admin, owner = await _admin_and_owner(db_session)
    stranger, _ = await get_or_create_user(db_session, telegram_id=STRANGER_TG)
    video = tmp_path / "final.mp4"
    video.write_bytes(b"\x00" * 2048)
    srt = tmp_path / "subs.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
    _seed_recap_download(db_session, owner.id, RECAP_JOB, video, srt)
    await db_session.commit()
    try:
        req = SimpleNamespace(
            url=SimpleNamespace(path=f"/api/recap/stream/{RECAP_JOB}/video")
        )
        stranger_token = api_mod._make_stream_token(RECAP_JOB, stranger.id, "video")
        with pytest.raises(HTTPException) as exc:
            await api_mod.download_recap_file(
                RECAP_JOB, "video", request=req, user=None,
                t=stranger_token, db=db_session,
            )
        assert exc.value.status_code == 404
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop(RECAP_JOB, None)


@pytest.mark.asyncio
async def test_cj_audio_endpoint_accepts_admin_token(db_session, tmp_path):
    admin, owner = await _admin_and_owner(db_session)
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFF-fake-audio")
    job = Job(
        id=VOICE_JOB, user_id=owner.id, status=JobStatus.SUCCESS.value,
        job_type=JobType.VOICE_CLONE.value, engine="voxcpm",
        preset_voice="komin", input_text="hello", result_path=str(audio),
    )
    db_session.add(job)
    await db_session.commit()

    req = SimpleNamespace(url=SimpleNamespace(path=f"/api/audio/download/{VOICE_JOB}"))
    token = api_mod._make_stream_token(VOICE_JOB, admin.id, "audio")
    res = await api_mod.download_job_audio(
        VOICE_JOB, req, user=None, db=db_session, t=token
    )
    assert isinstance(res, FileResponse)
    assert res.status_code == 200


# ── Client: openAuthFile openLink preference + fallbacks ────────────

def _run_open_harness(scenario_js: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    start = APP_JS.index("// ── Authenticated result media (round-6 P1)")
    end = APP_JS.index("window.openAuthFile = openAuthFile;")
    helpers = APP_JS[start:end]
    script = """
const clicks = [];
const opened = [];
const toasts = [];
let blobSeq = 0;
global.URL.createObjectURL = (blob) => "blob:fake-" + (++blobSeq);
global.URL.revokeObjectURL = () => {};
function getAuthHeaders(json) { return { "X-Telegram-Init-Data": "INIT" }; }
function isAuthFailure(res) { return false; }
function haptic() {}
function showErrorToast(e, kind) { toasts.push(kind); }
function t(k) { return k; }
function tf(k, p) { return k; }
global.document = {
    createElement: (tag) => ({ tag, click() { clicks.push({ href: this.href, target: this.target }); }, remove() {} }),
    body: { appendChild() {} },
    addEventListener() {},
};
const window = { location: { origin: "https://app.example" } };
""" + helpers + scenario_js
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-800:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_cj_openlink_preferred_for_token_url_in_telegram():
    res = _run_open_harness("""
window.Telegram = { WebApp: { openLink: (u) => opened.push(u) } };
global.fetch = async () => { throw new Error("fetch must not run on the openLink path"); };
(async () => {
    await openAuthFile("/api/recap/stream/rc1/video?t=111.222.abc");
    console.log(JSON.stringify({ clicks, opened, toasts }));
    process.exit(0);
})().catch(e => { console.error(String(e)); process.exit(1); });
""")
    assert res["opened"] == ["https://app.example/api/recap/stream/rc1/video?t=111.222.abc"]
    assert res["clicks"] == []
    assert res["toasts"] == []


def test_cj_open_blob_fallback_when_url_has_no_token():
    res = _run_open_harness("""
window.Telegram = { WebApp: { openLink: (u) => opened.push(u) } };
global.fetch = async () => ({ ok: true, status: 200, blob: async () => ({}) });
(async () => {
    await openAuthFile("/api/recap/stream/rc1/video");
    console.log(JSON.stringify({ clicks, opened, toasts }));
    process.exit(0);
})().catch(e => { console.error(String(e)); process.exit(1); });
""")
    assert res["opened"] == []
    assert res["clicks"] and res["clicks"][0]["href"].startswith("blob:")
    assert res["clicks"][0]["target"] == "_blank"


def test_cj_open_blob_fallback_outside_telegram():
    res = _run_open_harness("""
global.fetch = async () => ({ ok: true, status: 200, blob: async () => ({}) });
(async () => {
    await openAuthFile("/api/recap/stream/rc1/video?t=111.222.abc");
    console.log(JSON.stringify({ clicks, opened, toasts }));
    process.exit(0);
})().catch(e => { console.error(String(e)); process.exit(1); });
""")
    assert res["opened"] == []
    assert res["clicks"] and res["clicks"][0]["href"].startswith("blob:")


def test_cj_open_openlink_throw_falls_back_to_blob():
    res = _run_open_harness("""
window.Telegram = { WebApp: { openLink: (u) => { throw new Error("no bridge"); } } };
global.fetch = async () => ({ ok: true, status: 200, blob: async () => ({}) });
(async () => {
    await openAuthFile("/api/recap/stream/rc1/video?t=111.222.abc");
    console.log(JSON.stringify({ clicks, opened, toasts }));
    process.exit(0);
})().catch(e => { console.error(String(e)); process.exit(1); });
""")
    assert res["opened"] == []
    assert res["clicks"] and res["clicks"][0]["href"].startswith("blob:")
    assert res["toasts"] == []


# ── Release pin: cache token r107 ───────────────────────────────────

def test_cj_cache_token_is_r107():
    assert INDEX_HTML.count("20261115_r154") == 4
    assert ("20261104_r" + "106") not in INDEX_HTML

"""Batch BJ — in-app download buttons do nothing on phones (2026-11-06).

Owner report: on a real phone, the Mini App's download buttons don't
deliver files. The delegated download path (downloadAuthFile) fetched
the file as a Blob and programmatically clicked an <a download> on an
object URL — inside Telegram's mobile WebViews that final save step
is silently dropped (no blob-download handler; iOS WKWebView worst),
so the fetch and its progress paint succeeded and then nothing
happened. Recap downloads escaped the bug because their URLs are
self-authenticating (scoped stream tokens) and can be handed to
Telegram.WebApp.openLink, which opens the system browser.

Batch BJ:
- BJ-1 (client): downloadAuthFile prefers openLink when running
  inside the Telegram WebApp AND the URL carries a stream token;
  tokenless URLs, plain browsers and an openLink failure keep the
  existing blob path (Batch AF progress/restore behaviour intact).
- BJ-2 (server): voice/TTS audio joins the recap token scheme. The
  /api/audio/stream|download route now accepts a scoped stream token
  (job, "audio", user) exactly like the recap route (optional header
  user, BLOCKED + maintenance guards, ownership checks unchanged),
  and the payloads that hand audio URLs to the client — the job
  status payload's output_audio_url and /api/history's voice/TTS
  download_url + deliverables — sign them. The Downloader tab's file
  route stays header-only (its URLs are not job-scoped), so its
  client path is unchanged.

These tests pin:
- the client openLink preference + every blob fallback (node
  harness, Batch AF style);
- the signed status/history payload fields and their token scope;
- the audio endpoint accepting a valid token with no header user,
  and rejecting missing / wrong-scope / stranger tokens, with the
  header path and the pre-BJ positional call shape unchanged;
- a token minted before a ban no longer serving the file.
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
VOICE_JOB = "job_bj_voice_1"
TTS_JOB = "job_bj_tts_1"
OWNER_TG = 880101
STRANGER_TG = 880102

APP_JS = (
    Path(__file__).resolve().parents[2] / "app" / "webapp" / "static" / "js" / "app.js"
).read_text(encoding="utf-8")
API_PY = (
    Path(__file__).resolve().parents[2] / "app" / "webapp" / "api.py"
).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _signing_secret(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", TEST_SECRET)


def _token_of(url: str) -> str:
    return parse_qs(urlparse(url).query)["t"][0]


# ── Source pins: the BJ shapes exist where they belong ──────────────

def test_bj_source_pins():
    # Client: the token gate + openLink preference live in
    # downloadAuthFile, before the blob fetch.
    assert "function urlCarriesStreamToken(url)" in APP_JS
    start = APP_JS.index("async function downloadAuthFile(url, filename, el)")
    body = APP_JS[start:start + 4000]
    assert "urlCarriesStreamToken(url) && window.Telegram?.WebApp?.openLink" in body
    assert body.index("window.Telegram.WebApp.openLink(absUrl)") < body.index(
        "fetchAuthBlobUrlWithProgress(url, onProgress)"
    )
    # Server: the audio route mirrors the recap route's token auth,
    # and both user-facing payload producers sign with the audio
    # scope.
    assert 'token_uid = _verify_stream_token(t, job_id, "audio")' in API_PY
    assert '_make_stream_token(job.id, user.id, "audio")' in API_PY
    assert '_make_stream_token(j.id, user.id, "audio")' in API_PY


# ── Client: openLink preference + blob fallbacks (node harness) ─────

def _run_bj_harness(scenario_js: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    start = APP_JS.index("// ── Authenticated result media (round-6 P1)")
    end = APP_JS.index("window.openAuthFile = openAuthFile;")
    helpers = APP_JS[start:end]
    script = """
const paints = [];
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
function t(k) { return { dl_downloading: "DL_INDETERMINATE", dl_downloading_pct: "DL {pct}%" }[k] || k; }
function tf(k, p) { return t(k).split("{pct}").join(String(p.pct)); }
global.document = {
    createElement: (tag) => ({ tag, click() { clicks.push({ href: this.href, download: this.download }); }, remove() {} }),
    body: { appendChild() {} },
    addEventListener() {},
};
function makeEl() {
    const el = {
        _html: "ORIGINAL", dataset: {}, attrs: {},
        classList: {
            _s: new Set(),
            add(c) { this._s.add(c); }, remove(c) { this._s.delete(c); },
            contains(c) { return this._s.has(c); },
        },
        setAttribute(k, v) { this.attrs[k] = v; },
        removeAttribute(k) { delete this.attrs[k]; },
    };
    Object.defineProperty(el, "innerHTML", {
        get() { return this._html; },
        set(v) { this._html = v; paints.push(v); },
    });
    return el;
}
function streamResponse(chunks, total, failAt) {    let i = 0;
    return {
        ok: true, status: 200,
        headers: { get: (n) => n === "content-length" ? (total == null ? null : String(total))
                               : (n === "content-type" ? "audio/wav" : null) },
        body: { getReader: () => ({ read: async () => {
            if (failAt != null && i === failAt) throw new Error("stream died");
            return i < chunks.length ? { done: false, value: chunks[i++] } : { done: true };
        } }) },
        blob: async () => { throw new Error("blob() must not be used when a reader exists"); },
    };
}
const window = { location: { origin: "https://app.example" } };
""" + helpers + scenario_js
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-800:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_bj_openlink_preferred_for_token_url_in_telegram():
    res = _run_bj_harness("""
window.Telegram = { WebApp: { openLink: (u) => opened.push(u) } };
global.fetch = async () => { throw new Error("fetch must not run on the openLink path"); };
(async () => {
    const el = makeEl();
    await downloadAuthFile("/api/audio/download/job1?t=111.222.abc", "voice.wav", el);
    console.log(JSON.stringify({
        paints, clicks, opened, toasts,
        finalHTML: el.innerHTML,
        busyCleared: el.dataset.dlBusy === undefined,
    }));
})().catch(e => { console.error(String(e)); process.exit(1); });
""")
    # The signed URL is handed to the system browser, absolutized
    # against the app origin; no blob fetch, no anchor click.
    assert res["opened"] == ["https://app.example/api/audio/download/job1?t=111.222.abc"]
    assert res["clicks"] == []
    assert res["toasts"] == []
    # AF contract: the button's progress paint is restored at once.
    assert res["paints"][0] == "DL_INDETERMINATE"
    assert res["finalHTML"] == "ORIGINAL"
    assert res["busyCleared"] is True


def test_bj_blob_fallback_when_url_has_no_token():
    res = _run_bj_harness("""
window.Telegram = { WebApp: { openLink: (u) => opened.push(u) } };
global.fetch = async () => streamResponse([new Uint8Array(10)], 10, null);
(async () => {
    const el = makeEl();
    await downloadAuthFile("/api/audio/download/job1", "voice.wav", el);
    console.log(JSON.stringify({ clicks, opened, toasts, finalHTML: el.innerHTML }));
})().catch(e => { console.error(String(e)); process.exit(1); });
""")
    assert res["opened"] == []
    assert res["clicks"] and res["clicks"][0]["href"].startswith("blob:")
    assert res["clicks"][0]["download"] == "voice.wav"
    assert res["finalHTML"] == "ORIGINAL"


def test_bj_blob_fallback_outside_telegram():
    res = _run_bj_harness("""
global.fetch = async () => streamResponse([new Uint8Array(10)], 10, null);
(async () => {
    const el = makeEl();
    await downloadAuthFile("/api/audio/download/job1?t=111.222.abc", "voice.wav", el);
    console.log(JSON.stringify({ clicks, opened, toasts }));
})().catch(e => { console.error(String(e)); process.exit(1); });
""")
    assert res["opened"] == []
    assert res["clicks"] and res["clicks"][0]["href"].startswith("blob:")


def test_bj_openlink_throw_falls_back_to_blob():
    res = _run_bj_harness("""
window.Telegram = { WebApp: { openLink: (u) => { throw new Error("no bridge"); } } };
global.fetch = async () => streamResponse([new Uint8Array(10)], 10, null);
(async () => {
    const el = makeEl();
    await downloadAuthFile("/api/audio/download/job1?t=111.222.abc", "voice.wav", el);
    console.log(JSON.stringify({ clicks, opened, toasts, finalHTML: el.innerHTML }));
})().catch(e => { console.error(String(e)); process.exit(1); });
""")
    assert res["clicks"] and res["clicks"][0]["href"].startswith("blob:")
    assert res["toasts"] == []
    assert res["finalHTML"] == "ORIGINAL"


# ── Server: payloads hand out signed audio URLs ─────────────────────

def _seed_audio_job(db_session, owner_id, job_id, job_type, audio_path, preset="komin"):
    job = Job(
        id=job_id, user_id=owner_id, status=JobStatus.SUCCESS.value,
        job_type=job_type, engine="voxcpm", preset_voice=preset,
        input_text="hello", result_path=str(audio_path),
    )
    db_session.add(job)
    return job


@pytest.mark.asyncio
async def test_bj_status_payload_signs_output_audio_url(db_session, tmp_path):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFF-fake-audio")
    _seed_audio_job(db_session, owner.id, VOICE_JOB, JobType.VOICE_CLONE.value, audio)
    await db_session.commit()

    res = await api_mod.get_job_status(VOICE_JOB, user=owner, db=db_session)
    url = res["output_audio_url"]
    assert url.startswith(f"/api/audio/download/{VOICE_JOB}?t=")
    assert api_mod._verify_stream_token(_token_of(url), VOICE_JOB, "audio") == owner.id


@pytest.mark.asyncio
async def test_bj_history_signs_voice_and_tts_urls(db_session, tmp_path):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    now = datetime.datetime.now(datetime.timezone.utc)
    voice_file = tmp_path / "voice_out.wav"
    voice_file.write_bytes(b"RIFF-fake")
    tts_file = tmp_path / "tts_out.wav"
    tts_file.write_bytes(b"RIFF-fake")
    for job_id, job_type, path in (
        (VOICE_JOB, JobType.VOICE_CLONE.value, voice_file),
        (TTS_JOB, JobType.TTS.value, tts_file),
    ):
        job = _seed_audio_job(db_session, owner.id, job_id, job_type, path)
        job.created_at = now
    await db_session.commit()

    hist = await api_mod.get_user_history(user=owner, db=db_session)
    by_id = {x["id"]: x for x in hist["history"]}
    for job_id in (VOICE_JOB, TTS_JOB):
        entry = by_id[job_id]
        assert entry["download_url"].startswith(f"/api/audio/stream/{job_id}?t=")
        assert api_mod._verify_stream_token(
            _token_of(entry["download_url"]), job_id, "audio"
        ) == owner.id
        assert entry["deliverables"]["audio"] == entry["download_url"]
        assert entry["deliverables"]["download"].startswith(
            f"/api/audio/download/{job_id}?t="
        )
        assert api_mod._verify_stream_token(
            _token_of(entry["deliverables"]["download"]), job_id, "audio"
        ) == owner.id


# ── Server: the audio endpoint's token auth ─────────────────────────

def _audio_request(job_id, kind="download"):
    return SimpleNamespace(
        url=SimpleNamespace(path=f"/api/audio/{kind}/{job_id}")
    )


@pytest.mark.asyncio
async def test_bj_audio_endpoint_accepts_valid_token_without_header(db_session, tmp_path):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFF-fake-audio")
    _seed_audio_job(db_session, owner.id, VOICE_JOB, JobType.VOICE_CLONE.value, audio)
    await db_session.commit()

    token = api_mod._make_stream_token(VOICE_JOB, owner.id, "audio")
    res = await api_mod.download_job_audio(
        VOICE_JOB, _audio_request(VOICE_JOB), user=None, db=db_session, t=token
    )
    assert isinstance(res, FileResponse)
    assert res.status_code == 200
    assert res.headers["content-disposition"].startswith("attachment")
    assert "VoiceClonePro_Voice_komin.wav" in res.headers["content-disposition"]

    # The /stream/ form of the same route + scope stays inline.
    res2 = await api_mod.download_job_audio(
        VOICE_JOB, _audio_request(VOICE_JOB, "stream"), user=None, db=db_session, t=token
    )
    assert res2.status_code == 200
    assert res2.headers["content-disposition"].startswith("inline")


@pytest.mark.asyncio
async def test_bj_audio_endpoint_rejects_missing_and_wrong_scope_tokens(db_session, tmp_path):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFF-fake-audio")
    _seed_audio_job(db_session, owner.id, VOICE_JOB, JobType.VOICE_CLONE.value, audio)
    await db_session.commit()
    req = _audio_request(VOICE_JOB)

    # No header user, no token -> 401 (the browser-open failure mode).
    with pytest.raises(HTTPException) as exc:
        await api_mod.download_job_audio(VOICE_JOB, req, user=None, db=db_session, t=None)
    assert exc.value.status_code == 401

    # A token minted for a different file scope must not open audio.
    video_token = api_mod._make_stream_token(VOICE_JOB, owner.id, "video")
    with pytest.raises(HTTPException) as exc:
        await api_mod.download_job_audio(
            VOICE_JOB, req, user=None, db=db_session, t=video_token
        )
    assert exc.value.status_code == 401

    # Nor a token minted for another job.
    other_token = api_mod._make_stream_token("job_someone_else", owner.id, "audio")
    with pytest.raises(HTTPException) as exc:
        await api_mod.download_job_audio(
            VOICE_JOB, req, user=None, db=db_session, t=other_token
        )
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_bj_audio_endpoint_stranger_token_still_refused(db_session, tmp_path):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    stranger, _ = await get_or_create_user(db_session, telegram_id=STRANGER_TG)
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFF-fake-audio")
    _seed_audio_job(db_session, owner.id, VOICE_JOB, JobType.VOICE_CLONE.value, audio)
    await db_session.commit()

    # The stranger's own validly-scoped token verifies AS the
    # stranger; the unchanged ownership check then refuses -> 404.
    stranger_token = api_mod._make_stream_token(VOICE_JOB, stranger.id, "audio")
    with pytest.raises(HTTPException) as exc:
        await api_mod.download_job_audio(
            VOICE_JOB, _audio_request(VOICE_JOB),
            user=None, db=db_session, t=stranger_token,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_bj_audio_endpoint_banned_owner_token_refused(db_session, tmp_path):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFF-fake-audio")
    _seed_audio_job(db_session, owner.id, VOICE_JOB, JobType.VOICE_CLONE.value, audio)
    token = api_mod._make_stream_token(VOICE_JOB, owner.id, "audio")
    owner.status = "BLOCKED"
    await db_session.commit()

    with pytest.raises(HTTPException) as exc:
        await api_mod.download_job_audio(
            VOICE_JOB, _audio_request(VOICE_JOB), user=None, db=db_session, t=token
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_bj_audio_endpoint_header_path_and_call_shape_unchanged(db_session, tmp_path):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    audio = tmp_path / "voice.wav"
    audio.write_bytes(b"RIFF-fake-audio")
    _seed_audio_job(db_session, owner.id, VOICE_JOB, JobType.VOICE_CLONE.value, audio)
    await db_session.commit()

    # The pre-BJ positional call shape (job, request, user, db) —
    # the Batch AJ tests call it exactly like this.
    res = await api_mod.download_job_audio(
        VOICE_JOB, _audio_request(VOICE_JOB), owner, db_session
    )
    assert isinstance(res, FileResponse)
    assert res.status_code == 200

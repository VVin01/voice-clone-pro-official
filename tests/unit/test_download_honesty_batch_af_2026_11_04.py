"""Batch AF (2026-11-04): download honesty.

Background (read-only investigation the same day; live observation
the same evening):

1. Send-to-Telegram could lie. POST /api/recap/{job_id}/send-telegram
   awaited _send_recap_telegram_delivery and then returned
   {"success": true} UNCONDITIONALLY, while the helper caught every
   exception into a warning log. The client therefore showed
   "✅ ပို့ပြီးပါပြီ" even when nothing was sent — observed live
   2026-11-04 (toast at ~23:00, no message in the bot chat 14+
   minutes later). The client fetch also had no timeout at all.

2. The download button fetched the ENTIRE file into a Blob before
   any save prompt (downloadAuthFile -> fetchAuthBlobUrl) with no
   progress feedback; failures surfaced only as a generic toast.
   Playback had already been moved off this blob path for exactly
   this large-file reason (R40), downloads had not.

Fixes pinned here:
- the delivery helper returns (delivered, reason); the endpoint
  answers 502 with the coded detail "telegram_delivery_failed" when
  delivery did not happen and {"success": true} only on real
  delivery (same coded-detail style as file_too_large_telegram);
- the client routes the send through fetchWithTimeout with
  TELEGRAM_SEND_TIMEOUT_MS and maps the coded detail to the
  localized toast_telegram_send_failed (my + en);
- downloadAuthFile takes the tapped element from the shared
  data-dl-url handler and paints streamed progress on it
  (percentage from Content-Length when known, an indeterminate
  "downloading…" state otherwise), restoring the element's original
  content on success AND on failure. Voice-job downloads ride the
  same data-dl-url helper, so they get the same feedback.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import settings
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")
API_PY = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")

OWNER_ID = 880101
JOB_ID = "rc_af_test_1"


def _translations() -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    script = """
global.localStorage = { getItem: () => null, setItem: () => {} };
global.document = { querySelectorAll: () => [], documentElement: {}, body: { classList: { toggle: () => {} } }, getElementById: () => null };
global.window = {};
const fs = require('fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const translations = eval(src + '; translations;');
console.log(JSON.stringify(translations));
"""
    out = subprocess.run(
        [node, "-e", script, str(ROOT / "app/webapp/static/js/i18n.js")],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr[-500:]
    return json.loads(out.stdout)


# ── Server: the delivery helper reports its real outcome ──────────

@pytest.fixture
def video_file(tmp_path):
    p = tmp_path / "recap.mp4"
    p.write_bytes(b"\x00" * 1024)
    return p


async def _call_helper(video_file):
    # Batch AP signature update (mechanical): the delivery helper no
    # longer accepts companion-file paths at all, so the former
    # audio_path / srt_path / script_path=None arguments are gone.
    # The AF contract under test — (delivered, reason) honesty — is
    # unchanged.
    return await api_mod._send_recap_telegram_delivery(
        chat_id=OWNER_ID,
        video_path=str(video_file),
        title="Some Title",
        style="Pro Dubbed",
        voice_name="Ko Min",
        duration_sec=0,
        job_id=JOB_ID,
    )


@pytest.mark.asyncio
async def test_helper_reports_failure_when_bot_not_configured(monkeypatch, video_file):
    monkeypatch.setattr(settings, "bot_token", "")
    delivered, reason = await _call_helper(video_file)
    assert delivered is False
    assert reason == "bot_not_configured"


class _FakeSession:
    async def close(self):
        pass


class _FakeBot:
    behaviour = "ok"
    sent: list = []

    def __init__(self, token=None, **_kw):
        self.session = _FakeSession()
        _FakeBot.sent = []

    async def send_video(self, **kw):
        if _FakeBot.behaviour == "raise":
            raise RuntimeError("telegram exploded")
        _FakeBot.sent.append(("video", kw))

    async def send_message(self, **kw):
        _FakeBot.sent.append(("message", kw))

    async def send_document(self, **kw):
        _FakeBot.sent.append(("document", kw))


@pytest.fixture
def fake_bot(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "123456:batch-af-test")
    _FakeBot.behaviour = "ok"
    monkeypatch.setattr("aiogram.Bot", _FakeBot)
    return _FakeBot


@pytest.mark.asyncio
async def test_helper_reports_success_only_when_video_goes_out(fake_bot, video_file):
    delivered, reason = await _call_helper(video_file)
    assert delivered is True
    assert reason is None
    assert [kind for kind, _ in fake_bot.sent] == ["video"]


@pytest.mark.asyncio
async def test_helper_reports_failure_when_send_raises(fake_bot, video_file):
    fake_bot.behaviour = "raise"
    delivered, reason = await _call_helper(video_file)
    assert delivered is False
    assert reason == "RuntimeError"
    assert fake_bot.sent == []


# ── Server: the endpoint answers honestly ─────────────────────────

class _FakeDB:
    async def get(self, model, pk):
        return None


@pytest.fixture
def endpoint_user(monkeypatch, video_file):
    monkeypatch.setitem(
        api_mod.RECAP_JOBS_REGISTRY, JOB_ID, {"user_id": OWNER_ID, "title": "T"}
    )

    def _resolve(job_id, kind, result_path=None):
        return str(video_file) if kind == "video" else None

    monkeypatch.setattr(api_mod, "resolve_recap_deliverable_path", _resolve)
    return SimpleNamespace(id=OWNER_ID)


@pytest.mark.asyncio
async def test_endpoint_502_when_delivery_fails(monkeypatch, endpoint_user):
    async def _fail(**_kw):
        return False, "RuntimeError"

    monkeypatch.setattr(api_mod, "_send_recap_telegram_delivery", _fail)
    with pytest.raises(HTTPException) as ei:
        await api_mod.send_recap_to_telegram(
            job_id=JOB_ID, user=endpoint_user, db=_FakeDB(), _gate=endpoint_user
        )
    assert ei.value.status_code == 502
    assert ei.value.detail == "telegram_delivery_failed"


@pytest.mark.asyncio
async def test_endpoint_success_only_on_real_delivery(monkeypatch, endpoint_user):
    async def _ok(**_kw):
        return True, None

    monkeypatch.setattr(api_mod, "_send_recap_telegram_delivery", _ok)
    out = await api_mod.send_recap_to_telegram(
        job_id=JOB_ID, user=endpoint_user, db=_FakeDB(), _gate=endpoint_user
    )
    assert out["success"] is True


def test_endpoint_source_no_unconditional_success():
    start = API_PY.index('@api_router.post("/recap/{job_id}/send-telegram")')
    body = API_PY[start:API_PY.index('@api_router.post("/voices/{job_id}/send-telegram")', start)]
    assert "delivered, fail_reason = await _send_recap_telegram_delivery(" in body
    assert 'detail="telegram_delivery_failed"' in body
    assert "if not delivered:" in body


# ── Client: send timeout + honest failure branch ──────────────────

def test_client_send_telegram_timeout_and_honest_branch():
    assert "const TELEGRAM_SEND_TIMEOUT_MS = 180000;" in APP_JS
    start = APP_JS.index("async function sendRecapToTelegram(")
    body = APP_JS[start:APP_JS.index("window.sendRecapToTelegram", start)]
    assert "fetchWithTimeout(`/api/recap/${jobId}/send-telegram`" in body
    assert "TELEGRAM_SEND_TIMEOUT_MS" in body
    assert 'data.detail === "telegram_delivery_failed"' in body
    assert 't("toast_telegram_send_failed")' in body
    # The failure branch must come before the ✅ success paint, and the
    # success toast stays gated behind res.ok.
    assert body.index('data.detail === "telegram_delivery_failed"') < body.index('t("toast_telegram_sent")')


# ── Client: download progress (source pins) ───────────────────────

def test_download_progress_source_pins():
    assert "async function fetchAuthBlobUrlWithProgress(url, onProgress)" in APP_JS
    assert "async function _streamedResponseBlob(res, onProgress)" in APP_JS
    assert "async function downloadAuthFile(url, filename, el)" in APP_JS
    # The shared delegated handler hands the tapped element over, so
    # the result panel, History and LiveJobs anchors all get progress.
    assert (
        'downloadAuthFile(a.getAttribute("data-dl-url"), '
        'a.getAttribute("data-dl-name") || a.getAttribute("download") || undefined, a);'
    ) in APP_JS
    # Voice-job downloads ride the same data-dl-url path (result
    # button + History rows), so they inherit the same feedback.
    assert 'btnDl.setAttribute("data-dl-url", data.output_audio_url)' in APP_JS
    assert 'data-dl-url="${j.download_url}"' in APP_JS
    # Playback is untouched: the media-source helper still uses the
    # plain whole-blob fetch, and the recap player still prefers the
    # signed stream URL.
    start = APP_JS.index("async function setAuthMediaSource(")
    assert "await fetchAuthBlobUrl(url)" in APP_JS[start:start + 400]
    assert ".dl-downloading" in STYLE_CSS


def test_new_i18n_keys_exist_in_both_locales():
    tr = _translations()
    for key in ("toast_telegram_send_failed", "dl_downloading", "dl_downloading_pct"):
        assert tr["my"][key], key
        assert tr["en"][key], key
    assert "{pct}" in tr["my"]["dl_downloading_pct"]
    assert "{pct}" in tr["en"]["dl_downloading_pct"]


# ── Client: download progress (node harness, round-6 style) ───────

def _run_download_harness(scenario_js: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    start = APP_JS.index("// ── Authenticated result media (round-6 P1)")
    end = APP_JS.index("window.openAuthFile = openAuthFile;")
    helpers = APP_JS[start:end]
    script = """
const paints = [];
const clicks = [];
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
                               : (n === "content-type" ? "video/mp4" : null) },
        body: { getReader: () => ({ read: async () => {
            if (failAt != null && i === failAt) throw new Error("stream died");
            return i < chunks.length ? { done: false, value: chunks[i++] } : { done: true };
        } }) },
        blob: async () => { throw new Error("blob() must not be used when a reader exists"); },
    };
}
const window = {};
""" + helpers + scenario_js
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-800:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_download_progress_percentage_and_restore_on_success():
    res = _run_download_harness("""
global.fetch = async () => streamResponse([new Uint8Array(50), new Uint8Array(50)], 100, null);
(async () => {
    const el = makeEl();
    await downloadAuthFile("/api/recap/download/job1/video", "movie.mp4", el);
    console.log(JSON.stringify({
        paints, clicks, toasts,
        finalHTML: el.innerHTML,
        busyCleared: el.dataset.dlBusy === undefined,
        classCleared: !el.classList.contains("dl-downloading"),
    }));
})().catch(e => { console.error(String(e)); process.exit(1); });
""")
    assert "DL 50%" in res["paints"]          # real mid-stream percentage
    assert res["paints"][0] == "DL_INDETERMINATE"
    assert res["finalHTML"] == "ORIGINAL"     # label restored
    assert res["busyCleared"] is True and res["classCleared"] is True
    assert res["toasts"] == []
    assert res["clicks"] and res["clicks"][0]["download"] == "movie.mp4"
    assert res["clicks"][0]["href"].startswith("blob:")


def test_download_progress_indeterminate_without_content_length():
    res = _run_download_harness("""
global.fetch = async () => streamResponse([new Uint8Array(10), new Uint8Array(10)], null, null);
(async () => {
    const el = makeEl();
    await downloadAuthFile("/api/recap/download/job1/video", "movie.mp4", el);
    console.log(JSON.stringify({ paints, finalHTML: el.innerHTML, toasts }));
})().catch(e => { console.error(String(e)); process.exit(1); });
""")
    assert res["paints"] and all(p == "DL_INDETERMINATE" for p in res["paints"][:-1])
    assert res["finalHTML"] == "ORIGINAL"
    assert res["toasts"] == []


def test_download_progress_restores_label_on_failure():
    res = _run_download_harness("""
global.fetch = async () => streamResponse([new Uint8Array(50), new Uint8Array(50)], 100, 1);
(async () => {
    const el = makeEl();
    await downloadAuthFile("/api/recap/download/job1/video", "movie.mp4", el);
    console.log(JSON.stringify({
        paints, clicks, toasts,
        finalHTML: el.innerHTML,
        busyCleared: el.dataset.dlBusy === undefined,
        classCleared: !el.classList.contains("dl-downloading"),
    }));
})().catch(e => { console.error(String(e)); process.exit(1); });
""")
    assert "DL 50%" in res["paints"]          # progress was shown before the failure
    assert res["finalHTML"] == "ORIGINAL"     # … and the label still restored
    assert res["busyCleared"] is True and res["classCleared"] is True
    assert res["toasts"] == ["download"]      # failure surfaces as the download toast
    assert res["clicks"] == []                # no save was triggered


# ── Cache token ───────────────────────────────────────────────────

def test_cache_token_is_r64():
    assert INDEX_HTML.count("?v=20261104_r96") == 4
    assert "20261104_r55" not in INDEX_HTML

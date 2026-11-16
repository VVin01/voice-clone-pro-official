"""Batch CK — Admin media modal: dead download clicks + download-route
disposition (2026-11-07).

Owner verdict after CJ shipped: on Ulfa's real phone NONE of the admin
media modal's buttons work (Download Video / Audio / SRT / Script,
Browser Tab) — while user-side downloads work and CJ's serializer
signing was proven live (a TTS row's signed ?t= URL opened fine).

True root cause (code read): the modal's content wrapper in
index.html carries onclick="event.stopPropagation()", and the modal's
download anchors are handled ONLY by a document-level delegated click
listener. Clicks inside the modal die at the wrapper and never reach
document, so downloadAuthFile/openAuthFile never run at all — CJ's
signing could never take effect for this modal. Second layer: recap
video/audio deliverables pointed at the STREAM route, and
download_recap_file forced inline disposition for video/audio file
types regardless of route, so even a live tap would PLAY, not SAVE.

Batch CK:
- CK-1 (client): openAdminMediaModal wires its deliverable anchors
  DIRECTLY (target-phase listeners fire regardless of the ancestor's
  stopPropagation); handlers flag the event __dlHandled and the
  document delegated handler returns early on flagged events, so the
  two paths can never double-fire. The RECAP player prefers
  downloadUrl (stream form) over deliverables.video.
- CK-2 (server): download_recap_file disposition is route-only
  (stream route = inline, download route = attachment for every file
  type). Both ADMIN serializers swap the signed recap video/audio
  deliverables to the download route after the CJ signing (the token
  is bound to job+file type, not the route — it still verifies);
  download_url keeps the signed stream form. The user serializer is
  untouched.

These tests pin:
- the modal wiring in a node mini-DOM with real bubble semantics
  (stopPropagation wrapper): modal anchors fire their handlers
  exactly once; an outside anchor still uses the delegated handler
  exactly once; with the wrapper's stopPropagation removed, a wired
  anchor still fires exactly once (the __dlHandled guard);
- endpoint dispositions: download-route video/audio/srt =
  attachment, stream-route video = inline, for admin-uid tokens;
- admin queue + history recap rows: video/audio deliverables in
  download form with tokens verifying for (job, scope) under the
  ADMIN's uid; download_url stays stream form; the user history
  recap row is UNCHANGED (stream forms, owner uid);
- the r108 cache-token release pin.
"""

import datetime
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.responses import FileResponse

from app.config import settings
from app.models.enums import JobStatus, JobType
from app.models.schema import Job
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod

TEST_SECRET = "123456:unit-test-dummy-token"
ADMIN_TG = 880301
OWNER_TG = 880302
RECAP_JOB = "rc_ck_recap_1"

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


def _seed_recap_job(db_session, owner_id, tmp_path):
    now = datetime.datetime.now(datetime.timezone.utc)
    video = tmp_path / "final.mp4"
    video.write_bytes(b"\x00" * 2048)
    job = Job(
        id=RECAP_JOB, user_id=owner_id, status=JobStatus.SUCCESS.value,
        job_type=JobType.RECAP.value, engine="recap", input_text="a recap",
        result_path=str(video), created_at=now,
    )
    db_session.add(job)
    return job


# ── Source pins: the CK shapes exist where they belong ──────────────

def test_ck_source_pins():
    # CK-1: the delegated handler refuses flagged events before it
    # resolves any anchor.
    start = APP_JS.index("// Delegated handlers:")
    end = APP_JS.index("});", start)
    delegated = APP_JS[start:end]
    assert "if (e.__dlHandled) return;" in delegated
    assert delegated.index("if (e.__dlHandled) return;") < delegated.index(
        "e.target.closest"
    )
    # CK-1: the modal wires its own anchors directly, flagging events.
    assert 'delivEl.querySelectorAll("a[data-dl-url]")' in APP_JS
    assert 'delivEl.querySelectorAll("a[data-open-url]")' in APP_JS
    assert APP_JS.count("e.__dlHandled = true") == 2
    # The wrapper's stopPropagation stays (it keeps content clicks
    # from closing the modal) — CK works around it, never removes it.
    assert 'onclick="event.stopPropagation()"' in INDEX_HTML
    # CK-1: the RECAP player prefers the stream-form downloadUrl.
    assert "const videoUrl = downloadUrl || deliverables.video;" in APP_JS
    assert "const videoUrl = deliverables.video || downloadUrl;" not in APP_JS
    # CK-2: disposition is route-only in the recap endpoint — the
    # file-type inline clause is gone everywhere.
    assert 'or file_type in ["video", "audio"]' not in API_PY
    fn_start = API_PY.index("async def download_recap_file(")
    fn_body = API_PY[fn_start:fn_start + 9000]
    assert 'is_stream = "/stream/" in request.url.path' in fn_body
    # CK-2: both admin serializers apply the download-form swap.
    assert API_PY.count("_recap_deliverables_download_form(deliverables)") == 2


# ── CK-2 helper: prefix swap preserves the token ────────────────────

def test_ck_download_form_swap_preserves_tokens():
    signed = api_mod._with_stream_url(
        {
            "video": f"/api/recap/stream/{RECAP_JOB}/video",
            "audio": f"/api/recap/stream/{RECAP_JOB}/audio",
            "srt": f"/api/recap/download/{RECAP_JOB}/srt",
            "script": f"/api/recap/download/{RECAP_JOB}/script",
        },
        RECAP_JOB,
        4242,
    )
    swapped = api_mod._recap_deliverables_download_form(signed)
    for key in ("video", "audio"):
        url = swapped[key]
        assert url.startswith(f"/api/recap/download/{RECAP_JOB}/{key}?t="), url
        # The token is bound to (job, file type), not the route: the
        # swapped URL's token still verifies for its scope.
        assert api_mod._verify_stream_token(
            _token_of(url), RECAP_JOB, key
        ) == 4242, key
    # Download-route entries pass through byte-identical.
    assert swapped["srt"] == signed["srt"]
    assert swapped["script"] == signed["script"]
    # The player URL keeps the stream form.
    assert swapped["stream_url"].startswith(f"/api/recap/stream/{RECAP_JOB}/video?t=")
    # The input dict is not mutated.
    assert signed["video"].startswith(f"/api/recap/stream/{RECAP_JOB}/video?t=")


# ── CK-2 serializers: admin rows save-form, user rows unchanged ────

def _assert_admin_recap_row(row, admin_id):
    assert row["download_url"].startswith(f"/api/recap/stream/{RECAP_JOB}/video?t=")
    assert api_mod._verify_stream_token(
        _token_of(row["download_url"]), RECAP_JOB, "video"
    ) == admin_id
    deliverables = row["deliverables"]
    for key, base in (
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
    assert deliverables["stream_url"].startswith(
        f"/api/recap/stream/{RECAP_JOB}/video?t="
    )


@pytest.mark.asyncio
async def test_ck_admin_history_recap_deliverables_use_download_route(db_session, tmp_path):
    admin, owner = await _admin_and_owner(db_session)
    _seed_recap_job(db_session, owner.id, tmp_path)
    await db_session.commit()

    res = await api_mod.get_admin_global_history(user=admin, db=db_session)
    by_id = {x["id"]: x for x in res["history"]}
    _assert_admin_recap_row(by_id[RECAP_JOB], admin.id)


@pytest.mark.asyncio
async def test_ck_admin_queue_recap_deliverables_use_download_route(db_session, tmp_path):
    admin, owner = await _admin_and_owner(db_session)
    _seed_recap_job(db_session, owner.id, tmp_path)
    await db_session.commit()

    res = await api_mod.get_admin_queue(admin=admin, db=db_session)
    by_id = {x["id"]: x for x in res["jobs"]}
    _assert_admin_recap_row(by_id[RECAP_JOB], admin.id)


@pytest.mark.asyncio
async def test_ck_user_history_recap_deliverables_unchanged(db_session, tmp_path):
    admin, owner = await _admin_and_owner(db_session)
    _seed_recap_job(db_session, owner.id, tmp_path)
    await db_session.commit()

    hist = await api_mod.get_user_history(user=owner, db=db_session)
    by_id = {x["id"]: x for x in hist["history"]}
    row = by_id[RECAP_JOB]
    # The user serializer keeps the CJ/BJ stream forms throughout —
    # CK's download-form swap is admin-only.
    assert row["download_url"].startswith(f"/api/recap/stream/{RECAP_JOB}/video?t=")
    assert api_mod._verify_stream_token(
        _token_of(row["download_url"]), RECAP_JOB, "video"
    ) == owner.id
    for key in ("video", "audio"):
        url = row["deliverables"][key]
        assert url.startswith(f"/api/recap/stream/{RECAP_JOB}/{key}?t="), (key, url)
        assert api_mod._verify_stream_token(
            _token_of(url), RECAP_JOB, key
        ) == owner.id, key


# ── CK-2 endpoint: route-only disposition ───────────────────────────

def _seed_recap_download(db_session, owner_id, tmp_path):
    video = tmp_path / "final.mp4"
    video.write_bytes(b"\x00" * 2048)
    audio = tmp_path / "narration.wav"
    audio.write_bytes(b"RIFF-fake-audio")
    srt = tmp_path / "subs.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhi\n", encoding="utf-8")
    job = Job(
        id=RECAP_JOB, user_id=owner_id, status=JobStatus.SUCCESS.value,
        job_type=JobType.RECAP.value, engine="recap",
    )
    db_session.add(job)
    api_mod.RECAP_JOBS_REGISTRY[RECAP_JOB] = {
        "user_id": owner_id, "status": "SUCCESS",
        "video_path": str(video), "audio_path": str(audio),
        "srt_path": str(srt),
    }
    return job


async def _get_recap(db_session, route, file_type, token):
    req = SimpleNamespace(
        url=SimpleNamespace(path=f"/api/recap/{route}/{RECAP_JOB}/{file_type}")
    )
    return await api_mod.download_recap_file(
        RECAP_JOB, file_type, request=req, user=None, t=token, db=db_session
    )


@pytest.mark.asyncio
async def test_ck_download_route_video_is_attachment(db_session, tmp_path):
    admin, owner = await _admin_and_owner(db_session)
    _seed_recap_download(db_session, owner.id, tmp_path)
    await db_session.commit()
    try:
        token = api_mod._make_stream_token(RECAP_JOB, admin.id, "video")
        res = await _get_recap(db_session, "download", "video", token)
        assert isinstance(res, FileResponse)
        assert res.headers["content-disposition"].startswith("attachment")
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop(RECAP_JOB, None)


@pytest.mark.asyncio
async def test_ck_stream_route_video_stays_inline(db_session, tmp_path):
    admin, owner = await _admin_and_owner(db_session)
    _seed_recap_download(db_session, owner.id, tmp_path)
    await db_session.commit()
    try:
        token = api_mod._make_stream_token(RECAP_JOB, admin.id, "video")
        res = await _get_recap(db_session, "stream", "video", token)
        assert isinstance(res, FileResponse)
        assert res.headers["content-disposition"].startswith("inline")
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop(RECAP_JOB, None)


@pytest.mark.asyncio
async def test_ck_download_route_audio_and_srt_are_attachment(db_session, tmp_path):
    admin, owner = await _admin_and_owner(db_session)
    _seed_recap_download(db_session, owner.id, tmp_path)
    await db_session.commit()
    try:
        for file_type in ("audio", "srt"):
            token = api_mod._make_stream_token(RECAP_JOB, admin.id, file_type)
            res = await _get_recap(db_session, "download", file_type, token)
            assert isinstance(res, FileResponse)
            assert res.headers["content-disposition"].startswith(
                "attachment"
            ), file_type
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop(RECAP_JOB, None)


# ── CK-1 client: modal wiring under a stopPropagation wrapper ──────

_MINI_DOM = r"""
class El {
    constructor(tag, attrs) {
        this.tagName = tag;
        this.attrs = attrs || {};
        this.children = [];
        this.parent = null;
        this.listeners = {};
    }
    addEventListener(type, fn) {
        (this.listeners[type] = this.listeners[type] || []).push(fn);
    }
    getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }
    hasAttribute(name) { return name in this.attrs; }
    appendChild(c) { c.parent = this; this.children.push(c); return c; }
    matches(sel) {
        const m = /^a\[data-([a-z-]+)\]$/.exec(sel);
        if (!m) return false;
        return this.tagName === "a" && this.hasAttribute("data-" + m[1]);
    }
    closest(sel) {
        const sels = sel.split(",").map((s) => s.trim());
        let n = this;
        while (n) {
            if (n.matches && sels.some((s) => n.matches(s))) return n;
            n = n.parent;
        }
        return null;
    }
    querySelectorAll(sel) {
        const out = [];
        const walk = (n) => {
            for (const c of n.children) {
                if (c.matches(sel)) out.push(c);
                walk(c);
            }
        };
        walk(this);
        return out;
    }
}
function dispatchClick(el) {
    const e = {
        target: el,
        defaultPrevented: false,
        _stopped: false,
        preventDefault() { this.defaultPrevented = true; },
        stopPropagation() { this._stopped = true; },
    };
    const path = [];
    let n = el;
    while (n) { path.push(n); n = n.parent; }
    for (const node of path) {
        const fns = (node.listeners && node.listeners.click) || [];
        for (const fn of fns) fn(e);
        if (e._stopped) break;
    }
    return e;
}
const document = new El("#document");
const dlCalls = [];
const openCalls = [];
function downloadAuthFile(url, name, el) { dlCalls.push({ url, name }); }
function openAuthFile(url) { openCalls.push(url); }
function buildModal(stopPropagates) {
    const modalRoot = document.appendChild(new El("div", { id: "admin-media-modal" }));
    const wrapper = modalRoot.appendChild(new El("div", { class: "modal-content admin-media-content" }));
    if (stopPropagates) {
        // The index.html wrapper: onclick="event.stopPropagation()".
        wrapper.addEventListener("click", (e) => e.stopPropagation());
    }
    const delivEl = wrapper.appendChild(new El("div", { id: "admin-media-deliverables" }));
    return { modalRoot, wrapper, delivEl };
}
"""


def _delegated_handler_js() -> str:
    start = APP_JS.index("// Delegated handlers:")
    end = APP_JS.index("});", start) + 3
    return APP_JS[start:end]


def _ck_wiring_js() -> str:
    start = APP_JS.index("// Batch CK: wire these anchors DIRECTLY.")
    end = APP_JS.index('modal.classList.remove("hidden")', start)
    body = APP_JS[start:end].rstrip()
    assert body.endswith("}")
    # Drop the closing brace of the enclosing if (delivEl) block: the
    # harness supplies delivEl itself and runs the wiring statements.
    return body[: body.rfind("}")]


def _run_modal_harness(scenario_js: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    script = (
        _MINI_DOM
        + _delegated_handler_js()
        + "\n"
        + "const WIRING = " + json.dumps(_ck_wiring_js()) + ";\n"
        + scenario_js
    )
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-800:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_ck_modal_dl_anchor_fires_despite_stop_propagation():
    res = _run_modal_harness("""
const { delivEl } = buildModal(true);
delivEl.appendChild(new El("a", {
    href: "#",
    "data-dl-url": "/api/recap/download/rc1/video?t=111.222.abc",
    "data-dl-name": "VoiceClonePro_Recap.mp4",
}));
eval(WIRING);
const a = delivEl.children[0];
const e = dispatchClick(a);
console.log(JSON.stringify({
    dl: dlCalls, open: openCalls,
    prevented: e.defaultPrevented, flagged: e.__dlHandled === true,
}));
""")
    assert res["dl"] == [
        {"url": "/api/recap/download/rc1/video?t=111.222.abc",
         "name": "VoiceClonePro_Recap.mp4"}
    ]
    assert res["open"] == []
    assert res["prevented"] is True
    assert res["flagged"] is True


def test_ck_modal_open_anchor_fires_despite_stop_propagation():
    res = _run_modal_harness("""
const { delivEl } = buildModal(true);
delivEl.appendChild(new El("a", {
    href: "#",
    "data-open-url": "/api/recap/stream/rc1/video?t=111.222.abc",
}));
eval(WIRING);
const e = dispatchClick(delivEl.children[0]);
console.log(JSON.stringify({
    dl: dlCalls, open: openCalls,
    prevented: e.defaultPrevented, flagged: e.__dlHandled === true,
}));
""")
    assert res["open"] == ["/api/recap/stream/rc1/video?t=111.222.abc"]
    assert res["dl"] == []
    assert res["prevented"] is True
    assert res["flagged"] is True


def test_ck_outside_anchor_still_uses_delegated_handler_once():
    res = _run_modal_harness("""
const host = document.appendChild(new El("div", {}));
host.appendChild(new El("a", {
    href: "#",
    "data-dl-url": "/api/recap/download/rc1/srt?t=111.222.abc",
    "data-dl-name": "VoiceClonePro_Subtitles.srt",
}));
const e = dispatchClick(host.children[0]);
console.log(JSON.stringify({ dl: dlCalls, open: openCalls, prevented: e.defaultPrevented }));
""")
    assert res["dl"] == [
        {"url": "/api/recap/download/rc1/srt?t=111.222.abc",
         "name": "VoiceClonePro_Subtitles.srt"}
    ]
    assert res["open"] == []
    assert res["prevented"] is True


def test_ck_wired_anchor_never_double_fires_without_stop_propagation():
    # Future-proofing: if the wrapper's stopPropagation is ever
    # removed, a directly wired anchor's click reaches BOTH its own
    # listener and the document delegated handler — the __dlHandled
    # flag must keep the total at exactly one download.
    res = _run_modal_harness("""
const { delivEl } = buildModal(false);
delivEl.appendChild(new El("a", {
    href: "#",
    "data-dl-url": "/api/recap/download/rc1/video?t=111.222.abc",
    "data-dl-name": "VoiceClonePro_Recap.mp4",
}));
eval(WIRING);
dispatchClick(delivEl.children[0]);
console.log(JSON.stringify({ dl: dlCalls, open: openCalls }));
""")
    assert len(res["dl"]) == 1
    assert res["dl"][0]["url"] == "/api/recap/download/rc1/video?t=111.222.abc"
    assert res["open"] == []


# ── Release pin: cache token r108 ───────────────────────────────────

def test_ck_cache_token_is_r108():
    assert INDEX_HTML.count("20261116_r156") == 4
    assert ("20261104_r" + "107") not in INDEX_HTML

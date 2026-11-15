"""Journey-upgrade batch (2026-11-02) — Release 38 pins.

Built from the hands-on user-journey audit (Telegram Web, admin +
free @Milo_N4U accounts, real jobs run as a user):

1. Admin Overview showed ALL ZEROS live while the bot /admin showed
   199 users / 146 jobs: the endpoint's COUNT queries were correct —
   the stat cards shipped literal "0" placeholders and any failed or
   slow fetch left them standing with no error anywhere. Cards now
   ship "—" and failures paint an error + Retry over the stats area.
2. Downloader honesty: an unverifiable link (example.com) got a
   plausible result card WITH download buttons — the extract payload
   invented a fixed formats list in both branches. formats_available
   now derives from yt-dlp's real format list ([] when none / when
   unverified), the client routes such payloads to the persistent
   error box, and the result card clears when the input is emptied
   or a new extraction starts.
3. Recap failure copy leaked raw yt-dlp text ("Failed to extract any
   player response; please report this issue on
   https://github.com/yt-dlp/…") to the user. The source-error
   classifier now covers the "failed to extract" family plus a
   last-resort catch for raw "ERROR:" extractor strings, and the
   recap failure path classifies before storing AND notifying.
4. Group AI stated the free daily limit as 3: the fact-sheet
   plumbing was already dynamic — the packed sentence put "Recap
   Credit 3" immediately before the daily limit and the model
   conflated them. One fact per line now.
5. Voice ETA honesty: the studio claimed "5–15 seconds" while a real
   job took ~90–110s. Copy now says ~1–2 minutes, and the progress
   ETA line shows real elapsed seconds while polling.
6. Reopen flash: every Mini App reopen flashed the join gate +
   0.0m/0c balances during revalidation. The last /user/me payload
   is cached, painted instantly, and a known verified member starts
   unlocked; the gate appears only on a confirmed non-member verdict.
7. History detail for a finished voice job had Download/Telegram
   only — no player. The detail now carries the same auth-blob
   <audio> player as the Voice result view, wired lazily on open.
8. Free recap terms (2 min • watermark) sat only in a rotating
   carousel slide. A dedicated note now sits under the Recap source
   input, shown to FREE users only.
9. Bot-chat /start on the admin account rendered the admin's FREE
   grants (3.0 min) while the Mini App header shows ADMIN ∞.
   render_main_menu now uses an admin account-info variant with ∞.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.db.session as session_mod
import app.services.downloader as downloader_mod
import app.services.url_safety as url_safety_mod
from app.config import settings
from app.db.base import Base
from app.handlers.menu import render_main_menu
from app.models.enums import AccountCategory, JobStatus, JobType, UserStatus
from app.models.schema import Job, User
from app.services.downloader import classify_source_error
from app.services.group_ai_admin import build_fact_sheet
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "app" / "webapp" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (STATIC / "js" / "i18n.js").read_text(encoding="utf-8")
CSS = (STATIC / "css" / "style.css").read_text(encoding="utf-8")
API_PY = (ROOT / "app" / "webapp" / "api.py").read_text(encoding="utf-8")


def _js_slice(start_marker: str, end_marker: str) -> str:
    i = APP_JS.index(start_marker)
    j = APP_JS.index(end_marker, i + len(start_marker))
    return APP_JS[i:j]


def _user(uid: int, **kw) -> User:
    return User(
        id=uid,
        first_name=kw.pop("first_name", "Test"),
        username=kw.pop("username", None),
        account_category=kw.pop("account_category", AccountCategory.FREE.value),
        language_code="my",
        **kw,
    )


# ── Statics token ───────────────────────────────────────────────────

def test_statics_token_r38():
    assert HTML.count("?v=20261115_r153") == 4
    assert "20261102_r37" not in HTML


# ── 1. Admin Overview zeros ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_overview_endpoint_returns_real_counts(db_session):
    admin = _user(999101)
    free = _user(700101)
    vip = _user(700102, account_category=AccountCategory.PURCHASED.value)
    db_session.add_all([admin, free, vip])
    await db_session.commit()
    db_session.add_all([
        Job(id="ov-q1", user_id=700101, status=JobStatus.QUEUED.value,
            job_type=JobType.VOICE_CLONE.value, engine="voxcpm", input_text="x"),
        Job(id="ov-f1", user_id=700101, status=JobStatus.FAILED.value,
            job_type=JobType.VOICE_CLONE.value, engine="voxcpm", input_text="x"),
        Job(id="ov-s1", user_id=700102, status=JobStatus.SUCCESS.value,
            job_type=JobType.RECAP.value, engine="recap", input_text="x"),
    ])
    await db_session.commit()
    data = await api_mod.get_admin_overview(admin, db_session)
    assert data["total_users"] == 3
    assert data["vip_users"] == 1
    assert data["queued_jobs"] == 1
    assert data["failed_jobs_24h"] == 1
    assert data["jobs_by_status"][JobStatus.SUCCESS.value] == 1


def test_overview_cards_ship_dash_not_zero():
    for el_id in (
        "adm-total-users", "adm-vip-users", "adm-pending-slips",
        "adm-queued-jobs", "adm-failed-24h", "adm-banned-users",
        "adm-new-24h",
    ):
        assert f'id="{el_id}">—</span>' in HTML, el_id
        assert f'id="{el_id}">0</span>' not in HTML, el_id


def test_overview_failure_paints_error_state():
    assert 'id="adm-overview-error"' in HTML
    body = _js_slice("async function loadAdminData()", "// Pending payment slips")
    assert "adm-overview-error" in body
    # Both failure shapes route to the error state: non-OK response
    # (else branch) and a thrown/timed-out fetch (catch).
    assert body.count("admOverviewError();") >= 2
    assert "admStateError(host" in body


# ── 2. Downloader honesty ───────────────────────────────────────────

class _FakeDownloader:
    info: dict | None = None
    exc: Exception | None = None

    def get_info(self, url):
        if type(self).exc is not None:
            raise type(self).exc
        return dict(type(self).info or {})


def _patch_extract(monkeypatch, info=None, exc=None):
    _FakeDownloader.info = info
    _FakeDownloader.exc = exc
    monkeypatch.setattr(downloader_mod, "MediaDownloader", _FakeDownloader)
    monkeypatch.setattr(downloader_mod, "unshorten_url", lambda u: u)
    monkeypatch.setattr(url_safety_mod, "validate_public_url", lambda u: u)


def _extract_req(url="https://example.com/notavideo"):
    return api_mod.ExtractMediaRequest(url=url)


@pytest.mark.asyncio
async def test_extract_fallback_offers_no_formats(monkeypatch):
    _patch_extract(monkeypatch, exc=RuntimeError("no extractor for this"))
    user = _user(700201)
    data = await api_mod.extract_media_info(_extract_req(), user, user)
    assert data["metadata_verified"] is False
    assert data["formats_available"] == []


@pytest.mark.asyncio
async def test_extract_verified_with_real_formats(monkeypatch):
    _patch_extract(monkeypatch, info={
        "title": "Real Video", "uploader": "Chan", "duration": 75,
        "thumbnail": "", "width": 1280, "height": 720,
        "has_subtitles": True, "platform": "YouTube",
        "raw_info": {"formats": [{"vcodec": "avc1", "acodec": "mp4a"}]},
    })
    user = _user(700202)
    data = await api_mod.extract_media_info(
        _extract_req("https://www.youtube.com/watch?v=abc"), user, user)
    assert data["metadata_verified"] is True
    assert any("MP4" in f for f in data["formats_available"])
    assert any("MP3" in f for f in data["formats_available"])
    assert any("SRT" in f for f in data["formats_available"])


@pytest.mark.asyncio
async def test_extract_verified_but_no_media_formats(monkeypatch):
    # A generic page yt-dlp "reads" but with zero media streams: the
    # audit's example.com case — nothing downloadable may be offered.
    _patch_extract(monkeypatch, info={
        "title": "Example Domain", "uploader": "Web", "duration": 0,
        "thumbnail": "", "width": None, "height": None,
        "has_subtitles": False, "platform": "Web Media",
        "raw_info": {"formats": []},
    })
    user = _user(700203)
    data = await api_mod.extract_media_info(_extract_req(), user, user)
    assert data["formats_available"] == []


def test_downloader_client_honesty_gate_and_stale_clear():
    body = _js_slice("function setupDownloader()", "// ── Plan Category Tabs")
    # Unverified / format-less payloads route to the error box, never
    # to a result card with download buttons.
    assert "data.metadata_verified === false || !(data.formats_available || []).length" in body
    assert "dlShowExtractError();" in body
    # Stale card clears: at extraction start and when input empties.
    assert "function dlClearResult()" in APP_JS
    assert body.count("dlClearResult();") >= 2
    assert 'dlUrlInput.dataset.dlClearBound' in body
    assert 'addEventListener("input"' in body


# ── 3. Recap failure copy ───────────────────────────────────────────

AUDIT_RAW = (
    "ERROR: [youtube] dQw4w9WgXcQ: Failed to extract any player response; "
    "please report this issue on https://github.com/yt-dlp/yt-dlp/issues , "
    "filling out the appropriate issue template"
)


def test_classifier_failed_to_extract_family():
    msg = classify_source_error(AUDIT_RAW, "https://www.youtube.com/shorts/dQw4w9WgXcQ")
    assert msg is not None
    assert msg.startswith("⚠️")
    assert "ပိတ်ထားပါသည်" in msg  # blocked-family copy
    assert "Video Upload" in msg  # upload guidance
    assert "yt-dlp" not in msg and "Failed to extract" not in msg


def test_classifier_bare_error_prefix_catch():
    # yt-dlp's bracketed extractor-error shape with an unknown message
    # still maps to the generic extraction copy — never raw text.
    msg = classify_source_error(
        "ERROR: [generic] Unable to download webpage: some new failure",
        "https://example.com/v",
    )
    assert msg is not None
    assert msg.startswith("⚠️")
    assert "ERROR" not in msg
    # A bare "ERROR: …" string (no extractor bracket) is NOT
    # necessarily a source error and stays unclassified — the
    # long-standing no-overreach contract (hunt-6 / TikTok-block pins).
    assert classify_source_error(
        "ERROR: some brand-new failure mode",
        "https://www.tiktok.com/@someone/video/7234567890123456789",
    ) is None


def test_classifier_non_source_errors_still_none():
    assert classify_source_error("ffmpeg exited with code 1", "https://x.example/v") is None
    assert classify_source_error("", "https://x.example/v") is None
    # Existing families unchanged.
    assert "ဖွင့်၍ မရပါ" in classify_source_error(
        "ERROR: Video unavailable", "https://www.youtube.com/watch?v=abc")


def test_recap_failure_path_classifies_before_store_and_notify():
    # Audit fix 2026-11-03: the fallback is default-deny now — an
    # unclassified error becomes the generic Burmese technical
    # message, never the raw str(e) this pin used to require.
    assert "display_err = classify_source_error(str(e), req.url) or (" in API_PY
    assert "နည်းပညာဆိုင်ရာ အမှားတစ်ခု ဖြစ်ပေါ်ခဲ့ပါသည်" in API_PY
    assert 'entry["error_message"] = display_err' in API_PY
    assert "error_message=display_err" in API_PY
    # Batch K: the failure DM is composed by the shared helper, which
    # receives the SAME classified display_err and HTML-escapes it
    # (the old inline err_short = html.escape(display_err[:180])).
    assert "reason=display_err" in API_PY
    assert "reason=html.escape(reason_text)" in API_PY


class _ExplodingDownloader:
    def __init__(self):
        raise RuntimeError(AUDIT_RAW)


async def _tmp_maker(tmp_path, monkeypatch, name="journey38.db"):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/{name}", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    return engine, maker


@pytest.mark.asyncio
async def test_recap_job_stores_classified_error(tmp_path, monkeypatch):
    engine, maker = await _tmp_maker(tmp_path, monkeypatch)
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "bot_token", "")
    monkeypatch.setattr(downloader_mod, "MediaDownloader", _ExplodingDownloader)
    monkeypatch.setattr(api_mod, "RECAP_EXECUTION_SEMAPHORE", asyncio.Semaphore(2))

    jid = "rc_journey38"
    async with maker() as s:
        user, _ = await get_or_create_user(s, telegram_id=991038)
        uid = user.id
        s.add(Job(
            id=jid, user_id=uid, chat_id=uid,
            status=JobStatus.QUEUED.value,
            job_type=JobType.RECAP.value, engine="recap",
            preset_voice="komin", input_text="T",
            source_audio_path="https://www.youtube.com/shorts/dQw4w9WgXcQ",
        ))
        await s.commit()
    entry = {
        "id": jid, "user_id": uid, "title": "T", "status": "QUEUED",
        "progress_percent": 5, "progress_step": "0/4",
        "progress_label": "queued", "error_message": None,
        "credit_deducted": False, "credits_charged": 0,
        "credits_estimated": None, "parts": [],
    }
    api_mod.RECAP_JOBS_REGISTRY[jid] = entry
    req = api_mod.RecapConfigRequest(
        url="https://www.youtube.com/shorts/dQw4w9WgXcQ", title="T",
        voice_slug="komin",
    )
    try:
        await asyncio.wait_for(
            api_mod._run_recap_background_job(jid, req, uid, uid),
            timeout=15,
        )
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop(jid, None)
        await engine.dispose()

    assert entry["status"] == "FAILED"
    stored = entry["error_message"] or ""
    assert stored.startswith("⚠️")
    assert "yt-dlp" not in stored and "Failed to extract" not in stored
    async with maker() as s:
        job = await s.get(Job, jid)
        assert job.error_message == stored


# ── 4. Group AI daily-limit fact ────────────────────────────────────

def test_fact_sheet_daily_limit_on_own_line(monkeypatch):
    monkeypatch.setattr(settings, "free_daily_job_limit", 7)
    sheet = build_fact_sheet()
    daily_lines = [ln for ln in sheet.splitlines() if "တစ်နေ့ Job" in ln]
    assert len(daily_lines) == 1
    assert "တစ်နေ့ Job 7 ခုအထိ" in daily_lines[0]
    # The conflation source is gone: the daily-limit line no longer
    # carries the Recap Credit count.
    assert "Recap Credit" not in daily_lines[0]


# ── 5. Voice ETA honesty ────────────────────────────────────────────

def test_voice_eta_copy_and_elapsed():
    assert "၁–၂ မိနစ်" in I18N_JS
    assert "about 1–2 minutes" in I18N_JS
    assert "5–15" not in I18N_JS and "၅-၁၅" not in HTML
    assert 'voice_elapsed: "⏱️ စောင့်ဆိုင်းနေချိန်: {sec} စက္ကန့်"' in I18N_JS
    assert 'voice_elapsed: "⏱️ Waiting: {sec}s"' in I18N_JS
    body = _js_slice("function startJobPolling(jobId)", "// ── Recap duration caps")
    assert "pollStartedAt" in body
    assert body.count('tf("voice_elapsed"') == 2  # QUEUED + RUNNING


# ── 6. Reopen state flash ───────────────────────────────────────────

def test_reopen_cache_boot_paint_and_gate():
    assert 'const USER_ME_CACHE_KEY = "vcp_user_me_cache";' in APP_JS
    body = _js_slice("async function loadUserProfile()", "// ── API: Curated Voices")
    # R40 item 8: the cache write goes through the per-user wrapper
    # (the stored record carries the owner's id so a foreign cached
    # profile can never paint — e.g. an admin's ∞/∞ flashing for a
    # finite user in the same Web client).
    assert "writeCachedUserProfile(data)" in body
    wrapper = _js_slice("function writeCachedUserProfile(data)", "function readCachedUserProfile()")
    assert "owner_id: currentTelegramUserId()" in wrapper
    assert "profile: data" in wrapper
    assert "function paintCachedUserHeader(data)" in APP_JS
    assert "function bootFromCachedProfile()" in APP_JS
    gate = _js_slice("function initEntryGate()", "window.initEntryGate = initEntryGate;")
    assert "if (bootFromCachedProfile()) unlockEntryGate(); else lockEntryGate();" in gate
    # The live check still runs at startup and remains the only lock
    # trigger for a cached user (confirmed non-member verdict).
    assert 'runEntryGateCheck("startup")' in gate


# ── 7. History detail player ────────────────────────────────────────

def test_history_detail_voice_player():
    body = _js_slice("function hubHistoryDetailHtml(j)", "function hubHistoryRowHtml(j)")
    assert "hub-detail-audio" in body
    assert 'data-auth-src="${j.download_url}"' in body
    assert "function wireHubDetailAudio(scope)" in APP_JS
    toggle = _js_slice("function toggleHistoryDetail(jobId)", "window.toggleHistoryDetail = toggleHistoryDetail;")
    assert "wireHubDetailAudio(row)" in toggle
    assert ".hub-detail-audio" in CSS


# ── 8. Free recap terms up front ────────────────────────────────────

def test_free_recap_terms_note_placement_and_visibility():
    note_at = HTML.index('id="recap-free-terms-note"')
    info_at = HTML.index('id="recap-video-info-box"')
    source_at = HTML.index('id="panel-source-file"')
    assert source_at < note_at < info_at  # under the source input area
    assert 'recap_free_terms_note: "🎬 အခမဲ့ အသုံးပြုသူ: အများဆုံး ၂ မိနစ် • Watermark ပါဝင်မည်"' in I18N_JS
    assert 'recap_free_terms_note: "🎬 Free plan: up to 2 minutes • Watermark included"' in I18N_JS
    body = _js_slice("async function loadUserProfile()", "// ── API: Curated Voices")
    assert 'getElementById("recap-free-terms-note")' in body
    assert 'classList.toggle("hidden", !!(data.is_admin || data.is_vip))' in body
    assert ".recap-free-terms" in CSS


# ── 9. Bot-chat /start admin parity ────────────────────────────────

@pytest.mark.asyncio
async def test_main_menu_admin_shows_unlimited(db_session, monkeypatch):
    admin_id = 880138
    monkeypatch.setattr(settings, "admin_ids_raw", str(admin_id))
    user, _ = await get_or_create_user(db_session, telegram_id=admin_id, language_code="my")
    text, _kb = await render_main_menu(user.id, lang="my", session=db_session)
    assert "SYSTEM ADMIN" in text
    assert "∞ (အကန့်အသတ်မဲ့)" in text
    # The admin's underlying FREE grants must not be what renders.
    assert "3.0 မိနစ်" not in text


@pytest.mark.asyncio
async def test_main_menu_free_rendering_unchanged(db_session, monkeypatch):
    monkeypatch.setattr(settings, "admin_ids_raw", "999999999")
    user, _ = await get_or_create_user(db_session, telegram_id=880139, language_code="my")
    text, _kb = await render_main_menu(user.id, lang="my", session=db_session)
    assert "∞" not in text
    assert "3.0 မိနစ်" in text  # fresh free grant, rendered as before

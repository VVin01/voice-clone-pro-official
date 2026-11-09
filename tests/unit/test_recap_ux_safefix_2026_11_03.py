"""Batch K — recap UX SAFE-FIXES (2026-11-03).

Display/notification-only fixes from the recap UX audit:

1. the script-review opt-in gains a visible twin toggle in the main
   options area, bound to the same state as the advanced checkbox;
2. the retention line (24 hours since Batch X) is disclosed on the
   result card and in the completion DM, not only in History;
3. every completed recap shows a per-job receipt (charged credits)
   and failed jobs show a refund marker — the amounts come from the
   server payload, extended here to carry the persisted credit state
   (credits_charged / credit_refunded / credits_refunded) on both
   the status and history serializers;
4. the recap tab states its supported link platforms and the upload
   limits up front, and the file input's accept list matches the
   server upload format list exactly;
5. failed recap jobs get a failure DM (locale copy, job title,
   curated reason, refund confirmation) — fired once per job from
   the runner's terminal failure path and from the SCRIPT_READY
   expiry sweep, which previously expired silently;
6. the voiceover deliverable is an MP3 (engine exports
   ``final.with_suffix(".mp3")`` via libmp3lame) — History branded
   it WAV; both surfaces now say MP3;
7. recap pill controls get a 44px touch floor, scoped to the recap
   tab because .pill-btn is shared with other tabs;
8. the start CTA shows the segmented credit estimate when one is
   known pre-submit instead of the static "(1 Credit)".

Frontend behaviour is pinned with source-level assertions, as in
prior batches; serializer and DM behaviour is tested live.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aiogram
import app.db.session as session_mod
from app.config import settings
from app.db.base import Base
from app.models.enums import JobStatus, JobType
from app.models.schema import Job, User
from app.services.i18n import t
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")
API_SRC = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")
CORE_SRC = (ROOT / "app/services/recap_engine/core.py").read_text(encoding="utf-8")
MESSAGES_MY = json.loads((ROOT / "locales/my/messages.json").read_text(encoding="utf-8"))
MESSAGES_EN = json.loads((ROOT / "locales/en/messages.json").read_text(encoding="utf-8"))

OWNER_TG = 880201

UI_KEYS_K = [
    "recap_script_review_hint",
    "recap_cost_charged_line",
    "recap_refunded_line",
    "btn_start_recap_credits",
    "recap_platforms_note",
    "recap_drop_limits",
]
SERVER_KEYS_K = [
    "recap_retention_note",
    "recap_failure_notify_text",
    "recap_failure_refund_note",
    "recap_failure_upload_hint",
    "recap_failure_reason_script_expired",
]

_JOB_IDS = (
    "rc_k_status_reg", "rc_k_status_db_ref", "rc_k_status_db_plain",
    "rc_k_hist_ref", "rc_k_hist_ok", "rc_k_expire",
)


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    for jid in _JOB_IDS:
        api_mod.RECAP_JOBS_REGISTRY.pop(jid, None)


def _recap_state(**over):
    cfg = api_mod.RecapConfigRequest(
        url="https://example.com/movie",
        title="Batch K Movie",
        voice_slug="mahnin",
    )
    state = {
        "config": cfg.model_dump(),
        "credit_deducted": True,
        "credits_charged": 1,
        "is_vip": False,
        "phase": "script",
        "credits_estimated": None,
        "credit_refunded": False,
        "credits_refunded": 0,
    }
    state.update(over)
    return state


def _job(job_id, user_id, state, status=JobStatus.FAILED.value):
    return Job(
        id=job_id,
        user_id=user_id,
        chat_id=user_id,
        status=status,
        job_type=JobType.RECAP.value,
        engine="recap",
        preset_voice="mahnin",
        input_text="Batch K Movie",
        source_audio_path="https://example.com/movie",
        recap_config=json.dumps(state),
        created_at=datetime.now(timezone.utc),
    )


# ── (3) status serializer: charged credits + refund state ────────────

@pytest.mark.asyncio
async def test_status_registry_branch_carries_receipt_fields(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    jid = "rc_k_status_reg"
    api_mod.RECAP_JOBS_REGISTRY[jid] = {
        "user_id": owner.id, "status": "FAILED",
        "credits_charged": 3, "credit_refunded": True, "credits_refunded": 3,
        "error_message": "boom",
    }
    res = await api_mod.get_recap_status(jid, user=owner, db=db_session)
    assert res["credits_charged"] == 3
    assert res["credit_refunded"] is True
    assert res["credits_refunded"] == 3


@pytest.mark.asyncio
async def test_status_db_branch_reads_persisted_receipt(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    refunded = _recap_state(
        credits_charged=2, credit_refunded=True, credits_refunded=2,
    )
    db_session.add(_job("rc_k_status_db_ref", owner.id, refunded))
    await db_session.commit()
    res = await api_mod.get_recap_status(
        "rc_k_status_db_ref", user=owner, db=db_session
    )
    assert res["credits_charged"] == 2
    assert res["credit_refunded"] is True
    assert res["credits_refunded"] == 2


@pytest.mark.asyncio
async def test_status_db_branch_non_refunded_job(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    plain = _recap_state(credits_charged=1)
    db_session.add(_job("rc_k_status_db_plain", owner.id, plain))
    await db_session.commit()
    res = await api_mod.get_recap_status(
        "rc_k_status_db_plain", user=owner, db=db_session
    )
    assert res["credits_charged"] == 1
    assert res["credit_refunded"] is False
    assert res["credits_refunded"] == 0


# ── (3) history serializer: charged credits + refund state ───────────

@pytest.mark.asyncio
async def test_history_carries_receipt_and_refund_state(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=OWNER_TG)
    refunded = _recap_state(
        credits_charged=2, credit_refunded=True, credits_refunded=2,
    )
    db_session.add(_job("rc_k_hist_ref", owner.id, refunded))
    ok = _recap_state(credits_charged=1)
    db_session.add(_job(
        "rc_k_hist_ok", owner.id, ok, status=JobStatus.SUCCESS.value,
    ))
    await db_session.commit()
    res = await api_mod.get_user_history(user=owner, db=db_session)
    items = {item["id"]: item for item in res["history"]}
    ref = items["rc_k_hist_ref"]
    assert ref["credits_charged"] == 2
    assert ref["credit_refunded"] is True
    assert ref["credits_refunded"] == 2
    done = items["rc_k_hist_ok"]
    assert done["credits_charged"] == 1
    assert done["credit_refunded"] is False
    assert done["credits_refunded"] == 0


# ── (5) failure DM: builder ──────────────────────────────────────────

def test_failure_dm_text_full_shape_with_refund():
    text = api_mod._build_recap_failure_dm_text(
        "my",
        title="Cool Movie",
        job_id="rc_k_x",
        reason="လင့်ခ် ဖွင့်လို့ မရပါ",
        credits_refunded=2,
    )
    assert "Cool Movie" in text
    assert "rc_k_x" in text
    assert "လင့်ခ် ဖွင့်လို့ မရပါ" in text
    assert t("recap_failure_refund_note", "my", credits=2) in text
    # Not an upload job: no upload hint.
    assert t("recap_failure_upload_hint", "my") not in text


def test_failure_dm_text_no_refund_line_without_refund():
    text = api_mod._build_recap_failure_dm_text(
        "en",
        title="Cool Movie",
        job_id="rc_k_y",
        reason="Download failed",
        credits_refunded=0,
    )
    assert "Download failed" in text
    assert t("recap_failure_refund_note", "en", credits=1) not in text
    assert "refunded" not in text


def test_failure_dm_text_escapes_markup_and_adds_upload_hint():
    text = api_mod._build_recap_failure_dm_text(
        "my",
        title="Uploaded Video (up_ab12cd34)",
        job_id="rc_k_z",
        reason="bad <script>alert(1)</script> input",
        credits_refunded=1,
    )
    # Curated reason is shown, but its markup can never break out of
    # the HTML parse mode message.
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert t("recap_failure_upload_hint", "my") in text


def test_failure_dm_text_reason_key_localized():
    text = api_mod._build_recap_failure_dm_text(
        "en",
        title="Parked Movie",
        job_id="rc_k_w",
        reason_key="recap_failure_reason_script_expired",
    )
    assert t("recap_failure_reason_script_expired", "en") in text


def test_success_dm_retention_note_renders_hours():
    # Batch X (2026-11-04): the note states the 24-hour window in
    # hours ({retention_hours}); supersedes the old days-based pin.
    note = t("recap_retention_note", "my", retention_hours=24)
    assert "24" in note
    assert "{retention_hours}" not in note


# ── (5) failure DM: sender fires exactly once ────────────────────────

class _FakeBotSession:
    async def close(self):
        pass


class _FakeBot:
    sent: list = []

    def __init__(self, token=None, **kwargs):
        self.session = _FakeBotSession()

    async def send_message(self, chat_id=None, text=None, **kwargs):
        _FakeBot.sent.append((chat_id, text))


async def _open_store(tmp_path, monkeypatch):
    """Point the API helpers' own session factory at a real file DB."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/batch_k.db", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    return engine, maker


@pytest.fixture
def fake_bot(monkeypatch):
    _FakeBot.sent = []
    monkeypatch.setattr(aiogram, "Bot", _FakeBot)
    monkeypatch.setattr(settings, "bot_token", "123456:batch-k-test")
    return _FakeBot


@pytest.mark.asyncio
async def test_failure_dm_sender_fires_once(tmp_path, monkeypatch, fake_bot):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = 880202
        async with maker() as s:
            s.add(User(id=uid, language_code="en", recap_credits=3))
            await s.commit()
        entry = {}
        sent = await api_mod._send_recap_failure_dm(
            job_id="rc_k_dm1",
            chat_id=uid,
            title="Cool Movie",
            reason="Download failed",
            credits_refunded=1,
            entry=entry,
        )
        assert sent is True
        assert len(fake_bot.sent) == 1
        chat_id, text = fake_bot.sent[0]
        assert chat_id == uid
        # Language came from the User row; refund line present.
        assert "Your Recap could not be created" in text
        assert "Download failed" in text
        assert "refunded" in text
        # Second attempt for the same job: guarded, no duplicate.
        again = await api_mod._send_recap_failure_dm(
            job_id="rc_k_dm1",
            chat_id=uid,
            title="Cool Movie",
            reason="Download failed",
            credits_refunded=1,
            entry=entry,
        )
        assert again is False
        assert len(fake_bot.sent) == 1
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_failure_dm_sender_no_token_no_send(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "")
    entry = {}
    sent = await api_mod._send_recap_failure_dm(
        job_id="rc_k_dm2",
        chat_id=880203,
        title="Cool Movie",
        reason="Download failed",
        entry=entry,
    )
    assert sent is False


# ── (5) SCRIPT_READY expiry sweep now notifies ───────────────────────

@pytest.mark.asyncio
async def test_expired_script_ready_job_sends_failure_dm(
    tmp_path, monkeypatch, fake_bot
):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid = 880204
        state = _recap_state(
            phase="render",
            script_ready=True,
            source_path="/data/source.mp4",
            segments_count=4,
            script_ready_at=(
                datetime.now(timezone.utc) - timedelta(hours=25)
            ).isoformat(),
        )
        async with maker() as s:
            s.add(User(id=uid, recap_credits=4))  # submit took 1 of 5
            s.add(_job(
                "rc_k_expire", uid, state, status=JobStatus.RUNNING.value,
            ))
            await s.commit()

        expired = await api_mod.expire_abandoned_script_ready_jobs()
        assert expired == 1
        # Exactly one DM: curated localized reason + refund proof.
        assert len(fake_bot.sent) == 1
        chat_id, text = fake_bot.sent[0]
        assert chat_id == uid
        assert t("recap_failure_reason_script_expired", "my") in text
        assert t("recap_failure_refund_note", "my", credits=1) in text

        # Second sweep: nothing expires, no second DM, no re-refund.
        assert await api_mod.expire_abandoned_script_ready_jobs() == 0
        assert len(fake_bot.sent) == 1
        async with maker() as s:
            user = await s.get(User, uid)
            assert user.recap_credits == 5
    finally:
        await engine.dispose()


# ── (6) audio format: engine truth is MP3, surfaces agree ────────────

def test_engine_voiceover_deliverable_is_mp3():
    assert 'with_suffix(".mp3")' in CORE_SRC
    assert "libmp3lame" in CORE_SRC


def test_all_recap_audio_brands_say_mp3():
    assert 'brandedDlName("RecapAudio", "wav"' not in APP_JS
    assert APP_JS.count('brandedDlName("RecapAudio", "mp3"') >= 5
    # The voice-clone deliverable really is WAV — untouched.
    assert 'brandedDlName("Voice", "wav"' in APP_JS


# ── (4) accept list equals the server upload format list ─────────────

def test_accept_extensions_equal_server_upload_formats():
    m = re.search(
        r'id="recap-video-file-input"[^>]*accept="([^"]+)"', INDEX_HTML
    )
    assert m, "recap file input not found"
    accept_exts = {
        tok.strip().lstrip(".").lower()
        for tok in m.group(1).split(",")
        if tok.strip().startswith(".")
    }
    m2 = re.search(r'if ext not in \{([^}]*\.avi[^}]*\.m4v[^}]*)\}', API_SRC)
    assert m2, "server upload format set not found"
    server_exts = set(re.findall(r'"\.(\w+)"', m2.group(1)))
    assert accept_exts == server_exts == {
        "mp4", "mkv", "mov", "avi", "webm", "ts", "m4v",
    }


def test_pre_submit_source_facts_present():
    # Supported platforms listed on the recap tab (item 4a), upload
    # limits stated in the drop zone (item 4b) — both i18n-wired.
    assert 'data-i18n="recap_platforms_note"' in INDEX_HTML
    assert 'data-i18n="recap_drop_limits"' in INDEX_HTML
    panel = INDEX_HTML.index('id="panel-source-url"')
    note = INDEX_HTML.index('data-i18n="recap_platforms_note"')
    assert panel < note


# ── (1) script-review toggle (Batch AH: de-duplicated) ──────────────

def test_script_review_single_toggle_only():
    # Batch AH (owner report 2026-11-05): the Batch K twin toggles
    # are gone — exactly ONE script-review checkbox exists, carrying
    # the canonical id, the hint text, next to the primary CTA.
    assert INDEX_HTML.count('id="recap-script-review-toggle"') == 1
    assert "recap-script-review-toggle-main" not in INDEX_HTML
    assert "recap-script-review-toggle-main" not in APP_JS
    assert 'data-i18n="recap_script_review_hint"' in INDEX_HTML
    # The payload reads the one toggle…
    assert 'getElementById("recap-script-review-toggle")' in APP_JS
    # …and no mirror-sync remains.
    assert "srMainToggle" not in APP_JS
    assert "srAdvToggle" not in APP_JS
    # The toggle is reachable before the primary CTA.
    # Batch AV: the advanced accordion no longer exists — the toggle
    # lives in Step 3 (Voice) and the CTA in Step 4 (Create).
    cta = INDEX_HTML.index('id="btn-start-recap"')
    toggle = INDEX_HTML.index('id="recap-script-review-toggle"')
    assert 'id="recap-advanced-accordion"' not in INDEX_HTML
    assert INDEX_HTML.index('id="recap-step-3"') < toggle < cta


# ── (2) expiry disclosure on the result card ─────────────────────────

def test_result_card_shows_expiry_and_receipt_lines():
    box = INDEX_HTML.index('id="recap-result-box"')
    expiry = INDEX_HTML.index('data-i18n="history_expiry_note"', box)
    cost = INDEX_HTML.index('id="recap-cost-line"', box)
    card_end = INDEX_HTML.index("<!-- 🌟 AUTO-SLIDING", box)
    assert box < cost < card_end
    assert box < expiry < card_end
    assert 'tf("recap_cost_charged_line"' in APP_JS


# ── (3) frontend receipt / refund rendering ──────────────────────────

def test_frontend_receipt_and_refund_markers_wired():
    # Result-card polling surface.
    assert 'data.credit_refunded' in APP_JS
    assert 'tf("recap_refunded_line"' in APP_JS
    # History detail: receipt for completed jobs, marker for failed.
    detail = APP_JS[APP_JS.index("function hubHistoryDetailHtml"):]
    detail = detail[: detail.index("function hubHistoryRowHtml")]
    assert 'j.credit_refunded' in detail
    assert 'tf("recap_cost_charged_line"' in detail
    assert 'tf("recap_refunded_line"' in detail


# ── (7) recap pill touch targets ─────────────────────────────────────

def test_recap_pills_meet_44px_floor_scoped():
    m = re.search(r"#tab-recap \.pill-btn \{([^}]+)\}", STYLE_CSS)
    assert m, "recap-scoped pill rule missing"
    assert "min-height: 44px" in m.group(1)
    # The shared base rule keeps its compact height for other tabs.
    base = re.search(r"(?m)^\.pill-btn \{\n([^}]+)\}", STYLE_CSS)
    assert base and "min-height: 38px" in base.group(1)


# ── (8) dynamic CTA price ────────────────────────────────────────────

def test_cta_shows_segmented_estimate_when_known():
    assert 'tf("btn_start_recap_credits"' in APP_JS
    assert 't("btn_start_recap")' in APP_JS
    # Language switches repaint the dynamic label via the window hook.
    assert "window.repaintRecapCtaPrice = updateRecapSplitCostHint" in APP_JS
    assert "window.repaintRecapCtaPrice" in I18N_JS


# ── i18n symmetry for the batch K keys (batch F style) ───────────────

def _locale_block(lang: str) -> str:
    marker = "    " + lang + ": {"
    start = I18N_JS.index(marker) + len(marker)
    end = I18N_JS.index("\n    }", start)
    return I18N_JS[start:end]


def _i18n_keys(lang: str) -> set[str]:
    return set(re.findall(r"(?m)^        (\w+):", _locale_block(lang)))


def test_batch_k_ui_keys_present_in_all_four_files():
    my_keys, en_keys = _i18n_keys("my"), _i18n_keys("en")
    for key in UI_KEYS_K:
        assert key in my_keys, f"i18n.js my missing {key}"
        assert key in en_keys, f"i18n.js en missing {key}"
        assert key in MESSAGES_MY, f"messages.json my missing {key}"
        assert key in MESSAGES_EN, f"messages.json en missing {key}"


def test_batch_k_server_keys_in_messages_only():
    for key in SERVER_KEYS_K:
        assert key in MESSAGES_MY, f"messages.json my missing {key}"
        assert key in MESSAGES_EN, f"messages.json en missing {key}"
        # api.py references every server key (via t() or as a
        # reason_key literal resolved inside the DM builder).
        assert f'"{key}"' in API_SRC, f"api.py never references {key}"


def test_batch_k_key_sets_stay_symmetric():
    assert _i18n_keys("my") == _i18n_keys("en")
    assert set(MESSAGES_MY) == set(MESSAGES_EN)


# ── static version lockstep ──────────────────────────────────────────

def test_cache_bust_version_is_r45():
    assert INDEX_HTML.count("?v=20261104_r123") == 4
    assert "20261103_r42" not in INDEX_HTML

"""Batch N — decision-backlog SAFE-FIXES (2026-11-04).

Behavioural coverage for the batch's testable items:

2.  size-aware post-render splitting (pure cap helper + plan cap +
    one-shot re-split of an oversized part, with a fake renderer so
    the byte sizes are exact);
5.  group-AI escalation AuditLog rows (created / approved /
    edit_claimed / edit_released; losing taps write nothing);
6.  rate-limit additions (template matching, new default entries,
    DELETE limiting for history);
7.  recap submit concurrency lock (sequential double-submit is
    rejected before a second charge; FOR UPDATE pin);
9.  SCRIPT_READY surfaced in GET /history (+ the history detail's
    parked branch and the new i18n keys);
10. admin cancel of a parked job clears script_ready via the core;
11. payment-review claim TTL 60 min (stale claims stealable, live
    claims still block, _claimed_by_other is age-aware).

Plus source pins for the deletion / wiring items (1, 3, 4, 8).
"""

from __future__ import annotations

import inspect
import json
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.requests import Request
from starlette.responses import Response

from app.config import settings
from app.db import session as session_mod
from app.db.base import Base
from app.handlers.admin import _claimed_by_other
from app.models.enums import JobStatus, JobType, PaymentStatus
from app.models.schema import AuditLog, Job, PaymentRequest, User, utc_now
from app.services import gemini_recap
from app.services.group_ai_admin import (
    claim_escalation,
    claim_escalation_for_edit,
    create_escalation,
    release_escalation_edit,
)
from app.services.ledger import get_or_create_user
from app.services.payment_review import (
    PAYMENT_REVIEW_CLAIM_TTL,
    claim_payment_review,
    payment_claim_is_stale,
)
from app.services.recap_engine import video_splitter as vs
from app.webapp import api as api_mod
from app.webapp import rate_limit as rl_mod

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
API_SRC = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")
DOWNLOADER_SRC = (ROOT / "app/services/downloader.py").read_text(encoding="utf-8")


# ── shared builders (patterns copied from the batch L / F2 suites) ───


def _recap_state(url="https://example.com/movie", **over):
    cfg = api_mod.RecapConfigRequest(
        url=url,
        title="Batch N Movie",
        voice_slug="mahnin",
        duration_seconds=60,
        ratio="9:16",
    )
    state = {
        "config": cfg.model_dump(),
        "credit_deducted": True,
        "credits_charged": 1,
        "is_vip": False,
        "phase": "full",
        "credits_estimated": None,
        "credit_refunded": False,
        "credits_refunded": 0,
    }
    state.update(over)
    return state


def _job(job_id, user_id, state, status=JobStatus.FAILED.value, **kw):
    job = Job(
        id=job_id,
        user_id=user_id,
        chat_id=user_id,
        status=status,
        job_type=JobType.RECAP.value,
        engine="recap",
        preset_voice="mahnin",
        input_text="Batch N Movie",
        source_audio_path=state["config"]["url"],
        recap_config=json.dumps(state),
        created_at=datetime.now(timezone.utc),
    )
    for key, value in kw.items():
        setattr(job, key, value)
    return job


async def _open_store(tmp_path, monkeypatch):
    """Point the API helpers' own session factory at a real file DB
    (refund helpers open AsyncSessionLocal themselves)."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/batch_n.db", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    return engine, maker


# ── (2) size-aware post-split ────────────────────────────────────────


def test_size_cap_helper_binds_and_floors():
    # Low byte rate: the cap never binds.
    assert vs.size_capped_part_seconds(600, 6_000_000, 300) == 300
    # 1 MB/s average: a 48 MB cap allows ~48 s of it per part.
    capped = vs.size_capped_part_seconds(600, 600_000_000, 300)
    assert capped == pytest.approx(48 * 1024 * 1024 / 1_000_000, rel=0.01)
    # Unknown rate (no bytes / no duration): unchanged.
    assert vs.size_capped_part_seconds(0, 10_000_000, 300) == 300
    assert vs.size_capped_part_seconds(600, 0, 300) == 300
    # Absurd rate: plan_parts' 5 s floor still applies.
    assert vs.size_capped_part_seconds(10, 10_000_000_000, 300) == 5.0
    # A custom cap is honoured.
    assert vs.size_capped_part_seconds(100, 100_000_000, 90, cap_bytes=10_000_000) == 10.0


def _make_video(path, seconds, *, black=False):
    video_src = (
        f"color=c=black:duration={seconds}:size=320x180:rate=15"
        if black
        else f"testsrc2=duration={seconds}:size=320x180:rate=15"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", video_src,
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(path),
        ],
        check=True,
    )


def _fake_render(rate_per_second):
    def _render(ffmpeg, source, start, end, out, **kwargs):
        Path(out).write_bytes(b"x" * int((float(end) - float(start)) * rate_per_second))

    return _render


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_split_plan_is_size_capped(tmp_path, monkeypatch):
    from app.services.recap_engine.core import probe_duration

    src = tmp_path / "capped.mp4"
    _make_video(src, 40)
    monkeypatch.setattr(vs, "MAX_PART_BYTES", 150_000)
    monkeypatch.setattr(vs, "render_part", _fake_render(10))
    parts = vs.split_rendered_video(
        str(src), tmp_path / "parts", part_minutes=1, smart_cut=False
    )
    real = [p for p in parts if p["path"]]
    duration = probe_duration("ffprobe", src)
    expected_seconds = vs.size_capped_part_seconds(
        duration, src.stat().st_size, 60, 150_000
    )
    expected = vs.plan_parts(duration, expected_seconds, smart=False)
    # The cap bound the plan (uncapped, 40 s fits in a single 60 s part).
    assert len(real) == len(expected) >= 2
    assert [(p["start"], p["end"]) for p in real] == [
        (p["start"], p["end"]) for p in expected
    ]


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_split_resplits_an_oversized_part_once(tmp_path, monkeypatch):
    src = tmp_path / "hot.mp4"
    # A near-zero-bitrate SOURCE: its average rate leaves the 30 s
    # plan untouched, so only the re-split pass can react to the
    # (faked) hot render sizes below.
    _make_video(src, 40, black=True)
    monkeypatch.setattr(vs, "MAX_PART_BYTES", 300_000)
    # 20 KB per rendered second: the 30 s part lands at 600 KB (over
    # the 300 KB cap) and must be halved; the 10 s tail (200 KB) and
    # the 15 s halves (300 KB, not over) stay untouched.
    monkeypatch.setattr(vs, "render_part", _fake_render(20_000))
    parts = vs.split_rendered_video(
        str(src), tmp_path / "parts", part_minutes=0.5, smart_cut=False
    )
    real = [p for p in parts if p["path"]]
    assert [(p["start"], p["end"]) for p in real] == [
        (0.0, 15.0),
        (15.0, 30.0),
        (30.0, 40.0),
    ]
    assert [p["n"] for p in real] == [1, 2, 3]
    for p in real:
        assert Path(p["path"]).stat().st_size <= 300_000


# ── (5) escalation AuditLog ──────────────────────────────────────────


async def _audit_actions(db_session):
    rows = (
        await db_session.execute(
            select(AuditLog)
            .where(AuditLog.target_type == "group_ai_escalation")
            .order_by(AuditLog.id)
        )
    ).scalars().all()
    return [(r.action, r.admin_id, r.target_id) for r in rows]


@pytest.mark.asyncio
async def test_escalation_actions_write_audit_rows(db_session):
    esc_id = await create_escalation(
        db_session,
        chat_id=-1003795677289,
        message_id=42,
        author_id=7001,
        author_label="Member One",
        question_text="ဘယ်လိုဝယ်ရမလဲ",
        draft_text="draft answer",
    )
    assert esc_id is not None
    assert await _audit_actions(db_session) == [
        ("group_ai_escalation.created", 7001, str(esc_id))
    ]

    outcome, _ = await claim_escalation(db_session, esc_id, admin_id=9101, approve=True)
    assert outcome == "claimed"
    # A losing second tap writes NO further audit row.
    outcome2, _ = await claim_escalation(db_session, esc_id, admin_id=9102, approve=False)
    assert outcome2 == "already_handled"
    actions = await _audit_actions(db_session)
    assert actions[-1] == ("group_ai_escalation.approved", 9101, str(esc_id))
    assert len(actions) == 2

    esc2 = await create_escalation(
        db_session,
        chat_id=-1003795677289,
        message_id=43,
        author_id=7002,
        author_label="Member Two",
        question_text="နောက်တစ်ခု",
        draft_text="draft two",
    )
    outcome, _ = await claim_escalation_for_edit(db_session, esc2, admin_id=9102)
    assert outcome == "claimed"
    released, _ = await release_escalation_edit(db_session, esc2, admin_id=9102)
    assert released is True
    actions = await _audit_actions(db_session)
    assert ("group_ai_escalation.edit_claimed", 9102, str(esc2)) in actions
    assert actions[-1] == ("group_ai_escalation.edit_released", 9102, str(esc2))


# ── (6) rate-limit additions ─────────────────────────────────────────


def test_rate_limit_template_matching():
    assert rl_mod._template_matches("/api/recap/{job_id}/retry", "/api/recap/rc_1/retry")
    assert not rl_mod._template_matches("/api/recap/{job_id}/retry", "/api/recap/rc_1/cancel")
    assert not rl_mod._template_matches("/api/recap/{job_id}/retry", "/api/recap/a/b/retry")
    assert not rl_mod._template_matches("/api/recap/{job_id}/retry", "/api/recap//retry")


def test_rate_limit_new_default_entries():
    mw = rl_mod.RateLimitMiddleware(app=lambda scope, receive, send: None)
    assert mw._is_limited("/api/recap/rc_x/retry") == (10, 60)
    assert mw._is_limited("/api/recap/rc_x/cancel") == (30, 60)
    assert mw._is_limited("/api/recap/rc_x/render") == (20, 60)
    assert mw._is_limited("/api/recap/rc_x/send-telegram") == (20, 60)
    assert mw._is_limited("/api/voices/rc_x/send-telegram") == (20, 60)
    assert mw._is_limited("/api/downloader/dl_x/send-telegram") == (20, 60)
    assert mw._is_limited("/api/history/rc_x") == (30, 60)
    assert mw._is_limited("/api/recap/demo/send") == (10, 60)
    assert mw._is_limited("/api/recap/preview-frame") == (60, 60)
    assert mw._is_limited("/api/downloader/fetch") == (20, 60)
    # The originals are untouched, and unknown job actions stay open.
    assert mw._is_limited("/api/recap/generate") == (10, 60)
    assert mw._is_limited("/api/recap/rc_x/status") is None
    assert mw._is_limited("/api/history") is None


def _request(method: str, path: str) -> Request:
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(b"x-forwarded-for", b"203.0.113.7")],
        "client": ("10.0.0.9", 4321),
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_rate_limiter_covers_delete_history():
    mw = rl_mod.RateLimitMiddleware(
        app=lambda scope, receive, send: None,
        limits={"/api/history/{job_id}": (2, 60)},
    )

    async def _ok(_request):
        return Response("ok", status_code=200)

    path = "/api/history/rc_123"
    assert (await mw.dispatch(_request("DELETE", path), _ok)).status_code == 200
    assert (await mw.dispatch(_request("DELETE", path), _ok)).status_code == 200
    assert (await mw.dispatch(_request("DELETE", path), _ok)).status_code == 429
    # GETs are never limited, even on a listed template.
    assert (await mw.dispatch(_request("GET", path), _ok)).status_code == 200


# ── (7) recap submit concurrency lock ────────────────────────────────


@pytest.fixture
def recap_runner(monkeypatch):
    calls = []

    async def _fake_runner(job_id, req, user_id, chat_id, **kwargs):
        calls.append({"job_id": job_id, "kwargs": kwargs})

    monkeypatch.setattr(api_mod, "_run_recap_background_job", _fake_runner)
    yield calls
    api_mod.RECAP_JOBS_REGISTRY.clear()


def _recap_req(**over):
    base = {"url": "", "title": "Test Movie", "voice_slug": "komin", "duration_seconds": 60}
    base.update(over)
    return api_mod.RecapConfigRequest(**base)


@pytest.mark.asyncio
async def test_double_submit_second_is_rejected_before_charging(db_session, recap_runner):
    user, _ = await get_or_create_user(db_session, telegram_id=880701)
    user.account_category = "PURCHASED"
    user.recap_credits = 5
    await db_session.commit()

    resp = await api_mod.create_recap_job(
        _recap_req(), user=user, db=db_session, _gate=user
    )
    assert resp["success"] is True
    assert resp["remaining_credits"] == 4

    # The first job is still active (registry QUEUED + Job row): a
    # second submit — the double-tap / racing-retry shape — must 429
    # BEFORE any further credit moves.
    with pytest.raises(HTTPException) as exc:
        await api_mod.create_recap_job(
            _recap_req(), user=user, db=db_session, _gate=user
        )
    assert exc.value.status_code == 429
    await db_session.refresh(user)
    assert user.recap_credits == 4
    assert len(recap_runner) == 1


def test_create_recap_job_holds_user_row_lock():
    start = API_SRC.index("async def create_recap_job")
    end = API_SRC.index("async def _deferred_db_session")
    segment = API_SRC[start:end]
    assert "with_for_update()" in segment
    # The lock is taken before the active-count check, and the charge
    # commits only together with the job insert (single commit after
    # the savepoint block).
    assert segment.index("with_for_update()") < segment.index("_recap_active_job_count")
    assert "begin_nested()" in segment


# ── (9) SCRIPT_READY in history ──────────────────────────────────────


@pytest.mark.asyncio
async def test_history_marks_parked_job_from_persisted_state(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=880901)
    parked_at = datetime.now(timezone.utc).isoformat()
    state = _recap_state(
        script_ready=True, script_ready_at=parked_at, phase="render"
    )
    db_session.add(
        _job("rc_n_parked", owner.id, state, status=JobStatus.RUNNING.value)
    )
    db_session.add(
        _job("rc_n_running", owner.id, _recap_state(), status=JobStatus.RUNNING.value)
    )
    await db_session.commit()

    hist = await api_mod.get_user_history(user=owner, db=db_session)
    by_id = {h["id"]: h for h in hist["history"]}
    assert by_id["rc_n_parked"]["script_ready"] is True
    assert by_id["rc_n_parked"]["script_ready_at"] == parked_at
    assert by_id["rc_n_running"]["script_ready"] is False
    assert by_id["rc_n_running"]["script_ready_at"] is None


@pytest.mark.asyncio
async def test_history_marks_parked_job_from_live_registry(db_session):
    owner, _ = await get_or_create_user(db_session, telegram_id=880902)
    parked_at = datetime.now(timezone.utc).isoformat()
    db_session.add(
        _job("rc_n_hot", owner.id, _recap_state(), status=JobStatus.RUNNING.value)
    )
    await db_session.commit()
    api_mod.RECAP_JOBS_REGISTRY["rc_n_hot"] = {
        "id": "rc_n_hot",
        "user_id": owner.id,
        "status": "SCRIPT_READY",
        "script_ready_at": parked_at,
    }
    try:
        hist = await api_mod.get_user_history(user=owner, db=db_session)
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop("rc_n_hot", None)
    by_id = {h["id"]: h for h in hist["history"]}
    assert by_id["rc_n_hot"]["script_ready"] is True
    assert by_id["rc_n_hot"]["script_ready_at"] == parked_at


def test_history_detail_has_parked_branch_and_i18n_keys():
    start = APP_JS.index("function hubHistoryDetailHtml")
    segment = APP_JS[start:APP_JS.index("window.rerunRecapFromHistory", start)]
    assert "j.script_ready" in segment
    # The parked branch reuses the poller's review action and offers
    # cancel; it must be matched BEFORE the generic RUNNING branch.
    assert "openRecapScriptEditor" in segment
    assert segment.index("j.script_ready") < segment.index('j.status === "RUNNING"')
    for key in ("hist_script_ready_badge", "hist_script_ready_expires", "btn_review_script"):
        assert I18N_JS.count(f"{key}:") == 2  # my + en, symmetry kept


# ── (10) admin cancel clears script_ready via the core ───────────────


@pytest.mark.asyncio
async def test_admin_cancel_parked_job_clears_script_ready(tmp_path, monkeypatch):
    engine, maker = await _open_store(tmp_path, monkeypatch)
    try:
        uid, jid = 880421, "rc_n_admin_parked"
        state = _recap_state(script_ready=True, phase="render")
        async with maker() as s:
            s.add(User(id=uid, language_code="my", recap_credits=1))
            s.add(_job(jid, uid, state, status=JobStatus.RUNNING.value))
            await s.commit()
        api_mod.RECAP_JOBS_REGISTRY[jid] = {
            "id": jid, "user_id": uid, "status": "SCRIPT_READY",
            "credit_deducted": True, "credits_charged": 1,
            "credit_refunded": False, "credits_refunded": 0,
            "error_message": None, "created_at": time.time(),
        }
        async with maker() as s:
            res = await api_mod.cancel_queued_job(jid, User(id=999001), db=s)
        assert res["success"] is True
        async with maker() as s:
            job = await s.get(Job, jid)
            user = await s.get(User, uid)
        assert job.status == JobStatus.CANCELLED.value
        assert user.recap_credits == 2  # charged credit refunded
        persisted = api_mod._load_persisted_recap_state(job)
        assert persisted["script_ready"] is False
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop(jid, None)
        await engine.dispose()


def test_cancel_core_owns_script_ready_clearing():
    core_start = API_SRC.index("async def _cancel_recap_job_core")
    core = API_SRC[core_start:API_SRC.index("def _voice_job_error_status", core_start)]
    assert '_persisted_state["script_ready"] = False' in core
    # …in the same commit as the status flip (clearing precedes it).
    assert core.index('_persisted_state["script_ready"] = False') < core.index(
        "job.status = JobStatus.CANCELLED.value"
    )
    # The owner endpoint's old second-session post-step is gone.
    cancel_start = API_SRC.index("async def cancel_recap_job")
    cancel_seg = API_SRC[cancel_start:API_SRC.index("@api_router", cancel_start + 10)]
    assert "fresh_state" not in cancel_seg


# ── (11) payment-review claim TTL ────────────────────────────────────

BUYER_TG = 7001
ADMIN1 = 9101
ADMIN2 = 9102


async def _make_payment(db_session, pay_id: str):
    buyer, _ = await get_or_create_user(db_session, telegram_id=BUYER_TG)
    payment = PaymentRequest(
        id=pay_id,
        user_id=buyer.id,
        package_code="MIN_10",
        minutes=10,
        amount_mmk=8000,
        receipt_file_id="slip-file-1",
        status=PaymentStatus.PENDING.value,
    )
    db_session.add(payment)
    await db_session.flush()
    return payment


def test_payment_claim_stale_helper_rules():
    payment = PaymentRequest(id="PAY-N1", status=PaymentStatus.PENDING.value)
    assert payment_claim_is_stale(payment) is False  # unclaimed
    payment.review_claimed_by = ADMIN1
    assert payment_claim_is_stale(payment) is False  # no timestamp (legacy)
    payment.review_claimed_at = utc_now()
    assert payment_claim_is_stale(payment) is False  # fresh claim
    payment.review_claimed_at = utc_now() - PAYMENT_REVIEW_CLAIM_TTL - timedelta(minutes=1)
    assert payment_claim_is_stale(payment) is True  # older than the TTL
    assert PAYMENT_REVIEW_CLAIM_TTL == timedelta(minutes=60)


@pytest.mark.asyncio
async def test_stale_payment_claim_is_stealable_live_claim_blocks(db_session):
    payment = await _make_payment(db_session, "PAY-N2")
    ok, _, code = await claim_payment_review(db_session, payment.id, ADMIN1)
    assert ok is True and code == "CLAIMED"

    # A 30-minute-old claim is LIVE: the second admin is still blocked.
    payment.review_claimed_at = utc_now() - timedelta(minutes=30)
    await db_session.flush()
    ok, _, code = await claim_payment_review(db_session, payment.id, ADMIN2)
    assert ok is False and code == "CLAIMED_BY_OTHER"

    # A 61-minute-old claim is STALE: the second admin takes it over.
    payment.review_claimed_at = utc_now() - (PAYMENT_REVIEW_CLAIM_TTL + timedelta(minutes=1))
    await db_session.flush()
    ok, claimed, code = await claim_payment_review(db_session, payment.id, ADMIN2)
    assert ok is True and code == "CLAIMED"
    assert claimed.review_claimed_by == ADMIN2


def test_claimed_by_other_is_age_aware():
    payment = PaymentRequest(id="PAY-N3", status=PaymentStatus.PENDING.value)
    payment.review_claimed_by = ADMIN1
    payment.review_claimed_at = utc_now()
    assert _claimed_by_other(payment, ADMIN2) is True  # live claim blocks
    assert _claimed_by_other(payment, ADMIN1) is False  # own claim never blocks
    payment.review_claimed_at = utc_now() - timedelta(minutes=61)
    assert _claimed_by_other(payment, ADMIN2) is False  # stale claim does not


# ── (1, 3, 4, 8) wiring / deletion pins ──────────────────────────────


def test_fetch_interceptor_routes_401s():
    start = APP_JS.index("installGateFetchInterceptor")
    segment = APP_JS[start:APP_JS.index("})();", start)]
    assert "res.status === 401" in segment
    assert "isAuthFailure(res)" in segment


def test_dead_crop_handles_removed():
    assert "setupInteractiveCropHandles" not in APP_JS
    assert "crop-handle-" not in APP_JS


def test_dead_viral_recap_chain_removed():
    assert "generate_viral_recap" not in DOWNLOADER_SRC
    assert "generate_tiktok_recap_and_srt" not in DOWNLOADER_SRC
    assert not hasattr(gemini_recap, "generate_tiktok_recap_and_srt")
    assert not hasattr(gemini_recap, "get_gemini_api_key")
    # …while the live model resolver the suite imports stays put.
    assert callable(gemini_recap._resolve_primary_model)


def test_blur_x_w_params_present_batch_ag():
    # Batch N removed blur_x/blur_w as DEAD params (accepted but
    # never wired). Batch AG (2026-11-05, Ulfa-approved) re-adds
    # them as LIVE params threaded to the one conversion point, so
    # the pin flips: all four entry points accept them alongside
    # blur_y/blur_h. The AG file pins the wiring itself.
    from app.services.downloader import MediaDownloader
    from app.services.recap_engine import recap_pipeline

    for fn in (
        recap_pipeline.run_recap_pipeline,
        recap_pipeline.generate_recap_preview,
        MediaDownloader.generate_recap_video,
        MediaDownloader.generate_recap_preview,
    ):
        params = inspect.signature(fn).parameters
        assert "blur_x" in params and "blur_w" in params
        assert "blur_y" in params and "blur_h" in params

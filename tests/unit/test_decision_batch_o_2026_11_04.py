"""Batch O — DECISION backlog resolutions (2026-11-04).

Behavioural coverage for the batch's items:

1.  Gemini key rotation per call — the recap planner and Group AI now
    take their starting key from KeyHealth.order() (rotating), so
    consecutive jobs/messages start at different keys; cooled keys go
    last and the pool never shrinks.
2.  User-facing blur strength — RecapConfigRequest default/clamp,
    the value reaching the render overlays AND the preview overlays
    unchanged (parity), and the Mini App slider wiring.
3.  Dependency pinning — requirements.txt is a fully pinned set and
    requirements.lock is identical to it.
4.  Recap billing hybrid (option C) — ledger events for charges and
    refunds, the persisted refund claim taken BEFORE the balance
    moves on every path (double-refund attempts credit once, the
    crash window is closed), and a registry-less cancel refunding a
    segmented job's recorded credits_charged rather than a flat 1.
5.  Price display unification — get_vip_plans() resolves every plan
    from PLAN_CATALOGUE overlaid with DB PricingPlan rows (DB wins).
6.  Broadcast report cosmetics — the pre-existing-blocked count
    renders as its own note block (not a breakdown row) and the
    blocked label names the bot_blocked flag, not a DB status.
7.  Fallback disclosure — persistent result-card notes for voice and
    recap (incl. the full-fallback recap wording), upgraded wording
    in the Mini App i18n and the bot locales.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import session as session_mod
from app.db.base import Base
from app.models.enums import JobStatus, JobType
from app.models.schema import Job, PricingPlan, RecapCreditLedgerEvent, User
from app.services import key_rotation
from app.services.ledger import (
    get_or_create_user,
    recap_refund_event_exists,
    record_recap_credit_event,
)
from app.services.recap_engine import recap_pipeline as pipeline_mod
from app.services.recap_engine import recap_planner as planner_mod
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
API_SRC = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")


# ── shared builders ─────────────────────────────────────────────────


async def _open_store(tmp_path, monkeypatch, name="batch_o.db"):
    """Point the API helpers' own session factory at a real file DB
    (the credit helpers open AsyncSessionLocal themselves)."""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/{name}", future=True
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    return engine, maker


async def _make_user(maker, telegram_id=777001, recap_credits=0):
    async with maker() as session:
        user, _created = await get_or_create_user(session, telegram_id)
        user.recap_credits = recap_credits
        await session.commit()
        return user.id


def _recap_state(**over):
    cfg = api_mod.RecapConfigRequest(
        url="https://example.com/movie",
        title="Batch O Movie",
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


def _job(job_id, user_id, state, status=JobStatus.RUNNING.value):
    return Job(
        id=job_id,
        user_id=user_id,
        chat_id=user_id,
        status=status,
        job_type=JobType.RECAP.value,
        engine="recap",
        preset_voice="mahnin",
        input_text="Batch O Movie",
        source_audio_path=state["config"]["url"],
        recap_config=json.dumps(state),
        created_at=datetime.now(timezone.utc),
    )


async def _balance(maker, user_id):
    async with maker() as session:
        user = await session.get(User, user_id)
        return int(user.recap_credits or 0)


async def _blob(maker, job_id):
    async with maker() as session:
        job = await session.get(Job, job_id)
        return json.loads(job.recap_config)


async def _ledger_rows(maker, user_id):
    async with maker() as session:
        rows = (
            await session.execute(
                select(RecapCreditLedgerEvent).where(
                    RecapCreditLedgerEvent.user_id == user_id
                )
            )
        ).scalars().all()
        return [(r.direction, r.credits, r.reference_id) for r in rows]


# ── (1) Gemini rotation per call ────────────────────────────────────


class _StubAI:
    def __init__(self):
        self.client = None

    def _enable_proxy(self):
        pass

    def _disable_proxy(self):
        pass


def _planner_start(pool, factory_calls):
    ai = _StubAI()

    def factory(*, api_key, http_options=None):
        factory_calls.append(api_key)
        return object()

    planner_mod._configure_gemini_key_pool(
        ai, pool, client_factory=factory, log=lambda _m: None
    )
    return ai


def test_planner_rotates_start_key_per_job():
    health = key_rotation.health_for("gemini")
    health.reset()
    old_env = os.environ.get("GEMINI_API_KEY")
    pool = [("K1", "key-one"), ("K2", "key-two"), ("K3", "key-three")]
    try:
        calls: list[str] = []
        first = _planner_start(pool, calls)
        second = _planner_start(pool, calls)
        third = _planner_start(pool, calls)
        # Consecutive jobs start at different keys; the pool never shrinks.
        assert first._recap_active_key_label == "K1"
        assert second._recap_active_key_label == "K2"
        assert third._recap_active_key_label == "K3"
        assert first._recap_key_pool_size == 3
        assert second._recap_key_pool_size == 3
    finally:
        health.reset()
        if old_env is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = old_env


def test_planner_cooled_key_goes_last_pool_intact():
    health = key_rotation.health_for("gemini")
    health.reset()
    health.mark_failure("key-one")
    old_env = os.environ.get("GEMINI_API_KEY")
    pool = [("K1", "key-one"), ("K2", "key-two"), ("K3", "key-three")]
    try:
        ai = _planner_start(pool, [])
        # The cooled key is skipped as the starter but stays in the pool.
        assert ai._recap_active_key_label != "K1"
        assert ai._recap_key_pool_size == 3
    finally:
        health.reset()
        if old_env is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = old_env


def test_group_ai_rotates_start_key_per_message(monkeypatch):
    from app.services import group_ai_admin

    health = key_rotation.health_for("gemini")
    health.reset()
    pool = [("L1", "g-key-1"), ("L2", "g-key-2"), ("L3", "g-key-3")]
    monkeypatch.setattr(group_ai_admin, "collect_gemini_key_pool", lambda: pool)
    used: list[str] = []

    class _Resp:
        text = "ok"

    class _Models:
        def generate_content(self, model=None, contents=None):
            return _Resp()

    class _Client:
        def __init__(self):
            self.models = _Models()

    def factory(api_key):
        used.append(api_key)
        return _Client()

    assert group_ai_admin._ask_gemini_sync("hi", client_factory=factory) == "ok"
    assert group_ai_admin._ask_gemini_sync("hi", client_factory=factory) == "ok"
    assert used[0] == "g-key-1"
    assert used[1] == "g-key-2"
    health.reset()


def test_gemini_tts_engine_keeps_prioritized():
    # SUPERSEDED by Batch U (2026-11-04, owner scope extension "tts
    # ကိုလဲသုံးနော်"): the TTS engine now rotates its starting key per
    # call via KeyHealth like the planner and Group AI, so this
    # Batch O pin is deliberately inverted. Behavioural coverage
    # lives in test_gemini_model_rotation_batch_u_2026_11_04.py.
    # (Batch DN: the per-model ordering call is order_for_model —
    # the same rotation, additionally quota-sorted.)
    src = (ROOT / "app/services/recap_engine/gemini_tts_engine.py").read_text(encoding="utf-8")
    assert "tts_health.order_for_model(" in src
    assert "tts_health.prioritized(" not in src


# ── (2) user-facing blur strength ───────────────────────────────────


def test_blur_strength_default_and_clamp():
    cfg = api_mod.RecapConfigRequest(url="https://example.com/v")
    assert cfg.blur_strength == 18
    assert api_mod.RecapConfigRequest(url="https://example.com/v", blur_strength=30).blur_strength == 30
    assert api_mod.RecapConfigRequest(url="https://example.com/v", blur_strength=100).blur_strength == 40
    assert api_mod.RecapConfigRequest(url="https://example.com/v", blur_strength=1).blur_strength == 4
    # The clamped value persists into recap_config like every other
    # recap setting (retry / history re-run read it back).
    assert cfg.model_dump()["blur_strength"] == 18


def test_clamp_blur_strength_helper():
    assert pipeline_mod.clamp_blur_strength(18) == 18
    assert pipeline_mod.clamp_blur_strength(4) == 4
    assert pipeline_mod.clamp_blur_strength(40) == 40
    assert pipeline_mod.clamp_blur_strength(999) == 40
    assert pipeline_mod.clamp_blur_strength(0) == 4
    assert pipeline_mod.clamp_blur_strength(None) == 18
    assert pipeline_mod.clamp_blur_strength("x") == 18


def _make_tiny_video(path, seconds=2):
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=duration={seconds}:size=320x180:rate=15",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(path),
        ],
        check=True,
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_preview_overlays_carry_blur_strength(tmp_path, monkeypatch):
    src = tmp_path / "tiny.mp4"
    _make_tiny_video(src)
    captured = {}

    def fake_extract(**kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr(pipeline_mod, "extract_preview_with_overlays", fake_extract)
    monkeypatch.setattr(pipeline_mod, "detect_source_subtitle_bands", lambda *a, **k: [])
    pipeline_mod.generate_recap_preview(
        src, tmp_path / "prev.jpg", blur_strength=33, at_seconds=0.5
    )
    assert captured["overlays"]["blur_strength"] == 33
    # Default keeps the historical behaviour.
    captured.clear()
    pipeline_mod.generate_recap_preview(src, tmp_path / "prev2.jpg", at_seconds=0.5)
    assert captured["overlays"]["blur_strength"] == 18


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
def test_render_overlays_carry_blur_strength(tmp_path, monkeypatch):
    src = tmp_path / "tiny.mp4"
    _make_tiny_video(src)
    captured = {}

    class FakeBuilder:
        def __init__(self, **kwargs):
            pass

        def build(self, *, source, job_dir, source_title, reuse=True):
            plan_path = Path(job_dir) / "plan.json"
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            plan_path.write_text("{}", encoding="utf-8")
            return plan_path, {"segments": []}

    class FakeRunner:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self):
            return {"final": "x.mp4", "audio": None, "subtitles_srt": None, "segments": 0, "duration": 0.0}

    monkeypatch.setattr(pipeline_mod, "RecapPlanBuilder", FakeBuilder)
    monkeypatch.setattr(pipeline_mod, "EdgeSmartSync", FakeRunner)
    monkeypatch.setattr(pipeline_mod, "detect_source_subtitle_bands", lambda *a, **k: [])
    pipeline_mod.run_recap_pipeline(src, job_dir=tmp_path / "job", blur_strength=27)
    assert captured["overlays"]["blur_strength"] == 27
    captured.clear()
    pipeline_mod.run_recap_pipeline(src, job_dir=tmp_path / "job2")
    assert captured["overlays"]["blur_strength"] == 18


def test_blur_strength_frontend_wiring():
    # Slider in the mask panel, next to the other blur controls.
    assert 'id="slider-blur-strength"' in INDEX_HTML
    assert 'min="4" max="40" value="18"' in INDEX_HTML
    assert 'id="val-blur-strength"' in INDEX_HTML
    # Payload carries it; settings snapshot/restore/prefill know it.
    assert "blur_strength: sliderBlurStrength ? parseInt(sliderBlurStrength.value) : 18" in APP_JS
    assert "recapBlurStrength: [4, 40]" in APP_JS
    assert "recapBlurStrength: cfg.blur_strength" in APP_JS
    # i18n label exists in both languages.
    assert I18N_JS.count("slider_blur_strength") == 2
    # The API forwards the request value into the render call.
    assert "blur_strength=req.blur_strength" in API_SRC


# ── (3) dependency pinning ──────────────────────────────────────────


def test_requirements_fully_pinned_and_lock_identical():
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    lock = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    assert req == lock
    lines = [
        ln.strip() for ln in req.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    assert len(lines) >= 80
    for ln in lines:
        assert re.fullmatch(r"[A-Za-z0-9_.\-]+==[^\s;]+", ln), ln
    # Direct dependencies are among the pins.
    joined = "\n".join(lines).lower()
    for name in ("aiogram==", "fastapi==", "yt-dlp==", "sqlalchemy==", "gradio_client=="):
        assert name in joined


# ── (4) recap billing hybrid (option C) ─────────────────────────────


@pytest.mark.asyncio
async def test_refund_writes_ledger_row_and_never_twice(tmp_path, monkeypatch):
    _engine, maker = await _open_store(tmp_path, monkeypatch)
    uid = await _make_user(maker, recap_credits=5)
    assert await api_mod._refund_recap_credits(uid, 2, job_id="rc_t1") is True
    assert await _balance(maker, uid) == 7
    assert await _ledger_rows(maker, uid) == [("CREDIT", 2, "REFUND:rc_t1")]
    # A second refund attempt for the same job is refused by the
    # ledger cross-check and moves nothing.
    assert await api_mod._refund_recap_credits(uid, 2, job_id="rc_t1") is False
    assert await _balance(maker, uid) == 7
    assert len(await _ledger_rows(maker, uid)) == 1


@pytest.mark.asyncio
async def test_charge_writes_ledger_row(tmp_path, monkeypatch):
    _engine, maker = await _open_store(tmp_path, monkeypatch)
    uid = await _make_user(maker, recap_credits=5)
    entry = {"id": "rc_t2", "credits_charged": 1, "credit_deducted": True}
    assert await api_mod._top_up_recap_segment_charge(uid, entry, 3) == 3
    assert await _balance(maker, uid) == 3
    rows = await _ledger_rows(maker, uid)
    assert ("DEBIT", 2, "rc_t2") in rows


@pytest.mark.asyncio
async def test_job_charge_refund_exactly_once_across_paths(tmp_path, monkeypatch):
    _engine, maker = await _open_store(tmp_path, monkeypatch)
    uid = await _make_user(maker, recap_credits=0)
    state = _recap_state(credits_charged=2)
    async with maker() as session:
        session.add(_job("rc_t3", uid, state))
        await session.commit()
    entry = {
        "id": "rc_t3", "user_id": uid, "credits_charged": 2,
        "credit_deducted": True, "credit_refunded": False, "credits_refunded": 0,
    }
    assert await api_mod._refund_recap_job_charge(uid, entry) is True
    assert await _balance(maker, uid) == 2
    # Same entry again: the in-memory claim makes it a no-op.
    assert await api_mod._refund_recap_job_charge(uid, entry) is False
    # Crash-window simulation: a FRESH entry (registry rebuilt after a
    # restart) that never saw the in-memory flag must still not
    # refund — the persisted claim was taken before the balance moved.
    fresh_entry = {
        "id": "rc_t3", "user_id": uid, "credits_charged": 2,
        "credit_deducted": True, "credit_refunded": False, "credits_refunded": 0,
    }
    assert await api_mod._refund_recap_job_charge(uid, fresh_entry) is False
    assert await _balance(maker, uid) == 2
    blob = await _blob(maker, "rc_t3")
    assert blob["credit_refunded"] is True
    assert blob["credits_refunded"] == 2
    rows = await _ledger_rows(maker, uid)
    assert rows == [("CREDIT", 2, "REFUND:rc_t3")]


@pytest.mark.asyncio
async def test_refund_claim_precedes_balance_move(tmp_path, monkeypatch):
    _engine, maker = await _open_store(tmp_path, monkeypatch)
    uid = await _make_user(maker, recap_credits=0)
    state = _recap_state(credits_charged=1)
    async with maker() as session:
        session.add(_job("rc_t4", uid, state))
        await session.commit()
    seen = {}
    real_refund = api_mod._refund_recap_credits

    async def spy_refund(user_id, count, *, job_id=None):
        # At the moment the balance refund runs, the persisted claim
        # must ALREADY be recorded (claim-before-money ordering).
        seen["blob"] = await _blob(maker, "rc_t4")
        return await real_refund(user_id, count, job_id=job_id)

    monkeypatch.setattr(api_mod, "_refund_recap_credits", spy_refund)
    entry = {
        "id": "rc_t4", "user_id": uid, "credits_charged": 1,
        "credit_deducted": True, "credit_refunded": False, "credits_refunded": 0,
    }
    assert await api_mod._refund_recap_job_charge(uid, entry) is True
    assert seen["blob"]["credit_refunded"] is True
    assert seen["blob"]["credits_refunded"] == 1


@pytest.mark.asyncio
async def test_failed_refund_releases_persisted_claim(tmp_path, monkeypatch):
    _engine, maker = await _open_store(tmp_path, monkeypatch)
    uid = await _make_user(maker, recap_credits=0)
    state = _recap_state(credits_charged=1)
    async with maker() as session:
        session.add(_job("rc_t5", uid, state))
        await session.commit()

    async def failing_refund(user_id, count, *, job_id=None):
        return False

    monkeypatch.setattr(api_mod, "_refund_recap_credits", failing_refund)
    entry = {
        "id": "rc_t5", "user_id": uid, "credits_charged": 1,
        "credit_deducted": True, "credit_refunded": False, "credits_refunded": 0,
    }
    assert await api_mod._refund_recap_job_charge(uid, entry) is False
    # Claim rolled back in BOTH places, so a later retry can refund.
    assert entry["credit_refunded"] is False
    blob = await _blob(maker, "rc_t5")
    assert blob["credit_refunded"] is False


@pytest.mark.asyncio
async def test_cancel_core_refunds_recorded_segmented_amount(tmp_path, monkeypatch):
    _engine, maker = await _open_store(tmp_path, monkeypatch)
    uid = await _make_user(maker, recap_credits=0)
    # Legacy-style row: persisted blob records a 3-credit segmented
    # charge; the registry has no entry (restart). The refund must be
    # the recorded 3, not a flat 1.
    state = _recap_state(credits_charged=3)
    async with maker() as session:
        job = _job("rc_t6", uid, state)
        session.add(job)
        await session.commit()
    api_mod.RECAP_JOBS_REGISTRY.pop("rc_t6", None)
    async with maker() as session:
        job = await session.get(Job, "rc_t6")
        info = await api_mod._cancel_recap_job_core(
            session, job,
            db_error_message="Cancelled", registry_error_message="Cancelled",
        )
    assert info["refunded"] is True
    assert info["recap_credits"] == 3
    assert await _balance(maker, uid) == 3
    blob = await _blob(maker, "rc_t6")
    assert blob["credit_refunded"] is True
    assert await _ledger_rows(maker, uid) == [("CREDIT", 3, "REFUND:rc_t6")]


@pytest.mark.asyncio
async def test_ledger_helpers_roundtrip(tmp_path, monkeypatch):
    _engine, maker = await _open_store(tmp_path, monkeypatch)
    uid = await _make_user(maker)
    async with maker() as session:
        assert await recap_refund_event_exists(session, uid, "rc_t7") is False
        await record_recap_credit_event(
            session, user_id=uid, direction="CREDIT", credits=1,
            reference_id="REFUND:rc_t7", note="t",
        )
        await session.commit()
    async with maker() as session:
        assert await recap_refund_event_exists(session, uid, "rc_t7") is True


# ── (5) price display unification ───────────────────────────────────


class _StubUser:
    language_code = "en"


@pytest.mark.asyncio
async def test_vip_plans_db_price_wins_and_catalogue_fills(tmp_path, monkeypatch):
    _engine, maker = await _open_store(tmp_path, monkeypatch)
    async with maker() as session:
        session.add(PricingPlan(
            code="MIN_30", minutes=31, price_mmk=99999,
            is_active=True, sort_order=2,
        ))
        await session.commit()
    async with maker() as session:
        result = await api_mod.get_vip_plans(_StubUser(), session)
    plans = {p["id"]: p for p in result["plans"]}
    assert set(plans) == {
        "MIN_10", "MIN_30", "MIN_60", "voice_studio_pro",
        "recap_starter", "recap_studio_pro", "combined_starter",
        "all_in_one_master",
    }
    # DB row wins over the catalogue for the seeded plan.
    assert plans["MIN_30"]["price"] == "99,999"
    assert plans["MIN_30"]["minutes"] == 31
    # No DB row: catalogue values, formatted exactly as before.
    assert plans["MIN_10"]["price"] == "8,000"
    assert plans["recap_starter"]["price"] == "20,000"
    assert plans["recap_starter"]["recap_credits"] == 5
    assert plans["all_in_one_master"]["price"] == "180,000"
    assert plans["voice_studio_pro"]["minutes"] == 250


# ── (6) broadcast report cosmetics ──────────────────────────────────


def test_broadcast_preblocked_is_own_block_not_breakdown_row():
    detail_start = APP_JS.index('<div class="adm-bc-detail">')
    detail_end = APP_JS.index("</div>", detail_start)
    detail_block = APP_JS[detail_start:detail_end]
    assert "admin_bc_preblocked" not in detail_block
    assert '<div class="adm-bc-note">${row(t("admin_bc_preblocked")' in APP_JS
    # Counting is untouched.
    assert '"pre_existing_blocked_in_db": db_blocked_count' in API_SRC


def test_broadcast_blocked_label_names_flag_not_status():
    assert "DB တွင် BLOCKED မှတ်ပြီး" not in I18N_JS
    assert "auto-marked BLOCKED in DB" not in I18N_JS
    assert "bot_blocked flag မှတ်ပြီး" in I18N_JS
    assert "bot_blocked flag recorded" in I18N_JS
    # The endpoint still sets ONLY the flag (label now matches code).
    assert ".values(bot_blocked=True)" in API_SRC


# ── (7) fallback disclosure ─────────────────────────────────────────


def test_fallback_persistent_notes_wired():
    assert 'id="voice-fallback-note"' in INDEX_HTML
    assert 'id="recap-fallback-note"' in INDEX_HTML
    # Voice helper defined once, rendered from BOTH success surfaces
    # (live poller + deep-link reopen): 1 definition + 2 call sites.
    assert APP_JS.count("renderVoiceFallbackNote(data)") == 3
    assert "function renderVoiceFallbackNote(data)" in APP_JS
    # Recap note is set on the result card, with the full-fallback
    # variant driven by the payload's segment totals.
    assert 'getElementById("recap-fallback-note")' in APP_JS
    assert 't("recap_fallback_note_all")' in APP_JS
    # Toasts are kept.
    assert 'showStudioToast(t("voice_fallback_notice")' in APP_JS
    assert 'showStudioToast(tf("recap_fallback_note"' in APP_JS


def test_fallback_wording_upgraded_everywhere():
    # Mini App i18n: new full-fallback key in both languages, and the
    # shared strings now say the sound may differ.
    assert I18N_JS.count("recap_fallback_note_all") == 2
    assert "may sound slightly different" in I18N_JS
    assert "ကွာခြားနိုင်ပါသည်" in I18N_JS
    # Bot locales: same upgrade, ⚠️ convention kept.
    for lang in ("my", "en"):
        data = json.loads((ROOT / f"locales/{lang}/messages.json").read_text(encoding="utf-8"))
        assert "⚠️" in data["worker_fallback_note"]
        assert "⚠️" in data["recap_fallback_note"]
    en = json.loads((ROOT / "locales/en/messages.json").read_text(encoding="utf-8"))
    assert "slightly different" in en["worker_fallback_note"]
    assert "slightly different" in en["recap_fallback_note"]

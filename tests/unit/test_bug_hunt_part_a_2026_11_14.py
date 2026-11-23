"""Bug Hunt Part A (2026-11-14, r150) — Regression pins for A1–A5.

A1: Admin TTS cost hint stays single-line across EN and MY (short tier hint hidden
    while ``shortEl.textContent`` remains populated with ``tts_cost_hint_admin``;
    non-admin tiers show both short and detailed hints).
A2: Recap render bitrate caps & ``apply_protection_strong(..., render_quality=...)``
    are pinned in ``tests/unit/test_recap_render_quality.py``.
A3: Shared Gemini KeyHealth persistence — generic ``postgres://``/``postgresql://``
    + asyncpg-param URL conversion, ``settings.database_url`` fallback when
    ``DATABASE_URL`` env is absent, atomic ``botsetting_sync_write`` upserts under
    concurrent writes, warning logs with credential redaction on store errors,
    cold rehydration from ``aj_kh:gemini_tts``, and ``flush_all_persistence()`` on
    lifespan shutdown / ``atexit``.
A4: ``WAITING_CHOICE`` (Batch DP) durability & Admin Queue visibility —
    simulated restart survival + resumer pickup, 48 h expiry refunds across
    recap credits, clone net seconds, and Option B credit+voice combo, Admin
    overview + queue ``waiting_choice`` counts/filter/cancel refund, and
    retention / orphan sweep preservation of parked jobs & ``dp_refs`` / media.
A5: Mobile 360x800 regression pins (verified NOT reproduced in headless Chromium) —
    pill ``.live-jobs-pill-main`` vertical stacking, terminal ``liveJobStageText``
    suppression of stale step labels, active recap hiding create buttons, and
    absence of the retired recap title overlay / restored boolean.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.key_rotation as kr
import app.services.voice_choice as vc
from app.models.enums import AccountCategory, JobStatus, JobType
from app.models.schema import BotSetting, Job, MinuteBalance, User
from app.services.ledger import (
    consume_seconds,
    get_minute_balance,
    get_or_create_user,
    record_recap_credit_event,
)

REPO = Path(__file__).resolve().parents[2]
INDEX_HTML = (REPO / "app/webapp/static/index.html").read_text(encoding="utf-8")
STYLE_CSS = (REPO / "app/webapp/static/css/style.css").read_text(encoding="utf-8")
APP_JS = (REPO / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (REPO / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
SERVER_PY = (REPO / "app/webapp/server.py").read_text(encoding="utf-8")


def _extract_js_fn(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    depth = 0
    in_body = False
    for idx in range(start, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
            in_body = True
        elif ch == "}":
            depth -= 1
            if in_body and depth == 0:
                return src[start : idx + 1]
    raise AssertionError(f"Unclosed JS function {name}")


def test_r150_cache_token():
    assert INDEX_HTML.count("?v=20261123_round14") == 5
    assert "20261114_r149" not in INDEX_HTML


# ── A1: Admin TTS cost hint single-line vs tier hints ───────────────

def test_a1_paint_tts_cost_hint_tiered_admin_hides_short_line():
    fn_src = _extract_js_fn(APP_JS, "paintTtsCostHintTiered")
    node_script = """
const vm = require("vm");
const fs = require("fs");
const i18nSrc = fs.readFileSync(process.argv[1], "utf8");
const fnSrc = process.argv[2];

function mkEl() {
  const classes = new Set();
  return {
    textContent: "",
    classList: {
      toggle(cls, force) {
        if (force === undefined) {
          if (classes.has(cls)) classes.delete(cls);
          else classes.add(cls);
        } else if (force) {
          classes.add(cls);
        } else {
          classes.delete(cls);
        }
      },
      contains(cls) { return classes.has(cls); },
    },
  };
}

function runFor(lang, profile, quota) {
  const shortEl = mkEl();
  const moreEl = mkEl();
  const ctx = {
    window: {},
    localStorage: { getItem: () => lang, setItem: () => {} },
    state: { user: profile },
    document: {
      querySelector(sel) {
        if (sel === '#tab-tts [data-i18n="tts_cost_hint"]') return shortEl;
        if (sel === '#tts-cost-hint-more [data-i18n="tts_cost_hint_more"]') return moreEl;
        return null;
      },
    },
  };
  vm.runInNewContext(i18nSrc + "; globalThis.__tr = translations; currentLang = '" + lang + "'; " + fnSrc, ctx);
  ctx.paintTtsCostHintTiered(quota, profile);
  return {
    shortText: shortEl.textContent,
    shortHidden: shortEl.classList.contains("hidden"),
    moreText: moreEl.textContent,
    adminExpected: ctx.__tr[lang].tts_cost_hint_admin,
  };
}

const out = {
  adminEn: runFor("en", { is_admin: true, character_limit: 5000 }, { is_admin: true }),
  adminMy: runFor("my", { is_admin: true, character_limit: 5000 }, { is_admin: true }),
  vipRefEn: runFor("en", { is_admin: false, is_vip: true, tts_referral_seconds: 120, character_limit: 5000 }, { is_admin: false, referral_seconds: 120 }),
  freeNoRefEn: runFor("en", { is_admin: false, is_vip: false, tts_referral_seconds: 0, character_limit: 1000 }, { is_admin: false, referral_seconds: 0 }),
};
console.log(JSON.stringify(out));
"""
    proc = subprocess.run(
        ["node", "-e", node_script, str(REPO / "app/webapp/static/js/i18n.js"), fn_src],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    res = json.loads(proc.stdout.strip())
    for key in ("adminEn", "adminMy"):
        assert res[key]["shortHidden"] is True
        assert res[key]["shortText"] == res[key]["adminExpected"]
        assert "Admin" in res[key]["moreText"] or "5,000" in res[key]["moreText"]
    assert res["vipRefEn"]["shortHidden"] is False
    assert "referral" in res["vipRefEn"]["shortText"].lower()
    assert res["freeNoRefEn"]["shortHidden"] is False
    assert "15 min" in res["freeNoRefEn"]["shortText"]


# ── A3: KeyHealth sync-store, URL conversion & flush on shutdown ─────

def _create_bot_settings_db(db_path: Path) -> str:
    url = f"sqlite:///{db_path}"
    eng = create_engine(url)
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS bot_settings ("
                "key VARCHAR(100) PRIMARY KEY, "
                "value TEXT NOT NULL, "
                "updated_at DATETIME)"
            )
        )
    eng.dispose()
    return url


def test_a3_to_sync_url_conversions_and_param_stripping():
    assert kr._to_sync_url("sqlite+aiosqlite:///./a.db") == "sqlite:///./a.db"
    assert (
        kr._to_sync_url("postgres://u:p@db.example.com:5432/appdb")
        == "postgresql+psycopg2://u:p@db.example.com:5432/appdb"
    )
    assert (
        kr._to_sync_url("postgresql://u:p@db.example.com:5432/appdb")
        == "postgresql+psycopg2://u:p@db.example.com:5432/appdb"
    )
    converted = kr._to_sync_url(
        "postgresql+asyncpg://u:p@pooler.supabase.com:6543/postgres"
        "?ssl=require&statement_cache_size=0&prepared_statement_cache_size=0&connect_timeout=10"
    )
    assert converted.startswith("postgresql+psycopg2://u:p@pooler.supabase.com:6543/postgres?")
    assert "statement_cache_size" not in converted
    assert "prepared_statement_cache_size" not in converted
    assert "sslmode=require" in converted
    assert "connect_timeout=10" in converted


def test_a3_sync_engine_uses_settings_database_url_without_env(tmp_path, monkeypatch):
    from app.config import settings

    db_file = tmp_path / "settings_fallback.db"
    _create_bot_settings_db(db_file)
    kr.configure_sync_store(None)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_file}")
    try:
        assert kr.botsetting_sync_write("k_fallback", "v_ok") is True
        assert kr.botsetting_sync_read("k_fallback") == "v_ok"
    finally:
        kr.configure_sync_store(None)


def test_a3_atomic_upsert_concurrent_writes(tmp_path):
    db_file = tmp_path / "concurrent.db"
    url = _create_bot_settings_db(db_file)
    kr.configure_sync_store(url)
    errors: list[Exception] = []
    barrier = threading.Barrier(6)

    def _worker(idx: int) -> None:
        try:
            barrier.wait(timeout=5)
            for n in range(10):
                assert kr.botsetting_sync_write("aj_kh:race_key", f"w{idx}-{n}") is True
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    try:
        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert not errors
        assert re.fullmatch(r"w\d-\d", kr.botsetting_sync_read("aj_kh:race_key") or "")
    finally:
        kr.configure_sync_store(None)


def test_a3_store_failure_logs_warning_and_redacts_creds(caplog, monkeypatch):
    class _BrokenConn:
        def __enter__(self):
            raise RuntimeError("boom postgresql://admin:SuperSecretPass@db.host:5432/vcp")

        def __exit__(self, *args):
            return False

    class _BrokenEngine:
        def connect(self):
            return _BrokenConn()

        def begin(self):
            return _BrokenConn()

    monkeypatch.setattr(kr, "_sync_engine", lambda: _BrokenEngine())
    with caplog.at_level(logging.WARNING, logger="app.services.key_rotation"):
        assert kr.botsetting_sync_read("aj_kh:gemini_tts") is None
        assert kr.botsetting_sync_write("aj_kh:gemini_tts", '{"secret": "AIzaSyDontLeak"}') is False
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 2
    joined = "\n".join(warnings)
    assert "SuperSecretPass" not in joined
    assert "***:***@" in joined
    assert "AIzaSyDontLeak" not in joined


def test_a3_flush_all_persistence_and_subprocess_rehydration(tmp_path, monkeypatch):
    db_file = tmp_path / "gemini_tts_persist.db"
    sync_url = _create_bot_settings_db(db_file)
    async_url = f"sqlite+aiosqlite:///{db_file}"
    kr.configure_sync_store(sync_url)
    monkeypatch.setattr(kr, "_REGISTRY", {})
    raw_key = "AIzaSy-synthetic-test-key-0001"
    try:
        health = kr.health_for("gemini_tts")
        # Trigger mark_failure (schedules 2.0 s debounced timer); flush immediately
        # without sleeping 2 s and confirm subprocess rehydrates cooldown.
        health.mark_failure(raw_key, cooldown_seconds=600)
        assert kr.flush_all_persistence() >= 1

        stored = kr.botsetting_sync_read("aj_kh:gemini_tts")
        assert stored is not None
        assert raw_key not in stored
        assert kr.key_id(raw_key) in stored

        probe_code = (
            "import os; "
            "from app.services import key_rotation as kr; "
            "h = kr.health_for('gemini_tts'); "
            f"print(int(h.in_cooldown({raw_key!r})))"
        )
        env = dict(os.environ)
        env["DATABASE_URL"] = async_url
        proc = subprocess.run(
            [sys.executable, "-c", probe_code],
            cwd=str(REPO),
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc.stdout.strip().splitlines()[-1] == "1"
    finally:
        for h in list(kr._REGISTRY.values()):
            timer = getattr(h, "_persist_timer", None)
            if timer is not None:
                timer.cancel()
        monkeypatch.setattr(kr, "_REGISTRY", {})
        kr.configure_sync_store(None)


def test_a3_server_lifespan_calls_flush_all_persistence():
    assert "flush_all_persistence()" in SERVER_PY


# ── A4: WAITING_CHOICE persistence, refunds, Admin Queue & sweeps ───

async def _mk_a4_user(session, user_id: int, *, credits: int = 5, seconds: int = 600, is_admin: bool = False):
    from app.config import settings

    user, _created = await get_or_create_user(session, user_id, username=f"a4_{user_id}")
    user.account_category = AccountCategory.PURCHASED.value
    user.recap_credits = credits
    if is_admin:
        cur = str(settings.admin_ids_raw or "").strip()
        settings.admin_ids_raw = f"{cur},{user_id}" if cur else str(user_id)
    await session.commit()
    bal = await get_minute_balance(session, user.id)
    bal.purchased_seconds = seconds
    await session.commit()
    return user


def _waiting_state(kind: str, *, choice: str | None = None, parked_at: float | None = None) -> str:
    state = vc.new_choice_state(kind)
    state["choice"] = choice
    if parked_at is not None:
        state["parked_at"] = parked_at
    state["prompt_chat_id"] = 70001
    state["prompt_message_id"] = 101
    return vc.dump_choice_state(state)


@pytest.mark.asyncio
async def test_a4_parked_job_survives_restart_and_resumer_picks_up(db_session, monkeypatch):
    import app.webapp.api as api

    sf = async_sessionmaker(bind=db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", sf)

    user = await _mk_a4_user(db_session, 84001)
    job = Job(
        id="rc_a4_restart_1",
        user_id=user.id,
        job_type=JobType.RECAP.value,
        engine="recap",
        status=JobStatus.WAITING_CHOICE.value,
        input_text="Parked Video",
        chat_id=user.id,
        voice_choice_state=_waiting_state("recap", choice="edge_mix"),
    )
    db_session.add(job)
    await db_session.commit()

    # Simulate cold process: empty RECAP_JOBS_REGISTRY and fresh session factory.
    monkeypatch.setattr(api, "RECAP_JOBS_REGISTRY", {})
    counts = await vc.sweep_parked_jobs()
    assert counts["resumed"] == 1

    async with sf() as fresh_sess:
        fresh = await fresh_sess.get(Job, "rc_a4_restart_1")
        assert fresh is not None
        assert fresh.status == JobStatus.QUEUED.value
        assert vc.parse_choice_state(fresh.voice_choice_state)["choice"] == "edge_mix"


@pytest.mark.asyncio
async def test_a4_48h_expiry_refunds_across_all_tiers(db_session, monkeypatch):
    import app.webapp.api as api

    sf = async_sessionmaker(bind=db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", sf)
    monkeypatch.setattr(api, "AsyncSessionLocal", sf)
    monkeypatch.setattr(vc, "voxcpm_capacity_available", lambda: False)
    monkeypatch.setattr(api, "_send_recap_failure_dm", AsyncMock(return_value=True))
    monkeypatch.setattr(api, "RECAP_JOBS_REGISTRY", {})

    stale_ts = time.time() - (vc.PARK_SAFETY_VALVE_SECONDS + 120)

    # 1. Standard Recap: 2 credits deducted + ledger debit -> refunded back to 5 credits
    u_recap = await _mk_a4_user(db_session, 84010, credits=3, seconds=0)
    await record_recap_credit_event(
        db_session, user_id=u_recap.id, direction="DEBIT", credits=2, reference_id="rc_a4_exp_std"
    )
    db_session.add(
        Job(
            id="rc_a4_exp_std",
            user_id=u_recap.id,
            job_type=JobType.RECAP.value,
            engine="recap",
            status=JobStatus.WAITING_CHOICE.value,
            input_text="Std Recap",
            chat_id=u_recap.id,
            voice_choice_state=_waiting_state("recap", parked_at=stale_ts),
            recap_config=json.dumps({
                "config": {"url": "https://example.com/v.mp4", "title": "Std", "voice_slug": "komin"},
                "is_vip": True, "phase": "full",
                "credit_deducted": True, "credits_charged": 2,
                "credit_refunded": False, "credits_refunded": 0,
            }),
        )
    )

    # 2. Voice Clone: 90s debited via consume_seconds -> refunded back to 600s
    u_clone = await _mk_a4_user(db_session, 84011, credits=0, seconds=600)
    clone_id = str(uuid.uuid4())
    await consume_seconds(db_session, u_clone.id, 90, job_id=clone_id, note="A4 clone hold")
    db_session.add(
        Job(
            id=clone_id,
            user_id=u_clone.id,
            job_type=JobType.VOICE_CLONE.value,
            engine="voxcpm",
            status=JobStatus.WAITING_CHOICE.value,
            input_text="Clone text",
            chat_id=u_clone.id,
            voice_choice_state=_waiting_state("clone", parked_at=stale_ts),
        )
    )

    # 3. Option B Combo Recap (own-voice clone): 1 credit + 120s voice held -> BOTH refunded
    u_optb = await _mk_a4_user(db_session, 84012, credits=4, seconds=500)
    optb_id = "rc_a4_exp_optb"
    await record_recap_credit_event(
        db_session, user_id=u_optb.id, direction="DEBIT", credits=1, reference_id=optb_id
    )
    await consume_seconds(db_session, u_optb.id, 120, job_id=optb_id, note="Option B voice hold")
    db_session.add(
        Job(
            id=optb_id,
            user_id=u_optb.id,
            job_type=JobType.RECAP.value,
            engine="recap",
            status=JobStatus.WAITING_CHOICE.value,
            input_text="Own Voice Recap",
            chat_id=u_optb.id,
            voice_choice_state=_waiting_state("recap", parked_at=stale_ts),
            recap_config=json.dumps({
                "config": {
                    "url": "https://example.com/v2.mp4",
                    "title": "OptB",
                    "voice_slug": "own_voice",
                    "own_voice_mode": "clone",
                },
                "is_vip": True, "phase": "full",
                "credit_deducted": True, "credits_charged": 1,
                "credit_refunded": False, "credits_refunded": 0,
                "billing_mode": api._RECAP_BILLING_CREDIT_PLUS_VOICE,
                "voice_seconds_held": 120,
                "voice_refunded": False,
                "voice_seconds_refunded": 0,
            }),
        )
    )
    await db_session.commit()

    counts = await vc.sweep_parked_jobs()
    assert counts["expired"] == 3

    db_session.expunge_all()
    assert (await db_session.get(Job, "rc_a4_exp_std")).status == JobStatus.FAILED.value
    assert (await db_session.get(Job, clone_id)).status == JobStatus.FAILED.value
    assert (await db_session.get(Job, optb_id)).status == JobStatus.FAILED.value

    assert (await db_session.get(User, u_recap.id)).recap_credits == 5
    assert (await get_minute_balance(db_session, u_clone.id)).purchased_seconds == 600
    assert (await db_session.get(User, u_optb.id)).recap_credits == 5
    assert (await get_minute_balance(db_session, u_optb.id)).purchased_seconds == 500


@pytest.mark.asyncio
async def test_a4_admin_overview_queue_filter_and_cancel_refund(db_session, monkeypatch):
    import app.webapp.api as api

    sf = async_sessionmaker(bind=db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", sf)
    monkeypatch.setattr(api, "AsyncSessionLocal", sf)
    monkeypatch.setattr(api, "RECAP_JOBS_REGISTRY", {})

    admin = await _mk_a4_user(db_session, 84020, credits=0, seconds=0, is_admin=True)
    user = await _mk_a4_user(db_session, 84021, credits=4, seconds=600)
    await record_recap_credit_event(
        db_session, user_id=user.id, direction="DEBIT", credits=1, reference_id="rc_a4_adm_1"
    )
    await consume_seconds(db_session, user.id, 100, job_id="rc_a4_adm_1", note="Option B hold")
    db_session.add(
        Job(
            id="rc_a4_adm_1",
            user_id=user.id,
            job_type=JobType.RECAP.value,
            engine="recap",
            status=JobStatus.WAITING_CHOICE.value,
            input_text="Admin Visible Parked",
            chat_id=user.id,
            voice_choice_state=_waiting_state("recap"),
            recap_config=json.dumps({
                "config": {"url": "https://example.com/v3.mp4", "title": "Adm", "voice_slug": "own_voice"},
                "is_vip": True, "phase": "full",
                "credit_deducted": True, "credits_charged": 1,
                "credit_refunded": False, "credits_refunded": 0,
                "billing_mode": api._RECAP_BILLING_CREDIT_PLUS_VOICE,
                "voice_seconds_held": 100,
                "voice_refunded": False,
                "voice_seconds_refunded": 0,
            }),
        )
    )
    await db_session.commit()

    ov = await api.get_admin_overview(admin=admin, db=db_session)
    assert ov["waiting_choice_jobs"] == 1
    assert ov["jobs_by_status"]["WAITING_CHOICE"] == 1

    q_filtered = await api.get_admin_queue(
        status="waiting_choice", engine="all", limit=20, admin=admin, db=db_session
    )
    assert q_filtered["counts"]["waiting_choice"] == 1
    assert [j["id"] for j in q_filtered["jobs"]] == ["rc_a4_adm_1"]

    res = await api.cancel_queued_job("rc_a4_adm_1", admin=admin, db=db_session)
    assert res["success"] is True
    assert res["refund"]["refunded"] is True

    db_session.expunge_all()
    cancelled = await db_session.get(Job, "rc_a4_adm_1")
    assert cancelled.status == JobStatus.CANCELLED.value
    assert (await db_session.get(User, user.id)).recap_credits == 5
    assert (await get_minute_balance(db_session, user.id)).purchased_seconds == 600


@pytest.mark.asyncio
async def test_a4_retention_and_orphan_sweeps_preserve_waiting_choice_artifacts(db_session, tmp_path, monkeypatch):
    import app.webapp.api as api
    from app.config import settings

    sf = async_sessionmaker(bind=db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.db.session.AsyncSessionLocal", sf)
    monkeypatch.setattr(settings, "storage_root", tmp_path)

    user = await _mk_a4_user(db_session, 84030)
    parked_id = "rc_a4_keep_1"
    recap_dir = tmp_path / "recap_output" / parked_id
    recap_dir.mkdir(parents=True, exist_ok=True)
    recap_clip = recap_dir / "checkpoint_chunk.mp4"
    recap_clip.write_bytes(b"video-data")

    dp_refs_dir = tmp_path / "dp_refs"
    dp_refs_dir.mkdir(parents=True, exist_ok=True)
    dp_ref = dp_refs_dir / "clone_ref.wav"
    dp_ref.write_bytes(b"RIFFref")

    temp_job_dir = tmp_path / "temp" / parked_id
    temp_job_dir.mkdir(parents=True, exist_ok=True)
    temp_marker = temp_job_dir / "work.txt"
    temp_marker.write_text("keep", encoding="utf-8")

    # Backdate files & created_at past retention window so only WAITING_CHOICE protection saves them.
    old_epoch = time.time() - 10 * 86400
    for f in (recap_clip, dp_ref, temp_marker):
        os.utime(f, (old_epoch, old_epoch))

    db_session.add(
        Job(
            id=parked_id,
            user_id=user.id,
            job_type=JobType.RECAP.value,
            engine="recap",
            status=JobStatus.WAITING_CHOICE.value,
            input_text="Aged Parked Job",
            chat_id=user.id,
            source_audio_path=str(dp_ref),
            voice_choice_state=_waiting_state("recap"),
            created_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
    )
    await db_session.commit()

    purged = await api.cleanup_expired_jobs(retention_days=0)
    assert purged == 0
    stats = await api._sweep_storage_orphans(retention_cutoff_ts=time.time() + 60)
    assert stats["dirs"] == 0
    assert await db_session.get(Job, parked_id) is not None
    assert recap_clip.is_file()
    assert dp_ref.is_file()
    assert temp_marker.is_file()


def test_a4_admin_ui_wires_waiting_choice_filter_banner_and_i18n():
    assert 'data-status="waiting_choice"' in INDEX_HTML
    assert 'id="cnt-waiting-choice"' in INDEX_HTML
    assert '"WAITING_CHOICE"' in APP_JS
    assert "admin_queue_parked_title" in I18N_JS
    assert "badge_waiting_choice" in I18N_JS
    assert "filter_waiting_choice" in I18N_JS
    assert ".adm-status-tag.waiting_choice" in STYLE_CSS
    assert ".active-queue-status-banner.waiting_choice" in STYLE_CSS


# ── A5: Mobile 360x800 UX regression pins (not reproduced) ──────────

def test_a5_live_pill_stacking_terminal_stage_and_create_cta_pins():
    # 1. Floating pill stacks vertically inside flex-direction: column container
    pill_rule = re.search(r"\.live-jobs-pill-main\s*\{[^}]+\}", STYLE_CSS)
    assert pill_rule is not None
    assert "flex-direction: column" in pill_rule.group(0)

    # 2. Terminal jobs never surface stale progress_stage / progress_label;
    # 3. Active recap hides both #btn-start-recap and #sticky-cta-recap
    stage_fn = _extract_js_fn(APP_JS, "liveJobStageText")
    cta_fn = _extract_js_fn(APP_JS, "recapJobActive")
    node_script = """
const vm = require("vm");
const fs = require("fs");
const i18nSrc = fs.readFileSync(process.argv[1], "utf8");
const stageSrc = process.argv[2];
const ctaSrc = process.argv[3];

const ctx = {
  window: {},
  localStorage: { getItem: () => "my", setItem: () => {} },
  liveJobsIsTerminal: s => s === "SUCCESS" || s === "FAILED" || s === "CANCELLED",
  LiveJobs: { activeJobs: () => [{ id: "rc_1", kind: "recap", status: "RUNNING" }] },
  recapJobIsActive: () => false,
};
vm.runInNewContext(i18nSrc + "; " + stageSrc + "; " + ctaSrc, ctx);
const doneText = ctx.liveJobStageText({
  status: "SUCCESS",
  progress_stage: "downloading",
  progress_label: "99% stale",
});
const blocked = ctx.recapJobActive();
console.log(JSON.stringify({ doneText, blocked }));
"""
    proc = subprocess.run(
        ["node", "-e", node_script, str(REPO / "app/webapp/static/js/i18n.js"), stage_fn, cta_fn],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    out = json.loads(proc.stdout.strip())
    assert "downloading" not in out["doneText"].lower()
    assert "stale" not in out["doneText"].lower()
    assert out["blocked"] is True

    # 4 & 5. Retired recap title overlay DOM & restored boolean stay absent
    assert 'id="recap-title-box"' not in INDEX_HTML
    assert "recapTitleEnabled" not in APP_JS

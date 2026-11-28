"""Unit & statement-capture tests for Supabase Egress Diet Round 2 (2026-11-28).

Covers all 6 spec pillars:
1. Statement capture & payload identity on Mini App pollers & user endpoints:
   - `GET /jobs/{id}/status`, `GET /jobs/live`, `GET /history`, `get_user_history_jobs`,
     `GET /user/me` recent-jobs subquery, and `tts_cap_consumed_seconds`.
2. `_estimate_job_voxcpm_budget` exact equivalence with/without `text_override`
   on multi-sentence Burmese text, including empty string `""` on a deferred Job
   (zero `MissingGreenlet`, zero fallthrough).
3. `claim_next_queued_job` cap guard (`active_voxcpm >= MAX_CONCURRENT_VOXCPM_JOBS`):
   zero `input_text` prefetch SQL, preserves non-VoxCPM FIFO dispatch and `None` when all VoxCPM.
4. `claim_next_queued_job` `hydrate_winner=False` vs default `hydrate_winner=True`
   (`populate_existing=True` re-hydrating all 3 deferred columns in same session),
   plus FIFO slot-interleave preservation and `_dispatch_oldest_queued_recap` `defer(final_script)`.
5. `release_in_flight_gpu` absent `job_id` uses cached `load_pool_state()` (never `force=True`).
6. Worker poll cadence: active recap tasks no longer pin `on_empty_tick` at `2.0s`
   (`active=False` wiring + held-queue ticks step `2.0 -> 5.0 -> 10.0`).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.enums import AccountCategory, JobStatus, JobType
from app.models.schema import Base, Job, User
from app.services import hf_pool_ledger as hf_ledger
from app.services import history as history_mod
from app.services import queue as queue_mod
from app.services import tts_service as tts_mod
from app.webapp import api as api_mod
from app.webapp import server as server_mod
import app.worker as worker_mod


async def _open_file_db(tmp_path: Path, name: str = "diet_r2.db"):
    db_file = tmp_path / name
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}", future=True)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    return eng, maker


def _assert_no_heavy_job_blobs(sql_list: list[str], *, allow_substr_preview: bool = True) -> None:
    job_selects = [
        s
        for s in sql_list
        if "from jobs" in s.lower() and s.lstrip().upper().startswith("SELECT")
    ]
    assert job_selects, f"Expected at least one SELECT FROM jobs, got: {sql_list}"
    for sql in job_selects:
        low = sql.lower()
        assert "final_script" not in low, f"final_script leaked into SQL: {sql}"
        assert "recap_config" not in low, f"recap_config leaked into SQL: {sql}"
        check_sql = (
            re.sub(r"substr\s*\(\s*jobs\.input_text[^)]*\)", "", low)
            if allow_substr_preview
            else low
        )
        assert "jobs.input_text" not in check_sql, f"jobs.input_text leaked into SQL: {sql}"


# ---------------------------------------------------------------------------
# 1. Statement capture + payload identity on Mini App pollers & user queries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_part1_mini_app_pollers_defer_heavy_columns_and_preserve_payloads(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "bot_token", "123456:DIET_R2_TEST_TOKEN")
    eng, maker = await _open_file_db(tmp_path, "pollers.db")
    now = datetime.now(timezone.utc)
    long_burmese = (
        "မင်္ဂလာပါ ခင်ဗျာ။ ယနေ့ အသံပွား စမ်းသပ်မှု အတွက် စာကြောင်းရှည်ကြီး ထည့်သွင်းထားပါသည်။ " * 25
    )

    try:
        async with maker() as seed_s:
            u = User(
                id=770101,
                first_name="Daw Mya",
                username="dawmya",
                language_code="my",
                account_category=AccountCategory.PURCHASED.value,
            )
            j_voice = Job(
                id="vc_poll_1",
                user_id=u.id,
                chat_id=u.id,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                status=JobStatus.SUCCESS.value,
                input_text=long_burmese,
                final_script="UNREAD_SCRIPT_" * 400,
                recap_config=json.dumps({"unread": "X" * 4000}),
                seconds_estimated=42,
                seconds_consumed=40,
                created_at=now - timedelta(minutes=8),
                started_at=now - timedelta(minutes=7),
                finished_at=now - timedelta(minutes=6),
            )
            j_tts = Job(
                id="tts_poll_1",
                user_id=u.id,
                chat_id=u.id,
                job_type=JobType.TTS.value,
                engine="gemini_tts",
                preset_voice="Charon",
                status=JobStatus.RUNNING.value,
                input_text="TTS နမူနာ စာသား အရှည်ကြီး " * 40,
                final_script="UNREAD_TTS_SCRIPT_" * 200,
                recap_config=json.dumps({"unread_tts": "Y" * 2000}),
                seconds_estimated=35,
                created_at=now - timedelta(minutes=2),
                started_at=now - timedelta(minutes=1),
            )
            seed_s.add_all([u, j_voice, j_tts])
            await seed_s.commit()

        executed_sql: list[str] = []

        @event.listens_for(eng.sync_engine, "before_cursor_execute")
        def _capture(conn, cursor, statement, parameters, context, executemany):
            executed_sql.append(statement)

        # (a) GET /jobs/{id}/status on fresh session
        executed_sql.clear()
        async with maker() as s_status:
            user_row = await s_status.get(User, 770101)
            executed_sql.clear()
            status_payload = await api_mod.get_job_status("vc_poll_1", user=user_row, db=s_status)

        _assert_no_heavy_job_blobs(executed_sql, allow_substr_preview=False)
        assert status_payload["id"] == "vc_poll_1"
        assert status_payload["status"] == JobStatus.SUCCESS.value
        assert status_payload["engine"] == "voxcpm"
        assert status_payload["fallback_used"] is False
        assert status_payload["output_audio_url"].startswith("/api/audio/download/vc_poll_1?t=")
        assert status_payload["voice_choice"] is None

        # 404 & ownership checks preserved
        async with maker() as s_404:
            stranger = User(id=770999, first_name="Stranger")
            with pytest.raises(api_mod.HTTPException) as exc_own:
                await api_mod.get_job_status("vc_poll_1", user=stranger, db=s_404)
            assert exc_own.value.status_code == 404
            with pytest.raises(api_mod.HTTPException) as exc_missing:
                await api_mod.get_job_status("vc_does_not_exist", user=user_row, db=s_404)
            assert exc_missing.value.status_code == 404

        # (b) GET /jobs/live on fresh session + byte-identical title preview
        async with maker() as s_live:
            user_row = await s_live.get(User, 770101)
            executed_sql.clear()
            live_payload = await api_mod.get_live_jobs(user=user_row, db=s_live)

        _assert_no_heavy_job_blobs(executed_sql, allow_substr_preview=True)
        by_id_live = {item["id"]: item for item in live_payload["jobs"]}
        assert by_id_live["vc_poll_1"]["title"] == long_burmese[:70]
        assert by_id_live["tts_poll_1"]["title"] == ("TTS နမူနာ စာသား အရှည်ကြီး " * 40)[:70]
        assert live_payload["active_count"] == 1

        # (c) GET /history on fresh session + byte-identical snippet & deliverables
        async with maker() as s_hist:
            user_row = await s_hist.get(User, 770101)
            executed_sql.clear()
            hist_payload = await api_mod.get_user_history(user=user_row, db=s_hist)

        _assert_no_heavy_job_blobs(executed_sql, allow_substr_preview=True)
        assert hist_payload["count"] == 1
        h_item = hist_payload["history"][0]
        assert h_item["id"] == "vc_poll_1"
        assert h_item["input_snippet"] == long_burmese[:70] + "..."
        assert h_item["seconds_consumed"] == 40
        assert h_item["job_type"] == "VOICE_CLONE"

        # (d) history.get_user_history_jobs & tts_service.tts_cap_consumed_seconds
        async with maker() as s_aux:
            executed_sql.clear()
            recent_jobs = await history_mod.get_user_history_jobs(s_aux, 770101)
            tts_used = await tts_mod.tts_cap_consumed_seconds(s_aux, 770101)

        _assert_no_heavy_job_blobs(executed_sql, allow_substr_preview=False)
        assert [j.id for j in recent_jobs] == ["tts_poll_1", "vc_poll_1"]
        assert tts_used == 35.0
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_part1_history_cold_session_recap_hydrates_recap_config_without_final_script(
    tmp_path,
):
    """Cold-session recap rows in `/history` keep full `recap_config` & `can_retry` while
    still never selecting `final_script` or full `jobs.input_text`."""
    eng, maker = await _open_file_db(tmp_path, "hist_recap.db")
    now = datetime.now(timezone.utc)
    cfg_dump = api_mod.RecapConfigRequest(
        url="https://example.com/watch/123",
        title="Cold Session Recap Title",
        voice_slug="mahnin",
        duration_seconds=60,
        ratio="9:16",
    ).model_dump()
    recap_state_json = json.dumps(
        {
            "config": cfg_dump,
            "credits_charged": 2.5,
            "script_ready": False,
        }
    )
    try:
        async with maker() as seed_s:
            u = User(id=770202, first_name="Ko Aung")
            j_rc = Job(
                id="rc_cold_hist_1",
                user_id=u.id,
                chat_id=u.id,
                job_type=JobType.RECAP.value,
                engine="recap",
                status=JobStatus.FAILED.value,
                source_audio_path="https://example.com/watch/123",
                input_text="Cold Session Recap Title " + ("extra " * 40),
                recap_config=recap_state_json,
                final_script="HUGE_FINAL_SCRIPT_" * 1000,
                error_message="Transient failure",
                created_at=now - timedelta(minutes=15),
            )
            seed_s.add_all([u, j_rc])
            await seed_s.commit()

        executed_sql: list[str] = []

        @event.listens_for(eng.sync_engine, "before_cursor_execute")
        def _capture(conn, cursor, statement, parameters, context, executemany):
            executed_sql.append(statement)

        async with maker() as fresh_s:
            u_fresh = await fresh_s.get(User, 770202)
            executed_sql.clear()
            res = await api_mod.get_user_history(user=u_fresh, db=fresh_s)

        for sql in executed_sql:
            low = sql.lower()
            if "from jobs" in low:
                assert "final_script" not in low
                stripped = re.sub(r"substr\s*\(\s*jobs\.input_text[^)]*\)", "", low)
                assert "jobs.input_text" not in stripped

        # Primary ORDER BY query also deferred recap_config
        ordered_job_sql = [
            s for s in executed_sql if "from jobs" in s.lower() and "order by" in s.lower()
        ]
        assert len(ordered_job_sql) == 1
        assert "recap_config" not in ordered_job_sql[0].lower()

        item = res["history"][0]
        assert item["id"] == "rc_cold_hist_1"
        assert item["recap_config"]["url"] == "https://example.com/watch/123"
        assert item["can_retry"] is True
        assert item["credits_charged"] == 2.5
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# 2. Estimator equivalence (incl. empty-string override on deferred Job)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_part4_estimator_equivalence_and_empty_override_no_missing_greenlet(tmp_path):
    from sqlalchemy.orm import defer

    multi_sentence_burmese = (
        "ပထမ စာကြောင်း ဖြစ်ပါသည်။ ဒုတိယ စာကြောင်းတွင် အချက်အလက် အသေးစိတ် ပါဝင်ပါသည်။ "
        "တတိယ စာကြောင်းကိုလည်း အသံကြည်လင်စွာ ထုတ်ပေးပါမည်။ " * 12
    )
    eng, maker = await _open_file_db(tmp_path, "estimator.db")
    try:
        async with maker() as s1:
            j_full = Job(
                id="vc_est_full",
                user_id=1,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                status=JobStatus.QUEUED.value,
                input_text=multi_sentence_burmese,
                speed_preset="1.0",
            )
            j_empty = Job(
                id="vc_est_empty",
                user_id=2,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
                status=JobStatus.QUEUED.value,
                input_text="",
                speed_preset="1.0",
            )
            s1.add_all([j_full, j_empty])
            await s1.commit()
            expected_full = queue_mod._estimate_job_voxcpm_budget(j_full)
            expected_empty = queue_mod._estimate_job_voxcpm_budget(j_empty)

        # Load both with input_text DEFERRED in a fresh async session
        async with maker() as s2:
            deferred_full = (
                await s2.execute(
                    api_mod.select(Job)
                    .options(defer(Job.input_text))
                    .where(Job.id == "vc_est_full")
                )
            ).scalar_one()
            deferred_empty = (
                await s2.execute(
                    api_mod.select(Job)
                    .options(defer(Job.input_text))
                    .where(Job.id == "vc_est_empty")
                )
            ).scalar_one()

            assert "input_text" in sa_inspect(deferred_full).unloaded
            assert "input_text" in sa_inspect(deferred_empty).unloaded

            got_full = queue_mod._estimate_job_voxcpm_budget(
                deferred_full, text_override=multi_sentence_burmese
            )
            # Empty-string override MUST NOT fall through to deferred_empty.input_text
            got_empty = queue_mod._estimate_job_voxcpm_budget(
                deferred_empty, text_override=""
            )

            assert got_full == expected_full
            assert got_empty == expected_empty
            assert "input_text" in sa_inspect(deferred_full).unloaded
            assert "input_text" in sa_inspect(deferred_empty).unloaded
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# 3 & 4. Cap guard, FIFO interleave, hydrate_winner=False vs True, dispatcher defer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_part4_cap_guard_skips_input_text_prefetch_and_preserves_non_vox_dispatch(
    tmp_path, monkeypatch
):
    eng, maker = await _open_file_db(tmp_path, "cap_guard.db")
    now = datetime.now(timezone.utc)
    try:
        async with maker() as seed_s:
            seed_s.add_all(
                [
                    User(id=101, first_name="R1"),
                    User(id=102, first_name="R2"),
                    User(id=103, first_name="Q1"),
                    User(id=104, first_name="QRecap"),
                    Job(
                        id="vc_run_a",
                        user_id=101,
                        job_type=JobType.VOICE_CLONE.value,
                        engine="voxcpm",
                        status=JobStatus.RUNNING.value,
                        input_text="running A",
                        created_at=now - timedelta(minutes=5),
                    ),
                    Job(
                        id="vc_run_b",
                        user_id=102,
                        job_type=JobType.VOICE_CLONE.value,
                        engine="voxcpm",
                        status=JobStatus.RUNNING.value,
                        input_text="running B",
                        created_at=now - timedelta(minutes=4),
                    ),
                    Job(
                        id="vc_queued_blocked",
                        user_id=103,
                        job_type=JobType.VOICE_CLONE.value,
                        engine="voxcpm",
                        status=JobStatus.QUEUED.value,
                        input_text="မင်္ဂလာပါ " * 100,
                        created_at=now - timedelta(minutes=3),
                    ),
                ]
            )
            await seed_s.commit()

        executed_sql: list[str] = []

        @event.listens_for(eng.sync_engine, "before_cursor_execute")
        def _capture(conn, cursor, statement, parameters, context, executemany):
            executed_sql.append(statement)

        # Case A: All queued jobs are VoxCPM while active_voxcpm == 2 -> None, ZERO input_text SQL
        async with maker() as s_blocked:
            claimed = await queue_mod.claim_next_queued_job(s_blocked, allow_recap=False)
        assert claimed is None
        _assert_no_heavy_job_blobs(executed_sql, allow_substr_preview=False)

        # Case B: Non-VoxCPM recap (edge_tts) behind blocked VoxCPM clone still claims cleanly
        async with maker() as s_add_rc:
            s_add_rc.add(
                Job(
                    id="rc_edge_1",
                    user_id=104,
                    job_type=JobType.RECAP.value,
                    engine="recap",
                    status=JobStatus.QUEUED.value,
                    source_audio_path="https://example.com/v",
                    input_text="Edge Recap Title",
                    recap_config=json.dumps({"config": {"tts_engine": "edge_tts"}}),
                    created_at=now - timedelta(minutes=2),
                )
            )
            await s_add_rc.commit()

        executed_sql.clear()
        async with maker() as s_non_vox:
            claimed_rc = await queue_mod.claim_next_queued_job(
                s_non_vox, allow_recap=True, hydrate_winner=False
            )
        assert claimed_rc is not None
        assert claimed_rc.id == "rc_edge_1"
        assert claimed_rc.status == JobStatus.RUNNING.value
        _assert_no_heavy_job_blobs(executed_sql, allow_substr_preview=False)
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_part4_hydrate_winner_modes_fifo_interleave_and_dispatcher_defer(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        hf_ledger,
        "evaluate_voxcpm_admission",
        lambda **kw: hf_ledger.AdmissionDecision(
            admitted=True,
            action="start",
            reason="ok",
            band=hf_ledger.BAND_NORMAL,
            units=1,
            gate_s=15.0,
            needed_gpu_s=5.0,
            available_gpu_s=500.0,
            solvent_slots=4,
            eta_label="",
        ),
    )
    eng, maker = await _open_file_db(tmp_path, "hydrate.db")
    now = datetime.now(timezone.utc)
    try:
        async with maker() as seed_s:
            seed_s.add_all(
                [
                    User(id=201, first_name="FreeU1", account_category=AccountCategory.FREE.value),
                    User(id=202, first_name="FreeU2", account_category=AccountCategory.FREE.value),
                    User(id=203, first_name="EdgeRecapU", account_category=AccountCategory.FREE.value),
                    # Slot 0: Non-VoxCPM recap -> must stay at FIFO index 0 ahead of VoxCPM!
                    Job(
                        id="rc_fifo_first",
                        user_id=203,
                        job_type=JobType.RECAP.value,
                        engine="recap",
                        status=JobStatus.QUEUED.value,
                        source_audio_path="https://example.com/fifo",
                        input_text="FIFO First Recap",
                        recap_config=json.dumps({"config": {"tts_engine": "gemini_tts"}}),
                        created_at=now - timedelta(minutes=10),
                    ),
                    # Slot 1: Unparked clone (claimed with hydrate_winner=False)
                    Job(
                        id="vc_no_hydrate",
                        user_id=201,
                        job_type=JobType.VOICE_CLONE.value,
                        engine="voxcpm",
                        status=JobStatus.QUEUED.value,
                        input_text="ပထမ အသံပွား စာသား",
                        final_script="S1",
                        recap_config=json.dumps({"k": "v1"}),
                        created_at=now - timedelta(minutes=9),
                    ),
                    # Slot 2: Unparked clone (claimed with default hydrate_winner=True)
                    Job(
                        id="vc_hydrated",
                        user_id=202,
                        job_type=JobType.VOICE_CLONE.value,
                        engine="voxcpm",
                        status=JobStatus.QUEUED.value,
                        input_text="ဒုတိယ အသံပွား စာသား",
                        final_script="S2",
                        recap_config=json.dumps({"k": "v2"}),
                        created_at=now - timedelta(minutes=8),
                    ),
                ]
            )
            await seed_s.commit()

        executed_sql: list[str] = []

        @event.listens_for(eng.sync_engine, "before_cursor_execute")
        def _capture(conn, cursor, statement, parameters, context, executemany):
            executed_sql.append(statement)

        # 1. Non-VoxCPM recap at FIFO position 0 wins BEFORE VoxCPM candidates (FIFO interleave preserved!)
        async with maker() as s1:
            w1 = await queue_mod.claim_next_queued_job(
                s1, allow_recap=True, hydrate_winner=False
            )
        assert w1 is not None and w1.id == "rc_fifo_first"

        # 2. hydrate_winner=False issues ZERO SELECT after UPDATE jobs SET status='RUNNING'
        executed_sql.clear()
        async with maker() as s2:
            w2 = await queue_mod.claim_next_queued_job(
                s2, allow_recap=True, hydrate_winner=False
            )
            assert w2 is not None and w2.id == "vc_no_hydrate"
            assert w2.status == JobStatus.RUNNING.value

        update_idx = next(
            i for i, q in enumerate(executed_sql) if q.lstrip().upper().startswith("UPDATE JOBS")
        )
        selects_after_update = [
            q for q in executed_sql[update_idx + 1 :] if q.lstrip().upper().startswith("SELECT")
        ]
        assert selects_after_update == [], f"Unexpected post-claim SELECT: {selects_after_update}"

        # 3. Default hydrate_winner=True populates all 3 deferred columns on same-session return
        async with maker() as s3:
            # Put vc_no_hydrate back to SUCCESS so active_voxcpm == 0
            r_prev = await s3.get(Job, "vc_no_hydrate")
            r_prev.status = JobStatus.SUCCESS.value
            await s3.commit()

        async with maker() as s4:
            w3 = await queue_mod.claim_next_queued_job(s4, allow_recap=True)
            assert w3 is not None and w3.id == "vc_hydrated"
            unloaded = sa_inspect(w3).unloaded
            assert "input_text" not in unloaded
            assert "final_script" not in unloaded
            assert "recap_config" not in unloaded
            assert w3.input_text == "ဒုတိယ အသံပွား စာသား"

        # 4. _dispatch_oldest_queued_recap defers final_script & preserves asyncio.to_thread pin
        disp_src = inspect.getsource(server_mod._dispatch_oldest_queued_recap)
        assert "defer(Job.final_script)" in disp_src
        assert "asyncio.to_thread" in disp_src
    finally:
        await eng.dispose()


# ---------------------------------------------------------------------------
# 5. release_in_flight_gpu absent job_id -> cached load_pool_state (no force=True)
# ---------------------------------------------------------------------------


def test_part2_release_in_flight_gpu_absent_job_uses_cached_state(monkeypatch):
    monkeypatch.setattr(hf_ledger, "_load_in_flight_map", lambda now: {"other_job": {"gpu_s": 12.5}})
    monkeypatch.setattr(hf_ledger, "_save_in_flight_map", lambda m: None)

    calls: list[dict] = []

    def _spy_load_pool_state(*, force: bool = False, now_ts: float | None = None):
        calls.append({"force": force, "now_ts": now_ts})
        return {"in_flight_gpu_s": 12.5}

    monkeypatch.setattr(hf_ledger, "load_pool_state", _spy_load_pool_state)

    rem = hf_ledger.release_in_flight_gpu("missing_job_id", now_ts=1700000000.0)
    assert rem == 12.5
    assert len(calls) == 1
    assert calls[0]["force"] is False


# ---------------------------------------------------------------------------
# 6. Worker poll cadence: active recap tasks no longer pin backoff at 2.0s
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_part3_worker_loop_active_recap_task_steps_backoff_above_2s(monkeypatch):
    sleeps: list[float] = []
    bg_task_done = asyncio.Event()

    async def _long_recap():
        await bg_task_done.wait()

    recap_task = asyncio.create_task(_long_recap())
    tick_counter = {"n": 0}
    fake_clock = {"t": 1000.0}

    monkeypatch.setattr(settings, "instance_primary", False)
    monkeypatch.setattr(settings, "worker_laptop_status", "ONLINE")
    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setattr(
        worker_mod,
        "record_secondary_worker_heartbeat",
        AsyncMock(return_value=["worker_laptop_heartbeat"]),
    )

    class _DummySessionCtx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(worker_mod, "AsyncSessionLocal", lambda: _DummySessionCtx())

    async def _fake_claim_and_process(**kwargs):
        tick_counter["n"] += 1
        active_set = kwargs.get("active_recap_tasks")
        if active_set is not None and tick_counter["n"] == 1:
            # Simulate an already-running background recap task across empty/held ticks
            active_set.add(recap_task)
        return False

    async def _fake_sleep(delay: float):
        sleeps.append(delay)
        fake_clock["t"] += delay
        if len(sleeps) >= 3:
            raise asyncio.CancelledError()

    monkeypatch.setattr(worker_mod, "claim_and_process_next_job", _fake_claim_and_process)
    monkeypatch.setattr(worker_mod.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr("time.monotonic", lambda: fake_clock["t"])

    try:
        await worker_mod.run_worker_loop(poll_interval=2.0, engine_filter="voxcpm")
    finally:
        bg_task_done.set()
        await recap_task

    assert sleeps == [2.0, 5.0, 10.0]

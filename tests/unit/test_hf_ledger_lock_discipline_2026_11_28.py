"""Unit tests for the 2026-11-28 HF Ledger Lock & Transaction Discipline Fix.

Covers:
1. Deterministic consultation reproduction: `record_success_debit(..., attempt_started_ts=...)`
   against a real `_sync_engine()` (no monkeypatched store functions) completes < 1.5 s
   instead of self-deadlocking forever on `_SYNC_STORE_LOCK`.
2. Real-engine (`_sync_engine()`) debit + mid-flight causality-skip (`truth_skip_debits`)
   tests for `record_success_debit`, `record_failure_debit`, `record_calibration_drift_stats`,
   and converted `_mutate_pool_state` writers (< 200 ms each).
3. Bounded PostgreSQL `55P03` / `40P01` retry with lock release between sleeps + single
   summary warning on final failure + `_mem_state` preservation (never wiped to empty).
4. `_IN_MUTATE` pure-CPU callback guard: nested `botsetting_sync_read` / `write` / `delete` /
   `mutate` inside `botsetting_sync_mutate` are refused with an `ERROR` stack log and no deadlock.
5. Event-loop non-wedge test: while `_SYNC_STORE_LOCK` is held for 1 s in a background thread,
   `/health` and cached `evaluate_voxcpm_admission` on the `asyncio` loop answer in < 100 ms;
   plus async read-transaction rollback hygiene (`_end_clean_readonly_txn`, `process_recap_job`,
   `calibrate_hf_ledger.py` second-signal hard exit) and `diagnostics.py` session tagging &
   report-only `TransactionWatchdog`.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import inspect
import json
import logging
from pathlib import Path
import signal
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import worker as worker_mod
from app.db import diagnostics as diag_mod
import app.db.session as db_session_mod
from app.models.enums import JobStatus, JobType
from app.models.schema import Base, Job, User
from app.services import hf_calibration as hf_cal
from app.services import hf_pool_ledger as hf_ledger
from app.services import key_rotation as kr_mod
from app.services import voice_choice as vc_mod
from app.services import voxcpm_engine as vox_mod
from app.webapp import api as api_mod
from app.webapp import server as server_mod
from scripts import calibrate_hf_ledger as cal_cli


def _create_bot_settings_sqlite(db_file: Path) -> str:
    conn = sqlite3.connect(str(db_file))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS bot_settings ("
            "key VARCHAR(128) PRIMARY KEY, "
            "value TEXT, "
            "updated_at TIMESTAMP)"
        )
        conn.commit()
    finally:
        conn.close()
    return f"sqlite:///{db_file.as_posix()}"


@pytest.fixture
def real_sync_store(tmp_path):
    """Configure `key_rotation` against a real SQLite engine with ZERO store monkeypatches."""
    db_url = _create_bot_settings_sqlite(tmp_path / "ledger_lock_discipline.db")
    kr_mod.configure_sync_store(db_url)
    hf_ledger.reset_ledger_for_tests(clear_store=True)
    vox_mod.VoxCPMEngine._token_cooldowns.clear()
    vox_mod.VoxCPMEngine._token_balances.clear()
    vox_mod.VoxCPMEngine._space_admission_prices.clear()
    try:
        assert kr_mod.botsetting_sync_read is kr_mod._ORIG_BOTSETTING_SYNC_READ
        assert kr_mod.botsetting_sync_write is kr_mod._ORIG_BOTSETTING_SYNC_WRITE
        assert kr_mod._sync_engine() is not None
        yield db_url
    finally:
        hf_ledger.reset_ledger_for_tests(clear_store=True)
        kr_mod.configure_sync_store(None)


def test_repro_self_deadlock_resolved_on_real_sync_engine(real_sync_store):
    """Consultation deterministic repro: hangs forever on HEAD 457e97f; completes < 1.5 s now."""
    sp = "awints001/voxcpm2-a"
    tok = "hf_repro_deadlock_tok"
    t0 = 1_795_812_800.0
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok): {
                    "status": "ok",
                    "remaining_s": 300.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": datetime.fromtimestamp(t0, tz=timezone.utc).isoformat(),
                }
            },
            "space_gates": {sp: 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=t0,
    )

    done = threading.Event()
    err_box: list[BaseException] = []

    def _writer() -> None:
        try:
            hf_ledger.record_success_debit(
                sp,
                tok,
                debit_s=15.0,
                attempt_started_ts=t0 + 1.0,
                now_ts=t0 + 5.0,
            )
        except BaseException as exc:  # pragma: no cover
            err_box.append(exc)
        finally:
            done.set()

    started = time.perf_counter()
    worker_thread = threading.Thread(target=_writer, name="repro-debit-writer", daemon=True)
    worker_thread.start()
    finished = done.wait(timeout=1.5)
    elapsed_s = time.perf_counter() - started

    assert finished is True, "SELF-DEADLOCK: record_success_debit hung holding _SYNC_STORE_LOCK"
    assert not err_box
    assert elapsed_s < 1.5
    assert hf_ledger.get_token_remaining_s(tok, now_ts=t0 + 6.0) == pytest.approx(285.0)
    # Verify persisted directly in SQLite (not only in memory)
    hf_ledger.reset_ledger_for_tests(clear_store=False)
    assert hf_ledger.get_token_remaining_s(tok, now_ts=t0 + 6.0) == pytest.approx(285.0)


def test_real_engine_debits_causality_skip_and_calibration_drift_under_200ms(real_sync_store):
    """Verify real-engine debits, mid-flight causality skips, drift stats, and converted writers."""
    sp = "yunaisbusy/voxcpm2-9"
    tok = "hf_real_eng_causality_tok"
    t0 = 1_795_820_000.0

    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok): {
                    "status": "ok",
                    "remaining_s": 240.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": datetime.fromtimestamp(t0, tz=timezone.utc).isoformat(),
                }
            },
            "space_gates": {sp: 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=t0,
    )

    # 1. Normal success debit on real engine (< 200 ms)
    t_s1 = time.perf_counter()
    skipped_1: list[bool] = []
    hf_ledger.record_success_debit(
        sp,
        tok,
        debit_s=20.0,
        attempt_started_ts=t0 + 10.0,
        skipped_out=skipped_1,
        now_ts=t0 + 15.0,
    )
    assert (time.perf_counter() - t_s1) < 0.200
    assert skipped_1 == []
    assert hf_ledger.get_token_remaining_s(tok, now_ts=t0 + 15.0) == pytest.approx(220.0)

    # 2. Mid-flight revealed truth (`left_s = 135.0` at `t0 + 25.0`) skips BOTH success & failure
    #    debits whose `attempt_started_ts <= t0 + 25.0`, even after clearing in-memory truth cache!
    hf_ledger.record_admission_refusal(
        sp,
        tok,
        137.0,
        135.0,
        update_space_gate=False,
        session_mode=True,
        session_chars=849,
        now_ts=t0 + 25.0,
    )
    # Wipe in-memory `_mem_truth_ts` to prove `aux_keys=(TRUTH_TS_STORE_KEY,)` reloads truth on same DB conn!
    hf_ledger._mem_truth_ts.clear()

    eng = vox_mod.VoxCPMEngine(work_dir=Path("."), spaces=[sp], hf_tokens=[tok])
    t_s2 = time.perf_counter()
    was_skipped = vox_mod.VoxCPMEngine._record_success_balance(
        sp,
        tok,
        debit_s=30.0,
        attempt_started_ts=t0 + 20.0,  # started BEFORE truth_ts (t0 + 25.0) -> MUST skip!
    )
    assert (time.perf_counter() - t_s2) < 0.200
    assert was_skipped is True
    eng.job_memory.record_truth_skip_debit(1)
    assert eng.truth_skip_debits == 1
    assert hf_ledger.get_token_remaining_s(tok, now_ts=t0 + 30.0) == pytest.approx(135.0)

    hf_ledger._mem_truth_ts.clear()
    t_f1 = time.perf_counter()
    skipped_fail: list[bool] = []
    hf_ledger.record_failure_debit(
        sp,
        tok,
        18.0,
        attempt_started_ts=t0 + 25.0,  # `truth_ts >= attempt_started_ts` -> MUST skip!
        skipped_out=skipped_fail,
        now_ts=t0 + 32.0,
    )
    assert (time.perf_counter() - t_f1) < 0.200
    assert skipped_fail == [True]
    assert hf_ledger.get_token_remaining_s(tok, now_ts=t0 + 32.0) == pytest.approx(135.0)

    # Attempt starting AFTER `truth_ts` (`t0 + 40.0 > t0 + 25.0`) debits normally (< 200 ms)
    t_f2 = time.perf_counter()
    skipped_fail_after: list[bool] = []
    hf_ledger.record_failure_debit(
        sp,
        tok,
        15.0,
        attempt_started_ts=t0 + 40.0,
        skipped_out=skipped_fail_after,
        now_ts=t0 + 45.0,
    )
    assert (time.perf_counter() - t_f2) < 0.200
    assert skipped_fail_after == []
    assert hf_ledger.get_token_remaining_s(tok, now_ts=t0 + 45.0) == pytest.approx(120.0)

    # 3. `record_calibration_drift_stats` on real engine (< 200 ms, merges `aj_hf:drift_window`)
    hf_ledger._mem_drift_window.clear()
    t_cal = time.perf_counter()
    ds_st = hf_ledger.record_calibration_drift_stats(42.0, 1, 42.0, now_ts=t0 + 50.0)
    assert (time.perf_counter() - t_cal) < 0.200
    # Earlier `record_admission_refusal` observed `220.0 - 135.0 = +85.0s` (> 25s) in `aj_hf:drift_window`!
    assert ds_st["drift_stats"]["positive_drift_s"] == pytest.approx(85.0)
    assert ds_st["drift_stats"]["overestimated_tokens"] == 1
    assert ds_st["drift_stats"]["max_token_drift_s"] == pytest.approx(85.0)

    # 4. Converted `_mutate_pool_state` writers + calibration truth-stamp ordering work cleanly on real engine
    assert hf_ledger.acquire_in_flight_gpu("job_lock_1", 55.0, now_ts=t0 + 55.0) == pytest.approx(55.0)
    assert hf_ledger.release_in_flight_gpu("job_lock_1", now_ts=t0 + 56.0) == pytest.approx(0.0)
    hf_ledger.record_session_space_floor(sp, 75.0, 40, now_ts=t0 + 57.0)
    assert hf_ledger.get_session_space_floor(sp, now_ts=t0 + 57.0) == pytest.approx(75.0)
    hf_ledger.record_space_timeout(sp, now_ts=t0 + 58.0)
    assert hf_ledger.is_space_cooling(sp, now_ts=t0 + 59.0) is True
    _b, after_p, _deb, _ = hf_ledger.apply_calibration_probe_success(tok, 1.0, now_ts=t0 + 60.0)
    assert after_p == pytest.approx(295.0)
    assert hf_ledger.get_token_meta(tok, reload_store=True, now_ts=t0 + 60.0)["verified_rem_s"] == pytest.approx(295.0)


def test_lock_timeout_55p03_bounded_retry_and_fallback_preserves_mem_state(monkeypatch, caplog):
    """Simulated PostgreSQL `55P03` retries up to 3x, releases lock while sleeping, logs 1 warning,
    and preserves `_mem_state` on fallback without deadlocking."""
    tok = "hf_lock_timeout_tok"
    sp = "awints001/voxcpm2-b"
    t0 = 1_795_830_000.0

    # Seed valid in-memory state first (with store temporarily disabled)
    kr_mod.configure_sync_store(None)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    hf_ledger.reset_ledger_for_tests(clear_store=False)
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok): {
                    "status": "ok",
                    "remaining_s": 180.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": datetime.fromtimestamp(t0, tz=timezone.utc).isoformat(),
                }
            },
            "space_gates": {sp: 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=t0,
    )

    class LockNotAvailable(Exception):
        pgcode = "55P03"

    attempts = {"n": 0}
    sleeps: list[tuple[float, bool]] = []

    def _fake_sleep(secs: float) -> None:
        # Check whether another thread can acquire `_SYNC_STORE_LOCK` while we sleep!
        probe_acquired: list[bool] = []

        def _probe() -> None:
            got = kr_mod._SYNC_STORE_LOCK.acquire(blocking=False)
            if got:
                kr_mod._SYNC_STORE_LOCK.release()
            probe_acquired.append(got)

        pt = threading.Thread(target=_probe)
        pt.start()
        pt.join(timeout=1.0)
        sleeps.append((secs, bool(probe_acquired and probe_acquired[0])))

    class _AlwaysLockedPgEngine:
        dialect = SimpleNamespace(name="postgresql")

        @contextmanager
        def begin(self):
            attempts["n"] += 1
            raise LockNotAvailable("canceling statement due to lock timeout")
            yield  # pragma: no cover

    monkeypatch.setattr(kr_mod, "_sync_engine", lambda: _AlwaysLockedPgEngine())
    monkeypatch.setattr(kr_mod.time, "sleep", _fake_sleep)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.services.key_rotation"):
        updated = hf_ledger.record_success_debit(
            sp,
            tok,
            debit_s=25.0,
            attempt_started_ts=t0 + 1.0,
            now_ts=t0 + 5.0,
        )

    # 1 initial + 2 retries = 3 total attempts; lock was FREE during both backoff sleeps!
    assert attempts["n"] == 3
    assert len(sleeps) == 2
    assert 0.15 <= sleeps[0][0] <= 0.40 and sleeps[0][1] is True
    assert 0.45 <= sleeps[1][0] <= 0.95 and sleeps[1][1] is True

    # Exactly ONE summary warning emitted on final failure
    warns = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1
    assert "sync store mutate failed" in warns[0]

    # `_mem_state` was NOT wiped to empty: existing token survived with in-memory fallback debit applied!
    assert updated is not None
    assert hf_ledger.token_digest(tok) in updated["tokens"]
    assert hf_ledger.get_token_remaining_s(tok, now_ts=t0 + 5.0) == pytest.approx(155.0)

    # Transient `55P03` recovering on 2nd attempt succeeds with 0 warnings!
    flaky_attempts = {"n": 0}

    class _FlakyPgEngine:
        dialect = SimpleNamespace(name="postgresql")

        @contextmanager
        def begin(self):
            flaky_attempts["n"] += 1
            if flaky_attempts["n"] == 1:
                raise LockNotAvailable("transient lock contention")
            yield SimpleNamespace(
                execute=lambda *_a, **_k: SimpleNamespace(first=lambda: ('{"v": 1}',))
            )

    monkeypatch.setattr(kr_mod, "_sync_engine", lambda: _FlakyPgEngine())
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.services.key_rotation"):
        ok = kr_mod.botsetting_sync_mutate("aj_hf:pool_state", lambda _cur: '{"v": 2}')
    assert ok is True
    assert flaky_attempts["n"] == 2
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_in_mutate_guard_refuses_nested_store_calls_loudly(real_sync_store, caplog):
    """Any mutator attempting a nested `botsetting_sync_*` call is refused with ERROR + stack trace."""
    kr_mod.botsetting_sync_write("aj_test:outer", "init")
    kr_mod.botsetting_sync_write("aj_test:other", "secret_ free_val")

    nested_outcomes: dict[str, object] = {}

    def _bad_mutator(cur: str | None) -> str | None:
        nested_outcomes["read"] = kr_mod.botsetting_sync_read("aj_test:other")
        nested_outcomes["write"] = kr_mod.botsetting_sync_write("aj_test:other", "bad")
        nested_outcomes["delete"] = kr_mod.botsetting_sync_delete("aj_test:other")
        nested_outcomes["mutate"] = kr_mod.botsetting_sync_mutate("aj_test:other", lambda _x: "bad2")
        return f"{cur}:updated"

    caplog.clear()
    with caplog.at_level(logging.ERROR, logger="app.services.key_rotation"):
        ok = kr_mod.botsetting_sync_mutate("aj_test:outer", _bad_mutator)

    assert ok is True
    assert nested_outcomes == {
        "read": None,
        "write": False,
        "delete": False,
        "mutate": False,
    }
    err_msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(err_msgs) == 4
    assert all("illegal nested botsetting_sync_" in m and "_bad_mutator" in m for m in err_msgs)
    # Outer mutation succeeded cleanly and `aj_test:other` was untouched!
    assert kr_mod.botsetting_sync_read("aj_test:outer") == "init:updated"
    assert kr_mod.botsetting_sync_read("aj_test:other") == "secret_ free_val"


@pytest.mark.asyncio
async def test_event_loop_unwedged_while_sync_store_lock_held_1s(tmp_path, monkeypatch):
    """Hold `_SYNC_STORE_LOCK` for 1.0 s in a background thread; `/health` and cached admission
    read on the `asyncio` loop must still answer in < 100 ms."""
    db_file = tmp_path / "health_loop.db"
    async_eng = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}", future=True)
    async with async_eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=async_eng, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_session_mod, "AsyncSessionLocal", maker)

    # Warm `hf_pool_ledger` in-memory snapshot within 30 s TTL
    t0 = time.time()
    kr_mod.configure_sync_store(None)
    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest("hf_warm_loop_tok"): {
                    "status": "ok",
                    "remaining_s": 300.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": datetime.fromtimestamp(t0, tz=timezone.utc).isoformat(),
                }
            },
            "space_gates": {"awints001/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=t0,
    )

    locked_evt = threading.Event()
    release_evt = threading.Event()

    def _hold_store_lock() -> None:
        with kr_mod._SYNC_STORE_LOCK:
            locked_evt.set()
            release_evt.wait(timeout=1.0)

    holder = threading.Thread(target=_hold_store_lock, name="lock-holder-1s", daemon=True)
    holder.start()
    try:
        assert locked_evt.wait(timeout=1.0)

        t_start = time.perf_counter()
        health_resp = await server_mod.health_check()
        adm = hf_ledger.evaluate_voxcpm_admission(
            kind="clone",
            is_paid=True,
            units=1,
            est_audio_seconds=10.0,
        )
        elapsed_s = time.perf_counter() - t_start

        assert health_resp.status_code == 200
        assert adm.admitted is True
        assert elapsed_s < 0.100, f"Event loop blocked for {elapsed_s * 1000:.1f} ms (>= 100 ms)"
    finally:
        release_evt.set()
        holder.join(timeout=2.0)
        await async_eng.dispose()

    # Also pin `asyncio.to_thread` wrappers at all required async call sites
    assert "asyncio.to_thread" in inspect.getsource(server_mod._dispatch_oldest_queued_recap)
    assert "asyncio.to_thread" in inspect.getsource(vc_mod._sweep_one)
    assert "asyncio.to_thread" in inspect.getsource(hf_cal.check_calibration_guards)
    assert "asyncio.to_thread" in inspect.getsource(api_mod.get_voxcpm_pool_status)


@pytest.mark.asyncio
async def test_readonly_txn_hygiene_and_worker_recap_commit(tmp_path, monkeypatch):
    """Verify `_end_clean_readonly_txn` preserves scalar attributes while ending `in_transaction()`,
    and `process_recap_job` commits before `_execute_recap_background_job`."""
    db_file = tmp_path / "hygiene_check.db"
    async_eng = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}", future=True)
    async with async_eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=async_eng, class_=AsyncSession, expire_on_commit=False)

    try:
        async with maker() as s:
            s.add(User(id=88001, username="hygiene_u", language_code="my", account_category="PURCHASED"))
            s.add(
                Job(
                    id="rc_1795899999_hyg001",
                    user_id=88001,
                    status=JobStatus.RUNNING.value,
                    job_type=JobType.RECAP.value,
                    engine="recap",
                    recap_config=json.dumps({"config": {"url": "https://example.com/v", "tts_engine": "edge"}}),
                )
            )
            await s.commit()

        # 1. `_end_clean_readonly_txn` ends `in_transaction()` without expiring loaded `User` attributes
        #    AND keeps `u` persistent in `s` for later `refresh()` / mutations.
        async with maker() as s:
            u = await s.get(User, 88001)
            assert s.in_transaction() is True
            await api_mod._end_clean_readonly_txn(s)
            assert s.in_transaction() is False
            assert u.id == 88001
            assert u.language_code == "my"
            assert u.account_category == "PURCHASED"
            await s.refresh(u)
            u.language_code = "en"
            await s.commit()
        async with maker() as s_verify:
            u_ver = await s_verify.get(User, 88001)
            assert u_ver.language_code == "en"

        # 2. `process_recap_job` commits & releases read transaction BEFORE `_execute_recap_background_job`
        tx_during_exec: list[bool] = []

        async def _fake_exec(jid, *_a, **_kw):
            tx_during_exec.append(recap_sess.in_transaction())
            async with maker() as done_s:
                j_row = await done_s.get(Job, jid)
                j_row.status = JobStatus.SUCCESS.value
                await done_s.commit()

        monkeypatch.setattr(api_mod, "_execute_recap_background_job", _fake_exec)
        async with maker() as recap_sess:
            job_obj = await recap_sess.get(Job, "rc_1795899999_hyg001")
            ok = await worker_mod.process_recap_job(recap_sess, job_obj, bot=None)
        assert ok is True
        assert tx_during_exec == [False]
    finally:
        await async_eng.dispose()


def test_calibrate_cli_second_signal_hard_exits(monkeypatch):
    """Verify `calibrate_hf_ledger.py` sets graceful flag on 1st signal and hard-exits on 2nd signal."""
    hard_exits: list[int] = []
    registered_handlers: dict[int, object] = {}

    def _fake_signal(sig, handler):
        prev = registered_handlers.get(sig, signal.default_int_handler)
        registered_handlers[sig] = handler
        return prev

    def _fake_getsignal(sig):
        return registered_handlers.get(sig, signal.default_int_handler)

    async def _fake_manual_once(**kwargs):
        shut_fn = kwargs["is_shutting_down_fn"]
        assert shut_fn() is False
        handler = registered_handlers[signal.SIGINT]
        handler(signal.SIGINT, None)  # 1st signal -> graceful flag only
        assert shut_fn() is True
        assert hard_exits == []
        handler(signal.SIGINT, None)  # 2nd signal -> hard exit!
        assert hard_exits == [128 + int(signal.SIGINT)]
        return {"skipped": True, "reason": "shutting_down"}

    monkeypatch.setattr(cal_cli.signal, "signal", _fake_signal)
    monkeypatch.setattr(cal_cli.signal, "getsignal", _fake_getsignal)
    monkeypatch.setattr(hf_cal, "run_manual_calibration_once", _fake_manual_once)
    rc = cal_cli.main(["--once"], tokens=["hf_dummy"], hard_exit_fn=hard_exits.append, out_fn=lambda _s: None)
    assert rc == 0
    assert hard_exits == [128 + int(signal.SIGINT)]


def test_diagnostics_app_name_pg_connect_listener_and_watchdog(tmp_path, caplog):
    """Verify `<role>:<engine>` naming, PG connect SET listener, and 10s/30s TransactionWatchdog."""
    assert diag_mod.format_application_name("async", argv=["main.py", "--mode", "web_and_bot"], environ={}) == "vcp-web:async"
    assert diag_mod.format_application_name("sync", argv=["main.py"], environ={"RUN_MODE": "worker"}) == "vcp-worker:sync"
    assert (
        diag_mod.format_application_name("sync", argv=["scripts/calibrate_hf_ledger.py", "--once"], environ={})
        == "vcp-script:calibrate_hf_ledger:sync"
    )
    assert diag_mod.format_application_name("async", argv=["main.py"], environ={"VCP_APP_NAME": "custom-canary"}) == "custom-canary:async"

    # PG connect listener executes expected SET statements + commit on simulated Postgres target
    executed_sets: list[str] = []
    committed = {"n": 0}

    class _FakeCursor:
        def execute(self, sql: str) -> None:
            executed_sets.append(sql)

        def close(self) -> None:
            pass

    class _FakeDbapiConn:
        def cursor(self):
            return _FakeCursor()

        def commit(self) -> None:
            committed["n"] += 1

    listeners: dict[str, object] = {}

    class _FakePgTarget:
        dialect = SimpleNamespace(name="postgresql")
        dispatch = object()

    orig_listen = None
    import sqlalchemy.event as sa_event

    orig_listen = sa_event.listen
    try:
        sa_event.listen = lambda _t, ev, fn: listeners.__setitem__(ev, fn)
        installed = diag_mod.install_postgres_session_listener(
            _FakePgTarget(),
            engine_kind="sync",
            lock_timeout_ms=3000,
            statement_timeout_ms=8000,
            idle_in_tx_timeout_ms=15000,
        )
    finally:
        sa_event.listen = orig_listen

    assert installed is True
    listeners["connect"](_FakeDbapiConn(), None)
    assert any(s.startswith("SET application_name = '") and s.endswith(":sync'") for s in executed_sets)
    assert "SET lock_timeout = '3000ms'" in executed_sets
    assert "SET statement_timeout = '8000ms'" in executed_sets
    assert "SET idle_in_transaction_session_timeout = '15000ms'" in executed_sets
    assert committed["n"] == 1

    # Watchdog emits WARNING at >=10s and ERROR + stack at >=30s once per threshold, redacting literals!
    db_file = tmp_path / "watchdog.db"
    eng = create_engine(f"sqlite:///{db_file.as_posix()}")
    wd = diag_mod.TransactionWatchdog(interval_s=60.0, warn_age_s=10.0, error_age_s=30.0)
    assert diag_mod.register_engine_watchdog(eng, engine_kind="sync", watchdog=wd) is True

    try:
        with eng.begin() as conn:
            conn.execute(text("SELECT 'hf_SUPER_SECRET_LITERAL_999'"))
            assert wd.active_count() == 1
            t_base = time.monotonic()

            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="app.db.diagnostics"):
                ev_5s = wd.inspect_once(now_mono=t_base + 5.0)
                assert ev_5s == []

                ev_12s = wd.inspect_once(now_mono=t_base + 12.0)
                assert len(ev_12s) == 1 and ev_12s[0]["level"] == "WARNING"
                # Deduplicated at 15s (still < 30s)
                assert wd.inspect_once(now_mono=t_base + 15.0) == []

                ev_35s = wd.inspect_once(now_mono=t_base + 35.0)
                assert len(ev_35s) == 1 and ev_35s[0]["level"] == "ERROR"
                assert "test_diagnostics_app_name_pg_connect_listener_and_watchdog" in ev_35s[0]["stack"]
                # Deduplicated after 30s
                assert wd.inspect_once(now_mono=t_base + 45.0) == []

            combined_logs = "\n".join(r.getMessage() for r in caplog.records)
            assert "hf_SUPER_SECRET_LITERAL_999" not in combined_logs
            assert "'?'" in combined_logs

        # Transaction exit clears watchdog active map immediately
        assert wd.active_count() == 0
    finally:
        eng.dispose()

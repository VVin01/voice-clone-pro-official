"""Unit tests for HF Ledger Truth — Round 2 (Method 3 Daily Calibration Sweep, 2026-11-27).

Covers Parts 1–7:
1. Quota-403 circuit breaker (`key_health._probe_hf_account`): 24 h `quota_scope_unsupported`
   marking in memory + `bn_key_health_v1` (never in `aj_hf:pool_state`), skipping
   `_zero_gpu_quota` while keeping `whoami-v2` liveness, counting `quota_probe_errors`
   only for tokens actually attempted that cycle, and preserving `drift_stats` balance fields.
2. Uncertainty classifier (`hf_pool_ledger.classify_token_uncertainty` / `classify_pool_uncertainty`):
   rules 1–5 (`stale_drained`, `pro_lapse`, `unverified_taint`, `aged_partial`, `never_verified`),
   `auth_dead` exclusion, and `>= 295 s` untouched CERTAIN fast-skip.
3. Sweep executor (`hf_calibration.run_calibration_sweep`): probe Space selection, all 3
   outcomes (`numeric_refusal`, `probe_success`, `generic_refusal` + `<= 2` 1-char
   `/generate_session` follow-ups per sweep for `existing_rem >= 45.0`), priority order,
   and hard caps (`SWEEP_MAX_GPU_S = 60.0`, `120 s` wall-clock, `2` consecutive transport errors).
4. Scheduler + guards: `instance_primary`, `04:15–06:00 Asia/Yangon`, `last_sweep_date_mmt`
   dedupe, `hf_daily_calibration_enabled` kill switch (default `False`), active/queued VoxCPM
   job guards, `in_flight_gpu_s == 0`, `boot_age >= 600 s`, `maintenance_mode`, and shutdown.
5. Drift report (`aj_hf:calibration_report`), 7-day rolling history, `hf_pool_calibration_sweep_report`
   INFO log, Admin Key Health overlay, and strict ZERO-LEAK pin.
6. Manual CLI runner (`scripts/calibrate_hf_ledger.py`): `--dry-run` (0 probe calls) and `--once`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from types import SimpleNamespace
import wave
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.models.enums import JobStatus, JobType
from app.models.schema import Base, Job, User
from app.services import dynamic_settings as ds_mod
from app.services import hf_calibration as hf_cal
from app.services import hf_pool_ledger as hf_ledger
from app.services import key_health as kh_mod
from app.services import key_rotation as kr_mod
from app.services import voxcpm_engine as vox_mod
from scripts import calibrate_hf_ledger as cal_cli


def _make_wav(path: Path, *, duration_s: float = 1.5, sr: int = 24000) -> Path:
    n = max(1, int(round(duration_s * sr)))
    ts = np.arange(n, dtype=np.float64) / sr
    pcm = (0.4 * np.sin(2.0 * np.pi * 180.0 * ts) * 16000.0).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())
    return path


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


@pytest.fixture(autouse=True)
def _isolated_round2_store(monkeypatch):
    kv: dict[str, str] = {}

    def _read(k: str):
        return kv.get(str(k))

    def _write(k: str, v: str):
        kv[str(k)] = str(v)
        return True

    def _delete(k: str):
        kv.pop(str(k), None)
        return True

    for mod in (kr_mod, hf_ledger, hf_cal):
        monkeypatch.setattr(mod, "botsetting_sync_read", _read)
        monkeypatch.setattr(mod, "botsetting_sync_write", _write)
        monkeypatch.setattr(mod, "botsetting_sync_delete", _delete)

    hf_ledger.reset_ledger_for_tests(clear_store=True)
    hf_cal.reset_calibration_for_tests(clear_store=True)
    kh_mod._reset_cache_for_tests()
    vox_mod.VoxCPMEngine._token_cooldowns.clear()
    vox_mod.VoxCPMEngine._token_balances.clear()
    vox_mod.VoxCPMEngine._space_admission_prices.clear()
    vox_mod.VoxCPMEngine._last_admission_price = None
    yield kv
    hf_ledger.reset_ledger_for_tests(clear_store=True)
    hf_cal.reset_calibration_for_tests(clear_store=True)
    kh_mod._reset_cache_for_tests()


async def _open_test_db(tmp_path: Path, name: str = "r2_guards.db"):
    db_file = tmp_path / name
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}", future=True)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=eng, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    return eng, maker


# ── Part 1: Quota-403 Circuit Breaker & `drift_stats` ────────────────────────


def test_part1_quota_403_circuit_breaker_24h_and_drift_stats(monkeypatch, _isolated_round2_store):
    kv = _isolated_round2_store
    tok_403 = "hf_SECRET_SCOPE403_TOK"
    tok_ok = "hf_SECRET_SCOPEOK_TOK"
    tokens = [tok_403, tok_ok]

    clock = {"t": 1_795_800_000.0}
    monkeypatch.setattr(kh_mod.time, "time", lambda: clock["t"])
    monkeypatch.setattr(hf_ledger.time, "time", lambda: clock["t"])

    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(tok_403): {
                    "status": "ok",
                    "remaining_s": 210.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(clock["t"] - 600),
                },
                hf_ledger.token_digest(tok_ok): {
                    "status": "ok",
                    "remaining_s": 300.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(clock["t"] - 600),
                },
            },
            "space_gates": {"awints001/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=clock["t"],
    )

    whoami_calls: list[str] = []
    quota_calls: list[str] = []

    class FakeHf403Error(Exception):
        def __init__(self):
            super().__init__("403 Client Error: Forbidden for zero-gpu-quota")
            self.response = SimpleNamespace(status_code=403)

    def _fake_http(url: str, headers=None, timeout: float = 10.0):
        if "whoami-v2" in url:
            auth = (headers or {}).get("Authorization", "")
            tok = auth.replace("Bearer ", "")
            whoami_calls.append(tok)
            nm = "acct-403" if tok == tok_403 else "acct-ok"
            return 200, {"name": nm, "type": "user"}
        if "api/spaces/" in url:
            return 200, {"runtime": {"stage": "RUNNING"}}
        return 200, {}

    def _fake_quota(token: str) -> dict:
        quota_calls.append(token)
        if token == tok_403:
            raise FakeHf403Error()
        raise FakeHf403Error()  # both fail 403 on Cycle 1!

    monkeypatch.setattr(kh_mod, "_http_json", _fake_http)
    monkeypatch.setattr(kh_mod, "_zero_gpu_quota", _fake_quota)
    monkeypatch.setattr(kh_mod, "_load_pools", lambda: (tokens, [], [], ["awints001/voxcpm2-a"]))

    # Seed runtime positive drift via numeric refusal (`210.0 -> 130.0`, +80.0s drift > 25s!)
    hf_ledger.record_admission_refusal(
        "awints001/voxcpm2-a",
        tok_403,
        137.0,
        130.0,
        update_space_gate=False,
        session_mode=True,
        session_chars=849,
        now_ts=clock["t"],
    )

    # Cycle 1: both tokens attempt `_zero_gpu_quota` -> fail 403 -> `quota_probe_errors == 2`,
    # marked `quota_scope_unsupported` in `bn_key_health_v1` (NOT `aj_hf:pool_state`!),
    # and `drift_stats` preserves the +80.0s refusal drift!
    rep1 = kh_mod.get_key_health(force_refresh=True)
    assert sorted(quota_calls) == sorted(tokens)
    assert rep1["providers"]["hf"]["quota_probe_errors"] == 2
    assert kh_mod.is_quota_scope_unsupported(tok_403, now_ts=clock["t"] + 10) is True
    assert kh_mod.is_quota_scope_unsupported(tok_ok, now_ts=clock["t"] + 10) is True

    # Verify `aj_hf:pool_state` token rows stay 6-key clean and `bn_key_health_v1` carries no raw tokens
    pool_raw = kv[hf_ledger.POOL_STATE_STORE_KEY]
    kh_raw = kv[kh_mod._STORE_KEY]
    for secret in tokens:
        assert secret not in pool_raw and secret not in kh_raw
    kh_doc = json.loads(kh_raw)
    assert hf_ledger.token_digest(tok_403) in kh_doc["quota_scope_unsupported"]
    pool_doc = json.loads(pool_raw)
    assert set(pool_doc["tokens"][hf_ledger.token_digest(tok_ok)].keys()) == {
        "status",
        "remaining_s",
        "base_s",
        "resets_at",
        "cooldown_until",
        "updated_at",
    }
    ds1 = hf_ledger.get_drift_stats(now_ts=clock["t"])
    assert ds1 == {
        "positive_drift_s": 80.0,
        "overestimated_tokens": 1,
        "max_token_drift_s": 80.0,
        "quota_probe_errors": 2,
    }

    # Cycle 2 (1 hour later, even after in-memory cache reset):
    # `whoami-v2` STILL runs for both tokens, `_zero_gpu_quota` is SKIPPED (0 calls),
    # `quota_probe_errors == 0`, and `drift_stats` keeps its 80.0s balance fields!
    clock["t"] += 3600.0
    kh_mod._reset_cache_for_tests()  # simulates process restart; persisted `bn_key_health_v1` reloads!
    whoami_calls.clear()
    quota_calls.clear()
    rep2 = kh_mod.get_key_health(force_refresh=True)
    assert sorted(whoami_calls) == sorted(tokens)
    assert quota_calls == []  # ZERO `_zero_gpu_quota` calls!
    assert rep2["providers"]["hf"]["quota_probe_errors"] == 0
    assert rep2["providers"]["hf"]["last_drift_s"] == 80.0

    # Cycle 3 (after 24 h TTL expires): circuit breaker re-opens and attempts `_zero_gpu_quota` again!
    clock["t"] += 86400.0
    quota_calls.clear()
    kh_mod.get_key_health(force_refresh=True)
    assert sorted(quota_calls) == sorted(tokens)


# ── Part 2: Uncertainty Classifier Rules 1–5 + Certain Skip ──────────────────


def test_part2_uncertainty_classifier_rules_1_to_5_and_certain_skip():
    # Test BEFORE Dec 1 (2026-11-28T00:00:00Z) and ON/AFTER Dec 1 (2026-12-01T01:00:00Z)
    nov28_ts = datetime(2026, 11, 28, 0, 0, 0, tzinfo=timezone.utc).timestamp()
    dec01_ts = datetime(2026, 12, 1, 1, 0, 0, tzinfo=timezone.utc).timestamp()

    t_dead = "hf_cls_dead"
    t_stale_rem = "hf_cls_stale_rem"
    t_stale_ceil = "hf_cls_stale_ceil"
    t_fresh_drained = "hf_cls_fresh_drained"
    t_pro = "hf_cls_pro"
    t_taint_fail = "hf_cls_taint_fail"
    t_taint_gen = "hf_cls_taint_gen"
    t_aged = "hf_cls_aged"
    t_never = "hf_cls_never"
    t_certain = "hf_cls_certain"

    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(t_dead): {
                    "status": "auth_dead",
                    "remaining_s": 0.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": _iso(dec01_ts + 40000),
                    "updated_at": _iso(nov28_ts - 90000),
                },
                hf_ledger.token_digest(t_stale_rem): {
                    "status": "ok",
                    "remaining_s": 20.0,  # < 45.0 & updated_at >= 6h old -> stale_drained!
                    "base_s": 300.0,
                    "resets_at": _iso(nov28_ts + 10000),
                    "cooldown_until": "",
                    "updated_at": _iso(nov28_ts - 6 * 3600 - 10),
                },
                hf_ledger.token_digest(t_stale_ceil): {
                    "status": "ok",
                    "remaining_s": 120.0,  # active ceiling <= 60 & resets_at <= now -> stale_drained!
                    "base_s": 300.0,
                    "resets_at": _iso(nov28_ts - 5),
                    "cooldown_until": "",
                    "updated_at": _iso(nov28_ts - 600),
                    "refused_gate_s": 60.0,
                    "refused_until": _iso(nov28_ts + 1800),
                },
                hf_ledger.token_digest(t_fresh_drained): {
                    "status": "ok",
                    "remaining_s": 15.0,  # < 45.0, but updated 1h ago & resets_at in future -> NOT stale_drained!
                    "base_s": 300.0,
                    "resets_at": _iso(nov28_ts + 50000),
                    "cooldown_until": "",
                    "updated_at": _iso(nov28_ts - 3600),
                },
                hf_ledger.token_digest(t_pro): {
                    "status": "ok",
                    "remaining_s": 298.0,  # <= 300, base_s = 2400 -> NOT pro_lapse on Nov 28, IS pro_lapse on Dec 1!
                    "base_s": 2400.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(nov28_ts - 3600),
                },
                hf_ledger.token_digest(t_taint_fail): {
                    "status": "ok",
                    "remaining_s": 220.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(nov28_ts - 600),
                },
                hf_ledger.token_digest(t_taint_gen): {
                    "status": "ok",
                    "remaining_s": 180.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(nov28_ts - 600),
                },
                hf_ledger.token_digest(t_aged): {
                    "status": "ok",
                    "remaining_s": 240.0,  # < 300 - 5, truth 19h ago -> aged_partial!
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(nov28_ts - 19 * 3600),
                },
                hf_ledger.token_digest(t_never): {
                    "status": "ok",
                    "remaining_s": 300.0,  # no truth stamp & updated_at > 24h -> never_verified!
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(nov28_ts - 25 * 3600),
                },
                hf_ledger.token_digest(t_certain): {
                    "status": "ok",
                    "remaining_s": 296.0,  # >= 295, verified 22h ago, 0 attempts since -> CERTAIN!
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(nov28_ts - 22 * 3600),
                },
            },
            "space_gates": {"awints001/voxcpm2-a": 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=nov28_ts,
    )

    hf_ledger.record_token_truth_timestamp(t_aged, nov28_ts - 19 * 3600, verified_rem_s=240.0)
    hf_ledger.record_token_truth_timestamp(t_certain, nov28_ts - 22 * 3600, verified_rem_s=296.0)
    hf_ledger.record_token_truth_timestamp(t_pro, nov28_ts - 3600, verified_rem_s=298.0)
    hf_ledger.record_failure_debit("awints001/voxcpm2-a", t_taint_fail, 12.0, now_ts=nov28_ts - 300)
    hf_ledger.record_refusal_ceiling(t_taint_gen, 137.0, now_ts=nov28_ts - 200)

    all_toks = [
        t_dead,
        t_stale_rem,
        t_stale_ceil,
        t_fresh_drained,
        t_pro,
        t_taint_fail,
        t_taint_gen,
        t_aged,
        t_never,
        t_certain,
    ]
    unc_nov, items_nov = hf_ledger.classify_pool_uncertainty(all_toks, now_ts=nov28_ts)
    by_tok_nov = {it.token: it for it in items_nov}

    assert by_tok_nov[t_dead].is_uncertain is False and by_tok_nov[t_dead].reason == "auth_dead"
    assert by_tok_nov[t_stale_rem].is_uncertain is True and by_tok_nov[t_stale_rem].reason == "stale_drained"
    assert by_tok_nov[t_stale_ceil].is_uncertain is True and by_tok_nov[t_stale_ceil].reason == "stale_drained"
    assert by_tok_nov[t_fresh_drained].is_uncertain is False
    assert by_tok_nov[t_pro].is_uncertain is False  # before Dec 1 and remaining_s=298 <= 300!
    assert by_tok_nov[t_taint_fail].is_uncertain is True and by_tok_nov[t_taint_fail].reason == "unverified_taint"
    assert by_tok_nov[t_taint_gen].is_uncertain is True and by_tok_nov[t_taint_gen].reason == "unverified_taint"
    assert by_tok_nov[t_aged].is_uncertain is True and by_tok_nov[t_aged].reason == "aged_partial"
    assert by_tok_nov[t_never].is_uncertain is True and by_tok_nov[t_never].reason == "never_verified"
    assert by_tok_nov[t_certain].is_uncertain is False and by_tok_nov[t_certain].is_certain_full is True

    # On Dec 1, `t_pro` (`base_s=2400.0 > 300.0`) flips immediately to `pro_lapse` (even though `298 >= 295`)!
    _unc_dec, items_dec = hf_ledger.classify_pool_uncertainty([t_pro, t_certain], now_ts=dec01_ts)
    by_tok_dec = {it.token: it for it in items_dec}
    assert by_tok_dec[t_pro].is_uncertain is True and by_tok_dec[t_pro].reason == "pro_lapse"
    assert by_tok_dec[t_certain].is_uncertain is False and by_tok_dec[t_certain].is_certain_full is True

    # Once a synthesis attempt touches `t_certain` and lowers it to 270s (aged > 18h), it is NO LONGER certain!
    hf_ledger.record_success_debit("awints001/voxcpm2-a", t_certain, debit_s=26.0, now_ts=nov28_ts - 20 * 3600)
    c_after_use = hf_ledger.classify_token_uncertainty(t_certain, reload_meta=True, now_ts=nov28_ts)
    assert c_after_use.is_certain_full is False
    assert c_after_use.is_uncertain is True and c_after_use.reason == "aged_partial"


# ── Part 3: Space Selection, Priority Order, Probe Outcomes, & Hard Caps ─────


def test_part3_space_selection_and_priority_order():
    now = datetime(2026, 12, 1, 2, 0, 0, tzinfo=timezone.utc).timestamp()

    # 1. Space selection skips demo, cooling, non-RUNNING, and v1 Spaces without `/probe_quota`
    sp_cooling = "yunaisbusy/voxcpm2-9"
    sp_building = "yunaisbusy/voxcpm2-8"
    sp_v1_no_probe = "awints001/voxcpm2-b"
    sp_ready = "oneclick-org/voxcpm2-b"
    hf_ledger.record_space_timeout(sp_cooling, now_ts=now)

    stages = {
        sp_cooling: "RUNNING",
        sp_building: "BUILDING",
        sp_v1_no_probe: "RUNNING",
        sp_ready: "RUNNING",
    }

    def _client_factory(sp: str, _tok: str | None):
        if sp == sp_v1_no_probe:
            return SimpleNamespace(view_api=lambda return_format="dict": {"named_endpoints": {"/generate": {}}})
        return SimpleNamespace(
            view_api=lambda return_format="dict": {"named_endpoints": {"/generate": {}, "/probe_quota": {}}}
        )

    chosen = hf_cal.select_calibration_space(
        ["openbmb/VoxCPM-Demo", sp_cooling, sp_building, sp_v1_no_probe, sp_ready],
        probe_token="hf_any",
        space_stage_fn=lambda sp: stages.get(sp, "UNKNOWN"),
        client_factory=_client_factory,
        now_ts=now,
    )
    assert chosen == sp_ready

    # 2. Exact Priority Order:
    # (1) pro_lapse -> (2) stale_drained -> (3) rem >= 137 tainted/aged DESCENDING
    # -> (4) 45 <= rem < 137 tainted/aged ASCENDING -> (5) never_verified OLDEST first
    t_nv_newer = "hf_p_nv_newer"
    t_nv_older = "hf_p_nv_older"
    t_mid_110 = "hf_p_mid_110"
    t_mid_60 = "hf_p_mid_60"
    t_hi_150 = "hf_p_hi_150"
    t_hi_220 = "hf_p_hi_220"
    t_stale = "hf_p_stale"
    t_pro = "hf_p_pro"

    hf_ledger.save_pool_state(
        {
            "tokens": {
                hf_ledger.token_digest(t_nv_newer): {
                    "status": "ok",
                    "remaining_s": 300.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now - 30 * 3600),
                },
                hf_ledger.token_digest(t_nv_older): {
                    "status": "ok",
                    "remaining_s": 300.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now - 50 * 3600),
                },
                hf_ledger.token_digest(t_mid_110): {
                    "status": "ok",
                    "remaining_s": 110.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now - 20 * 3600),
                },
                hf_ledger.token_digest(t_mid_60): {
                    "status": "ok",
                    "remaining_s": 60.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now - 20 * 3600),
                },
                hf_ledger.token_digest(t_hi_150): {
                    "status": "ok",
                    "remaining_s": 150.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now - 20 * 3600),
                },
                hf_ledger.token_digest(t_hi_220): {
                    "status": "ok",
                    "remaining_s": 220.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now - 20 * 3600),
                },
                hf_ledger.token_digest(t_stale): {
                    "status": "ok",
                    "remaining_s": 10.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now - 8 * 3600),
                },
                hf_ledger.token_digest(t_pro): {
                    "status": "ok",
                    "remaining_s": 2400.0,
                    "base_s": 2400.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now - 3600),
                },
            },
            "space_gates": {sp_ready: 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )
    for tk in (t_mid_110, t_mid_60, t_hi_150, t_hi_220):
        hf_ledger.record_token_truth_timestamp(tk, now - 20 * 3600, verified_rem_s=150.0)

    ordered, _ = hf_ledger.classify_pool_uncertainty(
        [t_nv_newer, t_nv_older, t_mid_110, t_mid_60, t_hi_150, t_hi_220, t_stale, t_pro],
        now_ts=now,
    )
    assert [it.token for it in ordered] == [
        t_pro,       # (1) pro_lapse
        t_stale,     # (2) stale_drained
        t_hi_220,    # (3) >= 137 descending (220 before 150)
        t_hi_150,
        t_mid_60,    # (4) 45..137 ascending (60 before 110)
        t_mid_110,
        t_nv_older,  # (5) never_verified oldest first (-50h before -30h)
        t_nv_newer,
    ]


def test_part3_probe_outcomes_numeric_success_and_generic_followups(tmp_path):
    now = datetime(2026, 12, 1, 3, 0, 0, tzinfo=timezone.utc).timestamp()
    sp = "yunaisbusy/voxcpm2-9"
    gen_wav = _make_wav(tmp_path / "fup.wav")

    tok_num = "hf_out_numeric"
    tok_succ = "hf_out_success"
    tok_gen_cap = "hf_out_gen_capped45"
    tok_gen_adm = "hf_out_gen_admitted"
    tok_gen_3rd = "hf_out_gen_3rd_cap"
    tok_gen_sub45 = "hf_out_gen_sub45"

    # Seed all 6 as uncertain (`stale_drained` / `pro_lapse` / `unverified_taint`)
    hf_ledger.save_pool_state(
        {
            "tokens": {
                # Lapsed Pro (2400) with stale 90s ceiling -> numeric refusal `(297s vs 115s left)`
                # clears 90s ceiling (90 <= 115), does NOT set 297s ceiling, clamps base_s=300!
                hf_ledger.token_digest(tok_num): {
                    "status": "ok",
                    "remaining_s": 2400.0,
                    "base_s": 2400.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now - 3600),
                    "refused_gate_s": 90.0,
                    "refused_until": _iso(now + 1800),
                },
                # Drained yesterday (12s) with 60s ceiling & cooldown -> `/probe_quota` succeeds (gpu_s=1.5 -> debit=2.5)
                # -> recovers to `297 - 2.5 = 294.5` (or with gpu_s=0.8 -> debit=1.8 -> 295.2), clears ceiling & cooldown!
                hf_ledger.token_digest(tok_succ): {
                    "status": "ok",
                    "remaining_s": 12.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": _iso(now + 900),
                    "updated_at": _iso(now - 8 * 3600),
                    "refused_gate_s": 60.0,
                    "refused_until": _iso(now + 1800),
                },
                # Generic refusal on `/probe_quota`, `rem=80 >= 45` (2nd ascending in 45..137) -> follow-up generically refused at G=45 -> rem=44, ceiling=45!
                hf_ledger.token_digest(tok_gen_cap): {
                    "status": "ok",
                    "remaining_s": 80.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now - 600),
                },
                # Generic refusal on `/probe_quota`, `rem=50 >= 45` (1st ascending in 45..137) -> follow-up ADMITTED (`gpu_s=18.0 -> debit=19.0`) -> rem=31.0!
                hf_ledger.token_digest(tok_gen_adm): {
                    "status": "ok",
                    "remaining_s": 50.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now - 600),
                },
                # 3rd generic refusal (`rem=120 >= 45`, 3rd ascending in 45..137) -> skipped follow-up because max 2 follow-ups per sweep!
                hf_ledger.token_digest(tok_gen_3rd): {
                    "status": "ok",
                    "remaining_s": 120.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now - 600),
                },
                # Generic refusal with `rem=20 < 45` -> no follow-up runs (`generic_unprobed`)!
                hf_ledger.token_digest(tok_gen_sub45): {
                    "status": "ok",
                    "remaining_s": 20.0,
                    "base_s": 300.0,
                    "resets_at": "",
                    "cooldown_until": "",
                    "updated_at": _iso(now - 8 * 3600),
                },
            },
            "space_gates": {sp: 60},
            "in_flight_gpu_s": 0,
        },
        now_ts=now,
    )
    for tk in (tok_gen_cap, tok_gen_adm, tok_gen_3rd):
        hf_ledger.record_token_taint(tk, "failure_debit", now_ts=now - 100)

    calls_log: list[tuple[str, str]] = []

    class _OutcomeClient:
        def __init__(self, token: str | None):
            self.token = token

        def submit(self, *, api_name: str, **_kw):
            calls_log.append((str(self.token), api_name))
            if api_name == "/probe_quota":
                if self.token == tok_num:
                    return SimpleNamespace(
                        result=lambda **_k: (_ for _ in ()).throw(
                            RuntimeError(
                                "You have exceeded your free ZeroGPU quota (297s requested vs. 115s left). "
                                "Try again in 0:20:00."
                            )
                        )
                    )
                if self.token == tok_succ:
                    return SimpleNamespace(result=lambda **_k: 0.8)  # debit = clamp(1.8, 1.0, 5.0) = 1.8
                raise RuntimeError("ZeroGPU quota runs limit reached")
            if api_name == "/generate_session":
                if self.token == tok_gen_cap:
                    raise RuntimeError("ZeroGPU quota exceeded")
                if self.token == tok_gen_adm:
                    return SimpleNamespace(result=lambda **_k: (str(gen_wav), 18.0))  # debit = 19.0
                raise AssertionError(f"Unexpected follow-up for {self.token}")
            raise AssertionError(f"Unexpected endpoint {api_name}")

    tokens = [tok_num, tok_succ, tok_gen_cap, tok_gen_adm, tok_gen_3rd, tok_gen_sub45]
    rep = hf_cal.run_calibration_sweep(
        tokens=tokens,
        spaces=[sp],
        space_stage_fn=lambda _sp: "RUNNING",
        client_factory=lambda _sp, tok: _OutcomeClient(tok),
        now_fn=lambda: now,
    )

    rows_by_slot = {r["slot"]: r for r in rep["rows"]}
    st_after = hf_ledger.load_pool_state(now_ts=now)

    # 1. Numeric refusal: 115.0s, base_s clamped 2400 -> 300, resets_at = now + 1200, ceiling CLEARED (not 297!)
    r_num = rows_by_slot[0]
    assert r_num["outcome"] == "numeric_refusal" and r_num["gpu_cost_s"] == 0.0
    assert r_num["after_s"] == pytest.approx(115.0)
    row0 = st_after["tokens"][hf_ledger.token_digest(tok_num)]
    assert row0["base_s"] == pytest.approx(300.0)
    assert "refused_gate_s" not in row0
    assert hf_ledger._parse_ts(row0["resets_at"]) == pytest.approx(now + 1200.0)

    # 2. Probe success: `297.0 - 1.8 = 295.2s`, ceiling & cooldown cleared, stamped truth!
    r_succ = rows_by_slot[1]
    assert r_succ["outcome"] == "probe_success" and r_succ["gpu_cost_s"] == pytest.approx(1.8)
    assert r_succ["after_s"] == pytest.approx(295.2)
    row1 = st_after["tokens"][hf_ledger.token_digest(tok_succ)]
    assert "refused_gate_s" not in row1 and row1["cooldown_until"] == ""

    # 3. Generic refusal follow-up #1 (refused at 45s): `remaining_s = 44.0`, `refused_gate_s = 45.0`
    r_cap = rows_by_slot[2]
    assert r_cap["outcome"] == "generic_capped_45" and r_cap["after_s"] == pytest.approx(44.0)
    assert hf_ledger.get_token_refused_gate_s(tok_gen_cap, state=st_after, now_ts=now) == pytest.approx(45.0)

    # 4. Generic refusal follow-up #2 (admitted, debit 19.0s): `50.0 - 19.0 = 31.0s`, truth stamped!
    r_adm = rows_by_slot[3]
    assert r_adm["outcome"] == "followup_admitted" and r_adm["gpu_cost_s"] == pytest.approx(19.0)
    assert r_adm["after_s"] == pytest.approx(31.0)
    assert hf_ledger.get_token_last_truth_ts(tok_gen_adm) == pytest.approx(now)

    # 5. 3rd generic refusal & `< 45s` generic refusal: `generic_unprobed`, 0 follow-up calls, balances unchanged!
    assert rows_by_slot[4]["outcome"] == "generic_unprobed" and rows_by_slot[4]["after_s"] == pytest.approx(120.0)
    assert rows_by_slot[5]["outcome"] == "generic_unprobed" and rows_by_slot[5]["after_s"] == pytest.approx(20.0)
    followup_calls = [tok for tok, ep in calls_log if ep == "/generate_session"]
    assert len(followup_calls) == 2  # strictly capped at 2 per sweep!
    assert rep["totals"]["total_probe_gpu_cost_s"] == pytest.approx(1.8 + 19.0)


def test_part3_hard_caps_gpu_wall_and_transport_abort():
    now = 1_796_200_000.0
    sp = "yunaisbusy/voxcpm2-9"
    toks = [f"hf_cap_tok_{i}" for i in range(6)]

    def _seed_stale():
        hf_ledger.save_pool_state(
            {
                "tokens": {
                    hf_ledger.token_digest(t): {
                        "status": "ok",
                        "remaining_s": 10.0,
                        "base_s": 300.0,
                        "resets_at": "",
                        "cooldown_until": "",
                        "updated_at": _iso(now - 9 * 3600),
                    }
                    for t in toks
                },
                "space_gates": {sp: 60},
                "in_flight_gpu_s": 0,
            },
            now_ts=now,
        )

    # 1. GPU cap (`max_gpu_s = 12.0`): each probe returns `gpu_s = 10.0` -> clamped `debit_s = 5.0`.
    #    Probes 1 & 2 spend `10.0s`; before Probe 3 (`10.0 + 5.0 = 15.0 > 12.0`), stops with `gpu_cap`!
    _seed_stale()
    rep_gpu = hf_cal.run_calibration_sweep(
        tokens=toks,
        spaces=[sp],
        max_gpu_s=12.0,
        space_stage_fn=lambda _sp: "RUNNING",
        client_factory=lambda _sp, _tok: SimpleNamespace(predict=lambda **_k: 10.0),
        now_fn=lambda: now,
    )
    assert rep_gpu["aborted_reason"] == "gpu_cap"
    assert rep_gpu["totals"]["probed_count"] == 2
    assert rep_gpu["totals"]["total_probe_gpu_cost_s"] == pytest.approx(10.0)

    # 2. Wall-clock cap (`120.0 s`): stops as soon as elapsed >= 120.0 s!
    _seed_stale()
    mono = {"m": 500.0}

    def _slow_client(_sp, _tok):
        def _pred(**_k):
            mono["m"] += 65.0
            return 0.5

        return SimpleNamespace(predict=_pred)

    rep_wall = hf_cal.run_calibration_sweep(
        tokens=toks,
        spaces=[sp],
        space_stage_fn=lambda _sp: "RUNNING",
        client_factory=_slow_client,
        now_fn=lambda: now,
        mono_fn=lambda: mono["m"],
    )
    assert rep_wall["aborted_reason"] == "wall_clock_cap"
    assert rep_wall["totals"]["probed_count"] == 2

    # 3. Consecutive Space transport error abort (`2` consecutive errors aborts; non-transport resets streak!)
    _seed_stale()
    seq = iter(
        [
            ConnectionResetError("drop 1"),
            0.5,  # success resets streak back to 0!
            RuntimeError("HTTP 502 Bad Gateway"),
            RuntimeError("HTTP 503 Service Unavailable"),  # 2nd consecutive -> abort!
            0.5,  # never reached!
        ]
    )

    def _flaky_client(_sp, tok):
        if tok == toks[0] and not hasattr(_flaky_client, "_sel_done"):
            _flaky_client._sel_done = True  # select_calibration_space check
            return SimpleNamespace()

        def _pred(**_k):
            item = next(seq)
            if isinstance(item, BaseException):
                raise item
            return item

        return SimpleNamespace(predict=_pred)

    rep_tr = hf_cal.run_calibration_sweep(
        tokens=toks,
        spaces=[sp],
        space_stage_fn=lambda _sp: "RUNNING",
        client_factory=_flaky_client,
        now_fn=lambda: now,
    )
    assert rep_tr["aborted_reason"] == "transport_errors"
    assert rep_tr["totals"]["probed_count"] == 4


# ── Parts 4, 5, 6: Scheduler, Guards, Drift Report, No-Leak, Admin, & CLI ────


@pytest.mark.asyncio
async def test_part4_5_6_scheduler_guards_report_noleak_admin_and_cli(
    tmp_path, monkeypatch, caplog, _isolated_round2_store
):
    kv = _isolated_round2_store

    # 1. Dynamic setting registration & Supabase diet pins
    assert settings.hf_daily_calibration_enabled is False
    assert ds_mod.SETTING_TYPES["hf_daily_calibration_enabled"] is bool
    assert "hf_daily_calibration_enabled" in ds_mod.DYNAMIC_SETTING_KEYS
    assert "hf_daily_calibration_enabled" in ds_mod.CONFIG_SETTING_KEYS
    for non_cfg_key in (
        hf_cal.CALIBRATION_REPORT_STORE_KEY,
        hf_cal.WEB_BOOT_TS_STORE_KEY,
        hf_ledger.TOKEN_META_STORE_KEY,
        hf_ledger.DRIFT_WINDOW_STORE_KEY,
    ):
        assert non_cfg_key not in ds_mod.CONFIG_SETTING_KEYS

    eng, maker = await _open_test_db(tmp_path)
    orig_primary = settings.instance_primary
    orig_maint = settings.maintenance_mode
    orig_cal_en = settings.hf_daily_calibration_enabled
    try:
        mmt = ZoneInfo("Asia/Yangon")
        quiet_ts = datetime(2026, 11, 28, 4, 30, 0, tzinfo=mmt).timestamp()
        day_ts = datetime(2026, 11, 28, 14, 0, 0, tzinfo=mmt).timestamp()

        tok_secret = "hf_ULTRA_SECRET_RAW_CALIB_TOKEN_99"
        sp = "yunaisbusy/voxcpm2-9"
        hf_ledger.save_pool_state(
            {
                "tokens": {
                    hf_ledger.token_digest(tok_secret): {
                        "status": "ok",
                        "remaining_s": 25.0,
                        "base_s": 300.0,
                        "resets_at": "",
                        "cooldown_until": "",
                        "updated_at": _iso(quiet_ts - 8 * 3600),
                    }
                },
                "space_gates": {sp: 60},
                "in_flight_gpu_s": 0,
            },
            now_ts=quiet_ts,
        )

        # 2. Scheduler guards:
        # (a) Secondary instance (`instance_primary=False`) -> skip
        monkeypatch.setattr(settings, "instance_primary", False)
        assert (
            await hf_cal.check_calibration_guards(scheduled=True, boot_age_s=900.0, session_factory=maker, now_ts=quiet_ts)
        ).reason == "not_primary_instance"
        monkeypatch.setattr(settings, "instance_primary", True)

        # (b) Outside 04:15–06:00 MMT -> skip
        assert (
            await hf_cal.check_calibration_guards(scheduled=True, boot_age_s=900.0, session_factory=maker, now_ts=day_ts)
        ).reason == "outside_quiet_window"

        # (c) Boot age < 600s -> skip (both scheduled AND manual!)
        assert (
            await hf_cal.check_calibration_guards(scheduled=False, boot_age_s=599.0, session_factory=maker, now_ts=day_ts)
        ).reason == "boot_age_below_600s"

        # (d) Kill switch `hf_daily_calibration_enabled=False` -> blocks scheduled, ALLOWS manual!
        monkeypatch.setattr(settings, "hf_daily_calibration_enabled", False)
        assert (
            await hf_cal.check_calibration_guards(scheduled=True, boot_age_s=900.0, session_factory=maker, now_ts=quiet_ts)
        ).reason == "kill_switch_disabled"
        assert (
            await hf_cal.check_calibration_guards(scheduled=False, boot_age_s=900.0, session_factory=maker, now_ts=day_ts)
        ).allowed is True

        # (e) Maintenance mode, shutdown, in_flight_gpu_s > 0, active VoxCPM job, queued VoxCPM job
        monkeypatch.setattr(settings, "hf_daily_calibration_enabled", True)
        monkeypatch.setattr(settings, "maintenance_mode", True)
        assert (
            await hf_cal.check_calibration_guards(scheduled=True, boot_age_s=900.0, session_factory=maker, now_ts=quiet_ts)
        ).reason == "maintenance_mode"
        monkeypatch.setattr(settings, "maintenance_mode", False)

        assert (
            await hf_cal.check_calibration_guards(
                scheduled=True,
                boot_age_s=900.0,
                is_shutting_down_fn=lambda: True,
                session_factory=maker,
                now_ts=quiet_ts,
            )
        ).reason == "shutting_down"

        hf_ledger.acquire_in_flight_gpu("job_inflight_1", 45.0, now_ts=quiet_ts)
        assert (
            await hf_cal.check_calibration_guards(scheduled=True, boot_age_s=900.0, session_factory=maker, now_ts=quiet_ts)
        ).reason == "in_flight_gpu_nonzero"
        hf_ledger.release_in_flight_gpu("job_inflight_1", now_ts=quiet_ts)

        async with maker() as s:
            s.add(User(id=77001, username="cal_u", account_category="FREE"))
            await s.flush()
            j_run = Job(
                id="vc_running_guard",
                user_id=77001,
                status=JobStatus.RUNNING.value,
                job_type=JobType.VOICE_CLONE.value,
                engine="voxcpm",
            )
            s.add(j_run)
            await s.commit()
        assert (
            await hf_cal.check_calibration_guards(scheduled=True, boot_age_s=900.0, session_factory=maker, now_ts=quiet_ts)
        ).reason == "active_voxcpm_jobs"

        async with maker() as s:
            j = await s.get(Job, "vc_running_guard")
            j.status = JobStatus.QUEUED.value
            await s.commit()
        assert (
            await hf_cal.check_calibration_guards(scheduled=True, boot_age_s=900.0, session_factory=maker, now_ts=quiet_ts)
        ).reason == "queued_voxcpm_jobs"

        async with maker() as s:
            j = await s.get(Job, "vc_running_guard")
            j.status = JobStatus.SUCCESS.value
            await s.commit()

        # Verify per-token live job guard aborts mid-sweep (`aborted_reason == "job_guard"`)
        rep_jg = hf_cal.run_calibration_sweep(
            tokens=[tok_secret],
            spaces=[sp],
            space_stage_fn=lambda _s: "RUNNING",
            client_factory=lambda _s, _t: SimpleNamespace(predict=lambda **_k: 0.5),
            per_token_guard_fn=lambda: False,
            mark_daily_dedupe=False,
            now_fn=lambda: quiet_ts,
        )
        assert rep_jg["aborted_reason"] == "job_guard"
        assert rep_jg["totals"]["probed_count"] == 0

        # 3. Scheduled sweep executes ONCE for `2026-11-28`, logs `hf_pool_calibration_sweep_report`,
        #    dedupes a second call on the same MMT day, and maintains a 7-day rolling history!
        probe_hits = {"n": 0}

        def _cal_client(_sp, _tok):
            def _pred(**_k):
                probe_hits["n"] += 1
                return 0.5  # debit = 1.5s -> 25.0s recovers to 295.5s (+270.5s recovered!)

            return SimpleNamespace(predict=_pred)

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="app.services.hf_calibration"):
            res1 = await hf_cal.maybe_run_scheduled_calibration_sweep(
                boot_age_s=900.0,
                session_factory=maker,
                now_ts=quiet_ts,
                sweep_kwargs={
                    "tokens": [tok_secret],
                    "spaces": [sp],
                    "account_names": {0: "owner-acct-0"},
                    "space_stage_fn": lambda _s: "RUNNING",
                    "client_factory": _cal_client,
                },
            )
        assert res1["last_sweep_date_mmt"] == "2026-11-28"
        assert probe_hits["n"] == 1
        assert res1["totals"] == {
            "uncertain_count": 1,
            "probed_count": 1,
            "positive_drift_corrected_s": 0.0,
            "negative_drift_recovered_s": 270.5,
            "max_abs_drift_s": 270.5,
            "total_probe_gpu_cost_s": 1.5,
        }
        assert set(res1["rows"][0].keys()) == {
            "slot",
            "account",
            "key_id",
            "reason",
            "before_s",
            "after_s",
            "delta_s",
            "outcome",
            "gpu_cost_s",
        }
        log_lines = [r.getMessage() for r in caplog.records if "hf_pool_calibration_sweep_report" in r.getMessage()]
        assert len(log_lines) == 1 and "negative_drift_recovered_s=270.5" in log_lines[0]

        # Second scheduled call 10 minutes later on same MMT day -> deduped (`already_swept_today`, 0 probes)!
        res_dedup = await hf_cal.maybe_run_scheduled_calibration_sweep(
            boot_age_s=1500.0,
            session_factory=maker,
            now_ts=quiet_ts + 600.0,
            sweep_kwargs={"tokens": [tok_secret], "spaces": [sp]},
        )
        assert res_dedup == {"skipped": True, "reason": "already_swept_today"}
        assert probe_hits["n"] == 1

        # Simulate 8 distinct MMT days -> rolling `history` trims cleanly to 7 days!
        for day_offset in range(1, 9):
            d_ts = quiet_ts + day_offset * 86400.0
            hf_cal.run_calibration_sweep(
                tokens=[],
                spaces=[sp],
                now_fn=lambda _t=d_ts: _t,
            )
        rep_stored = hf_cal.load_calibration_report()
        assert rep_stored is not None
        assert len(rep_stored["history"]) == 7

        # 4. Strict NO-LEAK + Admin Key Health overlay check
        monkeypatch.setattr(kh_mod, "_load_pools", lambda: ([tok_secret], [], [], [sp]))
        monkeypatch.setattr(
            kh_mod,
            "_probe_hf_account",
            lambda idx, _t: {
                "index": idx,
                "account": "owner-acct-0",
                "status": "ok",
                "remaining_s": None,
                "base_s": None,
                "quota_resets_at": None,
                "quota_error": kh_mod.QUOTA_SCOPE_UNSUPPORTED_ERROR,
                "refill": None,
            },
        )
        monkeypatch.setattr(kh_mod, "_probe_space", lambda name: {"name": name, "stage": "RUNNING"})
        kh_payload = kh_mod.get_key_health(force_refresh=True)
        assert "calibration" in kh_payload["providers"]["hf"]
        assert len(kh_payload["providers"]["hf"]["calibration"]["history"]) == 7

        combined_dump = json.dumps({"kv": kv, "kh": kh_payload}, ensure_ascii=False)
        assert tok_secret not in combined_dump
        assert tok_secret[:12] not in combined_dump
        assert "ULTRA_SECRET" not in combined_dump
    finally:
        settings.instance_primary = orig_primary
        settings.maintenance_mode = orig_maint
        settings.hf_daily_calibration_enabled = orig_cal_en
        await eng.dispose()


def test_part6_manual_cli_dry_run_and_once(tmp_path, monkeypatch):
    import asyncio as _aio

    eng, maker = _aio.run(_open_test_db(tmp_path, "cli_r2.db"))
    orig_cal_en = settings.hf_daily_calibration_enabled
    try:
        mmt = ZoneInfo("Asia/Yangon")
        day_ts = datetime(2026, 11, 28, 14, 0, 0, tzinfo=mmt).timestamp()
        tok_secret = "hf_ULTRA_SECRET_CLI_TOK_88"
        sp = "yunaisbusy/voxcpm2-9"
        hf_ledger.save_pool_state(
            {
                "tokens": {
                    hf_ledger.token_digest(tok_secret): {
                        "status": "ok",
                        "remaining_s": 40.0,
                        "base_s": 300.0,
                        "resets_at": "",
                        "cooldown_until": "",
                        "updated_at": _iso(day_ts - 8 * 3600),
                    }
                },
                "space_gates": {sp: 60},
                "in_flight_gpu_s": 0,
            },
            now_ts=day_ts,
        )

        # `--dry-run`: 0 probe calls, prints planned list, zero secret material!
        monkeypatch.setattr(settings, "hf_daily_calibration_enabled", False)
        probe_hits = {"n": 0}

        def _cal_client(_sp, _tok):
            def _pred(**_k):
                probe_hits["n"] += 1
                return 0.5

            return SimpleNamespace(predict=_pred)

        cli_logs: list[str] = []
        rc_dry = cal_cli.main(
            ["--dry-run"],
            tokens=[tok_secret],
            now_ts=day_ts,
            out_fn=cli_logs.append,
        )
        assert rc_dry == 0
        assert probe_hits["n"] == 0
        assert any("uncertain_count=1" in ln for ln in cli_logs)
        assert all(tok_secret not in ln and "ULTRA_SECRET" not in ln for ln in cli_logs)

        # `--once`: enforces boot/job guards (skips when boot_age < 600s; runs when >= 600s even outside 04:15 MMT & kill switch False!)
        cli_logs.clear()
        rc_skip = cal_cli.main(
            ["--once"],
            tokens=[tok_secret],
            boot_age_s=120.0,
            session_factory=maker,
            now_ts=day_ts + 1.0,
            out_fn=cli_logs.append,
        )
        assert rc_skip == 0
        assert probe_hits["n"] == 0
        assert any("SKIP calibration sweep: guard=boot_age_below_600s" in ln for ln in cli_logs)

        cli_logs.clear()
        rc_once = cal_cli.main(
            ["--once"],
            tokens=[tok_secret],
            boot_age_s=900.0,
            session_factory=maker,
            now_ts=day_ts + 2.0,
            sweep_kwargs={
                "spaces": [sp],
                "space_stage_fn": lambda _s: "RUNNING",
                "client_factory": _cal_client,
            },
            out_fn=cli_logs.append,
        )
        assert rc_once == 0
        assert probe_hits["n"] == 1
        assert any("OK calibration sweep:" in ln for ln in cli_logs)
        assert all(tok_secret not in ln and "ULTRA_SECRET" not in ln for ln in cli_logs)
    finally:
        settings.hf_daily_calibration_enabled = orig_cal_en
        _aio.run(eng.dispose())

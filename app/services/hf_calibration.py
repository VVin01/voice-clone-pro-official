"""Daily HF ZeroGPU Pool Calibration Sweep (Method 3 — Round 2, 2026-11-27).

Probes ONLY uncertain tokens once daily during 04:15–06:00 Asia/Yangon on the
primary web instance (`settings.instance_primary == True`) when `hf_daily_calibration_enabled`
is True (or on demand via `scripts/calibrate_hf_ledger.py --once`).

Hard Guardrails:
- Never touches `aj_hf:pool_state` schema (uses Round 1 atomic mutations + separate
  `aj_hf:calibration_report` / `aj_hf:web_boot_ts` keys, outside `CONFIG_SETTING_KEYS`).
- Zero raw token material in logs, exceptions, or `aj_hf:calibration_report`
  (account name + 12-char SHA-256 digest `key_id` only).
- Hard caps per sweep: `SWEEP_MAX_GPU_S = 60.0`, `SWEEP_MAX_WALL_S = 120.0`,
  concurrency `1`, at most `2` generic-refusal 1-char `/generate_session` follow-ups,
  and abort after `2` consecutive Space transport errors.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import threading
import time
from typing import Any, Awaitable, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from app.config import settings
from app.services import hf_pool_ledger as hf_ledger
from app.services.key_rotation import (
    botsetting_sync_delete,
    botsetting_sync_mutate,
    botsetting_sync_read,
    botsetting_sync_write,
)
from app.services.voxcpm_engine import (
    _is_hf_token_auth_failure,
    _is_pair_space_timeout,
    _is_pair_transport_error,
    _parse_admission_refusal,
    _quota_failure_kind,
    _resolve_result_and_gpu_seconds,
    get_available_hf_tokens,
    get_configured_spaces,
    is_cpu_demo_space,
)

logger = logging.getLogger(__name__)

CALIBRATION_REPORT_STORE_KEY = "aj_hf:calibration_report"
WEB_BOOT_TS_STORE_KEY = "aj_hf:web_boot_ts"

SWEEP_MAX_GPU_S = 60.0
SWEEP_MAX_WALL_S = 120.0
SWEEP_MAX_FOLLOWUPS = 2
SWEEP_MAX_CONSECUTIVE_TRANSPORT_ERRORS = 2
PROBE_QUOTA_MAX_DEBIT_S = 5.0
FOLLOWUP_DECLARED_GPU_S = 30.0
FOLLOWUP_DOOR_S = 45.0
BOOT_GUARD_MIN_AGE_S = 600.0
REPORT_HISTORY_DAYS = 7

QUIET_WINDOW_START_MINUTES = 4 * 60 + 15  # 04:15 Asia/Yangon
QUIET_WINDOW_END_MINUTES = 6 * 60         # 06:00 Asia/Yangon

OWNED_SPACE_OWNERS = frozenset({"yunaisbusy", "awints001", "oneclick-org"})

_cal_lock = threading.Lock()
_sweep_run_lock = threading.Lock()
_mem_last_sweep_date_mmt: str | None = None
_process_boot_mono: float = time.monotonic()


def _mmt_tz():
    try:
        return ZoneInfo("Asia/Yangon")
    except Exception:
        return timezone(timedelta(hours=6, minutes=30))


def current_mmt_datetime(now_ts: float | None = None) -> datetime:
    ts = time.time() if now_ts is None else float(now_ts)
    return datetime.fromtimestamp(ts, tz=_mmt_tz())


def is_in_quiet_window_mmt(now_ts: float | None = None) -> tuple[bool, str]:
    """Return `(in_window, YYYY-MM-DD_mmt)` for 04:15–06:00 Asia/Yangon."""
    dt = current_mmt_datetime(now_ts)
    mins = dt.hour * 60 + dt.minute
    in_win = QUIET_WINDOW_START_MINUTES <= mins < QUIET_WINDOW_END_MINUTES
    return in_win, dt.strftime("%Y-%m-%d")


def record_web_boot_timestamp(now_ts: float | None = None) -> None:
    """Record `vcp-web` startup epoch in `aj_hf:web_boot_ts` for deploy-overlap boot guard."""
    global _process_boot_mono
    _process_boot_mono = time.monotonic()
    ts = time.time() if now_ts is None else float(now_ts)
    try:
        botsetting_sync_write(WEB_BOOT_TS_STORE_KEY, f"{ts:.3f}")
    except Exception:
        pass


def get_effective_boot_age_s(*, now_ts: float | None = None, in_web_process: bool = False) -> float:
    """Return seconds since `vcp-web` booted (combining persisted deploy stamp and in-process uptime)."""
    now = time.time() if now_ts is None else float(now_ts)
    ages: list[float] = []
    if in_web_process:
        ages.append(max(0.0, time.monotonic() - _process_boot_mono))
        try:
            from app.webapp import server as _srv

            boot_dt = getattr(_srv, "_SERVER_BOOT_AT", None)
            if boot_dt is not None:
                b_ts = boot_dt.timestamp() if boot_dt.tzinfo else boot_dt.replace(tzinfo=timezone.utc).timestamp()
                ages.append(max(0.0, now - b_ts))
        except Exception:
            pass
    raw = botsetting_sync_read(WEB_BOOT_TS_STORE_KEY)
    if raw:
        try:
            web_ts = float(raw)
            if web_ts > 0:
                ages.append(max(0.0, now - web_ts))
        except (TypeError, ValueError):
            pass
    if ages:
        return min(ages)
    # Standalone CLI invocation with no active web-deploy stamp recorded -> treat as steady state.
    return BOOT_GUARD_MIN_AGE_S + 1.0


def _default_is_shutting_down() -> bool:
    try:
        from app.webapp import server as _srv

        return bool(getattr(_srv, "_SHUTTING_DOWN", False))
    except Exception:
        return False


def load_calibration_report() -> dict[str, Any] | None:
    """Load persisted `aj_hf:calibration_report` dict, or None if absent."""
    raw = botsetting_sync_read(CALIBRATION_REPORT_STORE_KEY)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
        return dict(parsed) if isinstance(parsed, Mapping) else None
    except Exception:
        return None


def get_latest_calibration_summary() -> dict[str, Any] | None:
    """Display-safe summary for Admin Key Health (totals + 7-day history, zero token rows/secrets)."""
    rep = load_calibration_report()
    if not isinstance(rep, Mapping):
        return None
    totals = rep.get("totals")
    if not isinstance(totals, Mapping):
        return None
    return {
        "swept_at": rep.get("swept_at"),
        "last_sweep_date_mmt": rep.get("last_sweep_date_mmt"),
        "probe_space": rep.get("probe_space"),
        "aborted_reason": rep.get("aborted_reason"),
        "totals": dict(totals),
        "history": list(rep.get("history") or [])[-REPORT_HISTORY_DAYS:],
    }


def reset_calibration_for_tests(*, clear_store: bool = False) -> None:
    global _mem_last_sweep_date_mmt
    with _cal_lock:
        _mem_last_sweep_date_mmt = None
    try:
        from app.webapp import server as _srv

        _srv._SHUTTING_DOWN = False
    except Exception:
        pass
    if clear_store:
        botsetting_sync_delete(CALIBRATION_REPORT_STORE_KEY)
        botsetting_sync_delete(WEB_BOOT_TS_STORE_KEY)


def _default_space_stage(space_name: str) -> str:
    from app.services.key_health import _probe_space

    res = _probe_space(space_name)
    return str((res or {}).get("stage") or "UNKNOWN").strip().upper()


def _default_client_factory(space_name: str, hf_token: str | None) -> Any:
    from gradio_client import Client

    return Client(space_name, token=hf_token)


def _client_supports_probe_quota(client: Any) -> bool:
    """True when `client` advertises `/probe_quota` (or is a lightweight unit-test stub without `view_api`)."""
    view_fn = getattr(client, "view_api", None)
    if not callable(view_fn):
        return True
    try:
        info = view_fn(return_format="dict")
    except TypeError:
        info = view_fn()
    except Exception:
        return False
    if not isinstance(info, Mapping):
        return True
    named = info.get("named_endpoints")
    if not isinstance(named, Mapping):
        return True
    return "/probe_quota" in named or "probe_quota" in named


def select_calibration_space(
    spaces: Sequence[str] | None = None,
    *,
    state: Mapping[str, Any] | None = None,
    probe_token: str | None = None,
    space_stage_fn: Callable[[str], str] | None = None,
    client_factory: Callable[[str, str | None], Any] | None = None,
    now_ts: float | None = None,
) -> str | None:
    """Pick ONE `RUNNING`, non-cooling Space whose app advertises `/probe_quota`."""
    now = time.time() if now_ts is None else float(now_ts)
    st = hf_ledger.load_pool_state(now_ts=now) if state is None else state
    raw_spaces = list(spaces) if spaces is not None else get_configured_spaces()
    stage_fn = space_stage_fn or _default_space_stage
    c_factory = client_factory or _default_client_factory

    # Prefer owned fleet Spaces first while preserving configured order.
    owned_first: list[str] = []
    others: list[str] = []
    for sp in raw_spaces:
        clean_sp = str(sp or "").strip()
        if not clean_sp or is_cpu_demo_space(clean_sp):
            continue
        owner = clean_sp.split("/", 1)[0].strip().lower() if "/" in clean_sp else ""
        if owner in OWNED_SPACE_OWNERS:
            owned_first.append(clean_sp)
        else:
            others.append(clean_sp)

    for sp in owned_first + others:
        if hf_ledger.is_space_cooling(sp, state=st, now_ts=now):
            continue
        try:
            stage = str(stage_fn(sp) or "").strip().upper()
        except Exception:
            continue
        if stage != "RUNNING":
            continue
        try:
            probe_client = c_factory(sp, probe_token)
            if not _client_supports_probe_quota(probe_client):
                continue
        except Exception:
            continue
        return sp
    return None


def _invoke_gradio_endpoint(
    client: Any,
    *,
    api_name: str,
    timeout_s: float,
    **kwargs: Any,
) -> Any:
    """Submit an endpoint call on `client` and wait up to `timeout_s` for its result."""
    if hasattr(client, "submit"):
        job = client.submit(api_name=api_name, **kwargs)
        res_fn = getattr(job, "result", None)
        if callable(res_fn):
            try:
                return res_fn(timeout=max(1.0, float(timeout_s)))
            except TypeError:
                return res_fn()
        return job
    if hasattr(client, "predict"):
        return client.predict(api_name=api_name, **kwargs)
    raise RuntimeError("Gradio client has neither submit nor predict")


def _extract_probe_gpu_s(raw_res: Any) -> float:
    cand = raw_res[0] if isinstance(raw_res, (list, tuple)) and len(raw_res) >= 1 else raw_res
    if isinstance(cand, (int, float)) and not isinstance(cand, bool):
        f = float(cand)
        if math.isfinite(f) and f >= 0.0:
            return f
    return 0.5


def plan_calibration_sweep(
    tokens: Sequence[str] | None = None,
    *,
    state: Mapping[str, Any] | None = None,
    account_names: Mapping[int, str] | None = None,
    now_ts: float | None = None,
) -> list[dict[str, Any]]:
    """Classify and return the ordered uncertain plan without making any network/probe calls (`--dry-run`)."""
    now = time.time() if now_ts is None else float(now_ts)
    pool = list(tokens) if tokens is not None else get_available_hf_tokens()
    if account_names is None:
        from app.services.key_health import get_cached_hf_account_names

        account_names = get_cached_hf_account_names()
    ordered_uncertain, _all = hf_ledger.classify_pool_uncertainty(pool, state=state, now_ts=now)
    plan_rows: list[dict[str, Any]] = []
    for rank, item in enumerate(ordered_uncertain, start=1):
        plan_rows.append(
            {
                "rank": rank,
                "slot": item.slot,
                "account": account_names.get(item.slot),
                "key_id": item.key_id,
                "reason": item.reason,
                "reasons": list(item.reasons),
                "remaining_s": item.remaining_s,
                "base_s": item.base_s,
            }
        )
    return plan_rows


def _persist_calibration_report(
    *,
    swept_at_iso: str,
    date_mmt: str,
    mark_daily_dedupe: bool,
    probe_space: str | None,
    aborted_reason: str | None,
    rows: list[dict[str, Any]],
    totals: dict[str, Any],
) -> dict[str, Any]:
    global _mem_last_sweep_date_mmt
    result_box: dict[str, Any] = {}

    def _mutate_rep(raw_str: str | None) -> str | None:
        prev: dict[str, Any] = {}
        if raw_str:
            try:
                parsed = json.loads(raw_str)
                if isinstance(parsed, Mapping):
                    prev = dict(parsed)
            except Exception:
                prev = {}
        prev_hist = [
            dict(h)
            for h in (prev.get("history") or [])
            if isinstance(h, Mapping) and h.get("date_mmt")
        ]
        hist_by_date: dict[str, dict[str, Any]] = {str(h["date_mmt"]): h for h in prev_hist}
        hist_by_date[date_mmt] = {
            "date_mmt": date_mmt,
            "swept_at": swept_at_iso,
            **totals,
        }
        ordered_dates = sorted(hist_by_date.keys())[-REPORT_HISTORY_DAYS:]
        new_history = [hist_by_date[d] for d in ordered_dates]

        last_date = date_mmt if mark_daily_dedupe else (prev.get("last_sweep_date_mmt") or "")
        doc: dict[str, Any] = {
            "v": 1,
            "swept_at": swept_at_iso,
            "last_sweep_date_mmt": last_date,
            "probe_space": probe_space,
            "aborted_reason": aborted_reason,
            "totals": totals,
            "rows": rows,
            "history": new_history,
        }
        result_box["doc"] = doc
        return json.dumps(doc, separators=(",", ":"))

    botsetting_sync_mutate(
        CALIBRATION_REPORT_STORE_KEY,
        _mutate_rep,
        read_fn=botsetting_sync_read,
        write_fn=botsetting_sync_write,
    )
    if mark_daily_dedupe:
        with _cal_lock:
            _mem_last_sweep_date_mmt = date_mmt
    return result_box.get("doc") or {}


def run_calibration_sweep(
    *,
    tokens: Sequence[str] | None = None,
    spaces: Sequence[str] | None = None,
    account_names: Mapping[int, str] | None = None,
    client_factory: Callable[[str, str | None], Any] | None = None,
    space_stage_fn: Callable[[str], str] | None = None,
    per_token_guard_fn: Callable[[], bool] | None = None,
    mark_daily_dedupe: bool = True,
    max_gpu_s: float = SWEEP_MAX_GPU_S,
    max_wall_s: float = SWEEP_MAX_WALL_S,
    max_followups: int = SWEEP_MAX_FOLLOWUPS,
    max_consecutive_transport_errors: int = SWEEP_MAX_CONSECUTIVE_TRANSPORT_ERRORS,
    now_fn: Callable[[], float] = time.time,
    mono_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Execute one Method 3 calibration sweep (concurrency 1) and persist `aj_hf:calibration_report`."""
    if not _sweep_run_lock.acquire(blocking=False):
        return {"skipped": True, "reason": "already_running"}

    try:
        start_now = float(now_fn())
        start_mono = float(mono_fn())
        _in_win, date_mmt = is_in_quiet_window_mmt(start_now)
        pool = list(tokens) if tokens is not None else get_available_hf_tokens()
        if account_names is None:
            from app.services.key_health import get_cached_hf_account_names

            account_names = get_cached_hf_account_names()

        c_factory = client_factory or _default_client_factory
        st = hf_ledger.load_pool_state(force=True, now_ts=start_now)
        ordered_uncertain, _all = hf_ledger.classify_pool_uncertainty(pool, state=st, now_ts=start_now)
        uncertain_count = len(ordered_uncertain)

        chosen_space: str | None = None
        aborted_reason: str | None = None
        rows: list[dict[str, Any]] = []
        gpu_spent_s = 0.0
        followups_used = 0
        consecutive_transport_errors = 0

        if uncertain_count > 0:
            first_tok = ordered_uncertain[0].token
            chosen_space = select_calibration_space(
                spaces,
                state=st,
                probe_token=first_tok,
                space_stage_fn=space_stage_fn,
                client_factory=c_factory,
                now_ts=start_now,
            )
            if chosen_space is None:
                aborted_reason = "no_probe_space"

        if chosen_space is not None:
            for item in ordered_uncertain:
                if per_token_guard_fn is not None and not per_token_guard_fn():
                    aborted_reason = "job_guard"
                    break
                elapsed_wall = float(mono_fn()) - start_mono
                if elapsed_wall >= float(max_wall_s):
                    aborted_reason = "wall_clock_cap"
                    break
                # Stop BEFORE a `/probe_quota` call whose max debit (`5.0 s`) could exceed `max_gpu_s`.
                if (gpu_spent_s + PROBE_QUOTA_MAX_DEBIT_S) > (float(max_gpu_s) + 1e-9):
                    aborted_reason = "gpu_cap"
                    break

                tok = item.token
                call_now = float(now_fn())
                rem_wall = max(1.0, float(max_wall_s) - elapsed_wall)
                before_s = item.remaining_s
                after_s = item.remaining_s
                gpu_cost_s = 0.0
                outcome = "generic_unprobed"

                try:
                    client = c_factory(chosen_space, tok)
                    raw_probe = _invoke_gradio_endpoint(
                        client,
                        api_name="/probe_quota",
                        timeout_s=min(30.0, rem_wall),
                    )
                    probe_gpu_s = _extract_probe_gpu_s(raw_probe)
                    before_s, after_s, debit_s, _ = hf_ledger.apply_calibration_probe_success(
                        tok,
                        probe_gpu_s,
                        now_ts=float(now_fn()),
                    )
                    gpu_cost_s = debit_s
                    gpu_spent_s = round(gpu_spent_s + debit_s, 4)
                    consecutive_transport_errors = 0
                    outcome = "probe_success"
                except Exception as exc:
                    err_str = str(exc or "")
                    adm = _parse_admission_refusal(err_str)
                    if adm is not None:
                        _req_s, left_s = adm
                        before_s, after_s, _ = hf_ledger.apply_calibration_numeric_refusal(
                            tok,
                            left_s,
                            error_text=err_str,
                            now_ts=float(now_fn()),
                        )
                        gpu_cost_s = 0.0
                        consecutive_transport_errors = 0
                        outcome = "numeric_refusal"
                    elif _quota_failure_kind(err_str) is not None:
                        consecutive_transport_errors = 0
                        cur_rem = hf_ledger.get_token_remaining_s(tok, now_ts=call_now)
                        eff_rem = float(cur_rem) if cur_rem is not None else item.remaining_s
                        can_followup = (
                            followups_used < int(max_followups)
                            and eff_rem >= FOLLOWUP_DOOR_S
                            and (gpu_spent_s + FOLLOWUP_DECLARED_GPU_S) <= (float(max_gpu_s) + 1e-9)
                            and (float(mono_fn()) - start_mono) < float(max_wall_s)
                        )
                        if can_followup:
                            followups_used += 1
                            try:
                                f_client = c_factory(chosen_space, tok)
                                f_rem_wall = max(1.0, float(max_wall_s) - (float(mono_fn()) - start_mono))
                                f_raw = _invoke_gradio_endpoint(
                                    f_client,
                                    api_name="/generate_session",
                                    timeout_s=min(45.0, f_rem_wall),
                                    text_input="က",
                                    control_instruction="",
                                    reference_wav_path_input=None,
                                    use_prompt_text=False,
                                    prompt_text_input="",
                                    cfg_value_input=2.0,
                                    do_normalize=True,
                                    denoise=False,
                                )
                                _wav_p, f_gpu_s = _resolve_result_and_gpu_seconds(f_raw)
                                f_debit = (
                                    hf_ledger.compute_method1_debit_s(
                                        f_gpu_s,
                                        session_mode=True,
                                        chars=1,
                                    )
                                    if f_gpu_s is not None
                                    else hf_ledger.session_call_gate_s(1)
                                )
                                before_s, after_s, _ = hf_ledger.apply_calibration_followup_admitted(
                                    tok,
                                    f_debit,
                                    floor_s=FOLLOWUP_DOOR_S,
                                    now_ts=float(now_fn()),
                                )
                                gpu_cost_s = f_debit
                                gpu_spent_s = round(gpu_spent_s + f_debit, 4)
                                outcome = "followup_admitted"
                            except Exception as f_exc:
                                f_str = str(f_exc or "")
                                f_adm = _parse_admission_refusal(f_str)
                                if f_adm is not None:
                                    before_s, after_s, _ = hf_ledger.apply_calibration_numeric_refusal(
                                        tok,
                                        f_adm[1],
                                        error_text=f_str,
                                        now_ts=float(now_fn()),
                                    )
                                    gpu_cost_s = 0.0
                                    outcome = "numeric_refusal"
                                elif _quota_failure_kind(f_str) is not None:
                                    before_s, after_s, _ = hf_ledger.apply_calibration_generic_capped(
                                        tok,
                                        FOLLOWUP_DOOR_S,
                                        ttl_seconds=3600.0,
                                        now_ts=float(now_fn()),
                                    )
                                    gpu_cost_s = 0.0
                                    outcome = "generic_capped_45"
                                elif _is_pair_transport_error(f_exc) or _is_pair_space_timeout(f_exc):
                                    consecutive_transport_errors += 1
                                    before_s = round(eff_rem, 2)
                                    after_s = round(eff_rem, 2)
                                    outcome = "transport_error"
                                else:
                                    before_s = round(eff_rem, 2)
                                    after_s = round(eff_rem, 2)
                                    outcome = "generic_unprobed"
                        else:
                            before_s = round(eff_rem, 2)
                            after_s = round(eff_rem, 2)
                            outcome = "generic_unprobed"
                    elif _is_hf_token_auth_failure(err_str):
                        consecutive_transport_errors = 0
                        quarantined = hf_ledger.handle_auth_failure(tok, now_ts=float(now_fn()))
                        before_s = round(item.remaining_s, 2)
                        after_s = 0.0 if quarantined else before_s
                        outcome = "auth_failure"
                    elif _is_pair_transport_error(exc) or _is_pair_space_timeout(exc) or isinstance(exc, TimeoutError):
                        consecutive_transport_errors += 1
                        before_s = round(item.remaining_s, 2)
                        after_s = round(item.remaining_s, 2)
                        outcome = "transport_error"
                    else:
                        consecutive_transport_errors += 1
                        before_s = round(item.remaining_s, 2)
                        after_s = round(item.remaining_s, 2)
                        outcome = "transport_error"

                rows.append(
                    {
                        "slot": int(item.slot),
                        "account": account_names.get(item.slot),
                        "key_id": item.key_id,
                        "reason": item.reason,
                        "before_s": round(float(before_s), 2),
                        "after_s": round(float(after_s), 2),
                        "delta_s": round(float(after_s) - float(before_s), 2),
                        "outcome": outcome,
                        "gpu_cost_s": round(float(gpu_cost_s), 2),
                    }
                )

                if consecutive_transport_errors >= int(max_consecutive_transport_errors):
                    aborted_reason = "transport_errors"
                    break

        pos_corrected = 0.0
        neg_recovered = 0.0
        max_abs = 0.0
        overestimated_cnt = 0
        max_pos_drift = 0.0
        for r in rows:
            b_val = float(r["before_s"])
            a_val = float(r["after_s"])
            cost_val = float(r["gpu_cost_s"])
            if a_val > b_val:
                rec = round(a_val - b_val, 2)
                neg_recovered += rec
                if rec > max_abs:
                    max_abs = rec
            elif b_val > a_val:
                corr = round(max(0.0, (b_val - a_val) - cost_val), 2)
                pos_corrected += corr
                if corr > max_abs:
                    max_abs = corr
                if corr > max_pos_drift:
                    max_pos_drift = corr
                if corr > 25.0:
                    overestimated_cnt += 1

        totals = {
            "uncertain_count": int(uncertain_count),
            "probed_count": len(rows),
            "positive_drift_corrected_s": round(pos_corrected, 2),
            "negative_drift_recovered_s": round(neg_recovered, 2),
            "max_abs_drift_s": round(max_abs, 2),
            "total_probe_gpu_cost_s": round(gpu_spent_s, 2),
        }

        if pos_corrected > 0.0 or len(rows) > 0:
            hf_ledger.record_calibration_drift_stats(
                pos_corrected,
                overestimated_cnt,
                max_pos_drift,
                now_ts=float(now_fn()),
            )

        swept_at_iso = datetime.fromtimestamp(float(now_fn()), tz=timezone.utc).isoformat()
        report_doc = _persist_calibration_report(
            swept_at_iso=swept_at_iso,
            date_mmt=date_mmt,
            mark_daily_dedupe=bool(mark_daily_dedupe),
            probe_space=chosen_space,
            aborted_reason=aborted_reason,
            rows=rows,
            totals=totals,
        )

        logger.info(
            "hf_pool_calibration_sweep_report space=%s uncertain_count=%d probed_count=%d "
            "positive_drift_corrected_s=%.1f negative_drift_recovered_s=%.1f "
            "max_abs_drift_s=%.1f total_probe_gpu_cost_s=%.2f aborted_reason=%s",
            chosen_space or "none",
            totals["uncertain_count"],
            totals["probed_count"],
            totals["positive_drift_corrected_s"],
            totals["negative_drift_recovered_s"],
            totals["max_abs_drift_s"],
            totals["total_probe_gpu_cost_s"],
            aborted_reason or "none",
        )
        return report_doc
    finally:
        _sweep_run_lock.release()


@dataclass(frozen=True)
class GuardStatus:
    allowed: bool
    reason: str


async def check_calibration_guards(
    *,
    scheduled: bool = True,
    boot_age_s: float | None = None,
    in_web_process: bool = False,
    is_shutting_down_fn: Callable[[], bool] | None = None,
    session_factory: Callable[[], Any] | None = None,
    now_ts: float | None = None,
) -> GuardStatus:
    """Evaluate Part 4 calibration guards.

    When `scheduled=True`, enforces `instance_primary`, 04:15–06:00 MMT window,
    `last_sweep_date_mmt` dedupe, and `hf_daily_calibration_enabled`.
    Both scheduled and manual (`scheduled=False`) runs enforce shutdown, boot age >= 600 s,
    `maintenance_mode is False`, `in_flight_gpu_s == 0`, `count_active_voxcpm_jobs == 0`,
    and `count_queued_voxcpm_jobs == 0`.
    """
    global _mem_last_sweep_date_mmt
    now = time.time() if now_ts is None else float(now_ts)
    shut_fn = is_shutting_down_fn or _default_is_shutting_down

    if scheduled and not bool(getattr(settings, "instance_primary", True)):
        return GuardStatus(False, "not_primary_instance")

    if shut_fn():
        return GuardStatus(False, "shutting_down")

    if scheduled:
        in_win, today_mmt = is_in_quiet_window_mmt(now)
        if not in_win:
            return GuardStatus(False, "outside_quiet_window")
        with _cal_lock:
            cached_date = _mem_last_sweep_date_mmt
        if cached_date == today_mmt:
            return GuardStatus(False, "already_swept_today")
        persisted_rep = load_calibration_report()
        if isinstance(persisted_rep, Mapping) and persisted_rep.get("last_sweep_date_mmt") == today_mmt:
            with _cal_lock:
                _mem_last_sweep_date_mmt = today_mmt
            return GuardStatus(False, "already_swept_today")

    eff_boot_age = (
        float(boot_age_s)
        if boot_age_s is not None
        else get_effective_boot_age_s(now_ts=now, in_web_process=in_web_process)
    )
    if eff_boot_age < BOOT_GUARD_MIN_AGE_S:
        return GuardStatus(False, "boot_age_below_600s")

    from app.db.session import AsyncSessionLocal
    from app.services import dynamic_settings as ds_mod
    from app.services.queue import count_active_voxcpm_jobs, count_queued_voxcpm_jobs

    sess_maker = session_factory or AsyncSessionLocal
    async with sess_maker() as session:
        try:
            await ds_mod.refresh_dynamic_settings_if_changed(session)
        except Exception:
            pass
        if scheduled and not bool(getattr(settings, "hf_daily_calibration_enabled", False)):
            return GuardStatus(False, "kill_switch_disabled")
        if bool(getattr(settings, "maintenance_mode", False)):
            return GuardStatus(False, "maintenance_mode")
        st = await asyncio.to_thread(hf_ledger.load_pool_state, force=True, now_ts=now)
        if float((st or {}).get("in_flight_gpu_s") or 0.0) > 0.0:
            return GuardStatus(False, "in_flight_gpu_nonzero")
        active_cnt = await count_active_voxcpm_jobs(session)
        if active_cnt != 0:
            return GuardStatus(False, "active_voxcpm_jobs")
        queued_cnt = await count_queued_voxcpm_jobs(session)
        if queued_cnt != 0:
            return GuardStatus(False, "queued_voxcpm_jobs")

    return GuardStatus(True, "ok")


async def _execute_guarded_sweep_async(
    *,
    scheduled: bool,
    boot_age_s: float | None = None,
    in_web_process: bool = False,
    is_shutting_down_fn: Callable[[], bool] | None = None,
    session_factory: Callable[[], Any] | None = None,
    now_ts: float | None = None,
    sweep_kwargs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    guard = await check_calibration_guards(
        scheduled=scheduled,
        boot_age_s=boot_age_s,
        in_web_process=in_web_process,
        is_shutting_down_fn=is_shutting_down_fn,
        session_factory=session_factory,
        now_ts=now_ts,
    )
    if not guard.allowed:
        return {"skipped": True, "reason": guard.reason}

    loop = asyncio.get_running_loop()
    shut_fn = is_shutting_down_fn or _default_is_shutting_down
    from app.db.session import AsyncSessionLocal
    from app.services.queue import count_active_voxcpm_jobs, count_queued_voxcpm_jobs

    sess_maker = session_factory or AsyncSessionLocal

    async def _check_live_job_guards_async() -> bool:
        if shut_fn() or bool(getattr(settings, "maintenance_mode", False)):
            return False
        st = hf_ledger.load_pool_state(force=True)
        if float((st or {}).get("in_flight_gpu_s") or 0.0) > 0.0:
            return False
        async with sess_maker() as s:
            if await count_active_voxcpm_jobs(s) != 0:
                return False
            if await count_queued_voxcpm_jobs(s) != 0:
                return False
        return True

    def _per_token_guard_sync() -> bool:
        try:
            fut = asyncio.run_coroutine_threadsafe(_check_live_job_guards_async(), loop)
            return bool(fut.result(timeout=10.0))
        except Exception:
            return False

    kw = dict(sweep_kwargs or {})
    kw.setdefault("per_token_guard_fn", _per_token_guard_sync)
    kw.setdefault("mark_daily_dedupe", True)
    if now_ts is not None and "now_fn" not in kw:
        kw["now_fn"] = lambda: float(now_ts)
    return await asyncio.to_thread(run_calibration_sweep, **kw)


async def maybe_run_scheduled_calibration_sweep(
    *,
    boot_age_s: float | None = None,
    is_shutting_down_fn: Callable[[], bool] | None = None,
    session_factory: Callable[[], Any] | None = None,
    now_ts: float | None = None,
    sweep_kwargs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one scheduled calibration sweep if inside 04:15–06:00 MMT and all Part 4 guards pass."""
    return await _execute_guarded_sweep_async(
        scheduled=True,
        boot_age_s=boot_age_s,
        in_web_process=True,
        is_shutting_down_fn=is_shutting_down_fn,
        session_factory=session_factory,
        now_ts=now_ts,
        sweep_kwargs=sweep_kwargs,
    )


async def run_manual_calibration_once(
    *,
    boot_age_s: float | None = None,
    is_shutting_down_fn: Callable[[], bool] | None = None,
    session_factory: Callable[[], Any] | None = None,
    now_ts: float | None = None,
    sweep_kwargs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one manual `--once` sweep with job/boot/maintenance/shutdown guards (ignoring window & kill switch)."""
    return await _execute_guarded_sweep_async(
        scheduled=False,
        boot_age_s=boot_age_s,
        in_web_process=False,
        is_shutting_down_fn=is_shutting_down_fn,
        session_factory=session_factory,
        now_ts=now_ts,
        sweep_kwargs=sweep_kwargs,
    )


__all__ = [
    "BOOT_GUARD_MIN_AGE_S",
    "CALIBRATION_REPORT_STORE_KEY",
    "FOLLOWUP_DECLARED_GPU_S",
    "FOLLOWUP_DOOR_S",
    "GuardStatus",
    "PROBE_QUOTA_MAX_DEBIT_S",
    "REPORT_HISTORY_DAYS",
    "SWEEP_MAX_CONSECUTIVE_TRANSPORT_ERRORS",
    "SWEEP_MAX_FOLLOWUPS",
    "SWEEP_MAX_GPU_S",
    "SWEEP_MAX_WALL_S",
    "WEB_BOOT_TS_STORE_KEY",
    "check_calibration_guards",
    "current_mmt_datetime",
    "get_effective_boot_age_s",
    "get_latest_calibration_summary",
    "is_in_quiet_window_mmt",
    "load_calibration_report",
    "maybe_run_scheduled_calibration_sweep",
    "plan_calibration_sweep",
    "record_web_boot_timestamp",
    "reset_calibration_for_tests",
    "run_calibration_sweep",
    "run_manual_calibration_once",
    "select_calibration_space",
]

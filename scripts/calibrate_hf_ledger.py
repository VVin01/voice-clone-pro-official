#!/usr/bin/env python3
"""Manual runner & dry-run planner for the HF ZeroGPU ledger calibration sweep (Round 2 Part 6).

Usage:
    python scripts/calibrate_hf_ledger.py --dry-run
    python scripts/calibrate_hf_ledger.py --once

Rules:
- `--dry-run`: classifies all configured HF tokens and prints the uncertain list
  and planned execution order with ZERO probe calls and ZERO raw token material.
- `--once`: runs one calibration sweep now with the Part 4 job/boot/maintenance/shutdown
  guards (the 04:15–06:00 MMT window and `hf_daily_calibration_enabled` kill switch
  do NOT apply to manual runs).
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import signal
import sys
from typing import Any, Callable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect or run one HF ZeroGPU pool ledger calibration sweep."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Classify pool tokens and print uncertain list + planned priority order only (0 probe calls).",
    )
    mode.add_argument(
        "--once",
        action="store_true",
        help="Run one calibration sweep now subject to active-job, queued-job, in-flight, and boot guards.",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    tokens: Sequence[str] | None = None,
    boot_age_s: float | None = None,
    session_factory: Callable[[], Any] | None = None,
    sweep_kwargs: Mapping[str, Any] | None = None,
    now_ts: float | None = None,
    out_fn: Callable[[str], None] = print,
    hard_exit_fn: Callable[[int], None] = os._exit,
) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    from app.services import hf_calibration as hf_cal
    from app.services.voxcpm_engine import get_available_hf_tokens

    pool = list(tokens) if tokens is not None else get_available_hf_tokens()

    if args.dry_run:
        plan = hf_cal.plan_calibration_sweep(pool, now_ts=now_ts)
        out_fn(
            f"HF Calibration Dry-Run: pool_tokens={len(pool)} uncertain_count={len(plan)}"
        )
        for row in plan:
            acct = row.get("account") or "-"
            out_fn(
                f"  #{row['rank']:02d} slot={row['slot']:03d} account={acct} "
                f"key_id={row['key_id']} reason={row['reason']} "
                f"rem={row['remaining_s']:.1f}s base={row['base_s']:.1f}s"
            )
        return 0

    stop_flag = {"sig": False, "count": 0}

    def _on_sig(signum, _frame):
        stop_flag["count"] = int(stop_flag["count"]) + 1
        stop_flag["sig"] = True
        if stop_flag["count"] >= 2:
            hard_exit_fn(128 + int(signum))

    prev_int = signal.getsignal(signal.SIGINT)
    prev_term = signal.getsignal(signal.SIGTERM)
    try:
        signal.signal(signal.SIGINT, _on_sig)
        signal.signal(signal.SIGTERM, _on_sig)
    except Exception:
        pass

    try:
        eff_sweep_kw = dict(sweep_kwargs or {})
        if tokens is not None:
            eff_sweep_kw.setdefault("tokens", pool)
        res = asyncio.run(
            hf_cal.run_manual_calibration_once(
                boot_age_s=boot_age_s,
                is_shutting_down_fn=lambda: bool(stop_flag["sig"]) or hf_cal._default_is_shutting_down(),
                session_factory=session_factory,
                now_ts=now_ts,
                sweep_kwargs=eff_sweep_kw,
            )
        )
    finally:
        try:
            signal.signal(signal.SIGINT, prev_int)
            signal.signal(signal.SIGTERM, prev_term)
        except Exception:
            pass

    if res.get("skipped"):
        out_fn(f"SKIP calibration sweep: guard={res.get('reason')}")
        return 0

    totals = res.get("totals") or {}
    out_fn(
        f"OK calibration sweep: space={res.get('probe_space') or 'none'} "
        f"uncertain={totals.get('uncertain_count', 0)} "
        f"probed={totals.get('probed_count', 0)} "
        f"pos_drift_corrected_s={totals.get('positive_drift_corrected_s', 0.0):.1f} "
        f"neg_drift_recovered_s={totals.get('negative_drift_recovered_s', 0.0):.1f} "
        f"max_abs_drift_s={totals.get('max_abs_drift_s', 0.0):.1f} "
        f"probe_gpu_cost_s={totals.get('total_probe_gpu_cost_s', 0.0):.2f} "
        f"aborted={res.get('aborted_reason') or 'none'}"
    )
    for row in res.get("rows") or []:
        acct = row.get("account") or "-"
        out_fn(
            f"  slot={row['slot']:03d} account={acct} key_id={row['key_id']} "
            f"reason={row['reason']} {row['before_s']:.1f}s -> {row['after_s']:.1f}s "
            f"(delta={row['delta_s']:+.1f}s) outcome={row['outcome']} cost={row['gpu_cost_s']:.2f}s"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

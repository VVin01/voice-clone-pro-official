#!/usr/bin/env python3
"""Read-only Hugging Face token pool validator & account deduplicator (Part 8).

Usage:
    python scripts/probe_hf_tokens.py /path/to/candidate_tokens.txt
    cat candidate_tokens.txt | python scripts/probe_hf_tokens.py

Security & Zero-GPU rules:
- Calls ONLY `GET /api/whoami-v2` and `HfApi.get_zero_gpu_quota` (read-only, 0 GPU spent).
- Deduplicates by HF account username (two tokens minted under one account share ONE quota).
- NEVER prints, logs, or echoes raw token strings or token fragments.
- Exits non-zero (1) if any input token is invalid/unreachable or if zero valid tokens exist.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
import json
from pathlib import Path
import re
import sys
from typing import Any
import urllib.error
import urllib.request

_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
_PROBE_TIMEOUT_S = 10.0
_OVERALL_BUDGET_S = 120.0
_MAX_WORKERS = 8


def _parse_token_inputs(raw_text: str) -> list[str]:
    """Extract candidate tokens (supports newline, comma, semicolon, `#` comments)."""
    candidates: list[str] = []
    for raw_line in (raw_text or "").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" in line and not line.startswith("hf_"):
            line = line.split("=", 1)[1].strip()
        for piece in re.split(r"[,;\s]+", line):
            tok = piece.strip().strip("'\"")
            if tok:
                candidates.append(tok)
    return candidates


def _whoami(token: str) -> tuple[int | None, str | None]:
    req = urllib.request.Request(
        "https://huggingface.co/api/whoami-v2",
        headers={"User-Agent": _BROWSER_UA, "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_S) as resp:
            body = json.loads(resp.read() or b"{}")
            acct = body.get("name") if isinstance(body, dict) else None
            return int(resp.status), (str(acct) if acct else None)
    except urllib.error.HTTPError as exc:
        return int(exc.code), None
    except Exception:
        return None, None


def _quota(token: str) -> dict[str, Any]:
    from huggingface_hub import HfApi

    q = HfApi().get_zero_gpu_quota(token=token)
    return {
        "remaining_s": round(float(q.remaining), 1) if q.remaining is not None else None,
        "base_s": round(float(q.base), 1) if q.base is not None else None,
        "resets_at": q.resets_at.isoformat() if getattr(q, "resets_at", None) else None,
    }


def probe_single_token(slot_index: int, token: str) -> dict[str, Any]:
    """Probe one candidate token without ever returning or logging the token value."""
    if not str(token).startswith("hf_") or len(str(token)) < 8:
        return {
            "slot": slot_index,
            "status": "invalid_format",
            "account": None,
            "base_s": None,
            "remaining_s": None,
            "resets_at": None,
            "quota_error": None,
        }
    http_status, account_name = _whoami(token)
    if http_status == 200 and account_name:
        status = "ok"
    elif http_status in (401, 403):
        status = "auth_dead"
    else:
        status = "error"

    row: dict[str, Any] = {
        "slot": slot_index,
        "status": status,
        "account": account_name if status == "ok" else None,
        "base_s": None,
        "remaining_s": None,
        "resets_at": None,
        "quota_error": None,
    }
    if status == "ok":
        try:
            q = _quota(token)
            row["base_s"] = q["base_s"]
            row["remaining_s"] = q["remaining_s"]
            row["resets_at"] = q["resets_at"]
        except Exception as exc:
            row["quota_error"] = type(exc).__name__
    return row


def probe_and_summarize(tokens: list[str], *, probe_fn=probe_single_token) -> tuple[dict[str, Any], int]:
    """Run read-only probes, deduplicate by HF account username, and compute exit code."""
    if not tokens:
        return {
            "total_tokens": 0,
            "valid_tokens": 0,
            "invalid_tokens": 0,
            "unique_accounts": 0,
            "duplicate_tokens": 0,
            "total_base_s": 0.0,
            "total_remaining_s": 0.0,
            "accounts": [],
            "invalid_slots": [],
        }, 1

    results: dict[int, dict[str, Any]] = {}
    workers = max(1, min(_MAX_WORKERS, len(tokens)))
    ex = ThreadPoolExecutor(max_workers=workers)
    try:
        futs = {ex.submit(probe_fn, idx + 1, tok): idx + 1 for idx, tok in enumerate(tokens)}
        done, _pending = wait(futs, timeout=_OVERALL_BUDGET_S)
        for fut in done:
            slot = futs[fut]
            try:
                results[slot] = fut.result()
            except Exception as exc:
                results[slot] = {
                    "slot": slot,
                    "status": "error",
                    "account": None,
                    "base_s": None,
                    "remaining_s": None,
                    "resets_at": None,
                    "quota_error": type(exc).__name__,
                }
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    by_account: dict[str, dict[str, Any]] = {}
    invalid_slots: list[dict[str, Any]] = []
    valid_tokens = 0

    for slot in range(1, len(tokens) + 1):
        row = results.get(slot) or {"slot": slot, "status": "timeout", "account": None}
        if row.get("status") == "ok" and row.get("account"):
            valid_tokens += 1
            acct = str(row["account"])
            if acct not in by_account:
                by_account[acct] = {
                    "account": acct,
                    "token_count": 1,
                    "base_s": row.get("base_s"),
                    "remaining_s": row.get("remaining_s"),
                    "resets_at": row.get("resets_at"),
                    "quota_error": row.get("quota_error"),
                }
            else:
                by_account[acct]["token_count"] += 1
        else:
            invalid_slots.append({"slot": slot, "status": row.get("status") or "error"})

    accounts_list = sorted(by_account.values(), key=lambda r: r["account"].lower())
    duplicate_tokens = max(0, valid_tokens - len(accounts_list))
    total_base_s = round(
        sum(float(a["base_s"]) for a in accounts_list if a.get("base_s") is not None), 1
    )
    total_remaining_s = round(
        sum(float(a["remaining_s"]) for a in accounts_list if a.get("remaining_s") is not None), 1
    )
    summary = {
        "total_tokens": len(tokens),
        "valid_tokens": valid_tokens,
        "invalid_tokens": len(invalid_slots),
        "unique_accounts": len(accounts_list),
        "duplicate_tokens": duplicate_tokens,
        "total_base_s": total_base_s,
        "total_remaining_s": total_remaining_s,
        "accounts": accounts_list,
        "invalid_slots": invalid_slots,
    }
    exit_code = 0 if (len(invalid_slots) == 0 and valid_tokens > 0) else 1
    return summary, exit_code


def format_report(summary: dict[str, Any]) -> str:
    """Render human-readable report containing ONLY counts and HF usernames (zero secrets)."""
    lines = [
        "=== HF ZeroGPU Token Pool Read-Only Probe ===",
        f"Total candidate tokens : {summary['total_tokens']}",
        f"Valid tokens           : {summary['valid_tokens']}",
        f"Invalid tokens         : {summary['invalid_tokens']}",
        f"Unique HF accounts     : {summary['unique_accounts']}",
        f"Duplicate acct tokens  : {summary['duplicate_tokens']}",
        f"Total base quota       : {summary['total_base_s']:.1f} s ({summary['total_base_s'] / 60.0:.1f} min)",
        f"Total remaining quota  : {summary['total_remaining_s']:.1f} s ({summary['total_remaining_s'] / 60.0:.1f} min)",
    ]
    if summary["accounts"]:
        lines.append("")
        lines.append("Accounts (deduplicated):")
        for acct in summary["accounts"]:
            dup_note = f" [x{acct['token_count']} tokens -> 1 shared quota]" if acct["token_count"] > 1 else ""
            base_lbl = f"{acct['base_s']}s" if acct.get("base_s") is not None else "n/a"
            rem_lbl = f"{acct['remaining_s']}s" if acct.get("remaining_s") is not None else "n/a"
            err_lbl = f" (quota_error={acct['quota_error']})" if acct.get("quota_error") else ""
            lines.append(f"  - {acct['account']}: remaining={rem_lbl} / base={base_lbl}{dup_note}{err_lbl}")
    if summary["invalid_slots"]:
        lines.append("")
        lines.append("Invalid candidate positions (1-based, token values redacted):")
        for bad in summary["invalid_slots"]:
            lines.append(f"  - position #{bad['slot']}: {bad['status']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, probe_fn=probe_single_token) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    if args and args[0] != "-":
        src_path = Path(args[0])
        if not src_path.is_file():
            print(f"Error: input file not found: {src_path.name}", file=sys.stderr)
            return 2
        raw_text = src_path.read_text(encoding="utf-8")
    else:
        raw_text = sys.stdin.read()

    tokens = _parse_token_inputs(raw_text)
    summary, code = probe_and_summarize(tokens, probe_fn=probe_fn)
    print(format_report(summary))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

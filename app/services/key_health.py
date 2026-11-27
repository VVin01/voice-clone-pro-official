"""Batch BN (2026-11-06): in-app API key health for the Admin tab.

The owner asked for the twice-daily cron key-health report (HF tokens
+ ZeroGPU quota, Gemini keys, Groq keys, VoxCPM Space stages) to be
visible on demand in the Mini App's Admin tab. This module is the
in-process port of that cron checker
(``key_health_check.py`` in the audit goal's hidden files): the app
already holds every pool in its own environment, so it can check them
itself with the same semantics —

* HF: ``GET /api/whoami-v2`` per token (valid / account name), plus
  ``HfApi.get_zero_gpu_quota`` per token (read-only, spends no GPU)
  for remaining/base seconds.
* Gemini: ``GET /v1beta/models?pageSize=1`` per pool key, classified
  OK (200) / LIMIT (429) / DEAD (anything else, incl. transport
  errors).
* Groq: ``GET /openai/v1/models`` per pool key, same classes. A
  browser User-Agent is sent on every probe: Groq's edge answers the
  default urllib UA with a bot-block 403 even for valid keys (the
  2026-11-06 evening cron run proved 403 → 200 by UA alone).
* Spaces: ``GET /api/spaces/<name>`` → ``runtime.stage``.

Pools are loaded with the app's OWN loaders (``get_available_hf_tokens``
/ ``get_configured_spaces`` from the VoxCPM engine,
``collect_gemini_key_pool`` from key rotation, and the transcriber's
``GROQ_API_KEYS``/``GROQ_API_KEY`` expression) — never a private copy
of the parsing rules.

NO-LEAK DISCIPLINE (the cron checker's rule, kept verbatim): key and
token VALUES never appear in the payload, in logs, or in error
strings. The payload carries pool indexes, env-var LABELS (which
contain no key material) and provider-returned account names only.
Exceptions are recorded by TYPE NAME only — an HTTP error's string
form can embed the request URL, and the Gemini probe URL carries the
key in its query string.

CACHING: a check costs ~90 provider calls, so results are cached
~15 minutes — module memory first, with a persisted copy in the
bot_settings KV store (via key_rotation's sync store, the AJ pattern)
so a restart doesn't re-probe and the refill history (below) survives.
``force=True`` (the UI Refresh button, ``?refresh=1``) bypasses the
TTL. If a check is already in flight, concurrent callers are served
the last cached payload rather than piling on a second probe round.

REFILL ESTIMATES (BN-2): ZeroGPU quota is a rolling 24 h window —
seconds trickle back as past usage ages out; there is no fixed reset
moment for the FULL base. Each check persists a per-account
remaining-seconds snapshot; for an account below its base, the
estimate extrapolates the net recovery slope between the oldest and
newest stored points: ``eta = (base - remaining) / slope``, capped at
24 h. With fewer than two usable points (or a non-positive slope —
usage outpacing recovery) the estimate falls back to
``method="window_24h"`` (the UI renders "refills within 24 h").
Estimates are labelled with their ``method`` in the payload — they
are estimates, not provider promises. The provider's own
``resets_at`` from the quota probe rides along as
``quota_resets_at`` for reference.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 900.0  # ~15 min (BN-1)
QUOTA_SCOPE_UNSUPPORTED_ERROR = "quota_scope_unsupported"
QUOTA_SCOPE_UNSUPPORTED_TTL_SECONDS = 86400.0  # 24 hours (Round 2 Part 1)
_PROBE_TIMEOUT_S = 10.0
_OVERALL_BUDGET_S = 120.0
_MAX_WORKERS = 8
_HISTORY_LIMIT = 8
_MIN_SLOPE_SPAN_S = 60.0
_STORE_KEY = "bn_key_health_v1"
# Groq's edge bot-blocks the default urllib UA (see module docstring).
_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Probe seams (module-level so tests can monkeypatch the whole layer)
# ---------------------------------------------------------------------------

def _http_json(url: str, headers: dict | None = None, timeout: float = _PROBE_TIMEOUT_S):
    """GET JSON; returns (status|None, body dict). NEVER logs the URL:
    the Gemini probe URL embeds its key in the query string."""
    req = urllib.request.Request(url, headers={"User-Agent": _BROWSER_UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            try:
                body = json.loads(raw or b"{}")
            except Exception:
                body = {}
            return r.status, body if isinstance(body, dict) else {}
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception:
        return None, {}


def _zero_gpu_quota(token: str) -> dict:
    """One token's ZeroGPU quota (read-only; spends no GPU).

    Raises on failure — the caller records the exception TYPE only.
    """
    from huggingface_hub import HfApi

    q = HfApi().get_zero_gpu_quota(token=token)
    return {
        "remaining_s": float(q.remaining) if q.remaining is not None else None,
        "base_s": float(q.base) if q.base is not None else None,
        "resets_at": q.resets_at.isoformat() if getattr(q, "resets_at", None) else None,
        "overquota_used_s": float(q.overquota_used or 0),
    }


def _is_quota_401_403_error(exc: BaseException) -> bool:
    """True when `get_zero_gpu_quota` failed with a 401/403-class auth/scope rejection."""
    resp = getattr(exc, "response", None)
    status_code = getattr(resp, "status_code", None) or getattr(exc, "code", None)
    if status_code in (401, 403):
        return True
    msg = str(exc or "").lower()
    if any(m in msg for m in ("forbidden", "unauthorized", "insufficient scope", "quota_scope_unsupported")):
        return True
    return bool(re.search(r"\b(?:401|403)\b", msg))


def _token_digest_for_breaker(token: str) -> str:
    import hashlib

    raw = str(token or "").strip()
    if len(raw) == 64 and all(c in "0123456789abcdef" for c in raw):
        return raw
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def is_quota_scope_unsupported(token: str | None, *, now_ts: float | None = None) -> bool:
    """Round 2 Part 1: True when `token`'s digest is marked `quota_scope_unsupported` within its 24 h TTL."""
    if not token or not str(token).strip():
        return False
    now = time.time() if now_ts is None else float(now_ts)
    d_key = _token_digest_for_breaker(str(token))
    with _state_lock:
        if not _mem_quota_unsupported_loaded:
            _hydrate_quota_unsupported_locked(now)
        exp = _mem_quota_unsupported.get(d_key)
        if exp is None:
            return False
        if exp <= now:
            _mem_quota_unsupported.pop(d_key, None)
            return False
        return True


def mark_quota_scope_unsupported(
    token: str | None,
    *,
    ttl_seconds: float = QUOTA_SCOPE_UNSUPPORTED_TTL_SECONDS,
    now_ts: float | None = None,
) -> float | None:
    """Mark `token`'s SHA-256 digest `quota_scope_unsupported` for 24 h in memory + `bn_key_health_v1`."""
    if not token or not str(token).strip():
        return None
    now = time.time() if now_ts is None else float(now_ts)
    d_key = _token_digest_for_breaker(str(token))
    expires_at = round(now + max(0.0, float(ttl_seconds)), 3)
    with _state_lock:
        if not _mem_quota_unsupported_loaded:
            _hydrate_quota_unsupported_locked(now)
        _mem_quota_unsupported[d_key] = expires_at
        _persist_quota_unsupported_locked(now)
    return expires_at


def _probe_hf_account(index: int, token: str) -> dict:
    status, body = _http_json(
        "https://huggingface.co/api/whoami-v2",
        {"Authorization": f"Bearer {token}"},
    )
    if status == 200:
        acct_status = "ok"
    elif status in (401, 403):
        acct_status = "dead"
    else:
        acct_status = "error"
    row = {
        "index": index,
        "account": body.get("name") if status == 200 else None,
        "status": acct_status,
        "remaining_s": None,
        "base_s": None,
        "quota_resets_at": None,
        "quota_error": None,
        "refill": None,
    }
    if is_quota_scope_unsupported(token):
        row["quota_error"] = QUOTA_SCOPE_UNSUPPORTED_ERROR
        return row
    try:
        quota = _zero_gpu_quota(token)
        row["remaining_s"] = (
            round(quota["remaining_s"], 1) if quota["remaining_s"] is not None else None
        )
        row["base_s"] = quota["base_s"]
        row["quota_resets_at"] = quota["resets_at"]
    except Exception as e:  # type name only — never the message (no-leak)
        row["quota_error"] = type(e).__name__
        if _is_quota_401_403_error(e):
            mark_quota_scope_unsupported(token)
    return row


def _classify(status) -> str:
    if status == 200:
        return "ok"
    if status == 429:
        return "limit"
    return "dead"


def _probe_gemini(label: str, key: str) -> dict:
    # The key rides in the query string (Google's endpoint) — the URL
    # is built here and never logged or echoed anywhere.
    status, _body = _http_json(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1&key=" + key
    )
    return {"label": label, "status": _classify(status)}


def _probe_groq(index: int, key: str) -> dict:
    status, _body = _http_json(
        "https://api.groq.com/openai/v1/models",
        {"Authorization": f"Bearer {key}"},
    )
    return {"label": f"GROQ_API_KEYS[{index}]", "status": _classify(status)}


def _probe_space(name: str) -> dict:
    _status, body = _http_json(f"https://huggingface.co/api/spaces/{name}")
    rt = body.get("runtime") if isinstance(body, dict) else None
    stage = rt.get("stage") if isinstance(rt, dict) else None
    return {"name": name, "stage": stage or "UNKNOWN"}


# ---------------------------------------------------------------------------
# Pool loaders (the app's own — see module docstring)
# ---------------------------------------------------------------------------

def _load_groq_keys() -> list[str]:
    """The transcriber's expression, shared by both Whisper call sites."""
    raw = os.getenv("GROQ_API_KEYS", "") or os.getenv("GROQ_API_KEY", "")
    return [k.strip() for k in raw.split(",") if k.strip()]


def _load_pools():
    from app.services.key_rotation import collect_gemini_key_pool
    from app.services.voxcpm_engine import get_available_hf_tokens, get_configured_spaces

    return (
        get_available_hf_tokens(),
        collect_gemini_key_pool(),
        _load_groq_keys(),
        get_configured_spaces(),
    )


# ---------------------------------------------------------------------------
# Refill estimates (BN-2)
# ---------------------------------------------------------------------------

def _account_history_key(row: dict) -> str:
    return str(row.get("account") or f"idx:{row.get('index')}")


def compute_refill_estimate(history, account_key, remaining_s, base_s, now_ts) -> dict:
    """Estimated FULL-refill for one account below its base quota.

    ``history`` is the stored snapshot list (oldest first):
    ``{"ts": epoch, "remaining": {account_key: seconds}}``. The
    current point is appended virtually. Returns the payload refill
    object; ``method`` is "slope" when a positive net recovery slope
    over ≥2 points spanning ≥60 s projects the refill (capped at the
    rolling window's 24 h), else "window_24h".
    """
    fallback = {"method": "window_24h", "eta_seconds": None, "refill_at": None}
    if remaining_s is None or base_s is None or remaining_s >= base_s:
        return fallback
    points: list[tuple[float, float]] = []
    for snap in history or []:
        try:
            rem = (snap.get("remaining") or {}).get(account_key)
            if rem is not None:
                points.append((float(snap.get("ts") or 0), float(rem)))
        except Exception:
            continue
    points.append((float(now_ts), float(remaining_s)))
    points = sorted({ts: rem for ts, rem in points}.items())
    if len(points) < 2:
        return fallback
    (t0, r0), (t1, r1) = points[0], points[-1]
    span = t1 - t0
    if span < _MIN_SLOPE_SPAN_S:
        return fallback
    slope = (r1 - r0) / span  # quota seconds recovered per wall second
    if slope <= 1e-9:
        return fallback
    eta = (float(base_s) - float(remaining_s)) / slope
    eta = min(eta, 86400.0)
    return {
        "method": "slope",
        "eta_seconds": round(eta),
        "refill_at": _iso(now_ts + eta),
    }


def _overlay_space_ledger_telemetry(payload: dict, now_ts: float | None = None) -> dict:
    """Part C4 + Part Q4.3: attach live Space timeout/cooldown AND HF pool ledger metrics
    (`ledger_effective_s`, `session_ready_tokens`, `refusal_capped_tokens`, `quota_probe_errors`,
    `last_drift_s`, and per-account `ledger_remaining_s` / `refused_gate_s`) onto BOTH fresh
    and cached `/api/admin/key-health` responses (display-only, zero key material)."""
    try:
        from app.services import hf_pool_ledger as _hf_ledger

        now = time.time() if now_ts is None else float(now_ts)
        st = _hf_ledger.load_pool_state(now_ts=now)
        to_counts = _hf_ledger.get_space_timeout_counts(state=st, now_ts=now)
        spaces_prov = (payload.get("providers") or {}).get("spaces")
        if isinstance(spaces_prov, dict):
            items = spaces_prov.get("items") or []
            cooling_cnt = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                sp_name = str(item.get("name") or "")
                cd_until = _hf_ledger.get_space_cooldown_until(sp_name, state=st, now_ts=now)
                item["timeouts"] = int(to_counts.get(sp_name, 0))
                item["cooling_down"] = cd_until is not None
                item["cooldown_until"] = cd_until
                if cd_until is not None:
                    cooling_cnt += 1
            spaces_prov["cooling_down"] = cooling_cnt
            spaces_prov["space_timeouts"] = dict(to_counts)

        hf_prov = (payload.get("providers") or {}).get("hf")
        if isinstance(hf_prov, dict):
            hf_tokens, _, _, _ = _load_pools()
            accounts = hf_prov.get("accounts") or []
            tokens_map = st.get("tokens") if isinstance(st, dict) else None
            has_ledger_tokens = bool(isinstance(tokens_map, dict) and tokens_map)

            ledger_eff_total = 0.0
            session_ready_cnt = 0
            refusal_capped_cnt = 0

            for idx, acct_row in enumerate(accounts):
                if not isinstance(acct_row, dict):
                    continue
                tok = hf_tokens[idx] if idx < len(hf_tokens) else None
                lrow = None
                if tok and has_ledger_tokens:
                    _d, lrow = _hf_ledger.lookup_token_entry(st, tok)

                if lrow is not None and str(lrow.get("status") or "ok") != "auth_dead":
                    l_rem = float(lrow.get("remaining_s") if lrow.get("remaining_s") is not None else 0.0)
                    rg = _hf_ledger.get_token_refused_gate_s(lrow, now_ts=now)
                    ru = _hf_ledger.get_token_refused_until(lrow, now_ts=now)
                    acct_row["ledger_remaining_s"] = round(l_rem, 1)
                    acct_row["refused_gate_s"] = round(rg, 1) if rg is not None else None
                    acct_row["refused_until"] = ru
                    if rg is not None:
                        refusal_capped_cnt += 1
                        # Effective usable capacity under ceiling (`<= 60s` gate refusal blocks all standard calls)
                        eff_rem = (
                            min(l_rem, max(0.0, rg - 1.0))
                            if rg > _hf_ledger.DEFAULT_SPACE_GATE_S
                            else 0.0
                        )
                    else:
                        eff_rem = l_rem
                    if eff_rem >= _hf_ledger.MIN_USABLE_DUST_GATE_S:
                        ledger_eff_total += eff_rem
                    if rg is None and eff_rem >= _hf_ledger.SESSION_READY_MIN_GPU_S:
                        session_ready_cnt += 1
                elif lrow is not None and str(lrow.get("status") or "ok") == "auth_dead":
                    acct_row["ledger_remaining_s"] = 0.0
                    acct_row["refused_gate_s"] = None
                    acct_row["refused_until"] = None
                else:
                    # Fallback when pool ledger has no entry for this slot yet
                    p_rem = acct_row.get("remaining_s")
                    acct_row["ledger_remaining_s"] = round(float(p_rem), 1) if p_rem is not None else None
                    acct_row["refused_gate_s"] = None
                    acct_row["refused_until"] = None
                    if p_rem is not None and float(p_rem) >= _hf_ledger.MIN_USABLE_DUST_GATE_S:
                        ledger_eff_total += float(p_rem)
                    if p_rem is not None and float(p_rem) >= _hf_ledger.SESSION_READY_MIN_GPU_S:
                        session_ready_cnt += 1

            in_flight_s = max(0.0, float((st or {}).get("in_flight_gpu_s") or 0.0))
            drift_stats = _hf_ledger.get_drift_stats(state=st, now_ts=now) or {}
            probe_err_fallback = sum(
                1
                for r in accounts
                if isinstance(r, dict)
                and r.get("status") == "ok"
                and r.get("quota_error") != QUOTA_SCOPE_UNSUPPORTED_ERROR
                and (r.get("quota_error") is not None or r.get("remaining_s") is None)
            )
            hf_prov["ledger_effective_s"] = round(max(0.0, ledger_eff_total - in_flight_s), 1)
            hf_prov["session_ready_tokens"] = int(session_ready_cnt)
            hf_prov["refusal_capped_tokens"] = int(refusal_capped_cnt)
            hf_prov["quota_probe_errors"] = int(
                drift_stats.get("quota_probe_errors")
                if "quota_probe_errors" in drift_stats
                else probe_err_fallback
            )
            hf_prov["last_drift_s"] = round(float(drift_stats.get("positive_drift_s") or 0.0), 1)
            try:
                from app.services import hf_calibration as _hf_cal

                cal_summary = _hf_cal.get_latest_calibration_summary()
                if cal_summary is not None:
                    hf_prov["calibration"] = cal_summary
            except Exception:
                pass
    except Exception:
        logger.debug("key_health: ledger telemetry overlay skipped", exc_info=True)
    return payload


# ---------------------------------------------------------------------------
# The check itself
# ---------------------------------------------------------------------------

def run_key_health_check(history=None) -> dict:
    """Probe every pool and assemble the payload (no caching here)."""
    now_ts = time.time()
    hf_tokens, gemini_pool, groq_keys, spaces = _load_pools()

    hf_rows: dict[int, dict] = {}
    gemini_rows: dict[int, dict] = {}
    groq_rows: dict[int, dict] = {}
    space_rows: dict[int, dict] = {}

    ex = ThreadPoolExecutor(max_workers=_MAX_WORKERS)
    try:
        futs = {}
        for i, tok in enumerate(hf_tokens):
            futs[ex.submit(_probe_hf_account, i, tok)] = ("hf", i)
        for j, (label, key) in enumerate(gemini_pool):
            futs[ex.submit(_probe_gemini, label, key)] = ("gemini", j)
        for j, key in enumerate(groq_keys):
            futs[ex.submit(_probe_groq, j, key)] = ("groq", j)
        for j, name in enumerate(spaces):
            futs[ex.submit(_probe_space, name)] = ("space", j)
        done, _pending = wait(futs, timeout=_OVERALL_BUDGET_S)
        for fut in done:
            kind, idx = futs[fut]
            try:
                row = fut.result()
            except Exception as e:  # a probe seam itself blew up
                row = {"_error": type(e).__name__}
            if kind == "hf":
                hf_rows[idx] = row
            elif kind == "gemini":
                gemini_rows[idx] = row
            elif kind == "groq":
                groq_rows[idx] = row
            else:
                space_rows[idx] = row
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    accounts = []
    for i in range(len(hf_tokens)):
        row = hf_rows.get(i) or {
            "index": i, "account": None, "status": "error",
            "remaining_s": None, "base_s": None, "quota_resets_at": None,
            "quota_error": "ProbeTimeout", "refill": None,
        }
        row = {k: row.get(k) for k in (
            "index", "account", "status", "remaining_s", "base_s",
            "quota_resets_at", "quota_error", "refill")}
        row["index"] = i
        if row["status"] not in ("ok", "dead", "error"):
            row["status"] = "error"
        if row["remaining_s"] is not None and row["base_s"] is not None \
                and row["remaining_s"] < row["base_s"]:
            row["refill"] = compute_refill_estimate(
                history, _account_history_key(row),
                row["remaining_s"], row["base_s"], now_ts)
        accounts.append(row)

    # Phase 1 Part 5: reconcile the shared rolling-seconds ledger
    # (`aj_hf:pool_state`) from this probe pass and surface quarantined
    # `auth_dead` entries in Admin Key Health without exposing raw tokens.
    auth_dead_count = 0
    try:
        from app.services import hf_pool_ledger as _hf_ledger

        reconciled = _hf_ledger.reconcile_from_probe_rows(hf_tokens, accounts, now_ts=now_ts)
        for i, tok in enumerate(hf_tokens):
            _d, lrow = _hf_ledger.lookup_token_entry(reconciled, tok)
            if lrow and lrow.get("status") == "auth_dead":
                accounts[i]["quarantined"] = True
                accounts[i]["quarantined_until"] = lrow.get("cooldown_until") or None
                auth_dead_count += 1
            else:
                accounts[i]["quarantined"] = False
                accounts[i]["quarantined_until"] = None
    except Exception:
        logger.debug("key_health: ledger reconciliation skipped", exc_info=True)

    def _key_row(raw, label):
        if raw and raw.get("status") in ("ok", "limit", "dead"):
            return {"label": raw.get("label") or label, "status": raw["status"]}
        return {"label": label, "status": "dead"}

    gemini_keys = [
        _key_row(gemini_rows.get(j), label)
        for j, (label, _k) in enumerate(gemini_pool)
    ]
    groq_list = [
        _key_row(groq_rows.get(j), f"GROQ_API_KEYS[{j}]")
        for j in range(len(groq_keys))
    ]
    space_items = [
        space_rows.get(j) or {"name": name, "stage": "UNKNOWN"}
        for j, name in enumerate(spaces)
    ]

    def _counts(rows, kinds):
        return {k: sum(1 for r in rows if r["status"] == k) for k in kinds}

    hf_counts = _counts(accounts, ("ok", "dead", "error"))
    payload = {
        "checked_at": _iso(now_ts),
        "cached": False,
        "providers": {
            "hf": {
                "total": len(accounts),
                **hf_counts,
                "auth_dead": auth_dead_count,
                "quota_remaining_s": round(
                    sum(r["remaining_s"] for r in accounts if r["remaining_s"] is not None), 1),
                "quota_total_s": round(
                    sum(r["base_s"] for r in accounts if r["base_s"] is not None), 1),
                "quota_exhausted": sum(
                    1 for r in accounts
                    if r["remaining_s"] is not None and r["remaining_s"] <= 0),
                "accounts": accounts,
            },
            "gemini": {
                "total": len(gemini_keys),
                **_counts(gemini_keys, ("ok", "limit", "dead")),
                "keys": gemini_keys,
            },
            "groq": {
                "total": len(groq_list),
                **_counts(groq_list, ("ok", "limit", "dead")),
                "keys": groq_list,
            },
            "spaces": {
                "total": len(space_items),
                "running": sum(1 for s in space_items if s["stage"] == "RUNNING"),
                "items": space_items,
            },
        },
    }
    _overlay_space_ledger_telemetry(payload, now_ts=now_ts)
    logger.info(
        "key_health: check complete — HF %s/%s ok, Gemini %s/%s ok, Groq %s/%s ok, Spaces %s/%s running",
        hf_counts["ok"], len(accounts),
        payload["providers"]["gemini"]["ok"], len(gemini_keys),
        payload["providers"]["groq"]["ok"], len(groq_list),
        payload["providers"]["spaces"]["running"], len(space_items),
    )
    return payload


# ---------------------------------------------------------------------------
# Cache (memory + bot_settings persisted copy) and history
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
_check_lock = threading.Lock()
_mem_payload: dict | None = None
_mem_ts: float = 0.0
_mem_history: list = []
_mem_quota_unsupported: dict[str, float] = {}
_mem_quota_unsupported_loaded: bool = False


def _clean_quota_unsupported_map(raw: object, now_ts: float) -> dict[str, float]:
    out: dict[str, float] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        try:
            exp = float(v)
        except (TypeError, ValueError):
            continue
        if exp > now_ts:
            out[str(k)] = round(exp, 3)
    return out


def _hydrate_quota_unsupported_locked(now_ts: float) -> None:
    global _mem_quota_unsupported_loaded
    _mem_quota_unsupported_loaded = True
    try:
        from app.services.key_rotation import botsetting_sync_read

        raw = botsetting_sync_read(_STORE_KEY)
        if not raw:
            return
        data = json.loads(raw)
        if isinstance(data, dict):
            loaded = _clean_quota_unsupported_map(data.get("quota_scope_unsupported"), now_ts)
            for d_key, exp in loaded.items():
                prev = _mem_quota_unsupported.get(d_key, 0.0)
                if exp > prev:
                    _mem_quota_unsupported[d_key] = exp
    except Exception:
        pass


def _persist_quota_unsupported_locked(now_ts: float) -> None:
    try:
        from app.services import key_rotation as _kr

        active_map = _clean_quota_unsupported_map(_mem_quota_unsupported, now_ts)
        _mem_quota_unsupported.clear()
        _mem_quota_unsupported.update(active_map)

        raw = _kr.botsetting_sync_read(_STORE_KEY)
        doc: dict = {}
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    doc = parsed
            except Exception:
                doc = {}
        if not doc:
            doc = {"v": 1}
        existing_map = _clean_quota_unsupported_map(doc.get("quota_scope_unsupported"), now_ts)
        for k, exp in active_map.items():
            if exp > existing_map.get(k, 0.0):
                existing_map[k] = exp
        doc["quota_scope_unsupported"] = existing_map
        _mem_quota_unsupported.update(existing_map)
        _kr.botsetting_sync_write(_STORE_KEY, json.dumps(doc))
    except Exception:
        pass


def _load_persisted():
    """(payload, checked_ts, history) from the KV store, or None."""
    global _mem_quota_unsupported_loaded
    try:
        from app.services.key_rotation import botsetting_sync_read

        raw = botsetting_sync_read(_STORE_KEY)
        if not raw:
            return None
        data = json.loads(raw)
        if isinstance(data, dict):
            loaded_qu = _clean_quota_unsupported_map(data.get("quota_scope_unsupported"), time.time())
            for k, exp in loaded_qu.items():
                if exp > _mem_quota_unsupported.get(k, 0.0):
                    _mem_quota_unsupported[k] = exp
            _mem_quota_unsupported_loaded = True
        payload = data.get("payload")
        if not isinstance(payload, dict) or "providers" not in payload:
            return None
        history = data.get("history")
        return payload, float(data.get("checked_ts") or 0), \
            history if isinstance(history, list) else []
    except Exception:
        return None


def _persist(payload: dict, checked_ts: float, history: list) -> None:
    try:
        from app.services import key_rotation as _kr

        now = time.time()
        with _state_lock:
            qu_map = _clean_quota_unsupported_map(_mem_quota_unsupported, now)
        raw_prev = _kr.botsetting_sync_read(_STORE_KEY)
        if raw_prev:
            try:
                prev_doc = json.loads(raw_prev)
                if isinstance(prev_doc, dict):
                    for k, exp in _clean_quota_unsupported_map(prev_doc.get("quota_scope_unsupported"), now).items():
                        if exp > qu_map.get(k, 0.0):
                            qu_map[k] = exp
            except Exception:
                pass
        doc: dict = {
            "v": 1,
            "checked_ts": checked_ts,
            "payload": payload,
            "history": history,
        }
        if qu_map:
            doc["quota_scope_unsupported"] = qu_map
        _kr.botsetting_sync_write(_STORE_KEY, json.dumps(doc))
    except Exception:
        pass  # persistence is best-effort (the AJ pattern)


def get_cached_hf_account_names() -> dict[int, str]:
    """Return `{slot_index: account_name}` from cached/persisted Key Health without probing."""
    with _state_lock:
        payload = _mem_payload
    if payload is None:
        persisted = _load_persisted()
        if persisted is not None:
            payload = persisted[0]
    if not isinstance(payload, dict):
        return {}
    accounts = ((payload.get("providers") or {}).get("hf") or {}).get("accounts") or []
    out: dict[int, str] = {}
    for row in accounts:
        if isinstance(row, dict) and row.get("account"):
            try:
                out[int(row["index"])] = str(row["account"])
            except (KeyError, TypeError, ValueError):
                continue
    return out


def _snapshot_of(payload: dict, ts: float) -> dict:
    remaining = {}
    for row in payload["providers"]["hf"]["accounts"]:
        if row.get("remaining_s") is not None:
            remaining[_account_history_key(row)] = row["remaining_s"]
    return {"ts": ts, "remaining": remaining}


def get_key_health(force_refresh: bool = False) -> dict:
    """Serve the cached payload when fresh; otherwise run a check.

    The returned dict is a copy whose ``cached`` flag reflects how it
    was served; the stored payload itself keeps ``cached=False``.
    """
    global _mem_payload, _mem_ts, _mem_history
    now = time.time()
    with _state_lock:
        if _mem_payload is None:
            persisted = _load_persisted()
            if persisted is not None:
                _mem_payload, _mem_ts, _mem_history = persisted
        if not force_refresh and _mem_payload is not None \
                and now - _mem_ts < CACHE_TTL_SECONDS:
            out = copy.deepcopy(_mem_payload)
            out["cached"] = True
            return _overlay_space_ledger_telemetry(out, now_ts=now)
        history = list(_mem_history)

    if not _check_lock.acquire(blocking=False):
        # A check is already running: serve whatever cache exists
        # rather than starting a second ~90-call probe round.
        with _state_lock:
            if _mem_payload is not None:
                out = copy.deepcopy(_mem_payload)
                out["cached"] = True
                return _overlay_space_ledger_telemetry(out)
        _check_lock.acquire()  # no cache at all — wait for the checker
        with _state_lock:  # the in-flight check may have just landed
            if _mem_payload is not None and time.time() - _mem_ts < CACHE_TTL_SECONDS:
                out = copy.deepcopy(_mem_payload)
                out["cached"] = True
                _check_lock.release()
                return _overlay_space_ledger_telemetry(out)
            history = list(_mem_history)
    try:
        payload = run_key_health_check(history=history)
        checked_ts = time.time()
        history = (history + [_snapshot_of(payload, checked_ts)])[-_HISTORY_LIMIT:]
        with _state_lock:
            _mem_payload, _mem_ts, _mem_history = payload, checked_ts, history
        _persist(payload, checked_ts, history)
        return copy.deepcopy(payload)
    finally:
        _check_lock.release()


def _reset_cache_for_tests(*, clear_quota_breaker: bool = True) -> None:
    global _mem_payload, _mem_ts, _mem_history, _mem_quota_unsupported_loaded
    with _state_lock:
        _mem_payload, _mem_ts, _mem_history = None, 0.0, []
        _mem_quota_unsupported_loaded = False
        if clear_quota_breaker:
            _mem_quota_unsupported.clear()


__all__ = [
    "CACHE_TTL_SECONDS",
    "QUOTA_SCOPE_UNSUPPORTED_ERROR",
    "QUOTA_SCOPE_UNSUPPORTED_TTL_SECONDS",
    "compute_refill_estimate",
    "get_cached_hf_account_names",
    "get_key_health",
    "is_quota_scope_unsupported",
    "mark_quota_scope_unsupported",
    "run_key_health_check",
]

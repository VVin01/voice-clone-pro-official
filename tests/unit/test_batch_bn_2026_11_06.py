"""Batch BN (2026-11-06): Admin-tab Key Health (owner request).

The twice-daily cron key-health report moves into the Mini App: the
app checks its OWN provider pools in-process (a port of the cron
checker) and serves them to the Admin tab.

1. BN-1 — app/services/key_health.py + GET /api/admin/key-health
   (require_admin): HF whoami + ZeroGPU quota per token, Gemini /
   Groq per-key classes, Space stages; ~15 min server-side cache
   (memory + bot_settings persisted copy); ?refresh=1 forces.
2. BN-2 — refill estimates from the rolling-24 h window: per-account
   remaining-seconds snapshots persist across checks; a below-base
   account gets a slope estimate (method="slope") or the honest
   fallback (method="window_24h") when history is insufficient.
3. BN-3 — Admin tab "keys" section (nav + screen + loader), all
   strings via i18n keys in both my + en.
4. BN-4 — these tests: admin gate, payload shape, cache behaviour
   (no re-probe inside the TTL; refresh re-probes; persisted copy
   serves after a memory reset), the NO-LEAK pin (sentinel key
   values appear NOWHERE in the serialized payload), and the
   refill-estimate math.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
from pathlib import Path

import pytest
from fastapi import HTTPException

import app.webapp.api as api_mod
from app.config import settings
from app.models.schema import User
from app.services import key_health as kh
from app.services import key_rotation

ROOT = Path(__file__).resolve().parents[2]
ADMIN_ID = 999555
TOKEN = "batch-bn-test-token"

HF_SENTINELS = ["hf_SENTINELAAA0", "hf_SENTINELBBB1", "hf_SENTINELCCC2"]
GEM_SENTINEL = "AIzaSENTINELGEMINI"
GROQ_SENTINELS = ["gsk_SENTINELG0", "gsk_SENTINELG1"]


def _signed_init_data(fields, token=TOKEN):
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode({**fields, "hash": digest})


def _fields(uid):
    return {
        "user": json.dumps({"id": uid, "first_name": "T"}),
        "query_id": "AAE",
        "auth_date": str(int(time.time())),
    }


def _user(uid: int) -> User:
    return User(id=uid, first_name="T", language_code="my")


@pytest.fixture(autouse=True)
def _bn_env(monkeypatch):
    """Isolate the module cache + give the persisted copy a fake,
    test-local store (the real sync store must not leak state
    between tests or touch a real database)."""
    kh._reset_cache_for_tests()
    store: dict[str, str] = {}
    monkeypatch.setattr(key_rotation, "botsetting_sync_read", lambda k: store.get(k))
    monkeypatch.setattr(
        key_rotation, "botsetting_sync_write",
        lambda k, v: store.__setitem__(k, v) or True,
    )
    monkeypatch.setattr(settings, "admin_ids_raw", str(ADMIN_ID))
    monkeypatch.setattr(settings, "bot_token", TOKEN)
    yield store
    kh._reset_cache_for_tests()


@pytest.fixture()
def probes(monkeypatch):
    """Fake probe seams with call counters. Quota profile: account 0
    partial (120/300), account 1 exhausted (0/300), account 2 full."""
    calls = {"http": 0, "quota": 0}

    def fake_http(url, headers=None, timeout=10.0):
        calls["http"] += 1
        if "whoami-v2" in url:
            auth = (headers or {}).get("Authorization", "")
            idx = auth[-1] if auth else "?"
            return 200, {"name": f"sentinel-acct-{idx}", "type": "user"}
        if "generativelanguage" in url:
            return 200, {}
        if "api.groq.com" in url:
            return 200, {}
        if "api/spaces/" in url:
            return 200, {"runtime": {"stage": "RUNNING"}}
        return None, {}

    def fake_quota(token):
        calls["quota"] += 1
        remaining = {"0": 120.0, "1": 0.0, "2": 300.0}[token[-1]]
        return {"remaining_s": remaining, "base_s": 300.0,
                "resets_at": None, "overquota_used_s": 0.0}

    monkeypatch.setattr(kh, "_http_json", fake_http)
    monkeypatch.setattr(kh, "_zero_gpu_quota", fake_quota)
    monkeypatch.setattr(kh, "_load_pools", lambda: (
        list(HF_SENTINELS),
        [("GEMINI_API_KEY", GEM_SENTINEL)],
        list(GROQ_SENTINELS),
        ["sentinel/space-a", "sentinel/space-b"],
    ))
    return calls


# ── Token ────────────────────────────────────────────────────────────

def test_bn_cache_token_is_r86():
    html = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
    assert html.count("20261104_r125") == 4
    assert "20261104_r85" not in html


# ── BN-4: the endpoint is admin-gated ────────────────────────────────

def test_bn_route_registered_with_require_admin():
    routes = [r for r in api_mod.api_router.routes if r.path == "/api/admin/key-health"]
    assert len(routes) == 1
    deps = [d.call for d in routes[0].dependant.dependencies]
    assert api_mod.require_admin in deps


def test_bn_require_admin_rejects_non_admin_accepts_admin():
    plain = _user(424242)
    with pytest.raises(HTTPException) as exc:
        api_mod.require_admin(
            x_telegram_init_data=_signed_init_data(_fields(424242)), user=plain)
    assert exc.value.status_code == 403

    admin = _user(ADMIN_ID)
    assert api_mod.require_admin(
        x_telegram_init_data=_signed_init_data(_fields(ADMIN_ID)), user=admin) is admin


@pytest.mark.asyncio
async def test_bn_endpoint_returns_payload_for_admin(probes):
    res = await api_mod.get_admin_key_health(0, _user(ADMIN_ID))
    assert res["cached"] is False
    assert "checked_at" in res
    hf = res["providers"]["hf"]
    assert hf["total"] == 3 and hf["ok"] == 3
    assert hf["quota_exhausted"] == 1
    assert [a["account"] for a in hf["accounts"]] == [
        "sentinel-acct-0", "sentinel-acct-1", "sentinel-acct-2"]
    assert res["providers"]["gemini"] == {
        "total": 1, "ok": 1, "limit": 0, "dead": 0,
        "keys": [{"label": "GEMINI_API_KEY", "status": "ok"}]}
    assert res["providers"]["groq"]["ok"] == 2
    assert res["providers"]["spaces"]["running"] == 2


# ── BN-4: cache behaviour ────────────────────────────────────────────

def test_bn_second_call_within_ttl_does_not_reprobe(probes):
    first = kh.get_key_health()
    assert first["cached"] is False
    http_after_first = probes["http"]
    assert http_after_first > 0

    second = kh.get_key_health()
    assert second["cached"] is True
    assert probes["http"] == http_after_first  # no new probe round


def test_bn_refresh_forces_reprobe(probes):
    kh.get_key_health()
    http_after_first = probes["http"]
    forced = kh.get_key_health(force_refresh=True)
    assert forced["cached"] is False
    assert probes["http"] > http_after_first


def test_bn_persisted_copy_serves_after_memory_reset(probes):
    kh.get_key_health()
    http_after_first = probes["http"]
    kh._reset_cache_for_tests()  # memory gone; the KV copy remains
    served = kh.get_key_health()
    assert served["cached"] is True
    assert probes["http"] == http_after_first


# ── BN-4: NO-LEAK ────────────────────────────────────────────────────

def test_bn_no_key_material_anywhere_in_payload(monkeypatch):
    """Real env loaders + fake network seams: the sentinel key values
    flow through the whole service and must not surface anywhere in
    the serialized payload (nor a prefix of them)."""
    monkeypatch.setenv("HF_TOKENS", ",".join(HF_SENTINELS))
    monkeypatch.setenv("GEMINI_API_KEY", GEM_SENTINEL)
    monkeypatch.setenv("GROQ_API_KEYS", ",".join(GROQ_SENTINELS))
    monkeypatch.setenv("VOXCPM_SPACES", "sentinel/space-a")

    def fake_http(url, headers=None, timeout=10.0):
        if "whoami-v2" in url:
            return 200, {"name": "leakcheck-acct", "type": "user"}
        if "api/spaces/" in url:
            return 200, {"runtime": {"stage": "RUNNING"}}
        return 200, {}

    monkeypatch.setattr(kh, "_http_json", fake_http)
    monkeypatch.setattr(kh, "_zero_gpu_quota", lambda token: {
        "remaining_s": 300.0, "base_s": 300.0,
        "resets_at": None, "overquota_used_s": 0.0})

    payload = kh.get_key_health(force_refresh=True)
    blob = json.dumps(payload, ensure_ascii=False)
    for secret in HF_SENTINELS + [GEM_SENTINEL] + GROQ_SENTINELS:
        assert secret not in blob
        assert secret[:12] not in blob  # not even a prefix
    assert "SENTINEL" not in blob
    # …while the non-secret facts ARE present.
    assert payload["providers"]["hf"]["total"] >= 3
    assert payload["providers"]["hf"]["accounts"][0]["account"] == "leakcheck-acct"


def test_bn_gemini_429_is_limit_and_groq_403_is_dead(monkeypatch, probes):
    def http2(url, headers=None, timeout=10.0):
        if "generativelanguage" in url:
            return 429, {}
        if "api.groq.com" in url:
            return 403, {}
        return probes_http(url, headers, timeout)

    probes_http = kh._http_json  # the fixture's fake, for other URLs
    monkeypatch.setattr(kh, "_http_json", http2)
    res = kh.run_key_health_check()
    assert res["providers"]["gemini"]["limit"] == 1
    assert res["providers"]["groq"]["dead"] == 2


# ── BN-2 / BN-4: refill-estimate math ────────────────────────────────

def test_bn_estimate_slope_from_two_points():
    now = 1_700_000_000.0
    history = [{"ts": now - 3600, "remaining": {"acct": 100.0}}]
    est = kh.compute_refill_estimate(history, "acct", 160.0, 300.0, now)
    # Net recovery 60 s over 3600 s → (300-160) / (60/3600) = 8400 s.
    assert est["method"] == "slope"
    assert est["eta_seconds"] == 8400
    assert est["refill_at"] == kh._iso(now + 8400)


def test_bn_estimate_single_point_falls_back_to_window():
    now = 1_700_000_000.0
    est = kh.compute_refill_estimate([], "acct", 160.0, 300.0, now)
    assert est == {"method": "window_24h", "eta_seconds": None, "refill_at": None}


def test_bn_estimate_flat_slope_falls_back_to_window():
    now = 1_700_000_000.0
    history = [{"ts": now - 3600, "remaining": {"acct": 160.0}}]
    est = kh.compute_refill_estimate(history, "acct", 160.0, 300.0, now)
    assert est["method"] == "window_24h"


def test_bn_estimate_eta_capped_at_24h():
    now = 1_700_000_000.0
    # Slope of 1 s per 3600 s against a 299 s deficit → way past 24 h.
    history = [{"ts": now - 3600, "remaining": {"acct": 0.0}}]
    est = kh.compute_refill_estimate(history, "acct", 1.0, 300.0, now)
    assert est["method"] == "slope"
    assert est["eta_seconds"] == 86400


def test_bn_estimate_attached_only_below_base(probes):
    payload = kh.run_key_health_check(history=[
        {"ts": time.time() - 3600,
         "remaining": {"sentinel-acct-0": 60.0, "sentinel-acct-1": 0.0}},
    ])
    accounts = payload["providers"]["hf"]["accounts"]
    assert accounts[0]["refill"]["method"] == "slope"       # 60 → 120 rising
    assert accounts[1]["refill"]["method"] == "window_24h"  # flat at 0
    assert accounts[2]["refill"] is None                    # full


# ── BN-3: frontend wiring ────────────────────────────────────────────

def test_bn_frontend_section_loader_and_i18n():
    html = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
    assert 'id="adm-screen-keys"' in html
    assert 'data-adm-section="keys"' in html
    assert 'data-i18n="adm_nav_keys"' in html
    assert 'data-i18n="adm_keys_title"' in html

    js = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
    assert '"/api/admin/key-health"' in js
    assert '"keys"' in js.split("ADM_SECTIONS = [", 1)[1].split("]", 1)[0]
    assert "loadAdminKeyHealth" in js

    i18n = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
    for key in (
        "adm_nav_keys", "adm_keys_title", "adm_keys_checked", "adm_keys_cached",
        "adm_keys_checking", "adm_keys_hf", "adm_keys_gemini", "adm_keys_groq",
        "adm_keys_spaces", "adm_keys_working", "adm_keys_limit", "adm_keys_dead",
        "adm_keys_error", "adm_keys_quota_left", "adm_keys_exhausted",
        "adm_keys_accounts_show", "adm_keys_accounts_hide", "adm_keys_remaining",
        "adm_keys_refill", "adm_keys_refill_24h", "adm_keys_est",
        "adm_keys_full", "adm_keys_unit_s",
    ):
        # Once in the my dict, once in the en dict.
        assert i18n.count(f"{key}:") == 2, key

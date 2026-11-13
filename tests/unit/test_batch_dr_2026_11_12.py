"""Batch DR (2026-11-12): paid-tier VoxCPM walk — longer budget,
reserve-first, wider park trigger.

Evidence (production, 2026-11-11 15:32–15:46Z): post-DP, paid/admin
recap segments STILL fell back to Edge (VIP job rc_1794410974_2e91a6
— 6 segments; admin job rc_1794411615_03ac5a — 10 segments) and DP
recorded ZERO parks. The dominant failure class was the Batch CY
per-segment failover-budget exhaustion (45 s for short lines):
evening ZeroGPU congestion made attempts slow, token balances were
fragmented (75–94 s left vs 94–180 s admission asks), and the walk
died on the clock before reaching an admittable pair — the DP
reserve pair included, because DP sorted the reserve AFTER shared
tokens within a health class.

DR answers on three levels:
  1. core.vox_failover_budget gains a keyword-only ``paid`` flag:
     paid segments use a 120 s floor / 300 s cap (same base 25.0
     and slope 2.5); free results are byte-identical.
  2. The paid walk leads with the reserve INSIDE each health class
     (health class still dominates; balance order still applies
     within each class). Free walks are untouched.
  3. A paid walk CUT BY the failover-budget deadline after at least
     one quota-class refusal now raises VoxCPMQuotaExhaustedError
     (park-eligible) instead of the generic RuntimeError. Pure
     congestion with zero quota refusals still raises RuntimeError
     in both tiers, and the free tail is unchanged.

All clients are fakes — no network, synthetic tokens only.
"""
from __future__ import annotations

import time
import wave
from pathlib import Path

import pytest

import app.services.recap_engine.core as core
from app.services import voxcpm_engine as vx
from app.services.voxcpm_engine import VoxCPMEngine, VoxCPMQuotaExhaustedError

REPO = Path(__file__).resolve().parents[2]


# ── Cache token ─────────────────────────────────────────────────────

def test_dr_cache_token_is_r140():
    html = (REPO / "app/webapp/static/index.html").read_text(encoding="utf-8")
    assert html.count("20261113_r142") == 4
    assert "r139" not in html


# ── 1: tier-aware failover budget ───────────────────────────────────

def test_dr_budget_constants():
    assert core.VOX_FAILOVER_BUDGET_PAID_FLOOR_SECONDS == 120.0
    assert core.VOX_FAILOVER_BUDGET_PAID_CAP_SECONDS == 300.0
    # Free constants untouched.
    assert core.VOX_FAILOVER_BUDGET_FLOOR_SECONDS == 45.0
    assert core.VOX_FAILOVER_BUDGET_CAP_SECONDS == 150.0


def test_dr_budget_free_values_unchanged():
    # The exact values the AL/CY tests pin, through both the default
    # call and the explicit free flag.
    for paid in (None, False):
        kw = {} if paid is None else {"paid": paid}
        assert core.vox_failover_budget(0.2, 5, **kw) == 45.0      # floor
        assert core.vox_failover_budget(30.0, 620, **kw) == 100.0  # slope
        assert core.vox_failover_budget(60.0, 900, **kw) == 150.0  # cap
        assert core.vox_failover_budget(600.0, 12000, **kw) == 150.0


def test_dr_budget_paid_values():
    # Short line: the paid floor replaces the 45 s free floor.
    assert core.vox_failover_budget(0.2, 5, paid=True) == 120.0
    # Mid case on the shared slope, between the paid floor and cap:
    # expected_audio = max(70, 0) = 70 → 25 + 2.5*70 = 200.
    assert core.vox_failover_budget(70.0, 0, paid=True) == 200.0
    # The free cap case rides the slope further under the paid cap:
    # expected_audio = max(60, 900/21) = 60 → 25 + 150 = 175.
    assert core.vox_failover_budget(60.0, 900, paid=True) == 175.0
    # Very long: the paid cap.
    assert core.vox_failover_budget(600.0, 12000, paid=True) == 300.0


def test_dr_budget_paid_is_keyword_only():
    with pytest.raises(TypeError):
        core.vox_failover_budget(0.2, 5, True)  # noqa: FBT003 — pinning the signature


def test_dr_budget_call_site_passes_paid_tier():
    src = Path(core.__file__).read_text(encoding="utf-8")
    assert "paid=self.paid_tier" in src


# ── Walk harness (DP/DI-style fake clients) ─────────────────────────

@pytest.fixture(autouse=True)
def _dr_engine_state(monkeypatch):
    monkeypatch.setattr(VoxCPMEngine, "_token_balances", {})
    monkeypatch.setattr(VoxCPMEngine, "_token_cooldowns", {})
    monkeypatch.setattr(VoxCPMEngine, "_last_admission_price", None)
    monkeypatch.delenv("HF_RESERVE_TOKENS", raising=False)
    yield


def _pool(n, prefix="hf_t"):
    return [f"{prefix}{i}" for i in range(n)]


def _write_wav(path: Path, seconds: float = 0.3, rate: int = 24000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


class _FakeJob:
    def __init__(self, result_path):
        self._result_path = result_path

    def done(self):
        return True

    def status(self):
        return None

    def result(self):
        return str(self._result_path)


class _WalkClient:
    """Per-(space, token) behaviour: "ok" serves; "gate:<left>"
    refuses at the admission gate with the production quota text;
    "slow:<secs>" sleeps, then fails with a generic (non-quota)
    error — the evening congestion shape."""

    def __init__(self, behaviour, result_path, record, key):
        self.behaviour = behaviour
        self._result_path = result_path
        self.record = record
        self.key = key

    def submit(self, **kwargs):
        self.record.append(self.key)
        b = self.behaviour
        if isinstance(b, str) and b.startswith("gate:"):
            left = b.split(":", 1)[1]
            raise RuntimeError(
                "AppError: You have exceeded your free ZeroGPU quota "
                f"(94s requested vs. {left}s left)"
            )
        if isinstance(b, str) and b.startswith("slow:"):
            time.sleep(float(b.split(":", 1)[1]))
            raise RuntimeError("synthetic slow space failure (congestion)")
        return _FakeJob(self._result_path)


def _engine(tmp_path, monkeypatch, tokens, spaces=("spaceA",), logs=None):
    monkeypatch.setattr(vx, "get_available_hf_tokens", lambda: list(tokens))
    monkeypatch.setattr(VoxCPMEngine, "_token_rr_counter", 0)
    monkeypatch.setattr(VoxCPMEngine, "_token_bind_rr_counters", {})
    monkeypatch.setattr(VoxCPMEngine, "_space_admission_prices", {})
    engine = VoxCPMEngine(
        work_dir=tmp_path / "cache",
        log_fn=logs.append if logs is not None else None,
    )
    engine.spaces = list(spaces)
    engine.hf_tokens = list(tokens)
    return engine


def _patch_clients(monkeypatch, behaviours, result_path, record):
    clients = {
        key: _WalkClient(behaviour, result_path, record, key)
        for key, behaviour in behaviours.items()
    }

    def _get_client(cls, space_name, hf_token=None):
        return clients[(space_name, hf_token)]

    monkeypatch.setattr(VoxCPMEngine, "get_client", classmethod(_get_client))


def _behaviours(tokens, default="ok", **overrides):
    out = {("spaceA", tok): default for tok in tokens}
    out[("spaceA", None)] = default
    for tok, behaviour in overrides.items():
        out[("spaceA", tok)] = behaviour
    return out


# ── 2: reserve-first paid walk ──────────────────────────────────────

def test_dr_paid_walk_attempts_reserve_before_shared(tmp_path, monkeypatch):
    toks = _pool(8)  # positional reserve = last 2 (hf_t6, hf_t7)
    engine = _engine(tmp_path, monkeypatch, toks)
    record: list = []
    # The first reserve token refuses at the gate; the walk must
    # stay inside the reserve class (hf_t7 next) and succeed there —
    # shared tokens are never attempted.
    behaviours = _behaviours(toks, hf_t6="gate:5")
    _patch_clients(monkeypatch, behaviours, _write_wav(tmp_path / "ok.wav"), record)
    out = engine.synthesize(
        text="မင်္ဂလာပါ", output_wav=tmp_path / "o.wav",
        allow_fallback=False, tier="paid",
    )
    assert out.is_file()
    assert record == [("spaceA", "hf_t6"), ("spaceA", "hf_t7")]


def test_dr_paid_walk_designated_reserve_leads(tmp_path, monkeypatch):
    # HF_RESERVE_TOKENS pattern (DP): a designated token is reserve
    # even mid-pool in a pool too small for a positional slice.
    monkeypatch.setenv("HF_RESERVE_TOKENS", "hf_pro")
    toks = ["hf_a", "hf_b", "hf_pro", "hf_c"]
    engine = _engine(tmp_path, monkeypatch, toks)
    record: list = []
    _patch_clients(monkeypatch, _behaviours(toks), _write_wav(tmp_path / "ok.wav"), record)
    engine.synthesize(
        text="မင်္ဂလာပါ", output_wav=tmp_path / "o.wav",
        allow_fallback=False, tier="paid",
    )
    assert record[0] == ("spaceA", "hf_pro")


def test_dr_paid_walk_health_class_still_dominates(tmp_path, monkeypatch):
    # A COOLING reserve token (health class 2) must not jump ahead
    # of healthy shared tokens (health class 0): the reserve class
    # only orders WITHIN a health class.
    toks = _pool(8)
    engine = _engine(tmp_path, monkeypatch, toks)
    now = time.time()
    monkeypatch.setattr(
        VoxCPMEngine, "_token_cooldowns",
        {"hf_t6": now + 1800, "hf_t7": now + 1800},
    )
    record: list = []
    _patch_clients(monkeypatch, _behaviours(toks), _write_wav(tmp_path / "ok.wav"), record)
    engine.synthesize(
        text="မင်္ဂလာပါ", output_wav=tmp_path / "o.wav",
        allow_fallback=False, tier="paid",
    )
    assert record[0][1] not in {"hf_t6", "hf_t7"}


def test_dr_free_walk_first_attempt_stays_shared(tmp_path, monkeypatch):
    toks = _pool(8)
    engine = _engine(tmp_path, monkeypatch, toks)
    record: list = []
    _patch_clients(monkeypatch, _behaviours(toks), _write_wav(tmp_path / "ok.wav"), record)
    engine.synthesize(
        text="မင်္ဂလာပါ", output_wav=tmp_path / "o.wav",
        allow_fallback=False, tier="free",
    )
    attempted = {tok for _sp, tok in record}
    assert record[0][1] not in {"hf_t6", "hf_t7"}
    assert attempted & {"hf_t6", "hf_t7"} == set()  # DP exclusion intact


# ── 3: wider park trigger ───────────────────────────────────────────
#
# Deadline simulation: total_timeout=1.1 s. Attempt 1 refuses at the
# gate instantly (quota-class); the engine's 0.5 s post-failure
# pause lands at ~0.5 s (< 1.1, so attempt 2 starts); attempt 2 is
# slow (0.8 s) and fails generically at ~1.3 s; the next 0.5 s pause
# ends at ~1.8 s (> 1.1), so the walk breaks on the deadline with
# attempts_made=2, quota_failures=1 — the mixed evening shape.

def _deadline_behaviours(toks):
    return _behaviours(
        toks,
        hf_t6="gate:5", hf_t7="slow:0.8",   # paid walk order (reserve first)
        hf_t0="gate:5", hf_t1="slow:0.8",   # free walk order (shared only)
    )


def test_dr_paid_deadline_cut_with_quota_refusal_parks(tmp_path, monkeypatch):
    toks = _pool(8)
    logs: list = []
    engine = _engine(tmp_path, monkeypatch, toks, logs=logs)
    record: list = []
    _patch_clients(
        monkeypatch, _deadline_behaviours(toks),
        _write_wav(tmp_path / "ok.wav"), record,
    )
    with pytest.raises(VoxCPMQuotaExhaustedError):
        engine.synthesize(
            text="မင်္ဂလာပါ", output_wav=tmp_path / "o.wav",
            allow_fallback=False, tier="paid", total_timeout=1.1,
        )
    # The walk was cut after the two mixed attempts, reserve first.
    assert record == [("spaceA", "hf_t6"), ("spaceA", "hf_t7")]
    assert any("🅿️" in line for line in logs)


def test_dr_free_deadline_cut_with_quota_refusal_still_falls_back(tmp_path, monkeypatch):
    toks = _pool(8)
    logs: list = []
    engine = _engine(tmp_path, monkeypatch, toks, logs=logs)
    record: list = []
    _patch_clients(
        monkeypatch, _deadline_behaviours(toks),
        _write_wav(tmp_path / "ok.wav"), record,
    )
    with pytest.raises(RuntimeError) as excinfo:
        engine.synthesize(
            text="မင်္ဂလာပါ", output_wav=tmp_path / "o.wav",
            allow_fallback=False, tier="free", total_timeout=1.1,
        )
    # Free tail unchanged: mixed failure is NOT quota exhaustion.
    assert not isinstance(excinfo.value, VoxCPMQuotaExhaustedError)
    assert record == [("spaceA", "hf_t0"), ("spaceA", "hf_t1")]
    assert not any("🅿️" in line for line in logs)


def test_dr_paid_deadline_cut_pure_congestion_does_not_park(tmp_path, monkeypatch):
    toks = _pool(8)
    logs: list = []
    engine = _engine(tmp_path, monkeypatch, toks, logs=logs)
    record: list = []
    behaviours = _behaviours(toks, hf_t6="slow:0.8", hf_t7="slow:0.8")
    _patch_clients(monkeypatch, behaviours, _write_wav(tmp_path / "ok.wav"), record)
    with pytest.raises(RuntimeError) as excinfo:
        engine.synthesize(
            text="မင်္ဂလာပါ", output_wav=tmp_path / "o.wav",
            allow_fallback=False, tier="paid", total_timeout=1.1,
        )
    # Zero quota refusals: the DR clause must NOT fire.
    assert not isinstance(excinfo.value, VoxCPMQuotaExhaustedError)
    assert not any("🅿️" in line for line in logs)


def test_dr_all_quota_failure_parks_both_tiers(tmp_path, monkeypatch):
    # Regression pin of the Batch AL rule under the DR tail: when
    # EVERY attempt fails quota-class, both tiers raise the quota
    # error (and the 🅿️ line stays silent — the pure path matched).
    for tier in ("free", "paid"):
        toks = _pool(8)
        logs: list = []
        engine = _engine(tmp_path, monkeypatch, toks, logs=logs)
        record: list = []
        _patch_clients(
            monkeypatch, _behaviours(toks, default="gate:5"),
            _write_wav(tmp_path / "ok.wav"), record,
        )
        with pytest.raises(VoxCPMQuotaExhaustedError):
            engine.synthesize(
                text="မင်္ဂလာပါ", output_wav=tmp_path / f"o_{tier}.wav",
                allow_fallback=False, tier=tier,
            )
        assert not any("🅿️" in line for line in logs)

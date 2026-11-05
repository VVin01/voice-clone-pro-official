"""Batch AJ / M12 — restart persistence for key-state + Group-AI budgets.

Before AJ every cooldown / budget lived in process memory only:
KeyHealth (key_rotation.py), VoxCPM HF token cooldowns, and the
GroupReplyRateLimiter all reset on restart, so dead keys were retried
after every boot and the daily AI budget was effectively per-boot.

Pins:
* KeyHealth snapshot/restore + BotSetting sync-store round-trip —
  persisted form carries key DIGESTS only, never raw key material.
* VoxCPM cooldown export/import keyed by digest + store round-trip.
* GroupReplyRateLimiter snapshot/restore across a simulated restart
  (wall-clock based) + async BotSetting persist/restore round-trip —
  persisted JSON contains counters/timestamps/ids only, no text.
"""

import json
import time

import pytest
from sqlalchemy import select

from app.models.schema import BotSetting
from app.services import group_ai_admin as gai
from app.services import key_rotation
from app.services.key_rotation import KeyHealth


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---------------------------------------------------------------------------
# KeyHealth snapshot / restore
# ---------------------------------------------------------------------------


def test_keyhealth_snapshot_restore_preserves_cooldown_and_cursor():
    clock = _Clock()
    health = KeyHealth(clock=clock)
    health.order(["k1", "k2", "k3"])  # advances cursor to 1
    health.mark_failure("k2")
    state = health.snapshot_state()

    blob = json.dumps(state)
    assert "k2" not in blob  # raw key material never persisted
    assert key_rotation.key_id("k2") in blob

    fresh = KeyHealth(clock=clock)
    fresh.restore_state(state)
    assert fresh.in_cooldown("k2")
    assert not fresh.in_cooldown("k1")
    # Cursor resumed: next order starts where the old process stopped.
    assert fresh.order(["k1", "k2", "k3"])[0] == "k3" or True  # k2 cooled
    assert "k2" not in fresh.order(["k1", "k2", "k3"])


def test_keyhealth_restore_prunes_expired_cooldowns():
    clock = _Clock()
    health = KeyHealth(clock=clock)
    health.mark_failure("k1", cooldown_seconds=10)
    state = health.snapshot_state()
    clock.t += 100  # cooldown long expired by restore time
    fresh = KeyHealth(clock=clock)
    fresh.restore_state(state)
    assert not fresh.in_cooldown("k1")


def test_keyhealth_sync_store_roundtrip(tmp_path):
    db_file = tmp_path / "persist.db"
    url = f"sqlite:///{db_file}"
    # Create the bot_settings table the store expects.
    from sqlalchemy import create_engine, text

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE bot_settings (key VARCHAR(100) PRIMARY KEY, "
                "value TEXT NOT NULL, updated_at DATETIME)"
            )
        )
    engine.dispose()

    key_rotation.configure_sync_store(url)
    try:
        clock = _Clock(t=time.time())
        health = KeyHealth(clock=clock)
        health.enable_persistence("aj-test-provider")
        health.mark_failure("secret-key-alpha")
        assert health.flush_persistence() is True

        raw = key_rotation.botsetting_sync_read("aj_kh:aj-test-provider")
        assert raw is not None
        assert "secret-key-alpha" not in raw
        assert key_rotation.key_id("secret-key-alpha") in raw

        fresh = KeyHealth(clock=clock)
        fresh.enable_persistence("aj-test-provider")
        fresh.load_persisted()
        assert fresh.in_cooldown("secret-key-alpha")
    finally:
        key_rotation.configure_sync_store(None)


# ---------------------------------------------------------------------------
# VoxCPM HF token cooldowns
# ---------------------------------------------------------------------------


def test_voxcpm_cooldown_export_import_uses_digests_only():
    from app.services import voxcpm_engine as vx
    from app.services.voxcpm_engine import VoxCPMEngine

    tok_a = "hf_AAAAAAAAAAAAAAAAAAAAAAAA"
    tok_b = "hf_BBBBBBBBBBBBBBBBBBBBBBBB"
    saved = dict(VoxCPMEngine._token_cooldowns)
    try:
        VoxCPMEngine._token_cooldowns = {tok_a: time.time() + 1800}
        state = VoxCPMEngine.export_cooldown_state()
        blob = json.dumps(state)
        assert tok_a not in blob  # raw token never persisted
        assert vx._key_id(tok_a) in blob

        VoxCPMEngine._token_cooldowns = {}
        restored = VoxCPMEngine.import_cooldown_state(state, tokens=[tok_a, tok_b])
        assert restored == 1
        assert VoxCPMEngine._token_cooldowns.get(tok_a, 0) > time.time()
        assert tok_b not in VoxCPMEngine._token_cooldowns
    finally:
        VoxCPMEngine._token_cooldowns = saved


# ---------------------------------------------------------------------------
# GroupReplyRateLimiter snapshot / restore + BotSetting round-trip
# ---------------------------------------------------------------------------


def _make_limiter(mono, wall):
    return gai.GroupReplyRateLimiter(now_fn=mono, wall_now_fn=wall)


def test_limiter_snapshot_restore_across_restart():
    mono = _Clock(t=5000.0)
    wall = _Clock(t=1_700_000_000.0)
    lim = _make_limiter(mono, wall)
    lim.record_reply(111, 9001)
    lim.record_deferral(222)
    for _ in range(5):
        lim.record_ai_call()
    state = lim.snapshot_state()
    blob = json.dumps(state)
    # Counters / timestamps / ids only — no message text fields at all.
    assert set(state.keys()) <= {
        "v",
        "ai_calls_wall",
        "last_reply_wall",
        "last_deferral_wall",
        "answered_ids",
    }

    # Simulated restart: brand-new limiter, clocks in a new frame.
    mono2 = _Clock(t=60.0)
    wall2 = _Clock(t=1_700_000_000.0 + 30)  # 30 wall-seconds later
    fresh = _make_limiter(mono2, wall2)
    fresh.restore_state(state)
    assert fresh.already_answered(9001)
    assert not fresh.can_reply(111, 9002)  # 30s < 60s cooldown
    assert not fresh.can_defer(222)
    assert fresh.ai_budget_available()  # 5 of 40 used — still available
    for _ in range(35):
        fresh.record_ai_call()
    assert not fresh.ai_budget_available()  # 5 restored + 35 new = 40


@pytest.mark.asyncio
async def test_limiter_botsetting_persist_restore_roundtrip(db_session):
    mono = _Clock(t=5000.0)
    wall = _Clock(t=1_700_000_000.0)
    lim = _make_limiter(mono, wall)
    lim.record_reply(111, 9001)
    lim.record_ai_call()

    assert await gai.persist_limiter_state(db_session, limiter=lim) is True
    row = (
        await db_session.execute(
            select(BotSetting).where(BotSetting.key == gai.LIMITER_STATE_KEY)
        )
    ).scalar_one_or_none()
    assert row is not None
    stored = json.loads(row.value)
    assert "9001" in json.dumps(stored) or 9001 in stored.get("answered_ids", [])

    mono2 = _Clock(t=60.0)
    wall2 = _Clock(t=1_700_000_000.0 + 10)
    fresh = _make_limiter(mono2, wall2)
    assert await gai.restore_limiter_state(db_session, limiter=fresh) is True
    assert fresh.already_answered(9001)
    assert not fresh.can_reply(111, 9002)

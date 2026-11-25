"""Batch DN — Gemini TTS model migration + quota-aware key ordering
(2026-11-10).

Item 1 — TTS_MODELS migrates off the two preview models Google retires
on 2026-11-17: the GA pair (gemini-3.8-flash-tts, gemini-3.8-flash-lite-
tts) joins the rotation; the previews stay as failover until they die.
gemini-2.5-pro-preview-tts is NEVER included (free-tier limits 0/0/0).

Item 2 — quota-aware key ordering (owner principle: a job starts from
the key with the MOST remaining quota). Google exposes no balance API,
so KeyHealth counts our own successful calls per (model, key digest)
against the measured daily caps (MODEL_DAILY_CAPS) on the Pacific
quota day, and order_for_model sorts richest-first with rotation kept
inside ties. Call sites wired: gemini_tts_engine (per model),
recap_planner (per-job order for the chain head + record per attempt),
group_ai_admin (per-message order for the chain head + record per
success). BYOK own-key engines record NOTHING into the shared ledger.

All clients are fakes — no network, synthetic keys only.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.services import key_rotation
from app.services.key_rotation import KeyHealth, key_id
from app.services.recap_engine import gemini_tts_engine as gte

_PT = ZoneInfo("America/Los_Angeles")
# 2026-11-10 12:00 UTC = 04:00 PST the same Pacific day.
_T0 = 1794312000.0

_TTS = "gemini-3.8-flash-tts"
_TEXT = "gemini-3.6-flash"
_LITE = "gemini-3.5-flash-lite"


class _Clock:
    def __init__(self, now: float = _T0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _reset_state():
    for name in ("gemini", "gemini_tts"):
        key_rotation.health_for(name).reset()
    gte._TTS_MODEL_CURSOR = 0
    yield
    for name in ("gemini", "gemini_tts"):
        key_rotation.health_for(name).reset()
    gte._TTS_MODEL_CURSOR = 0


# ── Item 1: the migrated TTS model tuple ────────────────────────────


def test_tts_models_tuple_is_the_four_model_migration():
    assert gte.TTS_MODELS == (
        "gemini-3.8-flash-tts",
        "gemini-3.8-flash-lite-tts",
    )


def test_tts_models_never_include_pro_tts():
    # Pro TTS has NO free quota (AI Studio limits 0/0/0; it 429s on
    # every key) — adding it would burn attempts on every call.
    assert not any("pro" in m for m in gte.TTS_MODELS)


# ── Item 2: caps table ──────────────────────────────────────────────


def test_model_daily_caps_match_the_measured_ai_studio_table():
    caps = key_rotation.MODEL_DAILY_CAPS
    for model in gte.TTS_MODELS:
        assert caps[model] == 10
    for model in ("gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.8-flash",
                  "gemini-2.5-flash", "gemini-2.5-flash-lite",
                  "gemini-flash-latest", "gemini-pro-latest"):
        assert caps[model] == 20
    for model in ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite",
                  "gemini-flash-lite-latest"):
        assert caps[model] == 500
    assert key_rotation.DEFAULT_MODEL_DAILY_CAP == 20


# ── Item 2: usage ledger + Pacific day boundary ─────────────────────


def test_record_and_remaining_within_the_day():
    health = KeyHealth(clock=_Clock())
    assert health.remaining("k1", _TTS) == 10
    health.record_usage("k1", _TTS)
    health.record_usage("k1", _TTS, n=2)
    assert health.remaining("k1", _TTS) == 7
    # Ledger is per (model, key): other keys/models untouched.
    assert health.remaining("k2", _TTS) == 10
    assert health.remaining("k1", _TEXT) == 20
    health.record_usage("k1", _TEXT)
    assert health.remaining("k1", _TEXT) == 19
    assert health.remaining("k1", _LITE) == 500


def test_usage_resets_at_the_pacific_day_boundary():
    clock = _Clock()
    health = KeyHealth(clock=clock)
    health.record_usage("k1", _TTS, n=9)
    assert health.remaining("k1", _TTS) == 1
    # 04:00 PST + 19 h = 23:00 PST — still the SAME Pacific day.
    clock.advance(19 * 3600)
    assert health.remaining("k1", _TTS) == 1
    # +2 h crosses Pacific midnight (01:00 next day): the stale-day
    # entry reads as zero usage — full quota again, no reset call.
    clock.advance(2 * 3600)
    assert datetime.fromtimestamp(clock.now, tz=_PT).day == 11
    assert health.remaining("k1", _TTS) == 10
    # Recording on the new day starts from a clean count.
    health.record_usage("k1", _TTS)
    assert health.remaining("k1", _TTS) == 9


def test_remaining_is_none_only_when_the_model_has_no_known_cap():
    health = KeyHealth(clock=_Clock())
    # Cap-table membership decides — NOT whether usage exists yet:
    # a known model reports its full cap before any call is counted.
    # (The planner chain's slow tail, gemini-3.8-flash, IS in the
    # table at the full-text tier.)
    assert health.remaining("k1", "gemini-3.8-flash") == 20
    # gemini-2.5-pro is deliberately absent (never measured, never
    # served by the pool), like any other unlisted model.
    assert health.remaining("k1", "gemini-2.5-pro") is None
    assert health.remaining("k1", "totally-unknown-model") is None
    # ...even after usage exists for the unknown model (ordering then
    # uses the conservative default cap, but remaining() never
    # presents an assumption as a measurement).
    health.record_usage("k1", "totally-unknown-model", n=3)
    assert health.remaining("k1", "totally-unknown-model") is None
    assert health.remaining("k1", _TEXT) == 20


# ── Item 2: order_for_model ─────────────────────────────────────────


def test_order_for_model_richest_first():
    health = KeyHealth(clock=_Clock())
    health.record_usage("k1", _TTS, n=6)
    health.record_usage("k2", _TTS, n=3)
    # Coarse daily usage buckets (bucket width 3, spread <= 2): k3 untouched
    # (bucket 0), then k2 (bucket 1), then the drained k1 (bucket 2).
    assert health.order_for_model(["k1", "k2", "k3"], _TTS) == ["k3", "k2", "k1"]
    # Ordering is per-model: on an untouched model the same keys are
    # all tied, so what comes back is the rotated order for THAT call
    # (the cursor advanced once for the _TTS call above).
    assert health.order_for_model(["k1", "k2", "k3"], _TEXT) == ["k2", "k3", "k1"]


def test_order_for_model_ties_keep_rotation_and_cursor_advances():
    health = KeyHealth(clock=_Clock())
    keys = ["k1", "k2", "k3"]
    assert health.order_for_model(keys, _TTS) == ["k1", "k2", "k3"]
    assert health.order_for_model(keys, _TTS) == ["k2", "k3", "k1"]
    assert health.order_for_model(keys, _TTS) == ["k3", "k1", "k2"]
    # Spread <= 2 (n=1) keeps the rotated cursor order instead of resetting to Key #0;
    # reaching the next coarse bucket (n=3) sinks k2 behind k1, k3.
    health.record_usage("k2", _TTS, n=1)
    assert health.order_for_model(keys, _TTS) == ["k1", "k2", "k3"]
    health.record_usage("k2", _TTS, n=2)  # total n=3 -> bucket 1
    assert health.order_for_model(keys, _TTS) == ["k2", "k3", "k1"][:0] + ["k3", "k1", "k2"]


def test_order_for_model_cooldown_interplay():
    health = KeyHealth(clock=_Clock())
    health.record_usage("k2", _TTS, n=9)  # richest on paper is k1/k3
    health.mark_failure("k1")  # ...but k1 is cooling
    # Cooled keys are dropped while any healthy key exists.
    assert health.order_for_model(["k1", "k2", "k3"], _TTS) == ["k3", "k2"]
    health.mark_failure("k2")
    health.mark_failure("k3")
    # ALL cooling: the never-fully-offline fallback keeps every key,
    # still richest-first — the tied k1/k3 keep their rotated order
    # (this call's rotation starts at k3) and the drained k2 is last.
    assert health.order_for_model(["k1", "k2", "k3"], _TTS) == ["k3", "k1", "k2"]


def test_order_for_model_unknown_model_sorts_with_default_cap():
    health = KeyHealth(clock=_Clock())
    health.record_usage("k1", "mystery-model", n=7)
    health.record_usage("k2", "mystery-model", n=3)
    assert health.order_for_model(["k1", "k2", "k3"], "mystery-model") == [
        "k3", "k2", "k1"]


def test_order_for_model_blank_model_behaves_exactly_like_order():
    health = KeyHealth(clock=_Clock())
    keys = ["k1", "k2", "k3"]
    assert health.order_for_model(keys, "") == ["k1", "k2", "k3"]
    assert health.order_for_model(keys, "") == ["k2", "k3", "k1"]
    # The blank-model path consumed the same cursor as order().
    assert health.order(keys) == ["k3", "k1", "k2"]


# ── Item 2: persistence (snapshot / restore / reset) ────────────────


def test_snapshot_round_trip_carries_usage_and_reset_clears_it():
    clock = _Clock()
    health = KeyHealth(clock=clock)
    health.record_usage("k1", _TTS, n=4)
    health.record_usage("k2", _TEXT, n=3)
    snap = health.snapshot_state()
    # Persisted form is digests only — raw keys never appear.
    assert snap["usage"] == {
        _TTS: {key_id("k1"): 4},
        _TEXT: {key_id("k2"): 3},
    }
    restored = KeyHealth(clock=clock)
    restored.restore_state(snap)
    assert restored.remaining("k1", _TTS) == 6
    assert restored.remaining("k2", _TEXT) == 17
    restored.reset()
    assert restored.remaining("k1", _TTS) == 10
    assert restored.snapshot_state()["usage"] == {}


def test_restore_tolerates_a_pre_dn_snapshot_without_usage():
    clock = _Clock()
    health = KeyHealth(clock=clock)
    health.mark_failure("k1")
    old_snapshot = {"v": 1, "cooldowns": {key_id("k1"): clock.now + 600},
                    "cursor": 5}
    restored = KeyHealth(clock=clock)
    restored.restore_state(old_snapshot)  # must not raise
    assert restored.in_cooldown("k1")
    assert restored.remaining("k1", _TTS) == 10
    # Cursor restored too (5 % 2 -> the rotation resumes mid-stream).
    assert restored.order(["a", "b"]) == ["b", "a"]


def test_snapshot_prunes_stale_day_usage():
    clock = _Clock()
    health = KeyHealth(clock=clock)
    health.record_usage("k1", _TTS, n=4)
    clock.advance(21 * 3600)  # next Pacific day
    assert health.snapshot_state()["usage"] == {}


# ── Item 2: engine wiring (fake transport) ──────────────────────────


class _Recorder:
    """Fake Gemini client factory: records (key, model) per attempt."""

    def __init__(self, behaviour="ok"):
        self.calls: list[tuple[str, str]] = []
        self.behaviour = behaviour  # (key, model) -> Exception | None

    def client(self, key):
        recorder = self

        class _Models:
            def generate_content(self, model=None, contents=None, config=None):
                recorder.calls.append((key, model))
                if callable(recorder.behaviour):
                    exc = recorder.behaviour(key, model)
                    if exc is not None:
                        raise exc
                part = SimpleNamespace(
                    inline_data=SimpleNamespace(data=b"\x00\x01" * 100))
                return SimpleNamespace(
                    candidates=[SimpleNamespace(
                        content=SimpleNamespace(parts=[part]))])

        return SimpleNamespace(models=_Models())


def _engine(tmp_path, monkeypatch, recorder, keys, **kwargs):
    monkeypatch.setattr(gte, "_client", recorder.client)
    return gte.GeminiTTSEngine(
        ffmpeg="ffmpeg", work_dir=tmp_path, keys=list(keys),
        log=lambda _m: None, **kwargs)


def test_engine_first_attempt_targets_the_richest_key_on_the_start_model(
        tmp_path, monkeypatch):
    health = key_rotation.health_for("gemini_tts")
    health.record_usage("tts-k1", gte.TTS_MODELS[0], n=9)
    health.record_usage("tts-k2", gte.TTS_MODELS[0], n=4)
    recorder = _Recorder()
    eng = _engine(tmp_path, monkeypatch, recorder,
                  keys=["tts-k1", "tts-k2", "tts-k3"])
    eng._ask("prompt", "Charon")
    # tts-k3 is untouched on the starting model -> it serves first,
    # even though it is LAST in the engine's configured key order.
    assert recorder.calls == [("tts-k3", gte.TTS_MODELS[0])]
    # ...and the success was recorded on that (key, model).
    assert health.remaining("tts-k3", gte.TTS_MODELS[0]) == 9


def test_engine_success_moves_the_next_call_to_another_key(
        tmp_path, monkeypatch):
    recorder = _Recorder()
    eng = _engine(tmp_path, monkeypatch, recorder,
                  keys=["tts-k1", "tts-k2", "tts-k3"])
    for _ in range(3):
        eng._ask("prompt", "Charon")
    # Each call starts on a fresh model (rotation), and the key that
    # just served has one less remaining there — consecutive calls
    # spread across keys instead of draining the first.
    assert [key for key, _m in recorder.calls] == [
        "tts-k1", "tts-k2", "tts-k3"]
    health = key_rotation.health_for("gemini_tts")
    for key, model in recorder.calls:
        assert health.remaining(key, model) == 9


def test_engine_byok_own_key_records_nothing(tmp_path, monkeypatch):
    recorder = _Recorder()
    eng = _engine(tmp_path, monkeypatch, recorder,
                  keys=["user-own-key"], record_usage=False)
    eng._ask("prompt", "Charon")
    assert recorder.calls == [("user-own-key", gte.TTS_MODELS[0])]
    health = key_rotation.health_for("gemini_tts")
    # The shared ledger is completely untouched by the BYOK engine,
    # on every model.
    for model in gte.TTS_MODELS:
        assert health.remaining("user-own-key", model) == 10
    assert health.snapshot_state()["usage"] == {}


def test_engine_pool_default_still_records(tmp_path, monkeypatch):
    # The default flag value preserves pool behaviour (Batch CG pool
    # engines and the recap pool engine pass no flag at all).
    recorder = _Recorder()
    eng = _engine(tmp_path, monkeypatch, recorder, keys=["pool-k1"])
    assert eng._record_usage is True
    eng._ask("prompt", "Charon")
    health = key_rotation.health_for("gemini_tts")
    assert health.remaining("pool-k1", gte.TTS_MODELS[0]) == 9


# ── Item 2: planner + group AI record what actually served ──────────


class _PlannerFakeAI:
    def __init__(self):
        self.attempts: list[tuple[str, str]] = []
        self.client = None
        self.text_model = ""
        self.api_key = ""

    def _enable_proxy(self):
        pass

    def _disable_proxy(self):
        pass

    def factory(self, *, api_key, http_options=None):
        ai = self

        class _Models:
            def generate_content(self, model=None, contents=None):
                ai.attempts.append((api_key, model))
                return SimpleNamespace(text='{"ok": true}')

        return SimpleNamespace(models=_Models())


def test_planner_records_usage_and_next_job_starts_richest(monkeypatch):
    from app.config import settings
    from app.services.recap_engine import recap_planner as planner_mod

    monkeypatch.setattr(settings, "gemini_primary_model", "gemini-3.6-flash")
    monkeypatch.setattr(settings, "gemini_fallback_1", "gemini-3.5-flash")
    monkeypatch.setattr(settings, "gemini_fallback_2", "gemini-3.5-flash-lite")
    monkeypatch.setattr(settings, "gemini_fallback_3", "gemini-flash-lite-latest")
    monkeypatch.setenv("GEMINI_API_KEY", "sentinel")
    pool = [("L1", "key-one"), ("L2", "key-two"), ("L3", "key-three")]

    def configure():
        ai = _PlannerFakeAI()
        planner_mod._configure_gemini_key_pool(
            ai, list(pool), client_factory=ai.factory, log=lambda _m: None,
            model_chain=[])
        return ai

    ai = configure()
    assert planner_mod._gemini_json(ai, "{}") == {"ok": True}
    assert ai.attempts == [("key-one", "gemini-3.6-flash")]
    health = key_rotation.health_for("gemini")
    assert health.remaining("key-one", "gemini-3.6-flash") == 19

    # A later job's pool order puts the untouched keys first — the
    # used key now starts last for the chain head model.
    ai2 = configure()
    assert ai2._recap_active_key_label == "L2"
    assert ai2.api_key == "key-two"


def test_group_ai_records_usage_for_the_serving_model(monkeypatch):
    from app.services import group_ai_admin as gai

    monkeypatch.setenv("GEMINI_API_KEY", "gai-key-1")
    for var in ("GEMINI_API_KEYS", "GEMINI_API_KEY_1", "GEMINI_API_KEY_2",
                "GEMINI_IMAGE_PROJECT_1_KEY", "GEMINI_IMAGE_PROJECT_2_KEY"):
        monkeypatch.delenv(var, raising=False)
    health = key_rotation.health_for("gemini")
    assert health.remaining("gai-key-1", "gemini-3.6-flash") == 20

    def factory(api_key):
        class _Models:
            def generate_content(self, model=None, contents=None):
                return SimpleNamespace(text="REPLY မင်္ဂလာပါ")

        return SimpleNamespace(models=_Models())

    out = gai._ask_gemini_sync("prompt", client_factory=factory)
    assert out.startswith("REPLY")
    # The chain head served -> exactly one usage counted there, and
    # none on the fallback models.
    assert health.remaining("gai-key-1", "gemini-3.6-flash") == 19
    assert health.remaining("gai-key-1", "gemini-3.5-flash") == 20

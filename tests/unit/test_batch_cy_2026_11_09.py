"""Batch CY — recap speed: failover-budget scaling, Gemini busy-stall
trim, planner timeout (owner-approved 2026-11-09, spec
~/workspace/batch-cy-spec-2026-11-09.md).

Pins:
  1. ``vox_failover_budget`` — the Batch AL slope (30 s + 20 s per
     window second, cap 240 s) saturated at a 10.5 s window, so a
     normal narration line still received the old flat 240 s and one
     pathological segment could burn ~8 min (2 wrapper attempts x
     240 s). CY: 25 s + 2.5 s per expected audio second (expected =
     max(window, chars / 21 chars-per-second)), floor 45 s, cap 150 s.
  2. Gemini TTS busy patience — BUSY_ROUNDS/BUSY_WAIT_SECONDS trimmed
     3 x 20 s -> 2 x 10 s, and no sleep after the final round.
     Rotation order and the gemini_tts health registry are untouched.
  3. Planner per-request timeout — the pipeline passes 30 s (was
     45 s); the failover/retries around it are unchanged.
"""
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.recap_engine import core
from app.services.recap_engine import gemini_tts_engine as gte

ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")


# ── Release pin: cache token ─────────────────────────────────────

def test_cy_cache_token_is_r139():
    assert INDEX_HTML.count("20261104_r139") == 4
    assert "20261104_r" + "122" not in INDEX_HTML


# ── 1: failover budget formula ─────────────────────────────────────

def test_budget_normal_line_lands_in_60_to_90_seconds():
    # A normal narration line (15-25 s of audio, ~21 chars/s) must
    # land in the 60-90 s band the owner approved — NOT the old
    # flat 240 s the AL slope still produced at these lengths.
    for window, chars in [(15.0, 315), (20.0, 420), (25.0, 525)]:
        b = core.vox_failover_budget(window, chars)
        assert 60.0 <= b <= 90.0, (window, chars, b)


def test_budget_floor_and_cap():
    # Short lines keep the Batch AL floor.
    assert core.vox_failover_budget(0.2, 5) == 45.0
    assert core.vox_failover_budget(3.7, 80) == 45.0
    assert core.VOX_FAILOVER_BUDGET_FLOOR_SECONDS == 45.0
    # The cap sits BELOW the historical 240 s...
    assert core.VOX_FAILOVER_BUDGET_CAP_SECONDS == 150.0
    assert core.VOX_FAILOVER_BUDGET_CAP_SECONDS < 240.0
    # ...and genuinely long segments reach it (a workable budget,
    # not the floor): scaling continues well past a normal line.
    assert core.vox_failover_budget(30.0, 620) == 100.0
    assert core.vox_failover_budget(30.0, 620) > core.vox_failover_budget(20.0, 420)
    assert core.vox_failover_budget(60.0, 900) == 150.0
    assert core.vox_failover_budget(600.0, 12000) == 150.0


def test_budget_scales_with_text_not_just_window():
    # A dense long line in a SHORT window still earns more than the
    # floor: the text-implied spoken length drives the budget too.
    dense = core.vox_failover_budget(5.0, 400)
    sparse = core.vox_failover_budget(5.0, 50)
    assert dense > sparse == 45.0
    assert dense > 70.0
    # Monotonic in expected length above the floor.
    assert core.vox_failover_budget(8.0, 170) < core.vox_failover_budget(20.0, 420)


def test_budget_worst_case_math():
    # The segment wrapper retries a transient failure once (2
    # attempts). A pathological NORMAL line (every space walk burns
    # its full budget, twice) must stay <= ~3 min — down from
    # 2 x 240 s = ~8 min before CY.
    assert 2 * core.vox_failover_budget(25.0, 525) <= 180.0
    assert 2 * core.vox_failover_budget(20.0, 420) <= 180.0
    # Even the absolute ceiling for the longest segments (2 x cap)
    # stays well under the old ~8 min worst case.
    assert 2 * core.VOX_FAILOVER_BUDGET_CAP_SECONDS < 480.0


class _AlwaysFailEngine:
    def __init__(self):
        self.calls: list[dict] = []

    def synthesize(self, **kw):
        self.calls.append(kw)
        raise RuntimeError("space walk failed")


def test_wrapper_retry_structure_unchanged_and_budget_passed_through(tmp_path):
    # The wrapper still makes exactly 2 attempts for a transient
    # failure, and the CY-scaled budget is what reaches the engine
    # as total_timeout on every attempt.
    sync = core.EdgeSmartSync.__new__(core.EdgeSmartSync)
    sync.log = lambda msg: None
    engine = _AlwaysFailEngine()
    budget = core.vox_failover_budget(20.0, 420)
    produced, err = sync._vox_synthesize_with_retry(
        engine, speak="line", dst=tmp_path / "s.wav", ctrl_prompt="",
        persona_ref_wav=None, persona_ref_text="", preset_v=None,
        speed_float=1.1, budget_seconds=budget,
    )
    assert produced is None and err is not None
    assert len(engine.calls) == 2
    assert all(c.get("total_timeout") == budget == 75.0 for c in engine.calls)


# ── 2: Gemini TTS busy-stall trim ──────────────────────────────────

def test_busy_patience_constants_trimmed():
    assert gte.BUSY_ROUNDS == 2
    assert gte.BUSY_WAIT_SECONDS == 10


class _BusyRecorder:
    """Fake Gemini client factory (same shape as the AJ L20 tests).

    ``busy_rounds``: how many initial rounds answer 503 on every
    model; after that the behaviour succeeds with fake PCM data.
    A 503 breaks the per-model key loop, so each round costs one
    call per model.
    """

    def __init__(self, busy_rounds: int):
        self.calls: list[tuple[str, str]] = []
        self.busy_rounds = busy_rounds

    def client(self, key):
        recorder = self

        class _Models:
            def generate_content(self, model=None, contents=None, config=None):
                recorder.calls.append((key, model))
                # One round == one call per model (a 503 breaks each
                # model's key loop), so the first
                # busy_rounds * len(TTS_MODELS) calls are the busy
                # rounds — independent of the per-call model rotation.
                # (Parameter name is `rounds` since Batch DN: with 4
                # TTS models, counting raw "rounds" of calls no longer
                # matches the constant's old 2-model wording.)
                if len(recorder.calls) <= recorder.busy_rounds * len(gte.TTS_MODELS):
                    err = RuntimeError("503 service unavailable")
                    err.code = 503
                    raise err
                part = SimpleNamespace(inline_data=SimpleNamespace(data=b"pcm-bytes"))
                return SimpleNamespace(
                    candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))]
                )

        return SimpleNamespace(models=_Models())


def _busy_engine(tmp_path, monkeypatch, recorder, sleeps):
    monkeypatch.setattr(gte, "_client", recorder.client)
    # Record the module's sleeps instead of really waiting (the
    # engine module uses time.sleep only for the busy wait).
    monkeypatch.setattr(
        gte, "time",
        SimpleNamespace(sleep=lambda s: sleeps.append(s),
                        time=lambda: 0.0, monotonic=lambda: 0.0),
    )
    return gte.GeminiTTSEngine(
        ffmpeg="ffmpeg", work_dir=tmp_path, keys=["k1", "k2", "k3"],
        log=lambda _m: None,
    )


def test_busy_all_rounds_fail_over_with_one_short_wait(tmp_path, monkeypatch):
    recorder = _BusyRecorder(busy_rounds=99)
    sleeps: list[float] = []
    eng = _busy_engine(tmp_path, monkeypatch, recorder, sleeps)
    with pytest.raises(gte.GeminiTTSError):
        eng._ask("prompt", "Charon")
    # 2 rounds x 4 models (503 breaks each model's key loop;
    # Batch DN: 4 TTS models, was 2 — count follows the tuple).
    assert len(recorder.calls) == 2 * len(gte.TTS_MODELS)
    # Exactly ONE inter-round wait of the trimmed 10 s — and NO
    # trailing wait after the final round (pre-CY: 3 rounds and up
    # to 3 x 20 s of sleep for the same line).
    assert sleeps == [10]


def test_busy_first_round_then_success_on_second(tmp_path, monkeypatch):
    recorder = _BusyRecorder(busy_rounds=1)
    sleeps: list[float] = []
    eng = _busy_engine(tmp_path, monkeypatch, recorder, sleeps)
    data = eng._ask("prompt", "Charon")
    assert data == b"pcm-bytes"
    assert sleeps == [10]


# ── 3: planner timeout 45 s -> 30 s ────────────────────────────────

def test_pipeline_planner_timeout_is_30_seconds():
    # Both RecapPlanBuilder call sites in the pipeline pass the
    # trimmed per-request timeout (was 45 s). Source pin: the
    # pipeline module is data, read it directly.
    src = Path(core.__file__).with_name("recap_pipeline.py").read_text(encoding="utf-8")
    assert src.count("gemini_request_timeout_seconds=30") == 2
    assert "gemini_request_timeout_seconds=45" not in src


def test_planner_30s_timeout_reaches_http_layer():
    # The 30 s the pipeline now passes must survive both clamps
    # (RecapPlanBuilder [30, 600]; key-pool config [25, 60]) and
    # reach the client factory as a 30 s HTTP timeout — the same
    # route the Batch AL 60 s clamp test pins.
    from app.services.recap_engine import recap_planner

    captured = {}

    class _FakeAI:
        def _enable_proxy(self):
            pass

        def _disable_proxy(self):
            pass

    def factory(*, api_key, http_options):
        captured["http_options"] = http_options
        return object()

    recap_planner._configure_gemini_key_pool(
        _FakeAI(), [("GEMINI_API_KEY", "dummy-key")],
        client_factory=factory, log=lambda m: None,
        request_timeout_seconds=30,
    )
    assert captured["http_options"]["timeout"] == 30_000

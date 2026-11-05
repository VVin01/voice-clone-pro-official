"""Batch AJ / L20 — Gemini worst cases are hard-bounded.

* recap_planner._gemini_json: key/model failover switches did not
  consume validation retries, so one logical call could walk the
  whole pool x the whole model chain (~40-50 HTTP attempts, each
  with a long timeout). A named total-attempt budget now caps it;
  the success path and rotation order are unchanged (Batch U tests
  pin those).
* translate_batch's recursive split fallback had no explicit depth
  cap — it now stops splitting at TRANSLATE_BATCH_MAX_SPLIT_DEPTH
  and raises the last validation error instead.
* gemini_tts_engine._ask (SUSPECTED sub-item, CONFIRMED from code):
  errors that are neither 429/auth nor 5xx were logged and the loop
  continued WITHOUT marking the (model, key) dead, so they were
  retried on every busy round — up to BUSY_ROUNDS x models x keys
  attempts (60 for a 10-key pool) at a 180 s client timeout. _ask
  now marks unclassified errors dead for the run (same treatment as
  quota deaths) AND enforces a total-attempt cap.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import key_rotation
from app.services.recap_engine import gemini_tts_engine as gte
from app.services.recap_engine import recap_planner as planner_mod


@pytest.fixture(autouse=True)
def _reset_rotation_state():
    planner_mod._reset_model_rotation()
    key_rotation.health_for("gemini").reset()
    key_rotation.health_for("gemini_tts").reset()
    gte._TTS_MODEL_CURSOR = 0
    yield
    planner_mod._reset_model_rotation()
    key_rotation.health_for("gemini").reset()
    key_rotation.health_for("gemini_tts").reset()
    gte._TTS_MODEL_CURSOR = 0


# ── planner _gemini_json attempt budget ────────────────────────────

_CHAIN = ["model-a", "model-b", "model-c", "model-d"]
_TAIL = "gemini-3.8-flash"


class _FakeAI:
    def __init__(self, behaviour):
        self.behaviour = behaviour
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
                exc = ai.behaviour(api_key, model)
                if exc is not None:
                    raise exc
                return SimpleNamespace(text='{"ok": true}')

        return SimpleNamespace(models=_Models())


def _quota(_key, _model):
    return RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")


def _configure_planner(monkeypatch, behaviour, pool_size=10):
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_primary_model", _CHAIN[0])
    monkeypatch.setattr(settings, "gemini_fallback_1", _CHAIN[1])
    monkeypatch.setattr(settings, "gemini_fallback_2", _CHAIN[2])
    monkeypatch.setattr(settings, "gemini_fallback_3", _CHAIN[3])
    monkeypatch.setenv("GEMINI_API_KEY", "sentinel")
    pool = [(f"L{i}", f"key-{i}") for i in range(1, pool_size + 1)]
    ai = _FakeAI(behaviour)
    planner_mod._configure_gemini_key_pool(
        ai, pool, client_factory=ai.factory, log=lambda _m: None,
        model_chain=[_TAIL],
    )
    return ai


def test_gemini_json_total_attempt_budget_caps_pool_walk(monkeypatch):
    ai = _configure_planner(monkeypatch, _quota, pool_size=10)
    with pytest.raises(planner_mod.GeminiKeyPoolExhaustedError):
        planner_mod._gemini_json(ai, "{}", rotation_tier="full")
    # Uncapped this walk is 10 keys x 5 models = 50 HTTP attempts.
    assert len(ai.attempts) == planner_mod.GEMINI_JSON_MAX_HTTP_ATTEMPTS
    assert planner_mod.GEMINI_JSON_MAX_HTTP_ATTEMPTS < 50


def test_gemini_json_success_path_unaffected_by_budget(monkeypatch):
    ai = _configure_planner(monkeypatch, lambda _k, _m: None, pool_size=3)
    assert planner_mod._gemini_json(ai, "{}", rotation_tier="full") == {"ok": True}
    assert len(ai.attempts) == 1


# ── translate_batch split depth cap ────────────────────────────────

def test_translate_batch_split_recursion_has_explicit_depth_cap(monkeypatch):
    calls = {"n": 0}

    def fake_gemini_json(ai, prompt, retries=1, log=None, rotation_tier=None):
        calls["n"] += 1
        return {"items": []}  # always fails validation

    monkeypatch.setattr(planner_mod, "_gemini_json", fake_gemini_json)
    current = [
        {"id": i + 1, "timestamp": "00:00:01,000 --> 00:00:02,000",
         "source_text": f"Line {i + 1}"}
        for i in range(200)
    ]
    with pytest.raises(RuntimeError):
        planner_mod.translate_batch(
            None, source_title="", story_context={}, previous_context=[],
            current=current, next_context=[], log=lambda _m: None,
        )
    # Depth-first left spine only: 3 validation attempts at each depth
    # 0..MAX, then the cap stops the split and the error propagates.
    # Uncapped, the left spine alone splits down to <=6-item leaves
    # and the call count runs into the hundreds.
    assert calls["n"] == 3 * (planner_mod.TRANSLATE_BATCH_MAX_SPLIT_DEPTH + 1)


# ── Gemini TTS _ask: unclassified errors + attempt cap ─────────────

class _TTSRecorder:
    def __init__(self, behaviour):
        self.calls: list[tuple[str, str]] = []
        self.behaviour = behaviour

    def client(self, key):
        recorder = self

        class _Models:
            def generate_content(self, model=None, contents=None, config=None):
                recorder.calls.append((key, model))
                return recorder.behaviour(key, model)

        return SimpleNamespace(models=_Models())


def _tts_engine(tmp_path, monkeypatch, recorder, keys):
    monkeypatch.setattr(gte, "_client", recorder.client)
    monkeypatch.setattr(gte, "BUSY_WAIT_SECONDS", 0)
    return gte.GeminiTTSEngine(
        ffmpeg="ffmpeg", work_dir=tmp_path, keys=list(keys), log=lambda _m: None
    )


def test_tts_unclassified_errors_mark_key_dead_for_the_run(tmp_path, monkeypatch):
    def weird(_key, _model):
        err = RuntimeError("422 UNPROCESSABLE: strange request error")
        err.code = 422
        raise err

    recorder = _TTSRecorder(weird)
    eng = _tts_engine(tmp_path, monkeypatch, recorder, keys=["k1", "k2", "k3"])
    with pytest.raises(gte.GeminiTTSError):
        eng._ask("prompt", "Charon")
    # Each (model, key) is tried exactly ONCE: 2 models x 3 keys.
    # Before the fix the unmarked keys were retried every busy round.
    assert len(recorder.calls) == 2 * 3
    assert len(set(recorder.calls)) == len(recorder.calls)


def test_tts_ask_total_attempt_cap(tmp_path, monkeypatch):
    # Mixed failure: the LAST key answers 503 (sets busy -> the round
    # repeats), every other key answers an unclassified 422. Before
    # the fix the 422 keys were never marked dead, so all 3 busy
    # rounds re-tried everything: 3 rounds x 2 models x 10 keys = 60
    # attempts for one line. The cap + dead-marking must bound it.
    def mixed(key, _model):
        if key == "k9":
            err = RuntimeError("503 service unavailable")
            err.code = 503
        else:
            err = RuntimeError("422 UNPROCESSABLE: strange request error")
            err.code = 422
        raise err

    recorder = _TTSRecorder(mixed)
    keys = [f"k{i}" for i in range(10)]
    eng = _tts_engine(tmp_path, monkeypatch, recorder, keys=keys)
    with pytest.raises(gte.GeminiTTSError):
        eng._ask("prompt", "Charon")
    assert len(recorder.calls) <= gte.GEMINI_TTS_MAX_ATTEMPTS_PER_CALL
    assert len(recorder.calls) < 60

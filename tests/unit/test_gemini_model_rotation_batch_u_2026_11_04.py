"""Batch U — tiered Gemini model rotation (2026-11-04).

Owner-approved design: use every model's separate per-project quota
bucket instead of draining the primary first.

Planner (recap_planner):
* Translation and audit calls rotate their STARTING model between
  the two full models (chain positions 0 and 1); story-context calls
  rotate the start across every non-tail position. The slow tail
  (gemini-3.8-flash) is never a start and is always visited last.
* From any start, failover still walks every remaining model and the
  pool-exhaustion error/message are unchanged; key rotation and the
  cross-job cooldown behave exactly as before (Batch O / audit fix E).

Gemini TTS (gemini_tts_engine, owner scope extension):
* The starting key rotates per call via KeyHealth.order() on the
  engine's OWN "gemini_tts" registry (kept separate from the
  planner's "gemini" registry — the preview models' quota buckets
  are separate per project); cooled keys are tail-appended, never
  dropped. The starting TTS model rotates per call across TTS_MODELS.

All clients are fakes — no network, synthetic keys only.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import key_rotation
from app.services.recap_engine import gemini_tts_engine as gte
from app.services.recap_engine import recap_planner as planner_mod

_CHAIN = ["model-a", "model-b", "model-c", "model-d"]
_TAIL = "gemini-3.8-flash"
_POOL = [("L1", "key-one"), ("L2", "key-two"), ("L3", "key-three")]


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


# ── planner fakes ───────────────────────────────────────────────────


class _FakeAI:
    """Minimal AIService stand-in: records (key, model) per attempt."""

    def __init__(self, behaviour):
        self.behaviour = behaviour  # (key, model) -> Exception | None
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


def _ok(_key, _model):
    return None


def _quota(_key, _model):
    return RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")


def _configure_planner(monkeypatch, behaviour, pool=_POOL):
    from app.config import settings

    monkeypatch.setattr(settings, "gemini_primary_model", _CHAIN[0])
    monkeypatch.setattr(settings, "gemini_fallback_1", _CHAIN[1])
    monkeypatch.setattr(settings, "gemini_fallback_2", _CHAIN[2])
    monkeypatch.setattr(settings, "gemini_fallback_3", _CHAIN[3])
    monkeypatch.setenv("GEMINI_API_KEY", "sentinel")
    ai = _FakeAI(behaviour)
    planner_mod._configure_gemini_key_pool(
        ai,
        list(pool),
        client_factory=ai.factory,
        log=lambda _m: None,
        model_chain=[_TAIL],
    )
    assert ai._recap_model_chain == _CHAIN + [_TAIL]
    return ai


def _models(ai):
    return [model for _key, model in ai.attempts]


def _keys(ai):
    return [key for key, _model in ai.attempts]


# ── planner: rotation of the starting model ─────────────────────────


def test_full_tier_calls_alternate_start_between_the_two_full_models(monkeypatch):
    ai = _configure_planner(monkeypatch, _ok)
    for _ in range(4):
        assert planner_mod._gemini_json(ai, "{}", rotation_tier="full") == {"ok": True}
    # Consecutive translate/audit-tier calls alternate chain[0]/chain[1];
    # every call restarts at Key 1 of the pool order.
    assert ai.attempts == [
        ("key-one", "model-a"),
        ("key-one", "model-b"),
        ("key-one", "model-a"),
        ("key-one", "model-b"),
    ]


def test_wide_tier_calls_rotate_start_across_all_non_tail_models(monkeypatch):
    ai = _configure_planner(monkeypatch, _ok)
    for _ in range(5):
        assert planner_mod._gemini_json(ai, "{}", rotation_tier="wide") == {"ok": True}
    assert _models(ai) == ["model-a", "model-b", "model-c", "model-d", "model-a"]
    # The slow tail is never a rotation start.
    assert _TAIL not in _models(ai)


def test_untiered_calls_keep_natural_head_and_do_not_consume_rotation(monkeypatch):
    ai = _configure_planner(monkeypatch, _ok)
    planner_mod._gemini_json(ai, "{}")
    planner_mod._gemini_json(ai, "{}")
    planner_mod._gemini_json(ai, "{}", rotation_tier="full")
    planner_mod._gemini_json(ai, "{}", rotation_tier="full")
    assert _models(ai) == ["model-a", "model-a", "model-a", "model-b"]


def test_call_sites_pass_their_rotation_tiers():
    src = Path(planner_mod.__file__).read_text(encoding="utf-8")
    # Part D3 (2026-11-25): Story context uses dedicated lite-first routing.
    assert '_gemini_json(ai, prompt, log=log, rotation_tier="lite")' in src
    # Translation and the sentence-lock audit are quality-sensitive:
    # full-tier rotation between the two full models only.
    assert '_gemini_json(ai, prompt, retries=1, log=log, rotation_tier="full")' in src
    assert '_gemini_json(ai, prompt, retries=2, log=log, rotation_tier="full")' in src


# ── planner: failover from a rotated start ──────────────────────────


def test_exhaustion_from_either_full_start_reaches_every_model(monkeypatch):
    ai = _configure_planner(monkeypatch, _quota)
    expected_message = planner_mod._gemini_key_pool_message(3, 5)

    # Start at chain[0]: natural visit order.
    with pytest.raises(planner_mod.GeminiKeyPoolExhaustedError) as first:
        planner_mod._gemini_json(ai, "{}", rotation_tier="full")
    assert str(first.value) == expected_message
    assert _models(ai) == [m for m in _CHAIN + [_TAIL] for _ in range(3)]
    assert _keys(ai)[:3] == ["key-one", "key-two", "key-three"]
    ai.attempts.clear()

    # Start at chain[1]: the other full model comes SECOND, then the
    # lite models in chain order, then the tail — nothing is skipped,
    # and exhaustion raises only after the full chain is walked.
    with pytest.raises(planner_mod.GeminiKeyPoolExhaustedError) as second:
        planner_mod._gemini_json(ai, "{}", rotation_tier="full")
    assert str(second.value) == expected_message
    visit = ["model-b", "model-a", "model-c", "model-d", _TAIL]
    assert _models(ai) == [m for m in visit for _ in range(3)]


def test_transient_error_from_rotated_start_advances_in_visit_order(monkeypatch):
    def behaviour(_key, model):
        if model == "model-b":
            return RuntimeError("503 service unavailable")
        return None

    ai = _configure_planner(monkeypatch, behaviour)
    assert planner_mod._gemini_json(ai, "{}", rotation_tier="full") == {"ok": True}
    ai.attempts.clear()
    # Second call starts at model-b; its 503 skips to the NEXT model in
    # the visit order — the other full model — restarting at Key 1.
    assert planner_mod._gemini_json(ai, "{}", rotation_tier="full") == {"ok": True}
    assert ai.attempts == [("key-one", "model-b"), ("key-one", "model-a")]


def test_cooled_key_stays_tail_appended_for_any_model_start(monkeypatch):
    # A key cooled by an earlier job is reordered to the pool tail at
    # configure time (Batch O semantics, unchanged by Batch U)...
    key_rotation.health_for("gemini").mark_failure("key-two")
    ai = _configure_planner(monkeypatch, _quota)
    assert ai._recap_key_pool_size == 3
    with pytest.raises(planner_mod.GeminiKeyPoolExhaustedError):
        planner_mod._gemini_json(ai, "{}", rotation_tier="full")
    # ...and within EVERY model's key walk the cooled key is tried
    # last but never dropped.
    keys = _keys(ai)
    assert len(keys) == 15
    for block in range(5):
        assert keys[block * 3:(block + 1) * 3] == ["key-one", "key-three", "key-two"]


def test_group_ai_has_no_planner_rotation_threading():
    # Batch U is planner + TTS only: Group AI keeps its own
    # keys-outer failover and gains no model-rotation hint.
    from app.services import group_ai_admin

    src = Path(group_ai_admin.__file__).read_text(encoding="utf-8")
    assert "_recap_begin_gemini_call" not in src
    assert "rotation_tier" not in src


# ── Gemini TTS: per-call key + model rotation ───────────────────────


class _TTSRecorder:
    def __init__(self, behaviour="ok"):
        self.calls: list[tuple[str, str]] = []
        self.behaviour = behaviour

    def client(self, key):
        recorder = self

        class _Models:
            def generate_content(self, model=None, contents=None, config=None):
                recorder.calls.append((key, model))
                if recorder.behaviour == "quota":
                    err = RuntimeError("429 RESOURCE_EXHAUSTED: quota")
                    err.code = 429
                    raise err
                part = SimpleNamespace(inline_data=SimpleNamespace(data=b"\x00\x01" * 100))
                return SimpleNamespace(
                    candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))]
                )

        return SimpleNamespace(models=_Models())


def _tts_engine(tmp_path, monkeypatch, recorder, keys=("tts-key-1", "tts-key-2", "tts-key-3")):
    monkeypatch.setattr(gte, "_client", recorder.client)
    return gte.GeminiTTSEngine(
        ffmpeg="ffmpeg", work_dir=tmp_path, keys=list(keys), log=lambda _m: None
    )


def test_tts_start_key_rotates_per_call(tmp_path, monkeypatch):
    recorder = _TTSRecorder()
    eng = _tts_engine(tmp_path, monkeypatch, recorder)
    for _ in range(3):
        assert eng._ask("prompt", "Charon")
    # One attempt per successful call, each starting at the NEXT key.
    assert [key for key, _model in recorder.calls] == ["tts-key-1", "tts-key-2", "tts-key-3"]


def test_tts_start_model_rotates_per_call(tmp_path, monkeypatch):
    recorder = _TTSRecorder()
    eng = _tts_engine(tmp_path, monkeypatch, recorder)
    for _ in range(3):
        assert eng._ask("prompt", "Charon")
    models = gte.TTS_MODELS
    # Quality Pass §2C item E: lite stays behind primary models while primaries
    # are < 7/10 used, so the primary model leads first.
    primaries = [m for m in models if "lite" not in m]
    assert [model for _key, model in recorder.calls] == [primaries[0]] * 3

    # Once primaries reach >= 7/10 usage on pool keys, lite joins starting rotation.
    health = key_rotation.health_for("gemini_tts")
    health.reset()
    gte._TTS_MODEL_CURSOR = 0
    for k in ("tts-key-1", "tts-key-2", "tts-key-3"):
        for m in primaries:
            health.record_usage(k, m, n=7)
    recorder.calls.clear()
    for _ in range(4):
        assert eng._ask("prompt", "Charon")
    assert [model for _key, model in recorder.calls] == [
        models[0], models[1], models[0], models[1]]


def test_tts_cooled_key_tail_appended_never_dropped(tmp_path, monkeypatch):
    key_rotation.health_for("gemini_tts").mark_failure("tts-key-2")
    recorder = _TTSRecorder(behaviour="quota")
    eng = _tts_engine(tmp_path, monkeypatch, recorder)
    with pytest.raises(gte.GeminiTTSError):
        eng._ask("prompt", "Charon")
    first, _second = gte.TTS_MODELS
    assert recorder.calls[:3] == [
        ("tts-key-1", first), ("tts-key-3", first), ("tts-key-2", first),
    ]
    assert len(recorder.calls) == 3 * len(gte.TTS_MODELS)
    assert sorted(recorder.calls) == sorted(
        (key, model)
        for key in ("tts-key-1", "tts-key-2", "tts-key-3")
        for model in gte.TTS_MODELS
    )
    # Registry separation is kept: TTS quota deaths cool keys ONLY in
    # the "gemini_tts" registry, never in the planner's "gemini" one.
    assert key_rotation.health_for("gemini_tts").in_cooldown("tts-key-1")
    assert not key_rotation.health_for("gemini").in_cooldown("tts-key-1")

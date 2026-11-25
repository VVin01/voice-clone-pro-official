"""Part D (2026-11-25) tests: Recap translation speedup D0-D4 + sub_render_mode timeline default."""
from __future__ import annotations

import inspect
import threading
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import downloader as dl_mod
from app.services import key_rotation as kr
from app.services.recap_engine import recap_pipeline as pipeline_mod
from app.services.recap_engine import recap_planner as planner_mod
from app.webapp import api as api_mod


@pytest.fixture(autouse=True)
def _reset_rotation():
    planner_mod._reset_model_rotation()
    kr.health_for("gemini").reset()
    yield
    planner_mod._reset_model_rotation()
    kr.health_for("gemini").reset()


def test_d0_strided_worker_starts_and_shared_dead_model_keys(monkeypatch):
    monkeypatch.setattr(settings, "gemini_primary_model", "gemini-3.6-flash")
    monkeypatch.setattr(settings, "gemini_fallback_1", "gemini-3.5-flash")
    monkeypatch.setattr(settings, "gemini_fallback_2", "gemini-3.5-flash-lite")
    monkeypatch.setattr(settings, "gemini_fallback_3", "gemini-flash-lite-latest")
    monkeypatch.setenv("GEMINI_API_KEY", "sentinel")

    pool = [(f"K{i}", f"key-{i:02d}") for i in range(16)]
    dead_shared: set[tuple[str, str]] = set()
    dead_lock = threading.Lock()
    attempts: list[tuple[int, str, str]] = []

    def _make_worker(idx: int):
        class _AI:
            def _enable_proxy(self): pass
            def _disable_proxy(self): pass
            def factory(self, *, api_key, http_options=None):
                class _Models:
                    def generate_content(self, model=None, contents=None):
                        attempts.append((idx, api_key, model))
                        if api_key == "key-04" and model == "gemini-3.6-flash":
                            raise RuntimeError("429 RESOURCE_EXHAUSTED: quota")
                        return SimpleNamespace(text='{"ok": true}')
                return SimpleNamespace(models=_Models())

        ai = _AI()
        planner_mod._configure_gemini_key_pool(
            ai,
            list(pool),
            client_factory=ai.factory,
            log=lambda _m: None,
            model_chain=["gemini-3.8-flash"],
            worker_index=idx,
            dead_model_keys=dead_shared,
            dead_lock=dead_lock,
        )
        return ai

    # Reset health cursor between builds to isolate pure worker stride
    w0 = _make_worker(0)
    kr.health_for("gemini").reset()
    w1 = _make_worker(1)
    kr.health_for("gemini").reset()
    w2 = _make_worker(2)

    assert w0.api_key == "key-00"
    assert w1.api_key == "key-04"
    assert w2.api_key == "key-08"

    # Worker 1 starts on gemini-3.6-flash @ key-04 -> gets 429 -> wraps to key-05 & marks (model, key-04) dead
    assert planner_mod._gemini_json(w1, "{}", rotation_tier="full") == {"ok": True}
    assert ("gemini-3.6-flash", "key-04") in dead_shared
    # Health cooldown was scoped per-model (not global key-wide lock across unrelated models)
    h = kr.health_for("gemini")
    assert h.in_cooldown("key-04", model="gemini-3.6-flash")
    assert not h.in_cooldown("key-04", model="gemini-3.5-flash")

    # Worker 1 re-running full call starting at gemini-3.6-flash skips dead key-04 immediately
    planner_mod._reset_model_rotation()
    w1_again = _make_worker(1)
    attempts.clear()
    assert planner_mod._gemini_json(w1_again, "{}", rotation_tier="full") == {"ok": True}
    assert all(k != "key-04" for _idx, k, _m in attempts)


def test_d0_rotate_key_wraps_around_pool_from_strided_offset(monkeypatch):
    monkeypatch.setattr(settings, "gemini_primary_model", "model-a")
    monkeypatch.setattr(settings, "gemini_fallback_1", "model-b")
    monkeypatch.setattr(settings, "gemini_fallback_2", "model-c")
    monkeypatch.setattr(settings, "gemini_fallback_3", "model-d")
    monkeypatch.setenv("GEMINI_API_KEY", "sentinel")

    pool = [(f"K{i}", f"key-{i:02d}") for i in range(6)]
    seen: list[str] = []

    class _AI:
        def _enable_proxy(self): pass
        def _disable_proxy(self): pass
        def factory(self, *, api_key, http_options=None):
            class _Models:
                def generate_content(self, model=None, contents=None):
                    seen.append(api_key)
                    if api_key != "key-02":
                        raise RuntimeError("429 RESOURCE_EXHAUSTED")
                    return SimpleNamespace(text='{"ok": true}')
            return SimpleNamespace(models=_Models())

    ai = _AI()
    planner_mod._configure_gemini_key_pool(
        ai, list(pool), client_factory=ai.factory, log=lambda _m: None,
        model_chain=["gemini-3.8-flash"], worker_index=1,  # offset = 4 % 6 = 4 (key-04)
    )
    assert planner_mod._gemini_json(ai, "{}", rotation_tier="full") == {"ok": True}
    # Started at key-04 -> key-05 -> wrapped around to key-00 -> key-01 -> key-02!
    assert seen == ["key-04", "key-05", "key-00", "key-01", "key-02"]


def test_d0_attempt_budget_and_d1_d2_defaults_and_clamps(monkeypatch, tmp_path):
    assert planner_mod.GEMINI_JSON_MAX_HTTP_ATTEMPTS == 48
    assert planner_mod.TRANSLATE_BATCH_MAX_SPLIT_DEPTH == 3

    monkeypatch.delenv("RECAP_PLANNER_TRANSLATE_WORKERS", raising=False)
    b = planner_mod.RecapPlanBuilder(
        runtime_root=str(tmp_path),
        ffprobe_path="ffprobe",
        log=lambda _m: None,
        progress=lambda _c, _t, _l: None,
        should_stop=lambda: False,
    )
    assert b.translate_workers == 5
    assert b.translation_batch_max_items == 28
    assert b.context_overlap == 6
    assert b.lookahead == 6

    # Cap clamp at 6 & env override honored
    b_capped = planner_mod.RecapPlanBuilder(
        runtime_root=str(tmp_path), ffprobe_path="ffprobe", log=lambda _m: None,
        progress=lambda _c, _t, _l: None, should_stop=lambda: False, translate_workers=99,
    )
    assert b_capped.translate_workers == 6

    # Timeout clamps down to 20s floor and preserves 25s
    b_low = planner_mod.RecapPlanBuilder(
        runtime_root=str(tmp_path), ffprobe_path="ffprobe", log=lambda _m: None,
        progress=lambda _c, _t, _l: None, should_stop=lambda: False, gemini_request_timeout_seconds=10,
    )
    assert b_low.gemini_request_timeout_seconds == 20
    b_25 = planner_mod.RecapPlanBuilder(
        runtime_root=str(tmp_path), ffprobe_path="ffprobe", log=lambda _m: None,
        progress=lambda _c, _t, _l: None, should_stop=lambda: False, gemini_request_timeout_seconds=25,
    )
    assert b_25.gemini_request_timeout_seconds == 25

    captured = {}
    class _AI:
        def _enable_proxy(self): pass
        def _disable_proxy(self): pass
    planner_mod._configure_gemini_key_pool(
        _AI(), [("K1", "dummy")],
        client_factory=lambda *, api_key, http_options: captured.setdefault("http", http_options),
        log=lambda _m: None, request_timeout_seconds=10,
    )
    assert captured["http"]["timeout"] == 20_000

    # Source pins for batch chars=12000 and worker cap 6 in translate_batches_in_parallel
    build_src = inspect.getsource(planner_mod.RecapPlanBuilder.build)
    assert "max_chars=12000" in build_src
    par_src = inspect.getsource(planner_mod.translate_batches_in_parallel)
    assert "min(int(max_workers or 1), 6, len(pending))" in par_src


def test_d1_parallel_translate_honors_5_workers_and_passes_worker_indices(monkeypatch):
    seen_worker_indices: list[int] = []
    units = [
        {"id": i + 1, "timestamp": "00:00:01,000 --> 00:00:02,000", "source_text": f"Line {i}"}
        for i in range(10)
    ]
    batches = [[u] for u in units]
    unit_index = {int(u["id"]): i for i, u in enumerate(units)}

    monkeypatch.setattr(
        planner_mod,
        "translate_batch",
        lambda ai, *, source_title, story_context, previous_context, current, next_context, log, narrative_style="narrator", is_female=False, previous_context_is_source=False: [
            {**current[0], "translation": f"မြန်မာ {current[0]['id']}"}
        ],
    )
    out = planner_mod.translate_batches_in_parallel(
        ai=SimpleNamespace(),
        worker_ai_factory=lambda idx: seen_worker_indices.append(idx) or SimpleNamespace(idx=idx),
        batches=batches,
        pending=list(range(len(batches))),
        units=units,
        unit_index=unit_index,
        initial_translated=[],
        story_context={},
        source_title="S",
        log=lambda _m: None,
        should_stop=lambda: False,
        context_overlap=2,
        lookahead=2,
        translation_audit=False,
        narrative_style="narrator",
        is_female=False,
        max_workers=5,
    )
    assert len(out) == 10
    assert sorted(seen_worker_indices) == [0, 0, 1, 1, 2, 2, 3, 3, 4, 4]


def test_d3_story_context_uses_lite_first_routing(monkeypatch):
    monkeypatch.setattr(settings, "gemini_primary_model", "gemini-3.6-flash")
    monkeypatch.setattr(settings, "gemini_fallback_1", "gemini-3.5-flash")
    monkeypatch.setattr(settings, "gemini_fallback_2", "gemini-3.5-flash-lite")
    monkeypatch.setattr(settings, "gemini_fallback_3", "gemini-flash-lite-latest")
    monkeypatch.setenv("GEMINI_API_KEY", "sentinel")

    models_called: list[str] = []

    class _AI:
        def _enable_proxy(self): pass
        def _disable_proxy(self): pass
        def factory(self, *, api_key, http_options=None):
            class _Models:
                def generate_content(self, model=None, contents=None):
                    models_called.append(model)
                    if "lite" in (model or ""):
                        raise RuntimeError("503 unavailable")
                    return SimpleNamespace(text='{"characters": [], "relationships": [], "places": [], "terms": [], "plot_state": [], "name_consistency": [], "unresolved": []}')
            return SimpleNamespace(models=_Models())

    ai = _AI()
    planner_mod._configure_gemini_key_pool(
        ai, [("K1", "k1")], client_factory=ai.factory, log=lambda _m: None,
        model_chain=["gemini-3.8-flash"],
    )
    planner_mod.build_story_context(
        ai,
        [{"id": 1, "timestamp": "00:00:01,000 --> 00:00:02,000", "source_text": "Hello."}],
        batch_max_items=80,
        log=lambda _m: None,
        should_stop=lambda: False,
        reuse=False,
    )
    # Must try BOTH flash-lite models ahead of full model fallback
    assert models_called[:2] == ["gemini-3.5-flash-lite", "gemini-flash-lite-latest"]
    assert models_called[2] == "gemini-3.6-flash"


def test_d4_backend_sub_render_mode_defaults_to_timeline():
    sig_pipe = inspect.signature(pipeline_mod.run_recap_pipeline)
    assert sig_pipe.parameters["sub_render_mode"].default == "timeline"

    sig_dl = inspect.signature(dl_mod.MediaDownloader.generate_recap_video)
    assert sig_dl.parameters["sub_render_mode"].default == "timeline"

    req = api_mod.RecapConfigRequest(url="https://example.com/v.mp4", duration_seconds=60.0)
    assert req.sub_render_mode == "timeline"

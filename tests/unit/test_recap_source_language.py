"""Tests for source-language selection (production-grade Feature 2)."""
import pytest

from app.services.recap_engine.recap_planner import (
    RecapPlanBuilder,
    normalize_source_language,
)


def test_normalize_auto_variants():
    for v in (None, "", "auto", "AUTO", " detect ", "none", "automatic"):
        assert normalize_source_language(v) == ""


def test_normalize_codes():
    assert normalize_source_language("en") == "en"
    assert normalize_source_language(" EN ") == "en"
    assert normalize_source_language("zh") == "zh"
    assert normalize_source_language("my") == "my"


def test_normalize_rejects_garbage():
    for v in ("english", "e1", "en-US-x", "!!", "klingon"):
        with pytest.raises(ValueError):
            normalize_source_language(v)


def _planner(source_language=""):
    return RecapPlanBuilder(
        runtime_root="/tmp",
        ffprobe_path="ffprobe",
        log=lambda m: None,
        progress=lambda c, t, l: None,
        should_stop=lambda: False,
        source_language=source_language,
    )


def test_planner_stores_language():
    assert _planner("ko").source_language == "ko"
    assert _planner("auto").source_language == ""
    assert _planner("").source_language == ""


def test_planner_rejects_bad_language_at_construction():
    with pytest.raises(ValueError):
        _planner("not-a-language")


def test_pipeline_and_downloader_accept_language_kwarg():
    import inspect

    from app.services.downloader import MediaDownloader
    from app.services.recap_engine.recap_pipeline import (
        build_recap_script_plan,
        run_recap_pipeline,
    )

    for fn in (run_recap_pipeline, build_recap_script_plan, MediaDownloader.generate_recap_video):
        assert "source_language" in inspect.signature(fn).parameters

"""Batch DO — per-model TTS prompt shape + sync-store URL translation
(2026-11-11).

Item 1 — BOTH gemini-3.8 TTS models READ the production
``narrator_prompt`` AUDIO-PROFILE aloud instead of following it
(proven firsthand on the DN deploy: 130+ s of profile-reading per
line, ASR-verified; the preview models follow the same profile
correctly). The 3.8 family now gets the compact
``narrator_prompt_short`` two-line shape — ASR-verified on both 3.8
models to speak only the transcript — while every other model keeps
the full profile. The prompt is built PER MODEL inside ``_ask``.

Item 2 — KeyHealth BotSetting persistence (aj_kh:*) has been
silently dead since the 2026-11-08 Supabase cutover: ``_sync_engine``
swapped the driver to psycopg2 but left the asyncpg-style
``?ssl=require`` query param, which psycopg2 rejects ("invalid
connection option \"ssl\""). ``_to_sync_url`` now translates the
query: ``ssl`` is dropped and ``sslmode`` is set from it.

All clients are fakes — no network, synthetic keys only.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.engine import make_url

from app.services import key_rotation
from app.services.key_rotation import _to_sync_url
from app.services.recap_engine import gemini_tts_engine as gte


@pytest.fixture(autouse=True)
def _reset_state():
    for name in ("gemini", "gemini_tts"):
        key_rotation.health_for(name).reset()
    gte._TTS_MODEL_CURSOR = 0
    yield
    for name in ("gemini", "gemini_tts"):
        key_rotation.health_for(name).reset()
    gte._TTS_MODEL_CURSOR = 0


# ── Item 1: the short prompt builder ────────────────────────────────


def test_short_prompt_exact_shape_male():
    out = gte.narrator_prompt_short("မင်္ဂလာပါ", "Charon")
    assert out == (
        "Narrate as ONE adult male Burmese storyteller — warm, lively, varied pace, "
        "the same voice throughout. Read only the transcript below, exactly once, adding nothing.\n"
        "[expressive storytelling] မင်္ဂလာပါ"
    )
    # Exactly two lines: the instruction line + the tagged transcript.
    assert out.count("\n") == 1


def test_short_prompt_exact_shape_female():
    out = gte.narrator_prompt_short("မင်္ဂလာပါ", "Kore")
    lines = out.split("\n")
    assert lines[0] == (
        "Narrate as ONE adult female Burmese storyteller — warm, lively, varied pace, "
        "the same voice throughout. Read only the transcript below, exactly once, adding nothing."
    )
    assert lines[1] == "[expressive storytelling] မင်္ဂလာပါ"


def test_short_prompt_gender_prefix_and_unknown_voice_default():
    # The gemini: prefix is stripped like the profile builder does,
    # and an unlisted voice falls back to the male wording — the same
    # GEMINI_VOICE_GENDER lookup semantics as narrator_prompt.
    assert gte.narrator_prompt_short("စာသား", "gemini:Kore").startswith(
        "Narrate as ONE adult female")
    assert gte.narrator_prompt_short("စာသား", "gemini:Charon").startswith(
        "Narrate as ONE adult male")


def test_short_prompt_cleaning_matches_profile_cleaning():
    raw = "မင်္ဂလာပါ (Hello)   ကမ္ဘာကြီး"
    short_line = gte.narrator_prompt_short(raw, "Charon").split("\n")[1]
    profile_line = gte.narrator_prompt(raw, "Charon").split("TRANSCRIPT:\n")[1]
    # Both shapes carry the IDENTICAL cleaned, tagged transcript line:
    # gloss parentheses stripped, whitespace collapsed.
    assert short_line == profile_line
    assert "(Hello)" not in short_line


def test_short_prompt_carries_no_profile_or_direction_text():
    out = gte.narrator_prompt_short("စာသား", "Charon")
    assert "AUDIO PROFILE" not in out
    assert "TRANSCRIPT:" not in out
    # The long direction block is intentionally absent (leak risk).
    assert gte.DEFAULT_DIRECTION[:30] not in out


# ── Item 1: per-model prompt selection through _ask ─────────────────


class _Recorder:
    """Fake Gemini client factory: records (key, model, contents)."""

    def __init__(self, behaviour=None):
        self.calls: list[tuple[str, str, str]] = []
        self.behaviour = behaviour  # (key, model) -> Exception | None

    def client(self, key):
        recorder = self

        class _Models:
            def generate_content(self, model=None, contents=None, config=None):
                recorder.calls.append((key, model, contents))
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


def _engine(tmp_path, monkeypatch, recorder, keys=("k1",)):
    from app.config import settings

    # Pin legacy Arm 'a' (Batch DO's two-line narrator_prompt_short shape) for these
    # historical Batch DO tests; Leak Phase 2 flipped the default to 'c1'.
    monkeypatch.setattr(settings, "gemini_tts_prompt_shape", "a")
    gte.reset_prompt_shape_cache()
    monkeypatch.setattr(gte, "_client", recorder.client)
    return gte.GeminiTTSEngine(
        ffmpeg="ffmpeg", work_dir=tmp_path, keys=list(keys),
        log=lambda _m: None)


def test_ask_on_38_flash_sends_the_short_prompt(tmp_path, monkeypatch):
    recorder = _Recorder()
    eng = _engine(tmp_path, monkeypatch, recorder)
    gte._TTS_MODEL_CURSOR = 0  # rotation starts on TTS_MODELS[0]
    eng._ask("မင်္ဂလာပါ", "Charon")
    _key, model, contents = recorder.calls[0]
    assert model == "gemini-3.8-flash-tts"
    assert contents.startswith("Narrate as ONE")
    assert "AUDIO PROFILE" not in contents
    assert contents == gte.narrator_prompt_short("မင်္ဂလာပါ", "Charon")


def test_ask_on_38_lite_sends_the_short_prompt(tmp_path, monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(gte, "_effective_tts_models", lambda: ("gemini-3.8-flash-lite-tts",))
    eng = _engine(tmp_path, monkeypatch, recorder)
    eng._ask("မင်္ဂလာပါ", "Kore")
    _key, model, contents = recorder.calls[0]
    assert model == "gemini-3.8-flash-lite-tts"
    assert contents.startswith("Narrate as ONE adult female")
    assert "AUDIO PROFILE" not in contents


def test_ask_on_preview_sends_the_full_profile(tmp_path, monkeypatch):
    recorder = _Recorder()
    monkeypatch.setattr(
        gte,
        "_effective_tts_models",
        lambda: ("gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview"),
    )
    eng = _engine(tmp_path, monkeypatch, recorder)
    gte._TTS_MODEL_CURSOR = 1  # rotation starts on preview model[1]
    eng._ask("မင်္ဂလာပါ", "Charon")
    _key, model, contents = recorder.calls[0]
    assert model == "gemini-3.1-flash-tts-preview"
    assert "AUDIO PROFILE" in contents
    assert contents == gte.narrator_prompt("မင်္ဂလာပါ", "Charon", eng.direction)


def test_ask_rebuilds_the_prompt_when_failing_over_to_a_preview(
        tmp_path, monkeypatch):
    # The 3.8 head dies on quota; the SAME call walks on to the 3.1
    # preview — each model must receive ITS OWN prompt shape.
    def quota_on_38(_key, model):
        if model.startswith("gemini-3.8"):
            err = RuntimeError("429 RESOURCE_EXHAUSTED: quota")
            err.code = 429
            return err
        return None

    recorder = _Recorder(behaviour=quota_on_38)
    monkeypatch.setattr(
        gte,
        "_effective_tts_models",
        lambda: ("gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview"),
    )
    eng = _engine(tmp_path, monkeypatch, recorder)
    gte._TTS_MODEL_CURSOR = 0
    data = eng._ask("မင်္ဂလာပါ", "Charon")
    assert data
    by_model = {model: contents for _k, model, contents in recorder.calls}
    assert by_model["gemini-3.8-flash-tts"].startswith("Narrate as ONE")
    assert "AUDIO PROFILE" not in by_model["gemini-3.8-flash-tts"]
    assert "AUDIO PROFILE" in by_model["gemini-3.1-flash-tts-preview"]


# ── Item 2: _to_sync_url ─────────────────────────────────────────────


def _query(url: str) -> dict:
    return dict(make_url(url).query)


def test_to_sync_url_ssl_require_becomes_sslmode_require():
    out = _to_sync_url("postgresql+asyncpg://u:p@h:5432/db?ssl=require")
    assert out.startswith("postgresql+psycopg2://")
    assert _query(out) == {"sslmode": "require"}


def test_to_sync_url_ssl_disable_becomes_sslmode_disable():
    out = _to_sync_url("postgresql+asyncpg://u:p@h:5432/db?ssl=disable")
    assert _query(out) == {"sslmode": "disable"}


def test_to_sync_url_ssl_variants_map_through():
    for raw, want in (("true", "require"), ("prefer", "prefer"),
                      ("verify-ca", "verify-ca"),
                      ("verify-full", "verify-full")):
        out = _to_sync_url(f"postgresql+asyncpg://u:p@h/db?ssl={raw}")
        assert _query(out) == {"sslmode": want}


def test_to_sync_url_unknown_ssl_defaults_to_require():
    out = _to_sync_url("postgresql+asyncpg://u:p@h/db?ssl=bogus")
    assert _query(out) == {"sslmode": "require"}


def test_to_sync_url_existing_sslmode_wins_and_ssl_is_dropped():
    out = _to_sync_url(
        "postgresql+asyncpg://u:p@h/db?ssl=require&sslmode=verify-full")
    assert _query(out) == {"sslmode": "verify-full"}


def test_to_sync_url_preserves_other_query_params():
    out = _to_sync_url("postgres+asyncpg://u:p@h/db?ssl=true&application_name=svc")
    assert out.startswith("postgresql+psycopg2://")
    assert _query(out) == {"application_name": "svc", "sslmode": "require"}


def test_to_sync_url_sqlite_passthrough():
    assert _to_sync_url("sqlite+aiosqlite:///./app.db") == "sqlite:///./app.db"


def test_to_sync_url_no_query_postgres_changes_driver_only():
    assert _to_sync_url("postgresql+asyncpg://u:p@h:5432/db") == \
        "postgresql+psycopg2://u:p@h:5432/db"

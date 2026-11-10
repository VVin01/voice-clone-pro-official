"""Batch CE (2026-11-07) — TTS Gemini→Edge auto-fallback.

Pins:
* exhaustion-class Gemini failures ONLY (no keys, every key
  dead/exhausted, the engine's fail-fast state) re-synthesise the
  WHOLE job on the Edge twin of the picked voice — partial Gemini
  chunks are discarded, voices never mix in one file;
* a non-exhaustion Gemini error still fails the job (no fallback);
  both engines failing = FAILED, nothing consumed;
* the job row keeps the PICKED engine/voice (History truth, BK
  split) while the delivery caption names the ACTUALLY-used Edge
  engine/voice and gains the i18n fallback note;
* the daily TTS cap counts a fallback job ONCE, by delivered
  duration (executor end-to-end);
* exactly one structured log line per fallback, marker
  ``TTS fallback: gemini->edge``;
* the synth helper serves ONLY the TTS job executor — no preview /
  pre-checkout path routes through it.
"""
from __future__ import annotations

import inspect
import logging
import math
import shutil
import struct
import wave
from pathlib import Path

import pytest
import pytest_asyncio

import app.services.tts_service as tts_service
from app.config import settings
from app.models.schema import Job
from app.services.i18n import t
from app.services.recap_engine.gemini_tts_engine import (
    GEMINI_VOICE_GENDER,
    GEMINI_VOICE_NAMES,
    GeminiTTSError,
)
from app.services.tts_service import (
    EDGE_TTS_VOICES,
    _is_gemini_exhaustion,
    edge_voice_key_for_gemini_voice,
)

pytestmark_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not available",
)

ROOT = Path(__file__).resolve().parents[2]

# The engine's REAL exhaustion messages (gemini_tts_engine.py).
MSG_ALL_KEYS = (
    "Gemini TTS: all keys' quota is exhausted or unavailable for today — "
    "remaining lines will use the persona engine instead. (fake detail)"
)
MSG_FAIL_FAST = "Gemini TTS exhausted for this run — persona fallback."
MSG_ATTEMPT_BUDGET = (
    "Gemini TTS: attempt budget exhausted (24 requests for one line) — "
    "remaining lines will use the persona engine instead. (fake detail)"
)
MSG_NO_KEY = (
    "Gemini TTS: no Gemini API key configured "
    "(GEMINI_API_KEY / GEMINI_API_KEYS)."
)
MSG_LOCATION = (
    "Gemini TTS: this server location is not supported by Gemini TTS — "
    "narration lines will use the persona engine instead."
)


def _write_wav(path: Path, seconds: float = 0.5, freq: int = 440) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 24000
    frames = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(int(rate * seconds))
    )
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return path


class _FakeGemini:
    behaviour = "ok"
    calls = 0
    voices: list = []

    def __init__(self, **kw):
        self.work_dir = Path(kw["work_dir"])
        self.exhausted = False

    def generate_channel99_voice(self, text, voice):
        cls = type(self)
        cls.calls += 1
        cls.voices.append(voice)
        b = cls.behaviour
        if b == "exhaust":
            self.exhausted = True
            raise GeminiTTSError(MSG_ALL_KEYS)
        if b == "second_exhaust":
            if cls.calls >= 2:
                self.exhausted = True
                raise GeminiTTSError(MSG_FAIL_FAST)
        elif b == "failfast":
            self.exhausted = True
            raise GeminiTTSError(MSG_FAIL_FAST)
        elif b == "unknown_voice":
            raise GeminiTTSError(
                f"Unknown Gemini voice '{voice}'. Use one of: {', '.join(GEMINI_VOICE_NAMES)}"
            )
        elif b == "generic":
            raise RuntimeError("synthesis exploded")
        elif b == "empty":
            p = self.work_dir / "tiny.wav"
            p.write_bytes(b"xx")
            return p
        return _write_wav(self.work_dir / f"gem_{cls.calls}.wav", freq=660)


class _FakeEdge:
    behaviour = "ok"
    calls = 0
    voices: list = []

    def __init__(self, **kw):
        self.work_dir = Path(kw["work_dir"])

    def generate_channel99_voice(self, text, voice):
        cls = type(self)
        cls.calls += 1
        cls.voices.append(voice)
        if cls.behaviour == "fail":
            raise RuntimeError("edge exploded")
        # 1.6 s so the executor's int(round(duration)) settle is > 0.
        return _write_wav(self.work_dir / f"edge_{cls.calls}.wav", seconds=1.6, freq=440)


@pytest.fixture
def engines(monkeypatch):
    import app.services.recap_engine.core as core_mod
    import app.services.recap_engine.gemini_tts_engine as gem_mod

    _FakeGemini.calls = 0
    _FakeGemini.voices = []
    _FakeGemini.behaviour = "ok"
    _FakeEdge.calls = 0
    _FakeEdge.voices = []
    _FakeEdge.behaviour = "ok"
    monkeypatch.setattr(gem_mod, "GeminiTTSEngine", _FakeGemini)
    monkeypatch.setattr(gem_mod, "collect_gemini_keys", lambda: ["fake-key"])
    monkeypatch.setattr(core_mod, "EdgeTTSEngine", _FakeEdge)
    return _FakeGemini, _FakeEdge


def _job(**over) -> Job:
    text = over.pop("input_text", None) or ("က" * 350 + "။ " + "ခ" * 350 + "။")
    job = Job(
        id=over.pop("id", "ce-job-1"),
        user_id=777,
        chat_id=777,
        job_type="TTS",
        engine="gemini",
        input_text=text,
        preset_voice="Kore",
        edge_voice=None,
        speed_preset="+12%",
        status="QUEUED",
    )
    job.tts_fallback_used = False
    for k, v in over.items():
        setattr(job, k, v)
    return job


# ── Trigger set: the classifier ──────────────────────────────────────

def test_ce_classifier_accepts_the_engines_real_exhaustion_errors():
    assert _is_gemini_exhaustion(GeminiTTSError(MSG_ALL_KEYS))
    assert _is_gemini_exhaustion(GeminiTTSError(MSG_FAIL_FAST))
    assert _is_gemini_exhaustion(GeminiTTSError(MSG_ATTEMPT_BUDGET))
    assert _is_gemini_exhaustion(GeminiTTSError(MSG_NO_KEY))
    assert _is_gemini_exhaustion(GeminiTTSError(MSG_LOCATION))
    # tts_service's own no-keys raise is a plain RuntimeError.
    assert _is_gemini_exhaustion(
        RuntimeError("Gemini TTS: no Gemini API key configured.")
    )


def test_ce_classifier_rejects_non_exhaustion_errors():
    assert not _is_gemini_exhaustion(
        GeminiTTSError("Unknown Gemini voice 'X'. Use one of: Charon")
    )
    assert not _is_gemini_exhaustion(RuntimeError("synthesis exploded"))
    assert not _is_gemini_exhaustion(
        RuntimeError("TTS engine returned no usable audio.")
    )
    assert not _is_gemini_exhaustion(ValueError("EMPTY_TEXT"))


def test_ce_classifier_trusts_the_engine_exhausted_flag():
    class _Eng:
        exhausted = True

    assert _is_gemini_exhaustion(GeminiTTSError("anything"), _Eng())


# ── Voice mapping ────────────────────────────────────────────────────

def test_ce_every_gemini_voice_maps_to_its_edge_twin():
    for name in GEMINI_VOICE_NAMES:
        key = edge_voice_key_for_gemini_voice(name)
        expected = "nilar" if GEMINI_VOICE_GENDER[name] == "female" else "thiha"
        assert key == expected, name
        assert EDGE_TTS_VOICES[key] in (
            "my-MM-ThihaNeural",
            "my-MM-NilarNeural",
        )
    # Unmapped names default to thiha; the gemini: prefix is tolerated.
    assert edge_voice_key_for_gemini_voice("NoSuchVoice") == "thiha"
    assert edge_voice_key_for_gemini_voice(None) == "thiha"
    assert edge_voice_key_for_gemini_voice("gemini:Kore") == "nilar"
    assert edge_voice_key_for_gemini_voice("Charon") == "thiha"


# ── Synthesis behaviour ──────────────────────────────────────────────

@pytestmark_ffmpeg
def test_ce_exhaustion_resynthesises_the_whole_job_on_the_edge_twin(
    tmp_path, monkeypatch, engines, caplog
):
    gem, edge = engines
    gem.behaviour = "exhaust"
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    job = _job()  # Kore (female), two chunks
    with caplog.at_level(logging.WARNING, logger="app.services.tts_service"):
        out_path, duration = tts_service._synthesize_tts_audio_sync(job)
    assert out_path.is_file()
    assert duration > 0
    # Gemini was tried once (first chunk); Edge re-made BOTH chunks on
    # the female twin — no Gemini part survives into the output.
    assert gem.calls == 1
    assert edge.calls == 2
    assert edge.voices == ["my-MM-NilarNeural", "my-MM-NilarNeural"]
    # Truth stamps on the row; picked values untouched.
    assert job.tts_fallback_used is True
    assert job.edge_voice == "my-MM-NilarNeural"
    assert job.engine == "gemini"
    assert job.preset_voice == "Kore"
    markers = [
        r for r in caplog.records if "TTS fallback: gemini->edge" in r.message
    ]
    assert len(markers) == 1
    assert "ce-job-1" in markers[0].message
    assert "pool_exhausted" in markers[0].message


@pytestmark_ffmpeg
def test_ce_partial_gemini_progress_is_discarded_on_fallback(
    tmp_path, monkeypatch, engines
):
    gem, edge = engines
    gem.behaviour = "second_exhaust"  # chunk 1 OK, chunk 2 fail-fast
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    job = _job()
    out_path, _ = tts_service._synthesize_tts_audio_sync(job)
    assert out_path.is_file()
    assert gem.calls == 2
    # The whole job was re-made on Edge — both chunks, not just the
    # failed remainder.
    assert edge.calls == 2
    assert job.tts_fallback_used is True


@pytestmark_ffmpeg
def test_ce_no_keys_configured_falls_back(tmp_path, monkeypatch, engines):
    gem, edge = engines
    import app.services.recap_engine.gemini_tts_engine as gem_mod

    monkeypatch.setattr(gem_mod, "collect_gemini_keys", lambda: [])
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    job = _job(preset_voice="Charon")  # male → thiha
    out_path, _ = tts_service._synthesize_tts_audio_sync(job)
    assert out_path.is_file()
    assert gem.calls == 0
    assert edge.calls == 2
    assert edge.voices == ["my-MM-ThihaNeural"] * 2
    assert job.tts_fallback_used is True


@pytestmark_ffmpeg
def test_ce_non_exhaustion_gemini_error_fails_without_fallback(
    tmp_path, monkeypatch, engines
):
    gem, edge = engines
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    for behaviour in ("generic", "unknown_voice", "empty"):
        gem.behaviour = behaviour
        gem.calls = 0
        edge.calls = 0
        job = _job(id=f"ce-job-{behaviour}")
        with pytest.raises(Exception):
            tts_service._synthesize_tts_audio_sync(job)
        assert edge.calls == 0, behaviour
        assert job.tts_fallback_used is False, behaviour


@pytestmark_ffmpeg
def test_ce_both_engines_failing_fails_the_job(tmp_path, monkeypatch, engines):
    gem, edge = engines
    gem.behaviour = "exhaust"
    edge.behaviour = "fail"
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    job = _job()
    with pytest.raises(RuntimeError):
        tts_service._synthesize_tts_audio_sync(job)
    assert job.tts_fallback_used is False


@pytestmark_ffmpeg
def test_ce_normal_gemini_job_never_touches_edge(
    tmp_path, monkeypatch, engines, caplog
):
    gem, edge = engines
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    job = _job()
    with caplog.at_level(logging.WARNING, logger="app.services.tts_service"):
        out_path, _ = tts_service._synthesize_tts_audio_sync(job)
    assert out_path.is_file()
    assert gem.calls == 2
    assert edge.calls == 0
    assert job.tts_fallback_used is False
    assert not [
        r for r in caplog.records if "TTS fallback: gemini->edge" in r.message
    ]


# ── Delivery caption truth ───────────────────────────────────────────

class _FakeBot:
    sent: list = []

    def __init__(self, token=None, **kw):
        self.session = self

    async def close(self):
        pass

    async def send_audio(self, **kwargs):
        type(self).sent.append(kwargs)


@pytest.fixture
def _caption_env(monkeypatch, tmp_path):
    import aiogram

    _FakeBot.sent = []
    monkeypatch.setattr(aiogram, "Bot", _FakeBot)
    monkeypatch.setattr(settings, "bot_token", "123456:fake")
    out = tmp_path / "out.wav"
    _write_wav(out)
    return out


@pytest.mark.asyncio
async def test_ce_fallback_caption_names_edge_and_carries_the_note(
    _caption_env, monkeypatch
):
    # Owner language load is best-effort; force its fallback ("my").
    import app.db.session as session_mod

    def _boom():
        raise RuntimeError("no db in this test")

    monkeypatch.setattr(session_mod, "AsyncSessionLocal", _boom)
    job = _job(preset_voice="Kore", edge_voice="my-MM-NilarNeural")
    job.tts_fallback_used = True
    await tts_service._deliver_tts_to_chat(job, _caption_env)
    assert len(_FakeBot.sent) == 1
    caption = _FakeBot.sent[0]["caption"]
    # The engine line names the used engine + twin voice, never Gemini
    # (the note line itself mentions Gemini — it explains the switch).
    assert "Edge · မနီလာ" in caption
    assert "Gemini ·" not in caption
    assert t("tts_fallback_note", "my") in caption


@pytest.mark.asyncio
async def test_ce_normal_caption_has_no_fallback_note(_caption_env, monkeypatch):
    import app.db.session as session_mod

    def _boom():
        raise RuntimeError("no db in this test")

    monkeypatch.setattr(session_mod, "AsyncSessionLocal", _boom)
    job = _job(preset_voice="Kore")
    await tts_service._deliver_tts_to_chat(job, _caption_env)
    caption = _FakeBot.sent[0]["caption"]
    assert "Gemini" in caption
    assert t("tts_fallback_note", "my") not in caption


def test_ce_fallback_note_i18n_both_languages():
    my = t("tts_fallback_note", "my")
    en = t("tts_fallback_note", "en")
    assert my != "tts_fallback_note" and en != "tts_fallback_note"
    assert "Gemini" in my and "Edge" in my
    assert "Gemini" in en and "Edge" in en


# ── Executor: cap counted once by delivered duration ─────────────────

@pytest_asyncio.fixture
async def _exec_db(monkeypatch, tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.base import Base
    import app.db.session as session_mod

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(session_mod, "AsyncSessionLocal", maker)
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "bot_token", "")
    yield maker
    await engine.dispose()


@pytestmark_ffmpeg
@pytest.mark.asyncio
async def test_ce_executor_counts_a_fallback_job_once(_exec_db, engines):
    gem, edge = engines
    gem.behaviour = "exhaust"
    maker = _exec_db
    from sqlalchemy import select

    from app.models.schema import Job as JobModel
    from app.services.ledger import get_or_create_user

    async with maker() as session:
        user, _ = await get_or_create_user(session, 909001)
        await session.commit()
        job, reason = await tts_service.create_tts_job(
            session, user.id,
            text="မင်္ဂလာပါ။", engine="gemini", voice="Kore",
            speed_rate="+12%",
        )
        await session.commit()
        assert job is not None, reason
        job_id = job.id

    await tts_service.execute_tts_job(job_id)

    async with maker() as session:
        row = (
            await session.execute(select(JobModel).where(JobModel.id == job_id))
        ).scalar_one()
        assert row.status == "SUCCESS"
        assert row.tts_fallback_used is True
        assert row.engine == "gemini"  # picked values kept for History
        assert row.preset_voice == "Kore"
        assert row.edge_voice == "my-MM-NilarNeural"
        assert row.seconds_consumed > 0
        consumed = await tts_service.tts_cap_consumed_seconds(session, row.user_id)
        # Counted exactly once, by the delivered duration.
        assert consumed == pytest.approx(float(row.seconds_consumed))


# ── Scope pins ───────────────────────────────────────────────────────

def test_ce_synth_helper_serves_only_the_job_executor():
    src = inspect.getsource(tts_service.execute_tts_job)
    assert "_synthesize_tts_audio_sync" in src
    api_src = (ROOT / "app" / "webapp" / "api.py").read_text(encoding="utf-8")
    assert "_synthesize_tts_audio_sync" not in api_src


def test_ce_cache_token_is_r103():
    html = (ROOT / "app" / "webapp" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert html.count("20261104_r127") == 4
    assert ("20261104_r" + "103") not in html


def test_ce_fallback_flag_column_and_migrations_exist():
    schema_src = (ROOT / "app" / "models" / "schema.py").read_text(encoding="utf-8")
    assert "tts_fallback_used" in schema_src
    session_src = (ROOT / "app" / "db" / "session.py").read_text(encoding="utf-8")
    # Postgres IF NOT EXISTS + the SQLite pragma-guarded pair.
    assert session_src.count("tts_fallback_used") >= 3

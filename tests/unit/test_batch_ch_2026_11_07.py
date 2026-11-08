"""Batch CH (2026-11-07): chunked voice-clone synthesis + no voice
minute charge on Edge fallback.

Diagnosis pinned by these tests: the voice path used to send the
whole text to VoxCPM in ONE call; the model's total-input cap is
2,096 tokens and Burmese costs several tokens per character, so long
texts (or long custom references) failed on every Space and the job
was delivered in an Edge persona voice while marked successful.

CH Item 1: the worker plans sentence-aligned chunks whose estimated
total input stays under a 1,700-token budget; chunks are synthesized
with allow_fallback=False (a chunk can never silently switch voice);
a chunk failure propagates to the worker's existing whole-job
fallback; short texts keep the exact pre-CH single call.

CH Item 2: a voice job delivered via Edge fallback deducts NO voice
balance and does not count toward the Batch AN daily voice-minutes
cap (the queue counting skips SUCCESS rows marked edge_fallback —
their seconds_consumed is 0, which the estimate fallback would
otherwise resurrect).
"""
from __future__ import annotations

import logging
import shutil
import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.enums import JobStatus, JobType, LedgerDirection
from app.models.schema import Job, MinuteLedgerEvent, VoiceModel
from app.services.audio import concat_wav_files
from app.services.ledger import get_minute_balance, get_or_create_user
from app.services.queue import create_voice_clone_job
from app.services.voice_chunking import (
    INPUT_TOKEN_BUDGET,
    MIN_CHUNK_CHARS,
    MODEL_TOKEN_CAP,
    chunk_char_budget,
    estimate_tokens,
    estimated_input_tokens,
    plan_text_chunks,
    split_sentences,
)
from app.worker import process_single_job


# ---------------------------------------------------------------------------
# Chunker (pure)
# ---------------------------------------------------------------------------

_SENT = "မင်္ဂလာပါ၊ ဒါက အသံစမ်းသပ်မှု စာသားတစ်ခု ဖြစ်ပါတယ်။ "  # ~55 chars


def test_ch_estimate_tokens_uses_burmese_factor():
    assert estimate_tokens("") == 0
    assert estimate_tokens("က" * 100) == 220
    assert estimate_tokens("က" * 101) == 223  # ceil(101 × 2.2)


def test_ch_split_sentences_keeps_endings():
    text = "ပထမ စာကြောင်းပါ။ ဒုတိယ စာကြောင်းပါ! တတိယ\nနောက်ဆုံး အပိုင်း"
    assert split_sentences(text) == [
        "ပထမ စာကြောင်းပါ။",
        "ဒုတိယ စာကြောင်းပါ!",
        "တတိယ",
        "နောက်ဆုံး အပိုင်း",
    ]


def test_ch_short_text_single_chunk_identity():
    text = "မင်္ဂလာပါ၊ စမ်းသပ်မှု တစ်ခုပါ။"
    assert plan_text_chunks(text) == [text]


def test_ch_sentence_packing_under_budget():
    text = (_SENT * 40).strip()  # ~2,200 chars of whole sentences
    chunks = plan_text_chunks(text)
    assert len(chunks) >= 3
    budget = chunk_char_budget()
    for chunk in chunks:
        assert len(chunk) <= budget
        # Never split mid-sentence when avoidable: whole sentences only.
        assert chunk.endswith("။")
        assert estimated_input_tokens(chunk) <= INPUT_TOKEN_BUDGET
    # No content lost: the chunks re-join to the original sentences.
    assert " ".join(chunks).split() == text.split()


def test_ch_overlong_sentence_hard_split_at_clause_marks():
    clause = "အပိုင်းခွဲ စမ်းသပ်စာသား ဆက်တိုက် ရေးထားတာ ဖြစ်ပါတယ်၊ "
    sentence = (clause * 30).strip() + "。"
    assert len(sentence) > chunk_char_budget()
    chunks = plan_text_chunks(sentence)
    assert len(chunks) >= 3
    budget = chunk_char_budget()
    for chunk in chunks[:-1]:
        assert len(chunk) <= budget
        # Clause marks were available, so splits land on them.
        assert chunk.endswith("၊") or chunk.endswith("။")


def test_ch_overlong_sentence_without_marks_split_by_length():
    text = "က" * 2000
    chunks = plan_text_chunks(text)
    budget = chunk_char_budget()
    assert len(chunks) >= 3
    assert all(len(c) <= budget for c in chunks)
    assert "".join(chunks) == text


def test_ch_budget_shrinks_with_long_reference():
    base = chunk_char_budget()
    with_audio = chunk_char_budget(reference_seconds=45.0)
    with_transcript = chunk_char_budget(reference_transcript="က" * 300)
    assert with_audio < base
    assert with_transcript < base
    # 45 s of reference audio + a 300-char transcript overruns the
    # whole budget on overhead alone -> the floor engages.
    floored = chunk_char_budget(
        reference_transcript="က" * 300, reference_seconds=45.0
    )
    assert floored == MIN_CHUNK_CHARS


def test_ch_floor_chunk_stays_under_real_model_cap():
    """Even when the floor engages, a floored chunk's estimated input
    stays under the model's real 2,096-token cap for realistic
    overheads (45 s reference + a long English control)."""
    control = "x" * 230
    chunk = "က" * MIN_CHUNK_CHARS
    est = estimated_input_tokens(
        chunk, control_instruction=control, reference_seconds=45.0
    )
    assert est <= MODEL_TOKEN_CAP


def test_ch_every_chunk_within_budget_across_scenarios():
    text = (_SENT * 40).strip()
    for kwargs in (
        {},
        {"reference_seconds": 10.0},
        {"reference_transcript": "က" * 100},
        {"reference_seconds": 20.0, "control_instruction": "x" * 200},
    ):
        chunks = plan_text_chunks(text, **kwargs)
        assert len(chunks) >= 2
        for chunk in chunks:
            assert estimated_input_tokens(chunk, **kwargs) <= INPUT_TOKEN_BUDGET


# ---------------------------------------------------------------------------
# Concat helper (real ffmpeg, skipped when unavailable)
# ---------------------------------------------------------------------------


def _write_wav(path: Path, seconds: float, rate: int = 48000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
def test_ch_concat_wav_files_joins_in_order(tmp_path):
    p1 = _write_wav(tmp_path / "a.wav", 1.0)
    p2 = _write_wav(tmp_path / "b.wav", 0.5)
    out = concat_wav_files([p1, p2], tmp_path / "joined.wav")
    with wave.open(str(out), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getframerate() == 48000
        assert abs(wf.getnframes() / wf.getframerate() - 1.5) < 0.05


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg unavailable")
def test_ch_concat_wav_files_mixed_rates_keep_first_rate(tmp_path):
    p1 = _write_wav(tmp_path / "a.wav", 0.5, rate=44100)
    p2 = _write_wav(tmp_path / "b.wav", 0.5, rate=48000)
    out = concat_wav_files([p1, p2], tmp_path / "joined.wav")
    with wave.open(str(out), "rb") as wf:
        assert wf.getframerate() == 44100
        assert abs(wf.getnframes() / wf.getframerate() - 1.0) < 0.05


# ---------------------------------------------------------------------------
# Worker path (fake engine)
# ---------------------------------------------------------------------------


class _FakeEngine:
    """Stands in for VoxCPMEngine: records every synthesize call and
    writes a small real WAV per call. Class-level knobs let a test
    make a chosen call raise, or mark the internal fallback flag."""

    instances: list["_FakeEngine"] = []
    fail_on_call: int | None = None
    internal_fallback: bool = False

    def __init__(self, work_dir=None, **kwargs):
        self.work_dir = work_dir
        self.last_fallback_used = False
        self.last_reference_trimmed = False
        self.calls: list[dict] = []
        _FakeEngine.instances.append(self)

    def synthesize(self, **kwargs):
        self.calls.append(kwargs)
        if _FakeEngine.fail_on_call == len(self.calls):
            raise RuntimeError("VoxCPM space blew up mid-job")
        out = Path(kwargs["output_wav"])
        _write_wav(out, 1.0)
        if _FakeEngine.internal_fallback:
            self.last_fallback_used = True
        return out


@pytest.fixture(autouse=True)
def _reset_fake_engine():
    _FakeEngine.instances = []
    _FakeEngine.fail_on_call = None
    _FakeEngine.internal_fallback = False
    yield


async def _make_user_model_job(db_session, tmp_path, *, telegram_id, text,
                               welcome=300):
    user, _ = await get_or_create_user(db_session, telegram_id=telegram_id)
    bal = await get_minute_balance(db_session, user.id)
    bal.welcome_seconds = welcome

    pth_file = tmp_path / "CHModel.pth"
    pth_file.write_bytes(b"pth_content")
    model = VoiceModel(
        name="CHModel",
        slug=f"system-chmodel-{telegram_id}",
        type="SYSTEM",
        model_dir=str(tmp_path),
        pth_path=str(pth_file),
        status="ACTIVE",
        is_default_free=True,
    )
    db_session.add(model)
    await db_session.flush()

    source = _write_wav(tmp_path / "sample.wav", 12.0)
    job, reason = await create_voice_clone_job(
        session=db_session,
        user_id=user.id,
        text=text,
        edge_voice="my-MM-ThihaNeural",
        model_id=model.id,
        chat_id=telegram_id,
        status_message_id=888,
        source_audio_path=str(source),
    )
    assert job is not None, f"job creation failed: {reason}"
    await db_session.commit()
    return user, job, source


def _duration_side_effect(source: Path, output_seconds: int):
    def _fn(path):
        return 6 if Path(path) == source else output_seconds

    return _fn


def _clone_balance_total(bal) -> int:
    """Clone-spendable seconds (consume_seconds' cascade buckets)."""
    return (
        int(bal.monthly_free_seconds or 0)
        + int(bal.welcome_seconds or 0)
        + int(bal.purchased_seconds or 0)
    )


def _persona_ok(text, persona_id, output_wav, **kwargs):
    out = Path(output_wav)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(b"fake_fallback_wav")
    return out


@pytest.mark.asyncio
async def test_ch_worker_multi_chunk_success(db_session, tmp_path, caplog):
    """A ~900-char job (over the chunk budget once the reference and
    control overheads are counted) is synthesized in several chunks —
    same reference every time, allow_fallback=False, concatenated in
    order — and billed normally by delivered duration."""
    text = (_SENT * 17).strip()
    user, job, source = await _make_user_model_job(
        db_session, tmp_path, telegram_id=7201, text=text
    )
    before_total = _clone_balance_total(await get_minute_balance(db_session, user.id))
    concat_calls: list[list[Path]] = []

    def fake_concat(parts, output):
        concat_calls.append(list(parts))
        return _write_wav(Path(output), 1.0)

    mock_bot = AsyncMock()
    with (
        patch("app.services.voxcpm_engine.VoxCPMEngine", _FakeEngine),
        patch("app.worker.concat_wav_files", side_effect=fake_concat),
        patch(
            "app.worker.get_wav_duration_seconds",
            side_effect=_duration_side_effect(source, 90),
        ),
        caplog.at_level(logging.INFO, logger="app.worker"),
    ):
        success = await process_single_job(session=db_session, job=job, bot=mock_bot)

    assert success is True
    assert job.status == JobStatus.SUCCESS.value
    assert job.engine == "voxcpm"
    engine = _FakeEngine.instances[-1]
    assert len(engine.calls) >= 2, "long text must be chunked"
    for call in engine.calls:
        assert call["allow_fallback"] is False
        assert Path(call["reference_wav"]) == source
    # Chunks partition the text, in order.
    assert " ".join(c["text"] for c in engine.calls).split() == text.split()
    # Concat received exactly the chunk parts, in order.
    assert len(concat_calls) == 1
    part_names = [p.name for p in concat_calls[0]]
    assert part_names == sorted(part_names)
    assert len(part_names) == len(engine.calls)
    # Per-job chunk marker logged at INFO.
    assert f"Voice clone chunked synthesis: job={job.id} chunks={len(engine.calls)}" in caplog.text
    # Genuine clone success: billed by delivered duration.
    assert job.seconds_consumed == 90
    bal = await get_minute_balance(db_session, user.id)
    assert _clone_balance_total(bal) == before_total - 90


@pytest.mark.asyncio
async def test_ch_worker_chunk_failure_whole_job_fallback_no_charge(
    db_session, tmp_path
):
    """Chunk 2 of a multi-chunk job raises -> NO partial concat is
    delivered; the worker's existing whole-job Edge fallback produces
    the audio, and the fallback delivery deducts NOTHING."""
    text = (_SENT * 17).strip()
    user, job, source = await _make_user_model_job(
        db_session, tmp_path, telegram_id=7202, text=text
    )
    before_total = _clone_balance_total(await get_minute_balance(db_session, user.id))
    _FakeEngine.fail_on_call = 2
    concat_calls: list = []

    mock_bot = AsyncMock()
    with (
        patch("app.services.voxcpm_engine.VoxCPMEngine", _FakeEngine),
        patch(
            "app.worker.concat_wav_files",
            side_effect=lambda parts, output: concat_calls.append(parts),
        ),
        patch(
            "app.services.curated_voices.synthesize_curated_persona",
            side_effect=_persona_ok,
        ),
        patch(
            "app.worker.get_wav_duration_seconds",
            side_effect=_duration_side_effect(source, 90),
        ),
    ):
        success = await process_single_job(session=db_session, job=job, bot=mock_bot)

    assert success is True
    assert job.status == JobStatus.SUCCESS.value
    assert job.engine == "edge_fallback"
    assert concat_calls == [], "a failed chunk must never reach concat"
    # Item 2: no deduction, no cap consumption recorded.
    assert job.seconds_consumed == 0
    bal = await get_minute_balance(db_session, user.id)
    assert _clone_balance_total(bal) == before_total
    debits = (
        await db_session.execute(
            select(MinuteLedgerEvent).where(
                MinuteLedgerEvent.user_id == user.id,
                MinuteLedgerEvent.reference_id == str(job.id),
                MinuteLedgerEvent.direction == LedgerDirection.DEBIT.value,
            )
        )
    ).scalars().all()
    assert debits == []


@pytest.mark.asyncio
async def test_ch_worker_short_text_single_call_unchanged(db_session, tmp_path):
    """Regression pin: a short text stays ONE engine call with the
    exact pre-CH argument shape (no allow_fallback kwarg) and normal
    billing."""
    user, job, source = await _make_user_model_job(
        db_session, tmp_path, telegram_id=7203, text="မင်္ဂလာပါ၊ စမ်းသပ်မှု တစ်ခုပါ။"
    )
    before_total = _clone_balance_total(await get_minute_balance(db_session, user.id))
    mock_bot = AsyncMock()
    with (
        patch("app.services.voxcpm_engine.VoxCPMEngine", _FakeEngine),
        patch(
            "app.worker.get_wav_duration_seconds",
            side_effect=_duration_side_effect(source, 10),
        ),
    ):
        success = await process_single_job(session=db_session, job=job, bot=mock_bot)

    assert success is True
    engine = _FakeEngine.instances[-1]
    assert len(engine.calls) == 1
    assert "allow_fallback" not in engine.calls[0]
    assert job.seconds_consumed == 10
    bal = await get_minute_balance(db_session, user.id)
    assert _clone_balance_total(bal) == before_total - 10


@pytest.mark.asyncio
async def test_ch_worker_engine_internal_fallback_no_charge(db_session, tmp_path):
    """The common production path: the engine falls back internally
    (last_fallback_used) on a short job. The delivery still succeeds
    with the fallback marking — but deducts nothing (Item 2)."""
    user, job, source = await _make_user_model_job(
        db_session, tmp_path, telegram_id=7204, text="မင်္ဂလာပါ၊ စမ်းသပ်မှု တစ်ခုပါ။"
    )
    before_total = _clone_balance_total(await get_minute_balance(db_session, user.id))
    _FakeEngine.internal_fallback = True
    mock_bot = AsyncMock()
    with (
        patch("app.services.voxcpm_engine.VoxCPMEngine", _FakeEngine),
        patch(
            "app.worker.get_wav_duration_seconds",
            side_effect=_duration_side_effect(source, 10),
        ),
    ):
        success = await process_single_job(session=db_session, job=job, bot=mock_bot)

    assert success is True
    assert job.engine == "edge_fallback"
    assert job.seconds_consumed == 0
    bal = await get_minute_balance(db_session, user.id)
    assert _clone_balance_total(bal) == before_total


# ---------------------------------------------------------------------------
# Release pin: cache token r105
# ---------------------------------------------------------------------------


def test_ch_cache_token_is_r105():
    from pathlib import Path

    html = (
        Path(__file__).resolve().parents[2] / "app/webapp/static/index.html"
    ).read_text(encoding="utf-8")
    assert html.count("20261104_r117") == 4
    assert ("20261104_r" + "105") not in html


# ---------------------------------------------------------------------------
# Queue daily-cap counting (Item 2, Batch AN site)
# ---------------------------------------------------------------------------


async def _seed_completed_job(db_session, user_id, model_id, *, engine,
                              input_text):
    from datetime import datetime, timezone

    job = Job(
        id=f"ch-seed-{user_id}-{engine}",
        user_id=user_id,
        chat_id=user_id,
        input_text=input_text,
        edge_voice="my-MM-ThihaNeural",
        model_id=model_id,
        status=JobStatus.SUCCESS.value,
        job_type=JobType.VOICE_CLONE.value,
        engine=engine,
        seconds_consumed=0,
        speed_preset="normal",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(job)
    await db_session.flush()
    return job


@pytest.mark.asyncio
async def test_ch_fallback_success_row_excluded_from_daily_cap(
    db_session, tmp_path, monkeypatch
):
    """A SUCCESS row marked edge_fallback with seconds_consumed=0 and
    a LONG text must not eat the daily voice-minutes cap — without
    the CH skip, the counting's estimate fallback would resurrect it
    as ~80 s and block the next job under a 1-minute cap."""
    monkeypatch.setattr(settings, "voice_daily_limit_minutes_free", 1)
    user, _ = await get_or_create_user(db_session, telegram_id=7205)
    model = VoiceModel(
        name="CHCapModel", slug="system-chcap", type="SYSTEM",
        model_dir=str(tmp_path), pth_path="voxcpm", status="ACTIVE",
        is_default_free=True, allowed_for_free=True,
        allowed_for_purchased=True,
    )
    db_session.add(model)
    await db_session.flush()
    await _seed_completed_job(
        db_session, user.id, model.id,
        engine="edge_fallback", input_text="က" * 1200,
    )
    job, reason = await create_voice_clone_job(
        session=db_session, user_id=user.id, text="hello",
        edge_voice="my-MM-ThihaNeural", model_id=model.id,
    )
    assert job is not None
    assert reason == "QUEUED"


@pytest.mark.asyncio
async def test_ch_genuine_success_zero_record_still_counts_estimate(
    db_session, tmp_path, monkeypatch
):
    """Control case: a SUCCESS row NOT marked edge_fallback with
    seconds_consumed=0 keeps the pre-CH behaviour — the submit-time
    estimate counts — so the same setup DOES block. This pins that
    the exclusion is the edge_fallback marking, not the zero."""
    monkeypatch.setattr(settings, "voice_daily_limit_minutes_free", 1)
    user, _ = await get_or_create_user(db_session, telegram_id=7206)
    model = VoiceModel(
        name="CHCapModel2", slug="system-chcap2", type="SYSTEM",
        model_dir=str(tmp_path), pth_path="voxcpm", status="ACTIVE",
        is_default_free=True, allowed_for_free=True,
        allowed_for_purchased=True,
    )
    db_session.add(model)
    await db_session.flush()
    await _seed_completed_job(
        db_session, user.id, model.id,
        engine="voxcpm", input_text="က" * 1200,
    )
    job, reason = await create_voice_clone_job(
        session=db_session, user_id=user.id, text="hello",
        edge_voice="my-MM-ThihaNeural", model_id=model.id,
    )
    assert job is None
    assert reason == "DAILY_MINUTE_LIMIT_EXCEEDED"

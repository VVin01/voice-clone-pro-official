"""Batch CW (2026-11-09): digit -> Myanmar number-words normalisation
for voice synthesis.

Owner report: the voice clone read digit sequences (e.g. "1234",
"၂၀၂၆") with English pronunciation inside Burmese narration, because
VoxCPM speaks digit strings the way its training data pronounces
them. Gemini/Edge voices were not affected.

CW Item 1: ``app.services.number_words.normalize_numbers_for_speech``
rewrites digit runs as Myanmar number words (magnitudes, connector
forms, decimals, percents, comma groups, digit-by-digit for
phone-length runs), with guards that leave Latin-glued identifiers,
model names and URLs untouched.

CW Item 2: the rewrite is applied to the SYNTHESIS text only —
``process_single_job`` normalizes its ``clean_text`` upstream of the
Batch CH chunker (so chunk budgets are computed on the normalized
text) and upstream of the worker's Edge-fallback persona call (which
receives the same normalized text); the recap VoxCPM choke point
``_vox_synthesize_with_retry`` normalizes the line it hands the
engine. ``job.input_text`` — what validation counted, billing
charged and History shows — is never rewritten.
"""
from __future__ import annotations

import wave
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from app.models.enums import JobStatus
from app.models.schema import VoiceModel
from app.services.ledger import get_minute_balance, get_or_create_user
from app.services.number_words import int_to_myanmar_words, normalize_numbers_for_speech
from app.services.queue import create_voice_clone_job
from app.worker import process_single_job

N = normalize_numbers_for_speech


# ---------------------------------------------------------------------------
# CW Item 1 — the number table
# ---------------------------------------------------------------------------

_INT_TABLE = {
    0: "သုည",
    5: "ငါး",
    9: "ကိုး",
    10: "တစ်ဆယ်",
    11: "တစ်ဆယ့်တစ်",
    15: "တစ်ဆယ့်ငါး",
    20: "နှစ်ဆယ်",
    99: "ကိုးဆယ့် ကိုး",
    100: "တစ်ရာ",
    101: "တစ်ရာ့ တစ်",
    110: "တစ်ရာ့ တစ်ဆယ်",
    115: "တစ်ရာ့ တစ်ဆယ့်ငါး",
    999: "ကိုးရာ့ ကိုးဆယ့် ကိုး",
    1000: "တစ်ထောင်",
    1001: "တစ်ထောင့် တစ်",
    1234: "တစ်ထောင့် နှစ်ရာ့ သုံးဆယ့် လေး",
    10000: "တစ်သောင်း",
    15000: "တစ်သောင်း ငါးထောင်",
    20000: "နှစ်သောင်း",
    100000: "တစ်သိန်း",
    123456: "တစ်သိန်း နှစ်သောင်း သုံးထောင် လေးရာ့ ငါးဆယ့် ခြောက်",
    999999: "ကိုးသိန်း ကိုးသောင်း ကိုးထောင် ကိုးရာ့ ကိုးဆယ့် ကိုး",
    1000000: "တစ်သန်း",
    10000000: "တစ်ကုဋေ",
    25000000: "နှစ်ကုဋေ ငါးသန်း",
}


@pytest.mark.parametrize("n,words", sorted(_INT_TABLE.items()))
def test_cw_int_to_myanmar_words_table(n, words):
    assert int_to_myanmar_words(n) == words


@pytest.mark.parametrize(
    "n,words", [(n, w) for n, w in sorted(_INT_TABLE.items()) if n < 1_000_000]
)
def test_cw_normalize_integer_strings_table(n, words):
    assert N(str(n)) == words


def test_cw_plain_seven_plus_digit_runs_are_identifiers():
    # The same values as PLAIN (comma-free) strings are >= 7-digit
    # runs, so the digit-by-digit rule governs them; their full
    # magnitude readings live in the comma-grouped forms (tested in
    # test_cw_comma_grouped_quantities) and in int_to_myanmar_words.
    assert N("1000000") == "တစ် သုည သုည သုည သုည သုည သုည"
    assert N("10000000") == "တစ် သုည သုည သုည သုည သုည သုည သုည"
    assert N("25000000") == "နှစ် ငါး သုည သုည သုည သုည သုည သုည"


def test_cw_decimals():
    assert N("3.5") == "သုံး ဒသမ ငါး"
    assert N("3.14") == "သုံး ဒသမ တစ် လေး"
    assert N("0.5") == "သုည ဒသမ ငါး"
    assert N("12.05") == "တစ်ဆယ့်နှစ် ဒသမ သုည ငါး"


def test_cw_percent():
    assert N("50%") == "ငါးဆယ် ရာခိုင်နှုန်း"
    assert N("50 %") == "ငါးဆယ် ရာခိုင်နှုန်း"
    assert N("100%") == "တစ်ရာ ရာခိုင်နှုန်း"
    assert N("3.5%") == "သုံး ဒသမ ငါး ရာခိုင်နှုန်း"


def test_cw_comma_grouped_quantities():
    assert N("20,000") == "နှစ်သောင်း"
    assert N("1,000,000") == "တစ်သန်း"
    assert N("25,000,000") == "နှစ်ကုဋေ ငါးသန်း"
    assert N("1,234") == "တစ်ထောင့် နှစ်ရာ့ သုံးဆယ့် လေး"


def test_cw_burmese_digits_and_mixed_runs():
    assert N("၂၀၂၆") == "နှစ်ထောင့် နှစ်ဆယ့် ခြောက်"
    assert N("၂0၂6") == "နှစ်ထောင့် နှစ်ဆယ့် ခြောက်"  # mixed scripts, one run
    assert N("၅၀%") == "ငါးဆယ် ရာခိုင်နှုန်း"
    assert N("အပိုင်း၁") == "အပိုင်းတစ်"  # glued to Burmese still converts


def test_cw_long_runs_read_digit_by_digit():
    assert N("099784306181") == (
        "သုည ကိုး ကိုး ခုနှစ် ရှစ် လေး သုံး သုည ခြောက် တစ် ရှစ် တစ်"
    )
    # Exactly 7 plain digits is already an identifier, not a quantity.
    assert N("1234567") == "တစ် နှစ် သုံး လေး ငါး ခြောက် ခုနှစ်"
    # Six digits still convert in full.
    assert N("123456") == _INT_TABLE[123456]


def test_cw_guards_latin_glued():
    for text in ("Ep1", "S02", "1080p", "v2", "B2B", "abc123", "123abc"):
        assert N(text) == text


def test_cw_guards_hyphenated_names():
    assert N("gemini-3.5-flash") == "gemini-3.5-flash"
    assert N("abc-123") == "abc-123"
    assert N("123-abc") == "123-abc"
    # A bare numeric range has no Latin neighbour: both sides convert.
    assert N("1-2") == "တစ်-နှစ်"


def test_cw_guards_urls_and_emails():
    assert N("https://example.com/ep2026") == "https://example.com/ep2026"
    assert N("www.example2026.com") == "www.example2026.com"
    assert N("user2026@example.com") == "user2026@example.com"
    # A number OUTSIDE the address token still converts.
    assert N("2026 မှာ www.example.com ကိုကြည့်ပါ") == (
        "နှစ်ထောင့် နှစ်ဆယ့် ခြောက် မှာ www.example.com ကိုကြည့်ပါ"
    )


def test_cw_mixed_burmese_sentence():
    src = "ကျွန်တော် 2026 ခုနှစ်မှာ 15000 ကျပ် ရခဲ့ပြီး 50% ကို စုခဲ့တယ်"
    assert N(src) == (
        "ကျွန်တော် နှစ်ထောင့် နှစ်ဆယ့် ခြောက် ခုနှစ်မှာ တစ်သောင်း ငါးထောင် "
        "ကျပ် ရခဲ့ပြီး ငါးဆယ် ရာခိုင်နှုန်း ကို စုခဲ့တယ်"
    )


_IDEMPOTENCY_CORPUS = [
    "1234",
    "၂၀၂၆ ခုနှစ်မှာ 20,000 ကျပ်",
    "3.14 နဲ့ 50% တိုးတက်မှု",
    "ဖုန်း 099784306181 ကို ဆက်ပါ",
    "Ep1 နဲ့ gemini-3.5-flash က မပြောင်းပါ",
    "မင်္ဂလာပါ ခင်ဗျာ",
    "",
    "%",
    ".5",
    "1.",
]


def test_cw_idempotent():
    for text in _IDEMPOTENCY_CORPUS:
        once = N(text)
        assert N(once) == once


def test_cw_passthrough_and_graceful_degradation():
    assert N("") == ""
    assert N("မင်္ဂလာပါ ခင်ဗျာ") == "မင်္ဂလာပါ ခင်ဗျာ"
    assert N("%") == "%"
    assert N(".5") == ".ငါး"
    assert N("1.") == "တစ်."
    assert N("12,34") == "တစ်ဆယ့်နှစ်,သုံးဆယ့် လေး"  # broken grouping degrades


# ---------------------------------------------------------------------------
# CW Item 2 — clone path integration (mirrors the Batch CH harness)
# ---------------------------------------------------------------------------


def _write_wav(path: Path, seconds: float, rate: int = 48000) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(rate * seconds))
    return path


class _FakeEngine:
    """VoxCPM stand-in: records every synthesize call, writes a real
    WAV per call, and can be told to fail (fallback test)."""

    instances: list["_FakeEngine"] = []
    fail: bool = False

    def __init__(self, work_dir=None, **kwargs):
        self.work_dir = work_dir
        self.last_fallback_used = False
        self.last_reference_trimmed = False
        self.calls: list[dict] = []
        _FakeEngine.instances.append(self)

    def synthesize(self, **kwargs):
        self.calls.append(kwargs)
        if _FakeEngine.fail:
            raise RuntimeError("VoxCPM space blew up")
        out = Path(kwargs["output_wav"])
        _write_wav(out, 1.0)
        return out


@pytest.fixture(autouse=True)
def _reset_fake_engine():
    _FakeEngine.instances = []
    _FakeEngine.fail = False
    yield


async def _make_user_model_job(db_session, tmp_path, *, telegram_id, text,
                               welcome=300):
    user, _ = await get_or_create_user(db_session, telegram_id=telegram_id)
    bal = await get_minute_balance(db_session, user.id)
    bal.welcome_seconds = welcome

    pth_file = tmp_path / "CWModel.pth"
    pth_file.write_bytes(b"pth_content")
    model = VoiceModel(
        name="CWModel",
        slug=f"system-cwmodel-{telegram_id}",
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


_ORIGINAL_TEXT = "ကျွန်တော် 2026 ခုနှစ်မှာ 15000 ကျပ် ရခဲ့တယ်"
_NORMALIZED_TEXT = (
    "ကျွန်တော် နှစ်ထောင့် နှစ်ဆယ့် ခြောက် ခုနှစ်မှာ တစ်သောင်း ငါးထောင် ကျပ် ရခဲ့တယ်"
)


def _duration_side_effect(source: Path, output_seconds: int):
    def _fn(path):
        return 6 if Path(path) == source else output_seconds

    return _fn


@pytest.mark.asyncio
async def test_cw_clone_synthesis_receives_normalized_text(db_session, tmp_path):
    """The engine is handed the number-words text; the stored job
    text (History/display) keeps the user's original digits."""
    user, job, source = await _make_user_model_job(
        db_session, tmp_path, telegram_id=9201, text=_ORIGINAL_TEXT
    )
    mock_bot = AsyncMock()
    with (
        patch("app.services.voxcpm_engine.VoxCPMEngine", _FakeEngine),
        patch(
            "app.worker.get_wav_duration_seconds",
            side_effect=_duration_side_effect(source, 30),
        ),
    ):
        result = await process_single_job(session=db_session, job=job, bot=mock_bot)

    assert result is True
    assert job.status == JobStatus.SUCCESS.value
    engine = _FakeEngine.instances[-1]
    assert engine.calls, "engine was never called"
    spoken = "".join(call["text"] for call in engine.calls)
    assert spoken == _NORMALIZED_TEXT
    assert not any(ch.isdigit() for ch in spoken)
    # The stored/display text is untouched.
    assert job.input_text == _ORIGINAL_TEXT


@pytest.mark.asyncio
async def test_cw_clone_edge_fallback_receives_same_normalized_text(
    db_session, tmp_path
):
    """When VoxCPM fails, the worker's persona fallback gets the SAME
    normalized text (normalize once, upstream of both paths)."""
    _FakeEngine.fail = True
    user, job, source = await _make_user_model_job(
        db_session, tmp_path, telegram_id=9202, text=_ORIGINAL_TEXT
    )
    persona_texts: list[str] = []

    def _persona_recording(text, persona_id, output_wav, **kwargs):
        persona_texts.append(text)
        out = Path(output_wav)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake_fallback_wav")
        return out

    mock_bot = AsyncMock()
    with (
        patch("app.services.voxcpm_engine.VoxCPMEngine", _FakeEngine),
        patch(
            "app.worker.get_wav_duration_seconds",
            side_effect=_duration_side_effect(source, 30),
        ),
        patch(
            "app.services.curated_voices.synthesize_curated_persona",
            _persona_recording,
        ),
    ):
        result = await process_single_job(session=db_session, job=job, bot=mock_bot)

    assert result is True
    assert job.status == JobStatus.SUCCESS.value
    assert persona_texts == [_NORMALIZED_TEXT]
    assert job.input_text == _ORIGINAL_TEXT


# ---------------------------------------------------------------------------
# CW Item 2 — recap VoxCPM choke point
# ---------------------------------------------------------------------------


def test_cw_recap_vox_path_normalizes_but_caller_line_untouched(tmp_path):
    """_vox_synthesize_with_retry hands the ENGINE normalized text;
    the caller's shared speak line (Gemini/Edge paths) is not the
    function's to mutate — it rebinds its local copy only."""
    from app.services.recap_engine.core import EdgeSmartSync

    received: list[str] = []

    class _FakeVoxEngine:
        def synthesize(self, **kwargs):
            received.append(kwargs["text"])
            return kwargs["output_wav"]

    engine = EdgeSmartSync.__new__(EdgeSmartSync)
    engine.log = lambda msg: None
    speak = "2026 ခုနှစ်မှာ လူ 15000 လာကြည့်တယ်"
    produced, err = engine._vox_synthesize_with_retry(
        _FakeVoxEngine(),
        speak=speak,
        dst=tmp_path / "seg.wav",
        ctrl_prompt="",
        persona_ref_wav=None,
        persona_ref_text="",
        preset_v=None,
        speed_float=1.0,
    )
    assert err is None
    assert produced == tmp_path / "seg.wav"
    assert received == [
        "နှစ်ထောင့် နှစ်ဆယ့် ခြောက် ခုနှစ်မှာ လူ တစ်သောင်း ငါးထောင် လာကြည့်တယ်"
    ]
    assert speak == "2026 ခုနှစ်မှာ လူ 15000 လာကြည့်တယ်"

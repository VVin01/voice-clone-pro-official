"""Zawgyi -> Unicode conversion at intake (deep-hunt #4 A1).

Before this fix there was NO encoding conversion anywhere: text typed
on a Zawgyi keyboard was stored as-is, spoken garbled by the TTS, and
still charged. ``app/services/myanmar_text.py`` now converts at every
intake point (voice job service choke point, bot text handler, both
Mini App voice endpoints, recap title at submit, recap script edits).

Test-vector approach (the heart of this file):

- BOTH sides of every vector are built from explicit codepoints, so
  the tests never depend on how Myanmar glyphs happened to be typed
  into this file.
- The Zawgyi sides were hand-derived from the documented Zawgyi
  storage rules (pre-posed e/RA, postfix kinzi U+1064, stack glyphs
  U+1060-U+1085, asat U+1039, medial codepoint shifts, contextual
  U+1033/U+1034, fused U+105A/U+108B forms), cross-checked against an
  independent rule-based Unicode->Zawgyi encoder and against real
  Zawgyi sample strings (Google's myanmar-tools compatibility data).
- The Unicode-preservation side is guarded three ways: every vector's
  Unicode form must pass through byte-identical, every string in the
  product's own Burmese locale file must pass through byte-identical,
  and a list of tricky genuine-Unicode words (kinzi, stacks, U+103F,
  onset+coda clusters) must pass through byte-identical. Corrupting
  proper Unicode would be worse than missing a conversion.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.models.enums import VoiceModelStatus, VoiceModelType
from app.models.schema import VoiceModel
from app.services.ledger import get_or_create_user
from app.services.myanmar_text import convert_zawgyi_to_unicode, is_probably_zawgyi
from app.services.queue import create_voice_clone_job
from app.services.recap_engine.script_editing import (
    apply_script_edits,
    clean_edited_narration,
)
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]


def U(*cps: int) -> str:
    return "".join(chr(c) for c in cps)


# (label, unicode codepoints, zawgyi codepoints)
VECTORS = [
    (
        "myanmar-language: pre-posed RA + coda asat",
        (0x1019, 0x103C, 0x1014, 0x103A, 0x1019, 0x102C, 0x1018, 0x102C, 0x101E, 0x102C),
        (0x103B, 0x1019, 0x1014, 0x1039, 0x1019, 0x102C, 0x1018, 0x102C, 0x101E, 0x102C),
    ),
    (
        "thank-you: pre-posed e, YA shift, nga coda, tall AA",
        (0x1000, 0x103B, 0x1031, 0x1038, 0x1007, 0x1030, 0x1038, 0x1010, 0x1004, 0x103A, 0x1015, 0x102B, 0x1010, 0x101A, 0x103A),
        (0x1031, 0x1000, 0x103A, 0x1038, 0x1007, 0x1030, 0x1038, 0x1010, 0x1004, 0x1039, 0x1015, 0x102B, 0x1010, 0x101A, 0x1039),
    ),
    (
        "mingalabar: postfix kinzi, tall AA",
        (0x1019, 0x1004, 0x103A, 0x1039, 0x1002, 0x101C, 0x102C, 0x1015, 0x102B),
        (0x1019, 0x1002, 0x1064, 0x101C, 0x102C, 0x1015, 0x102B),
    ),
    (
        "kinzi on a YA cluster (kinzi stored right after the base)",
        (0x101E, 0x1004, 0x103A, 0x1039, 0x1001, 0x103B, 0x102D, 0x102F, 0x1004, 0x103A, 0x1038),
        (0x101E, 0x1001, 0x1064, 0x103A, 0x102D, 0x102F, 0x1004, 0x1039, 0x1038),
    ),
    (
        "kinzi on a YA cluster (alternative storage: medials first)",
        (0x101E, 0x1004, 0x103A, 0x1039, 0x1001, 0x103B, 0x102D, 0x102F, 0x1004, 0x103A, 0x1038),
        (0x101E, 0x1001, 0x103A, 0x1064, 0x102D, 0x102F, 0x1004, 0x1039, 0x1038),
    ),
    (
        "kyauung: U+107E great-RA variant + pre-posed e + dot-below",
        (0x1000, 0x103C, 0x1031, 0x102C, 0x1004, 0x1037, 0x103A),
        (0x1031, 0x107E, 0x1000, 0x102C, 0x1004, 0x1037, 0x1039),
    ),
    (
        "two medials RA+WA (U+107E form)",
        (0x1000, 0x103C, 0x103D, 0x101A, 0x103A, 0x101D),
        (0x107E, 0x1000, 0x103C, 0x101A, 0x1039, 0x101D),
    ),
    (
        "two medials RA+WA (U+103B form)",
        (0x1000, 0x103C, 0x103D, 0x101A, 0x103A, 0x101D),
        (0x103B, 0x1000, 0x103C, 0x101A, 0x1039, 0x101D),
    ),
    (
        "metta: stacked TA as one codepoint",
        (0x1019, 0x1031, 0x1010, 0x1039, 0x1010, 0x102C),
        (0x1031, 0x1019, 0x1010, 0x1071, 0x102C),
    ),
    (
        "stacked KA + contextual UU U+1034",
        (0x1005, 0x1000, 0x1039, 0x1000, 0x1030),
        (0x1005, 0x1000, 0x1060, 0x1034),
    ),
    (
        "medial YA + contextual U U+1033",
        (0x1000, 0x103B, 0x102F, 0x1015, 0x103A),
        (0x1000, 0x103A, 0x1033, 0x1015, 0x1039),
    ),
    (
        "great-sa U+103F stored as U+1086",
        (0x101D, 0x102D, 0x1015, 0x103F, 0x1014, 0x102C),
        (0x101D, 0x102D, 0x1015, 0x1086, 0x1014, 0x102C),
    ),
    (
        "stacked DHA",
        (0x101E, 0x1013, 0x1039, 0x1013, 0x102C),
        (0x101E, 0x1013, 0x1076, 0x102C),
    ),
    (
        "stacked DHA after a vowel sign",
        (0x1017, 0x102F, 0x1013, 0x1039, 0x1013),
        (0x1017, 0x102F, 0x1013, 4214),  # last value is the stacked-DHA glyph, decimal
    ),
    (
        "fused tall-AA + asat U+105A",
        (0x1015, 0x1031, 0x102B, 0x103A),
        (0x1031, 0x1015, 0x105A),
    ),
    (
        "fused kinzi+i U+108B (syllable fragment, rule-derived)",
        (0x1021, 0x1004, 0x103A, 0x1039, 0x1002, 0x102D),
        (0x1021, 0x1002, 0x108B),
    ),
    (
        "full sentence (ha/wa shifts, codas, pre-posed e)",
        (
            0x101C, 0x1030, 0x1010, 0x102D, 0x102F, 0x1004, 0x103A, 0x1038, 0x1019, 0x103E, 0x102C, 0x20,
            0x1021, 0x1001, 0x103D, 0x1004, 0x1037, 0x103A, 0x1021, 0x101B, 0x1031, 0x1038, 0x20,
            0x101B, 0x102D, 0x1015, 0x102B, 0x1010, 0x101A, 0x103A,
        ),
        (
            0x101C, 0x1030, 0x1010, 0x102D, 0x102F, 0x1004, 0x1039, 0x1038, 0x1019, 0x103D, 0x102C, 0x20,
            0x1021, 0x1001, 0x103C, 0x1004, 0x1037, 0x1039, 0x1021, 0x1031, 0x101B, 0x1038, 0x20,
            0x101B, 0x102D, 0x1015, 0x102B, 0x1010, 0x101A, 0x1039,
        ),
    ),
]

_READABLE = {
    "မြန်မာဘာသာ": (0x1019, 0x103C, 0x1014, 0x103A, 0x1019, 0x102C, 0x1018, 0x102C, 0x101E, 0x102C),
    "ကျေးဇူးတင်ပါတယ်": (0x1000, 0x103B, 0x1031, 0x1038, 0x1007, 0x1030, 0x1038, 0x1010, 0x1004, 0x103A, 0x1015, 0x102B, 0x1010, 0x101A, 0x103A),
    "မင်္ဂလာပါ": (0x1019, 0x1004, 0x103A, 0x1039, 0x1002, 0x101C, 0x102C, 0x1015, 0x102B),
}


def test_readable_literals_match_their_codepoints():
    """Guard the guard: the readable literals used across this suite
    really are the Unicode strings the codepoint vectors describe."""
    for literal, cps in _READABLE.items():
        assert literal == U(*cps)


@pytest.mark.parametrize("label,uni_cps,zg_cps", VECTORS, ids=[v[0] for v in VECTORS])
def test_zawgyi_vectors_convert_to_exact_unicode(label, uni_cps, zg_cps):
    uni, zg = U(*uni_cps), U(*zg_cps)
    assert is_probably_zawgyi(zg), label
    assert convert_zawgyi_to_unicode(zg) == uni, label


@pytest.mark.parametrize("label,uni_cps,zg_cps", VECTORS, ids=[v[0] for v in VECTORS])
def test_unicode_side_of_every_vector_is_untouched(label, uni_cps, zg_cps):
    uni = U(*uni_cps)
    assert not is_probably_zawgyi(uni), label
    assert convert_zawgyi_to_unicode(uni) == uni, label


@pytest.mark.parametrize("label,uni_cps,zg_cps", VECTORS, ids=[v[0] for v in VECTORS])
def test_conversion_is_idempotent(label, uni_cps, zg_cps):
    once = convert_zawgyi_to_unicode(U(*zg_cps))
    assert convert_zawgyi_to_unicode(once) == once, label


def _locale_strings() -> list[str]:
    data = json.loads((ROOT / "locales" / "my" / "messages.json").read_text(encoding="utf-8"))
    out: list[str] = []

    def walk(o):
        if isinstance(o, str):
            out.append(o)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    return out


def test_every_burmese_locale_string_passes_byte_identical():
    strings = _locale_strings()
    assert len(strings) > 300  # the corpus really is the product copy
    for s in strings:
        assert not is_probably_zawgyi(s), s
        assert convert_zawgyi_to_unicode(s) == s, s


# Genuine Unicode that a naive converter would corrupt: kinzi words,
# stacked consonants, U+103F, onset+coda clusters, tall-AA + asat.
TRICKY_UNICODE = [
    "မင်္ဂလာပါ", "သင်္ချိုင်း", "သင်္ကြန်", "အင်္ဂလိပ်", "မန္တလေး",
    "တက္ကသိုလ်", "ဗုဒ္ဓဟူး", "ဝိပဿနာ", "ပြဿနာ", "သဒ္ဒါ",
    "ကျက်", "စက်တင်ဘာ", "လွှတ်", "မြန်မာနိုင်ငံ", "ခင်ဗျား",
    "ကျေးဇူးတင်ပါတယ်", "ပေါ်", "ကြောင့်", "လူတိုင်းမှာ အခွင့်အရေး ရှိပါတယ်",
]


def test_tricky_genuine_unicode_words_pass_byte_identical():
    for word in TRICKY_UNICODE:
        assert convert_zawgyi_to_unicode(word) == word, word


def test_unicode_with_zero_width_space_passes_byte_identical():
    s = "မြန်မာ\u200bဘာသာ"
    assert convert_zawgyi_to_unicode(s) == s


def test_mixed_zawgyi_english_digits_converts_only_myanmar():
    zg_word = U(0x1000, 0x103A, 0x1015, 0x1039)  # ZG storage of "kyat"
    mixed = f"Price 5000 Ks {zg_word} / {U(*VECTORS[0][2])} OK!"
    expected = f"Price 5000 Ks {U(0x1000, 0x103B, 0x1015, 0x103A)} / {U(*VECTORS[0][1])} OK!"
    assert convert_zawgyi_to_unicode(mixed) == expected


def test_non_myanmar_inputs_unchanged():
    for s in ["", "Hello world! 123", "🎙✨🔥", "https://example.com/v?id=42", "  spaces  "]:
        assert convert_zawgyi_to_unicode(s) == s
        assert not is_probably_zawgyi(s)


def test_ambiguous_and_short_inputs_follow_the_conservative_pins():
    # A lone Zawgyi-only codepoint: fewer than 2 Myanmar characters,
    # so the gate stays closed and nothing is rewritten.
    assert convert_zawgyi_to_unicode("\u1064") == "\u1064"
    # Myanmar digits carry no encoding signal: never converted.
    assert convert_zawgyi_to_unicode("၁၂၃၄၅") == "၁၂၃၄၅"
    # The Unicode kinzi trigraph on its own is valid Unicode: untouched.
    assert convert_zawgyi_to_unicode(U(0x1004, 0x103A, 0x1039)) == U(0x1004, 0x103A, 0x1039)
    # Unicode "ke" (base + post-posed e) is untouched...
    assert convert_zawgyi_to_unicode(U(0x1000, 0x1031)) == U(0x1000, 0x1031)
    # ...while the Zawgyi storage of the same syllable (pre-posed e,
    # two Myanmar characters) IS converted.
    assert convert_zawgyi_to_unicode(U(0x1031, 0x1000)) == U(0x1000, 0x1031)
    # Base + postfix kinzi (two Myanmar characters) converts.
    assert convert_zawgyi_to_unicode(U(0x1000, 0x1064)) == U(0x1004, 0x103A, 0x1039, 0x1000)
    # A single stack glyph after its base converts.
    assert convert_zawgyi_to_unicode(U(0x1000, 0x1060)) == U(0x1000, 0x1039, 0x1000)


# ── Intake wiring ─────────────────────────────────────────────────────────

ZG_THANK_YOU = U(*VECTORS[1][2])
UNI_THANK_YOU = U(*VECTORS[1][1])
ZG_MINGALABAR = U(*VECTORS[2][2])
UNI_MINGALABAR = U(*VECTORS[2][1])


async def _make_user(db_session, telegram_id, **fields):
    user, _ = await get_or_create_user(db_session, telegram_id=telegram_id)
    for key, value in fields.items():
        setattr(user, key, value)
    await db_session.commit()
    return user


async def _seed_voice_model(db_session):
    db_session.add(VoiceModel(
        name="VoxCPM Cloud", slug="voxcpm-cloud-ai",
        type=VoiceModelType.SYSTEM.value, owner_user_id=None,
        model_dir="cloud", pth_path="voxcpm",
        status=VoiceModelStatus.ACTIVE.value,
    ))
    await db_session.commit()


@pytest.mark.asyncio
async def test_voice_job_stores_the_unicode_form(db_session):
    user = await _make_user(db_session, 883001)
    await _seed_voice_model(db_session)
    job, reason = await create_voice_clone_job(
        db_session, user.id, text=ZG_THANK_YOU, preset_voice="komin"
    )
    assert reason == "QUEUED" and job is not None
    assert job.input_text == UNI_THANK_YOU


@pytest.mark.asyncio
async def test_voice_length_limit_counts_the_converted_text(db_session, monkeypatch):
    """Conversion runs BEFORE the character-limit check: this Zawgyi
    text is 7 chars raw but 9 once converted, so a limit of 8 must
    reject it — the counted length is the spoken length."""
    from app.config import settings

    user = await _make_user(db_session, 883002)
    await _seed_voice_model(db_session)
    assert len(ZG_MINGALABAR) == 7 and len(UNI_MINGALABAR) == 9
    monkeypatch.setattr(settings, "character_limit_free", 8)
    job, reason = await create_voice_clone_job(
        db_session, user.id, text=ZG_MINGALABAR, preset_voice="komin"
    )
    assert job is None
    assert reason == "TEXT_EXCEEDS_CHARACTER_LIMIT"


def test_script_edit_saves_unicode():
    assert clean_edited_narration(ZG_THANK_YOU) == UNI_THANK_YOU
    plan = {
        "planner_mode": "test",
        "segments": [
            {
                "source_segment_ids": [1],
                "atomic_id": 1,
                "timestamp": "00:00:00,000 --> 00:00:04,000",
                "video_start": 0.0,
                "video_end": 4.0,
                "source_text": "Hello.",
                "narration": "မူရင်း စာသား",
                "translation": "မူရင်း စာသား",
            },
        ],
    }
    result = apply_script_edits(plan, None, [{"index": 1, "narration": ZG_THANK_YOU}])
    assert result["updated"] == 1
    assert plan["segments"][0]["narration"] == UNI_THANK_YOU
    assert plan["segments"][0]["translation"] == UNI_THANK_YOU


@pytest.fixture
def recap_runner(monkeypatch):
    calls = []

    async def _fake_runner(job_id, req, user_id, chat_id, **kwargs):
        calls.append({"job_id": job_id, "req": req, "user_id": user_id, "kwargs": kwargs})

    monkeypatch.setattr(api_mod, "_run_recap_background_job", _fake_runner)
    yield calls
    api_mod.RECAP_JOBS_REGISTRY.clear()


@pytest.mark.asyncio
async def test_recap_title_is_converted_at_submit(db_session, recap_runner):
    user = await _make_user(db_session, 883003, recap_credits=1)
    req = api_mod.RecapConfigRequest(
        url="", title=ZG_MINGALABAR, voice_slug="komin", duration_seconds=60
    )
    resp = await api_mod.create_recap_job(req, user=user, db=db_session, _gate=user)
    await asyncio.sleep(0)  # let the captured background dispatch run
    assert resp["success"] is True
    assert len(recap_runner) == 1
    assert recap_runner[0]["req"].title == UNI_MINGALABAR
    entry = api_mod.RECAP_JOBS_REGISTRY[resp["recap_id"]]
    assert entry["title"] == UNI_MINGALABAR

"""Burmese syllable mark-order canonicalizer (audit fix, 2026-11-03).

Covers ``repair_burmese_mark_order`` in app.services.myanmar_text and
its wiring into the recap pipeline:

* Mis-ordered syllables (marks typed in visual order) are re-sorted
  into UTN #11 storage order: medials -> e-vowel -> upper vowels ->
  lower vowels -> aa signs -> U+1036 -> asat/dot -> visarga.
* Correctly-written text is BYTE-IDENTICAL afterwards -- the critical
  invariant. It is pinned three ways: a hand-picked word corpus, the
  protected sentences the spelling suite already guards, and EVERY
  string shipped in locales/my/messages.json.
* Wiring: ``spoken_text`` (TTS), ``_sanitize_translation_text``
  (planner) and ``clean_edited_narration`` (user script edits) all run
  the repair; the last one gets ONLY the repair, never the spelling
  normalizer (its documented design rule).

Test strings whose visual form is ambiguous are built from explicit
\\u escapes: e.g. the correct "ကြီး" is U+1000 U+103C U+102E U+1038,
and its mis-ordered twin swaps the first two marks.
"""
import json
from pathlib import Path

import pytest

from app.services.myanmar_text import repair_burmese_mark_order
from app.services.recap_engine.core import (
    canonicalize_burmese_text,
    spoken_text,
)
from app.services.recap_engine.recap_planner import _sanitize_translation_text
from app.services.recap_engine.script_editing import clean_edited_narration

REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# (a) Transformations: mis-ordered -> canonical
# ---------------------------------------------------------------------------

TRANSFORMATIONS = [
    # Vowel sign stored before the medial: က + ီ + ြ + း -> က + ြ + ီ + း
    ("ခွန်အား" + "\u1000\u102E\u103C\u1038" + "မားလှ",
     "ခွန်အား" + "\u1000\u103C\u102E\u1038" + "မားလှ"),
    # Dot below before the e-vowel (ရွ့ + ေ), and the ော digraph typed
    # in reverse (အ + ာ + ေ + င်): both land in storage order.
    ("ကားနှစ်စီးကို " + "\u101B\u103D\u1037\u1031" + "လျားသွား" + "\u1021\u102C\u1031\u1004\u103A",
     "ကားနှစ်စီးကို " + "\u101B\u103D\u1031\u1037" + "လျားသွား" + "\u1021\u1031\u102C\u1004\u103A"),
    # Medial stored after the upper vowel: ကိျုး -> ကျိုး
    ("\u1000\u102D\u103B\u102F\u1038", "\u1000\u103B\u102D\u102F\u1038"),
    # E-vowel stored before both medials: မေွှး -> မွှေး
    ("\u1019\u1031\u103D\u103E\u1038", "\u1019\u103D\u103E\u1031\u1038"),
    # Anusvara stored before the medial: ကံျ -> ကျံ
    ("\u1000\u1036\u103B", "\u1000\u103B\u1036"),
    # Medial stored after upper vowel AND dot below: ကီ့ြး -> ကြီ့း
    ("\u1000\u102E\u1037\u103C\u1038", "\u1000\u103C\u102E\u1037\u1038"),
    # Pre-posed e typed after a completed syllable: ကိုေရွ့ -> ကိုရွေ့
    ("ကို" + "\u1031\u101B\u103D\u1037", "ကို" + "\u101B\u103D\u1031\u1037"),
    # Pre-posed e after a closed (coda) syllable: စိတ်ေမာ -> စိတ်မော
    ("\u1005\u102D\u1010\u103A" + "\u1031\u1019\u102C",
     "\u1005\u102D\u1010\u103A" + "\u1019\u1031\u102C"),
    # Pre-posed e after a complete aa+visarga syllable: ကားေမာင်း -> ကားမောင်း
    ("\u1000\u102C\u1038" + "\u1031\u1019\u102C\u1004\u103A\u1038",
     "\u1000\u102C\u1038" + "\u1019\u1031\u102C\u1004\u103A\u1038"),
    # Pre-posed e at the very start of the text: ေမ -> မေ
    ("\u1031\u1019", "\u1019\u1031"),
]


@pytest.mark.parametrize("wrong,right", TRANSFORMATIONS)
def test_mark_order_transformations(wrong, right):
    assert repair_burmese_mark_order(wrong) == right


@pytest.mark.parametrize("wrong,right", TRANSFORMATIONS)
def test_mark_order_repair_is_idempotent(wrong, right):
    assert repair_burmese_mark_order(right) == right
    assert repair_burmese_mark_order(repair_burmese_mark_order(wrong)) == right


# ---------------------------------------------------------------------------
# (b) Identity: correct text is byte-identical
# ---------------------------------------------------------------------------

IDENTITY_WORDS = [
    "မေမေ",        # the historical corruption case for pre-posed ေ swaps
    "တွေကို",
    "နေတယ်",
    "မင်္ဂလာပါ",    # kinzi
    "သင်္ချိုင်း",   # kinzi + stacked syllable
    "ပဋိပက္ခ",      # Pali stacks
    "အဋ္ဌကထာ",
    "သဒ္ဓါ",        # coda written as a stack
    "ကွန်ပျူတာ",    # coda + tones
    "အမိန့်",        # coda with internal dot below (န + ့ + ်)
    "နှင့်",
    "အောက်",        # coda without dot below
    "ကျော်",        # loose asat after aa sign
    "ရွေ့",         # loose dot below after e-vowel
]


@pytest.mark.parametrize("word", IDENTITY_WORDS)
def test_correct_words_are_byte_identical(word):
    assert repair_burmese_mark_order(word) == word


def _locale_strings():
    data = json.loads(
        (REPO_ROOT / "locales" / "my" / "messages.json").read_text(encoding="utf-8")
    )

    def walk(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for item in value.values():
                yield from walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from walk(item)

    return list(walk(data))


def test_every_burmese_locale_string_is_byte_identical():
    strings = _locale_strings()
    assert len(strings) > 300  # the shipped Burmese locale, guarded whole
    for value in strings:
        assert repair_burmese_mark_order(value) == value


def test_non_myanmar_text_is_untouched():
    for value in ("", "Hello world", "12345", "Voice Clone Pro 🎙️", "a\u1031b"):
        assert repair_burmese_mark_order(value) == value


# ---------------------------------------------------------------------------
# Canonicalize upgrade: zero-width + space-before-mark pre-clean
# ---------------------------------------------------------------------------

def test_canonicalize_strips_zero_width_before_repair():
    # Zero-width space hiding inside the syllable, marks mis-ordered.
    assert canonicalize_burmese_text("\u1000\u200B\u102E\u103C\u1038") == \
        "\u1000\u103C\u102E\u1038"


def test_canonicalize_drops_space_before_combining_marks():
    # "က" + space + mis-ordered marks: the space goes, the syllable heals.
    assert canonicalize_burmese_text("\u1000 \u102E\u103C\u1038") == \
        "\u1000\u103C\u102E\u1038"


def test_canonicalize_still_fixes_spelling_after_repair():
    # Orthography behaviour is unchanged (it now runs on repaired text).
    assert canonicalize_burmese_text("သံသဝင်") == "သံသယဝင်"
    assert canonicalize_burmese_text("အခန်း") == "အခန်း"


# ---------------------------------------------------------------------------
# (c) Wiring: TTS path and planner sanitizer canonicalize their input
# ---------------------------------------------------------------------------

MISORDERED_LINE = "ခွန်အား" + "\u1000\u102E\u103C\u1038" + "မားလှ သူ ပြောတယ်"
FIXED_LINE = "ခွန်အား" + "\u1000\u103C\u102E\u1038" + "မားလှ သူ ပြောတယ်"


def test_spoken_text_canonicalizes_mark_order():
    assert spoken_text(MISORDERED_LINE) == FIXED_LINE


def test_sanitize_translation_text_canonicalizes_mark_order():
    assert _sanitize_translation_text(MISORDERED_LINE) == FIXED_LINE


def test_sanitize_translation_text_wrapper_inner_is_canonicalized():
    wrapped = "[Unused filler adjustment skipped - translated faithfully: " \
        + MISORDERED_LINE + "]"
    assert _sanitize_translation_text(wrapped) == FIXED_LINE


# ---------------------------------------------------------------------------
# Script editing: repair only, never a spelling rewrite of user text
# ---------------------------------------------------------------------------

def test_clean_edited_narration_repairs_mark_order():
    assert clean_edited_narration(MISORDERED_LINE) == FIXED_LINE


@pytest.mark.parametrize("line", [
    "မေမေ တွေကို ပြောတယ်၊ သူတို့ နေတယ်",
    "အခန်း ၁၀ ခန်း ပါတယ်",       # the ခန overreach case: never rewritten
    "သူ့ စာအုပ်ကို ဖတ်နေတယ်",
    "ကျွန်တော် မနက်ဖြန် လာခဲ့ပါမယ်",
])
def test_clean_edited_narration_keeps_correct_user_text_identical(line):
    assert clean_edited_narration(line) == line

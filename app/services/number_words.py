"""Batch CW: digit -> Myanmar number-words normalisation for speech.

Why this exists (owner report 2026-11-09): the voice-clone engine
(VoxCPM) speaks exactly what it is given, and digit strings are
symbols its training data pronounces the English way — a clone job
whose text contained "1234" or "၂၀၂၆" came back reading the numbers
in English inside otherwise Burmese narration. Nothing in the
pipeline rewrote digits before synthesis.

``normalize_numbers_for_speech`` rewrites digit runs as Myanmar
number words BEFORE synthesis, so the spoken form is Burmese:

* "1234" -> "တစ်ထောင့် နှစ်ရာ့ သုံးဆယ့် လေး"
* "20,000 ကျပ်" -> "နှစ်သောင်း ကျပ်"
* "50%" -> "ငါးဆယ် ရာခိုင်နှုန်း"
* a phone-length run is read digit by digit (see below)

Scope and placement: this is a SYNTHESIS-side rewrite only. Callers
apply it to the text they hand the engine; the user's original text
keeps owning validation, billing and display (the worker's
``clean_text``, recap's per-segment VoxCPM call). Gemini/Edge TTS
paths deliberately do not use it.

Conversion rules (pinned by tests/unit/test_batch_cw_2026_11_09.py):

* ASCII 0-9 and Burmese ၀-၉ digits, including mixed runs, are one
  number token. Comma-grouped forms ("20,000", "1,000,000") are
  quantities: commas are stripped and the value converts in full,
  however long it is.
* A PLAIN (comma-free) run of >= 7 digits is an identifier, not a
  quantity (phone numbers, ids): it reads digit by digit. A plain
  run of <= 6 digits converts in full.
* Decimals: integer part converts by the same rules, then "ဒသမ",
  then the fraction digits one by one ("3.14" -> "သုံး ဒသမ တစ် လေး").
* A number directly followed by "%" (one optional space) gains
  "ရာခိုင်နှုန်း".
* Myanmar composition: coefficient+magnitude fused ("တစ်ဆယ်",
  "နှစ်ရာ", "ငါးထောင်"); the connector forms ဆယ့်/ရာ့/ထောင့် are used
  when a remainder follows — EXCEPT that ထောင် only takes its
  connector when it is the highest magnitude present (999999 reads
  "ကိုးသိန်း ကိုးသောင်း ကိုးထောင် ကိုးရာ့ ကိုးဆယ့် ကိုး", while a leading
  ထောင် does connect: 1234 -> "တစ်ထောင့် ..."). သောင်း/သိန်း/သန်း/ကုဋေ
  never take connectors. Zero places are skipped (1001 ->
  "တစ်ထောင့် တစ်"). Values >= 10^7 recurse on ကုဋေ (25,000,000 ->
  "နှစ်ကုဋေ ငါးသန်း"). Components join with single spaces, except the
  teens: a connector tens with coefficient 1 fuses with the ones
  digit (11 -> "တစ်ဆယ့်တစ်", 115 -> "တစ်ရာ့ တစ်ဆယ့်ငါး"), matching
  standard Burmese orthography.

Guards — a digit run is left EXACTLY as written when:

* it is glued to Latin text (the char immediately before or after
  the run is [A-Za-z]: "Ep1", "S02", "1080p"), or separated from a
  Latin letter by a single hyphen on either side ("gemini-3.5-flash"
  keeps "3.5");
* it sits inside a whitespace-delimited token that looks like a
  URL or address (contains "://", "@" or "www.").

The function is idempotent (its output contains no convertible
digit runs), returns digit-free text unchanged, and never raises on
malformed input ("%", ".5", "1.", "") — it degrades to converting
what it safely can.
"""
from __future__ import annotations

import re

__all__ = ["normalize_numbers_for_speech", "int_to_myanmar_words"]


_DIGIT_WORDS = (
    "သုည",  # 0
    "တစ်",  # 1
    "နှစ်",  # 2
    "သုံး",  # 3
    "လေး",  # 4
    "ငါး",  # 5
    "ခြောက်",  # 6
    "ခုနှစ်",  # 7
    "ရှစ်",  # 8
    "ကိုး",  # 9
)

# (value, word, connector-word-or-None), descending. Only ဆယ်/ရာ/ထောင်
# have connector forms, used when a remainder follows (ထောင် only as
# the highest magnitude present — see the module docstring).
_MAGNITUDES = (
    (1_000_000, "သန်း", None),
    (100_000, "သိန်း", None),
    (10_000, "သောင်း", None),
    (1_000, "ထောင်", "ထောင့်"),
    (100, "ရာ", "ရာ့"),
    (10, "ဆယ်", "ဆယ့်"),
)

_KUDE_VALUE = 10_000_000  # ကုဋေ — the recursion step above သန်း
_KUDE_WORD = "ကုဋေ"
_DECIMAL_WORD = "ဒသမ"
_PERCENT_WORD = "ရာခိုင်နှုန်း"

# A plain digit run at least this long is an identifier (phone/id),
# read digit by digit instead of as a quantity.
_DIGIT_BY_DIGIT_MIN_LENGTH = 7

_DIGIT_CLASS = "0-9\u1040-\u1049"
_TOKEN_RE = re.compile(
    rf"(?P<intpart>[{_DIGIT_CLASS}]{{1,3}}(?:,[{_DIGIT_CLASS}]{{3}})+|[{_DIGIT_CLASS}]+)"
    rf"(?:\.(?P<frac>[{_DIGIT_CLASS}]+))?"
    rf"(?P<pct> ?%)?"
)
_LATIN = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _digit_value(ch: str) -> int:
    """Numeric value of one ASCII or Burmese digit character."""
    if "0" <= ch <= "9":
        return ord(ch) - ord("0")
    return ord(ch) - 0x1040


def _digits_to_words(digits: str) -> str:
    """Read a digit string one digit at a time ("0997" ->
    "သုည ကိုး ကိုး ခုနှစ်")."""
    return " ".join(_DIGIT_WORDS[_digit_value(ch)] for ch in digits)


def int_to_myanmar_words(n: int) -> str:
    """Full Myanmar magnitude reading of a non-negative integer.

    0 -> "သုည"; 1234 -> "တစ်ထောင့် နှစ်ရာ့ သုံးဆယ့် လေး";
    999999 -> "ကိုးသိန်း ကိုးသောင်း ကိုးထောင် ကိုးရာ့ ကိုးဆယ့် ကိုး";
    >= 10^7 recurses on ကုဋေ (25,000,000 -> "နှစ်ကုဋေ ငါးသန်း").
    """
    if n <= 0:
        return _DIGIT_WORDS[0]
    if n >= _KUDE_VALUE:
        quotient, remainder = divmod(n, _KUDE_VALUE)
        head = int_to_myanmar_words(quotient) + _KUDE_WORD
        if remainder:
            return f"{head} {int_to_myanmar_words(remainder)}"
        return head

    # Components in descending magnitude order. Each entry is
    # (spoken text, magnitude value, coefficient, used_connector);
    # the ones place rides along as magnitude 1 at the end.
    components: list[tuple[str, int, int, bool]] = []
    for value, word, connector in _MAGNITUDES:
        digit = (n // value) % 10
        if digit == 0:
            continue
        remainder_follows = (n % value) != 0
        use_connector = False
        if connector is not None and remainder_follows:
            # ထောင် connects only when it leads the number (no higher
            # component emitted yet); ဆယ်/ရာ connect whenever a
            # remainder follows.
            use_connector = value != 1_000 or not components
        spoken = _DIGIT_WORDS[digit] + (connector if use_connector else word)
        components.append((spoken, value, digit, use_connector))
    ones = n % 10
    if ones:
        components.append((_DIGIT_WORDS[ones], 1, ones, False))

    out: list[str] = []
    prev: tuple[str, int, int, bool] | None = None
    for component in components:
        spoken, value, _digit, _used_connector = component
        if (
            prev is not None
            and value == 1
            and prev[1] == 10
            and prev[3]
            and prev[2] == 1
        ):
            # Teens fuse: "တစ်ဆယ့်" + "တစ်" -> "တစ်ဆယ့်တစ်".
            out[-1] = out[-1] + spoken
        else:
            out.append(spoken)
        prev = component
    return " ".join(out)


def _integer_part_words(intpart: str) -> str:
    """Spoken form of one integer token (the part before any ".").

    Comma-grouped tokens are quantities and convert in full at any
    length; plain runs of >= _DIGIT_BY_DIGIT_MIN_LENGTH digits are
    identifiers and read digit by digit.
    """
    if "," in intpart:
        digits = intpart.replace(",", "")
        return int_to_myanmar_words(int("".join(str(_digit_value(ch)) for ch in digits)))
    if len(intpart) >= _DIGIT_BY_DIGIT_MIN_LENGTH:
        return _digits_to_words(intpart)
    return int_to_myanmar_words(int("".join(str(_digit_value(ch)) for ch in intpart)))


def _is_guarded(text: str, start: int, end: int) -> bool:
    """True when the digit token at text[start:end] must be left
    exactly as written (Latin-glued identifiers, model names,
    URLs/emails) — see the module docstring."""
    if start > 0 and text[start - 1] in _LATIN:
        return True
    if end < len(text) and text[end] in _LATIN:
        return True
    # A single hyphen between the run and a Latin letter on either
    # side ("gemini-3.5-flash", "S-02" style names).
    if start >= 2 and text[start - 1] == "-" and text[start - 2] in _LATIN:
        return True
    if end + 1 < len(text) and text[end] == "-" and text[end + 1] in _LATIN:
        return True
    # URL / email / web-address token.
    word_start = start
    while word_start > 0 and not text[word_start - 1].isspace():
        word_start -= 1
    word_end = end
    while word_end < len(text) and not text[word_end].isspace():
        word_end += 1
    word = text[word_start:word_end]
    return "://" in word or "@" in word or "www." in word


def normalize_numbers_for_speech(text: str) -> str:
    """Rewrite digit runs in ``text`` as Myanmar number words so a
    speech engine pronounces numbers in Burmese, not English.

    Guards, edge cases and composition rules: see the module
    docstring. Idempotent; digit-free text is returned unchanged;
    never raises.
    """
    if not text:
        return text or ""

    def _replace(match: re.Match[str]) -> str:
        start, end = match.span()
        if _is_guarded(text, start, end):
            return match.group(0)
        spoken = _integer_part_words(match.group("intpart"))
        frac = match.group("frac")
        if frac:
            spoken = f"{spoken} {_DECIMAL_WORD} {_digits_to_words(frac)}"
        if match.group("pct"):
            spoken = f"{spoken} {_PERCENT_WORD}"
        return spoken

    return _TOKEN_RE.sub(_replace, text)

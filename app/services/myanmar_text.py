"""Zawgyi -> Unicode conversion for user-supplied Myanmar text.

Why this exists (deep-hunt #4, A1): a large share of Myanmar users still
type with Zawgyi-encoded keyboards/fonts. Zawgyi is a *visual-order*
encoding squatting on the Unicode Myanmar block -- the same codepoints
mean different things:

* U+1039 is the ASAT sign (Unicode: the stacker/virama);
* U+103A / U+103B / U+103C / U+103D are the medials YA / RA / WA / HA
  (Unicode: asat / YA / RA / WA, with HA at U+103E);
* stacked consonants are single codepoints in U+1060..U+1097, as are
  fused forms (kinzi+vowel, HA+vowel, tall-AA+asat) and contextual
  glyph variants (three different RA shapes, YA/NA/RA look variants);
* the e-vowel U+1031 and the medial RA are stored *before* the
  consonant they belong to, and the kinzi sign U+1064 *after* its
  whole onset cluster.

Nothing in the product converted it, so Zawgyi voice text was spoken as
garbled Unicode codepoints -- and charged for. This module converts at
intake (voice job creation, bot clone text, script edits, recap titles).

Design rules, in order of importance:

1. Genuine Unicode Burmese passes through BYTE-IDENTICAL. The detector
   only fires on patterns that cannot occur in valid Unicode Burmese
   (see ``_looks_like_zawgyi``), so proper-Unicode input -- the
   overwhelming majority -- is never rewritten.
2. Conversion requires at least two Myanmar-block characters: a lone
   codepoint is a meaningless fragment in both encodings and is left
   exactly as typed.
3. The transliteration follows the long-established Zawgyi-One ->
   Unicode rule order (the Paragu/Rabbit rule family): medial
   normalisation, kinzi repositioning, asat restoration, fused-form
   expansion, stacked-consonant expansion, RA repositioning, and a
   final syllable storage-order canonicalisation.

Deliberately NOT converted (guessing would corrupt legitimate text):

* U+1040 / U+1047: Zawgyi sometimes uses the digit codepoints for the
  look-alike letters WA / RA. Real digits stand in the same positions,
  so the ambiguity is left exactly as typed.
* U+104E: valid in both encodings (the genitive sign); Zawgyi also
  uses it as a contraction glyph, but the readings cannot be told
  apart safely.
* The NYA (U+1009) / U (U+1025) look-alike pair: left as typed (the
  one fused Zawgyi form, U+106A, follows the classic converters and
  maps to U+1009).
* Stacked consonants typed as an explicit U+1039 sequence for the few
  subjoined forms Zawgyi gives no single codepoint (NYA, NNYA, YA, RA,
  WA, SA, LLA, A): the U+1039 is read as the asat sign, exactly as in
  the classic converters. These stacks are vanishingly rare in
  Burmese prose.
* Non-Burmese scripts that own parts of U+1060..U+1097 in the Unicode
  standard (Shan/Tai letters and digits): outside this product's
  domain -- Burmese narration and titles never contain them.

The module also hosts ``repair_burmese_mark_order``: a canonicaliser
for text that is already Unicode but whose combining marks were typed
in visual rather than storage order (a mis-order the Zawgyi detector
above deliberately does not flag, because every codepoint involved is
legitimate Unicode). It only ever re-sorts marks inside a syllable;
correctly-written text passes through byte-identical.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Detection (conservative: every signal is impossible in valid Unicode)
# ---------------------------------------------------------------------------

_MY_BLOCK_RE = re.compile(r"[\u1000-\u109F]")

# Zawgyi-only codepoints: stacked consonants, fused vowel/kinzi forms and
# contextual glyph variants. Burmese Unicode text never contains them.
_ZAWGYI_ONLY_RE = re.compile(r"[\u1060-\u1097]")

# U+1039 must be followed by a consonant in Unicode (it is the stacker).
# In Zawgyi it is the asat and usually is not. A preceding U+1004 U+103A
# is the Unicode kinzi sequence, whose final U+1039 is exempt: it is a
# stacker, and we never rewrite anything that looks like kinzi.
_ASAT_POSITION_RE = re.compile(r"(?<!\u1004\u103A)\u1039(?![\u1000-\u102A])")

# A genuine Unicode stack: consonant + U+1039 + consonant (the stacker
# sandwiched between the onset and its subjoined consonant, e.g. the
# geminate in "သဒ္ဒါ"). When a document contains one, its writer is
# producing real Unicode stacks, so a stray unstacked U+1039 elsewhere
# (e.g. a trailing fragment like "က\u1039" after an otherwise valid
# word) is a typing artefact, NOT Zawgyi's asat -- the asat-position
# signal alone must not flip such a document (hunt #5). The shape is
# deliberately the full sandwich: Zawgyi's coda asat is routinely
# followed by the next syllable's onset, so "1039 + consonant" alone
# proves nothing; what Zawgyi cannot produce is the stacker wedged
# between two consonants as one cluster. The other signals are
# unaffected: they describe shapes a stack-producing writer cannot emit.
_VALID_STACK_RE = re.compile(r"[\u1000-\u102A]\u1039[\u1000-\u102A]")

# U+1031 (e-vowel) stored *before* its consonant. The lookbehind lists
# everything that may legitimately precede U+1031 inside a Unicode
# syllable cluster (base consonants, independent vowels, the stacker,
# the medials). Anything else before it -- start of text, a space, a
# vowel sign, a tone mark, the asat -- means the sign was pre-posed the
# Zawgyi way and its consonant follows it.
_PREPOSED_E_RE = re.compile(
    r"(?<![\u1000-\u1021\u1023-\u102A\u1039\u103B-\u103E])"
    r"\u1031(?=[\u1000-\u1021])"
)

# Medial RA stored before its base: Zawgyi uses U+103B and the variant
# glyphs U+107D..U+1084. In Unicode U+103B (medial YA) always follows a
# base consonant, a stack, or another medial -- the lookbehind mirrors
# exactly those, so a syllable-initial U+103B before a consonant (or a
# variant glyph there) can only be Zawgyi's RA.
_PREPOSED_RA_RE = re.compile(
    r"(?<![\u1000-\u1021\u1039\u103B-\u103E])"
    r"[\u103B\u107D-\u1084](?=[\u1000-\u1021])"
)

# U+103A followed by a vowel sign or a medial: impossible for the
# Unicode asat (which is followed by a consonant, a tone mark, or the
# end of the syllable), but exactly how the Zawgyi medial YA combines.
_YA_POSITION_RE = re.compile(r"\u103A(?=[\u102B-\u1034\u1036\u103B-\u103E])")


def is_probably_zawgyi(text: str) -> bool:
    """Conservative Zawgyi detector.

    Returns True only on patterns that valid Unicode Burmese cannot
    produce (see the pattern comments above). Proper Unicode -- even a
    single kinzi fragment -- returns False.
    """
    if not text or len(_MY_BLOCK_RE.findall(text)) < 2:
        return False
    if _ZAWGYI_ONLY_RE.search(text):
        return True
    if (
        _PREPOSED_E_RE.search(text)
        or _PREPOSED_RA_RE.search(text)
        or _YA_POSITION_RE.search(text)
    ):
        return True
    # The asat-position signal is the weakest one: distrust it when the
    # text also proves it can write genuine Unicode stacks (see
    # _VALID_STACK_RE). A real Zawgyi document's stacks are single
    # codepoints (U+1060..) or caught by the signals above.
    return bool(
        _ASAT_POSITION_RE.search(text) and not _VALID_STACK_RE.search(text)
    )


# ---------------------------------------------------------------------------
# Conversion (ordered passes; the order is load-bearing throughout)
# ---------------------------------------------------------------------------

# Single-codepoint Zawgyi forms -> Unicode sequences: the stacked
# consonants and the remaining fused/variant glyphs. Keys in
# U+107D..U+1084 (RA variants), U+108A..U+108D (fused WA+HA and
# kinzi+vowel), U+1064 (kinzi) and U+105A / U+108E / U+1033 / U+1034
# are handled by the ordered passes in _convert() and are intentionally
# absent here.
_SINGLE_MAP = {
    0x1060: "\u1039\u1000",  # stacked KA
    0x1061: "\u1039\u1001",
    0x1062: "\u1039\u1002",
    0x1063: "\u1039\u1003",
    0x1065: "\u1039\u1005",
    0x1066: "\u1039\u1006",
    0x1067: "\u1039\u1006",  # wide-base variant of stacked CHA
    0x1068: "\u1039\u1007",
    0x1069: "\u1039\u1008",
    0x106A: "\u1009",        # tall form used for U before stacks/u-signs
    0x106B: "\u100A",        # NNYA variant before stacks/u-signs
    0x106C: "\u1039\u100B",
    0x106D: "\u1039\u100C",
    0x106E: "\u100D\u1039\u100D",  # fused DDA + stacked DDA
    0x106F: "\u100E\u1039\u100D",  # fused DDHA + stacked DDA
    0x1070: "\u1039\u100F",
    0x1071: "\u1039\u1010",
    0x1072: "\u1039\u1010",  # wide-base variant of stacked TA
    0x1073: "\u1039\u1011",
    0x1074: "\u1039\u1011",  # wide-base variant of stacked THA
    0x1075: "\u1039\u1012",
    0x1076: "\u1039\u1013",
    0x1077: "\u1039\u1014",
    0x1078: "\u1039\u1015",
    0x1079: "\u1039\u1016",
    0x107A: "\u1039\u1017",
    0x107B: "\u1039\u1018",
    0x107C: "\u1039\u1019",
    0x1085: "\u1039\u101C",
    0x1086: "\u103F",        # great SA
    0x108F: "\u1014",        # NA variant before stacks/signs
    0x1090: "\u101B",        # RA variant before u-signs
    0x1091: "\u100F\u1039\u100D",  # fused NNA + stacked DDA
    0x1092: "\u100B\u1039\u100C",  # fused TTA + stacked TTHA
    0x1094: "\u1037",        # aukmyint variant
    0x1095: "\u1037",        # aukmyint variant
    0x1097: "\u100B\u1039\u100B",  # fused TTA + stacked TTA
}
_SINGLE_TRANSLATE = str.maketrans(_SINGLE_MAP)

# Kinzi: in Zawgyi the sign (and its fused vowel forms U+108B..U+108D)
# is stored AFTER the onset cluster it sits on -- behind any pre-posed
# e-vowel / RA and behind the base's own medials. Unicode stores it in
# front of the base consonant.
_KINZI_RE = re.compile(
    r"(\u1031)?(\u103C)?([\u1000-\u1021])([\u103B-\u103E]*)\u1064"
)
_KINZI_VOWEL_RES = (
    (re.compile(r"(\u1031)?(\u103C)?([\u1000-\u1021])([\u103B-\u103E]*)\u108B"), "\u102D"),
    (re.compile(r"(\u1031)?(\u103C)?([\u1000-\u1021])([\u103B-\u103E]*)\u108C"), "\u102E"),
    (re.compile(r"(\u1031)?(\u103C)?([\u1000-\u1021])([\u103B-\u103E]*)\u108D"), "\u1036"),
)

# Final syllable storage-order canonicalisation (the classic closing
# pass): a syllable arrives as [e?] base [stack?] [upper?] [tones?]
# [medials?] [lower?] [upper?] and leaves as base, stack, medials, e,
# upper, upper, lower, tones -- canonical Unicode order. The tone and
# medial runs are captured whole (the historical rule text captured
# only the last repetition, silently dropping signs on 2-medial and
# 2-tone syllables).
_STORAGE_ORDER_RE = re.compile(
    r"(\u1031)?([\u1000-\u1021])(\u1039[\u1000-\u1021])?"
    r"([\u102D\u102E\u1032])?((?:[\u1036\u1037\u1038]){0,2})"
    r"((?:[\u103B-\u103E])*)([\u102F\u1030])?([\u102D\u102E\u1032])?"
)


def _storage_order_repl(match: re.Match) -> str:
    return "".join(
        (
            match.group(2),
            match.group(3) or "",
            match.group(6) or "",
            match.group(1) or "",
            match.group(4) or "",
            match.group(8) or "",
            match.group(7) or "",
            match.group(5) or "",
        )
    )


def _convert(text: str) -> str:
    s = text

    # Pass 1 -- medial signs. Zawgyi shifts them one codepoint down and
    # gives RA extra pre-posed variant glyphs. Restore the Unicode
    # codepoints in an order that never re-processes its own output
    # (HA first, so the WA pass may then produce U+103D freely, etc.).
    # RA is normalised to U+103C but stays pre-posed until pass 6.
    s = re.sub(r"[\u103D\u1087]", "\u103E", s)         # HA
    s = s.replace("\u103C", "\u103D")                   # WA
    s = re.sub(r"[\u103B\u107E-\u1084]", "\u103C", s)   # RA variants -> RA
    s = re.sub(r"[\u103A\u107D]", "\u103B", s)          # YA
    s = s.replace("\u103E\u103B", "\u103B\u103E")       # order: YA before HA
    s = s.replace("\u108A", "\u103D\u103E")             # fused WA+HA

    # Pass 2 -- kinzi repositioning (sign itself expands in pass 5).
    s = _KINZI_RE.sub("\u1064\\1\\2\\3\\4", s)
    for pattern, vowel in _KINZI_VOWEL_RES:
        s = pattern.sub("\u1064\\1\\2\\3\\4" + vowel, s)

    # Pass 3 -- every remaining U+1039 is the Zawgyi asat: genuine
    # stacks are the single codepoints expanded in pass 5, never a
    # U+1039 sequence (see the header for the rare exception).
    s = s.replace("\u1039", "\u103A")

    # Pass 4 -- fused / contextual vowel forms. After pass 3, so the
    # U+103A that U+105A expands to is already the final Unicode asat.
    s = s.replace("\u105A", "\u102B\u103A")   # tall AA + asat
    s = s.replace("\u108E", "\u102D\u1036")   # I + anusvara
    s = s.replace("\u1033", "\u102F")         # contextual U sign
    s = s.replace("\u1034", "\u1030")         # contextual UU sign
    s = s.replace("\u1088", "\u103E\u102F")   # HA + U
    s = s.replace("\u1089", "\u103E\u1030")   # HA + UU

    # Pass 5 -- kinzi expansion + stacked/fused single codepoints.
    s = s.replace("\u1064", "\u1004\u103A\u1039")
    s = s.translate(_SINGLE_TRANSLATE)

    # Pass 6 -- medial RA repositioning: move a pre-posed U+103C behind
    # its base consonant (and behind a stacked consonant of the same
    # syllable). Post-posed U+103C never matches: in converted text it
    # is followed by vowel signs, tones or the asat, not a consonant
    # that starts its own cluster.
    s = re.sub(
        r"(\u103C)([\u1000-\u1021])((?:\u1039[\u1000-\u1021])?)",
        r"\2\3\1",
        s,
    )

    # Pass 7 -- syllable storage order (e-vowel behind its cluster,
    # medials / vowel signs / tone marks in canonical order).
    s = _STORAGE_ORDER_RE.sub(_storage_order_repl, s)
    return s


def convert_zawgyi_to_unicode(text: str) -> str:
    """Return ``text`` converted from Zawgyi to Unicode when it is
    confidently Zawgyi; otherwise return it unchanged, byte-identical.

    Safe to call on any user text at intake: English, emoji, digits and
    genuine Unicode Burmese all pass through untouched, and the
    conversion is idempotent (its own output is valid Unicode, which
    the detector never flags).
    """
    if not text or not is_probably_zawgyi(text):
        return text
    return _convert(text)


# ---------------------------------------------------------------------------
# Mark-order repair (Unicode -> canonical Unicode storage order)
# ---------------------------------------------------------------------------
# A syllable typed on a visual-order keyboard (or emitted by a model
# that "thinks" in glyphs) can carry every mark in the wrong storage
# position: "ကီြး" for "ကြီး", "ရွ့ေ" for "ရွေ့". Each codepoint is
# legal Unicode, so the Zawgyi detector above stays silent, but TTS
# engines and subtitle shaping read the marks literally and garble
# them. The repair parses the text into syllable clusters (kinzi /
# base consonant / U+1039 stacks / combining marks / codas) and
# stable-sorts each cluster's marks into the UTN #11 / OpenType mym2
# storage order:
#
#   medials (U+103B..U+103E) -> U+1031 -> upper vowels (U+102D, U+102E,
#   U+1032) -> lower vowels (U+102F, U+1030) -> aa signs (U+102B,
#   U+102C) -> U+1036 -> asat / dot below (U+103A, U+1037) -> U+1038.
#
# Two shapes need more than sorting:
#
# * Coda: a consonant killed by U+103A belongs to the syllable in
#   front of it, and the dot below of such a coda is stored INSIDE the
#   coda, before the asat (န + ့ + ် in "အမိန့်"). The whole coda
#   (consonant + optional dot + asat) is therefore one sorting unit.
# * Pre-posed U+1031: a "ေ" typed before its consonant -- at the start
#   of a word, or right after a completed syllable ("ကို" + "ေ" +
#   "ရွ့") -- belongs to the FOLLOWING syllable. It is only moved when
#   it cannot belong to the syllable in front of it: the old
#   unconditional swap corrupted "မေမေ" -> "မမေေ", "တွေကို" ->
#   "တွကေို", "နေတယ်" -> "နတေယ်", so a U+1031 that follows an open
#   syllable (base + medials, vowel slot still empty) stays exactly
#   where it is, and one met mid-cluster is absorbed only while the
#   cluster is still open (see _parse_mark_cluster).
#
# Critical invariant, same as the converter's rule 1: text that is
# already in canonical order comes out BYTE-IDENTICAL. Sorting is
# stable and consonants are never added, removed or rewritten.

_REPAIR_MEDIALS = "\u103B\u103C\u103D\u103E"
_REPAIR_UPPER_VOWELS = "\u102D\u102E\u1032"
_REPAIR_LOWER_VOWELS = "\u102F\u1030"
_REPAIR_AA_SIGNS = "\u102B\u102C"
_REPAIR_E_VOWEL = "\u1031"
_REPAIR_ANUSVARA = "\u1036"
_REPAIR_ASAT = "\u103A"
_REPAIR_DOT_BELOW = "\u1037"
_REPAIR_VISARGA = "\u1038"
_REPAIR_STACKER = "\u1039"

_REPAIR_MARK_CHARS = frozenset(
    _REPAIR_MEDIALS + _REPAIR_UPPER_VOWELS + _REPAIR_LOWER_VOWELS
    + _REPAIR_AA_SIGNS + _REPAIR_E_VOWEL + _REPAIR_ANUSVARA
    + _REPAIR_ASAT + _REPAIR_DOT_BELOW + _REPAIR_VISARGA
)
# Marks that fill a syllable's vowel slot: once one of these is seen, a
# later U+1031 can no longer belong to the same syllable.
_REPAIR_CLOSING_VOWELS = frozenset(
    _REPAIR_UPPER_VOWELS + _REPAIR_LOWER_VOWELS + _REPAIR_ANUSVARA
)
_REPAIR_TONES = frozenset(_REPAIR_DOT_BELOW + _REPAIR_VISARGA)


def _repair_is_base(ch: str) -> bool:
    """A syllable onset letter: consonants U+1000..U+1021 plus the
    independent vowels U+1023..U+102A, which head their own syllable."""
    return "\u1000" <= ch <= "\u1021" or "\u1023" <= ch <= "\u102A"


def _repair_mark_rank(ch: str) -> tuple[int, int]:
    if ch in _REPAIR_MEDIALS:
        return (0, ord(ch))
    if ch == _REPAIR_E_VOWEL:
        return (1, 0)
    if ch in _REPAIR_UPPER_VOWELS:
        return (2, ord(ch))
    if ch in _REPAIR_LOWER_VOWELS:
        return (3, ord(ch))
    if ch in _REPAIR_AA_SIGNS:
        return (4, ord(ch))
    if ch == _REPAIR_ANUSVARA:
        return (5, 0)
    if ch == _REPAIR_ASAT:
        return (6, 0)
    if ch == _REPAIR_DOT_BELOW:
        return (7, 0)
    return (8, 0)  # U+1038 visarga


def _repair_is_kinzi_at(text: str, i: int) -> bool:
    """Kinzi (U+1004 U+103A U+1039 + base) opens the NEXT syllable; the
    U+1004 is never read as a coda when this shape matches."""
    return (
        text[i] == "\u1004"
        and i + 3 < len(text)
        and text[i + 1] == _REPAIR_ASAT
        and text[i + 2] == _REPAIR_STACKER
        and _repair_is_base(text[i + 3])
    )


def _repair_starts_cluster(text: str, i: int) -> bool:
    if i >= len(text):
        return False
    return _repair_is_base(text[i]) or _repair_is_kinzi_at(text, i)


def _repair_parse_cluster(
    text: str, i: int, pending_e: bool
) -> tuple[str, int]:
    """Render one syllable cluster starting at index ``i`` with its
    marks in canonical order; return (rendered, next_index).

    ``pending_e`` carries a pre-posed U+1031 held over from the
    previous cluster (or from the start of the text): it joins this
    cluster's marks and sorts into the e-vowel slot.
    """
    n = len(text)
    prefix = ""
    if _repair_is_kinzi_at(text, i):
        prefix = text[i:i + 3]
        i += 3
    onset = [text[i]]
    i += 1
    while i + 1 < n and text[i] == _REPAIR_STACKER and _repair_is_base(text[i + 1]):
        onset.append(text[i:i + 2])
        i += 2

    units: list[tuple[tuple[int, int], str]] = []
    has_e = False
    if pending_e:
        units.append(((1, 0), _REPAIR_E_VOWEL))
        has_e = True
    has_coda = False
    vowel_seen = False
    aa_seen = False
    tone_seen = False
    while i < n:
        ch = text[i]
        if ch in _REPAIR_MARK_CHARS:
            if ch == _REPAIR_E_VOWEL:
                # A late U+1031 still belongs to THIS syllable only
                # while the syllable is open: no e yet, no coda yet,
                # no closing vowel yet, and not (aa + tone), which is
                # already a complete syllable ("ကား" + "ေ" opens the
                # next one). Medials / tones alone leave the vowel
                # slot empty ("ရွ့" + "ေ"), and medials plus a lone aa
                # sign are the "ေါ" digraph typed in reverse
                # ("အ" + "ာ" + "ေ" -> "အ" + "ေ" + "ာ").
                if has_e or has_coda or vowel_seen or (aa_seen and tone_seen):
                    break
                units.append(((1, 0), ch))
                has_e = True
                i += 1
                continue
            units.append((_repair_mark_rank(ch), ch))
            if ch in _REPAIR_CLOSING_VOWELS:
                vowel_seen = True
            elif ch in _REPAIR_AA_SIGNS:
                aa_seen = True
            elif ch in _REPAIR_TONES:
                tone_seen = True
            i += 1
            continue
        if _repair_is_base(ch):
            if _repair_is_kinzi_at(text, i):
                break
            # Coda: a consonant killed by U+103A belongs to this
            # syllable. Its dot below, when present, is stored inside
            # the coda before the asat, so the whole coda is one unit.
            if i + 1 < n and text[i + 1] == _REPAIR_ASAT:
                units.append(((6, 0), text[i:i + 2]))
                has_coda = True
                i += 2
                continue
            if (
                i + 2 < n
                and text[i + 1] == _REPAIR_DOT_BELOW
                and text[i + 2] == _REPAIR_ASAT
            ):
                units.append(((6, 0), text[i:i + 3]))
                has_coda = True
                i += 3
                continue
            break
        break

    # A loose dot below directly beside a coda chunk belongs to that
    # coda (typed after its asat, or before its consonant): fold it
    # inside, before the asat, instead of sorting it away from it.
    merged: list[tuple[tuple[int, int], str]] = []
    for rank, chunk in units:
        if (
            chunk == _REPAIR_DOT_BELOW
            and merged
            and len(merged[-1][1]) == 2
            and merged[-1][1][1] == _REPAIR_ASAT
            and _repair_is_base(merged[-1][1][0])
        ):
            prev_rank, prev = merged[-1]
            merged[-1] = (prev_rank, prev[0] + _REPAIR_DOT_BELOW + _REPAIR_ASAT)
        elif (
            rank == (6, 0)
            and len(chunk) in (2, 3)
            and chunk[-1] == _REPAIR_ASAT
            and merged
            and merged[-1][1] == _REPAIR_DOT_BELOW
        ):
            merged[-1] = (rank, chunk[0] + _REPAIR_DOT_BELOW + _REPAIR_ASAT)
        else:
            merged.append((rank, chunk))
    merged.sort(key=lambda unit: unit[0])
    return prefix + "".join(onset) + "".join(chunk for _, chunk in merged), i


def repair_burmese_mark_order(text: str) -> str:
    """Re-sort every Myanmar syllable's combining marks into canonical
    UTN #11 storage order and return the result.

    For Unicode text whose marks were typed in visual order. Text that
    is already correctly ordered -- the overwhelming majority, and
    every string in the shipped Burmese locale -- comes back
    byte-identical; consonants, digits, punctuation, other scripts and
    spacing are never touched. The repair is idempotent.
    """
    s = str(text or "")
    if not s or not any("\u1000" <= ch <= "\u102A" for ch in s):
        return s
    out: list[str] = []
    pending_e = False
    i, n = 0, len(s)
    while i < n:
        if _repair_starts_cluster(s, i):
            rendered, i = _repair_parse_cluster(s, i, pending_e)
            pending_e = False
            out.append(rendered)
        elif s[i] == _REPAIR_E_VOWEL and _repair_starts_cluster(s, i + 1):
            # Pre-posed U+1031: hold it for the syllable that follows.
            pending_e = True
            i += 1
        else:
            if pending_e:
                out.append(_REPAIR_E_VOWEL)
                pending_e = False
            out.append(s[i])
            i += 1
    if pending_e:
        out.append(_REPAIR_E_VOWEL)
    return "".join(out)

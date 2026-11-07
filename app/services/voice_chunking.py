"""Batch CH: chunk planning for VOICE-CLONE synthesis.

Why this exists (diagnosis, log-verified 2026-11-07): the voice-clone
path used to send the WHOLE target text to VoxCPM in one call. The
model's input cap is 2,096 tokens for the TOTAL input (target text +
reference transcript + reference audio + control), and Burmese costs
several tokens per character — so a long text (or a long custom
reference sample) blew the cap on every Space and the job silently
landed on the Edge TTS persona fallback while marked successful.

This module plans sentence-aligned chunks whose ESTIMATED total input
stays under a conservative budget (1,700 of the 2,096 cap). The recap
path does NOT use it — recap segments are pre-chunked upstream and
keep their segment↔audio mapping.

Estimation model (deliberately conservative):
- text tokens  ≈ ceil(chars × 2.2) — Burmese-heavy text; a sandbox
  measurement put real prose nearer 1.5–1.8 tokens/char, so 2.2 keeps
  margin. The same factor is applied to the control instruction
  (mostly English, really ~0.3/char) — over-estimating is safe here.
- reference audio ≈ 25 tokens/second of (trimmed) reference audio.
- reference transcript (persona prompt text, when known) at 2.2/char.

Chunks are packed from whole sentences; an over-long sentence is
hard-split at clause marks ("၊") and only then by raw length. A floor
of MIN_CHUNK_CHARS keeps chunks synthesis-worthy when the reference
overhead alone nearly exhausts the budget (worst realistic overhead —
45 s reference + control — still leaves the floored chunk's estimated
input under the real 2,096 cap).
"""
from __future__ import annotations

import math

# Conservative per-call budget for the ESTIMATED total input, against
# the model's real 2,096-token cap.
INPUT_TOKEN_BUDGET = 1700
MODEL_TOKEN_CAP = 2096
TOKENS_PER_CHAR = 2.2
REF_AUDIO_TOKENS_PER_SECOND = 25.0
# The engine trims clone references to their first 45 s (Batch BU), so
# longer uploads cannot add more reference overhead than this.
REFERENCE_MAX_SECONDS = 45.0
# Never plan a chunk smaller than this, even when the reference
# overhead alone nearly exhausts the budget.
MIN_CHUNK_CHARS = 120

_SENTENCE_ENDINGS = "။?!\n"
_CLAUSE_MARK = "၊"


def estimate_tokens(text: str | None) -> int:
    """Estimated model tokens for a text at TOKENS_PER_CHAR."""
    if not text:
        return 0
    # Round first: 100 × 2.2 is 220.00000000000003 in binary float and
    # a bare ceil would inflate it to 221.
    return int(math.ceil(round(len(str(text)) * TOKENS_PER_CHAR, 9)))


def input_overhead_tokens(
    *,
    reference_transcript: str = "",
    control_instruction: str = "",
    reference_seconds: float = 0.0,
) -> int:
    """Estimated tokens every call pays before the target text:
    reference transcript + control instruction + reference audio."""
    ref_seconds = min(
        max(float(reference_seconds or 0.0), 0.0), REFERENCE_MAX_SECONDS
    )
    return (
        estimate_tokens(reference_transcript)
        + estimate_tokens(control_instruction)
        + int(math.ceil(ref_seconds * REF_AUDIO_TOKENS_PER_SECOND))
    )


def estimated_input_tokens(
    chunk_text: str,
    *,
    reference_transcript: str = "",
    control_instruction: str = "",
    reference_seconds: float = 0.0,
) -> int:
    """Estimated TOTAL input tokens for one call carrying chunk_text."""
    return input_overhead_tokens(
        reference_transcript=reference_transcript,
        control_instruction=control_instruction,
        reference_seconds=reference_seconds,
    ) + estimate_tokens(chunk_text)


def chunk_char_budget(
    *,
    reference_transcript: str = "",
    control_instruction: str = "",
    reference_seconds: float = 0.0,
) -> int:
    """Max target-text characters per call so the estimated total
    input stays within INPUT_TOKEN_BUDGET (floored at MIN_CHUNK_CHARS
    when the reference overhead alone nearly exhausts the budget)."""
    overhead = input_overhead_tokens(
        reference_transcript=reference_transcript,
        control_instruction=control_instruction,
        reference_seconds=reference_seconds,
    )
    allowance = INPUT_TOKEN_BUDGET - overhead
    if allowance <= 0:
        return MIN_CHUNK_CHARS
    # ceil(chars × 2.2) <= allowance  <=>  chars <= allowance / 2.2
    return max(MIN_CHUNK_CHARS, int(allowance / TOKENS_PER_CHAR))


def split_sentences(text: str) -> list[str]:
    """Split text into sentences on Burmese/Latin sentence endings
    (။ ? ! and line breaks), keeping the ending punctuation attached.
    Pieces are stripped; empty pieces are dropped."""
    sentences: list[str] = []
    buf: list[str] = []
    for ch in str(text or ""):
        buf.append(ch)
        if ch in _SENTENCE_ENDINGS:
            piece = "".join(buf).strip()
            if piece:
                sentences.append(piece)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        sentences.append(tail)
    return sentences


def _hard_split(sentence: str, budget_chars: int) -> list[str]:
    """Split one over-budget sentence at clause marks ("၊") where
    possible, then by raw length. Every piece is <= budget_chars."""
    if len(sentence) <= budget_chars:
        return [sentence]
    pieces: list[str] = []
    rest = sentence.strip()
    while len(rest) > budget_chars:
        cut = rest.rfind(_CLAUSE_MARK, 0, budget_chars)
        if cut >= budget_chars // 3:
            # Split just after the clause mark.
            piece, rest = rest[: cut + 1], rest[cut + 1 :].strip()
        else:
            piece, rest = rest[:budget_chars], rest[budget_chars:].strip()
        if piece.strip():
            pieces.append(piece.strip())
    if rest:
        pieces.append(rest)
    return [p for p in pieces if p]


def plan_text_chunks(
    text: str,
    *,
    reference_transcript: str = "",
    control_instruction: str = "",
    reference_seconds: float = 0.0,
) -> list[str]:
    """Plan synthesis chunks for a voice-clone job's target text.

    Returns [text] unchanged when the whole text fits in one call
    (the common case — no behaviour change). Otherwise returns >= 2
    chunks, each within the character budget derived from the token
    budget minus the reference/control overhead, packed from whole
    sentences wherever possible.
    """
    text = str(text or "")
    if not text.strip():
        return [text]
    budget_chars = chunk_char_budget(
        reference_transcript=reference_transcript,
        control_instruction=control_instruction,
        reference_seconds=reference_seconds,
    )
    if len(text) <= budget_chars:
        return [text]

    chunks: list[str] = []
    current = ""
    for sentence in split_sentences(text):
        for piece in _hard_split(sentence, budget_chars):
            if not current:
                current = piece
            elif len(current) + 1 + len(piece) <= budget_chars:
                current = f"{current} {piece}"
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks or [text]


__all__ = [
    "INPUT_TOKEN_BUDGET",
    "MODEL_TOKEN_CAP",
    "TOKENS_PER_CHAR",
    "REF_AUDIO_TOKENS_PER_SECOND",
    "REFERENCE_MAX_SECONDS",
    "MIN_CHUNK_CHARS",
    "estimate_tokens",
    "input_overhead_tokens",
    "estimated_input_tokens",
    "chunk_char_budget",
    "split_sentences",
    "plan_text_chunks",
]

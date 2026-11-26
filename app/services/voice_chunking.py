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
import re
import unicodedata

# Conservative per-call budget for the ESTIMATED total input, against
# the model's real 2,096-token cap.
INPUT_TOKEN_BUDGET = 1700
MODEL_TOKEN_CAP = 2096
TOKENS_PER_CHAR = 2.2
CONTROL_TOKENS_PER_CHAR = 0.4
REF_AUDIO_TOKENS_PER_SECOND = 25.0
# The engine trims clone references to their first 45 s (Batch BU), so
# longer uploads cannot add more reference overhead than this.
REFERENCE_MAX_SECONDS = 45.0
# Never plan a chunk smaller than this, even when the reference
# overhead alone nearly exhausts the budget.
MIN_CHUNK_CHARS = 120
# Soft sentence-packing ceiling for prosodic stability (~4-6 chunks on a 122 s job).
MAX_CHUNK_CHARS = 425
# Clone Prosody Phase 3 (Part 1): sweet-spot target ceiling (~16-20 s uniform chunks)
# and minimum syllable floor to prevent short runt final chunks on jobs >= 140 syllables.
TARGET_CHUNK_CHARS = 385
MIN_TAIL_SYLLABLES = 65
MIN_JOB_SYLLABLES_FOR_TAIL_GUARD = 140
# Clone Prosody Phase 2 (Part 5): prefer breaking at a paragraph separator (`\n\n`)
# once the current packed chunk has reached at least this many characters.
PARAGRAPH_BREAK_MIN_CHARS = 220

_SENTENCE_ENDINGS = "။?!\n"
_CLAUSE_MARK = "၊"

# Burmese syllable break pattern (matches boundary before a base consonant / independent vowel):
# Does not break after a virama stacker (္), and only matches before an onset letter not followed by asat.
BURMESE_SYLLABLE_BREAK = re.compile(
    r'(?<![္])(?=[က-အ\u1023-\u102A\u104E](?![\u1037\u1038]?[်္]))'
)
_ASCII_SYLLABLE_FALLBACK_RE = re.compile(r"[A-Za-z0-9]+")


def _count_syllables(text: str) -> int:
    s = str(text or "").strip()
    if not s:
        return 0
    return len(BURMESE_SYLLABLE_BREAK.findall(s)) + len(_ASCII_SYLLABLE_FALLBACK_RE.findall(s))


def is_chunk_balancing_v2_enabled() -> bool:
    """True when dynamic setting ``chunk_balancing_v2_enabled`` is active (default True)."""
    try:
        from app.config import settings as _settings

        val = getattr(_settings, "chunk_balancing_v2_enabled", True)
        if isinstance(val, str):
            return val.strip().lower() not in ("0", "false", "no", "off")
        return bool(val)
    except Exception:
        return True


def _is_combining_or_stack(ch: str) -> bool:
    """True if character is a Myanmar combining mark, vowel sign, tone, asat, or virama."""
    if unicodedata.combining(ch) > 0:
        return True
    return ch in "\u1039\u103A\u1037\u1038\u102B\u102C\u102D\u102E\u102F\u1030\u1031\u1032\u1036\u103B\u103C\u103D\u103E"



def estimate_tokens(text: str | None) -> int:
    """Estimated model tokens for a text at TOKENS_PER_CHAR."""
    if not text:
        return 0
    # Round first: 100 × 2.2 is 220.00000000000003 in binary float and
    # a bare ceil would inflate it to 221.
    return int(math.ceil(round(len(str(text)) * TOKENS_PER_CHAR, 9)))


def estimate_control_tokens(text: str | None) -> int:
    """Estimated model tokens for control_instruction (~0.4 tok/char for English/ASCII)."""
    if not text:
        return 0
    s = str(text)
    ascii_chars = sum(1 for ch in s if ord(ch) < 128)
    non_ascii = len(s) - ascii_chars
    raw = ascii_chars * CONTROL_TOKENS_PER_CHAR + non_ascii * TOKENS_PER_CHAR
    return int(math.ceil(round(raw, 9)))


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
        + estimate_control_tokens(control_instruction)
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


def _split_sentences_with_para(text: str) -> list[tuple[str, bool]]:
    """Split text into ``(sentence, starts_new_paragraph)`` pairs.

    ``starts_new_paragraph`` is True when two or more newlines (``\\n\\n``,
    ignoring spaces/tabs between newlines) preceded the sentence.
    """
    items: list[tuple[str, bool]] = []
    buf: list[str] = []
    nl_streak = 0
    pending_para = False
    for ch in str(text or ""):
        if ch == "\r":
            continue
        if ch == "\n":
            piece = "".join(buf).strip()
            if piece:
                items.append((piece, pending_para and len(items) > 0))
                pending_para = False
            buf = []
            nl_streak += 1
            if nl_streak >= 2:
                pending_para = True
            continue
        if ch in (" ", "\t") and not buf and nl_streak > 0:
            continue
        nl_streak = 0
        buf.append(ch)
        if ch in _SENTENCE_ENDINGS:
            piece = "".join(buf).strip()
            if piece:
                items.append((piece, pending_para and len(items) > 0))
                pending_para = False
            buf = []
    tail = "".join(buf).strip()
    if tail:
        items.append((tail, pending_para and len(items) > 0))
    return items


def split_sentences(text: str) -> list[str]:
    """Split text into sentences on Burmese/Latin sentence endings
    (။ ? ! and line breaks), keeping the ending punctuation attached.
    Pieces are stripped; empty pieces are dropped."""
    return [piece for piece, _is_para in _split_sentences_with_para(text)]


def _hard_split(sentence: str, budget_chars: int) -> list[str]:
    """Split one over-budget sentence at safe boundaries:
    1. Clause marks ("၊")
    2. Phrase spaces (" ")
    3. Burmese syllable boundaries (never inside a syllable cluster)
    4. Safe character boundary avoiding combining mark splits.
    Every piece is <= budget_chars."""
    if len(sentence) <= budget_chars:
        return [sentence]
    has_spaces = " " in sentence
    pieces: list[str] = []
    rest = sentence.strip() if has_spaces else sentence
    while len(rest) > budget_chars:
        # 1. Clause mark "၊"
        c_mark = rest.rfind(_CLAUSE_MARK, 0, budget_chars)
        if c_mark >= budget_chars // 3:
            cut = c_mark + 1
            piece = rest[:cut].strip()
            rest = rest[cut:].strip()
        else:
            # 2. Phrase space " "
            s_mark = rest.rfind(" ", 0, budget_chars)
            if s_mark >= budget_chars // 3:
                cut = s_mark
                piece = rest[:cut].strip()
                rest = rest[cut:].strip()
            else:
                # 3. Syllable boundary
                syl_cuts = [m.start() for m in BURMESE_SYLLABLE_BREAK.finditer(rest[:budget_chars])]
                valid_syl_cuts = [c for c in syl_cuts if c >= budget_chars // 3]
                if valid_syl_cuts:
                    cut = valid_syl_cuts[-1]
                    piece = rest[:cut]
                    rest = rest[cut:]
                    if has_spaces:
                        piece = piece.strip()
                        rest = rest.strip()
                else:
                    # 4. Safe fallback: don't break combining marks or virama/asat stacks
                    cut = budget_chars
                    while cut > 1 and cut < len(rest) and (
                        _is_combining_or_stack(rest[cut])
                        or rest[cut - 1] in ("\u1039", "\u103A")
                    ):
                        cut -= 1
                    piece = rest[:cut]
                    rest = rest[cut:]
                    if has_spaces:
                        piece = piece.strip()
                        rest = rest.strip()
        if piece:
            pieces.append(piece)
    if rest:
        pieces.append(rest)
    return [p for p in pieces if p]


def _plan_text_chunks_greedy_v1(text: str, effective_cap: int) -> list[str]:
    """Phase 2 greedy left-to-right sentence packer (used for single-chunk checks and kill-switch fallback)."""
    norm_check = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) <= effective_cap and "\n\n" not in norm_check:
        return [text]

    chunks: list[str] = []
    current = ""
    para_break_floor = min(PARAGRAPH_BREAK_MIN_CHARS, max(MIN_CHUNK_CHARS, effective_cap // 2))
    for sentence, starts_new_para in _split_sentences_with_para(text):
        if starts_new_para and current and len(current) >= para_break_floor:
            chunks.append(current)
            current = ""
        for piece in _hard_split(sentence, effective_cap):
            if not current:
                current = piece
            elif len(current) + 1 + len(piece) <= effective_cap:
                current = f"{current} {piece}"
            else:
                chunks.append(current)
                current = piece
    if current:
        chunks.append(current)
    return chunks or [text]


def _plan_text_chunks_balanced_dp(
    text: str,
    effective_cap: int,
    greedy_chunks: list[str],
) -> list[str]:
    """Phase 3 sentence-level DP balanced partitioning minimizing Σ(syls_i - target_syls)^2."""
    units: list[tuple[str, int, int, int, bool, str]] = []
    for sentence, starts_new_para in _split_sentences_with_para(text):
        pieces = _hard_split(sentence, effective_cap)
        unspaced_hard = len(pieces) > 1 and (" " not in sentence)
        for p_idx, piece in enumerate(pieces):
            if not piece:
                continue
            raw_s = _count_syllables(piece)
            eff_s = max(1, raw_s)
            is_para = bool(starts_new_para and p_idx == 0 and len(units) > 0)
            sep = "" if (p_idx > 0 and unspaced_hard) else " "
            units.append((piece, len(piece), raw_s, eff_s, is_para, sep))

    n_u = len(units)
    if n_u <= 1:
        return greedy_chunks

    total_raw_syls = sum(u[2] for u in units)
    total_eff_syls = sum(u[3] for u in units)
    total_chars = sum(u[1] for u in units) + sum(len(units[k][5]) for k in range(1, n_u))

    if effective_cap >= MAX_CHUNK_CHARS:
        target_max_chars = min(effective_cap, TARGET_CHUNK_CHARS)
    else:
        target_max_chars = min(
            effective_cap,
            max(MIN_CHUNK_CHARS, int(round(effective_cap * (TARGET_CHUNK_CHARS / float(MAX_CHUNK_CHARS))))),
        )

    suffix_raw_syls = [0] * (n_u + 1)
    for k in range(n_u - 1, -1, -1):
        suffix_raw_syls[k] = suffix_raw_syls[k + 1] + units[k][2]

    # Precompute all valid spans [i..j) with joined length <= effective_cap.
    valid_spans: list[list[tuple[int, int, int, int]]] = [[] for _ in range(n_u)]
    for i in range(n_u):
        c_len = 0
        r_syl = 0
        e_syl = 0
        for j in range(i + 1, n_u + 1):
            u_txt, u_len, u_rs, u_es, _u_para, u_sep = units[j - 1]
            add_len = u_len if j - 1 == i else (len(u_sep) + u_len)
            if j - 1 > i and c_len + add_len > effective_cap:
                break
            c_len += add_len
            r_syl += u_rs
            e_syl += u_es
            if c_len <= effective_cap or j == i + 1:
                valid_spans[i].append((j, c_len, r_syl, e_syl))

    m_lo = max(2, int(math.ceil(total_chars / float(max(1, effective_cap)))))
    # Never plan more chunks than the Phase 2 greedy pass (guarantees 0 extra synthesis calls, §0.1)
    m_greedy = max(m_lo, len(greedy_chunks))
    m_pref = min(m_greedy, max(m_lo, int(math.ceil(total_chars / float(max(1, target_max_chars))))))
    m_hi = min(n_u, m_greedy)
    enforce_tail_guard = total_raw_syls >= MIN_JOB_SYLLABLES_FOR_TAIL_GUARD

    best_overall_cost = float("inf")
    best_breaks: list[tuple[int, int]] | None = None

    for m_cand in range(m_lo, m_hi + 1):
        t_syl = total_eff_syls / float(m_cand)
        para_bonus = (0.45 * t_syl) ** 2
        dp = [[float("inf")] * (n_u + 1) for _ in range(m_cand + 1)]
        parent = [[-1] * (n_u + 1) for _ in range(m_cand + 1)]
        dp[0][0] = 0.0

        for step in range(1, m_cand + 1):
            rem_chunks_after = m_cand - step
            # Prune unreachable states
            i_max = n_u - rem_chunks_after
            for i in range(step - 1, i_max):
                base_c = dp[step - 1][i]
                if not math.isfinite(base_c):
                    continue
                for j, sp_chars, sp_raw_syls, sp_eff_syls in valid_spans[i]:
                    if n_u - j < rem_chunks_after:
                        continue
                    if step == m_cand and j != n_u:
                        continue
                    if step < m_cand and j == n_u:
                        continue

                    cost = (sp_eff_syls - t_syl) ** 2
                    if sp_chars > target_max_chars:
                        over_syl_eq = (sp_chars - target_max_chars) * (t_syl / max(1.0, float(target_max_chars)))
                        cost += 0.10 * (over_syl_eq ** 2)

                    if enforce_tail_guard and sp_raw_syls < MIN_TAIL_SYLLABLES:
                        deficit = MIN_TAIL_SYLLABLES - sp_raw_syls
                        if j == n_u:
                            cost += 1e7 + 1e4 * (deficit ** 2)
                        else:
                            cost += 5e6 + 5e3 * (deficit ** 2)

                    # Soft paragraph break bonus ONLY when remaining text >= MIN_TAIL_SYLLABLES (lookahead guard)
                    if j < n_u and units[j][4]:
                        rem_syls = suffix_raw_syls[j]
                        if rem_syls >= MIN_TAIL_SYLLABLES and (
                            not enforce_tail_guard or sp_raw_syls >= MIN_TAIL_SYLLABLES
                        ):
                            cost -= para_bonus

                    cand_total = base_c + cost
                    if cand_total < dp[step][j]:
                        dp[step][j] = cand_total
                        parent[step][j] = i

        if math.isfinite(dp[m_cand][n_u]):
            total_m_cost = dp[m_cand][n_u] + ((0.18 * t_syl) ** 2) * abs(m_cand - m_pref)
            if total_m_cost < best_overall_cost:
                best_overall_cost = total_m_cost
                spans_rev: list[tuple[int, int]] = []
                cur_j = n_u
                for st_back in range(m_cand, 0, -1):
                    prev_i = parent[st_back][cur_j]
                    spans_rev.append((prev_i, cur_j))
                    cur_j = prev_i
                spans_rev.reverse()
                best_breaks = spans_rev

    if not best_breaks:
        return greedy_chunks

    out_chunks: list[str] = []
    for i, j in best_breaks:
        buf = units[i][0]
        for k in range(i + 1, j):
            buf = f"{buf}{units[k][5]}{units[k][0]}"
        out_chunks.append(buf)
    return out_chunks or greedy_chunks


def plan_text_chunks(
    text: str,
    *,
    reference_transcript: str = "",
    control_instruction: str = "",
    reference_seconds: float = 0.0,
    max_chunk_chars: int = MAX_CHUNK_CHARS,
) -> list[str]:
    """Plan synthesis chunks for a voice-clone job's target text.

    Returns [text] unchanged when the whole text fits in one call
    (the common case — no behaviour change). Otherwise returns >= 2
    chunks, each within the character budget derived from the token
    budget minus the reference/control overhead (soft-capped at
    ``max_chunk_chars``), packed via balanced sentence DP when
    ``chunk_balancing_v2_enabled`` is True (or Phase 2 greedy packing when False).
    """
    text = str(text or "")
    if not text.strip():
        return [text]
    budget_chars = chunk_char_budget(
        reference_transcript=reference_transcript,
        control_instruction=control_instruction,
        reference_seconds=reference_seconds,
    )
    effective_cap = min(budget_chars, max(MIN_CHUNK_CHARS, int(max_chunk_chars)))
    greedy_chunks = _plan_text_chunks_greedy_v1(text, effective_cap)
    if len(greedy_chunks) <= 1 or not is_chunk_balancing_v2_enabled():
        return greedy_chunks
    return _plan_text_chunks_balanced_dp(text, effective_cap, greedy_chunks)


__all__ = [
    "INPUT_TOKEN_BUDGET",
    "MODEL_TOKEN_CAP",
    "TOKENS_PER_CHAR",
    "CONTROL_TOKENS_PER_CHAR",
    "REF_AUDIO_TOKENS_PER_SECOND",
    "REFERENCE_MAX_SECONDS",
    "MIN_CHUNK_CHARS",
    "MAX_CHUNK_CHARS",
    "TARGET_CHUNK_CHARS",
    "MIN_TAIL_SYLLABLES",
    "is_chunk_balancing_v2_enabled",
    "estimate_tokens",
    "estimate_control_tokens",
    "input_overhead_tokens",
    "estimated_input_tokens",
    "chunk_char_budget",
    "split_sentences",
    "plan_text_chunks",
]

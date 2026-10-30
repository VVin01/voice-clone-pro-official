"""User narration script editing for recap jobs.

Ported feature (2026-10-29) from the owner's desktop Recapter tool
(``app/narration_editor.py`` — its "Edit Text" window): after the planner
has transcribed + translated a video, the user can read every source line
next to its Burmese narration and fix the Burmese by hand BEFORE any TTS
or rendering happens.

Semantics kept identical to the desktop tool:

- Edits are written into ``smart_sync_plan.json`` (``narration`` and
  ``translation`` of the edited row) and mirrored into
  ``timestamp_translations.json`` (same ``atomic_id``), so a later plan
  rebuild/reuse can never resurrect the unedited text.
- The renderer's per-segment WAV cache is keyed by a fingerprint that
  includes the narration text (``segment_fingerprint`` in core.py), so on
  render only edited lines are re-voiced; every other line keeps its WAV.
- ``narration_problems`` mirrors the desktop editor's live warnings:
  Latin words, letters of another script, translator notes instead of
  narration, and mixed Myanmar/Latin digits.  Problems are warnings, not
  blockers — the user may keep a line on purpose — but empty lines are
  rejected (the desktop editor refuses to save an empty line too).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .core import normalize_segments
from .recap_planner import (
    _foreign_letters,
    _latin_words,
    _note_text,
    normalize_burmese_homoglyphs,
)

MAX_NARRATION_CHARS = 1000

_MIXED_DIGITS_RE = re.compile(r"[၀-၉][0-9]|[0-9][၀-၉]")


def narration_problems(text: str) -> list[str]:
    """Same checks as the desktop narration editor's problem line."""
    issues: list[str] = []
    latin = _latin_words(text)
    if latin:
        issues.append("English words: " + ", ".join(latin[:6]) + " (write them in Burmese)")
    letters = _foreign_letters(text)
    if letters:
        issues.append("letters of another script: " + " ".join(letters))
    if _note_text(text):
        issues.append("a note about IDs instead of narration")
    if _MIXED_DIGITS_RE.search(str(text or "")):
        issues.append("mixed Myanmar/Latin digits (e.g. ၂၀0၁)")
    return issues


def clean_edited_narration(text: Any) -> str:
    """Light-touch cleanup of a user-edited line: collapse whitespace and
    normalize foreign homoglyphs to real Myanmar characters.

    Deliberately does NOT re-run the full orthography normalizer on user
    text: the user's own spelling must never be silently rewritten (and on
    the live tree that normalizer still carries the known ခန→ခဏ overreach
    that corrupts correct words like အခန်း — fixed separately on the
    recap-script-spelling branch)."""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    if value:
        value = normalize_burmese_homoglyphs(value)
    return value


def list_script_segments(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """Ordered, render-ready view of the plan for the script editor UI.

    ``index`` is the 1-based segment number the renderer (and the user)
    sees; it comes from :func:`core.normalize_segments`, the same ordering
    the Smart Sync renderer uses.
    """
    out: list[dict[str, Any]] = []
    rows = plan.get("segments") or []
    for seg in normalize_segments(plan):
        row = rows[int(seg["source_index"])]
        out.append({
            "index": int(seg["segment_id"]),
            "start": float(seg["video_start"]),
            "end": float(seg["video_end"]),
            "timestamp": str(row.get("timestamp") or ""),
            "source_text": str(row.get("source_text") or ""),
            "narration": str(seg["narration"]),
        })
    return out


def apply_script_edits(
    plan: dict[str, Any],
    translations: dict[str, Any] | None,
    edits: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply user edits to *plan* (and the translations checkpoint) in place.

    Each edit is ``{"index": <1-based segment number>, "narration": str}``.
    Raises ValueError on an unknown index, an empty line, or an over-long
    line — callers turn that into an HTTP 400 and write nothing.
    Returns ``{"updated": n, "warnings": {index: [problems...]}}``.
    """
    by_index: dict[int, int] = {}
    for seg in normalize_segments(plan):
        by_index[int(seg["segment_id"])] = int(seg["source_index"])

    # Validate everything first: a bad edit anywhere in the batch must
    # leave the plan completely untouched.
    prepared: list[tuple[int, str]] = []
    for edit in edits:
        try:
            index = int(edit.get("index"))
        except Exception:
            raise ValueError(f"Invalid segment index: {edit.get('index')!r}")
        if index not in by_index:
            raise ValueError(f"Unknown segment index: {index}")
        text = clean_edited_narration(edit.get("narration"))
        if not text:
            raise ValueError(f"Segment {index}: narration must not be empty")
        if len(text) > MAX_NARRATION_CHARS:
            raise ValueError(
                f"Segment {index}: narration is too long ({len(text)} > {MAX_NARRATION_CHARS} chars)"
            )
        prepared.append((index, text))

    rows = plan.get("segments") or []
    warnings: dict[int, list[str]] = {}
    updated = 0
    for index, text in prepared:
        row = rows[by_index[index]]
        if str(row.get("narration") or "") == text:
            continue
        row["narration"] = text
        row["translation"] = text
        updated += 1
        problems = narration_problems(text)
        if problems:
            warnings[index] = problems
        # Mirror into the translation checkpoint (desktop editor parity):
        # if the plan is ever rebuilt, the edited line survives.
        atomic_id = row.get("atomic_id")
        if translations and atomic_id is not None:
            for item in translations.get("items") or []:
                try:
                    if int(item.get("id", -1)) == int(atomic_id):
                        item["translation"] = text
                except Exception:
                    continue
    return {"updated": updated, "warnings": warnings}


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON via a temp file + rename (desktop editor parity), so a
    crash mid-save can never leave a half-written plan behind."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)

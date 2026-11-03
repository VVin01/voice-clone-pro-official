"""Premium branded download filenames (2026-11-03).

One consistent scheme for every user-facing download / delivery name:

    VoiceClonePro_<Kind>_<Label>.<ext>

``Label`` is the sanitized content title when one is available, otherwise
the job tag (last 6 alphanumeric characters of the job id). Parts get a
``_PartNN`` suffix before the extension. Only the *display* name changes
(Content-Disposition, Telegram upload names, client-side save names) —
files on disk keep their existing names.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

BRAND_PREFIX = "VoiceClonePro"
MAX_FILENAME_LEN = 90

_KIND_STRIP_RE = re.compile(r"[^A-Za-z0-9]")
_EXT_STRIP_RE = re.compile(r"[^a-z0-9]")
_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")


def sanitize_filename_label(raw: object) -> str:
    """Make a title filesystem-safe for use inside a download filename.

    Keeps letters, marks and numbers (so Burmese titles survive with
    their combining marks intact) plus spaces and hyphens; path
    separators, control characters and punctuation are dropped.
    Whitespace runs become single underscores.
    """
    if not raw:
        return ""
    s = unicodedata.normalize("NFC", str(raw))
    kept = []
    for ch in s:
        if unicodedata.category(ch)[0] in ("L", "M", "N") or ch in (" ", "-", "_"):
            kept.append(ch)
        elif ch.isspace():
            kept.append(" ")
    s = re.sub(r"\s+", "_", "".join(kept))
    s = re.sub(r"_+", "_", s)
    return s.strip("_-. ")


def job_tag(job_id: object) -> str:
    """Short fallback label: last 6 alphanumeric chars of the job id."""
    alnum = _ALNUM_RE.sub("", str(job_id or ""))
    return alnum[-6:] if alnum else ""


def branded_download_filename(
    kind: str,
    ext: str,
    *,
    title: Optional[str] = None,
    job_id: Optional[str] = None,
    part: Optional[int] = None,
) -> str:
    """Build a ``VoiceClonePro_<Kind>_<Label>.<ext>`` download filename.

    ``kind`` is the branded kind segment (``Recap``, ``RecapAudio``,
    ``Subtitles``, ``Script``, ``Voice``). ``ext`` is the file's real
    extension (leading dot optional). The whole name is capped at
    ~90 characters by truncating the label, never the brand or the
    extension.
    """
    kind_clean = _KIND_STRIP_RE.sub("", str(kind or "")) or "File"
    ext_clean = _EXT_STRIP_RE.sub("", str(ext or "").lower().lstrip(".")) or "bin"
    label = sanitize_filename_label(title)
    if not label:
        label = job_tag(job_id) or "File"
    part_suffix = ""
    if part is not None:
        try:
            part_suffix = f"_Part{int(part):02d}"
        except (TypeError, ValueError):
            part_suffix = ""
    prefix = f"{BRAND_PREFIX}_{kind_clean}_"
    suffix = f"{part_suffix}.{ext_clean}"
    budget = max(0, MAX_FILENAME_LEN - len(prefix) - len(suffix))
    if len(label) > budget:
        label = label[:budget].rstrip("_-. ") or label[:budget]
    return f"{prefix}{label}{suffix}"

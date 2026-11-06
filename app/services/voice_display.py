"""Shared display-name resolution for recap narration voices (R40 item 1).

A recap job records only the persona slug on its Job row, but when the
narration engine is Gemini the audio the user hears is a Gemini voice —
completion notifications and delivery captions that printed the persona
slug ("Komin") for a Puck-voiced job were simply wrong. Every user-facing
surface must resolve the name through :func:`recap_voice_display_name`:

* engine == "gemini"  -> the Gemini voice's Burmese display name
  (``GEMINI_VOICE_DISPLAY_MY``, the same names the Mini App picker shows);
* engine == "edge" / "edge_tts" (Batch AW) -> "🎧 Edge TTS ·" plus the
  Edge base voice's name (the persona's gender-matched Thiha/Nilar);
* otherwise           -> the persona's Burmese display name from
  ``curated_voices`` (short form, without the role gloss).

Unknown slugs fall back to the capitalised slug so legacy rows still
render something sensible.
"""
from __future__ import annotations

import re
from typing import Optional

from app.services.recap_engine.gemini_tts_engine import GEMINI_VOICE_DISPLAY_MY

_GLOSS_RE = re.compile(r"\s*\([^)]*\)\s*$")


def gemini_voice_display_name(voice: Optional[str]) -> Optional[str]:
    """Burmese display name for a Gemini voice name, or None."""
    if not voice:
        return None
    name = str(voice).strip().removeprefix("gemini:")
    return GEMINI_VOICE_DISPLAY_MY.get(name)


def persona_display_name(persona_slug: Optional[str]) -> str:
    """Short Burmese display name for a curated persona slug."""
    slug = str(persona_slug or "").strip()
    if not slug:
        return ""
    try:
        from app.services.curated_voices import get_curated_voice

        info = get_curated_voice(slug)
    except Exception:
        info = None
    if info:
        name = str(info.get("name_my") or info.get("name_en") or "").strip()
        if name:
            return _GLOSS_RE.sub("", name).strip()
    return slug.capitalize()


_EDGE_BASE_DISPLAY_MY = {
    "my-MM-ThihaNeural": "ကိုသီဟ",
    "my-MM-NilarNeural": "မနီလာ",
}
_LEGACY_FEMALE_EDGE_SLUGS = frozenset(
    {"mahnin", "nilar", "mamay", "manadi", "mayoon", "machaw", "mathiri"}
)


def edge_voice_display_name(persona_slug: Optional[str]) -> str:
    """Burmese display name for the Edge base voice a recap job used:
    the persona's gender-matched Edge base (Thiha/Nilar), the legacy
    female slugs' Nilar, Thiha otherwise."""
    slug = str(persona_slug or "").strip().lower()
    edge_voice = None
    if slug:
        try:
            from app.services.curated_voices import get_curated_voice

            info = get_curated_voice(slug)
        except Exception:
            info = None
        if info:
            edge_voice = info.get("edge_voice")
    if not edge_voice:
        edge_voice = (
            "my-MM-NilarNeural" if slug in _LEGACY_FEMALE_EDGE_SLUGS else "my-MM-ThihaNeural"
        )
    return _EDGE_BASE_DISPLAY_MY.get(edge_voice, "ကိုသီဟ")


def recap_voice_display_name(
    tts_engine: Optional[str],
    gemini_voice: Optional[str],
    persona_slug: Optional[str],
) -> str:
    """The voice name a user should see for a recap job.

    Gemini engine + a known Gemini voice wins over the persona slug
    (the persona is only the per-segment fallback in that mode). The
    Edge engine (Batch AW: user-selected "edge", internally
    "edge_tts") names the Edge base voice that actually narrated —
    labelling it with the persona's name would claim a clone the user
    did not hear. Every other engine names the persona.
    """
    engine = str(tts_engine or "").strip().lower()
    if engine == "gemini":
        display = gemini_voice_display_name(gemini_voice)
        if display:
            return display
    if engine in ("edge", "edge_tts"):
        return f"🎧 Edge TTS · {edge_voice_display_name(persona_slug)}"
    return persona_display_name(persona_slug)

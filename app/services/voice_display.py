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

Batch BI (2026-11-06) exception: the bot's COMPLETION message resolves
through :func:`recap_completion_voice_display_name` instead — it names
the voice the user PICKED (the persona label the Mini App's Step 4 and
History show), because a completion line that said "🎙️ အသံ: ကိုသီဟ"
for a job the user set to ကိုမင်း read as the wrong voice next to the
app (owner report, Milo job rc_1793951089_a4f17f). The on-demand
delivery caption keeps the heard-voice resolver above.

Batch BK (2026-11-06) reversal: the owner reversed the BI exception
the same day — the completion message names the voice ACTUALLY USED
(the heard-voice resolver above) after all, and
:func:`recap_completion_voice_display_name` is retained unchanged but
no longer called by the notification. When the Gemini engine mapped
the picked persona to a differently-named Gemini voice, the message
adds a short polite note explaining the difference;
:func:`recap_voice_name_note_params` decides that case. The app /
History keep showing the picked name — that split is the owner's
settled preference.
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


def recap_completion_voice_display_name(
    tts_engine: Optional[str],
    gemini_voice: Optional[str],
    persona_slug: Optional[str],
) -> str:
    """The voice name for the bot's recap COMPLETION message (Batch BI).

    Batch BK (2026-11-06): retained unchanged, but the completion
    message no longer calls this resolver — the owner reversed BI's
    rule the same day and the notification names the heard voice via
    :func:`recap_voice_display_name` again (with the explanatory note
    from :func:`recap_voice_name_note_params` when the names differ).

    The completion message names the voice the user PICKED — the
    persona display name the Mini App shows in Step 4 and History —
    because that is the name the user chose and recognises. For
    Gemini-engine jobs the heard-voice resolver above would leak the
    Gemini voice's own display name instead (a free-tier job the user
    set to ကိုမင်း completed with "🎙️ အသံ: ကိုသီဟ", owner report
    2026-11-06), which reads as a different voice next to the app.
    Edge jobs keep the honest Edge label — the persona name would
    claim a clone the user did not hear (Batch AW) — and every other
    engine already names the persona. ``gemini_voice`` is accepted
    for call-site symmetry with :func:`recap_voice_display_name` and
    is deliberately unused.
    """
    engine = str(tts_engine or "").strip().lower()
    if engine in ("edge", "edge_tts"):
        return f"🎧 Edge TTS · {edge_voice_display_name(persona_slug)}"
    return persona_display_name(persona_slug)


def recap_voice_name_note_params(
    tts_engine: Optional[str],
    gemini_voice: Optional[str],
    persona_slug: Optional[str],
) -> Optional[dict[str, str]]:
    """Interpolation params for the completion message's voice-name
    note (Batch BK), or None when no note is owed.

    The note exists for exactly one case: the Gemini engine mapped the
    persona the user picked to a Gemini voice whose display name
    differs from the persona's (picked ကိုမင်း, heard ကိုသီဟ) — the
    completion line names the heard voice, so the difference is
    explained politely instead of looking like a mix-up. Every other
    case returns None: VoxCPM/persona engines name the persona itself
    (heard == picked); a Gemini job whose voice is unknown falls back
    to the persona name (heard == picked again); Edge jobs carry the
    self-explanatory "🎧 Edge TTS · …" label the user opted into, so a
    note would only repeat it. The caller appends the note only when
    the job's fallback note did not already explain a substitution.
    """
    engine = str(tts_engine or "").strip().lower()
    if engine != "gemini":
        return None
    heard = recap_voice_display_name(tts_engine, gemini_voice, persona_slug)
    picked = persona_display_name(persona_slug)
    if not heard or heard == picked:
        return None
    return {"picked_name": picked, "heard_name": heard, "engine_name": "Gemini"}

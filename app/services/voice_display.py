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
from typing import Any, Mapping, Optional

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
    "en-US-AndrewMultilingualNeural": "ကိုအန်ဒရူး",
    "en-US-BrianMultilingualNeural": "ကိုဘရိုင်ယန်",
    "fr-FR-RemyMultilingualNeural": "ကိုရီမီ",
    "en-US-AvaMultilingualNeural": "မအေဗာ",
    "en-US-EmmaMultilingualNeural": "မအမ်မာ",
    "fr-FR-VivienneMultilingualNeural": "မဗီဗီယန်",
}
_EDGE_SHORT_KEY_TO_NEURAL = {
    "thiha": "my-MM-ThihaNeural",
    "nilar": "my-MM-NilarNeural",
    "andrew": "en-US-AndrewMultilingualNeural",
    "brian": "en-US-BrianMultilingualNeural",
    "remy": "fr-FR-RemyMultilingualNeural",
    "ava": "en-US-AvaMultilingualNeural",
    "emma": "en-US-EmmaMultilingualNeural",
    "vivienne": "fr-FR-VivienneMultilingualNeural",
}
_EDGE_BASE_DISPLAY_MY_LOWER = {k.lower(): v for k, v in _EDGE_BASE_DISPLAY_MY.items()}
_LEGACY_FEMALE_EDGE_SLUGS = frozenset(
    {"mahnin", "nilar", "mamay", "manadi", "mayoon", "machaw", "mathiri"}
)


def _lookup_edge_display_name(raw_voice: Optional[str]) -> Optional[str]:
    """Resolve an Edge short key ('andrew') or neural id ('en-US-AndrewMultilingualNeural')
    to its Burmese display name, or None when unrecognized."""
    raw = str(raw_voice or "").strip()
    if not raw:
        return None
    if raw in _EDGE_BASE_DISPLAY_MY:
        return _EDGE_BASE_DISPLAY_MY[raw]
    low = raw.lower()
    if low in _EDGE_BASE_DISPLAY_MY_LOWER:
        return _EDGE_BASE_DISPLAY_MY_LOWER[low]
    neural = _EDGE_SHORT_KEY_TO_NEURAL.get(low)
    if neural:
        return _EDGE_BASE_DISPLAY_MY[neural]
    return None


def edge_voice_display_name(
    persona_slug: Optional[str] = None,
    *,
    edge_voice: Optional[str] = None,
) -> str:
    """Burmese display name for the Edge voice a recap job used.

    An explicit ``edge_voice`` (any of the 8 Round-13 Edge voices, by short key
    or canonical neural id) always wins over persona-derived Thiha/Nilar mapping.
    """
    direct = _lookup_edge_display_name(edge_voice)
    if direct:
        return direct
    slug_direct = _lookup_edge_display_name(persona_slug)
    if slug_direct:
        return slug_direct
    slug = str(persona_slug or "").strip().lower()
    resolved_edge = None
    if slug:
        try:
            from app.services.curated_voices import get_curated_voice

            info = get_curated_voice(slug)
        except Exception:
            info = None
        if info:
            resolved_edge = info.get("edge_voice")
    if not resolved_edge:
        resolved_edge = (
            "my-MM-NilarNeural" if slug in _LEGACY_FEMALE_EDGE_SLUGS else "my-MM-ThihaNeural"
        )
    return _EDGE_BASE_DISPLAY_MY.get(resolved_edge, "ကိုသီဟ")


def own_voice_display_name() -> str:
    """Display name for a Batch BV own-voice recap narration (the
    user's uploaded sample cloned by VoxCPM). Resolved through the
    server i18n table like every other bot-facing voice label; the
    hard-coded fallback matches the "my" string so a locale-load
    failure can never blank the completion message."""
    try:
        from app.services.i18n import t

        name = t("recap_own_voice_display", "my")
        if name and name != "recap_own_voice_display":
            return name
    except Exception:
        pass
    return "🎙️ ကိုယ်ပိုင်အသံ"


def recap_voice_display_name(
    tts_engine: Optional[str],
    gemini_voice: Optional[str],
    persona_slug: Optional[str],
    own_voice: bool = False,
    *,
    edge_voice: Optional[str] = None,
) -> str:
    """The voice name a user should see for a recap job.

    Gemini engine + a known Gemini voice wins over the persona slug
    (the persona is only the per-segment fallback in that mode). The
    Edge engine (Batch AW: user-selected "edge", internally
    "edge_tts") names the Edge voice that actually narrated —
    labelling it with the persona's name would claim a clone the user
    did not hear. Every other engine names the persona.

    Batch BV: an own-voice job (``own_voice=True``) narrated on the
    clone engine names the user's own voice — the persona slug on
    the row is only that job's per-segment fallback voice. The flag
    is ignored for the Gemini/Edge engines: a job whose narration
    actually ran there is named for what was heard, exactly as
    before (an own-voice ref can only ever execute on voxcpm — the
    tier/engine clamps drop it everywhere else).
    """
    engine = str(tts_engine or "").strip().lower()
    if engine == "gemini":
        display = gemini_voice_display_name(gemini_voice)
        if display:
            return display
    if engine in ("edge", "edge_tts"):
        return f"🎧 Edge TTS · {edge_voice_display_name(persona_slug, edge_voice=edge_voice)}"
    if own_voice:
        return own_voice_display_name()
    return persona_display_name(persona_slug)


# ---------------------------------------------------------------------------
# Standalone TTS display names (Batch BX). A TTS job row persists the
# RAW engine voice ("Charon" for Gemini, "my-MM-ThihaNeural" for Edge)
# in preset_voice — internal ids that leaked into user History and
# were missing entirely from the bot delivery message. TTS rows never
# carry a persona slug, so the recap resolvers above do not apply.
# ---------------------------------------------------------------------------

_TTS_ENGINE_DISPLAY = {"gemini": "Gemini", "edge": "Edge", "edge_tts": "Edge"}


def tts_voice_display_name(engine: Optional[str], preset_voice: Optional[str]) -> str:
    """User-facing voice name for a standalone TTS job row.

    Gemini rows resolve through the same Gemini display table the
    recap path uses; Edge rows map the persisted neural name to its
    Burmese base name. Unknown values degrade to the raw value so a
    display never blanks.
    """
    engine_norm = str(engine or "").strip().lower()
    raw = str(preset_voice or "").strip()
    if engine_norm == "gemini":
        display = gemini_voice_display_name(raw)
        if display:
            return display
        return raw.removeprefix("gemini:") or raw
    if engine_norm in ("edge", "edge_tts"):
        return _lookup_edge_display_name(raw) or (raw or "ကိုသီဟ")
    return raw


def tts_engine_display_name(engine: Optional[str]) -> str:
    """User-facing engine label for a standalone TTS job row."""
    engine_norm = str(engine or "").strip().lower()
    return _TTS_ENGINE_DISPLAY.get(engine_norm, engine_norm)


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


def resolve_recap_voice_presentation(
    requested: Optional[Mapping[str, Any]] = None,
    actual: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Single canonical presentation resolver for recap delivery surfaces (2B-2).

    Computes both the header voice label (``🎙️ အသံ:`` / History ``voice_display``)
    and the mutually exclusive explanatory note state strictly from what the
    engine ACTUALLY synthesized (``actual``) compared against what the user
    requested (``requested``).

    Returns a dict with:
      - ``header_voice``: display string for the header/voice line
      - ``actual_provider``: normalized provider actually used
      - ``actual_voice``: canonical voice id actually synthesized
      - ``actual_voice_name``: bare friendly voice name (e.g. ``ကိုသီဟ``)
      - ``provider_label``: human provider name (``Edge TTS``, ``Gemini``, ``VoxCPM``, ``""``)
      - ``note_kind``: one of ``"none"``, ``"uniform_restart"``, ``"all_fallback"``,
        ``"partial_fallback"``, ``"gemini_voice_mapped"``
      - ``note_params``: interpolation kwargs for the matching locale key, or ``None``
    """
    req = dict(requested or {})
    act = dict(actual or {})

    req_engine = str(req.get("tts_engine") or req.get("engine") or "").strip().lower()
    req_gemini_voice = str(req.get("gemini_voice") or "").strip() or None
    req_edge_voice = str(req.get("edge_voice") or "").strip() or None
    req_persona = str(req.get("voice_slug") or req.get("persona_slug") or "").strip() or None
    req_own_voice = bool(req.get("own_voice") or req.get("voice_ref_path"))

    if "gemini_voice_explicit" in req and req["gemini_voice_explicit"] is not None:
        gemini_voice_explicit = bool(req["gemini_voice_explicit"])
    else:
        gemini_voice_explicit = bool(req_gemini_voice)

    raw_provider = str(
        act.get("actual_tts_provider") or act.get("tts_provider") or ""
    ).strip().lower()
    raw_actual_voice = str(act.get("actual_voice") or "").strip() or None
    uniform_restart = bool(act.get("uniform_restart"))
    fallback_segs = act.get("fallback_segments") or []
    if act.get("fallback_count") is not None:
        try:
            fallback_count = max(0, int(act["fallback_count"]))
        except (TypeError, ValueError):
            fallback_count = len(fallback_segs)
    else:
        fallback_count = len(fallback_segs)
    fallback_used = bool(act.get("fallback_used") or fallback_count > 0)
    try:
        total_segments = max(0, int(act.get("total_segments") or 0))
    except (TypeError, ValueError):
        total_segments = 0
    all_fallback = bool(
        fallback_used and total_segments > 0 and fallback_count >= total_segments
    )

    # 1. Subtitle-only / no-voice mode
    if raw_provider in ("none", "subtitle_only", "no_voice") or (
        not raw_provider and req_engine in ("none", "subtitle_only", "no_voice")
    ):
        return {
            "header_voice": "📝 စာတန်းထိုးသာ",
            "actual_provider": "subtitle_only",
            "actual_voice": "",
            "actual_voice_name": "📝 စာတန်းထိုးသာ",
            "provider_label": "",
            "note_kind": "none",
            "note_params": None,
        }

    # 2. Actually voiced 100% on Edge TTS (uniform restart, native Edge, or 100% fallback)
    actually_all_edge = bool(
        uniform_restart
        or raw_provider in ("edge", "edge_tts")
        or (all_fallback and raw_provider in ("gemini+persona_fallback", "voxcpm+edge_fallback"))
        or (not raw_provider and req_engine in ("edge", "edge_tts"))
    )
    if actually_all_edge:
        edge_candidate = raw_actual_voice or req_edge_voice
        if edge_candidate and edge_candidate.lower().startswith(("voxcpm", "vox_")):
            edge_candidate = None
        bare_name = edge_voice_display_name(req_persona, edge_voice=edge_candidate)
        header_voice = f"🎧 Edge TTS · {bare_name}"
        if uniform_restart:
            note_kind = "uniform_restart"
            note_params: Optional[dict[str, Any]] = {
                "provider_name": "Edge TTS",
                "heard_name": bare_name,
            }
        elif all_fallback or (req_engine in ("gemini", "voxcpm") and raw_provider in ("edge", "edge_tts")):
            note_kind = "all_fallback"
            note_params = {
                "provider_name": "Edge TTS",
                "heard_name": bare_name,
                "count": fallback_count or total_segments or 1,
            }
        else:
            note_kind = "none"
            note_params = None
        return {
            "header_voice": header_voice,
            "actual_provider": "edge_tts",
            "actual_voice": str(edge_candidate or raw_actual_voice or req_edge_voice or ""),
            "actual_voice_name": bare_name,
            "provider_label": "Edge TTS",
            "note_kind": note_kind,
            "note_params": note_params,
        }

    # 3. Gemini TTS (clean or partial fallback)
    if raw_provider in ("gemini", "gemini+persona_fallback") or (
        not raw_provider and req_engine == "gemini"
    ):
        gem_voice = raw_actual_voice or req_gemini_voice
        header_voice = recap_voice_display_name("gemini", gem_voice, req_persona)
        if fallback_used:
            note_kind = "partial_fallback"
            note_params = {"count": fallback_count or 1}
        elif not gemini_voice_explicit:
            mapped = recap_voice_name_note_params("gemini", gem_voice, req_persona)
            if mapped:
                note_kind = "gemini_voice_mapped"
                note_params = mapped
            else:
                note_kind = "none"
                note_params = None
        else:
            note_kind = "none"
            note_params = None
        return {
            "header_voice": header_voice,
            "actual_provider": raw_provider or "gemini",
            "actual_voice": str(gem_voice or ""),
            "actual_voice_name": header_voice,
            "provider_label": "Gemini",
            "note_kind": note_kind,
            "note_params": note_params,
        }

    # 4. VoxCPM / Curated Persona / Own Voice (clean or partial fallback)
    effective_persona = req_persona
    if raw_actual_voice:
        _stripped = raw_actual_voice.removeprefix("voxcpm_").removeprefix("vox_")
        if _stripped:
            effective_persona = _stripped
    header_voice = recap_voice_display_name(
        "voxcpm", None, effective_persona, own_voice=req_own_voice
    )
    if fallback_used:
        note_kind = "partial_fallback"
        note_params = {"count": fallback_count or 1}
    else:
        note_kind = "none"
        note_params = None
    return {
        "header_voice": header_voice,
        "actual_provider": raw_provider or (req_engine or "voxcpm"),
        "actual_voice": str(raw_actual_voice or req_persona or ""),
        "actual_voice_name": header_voice,
        "provider_label": "VoxCPM",
        "note_kind": note_kind,
        "note_params": note_params,
    }

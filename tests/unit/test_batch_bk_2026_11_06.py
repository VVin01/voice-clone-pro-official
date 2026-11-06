"""Batch BK — completion message names the heard voice, plus a
polite note when it differs from the picked persona (2026-11-06).

Batch BI-3 had made the bot's recap completion message name the
voice the user PICKED (ကိုမင်း for a komin job). The owner reversed
that the same day: the message names the voice ACTUALLY USED — for a
Gemini-engine job, the Gemini voice that really read the recap
(ကိုသီဟ), which is the pre-BI behaviour of the heard-voice resolver
(:func:`recap_voice_display_name`, also used by the delivery
caption).

Owner refinement (2026-11-06 16:03): when the heard voice differs
from the picked persona, the message must politely EXPLAIN the
difference instead of silently showing another name. The note
(locale key ``recap_voice_name_note``, new in my + en) is appended
only for the Gemini engine-mapping case, decided by
:func:`recap_voice_name_note_params`; it never appears when the
names match (VoxCPM persona jobs), and it never stacks on the
fallback note, which already explains a substitution.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.services.i18n import t  # noqa: E402
from app.services.voice_display import (  # noqa: E402
    recap_voice_display_name,
    recap_voice_name_note_params,
)

API_SRC = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")
MY_MESSAGES = json.loads((ROOT / "locales/my/messages.json").read_text(encoding="utf-8"))
EN_MESSAGES = json.loads((ROOT / "locales/en/messages.json").read_text(encoding="utf-8"))


# ── The note decision ───────────────────────────────────────────────

def test_note_params_gemini_mapping_case():
    # The Milo case: picked komin, Gemini voice Charon actually read
    # it — the note is owed, with both names interpolated.
    params = recap_voice_name_note_params("gemini", "Charon", "komin")
    assert params == {
        "picked_name": "ကိုမင်း",
        "heard_name": "ကိုသီဟ",
        "engine_name": "Gemini",
    }


def test_note_params_none_when_names_match():
    # VoxCPM persona job: the heard voice IS the picked persona.
    assert recap_voice_name_note_params("voxcpm", None, "komin") is None
    assert recap_voice_name_note_params(None, None, "komin") is None
    # Gemini job whose voice is unknown: the resolver falls back to
    # the persona name, so the names match and no note is owed.
    assert recap_voice_name_note_params("gemini", None, "komin") is None
    assert recap_voice_name_note_params("gemini", "NoSuchVoice", "komin") is None


def test_note_params_none_for_edge_engine():
    # Edge jobs carry the self-explanatory "🎧 Edge TTS · …" label the
    # user opted into — no extra note.
    assert recap_voice_name_note_params("edge", None, "komin") is None
    assert recap_voice_name_note_params("edge_tts", None, "mathiri") is None


# ── The composed message ────────────────────────────────────────────

def _compose_notify(lang: str, engine, gemini_voice, slug: str) -> str:
    """Mirror the api.py completion composition (voice line + the
    name note when owed; the fallback branch is pinned separately)."""
    text = t(
        "recap_notify_text",
        lang,
        clean_title="Me at the zoo",
        voice_name=recap_voice_display_name(engine, gemini_voice, slug),
        job_id="rc_test",
    )
    params = recap_voice_name_note_params(engine, gemini_voice, slug)
    if params:
        text += t(
            "recap_voice_name_note",
            lang,
            picked_name=params["picked_name"],
            heard_name=params["heard_name"],
            engine_name=params["engine_name"],
        )
    return text


def test_completion_message_gemini_carries_heard_name_and_note():
    text = _compose_notify("my", "gemini", "Charon", "komin")
    # The voice line names the heard Gemini voice…
    assert "🎙️ <b>အသံ:</b> ကိုသီဟ" in text
    # …and the note explains the difference with BOTH names.
    assert "📝" in text
    assert "ရွေးထားတဲ့ 'ကိုမင်း' အသံအစား" in text
    assert "Gemini ရဲ့ 'ကိုသီဟ' အသံဖြင့်" in text


def test_completion_message_voxcpm_persona_no_note():
    text = _compose_notify("my", "voxcpm", None, "komin")
    assert "🎙️ <b>အသံ:</b> ကိုမင်း" in text
    assert "📝" not in text


def test_note_template_english_renders_with_both_names():
    text = _compose_notify("en", "gemini", "Charon", "komin")
    assert "instead of the 'ကိုမင်း' voice you picked" in text
    assert "Gemini's 'ကိုသီဟ' voice" in text


# ── The locale keys ─────────────────────────────────────────────────

def test_note_key_exists_in_both_locales_with_all_placeholders():
    for messages in (MY_MESSAGES, EN_MESSAGES):
        template = messages["recap_voice_name_note"]
        for placeholder in ("{picked_name}", "{heard_name}", "{engine_name}"):
            assert placeholder in template


# ── Wiring: the fallback note never stacks with the name note ───────

def test_notify_wiring_fallback_note_excludes_name_note():
    span = API_SRC.split('"recap_notify_text"', 1)[1].split(
        '"recap_retention_note"', 1
    )[0]
    fallback_if = span.index('if entry.get("fallback_used"):')
    fallback_note = span.index('"recap_fallback_note"')
    else_branch = span.index("else:", fallback_note)
    name_note = span.index('"recap_voice_name_note"')
    # The fallback note is appended inside the fallback branch; the
    # name note is appended only in the else branch, after it.
    assert fallback_if < fallback_note < else_branch < name_note
    # The else branch decides via the note-params helper.
    assert "recap_voice_name_note_params(" in span[else_branch:]

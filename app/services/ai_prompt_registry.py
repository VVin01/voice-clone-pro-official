"""Versioned AI Prompt Registry & Signature-Safe Gemini Transport Helper.

Centralizes runtime prompt profile versioning (``v2`` upgraded default with
instant ``v1`` rollback via ``AI_PROMPT_PROFILE=v1`` or ``bot_settings``) and
provides a signature-aware wrapper for ``client.models.generate_content`` that
applies structured ``GenerateContentConfig`` on real Gemini SDK transports while
degrading gracefully on two-argument test doubles.
"""

from __future__ import annotations

import inspect
import os
import re
from typing import Any, Callable

VALID_PROMPT_PROFILES: tuple[str, ...] = ("v1", "v2")
DEFAULT_PROMPT_PROFILE: str = "v2"


def get_prompt_profile() -> str:
    """Return active AI prompt profile (``'v2'`` default or ``'v1'`` rollback)."""
    raw: Any = None
    try:
        from app.config import settings

        raw = getattr(settings, "ai_prompt_profile", None)
    except Exception:
        raw = None
    if raw is None or not str(raw).strip():
        raw = os.getenv("AI_PROMPT_PROFILE", DEFAULT_PROMPT_PROFILE)
    norm = str(raw or DEFAULT_PROMPT_PROFILE).strip().lower()
    return norm if norm in VALID_PROMPT_PROFILES else DEFAULT_PROMPT_PROFILE


def is_v2_prompts() -> bool:
    """True when upgraded v2 prompts & context safeguards are active."""
    return get_prompt_profile() == "v2"


def sanitize_xml_tag_content(text: str, *tags: str) -> str:
    """Neutralize XML closing/opening tag injection inside untrusted text.

    Replaces literal ``</tag>`` and ``<tag>`` sequences (case-insensitive) for
    any specified wrapper tag names with HTML-escaped entities so user input
    cannot prematurely close delimiter blocks inside prompts.
    """
    value = str(text or "")
    if not value or not tags:
        return value
    for tag in tags:
        clean_tag = re.sub(r"[^A-Za-z0-9_\-]", "", str(tag or ""))
        if not clean_tag:
            continue
        pattern = re.compile(rf"<\s*(/?)\s*{re.escape(clean_tag)}\s*>", re.IGNORECASE)
        value = pattern.sub(lambda m: f"&lt;{m.group(1)}{clean_tag}&gt;", value)
    return value


def _merge_system_and_contents(system_instruction: str | None, contents: Any) -> Any:
    sys_txt = str(system_instruction or "").strip()
    if not sys_txt:
        return contents
    if isinstance(contents, str):
        return f"{sys_txt}\n\n{contents}"
    if isinstance(contents, list):
        return [sys_txt, *contents]
    return contents


def call_generate_content_compat(
    gen_fn: Callable[..., Any],
    *,
    model: str,
    contents: Any,
    system_instruction: str | None = None,
    config_kwargs: dict[str, Any] | None = None,
) -> Any:
    """Invoke ``generate_content`` passing ``GenerateContentConfig`` when supported.

    Falls back to a two-argument call ``(model=..., contents=...)`` with any
    ``system_instruction`` prepended to ``contents`` when ``gen_fn`` does not
    accept a ``config`` parameter (e.g., two-arg unit test mocks).
    """
    extra = {k: v for k, v in (config_kwargs or {}).items() if v is not None}
    sys_txt = str(system_instruction or "").strip() or None

    supports_config = False
    try:
        sig = inspect.signature(gen_fn)
        params = sig.parameters
        supports_config = ("config" in params) or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    except Exception:
        supports_config = False

    if supports_config and (sys_txt is not None or extra):
        try:
            from google.genai import types

            cfg_args: dict[str, Any] = dict(extra)
            if sys_txt is not None:
                cfg_args["system_instruction"] = sys_txt
            cfg = types.GenerateContentConfig(**cfg_args)
            return gen_fn(model=model, contents=contents, config=cfg)
        except TypeError as exc:
            msg = str(exc).lower()
            if "config" not in msg and "unexpected keyword" not in msg and "positional" not in msg:
                raise
        except ImportError:
            pass

    merged = _merge_system_and_contents(sys_txt, contents)
    return gen_fn(model=model, contents=merged)


PROMPT_REGISTRY_CATALOG: dict[str, dict[str, Any]] = {
    "group_ai_reply": {
        "module": "app/services/group_ai_admin.py",
        "function": "build_prompt",
        "model_family": "Gemini 3.x Text Chain",
        "temperature": 0.3,
        "rollback_flag": "AI_PROMPT_PROFILE=v1",
    },
    "group_ai_moderation": {
        "module": "app/services/group_ai_admin.py",
        "function": "detect_moderation_violation / parse_ai_decision",
        "model_family": "Gemini 3.x Text Chain",
        "temperature": 0.1,
        "rollback_flag": "AI_PROMPT_PROFILE=v1",
    },
    "group_ai_escalation_draft": {
        "module": "app/services/group_ai_admin.py",
        "function": "build_draft_prompt",
        "model_family": "Gemini 3.x Text Chain",
        "temperature": 0.25,
        "rollback_flag": "AI_PROMPT_PROFILE=v1",
    },
    "voice_note_asr": {
        "module": "app/services/group_ai_admin.py",
        "function": "transcribe_voice_gemini",
        "model_family": "Gemini 3.x Multimodal Chain",
        "temperature": 0.1,
        "rollback_flag": "AI_PROMPT_PROFILE=v1",
    },
    "private_ai_reply": {
        "module": "app/services/private_ai.py",
        "function": "build_private_prompt",
        "model_family": "Gemini 3.x Text Chain",
        "temperature": 0.3,
        "rollback_flag": "AI_PROMPT_PROFILE=v1",
    },
    "recap_bible": {
        "module": "app/services/recap_engine/recap_planner.py",
        "function": "build_story_context",
        "model_family": "Gemini 3.x JSON Mode",
        "temperature": 0.25,
        "rollback_flag": "AI_PROMPT_PROFILE=v1",
    },
    "recap_writer_5_styles": {
        "module": "app/services/recap_engine/recap_planner.py",
        "function": "translate_batch",
        "model_family": "Gemini 3.x JSON Mode",
        "temperature": 0.3,
        "rollback_flag": "AI_PROMPT_PROFILE=v1",
    },
    "recap_auditor": {
        "module": "app/services/recap_engine/recap_planner.py",
        "function": "audit_and_repair_batch",
        "model_family": "Gemini 3.x JSON Mode",
        "temperature": 0.2,
        "rollback_flag": "AI_PROMPT_PROFILE=v1",
    },
    "recap_ending_credit": {
        "module": "app/services/recap_engine/recap_planner.py",
        "function": "audit_script_ending_credit",
        "model_family": "Deterministic + Structural Audit",
        "rollback_flag": "AI_PROMPT_PROFILE=v1",
    },
    "whisper_asr": {
        "module": "app/services/recap_engine/services/whisper_service.py",
        "function": "_transcribe_groq",
        "model_family": "Groq Whisper Large v3 Turbo",
        "rollback_flag": "AI_PROMPT_PROFILE=v1",
    },
    "gemini_tts": {
        "module": "app/services/recap_engine/gemini_tts_engine.py",
        "function": "narrator_prompt_short / narrator_prompt",
        "model_family": "Gemini 3.8 GA TTS Chain",
        "rollback_flag": "AI_PROMPT_PROFILE=v1",
    },
    "voxcpm2_clone": {
        "module": "app/services/voxcpm_engine.py",
        "function": "synthesize",
        "model_family": "VoxCPM2 Gradio ZeroGPU",
        "rollback_flag": "AI_PROMPT_PROFILE=v1",
    },
    "studio_tts_router": {
        "module": "app/services/tts_service.py",
        "function": "_synthesize_tts_audio_sync",
        "model_family": "Gemini 3.8 GA / Edge TTS / VoxCPM2",
        "rollback_flag": "AI_PROMPT_PROFILE=v1",
    },
}

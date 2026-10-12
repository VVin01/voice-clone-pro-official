from __future__ import annotations

import json
import logging
from pathlib import Path
from app.config import settings

logger = logging.getLogger(__name__)

_LOCALES: dict[str, dict[str, str]] = {}


def load_locales(root: Path | None = None) -> None:
    """Load localization json files into memory."""
    locales_dir = root or settings.locales_root
    _LOCALES.clear()

    if not locales_dir.exists():
        logger.warning("Locales directory %s does not exist", locales_dir)
        return

    for lang_dir in locales_dir.iterdir():
        if lang_dir.is_dir():
            msg_file = lang_dir / "messages.json"
            if msg_file.is_file():
                try:
                    with open(msg_file, "r", encoding="utf-8") as f:
                        _LOCALES[lang_dir.name] = json.load(f)
                except Exception as e:
                    logger.error("Failed to load locale %s: %s", lang_dir.name, e)


def t(key: str, lang: str = "my", **kwargs) -> str:
    """Get localized string by key and language with keyword interpolation."""
    if not _LOCALES:
        load_locales()

    lang_dict = _LOCALES.get(lang) or _LOCALES.get("my") or {}
    msg = lang_dict.get(key)

    if msg is None:
        # Fallback to English
        en_dict = _LOCALES.get("en", {})
        msg = en_dict.get(key, key)

    if kwargs:
        try:
            return msg.format(**kwargs)
        except Exception as e:
            logger.debug("Failed to format string key '%s' with %s: %s", key, kwargs, e)
            return msg

    return msg

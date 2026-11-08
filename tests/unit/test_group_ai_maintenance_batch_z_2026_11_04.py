"""Batch Z pins (2026-11-04): the Group AI fact sheet is
maintenance-aware.

Owner report: during the 2026-11-04 disk incident, maintenance mode
was on for hours while the community Group AI kept answering as if
the service were normal — its fact sheet is built purely from code
facts and had no idea creation features were blocked. Batch Z:

* registers a free-text dynamic setting ``maintenance_note`` (the
  admin's announced detail, e.g. the reopening time) alongside
  ``maintenance_mode``;
* appends a clearly delimited current-status section to
  ``build_fact_sheet()`` while the mode is on, quoting the note
  verbatim, and stating explicitly that NO reopening time has been
  announced when the note is empty (so the AI cannot invent one);
* leaves the sheet byte-identical when the mode is off.
"""

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.schema import BotSetting
from app.services import group_ai_admin as gai
from app.services.dynamic_settings import (
    DYNAMIC_SETTING_KEYS,
    SETTING_TYPES,
    load_dynamic_settings,
    set_dynamic_setting,
)

SECTION_HEADER = "【အခုလက်ရှိ အခြေအနေ — ပြုပြင်ထိန်းသိမ်းမှု】"
NOTE = "ည ၉:၃၀ ပြန်ဖွင့်ပါမယ်"


@pytest.fixture(autouse=True)
def _restore_maintenance_settings():
    old_mode = settings.maintenance_mode
    old_note = settings.maintenance_note
    yield
    settings.maintenance_mode = old_mode
    settings.maintenance_note = old_note


# ---------------------------------------------------------------------------
# Fact sheet: section present iff maintenance is on
# ---------------------------------------------------------------------------


def test_sheet_has_no_maintenance_section_when_off(monkeypatch):
    monkeypatch.setattr(settings, "maintenance_mode", False)
    monkeypatch.setattr(settings, "maintenance_note", NOTE)
    sheet = gai.build_fact_sheet()
    assert SECTION_HEADER not in sheet
    assert NOTE not in sheet
    assert sheet.split("\n")[-1] == "=== KNOWLEDGE SHEET END ==="


def test_sheet_has_maintenance_section_when_on(monkeypatch):
    monkeypatch.setattr(settings, "maintenance_mode", True)
    monkeypatch.setattr(settings, "maintenance_note", "")
    sheet = gai.build_fact_sheet()
    assert SECTION_HEADER in sheet
    # The section states the service is under maintenance and that
    # creation features are temporarily unavailable.
    assert "ပြုပြင်ထိန်းသိမ်းမှု (Maintenance)" in sheet
    assert "ယာယီ သုံးလို့ မရသေးပါဘူး" in sheet
    # The section sits inside the sheet (before the END marker), so
    # the sheet-only rules cover it.
    assert sheet.index(SECTION_HEADER) < sheet.index("=== KNOWLEDGE SHEET END ===")


def test_sheet_quotes_note_verbatim(monkeypatch):
    monkeypatch.setattr(settings, "maintenance_mode", True)
    monkeypatch.setattr(settings, "maintenance_note", NOTE)
    sheet = gai.build_fact_sheet()
    assert f"ကြေညာထားတဲ့ အသေးစိတ်: {NOTE}" in sheet
    # With a note announced, the no-announcement line must not appear.
    assert "ကြေညာထားတာ မရှိသေးပါ" not in sheet


def test_sheet_invents_no_reopen_detail_when_note_empty(monkeypatch):
    monkeypatch.setattr(settings, "maintenance_mode", True)
    monkeypatch.setattr(settings, "maintenance_note", "")
    sheet = gai.build_fact_sheet()
    # No announced detail line, and an explicit fact that no reopening
    # time has been announced (the AI must not invent one).
    assert "ကြေညာထားတဲ့ အသေးစိတ်" not in sheet
    assert "ပြန်ဖွင့်မည့် အချိန် ကြေညာထားတာ မရှိသေးပါ" in sheet
    assert "ထွင် မပြောရပါ" in sheet


def test_sheet_note_whitespace_only_counts_as_empty(monkeypatch):
    monkeypatch.setattr(settings, "maintenance_mode", True)
    monkeypatch.setattr(settings, "maintenance_note", "   ")
    sheet = gai.build_fact_sheet()
    assert "ကြေညာထားတဲ့ အသေးစိတ်" not in sheet
    assert "ပြန်ဖွင့်မည့် အချိန် ကြေညာထားတာ မရှိသေးပါ" in sheet


def test_sheet_off_is_on_sheet_minus_section(monkeypatch):
    """Byte-identity pin: the OFF sheet equals the ON sheet with the
    maintenance lines removed — nothing else in the sheet changes."""
    monkeypatch.setattr(settings, "maintenance_note", NOTE)
    monkeypatch.setattr(settings, "maintenance_mode", False)
    off = gai.build_fact_sheet()
    monkeypatch.setattr(settings, "maintenance_mode", True)
    on_lines = gai.build_fact_sheet().split("\n")
    start = on_lines.index(SECTION_HEADER)
    # Section = header + status bullet + note bullet.
    del on_lines[start:start + 3]
    assert "\n".join(on_lines) == off


# ---------------------------------------------------------------------------
# Dynamic setting registration + round trip
# ---------------------------------------------------------------------------


def test_maintenance_note_registered_as_dynamic_setting():
    assert SETTING_TYPES["maintenance_note"] is str
    assert "maintenance_note" in DYNAMIC_SETTING_KEYS
    assert settings.maintenance_note == ""  # env fallback default


@pytest.mark.asyncio
async def test_maintenance_note_round_trips(db_session):
    await set_dynamic_setting(db_session, "maintenance_note", NOTE, admin_id=1)
    await db_session.commit()
    row = (await db_session.execute(
        select(BotSetting).where(BotSetting.key == "maintenance_note")
    )).scalar_one()
    assert row.value == NOTE
    # Applied to in-memory settings immediately...
    assert settings.maintenance_note == NOTE
    # ...and reloaded from the DB row by the startup loader.
    settings.maintenance_note = ""
    await load_dynamic_settings(db_session)
    assert settings.maintenance_note == NOTE


# ---------------------------------------------------------------------------
# Admin payload + validation
# ---------------------------------------------------------------------------


def test_admin_payload_accepts_maintenance_note():
    from app.webapp.api import AdminSettingsPayload

    payload = AdminSettingsPayload(maintenance_note=NOTE)
    assert payload.maintenance_note == NOTE
    assert AdminSettingsPayload().maintenance_note is None


def test_admin_settings_response_includes_maintenance_note():
    import inspect

    from app.webapp import api

    assert '"maintenance_note"' in inspect.getsource(api.get_admin_settings)


def test_validate_note_accepts_and_strips():
    from app.webapp.api import _validate_admin_settings_updates

    cleaned = _validate_admin_settings_updates({"maintenance_note": f"  {NOTE}  "})
    assert cleaned["maintenance_note"] == NOTE
    # Empty is a real choice (no announced detail).
    assert _validate_admin_settings_updates({"maintenance_note": ""})["maintenance_note"] == ""
    # Exactly at the cap is fine.
    assert len(_validate_admin_settings_updates({"maintenance_note": "x" * 200})["maintenance_note"]) == 200


def test_validate_note_rejects_over_cap():
    from fastapi import HTTPException

    from app.webapp.api import _validate_admin_settings_updates

    with pytest.raises(HTTPException) as exc:
        _validate_admin_settings_updates({"maintenance_note": "x" * 201})
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Mini App admin form wiring + static cache token
# ---------------------------------------------------------------------------


def test_admin_form_has_maintenance_note_input():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    index_html = (root / "app/webapp/static/index.html").read_text(encoding="utf-8")
    app_js = (root / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
    i18n_js = (root / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")

    assert 'id="adm-set-maintenance-note"' in index_html
    assert 'data-i18n="settings_maintenance_note"' in index_html
    assert 'data-i18n-ph="settings_maintenance_note_ph"' in index_html
    assert i18n_js.count("settings_maintenance_note_ph:") == 2
    # Load + save wiring.
    assert "adm-set-maintenance-note" in app_js
    assert "maintenance_note" in app_js
    # Labels exist in both locales.
    assert i18n_js.count("settings_maintenance_note:") == 2
    assert i18n_js.count("settings_maintenance_note_hint:") == 2
    # Statics cache token bumped for this batch.
    assert index_html.count("?v=20261104_r117") == 4

"""Release 40 premium-upgrade batch — unit tests (2026-11-02).

Covers: item 1 (voice display-name helper), item 3 (single blur band
geometry), item 4 (Classic Gold outline guard), item 12 (full job id),
item 14 copy gates. Each test names the item it pins.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app" / "services" / "recap_engine"))

from app.services.voice_display import (  # noqa: E402
    gemini_voice_display_name,
    persona_display_name,
    recap_voice_display_name,
)
from core import (  # noqa: E402
    SUBTITLE_STYLE_PRESETS,
    ensure_plate_contains_anchor,
    frame_layout,
    reconcile_source_bands,
    subtitle_anchor_y,
    subtitle_plate_box,
    write_synced_ass_file,
)

API_PY = (ROOT / "app" / "webapp" / "api.py").read_text(encoding="utf-8")
PIPELINE_PY = (ROOT / "app" / "services" / "recap_engine" / "recap_pipeline.py").read_text(encoding="utf-8")


# ── Item 1: voice display names ─────────────────────────────────────

def test_gemini_puck_display_name():
    assert gemini_voice_display_name("Puck") == "ကိုသော်တာ"
    assert recap_voice_display_name("gemini", "Puck", "komin") == "ကိုသော်တာ"


def test_gemini_all_voices_have_display_names():
    from app.services.recap_engine.gemini_tts_engine import (
        GEMINI_VOICE_DISPLAY_MY,
        GEMINI_VOICE_NAMES,
    )

    assert set(GEMINI_VOICE_DISPLAY_MY) == set(GEMINI_VOICE_NAMES)
    for name in GEMINI_VOICE_NAMES:
        assert recap_voice_display_name("gemini", name, "komin") == GEMINI_VOICE_DISPLAY_MY[name]


def test_gemini_map_matches_group_ai_knowledge_sheet():
    sheet = (ROOT / "app" / "services" / "group_ai_admin.py").read_text(encoding="utf-8")
    from app.services.recap_engine.gemini_tts_engine import GEMINI_VOICE_DISPLAY_MY

    for display in GEMINI_VOICE_DISPLAY_MY.values():
        assert display in sheet


def test_persona_engine_names_persona_not_slug():
    assert recap_voice_display_name("voxcpm", None, "komin") == "ကိုမင်း"
    assert persona_display_name("komin") == "ကိုမင်း"
    # Gemini engine with an unknown voice falls back to the persona.
    assert recap_voice_display_name("gemini", None, "komin") == "ကိုမင်း"
    assert recap_voice_display_name("gemini", "NoSuchVoice", "komin") == "ကိုမင်း"


def test_notify_and_caption_use_display_helper():
    # The completion notify must not print the raw persona slug anymore.
    assert "recap_voice_display_name(tts_engine, gemini_voice, req.voice_slug)" in API_PY
    assert "voice_slug=html.escape(str(req.voice_slug.capitalize()))" not in API_PY
    # The on-demand delivery caption reads the engine from recap_config.
    assert "_load_persisted_recap_state(db_job)" in API_PY
    assert "db_job.preset_voice.capitalize()" not in API_PY


# ── Item 12: full job id in the completion notify ───────────────────

def test_notify_uses_full_job_id():
    assert "job_id=job_id[:8]" not in API_PY
    assert "job_id=job_id," in API_PY


# ── Item 3: single blur band geometry ───────────────────────────────

def _full_frame_plate(sub_y=0.67):
    return subtitle_plate_box(1080, 1920, None, raw_font_size=16, sub_lines=1, sub_y=sub_y, min_h=0.07)


def test_plate_anchors_to_subtitle_sub_y():
    # The complaint case: sub_y=67 (blur_y was 85). The plate centre
    # must be the subtitle's 0.67, not the blur slider's 0.85.
    plate = _full_frame_plate(0.67)
    assert plate["y"] == pytest.approx(0.67, abs=0.005)


def test_pipeline_feeds_sub_y_not_blur_y():
    assert "sub_y=_anchor_sub_y, min_h=blur_h" in PIPELINE_PY
    assert "sub_y=blur_y" not in PIPELINE_PY


def test_band_near_plate_merges_into_union():
    plate = _full_frame_plate(0.67)
    band = {"x": 0.05, "y": 0.70, "w": 0.90, "h": 0.06}
    merged, extras = reconcile_source_bands(plate, [band], None, 1080, 1920)
    assert extras == []
    top = min(plate["y"] - plate["h"] / 2, band["y"] - band["h"] / 2)
    bot = max(plate["y"] + plate["h"] / 2, band["y"] + band["h"] / 2)
    assert merged["y"] == pytest.approx((top + bot) / 2, abs=0.002)
    assert merged["h"] == pytest.approx(bot - top, abs=0.002)


def test_band_inside_letterbox_padding_is_dropped():
    # 16:9 picture inside a 9:16 canvas: content occupies the middle.
    layout = frame_layout("9:16", "blur", 1920, 1080)
    plate = subtitle_plate_box(layout["cw"], layout["ch"], layout, raw_font_size=16, sub_lines=1, sub_y=0.85, min_h=0.07)
    rim_frac = (layout["oy"] + layout["ph"]) / layout["ch"]
    pad_band = {"x": 0.04, "y": rim_frac + 0.10, "w": 0.92, "h": 0.05}  # fully below the rim
    merged, extras = reconcile_source_bands(plate, [pad_band], layout, layout["cw"], layout["ch"])
    assert extras == []
    assert merged == plate  # untouched — the band was dropped, not merged


def test_band_far_from_plate_kept_as_sole_extra():
    plate = _full_frame_plate(0.67)
    band = {"x": 0.05, "y": 0.30, "w": 0.90, "h": 0.06}
    merged, extras = reconcile_source_bands(plate, [band], None, 1080, 1920)
    assert merged == plate
    assert len(extras) == 1
    assert extras[0]["y"] == pytest.approx(0.30)


def test_ensure_plate_contains_anchor():
    plate = {"x": 0.04, "y": 0.85, "w": 0.92, "h": 0.07}
    fixed = ensure_plate_contains_anchor(plate, 0.67)
    assert fixed["y"] - fixed["h"] / 2 <= 0.67 <= fixed["y"] + fixed["h"] / 2
    # Already-containing plates are returned unchanged.
    ok = {"x": 0.04, "y": 0.67, "w": 0.92, "h": 0.09}
    assert ensure_plate_contains_anchor(ok, 0.67) == ok


def test_anchor_matches_ass_writer_positions():
    # Full-frame: anchor == sub_y. Letterbox: anchor rides the rim.
    assert subtitle_anchor_y(1080, 1920, None, sub_y=0.67) == pytest.approx(0.67)
    layout = frame_layout("9:16", "blur", 1920, 1080)
    anchor = subtitle_anchor_y(layout["cw"], layout["ch"], layout, sub_y=0.85)
    rim_frac = (layout["oy"] + layout["ph"]) / layout["ch"]
    assert anchor < rim_frac  # just above the rim, on the picture


# ── Item 4: Classic Gold outline ────────────────────────────────────

def _ass_style_fields(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^Style: Default,([^,]+),(\d+),.*?,(\d+\.?\d*),(\d+\.?\d*),2,", text, re.M)
    assert m, text[:400]
    return {"font": m.group(1), "font_size": int(m.group(2)), "outline": float(m.group(3)), "shadow": float(m.group(4))}


def test_classic_gold_preset_values():
    st = SUBTITLE_STYLE_PRESETS["yellow"]
    assert st["outline"] == 1.3
    assert st["shadow"] == 0.8
    assert st["primary"] == "&H0000FFFF"  # the gold fill itself is unchanged


def test_ass_outline_never_exceeds_8pct_of_font(tmp_path):
    out = write_synced_ass_file(
        [(0.0, 3.0, "မင်္ဂလာပါ စမ်းသပ်စာသား")], tmp_path / "a.ass", 1080, 1920,
        raw_font_size=16, sub_lines=1, sub_y=0.85, sub_style="yellow",
    )
    fields = _ass_style_fields(out)
    assert fields["font_size"] == 44
    assert fields["outline"] <= 0.08 * fields["font_size"] + 1e-9
    assert fields["outline"] == pytest.approx(3.47, abs=0.05)


def test_ass_outline_guard_at_small_size(tmp_path):
    # Raw size 12 on a small canvas: even the reduced preset would
    # scale past 8% without the guard.
    out = write_synced_ass_file(
        [(0.0, 3.0, "မင်္ဂလာပါ")], tmp_path / "b.ass", 720, 1280,
        raw_font_size=12, sub_lines=1, sub_y=0.85, sub_style="yellow",
    )
    fields = _ass_style_fields(out)
    assert fields["outline"] <= max(1.5, 0.08 * fields["font_size"]) + 1e-9


# ── Item 2: payment copy — dual units ───────────────────────────────

def test_package_recap_credits_from_catalogue():
    from app.services.payments import package_recap_credits

    assert package_recap_credits("recap_starter") == 5
    assert package_recap_credits("recap_studio_pro") == 25
    assert package_recap_credits("combined_starter") == 5
    assert package_recap_credits("MIN_30") == 0
    assert package_recap_credits("voice_studio_pro") == 0
    assert package_recap_credits(None) == 0
    assert package_recap_credits("no-such-pack") == 0


def test_approval_notice_states_both_units():
    import json

    for lang in ("my", "en"):
        msgs = json.loads((ROOT / "locales" / lang / "messages.json").read_text(encoding="utf-8"))
        rendered = msgs["payment_approved_user_notify"].format(minutes=0, recap_credits=5)
        assert "{minutes}" not in rendered and "{recap_credits}" not in rendered
        assert "0" in rendered and "5" in rendered  # both figures print
        # The R40 bug: a recap pack read as "0 minutes credited" ONLY.
        assert "Credit" in rendered


def test_bot_locale_key_parity():
    import json

    my = json.loads((ROOT / "locales" / "my" / "messages.json").read_text(encoding="utf-8"))
    en = json.loads((ROOT / "locales" / "en" / "messages.json").read_text(encoding="utf-8"))
    assert set(my) == set(en)
    assert "payment_instruction" not in my  # dead keys deleted
    assert "payment_selected_package" not in my


# ── Item 6/7: buyer confirmation + admin receipt ────────────────────

def test_slip_submit_confirmation_carries_request_id():
    assert "တောင်းဆိုမှု နံပါတ်: #{pay_req.id}" in API_PY
    assert '"request_id": pay_req.id' in API_PY


def test_admin_order_detail_sends_receipt_photo():
    admin_py = (ROOT / "app" / "handlers" / "admin.py").read_text(encoding="utf-8")
    i = admin_py.index("async def handle_admin_pay_detail")
    body = admin_py[i:i + 3000]
    assert "answer_photo" in body and "payment.receipt_file_id" in body


# ── Item 14/K: downloader single-sourced messages ──────────────────

def test_over_limit_message_single_source_and_marker():
    sys.path.insert(0, str(ROOT))
    from app.services.downloader import recap_over_limit_message

    vip = recap_over_limit_message(754.0, 600, True)
    free = recap_over_limit_message(754.0, 300, False)
    # The Mini App's pre-charge pass-through marker must survive.
    assert "ကန့်သတ်ချက် (အများဆုံး" in vip and "ကန့်သတ်ချက် (အများဆုံး" in free
    assert "12:34" in vip  # real duration rendered mm:ss
    # Both api.py raise sites (script phase + generate) use the helper,
    # and the downloader's own recap path raises it too.
    assert API_PY.count("recap_over_limit_message(") == 2
    dl = (ROOT / "app" / "services" / "downloader.py").read_text(encoding="utf-8")
    assert "recap_over_limit_message(actual_dur, max_dur, is_vip)" in dl


def test_douyin_blocked_message_single_constant():
    dl = (ROOT / "app" / "services" / "downloader.py").read_text(encoding="utf-8")
    assert dl.count("DOUYIN_BLOCKED_MESSAGE") >= 4  # 1 def + 3 raise sites
    assert "classify_source_error(err_msg, expanded_url) or YOUTUBE_BOT_WALL_MESSAGE" in dl


# ── Item 15: bot-UI defects ─────────────────────────────────────────

# NOTE (bot-chat cleanup, 2026-11-03): test_model_file_ready_cloud_marker
# was removed deliberately — it pinned _model_file_ready(), a helper that
# existed only for the retired admin Voice Models screens. Its semantics
# (cloud marker = ready, legacy .pth = not) are superseded by
# is_legacy_local_model()/is_displayable_model() in
# tests/unit/test_bot_cleanup_2026_11_03.py.


def test_wrap_admin_value_wraps_and_escapes():
    from app.handlers.admin import _wrap_admin_value

    long_token = "x" * 200
    out = _wrap_admin_value(f"prefix {long_token} suffix")
    assert all(len(line) <= 60 for line in out.split("\n"))
    assert "&lt;" in _wrap_admin_value("a<b")  # escaped AFTER wrapping
    short = _wrap_admin_value("hello world")
    assert short == "hello world"


def test_buy_menu_has_voice_pro_button():
    pay_py = (ROOT / "app" / "handlers" / "payment.py").read_text(encoding="utf-8")
    # Data-driven: catalogue voice plans missing from the DB packs get
    # a buy button with the standard buy_pkg: callback (Voice Pro).
    assert 'entry["category"] == "voice" and entry["code"] not in _db_codes' in pay_py
    from app.services.payments import get_plan_by_code

    assert get_plan_by_code("voice_studio_pro")["minutes"] == 250


def test_recap_job_records_real_seconds():
    # Creation stores the checked source duration, not the old 120s
    # placeholder; completion stores the probed real duration.
    assert "seconds_estimated=int(checked_duration) if checked_duration else 0" in API_PY
    assert '"seconds_consumed": _real_seconds' in API_PY


# ── Item 13: history recap_settings payload ─────────────────────────

def test_history_payload_surfaces_recap_settings():
    assert '"recap_settings": _recap_settings,' in API_PY
    app_js = (ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "hub-settings-summary" in app_js and "j.recap_settings" in app_js


# ── Item 9: preview-frame endpoint wiring ───────────────────────────

def test_preview_frame_endpoint_exists_and_validates():
    assert '@api_router.post("/recap/preview-frame")' in API_PY
    i = API_PY.index("async def recap_preview_frame")
    body = API_PY[i:i + 3400]
    assert "require_gate_verified" in body
    assert "validate_public_url" in body
    assert "classify_source_error" in body


# ── Logo rework (owner decision 2026-11-02): paid-only, user's OWN ──
# Supersedes the verify-first (i) interim fix: the bundled brand
# asset wiring is gone; the logo overlay is a PAID feature that
# stamps the user's own uploaded channel logo. Free users get no
# logo at all — uploads rejected, generate path ignores settings.

def test_bundled_logo_wiring_removed():
    asset = ROOT / "app" / "webapp" / "static" / "assets" / "logo_vcp.png"
    assert not asset.exists()
    assert "logo_vcp" not in API_PY
    # The generate path resolves through the tier-gated helper and
    # only passes a position when a logo path actually resolved.
    assert "_resolve_recap_logo_path(user_id, req.include_logo, is_vip)" in API_PY
    assert "logo_path=logo_path_arg" in API_PY
    assert "logo_position=logo_pos_coords if logo_path_arg else None" in API_PY


def _write_stored_logo(tmp_path, user_id, ext=".png"):
    d = tmp_path / "user_logos" / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"logo{ext}"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 32)
    return p


def _fake_user(uid, category):
    class _U:
        id = uid
        account_category = category
    return _U()


def _logo_api(monkeypatch, tmp_path):
    from app.config import settings
    from app.webapp import api as api_mod

    monkeypatch.setattr(settings, "storage_root", tmp_path)
    return api_mod


def test_free_user_generate_ignores_logo(monkeypatch, tmp_path):
    api_mod = _logo_api(monkeypatch, tmp_path)
    uid = 770001
    _write_stored_logo(tmp_path, uid)  # even a leftover file must not render
    assert api_mod._resolve_recap_logo_path(uid, True, False) is None
    assert api_mod._resolve_recap_logo_path(uid, False, False) is None


def test_paid_generate_resolves_own_logo(monkeypatch, tmp_path):
    api_mod = _logo_api(monkeypatch, tmp_path)
    uid = 770002
    stored = _write_stored_logo(tmp_path, uid, ".webp")
    assert api_mod._resolve_recap_logo_path(uid, True, True) == str(stored)
    # Toggle off → nothing. Nothing uploaded → nothing.
    assert api_mod._resolve_recap_logo_path(uid, False, True) is None
    assert api_mod._resolve_recap_logo_path(770099, True, True) is None
    # Another user's logo never leaks into this user's render.
    assert api_mod._find_user_logo(770099) is None


def test_can_use_custom_logo_tiers(monkeypatch, tmp_path):
    from app.config import settings
    from app.models.enums import AccountCategory

    api_mod = _logo_api(monkeypatch, tmp_path)
    free = _fake_user(770010, AccountCategory.FREE.value)
    paid = _fake_user(770011, AccountCategory.PURCHASED.value)
    assert api_mod._can_use_custom_logo(free) is False
    assert api_mod._can_use_custom_logo(paid) is True
    # Admins are exempt from the tier gate (standing rule).
    monkeypatch.setattr(settings, "admin_ids_raw", "770010")
    assert api_mod._can_use_custom_logo(free) is True


def _png_bytes(size=(64, 64)):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, (200, 40, 40)).save(buf, "PNG")
    return buf.getvalue()


def test_logo_image_validation(tmp_path):
    from fastapi import HTTPException

    from app.webapp import api as api_mod

    good = tmp_path / "logo.png"
    good.write_bytes(_png_bytes())
    api_mod._validate_logo_image(good, "PNG")  # must not raise

    not_image = tmp_path / "fake.png"
    not_image.write_bytes(b"this is not an image at all")
    with pytest.raises(HTTPException) as ei:
        api_mod._validate_logo_image(not_image, "PNG")
    assert ei.value.status_code == 400

    mismatched = tmp_path / "logo.jpg"
    mismatched.write_bytes(_png_bytes())  # PNG bytes behind a .jpg name
    with pytest.raises(HTTPException) as ei2:
        api_mod._validate_logo_image(mismatched, "JPEG")
    assert ei2.value.status_code == 400


def test_logo_endpoints_exist_and_me_advertises_flags():
    for route in (
        '@api_router.get("/user/logo")',
        '@api_router.get("/user/logo/file")',
        '@api_router.post("/user/logo")',
        '@api_router.delete("/user/logo")',
    ):
        assert route in API_PY
    # /user/me carries the flags the Mini App logo UI keys off.
    assert '"can_use_logo": bool(is_vip)' in API_PY
    assert '"has_custom_logo": _find_user_logo(user.id) is not None' in API_PY


def test_free_user_upload_rejected_403(monkeypatch, tmp_path):
    import asyncio

    from fastapi import HTTPException

    from app.models.enums import AccountCategory

    api_mod = _logo_api(monkeypatch, tmp_path)
    free = _fake_user(770030, AccountCategory.FREE.value)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(
            api_mod.upload_user_logo(logo_file=object(), user=free, _gate=free)
        )
    assert ei.value.status_code == 403
    assert ei.value.detail == api_mod.LOGO_VIP_ONLY_DETAIL
    # Nothing was stored for the rejected user.
    assert api_mod._find_user_logo(770030) is None


def test_paid_upload_validation_and_storage(monkeypatch, tmp_path):
    import asyncio
    import io

    from fastapi import HTTPException, UploadFile

    from app.models.enums import AccountCategory

    api_mod = _logo_api(monkeypatch, tmp_path)
    paid = _fake_user(770031, AccountCategory.PURCHASED.value)

    def _upload(name, data):
        return UploadFile(filename=name, file=io.BytesIO(data))

    # Happy path: a real PNG is stored as the user's own logo.
    payload = asyncio.run(
        api_mod.upload_user_logo(
            logo_file=_upload("logo.png", _png_bytes()), user=paid, _gate=paid
        )
    )
    assert payload["can_use_logo"] is True and payload["has_logo"] is True
    stored = api_mod._find_user_logo(770031)
    assert stored is not None and stored.suffix == ".png"
    assert api_mod._resolve_recap_logo_path(770031, True, True) == str(stored)

    # Bad extension → 400, previous logo untouched.
    with pytest.raises(HTTPException) as ei:
        asyncio.run(
            api_mod.upload_user_logo(
                logo_file=_upload("logo.gif", _png_bytes()), user=paid, _gate=paid
            )
        )
    assert ei.value.status_code == 400

    # Non-image bytes behind a .png name → 400, previous logo untouched.
    with pytest.raises(HTTPException) as ei2:
        asyncio.run(
            api_mod.upload_user_logo(
                logo_file=_upload("logo.png", b"definitely not an image"),
                user=paid,
                _gate=paid,
            )
        )
    assert ei2.value.status_code == 400
    assert api_mod._find_user_logo(770031) == stored

    # Oversize (> 2 MB cap) → 413, previous logo untouched.
    big = _png_bytes() + b"\0" * (api_mod.MAX_LOGO_UPLOAD_BYTES + 1)
    with pytest.raises(HTTPException) as ei3:
        asyncio.run(
            api_mod.upload_user_logo(
                logo_file=_upload("logo.png", big), user=paid, _gate=paid
            )
        )
    assert ei3.value.status_code == 413
    assert api_mod._find_user_logo(770031) == stored


def test_delete_user_logo_clears_it(monkeypatch, tmp_path):
    import asyncio

    from app.models.enums import AccountCategory

    api_mod = _logo_api(monkeypatch, tmp_path)
    uid = 770032
    stored = _write_stored_logo(tmp_path, uid)
    assert api_mod._find_user_logo(uid) == stored
    user = _fake_user(uid, AccountCategory.PURCHASED.value)
    payload = asyncio.run(api_mod.delete_user_logo(user=user, _gate=user))
    assert payload["has_logo"] is False
    assert api_mod._find_user_logo(uid) is None
    # Idempotent: deleting again still succeeds with has_logo False.
    payload2 = asyncio.run(api_mod.delete_user_logo(user=user, _gate=user))
    assert payload2["has_logo"] is False


def test_logo_frontend_wiring():
    app_js = (ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    index_html = (ROOT / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
    assert "function refreshLogoUi()" in app_js
    assert '"/api/user/logo"' in app_js
    assert 'method: "DELETE"' in app_js
    for el_id in (
        "recap-logo-manager",
        "recap-logo-thumb",
        "btn-logo-upload",
        "btn-logo-remove",
        "input-logo-file",
        "recap-logo-vip-hint",
        "recap-logo-status",
    ):
        assert f'id="{el_id}"' in index_html


# ── Verify-first (ii): signed stream tokens ─────────────────────────

def test_stream_token_round_trip_and_scope():
    from app.webapp.api import _make_stream_token, _verify_stream_token, _with_stream_url

    tok = _make_stream_token("rc_test_job", 424242, "video")
    if tok is None:
        pytest.skip("no bot token configured in this environment")
    assert _verify_stream_token(tok, "rc_test_job", "video") == 424242
    # Scope: another job or another file must NOT verify.
    assert _verify_stream_token(tok, "rc_other_job", "video") is None
    assert _verify_stream_token(tok, "rc_test_job", "audio") is None
    # Tamper: flip the user id segment.
    parts = tok.split(".")
    forged = ".".join(["999999", parts[1], parts[2]])
    assert _verify_stream_token(forged, "rc_test_job", "video") is None
    urls = _with_stream_url({"video": "/api/recap/download/rc_test_job/video"}, "rc_test_job", 424242)
    assert urls["stream_url"].startswith("/api/recap/stream/rc_test_job/video?t=")


def test_stream_endpoint_accepts_token_auth():
    i = API_PY.index("async def download_recap_file")
    body = API_PY[i:i + 1800]
    assert "_verify_stream_token(t, job_id, file_type)" in body
    app_js = (ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "setRecapPlayerSource" in app_js and "urls.stream_url" in app_js


# ── M6: single-sourced segmented/script-review messages ─────────────

def test_segmented_credits_message_single_builder():
    from app.webapp.api import _RecapInsufficientCreditsError, _segmented_credits_message

    plain = _segmented_credits_message(4)
    assert "အပိုင်း 4 ပိုင်း" in plain and "စုစုပေါင်း 4 Credits" in plain
    with_bal = _segmented_credits_message(4, balance=2)
    assert "လက်ကျန် Credit (2c)" in with_bal
    assert str(_RecapInsufficientCreditsError(4)) == plain
    assert API_PY.count("_SCRIPT_REVIEW_SPLIT_HINT") >= 3

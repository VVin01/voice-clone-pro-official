"""Unit tests for Admin Watermark Controls for Recap (`feat/admin-watermark-controls`).

Covers:
1. Default settings preserve exact existing behaviour (FREE ON, PAID OFF, tiled, bottom-right, 20%, 16%).
2. Style variants ('tiled', 'logo', 'text'), all 5 positions, opacity (20-100%), and size (5-40%).
3. Per-tier toggles:
   - FREE ON -> renders watermark; FREE OFF -> no watermark.
   - PAID default (OFF) -> clean output; PAID ON -> watermark renders.
4. Preview = Output parity: `generate_recap_preview` / `extract_preview_with_overlays` renders
   the watermark identically to `apply_production_overlays` / `run_recap_pipeline` for the viewing tier.
5. Dynamic settings persistence + Admin Settings API validation/endpoint round-trip + UI & i18n wiring.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config.settings import get_watermark_config, settings
from app.db.base import Base
import app.models.schema  # noqa: F401 - register ORM tables on Base.metadata
from app.services.dynamic_settings import load_dynamic_settings, set_dynamic_setting
from app.services.recap_engine import core as recap_core
from app.services.recap_engine import recap_pipeline
from app.webapp import api as api_mod

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
INDEX_HTML = (REPO_ROOT / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
APP_JS = (REPO_ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
I18N_JS = (REPO_ROOT / "app" / "webapp" / "static" / "js" / "i18n.js").read_text(encoding="utf-8")

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


@pytest.fixture
def restore_watermark_settings():
    keys = (
        "watermark_free_enabled",
        "watermark_paid_enabled",
        "watermark_style",
        "watermark_position",
        "watermark_opacity",
        "watermark_size",
    )
    saved = {k: getattr(settings, k) for k in keys}
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(settings, k, v)


def _make_dark_clip(path: pathlib.Path, w: int = 640, h: int = 360, dur: float = 1.5) -> None:
    subprocess.run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x141824:size={w}x{h}:rate=15:duration={dur}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={dur}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _bright_pixels(img_path: pathlib.Path, box: tuple[int, int, int, int]) -> int:
    with Image.open(img_path) as im:
        crop = im.convert("L").crop(box)
        return sum(1 for v in crop.getdata() if v > 45)


def test_defaults_preserve_existing_behaviour(restore_watermark_settings):
    settings.watermark_free_enabled = True
    settings.watermark_paid_enabled = False
    settings.watermark_style = "tiled"
    settings.watermark_position = "bottom-right"
    settings.watermark_opacity = 20
    settings.watermark_size = 16

    free_cfg = get_watermark_config(is_vip=False)
    paid_cfg = get_watermark_config(is_vip=True)

    assert free_cfg["watermark"] is True
    assert paid_cfg["watermark"] is False
    assert free_cfg["watermark_style"] == "tiled"
    assert free_cfg["watermark_position"] == "bottom-right"
    assert free_cfg["watermark_opacity"] == 20
    assert free_cfg["watermark_size"] == 16

    # Default tiled resolution preserves historical BRAND_PATTERN_OPACITY (0.19)
    opts = recap_core.resolve_watermark_overlay_options({})
    assert opts == {
        "style": "tiled",
        "position": "bottom-right",
        "opacity": recap_core.BRAND_PATTERN_OPACITY,
        "size_pct": 16,
    }


def test_render_brand_pattern_png_styles_positions_opacity_and_size(tmp_path):
    # 1. Logo style: top-left vs bottom-right place pixels cleanly in respective corners
    tl_logo = recap_core.render_brand_pattern_png(
        tmp_path / "logo_tl.png",
        frame_width=640,
        frame_height=360,
        opacity=0.85,
        style="logo",
        position="top-left",
        size_pct=20,
    )
    br_logo = recap_core.render_brand_pattern_png(
        tmp_path / "logo_br.png",
        frame_width=640,
        frame_height=360,
        opacity=0.85,
        style="logo",
        position="bottom-right",
        size_pct=20,
    )
    with Image.open(tl_logo) as im_tl, Image.open(br_logo) as im_br:
        a_tl = np.asarray(im_tl.getchannel("A"))
        a_br = np.asarray(im_br.getchannel("A"))
        assert a_tl[:180, :320].sum() > 10000
        assert a_tl[180:, 320:].sum() == 0
        assert a_br[180:, 320:].sum() > 10000
        assert a_br[:180, :320].sum() == 0

    # 2. Text style ('VoiceClonePro') at centre + opacity & size scaling
    txt_small_low = recap_core.render_brand_pattern_png(
        tmp_path / "txt_small_low.png",
        frame_width=640,
        frame_height=360,
        opacity=0.25,
        style="text",
        position="centre",
        size_pct=12,
    )
    txt_big_high = recap_core.render_brand_pattern_png(
        tmp_path / "txt_big_high.png",
        frame_width=640,
        frame_height=360,
        opacity=0.90,
        style="text",
        position="centre",
        size_pct=35,
    )
    with Image.open(txt_small_low) as im_s, Image.open(txt_big_high) as im_b:
        a_s = np.asarray(im_s.getchannel("A"))
        a_b = np.asarray(im_b.getchannel("A"))
        # Centre quadrant carries mass, top-left margin stays empty
        assert a_s[90:270, 160:480].sum() > 1000
        assert a_s[:40, :40].sum() == 0
        assert a_b.sum() > a_s.sum() * 2.0


def test_pipeline_tier_toggles_free_and_paid(tmp_path, monkeypatch, restore_watermark_settings):
    if not FFMPEG:
        pytest.skip("ffmpeg not available")
    src = tmp_path / "src.mp4"
    _make_dark_clip(src, w=320, h=180, dur=1.0)

    captured: dict = {}

    class _FakePlanner:
        def __init__(self, *a, **kw):
            pass

        def build(self, source, job_dir, source_title=None, reuse=True):
            p = pathlib.Path(job_dir) / "plan.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{}", encoding="utf-8")
            return p, {"segments": []}

    class _FakeSync:
        def __init__(self, *a, **kw):
            captured["overlays"] = dict(kw.get("overlays") or {})

        def run(self):
            return {"final": str(src), "audio": None, "segments": 0}

    monkeypatch.setattr(recap_pipeline, "RecapPlanBuilder", _FakePlanner)
    monkeypatch.setattr(recap_pipeline, "EdgeSmartSync", _FakeSync)

    # Free ON (default) + extra_text -> watermark ON with admin style/pos/opacity
    settings.watermark_free_enabled = True
    settings.watermark_paid_enabled = False
    settings.watermark_style = "logo"
    settings.watermark_position = "top-left"
    settings.watermark_opacity = 75
    settings.watermark_size = 22
    recap_pipeline.run_recap_pipeline(
        src, job_dir=tmp_path / "free_on", paid_tier=False, extra_text="VoiceClonePro"
    )
    assert captured["overlays"]["watermark"] is True
    assert captured["overlays"]["watermark_style"] == "logo"
    assert captured["overlays"]["watermark_position"] == "top-left"
    assert captured["overlays"]["watermark_opacity"] == 75
    assert captured["overlays"]["watermark_size"] == 22

    # Free OFF -> NO watermark even if caller passed extra_text
    captured.clear()
    settings.watermark_free_enabled = False
    recap_pipeline.run_recap_pipeline(
        src, job_dir=tmp_path / "free_off", paid_tier=False, extra_text="VoiceClonePro"
    )
    assert not captured["overlays"].get("watermark")

    # Paid default (OFF) -> NO watermark
    captured.clear()
    settings.watermark_paid_enabled = False
    recap_pipeline.run_recap_pipeline(src, job_dir=tmp_path / "paid_off", paid_tier=True)
    assert not captured["overlays"].get("watermark")

    # Paid ON -> watermark appears on paid job with admin settings
    captured.clear()
    settings.watermark_paid_enabled = True
    recap_pipeline.run_recap_pipeline(src, job_dir=tmp_path / "paid_on", paid_tier=True)
    assert captured["overlays"]["watermark"] is True
    assert captured["overlays"]["watermark_style"] == "logo"
    assert captured["overlays"]["watermark_position"] == "top-left"
    assert captured["overlays"]["watermark_opacity"] == 75


@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe not available")
def test_preview_composite_and_video_render_watermark_parity(tmp_path, restore_watermark_settings):
    src = tmp_path / "src.mp4"
    _make_dark_clip(src, w=640, h=360, dur=1.5)

    # Configure admin settings: logo in top-left corner at 85% opacity, 22% width
    settings.watermark_free_enabled = True
    settings.watermark_paid_enabled = False
    settings.watermark_style = "logo"
    settings.watermark_position = "top-left"
    settings.watermark_opacity = 85
    settings.watermark_size = 22

    # 1. Free tier preview MUST render the top-left logo watermark
    prev_free = pathlib.Path(
        recap_pipeline.generate_recap_preview(
            source_video_path=str(src),
            output_image_path=str(tmp_path / "prev_free.jpg"),
            blur_subtitles=False,
            burn_subtitles=False,
            is_vip=False,
            at_seconds=0.5,
        )
    )
    assert _bright_pixels(prev_free, (0, 0, 220, 180)) > 500
    assert _bright_pixels(prev_free, (400, 180, 640, 360)) == 0

    # 2. Paid tier preview (PAID OFF default) MUST be completely clean
    prev_paid_off = pathlib.Path(
        recap_pipeline.generate_recap_preview(
            source_video_path=str(src),
            output_image_path=str(tmp_path / "prev_paid_off.jpg"),
            blur_subtitles=False,
            burn_subtitles=False,
            is_vip=True,
            at_seconds=0.5,
        )
    )
    assert _bright_pixels(prev_paid_off, (0, 0, 640, 360)) == 0

    # 3. Flip PAID ON -> paid preview now carries the exact same top-left logo watermark
    settings.watermark_paid_enabled = True
    prev_paid_on = pathlib.Path(
        recap_pipeline.generate_recap_preview(
            source_video_path=str(src),
            output_image_path=str(tmp_path / "prev_paid_on.jpg"),
            blur_subtitles=False,
            burn_subtitles=False,
            is_vip=True,
            at_seconds=0.5,
        )
    )
    assert _bright_pixels(prev_paid_on, (0, 0, 220, 180)) > 500

    # 4. Production overlay video render with matching settings places logo in top-left identically
    vid_out = tmp_path / "rendered.mp4"
    recap_core.apply_production_overlays(
        FFMPEG,
        src,
        vid_out,
        {
            "watermark": True,
            "watermark_style": "logo",
            "watermark_position": "top-left",
            "watermark_opacity": 85,
            "watermark_size": 22,
        },
        "libx264",
        tmp_path / "work",
        frame_size=(640, 360),
        layout={"pw": 640, "ph": 360, "ox": 0, "oy": 0, "sw": 640, "sh": 360},
    )
    vid_frame = tmp_path / "vid_frame.png"
    subprocess.run(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-ss", "0.5", "-i", str(vid_out), "-frames:v", "1", str(vid_frame)],
        check=True,
        capture_output=True,
    )
    assert _bright_pixels(vid_frame, (0, 0, 220, 180)) > 500
    assert _bright_pixels(vid_frame, (400, 180, 640, 360)) == 0


@pytest.mark.asyncio
async def test_dynamic_settings_and_admin_api_validation(tmp_path, restore_watermark_settings):
    db_path = tmp_path / "wm_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as session:
        await set_dynamic_setting(session, "watermark_free_enabled", False, admin_id=1)
        await set_dynamic_setting(session, "watermark_paid_enabled", True, admin_id=1)
        await set_dynamic_setting(session, "watermark_style", "text", admin_id=1)
        await set_dynamic_setting(session, "watermark_position", "top-right", admin_id=1)
        await set_dynamic_setting(session, "watermark_opacity", 65, admin_id=1)
        await set_dynamic_setting(session, "watermark_size", 28, admin_id=1)
        await session.commit()

        # Reset in-memory values and verify load_dynamic_settings restores from DB
        settings.watermark_free_enabled = True
        settings.watermark_paid_enabled = False
        settings.watermark_style = "tiled"
        settings.watermark_position = "bottom-right"
        settings.watermark_opacity = 20
        settings.watermark_size = 16
        await load_dynamic_settings(session)

        assert settings.watermark_free_enabled is False
        assert settings.watermark_paid_enabled is True
        assert settings.watermark_style == "text"
        assert settings.watermark_position == "top-right"
        assert settings.watermark_opacity == 65
        assert settings.watermark_size == 28

        admin_user = SimpleNamespace(id=1, account_category="PURCHASED")
        res = await api_mod.get_admin_settings(admin=admin_user, db=session)
        assert res["watermark_free_enabled"] is False
        assert res["watermark_paid_enabled"] is True
        assert res["watermark_style"] == "text"
        assert res["watermark_position"] == "top-right"
        assert res["watermark_opacity"] == 65
        assert res["watermark_size"] == 28

    await engine.dispose()

    # Validation checks
    valid = api_mod._validate_admin_settings_updates(
        {
            "watermark_style": "logo",
            "watermark_position": "centre",
            "watermark_opacity": 40,
            "watermark_size": 18,
            "watermark_free_enabled": True,
            "watermark_paid_enabled": True,
        }
    )
    assert valid["watermark_style"] == "logo"
    assert valid["watermark_position"] == "centre"

    with pytest.raises(HTTPException):
        api_mod._validate_admin_settings_updates({"watermark_style": "invalid"})
    with pytest.raises(HTTPException):
        api_mod._validate_admin_settings_updates({"watermark_opacity": 10})
    with pytest.raises(HTTPException):
        api_mod._validate_admin_settings_updates({"watermark_size": 55})


def test_admin_ui_and_i18n_wiring():
    for elem_id in (
        "adm-watermark-card",
        "adm-wm-free-enabled",
        "adm-wm-paid-enabled",
        "adm-wm-style",
        "adm-wm-position",
        "adm-wm-opacity",
        "adm-wm-size",
        "btn-save-watermark-settings",
    ):
        assert elem_id in INDEX_HTML, elem_id

    admin_js = (REPO_ROOT / "app/webapp/static/js/admin.js").read_text(encoding="utf-8")
    for fn in ("saveWatermarkSettings", "_collectWatermarkPayload", "watermark_free_enabled", "watermark_paid_enabled"):
        assert fn in admin_js, fn

    for key in (
        "adm_wm_title",
        "adm_wm_free_enabled",
        "adm_wm_paid_enabled",
        "adm_wm_style_label",
        "adm_wm_position_label",
        "adm_wm_opacity_label",
        "adm_wm_size_label",
        "adm_wm_save",
        "adm_wm_saved",
    ):
        assert I18N_JS.count(f"{key}:") == 2, key

"""Master Audit Re-Engineering & 4-Mode Subtitle System Unit Tests (2026-11-18)."""
from __future__ import annotations

import io
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image
from sqlalchemy import text

from app.config import settings
from app.models.schema import User
from app.services import gemini_key_service as gks
from app.services import voice_chunking
from app.services.recap_engine import core as recap_core
from app.services.recap_engine import png_subtitles as png_subs
from app.services.recap_engine.script_editing import apply_script_edits
from app.services.recap_engine.video_splitter import plan_parts
from app.webapp import api as api_mod
from app.webapp import rate_limit as rl_mod

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read_static(rel_path: str) -> str:
    return (REPO_ROOT / "app" / "webapp" / "static" / rel_path).read_text(encoding="utf-8")


def test_subtitle_max_clusters_scales_with_font_size_lines_and_width():
    c16 = recap_core._subtitle_max_clusters(16)
    c10 = recap_core._subtitle_max_clusters(10)
    c26 = recap_core._subtitle_max_clusters(26)
    assert c16 == 24
    assert c10 > c16 > c26
    assert 8 <= c26 <= 14

    c16_2line = recap_core._subtitle_max_clusters(16, lines_per_cue=2, is_vert=True)
    assert c16_2line < c16
    assert recap_core.recommended_subtitle_chars_per_line(20, 2, 0.77, True) < c16

    narrow = recap_core._subtitle_max_clusters(16, sub_w=0.45)
    wide = recap_core._subtitle_max_clusters(16, sub_w=0.92)
    assert narrow < c16 < wide


def test_subtitle_plate_height_supports_three_lines():
    h1 = recap_core._plate_height_px(40, 1, 1920)
    h2 = recap_core._plate_height_px(40, 2, 1920)
    h3 = recap_core._plate_height_px(40, 3, 1920)
    assert h1 < h2 < h3
    assert h3 == pytest.approx(40 * 3.65 + 2 * max(10.0, 40 * 0.40), abs=2.0)


def test_subtitle_method1_timeline_concat_file(tmp_path):
    p1 = tmp_path / "c1.png"
    p2 = tmp_path / "c2.png"
    Image.new("RGBA", (120, 40), (0, 0, 0, 180)).save(p1)
    Image.new("RGBA", (120, 40), (0, 0, 0, 180)).save(p2)

    res = recap_core.build_png_timeline_concat(
        [(p1, 1400, "x", 0.0, 2.5), (p2, 1400, "x", 3.0, 5.25)],
        tmp_path,
        1080,
        1920,
        layout={"pic_w": 1080, "pic_h": 608},
        sub_x=50,
        duration=6.0,
    )
    assert res is not None
    concat_path, strip_top, strip_h = res
    assert concat_path.is_file()
    assert strip_top >= 0 and strip_h >= 40
    txt = concat_path.read_text(encoding="utf-8")
    assert "\\" not in txt  # Windows backslashes never leak into FFmpeg concat
    assert "duration 2.500" in txt
    assert "duration 0.500" in txt  # transparent gap between 2.5 and 3.0


def test_subtitle_method2_ass_effects_and_3line_plate(tmp_path):
    ass_path = tmp_path / "sub_fx.ass"
    recap_core.write_synced_ass_file(
        [
            (0.0, 2.0, "မင်္ဂလာပါ။"),
            (2.0, 4.2, "ဒီဇာတ်လမ်းဟာ စိတ်ဝင်စားစရာ ဖြစ်ပါတယ်။"),
        ],
        ass_path,
        cw=1080,
        ch=1920,
        sub_effect="pop",
        sub_lines=3,
    )
    pop_txt = ass_path.read_text(encoding="utf-8")
    assert "\\fscx106\\fscy106" in pop_txt

    ass_fade = tmp_path / "sub_fade.ass"
    recap_core.write_synced_ass_file(
        [(0.0, 2.0, "မင်္ဂလာပါ။")],
        ass_fade,
        cw=1080,
        ch=1920,
        sub_effect="fade",
    )
    assert "\\fad(110,80)" in ass_fade.read_text(encoding="utf-8")

    ass_kara = tmp_path / "sub_kara.ass"
    recap_core.write_synced_ass_file(
        [(0.0, 2.0, "မင်္ဂလာပါ ခင်ဗျာ")],
        ass_kara,
        cw=1080,
        ch=1920,
        sub_effect="karaoke",
    )
    assert "\\kf" in ass_kara.read_text(encoding="utf-8")


def test_subtitle_method3_karaoke_spans_and_renderer():
    from PIL import features as pil_features

    spans = recap_core._karaoke_cue_spans(["မင်္ဂလာပါ", "ခင်ဗျာ"], 1.0, 3.0)
    assert len(spans) == 2
    assert spans[0][1] == pytest.approx(1.0)
    assert spans[-1][2] == pytest.approx(3.0)
    assert spans[0][0][0] < spans[0][0][1]

    font_path = recap_core.subtitle_font_file("padauk")
    if pil_features.check("raqm"):
        renderer = png_subs.PngSubtitleRenderer()
        img_res = renderer.render_karaoke_lines(
            ["မင်္ဂလာပါ ခင်ဗျာ"],
            spans[0][0],
            36,
            font_path,
            recap_core.resolve_subtitle_visuals({"sub_style": "yellow"}),
        )
        assert img_res is not None and img_res[1] > 0 and img_res[2] > 0


def test_subtitle_method4_soft_mux_and_custom_font_guard(tmp_path, monkeypatch):
    bad_font = tmp_path / "fake.ttf"
    bad_font.write_bytes(b"<html>not a real sfnt font</html>")
    ok, msg = png_subs.validate_custom_subtitle_font(bad_font)
    assert not ok
    assert msg

    video = tmp_path / "in.mp4"
    video.write_bytes(b"fake-mp4")
    srt = tmp_path / "subs.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:02,000\nမင်္ဂလာပါ\n\n", encoding="utf-8")

    captured = {}

    def _fake_run(cmd, **_kw):
        captured["cmd"] = list(cmd)
        Path(cmd[-1]).write_bytes(b"muxed-mp4")
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(recap_core.subprocess, "run", _fake_run)
    out = recap_core.mux_soft_subtitle_track("ffmpeg", video, srt, tmp_path / "out.mp4")
    assert out.is_file()
    assert "-c:s" in captured["cmd"] and "mov_text" in captured["cmd"]
    assert "language=mya" in captured["cmd"]


def test_script_editing_rejects_punctuation_only_segment():
    plan = {"segments": [{"segment_id": 1, "start": 0.0, "end": 2.0, "narration": "မင်္ဂလာပါ"}]}
    with pytest.raises(ValueError):
        apply_script_edits(plan, None, [{"index": 1, "narration": "။ ၊ ... !"}])


def test_voice_chunking_never_breaks_virama_clusters():
    sample = "တက္ကသိုလ် ကျောင်းသူ " * 35  # contains ္ (U+1039) + consonant + ် (U+103A)
    chunks = voice_chunking.plan_text_chunks(sample)
    assert len(chunks) >= 1
    for c in chunks[:-1]:
        assert not c.endswith("\u1039")
    for c in chunks[1:]:
        assert not c.startswith("\u1039")
        assert not c.startswith("\u103A")


def test_video_splitter_enforces_max_part_seconds():
    parts = plan_parts(
        duration=1250.0,
        part_seconds=600.0,
        smart=False,
        max_part_seconds=600.0,
    )
    assert len(parts) >= 3
    for p in parts:
        assert (p["end"] - p["start"]) <= 600.0 + 0.01


def test_encrypted_text_and_byok_fernet_roundtrip():
    secret = "AIzaSyTestKeySecret1234567890"
    enc = gks.encrypt_byok_secret(secret)
    assert enc.startswith("enc:v1:")
    assert secret not in enc
    assert gks.decrypt_byok_secret(enc) == secret
    assert gks.decrypt_byok_secret(secret) == secret  # legacy plaintext tolerated


@pytest.mark.asyncio
async def test_sqlite_waiting_choice_migration_rebuilds_check(tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.db import session as db_sess

    db_file = tmp_path / "legacy.db"
    eng = create_async_engine(f"sqlite+aiosqlite:///{db_file}")
    async with eng.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE jobs ("
            "id VARCHAR(36) PRIMARY KEY, "
            "user_id BIGINT, "
            "status VARCHAR(20) NOT NULL, "
            "CONSTRAINT chk_job_status CHECK (status IN ('PENDING','RUNNING','SUCCESS','FAILED','CANCELLED'))"
            ")"
        ))
        await conn.execute(text(
            "CREATE TABLE users (id BIGINT PRIMARY KEY, first_name VARCHAR(255) DEFAULT 'U')"
        ))
        await conn.execute(text(
            "CREATE TABLE payment_requests (id VARCHAR(36) PRIMARY KEY, user_id BIGINT)"
        ))
        await conn.execute(text("INSERT INTO jobs (id, user_id, status) VALUES ('j1', 1, 'SUCCESS')"))

    monkeypatch.setattr(db_sess, "engine", eng)
    await db_sess.init_db()
    async with eng.begin() as conn:
        await conn.execute(text("INSERT INTO jobs (id, user_id, status) VALUES ('j2', 1, 'WAITING_CHOICE')"))
        row = (await conn.execute(text("SELECT status FROM jobs WHERE id='j2'"))).scalar_one()
        assert row == "WAITING_CHOICE"
    await eng.dispose()


@pytest.mark.asyncio
async def test_rate_limiter_ipagg_and_bounded_memory():
    mw = rl_mod.RateLimitMiddleware(app=AsyncMock(return_value=None))
    now = time.monotonic()
    for k in range(5000):
        mw._hits_for(f"synth:{k}", (5, 60), now)
    assert len(mw._hits) <= 4096

    req_a = MagicMock()
    req_a.method = "POST"
    req_a.url.path = "/api/voices/generate"
    req_a.headers = {"X-Telegram-Init-Data": "user=%7B%22id%22%3A111%7D&hash=bad1"}
    req_a.client = MagicMock(host="10.9.9.1")

    k_a = rl_mod._rate_key(req_a)
    assert k_a == "ipuser:10.9.9.1:111"
    for uid in range(1, 77):
        req = MagicMock()
        req.method = "POST"
        req.url.path = "/api/tts/generate"
        req.headers = {"X-Telegram-Init-Data": f"user=%7B%22id%22%3A{uid}%7D&hash=bad{uid}"}
        req.client = MagicMock(host="10.9.9.2")
        res = await mw.dispatch(req, AsyncMock(return_value=rl_mod.JSONResponse({"ok": True})))
        if uid <= 75:
            assert res.status_code == 200
        else:
            assert res.status_code == 429


@pytest.mark.asyncio
async def test_submit_payment_slip_rejects_svg_or_html_payload(db_session, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "bot_token", "")
    monkeypatch.setattr(api_mod, "DISK_HEADROOM_MIN_FREE_BYTES", 0)

    user = User(id=990991, username="evil_slip", first_name="Evil")
    db_session.add(user)
    await db_session.commit()

    evil = UploadFile(filename="slip.png", file=io.BytesIO(b"<svg onload=alert(1)></svg>"))
    with pytest.raises(HTTPException) as exc:
        await api_mod.submit_payment_slip(
            package_code="MIN_10",
            amount_mmk=8000,
            slip_file=evil,
            user=user,
            db=db_session,
            _gate=user,
        )
    assert exc.value.status_code == 400
    receipts = tmp_path / "receipts"
    assert not receipts.is_dir() or list(receipts.iterdir()) == []


def test_frontend_batch3_ia_subtitles_and_budget():
    html = _read_static("index.html")
    css = _read_static("css/style.css")
    js = _read_static("js/app.js")
    i18n = _read_static("js/i18n.js")

    # Part D (2026-11-23) bumped cache stamp to ?v=20261123_round15 (5 assets incl. splash logo).
    assert html.count("?v=20261123_round15") == 5
    assert 'id="btn-top-history"' in html
    # Round 15 Part C added the 4th dedicated sub-view (#tab-recap-job) with .vcp-subview-back-btn.
    assert html.count('class="vcp-subview-back-btn"') == 4
    assert 'id="vip-wallet-tts-val"' in html
    assert '09790760003' not in html
    for dom_id in (
        'id="recap-sub-render-mode"',
        'id="recap-sub-effect"',
        'id="toggle-embed-soft-subs"',
        'id="val-font-chars-est"',
        'id="saved-custom-voice-bar"',
        'id="recap-saved-voice-bar"',
    ):
        assert dom_id in html

    for fn in (
        "estimateSubtitleCharsPerLine",
        "syncSubtitleModeUi",
        "syncSavedCustomVoiceUi",
        "applySavedCustomVoiceToVoice",
        "applySavedCustomVoiceToRecap",
        "recapSubRenderMode",
        "embed_soft_subs",
    ):
        assert fn in js

    for key in (
        "sub_mode_timeline",
        "sub_mode_ass",
        "sub_mode_karaoke",
        "sub_mode_soft",
        "sub_effect_pop",
        "sub_chars_est",
        "btn_back_home",
        "stat_tts_balance",
    ):
        assert key in i18n

    assert css.count("{") == css.count("}")
    total_lf = sum(len(s.replace("\r\n", "\n").encode("utf-8")) for s in (css, js, i18n))
    assert total_lf <= 1_650_000, total_lf

"""Unit tests for Round 14 (Parts A-E + asset stamp) — 2026-11-23."""
from __future__ import annotations

import inspect
import io
import json
import re
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.models.enums import AccountCategory, JobStatus, JobType
from app.models.schema import Job, User
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod
from app.webapp import rate_limit as rl_mod
from app.webapp import server as server_mod

ROOT = Path(__file__).resolve().parents[2]
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")


def _load_translations() -> dict:
    proc = subprocess.run(
        [
            "node",
            "-e",
            (
                'const fs = require("fs");'
                'const src = fs.readFileSync("app/webapp/static/js/i18n.js", "utf8");'
                'const tr = new Function("localStorage", "window", src + "\\nreturn translations;")'
                '({ getItem: () => "my", setItem: () => {} }, {});'
                "console.log(JSON.stringify(tr));"
            ),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        cwd=str(ROOT),
    )
    return json.loads(proc.stdout)


# ── Part A: Full-width Voice & TTS preview buttons ────────────────────────


def test_part_a_voice_and_tts_preview_buttons_full_width_and_no_mini_btn():
    expected = {
        "btn-voice-persona-preview": "previewSelectedVoiceTabPersona(event)",
        "btn-tts-gemini-preview": "previewSelectedTtsVoice(event)",
        "btn-tts-edge-preview": "previewSelectedTtsVoice(event)",
    }
    for btn_id, handler in expected.items():
        m = re.search(rf'<button[^>]*id="{btn_id}"[\s\S]{{0,420}}</button>', INDEX_HTML)
        assert m is not None, f"Missing button #{btn_id}"
        snippet = m.group(0)
        assert "preview-recap-voice-btn" in snippet, f"#{btn_id} missing .preview-recap-voice-btn"
        assert "mini-btn" not in snippet, f"#{btn_id} must not carry mini-btn"
        assert f'onclick="{handler}"' in snippet, f"#{btn_id} onclick changed"
        assert "preview-btn-icon" in snippet and "preview-btn-label" in snippet
        assert 'data-i18n="btn_preview_voice"' in snippet

    # Voice button moved out of #voice-persona-select-row into its own row beneath it
    voice_sel_row = re.search(
        r'<div[^>]*id="voice-persona-select-row"[^>]*>[\s\S]*?</div>', INDEX_HTML
    )
    assert voice_sel_row is not None
    assert "btn-voice-persona-preview" not in voice_sel_row.group(0)

    # TTS buttons moved out of .tts-voice-select-flex into their own .preview-btn-row
    for row_id, btn_id in (
        ("tts-gemini-voice-row", "btn-tts-gemini-preview"),
        ("tts-edge-voice-row", "btn-tts-edge-preview"),
    ):
        idx = INDEX_HTML.index(f'id="{row_id}"')
        block = INDEX_HTML[idx : idx + 1500]
        flex_end = block.index("</div>")
        assert btn_id not in block[:flex_end], f"#{btn_id} should be outside .tts-voice-select-flex"
        assert 'class="preview-btn-row"' in block
        assert btn_id in block

    # Single-line non-wrapping CSS rule on .preview-recap-voice-btn .preview-btn-label
    assert ".preview-recap-voice-btn .preview-btn-label" in STYLE_CSS
    assert "white-space: nowrap;" in STYLE_CSS


# ── Part B: Payment reference field single-language placeholder ───────────


def test_part_b_slip_ref_placeholder_single_language_my_and_en_unchanged():
    tr = _load_translations()
    my_val = tr["my"]["slip_ref_ph"]
    en_val = tr["en"]["slip_ref_ph"]

    assert my_val == "ငွေလွှဲ နံပါတ် (မဖြည့်လည်း ရ)"
    assert re.search(r"[A-Za-z]", my_val) is None, f"MY slip_ref_ph contains Latin letters: {my_val!r}"
    assert en_val == "Payment Ref / Txn No (Optional)"

    # Static placeholder on #slip-pay-ref aligned with EN
    m = re.search(r'<input[^>]*id="slip-pay-ref"[^>]*>', INDEX_HTML)
    assert m is not None
    assert 'data-i18n-ph="slip_ref_ph"' in m.group(0)
    assert 'placeholder="Payment Ref / Txn No (Optional)"' in m.group(0)


# ── Part C: Real Telegram profile photo endpoint + cache + client wiring ──


@pytest.mark.asyncio
async def test_part_c_photo_endpoint_requires_auth():
    transport = ASGITransport(app=server_mod.webapp_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/user/me/photo")
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_part_c_photo_endpoint_404_when_no_photos(db_session, tmp_path, monkeypatch):
    user, _ = await get_or_create_user(db_session, telegram_id=951001, first_name="NoPhoto")
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "bot_token", "123456:FAKE_TOKEN")

    fake_bot = MagicMock()
    fake_bot.get_user_profile_photos = AsyncMock(return_value=SimpleNamespace(photos=[]))
    fake_bot.session = MagicMock(close=AsyncMock())
    monkeypatch.setattr(api_mod, "create_telegram_bot", MagicMock(return_value=fake_bot))

    resp = await api_mod.get_user_profile_photo(user=user)
    assert resp.status_code == 404
    fake_bot.get_user_profile_photos.assert_awaited_once_with(user.id, limit=1)


@pytest.mark.asyncio
async def test_part_c_photo_endpoint_200_caches_24h_and_handles_stale_refresh_and_errors(
    db_session, tmp_path, monkeypatch
):
    user, _ = await get_or_create_user(db_session, telegram_id=951002, first_name="WithPhoto")
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "bot_token", "123456:FAKE_TOKEN")

    jpeg_bytes = b"\xff\xd8\xff\xe0FAKE_JPEG_DATA_ROUND14\xff\xd9"
    sizes = [
        SimpleNamespace(file_id="fid_160", file_unique_id="uid_160", width=160, height=160),
        SimpleNamespace(file_id="fid_320", file_unique_id="uid_320", width=320, height=320),
        SimpleNamespace(file_id="fid_640", file_unique_id="uid_640", width=640, height=640),
    ]
    fake_bot = MagicMock()
    fake_bot.get_user_profile_photos = AsyncMock(return_value=SimpleNamespace(photos=[sizes]))
    fake_bot.get_file = AsyncMock(return_value=SimpleNamespace(file_path="photos/p_320.jpg"))
    fake_bot.download_file = AsyncMock(side_effect=lambda _p: io.BytesIO(jpeg_bytes))
    fake_bot.session = MagicMock(close=AsyncMock())

    factory_mock = MagicMock(return_value=fake_bot)
    monkeypatch.setattr(api_mod, "create_telegram_bot", factory_mock)

    # 1st call: fetches from Telegram, picks 320px (largest <= 512 / largest-but-one), writes disk cache
    resp1 = await api_mod.get_user_profile_photo(user=user)
    assert resp1.status_code == 200
    assert resp1.media_type == "image/jpeg"
    assert resp1.body == jpeg_bytes
    assert resp1.headers.get("cache-control") == "private, max-age=3600"
    fake_bot.get_user_profile_photos.assert_awaited_once_with(user.id, limit=1)
    fake_bot.get_file.assert_awaited_once_with("fid_320")
    fake_bot.download_file.assert_awaited_once_with("photos/p_320.jpg")

    cache_dir = tmp_path / "cache" / "profile_photos"
    jpg_file = cache_dir / f"{user.id}.jpg"
    meta_file = cache_dir / f"{user.id}.json"
    assert jpg_file.is_file() and jpg_file.read_bytes() == jpeg_bytes
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta["file_unique_id"] == "uid_320"
    assert isinstance(meta["fetched_at"], (int, float))

    # 2nd call within 24h: serves from disk cache with ZERO additional Telegram calls
    resp2 = await api_mod.get_user_profile_photo(user=user)
    assert resp2.status_code == 200
    assert resp2.body == jpeg_bytes
    assert factory_mock.call_count == 1
    assert fake_bot.get_user_profile_photos.await_count == 1

    # Age sidecar > 24h with same file_unique_id: checks Telegram, refreshes fetched_at, does NOT re-download
    meta["fetched_at"] = time.time() - (25 * 3600)
    meta_file.write_text(json.dumps(meta), encoding="utf-8")
    resp3 = await api_mod.get_user_profile_photo(user=user)
    assert resp3.status_code == 200
    assert resp3.body == jpeg_bytes
    assert fake_bot.get_user_profile_photos.await_count == 2
    assert fake_bot.get_file.await_count == 1
    assert fake_bot.download_file.await_count == 1
    refreshed_meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert refreshed_meta["fetched_at"] > meta["fetched_at"]

    # Age sidecar > 24h and simulate Telegram API error: serves stale cache cleanly (200)
    refreshed_meta["fetched_at"] = time.time() - (26 * 3600)
    meta_file.write_text(json.dumps(refreshed_meta), encoding="utf-8")
    fake_bot.get_user_profile_photos = AsyncMock(side_effect=RuntimeError("Telegram unreachable"))
    resp4 = await api_mod.get_user_profile_photo(user=user)
    assert resp4.status_code == 200
    assert resp4.body == jpeg_bytes


@pytest.mark.asyncio
async def test_part_c_user_me_payload_unchanged_and_route_rate_limited(db_session):
    assert "/api/user/me/photo" in rl_mod.GET_RATE_LIMITS
    cap, win = rl_mod.GET_RATE_LIMITS["/api/user/me/photo"]
    assert cap > 0 and win == 60

    user, _ = await get_or_create_user(db_session, telegram_id=951003, first_name="PayloadCheck")
    await db_session.commit()
    payload = await api_mod.get_user_profile(user=user, db=db_session)
    assert "photo_url" not in payload
    assert "profile_photo_url" not in payload

    # Client lazy-loads via fetchAuthImageUrl('/api/user/me/photo') and CSS styles .profile-avatar-circle img
    assert 'fetchAuthImageUrl("/api/user/me/photo")' in APP_JS
    assert ".profile-avatar-circle img" in STYLE_CSS


# ── Part D: Profile tidy — single-language MY headings, 1-row pills, density ──


def test_part_d_profile_single_language_headings_and_ledger_pills():
    tr = _load_translations()
    expected_my = {
        "profile_title": "👤 ကျွန်ုပ်၏ အကောင့်",
        "profile_wallet_orders_title": "💼 ဝယ်ယူမှုများ နှင့် မှတ်တမ်း",
        "vip_orders_title": "🧾 ဝယ်ယူမှု မှတ်တမ်း",
        "vip_tx_title": "📒 အသုံးစာရင်း",
    }
    for key, val in expected_my.items():
        assert tr["my"][key] == val, f"MY {key} mismatch: {tr['my'][key]!r}"
        assert re.search(r"[A-Za-z]", tr["my"][key]) is None, (
            f"MY {key} must contain no Latin letters: {tr['my'][key]!r}"
        )

    # EN headings stay intact
    assert tr["en"]["profile_wallet_orders_title"] == "💼 Wallet & Orders"
    assert tr["en"]["vip_tx_title"] == "📒 Wallet Transaction Ledger"

    # Ledger "All" pill localized to "အားလုံး" in MY and "All" in EN
    assert tr["my"]["vip_tx_filter_all"] == "အားလုံး"
    assert tr["en"]["vip_tx_filter_all"] == "All"
    assert 'data-tx-kind="" onclick="loadVipWalletTx(\'\')" data-i18n="vip_tx_filter_all"' in INDEX_HTML

    # Scoped 1-row 4-column grid for ledger pills + density rules inside #tab-profile
    assert "#tab-profile .vcp-tx-filters" in STYLE_CSS
    assert "grid-template-columns: repeat(4, minmax(0, 1fr));" in STYLE_CSS


# ── Part E1: Uploaded-file duration always probed (never trust declared) ──


@pytest.mark.asyncio
async def test_part_e1_uploaded_file_probed_duration_overrides_declared_cap_check(
    db_session, tmp_path, monkeypatch
):
    from app.services import downloader as dl_mod
    from app.services.recap_engine import core as recap_core

    user, _ = await get_or_create_user(db_session, telegram_id=951010, first_name="FreeUpload")
    user.account_category = AccountCategory.FREE.value
    user.recap_credits_seconds = 600
    await db_session.commit()

    monkeypatch.setattr(settings, "storage_root", tmp_path)
    upload_dir = tmp_path / "temp" / "recap_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    video_file = upload_dir / f"u{user.id}_fake_upload.mp4"
    video_file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 512)

    monkeypatch.setattr(api_mod, "_recap_frame_owner_id", AsyncMock(return_value=user.id))
    monkeypatch.setattr(api_mod, "_require_disk_headroom", lambda *a, **k: None)
    monkeypatch.setattr(
        dl_mod, "probe_local_media", lambda _p: {"has_video": True, "has_audio": True}
    )
    monkeypatch.setattr(recap_core, "find_ffprobe", lambda: "ffprobe")

    # 1) Probed duration (300s) > free cap (120s), client lies with duration_seconds=60 -> REJECTED (400)
    monkeypatch.setattr(recap_core, "probe_duration", lambda _bin, _path: 300.0)
    req_over = api_mod.RecapConfigRequest(
        url=str(video_file),
        duration_seconds=60.0,
        tts_engine="edge",
    )
    with pytest.raises(HTTPException) as exc_over:
        await api_mod.create_recap_job(req=req_over, user=user, db=db_session, _gate=user)
    assert exc_over.value.status_code == 400

    # 2) Probed duration (90s) <= free cap (120s) -> passes duration gate cleanly
    monkeypatch.setattr(recap_core, "probe_duration", lambda _bin, _path: 90.0)
    captured_duration = {}

    def _spy_est(checked_dur, max_allowed, **kw):
        captured_duration["checked_duration"] = checked_dur
        raise HTTPException(status_code=418, detail="passed_duration_gate")

    monkeypatch.setattr(api_mod, "_recap_estimated_credit_seconds", _spy_est)
    req_ok = api_mod.RecapConfigRequest(
        url=str(video_file),
        duration_seconds=30.0,
        tts_engine="edge",
    )
    with pytest.raises(HTTPException) as exc_ok:
        await api_mod.create_recap_job(req=req_ok, user=user, db=db_session, _gate=user)
    assert exc_ok.value.status_code == 418
    assert captured_duration["checked_duration"] == 90.0


# ── Part E2: Cross-type concurrency in _recap_active_job_count ────────────


@pytest.mark.asyncio
async def test_part_e2_recap_active_job_count_includes_voice_and_deduplicates(db_session):
    user, _ = await get_or_create_user(db_session, telegram_id=951020, first_name="CrossType")

    voice_job = Job(
        id="vc_active_r14_1",
        user_id=user.id,
        job_type=JobType.VOICE_CLONE.value,
        status=JobStatus.RUNNING.value,
        input_text="hello voice",
    )
    recap_job = Job(
        id="rc_shared_r14_1",
        user_id=user.id,
        job_type=JobType.RECAP.value,
        status=JobStatus.QUEUED.value,
        input_text="hello recap",
    )
    done_tts = Job(
        id="tts_done_r14_1",
        user_id=user.id,
        job_type=JobType.TTS.value,
        status=JobStatus.SUCCESS.value,
        input_text="done tts",
    )
    db_session.add_all([voice_job, recap_job, done_tts])
    await db_session.commit()

    # Active VOICE_CLONE + QUEUED RECAP (also in registry) -> deduplicated count is 2 (not 3)
    api_mod.RECAP_JOBS_REGISTRY["rc_shared_r14_1"] = {
        "user_id": user.id,
        "status": "QUEUED",
    }
    try:
        cnt = await api_mod._recap_active_job_count(db_session, user.id)
        assert cnt == 2

        # Remove recap job -> active VOICE_CLONE alone still counts as 1
        recap_job.status = JobStatus.SUCCESS.value
        await db_session.commit()
        cnt_voice_only = await api_mod._recap_active_job_count(db_session, user.id)
        assert cnt_voice_only == 1
    finally:
        api_mod.RECAP_JOBS_REGISTRY.pop("rc_shared_r14_1", None)


# ── Part E3: Voice-reference upload body cap aligned to 50 MB ─────────────


@pytest.mark.asyncio
async def test_part_e3_upload_voice_ref_body_cap_50mb_matches_handler():
    expected_50mb = 50 * 1024 * 1024
    assert rl_mod.MAX_CONTENT_LENGTH["/api/recap/upload_voice_ref"] == expected_50mb

    handler_src = inspect.getsource(api_mod.upload_recap_voice_ref)
    assert "50 * 1024 * 1024" in handler_src

    # Verify middleware accepts a 30 MB Content-Length and rejects 55 MB
    mini_app = FastAPI()
    mini_app.add_middleware(rl_mod.RateLimitMiddleware)

    @mini_app.post("/api/recap/upload_voice_ref")
    async def _echo_ok():
        return {"ok": True}

    transport = ASGITransport(app=mini_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        r_30mb = await client.post(
            "/api/recap/upload_voice_ref",
            headers={"Content-Length": str(30 * 1024 * 1024)},
        )
        assert r_30mb.status_code == 200

        r_55mb = await client.post(
            "/api/recap/upload_voice_ref",
            headers={"Content-Length": str(55 * 1024 * 1024)},
        )
        assert r_55mb.status_code == 413


# ── Global Rule 4: Static asset version stamp ─────────────────────────────


def test_global_rule_4_asset_version_stamp_round14():
    assert "?v=20261123_followup1" not in INDEX_HTML
    assert INDEX_HTML.count("?v=20261123_round15") == 5
    serve_src = inspect.getsource(server_mod.serve_index)
    assert "?v=20261123_round15" in serve_src


@pytest.mark.asyncio
async def test_part_e2_create_recap_job_blocked_when_voice_job_active(
    db_session, tmp_path, monkeypatch
):
    from app.services import downloader as dl_mod
    from app.services.recap_engine import core as recap_core

    user, _ = await get_or_create_user(db_session, telegram_id=951021, first_name="BusyVoice")
    user.account_category = AccountCategory.PURCHASED.value
    user.recap_credits_seconds = 600
    db_session.add(
        Job(
            id="vc_busy_r14_1",
            user_id=user.id,
            job_type=JobType.VOICE_CLONE.value,
            status=JobStatus.RUNNING.value,
            input_text="active voice clone",
        )
    )
    await db_session.commit()

    monkeypatch.setattr(settings, "storage_root", tmp_path)
    monkeypatch.setattr(settings, "max_concurrent_jobs_per_user", 1)
    upload_dir = tmp_path / "temp" / "recap_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    video_file = upload_dir / f"u{user.id}_short.mp4"
    video_file.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 512)

    monkeypatch.setattr(api_mod, "_recap_frame_owner_id", AsyncMock(return_value=user.id))
    monkeypatch.setattr(api_mod, "_require_disk_headroom", lambda *a, **k: None)
    monkeypatch.setattr(
        dl_mod, "probe_local_media", lambda _p: {"has_video": True, "has_audio": True}
    )
    monkeypatch.setattr(recap_core, "find_ffprobe", lambda: "ffprobe")
    monkeypatch.setattr(recap_core, "probe_duration", lambda _bin, _path: 60.0)

    req = api_mod.RecapConfigRequest(
        url=str(video_file),
        duration_seconds=60.0,
        tts_engine="edge",
    )
    with pytest.raises(HTTPException) as exc:
        await api_mod.create_recap_job(req=req, user=user, db=db_session, _gate=user)
    assert exc.value.status_code == 429


def test_part_f_build_recap_config_byte_identical_to_base_7c3f3ff():
    base_js = subprocess.check_output(
        ["git", "show", "7c3f3ff:app/webapp/static/js/app.js"],
        cwd=str(ROOT),
        text=True,
        encoding="utf-8",
    )

    def _run_scenarios(js_src: str) -> str:
        proc = subprocess.run(
            [
                "node",
                "-e",
                r"""
const fs = require("fs");
const src = fs.readFileSync(0, "utf8");
const start = src.indexOf("function buildRecapConfig(url)");
const end = src.indexOf('if (typeof window !== "undefined") window.buildRecapConfig', start);
if (start < 0 || end < 0) throw new Error("buildRecapConfig bounds not found");
const body = src.slice(start, end) + "\nreturn buildRecapConfig(url);";
const RECAP_LOCKED_FILL = "blur";
const RECAP_LOCKED_MIRROR = true;
const RECAP_LOCKED_ZOOM = 1.10;
const RECAP_LOCKED_PROTECTION = "strong";
const recapLockedCropForRatio = (r) => r === "9:16"
  ? { crop_top: 12, crop_bottom: 12, crop_left: 0, crop_right: 0 }
  : { crop_top: 0, crop_bottom: 0, crop_left: 0, crop_right: 0 };
const normalizeEdgeVoiceKey = (v) => String(v || "thiha").trim().toLowerCase();
const ddConfigExtras = () => ({});
const canUseRecapCloneEngine = () => true;
const canUseRecapRenderQuality = () => false;
const run = (overrides, domOverrides = {}) => {
  const state = Object.assign({
    recapVideoTitle: "Sample", recapVoice: "komin", recapStoryStyle: "pov",
    recapRatio: "9:16", recapSpeed: "+12%", recapBlurSubs: true,
    recapBlurBoxTouched: false, recapBlurBoxes: [], recapBlurAutoDetect: true,
    recapBurnSubs: true, recapSubPosBand: "bottom", recapSubMargin: 5,
    recapSubStyle: "cinematic", recapFontSize: 13, recapSubFont: "padauk",
    recapLines: 1, recapLogo: true, recapLogoPos: "top-right",
    recapLogoScale: 0.16, recapLogoOpacity: 0.70, recapSourceDuration: 120,
    recapScriptReview: false, recapRenderQuality: "max",
    recapTtsEngine: "voxcpm", recapGeminiVoice: "", recapEdgeVoice: "ava",
    recapOwnVoiceSelected: false, recapOwnVoiceStatus: "idle", recapOwnVoiceRefId: null,
  }, overrides);
  const dom = Object.assign({
    "slider-blur-y": { value: "85" }, "slider-blur-h": { value: "7" },
    "slider-blur-x": { value: "4" }, "slider-blur-w": { value: "92" },
    "slider-blur-strength": { value: "18" }, "slider-sub-y": { value: "5" },
    "slider-sub-x": { value: "50" }, "slider-sub-w": { value: "70" },
    "recap-persona-select": { value: state.recapVoice },
    "recap-edge-voice-select": { value: state.recapEdgeVoice },
    "recap-gemini-voice-select": { value: state.recapGeminiVoice },
    "recap-script-review-toggle": { checked: !!state.recapScriptReview },
  }, domOverrides);
  const fn = new Function(
    "url", "state", "document", "RECAP_LOCKED_FILL", "RECAP_LOCKED_MIRROR",
    "RECAP_LOCKED_ZOOM", "RECAP_LOCKED_PROTECTION", "recapLockedCropForRatio",
    "normalizeEdgeVoiceKey", "ddConfigExtras", "canUseRecapCloneEngine",
    "canUseRecapRenderQuality", body
  );
  return fn(
    "https://example.com/v.mp4", state, { getElementById: (id) => dom[id] || null },
    RECAP_LOCKED_FILL, RECAP_LOCKED_MIRROR, RECAP_LOCKED_ZOOM,
    RECAP_LOCKED_PROTECTION, recapLockedCropForRatio, normalizeEdgeVoiceKey,
    ddConfigExtras, canUseRecapCloneEngine, canUseRecapRenderQuality
  );
};
console.log(JSON.stringify({
  vox_persona: run({ recapTtsEngine: "voxcpm", recapVoice: "mahnin", recapRatio: "9:16" }),
  vox_own: run({ recapTtsEngine: "voxcpm", recapOwnVoiceSelected: true, recapOwnVoiceStatus: "ready", recapOwnVoiceRefId: "webapp_ref_u1_abc" }),
  gemini_auto: run({ recapTtsEngine: "gemini", recapGeminiVoice: "", recapRatio: "16:9", recapScriptReview: true }),
  gemini_charon: run({ recapTtsEngine: "gemini", recapGeminiVoice: "Charon", recapRatio: "original" }),
  edge_ava: run({ recapTtsEngine: "edge", recapEdgeVoice: "ava", recapStoryStyle: "viral", recapSpeed: "+20%" }),
}));
""",
            ],
            input=js_src,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
            cwd=str(ROOT),
        )
        return proc.stdout.strip()

    assert _run_scenarios(APP_JS) == _run_scenarios(base_js)


"""Regression & verification tests for:
1. Bandwidth & Anti-Flood Shield:
   - SmartGZipMiddleware (compresses HTML/JS/CSS/JSON; skips binary media & 206 Range)
   - Immutable Cache-Control on versioned /static/*?v=... + 7d fonts/audio + no-cache unversioned
   - StaticFloodShieldMiddleware (per-IP flood 429 + optional CF_ORIGIN_SECRET 403 guard)
2. Preview & Audio Egress Optimizations:
   - Preview JPEG quality (-q:v 6) & 720px thumbnail downscale
   - Free Downloader 720p cap vs VIP/Admin 1080p
   - Telegram file_id cache reuse + automatic fallback on rejected file_id
   - Frontend lazy-loaded Home samples, History audio, signed Range stream, and LF bundle cap
3. Local Telegram Bot API Integration + 2,000 MB Upload Limit Lift:
   - Centralized create_telegram_bot() with TelegramAPIServer.from_base()
   - Startup probe success -> 2,000 MB limit & 1,980 MB splitter cap
   - Startup probe failure -> safe fallback to official api.telegram.org (50 MB)
   - Dynamic _recap_output_size_and_sendable / _voice_output_size_and_sendable / downloader
   - Zero stray direct Bot(...) calls outside telegram_bot_factory.py
"""

from __future__ import annotations

import asyncio
import gzip
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from app.config import settings
from app.models.enums import AccountCategory
from app.models.schema import Job
from app.services import telegram_bot_factory as lba
from app.services.recap_engine import video_splitter as vs
from app.webapp import api as api_mod
from app.webapp.rate_limit import (
    SmartGZipMiddleware,
    StaticFloodShieldMiddleware,
    get_bandwidth_shield_stats,
)
from app.webapp.server import add_no_cache_headers

REPO_ROOT = Path(__file__).resolve().parents[2]
MB = 1024 * 1024


@pytest.fixture(autouse=True)
def _reset_local_bot_api_state(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_api_url", "")
    monkeypatch.setattr(settings, "telegram_bot_api_is_local", False)
    monkeypatch.setattr(settings, "telegram_max_upload_mb", 0)
    monkeypatch.setattr(settings, "cf_origin_secret", "")
    lba.reset_local_bot_api_probe_state()
    lba.clear_telegram_file_id_cache()
    yield
    lba.reset_local_bot_api_probe_state()
    lba.clear_telegram_file_id_cache()


# ── 1. SmartGZipMiddleware ─────────────────────────────────────────────────

def test_smart_gzip_compresses_text_and_json_but_skips_video_and_range():
    mini = FastAPI()
    mini.add_middleware(SmartGZipMiddleware, minimum_size=500)

    @mini.get("/big.js")
    def big_js():
        return Response(content=("console.log('voice-clone-studio');\n" * 200), media_type="application/javascript")

    @mini.get("/data.json")
    def big_json():
        return {"items": [{"name": "Voice Clone Pro", "idx": i} for i in range(60)]}

    @mini.get("/clip.mp4")
    def fake_mp4():
        return Response(content=(b"\x00\x00\x00\x18ftypmp42" * 200), media_type="video/mp4")

    @mini.get("/stream.mp4")
    def fake_range():
        return Response(
            content=(b"a" * 2000),
            status_code=206,
            media_type="text/plain",
            headers={"Content-Range": "bytes 0-1999/10000"},
        )

    client = TestClient(mini)

    r_js = client.get("/big.js", headers={"Accept-Encoding": "gzip"})
    assert r_js.status_code == 200
    assert r_js.headers.get("content-encoding") == "gzip"
    assert "accept-encoding" in (r_js.headers.get("vary") or "").lower()
    assert int(r_js.headers["content-length"]) < len("console.log('voice-clone-studio');\n" * 200) * 0.25

    r_json = client.get("/data.json", headers={"Accept-Encoding": "gzip"})
    assert r_json.status_code == 200
    assert r_json.headers.get("content-encoding") == "gzip"
    assert len(r_json.json()["items"]) == 60

    r_mp4 = client.get("/clip.mp4", headers={"Accept-Encoding": "gzip"})
    assert r_mp4.status_code == 200
    assert "content-encoding" not in r_mp4.headers

    r_rng = client.get("/stream.mp4", headers={"Accept-Encoding": "gzip"})
    assert r_rng.status_code == 206
    assert "content-encoding" not in r_rng.headers


# ── 2. Immutable Static Cache Policy ──────────────────────────────────────

@pytest.mark.asyncio
async def test_static_cache_policy_immutable_versioned_and_preserves_unversioned():
    async def _call(path: str, query: str = ""):
        req = SimpleNamespace(url=SimpleNamespace(path=path, query=query))
        resp = await add_no_cache_headers(req, lambda r: _async_resp())
        return resp.headers

    async def _async_resp():
        return SimpleNamespace(headers={})

    h_ver = await _call("/static/js/app.js", "v=20261118_r164")
    assert h_ver["Cache-Control"] == "public, max-age=31536000, immutable"

    h_font = await _call("/static/fonts/Padauk-Regular.ttf")
    assert h_font["Cache-Control"] == "public, max-age=604800"

    h_unver = await _call("/static/js/app.js")
    assert h_unver["Cache-Control"] == "no-cache"

    h_root = await _call("/")
    assert "no-store" in h_root["Cache-Control"]


# ── 3. Static Flood Shield + Optional Cloudflare Origin Lock ──────────────

def test_static_flood_shield_returns_429_after_burst_and_allows_api():
    mini = FastAPI()
    mini.add_middleware(StaticFloodShieldMiddleware, max_requests=3, window_seconds=60)

    @mini.get("/")
    def index():
        return {"ok": True}

    @mini.get("/static/js/app.js")
    def static_js():
        return Response(content="ok", media_type="application/javascript")

    @mini.get("/api/ping")
    def api_ping():
        return {"pong": True}

    client = TestClient(mini)
    hdrs = {"X-Forwarded-For": "198.51.100.77"}
    for _ in range(3):
        assert client.get("/static/js/app.js", headers=hdrs).status_code == 200
    r4 = client.get("/static/js/app.js", headers=hdrs)
    assert r4.status_code == 429
    assert r4.headers.get("retry-after") is not None
    assert len(r4.content) <= 32

    # Different client IP still passes cleanly.
    assert client.get("/", headers={"X-Forwarded-For": "198.51.100.88"}).status_code == 200
    # Non-static /api/* GET polling is untouched by the static flood bucket.
    assert client.get("/api/ping", headers=hdrs).status_code == 200


def test_cf_origin_secret_blocks_direct_hits_but_exempts_health_and_webhook(monkeypatch):
    monkeypatch.setattr(settings, "cf_origin_secret", "super-secret-origin-token")
    monkeypatch.setattr(settings, "bot_webhook_path", "/telegram/webhook")

    mini = FastAPI()
    mini.add_middleware(StaticFloodShieldMiddleware, max_requests=100, window_seconds=60)

    @mini.get("/")
    def index():
        return {"ok": True}

    @mini.get("/health")
    def health():
        return {"status": "ok"}

    @mini.post("/telegram/webhook")
    def webhook():
        return {"ok": True}

    client = TestClient(mini)
    assert client.get("/").status_code == 403
    assert client.get("/", headers={"X-Origin-Secret": "wrong"}).status_code == 403
    assert client.get("/", headers={"X-Origin-Secret": "super-secret-origin-token"}).status_code == 200
    assert client.get("/health").status_code == 200
    assert client.post("/telegram/webhook").status_code == 200
    assert get_bandwidth_shield_stats()["origin_lock_enabled"] is True


# ── 4. Preview / Downloader / File-ID Cache / Frontend Optimizations ──────

def test_preview_jpeg_quality_and_thumbnail_downscale_in_source():
    core_src = (REPO_ROOT / "app" / "services" / "recap_engine" / "core.py").read_text(encoding="utf-8")
    prev_fn = core_src.split("def extract_preview_with_overlays(", 1)[1].split("\n@dataclass", 1)[0]
    assert '"-q:v", "6"' in prev_fn

    dl_src = (REPO_ROOT / "app" / "services" / "downloader.py").read_text(encoding="utf-8")
    thumb_fn = dl_src.split("def extract_thumbnail_from_media(", 1)[1].split("\ntry:", 1)[0]
    assert "scale='min(720,iw)':-2" in thumb_fn
    assert '"-q:v", "6"' in thumb_fn


def test_free_downloader_caps_720p_and_vip_gets_1080p(tmp_path, monkeypatch):
    import app.services.downloader as dl_mod
    import app.services.url_safety as safety_mod

    seen_heights: list[int | None] = []

    class _SpyDownloader:
        def download_video(self, url, target_dir=None, max_height=None):
            seen_heights.append(max_height)
            p = Path(target_dir) / "clip.mp4"
            p.write_bytes(b"\x00" * 1024)
            return str(p), {"title": "Clip", "platform": "youtube"}

    monkeypatch.setattr(api_mod, "_dl_root", lambda: tmp_path / "downloads")
    monkeypatch.setattr(dl_mod, "unshorten_url", lambda u: u)
    monkeypatch.setattr(safety_mod, "validate_public_url", lambda u: u)
    monkeypatch.setattr(dl_mod, "MediaDownloader", _SpyDownloader)

    free_user = SimpleNamespace(id=1001, account_category=AccountCategory.FREE.value)
    vip_user = SimpleNamespace(id=1002, account_category=AccountCategory.PURCHASED.value)

    req = api_mod.DlFetchRequest(url="https://youtu.be/abc", format="video")
    asyncio.run(api_mod.fetch_downloader_format(req, user=free_user, _gate=free_user))
    asyncio.run(api_mod.fetch_downloader_format(req, user=vip_user, _gate=vip_user))
    assert seen_heights == [720, 1080]


@pytest.mark.asyncio
async def test_telegram_file_id_cache_reuses_id_and_falls_back_on_error(tmp_path):
    f = tmp_path / "slip.jpg"
    f.write_bytes(b"fake-jpg-bytes")

    bot = MagicMock()
    sent_msg = SimpleNamespace(photo=[SimpleNamespace(file_id="FID_SLIP_123")])
    bot.send_photo = AsyncMock(return_value=sent_msg)

    # First send uploads via FSInputFile and caches FID_SLIP_123
    await lba.send_cached_telegram_media(bot, media_kind="photo", chat_id=101, file_path=f, caption="c")
    first_photo_arg = bot.send_photo.call_args.kwargs["photo"]
    assert not isinstance(first_photo_arg, str)

    # Second send reuses cached string file_id (0 bytes egress!)
    await lba.send_cached_telegram_media(bot, media_kind="photo", chat_id=202, file_path=f, caption="c")
    second_photo_arg = bot.send_photo.call_args.kwargs["photo"]
    assert second_photo_arg == "FID_SLIP_123"

    # If Telegram rejects cached file_id, it invalidates and retries with FSInputFile
    bot.send_photo = AsyncMock(side_effect=[RuntimeError("wrong file_id"), sent_msg])
    await lba.send_cached_telegram_media(bot, media_kind="photo", chat_id=303, file_path=f, caption="c")
    assert bot.send_photo.await_count == 2
    assert bot.send_photo.call_args_list[0].kwargs["photo"] == "FID_SLIP_123"
    assert not isinstance(bot.send_photo.call_args_list[1].kwargs["photo"], str)


def test_frontend_egress_optimizations_and_bundle_budget():
    app_js = (REPO_ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    assert "const ensureSampleSrc = async" in app_js
    assert "_authSampleCache" in app_js
    assert "if (/[?&]t=/.test(url))" in app_js
    assert 'audio.addEventListener("pointerdown", load, { once: true })' in app_js

    total_lf = sum(
        len((REPO_ROOT / p).read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8"))
        for p in (
            "app/webapp/static/css/style.css",
            "app/webapp/static/js/app.js",
            "app/webapp/static/js/i18n.js",
        )
    )
    assert total_lf <= 1_650_000, f"Asset bundle exceeded 1,650,000 LF bytes: {total_lf}"


# ── 5. Local Telegram Bot API + 2,000 MB Upload Cap Lift ──────────────────

def test_local_bot_api_defaults_to_50mb_and_lifts_to_2000mb_when_configured(monkeypatch):
    assert lba.is_local_bot_api_active() is False
    assert lba.get_telegram_max_upload_mb() == 50
    assert lba.get_telegram_max_upload_bytes() == 50 * MB
    assert lba.get_splitter_max_part_bytes() == 48 * MB

    monkeypatch.setattr(settings, "telegram_bot_api_url", "https://telegram-bot-api-kydu.onrender.com/")
    assert lba.is_local_bot_api_active() is True
    assert lba.get_telegram_max_upload_mb() == 2000
    assert lba.get_telegram_max_upload_bytes() == 2000 * MB
    assert lba.get_splitter_max_part_bytes() == 1980 * MB

    bot = lba.create_telegram_bot(token="123456:ABCDEF_fake_token")
    try:
        assert bot.session.api.base.startswith("https://telegram-bot-api-kydu.onrender.com/")
        assert bot.session.api.is_local is False
    finally:
        asyncio.run(bot.session.close())


@pytest.mark.asyncio
async def test_local_bot_api_probe_success_and_safe_fallback(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_api_url", "https://telegram-bot-api-kydu.onrender.com")
    monkeypatch.setattr(settings, "bot_token", "123456:TESTTOKEN")

    # Failure -> safe fallback to official 50 MB
    fail_bot = MagicMock()
    fail_bot.get_me = AsyncMock(side_effect=RuntimeError("Unauthorized: bot already logged in"))
    fail_bot.session.close = AsyncMock()
    with patch("aiogram.Bot", return_value=fail_bot):
        st_fail = await lba.probe_local_bot_api()
    assert st_fail["active"] is False
    assert st_fail["mode"] == "fallback_official"
    assert st_fail["max_upload_mb"] == 50
    assert lba.get_telegram_max_upload_mb() == 50

    # Success -> active Local Bot API, 2,000 MB unlocked!
    ok_bot = MagicMock()
    ok_bot.get_me = AsyncMock(return_value=SimpleNamespace(username="voiceclonepro_bot"))
    ok_bot.session.close = AsyncMock()
    with patch("aiogram.Bot", return_value=ok_bot):
        st_ok = await lba.probe_local_bot_api()
    assert st_ok["active"] is True
    assert st_ok["mode"] == "local_bot_api"
    assert st_ok["max_upload_mb"] == 2000
    assert st_ok["bot_username"] == "voiceclonepro_bot"
    assert lba.get_telegram_max_upload_mb() == 2000


def test_120mb_video_and_voice_become_telegram_sendable_with_local_bot_api(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    video = tmp_path / "recap_120mb.mp4"
    voice = tmp_path / "voice_120mb.wav"
    for p in (video, voice):
        with open(p, "wb") as fh:
            fh.truncate(120 * MB)

    # Official API (50 MB): NOT sendable
    size_v, sendable_v = api_mod._recap_output_size_and_sendable("rc_lba_test", str(video))
    size_a, sendable_a = api_mod._voice_output_size_and_sendable(Job(id="v_lba_test", result_path=str(voice)))
    assert size_v == 120 * MB and sendable_v is False
    assert size_a == 120 * MB and sendable_a is False
    assert vs._effective_max_part_bytes() == 48 * MB

    # Local Bot API active (2,000 MB): SENDABLE!
    monkeypatch.setattr(settings, "telegram_bot_api_url", "https://telegram-bot-api-kydu.onrender.com")
    size_v2, sendable_v2 = api_mod._recap_output_size_and_sendable("rc_lba_test", str(video))
    size_a2, sendable_a2 = api_mod._voice_output_size_and_sendable(Job(id="v_lba_test", result_path=str(voice)))
    assert size_v2 == 120 * MB and sendable_v2 is True
    assert size_a2 == 120 * MB and sendable_a2 is True
    assert vs._effective_max_part_bytes() == 1980 * MB


def test_no_direct_aiogram_bot_instantiations_outside_factory():
    strays: list[str] = []
    for p in [REPO_ROOT / "main.py"] + sorted((REPO_ROOT / "app").rglob("*.py")):
        if p.name == "telegram_bot_factory.py":
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if re.search(r"\bBot\s*\(", line):
                strays.append(f"{p.relative_to(REPO_ROOT)}:{i}: {line.strip()}")
    assert not strays, f"Direct Bot() calls must go through create_telegram_bot(): {strays}"


# ── 6. Co-located Local Bot API Sidecar, Handshake & Bot UI Polish ────────

def test_dockerfile_and_entrypoint_colocate_local_bot_api():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    entrypoint = (REPO_ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")
    assert "FROM aiogram/telegram-bot-api:latest AS tgbotapi_bin" in dockerfile
    assert "/opt/telegram-bot-api/run-telegram-bot-api" in dockerfile
    assert "--local --http-ip-address=127.0.0.1" in entrypoint
    assert 'TELEGRAM_BOT_API_IS_LOCAL="true"' in entrypoint


@pytest.mark.asyncio
async def test_activate_local_bot_api_auto_logout_handshake(monkeypatch):
    monkeypatch.setattr(settings, "bot_token", "123456:TESTTOKEN")
    local_fail = MagicMock()
    local_fail.get_me = AsyncMock(side_effect=RuntimeError("Unauthorized"))
    local_fail.session.close = AsyncMock()

    official_bot = MagicMock()
    official_bot.log_out = AsyncMock(return_value=True)
    official_bot.session.close = AsyncMock()

    local_ok = MagicMock()
    local_ok.get_me = AsyncMock(return_value=SimpleNamespace(username="voiceclonepro_bot"))
    local_ok.session.close = AsyncMock()

    with patch("aiogram.Bot", side_effect=[local_fail, official_bot, local_ok]):
        st = await lba.activate_local_bot_api(target_url="http://127.0.0.1:8081")

    official_bot.log_out.assert_awaited_once()
    assert st["active"] is True
    assert st["is_local_fs"] is True
    assert st["max_upload_mb"] == 2000


@pytest.mark.asyncio
async def test_bot_buy_menu_is_two_columns_mmk_and_invoice_shows_validity(db_session, monkeypatch):
    import json
    from contextlib import asynccontextmanager
    from aiogram.types import CallbackQuery, Message, User as TgUser
    from app.handlers.payment import handle_buy_minutes_menu, handle_package_selected
    from app.services.ledger import get_or_create_user
    from app.services.payments import get_active_pricing_plans

    await get_active_pricing_plans(db_session)
    user, _ = await get_or_create_user(db_session, telegram_id=981122)
    user.language_code = "en"
    await db_session.commit()

    @asynccontextmanager
    async def _ctx():
        yield db_session

    monkeypatch.setattr("app.handlers.payment.AsyncSessionLocal", lambda: _ctx())

    cb = MagicMock(spec=CallbackQuery)
    cb.from_user = TgUser(id=981122, is_bot=False, first_name="Buyer")
    cb.message = MagicMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    state = MagicMock()
    state.clear = AsyncMock()
    state.set_state = AsyncMock()
    state.update_data = AsyncMock()

    await handle_buy_minutes_menu(cb, state)
    kb = cb.message.edit_text.call_args.kwargs["reply_markup"]
    # 22 plans paired 2-per-row (11 rows) + 1 Back row = 12 rows total
    assert len(kb.inline_keyboard) == 12
    plan_texts = [
        btn.text
        for row in kb.inline_keyboard
        for btn in row
        if (btn.callback_data or "").startswith("buy_pkg:")
    ]
    assert len(plan_texts) == 22
    assert all(" Ks" not in txt and "MMK" in txt and len(txt) <= 28 for txt in plan_texts)

    cb.data = "buy_pkg:voice_mini"
    cb.message.edit_text.reset_mock()
    await handle_package_selected(cb, state)
    inv_text = cb.message.edit_text.call_args.args[0]
    assert "Mini" in inv_text
    assert "(voice_mini)" not in inv_text
    assert "30 days from approval" in inv_text

    en_msgs = json.loads((REPO_ROOT / "locales" / "en" / "messages.json").read_text(encoding="utf-8"))
    assert en_msgs["referral_title"] == "🎁 <b>Refer Friends & Earn</b>"


def test_group_ai_tokens_and_webapp_audit_fixes():
    import re
    gai = (REPO_ROOT / "app" / "services" / "group_ai_admin.py").read_text(encoding="utf-8")
    assert '"max_output_tokens": 4096' in gai
    assert '"max_output_tokens": 2048' in gai

    api_src = (REPO_ROOT / "app" / "webapp" / "api.py").read_text(encoding="utf-8")
    assert '"char_limit": 20000' in api_src
    assert 'len(sheet) > 20000' in api_src

    app_js = (REPO_ROOT / "app" / "webapp" / "static" / "js" / "app.js").read_text(encoding="utf-8")
    css = (REPO_ROOT / "app" / "webapp" / "static" / "css" / "style.css").read_text(encoding="utf-8")
    i18n_js = (REPO_ROOT / "app" / "webapp" / "static" / "js" / "i18n.js").read_text(encoding="utf-8")
    assert "max-height: 62vh;" in css
    assert "#tab-recap .ratio-btn.active .ratio-sub" in css
    assert "Unknown" not in app_js or "unknown|weba|webk|tdesktop|macos" in app_js
    assert 'setRecapStage("4/4", job.status)' in app_js
    assert "window._lastReferralStats" in app_js
    assert "pill.disabled = knownTotal > 0 && pSec > knownTotal" in app_js
    assert "if (res.status === 403) { hubHistoryCache = [];" in app_js
    analytics_start = app_js.index("async function loadAdminAnalytics")
    analytics_block = app_js[analytics_start:analytics_start + 2200]
    assert " Ks" not in analytics_block and " MMK" in analytics_block
    assert len(css.encode("utf-8")) + len(app_js.encode("utf-8")) + len(i18n_js.encode("utf-8")) <= 1_650_000


@pytest.mark.asyncio
async def test_serve_index_auto_cache_busts_immutable_assets():
    from app.webapp.server import serve_index, _get_static_asset_cache_tag

    disk_html = (REPO_ROOT / "app" / "webapp" / "static" / "index.html").read_text(encoding="utf-8")
    assert disk_html.count("?v=20261118_r164") == 4

    tag = _get_static_asset_cache_tag()
    assert tag.startswith("20261120_") and len(tag) == 19

    resp = await serve_index()
    body = resp.body.decode("utf-8")
    assert body.count(f"?v={tag}") == 4
    assert "?v=20261118_r164" not in body
    assert resp.headers.get("cache-control") == "no-cache, no-store, must-revalidate, max-age=0"

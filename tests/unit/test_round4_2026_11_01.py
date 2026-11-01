"""Cycle round-4 fixes (live re-inspection after releases 17-20).

Round 3 (cycle-round3-2026-11-01.md) found, with live evidence:
F-A  The blocked-source path (19th F2/F3) never fired for a real
     TikTok IP-block failure ("Your IP address is blocked from
     accessing this post", job rc_1793505308_08cd6b): the shared
     blockedLinkInfo detector missed the whole yt-dlp refusal family
     (IP blocked / login / age gate / region / private-removed), so
     the user got the generic toast and no upload-tab auto-switch.
F-B  The Downloader result card ignored metadata_verified and
     rendered the extract fallback's invented "03:30" as fact (the
     Recap card already had the amber unverified treatment).
F-C  Recap status line showed raw English engine labels ("Smart
     Sync: VoxCPM 2 3/10", doubled "Smart Sync: Smart Sync video
     10/10", "Timestamp translation") in the Burmese UI, with the
     progress fill pinned at 75% for the whole render.
F-D  Info cards showed the broken-image glyph + "Thumbnail" alt when
     the thumbnail URL was empty; the upload flow never populated the
     card image at all.
F-E  Recap completion stacked TWO surfaces: the result card and a
     near-identical floating alert card.
F-F  After an upload cancel, recapUploadPending stayed true and the
     Start guard stayed locked even in URL mode.
F-G  /api/history reported fallback_used=False for recap jobs whose
     completion toast disclosed Edge-fallback segments (it derived
     the flag from the voice-only engine column).
F-I  The recap render blurred a horizontal strip ("tear") across the
     middle of the picture on every frame: detect_source_subtitle_
     bands returns SOURCE-frame fractions but its boxes were merged
     into overlays["blur_boxes"], whose consumers read CANVAS
     fractions — and the detector also accepted a hairline (0.5%
     tall) false-positive group on a subtitle-free cartoon.

F-H was an observation only (free character_limit reads 500 live):
no code change — the code default is 1000 and the live value comes
from the dynamic-settings override. Nothing here pins behaviour
for it beyond the settings default staying untouched.
"""

from __future__ import annotations

import datetime
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.config import settings
from app.models.enums import JobStatus, JobType
from app.models.schema import Job
from app.services.ledger import get_or_create_user
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
API_PY = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")
PIPELINE_PY = (ROOT / "app/services/recap_engine/recap_pipeline.py").read_text(encoding="utf-8")


def _run_node(script_body: str, tmp_name: str) -> dict:
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    tmp = ROOT / "tests/unit" / tmp_name
    try:
        tmp.write_text(script_body, encoding="utf-8")
        res = subprocess.run([node, str(tmp)], capture_output=True, text=True, timeout=60)
    finally:
        tmp.unlink(missing_ok=True)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def _mapper_slice() -> str:
    start = APP_JS.index("function mapErrorToToast")
    end = APP_JS.index("function showStudioInputModal")
    return APP_JS[start:end]


# ── F-A: blockedLinkInfo covers the yt-dlp refusal families ─────────

def _run_blocked_cases() -> dict:
    driver = r"""
const cases = {
  round3_ip_blocked: "ERROR: [TikTok] 7391505677920816389: Your IP address is blocked from accessing this post",
  login_required: "ERROR: [youtube] abc123: Login required",
  age_gate: "ERROR: [youtube] abc123: This video is age-restricted",
  region: "ERROR: [youtube] abc123: The uploader has not made this video available in your country",
  private_vid: "ERROR: [youtube] abc123: This video is private",
  removed: "ERROR: [youtube] abc123: This video has been removed by the uploader",
  unknown: "weird failure",
  quota: "ZeroGPU quota exhausted",
};
const out = { my: {}, en: {} };
for (const lang of ["my", "en"]) {
  currentLang = lang;
  for (const [name, raw] of Object.entries(cases)) {
    out[lang][name + "_info"] = blockedLinkInfo(raw);
    out[lang][name + "_recap"] = mapErrorToToast(raw, "recap");
  }
  out[lang].generic_recap = t("err_recap_failed");
}
console.log(JSON.stringify(out));
"""
    script = (
        "var window = {};\n"
        "var localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };\n"
        + I18N_JS + "\n" + _mapper_slice() + "\n" + driver
    )
    return _run_node(script, "_round4_blocked_tmp.js")


def test_fa_round3_real_error_is_blocked_and_names_tiktok():
    out = _run_blocked_cases()
    for lang in ("my", "en"):
        info = out[lang]["round3_ip_blocked_info"]
        assert info == {"blocked": True, "source": "TikTok"}
        toast = out[lang]["round3_ip_blocked_recap"]
        assert "TikTok" in toast
        assert toast != out[lang]["generic_recap"]


def test_fa_refusal_families_all_blocked():
    out = _run_blocked_cases()
    for name in ("login_required", "age_gate", "region", "private_vid", "removed"):
        for lang in ("my", "en"):
            assert out[lang][name + "_info"]["blocked"] is True, (name, lang)
            assert out[lang][name + "_info"]["source"] == "YouTube"
    # No overreach: unknown errors and engine quota stay unblocked.
    for lang in ("my", "en"):
        assert out[lang]["unknown_info"]["blocked"] is False
        assert out[lang]["quota_info"]["blocked"] is False
        assert out[lang]["unknown_recap"] == out[lang]["generic_recap"]


# ── F-B: downloader extract flags + card treatment ──────────────────

@pytest.mark.asyncio
async def test_fb_extract_payload_flags_metadata_verified(monkeypatch):
    from app.services import downloader as dl_mod
    from app.services import url_safety as url_safety_mod

    monkeypatch.setattr(dl_mod, "unshorten_url", lambda u: u)
    monkeypatch.setattr(url_safety_mod, "validate_public_url", lambda u: u)

    user = api_mod.User(id=880841, first_name="T", username=None,
                         account_category="FREE", language_code="my")
    req = api_mod.ExtractMediaRequest(url="https://www.youtube.com/watch?v=abc123")

    def fake_get_info(self, url):
        return {"duration": 125, "title": "Sample", "platform": "YouTube",
                "uploader": "Chan", "thumbnail": "", "width": 1920,
                "height": 1080, "has_subtitles": False}

    monkeypatch.setattr(dl_mod.MediaDownloader, "get_info", fake_get_info)
    res = await api_mod.extract_media_info(req, user, user)
    assert res["metadata_verified"] is True

    def boom(self, url):
        raise RuntimeError("ERROR: [youtube] abc123: Sign in to confirm")

    monkeypatch.setattr(dl_mod.MediaDownloader, "get_info", boom)
    res = await api_mod.extract_media_info(req, user, user)
    assert res["metadata_verified"] is False
    # The fallback payload is exactly what the client must not show
    # as fact: placeholder title/uploader + invented duration.
    assert res["estimated_duration"] == "03:30"


def test_fb_downloader_card_applies_unverified_treatment():
    start = APP_JS.index("function setupDownloader")
    end = APP_JS.index("function switchPlanCategory")
    dl = APP_JS[start:end]
    assert "data.metadata_verified === false" in dl
    assert "dl-result-duration" in dl
    assert "recap_unverified_note" in dl


# ── F-C: stage mapping, honest monotonic percent, localized line ────

def test_fc_stage_mapping_core_labels():
    f = api_mod._recap_stage_progress
    stage, sub, pct, step = f("Smart Sync: VoxCPM 2 3/10", 15)
    assert (stage, sub, step) == ("tts", "3/10", "3/4")
    assert pct == 69  # 64 + 0.3 * (82-64)

    stage, sub, pct, step = f("Smart Sync: Smart Sync video 10/10", 69)
    assert (stage, sub, step) == ("sync", "10/10", "3/4")
    assert pct == 95

    assert f("Timestamp translation", 30)[0] == "translate"
    assert f("Story context memory", 15)[0] == "context"
    assert f("Speech timestamp transcription", 15)[0] == "transcribe"
    assert f("Build Smart Sync plan", 54)[0] == "plan"
    assert f("Plan ready", 54) == ("plan", "", 64, "2/4")
    assert f("🗣️ [3/4] VoxCPM 2 Cloud Voice & Smart Sync Render...", 54)[0] == "tts"
    assert f("Smart Sync: Audio 4/10", 64)[0] == "tts"
    assert f("Video 2/10", 82)[0] == "sync"
    assert f("✅ Final Recap Video Ready!", 95)[0] == "finalize"


def test_fc_unmapped_labels_keep_previous_state():
    stage, sub, pct, step = api_mod._recap_stage_progress(
        "📥 Downloading source video from URL...", 15
    )
    assert stage is None and pct == 15


def test_fc_percent_is_monotonic_and_never_pinned_at_75():
    f = api_mod._recap_stage_progress
    pct = 15
    seen = []
    for label in (
        "Speech timestamp transcription",
        "Story context memory",
        "Timestamp translation",
        "Smart Sync: VoxCPM 2 1/10",
        "Smart Sync: VoxCPM 2 9/10",
        "Smart Sync: Smart Sync video 1/10",
        "Smart Sync: Smart Sync video 10/10",
    ):
        _, _, pct, _ = f(label, pct)
        seen.append(pct)
    assert seen == sorted(seen)  # never moves backwards
    assert seen[-1] > 75  # the render tail escapes the old 75% pin
    # A late low-fraction report can never drag the bar back down.
    assert f("Smart Sync: VoxCPM 2 1/10", 90)[2] == 90
    # The old coarse formula is gone from the runner.
    assert "(cur / max(1, tot)) * 100" not in API_PY


def test_fc_clean_label_dedupes_smart_sync_prefix():
    f = api_mod._recap_clean_progress_label
    assert f("Smart Sync: Smart Sync video 10/10") == "Smart Sync video 10/10"
    assert f("Smart Sync: VoxCPM 2 3/10") == "Smart Sync: VoxCPM 2 3/10"
    assert f("Timestamp translation") == "Timestamp translation"


def test_fc_status_payload_and_client_rendering():
    assert '"progress_stage": entry.get("progress_stage")' in API_PY
    assert '"progress_sub": entry.get("progress_sub", "")' in API_PY
    assert 'data.progress_stage' in APP_JS
    assert '"recap_stage_" + data.progress_stage' in APP_JS
    for key in (
        "recap_stage_transcribe", "recap_stage_context", "recap_stage_translate",
        "recap_stage_plan", "recap_stage_tts", "recap_stage_sync",
        "recap_stage_finalize",
    ):
        # Exactly twice: once in the my dict, once in the en dict.
        assert I18N_JS.count(key + ":") == 2, key


# ── F-D: thumbnails never render the broken glyph ───────────────────

def test_fd_card_thumb_helper_and_sites():
    assert "function setCardThumb(img, url)" in APP_JS
    assert "window.setCardThumb = setCardThumb" in APP_JS
    # Call sites: recap analyze, downloader card, upload pick,
    # upload success.
    assert APP_JS.count("setCardThumb(") >= 5
    assert 'setCardThumb(thumbImg, data.thumbnail || null)' in APP_JS
    assert 'setCardThumb(dlThumb, data.thumbnail || null)' in APP_JS
    assert 'setCardThumb(document.getElementById("recap-info-thumb"), state.recapThumbnailUrl)' in APP_JS


def test_fd_html_has_no_empty_src_thumbnail():
    assert 'alt="Thumbnail"' not in INDEX_HTML
    assert 'id="recap-info-thumb" src=' not in INDEX_HTML
    assert 'id="dl-result-thumb" class="dl-thumb" src=' not in INDEX_HTML
    assert 'id="recap-info-thumb" alt=""' in INDEX_HTML


# ── F-E: one completion surface ─────────────────────────────────────

def test_fe_recap_success_has_no_floating_alert():
    start = APP_JS.index("function startRecapJobPolling")
    end = APP_JS.index("async function openRecapScriptEditor")
    poller = APP_JS[start:end]
    assert "showFloatingAlert" not in poller
    # The one surviving alert_recap_ready use is the cold-start
    # restore path, where the alert is the only completion signal
    # (the user may be on any tab) — not a duplicate of a visible card.
    assert APP_JS.count("alert_recap_ready") == 1
    # Voice completion alerts are a different flow — untouched.
    assert APP_JS.count("showFloatingAlert(") >= 3


# ── F-F: cancel/error clear the upload-pending flag ─────────────────

def test_ff_pending_cleared_on_abort_and_error():
    start = APP_JS.index("xhr.onabort")
    end = APP_JS.index('formData.append("video_file", file)')
    assert "state.recapUploadPending = false" in APP_JS[start:end]

    start = APP_JS.index("function onRecapUploadError")
    end = APP_JS.index("async function onRecapUploadSuccess")
    assert "state.recapUploadPending = false" in APP_JS[start:end]


# ── F-G: history reports the disk-truth fallback for recap jobs ─────

@pytest.fixture
def storage(tmp_path, monkeypatch):
    root = tmp_path / "storage"
    root.mkdir()
    monkeypatch.setattr(settings, "storage_root", root)
    for jid in ("rc_r4_fb1", "rc_r4_fb2"):
        api_mod.RECAP_JOBS_REGISTRY.pop(jid, None)
    return root


def _job(job_id, user_id, *, job_type, engine, status=JobStatus.SUCCESS.value):
    return Job(
        id=job_id,
        user_id=user_id,
        status=status,
        job_type=job_type,
        engine=engine,
        preset_voice="komin",
        input_text="https://example.com/source",
        created_at=datetime.datetime.now(datetime.timezone.utc),
    )


@pytest.mark.asyncio
async def test_fg_history_recap_fallback_comes_from_plan_json(db_session, storage):
    user, _ = await get_or_create_user(db_session, telegram_id=770401, first_name="R4")

    plan_dir = storage / "recap_output" / "rc_r4_fb1"
    plan_dir.mkdir(parents=True)
    (plan_dir / "smart_sync_plan.json").write_text(
        json.dumps({"fallback_used": True, "fallback_segments": [2, 3]}),
        encoding="utf-8",
    )
    clean_dir = storage / "recap_output" / "rc_r4_fb2"
    clean_dir.mkdir(parents=True)
    (clean_dir / "smart_sync_plan.json").write_text(
        json.dumps({"fallback_used": False, "fallback_segments": []}),
        encoding="utf-8",
    )

    db_session.add_all([
        # Recap job whose row engine is NOT edge_fallback, yet 2 of its
        # segments fell back — history must not claim False.
        _job("rc_r4_fb1", user.id, job_type=JobType.RECAP.value, engine="recap"),
        _job("rc_r4_fb2", user.id, job_type=JobType.RECAP.value, engine="recap"),
        # Voice semantics unchanged.
        _job("vc_r4_fb3", user.id, job_type=JobType.VOICE_CLONE.value, engine="edge_fallback"),
        _job("vc_r4_fb4", user.id, job_type=JobType.VOICE_CLONE.value, engine="voxcpm"),
    ])
    await db_session.commit()

    hist = await api_mod.get_user_history(user=user, db=db_session)
    by_id = {h["id"]: h for h in hist["history"]}
    assert by_id["rc_r4_fb1"]["fallback_used"] is True
    assert by_id["rc_r4_fb1"]["fallback_segments"] == [2, 3]
    assert by_id["rc_r4_fb2"]["fallback_used"] is False
    assert by_id["vc_r4_fb3"]["fallback_used"] is True
    assert by_id["vc_r4_fb4"]["fallback_used"] is False


# ── F-I: source-band boxes are mapped into canvas space ─────────────

def test_fi_band_conversion_uses_picture_rect():
    from app.services.recap_engine.core import frame_layout, source_band_to_canvas_box

    layout = frame_layout("9:16", "blur", 640, 480)
    assert (layout["cw"], layout["ch"]) == (1080, 1920)
    assert (layout["pw"], layout["ph"]) == (1080, 810)
    oy = layout["oy"]

    # A genuine subtitle band at source y=0.70, h=0.10 must land ON the
    # picture: centre oy + 0.70*810 canvas px, height 81 px.
    box = source_band_to_canvas_box({"x": 0.0, "y": 0.70, "w": 1.0, "h": 0.10}, layout)
    assert box["x"] == 0.0 and box["w"] == 1.0
    assert box["y"] == round((oy + 0.70 * 810) / 1920, 4)
    assert box["h"] == round(81 / 1920, 4)
    # The old pass-through would have put it at 0.70 (canvas y=1344).
    assert box["y"] != 0.70


def test_fi_band_conversion_is_identity_for_full_frame_layout():
    from app.services.recap_engine.core import frame_layout, source_band_to_canvas_box

    layout = frame_layout("16:9", "crop", 1920, 1080)
    box = source_band_to_canvas_box({"x": 0.0, "y": 0.70, "w": 1.0, "h": 0.10}, layout)
    assert box == {"x": 0.0, "y": 0.7, "w": 1.0, "h": 0.1}


def test_fi_pipeline_converts_detected_bands_before_merging():
    assert "source_band_to_canvas_box(band, _band_layout)" in PIPELINE_PY
    assert "probe_video_size(ffprobe_bin, source)" in PIPELINE_PY


def _synthetic_clip(tmp_path: Path, name: str, band_rows) -> Path:
    """A dark clip whose given rows carry bright glyph-like stripes
    (bright strokes with dark neighbours — the detector's text signal),
    persistent in every frame."""
    from PIL import Image

    img = Image.new("L", (320, 240), 40)
    px = img.load()
    for y in band_rows:
        for x in range(320):
            px[x, y] = 235 if (x // 3) % 2 == 0 else 25
    png = tmp_path / f"{name}.png"
    img.save(png)
    out = tmp_path / f"{name}.mp4"
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg not available")
    res = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-loop", "1", "-i", str(png),
         "-t", "6", "-pix_fmt", "yuv420p", "-r", "10", str(out)],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    return out


def test_fi_detector_rejects_hairline_and_keeps_real_band(tmp_path):
    from app.services.recap_engine.core import detect_source_subtitle_bands
    from app.services.recap_engine.recap_pipeline import find_binary

    ffmpeg, ffprobe = find_binary("ffmpeg"), find_binary("ffprobe")

    hairline = _synthetic_clip(tmp_path, "hairline", band_rows=(115, 116))
    boxes = detect_source_subtitle_bands(
        ffmpeg, ffprobe, hairline, tmp_path / "w1", log=lambda m: None
    )
    assert boxes == []

    real = _synthetic_clip(tmp_path, "realband", band_rows=tuple(range(163, 183)))
    boxes = detect_source_subtitle_bands(
        ffmpeg, ffprobe, real, tmp_path / "w2", log=lambda m: None
    )
    assert len(boxes) == 1
    assert 0.60 <= boxes[0]["y"] <= 0.78
    assert boxes[0]["h"] <= 0.161

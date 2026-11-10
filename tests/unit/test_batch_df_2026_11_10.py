"""Batch DF (2026-11-10) — no-audio recap sources fail LOUDLY and EARLY.

Incident: a new FREE user uploaded a 46 s video with NO audio track
and submitted it four times in six minutes (jobs rc_1794268862_191c9e,
rc_1794268920_1a002e, rc_1794268970_861c18, rc_1794269221_99eeab).
Every job was charged, died in ~1 s when the planner's Whisper-prep
ffmpeg hit "-map 0:a:0" on a file with no audio stream ("Stream map
'' matches no streams."), hid behind the generic technical-error
message, and refunded. The user had no way to learn the file itself
was the problem.

DF layers:
1. classify_source_error maps the ffmpeg no-stream signature to the
   specific NO_AUDIO_TRACK_MESSAGE (bot notice + app job detail).
2. POST /recap/upload_video surfaces the probe's has_audio flag.
3. create_recap_job refuses a positively-probed no-audio local
   upload BEFORE any credit moves, with the same message.
4. The Mini App warns on the upload card, locks the CTA, and
   handleStartRecap refuses while the flag is set (i18n my + en).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
DOWNLOADER = (ROOT / "app/services/downloader.py").read_text(encoding="utf-8")


# ---------- layer 1: classifier (behavioural) ----------

def test_df_classifier_maps_ffmpeg_no_stream_error():
    from app.services.downloader import NO_AUDIO_TRACK_MESSAGE, classify_source_error

    sample = (
        "Whisper audio preparation failed:\n"
        "Stream map '' matches no streams.\n"
        "To ignore this, add a trailing '?' to the map.\n"
        "Failed to set value '0:a:0' for option 'map': Invalid argument\n"
        "Error parsing options for output file "
        "/app/storage/recap_output/rc_x/workspace/_whisper_audio_16k.wav."
    )
    assert classify_source_error(sample, "/app/storage/temp/recap_uploads/up_x.mp4") == NO_AUDIO_TRACK_MESSAGE
    assert NO_AUDIO_TRACK_MESSAGE.startswith("⚠️")
    assert "အသံ လုံးဝ မပါဝင်ပါ" in NO_AUDIO_TRACK_MESSAGE


def test_df_classifier_leaves_unrelated_errors_alone():
    from app.services.downloader import NO_AUDIO_TRACK_MESSAGE, classify_source_error

    assert classify_source_error("some totally unknown failure", "https://example.com/v.mp4") != NO_AUDIO_TRACK_MESSAGE


# ---------- layer 2: upload response ----------

def test_df_upload_response_surfaces_has_audio():
    assert '"has_audio": bool(meta.get("has_audio"))' in API


# ---------- layer 3: submit guard before the charge ----------

def test_df_submit_guard_precedes_credit_read():
    guard = API.index("NO_AUDIO_TRACK_MESSAGE, probe_local_media")
    credits = API.index('credits = getattr(user, "recap_credits", 0) or 0')
    assert guard < credits
    assert 'if _df_meta.get("has_video") and not _df_meta.get("has_audio"):' in API
    assert "raise HTTPException(status_code=400, detail=NO_AUDIO_TRACK_MESSAGE)" in API
    # Only LOCAL sources are probed — URL sources keep the classifier net.
    assert 'not _req_url.startswith(("http://", "https://"))' in API[guard - 400:guard]


# ---------- layer 4: Mini App ----------

def test_df_app_records_and_blocks_no_audio_uploads():
    assert "state.recapNoAudio = data.has_audio === false;" in APP_JS
    assert 't("recap_no_audio_tag")' in APP_JS
    assert "if (state.recapNoAudio) {" in APP_JS
    # URL mode clears the file's verdict.
    assert "state.recapNoAudio = false;" in APP_JS


def test_df_i18n_keys_complete_both_languages():
    assert APP_JS.count('t("recap_no_audio")') >= 2  # toast on paint + submit guard
    for key in ("recap_no_audio:", "recap_no_audio_tag:"):
        assert I18N.count(key) == 2, key  # my + en


def test_df_cache_token_is_r130():
    assert INDEX.count("20261104_r130") == 4
    assert "20261104_r127" not in INDEX

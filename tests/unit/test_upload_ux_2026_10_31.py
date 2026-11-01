"""Upload / waiting-UX pass (branch fix-upload-ux-2026-10-31).

Pins for the premium waiting-experience upgrades:
- U1 Recap file upload uses XHR with REAL progress events — the old
  hardcoded frozen "Uploading to Cloud Studio... 40%" (fetch, no
  progress events possible) is gone;
- U2 honest phases (uploading % -> server "preparing" indeterminate),
  elapsed clock, size hint, CANCEL (abort) and inline RETRY reusing
  the already-picked file; same-file re-pick works (input reset);
- U3 upload motion CSS (sheen while busy/indeterminate, error state);
- U4 i18n keys for every new string, in BOTH locales;
- U5 the other silent waits got timeouts / in-flight labels: voice
  submit, recap URL inspect, downloader extract+fetch, slip submit;
- U6 voice polling surfaces repeated failures ("reconnecting") and
  closes cleanly at its cap instead of leaving a frozen box;
- U7 sample preview shows a loading state immediately and reports
  failures instead of silence;
- U8 the upload endpoint runs its blocking ffprobe/ffmpeg work off
  the event loop.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "app/webapp/static/js/app.js").read_text(encoding="utf-8")
I18N_JS = (ROOT / "app/webapp/static/js/i18n.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "app/webapp/static/index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "app/webapp/static/css/style.css").read_text(encoding="utf-8")
API_PY = (ROOT / "app/webapp/api.py").read_text(encoding="utf-8")

NEW_KEYS = [
    "upload_progress_label", "upload_processing_label", "upload_ready_label",
    "upload_expect_hint", "upload_cancel_btn", "upload_retry_btn",
    "upload_cancelled_label", "upload_slow_hint", "upload_error_hint",
    "voice_synthesizing", "voice_reconnecting", "voice_wait_timeout",
    "slip_sending", "preview_loading", "preview_load_failed",
    "recap_info_uploading", "recap_info_upload_failed", "recap_info_ready",
    "toast_recap_not_uploaded",
]


def _locale_block(lang: str) -> str:
    marker = "    " + lang + ": {"
    start = I18N_JS.index(marker) + len(marker)
    end = I18N_JS.index("\n    }", start)
    return I18N_JS[start:end]


def _keys(lang: str) -> set[str]:
    return set(re.findall(r"(?m)^        (\w+):", _locale_block(lang)))


# ── U1: real progress, fake 40% gone ───────────────────────────────────

def test_upload_uses_xhr_real_progress():
    assert "new XMLHttpRequest()" in APP_JS
    assert "xhr.upload.onprogress" in APP_JS
    assert "lengthComputable" in APP_JS
    # The frozen fake progress is gone for good.
    assert "Uploading to Cloud Studio... 40%" not in APP_JS
    assert 'progressBar.style.width = "40%"' not in APP_JS
    assert "✅ Video Ready for 1080p Recap!" not in APP_JS


def test_upload_no_longer_posts_via_fetch():
    # The recap upload endpoint is only ever called by the XHR path.
    assert APP_JS.count("/api/recap/upload_video") == 1
    assert 'xhr.open("POST", "/api/recap/upload_video")' in APP_JS


# ── U2: phases / cancel / retry / elapsed ──────────────────────────────

def test_upload_phases_and_controls_wired():
    for el_id in ("recap-upload-progress", "recap-upload-bar",
                  "recap-upload-label", "recap-upload-elapsed",
                  "recap-upload-hint", "recap-upload-cancel",
                  "recap-upload-error", "recap-upload-error-text",
                  "recap-upload-retry"):
        assert f'id="{el_id}"' in INDEX_HTML, el_id
    assert "function startRecapUpload" in APP_JS
    assert "function onRecapUploadError" in APP_JS
    assert "function onRecapUploadSuccess" in APP_JS
    assert 't("upload_processing_label")' in APP_JS
    assert 'tf("upload_progress_label"' in APP_JS
    assert 'tf("upload_expect_hint"' in APP_JS
    assert 't("upload_cancelled_label")' in APP_JS
    assert 't("upload_slow_hint")' in APP_JS
    # Cancel aborts the live XHR; retry re-sends the picked file.
    assert "recapUploadXhr.abort()" in APP_JS
    assert "state.recapUploadFile" in APP_JS
    assert "startRecapUpload(state.recapUploadFile)" in APP_JS
    # Same-file re-pick after failure must fire "change" again.
    assert 'e.target.value = "";' in APP_JS
    # Server error details still go through the safe toast mapper.
    assert 'mapErrorToToast(data.detail' in APP_JS


# ── U3: motion CSS ─────────────────────────────────────────────────────

def test_upload_motion_css():
    assert "@keyframes uploadSheen" in STYLE_CSS
    assert ".progress-bar-fill.busy::after" in STYLE_CSS
    assert ".progress-bar-fill.indeterminate::after" in STYLE_CSS
    assert ".progress-bar-fill.error" in STYLE_CSS
    assert ".upload-error-box" in STYLE_CSS
    assert ".upload-elapsed" in STYLE_CSS


# ── U4: i18n keys in both locales ──────────────────────────────────────

def test_upload_ux_i18n_keys_both_locales():
    my, en = _keys("my"), _keys("en")
    for key in NEW_KEYS:
        assert key in my, f"{key} missing in my"
        assert key in en, f"{key} missing in en"


# ── U5: timeouts / in-flight labels on the other silent waits ──────────

def test_other_waits_have_timeouts():
    assert APP_JS.count('fetchWithTimeout("/api/downloader/extract"') == 2
    assert 'fetchWithTimeout("/api/downloader/fetch"' in APP_JS
    assert 'fetchWithTimeout("/api/vip/submit-slip"' in APP_JS
    assert 'fetchWithTimeout("/api/voices/generate"' in APP_JS
    assert 'fetchWithTimeout("/api/voices/generate-with-audio"' in APP_JS
    assert 't("slip_sending")' in APP_JS
    assert 't("recap_preview_loading")' in APP_JS  # downloader extract button


# ── U6: voice poll honesty ─────────────────────────────────────────────

def test_voice_poll_failure_honesty():
    assert "function startJobPolling" in APP_JS
    assert "registerPollFailure" in APP_JS
    assert "stopPollingTimedOut" in APP_JS
    assert 't("voice_reconnecting")' in APP_JS
    assert 't("voice_wait_timeout")' in APP_JS
    assert 't("voice_synthesizing")' in APP_JS
    # The RUNNING status line is i18n now, not a hardcoded Burmese string.
    assert "အသံထွက်ပေါ်အောင် ပေါင်းစပ်နေပါသည်" not in APP_JS


# ── U7: sample preview loading state ───────────────────────────────────

def test_sample_preview_loading_state():
    assert 't("preview_loading")' in APP_JS
    assert 't("preview_load_failed")' in APP_JS


# ── U8: server probe off the event loop ────────────────────────────────

def test_upload_probe_off_event_loop():
    assert "asyncio.to_thread(_probe_and_thumb)" in API_PY
    # The response contract of the endpoint is unchanged.
    assert '"local_path": str(saved_path)' in API_PY
    assert '"duration_seconds": int(round(dur_sec))' in API_PY


# ── U9: the info card never claims Ready before the server confirms ────
# Owner-caught defect (2026-11-01): in the upload ERROR state the file
# card below still showed "1:35 (Ready)" / "Ready for 1080p Recap" from
# an earlier successful upload — a stale Ready that contradicts the
# failure and implies a recap could start on a file the server never
# received. The card is now state-driven and the CTA is guarded.

def test_info_card_upload_state_machine():
    assert "function setRecapInfoUploadState" in APP_JS
    assert 'setRecapInfoUploadState("pending")' in APP_JS
    assert 'setRecapInfoUploadState("failed")' in APP_JS
    assert 'setRecapInfoUploadState("ready")' in APP_JS
    assert 't("recap_info_uploading")' in APP_JS
    assert 't("recap_info_upload_failed")' in APP_JS
    assert 't("recap_info_ready")' in APP_JS
    # (Re)starting an upload drops the card to the pending state, so a
    # retry never displays the previous outcome while it is in flight.
    start = APP_JS.index("function startRecapUpload")
    seg = APP_JS[start:start + 3000]
    assert 'setRecapInfoUploadState("pending")' in seg


def test_upload_error_clears_ready_and_keeps_card():
    err = APP_JS.index("function onRecapUploadError")
    seg = APP_JS[err:err + 1600]
    # The card stays visible (keepInfoCard) but in the failed state,
    # next to the error box — both tell the same story.
    assert "resetRecapUploadPreview(true)" in seg
    assert 'setRecapInfoUploadState("failed")' in seg
    assert "keepInfoCard" in APP_JS
    # The failed state clears the duration chip (no stale "(Ready)").
    fn = APP_JS.index("function setRecapInfoUploadState")
    body = APP_JS[fn:fn + 1800]
    assert '"⏱️ —"' in body
    assert 't("recap_info_upload_failed")' in body


def test_upload_success_restores_ready():
    ok = APP_JS.index("async function onRecapUploadSuccess")
    seg = APP_JS[ok:ok + 6000]
    assert 'setRecapInfoUploadState("ready")' in seg
    assert "state.recapUploadPending = false" in seg
    # The pin-restored adjacency is untouched: the ready label line is
    # still immediately followed by the preview-state call.
    assert 't("upload_ready_label");\n        setRecapPreviewState("ready");' in APP_JS


def test_new_pick_clears_stale_source_and_cta_is_guarded():
    pick = APP_JS.index("async function handleVideoFileSelect")
    seg = APP_JS[pick:pick + 3200]
    # A new pick drops the previous file's confirmed server path and
    # marks the pick unconfirmed BEFORE the upload starts.
    assert "state.uploadedVideoPath = null" in seg
    assert "state.recapUploadPending = true" in seg
    assert seg.index("state.recapUploadPending = true") < seg.index("startRecapUpload(file)")
    # The 1-Click Recap CTA hard-guards on the unconfirmed pick.
    start = APP_JS.index("async function handleStartRecap")
    seg2 = APP_JS[start:start + 1600]
    assert "if (state.recapUploadPending)" in seg2
    assert 't("toast_recap_not_uploaded")' in seg2
    # A successful URL analyze confirms a (URL) source and clears the
    # file-upload pending flag, so link recaps are never blocked.
    ana = APP_JS.index("state.uploadedVideoPath = null;\n                state.recapUploadPending = false;")
    assert ana > 0

"""Batch V (2026-11-04) — classifier needles for Whisper-stage errors.

Companion to the image fix (test_whisper_model_image_batch_v):
even with the model baked in, a local-Whisper failure must never
again reach the user as the generic technical-error message.

- The model-download RuntimeError ("Could not download the Whisper
  model from huggingface.co …") and the backend-missing RuntimeError
  ("Local Whisper backend မတွေ့ပါ …") map to a transient busy message.
- NoUsableSpeechError codes (NO_USABLE_SPEECH /
  MUSIC_ONLY_OR_TRANSCRIPT_TOO_THIN) map to a content message: the
  source audio has no usable speech.

This commit is deliberately independent of the Dockerfile commit —
it can be dropped without affecting the image fix.
"""

from __future__ import annotations

from app.services.downloader import classify_source_error

URL = "https://www.youtube.com/watch?v=GlunZpix30I"

_DOWNLOAD_ERR = (
    "Could not download the Whisper model from huggingface.co "
    "(HTTPSConnectionPool(host='huggingface.co', port=443): Max retries "
    "exceeded). Check the internet. If huggingface.co does not open on "
    "this PC, use a VPN, or put HF_ENDPOINT=https://hf-mirror.com in "
    "the .env file and start again."
)
_BACKEND_ERR = (
    "Local Whisper backend မတွေ့ပါ။ faster-whisper သို့မဟုတ် "
    "openai-whisper install/config ကိုစစ်ပါ။"
)


def test_whisper_model_download_failure_maps_to_busy_message():
    msg = classify_source_error(_DOWNLOAD_ERR, URL)
    assert msg is not None
    assert msg.startswith("⚠️")
    assert "စာသားပြောင်းစနစ်" in msg
    assert "အလုပ်များနေပါသည်" in msg


def test_whisper_backend_missing_maps_to_same_busy_message():
    msg = classify_source_error(_BACKEND_ERR, URL)
    assert msg == classify_source_error(_DOWNLOAD_ERR, URL)


def test_no_usable_speech_maps_to_content_message():
    for err in (
        "NO_USABLE_SPEECH: ffprobe found no audio stream in source media.",
        "NO_USABLE_SPEECH: Whisper returned 0 usable transcript segments after fallback.",
        "MUSIC_ONLY_OR_TRANSCRIPT_TOO_THIN: low_confidence_fallback_transcript",
    ):
        msg = classify_source_error(err, URL)
        assert msg is not None, err
        assert msg.startswith("⚠️")
        assert "စကားပြောသံ" in msg
        # Distinct from the transient busy message: retrying the same
        # source is not the remedy here.
        assert msg != classify_source_error(_DOWNLOAD_ERR, URL)


def test_whisper_needles_do_not_overreach():
    # Vague whisper/download wording without the exceptions' own
    # distinctive openings must stay unclassified (callers keep the
    # generic fallback), exactly like the other narrow needles.
    assert (
        classify_source_error(
            "Whisper transcription failed for an unknown reason", URL
        )
        is None
    )
    assert (
        classify_source_error("Could not download the file from the host", URL)
        is None
    )

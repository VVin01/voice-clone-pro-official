"""PO Token forced-mint experiment (owner-approved 2026-11-02) —
CONCLUDED and REVERTED in the R40 release merge.

History: the sidecar's first prod proof failed on all 3 attempts
with NO /get_pot or mint activity visible in the app logs. The
fetch policy is yt-dlp core's — YoutubeIE._fetch_po_token()
(yt_dlp/extractor/youtube/_video.py) reads the 'youtube' extractor
arg 'fetch_pot'; under the default 'auto' it fetches only when the
request is pre-marked required, which never happened on the walled
path. PR #51 therefore set fetch_pot='always' on every YouTube
attempt (via _pot_extractor_args / _with_pot, the ONE wiring point)
and attached _PotLogShim so provider fetches surface in the app
logs. Hammock's prod proof of that build: fetches fired, mints
completed (zero fetch errors), and YouTube STILL refused Render's
egress IP at the player-response stage on every attempt — the
PO-token route is exhausted and 'always' only added latency to
walled failures.

Disposition (this file's current pins): the fetch policy is
REVERTED — no fetch_pot arg is set anywhere, so core's default
'auto' applies — while _PotLogShim and the provider base_url
wiring are kept exactly as-is. The policy tests below now pin the
REVERTED state (their names are kept from the experiment for
continuity); the shim tests are unchanged from the experiment.
"""

from __future__ import annotations

import logging

import pytest

import app.services.downloader as dl_mod
from app.services.downloader import MediaDownloader

YOUTUBE_URL = "https://www.youtube.com/watch?v=L3tsYC5OYhQ"
SHORTS_URL = "https://www.youtube.com/shorts/g2NWf7CVy3U"
TIKTOK_URL = "https://www.tiktok.com/@someone/video/7391505677920816389"

FETCH_LINE = (
    "[pot:bgutil:http] Generating a player PO Token for web client "
    "via bgutil HTTP server"
)


def _plans(dl, url, stage="info"):
    base = dl._get_base_opts(url)
    return list(dl._attempt_plans(base, url, stage=stage))


def test_fetch_pot_always_on_every_youtube_attempt(monkeypatch, tmp_path):
    # Name kept from the experiment; the pin is now the REVERTED
    # state: NO fetch_pot arg on any YouTube attempt, either stage —
    # yt-dlp core's default 'auto' policy applies.
    monkeypatch.delenv("POT_SERVER_URL", raising=False)
    dl = MediaDownloader(download_dir=str(tmp_path))
    for stage in ("info", "media"):
        plans = _plans(dl, YOUTUBE_URL, stage=stage)
        assert [label for label, _ in plans] == [
            "web_embedded",
            "default",
            "android",
            "mweb",
        ]
        for _label, opts in plans:
            yt_args = opts["extractor_args"].get("youtube") or {}
            assert "fetch_pot" not in yt_args


def test_fetch_pot_merges_with_player_client(monkeypatch, tmp_path):
    monkeypatch.delenv("POT_SERVER_URL", raising=False)
    dl = MediaDownloader(download_dir=str(tmp_path))
    plans = dict(_plans(dl, SHORTS_URL))
    # _with_pot must not disturb the 'youtube' args _with_client
    # set: after the revert they hold player_client ONLY (during
    # the experiment they additionally held fetch_pot=['always']).
    assert plans["web_embedded"]["extractor_args"]["youtube"] == {
        "player_client": ["web_embedded"],
    }
    assert plans["android"]["extractor_args"]["youtube"] == {
        "player_client": ["android"],
    }
    assert plans["mweb"]["extractor_args"]["youtube"] == {
        "player_client": ["mweb"],
    }


def test_log_shim_attached_to_youtube_attempts_only(monkeypatch, tmp_path):
    monkeypatch.delenv("POT_SERVER_URL", raising=False)
    dl = MediaDownloader(download_dir=str(tmp_path))
    for _label, opts in _plans(dl, YOUTUBE_URL):
        assert opts["logger"] is dl_mod._POT_LOG_SHIM
    base = dl._get_base_opts(TIKTOK_URL)
    plans = list(dl._attempt_plans(base, TIKTOK_URL, stage="info"))
    assert "logger" not in plans[0][1]
    assert "youtube" not in (plans[0][1].get("extractor_args") or {})


def test_ytdlp_core_resolves_always_from_attempt_opts(monkeypatch, tmp_path):
    """Behavioural consumption pin: build the exact opts the app
    hands YoutubeDL, then resolve fetch_pot through the same
    _configuration_arg('fetch_pot', [''], ie_key=YoutubeIE) call that
    YoutubeIE._fetch_po_token makes in yt-dlp 2026.10.26. Name kept
    from the experiment; the pin is now the REVERTED state: the arg
    is unset (''), which _fetch_po_token coerces to the default
    'auto' policy."""
    import yt_dlp
    from yt_dlp.extractor.youtube._video import YoutubeIE

    monkeypatch.delenv("POT_SERVER_URL", raising=False)
    dl = MediaDownloader(download_dir=str(tmp_path))
    _label, opts = _plans(dl, YOUTUBE_URL)[0]
    with yt_dlp.YoutubeDL(opts) as ydl:
        ie = YoutubeIE(ydl)
        assert (
            ie._configuration_arg("fetch_pot", [""], ie_key=YoutubeIE)[0]
            == ""
        )


def test_shim_forwards_pot_lines_drops_chatter(caplog):
    shim = dl_mod._POT_LOG_SHIM
    with caplog.at_level(logging.DEBUG, logger=dl_mod.__name__):
        shim.debug(FETCH_LINE)
        shim.debug("[youtube] L3tsYC5OYhQ: Downloading webpage")
        shim.info("[pot:bgutil:http] Generating POT via HTTP server")
        shim.info("Downloading API JSON")
    messages = [r.getMessage() for r in caplog.records]
    assert any(FETCH_LINE in m for m in messages)
    assert any("Generating POT via HTTP server" in m for m in messages)
    assert not any("Downloading webpage" in m for m in messages)
    assert not any("Downloading API JSON" in m for m in messages)


def test_shim_forwards_warnings_and_errors(caplog):
    shim = dl_mod._POT_LOG_SHIM
    with caplog.at_level(logging.DEBUG, logger=dl_mod.__name__):
        shim.warning("[pot:bgutil:http] Error reaching GET /ping")
        shim.error("something failed")
    levels = {(r.levelno, r.getMessage()) for r in caplog.records}
    assert any(lvl == logging.WARNING and "Error reaching" in m for lvl, m in levels)
    assert any(lvl == logging.ERROR and "something failed" in m for lvl, m in levels)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

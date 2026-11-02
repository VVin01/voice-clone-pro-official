"""PO Token forced-mint experiment (owner-approved 2026-11-02).

The sidecar's first prod proof failed on all 3 attempts with NO
/get_pot or mint activity visible in the app logs. Root mechanism:
the fetch policy is yt-dlp core's — YoutubeIE._fetch_po_token()
(yt_dlp/extractor/youtube/_video.py) reads the 'youtube' extractor
arg 'fetch_pot'; under the default 'auto' it fetches only when the
request is pre-marked required, which never happened on the walled
path. This experiment sets fetch_pot='always' on every YouTube
attempt (via _pot_extractor_args / _with_pot, the ONE wiring point)
and attaches _PotLogShim so provider fetches surface in the app logs.

These tests pin (no network): the policy on all 4 attempts in both
stages, its merge alongside player_client, the shim's attachment and
filtering, non-YouTube plans staying untouched, and — behaviourally —
that yt-dlp core actually resolves 'always' from the exact opts dict
the app hands to YoutubeDL, through the same _configuration_arg call
_fetch_po_token makes.
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
            yt_args = opts["extractor_args"]["youtube"]
            assert yt_args["fetch_pot"] == ["always"]


def test_fetch_pot_merges_with_player_client(monkeypatch, tmp_path):
    monkeypatch.delenv("POT_SERVER_URL", raising=False)
    dl = MediaDownloader(download_dir=str(tmp_path))
    plans = dict(_plans(dl, SHORTS_URL))
    # _with_pot must merge into the 'youtube' args _with_client set,
    # not clobber them (the pre-experiment ea.update() would have).
    assert plans["web_embedded"]["extractor_args"]["youtube"] == {
        "player_client": ["web_embedded"],
        "fetch_pot": ["always"],
    }
    assert plans["android"]["extractor_args"]["youtube"] == {
        "player_client": ["android"],
        "fetch_pot": ["always"],
    }
    assert plans["mweb"]["extractor_args"]["youtube"] == {
        "player_client": ["mweb"],
        "fetch_pot": ["always"],
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
    YoutubeIE._fetch_po_token makes in yt-dlp 2026.10.26."""
    import yt_dlp
    from yt_dlp.extractor.youtube._video import YoutubeIE

    monkeypatch.delenv("POT_SERVER_URL", raising=False)
    dl = MediaDownloader(download_dir=str(tmp_path))
    _label, opts = _plans(dl, YOUTUBE_URL)[0]
    with yt_dlp.YoutubeDL(opts) as ydl:
        ie = YoutubeIE(ydl)
        assert (
            ie._configuration_arg("fetch_pot", [""], ie_key=YoutubeIE)[0]
            == "always"
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

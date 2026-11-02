"""PO Token sidecar — bgutil-ytdlp-pot-provider wiring (2026-11-02).

Owner-approved route for the YouTube datacenter bot wall (R39/R39.1
proved it is Render egress-IP reputation, not the yt-dlp version):
the bgutil provider server runs inside the container on loopback
(Dockerfile + docker-entrypoint.sh) and the pip plugin feeds its
proof-of-origin tokens to yt-dlp. The ONLY app-side wiring is in
MediaDownloader._attempt_plans: every YouTube attempt carries
extractor_args['youtubepot-bgutilhttp']['base_url']; every other
host keeps exactly one untouched attempt.

These tests pin the wiring shape (no network): attempt plans, the
env override, plugin presence in requirements, and the Dockerfile /
entrypoint contract.
"""

from __future__ import annotations

from pathlib import Path

import app.services.downloader as dl_mod
from app.services.downloader import MediaDownloader, _pot_extractor_args

ROOT = Path(__file__).resolve().parents[2]
REQUIREMENTS = (ROOT / "requirements.txt").read_text(encoding="utf-8")
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
ENTRYPOINT = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")

YOUTUBE_URL = "https://www.youtube.com/watch?v=L3tsYC5OYhQ"
SHORTS_URL = "https://www.youtube.com/shorts/g2NWf7CVy3U"
TIKTOK_URL = "https://www.tiktok.com/@someone/video/7391505677920816389"

DEFAULT_POT_URL = "http://127.0.0.1:4416"


def _plans(dl, url, stage="info"):
    base = dl._get_base_opts(url)
    return list(dl._attempt_plans(base, url, stage=stage))


def test_pot_args_default_url(monkeypatch):
    monkeypatch.delenv("POT_SERVER_URL", raising=False)
    assert _pot_extractor_args() == {
        "youtubepot-bgutilhttp": {"base_url": [DEFAULT_POT_URL]}
    }


def test_pot_args_env_override(monkeypatch):
    monkeypatch.setenv("POT_SERVER_URL", "http://127.0.0.1:9999")
    assert _pot_extractor_args() == {
        "youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:9999"]}
    }


def test_every_youtube_attempt_carries_pot_args(monkeypatch, tmp_path):
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
            ea = opts.get("extractor_args") or {}
            assert ea["youtubepot-bgutilhttp"] == {"base_url": [DEFAULT_POT_URL]}


def test_pot_args_do_not_disturb_player_clients(monkeypatch, tmp_path):
    monkeypatch.delenv("POT_SERVER_URL", raising=False)
    dl = MediaDownloader(download_dir=str(tmp_path))
    plans = dict(_plans(dl, SHORTS_URL))
    assert plans["web_embedded"]["extractor_args"]["youtube"]["player_client"] == [
        "web_embedded"
    ]
    assert plans["android"]["extractor_args"]["youtube"]["player_client"] == ["android"]
    assert plans["mweb"]["extractor_args"]["youtube"]["player_client"] == ["mweb"]
    # The cookie'd default attempt keeps NO player_client override —
    # only the POT args are added.
    assert "youtube" not in plans["default"]["extractor_args"]


def test_non_youtube_attempts_untouched(monkeypatch, tmp_path):
    monkeypatch.delenv("POT_SERVER_URL", raising=False)
    dl = MediaDownloader(download_dir=str(tmp_path))
    base = dl._get_base_opts(TIKTOK_URL)
    plans = list(dl._attempt_plans(base, TIKTOK_URL, stage="info"))
    assert [label for label, _ in plans] == ["default"]
    assert plans[0][1] is base
    assert "youtubepot-bgutilhttp" not in (plans[0][1].get("extractor_args") or {})


def test_plugin_pinned_in_requirements():
    assert "bgutil-ytdlp-pot-provider==2.0.2" in REQUIREMENTS


def test_dockerfile_builds_pot_server_and_node22():
    # Node 22 pinned tarball (bgutil 2.0.2 server requires node >=22)
    assert "NODE_VERSION=22.23.4" in DOCKERFILE
    assert "NODE_SHA256=1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce" in DOCKERFILE
    # Provider server pinned + checksum-verified + compiled at build time
    assert "BGUTIL_VERSION=2.0.2" in DOCKERFILE
    assert "BGUTIL_SHA256=1f2ae9ec39405b8e80397dfc2551f58deed09d8dff4b2b8fbc314df8e091c0a1" in DOCKERFILE
    assert "/opt/bgutil-pot-server" in DOCKERFILE
    assert "npx tsc" in DOCKERFILE
    # Entrypoint starts the provider before the app; CMD unchanged
    assert 'ENTRYPOINT ["/app/docker-entrypoint.sh"]' in DOCKERFILE
    assert 'CMD ["python", "main.py", "--mode", "all"]' in DOCKERFILE


def test_entrypoint_contract():
    # Starts the server on loopback, bounded wait, app starts regardless
    assert "build/main.js" in ENTRYPOINT
    assert 'exec "$@"' in ENTRYPOINT
    assert "POT_SERVER_PORT" in ENTRYPOINT

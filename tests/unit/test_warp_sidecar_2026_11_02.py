"""WARP egress sidecar — usque / Cloudflare WARP wiring (2026-11-02).

Owner-approved free-egress route for the YouTube datacenter bot
wall: the POT sidecar alone did not pass the prod wall (tokens
minted and attached; the player response was still refused — the
wall keys on the egress IP). The feasibility proof
(warp-test/REPORT.md) showed this app's own extraction code
succeeding through Cloudflare WARP's free egress (Shorts 153
formats/3840p, Watch 38 formats/1080p, on two egress IPs).

usque (MASQUE over HTTP/2+TCP — Render restricts UDP egress) runs
inside the container as a loopback SOCKS5 proxy (Dockerfile +
docker-entrypoint.sh). Its anonymous device config is registered
at first boot and kept on the persistent disk — never in the repo
(public) or the image. The ONLY app-side wiring is in
MediaDownloader._attempt_plans (the same _with_pot point as POT):
every YouTube attempt carries 'proxy'; every other host keeps
exactly one untouched attempt (TikTok must NOT be proxied).

These tests pin the wiring shape (no network): attempt plans, the
env override/disable, and the Dockerfile / entrypoint contract.
"""

from __future__ import annotations

from pathlib import Path

from app.services.downloader import MediaDownloader, _warp_proxy_url

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
ENTRYPOINT = (ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")

YOUTUBE_URL = "https://www.youtube.com/watch?v=L3tsYC5OYhQ"
SHORTS_URL = "https://www.youtube.com/shorts/g2NWf7CVy3U"
TIKTOK_URL = "https://www.tiktok.com/@someone/video/7391505677920816389"

DEFAULT_WARP_PROXY = "socks5h://127.0.0.1:18080"
DEFAULT_POT_URL = "http://127.0.0.1:4416"


def _plans(dl, url, stage="info"):
    base = dl._get_base_opts(url)
    return list(dl._attempt_plans(base, url, stage=stage))


def test_warp_proxy_default_url(monkeypatch):
    monkeypatch.delenv("WARP_PROXY_URL", raising=False)
    assert _warp_proxy_url() == DEFAULT_WARP_PROXY


def test_warp_proxy_env_override(monkeypatch):
    monkeypatch.setenv("WARP_PROXY_URL", "socks5h://127.0.0.1:9999")
    assert _warp_proxy_url() == "socks5h://127.0.0.1:9999"


def test_warp_proxy_env_empty_disables(monkeypatch):
    monkeypatch.setenv("WARP_PROXY_URL", "")
    assert _warp_proxy_url() is None
    monkeypatch.setenv("WARP_PROXY_URL", "   ")
    assert _warp_proxy_url() is None


def test_every_youtube_attempt_carries_proxy(monkeypatch, tmp_path):
    monkeypatch.delenv("WARP_PROXY_URL", raising=False)
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
            assert opts.get("proxy") == DEFAULT_WARP_PROXY


def test_proxy_env_override_reaches_attempts(monkeypatch, tmp_path):
    monkeypatch.setenv("WARP_PROXY_URL", "socks5h://127.0.0.1:9999")
    dl = MediaDownloader(download_dir=str(tmp_path))
    for _label, opts in _plans(dl, SHORTS_URL):
        assert opts.get("proxy") == "socks5h://127.0.0.1:9999"


def test_proxy_disabled_attempts_carry_no_proxy_key(monkeypatch, tmp_path):
    monkeypatch.setenv("WARP_PROXY_URL", "")
    dl = MediaDownloader(download_dir=str(tmp_path))
    for stage in ("info", "media"):
        for _label, opts in _plans(dl, YOUTUBE_URL, stage=stage):
            assert "proxy" not in opts


def test_pot_wiring_unchanged_alongside_proxy(monkeypatch, tmp_path):
    # The WARP proxy rides the SAME wiring point as POT: provider
    # args and the log shim must be exactly as the POT builds left
    # them (fetch_pot stays 'always' on this base — its revert
    # rides R40, a different branch).
    monkeypatch.delenv("WARP_PROXY_URL", raising=False)
    monkeypatch.delenv("POT_SERVER_URL", raising=False)
    dl = MediaDownloader(download_dir=str(tmp_path))
    plans = dict(_plans(dl, SHORTS_URL))
    for _label, opts in plans.items():
        ea = opts.get("extractor_args") or {}
        assert ea["youtubepot-bgutilhttp"] == {"base_url": [DEFAULT_POT_URL]}
        assert "logger" in opts
        assert opts.get("proxy") == DEFAULT_WARP_PROXY
    assert plans["default"]["extractor_args"]["youtube"] == {
        "fetch_pot": ["always"]
    }


def test_non_youtube_attempts_have_no_proxy(monkeypatch, tmp_path):
    monkeypatch.delenv("WARP_PROXY_URL", raising=False)
    dl = MediaDownloader(download_dir=str(tmp_path))
    base = dl._get_base_opts(TIKTOK_URL)
    plans = list(dl._attempt_plans(base, TIKTOK_URL, stage="info"))
    assert [label for label, _ in plans] == ["default"]
    assert plans[0][1] is base
    assert "proxy" not in plans[0][1]


def test_dockerfile_pins_usque():
    # Pinned release artefact + checksum verification + fixed path
    assert "USQUE_VERSION=4.2.1" in DOCKERFILE
    assert (
        "USQUE_SHA256=4117e20695078af9c11edecd1a826c009bbc7ea0b7f64458612b4198910bc313"
        in DOCKERFILE
    )
    assert "Diniboy1123/usque/releases/download" in DOCKERFILE
    assert "/opt/usque/usque" in DOCKERFILE
    # Entrypoint still wraps the app; CMD unchanged
    assert 'ENTRYPOINT ["/app/docker-entrypoint.sh"]' in DOCKERFILE
    assert 'CMD ["python", "main.py", "--mode", "all"]' in DOCKERFILE


def test_entrypoint_contract():
    # Binary + fixed install path, env overrides for testing
    assert "/opt/usque/usque" in ENTRYPOINT
    assert "WARP_BIN" in ENTRYPOINT
    assert "WARP_SOCKS_PORT" in ENTRYPOINT
    assert "WARP_CONFIG_PATH" in ENTRYPOINT
    # Device config lives on the persistent disk, never in the image
    assert "/app/storage/warp/usque.json" in ENTRYPOINT
    # Registered only when missing, non-interactive TOS acceptance
    assert "register --accept-tos" in ENTRYPOINT
    assert '[ ! -f "$WARP_CONFIG" ]' in ENTRYPOINT
    # SOCKS5 in HTTP/2 mode, bound to loopback only
    assert "socks --http2" in ENTRYPOINT
    assert "--bind 127.0.0.1" in ENTRYPOINT
    # Bounded wait + the startup line the deploy check greps for,
    # and the app execs regardless of sidecar state
    assert "usque WARP SOCKS5 is up on 127.0.0.1:" in ENTRYPOINT
    assert 'exec "$@"' in ENTRYPOINT

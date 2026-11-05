"""Batch AJ / L7 — the downloader's default directory lives under
settings.storage_root (where the sweeps can see it), not in
os.getcwd()/downloads. Explicit-dir callers are untouched.
"""
from __future__ import annotations

from pathlib import Path

from app.config import settings
from app.services.downloader import MediaDownloader


def test_default_download_dir_is_under_storage_root():
    dl = MediaDownloader()
    default_dir = Path(dl.download_dir)
    root = Path(settings.storage_root)
    assert default_dir == root / "downloads"
    assert root in default_dir.parents


def test_explicit_download_dir_is_untouched(tmp_path):
    dl = MediaDownloader(download_dir=str(tmp_path / "custom"))
    assert dl.download_dir == str(tmp_path / "custom")

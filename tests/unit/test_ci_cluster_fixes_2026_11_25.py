"""Part E (2026-11-25) regression tests for CI Cluster A (shallow clone) & Cluster B (/work/ ancestor)."""
from __future__ import annotations

from pathlib import Path

from app.config import settings
from app.webapp import api as api_mod

ROOT = Path(__file__).resolve().parents[2]


def test_cluster_a_ci_uses_full_git_history_and_tests_do_not_silently_mask():
    ci_yml = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "fetch-depth: 0" in ci_yml

    for rel in (
        "tests/unit/test_round11_2026_11_22.py",
        "tests/unit/test_round12_2026_11_23.py",
        "tests/unit/test_phase_1_v2_liquid_glass.py",
        "tests/unit/test_phase_2_v2_home.py",
        "tests/unit/test_phase_3_v2_recap.py",
    ):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "base_js = APP_JS" not in src
        assert "except Exception:\n        return" not in src


def test_cluster_b_resolve_recap_deliverable_path_ignores_work_in_ancestor_root(tmp_path, monkeypatch):
    # Simulates GitHub Actions TMPDIR=/home/runner/work/repo/repo/.pytest-tmp/...
    ancestor_work_storage = tmp_path / "runner" / "work" / "voice-clone-pro" / "storage"
    ancestor_work_storage.mkdir(parents=True)
    monkeypatch.setattr(settings, "storage_root", ancestor_work_storage)
    jid = "rc_ci_cluster_b_test"
    api_mod.RECAP_JOBS_REGISTRY.pop(jid, None)

    sync_dir = ancestor_work_storage / "recap_output" / jid / "workspace" / "edge_tts_smart_sync"
    sync_dir.mkdir(parents=True)
    final_mp4 = sync_dir / "final_edge_tts_smart_sync.mp4"
    final_mp3 = sync_dir / "final_edge_tts_smart_sync.mp3"
    final_mp4.write_bytes(b"\x00" * 5000)
    final_mp3.write_bytes(b"\x00" * 2000)

    # Genuine intermediate work/ and tts_parts/ inside job_dir MUST still be ignored
    work_sub = ancestor_work_storage / "recap_output" / jid / "workspace" / "work"
    work_sub.mkdir(parents=True)
    (work_sub / "intermediate.mp4").write_bytes(b"\x00" * 5000)
    (work_sub / "intermediate.mp3").write_bytes(b"\x00" * 2000)

    tts_parts = ancestor_work_storage / "recap_output" / jid / "workspace" / "tts_parts"
    tts_parts.mkdir(parents=True)
    (tts_parts / "part_01.wav").write_bytes(b"\x00" * 2000)

    assert api_mod.resolve_recap_deliverable_path(jid, "video") == str(final_mp4)
    assert api_mod.resolve_recap_deliverable_path(jid, "audio") == str(final_mp3)

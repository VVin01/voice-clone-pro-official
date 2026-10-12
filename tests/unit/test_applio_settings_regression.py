from pathlib import Path
import pytest

from app.config import settings
from app.services.audio import (
    resolve_applio_python_path,
    resolve_applio_runner_path,
    run_applio_conversion,
)


def test_default_runner_path_resolution(monkeypatch):
    """When APPLIO_RUNNER is empty, defaults to <project_root>/worker/applio_runner.py."""
    monkeypatch.setattr(settings, "applio_runner", "")
    runner = resolve_applio_runner_path()
    assert runner == settings.root / "worker" / "applio_runner.py"
    assert runner.is_file(), "Default bridge runner script must exist on disk"


def test_explicit_runner_path_configuration(monkeypatch, tmp_path):
    """When APPLIO_RUNNER is explicitly set, resolve_applio_runner_path returns it."""
    custom_script = tmp_path / "custom_runner.py"
    custom_script.write_text("# custom runner", encoding="utf-8")

    monkeypatch.setattr(settings, "applio_runner", str(custom_script))
    runner = resolve_applio_runner_path()
    assert runner == custom_script


@pytest.mark.asyncio
async def test_missing_or_invalid_runner_path_handling(monkeypatch, tmp_path):
    """Missing runner script raises RuntimeError with APPLIO_RUNNER_NOT_FOUND."""
    non_existent = tmp_path / "ghost_runner.py"
    monkeypatch.setattr(settings, "applio_runner", str(non_existent))

    # Also point python to a dummy valid file so it reaches runner check
    fake_python = tmp_path / "python.exe"
    fake_python.write_bytes(b"dummy")
    monkeypatch.setattr(settings, "applio_python", str(fake_python))

    dummy_pth = tmp_path / "dummy.pth"
    dummy_wav = tmp_path / "dummy.wav"
    out_wav = tmp_path / "out.wav"

    with pytest.raises(RuntimeError, match="APPLIO_RUNNER_NOT_FOUND"):
        await run_applio_conversion(
            model_pth=dummy_pth,
            index_path=None,
            input_wav=dummy_wav,
            output_wav=out_wav,
        )


@pytest.mark.asyncio
async def test_missing_python_path_handling(monkeypatch, tmp_path):
    """Missing Applio python executable raises RuntimeError with APPLIO_PYTHON_NOT_FOUND."""
    non_existent_py = tmp_path / "missing_dir" / "python.exe"
    monkeypatch.setattr(settings, "applio_python", str(non_existent_py))

    dummy_pth = tmp_path / "dummy.pth"
    dummy_wav = tmp_path / "dummy.wav"
    out_wav = tmp_path / "out.wav"

    with pytest.raises(RuntimeError, match="APPLIO_PYTHON_NOT_FOUND"):
        await run_applio_conversion(
            model_pth=dummy_pth,
            index_path=None,
            input_wav=dummy_wav,
            output_wav=out_wav,
        )

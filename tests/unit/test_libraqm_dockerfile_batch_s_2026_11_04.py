"""Batch S hotfix (2026-11-04) — the production image must ship libraqm.

Batch Q's PNG-overlay subtitle renderer (app/services/recap_engine/
png_subtitles.py) hard-requires Pillow's RAQM layout engine:
assert_raqm() raises when features.check("raqm") is False, because
correct Myanmar shaping must never silently degrade. Pillow binds
RAQM from the system shared library at runtime — the sandbox has it,
so the whole suite passed, but the production Dockerfile
(python:3.12-slim, Debian bookworm) installed ffmpeg/fontconfig/fonts
and NOT libraqm, so every subtitled recap job on prod failed at the
first cue. The fix is one apt package: libraqm0 (bookworm runtime
package, verified on packages.debian.org). This pin keeps the package
in the Dockerfile's apt-get install list so the regression cannot
come back silently.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")


def _apt_install_block(dockerfile: str) -> str:
    match = re.search(
        r"apt-get install .*?&& rm -rf /var/lib/apt/lists/\*",
        dockerfile,
        flags=re.DOTALL,
    )
    assert match, "Dockerfile apt-get install block not found"
    return match.group(0)


def test_dockerfile_installs_libraqm0():
    block = _apt_install_block(DOCKERFILE)
    # A standalone package entry in the install list (`libraqm0 \`),
    # not a comment or an unrelated package name containing the string.
    assert re.search(r"(?m)^\s*libraqm0\s*\\?$", block), (
        "Dockerfile must install libraqm0: Pillow's RAQM layout engine "
        "(required by png_subtitles.assert_raqm) binds the system "
        "libraqm shared library, which python:3.12-slim does not ship."
    )

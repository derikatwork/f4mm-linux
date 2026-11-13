"""tools/make_screenshots.py still makes the README's pictures (in a
temporary folder: the ones in docs/ are only changed by hand)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets")
pytestmark = pytest.mark.skipif(not shutil.which("7z"), reason="7z not installed")

ROOT = Path(__file__).resolve().parent.parent
PICTURES = ("main-window.png", "plugins-tab.png", "install-dialog.png", "fomod-installer.png")


def test_the_screenshots_are_made(tmp_path):
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    result = subprocess.run([sys.executable, str(ROOT / "tools" / "make_screenshots.py"), str(tmp_path / "shots"),
                             str(tmp_path / "home")], capture_output=True, text=True, env=env, timeout=300)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    for name in PICTURES:
        picture = tmp_path / "shots" / name
        assert picture.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and picture.stat().st_size > 10_000, name

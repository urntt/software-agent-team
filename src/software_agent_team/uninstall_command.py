"""Dispatch the canonical CLI to the installation-owned uninstaller."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path


def uninstall(arguments: Sequence[str]) -> int:
    """Preserve shell lifecycle policy and its exact exit status."""

    script = Path(__file__).resolve().parents[2] / "scripts" / "uninstall.sh"
    if not script.is_file():
        print("error: the installation-owned uninstall script is missing")
        return 1
    return subprocess.call(("bash", str(script), *arguments))

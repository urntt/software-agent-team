"""One prerequisite check for tests that execute the private pinned runtime."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_MISSING_RUNTIME = (
    "pinned OpenClaw runtime is missing; run `make setup` from the repository root"
)


def pinned_openclaw_binary(root: Path) -> Path:
    """Return the actual OpenClaw launcher or skip an unprepared checkout."""

    binary = root / ".sat/openclaw/bin/openclaw"
    if not binary.is_file():
        pytest.skip(_MISSING_RUNTIME)
    return binary


def pinned_openclaw_node(root: Path) -> Path:
    """Return the pinned Node binary or skip an unprepared checkout."""

    pins = (root / "configs/toolchain.sh").read_text(encoding="utf-8")
    match = re.search(r'^task_node_version="([^"]+)"$', pins, re.MULTILINE)
    assert match is not None
    node = root / ".sat/openclaw/tools" / f"node-v{match[1]}/bin/node"
    if not node.is_file():
        pytest.skip(_MISSING_RUNTIME)
    return node

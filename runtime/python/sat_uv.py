#!/usr/local/bin/python
"""Seed a writable offline uv cache, then replace this process with uv."""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

PUBLIC_CACHE = Path("/opt/software-agent-team/uv-public-cache")
DEFAULT_CACHE = Path("/tmp/uv-cache")
REAL_UV = Path("/usr/local/bin/uv-real")
CACHE_MARKER = ".sat-public-cache-v1"
MARKER_CONTENT = "software-agent-team-public-uv-cache-v1\n"


def _fail(message: str) -> NoReturn:
    print(f"uv: SAT offline cache error: {message}", file=sys.stderr)
    raise SystemExit(2)


def _cache_target() -> Path:
    raw = os.environ.get("UV_CACHE_DIR", str(DEFAULT_CACHE))
    target = Path(os.path.normpath(raw))
    temporary_root = Path("/tmp")
    if not target.is_absolute() or target == temporary_root:
        _fail("UV_CACHE_DIR must name a directory below /tmp")
    try:
        target.relative_to(temporary_root)
    except ValueError:
        _fail("UV_CACHE_DIR must name a directory below /tmp")

    cursor = temporary_root
    for part in target.relative_to(temporary_root).parts[:-1]:
        cursor /= part
        if cursor.is_symlink():
            _fail("UV_CACHE_DIR cannot traverse a symlink")
    return target


def _ready(cache: Path) -> bool:
    marker = cache / CACHE_MARKER
    if cache.is_symlink() or marker.is_symlink() or not marker.is_file():
        return False
    try:
        return marker.read_text(encoding="utf-8") == MARKER_CONTENT
    except OSError:
        return False


def _make_writable(cache: Path) -> None:
    for path in (cache, *cache.rglob("*")):
        if path.is_symlink():
            continue
        path.chmod(path.stat().st_mode | stat.S_IWUSR)


def _seed(target: Path) -> None:
    if _ready(target):
        return
    if target.exists() or target.is_symlink():
        _fail(f"cache target exists without a valid marker: {target}")
    if not _ready(PUBLIC_CACHE):
        _fail("frozen public package cache is unavailable")

    target.parent.mkdir(parents=True, exist_ok=True)
    candidate = Path(tempfile.mkdtemp(prefix=".sat-uv-cache-", dir=str(target.parent)))
    candidate.rmdir()
    try:
        shutil.copytree(PUBLIC_CACHE, candidate, symlinks=True)
        _make_writable(candidate)
        try:
            candidate.rename(target)
        except OSError:
            if not _ready(target):
                raise
    finally:
        if candidate.exists():
            shutil.rmtree(candidate)


def main() -> None:
    target = _cache_target()
    _seed(target)
    environment = os.environ.copy()
    environment["UV_CACHE_DIR"] = str(target)
    os.execve(REAL_UV, [str(REAL_UV), *sys.argv[1:]], environment)


if __name__ == "__main__":
    main()

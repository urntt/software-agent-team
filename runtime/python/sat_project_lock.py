#!/usr/local/bin/python
"""Generate or check a portable project lock with the frozen public cache."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PUBLIC_CACHE = Path("/opt/software-agent-team/uv-public-cache")
SHARED_CACHE = Path("/tmp/uv-cache")


def _environment(cache: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("UV_"):
            environment.pop(name)
    environment.update(
        {
            "UV_CACHE_DIR": str(cache),
            "UV_CONFIG_FILE": "/dev/null",
            "UV_DEFAULT_INDEX": "https://pypi.org/simple",
            "UV_OFFLINE": "1",
        }
    )
    return environment


def _self_test(temporary: Path, cache: Path) -> int:
    project = temporary / "self-test"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\n"
        'name = "sat-project-lock-self-test"\n'
        'version = "0.0.0"\n'
        'requires-python = ">=3.12,<3.13"\n'
        'dependencies = ["fastapi==0.116.1"]\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["uv", "lock", "--offline"],
        cwd=project,
        check=False,
        env=_environment(cache),
    )
    if result.returncode != 0:
        return result.returncode
    lock = (project / "uv.lock").read_text(encoding="utf-8")
    if (
        'registry = "https://pypi.org/simple"' not in lock
        or "/opt/software-agent-team" in lock
    ):
        print("sat-project-lock: self-test produced a non-portable lock")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if not PUBLIC_CACHE.is_dir():
        raise SystemExit("sat-project-lock: frozen public uv cache is unavailable")

    try:
        if args.self_test:
            with tempfile.TemporaryDirectory(prefix="sat-project-lock-") as name:
                raise SystemExit(_self_test(Path(name), SHARED_CACHE))
        argv = ["uv", "lock", "--offline"]
        if args.check:
            argv.append("--check")
        result = subprocess.run(argv, check=False, env=_environment(SHARED_CACHE))
    except OSError:
        print(
            "sat-project-lock: unable to prepare the offline lock command; "
            "check writable /tmp space and the uv runtime",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()

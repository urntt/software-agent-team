"""Black-box acceptance for the frozen offline text-statistics request."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def invoke(repository: Path, path: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(repository)
    return subprocess.run(
        [sys.executable, "-m", "textstats", str(path), "--json"],
        cwd=repository,
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    args = parser.parse_args()
    repository = args.repository.resolve()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        cases = (
            (
                "empty.txt",
                "",
                {"lines": 0, "words": 0, "characters": 0, "unique_words": 0},
            ),
            (
                "unicode.txt",
                "Hello hello\n世界 world\n",
                {"lines": 2, "words": 4, "characters": 21, "unique_words": 3},
            ),
        )
        for name, content, expected in cases:
            path = root / name
            path.write_text(content, encoding="utf-8")
            result = invoke(repository, path)
            if result.returncode != 0:
                raise AssertionError(f"{name}: CLI failed: {result.stderr[-400:]}")
            actual = json.loads(result.stdout)
            if actual != expected:
                raise AssertionError(f"{name}: expected {expected}, got {actual}")
        for path in (root / "missing.txt", root / "invalid.txt"):
            if path.name == "invalid.txt":
                path.write_bytes(b"\xff\xfe")
            result = invoke(repository, path)
            if (
                result.returncode == 0
                or not result.stderr.strip()
                or result.stdout.strip()
            ):
                raise AssertionError(f"{path.name}: expected a clean error")
    readme = (repository / "README.md").read_text(encoding="utf-8").lower()
    if not all(word in readme for word in ("install", "usage", "test", "limit")):
        raise AssertionError("README must cover installation, usage, tests, and limits")
    print("text statistics acceptance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Black-box acceptance for the frozen recursive JSON merge request."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def invoke(
    repository: Path, left: Path, right: Path
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(repository)
    return subprocess.run(
        [sys.executable, "-m", "jsonmerge", str(left), str(right)],
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
    repository = parser.parse_args().repository.resolve()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        left = root / "left.json"
        right = root / "right.json"
        left.write_text('{"nested":{"a":1,"b":2},"array":[1,2],"keep":true}')
        right.write_text('{"nested":{"b":3,"c":4},"array":[9],"new":null}')
        valid = invoke(repository, left, right)
        if valid.returncode != 0:
            raise AssertionError(f"valid JSON merge failed: {valid.stderr[-400:]}")
        expected = {
            "nested": {"a": 1, "b": 3, "c": 4},
            "array": [9],
            "keep": True,
            "new": None,
        }
        if json.loads(valid.stdout) != expected:
            raise AssertionError("nested objects must merge and arrays must replace")
        invalid = root / "invalid.json"
        invalid.write_text("{")
        array_root = root / "array.json"
        array_root.write_text("[]")
        for path in (invalid, array_root, root / "absent.json"):
            result = invoke(repository, left, path)
            if (
                result.returncode == 0
                or not result.stderr.strip()
                or result.stdout.strip()
            ):
                raise AssertionError(f"{path.name}: expected a clean error")
    readme = (repository / "README.md").read_text(encoding="utf-8").lower()
    if not all(word in readme for word in ("install", "usage", "test", "limit")):
        raise AssertionError("README must cover installation, usage, tests, and limits")
    print("JSON merge acceptance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

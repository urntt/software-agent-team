"""Black-box acceptance for the frozen offline Markdown link request."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def invoke(repository: Path, target: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(repository)
    return subprocess.run(
        [sys.executable, "-m", "linkcheck", str(target), "--json"],
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
        (root / "guide.md").write_text("# Intro\nUseful details.\n", encoding="utf-8")
        (root / "index.md").write_text(
            "[Guide](guide.md#intro)\n[Remote](https://example.com/x)\n",
            encoding="utf-8",
        )
        valid = invoke(repository, root)
        if valid.returncode != 0:
            raise AssertionError(f"valid Markdown links failed: {valid.stderr[-400:]}")
        if json.loads(valid.stdout).get("broken_links") != []:
            raise AssertionError("valid links must produce an empty broken_links array")
        (root / "broken.md").write_text(
            "[Missing](absent.md)\n[Anchor](guide.md#missing)\n",
            encoding="utf-8",
        )
        broken = invoke(repository, root)
        if broken.returncode == 0:
            raise AssertionError("broken local links must fail")
        findings = json.loads(broken.stdout).get("broken_links")
        if not isinstance(findings, list) or len(findings) != 2:
            raise AssertionError("two broken local links must be reported")
        rendered = json.dumps(findings)
        if "broken.md" not in rendered or "1" not in rendered or "2" not in rendered:
            raise AssertionError("broken links need source file and line evidence")
    readme = (repository / "README.md").read_text(encoding="utf-8").lower()
    if not all(word in readme for word in ("install", "usage", "test", "limit")):
        raise AssertionError("README must cover installation, usage, tests, and limits")
    print("Markdown link acceptance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Bind the stable bootstrap shell asset to the exact published source tag."""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

TAG_PLACEHOLDER = "__SAT_RELEASE_TAG__"
REVISION_PLACEHOLDER = "__SAT_RELEASE_REVISION__"


def render_bootstrap_asset(*, repository: Path, tag: str, output: Path) -> None:
    """Write a release-specific entry point after checking the local tag identity."""

    if re.fullmatch(r"v\d+\.\d+\.\d+", tag) is None:
        raise ValueError("bootstrap asset requires a version tag")
    revision = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", f"refs/tags/{tag}^{{commit}}"],
        text=True,
    ).strip()
    head = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    if revision != head or re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("bootstrap asset must be built from its exact release tag")
    template = (repository / "scripts/bootstrap.sh").read_text(encoding="utf-8")
    if (
        template.count(TAG_PLACEHOLDER) != 1
        or template.count(REVISION_PLACEHOLDER) != 1
    ):
        raise ValueError("bootstrap template release placeholders are invalid")
    rendered = template.replace(TAG_PLACEHOLDER, tag).replace(
        REVISION_PLACEHOLDER, revision
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    output.chmod(0o755)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    render_bootstrap_asset(
        repository=args.repository.resolve(), tag=args.tag, output=args.output
    )


if __name__ == "__main__":
    main()

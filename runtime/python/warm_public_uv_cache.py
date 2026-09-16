#!/usr/local/bin/python
"""Create a temporary uv project that warms the public package cache."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", required=True, type=Path)
    parser.add_argument("--project", required=True, type=Path)
    args = parser.parse_args()

    requirements = []
    for raw_line in args.requirements.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # uv is the package manager already installed in the image. Generated
        # projects do not depend on its 20+ MiB distribution, so copying that
        # distribution into every sandbox cache would waste the bounded tmpfs.
        if canonicalize_name(Requirement(line).name) != "uv":
            requirements.append(line)
    requirements.append("colorama==0.4.6")
    args.project.mkdir(parents=True)
    (args.project / "pyproject.toml").write_text(
        "[project]\n"
        'name = "sat-runtime-catalog"\n'
        'version = "0.0.0"\n'
        'requires-python = ">=3.12,<3.13"\n'
        f"dependencies = {json.dumps(requirements)}\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

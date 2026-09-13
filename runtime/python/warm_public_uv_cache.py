#!/usr/local/bin/python
"""Create a temporary uv project that warms public registry metadata."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--requirements", required=True, type=Path)
    parser.add_argument("--project", required=True, type=Path)
    args = parser.parse_args()

    requirements = [
        line.strip()
        for line in args.requirements.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
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

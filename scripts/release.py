#!/usr/bin/env python3
"""Validate or materialize the immutable SAT release manifest."""

from __future__ import annotations

import argparse
from pathlib import Path

from software_agent_team.release_tools import build_release_manifest
from software_agent_team.releases import DEFAULT_REPOSITORY_URL, release_manifest_bytes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--repository-url", default=DEFAULT_REPOSITORY_URL)
    parser.add_argument("--allow-untagged-candidate", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    manifest = build_release_manifest(
        repository=args.repository,
        tag=args.tag,
        repository_url=args.repository_url,
        require_tag=not args.allow_untagged_candidate,
    )
    payload = release_manifest_bytes(manifest)
    if args.output is None:
        print(payload.decode(), end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(payload)
        print(f"release manifest: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

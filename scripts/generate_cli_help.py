#!/usr/bin/env python3
"""Regenerate the lightweight root-help snapshot from the canonical parser."""

import json
from pathlib import Path

from software_agent_team.cli import build_parser


def main() -> None:
    destination = (
        Path(__file__).resolve().parents[1] / "src/software_agent_team/_cli_help.py"
    )
    rendered = build_parser().format_help()
    chunks = [rendered[index : index + 68] for index in range(0, len(rendered), 68)]
    destination.write_text(
        '"""Generated root CLI help; see scripts/generate_cli_help.py."""\n\n'
        "ROOT_HELP = (\n"
        + "".join(f"    {json.dumps(chunk, ensure_ascii=False)}\n" for chunk in chunks)
        + ")\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()

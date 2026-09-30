"""Keep the local version query outside the full workflow CLI import graph."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch CLI work, loading the workflow graph only when it is needed."""

    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ("uninstall",):
        from software_agent_team.uninstall_command import uninstall

        return uninstall(arguments[1:])
    if arguments in {("--help",), ("-h",)}:
        from software_agent_team._cli_help import ROOT_HELP

        print(ROOT_HELP, end="")
        return 0
    if arguments == ("--version",):
        from software_agent_team.versioning import (
            inspect_software_version,
            render_short_version,
        )

        try:
            project_root = Path(__file__).resolve().parents[2]
            print(
                render_short_version(
                    inspect_software_version(project_root=project_root)
                )
            )
            return 0
        except KeyboardInterrupt:
            print("\nBuild interrupted. SAT did not claim a successful delivery.")
            return 130
        except (OSError, ValueError, RuntimeError) as error:
            print(f"error: {error}")
            return 1

    from software_agent_team.cli import main as workflow_main

    return workflow_main(arguments)

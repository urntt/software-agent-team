"""Verify lightweight process startup without changing the public CLI contract."""

from __future__ import annotations

import subprocess
import sys

import software_agent_team
from software_agent_team import AgentBudget, ArtifactStore
from software_agent_team._cli_help import ROOT_HELP
from software_agent_team.budgets import AgentBudget as BudgetDefinition
from software_agent_team.cli import build_parser
from software_agent_team.cli import main as full_cli_main
from software_agent_team.entrypoint import main as entrypoint_main


def test_public_package_exports_keep_their_canonical_identities() -> None:
    assert AgentBudget is BudgetDefinition
    assert ArtifactStore.__module__ == "software_agent_team.artifact_store"
    assert all(
        hasattr(software_agent_team, name) for name in software_agent_team.__all__
    )


def test_version_fast_path_matches_full_cli(capsys) -> None:
    assert full_cli_main(["--version"]) == 0
    expected = capsys.readouterr().out

    assert entrypoint_main(["--version"]) == 0
    assert capsys.readouterr().out == expected


def test_version_process_does_not_import_workflow_cli() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from software_agent_team.entrypoint import main; "
            "status = main(['--version']); "
            "assert 'software_agent_team.cli' not in sys.modules; "
            "raise SystemExit(status)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.startswith("sat ")


def test_root_help_snapshot_matches_canonical_parser(capsys) -> None:
    assert build_parser().format_help() == ROOT_HELP
    assert entrypoint_main(["--help"]) == 0
    assert capsys.readouterr().out == ROOT_HELP


def test_help_process_does_not_import_workflow_cli() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from software_agent_team.entrypoint import main; "
            "status = main(['--help']); "
            "assert 'software_agent_team.cli' not in sys.modules; "
            "raise SystemExit(status)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ROOT_HELP


def test_uninstall_help_runs_owned_script_without_workflow_import() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from software_agent_team.entrypoint import main; "
            "status = main(['uninstall', '--help']); "
            "assert 'software_agent_team.cli' not in sys.modules; "
            "raise SystemExit(status)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Usage: sat uninstall [options]" in completed.stdout
    assert "--export-to PATH" in completed.stdout

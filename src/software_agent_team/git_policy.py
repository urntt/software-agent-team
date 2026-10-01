"""One environment/config policy for host-side Git operations."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

UNSAFE_CONFIG_PATTERN = (
    r"^(core\.hookspath|core\.fsmonitor|"
    r"filter\..*\.(clean|smudge|process))$"
)


def git_environment(home: Path) -> dict[str, str]:
    """Exclude ambient Git, credential, loader, and shell configuration."""

    return {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": str(home.resolve(strict=False)),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.environ.get("PATH", ""),
    }


def git_command(
    arguments: list[str],
    *,
    binary: str = "git",
    controller_config: bool = True,
    allow_https: bool = False,
) -> list[str]:
    """Disable host callbacks; permit only local repository transport."""

    command = [binary]
    if controller_config:
        command.extend(
            [
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "credential.helper=",
                "-c",
                "core.attributesFile=/dev/null",
                "-c",
                "protocol.allow=never",
                "-c",
                "protocol.file.allow=always",
                "-c",
                "commit.gpgSign=false",
                "-c",
                "tag.gpgSign=false",
            ]
        )
        if allow_https:
            command.extend(["-c", "protocol.https.allow=always"])
    return [*command, *arguments]


def repository_has_callbacks(repository: Path) -> bool:
    """Read local/include configuration without hiding unsafe callback values."""

    result = subprocess.run(
        git_command(
            [
                "-C",
                str(repository),
                "config",
                "--includes",
                "--get-regexp",
                UNSAFE_CONFIG_PATTERN,
            ],
            controller_config=False,
        ),
        env=git_environment(repository),
        check=False,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=30,
    )
    if result.returncode not in (0, 1):
        raise ValueError("could not verify repository callback configuration")
    return result.returncode == 0 and bool(result.stdout.strip())

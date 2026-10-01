"""Verify shared uv download authority and preservation through the shell entry."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("arch", ["x86_64", "aarch64"])
@pytest.mark.parametrize("corrupt", [False, True])
def test_uv_archive_is_verified_before_execution(
    tmp_path: Path, arch: str, corrupt: bool
) -> None:
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "configs").mkdir()
    shutil.copy2(ROOT / "scripts/install-uv.sh", checkout / "scripts/install-uv.sh")
    unpacked = tmp_path / f"uv-{arch}-unknown-linux-gnu"
    unpacked.mkdir()
    marker = tmp_path / "executed"
    binary = unpacked / "uv"
    binary.write_text(f"#!/bin/sh\ntouch '{marker}'\necho 'uv 0.12.0'\n")
    binary.chmod(0o755)
    archive = tmp_path / "archive.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        stream.add(unpacked, arcname=unpacked.name)
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (checkout / "configs/toolchain.sh").write_text(
        'task_uv_version="0.12.0"\n'
        f'task_uv_x64_sha256="{checksum}"\n'
        f'task_uv_arm64_sha256="{checksum}"\n'
    )
    if corrupt:
        with archive.open("ab") as stream:
            stream.write(b"unverified mutation")
    tools = tmp_path / "tools"
    tools.mkdir()
    curl = tools / "curl"
    curl.write_text(
        "#!/bin/bash\nset -eu\n"
        '[[ "$*" == *"/0.12.0/uv-"* ]] || exit 3\n'
        'while [[ "$1" != -o ]]; do shift; done\n'
        f'cp "{archive}" "$2"\n'
    )
    curl.chmod(0o755)
    uname = tools / "uname"
    uname.write_text(f'#!/bin/sh\n[ "$1" = -s ] && echo Linux || echo {arch}\n')
    uname.chmod(0o755)
    target = tmp_path / "bin/uv"
    result = subprocess.run(
        ["bash", str(checkout / "scripts/install-uv.sh"), str(target)],
        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    if corrupt:
        assert result.returncode != 0
        assert "checksum mismatch" in result.stderr
        assert not marker.exists()
        assert not target.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert marker.exists() and target.is_file()
        assert target.read_bytes() == binary.read_bytes()
        target.write_text("#!/bin/sh\necho existing shared version\n")
        preserved = target.read_bytes()
        second = subprocess.run(
            ["bash", str(checkout / "scripts/install-uv.sh"), str(target)],
            capture_output=True,
            timeout=10,
        )
        assert second.returncode == 0
        assert target.read_bytes() == preserved
    assert not list(target.parent.glob(".sat-uv.*"))

"""Reject altered dependency authority before evaluating downloaded packages."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from pinned_runtime import pinned_openclaw_node

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("mutation", ["checksum", "integrity", "lifecycle"])
def test_runtime_lock_refuses_unapproved_authority(
    tmp_path: Path, mutation: str
) -> None:
    runtime = tmp_path / "runtime"
    shutil.copytree(ROOT / "configs/openclaw-runtime", runtime)
    manifest = runtime / "package.json"
    lock = runtime / "package-lock.json"
    manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    lock_digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    if mutation == "lifecycle":
        data = json.loads(manifest.read_text())
        data["satLifecyclePolicy"]["allowlist"] = ["unreviewed-script"]
        manifest.write_text(json.dumps(data))
        manifest_digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    else:
        data = json.loads(lock.read_text())
        data["packages"]["node_modules/openclaw"]["integrity"] = "unverified"
        lock.write_text(json.dumps(data))
        if mutation == "integrity":
            lock_digest = hashlib.sha256(lock.read_bytes()).hexdigest()
    result = subprocess.run(
        [
            str(pinned_openclaw_node(ROOT)),
            str(ROOT / "scripts/check-runtime-lock.mjs"),
            str(runtime),
            "2026.9.6",
            manifest_digest,
            lock_digest,
        ],
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert {
        "checksum": "checksum mismatch",
        "integrity": "lacks frozen registry integrity",
        "lifecycle": "lifecycle authority mismatch",
    }[mutation] in result.stderr

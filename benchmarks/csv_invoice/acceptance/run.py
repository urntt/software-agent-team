"""Black-box acceptance for the frozen invoice CSV request."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def invoke(repository: Path, target: Path) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(repository)
    return subprocess.run(
        [sys.executable, "-m", "invoice_totals", str(target), "--json"],
        cwd=repository,
        env=environment,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, required=True)
    repository = parser.parse_args().repository.resolve()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        valid_path = root / "valid.csv"
        valid_path.write_text(
            "date,client,amount\n2026-09-01,Alice,1.20\n"
            "2026-09-02,Bob,0.10\n2026-09-03,Alice,2.30\n",
            encoding="utf-8",
        )
        valid = invoke(repository, valid_path)
        if valid.returncode != 0:
            raise AssertionError(f"valid invoice CSV failed: {valid.stderr[-400:]}")
        expected = {"Alice": "3.50", "Bob": "0.10"}
        if json.loads(valid.stdout).get("totals") != expected:
            raise AssertionError("CSV totals must use exact two-decimal strings")
        bad_amount = root / "bad-amount.csv"
        bad_amount.write_text("date,client,amount\n2026-09-01,Alice,nope\n")
        missing_column = root / "missing-column.csv"
        missing_column.write_text("date,client\n2026-09-01,Alice\n")
        for path in (bad_amount, missing_column, root / "absent.csv"):
            result = invoke(repository, path)
            if (
                result.returncode == 0
                or not result.stderr.strip()
                or result.stdout.strip()
            ):
                raise AssertionError(f"{path.name}: expected a clean error")
    readme = (repository / "README.md").read_text(encoding="utf-8").lower()
    if not all(word in readme for word in ("install", "usage", "test", "limit")):
        raise AssertionError("README must cover installation, usage, tests, and limits")
    print("invoice CSV acceptance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

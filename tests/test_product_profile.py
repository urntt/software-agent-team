"""Contract tests for the task-independent Python product profile."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from software_agent_team.quality_gates import load_quality_gate_configuration

REPOSITORY_ROOT = Path(__file__).parents[1]
PROFILE_ROOT = REPOSITORY_ROOT / "profiles" / "python"


def write_valid_project(root: Path) -> None:
    """Create the smallest project accepted by the trusted profile validator."""

    subprocess.run(
        ["git", "init", "--quiet", str(root)],
        check=True,
        capture_output=True,
        text=True,
    )
    (root / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (root / "README.md").write_text(
        """# Link Checker

## Installation

`uv sync --dev`

## Usage

`uv run link-checker .`

## Testing

`uv run pytest`

## Known limitations

Local files only.
""",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        "[project]\nname='link-checker'\n", encoding="utf-8"
    )
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(root), "add", "uv.lock"],
        check=True,
        capture_output=True,
        text=True,
    )
    (root / "sat-project.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "setup": ["uv", "sync", "--dev"],
                "start": ["uv", "run", "link-checker", "."],
                "test": ["uv", "run", "pytest"],
            }
        ),
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir()
    (tests / "test_links.py").write_text(
        "def test_placeholder():\n    assert True\n", encoding="utf-8"
    )


def run_validator(repository: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(PROFILE_ROOT / "validation" / "run.py"),
            "--repository",
            str(repository),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def commit_project(repository: Path) -> None:
    """Commit the test project so clean-copy validation has a Git authority."""

    for arguments in (
        ("config", "user.name", "urntt"),
        ("config", "user.email", "urntts@gmail.com"),
        ("add", "."),
        ("commit", "--quiet", "-m", "test: initialize generated project"),
    ):
        subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )


def write_fake_uv(path: Path) -> Path:
    """Create a deterministic exact-command executable for host-side tests."""

    executable = path / "uv"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

cwd = Path.cwd()
argv = sys.argv[1:]
log = Path(os.environ["FAKE_UV_LOG"])
with log.open("a", encoding="utf-8") as handle:
    handle.write(
        json.dumps(
            {
                "argv": argv,
                "cwd": str(cwd),
                "uv_config_file": os.environ.get("UV_CONFIG_FILE"),
                "uv_default_index": os.environ.get("UV_DEFAULT_INDEX"),
                "uv_index_url": os.environ.get("UV_INDEX_URL"),
            }
        )
        + "\\n"
    )
if (cwd / ".git").exists() or (cwd / "local-only.txt").exists():
    raise SystemExit(31)
if os.environ.get("FAKE_UV_FAIL") == " ".join(argv):
    raise SystemExit(32)
if argv == ["lock", "--check", "--offline"]:
    pass
elif argv == ["sync", "--dev"]:
    (cwd / ".venv").mkdir()
    (cwd / ".venv" / "ready").write_text("ready", encoding="utf-8")
    if os.environ.get("FAKE_UV_CREATE_UNIGNORED") == "1":
        (cwd / "generated.json").write_text("{}\\n", encoding="utf-8")
elif argv == ["run", "pytest"]:
    if not (cwd / ".venv" / "ready").is_file():
        raise SystemExit(33)
elif argv == ["run", "link-checker", "."]:
    if not (cwd / ".venv" / "ready").is_file():
        raise SystemExit(34)
    if os.environ.get("FAKE_UV_INTERACTIVE_JSON"):
        stream = (
            sys.stdout
            if os.environ["FAKE_UV_INTERACTIVE_JSON"] == "stdout"
            else sys.stderr
        )
        print("File path: ", end="", file=stream, flush=True)
        path = sys.stdin.readline().strip()
        if not path:
            raise SystemExit(36)
        content = (cwd / path).read_bytes().decode("utf-8")
        print(json.dumps({"code_point_count": len(content)}))
    if os.environ.get("FAKE_UV_WAIT_FOR_STDIN") == "1":
        print("Enter file path: ", end="", file=sys.stderr, flush=True)
        if sys.stdin.buffer.read(1) == b"":
            raise SystemExit(36)
elif (
    argv[:3] == ["run", "link-checker", "."]
    and len(argv) == 4
    and argv[3].endswith(".txt")
):
    content = (cwd / argv[3]).read_bytes().decode("utf-8")
    if os.environ.get("FAKE_UV_NORMALIZE_NEWLINES") == "1":
        content = content.replace("\\r\\n", "\\n")
    print(json.dumps({"code_point_count": len(content)}))
else:
    raise SystemExit(35)
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def run_command_validator(
    repository: Path,
    *,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(PROFILE_ROOT / "validation" / "run_commands.py"),
            "--repository",
            str(repository),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=20,
    )


def test_product_profile_is_separate_from_the_task_manager_evaluation() -> None:
    configuration = load_quality_gate_configuration(
        REPOSITORY_ROOT / "configs" / "product-policy.json",
        PROFILE_ROOT / "quality.json",
    )

    assert configuration.policy.id == "product_python_v1"
    assert configuration.manifest.id == "python_product_v1"
    assert configuration.policy.sandbox.image == "sat-python-quality:phase1-v10"
    assert configuration.policy.limits.total_timeout_seconds == 420
    serialized = json.dumps(
        {
            "policy": configuration.policy.model_dump(mode="json"),
            "profile": configuration.manifest.model_dump(mode="json"),
            "brief": configuration.task_brief.model_dump(mode="json"),
        }
    )
    assert "task-manager" not in serialized


def test_product_profile_exposes_fixed_command_ownership_to_planning() -> None:
    configuration = load_quality_gate_configuration(
        REPOSITORY_ROOT / "configs" / "product-policy.json",
        PROFILE_ROOT / "quality.json",
    )

    command_constraint = next(
        constraint
        for constraint in configuration.task_brief.constraints
        if "sat-project.json" in constraint
    )

    assert '["uv", "sync", "--dev"]' in command_constraint
    assert '["uv", "run", "pytest"]' in command_constraint
    assert "replace only the start placeholder" in command_constraint
    assert any(
        "directly usable from the project root" in constraint
        for constraint in configuration.task_brief.constraints
    )
    assert any(
        "clean quality workspace before setup" in constraint
        for constraint in configuration.task_brief.constraints
    )


def test_product_test_gate_matches_the_delivered_pytest_entrypoint() -> None:
    configuration = load_quality_gate_configuration(
        REPOSITORY_ROOT / "configs" / "product-policy.json",
        PROFILE_ROOT / "quality.json",
    )
    gate = next(
        gate
        for gate in configuration.manifest.gates
        if gate.id == "CHECK_PROJECT_TESTS"
    )
    project_contract = json.loads(
        (PROFILE_ROOT / "seed" / "sat-project.json").read_text(encoding="utf-8")
    )

    assert project_contract["test"] == ["uv", "run", "pytest"]
    assert gate.argv == ("pytest", "-q", "-p", "no:cacheprovider")
    exact_gate = next(
        gate
        for gate in configuration.manifest.gates
        if gate.id == "CHECK_EXACT_PROJECT_COMMANDS"
    )
    assert exact_gate.argv == (
        "python",
        "/opt/software-agent-team/inputs/python-product-contract/run_commands.py",
        "--repository",
        "/workspace",
    )
    assert exact_gate.criterion_ids == (
        "AC_RUNNABLE",
        "AC_TESTS",
        "AC_DOCUMENTATION",
    )
    seed_configuration = tomllib.loads(
        (PROFILE_ROOT / "seed" / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert seed_configuration["tool"]["pytest"]["ini_options"]["pythonpath"] == [
        ".",
        "src",
    ]


def test_product_contract_validator_accepts_project_specific_commands(
    tmp_path: Path,
) -> None:
    write_valid_project(tmp_path)

    result = run_validator(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "passed" in result.stdout


def test_product_contract_validator_rejects_the_starter_placeholder(
    tmp_path: Path,
) -> None:
    write_valid_project(tmp_path)
    payload = json.loads((tmp_path / "sat-project.json").read_text(encoding="utf-8"))
    payload["start"] = ["uv", "run", "replace-with-project-entrypoint"]
    (tmp_path / "sat-project.json").write_text(json.dumps(payload), encoding="utf-8")

    result = run_validator(tmp_path)

    assert result.returncode == 1
    assert "starter placeholder" in result.stderr


def test_product_contract_accepts_ordinary_documentation_headings(
    tmp_path: Path,
) -> None:
    write_valid_project(tmp_path)

    result = run_validator(tmp_path)

    assert result.returncode == 0, result.stderr
    readme = (tmp_path / "README.md").read_text(encoding="utf-8").casefold()
    assert "setup" not in readme
    assert "start" not in readme


def test_product_contract_requires_each_exact_manifest_command(
    tmp_path: Path,
) -> None:
    write_valid_project(tmp_path)
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    (tmp_path / "README.md").write_text(
        readme.replace("`uv run link-checker .`", "`uv run link-checker --help`"),
        encoding="utf-8",
    )

    result = run_validator(tmp_path)

    assert result.returncode == 1
    assert "missing exact command guidance for: start" in result.stderr


def test_product_contract_requires_a_committed_uv_lock(
    tmp_path: Path,
) -> None:
    write_valid_project(tmp_path)
    subprocess.run(
        ["git", "-C", str(tmp_path), "rm", "--cached", "--force", "uv.lock"],
        check=True,
        capture_output=True,
        text=True,
    )
    (tmp_path / "uv.lock").unlink()
    (tmp_path / ".gitignore").write_text(".venv/\nuv.lock\n", encoding="utf-8")

    result = run_validator(tmp_path)

    assert result.returncode == 1
    assert (
        "uv.lock must be committed as dependency-resolution metadata" in result.stderr
    )


def test_product_contract_accepts_a_present_bounded_uv_lock(
    tmp_path: Path,
) -> None:
    write_valid_project(tmp_path)
    (tmp_path / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    result = run_validator(tmp_path)

    assert result.returncode == 0, result.stderr


def test_product_contract_accepts_project_relative_uv_lock_sources(
    tmp_path: Path,
) -> None:
    write_valid_project(tmp_path)
    (tmp_path / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "example.whl").write_bytes(b"portable project artifact")
    (tmp_path / "uv.lock").write_text(
        """version = 1

[[package]]
name = "example"
source = { editable = "." }
wheels = [{ path = "vendor/example.whl" }]
""",
        encoding="utf-8",
    )

    result = run_validator(tmp_path)

    assert result.returncode == 0, result.stderr


def test_product_contract_accepts_remote_uv_lock_sources(tmp_path: Path) -> None:
    write_valid_project(tmp_path)
    (tmp_path / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text(
        """version = 1

[[package]]
name = "example"
source = { registry = "https://pypi.org/simple" }
wheels = [{ url = "https://files.pythonhosted.org/example.whl" }]
""",
        encoding="utf-8",
    )

    result = run_validator(tmp_path)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "reference",
    (
        "/opt/software-agent-team/wheels/example.whl",
        "file:///tmp/example.whl",
        "C:\\sandbox\\example.whl",
        "../shared/example.whl",
    ),
)
def test_product_contract_rejects_non_portable_uv_lock_paths(
    tmp_path: Path,
    reference: str,
) -> None:
    write_valid_project(tmp_path)
    (tmp_path / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text(
        "version = 1\n"
        "[[package]]\n"
        'name = "example"\n'
        f"wheels = [{{ path = {json.dumps(reference)} }}]\n",
        encoding="utf-8",
    )

    result = run_validator(tmp_path)

    assert result.returncode == 1
    assert "non-portable local reference" in result.stderr


def test_product_contract_rejects_missing_or_symlinked_local_lock_sources(
    tmp_path: Path,
) -> None:
    write_valid_project(tmp_path)
    (tmp_path / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text(
        'version = 1\nwheels = [{ path = "vendor/missing.whl" }]\n',
        encoding="utf-8",
    )

    missing = run_validator(tmp_path)

    assert missing.returncode == 1
    assert "existing path inside the project" in missing.stderr

    vendor = tmp_path / "vendor"
    vendor.mkdir()
    target = vendor / "real.whl"
    target.write_bytes(b"wheel")
    (vendor / "missing.whl").symlink_to(target.name)

    symlink = run_validator(tmp_path)

    assert symlink.returncode == 1
    assert "cannot traverse a symlink" in symlink.stderr


def test_product_contract_rejects_an_intermediate_symlink_in_lock_source(
    tmp_path: Path,
) -> None:
    write_valid_project(tmp_path)
    (tmp_path / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    real_vendor = tmp_path / "real-vendor"
    real_vendor.mkdir()
    (real_vendor / "example.whl").write_bytes(b"wheel")
    (tmp_path / "vendor").symlink_to(real_vendor.name, target_is_directory=True)
    (tmp_path / "uv.lock").write_text(
        'version = 1\nwheels = [{ path = "vendor/example.whl" }]\n',
        encoding="utf-8",
    )

    result = run_validator(tmp_path)

    assert result.returncode == 1
    assert "cannot traverse a symlink" in result.stderr


def test_product_contract_rejects_sandbox_only_uv_registry(
    tmp_path: Path,
) -> None:
    write_valid_project(tmp_path)
    (tmp_path / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text(
        """version = 1

[[package]]
name = "example"
source = { registry = "/opt/software-agent-team/wheels" }
wheels = [
    { path = "/opt/software-agent-team/wheels/example.whl" },
]
""",
        encoding="utf-8",
    )

    result = run_validator(tmp_path)

    assert result.returncode == 1
    assert "non-portable local reference" in result.stderr


def test_product_contract_rejects_a_symlinked_uv_lock(tmp_path: Path) -> None:
    write_valid_project(tmp_path)
    (tmp_path / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    outside = tmp_path / "outside.lock"
    outside.write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "uv.lock").unlink()
    (tmp_path / "uv.lock").symlink_to(outside)

    result = run_validator(tmp_path)

    assert result.returncode == 1
    assert "uv.lock must be a regular file" in result.stderr


def test_product_contract_rejects_an_oversized_uv_lock(tmp_path: Path) -> None:
    write_valid_project(tmp_path)
    (tmp_path / ".gitignore").write_text(".venv/\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_bytes(b"x" * 1_048_577)

    result = run_validator(tmp_path)

    assert result.returncode == 1
    assert "uv.lock is too large" in result.stderr


def test_product_contract_rejects_an_untracked_excluded_runtime_lock(
    tmp_path: Path,
) -> None:
    """An ignored lock cannot stand in for committed resolution metadata."""

    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    commit_project(project)
    subprocess.run(
        ["git", "-C", str(project), "rm", "--cached", "--force", "uv.lock"],
        check=True,
        capture_output=True,
        text=True,
    )
    (project / ".gitignore").write_text(".venv/\nuv.lock\n", encoding="utf-8")
    (project / "uv.lock").write_text(
        'version = 1\nwheels = [{ path = "/opt/software-agent-team/wheels" }]\n',
        encoding="utf-8",
    )

    result = run_validator(project)

    assert result.returncode == 1
    assert (
        "uv.lock must be committed as dependency-resolution metadata" in result.stderr
    )


def test_product_contract_validates_ignored_lock_when_forced_into_delivery(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    commit_project(project)
    (project / "uv.lock").write_text(
        'version = 1\nwheels = [{ path = "/opt/software-agent-team/wheels" }]\n',
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", str(project), "add", "--force", "uv.lock"],
        check=True,
        capture_output=True,
    )

    result = run_validator(project)

    assert result.returncode == 1
    assert "non-portable local reference" in result.stderr


def test_product_contract_requires_the_setup_environment_to_be_ignored(
    tmp_path: Path,
) -> None:
    write_valid_project(tmp_path)
    (tmp_path / ".gitignore").write_text("uv.lock\n", encoding="utf-8")

    result = run_validator(tmp_path)

    assert result.returncode == 1
    assert "root .venv setup directory" in result.stderr


def test_product_contract_rejects_a_negated_venv_ignore_rule(
    tmp_path: Path,
) -> None:
    write_valid_project(tmp_path)
    (tmp_path / ".gitignore").write_text(
        ".venv/\n!.venv/\n",
        encoding="utf-8",
    )

    result = run_validator(tmp_path)

    assert result.returncode == 1
    assert ".gitignore must effectively exclude root .venv" in result.stderr


def test_exact_command_gate_uses_only_committed_files_in_fresh_scratch(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    commit_project(project)
    (project / "local-only.txt").write_text("must not be copied", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_uv(fake_bin)
    log = tmp_path / "uv.jsonl"
    environment = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_UV_LOG": str(log),
        "UV_INDEX_URL": "https://private.invalid/simple",
    }

    result = run_command_validator(project, environment=environment)

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary == {
        "setup": "passed",
        "source": "committed_tracked_files",
        "start": "exited_zero",
        "test": "passed",
        "workspace": "fresh_sandbox_scratch_copy",
    }
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [call["argv"] for call in calls] == [
        ["lock", "--check", "--offline"],
        ["sync", "--dev"],
        ["run", "pytest"],
        ["run", "link-checker", "."],
    ]
    assert calls[0]["uv_config_file"] == "/dev/null"
    assert calls[0]["uv_default_index"] == "https://pypi.org/simple"
    assert calls[0]["uv_index_url"] is None
    assert [call["uv_config_file"] for call in calls] == [
        "/dev/null",
        None,
        None,
        None,
    ]
    assert len({call["cwd"] for call in calls}) == 1
    assert calls[0]["cwd"] != str(project)
    assert not (project / ".venv").exists()


@pytest.mark.parametrize("documented_count", (35, 34))
@pytest.mark.parametrize("label_before_shell", (False, True))
@pytest.mark.parametrize("fixture_command", ("echo", "printf"))
def test_exact_command_gate_checks_documented_json_against_running_cli(
    tmp_path: Path,
    documented_count: int,
    label_before_shell: bool,
    fixture_command: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    readme = project / "README.md"
    fixture = (
        'echo -e "Hello world\\nWelcome to text-stats" > sample.txt\n'
        if fixture_command == "echo"
        else 'printf "Hello world\\nWelcome to text-stats\\n" > sample.txt\n'
    )
    readme.write_text(
        readme.read_text(encoding="utf-8")
        + "\n### Example\n\n"
        + ("Expected JSON output:\n" if label_before_shell else "")
        + "```bash\n"
        + fixture
        + "uv run link-checker . sample.txt\n```\n\n"
        + ("" if label_before_shell else "Expected JSON output:\n")
        + "```json\n"
        + json.dumps({"code_point_count": documented_count})
        + "\n```\n",
        encoding="utf-8",
    )
    commit_project(project)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_uv(fake_bin)
    log = tmp_path / "uv.jsonl"

    result = run_command_validator(
        project,
        environment={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_UV_LOG": str(log),
        },
    )

    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert calls[-1]["argv"] == [
        "run",
        "link-checker",
        ".",
        "sample.txt" if documented_count == 35 else "sat-example-1-mixed-newlines.txt",
    ]
    assert not (project / "sample.txt").exists()
    if documented_count == 35:
        assert result.returncode == 1
        assert "README JSON example 1 output differs" in result.stderr
        assert '"code_point_count": 34' in result.stderr
    else:
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("fixture", "expected_error"),
    (
        (
            r"printf 'caf\xc3\xa9\r\n\xc3\xbcber line\n' > sample.txt",
            "unsupported printf escape",
        ),
        (
            r'echo -ne "café\r\nüber line\n" > sample.txt',
            "unsupported echo flags",
        ),
        (
            r"echo -e 'café\r\nüber line\n\c' > sample.txt",
            "unsupported echo escape",
        ),
        (
            r"printf 'café\r\nüber line\n' > sample.txt",
            None,
        ),
    ),
)
def test_exact_command_gate_guides_historical_readme_fixture_corrections(
    tmp_path: Path, fixture: str, expected_error: str | None
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    readme = project / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8")
        + "\nExpected JSON output:\n```bash\n"
        + fixture
        + "\nuv run link-checker . sample.txt\n```\n```json\n"
        + json.dumps({"code_point_count": 16})
        + "\n```\n",
        encoding="utf-8",
    )
    commit_project(project)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_uv(fake_bin)
    result = run_command_validator(
        project,
        environment={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_UV_LOG": str(tmp_path / "uv.jsonl"),
        },
    )
    if expected_error is None:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode == 1
        assert expected_error in result.stderr
        assert "printf '%s' 'hello world' > example.txt" in result.stderr


def test_exact_command_gate_rejects_normalized_crlf_code_point_count(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    readme = project / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8")
        + "\nExpected JSON output:\n```bash\n"
        + 'printf "Hello world\\nWelcome to text-stats\\n" > sample.txt\n'
        + "uv run link-checker . sample.txt\n```\n```json\n"
        + json.dumps({"code_point_count": 34})
        + "\n```\n",
        encoding="utf-8",
    )
    commit_project(project)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_uv(fake_bin)
    log = tmp_path / "uv.jsonl"

    result = run_command_validator(
        project,
        environment={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_UV_LOG": str(log),
            "FAKE_UV_NORMALIZE_NEWLINES": "1",
        },
    )

    assert result.returncode == 1
    assert "code_point_count miscounts mixed CRLF/LF" in result.stderr
    assert "expected=5, actual=4" in result.stderr
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert calls[-1]["argv"][-1] == "sat-example-1-mixed-newlines.txt"


def test_exact_command_gate_rejects_json_claim_without_executable_example(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    readme = project / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8")
        + "\nExpected JSON output:\n```json\n{}\n```\n",
        encoding="utf-8",
    )
    commit_project(project)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_uv(fake_bin)

    result = run_command_validator(
        project,
        environment={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_UV_LOG": str(tmp_path / "uv.jsonl"),
        },
    )

    assert result.returncode == 1
    assert "needs a preceding shell example" in result.stderr


def test_exact_command_gate_rejects_label_without_json_block(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    readme = project / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8")
        + "\nExpected JSON output:\n```bash\n"
        + 'printf "hello\\n" > sample.txt\n'
        + "uv run link-checker . sample.txt\n```\n",
        encoding="utf-8",
    )
    commit_project(project)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_uv(fake_bin)

    result = run_command_validator(
        project,
        environment={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_UV_LOG": str(tmp_path / "uv.jsonl"),
        },
    )

    assert result.returncode == 1
    assert "needs adjacent shell and JSON blocks" in result.stderr


def test_exact_command_gate_keeps_interactive_start_stdin_open(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    commit_project(project)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_uv(fake_bin)
    log = tmp_path / "uv.jsonl"
    environment = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_UV_LOG": str(log),
        "FAKE_UV_WAIT_FOR_STDIN": "1",
    }

    result = run_command_validator(project, environment=environment)

    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["start"] == "running_after_grace"
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [call["argv"] for call in calls] == [
        ["lock", "--check", "--offline"],
        ["sync", "--dev"],
        ["run", "pytest"],
        ["run", "link-checker", "."],
    ]


@pytest.mark.parametrize("prompt_stream", ("stdout", "stderr"))
def test_exact_command_gate_checks_interactive_json_stdout(
    tmp_path: Path,
    prompt_stream: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    readme = project / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8")
        + "\nExpected JSON output:\n```bash\n"
        + "printf '%s' 'hello world' > sample.txt\n"
        + "uv run link-checker . sample.txt\n```\n"
        + '```json\n{"code_point_count": 11}\n```\n',
        encoding="utf-8",
    )
    commit_project(project)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_uv(fake_bin)
    result = run_command_validator(
        project,
        environment={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_UV_LOG": str(tmp_path / "uv.jsonl"),
            "FAKE_UV_INTERACTIVE_JSON": prompt_stream,
        },
    )
    assert (result.returncode == 0) == (prompt_stream == "stderr"), result.stderr
    if prompt_stream == "stdout":
        assert "interactive start did not emit one JSON value" in result.stderr
        assert "write prompts to stderr" in result.stderr
    calls = [
        json.loads(line)["argv"]
        for line in (tmp_path / "uv.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert calls.count(["run", "link-checker", "."]) == 2


def test_exact_command_gate_still_reports_an_immediate_start_failure(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    commit_project(project)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_uv(fake_bin)
    log = tmp_path / "uv.jsonl"
    environment = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_UV_LOG": str(log),
        "FAKE_UV_FAIL": "run link-checker .",
    }

    result = run_command_validator(project, environment=environment)

    assert result.returncode == 1
    assert "start command failed (exit=32, timed_out=false)" in result.stderr
    assert "completed stages: portable lock check, setup, test" in result.stderr
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [call["argv"] for call in calls] == [
        ["lock", "--check", "--offline"],
        ["sync", "--dev"],
        ["run", "pytest"],
        ["run", "link-checker", "."],
    ]


def test_exact_command_gate_ignores_excluded_runtime_artifact_in_source_workspace(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    (project / ".gitignore").write_text(
        ".venv/\n.local-setup-cache\n",
        encoding="utf-8",
    )
    commit_project(project)
    (project / ".local-setup-cache").write_text("runtime only\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_uv(fake_bin)
    log = tmp_path / "uv.jsonl"
    environment = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_UV_LOG": str(log),
    }

    result = run_command_validator(project, environment=environment)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["source"] == "committed_tracked_files"


def test_exact_command_gate_rejects_an_inconsistent_committed_lock(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    commit_project(project)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_uv(fake_bin)
    environment = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_UV_LOG": str(tmp_path / "uv.jsonl"),
        "FAKE_UV_FAIL": "lock --check --offline",
    }

    result = run_command_validator(project, environment=environment)

    assert result.returncode == 1
    assert "portable lock consistency check failed" in result.stderr


def test_exact_command_gate_rejects_unignored_setup_artifacts(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    commit_project(project)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_uv(fake_bin)
    environment = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_UV_LOG": str(tmp_path / "uv.jsonl"),
        "FAKE_UV_CREATE_UNIGNORED": "1",
    }

    result = run_command_validator(project, environment=environment)

    assert result.returncode == 1
    assert "setup command generated unignored repository artifacts" in result.stderr
    assert "generated.json" in result.stderr


def test_exact_command_gate_reports_setup_failure_without_running_later_commands(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    commit_project(project)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_uv(fake_bin)
    log = tmp_path / "uv.jsonl"
    environment = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_UV_LOG": str(log),
        "FAKE_UV_FAIL": "sync --dev",
    }

    result = run_command_validator(project, environment=environment)

    assert result.returncode == 1
    assert "setup command failed" in result.stderr
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [call["argv"] for call in calls] == [
        ["lock", "--check", "--offline"],
        ["sync", "--dev"],
    ]


def test_exact_command_gate_rejects_modified_tracked_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    commit_project(project)
    readme = (project / "README.md").read_text(encoding="utf-8")
    (project / "README.md").write_text(
        f"{readme}\nPost-commit change.\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_uv(fake_bin)
    environment = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_UV_LOG": str(tmp_path / "uv.jsonl"),
    }

    result = run_command_validator(project, environment=environment)

    assert result.returncode == 1
    assert "differ from the immutable commit" in result.stderr


def test_exact_command_gate_rejects_a_tracked_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    write_valid_project(project)
    (project / "linked-readme").symlink_to("README.md")
    commit_project(project)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    write_fake_uv(fake_bin)
    environment = {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_UV_LOG": str(tmp_path / "uv.jsonl"),
    }

    result = run_command_validator(project, environment=environment)

    assert result.returncode == 1
    assert "tracked entries must be regular files: linked-readme" in result.stderr

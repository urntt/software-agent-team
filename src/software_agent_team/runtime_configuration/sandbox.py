"""Sandbox image inspection, runtime probes, and run configuration materialization."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from software_agent_team.configuration import load_openclaw_template
from software_agent_team.docker_engine import (
    DockerEngineError,
    DockerEngineIdentity,
    container_user,
    current_docker_engine,
    discover_docker_engine,
    engine_environment,
    verify_docker_engine,
)
from software_agent_team.model_runtime import (
    ModelApi,
    ModelRuntimeProfile,
    runtime_profile_for_model,
)
from software_agent_team.submissions import (
    ARTIFACT_SUBMISSION_PLUGIN_ID,
    ARTIFACT_SUBMISSION_TOOL,
    artifact_submission_plugin_path,
)
from software_agent_team.teams import (
    AgentCapability,
    PermissionProfile,
    TeamManifest,
    TeamPlan,
)

from .model_inspection import _apply_model_compatibility
from .preflight import _persist_private_json
from .types import (
    PREFLIGHT_COMMAND_TIMEOUT_SECONDS,
    RuntimeConfigurationError,
    SandboxImageInspection,
    SandboxRuntimeProbe,
    _ArtifactSubmissionMode,
)

_CAPABILITY_TEMPLATE_ROLES = {
    AgentCapability.CLARIFICATION: "clarifier",
    AgentCapability.PLANNING: "planner",
    AgentCapability.IMPLEMENTATION: "generalist_developer",
    AgentCapability.INTEGRATION: "integrator",
    AgentCapability.TESTING: "tester",
    AgentCapability.REVIEW: "reviewer",
}


def require(condition: bool, message: str) -> None:
    """Validate a condition or raise SystemExit in rootless contract scripts."""
    if not condition:
        raise SystemExit(message)


def _current_docker_engine() -> DockerEngineIdentity | None:
    """Resolve docker engine allowing monkeypatching on the package root."""
    import software_agent_team.runtime_configuration as runtime_configuration

    fn = getattr(runtime_configuration, "current_docker_engine", current_docker_engine)
    return fn()


def _verify_docker_engine(engine: DockerEngineIdentity) -> None:
    """Verify docker engine allowing monkeypatching on the package root."""
    import software_agent_team.runtime_configuration as runtime_configuration

    fn = getattr(runtime_configuration, "verify_docker_engine", verify_docker_engine)
    fn(engine)


def inspect_sandbox_image(
    *,
    sandbox_binary: str,
    sandbox_image: str,
    timeout_seconds: int = PREFLIGHT_COMMAND_TIMEOUT_SECONDS,
    environment: Mapping[str, str] | None = None,
) -> SandboxImageInspection:
    """Resolve a local Docker reference without pulling or running an image."""

    if timeout_seconds < 1:
        raise RuntimeConfigurationError("preflight timeout must be positive")
    if (
        not sandbox_image.strip()
        or sandbox_image != sandbox_image.strip()
        or sandbox_image.startswith("-")
        or any(character in sandbox_image for character in ("\x00", "\r", "\n"))
    ):
        raise RuntimeConfigurationError("sandbox image reference is invalid")
    resolved_sandbox = shutil.which(sandbox_binary)
    if resolved_sandbox is None:
        raise RuntimeConfigurationError(
            f"sandbox binary is unavailable: {sandbox_binary}"
        )
    engine = _current_docker_engine()
    if engine is not None:
        _verify_docker_engine(engine)
        environment = engine_environment(
            engine, os.environ if environment is None else environment
        )
    try:
        sandbox_version = subprocess.run(
            [resolved_sandbox, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
        )
        image = subprocess.run(
            [
                resolved_sandbox,
                "image",
                "inspect",
                "--format",
                "{{.Id}}",
                sandbox_image,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeConfigurationError("sandbox preflight command failed") from error

    sandbox_version_text = (
        sandbox_version.stdout.strip() or sandbox_version.stderr.strip()
    )
    if "docker version" not in sandbox_version_text.lower():
        raise RuntimeConfigurationError("sandbox binary is not Docker")
    image_id = image.stdout.strip() if image.returncode == 0 else None
    if image_id is not None and not (
        image_id.startswith("sha256:")
        and len(image_id) == 71
        and all(character in "0123456789abcdef" for character in image_id[7:])
    ):
        raise RuntimeConfigurationError("Docker returned an invalid sandbox image ID")

    return SandboxImageInspection(
        sandbox_binary=resolved_sandbox,
        sandbox_version=sandbox_version_text,
        sandbox_image=sandbox_image,
        sandbox_image_id=image_id,
        sandbox_image_present=image.returncode == 0,
    )


_ROOTLESS_CONTRACT_SCRIPT = """
import os
from pathlib import Path

def require(condition, message):
    if not condition:
        raise SystemExit(message)

require(Path('/workspace/marker').read_text() == 'sat-rootless-probe\\n', 'bind read')
mapping_line = Path('/proc/self/uid_map').read_text().splitlines()[0]
mapping = tuple(int(value) for value in mapping_line.split())
require(mapping == (0, int(os.environ['SAT_HOST_UID']), 1), 'UID mapping')
status_lines = Path('/proc/self/status').read_text().splitlines()
status = dict(line.split(':', 1) for line in status_lines if ':' in line)
require(int(status['CapEff'].strip(), 16) == 0, 'capabilities')
require(status['NoNewPrivs'].strip() == '1', 'no-new-privileges')
memory_limit = Path('/sys/fs/cgroup/memory.max').read_text().strip()
require(memory_limit == str(512 * 1024 * 1024), 'memory limit')
require(Path('/sys/fs/cgroup/pids.max').read_text().strip() == '128', 'PID limit')
quota, period = map(int, Path('/sys/fs/cgroup/cpu.max').read_text().split())
require(quota == period and quota > 0, 'CPU limit')
require(not Path('/sys/class/net/eth0').exists(), 'network isolation')
for target in ('/workspace/write-forbidden', '/etc/sat-write-forbidden'):
    try:
        Path(target).write_text('forbidden')
    except OSError:
        pass
    else:
        raise SystemExit('read-only boundary')
Path('/tmp/sat-write-ok').write_text('ok')
require(Path('/tmp/sat-write-ok').read_text() == 'ok', 'tmpfs write')
print('SAT_ROOTLESS_CONTRACT_OK')
"""


def _probe_rootless_sandbox_contract(
    *,
    sandbox_binary: str,
    sandbox_image_id: str,
    engine: DockerEngineIdentity,
    timeout_seconds: int,
    environment: Mapping[str, str] | None,
) -> None:
    """Prove actual namespace, mount, privilege, and cgroup enforcement."""

    _verify_docker_engine(engine)
    sandbox = tempfile.TemporaryDirectory(prefix="sat-rootless-contract-")
    name = f"sat-rootless-contract-{uuid4().hex}"
    selected_environment = os.environ if environment is None else environment
    try:
        workspace = Path(sandbox.name)
        marker = workspace / "marker"
        marker.write_text("sat-rootless-probe\n", encoding="utf-8")
        marker.chmod(0o600)
        command = [
            sandbox_binary,
            "--host",
            engine.endpoint,
            "run",
            "--rm",
            "--init",
            "--name",
            name,
            "--pull",
            "never",
            "--network",
            "none",
            "--ipc",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "0:0",
            "--pids-limit",
            "128",
            "--memory",
            "512m",
            "--memory-swap",
            "512m",
            "--cpus",
            "1",
            "--ulimit",
            "nofile=1024:1024",
            "--tmpfs",
            "/tmp:rw,exec,nosuid,nodev,size=128m",
            "--mount",
            f"type=bind,source={workspace},target=/workspace,readonly",
            "--env",
            f"SAT_HOST_UID={engine.owner_uid}",
            sandbox_image_id,
            "python3",
            "-c",
            _ROOTLESS_CONTRACT_SCRIPT,
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout_seconds,
                env=selected_environment,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeConfigurationError(
                "rootless Docker isolation probe could not complete"
            ) from error
        if (
            completed.returncode != 0
            or completed.stdout.strip() != "SAT_ROOTLESS_CONTRACT_OK"
        ):
            raise RuntimeConfigurationError(
                "rootless Docker did not enforce the required workspace, "
                "namespace, privilege, or cgroup boundaries"
            )
    finally:
        try:
            _verify_docker_engine(engine)
            subprocess.run(
                [
                    sandbox_binary,
                    "--host",
                    engine.endpoint,
                    "container",
                    "rm",
                    "--force",
                    name,
                ],
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout_seconds,
                env=selected_environment,
            )
            remaining = subprocess.run(
                [
                    sandbox_binary,
                    "--host",
                    engine.endpoint,
                    "container",
                    "inspect",
                    name,
                ],
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=timeout_seconds,
                env=selected_environment,
            )
            if remaining.returncode == 0:
                raise RuntimeConfigurationError(
                    "rootless Docker isolation probe cleanup left a container"
                )
        finally:
            sandbox.cleanup()


def probe_sandbox_runtime(
    *,
    sandbox_binary: str,
    sandbox_image_id: str,
    timeout_seconds: int = PREFLIGHT_COMMAND_TIMEOUT_SECONDS,
    settle_seconds: float = 0.2,
    environment: Mapping[str, str] | None = None,
) -> SandboxRuntimeProbe:
    """Verify that a restricted sandbox can start and execute a tool helper.

    OpenClaw starts a long-lived container and then executes file and process
    helpers inside it. Merely resolving an image or observing a successful
    ``docker run`` is insufficient. This probe supplies OpenClaw's explicit
    supervisor command, applies the same process and resource boundaries, runs
    one Python helper, inspects the resulting state, and removes the container.
    It never calls a model.
    """

    if timeout_seconds < 1:
        raise RuntimeConfigurationError("preflight timeout must be positive")
    if not 0 <= settle_seconds <= 5:
        raise RuntimeConfigurationError("sandbox settle duration is invalid")
    if not (
        sandbox_image_id.startswith("sha256:")
        and len(sandbox_image_id) == 71
        and all(character in "0123456789abcdef" for character in sandbox_image_id[7:])
    ):
        raise RuntimeConfigurationError("sandbox image ID is invalid")
    resolved_sandbox = shutil.which(sandbox_binary)
    if resolved_sandbox is None:
        raise RuntimeConfigurationError(
            f"sandbox binary is unavailable: {sandbox_binary}"
        )
    selected_environment = os.environ if environment is None else environment
    engine = _current_docker_engine()
    if selected_environment.get("SAT_DOCKER_ENGINE_MODE") == "rootless":
        try:
            discovered = discover_docker_engine(selected_environment)
        except DockerEngineError as error:
            raise RuntimeConfigurationError(
                "rootless Docker engine identity could not be verified"
            ) from error
        if not discovered.rootless or (engine is not None and discovered != engine):
            raise RuntimeConfigurationError(
                "the configured Docker engine is not the verified rootless daemon"
            )
        engine = discovered
    sandbox_user = container_user(engine)
    if engine is not None:
        _verify_docker_engine(engine)
        environment = engine_environment(engine, selected_environment)

    container_name = f"sat-runtime-probe-{uuid4().hex}"
    start_attempted = False
    created = False
    ready = False
    error_detail: str | None = None
    try:
        start_attempted = True
        started = subprocess.run(
            [
                resolved_sandbox,
                "run",
                "--detach",
                "--name",
                container_name,
                "--pull",
                "never",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--user",
                sandbox_user,
                "--env",
                "HOME=/tmp",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=128m",
                "--tmpfs",
                "/var/tmp:rw,nosuid,nodev,size=32m",
                "--tmpfs",
                "/run:rw,nosuid,nodev,size=16m",
                "--pids-limit",
                "128",
                "--memory",
                "512m",
                "--memory-swap",
                "512m",
                "--cpus",
                "1",
                "--ulimit",
                "nofile=1024:1024",
                "--workdir",
                "/workspace",
                "--label",
                "software-agent-team.runtime-probe=true",
                sandbox_image_id,
                "sleep",
                "infinity",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=environment,
        )
        created = started.returncode == 0
        if not created:
            error_detail = (
                "Docker could not start the sandbox probe "
                f"(exit code {started.returncode})"
            )
        else:
            time.sleep(settle_seconds)
            tool_check = subprocess.run(
                [
                    resolved_sandbox,
                    "exec",
                    "--workdir",
                    "/workspace",
                    container_name,
                    "sat-probe-run",
                    "--self-test",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=environment,
            )
            lock_tool_check = subprocess.run(
                [
                    resolved_sandbox,
                    "exec",
                    "--workdir",
                    "/workspace",
                    container_name,
                    "sat-project-lock",
                    "--self-test",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=environment,
            )
            inspected = subprocess.run(
                [
                    resolved_sandbox,
                    "container",
                    "inspect",
                    "--format",
                    "{{json .State}}",
                    container_name,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=environment,
            )
            if inspected.returncode != 0:
                error_detail = (
                    "Docker could not inspect the started sandbox probe "
                    f"(exit code {inspected.returncode})"
                )
            else:
                try:
                    state = json.loads(inspected.stdout)
                except (json.JSONDecodeError, TypeError):
                    error_detail = "Docker returned invalid sandbox probe state"
                else:
                    if not isinstance(state, dict):
                        error_detail = "Docker returned invalid sandbox probe state"
                    elif state.get("Running") is True:
                        if tool_check.returncode != 0:
                            error_detail = (
                                "sandbox probe could not execute the Reviewer "
                                f"probe runner (exit_code={tool_check.returncode})"
                            )
                        elif lock_tool_check.returncode != 0:
                            error_detail = (
                                "sandbox probe could not verify the portable "
                                "lock helper "
                                f"(exit_code={lock_tool_check.returncode})"
                            )
                        else:
                            ready = True
                    else:
                        status = str(state.get("Status") or "unknown")
                        exit_code = state.get("ExitCode")
                        oom_killed = state.get("OOMKilled") is True
                        error_detail = (
                            "sandbox probe exited before tool execution "
                            f"(status={status}, exit_code={exit_code}, "
                            f"oom_killed={str(oom_killed).lower()})"
                        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeConfigurationError("sandbox runtime probe failed") from error
    finally:
        if created:
            try:
                removed = subprocess.run(
                    [
                        resolved_sandbox,
                        "container",
                        "rm",
                        "--force",
                        container_name,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    env=environment,
                )
            except (OSError, subprocess.SubprocessError) as cleanup_error:
                raise RuntimeConfigurationError(
                    "sandbox runtime probe cleanup failed"
                ) from cleanup_error
            if removed.returncode != 0:
                raise RuntimeConfigurationError("sandbox runtime probe cleanup failed")
        elif start_attempted:
            with suppress(OSError, subprocess.SubprocessError):
                subprocess.run(
                    [
                        resolved_sandbox,
                        "container",
                        "rm",
                        "--force",
                        container_name,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    env=environment,
                )

    if ready and engine is not None and engine.rootless:
        try:
            _probe_rootless_sandbox_contract(
                sandbox_binary=resolved_sandbox,
                sandbox_image_id=sandbox_image_id,
                engine=engine,
                timeout_seconds=timeout_seconds,
                environment=environment,
            )
        except (DockerEngineError, RuntimeConfigurationError) as error:
            ready = False
            error_detail = str(error)

    return SandboxRuntimeProbe(
        sandbox_binary=resolved_sandbox,
        sandbox_image_id=sandbox_image_id,
        sandbox_container_ready=ready,
        error=error_detail,
    )


def materialize_run_configuration(
    template_path: Path,
    destination: Path,
    *,
    manifest: TeamManifest,
    workspace: Path,
    sandbox_image: str,
    sandbox_memory_mb: int = 512,
    sandbox_cpus: float = 1.0,
    sandbox_pids_limit: int = 128,
    sandbox_open_files: int = 1024,
    sandbox_tmpfs_mb: int = 128,
    sandbox_user: str | None = None,
    model: str | None = None,
    model_runtime_profile: ModelRuntimeProfile | None = None,
    team_plan: TeamPlan | None = None,
    bootstrap_capability: AgentCapability | None = None,
) -> Path:
    """Create a secret-free OpenClaw config bound to one verified workspace.

    The checked-in template owns vetted capability and permission profiles.
    This function emits only approved Agent identities, or one explicitly
    selected pre-execution bootstrap capability, and changes machine-local
    runtime values: workspace, sandbox scope, image, resource limits, and model
    routes. Provider credentials remain in OpenClaw's external state or the
    trusted caller environment.
    """

    if not sandbox_image.strip():
        raise RuntimeConfigurationError("sandbox image must not be blank")
    if not 64 <= sandbox_memory_mb <= 32_768:
        raise RuntimeConfigurationError("sandbox memory limit is invalid")
    if not 0 < sandbox_cpus <= 8:
        raise RuntimeConfigurationError("sandbox CPU limit is invalid")
    if not 8 <= sandbox_pids_limit <= 4096:
        raise RuntimeConfigurationError("sandbox process limit is invalid")
    if not 32 <= sandbox_open_files <= 65_536:
        raise RuntimeConfigurationError("sandbox open-file limit is invalid")
    if not 16 <= sandbox_tmpfs_mb <= 8192:
        raise RuntimeConfigurationError("sandbox tmpfs limit is invalid")
    if model is not None:
        model = model.strip()
        if not model:
            raise RuntimeConfigurationError("run model must not be blank")
    if model is None and model_runtime_profile is not None:
        raise RuntimeConfigurationError(
            "a runtime profile cannot be supplied without its model reference"
        )
    if team_plan is not None and bootstrap_capability is not None:
        raise RuntimeConfigurationError(
            "runtime configuration cannot mix a TeamPlan with bootstrap Planning"
        )
    if bootstrap_capability is not None and bootstrap_capability not in {
        AgentCapability.CLARIFICATION,
        AgentCapability.PLANNING,
    }:
        raise RuntimeConfigurationError(
            "bootstrap runtime requires a clarification or planning capability"
        )
    if team_plan is not None:
        default_route = team_plan.model_routes.get_route(
            team_plan.model_routes.default_route_id
        )
        if model is not None and model != default_route.model:
            raise RuntimeConfigurationError(
                "run model differs from the TeamPlan default route"
            )
        model = default_route.model
        if (
            model_runtime_profile is not None
            and model_runtime_profile != default_route.runtime_profile
        ):
            raise RuntimeConfigurationError(
                "the runtime profile differs from the TeamPlan default route"
            )
        model_runtime_profile = default_route.runtime_profile
    try:
        resolved_workspace = workspace.resolve(strict=True)
    except OSError as error:
        raise RuntimeConfigurationError("run workspace does not exist") from error
    if not resolved_workspace.is_dir() or resolved_workspace.is_symlink():
        raise RuntimeConfigurationError("run workspace must be a real directory")
    host_uid, host_gid = os.getuid(), os.getgid()
    engine = _current_docker_engine()
    if sandbox_user is None:
        if host_uid == 0 or host_gid == 0:
            raise RuntimeConfigurationError(
                "live Agent sandboxes require an unprivileged host user"
            )
        sandbox_uid, sandbox_gid = (
            (0, 0) if engine is not None and engine.rootless else (host_uid, host_gid)
        )
    else:
        parts = sandbox_user.split(":")
        if len(parts) != 2 or not all(part.isdecimal() for part in parts):
            raise RuntimeConfigurationError("sandbox user must use numeric UID:GID")
        sandbox_uid, sandbox_gid = (int(part) for part in parts)
    if (sandbox_uid == 0 or sandbox_gid == 0) and not (
        sandbox_uid == sandbox_gid == 0 and engine is not None and engine.rootless
    ):
        raise RuntimeConfigurationError(
            "live Agent sandboxes require an unprivileged host user"
        )

    config = load_openclaw_template(template_path, manifest)
    # Round-trip through JSON so the caller-owned parsed template is not
    # accidentally mutated through shared nested values.
    payload: dict[str, Any] = json.loads(json.dumps(config))
    tools = payload.setdefault("tools", {})
    # Pinned OpenClaw ships content-aware loop detection disabled by default.
    # SAT enables that runtime guard for every materialized invocation while
    # retaining the pinned runtime's detector set and threshold authority.
    tools["loopDetection"] = {"enabled": True}
    agents = payload["agents"]
    # SAT owns the complete roster; the pinned runtime must not infer a default.
    agents["ownership"] = "explicit"
    for template_agent in agents["list"]:
        template_agent.get("tools", {}).pop("loopDetection", None)
    defaults = agents["defaults"]
    defaults["repoRoot"] = str(resolved_workspace)
    defaults["skipBootstrap"] = True
    # Runtime Agents use isolated, single-purpose sessions. The pinned runtime
    # owns pruning thresholds; SAT selects supported modes and durable state
    # remains in controller-owned typed artifacts rather than chat memory.
    defaults["contextPruning"] = {
        "mode": "cache-ttl",
        "ttl": "2m",
        "hardClear": {
            "enabled": True,
            "placeholder": "[Earlier tool result cleared from active context]",
        },
    }
    defaults["compaction"] = {
        "mode": "default",
        "keepRecentTokens": 12_000,
        "recentTurnsPreserve": 2,
        "identifierPolicy": "strict",
        "midTurnPrecheck": {"enabled": True},
        "postIndexSync": "off",
        "memoryFlush": {"enabled": False},
        "timeoutSeconds": 180,
    }
    artifact_submission_mode: _ArtifactSubmissionMode | None = None
    if bootstrap_capability is not None:
        artifact_submission_mode = "single_tool"
    elif team_plan is not None:
        artifact_submission_mode = "tool_loop"
    if model is not None:
        defaults["model"] = {"primary": model, "fallbacks": []}
        _apply_model_compatibility(
            payload,
            model,
            runtime_profile=model_runtime_profile,
            artifact_submission_mode=artifact_submission_mode,
        )
        profile = model_runtime_profile or runtime_profile_for_model(model)
        if (
            bootstrap_capability is not None
            and profile.api is ModelApi.GOOGLE_GENERATIVE_AI
            and profile.reasoning is True
            and profile.invocation_max_tokens is not None
            and profile.max_output_tokens is not None
        ):
            # A full Planning proposal needs room for model reasoning and its
            # terminal typed tool call. Keep the smaller per-turn limit for
            # execution Agents; the task budget still owns total spend.
            defaults["models"][model]["params"]["maxTokens"] = profile.max_output_tokens
    if team_plan is not None:
        for route in team_plan.model_routes.routes:
            _apply_model_compatibility(
                payload,
                route.model,
                runtime_profile=route.runtime_profile,
                artifact_submission_mode="tool_loop",
            )
    sandbox = defaults["sandbox"]
    sandbox["scope"] = "session"
    docker = sandbox.setdefault("docker", {})
    docker.update(
        {
            "image": sandbox_image,
            "network": "none",
            "readOnlyRoot": True,
            "capDrop": ["ALL"],
            "user": f"{sandbox_uid}:{sandbox_gid}",
            "env": {
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "HOME": "/tmp",
                "LANG": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
                "RUFF_CACHE_DIR": "/tmp/ruff-cache",
                "TMPDIR": "/tmp",
                "XDG_CACHE_HOME": "/tmp/cache",
                "XDG_CONFIG_HOME": "/tmp/config",
            },
            "pidsLimit": sandbox_pids_limit,
            "memory": f"{sandbox_memory_mb}m",
            "memorySwap": f"{sandbox_memory_mb}m",
            "cpus": sandbox_cpus,
            "tmpfs": [
                f"/tmp:rw,nosuid,nodev,size={sandbox_tmpfs_mb}m",
                "/var/tmp:rw,nosuid,nodev,size=32m",
                "/run:rw,nosuid,nodev,size=16m",
            ],
            "ulimits": {
                "nofile": {
                    "soft": sandbox_open_files,
                    "hard": sandbox_open_files,
                }
            },
        }
    )
    if bootstrap_capability is not None or team_plan is not None:
        # SAT binds a single submission tool to the invocation. OpenClaw's
        # automatic tool discovery can hide its schema while a model-specific
        # tool_choice still names it, producing an invalid provider request.
        # Keep the approved tools directly visible for both Planning and runs.
        tools["toolSearch"] = False
        tools["codeMode"] = False
        plugin_path = artifact_submission_plugin_path()
        payload["plugins"] = {
            "enabled": True,
            "allow": [ARTIFACT_SUBMISSION_PLUGIN_ID],
            "deny": [],
            "slots": {"memory": "none"},
            "load": {"paths": [str(plugin_path)]},
            "entries": {
                ARTIFACT_SUBMISSION_PLUGIN_ID: {
                    "enabled": True,
                }
            },
        }
        sandbox_tools = tools.setdefault("sandbox", {}).setdefault("tools", {})
        sandbox_tools["alsoAllow"] = [ARTIFACT_SUBMISSION_TOOL]
    if bootstrap_capability is not None:
        template_role = _CAPABILITY_TEMPLATE_ROLES[bootstrap_capability]
        templates = {agent["id"]: agent for agent in agents["list"]}
        template = templates.get(template_role)
        if not isinstance(template, dict):
            raise RuntimeConfigurationError(
                f"OpenClaw template is missing {template_role}"
            )
        agent = json.loads(json.dumps(template))
        agent["workspace"] = str(resolved_workspace)
        agent["tools"] = {"allow": [ARTIFACT_SUBMISSION_TOOL]}
        sandbox_config = agent.setdefault("sandbox", {})
        sandbox_config["workspaceAccess"] = "ro"
        if model is not None:
            agent["model"] = {"primary": model, "fallbacks": []}
        agents["list"] = [agent]
    elif team_plan is None:
        for agent in agents["list"]:
            agent["workspace"] = str(resolved_workspace)
    else:
        templates = {agent["id"]: agent for agent in agents["list"]}
        dynamic_agents: list[dict[str, Any]] = []
        for spec in team_plan.agents:
            template_role = _CAPABILITY_TEMPLATE_ROLES[spec.capability]
            template = templates.get(template_role)
            if not isinstance(template, dict):
                raise RuntimeConfigurationError(
                    f"OpenClaw template is missing {template_role}"
                )
            agent = json.loads(json.dumps(template))
            agent["id"] = spec.id
            agent["name"] = spec.label
            agent["workspace"] = str(resolved_workspace)
            agent.pop("default", None)
            sandbox_config = agent.setdefault("sandbox", {})
            sandbox_config["workspaceAccess"] = (
                "ro" if spec.permission_profile is PermissionProfile.READ_ONLY else "rw"
            )
            route = team_plan.model_routes.get_route(spec.model_route_id)
            agent["model"] = {"primary": route.model, "fallbacks": []}
            dynamic_agents.append(agent)
        agents["list"] = dynamic_agents

    # The public template keeps a list for strict role registry validation;
    # current OpenClaw consumes keyed entries and no per-Agent default flag.
    agent_entries: dict[str, dict[str, Any]] = {}
    for agent in agents.pop("list"):
        agent_id = agent.pop("id")
        agent.pop("default", None)
        agent_entries[agent_id] = agent
    agents["entries"] = agent_entries

    return _persist_private_json(
        payload,
        destination,
        label="runtime configuration",
    )


__all__ = [
    "_CAPABILITY_TEMPLATE_ROLES",
    "_ROOTLESS_CONTRACT_SCRIPT",
    "_probe_rootless_sandbox_contract",
    "inspect_sandbox_image",
    "materialize_run_configuration",
    "probe_sandbox_runtime",
    "require",
]

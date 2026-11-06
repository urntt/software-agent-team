"""Execution supervisor and evidence collector for quality gates.

Implements QualityGateRunner to sequentially execute fixed gate commands,
enforce aggregate time budgets, persist bounded stdout/stderr evidence, and
produce structured CommandEvidence records.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from software_agent_team.artifacts import CommandEvidence
from software_agent_team.docker_engine import current_docker_engine
from software_agent_team.quality_gates.backends import (
    DockerSandboxBackend,
    SandboxBackend,
    _append_bounded,
    _evidence_tail,
)
from software_agent_team.quality_gates.types import (
    DockerSandboxPolicy,
    QualityGateBudgetExceeded,
    QualityGateConfiguration,
    QualityGateConfigurationError,
    QualityGateDefinition,
    QualityGateEvidenceError,
    SandboxInvocation,
)


class QualityGateRunner:
    """Run fixed gates sequentially and persist existing ``CommandEvidence``."""

    def __init__(
        self,
        configuration: QualityGateConfiguration,
        *,
        run_directory: Path | str,
        workspace: Path | str,
        sandbox_image_id: str | None = None,
        backend: SandboxBackend | None = None,
        allow_test_backends: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
        result_handler: Callable[[CommandEvidence, int, int, int], None] | None = None,
    ) -> None:
        self.configuration = configuration
        try:
            self.run_directory = Path(run_directory).resolve(strict=True)
            self.workspace = Path(workspace).resolve(strict=True)
        except OSError as error:
            raise QualityGateConfigurationError(
                "run directory and workspace must already exist"
            ) from error
        if not self.run_directory.is_dir() or not self.workspace.is_dir():
            raise QualityGateConfigurationError(
                "run directory and workspace must be directories"
            )
        for mount in configuration.input_mounts:
            if mount.source == self.workspace or mount.source.is_relative_to(
                self.workspace
            ):
                raise QualityGateConfigurationError(
                    "trusted quality inputs must be outside the generated workspace"
                )
        if sandbox_image_id is None:
            self.sandbox = configuration.policy.sandbox
        else:
            if re.fullmatch(r"sha256:[0-9a-f]{64}", sandbox_image_id) is None:
                raise QualityGateConfigurationError(
                    "sandbox image ID must be an immutable SHA-256 identity"
                )
            self.sandbox = configuration.policy.sandbox.model_copy(
                update={"image": sandbox_image_id}
            )

        selected_backend: SandboxBackend = backend or DockerSandboxBackend()
        if selected_backend.kind != "docker" and (
            not allow_test_backends
            or selected_backend.kind
            not in {
                "fake",
                "host-test",
            }
        ):
            raise QualityGateConfigurationError(
                "non-Docker backends require allow_test_backends=True"
            )
        self.backend = selected_backend
        self.monotonic = monotonic
        self.result_handler = result_handler

    def _runtime_sandbox(self) -> DockerSandboxPolicy:
        """Resolve the portable host-user policy for the Docker boundary."""

        if self.backend.kind != "docker" or self.sandbox.user != "host":
            return self.sandbox
        uid = os.getuid()
        gid = os.getgid()
        if uid == 0 or gid == 0:
            raise QualityGateConfigurationError(
                "live quality gates require an unprivileged host user"
            )
        engine = current_docker_engine()
        if engine is not None and engine.rootless:
            return self.sandbox.model_copy(update={"user": "0:0"})
        return self.sandbox.model_copy(update={"user": f"{uid}:{gid}"})

    def _working_directory(self, gate: QualityGateDefinition) -> PurePosixPath:
        relative = PurePosixPath(gate.working_directory)
        candidate = (self.workspace / relative).resolve(strict=True)
        if not candidate.is_dir() or not candidate.is_relative_to(self.workspace):
            raise QualityGateConfigurationError(
                f"gate {gate.id} working directory escapes or is not a directory"
            )
        return relative

    def _output_paths(
        self, gate: QualityGateDefinition, iteration: int
    ) -> tuple[Path, Path, str, str]:
        output_directory = (
            self.run_directory / "iterations" / f"{iteration:02d}" / "commands"
        )
        output_directory.mkdir(parents=True, exist_ok=True)
        resolved_directory = output_directory.resolve(strict=True)
        if not resolved_directory.is_relative_to(self.run_directory):
            raise QualityGateEvidenceError(
                "command output directory escapes run directory"
            )
        stem = gate.id.lower()
        stdout = resolved_directory / f"{stem}.stdout.txt"
        stderr = resolved_directory / f"{stem}.stderr.txt"
        stdout_relative = stdout.relative_to(self.run_directory).as_posix()
        stderr_relative = stderr.relative_to(self.run_directory).as_posix()
        return stdout, stderr, stdout_relative, stderr_relative

    @staticmethod
    def _persist_once(path: Path, content: bytes) -> None:
        try:
            with path.open("xb") as destination:
                destination.write(content)
                destination.flush()
                os.fsync(destination.fileno())
        except FileExistsError as error:
            raise QualityGateEvidenceError(
                f"quality-gate evidence already exists: {path.name}"
            ) from error
        except OSError as error:
            raise QualityGateEvidenceError(
                f"cannot persist quality-gate evidence: {path}"
            ) from error

    def run(self, *, iteration: int) -> tuple[CommandEvidence, ...]:
        """Execute every fixed gate once and persist bounded stdout/stderr."""

        if (
            isinstance(iteration, bool)
            or not isinstance(iteration, int)
            or iteration < 1
        ):
            raise QualityGateConfigurationError("iteration must be a positive integer")
        limits = self.configuration.policy.limits
        runtime_sandbox = self._runtime_sandbox()
        started = self.monotonic()
        evidence: list[CommandEvidence] = []
        output_paths = {
            gate.id: self._output_paths(gate, iteration)
            for gate in self.configuration.manifest.gates
        }
        existing = [
            path
            for paths in output_paths.values()
            for path in paths[:2]
            if path.exists()
        ]
        if existing:
            raise QualityGateEvidenceError(
                f"quality-gate evidence already exists: {existing[0].name}"
            )
        gates = self.configuration.manifest.gates
        for index, gate in enumerate(gates, start=1):
            elapsed = self.monotonic() - started
            remaining = limits.total_timeout_seconds - elapsed
            if remaining < 1:
                raise QualityGateBudgetExceeded(
                    "deterministic quality-gate time budget is exhausted"
                )
            gate_timeout = gate.timeout_seconds or limits.command_timeout_seconds
            timeout = min(float(gate_timeout), remaining)
            invocation = SandboxInvocation(
                gate_id=gate.id,
                argv=gate.argv,
                working_directory=self._working_directory(gate),
                workspace=self.workspace,
                input_mounts=self.configuration.input_mounts,
                environment=tuple(runtime_sandbox.environment.items()),
                sandbox=runtime_sandbox,
                limits=limits,
                timeout_seconds=timeout,
            )
            result = self.backend.execute(invocation)
            output_limited = result.output_limit_exceeded
            stdout = result.stdout
            stderr = result.stderr
            if len(stdout) > limits.stdout_max_bytes:
                buffer = bytearray()
                _append_bounded(buffer, stdout, limits.stdout_max_bytes)
                stdout = bytes(buffer)
                output_limited = True
            if len(stderr) > limits.stderr_max_bytes:
                buffer = bytearray()
                _append_bounded(buffer, stderr, limits.stderr_max_bytes)
                stderr = bytes(buffer)
                output_limited = True

            stdout_path, stderr_path, stdout_relative, stderr_relative = output_paths[
                gate.id
            ]
            self._persist_once(stdout_path, stdout)
            try:
                self._persist_once(stderr_path, stderr)
            except QualityGateEvidenceError:
                stdout_path.unlink(missing_ok=True)
                raise

            exit_code = result.exit_code
            timed_out = result.timed_out
            if output_limited and not timed_out:
                exit_code = 137
            if timed_out:
                summary = f"Timed out after {result.duration_ms} ms."
            elif output_limited:
                summary = "Sandbox output exceeded the configured limit."
            elif exit_code == 0:
                summary = "Deterministic quality gate passed."
            else:
                summary = f"Deterministic quality gate exited with code {exit_code}."
            stdout_tail, stdout_truncated = _evidence_tail(stdout)
            stderr_tail, stderr_truncated = _evidence_tail(stderr)
            command = CommandEvidence(
                id=gate.id,
                argv=gate.argv,
                criterion_ids=gate.criterion_ids,
                exit_code=exit_code,
                timed_out=timed_out,
                duration_ms=result.duration_ms,
                stdout_path=stdout_relative,
                stderr_path=stderr_relative,
                stdout_tail=stdout_tail,
                stderr_tail=stderr_tail,
                stdout_truncated=stdout_truncated,
                stderr_truncated=stderr_truncated,
                summary=summary,
            )
            evidence.append(command)
            if self.result_handler is not None:
                self.result_handler(command, iteration, index, len(gates))
        return tuple(evidence)

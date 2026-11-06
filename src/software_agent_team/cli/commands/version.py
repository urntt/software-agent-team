"""CLI commands for software version, channels, and managed install transitions."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from pydantic import BaseModel

from software_agent_team.cli.constants import PROJECT_ROOT
from software_agent_team.managed_install import (
    ManagedChannel,
    ManagedInstallPaths,
    finalize_staged_sandbox_image_transition,
    prepare_staged_sandbox_image_transition,
    resolve_dev_target,
    target_from_stable_release,
)
from software_agent_team.releases import (
    ReleaseResolutionError,
)
from software_agent_team.schema_compatibility import (
    CandidateCompatibilityEnvelope,
    inspect_candidate_persisted_state,
)
from software_agent_team.updates import (
    ManagedChangePlan,
    ManagedChangeStatus,
    plan_managed_change,
    validate_current_managed_install,
)
from software_agent_team.versioning import (
    IdentityStatus,
    SoftwareVersionReport,
    render_version_report,
)


def _load_json_model[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    """Read a JSON file and validate it as a Pydantic model."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    return model.model_validate(payload)


def _software_version_report() -> SoftwareVersionReport:
    """Return the one authoritative release and provenance report."""

    import software_agent_team.cli as cli

    return cli.inspect_software_version(project_root=cli.PROJECT_ROOT)


def _show_version(args: argparse.Namespace) -> int:
    import software_agent_team.cli as cli

    report = cli._software_version_report()
    if args.json:
        print(json.dumps(report.model_dump(mode="json"), indent=2))
    else:
        print(render_version_report(report))
    return 0


def _managed_install_context():
    """Validate and return the active managed identity for this process."""

    import software_agent_team.cli as cli

    paths = ManagedInstallPaths.from_environment()
    record, _marker = validate_current_managed_install(
        project_root=cli.PROJECT_ROOT,
        paths=paths,
    )
    return paths, record


def _render_managed_change(plan: ManagedChangePlan) -> None:
    print(f"status: {plan.status.value}")
    print(
        "current: "
        f"{plan.current_channel.value} {plan.current_version} "
        f"g{plan.current_revision[:12]}"
    )
    target_version = plan.target_version or "candidate"
    print(
        "target: "
        f"{plan.target_channel.value} {target_version} "
        f"g{plan.target_revision[:12]} ({plan.target_ref})"
    )
    print(f"detail: {plan.detail}")


def _resolve_managed_change(
    *,
    channel: ManagedChannel,
    dev_ref: str | None,
):
    import software_agent_team.cli as cli

    paths, record = cli._managed_install_context()
    target = cli.resolve_requested_target(
        record=record,
        channel=channel,
        dev_ref=dev_ref,
    )
    return paths, record, target, plan_managed_change(record, target)


def _update_managed_install(args: argparse.Namespace) -> int:
    import software_agent_team.cli as cli

    paths, record = cli._managed_install_context()
    target = cli.resolve_requested_target(
        record=record,
        channel=record.channel,
    )
    plan = plan_managed_change(record, target)
    if args.json:
        if not args.check:
            raise ValueError("--json is supported only with `sat update --check`")
        print(json.dumps(plan.model_dump(mode="json"), indent=2))
        return 0
    _render_managed_change(plan)
    if args.check:
        return 0
    return _apply_managed_change(
        paths=paths,
        target=target,
        plan=plan,
        assume_yes=args.yes,
    )


def _show_channel(args: argparse.Namespace) -> int:
    import software_agent_team.cli as cli

    _paths, record = cli._managed_install_context()
    payload = {
        "channel": record.channel.value,
        "release_version": record.release_version,
        "source_revision": record.source_revision,
        "source_ref": record.source_ref,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"channel: {record.channel.value}")
        print(f"release: {record.release_version}")
        print(f"revision: {record.source_revision}")
        print(f"source ref: {record.source_ref}")
    return 0


def _switch_channel(args: argparse.Namespace) -> int:
    import software_agent_team.cli as cli

    requested = ManagedChannel(args.channel)
    paths, record = cli._managed_install_context()
    if requested is ManagedChannel.STABLE and args.ref is not None:
        raise ValueError("--ref is available only when switching to dev")
    if requested is record.channel and args.ref is None:
        print(f"channel is already {requested.value}; use `sat update` to refresh it")
        return 0
    target = cli.resolve_requested_target(
        record=record,
        channel=requested,
        dev_ref=args.ref,
    )
    plan = plan_managed_change(record, target)
    _render_managed_change(plan)
    return _apply_managed_change(
        paths=paths,
        target=target,
        plan=plan,
        assume_yes=args.yes,
    )


def _apply_managed_change(
    *,
    paths: ManagedInstallPaths,
    target,
    plan: ManagedChangePlan,
    assume_yes: bool,
) -> int:
    import software_agent_team.cli as cli

    if plan.status is ManagedChangeStatus.INCONSISTENT:
        raise RuntimeError(plan.detail)
    if not plan.requires_activation:
        print("No managed application change was made.")
        return 0
    if not assume_yes:
        if not sys.stdin.isatty():
            raise ValueError("interactive confirmation is required; use --yes")
        if not cli._prompt_yes_no(
            "Stage, verify, and activate this target?", default=False
        ):
            print("Managed application change cancelled.")
            return 0
    installed = cli.install_managed_target(target, paths)
    print(
        "activated: "
        f"{installed.channel.value} {installed.release_version} "
        f"g{installed.source_revision[:12]}"
    )
    print("rollback: the previous application remains preserved")
    return 0


def _bootstrap_managed_install(args: argparse.Namespace) -> int:
    """Install one managed target for the minimal remote bootstrap helper."""

    import software_agent_team.cli as cli

    channel = ManagedChannel(args.channel)
    if channel is ManagedChannel.STABLE:
        if args.ref is not None:
            raise ValueError("a source ref cannot override the stable release")
        try:
            target = target_from_stable_release(
                cli.resolve_latest_stable_release(
                    release_api_url=args.release_api_url,
                    expected_repository_url=args.repository,
                )
            )
        except ReleaseResolutionError as error:
            raise ReleaseResolutionError(
                "stable channel could not resolve a verified published release: "
                f"{error}. SAT did not install or substitute a dev build. Check "
                "the published release and network, or deliberately rerun the "
                "bootstrap with SAT_INSTALL_CHANNEL=dev"
            ) from error
    else:
        target = resolve_dev_target(
            repository_url=args.repository,
            source_ref=args.ref or "main",
        )
    paths = ManagedInstallPaths.from_environment()
    target_version = target.release_version or "candidate"
    print(
        "bootstrap target: "
        f"{target.channel.value} {target_version} "
        f"g{target.source_revision[:12]} ({target.source_ref})"
    )
    installed = cli.install_managed_target(target, paths)
    print(
        "bootstrap activated: "
        f"{installed.channel.value} {installed.release_version} "
        f"g{installed.source_revision[:12]}"
    )
    return 0


def _inspect_candidate_managed_state(args: argparse.Namespace) -> int:
    """Emit the staged candidate's own persisted-state compatibility result."""

    import software_agent_team.cli as cli

    paths = {
        "configuration": args.configuration_path,
        "installation record": args.installation_record_path,
        "state root": args.state_root,
    }
    for label, path in paths.items():
        if (
            not path.is_absolute()
            or path == Path(path.anchor)
            or Path(os.path.normpath(path)) != path
        ):
            raise ValueError(f"candidate {label} path must be specific and absolute")
    version = cli.inspect_software_version(
        project_root=cli.PROJECT_ROOT,
        environment={"SAT_INSTALL_METADATA_PATH": str(args.installation_record_path)},
    )
    if (
        version.source_revision != args.expected_source_revision
        or version.dirty is not False
        or version.identity_status is not IdentityStatus.VERIFIED
    ):
        raise ValueError(
            "candidate runtime does not match the expected immutable source revision"
        )
    envelope: CandidateCompatibilityEnvelope = inspect_candidate_persisted_state(
        source_revision=args.expected_source_revision,
        configuration_path=args.configuration_path,
        installation_record_path=args.installation_record_path,
        state_root=args.state_root,
    )
    print(envelope.model_dump_json())
    return 0


def _record_managed_image_transition(args: argparse.Namespace) -> int:
    """Capture one target-owned Docker transition around its managed build."""

    operation = {
        "prepare": prepare_staged_sandbox_image_transition,
        "finalize": finalize_staged_sandbox_image_transition,
    }[args.phase]
    operation(
        application_root=PROJECT_ROOT,
        installation_record_path=args.installation_record_path,
    )
    return 0

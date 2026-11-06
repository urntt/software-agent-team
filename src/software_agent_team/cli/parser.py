"""Command-line argument parser construction for the SAT CLI."""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from software_agent_team.managed_install import ManagedChannel
from software_agent_team.model_routing import ModelRoutingMode
from software_agent_team.progress import (
    RunEventVisibility,
    TerminalColorMode,
    TerminalProgressDisplay,
)
from software_agent_team.releases import DEFAULT_LATEST_RELEASE_API_URL
from software_agent_team.teams import AgentCapability

from .commands.configure import _configure
from .commands.validation import (
    _cleanup_owned_resources,
    _list_teams,
    _preflight,
    _prepare_benchmark,
    _validate_artifact,
    _validate_config,
    _validate_handoff,
    _validate_task_brief,
)
from .commands.version import (
    _bootstrap_managed_install,
    _inspect_candidate_managed_state,
    _record_managed_image_transition,
    _show_channel,
    _show_version,
    _switch_channel,
    _update_managed_install,
)
from .constants import (
    DEFAULT_BENCHMARK,
    DEFAULT_BENCHMARK_SEED,
    DEFAULT_OPENCLAW_CONFIG,
    DEFAULT_RUN_POLICY,
    DEFAULT_RUNS_ROOT,
    DEFAULT_TEAM_CONFIG,
    DEFAULT_WORKSPACES_ROOT,
)
from .workflow import _run_workflow


def build_parser() -> argparse.ArgumentParser:
    """Build the product CLI parser for implemented commands."""

    parser = argparse.ArgumentParser(
        prog="sat",
        description=(
            "Build a confirmed software request with a guided Agent team, "
            "or use an explicit subcommand for configuration and evaluation."
        ),
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Show the installed SAT release and exact source identity, then exit.",
    )
    parser.add_argument(
        "--progress-visibility",
        choices=tuple(item.value for item in RunEventVisibility),
        help="Override progress detail for this guided run only.",
    )
    parser.add_argument(
        "--progress-display",
        choices=tuple(item.value for item in TerminalProgressDisplay),
        help="Override automatic, live, or append-only progress for this run only.",
    )
    parser.add_argument(
        "--progress-color",
        choices=tuple(item.value for item in TerminalColorMode),
        help="Override automatic, preferred, or disabled color for this run only.",
    )
    commands = parser.add_subparsers(dest="command")

    commands.add_parser(
        "uninstall",
        help="Remove the owned installation; preserve saved data by default.",
        add_help=False,
        description="Use sat uninstall --help for lifecycle options.",
    )

    version = commands.add_parser(
        "version",
        help="Show detailed, non-networked release and provenance information.",
    )
    version.add_argument(
        "--json",
        action="store_true",
        help="Emit the version report as machine-readable JSON.",
    )
    version.set_defaults(handler=_show_version)

    update = commands.add_parser(
        "update",
        help="Check or apply an update from the active managed channel.",
    )
    update.add_argument(
        "--check",
        action="store_true",
        help="Resolve and report the current channel target without changing files.",
    )
    update.add_argument(
        "--yes",
        action="store_true",
        help="Apply an available update without interactive confirmation.",
    )
    update.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable output; requires --check.",
    )
    update.set_defaults(handler=_update_managed_install)

    channel = commands.add_parser(
        "channel",
        help="Inspect or explicitly switch the managed release channel.",
    )
    channel_commands = channel.add_subparsers(dest="channel_command", required=True)
    channel_status = channel_commands.add_parser(
        "status",
        help="Show the current managed channel without a network request.",
    )
    channel_status.add_argument(
        "--json",
        action="store_true",
        help="Emit the local channel identity as JSON.",
    )
    channel_status.set_defaults(handler=_show_channel)
    channel_switch = channel_commands.add_parser(
        "switch",
        help="Stage, verify, and explicitly activate stable or dev.",
    )
    channel_switch.add_argument(
        "channel",
        choices=tuple(item.value for item in ManagedChannel),
    )
    channel_switch.add_argument(
        "--ref",
        help=(
            "Dev branch, tag, or exact commit; defaults to main on the first "
            "switch and can retarget an existing dev installation."
        ),
    )
    channel_switch.add_argument(
        "--yes",
        action="store_true",
        help="Apply the channel switch without interactive confirmation.",
    )
    channel_switch.set_defaults(handler=_switch_channel)

    cleanup = commands.add_parser(
        "cleanup",
        help="Inspect or reclaim only proven orphaned SAT runtime resources.",
    )
    cleanup.add_argument(
        "--orphans",
        action="store_true",
        help=(
            "Stop proven orphaned SAT process groups, remove their exact sandbox "
            "sessions, and discard stale leases."
        ),
    )
    cleanup.add_argument(
        "--yes",
        action="store_true",
        help="Apply orphan cleanup without an interactive confirmation.",
    )
    cleanup.set_defaults(handler=_cleanup_owned_resources)

    managed_bootstrap = commands.add_parser(
        "_managed-install",
        help=argparse.SUPPRESS,
    )
    managed_bootstrap.add_argument(
        "--channel",
        required=True,
        choices=tuple(item.value for item in ManagedChannel),
    )
    managed_bootstrap.add_argument("--repository", required=True)
    managed_bootstrap.add_argument("--ref")
    managed_bootstrap.add_argument(
        "--release-api-url",
        default=DEFAULT_LATEST_RELEASE_API_URL,
    )
    managed_bootstrap.set_defaults(handler=_bootstrap_managed_install)

    candidate_compatibility = commands.add_parser(
        "_managed-state-compatibility",
        help=argparse.SUPPRESS,
    )
    candidate_compatibility.add_argument("--expected-source-revision", required=True)
    candidate_compatibility.add_argument(
        "--configuration-path",
        required=True,
        type=Path,
    )
    candidate_compatibility.add_argument(
        "--installation-record-path",
        required=True,
        type=Path,
    )
    candidate_compatibility.add_argument("--state-root", required=True, type=Path)
    candidate_compatibility.set_defaults(handler=_inspect_candidate_managed_state)

    image_transition = commands.add_parser(
        "_managed-image-transition",
        help=argparse.SUPPRESS,
    )
    image_transition.add_argument("phase", choices=("prepare", "finalize"))
    image_transition.add_argument(
        "--installation-record-path",
        required=True,
        type=Path,
    )
    image_transition.set_defaults(handler=_record_managed_image_transition)

    configure = commands.add_parser(
        "configure",
        help="Create or replace secret-free model and advanced run defaults.",
    )
    configure.add_argument(
        "--show",
        action="store_true",
        help="Show saved defaults and the run guide without changing anything.",
    )
    configure.add_argument(
        "--non-interactive",
        action="store_true",
        help="Never prompt; useful when supplying configuration flags in scripts.",
    )
    configure.add_argument("--model")
    configure.add_argument("--input-cost-per-million-usd", type=Decimal)
    configure.add_argument("--output-cost-per-million-usd", type=Decimal)
    configure.add_argument(
        "--add-model-profile",
        action="append",
        default=[],
        metavar="ID=PROVIDER/MODEL",
        help="Add one secret-free model profile; repeat for multiple profiles.",
    )
    configure.add_argument(
        "--remove-model-profile",
        action="append",
        default=[],
        metavar="ID",
        help="Remove a non-default model profile and its saved routes.",
    )
    configure.add_argument(
        "--profile-capabilities",
        action="append",
        default=[],
        metavar="ID=CAPABILITY,...",
        help=(
            "Declare which SAT capabilities a profile is authorized to serve; "
            "repeat as needed."
        ),
    )
    configure.add_argument(
        "--profile-priority",
        action="append",
        default=[],
        metavar="ID=INTEGER",
        help="Set deterministic auto-selection priority (smaller is preferred).",
    )
    configure.add_argument(
        "--profile-pricing",
        action="append",
        default=[],
        metavar="ID=INPUT,OUTPUT",
        help="Set optional USD prices per million tokens for one profile.",
    )
    configure.add_argument(
        "--profile-cache-pricing",
        action="append",
        default=[],
        metavar="ID=READ,WRITE",
        help="Set explicit USD cache read/write rates per million input tokens.",
    )
    configure.add_argument(
        "--profile-api",
        action="append",
        default=[],
        metavar="ID=API",
        help=(
            "Set a pinned OpenClaw transport such as openai-responses, "
            "anthropic-messages, or ollama."
        ),
    )
    configure.add_argument(
        "--profile-endpoint-kind",
        action="append",
        default=[],
        choices=None,
        metavar="ID=remote|local",
        help="Classify a custom endpoint for network-policy validation.",
    )
    configure.add_argument(
        "--profile-base-url",
        action="append",
        default=[],
        metavar="ID=URL",
        help="Set a custom remote HTTPS or local loopback endpoint.",
    )
    configure.add_argument(
        "--profile-native-model-id",
        action="append",
        default=[],
        metavar="ID=MODEL",
        help="Set the provider-native model ID exposed at the endpoint.",
    )
    configure.add_argument(
        "--profile-credential-env",
        action="append",
        default=[],
        metavar="ID=ENV",
        help="Reference a credential environment variable without storing its value.",
    )
    configure.add_argument(
        "--profile-context-window-tokens",
        action="append",
        default=[],
        metavar="ID=INTEGER",
        help="Set provider-documented context length when it cannot be discovered.",
    )
    configure.add_argument(
        "--profile-max-output-tokens",
        action="append",
        default=[],
        metavar="ID=INTEGER",
        help="Set the provider model's maximum output length.",
    )
    configure.add_argument(
        "--profile-input-modalities",
        action="append",
        default=[],
        metavar="ID=TEXT,IMAGE,...",
        help="Declare provider-documented input modalities.",
    )
    configure.add_argument(
        "--profile-supports-tools",
        action="append",
        default=[],
        metavar="ID=true|false",
        help="Declare provider-documented tool-calling capability.",
    )
    configure.add_argument(
        "--profile-request-timeout-seconds",
        action="append",
        default=[],
        metavar="ID=INTEGER",
        help="Set the provider's liveness timeout for one request.",
    )
    configure.add_argument(
        "--default-model-profile",
        metavar="ID",
        help="Select the profile used for bootstrap Planning and default routing.",
    )
    configure.add_argument(
        "--routing-mode",
        choices=tuple(mode.value for mode in ModelRoutingMode),
        help="Use strict pinning or approved deterministic policy routing.",
    )
    configure.add_argument(
        "--route-capability",
        action="append",
        default=[],
        metavar="CAPABILITY=PROFILE",
        help="Set an explicit profile for one Agent capability; repeat as needed.",
    )
    configure.add_argument(
        "--clear-capability-route",
        action="append",
        default=[],
        choices=tuple(capability.value for capability in AgentCapability),
        metavar="CAPABILITY",
        help="Remove one saved capability-specific route.",
    )
    configure.add_argument(
        "--route-stage",
        action="append",
        default=[],
        metavar="STAGE=PROFILE",
        help="Set an explicit profile for a planned stage ID.",
    )
    configure.add_argument(
        "--clear-stage-route",
        action="append",
        default=[],
        metavar="STAGE",
        help="Remove one saved stage-specific route.",
    )
    provider_switch = configure.add_mutually_exclusive_group()
    provider_switch.add_argument(
        "--allow-provider-switch",
        action="store_true",
        help=(
            "Authorize fallback through the finite configured route list only after "
            "an attributable provider failure."
        ),
    )
    provider_switch.add_argument(
        "--disable-provider-switch",
        action="store_true",
        help="Remove provider-failure switch authorization.",
    )
    configure.add_argument(
        "--clear-model-routing",
        action="store_true",
        help="Return to one strict default profile and remove all route overrides.",
    )
    configure.add_argument(
        "--max-concurrency",
        type=int,
        help=(
            "Maximum ready Agents the adaptive scheduler may run concurrently; "
            "shared-workspace writer safety can reduce actual concurrency."
        ),
    )
    configure.add_argument(
        "--progress-visibility",
        choices=tuple(item.value for item in RunEventVisibility),
        help=(
            "Select compact, standard, or detailed controller-backed progress "
            "without changing execution behavior."
        ),
    )
    configure.add_argument(
        "--progress-display",
        choices=tuple(item.value for item in TerminalProgressDisplay),
        help=(
            "Use automatic TTY detection, a bounded live panel, or append-only "
            "log rendering."
        ),
    )
    configure.add_argument(
        "--progress-color",
        choices=tuple(item.value for item in TerminalColorMode),
        help="Use automatic color detection, prefer color, or disable color.",
    )
    configure.set_defaults(handler=_configure)

    handoff = commands.add_parser(
        "validate-handoff",
        help="Validate a persisted handoff envelope.",
    )
    handoff.add_argument("path", type=Path)
    handoff.add_argument("--teams", type=Path, default=DEFAULT_TEAM_CONFIG)
    handoff.set_defaults(handler=_validate_handoff)

    task_brief = commands.add_parser(
        "validate-task-brief",
        help="Validate a clarified task brief.",
    )
    task_brief.add_argument("path", type=Path)
    task_brief.set_defaults(handler=_validate_task_brief)

    artifact = commands.add_parser(
        "validate-artifact",
        help="Validate the structure of a persisted phase artifact.",
    )
    artifact.add_argument("path", type=Path)
    artifact.set_defaults(handler=_validate_artifact)

    config = commands.add_parser(
        "validate-config",
        help="Validate all checked-in team, runtime, policy, and benchmark config.",
    )
    config.add_argument("--teams", type=Path, default=DEFAULT_TEAM_CONFIG)
    config.add_argument("--openclaw", type=Path, default=DEFAULT_OPENCLAW_CONFIG)
    config.add_argument("--policy", type=Path, default=DEFAULT_RUN_POLICY)
    quality_manifest = config.add_mutually_exclusive_group()
    quality_manifest.add_argument(
        "--quality-manifest",
        type=Path,
        help="Quality profile or evaluation manifest; defaults to the benchmark.",
    )
    quality_manifest.add_argument(
        "--benchmark",
        type=Path,
        help="Compatibility spelling for an evaluation quality manifest.",
    )
    config.set_defaults(handler=_validate_config)

    teams = commands.add_parser(
        "list-teams",
        help="List versioned experimental team configurations.",
    )
    teams.add_argument("--config", type=Path, default=DEFAULT_TEAM_CONFIG)
    teams.set_defaults(handler=_list_teams)

    benchmark = commands.add_parser(
        "prepare-benchmark",
        help="Create the fixed task-manager seed as a clean Git repository.",
    )
    benchmark.add_argument("destination", type=Path)
    benchmark.add_argument("--seed", type=Path, default=DEFAULT_BENCHMARK_SEED)
    benchmark.add_argument("--author-name", default="urntt")
    benchmark.add_argument("--author-email", default="urntts@gmail.com")
    benchmark.set_defaults(handler=_prepare_benchmark)

    preflight = commands.add_parser(
        "preflight",
        help=(
            "Check the source repository, OpenClaw configuration, and sandbox image "
            "without a model call."
        ),
    )
    preflight.add_argument("source_repository", type=Path)
    preflight.add_argument("--base-ref", default="HEAD")
    preflight.add_argument("--teams", type=Path, default=DEFAULT_TEAM_CONFIG)
    preflight.add_argument("--policy", type=Path, default=DEFAULT_RUN_POLICY)
    preflight.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
    preflight.add_argument("--sandbox-binary", default="docker")
    preflight.set_defaults(handler=_preflight)

    run = commands.add_parser(
        "run",
        help="Execute the complete function-specialized Phase 1 workflow.",
    )
    run.add_argument("task_brief", type=Path)
    run.add_argument("source_repository", type=Path)
    run.add_argument("--base-ref", default="HEAD")
    run.add_argument("--teams", type=Path, default=DEFAULT_TEAM_CONFIG)
    run.add_argument(
        "--team",
        choices=("function_specialized", "single_agent"),
        default="function_specialized",
        help=(
            "Fixed evaluation topology; single_agent requires "
            "deterministic-only acceptance."
        ),
    )
    run.add_argument("--policy", type=Path, default=DEFAULT_RUN_POLICY)
    run.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
    run.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    run.add_argument("--workspaces-root", type=Path, default=DEFAULT_WORKSPACES_ROOT)
    run.add_argument("--sandbox-binary", default="docker")
    run.add_argument(
        "--model",
        help="Exact OpenClaw model reference; defaults to 'sat configure' value.",
    )
    run.add_argument(
        "--input-cost-per-million-usd",
        type=Decimal,
        help="Exact input price; defaults to 'sat configure' value.",
    )
    run.add_argument(
        "--cache-read-cost-per-million-usd",
        type=Decimal,
        help="Explicit cache read rate; paired with the cache write rate.",
    )
    run.add_argument(
        "--cache-write-cost-per-million-usd",
        type=Decimal,
        help="Explicit cache write rate; defaults to the matching saved profile pair.",
    )
    run.add_argument(
        "--output-cost-per-million-usd",
        type=Decimal,
        help="Exact output price; defaults to 'sat configure' value.",
    )
    run_timeout = run.add_mutually_exclusive_group()
    run_timeout.add_argument(
        "--stage-timeout-seconds",
        type=int,
        help=(
            "Global timeout for each Agent invocation, applied independently "
            "to one bounded response correction; defaults to the saved override "
            "or checked-in role defaults."
        ),
    )
    run_timeout.add_argument(
        "--agent-timeout-seconds",
        dest="deprecated_agent_timeout_seconds",
        type=int,
        help=argparse.SUPPRESS,
    )
    run_timeout.add_argument(
        "--use-role-timeouts",
        action="store_true",
        help="Ignore a saved global override for this run and use role defaults.",
    )
    run.add_argument("--artifact-repair-limit", type=int, choices=(0, 1), default=1)
    run.add_argument(
        "--verification-concurrency",
        type=int,
        choices=(1, 2),
        help=(
            "Maximum concurrent Tester/Reviewer calls; defaults to the configured "
            "value or 2. Use 1 for providers with one generation slot."
        ),
    )
    run.set_defaults(handler=_run_workflow)

    return parser

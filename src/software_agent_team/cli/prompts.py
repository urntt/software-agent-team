"""Interactive user prompts and terminal input utilities for CLI commands."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from software_agent_team.runtime_configuration import RuntimeConfigurationError
from software_agent_team.self_check import (
    TaskModelMetadata,
    TaskResourceAuthorization,
)
from software_agent_team.terminal_input import DEFAULT_TERMINAL_INPUT
from software_agent_team.user_configuration import UserConfiguration


def _prompt_value(label: str, current: object | None = None) -> str:
    suffix = "" if current is None else f" [{current}]"
    response = DEFAULT_TERMINAL_INPUT.read_line(f"{label}{suffix}: ").strip()
    if response:
        return response
    if current is None:
        raise ValueError(f"{label} is required")
    return str(current)


def _prompt_product_text(
    prompt: str,
    *,
    label: str,
    required: bool = False,
    maximum_length: int | None = None,
    multiline: bool = True,
) -> str:
    """Read editable product text without discarding a failed validation draft."""

    def validate(value: str) -> str | None:
        response = value.strip()
        try:
            response.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return f"Invalid terminal text in {label}; please edit it."
        if required and not response:
            return f"{label.capitalize()} must not be blank; please edit it."
        if maximum_length is not None and len(response) > maximum_length:
            return (
                f"{label.capitalize()} must be at most {maximum_length} "
                "characters; please shorten it."
            )
        return None

    reader = (
        DEFAULT_TERMINAL_INPUT.read_text
        if multiline
        else DEFAULT_TERMINAL_INPUT.read_line
    )
    return reader(prompt, validate=validate).strip()


def _read_short(prompt: str) -> str:
    """Read one editable decision or scalar value."""

    return DEFAULT_TERMINAL_INPUT.read_line(prompt)


def _read_natural_text(prompt: str) -> str:
    """Read one editable multiline natural-language answer."""

    return DEFAULT_TERMINAL_INPUT.read_text(prompt).strip()


def _prompt_nonnegative_decimal(label: str) -> Decimal:
    """Read one explicit finite USD price without treating blank as zero."""

    while True:
        raw = _read_short(f"{label}: ").strip()
        try:
            value = Decimal(raw)
        except Exception:
            print("Enter a non-negative decimal amount in USD.")
            continue
        if not value.is_finite() or value < 0:
            print("Enter a finite non-negative USD amount.")
            continue
        return value


def _prompt_task_cost_ceiling() -> Decimal:
    """Ask for the one user-owned aggregate budget before any model call."""

    print("\nTask model budget")
    print("  This is a maximum authorized spend, not a predicted final cost.")
    print("  $5.00 is a starting suggestion for a small project; change it freely.")
    while True:
        raw = _read_short(
            "Maximum total model spend for this task (USD) [5.00]: "
        ).strip()
        if not raw:
            return Decimal("5.00")
        try:
            value = Decimal(raw)
        except Exception:
            print("Enter a non-negative decimal amount in USD.")
            continue
        if value.is_finite() and value >= 0:
            return value
        print("Enter a finite non-negative USD amount.")


def _prompt_optional_run_deadline() -> int | None:
    """Ask whether a real whole-run deadline exists; default to none."""

    print("\nWhole-run deadline")
    print("  No deadline is recommended unless you have an actual time limit.")
    if not _prompt_yes_no("Set a deadline for this task?", default=False):
        return None
    while True:
        raw = _read_short("Maximum whole-run time in minutes [120]: ").strip() or "120"
        if raw.isdecimal() and 1 <= int(raw) <= 525_600:
            return int(raw) * 60
        print("Enter a whole number of minutes between 1 and 525600.")


def _collect_task_resource_authorization(
    configuration: UserConfiguration,
    *,
    authorized_at: datetime | None = None,
) -> TaskResourceAuthorization | None:
    """Freeze known model facts and explicit cost/deadline authority for one task."""

    when = (authorized_at or datetime.now(UTC)).astimezone(UTC)
    snapshots: list[TaskModelMetadata] = []
    print("\nModels available to this task")
    for profile in configuration.model_profiles:
        if (
            profile.input_cost_per_million_usd is None
            or profile.output_cost_per_million_usd is None
            or profile.pricing_source is None
            or profile.cache_pricing is None
            or profile.context_window_tokens is None
            or profile.context_source is None
        ):
            raise RuntimeConfigurationError(
                f"model metadata is incomplete for profile {profile.id}; "
                "run task self-check remediation before Planning"
            )
        print(
            f"  - {profile.id}: {profile.model}; "
            f"${profile.input_cost_per_million_usd} input / "
            f"${profile.output_cost_per_million_usd} output per million tokens "
            f"({profile.pricing_source.value}); "
            f"${profile.cache_pricing.read_cost_per_million_usd} cache read / "
            f"${profile.cache_pricing.write_cost_per_million_usd} cache write "
            f"per million tokens ({profile.cache_pricing.source.value}); "
            f"context {profile.context_window_tokens} "
            f"({profile.context_source.value})"
        )
        snapshots.append(
            TaskModelMetadata(
                profile_id=profile.id,
                model=profile.model,
                input_cost_per_million_usd=(profile.input_cost_per_million_usd),
                output_cost_per_million_usd=(profile.output_cost_per_million_usd),
                pricing_source=profile.pricing_source,
                context_window_tokens=profile.context_window_tokens,
                context_source=profile.context_source,
                observed_at=when,
                cache_pricing=profile.cache_pricing,
            )
        )
    maximum_cost = _prompt_task_cost_ceiling()
    deadline = _prompt_optional_run_deadline()
    print("\nTask resource authorization")
    print(f"  Total model-spend ceiling: ${maximum_cost}")
    print(
        "  Whole-run deadline: "
        + (f"{deadline} seconds" if deadline is not None else "none")
    )
    print("  Call count, token count, Agent count, and iteration count are telemetry,")
    print("  not separate limits for this ordinary product task.")
    print("  SAT stops new calls after recorded estimated spend reaches this ceiling;")
    print("  an absolute billing cap requires a provider-side spending or quota limit.")
    if not _prompt_yes_no(
        "Authorize these model routes and task limits?",
        default=False,
    ):
        print("Build cancelled; no model request was made.")
        return None
    return TaskResourceAuthorization(
        maximum_estimated_cost_usd=maximum_cost,
        run_deadline_seconds=deadline,
        model_metadata=tuple(snapshots),
        authorized_at=when,
    )


def _prompt_context_window(model: str) -> int:
    """Ask only when the local runtime cannot discover model context length."""

    while True:
        raw = _read_short(
            f"Context-window tokens for {model} (provider-documented value): "
        ).strip()
        if raw.isdecimal() and int(raw) >= 1:
            return int(raw)
        print("Enter a positive integer.")


def _prompt_yes_no(label: str, *, default: bool) -> bool:
    """Read one explicit bounded confirmation from an interactive terminal."""

    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        answer = _read_short(f"{label} {suffix} ").strip().casefold()
        if not answer:
            return default
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False
        print("Please answer y or n.")

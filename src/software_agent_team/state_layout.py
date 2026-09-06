"""Authoritative lifecycle manifest for SAT-owned user state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class StateLifecycleGroup(StrEnum):
    """The user-visible lifecycle authority for one state category."""

    DATA = "data"
    PROVIDER = "provider"
    EPHEMERAL = "ephemeral"


@dataclass(frozen=True)
class ProductStateCategory:
    """One owned state directory and its export/removal semantics."""

    attribute: str
    directory_name: str
    lifecycle_group: StateLifecycleGroup
    export_name: str | None

    @property
    def exported(self) -> bool:
        return self.export_name is not None


PRODUCT_STATE_CATEGORIES = (
    ProductStateCategory(
        attribute="runs",
        directory_name="runs",
        lifecycle_group=StateLifecycleGroup.DATA,
        export_name="runs",
    ),
    ProductStateCategory(
        attribute="workspaces",
        directory_name="workspaces",
        lifecycle_group=StateLifecycleGroup.DATA,
        export_name="workspaces",
    ),
    ProductStateCategory(
        attribute="sources",
        directory_name="sources",
        lifecycle_group=StateLifecycleGroup.DATA,
        export_name="sources",
    ),
    ProductStateCategory(
        attribute="planning",
        directory_name="planning",
        lifecycle_group=StateLifecycleGroup.DATA,
        export_name="planning",
    ),
    ProductStateCategory(
        attribute="self_checks",
        directory_name="self-checks",
        lifecycle_group=StateLifecycleGroup.DATA,
        export_name="self-checks",
    ),
    ProductStateCategory(
        attribute="process_leases",
        directory_name="process-leases",
        lifecycle_group=StateLifecycleGroup.EPHEMERAL,
        export_name=None,
    ),
    ProductStateCategory(
        attribute="openclaw",
        directory_name="openclaw",
        lifecycle_group=StateLifecycleGroup.PROVIDER,
        export_name=None,
    ),
)


def state_category_paths(root: Path) -> dict[str, Path]:
    """Resolve every manifest category beneath one state root."""

    return {
        category.attribute: root / category.directory_name
        for category in PRODUCT_STATE_CATEGORIES
    }

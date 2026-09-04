"""Shared attributable source labels for model price and capability metadata."""

from enum import StrEnum


class ModelMetadataSource(StrEnum):
    """Attributable source for saved or run-scoped model metadata."""

    RUNTIME_CATALOG = "runtime_catalog"
    PROVIDER_CATALOG = "provider_catalog"
    USER_SUPPLIED = "user_supplied"
    CONFIRMED_ZERO = "confirmed_zero"

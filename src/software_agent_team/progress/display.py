"""Terminal presentation capability and color detection."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TextIO

from .types import TerminalColorMode


def terminal_presentation_capable(
    output: TextIO,
    *,
    environment: Mapping[str, str] | None = None,
    is_terminal: bool | None = None,
) -> bool:
    """Return whether cursor movement and ANSI styling are safe on this output."""

    resolved_environment = os.environ if environment is None else environment
    if is_terminal is None:
        try:
            detected_terminal = bool(output.isatty())
        except (AttributeError, OSError):
            detected_terminal = False
    else:
        detected_terminal = is_terminal
    return detected_terminal and resolved_environment.get("TERM") != "dumb"


def terminal_color_enabled(
    output: TextIO,
    mode: TerminalColorMode | str,
    *,
    environment: Mapping[str, str] | None = None,
    is_terminal: bool | None = None,
) -> bool:
    """Resolve one color mode without allowing ANSI on an unsafe output."""

    resolved_mode = TerminalColorMode(mode)
    resolved_environment = os.environ if environment is None else environment
    capable = terminal_presentation_capable(
        output,
        environment=resolved_environment,
        is_terminal=is_terminal,
    )
    return (
        resolved_mode is not TerminalColorMode.NEVER
        and capable
        and (
            resolved_mode is TerminalColorMode.ALWAYS
            or "NO_COLOR" not in resolved_environment
        )
    )

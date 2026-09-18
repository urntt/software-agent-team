"""TTY-aware input editing for user-authored SAT text."""

from __future__ import annotations

import builtins
import sys
from collections.abc import Callable
from typing import TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.output import Output
from prompt_toolkit.styles import Style
from prompt_toolkit.validation import ValidationError, Validator

TextValidator = Callable[[str], str | None]

_STYLE = Style.from_dict(
    {
        "prompt": "bold #00afff",
        "bottom-toolbar": "bg:#303030 #eeeeee",
        "validation-toolbar": "bg:#800000 #ffffff",
    }
)


class _TextValidator(Validator):
    """Keep invalid text in the editor while showing one actionable message."""

    def __init__(self, validate_text: TextValidator) -> None:
        self._validate_text = validate_text

    def validate(self, document: Document) -> None:
        message = self._validate_text(document.text)
        if message is not None:
            raise ValidationError(
                cursor_position=document.cursor_position,
                message=message,
            )


def _natural_text_bindings() -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("enter")
    def _submit(event) -> None:  # type: ignore[no-untyped-def]
        event.current_buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    @bindings.add("c-o")
    def _insert_newline(event) -> None:  # type: ignore[no-untyped-def]
        event.current_buffer.insert_text("\n")

    return bindings


class TerminalInput:
    """Own short, natural-language, and secret terminal input contracts."""

    def __init__(
        self,
        *,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
        prompt_input: Input | None = None,
        prompt_output: Output | None = None,
        interactive: bool | None = None,
    ) -> None:
        self.input_stream = input_stream
        self.output_stream = output_stream
        self.prompt_input = prompt_input
        self.prompt_output = prompt_output
        self.interactive = interactive
        self._short_history = InMemoryHistory()
        self._natural_history = InMemoryHistory()

    def read_line(
        self,
        prompt: str,
        *,
        default: str = "",
        validate: TextValidator | None = None,
    ) -> str:
        """Read one editable line, with a deterministic non-TTY fallback."""

        return self._read(
            prompt,
            default=default,
            validate=validate,
            multiline=False,
            secret=False,
        )

    def read_text(
        self,
        prompt: str,
        *,
        default: str = "",
        validate: TextValidator | None = None,
    ) -> str:
        """Read an editable multiline document; Enter submits the whole buffer."""

        return self._read(
            prompt,
            default=default,
            validate=validate,
            multiline=True,
            secret=False,
        )

    def read_secret(
        self,
        prompt: str,
        *,
        validate: TextValidator | None = None,
    ) -> str:
        """Read a non-echoed value without placing it in either input history."""

        return self._read(
            prompt,
            default="",
            validate=validate,
            multiline=False,
            secret=True,
        )

    def _read(
        self,
        prompt: str,
        *,
        default: str,
        validate: TextValidator | None,
        multiline: bool,
        secret: bool,
    ) -> str:
        if not self._editor_available():
            return self._read_plain(prompt, default=default, validate=validate)

        session: PromptSession[str] = PromptSession(
            input=self.prompt_input,
            output=self.prompt_output,
            erase_when_done=False,
            history=(
                None
                if secret
                else self._natural_history
                if multiline
                else self._short_history
            ),
        )
        return session.prompt(
            [("class:prompt", prompt)],
            default=default,
            multiline=multiline,
            wrap_lines=True,
            is_password=secret,
            key_bindings=_natural_text_bindings() if multiline else None,
            prompt_continuation=(
                (lambda width, _line, _wrap: " " * max(0, width - 2) + "· ")
                if multiline
                else None
            ),
            bottom_toolbar=(
                " Enter submit  •  Alt+Enter or Ctrl+O newline  •  Ctrl+C cancel "
                if multiline
                else None
            ),
            validator=None if validate is None else _TextValidator(validate),
            validate_while_typing=False,
            style=_STYLE,
        )

    def _editor_available(self) -> bool:
        if self.interactive is not None:
            return self.interactive
        if self.prompt_input is not None or self.prompt_output is not None:
            return self.prompt_input is not None and self.prompt_output is not None
        input_stream = sys.stdin if self.input_stream is None else self.input_stream
        output_stream = sys.stdout if self.output_stream is None else self.output_stream
        return input_stream.isatty() and output_stream.isatty()

    def _read_plain(
        self,
        prompt: str,
        *,
        default: str,
        validate: TextValidator | None,
    ) -> str:
        while True:
            input_stream = sys.stdin if self.input_stream is None else self.input_stream
            output_stream = (
                sys.stdout if self.output_stream is None else self.output_stream
            )
            if input_stream is sys.stdin and output_stream is sys.stdout:
                value = builtins.input(prompt)
            else:
                output_stream.write(prompt)
                output_stream.flush()
                line = input_stream.readline()
                if line == "":
                    raise EOFError
                value = line.removesuffix("\n").removesuffix("\r")
            value = value if value else default
            message = None if validate is None else validate(value)
            if message is None:
                return value
            print(message, file=output_stream, flush=True)


DEFAULT_TERMINAL_INPUT = TerminalInput()

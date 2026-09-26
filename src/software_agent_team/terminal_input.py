"""TTY-aware input editing for user-authored SAT text."""

from __future__ import annotations

import asyncio
import builtins
import sys
import threading
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
    """Own short and natural-language terminal input contracts."""

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
        self._active_read_lock = threading.Lock()
        self._active_read: (
            tuple[asyncio.AbstractEventLoop, asyncio.Task[str]] | None
        ) = None
        self._read_cancelled = False

    def cancel_active_read(self) -> None:
        """Cancel an in-flight TTY editor and restore its terminal before exit."""

        with self._active_read_lock:
            self._read_cancelled = True
            active = self._active_read
        if active is not None:
            loop, task = active
            loop.call_soon_threadsafe(task.cancel)

    def read_line_cancellable(self, prompt: str, *, default: str = "") -> str:
        """Read a control line that the run owner can cancel during shutdown."""

        return self._read(
            prompt,
            default=default,
            validate=None,
            multiline=False,
            cancellable=True,
        )

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
        )

    def _read(
        self,
        prompt: str,
        *,
        default: str,
        validate: TextValidator | None,
        multiline: bool,
        cancellable: bool = False,
    ) -> str:
        if cancellable:
            with self._active_read_lock:
                if self._read_cancelled:
                    raise EOFError
        if not self._editor_available():
            return self._read_plain(prompt, default=default, validate=validate)

        session: PromptSession[str] = PromptSession(
            input=self.prompt_input,
            output=self.prompt_output,
            erase_when_done=False,
            history=self._natural_history if multiline else self._short_history,
        )
        prompt_options = {
            "message": [("class:prompt", prompt)],
            "default": default,
            "multiline": multiline,
            "wrap_lines": True,
            "key_bindings": _natural_text_bindings() if multiline else None,
            "prompt_continuation": (
                (lambda width, _line, _wrap: " " * max(0, width - 2) + "· ")
                if multiline
                else None
            ),
            "bottom_toolbar": (
                " Enter submit  •  Alt+Enter or Ctrl+O newline  •  Ctrl+C cancel "
                if multiline
                else None
            ),
            "validator": None if validate is None else _TextValidator(validate),
            "validate_while_typing": False,
            "style": _STYLE,
        }
        if not cancellable:
            return session.prompt(**prompt_options)

        async def run_cancellable() -> str:
            loop = asyncio.get_running_loop()
            task = asyncio.current_task()
            assert task is not None
            with self._active_read_lock:
                if self._read_cancelled:
                    raise EOFError
                self._active_read = (loop, task)
            try:
                return await session.prompt_async(**prompt_options)
            finally:
                with self._active_read_lock:
                    self._active_read = None

        try:
            return asyncio.run(run_cancellable())
        except asyncio.CancelledError as error:
            raise EOFError from error

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

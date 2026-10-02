"""One terminal owner for live status, command editing, and user answers."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TextIO

from prompt_toolkit.application import Application, run_in_terminal
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer
from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import Input, create_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.output import Output, create_output
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.shortcuts import print_formatted_text
from prompt_toolkit.styles import Style


@dataclass
class _AnswerRequest:
    prompt: str
    multiline: bool
    finished: threading.Event = field(default_factory=threading.Event)
    value: str | None = None


class TerminalDashboard:
    """Keep an editable control line below progress without competing redraws."""

    def __init__(
        self,
        *,
        submit: Callable[[str], str],
        completer: Completer,
        live_lines: Callable[[], list[str]],
        detail_lines: Callable[[], list[str]] | None = None,
        input_stream: TextIO | None = None,
        output_stream: TextIO | None = None,
        prompt_input: Input | None = None,
        prompt_output: Output | None = None,
        color: bool = True,
    ) -> None:
        self.submit = submit
        self.live_lines = live_lines
        self.detail_lines = detail_lines or (lambda: [])
        self.input = prompt_input or create_input(stdin=input_stream)
        self.output = prompt_output or create_output(stdout=output_stream)
        self._owns_input = prompt_input is None
        self._answer: _AnswerRequest | None = None
        self._answers_cancelled = threading.Event()
        self._encoding = getattr(output_stream, "encoding", None) or "utf-8"
        self._command_draft = Document()
        self._closed = threading.Event()
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending_writes: set = set()
        self._writes_lock = threading.Lock()
        self._notice = "/help: commands  •  Tab: complete  •  Enter: submit"
        self.buffer = Buffer(
            completer=completer,
            history=InMemoryHistory(),
            multiline=True,
            complete_while_typing=True,
        )
        bindings = KeyBindings()

        @bindings.add("enter")
        def accept(event) -> None:
            self._accept()

        @bindings.add("c-o")
        @bindings.add("escape", "enter")
        def newline(event) -> None:
            if self._answer is not None and self._answer.multiline:
                event.current_buffer.insert_text("\n")

        @bindings.add("c-c")
        def clear(event) -> None:
            event.current_buffer.reset()
            self._notice = "Entry cleared. Use /cancel confirm to stop."

        @bindings.add("c-d")
        def eof(event) -> None:
            if not event.current_buffer.text:
                self._notice = (
                    "Use /cancel confirm to stop, or Ctrl+C to clear an entry."
                )
                self.invalidate()

        def panel() -> ANSI:
            detail_height = min(9, len(self.detail_lines()))
            completions = self.buffer.complete_state
            menu_height = (
                0 if completions is None else min(3, len(completions.completions))
            )
            rows = max(1, self.output.get_size().rows - 7 - detail_height - menu_height)
            lines = list(self.live_lines())
            # The layout owns the scrollback boundary, including when a callback
            # has no live observation or changes its leading padding.
            while lines and not lines[0].strip():
                lines.pop(0)
            if len(lines) > rows:
                lines = [
                    *lines[: max(0, rows - 1)],
                    f"… {len(lines) - rows + 1} more lines; enlarge the terminal",
                ]
            return ANSI(self._safe_text("\n".join(lines)))

        editor = BufferControl(buffer=self.buffer)
        layout = HSplit(
            [
                Window(height=1),
                Window(FormattedTextControl(panel), dont_extend_height=True),
                Window(
                    FormattedTextControl(
                        lambda: ANSI(
                            self._safe_text("\n".join(self.detail_lines()[:9]))
                        )
                    ),
                    dont_extend_height=True,
                ),
                Window(
                    FormattedTextControl(
                        lambda: [("class:prompt", self._safe_text(self._prompt()))]
                    ),
                    height=1,
                ),
                Window(editor, height=lambda: min(4, self.buffer.document.line_count)),
                CompletionsMenu(max_height=3, scroll_offset=1),
                Window(
                    FormattedTextControl(lambda: self._safe_text(self._notice)),
                    height=1,
                    style="class:hint",
                ),
            ]
        )
        self.app: Application[None] = Application(
            layout=Layout(layout, focused_element=editor),
            input=self.input,
            output=self.output,
            key_bindings=bindings,
            full_screen=False,
            erase_when_done=True,
            refresh_interval=1.0,
            color_depth=None if color else ColorDepth.DEPTH_1_BIT,
            style=Style.from_dict(
                {"prompt": "bold ansicyan", "hint": "ansibrightblack"} if color else {}
            ),
        )
        self._thread = threading.Thread(
            target=self._run, name="sat-terminal", daemon=True
        )

    def _safe_text(self, value: str) -> str:
        return value.encode(self._encoding, errors="replace").decode(self._encoding)

    def _prompt(self) -> str:
        return (
            "control>"
            if self._answer is None
            else self._answer.prompt + "  (/help for commands)"
        )

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(3) or self._closed.is_set():
            self.close()
            raise RuntimeError("the terminal dashboard could not start")

    def _run(self) -> None:
        def ready() -> None:
            self._loop = asyncio.get_running_loop()
            self._ready.set()

        try:
            self.app.run(pre_run=ready, set_exception_handler=False)
        finally:
            self._closed.set()
            self._ready.set()
            if self._answer is not None:
                self._answer.finished.set()

    def invalidate(self) -> None:
        self.app.invalidate()

    def write(self, text: str) -> None:
        """Append a semantic log above the layout, preserving the entire draft."""

        if self._loop is None or self._closed.is_set():
            return

        async def write() -> None:
            await run_in_terminal(
                lambda: print_formatted_text(
                    ANSI(self._safe_text(text)), output=self.output
                )
            )

        future = asyncio.run_coroutine_threadsafe(write(), self._loop)
        with self._writes_lock:
            self._pending_writes.add(future)

        def done(completed) -> None:
            with self._writes_lock:
                self._pending_writes.discard(completed)

        future.add_done_callback(done)

    def read(self, prompt: str, *, multiline: bool = False) -> str:
        """Read one answer while slash commands retain their control authority."""

        if (
            self._closed.is_set()
            or self._answers_cancelled.is_set()
            or self._loop is None
        ):
            raise EOFError
        answer = _AnswerRequest(prompt, multiline)

        def activate() -> None:
            if self._answers_cancelled.is_set():
                answer.finished.set()
                return
            self._command_draft = self.buffer.document
            self.buffer.reset()
            self._answer = answer
            self._notice = (
                "Enter: answer  •  Alt+Enter / Ctrl+O: newline  •  /help: commands"
                if multiline
                else "Enter: answer  •  /help: commands"
            )
            self.invalidate()

        self._loop.call_soon_threadsafe(activate)
        answer.finished.wait()
        if answer.value is None:
            raise EOFError
        return answer.value

    def _accept(self) -> None:
        value = self.buffer.text
        literal_answer = self._answer is not None and value.startswith("//")
        if (
            value.lstrip().startswith("/") and not literal_answer
        ) or self._answer is None:
            if not value.strip():
                return
            try:
                message = self.submit(value)
            except ValueError as error:
                self._notice = str(error)
                self.invalidate()
                return
            self.buffer.append_to_history()
            self.buffer.reset()
            self._notice = (
                message.splitlines()[0]
                if message
                else "/help: commands  •  Tab: complete"
            )
            if message:
                self.write(message)
        else:
            answer = self._answer
            self._answer = None
            self.buffer.reset()
            self.buffer.set_document(self._command_draft)
            answer.value = value[1:] if literal_answer else value
            answer.finished.set()
        self.invalidate()

    def cancel_answer(self) -> None:
        """Wake an answer reader after phase cancellation without submitting text."""

        self._answers_cancelled.set()
        if self._answer is not None:
            self._answer.finished.set()

    def close(self) -> None:
        if self._loop is not None and not self._closed.is_set():
            # Drain scheduled history before restoring the terminal.
            with self._writes_lock:
                pending = tuple(self._pending_writes)
            for future in pending:
                future.result(timeout=3)
            self._loop.call_soon_threadsafe(self.app.exit)
        self._thread.join(timeout=3)
        if self._thread.is_alive():
            raise RuntimeError("the terminal dashboard did not release its input")
        if self._owns_input:
            self.input.close()

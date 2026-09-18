"""Tests for the shared TTY and deterministic line-input adapter."""

from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from io import StringIO

import pytest
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.output import DummyOutput

from software_agent_team.terminal_input import TerminalInput


def _feed_later(pipe_input, value: str) -> threading.Thread:  # type: ignore[no-untyped-def]
    thread = threading.Timer(0.05, lambda: pipe_input.send_text(value))
    thread.start()
    return thread


def test_natural_text_editor_edits_across_real_lines() -> None:
    with create_pipe_input() as pipe_input:
        editor = TerminalInput(
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
            interactive=True,
        )
        # Ctrl+O inserts a real newline. The remaining sequences exercise
        # Delete, Up, Home, and editing the preceding line before submission.
        feeder = _feed_later(
            pipe_input,
            "first\x0fsecondX\x1b[D\x1b[3~\x1b[A\x1b[H你好 \r",
        )
        try:
            value = editor.read_text("> ")
        finally:
            feeder.join()

    assert value == "你好 first\nsecond"


def test_validation_keeps_the_editable_draft() -> None:
    with create_pipe_input() as pipe_input:
        editor = TerminalInput(
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
            interactive=True,
        )
        feeder = _feed_later(pipe_input, "abcdef\r\x7f\r")
        try:
            value = editor.read_text(
                "> ",
                validate=lambda text: "Too long" if len(text) > 5 else None,
            )
        finally:
            feeder.join()

    assert value == "abcde"


def test_secret_input_is_not_added_to_editable_histories() -> None:
    with create_pipe_input() as pipe_input:
        editor = TerminalInput(
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
            interactive=True,
        )
        feeder = _feed_later(pipe_input, "private-value\r")
        try:
            value = editor.read_secret("Secret: ")
        finally:
            feeder.join()

    assert value == "private-value"
    assert editor._short_history.get_strings() == []
    assert editor._natural_history.get_strings() == []


@pytest.mark.parametrize(
    ("control", "exception"),
    (("\x03", KeyboardInterrupt), ("\x04", EOFError)),
)
def test_editor_preserves_cancel_and_eof(
    control: str, exception: type[BaseException]
) -> None:
    with create_pipe_input() as pipe_input:
        editor = TerminalInput(
            prompt_input=pipe_input,
            prompt_output=DummyOutput(),
            interactive=True,
        )
        feeder = _feed_later(pipe_input, control)
        try:
            with pytest.raises(exception):
                editor.read_text("> ")
        finally:
            feeder.join()


def test_non_tty_uses_one_plain_line_without_control_sequences() -> None:
    output = StringIO()
    editor = TerminalInput(
        input_stream=StringIO("plain request\n"),
        output_stream=output,
        interactive=False,
    )

    assert editor.read_text("> ") == "plain request"
    assert output.getvalue() == "> "
    assert "\x1b" not in output.getvalue()


def _set_terminal_size(descriptor: int, *, columns: int) -> None:
    fcntl.ioctl(
        descriptor,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", 24, columns, 0, 0),
    )


def _read_pty_until(
    descriptor: int,
    process: subprocess.Popen[bytes],
    *,
    expected: bytes | None = None,
    timeout: float = 8,
) -> bytes:
    captured = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if expected is not None and expected in captured:
            return bytes(captured)
        if process.poll() is not None:
            break
        ready, _, _ = select.select([descriptor], [], [], 0.05)
        if ready:
            try:
                captured.extend(os.read(descriptor, 65_536))
            except OSError:
                break
    while True:
        ready, _, _ = select.select([descriptor], [], [], 0)
        if not ready:
            break
        try:
            captured.extend(os.read(descriptor, 65_536))
        except OSError:
            break
    return bytes(captured)


def test_real_pty_handles_soft_wrap_cursor_unicode_and_resize() -> None:
    master, slave = pty.openpty()
    _set_terminal_size(slave, columns=10)
    program = """
from software_agent_team.terminal_input import TerminalInput

value = TerminalInput().read_text("> ")
print("RESULT=" + value.encode("unicode_escape").decode())
"""
    process = subprocess.Popen(
        [sys.executable, "-c", program],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    os.close(slave)
    try:
        before = _read_pty_until(master, process, expected=b"> ")
        assert b"> " in before
        # The text wraps in the ten-column terminal. Move into the preceding
        # display row, insert one character, add Unicode, then resize and submit.
        os.write(master, b"abcdefghijk" + (b"\x1b[D" * 8) + b"X")
        os.write(master, "世界".encode() + b"Q\x1b[D\x1b[3~")
        _set_terminal_size(master, columns=24)
        os.kill(process.pid, signal.SIGWINCH)
        os.write(master, b"\r")
        after = _read_pty_until(master, process)
        assert process.wait(timeout=2) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        os.close(master)

    assert b"RESULT=abcX\\u4e16\\u754cdefghijk" in before + after


@pytest.mark.parametrize(
    ("control", "marker"),
    ((b"\x03", b"RESULT=cancelled"), (b"\x04", b"RESULT=eof")),
)
def test_real_pty_preserves_cancel_and_eof(control: bytes, marker: bytes) -> None:
    master, slave = pty.openpty()
    program = """
from software_agent_team.terminal_input import TerminalInput

try:
    TerminalInput().read_text("> ")
except KeyboardInterrupt:
    print("RESULT=cancelled")
except EOFError:
    print("RESULT=eof")
"""
    process = subprocess.Popen(
        [sys.executable, "-c", program],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        env={**os.environ, "TERM": "xterm-256color"},
    )
    os.close(slave)
    try:
        before = _read_pty_until(master, process, expected=b"> ")
        assert b"> " in before
        os.write(master, control)
        after = _read_pty_until(master, process)
        assert process.wait(timeout=2) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        os.close(master)

    assert marker in before + after

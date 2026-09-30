"""Exercise the actual editor, completion, and answer/control ownership."""

from __future__ import annotations

import threading
import time

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from software_agent_team.control_console import ControlCompleter
from software_agent_team.terminal_dashboard import TerminalDashboard


def wait_until(predicate) -> None:
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.01)


def test_completion_and_answers_share_editor_without_early_submission() -> None:
    commands: list[str] = []
    answers: list[str] = []
    with create_pipe_input() as pipe:
        dashboard = TerminalDashboard(
            submit=lambda line: commands.append(line) or "accepted",
            completer=ControlCompleter(planning=True),
            live_lines=lambda: ["Planning active"],
            prompt_input=pipe,
            prompt_output=DummyOutput(),
        )
        dashboard.start()
        try:
            pipe.send_text("/vis")
            wait_until(lambda: dashboard.buffer.text == "/vis")
            wait_until(lambda: dashboard.buffer.complete_state is not None)
            pipe.send_text("\t")
            wait_until(lambda: dashboard.buffer.text == "/visibility")
            assert not commands
            pipe.send_text(" standard\r")
            wait_until(lambda: commands == ["/visibility standard"])
            reader = threading.Thread(
                target=lambda: answers.append(
                    dashboard.read("Your answer", multiline=True)
                )
            )
            reader.start()
            wait_until(lambda: dashboard._answer is not None)
            pipe.send_text("/help\r")
            wait_until(lambda: commands[-1] == "/help")
            assert reader.is_alive() and not answers
            pipe.send_text("first\x0fsecond")
            wait_until(lambda: dashboard.buffer.text == "first\nsecond")
            pipe.send_text("\x1b[A\x1b[D!\r")
            reader.join(timeout=3)
            assert not reader.is_alive()
            assert answers == ["firs!t\nsecond"]
            literal = threading.Thread(
                target=lambda: answers.append(dashboard.read("Path"))
            )
            literal.start()
            wait_until(lambda: dashboard._answer is not None)
            pipe.send_text("//usr/local/bin\r")
            literal.join(timeout=3)
            assert answers[-1] == "/usr/local/bin"
        finally:
            dashboard.close()
        assert not dashboard._thread.is_alive()


def test_partial_control_draft_survives_question_and_shutdown_never_submits() -> None:
    submitted: list[str] = []
    answers: list[str] = []
    with create_pipe_input() as pipe:
        dashboard = TerminalDashboard(
            submit=lambda value: submitted.append(value) or "ok",
            completer=ControlCompleter(planning=True),
            live_lines=lambda: [],
            prompt_input=pipe,
            prompt_output=DummyOutput(),
        )
        dashboard.start()
        pipe.send_text("/visibility comp")
        wait_until(lambda: dashboard.buffer.text == "/visibility comp")
        reader = threading.Thread(
            target=lambda: answers.append(dashboard.read("Choose"))
        )
        reader.start()
        wait_until(lambda: dashboard._answer is not None)
        pipe.send_text("2\r")
        reader.join(timeout=3)
        assert answers == ["2"]
        assert dashboard.buffer.text == "/visibility comp"
        dashboard.close()
        assert not submitted


def test_cancel_wakes_pending_answer_and_refuses_late_answer_activation() -> None:
    errors: list[Exception] = []
    with create_pipe_input() as pipe:
        dashboard = TerminalDashboard(
            submit=lambda _: dashboard.cancel_answer() or "Cancelled",
            completer=ControlCompleter(planning=True),
            live_lines=lambda: [],
            prompt_input=pipe,
            prompt_output=DummyOutput(),
        )
        dashboard.start()

        def answer() -> None:
            try:
                dashboard.read("Your answer")
            except EOFError as error:
                errors.append(error)

        reader = threading.Thread(target=answer)
        try:
            reader.start()
            wait_until(lambda: dashboard._answer is not None)
            pipe.send_text("/cancel confirm\r")
            reader.join(timeout=3)
            assert not reader.is_alive() and len(errors) == 1
            with pytest.raises(EOFError):
                dashboard.read("Late answer")
        finally:
            dashboard.close()

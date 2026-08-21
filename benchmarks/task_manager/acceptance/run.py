"""Black-box acceptance suite for the generated task-manager application."""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8765
BASE_URL = f"http://{HOST}:{PORT}"
TASK_LOCATION = re.compile(r"^/tasks/(?P<task_id>[1-9][0-9]*)$")
INVALID_FORM_STATUSES = frozenset({200, 400, 422})


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Expose redirects as evidence instead of following them implicitly."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def request(
    path: str,
    *,
    data: dict[str, str] | None = None,
    expected: frozenset[int] | set[int] = frozenset({200}),
) -> tuple[int, str, str | None]:
    """Send one local HTTP request without following redirects."""

    encoded = None if data is None else urllib.parse.urlencode(data).encode()
    method = "GET" if encoded is None else "POST"
    opener = urllib.request.build_opener(NoRedirectHandler())
    call = urllib.request.Request(BASE_URL + path, data=encoded, method=method)
    try:
        response = opener.open(call, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    status = response.status
    body = response.read().decode("utf-8", errors="strict")
    location = response.headers.get("Location")
    if status not in expected:
        raise AssertionError(
            f"{method} {path} returned {status}, expected {sorted(expected)}; "
            f"body={body[:500]!r}"
        )
    return status, body, location


def wait_until_ready(process: subprocess.Popen[bytes]) -> None:
    """Wait for the server to accept local HTTP requests."""

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(
                "application exited before becoming ready; "
                f"stdout={stdout.decode(errors='replace')!r}; "
                f"stderr={stderr.decode(errors='replace')!r}"
            )
        try:
            request("/", expected={200})
            return
        except (OSError, AssertionError):
            time.sleep(0.1)
    raise AssertionError("application did not become ready within 15 seconds")


@contextmanager
def server(repository: Path, database: Path) -> Iterator[None]:
    """Run the fixed ASGI entry point and terminate its process group safely."""

    environment = {
        **os.environ,
        "TASK_MANAGER_DATABASE_PATH": str(database),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            HOST,
            "--port",
            str(PORT),
        ],
        cwd=repository,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        wait_until_ready(process)
        yield
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)


def require_text(body: str, *values: str) -> None:
    """Require case-insensitive visible evidence in one HTML response."""

    lowered = body.lower()
    for value in values:
        if value.lower() not in lowered:
            raise AssertionError(f"response does not contain {value!r}")


def require_any_text(body: str, *values: str) -> None:
    """Require at least one equivalent case-insensitive value in HTML."""

    lowered = body.lower()
    if not any(value.lower() in lowered for value in values):
        raise AssertionError(f"response does not contain any of {values!r}")


def create_task(title: str, *, description: str = "persisted") -> str:
    """Create a complete task and return its canonical detail location."""

    _, body, location = request(
        "/tasks",
        data={
            "title": title,
            "description": description,
            "due_date": "2026-09-01",
            "status": "todo",
            "priority": "medium",
        },
        expected={200, 201, 302, 303},
    )
    if location is None:
        match = re.search(r'href=["\'](/tasks/[1-9][0-9]*)["\']', body)
        location = None if match is None else match.group(1)
    if location is None or TASK_LOCATION.fullmatch(location) is None:
        raise AssertionError("task creation did not expose a canonical detail URL")
    return location


def run_acceptance(repository: Path) -> None:
    """Exercise the complete fixed benchmark contract."""

    with tempfile.TemporaryDirectory(prefix="sat-task-manager-") as temporary:
        database = Path(temporary) / "tasks.sqlite3"
        with server(repository, database):
            _, list_html, _ = request("/")
            require_text(list_html, "task", "<form", "<label")

            location = create_task("Phase One Task", description="Keep this value")
            _, detail_html, _ = request(location)
            require_text(detail_html, "Phase One Task", "Keep this value")
            require_any_text(detail_html, "todo", "to do")

            _, _, _ = request(
                f"{location}/edit",
                data={
                    "title": "Updated Phase One Task",
                    "description": "Updated description",
                    "due_date": "2026-09-02",
                    "status": "done",
                    "priority": "high",
                },
                expected={200, 302, 303},
            )
            _, updated_html, _ = request(location)
            require_text(
                updated_html,
                "Updated Phase One Task",
                "Updated description",
                "done",
                "high",
            )

            _, filtered_html, _ = request("/?status=done&priority=high")
            require_text(filtered_html, "Updated Phase One Task")

            _, invalid_html, _ = request(
                "/tasks",
                data={
                    "title": "",
                    "description": "Preserve this description",
                    "due_date": "",
                    "status": "todo",
                    "priority": "medium",
                },
                expected=INVALID_FORM_STATUSES,
            )
            require_text(invalid_html, "title", "Preserve this description")
            request("/tasks/999999999", expected={404})

            _, confirmation_html, _ = request(f"{location}/delete")
            require_text(confirmation_html, "delete", "Updated Phase One Task")
            request(
                f"{location}/delete",
                data={"confirm": "yes"},
                expected={200, 302, 303},
            )
            request(location, expected={404})

            persisted_location = create_task("Persistent Task")

        with server(repository, database):
            _, persisted_html, _ = request(persisted_location)
            require_text(persisted_html, "Persistent Task")


def main() -> int:
    """Parse the repository path and execute the acceptance suite."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    args = parser.parse_args()
    repository = args.repository.resolve(strict=True)
    if not repository.is_dir():
        raise SystemExit("repository must be a directory")
    run_acceptance(repository)
    print("task-manager acceptance suite passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

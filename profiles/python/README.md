# Python Product Profile

This contributor reference documents the first generated-project execution
profile used by bare `sat`. The profile constrains runtime and verification,
not the user's task domain. The task-manager materials under `benchmarks/` are
a separate controlled evaluation fixture and are never used to materialize a
product request.

## Supported Boundary

The profile targets a small greenfield Python 3.12 project that can run locally
without credentials, hosted services, an external database, or runtime network
access. The pinned image includes FastAPI, Jinja2, SQLite support from the
standard library, pytest, Ruff, HTTPX, and Uvicorn. A project may be a Web
application, CLI tool, or local automation as long as it stays within this
runtime boundary.

The profile does not yet support arbitrary languages, mobile toolchains,
external service integration, or existing-repository modification.

## Owned Contracts

- `seed/` is the task-independent greenfield source baseline;
- `contract-template.json` fixes criterion IDs used by the quality manifest;
- `quality.json` owns trusted commands and criterion coverage;
- `validation/run.py` validates project metadata, documentation, and test
  presence outside the generated workspace;
- `validation/run_commands.py` copies the clean immutable project into fresh
  scratch and executes the exact setup, test, and start command contract.

Every generated project must replace the starter entry point and provide:

```json
{
  "schema_version": 1,
  "setup": ["uv", "sync", "--dev"],
  "start": ["uv", "run", "project-specific-entrypoint"],
  "test": ["uv", "run", "pytest"]
}
```

These values are argv arrays rather than shell strings. SAT validates the file
before delivery and renders its commands to the user. Setup and test remain
fixed for reproducibility; `start` belongs to the generated project.

The exact `start` argv must be directly usable from the project root without
extra arguments or configuration edits. A CLI therefore needs a safe default
input or interactive flow; a local service needs a complete startup command.
The deterministic clean-copy gate executes that exact argv because static
manifest validation cannot infer whether a project entry point has required
positional arguments.
README headings may use ordinary terms such as Installation, Usage, and
Testing, but the document must show the exact shell form of every manifest
command.

The validated setup contract also protects the first-use repository state.
The root `.venv` must be ignored. A root `uv.lock` must either be a bounded
regular file already present in the accepted clean snapshot or be explicitly
ignored, so running the documented setup command does not silently introduce
unexplained local state. The task-independent seed supplies the ignore policy;
a generated project may instead commit a lock when its implementation and
tests establish that as the reproducible choice. The validator asks Git to
confirm that the explicit rules remain effective after applying later patterns
or negations.

## Evidence Boundary

Deterministic gates validate the command/documentation contract, compile the
Python source, run Ruff, and run the generated pytest suite. A separate exact-
command gate first verifies that tracked files equal `HEAD`, copies only
committed regular files into fresh disposable scratch, and runs `uv sync
--dev`, the exact test argv, and the exact start argv with network disabled.
Untracked local files, an existing `.venv`, and the source repository's `.git`
directory cannot satisfy that check. The runtime image contains a locked
offline wheelhouse for setup and build dependencies.

The ordinary pytest gate
uses the console entry point, matching the `uv run pytest` command delivered to
the user; it must not substitute `python -m pytest`, which changes import-path
behavior and can hide a project that fails from a fresh user environment.
The gate runs in the clean quality workspace before user setup. Projects using
a `src` layout must therefore declare the pytest import path (the seed includes
`pythonpath = [".", "src"]`) instead of depending on an editable install that
happens to exist. User-specific behavior is also assigned to independent review because no
task-independent test suite can prove an arbitrary request. A passing profile
therefore means the bounded controller evidence and review accepted the result;
it is not a claim that one generic test suite can establish every possible
product requirement.

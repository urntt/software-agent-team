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
The root `.venv` must be ignored, while a bounded regular root `uv.lock` must be
committed as dependency-resolution metadata. The task-independent seed supplies
both policies. The lock must be portable with the delivered repository: its
TOML must not contain absolute or Windows drive paths, `file:` sources,
parent-directory references, missing project-relative artifacts, symlinked
local sources, or a registry/path that exists only in SAT's private offline
wheelhouse. Remote registries and archives must use remote URLs. The validator
parses the committed lock before any same-image setup command can mask a
host-local source. Writers use the immutable `sat-project-lock` helper after
sandbox setup or test commands; its frozen public metadata cache can refresh
the portable form without network access. The clean-copy command gate first
checks lock consistency against that neutral public cache, then verifies that
setup changes no other committed file and creates only artifacts covered by
committed `.gitignore` rules. The private wheelhouse may rewrite only the
disposable scratch copy of the lock while resolving the exact setup command.

## Evidence Boundary

Deterministic gates validate the command/documentation contract, compile the
Python source, run Ruff, and run the generated pytest suite. A separate exact-
command gate first verifies that tracked files equal `HEAD`, copies only
committed regular files into fresh disposable scratch, and runs `uv sync
--dev`, the exact test argv, and the exact start argv with network disabled.
Untracked local files, an existing `.venv`, and the source repository's `.git`
directory cannot satisfy that check. The runtime image contains a locked
offline wheelhouse for setup and build dependencies. That wheelhouse is
ephemeral controller infrastructure: it may satisfy the clean-copy command
gate, but its private path must never become generated-project metadata.
After setup, the gate compares every other committed file with its pre-setup
digest and executable bit and rejects every new file that is not covered by the
committed ignore policy. Setup and test run with closed standard input and must
exit successfully. The
bounded start probe keeps its standard-input pipe open through the startup
grace so an interactive CLI can wait for user input instead of receiving a
synthetic EOF; a process still running after that grace is terminated through
the same bounded process-group cleanup.

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

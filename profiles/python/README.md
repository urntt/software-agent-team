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
  presence outside the generated workspace.

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

## Evidence Boundary

Deterministic gates validate the command/documentation contract, compile the
Python source, run Ruff, and run the generated pytest suite. User-specific
behavior is also assigned to independent review because no task-independent
test suite can prove an arbitrary request. A passing profile therefore means
the bounded controller evidence and review accepted the result; it is not a
claim that one generic test suite can establish every possible product
requirement.

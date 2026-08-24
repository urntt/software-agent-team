# Development Guide

This guide owns local checkout setup, offline validation commands, benchmark
maintenance, repository layout, and the contribution workflow. Product,
architecture, experiment, scope, and roadmap changes must first be reconciled
with [`VISION.md`](../VISION.md).

## Local Setup

From the repository root:

```bash
make setup
make check
```

`make setup` prepares the pinned local toolchain and locked Python environment.
`make check` is fully offline after setup. It verifies tool versions, Git and
ignore boundaries, configuration contracts, formatting, lint, the complete
test suite, and all offline workflow paths. It does not call a model or require
provider credentials.

Useful targets are:

```bash
make doctor
make validate
make format
make format-check
make lint
make test
make check
make lock-benchmark
```

Use `make format` when source formatting changes are required. Always run
`make check` before committing.

## Validation CLI

The unified CLI exposes focused validators for checked-in contracts:

```bash
uv run sat validate-config
uv run sat list-teams
uv run sat validate-task-brief benchmarks/task_manager/task-brief.json
uv run sat validate-artifact examples/implementation-plan.json
uv run sat validate-handoff examples/handoff.json
```

Structural validation is only the first boundary. Before persistence, the
artifact store also verifies run, team, iteration, role, stage, commit,
acceptance-criterion, canonical-path, referenced-content, and digest context.
See [`runtime-evidence.md`](runtime-evidence.md) for the full evidence model.

## Benchmark Dependency and Image Updates

`make lock-benchmark` intentionally refreshes the benchmark dependency lock.
Set `BENCHMARK_EXCLUDE_NEWER=YYYY-MM-DD` only as part of a reviewed dependency
update, then rebuild and record a new sandbox image ID.

Build the exact image named by `configs/run-policy.json` with:

```bash
docker build \
  --tag sat-task-manager-quality:phase1-v1 \
  benchmarks/task_manager
```

At run start, the controller resolves the configured image tag to its local
`sha256:...` image ID. The run-scoped Agent configuration and every quality
gate use that immutable ID; preflight fails if the configured tag changes
between resolution and workspace setup.

The benchmark contract is frozen for comparable trials. The confirmed
[`task-brief.json`](../benchmarks/task_manager/task-brief.json) is the
authoritative Agent input. The human-readable
[`requirements.md`](../benchmarks/task_manager/requirements.md) summarizes it
and must not introduce additional requirements.

## Source-of-Truth Map

The authoritative owner for every documentary and executable concept is listed
once in [`VISION.md`](../VISION.md#ownership-boundaries). Use that table before
adding a new definition or moving an existing one.

Do not maintain parallel role lists, schemas, state machines, or legacy CLI
entry points. A replacement removes or migrates its predecessor in the same
change unless a time-bounded removal plan is documented.

## Repository Layout

```text
benchmarks/task_manager/
  task-brief.json              Frozen confirmed benchmark input
  requirements.md              Human-readable contract summary
  benchmark.json               Fixed commands and criterion coverage
  Dockerfile                   Frozen execution image
  requirements.lock            Benchmark dependency lock
  seed/                        Deterministic starting repository
configs/
  teams.json                   Team topology source of truth
  run-policy.json              Sandbox, aggregate, and role-stage budgets
  openclaw.example.json5       Sanitized role and tool policy template
docs/
  README.md                    Documentation index
  product-demo-slice.md        Next user-facing acceptance contract
  installation.md             Install, configure, export, and uninstall
  runtime-evidence.md          Runtime, artifact, evidence, and safety model
  phase1-runbook.md            Controlled provider-backed evaluation procedure
  development.md               This development guide
src/software_agent_team/
  artifacts.py                 Persisted schemas
  artifact_store.py            Write-once artifact and output persistence
  budgets.py                   Agent and pricing budgets
  cli.py                       Unified command-line interface
  execution.py                 OpenClaw and offline execution adapters
  git_workspace.py             Standalone clones and snapshot verification
  prompting.py                 Minimum-context role prompts
  quality_gates.py             Fixed sandboxed command runner
  responses.py                 Strict Agent semantic response parser
  run_control.py               Lifecycle state and atomic persistence
  runtime_configuration.py     Run-scoped OpenClaw config and preflight
  user_configuration.py        User-local secret-free live-run defaults
  workflow.py                  Phase 1 orchestration and final reporting
scripts/
  install.sh                   One-command Linux/WSL installation
  uninstall.sh                 Guided preservation, export, and removal
  setup.sh                     Development environment setup
  doctor.sh                    Environment and boundary diagnostics
tests/                         Offline unit, integration, and end-to-end tests
README.md                      Public entry point and command map
STATUS.md                      Current implementation and evaluation evidence
VISION.md                      Product, architecture, experiment, and roadmap
```

`openclaw/workspaces/` contains stable ignored role workspace boundaries.
Generated `runs/` and `workspaces/`, provider credentials, active OpenClaw
state, and runtime configuration never enter Git.

## Documentation Workflow

Update the document that owns the changed fact:

- Update `README.md` when the public overview, primary commands, or repository
  map changes;
- Update `VISION.md` when product behavior, architecture decisions,
  experimental design, scope, or roadmap changes;
- Update `STATUS.md` when an implementation path, milestone, evaluation
  evidence, or known gap changes;
- Update `docs/product-demo-slice.md` when the approved next user journey or
  its acceptance criteria change;
- Update `docs/installation.md` when setup, saved configuration, export, or
  uninstallation behavior changes;
- Update `docs/runtime-evidence.md` when runtime, artifact, response, integrity,
  or safety behavior changes;
- Update `docs/phase1-runbook.md` when a controlled provider-backed evaluation
  procedure or evidence checklist changes;
- Update benchmark documentation only with an explicitly versioned benchmark
  change.

Avoid copying a full contract into multiple documents. A summary should link
to its authoritative owner.

## Contribution Workflow

1. Inspect `git status` before editing.
2. Read `VISION.md` before changing architecture, scope, or experiments.
3. Keep every experimental variable and budget explicit.
4. Add or update tests with behavior changes.
5. Update public documentation in the same change.
6. Run `make check`.
7. Review the staged diff and commit with a Conventional Commit message.

Use this message form:

```text
<type>(<scope>): <lowercase subject without a period>
```

Negative and inconclusive outcomes are acceptable when their evidence and
limits are reported honestly.

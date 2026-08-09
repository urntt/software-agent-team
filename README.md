# Software Agent Team

Software Agent Team is an experimental, local-first command-line harness for
building software with configurable teams of AI Agents. It uses OpenClaw for
model execution while a deterministic Python control plane owns workflow
state, budgets, validation, Git isolation, and experiment reporting.

The product target is a short request followed by bounded clarification,
internal planning, implementation, testing, review, and revision, resulting in
one runnable delivery with reproducible evidence.

## Current Status

This repository currently implements the development and configuration
foundation plus the first deterministic control-plane slice. It includes:

- A reproducible Python 3.12 and OpenClaw toolchain;
- A unified `sat` validation CLI;
- A versioned manifest for three experimental team configurations;
- Validated task-brief and cross-Agent handoff contracts;
- Concrete plan, work, test, review, iteration, and final-report contracts;
- Immutable phase-artifact storage with canonical paths and content hashes;
- A persisted run lifecycle with validated transitions, atomic updates, and
  integrity-checked recovery;
- A sanitized OpenClaw role and permission template;
- A controlled task-management Web application benchmark;
- Offline tests for configuration, contracts, run control, CLI behavior, and
  repository boundaries.

It does **not** yet execute a complete live Agent workflow. The next milestone
is the function-specialized vertical slice described in [`VISION.md`](VISION.md).

## Product Flow

```text
Brief request
    ↓
Bounded requirement clarification
    ↓
Confirmed TaskBrief
    ↓
Selected team configuration
    ↓
Plan → implement → verify → review → bounded revision
    ↓
Runnable software + evidence + final report
```

For controlled team experiments, every configuration starts from the same
frozen confirmed `TaskBrief`. Requirement clarification is evaluated separately
so it does not confound the first topology comparison.

## Team Configurations

[`configs/teams.json`](configs/teams.json) is the versioned source of truth.

| Configuration | Purpose | Initial stages |
| --- | --- | --- |
| `single_agent` | One-pass baseline without independent Agent review | implement |
| `function_specialized` | Separate planning, generalist coding, testing, and review | plan → implement → verify |
| `implementation_domain_specialized` | Split frontend/backend coding with explicit integration while retaining planning and quality control | plan → parallel implement → integrate → verify |

The default configuration is `function_specialized` because it provides the
smallest complete multi-Agent quality loop. It is a starting point, not a
preselected experimental winner.

## Requirements

- Linux, macOS, or Windows through WSL;
- Git;
- Bash;
- Network access during initial setup;
- Docker or another supported sandbox backend before generated code is run.

The setup pins:

- Python 3.12;
- OpenClaw 2026.7.1-2;
- OpenClaw's user-local Node.js 24.15.0 runtime;
- Dependencies from `uv.lock`.

## Quick Start

```bash
make setup
make check
```

`make setup` installs missing user-local tools, synchronizes the locked Python
environment, and reconciles ignored role workspace directories with the team
manifest. It removes empty stale role directories but stops instead of
deleting a stale directory that contains local state.

`make check` verifies:

- Tool and runtime versions;
- Git and ignore boundaries;
- Team and OpenClaw configuration consistency;
- Checked-in example contracts;
- Formatting and lint rules;
- The complete offline test suite.

## CLI Commands

Validate the full configuration boundary:

```bash
uv run sat validate-config
```

List the experimental teams:

```bash
uv run sat list-teams
```

Validate a confirmed task brief:

```bash
uv run sat validate-task-brief examples/task-brief.json
```

Validate the structure of a persisted phase artifact:

```bash
uv run sat validate-artifact examples/implementation-plan.json
```

Structural CLI validation does not replace run-context validation. Before an
artifact is persisted, the artifact store also checks its run, team,
iteration, producer, and acceptance-criterion references against the frozen
task brief and selected team.

Validate a cross-Agent handoff against its selected team configuration:

```bash
uv run sat validate-handoff examples/handoff.json
```

The `sat run` workflow is intentionally absent until the deterministic
controller and OpenClaw adapter execute a real end-to-end trace. The repository
does not expose placeholder commands that imply unimplemented behavior.

## Persisted Run State

The Python `RunController` is the only authority that advances a run. It
validates every phase transition, enforces the selected team's iteration
limit, records structured termination reasons, and rejects updates based on an
obsolete state revision.

Local state is stored under the ignored `runs/` directory:

```text
runs/<run_id>/
├── task-brief.json                  # Frozen confirmed input
├── run.json                         # State and transition history
├── implementation-plan.json         # Write-once planning artifact
├── iterations/<nn>/                  # Write-once iteration artifacts
└── final-report.json                 # Write-once terminal report
```

Run creation and state replacement use atomic filesystem operations. Recovery
loads the last complete record, verifies hashes for the frozen task brief and
exact selected team definition, and resumes from the persisted phase. It does
not infer that an unrecorded external action succeeded.

The first function-specialized trace will use an explicit iteration limit of
two: one initial implementation and at most one revision. This remains below
the team manifest's general maximum of three iterations.

## Development Commands

```bash
make setup          # Install or synchronize the pinned local toolchain
make doctor         # Validate environment, configuration, and Git boundaries
make validate       # Validate team and OpenClaw configuration
make format         # Format Python source and tests
make format-check   # Check formatting without modifying files
make lint           # Run Ruff
make test           # Run pytest
make check          # Run all required checks
make lock           # Refresh uv.lock after intentional dependency changes
```

## Repository Layout

```text
benchmarks/
└── task_manager/             # Controlled first benchmark specification
configs/
├── openclaw.example.json5    # Sanitized role and permission template
└── teams.json                # Versioned experiment configurations
examples/
├── handoff.json              # Valid HandoffEnvelope example
├── implementation-plan.json # Valid phase-artifact example
└── task-brief.json           # Valid confirmed TaskBrief example
openclaw/
└── workspaces/               # Ignored local role workspaces
scripts/
├── doctor.sh                 # Non-mutating environment diagnostics
└── setup.sh                  # Idempotent toolchain setup
src/software_agent_team/
├── artifact_store.py         # Immutable artifact persistence and integrity
├── artifacts.py              # Persisted artifact schema source of truth
├── cli.py                    # Implemented foundation CLI
├── configuration.py          # Cross-configuration validation
├── run_control.py            # Persisted lifecycle and recovery boundary
└── teams.py                  # Team manifest models and validation
tests/                        # Offline contract and configuration tests
VISION.md                     # Product, architecture, and development decisions
```

## Architecture Boundary

The deterministic Python control plane owns persisted lifecycle state and will
also own:

- Run lifecycle and state transitions;
- Team selection and stage ordering;
- Time, iteration, model-call, and cost budgets;
- Artifact validation;
- Git worktrees and immutable iteration commits;
- Deterministic quality gates;
- Metrics and final reports.

OpenClaw owns:

- Model and provider integration;
- Role sessions and context isolation;
- Tool exposure;
- Sandbox integration;
- Agent execution and runtime diagnostics.

Agents own semantic work such as clarification, planning, implementation,
failure analysis, and review. No Agent is registered as the authoritative
workflow coordinator.

## Configuration Ownership

- `configs/teams.json` owns team membership and initial stage ordering.
- `src/software_agent_team/teams.py` owns manifest validation rules.
- `src/software_agent_team/artifacts.py` owns persisted artifact schemas.
- `src/software_agent_team/artifact_store.py` owns canonical artifact paths,
  immutable writes, content hashes, and contextual loading.
- `src/software_agent_team/run_control.py` owns lifecycle state, transition
  validation, persistence, and recovery.
- `configs/openclaw.example.json5` records the sanitized Agent runtime boundary.
- `VISION.md` owns product and architecture decisions.

Generate downstream representations from these sources instead of maintaining
parallel hand-written definitions.

## OpenClaw and Secrets

The checked-in OpenClaw file is a template, not an active configuration.
Materialize active configuration and provider credentials in a trusted
user-local location.

Never commit:

- Provider API keys;
- Active `openclaw.json` files;
- Session history or memory;
- Raw generated runs;
- Generated worktrees;
- Role workspace state;
- Machine-specific paths or credentials.

Read-only roles deny filesystem mutation and shell/process tools. Coding and
integration roles receive read-write workspace access, but live generated-code
execution still requires a configured sandbox and an isolated Git worktree.

## First Benchmark

The first benchmark asks each configuration to build the same FastAPI,
Jinja2, and SQLite task-management Web application. The exact requirements and
acceptance criteria are in
[`benchmarks/task_manager/requirements.md`](benchmarks/task_manager/requirements.md).

The benchmark is an evaluation input. It is not a prebuilt application and is
not the product delivered by this repository.

## Contribution Workflow

1. Read `VISION.md` before architecture, experiment, or scope changes.
2. Keep experimental variables explicit in `configs/teams.json`.
3. Add or update tests with behavior changes.
4. Update documentation when behavior or architecture changes.
5. Run `make check`.
6. Review the staged diff and commit with a Conventional Commit message.

An experiment may produce a negative or inconclusive result. That is valid as
long as the run is reproducible and the trade-offs and failures are reported
honestly.

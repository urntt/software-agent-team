# Software Agent Team

Software Agent Team is an experimental, local-first CLI harness that uses a
deterministic Python control plane to coordinate AI Agent teams through
OpenClaw. A confirmed software request becomes an isolated Git result with
planning, implementation, quality-gate, review, revision, telemetry, and final
report evidence.

The first supported vertical slice builds a controlled task-management Web
application with a function-specialized team:

```text
Planner
  → Generalist Developer
  → controller-verified Git snapshot
  → fixed sandboxed quality gates
  → independent Tester + Reviewer
  → deterministic ACCEPT / REVISE / FAIL decision
  → at most one Developer revision
  → machine-readable and human-readable final reports
```

## Status

Phase 1 is complete as an engine milestone. The current harness has passed its
offline end-to-end suite and produced a qualifying provider-backed evaluation
plus two consecutive replays that reached `completed` through the bounded
evidence-driven revision loop. Each completed evaluation preserved
controller-verified Git, quality-gate, acceptance, independent-review, model,
token, duration, and artifact-integrity evidence.

The intended first-run product journey is not implemented yet. The current
`sat` entry point prints an operator guide, while an actual build still starts
from an existing TaskBrief and source path. The next milestone is the
[`Product Demo Slice`](docs/product-demo-slice.md): managed installation,
automatic diagnostics, guided configuration, a natural-language request and
bounded clarification flow, automatic internal preparation, visible progress,
and a direct runnable delivery.

The executable single-Agent baseline, implementation-domain-specialized path,
repeated comparative evaluation, and automatic CLI resume also remain future
work. See [`STATUS.md`](STATUS.md) for the exact implementation boundary and
[`VISION.md`](VISION.md) for the product direction and roadmap.

## Intended Product Experience

```text
one installation command
→ enter or create a project folder
→ run `sat`
→ automatic diagnostics and first-run configuration
→ "What would you like to build?"
→ bounded clarification and requirements confirmation
→ controller-backed summaries and progress
→ runnable project, verification result, and exact next commands
```

Normal users must not prepare run IDs, TaskBrief JSON, benchmark repositories,
team IDs, policy paths, concurrency, timeouts, repair limits, or evidence roots.
Those remain internal or advanced evaluation concepts. See
[`docs/product-demo-slice.md`](docs/product-demo-slice.md) for the complete
acceptance contract.

## Architecture at a Glance

The deterministic controller is the only workflow authority. It owns phase
ordering, budgets, artifact validation, Git snapshots, quality-gate evidence,
decisions, and terminal reports. OpenClaw owns provider integration, role
sessions, tool exposure, and Agent sandboxing. Agents own semantic work such as
planning, implementation, evidence analysis, and review.

Persisted artifacts, rather than hidden chat history, are the authoritative
handoff boundary. The controller assembles each persisted artifact from a
validated Agent semantic response and controller-owned facts; models never
need to echo known identity, Git, command, status, criterion, or scope fields.
A failed run remains a valid auditable result.

Read [`VISION.md`](VISION.md) for decisions and experimental design, and
[`docs/runtime-evidence.md`](docs/runtime-evidence.md) for the implemented
runtime, evidence, response, and safety boundaries.

## Requirements

- Linux, or Windows through WSL;
- Git, Bash, and curl;
- Docker for live Agent sandboxes and generated-code quality gates;
- An unprivileged host account with access to the Docker daemon;
- Network access for initial setup and the selected model provider;
- Provider credentials configured through OpenClaw or a trusted caller
  environment, never in this repository.

The checked-in setup pins Python 3.12, OpenClaw 2026.7.1-2, OpenClaw's local
Node.js 24.15.0 runtime, and Python dependencies through `uv.lock`.

## Current Evaluation Quick Start

The commands below document the implemented Phase 1 contributor/operator
surface. They are useful for controlled evaluation and regression work, but
they are not the target first-run product experience described above.

Install from a clean checkout on Linux or WSL:

```bash
./scripts/install.sh
```

The installer prepares the pinned user-local toolchain, locked project
environment, benchmark image, offline checks, and checkout-bound `sat` and
`sat-uninstall` launchers. It does not install an OS-level Docker daemon or
create provider credentials.

Start the current operator guide and save secret-free evaluation defaults:

```bash
sat
sat configure
sat configure --show
```

Configure provider credentials separately through OpenClaw, then prepare and
check a deterministic benchmark source repository:

```bash
$HOME/.openclaw/bin/openclaw configure --section model
$HOME/.openclaw/bin/openclaw models status --check

sat prepare-benchmark ./task-manager-source
sat preflight ./task-manager-source
```

Run the Phase 1 evaluation workflow with the saved model, prices, verification
concurrency, and timeout policy:

```bash
sat run \
  benchmarks/task_manager/task-brief.json \
  ./task-manager-source
```

`sat run` returns `0` for an accepted completed run, `2` for an auditable
failed run, and `1` when invalid input or local setup prevents a normal
workflow result. Follow the complete
[`Phase 1 provider-backed evaluation runbook`](docs/phase1-runbook.md) before
treating a run as qualifying experimental evidence.

## Command Map

| Command | Purpose | Detailed reference |
| --- | --- | --- |
| `sat` | Currently show operator onboarding; Phase 2 turns it into the guided product entry point | [`docs/product-demo-slice.md`](docs/product-demo-slice.md) |
| `sat configure` | Create or replace secret-free model, pricing, concurrency, and timeout defaults | [`docs/installation.md`](docs/installation.md) |
| `sat prepare-benchmark PATH` | Advanced: materialize a deterministic evaluation source repository | [`docs/phase1-runbook.md`](docs/phase1-runbook.md) |
| `sat preflight PATH` | Advanced: validate evaluation runtime, image, and source without a model call | [`docs/phase1-runbook.md`](docs/phase1-runbook.md) |
| `sat run BRIEF PATH` | Advanced: execute a controlled workflow from explicit inputs | [`docs/phase1-runbook.md`](docs/phase1-runbook.md) |
| `sat validate-*` and `sat list-teams` | Validate checked-in contracts or inspect team definitions | [`docs/development.md`](docs/development.md) |
| `sat-uninstall` | Preserve by default, optionally export, and explicitly purge installed SAT state | [`docs/installation.md`](docs/installation.md) |
| `make setup` / `make check` | Prepare and validate a development checkout offline | [`docs/development.md`](docs/development.md) |

Run-specific `sat run` flags override saved defaults without modifying the
configuration file. Use `--verification-concurrency 1` for a provider that can
serve only one generation at a time. Use real token prices for paid models;
zero is appropriate only for a genuinely free model.

## Documentation

[`docs/README.md`](docs/README.md) is the documentation index. The primary
documents have deliberately separate responsibilities:

| Document | Responsibility |
| --- | --- |
| [`README.md`](README.md) | Public overview, command map, and repository map |
| [`VISION.md`](VISION.md) | Product contract, architecture decisions, experiment design, scope, and roadmap |
| [`STATUS.md`](STATUS.md) | Current implementation, evaluation evidence, known gaps, and next milestone |
| [`docs/product-demo-slice.md`](docs/product-demo-slice.md) | Next user-facing installation, onboarding, request, progress, and delivery acceptance contract |
| [`docs/installation.md`](docs/installation.md) | Installation, first-launch configuration, saved defaults, export, and uninstallation |
| [`docs/runtime-evidence.md`](docs/runtime-evidence.md) | Runtime authority, response boundary, persisted evidence, integrity, and operator safety |
| [`docs/phase1-runbook.md`](docs/phase1-runbook.md) | Contributor/operator procedure for a controlled Phase 1 provider-backed evaluation |
| [`docs/development.md`](docs/development.md) | Local setup, checks, validation commands, layout, and contribution workflow |
| [`benchmarks/task_manager/requirements.md`](benchmarks/task_manager/requirements.md) | Human-readable summary of the frozen benchmark contract |

## Repository Layout

```text
benchmarks/task_manager/       Frozen brief, seed, acceptance suite, and image
configs/                       Team, run-policy, and sanitized runtime inputs
docs/                          Installation, operations, and development guides
examples/                      Example validated artifacts and handoffs
openclaw/                      Stable role workspace boundaries
scripts/                       Setup, installation, uninstall, and diagnostics
src/software_agent_team/       Controller, contracts, adapters, and CLI
tests/                         Offline unit, integration, and end-to-end tests
STATUS.md                      Current implementation and evidence boundary
VISION.md                      Product and architecture decisions
```

Generated `runs/` and `workspaces/` are local, ignored state. The harness does
not merge, push, deploy, or publish generated results, and human authorization
remains required for those actions.

## Contributing

Read [`VISION.md`](VISION.md) before changing architecture, scope, or
experimental design. Keep variables and budgets explicit, add or update tests
with behavior changes, update the owning document in the same change, and run:

```bash
make check
```

Negative and inconclusive outcomes are acceptable when their evidence and
limits are reported honestly. See
[`docs/development.md`](docs/development.md) for the full workflow.

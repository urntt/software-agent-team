# Software Agent Team

Software Agent Team is an experimental, local-first CLI harness that uses a
deterministic Python control plane to coordinate AI Agent teams through
OpenClaw. A confirmed software request becomes an isolated Git result with
planning, implementation, quality-gate, review, revision, telemetry, and final
report evidence.

The first product profile builds small greenfield Python 3.12 projects, such as
Web applications, CLI tools, and local automation, with a
function-specialized team:

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

Phase 1 is complete as an engine milestone. The harness has passed its
offline end-to-end suite and produced a qualifying provider-backed evaluation
plus two consecutive replays that reached `completed` through the bounded
evidence-driven revision loop. Each completed evaluation preserved
controller-verified Git, quality-gate, acceptance, independent-review, model,
token, duration, and artifact-integrity evidence.

The [`Product Demo Slice`](docs/product-demo-slice.md) is now implemented and
offline tested. A managed installation can launch `sat`, diagnose the local
runtime, guide secret-free model setup, collect and confirm a request, generate
all internal run inputs, show controller-backed progress, and atomically
deliver an accepted Git result. The current product scope is deliberately
bounded by the checked-in Python execution profile, but the request,
requirements, success conditions, project commands, and delivered behavior are
created from the user's confirmed intent rather than from an evaluation
benchmark.

This product journey has not yet completed its fresh-device, provider-backed
acceptance rehearsal. Until that evidence exists, the repository does not
claim the demo path is release-stable. See [`STATUS.md`](STATUS.md) for that
exact boundary.

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

## Product Quick Start

On Linux or WSL with Docker installed and running, use the managed installer:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/urntt/software-agent-team/main/scripts/bootstrap.sh \
  | bash && exec "${SHELL:-/bin/bash}" -l
```

Then enter a directory that may receive a new project child and run:

```bash
sat
```

SAT performs local diagnostics before asking for a model or making a provider
request. On first launch it delegates credential entry to OpenClaw, stores only
the selected `provider/model` reference, and offers an explicitly authorized
minimal smoke check. It then asks what to build, explains the current Python
execution profile, collects success conditions and optional constraints, asks
for a new project-directory name, shows the resulting requirements summary,
and requires confirmation before the model-backed workflow begins.

The product path never substitutes the task-manager evaluation fixture for a
user request. Requests that cannot use the current local Python profile are
declined explicitly; expanding the set of execution profiles is roadmap work.

## Advanced Evaluation Quick Start

The commands below document the implemented Phase 1 contributor/operator
surface. They are useful for controlled evaluation and regression work, but
they are not the target first-run product experience described above.

Contributors may install from a clean checkout on Linux or WSL:

```bash
./scripts/install.sh
```

The installer prepares the pinned user-local toolchain, locked project
environment, shared Python quality image, offline checks, and checkout-bound `sat` and
`sat-uninstall` launchers. It does not install an OS-level Docker daemon or
create provider credentials.

Save or inspect secret-free advanced evaluation defaults:

```bash
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
| `sat` | Run diagnostics, first-use setup, request confirmation, progress, and safe delivery | [`docs/product-demo-slice.md`](docs/product-demo-slice.md) |
| `sat configure` | Reconfigure the secret-free model reference; advanced flags can also set evaluation defaults | [`docs/installation.md`](docs/installation.md) |
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
| [`docs/product-demo-slice.md`](docs/product-demo-slice.md) | User-facing installation, onboarding, request, progress, and delivery acceptance contract |
| [`docs/installation.md`](docs/installation.md) | Installation, first-launch configuration, saved defaults, export, and uninstallation |
| [`docs/runtime-evidence.md`](docs/runtime-evidence.md) | Runtime authority, response boundary, persisted evidence, integrity, and operator safety |
| [`docs/phase1-runbook.md`](docs/phase1-runbook.md) | Contributor/operator procedure for a controlled Phase 1 provider-backed evaluation |
| [`docs/development.md`](docs/development.md) | Local setup, checks, validation commands, layout, and contribution workflow |
| [`profiles/python/README.md`](profiles/python/README.md) | First generated-project execution profile and command/evidence contract |
| [`benchmarks/task_manager/requirements.md`](benchmarks/task_manager/requirements.md) | Human-readable summary of the frozen evaluation fixture; it is not the product request contract |

## Repository Layout

```text
benchmarks/task_manager/       Frozen evaluation brief, seed, and acceptance suite
configs/                       Team, product/evaluation policy, and runtime inputs
docs/                          Installation, operations, and development guides
examples/                      Example validated artifacts and handoffs
openclaw/                      Stable role workspace boundaries
profiles/python/               Product seed, quality contract, and validator
runtime/python/                Shared content-pinned quality image and dependency lock
scripts/                       Setup, installation, uninstall, and diagnostics
src/software_agent_team/       Product flow, controller, contracts, adapters, and CLI
tests/                         Offline unit, integration, and end-to-end tests
STATUS.md                      Current implementation and evidence boundary
VISION.md                      Product and architecture decisions
```

Product runs, workspaces, and trusted source baselines live beneath
`${XDG_STATE_HOME:-$HOME/.local/state}/software-agent-team/` and never enter
Git. The harness does not merge, push, deploy, or publish generated results,
and human authorization remains required for those actions.

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

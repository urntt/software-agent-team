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

`make setup` prepares the pinned local toolchain, locked Python environment,
and a marked private OpenClaw runtime at `.sat/openclaw/`. It never discovers,
adopts, or changes another OpenClaw installation or profile. `make check` is
fully offline after setup. It verifies tool versions, ownership, Git and ignore
boundaries, configuration contracts, formatting, lint, the complete test
suite, and all offline workflow paths. It does not call a model or require
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
make lock-runtime
```

Use `make format` when source formatting changes are required. Always run
`make check` before committing.

## Checkout Installation

Contributors who need checkout-bound `sat` and `sat-uninstall` launchers may
run:

```bash
./scripts/install.sh
```

This path performs the locked runtime, image, configuration, and launcher setup
used by a managed installation, then also runs formatting, lint, and the full
offline test suite. It does not mark the checkout as a managed application.
The uninstaller can therefore remove SAT's launchers, environment, and private
OpenClaw runtime while preserving the development checkout. Normal users
should use the managed command in the repository
[`README.md`](../README.md#install).

To update a checkout-bound installation, update the checkout through the
contributor's normal Git workflow, confirm that the tracked worktree is clean,
and rerun `./scripts/install.sh`.

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

Validate the product profile separately from the default evaluation fixture:

```bash
uv run sat validate-config \
  --policy configs/product-policy.json \
  --quality-manifest profiles/python/quality.json
```

## Runtime Image and Evaluation-Fixture Updates

`make lock-runtime` intentionally refreshes the shared Python runtime dependency
lock. Set `RUNTIME_EXCLUDE_NEWER=YYYY-MM-DD` only as part of a reviewed
dependency update, then rebuild and record a new sandbox image ID.

Build the exact image named by both product and evaluation policies with:

```bash
docker build \
  --tag sat-python-quality:phase1-v6 \
  runtime/python
```

OpenClaw explicitly supplies `sleep infinity` when it creates a scope-owned
role container; the image uses the same command as a convenient standalone
diagnostic default. `scripts/install.sh` and live-run preflight both start a
no-network, read-only-root probe, execute the Reviewer probe runner's self-test
inside it, inspect its state, and remove it. A successful `docker build`, image
lookup, or momentary container start alone is not sufficient runtime evidence.

The image includes the exact `uv` pinned in `runtime/python/requirements.in`
and a locked offline wheelhouse containing project setup and build
dependencies. The product quality profile copies clean committed files into
fresh executable tmpfs scratch, then runs the exact generated setup, test, and
start argv with network disabled. The source and container root remain
read-only, and the process remains non-root, capability-dropped, and
resource-bounded. Before setup, the profile parses every `uv.lock` tracked in
the proposed Git delivery, even when an ignore rule also matches it, and rejects
host- or sandbox-only local sources. An effectively ignored untracked lock is
runtime residue outside the delivery and is absent from the clean-copy command
gate; the private wheelhouse may satisfy runtime resolution but its absolute
path must never be committed into the generated project. The image also
installs the root-owned immutable
`sat-probe-write` helper. That command can atomically create only a new bounded
`/tmp/sat-review-probe-*` `.py`, `.json`, or `.txt` direct child; it refuses
overwrite and unsafe paths while the project mount and general write tools stay
read-only. Authored Python probes run only through `sat-probe-run`; it validates
the owner-only file, executes its open descriptor with a fixed interpreter and
project working directory, enforces a 30-second child timeout and bounded
output, and emits `SAT_PROBE_RESULT_V1` as the authoritative terminal child
result. Its explicit stdout/stderr frames let the controller exclude traceback
source text from positive satisfied claims while preserving stderr for blocked
counterexamples. Change either runtime capability and the policy image tag together; an
old tag must not claim the newer probe capability.

Use the Docker cgroup `--pids-limit` for the per-container process boundary.
Do not add an `nproc` ulimit as a duplicate control: `RLIMIT_NPROC` can count
processes sharing the numeric UID outside the container, which can make Docker
fail to execute even the container's initial process with `EAGAIN`.

At run start, the controller resolves the configured image tag to its local
`sha256:...` image ID. The run-scoped Agent configuration and every quality
gate use that immutable ID; preflight fails if the configured tag changes
between resolution and workspace setup or if the restricted probe cannot run
its tool helper and remain alive.

## Model Catalog Compatibility

`src/software_agent_team/runtime_configuration.py` owns reviewed catalog
supplements for exact provider models absent from the pinned OpenClaw release.
A supplement must remain narrow, versioned in Git, secret-free, and covered by
materialization plus exact-availability tests. Record only verified routing,
modality, context, output, and compatibility metadata; do not add a credential,
mutable fallback, or guessed price.

Startup and run preflight inspect OpenClaw's configured model view without a
provider filter so the check stays on configured local catalog/auth evidence
and does not invoke provider discovery or content generation. A real provider
smoke request is a separate explicitly authorized test. When validating a
trusted shell credential path, the generated configuration may contain the
environment-variable reference but never its value.

The product profile and evaluation fixture share this dependency image, not a
TaskBrief, seed, acceptance suite, environment contract, or delivery command.
The benchmark contract remains frozen for comparable trials. The confirmed
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
  seed/                        Deterministic starting repository
configs/
  teams.json                   Team topology source of truth
  product-policy.json          Product sandbox and bounded run policy
  run-policy.json              Controlled evaluation policy
  openclaw.example.json5       Sanitized role and tool policy template
docs/
  README.md                    Documentation index
  product-demo-slice.md        Guided user-journey acceptance specification
  adaptive-orchestration.md    Dynamic-team and interactive-control specification
  installation.md             Install, configure, export, and uninstall
  runtime-evidence.md          Runtime, artifact, evidence, and safety model
  phase1-runbook.md            Controlled provider-backed evaluation procedure
  development.md               This development guide
profiles/python/
  contract-template.json       Stable product criterion-ID contract
  quality.json                 Generic project checks and coverage
  seed/                        Greenfield product source baseline
  validation/run.py            Trusted project-command and docs validator
  validation/run_commands.py   Clean-copy exact-command validator
runtime/python/
  Dockerfile                   Shared content-pinned quality image
  requirements.in             Direct runtime dependencies
  requirements.lock           Exact transitive dependency lock
src/software_agent_team/
  artifacts.py                 Persisted schemas
  artifact_store.py            Write-once artifact and output persistence
  assembly.py                  Semantic response and verified-fact assembly
  budgets.py                   Agent and pricing budgets
  cli.py                       Unified command-line interface
  controls.py                  Persisted user-control command contracts
  dynamic_runner.py            Approved dynamic Agent invocation lifecycle
  dynamic_workflow.py          Adaptive lifecycle convergence and decisions
  execution.py                 OpenClaw and offline execution adapters
  git_workspace.py             Standalone clones and snapshot verification
  integrity.py                 Canonical persisted-model integrity digest
  invocation.py                Controller-owned call accounting and evidence
  openclaw_session_evidence.py Pinned current-turn tool-evidence extraction
  openclaw_runtime.py          Private OpenClaw path and environment isolation
  paths.py                     User-local product state resolution
  planning.py                  Adaptive dialogue, proposals, approval, and evidence
  product.py                   Diagnostics, source preparation, and delivery
  progress.py                  RunEvent journal and terminal rendering
  prompting.py                 Fixed-role and task-defined capability prompts
  quality_gates.py             Fixed sandboxed command runner
  reporting.py                 Shared terminal report rendering
  responses.py                 Strict fixed and run-scoped Agent response parser
  run_control.py               Lifecycle state and atomic persistence
  runtime_configuration.py     Run-scoped OpenClaw config and preflight
  scheduling.py                Approved DAG and shared-workspace scheduling
  teams.py                     TeamPlan contracts and fixed-fixture compilation
  user_configuration.py        User-local secret-free live-run defaults
  workflow.py                  Fixed-fixture compatibility orchestration
scripts/
  bootstrap.sh                 Remote managed-install entry point
  install.sh                   Locked Linux/WSL application installation
  openclaw-environment.sh      Private OpenClaw shell-process environment
  uninstall.sh                 Guided preservation, export, and removal
  setup.sh                     Development environment setup
  doctor.sh                    Environment and boundary diagnostics
tests/                         Offline unit, integration, and end-to-end tests
README.md                      User-facing public overview and quick start
STATUS.md                      Current implementation and evaluation evidence
VISION.md                      Product, architecture, experiment, and roadmap
```

`openclaw/workspaces/` contains stable ignored role workspace boundaries. The
installed private OpenClaw binary lives under ignored `.sat/openclaw/`.
Product `planning/`, `runs/`, `workspaces/`, `sources/`, and isolated OpenClaw
provider state live under the private user-state root. Provider credentials,
active OpenClaw state, generated state, and runtime configuration never enter
Git.

## Documentation Workflow

Update the document that owns the changed fact:

- Update `README.md` only when the user-facing overview, requirements, install
  command, first-use path, or primary user commands change;
- Update `VISION.md` when product behavior, architecture decisions,
  experimental design, scope, or roadmap changes;
- Update `STATUS.md` when an implementation path, milestone, evaluation
  evidence, or known gap changes;
- Update `docs/product-demo-slice.md` when the guided user-journey interaction
  or acceptance criteria change; keep implementation status in `STATUS.md`;
- Update `docs/adaptive-orchestration.md` when the planned Planning, TeamPlan,
  progress, control, model-routing interaction, implementation sequence, or
  acceptance contract changes; keep implementation status in `STATUS.md`;
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

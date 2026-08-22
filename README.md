# Software Agent Team

Software Agent Team is an experimental, local-first CLI harness that uses a
deterministic Python control plane to coordinate a team of AI Agents through
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
  → independent Tester + Reviewer (parallel or provider-limited serial dispatch)
  → deterministic ACCEPT / REVISE / FAIL decision
  → at most one Developer revision
  → machine-readable and human-readable final reports
```

## Status

The Phase 1 implementation is complete and covered by offline end-to-end
tests. The code now includes:

- A real `sat run` entry point for the `function_specialized` team;
- A replaceable OpenClaw subprocess adapter with stable role sessions,
  version-pinned local/Gateway JSON parsing, and canonical model telemetry;
- Role-specific minimum-context prompts, strict semantic-response parsing, and
  controller assembly of persisted artifact facts;
- A persisted lifecycle whose phase transitions require artifact evidence;
- Write-once phase artifacts, handoffs, execution logs, and SHA-256 references;
- Detached standalone Git clones and controller-verified commit snapshots;
- A frozen benchmark TaskBrief, seed repository, dependency lock, acceptance
  suite, image recipe, and policy;
- Docker quality gates with no network, read-only source mounts, non-root
  execution, resource limits, bounded output, and fixed commands;
- Independent Tester and Reviewer execution against the same immutable commit,
  parallel by default or serialized for a single-generation provider;
- Explicit command-to-criterion coverage, manual-review scope, and
  controller-resolved acceptance results instead of treating semantic review
  as a missing dependency;
- Bounded command-output tails in verification prompts and a correct read-only
  source mount for independent review;
- Tool-policy enforcement that prevents role Agents from spawning untracked
  model calls outside controller accounting;
- One controlled semantic-response repair within the original role-stage
  deadline, and at most one implementation revision;
- A pre-call Agent invocation cap and post-call token, duration, and
  estimated-cost stop thresholds;
- Explicit completed and failed terminal states with JSON and Markdown reports;
- A one-command Linux/WSL installer that prepares the pinned toolchain, locked
  project environment, `sat` and `sat-uninstall` launchers, frozen Docker image,
  and offline checks;
- First-launch and repeatable `sat configure` guidance with private,
  secret-free run defaults and explicit CLI override precedence;
- A safe uninstaller that preserves configuration and generated evidence by
  default, can export both first, and purges them only by explicit request;
- Offline success, revision, timeout, evidence-tampering, non-convergence,
  no-change, invalid-response, and budget-failure tests.

Phase 1 has produced a live version-two trace that reached `completed`: one
controller-verified implementation commit passed every deterministic gate, all
ten acceptance criteria, and independent review, with complete model, token,
hash, and Git-boundary evidence. Earlier version-one traces remain exploratory
evidence and are not comparable with version-two results. Two consecutive live
replays of the current harness commit have also reached `completed`, each using
the bounded evidence-driven revision loop. Installation, run-default onboarding,
and safe uninstallation are implemented. Provider credential creation remains
an OpenClaw/operator responsibility, and repeated comparative experiments
remain pending. See [`docs/phase1-runbook.md`](docs/phase1-runbook.md).

Interactive clarification, the single-Agent baseline path, domain-specialized
implementation, repeated comparisons, and automatic interrupted-run resume are
later phases. Phase 1 starts from a confirmed `TaskBrief` and a fresh run ID.

## Architecture

The deterministic controller is the only workflow authority. It owns phase
ordering, iteration and resource budgets, artifact validation, Git evidence,
quality-gate evidence, decisions, and terminal reports. No Agent may advance
the lifecycle or declare its own work accepted.

OpenClaw owns model/provider integration, role sessions, tool exposure, and the
Agent sandbox. Agents own semantic work: planning, coding, evidence analysis,
and review. Persisted artifacts, rather than hidden chat history, are the
authoritative handoff boundary.

The controller assembles every persisted phase artifact from two distinct
sources: the Agent's validated semantic response body and controller-owned
facts. Artifact identity and envelope fields, run/team/role context, Git
snapshots and changed files, fixed commands and their results, acceptance
coverage, and manual-review scope never depend on a model echoing known values.

The controller accepts an iteration only when all of the following agree:

1. The Developer returns a semantic work summary, then the controller verifies
   a clean descendant Git commit and binds its exact changed-file set into the
   `WorkResult`.
2. The Tester analyzes the supplied evidence, while the controller binds the
   actual commands, exit-derived status, command-to-criterion coverage, and
   blocker state into the `TestReport`.
3. Every deterministic criterion passes; criteria assigned to independent
   review remain explicitly `pending_review` in the Tester's criterion results,
   while the overall Tester status is `passed` when no deterministic failure or
   blocker exists.
4. The Reviewer evaluates the controller-supplied manual-review scope on the
   same immutable commit and returns `accept` with no blocking finding; the
   controller binds that commit and scope into the `ReviewReport`.
5. The controller, not either Agent, resolves those pending criteria to
   `passed` in the final report.

Reviewer severity and controller termination are separate concepts. Any
correctable implementation defect, including a failed acceptance gate or a
critical-impact product bug, produces `revise` while the iteration budget
allows it. Reviewer `fail` requires an explicit terminal reason proving that a
run safety or evidence-integrity boundary makes another Developer revision
unsafe.

A failed run is a valid, auditable result. Provider failures, invalid
artifacts, timeouts, missing runtime telemetry, missing dependencies, budget
exhaustion, unsafe Git state, and iteration-limit exhaustion remain visible in
`run.json` and the final reports.

The response boundary accepts one unambiguous semantic JSON object, either raw,
inside one `json` code fence, or surrounded by presentation-only prose.
Surrounding text is discarded only when it contains no other JSON structure or
fence. If a model also returns controller-owned fields, they are ignored and
recorded in the immutable execution record; missing or incorrect echoes such as
`kind`, commit hashes, test status, or command lists do not trigger repair.
Duplicate keys, multiple objects, multiple fences, non-standard constants,
unknown semantic fields, and invalid semantic content remain invalid and may
consume the one bounded repair attempt. That repair receives a bounded,
value-free structural diagnostic, such as the duplicate key name, while the
execution record retains the raw provider output.

## Requirements

- Linux, or Windows through WSL;
- Git and Bash;
- Docker for live Agent sandboxes and generated-code quality gates;
- An unprivileged host account for preflight and live runs;
- Network access only for initial setup and the selected model provider;
- Provider credentials configured through OpenClaw or the trusted caller
  environment, never in this repository.

The checked-in setup pins Python 3.12, OpenClaw 2026.7.1-2, OpenClaw's local
Node.js 24.15.0 runtime, and Python dependencies through `uv.lock`.

## Installation

From a clean checkout on Linux or WSL, run:

```bash
./scripts/install.sh
```

The installer must run as an unprivileged user with Git, curl, and access to a
running Docker daemon already available. It installs the pinned uv, Python, and
OpenClaw toolchain when needed; synchronizes the locked project environment;
builds the benchmark image named by `configs/run-policy.json`; runs
configuration, formatting, lint, and test checks; and creates checkout-bound
`$HOME/.local/bin/sat` and `$HOME/.local/bin/sat-uninstall` launchers. It is
idempotent for the same checkout and refuses to overwrite unrelated commands.
Use `SAT_BIN_DIR`, `UV_BIN`, or `OPENCLAW_PREFIX` only when the corresponding
user-local location must be changed.

The installer neither installs an OS-level Docker daemon nor creates provider
credentials or an active OpenClaw provider configuration. Keep those values
outside the checkout. Start the installed command without arguments to enter
the first-launch guide:

```bash
sat
```

## First Launch and Configuration

`sat` reports whether user defaults exist and always prints the provider,
benchmark-preparation, preflight, run, reconfiguration, and uninstall path.
Create or replace the defaults interactively with:

```bash
sat configure
```

The saved values are the exact OpenClaw model reference, current input and
output prices per million tokens, Tester/Reviewer concurrency, and an optional
global role-stage timeout override. They are stored with mode `0600` in
`${XDG_CONFIG_HOME:-$HOME/.config}/software-agent-team/config.json`. Set an
absolute `SAT_CONFIG_PATH` only when this location must be overridden, and keep
the same override set for later `sat` and `sat-uninstall` invocations.

Without a global override, `configs/run-policy.json` supplies measured
role-specific stage budgets: 120 seconds for Clarifier and Planner, 900 seconds
for implementation roles, and 300 seconds for Tester and Reviewer. One stage
budget covers the initial response and its optional repair together; repair
does not restart the clock. The resolved values are frozen in `run.json`.

Provider credentials are deliberately not accepted or stored. Configure them
through OpenClaw's credential store or the trusted caller environment, then
inspect both provider and SAT state with:

```bash
$HOME/.openclaw/bin/openclaw configure --section model
$HOME/.openclaw/bin/openclaw models status --check
sat configure --show
```

For scripted setup, supply every required first-time value explicitly:

```bash
sat configure --non-interactive \
  --model provider/model \
  --input-cost-per-million-usd 0.00 \
  --output-cost-per-million-usd 0.00 \
  --verification-concurrency 1 \
  --use-role-timeouts
```

Use real prices for a paid model. A later `sat configure` run replaces the
saved defaults atomically; run-specific `sat run` flags take precedence without
modifying the saved file. Use `--stage-timeout-seconds N` only when an
experiment deliberately gives every role the same stage budget. Use
`--use-role-timeouts` to clear a saved override or ignore it for one run. The
old `--agent-timeout-seconds` spelling is accepted only as a deprecated alias
for the same shared-stage semantics and is scheduled for removal in the next
major release.

## Uninstallation

Run the guided uninstaller from any directory:

```bash
sat-uninstall
```

The default removes the two launchers and this checkout's `.venv` while
preserving the SAT configuration, `runs/`, `workspaces/`, source checkout,
OpenClaw and its credentials, uv, Docker, and the benchmark image. Export the
SAT configuration and default generated data before uninstalling with:

```bash
sat-uninstall --export-to "$HOME/sat-backup" --yes
```

The destination must be absolute and must not already exist. The export
contains `configuration/config.json`, available default `data/runs/` and
`data/workspaces/`, and `EXPORT.txt`. It intentionally excludes provider
credentials and any custom `--runs-root` or `--workspaces-root` locations.

Deletion requires explicit purge flags and can be combined with export:

```bash
sat-uninstall \
  --export-to "$HOME/sat-backup" \
  --purge-config \
  --purge-data \
  --yes
```

Use `sat-uninstall --help` to review all keep, purge, export, and confirmation
options. `make uninstall` runs the same guided script from the checkout.

## Development Setup

```bash
make setup
make check
```

`make check` is fully offline after setup. It verifies tool versions, Git and
ignore boundaries, configuration contracts, formatting, lint, the complete
test suite, and all offline workflow paths. It does not call a model or require
provider credentials.

Useful commands:

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

`make lock-benchmark` intentionally refreshes the benchmark dependency lock.
Set `BENCHMARK_EXCLUDE_NEWER=YYYY-MM-DD` only as part of a reviewed dependency
update, then rebuild and record a new sandbox image ID.

At run start, the controller resolves the configured image tag to its local
`sha256:...` image ID. The run-scoped Agent configuration and every quality
gate use that immutable ID; preflight fails if the configured tag changes
between resolution and workspace setup.

## Phase 1 Run

Build the exact image named by `configs/run-policy.json`:

```bash
docker build \
  --tag sat-task-manager-quality:phase1-v1 \
  benchmarks/task_manager
```

Prepare a clean, deterministic benchmark repository:

```bash
uv run sat prepare-benchmark ./task-manager-source
```

Check OpenClaw configuration and the local image without making a model call:

```bash
uv run sat preflight ./task-manager-source
```

Run preflight and the live workflow as a non-root user. Writable Agent
containers inherit that user's numeric UID/GID; root identities are rejected.

After `sat configure`, run the vertical slice using the saved model, prices,
concurrency, and either the saved global stage override or checked-in per-role
stage budgets:

```bash
sat run \
  benchmarks/task_manager/task-brief.json \
  ./task-manager-source
```

For a controlled one-off override, supply the exact model and prices on the
command line:

```bash
uv run sat run \
  benchmarks/task_manager/task-brief.json \
  ./task-manager-source \
  --model provider/model \
  --input-cost-per-million-usd 0.00 \
  --output-cost-per-million-usd 0.00
```

The default runs Tester and Reviewer concurrently. If the selected provider
can serve only one generation at a time, add
`--verification-concurrency 1`. This changes scheduling, not role inputs:
both roles still inspect the same immutable commit and neither receives the
other's interpretation.

Use real provider prices for paid models. Zero is appropriate only for a model
that is genuinely free to run. `sat run` returns `0` for a completed run, `2`
for an auditable failed run, and `1` for invalid CLI input or setup errors.

The frozen benchmark content may not change. For repeated trials, only
`TaskBrief.run_id` may differ; use a separate source checkout at the same base
commit or a separate runs/workspaces root for each trial.

## Validation CLI

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

## Persisted Run Evidence

Local generated state is ignored by Git:

```text
runs/<run_id>/
├── task-brief.json
├── run.json
├── openclaw.runtime.json
├── runtime-preflight.json
├── implementation-plan.json
├── iterations/
│   └── <nn>/
│       ├── work-result.json
│       ├── test-report.json
│       ├── review-report.json
│       ├── iteration-record.json
│       ├── commands/
│       │   ├── check_<name>.stdout.txt
│       │   └── check_<name>.stderr.txt
│       ├── executions/<stage>/
│       │   ├── <role>-attempt-<nn>.json
│       │   ├── <role>-attempt-<nn>.stdout.txt
│       │   └── <role>-attempt-<nn>.stderr.txt
│       └── handoffs/<stage>/
│           └── <sequence>-<source>-to-<target>.json
├── final-report.json
└── final-report.md

workspaces/<run_id>/
└── detached self-contained Git clone and generated result
```

Phase artifacts and captured process output are write-once. `run.json` is
atomically replaced under an optimistic revision check and records the
evidence references required for every material transition. A loader verifies
the frozen TaskBrief and selected team definition before returning state.

The controller supports explicit recovery of an isolated clone created
immediately before a crash. The current `sat run` command intentionally starts
only a fresh run and does not infer that an unrecorded external action
succeeded.

## Safety and Experiment Boundaries

- The source checkout must be clean, safe to materialize, and define local Git
  `user.name` and `user.email` values for the isolated clone.
- Run workspaces are self-contained clones with no remote and a detached HEAD,
  so the Agent can commit inside its container without access to source Git
  metadata. The harness does not merge, push, or deploy.
- Submodules, executable hooks, external Git filters, and unsafe fsmonitor
  configuration are rejected before checkout.
- Agent sandboxes and quality gates use no external network by default.
- Controlled roles receive no ambient OpenClaw skills; their explicit prompt,
  tool policy, and run-scoped repository are the complete execution boundary.
- Read-only roles deny mutation and process tools.
- Every role denies Agent-spawning tools; only the controller may authorize and
  account for a model invocation.
- Runtime configuration is run-scoped, secret-free, mode `0600`, and ignored.
- Agent containers receive an explicit non-secret environment instead of the
  host process environment or provider credentials.
- Model identity is frozen for a run, runtime fallback is disabled, and missing
  or different model telemetry is rejected.
- Agent invocation count, iterations, per-role stage time, command time, CPU,
  memory, processes, open files, tmpfs, and captured output bytes are hard
  limited. An initial response and its optional semantic repair share one
  monotonic stage deadline.
- Reported aggregate input/output tokens, Agent duration, and estimated cost
  are checked after every invocation; crossing a threshold fails the run and
  prevents another invocation. An absolute monetary cap must also be enforced
  at the provider account because usage is not known before a call completes.
- Host quality-gate execution exists only as a doubly opted-in test backend;
  Docker is the sole production backend.
- The operator must place run state on a disposable or quota-controlled
  filesystem because Docker bind mounts do not provide a portable workspace
  disk quota.
- Human authorization remains required for merge, push, deployment,
  publication, destructive operations, external communication, or additional
  spending.

## Team Configurations

[`configs/teams.json`](configs/teams.json) defines three comparable topologies:

| Configuration | Purpose | Implementation status |
| --- | --- | --- |
| `single_agent` | One-pass baseline | Phase 2 |
| `function_specialized` | Planner, generalist implementation, independent testing/review | Phase 1 implemented |
| `implementation_domain_specialized` | Parallel frontend/backend work plus integration | Phase 2 |

The configuration file owns membership and initial stage ordering. The Python
controller owns dynamic revision and termination decisions.

## Repository Layout

```text
benchmarks/task_manager/       Frozen brief, seed, acceptance suite, Dockerfile
configs/teams.json             Team topology source of truth
configs/run-policy.json        Sandbox, aggregate, and per-role stage budgets
configs/openclaw.example.json5 Sanitized role and tool policy template
src/software_agent_team/
  artifacts.py                 Persisted schemas
  artifact_store.py            Write-once artifact and output persistence
  budgets.py                   Agent and pricing budgets
  execution.py                 OpenClaw and offline execution adapters
  git_workspace.py             Standalone clones and snapshot verification
  prompting.py                 Minimum-context role prompts
  quality_gates.py             Fixed sandboxed command runner
  responses.py                 Strict Agent response parser
  run_control.py               Lifecycle state and atomic persistence
  runtime_configuration.py     Run-scoped OpenClaw config and preflight
  user_configuration.py        User-local secret-free live-run defaults
  workflow.py                  Phase 1 orchestration and final reporting
scripts/install.sh             One-command Linux/WSL installation
scripts/uninstall.sh           Guided preservation, export, and uninstall
tests/                         Offline unit, integration, and end-to-end tests
docs/phase1-runbook.md         Live-trace operating procedure
VISION.md                      Product and architecture decisions
```

## Contribution Workflow

1. Read `VISION.md` before changing architecture, scope, or experiments.
2. Keep every experimental variable and budget explicit.
3. Add or update tests with behavior changes.
4. Update public documentation in the same change.
5. Run `make check`.
6. Review the staged diff and commit with a Conventional Commit message.

Negative and inconclusive outcomes are acceptable when their evidence and
limits are reported honestly.

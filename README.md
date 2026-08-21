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
- Role-specific minimum-context prompts and strict JSON response parsing;
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
- One controlled response repair and at most one implementation revision;
- A pre-call Agent invocation cap and post-call token, duration, and
  estimated-cost stop thresholds;
- Explicit completed and failed terminal states with JSON and Markdown reports;
- Offline success, revision, timeout, evidence-tampering, non-convergence,
  no-change, invalid-response, and budget-failure tests.

Phase 1 has **not yet met its formal exit criterion**. Authorized exploratory
live traces have exercised Planner, Developer, deterministic gates, Tester, and
Reviewer, produced controller-verified commits, and completed an evidence-driven
revision. The latest trace passed compilation, lint, and 18 generated tests in
both iterations, then exposed that the version-one acceptance suite constrained
HTTP status and presentation details not present in the confirmed TaskBrief.
The benchmark is now versioned as `task_manager_phase1_v2`; its black-box checks
match the observable product contract, and its confirmed brief exposes the
fixed form, field, enum, and canonical-URL requirements. Earlier traces remain
exploratory evidence rather than comparable version-two results. A new trace
must still reach `completed` and satisfy the evidence checklist. See
[`docs/phase1-runbook.md`](docs/phase1-runbook.md).

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

The controller accepts an iteration only when all of the following agree:

1. The Developer's `WorkResult` matches a clean descendant Git commit and its
   exact changed-file set.
2. The Tester reproduces the controller-recorded command evidence and its
   command-to-criterion coverage exactly.
3. Every deterministic criterion passes; criteria assigned to independent
   review remain explicitly `pending_review` in the Tester's report.
4. The Reviewer confirms the exact manual-review scope on the same immutable
   commit and returns `accept` with no blocking finding.
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

The response boundary accepts one unambiguous JSON object, either raw, inside
one `json` code fence, or surrounded by presentation-only prose. Surrounding
text is discarded only when it contains no other JSON structure or fence;
duplicate keys, multiple objects, multiple fences, non-standard constants, and
schema violations remain invalid and consume the one bounded repair attempt.
That repair receives a bounded, value-free structural diagnostic, such as the
duplicate key name, while the immutable execution record retains the raw
provider output.

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

Run the vertical slice after choosing one fixed model and recording its current
per-million-token prices:

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
- Read-only roles deny mutation and process tools.
- Every role denies Agent-spawning tools; only the controller may authorize and
  account for a model invocation.
- Runtime configuration is run-scoped, secret-free, mode `0600`, and ignored.
- Agent containers receive an explicit non-secret environment instead of the
  host process environment or provider credentials.
- Model identity is frozen for a run, runtime fallback is disabled, and missing
  or different model telemetry is rejected.
- Agent invocation count, iterations, per-process time, command time, CPU,
  memory, processes, open files, tmpfs, and captured output bytes are hard
  limited.
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
configs/run-policy.json        Sandbox and aggregate Agent budgets
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
  workflow.py                  Phase 1 orchestration and final reporting
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

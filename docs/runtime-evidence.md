# Runtime, Evidence, and Safety Reference

This engineering reference describes how the implemented harness realizes the
decisions in [`VISION.md`](../VISION.md). `VISION.md` remains authoritative for
product and architecture decisions; the checked-in schemas, policies, and
controller code remain authoritative for executable contracts. This reference
owns the engineering explanation of runtime authority, response processing,
persisted evidence, integrity checks, and operator safety boundaries.

## Runtime Authority

The deterministic controller is the only workflow authority. It owns phase
ordering, iteration and resource budgets, artifact validation, Git evidence,
quality-gate evidence, decisions, and terminal reports. No Agent may advance
the lifecycle or declare its own work accepted.

OpenClaw owns model/provider integration, role sessions, tool exposure, and the
Agent sandbox. Agents own semantic work: planning, coding, evidence analysis,
and review. Persisted artifacts, rather than hidden chat history, are the
authoritative handoff boundary.

The controller assembles every persisted phase artifact from two distinct
sources:

1. The Agent's validated semantic response body;
2. Controller-owned facts derived from the frozen run state and verified
   execution evidence.

Artifact identity and envelope fields, run/team/role context, Git snapshots
and changed files, fixed commands and their results, acceptance coverage, and
manual-review scope never depend on a model echoing known values.

The controller accepts an iteration only when all of the following agree:

1. The Developer returns a semantic work summary, then the controller verifies
   a clean descendant Git commit and binds its exact changed-file set into the
   `WorkResult`.
2. The Tester analyzes supplied evidence, while the controller binds the
   actual commands, exit-derived status, command-to-criterion coverage, and
   blocker state into the `TestReport`.
3. Every deterministic criterion passes. Criteria assigned to independent
   review remain explicitly `pending_review` in the Tester's criterion results,
   while the overall Tester status is `passed` when no deterministic failure
   or blocker exists.
4. The Reviewer evaluates the controller-supplied manual-review scope on the
   same immutable commit and returns `accept` with no blocking finding. The
   controller binds that commit and scope into the `ReviewReport`.
5. The controller, not either Agent, resolves pending criteria to `passed` in
   the final report.

Reviewer severity and controller termination are separate concepts. Any
correctable implementation defect, including a failed acceptance gate or a
critical-impact product bug, produces `revise` while the iteration budget
allows it. Reviewer `fail` requires an explicit terminal reason proving that a
run safety or evidence-integrity boundary makes another Developer revision
unsafe.

Iteration limits belong to the launch policy, not the model. The frozen Phase 1
evaluation uses two implementation iterations for comparability. The normal
product flow may use the function-specialized team's explicit limit of three so
it can address a distinct blocker revealed only after the first revision. The
controller still stops on acceptance, repeated blockers without measurable
progress, no relevant Git change, safety or evidence failure, resource limits,
or iteration exhaustion.

## Artifact Boundary

The artifact layer is the reproducible interface between Agents and the
controller. The current implementation defines:

- `TaskBrief`;
- `HandoffEnvelope`;
- `ArtifactReference`;
- `AgentExecutionRecord`;
- `TeamPlan`, `AgentSpec`, and `ModelRoutePlan` run authority;
- Versioned Agent roles and fixed evaluation team definitions;
- `ImplementationPlan`;
- `WorkResult`;
- `TestReport`;
- `ReviewReport`;
- `IterationRecord`;
- `FinalReport`.

`src/software_agent_team/teams.py` owns run-scoped team authority and fixed
fixture compilation. `src/software_agent_team/artifacts.py` owns phase and
handoff artifacts. `src/software_agent_team/responses.py` owns the smaller
role-response bodies and the explicit mapping of controller-owned fields for
each artifact kind. These are different boundaries, not duplicate persisted
schemas.

Phase artifacts use canonical run-relative paths, write-once persistence, and
SHA-256 references. Structural schema validation is followed by contextual
validation against the frozen task brief and selected team before persistence.
The schemas exist independently of live Agent execution; an artifact is not
evidence of a real run until the controller assembles it from a validated
semantic response and verified controller inputs.

Agents do not author the persisted envelope. Artifact kind and schema version,
run/team/role/iteration context, timestamps, Git commits and changed files,
fixed commands and their exit-derived results, criterion coverage, blockers,
and review scope come from controller state. A model may redundantly return
these fields for compatibility, but the parser strips them, records which ones
were ignored, and never lets them override authoritative values. Missing or
incorrect controller-owned fields are therefore neither model-quality failures
nor reasons to spend a repair call.

## Semantic Response Boundary

Transport normalization is deterministic. The controller accepts one
unambiguous semantic JSON object in any of these forms:

- Raw JSON;
- One `json` code fence;
- JSON surrounded by presentation-only prose;
- One complete object followed by at most four redundant unmatched closing
  delimiters.

Text outside a single explicit JSON fence is discarded only when it contains no
other decodable JSON object or array and no other fence. Ordinary documentation
notation there, such as `[project.scripts]`, Python-style argv examples, or
braces around non-JSON text, does not create a competing JSON candidate. The
stricter raw-object form continues to reject any structural delimiters in its
surrounding prose. A closing-delimiter suffix is discarded only after the
decoder has already recovered exactly one complete top-level object; raw
transport output remains immutable evidence. The parser never guesses between
multiple candidates. Duplicate keys, multiple objects, multiple fences,
non-standard constants, unknown semantic fields, and invalid semantic content
remain invalid.

One controlled repair may address only the semantic contract. It receives a
bounded, value-free structural diagnostic, such as the duplicate key name,
while the immutable execution record retains the raw provider output. The
initial response and optional repair each receive the frozen per-invocation
timeout. Both calls remain inside aggregate call, duration, token, and cost
budgets.

If a model returns controller-owned fields, they are ignored and recorded in
the execution record. Missing or incorrect echoes such as `kind`, commit
hashes, test status, command lists, criterion identifiers, or review scope do
not trigger repair.

## Persisted Run Evidence

Local generated state is ignored by Git. Product runs use
`${XDG_STATE_HOME:-$HOME/.local/state}/software-agent-team/` as the parent;
controlled evaluations may select explicit roots. Beneath the selected roots,
the evidence follows this layout:

```text
runs/<run_id>/
├── task-brief.json
├── team-plan.json
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

`runtime-preflight.json` records the private OpenClaw and Docker identities,
configuration validity, the exact selected model and local availability result,
image presence and immutable ID, restricted-container tool execution and
liveness, and any non-secret model or container probe error. A run is ready
only when the configuration, exact model route, image identity, and container
execution checks all pass.

Phase artifacts and captured process output are write-once. `team-plan.json`
freezes Agent identities, responsibilities, dependency waves, workspace and
permission boundaries, invocation timeouts, model authorization, concurrency,
iteration policy, and aggregate budget. Its digest is immutable run metadata,
and the plan itself binds the exact confirmed `TaskBrief` digest. `run.json` is
atomically replaced under an optimistic revision check and records the
evidence references required for every material transition. A loader verifies
the frozen TaskBrief, TeamPlan, fixed-fixture provenance when applicable, and
all cross-file digests before returning state.

The controller supports explicit recovery of an isolated clone created
immediately before a crash. The current `sat run` command intentionally starts
only a fresh run and does not infer that an unrecorded external action
succeeded.

## Failure as Evidence

A failed run is a valid, auditable result. Provider failures, invalid semantic
responses, timeouts, missing runtime telemetry, missing dependencies, budget
exhaustion, unsafe Git state, evidence-integrity failures, and iteration-limit
exhaustion remain visible in `run.json` and the final JSON and Markdown
reports.

The harness does not silently retry away terminal evidence, reinterpret an
unrecorded action as successful, or continue after its deterministic controller
has recorded a terminal state. Preserve a failed run directory and workspace
when investigating it rather than editing artifacts in place.

## Git and Workspace Boundary

- The source checkout must be clean, safe to materialize, and define local Git
  `user.name` and `user.email` values for the isolated clone.
- Every run workspace is a self-contained clone with no remote and a detached
  HEAD. The Agent can commit inside its container without access to source Git
  metadata.
- The controller verifies that each implementation snapshot is clean,
  descendant from the expected base, and limited to its exact changed-file
  set.
- Submodules, executable hooks, external Git filters, and unsafe fsmonitor
  configuration are rejected before checkout.
- The harness does not merge, push, deploy, or publish generated results.

## Sandbox and Permission Boundary

- Generated code has no external network access by default.
- Agent sandboxes and production quality gates run through Docker with external
  network access disabled.
- OpenClaw role tools execute into long-lived scope-owned containers and
  explicitly supplies their supervisor command. The pinned runtime image uses
  the same standalone default, while installation plus run preflight prove an
  actual helper can execute and the container remains alive under restricted
  settings instead of treating image presence as readiness.
- OpenClaw's long-lived lifetime is an internal runtime primitive, not SAT's
  product lifetime. On every completed, failed, interrupted, or exceptional
  workflow exit, SAT queries Docker only for exact controller-generated session
  labels for that run, then removes an entry only when a bind mount is also
  beneath that run's SAT-owned state or workspace. A matching label outside
  those paths is refused, so an existing OpenClaw remains outside both the
  inspection and cleanup boundary.
- A cgroup PID limit bounds each container. SAT intentionally omits
  `RLIMIT_NPROC`, whose numeric-UID-wide accounting can include processes
  outside the container and reject PID 1 on some Docker hosts.
- If trusted OpenClaw tool-runtime stderr later reports that Docker became
  unavailable or the scope container stopped, the controller records a
  dependency failure. Agent-authored prose cannot assign that classification.
- Agent and quality containers drop Linux capabilities, use read-only root
  filesystems, and receive only the assigned workspace and frozen inputs.
- Live runs require an unprivileged invoking account. Writable Agent
  containers use that account's numeric UID/GID; root identities are rejected.
- Controlled roles receive no ambient OpenClaw skills. Their explicit prompt,
  tool policy, and run-scoped repository are the complete execution boundary.
- Clarifier, Planner, Tester, and Reviewer are read-only roles. Read-only roles
  deny mutation and process tools and inspect verified source through the
  read-only `/agent` mount.
- Coding and Integration roles may write only inside the assigned `/workspace`
  mount.
- Every role denies Agent-spawning and one-shot model tools. Only the
  controller may authorize, schedule, attribute, and account for a model
  invocation.
- Host quality-gate execution exists only as a doubly opted-in test backend;
  Docker is the sole production backend.

## Configuration, Credentials, and Model Boundary

- SAT installs and invokes only its marked private OpenClaw binary. Every
  invocation receives explicit SAT-owned config, credential, state, workspace,
  and Agent paths; ambient `OPENCLAW_*` settings and the legacy Agent-directory
  selector are neutralized. Ordinary provider API-key variables may still be
  inherited from the trusted caller environment.
- An OpenClaw binary, Gateway, process, config, profile, credential store,
  session, cache, or workspace outside those marked paths is never probed,
  reused, reconfigured, stopped, upgraded, downgraded, or uninstalled by SAT.
- Runtime configuration is run-scoped, secret-free, mode `0600`, and ignored
  by Git.
- A selected model must appear exactly once as available in OpenClaw's
  configured local model view before Agent execution. This inspection does not
  generate content. `runtime-preflight.json` persists the selected model,
  availability result, and a bounded non-secret error.
- A reviewed compatibility catalog may add routing and model metadata absent
  from the pinned OpenClaw release. It cannot contain a credential or silently
  select a fallback. When a trusted caller credential variable is available,
  the generated config contains only its variable reference; SAT's isolated
  auth profiles remain the other credential source.
- Agent containers receive an explicit non-secret environment instead of the
  host process environment or provider credentials.
- SAT's isolated OpenClaw host process owns model-provider access. Credentials
  may live in its private OpenClaw-owned state or come from trusted caller
  environment variables; Agents never receive provider credentials or
  unrelated host data.
- Model identity is frozen for a run and runtime fallback is disabled. A
  successful call must report the selected canonical `provider/model` and
  integer input/output token counts; missing or different telemetry fails the
  run.
- Retrieved content and generated repository instructions are untrusted input.

## Resource and Cost Boundary

- CPU, memory, process, open-file, tmpfs, captured command-output, wall-clock,
  iteration, and Agent-invocation limits are mandatory before live runs.
- Checked-in role-specific invocation timeouts reflect measured workloads. A
  global CLI or saved timeout override is an explicit experimental variable.
- An initial semantic response and its optional one-call repair each receive
  the resolved role timeout. A repair must regenerate the complete response;
  it is not given an arbitrary remainder from the first call. The run-wide
  call-count and Agent-duration budgets still include both invocations.
- Model compatibility supplements do not set a second provider-transport
  timeout. The controller passes the frozen per-role timeout to OpenClaw for
  every invocation, and the outer subprocess boundary permits only bounded
  shutdown grace beyond it.
- Reported aggregate input/output tokens, Agent duration, and estimated cost
  are checked after every invocation. Crossing a threshold fails the run and
  prevents another invocation.
- A product run without a trustworthy configured price records estimated cost
  as unavailable rather than zero. Controlled comparisons require an explicit
  paired price table.
- Usage is not known before a provider call completes. A provider-side spending
  or quota limit is therefore the hard monetary authorization boundary; the
  controller cannot reverse the cost of the call that crosses a post-call
  threshold.
- The operator must place run state on a disposable or quota-controlled
  filesystem. Docker bind mounts do not provide a portable workspace disk
  quota.
- OpenClaw installation and state isolation does not reserve host or provider
  capacity. A concurrently running program may still share CPU, memory,
  network, Docker, or provider quota with SAT; these are resource-contention
  boundaries, not permission for SAT to control that program.

## Human Authorization Boundary

Human authorization remains required before merge, push, deployment,
publication, external communication, destructive operations, or additional
spending. A completed harness run is an auditable candidate delivery, not
authorization for an external side effect.

For the procedure that verifies these boundaries in a controlled
provider-backed evaluation, use
[`phase1-runbook.md`](phase1-runbook.md).

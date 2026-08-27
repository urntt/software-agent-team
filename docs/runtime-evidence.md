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

OpenClaw owns model/provider integration, Agent sessions, tool exposure, and
the Agent sandbox. Agents own semantic work: planning, coding, evidence
analysis, and review. Persisted artifacts, rather than hidden chat history, are
the authoritative handoff boundary.

Every execution request and telemetry record now carries a run-scoped Agent ID
and controller-known capability. A fixed evaluation role is optional
compatibility metadata and must match that identity when present. Dynamic
requests bind the exact approved AgentSpec output contract, model route,
per-invocation timeout, deterministic session key, and assigned task IDs;
mismatched response or telemetry context is rejected before it can become
evidence. Shared controller-owned assembly binds dynamic implementation, test,
and review semantics to verified facts, and the normal product launcher now
executes those requests through the general DAG scheduler.

The controller assembles every persisted phase artifact from two distinct
sources:

1. The Agent's validated semantic response body;
2. Controller-owned facts derived from the frozen run state and verified
   execution evidence.

Artifact identity and envelope fields, run/team/role context, Git snapshots
and changed files, fixed commands and their results, acceptance coverage, and
manual-review scope never depend on a model echoing known values.

The controller accepts an iteration only when all of the following agree:

1. Every approved implementation or integration Agent returns a semantic work
   summary. The controller verifies a clean descendant Git commit for each
   writer and binds the exact changed-file set into an attributable
   `WorkResult`; the iteration requires those results to form one commit chain.
2. Every approved Testing Agent analyzes the same supplied evidence, while the
   controller binds the actual commands, exit-derived status,
   command-to-criterion coverage, and blocker state into each `TestReport`. If
   a valid team intentionally has no Testing Agent, the controller persists
   that deterministic evidence under its own identity.
3. Every deterministic criterion passes. Criteria assigned to independent
   review remain explicitly `pending_review` in the Tester's criterion results,
   while the overall Tester status is `passed` when no deterministic failure
   or blocker exists.
4. Approved Review Agents evaluate their controller-supplied manual-review
   scopes on the same immutable commit. A Dynamic Reviewer must return exactly
   one `criterion_assessments` entry for every assigned criterion, including a
   concrete adversarial check, observable evidence, and `satisfied` or
   `blocked` status. Every semantic assessment also supplies at least one
   distinctive fragment from a bounded result in that exact invocation. The
   controller requires each fragment to identify exactly one sanitized tool
   result and enriches the persisted assessment with its own tool-call ID.
   Every blocked assessment must map to a blocking finding with the same
   criterion ID. The controller binds each Agent, commit, scope, and grounded
   references into an attributable
   `ReviewReport`; their combined scope must exactly cover the manual criteria
   and finding IDs must remain unique across the iteration.
5. The controller, not either Agent, resolves pending criteria to `passed` in
   the final report.

Generic deterministic gates do not prove arbitrary task semantics. For an
unqualified prohibition or safety guarantee, the Planning prompt requires
acceptance intent across all relevant input boundaries, the implementation
prompt requires focused boundary tests, and the Review prompt requires an
adversarial counterexample search. A Dynamic Reviewer may run bounded
foreground inspection or probe commands in its no-network sandbox against the
read-only source and temporary fixtures. Its assessment remains attributable
model evidence rather than a controller fact: a later concrete counterexample
still invalidates acceptance and must be preserved as a product defect. The
controller must never reinterpret a model's broad claim or self-authored test
as deterministic proof.

Reviewer severity and controller termination are separate concepts. Any
correctable implementation defect, including a failed acceptance gate or a
critical-impact product bug, produces `revise` while the iteration budget
allows it. Reviewer `fail` requires an explicit terminal reason proving that a
run safety or evidence-integrity boundary makes another Developer revision
unsafe.

Iteration limits belong to approved controller policy, not to an execution
Agent. The frozen Phase 1 evaluation uses two implementation iterations for
comparability. Adaptive Planning may propose one through three iterations; the
user sees and approves the exact limit before the controller creates the team.
The controller still stops on acceptance, repeated blockers without measurable
progress, no relevant Git change, safety or evidence failure, resource limits,
or iteration exhaustion.

## Artifact Boundary

The artifact layer is the reproducible interface between Agents and the
controller. The current implementation defines:

- `TaskBrief`;
- `PlanningRequest`, `PlanningTurn`, `PlanningProposal`, and `PlanningApproval`;
- `AdaptiveImplementationPlan`;
- `HandoffEnvelope`;
- `ArtifactReference`;
- `AgentExecutionRecord`;
- `TeamPlan`, `AgentSpec`, and `ModelRoutePlan` run authority;
- `RunEvent` append-only progress evidence;
- `ControlCommand` request and resolution history;
- Versioned Agent roles and fixed evaluation team definitions;
- `ImplementationPlan`;
- `WorkResult`;
- `TestReport`;
- `ReviewReport`;
- `IterationRecord`;
- `FinalReport`.

`src/software_agent_team/teams.py` owns run-scoped team authority and fixed
fixture compilation. `src/software_agent_team/artifacts.py` owns phase and
handoff artifacts. `src/software_agent_team/planning.py` owns pre-run Adaptive
Planning dialogue, proposal compilation, and approval evidence.
`src/software_agent_team/responses.py` owns the smaller semantic response
bodies and the explicit mapping of controller-owned fields for each artifact
kind. These are different boundaries, not duplicate persisted schemas.
`src/software_agent_team/assembly.py` is the shared binding layer that combines
those semantic bodies with controller-owned Git, command, identity, commit,
and review-scope facts for both fixed and task-defined teams.

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

Reviewer tool claims cross a separate grounding boundary. The response schema
requires each criterion assessment to supply only small bounded exact
observable result fragments. It forbids the model from supplying or predicting
a controller tool ID and does not ask it to echo a tool name, outcome, exit
code, arguments, or digest. The fixed-fixture compatibility path searches only
the current sanitized execution record. The adaptive path does the same on an
initial response; during its one controlled semantic repair, it may also search
integrity-checked attempts from the same Reviewer, role stage, immutable commit,
and invocation chain. The controller requires every fragment to occur in at
least one eligible result, binds every matching `(execution_attempt,
tool-00N)` identity, and deterministically deduplicates repeated or overlapping
selectors. Evidence never crosses an Agent, stage, iteration, commit, or repair
chain. A no-match selector uses or exhausts the one bounded semantic repair;
multiple real matches do not.
Execution records label this grounded Reviewer shape `semantic_body_v2`; other
current semantic bodies remain `semantic_body_v1`. Existing schema-v2 Review
artifacts that predate attempt qualification are interpreted as attempt one and
serialize without an invented field, preserving their canonical content and
digests. Newly grounded references record the attempt explicitly.

## Semantic Response Boundary

Transport normalization is deterministic. The controller accepts one
unambiguous semantic JSON object in any of these forms:

- Raw JSON;
- One `json` code fence;
- JSON surrounded by presentation-only prose;
- One complete object followed by at most four redundant unmatched closing
  delimiters.

Text outside a single explicit JSON fence is discarded only when it contains no
other decodable JSON object and no other fence. The response contract requires
a top-level object, so a JSON argv array such as `["uv", "run", "pytest"]`
cannot compete with that object and is presentation data. An array containing
another decodable object is still rejected because the nested object is a real
candidate. Ordinary documentation notation such as `[project.scripts]`,
Python-style argv examples, or braces around non-JSON text likewise does not
create a competing object. The raw-object form rejects object delimiters or a
fence in surrounding prose while permitting contract-ineligible arrays. A
closing-delimiter suffix is discarded only after the decoder has already
recovered exactly one complete top-level object; raw transport output remains
immutable evidence. The parser never guesses between multiple objects.
Duplicate keys, multiple objects, multiple fences, non-standard constants,
unknown semantic fields, and invalid semantic content remain invalid.

OpenClaw transport may contain more than one visible payload when a semantic
answer is followed by an ancillary tool diagnostic. SAT retains the raw JSON
envelope and payload boundaries in telemetry, concatenates visible text in
order, and applies the object rules above to the combined presentation. It
therefore accepts one semantic object plus non-object diagnostic text but still
rejects two competing objects. Payload count alone is not a semantic verdict.

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

For an isolated OpenClaw invocation, SAT reads the session index and exact
session ID returned by the pinned runtime, verifies direct non-symlink paths,
requires a complete UTF-8 JSONL transcript, finds the latest user record that
exactly matches the current prompt, and stops at the next user turn. Tool calls
and results in that segment must pair one-to-one by the external call ID and
tool name. SAT then assigns stable invocation-local IDs in execution order,
normalizes success or failure from the result, hashes the external ID,
canonical arguments, complete result, and transcript, and retains only a
bounded output excerpt. For `exec`, it also records the direct executable token
while discarding the full command and any leading environment-assignment
values. Size and record-count limits apply before parsing.

A complete session with zero tool calls is valid captured evidence, but it
cannot satisfy a citation by itself. A missing, substituted, incomplete,
malformed, or unpaired session is invalid runtime evidence and stops Review at
the safety boundary rather than spending a semantic repair. On the adaptive
path, a repair may reuse an earlier eligible attempt without repeating an
unchanged probe; every reference records the originating execution attempt.
Raw OpenClaw session JSONL is never copied into run artifacts; the sanitized
records, transcript SHA-256, and current-turn record count make the accepted
claim auditable without making raw session history a later replay dependency.

## Persisted Run Evidence

Local generated state is ignored by Git. Product runs use
`${XDG_STATE_HOME:-$HOME/.local/state}/software-agent-team/` as the parent;
controlled evaluations may select explicit roots. Beneath the selected roots,
the evidence follows this layout:

```text
planning/<run_id>/
├── request.json
├── session.json
├── turns/
│   └── <sequence>.json
├── proposals/
│   └── <revision>.json
└── approvals/
    └── <revision>.json

runs/<run_id>/
├── task-brief.json
├── team-plan.json
├── run.json
├── events/
│   └── <sequence>.json
├── controls/
│   └── <command_id>/
│       ├── 000001.json
│       └── 000002.json
├── openclaw.runtime.json
├── runtime-preflight.json
├── implementation-plan.json
├── iterations/
│   └── <nn>/
│       ├── iteration-record.json
│       ├── agents/<agent_id>/
│       │   └── <typed-agent-artifact>.json
│       ├── commands/
│       │   ├── check_<name>.stdout.txt
│       │   └── check_<name>.stderr.txt
│       ├── executions/<stage>/
│       │   ├── <agent_id>-attempt-<nn>.json
│       │   ├── <agent_id>-attempt-<nn>.stdout.txt
│       │   └── <agent_id>-attempt-<nn>.stderr.txt
│       └── handoffs/<stage>/
│           └── <sequence>-<source>-to-<target>.json
├── final-report.json
└── final-report.md

workspaces/<run_id>/
└── detached self-contained Git clone and generated result
```

Artifact schema v2 attributes handoffs, execution telemetry, and Agent-owned
artifacts to run-scoped Agent IDs. The Agent namespace prevents two Agents with
the same output kind from claiming the same immutable path. On every write and
load, the store checks producer identity, stage membership, capability, and
handoff endpoints against the approved `TeamPlan`.

The bare `sat` launcher uses the Adaptive Planning store before creating a run.
Its request proves explicit model-work authorization. Every
model invocation, including a rejected semantic response, becomes a write-once
turn containing prompt and response digests plus bounded provider evidence.
The turn preserves the exact raw response. When the controller infers an
unambiguous missing question/proposal discriminator or canonicalizes a safe
relative Planning path presentation, it stores the validated normalized body
and an explicit field-level normalization list alongside that raw evidence.
Unsafe, ambiguous, or permission-changing values remain validation failures.
Turns form a predecessor-digest chain anchored by atomic `session.json` state.
Proposal revisions are immutable and must match their source turn or a
controller-owned structured edit. Approval binds the exact proposal, confirmed
TaskBrief, adaptive implementation plan, and TeamPlan digests. It also stores
each Agent's workload class, allowed timeout envelope, resolution source, and
exact resolved seconds. Reviewer resolutions also bind the exact criterion
count and controller-derived minimum when scope raises the floor. The approval
then revalidates those seconds against the TeamPlan at the execution boundary.
The bootstrap Planner cannot create Agents or change lifecycle state.

Task-criterion binding is context-aware without transferring profile ownership
to the model. A proposal must cover every criterion it defines. It may also
bind a task to a profile criterion ID explicitly supplied by the current
controller policy, while the controller remains the sole source of that
criterion's description and verification contract. Before persisting a valid
proposal, the controller rejects task references outside the union of those two
sets, materializes profile criteria into the confirmed TaskBrief, and preserves
the task bindings in the adaptive implementation plan. Prompt construction
rechecks every persisted task reference against the exact TaskBrief bound by
the approved TeamPlan.

When an adaptive run enters implementation, its lifecycle transition binds the
approved adaptive implementation-plan digest directly. It does not manufacture
a fixed-role `ImplementationPlan` artifact. Fixed compatibility runs continue
to reference their actual Planner artifact, and recovery rejects a transition
whose evidence form or digest differs from the frozen TeamPlan origin.

`runtime-preflight.json` records the private OpenClaw and Docker identities,
configuration validity, the bootstrap model and every TeamPlan-authorized
model's local availability result, image presence and immutable ID,
restricted-container tool execution and liveness, and any non-secret model or
container probe error. A run is ready only when the configuration, every exact
model route, image identity, and container execution checks all pass.

Phase artifacts and captured process output are write-once. `team-plan.json`
freezes Agent identities, responsibilities, dependency waves, workspace and
permission boundaries, invocation timeouts, model authorization, concurrency,
iteration policy, and aggregate budget. Its digest is immutable run metadata,
and the plan itself binds the exact confirmed `TaskBrief` digest. `run.json` is
atomically replaced under an optimistic revision check and records the
evidence references required for every material transition. A loader verifies
the frozen TaskBrief, TeamPlan, fixed-fixture provenance when applicable, and
all cross-file digests before returning state.

Every compatibility-workflow status update is first enriched into a versioned
`RunEvent`. It stores its run ID, contiguous sequence, UTC timestamp, lifecycle
revision, category, minimum visibility, phase, and attributable Agent attempt
when applicable. Dynamic events additionally record queue/readiness/provider
wait/repair/terminal state, safe activity, dependencies, capability, stage,
approved model, route-switch references, duration, invocation reference, and
aggregate budget snapshot.
Heartbeat lifecycle follows controller state even when the ending event is
hidden by the selected visibility: an invocation-completed checkpoint stops
provider waiting, and a terminal Agent event closes every semantic-repair
attempt for that Agent. Quality-gate events encode passed versus failed
completion so terminal renderers do not use a success mark merely because a
command finished. Pre-execution Planning uses the same user-safe principle for
ephemeral elapsed heartbeats, response receipt, validation, and bounded repair;
the immutable Planning turn remains the authoritative evidence.
Events are written as an append-only predecessor-digest chain. After each
append, `run.json` atomically anchors the exact event count and latest digest;
if anchoring fails, the unowned event file is removed before presentation code
sees it. Recovery therefore detects missing, reordered, modified, or extra
events, including tampering with the latest event. Compact, standard, and
detailed renderers consume this same persisted contract. Renderer failure is
isolated from controller execution and cannot erase the event.

`ControlCommand` defines `guide`, `correct`, `pause`, `resume`, `interrupt`,
and `cancel` requests, typed targets, safe application boundaries, and the
`queued`, `applied`, `rejected`, `superseded`, and `best_effort_failed` result
states. The controller-owned store writes the request and terminal resolution
as an immutable two-revision digest chain with optimistic concurrency. A
controller-assigned request sequence preserves mailbox order even when clocks
collide. The foreground product CLI now submits these commands through a
line-mode palette. `RuntimeControlChannel` applies them only at controller
checkpoints: guidance is consumed once by a future Agent invocation; pause and
correction stop new launches and drain active work; resume withdraws or resumes
a cooperative pause; interrupt and cancel request best-effort termination only
for SAT-owned OpenClaw process groups. Received and resolved commands are
correlated from the append-only event stream to the exact command revision and
digest. Provider cost already incurred before termination is never presented
as reversible.

Correction and cancellation retain the run's existing artifacts and produce a
machine-readable `cancelled` final report rather than converting partial work
into delivery. A correction then creates a fresh Planning request and run ID,
preserves the original request and destination, adds the explicit superseding
requirement, and requires approval of the replacement overview. Foreground
process-crash resume and a secondary-process control client remain deferred;
recovery therefore never invents the application of a command that lacks a
persisted terminal revision.

The dynamic runtime keeps scheduling and invocation as separate controller
responsibilities. `DagScheduler` decides readiness, actual launch order,
bounded concurrency, and shared-workspace exclusion from the approved
`TeamPlan`. `DynamicAgentRunner` may execute only the ready `AgentSpec` passed
to it. It binds that invocation to the approved capability, model route,
per-Agent timeout, permission profile, assigned tasks, and dependency
handoffs; it cannot create Agents, edit the DAG, or extend a timeout.

Each invocation reserves aggregate call capacity atomically before launch and
records reported tokens, duration, and known price after completion. Raw
stdout/stderr, telemetry, the semantic response reference when valid, and any
post-call budget rejection are persisted together. When session collection is
active, the same `AgentExecutionRecord` stores its collection status,
transcript digest, current-turn record count, ordered sanitized tool records,
and any bounded integrity error. Missing model, provider, token, or required
Reviewer tool evidence is never treated as zero usage or success. A repair is
permitted only for a semantic-body failure, receives the same approved
invocation timeout, and remains bounded by the aggregate call and resource
budgets.

Agent-authored summaries are complete immutable artifact evidence and are not
forced to guess a hidden downstream display limit. When a summary exceeds the
bounded scheduler or Agent-prompt field, the controller derives a deterministic
projection. The projection retains a prefix, states the source character
length, includes the SHA-256 of the complete cleaned summary, and points back
to immutable artifact evidence. Scheduler state is bounded to 2,000 characters
and downstream prompt context to 1,000; the source WorkResult, TestReport, or
ReviewReport remains unchanged. Projection therefore cannot turn successful
verified work into an artifact failure or spend a model repair call.

For policy routing, `team-plan.json` freezes one primary route and an ordered,
bounded fallback list for every Agent, plus the controller's attributable
selection source and reason. Prompts and response validation bind the active
route. Only an execution classified as an attributable provider failure may
advance to the next explicitly approved fallback, and only when the TeamPlan
authorizes `provider_failure`. The failed invocation, usage, route reference,
switch event, next route, and possible billable consequence remain evidence.
Semantic response repair does not itself authorize a route switch.

Writers are serialized for the current single-clone Git backend. The
controller verifies their clean input commit, descendant output commit,
changed paths, and workspace scope. Read-only quality Agents may run in
parallel but must leave the exact final commit and clean tree unchanged. Every
quality Agent is downstream of every writer, deterministic gates execute once
per immutable iteration, and all TestReports and ReviewReports bind to that
same commit. Completed dependency and terminal handoffs retain both phase and
execution references. `DynamicWorkflowCoordinator` records the aggregate
snapshot before starting quality work, resolves each iteration, binds blocking
evidence to a bounded revision, and writes terminal evidence. Bare `sat` passes
the exact approved Planning result through this path and then uses the existing
accepted-result delivery boundary.

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
- A generated Python project must ignore its root setup environment and must
  either contain a bounded regular `uv.lock` in the accepted clean snapshot or
  explicitly ignore that local lock artifact. This contract prevents the exact
  documented setup command from silently dirtying first-use Git state; it does
  not claim that an ignored lock provides dependency reproducibility.
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
- OpenClaw Agent tools execute into long-lived scope-owned containers and
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
- Controlled Agents receive no ambient OpenClaw skills. Their explicit prompt,
  tool policy, and run-scoped repository are the complete execution boundary.
- Clarification, Planning, Testing, and Review capabilities use read-only
  permission profiles and inspect verified source through the read-only
  `/agent` mount. They deny direct mutation, background-process, and
  Agent-spawning tools. Review may run bounded foreground inspection or
  adversarial probe commands against `/agent` and fixtures under the sandbox's
  writable `/tmp`; the general `write`, `edit`, and `apply_patch` tools stay
  denied. The immutable `sat-probe-write` executable accepts only a new bounded
  canonical direct child matching `/tmp/sat-review-probe-*`, creates it
  atomically with mode `0600`, and rejects overwrite, symlinks, nesting, and
  traversal. Simple direct probe invocations pass command preflight, and the
  image includes pinned `uv` for relevant bounded probes. The source mount
  remains read-only, and resulting
  criterion-by-criterion evidence is attributable rather than controller-owned
  deterministic command evidence.
- Product quality verification mounts clean executable tmpfs scratch because
  an offline `uv sync` creates project-local virtual-environment entry points
  that must launch there. It first rejects tracked drift and unsafe entries,
  copies only committed regular files, and executes exact setup, test, and
  start argv. Network remains disabled; source and root filesystems remain
  read-only; the process remains non-root, capability-dropped, and bounded by
  PID, file, memory, CPU, output, and timeout limits. Scratch is discarded with
  the gate container.
- Implementation and Integration capabilities may write only inside the
  assigned `/workspace` mount.
- Every Agent denies Agent-spawning and one-shot model tools. Only the
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
- The bootstrap model and every TeamPlan-authorized primary or fallback model
  must appear exactly once as available in OpenClaw's configured local model
  view before Agent execution. This inspection does not generate content.
  `runtime-preflight.json` persists every inspected model, availability result,
  and bounded non-secret error.
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
- Every Agent's authorized model set and order are frozen for a run. Strict
  evaluation disables fallback. A policy run may advance only through its
  explicit `provider_failure` fallback list and records the change; any other
  model is rejected. A successful call must report the active canonical
  `provider/model` and integer input/output token counts; missing or different
  telemetry fails the run.
- Retrieved content and generated repository instructions are untrusted input.

## Resource and Cost Boundary

- CPU, memory, process, open-file, tmpfs, captured command-output, wall-clock,
  iteration, and Agent-invocation limits are mandatory before live runs.
- Checked-in capability defaults and fixed-role compatibility timeouts reflect
  measured workloads. Adaptive Planning supplies only a routine, substantial,
  or complex workload class. Product policy deterministically resolves that
  class inside a capability-specific default-to-ceiling envelope; a direct user
  timeout override must remain inside the effective envelope. Reviewer scope
  independently maps fewer than 6, 6–10, or 11+ criteria to a routine,
  substantial, or complex minimum; the controller uses the higher of that
  floor and Planner workload. The resulting
  Adaptive TeamPlan freezes the exact timeout for each run-scoped Agent. A
  global CLI or saved timeout override collapses every Adaptive envelope to one
  explicit value but cannot undercut the scope floor; it remains an
  experimental variable.
- Without a global override, the product envelope uses the checked-in
  capability timeout as its default and twice that value, capped at 3,600
  seconds, as its ceiling. Routine selects the default, complex selects the
  ceiling, and substantial selects their integer midpoint.
- An initial semantic response and its optional one-call repair each receive
  the resolved Agent timeout. A repair must regenerate the complete response;
  it is not given an arbitrary remainder from the first call. The run-wide
  call-count and Agent-duration budgets still include both invocations.
- Model compatibility supplements do not set a second provider-transport
  timeout. The controller passes the approved per-Agent timeout, or its exact
  fixed-role compatibility value, to OpenClaw for every invocation, and the
  outer subprocess boundary permits only bounded shutdown grace beyond it.
- Reported aggregate input/output tokens, Agent duration, and estimated cost
  are checked after every invocation. Crossing a threshold fails the run and
  prevents another invocation.
- One thread-safe controller ledger atomically reserves the call count before
  launch, so concurrently ready Agents cannot oversubscribe the budget. It
  records completed-call telemetry before raising a post-call token, duration,
  or known-cost rejection; missing token telemetry and unavailable pricing are
  counted explicitly rather than converted to zero.
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

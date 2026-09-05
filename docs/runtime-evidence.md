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
time authority, deterministic session key, and assigned task IDs;
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
   bounded fragment from an eligible Reviewer result or deterministic command
   output. The controller prefers exact text and permits only RFC JSON
   whitespace differences outside quoted strings for keyed JSON fragments. It
   enriches the persisted assessment with every protocol-eligible actual
   tool-call or command ID. For a satisfied direct probe, only framed child
   stdout and the terminal result are positive evidence; child stderr remains
   available for blocked counterexamples. Blocked assessments and blocking
   findings must cover the same
   criteria. A sole unscoped blocker is bound to otherwise-uncovered blocked
   criteria; multiple unscoped blockers remain invalid. The controller binds
   each Agent, commit, scope, and grounded
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

A blocking Review finding remains unresolved until a later independent Review
accepts the changed commit. If execution stops after a writer produced a new
commit but before that re-verification completes, `FinalReport` identifies the
iteration and commit where the finding was proved, identifies the newer commit,
and labels the state pending re-verification. It does not rewrite the old
finding as resolved or present its old observation as a fresh claim about the
new commit.

Reviewer evidence claims cross a separate grounding boundary. The response
schema requires each criterion assessment to supply only small bounded
observable result fragments. It forbids the model from supplying or predicting
a controller tool or command ID and does not ask it to echo a tool name,
outcome, exit code, arguments, or digest. Exact text is preferred. A fallback
comparison removes only RFC JSON whitespace outside quoted strings, and only
for a keyed JSON fragment; values, punctuation, ordering, and string content
remain exact. The fixed-fixture compatibility path searches the current
sanitized execution record. The adaptive path also searches controller-owned
deterministic command stdout/stderr from the same immutable iteration; during
its one controlled semantic repair, it may additionally search
integrity-checked attempts from the same Reviewer, role stage, immutable commit,
and invocation chain. The controller requires every fragment to occur in at
least one eligible output and binds every protocol-eligible
`(execution_attempt, tool-00N)` identity and/or `CHECK_*` command ID. A
satisfied direct probe uses only its completely framed child stdout and terminal
result; unframed or partial output and traceback source text in child stderr
cannot establish success. When a failed direct probe and a later successful
direct probe emit the same fragment, the successful emission is bound while the
failed attempt remains in the execution record. Repeated or overlapping
selectors are deterministically deduplicated. A report that is already
`revise`, contains an independently blocked assessment and blocking finding,
and has one additional positive assessment backed only by a failed or
protocol-ineligible result may be recovered without another model call. The
controller changes only that assessment to `blocked`, binds the same evidence
under blocked semantics, and adds a criterion-scoped evidence-gap finding. It
never applies this monotonic downgrade to `accept`, `fail`, a no-match selector,
or an otherwise-invalid blocker relationship. Evidence never crosses an
Agent, stage, iteration, commit, or repair chain. A no-match selector uses or
exhausts the one bounded semantic repair; multiple real matches do not.

Execution records label this grounded Reviewer shape `semantic_body_v4`; other
current semantic bodies remain `semantic_body_v1`. `semantic_body_v2` and
`semantic_body_v3` remain valid historical evidence for the attempt-qualified
tool-only and deterministic-command grounding contracts, respectively.
Existing schema-v2 Review artifacts that predate attempt qualification or
command references serialize without invented fields, preserving their
canonical content and digests. Newly grounded references record actual
attempt-qualified tool IDs and command IDs explicitly. Version four also
persists controller-approved entry-boundary checks and their distinct grounded
evidence.

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
while the immutable execution record retains the raw provider output. In
controlled evaluations, the initial response and optional repair each receive
the frozen per-invocation timeout. Product calls instead share the
user-authorized USD ceiling and optional whole-run deadline; their call,
duration, and token counts remain telemetry.

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
values. Executable attribution lazily consumes only leading assignments and the
first executable token; it does not require an unpersisted shell suffix to
satisfy a second parser's complete-command grammar. The complete canonical
argument object is still hashed and the actual result remains authoritative. An
unavailable, empty, NUL-containing, overlong, or unparseable executable prefix
is invalid. Size and record-count limits apply before parsing.

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

self-checks/<run_id>/
├── 0001-task_admission.json
└── 0002-plan_execution.json

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
unambiguous missing question/proposal discriminator, removes an exact active
profile criterion definition that is already controller-owned, or canonicalizes
a safe relative Planning path presentation, it stores the validated normalized
body and an explicit normalization list alongside that raw evidence. Removing
an echoed profile definition never adopts its model-authored text or removes a
legal task binding to the canonical profile ID.
Quality-owned tasks are retained as approved semantic intent and passed to the
matching read-only Agent. They do not alter the AgentSpec-owned permission,
dependency, model, time authority, scope, or invocation contract.
Unsafe, ambiguous, or permission-changing values remain validation failures.
Turns form a predecessor-digest chain anchored by atomic `session.json` state.
Proposal revisions are immutable and must match their source turn or a
controller-owned structured edit. Approval binds the exact proposal, confirmed
TaskBrief, adaptive implementation plan, and TeamPlan digests. It also stores
each Agent's workload class and time-authority resolution. Product approval
records provider activity with no per-Agent wall-clock limit; a controlled
evaluation may instead record an allowed timeout envelope and exact resolved
seconds. Approval revalidates that authority against the TeamPlan at the
execution boundary.
The bootstrap Planner cannot create Agents or change lifecycle state.

Task-criterion binding is context-aware without transferring profile ownership
to the model. A proposal must cover every Planner-owned criterion it defines.
An exact definition echo of an active profile ID is discarded and audited
before that coverage check because the model definition has no authority. A
task may bind to a profile criterion ID explicitly supplied by the current
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
container probe error. It also records the ordinary preflight-command timeout
and the separate model-inspection timeout used for that evidence. A run is
ready only when the configuration, every exact model route, image identity,
and container execution checks all pass.

Phase artifacts and captured process output are write-once. `team-plan.json`
freezes Agent identities, responsibilities, dependency waves, workspace and
permission boundaries, time authority, model authorization, concurrency,
iteration policy, and aggregate budget. Its digest is immutable run metadata,
and the plan itself binds the exact confirmed `TaskBrief` digest. `run.json` is
atomically replaced under an optimistic revision check and records the
evidence references required for every material transition. A loader verifies
the frozen TaskBrief, TeamPlan, fixed-fixture provenance when applicable, and
all cross-file digests before returning state.

Task self-check reports form a separate write-once digest chain because they
exist before a run workspace and may be revised when an observed dependency
changes. Each result identifies its checkpoint, category, authority, inputs,
dependencies, freshness, severity, status, observed non-secret fact, evidence,
consequence, remediation, and rerun rule. A changed fact invalidates only that
result and its transitive dependents; a stale result cannot authorize Planning
or execution.

The ordinary product entry now consumes that contract at both mandatory
boundaries. Revision 1 is persisted after request/model metadata/USD/deadline
authorization and before the first Planning model call. It includes startup
facts, the complete SAT release/source identity, persisted-schema
compatibility, and one fresh foreground managed-channel observation. A release
endpoint failure is a non-blocking warning; a locally inconsistent managed
identity or unreadable schema is blocking. Source and unmanaged package modes
do not contact the updater. Revision 2 extends the same digest chain after the
user approves a TeamPlan and before SAT creates a run source/workspace. It
materializes and validates the approved dynamic OpenClaw policy against the
read-only profile seed, probes the restricted sandbox, inspects every approved
model route locally, and records each Agent capability/permission/route plus
the workspace and delivery boundaries. Failed approved-plan preflight is
persisted as blocking evidence instead of becoming a late Agent-launch error.
Neither checkpoint makes a semantic provider request.

Every new completed, failed, or user-cancelled `FinalReport` embeds the same
typed `SoftwareVersionReport` captured at task admission (or immediately before
an explicit evaluation run). This binds the controlling release, full source
revision, install mode, channel, artifact provenance, identity status, and
schema support into `final-report.json`; the human report renders the exact
display version and full revision. JSON, Markdown, and the model-spend ledger
are then committed through the same rollback-capable terminal bundle. Older
reports without software identity remain readable and keep their original
canonical serialization.

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
time authority, permission profile, assigned tasks, and dependency
handoffs; it cannot create Agents, edit the DAG, or extend a user deadline.

Each invocation enters the shared budget ledger atomically before launch and
records reported tokens, duration, and known price after completion. Raw
stdout/stderr, telemetry, the semantic response reference when valid, and any
post-call budget rejection are persisted together. When session collection is
active, the same `AgentExecutionRecord` stores its typed execution status,
content-free provider-liveness policy and counters, collection status,
transcript digest, current-turn record count, ordered sanitized tool records,
and any bounded integrity error. Missing model, provider, token, or required
Reviewer tool evidence is never treated as zero usage or success. A repair is
permitted only for a semantic-body failure. Product repair uses the same
provider-liveness and remaining whole-run deadline authority; controlled
evaluation repair receives its frozen invocation timeout. Both remain inside
the applicable budget. Bootstrap Planning turns persist the same liveness
evidence in `PlanningExecutionEvidence`, so a stall or degraded observer cannot
disappear before the execution run exists.

Agent-authored summaries are complete immutable artifact evidence and are not
forced to guess a hidden downstream display limit. When a summary exceeds the
bounded scheduler or Agent-prompt field, the controller derives a deterministic
projection. The projection retains a prefix, states the source character
length, includes the SHA-256 of the complete cleaned summary, and points back
to immutable artifact evidence. Scheduler state is bounded to 2,000 characters
and downstream prompt context to 1,000; the source WorkResult, TestReport, or
ReviewReport remains unchanged. The separate 500-character terminal event
projection normalizes whitespace, ends at a word boundary when possible, and
adds `… [truncated]` instead of silently ending mid-sentence. Projection
therefore cannot turn successful verified work into an artifact failure or
spend a model repair call.

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
parallel up to the approved cap or sequentially when the approved DAG gives one
quality Agent another's durable output as a dependency. They must leave the
exact final commit and clean tree unchanged. Every quality Agent is downstream
of every writer, deterministic gates execute once per immutable iteration, and
all TestReports and ReviewReports bind to that same commit. Completed dependency
and terminal handoffs retain both phase and execution references.
`DynamicWorkflowCoordinator` records the aggregate
snapshot before starting quality work, resolves each iteration, binds blocking
evidence to a bounded revision, and writes terminal evidence. Bare `sat` passes
the exact approved Planning result through this path and then uses the existing
accepted-result delivery boundary.

Terminal finalization first validates the machine report, human rendering, and
complete model-spend ledger in memory. `ArtifactStore` then stages and publishes
`final-report.json`, `final-report.md`, and `budget-ledger.json` as one
write-once bundle. A file-publication failure removes every file created by that
attempt. If the following controller transition fails, SAT verifies all three
digests and rolls back that still-uncommitted bundle before recording the
failure. A detailed-rendering failure uses a dependency-free failure view, so
it cannot overwrite the original terminal reason with a duplicate report error.

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
  explicitly ignore that local lock artifact. Every lock tracked in the
  proposed Git delivery is parsed before setup even when an ignore pattern also
  matches it, and must not contain absolute or Windows-drive paths, `file:`
  sources, parent-directory references, missing or symlinked project-local
  artifacts, or SAT's private offline-wheelhouse location. An effectively
  ignored untracked lock is runtime residue outside the delivery and is neither
  parsed nor copied into clean scratch. This contract prevents the exact
  documented setup command from silently dirtying first-use Git state and
  prevents same-image setup from masking non-portable delivery metadata; it does
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
  traversal. Python probes run only through immutable `sat-probe-run`, which
  validates the owner-only file, executes its open descriptor with a fixed
  interpreter, limits runtime and output, and emits a terminal
  `SAT_PROBE_RESULT_V1` marker. The runner frames child stdout and stderr. For a
  satisfied claim, only completely framed child stdout plus the terminal result
  are eligible; missing or partial framing fails closed, and a marker string
  repeated in traceback source text cannot look like a passing emission. A
  later successful direct probe may supersede an earlier failed
  direct-probe match for the same fragment, while both calls remain captured.
  SAT otherwise rejects a satisfied assessment when a matched tool result
  failed, a matched deterministic command failed or timed out, or no successful
  probe marker exists. An already-revising report with a separate grounded
  blocker may conservatively downgrade such an unsafe positive assessment to a
  new blocked evidence gap; it cannot preserve an accepted verdict or turn the
  failed result into positive evidence. The image includes pinned `uv`
  for relevant bounded probes. The source mount
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
  SAT announces the inspection and gives each potentially cold local catalog
  process up to 90 seconds, independently of the 30-second ordinary-command
  limit and every model-work time authority. An expiry states that no provider
  request was made and prevents Agent creation.
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

- CPU, memory, process, open-file, tmpfs, and captured command-output guards
  protect machine and protocol integrity. They are not substitutes for task
  scope, team topology, or model-work budgets.
- Product Planning and execution use zero as the explicit OpenClaw
  whole-invocation timeout. This removes the former workload-to-seconds mapping
  and saved global timeout override from the product path. Zero does not disable
  liveness: OpenClaw retains its transport boundary, while SAT observes the
  pinned runtime's private raw stream without reading or persisting its content
  and counts only attributable current-turn assistant and tool lifecycle records.
- SAT resolves a provider/model-aware inactivity lease from the pinned runtime's
  cloud or local stream boundary. An explicit provider request timeout replaces
  that implicit boundary, including when it deliberately gives a slow model more
  time. Process and interpreter startup do not start the lease: it begins only
  after SAT attributes the exact current-turn checkpoint or the invocation's
  private provider stream. Stream activity and completed attributable session
  records then renew it. An active tool suspends the provider lease and remains subject to its
  own tool-specific guard; SAT heartbeat text and process existence never renew
  provider liveness.
- Sustained silence first emits a persisted `suspected stalled` event naming the
  observed inactivity, policy source, interruption consequence, and grace period.
  SAT continues checking the private stream and attributable tool state during
  that grace. Trusted activity emits recovery and continues the same invocation;
  continued silence becomes typed `provider_stalled`, terminates only the exact
  SAT-owned process group, and preserves content-free counters in the Planning
  turn or `AgentExecutionRecord`. If attribution or the private observer is
  unavailable, SAT records degraded liveness and does not guess that silence is
  a stall.
- Before the first model call, SAT asks whether the user has a real whole-run
  deadline and recommends no deadline by default. When authorized, the exact
  deadline starts at resource authorization, covers Planning and execution, and
  is converted to remaining seconds before every call. An expired deadline
  prevents another provider call; it never becomes a new per-Agent default.
- Product TeamPlan timeout resolutions record zero with
  `provider_activity` as their source. Fixed evaluation TeamPlans retain
  positive role-specific invocation limits so comparisons can freeze and vary
  time as an explicit experiment variable.
- In controlled evaluations, an initial semantic response and its optional
  repair each receive the frozen evaluation timeout, while call count and
  Agent duration remain measured experiment variables. The ordinary product
  path does not treat call count or Agent duration as user budgets.
- Model compatibility supplements do not set a second provider-transport
  timeout. Product calls pass either zero or the remaining user deadline to
  OpenClaw; controlled evaluations pass their frozen timeout. The outer
  subprocess boundary applies bounded process-shutdown grace after a deadline,
  evaluation timeout, user interrupt, or confirmed provider stall; that cleanup
  guard is not productive work time.
- Controlled evaluations check their frozen token, duration, call, and cost
  thresholds after each invocation. Ordinary tasks use one USD authorization;
  their call, token, duration, Agent, and iteration counts remain telemetry.
- One thread-safe controller ledger atomically reserves every invocation before
  launch. For ordinary tasks, that reservation requires the run, phase, Agent,
  attempt, route, model, paired price, price source, and authorization snapshot;
  the ledger itself calculates cost from those frozen terms and provider usage.
  Controlled evaluations additionally reserve their fixed call count. Completed
  telemetry is retained before any post-call evaluation threshold or task-cost
  rejection; missing token telemetry and unavailable pricing are counted
  explicitly rather than converted to zero.
- An ordinary task cannot make its first model call until every authorized
  route has a frozen paired price or the user explicitly confirms that route
  as zero-cost. Unknown is never converted to zero. Controlled comparisons
  likewise require an explicit paired price table.
- The shared ledger covers Planning, dynamic execution, correction Planning,
  semantic repair, and authorized route switching in one process. Standard
  progress shows recorded spend, authorization remaining, and price source.
  Terminal `budget-ledger.json` preserves every call in reservation order, and
  the human report breaks it down by run, phase, Agent, attempt, route, model,
  token usage, price source, and cost basis. Provider-backed validation remains
  pending.
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

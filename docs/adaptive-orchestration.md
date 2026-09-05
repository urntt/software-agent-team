# Adaptive Orchestration and Interactive Control Specification

This contributor-facing specification defines the product contract for
task-defined Agent teams, interactive planning, observable execution, user
controls, and model routing. Planning, run-scoped execution identity, prompts,
runtime configuration, handoffs, telemetry, Agent-namespaced artifact
persistence, deterministic DAG scheduling, multi-iteration lifecycle
convergence, bare-`sat` activation, and configurable per-Agent progress are
implemented and offline verified. A fresh public-install, bare-`sat`, strict
single-route rehearsal has also completed accepted delivery and independent
post-delivery validation. The foreground control channel is implemented and
offline verified; a separate provider-backed ordinary-user rehearsal has now
applied detailed visibility, guidance, cooperative pause, `/controls`, and
resume. Durable process-restart recovery remains pending. Secret-free model profiles,
deterministic plan-time route resolution, route-specific runtime validation,
and explicit provider-failure switching are implemented and offline verified;
a provider-backed two-route rehearsal remains pending.
Current behavior and gaps remain authoritative in
[`STATUS.md`](../STATUS.md); the completed guided baseline remains specified in
[`product-demo-slice.md`](product-demo-slice.md).

The durable product and architecture decisions behind this design belong to
[`VISION.md`](../VISION.md). This document owns the detailed interaction,
runtime contract, staged implementation plan, and acceptance criteria for the
adaptive-orchestration milestone.

## Design Goals

The next product milestone must let a user:

- Describe a task without choosing a predefined team topology;
- Clarify requirements through ordinary dialogue and focused questions with
  suggested answers plus a custom-answer path;
- Review and revise one overview of requirements, implementation intent, Agent
  responsibilities, dependencies, budgets, and model choices before execution;
- See what the run and every Agent are doing at an appropriate level of detail;
- Guide, correct, pause, resume, interrupt, or cancel a long-running build;
- Configure different models by task, stage, Agent, or scenario without silent
  fallback or loss of experimental reproducibility.

Dynamic orchestration does not mean unbounded autonomous spawning. The
controller, not a model, continues to own Agent creation, permissions,
scheduling, budgets, lifecycle transitions, evidence, and cleanup.

## Fixed System Capabilities and Dynamic Execution Roles

The product keeps a small set of fixed system capabilities:

- The product CLI and interaction layer;
- A bootstrap Planning capability;
- The deterministic controller;
- Versioned permission profiles and sandbox policies;
- Deterministic quality gates and artifact validation;
- The progress renderer and control channel.

The bootstrap Planning capability is not a permanent execution-team role and
is not an orchestrator. It may ask questions and propose a plan, but it cannot
spawn Agents, grant tools, advance lifecycle state, or accept its own proposal.

Execution roles are run-scoped. Their names, number, responsibilities,
dependencies, prompt purposes, and model routes are derived from the confirmed
task. A small CLI utility may need one implementation Agent and one independent
quality Agent. A Web application may justify separate interface, backend,
integration, testing, and review responsibilities. The system must explain why
each proposed Agent exists rather than selecting a larger team by default.

Independent quality control is a controller requirement, not a fixed role
name. A plan may assign testing and review to one or more read-only Agents, but
the same Agent that writes a change cannot be the sole authority accepting it.
When separate Testing and Review Agents exist, the approved DAG may make them
parallel peers or may place Review after Testing so it can consume that durable
handoff. Both remain read-only and downstream of every writer; independence
does not impose a hidden peer-only topology.

## Decision and Control Responsibility

Adaptive does not make execution order, parallelism, Agent count, or time boundaries
unowned model choices. Responsibility is divided explicitly:

| Decision | Proposal | Approval or default | Runtime enforcement |
| --- | --- | --- | --- |
| Agent number, labels, responsibilities, and capabilities | Bootstrap Planning derives them from the task and explains each one | User approves or revises the overview | Controller creates only approved `AgentSpec` entries |
| Dependencies and possible parallel waves | Bootstrap Planning proposes a DAG | User approves it; policy supplies safe limits | Controller validates acyclicity and schedules only ready nodes |
| Maximum concurrency | Bootstrap Planning proposes a bounded value | User may edit it; policy caps it | Controller decides which ready Agents actually start without exceeding the cap |
| Whole-run time | Planning may explain likely duration but does not invent a deadline | SAT asks before the first model call; default is no deadline unless the user has a real one | Controller applies only the explicitly authorized task deadline |
| Provider-call liveness | The model does not choose its own watchdog | Provider/model capability and measured evidence define a renewable inactivity lease plus probe/grace behavior | Controller renews only from trustworthy activity and interrupts only after sustained verified silence |
| Task model cost | Planning may explain route use and cost exposure | User authorizes one total USD ceiling covering the complete task journey | One monotonic ledger accounts for Planning, execution, targeted correction, and switching; calls, tokens, Agent count, iterations, and duration remain telemetry |
| Model route | Planning may recommend task needs; configured profiles and routing policy provide candidates | User approves effective routes and switch conditions | Controller resolves and records the authorized route; there is no silent fallback |
| Replanning or team changes during execution | User correction or an Agent recommendation may request a change | Material changes require a new validated revision and user confirmation | Controller applies a revision only at a safe checkpoint |

The Planner therefore proposes semantic organization and workload estimates,
the user authorizes material choices, policy resolves the allowed operational
envelope, and the controller owns validation, creation, scheduling, time authority,
lifecycle, evidence, and cleanup.

Dependencies are the complete sequencing contract. A quality Agent may depend
on another quality Agent when the overview makes that handoff explicit. The
controller does not silently rewrite the DAG to maximize parallelism, and the
scheduler never starts a dependent Agent early.

Fixed capability seconds, call/token ceilings, and iteration counts remain
valid only when a controlled evaluation deliberately freezes them as measured
variables. They are not ordinary-product defaults. Product admission instead
records one user-approved USD ceiling and an optional whole-run deadline. Each
provider invocation is protected by a separate renewable inactivity contract:
trustworthy provider streaming, tool lifecycle/output, controller-verified
artifact, or checkpoint activity renews the lease; SAT's own elapsed-time
heartbeat and a live process do not. Sustained silence enters a visible
suspected-stall probe and grace state before interruption and evidence cleanup.

Before Planning or execution, a separate local readiness check verifies each
authorized OpenClaw model route without generating content. Its 90-second
model-catalog boundary is infrastructure policy, not part of the TeamPlan and
not time available to an Agent; ordinary preflight commands retain a 30-second
bound. SAT announces this wait and stops before Agent creation if it expires.

## Planned User Journey

```text
run `sat`
→ complete automatic diagnostics and first-run setup when needed
→ describe the desired outcome
→ authorize model-backed planning
→ clarify through dialogue and focused questions
→ review one plan overview
   ├── approve
   ├── request a natural-language revision
   └── edit supported structured fields
→ controller validates requirements, team, routes, permissions, and budgets
→ controller creates run-scoped Agents and starts execution
→ watch run-level and per-Agent progress
→ optionally guide, correct, pause, resume, interrupt, or cancel
→ receive a runnable result or an honest failure/cancellation report
```

The user does not edit JSON, internal prompts, run IDs, workspace roots, or
OpenClaw configuration in the normal flow. Advanced output may expose the
validated plan and resolved model routes for inspection without making those
files prerequisites for starting a build.

## Planning Session

### Entry and Authorization

SAT first collects enough non-model input to establish the requested outcome,
execution profile, destination boundary, and authorization to spend model
resources. No model-backed planning call occurs before that authorization.

The Planning session then maintains a versioned proposal. It can use:

- Normal conversational questions when the answer space is open;
- Focused questions with two or three recommended choices;
- A custom-answer option for every focused question;
- Direct confirmation when existing repository evidence already answers a
  question;
- A concise assumption when the choice is reversible and low risk, clearly
  shown in the overview before approval.

Questions should be selected for decision value. The planner must not turn
every implementation detail into a user prompt or silently decide a missing
product requirement on the user's behalf.

Every focused question carries a stable decision category and owner, the
evidence that is missing, the material consequences of choosing differently,
and two or three alternatives plus a custom-answer path. The controller owns
the category-to-authority mapping. It rejects questions about reversible local
implementation or scheduling, rejects any attempt to delegate safety or
evidence-integrity policy, and rejects a category whose declared owner does not
match the mapping. This is an admission boundary, not a claim that deterministic
code can infer the semantic value of arbitrary prose; ambiguous real tasks
remain the usability test for under- and over-questioning.

A current proposal records one stable ID per requirement, explicit non-goals,
and attributable decision records. Every answered question resolves exactly one
decision with the original category and owner. Assumptions may reference only
local implementation or scheduling decisions inside the approved boundary;
they cannot substitute for a user authorization or Controller invariant.

### Planning Response Boundary

Planning responses remain strict, but harmless presentation differences are
not treated as reasoning failures. Before schema validation, the controller
may perform only these bounded, semantics-preserving normalizations:

- Infer `kind` when exactly one non-null `question` or `proposal` body makes it
  unambiguous;
- Remove a criterion definition whose exact ID belongs to the active
  controller-owned execution profile, while retaining any task binding to that
  known ID and using only the profile's canonical definition;
- Canonicalize safe relative `expected_paths` values such as `tests/` to
  `tests`;
- Canonicalize safe `workspace_scope` presentation such as `repository/` to
  `repository`;
- Remove a schema-forbidden field only when removing it cannot grant or hide
  controller/evidence authority.

The immutable turn retains the exact raw response and records every normalized
field or removed profile-owned definition separately, including normalizations
completed before a later validation failure. The active policy is the
only source of IDs eligible for criterion-ownership normalization; the
controller does not compare or adopt the model-authored description,
verification text, or Review boundaries. Absolute paths, backslashes, parent
traversal and ambiguous response bodies remain invalid. Protected authority
fields are rejected rather than normalized away. Other model-owned defects are
eligible only for digest-bound correction of the exact typed paths identified
by validation; the model never regenerates the complete retained object.

OpenClaw may deliver the semantic response and an ancillary tool diagnostic as
separate visible payloads. The execution boundary preserves their original
payloads in raw telemetry and combines the visible text in order for strict
semantic parsing. One unambiguous top-level object plus presentation-only text
is valid; two object candidates remain ambiguous and are rejected. Transport
payload count is therefore not mistaken for semantic object count.

The pinned OpenClaw Agent CLI does not expose a response-schema parameter for a
tool-using turn. SAT therefore compiles responses at the controller boundary.
Transport failures and unlocated errors stop. A targetable model-owned failure
produces a content-free diagnostic and a `semantic_correction_v1` envelope bound
to the retained object's SHA-256. The model supplies values for exactly the
listed JSON-pointer paths; every other value remains immutable. Product
Planning continues only after every prior validator-owned invariant/subject
identity disappears and a distinct targetable failure remains within the task
budget. A newly exposed relational error is not treated as the same defect merely
because its JSON pointer overlaps the corrected field; each Planning relational
validator supplies a stable invariant ID, structured criterion/task/Agent or
other entity subjects, and the smallest model-owned authority path it can
justify. Error prose is display-only. An unclassified relation fails closed
without guessing a broad replacement. A repeated fingerprint or the same typed
issue stops, while controlled evaluation may intentionally impose a zero-or-one
correction cap.

Every workspace scope describes controller authority inside the generated
repository: `repository` grants whole-project access and `repository/path`
grants a narrower boundary. A destination or project directory name is not a
workspace scope and is rejected rather than silently widened.

Acceptance criteria have two distinct owners. The Planner defines
task-specific criteria and must bind every one of them to at least one
implementation task. The execution profile defines fixed criteria whose text
and verification contract remain controller-owned; the Planner should not echo
those definitions, but a task may reference a profile criterion ID supplied in
the current Planning context when the task materially implements or verifies
it. If a response nevertheless repeats an exact active profile ID in its
definition list, the controller never imports that model-authored text as the
profile definition. A redundant echo is removed and audited. If deleting it
would lose a requirement-to-acceptance relation that has a responsible writer,
the controller instead assigns that task-specific criterion a deterministic
non-reserved ID, preserves its model-owned verifier and Review-boundary fields,
and expands the ambiguous task reference to both the task-specific and canonical
profile IDs. Context-free validation then checks ID syntax and complete coverage
of the resulting Planner-owned criteria. The policy-aware controller resolves every
task reference against the union of proposal and current profile IDs. It
rejects any other ID before an overview is shown and preserves valid profile
bindings when it materializes the TaskBrief and implementation plan. A profile
criterion need not be forced onto a task merely because it exists.

Each Planner-owned criterion also declares `review_boundaries`. Most criteria
use an empty list. A description containing an unqualified prohibition or
safety guarantee must declare all four controller-known entry boundaries:
top-level input, nested input, alias or indirection, and failure path. These
obligations are shown in the overview and become part of the confirmed
TaskBrief; they cannot be silently weakened by the execution Reviewer.

Boundary identifiers have controller-owned meanings; they are not casual labels
that an Agent may reinterpret from filesystem depth:

| Identifier | Protocol meaning |
| --- | --- |
| `top_level_input` | The primary input value, object, resource, or entry point selected or supplied directly by the user or upstream caller, before traversal, expansion, or decomposition. If a path or directory is selected as a root, the root itself is the top-level input; an immediate child inside it is already nested input. |
| `nested_input` | An input discovered inside or below the primary input after traversal, expansion, or decomposition; both immediate children and deeper descendants qualify. |
| `alias_or_indirection` | The same logical input reached through an alias, symlink, redirect, wrapper, reference, configuration indirection, or another non-canonical route. |
| `failure_path` | A missing, malformed, invalid, inaccessible, unsupported, rejected, or otherwise failing input or operation; Review checks the observable failure behavior, not merely whether the process avoided a crash. |

`artifacts.py` owns these exact definitions. Planning context, the approval
overview, every runtime Agent context, and public documentation project the
same mapping. A model response can choose relevant obligations and describe a
concrete challenge, but it cannot redefine a boundary.

The `tasks` collection records approved work intent for any runtime Agent.
Implementation and integration Agents must each own at least one task, and
their tasks—not quality-only tasks—must cover every Planner-owned acceptance
criterion. Testing and Review Agents may own tasks that state their verification
focus. Those entries are preserved in the overview and prompt, but they do not
create an Agent, grant tools or write access, expand review scope, choose a
model, set a timeout, or create another model call. Those authorities come only
from the approved `AgentSpec` and controller policy. Every task owner must exist,
the task DAG must be acyclic, and a cross-Agent task dependency is valid only
when the owning Agent depends transitively on the dependency owner. The
controller applies the same binding validation during proposal parsing, prompt
construction, and runner startup.

Testing and Review capabilities are always read-only. Their tasks may describe
inspection, evidence analysis, exercising existing behavior, or review focus,
but every task that creates or changes project code, tests, configuration, or
documentation belongs to an implementation or integration Agent. The overview
prints the effective task authority derived from the owner's `AgentSpec`, so a
free-text task description cannot grant mutation authority or hide a mismatch
from the user before approval.

### Overview Before Execution

The proposal shown before execution contains:

1. The requested outcome and explicit non-goals;
2. Success conditions, constraints, assumptions, and decision provenance split
   by user, Planning, execution autonomy, and non-negotiable Controller policy;
3. The implementation approach, major deliverables, and each task's criterion
   bindings and dependencies;
4. The proposed Agents, why each exists, and what each owns;
5. Agent dependencies, expected handoffs, and independent quality coverage;
6. Permission and workspace boundaries in plain language;
7. Model choices or routing preferences, including any authorized automatic
   selection or switching;
8. The user-approved task USD authorization and optional deadline; controlled
   evaluations additionally show their frozen call, token, duration, and cost
   limits;
9. The delivery destination and expected validation commands.

The clarity gate requires every requirement to reach at least one observable
criterion, every criterion to reach a responsible writer task, and every such
writer to reach a named downstream read-only verifier. The overview renders
that graph and each Agent's inputs, expected output, and handoff. Completeness
is deterministic; whether the presentation is understandable remains a real
user test.

The overview presents controller-owned execution-profile constraints separately
from additional task-specific constraints proposed during Planning. The Planner
is instructed not to repeat, paraphrase, shorten, or broaden the former. Both
collections remain present in the compiled TaskBrief so source labeling improves
the approval experience without discarding a material Planner addition.

The default editor supports natural-language revision and structured changes
to requirements, priorities, Agent responsibilities, dependencies, and model
preferences. Raw system prompts, arbitrary tool grants, and direct policy-file
editing remain an advanced contributor surface. Even advanced changes pass the
same controller validation.

Approval freezes version one of the run contract. Product plans record
`provider_activity` with zero per-Agent wall-clock limits plus the optional
user-authorized whole-run deadline. Controlled evaluations may instead freeze a
positive timeout, its policy envelope, and exact resolution source. Later
corrections create a new version; they never mutate an already referenced plan
in place.

## Versioned Contracts

The Planning, team, event, and control contracts below are executable schemas.
Dynamic prompt compilation and control application build on them rather than
creating a parallel configuration system.

### `TeamPlan`

A `TeamPlan` binds one confirmed TaskBrief and ImplementationPlan to:

- A stable plan ID and revision;
- Run-scoped `AgentSpec` entries;
- A directed acyclic dependency graph;
- Required handoffs and completion conditions;
- Independent verification and review coverage;
- Aggregate iteration and resource budgets;
- A `ModelRoutePlan`;
- The user approval record, controller time-authority resolutions, and planner
  proposal evidence.

### `AgentSpec`

Each `AgentSpec` contains:

- A stable run-scoped Agent ID and user-facing label;
- One distinct responsibility and an explanation of why it is needed;
- Required inputs and typed outputs;
- Predecessors, successors, and scheduling constraints;
- A controlled permission profile;
- Read-only or writable workspace ownership;
- Acceptance responsibility without authority to advance lifecycle state;
- Per-invocation and aggregate budget allocation;
- A model-route reference;
- A prompt-purpose specification compiled with versioned controller templates.

The plan stores prompt intent and inputs, not an unreviewed instruction blob
that can silently grant authority.

### `ModelRoutePlan`

A `ModelRoutePlan` contains:

- User-authorized provider/model candidates;
- A default route;
- Optional task-, phase-, capability-, or Agent-specific overrides;
- Required capabilities such as context size, tool use, structured output, or
  vision input;
- Cost, latency, and quality preferences;
- Explicit switch conditions and limits;
- A strict evaluation-mode flag;
- The deterministic resolution reason recorded for every invocation.

### `RunEvent`

The controller appends structured events with:

- Run ID, sequence, timestamp, and lifecycle revision;
- Agent ID and attempt when applicable;
- Event category and state transition;
- A bounded user-safe activity summary;
- Artifact, handoff, gate, Git, budget, or model-route references;
- Visibility class;
- Optional control-command correlation.

Events are evidence-backed status, not hidden chain-of-thought. An Agent may
return a bounded status summary at defined checkpoints, but the controller
labels its source and never turns free-form reasoning into authoritative state.
An overlong terminal event summary ends at a word boundary when possible and
uses an explicit `… [truncated]` suffix; the complete artifact summary remains
available instead of being silently cut in the user display.

### `ControlCommand`

A control command records:

- Command ID, run ID, request time, and requester;
- Command type and bounded payload;
- Target run, Agent, phase, or future work;
- Requested application boundary;
- `queued`, `applied`, `rejected`, `superseded`, or `best_effort_failed` state;
- Resulting plan revision or lifecycle transition;
- A plain-language consequence and any provider-cost caveat.

## Controller Validation and Agent Creation

After user approval, the controller validates all of the following before an
Agent is created:

- Schema and TaskBrief consistency;
- Complete task coverage of Planner-owned criteria, controller ownership of
  profile criterion definitions, and no task reference outside their known
  union;
- Acyclic dependencies and at least one terminal delivery path;
- Unique writable ownership or an explicit integration protocol;
- Permission profiles compatible with each responsibility;
- Independent quality coverage;
- Ordinary-task USD/deadline authority, host-derived concurrency, and any
  separate controlled-evaluation call, iteration, duration, token, or cost
  limits;
- Available and authorized model routes;
- Prompt inputs that can be reconstructed from versioned templates and
  persisted artifacts;
- No Agent-spawning, lifecycle, credential, deployment, or publication
  authority in an Agent profile.

The controller then materializes run-scoped OpenClaw Agent sessions and
schedules ready nodes in the dependency graph. Agents may recommend another
specialist, but that recommendation is only a plan-amendment request. It does
not create a model call. A team change requires a safe checkpoint, a new
validated TeamPlan revision, budget authorization, and user confirmation when
the change affects scope, cost, or delivery expectations.

Task-specific quality remains semantic work rather than a claim made by the
generic profile gates. Planning must turn every unqualified prohibition or
safety guarantee into acceptance and test intent across relevant entry
boundaries. Implementation must exercise top-level and nested inputs, aliases
or indirection, and failure paths when they apply. Independent Review must
adversarially challenge the same scope. A Dynamic Reviewer returns exactly one
criterion assessment for every assigned criterion, with a concrete negative or
boundary case and observable evidence. Blocked assessments and blocking
findings must cover the same criteria. When exactly one unscoped blocking
finding remains, the controller binds it to every otherwise-uncovered blocked
criterion; multiple unscoped findings are ambiguous and invalid. Missing
coverage is never implicit acceptance. Every assessment must also supply one
or more bounded result fragments. A `semantic_body_v4` assessment returns
`boundary_checks` explicitly. A satisfied criterion must check every boundary
approved in its TaskBrief, with a distinct attributable fragment for each; a
blocked absolute criterion may stop after one grounded counterexample. A
criterion with no approved boundary should return an empty list, and a Reviewer
cannot add or remove obligations. If it nevertheless supplies extra boundary
entries, the controller removes and records them against the frozen TaskBrief
scope before validating their nested content; only approved entries can create
coverage or correction obligations. This makes entry coverage a controller-
validated contract rather than a summary claim. The response schema makes the fragment
structurally required and forbids model-supplied attempt, tool, or command IDs.
Exact text is preferred; a keyed JSON fragment may differ only in RFC JSON
whitespace outside quoted strings. An initial response uses its current
invocation. During a targeted correction, the same Reviewer may also reuse
integrity-checked results captured by
an earlier attempt in that same role-stage, immutable-commit, and invocation
chain. Deterministic command stdout/stderr from the same immutable iteration is
also eligible. The controller requires every fragment to occur in at least one
eligible output, enriches the persisted assessment with every protocol-eligible
actual attempt-qualified tool ID or command ID and available provenance, and
deduplicates repeated or overlapping selectors. Evidence cannot cross an Agent,
stage, iteration, commit, or correction chain. A zero match is invalid; multiple
real matches are preserved instead of delegated back to model wording. Review may
run bounded foreground
probes in the no-network sandbox against the read-only source and temporary
fixtures. General write tools remain denied. Review creates a script or fixture
only through the immutable `sat-probe-write` helper, which accepts a new bounded
canonical direct child matching `/tmp/sat-review-probe-*`, rejects overwrite
and path indirection, and returns an observable success or refusal. The
Reviewer then invokes a Python probe only through the immutable
`sat-probe-run` helper. It validates the owner-only probe, executes an immutable
file descriptor with a fixed interpreter and project working directory, bounds
time and output, frames child stdout separately from child stderr, and emits a
terminal `SAT_PROBE_RESULT_V1` child result.
Project mutation, complex interpreter invocations, background processes, and
network remain unavailable. For a satisfied direct-probe claim, only framed
child stdout and the terminal result are positive evidence; traceback source
text in child stderr cannot establish success. If the same fragment was emitted
by a failed direct probe and a later successful direct probe, the successful
emission supplies the claim while both calls remain auditable. A satisfied
assessment still cannot select a positive substring from any other matched
failed tool result, failed or timed-out deterministic command, or a chain with
no successful probe emission. If the report is already `revise`, has at least
one independently blocked assessment and blocking finding, and an additional
positive assessment fails only this safety check, the controller performs a
monotonic recovery: it re-grounds that assessment as `blocked` against the same
failed evidence and adds a criterion-scoped controller finding requiring fresh
verification. This keeps an otherwise useful revision in the iteration loop
without treating failure as proof of success. It does not apply to `accept` or
`fail`, unmatched selectors, an invalid pre-existing blocker relationship, or
any other semantic defect. That self-directed evidence remains
attributable and is not
relabeled as a controller deterministic gate. Documentation may state only the
boundary established by the implementation and evidence. The generated-project
contract separately checks that the README shows each exact manifest command,
that documented first setup does not leave an unexplained root virtual
environment or lock file in an otherwise clean delivery, and that every lock in
the proposed Git delivery contains no host- or sandbox-only dependency source.
An effectively ignored untracked lock is setup/runtime residue outside that
delivery and is neither parsed as product metadata nor copied into validation
scratch. A deterministic gate
copies clean committed files into fresh scratch, then executes exact setup,
test, and start argv through the runtime's offline wheelhouse. Its start argv
must work from the project root without appended arguments. Independent Review
probes task-specific runtime behavior that this generic contract cannot infer.

Existing fixed team manifests remain versioned evaluation fixtures. During
migration they are compiled into the same `TeamPlan` contract so the repository
does not retain two lifecycle implementations.

The current Git workspace backend uses one detached clone and one Git index.
It may execute read-only ready nodes concurrently, but it must serialize
workspace writers even when their declared path scopes do not overlap. Parallel
writer execution requires isolated worktrees or clones plus an explicit,
controller-verified integration step; the scheduler must not infer Git safety
from path ownership alone.

## Progress and Information Design

### Progress Hierarchy

The display follows one stable hierarchy:

```text
run
└── planning or execution phase
    └── Agent
        └── attempt, activity, handoff, or quality check
```

Every Agent can be `queued`, `ready`, `running`, `waiting_provider`,
`waiting_dependency`, `blocked`, `paused`, `completed`, `interrupted`,
`cancelled`, or `failed`. The interface shows elapsed time, the last meaningful
safe summary, and the dependency or blocker when known. It does not invent a
completion percentage when the controller lacks a meaningful denominator.

### Visibility Levels

The user may change visibility during a run without changing execution:

- `compact`: current run phase, important decisions, blockers, and terminal
  result;
- `standard` (default): compact information plus every Agent's state, current
  safe activity, completed handoffs, gates, revisions, and elapsed time;
- `detailed`: standard information plus attempt IDs, dependency transitions,
  model route, budget consumption, tool categories, artifact references, and
  controller validation events.

Raw provider credentials, environment secrets, hidden reasoning, unbounded
model output, and unrelated host information are excluded from every level.
TTY mode may update a live panel; non-TTY mode emits ordered line events using
the same event source. Logs and a future graphical UI must consume that same
contract rather than infer progress independently.

## Input and Interaction Quality

The terminal interaction must remain usable while progress is updating:

- Ask one decision at a time and explain why it matters;
- Mark the recommended answer and its trade-off instead of presenting an
  unexplained list;
- Always accept a custom answer, and support back, revise, skip-when-optional,
  and cancel actions without discarding earlier answers;
- Validate input at the affected question and preserve the user's text after a
  recoverable error;
- Allow concise and multiline natural-language input without requiring JSON or
  shell escaping;
- Suspend live-panel redraw while the user is typing so keystrokes and text are
  never overwritten;
- State when an action will spend model budget, invalidate work, interrupt an
  active attempt, or make cancellation terminal;
- Adapt to narrow terminals and provide a stable line-mode fallback with no
  color or Unicode dependency;
- Keep secrets in trusted provider setup prompts and never echo them into the
  Planning conversation, progress stream, or plan overview.

The current answer set and plan draft are recoverable local state. Returning
to an earlier question creates a new draft revision instead of silently
changing an already approved plan.

## User Controls

The controller owns a local authenticated control mailbox. The foreground TTY
exposes the following line-mode palette after plan approval:

```text
/guide <agent|future|phase:name> <instruction>
/correct <replacement requirement>
/pause
/resume
/interrupt <active-agent-id>
/cancel confirm
/visibility <compact|standard|detailed>
/controls
/help
```

Each accepted command is written before the controller applies it. Request
ordering uses a controller-assigned mailbox sequence rather than timestamp or
random command-ID ordering. A later secondary `sat` process may submit the same
contract, but that secondary-process interface is not implemented yet.
Presentation may evolve, but these semantics remain stable:

### Guide

Adds prospective context for incomplete work at the next safe checkpoint. It
does not silently rewrite confirmed requirements or invalidate completed
evidence. The UI shows which Agents or phases will receive it.

### Correct

Declares that requirements, assumptions, priorities, team design, or model
choices are wrong. The controller stops scheduling new work, preserves the
current evidence, opens a Planning revision, shows the invalidated downstream
work, and requires confirmation before continuing.

### Pause and Resume

Cooperative pause stops new invocations and reaches `paused` after active work
arrives at the selected safe boundary. It does not promise to suspend an
arbitrary provider HTTP request or process instruction instantly. Resume first
revalidates evidence integrity, workspace state, remaining budgets,
dependencies, credentials, and model availability.

### Interrupt

Interrupt requests best-effort termination of one active invocation or Agent
attempt. The attempt and any partial output remain evidence. Provider usage may
already have been incurred. The controller does not retry automatically; the
user chooses whether to replan, retry within budget, continue other independent
work, or cancel.

### Cancel

Cancel is terminal. The controller stops scheduling, requests termination of
active SAT-owned work, cleans only resources proven to belong to that run, and
produces a cancellation report. It preserves evidence and never presents a
partial workspace as an accepted delivery.

## Model Configuration and Routing

Users can maintain multiple secret-free model profiles while credentials stay
inside SAT's isolated provider boundary. Route resolution uses this precedence:

1. An explicit per-Agent profile edit approved in the current TeamPlan;
2. A configured stage override;
3. A configured Agent-capability override;
4. The default profile when it is authorized for that capability;
5. The lowest numeric priority among remaining capability-authorized profiles,
   with saved profile order as a deterministic tie-breaker.

The final step is controller-owned deterministic selection, not an
unconstrained model decision or an unverified quality claim. The bootstrap
Planner describes capability and workload needs but cannot add model fields or
authorize a route. The user may inspect and edit the effective per-Agent
profile in the Planning overview; the controller then validates and freezes
the exact primary and fallback assignments.

Before execution, SAT verifies the bootstrap model and every route authorized
by the approved TeamPlan through its isolated OpenClaw catalog/auth boundary.
Task admission first refreshes every configured route's context capacity and
input/output price. Discovery is preferred; the user is asked only when context
remains unknown, while an unknown price must be supplied or explicitly
confirmed as zero. Every invocation records the canonical provider/model,
route reference, resolution source and reason, frozen price source and
observation time, telemetry, estimated cost, and remaining task authorization.
The standard progress view shows the updated amount after every invocation;
the terminal ledger and report preserve the complete Planning-to-delivery
breakdown by phase, Agent, attempt, route, and model.
Runtime switching is currently permitted only after an attributable
`provider_failure`, only when the approved Agent assignment lists a next route,
and only within that finite approved route list. The failed invocation is
persisted and budget-accounted before the UI announces the switch and its
possible provider-cost consequence. Targeted semantic correction is a separate,
improvement-gated mechanism. There is no silent fallback.

Controlled evaluation mode remains stricter: one canonical model and price
table are pinned for the run, switching is disabled, and topology trials remain
comparable. Model-routing experiments hold the TaskBrief and TeamPlan constant
and vary only the route policy.

## Persistence and Recovery

Approved plan revisions, RunEvents, ControlCommands, resolved routes, and Agent
creation records become write-once evidence referenced from atomic run state.
Session history remains diagnostic and cannot be the only copy of guidance or
correction.

Durable pause/resume and process-crash recovery require an integrity-checked
checkpoint. Recovery never assumes an unrecorded provider, tool, Git, or
delivery action succeeded. If the exact boundary cannot be proven, SAT reports
the uncertainty and requires a new attempt or run instead of guessing.

## Implementation Batches

### Batch 3A: Contracts and Compatibility Path

- Add versioned TeamPlan, AgentSpec, ModelRoutePlan, RunEvent, and
  ControlCommand schemas;
- Add deterministic validators for DAGs, ownership, permissions, budgets,
  quality independence, and model authorization;
- Compile existing fixed manifests into TeamPlan;
- Move the current workflow onto that contract and remove the direct parallel
  role-list path;
- Persist append-only events while preserving current user behavior.

**Exit:** the current function-specialized offline suite and product path run
through TeamPlan with no behavior regression, and invalid dynamic plans fail
before any model invocation.

### Batch 3B: Planning Dialogue and Overview

- Add model-work authorization followed by multi-round Planning dialogue;
- Support free-form answers, suggested options, and custom responses;
- Produce one requirements, implementation, team, route, and budget overview;
- Support natural-language revision and safe structured edits;
- Freeze the approved plan and provide a non-interactive fixture path for
  deterministic tests.

**Exit:** a user can start from an ordinary request, revise the proposed team,
and approve a complete validated TeamPlan without editing an internal file.

**Implementation note:** the versioned request, question/proposal response,
append-only turn and proposal store, natural-language revision, safe limit
editor, complete overview, explicit approval evidence, and deterministic
ordinary-user interaction test are implemented. Bare `sat` now activates this
interaction together with Batch 3C, so an approved dynamic plan is the exact
plan the controller executes. Before strict validation, the Planning boundary
infers only an unambiguous response kind, canonicalizes only safe relative-path
presentation, removes redundant active-profile definition echoes, and
deterministically deconflicts an echo whose model-owned relationship is still
needed. The canonical profile text remains controller-owned, both task bindings
are retained, and the raw response plus every normalization remain recorded in
the Planning turn.
Planning schema v4 adds typed validation diagnostics, deterministic
normalization, and targeted-correction evidence while retaining read support
for schema-v2 and schema-v3 evidence. Current live response schemas make the
responsibility and clarity fields mandatory and non-null. Structured edits of
historical evidence preserve its schema identity rather than relabeling it as
current.
Blocking model waits emit a concise heartbeat every ten seconds, record when a
response returns, show contract validation, and explicitly announce each exact
correction target. These messages expose elapsed time and controller state, not
prompts or hidden reasoning. The execution adapter additionally projects
content-free provider stream and attributable tool lifecycle activity. A
provider/model-aware renewable silence lease emits a policy-attributed warning,
grace, recovery, or typed stall; Planning turn evidence preserves the same
counters and never stores streamed response content as progress.

### Batch 3C: Dynamic Team Runtime

- Compile run-scoped prompts from AgentSpec and persisted inputs;
- Create only controller-authorized OpenClaw sessions;
- Schedule the dependency graph with bounded concurrency;
- Enforce permission profiles, workspace ownership, typed handoffs, independent
  quality coverage, and aggregate budgets;
- Support versioned team amendments at safe checkpoints.

**Exit:** at least two materially different tasks produce different justified
teams and complete or fail through the same controller, evidence, and cleanup
boundary.

**Implementation note:** run-scoped Agent identity and capability telemetry,
approved-Agent-only OpenClaw configuration, AgentSpec-derived prompt and
response contracts, exact model and controller time-authority binding, and
AgentSpec-derived cleanup selection are implemented. Adaptive plans may use one downstream independent
quality Agent for a small task; separate testing and review Agents remain an
explicit justified choice rather than a hidden minimum topology. Controller
artifact/handoff attribution, bounded DAG dispatch, shared controller-owned
WorkResult/TestReport/ReviewReport assembly, and dynamic iteration aggregation
are also implemented. Iteration validation requires a chained result from
every approved writer, deterministic evidence from every approved Tester or
from the controller when no Tester exists, evidence from every approved
Reviewer, one immutable quality commit, and complete manual-review coverage.
The dynamic runner now binds each scheduler-approved Agent to its exact model,
time authority, prompt, semantic-correction policy, Git or read-only boundary, aggregate
budget, execution record, and durable handoffs. Its persisted events distinguish
provider activity, tool start/completion, degraded observation, suspected stall,
grace recovery, and terminal provider stall. A confirmed stall preserves typed
content-free evidence and may use only an already approved provider-failure
fallback; total productive wall-clock time is not a stopping condition. Quality gates are shared once
per immutable iteration, and every quality Agent must be downstream of every
writer. Controlled evaluation timeout resolution remains separate from the
product's provider-liveness and optional whole-run deadline. The Reviewer runtime keeps project
source read-only and denies the general write tool. Its immutable
`sat-probe-write` command provides the bounded `/tmp` probe-authoring capability
that actually exists in the foreground execution surface. `sat-probe-run`
provides the matching fixed, bounded, controller-verifiable execution path; the
runtime also contains pinned `uv` for relevant bounded probes. The OpenClaw adapter validates
each exact session turn, pairs actual tool calls and results, and persists only
bounded sanitized records. Dynamic semantic correction may carry those records
forward only within one Reviewer, role stage, immutable commit, and invocation
chain. Every protocol-eligible semantic fragment is bound to an
attempt-qualified controller-owned tool ID; overlapping selectors are
deduplicated, and zero-call
or absent Reviewer evidence is rejected. Complete Agent
summaries remain immutable artifact evidence; the controller derives bounded
scheduler-record and downstream-prompt projections with an explicit truncation
marker, original length, and source-summary SHA-256 rather than failing a
handoff or asking the model to regenerate known content. Its shorter terminal
event projection also uses an explicit truncation suffix and a word boundary
when available. The
adaptive lifecycle coordinator crosses the authoritative snapshot
boundary before the first quality Agent starts, aggregates every approved
output, decides accept/revise/fail, binds prior blocking evidence to the next
iteration's starting commit, stops an unchanged repeated blocker, and writes
the same integrity-checked final evidence and human report as the compatibility
workflow. Bare `sat` now authorizes Planning, creates its read-only bootstrap
runtime, presents the complete overview, materializes only an approved
source/run, executes the approved Dynamic Team, delivers only acceptance, and
cleans bootstrap and runtime sandboxes. Safe plan amendment checkpoints remain
in this batch.

The current single-clone Git backend serializes every writer and excludes
readers while a writer is active; independently ready read-only quality Agents
may run concurrently up to the user-approved concurrency value and available
host capacity. Ordinary product Planning has no fixed Agent, Reviewer, call, or
iteration maximum. Team size and complementary quality responsibilities come
from the task, risk, dependency graph, and approved USD budget; controlled
evaluation limits remain separate experiment inputs.

### Batch 3D: Observable and Controllable Execution

- Implement compact, standard, and detailed renderers over RunEvent;
- Show every Agent's state, safe activity, dependencies, handoffs, model route,
  gates, and budgets as allowed by visibility;
- Add controller-owned guide, correct, cooperative pause/resume, interrupt, and
  cancel commands;
- Add cancellation, interruption, restart, event-order, non-TTY, and resource-
  cleanup tests.

**Exit:** an offline end-to-end run demonstrates every command and visibility
level with deterministic event evidence; an authorized live run demonstrates
at least guidance and cooperative pause/resume without losing integrity.

**Implementation note:** append-only events now project scheduler queue and
readiness, invocation and provider wait, targeted semantic correction, completion,
failure, and blocked states with Agent dependencies, capability, stage, model,
duration, evidence, and aggregate budget data. Configuration schema v8 selects
compact, standard, or detailed terminal projection. The foreground palette can
change that projection without changing execution. Its persisted runtime
channel applies prospective guidance to the next invocation, drains active work
for safe correction and pause checkpoints, resumes cooperatively, sends
best-effort process-group termination for interrupt or cancel, and records
provider-cost caveats. Correction produces a cancelled superseded-run report
and starts a fresh Planning request with the user's correction preserved;
cancel produces a terminal cancellation report and the existing exact-owned
sandbox cleanup still runs. Offline unit and end-to-end tests cover these
semantics. Heartbeat lifecycle is independent of visibility filtering, hidden
invocation-completed events stop provider waits, and an Agent terminal state
closes every repaired attempt. Gate events distinguish pass from failure in
their symbols instead of marking every completed command with a check mark.
Provider-backed guidance/pause rehearsal, process-crash recovery,
and a secondary-process control client remain in this batch.

### Batch 3E: Model Profiles and Routing

- Extend secret-free configuration to multiple model profiles;
- Add task, phase, capability, and Agent overrides;
- Add deterministic authorized `auto` resolution and explicit switch policy;
- Record resolved routes, reasons, telemetry, and unavailable-price state;
- Preserve strict pinned-model evaluation mode.

**Exit:** routing tests cover precedence, missing capability, unavailable
provider, budget rejection, authorized switch, refused switch, and strict
evaluation behavior; one authorized run uses two planned routes without silent
fallback.

**Implementation note:** configuration schema v8 now owns the secret-free
profiles, attributable price/context metadata, and route policy. Planning resolves and displays an exact assignment
for every Agent, preflight checks every approved model, prompts and response
validation bind the active route, and the runtime records or refuses provider
switches at the controller boundary. Offline tests cover precedence,
capability mismatch, unavailable routes, fallback call-budget rejection,
authorized and refused switching, and strict-mode compatibility. The
provider-backed two-route exit run remains pending.

### Batch 3F: Product Acceptance and Experiment Handoff

- Rehearse fresh installation, Planning dialogue, overview revision, dynamic
  execution, progress, controls, model routing, delivery, and uninstall;
- Record remaining usability and coordination defects;
- Freeze an adaptive-team evaluation configuration;
- Resume fixed-topology comparison, then compare the adaptive team while
  keeping model policy fixed.

**Exit:** a fresh supported device completes the adaptive journey without
internal files or evaluation commands, and the resulting plan, events, model
routes, interventions, Git evidence, quality results, and cleanup are
auditable.

**Implementation note:** the strict single-route ordinary-user subset now has
provider-backed evidence from public installation through Planning, user
approval, dynamic execution, evidence-backed Review, accepted delivery,
independent project checks, and cleanup. A subsequent run supplies
provider-backed foreground guidance and pause/resume evidence but exposed
Review-protocol and generated-lock portability defects; their corrected clean
retry remains pending. The remaining exit evidence also covers a two-route run
and an independent supported device.

## Acceptance Criteria

The adaptive-orchestration milestone is complete only when:

1. A normal product request does not require the user to select a fixed role
   list or team ID.
2. Planning supports both conversation and focused questions with a custom
   answer path.
3. The user sees and can revise requirements, implementation intent, every
   proposed Agent, dependencies, permissions, model routes, and budgets before
   execution.
4. The controller rejects invalid, cyclic, over-budget, over-privileged,
   unauthenticated, or quality-incomplete plans before Agent creation.
5. All product and fixed evaluation teams execute through one TeamPlan-based
   controller path.
6. Interactive input survives validation errors and progress redraw, supports
   custom/revision/cancellation paths, and has a usable non-TTY line mode.
7. Each Agent has an attributable state and safe current-activity summary, and
   compact, standard, and detailed output consume one event stream.
8. Guide, correction, pause, resume, interrupt, and cancellation have tested,
   persisted, user-visible semantics.
9. Cancellation and interruption clean only SAT-owned resources and preserve
   evidence; provider work and cost that cannot be revoked are reported.
10. Multiple model profiles and route precedence work without exposing secrets
   or silently switching models.
11. Strict evaluation mode pins the model and rejects fallback so controlled
    topology comparisons remain reproducible.
12. Approved plan revisions, events, controls, routes, artifacts, and Git facts
    form one integrity-checked report.
13. Reviewer criterion claims are bound to attributable attempt-qualified tool
    records from the same bounded Reviewer chain; fabricated, cross-Agent,
    cross-stage, cross-commit, mismatched, or unavailable evidence cannot pass
    as accepted Review.
14. Offline tests cover success, correction, pause/recovery, interruption,
    cancellation, invalid plans, routing failures, and non-TTY output, followed
    by at least one explicitly authorized provider-backed acceptance run.

## Non-Goals and Boundaries

This milestone does not authorize:

- Peer Agents to spawn or grant permissions to other Agents;
- Unlimited team size, retries, replanning, model switching, or spending;
- Hidden chain-of-thought or raw secret-bearing runtime output in progress UI;
- Automatic merge, deployment, publication, or external communication;
- A second orchestration implementation alongside the current controller;
- Claims that an automatic model selector is objectively intelligent without
  controlled evidence.

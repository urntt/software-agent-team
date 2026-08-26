# Adaptive Orchestration and Interactive Control Specification

This contributor-facing specification defines the planned product contract for
task-defined Agent teams, interactive planning, observable execution, user
controls, and model routing. It is a target design, not a claim about current
implementation. Current behavior and gaps remain authoritative in
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

### Overview Before Execution

The proposal shown before execution contains:

1. The requested outcome and explicit non-goals;
2. Success conditions, constraints, and unresolved assumptions;
3. The implementation approach and major deliverables;
4. The proposed Agents, why each exists, and what each owns;
5. Agent dependencies, expected handoffs, and independent quality coverage;
6. Permission and workspace boundaries in plain language;
7. Model choices or routing preferences, including any authorized automatic
   selection or switching;
8. Time, call, token, and cost limits when known;
9. The delivery destination and expected validation commands.

The default editor supports natural-language revision and structured changes
to requirements, priorities, Agent responsibilities, dependencies, and model
preferences. Raw system prompts, arbitrary tool grants, and direct policy-file
editing remain an advanced contributor surface. Even advanced changes pass the
same controller validation.

Approval freezes version one of the run contract. Later corrections create a
new version; they never mutate an already referenced plan in place.

## Planned Contracts

Names below describe logical versioned contracts. Executable schemas will be
added to the existing artifact layer rather than maintained as a parallel
configuration system.

### `TeamPlan`

A `TeamPlan` binds one confirmed TaskBrief and ImplementationPlan to:

- A stable plan ID and revision;
- Run-scoped `AgentSpec` entries;
- A directed acyclic dependency graph;
- Required handoffs and completion conditions;
- Independent verification and review coverage;
- Aggregate iteration and resource budgets;
- A `ModelRoutePlan`;
- The user approval record and planner proposal evidence.

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
- Acyclic dependencies and at least one terminal delivery path;
- Unique writable ownership or an explicit integration protocol;
- Permission profiles compatible with each responsibility;
- Independent quality coverage;
- Agent, call, iteration, duration, token, and cost limits;
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

Existing fixed team manifests remain versioned evaluation fixtures. During
migration they are compiled into the same `TeamPlan` contract so the repository
does not retain two lifecycle implementations.

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
can expose a command palette; a later secondary `sat` process may submit the
same commands to a running controller. Presentation may evolve, but these
semantics remain stable:

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

1. An explicit per-Agent override approved in the current TeamPlan;
2. A phase or required-capability override;
3. A task or scenario profile;
4. The user default;
5. An authorized deterministic `auto` policy.

`auto` is a controller policy, not an unconstrained model decision. It filters
for available authorized candidates and required capabilities, then ranks them
by the user's quality, latency, and cost preferences. A model may recommend a
route, but the controller resolves and records it.

Before every invocation, SAT records the canonical provider/model, resolution
rule, required capabilities, price source when available, and remaining
budget. Runtime switching is allowed only when the TeamPlan explicitly lists
the candidate and condition, such as provider unavailability or a verified
capability mismatch. The UI announces the switch and its consequence. There is
no silent fallback.

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
13. Offline tests cover success, correction, pause/recovery, interruption,
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

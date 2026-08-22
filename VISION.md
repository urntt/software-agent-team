# Vision: An Experimental Multi-Agent Software Builder

**Status:** Phase 1 complete; Phase 2 comparison paths pending

**Last updated:** August 22, 2026

## Purpose

Build a local-first command-line harness that turns a short software request
into a runnable, tested, and reviewed product by coordinating configurable
teams of AI Agents through OpenClaw.

The intended experience is externally one-shot but internally iterative. After
the user confirms the requirements, the harness may plan, implement, test,
review, and revise within explicit limits before returning one delivery. The
user should not need to supervise individual Agents or repeatedly prompt the
system to correct avoidable defects.

This repository implements both the harness and the experiment needed to
determine which team organization works best.

## Problem

Single coding Agents can produce software quickly, but their first delivery is
often inconsistent. They may silently interpret ambiguous requirements,
approve their own work, claim tests without reproducible evidence, or require
several rounds of human correction.

Calling opaque sub-Agents does not by itself create a software team. Useful
collaboration requires distinct responsibilities, durable handoffs,
independent evidence, bounded revision, and an observable result.

The project hypothesis is:

> A controlled Agent team with explicit handoffs and independent quality
> control can improve first-delivery quality, but the best division of work
> must be established experimentally.

## Product Contract

### Inputs

The product accepts either:

- A brief natural-language request followed by bounded clarification; or
- A pre-confirmed `TaskBrief` for a reproducible run.

A run also receives:

- A clean or seeded Git repository;
- A selected team configuration;
- Allowed tools and sandbox policy;
- Fixed validation commands;
- Time, iteration, Agent-invocation, token, and cost limits.

### Outputs

A completed or failed run produces:

- Code in an isolated, self-contained Git clone;
- Immutable iteration commit references;
- Structured planning, implementation, test, review, and decision artifacts;
- Real command output and exit codes from deterministic quality gates;
- Model, usage, duration, retry, and error telemetry;
- A final machine-readable record and human-readable report;
- An explicit termination reason.

The harness never merges, pushes, deploys, publishes, or intentionally starts
another Agent invocation after its recorded budget is exhausted. Absolute
monetary authorization also requires a provider-side spending or quota limit
because final usage is reported only after a call.

### Primary User

The first user is a developer, AI engineer, researcher, or technical
prototyper who can operate a terminal and inspect a Git result. A graphical
interface is not required for the core product.

### Killer Use Case

The first controlled use case is a small greenfield Web application. The
initial benchmark is a task-management application using Python 3.12,
FastAPI, Jinja2, SQLite, and pytest.

The benchmark is complex enough to require frontend behavior, backend logic,
persistence, validation, tests, and integration, but small enough to repeat
under limited model budgets.

## Current Technical Decisions

These decisions are active implementation constraints. They change only when
code or experiment evidence justifies a replacement.

### Interface and Runtime

- The product interface is a `sat` CLI.
- The supported core runtime is Linux, including WSL when the required tools
  work. Native macOS is not part of the Phase 1 acceptance environment.
- Python 3.12 implements the deterministic control plane.
- OpenClaw 2026.7.1-2 is the initial Agent runtime.
- The execution boundary normalizes OpenClaw's local and Gateway JSON response
  shapes and records split provider/model metadata as one canonical
  `provider/model` identity.
- Open-weight or open-source models are preferred when practical, but model
  providers do not define artifact or team contracts.

### Control Plane

- A deterministic Python controller owns all workflow state.
- No LLM or OpenClaw Agent owns lifecycle transitions.
- Every transition is validated, persisted, and bounded. Persisted recovery is
  integrity checked; automatic interrupted-run resume from the CLI is deferred.
- Git owns source history.
- Persisted artifacts own cross-Agent communication.
- OpenClaw session history is diagnostic state, not a reproducibility
  dependency.
- Role Agents cannot spawn additional model calls. The controller is the sole
  authority for Agent invocation, accounting, and ordering.

### Communication

- Structured asynchronous handoffs are the default communication mechanism.
- A downstream role receives only its required inputs and attributable upstream
  artifacts.
- Direct Agent messages or timed checkpoints are experimental extensions, not
  authoritative state.
- A handoff records the run, team, iteration, source role, target role, status,
  input commit, artifacts, blockers, and summary.

### Isolation and Permissions

- Every run uses an isolated, self-contained Git clone with no remote and a
  detached HEAD.
- Generated code executes only inside a restricted sandbox.
- Live Agent containers run as the invoking unprivileged host identity; UID or
  GID `0` is rejected.
- Clarifier, Planner, Tester, and Reviewer are read-only roles.
- Coding and Integration roles may write only inside the assigned workspace.
- A read-only OpenClaw workspace is mounted at `/agent`; a writable workspace
  is mounted at `/workspace`.
- Controlled roles load no ambient runtime skills, so unrelated skill prompts
  and files cannot enter the benchmark workspace or its quality checks.
- The controller verifies actual commits, diffs, files, commands, and exit
  codes instead of trusting Agent claims.
- Credentials, active runtime state, generated workspaces, and raw runs remain
  outside Git.

## Ownership Boundaries

Each concept has one authoritative owner.

| Concept | Owner |
| --- | --- |
| Product and architecture decisions | `VISION.md` |
| Team membership and initial stage order | `configs/teams.json` |
| Team-manifest validation | `src/software_agent_team/teams.py` |
| Artifact schemas | `src/software_agent_team/artifacts.py` |
| Immutable artifact, handoff, and output persistence | `src/software_agent_team/artifact_store.py` |
| Sanitized Agent runtime boundary | `configs/openclaw.example.json5` |
| Run-scoped runtime materialization and preflight | `src/software_agent_team/runtime_configuration.py` |
| Run lifecycle state and persistence | `src/software_agent_team/run_control.py` |
| Phase 1 orchestration, decisions, and reports | `src/software_agent_team/workflow.py` |
| Agent-call, token, duration, and cost budgets | `src/software_agent_team/budgets.py` and `configs/run-policy.json` |
| Fixed benchmark and quality-gate execution | `benchmarks/task_manager/` and `src/software_agent_team/quality_gates.py` |
| Agent process invocation and telemetry parsing | `src/software_agent_team/execution.py` |
| Role prompts and response validation | `src/software_agent_team/prompting.py` and `src/software_agent_team/responses.py` |
| Source history and iteration snapshots | Git |
| Agent execution and sessions | OpenClaw |
| Cross-Agent communication | Persisted run artifacts |
| Git workspace isolation and snapshot verification | `src/software_agent_team/git_workspace.py` |

Do not maintain parallel role lists, schemas, state machines, or legacy CLI
entry points. A replacement removes or migrates its predecessor in the same
change unless a time-bounded removal plan is documented.

## Experimental Configurations

`configs/teams.json` defines the initial configurations.

### Baseline: `single_agent`

One generalist Agent implements the confirmed task brief once.

- No independent Agent review;
- No review-driven revision;
- The same deterministic acceptance checks still run;
- Maximum one implementation pass.

This measures what the additional team structure must outperform or improve
upon.

### Configuration A: `function_specialized`

The first end-to-end vertical slice uses:

1. Planner;
2. Generalist Developer;
3. Tester and Reviewer independently, parallel by default or serial when the
   provider capacity is one generation;
4. Generalist Developer revision when evidence requires it.

This is the default starting configuration because it separates planning,
implementation, and quality control without introducing code-integration
conflicts. Phase 1 permits one initial implementation and at most one
evidence-driven revision, even though the reusable team definition allows a
higher future limit.

The Tester owns deterministic command evidence and preserves the benchmark's
command-to-criterion assignment. Criteria with a manual component remain
`pending_review` in the Tester's criterion results, but the overall Tester
status is `passed` when all deterministic evidence passes and no blocker exists.
The Reviewer owns that explicit manual-review scope on the same immutable
commit. Only the controller may merge a passing deterministic report and an
accepted independent review into final passed acceptance results.

### Configuration B: `implementation_domain_specialized`

The alternative keeps planning and quality-control policy stable while
splitting implementation:

1. Planner;
2. Frontend Developer and Backend Developer in parallel;
3. Integrator;
4. Tester and Reviewer in parallel;
5. Domain-specific correction and reintegration when evidence requires it.

This configuration tests whether implementation specialization justifies the
additional Agents, handoffs, integration risk, time, and cost.

### Experimental Control

Team organization is the first independent variable. Initial comparisons hold
the following constant wherever possible:

- Confirmed task brief;
- Starting repository commit;
- Model and provider;
- Tool and sandbox policy;
- Acceptance tests and review rubric;
- Aggregate resource limits;
- Randomness controls when available.

Agent count and internal allocation are inherent parts of a team
configuration. Actual cost and duration are reported rather than normalized
away.

Requirement clarification is evaluated after the first topology comparison.
The topology experiment starts from one frozen confirmed `TaskBrief` so input
interpretation does not confound the result.

## Phase 1 Decision Record

| Decision | Reason |
| --- | --- |
| Use a local-first CLI instead of a Web service | The first users can inspect Git and terminal evidence, while local execution keeps credentials, workspaces, and experimental state under their control. |
| Keep the Python controller authoritative | Lifecycle, budgets, evidence checks, and termination must be deterministic rather than dependent on an Agent's self-report. |
| Use OpenClaw as the Agent runtime, not the orchestrator | OpenClaw provides model/provider integration, sessions, tools, and sandboxing; the experiment still needs a model-independent control plane. |
| Start with `function_specialized` | It introduces independent planning, testing, and review without the merge conflicts that would confound the first vertical slice. |
| Keep Tester and Reviewer independent, with configurable dispatch concurrency | They inspect the same immutable evidence and never consume each other's interpretation. Parallel dispatch reduces elapsed time when provider capacity permits; serial dispatch prevents overload without changing the semantic experiment. |
| Deny Agent-spawning tools to every role | Untracked sub-Agent calls bypass controller budgets, attribution, and scheduling, so only the deterministic controller may authorize model invocations. |
| Include bounded command-output tails in verifier context | Exit codes and generic summaries identify failure but not its cause. Bounded untrusted excerpts make diagnosis possible while full write-once output remains authoritative. |
| Use persisted structured handoffs | Durable, attributable artifacts make context and decisions auditable without treating hidden conversation history as state. |
| Run fixed Docker quality gates before Agent judgment | Reproducible command evidence is stronger than claimed test results and keeps generated code isolated from the host. |
| Resolve the sandbox tag to one local image ID per run | Both Agent sandboxes and quality gates execute the same immutable image even if a mutable local tag is later reassigned. |
| Allow one response repair and one implementation revision | A small bounded loop can correct formatting or implementation defects without hiding non-convergence, time, or cost. |
| Separate review severity from terminal failure | Even a critical-impact product defect may be correctable. Reviewer `fail` therefore requires an explicit safety or evidence-integrity termination reason; ordinary gate failures and implementation defects request `revise`. |
| Version requirement or acceptance corrections | A hidden or over-specified acceptance condition confounds model evaluation. The confirmed TaskBrief must expose the product contract, black-box checks must accept equivalent compliant presentations, and a correction starts a new benchmark version. |
| Freeze model identity and prices for each run | Explicit model telemetry and estimated cost are required for comparable experiments; model fallback would change the independent variables. |
| Treat terminal failure as evidence | Provider, sandbox, artifact, budget, and convergence failures must remain observable instead of being retried or discarded silently. |
| Keep saved user defaults secret-free | Model, pricing, concurrency, and timeout improve repeatability, but provider credentials remain in OpenClaw's trusted user state and never enter SAT configuration or exports. |
| Make uninstall preservation-first | Removing the CLI must not silently destroy run evidence, generated workspaces, provider state, shared tools, or a source checkout; export and purge therefore require explicit user choices. |

## Planned Workflow

```text
REQUEST
→ CLARIFY
→ CONFIRM_REQUIREMENTS
→ SELECT_TEAM
→ PREPARE_WORKSPACE
→ PLAN
→ IMPLEMENT
→ SNAPSHOT
→ VERIFY
→ REVIEW
→ DECIDE
   ├── ACCEPT → DELIVER
   ├── REVISE → IMPLEMENT
   └── FAIL → REPORT_FAILURE
```

Only the deterministic controller may advance this state machine.

Phase 1 starts at `CONFIRM_REQUIREMENTS` with a frozen `TaskBrief`; interactive
`REQUEST` and `CLARIFY` behavior is Phase 4. The implemented vertical slice
runs Tester and Reviewer independently after deterministic gates. Dispatch is
concurrent by default and may be serialized for a provider with one generation
slot. It performs at most two implementation iterations: the initial pass and
one revision. Later configurations may use the manifest's higher explicit
limit.

The Reviewer recommends `revise` for every correctable product defect,
including failed deterministic acceptance and security defects in generated
source. Finding severity records product impact and does not independently
authorize terminal failure. A Reviewer `fail` verdict is valid only with an
explicit terminal reason showing that another implementation attempt would
cross a run safety boundary or rely on compromised evidence; the deterministic
controller maps that reason to the final termination category.

The workflow stops earlier when fixed acceptance checks pass, every configured
manual criterion receives independent review, and no blocking review finding
remains. It stops with a report when:

- A resource or iteration limit is reached;
- A required runtime, model, dependency, or sandbox is unavailable;
- An artifact remains invalid after one controlled repair attempt;
- A safety boundary is crossed;
- A revision produces no relevant change;
- The same blocker repeats without measurable progress.

Failure and non-convergence are valid outcomes and must remain visible.

## Artifact Boundary

The artifact layer is the reproducible interface between Agents and the
controller. The current implementation defines:

- `TaskBrief`;
- `HandoffEnvelope`;
- `ArtifactReference`;
- `AgentExecutionRecord`;
- Versioned Agent roles and team definitions;
- `ImplementationPlan`;
- `WorkResult`;
- `TestReport`;
- `ReviewReport`;
- `IterationRecord`;
- `FinalReport`.

`src/software_agent_team/artifacts.py` remains the schema source of truth.
Generated JSON Schema, documentation tables, or transport objects must be
derived from those models.

Phase artifacts use canonical run-relative paths, write-once persistence, and
SHA-256 references. Structural schema validation is followed by contextual
validation against the frozen task brief and selected team before persistence.
The schemas exist independently of live Agent execution; an artifact is not
evidence of a real run until the controller records it from verified inputs.

Transport normalization must remain deterministic. The controller accepts one
unambiguous JSON object, either raw, inside one `json` fence, or surrounded by
presentation prose. It may discard only text containing no other JSON
structures or fences and never guesses between multiple candidates. Duplicate
keys, multiple objects, non-standard constants, and any structural or
contextual schema violation remain invalid.

## Evaluation

### Output Quality

- Acceptance criteria satisfied;
- Automated tests passed and failed;
- Static-analysis findings;
- Blocking and non-blocking review findings;
- Human rubric score for correctness and usability;
- Reproducibility from a clean checkout.

### First-Delivery Effectiveness

- Human corrections after requirements confirmation;
- Internal implementation iterations;
- Earlier findings resolved;
- Critical regressions;
- Completion and convergence rate.

### Reliability and Coordination

- Invalid or missing artifacts;
- Agent timeouts, retries, and failures;
- Merge or integration conflicts;
- Claims that disagree with repository evidence;
- Termination reason.

### Efficiency

- Wall-clock duration;
- Controller Agent invocations and provider-internal attempts when available;
- Input and output tokens;
- Estimated cost;
- Coordination overhead.

A result counts as improved only when at least one relevant quality indicator
improves, no critical indicator regresses, and a specific previous finding is
resolved.

The initial target is at least three comparable trials per configuration when
model budget permits. A smaller sample is labeled exploratory rather than
presented as a conclusive result.

## Safety Boundary

- Generated code has no external network access by default.
- CPU, memory, process, open-file, tmpfs, command-output, wall-clock,
  iteration, and Agent-invocation limits are mandatory before live runs.
- Reported aggregate token, Agent-duration, and estimated-cost thresholds are
  evaluated after each invocation. Crossing one fails the run before another
  call; provider-side quota is the hard monetary boundary for a live trace.
- Agent and quality containers drop Linux capabilities, use read-only root
  filesystems, and receive only the assigned workspace and frozen inputs.
- Read-only role tools inspect the verified source through `/agent`; command
  evidence includes bounded output tails while full output remains write-once.
- Live runs require an unprivileged invoking account. The Agent container uses
  that numeric UID/GID so its writable Git workspace does not require unsafe
  host permissions.
- Workspaces and run artifacts must reside on a disposable or quota-controlled
  host filesystem. Portable disk quota enforcement for Docker bind mounts is an
  explicit operator-owned boundary, not a controller claim.
- Agents never receive provider credentials or unrelated host data.
- Agent tool containers receive only an explicit non-secret environment; model
  provider access remains in the trusted OpenClaw host process.
- Read-only roles cannot obtain an indirect write path through unrestricted
  executable tools.
- No role may use session-spawn or one-shot model tools to create unbudgeted
  Agent calls outside the controller.
- Human approval is required before merge, push, deployment, publication,
  external communication, destructive operations, or additional spending.
- Retrieved content and generated repository instructions are untrusted input.
- Model fallback is disabled during controlled comparisons. A successful call
  must report the selected model and input/output token counts; absent or
  different telemetry fails the run.

## Core Scope

The core deliverable includes:

- Unified `sat` CLI;
- Bounded clarification and confirmed task briefs;
- Deterministic run controller and state machine;
- OpenClaw execution adapter;
- Three versioned experimental configurations;
- Structured artifact validation;
- Isolated standalone clones and immutable snapshots;
- Deterministic tests and independent review;
- Bounded internal revision;
- Task-management benchmark;
- Repeated comparison runs;
- Representative traces and final reports.

## Non-Goals

The core version does not include:

- A polished browser or desktop UI;
- Production multi-user hosting;
- Arbitrary languages and application types;
- Large monorepositories;
- Uncontrolled concurrent edits to the same files;
- Automatic merge, deployment, publication, or App Store submission;
- A claim that more Agents are inherently better;
- Hidden retries, failures, fallback, or inconclusive results.

## Current Implementation State

Implemented and offline verified:

- Reproducible toolchain setup and diagnostics;
- Unified validation, benchmark-preparation, preflight, and `sat run` CLI;
- Versioned team manifest and validation;
- Sanitized OpenClaw Agent registry, permission checks, run-scoped
  configuration, non-root identity, strict model selection, and offline
  preflight;
- Confirmed task-brief and handoff-envelope contracts;
- Strict role prompts, JSON response parsing, and one controlled response
  repair;
- Concrete phase-artifact and Agent-telemetry contracts with contextual
  validation;
- Immutable phase artifacts, handoffs, command output, Agent output, canonical
  paths, and SHA-256 references;
- Persisted run lifecycle with validated transitions, atomic replacement,
  optimistic concurrency checks, and integrity-checked recovery;
- Safe detached standalone-clone creation and chained iteration snapshot
  verification;
- Frozen task-management TaskBrief, deterministic seed commit, content-pinned
  base image, Python dependency lock, per-run immutable local image identity,
  fixed quality-gate manifest, and independent acceptance suite;
- Docker-only production gates with no network, read-only workspace execution,
  non-root identity, fixed commands, resource limits, timeouts, and bounded
  output;
- The complete function-specialized workflow: Planner, Developer, controller
  snapshot, deterministic gates, independent Tester and Reviewer with
  configurable dispatch concurrency, decision, and at most one evidence-driven
  revision;
- Bounded command-output diagnostics for verification, correct read-only
  source visibility, and controller-only Agent invocation policy;
- Explicit deterministic command coverage, `pending_review` manual criteria,
  Reviewer scope attestation, and controller-owned evidence resolution;
- Pre-call Agent invocation limits and post-call token, duration, and
  estimated-cost stop thresholds;
- Explicit completed and failed terminal outcomes with machine-readable and
  human-readable reports;
- One-command Linux/WSL installation for the pinned toolchain, locked project
  environment, checkout-bound CLI launchers, fixed Docker image, and offline
  validation, without taking ownership of OS-level Docker or provider secrets;
- First-launch and repeatable configuration guidance with private, atomic,
  secret-free defaults, plus explicit per-run CLI overrides;
- Guided one-command uninstall with preservation defaults, pre-removal export,
  explicit purge choices, and clear shared-resource boundaries;
- Offline end-to-end coverage for success, revision, response repair, timeout,
  evidence tampering, iteration exhaustion, missing Git changes, missing model
  or token telemetry, and cost exhaustion.

Not yet available or completed:

- Interactive clarification;
- Automatic CLI resume of an interrupted run;
- Executable `single_agent` and `implementation_domain_specialized` workflow
  paths;
- Repeated comparative trials, human rubric scoring, and topology selection;
- A second product benchmark and product-level clarification flow.

Authorized live traces have exercised all four roles, real OpenClaw/provider
calls, controller-verified Git snapshots, Docker quality gates, and the bounded
revision loop. A version-two trace has reached `completed` with all ten
acceptance criteria passed, independent review accepted, complete model and
token telemetry, verified artifact hashes, and clean isolated Git boundaries.
The earlier benchmark defect was corrected and explicitly versioned as
`task_manager_phase1_v2`; version-one traces remain exploratory and must not be
mixed with version-two comparisons. Two consecutive replays of the current
harness commit have now completed through the bounded revision loop. Broader
comparative repetition remains later work. Offline scripted executions prove
controller behavior, not model quality.

## Development Route

### Phase 0: Reproducible Foundation

- Pin Python and OpenClaw;
- Establish repository, secret, and generated-state boundaries;
- Validate team and Agent configuration;
- Define TaskBrief and handoff contracts;
- Provide the foundation CLI and offline checks.

**Exit criterion:** `make check` passes from a clean checkout.

### Phase 1: Function-Specialized Vertical Slice

- Add persisted run directories and lifecycle state;
- Create an isolated standalone clone from a confirmed `TaskBrief`;
- Invoke Planner, Generalist Developer, Tester, and Reviewer through an adapter;
- Run deterministic quality gates;
- Perform at most one revision in the first trace;
- Produce a final report.

**Exit criterion:** one authorized real-model trace reaches `completed` with
reproducible artifacts and a clean controller-verified Git snapshot.

**Current status:** complete. A version-two authorized real-model trace and two
consecutive replays reached `completed`, exercised all four roles, passed the
frozen gates, preserved model and usage telemetry, and satisfied the evidence
boundary in `docs/phase1-runbook.md`.

### Phase 2: Baseline and Domain Specialization

- Add the one-pass single-Agent path;
- Add parallel frontend/backend work with explicit ownership;
- Add deterministic integration before verification;
- Keep shared inputs, quality gates, model policy, and telemetry comparable.

**Exit criterion:** all three configurations complete or fail through the same
controller and reporting boundary.

### Phase 3: Controlled Evaluation

- Freeze the benchmark task brief and starting commit;
- Run repeated trials under predefined budgets;
- Analyze quality, reliability, cost, time, and coordination failures;
- Select a supported default or report an inconclusive result.

**Exit criterion:** the recommendation is traceable to run evidence.

### Phase 4: Product Completion

- Add bounded interactive clarification;
- Validate the selected configuration on a second use case;
- Harden recovery, sandbox policy, and diagnostics;
- Package a demonstration and public-ready technical report.

**Exit criterion:** a user can start from a brief request and receive one
auditable delivery without manually coordinating Agents.

## Decision Policy

Routine implementation choices are owned by the project. When a choice is
uncertain:

1. State the competing options;
2. Identify the smallest relevant experiment;
3. Hold unrelated variables constant;
4. Record the result and trade-offs;
5. Update this document and remove the rejected path.

Architecture changes must preserve deterministic state ownership, explicit
experimental variables, safe execution, reproducible evidence, and a single
source of truth.

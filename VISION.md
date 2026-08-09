# Vision: An Experimental Multi-Agent Software Builder

**Status:** Active

**Last updated:** August 9, 2026

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
- Time, iteration, model-call, token, and cost limits.

### Outputs

A completed or failed run produces:

- Code in an isolated Git worktree;
- Immutable iteration commit references;
- Structured planning, implementation, test, review, and decision artifacts;
- Real command output and exit codes from deterministic quality gates;
- Model, usage, duration, retry, and error telemetry;
- A final machine-readable record and human-readable report;
- An explicit termination reason.

The harness never merges, pushes, deploys, publishes, or spends beyond an
approved budget without human authorization.

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
- The supported core runtime is Linux; macOS and WSL are compatible when the
  required tools work.
- Python 3.12 implements the deterministic control plane.
- OpenClaw 2026.7.1-2 is the initial Agent runtime.
- Open-weight or open-source models are preferred when practical, but model
  providers do not define artifact or team contracts.

### Control Plane

- A deterministic Python controller owns all workflow state.
- No LLM or OpenClaw Agent owns lifecycle transitions.
- Every transition is validated, persisted, bounded, and recoverable.
- Git owns source history.
- Persisted artifacts own cross-Agent communication.
- OpenClaw session history is diagnostic state, not a reproducibility
  dependency.

### Communication

- Structured asynchronous handoffs are the default communication mechanism.
- A downstream role receives only its required inputs and attributable upstream
  artifacts.
- Direct Agent messages or timed checkpoints are experimental extensions, not
  authoritative state.
- A handoff records the run, team, iteration, source role, target role, status,
  input commit, artifacts, blockers, and summary.

### Isolation and Permissions

- Every run uses an isolated Git worktree.
- Generated code executes only inside a restricted sandbox.
- Clarifier, Planner, Tester, and Reviewer are read-only roles.
- Coding and Integration roles may write only inside the assigned workspace.
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
| Sanitized Agent runtime boundary | `configs/openclaw.example.json5` |
| Run lifecycle state | Deterministic controller |
| Source history and iteration snapshots | Git |
| Agent execution and sessions | OpenClaw |
| Cross-Agent communication | Persisted run artifacts |

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
3. Tester and Reviewer in parallel;
4. Generalist Developer revision when evidence requires it.

This is the default starting configuration because it separates planning,
implementation, and quality control without introducing code-integration
conflicts.

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

## Planned Workflow

```text
REQUEST
→ CLARIFY
→ CONFIRM_REQUIREMENTS
→ SELECT_TEAM
→ PREPARE_WORKTREE
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

A multi-Agent run performs at most three implementation iterations by default.
It stops earlier when fixed acceptance checks pass and no blocking review
finding remains. It stops with a report when:

- A resource or iteration limit is reached;
- A required runtime, model, dependency, or sandbox is unavailable;
- An artifact remains invalid after one controlled repair attempt;
- A safety boundary is crossed;
- A revision produces no relevant change;
- The same blocker repeats without measurable progress.

Failure and non-convergence are valid outcomes and must remain visible.

## Artifact Boundary

The artifact layer is the reproducible interface between Agents and the
controller.

The current foundation defines:

- `TaskBrief`;
- `HandoffEnvelope`;
- `ArtifactReference`;
- Versioned Agent roles and team definitions.

The vertical slice will add concrete payload schemas for:

- `ImplementationPlan`;
- `WorkResult`;
- `TestReport`;
- `ReviewReport`;
- `IterationRecord`;
- `FinalReport`.

`src/software_agent_team/artifacts.py` remains the schema source of truth.
Generated JSON Schema, documentation tables, or transport objects must be
derived from those models.

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
- Model calls;
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
- CPU, memory, disk, process, wall-clock, iteration, and model-call limits are
  mandatory before live runs.
- Agents never receive provider credentials or unrelated host data.
- Read-only roles cannot obtain an indirect write path through unrestricted
  executable tools.
- Human approval is required before merge, push, deployment, publication,
  external communication, destructive operations, or additional spending.
- Retrieved content and generated repository instructions are untrusted input.
- Model fallback is recorded and disabled during controlled comparisons unless
  fallback itself is the declared experimental variable.

## Core Scope

The core deliverable includes:

- Unified `sat` CLI;
- Bounded clarification and confirmed task briefs;
- Deterministic run controller and state machine;
- OpenClaw execution adapter;
- Three versioned experimental configurations;
- Structured artifact validation;
- Isolated worktrees and immutable snapshots;
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

Implemented:

- Reproducible toolchain setup and diagnostics;
- Unified validation CLI;
- Versioned team manifest and validation;
- Sanitized OpenClaw Agent registry and permission checks;
- Confirmed task-brief and handoff-envelope contracts;
- Controlled task-management benchmark;
- Offline tests for the foundation.

Not yet implemented:

- Interactive clarification;
- Run controller and persisted state machine;
- Git worktree manager;
- Live OpenClaw adapter;
- Role prompts and response parsing;
- Concrete phase artifact payloads;
- Deterministic benchmark runner;
- Revision synthesis;
- Metrics and final report generation;
- Comparative live runs.

Documentation must describe this distinction accurately. A command or feature
is not documented as available until it executes a real validated path.

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
- Create an isolated worktree from a confirmed `TaskBrief`;
- Invoke Planner, Generalist Developer, Tester, and Reviewer through an adapter;
- Run deterministic quality gates;
- Perform at most one revision in the first trace;
- Produce a final report.

**Exit criterion:** one complete trace reaches a valid terminal state with
reproducible artifacts.

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

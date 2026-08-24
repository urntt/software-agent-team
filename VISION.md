# Vision: An Experimental Multi-Agent Software Builder

**Implementation status:** [`STATUS.md`](STATUS.md)

**Last updated:** August 24, 2026

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

The product surface derives or defaults these internal inputs after user
confirmation. The evaluation surface may provide them explicitly to hold
experimental variables constant.

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

The first product use case is a developer describing a small greenfield
software project in ordinary language and receiving a runnable, tested,
reviewed Git result without manually coordinating Agents. The first execution
profile supports Python 3.12 Web applications, CLI tools, and local automation.

The topology experiment separately uses a frozen task-management Web
application fixture. That fixture is complex enough to exercise frontend
behavior, backend logic, persistence, validation, tests, and integration while
remaining repeatable under limited model budgets. It is evaluation input, not
the application SAT exists to build and not a template for product requests.

### Primary Product Experience

The primary experience is not an operator assembling an evaluation trial from
internal files and flags. A new user installs SAT, enters or creates a project
directory, and runs `sat` with no subcommand. SAT then diagnoses the local
environment, guides first-run provider configuration, asks what the user wants
to build, performs bounded clarification, confirms a requirements summary,
prepares all internal run state, shows controller-backed progress, and returns
a runnable result with exact next commands.

Internal run IDs, TaskBrief JSON, benchmark source paths, team IDs, policy
paths, concurrency, timeouts, repair limits, and evidence roots are advanced
implementation or evaluation concepts. The normal user must not prepare or
edit them.

The complete next-milestone acceptance contract is
[`docs/product-demo-slice.md`](docs/product-demo-slice.md).

## Current Technical Decisions

These decisions are active implementation constraints. They change only when
code or experiment evidence justifies a replacement.

### Interface and Runtime

- The primary product interface is `sat` with no subcommand. Explicit
  subcommands and policy overrides form a separate contributor/operator
  evaluation surface.
- The supported core runtime is Linux, including WSL when the required tools
  work. Native macOS is not part of the Phase 1 acceptance environment.
- Python 3.12 implements the deterministic control plane.
- The first generated-project execution profile is Python 3.12. Product
  requirements and success conditions remain user-owned; the profile limits
  available runtime and deterministic checks, not the task domain.
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
- The controller assembles persisted artifacts. Agents supply semantic content;
  the controller supplies artifact identity, run context, verified Git facts,
  deterministic command evidence, and fixed review scope.
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
| Current implementation, milestone evidence, and known gaps | `STATUS.md` |
| Public overview, commands, and repository map | `README.md` |
| Product Demo Slice interaction and acceptance specification | `docs/product-demo-slice.md` |
| Installation, saved configuration, export, and removal behavior | `docs/installation.md` |
| Runtime, response, persisted-evidence, integrity, and operator-safety reference | `docs/runtime-evidence.md` |
| Controlled Phase 1 provider-backed evaluation procedure | `docs/phase1-runbook.md` |
| Development workflow and repository reference | `docs/development.md` |
| Team membership and initial stage order | `configs/teams.json` |
| Team-manifest validation | `src/software_agent_team/teams.py` |
| Artifact schemas | `src/software_agent_team/artifacts.py` |
| Immutable artifact, handoff, and output persistence | `src/software_agent_team/artifact_store.py` |
| Sanitized Agent runtime boundary | `configs/openclaw.example.json5` |
| Run-scoped runtime materialization and preflight | `src/software_agent_team/runtime_configuration.py` |
| Run lifecycle state and persistence | `src/software_agent_team/run_control.py` |
| Phase 1 orchestration, decisions, and reports | `src/software_agent_team/workflow.py` |
| Agent-call, token, duration, cost, and per-role stage budgets | `src/software_agent_team/budgets.py`, `src/software_agent_team/workflow.py`, `configs/product-policy.json`, and `configs/run-policy.json` |
| Product execution profile and generic quality contract | `profiles/python/`, `runtime/python/`, and `configs/product-policy.json` |
| Frozen evaluation fixture and task-specific acceptance | `benchmarks/task_manager/` and `configs/run-policy.json` |
| Shared quality-manifest validation and execution | `src/software_agent_team/quality_gates.py` |
| Agent process invocation and telemetry parsing | `src/software_agent_team/execution.py` |
| CLI commands and runtime option resolution | `src/software_agent_team/cli.py` |
| Product diagnostics, supported request materialization, and safe delivery | `src/software_agent_team/product.py` |
| Controller-backed progress event rendering | `src/software_agent_team/progress.py` |
| User-local product state path | `src/software_agent_team/paths.py` |
| User-local default schema and persistence | `src/software_agent_team/user_configuration.py` |
| Managed bootstrap, installation, and uninstallation execution | `scripts/bootstrap.sh`, `scripts/install.sh`, and `scripts/uninstall.sh` |
| Role prompt assembly | `src/software_agent_team/prompting.py` |
| Role semantic response validation and field mapping | `src/software_agent_team/responses.py` |
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

The controller owns deterministic command evidence, command-to-criterion
assignment, exit-derived status, and blocker state. The Tester owns analysis of
that evidence and semantic findings. Criteria with a manual component remain
`pending_review` in the controller-assembled `TestReport`, but its overall
status is `passed` when all deterministic evidence passes and no blocker exists.
The Reviewer owns semantic evaluation of the explicit manual-review scope; the
controller binds that scope and immutable commit into the `ReviewReport`. Only
the controller may merge a passing deterministic report and an accepted
independent review into final passed acceptance results.

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

The Product Demo Slice implements bounded clarification before topology
comparison so the primary product journey is executable. Its TaskBrief is
constructed from the user's request, success conditions, and constraints; it
does not inherit the evaluation fixture. The topology
experiment still starts from one frozen confirmed `TaskBrief`; clarification
behavior is not varied during that comparison and therefore does not confound
the result. Clarification quality is evaluated separately.

## Decision Record

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
| Assemble persisted artifacts in the controller | Models should produce planning, implementation summaries, evidence analysis, and review judgment. Known identity, Git, command, status, criterion, and scope facts must come from authoritative controller state instead of requiring an exact model echo. |
| Allow one semantic-response repair and one implementation revision | A small bounded loop can correct genuinely invalid semantic content or implementation defects without hiding non-convergence, time, or cost. Controller-owned fields are ignored and audited rather than repaired. |
| Use per-role stage budgets with a shared repair deadline | Planning, implementation, and verification have different measured workloads. Checked-in defaults are 120 seconds for Clarifier/Planner, 900 for coding/integration roles, and 300 for Tester/Reviewer. The initial response and optional repair share one deadline so repair cannot double the authorized time. |
| Separate review severity from terminal failure | Even a critical-impact product defect may be correctable. Reviewer `fail` therefore requires an explicit safety or evidence-integrity termination reason; ordinary gate failures and implementation defects request `revise`. |
| Version requirement or acceptance corrections | A hidden or over-specified acceptance condition confounds model evaluation. The confirmed TaskBrief must expose the product contract, black-box checks must accept equivalent compliant presentations, and a correction starts a new benchmark version. |
| Freeze model identity and evaluation prices for each run | Explicit model telemetry and a fixed price table are required for comparable experiments; model fallback would change the independent variables. A product run may record cost as unavailable when the user has not supplied a trustworthy price instead of inventing a zero estimate. |
| Treat terminal failure as evidence | Provider, sandbox, artifact, budget, and convergence failures must remain observable instead of being retried or discarded silently. |
| Keep saved user defaults secret-free | The product wizard stores the model reference only. Optional pricing, concurrency, and a global stage-timeout override remain advanced evaluation settings; provider credentials remain in OpenClaw's trusted user state and never enter SAT configuration or exports. Checked-in role defaults remain the normal timeout policy. |
| Make uninstall preservation-first | Removing the CLI must not silently destroy run evidence, generated workspaces, provider state, shared tools, or a source checkout; export and purge therefore require explicit user choices. |
| Implement the Product Demo Slice before topology comparison | A reproducible engine is not yet a usable product. The core promise starts from a short request, so installation, onboarding, clarification, progress, and delivery must be executable before the project presents an internal evaluation workflow as its demo. |
| Keep product and evaluation CLI surfaces distinct | Normal users run `sat` and receive guided defaults. Explicit TaskBrief files, benchmark preparation, team IDs, policy paths, timeouts, concurrency, and repair controls remain available to contributors without becoming first-run questions. |
| Keep generated-product profiles independent from evaluation fixtures | A benchmark must hold experiment inputs constant, while a product request must express the user's intent. Sharing the controller and quality-manifest schema is useful; sharing a task-specific TaskBrief, seed, acceptance suite, environment contract, or delivery command would silently replace the user's request and invalidate both boundaries. |
| Derive product requirements before the first model call | The current bounded wizard records the user's request, success conditions, and constraints directly, shows the resulting TaskBrief summary, and requires authorization. This avoids charging for a model-authored interpretation before consent and prevents an LLM from becoming the authority for missing user intent. |
| Require a generated-project command manifest | Setup, start, and test commands vary by project. A validated `sat-project.json` argv contract lets SAT deliver exact commands without assuming FastAPI, a Web server, or any task-specific entry point. |
| Show controller-backed progress rather than hidden reasoning | Users need attributable phase summaries, elapsed waiting time, Git snapshots, gates, review, and revision status. Hidden chain-of-thought, secrets, and unverifiable percentages are neither required nor appropriate. |
| Keep product state outside the application checkout | Managed runs, workspaces, and trusted source baselines live under the user-local state root. Installation updates cannot overwrite evidence, and uninstallation can preserve, export, or explicitly purge state independently of the application files. |
| Deliver only an accepted result to a new child directory | The model works in an isolated detached clone. After acceptance, SAT copies the exact accepted commit through a same-parent staging directory and publishes it with Linux atomic no-replace semantics, so a failed run, late destination, or conflict never overwrites user files. |

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

Phase 1 starts at `CONFIRM_REQUIREMENTS` with a frozen `TaskBrief`. The Product
Demo Slice connects `REQUEST`, bounded scope clarification, first-run
onboarding, automatic internal materialization, progress, and delivery to that
verified engine. The
implemented vertical slice runs Tester and Reviewer independently after
deterministic gates. Dispatch is concurrent by default and may be serialized
for a provider with one generation slot. It performs at most two implementation
iterations: the initial pass and one revision. Later configurations may use the
manifest's higher explicit limit.

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
- An Agent's semantic response remains invalid after one controlled repair
  attempt within the original role-stage deadline;
- A safety boundary is crossed;
- A revision produces no relevant change;
- The same blocker repeats without measurable progress.

Failure and non-convergence are valid outcomes and must remain visible.

## Runtime Realization

The artifact layer remains the reproducible interface between Agents and the
controller. Persisted artifact schemas, smaller role-response bodies,
controller-owned field assembly, canonical paths, write-once storage, SHA-256
references, contextual validation, and deterministic transport normalization
must realize the control-plane decisions above without creating a second
lifecycle authority.

The complete implemented artifact, response, evidence, integrity, recovery,
and failure semantics are maintained in
[`docs/runtime-evidence.md`](docs/runtime-evidence.md). Executable schemas and
policies retain the code owners listed in
[`Ownership Boundaries`](#ownership-boundaries).

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

## Safety Requirement

Safe execution is a product constraint, not an optional deployment concern.
The implementation must preserve sandbox isolation, least-privilege role
tools, explicit non-secret environments, immutable model identity, bounded
resources and cost, integrity-checked evidence, and human authorization for
external side effects.

The concrete Git, sandbox, credential, model, resource, storage, and human
authorization boundaries are maintained in
[`docs/runtime-evidence.md`](docs/runtime-evidence.md). The qualifying
operator checklist is maintained in
[`docs/phase1-runbook.md`](docs/phase1-runbook.md).

## Core Scope

The core deliverable includes:

- Unified `sat` CLI;
- One-command managed installation with automatic environment diagnostics;
- A no-subcommand product entry point with guided first-run configuration;
- Bounded clarification and confirmed task briefs;
- Automatic internal run, TaskBrief, source, workspace, and delivery
  materialization;
- Controller-backed progress summaries and a concise final delivery view;
- Deterministic run controller and state machine;
- OpenClaw execution adapter;
- Three versioned experimental configurations;
- Structured artifact validation;
- Isolated standalone clones and immutable snapshots;
- Deterministic tests and independent review;
- Bounded internal revision;
- A controlled evaluation fixture, currently the task-management benchmark;
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

## Implementation Status

Current implementation, provider-backed evaluation evidence, known gaps, and
the next executable milestone are maintained in [`STATUS.md`](STATUS.md).
Keeping those time-sensitive facts separate prevents completed work from being
confused with the durable product and experiment decisions in this document.

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
- Perform at most one revision in the first evaluation trial;
- Produce a final report.

**Exit criterion:** one authorized provider-backed evaluation reaches
`completed` with reproducible artifacts and a clean controller-verified Git
snapshot.

**Current status:** complete. See [`STATUS.md`](STATUS.md) for evidence and
[`docs/phase1-runbook.md`](docs/phase1-runbook.md) for the contributor/operator
acceptance boundary.

### Phase 2: Product Demo Slice

- Install SAT into a managed user-local location with one command;
- Diagnose supported environment conditions automatically and actionably;
- Make `sat` the guided product entry point;
- Detect or configure a provider without storing secrets in SAT state;
- Ask what the user wants to build, record success conditions and constraints
  within the installed execution profile, and confirm a concise requirements
  summary;
- Generate internal run IDs, TaskBriefs, sources, workspaces, and destinations
  automatically;
- Show controller-backed progress, review, revision, and failure summaries;
- Deliver a clean runnable result with exact next commands.

**Exit criterion:** the journey in
[`docs/product-demo-slice.md`](docs/product-demo-slice.md) passes its offline
interaction tests and one fresh supported-device rehearsal without requiring
the user to operate the evaluation CLI.

**Current status:** implemented and offline tested; the fresh-device,
provider-backed rehearsal remains required before Phase 2 is complete.

### Phase 3: Baseline and Domain Specialization

- Add the one-pass single-Agent path;
- Add parallel frontend/backend work with explicit ownership;
- Add deterministic integration before verification;
- Keep shared inputs, quality gates, model policy, and telemetry comparable.

**Exit criterion:** all three configurations complete or fail through the same
controller and reporting boundary.

### Phase 4: Controlled Evaluation

- Freeze the benchmark task brief and starting commit;
- Run repeated trials under predefined budgets;
- Analyze quality, reliability, cost, time, and coordination failures;
- Select a supported default or report an inconclusive result.

**Exit criterion:** the recommendation is traceable to run evidence.

### Phase 5: Generalization and Pass-Off

- Validate the selected configuration on a second use case;
- Harden interrupted-run recovery and remaining sandbox diagnostics;
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

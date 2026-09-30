# Vision: An Experimental Multi-Agent Software Builder

**Implementation status:** [`STATUS.md`](STATUS.md)

**Last updated:** September 11, 2026

## Purpose

Build a local-first command-line harness that turns a short software request
into a runnable, tested, and reviewed product by coordinating configurable
teams of AI Agents through OpenClaw.

The intended experience is externally one delivery but internally iterative.
The user should not need to supervise individual Agents by default, while still
being able to inspect, guide, correct, pause, interrupt, or cancel a long run.
Before execution, the harness proposes a task-specific implementation, Agent,
and model plan for user approval. It may then implement, test, review, and
revise within explicit limits before returning one delivery.

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
- A confirmed `TaskBrief`, including its approved `ProductDefinition`, and a
  user-approved `TeamPlan`;
- Run-scoped `AgentSpec` entries and a `ModelRoutePlan`;
- Allowed tools and sandbox policy;
- Fixed validation commands;
- A frozen model-capability and price snapshot, one user-approved task-wide USD
  budget, an optional user deadline, and controlled-evaluation limits when that
  surface deliberately freezes experimental variables.

The product surface derives or defaults these internal inputs after user
confirmation. The evaluation surface may provide them explicitly to hold
experimental variables constant.

### Outputs

A completed or failed run produces:

- Code in an isolated, self-contained Git clone;
- Immutable iteration commit references;
- Structured planning, implementation, test, review, and decision artifacts;
- The approved team/model plan, its revisions, and Agent-creation records;
- An ordered progress and user-control event record;
- Real command output and exit codes from deterministic quality gates and
  bounded attributable Agent tool evidence;
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
to build, and conducts a bounded multi-round Planning dialogue. Questions may
use normal conversation or suggested choices with a custom answer path. The
interface distinguishes a Planner-selected task suggestion from a question
required by Controller validation, and each focused choice is bound to the
exact decision fields it may change.

Before execution, SAT shows one editable overview that begins with target
users, the killer workflow, delivery maturity, quality expectations, non-goals,
and their architecture, team, cost, and delivery effects, then shows detailed
requirements, implementation intent, task-defined Agents, dependencies,
permissions, budgets, and model routes. Planning and execution share a persistent
editable control area with phase-appropriate commands, hints, and completion.
Visibility applies to the whole journey: compact retains the readable checkpoint
view, standard adds task-wide telemetry and work milestones, and detailed adds
bounded visible tool and model previews without granting them authority.
Fixed Controller policy remains
available through a lossless display toggle instead of dominating the default
task-specific view. After approval, the controller validates
and creates the run-scoped team. During execution, SAT shows configurable run-level and
per-Agent progress and accepts user guidance, correction, pause, resume,
interrupt, or cancellation. It returns a runnable result or an honest terminal
report with exact next commands.

Internal run IDs, TaskBrief JSON, benchmark source paths, team IDs, policy
paths, concurrency, timeouts, controlled correction caps, and evidence roots are advanced
implementation or evaluation concepts. The normal user must not prepare or
edit them.

The guided product-journey acceptance specification is
[`docs/product-demo-slice.md`](docs/product-demo-slice.md).
The next adaptive interaction and orchestration milestone is specified in
[`docs/adaptive-orchestration.md`](docs/adaptive-orchestration.md).

## Technical Direction and Compatibility Constraints

These are durable architecture choices. The topic documents own the precise
behavior and compatibility rules; [`STATUS.md`](STATUS.md) says which paths have
current implementation and evidence.

### Interface and Runtime

- `sat` without a subcommand is the normal guided product interface. Explicit
  commands and policy overrides serve contributors and evaluators.
- Linux, including WSL with working prerequisites, is the supported host. A
  Python 3.12 Controller uses a pinned, SAT-private OpenClaw runtime and a
  restricted local Docker sandbox. The first generated-project profile is
  Python 3.12. See the [installation](docs/installation.md) and
  [product-profile](profiles/python/README.md) contracts.
- Stable installs resolve published releases; dev follows an explicitly chosen
  channel or revision. SemVer, complete source revision, artifact digest, and
  managed provenance remain distinct. Install, update, rollback, and uninstall
  use one ownership-bound lifecycle; `sat uninstall` is the canonical removal
  command. See [releases](docs/releases.md) and
  [installation](docs/installation.md).

### Control Plane

- The deterministic Controller owns lifecycle, Agent creation, authority,
  scheduling, budget settlement, approval, and terminal results. Agents provide
  bounded semantic content; they do not create Agents or advance state.
- User product decisions and material changes require attributable input or
  approval. Planning may recommend but cannot silently decide for the user.
  A task-defined team uses Controller-owned specialization and capability
  contracts, with independent downstream quality judgment.
- Material transitions, submissions, corrections, handoffs, Git facts, and
  costs have bounded, integrity-checked evidence. Corrections change only
  typed model-owned fields; provider or transport failure is not a semantic
  retry. See [adaptive orchestration](docs/adaptive-orchestration.md) and
  [runtime evidence](docs/runtime-evidence.md).

### Communication

- Persisted structured handoffs, scoped artifacts, and the approved TeamPlan
  carry cross-Agent work. Hidden conversation history and direct Agent messages
  are not authoritative state.
- One Controller event stream projects progress to configurable terminal views;
  the persisted control channel accepts guidance, correction, pause/resume,
  interruption, and cancellation at defined safe boundaries. See
  [adaptive orchestration](docs/adaptive-orchestration.md).

### Isolation and Permissions

- The Controller creates only approved Agents with catalog-bound tools,
  least-privilege workspace scopes, explicit model routes, and a sandboxed
  execution profile. Generated repositories and their instructions are
  untrusted. Deterministic commands run in the restricted sandbox before Agent
  judgment; writers cannot be their own sole acceptance authority.
- SAT's runtime, credentials, sessions, workspaces, and user state are isolated
  from other OpenClaw installations. Exact cleanup is limited to attributable
  SAT-owned resources. See [installation](docs/installation.md) and
  [runtime evidence](docs/runtime-evidence.md).

## Ownership Boundaries

Each concept has one authoritative owner.

| Concept | Owner |
| --- | --- |
| Product and architecture decisions | `VISION.md` |
| Current implementation, milestone evidence, and known gaps | `STATUS.md` |
| User-facing public overview and quick start | `README.md` |
| Guided product-journey interaction and acceptance specification | `docs/product-demo-slice.md` |
| Adaptive Planning, task-defined teams, progress visibility, user controls, and model-routing acceptance design | `docs/adaptive-orchestration.md` |
| Installation, saved configuration, export, and removal behavior | `docs/installation.md` |
| Terminal display, telemetry limits, keyboard editing, and phase controls | `docs/terminal-interface.md` |
| Runtime, response, persisted-evidence, integrity, and operator-safety reference | `docs/runtime-evidence.md` |
| Controlled Phase 1 provider-backed evaluation procedure | `docs/phase1-runbook.md` |
| Development workflow and repository reference | `docs/development.md` |
| Versioned fixed evaluation topology fixtures | `configs/teams.json` |
| Run-scoped TeamPlan, AgentSpec, ModelRoutePlan, validation, and fixed-manifest compatibility compilation | `src/software_agent_team/teams.py` |
| Adaptive Planning dialogue, semantic proposal compilation, validation, overview, approval, and write-once evidence | `src/software_agent_team/planning.py` |
| Pure pre-schema Planning envelope, stable-ID presentation, and path normalization | `src/software_agent_team/planning_normalization.py` |
| Task-admission and plan-execution self-check schema, dependency freshness, rendering, and write-once evidence | `src/software_agent_team/self_check.py` |
| Provider subprocess identity leases and orphan recovery | `src/software_agent_team/process_lifecycle.py` |
| ProductDefinition, TaskBrief, phase, and handoff artifact schemas | `src/software_agent_team/artifacts.py` |
| Canonical persisted-model integrity digest | `src/software_agent_team/integrity.py` |
| Immutable artifact, handoff, and output persistence | `src/software_agent_team/artifact_store.py` |
| Controller binding of Agent semantics to verified runtime facts | `src/software_agent_team/assembly.py` |
| Sanitized Agent runtime boundary | `configs/openclaw.example.json5` |
| SAT-owned OpenClaw process-environment isolation | `src/software_agent_team/openclaw_runtime.py` and `scripts/openclaw-environment.sh` |
| Frozen secret-free provider/model transport profile and reviewed presets | `src/software_agent_team/model_runtime.py` |
| Run-scoped runtime materialization and preflight | `src/software_agent_team/runtime_configuration.py` |
| Run lifecycle state and persistence | `src/software_agent_team/run_control.py` |
| Fixed-fixture compatibility orchestration and decisions | `src/software_agent_team/workflow.py` |
| Approved TeamPlan dependency and shared-workspace scheduling | `src/software_agent_team/scheduling.py` |
| Scheduler-approved dynamic Agent invocation, Git/quality enforcement, and durable handoffs | `src/software_agent_team/dynamic_runner.py` |
| Approved adaptive lifecycle convergence, revision feedback, decisions, and terminal outcomes | `src/software_agent_team/dynamic_workflow.py` |
| Shared human-readable terminal report rendering | `src/software_agent_team/reporting.py` |
| Versioned RunEvent contract, append-only journal, visibility filtering, and terminal rendering | `src/software_agent_team/progress.py` |
| Versioned ControlCommand contract and controller-owned mailbox history | `src/software_agent_team/controls.py` |
| Task-wide USD authority and shared model-usage ledger | `src/software_agent_team/budgets.py`, task resource authorization, Planning, and dynamic runtime |
| Controlled-evaluation call, token, duration, cost, and fixed-role limits | `configs/run-policy.json` and `src/software_agent_team/budgets.py` |
| Controller binding of each invocation to budget usage, raw outputs, and telemetry evidence | `src/software_agent_team/invocation.py` |
| Product execution profile and generic quality contract | `profiles/python/`, `runtime/python/`, and `configs/product-policy.json` |
| Frozen evaluation fixture and task-specific acceptance | `benchmarks/task_manager/` and `configs/run-policy.json` |
| Shared quality-manifest validation and execution | `src/software_agent_team/quality_gates.py` |
| Agent process invocation and telemetry parsing | `src/software_agent_team/execution.py` |
| Pinned OpenClaw current-turn tool-evidence extraction and sanitization | `src/software_agent_team/openclaw_session_evidence.py` |
| CLI commands, guided request and Planning activation, and runtime option resolution | `src/software_agent_team/cli.py` |
| Product diagnostics, trusted source preparation, and safe delivery | `src/software_agent_team/product.py` |
| User-local product state path | `src/software_agent_team/paths.py` |
| User-local default schema and persistence | `src/software_agent_team/user_configuration.py` |
| Generated-project command and documentation validation | `profiles/python/validation/run.py` and `profiles/python/validation/run_commands.py` |
| Managed bootstrap, installation, and uninstallation execution | `scripts/bootstrap.sh`, `scripts/install.sh`, and `scripts/uninstall.sh` |
| Fixed-role and task-defined capability prompt assembly | `src/software_agent_team/prompting.py` |
| Fixed-role and run-scoped Agent semantic response validation and field mapping | `src/software_agent_team/responses.py` |
| Typed response diagnostics, deterministic normalization, and field-targeted correction | `src/software_agent_team/response_corrections.py` |
| Source history and iteration snapshots | Git |
| Agent execution and sessions | OpenClaw |
| Cross-Agent communication | Persisted run artifacts |
| Git workspace isolation and snapshot verification | `src/software_agent_team/git_workspace.py` |

Do not maintain parallel role lists, schemas, state machines, or legacy CLI
entry points. A replacement removes or migrates its predecessor in the same
change unless a time-bounded removal plan is documented.

## Experimental Configurations

`configs/teams.json` defines fixed, versioned evaluation fixtures. These
configurations remain useful for controlled comparison, but they are not the
long-term product requirement that a normal user choose a predefined role
list.

### Product Default: Task-Defined Team

The product flow uses a bootstrap Planning session to propose a
run-scoped TeamPlan from the confirmed task. The user reviews and may revise
the plan; the deterministic controller validates and creates its Agents. The
number, names, and approved catalog specializations of execution roles therefore
vary with the work. Permission profiles, catalog compatibility, quality
independence, budgets, evidence, and lifecycle authority remain fixed system
boundaries; role labels never grant them.

The detailed contract and migration path are defined in
[`docs/adaptive-orchestration.md`](docs/adaptive-orchestration.md).

### Baseline: `single_agent`

One generalist Agent implements the confirmed task brief once.

- No independent Agent review;
- No review-driven revision;
- The same deterministic acceptance checks still run;
- Maximum one implementation pass.

This measures what the additional team structure must outperform or improve
upon.
The executable evaluation boundary and comparable fixture procedure are in
[`docs/fixed-comparison.md`](docs/fixed-comparison.md).

### Configuration A: `function_specialized`

The first end-to-end vertical slice uses:

1. Planner;
2. Generalist Developer;
3. Tester first as the deterministic quality checkpoint, then Reviewer after
   that checkpoint passes;
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
4. Tester first, then Reviewer after the deterministic checkpoint passes;
5. Domain-specific correction and reintegration when evidence requires it.

This configuration tests whether implementation specialization justifies the
additional Agents, handoffs, integration risk, time, and cost.

### Experimental Control

Fixed team organization remains the first independent variable. Initial
comparisons hold the following constant wherever possible:

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

After the fixed comparison establishes a baseline, a frozen task-generated
TeamPlan can be added as another configuration while model policy remains
constant. Model routing is evaluated separately by holding both the TaskBrief
and TeamPlan constant. Dynamic team formation and multi-model routing must not
change in the same controlled trial.

The one-writer baseline remains an explicit evaluation configuration. Do not
change the ordinary user's team default or claim that a team improves first
delivery from a two-request, one-model feasibility comparison. The next
topology decision requires repeated paired requests across more task types
and model sizes, with external quality scoring and terminal delivery status
reported separately. Add a task-defined team only after its approved plan can
be frozen without changing the TaskBrief or model policy.

## Decision Record

The titles below are stable decision identities. Each row keeps the durable
reason and links to the one topic document that owns its detailed contract.
The exact pre-consolidation rationale is retained in the
[decision migration map](docs/history/vision-2026-09-28.md); it is historical
context, not a second current specification.

| Decision | Durable reason and detailed owner |
| --- | --- |
| Regenerate a structurally broken complete Planning proposal once | One bounded regeneration can restore identities when independent schema errors make field correction unsafe. [Contract](docs/adaptive-orchestration.md) |
| Frame complete bare Planning bodies in the Controller | A typed submission can preserve every question or proposal field yet omit only the semantic response envelope. [Contract](docs/adaptive-orchestration.md) |
| Return missing user authority to dialogue rather than proposal repair | A model cannot fill a decision that belongs to the user. [Contract](docs/adaptive-orchestration.md) |
| Recover evidence selection only through verified, shrinking authority | Invalid model choices are not Controller faults. [Contract](docs/adaptive-orchestration.md) |
| Use a local-first CLI instead of a Web service | The first users can inspect Git and terminal evidence, while local execution keeps credentials, workspaces, and experimental state under their control. [Contract](docs/product-demo-slice.md) |
| Keep the Python controller authoritative | Lifecycle, budgets, evidence checks, and termination must be deterministic rather than dependent on an Agent's self-report. [Contract](docs/adaptive-orchestration.md) |
| Derive execution roles from the task, then let the controller create them | A fixed bootstrap Planning capability can propose a TeamPlan after dialogue, but it cannot spawn Agents. [Contract](docs/adaptive-orchestration.md) |
| Separate task-specific role identity from executable capability | A free-form label is necessary to explain why one Agent exists for this task, but it cannot safely define tools, permissions, output, or acceptance authority. [Contract](docs/adaptive-orchestration.md) |
| Derive specialist coverage from typed acceptance structure | Planner prose and role labels cannot reliably decide whether generic Review is sufficient. [Contract](docs/adaptive-orchestration.md) |
| Require independent quality coverage without imposing a permanent Tester/Reviewer pair | Every writer needs downstream read-only quality judgment; cohesive tasks need not create extra fixed roles. [Contract](docs/adaptive-orchestration.md) |
| Let the approved Agent DAG express quality handoffs inside a controller-owned quality checkpoint | Independence means a writer cannot be its own sole acceptance authority. [Contract](docs/adaptive-orchestration.md) |
| Admit Planning questions through a deterministic responsibility matrix | A deterministic responsibility matrix separates user-owned decisions from Planner recommendations and Agent autonomy. [Contract](docs/adaptive-orchestration.md) |
| Require an attributable ProductDefinition before team design | A feature list does not establish who will use the result, its killer workflow, or whether the user expects a throwaway prototype, a reusable local product, or a releasable small product. [Contract](docs/adaptive-orchestration.md) |
| Bind each ProductDefinition question to one declared decision bundle | A free-text answer authorizes only the ProductDefinition dimensions visibly declared by its question, not every nearby missing field. [Contract](docs/adaptive-orchestration.md) |
| Require a deterministic Planning clarity gate before approval | A syntactically executable plan may still conceal scope or responsibility. [Contract](docs/adaptive-orchestration.md) |
| Define ordinary-user evidence by the product boundary, not operator identity | A project owner, contributor, Codex, or another test operator may all supply user input through the managed bare-`sat` interface. [Contract](docs/product-demo-slice.md) |
| Show one editable plan overview before execution | Requirements, implementation intent, Agent responsibilities, dependencies, permissions, budgets, and model routes affect quality and cost. [Contract](docs/adaptive-orchestration.md) |
| Use OpenClaw as the Agent runtime, not the orchestrator | OpenClaw provides model/provider integration, sessions, tools, and sandboxing; the experiment still needs a model-independent control plane. [Contract](docs/runtime-evidence.md) |
| Isolate SAT's OpenClaw runtime and state from every existing installation | Compatibility is not ownership. [Contract](docs/runtime-evidence.md) |
| Separate release identity from source provenance | SemVer tells users whether an update changes the supported product contract; a complete source revision and artifact digest make the installed bytes auditable. [Contract](docs/releases.md) |
| Make stable publication release-first and keep dev explicit | Normal users need one reproducible version rather than whatever `main` contains at install time. [Contract](docs/releases.md) |
| Use one ownership-bound transaction for install, update, switch, and uninstall | Separate lifecycle paths drift on launchers, state compatibility, rollback, and custom destinations. [Contract](docs/installation.md) |
| Bind a managed installation to one local Docker daemon | An ambient Docker context can change between install, startup, execution, and cleanup, while rootless UID mapping differs from rootful mapping. [Contract](docs/installation.md) |
| Bound managed rollback storage and attribute sandbox-image cleanup | One direct predecessor is sufficient for application rollback; retaining every historical private runtime eventually prevents the next task from starting. [Contract](docs/installation.md) |
| Let the verified candidate interpret persisted state before activation | An older updater cannot know a newer candidate's state layout or lifecycle enum and must not decide compatibility or persisted-run liveness with its own scanner. [Contract](docs/installation.md) |
| Coordinate foreground tasks and managed activation with one kernel lifecycle lease | Checking `run.json` and then swapping a link leaves a race during pre-run Planning and admission. [Contract](docs/installation.md) |
| Check for updates only when foreground product use creates the need | SAT is an on-demand CLI, not a service. [Contract](docs/releases.md) |
| Change SemVer at release-scope freeze, not on every commit | Dev provenance already identifies every commit. [Contract](docs/releases.md) |
| Compile every model route from one frozen transport-neutral profile | A route is a frozen transport profile, not only a provider/model string; it binds endpoint, catalog, price, context, and safety. [Contract](docs/adaptive-orchestration.md) |
| Keep local readiness timeouts separate from Agent invocation timeouts | A cold local catalog process can take longer than a lightweight binary or Docker check without consuming provider tokens. [Contract](docs/runtime-evidence.md) |
| Separate user deadlines from initialization, provider liveness, response finalization, and process cleanup | An ordinary task has no whole-run deadline unless the user explicitly sets one before the first model call. [Contract](docs/runtime-evidence.md) |
| Bind initialization progress to the current invocation | A reused OpenClaw session may already contain a directory, index, binding, transcript header, or an identical historical prompt. [Contract](docs/runtime-evidence.md) |
| Bind provider subprocess cleanup to durable kernel identity | An in-memory process map disappears when the foreground controller crashes, while PID alone can later identify an unrelated process. [Contract](docs/runtime-evidence.md) |
| Start with `function_specialized` | It introduces independent planning, testing, and review without the merge conflicts that would confound the first vertical slice. [Contract](docs/phase1-runbook.md) |
| Keep Tester and Reviewer independent while ordering the deterministic checkpoint first | Tester findings and Reviewer judgment remain separately attributable. [Contract](docs/phase1-runbook.md) |
| Deny Agent-spawning tools to every execution Agent | Untracked sub-Agent calls bypass controller budgets, attribution, and scheduling, so only the deterministic controller may authorize model invocations. [Contract](docs/runtime-evidence.md) |
| Include bounded command-output tails in verifier context | Exit codes and generic summaries identify failure but not its cause. [Contract](docs/runtime-evidence.md) |
| Return failed deterministic evidence to revision writers before Review | A failed generated-project command is implementation evidence, not a reason to spend an independent Review call. [Contract](docs/adaptive-orchestration.md) |
| Attribute `exec` evidence from the minimum safe command prefix | SAT persists the first executable, a digest of the complete canonical arguments, and the actual result; it does not persist the shell command. [Contract](docs/runtime-evidence.md) |
| Use persisted structured handoffs | Durable, attributable artifacts make context and decisions auditable without treating hidden conversation history as state. [Contract](docs/runtime-evidence.md) |
| Run fixed Docker quality gates before Agent judgment | Reproducible command evidence is stronger than claimed test results and keeps generated code isolated from the host. [Contract](docs/runtime-evidence.md) |
| Resolve the sandbox tag to one local image ID per run | Both Agent sandboxes and quality gates execute the same immutable image even if a mutable local tag is later reassigned. [Contract](docs/runtime-evidence.md) |
| Assemble persisted artifacts in the controller | Models should produce planning, implementation summaries, evidence analysis, and review judgment. [Contract](docs/runtime-evidence.md) |
| Recover only verifiable upstream-incomplete tool loops | An incomplete tool loop is resumable only with verifiable attribution and without treating uncommitted work as success. [Contract](docs/runtime-evidence.md) |
| Preserve valid nonterminal async tool evidence without treating it as success | A pinned OpenClaw `exec` or `process` result may legitimately return a `running` process handle before a later poll reaches a terminal state. [Contract](docs/runtime-evidence.md) |
| Recover completed semantics across wrapper-finalization failure only from durable bound evidence | OpenClaw result-envelope serialization is not the semantic authority when the invocation has already written one valid bound submission and a complete terminal record to SAT's isolated state. [Contract](docs/runtime-evidence.md) |
| Use invocation-bound typed submission for Planning and dynamic Agent semantics | A model that already completed valid work can still wrap, truncate, duplicate, or corrupt a JSON-shaped assistant message. [Contract](docs/runtime-evidence.md) |
| Bind executed submission arguments through the plugin receipt | OpenClaw records model-authored tool arguments and separately delivers validated arguments to the tool; a runtime may normalize the latter without rewriting the immutable session turn. [Contract](docs/runtime-evidence.md) |
| Keep profile criterion definitions controller-owned while preserving model-owned relationships | An execution profile owns its fixed acceptance text and verification contract, so model-authored duplicates or rewrites have no authority. [Contract](docs/adaptive-orchestration.md) |
| Allow task-defined work for every runtime Agent while keeping `AgentSpec` authoritative | A task is useful semantic intent for an implementation, integration, testing, or Review Agent; deleting quality work loses information the user approved. [Contract](docs/adaptive-orchestration.md) |
| Require criterion-by-criterion adversarial Review evidence | A summary verdict can skip a boundary while still claiming complete coverage. [Contract](docs/adaptive-orchestration.md) |
| Define Review boundary identifiers once in the controller | Boundary names are protocol values, not informal depth labels. [Contract](docs/adaptive-orchestration.md) |
| Give Reviewer the minimum coherent probe capability | Source remains read-only and network, background processes, project edits, general write tools, and Agent spawning remain denied. [Contract](docs/runtime-evidence.md) |
| Treat contract-ineligible JSON arrays as presentation on fixed-role text-compatibility calls | Legacy fixed-role compatibility responses must contain one top-level object. [Contract](docs/runtime-evidence.md) |
| Separate transport payload count from semantic object count on text-compatibility calls | OpenClaw may emit a valid semantic response and an ancillary visible tool diagnostic as separate payloads. [Contract](docs/runtime-evidence.md) |
| Compile semantic responses and correct only typed model-owned fields | Transport failures are not semantic retries. [Contract](docs/runtime-evidence.md) |
| Return missing user authority to dialogue and settle Planning state consistently | A rejected proposal cannot model-correct a material decision that only the user may make. [Contract](docs/adaptive-orchestration.md) |
| Keep product resource authority to one task-wide USD budget plus an optional deadline | Ordinary work uses one user-approved USD ceiling and an optional deadline; call and token caps belong to controlled evaluation. [Contract](docs/adaptive-orchestration.md) |
| Separate review severity from terminal failure | Even a critical-impact product defect may be correctable. [Contract](docs/adaptive-orchestration.md) |
| Version requirement or acceptance corrections | A hidden or over-specified acceptance condition confounds model evaluation. [Contract](docs/adaptive-orchestration.md) |
| Separate product model routing from strict evaluation routing | Controlled evaluations pin one model and price table and disable switching. [Contract](docs/phase1-runbook.md) |
| Freeze attributable model metadata before task admission | Setup and route changes show discovered input/output prices, their source, and context capacity and allow correction. [Contract](docs/adaptive-orchestration.md) |
| Price disjoint cache usage under the same task authority | Normalized uncached input, cache reads, cache writes, and output are separate billing buckets. [Contract](docs/adaptive-orchestration.md) |
| Treat terminal failure as evidence | Provider, sandbox, artifact, budget, and convergence failures must remain observable instead of being retried or discarded silently. [Contract](docs/runtime-evidence.md) |
| Keep saved user defaults and model profiles secret-free, and commit them only after validation | Saved defaults must remain secret-free and be committed only after explicit confirmation and validation. [Contract](docs/installation.md) |
| Make uninstall preservation-first | Uninstall preserves work, run evidence, provider state, and shared tools unless the user chooses attributable removal. [Contract](docs/installation.md) |
| Implement the Product Demo Slice before topology comparison | A reproducible engine is not yet a usable product. [Contract](docs/product-demo-slice.md) |
| Keep product and evaluation CLI surfaces distinct | Normal users run `sat` and receive guided defaults. [Contract](docs/product-demo-slice.md) |
| Keep generated-product profiles independent from evaluation fixtures | A benchmark must hold experiment inputs constant, while a product request must express the user's intent. [Contract](profiles/python/README.md) |
| Verify bounded executable documentation claims in the clean-copy gate | Exact setup, test, and start commands alone do not establish that a README's claimed example output is true. [Contract](profiles/python/README.md) |
| Establish the base request and model-work authorization before model-assisted Planning | SAT first records enough direct user input to define the requested outcome, execution profile, destination, and authorization. [Contract](docs/product-demo-slice.md) |
| Require a generated-project command manifest | Setup, start, and test commands vary by project. [Contract](profiles/python/README.md) |
| Keep expected paths advisory | Expected paths are forecasts, never an extra permission or product requirement. [Contract](docs/adaptive-orchestration.md) |
| Assign required test suites to a writable owner before approval | When a proposal promises a pytest suite, an implementation or integration task must explicitly own test authoring and its Agent scope must cover `repository/tests`. [Contract](docs/adaptive-orchestration.md) |
| Use one controller event stream for configurable run-level and per-Agent progress | Compact, standard, and detailed views project the same persisted controller facts without changing execution. [Contract](docs/adaptive-orchestration.md) |
| Accept user controls through a persisted controller-owned channel | User controls enter one persisted channel and apply only at safe lifecycle checkpoints. [Contract](docs/adaptive-orchestration.md) |
| Keep product state outside the application checkout | Planning evidence, managed runs, workspaces, and trusted source baselines live under the user-local state root. [Contract](docs/installation.md) |
| Deliver only an accepted result to a new child directory | The model works in an isolated detached clone. [Contract](docs/product-demo-slice.md) |

## Planned Workflow

```text
REQUEST
→ AUTHORIZE_PLANNING
→ PLANNING_DIALOGUE
→ PROPOSE_PLAN
→ REVIEW_OVERVIEW
   ├── REVISE_PLAN → PLANNING_DIALOGUE
   └── CONFIRM_PLAN
→ VALIDATE_TEAM_AND_ROUTES
→ PREPARE_WORKSPACE
→ CREATE_RUN_SCOPED_AGENTS
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

Execution also accepts persisted control inputs. Guidance enters incomplete
work at a declared safe boundary. Correction suspends new scheduling and
creates a versioned Planning revision. Cooperative pause stops new invocations;
resume revalidates evidence, workspace, dependencies, budgets, credentials,
and routes. Interruption requests best-effort termination of an active attempt,
and cancellation terminates the run and cleans only its owned resources. An
in-flight provider request may already incur usage and may not stop instantly;
the event stream reports whether each command was queued, applied, rejected,
or could not take effect.

Phase 1 starts from a frozen confirmed `TaskBrief`. The Product
Demo Slice connects `REQUEST`, bounded scope clarification, first-run
onboarding, automatic internal materialization, progress, and delivery to that
verified engine. The function-specialized path runs Tester and Reviewer
independently after deterministic gates. Dispatch is concurrent by default and
may be serialized for a provider with one generation slot. The frozen Phase 1
evaluation performs at most two implementation iterations: the initial pass
and one revision. The Product Demo Slice may use the team manifest's
three-iteration limit so a first revision that measurably resolves one blocker
does not force termination when a distinct correctable defect is then exposed.

The adaptive milestone replaces product-side team selection with a Planning
session and a user-approved TeamPlan. Existing fixed evaluation manifests are
compiled to that same contract instead of retaining a parallel controller
path. The controller emits one RunEvent stream for all renderers and records
every resolved model route and user control alongside the artifact evidence.

The Reviewer recommends `revise` for every correctable product defect,
including failed deterministic acceptance and security defects in generated
source. Finding severity records product impact and does not independently
authorize terminal failure. A Reviewer `fail` verdict is valid only with an
explicit terminal reason showing that another implementation attempt would
cross a run safety boundary or rely on compromised evidence; the deterministic
controller maps that reason to the final termination category.

Non-blocking findings describe residual issues that do not justify another
implementation iteration. Because Review artifacts have no explicit resolution
signal for those findings, the controller retains their descriptions across
iterations and in every terminal report. Reviewer omission never counts as
resolution; exact repeated descriptions are presented once.

A serialized writer chain may contain one zero-length result only when a
downstream Integration Agent starts at an exact completed writer result, leaves
that commit clean, completes every assigned integration task with no unresolved
issue, and supplies an accepted typed submission plus complete attributable tool
evidence containing a successful substantive check. Runtime rejections
disqualify the result. Historical asynchronous starts are permitted only when
the execution lifecycle proves every started tool operation completed before
the terminal response. This records verified integration without manufacturing
an empty commit;
implementation and revision writers must still produce a relevant descendant
commit.

The workflow stops earlier when fixed acceptance checks pass, every configured
manual criterion receives independent review, and no blocking review finding
remains. It stops with a report when:

- An authorized resource boundary or controlled-evaluation iteration limit is
  reached;
- A required runtime, model, dependency, or sandbox is unavailable;
- An Agent's response has no safe typed correction path, repeats without
  improvement, or cannot continue within the applicable resource authority;
- A safety boundary is crossed;
- An implementation or revision writer produces no relevant change;
- The same blocker repeats without measurable progress.

Failure and non-convergence are valid outcomes and must remain visible.

## Runtime Realization

The artifact layer remains the reproducible interface between Agents and the
controller. Persisted artifact schemas, smaller role-response bodies,
controller-owned field assembly, canonical paths, write-once storage, SHA-256
references, contextual validation, and deterministic transport normalization
must realize the control-plane decisions above without creating a second
lifecycle authority.

The detailed artifact, response, evidence, integrity, recovery, and failure
semantics are maintained in
[`docs/runtime-evidence.md`](docs/runtime-evidence.md). Executable schemas and
policies retain the code owners listed in
[`Ownership Boundaries`](#ownership-boundaries).

## Evaluation

### Output Quality

- Acceptance criteria satisfied;
- Automated tests passed and failed;
- Static-analysis findings;
- Blocking and non-blocking review findings;
- User-side black-box rubric score for correctness and usability;
- Reproducibility from a clean checkout.

### First-Delivery Effectiveness

- User-side corrections after requirements confirmation;
- Guidance, correction, pause, interruption, and cancellation counts;
- Control-command application latency and invalidated downstream work;
- Internal implementation iterations;
- Earlier findings resolved;
- Critical regressions;
- Completion and convergence rate.

### Reliability and Coordination

- Invalid or missing artifacts;
- Agent timeouts, retries, and failures;
- Merge or integration conflicts;
- Claims that disagree with repository evidence;
- Invalid TeamPlans, dependency deadlocks, and permission conflicts;
- Unplanned model switches or route-resolution failures;
- Termination reason.

### Efficiency

- Wall-clock duration;
- Controller Agent invocations and provider-internal attempts when available;
- Input and output tokens;
- Estimated cost;
- Coordination overhead;
- Per-route latency, usage, cost, and switch reason.

A result counts as improved only when at least one relevant quality indicator
improves, no critical indicator regresses, and a specific previous finding is
resolved.

The initial target is at least three comparable trials per configuration when
model budget permits. A smaller sample is labeled exploratory rather than
presented as a conclusive result.

## Safety Requirement

Safe execution is a product constraint, not an optional deployment concern.
The implementation must preserve sandbox isolation, least-privilege capability
profiles, explicit non-secret environments, auditable authorized model routes,
strict pinned-model evaluation, bounded resources and cost, integrity-checked
evidence, and explicit authorization for external side effects.

The concrete Git, sandbox, credential, model, resource, storage, and external-action
authorization boundaries are maintained in
[`docs/runtime-evidence.md`](docs/runtime-evidence.md). The qualifying
operator checklist is maintained in
[`docs/phase1-runbook.md`](docs/phase1-runbook.md).

## Core Scope

The core deliverable includes:

- Unified `sat` CLI;
- One-command managed installation with automatic environment diagnostics;
- A no-subcommand product entry point with guided first-run configuration;
- Multi-round bounded Planning dialogue and confirmed task briefs;
- An editable requirements, implementation, Agent, dependency, budget, and
  model-route overview;
- Task-defined run-scoped Agents validated and created by the controller;
- Automatic internal run, TaskBrief, source, workspace, and delivery
  materialization;
- Configurable controller-backed run/per-Agent progress and a concise final
  delivery view;
- Persisted guidance, correction, pause, resume, interruption, and cancellation
  controls;
- Task-, phase-, capability-, and Agent-specific model routing with explicit
  authorized automatic selection;
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
- Agent-controlled spawning, permission expansion, or unbounded team growth;
- Hidden chain-of-thought or unverifiable progress percentages;
- Silent model fallback or switching outside an approved route plan;
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

### Phase 2: Product Demo Slice

- Install SAT into a managed user-local location with one command;
- Diagnose supported environment conditions automatically and actionably;
- Make `sat` the guided product entry point;
- Configure a provider inside SAT's isolated OpenClaw-owned state without
  storing secrets in SAT control-plane configuration or evidence;
- Ask what the user wants to build, record success conditions and constraints
  within the installed execution profile, and confirm a concise requirements
  summary;
- Generate internal run IDs, TaskBriefs, sources, workspaces, and destinations
  automatically;
- Show controller-backed progress, review, revision, and failure summaries;
- Permit at most two evidence-driven product revisions while each iteration
  makes measurable progress, without changing the Phase 1 evaluation limit;
- Deliver a clean runnable result with exact next commands.

**Exit criterion:** the journey in
[`docs/product-demo-slice.md`](docs/product-demo-slice.md) passes its offline
interaction tests and one fresh supported-device rehearsal without requiring
the user to operate the evaluation CLI.

### Phase 3: Adaptive Orchestration and Interactive Control

The detailed contracts, implementation batches, and acceptance criteria are in
[`docs/adaptive-orchestration.md`](docs/adaptive-orchestration.md). Work is
ordered so current fixed teams migrate onto one contract instead of creating a
parallel product controller.

#### Phase 3A: Contracts and Compatibility

- Add versioned TeamPlan, AgentSpec, ModelRoutePlan, RunEvent, and
  ControlCommand schemas;
- Validate dependencies, ownership, permissions, quality independence, routes,
  and budgets before model work;
- Compile existing fixed evaluation manifests into TeamPlan;
- Move the current workflow and progress source onto those contracts, then
  remove the direct fixed-role path.

**Exit criterion:** the current function-specialized path passes its complete
offline suite through TeamPlan, while invalid dynamic plans fail before Agent
creation.

#### Phase 3B: Planning Dialogue and Plan Approval

- Add model-work authorization followed by bounded multi-round dialogue;
- Combine free-form conversation with suggested options and custom answers;
- Establish an attributable ProductDefinition before deriving requirements and
  the execution team;
- Generate one requirements, implementation, team, dependency, budget, and
  model overview;
- Let the user approve, request a natural-language revision, or edit safe
  structured fields;
- Persist every approved plan revision.

**Exit criterion:** a user can revise and approve a task-defined TeamPlan from
an ordinary request without editing an internal file.

#### Phase 3C: Dynamic Team Runtime

- Compile role prompts from AgentSpec and versioned templates;
- Combine task-specific role identity with a versioned specialization catalog
  and separately validated executable capability/permission bundles;
- Derive and display non-overlapping Reviewer scopes from typed acceptance
  relationships, and reject missing specialist authority before approval;
- Create run-scoped OpenClaw sessions only through the controller;
- Schedule the dependency DAG with bounded concurrency;
- Enforce permission profiles, workspace ownership, typed handoffs, independent
  quality, and aggregate budgets;
- Apply team amendments only at validated safe checkpoints.

**Exit criterion:** two materially different tasks produce different justified
teams, DAGs, specialization prompts, typed artifacts, and acceptance strategies,
then complete or fail through the same controller, evidence, and cleanup
boundary.

#### Phase 3D: Observable and Controllable Execution

- Render compact, standard, and detailed views from one append-only event
  stream;
- Show each Agent's phase, safe current activity, dependencies, handoffs,
  model route, gates, elapsed time, and relevant budgets;
- Implement persisted guide, correct, cooperative pause/resume, best-effort
  interrupt, and terminal cancel semantics;
- Add integrity, restart, non-TTY, cancellation, and resource-cleanup coverage.

**Exit criterion:** offline end-to-end tests exercise every visibility level and
control; an authorized live run demonstrates guidance and cooperative
pause/resume without losing evidence integrity.

#### Phase 3E: Model Profiles and Routing

- Support multiple secret-free model profiles;
- Add task, phase, capability, and Agent preferences;
- Resolve `auto` deterministically within authorized candidates, capabilities,
  switch conditions, and budget;
- Record every resolved model, reason, switch, telemetry, and unavailable-price
  state;
- Preserve strict pinned-model evaluation mode.

**Exit criterion:** routing tests cover precedence, capability mismatch,
unavailable providers, budget rejection, authorized and refused switches, and
strict evaluation; one authorized run uses two planned routes without silent
fallback.

#### Phase 3F: Adaptive Product Acceptance

- Rehearse fresh install, Planning dialogue, plan revision, dynamic execution,
  progress, controls, routing, delivery, export, and uninstall;
- Record usability and coordination defects;
- Freeze one adaptive-team evaluation configuration.

**Exit criterion:** a fresh supported device completes the adaptive journey
without internal files or evaluation commands, with auditable plan, event,
control, route, Git, quality, and cleanup evidence.

### Phase 4: Baselines and Controlled Evaluation

- Add the one-pass single-Agent path;
- Add the fixed domain-specialized path with explicit integration;
- Compare fixed topologies with one frozen TaskBrief and model policy;
- Compare a frozen task-defined TeamPlan with the strongest fixed baseline;
- Hold TeamPlan constant in a separate model-routing experiment;
- Analyze quality, reliability, cost, time, intervention, and coordination
  failures;
- Select supported defaults or report an inconclusive result.

**Exit criterion:** every recommendation is traceable to comparable run
evidence, and adaptive team design is not confounded with model routing.

### Phase 5: Generalization and Pass-Off

- Validate the selected configuration on a second use case;
- Harden process-crash recovery and remaining sandbox diagnostics;
- Package a demonstration and public-ready technical report.

**Exit criterion:** a user can start from a brief request, understand and alter
the proposed approach, observe or control execution as desired, and receive one
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

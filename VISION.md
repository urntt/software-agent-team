# Vision: An Experimental Multi-Agent Software Builder

**Implementation status:** [`STATUS.md`](STATUS.md)

**Last updated:** August 26, 2026

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
- A confirmed `TaskBrief` and user-approved `TeamPlan`;
- Run-scoped `AgentSpec` entries and a `ModelRoutePlan`;
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
- The approved team/model plan, its revisions, and Agent-creation records;
- An ordered progress and user-control event record;
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
to build, and conducts a bounded multi-round Planning dialogue. Questions may
use normal conversation or suggested choices with a custom answer path.

Before execution, SAT shows one editable overview of requirements,
implementation intent, task-defined Agents, dependencies, permissions, budgets,
and model routes. After approval, the controller validates and creates the
run-scoped team. During execution, SAT shows configurable run-level and
per-Agent progress and accepts user guidance, correction, pause, resume,
interrupt, or cancellation. It returns a runnable result or an honest terminal
report with exact next commands.

Internal run IDs, TaskBrief JSON, benchmark source paths, team IDs, policy
paths, concurrency, timeouts, repair limits, and evidence roots are advanced
implementation or evaluation concepts. The normal user must not prepare or
edit them.

The guided product-journey acceptance specification is
[`docs/product-demo-slice.md`](docs/product-demo-slice.md).
The next adaptive interaction and orchestration milestone is specified in
[`docs/adaptive-orchestration.md`](docs/adaptive-orchestration.md).

## Technical Direction and Compatibility Constraints

These decisions govern both current behavior and the next implementation
milestone. [`STATUS.md`](STATUS.md) identifies which paths exist now; planned
adaptive contracts land in the ordered batches below. Decisions change only
when code, usability evidence, or controlled experiments justify a replacement.

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
- SAT installs that version beneath its own marked application-private runtime
  and uses a separate SAT-owned OpenClaw config, credential, session, cache,
  and workspace root. It never adopts another OpenClaw installation or
  profile, even when that installation is compatible and ready.
- Every resolved `provider/model` must pass SAT's isolated catalog and auth
  checks before its Agent invocation. Startup validates saved defaults;
  run-scoped preflight validates the approved route plan without generation,
  while an actual provider smoke request remains separately authorized.
- SAT may carry a small versioned, secret-free catalog supplement when an
  explicitly supported provider model exists upstream but is absent from the
  pinned OpenClaw catalog. The supplement declares only routing and model
  metadata; credentials remain in SAT's isolated auth profiles or a trusted
  caller environment, and the selected model never falls back silently.
- Product runs may select different authorized models by task, phase,
  capability, or Agent. A deterministic controller policy resolves `auto`
  preferences and records its reason. Runtime switching is allowed only for an
  approved candidate and condition; it is never silent. Controlled evaluation
  mode continues to pin one model and disable switching.
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
- Execution Agents cannot spawn additional model calls. The controller is the sole
  authority for Agent invocation, accounting, and ordering.
- A fixed bootstrap Planning capability may propose requirements,
  implementation, Agent, and model plans. It cannot create Agents or advance
  the lifecycle. The controller creates run-scoped execution Agents only after
  user approval and deterministic TeamPlan validation.
- The controller owns an append-only event stream and a persisted control
  channel. Guidance, correction, pause, resume, interruption, cancellation,
  and any resulting replan are lifecycle inputs, not hidden chat mutations.

### Communication

- Structured asynchronous handoffs are the default communication mechanism.
- A downstream Agent receives only its required inputs and attributable upstream
  artifacts.
- Direct Agent messages or timed checkpoints are experimental extensions, not
  authoritative state.
- A handoff records the run, team, iteration, source Agent ID, target Agent ID,
  status, input commit, artifacts, blockers, and summary.
- Role names and team membership are task-defined on the product surface. The
  plan must still define attributable responsibilities, typed inputs and
  outputs, dependencies, permissions, budgets, and quality independence.

### Isolation and Permissions

- Every run uses an isolated, self-contained Git clone with no remote and a
  detached HEAD.
- Generated code executes only inside a restricted sandbox.
- OpenClaw supplies an explicit long-lived supervisor command when it creates a
  scope-owned role container; the image uses the same command as its standalone
  diagnostic default. Installation and run preflight must both execute a real
  helper inside a restricted probe container. Image presence or a momentary
  running state alone is not readiness evidence.
- Because every SAT role session is unique to one immutable run, its OpenClaw
  container has no valid reuse after that run terminates. SAT removes exact
  run-scoped containers on success, failure, interruption, and exception. The
  cleanup selector requires both a controller-generated session identity and a
  mount inside SAT-owned state or workspace paths; it cannot use a broad name
  match or affect another OpenClaw installation.
- The per-container cgroup PID limit is the authoritative process-count
  boundary. SAT does not also set `RLIMIT_NPROC`: that limit can count every
  process sharing the numeric UID outside the container and can prevent PID 1
  from starting on otherwise healthy Docker hosts.
- Live Agent containers run as the invoking unprivileged host identity; UID or
  GID `0` is rejected.
- Planning, analysis, testing, and review responsibilities use controlled
  read-only permission profiles. Coding and integration responsibilities may
  use controlled writable profiles only inside their assigned workspace.
- An Agent label or model proposal never grants a permission. The controller
  validates every AgentSpec against versioned capability profiles before
  creating a session.
- A read-only OpenClaw workspace is mounted at `/agent`; a writable workspace
  is mounted at `/workspace`.
- Controlled roles load no ambient runtime skills, so unrelated skill prompts
  and files cannot enter the benchmark workspace or its quality checks.
- Every SAT-launched OpenClaw process receives explicit SAT-owned config,
  credential, state, workspace, and Agent paths. All ambient `OPENCLAW_*`
  settings and the legacy Agent-directory selector are neutralized while
  ordinary trusted provider API-key variables may still be inherited from the
  caller.
- An existing OpenClaw binary, Gateway, process, config, profile, credential
  store, session, cache, or workspace is outside SAT's ownership. SAT does not
  probe, reuse, reconfigure, stop, upgrade, downgrade, or uninstall it. Role
  execution uses local mode and never attaches to that Gateway.
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
| User-facing public overview and quick start | `README.md` |
| Guided product-journey interaction and acceptance specification | `docs/product-demo-slice.md` |
| Adaptive Planning, task-defined teams, progress visibility, user controls, and model-routing acceptance design | `docs/adaptive-orchestration.md` |
| Installation, saved configuration, export, and removal behavior | `docs/installation.md` |
| Runtime, response, persisted-evidence, integrity, and operator-safety reference | `docs/runtime-evidence.md` |
| Controlled Phase 1 provider-backed evaluation procedure | `docs/phase1-runbook.md` |
| Development workflow and repository reference | `docs/development.md` |
| Versioned fixed evaluation topology fixtures | `configs/teams.json` |
| Run-scoped TeamPlan, AgentSpec, ModelRoutePlan, validation, and fixed-manifest compatibility compilation | `src/software_agent_team/teams.py` |
| Adaptive Planning dialogue, proposal compilation, overview, approval, and write-once evidence | `src/software_agent_team/planning.py` |
| Phase and handoff artifact schemas | `src/software_agent_team/artifacts.py` |
| Canonical persisted-model integrity digest | `src/software_agent_team/integrity.py` |
| Immutable artifact, handoff, and output persistence | `src/software_agent_team/artifact_store.py` |
| Controller binding of Agent semantics to verified runtime facts | `src/software_agent_team/assembly.py` |
| Sanitized Agent runtime boundary | `configs/openclaw.example.json5` |
| SAT-owned OpenClaw process-environment isolation | `src/software_agent_team/openclaw_runtime.py` and `scripts/openclaw-environment.sh` |
| Run-scoped runtime materialization and preflight | `src/software_agent_team/runtime_configuration.py` |
| Run lifecycle state and persistence | `src/software_agent_team/run_control.py` |
| Phase 1 orchestration, decisions, and reports | `src/software_agent_team/workflow.py` |
| Approved TeamPlan dependency and shared-workspace scheduling | `src/software_agent_team/scheduling.py` |
| Versioned RunEvent contract, append-only journal, visibility filtering, and terminal rendering | `src/software_agent_team/progress.py` |
| Versioned ControlCommand contract and controller-owned mailbox history | `src/software_agent_team/controls.py` |
| Agent-call, token, duration, cost, per-Agent, and fixed-role compatibility invocation budgets | `src/software_agent_team/budgets.py`, `configs/product-policy.json`, and `configs/run-policy.json` |
| Controller binding of each invocation to budget usage, raw outputs, and telemetry evidence | `src/software_agent_team/invocation.py` |
| Product execution profile and generic quality contract | `profiles/python/`, `runtime/python/`, and `configs/product-policy.json` |
| Frozen evaluation fixture and task-specific acceptance | `benchmarks/task_manager/` and `configs/run-policy.json` |
| Shared quality-manifest validation and execution | `src/software_agent_team/quality_gates.py` |
| Agent process invocation and telemetry parsing | `src/software_agent_team/execution.py` |
| CLI commands and runtime option resolution | `src/software_agent_team/cli.py` |
| Product diagnostics, supported request materialization, and safe delivery | `src/software_agent_team/product.py` |
| User-local product state path | `src/software_agent_team/paths.py` |
| User-local default schema and persistence | `src/software_agent_team/user_configuration.py` |
| Managed bootstrap, installation, and uninstallation execution | `scripts/bootstrap.sh`, `scripts/install.sh`, and `scripts/uninstall.sh` |
| Fixed-role and task-defined capability prompt assembly | `src/software_agent_team/prompting.py` |
| Fixed-role and run-scoped Agent semantic response validation and field mapping | `src/software_agent_team/responses.py` |
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

The target product flow uses a bootstrap Planning session to propose a
run-scoped TeamPlan from the confirmed task. The user reviews and may revise
the plan; the deterministic controller validates and creates its Agents. The
number and names of execution roles therefore vary with the work, while
permission profiles, quality independence, budgets, evidence, and lifecycle
authority remain fixed system boundaries.

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

## Decision Record

| Decision | Reason |
| --- | --- |
| Use a local-first CLI instead of a Web service | The first users can inspect Git and terminal evidence, while local execution keeps credentials, workspaces, and experimental state under their control. |
| Keep the Python controller authoritative | Lifecycle, budgets, evidence checks, and termination must be deterministic rather than dependent on an Agent's self-report. |
| Derive execution roles from the task, then let the controller create them | A fixed bootstrap Planning capability can propose a TeamPlan after dialogue, but it cannot spawn Agents. User approval plus deterministic validation preserves authority, budget, permission, and audit boundaries while avoiding one permanent product role list. |
| Require independent quality coverage without imposing a permanent Tester/Reviewer pair | Every writing path needs a downstream read-only quality judgment before acceptance, but a small cohesive task may justify one quality Agent while a higher-risk task may justify independent testing and review. Fixed dual-quality fixtures remain useful experimental controls rather than product topology. |
| Show one editable plan overview before execution | Requirements, implementation intent, Agent responsibilities, dependencies, permissions, budgets, and model routes affect quality and cost. The user must be able to approve or revise them before the controller creates the team. |
| Use OpenClaw as the Agent runtime, not the orchestrator | OpenClaw provides model/provider integration, sessions, tools, and sandboxing; the experiment still needs a model-independent control plane. |
| Isolate SAT's OpenClaw runtime and state from every existing installation | Compatibility is not ownership. Installing a pinned private binary and overriding every mutable OpenClaw path gives SAT reproducibility without reading, changing, stopping, or deleting a user's existing binary, Gateway, profile, configuration, credentials, sessions, caches, or workspaces. A collision at SAT's private target fails safely instead of being adopted. |
| Validate and, when necessary, supplement the exact model catalog before Agent work | Saving a `provider/model` string does not prove that the pinned runtime can resolve it. A non-generation catalog/auth check catches unsupported or unauthenticated selections before a build, while a versioned secret-free supplement can bridge a known catalog lag without copying credentials or enabling fallback. |
| Keep the controller's invocation timeout authoritative | A provider compatibility supplement may describe routing and model metadata, but it must not add an independent transport timeout that conflicts with the approved per-Agent invocation policy. OpenClaw receives the resolved timeout for every call, and SAT's outer process boundary adds only bounded shutdown grace. |
| Start with `function_specialized` | It introduces independent planning, testing, and review without the merge conflicts that would confound the first vertical slice. |
| Keep Tester and Reviewer independent, with configurable dispatch concurrency | They inspect the same immutable evidence and never consume each other's interpretation. Parallel dispatch reduces elapsed time when provider capacity permits; serial dispatch prevents overload without changing the semantic experiment. |
| Deny Agent-spawning tools to every execution Agent | Untracked sub-Agent calls bypass controller budgets, attribution, and scheduling, so only the deterministic controller may authorize model invocations. |
| Include bounded command-output tails in verifier context | Exit codes and generic summaries identify failure but not its cause. Bounded untrusted excerpts make diagnosis possible while full write-once output remains authoritative. |
| Use persisted structured handoffs | Durable, attributable artifacts make context and decisions auditable without treating hidden conversation history as state. |
| Run fixed Docker quality gates before Agent judgment | Reproducible command evidence is stronger than claimed test results and keeps generated code isolated from the host. |
| Resolve the sandbox tag to one local image ID per run | Both Agent sandboxes and quality gates execute the same immutable image even if a mutable local tag is later reassigned. |
| Assemble persisted artifacts in the controller | Models should produce planning, implementation summaries, evidence analysis, and review judgment. Known identity, Git, command, status, criterion, and scope facts must come from authoritative controller state instead of requiring an exact model echo. |
| Allow one semantic-response repair per role stage and bounded implementation revisions | A small bounded loop can correct genuinely invalid semantic content or implementation defects without hiding non-convergence, time, or cost. The frozen Phase 1 evaluation remains one initial implementation plus one revision; the product flow permits one additional revision when the prior iteration measurably resolved a blocker and exposed a distinct correctable defect. Controller-owned fields are ignored and audited rather than repaired. |
| Use measured capability defaults, approved per-Agent invocation timeouts, and independent bounded-repair timeouts | Planning, implementation, and verification have different measured workloads. Fixed-fixture compatibility defaults are 120 seconds for Clarifier, 180 for Planner, 900 for coding/integration, and 300 for testing/review; Adaptive Planning proposes a task-specific value within the corresponding capability ceiling and the user approves it. A repair must regenerate a complete semantic response, so it receives the same per-invocation timeout instead of an arbitrarily small remainder from the first call. Repair remains limited to one call, while total call count, Agent duration, token, and cost budgets bound the complete run. |
| Separate review severity from terminal failure | Even a critical-impact product defect may be correctable. Reviewer `fail` therefore requires an explicit safety or evidence-integrity termination reason; ordinary gate failures and implementation defects request `revise`. |
| Version requirement or acceptance corrections | A hidden or over-specified acceptance condition confounds model evaluation. The confirmed TaskBrief must expose the product contract, black-box checks must accept equivalent compliant presentations, and a correction starts a new benchmark version. |
| Separate product model routing from strict evaluation routing | Controlled evaluations pin one model and price table and disable switching. Product runs may use approved task-, phase-, capability-, or Agent-specific routes, but the controller must resolve and record every model and may switch only under an explicit user-authorized condition. |
| Treat terminal failure as evidence | Provider, sandbox, artifact, budget, and convergence failures must remain observable instead of being retried or discarded silently. |
| Keep saved user defaults and model profiles secret-free | The product wizard may store provider/model references, routing preferences, and non-secret metadata, but provider credentials remain in SAT's isolated OpenClaw-owned state and never enter SAT control-plane configuration or exports. Optional pricing, concurrency, and a global invocation-timeout override remain advanced settings; checked-in capability defaults remain the normal timeout policy. |
| Make uninstall preservation-first | Removing the CLI must not silently destroy run evidence, generated workspaces, SAT's isolated provider state, shared tools, a source checkout, or any other OpenClaw installation; export and purge therefore require explicit user choices. |
| Implement the Product Demo Slice before topology comparison | A reproducible engine is not yet a usable product. The core promise starts from a short request, so installation, onboarding, clarification, progress, and delivery must be executable before the project presents an internal evaluation workflow as its demo. |
| Keep product and evaluation CLI surfaces distinct | Normal users run `sat` and receive guided defaults. Explicit TaskBrief files, benchmark preparation, team IDs, policy paths, timeouts, concurrency, and repair controls remain available to contributors without becoming first-run questions. |
| Keep generated-product profiles independent from evaluation fixtures | A benchmark must hold experiment inputs constant, while a product request must express the user's intent. Sharing the controller and quality-manifest schema is useful; sharing a task-specific TaskBrief, seed, acceptance suite, environment contract, or delivery command would silently replace the user's request and invalidate both boundaries. |
| Establish the base request and model-work authorization before model-assisted Planning | SAT first records enough direct user input to define the requested outcome, execution profile, destination, and authorization. The Planning session may then clarify and propose requirements, but the user remains their final authority and approves every frozen plan revision. |
| Require a generated-project command manifest | Setup, start, and test commands vary by project. A validated `sat-project.json` argv contract lets SAT deliver exact commands without assuming FastAPI, a Web server, or any task-specific entry point. |
| Use one controller event stream for configurable run-level and per-Agent progress | Compact, standard, and detailed views may expose different safe summaries, dependencies, routes, gates, and budgets without changing execution. Hidden chain-of-thought, secrets, raw unbounded output, and unverifiable percentages are excluded. |
| Accept user controls through a persisted controller-owned channel | Guidance applies prospectively; correction creates a versioned replan; pause/resume use integrity-checked safe boundaries; interruption and cancellation are best effort for active provider work and preserve cost/evidence. No control may be implemented as an untracked chat mutation. |
| Keep product state outside the application checkout | Managed runs, workspaces, and trusted source baselines live under the user-local state root. Installation updates cannot overwrite evidence, and uninstallation can preserve, export, or explicitly purge state independently of the application files. |
| Deliver only an accepted result to a new child directory | The model works in an isolated detached clone. After acceptance, SAT copies the exact accepted commit through a same-parent staging directory and publishes it with Linux atomic no-replace semantics, so a failed run, late destination, or conflict never overwrites user files. |

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
- Human rubric score for correctness and usability;
- Reproducibility from a clean checkout.

### First-Delivery Effectiveness

- Human corrections after requirements confirmation;
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
evidence, and human authorization for external side effects.

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
- Generate one requirements, implementation, team, dependency, budget, and
  model overview;
- Let the user approve, request a natural-language revision, or edit safe
  structured fields;
- Persist every approved plan revision.

**Exit criterion:** a user can revise and approve a task-defined TeamPlan from
an ordinary request without editing an internal file.

#### Phase 3C: Dynamic Team Runtime

- Compile role prompts from AgentSpec and versioned templates;
- Create run-scoped OpenClaw sessions only through the controller;
- Schedule the dependency DAG with bounded concurrency;
- Enforce permission profiles, workspace ownership, typed handoffs, independent
  quality, and aggregate budgets;
- Apply team amendments only at validated safe checkpoints.

**Exit criterion:** two materially different tasks produce different justified
teams and complete or fail through the same controller, evidence, and cleanup
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

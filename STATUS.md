# Project Status

**Current milestone:** Phase 3C dynamic Agent runner implemented; lifecycle convergence and product activation are next

**Last updated:** August 26, 2026

This document records what the repository implements now, what evidence
supports that claim, and what remains unavailable. It does not redefine the
product, architecture, experiment, or roadmap; those decisions belong to
[`VISION.md`](VISION.md).

## Phase 1 Result

The function-specialized vertical slice is implemented and has produced a
qualifying version-two provider-backed evaluation that reached `completed`.
One controller-verified implementation commit passed every deterministic gate,
all ten acceptance criteria, and independent review, with complete model,
token, hash, and Git-boundary evidence.

Two consecutive provider-backed replays of the same engine revision have also
reached `completed`, each through the bounded evidence-driven revision loop.
The earlier benchmark defect was corrected and versioned as
`task_manager_phase1_v2`; version-one evaluation records remain exploratory
evidence and must not be mixed with version-two comparisons.

Managed installation, startup diagnostics, secret-free first-run model setup,
execution-profile confirmation, user-owned success conditions, automatic run
preparation, controller-backed progress, accepted-result delivery, and safe
uninstallation are implemented and covered offline. Provider credential
creation remains in SAT's isolated OpenClaw-owned boundary. Repeated
comparative experiments and human rubric scoring remain pending.

The exact acceptance procedure is in
[`docs/phase1-runbook.md`](docs/phase1-runbook.md). Offline scripted executions
prove controller behavior, not model quality.

## Adaptive Orchestration Progress

The Phase 3A compatibility path is implemented. `TeamPlan`, `AgentSpec`,
and `ModelRoutePlan` are executable versioned contracts rather than roadmap-only
names. The current function-specialized workflow compiles its fixed evaluation
fixture into that contract, persists `team-plan.json`, and gives the frozen plan
to run control, artifact validation, timeout resolution, and verification
dispatch. There is no second fixed-role run-control path.

Validation rejects invalid dependencies, write-scope conflicts, incompatible
permissions, missing independent quality coverage, unauthorized model routes,
and over-limit concurrency or Agent counts before Agent creation. Recovery
verifies the exact TaskBrief binding, TeamPlan digest, fixed manifest version,
fixed team digest, resolved Agent timeouts, and cross-file run metadata. The
complete repository check passes with 514 offline tests.

`RunEvent` is also an executable, append-only contract. Every current workflow
progress update is persisted with a contiguous sequence, lifecycle revision,
phase, Agent identity when applicable, visibility class, and predecessor
digest. `run.json` atomically anchors the latest event, so recovery detects
missing, reordered, modified, or extra events, including a changed tail.
Compact, standard, and detailed filtering consumes the same event contract;
the current product launcher still selects the standard renderer.

`ControlCommand` and its controller-owned revision store define and preserve
the request, target, safe application boundary, status, consequence, plan or
lifecycle result, and provider-cost caveat for guide, correct, pause, resume,
interrupt, and cancel. This is a persistence boundary, not a claim that the
normal CLI can apply controls yet.

The Phase 3B Planning engine is also implemented. A versioned `PlanningRequest`
proves explicit model-work authorization before the first invocation. The
read-only bootstrap Planner may return either one decision-value question with
two or three suggestions and a custom-answer path, or one complete proposal.
Strict proposal validation covers requirements, acceptance criteria,
implementation tasks, task ownership, dynamic Agent responsibilities,
dependencies, workspace scopes, independent quality coverage, concurrency,
iterations, per-Agent timeouts, the configured model route, and controller
budgets before the proposal is shown.

The ordinary-user interaction supports free-form answers, natural-language
replacement revisions, safe edits to maximum concurrency, iteration count, and
individual Agent timeouts, cancellation, a complete plain-language overview,
and explicit approval. `PlanningStore` persists the authorized request,
hash-chained model turns including rejected response evidence, immutable
proposal revisions, and the exact approval digests. Approval promotes the
validated preview into an authorized confirmed `TaskBrief`, adaptive
implementation plan, and executable `TeamPlan`.
The bootstrap Planner still cannot create an Agent or advance run state.

The first Phase 3C runtime boundary is also implemented. Dynamic execution
requests and telemetry use an approved run-scoped Agent ID and capability;
fixed-role identity remains compatibility metadata only for the existing
evaluation workflow. Capability-specific templates compile the exact approved
responsibility, assigned tasks, dependencies, permission profile, model route,
and timeout into minimum-context prompts. Response parsing rejects mismatched
Agent identity, capability, session, task ownership, model, or timeout.

Artifact schema v2 removes fixed-role identity from durable handoffs and
execution records. Every Agent-produced iteration artifact is stored beneath
an Agent-ID namespace, so multiple approved Agents can produce the same typed
artifact without path collisions. Artifact-store validation binds each
producer, handoff endpoint, stage, and recorded capability back to the exact
run-scoped `AgentSpec`. The fixed evaluation adapter now writes through this
same generic evidence boundary.

Controller-owned artifact assembly is now shared by fixed and task-defined
teams. It combines validated Agent semantics with the exact approved AgentSpec
identity, controller-verified Git snapshot, deterministic command evidence,
immutable quality commit, and assigned review scope. Dynamic `IterationRecord`
aggregation accepts task-proportional teams rather than one hard-coded
Developer/Tester/Reviewer tuple: it requires a chained result from every
approved writer, consistent deterministic evidence from every approved Tester
(or one controller report when the team intentionally has none), and evidence
from every approved Reviewer. Split review scopes must exactly cover manual
criteria, and finding identities must be unique across the iteration.

Run configuration materialization emits only the approved AgentSpecs, clones
their least-privilege capability profiles, disables model fallback, and binds
every Agent to the verified workspace and selected route. Exact-label sandbox
cleanup can derive all owned session identities from those AgentSpecs. Adaptive
validation excludes the bootstrap Planning and Clarification capabilities from
the runtime team, requires every writer to own work, and allows a small task to
use one writer plus one independent quality Agent instead of imposing a hidden
Tester/Reviewer pair. Every quality Agent must depend on every writer path, so
parallel verification cannot start against an intermediate commit. Fixed
evaluation fixtures retain their explicit dual-quality topology.

The Phase 3C dynamic runner is now implemented behind the general DAG
scheduler. The scheduler remains the only owner of readiness, launch order,
bounded concurrency, and shared-Git writer exclusion. The runner invokes only
the supplied approved `AgentSpec`, preserves its exact model and timeout,
allows at most one full-timeout semantic repair, accounts for every call in one
thread-safe aggregate ledger, and persists raw output plus telemetry before a
post-call budget rejection stops the schedule. Agents cannot create another
Agent, change dependencies, reorder work, or extend timeouts.

Each dynamic writer starts from the controller's current clean commit, leaves
a clean descendant commit, and is rejected for changes outside its approved
workspace scope. Read-only quality Agents must leave the same immutable commit
and clean tree. Deterministic gates execute exactly once per iteration even
when Tester and Reviewer Agents run concurrently. Their reports share the same
controller-owned evidence and final commit; when a justified small team has no
Tester, the controller persists the deterministic TestReport itself. Dynamic
source-to-target and terminal handoffs are write-once and include attributable
phase and execution evidence. Offline integration tests exercise real Git
commits, parallel quality, semantic repair, missing telemetry, budget
exhaustion, read-only mutation, and write-scope violations.

The Planning interaction is offline-verified as a product-style flow but is
not yet activated by bare `sat`: activation is intentionally paired with the
Phase 3C runtime so SAT never presents a dynamic plan and then silently
executes the old fixed team. The general DAG scheduler, dynamic runner, generic
response assembly, dynamic iteration aggregation, persisted handoffs,
deterministic quality sharing, and aggregate invocation budgets are
implemented. Multi-iteration convergence, the lifecycle coordinator that
consumes a complete schedule, bare `sat` activation, safe concurrent writers
beyond the current serialized Git chain, and active control application remain
pending.

## Product Readiness Boundary

The primary CLI now implements the Product Demo Slice in code. A normal user
runs `sat`; SAT checks the device, guides model configuration, asks what to
build, explains and confirms the installed Python execution profile, collects
success conditions and constraints, chooses a new project destination,
generates a request-specific run ID, TaskBrief, trusted source, workspace, and
evidence roots, shows controller-derived progress, and delivers only an
accepted clean Git result with project-specific commands.

This implemented path still uses bounded fixed prompts and the
`function_specialized` evaluation fixture, now compiled into a frozen
run-scoped `TeamPlan`; it uses one selected model for the run,
controller-backed stage progress, and no interactive run-control channel. The
task-defined runtime, activation of the implemented multi-round Planning
dialogue, per-Agent visibility levels, live user controls, and model routing
described in
[`docs/adaptive-orchestration.md`](docs/adaptive-orchestration.md) are the next
milestone, not current capabilities.

This is not yet release-stable evidence. Two earlier WSL rehearsals completed
managed installation or update, startup diagnostics, isolated provider setup,
request confirmation, internal run materialization, and the Planner stage.
Both then reached a stopped Developer sandbox before any workspace tool could
run, so no project was delivered. The second run was correctly classified as
`dependency_unavailable` instead of a source-code failure.

The exported execution record identified the stopped container, and a
read-only Docker postmortem established the root cause: PID 1 exited in 72 ms
with status 255 and `exec /usr/bin/sleep: resource temporarily unavailable`.
OpenClaw had explicitly supplied `sleep infinity`; the image command was not
the cause. The duplicate `RLIMIT_NPROC=128` counted processes by numeric UID
beyond the container on this Docker Desktop host and rejected the initial
process. SAT now retains the per-container cgroup PID limit and omits
`RLIMIT_NPROC`. Installation and live-run probes also supply OpenClaw's command,
execute a Python helper, inspect liveness, and remove the probe. Invalid
terminal Unicode is now rejected and recollected at the affected prompt rather
than reaching Pydantic as a raw validation failure.

The third WSL rehearsal updated the managed application, passed installation,
and persisted `config_valid=true`, `sandbox_container_ready=true`, and no
container error in run `sat-20260824-144218-5102217f`. This directly confirms
the process-limit fix on the Docker Desktop/WSL host. The run then stopped
before a provider request because the pinned DeepSeek plugin catalog knew only
its stable model while the selected
`deepseek/deepseek-v4-flash-vision-exp` reference had not been declared in the
run-scoped OpenClaw provider catalog. OpenClaw reported the exact failure as
`Unknown model`, but the old preflight did not inspect that model route and the
terminal summary therefore surfaced it as a Planner process failure.

SAT now carries a narrow secret-free catalog supplement for that exact model,
checks the selected configured model and auth route during guided startup and
run preflight, persists the model result in `runtime-preflight.json`, and stops
with the direct model diagnostic before any Agent call when it is unavailable.
An available shell key is represented only by `${DEEPSEEK_API_KEY}`; otherwise
the isolated OpenClaw auth profile remains authoritative. The generated config
passes the pinned OpenClaw validator, the exact model is reported locally as
available, and an authorized minimal inference request returned HTTP 200 with
the exact provider/model and expected response.

A subsequent clean non-root rehearsal started from the public one-command
installer and then used only the normal `sat` entry point. Managed installation,
automatic device checks, first-run configuration, the optional provider smoke
check, natural-language request capture, confirmation, and run preflight all
passed with `deepseek/deepseek-v4-flash-vision-exp`. Run
`sat-20260824-225204-176978a8` reached the Planner through the same product flow
an end user sees. Its first response arrived after 72.1 seconds and contained a
short redundant closing-delimiter suffix as well as real semantic defects: an
unknown field and incomplete acceptance-criterion coverage. The bounded repair
was therefore required and produced a valid plan after another 67.0 seconds,
but the then-current shared 120-second Planner deadline had already expired.
SAT stopped without delivering a project; later live evidence showed that
sharing one deadline across two separately authorized calls was itself an
over-constrained timeout policy.

The parser now normalizes only a complete object followed by at most four
unmatched closing delimiters; it continues to reject additional values,
structures, unknown semantic fields, and incomplete plans. Raw provider output
remains unchanged in execution evidence. The Planner timeout is now 180 seconds
per invocation. An optional one-call repair receives that same complete
invocation allowance; the run-wide call-count, Agent-duration, token, and cost
budgets include both calls.

A second rehearsal used a newly created Linux account with its own home,
configuration, provider state, and project parent. The public installer checked
out the published revision, and the normal `sat` flow again passed every step
through planning. The Developer completed a clean implementation commit and 24
project tests in 854.4 seconds, within its existing 900-second budget. Its one
semantic JSON object was enclosed in the requested JSON fence, but the
presentation text before the fence included ordinary command notation such as
Python-style argv arrays. The former fence normalizer treated any square
bracket outside the fence as a competing JSON structure, requested an
unnecessary repair, and then rejected the combined 929-second path at the
then-current shared deadline.

The fence normalizer now distinguishes a separately decodable JSON object or
array from non-JSON documentation notation. It accepts the observed original
Developer response without repair while retaining the stricter raw-object
boundary and rejecting multiple fences or competing JSON values. The existing
900-second Developer budget is unchanged. The complete offline suite and the
corrected restricted Docker helper probe remain successful.

A third clean-account rehearsal then completed the entire controller workflow
in 1,000 seconds. The Developer response was accepted directly in 659.3
seconds, all four deterministic gates passed, independent Tester and Reviewer
responses passed, the decision was `accept`, and SAT delivered a 5/5 project.
The delivered setup command succeeded, but the exact delivered test command,
`uv run pytest`, failed while collecting tests because the generated top-level
`src` package was not importable from the pytest console entry point. The
then-current Docker gate had used `python -m pytest`; that invocation adds the
project root to Python's import path and reported 11 passing tests, masking the
fresh-user failure.

The Python product test gate now invokes the pytest console entry point, which
matches the delivered command after `uv run` selects the project environment.
A checked invariant prevents the gate and generated-project contract from
drifting back to different entry-point semantics. The accepted third-run
project is retained as failure evidence and is not treated as a runnable
delivery.

A fourth clean-account rehearsal confirmed that correction. The first
Developer commit reached the aligned gate in 590.2 seconds; contract, compile,
and lint passed, while the pytest console entry point correctly rejected an
unimportable `app` package before collection. Tester and Reviewer requested a
revision. The Developer then changed one file in 267.9 seconds, resolved that
recorded import finding, and advanced the suite to 15 of 16 passing tests. The
new remaining failure was distinct: the server could not create its configured
SQLite database because the parent data directory did not exist. With the
hard-coded two-iteration limit exhausted, SAT correctly failed without
delivery even though the second iteration had measurable progress.

The workflow iteration limit is now an explicit controller input bounded by the
team manifest. The advanced frozen evaluation remains at two iterations for
comparability; bare `sat` uses the manifest's three-iteration limit, allowing a
second evidence-driven revision without asking the user for an internal policy
choice. Repeated blockers, no-change revisions, resource limits, and all safety
or evidence-integrity stops remain unchanged. The complete 400-test offline
suite passes.

A fifth rehearsal began with another fresh non-root Linux account and the
public one-command installer at revision `a4c929d`. The user then invoked only
`sat`, completed guided first-run configuration, and described a small local
reading-list Web app in natural language. Run
`sat-20260825-005232-c8f61f0a` used
`deepseek/deepseek-v4-flash-vision-exp`. The Planner completed in 106.6
seconds. The Developer's first response omitted its required semantic JSON, so
the existing bounded repair path was legitimately used. The first
implementation then reached the aligned project gates, where pytest correctly
exposed an import defect.

On iteration two, the Developer changed one file in response to that evidence.
All four deterministic gates passed, eight generated-project tests passed, and
the independent Tester and Reviewer both accepted the result. SAT completed all
five user success conditions in 1,689 seconds and delivered clean commit
`6283aa12401e1e18272df5315bdc9ef92e2478da`. The exact generated setup and
test commands then succeeded outside the controller. The exact start command
bound the application only to `127.0.0.1`; manual HTTP checks added, edited,
finished, persisted across a clean stop and restart, and deleted a book. Both
application starts shut down cleanly, and no listener remained afterward.

The generated result therefore passed the functional Product Demo Slice on a
clean Linux account. A post-run resource audit then found all seven
session-scoped OpenClaw role containers still running. OpenClaw deliberately
retains these containers for session reuse, but SAT session keys are unique to
immutable runs; several older rehearsal containers also retained child test
processes. This invalidated the claim that the terminal product lifecycle was
complete even though the generated application itself had shut down.

SAT now performs bounded run-terminal cleanup after completed, failed,
interrupted, and exceptional workflows. It selects a container only when its
OpenClaw label has an exact controller-generated session key for that run and
one of its bind mounts is beneath the exact SAT-owned state or workspace path.
Broad name matching is forbidden, and a matching label outside those paths is
refused, preserving every other OpenClaw boundary.

A sixth rehearsal used another empty non-root account, the public installer at
revision `4f273fd`, bare `sat`, the same natural-language request, and the same
exact model. Installation, diagnostics, guided configuration, provider smoke,
confirmation, and run preflight passed. Run
`sat-20260825-022440-13824df0` recorded the Planner in 105.5 seconds and the
Developer in 595.5 seconds. The first implementation failed the aligned pytest
gate with an import defect. Tester returned malformed JSON in 106.2 seconds;
its one bounded repair returned a valid semantic response in 213.8 seconds.
Although neither invocation exceeded the 300-second Tester timeout, the old
controller added their durations and rejected the already-returned repair at
320 seconds.

That shared-deadline rule is now removed. Every initial response and optional
one-call repair receives the resolved per-role invocation timeout. The repair
does not escape resource control: both calls count against frozen total calls,
Agent duration, tokens, and estimated cost. The DeepSeek compatibility
supplement's conflicting fixed 600-second provider transport timeout is also
removed; the frozen controller timeout passed to OpenClaw is now authoritative.
Regression coverage reproduces a pair whose aggregate duration exceeds one
invocation timeout, separately proves that the total Agent-duration budget
still stops it, and prevents the compatibility supplement from restoring a
second transport cap. The complete 400-test suite passes. The sixth run also
confirmed terminal cleanup on the real failure path: SAT reported removing
three run-scoped containers, and an external exact-label audit found no
container belonging to the run.

A seventh rehearsal then started with another empty non-root Linux account and
the public installer at corrected revision `a032855`. The user invoked bare
`sat`, completed the same guided configuration, and supplied the same request,
success conditions, constraints, destination name, and exact
`deepseek/deepseek-v4-flash-vision-exp` model. Run
`sat-20260825-030006-255f469f` completed in 1,725 seconds. Planner completed in
89.3 seconds. The materialized runtime contained no independent provider
transport timeout; the first Developer commit completed in 577.1 seconds and
reached a real pytest failure. Tester completed in 109.0 seconds. Reviewer's
80.9-second response needed one bounded repair; the independent 81.1-second
repair succeeded and the controller chose `revise` without a shared-deadline
false failure.

On iteration two, Developer completed in 402.8 seconds and used one valid
115.3-second response repair. The controller verified two changed files, all
four deterministic gates passed, Tester completed in 88.9 seconds, Reviewer
completed in 93.4 seconds, and the decision was `accept`. SAT delivered clean
commit `8fa75927662b515fe5c57ed72acf5a4f8b4c3c2d` with 5/5 acceptance results.
The run used nine Agent calls, two bounded repairs, 1,637,869 milliseconds of
Agent time, 82,666 input tokens, and 31,727 output tokens, all on the frozen
model without fallback.

The exact delivered setup command succeeded, and the exact delivered test
command passed 21 tests. The exact start command listened only on
`127.0.0.1:8000`. HTTP form checks added, edited, marked finished, persisted
across a clean stop and restart, and deleted a book. Both starts shut down
cleanly and released the port. SAT reported removing seven run-scoped Agent
containers before returning control; an external exact-label audit found zero
live or stopped containers for the run, while the count of unrelated OpenClaw
sandboxes remained eleven. Credential scans of the trace, terminal record, and
delivered project were clear. The setup command generated an untracked
`uv.lock`; this is a non-blocking reproducibility observation because the
accepted delivery commit itself was clean and all promised commands and user
outcomes passed.

This confirms the complete Product Demo Slice on a fresh Linux account. The
current product supports small greenfield Python 3.12 projects; its bounded
clarification records explicit user input and is not yet an adaptive
requirements Agent. The task-manager contract remains isolated to the
advanced evaluation surface.

The advanced `prepare-benchmark`, `preflight`, and `run` commands remain a
separate evaluation surface and are not part of the expected product demo.
The acceptance contract is
[`docs/product-demo-slice.md`](docs/product-demo-slice.md).

## Implemented and Offline Verified

- Reproducible toolchain setup and diagnostics;
- Unified validation, benchmark-preparation, preflight, and `sat run` CLI;
- Versioned team manifest and validation;
- Versioned `TeamPlan`, `AgentSpec`, and `ModelRoutePlan` contracts with
  validation for dependency cycles, unknown references, write ownership,
  permission profiles, quality independence and coverage, model
  authorization, concurrency, iteration limits, and minimum call feasibility;
- Exact compilation of every fixed evaluation fixture into the same
  run-scoped contract, including frozen TaskBrief binding, Agent timeouts,
  dependency waves, workspace scopes, model route, budget, and manifest
  provenance;
- Versioned, hash-chained `RunEvent` persistence with run-state head anchoring,
  controller lifecycle and Agent attribution, safe summaries, and renderer
  visibility filtering;
- Versioned `ControlCommand` requests and terminal resolutions with typed
  targets, command-specific safe boundaries, optimistic revisions, immutable
  metadata, and predecessor-digest verification;
- Versioned, explicitly authorized Adaptive Planning requests; strict
  question-or-proposal responses; high-value focused questions with suggested
  and custom answers; controller validation and bounded semantic repair;
- Task-defined proposal compilation into confirmed requirements, adaptive
  implementation intent, least-privilege AgentSpecs, a strict model route,
  dependency waves, per-Agent timeouts, and aggregate controller budgets;
- Task-proportional Adaptive team validation with no bootstrap capability in
  the runtime team, exact task ownership and cross-Agent dependency alignment,
  and at least one downstream read-only quality path for every writer;
- Hash-chained Planning-turn evidence, immutable proposal revisions, exact
  user-approval digests, natural-language revision, safe structured limit
  edits, cancellation, and a complete plain-language overview;
- A replaceable OpenClaw subprocess adapter with stable fixed-role and
  run-scoped Agent sessions, explicit Agent ID and capability telemetry,
  version-pinned local and Gateway JSON parsing, and canonical
  `provider/model` telemetry;
- Sanitized OpenClaw Agent registry, permission checks, approved-Agent-only
  run-scoped configuration, non-root identity, strict per-Agent model
  selection, and offline preflight;
- A marked application-private OpenClaw binary plus explicit private config,
  credential, state, workspace, and Agent paths for every SAT invocation, with
  ambient OpenClaw settings neutralized and existing installations untouched;
- Exact run-scoped Agent-container cleanup on normal, failed, interrupted, and
  exceptional workflow exits, guarded by both session identity and SAT-owned
  mount provenance;
- Confirmed task-brief and handoff-envelope contracts;
- Fixed-role and task-defined capability minimum-context prompts, strict
  semantic JSON response parsing, dynamic identity/task/route/timeout binding,
  controller assembly of persisted envelope, Git, test, and scope facts, and
  one independently timed semantic response repair;
- Concrete phase-artifact and Agent-telemetry contracts with contextual
  validation;
- Immutable phase artifacts, handoffs, command output, Agent output, canonical
  paths, and SHA-256 references;
- Deterministic TeamPlan DAG scheduling with dependency readiness, exact Agent
  count, approved concurrency caps, per-Agent timeout propagation, fail-fast
  launch control, attributable skipped nodes, and ordered progress events;
- Shared-Git workspace safety that permits concurrent read-only Agents while
  making every workspace writer exclusive until isolated worktrees and an
  explicit integration protocol exist;
- Persisted run lifecycle with a write-once `team-plan.json`, validated
  transitions, atomic replacement, optimistic concurrency checks, cross-file
  digests, fixed-fixture provenance, and integrity-checked recovery;
- Safe detached standalone-clone creation and chained iteration snapshot
  verification;
- Frozen task-management TaskBrief, deterministic seed commit, independent
  acceptance suite, shared content-pinned Python image and dependency lock,
  per-run immutable local image identity,
  fixed quality-gate manifest, and independent acceptance suite;
- Docker-only production gates with no network, read-only workspace execution,
  non-root identity, fixed commands, resource limits, timeouts, and bounded
  output;
- The complete function-specialized workflow: Planner, Developer, controller
  snapshot, deterministic gates, independent Tester and Reviewer with
  configurable dispatch concurrency, decision, and launch-policy-bounded
  evidence-driven revisions;
- Bounded command-output diagnostics for verification, correct read-only
  source visibility, and controller-only Agent invocation policy;
- Explicit deterministic command coverage, `pending_review` manual criteria,
  Reviewer scope attestation, and controller-owned evidence resolution;
- Pre-call Agent invocation limits and post-call token, duration, and
  estimated-cost stop thresholds;
- Thread-safe aggregate budget reservations shared by fixed and task-defined
  execution, including post-call usage retention and explicit unpriced or
  missing-token counters;
- Checked-in capability defaults and fixed-role compatibility invocation
  timeouts, approved per-Agent Adaptive timeouts, optional global override,
  frozen resolved run policy, and configuration-schema migration from the
  former scalar timeout;
- Explicit completed and failed terminal outcomes with machine-readable and
  human-readable reports;
- Remote one-command Linux/WSL bootstrap into an owned user-local application
  directory, plus the pinned toolchain, locked environment, fixed Docker image,
  stable launchers, update validation, and a checkout-based contributor path;
- A versioned OpenClaw sandbox image plus install-time and run-time restricted
  probes that require a real tool helper to execute and reject a container that
  merely exists or starts momentarily;
- Automatic startup checks for platform, architecture, unprivileged identity,
  project-parent writability, required commands, SAT's pinned private
  OpenClaw, Docker daemon, Linux-container image, storage, and launcher
  visibility;
- Integrated first-run and repeatable model configuration with private,
  atomic, schema-versioned secret-free defaults, optional authorized provider
  smoke checking, and no invented zero-cost estimate when prices are unknown;
- Natural-language request capture, explicit Python execution-profile
  confirmation, user-provided success conditions and constraints, dynamic
  TaskBrief construction, concise destination confirmation, and authorization
  before model calls;
- Automatic private user-state roots, collision-resistant run IDs, confirmed
  TaskBrief materialization, trusted source creation, isolated workspaces, and
  write-once evidence;
- Controller-backed role, elapsed-waiting, Git-snapshot, quality-gate,
  independent-review, decision, revision, completion, and failure progress;
- Accepted-result-only delivery through a same-parent staging directory into a
  new non-overwriting project child, followed by exact setup, start, and test
  commands from a validated project-owned argv manifest;
- Guided one-command uninstall with preservation defaults, pre-removal export,
  separate configuration/data/private-provider-state purge choices,
  managed-application removal, and preservation of every other OpenClaw
  installation;
- Offline end-to-end coverage for success, revision, response repair,
  invalid-response failure, timeout, evidence tampering, non-convergence,
  iteration exhaustion, no-change failure, missing model or token telemetry,
  cost exhaustion, and trusted sandbox-runtime loss classification.

## Current Fixed Evaluation Team Paths

[`configs/teams.json`](configs/teams.json) defines three comparable topologies.
The configuration owns membership and initial stage ordering; the Python
controller owns dynamic revision and termination decisions.

These manifests are fixed evaluation fixtures. The current product path also
uses `function_specialized`, but it no longer passes that definition directly
to run control: SAT compiles it into the same `TeamPlan` contract that adaptive
teams will use. The target product design derives that plan from the task
instead of selecting a fixture.

| Configuration | Purpose | Implementation status |
| --- | --- | --- |
| `single_agent` | One-pass baseline | Phase 3 |
| `function_specialized` | Planner, generalist implementation, independent testing and review | Phase 1 implemented and provider-validated |
| `implementation_domain_specialized` | Parallel frontend/backend work plus integration | Phase 3 |

## Not Yet Available or Completed

- An independent fresh-device rehearsal and live demonstration outside the
  development host;
- Activation of the implemented Adaptive Planning interaction in bare `sat`;
- Multi-iteration dynamic quality convergence, lifecycle/final-report
  integration, and bare-`sat` activation around the implemented dynamic
  runner;
- Compact, standard, and detailed visibility backed by persisted per-Agent
  events;
- User guidance, correction, pause, resume, interruption, and cancellation
  through a controller-owned control channel;
- Multiple secret-free model profiles, per-task/per-stage/per-Agent routing,
  authorized automatic resolution, or recorded runtime switching;
- Generated-project execution profiles beyond the current local Python 3.12
  profile;
- Semantic provider/auth validation beyond the explicitly authorized minimal
  smoke check;
- Automatic CLI resume of an interrupted run;
- Executable `single_agent` and `implementation_domain_specialized` workflow
  paths;
- Repeated comparative trials, human rubric scoring, and topology selection;
- Additional product execution profiles and their independent quality
  contracts.

The current `sat run` command starts from a confirmed `TaskBrief`, requires a
fresh run ID, and intentionally does not infer that an unrecorded external
action succeeded after interruption.

## Next Milestone

The next Phase 3C batch connects the implemented dynamic schedule results to
run lifecycle transitions, iteration decisions, revision feedback, final
reports, cleanup, and delivery. It then activates the completed Planning
interaction and dynamic runtime together in bare `sat`.
Later Phase 3
batches add richer progress and controls, then model routing. Fixed-topology
comparison moves to Phase 4 so it can remain a controlled baseline rather than
defining the product's permanent role layout.
The detailed sequence and acceptance criteria are in
[`docs/adaptive-orchestration.md`](docs/adaptive-orchestration.md).

The development route and evaluation policy are defined in
[`VISION.md`](VISION.md#development-route).

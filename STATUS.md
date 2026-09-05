# Project Status

**Current milestone:** Phase 3E implemented; twenty fresh installed adaptive rehearsals include two accepted strict-route Adaptive Planning deliveries and one provider-backed foreground-control rehearsal; the latest delivery-boundary, Review-recovery, and cold model-inspection repairs are live-validated

**Last updated:** September 5, 2026

This document records what the repository implements now, what evidence
supports that claim, and what remains unavailable. It does not redefine the
product, architecture, experiment, or roadmap; those decisions belong to
[`VISION.md`](VISION.md).

The current development head implements configuration schema v8 model metadata
with attributable price/context sources, task-scoped route snapshots, one
explicit per-task USD authorization and optional deadline prompt, and separate
controlled-evaluation versus ordinary-user resource authority. Product Planning
and execution no longer use a fixed wall-clock work limit. SAT resolves a
provider/model-aware renewable inactivity lease, observes private stream and
attributable tool lifecycle without persisting their content, visibly separates
suspected stall, grace, recovery, degraded observation, and terminal stall, and
persists typed content-free evidence for Planning and runtime Agents. One
controller-priced ledger now covers Planning through terminal execution,
standard progress exposes spend and remaining authorization, and final reports
include attributable per-call cost evidence. Terminal JSON, Markdown, and the
ledger are prepared and published as one rollback-capable bundle before the
controller transition. The bare product entry now persists a typed
task-admission report before Planning and an approved-plan report before source,
workspace, or runtime-Agent creation. The first report includes full SAT
release/source identity, local/schema/model/task/budget facts, and exactly one
foreground managed-channel observation; the second covers every approved
route, Agent authority, runtime policy, sandbox, source, and delivery boundary.
Every new completed, failed, or cancelled terminal report embeds the same typed
SAT release/source/install/channel/artifact/schema identity and commits it with
the Markdown view and model-spend ledger through the rollback-capable terminal
bundle. Startup diagnostics now discover the tightest Linux/cgroup memory and
PID headroom, compare it visibly with the policy ceiling without treating that
ceiling as a minimum, make the existing disk guard blocking as declared, and
read-only inventory existing containers proven to mount SAT-owned state. The
approved-plan restricted container probe remains the readiness authority for
actual sandbox execution. These machine checks do not cap Agent count, call
count, or total model-work time. A failed task-admission or approved-plan check
can be repaired and rechecked in the same foreground task. Changed input
digests append an immutable revision that refreshes only the invalidated result
and its transitive dependents; an unchanged retry creates no duplicate
evidence. Rechecks now load the verified latest report from disk, so a new CLI
process can preserve unchanged evidence while refreshing model/configuration
changes and dynamic plan-graph additions, removals, or redefinitions. Every
live SAT-launched OpenClaw child also has a private PID/start-time/process-group
lease. A real controller-kill test proves that a new process can distinguish
and reclaim the exact orphan while holding a Linux pidfd across signalling;
active owners and PID-reused processes are not signalled, and sandbox recovery
additionally requires the exact leased session under SAT-owned state. Automatic
continuation of an interrupted Agent workflow
and fresh provider/device cost, liveness, self-check, process cleanup, and
managed-release validation remain incomplete; the corresponding issues are not
closed by offline evidence.

The response compiler now distinguishes transport, schema, contextual, and
evidence-grounding failures; a missing user-owned decision remains on the
typed Planning-question path instead of entering correction. It deterministically
removes only schema-forbidden fields that cannot carry controller/evidence
authority and records each normalization. Profile-criterion ID collisions now
remove only redundant echoes; a task-specific relation needed for requirement
coverage receives a deterministic non-reserved ID while the canonical profile
binding and all model-owned verification relationships remain intact. A
Reviewer response is also compiled against the exact TaskBrief-owned boundary
scope before nested boundary content is validated. Extra checks outside that
scope are removed with an explicit normalization instead of creating new
acceptance obligations or model correction calls; checks inside the approved
scope retain strict completeness, uniqueness, and evidence-grounding rules. A
remaining targetable model-owned failure creates a diagnostic-v2 invariant ID,
structured affected-entity subjects, precise model-owned JSON-pointer authority,
and a SHA-bound
`semantic_correction_v1` envelope; the controller retains every unrelated
field, freezes a writer's verified Git result, and records each request and
outcome. Planning relational validation no longer infers identity or correction
scope from human error prose; unclassified relations fail closed. Product
Planning and dynamic execution continue only after measurable
improvement within the task budget, while the fixed evaluation surface retains
its explicit zero-or-one cap. Transport, unlocated, repeated, invalid-envelope,
and non-improving failures stop without a full-response retry. Fresh
provider-backed Planning correction evidence now exists. Exact offline replay of
the latest distinct writer-coverage then verifier-authority failure now yields
different fingerprints and narrows correction from four proposal containers to
`tasks`, followed by the exact criterion `verification_agent_ids`; a corrected
Reviewer provider run remains pending.

A fresh installed run at `54b0275` reached approved Planning, a clean writer
commit, and five passing deterministic gates. Its Reviewer returned ten
criterion assessments, but also supplied four boundary checks for each of five
profile criteria whose frozen TaskBrief scope was empty. Two reused fragments
failed nested schema validation and caused an unnecessary correction call; that
call returned no JSON, so SAT withheld delivery. Exact offline replay of the
preserved first response against the current compiler now validates all ten
assessments in one pass and records removal of the five unapproved arrays. This
proves the captured regression path offline, not a corrected provider journey;
the issue remains open until a fresh run crosses Review without that call.

Doctor, formatter, lint, and the complete **941-test offline suite** pass with
the diagnostic-v2 change, including the exact captured two-invariant Planning
sequence and a three-call correction regression. The managed stable resolver
also distinguishes an unpublished/inaccessible HTTP 404, refuses silent dev
fallback, and explains the explicit dev-channel choice. Managed staging removes
the caller's active `VIRTUAL_ENV` only from the install child so the candidate
owns its `.venv` without changing the caller or other environment. Provider-backed
validation of the corrected journey remains pending. An explicit dev ref now
retargets an existing dev installation through the same staged activation and
rollback transaction; an unchanged resolved revision remains a no-op. Managed
installation now claims the immutable final release path before creating the
Python environment or isolated OpenClaw runtime, so path-bound entry points are
never relocated after verification. Activation executes the final `sat`
launcher and restores the prior link, installation record, and transaction-created
launchers if that probe fails.

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
and concurrency above the user-approved host setting before Agent creation.
Controlled evaluation additionally validates frozen Agent/call/iteration
limits; ordinary product plans do not inherit those limits. Recovery verifies
the exact TaskBrief binding, TeamPlan digest, fixed manifest version, fixed team
digest, resolved time authority, and cross-file run metadata.

`RunEvent` is also an executable, append-only contract. Every current workflow
progress update is persisted with a contiguous sequence, lifecycle revision,
phase, Agent identity when applicable, visibility class, and predecessor
digest. `run.json` atomically anchors the latest event, so recovery detects
missing, reordered, modified, or extra events, including a changed tail.
The dynamic scheduler and runner now project every approved Agent through
queued, ready, running, provider-waiting, provider activity, tool lifecycle,
suspected stall, grace recovery, degraded observation, bounded-repair,
completed, failed, or blocked transitions. Events include safe activity,
dependencies, capability, stage, approved model, attempt and duration where
applicable, invocation evidence, and aggregate budget snapshots. Compact,
standard, and detailed
filtering consumes the same event contract. Configuration schema v7 persists
the selected visibility together with secret-free model profiles and route
policy without changing renderer semantics, and bare `sat` applies the selected
visibility to the product renderer; standard remains the default.

`ControlCommand` and its controller-owned revision store define and preserve
the request, target, controller-assigned mailbox sequence, safe application
boundary, status, consequence, plan or lifecycle result, and provider-cost
caveat for guide, correct, pause, resume, interrupt, and cancel. The normal CLI
now exposes those commands through a foreground slash-command palette. The
dynamic scheduler polls the mailbox, stops new launches at cooperative
boundaries, applies guidance to the next invocation, and requests best-effort
termination only for exact SAT-owned OpenClaw process groups. Every receipt and
resolution is correlated to its command revision and digest in `RunEvent`.

The Phase 3B Planning engine is also implemented. A versioned `PlanningRequest`
proves explicit model-work authorization before the first invocation. The
read-only bootstrap Planner may return either one decision-value question with
two or three suggestions and a custom-answer path, or one complete proposal.
Planning schema v4 records each question's decision category and owner, missing
evidence, material consequences, and alternatives. The controller enforces the
fixed responsibility matrix before showing a question, and every answered
question must resolve to one proposal decision with unchanged ownership.
Strict proposal validation covers stable requirement IDs, explicit non-goals,
decision-owned assumptions, acceptance criteria and their requirement links,
Agent work assignments, task ownership, dynamic Agent responsibilities,
dependencies, workspace scopes, independent quality coverage, user-approved
concurrency, proposed iterations, per-Agent workload classes, the configured
model route, and resource authority before the proposal is shown. Product plans
resolve every per-Agent wall-clock value to zero and record
`provider_activity`; workload-to-timeout mapping and Review scope floors remain
only in controlled evaluation.

The ordinary-user interaction supports free-form answers, natural-language
replacement revisions, safe edits to maximum concurrency, iteration count, and
Agent model profile, cancellation, a complete plain-language overview, and
explicit approval. `PlanningStore` persists the authorized request,
hash-chained model turns including rejected response evidence, immutable
proposal revisions, exact approval digests, and the controller's per-Agent
time-authority resolutions. Product resolutions record zero with a
provider-activity source; controlled evaluation retains workload, policy
envelope, final seconds, and any Review scope evidence. Planning turns also
retain content-free provider-liveness evidence. Approval promotes the
validated preview into an authorized confirmed `TaskBrief`, adaptive
implementation plan, and executable `TeamPlan`.
The resulting `ApprovedPlanningResult` revalidates those exact digests and
cross-plan bindings at its execution boundary, so mutated approved inputs
cannot be substituted before runtime.
The bootstrap Planner still cannot create an Agent or advance run state.

The approval overview now separates user decisions, Planning recommendations,
Agent/Controller autonomy, and non-negotiable Controller policy. It renders
requirements and non-goals, visible assumptions, each
requirement-to-criterion-to-writer-to-independent-verifier path, and every
Agent's inputs, expected output, and handoff, followed by risks and the failure
and delivery boundary. Current proposals fail closed when any trace is missing
or a writer claims its own independent verification. Planning schema v2 remains
readable without changing its canonical serialization; safe edits retain the
legacy version rather than relabeling it as v3. Ambiguous-task provider behavior
and real-user comprehension are not established by these offline contracts.

Planning criterion ownership is now explicit. The response schema requires
every Planner-defined criterion to have implementation-task coverage and
accepts only stable criterion-ID syntax. Before that context-free coverage
check, the policy-aware response boundary removes and audits definition echoes
whose exact IDs belong to the active controller profile; task bindings remain,
and no model-authored profile text or Review boundary becomes authoritative.
The preview separately allows those bindings, rejects unknown IDs, materializes
only canonical profile definitions, and preserves valid bindings in the
approved implementation plan. Dynamic prompt validation rechecks those task
references against the exact controller-materialized TaskBrief rather than
trusting a standalone model response.

The first Phase 3C runtime boundary is also implemented. Dynamic execution
requests and telemetry use an approved run-scoped Agent ID and capability;
fixed-role identity remains compatibility metadata only for the existing
evaluation workflow. Capability-specific templates compile the exact approved
responsibility, assigned tasks, dependencies, permission profile, model route,
and time authority into minimum-context prompts. Response parsing rejects
mismatched Agent identity, capability, session, task ownership, model, or time
authority.

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
their least-privilege capability profiles, and binds every Agent to the
verified workspace and its exact authorized route set. Strict evaluation
disables fallback; policy routing can expose only the primary and ordered
fallbacks already frozen for that Agent. Exact-label sandbox cleanup can derive
all owned session identities from those AgentSpecs. Adaptive
validation excludes the bootstrap Planning and Clarification capabilities from
the runtime team, requires every writer to own work, and allows a small task to
use one writer plus one independent quality Agent instead of imposing a hidden
Tester/Reviewer pair. Every quality Agent must depend on every writer path, so
verification cannot start against an intermediate commit. Separate quality
Agents may be parallel peers or form an explicit handoff chain on that same
immutable commit. Fixed evaluation fixtures retain their explicit dual-quality
topology.

The Phase 3C dynamic runner is now implemented behind the general DAG
scheduler. The scheduler remains the only owner of readiness, launch order,
bounded concurrency, and shared-Git writer exclusion. The runner invokes only
the supplied approved `AgentSpec`, preserves its exact authorized model set and
time authority, applies improvement-gated targeted semantic correction,
accounts for every call in one thread-safe aggregate ledger, and persists raw
output plus telemetry before a
post-call budget rejection stops the schedule. Agents cannot create another
Agent, change dependencies, reorder work, or extend time authority.

Each dynamic writer starts from the controller's current clean commit, leaves
a clean descendant commit, and is rejected for changes outside its approved
workspace scope. Read-only quality Agents must leave the same immutable commit
and clean tree. Deterministic gates execute exactly once per iteration even
when Tester and Reviewer Agents run concurrently. Their reports share the same
controller-owned evidence and final commit; when a justified small team has no
Tester, the controller persists the deterministic TestReport itself. Dynamic
source-to-target and terminal handoffs are write-once and include attributable
phase and execution evidence. Offline integration tests exercise real Git
commits, parallel quality, semantic correction, missing telemetry, budget
exhaustion, read-only mutation, and write-scope violations.

The Phase 3C adaptive lifecycle coordinator is now implemented. It consumes
only an exact `ApprovedPlanningResult`, creates the generic `RunController`,
and lets `DagScheduler` remain the sole authority for readiness, order, and
parallel launch. When the first quality Agent becomes ready, a synchronous
controller checkpoint verifies the complete writer commit chain, records one
aggregate Git snapshot, and enters `VERIFYING` before that Agent starts. Tests
and reviews therefore cannot run first and have lifecycle evidence filled in
afterward.

The coordinator aggregates every approved writer, Tester (or the controller's
deterministic report), and Reviewer into one `IterationRecord`; resolves
accept, revise, terminal failure, iteration exhaustion, and repeated blockers;
and produces one integrity-checked JSON and shared Markdown final report.
Revision feedback contains only controller-derived blocking findings and test
reasons, is bound to the previous output commit as the next iteration's start,
and is distinct from each downstream Agent's current snapshot commit. Offline
end-to-end tests cover one-pass acceptance, evidence-driven revision followed
by acceptance, unchanged-blocker termination, pre-snapshot Agent failure, and
a valid Tester-only quality topology.

The Planning interaction and adaptive execution backend are now activated
atomically by bare `sat`. The normal launcher creates one read-only bootstrap
runtime, preserves Planning evidence separately, presents the validated
overview, prepares an execution source only after approval, materializes only
the approved run-scoped Agents, executes the dynamic lifecycle, cleans both
bootstrap and execution sandboxes, and uses the existing accepted-result
delivery boundary. It cannot approve a dynamic plan and silently execute the
old fixed team. During execution it accepts live visibility changes,
prospective guidance, cooperative pause/resume, best-effort Agent interruption,
terminal cancellation, and requirement correction. A correction preserves the
superseded run and opens a fresh Planning overview with a new run ID; it never
mutates approved evidence in place. Safe concurrent writers beyond the current
serialized Git chain, durable process-restart resume, and a secondary-process
control client remain pending.

Phase 3E adds a single canonical source for secret-free model profiles and
deterministic routing. The controller resolves Agent edit, stage override,
capability override, default-profile support, then eligible-profile priority;
the bootstrap Planner may describe capability needs but cannot authorize a
model. The Planning overview exposes every primary route, selection reason,
known pricing, and approved fallback before user approval. TeamPlan validation,
run configuration, prompts, response telemetry, budget feasibility, and
runtime preflight all bind those exact assignments. Only an attributable
provider failure or typed provider stall can advance under an explicitly
approved provider-failure switch condition; the failed call, liveness evidence,
and switch remain evidence, while semantic repair stays a separate mechanism.
Offline routing and dynamic-runner tests cover authorized and refused switches.
A provider-backed run using two planned routes remains pending.

## Product Readiness Boundary

The primary CLI now implements the adaptive Product Journey in code. A normal
user runs `sat`; SAT checks the device, guides model configuration, asks what
to build, confirms the installed Python execution profile and destination,
obtains explicit Planning authorization, conducts bounded clarification,
shows the complete task-defined team and controller limits, supports revision
or safe edits, and creates an execution run only after exact approval. It then
executes the approved TeamPlan and delivers only an accepted clean Git result
with project-specific commands.

The normal first-use path starts with one strict selected model profile; an
advanced user can configure multiple capability-authorized profiles and policy
routing before Planning. The product path also uses a user-configurable
controller progress renderer. Its active foreground control channel and model
routing are implemented and offline verified, including cancellation and
correction reports, one-shot guidance, safe pause/resume checkpoints, live
visibility changes, process interruption, deterministic route resolution,
explicit provider-failure switching, event correlation, and exact-owned
cleanup.

This is not yet release-stable evidence. Two earlier WSL rehearsals completed
managed installation or update, startup diagnostics, isolated provider setup,
request confirmation, internal run materialization, and the Planner stage.
Both then reached a stopped Developer sandbox before any workspace tool could
run, so no project was delivered. The second run was correctly classified as
`dependency_unavailable` instead of a source-code failure.

The first fresh installed rehearsal of the activated Adaptive Planning path
used the public installer, a new non-root account, bare `sat`, and
`deepseek/deepseek-v4-flash-vision-exp`. Device checks, configuration,
provider smoke, ordinary request capture, authorization, and Planning preflight
all passed. The Planner returned a task-defined proposal, but one expected
directory was written as `tests/` and one Agent repeated the destination name
as its workspace scope. The bounded repair corrected the permission scope but
retained the harmless trailing slash, so strict validation stopped before any
execution Agent or project workspace was created.

SAT now canonicalizes only safe, unambiguous Planning path presentation and
infers a missing response discriminator only when exactly one response body
makes it certain. Raw output remains immutable, every normalized field is
recorded, destination-shaped workspace scopes remain rejected, and unsafe or
ambiguous values still follow strict repair/failure policy. The complete
offline suite covers the observed response without consuming a repair call.

A second fresh installed provider-backed run confirmed that fix: its Planning
response passed on the first call, the user approved one task-defined
Implementation Agent followed by one independent Review Agent, and the writer
completed a clean seven-file commit. The controller verified the commit and
entered verification, but stopped before the Review provider call because the
1,172-character immutable WorkResult summary exceeded a separate 1,000-
character downstream prompt field. No destination was delivered.

The controller now keeps complete Agent summaries in immutable artifacts while
deriving deterministic bounded projections for scheduler status and downstream
prompt context. A truncated projection names the original character count and
SHA-256 and states that full text remains in artifact evidence. A regression
with a 4,045-character WorkResult completes both downstream quality Agents,
while the stored WorkResult remains unchanged.

A third fresh installed adaptive run confirmed the complete controller path at
that revision. Planning used one bounded repair, the user approved an
Implementation Agent followed by an independent Review Agent, and two
evidence-driven iterations completed. The first review correctly requested a
README revision; the second accepted all controller evidence. SAT recorded
11/11 criteria passed, delivered a clean 14-file commit, reported exact project
commands, and removed all four run-scoped containers. The delivered setup and
17 project tests passed, as did duplicate grouping, exclusion, minimum-size,
and nested symlink fixtures.

Independent post-delivery acceptance nevertheless found two product defects.
Selecting a directory symlink as the top-level scan root followed its target,
contradicting the unqualified request and README claim that symlinks were never
followed. The model-authored tests covered nested symlinks but not that entry
boundary, and independent Review did not challenge it. The documented setup
also generated an untracked `uv.lock`, leaving first-use Git state unexplained.
The controller's completed result is therefore retained as failed product
acceptance rather than promoted to demonstration evidence.

The shared quality prompts now require Planning, implementation, and independent
Review to cover every relevant entry boundary of an unqualified prohibition or
safety guarantee and to reject one concrete counterexample. The Python product
contract also requires the root setup environment to be ignored and `uv.lock`
to be either a bounded regular file in the accepted snapshot or explicitly
ignored. These are task-independent corrections; no duplicate-finder-specific
gate was added. A fresh installed run must confirm both changes before the
adaptive journey is called successful.

A fourth fresh-account adaptive rehearsal tested those changes through the
public installer and bare `sat` with the same duplicate-finder request. Planning
needed one bounded proposal repair, then the user approved one Implementation
Agent followed by one independent Reviewer. Iteration one correctly reached the
project-contract and pytest import failures. The Reviewer requested revision
for those deterministic defects but supplied only a summary assertion of all
14 criteria; it did not challenge the top-level symlink boundary or the
implementation's singleton hash groups.

The second Developer invocation committed a corrective revision and returned
one complete valid semantic object after explanatory text containing JSON argv
arrays. The old raw-object normalizer incorrectly treated those arrays as a
second response candidate and spent a repair call. That repair contained
unescaped quotation marks and was invalid JSON, so the controller stopped with
`artifact_invalid`, delivered nothing, and removed all three run-scoped
containers. Independent inspection of the preserved commit also confirmed
that the root symlink and singleton-group defects remained and that the test
argv still differed from the exact generated-project contract.

The response boundary now treats non-object JSON arrays as presentation when
there is exactly one semantic object, while still rejecting every additional
object, including one nested in an array. Dynamic Review now requires an exact
criterion-by-criterion assessment set with concrete adversarial checks and
evidence; blocked assessments and blocking findings must reference the same
criterion. Review can run bounded foreground probes against read-only source
and `/tmp` fixtures in its no-network sandbox, without converting its
self-directed result into controller-owned deterministic evidence. These are
generic protocol and quality-boundary corrections. The Python profile now also
places its fixed setup and test argv in the controller-owned Planning
constraints, and both the starter guidance and implementation prompt require
the writer to preserve them while replacing only the project-specific start
placeholder.

A fifth fresh-account rehearsal used the public installer, bare `sat`, and the
same model. Planning needed one bounded repair after the first proposal gave a
quality task to the writer, then the user approved one Implementation Agent
followed by one independent Reviewer. The writer returned one valid semantic
WorkResult plus a separately visible OpenClaw tool warning. The old transport
adapter incorrectly required exactly one visible text payload and requested a
semantic repair even though only one response object existed. The repair
succeeded and deterministic verification began.

That rehearsal exposed three independent controller defects. README headings
such as `Usage` did not satisfy a validator that looked for the literal word
`start`; clean-tree pytest could not import the generated src-layout package
even though the post-setup command passed 24 tests; and the progress renderer
showed success symbols for failed gates. The Reviewer then had read-only source
and foreground execution but no coherent way to create a temporary probe
script. OpenClaw rejected its attempted inline interpreter command, and the
routine 300-second timeout expired while it was responsible for all ten
criteria. SAT delivered nothing and terminal cleanup completed.

Independent inspection also found that the preserved candidate mishandled a
top-level directory symlink, returned success for a missing path, and required
an undocumented extra operand on its nominal start command. These remain
product-specific failure evidence; the resulting corrections are generic.
SAT now aggregates all visible transport text in order while requiring exactly
one semantic response object, stops hidden lifecycle heartbeats when the exact
Agent terminates, renders gate outcomes truthfully, and shows bounded Planning
heartbeats and repair checkpoints. Review scope supplies a controller-owned
timeout floor, so 6–10 criteria resolve to at least 450 seconds and 11 or more
to 600 seconds under the current policy. The pinned quality image includes
`uv` for the exact generated commands.

The Python profile now accepts ordinary documentation headings but requires
the exact setup, direct no-extra-argument start, and test commands. It requires
both clean-tree and post-setup pytest to work, including explicit src-layout
import configuration.

The sixth fresh-account rehearsal confirmed that Planning heartbeats, bounded
repair state, completed-Agent heartbeat termination, truthful gate symbols,
the exact generated-project command contract, and criterion-scope timeout
resolution all reached the installed path. One Implementation Agent completed
in a single call, produced a clean commit, and passed all four deterministic
gates. Its downstream Reviewer was responsible for 11 criteria and still
timed out at the 450-second substantial allowance before returning a semantic
report, so SAT correctly withheld delivery and cleaned its run containers.

The Reviewer prompt had two contradictory boundaries: a general prohibition
on modifying files and a final prohibition on mutating tools both conflicted
with the middle instruction to use the write tool for `/tmp` probe scripts.
The live session never used write and instead spent tool turns on heredoc
commands that deterministic preflight correctly rejected. The prompt now says
that project source is read-only, and it asks the Reviewer to consolidate
related probes rather than repeat commands. The provider-backed
11-criterion timeout at the substantial allowance now maps 11 or more criteria
to the existing complex allowance. The separate 10-criterion timeout only
proved that routine was insufficient, so 10 remains substantial; this does not
change coding, testing, smaller Review, call-count, or total-duration budgets.

A subsequent provider-backed authority probe found that the prompt-only change
still described an impossible tool boundary. The pinned OpenClaw runtime omits
the general `write` tool whenever a sandbox filesystem root exists, so removing
`write` from the deny list cannot expose it. The Reviewer completed semantically
through foreground `exec`, but its attributable session contained 11 `exec`
calls, zero `write` calls, rejected heredoc attempts, and a false final claim
that the requested write path had been used. That probe is failure evidence,
not a successful capability verification.

The runtime image is therefore versioned to `phase1-v4` and contains the
immutable `sat-probe-write` helper. It accepts only canonical direct children
matching `/tmp/sat-review-probe-*` with `.py`, `.json`, or `.txt`, enforces line
and total-size limits, creates with mode `0600`, and rejects overwrite,
symlinks, nesting, traversal, and partial-write residue. Reviewer explicitly
denies the nonexistent general `write` tool and uses the helper through its
available foreground `exec` surface. The full 656-test suite passes. A
restricted non-root, no-network, read-only-root container probe also confirmed
root-owned helper mode `0555`, caller-owned output mode `0600`, direct Python
execution, refusal of overwrite, symlink, and `/agent` targets, read-only
project enforcement, and terminal container removal. A fresh installed
provider authority probe then exposed a separate evidence-grounding defect:
the Reviewer returned a structurally valid accepted report claiming helper,
Python, Git, and boundary checks, while its exact attributable current session
contained zero tool calls and zero tool results. The semantic parser previously
validated criterion coverage and prose but had no controller-owned link from a
claim to an actual OpenClaw tool record. That accepted-looking response is
failure evidence and does not verify the helper or Review path.

The OpenClaw execution adapter now validates the returned session ID against
SAT's isolated session index, extracts only the latest exact current-prompt
turn, pairs tool calls and results one-to-one, and persists bounded sanitized
records with invocation-local IDs, hashes, outcomes, excerpts, and transcript
provenance. Raw session JSONL is not copied into run artifacts. Dynamic Review
response schema now requires every criterion assessment to supply one or more
bounded exact observable fragments from the current invocation. The model
cannot supply or predict a controller tool ID. SAT requires every fragment to
match at least one sanitized result, binds every matching current result, and
deduplicates repeated or overlapping selectors by controller-owned ID before
persisting the assessment. For `exec`, SAT records only the
direct executable plus a hash of the complete arguments, so helper/Python/Git
paths remain inspectable without persisting possible environment-assignment
values or the full command. A captured zero-call turn therefore cannot support
an accepted claim; an absent result enters the one bounded semantic repair,
while multiple real matches remain attributable evidence and invalid session
provenance stops at the safety boundary without another provider call.

A fresh installed probe against the first ID-bearing contract confirmed the
capture boundary and exposed why semantic bodies must not carry those IDs. The
controller recorded 20 actual calls, including successful `sat-probe-write` and
direct Python results with both required markers, while the model's four exact
observable fragments were paired with four incorrect guessed `tool-00N` values.
The parser correctly rejected the response, but requiring a model to count an
evolving tool loop and predict a controller-owned presentation ID was itself a
protocol defect. The first observable-only fresh retest then captured 13 actual
calls and all five supplied fragments matched current results, including helper,
direct Python, Git, and negative-boundary evidence. The old controller still
rejected the response because the ordinary clean-Git fragment occurred in both
an initial inspection and a final check. Both matches were real; requiring the
model to manufacture unique wording was another controller-owned mapping task.
The contract now binds all current matches and deduplicates repeated or
overlapping selectors. Offline regressions cover unrelated preliminary calls,
one-to-many resolution, zero matches, repeated and overlapping selectors, and
rejection of a model-supplied ID. A repeat fresh installed provider authority
probe and complete adaptive run remain required before this journey is
demonstration ready.

The next fresh-account adaptive run passed device checks, isolated
configuration, provider smoke, ordinary request capture, first-response
Planning, overview approval, one Implementation Agent, and all deterministic
quality gates. The writer produced a clean eight-file commit and its generated
suite passed 54 tests. The independent Reviewer then made 21 attributable tool
calls. Its first semantic response was invalid only because two finding paths
were absolute. The bounded repair corrected those fields and correctly reused
the unchanged probe fragments without repeating tool calls. The former
grounding logic searched only the repair invocation, so it rejected valid
evidence from the immediately preceding attempt and withheld delivery. This is
a controller protocol defect, not evidence that a more capable model is
required.

Adaptive Reviewer grounding now carries sanitized evidence forward only within
the same Reviewer, role stage, immutable commit, and bounded semantic-repair
chain. Persisted references use `(execution_attempt, tool_call_id)` identities,
so invocation-local IDs may safely repeat across attempts without ambiguity.
The model still supplies only exact observable fragments and never predicts an
attempt or tool ID. Regressions cover successful zero-call semantic repair,
same-local-ID attempts, invalid chain order, and attempted evidence substitution
outside the current chain. Replaying the preserved live attempts through the
corrected grounding boundary accepts all 12 criterion assessments and binds 38
references to attempt one while attempt two correctly contains zero tool calls.

The same run also exposed a separate delivery-command evidence gap. A Reviewer
probe had created a virtual environment beneath one mount path and later
observed it beneath another, so that environment was not relocatable; the
controller had not independently run every exact generated-project command
from a clean post-commit copy. Runtime image `phase1-v5` now contains a locked
offline wheelhouse. A new deterministic product gate rejects tracked drift and
unsafe Git entries, copies only committed regular files into fresh disposable
scratch, and runs the exact setup, test, and start argv with the network
disabled. Its tmpfs is executable only so project-local virtual-environment
entry points can launch; source and root filesystems remain read-only and the
container remains non-root, capability-dropped, and resource-bounded. A real
restricted container verification passed setup, all 54 project tests, and
start against image ID
`sha256:9e129565b0dea409d8808dbe58570701494db5181001a5480b40bf212f0013f4`.
The complete 667-test offline suite passes. A fresh installed provider rerun
remains required before declaring the adaptive journey demonstration ready.

That fresh-account rerun passed public installation, `phase1-v5` readiness,
device checks, isolated configuration, provider smoke, request capture, and
Planning authorization. Both the initial proposal and its repair selected a
coherent `impl -> reviewer` Agent DAG, but both also restated the independent
Review stage as reviewer-owned `TASK_REVIEW`. The internal `tasks` collection
incorrectly required every owner to be a writer, so the proposal was rejected
before the overview. No execution run, runtime Agent, workspace, or destination
was created. Repeating that hidden representation constraint in a repair did
not improve the proposal.

The task contract now permits explicit work assignments for every approved
runtime Agent. Quality-owned tasks preserve testing or review focus in the
overview and the exact Agent prompt; they do not create Agents, grant write
access, alter dependencies, expand criterion scope, or add model calls. The
approved `AgentSpec` DAG remains authoritative for those controls. Every writer
must still own work that covers every proposal-owned criterion, every task
owner must be an approved Agent, and cross-Agent task dependencies must agree
with the Agent DAG. The same binding validation runs during proposal parsing,
prompt construction, and runner startup. The exact preserved repaired response
now compiles unchanged with its original `impl -> reviewer` execution waves
and no additional model call. The complete 673-test offline suite passes. A new
fresh installed provider rerun is still required.

That next fresh-account run confirmed the quality-owned task fix at the real
Planning boundary, then exposed another hidden topology constraint. Both the
initial proposal and its repair selected implementation, testing, and Review
Agents and placed Review after Testing. `PlanningProposalBody` accepted the
DAG, but `TeamPlan` silently rejected any dependency between Testing and Review
with `testing and review capabilities must remain independent`. The repair saw
only that abstract error and repeated the same chain. No overview, execution
run, runtime Agent, workspace, or destination was created. The proposal also
described test-file creation in a task owned by the read-only Testing Agent;
the text could not grant writes but the mismatch was not visible enough.

Quality sequencing now belongs to the approved Agent DAG: Testing and Review
may be peers or one may consume the other's durable handoff, provided both are
read-only and transitively downstream of every writer. The dynamic runner test
executes `builder -> tester -> reviewer` on one immutable commit, passes the
Tester summary into the Reviewer prompt, runs deterministic gates once, and
records the exact handoff. The Planner contract now assigns all project-file
creation to implementation/integration Agents. The user overview derives a
write-versus-read-only authority line from each task owner's `AgentSpec`, so
task prose cannot grant mutation authority or conceal the actual boundary. The
two archived Planning responses now compile unchanged with their proposed
three-wave DAG. The complete 674-test offline suite passes. A new fresh
installed provider rerun remains required.

The next fresh-account run passed the public one-command installer, bare `sat`,
device checks, isolated configuration, provider smoke, request capture,
Planning authorization, bounded Planning repair, overview, and approval using
the exact `deepseek/deepseek-v4-flash-vision-exp` route. The approved plan used
one Implementation Agent followed by one Reviewer. Planning classified their
workloads as `substantial` and `routine`; controller policy resolved those
labels to 1,350-second and 600-second invocation timeouts and froze
`max_concurrency=1`. The writer committed eight tracked files. Deterministic
clean-copy verification then caught a real import failure in one subprocess
test while the generated project's exact post-setup test command passed.

The Reviewer performed 19 attributable read-only tool calls and returned the
correct `revise` verdict, all 13 criterion assessments, and one blocking
finding covering the import defect. The response was rejected because several
compact JSON evidence fragments differed from pretty-printed results only in
outside-string whitespace, three exact claims referred to controller command
output rather than Reviewer tool output, and the sole finding omitted a
redundant `criterion_ids` list. Its bounded repair returned prose instead of a
semantic object, so the run correctly failed without delivery and terminal
cleanup removed both run-scoped containers.

Reviewer grounding contract `semantic_body_v3` now prefers exact fragments but
permits only RFC JSON whitespace differences outside quoted strings for keyed
JSON. It can bind same-iteration deterministic command stdout/stderr and
persists the actual command IDs alongside attempt-qualified tool IDs. When one
unscoped blocking finding uniquely explains all otherwise-uncovered blocked
criteria, the controller binds that relationship; multiple unscoped findings
remain invalid. Exact replay of the preserved first response now retains its
`revise` verdict, resolves all 13 assessments, binds 11 tool references and 3
command references, and scopes the finding to `AC_TESTS` and `AC_TESTSUITE`
without another model call. The complete 677-test offline suite passes. A
fresh installed provider rerun remains required to verify the complete
revision loop and delivery path.

The following fresh-account run reached delivery, but independent post-delivery
validation found that a user-selected root symlink was followed despite an
absolute never-follow guarantee. The Reviewer had cited a probe result that
contained the expected path fragment but ended with a traceback, failed
assertion, and `EXIT=1`; another assessment cited a tool call whose normalized
result was failed. The former fragment resolver proved that selected text was
present but did not require the matched result as a whole to support a
`satisfied` assessment. That allowed a false acceptance even though the
attributable evidence already contradicted the verdict.

Runtime image `phase1-v6` now pairs `sat-probe-write` with immutable
`sat-probe-run`. The runner validates an owner-only bounded Python probe,
executes its open file descriptor with a fixed interpreter and project working
directory, limits child time and output, and emits a terminal
`SAT_PROBE_RESULT_V1` marker. Reviewer grounding rejects a satisfied assessment
when any matched tool result failed, any matched deterministic command failed
or timed out, a legacy terminal `EXIT=N` reports non-zero, or a direct runner
marker is missing, malformed, timed out, or non-zero. Exact replay of the
preserved false-acceptance response is now rejected deterministically before
assembly. A real restricted non-root container verified runner success,
assertion failure, timeout semantics, and runtime preflight against image ID
`sha256:a20a5bdd9a07d903beb78e78b9f69cb37faf1969eedf3aba3ee4945e416f3bd2`;
the complete 693-test offline suite passes. A fresh installed provider rerun is
still required to verify the corrected revision and delivery path.

Whole-result consistency alone does not prove that an absolute requirement was
challenged at every relevant entry. Planning now makes that scope explicit:
every proposed criterion returns `review_boundaries`, and a criterion whose
description contains an unqualified prohibition or safety guarantee must list
top-level input, nested input, alias or indirection, and failure path. The user
sees these obligations in the overview, and the confirmed TaskBrief freezes
them. `semantic_body_v4` requires explicit boundary checks; a satisfied
assessment must ground every approved boundary with a distinct attributable
fragment, while a blocked assessment may stop after one grounded
counterexample. Controller validation rejects missing, duplicate, reused, or
ungrounded approved boundary claims; checks outside the TaskBrief-owned scope
are deterministically removed and recorded before nested validation. The
complete 698-test offline suite passes. A fresh
installed provider rerun remains required to verify the expanded Planning and
Review contracts at the real provider boundary.

That fresh provider run is now recorded. Public install and bare `sat` produced
and approved a task-defined `implementer -> reviewer` team. The
`semantic_body_v4` Reviewer grounded all four approved boundary classes, found
that a user-selected top-level symlink root was still followed, and correctly
requested revision despite five passing deterministic gates. The Implementer
then fixed the root guard, its contradictory regression test, and README; all
five gates and 30 tests passed again. The second Review exceeded the former
450-second allowance before returning a semantic response, so the controller
failed without delivery. This confirms the Review evidence fixes and exposes a
separate timeout-policy gap: ten criteria concealed twenty additional explicit
boundary obligations.

Review timeout resolution now counts criteria plus boundary obligations as
work units. Under the current 300-to-600-second Review envelope, that live scope
resolves to the 600-second complex allowance and remains visible before
approval; small reviews retain routine or substantial values. Failure reports
also distinguish a finding proved on an earlier commit from a changed commit
whose independent re-verification did not complete. Focused Planning and
Dynamic Workflow tests cover both regressions, and the complete check is 700
tests passed; a new fresh-account provider run is still required before
claiming end-to-end delivery success.

That fresh-account retry passed the public installer, automatic diagnostics,
first-run configuration, provider smoke, and ordinary request capture. The
initial Planning response and its bounded repair both proposed the same
two-Agent `implementation -> review` DAG, but both copied the known
`AC_DOCUMENTATION` and `AC_QUALITY` definitions from policy context. The
initial response reached context-free writer coverage first and was rejected
because only the Reviewer task referenced `AC_QUALITY`; the repair added that
binding to the writer, then reached the later profile-ownership collision and
failed before overview. No runtime Agent, execution run, workspace, or
destination was created.

The Planning response boundary now removes exact active-profile definition
echoes before proposal-owned coverage validation, records each removal beside
the immutable raw response, retains task bindings, and materializes only the
controller's canonical criteria. It does not infer an unknown ID or alter any
Agent, task, dependency, concurrency, workload, model, timeout, or approval.
Both preserved provider responses now replay to their original two-Agent DAG
without a repair; focused Planning tests cover active-policy scoping, raw
evidence, canonical materialization, and unchanged strict rejection outside
that scope, and the complete repository check passes 702 tests. A new
fresh-account provider run remains required before claiming end-to-end
delivery success.

That fresh-account run confirmed the profile-criterion normalization: after one
ordinary syntax repair, the user approved a two-Agent `impl -> reviewer` plan,
the writer produced a clean eight-file commit, and all five deterministic gates
passed. The Reviewer completed 33 `exec` calls within its controller-resolved
600-second timeout, but SAT rejected the entire session before assembling a
Review because full-command `shlex.split` continued parsing text after a Bash
`#` comment and encountered an unmatched quote in the shell-ignored suffix.
Independent validation also proved the partial product still followed a
symlink used as the user-selected root; the Reviewer had mislabeled symlinked
children inside that root as `top_level_input` and returned a false `accept`.
No delivery was created.

Executable attribution now lazily consumes only leading environment assignments
and the first executable, while preserving the complete argument digest and
paired result. The same bounded replay captures all 33 archived calls; an
unparseable executable prefix remains invalid. Review boundary identifiers now
come from one immutable controller-owned definition mapping. Planning context,
the user overview, implementation and quality prompts all state that the
user-selected primary root itself is `top_level_input` and every child inside it
is `nested_input`. Focused artifact, Planning, dynamic-prompt, and OpenClaw
session-evidence tests pass, and the complete repository check passes 705 tests.
A fresh-account provider run was still required at that point before claiming
end-to-end delivery success.

That sixteenth fresh-account run now supplies the required evidence. The public
one-command installer completed in an empty non-root home, and the operator then
used bare `sat` without a TaskBrief, benchmark, explicit run ID, source path,
team, timeout, or concurrency arguments. One Planning call proposed an
`impl -> reviewer` team with maximum concurrency two. The user approved it;
policy resolved the substantial writer to 1,350 seconds and raised Review to
600 seconds from ten criteria plus seventeen explicit boundary obligations.
The controller froze those values, and the scheduler ran the Agents serially
because the approved dependency prevented parallel launch.

The Implementer produced one clean commit through 38 attributable tool calls.
All five deterministic gates passed, including 20 clean-workspace tests and
fresh-scratch execution of the exact generated setup, test, and start commands.
The first Reviewer attempt captured 37 real tool calls and 67 attributable
session records without an integrity error. Its response required one bounded
semantic JSON repair; the zero-call repair safely reused only the same
Reviewer's verified evidence against the same immutable commit. The final
Review accepted all ten criteria with no finding, including an explicit probe
that treated a symlink supplied as the scan root as `top_level_input`.

The controller completed and delivered the project. A separate ordinary-user
black-box check then reran exact setup, test, and no-argument start commands and
tested duplicate grouping, exclusion, minimum size, invalid size, missing input,
root and nested symlinks, symlink files, and post-command Git cleanliness.
All thirteen checks passed, the delivered HEAD matched the controller's final
commit, and terminal cleanup left zero containers or volumes. This establishes
the strict single-route ordinary product journey at that revision. It did not
exercise foreground controls, two authorized routes, durable process-restart
recovery, or an independent-device demonstration.

The seventeenth fresh-account run exercised the missing foreground interaction
instead of treating controls as an operator-only test surface. The public
installer and bare `sat` used
`deepseek/deepseek-v4-flash-vision-exp`; Planning proposed and the user approved
an `impl_agent -> integration_agent -> review_agent` DAG with maximum
concurrency two and resolved timeouts of 1,350, 900, and 600 seconds. The
scheduler correctly observed concurrency one because every Agent depended on
the preceding writer. During the live run, the user switched to detailed
visibility, queued project-specific guidance, paused before the first
invocation, inspected `/controls`, and resumed. Every command acquired a
controller revision, reached its safe applied boundary, appeared in the event
stream, and the guidance reference reached all four subsequent invocation
prompts. This supplies provider-backed evidence for guidance, visibility, and
cooperative pause/resume without claiming that a process crash is resumable.

Both writers completed a clean ten-file commit. All five deterministic gates
passed and the generated suite passed 20 tests. Review then ended as
`artifact_invalid` even though its final direct probe exited zero and emitted
all required markers. The same `SCAN_NESTED_DUP_OK` source text also appeared
inside traceback stderr from two earlier failed probes; the old whole-result
substring rule bound those failures together with the successful emission and
rejected the satisfied assessment. No result was delivered. Separately, an
ordinary external clone proved the committed `uv.lock` contained fourteen
references to `/opt/software-agent-team/wheels`, so exact `uv sync --dev`
failed outside SAT's image. Independent behavior validation still passed all
17 black-box checks after recording that setup failure, which proves useful
implementation behavior but does not override the portability defect. The
terminal display also silently clipped two completed Agent summaries at 500
characters.

The controller now treats direct-probe framing as a typed evidence channel. A
satisfied claim may match child stdout and the terminal result, never traceback
source text in child stderr. If the same marker appeared in a failed direct
probe and was later emitted by a successful direct probe, the successful call
supplies the claim while every failed call remains in telemetry. Persisted
boundary checks now coherently accept command-only grounding and still reject a
check with neither tool nor command evidence. Replaying the two exact archived
Reviewer attempts now accepts all 13 assessments with zero findings; the
`AC_SCAN` claim binds only successful attempt-one `tool-020` and attempt-two
`tool-005`, not failed `tool-013` or `tool-003`.

The generated-project contract also parses a committed lock before setup and
rejects absolute, Windows-drive, `file:`, parent-directory, missing,
symlinked, or private-wheelhouse dependency sources. The archived Round 17
output is now rejected directly at
`root.package[0].source.registry`, before same-image setup can mask the defect.
Developer, Reviewer, seed, profile, and decision documentation all state that
the offline wheelhouse is runtime infrastructure rather than delivery metadata.
Finally, bounded scheduler event text ends at a word boundary when possible and
uses an explicit `… [truncated]` suffix. The complete repository check passes
720 tests at that revision. The next fresh-account provider run, described
below, exercised these repairs and exposed additional delivery-boundary and
Review-recovery defects. Two-route switching, process-restart recovery, and the
independent-device demonstration remain separate evidence requirements.

The eighteenth fresh-account run used the public installer, bare `sat`, and the
same strict `deepseek/deepseek-v4-flash-vision-exp` route. A bounded Planning
repair removed one unknown response field, after which the user approved an
`impl -> tester -> reviewer` DAG with concurrency one and resolved timeouts of
1,350, 300, and 600 seconds. The writer committed eight tracked files and ten
tests. Compile, Ruff, and pytest passed, but the contract and exact-command
gates rejected a non-portable `uv.lock` created by sandbox setup. That lock was
effectively ignored and absent from the implementation commit; the validator
had inspected working-tree residue before determining whether it belonged to
the proposed delivery.

The Tester accurately retained both failed gates. Review then found a separate
product defect: documented `*.log` exclusion was implemented as exact string
membership rather than wildcard matching. Its bounded semantic repair produced
a useful `revise` report, but three positive assessments cited markers emitted
before their direct probes failed. The evidence boundary correctly refused to
treat those markers as success, yet the whole report became
`artifact_invalid`, losing the valid revision path.

The product contract now distinguishes Git delivery content before parsing a
lock. Every tracked lock is validated even when an ignore rule matches it; an
effectively ignored untracked lock is neither delivery metadata nor clean-copy
input. Review grounding now has one narrow monotonic recovery: only an
already-`revise` report with a separate blocked assessment and blocking finding
may change an unsafe positive assessment to `blocked` and add a
criterion-scoped controller evidence-gap finding. Accepted or terminal reports,
zero-match selectors, and invalid blocker mappings are never salvaged. An exact
replay of both archived Reviewer attempts and all 41 captured tool calls now
returns `revise`; AC_JSON, AC_MINSIZE, and AC_TESTS_SUITE are blocked rather than
misrepresented as satisfied. The plan overview also separates
controller-owned execution-profile constraints from additional Planning
constraints, and the Planner prompt forbids restating the former. Focused
coverage passes 115 tests and the complete repository check passes 725 tests.
A fresh provider-backed ordinary-user run remains required before claiming the
repaired path reaches accepted delivery.

The nineteenth fresh-account run again used the public installer, bare `sat`,
and the strict `deepseek/deepseek-v4-flash-vision-exp` route. Installation,
automatic diagnostics, isolated configuration, the authorized provider smoke,
ordinary request capture, destination confirmation, and Planning authorization
all succeeded. SAT then stopped before creating the bootstrap Planning Agent
because its local `openclaw models list --json` inspection did not finish
inside the shared 30-second preflight-command boundary. No provider request,
Agent, TeamPlan, run, source, workspace, or generated project was created by
that failed phase. Later read-only checks against the same isolated state
completed, supporting an insufficient cold-start margin as the cause without
claiming recovery of the swallowed original subprocess exception.

Model inspection now has a dedicated 90-second infrastructure timeout while
ordinary local preflight commands remain at 30 seconds. SAT announces the
bounded local wait before it starts, persists both values in runtime evidence,
and reports an exact safe timeout that states no provider request was made.
Planning wraps that diagnostic with the failed phase and confirms that no Agent
started. Simulated cold-catalog, timeout-safety, visible-status, and no-state
regressions pass 66 focused tests; the complete repository check passes 728
tests. At that point, a fresh ordinary-user provider retry was still required
before the repair could be considered live-validated.

The twentieth fresh-account run supplied that live validation at public commit
`cb5a11e`. A new unprivileged user installed with the documented one-command
bootstrap and then invoked only bare `sat`. Installation, device diagnostics,
isolated secret-free model setup, the explicitly authorized provider smoke,
request capture, execution-profile and destination confirmation, both visible
90-second-bound local model checks, and Planning runtime preflight all passed.
The Round 19 startup boundary no longer failed or sat silently.

Planning used one bounded repair after the first proposal made its revision
flag inconsistent with the one-iteration plan. The user then approved a
task-derived `duplicate_impl -> reviewer` DAG with concurrency one and
controller-resolved 1,350/600-second timeouts. The writer committed eight
changed files. All five deterministic gates and 28 project tests passed. The
Reviewer's first complete response contained an invalid JSON escape; one
94.3-second bounded repair safely reused the same Agent, stage, immutable
commit, and captured invocation-chain evidence. Independent Review accepted
all eleven criteria with zero findings, and SAT delivered clean commit
`62c3c50c46f1093715f9ce735a42d5f2fb441533` after removing two run-scoped
containers.

Independent ordinary-user validation then reran the exact setup, test, and
start commands. Setup succeeded, all 28 tests passed, start returned valid
JSON, and separate fixtures passed recursive duplicate grouping, wildcard
exclude, minimum size, nested-symlink exclusion, root-symlink rejection, and
file-root rejection checks without traceback. The project remained clean and
matched the controller's final commit. The complete evidence was archived
before an exact inventory-based cleanup removed the fresh account, state, and
temporary resources while preserving the shared image and historical
UID-reuse paths.

The rehearsal sequence below concerns the predecessor guided fixed-team
product path and is retained as defect and regression evidence. It proves the
installer, isolated runtime, fixed compatibility controller, delivery, and
cleanup boundaries at the named revisions; it does not prove the newly
activated Adaptive Planning and Dynamic Team journey.

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

At that revision, the workflow iteration limit became an explicit controller
input bounded by the team manifest. The advanced frozen evaluation remains at
two iterations for comparability. The current adaptive product path instead
uses the one-to-three iteration limit shown in and approved with each TeamPlan.
Repeated blockers, no-change revisions, resource limits, and all safety or
evidence-integrity stops remain unchanged.

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

This confirmed the predecessor Product Demo Slice on a fresh Linux account.
At that point the Adaptive Planning and Dynamic Team path still required its
own fresh provider-backed rehearsal; the adaptive evidence recorded earlier in
this document has since satisfied that historical gap. The current product
still supports small greenfield Python 3.12 projects and keeps the task-manager
contract isolated to the advanced evaluation surface.

The advanced `prepare-benchmark`, `preflight`, and `run` commands remain a
separate evaluation surface and are not part of the expected product demo.
The acceptance contract is
[`docs/product-demo-slice.md`](docs/product-demo-slice.md).

## Implemented and Offline Verified

- A versioned `TaskSelfCheckReport` contract with stable result identity,
  authority, dependency, freshness, severity, status, actionable evidence and
  remediation; transitive invalidation and exact stale-result refresh; a
  write-once per-task digest chain; compact, standard, and detailed rendering;
  and independent persisted-schema compatibility coverage;
- Local `sat --version`, human-readable `sat version`, and machine-readable
  `sat version --json` reporting from one release/source identity API, including
  managed-install provenance, exact Git revision and dirty state when
  available, explicit partial or inconsistent identity status, and one
  authoritative readable interval for every persisted schema family;
- Impact-driven release-candidate gates that bind `pyproject.toml`, `uv.lock`,
  the prior release baseline, minimum SemVer increment, exact tag/commit,
  deterministic source-archive digest, and every schema readable range;
- A pinned exact-tag GitHub workflow that reruns the offline gates and publishes
  one digest-verifiable `sat-release.json` asset, plus a stable resolver that
  rejects drafts, prereleases, tag/manifest drift, repository drift, missing or
  duplicate assets, and digest mismatch;
- Product-level `sat update --check`, confirmed `sat update`, local
  `sat channel status`, and explicit `sat channel switch stable|dev`, all using
  one immutable target resolver and one staged activation transaction;
- Managed lifecycle ownership for default and custom application paths,
  exclusive install/update/uninstall locking, pre-activation persisted-schema
  compatibility, active-run refusal, atomic application-link and install-record
  rollback, source-checkout refusal, and v1 managed-layout migration;
- Versioned managed uninstall that cross-checks the lifecycle root, active
  release, installation record, logical link, and recorded launchers before
  removing all retained application versions while preserving configuration,
  run data, isolated provider state, other OpenClaw installations, uv, Docker,
  and the sandbox image by default;
- Reproducible toolchain setup and diagnostics;
- Unified validation, benchmark-preparation, preflight, and `sat run` CLI;
- Versioned team manifest and validation;
- Versioned `TeamPlan`, `AgentSpec`, and `ModelRoutePlan` contracts with
  validation for dependency cycles, unknown references, write ownership,
  permission profiles, quality independence and coverage, model
  authorization, approved concurrency, and conditional controlled-evaluation
  limits;
- Exact compilation of every fixed evaluation fixture into the same
  run-scoped contract, including frozen TaskBrief binding, Agent time authority,
  dependency waves, workspace scopes, model route, budget, and manifest
  provenance;
- Versioned, hash-chained `RunEvent` persistence with run-state head anchoring,
  controller lifecycle and Agent attribution, dependency and route metadata,
  safe summaries, aggregate budget snapshots, and renderer visibility
  filtering;
- Versioned `ControlCommand` requests and terminal resolutions with typed
  targets, controller-assigned mailbox order, command-specific safe boundaries,
  optimistic revisions, immutable metadata, and predecessor-digest
  verification;
- Foreground plain-language run controls for prospective guidance, replacement
  Planning, cooperative pause/resume, best-effort per-Agent interruption,
  confirmed terminal cancellation, live visibility switching, exact command
  consequences, provider-cost caveats, and cancelled final reports;
- Versioned, explicitly authorized Adaptive Planning requests; strict
  question-or-proposal responses; high-value focused questions with suggested
  and custom answers; controller validation and targeted semantic correction;
- Task-defined proposal compilation into confirmed requirements, adaptive
  implementation intent, least-privilege AgentSpecs, exact primary and
  fallback model assignments, dependency waves, qualitative per-Agent workload
  estimates, controller-resolved time authority, and aggregate controller
  budgets;
- Task-proportional Adaptive team validation with no bootstrap capability in
  the runtime team, exact task ownership and cross-Agent dependency alignment,
  and at least one downstream read-only quality path for every writer;
- Hash-chained Planning-turn evidence including typed content-free provider
  liveness, immutable proposal revisions, exact user-approval digests,
  natural-language revision, safe structured edits, cancellation, and a
  complete plain-language overview;
- A replaceable OpenClaw subprocess adapter with stable fixed-role and
  run-scoped Agent sessions, explicit Agent ID and capability telemetry,
  version-pinned local and Gateway JSON parsing, private content-free stream
  observation, provider/model-aware renewable inactivity leases, and canonical
  `provider/model` telemetry;
- Sanitized OpenClaw Agent registry, permission checks, approved-Agent-only
  run-scoped configuration, non-root identity, exact per-Agent model-route
  enforcement, and offline preflight across every authorized route;
- A marked application-private OpenClaw binary plus explicit private config,
  credential, state, workspace, and Agent paths for every SAT invocation, with
  ambient OpenClaw settings neutralized and existing installations untouched;
- Exact run-scoped Agent-container cleanup on normal, failed, interrupted, and
  exceptional workflow exits, guarded by both session identity and SAT-owned
  mount provenance;
- Confirmed task-brief and handoff-envelope contracts;
- Fixed-role and task-defined capability minimum-context prompts, strict
  semantic JSON response parsing, dynamic identity/task/route/time-authority binding,
  controller assembly of persisted envelope, Git, test, and scope facts, and
  digest-bound field correction with controlled-evaluation caps;
- Contract-aware response normalization that permits presentation argv arrays
  around one semantic object while rejecting any additional object candidate;
- Exact Dynamic Reviewer criterion assessments with adversarial checks,
  same-chain attempt-qualified result selectors, same-iteration deterministic
  command selectors, controller-resolved tool and command IDs, unambiguous
  blocked-finding scope binding, and bounded no-network foreground probes
  against read-only source;
- Concrete phase-artifact and Agent-telemetry contracts with contextual
  validation;
- Immutable phase artifacts, handoffs, command output, Agent output, canonical
  paths, and SHA-256 references;
- Deterministic TeamPlan DAG scheduling with dependency readiness, exact
  approved team membership, approved concurrency, time-authority propagation,
  fail-fast launch control, attributable skipped nodes, and ordered progress
  events;
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
- Docker-only production gates with no network, read-only source execution,
  non-root identity, fixed commands, resource limits, timeouts, bounded output,
  plus fresh-scratch execution of exact generated setup, test, and start argv
  through a locked offline wheelhouse;
- The complete function-specialized workflow: Planner, Developer, controller
  snapshot, deterministic gates, independent Tester and Reviewer with
  configurable dispatch concurrency, decision, and launch-policy-bounded
  evidence-driven revisions;
- Bounded command-output diagnostics for verification, correct read-only
  source visibility, and controller-only Agent invocation policy;
- Explicit deterministic command coverage, `pending_review` manual criteria,
  Reviewer scope attestation, and controller-owned evidence resolution;
- Pre-call route-price and task-wide USD authorization for ordinary tasks, plus
  explicit call, token, duration, and cost thresholds only for controlled
  evaluation;
- One thread-safe ledger shared by Planning, task-defined execution, correction,
  repair, and switching. It prices provider usage from frozen call terms,
  preserves unpriced or missing-token states, exposes standard progress, and
  emits an attributable `budget-ledger.json` plus report breakdown;
- Typed decision-limit ownership metadata; one user-approved task-wide USD
  ceiling and optional deadline for ordinary tasks; provider-activity time
  authority; controlled-evaluation-only timeout/count envelopes; user-selected
  concurrency; and configuration-schema migrations from superseded product
  caps;
- Explicit completed and failed terminal outcomes with machine-readable and
  human-readable reports, exact controlling SAT software identity, and their
  model-spend ledger committed through a rollback-capable terminal bundle;
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
  atomic, schema-versioned secret-free profiles, deterministic route policy,
  optional authorized provider smoke checking, and no invented zero-cost
  estimate when prices are unknown;
- Natural-language request capture, explicit Python execution-profile
  confirmation, destination validation, explicit Planning authorization,
  bounded clarification, complete overview, natural-language revision, safe
  edits, and exact approval before execution Agents;
- Automatic private user-state roots, collision-resistant run IDs, separate
  Planning evidence, confirmed TaskBrief and TeamPlan materialization, trusted
  source creation after approval, isolated workspaces, and write-once evidence;
- Controller-backed role, elapsed-waiting, Git-snapshot, quality-gate,
  independent-review, decision, revision, completion, and failure progress,
  plus adaptive Agent queue, readiness, provider wait/activity, tool lifecycle,
  liveness degradation, suspected stall/grace/recovery, correction, duration,
  dependency, route, budget, and terminal-state projection;
- Accepted-result-only delivery through a same-parent staging directory into a
  new non-overwriting project child, followed by exact setup, start, and test
  commands from a validated project-owned argv manifest;
- Guided one-command uninstall with preservation defaults, pre-removal export,
  separate configuration/data/private-provider-state purge choices, Planning
  evidence preservation/export/purge,
  managed-application removal, and preservation of every other OpenClaw
  installation;
- Offline end-to-end coverage for success, revision, targeted response correction,
  invalid-response failure, timeout, evidence tampering, non-convergence,
  iteration exhaustion, no-change failure, missing model or token telemetry,
  cost exhaustion, and trusted sandbox-runtime loss classification.

## Current Fixed Evaluation Team Paths

[`configs/teams.json`](configs/teams.json) defines three comparable topologies.
The configuration owns membership and initial stage ordering; the Python
controller owns dynamic revision and termination decisions.

These manifests are fixed evaluation fixtures. Explicit `sat run` compiles the
selected fixture into the same `TeamPlan` contract used by the controller. The
normal product path does not select one of these fixtures: its approved plan is
derived from the task.

| Configuration | Purpose | Implementation status |
| --- | --- | --- |
| `single_agent` | One-pass baseline | Phase 3 |
| `function_specialized` | Planner, generalist implementation, independent testing and review | Phase 1 implemented and provider-validated |
| `implementation_domain_specialized` | Parallel frontend/backend work plus integration | Phase 3 |

## Not Yet Available or Completed

- A published stable GitHub Release and fresh supported-device evidence for
  stable install, stable update, stable↔dev switch, failed-activation rollback,
  and versioned uninstall;
- Fresh installed-device evidence for task-admission/approved-plan remediation
  and process-orphan recovery;
- An independent-device live demonstration of the activated Adaptive Planning
  and Dynamic Team journey;
- Durable control recovery after a foreground process crash and a
  secondary-process control client;
- A provider-backed run using two planned model routes and live switch
  evidence;
- Saved task/scenario-specific routing presets and empirically calibrated
  quality/latency/cost-aware selection beyond declared capability and priority;
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

The next verification steps are the independent-device demonstration and a
provider-backed run using two planned model routes with live switch evidence.
Phase 3F then closes the remaining acceptance and usability defects.
Fixed-topology comparison remains in Phase 4
so it can serve as a controlled baseline rather than define the product's
permanent role layout.
The detailed sequence and acceptance criteria are in
[`docs/adaptive-orchestration.md`](docs/adaptive-orchestration.md).

The development route and evaluation policy are defined in
[`VISION.md`](VISION.md#development-route).

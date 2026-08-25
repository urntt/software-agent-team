# Project Status

**Current milestone:** Fresh Linux product journey passed; independent device rehearsal next

**Last updated:** August 25, 2026

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

## Product Readiness Boundary

The primary CLI now implements the Product Demo Slice in code. A normal user
runs `sat`; SAT checks the device, guides model configuration, asks what to
build, explains and confirms the installed Python execution profile, collects
success conditions and constraints, chooses a new project destination,
generates a request-specific run ID, TaskBrief, trusted source, workspace, and
evidence roots, shows controller-derived progress, and delivers only an
accepted clean Git result with project-specific commands.

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
but the shared 120-second Planner deadline had already expired. SAT correctly
stopped without delivering an unaccepted project.

The parser now normalizes only a complete object followed by at most four
unmatched closing delimiters; it continues to reject additional values,
structures, unknown semantic fields, and incomplete plans. Raw provider output
remains unchanged in execution evidence. The Planner budget is now 180 seconds,
which covers the observed 139-second initial-plus-repair path with bounded
margin. A repair still shares the original deadline and cannot reset or double
the authorized time.

A second rehearsal used a newly created Linux account with its own home,
configuration, provider state, and project parent. The public installer checked
out the published revision, and the normal `sat` flow again passed every step
through planning. The Developer completed a clean implementation commit and 24
project tests in 854.4 seconds, within its existing 900-second budget. Its one
semantic JSON object was enclosed in the requested JSON fence, but the
presentation text before the fence included ordinary command notation such as
Python-style argv arrays. The former fence normalizer treated any square
bracket outside the fence as a competing JSON structure, requested an
unnecessary repair, and then correctly rejected the combined 929-second path
at the shared deadline.

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
or evidence-integrity stops remain unchanged. The complete 395-test offline
suite passes.

A fifth rehearsal began with another fresh non-root Linux account and the
public one-command installer at revision `a4c929d`. The user then invoked only
`sat`, completed guided first-run configuration, and described a small local
reading-list Web app in natural language. Run
`sat-20260825-005232-c8f61f0a` used
`deepseek/deepseek-v4-flash-vision-exp`. The Planner completed in 106.6
seconds. The Developer's first response omitted its required semantic JSON, so
the existing bounded repair path was legitimately used and remained inside the
shared 900-second deadline. The first implementation then reached the aligned
project gates, where pytest correctly exposed an import defect.

On iteration two, the Developer changed one file in response to that evidence.
All four deterministic gates passed, eight generated-project tests passed, and
the independent Tester and Reviewer both accepted the result. SAT completed all
five user success conditions in 1,689 seconds and delivered clean commit
`6283aa12401e1e18272df5315bdc9ef92e2478da`. The exact generated setup and
test commands then succeeded outside the controller. The exact start command
bound the application only to `127.0.0.1`; manual HTTP checks added, edited,
finished, persisted across a clean stop and restart, and deleted a book. Both
application starts shut down cleanly, and no listener remained afterward.

This confirms the complete provider-backed Product Demo Slice on a clean Linux
account: public installation, automatic checks, guided configuration, natural
request capture, visible progress, evidence-driven revision, accepted delivery,
and runnable-result verification. An independent WSL or second-device rehearsal
remains the next external confirmation, not a known product blocker. The
current product supports small greenfield Python 3.12 projects; its bounded
clarification records explicit user input and is not yet an adaptive
requirements Agent. The task-manager contract remains isolated to the advanced
evaluation surface.

The advanced `prepare-benchmark`, `preflight`, and `run` commands remain a
separate evaluation surface and are not part of the expected product demo.
The acceptance contract is
[`docs/product-demo-slice.md`](docs/product-demo-slice.md).

## Implemented and Offline Verified

- Reproducible toolchain setup and diagnostics;
- Unified validation, benchmark-preparation, preflight, and `sat run` CLI;
- Versioned team manifest and validation;
- A replaceable OpenClaw subprocess adapter with stable role sessions,
  version-pinned local and Gateway JSON parsing, and canonical
  `provider/model` telemetry;
- Sanitized OpenClaw Agent registry, permission checks, run-scoped
  configuration, non-root identity, strict model selection, and offline
  preflight;
- A marked application-private OpenClaw binary plus explicit private config,
  credential, state, workspace, and Agent paths for every SAT invocation, with
  ambient OpenClaw settings neutralized and existing installations untouched;
- Confirmed task-brief and handoff-envelope contracts;
- Role-specific minimum-context prompts, strict semantic JSON response parsing,
  controller assembly of persisted envelope, Git, test, and scope facts, and
  one deadline-sharing semantic response repair;
- Concrete phase-artifact and Agent-telemetry contracts with contextual
  validation;
- Immutable phase artifacts, handoffs, command output, Agent output, canonical
  paths, and SHA-256 references;
- Persisted run lifecycle with validated transitions, atomic replacement,
  optimistic concurrency checks, and integrity-checked recovery;
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
- Checked-in per-role stage budgets, optional global override, frozen resolved
  run policy, and configuration-schema migration from the former scalar
  timeout;
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

## Current Team Paths

[`configs/teams.json`](configs/teams.json) defines three comparable topologies.
The configuration owns membership and initial stage ordering; the Python
controller owns dynamic revision and termination decisions.

| Configuration | Purpose | Implementation status |
| --- | --- | --- |
| `single_agent` | One-pass baseline | Phase 3 |
| `function_specialized` | Planner, generalist implementation, independent testing and review | Phase 1 implemented and provider-validated |
| `implementation_domain_specialized` | Parallel frontend/backend work plus integration | Phase 3 |

## Not Yet Available or Completed

- An independent fresh-device rehearsal and live demonstration outside the
  development host;
- Adaptive follow-up clarification beyond the current bounded request, success
  condition, and constraint prompts;
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

Rehearse the published installer and bare `sat` journey from an intended-user
WSL environment, then perform the same one-command installation and guided
product flow during the live demonstration. Use a natural-language request,
observe the controller-derived progress, and execute the delivered project's
exact commands. Preserve any failure as evidence and apply the same
fix-test-publish-before-rerun discipline. The advanced evaluation commands are
not part of this product demonstration.

Topology implementation and comparison resume in Phase 3 after this gate. The
Phase 2 acceptance criteria are defined in
[`docs/product-demo-slice.md`](docs/product-demo-slice.md).

The development route and evaluation policy are defined in
[`VISION.md`](VISION.md#development-route).

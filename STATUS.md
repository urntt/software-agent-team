# Project Status

**Current milestone:** Product Demo Slice implemented offline; acceptance rehearsal next

**Last updated:** August 24, 2026

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
supported-scope request confirmation, automatic run preparation,
controller-backed progress, accepted-result delivery, and safe uninstallation
are implemented and covered offline. Provider credential creation remains in
OpenClaw's trusted boundary. Repeated comparative experiments and human rubric
scoring remain pending.

The exact acceptance procedure is in
[`docs/phase1-runbook.md`](docs/phase1-runbook.md). Offline scripted executions
prove controller behavior, not model quality.

## Product Readiness Boundary

The primary CLI now implements the Product Demo Slice in code. A normal user
runs `sat`; SAT checks the device, guides model configuration, asks what to
build, states and confirms the exact supported scope, chooses a new project
destination, generates the run ID, TaskBrief, trusted source, workspace, and
evidence roots, shows controller-derived progress, and delivers only an
accepted clean Git result.

This is not yet release-stable evidence. The complete 349-test offline suite,
including interaction, installer, workflow, failure, and delivery paths,
passes, but the journey has not yet passed the required rehearsal from a fresh
supported Linux/WSL
user environment with an explicitly authorized provider. The current product
also supports only the frozen local task-management application contract; its
bounded clarification is an explicit scope-confirmation step, not a general
requirements Agent.

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
- Checked-in per-role stage budgets, optional global override, frozen resolved
  run policy, and configuration-schema migration from the former scalar
  timeout;
- Explicit completed and failed terminal outcomes with machine-readable and
  human-readable reports;
- Remote one-command Linux/WSL bootstrap into an owned user-local application
  directory, plus the pinned toolchain, locked environment, fixed Docker image,
  stable launchers, update validation, and a checkout-based contributor path;
- Automatic startup checks for platform, architecture, unprivileged identity,
  project-parent writability, required commands, OpenClaw, Docker daemon,
  Linux-container image, storage, and launcher visibility;
- Integrated first-run and repeatable model configuration with private,
  atomic, schema-versioned secret-free defaults, optional authorized provider
  smoke checking, and no invented zero-cost estimate when prices are unknown;
- Natural-language request capture, explicit current-scope narrowing, concise
  requirements and destination confirmation, and authorization before model
  calls;
- Automatic private user-state roots, collision-resistant run IDs, confirmed
  TaskBrief materialization, trusted source creation, isolated workspaces, and
  write-once evidence;
- Controller-backed role, elapsed-waiting, Git-snapshot, quality-gate,
  independent-review, decision, revision, completion, and failure progress;
- Accepted-result-only delivery through a same-parent staging directory into a
  new non-overwriting project child, followed by exact setup, start, and test
  commands;
- Guided one-command uninstall with preservation defaults, pre-removal export,
  explicit configuration/data purge choices, managed-application removal, and
  clear shared-resource boundaries;
- Offline end-to-end coverage for success, revision, response repair,
  invalid-response failure, timeout, evidence tampering, non-convergence,
  iteration exhaustion, no-change failure, missing model or token telemetry,
  and cost exhaustion.

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

- A fresh-device, provider-backed acceptance rehearsal of the complete product
  journey and any fixes that rehearsal exposes;
- General request clarification or support beyond the current verified local
  task-management Web application;
- Automatic discovery and semantic validation of arbitrary existing OpenClaw
  provider profiles beyond OpenClaw's detected default model and SAT's optional
  smoke check;
- Automatic CLI resume of an interrupted run;
- Executable `single_agent` and `implementation_domain_specialized` workflow
  paths;
- Repeated comparative trials, human rubric scoring, and topology selection;
- A second product benchmark and product-level clarification flow.

The current `sat run` command starts from a confirmed `TaskBrief`, requires a
fresh run ID, and intentionally does not infer that an unrecorded external
action succeeded after interruption.

## Next Milestone

Complete the Phase 2 acceptance rehearsal from a fresh supported Linux/WSL
account: run the public managed-install command, configure one authorized
provider through the guided flow, request the supported product, observe every
progress stage, run the delivered project's exact commands, inspect failure
guidance, and record the result. Fix any root causes found by that rehearsal
before calling the Product Demo Slice complete.

Topology implementation and comparison resume in Phase 3 after this gate. The
Phase 2 acceptance criteria are defined in
[`docs/product-demo-slice.md`](docs/product-demo-slice.md).

The development route and evaluation policy are defined in
[`VISION.md`](VISION.md#development-route).

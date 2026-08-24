# Project Status

**Current milestone:** Phase 1 engine complete; Product Demo Slice next

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

Installation, secret-free run-default onboarding, and safe uninstallation are
implemented. Provider credential creation remains an OpenClaw/operator
responsibility. Repeated comparative experiments and human rubric scoring
remain pending.

The exact acceptance procedure is in
[`docs/phase1-runbook.md`](docs/phase1-runbook.md). Offline scripted executions
prove controller behavior, not model quality.

## Product Readiness Boundary

Phase 1 validates the engine, not the complete user journey. The current public
CLI still exposes a contributor/operator workflow:

- Installation starts from a source checkout and creates checkout-bound
  launchers;
- `sat` prints a guide instead of running an integrated diagnostic and
  first-run wizard;
- Configuration exposes model price, verification concurrency, and timeout
  controls intended for evaluation;
- A build requires an existing TaskBrief and source path;
- Request collection, clarification, confirmation, automatic internal
  materialization, continuous progress, and the final delivery view are not yet
  implemented.

The repository must not present `sat prepare-benchmark`, explicit TaskBrief
editing, or a provider-backed evaluation trial as the expected product demo.
The approved next acceptance contract is
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
- One-command Linux/WSL installation for the pinned toolchain, locked project
  environment, checkout-bound CLI launchers, fixed Docker image, and offline
  validation, without taking ownership of OS-level Docker or provider secrets;
- First-launch and repeatable configuration guidance with private, atomic,
  secret-free defaults, plus explicit per-run CLI overrides;
- Guided one-command uninstall with preservation defaults, pre-removal export,
  explicit purge choices, and clear shared-resource boundaries;
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

- Managed end-user installation and complete automatic environment diagnosis;
- An integrated no-subcommand `sat` startup and first-run configuration flow;
- Natural-language request collection, bounded clarification, and requirements
  confirmation;
- Automatic run ID, TaskBrief, source, workspace, and delivery preparation;
- Controller-backed progress summaries and a concise final delivery view;
- Automatic CLI resume of an interrupted run;
- Executable `single_agent` and `implementation_domain_specialized` workflow
  paths;
- Repeated comparative trials, human rubric scoring, and topology selection;
- A second product benchmark and product-level clarification flow.

The current `sat run` command starts from a confirmed `TaskBrief`, requires a
fresh run ID, and intentionally does not infer that an unrecorded external
action succeeded after interruption.

## Next Milestone

Phase 2 implements the Product Demo Slice: one managed installation command,
automatic diagnostics, guided provider setup, a natural-language request and
bounded clarification flow, automatic internal preparation, visible progress,
and a runnable delivery with exact next commands. Normal users must not operate
the evaluation CLI or edit internal JSON.

Topology implementation and comparison resume in Phase 3 after this product
journey is executable. The Phase 2 acceptance criteria are defined in
[`docs/product-demo-slice.md`](docs/product-demo-slice.md).

The development route and evaluation policy are defined in
[`VISION.md`](VISION.md#development-route).

# Project Status

**Last reviewed:** September 29, 2026

This page records current implementation and validation facts. Product direction
and experiments belong to [`VISION.md`](VISION.md); behavioral contracts and
operator instructions live in [`docs/`](docs/README.md). The exact earlier
status narrative remains available through the [historical source map](docs/history/README.md).

## Release and checkout boundary

The latest published stable release is `v0.5.1`, tagged at
`6de24f48d54b995f5dd09493f0345e644e82c72b`. Its exact-tag GitHub gate
passed and published the release-bound `bootstrap.sh` and `sat-release.json`
assets. The same clean commit passed a normal-user OVH canonical gate: doctor,
format, lint, and 1,978 tests passed; one cross-UID fixture was skipped because
it requires root. Isolated fresh-account checks installed both the exact dev
candidate and the public current stable, verified identity and isolated runtime,
and completed export, uninstall, and exact cleanup without provider calls.
The patch constrains each Review boundary to one distinct post-assertion
evidence marker. A fresh public stable installation on Linux completed the
official DeepSeek provider check, subsequent startup, four Planning calls,
user approval, run controls, an unchanged Integration handoff, all three
independent Reviews, and accepted delivery. The Security Reviewer corrected
two invalid evidence attempts within its approved scope before acceptance.
The delivered project passed 13/13 independent black-box checks, followed by
export, uninstall, and exact account cleanup. The run recorded 13 model calls,
an estimated $0.225933 in model spend, and no unknown-cost calls under its
$1.50 authorization. These observations apply to the tagged DeepSeek/Linux
route; they do not establish another provider or host mode.

## Implemented in the checkout

- The normal `sat` journey checks the local runtime, configures an isolated
  provider profile, captures a plain-language request, clarifies material user
  decisions, shows a task-defined proposal, requires approval, runs the
  approved Agent team, and delivers only an accepted result with setup, start,
  and test commands. [`docs/product-demo-slice.md`](docs/product-demo-slice.md)
  owns the user journey; [`docs/adaptive-orchestration.md`](docs/adaptive-orchestration.md)
  owns Planning and team contracts.
- The Controller owns lifecycle, approved task and Agent identities, scoped
  permissions, deterministic quality gates, independent Review, corrections,
  run controls, Git snapshots, budget accounting, and persisted evidence.
  [`docs/runtime-evidence.md`](docs/runtime-evidence.md) owns the runtime and
  evidence details.
- Managed Linux/WSL installation stages the private OpenClaw runtime, Python
  environment, and restricted Docker image. Updates and uninstall preserve
  user configuration and run data unless the user explicitly chooses removal.
  [`docs/installation.md`](docs/installation.md) owns the lifecycle contract.
- The fixed `function_specialized` evaluation workflow is executable. The
  `single_agent` one-pass evaluation path now uses the same isolated Git,
  OpenClaw, budget, deterministic quality, and report boundaries; its offline
  success and gate-failure integration checks pass. A [two-request
  provider-backed comparison](docs/evaluation-results/2026-09-28-fixed-topology.md)
  completed: this baseline delivered 2/2, while the fixed team delivered 1/2
  because one Reviewer response was invalid after its generated code passed
  every deterministic gate. The ordinary product derives its team from
  an approved task. `implementation_domain_specialized` remains a defined but
  non-executable manifest entry. The comparison procedure is in
  [`docs/fixed-comparison.md`](docs/fixed-comparison.md).
- Planning's wire and typed checks share one owner for user-only product
  decisions. Pure response-envelope and path normalization has a separate
  module; a content-free rule-audit tool can aggregate persisted Planning
  diagnostics by source version, stage, model fingerprint, and outcome.
  Response normalization now separates atomic relations, exact answered
  dimensions, profile-criterion ownership, and safe path presentation into
  named steps; the dynamic runner separates incomplete-call evidence from
  completed typed-response acceptance. The input fixture has one test owner,
  and normalization cases occupy a focused test module. A clean-checkout OVH
  canonical gate for revision `1e8b684` passed doctor, formatting, lint, and
  1,978 tests, with one root-only cross-UID test skipped. No provider-backed
  behavior improvement is inferred from this structural refactor.

## Validation limits and open capabilities

The tagged `v0.5.1` DeepSeek journey demonstrates one Linux managed-install
and single-route delivery path. It does not establish a fresh WSL recovery run,
rootless Docker without a host Docker group, every Gemini route, a live
provider switch, manual slow-key terminal usability, or paths requiring a
specific failure or later implementation iteration. Those conditions need
their own targeted evidence.

The two-request, one-route comparison is too small to establish general
quality, cost, or latency superiority, and it does not include a frozen
task-defined TeamPlan or a browser-based keyboard-only walkthrough.
Comparative results must continue to hold task, route, budget, and acceptance
criteria constant and report failures as well as successes.
Generated-project execution remains limited to the current local Python 3.12
profile. Automatic resume after an interrupted CLI process and durable
cross-process run controls are not available.

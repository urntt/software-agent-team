# Project Status

**Last reviewed:** September 28, 2026

This page records current implementation and validation facts. Product direction
and experiments belong to [`VISION.md`](VISION.md); behavioral contracts and
operator instructions live in [`docs/`](docs/README.md). The exact earlier
status narrative remains available through the [historical source map](docs/history/README.md).

## Release and checkout boundary

The latest published stable release is `v0.4.31`, tagged at
`e727ba48aa07e652c688385d2ab48425512d3a9c`. Its normal-user Linux
journey reached an approved plan, execution, independent Review, accepted
project delivery, and 13/13 external black-box checks. Export and uninstall
completed. That evidence applies to the tagged release, not automatically to
later checkout changes or a different provider, WSL device, or Docker mode.

The checkout retains package version `0.4.31` while post-release changes are
being prepared. Stable bootstrap has been changed to a release-bound asset and
`main` has CI configuration, but `v0.4.31` does not contain that new bootstrap
asset. The one-command URL in the current README therefore needs the next
published release before it can install this checkout. A public install,
upgrade, and rollback of that release-bound entry point remain unverified.

The post-release source revision `7af665007c23333f66c05a09270f3a7f7a2710fc`
passed a clean, normal-user OVH canonical gate: doctor, format, lint, and 1,977
tests passed; one cross-UID fixture was skipped because it requires root. This
is offline validation of that revision, not a new public release or a fresh
ordinary-user acceptance run.

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

## Validation limits and open capabilities

The tagged DeepSeek journey demonstrates one Linux managed-install and
single-route delivery path. It does not establish a fresh WSL recovery run,
rootless Docker without a host Docker group, every Gemini route, a live
provider switch, or manual slow-key terminal usability. Those conditions need
their own targeted evidence. A successful offline gate does not substitute for
a public release asset or ordinary-user installation.

The two-request, one-route comparison is too small to establish general
quality, cost, or latency superiority, and it does not include a frozen
task-defined TeamPlan or a browser-based keyboard-only walkthrough.
Comparative results must continue to hold task, route, budget, and acceptance
criteria constant and report failures as well as successes.
Generated-project execution remains limited to the current local Python 3.12
profile. Automatic resume after an interrupted CLI process and durable
cross-process run controls are not available.

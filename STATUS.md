# Project Status

**Last reviewed:** September 30, 2026

This page records current implementation and validation facts. Product direction
and experiments belong to [`VISION.md`](VISION.md); behavioral contracts and
operator instructions live in [`docs/`](docs/README.md). The exact earlier
status narrative remains available through the [historical source map](docs/history/README.md).

## Release and checkout boundary

The latest published stable release is [`v0.5.5`](https://github.com/urntt/software-agent-team/releases/tag/v0.5.5),
tagged at `e9de2058b452b3397fa6f28f43aa41fc30d92ab5`. Its exact-tag
GitHub gate passed and published release-bound `bootstrap.sh` and
`sat-release.json` assets; the candidate passed the OVH canonical gate with
1,995 tests passing and one root-only fixture skipped. The downloaded manifest
matches the tagged source archive digest, and both asset hashes match their
GitHub digests. A new ordinary account's pre-tag dev installation passed
identity, no-op, first-start interruption, export, uninstall, and exact cleanup
checks without provider calls. These checks do not establish a complete task.

The `0.5.5` release projects nested errors into authorized
parent correction slots and bounds only advisory option-description prose
before strict validation, marking omitted text with an ellipsis. The raw
submission and normalization remain in the turn; exact option IDs and product
definition values are retained. The archived 333-character submission now
validates as a three-option question with a 300-character description. The
Planning and response-correction focused suite passed 293 tests. Published
ordinary-user Planning, proposal approval, and delivery remain unvalidated.

The preceding `v0.5.4` public installation completed first configuration and
a subsequent self-check, but real DeepSeek Planning twice submitted an
overlong question-option description. Its whole-question correction omitted
the nested error, so no proposal was approved or delivered. The two calls have
recorded usage; external billing is not independently known. The `0.5.5` fix
addresses that captured shape but has not yet completed a provider-backed task.

The earlier `v0.5.1` release completed a DeepSeek/Linux ordinary-user delivery
and 13/13 independent black-box checks. That historical success does not
establish the current release's provider-backed behavior.

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
  A later five-task DeepSeek comparison delivered 5/5 with the single Agent
  and 3/5 with the fixed team; independent black-box acceptance passed all ten
  final Git snapshots. Two fixed-team runs stopped on invalid Reviewer output,
  so code acceptance cannot be counted as SAT delivery. A Gemini free-tier
  attempt returned HTTP 429 in its first cell and provided no paired outcome.
  These observations do not justify changing the ordinary-user team default.
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
  behavior improvement is inferred from this structural refactor. Two captured
  Planning submissions replayed identically before and after the split; focused
  normalization and runtime-invocation tests now cross the coordinator and
  persisted-turn boundary without importing a large test module for fixtures.
- The checkout pins private OpenClaw `2026.9.6` and emits its current keyed
  Agent configuration. An adapter compatibility check confirms Gemini 3.8
  thinking fallback maps to `LOW` rather than unsupported `MINIMAL`; a minimal
  DeepSeek call succeeded through the updated runtime. Gemini 3.8 provider
  validation remains outstanding after a free-tier 429 and an unavailable paid
  balance. Owned Agent timeouts with a positive wrapper exit retain their
  timeout lifecycle and actual exit in the execution record; the production
  execution-to-storage regression passes. The integrated checkout tree
  `98f70bb2c202bb251f72edc41c2deb20eadf750a` passed doctor, formatting,
  lint, and 1,987 tests on OVH, with one root-only cross-UID test skipped.

## Validation limits and open capabilities

The tagged `v0.5.1` DeepSeek journey demonstrates one Linux managed-install
and single-route delivery path. The `v0.5.5` canonical gate and pre-tag dev
installation checks cover offline behavior and lifecycle; public task delivery
remains unvalidated. None establishes a fresh WSL recovery run,
rootless Docker without a host Docker group, every Gemini route, a live
provider switch, manual slow-key terminal usability, or paths requiring a
specific failure or later implementation iteration. Those conditions need
their own targeted evidence.

The five-task, one-complete-route comparison is too small to establish general
quality, cost, or latency superiority. The second route stopped at a provider
quota boundary, and browser-based keyboard-only interaction was not assessed.
Comparative results must continue to hold task, route, budget, and acceptance
criteria constant and report failures as well as successes.
Generated-project execution remains limited to the current local Python 3.12
profile. Automatic resume after an interrupted CLI process and durable
cross-process run controls are not available.

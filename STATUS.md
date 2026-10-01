# Project Status

**Last reviewed:** September 30, 2026 (Pacific Time)

This page records current implementation and validation facts. Product direction
and experiments belong to [`VISION.md`](VISION.md); behavioral contracts and
operator instructions live in [`docs/`](docs/README.md). The exact earlier
status narrative remains available through the [historical source map](docs/history/README.md).

## Release and checkout boundary

The latest published stable release is [`v0.6.0`](https://github.com/urntt/software-agent-team/releases/tag/v0.6.0),
tagged at `6aaba348be9e64f365be8efbc1b6c7ce88bba169`. The read-only exact-tag
gate and separate publisher succeeded, and both release assets matched their
GitHub digests and the tagged source archive. The hosted gate passed 2,023 tests
with four environment skips; the same code passed 2,026 tests with one root-only
skip on Linux with the actual sandbox image.

A new ordinary account completed public installation, first provider setup and
check, subsequent startup, nine real DeepSeek Planning calls, and proposal
approval. Execution readiness then blocked the team because the pinned runtime
requires `agents.ownership="explicit"` for a multi-Agent roster. Single-Agent
Planning did not exercise that requirement. No execution Agent, workspace, or
product was delivered. Export and `sat uninstall` passed and the test account
and attributable resources were cleaned. The Planning estimate was $0.039044;
this is not an independently confirmed provider invoice.

The `0.6.1` candidate declares explicit roster ownership, binds local catalog checks to
an existing Agent, retains bounded and
credential-redacted configuration validation paths in the execution self-check,
and uses the readable standard Planning overview unless technical detail is
requested. Passing check evidence references and rerun rules appear in detailed
visibility. Focused production-interface tests passed 322 cases on Linux with
the actual pinned runtime. Replaying the failed, approved six-Agent TeamPlan
passed real configuration validation, local model/auth inspection, and the
sandbox probe without model generation. Candidate full-gate validation and
publication are still pending.

## Implemented in the checkout

- The published `0.6.0` release also cleans advisory option whitespace before its shared
  length bound, reserves the remaining authorization for each paid invocation,
  waits for temporary reservations and refuses insufficient known request
  headroom, freezes the npm tree with an empty lifecycle allowlist, and verifies
  the first uv archive from one shared installer. Host Git consumers share an
  isolated callback/transport policy; the release gate has read-only permission
  and passes checked assets to a separate publisher. Focused Planning/budget/
  runner checks passed 401 tests; lifecycle/permission checks passed 59, and
  budget/lock/release authority checks passed 57. These suites overlap. Fresh
  locked-runtime installation and both real OpenClaw localhost transport cases
  passed. The clean Linux candidate `e4d329fc27dbd6426942f73e4416728a83fe272c`
  passed the canonical gate with **2,026 tests passing**, one root-only fixture
  skipped, and successful doctor, formatting, and lint stages. Stage processes,
  temporary directories, leases, and attributable Docker resources were clean
  afterward. The public `0.6.0` lifecycle passed through
  approval and was then blocked by the roster contract described above. ARM64/WSL installation and optional native
  OpenClaw integrations are not established by these Linux x86_64 checks.
- The checkout adds `sat uninstall`, persistent shared Planning/execution
  controls with completion and multiline editing, journey-wide visibility,
  read-only elapsed/budget/context/Git telemetry, and bounded visible tool/model
  previews. These changes are in the published `v0.6.0` release. A clean
  non-root Linux canonical gate on `cefc8e7438cf6906c05128f7458f4e20a26d476b`
  passed **2,009 tests**, with one cross-UID fixture skipped because it requires
  root; doctor, formatting, and lint also passed. Both production-executor
  transport scenarios passed with pinned OpenClaw `2026.9.6` and a localhost
  HTTP fixture; attributable previews included visible streaming text,
  current-request context, and tools. This does not establish real provider
  behavior. The later public DeepSeek journey reached approval and the
  execution-readiness failure recorded above. Export/uninstall passed; WSL
  visual usability and successful team delivery remain pending. See the
  [terminal contract](docs/terminal-interface.md).
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

An independent review of `v0.5.5` identified open limits. An offline
counterexample confirms that option descriptions whose raw length exceeds 300
only because of surrounding whitespace still fail typed validation. Paid-call
reservations estimate one provider request, while invocation settlement can
aggregate multiple requests; the offline multi-request counterexample exceeds
the reserved amount. Installation pins the top-level OpenClaw archive but does
not freeze its transitive dependency tree, and the first uv installation uses
a moving installer. Release setup and tests share the publication job's write
permission. Delivery Git helpers also inherit ambient configuration that the
workspace helper isolates; a harmless global fsmonitor fixture confirms that
difference, without demonstrating a sandbox escape. The `0.6.0` candidate
addresses these owners, with the focused checks described above. The earlier
`0.5.5` gate does not cover the new counterexamples; final candidate and
published validation are still required.

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

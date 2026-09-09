# Development Guide

This guide owns local checkout setup, offline validation commands, benchmark
maintenance, repository layout, and the contribution workflow. Product,
architecture, experiment, scope, and roadmap changes must first be reconciled
with [`VISION.md`](../VISION.md).

## Local Setup

From the repository root:

```bash
make setup
make check
```

`make setup` prepares the pinned local toolchain, locked Python environment,
and a marked private OpenClaw runtime at `.sat/openclaw/`. It never discovers,
adopts, or changes another OpenClaw installation or profile. `make check` is
fully offline after setup. It verifies tool versions, ownership, Git and ignore
boundaries, configuration contracts, formatting, lint, the complete test
suite, and all offline workflow paths. It does not call a model or require
provider credentials.

`configs/toolchain.sh` is the shared setup/doctor authority for private Node and
OpenClaw versions and download checksums. A runtime pin update must verify the
official artifacts, package engine compatibility, linked SQLite safety, fresh
installation, failure preservation, and the real OpenClaw loopback gate. The
SAT-owned dependency installer must not delegate to a moving remote shell script
or introduce Gateway/service discovery. Keep npm configuration and cache inside
the ephemeral installer home; do not inherit another installation's npm prefix.
Regressions must also cover `STATE_DIRECTORY`, `NODE_OPTIONS`, and `NODE_PATH`:
isolating only `OPENCLAW_*` cannot isolate a package's postinstall cleanup roots.
The shared shell launcher authority owns Node's private compile-cache path and
validates it on every launch. Shell and Python subprocess isolation neutralize
ambient cache selectors; only that launcher enables the installation-owned
cache. Cover fresh installation, legacy launcher refresh without reinstall,
cache reuse, independent installations, redirected or foreign-owned cache
refusal, and unchanged external cache sentinels. A faster warm help command
does not by itself prove that a full Agent initialization stall is fixed.

The checkout must be an independent Git repository with a committed HEAD.
Feature branches and detached release tags are supported; branch names do not
establish repository identity. Release CI runs this same setup and full gate.

`make check` is also the canonical diagnostic full gate. Its supervisor streams
each stage's original output while atomically recording a report beneath the
ignored `artifacts/generated/full-gate/` directory. The report binds the exact
argument vectors, working directory, Git revision, stage outcomes, current and
last-completed pytest node, aggregate peak RSS and process/thread counts,
cgroup/OOM observations, and the terminal SAT-owned process-lease and Docker
inventory. Observer failures are recorded as typed unavailable facts and do
not replace a command's real exit status. If the supervisor disappears before
writing a terminal outcome, the next invocation marks that started report
`incomplete_observed_on_recovery`; it does not infer a cause from a later
successful run.

Diagnostic report schema v4 gives the canonical pytest stage an exact, short
private temporary leaf under `/var/tmp`. The supervisor overrides `TMPDIR` and
`PYTEST_DEBUG_TEMPROOT`, passes an explicit `--basetemp`, and records the exact
base, leaf, filesystem device, owner UID, and mode before launch. It removes
only that validated tree after all attributable stage processes stop on success, test failure, timeout,
or signal; a cleanup failure fails the gate. Recovery removes an abandoned
tree only after its persisted stage marker and PID/start-time identity show no
live owner. It defers cleanup while an exact process is live and refuses paths,
symlinks, ownership, base identity, or leaf shapes outside the gate-private
namespace.
Foreign shared pytest state is neither read as authority nor deleted.

Diagnostic report schema v3 distinguishes an attributable process-resource
sample from a process that exits before `/proc` can be observed. It samples
immediately after launch and reports typed `unavailable` plus `null` peaks when
no attributable sample exists; it never represents a missing observation as
numeric zero. Identity observations and nonzero RSS samples are counted
separately, so a terminal zombie identity cannot masquerade as a resource
sample. Each stage also records its own observation status and counts.
Schema v2 introduced the private, non-credential ownership identity inherited
by each stage's subprocesses. Linux process inventory uses
that identity together with a process-local child-subreaper boundary, process
groups, live ancestry, and PID/start-time identities. A child that creates a
new session is adopted by the supervisor when its parent exits, matched against
the inherited identity, terminated by exact identity, and reaped. It therefore
cannot disappear between topology samples or turn a leaking stage into a false
success. If the kernel boundary is unavailable, the report says so and falls
back to marker attribution. Terminal reports retain only a SHA-256 digest of
the identity and never capture a process environment. While a private
temporary tree is nonterminal, its mode-0700 report temporarily retains the
opaque stage marker needed to avoid deleting storage from an active orphan;
normal or recovered cleanup removes that marker.

Each stage has a 30-minute developer-gate infrastructure ceiling so an
unattended repository check cannot remain stuck forever. That ceiling is not a
product-run deadline or an Agent invocation work limit. A timeout or terminal
signal is forwarded to the exact stage process group, followed by a bounded
cleanup and residual inventory. To diagnose the supervisor itself or place
evidence elsewhere, run its explicit contributor entry point:

Process-lifecycle regressions must synchronize on an explicit readiness frame
or an atomically published durable record. File existence, a cumulative sleep,
or an intermediate `running` label cannot prove that a child installed its
signal handler or that a terminal report and inventory are durable. Diagnostic
ceilings belong to the specific handshake or terminal checkpoint, and timeout
failures must report which checkpoint was not reached.

```bash
uv run --frozen python -m software_agent_team.full_gate \
  --evidence-root /absolute/private/evidence/root \
  --stage-timeout-seconds 1800
```

Useful targets are:

```bash
make doctor
make validate
make format
make format-check
make lint
make test
make check
make loopback-check OPENCLAW=/absolute/path/to/the/pinned/openclaw
make lock-runtime
```

`make loopback-check` is an explicit live-local maintainer gate: it uses the
real pinned OpenClaw transport and Docker sandbox against a process-local
OpenAI-compatible SSE endpoint, but it makes no external provider request and
uses no real credential. Its four scenarios cover productive streaming,
Controller-observed stall recovery, network disconnect, and permanent silence.
The recovery endpoint releases activity only after the Controller has actually
published `stall_suspected`, leaving the declared diagnostic grace independent
of host scheduling jitter. Every scenario has a machine-readable status,
reason, phase, response, liveness, and cleanup oracle; any mismatch returns a
non-zero exit status even when resource cleanup succeeds. Use the module entry
point directly with `--output` to preserve a full JSON record:

```bash
uv run --frozen python -m software_agent_team.loopback_validation \
  --openclaw /absolute/path/to/the/pinned/openclaw \
  --output /absolute/private/evidence/loopback.json
```

Use `make format` when source formatting changes are required. Select checks
according to the [validation policy](#validation-policy), not commit count.

## Validation Policy

`tests/test_submission_bridge.py` executes the production submission plugin with
the pinned Node runtime, passes its actual output through session extraction and
bound capture, and applies the resulting semantic correction. It covers envelope,
slot identity, binding, failed-attempt, and terminal-order boundaries without
external requests, including preceding deferred work and runtime rejections.
Its simulated host session IO does not replace the separate
real OpenClaw loopback gate or provider-backed product acceptance.

The dynamic Reviewer correction integration also passes an actual plugin capture
into the production runner. It checks successful catalog binding and rejected
handles through final grounding, artifact persistence, and call settlement,
without inventing current-turn work tools for a terminal-only correction.
The mixed-slot case captures each correction through that same plugin boundary:
one valid choice is staged without publishing an artifact, only the remaining
slot is requested, and a later valid choice completes grounding. An explicit
evaluation repair limit still stops without publishing the partial result.
Earlier work, provider usage, and quality commands remain explicit fixtures;
this narrow integration is not a full sandbox or provider journey.

Also carry multi-selector capture through the workflow terminal boundary. Valid
choices must reach a completed report bound to the verified commit; a mixed
submission followed by an invalid remaining choice must never enter delivery.
Verify unpublished partial artifacts, unchanged source Git history, and unique
priced settlement in both cases. This is not installation or external delivery
acceptance: model content and quality-command execution remain fixtures.

Planning source-quotation integration routes both the initial proposal and its
correction through the real plugin and bound capture before the coordinator.
Assert the persisted submission evidence, precise source-only correction, and
rejection of unchanged invalid quotes; genuinely missing user authority must
remain a user decision. The provider content and lifecycle remain simulated.

Planning criterion checks collect independent sibling failures in one diagnostic
pass after proposal prerequisites are valid. Preserve prerequisite ordering
within each criterion and defer global coverage checks until those failures are
resolved. Regression tests must exercise the resulting correction through the
coordinator, including reordered keyed replacements and unchanged valid siblings;
checking the diagnostic message alone is insufficient.

Group related defects by their shared contract or state owner. Reproduce failures
with captured, sanitized inputs and integrated production paths before repairing
individual symptoms. Mock the external boundary where needed, not the accepted
submission or state transition that the test is supposed to establish.

Planning and dynamic runtime configuration explicitly disable OpenClaw's memory
plugin slot while enabling SAT's terminal submission plugin. Check the generated
configuration against the pinned runtime's actual slot resolver: an allow-list
alone still defaults to `memory-core`. This does not disable session history or
the default context engine, and a slot-resolution test does not establish overall
startup performance or eliminate imports required by upstream state migration.

Shared accounting integration must use ordinary task USD authority, not only
evaluation call counters. Carry priced Planning spend into the real workflow and
verify success and budget exhaustion through terminal persistence. Exercise an
authorized fallback with different frozen route prices, including billable failed
attempts: switching must neither reset spend nor inherit the previous model's
prices, and exhaustion must prevent subsequent work. Provider usage remains an
explicit fixture in these tests, not evidence of actual provider billing.

Carry upstream-incomplete writer results through the whole workflow as well as
the runner. With real Git and the shared ledger, verify same-session continuation
through gates and Review to a terminal report, plus no-progress, exhausted-budget,
and between-call cancellation branches. Queue cancellation through the persisted
control store, not a mocked stop decision. Partial work must remain unaccepted,
failed branches must never enter delivery, and every invocation settles once.
The external result fixture does not replace session classification or real
provider evidence, and workflow completion is not external project installation.

- During implementation, run affected unit and integration tests. Work-in-progress
  commits must record the checks performed and remaining validation; they do not
  claim batch acceptance. Documentation-only changes require content, link, and
  diff checks, not the full product suite.
- Run canonical `make check` on the completed implementation batch before
  integration or release. Repeat for relevant changes, diagnosed instability, or
  an explicit acceptance requirement, not automatically three times per revision.
- Exercise the real pinned runtime with a local endpoint when changing transport,
  plugin, tool-loop, or runtime compatibility boundaries. A mocked executor result
  cannot prove those interfaces work together.
- Test fresh installation when validating first use or changing installer,
  packaging, toolchain, ownership, or migration boundaries. Reuse an isolated
  installation for unrelated functional checks; distinguish upgrade and reused
  installation evidence from fresh-install evidence.
- Use provider-backed user journeys for shared batch acceptance after known
  offline-reproducible blockers are addressed. A new failure should lead to a
  focused reproduction and owner-level fix, not an automatic reinstall and full
  journey for each patch. Preserve original failed evidence.
- Bind every result to its revision, configuration, and tested assertions.
  Earlier results can support unchanged components with a documented impact
  assessment; they are not fresh whole-candidate acceptance. Release gates and
  safety checks remain mandatory.

Commit, push, installation, and evidence export are separate actions; completing
one does not require repeating all the others. Test fixtures should clean up
their exact owned resources and preserve failure diagnostics without touching
unrelated state or credentials.

The additional `--scenario tool-rejection` loopback case exercises the pinned
runtime's unsupported tool response, a subsequent independent fixture read,
and a final response. Its oracle requires separately captured negative
diagnostics and successful paired work without session-attribution degradation.
It uses no external provider or real credential and is separate from the four
default transport/liveness scenarios.

## Checkout Installation

Contributors who need checkout-bound `sat` and `sat-uninstall` launchers may
run:

```bash
./scripts/install.sh
```

This path performs the locked runtime, image, configuration, and launcher setup
used by a managed installation, then also runs formatting, lint, and the full
offline test suite. It does not mark the checkout as a managed application.
The uninstaller can therefore remove SAT's launchers, environment, and private
OpenClaw runtime while preserving the development checkout. Normal users
should use the managed command in the repository
[`README.md`](../README.md#install).

To update a checkout-bound installation, update the checkout through the
contributor's normal Git workflow, confirm that the tracked worktree is clean,
and rerun `./scripts/install.sh`.

Version changes and stable publication follow the separate
[`release and channel lifecycle`](releases.md). A normal development commit does
not increment the numeric release version or create a GitHub Release.

## Validation CLI

The unified CLI exposes focused validators for checked-in contracts:

```bash
uv run sat validate-config
uv run sat list-teams
uv run sat validate-task-brief benchmarks/task_manager/task-brief.json
uv run sat validate-artifact examples/implementation-plan.json
uv run sat validate-handoff examples/handoff.json
```

Structural validation is only the first boundary. Before persistence, the
artifact store also verifies run, team, iteration, role, stage, commit,
acceptance-criterion, canonical-path, referenced-content, and digest context.
See [`runtime-evidence.md`](runtime-evidence.md) for the full evidence model.

Planning-schema regressions must test relation-bearing model fields as atomic
records. In particular, requirements use `{id, description}` and assumptions
use `{statement, decision_id}` at the model boundary; tests must not recreate
parallel model-owned arrays whose cardinality can diverge. Persisted historical
schemas may retain their compiled indexes for backward readability.

Validate the product profile separately from the default evaluation fixture:

```bash
uv run sat validate-config \
  --policy configs/product-policy.json \
  --quality-manifest profiles/python/quality.json
```

## Runtime Image and Evaluation-Fixture Updates

`make lock-runtime` intentionally refreshes the shared Python runtime dependency
lock. Set `RUNTIME_EXCLUDE_NEWER=YYYY-MM-DD` only as part of a reviewed
dependency update, then rebuild and record a new sandbox image ID.

Build the exact image named by both product and evaluation policies with:

```bash
docker build \
  --tag sat-python-quality:phase1-v6 \
  runtime/python
```

OpenClaw explicitly supplies `sleep infinity` when it creates a scope-owned
role container; the image uses the same command as a convenient standalone
diagnostic default. `scripts/install.sh` and live-run preflight both start a
no-network, read-only-root probe, execute the Reviewer probe runner's self-test
inside it, inspect its state, and remove it. A successful `docker build`, image
lookup, or momentary container start alone is not sufficient runtime evidence.

The image includes the exact `uv` pinned in `runtime/python/requirements.in`
and a locked offline wheelhouse containing project setup and build
dependencies. The product quality profile copies clean committed files into
fresh executable tmpfs scratch, then runs the exact generated setup, test, and
start argv with network disabled. The source and container root remain
read-only, and the process remains non-root, capability-dropped, and
resource-bounded. Before setup, the profile parses every `uv.lock` tracked in
the proposed Git delivery, even when an ignore rule also matches it, and rejects
host- or sandbox-only local sources. An effectively ignored untracked lock is
runtime residue outside the delivery and is absent from the clean-copy command
gate; the private wheelhouse may satisfy runtime resolution but its absolute
path must never be committed into the generated project. The image also
installs the root-owned immutable
`sat-probe-write` helper. That command can atomically create only a new bounded
`/tmp/sat-review-probe-*` `.py`, `.json`, or `.txt` direct child; it refuses
overwrite and unsafe paths while the project mount and general write tools stay
read-only. Authored Python probes run only through `sat-probe-run`; it validates
the owner-only file, executes its open descriptor with a fixed interpreter and
project working directory, enforces a 30-second child timeout and bounded
output, and emits `SAT_PROBE_RESULT_V1` as the authoritative terminal child
result. Its explicit stdout/stderr frames let the controller exclude traceback
source text from positive satisfied claims while preserving stderr for blocked
counterexamples. Change either runtime capability and the policy image tag together; an
old tag must not claim the newer probe capability.

Use the Docker cgroup `--pids-limit` for the per-container process boundary.
Do not add an `nproc` ulimit as a duplicate control: `RLIMIT_NPROC` can count
processes sharing the numeric UID outside the container, which can make Docker
fail to execute even the container's initial process with `EAGAIN`.

At run start, the controller resolves the configured image tag to its local
`sha256:...` image ID. The run-scoped Agent configuration and every quality
gate use that immutable ID; preflight fails if the configured tag changes
between resolution and workspace setup or if the restricted probe cannot run
its tool helper and remain alive.

## Model Catalog Compatibility

`src/software_agent_team/runtime_configuration.py` owns reviewed catalog
supplements for exact provider models absent from the pinned OpenClaw release.
A supplement must remain narrow, versioned in Git, secret-free, and covered by
materialization plus exact-availability tests. Record only verified routing,
modality, context, output, and compatibility metadata; do not add a credential,
mutable fallback, or guessed price.

A compatibility entry may also carry provider-native choices for the
invocation-bound artifact protocol. Apply a named terminal-function choice only
when submission is the invocation's sole semantic action. A dynamic team with work
or evidence tools must use a compatible required-any-tool choice instead, so the
provider cannot return plain text but also cannot force the terminal tool before
work. Model inspection, provider smoke, and legacy text response paths receive no
choice override. Test both outbound requests through the pinned OpenClaw: the
bootstrap named choice, the dynamic work-tool-to-submission sequence, canonical
single `artifact` envelope, terminal one-request behavior, and exact sandbox cleanup.
Do not treat a prompt instruction as evidence that the provider must call a tool.

When changing targeted correction, test object-only capture followed by the exact
Controller-owned per-slot semantic schema,
order-independent opaque-handle binding, exact handle coverage, atomic rejection,
and post-application validation. A different diagnostic fingerprint is not by
itself improvement: regressions to a coarser type or shape in the same authority
slot must stop. Review evidence-selector correction must use Controller-issued
candidate handles and exact-byte binding; candidate generation and final
grounding must share the same whole-chain eligibility policy, including
cross-result failed-match contamination. A model-authored replacement string is
not an acceptable compatibility path. Add a regression where one exact fragment
appears in both a successful result and an otherwise failed result, and prove it
is absent from the correction catalog while an uncontaminated candidate still
passes post-application grounding.

Candidate-selection regressions must distinguish model-submission errors from
Controller plan faults. Cover mixed valid/invalid choices, wrong-slot handles,
duplicate or missing identity, all-invalid/no-progress termination, and immutable
staging with a strictly smaller pending set. Exercise the dynamic runner's final
grounding and ledger, not only the application helper. Partial bindings never
publish an artifact; free-form siblings must not be retained without validation.
Test cancel and interrupt recorded between calls: the shared invocation-admission
check must prevent initial work, correction, fallback, and continuation alike,
without creating another reservation or losing the previous invocation's evidence.

Writer instructions must name the exact invocation input commit as the immutable
revision base. Detached HEAD is expected; a branch can still name the starter
and is not authority to reset or rebuild history. Keep the independent ancestry
gate strict. Prompt guidance explains the contract but is not a capability-level
guarantee that an Agent cannot rewrite its writable Git metadata; a violation
must still fail closed without delivery.

Revision integration tests must exercise real temporary Git history through the
workflow, not only the snapshot helper. Pair a valid descendant with a writer
soft-resetting to the starter before its second commit. Verify that the latter
cannot reach another quality/Review stage or delivery, the report does not claim
the rejected commit, all calls settle once, and the source repository is unchanged.
Mocked model submissions and quality commands in this test do not establish
provider-backed revision acceptance.

When changing live progress, derive labels only from allow-listed tool identity.
Tests must prove that unknown executable names, command arguments, output, paths,
and secrets do not enter activity records or rendered summaries, and that
coalesced start/completion history retains the current lifecycle counters.

Startup and run preflight inspect OpenClaw's configured model view without a
provider filter so the check stays on configured local catalog/auth evidence
and does not invoke provider discovery or content generation. A real provider
smoke request is a separate explicitly authorized test. When validating a
trusted shell credential path, the generated configuration may contain the
environment-variable reference but never its value.

Keep the local model-catalog timeout independent from ordinary preflight and
model-work time authority. The catalog subprocess has a 90-second default to
cover cold pinned-OpenClaw startup; other local preflight commands remain at 30
seconds. Both values belong in `RuntimePreflight` evidence. Tests must verify
the exact subprocess bounds, safe timeout text, visible pre-wait status, and
that a model-inspection failure creates no Planning Agent or run state.

Provider liveness is a separate infrastructure contract. Model inspection
records catalog locality and any configured provider request timeout; the
execution adapter resolves those facts against the pinned OpenClaw first-event
boundary. The lease starts from an attributable current-turn or provider-stream
checkpoint, never subprocess startup. Tests for a liveness change must cover a
long productive stream, sustained silence, activity during grace, an active tool, degraded attribution,
provider-terminal handoff, renewable response finalization, finalization hang,
exact-process cleanup, persisted Planning and execution evidence, and compact
versus detailed progress. Register each changed threshold or polling constant in
`decision_limits.py`; do not turn it into an Agent work budget.

Initialization liveness precedes that lease and uses the same execution-adapter
lifecycle for Planning and runtime Agents. Tests must separately cover slow
checkpoint progress, permanent startup hang, recovery inside the visible grace,
missing or malformed state observation, exact user interrupt/cancel, user
deadline, controlled-evaluation timeout, TERM exit, KILL escalation, evidence
collection, coalesced tool start/completion snapshots, repeated active-tool
snapshots, absence of stale working heartbeats, inherited session state, and a
reused prompt that requires a newly observed turn occurrence. Historical tool counters
must still produce activity evidence, but only a change in the observed active
tool state may produce a lifecycle phase transition. Apply an attributed session
snapshot before publishing any tool-history delta from that observation. When a
single poll coalesces a start and completion, both history events therefore carry
the current inactive state and the current completed count; neither event may
reconstruct a stale `tool_active` phase from its kind. Persisted invocation state,
detailed footers, and heartbeat updates must all consume that checkpoint, including
hidden events and visibility changes. Test stream-during-tool, coalesced history,
late history during shutdown, same-phase clock preservation, and distinct scheduler
decisions. Include phase publication before the corresponding history delta:
the displayed completed-tool count must come from the current numeric snapshot,
not a cached prose total in the previous action description. `RunEvent` schema v5 must
remain canonically readable from v2 through v4, Artifact schema v10 from v2
through v9, lifecycle schema v3 from v1 through v2, and Planning schema v13 from
v2 through v12. Main-thread and repeated SIGINT tests must prove exact child
cleanup before lease release, CLI exit 130, and a terminal Planning turn/session.
Run the CLI interrupt path with the real task ledger as well: after that process
exits, reload the turn and verify its unique settlement, unknown cost, separate
accounting error, and terminal session hash. Separate mocked-exception and
unaccounted subprocess tests do not establish that composed boundary.
Adapter exceptions must retain unknown process/provider evidence and settle once;
accounting failures must not replace the original interruption or execution error.
Accounted Planning turns retain the exact shared-ledger call
record, including invalid responses before runtime creation; their exports must
remain independently cost-auditable without provider session stores. New plans always bind a material primary workflow to user input
or clarification and downstream requirements, even for a throwaway prototype;
historical preview alone retains the old exemption. Tool-evidence regression
tests must preserve a valid async `exec` start as nonterminal `deferred`
evidence, require a later terminal `process` result independently, and still
require the invocation-bound terminal submission. Deferred evidence must never
support a satisfied Review claim. Unknown status values, malformed handles, and
running shapes with terminal fields fail closed.
Initialization file-race tests must distinguish an exact open-time missing file
from malformed or unsafe evidence and must cover atomic publication between
observations. Do not infer an earlier `open` result from a later `exists` check.
Accelerated live-process fixtures must also own their semantic preconditions:
publish malformed observer input before launch when malformed input is under
test, and establish an earlier attributed checkpoint before deliberately
delaying a later one. Leave a clear order-of-magnitude margin between host
scheduling jitter and the tested delay; never require a child interpreter to
win a subsecond race merely to select the expected test branch.
Streaming-renewal tests must accept either attributable lease-start source;
test a specific source by controlling observer availability or publishing only
that source, not by assuming session polling wins a short sleep before streaming.
Any added phase, stop reason, or infrastructure threshold belongs in the one
shared lifecycle or decision-limit registry rather than a parallel adapter.

The live Planning response schema and the persisted Planning schema have
different ownership. Current model output binds every requirement as one atomic
`{id, description}` record. The Planning boundary compiles that relation into
the existing backward-readable description and stable-ID fields before semantic
validation. Do not reintroduce independently generated requirement arrays or
offer either array alone as a relational correction target. A live-contract
change updates its submission schema hash; bump the persisted Planning schema
only when stored canonical structure changes.

Apply the same authority atomicity to ProductDefinition questions. One focused
question may resolve exactly one ProductDefinition dimension. Tests must reject
a question that assigns one free-text answer to multiple dimensions and must
prove that resolving one dimension leaves every explicit sibling dimension
unchanged. Legacy persisted transcripts remain readable; current admission is
where the narrower authority contract is enforced.

Typed-submission tests must distinguish transport attempts from successful
semantic submissions. OpenClaw may return a schema error to the model and let it
retry within the same invocation. Any number of attributable failed attempts may
precede exactly one final successful call, because none wrote or authorized a
semantic payload. Continue to reject two successful calls, a successful call
followed by any other tool action, an all-failed sequence, or a private file that
does not bind the sole final success.

Apply the same atomicity rule to decision authority. The model-facing decision
schema must enumerate only legal category/provenance combinations; it must not
expose independently selectable values that the persisted model rejects after
submission. Test every new normalization with both the exact malformed shape it
accepts and a nearest valid counterexample it must preserve. In particular,
direct-product deduplication requires an exact ProductDefinition source, the
matching semantic category, and no retained reference to the candidate decision;
same-source privacy or risk decisions are not product-definition duplicates.

The product profile and evaluation fixture share this dependency image, not a
TaskBrief, seed, acceptance suite, environment contract, or delivery command.
The benchmark contract remains frozen for comparable trials. The confirmed
[`task-brief.json`](../benchmarks/task_manager/task-brief.json) is the
authoritative Agent input. The human-readable
[`requirements.md`](../benchmarks/task_manager/requirements.md) summarizes it
and must not introduce additional requirements.

## Source-of-Truth Map

The authoritative owner for every documentary and executable concept is listed
once in [`VISION.md`](../VISION.md#ownership-boundaries). Use that table before
adding a new definition or moving an existing one.

Do not maintain parallel role lists, schemas, state machines, or legacy CLI
entry points. A replacement removes or migrates its predecessor in the same
change unless a time-bounded removal plan is documented.

## Repository Layout

```text
benchmarks/task_manager/
  task-brief.json              Frozen confirmed benchmark input
  requirements.md              Human-readable contract summary
  benchmark.json               Fixed commands and criterion coverage
  seed/                        Deterministic starting repository
configs/
  teams.json                   Team topology source of truth
  product-policy.json          Product deterministic-quality sandbox policy
  run-policy.json              Controlled evaluation policy
  openclaw.example.json5       Sanitized role and tool policy template
docs/
  README.md                    Documentation index
  product-demo-slice.md        Guided user-journey acceptance specification
  adaptive-orchestration.md    Dynamic-team and interactive-control specification
  installation.md             Install, configure, export, and uninstall
  releases.md                 Version, release, and channel maintainer workflow
  runtime-evidence.md          Runtime, artifact, evidence, and safety model
  phase1-runbook.md            Controlled provider-backed evaluation procedure
  development.md               This development guide
profiles/python/
  contract-template.json       Stable product criterion-ID contract
  quality.json                 Generic project checks and coverage
  seed/                        Greenfield product source baseline
  validation/run.py            Trusted project-command and docs validator
  validation/run_commands.py   Clean-copy exact-command validator
runtime/python/
  Dockerfile                   Shared content-pinned quality image
  requirements.in             Direct runtime dependencies
  requirements.lock           Exact transitive dependency lock
src/software_agent_team/
  artifacts.py                 ProductDefinition, TaskBrief, and persisted schemas
  artifact_store.py            Write-once artifact and output persistence
  assembly.py                  Semantic response and verified-fact assembly
  budgets.py                   Agent and pricing budgets
  cli.py                       Unified command-line interface
  controls.py                  Persisted user-control command contracts
  dynamic_runner.py            Approved dynamic Agent invocation lifecycle
  dynamic_workflow.py          Adaptive lifecycle convergence and decisions
  execution.py                 OpenClaw and offline execution adapters
  git_workspace.py             Standalone clones and snapshot verification
  integrity.py                 Canonical persisted-model integrity digest
  invocation.py                Controller-owned call accounting and evidence
  loopback_validation.py       Controlled real-transport lifecycle oracle
  model_metadata.py            Attributable model price/context source values
  openclaw_session_evidence.py Pinned current-turn tool-evidence extraction
  openclaw_runtime.py          Private OpenClaw path and environment isolation
  paths.py                     User-local product state resolution
  planning.py                  Adaptive dialogue, proposals, approval, and evidence
  product.py                   Diagnostics, source preparation, and delivery
  progress.py                  RunEvent journal and terminal rendering
  process_lifecycle.py         Durable provider-process ownership and recovery
  prompting.py                 Fixed-role and task-defined capability prompts
  quality_gates.py             Fixed sandboxed command runner
  reporting.py                 Shared terminal report rendering
  response_corrections.py      Typed validation and field-targeted correction
  responses.py                 Strict fixed and run-scoped Agent response parser
  run_control.py               Lifecycle state and atomic persistence
  runtime_configuration.py     Run-scoped OpenClaw config and preflight
  scheduling.py                Approved DAG and shared-workspace scheduling
  schema_compatibility.py      Persisted-schema registry and candidate protocol
  self_check.py                Task-readiness schema, dependency graph, and store
  state_layout.py              Authoritative state-category lifecycle manifest
  teams.py                     TeamPlan contracts and fixed-fixture compilation
  uninstall_state.py           Transactional state export and purge boundary
  user_configuration.py        User-local secret-free live-run defaults
  versioning.py                Release, source, and managed-install identity
  releases.py                  Stable Release manifest and resolver
  release_tools.py             Change-impact and release-candidate gates
  managed_install.py           Staged activation and task/update lifecycle leases
  updates.py                   Update and channel-change planning
  workspace_mounts.py          User-owned sandbox mountpoint preparation and repair
  workflow.py                  Fixed-fixture compatibility orchestration
scripts/
  bootstrap.sh                 Remote managed-install entry point
  install-openclaw.sh          Checksum-verified private dependency installation
  install.sh                   Locked Linux/WSL application installation
  release.py                   Release manifest and candidate gate CLI
  openclaw-environment.sh      Private OpenClaw shell-process environment
  uninstall.sh                 Guided preservation, export, and removal
  setup.sh                     Development environment setup
  doctor.sh                    Environment and boundary diagnostics
tests/                         Offline unit, integration, and end-to-end tests
release/change-impact.json     Machine-readable SemVer impact ledger
.github/workflows/release.yml  Exact-tag GitHub Release automation
README.md                      User-facing public overview and quick start
STATUS.md                      Current implementation and evaluation evidence
VISION.md                      Product, architecture, experiment, and roadmap
```

`openclaw/workspaces/` contains stable ignored role workspace boundaries. The
installed private OpenClaw binary lives under ignored `.sat/openclaw/`.
Product `planning/`, `self-checks/`, `process-leases/`, `runs/`, `workspaces/`,
`sources/`, and isolated OpenClaw provider state live under the private
user-state root. Provider credentials,
active OpenClaw state, generated state, and runtime configuration never enter
Git.

## Documentation Workflow

Update the document that owns the changed fact:

- Update `README.md` only when the user-facing overview, requirements, install
  command, first-use path, or primary user commands change;
- Update `VISION.md` when product behavior, architecture decisions,
  experimental design, scope, or roadmap changes;
- Update `STATUS.md` when an implementation path, milestone, evaluation
  evidence, or known gap changes;
- Update `docs/product-demo-slice.md` when the guided user-journey interaction
  or acceptance criteria change; keep implementation status in `STATUS.md`;
- Update `docs/adaptive-orchestration.md` when the planned Planning, TeamPlan,
  progress, control, model-routing interaction, implementation sequence, or
  acceptance contract changes; keep implementation status in `STATUS.md`;
- Update `docs/installation.md` when setup, saved configuration, export, or
  uninstallation behavior changes;
- Update `docs/runtime-evidence.md` when runtime, artifact, response, integrity,
  or safety behavior changes;
- Update `docs/phase1-runbook.md` when a controlled provider-backed evaluation
  procedure or evidence checklist changes;
- Update benchmark documentation only with an explicitly versioned benchmark
  change.

Avoid copying a full contract into multiple documents. A summary should link
to its authoritative owner.

## Contribution Workflow

1. Inspect `git status` before editing.
2. Read `VISION.md` before changing architecture, scope, or experiments.
3. Keep every experimental variable and budget explicit.
4. Add or update tests with behavior changes.
5. Update public documentation in the same change.
6. Apply the [validation policy](#validation-policy); record scoped checks for
   work-in-progress commits and run `make check` before batch integration/release.
7. Review the staged diff and commit with a Conventional Commit message.

Use this message form:

```text
<type>(<scope>): <lowercase subject without a period>
```

Negative and inconclusive outcomes are acceptable when their evidence and
limits are reported honestly.

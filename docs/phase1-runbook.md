# Phase 1 Provider-Backed Evaluation Runbook

This contributor/operator runbook is the procedure for a controlled evaluation
using a real model provider. It is not the normal user journey; users should
start with the repository [`README.md`](../README.md). Current evaluation
results belong in [`STATUS.md`](../STATUS.md), not in this procedure.

Every trial requires a fresh run ID and the complete evidence checks below.
Repository development and offline tests do not make a model call. Following
the `sat run` step below does.

Use [`installation.md`](installation.md) for the normal install, configuration,
export, and uninstall lifecycle. Use
[`runtime-evidence.md`](runtime-evidence.md) for the underlying runtime,
artifact, response, integrity, and safety model. This runbook owns only the
ordered evaluation procedure and its acceptance checklist.

## Acceptance Objective

Exercise the complete `function_specialized` path against the frozen
`task_manager_phase1_v2` benchmark:

```text
Planner
→ Generalist Developer
→ deterministic quality gates
→ Tester + Reviewer
→ accept or one evidence-driven revision
→ terminal report
```

A useful failed run remains valid experimental evidence, but the Phase 1
acceptance evaluation should reach `completed`, exercise every role, preserve
the actual model and token telemetry, and leave a reproducible Git snapshot.
Do not combine earlier benchmark-version results with version-two comparisons.
Record the harness Git commit with every trial so the acceptance input is
attributable to its checked-in version.

## Preconditions

- Run on Linux or WSL under an unprivileged host account. `sat preflight` and
  `sat run` reject UID or GID `0` because the writable Agent container uses the
  invoking account's numeric identity.
- Make Git, Bash, Docker, and the pinned local toolchain available.
- Give the unprivileged account access to the Docker daemon without changing
  the repository files to world-writable mode.
- Place the repository and the explicitly selected `runs/` and `workspaces/`
  roots on a disposable or
  quota-controlled filesystem. Docker bind mounts do not provide a portable
  per-workspace disk quota, so host storage capacity is an operator-owned safety
  boundary.
- Configure credentials through `sat configure`, which delegates entry to
  SAT's isolated OpenClaw-owned state, or provide them through the trusted
  caller environment. Do not put credentials in this repository, SAT
  configuration, runtime evidence, command history, or a TaskBrief. SAT never
  imports an existing OpenClaw profile.
- Select one exact `provider/model` identifier and record its current input and
  output prices per million tokens. The run configuration disables model
  fallback, and the controller rejects missing or different model telemetry.
- Start with enough approved provider budget for the ceilings in
  `configs/run-policy.json`, and configure a provider-side spending or quota
  limit no greater than the authorized amount. Token and cost telemetry arrives
  after an invocation, so the controller can fail the run and prevent the next
  call but cannot reverse charges from the invocation that crossed a threshold.
- Treat `agent_calls` as controller invocations. OpenClaw or a provider SDK may
  make internal same-model attempts; retain provider usage records when that
  distinction matters to the experiment.
- Determine whether the provider supports two simultaneous generations. Keep
  the default verification concurrency of two only when it does; otherwise use
  `--verification-concurrency 1`. Role Agents are forbidden from spawning
  additional Agent calls outside controller accounting.

## 0. Install the Harness

From a clean checkout, run:

```bash
./scripts/install.sh
```

The installer prepares the pinned user-local toolchain, a marked
application-private OpenClaw runtime, locked Python environment, checkout-bound
`sat` and `sat-uninstall` launchers, shared Python quality image, and offline
checks. It requires Git, curl, and a running Docker daemon that the
unprivileged user can access. It does not install Docker at the operating-system
level or create provider credentials. Another OpenClaw installation, whether
configured and running or not, remains outside every SAT ownership boundary.

Run the guided provider setup once unless credentials come from the trusted
caller environment:

```bash
sat configure
```

This invokes only SAT's private OpenClaw binary and state. It does not read or
modify another OpenClaw profile.

Save or inspect the exact non-secret advanced defaults for the planned trial.
Do not run bare `sat`; that is the separate interactive product surface.

```bash
sat configure --non-interactive \
  --model provider/model \
  --input-cost-per-million-usd 0.00 \
  --output-cost-per-million-usd 0.00 \
  --verification-concurrency 1 \
  --use-role-timeouts
sat configure --show
```

The configuration records model, current token prices, verification
concurrency, and an optional global role-stage timeout override. It never
records an API key. With no override, the checked-in per-role budgets apply.
For tightly controlled experiments, the equivalent explicit `sat run` flags
below remain useful because the complete invocation can be copied into the
trial notes.

## 1. Verify the Checkout

From the repository root:

```bash
git status --short
make check
```

`git status --short` should print nothing. `make check` validates all checked-in
configuration and offline behavior without requiring Docker or provider
credentials.

## 2. Verify or Rebuild the Frozen Sandbox Image

The one-command installer builds the exact local tag named by
`configs/run-policy.json`. Rebuild it after an intentional runtime lock or
Dockerfile change:

```bash
docker build \
  --tag sat-python-quality:phase1-v2 \
  runtime/python
```

Do not substitute an unrecorded tag or allow an implicit registry pull. Both
Agent execution and quality gates use the locally built image, with external
network access disabled inside their containers.

## 3. Prepare the Benchmark Source

Create the deterministic seed repository at a new path:

```bash
uv run sat prepare-benchmark ./task-manager-source
git -C ./task-manager-source status --short
git -C ./task-manager-source log -1 --oneline
```

The status command should print nothing. Keep the reported base commit with the
trial notes. The preparation command refuses to overwrite an existing path.

## 4. Run Offline Preflight

```bash
uv run sat preflight ./task-manager-source
```

Expected result:

```text
runtime preflight: ready ... config=True image=True container=True ... source_commit=<commit>
```

Preflight validates the run-scoped, secret-free OpenClaw configuration, checks
SAT's pinned private OpenClaw binary and state boundary, confirms that the
sandbox executable is Docker, inspects the required image, and starts and
removes a restricted container after executing a Python tool helper inside it.
This proves both process startup and the operation OpenClaw needs before the
first Agent call. It does not contact a model provider or validate provider
quota.

Stop if preflight returns `not-ready` or exit code `1`/`2`. Correct the reported
environment problem before authorizing a paid call.

## 5. Execute One Provider-Backed Trial

Replace the model and prices with the exact operator-approved values. Do not
substitute or add another model trial without separate authorization:

```bash
uv run sat run \
  benchmarks/task_manager/task-brief.json \
  ./task-manager-source \
  --runs-root ./runs \
  --workspaces-root ./workspaces \
  --model provider/model \
  --input-cost-per-million-usd 0.00 \
  --output-cost-per-million-usd 0.00
```

For a provider with one generation slot, append
`--verification-concurrency 1` to that command. Serial dispatch preserves
independent verification: Tester and Reviewer still receive the same immutable
commit and controller evidence, and neither sees the other role's report.

The normal policy uses role-specific stage budgets from
`configs/run-policy.json`:

| Roles | Stage budget |
| --- | ---: |
| Clarifier | 120 seconds |
| Planner | 180 seconds |
| Single Agent, Developers, and Integrator | 900 seconds |
| Tester and Reviewer | 300 seconds |

A stage budget covers the initial response and its optional semantic repair
together. The controller starts one monotonic deadline before the initial
prompt, gives a repair only the remaining time, and records both the resolved
stage budget and remaining attempt budget. It does not reset the clock for a
repair.

If a measured provider requires an intentionally uniform budget, add
`--stage-timeout-seconds N` and record that override as an experimental
variable. `--use-role-timeouts` ignores a saved global override for one run.
The deprecated `--agent-timeout-seconds` spelling has the same shared-stage
meaning and will be removed in the next major release; it no longer represents
a separate per-attempt or per-process allowance. Do not change timeout policy
after a run starts or omit it from comparisons.

Use zero prices only when the selected model is genuinely free. The command
creates a fresh run and detached standalone clone; it never merges, pushes,
deploys, or publishes the generated result.

Exit codes are:

- `0`: the run completed and was accepted;
- `2`: the workflow reached an auditable failed terminal state;
- `1`: CLI input, setup, or an unexpected local runtime error prevented a
  normal workflow result.

Do not retry using the same run ID. For another trial, copy the frozen
TaskBrief, change only `TaskBrief.run_id`, and use a new source checkout at the
same base commit. The CLI rejects every other benchmark change.

## 6. Inspect the Evidence

For the default first run, inspect:

```bash
git -C workspaces/task-manager-phase1 status --short
git -C workspaces/task-manager-phase1 log --oneline --decorate -5
uv run sat validate-artifact runs/task-manager-phase1/final-report.json
```

The workspace status should print nothing. Review both
`runs/task-manager-phase1/final-report.md` and the structured run directory.

Confirm all of the following before marking Phase 1 accepted:

- `runtime-preflight.json` records the expected OpenClaw and Docker versions,
  `config_valid: true`, `sandbox_image_present: true`,
  `sandbox_container_ready: true`, no `sandbox_container_error`, and the exact
  local `sandbox_image_id`. Confirm the run-scoped OpenClaw config and
  quality-gate invocations use that ID rather than the mutable tag. Repeated
  comparison runs use the same image ID.
- `run.json` ends in `completed` with termination reason `succeeded` and
  references every material transition artifact.
- `implementation-plan.json` exists.
- Each iteration contains a controller-assembled and verified
  `work-result.json`, command
  stdout/stderr, `test-report.json`, `review-report.json`, and
  `iteration-record.json`.
- Planner, Generalist Developer, Tester, and Reviewer execution records exist;
  Tester and Reviewer evaluated the same immutable commit.
- Tester and Reviewer received bounded stdout/stderr tails for every fixed
  command. Full command output remains authoritative in
  `iterations/<nn>/commands/`. Reviewer source reads resolve through the
  read-only `/agent` mount.
- Every command record preserves its benchmark-owned `criterion_ids`. The
  controller copied the configured `manual_review_criteria`, marked those
  criteria `pending_review` after their deterministic portions passed, derived
  the overall Tester status from command results, and did not classify expected
  semantic review as a dependency blocker. The Tester supplied analysis and
  findings, not authoritative command or status echoes.
- The controller bound the same IDs into `reviewed_criteria`; the Reviewer
  inspected them on the immutable commit and returned no blocking finding. The
  final report, rather than the Tester report, contains the
  controller-resolved `passed` results for those criteria.
- A correctable implementation or acceptance-gate defect produced `revise`,
  regardless of product-impact severity. Any Reviewer `fail` record includes a
  terminal safety or evidence-integrity reason showing why another revision
  would be unsafe.
- No role execution spawned an untracked child Agent or one-shot model call.
- Every successful execution reports exactly the selected model and integer
  input/output token counts. No fallback or missing telemetry was accepted.
  The adapter normalizes the pinned OpenClaw local and Gateway JSON forms and
  compares their provider/model metadata using the canonical `provider/model`
  identity.
- Every semantic execution record identifies `semantic_body_v1`, lists the
  controller-supplied persisted fields, records any redundant model-returned
  controller fields that were ignored, and records the resolved stage and
  remaining attempt timeouts. Missing or incorrect echoes of `kind`, Git facts,
  command evidence, status, criteria, or scope are not treated as semantic
  response failures.
- Every handoff points to immutable artifact references with matching SHA-256
  digests.
- All fixed quality gates passed, every acceptance criterion has a result, and
  the Reviewer has no blocking finding.
- The final JSON and Markdown reports agree on status, commit, iteration count,
  calls, tokens, duration, estimated cost, and termination reason.
- The final standalone clone has no remote, remains detached and clean, and its
  commit descends from the recorded benchmark base commit.

If any item is missing or inconsistent, preserve the entire run directory and
workspace as failure evidence. Do not edit artifacts in place or reinterpret an
unrecorded action as successful.

## 7. Record the Evaluation Decision

Record the command, selected model, frozen prices, base commit, terminal commit,
artifact location, actual cost estimate, elapsed time, and any anomaly in the
experiment notes. Classify the trial against the checklist above. If it does
not satisfy the acceptance objective, preserve and classify the failure, fix
the controller or environment through a normal reviewed change, assign a new
run ID, and run a new trial.

Generated runs and workspaces are ignored local evidence. Archive any evidence
needed for the experiment before manually removing disposable run state.

## 8. Export Evidence Before Uninstalling

The guided uninstaller preserves product state by default. This runbook uses
explicit checkout-local `./runs` and `./workspaces` roots so trial evidence
stays adjacent to the recorded harness revision; those custom roots are not
owned by the uninstaller. Archive them separately before removal. The command
below exports SAT configuration and default product state only:

```bash
sat-uninstall --export-to "$HOME/sat-phase1-backup" --yes
```

The destination must not already exist. Inspect `EXPORT.txt` and any copied
configuration or default product-state roots before using `--purge-config` or
`--purge-data`. SAT's isolated provider credentials are excluded from export
and preserved unless `--purge-provider-state` is explicit. Every other
OpenClaw installation, uv, Docker, the shared quality image, the source
checkout, and the checkout-local trial roots remain outside the uninstall
ownership boundary. Review `sat-uninstall --help` before selecting a purge
option.

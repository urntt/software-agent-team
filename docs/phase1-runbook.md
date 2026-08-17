# Phase 1 Live-Trace Runbook

This runbook is the operating procedure for the next qualifying real
model/provider trace. Exploratory traces have already exercised the live path
and exposed implementation, protocol, and provider-capacity defects; none has
yet satisfied the Phase 1 exit criterion. Repository development and offline
tests do not make a model call. Following the `sat run` step below does.

## Acceptance Objective

Exercise the complete `function_specialized` path against the frozen
task-management benchmark:

```text
Planner
→ Generalist Developer
→ deterministic quality gates
→ Tester + Reviewer
→ accept or one evidence-driven revision
→ terminal report
```

A useful failed run remains valid experimental evidence, but the Phase 1
acceptance trace should reach `completed`, exercise every role, preserve the
actual model and token telemetry, and leave a reproducible Git snapshot.

## Preconditions

- Run on Linux or WSL under an unprivileged host account. `sat preflight` and
  `sat run` reject UID or GID `0` because the writable Agent container uses the
  invoking account's numeric identity.
- Make Git, Bash, Docker, and the pinned local toolchain available.
- Give the unprivileged account access to the Docker daemon without changing
  the repository files to world-writable mode.
- Place the repository, `runs/`, and `workspaces/` on a disposable or
  quota-controlled filesystem. Docker bind mounts do not provide a portable
  per-workspace disk quota, so host storage capacity is an operator-owned safety
  boundary.
- Configure credentials through OpenClaw's user-local credential store or the
  trusted caller environment. Do not put credentials in this repository,
  runtime configuration, command history, or a TaskBrief.
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

## 1. Verify the Checkout

From the repository root:

```bash
git status --short
make check
```

`git status --short` should print nothing. `make check` validates all checked-in
configuration and offline behavior without requiring Docker or provider
credentials.

## 2. Build the Frozen Sandbox Image

Build the exact local tag named by `configs/run-policy.json`:

```bash
docker build \
  --tag sat-task-manager-quality:phase1-v1 \
  benchmarks/task_manager
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
trace notes. The preparation command refuses to overwrite an existing path.

## 4. Run Offline Preflight

```bash
uv run sat preflight ./task-manager-source
```

Expected result:

```text
runtime preflight: ready ... config=True image=True ... source_commit=<commit>
```

Preflight validates the run-scoped, secret-free OpenClaw configuration, checks
the pinned OpenClaw binary, confirms that the sandbox executable is Docker, and
inspects the required image. It does not contact a model provider or validate
provider quota.

Stop if preflight returns `not-ready` or exit code `1`/`2`. Correct the reported
environment problem before authorizing a paid call.

## 5. Execute One Live Trace

Replace the model and prices with the exact approved values:

```bash
uv run sat run \
  benchmarks/task_manager/task-brief.json \
  ./task-manager-source \
  --model provider/model \
  --input-cost-per-million-usd 0.00 \
  --output-cost-per-million-usd 0.00
```

For a provider with one generation slot, append
`--verification-concurrency 1` to that command. Serial dispatch preserves
independent verification: Tester and Reviewer still receive the same immutable
commit and controller evidence, and neither sees the other role's report.

The default per-Agent timeout is 600 seconds. If a measured provider cannot
finish the Developer role inside that bound, choose a larger explicit
`--agent-timeout-seconds` value before the trial and record it as an
experimental variable. Do not change the timeout after a run starts or omit it
from comparisons.

Use zero prices only when the selected model is genuinely free. The command
creates a fresh run and detached standalone clone; it never merges, pushes,
deploys, or publishes the generated result.

Exit codes are:

- `0`: the run completed and was accepted;
- `2`: the workflow reached an auditable failed terminal state;
- `1`: CLI input, setup, or an unexpected local runtime error prevented a
  normal workflow result.

Do not retry using the same run ID. For another trial, copy the frozen
TaskBrief, change only `run_id`, and use a new source checkout at the same base
commit. The CLI rejects every other benchmark change.

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
  `config_valid: true`, `sandbox_image_present: true`, and the exact local
  `sandbox_image_id`. Confirm the run-scoped OpenClaw config and quality-gate
  invocations use that ID rather than the mutable tag. Repeated comparison runs
  use the same image ID.
- `run.json` ends in `completed` with termination reason `succeeded` and
  references every material transition artifact.
- `implementation-plan.json` exists.
- Each iteration contains a controller-verified `work-result.json`, command
  stdout/stderr, `test-report.json`, `review-report.json`, and
  `iteration-record.json`.
- Planner, Generalist Developer, Tester, and Reviewer execution records exist;
  Tester and Reviewer evaluated the same immutable commit.
- Tester and Reviewer received bounded stdout/stderr tails for every fixed
  command. Full command output remains authoritative in
  `iterations/<nn>/commands/`. Reviewer source reads resolve through the
  read-only `/agent` mount.
- No role execution spawned an untracked child Agent or one-shot model call.
- Every successful execution reports exactly the selected model and integer
  input/output token counts. No fallback or missing telemetry was accepted.
  The adapter normalizes the pinned OpenClaw local and Gateway JSON forms and
  compares their provider/model metadata using the canonical `provider/model`
  identity.
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

## 7. Record the Phase Decision

Record the command, selected model, frozen prices, base commit, terminal commit,
artifact location, actual cost estimate, elapsed time, and any anomaly in the
experiment notes. Mark Phase 1 complete only after the checklist above is
satisfied. Otherwise, classify the failure, fix the controller or environment
through a normal reviewed change, assign a new run ID, and run a new trace.

Generated runs and workspaces are ignored local evidence. Archive any evidence
needed for the experiment before manually removing disposable run state.

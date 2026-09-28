# Fixed-topology comparison

This procedure measures the one-writer `single_agent` baseline against the
`function_specialized` fixed team. It is an evaluation entry point through
`sat run --team`; it does not add a team selector to the ordinary `sat` journey.
The team choice changes Agent count and review/revision topology. Git isolation,
OpenClaw runtime boundaries, deterministic quality gates, run budget, model
route, telemetry, and terminal evidence remain shared.

## Frozen inputs

The checked-in comparison fixtures are:

| Request | Benchmark | Seed |
| --- | --- | --- |
| Task-management Web app | `benchmarks/task_manager/comparison-benchmark.json` | `benchmarks/task_manager/seed/` |
| Offline text statistics CLI | `benchmarks/text_stats/benchmark.json` | `benchmarks/text_stats/seed/` |

The task-manager comparison fixture (`task_manager_structure_comparison_v2`)
retains its functional acceptance suite, including same-origin absolute
`Location` handling, but removes the original benchmark's two manual-review
criteria. Documentation
and accessibility remain request requirements and must be scored by the same
external rubric after each arm. The separate fixture prevents the baseline
from claiming that a nonexistent independent Reviewer passed them. The text
statistics fixture checks exact CLI behavior, error paths, generated tests,
lint, and documentation with the same deterministic commands in both arms.
These fixtures do not replace the original Phase 1 task-manager benchmark.

For each request and arm, use a fresh run ID and a fresh clean Git repository
initialized from the same seed commit. Copy the fixture TaskBrief and change
only `run_id`; `sat run` rejects other TaskBrief changes. Use the same SAT
revision, benchmark file digest, exact `provider/model`, token prices, policy,
OpenClaw profile, sandbox image, and external quality rubric in both arms.
Keep the default model route strict and record provider internal retries if
visible. Team size, its allocated calls, and its actual cost and duration are
outcomes, not values to normalize away. Run each arm with one common aggregate
budget ceiling; the baseline has one implementation pass, while the fixed team
may use its allowed evidence-driven revision. Report that asymmetry explicitly.

The fixed evaluation command is:

```bash
sat run BRIEF.json SOURCE_REPOSITORY \
  --team single_agent \
  --benchmark BENCHMARK.json \
  --model provider/model \
  --input-cost-per-million-usd INPUT_RATE \
  --output-cost-per-million-usd OUTPUT_RATE \
  --verification-concurrency 1 \
  --runs-root RUNS_ROOT \
  --workspaces-root WORKSPACES_ROOT
```

Repeat with `--team function_specialized` and a different run ID and source
checkout. The baseline accepts only a benchmark whose criteria all have
controller-owned deterministic checks. Its final report explicitly records
that no independent Agent review occurred. An unsuccessful run still has a
terminal report, budget ledger, frozen workspace evidence when available, and
failure stage; do not recast it as a quality pass. Review the run directory's
`final-report.json`, `budget-ledger.json`, `run.json`, execution records, and
command evidence, then inspect the output commit in the isolated workspace.
Copy those immutable run and workspace trees to an evaluation archive to
export the result. Verify the archived report and commit against the source
before scoring.

## Comparison record

Predeclare the external rubric before running either arm. Score every frozen
request against the same functional and semantic checks, including README
usability, accessibility where relevant, maintainability, and security. Record
pass/fail per criterion, terminal status, failed stage, model calls, provider
usage, settled and estimated cost, elapsed time, output commit, and any
intervention. Report each arm's raw result and the aggregate success fraction;
with only two requests, describe direction and limitations rather than infer
statistical superiority. Missing telemetry, failed runs, or rubric gaps must
remain visible. A task-defined TeamPlan is a separate third arm only after its
approved plan, model policy, and starting TaskBrief can be frozen without
changing the ordinary user journey or its approval semantics.

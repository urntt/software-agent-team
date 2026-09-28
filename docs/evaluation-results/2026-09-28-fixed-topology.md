# Two-request fixed-topology comparison, September 28, 2026

This is the first provider-backed comparison of the executable one-writer
`single_agent` baseline with the fixed `function_specialized` team. It is a
controlled evaluation, not an ordinary-user installation-to-delivery run.
The procedure and its limits are defined in
[`fixed-comparison.md`](../fixed-comparison.md).

## Frozen conditions

Both arms for each request used SAT revision
`7af665007c23333f66c05a09270f3a7f7a2710fc`, the same confirmed TaskBrief
apart from `run_id`, the same initial Git commit, the same
`deepseek/deepseek-v4-flash` route, one verification slot, policy
`phase1_deterministic`, and quality image
`sha256:d1f5de5b370e3de25cf529468f921a4085b90d822d0bebdb3490ba579b9ff182`.
The task-manager seed was `ac950831a6bfffc9fbfa824948dd883ff36d629d`;
its comparison benchmark was `task_manager_structure_comparison_v2`. The
text-statistics seed was `9743bd3cf5484283c7265bb2c151362ebf16648a`;
both arms used the same checked-in benchmark. The TaskBrief, model route,
acceptance commands, sandbox, and aggregate limits were held fixed within
each pair. Team size, review, available revision, calls, cost, and time were
the measured structural differences.

DeepSeek's [published pricing](https://api-docs.deepseek.com/quick_start/pricing/)
at the trial time mapped the legacy route to V4.1 Flash. SAT used the
off-peak cache-miss input rate of $0.15 per million tokens, cache-read rate
of $0.003, and output rate of $0.60 for estimates. These are SAT estimates,
not provider invoices. Provider behavior behind the alias can change.

## Outcomes

| Request | Arm | SAT delivery | Deterministic gates | Independent extra input | Calls | Wall time | Estimated cost |
| --- | --- | --- | --- | --- | ---: | ---: | ---: |
| Task-management Web app | Single Agent | Completed | 4/4 pass | UTF-8 HTTP create/detail pass | 1 | 146.90 s | $0.014970 |
| Task-management Web app | Fixed team | Failed: Reviewer returned prose instead of the required JSON result | 4/4 pass | UTF-8 HTTP create/detail pass | 4 | 267.05 s | $0.028703 |
| Offline text statistics CLI | Single Agent | Completed | 4/4 pass | Extra UTF-8/whitespace case pass | 1 | 94.55 s | $0.007686 |
| Offline text statistics CLI | Fixed team | Completed | 4/4 pass | Extra UTF-8/whitespace case pass | 4 | 162.55 s | $0.014851 |

Across these two requests, Single Agent delivered 2/2 outputs in 241.45 s
and an estimated $0.022656 over two model calls. The fixed team delivered
1/2 in 429.60 s and an estimated $0.043554 over eight calls. The failed
team output still passed all four deterministic commands and the independent
HTTP probe; the Controller correctly refused to label it an accepted
delivery when the Reviewer submitted an invalid response. Treating that code
as an accepted SAT delivery would hide a real orchestration failure.

The independent static rubric found installation, use/start, tests, and
limitations in all four generated READMEs. Both Web outputs had a `<main>`
landmark, labels bound to form control IDs, native buttons, and no scripts or
remote URLs in application source. Both CLI outputs included generated tests
for missing paths and invalid UTF-8. A browser-based, human keyboard-only
walkthrough was not performed, so that narrower accessibility claim remains
unverified. The extra probes ran in the pinned quality image with a read-only
workspace and no network access; they did not replace SAT's terminal checks.

## Interpretation and reproducibility

These paired results do not establish statistical superiority or a result
for other tasks, models, providers, or the ordinary task-defined team. They
show that the one-writer baseline is executable and can produce accepted
deliveries under the same safety and quality gates; in this small sample,
the additional fixed-team Reviewer increased cost and latency and caused one
delivery failure even though the generated code passed functional checks.
The baseline has no independent Agent review, so external quality scoring
must remain separate from SAT delivery status. A task-defined TeamPlan is a
future, separately frozen arm, not an implied result of this comparison.

The four run IDs are `issue257-v3-task-manager-single-agent`,
`issue257-v3-task-manager-function-specialized`,
`issue257-v3-text-stats-single-agent`, and
`issue257-v3-text-stats-function-specialized`. Their output Git commits are,
respectively, `033c02ca2c52c376894c026ff6351f0aaf7e8603`,
`0ac93da47c41d195838adc54f4cb87fdcbc2683d`,
`312ad932516ea386187a8743a9902b800ae95561`, and
`b7f57edb03ae1df062d3ac2811242d91b06c4658`. Raw run traces and clean
Git output snapshots are retained by the evaluator outside this public
source repository; they were screened for credentials and checked by file
hash and Git blob ID before the summary was written.

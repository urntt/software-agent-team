# Documentation Index

Start with the repository [`README.md`](../README.md) for the product overview,
requirements, command map, and repository layout. The documents below separate
decisions, current facts, operating procedures, and development references so
that each fact has one clear owner.

## Choose a Document

| Need | Document |
| --- | --- |
| Understand the product, users, problem, architecture decisions, experiment, scope, or roadmap | [`VISION.md`](../VISION.md) |
| See what is implemented, what live evidence exists, and what remains | [`STATUS.md`](../STATUS.md) |
| Install, configure a provider/model default, export local data, or uninstall | [`installation.md`](installation.md) |
| Understand runtime authority, semantic responses, artifacts, persisted evidence, or safety boundaries | [`runtime-evidence.md`](runtime-evidence.md) |
| Prepare and inspect a qualifying Phase 1 real-model trace | [`phase1-runbook.md`](phase1-runbook.md) |
| Set up a development checkout, run checks, update the benchmark, or contribute | [`development.md`](development.md) |
| Read the frozen task-manager product contract | [`benchmarks/task_manager/requirements.md`](../benchmarks/task_manager/requirements.md) |
| Understand ignored OpenClaw role workspace boundaries | [`openclaw/workspaces/README.md`](../openclaw/workspaces/README.md) |

## Document Roles

- `README.md` is the public entry point and command map.
- `VISION.md` owns durable product, architecture, experiment, scope, and
  roadmap decisions.
- `STATUS.md` owns time-sensitive implementation and evidence facts.
- Guides in `docs/` own one operating or development concern each.
- Checked-in code and configuration remain authoritative for executable
  contracts; documentation explains those contracts and links to their owner.
- Benchmark documents may not add requirements beyond the frozen TaskBrief.

When behavior changes, update the owning document in the same change. When a
fact appears as a summary elsewhere, keep the summary short and link back to
the owner rather than creating a second detailed contract.

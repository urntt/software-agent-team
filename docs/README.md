# Documentation Index

Start with the repository [`README.md`](../README.md) for the user-facing
overview, requirements, installation, and first build. The documents below
separate decisions, current facts, operating procedures, and development
references so that each fact has one clear owner.

## Choose a Document

| Need | Document |
| --- | --- |
| Understand the product, users, problem, architecture decisions, experiment, scope, or roadmap | [`VISION.md`](../VISION.md) |
| See what is implemented, what provider-backed evidence exists, and what remains | [`STATUS.md`](../STATUS.md) |
| Maintain or verify acceptance criteria for the guided installation-to-delivery journey | [`product-demo-slice.md`](product-demo-slice.md) |
| Understand or implement Adaptive Planning, task-defined teams, progress, controls, and model routing | [`adaptive-orchestration.md`](adaptive-orchestration.md) |
| Install, configure a provider/model default, export local data, or uninstall | [`installation.md`](installation.md) |
| Prepare, publish, or verify a SAT version and stable/dev channel | [`releases.md`](releases.md) |
| Understand runtime authority, semantic responses, artifacts, persisted evidence, or safety boundaries | [`runtime-evidence.md`](runtime-evidence.md) |
| Prepare and inspect a controlled Phase 1 provider-backed evaluation | [`phase1-runbook.md`](phase1-runbook.md) |
| Set up a development checkout, run checks, update the benchmark, or contribute | [`development.md`](development.md) |
| Understand the first generated-project runtime and verification boundary | [`profiles/python/README.md`](../profiles/python/README.md) |
| Read the frozen task-manager evaluation fixture | [`benchmarks/task_manager/requirements.md`](../benchmarks/task_manager/requirements.md) |
| Understand ignored OpenClaw role workspace boundaries | [`openclaw/workspaces/README.md`](../openclaw/workspaces/README.md) |

## Document Roles

- `README.md` is the user-facing public entry point and quick start.
- `installation.md` is the user and operator reference for the supported
  install, configuration, update, export, and uninstall lifecycle.
- `releases.md` is the maintainer procedure for version changes, release
  candidates, immutable GitHub publication, and post-release verification.
- `VISION.md` serves product and engineering decision-makers by owning durable
  product, architecture, experiment, scope, and roadmap decisions.
- `STATUS.md` serves maintainers and evaluators by owning time-sensitive
  implementation, evidence, gaps, and next-milestone facts.
- `product-demo-slice.md` is a contributor-facing acceptance specification for
  the guided user journey; it does not own implementation status.
- `adaptive-orchestration.md` owns the detailed target interaction, runtime
  contracts, implementation batches, and acceptance criteria; `STATUS.md`
  distinguishes implemented batches from remaining work.
- `phase1-runbook.md` is an evaluation-operator procedure, and
  `development.md` is the contributor reference.
- Other guides in `docs/` own one operating or engineering concern each.
- Checked-in code and configuration remain authoritative for executable
  contracts; documentation explains those contracts and links to their owner.
- Benchmark documents may not add requirements beyond the frozen TaskBrief.

When behavior changes, update the owning document in the same change. When a
fact appears as a summary elsewhere, keep the summary short and link back to
the owner rather than creating a second detailed contract.

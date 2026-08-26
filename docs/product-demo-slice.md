# Guided Product Journey Acceptance Specification

This contributor-facing specification defines the user-visible behavior that
must pass before the guided installation-to-delivery journey is considered
usable. It is not an installation guide: users should start with the repository
[`README.md`](../README.md), while operators should use
[`installation.md`](installation.md).

The specification derives from the product and architecture decisions in
[`VISION.md`](../VISION.md). Current implementation and rehearsal status belong
only to [`STATUS.md`](../STATUS.md).

This document remains the completed baseline acceptance contract. The planned
multi-round Planning dialogue, task-defined Agent teams, configurable per-Agent
progress, live user controls, and model routing extend this journey without
retrospectively changing its completion criteria. Their target contract is
[`adaptive-orchestration.md`](adaptive-orchestration.md).

## User Journey Contract

A new user should be able to:

```text
start the computer
→ run one installation command
→ enter or create a project folder
→ run `sat`
→ follow automatic diagnostics and first-run configuration
→ answer "What would you like to build?"
→ answer bounded clarification questions
→ confirm a concise requirements summary
→ watch attributable stage summaries and progress
→ receive a runnable result, verification status, and next commands
```

The user must not need to understand or manually edit an internal run ID,
TaskBrief JSON, benchmark source path, team manifest, concurrency setting,
timeout policy, repair limit, runs root, or workspaces root during this flow.

## Product and Evaluation Surfaces

The CLI has two distinct surfaces.

### Product Surface

Running `sat` with no subcommand is the primary product entry point. It owns:

- Startup diagnostics;
- First-run provider and model guidance;
- Request collection and bounded clarification;
- Requirements confirmation;
- Internal run preparation;
- Progress rendering;
- Delivery and failure summaries.

This surface uses safe defaults and explains only choices that affect the
user's request, authorization, cost, or output.

### Evaluation Surface

Commands such as `sat prepare-benchmark`, `sat preflight`, `sat run`, artifact
validators, explicit policy paths, and experimental overrides remain available
for contributors and controlled comparisons. They preserve reproducibility and
write-once evidence, but they are not the first-run product journey.

## Installation Experience

The product installation command must:

1. Install SAT into a managed user-local location rather than require the user
   to treat the harness source checkout as a project folder;
2. Create a stable `sat` launcher on the user's command path;
3. Detect supported platform and architecture, unprivileged identity,
   filesystem suitability, required system commands, writable locations,
   Docker daemon access and Linux-container mode, available storage, launcher
   conflicts, and required download connectivity;
4. Install and verify the pinned application toolchain;
5. Build or obtain the pinned sandbox image and prove that its restricted
   runtime container remains alive for tool execution;
6. Run focused offline installation checks without executing the contributor
   test suite on the user's device;
7. Finish with one clear next action: enter a project folder and run `sat`.

The pinned OpenClaw runtime must be installed below the SAT application and
carry an ownership marker. An existing OpenClaw binary, process, Gateway,
profile, config, or credential store is outside the installer's ownership
boundary regardless of its path on `PATH`, version, or readiness. A collision
at SAT's intended private path fails safely instead of adopting or overwriting
unmarked files.

Diagnostics must identify the failed condition and a concrete corrective
action. Environment detection belongs to the installer; it must not be
converted into a long questionnaire for the user.

The installer cannot decide whether an organization permits Docker, model
usage, or generated code. Those authorization policies remain external
prerequisites.

## Startup Diagnostics

Every `sat` launch performs a fast, non-destructive check of the conditions
needed for the next step. The startup report distinguishes:

- Ready conditions;
- Conditions SAT can configure automatically;
- Conditions requiring user action;
- Provider checks that would make an external request or incur cost.

Normal startup must not rebuild the environment or Docker image when the
installed state is already valid. Expensive repair actions require an explicit
choice.

## First-Run Configuration

The first-run wizard asks only for information a normal user can reasonably
provide:

- The provider and model to use;
- A credential through OpenClaw's trusted credential boundary or an explicitly
  inherited environment variable;
- Authorization for an optional minimal provider smoke check;
- Any provider cost or quota warning that requires confirmation.

The wizard offers OpenClaw's trusted setup inside SAT's isolated state, detects
only that private state's configured default model without probing the
provider, and offers to use it. Before request collection, SAT confirms that
the exact model resolves through its configured local catalog and auth route
without generating content. It never imports an existing OpenClaw profile.
The optional authorized smoke check remains the semantic provider request
step. Secrets never enter SAT's control-plane configuration,
repository, generated project, run artifacts, logs, exports, or terminal echo;
OpenClaw stores them only in SAT's private OpenClaw state unless they are
inherited from the trusted caller environment.

Pricing overrides, verification concurrency, role-invocation timeouts, repair
limits, policy files, and team selection remain advanced evaluation options.
They are not first-run questions. The controller uses checked-in safe defaults
unless the user deliberately enters an advanced mode.

## Request, Clarification, and Confirmation

After the environment is ready, SAT asks:

```text
What would you like to build?
```

The user may answer in ordinary language. SAT then:

1. States the installed execution-profile boundary: a small local Python 3.12
   project whose checks run without network access;
2. Asks for semicolon-separated success conditions and optional constraints;
3. Builds acceptance criteria and requirements from those confirmed inputs;
4. Shows a concise structured summary in user language;
5. Requires explicit confirmation before a model-backed build begins.

Each interactive answer must be valid terminal Unicode. An undecodable answer
is rejected at its prompt and collected again; it must never reach TaskBrief
validation or start a run. At the optional-constraints prompt, `none` and `n/a`
mean that no additional constraint was supplied.

The runtime-profile boundary must be stated honestly. A request that requires
another language, platform, hosted dependency, credential, or unsupported
runtime is rejected or narrowed explicitly. It is never converted into an
evaluation fixture.

A pre-confirmed TaskBrief remains an advanced input, not a prerequisite for
the primary greenfield flow.

## Internal Materialization

After confirmation, the controller automatically and atomically:

- Generates a unique internal run ID;
- Persists the confirmed TaskBrief;
- Selects the default team, product policy, and Python quality profile;
- Creates a fresh isolated source baseline and run workspace;
- Verifies that no existing evidence or user file will be overwritten,
  including when a destination appears while the build is running;
- Records the exact model, policy, source, and environment identity.

Write-once evidence and fresh source isolation remain mandatory. The UX change
removes manual preparation; it does not weaken provenance or overwrite
protection.

The user starts SAT in a writable parent directory and chooses a new direct
child directory. Work occurs in an isolated managed workspace. An accepted
clean result is materialized into that child without overwriting existing
content. A failed run preserves diagnostic evidence without presenting a
partial workspace as a successful delivery.

## Progress Experience

The deterministic controller emits user-safe events. The terminal displays:

- The current phase and role;
- Completed phase summaries;
- Elapsed time while waiting for a provider response;
- Verified Git snapshot summaries;
- Quality-gate counts and results;
- Independent review status;
- Revision reasons and iteration count;
- Budget or provider failures in plain language.

The normal product policy permits the initial implementation and at most two
evidence-driven revisions. A third implementation iteration is available only
inside the existing controller loop; it does not add a user prompt or retry a
run in place, and non-convergence rules still stop repeated blockers or a
revision with no relevant change. The advanced frozen evaluation path keeps its
separate two-iteration comparison limit.

For example:

```text
✓ Requirements confirmed
✓ Implementation plan created
● Developer is implementing... 02:14 elapsed
✓ Git snapshot verified: 8 files changed
● Running quality gates... 3/4 passed
● Independent review in progress...
↻ Revision requested: deletion needs an explicit confirmation step
✓ Revision verified
✓ Project completed
```

The display never exposes hidden chain-of-thought, secrets, raw provider
credentials, or unbounded model output. It reports attributable artifacts and
controller-verified state. It must not invent a percentage when no meaningful
total is known.

## Delivery Experience

A completed run ends with one concise delivery view containing:

- Completed or failed status;
- The project directory;
- A plain-language summary of what was built;
- Exact install, start, and test commands;
- Acceptance and independent-review results;
- Known limitations or unresolved blockers;
- Elapsed time and cost summary when available;
- Paths to the human-readable report and advanced evidence.

Every accepted project must contain `sat-project.json`. This controller-checked
contract stores setup, project-specific start, and test commands as argv
arrays, not shell text. SAT validates it before delivery and renders those
exact commands instead of assuming a Web framework or fixed entry point.

The result folder is the primary delivery. The evidence tree supports trust and
diagnosis but is not the first thing a user must inspect.

## Failure and Interruption

Failures remain honest product outcomes. SAT explains:

- Which phase failed;
- Whether any provider charge may have occurred;
- Whether a safe result exists;
- Where preserved evidence can be found;
- The next supported action.

SAT never retries a failed run in place, overwrites immutable evidence, or
claims that an interrupted external action succeeded. When automatic resume is
unavailable, the CLI must clearly distinguish a new run from recovery.
Before returning control to the terminal, SAT removes the exact run-scoped
OpenClaw role containers, including any child process still running inside
them. Cleanup is required for success and also runs after workflow failure or
interruption; it must not target another OpenClaw installation.

## Acceptance Criteria

The Product Demo Slice is complete only when all of the following are true:

1. On a fresh supported Linux/WSL user environment with documented OS-level
   prerequisites, one command installs SAT and ends with `sat` as the only next
   command.
2. A pre-existing configured or running OpenClaw remains byte-for-byte outside
   SAT's install, configuration, execution, and uninstall targets.
3. Installation automatically diagnoses the supported environment conditions
   and produces actionable failures.
4. From a new project directory, `sat` completes startup diagnostics,
   confirms the exact model's local catalog/auth route, and completes first-run
   configuration without requiring another SAT subcommand.
5. The normal user is not asked for prices, concurrency, timeouts, repair
   limits, team IDs, policy paths, run IDs, or JSON files.
6. SAT asks what to build, performs bounded clarification, shows a requirements
   summary, and requires confirmation before model-backed work.
7. SAT generates every internal identifier, TaskBrief, source baseline, run
   path, and workspace automatically without weakening write-once evidence.
8. The terminal shows controller-backed phase changes, summaries, elapsed
   waiting time, gate progress, review, and revision information throughout the
   build.
9. Success returns a clean runnable project, exact start/test commands, quality
   results, limitations, and report paths.
10. A terminal workflow outcome leaves no live or stopped OpenClaw role
    container belonging to that SAT run and does not stop or remove a container
    outside SAT-owned state and workspace paths.
11. Failure returns an honest terminal result and preserved evidence without
   presenting a partial project as accepted.
12. The complete journey is covered by offline interaction tests and rehearsed
    from a fresh supported environment with one explicitly authorized provider
    run.

Implementation progress and the next rehearsal step are maintained in
[`STATUS.md`](../STATUS.md); the development sequence and exit criteria are
maintained in [`VISION.md`](../VISION.md#development-route).

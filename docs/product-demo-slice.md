# Product Demo Slice

**Status:** Implemented and offline tested; fresh-device provider rehearsal pending

This document defines the user-visible acceptance contract that must be
implemented before the harness is presented as a usable product. It derives
from the product and architecture decisions in [`VISION.md`](../VISION.md),
which remains authoritative when either document changes.

The Phase 1 controller, Agent workflow, sandbox, quality gates, and evidence
model are the engine beneath this experience. A provider-backed evaluation
trial proves that engine can run; it is not the product interaction that a new
user should be asked to perform.

## Demo Contract

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

The phrase "live trace" may describe historical provider-backed evaluation
evidence in contributor records. It is not a user-facing action or concept.

## Current Implementation Boundary

The primary flow now exists behind `sat` with no subcommand. The managed
bootstrap, startup diagnostics, first-run model setup, optional provider smoke
check, request capture, explicit execution-profile confirmation, automatic run
materialization, progress renderer, terminal result, and non-overwriting
delivery all have offline behavior coverage.

The installed OpenClaw boundary is also product-owned and isolated. SAT uses a
marked private binary plus a private config/state/credential root. It does not
probe, reuse, reconfigure, stop, upgrade, downgrade, or uninstall an OpenClaw
runtime or Gateway that was already present on the device.

The currently implemented clarification boundary is deterministic and bounded.
SAT explains that the installed execution profile supports a small greenfield
Python 3.12 project, collects the user's ordinary-language request, explicit
success conditions, and optional constraints, then constructs a confirmed
TaskBrief from those inputs. It does not use a task-specific template or ask a
model to invent missing intent before authorization. Declining the execution
profile or final summary ends without a model call.

The product goal is not a particular generated application. The checked-in
task-manager materials are a controlled evaluation fixture on the advanced
surface only. They never define, narrow, or replace a request made through bare
`sat`.

One acceptance gate remains open: the complete flow must be rehearsed from a
fresh supported Linux/WSL user account with an explicitly authorized provider,
and the delivered start and test commands must be run there. Offline tests do
not substitute for that evidence.

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
5. Build or obtain the pinned sandbox image;
6. Run offline configuration and behavior checks;
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
provider, and offers to use it. It never imports an existing OpenClaw profile.
The optional authorized smoke check is the current semantic provider/auth
validation step. Secrets never enter SAT's control-plane configuration,
repository, generated project, run artifacts, logs, exports, or terminal echo;
OpenClaw stores them only in SAT's private OpenClaw state unless they are
inherited from the trusted caller environment.

Pricing overrides, verification concurrency, role-stage timeouts, repair
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

The user starts SAT in an empty destination directory or chooses a new child
directory. Work occurs in an isolated managed workspace. An accepted clean
result is materialized into the destination without overwriting existing
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
claims that an interrupted external action succeeded. Automatic resume may be
implemented later; until then the CLI clearly distinguishes a new run from
recovery.

## Acceptance Criteria

The Product Demo Slice is complete only when all of the following are true:

1. On a fresh supported Linux/WSL user environment with documented OS-level
   prerequisites, one command installs SAT and ends with `sat` as the only next
   command.
2. A pre-existing configured or running OpenClaw remains byte-for-byte outside
   SAT's install, configuration, execution, and uninstall targets.
3. Installation automatically diagnoses the supported environment conditions
   and produces actionable failures.
4. From a new project directory, `sat` completes startup diagnostics and the
   first-run configuration without requiring another SAT subcommand.
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
10. Failure returns an honest terminal result and preserved evidence without
   presenting a partial project as accepted.
11. The complete journey is covered by offline interaction tests and rehearsed
    from a fresh supported environment with one explicitly authorized provider
    run.

## Implementation Order

1. Separate the product entry point from the evaluation subcommands;
2. Add explicit installer and startup environment diagnostics;
3. Add guided provider configuration inside an isolated OpenClaw-owned SAT
   state boundary without putting secrets in control-plane state or evidence;
4. Add request collection, bounded clarification, and confirmation;
5. Add automatic internal materialization and safe delivery destinations;
6. Emit controller events and implement the progress renderer;
7. Add the final delivery view and failure guidance;
8. Add interaction tests, installation tests, documentation, and a fresh-device
   rehearsal.

Steps 1 through 8 are implemented and offline tested. The remaining Phase 2
work is the fresh-device provider-backed rehearsal and any root-cause fixes it
exposes. Topology comparison resumes after that acceptance gate. The comparison
engine and its historical evidence remain valid inputs to that later work.

# Software Agent Team

Software Agent Team (`sat`) is an experimental, local-first command-line tool
that turns a short software request into a runnable project. It clarifies what
you want, coordinates a team of AI Agents, checks their work in isolated
containers, and delivers the accepted result with exact setup, start, and test
commands.

SAT currently creates new, small Python 3.12 projects, including Web
applications, CLI tools, and local automation. Each build is delivered into a
new project directory; SAT does not overwrite an existing project, push code,
or deploy it.

## Requirements

Before installing SAT, you need:

- Linux, or Windows with WSL;
- Git, Bash, and curl;
- Docker running Linux containers and available to your normal user account;
- Network access and credentials for a supported model provider; and
- Permission to send your request and relevant generated-project context to
  that provider.

SAT checks the local technical requirements during installation and every time
it starts. It installs and uses its own private OpenClaw runtime and provider
state. Any other OpenClaw installation, configuration, credentials, session,
or running Gateway on the device remains untouched.

See the [installation guide](docs/installation.md#external-prerequisites) for
the complete prerequisite and isolation boundaries.

## Install

Run the following command as a normal Linux or WSL user:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/urntt/software-agent-team/main/scripts/bootstrap.sh \
  | bash && exec "${SHELL:-/bin/bash}" -l
```

The installer validates the device, installs SAT's pinned private runtime,
prepares its Python environment and Docker image, proves that the restricted
sandbox container stays runnable and can execute a tool helper, runs offline
checks, and adds `sat` and `sat-uninstall` to the user-local command path. A
normal installation resolves the latest published stable release and binds it
to an exact source revision and artifact digest; it does not install a moving
`main` checkout as stable.

The command is safe to rerun for an owned installation. It stages and verifies
the requested target before activation, preserves the prior version on failure,
and can refresh an outdated update engine without asking the user to delete
configuration or run state.

If no stable release has been published yet, the command stops without
installing `main` and explains that the stable channel is unavailable. A
developer may explicitly choose the separate dev channel as documented in the
[installation guide](docs/installation.md#managed-one-command-installation);
SAT never makes that channel change silently.

## Build a Project

Enter a directory that may receive a new project folder, then run `sat`:

```bash
mkdir -p "$HOME/projects"
cd "$HOME/projects"
sat
```

On first use, SAT guides you through:

1. Local environment diagnostics and a fresh foreground version/update check;
2. Isolated model-provider configuration;
3. A plain-language description of what you want to build;
4. Confirmation of the installed execution profile and a new project-directory
   name;
5. Explicit task-wide USD/deadline authorization and a persisted task-admission
   self-check before model-backed Planning;
6. A bounded conversation containing only questions that can materially change
   the result. When a short request leaves them material and unknown, SAT asks
   about target users, the primary workflow, or whether the delivery is a
   throwaway prototype, reusable local product, or releasable small product;
   each focused question shows its exact decision scope and whether it was
   selected by the Planner or required by Controller validation. Suggested
   answers carry exact values for only that scope. Model-authored wording and
   rationale remain available as advisory details, but cannot become a system
   prerequisite or widen the answer's authority. If a proposal claims that a
   user-owned decision came from a question that was never asked, the Controller
   returns that exact decision to dialogue instead of repeatedly asking the model
   to repair user authority;
7. One overview that begins with the approved audience, killer workflow,
   delivery maturity, usability/operational/delivery expectations, non-goals,
   and their architecture, team, cost, and delivery effects, followed by
   requirements, explicit assumptions,
   requirement-to-evidence traceability, acceptance criteria, a compact count
   of fixed controller-owned details separated from additional task constraints,
   Agent work assignments with their controller-derived write or read-only
   authority, proposed Agents, task-specific roles, versioned specializations,
   dependencies, permissions, typed outputs, acceptance authority, the exact
   non-overlapping criterion scope assigned to each Reviewer, explicit Review
   entry obligations for absolute guarantees,
   resolved model profiles and fallback authority, time authority and liveness
   policy, concurrency, iterations, and budgets;
8. An option to show or hide the complete fixed policy, execution-profile
   constraints, and Review boundary definitions without changing the proposal
   or calling a model; and
9. Approval, an explicit natural-language revision request, a supported safe
   edit, or cancellation before any execution Agent is created.

After approval, SAT first persists a second self-check covering every approved
route, Agent specialization/capability combination, packaged specialization
prompt, permission, typed output, runtime, sandbox, workspace, and delivery
boundary. It creates only the task-defined Agents in that exact plan after the
required checks pass. Security and end-user-experience assessment use distinct
typed contracts while remaining read-only; a task-specific label cannot grant
either authority or expand tools. The Controller requires security authority
for an all-boundary safety criterion and experience authority when one criterion
is explicitly linked to both the primary workflow and usability expectations;
an invalid generic assignment returns to targeted Planning correction before
approval. The typed Agent graph—not a second prose description—determines the
team shown for approval. The resolved `ModelRoutePlan` likewise determines the
route count, mode, switch conditions, and per-Agent assignments instead of
Planner prose. Every Review task must remain inside its assigned criterion
scope. SAT sends an inconsistency back to the Planner automatically; `r` is
reserved for a user who actually wants to change the request.

The controller derives actual launch order from the approved dependency graph,
enforces concurrency and shared-workspace safety, monitors provider activity and
any user-authorized whole-run deadline, records verified Git snapshots and
quality evidence, and owns revision and termination decisions. On success, SAT reports the delivered
directory and exact setup, run, and test commands. Every new terminal report
also records the exact SAT release and source identity that controlled the run.
On failure, SAT preserves an auditable report instead of presenting unfinished
work as successful.

While execution is active, the same terminal accepts optional slash commands:

```text
/guide <agent|future|phase:name> <instruction>
/correct <replacement requirement>
/pause
/resume
/interrupt <active-agent-id>
/cancel confirm
/visibility <compact|standard|detailed>
/controls
/help
```

Guidance applies only to a future invocation. Correction stops at a safe
checkpoint, preserves the superseded run, and opens a new Planning overview for
approval. Pause is cooperative; interrupt and cancel are best effort for an
active provider call, so already-incurred usage may remain billable. Cancel is
terminal and never delivers partial work.

## Configure a Model

The first `sat` launch includes guided setup. Later launches recheck the exact
saved profile and continue without repeating setup when it is ready. You can
explicitly repeat setup or inspect SAT's non-secret settings with:

```bash
sat configure
sat configure --show
```

The normal wizard stores one selected `provider/model` as a strict default
profile. It can use an OpenClaw-native provider route, while the advanced
`sat configure` interface can declare custom remote or local endpoints with an
explicit transport, including OpenAI Completions, OpenAI Responses, Anthropic
Messages, and Ollama. Every profile is secret-free and carries one immutable
runtime digest. The same profile is used for startup inspection, provider
smoke, Planning, and execution instead of rebuilding provider details in each
path.

Configuration is validated before it is committed. Interactive setup uses a
staged copy of SAT's private provider state; non-interactive setup validates the
same profile contract. A validation failure or cancellation leaves the prior
SAT and private OpenClaw configuration unchanged, and success states explicitly
that the local routes passed while no live provider call was made. Advanced
configuration can also declare Agent capabilities, deterministic stage or
capability routes, and a bounded switch after an attributable provider failure.
Before each task's first model call, SAT refreshes non-secret price and context
facts and asks for one task-wide USD ceiling and an optional whole-run deadline;
no deadline is the default. Use `sat configure --help` for the complete advanced
interface and pragmatic custom-provider examples in the
[installation guide](docs/installation.md#saved-configuration).

Provider credentials remain in SAT's isolated OpenClaw state or in an
explicitly trusted caller environment; they are not written to the repository,
generated project, run evidence, model profiles, or SAT exports.

Before asking for a project, SAT checks that its isolated runtime recognizes
the bootstrap model. Task admission records the full local SAT version and
source provenance. Managed installs check their current channel once in the
foreground; only a newer stable SemVer produces the normal `sat update` prompt,
and an unavailable release endpoint does not block the task. Source/package
launches do not contact the managed updater. Before starting an Agent, the
approved-plan preflight checks every model route authorized by the TeamPlan,
with a local catalog/auth route for each. These checks do not generate content.
An optional provider smoke check
remains a separate, explicitly authorized action because it can incur usage.
SAT announces the local inspection before waiting. A cold model-catalog check
may use up to 90 seconds; that infrastructure boundary is separate from the
30-second ordinary preflight-command limit and from model work. Product Agent
calls have no fixed wall-clock duration. Before provider waiting begins, SAT
observes a finite sequence of attributable OpenClaw initialization checkpoints
in its private state. It captures a content-free baseline immediately before
launch, so a session directory, index, binding, transcript header, or matching
turn inherited from an earlier invocation is not current progress; reusing the
same prompt requires a newly observed turn occurrence. During that pre-readiness
interval, identity-bound CPU, fault, I/O, or complete process-topology changes
renew only the initialization inactivity lease; they never create a readiness
checkpoint or start provider waiting. Ninety seconds without either attributable
process activity or checkpoint progress opens a visible final 15-second diagnostic
window; continued inactivity stops only that invocation, while later activity
recovers the same invocation. An unavailable
or malformed initialization observer fails closed instead of leaving an
unobservable process running. A session index or transcript that is exactly
missing at open time is instead treated as “not published yet” and remains at
the preceding checkpoint; SAT does not combine that result with a later path
lookup that can race an atomic OpenClaw publish.

Once the current turn or its private provider stream is attributable, OpenClaw
retains its provider transport boundary and SAT watches content-free stream and
tool-lifecycle signals. Tool activity is shown only as a Controller-classified
action and target such as `testing quality checks (pytest)`; command arguments,
tool output, and unknown executable names are never used as progress text.
Repeated events remain in the audit journal, while unchanged checkpoint and
budget blocks are not printed over and over. Trusted activity renews the provider
lease regardless of total work time. Sustained silence first produces a visible
warning and grace period. An attributable final assistant record transfers the invocation to a
separate `finalizing_response` phase, so OpenClaw result serialization and exit
cannot be mistaken for provider silence. Observable process output renews that
60-second no-progress guard; a final 10-second diagnostic window precedes a
typed finalization stall. Any stop then remains visibly `stopping` and
`collecting_evidence` until the exact process outcome, output, evidence, and
cleanup are known; only then is it `stopped` and terminal. SAT applies a
whole-run deadline only when the user explicitly authorized one for that task.
If catalog inspection expires, SAT reports that no provider request was made
and does not create an Agent.

If an upstream tool loop exits immediately after a completed tool result and
before the required typed submission, SAT records that distinct incomplete
state instead of reporting a normal semantic failure. A write-capable Agent may
continue the same task and session only when repository identity, ancestry,
approved path scope, and content-sensitive workspace progress all verify, the
task budget and optional deadline still permit another call, and the user has
not stopped it. An unchanged repeated state stops; partial work is never
accepted, committed by the Controller, or allowed to bypass gates and Review.

Every live OpenClaw subprocess launched by SAT also receives a private durable
ownership lease. Startup distinguishes invocations owned by another live SAT
process from an exact orphan left by a crashed process and from a stale PID;
the identity includes Linux process start time, and recovery pins it with a
Linux pidfd, so PID reuse cannot authorize termination. Inspect locally with
`sat cleanup`. If startup reports a proven
orphan, `sat cleanup --orphans` asks before stopping only that process group and
removing only its exact SAT-owned sandbox session.

See the [installation and configuration guide](docs/installation.md) for
configuration paths, provider setup, saved defaults, and recovery boundaries.

## Update or Uninstall

Inspect the installed release and exact source identity without making a
network request:

```bash
sat --version
sat version
```

Use `sat version --json` when a script needs the full identity report. A
managed installation can check or apply its current channel target directly:

```bash
sat update --check
sat update
sat channel status
```

Developers can explicitly switch a managed installation to or from the moving
development channel:

```bash
sat channel switch dev
sat channel switch dev --ref <branch-tag-or-full-commit>
sat channel switch stable
```

The explicit dev ref can also retarget an installation that is already on the
dev channel; SAT shows the resolved revision and obtains confirmation before
activation.

Update and channel-switch commands show the exact current and target identities
before activation. They stage and verify the new application, check persisted
schema compatibility, refuse to change an active run, atomically switch the
logical application link, and retain the previous release for rollback. A
source checkout is never rewritten by the managed updater.

SAT verifies that it owns the installation and that the tracked application is
clean before updating it.

Run the guided uninstaller from any directory:

```bash
sat-uninstall
```

Uninstallation preserves configuration, Planning evidence, generated work, and
SAT's isolated provider state by default. It can export configuration and
generated data before removal, and it requires explicit choices before purging
preserved state. Other OpenClaw installations are never uninstall targets.

See [guided uninstallation](docs/installation.md#guided-uninstallation) for
the export and purge options.

## Current Scope

SAT is experimental software. Its current product profile builds new, small
Python 3.12 projects, including Web applications, CLI tools, and local
automation. It does not yet modify existing codebases, provide additional
generated-project runtimes, resume a user-interrupted run automatically,
deploy, or publish a generated project.

Read [`STATUS.md`](STATUS.md) for current evidence and known gaps, and
[`VISION.md`](VISION.md) for product direction, architecture, scope, and the
roadmap.

## Documentation

- [Installation, configuration, updating, and uninstallation](docs/installation.md)
- [Current implementation status and evidence](STATUS.md)
- [Product direction and architecture](VISION.md)
- [Complete documentation index](docs/README.md)
- [Contributor setup and checks](docs/development.md)
- [Controlled evaluation runbook](docs/phase1-runbook.md)

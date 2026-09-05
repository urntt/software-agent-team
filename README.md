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
   the result;
7. One overview of requirements, acceptance criteria, controller-owned
   execution-profile constraints separated from additional task constraints,
   Agent work assignments with their controller-derived write or read-only
   authority, proposed Agents, dependencies, permissions, explicit Review entry
   obligations for absolute guarantees and the exact meaning of each boundary,
   resolved model profiles and fallback authority, time authority and liveness
   policy, concurrency, iterations, and budgets; and
8. Approval, a natural-language revision request, a supported safe edit, or
   cancellation before any execution Agent is created.

After approval, SAT first persists a second self-check covering every approved
route, Agent capability, permission, runtime, sandbox, workspace, and delivery
boundary. It creates only the task-defined Agents in that exact plan after the
required checks pass.
The controller derives actual launch order from the approved dependency graph,
enforces concurrency and shared-workspace safety, monitors provider activity and
any user-authorized whole-run deadline, records verified Git snapshots and
quality evidence, and owns revision and termination decisions. On success, SAT reports the delivered
directory and exact setup, run, and test commands. On failure, it preserves an
auditable report instead of presenting unfinished work as successful.

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

The first `sat` launch includes guided setup. You can repeat setup or inspect
SAT's non-secret settings later:

```bash
sat configure
sat configure --show
```

The normal wizard stores one selected `provider/model` as a strict default
profile. Advanced configuration can add secret-free model profiles, declare
the Agent capabilities each profile may serve, choose deterministic stage or
capability routes, and explicitly authorize a bounded switch after an
attributable provider failure. It also stores non-secret price/context metadata
with its source, plus adaptive maximum concurrency and
compact/standard/detailed progress visibility. Before each task's first model
call, SAT refreshes those model facts and asks for one task-wide USD ceiling
and an optional whole-run deadline; no deadline is the default. Use
`sat configure --help` for the complete advanced interface.

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
calls have no fixed wall-clock duration. OpenClaw retains its provider transport
boundary, and SAT independently watches a private content-free signal for stream
events plus attributable tool lifecycle. Trusted activity renews the lease
regardless of total work time. Sustained silence first produces a visible warning
and grace period, then stops only that invocation and preserves its evidence. SAT
applies a whole-run deadline only when the user explicitly authorized one for
that task. If catalog inspection expires, SAT reports that no provider request
was made and does not create an Agent.

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
sat channel switch stable
```

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
generated-project runtimes, resume an interrupted process automatically,
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

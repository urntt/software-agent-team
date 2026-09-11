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
5. One task-wide USD ceiling and an optional whole-run deadline, followed by a
   pre-model self-check;
6. A bounded conversation about only the product decisions that remain material,
   then an overview of the proposed requirements, implementation, task-specific
   Agent team, evidence, model routes, cost, and delivery boundaries; and
7. Approval, a natural-language revision, a supported safe edit, or cancellation
   before execution begins.

After approval, SAT verifies the complete plan before creating its task-defined
Agents. The Controller derives launch order, permissions, independent Review,
model routes, revision, and termination from that approved plan. Inconsistencies
return to targeted Planning correction; `r` is reserved for a user-requested
change. On success, SAT reports the delivered directory and exact setup, run,
and test commands. On failure, it preserves an auditable report instead of
presenting unfinished work as successful.

The complete Planning, team, Review-scope, and execution contract is documented
in the [adaptive orchestration specification](docs/adaptive-orchestration.md).

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

Before model work, SAT checks its isolated runtime and every approved route
without generating content. An optional provider smoke check is separate because
it can incur usage. Product Agent calls have no fixed wall-clock duration: SAT
distinguishes startup, active provider work, finalization, user-authorized
deadlines, and cleanup, and it keeps partial work unaccepted. Use `sat cleanup`
to inspect SAT-owned runtime state and `sat cleanup --orphans` to review a proven
orphan before removal.

See the [runtime and evidence reference](docs/runtime-evidence.md) for lifecycle,
attribution, continuation, process-ownership, and fail-closed details. See the
[installation guide](docs/installation.md) for configuration paths, provider
setup, saved defaults, and recovery commands.

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

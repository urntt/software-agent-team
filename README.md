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
checks, and adds `sat` and `sat-uninstall` to the user-local command path.

## Build a Project

Enter a directory that may receive a new project folder, then run `sat`:

```bash
mkdir -p "$HOME/projects"
cd "$HOME/projects"
sat
```

On first use, SAT guides you through:

1. Local environment diagnostics;
2. Isolated model-provider configuration;
3. A plain-language description of what you want to build;
4. Confirmation of the installed execution profile and a new project-directory
   name;
5. Explicit authorization for model-backed Planning;
6. A bounded conversation containing only questions that can materially change
   the result;
7. One overview of requirements, acceptance criteria, implementation tasks,
   proposed Agents, dependencies, permissions, model, timeouts, concurrency,
   iterations, and budgets; and
8. Approval, a natural-language revision request, a supported safe edit, or
   cancellation before any execution Agent is created.

After approval, SAT creates only the task-defined Agents in that exact plan.
The controller derives actual launch order from the approved dependency graph,
enforces concurrency and shared-workspace safety, resolves and enforces each
Agent timeout, records verified Git snapshots and quality evidence, and owns
revision and termination decisions. On success, SAT reports the delivered
directory and exact setup, run, and test commands. On failure, it preserves an
auditable report instead of presenting unfinished work as successful.

## Configure a Model

The first `sat` launch includes guided setup. You can repeat setup or inspect
SAT's non-secret settings later:

```bash
sat configure
sat configure --show
```

The normal wizard stores only the selected `provider/model` reference. Advanced
configuration may also store secret-free prices, the adaptive maximum
concurrency, compact/standard/detailed progress visibility, or an explicit
global invocation-timeout override. Provider
credentials remain in SAT's isolated OpenClaw state or in an explicitly
trusted caller environment; they are not written to the repository, generated
project, run evidence, or SAT exports.

Before asking for a project or starting an Agent, SAT checks that its isolated
runtime recognizes the exact selected model and has a local catalog/auth route
for it. This check does not generate content. An optional provider smoke check
remains a separate, explicitly authorized action because it can incur usage.

See the [installation and configuration guide](docs/installation.md) for
configuration paths, provider setup, saved defaults, and recovery boundaries.

## Update or Uninstall

Rerun the installation command to update a managed installation. SAT verifies
that it owns the installation and that the tracked application is clean before
updating it.

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

## Current Scope and Maturity

SAT is experimental software. The supported build path is currently limited
to new, small Python 3.12 projects; existing-codebase modification, additional
runtime profiles, interrupted-run resume, deployment, and publication are not
yet supported. Adaptive Planning, plan approval, task-defined execution, and
delivery are integrated in the normal `sat` path and covered offline. The same
append-only event stream now drives configurable per-Agent progress, including
dependency, provider-wait, repair, terminal, route, duration, and budget facts.
Active run controls and multiple model routes remain under development.

The earlier guided fixed-team path passed a complete live-provider rehearsal
in a fresh non-root Linux account, including accepted delivery, exact project
commands, application checks, and zero run-scoped container residue. That
evidence does not prove the newly activated adaptive path; a fresh installed
adaptive rehearsal remains required before claiming demonstration readiness.

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

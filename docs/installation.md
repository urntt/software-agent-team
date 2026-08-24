# Installation, Configuration, and Uninstallation

This guide owns the supported Linux/WSL installation lifecycle, startup
diagnostics, first-run model configuration, user-local state, export behavior,
and removal boundaries. The full user-facing acceptance contract is
[`product-demo-slice.md`](product-demo-slice.md). Contributors running a
controlled provider-backed evaluation should also read the
[`Phase 1 evaluation runbook`](phase1-runbook.md).

## External Prerequisites

SAT diagnoses the supported device conditions during installation and startup,
but it cannot grant organizational authorization. Before installation, the
user or organization must decide that the following are permitted:

- Linux, or Windows through WSL;
- Docker running Linux containers;
- Model-provider use and any associated data transfer or cost;
- Generated-code execution inside SAT's restricted containers.

The installer does not install or start an OS-level Docker daemon. Install
Docker first and make it available to the unprivileged Linux/WSL user. The
installer and every `sat` launch then check that condition directly.

SAT pins Python 3.12, OpenClaw 2026.7.1-2, OpenClaw's local Node.js 24.15.0
runtime, Python dependencies through `uv.lock`, and the generated-code sandbox
image through `configs/product-policy.json`. The controlled evaluation policy
uses the same generic Python image but owns separate task-specific environment
settings.

## Managed One-Command Installation

Run this command as a normal Linux/WSL user:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/urntt/software-agent-team/main/scripts/bootstrap.sh \
  | bash && exec "${SHELL:-/bin/bash}" -l
```

The final login-shell step activates the standard user-local command path on a
new device. The only next product command is:

```bash
sat
```

The bootstrap:

1. Checks Linux/WSL, unprivileged identity, Git, curl, and safe target paths;
2. Clones or updates the public repository in
   `${XDG_DATA_HOME:-$HOME/.local/share}/software-agent-team/app`;
3. Marks that exact directory as a SAT-owned managed application;
4. Refuses an existing unowned directory or modified tracked application;
5. Runs the installation from that managed directory.

The installation then:

- Checks architecture, required commands, Docker access, and Linux-container
  mode;
- Installs the pinned uv, Python, and OpenClaw toolchain when needed;
- Synchronizes the locked SAT environment;
- Builds and resolves the pinned sandbox image;
- Runs configuration validation, formatting, lint, and the full offline test
  suite;
- Creates `$HOME/.local/bin/sat` and `$HOME/.local/bin/sat-uninstall` without
  overwriting unrelated commands;
- Reports the exact application, launcher, runtime, and image identities.

The process is idempotent for the same owned installation. Advanced operators
may override `SAT_INSTALL_ROOT`, `SAT_REPOSITORY_URL`, `SAT_INSTALL_REF`,
`SAT_BIN_DIR`, `UV_BIN`, or `OPENCLAW_PREFIX`, but those are not normal
first-use questions.

## Contributor Checkout Installation

Contributors may keep the application bound to a development checkout:

```bash
./scripts/install.sh
```

This path performs the same locked setup, image build, validation, and launcher
checks, but it does not create the managed-install marker. The uninstaller
therefore removes the launchers and checkout environment while preserving the
development checkout itself.

## First Launch

Run `sat` in a directory that may receive one new project child:

```bash
mkdir -p "$HOME/projects"
cd "$HOME/projects"
sat
```

SAT first performs a fast, non-provider startup inspection covering:

- Linux/WSL and supported x86-64 or ARM64 architecture;
- Non-root UID and GID;
- A writable real project-parent directory;
- Git and Docker commands;
- The pinned OpenClaw runtime;
- Docker daemon access and the pinned sandbox image;
- Available storage;
- `sat` launcher visibility.

Every failed condition includes a corrective action. SAT does not rebuild the
toolchain or image during normal startup; a missing installed component points
back to the installer.

On the first configured run, SAT then:

1. Offers to open OpenClaw's trusted provider-credential setup;
2. Reads OpenClaw's local default model without probing the provider and asks
   the user to confirm or replace the exact `provider/model` reference;
3. Saves only that model reference in SAT configuration;
4. Offers one explicit minimal provider smoke check, disabled by default;
5. Asks what the user wants to build;
6. States the current small-project Python 3.12 execution profile and asks the
   user to confirm that runtime boundary;
7. Collects explicit success conditions and optional constraints;
8. Asks for one new direct child project directory;
9. Generates and shows the request, acceptance, destination, and verification
   summary;
10. Requires explicit confirmation before any build Agent call.

Declining either the profile or build confirmation exits without starting a
build. The product path constructs its TaskBrief from the confirmed user input
and never substitutes the task-manager evaluation fixture.

## Saved Configuration

SAT configuration is stored atomically with mode `0600` at:

```text
${XDG_CONFIG_HOME:-$HOME/.config}/software-agent-team/config.json
```

Schema version 3 stores:

- The exact OpenClaw `provider/model` reference;
- Optional paired input/output prices for controlled evaluation;
- Optional advanced Tester/Reviewer concurrency;
- An optional advanced global role-stage timeout override.

The normal first-run wizard stores only the model and uses checked-in runtime
defaults. It never accepts or persists an API key. Provider credentials stay in
OpenClaw's credential store or a trusted caller environment and are excluded
from SAT exports and evidence.

Reconfigure or inspect the secret-free values with:

```bash
sat configure
sat configure --show
```

Scripted product setup needs only a model:

```bash
sat configure --non-interactive --model provider/model
```

Controlled evaluations may additionally freeze real prices and runtime
overrides:

```bash
sat configure --non-interactive \
  --model provider/model \
  --input-cost-per-million-usd 0.50 \
  --output-cost-per-million-usd 1.50 \
  --verification-concurrency 1 \
  --use-role-timeouts
```

Input and output prices must be supplied together. When the product wizard has
no trustworthy price, SAT records estimated cost as unavailable rather than
inventing `$0.00`. Provider-side quotas or spending limits remain necessary
because usage arrives only after a model call. The advanced `sat run` command
still requires explicit prices for comparable evaluation evidence.

Set an absolute `SAT_CONFIG_PATH` only when the configuration location must be
overridden. Keep the same value set for later `sat` and `sat-uninstall`
commands.

## Product State and Delivery

Internal product data lives beneath:

```text
${XDG_STATE_HOME:-$HOME/.local/state}/software-agent-team/
```

Its separate `runs/`, `workspaces/`, and `sources/` directories contain
write-once evidence, isolated Agent clones, and trusted seed repositories. Use
an absolute `SAT_STATE_ROOT` only for a deliberate state-location override.
SAT creates an exact ownership marker and refuses to adopt a non-empty unowned
directory, so a mistaken override cannot make an arbitrary directory eligible
for export or purge.

SAT generates every run ID and internal path after confirmation. The model
works only in the isolated workspace. A completed, accepted workspace is copied
through a same-parent staging directory and published with Linux no-replace
semantics into the new project child chosen by the user. SAT refuses a
destination that exists initially or appears during the build, and never
presents a failed workspace as a successful delivery. A filesystem that cannot
provide atomic no-replace publication fails safely instead of falling back to
an overwrite-prone operation.

The terminal result reports status, destination, summary, acceptance count,
elapsed time, report path, limitations or unresolved findings, and exact
project setup, start, and test commands. Those commands come from the accepted
project's validated `sat-project.json`; SAT does not assume a task domain, Web
framework, or fixed application entry point.

## Timeout Policy

Without an advanced global override, `configs/product-policy.json` supplies
role-specific product-stage budgets. `configs/run-policy.json` mirrors the
role defaults for controlled evaluation:

| Roles | Stage budget |
| --- | ---: |
| Clarifier and Planner | 120 seconds |
| Single Agent, Developers, and Integrator | 900 seconds |
| Tester and Reviewer | 300 seconds |

One stage budget covers the initial response and its optional semantic repair
together; repair does not restart the clock. The resolved values are frozen in
`run.json`.

Use `--stage-timeout-seconds N` only when an evaluation deliberately gives
every role the same budget. Use `--use-role-timeouts` to clear a saved override
or ignore it for one advanced run. The deprecated
`--agent-timeout-seconds` spelling retains the same shared-stage semantics only
until the next major release.

## Updating an Installation

Rerun the managed installation command. The bootstrap verifies ownership and a
clean tracked application before fetching the selected ref, then reconciles the
locked environment, image, launchers, and offline checks. User configuration
and state live outside the application directory and remain unchanged.

For a contributor checkout, update it through the contributor's normal Git
workflow and rerun `./scripts/install.sh`.

## Guided Uninstallation

Run from any directory:

```bash
sat-uninstall
```

The default removes SAT launchers and its Python environment. It also removes
the exact marked managed application directory, or preserves a development
checkout. By default it preserves:

- SAT configuration;
- Generated runs, workspaces, and trusted sources;
- OpenClaw and provider credentials;
- uv and its managed Python installation;
- Docker and the sandbox image.

Export configuration and generated state first with:

```bash
sat-uninstall --export-to "$HOME/sat-backup" --yes
```

The new absolute destination must not already exist and must be outside both
the application and SAT state. The export can contain
`configuration/config.json`, `data/runs/`, `data/workspaces/`,
`data/sources/`, and `EXPORT.txt`. Provider credentials remain excluded.

Deletion requires explicit purge flags and may follow the same export:

```bash
sat-uninstall \
  --export-to "$HOME/sat-backup" \
  --purge-config \
  --purge-data \
  --yes
```

Without a terminal, `--yes` is required. Use `sat-uninstall --help` to review
all keep, purge, export, and confirmation options. `make uninstall` invokes the
same script from a contributor checkout.

## Ownership and Recovery Boundaries

Uninstallation never removes provider credentials, shared OpenClaw, uv,
Docker, or the sandbox image. It deletes a managed application only when a
regular marker names that exact resolved directory; it refuses a missing,
symbolic, invalid, or mismatched marker. It also refuses symbolic configuration
or state targets and a missing or mismatched state-ownership marker before
export or purge.

Preservation is the default because removing a CLI must not silently destroy a
generated project or its audit evidence. Inspect a completed export before
selecting either purge option.

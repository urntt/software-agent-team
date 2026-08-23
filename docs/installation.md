# Installation, Configuration, and Uninstallation

This guide owns the supported Linux/WSL installation lifecycle, first-launch
configuration, saved defaults, export behavior, and removal boundaries. For a
qualifying model-backed experiment, continue with the
[`Phase 1 live-trace runbook`](phase1-runbook.md) after setup.

## System Requirements

- Linux, or Windows through WSL;
- Git, Bash, and curl;
- Docker for live Agent sandboxes and generated-code quality gates;
- An unprivileged host account with access to the running Docker daemon;
- Network access only for initial setup and the selected model provider;
- Provider credentials configured through OpenClaw or the trusted caller
  environment, never in this repository.

The checked-in setup pins Python 3.12, OpenClaw 2026.7.1-2, OpenClaw's local
Node.js 24.15.0 runtime, and Python dependencies through `uv.lock`.

The installer does not install an OS-level Docker daemon. Install and start
Docker first, then confirm that the unprivileged account can reach it:

```bash
docker version
docker run --rm hello-world
```

## One-Command Installation

From a clean checkout, run:

```bash
./scripts/install.sh
```

The installer:

- Installs the pinned uv, Python, and OpenClaw toolchain when needed;
- Synchronizes the locked project environment;
- Builds the benchmark image named by `configs/run-policy.json`;
- Runs configuration, formatting, lint, and test checks;
- Creates checkout-bound `$HOME/.local/bin/sat` and
  `$HOME/.local/bin/sat-uninstall` launchers.

It is idempotent for the same checkout and refuses to overwrite unrelated
commands. Use `SAT_BIN_DIR`, `UV_BIN`, or `OPENCLAW_PREFIX` only when the
corresponding user-local location must be changed.

The installer neither creates provider credentials nor activates an OpenClaw
provider configuration. Keep those values outside the checkout.

## First Launch

Start the installed command without arguments:

```bash
sat
```

`sat` reports whether user defaults exist and prints the provider,
benchmark-preparation, preflight, run, reconfiguration, and uninstall path.
Create or replace the defaults interactively with:

```bash
sat configure
```

Inspect the saved, non-secret state at any time:

```bash
sat configure --show
```

## Saved Configuration

The saved values are:

- The exact OpenClaw `provider/model` reference;
- Current input and output prices per million tokens;
- Tester/Reviewer concurrency;
- An optional global role-stage timeout override.

They are stored with mode `0600` in
`${XDG_CONFIG_HOME:-$HOME/.config}/software-agent-team/config.json`.

Set an absolute `SAT_CONFIG_PATH` only when this location must be overridden,
and keep the same override set for later `sat` and `sat-uninstall` invocations.

Provider credentials are deliberately not accepted or stored. Configure them
through OpenClaw's credential store or the trusted caller environment, then
inspect both provider and SAT state:

```bash
$HOME/.openclaw/bin/openclaw configure --section model
$HOME/.openclaw/bin/openclaw models status --check
sat configure --show
```

For scripted setup, supply every required first-time value explicitly:

```bash
sat configure --non-interactive \
  --model provider/model \
  --input-cost-per-million-usd 0.00 \
  --output-cost-per-million-usd 0.00 \
  --verification-concurrency 1 \
  --use-role-timeouts
```

Use real prices for a paid model. A later `sat configure` run replaces the
saved defaults atomically. Run-specific `sat run` flags take precedence without
modifying the saved file.

## Timeout Policy

Without a global override, `configs/run-policy.json` supplies measured
role-specific stage budgets:

| Roles | Stage budget |
| --- | ---: |
| Clarifier and Planner | 120 seconds |
| Single Agent, Developers, and Integrator | 900 seconds |
| Tester and Reviewer | 300 seconds |

One stage budget covers the initial response and its optional repair together;
repair does not restart the clock. The resolved values are frozen in
`run.json`.

Use `--stage-timeout-seconds N` only when an experiment deliberately gives
every role the same stage budget. Use `--use-role-timeouts` to clear a saved
override or ignore it for one run. The old `--agent-timeout-seconds` spelling
is accepted only as a deprecated alias for the same shared-stage semantics and
is scheduled for removal in the next major release.

## Updating an Installation

After updating the checkout, rerun:

```bash
./scripts/install.sh
```

The installer reconciles the locked environment, benchmark image, launchers,
and offline validation against that checkout. It preserves the user
configuration and generated evidence.

## Guided Uninstallation

Run the guided uninstaller from any directory:

```bash
sat-uninstall
```

The default removes the two launchers and this checkout's `.venv` while
preserving:

- The SAT configuration;
- Default `runs/` and `workspaces/` evidence;
- The source checkout;
- OpenClaw and its credentials;
- uv and its managed Python installation;
- Docker and the benchmark image.

Export the SAT configuration and default generated data before uninstalling
with:

```bash
sat-uninstall --export-to "$HOME/sat-backup" --yes
```

The destination must be absolute and must not already exist. The export
contains `configuration/config.json`, available default `data/runs/` and
`data/workspaces/`, and `EXPORT.txt`. It intentionally excludes provider
credentials and any custom `--runs-root` or `--workspaces-root` locations.

Deletion requires explicit purge flags and can be combined with export:

```bash
sat-uninstall \
  --export-to "$HOME/sat-backup" \
  --purge-config \
  --purge-data \
  --yes
```

Use `sat-uninstall --help` to review all keep, purge, export, and confirmation
options. `make uninstall` runs the same guided script from the checkout.

## Ownership and Recovery Boundaries

Uninstallation deliberately does not remove provider credentials, shared
OpenClaw/uv/Docker installations, the benchmark image, the source checkout, or
custom run roots. Export custom data separately before purging or deleting it.

The default preservation policy is intentional: removing the CLI must not
silently destroy generated code or audit evidence. Inspect an export before
selecting either purge option.

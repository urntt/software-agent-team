# Installation, Configuration, and Uninstallation

This user and operator guide owns the supported Linux/WSL installation
lifecycle, startup diagnostics, first-run model configuration, user-local
state, export behavior, and removal boundaries. Start with the repository
[`README.md`](../README.md) for the shortest install-to-first-build path.

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
image through `configs/product-policy.json`.

SAT never adopts an OpenClaw installation that predates SAT. Its pinned binary
and Node.js runtime live under the marked application-private
`.sat/openclaw/` directory. Provider configuration, credentials, sessions, and
caches live under SAT's separately owned user-state root. An OpenClaw binary,
Gateway, config, credential store, profile, or state directory anywhere else
is neither probed nor modified, even when it is compatible and already
configured. SAT runs role calls in OpenClaw local mode, so it does not attach
to, stop, or reconfigure an already running Gateway.

This is a state and ownership guarantee, not a reservation of shared hardware
or provider capacity. Concurrent programs can still compete for CPU, memory,
network bandwidth, Docker capacity, or a provider quota when the user gives
them access to the same external resources.

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
- Installs the pinned uv and Python toolchain plus a marked SAT-private
  OpenClaw runtime when needed;
- Synchronizes the locked SAT environment;
- Builds and resolves the pinned sandbox image, then starts a restricted probe,
  executes the immutable Reviewer probe runner's self-test inside it, verifies
  that the container remains alive, and removes it; the image also contains the
  pinned `uv` used by bounded Reviewer probes of generated-project commands;
- Runs focused offline configuration and CLI installation checks;
- Creates `$HOME/.local/bin/sat` and `$HOME/.local/bin/sat-uninstall` without
  overwriting unrelated commands;
- Reports the exact application, launcher, runtime, and image identities.

The managed path does not run repository formatting, lint, or the complete
developer test suite on the user's device. Those checks remain mandatory for
contributors and CI through `make check`.

The process is idempotent for the same owned installation. Advanced operators
may override `SAT_INSTALL_ROOT`, `SAT_REPOSITORY_URL`, `SAT_INSTALL_REF`,
`SAT_BIN_DIR`, or `UV_BIN`, but those are not normal first-use questions.
The OpenClaw prefix is intentionally not configurable: accepting an arbitrary
prefix would allow installation to mutate a pre-existing OpenClaw runtime.

If Docker, a download, or an offline check interrupts installation, correct the
reported condition and rerun the same managed-install command. The bootstrap
reuses only a marked, clean SAT application, reconciles the pinned runtime and
image, and preserves user configuration and state. It does not require deleting
the partial installation before a retry. A sandbox image that builds but exits
before OpenClaw can execute tools is rejected before SAT reports installation
success or creates a new launcher.

Contributors installing from a source checkout should follow
[`Checkout Installation`](development.md#checkout-installation) instead.

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

After the user confirms a build and before the first Agent call, the run
preflight repeats the restricted container and tool-execution probe against the
exact immutable image ID recorded for that run. It also verifies that the
bootstrap `provider/model` and every route authorized by the approved TeamPlan
resolve through the run-scoped catalog and SAT's isolated auth boundary. These
checks catch a stale container or unresolved primary or fallback route before
spending provider tokens.

SAT prints a status message before each potentially cold local catalog wait.
Model inspection may take up to 90 seconds per selected route; ordinary local
preflight commands retain a 30-second limit. Both values are recorded as
infrastructure-check settings and are independent of the timeout later granted
to any Agent. If model inspection times out, SAT identifies that exact phase,
states that no provider request was made, and stops before creating an Agent.

OpenClaw keeps role sandboxes alive for session reuse by default. SAT's run
sessions are unique and immutable, so SAT removes the exact run-scoped role
containers before returning a completed or failed result, and also attempts
the same cleanup after interruption. Selection requires both SAT's exact
session key and a bind mount inside SAT-owned state or the run workspace; an
existing OpenClaw installation is never a cleanup target.

On the first configured run, SAT then:

1. Explains that SAT uses its own isolated OpenClaw provider state and that any
   existing OpenClaw remains untouched;
2. Offers to open provider-credential setup inside that isolated state;
3. Reads only SAT's isolated default model without probing the provider and
   asks the user to confirm or replace the exact `provider/model` reference;
4. Announces and checks that the exact selection has a local catalog/auth route
   without generating content, allows up to 90 seconds for a cold local check,
   and gives a corrective configuration path when it does not complete;
5. Saves that model as one strict secret-free default profile in SAT
   configuration;
6. Offers one explicit minimal provider smoke check, disabled by default;
7. Asks what the user wants to build;
8. States the current small-project Python 3.12 execution profile and asks the
   user to confirm that runtime boundary;
9. Asks for one new direct child project directory;
10. Shows the request, destination, exact model, and provider-usage consequence;
11. Requires explicit authorization before model-backed Planning;
12. Uses a read-only bootstrap Planning capability for bounded material
    clarification, with elapsed model-wait heartbeats and visible response
    validation or bounded repair;
13. Shows one complete requirements, implementation, Dynamic Team, dependency,
    permission, model, timeout, concurrency, iteration, and budget overview;
14. Lets the user approve, request a natural-language revision, make a supported
    safe edit, or cancel before any execution Agent is created.

Interactive text is validated before it enters Planning evidence. If the terminal
supplies an invalid Unicode byte sequence, SAT explains the affected field and
asks for that answer again instead of exposing a schema-validation traceback.

Declining the profile or Planning authorization exits without a model request.
Cancelling Planning preserves its evidence but creates no execution run or
delivered project.

## Saved Configuration

SAT configuration is stored atomically with mode `0600` at:

```text
${XDG_CONFIG_HOME:-$HOME/.config}/software-agent-team/config.json
```

Schema version 6 stores one or more secret-free model profiles, the default
bootstrap profile, strict or policy routing, optional capability and stage
overrides, and the only currently supported runtime switch condition:
`provider_failure`. A profile contains a canonical OpenClaw `provider/model`,
its authorized SAT Agent capabilities, deterministic integer priority, and an
optional paired price table. It never contains a credential.

The same configuration may contain an adaptive `max_concurrency` from 1
through 16, `compact`, `standard`, or `detailed` progress visibility, and an
explicit global invocation-timeout override. The guided first-use flow writes
one strict default profile and uses controller defaults for other fields.
Existing schema-v1 through schema-v5 values migrate one way into schema 6;
the former scalar model and price fields become the default profile rather
than a second source of truth.

The normal first-run wizard stores only one strict default model profile in SAT
configuration and uses checked-in runtime defaults. Credential entry and
persistence remain owned by OpenClaw, but its state is isolated at:

```text
${SAT_STATE_ROOT:-${XDG_STATE_HOME:-$HOME/.local/state}/software-agent-team}/openclaw/
```

This directory is not shared with `~/.openclaw` or any caller-selected
OpenClaw profile. Credentials may instead come from a trusted caller
environment. They are excluded from SAT configuration, exports, generated
projects, and run evidence.

The pinned runtime may not yet list every explicitly supported provider model.
For a reviewed compatibility case, SAT adds versioned routing and model
metadata to its private run configuration without copying a credential. An
available trusted provider environment variable is represented only by its
variable name; otherwise OpenClaw resolves the provider through SAT's isolated
auth profiles. SAT checks the resulting exact model locally before continuing.

Reconfigure or inspect the secret-free values with:

```bash
sat configure
sat configure --show
```

Scripted product setup needs only a model:

```bash
sat configure --non-interactive --model provider/model
```

Non-interactive configuration records the requested reference; the next
`sat` launch validates its exact catalog/auth route before asking project
questions. Interactive `sat configure` performs that validation before saving.
Normal startup blocks only when the default bootstrap profile is unavailable.
It warns about an unavailable optional profile without deleting or resetting
the saved policy; if Planning selects that profile, run preflight stops before
the first execution Agent call.

Pricing, additional model profiles, route policy, adaptive maximum concurrency,
progress visibility, and timeout overrides are advanced configuration and are
not part of normal first-use setup. For example:

```bash
sat configure --non-interactive --model provider/model \
  --max-concurrency 4 \
  --progress-visibility detailed
```

This sets a scheduling cap and detailed progress; dependency readiness and
shared-workspace writer safety may reduce actual concurrency. Fixed-evaluation
verification concurrency
remains a separate `sat run` option documented in the
[`Phase 1 evaluation runbook`](phase1-runbook.md). When no trustworthy price is
available, a product run reports estimated cost as unavailable rather than
inventing `$0.00`.

An advanced policy-routing example is:

```bash
sat configure --non-interactive \
  --routing-mode policy \
  --add-model-profile fast=provider/fast-model \
  --profile-capabilities fast=implementation,integration,testing,review \
  --profile-priority fast=10 \
  --route-capability implementation=fast \
  --allow-provider-switch \
  --max-model-switches 1
```

The default profile continues to serve bootstrap clarification and Planning.
For each runtime Agent, the controller resolves Agent edit, stage override,
capability override, default-profile support, then lowest numeric eligible
priority. The Planning overview exposes the resulting primary route, reason,
fallback list, and pricing before approval. A fallback is not a silent retry:
it must be in that Agent's approved assignment, is used only after an
attributable provider failure, consumes the run call budget, and is recorded
with the failed call and possible cost consequence. `--clear-model-routing`
returns configuration to one strict default profile.

Set an absolute `SAT_CONFIG_PATH` only when the configuration location must be
overridden. Keep the same value set for later `sat` and `sat-uninstall`
commands.

## Product State and Delivery

Internal product data lives beneath:

```text
${XDG_STATE_HOME:-$HOME/.local/state}/software-agent-team/
```

Its separate `planning/`, `runs/`, `workspaces/`, `sources/`, and `openclaw/`
directories contain write-once Planning and run evidence, isolated Agent
clones, trusted seed repositories, and SAT's isolated OpenClaw state. Use an
absolute `SAT_STATE_ROOT` only for a deliberate state-location override. SAT
creates an exact ownership marker and refuses to adopt an existing unowned
directory, so an override cannot make an arbitrary OpenClaw or user directory
eligible for writes, export, or purge.

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

## Updating an Installation

Check the current release and source provenance locally:

```bash
sat --version
sat version
sat version --json
```

The short form prints the release plus an exact-revision suffix when one is
available. The detailed form separates the numeric release, full Git revision,
dirty state, install mode, managed channel, artifact digest, and provenance
status. These commands do not contact an update endpoint.

The product-level `sat update` and `sat channel` lifecycle is not implemented
yet. Until it is available, rerun the managed installation command.

Rerun the managed installation command. The bootstrap verifies ownership and a
clean tracked application before fetching the selected ref, then reconciles the
locked environment, private OpenClaw binary, image, launchers, and offline
checks. User configuration and isolated provider state live outside the
application directory and remain unchanged. Other OpenClaw installations are
not candidates for reconciliation.

For a contributor checkout, follow the update workflow in
[`development.md`](development.md#checkout-installation).

## Guided Uninstallation

Run from any directory:

```bash
sat-uninstall
```

The default removes SAT launchers, its Python environment, and its marked
private OpenClaw binary. It also removes the exact marked managed application
directory, or preserves a development checkout. By default it preserves:

- SAT configuration;
- Planning evidence, generated runs, workspaces, and trusted sources;
- SAT's isolated OpenClaw provider configuration, credentials, and sessions;
- Every OpenClaw installation and profile outside SAT;
- uv and its managed Python installation;
- Docker and the sandbox image.

Export configuration and generated state first with:

```bash
sat-uninstall --export-to "$HOME/sat-backup" --yes
```

The new absolute destination must not already exist and must be outside both
the application and SAT state. The export can contain
`configuration/config.json`, `data/planning/`, `data/runs/`,
`data/workspaces/`, `data/sources/`, and `EXPORT.txt`. Provider credentials
remain excluded.

Deletion requires explicit purge flags and may follow the same export:

```bash
sat-uninstall \
  --export-to "$HOME/sat-backup" \
  --purge-config \
  --purge-data \
  --yes
```

SAT's isolated provider state has its own choice because it contains secrets
and is intentionally excluded from export:

```bash
sat-uninstall --purge-provider-state --yes
```

That flag can delete only the `openclaw/` child of a validated SAT-owned state
root. It cannot select or delete `~/.openclaw`, a named OpenClaw profile, or
another installation.

Without a terminal, `--yes` is required. Use `sat-uninstall --help` to review
all keep, purge, export, and confirmation options. `make uninstall` invokes the
same script from a contributor checkout.

## Ownership and Recovery Boundaries

Uninstallation removes only SAT's marked private OpenClaw binary. SAT's
isolated provider state is preserved unless `--purge-provider-state` is
explicitly selected. Every other OpenClaw binary, running process, Gateway,
profile, configuration, credential, and session remains outside the ownership
boundary. uv, Docker, and the sandbox image are also preserved. The uninstaller
deletes a managed application or private runtime only when a regular marker
names that exact resolved directory; it refuses a missing, symbolic, invalid,
or mismatched marker. It also refuses symbolic configuration or state targets
and a missing or mismatched state-ownership marker before export or purge.

Preservation is the default because removing a CLI must not silently destroy a
generated project or its audit evidence. Inspect a completed export before
selecting any purge option.

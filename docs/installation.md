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
2. Downloads a temporary bootstrap helper without treating `main` as the
   installed application;
3. Resolves the latest published stable release to one SemVer, full source
   revision, tag, source-archive digest, and schema-support manifest;
4. Clones that immutable target into SAT-owned version storage and runs all
   installation checks before changing the active application;
5. Atomically activates the verified version through
   `${XDG_DATA_HOME:-$HOME/.local/share}/software-agent-team/app`, records its
   provenance, and removes the temporary helper.

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
may override `SAT_INSTALL_ROOT`, `SAT_REPOSITORY_URL`, `SAT_BIN_DIR`, or
`UV_BIN`, but those are not normal first-use questions. A custom application
path receives a dedicated hidden sidecar root for version storage and locks;
SAT never claims a generic sibling such as `versions/`. `SAT_INSTALL_CHANNEL`
may explicitly select `dev`, and only that channel accepts `SAT_INSTALL_REF`.
`SAT_RELEASE_API_URL` is an advanced stable-release resolver override.
The OpenClaw prefix is intentionally not configurable: accepting an arbitrary
prefix would allow installation to mutate a pre-existing OpenClaw runtime.

If Docker, a download, or an offline check interrupts installation, correct the
reported condition and rerun the same command. A lifecycle ownership marker
makes that retry distinguish SAT staging from unrelated files. The current
application link and installation record do not change until the candidate has
completed setup and verification; a failed activation restores both. User
configuration and state remain outside version storage. A sandbox image that
builds but exits before OpenClaw can execute tools is rejected before SAT
reports installation success or creates a new launcher.

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
- Available storage plus Linux/cgroup memory and PID headroom for at least one
  policy-bounded sandbox;
- Existing OpenClaw containers whose bind mounts prove that they belong to
  SAT's state boundary, reported without inspecting or changing another
  OpenClaw installation;
- Active, orphaned, and stale SAT provider-process leases, identified by PID,
  process group, and Linux process start time rather than PID alone;
- `sat` launcher visibility.

Every failed condition includes a corrective action. SAT does not rebuild the
toolchain or image during normal startup; a missing installed component points
back to the installer.

If task admission or approved-plan readiness is not ready, SAT stops before
the next model call, Agent creation, source preparation, or workspace mutation.
The foreground prompt lets the user apply the reported remediation and recheck
the same task. A changed observation appends a new immutable self-check
revision: only the changed result and its transitive dependents receive fresh
evidence, while unrelated results retain their original evidence and
timestamps. A recheck with no changed input creates no duplicate revision.

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
At the next task admission, SAT also performs a read-only inventory of any
such containers that already exist. It distinguishes running from stopped
resources and shows exact abbreviated container IDs. This observation never
silently removes a container because another foreground SAT process may still
own a running task.

SAT persists a private lease immediately after each isolated OpenClaw child is
started and removes it after that exact child reaches a terminal state. A new
foreground process can therefore distinguish a concurrently active invocation
from a proven orphan whose controller identity no longer exists. PID reuse is
classified as a stale lease and is never signalled. Inspect without changing
anything, or explicitly recover only proven remnants:

```bash
sat cleanup
sat cleanup --orphans
```

Recovery first terminates the exact orphaned process group, then revalidates
and removes only containers with the leased session key and a bind mount under
SAT's owned state root. Active leases and resources outside that root are left
unchanged. If Docker cleanup fails, the lease remains so the operation can be
retried rather than losing its ownership evidence. Recovery holds a Linux
pidfd across signalling so a PID or process-group reuse race cannot redirect
the action; without pidfd support it fails closed.

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

Schema version 7 stores one or more secret-free model profiles, the default
bootstrap profile, strict or policy routing, optional capability and stage
overrides, and the only currently supported runtime switch condition:
`provider_failure`. A profile contains a canonical OpenClaw `provider/model`,
its authorized SAT Agent capabilities, deterministic integer priority, paired
input/output prices with their source and observation time, and context-window
capacity with its source and observation time. Setup discovers these facts from
the isolated runtime when possible, displays them, and lets the user correct
prices. It asks for context only when discovery fails and never treats an
unknown price as zero. It never contains a credential.

The same configuration may contain an adaptive `max_concurrency` from 1
through 16 and `compact`, `standard`, or `detailed` progress visibility. The
guided first-use flow writes one strict default profile and uses controller
defaults for other fields. Existing schema-v1 through schema-v6 values migrate
one way into schema 7;
the former scalar model and price fields become the default profile rather
than a second source of truth.

Before each task's first model call, SAT refreshes every authorized route and
freezes a task-scoped metadata snapshot. It then asks for one maximum total
model spend in USD and, separately, whether to set a whole-run deadline; no
deadline is the recommended default. Calls, tokens, Agent count, Reviewer
count, iterations, and cumulative Agent duration are measured but are not
separate ordinary-product limits.

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
and progress visibility are advanced configuration and are not part of normal
first-use setup. For example:

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
available, task admission asks the user to supply it or explicitly confirm a
zero-cost route before the first model call rather than
inventing `$0.00`.

An advanced policy-routing example is:

```bash
sat configure --non-interactive \
  --routing-mode policy \
  --add-model-profile fast=provider/fast-model \
  --profile-capabilities fast=implementation,integration,testing,review \
  --profile-priority fast=10 \
  --route-capability implementation=fast \
  --allow-provider-switch
```

The default profile continues to serve bootstrap clarification and Planning.
For each runtime Agent, the controller resolves Agent edit, stage override,
capability override, default-profile support, then lowest numeric eligible
priority. The Planning overview exposes the resulting primary route, reason,
finite configured fallback list, and pricing before approval. A fallback is not a silent retry:
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
status, followed by the readable interval for each independently versioned
persisted schema family. These commands do not contact an update endpoint.

Check the currently selected managed channel without mutation:

```bash
sat update --check
sat channel status
```

Apply an available target with an interactive confirmation:

```bash
sat update
```

`sat update --yes` is the explicit non-interactive form. Stable update
availability compares only the numeric release version. A different Git
revision bound to the same stable number is an identity conflict, not an update
to accept. Dev-channel checks may report an exact ref revision change, but that
commit-only drift is not a normal stable update notification.

Switching channels is separate and always explicit:

```bash
sat channel switch dev
sat channel switch dev --ref <branch-tag-or-full-commit>
sat channel switch stable
```

Install, update, and channel switch use one transaction: validate lifecycle
ownership, resolve an immutable target, show the current and target identities,
obtain confirmation, hold the lifecycle lock, stage and verify the complete
application, check every persisted schema family, and atomically replace the
logical application link and installation record. An active run, unsupported
newer state, conflicting launcher, source drift, or failed candidate stops
before activation. If activation itself fails, the previous link and record are
restored. A successful change retains older release storage until uninstall;
user configuration and isolated provider state are not rewritten.

These commands operate only on a verified managed installation. A contributor
or other source checkout receives an explicit refusal instead of an implicit
Git mutation. It follows the workflow in
[`development.md`](development.md#checkout-installation).

For a contributor checkout, follow the update workflow in
[`development.md`](development.md#checkout-installation).

## Guided Uninstallation

Run from any directory:

```bash
sat-uninstall
```

The default removes SAT launchers and the complete marked managed application,
including retained versions and each version's private OpenClaw binary. A
source checkout is preserved after its checkout-local environment and private
runtime are removed. Before changing files, a managed uninstall binds the
active release marker, lifecycle-root marker, installation record, logical
application link, and recorded launchers; it also refuses an active run or a
concurrent install/update. By default it preserves:

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

The new absolute destination must not already exist and must be outside the
application, managed lifecycle root, and SAT state. The export can contain
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
deletes managed version storage only when regular root, release, and install
records agree on the exact active identity and owned paths; it refuses missing,
symbolic, invalid, or mismatched metadata. It also refuses symbolic
configuration or state targets and a missing or mismatched state-ownership
marker before export or purge.

Preservation is the default because removing a CLI must not silently destroy a
generated project or its audit evidence. Inspect a completed export before
selecting any purge option.

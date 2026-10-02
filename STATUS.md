# Project Status

**Last reviewed:** October 2, 2026 (Pacific Time)

This page records current implementation and validation facts. Product direction
belongs to [`VISION.md`](VISION.md); contracts and operating instructions live
in [`docs/`](docs/README.md). Previous status snapshots remain available through
the [history map](docs/history/README.md).

## Release and checkout boundary

The latest published stable release is [`v0.6.9`](https://github.com/urntt/software-agent-team/releases/tag/v0.6.9),
tagged at `d7384f013d94f2ed3af8bc6e9caca74c370e07aa`. Its normal
[exact-tag gate and publisher](https://github.com/urntt/software-agent-team/actions/runs/37000165888)
succeeded. Candidate and tag canonical gates each passed 2,075 tests with ten
environment skips and clean tracked source. Official latest, bootstrap, manifest,
and independently fetched public-tag archive identities and digests were verified.

Configuration staging handles two historical SDK compatibility boundaries.
Retired package peers can be rebound only after current managed-lifecycle and
ownership validation; unknown or unowned targets remain refused. Legacy shared
and agent SQLite databases are migrated by the pinned SDK before provider prompts,
inside a constrained, networkless container with only the staging copy writable.
Cancellation preserves the original state; committed registry paths remain relative
and subsequent configuration can read the migrated databases. Unresolved container
cleanup preserves its exact candidate and container receipts.

Real pinned-SDK/Docker checks on ordinary Linux covered schema-1 shared and agent
stores, WAL data, authentication-record preservation, cancellation/commit,
subsequent configuration, and damaged or unsupported targets. The affected suite
passed 174 checks; the migration suite passed eleven, with four selected checks
also validating the final repeated-configuration assertion. Docker-image skips in
CI are supplemented by these real local SDK checks.

Exact candidate and public stable installation assertions passed on an ordinary
Linux account using an isolated tmpfs profile because the host disk was full.
The checks covered identity, normal same-version update, actual first-launch
configuration cancellation, SDK migration/commit, repeated staging, default export,
and canonical uninstall. Attributable account, mount, process, and temporary-resource
cleanup preserved the prior Docker inventory. Original operator capacity/output
assertion failures remain failures; continuations completed the remaining assertions
in the same installations. This is not ordinary disk-capacity acceptance or a new
provider-backed journey. Unchanged installation/channel/rollback owners reuse
0.6.8 evidence after component comparison. Original same-machine WSL configuration,
model checks, subsequent startup, and uninstall acceptance remain pending; the
cause of the earlier catalog failure remains unknown.

Managed activation has one image rollback owner and retains both primary and
rollback errors. Container-referenced cleanup anchors remain attributable in
individual retired journals across repeated activations, including older target
formats. Actual candidate-to-stable activation preserved 3,181 configuration/run
files. After attributable old containers were removed, another public version
check retained the original anchor because other containers still referenced
its image. This validates safe deferral; successful reclamation after those
remaining references disappear is still unverified.

A Linux x86_64 ordinary-account journey began with public 0.6.2 installation,
first DeepSeek configuration/check, and subsequent startup, then continued in
the same installation through public 0.6.6 upgrade and task execution. This
was not a fresh 0.6.6 installation. The approved five-Agent task passed all five
deterministic quality gates, testing, general Review after an evidence
correction, and Security Review, and delivered an attributable Git revision.
All ten real invocations settled; there were no active, unpriced, or unreported
calls. The recorded model-cost estimate was $0.175494660 against a $1.50 task
ceiling; it is not an independently confirmed provider invoice.

An independent Chromium check ran the unchanged delivered setup/test/start
commands and application inside the configured immutable image, with no
network, UID 20000, a read-only root, 512 MiB, and 128 PIDs. Its complete core
workflow covered CRUD, ordinary search, delete confirmation/cancellation,
reload and server-restart persistence, error recovery, and literal content
rendering. Sixteen of nineteen checks passed. Three checks identified
SQL-wildcard search semantics and a hidden editor form exposed by CSS. The
accepted model Review did not establish that every external criterion passed;
these are observations about this generated output, without a demonstrated
systematic SAT defect.

Planning and runtime controls exercised slow partial input, completion,
cursor/delete editing, three visibility levels, future guidance, and pause at
a safe checkpoint followed by resume. Nine attributed OpenClaw Agent SQLite
sessions were snapshotted before purge. Export and canonical `sat uninstall`
passed; account, HOME, UID processes/temp, and attributable containers/volumes
were cleaned while the prior Docker baseline was preserved.

## Implemented in the checkout

- Legacy private-state recovery binds staged npm host peers to the current
  marked SDK, retains internal links across activation, and repairs only known
  empty root-owned SDK skill directories through the constrained Docker owner.
  Partial uninstall errors retain completed exports/cleanup and safe OS details.
  The implementation passed 169 focused checks and a Linux canonical gate
  (2,073 passed, one root-only skip). Actual UID 1000/Docker reproduction covered
  the permission failure, recovery, staging, and explicit state purge with the
  existing container inventory unchanged. This is not WSL, provider onboarding,
  or complete application-uninstall acceptance. The original nonzero WSL model
  catalog exit has no preserved underlying diagnostic; its cause remains unknown.
- Ordinary `sat` provides isolated startup diagnostics and provider setup,
  multiline requests, Planning and approval, adaptive teams, deterministic
  gates, bounded typed corrections, run controls, attributable delivery,
  export, and uninstall. The [journey contract](docs/product-demo-slice.md)
  and [terminal contract](docs/terminal-interface.md) own the details.
- The Controller owns lifecycle, permissions, snapshots, budgets, and evidence.
  Historical tool warnings cannot override a valid final submission solely
  because their text contains an error; native completion, session identity,
  and paired receipts remain required. [Runtime evidence](docs/runtime-evidence.md)
  owns these boundaries.
- Dependency versions/integrity and lifecycle permission are locked; first uv
  installation uses a shared checked policy. Host Git callers share isolated
  configuration, and publishing is separated from read-only release validation.
- Structural Planning changes preserve authority and existing rules. Recorded
  cross-version replay and production-interface checks support the refactor;
  they do not establish an improved model success rate.

## Validation limits and open capabilities

This journey establishes one Linux x86_64, existing-Docker, DeepSeek route.
Fresh WSL/ARM64 locked-runtime installation, rootless Docker without a host
Docker group, Gemini-specific response and timeout shapes, credential entry
without environment keys, live provider cancellation costs, and later-iteration
failure paths still need their specified targeted evidence. Pause does not
prove provider cancellation, and local authorization is not a provider hard
billing cap.

Generated-project execution supports the current local Python 3.12 profile.
Automatic resume after an interrupted CLI process and durable cross-process
controls are unavailable. The [fixed-topology comparison](docs/evaluation-results/2026-09-28-fixed-topology.md)
is a small controlled observation and does not establish general quality,
cost, or latency superiority or justify changing the ordinary team default.

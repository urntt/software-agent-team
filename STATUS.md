# Project Status

**Last reviewed:** October 2, 2026 (Pacific Time)

This page records current implementation and validation facts. Product direction
belongs to [`VISION.md`](VISION.md); contracts and operating instructions live
in [`docs/`](docs/README.md). Previous status snapshots remain available through
the [history map](docs/history/README.md).

## Release and checkout boundary

The latest published stable release is [`v0.6.8`](https://github.com/urntt/software-agent-team/releases/tag/v0.6.8),
tagged at `84f8e825e5c766f2ae5a097f27578b0df8dab481`. Its normal
[exact-tag gate and publisher](https://github.com/urntt/software-agent-team/actions/runs/36989698626)
succeeded. Both candidate and tag canonical gates passed 2,070 tests with four
environment skips, with the exact source clean. Official bootstrap, manifest,
and source-archive identities and digests were independently verified.

A same-machine WSL report confirmed that 0.6.7 upgraded successfully but still
refused the historical provider peer during first-run configuration. Read-only
metadata confirmed that the old SDK directory was gone and the current SDK's
ownership marker and host were valid. Normal activation retains only the active
release and its direct predecessor; requiring the retired SDK's marker therefore
made configuration recovery incompatible with normal cleanup.

The 0.6.8 fix recognizes both historical SDK package layouts and permits rebinding
a missing sibling only after validating the current active managed lifecycle.
It never follows or recreates the old target. The regression crosses production
activation, retirement, staging, cancellation, and configuration commit, with
negative checks for unrelated, unknown, and unowned targets. Seventy-five focused
migration and managed-install checks passed.

An ordinary Linux account installed the exact 0.6.8 candidate and started `sat`
with a dangling SDK peer matching the reported shape. The real provider-setup
prompt was reached, SIGINT exited 130, and cancellation preserved the original
peer and opaque credential fixture. Production staging/commit and actual Node
loading of the rebound SDK package passed, without changing the current SDK or
recreating the retired directory. Export, uninstall, and attributable cleanup
passed with the prior Docker inventory unchanged. A separate ordinary account installed public 0.6.7 and upgraded to 0.6.8 through
normal `sat update`, with the dangling peer present before upgrade. Exact stable
identity, state preservation, SDK-peer staging/commit, same-target no-op, export,
and canonical `sat uninstall` passed; cleanup preserved the prior Docker
inventory. The unchanged channel/rollback owners reuse 0.6.7 evidence after
component comparison, rather than claiming the whole matrix was rerun.
The original same-machine WSL acceptance remains pending. These checks do not
constitute a provider-backed journey or determine the earlier catalog failure's
cause.

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

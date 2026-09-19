# Project Status

**Current milestone:** validate published `v0.4.7` through one fresh public
ordinary-user journey after the `v0.4.6` Test Author permission failure.

**Last updated:** September 19, 2026

This document records current implementation and validation evidence. Product
and architecture decisions belong to [`VISION.md`](VISION.md).

Published `v0.4.6` passed the exact-tag hosted gate, and a fresh
non-Docker-group account installed it through the public one-command bootstrap
with rootless Docker. First and later 16/16 self-checks, real DeepSeek check,
29/29 task admission, Planning and the seven-Agent execution preflight passed.
The CLI Implementer completed, but the Test Author then changed root
`README.md` outside its `repository/tests` workspace and the Controller
correctly rejected the commit. The approved test task owned only tests; a
parallel role summary had independently claimed README work owned by the
Integrator. Build exited 2 with no accepted delivery. The raw evidence, formal
export and exact account cleanup were retained for diagnosis.

Published `v0.4.7` derives writable-Agent runtime responsibility from
assigned task descriptions and its runtime rationale from the approved task
count and workspace scope. Project-wide requirements do not expand an Agent's
assigned work or write scope. The original approved plan recompiled through
the production Planning interface no longer assigns README to the Test Author.
The affected Planning, dynamic-prompt and runner suite passed 375 tests. The
`v0.4.7` local canonical gate passed 1,883 tests; all four stages completed,
process and temporary-directory cleanup was complete, Docker and process-lease
inventories were empty, and cgroup/kernel OOM deltas were zero. The final
revision `3be7542a53bd7d3748e50ad07027ae449f02e6c5` upgraded an existing
non-Docker-group rootless dev account without changing its configuration or
daemon identity.
The exact-tag hosted gate passed 1,880 tests with three environment skips and
published a unique verified Release. A fresh full public-user journey remains
pending.

## Current Release

The immutable `v0.4.7` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.4.7),
package version, and release manifest identify source revision
`3be7542a53bd7d3748e50ad07027ae449f02e6c5` and Git archive digest
`sha256:aeed20024dadaae66558ec70e2751a0605ee6914ee7be85ad5cc0a2489133289`.
Exact-tag GitHub Actions
[run 35471899923](https://github.com/urntt/software-agent-team/actions/runs/35471899923)
passed 1,880 tests with three environment skips; the local candidate passed
1,883. The unique Release manifest asset digest is
`sha256:5d15e61dd636382e5334fb3f94b24a9c5c08d2e14fc5d66d9510e9bdc1e21cd7`.
The prior public rootless run described above stopped before delivery.


The immutable `v0.4.1` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.4.1),
package version, and release manifest identify source revision
`e0e36abfe4406568b9446690168f8f27d95e03f7` and Git archive digest
`sha256:c86892b3860494f5aba3963d5a601b4232ffa989a30cf6af45edb8e886b16644`.
Exact-tag GitHub Actions
[run 35434557934](https://github.com/urntt/software-agent-team/actions/runs/35434557934)
passed 1,860 tests with three environment skips; the clean rootless revision
passed 1,862 with one root-only fixture skip. The unique Release's manifest
asset digest is
`sha256:16f52037ec28d1d70eb82560f5d96c91bac17dd66fa43ce5484be6858fd038dd`.
The fresh public installation and provider-backed Planning outcome are
described above; that failure is not a completed product journey.

The immutable `v0.4.0` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.4.0),
package version, and release manifest identify source revision
`6df605436c98929820dc2cecf03987e1392f353a` and Git archive digest
`sha256:100a6f210fb87b96882555ca93122d5304cf5539b45b2c55485cf8bd76e264a5`.
Exact-tag GitHub Actions
[run 35426345810](https://github.com/urntt/software-agent-team/actions/runs/35426345810)
passed 1,855 tests with three environment skips; the exact local revision
passed 1,857 with one root-only fixture skip. The unique Release's manifest
asset digest is
`sha256:73a958ee3a07d488a4cbcc517a8e540f5031aa436d85b33d58894f1fa6b8fcc3`.
The public rootless and separate rootful installation lifecycles passed without
provider calls. The subsequent fresh rootless provider journey found the
Planning correction defect described above before Agent creation.

The immutable `v0.3.3` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.3.3),
package version, and release manifest identify source revision
`a4745795e469c4c71f0a7b19d400b6f039369eff` and Git archive digest
`sha256:30f377660d1b06983635e339c7ec40da393a5d2588b269b8d07c1027669a5354`.
Exact-tag GitHub Actions
[run 35421062689](https://github.com/urntt/software-agent-team/actions/runs/35421062689)
passed 1,836 tests with three environment-dependent skips and published one
identity manifest asset. The pre-commit local candidate gate passed all 1,839
tests; its report correctly records a dirty worktree, while the hosted report
binds the clean exact tag. Fresh non-root public bootstrap and a separate real
`v0.3.2` to `v0.3.3` upgrade passed identity, state preservation, no-op,
active-run refusal, injected rollback, channel round trip, export, full
uninstall, and exact cleanup without provider calls.

The immutable `v0.3.2` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.3.2),
package version, and release manifest identify source revision
`35f353d418b728c0e748b1663850d394d2c0ae34` and Git archive digest
`sha256:740c7d52cb9d023f6a62f6e9d138b6e4d402c1975a27e27a8f11b53bec1f5140`.
Exact-tag GitHub Actions
[run 35406364835](https://github.com/urntt/software-agent-team/actions/runs/35406364835)
passed 1,832 tests with three environment-dependent skips and published exactly
one identity manifest asset. The clean local release revision passed all 1,835
tests. A fresh non-root public bootstrap of current stable and a separate
`v0.3.1` to `v0.3.2` lifecycle passed exact identity, update, state migration,
same-target handling, active-run refusal, injected activation rollback, channel
round trip, export, full uninstall, and exact cleanup without making a provider
call. The terminal presentation changes still require ordinary-user live TTY
validation; no new complete provider journey was started for this patch.

The immutable `v0.3.1` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.3.1),
package version, and release manifest identify source revision
`4c71b6cd76b5aa78a25806c590f5c71a8044c30c` and Git archive digest
`sha256:83a0716d0f8c21e34539222408946a312c6975243a17427cf29f35db09457c7e`.
Exact-tag GitHub Actions
[run 35389376206](https://github.com/urntt/software-agent-team/actions/runs/35389376206)
passed 1,827 tests with three environment-dependent skips and published exactly
one identity manifest asset. The clean local release revision passed all 1,830
tests. A fresh non-root public bootstrap of current stable and a separate
`v0.3.0` to `v0.3.1` lifecycle passed exact identity, update, state migration,
same-target handling, active-run refusal, injected activation rollback, channel
round trip, export, full uninstall, and exact cleanup without making a provider
call. A complete provider-backed user journey remains pending.

The immutable `v0.3.0` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.3.0),
package version, and release manifest identify source revision
`7fc8520879c9b1c437065e774df78845734e83ec` and Git archive digest
`sha256:0156235a04d96ae1f0c0d2c4df85c7bfbf4e7a52914d98af73ffc8a961cd5364`.
Exact-tag GitHub Actions
[run 35309334528](https://github.com/urntt/software-agent-team/actions/runs/35309334528)
passed 1,820 tests with three environment-dependent skips and published exactly
one identity manifest asset. The clean local release revision passed all 1,823
tests. A fresh non-root public bootstrap of current stable and a separate
`v0.2.20` to `v0.3.0` lifecycle passed exact identity, update, state migration,
same-target handling, active-run refusal, injected activation rollback, channel
round trip, export, full uninstall, and exact cleanup without making a provider
call. A previously affected ordinary account also reran the public bootstrap and
activated exact `v0.3.0`; its later journey exposed the Review evidence and live
terminal defects corrected by `v0.3.1`.

The immutable `v0.2.20` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.2.20),
package version, and release manifest identify source revision
`440aa8cc8598d361f05cbc824d56b0d7cb8ff7bd` and Git archive digest
`sha256:e3c1f32d80d070bf5baf719673f81cf328228047150f91f898faafa3be38107b`.
Exact-tag GitHub Actions
[run 35244917057](https://github.com/urntt/software-agent-team/actions/runs/35244917057)
passed 1,792 tests with three environment-dependent skips and published exactly
one identity manifest asset. A fresh non-root `v0.2.19` to `v0.2.20` lifecycle
passed installation, fixed-revision upgrade, identity and current-version checks,
state preservation, export, full uninstall, and exact cleanup without making a
provider call. Later provider-backed ordinary-user evidence exposed the Review,
terminal-progress, and input defects released in `v0.3.0`.

The immutable `v0.2.19` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.2.19),
package version, and release manifest identify source revision
`a90fb1426554cf2f98066a37e4949458cfd9fccc` and Git archive digest
`sha256:f8ba508a0af15f30def9d4bbfe5707cc160f1042a9bb96d4e63b4006e103661d`.
Exact-tag GitHub Actions
[run 34884770667](https://github.com/urntt/software-agent-team/actions/runs/34884770667)
passed 1,683 tests with three environment-dependent skips and published exactly
one identity manifest asset. A fresh non-root `v0.2.18` to `v0.2.19` lifecycle
passed installation, upgrade discovery and activation, state preservation,
same-target and channel checks, rollback, foreign OpenClaw isolation, export,
full uninstall, and exact cleanup. A separate provider-backed journey reached
the exact configured route check, then timed out before Planning; it did not
produce a completed delivery.

The immutable `v0.2.18` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.2.18),
package version, and release manifest identify source revision
`388aca442fa5bb38c4866c8fb1796947f82c5de6` and Git archive digest
`sha256:abaf5ee77639997d2da8718d8627240ce26f97313e0c1c394903cf41a04ddeb0`.
It lets a write-capable Agent with attributable tool progress use the existing
bounded continuation when its terminal response omits the required typed
submission. The exact clean release head passed all 1,665 local tests; exact-tag
GitHub Actions
[run 34793125611](https://github.com/urntt/software-agent-team/actions/runs/34793125611)
passed 1,662 tests with three environment-dependent skips and published exactly
one identity manifest asset. A fresh non-root `v0.2.17` to `v0.2.18` lifecycle
passed installation, upgrade discovery and activation, state preservation,
same-target and same-development-ref no-ops, active-run refusal, fault-injected
rollback, channel round trip, image reconciliation, foreign OpenClaw isolation,
credential-free export, full uninstall, and exact cleanup.

An additional fresh provider-backed rehearsal installed `v0.2.17`, upgraded to
`v0.2.18`, completed first-use DeepSeek configuration and provider checking,
handled interrupted task input, passed repeat-startup self-checks and all 26 task
admission checks, and was intentionally cancelled after the first Planning
invocation was queued. Exact fresh-account cleanup completed, but that partial
rehearsal does not establish Planning, execution, acceptance, export, or delivery.
The intermittent Planning evidence absence observed on `v0.2.17` therefore remains
unclassified, and a fresh complete provider-backed journey remains pending.

The immutable `v0.2.13` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.2.13),
package version, and release manifest identify source revision
`0bc8cd7676b8c1c2299d6512d25921176a76dd4a` and Git archive digest
`sha256:22532cedefedf35ca68951f38daa6f21e63bbd3b9644742a35036c04f0dc878b`.
It removes the false pre-activation cleanup warning from a normal first install
while retaining fail-closed missing-link and dangling-link checks. The exact
clean release head passed all 1,648 local tests; exact-tag GitHub Actions
[run 34758547029](https://github.com/urntt/software-agent-team/actions/runs/34758547029)
passed 1,646 tests with two environment-dependent skips and published exactly
one identity manifest asset. A fresh non-root `v0.2.12` to `v0.2.13` lifecycle
passed configuration and state preservation, same-target no-op, active-run
refusal, fault-injected rollback, channel round trip, Docker image
reconciliation, foreign OpenClaw isolation, credential-free export, full
uninstall, and exact cleanup. A separate fresh provider-backed journey passed
installation, upgrade, first-use configuration and smoke checking, task-input
interruption, repeat startup, and all 26 task-admission checks before a
whole-record Planning correction crossed the otherwise-atomic decision branches.
Both provider calls settled and all resources were cleaned; no runtime Agent,
workspace, or delivery was created in that failed journey.

The immutable `v0.2.12` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.2.12),
package version, and release manifest identify source revision
`4c67ea0f3924a261b93599a30abb8d6c3f205cc9` and Git archive digest
`sha256:283d43bdbe74824037fd51dcd89ce364244b20720dbe2924aadd2dac536fde07`.
It repairs final-attempt Planning correction completeness and constrains
missing-specialist topology replacement to exact validated authority and
acyclic implementation coverage. The exact clean release head passed all 1,645
local tests; exact-tag GitHub Actions
[run 34755838222](https://github.com/urntt/software-agent-team/actions/runs/34755838222)
passed 1,643 tests with two environment-dependent skips and published exactly
one identity manifest asset. A fresh non-root `v0.2.11` to `v0.2.12` lifecycle
passed configuration and legacy-state preservation, same-target no-op,
active-run refusal, fault-injected rollback, channel round trip, Docker image
lineage reconciliation, foreign OpenClaw isolation, credential-free export,
full uninstall, and exact resource cleanup. Its first staged install exposed
the pre-activation warning addressed by the current candidate.

The immutable `v0.2.11` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.2.11),
package version, and release manifest identify source revision
`7aeebd8686c15a3d8dc6193674f2584b1628c66f` and Git archive digest
`sha256:7d1dae6d1302369cf6d680fb84a523c9d9fc5161aef3862ab08883145f3a9237`.
It bounds direct-input decision summaries and lets a newly activated target
reconcile Docker parent lineages left by an older updater. The exact clean
release head passed all 1,640 local tests; exact-tag GitHub Actions
[run 34749060028](https://github.com/urntt/software-agent-team/actions/runs/34749060028)
passed 1,638 tests with two environment-dependent skips and published exactly
one identity manifest asset. A fresh non-root `v0.2.10` to `v0.2.11` lifecycle
passed configuration and legacy-state preservation, no-op, active-run refusal,
fault-injected rollback, channel round trip, foreign OpenClaw isolation,
credential-free export, full uninstall, and exact resource cleanup. A later
provider-backed journey exposed the final-correction completeness defect now
addressed by the current candidate.

The immutable `v0.2.10` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.2.10),
package version, and release manifest identify source revision
`abb6da9b5b53ceeb971927669099539f8cac37a1` and Git archive digest
`sha256:fc0209f9788276192b12b6cb651e84c361d77ec01996998c4e3d1e45c5a2c3d3`.
Its exact clean candidate passed all 1,633 local tests; exact-tag GitHub Actions
[run 34744224945](https://github.com/urntt/software-agent-team/actions/runs/34744224945)
passed 1,631 tests with two environment-dependent skips and published exactly
one identity manifest asset. A fresh non-root published `v0.2.9` to `v0.2.10`
upgrade exercised both startup paths, official DeepSeek configuration and smoke
checking, task admission, and Planning correction before exposing the long
direct-input summary loop addressed by the current candidate. The same cold
upgrade showed that the legacy updater left the superseded parent records now
covered by the target-side handoff.

The immutable `v0.2.9` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.2.9),
package version, and release manifest identify source revision
`51a1a06e88fda0855b5678977a16d9fa289cd8f6` and Git archive digest
`sha256:9c990dedea7a9f0b4bc808258eb8c1f9627d05c2c8eeb11296739d382113dc67`.
It keeps a writable stdin pipe open only during the bounded exact start probe so
interactive generated CLIs are not rejected on synthetic EOF. The exact clean
revision passed all 1,629 local tests; exact-tag GitHub Actions
[run 34737613363](https://github.com/urntt/software-agent-team/actions/runs/34737613363)
passed 1,627 tests with two environment-dependent skips and published exactly
one identity manifest asset. Fresh fixed-revision development and published
`v0.2.8` to `v0.2.9` lifecycle rehearsals passed. A separate provider-backed
ordinary-user journey passed install, upgrade, first-run configuration and
provider check, repeat startup, and task admission before exposing the Planning
specialist-ID correction loop addressed by `v0.2.10`. The same
journey also confirmed that successful legacy Docker builds left attributable
superseded parent chains, motivating the bounded lineage cleanup in this
release.

The immutable `v0.2.8` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.2.8),
package version, and release manifest identify source revision
`703b1ac2dafe07e6b5ebe9b167a7d476f7bd7d29` and Git archive digest
`sha256:87e59c4db75c64f1125f6bfcf8b60b0a6a737fcc6b184e4438cc6cdf4ff93042`.
It limits ProductDefinition reference corrections to the affected ID array
while preserving validated source fields and sibling references. The exact
clean revision passed all 1,627 local tests; exact-tag GitHub Actions
[run 34733073206](https://github.com/urntt/software-agent-team/actions/runs/34733073206)
passed 1,625 tests with two environment-dependent skips and published exactly
one identity manifest asset. Fresh fixed-revision development and published
`v0.2.7` to `v0.2.8` lifecycle rehearsals passed. A provider-backed ordinary
user journey then passed installation, upgrade, both startup paths, official
DeepSeek configuration and check, Planning, live controls, and implementation.
The generated interactive CLI exposed the exact-start stdin defect addressed
by `v0.2.9`. A later Experience Reviewer request also reached the
declared provider-silence boundary and was stopped cleanly; its external cause
remains unknown and requires an independent fresh retry.

The immutable `v0.2.7` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.2.7),
package version, and release manifest identify source revision
`df5dd17790b09efdfda768abb46604bdeed42cd3` and Git archive digest
`sha256:4a93e006d7f9b3965eeb49ebf9b490171bba098ad24194894ad0ea7c3ffc6503`.
It distinguishes repeated no-progress background-process polling from verified
provider progress and enables the pinned runtime's native tool-loop detector.
The exact clean revision passed all 1,626 local tests; exact-tag GitHub Actions
[run 34729295174](https://github.com/urntt/software-agent-team/actions/runs/34729295174)
passed 1,624 tests with two environment-dependent skips and published exactly
one identity manifest asset. Fresh fixed-revision development and published
`v0.2.6` to `v0.2.7` lifecycle rehearsals passed. A provider-backed ordinary
user journey then passed install, upgrade, first-run configuration, provider
check, second-start self-check, and task admission before exposing the
ProductDefinition correction-scope defect addressed by `v0.2.8`.

The immutable `v0.2.6` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.2.6),
package version, and release manifest identify source revision
`ae5c86c19c797afa8b9339c043a5fd8eb7ad74ad` and Git archive digest
`sha256:3c6cd152ad1fab53270c339e8baba1d6d799fd98db2766d7861071dfafcc27ea`.
It keeps Planning specialist corrections monotonic across complete Agent-array
replacements and compiles Review-owned task bindings from each non-overlapping
criterion scope. The exact clean candidate passed all 1,621 local tests;
exact-tag GitHub Actions run 34722858123 passed 1,619 tests with two explicit
environment-dependent skips and published one identity manifest asset. Fresh
fixed-revision development and published `v0.2.5` to `v0.2.6` lifecycle
rehearsals passed. A provider-backed ordinary-user journey then completed
installation, upgrade, first and subsequent startup, official DeepSeek setup,
Planning, approval, and live controls before exposing the repeated no-progress
poll defect corrected by `v0.2.7`. Its controlled failure and
exact cleanup remain separate from successful acceptance.

The immutable `v0.2.5` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.2.5),
package version, and release manifest identify source revision
`c2e0819fa54333a2cc9ea867bd6a6ced46ef7632` and Git archive digest
`sha256:8307aa15c104652adc7588ebb541d474c6ddf707529381e0d6f435e816ec2bad`.
It narrows specialist Review artifact corrections to the security `surfaces` or
experience `workflows` entry collection while preserving validated shared
report fields. The exact clean revision passed the local canonical gate with
all 1,620 tests; exact-tag GitHub Actions
[run 34717499535](https://github.com/urntt/software-agent-team/actions/runs/34717499535)
passed 1,618 tests with two environment-dependent skips and published exactly
one identity manifest asset. A fresh published `v0.2.4` to `v0.2.5` lifecycle
passed upgrade, preservation, rollback, channel, export, uninstall, and exact
cleanup checks. A separate provider-backed ordinary-user run passed install,
upgrade, first-run configuration, provider check, second-start self-check, and
task admission, then exposed the Planning ownership defect corrected by the
`v0.2.6` release before any execution Agent was created.

The immutable `v0.2.4` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.2.4),
package version, and release manifest identify source revision
`8b93998160cf9f1c469d5b9475dcb5a0bdb77ce1` and Git archive digest
`sha256:3f5e23a26b85720d683078ab52ce07167ac462c14fb65bcca670de9984bdb9a8`.
This release recognizes the pinned official DeepSeek plugin's exact
`deepseek/deepseek-v4-flash` onboarding route as a reviewed preset. It freezes
the plugin's provider/native identity, OpenAI-compatible transport, public
endpoint, 1,000,000-token context, 384,000-token output limit, reasoning and
tool capabilities, while keeping the credential source in SAT's isolated
OpenClaw auth store. The plugin-generated provider catalog may omit
`compat.supportsTools`; that omission no longer converts this exact route into
an unreviewed custom profile or creates a second environment-key requirement.
Other provider/model pairs and custom endpoints still require explicit tool
support and retain the existing fail-closed behavior.

The package, lock, and change-impact ledger identify `v0.2.4` as a compatible
patch over `v0.2.3`. Focused model-profile, runtime-materialization,
configuration, transaction, CLI, release, and documentation tests pass. A
pinned OpenClaw local check using an isolated official-plugin state reports the
exact route available without `DEEPSEEK_API_KEY` and without a provider
request. The exact clean revision passed the local canonical `make check` with
doctor, formatting, lint, all 1,618 tests, complete cleanup coverage, and no
terminal resource residual. Exact-tag GitHub Actions
[run 34708846766](https://github.com/urntt/software-agent-team/actions/runs/34708846766)
passed 1,616 tests with two explicit environment-dependent skips, retained the
canonical gate artifact, and published exactly one identity manifest asset. A
fresh disposable non-root rehearsal started from the genuine `v0.2.3` Release,
discovered and activated `v0.2.4`, preserved configuration and legacy state,
exercised no-op, refusal, rollback, channel, foreign-resource, export, and purge
paths, and finished with no attributable resource residual or provider call. A
fresh task-admission rerun on the affected WSL environment remains pending.

The immutable `v0.2.3` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.2.3),
package version, and release manifest identify source revision
`56ec01ba0eb62d73d07d29ceeb4434b5b5e1e6b3` and Git archive digest
`sha256:35136d8cd38ebbf3944981725f579b2d18d2d81cdb805514efe25c181f02fb4a`.
The exact clean revision passed the local canonical `make check` with doctor,
formatting, lint, all 1,613 tests, complete cleanup coverage, and no terminal
resource residual. Exact-tag GitHub Actions
[run 34698877186](https://github.com/urntt/software-agent-team/actions/runs/34698877186)
passed 1,611 tests with two explicit environment-dependent skips on the hosted
non-root runner, retained the canonical gate artifact, and published exactly
one identity manifest asset. A fresh disposable non-root rehearsal then started
from the genuine `v0.2.2` Release, discovered and activated `v0.2.3`, preserved
configuration and legacy state, exercised no-op, refusal, rollback, channel,
foreign-resource, export, and purge paths, and finished with no attributable
resource residual or provider call.

The immutable `v0.2.2` tag,
[GitHub Release](https://github.com/urntt/software-agent-team/releases/tag/v0.2.2),
package version, and release manifest identify source revision
`acb9e89c423f8711623e801d40808d21fee271b8` and Git archive digest
`sha256:73a4b7335d01ab3eb74cd4e90aea9b079cf4913d85e614995045223cbcd02d5d`.
The exact clean revision passed the local canonical `make check` with Ruff
formatting and lint checks plus all 1,608 tests. Its successful exact-tag GitHub
Actions run passed 1,606 tests with two explicit environment-dependent skips on
the hosted non-root runner, retained the canonical gate artifact, and then
published exactly one identity manifest asset. An earlier workflow attempt on
the same immutable tag completed the repository gate but timed out while
creating the retained artifact; it published neither an artifact nor a Release.

The immutable `v0.2.1` tag points to
`b90b2634caa8b4a5360ed6bdc9b41d9c81ca4a2e`. Its local canonical gate passed
all 1,608 tests, but its exact-tag hosted gate failed one real CLI SIGINT
lifecycle check after 1,605 passes and two environment-dependent skips. No
Release was created. The tag remains immutable and unpublished, and its version
cannot be reused.

The earlier published stable remains immutable `v0.2.0` at
`4c0bdfcea6da0a6f3849c2b70a9e63ed2d5e098c`, with Git archive digest
`sha256:afeab5ec13d910c83ec06dbc64cfed491e4a84ee85725bbc8a5ff6ffaedf247f`.
The earlier stable `v0.1.2` remains immutable at
`64dcfc448fc229e4d03b4dd3722549ebeb09d2fb`, with Git archive digest
`sha256:6baebe8610673e9b9795779c9f5965ef452f8bd03d065f3b37a93bdd4fa9543e`.
The failed `v0.1.0` and `v0.1.1` tags also remain immutable and unpublished.

The product implementation at
`654b1521a3bfd302b0491e420d2b93aaf0f11f0f` completed one managed, non-root,
bare-`sat` reusable-product journey with
`deepseek/deepseek-v4-flash-vision-exp`. Task admission passed all
26 checks and approved-plan admission passed all 33 checks. Planning asked one
material audience/workflow/maturity question, then three automatic typed
corrections converged without an operator `r`. The approved Agent DAG placed a
read-only Reviewer after the Implementer; the Controller derived the matching
cross-Agent task dependency instead of asking the model or user to repeat it.

The Implementer produced a clean descendant commit, all five deterministic
project gates passed, and six exact-slot Reviewer corrections converged to an
accepted grounded report. The Controller delivered the same immutable commit
with all nine acceptance criteria passed. All 13 provider calls settled in one
task ledger, no call remained active, and no call lacked price or token usage.
External execution of the delivered setup, start, and test commands succeeded;
an independent nested fixture verified duplicate, unique, no-duplicate,
invalid-input, symlink, and read-only behavior. The documented default scan of
`.` also scans the newly created `.venv`, which produces correct but noisy
dependency-file matches and remains a non-blocking usability finding.

This evidence closes the shared Planning-correction, Review-authority, Git
ancestry, dependency-projection, and usable-product acceptance boundaries.
Release-head changes after the accepted product journey were limited to release
metadata and cross-environment test-fixture corrections; the exact release head
then passed the canonical gates above.

A fresh non-root installation using the documented one-command bootstrap
resolved the published stable Release and reported
`sat 0.1.2+g64dcfc448fc2 [stable]`. It completed local version and current-update
checks, first-run configuration with cancellation before any model call, an
explicit stable-to-dev-to-stable round trip on the same source revision, a
same-ref no-op, secret-free export, and preservation-aware full uninstall.
Stable and dev used distinct immutable provenance directories. A separately
owned OpenClaw process and its configuration, credential, and binary sentinels
remained unchanged throughout. SAT-owned application, configuration, and state
were removed, while uv, Docker, and the shared quality image were preserved.
Failed-activation rollback remains covered by real fault evidence whose three
lifecycle-owner Git blobs are identical at `v0.1.2`.

A later disposable non-root installation started from that genuine `v0.1.2`
Release, discovered `v0.2.0` through `sat update --check`, and activated it
through `sat update`. Exact release identity, schema-v9 configuration reading,
byte-identical configuration and legacy state preservation, same-target no-op,
active-run refusal, injected post-link-swap rollback, foreign OpenClaw
isolation, secret-free export, full uninstall, and exact account/HOME/resource
cleanup all passed. The scenario made no provider calls.

A fresh disposable non-root installation then started from the genuine
`v0.2.0` Release, discovered `v0.2.2` through `sat update --check`, and activated
it through `sat update`. Exact release identity, byte-identical configuration
and legacy state preservation, same-target and same-dev-ref no-ops, active-run
refusal, injected post-link-swap rollback, a stable-to-dev-to-stable round trip,
foreign OpenClaw isolation, secret-free export, full uninstall, and exact
account/HOME/resource cleanup all passed. The scenario made no provider calls.

## v0.3.2 implementation

The ordinary-user `v0.3.1` run completed all three Review roles and delivered
12/12 acceptance, while exposing remaining presentation problems. Active Agent
elapsed time used a ten-second log heartbeat even though the panel displayed
seconds, adjacent live cards had no visual separation, and startup, self-check,
Planning lifecycle, and approval output still printed controller-oriented detail
in the default standard view.

The development head now refreshes elapsed time independently once per second in
TTY live mode while retaining the ten-second append-only heartbeat. One blank
row separates permanent history from the live region and each active Agent, and
the renderer coalesces concurrent heartbeat wakes into one elapsed projection.
Default startup and self-check views show counts plus only warnings or required
actions. Planning uses the same bounded colored live-card behavior, keeps
adapter/attribution/shutdown detail out of standard scrollback, and presents a
human approval summary; `d` reveals the complete technical plan on demand.
`detailed + log + never` still restores the uncolored append-only presentation,
and the durable event and Planning evidence remain complete in every mode.

The affected progress, Planning, self-check, product, and CLI suites pass all
432 tests. The clean release revision passed all 1,835 local tests; its exact-tag
hosted gate passed 1,832 tests with three explicit environment skips. The public
fresh-install and `v0.3.1` to `v0.3.2` update lifecycles both completed with
exact cleanup and no provider calls. No new complete user journey was started.

## v0.3.1 implementation

The `v0.3.1` candidate repairs two defects observed in an ordinary-user
`v0.3.0` run. The pinned OpenClaw runtime may persist an orphaned not-found
result whose raw tool name contains an approved SAT tool followed by command
arguments after sanitizing the corresponding assistant call. SAT now recognizes
that exact canned rejection through a bounded printable shape and retains only
the approved first tool token plus provenance hashes. It still grants no
execution or progress authority, and unknown names, mismatched content, unsafe
characters, reused identities, or rejections after a terminal submission remain
invalid. Replaying the retained failing Reviewer session now attributes its later
successful typed submission without treating the rejected command as work.

The live TTY renderer now returns the cursor to the first erased panel row after
clearing it. Repeated refreshes, permanent milestones, input suspension, and
final cleanup therefore reuse the cleared rows instead of leaving blank regions
in scrollback. The event journal, non-TTY output, and explicit append-only log
mode are unchanged. The evidence, submission bridge, and progress modules pass
192 tests; the adjacent execution, dynamic-runner, and CLI modules pass 257
tests. The clean canonical release gate, immutable release identity, and
credential-free install and update lifecycle subsequently passed as recorded in
the current-release section above.

## Current v0.3.0 Implementation

The `v0.3.0` release preserves the actual Docker permission, unavailable-daemon,
timeout, malformed-response, or bounded unknown cause when managed staging fails
before any sandbox image mutation. That path performs only attributable temporary
cleanup and no longer attempts or reports image rollback. General Review
corrections now authorize the interdependent assessment, finding, and verdict
paths together, allowing the model to restore one valid semantic state without
deleting unresolved blocking evidence.

Interactive terminals now show one bounded, colored per-Agent progress panel,
update repeated observations in place, and retain state-changing milestones in
scrollback. Display mode, color, and visibility are configurable; redirected or
incapable terminals remain deterministic plain logs, and the explicit detailed
log mode preserves the append-only audit view. Initial task input and natural
Planning answers now use one shared multiline editor with cursor navigation,
cross-line deletion, Unicode and resize handling, retained text after validation,
and unchanged Ctrl-C, EOF, secret, and non-TTY contracts.

Focused verification passes 252 affected installer, Review, terminal, CLI,
configuration, and runtime-control tests; 240 Planning and documentation-role
tests; and 27 bootstrap, installer, and release-documentation checks. Bounded
subprocess cleanup now starts each TERM and KILL grace period after the first
discovered owned set receives its exact pidfd signal, preserving the configured
time for state transition, subreaper adoption, and zombie reaping. Production
signal handling, provider deadlines, and the default five-second termination
grace remain unchanged.

The final clean canonical gate passed doctor, formatting, lint, and all 1,823
tests in 1,901.51 seconds. All four stages recorded complete cleanup, zero OOM
deltas, and no process, lease, container, volume, or private-temporary residual.
The exact-tag hosted gate passed 1,820 tests with three explicit environment
skips, retained equivalent cleanup evidence, and published the immutable Release
and its sole identity manifest. Published fresh-install and upgrade lifecycles
then passed without provider calls. Those lifecycle results establish release,
installation, migration, rollback, export, uninstall, and cleanup behavior; they
do not establish a complete provider-backed Planning-to-delivery journey.

The current development head supervises each provider smoke check with a Linux
child-subreaper, inherited opaque ownership marker, UID and PID/start-time
revalidation, and pidfd signalling. A timeout now terminates a detached
session leader and reports the 180-second boundary; a command that exits while
leaving a descendant also fails after exact cleanup. Non-zero provider results
prefer a nested JSON code, status, and bounded message, remove the configured
environment credential, strip control characters, and never reflect
unstructured stdout or stderr.

Six exact stable Gemini Flash routes supplement the pinned OpenClaw catalog:
Gemini 3.1 Flash Lite, 3.5 Flash Lite, and 3.5 through 3.8 Flash. Every route
uses the native Google Generative AI transport, an environment credential
reference, the provider-documented 1,048,576-token context and 65,536-token
output limits, tool support, and a shared `medium` thinking setting for provider
smoke, Planning, and dynamic Agents. No model alias or unlisted Google route is
inferred.

The affected runtime, configuration, CLI, and subprocess suite passes 157 tests.
One local six-profile policy configuration validated and saved all routes with
no credential value. Free-tier provider smoke succeeded for 3.1 Flash Lite,
3.5 Flash Lite, and 3.5 through 3.7 Flash. The first 3.8 attempt returned a
structured HTTP 503 availability error; a later bounded retry succeeded.

A fresh `v0.2.20` candidate journey then completed install, fixed-revision
upgrade, both startup paths, six-route configuration, provider checking, task
input interruption, and all 41 admission checks. Its fourth Planning turn
exposed a fixed-length task-correction schema whose `items: false` tail marker
was copied as four boolean task values. The Controller rejected those values and
the journey stopped before approval or execution. The current correction schema
retains every positional task shape, ID, owner, and exact length while also
rendering the permitted object shapes through `items`; the same projection is
applied defensively to any bounded closed tuple used as a correction value.
Planning, correction, and production submission-bridge coverage passes all 260
focused tests, including preservation of a reachable closed tail and removal of
the misleading marker from the complete correction prompt.

A second fresh candidate journey on exact `19b642c4f463b48e5a09f9e46ba5f918de30af35`
again passed install, fixed-revision upgrade, both startup paths, free-tier
provider checking, task-input interruption, six-route configuration, and all 41
admission checks. Planning asked and accepted one `target_users` clarification.
Its next proposal cited a delivery decision from the operational-expectations
dimension, then encoded the whole object correction as a JSON string while
retaining the same incompatible decision ID. The Controller rejected the value
before approval or execution; both provider invocations completed at confirmed
zero price, with no quota, 429, 503, timeout, or paid-tier use.

The current development head now compiles wrong-authority or wrong-category
Planner recommendation links into Controller-owned candidates that preserve the
dimension and bind only existing decisions from its required category. The model
selects a short handle instead of rebuilding the object. At the shared correction
boundary, a free-form value encoded one extra time is decoded only when its exact
target schema exclusively requires an object or array; duplicate keys,
non-standard constants, oversized values, wrong roots, and JSON-looking literal
strings remain unnormalized. All 407 Planning, correction, dynamic-runner,
dynamic-prompting, workflow, and submission-bridge focused tests pass.

A third fresh candidate journey on exact `e37bb68c3ef0840dcbb802cf978a4250035590d0`
passed the same install, upgrade, startup, provider, routing, interruption, and
41-check admission boundaries. Its first Planning proposal had 26 independently
targeted schema failures, with string and structured fields represented as
booleans. The resulting leaf-by-leaf correction prompt was about 329 KiB; the
next typed submission contained one null record without a slot handle, so the
Controller rejected it before approval or execution. Both invocations completed
at confirmed zero price, with no quota, 429, 503, timeout, or paid-tier use.

The current development head treats more than eight schema-failing fields as one
`/proposal` correction only when every failure and target is model-owned inside
an object-valued proposal. It preserves `kind`, binds one exact proposal schema,
and retains full validation and strict issue-set improvement. Replaying the
captured failure reduces the correction section to about 99 KiB and one slot.
All 408 Planning, correction, dynamic-runner, dynamic-prompting, workflow, and
submission-bridge focused tests pass.

A fourth fresh candidate journey on exact `fb5cb7f4ba6a2d71fe42437c251fb8435cb0fdab`
passed installation, upgrade, both startup paths, a 3.5 Flash provider check,
six-route configuration, input interruption, and all 41 admission checks. Its
first Planning invocation observed a terminal provider response and continued
finalization progress, but OpenClaw then exited with status 1 before SAT captured
provider, usage, or submission evidence. The upstream cause remains unknown; no
quota, 429, 503, timeout, or paid-tier use was observed. Because 3.5 Flash Lite
remained an available free route, this single-route evidence gap did not require
changing the candidate before the next journey.

A later fresh journey on exact `1680ec43855a51a776d9ed05ed2776161fb8e626`
passed public `v0.2.19` installation, fixed-revision upgrade, both startup
paths, Gemini 3.5 Flash provider checking, five free Flash policy routes, input
interruption, 38 admission checks, and Planning preflight. Its first Planning
call submitted a typed target-users question. The second call exposed an
attributable provider stream, terminal response, and three finalization-progress
observations before OpenClaw exited with status 1. The prior adapter reduced
that state to `process_failed` without provider, usage, tool, or submission
evidence. No quota, HTTP 429/503, timeout, or paid-tier use occurred; the fresh
account and all owned resources were removed after evidence capture. This
failure is the production case covered by the current nonzero-terminal recovery.

The fifth fresh journey used 3.5 Flash Lite for Planning and crossed that point.
Ten typed, zero-price Planning calls completed one user-owned target-users
question and monotonically narrowed schema and relation defects. The final
correction had no available Agent-autonomy decision, so its exact replacement
schema admitted an empty atomic assumptions array. The model submitted that
value twice, but normalization retained the previous canonical
`assumption_decision_ids` index and the cardinality invariant eventually failed
closed before approval or execution.

The current development head now sends an empty model-facing assumptions array
through the same atomic compiler as non-empty `{statement, decision_id}` records,
clearing both backward-readable compatibility arrays together. The archived
failure shape and the existing non-empty relation pass focused regression; all
409 Planning, correction, dynamic-runner, dynamic-prompting, workflow, and
submission-bridge tests, all 33 release/documentation checks, and Ruff pass.

Managed staging now pins an existing mutable sandbox-image tag under a unique
temporary local rollback reference before the target installer may move it. A
failed stage restores the predecessor from that exact reference and retires only
the attributable candidate; successful activation releases the temporary
reference before normal predecessor cleanup. When staging and rollback both
fail, the user receives both bounded install-owned causes instead of one masking
message. All 81 managed-install, install-script, release-impact, and
documentation-role tests pass. A real Docker rehearsal with two distinct
owned images restored the predecessor, removed the failed candidate and
temporary reference, and preserved the pre-existing rollback-reference
inventory. Exact clean revision
`8a8f2fea83e01c88ee9e99acc2be3127f81a26e1` then passed doctor, formatting,
lint, and all 1,725 tests with complete stage cleanup, no residual process,
container, volume, lease, or private temporary directory, and no cgroup or
kernel OOM delta. The revision was pushed to `main`. A subsequent fresh
non-root public-bootstrap run began with a different SAT-labelled image at the
mutable product reference, installed and verified exact stable `v0.2.19`,
exported state during full uninstall, removed both launchers and the managed
application, restored the stable image, removed the predecessor and temporary
rollback reference, and completed disposable-account cleanup. The `v0.2.20`
tag, GitHub Release, and published-release lifecycle remain pending.

A subsequent fresh DeepSeek candidate journey completed public stable install,
fixed-revision upgrade, both startup paths, provider configuration and checking,
input interruption, all task-admission checks, approved Planning, and the first
implementation commit. Its Integration Agent then inspected that exact commit,
ran attributable setup, runtime, test, lint, and lock checks, and submitted all
assigned task IDs with a clean unchanged workspace because the upstream result
already contained the required integration files. The Controller incorrectly
reported that the Implementation Agent made no change and stopped before quality
Agents or delivery. The current runtime now permits only this evidence-backed
downstream Integration no-op as an explicit zero-length `WorkResult`; unchanged
implementation, unresolved, ungrounded, unsettled, rejected, dirty, and broken-
ancestry paths still fail closed. Real-Git runner and full workflow regressions
continue through Testing, Review, iteration aggregation, and the final report.

On hosts with limited memory or PID headroom, startup diagnostics now derive a
run-scoped concurrency ceiling from the same retained Linux/cgroup capacity
snapshot used for readiness. The effective value can only lower the saved user
maximum, is shown before Planning, and is frozen into the approved TeamPlan;
unknown capacity does not guess a smaller value and the saved configuration is
not rewritten. Product, CLI, self-check, and scheduling coverage passes 133
focused tests.

A fresh exact-candidate DeepSeek journey verified that a roughly 1 GiB host
visibly lowered the saved maximum from two Agents to one on both launches. It
also exposed a Planning session-identity defect: three semantic corrections in
separate dialogue cycles all reused generation 2 because each coordinator
invocation reset its local attempt counter. The eighth Planning call then exited
zero without a new attributable current turn or usage; SAT correctly rejected
the submission and refused an unaccounted retry under the task-wide USD limit.
The upstream reason for that missing turn remains unknown. Semantic-correction
generations now derive from the next persisted Planning-turn sequence, making
them distinct across dialogue, revision, and evidence-recovery cycles while
leaving the base dialogue session and fail-closed budget contract unchanged.
The cross-cycle regression and adjacent Planning, execution, dynamic-runner,
workflow, and typed-submission suites pass 453 tests; a fresh provider-backed
journey and the final release gate remain pending.

The prior development batch closed four correction and lifecycle defects found by
the 2026-09-14 review and by replaying archived Planning failures.

Record identity in a correction response schema now has exactly one owner. A
collection whose identity is immutable pins every existing record to its current
ID, and the Review-task-scope and writer-criterion-coverage projections re-apply
that binding when they rebuild task slots instead of replacing it. Order between
the two projections no longer changes the contract, and a slot-count mismatch
fails closed. Replaying the archived split-criterion failure against the same
builder confirms the separate `2b62dbe` identity fix already binds every
ProductDefinition `criterion_ids` slot to the criteria the proposal declares;
that replay is now a regression instead of an untested path.

A Planning invocation that produces no attributable typed submission is treated
as its own class. Nothing is accepted, the failed turn is persisted in full with
its execution status, usage and cost, the session stays in the dialogue rather
than terminating, and the identical request is reissued at most once. Assistant
text still never substitutes for a typed submission, and a submission that
exists but is rejected is never reissued.

State-layout inspection keeps one owner for deciding whether a layout is safe to
consume, while each caller projects its own remediation. Startup asks the user to
make a state root usable; uninstall asks the user to make the existing one
removable and no longer suggests selecting another state root.

Clean implementation revision `e0e446da65e4d71444f7470736b94c9f0544abf4` passed
the canonical gate with all 1,686 tests in 874.96 seconds. The report records
four stages at exit 0, 307,957,760 bytes aggregate peak RSS, no kernel OOM
delta, complete cleanup coverage for every stage, and no residual stage process,
process lease, sandbox container, volume, or private test directory.

That correction and lifecycle batch shipped as `v0.2.19` over `v0.2.18` after
exact release-head validation and published lifecycle verification.

The `v0.2.18` execution adapter classifies a missing typed submission as
`upstream_incomplete` when the exact attributable turn contains tool calls and
ends either on a paired tool result or on a later assistant response. Assistant
text remains non-authoritative. A write-capable Agent can use the existing
controlled continuation only after workspace identity, ancestry, approved path
scope, and a previously unseen content-sensitive state are verified; it retains
the same task, workspace, session, route, permission, budget, and deadline.
Zero-tool prose, unverifiable evidence, no progress, repeated state, exhausted
authority, and user stop remain bounded failures. Execution, dynamic-runner,
artifact, dynamic-workflow, and workflow modules pass all 256 focused tests at
implementation revision `853c4d8fec2ba4fb62d7fc2c26ea97fc7ce52da6`.
The package, lock, and change-impact ledger identify `v0.2.18` as a compatible
patch over `v0.2.17`; exact local and hosted release-head validation, immutable
publication, and the published stable lifecycle all pass at release revision
`388aca442fa5bb38c4866c8fb1796947f82c5de6`. A fresh complete provider-backed
journey remains pending.

The Controller released in `v0.2.16` accumulates every verified Review across
completed iterations. Since the current artifact contract has no explicit resolution
signal for non-blocking findings, completed, failed, and user-cancelled terminal
reports now retain every such description and collapse only exact repeats. A
later Review omission cannot erase an observed residual issue. Fixed and
adaptive workflow regressions reproduce both omission and repeated-report
cases; all 118 artifact and workflow tests pass. Clean implementation revision
`d2a5601e1d74b025b32756cdfdc1c2208f79f488` passed the canonical gate with all
1,661 tests in 636.49 seconds. The report records 402,006,016 bytes aggregate
peak RSS, zero cgroup/kernel OOM delta, complete cleanup coverage, and no
residual stage process, process lease, sandbox container, volume, or private
test directory. The package, lock, and change-impact ledger identify `v0.2.16`
as a compatible patch over `v0.2.15`; its exact release-head validation, hosted
publication, and published upgrade passed as recorded above.

Every generated Python delivery must commit a bounded portable root `uv.lock`.
The runtime image supplies an immutable `sat-project-lock` writer backed by one
frozen public-registry cache containing both metadata and installable
distributions; it clears inherited `UV_*` configuration and refreshes or checks
the portable lock offline. Installation and repeat-start preflight execute a
real restricted self-test of that path. The exact-command gate checks lock
consistency before setup, shares the same writable cache across setup/test/start,
then checks after each command and rejects every committed-file change and every
new file outside the committed ignore policy. Missing or ignored locks,
inconsistent or non-portable
locks, setup-time lock drift, unignored generated artifacts, inherited private
index configuration, and restricted no-network runtime behavior have direct
regression coverage.

The current implementation also records the active predecessor and candidate
sandbox-image references independently during managed staging. A changed-image
reference now retains the tagged direct predecessor lineage required by the
rollback application while reclaiming only exact detached legacy roots carrying
the old reference. Legacy same-reference handoffs retain their existing cleanup
behavior and schema-one records remain readable. Managed lifecycle and related
CLI, install, uninstall, update, and release checks passed all 179 affected
tests. Clean implementation revision
`d8d338bbac5842261de16a55da5d9f0611615968` passed the canonical gate with all
1,658 tests in 642.22 seconds. Its report records 373,215,232 bytes aggregate
peak RSS, zero cgroup and kernel OOM delta, complete cleanup coverage, and no
residual stage process, process lease, sandbox container, volume, or private
test directory. The package, lock, and change-impact ledger identify `v0.2.15`
as a compatible patch over `v0.2.14`; exact release-head validation, hosted
publication, published upgrade, and a fresh complete provider-backed journey
remain pending.

The implementation released as `v0.2.12` made the last Planning correction
complete and constrained specialist/dependency topology repair as a compatible
patch over immutable `v0.2.11`. Its focused and clean gates, exact-tag hosted
publication, published upgrade, and fresh provider-backed Planning convergence
passed. A later journey exposed the pre-activation warning addressed by
`v0.2.13`.

The implementation released as `v0.2.8` is frozen at the exact revision above.
Its Planning correction checks, canonical gate, exact-tag publication,
fixed-revision development rehearsal, and genuine published-stable upgrade
passed. The subsequent provider-backed ordinary-user journey reached the
generated project's deterministic exact-command gate and exposed the current
interactive-start input-ownership defect.

The implementation released as `v0.2.7` is frozen at the exact revision above.
Its no-progress process-poll checks, canonical gate, exact-tag hosted
publication, fixed-revision development rehearsal, and genuine
published-stable upgrade passed. The subsequent provider-backed ordinary-user
journey reached Planning corrections and exposed the current overly broad
ProductDefinition reference-correction authority.

The implementation released as `v0.2.6` is frozen at the exact revision above.
Its Planning ownership checks, canonical gate, exact-tag hosted publication,
fixed-revision development rehearsal, and genuine published-stable upgrade
passed. The first provider-backed ordinary-user journey reached dynamic
implementation and live controls, then exposed the current background-process
liveness defect.

The implementation released as `v0.2.5` is frozen at the exact revision above.
Its focused checks, canonical gate, exact-tag hosted publication, fixed-revision
development rehearsal, and genuine published-stable upgrade passed. The first
provider-backed ordinary-user journey then exposed the current Planning defect
after task admission and before execution.

The implementation released as `v0.2.4` is frozen at the exact revision above.
That release is a compatible patch over `v0.2.3`. It adds an exact reviewed preset
for the official `deepseek/deepseek-v4-flash` onboarding route and keeps its
credential authority in SAT's isolated OpenClaw auth store. The preset owns the
provider/native identity, transport, endpoint, context and output limits,
reasoning, tool, and streaming facts. Other provider/model pairs and custom
endpoints retain the explicit tool-support requirement. The focused checks,
pinned OpenClaw local inspection, canonical gates, exact-tag publication,
fixed-revision development rehearsal, and genuine published-stable upgrade all
passed. A fresh task-admission rerun on the affected WSL device remains the
validation boundary for the original first-run failure.

The included `v0.2.3` changes add a read-only, code-owned
state-layout inspection shared
by product startup, state creation, managed installation, and uninstallation.
Startup now checks the root marker, every known category, ownership, access, and
unknown top-level entries before changing permissions or reading provider
configuration. An ownership failure reports the exact path and UIDs with a
recovery action; an operating-system error that occurs after inspection is
wrapped in the same product boundary instead of leaking a raw exception.

Stable managed targets now use the verified release manifest to reject known
persisted-schema incompatibility before cloning the candidate, installing its
runtime, or building its image. Diagnostics are bounded and direct preserved
state outside the active state root. The installed candidate remains the final
compatibility authority, and development targets retain that candidate-owned
check. Product, self-check, CLI, managed-install, uninstall, and schema tests
pass all 180 affected checks. The package, lock, and change-impact ledger
identify `v0.2.3` over the `v0.2.2` baseline. The local and hosted canonical
gates, exact-tag publication, fixed-revision development rehearsal, and genuine
published-stable upgrade all passed. A fresh rerun on the affected WSL device
remains the evidence boundary for its historical ownership mismatch and manual
state recovery.

The `v0.2.1` hosted failure observed a CLI exit of 130, a cancelled Planning
session, and an interrupted turn, but the persisted invocation lifecycle was
missing. A SIGINT could arrive after the child process lease was durably
published but before lease acquisition returned to the executor. That interval
previously bypassed the executor's user-interrupt result path. The current
implementation includes lease acquisition in that path and releases only the
lease whose full observed child identity matches the launched process. The
controlled regression fails on the old implementation and passes ten
consecutive runs on the current implementation; all 86 execution and process
lifecycle module tests also pass. The fixed source passed the clean local and
hosted release gates described above before `v0.2.2` was published.

The `v0.2.2` release also contains these compatible fixes, first assembled in
the burned `v0.2.1` candidate:

- Finalization recovery sums attributable assistant usage across the current
  invocation. Missing counters, compaction, and observed sanitizer omissions remain unknown;
  conflicting model attribution is rejected, and the shared ledger prevents a
  subsequent call after recovered cost exhausts the task ceiling.
- Git snapshots accept every positive integer iteration. Ordinary product work
  can reach a fourth or later revision, while controlled-evaluation limits stay
  with their own budget policy.
- Writer artifacts follow the scheduler's completion order, so a valid Agent
  DAG has the same acceptance outcome regardless of array declaration order.
  Disconnected, forked, duplicate, and mismatched commit chains remain invalid.
- Planning validates retained user decisions against the verified persisted
  revision history through further model revisions, structured edits, overview,
  restart, and approval. Controller-generated edit descriptions do not become
  user authority.
- Configuration recovery storage is reserved before changing either authority.
  Failed preparation and interrupted saves or directory moves restore previous
  state; a secondary recovery failure preserves the original provider backup.
- Canonical validation uses a dedicated stage adopter to reap short-lived
  orphaned children while a stage runs. The command retains its own exit status;
  launcher failures cannot supply a missing command outcome, and unrelated
  callers retain ownership of their child-process wait status.

Affected integration checks exercise real Git, production controllers,
configuration files, process finalization, and the shared ledger. Model content
and quality-command outcomes remain explicit external fixtures. The clean
`v0.2.1` candidate `b90b2634caa8b4a5360ed6bdc9b41d9c81ca4a2e` passed the local
canonical gate with **1,608 tests in 835.36 seconds**; doctor, formatting, lint,
and cleanup also passed. The test-stage adopter reaped 389 children during
execution. No terminal process, temporary-directory, container, volume, or
lease residual remained. The report is
`artifacts/generated/full-gate/20260912T080910.783407Z-dc3cbe89b35c/report.json`.

An earlier candidate's gate had seven Git process-creation failures; an observed
retry was stopped after identifying accumulating adopted zombies. Those outcomes
remain separate from this successful gate and motivated the validation-supervisor
fix. This batch made no provider calls; the prior release evidence below does not
establish a fresh provider-backed journey for this checkout.

The previous `v0.2.0` impact ledger classified task-derived Agent specialization
and the unified multi-transport model-profile surface as minor changes, while
the Planning, runtime attribution, accounting, managed-resource, portable-lock,
and release-workflow changes were compatible fixes. The local candidate gate,
immutable exact-tag workflow, and genuine `v0.1.2`-to-`v0.2.0` managed upgrade
have now satisfied the publication boundary.

Configuration schema v10 now embeds a versioned, secret-free
`ModelRuntimeProfile` in every saved `ModelProfile`. The frozen profile and its
canonical digest carry provider-native identity, explicit OpenClaw transport,
native/remote/local endpoint policy, credential reference, model limits, and
capabilities through route resolution, TeamPlan persistence, startup
inspection, provider smoke, Planning, dynamic execution, telemetry validation,
prompts, and reports. Reviewed presets cover the earlier DeepSeek Vision route,
`deepseek/deepseek-flash`, and the pinned official plugin's exact
`deepseek/deepseek-v4-flash` onboarding route; custom endpoints no longer
require a per-model Python branch or handwritten run configuration.

First use, interactive reconfiguration, and non-interactive reconfiguration
now share validate-before-save semantics. Interactive OpenClaw changes are made
in a staged private state copy; every resulting route must pass schema,
endpoint, credential, capability, and local catalog/auth checks before the SAT
configuration and provider state are committed. Failure or cancellation
restores the previous files exactly and cannot print success. Custom OpenAI
Completions, OpenAI Responses, Anthropic Messages, and local Ollama profiles
compile through the same materializer and pass the pinned OpenClaw config
validator. Focused live checks with DeepSeek V4.1 Flash passed exact-route
provider smoke, Planning single-tool submission, and a dynamic tool loop with
no fallback. These checks establish the provider integration boundary but are
not a managed ordinary-user product campaign.

The `v0.2.0` release-candidate canonical gate completed doctor, format, lint, all
1,517 tests, and cleanup in 1,038.70 seconds. Its report recorded 316,174,336
bytes peak aggregate RSS, no new cgroup or kernel OOM event, and no residual
stage processes, process leases, sandbox containers, volumes, or private test
tree.

The current role-specialization implementation extends the line anchored by
`c36090d543fe6bdd230c88d8de7684082733b1bb` through code candidate
`0dc0af603a8368dfe61fdbf22362755025dfaf89`. Planning schema v19 keeps
schema-v2 through schema-v18 evidence readable and separates three authorities:
Controller-required recovery, Planner-selected task clarification, and user
approval. A focused question can change only its declared decision scope;
unknown user-question provenance returns to dialogue instead of model-owned
proposal repair. Complete unwrapped question or proposal bodies are framed by
the Controller only when the current semantic schema identifies exactly one
complete body; partial, mixed, and ambiguous objects still fail closed.

The default Planning overview now emphasizes task-specific product definition,
decisions, assumptions, risks, Agent rationale, and approval boundaries. Fixed
execution-profile constraints, lifecycle safeguards, and Review definitions
remain available through a lossless `f` display toggle. That toggle changes no
proposal, model-call count, or user requirement; `r` remains reserved for a
deliberate semantic requirement revision.

TeamPlan schema v3 now separates each task-specific Agent label and
responsibility from a versioned specialization and a compatible executable
capability. The Controller-owned catalog binds professional purpose, prompt
module, permission ceiling, typed output, handoff boundary, and acceptance
claim class. Security and end-user-experience assessment are the first two
non-generic specializations: both retain the read-only Review capability while
producing distinct grounded `SecurityAssessment` and `ExperienceAssessment`
artifacts. Unknown or incompatible combinations, permission or output
escalation, missing packaged prompt modules, mismatched execution identity, and
criterion-scope drift fail before Agent creation or artifact persistence.

The same identity now crosses prompt composition, execution requests and
telemetry, schema-v13 execution records, scheduler records, schema-v6 progress
events, typed handoffs, lifecycle transitions, and the approved-plan self-check.
A production-shaped Planning matrix verifies that an ordinary task, an
untrusted-input security task, and an interactive-workflow task compile to
different Agent sets, dependency waves, typed outputs, verifier assignments,
acceptance strategies, and user overviews without deriving authority from a
free-form role label.

The subsequent planning-authority changes make the typed Agent graph the sole
owner of approved team topology and use request-local semantic correction slot
identities without relaxing exact Controller authority. Runtime
`expected_paths` are now explicitly advisory planning forecasts: they do not
create required deliverables, write authority, or an exception to repository
ignore policy. A production-shaped regression preserves the Journey 49 path
set, including an ignored `uv.lock`, and verifies that the developer prompt
cannot turn that forecast into a lockfile obligation.

Managed upgrades now label SAT-owned sandbox images, retain only the active
application and its direct predecessor, restore the prior image tag on failed
activation, and remove a superseded image only when its exact identity is
attributable, untagged, and unused by every container. Unknown version entries,
legacy images, and foreign Docker resources fail closed or remain untouched.
Consecutive-upgrade, idempotent-reactivation, rollback, and foreign-resource
regressions exercise those ownership boundaries.

The exact clean development head
`059960ed489c6b964a4b24b87b077357afca4dae` passed one canonical
`make check` with 1,508 tests in 1,094.44 seconds. The report records
315,928,576 bytes aggregate peak RSS, zero cgroup/kernel OOM delta, and no
residual stage processes, process leases, sandbox containers, volumes, or
gate-private test directory.

A managed non-root bare-`sat` run on that exact head then completed the full
ordinary interface with `deepseek/deepseek-flash`. Task admission passed 26 of
26 checks and approved-plan admission passed 37 of 37. Planning reached a valid
four-Agent overview through Controller-owned targeted correction without a
user revision. The user then deliberately revised that valid plan to give the
interactive no-argument workflow to a distinct Experience assessment Reviewer;
the approved five-Agent graph included implementation, deterministic testing,
general review, security assessment, and experience assessment.

All five deterministic project gates passed. The three independent Reviewers
submitted grounded typed artifacts on the same immutable output commit, all 13
acceptance criteria passed, and the Controller delivered commit
`4144b53f37a1d20c293be4016df40af2b03424c9`. Independent execution of the
documented setup, no-argument start, test, and offline commands passed, including
57 project tests, while the delivered Git workspace remained clean. All 15
provider calls settled with no active, unpriced, or missing-usage call; no Agent
used an arbitrary semantic-work timeout, and run-owned runtime resources were
cleaned.

The preceding managed non-root bare-`sat` Planning acceptance on `d94850e`
used `deepseek/deepseek-v4-flash-vision-exp`, asked only the overwrite-policy
decision deliberately left open by the request, preserved its typed
provenance, and switched compact/expanded/compact views without a model call or
revision. It created no runtime Agent or delivery. Those unchanged Planning
authority facts remain evidence for this head, but they do not substitute for
the pending specialization acceptance.

## Implementation Evidence History

The following sections preserve chronological implementation evidence. Phrases
such as “pending” describe the candidate named in that paragraph and are not the
current release-state summary; the current facts are the section above and
[`Not Yet Available or Completed`](#not-yet-available-or-completed).

ProductDefinition validation now preserves per-invariant authority. A proposal
that lacks a material user-owned decision returns to one dimension-specific
question instead of attempting to model-correct that decision inside a proposal.
Mixed user- and model-owned defects are revalidated together after the answer.
Relational correction grants the shared `decisions` slot together with affected
ProductDefinition dimensions only when no eligible decision exists. Its
machine-readable value schemas now also bind requirement, criterion, decision,
Agent, and task references to canonical IDs whenever the defining collection is
immutable for that correction; jointly corrected identity collections remain
open so atomic definition-and-reference repair stays reachable. Active
profile-owned criterion IDs participate in the same vocabulary. Production-
coordinator regressions cover a real two-question, five-dimension decision
repair, multiple independent Review-boundary siblings, two ProductDefinition
dimensions carrying an unknown requirement reference, exact Agent and criterion
relations, and no-progress termination. All 155 Planning tests and an adjacent
236-test schema/submission/release-documentation group pass. The current
session-state path also persists terminally invalid initial Planning as failed
and restores the preceding proposal after a completed invalid revision.

Planning and dynamic Agent configuration now explicitly disable the optional
memory plugin slot. Four focused checks, including the pinned runtime's actual
slot resolver, verify that the previous implicit `memory-core` selection is
removed without disabling the submission plugin. No startup-performance or
provider-backed acceptance claim follows from those checks. Initialization now
has a separate activity-versus-readiness contract described below; the shared
provider-backed product acceptance remains pending.

Review candidate selection now distinguishes invalid model submissions from
Controller faults. Mixed catalog-backed selections can retain verified bindings
on an unpublished copy and request only a strictly smaller pending set; no
progress or invalid identity stops without guessing. The same convergence rule
now recognizes a strict reduction of independent typed sibling issues, narrows the
next request to the unresolved remainder, and stops if that remainder repeats.
Full grounding still gates artifact publication. Response, dynamic-runner,
prompting, and Review correction checks pass 187 tests; the updated candidate's
clean canonical gate and accepted ordinary delivery remain outstanding.

The durable user-stop check now covers every dynamic invocation admission,
including semantic correction and provider fallback, rather than only upstream
continuation. Deterministic between-call cancel/interrupt regressions cover the
gap where there is no live process for the executor to interrupt.

Invocation-state persistence, detailed labels, and heartbeats now use the current
Controller checkpoint instead of reconstructing activity from historical event
kinds. Completed-tool counts are displayed from the numeric observation, separately
from the last verified action. RunEvent v5 retains canonical v2–v4 readers;
scheduler decisions remain distinct from invocation state. Focused regression and
read-only historical replay cover this correction. Final clean-candidate and fresh
user validation remain outstanding.

Planning and targeted correction now share object-only transport capture before
Controller semantic validation. Exact slot binding and the no-improvement gate
remain enforced; invalid values no longer depend on an upstream schema retry
loop. Planning v13 also retains terminal invocation lifecycle evidence and a
failed session state while reading v2–v12 without rewriting historical records.
Main-thread interruption and adapter exceptions settle and persist before CLI
propagation; unknown usage remains unknown. Focused regressions cover real CLI
SIGINT, repeated interrupts, unknown settlement, and historical canonical identity.
An intermediate combined gate passed 1,316 tests. Subsequent regressions also
cover long error summaries without dropping the original execution error.
Clean-candidate, pinned transport, and installed-user acceptance remain pending.

Cost accounting now includes separate cache-read and cache-write usage and
frozen prices across Planning, execution, correction, progress, and reports.
Missing prices require confirmation before a task; missing usage remains
unknown. Partial usage preserves known buckets and settles atomically, and an
explicit model override cannot inherit another model's prices. Configuration v9
and Planning v11 retain historical readers without rewriting old evidence.
Settled Planning turns now retain the authoritative per-call record, including
cache usage and frozen prices, even when Planning fails before runtime creation.
Invalid model-authored direct-input quotes receive source-leaf-only correction;
missing user authority still cannot be supplied by a model. Focused Planning,
schema, release, and CLI checks passed 213 tests. The combined full gate passed
1,259 tests with no owned residuals or new OOM events; clean-candidate, fresh
delivery, and release validation remain pending.
Writer instructions also identify the exact input commit as the revision base;
the independent ancestry rejection remains unchanged.

Fresh installation exposed a mismatch between the pinned Node version and a
moving upstream CLI installer. Setup now owns dependency-only installation from
checksum-verified Node/OpenClaw artifacts, with one shared version manifest and
no Gateway lifecycle delegation. The fixed OpenClaw version starts successfully
with Node 24.19.0 in contributor setup. Focused installation regressions cover
checksum failures, launcher preservation, private npm configuration, and ambient
service-state/preload isolation. The same selectors are neutralized by the
runtime subprocess boundary, with cross-consumer regression coverage;
complete fresh managed validation remains pending.

The implementation gate for `5acbae8` passed all 1,201 tests before commit,
with no new OOM events or residual owned resources. Immutable installation
placement now uses complete provenance while retaining compatible legacy paths;
observer publication shares the lifecycle stop authority, so late session
history cannot reopen working progress. The current activity projection also
recognizes literal directory wrappers without changing direct-executable
evidence. Final candidate and fresh-user validation remain outstanding.

The preceding implementation gate passed all 1,190 tests. Planning schema v9 now
requires a material, user-attributable primary workflow even for a throwaway
prototype, without removing legacy schema readability or requiring a fixed
questionnaire. The shared session reader also accepts complete leading shell
comments before an executable without mistaking literal hashes inside command
names for comments. Captured-session replay retains all paired tool results and
the terminal submission; this is offline extraction evidence, not a fresh
successful delivery. Clean frozen gates and ordinary-user validation remain
required before release.

The canonical repository `make check` entry now runs doctor, formatting, lint,
and pytest through one diagnostic supervisor. It streams original stage output
and writes an atomic ignored report containing exact commands, Git and time
identity, per-stage terminal state, current/last pytest node, aggregate process
and memory observations, typed cgroup/kernel availability, and post-run
SAT-owned resource inventories. Signal, hang, detached-child, unavailable
observer, and abandoned-started-record paths have regression coverage. The
schema-v4 candidate also assigns pytest a short exact-owned temporary leaf,
passes an explicit basetemp, and records its base/filesystem identity and
cleanup on success, nonzero exit, timeout, signal, or abandoned-report
recovery. Recovery defers while the exact
stage owner is live and refuses unowned paths; focused regression covers
foreign-directory preservation and cleanup failure as a gate failure. One
complete diagnostic gate on the exact candidate content passed all 1,176 tests
without changing shared `/tmp` capacity and with no private temporary residual;
this is dirty-tree evidence rather than a clean-revision triple gate. The first
complete supervised gate passed all 997 tests with no residual stage
process, process lease, sandbox container, or volume; it observed no new cgroup
or kernel OOM event. The two historical incomplete suites still have an
evidence-bounded unknown cause rather than a retroactively invented diagnosis.
A historical clean batch gate at `65518d9` passed all 1,014 tests after a
deadline-crossing cleanup race was reproduced, fixed, and regression-tested;
it likewise recorded no new OOM evidence or residual owned resources.
A later diagnostic gate over the ProductDefinition candidate passed all 1,030
tests. Its schema-v2 report recorded exact inherited stage ownership,
process-local Linux subreaper attribution, successful boundary restoration,
zero residual stage processes or SAT resources, and no new cgroup or kernel OOM
event. That run was intentionally a dirty-tree implementation gate; final
closure still requires repeated clean gates on one frozen revision.
The subsequent typed-Planning candidate passed all 1,044 tests through the same
supervisor. Its dirty-tree diagnostic report recorded a 224,661,504-byte
aggregate peak RSS, no new cgroup or kernel OOM event, and zero residual stage
processes, process leases, sandbox containers, or volumes. Repeated clean gates
on the eventual frozen revision remain required.
A historical dirty-tree diagnostic gate passed all **1,099 tests** in 326.84
seconds. It recorded a 289,669,120-byte aggregate peak RSS, peak process/thread
counts of 5/19, no new cgroup or kernel OOM event, and zero residual stage
processes, process leases, sandbox containers, or volumes. It covers exact
correction-slot schemas, constraint-aware convergence, Controller-issued Review
evidence handles, safe tool-action classification, and repeated checkpoint
projection suppression. A clean committed gate and fresh provider journey are
still required before those corrections are considered closed.
The Review candidate-eligibility revision at `5f76776` passed three consecutive
clean canonical gates of **1,140 tests** each, exact captured-response replay,
and focused frozen-revision matrices. Its schema-v3 reports recorded a peak
aggregate RSS of 347,787,264 bytes, peak process/thread counts of 5/18, no new
cgroup or kernel OOM event, and zero residual stage processes, process leases,
sandbox containers, or volumes. It covers whole-chain rejection of correction
candidates whose exact fragment would also match an ineligible failed result.
A fresh provider journey remains required before that fix is considered closed.

The current development head implements configuration schema v8 model metadata
with attributable price/context sources, task-scoped route snapshots, one
explicit per-task USD authorization and optional deadline prompt, and separate
controlled-evaluation versus ordinary-user resource authority. Product Planning
and execution no longer use a fixed wall-clock work limit. SAT resolves a
finite attributable initialization sequence before a provider/model-aware
renewable inactivity lease. Planning and runtime Agents now share `launched`,
`initializing`, `provider_wait`, `tool_active`, `stopping`,
`collecting_evidence`, and `stopped` phases with distinct stop authorities and
typed cleanup evidence. SAT observes private stream and attributable tool
lifecycle without persisting their content, visibly separates suspected stall,
grace, recovery, degraded observation, and terminal stall, and does not publish
terminal failure before process reaping and evidence collection. RunEvent v4
adds a Controller-owned approved-task/checkpoint/Git/gate/Review/cost snapshot
while preserving canonical v2-v3 reads. Artifact schema v10 and Planning schema
v12 preserve pinned-runtime rejection diagnostics independently of paired tool
execution, retaining Artifact v2-v9 and Planning v2-v11 reads. Lifecycle schema
v3 records a content-free pre-invocation initialization baseline while retaining
lifecycle v1-v2 reads. Lifecycle v4 adds bounded identity-bound wait snapshots at
initialization suspicion and stall boundaries, carried by Artifact v11 and
Planning v14. Lifecycle v5 separates pre-readiness inactivity from readiness:
exact invocation-owned CPU, fault, I/O, or complete process-topology changes
renew only the initialization inactivity lease and are counted in Artifact v12
and Planning v15; only a new current turn or invocation-private stream starts
provider waiting. Historical schemas remain canonically readable. Focused
live-child checks cover active initialization, truly inactive stall, capture,
recovery, and exact cleanup; this contract does not retroactively identify one
unique cause for every historical OpenClaw startup stall. Artifact schema v8 added a distinct
nonterminal `deferred` outcome for validated async `exec`/`process` starts.
Deferred calls remain visible in the audit chain but cannot
prove a satisfied Review claim or replace a later terminal process result or
typed submission. Focused regression and exact replay of the captured
provider-backed failure pass on the current implementation; a committed clean
full gate and fresh delivery remain pending. One
controller-priced ledger now covers Planning through terminal execution,
standard progress exposes spend and remaining authorization, and final reports
include attributable per-call cost evidence. Terminal JSON, Markdown, and the
ledger are prepared and published as one rollback-capable bundle before the
controller transition. The bare product entry now persists a typed
task-admission report before Planning and an approved-plan report before source,
workspace, or runtime-Agent creation. The first report includes full SAT
release/source identity, local/schema/model/task/budget facts, and exactly one
foreground managed-channel observation; the second covers every approved
route, Agent authority, runtime policy, sandbox, source, and delivery boundary.
Every new completed, failed, or cancelled terminal report embeds the same typed
SAT release/source/install/channel/artifact/schema identity and commits it with
the Markdown view and model-spend ledger through the rollback-capable terminal
bundle. Startup diagnostics now discover the tightest Linux/cgroup memory and
PID headroom, compare it visibly with the policy ceiling without treating that
ceiling as a minimum, make the existing disk guard blocking as declared, and
read-only inventory existing containers proven to mount SAT-owned state. The
approved-plan restricted container probe remains the readiness authority for
actual sandbox execution. These machine checks do not cap Agent count, call
count, or total model-work time. A failed task-admission or approved-plan check
can be repaired and rechecked in the same foreground task. Changed input
digests append an immutable revision that refreshes only the invalidated result
and its transitive dependents; an unchanged retry creates no duplicate
evidence. Rechecks now load the verified latest report from disk, so a new CLI
process can preserve unchanged evidence while refreshing model/configuration
changes and dynamic plan-graph additions, removals, or redefinitions. Every
live SAT-launched OpenClaw child also has a private PID/start-time/process-group
lease. A real controller-kill test proves that a new process can distinguish
and reclaim the exact orphan while holding a Linux pidfd across signalling;
active owners and PID-reused processes are not signalled, and sandbox recovery
additionally requires the exact leased session under SAT-owned state. The
execution adapter now distinguishes an invocation that ends after a paired tool
result but before terminal typed submission. For write-capable work, the
Controller continues the same task/session only after verifying repository
identity, ancestry, approved scope, and a new content-sensitive workspace state;
the original task budget, optional deadline, and user stop authority remain in
force. An unchanged state stops, and completion still requires normal commit,
gates, independent Review, delivery, and cleanup. User-interrupted or cancelled
work is intentionally not resumed automatically. Fresh provider/device cost,
liveness, continuation, self-check, process cleanup, and managed-release
validation remain incomplete; the corresponding issues are not closed by
offline evidence.

The response compiler now distinguishes transport, schema, contextual, and
evidence-grounding failures. Each collected ProductDefinition invariant retains
its own authority and failure class; if a rejected proposal exposes a missing
target-user, primary-workflow, or delivery-maturity decision, the Coordinator
requests a question-only typed response constrained to that one dimension instead
of entering proposal-field correction. Mixed model/user diagnostics expose no
replacement path and the model-owned siblings are revalidated against the next
complete proposal. It deterministically
removes only schema-forbidden fields that cannot carry controller/evidence
authority and records each normalization. Profile-criterion ID collisions now
remove only redundant echoes; a task-specific relation needed for requirement
coverage receives a deterministic non-reserved ID while the canonical profile
binding and all model-owned verification relationships remain intact. This
deconfliction no longer depends on an already-correct writer-task binding: a
missing binding remains a separately reported `/proposal/tasks` defect instead
of authorizing the controller to erase the requirement relation. An
otherwise well-formed `DECISION_` token and its assumption reference are
canonicalized to uppercase only when the resulting identity is unique, so a
presentation-only case mismatch cannot spend another model call while a real
collision still fails strict validation. A
Reviewer response is also compiled against the exact TaskBrief-owned boundary
scope before nested boundary content is validated. Extra checks outside that
scope are removed with an explicit normalization instead of creating new
acceptance obligations or model correction calls; checks inside the approved
scope retain strict completeness, uniqueness, and evidence-grounding rules.
Review grounding failures now identify every independently invalid selector in
one pass with a stable invariant, criterion subject, and exact `observable`
leaf. Targeted correction can therefore replace those selector strings together
while preserving every assessment, finding, verdict, and summary field that
already passed validation. The Controller now derives a bounded catalog of exact
eligible same-chain evidence fragments for each invalid leaf; the model selects
an opaque handle and the Controller records the exact-byte binding. Candidate
construction and final grounding now share the same whole-chain match policy, so
a successful source fragment is not offered when that selector would also match
an ineligible failed tool result or failed deterministic command. No eligible
candidate means no random correction call. It never asks the model to regenerate
the whole assessment array or hand-type previously observed output. A remaining
targetable model-owned failure creates a
diagnostic-v2 invariant ID,
structured affected-entity subjects, precise model-owned JSON-pointer authority,
and SHA-bound correction-request evidence. The Controller binds those paths to
opaque response-bound handles and the model submits order-independent
`{slot_handle, replacement_value}` records, so it cannot submit the response
digest, select a parent container, or otherwise widen correction authority. The
correction contract discriminates each record by its constant handle and exact
value schema. A submission may contain a nonempty subset of unique authorized
handles; the Controller applies it only to a copy, preserves omitted slots, and
revalidates the complete response. Empty, duplicate, unknown, cross-response, and
legacy positional submissions are rejected before mutation. Constraint
convergence distinguishes genuine
refinement from regression: a more specific invariant exposed after a coarse
schema failure may continue, while falling back to a coarser type/shape error in
the same authority slot is not improvement merely because its fingerprint
changed. The
controller retains every unrelated field, freezes a writer's verified Git
result, and records each request and outcome. Planning relational validation no
longer infers identity or correction scope from human error prose; unclassified
relations fail closed. Product
Planning and dynamic execution continue only after measurable
improvement within the task budget, while the fixed evaluation surface retains
its explicit zero-or-one cap. Transport, unlocated, repeated, invalid-submission,
and non-improving failures stop without a full-response retry. Fresh
provider-backed Planning correction evidence now exists. Exact offline replay of
the latest distinct writer-coverage then verifier-authority failure now yields
different fingerprints and narrows correction from four proposal containers to
`tasks`, followed by the exact criterion `verification_agent_ids`; a corrected
Reviewer provider run remains pending.

Live progress now derives tool-action text from an allow-list at session capture
time. Planning and runtime Agents may show bounded labels such as `testing
quality checks (pytest)`, but unknown names, full commands, arguments, output,
paths, and secrets do not enter the activity object or renderer. Every real
event remains persisted and summarized; identical checkpoint and budget blocks
are suppressed within an invocation to avoid repeated terminal walls. Provider-
backed validation of the richer projection remains pending.

A fresh installed run at `4095086` exposed the final v1 authority defect before
this replacement: six lowercase-suffix decision IDs were the only invalid
fields, but the correction response replaced the parent `decisions` container
and was safely rejected as unauthorized. The current compiler instead resolves
that collision-free token presentation deterministically; genuine multi-field
correction accepts only an exact-length value vector whose paths remain
Controller-owned. Adaptive Planning now uses invocation-bound typed submission
for initial questions/proposals and for correction values. Planning schema v7
persists the submitted semantic payload and content-free binding separately
from non-authoritative assistant text while retaining canonical v2-v6 reads.
The submission tool now exposes one explicit outer `artifact` argument and the
plugin unwraps it exactly once before semantic validation. Protocol-v2 evidence binds
the outer tool-arguments digest separately from the inner semantic-object digest, so
direct objects, double envelopes, and argument/file mismatches fail closed while
historical v1 evidence remains readable. Planning places its permissive object-only
capture schema inside that argument and separately retains the exact semantic-schema
digest. Consequently, every syntactically structured Planning payload reaches strict
semantic validation and deterministic normalization instead of being discarded by a
redundant pre-Controller schema gate or mistaken for a semantic `artifact` field.
Submission capture distinguishes schema- or tool-rejected transport attempts from
successful semantic submissions. Failed attempts retain tool evidence but no semantic
authority and may precede one final successful file-bound call; multiple successes,
work after success, all-failed sequences, and binding mismatches still fail closed.
The reviewed DeepSeek compatibility route now distinguishes two provider request
contracts. Bootstrap Planning, whose only semantic action is terminal submission,
forces the exact named submission function rather than relying on a prompt.
Dynamic-team runtimes preserve the provider's normal completion choice: an Agent
may use capability tools and then call the bound submission tool, while a missing
or invalid terminal submission is rejected by the Controller. This prevents a
required-any-tool setting from turning repeated evidence activity into an endless
tool loop. Model inspection, provider smoke, and legacy text-compatibility
configurations retain their original request behavior. Pinned-OpenClaw loopbacks
observe both outbound choices, the work-tool-to-submission order, accepted v2
envelopes, terminal one-request behavior, and exact sandbox cleanup. The
model-facing request projection excludes
Controller-only run,
destination, route, authorization, and timestamp metadata, preventing execution-layer
redaction from breaking exact prompt/session attribution. Dynamic Agent submission
schemas remain exact inside the same explicit envelope.
The execution adapter now also preserves a uniquely bound typed submission when
OpenClaw has written a complete terminal current-turn record but later stalls
while finalizing its result envelope. It recovers the attributable provider,
model, split token/cache usage, sanitized tool chain, and semantic payload through
the normal validation and handoff path while retaining the actual wrapper signal,
`response_finalization_stall` lifecycle, and cleanup evidence. Production-shaped
tests cover stale, incomplete, nonterminal, and unattributed negative cases plus
dynamic Review grounding, artifact persistence, handoff, and unique ledger
settlement. The captured provider failure replays through the new reader; one
clean candidate gate and shared ordinary-user accepted delivery remain pending.
Live Review progress now keeps attributable tool activity separate from grounded
criterion coverage. Coverage remains explicitly `unverified` until a typed,
Controller-grounded assessment is accepted; repeating a probe cannot be presented
as improvement. Cost progress likewise reports settled estimates separately from
active calls whose provider usage is not yet available, and never labels arithmetic
headroom as confirmed remaining budget while such a call is active.
The fixed compatibility workflow now converts an unexpected executor exception into
an attributable failed execution before settling its call reservation. Missing token
telemetry and cost remain unknown, the original exception survives in the terminal
report, and parallel siblings finish before the ledger is frozen. The corresponding
throwing-executor regression, atomic initialization fixture, and complete 1054-test
diagnostic gate pass without new OOM events or stage residuals.
Targeted correction now projects the exact target value schema into every ordered
slot. ProductDefinition validation accepts Controller-supplied profile criterion IDs,
maps clear natural-language maturity evidence without requiring enum wording in the
user's sentence, reports independent invalid dimensions together, and makes each
dimension the atomic correction unit. This prevents a valid proposal relationship
from being rejected as unknown and prevents fail-fast cross-field validation from
spending one model call per coupled scalar.
An actual pinned-OpenClaw loopback invocation loaded the plugin in the read-only
bootstrap runtime, captured the final tool call, accepted the bound payload, and
removed its exact sandbox container. A new provider-backed product journey is
still required before the correction issues can close.

A fresh installed run at `54b0275` reached approved Planning, a clean writer
commit, and five passing deterministic gates. Its Reviewer returned ten
criterion assessments, but also supplied four boundary checks for each of five
profile criteria whose frozen TaskBrief scope was empty. Two reused fragments
failed nested schema validation and caused an unnecessary correction call; that
call returned no JSON, so SAT withheld delivery. Exact offline replay of the
preserved first response against the current compiler now validates all ten
assessments in one pass and records removal of the five unapproved arrays. This
proves the captured regression path offline, not a corrected provider journey;
the issue remains open until a fresh run crosses Review without that call.

At that correction milestone, doctor, formatter, lint, and the complete
**987-test offline suite** passed with
diagnostic-v2 and slot-bound semantic correction v2, including the exact
captured two-invariant Planning sequence, a three-call correction regression,
collision-safe decision-token canonicalization, and multi-field Controller path
binding. The managed stable resolver
also distinguishes an unpublished/inaccessible HTTP 404, refuses silent dev
fallback, and explains the explicit dev-channel choice. Managed staging removes
the caller's active `VIRTUAL_ENV` only from the install child so the candidate
owns its `.venv` without changing the caller or other environment. Provider-backed
validation of the corrected journey remains pending. An explicit dev ref now
retargets an existing dev installation through the same staged activation and
rollback transaction; an unchanged resolved revision remains a no-op. Managed
installation now claims the immutable final release path before creating the
Python environment or isolated OpenClaw runtime, so path-bound entry points are
never relocated after verification. Activation executes the final `sat`
launcher and restores the prior link, installation record, and transaction-created
launchers if that probe fails. Persisted-state activation is now evaluated by a
versioned read-only entry point from the verified candidate rather than by the
older active process. The same result uses the candidate lifecycle enum to
report active run identities. The transaction engine cross-checks its revision
and full schema registry before activation and fails closed on invalid output. Managed
bare-product entry also holds a shared kernel lifecycle lease through terminal
cleanup while activation requires the exclusive form; offline tests cover
version-skew authority, incompatible and malformed results, stale loaded
releases, active-task exclusion, rollback, and kernel release after a process
crash. A fresh non-root managed installation retained its original run lock and
persisted state while the public fixed-ref bootstrap recovered an updater that
predated candidate-owned compatibility; the exact-ref retry was idempotent and
the prior release remained available. In a separate live concurrency check, a
bare `sat` held the shared lease at the task-input prompt before run creation;
both channel activation and the public bootstrap were rejected clearly, and
the same bootstrap succeeded after the foreground process exited. These checks
validate candidate recovery and task/activation exclusion without a daemon or
manual lock cleanup.

Saved secret-free model profiles no longer depend on the accidental presence of
a persistent OpenClaw configuration file. Ordinary startup revalidates the exact
saved route through the isolated, versioned materialization path; first-run
dialogue appears only when no SAT profile exists, and repair or explicit
reconfiguration keeps the saved model ahead of an unrelated discovered default.
A fresh non-root managed installation at `841ce15` completed initial model,
context, and price setup without a persistent OpenClaw configuration, exited at
task input, and then reached task input again on a second bare `sat` invocation
without repeating provider, model, price, or smoke-test questions. Both exits
were user cancellations before run creation, so this validates repeated startup
and isolation but not provider generation or task delivery.

## Phase 1 Result

The function-specialized vertical slice is implemented and has produced a
qualifying version-two provider-backed evaluation that reached `completed`.
One controller-verified implementation commit passed every deterministic gate,
all ten acceptance criteria, and independent review, with complete model,
token, hash, and Git-boundary evidence.

Two consecutive provider-backed replays of the same engine revision have also
reached `completed`, each through the bounded evidence-driven revision loop.
The earlier benchmark defect was corrected and versioned as
`task_manager_phase1_v2`; version-one evaluation records remain exploratory
evidence and must not be mixed with version-two comparisons.

Managed installation, startup diagnostics, secret-free first-run model setup,
execution-profile confirmation, user-owned success conditions, automatic run
preparation, controller-backed progress, accepted-result delivery, and safe
uninstallation are implemented and covered offline. Provider credential
creation remains in SAT's isolated OpenClaw-owned boundary. Repeated
comparative experiments and cross-participant human-factors generalization
remain pending. The latter is a separate optional study, not a requirement for
the current operator to perform a user-side black-box rubric.

The exact acceptance procedure is in
[`docs/phase1-runbook.md`](docs/phase1-runbook.md). Offline scripted executions
prove controller behavior, not model quality.

## Adaptive Orchestration Progress

The Phase 3A compatibility path is implemented. `TeamPlan`, `AgentSpec`,
and `ModelRoutePlan` are executable versioned contracts rather than roadmap-only
names. The current function-specialized workflow compiles its fixed evaluation
fixture into that contract, persists `team-plan.json`, and gives the frozen plan
to run control, artifact validation, timeout resolution, and verification
dispatch. There is no second fixed-role run-control path.

Validation rejects invalid dependencies, write-scope conflicts, incompatible
permissions, missing independent quality coverage, unauthorized model routes,
and concurrency above the user-approved host setting before Agent creation.
Controlled evaluation additionally validates frozen Agent/call/iteration
limits; ordinary product plans do not inherit those limits. Recovery verifies
the exact TaskBrief binding, TeamPlan digest, fixed manifest version, fixed team
digest, resolved time authority, and cross-file run metadata.

`RunEvent` is also an executable, append-only contract. Every current workflow
progress update is persisted with a contiguous sequence, lifecycle revision,
phase, Agent identity when applicable, visibility class, and predecessor
digest. `run.json` atomically anchors the latest event, so recovery detects
missing, reordered, modified, or extra events, including a changed tail.
The dynamic scheduler and runner now project every approved Agent through
queued, ready, running, provider-waiting, provider activity, tool lifecycle,
suspected stall, grace recovery, degraded observation, bounded-repair,
completed, failed, or blocked transitions. Events include safe activity,
dependencies, capability, stage, approved model, attempt and duration where
applicable, invocation evidence, and aggregate budget snapshots. Compact,
standard, and detailed
filtering consumes the same event contract. Configuration schema v7 persists
the selected visibility together with secret-free model profiles and route
policy without changing renderer semantics, and bare `sat` applies the selected
visibility to the product renderer; standard remains the default.

`ControlCommand` and its controller-owned revision store define and preserve
the request, target, controller-assigned mailbox sequence, safe application
boundary, status, consequence, plan or lifecycle result, and provider-cost
caveat for guide, correct, pause, resume, interrupt, and cancel. The normal CLI
now exposes those commands through a foreground slash-command palette. The
dynamic scheduler polls the mailbox, stops new launches at cooperative
boundaries, applies guidance to the next invocation, and requests best-effort
termination only for exact SAT-owned OpenClaw process groups. Every receipt and
resolution is correlated to its command revision and digest in `RunEvent`.
Interrupt admission reports only invocations whose lifecycle atomically accepts
the user stop reason; a still-live process already stopping for another reason
is neither counted nor relabeled, and a repeated request is not reaccepted.

The Phase 3B Planning engine is also implemented. A versioned `PlanningRequest`
proves explicit model-work authorization before the first invocation. The
read-only bootstrap Planner may return either one decision-value question with
two or three suggestions and a custom-answer path, or one complete proposal.
Planning schema v5 records each question's decision category and owner, missing
evidence, material consequences, alternatives, and the exact ProductDefinition
dimensions it can resolve. The controller enforces the fixed responsibility
matrix before showing a question. Tightly coupled dimensions can share one
explicitly declared question and proposal decision; undeclared dimensions are
not authorized, and statement values are projected from the exact transcript
answer instead of model-authored user wording. Current proposals must carry
an attributable ProductDefinition covering target users, primary workflow,
delivery maturity, usability, operations, and delivery expectations. Each
dimension records whether it came from explicit input, a resolved question, a
Planner recommendation, or a justified not-material judgment, plus real
downstream requirement, criterion, or decision references.
Strict proposal validation covers stable requirement IDs, explicit non-goals,
decision-owned assumptions, acceptance criteria and their requirement links,
Agent work assignments, task ownership, dynamic Agent responsibilities,
dependencies, workspace scopes, independent quality coverage, user-approved
concurrency, proposed iterations, per-Agent workload classes, the configured
model route, and resource authority before the proposal is shown. Product plans
resolve every per-Agent wall-clock value to zero and record
`provider_activity`; workload-to-timeout mapping and Review scope floors remain
only in controlled evaluation.

The ordinary-user interaction supports free-form answers, natural-language
replacement revisions, safe edits to maximum concurrency, iteration count, and
Agent model profile, cancellation, a complete plain-language overview, and
explicit approval. `PlanningStore` persists the authorized request,
hash-chained model turns including rejected response evidence, immutable
proposal revisions, exact approval digests, and the controller's per-Agent
time-authority resolutions. Product resolutions record zero with a
provider-activity source; controlled evaluation retains workload, policy
envelope, final seconds, and any Review scope evidence. Planning turns also
retain content-free provider-liveness evidence. Approval promotes the
validated preview into an authorized confirmed `TaskBrief`, adaptive
implementation plan, and executable `TeamPlan`.
The resulting `ApprovedPlanningResult` revalidates those exact digests and
cross-plan bindings at its execution boundary, so mutated approved inputs
cannot be substituted before runtime.
The bootstrap Planner still cannot create an Agent or advance run state.

The approval overview now begins with the approved audience, killer workflow,
delivery maturity, quality and delivery expectations, non-goals, and their
architecture, team, cost, and delivery effects. It then separates user
decisions, Planning recommendations, Agent/Controller autonomy, and
non-negotiable Controller policy. It renders requirements, visible assumptions, each
requirement-to-criterion-to-writer-to-independent-verifier path, and every
Agent's inputs, expected output, and handoff, followed by risks and the failure
and delivery boundary. Current proposals fail closed when any trace is missing
or a writer claims its own independent verification. Planning schema v2 through
v4 remain readable without changing their canonical serialization; safe edits
retain the legacy version rather than relabeling it. Ambiguous-task provider
behavior and user-side comprehension are not established by these offline
contracts. That black-box interaction may be performed by the current test
operator; it does not require a separate human participant.

Planning criterion ownership is now explicit. The response schema requires
every Planner-defined criterion to have implementation-task coverage and
accepts only stable criterion-ID syntax. Before that context-free coverage
check, the policy-aware response boundary removes and audits definition echoes
whose exact IDs belong to the active controller profile; task bindings remain,
and no model-authored profile text or Review boundary becomes authoritative.
The preview separately allows those bindings, rejects unknown IDs, materializes
only canonical profile definitions, and preserves valid bindings in the
approved implementation plan. Dynamic prompt validation rechecks those task
references against the exact controller-materialized TaskBrief rather than
trusting a standalone model response.

The first Phase 3C runtime boundary is also implemented. Dynamic execution
requests and telemetry use an approved run-scoped Agent ID and capability;
fixed-role identity remains compatibility metadata only for the existing
evaluation workflow. Capability-specific templates compile the exact approved
responsibility, assigned tasks, dependencies, permission profile, model route,
and time authority into minimum-context prompts. Response parsing rejects
mismatched Agent identity, capability, session, task ownership, model, or time
authority.

Artifact schema v2 removes fixed-role identity from durable handoffs and
execution records. Every Agent-produced iteration artifact is stored beneath
an Agent-ID namespace, so multiple approved Agents can produce the same typed
artifact without path collisions. Artifact-store validation binds each
producer, handoff endpoint, stage, and recorded capability back to the exact
run-scoped `AgentSpec`. The fixed evaluation adapter now writes through this
same generic evidence boundary.

Controller-owned artifact assembly is now shared by fixed and task-defined
teams. It combines validated Agent semantics with the exact approved AgentSpec
identity, controller-verified Git snapshot, deterministic command evidence,
immutable quality commit, and assigned review scope. Dynamic `IterationRecord`
aggregation accepts task-proportional teams rather than one hard-coded
Developer/Tester/Reviewer tuple: it requires a chained result from every
approved writer, consistent deterministic evidence from every approved Tester
(or one controller report when the team intentionally has none), and evidence
from every approved Reviewer. Split review scopes must exactly cover manual
criteria, and finding identities must be unique across the iteration.

Run configuration materialization emits only the approved AgentSpecs, clones
their least-privilege capability profiles, and binds every Agent to the
verified workspace and its exact authorized route set. Strict evaluation
disables fallback; policy routing can expose only the primary and ordered
fallbacks already frozen for that Agent. Exact-label sandbox cleanup can derive
all owned session identities from those AgentSpecs. Adaptive
validation excludes the bootstrap Planning and Clarification capabilities from
the runtime team, requires every writer to own work, and allows a small task to
use one writer plus one independent quality Agent instead of imposing a hidden
Tester/Reviewer pair. Every quality Agent must depend on every writer path, so
verification cannot start against an intermediate commit. Separate quality
Agents may be parallel peers or form an explicit handoff chain on that same
immutable commit. Fixed evaluation fixtures retain their explicit dual-quality
topology.

The Phase 3C dynamic runner is now implemented behind the general DAG
scheduler. The scheduler remains the only owner of readiness, launch order,
bounded concurrency, and shared-Git writer exclusion. The runner invokes only
the supplied approved `AgentSpec`, preserves its exact authorized model set and
time authority, applies improvement-gated targeted semantic correction,
accounts for every call in one thread-safe aggregate ledger, and persists raw
output plus telemetry before a
post-call budget rejection stops the schedule. Agents cannot create another
Agent, change dependencies, reorder work, or extend time authority.

Each dynamic writer starts from the controller's current clean commit, leaves
a clean descendant commit, and is rejected for changes outside its approved
workspace scope. Read-only quality Agents must leave the same immutable commit
and clean tree. Deterministic gates execute exactly once per iteration even
when Tester and Reviewer Agents run concurrently. Their reports share the same
controller-owned evidence and final commit; when a justified small team has no
Tester, the controller persists the deterministic TestReport itself. Dynamic
source-to-target and terminal handoffs are write-once and include attributable
phase and execution evidence. Offline integration tests exercise real Git
commits, parallel quality, semantic correction, missing telemetry, budget
exhaustion, read-only mutation, and write-scope violations.

The Phase 3C adaptive lifecycle coordinator is now implemented. It consumes
only an exact `ApprovedPlanningResult`, creates the generic `RunController`,
and lets `DagScheduler` remain the sole authority for readiness, order, and
parallel launch. When the first quality Agent becomes ready, a synchronous
controller checkpoint verifies the complete writer commit chain, records one
aggregate Git snapshot, and enters `VERIFYING` before that Agent starts. Tests
and reviews therefore cannot run first and have lifecycle evidence filled in
afterward.

The coordinator aggregates every approved writer, Tester (or the controller's
deterministic report), and Reviewer into one `IterationRecord`; resolves
accept, revise, terminal failure, iteration exhaustion, and repeated blockers;
and produces one integrity-checked JSON and shared Markdown final report.
Revision feedback contains only controller-derived blocking findings and test
reasons, is bound to the previous output commit as the next iteration's start,
and is distinct from each downstream Agent's current snapshot commit. Offline
end-to-end tests cover one-pass acceptance, evidence-driven revision followed
by acceptance, unchanged-blocker termination, pre-snapshot Agent failure, and
a valid Tester-only quality topology.

The Planning interaction and adaptive execution backend are now activated
atomically by bare `sat`. The normal launcher creates one read-only bootstrap
runtime, preserves Planning evidence separately, presents the validated
overview, prepares an execution source only after approval, materializes only
the approved run-scoped Agents, executes the dynamic lifecycle, cleans both
bootstrap and execution sandboxes, and uses the existing accepted-result
delivery boundary. It cannot approve a dynamic plan and silently execute the
old fixed team. During execution it accepts live visibility changes,
prospective guidance, cooperative pause/resume, best-effort Agent interruption,
terminal cancellation, and requirement correction. A correction preserves the
superseded run and opens a fresh Planning overview with a new run ID; it never
mutates approved evidence in place. Safe concurrent writers beyond the current
serialized Git chain, durable process-restart resume, and a secondary-process
control client remain pending.

Phase 3E adds a single canonical source for secret-free model profiles and
deterministic routing. The controller resolves Agent edit, stage override,
capability override, default-profile support, then eligible-profile priority;
the bootstrap Planner may describe capability needs but cannot authorize a
model. The Planning overview exposes every primary route, selection reason,
known pricing, and approved fallback before user approval. TeamPlan validation,
run configuration, prompts, response telemetry, budget feasibility, and
runtime preflight all bind those exact assignments. Only an attributable
provider failure or typed provider stall can advance under an explicitly
approved provider-failure switch condition; the failed call, liveness evidence,
and switch remain evidence, while semantic repair stays a separate mechanism.
Offline routing and dynamic-runner tests cover authorized and refused switches.
A provider-backed run using two planned routes remains pending.

## Product Readiness Boundary

The primary CLI now implements the adaptive Product Journey in code. A normal
user runs `sat`; SAT checks the device, guides model configuration, asks what
to build, confirms the installed Python execution profile and destination,
obtains explicit Planning authorization, conducts bounded clarification,
shows the complete task-defined team and controller limits, supports revision
or safe edits, and creates an execution run only after exact approval. It then
executes the approved TeamPlan and delivers only an accepted clean Git result
with project-specific commands.

The normal first-use path starts with one strict selected model profile; an
advanced user can configure multiple capability-authorized profiles and policy
routing before Planning. Subsequent launches now revalidate the exact saved
profile even when no persistent OpenClaw config file exists; a temporary
secret-free effective config supplies supported catalog compatibility, while
missing or unsafe persistent config cannot silently select a different
discovered default. The product path also uses a user-configurable
controller progress renderer. Its active foreground control channel and model
routing are implemented and offline verified, including cancellation and
correction reports, one-shot guidance, safe pause/resume checkpoints, live
visibility changes, process interruption, deterministic route resolution,
explicit provider-failure switching, event correlation, and exact-owned
cleanup.

This is not yet release-stable evidence. Two earlier WSL rehearsals completed
managed installation or update, startup diagnostics, isolated provider setup,
request confirmation, internal run materialization, and the Planner stage.
Both then reached a stopped Developer sandbox before any workspace tool could
run, so no project was delivered. The second run was correctly classified as
`dependency_unavailable` instead of a source-code failure.

The first fresh installed rehearsal of the activated Adaptive Planning path
used the public installer, a new non-root account, bare `sat`, and
`deepseek/deepseek-v4-flash-vision-exp`. Device checks, configuration,
provider smoke, ordinary request capture, authorization, and Planning preflight
all passed. The Planner returned a task-defined proposal, but one expected
directory was written as `tests/` and one Agent repeated the destination name
as its workspace scope. The bounded repair corrected the permission scope but
retained the harmless trailing slash, so strict validation stopped before any
execution Agent or project workspace was created.

SAT now canonicalizes only safe, unambiguous Planning path presentation and
infers a missing response discriminator only when exactly one response body
makes it certain. Raw output remains immutable, every normalized field is
recorded, destination-shaped workspace scopes remain rejected, and unsafe or
ambiguous values still follow strict repair/failure policy. The complete
offline suite covers the observed response without consuming a repair call.

A second fresh installed provider-backed run confirmed that fix: its Planning
response passed on the first call, the user approved one task-defined
Implementation Agent followed by one independent Review Agent, and the writer
completed a clean seven-file commit. The controller verified the commit and
entered verification, but stopped before the Review provider call because the
1,172-character immutable WorkResult summary exceeded a separate 1,000-
character downstream prompt field. No destination was delivered.

The controller now keeps complete Agent summaries in immutable artifacts while
deriving deterministic bounded projections for scheduler status and downstream
prompt context. A truncated projection names the original character count and
SHA-256 and states that full text remains in artifact evidence. A regression
with a 4,045-character WorkResult completes both downstream quality Agents,
while the stored WorkResult remains unchanged.

A third fresh installed adaptive run confirmed the complete controller path at
that revision. Planning used one bounded repair, the user approved an
Implementation Agent followed by an independent Review Agent, and two
evidence-driven iterations completed. The first review correctly requested a
README revision; the second accepted all controller evidence. SAT recorded
11/11 criteria passed, delivered a clean 14-file commit, reported exact project
commands, and removed all four run-scoped containers. The delivered setup and
17 project tests passed, as did duplicate grouping, exclusion, minimum-size,
and nested symlink fixtures.

Independent post-delivery acceptance nevertheless found two product defects.
Selecting a directory symlink as the top-level scan root followed its target,
contradicting the unqualified request and README claim that symlinks were never
followed. The model-authored tests covered nested symlinks but not that entry
boundary, and independent Review did not challenge it. The documented setup
also generated an untracked `uv.lock`, leaving first-use Git state unexplained.
The controller's completed result is therefore retained as failed product
acceptance rather than promoted to demonstration evidence.

The shared quality prompts now require Planning, implementation, and independent
Review to cover every relevant entry boundary of an unqualified prohibition or
safety guarantee and to reject one concrete counterexample. The Python product
contract also requires the root setup environment to be ignored and `uv.lock`
to be either a bounded regular file in the accepted snapshot or explicitly
ignored. These are task-independent corrections; no duplicate-finder-specific
gate was added. A fresh installed run must confirm both changes before the
adaptive journey is called successful.

A fourth fresh-account adaptive rehearsal tested those changes through the
public installer and bare `sat` with the same duplicate-finder request. Planning
needed one bounded proposal repair, then the user approved one Implementation
Agent followed by one independent Reviewer. Iteration one correctly reached the
project-contract and pytest import failures. The Reviewer requested revision
for those deterministic defects but supplied only a summary assertion of all
14 criteria; it did not challenge the top-level symlink boundary or the
implementation's singleton hash groups.

The second Developer invocation committed a corrective revision and returned
one complete valid semantic object after explanatory text containing JSON argv
arrays. The old raw-object normalizer incorrectly treated those arrays as a
second response candidate and spent a repair call. That repair contained
unescaped quotation marks and was invalid JSON, so the controller stopped with
`artifact_invalid`, delivered nothing, and removed all three run-scoped
containers. Independent inspection of the preserved commit also confirmed
that the root symlink and singleton-group defects remained and that the test
argv still differed from the exact generated-project contract.

The response boundary now treats non-object JSON arrays as presentation when
there is exactly one semantic object, while still rejecting every additional
object, including one nested in an array. Dynamic Review now requires an exact
criterion-by-criterion assessment set with concrete adversarial checks and
evidence; blocked assessments and blocking findings must reference the same
criterion. Review can run bounded foreground probes against read-only source
and `/tmp` fixtures in its no-network sandbox, without converting its
self-directed result into controller-owned deterministic evidence. These are
generic protocol and quality-boundary corrections. The Python profile now also
places its fixed setup and test argv in the controller-owned Planning
constraints, and both the starter guidance and implementation prompt require
the writer to preserve them while replacing only the project-specific start
placeholder.

A fifth fresh-account rehearsal used the public installer, bare `sat`, and the
same model. Planning needed one bounded repair after the first proposal gave a
quality task to the writer, then the user approved one Implementation Agent
followed by one independent Reviewer. The writer returned one valid semantic
WorkResult plus a separately visible OpenClaw tool warning. The old transport
adapter incorrectly required exactly one visible text payload and requested a
semantic repair even though only one response object existed. The repair
succeeded and deterministic verification began.

That rehearsal exposed three independent controller defects. README headings
such as `Usage` did not satisfy a validator that looked for the literal word
`start`; clean-tree pytest could not import the generated src-layout package
even though the post-setup command passed 24 tests; and the progress renderer
showed success symbols for failed gates. The Reviewer then had read-only source
and foreground execution but no coherent way to create a temporary probe
script. OpenClaw rejected its attempted inline interpreter command, and the
routine 300-second timeout expired while it was responsible for all ten
criteria. SAT delivered nothing and terminal cleanup completed.

Independent inspection also found that the preserved candidate mishandled a
top-level directory symlink, returned success for a missing path, and required
an undocumented extra operand on its nominal start command. These remain
product-specific failure evidence; the resulting corrections are generic.
SAT now aggregates all visible transport text in order while requiring exactly
one semantic response object, stops hidden lifecycle heartbeats when the exact
Agent terminates, renders gate outcomes truthfully, and shows bounded Planning
heartbeats and repair checkpoints. Review scope supplies a controller-owned
timeout floor, so 6–10 criteria resolve to at least 450 seconds and 11 or more
to 600 seconds under the current policy. The pinned quality image includes
`uv` for the exact generated commands.

The Python profile now accepts ordinary documentation headings but requires
the exact setup, direct no-extra-argument start, and test commands. It requires
both clean-tree and post-setup pytest to work, including explicit src-layout
import configuration.

The sixth fresh-account rehearsal confirmed that Planning heartbeats, bounded
repair state, completed-Agent heartbeat termination, truthful gate symbols,
the exact generated-project command contract, and criterion-scope timeout
resolution all reached the installed path. One Implementation Agent completed
in a single call, produced a clean commit, and passed all four deterministic
gates. Its downstream Reviewer was responsible for 11 criteria and still
timed out at the 450-second substantial allowance before returning a semantic
report, so SAT correctly withheld delivery and cleaned its run containers.

The Reviewer prompt had two contradictory boundaries: a general prohibition
on modifying files and a final prohibition on mutating tools both conflicted
with the middle instruction to use the write tool for `/tmp` probe scripts.
The live session never used write and instead spent tool turns on heredoc
commands that deterministic preflight correctly rejected. The prompt now says
that project source is read-only, and it asks the Reviewer to consolidate
related probes rather than repeat commands. The provider-backed
11-criterion timeout at the substantial allowance now maps 11 or more criteria
to the existing complex allowance. The separate 10-criterion timeout only
proved that routine was insufficient, so 10 remains substantial; this does not
change coding, testing, smaller Review, call-count, or total-duration budgets.

A subsequent provider-backed authority probe found that the prompt-only change
still described an impossible tool boundary. The pinned OpenClaw runtime omits
the general `write` tool whenever a sandbox filesystem root exists, so removing
`write` from the deny list cannot expose it. The Reviewer completed semantically
through foreground `exec`, but its attributable session contained 11 `exec`
calls, zero `write` calls, rejected heredoc attempts, and a false final claim
that the requested write path had been used. That probe is failure evidence,
not a successful capability verification.

The runtime image is therefore versioned to `phase1-v4` and contains the
immutable `sat-probe-write` helper. It accepts only canonical direct children
matching `/tmp/sat-review-probe-*` with `.py`, `.json`, or `.txt`, enforces line
and total-size limits, creates with mode `0600`, and rejects overwrite,
symlinks, nesting, traversal, and partial-write residue. Reviewer explicitly
denies the nonexistent general `write` tool and uses the helper through its
available foreground `exec` surface. The full 656-test suite passes. A
restricted non-root, no-network, read-only-root container probe also confirmed
root-owned helper mode `0555`, caller-owned output mode `0600`, direct Python
execution, refusal of overwrite, symlink, and `/agent` targets, read-only
project enforcement, and terminal container removal. A fresh installed
provider authority probe then exposed a separate evidence-grounding defect:
the Reviewer returned a structurally valid accepted report claiming helper,
Python, Git, and boundary checks, while its exact attributable current session
contained zero tool calls and zero tool results. The semantic parser previously
validated criterion coverage and prose but had no controller-owned link from a
claim to an actual OpenClaw tool record. That accepted-looking response is
failure evidence and does not verify the helper or Review path.

The OpenClaw execution adapter now validates the returned session ID against
SAT's isolated session index, extracts only the latest exact current-prompt
turn, pairs tool calls and results one-to-one, and persists bounded sanitized
records with invocation-local IDs, hashes, outcomes, excerpts, and transcript
provenance. Raw session JSONL is not copied into run artifacts. Dynamic Review
response schema now requires every criterion assessment to supply one or more
bounded exact observable fragments from the current invocation. The model
cannot supply or predict a controller tool ID. SAT requires every fragment to
match at least one sanitized result, binds every matching current result, and
deduplicates repeated or overlapping selectors by controller-owned ID before
persisting the assessment. For `exec`, SAT records only the
direct executable plus a hash of the complete arguments, so helper/Python/Git
paths remain inspectable without persisting possible environment-assignment
values or the full command. A captured zero-call turn therefore cannot support
an accepted claim; an absent result enters the one bounded semantic repair,
while multiple real matches remain attributable evidence and invalid session
provenance stops at the safety boundary without another provider call.

A fresh installed probe against the first ID-bearing contract confirmed the
capture boundary and exposed why semantic bodies must not carry those IDs. The
controller recorded 20 actual calls, including successful `sat-probe-write` and
direct Python results with both required markers, while the model's four exact
observable fragments were paired with four incorrect guessed `tool-00N` values.
The parser correctly rejected the response, but requiring a model to count an
evolving tool loop and predict a controller-owned presentation ID was itself a
protocol defect. The first observable-only fresh retest then captured 13 actual
calls and all five supplied fragments matched current results, including helper,
direct Python, Git, and negative-boundary evidence. The old controller still
rejected the response because the ordinary clean-Git fragment occurred in both
an initial inspection and a final check. Both matches were real; requiring the
model to manufacture unique wording was another controller-owned mapping task.
The contract now binds all current matches and deduplicates repeated or
overlapping selectors. Offline regressions cover unrelated preliminary calls,
one-to-many resolution, zero matches, repeated and overlapping selectors, and
rejection of a model-supplied ID. A repeat fresh installed provider authority
probe and complete adaptive run remain required before this journey is
demonstration ready.

The next fresh-account adaptive run passed device checks, isolated
configuration, provider smoke, ordinary request capture, first-response
Planning, overview approval, one Implementation Agent, and all deterministic
quality gates. The writer produced a clean eight-file commit and its generated
suite passed 54 tests. The independent Reviewer then made 21 attributable tool
calls. Its first semantic response was invalid only because two finding paths
were absolute. The bounded repair corrected those fields and correctly reused
the unchanged probe fragments without repeating tool calls. The former
grounding logic searched only the repair invocation, so it rejected valid
evidence from the immediately preceding attempt and withheld delivery. This is
a controller protocol defect, not evidence that a more capable model is
required.

Adaptive Reviewer grounding now carries sanitized evidence forward only within
the same Reviewer, role stage, immutable commit, and bounded semantic-repair
chain. Persisted references use `(execution_attempt, tool_call_id)` identities,
so invocation-local IDs may safely repeat across attempts without ambiguity.
The model still supplies only exact observable fragments and never predicts an
attempt or tool ID. Regressions cover successful zero-call semantic repair,
same-local-ID attempts, invalid chain order, and attempted evidence substitution
outside the current chain. Replaying the preserved live attempts through the
corrected grounding boundary accepts all 12 criterion assessments and binds 38
references to attempt one while attempt two correctly contains zero tool calls.

The same run also exposed a separate delivery-command evidence gap. A Reviewer
probe had created a virtual environment beneath one mount path and later
observed it beneath another, so that environment was not relocatable; the
controller had not independently run every exact generated-project command
from a clean post-commit copy. Runtime image `phase1-v5` now contains a locked
offline wheelhouse. A new deterministic product gate rejects tracked drift and
unsafe Git entries, copies only committed regular files into fresh disposable
scratch, and runs the exact setup, test, and start argv with the network
disabled. Its tmpfs is executable only so project-local virtual-environment
entry points can launch; source and root filesystems remain read-only and the
container remains non-root, capability-dropped, and resource-bounded. A real
restricted container verification passed setup, all 54 project tests, and
start against image ID
`sha256:9e129565b0dea409d8808dbe58570701494db5181001a5480b40bf212f0013f4`.
The complete 667-test offline suite passes. A fresh installed provider rerun
remains required before declaring the adaptive journey demonstration ready.

That fresh-account rerun passed public installation, `phase1-v5` readiness,
device checks, isolated configuration, provider smoke, request capture, and
Planning authorization. Both the initial proposal and its repair selected a
coherent `impl -> reviewer` Agent DAG, but both also restated the independent
Review stage as reviewer-owned `TASK_REVIEW`. The internal `tasks` collection
incorrectly required every owner to be a writer, so the proposal was rejected
before the overview. No execution run, runtime Agent, workspace, or destination
was created. Repeating that hidden representation constraint in a repair did
not improve the proposal.

The task contract now permits explicit work assignments for every approved
runtime Agent. Quality-owned tasks preserve testing or review focus in the
overview and the exact Agent prompt; they do not create Agents, grant write
access, alter dependencies, expand criterion scope, or add model calls. The
approved `AgentSpec` DAG remains authoritative for those controls. Every writer
must still own work that covers every proposal-owned criterion, every task
owner must be an approved Agent, and cross-Agent task dependencies must agree
with the Agent DAG. The same binding validation runs during proposal parsing,
prompt construction, and runner startup. The exact preserved repaired response
now compiles unchanged with its original `impl -> reviewer` execution waves
and no additional model call. The complete 673-test offline suite passes. A new
fresh installed provider rerun is still required.

That next fresh-account run confirmed the quality-owned task fix at the real
Planning boundary, then exposed another hidden topology constraint. Both the
initial proposal and its repair selected implementation, testing, and Review
Agents and placed Review after Testing. `PlanningProposalBody` accepted the
DAG, but `TeamPlan` silently rejected any dependency between Testing and Review
with `testing and review capabilities must remain independent`. The repair saw
only that abstract error and repeated the same chain. No overview, execution
run, runtime Agent, workspace, or destination was created. The proposal also
described test-file creation in a task owned by the read-only Testing Agent;
the text could not grant writes but the mismatch was not visible enough.

Quality sequencing now belongs to the approved Agent DAG: Testing and Review
may be peers or one may consume the other's durable handoff, provided both are
read-only and transitively downstream of every writer. The dynamic runner test
executes `builder -> tester -> reviewer` on one immutable commit, passes the
Tester summary into the Reviewer prompt, runs deterministic gates once, and
records the exact handoff. The Planner contract now assigns all project-file
creation to implementation/integration Agents. The user overview derives a
write-versus-read-only authority line from each task owner's `AgentSpec`, so
task prose cannot grant mutation authority or conceal the actual boundary. The
two archived Planning responses now compile unchanged with their proposed
three-wave DAG. The complete 674-test offline suite passes. A new fresh
installed provider rerun remains required.

The next fresh-account run passed the public one-command installer, bare `sat`,
device checks, isolated configuration, provider smoke, request capture,
Planning authorization, bounded Planning repair, overview, and approval using
the exact `deepseek/deepseek-v4-flash-vision-exp` route. The approved plan used
one Implementation Agent followed by one Reviewer. Planning classified their
workloads as `substantial` and `routine`; controller policy resolved those
labels to 1,350-second and 600-second invocation timeouts and froze
`max_concurrency=1`. The writer committed eight tracked files. Deterministic
clean-copy verification then caught a real import failure in one subprocess
test while the generated project's exact post-setup test command passed.

The Reviewer performed 19 attributable read-only tool calls and returned the
correct `revise` verdict, all 13 criterion assessments, and one blocking
finding covering the import defect. The response was rejected because several
compact JSON evidence fragments differed from pretty-printed results only in
outside-string whitespace, three exact claims referred to controller command
output rather than Reviewer tool output, and the sole finding omitted a
redundant `criterion_ids` list. Its bounded repair returned prose instead of a
semantic object, so the run correctly failed without delivery and terminal
cleanup removed both run-scoped containers.

Reviewer grounding contract `semantic_body_v3` now prefers exact fragments but
permits only RFC JSON whitespace differences outside quoted strings for keyed
JSON. It can bind same-iteration deterministic command stdout/stderr and
persists the actual command IDs alongside attempt-qualified tool IDs. When one
unscoped blocking finding uniquely explains all otherwise-uncovered blocked
criteria, the controller binds that relationship; multiple unscoped findings
remain invalid. Exact replay of the preserved first response now retains its
`revise` verdict, resolves all 13 assessments, binds 11 tool references and 3
command references, and scopes the finding to `AC_TESTS` and `AC_TESTSUITE`
without another model call. The complete 677-test offline suite passes. A
fresh installed provider rerun remains required to verify the complete
revision loop and delivery path.

The following fresh-account run reached delivery, but independent post-delivery
validation found that a user-selected root symlink was followed despite an
absolute never-follow guarantee. The Reviewer had cited a probe result that
contained the expected path fragment but ended with a traceback, failed
assertion, and `EXIT=1`; another assessment cited a tool call whose normalized
result was failed. The former fragment resolver proved that selected text was
present but did not require the matched result as a whole to support a
`satisfied` assessment. That allowed a false acceptance even though the
attributable evidence already contradicted the verdict.

Runtime image `phase1-v6` now pairs `sat-probe-write` with immutable
`sat-probe-run`. The runner validates an owner-only bounded Python probe,
executes its open file descriptor with a fixed interpreter and project working
directory, limits child time and output, and emits a terminal
`SAT_PROBE_RESULT_V1` marker. Reviewer grounding rejects a satisfied assessment
when any matched tool result failed, any matched deterministic command failed
or timed out, a legacy terminal `EXIT=N` reports non-zero, or a direct runner
marker is missing, malformed, timed out, or non-zero. Exact replay of the
preserved false-acceptance response is now rejected deterministically before
assembly. A real restricted non-root container verified runner success,
assertion failure, timeout semantics, and runtime preflight against image ID
`sha256:a20a5bdd9a07d903beb78e78b9f69cb37faf1969eedf3aba3ee4945e416f3bd2`;
the complete 693-test offline suite passes. A fresh installed provider rerun is
still required to verify the corrected revision and delivery path.

Whole-result consistency alone does not prove that an absolute requirement was
challenged at every relevant entry. Planning now makes that scope explicit:
every proposed criterion returns `review_boundaries`, and a criterion whose
description contains an unqualified prohibition or safety guarantee must list
top-level input, nested input, alias or indirection, and failure path. The user
sees these obligations in the overview, and the confirmed TaskBrief freezes
them. `semantic_body_v4` requires explicit boundary checks; a satisfied
assessment must ground every approved boundary with a distinct attributable
fragment, while a blocked assessment may stop after one grounded
counterexample. Controller validation rejects missing, duplicate, reused, or
ungrounded approved boundary claims; checks outside the TaskBrief-owned scope
are deterministically removed and recorded before nested validation. The
complete 698-test offline suite passes. A fresh
installed provider rerun remains required to verify the expanded Planning and
Review contracts at the real provider boundary.

That fresh provider run is now recorded. Public install and bare `sat` produced
and approved a task-defined `implementer -> reviewer` team. The
`semantic_body_v4` Reviewer grounded all four approved boundary classes, found
that a user-selected top-level symlink root was still followed, and correctly
requested revision despite five passing deterministic gates. The Implementer
then fixed the root guard, its contradictory regression test, and README; all
five gates and 30 tests passed again. The second Review exceeded the former
450-second allowance before returning a semantic response, so the controller
failed without delivery. This confirms the Review evidence fixes and exposes a
separate timeout-policy gap: ten criteria concealed twenty additional explicit
boundary obligations.

Review timeout resolution now counts criteria plus boundary obligations as
work units. Under the current 300-to-600-second Review envelope, that live scope
resolves to the 600-second complex allowance and remains visible before
approval; small reviews retain routine or substantial values. Failure reports
also distinguish a finding proved on an earlier commit from a changed commit
whose independent re-verification did not complete. Focused Planning and
Dynamic Workflow tests cover both regressions, and the complete check is 700
tests passed; a new fresh-account provider run is still required before
claiming end-to-end delivery success.

That fresh-account retry passed the public installer, automatic diagnostics,
first-run configuration, provider smoke, and ordinary request capture. The
initial Planning response and its bounded repair both proposed the same
two-Agent `implementation -> review` DAG, but both copied the known
`AC_DOCUMENTATION` and `AC_QUALITY` definitions from policy context. The
initial response reached context-free writer coverage first and was rejected
because only the Reviewer task referenced `AC_QUALITY`; the repair added that
binding to the writer, then reached the later profile-ownership collision and
failed before overview. No runtime Agent, execution run, workspace, or
destination was created.

The Planning response boundary now removes exact active-profile definition
echoes before proposal-owned coverage validation, records each removal beside
the immutable raw response, retains task bindings, and materializes only the
controller's canonical criteria. It does not infer an unknown ID or alter any
Agent, task, dependency, concurrency, workload, model, timeout, or approval.
Both preserved provider responses now replay to their original two-Agent DAG
without a repair; focused Planning tests cover active-policy scoping, raw
evidence, canonical materialization, and unchanged strict rejection outside
that scope, and the complete repository check passes 702 tests. A new
fresh-account provider run remains required before claiming end-to-end
delivery success.

That fresh-account run confirmed the profile-criterion normalization: after one
ordinary syntax repair, the user approved a two-Agent `impl -> reviewer` plan,
the writer produced a clean eight-file commit, and all five deterministic gates
passed. The Reviewer completed 33 `exec` calls within its controller-resolved
600-second timeout, but SAT rejected the entire session before assembling a
Review because full-command `shlex.split` continued parsing text after a Bash
`#` comment and encountered an unmatched quote in the shell-ignored suffix.
Independent validation also proved the partial product still followed a
symlink used as the user-selected root; the Reviewer had mislabeled symlinked
children inside that root as `top_level_input` and returned a false `accept`.
No delivery was created.

Executable attribution now lazily consumes only leading environment assignments
and the first executable, while preserving the complete argument digest and
paired result. The same bounded replay captures all 33 archived calls; an
unparseable executable prefix remains invalid. Review boundary identifiers now
come from one immutable controller-owned definition mapping. Planning context,
the user overview, implementation and quality prompts all state that the
user-selected primary root itself is `top_level_input` and every child inside it
is `nested_input`. Focused artifact, Planning, dynamic-prompt, and OpenClaw
session-evidence tests pass, and the complete repository check passes 705 tests.
A fresh-account provider run was still required at that point before claiming
end-to-end delivery success.

That sixteenth fresh-account run now supplies the required evidence. The public
one-command installer completed in an empty non-root home, and the operator then
used bare `sat` without a TaskBrief, benchmark, explicit run ID, source path,
team, timeout, or concurrency arguments. One Planning call proposed an
`impl -> reviewer` team with maximum concurrency two. The user approved it;
policy resolved the substantial writer to 1,350 seconds and raised Review to
600 seconds from ten criteria plus seventeen explicit boundary obligations.
The controller froze those values, and the scheduler ran the Agents serially
because the approved dependency prevented parallel launch.

The Implementer produced one clean commit through 38 attributable tool calls.
All five deterministic gates passed, including 20 clean-workspace tests and
fresh-scratch execution of the exact generated setup, test, and start commands.
The first Reviewer attempt captured 37 real tool calls and 67 attributable
session records without an integrity error. Its response required one bounded
semantic JSON repair; the zero-call repair safely reused only the same
Reviewer's verified evidence against the same immutable commit. The final
Review accepted all ten criteria with no finding, including an explicit probe
that treated a symlink supplied as the scan root as `top_level_input`.

The controller completed and delivered the project. A separate ordinary-user
black-box check then reran exact setup, test, and no-argument start commands and
tested duplicate grouping, exclusion, minimum size, invalid size, missing input,
root and nested symlinks, symlink files, and post-command Git cleanliness.
All thirteen checks passed, the delivered HEAD matched the controller's final
commit, and terminal cleanup left zero containers or volumes. This establishes
the strict single-route ordinary product journey at that revision. It did not
exercise foreground controls, two authorized routes, durable process-restart
recovery, or an independent-device demonstration.

The seventeenth fresh-account run exercised the missing foreground interaction
instead of treating controls as an operator-only test surface. The public
installer and bare `sat` used
`deepseek/deepseek-v4-flash-vision-exp`; Planning proposed and the user approved
an `impl_agent -> integration_agent -> review_agent` DAG with maximum
concurrency two and resolved timeouts of 1,350, 900, and 600 seconds. The
scheduler correctly observed concurrency one because every Agent depended on
the preceding writer. During the live run, the user switched to detailed
visibility, queued project-specific guidance, paused before the first
invocation, inspected `/controls`, and resumed. Every command acquired a
controller revision, reached its safe applied boundary, appeared in the event
stream, and the guidance reference reached all four subsequent invocation
prompts. This supplies provider-backed evidence for guidance, visibility, and
cooperative pause/resume without claiming that a process crash is resumable.

Both writers completed a clean ten-file commit. All five deterministic gates
passed and the generated suite passed 20 tests. Review then ended as
`artifact_invalid` even though its final direct probe exited zero and emitted
all required markers. The same `SCAN_NESTED_DUP_OK` source text also appeared
inside traceback stderr from two earlier failed probes; the old whole-result
substring rule bound those failures together with the successful emission and
rejected the satisfied assessment. No result was delivered. Separately, an
ordinary external clone proved the committed `uv.lock` contained fourteen
references to `/opt/software-agent-team/wheels`, so exact `uv sync --dev`
failed outside SAT's image. Independent behavior validation still passed all
17 black-box checks after recording that setup failure, which proves useful
implementation behavior but does not override the portability defect. The
terminal display also silently clipped two completed Agent summaries at 500
characters.

The controller now treats direct-probe framing as a typed evidence channel. A
satisfied claim may match child stdout and the terminal result, never traceback
source text in child stderr. If the same marker appeared in a failed direct
probe and was later emitted by a successful direct probe, the successful call
supplies the claim while every failed call remains in telemetry. Persisted
boundary checks now coherently accept command-only grounding and still reject a
check with neither tool nor command evidence. Replaying the two exact archived
Reviewer attempts now accepts all 13 assessments with zero findings; the
`AC_SCAN` claim binds only successful attempt-one `tool-020` and attempt-two
`tool-005`, not failed `tool-013` or `tool-003`.

The generated-project contract also parses a committed lock before setup and
rejects absolute, Windows-drive, `file:`, parent-directory, missing,
symlinked, or private-wheelhouse dependency sources. The archived Round 17
output is now rejected directly at
`root.package[0].source.registry`, before same-image setup can mask the defect.
Developer, Reviewer, seed, profile, and decision documentation all state that
the offline wheelhouse is runtime infrastructure rather than delivery metadata.
Finally, bounded scheduler event text ends at a word boundary when possible and
uses an explicit `… [truncated]` suffix. The complete repository check passes
720 tests at that revision. The next fresh-account provider run, described
below, exercised these repairs and exposed additional delivery-boundary and
Review-recovery defects. Two-route switching, process-restart recovery, and the
independent-device demonstration remain separate evidence requirements.

The eighteenth fresh-account run used the public installer, bare `sat`, and the
same strict `deepseek/deepseek-v4-flash-vision-exp` route. A bounded Planning
repair removed one unknown response field, after which the user approved an
`impl -> tester -> reviewer` DAG with concurrency one and resolved timeouts of
1,350, 300, and 600 seconds. The writer committed eight tracked files and ten
tests. Compile, Ruff, and pytest passed, but the contract and exact-command
gates rejected a non-portable `uv.lock` created by sandbox setup. That lock was
effectively ignored and absent from the implementation commit; the validator
had inspected working-tree residue before determining whether it belonged to
the proposed delivery.

The Tester accurately retained both failed gates. Review then found a separate
product defect: documented `*.log` exclusion was implemented as exact string
membership rather than wildcard matching. Its bounded semantic repair produced
a useful `revise` report, but three positive assessments cited markers emitted
before their direct probes failed. The evidence boundary correctly refused to
treat those markers as success, yet the whole report became
`artifact_invalid`, losing the valid revision path.

The product contract now distinguishes Git delivery content before parsing a
lock. Every tracked lock is validated even when an ignore rule matches it; an
effectively ignored untracked lock is neither delivery metadata nor clean-copy
input. Review grounding now has one narrow monotonic recovery: only an
already-`revise` report with a separate blocked assessment and blocking finding
may change an unsafe positive assessment to `blocked` and add a
criterion-scoped controller evidence-gap finding. Accepted or terminal reports,
zero-match selectors, and invalid blocker mappings are never salvaged. An exact
replay of both archived Reviewer attempts and all 41 captured tool calls now
returns `revise`; AC_JSON, AC_MINSIZE, and AC_TESTS_SUITE are blocked rather than
misrepresented as satisfied. The plan overview also separates
controller-owned execution-profile constraints from additional Planning
constraints, and the Planner prompt forbids restating the former. Focused
coverage passes 115 tests and the complete repository check passes 725 tests.
A fresh provider-backed ordinary-user run remains required before claiming the
repaired path reaches accepted delivery.

The nineteenth fresh-account run again used the public installer, bare `sat`,
and the strict `deepseek/deepseek-v4-flash-vision-exp` route. Installation,
automatic diagnostics, isolated configuration, the authorized provider smoke,
ordinary request capture, destination confirmation, and Planning authorization
all succeeded. SAT then stopped before creating the bootstrap Planning Agent
because its local `openclaw models list --json` inspection did not finish
inside the shared 30-second preflight-command boundary. No provider request,
Agent, TeamPlan, run, source, workspace, or generated project was created by
that failed phase. Later read-only checks against the same isolated state
completed, supporting an insufficient cold-start margin as the cause without
claiming recovery of the swallowed original subprocess exception.

Model inspection now has a dedicated 90-second infrastructure timeout while
ordinary local preflight commands remain at 30 seconds. SAT announces the
bounded local wait before it starts, persists both values in runtime evidence,
and reports an exact safe timeout that states no provider request was made.
Planning wraps that diagnostic with the failed phase and confirms that no Agent
started. Simulated cold-catalog, timeout-safety, visible-status, and no-state
regressions pass 66 focused tests; the complete repository check passes 728
tests. At that point, a fresh ordinary-user provider retry was still required
before the repair could be considered live-validated.

The twentieth fresh-account run supplied that live validation at public commit
`cb5a11e`. A new unprivileged user installed with the documented one-command
bootstrap and then invoked only bare `sat`. Installation, device diagnostics,
isolated secret-free model setup, the explicitly authorized provider smoke,
request capture, execution-profile and destination confirmation, both visible
90-second-bound local model checks, and Planning runtime preflight all passed.
The Round 19 startup boundary no longer failed or sat silently.

Planning used one bounded repair after the first proposal made its revision
flag inconsistent with the one-iteration plan. The user then approved a
task-derived `duplicate_impl -> reviewer` DAG with concurrency one and
controller-resolved 1,350/600-second timeouts. The writer committed eight
changed files. All five deterministic gates and 28 project tests passed. The
Reviewer's first complete response contained an invalid JSON escape; one
94.3-second bounded repair safely reused the same Agent, stage, immutable
commit, and captured invocation-chain evidence. Independent Review accepted
all eleven criteria with zero findings, and SAT delivered clean commit
`62c3c50c46f1093715f9ce735a42d5f2fb441533` after removing two run-scoped
containers.

Independent ordinary-user validation then reran the exact setup, test, and
start commands. Setup succeeded, all 28 tests passed, start returned valid
JSON, and separate fixtures passed recursive duplicate grouping, wildcard
exclude, minimum size, nested-symlink exclusion, root-symlink rejection, and
file-root rejection checks without traceback. The project remained clean and
matched the controller's final commit. The complete evidence was archived
before an exact inventory-based cleanup removed the fresh account, state, and
temporary resources while preserving the shared image and historical
UID-reuse paths.

The rehearsal sequence below concerns the predecessor guided fixed-team
product path and is retained as defect and regression evidence. It proves the
installer, isolated runtime, fixed compatibility controller, delivery, and
cleanup boundaries at the named revisions; it does not prove the newly
activated Adaptive Planning and Dynamic Team journey.

The exported execution record identified the stopped container, and a
read-only Docker postmortem established the root cause: PID 1 exited in 72 ms
with status 255 and `exec /usr/bin/sleep: resource temporarily unavailable`.
OpenClaw had explicitly supplied `sleep infinity`; the image command was not
the cause. The duplicate `RLIMIT_NPROC=128` counted processes by numeric UID
beyond the container on this Docker Desktop host and rejected the initial
process. SAT now retains the per-container cgroup PID limit and omits
`RLIMIT_NPROC`. Installation and live-run probes also supply OpenClaw's command,
execute a Python helper, inspect liveness, and remove the probe. Invalid
terminal Unicode is now rejected and recollected at the affected prompt rather
than reaching Pydantic as a raw validation failure.

The third WSL rehearsal updated the managed application, passed installation,
and persisted `config_valid=true`, `sandbox_container_ready=true`, and no
container error in run `sat-20260824-144218-5102217f`. This directly confirms
the process-limit fix on the Docker Desktop/WSL host. The run then stopped
before a provider request because the pinned DeepSeek plugin catalog knew only
its stable model while the selected
`deepseek/deepseek-v4-flash-vision-exp` reference had not been declared in the
run-scoped OpenClaw provider catalog. OpenClaw reported the exact failure as
`Unknown model`, but the old preflight did not inspect that model route and the
terminal summary therefore surfaced it as a Planner process failure.

SAT now carries a narrow secret-free catalog supplement for that exact model,
checks the selected configured model and auth route during guided startup and
run preflight, persists the model result in `runtime-preflight.json`, and stops
with the direct model diagnostic before any Agent call when it is unavailable.
An available shell key is represented only by `${DEEPSEEK_API_KEY}`; otherwise
the isolated OpenClaw auth profile remains authoritative. The generated config
passes the pinned OpenClaw validator, the exact model is reported locally as
available, and an authorized minimal inference request returned HTTP 200 with
the exact provider/model and expected response.

A subsequent clean non-root rehearsal started from the public one-command
installer and then used only the normal `sat` entry point. Managed installation,
automatic device checks, first-run configuration, the optional provider smoke
check, natural-language request capture, confirmation, and run preflight all
passed with `deepseek/deepseek-v4-flash-vision-exp`. Run
`sat-20260824-225204-176978a8` reached the Planner through the same product flow
an end user sees. Its first response arrived after 72.1 seconds and contained a
short redundant closing-delimiter suffix as well as real semantic defects: an
unknown field and incomplete acceptance-criterion coverage. The bounded repair
was therefore required and produced a valid plan after another 67.0 seconds,
but the then-current shared 120-second Planner deadline had already expired.
SAT stopped without delivering a project; later live evidence showed that
sharing one deadline across two separately authorized calls was itself an
over-constrained timeout policy.

The parser now normalizes only a complete object followed by at most four
unmatched closing delimiters; it continues to reject additional values,
structures, unknown semantic fields, and incomplete plans. Raw provider output
remains unchanged in execution evidence. The Planner timeout is now 180 seconds
per invocation. An optional one-call repair receives that same complete
invocation allowance; the run-wide call-count, Agent-duration, token, and cost
budgets include both calls.

A second rehearsal used a newly created Linux account with its own home,
configuration, provider state, and project parent. The public installer checked
out the published revision, and the normal `sat` flow again passed every step
through planning. The Developer completed a clean implementation commit and 24
project tests in 854.4 seconds, within its existing 900-second budget. Its one
semantic JSON object was enclosed in the requested JSON fence, but the
presentation text before the fence included ordinary command notation such as
Python-style argv arrays. The former fence normalizer treated any square
bracket outside the fence as a competing JSON structure, requested an
unnecessary repair, and then rejected the combined 929-second path at the
then-current shared deadline.

The fence normalizer now distinguishes a separately decodable JSON object or
array from non-JSON documentation notation. It accepts the observed original
Developer response without repair while retaining the stricter raw-object
boundary and rejecting multiple fences or competing JSON values. The existing
900-second Developer budget is unchanged. The complete offline suite and the
corrected restricted Docker helper probe remain successful.

A third clean-account rehearsal then completed the entire controller workflow
in 1,000 seconds. The Developer response was accepted directly in 659.3
seconds, all four deterministic gates passed, independent Tester and Reviewer
responses passed, the decision was `accept`, and SAT delivered a 5/5 project.
The delivered setup command succeeded, but the exact delivered test command,
`uv run pytest`, failed while collecting tests because the generated top-level
`src` package was not importable from the pytest console entry point. The
then-current Docker gate had used `python -m pytest`; that invocation adds the
project root to Python's import path and reported 11 passing tests, masking the
fresh-user failure.

The Python product test gate now invokes the pytest console entry point, which
matches the delivered command after `uv run` selects the project environment.
A checked invariant prevents the gate and generated-project contract from
drifting back to different entry-point semantics. The accepted third-run
project is retained as failure evidence and is not treated as a runnable
delivery.

A fourth clean-account rehearsal confirmed that correction. The first
Developer commit reached the aligned gate in 590.2 seconds; contract, compile,
and lint passed, while the pytest console entry point correctly rejected an
unimportable `app` package before collection. Tester and Reviewer requested a
revision. The Developer then changed one file in 267.9 seconds, resolved that
recorded import finding, and advanced the suite to 15 of 16 passing tests. The
new remaining failure was distinct: the server could not create its configured
SQLite database because the parent data directory did not exist. With the
hard-coded two-iteration limit exhausted, SAT correctly failed without
delivery even though the second iteration had measurable progress.

At that revision, the workflow iteration limit became an explicit controller
input bounded by the team manifest. The advanced frozen evaluation remains at
two iterations for comparability. The current adaptive product path instead
uses the one-to-three iteration limit shown in and approved with each TeamPlan.
Repeated blockers, no-change revisions, resource limits, and all safety or
evidence-integrity stops remain unchanged.

A fifth rehearsal began with another fresh non-root Linux account and the
public one-command installer at revision `a4c929d`. The user then invoked only
`sat`, completed guided first-run configuration, and described a small local
reading-list Web app in natural language. Run
`sat-20260825-005232-c8f61f0a` used
`deepseek/deepseek-v4-flash-vision-exp`. The Planner completed in 106.6
seconds. The Developer's first response omitted its required semantic JSON, so
the existing bounded repair path was legitimately used. The first
implementation then reached the aligned project gates, where pytest correctly
exposed an import defect.

On iteration two, the Developer changed one file in response to that evidence.
All four deterministic gates passed, eight generated-project tests passed, and
the independent Tester and Reviewer both accepted the result. SAT completed all
five user success conditions in 1,689 seconds and delivered clean commit
`6283aa12401e1e18272df5315bdc9ef92e2478da`. The exact generated setup and
test commands then succeeded outside the controller. The exact start command
bound the application only to `127.0.0.1`; manual HTTP checks added, edited,
finished, persisted across a clean stop and restart, and deleted a book. Both
application starts shut down cleanly, and no listener remained afterward.

The generated result therefore passed the functional Product Demo Slice on a
clean Linux account. A post-run resource audit then found all seven
session-scoped OpenClaw role containers still running. OpenClaw deliberately
retains these containers for session reuse, but SAT session keys are unique to
immutable runs; several older rehearsal containers also retained child test
processes. This invalidated the claim that the terminal product lifecycle was
complete even though the generated application itself had shut down.

SAT now performs bounded run-terminal cleanup after completed, failed,
interrupted, and exceptional workflows. It selects a container only when its
OpenClaw label has an exact controller-generated session key for that run and
one of its bind mounts is beneath the exact SAT-owned state or workspace path.
Broad name matching is forbidden, and a matching label outside those paths is
refused, preserving every other OpenClaw boundary.

A sixth rehearsal used another empty non-root account, the public installer at
revision `4f273fd`, bare `sat`, the same natural-language request, and the same
exact model. Installation, diagnostics, guided configuration, provider smoke,
confirmation, and run preflight passed. Run
`sat-20260825-022440-13824df0` recorded the Planner in 105.5 seconds and the
Developer in 595.5 seconds. The first implementation failed the aligned pytest
gate with an import defect. Tester returned malformed JSON in 106.2 seconds;
its one bounded repair returned a valid semantic response in 213.8 seconds.
Although neither invocation exceeded the 300-second Tester timeout, the old
controller added their durations and rejected the already-returned repair at
320 seconds.

That shared-deadline rule is now removed. Every initial response and optional
one-call repair receives the resolved per-role invocation timeout. The repair
does not escape resource control: both calls count against frozen total calls,
Agent duration, tokens, and estimated cost. The DeepSeek compatibility
supplement's conflicting fixed 600-second provider transport timeout is also
removed; the frozen controller timeout passed to OpenClaw is now authoritative.
Regression coverage reproduces a pair whose aggregate duration exceeds one
invocation timeout, separately proves that the total Agent-duration budget
still stops it, and prevents the compatibility supplement from restoring a
second transport cap. The complete 400-test suite passes. The sixth run also
confirmed terminal cleanup on the real failure path: SAT reported removing
three run-scoped containers, and an external exact-label audit found no
container belonging to the run.

A seventh rehearsal then started with another empty non-root Linux account and
the public installer at corrected revision `a032855`. The user invoked bare
`sat`, completed the same guided configuration, and supplied the same request,
success conditions, constraints, destination name, and exact
`deepseek/deepseek-v4-flash-vision-exp` model. Run
`sat-20260825-030006-255f469f` completed in 1,725 seconds. Planner completed in
89.3 seconds. The materialized runtime contained no independent provider
transport timeout; the first Developer commit completed in 577.1 seconds and
reached a real pytest failure. Tester completed in 109.0 seconds. Reviewer's
80.9-second response needed one bounded repair; the independent 81.1-second
repair succeeded and the controller chose `revise` without a shared-deadline
false failure.

On iteration two, Developer completed in 402.8 seconds and used one valid
115.3-second response repair. The controller verified two changed files, all
four deterministic gates passed, Tester completed in 88.9 seconds, Reviewer
completed in 93.4 seconds, and the decision was `accept`. SAT delivered clean
commit `8fa75927662b515fe5c57ed72acf5a4f8b4c3c2d` with 5/5 acceptance results.
The run used nine Agent calls, two bounded repairs, 1,637,869 milliseconds of
Agent time, 82,666 input tokens, and 31,727 output tokens, all on the frozen
model without fallback.

The exact delivered setup command succeeded, and the exact delivered test
command passed 21 tests. The exact start command listened only on
`127.0.0.1:8000`. HTTP form checks added, edited, marked finished, persisted
across a clean stop and restart, and deleted a book. Both starts shut down
cleanly and released the port. SAT reported removing seven run-scoped Agent
containers before returning control; an external exact-label audit found zero
live or stopped containers for the run, while the count of unrelated OpenClaw
sandboxes remained eleven. Credential scans of the trace, terminal record, and
delivered project were clear. The setup command generated an untracked
`uv.lock`; this is a non-blocking reproducibility observation because the
accepted delivery commit itself was clean and all promised commands and user
outcomes passed.

This confirmed the predecessor Product Demo Slice on a fresh Linux account.
At that point the Adaptive Planning and Dynamic Team path still required its
own fresh provider-backed rehearsal; the adaptive evidence recorded earlier in
this document has since satisfied that historical gap. The current product
still supports small greenfield Python 3.12 projects and keeps the task-manager
contract isolated to the advanced evaluation surface.

The advanced `prepare-benchmark`, `preflight`, and `run` commands remain a
separate evaluation surface and are not part of the expected product demo.
The acceptance contract is
[`docs/product-demo-slice.md`](docs/product-demo-slice.md).

## Implemented and Offline Verified

- A versioned `TaskSelfCheckReport` contract with stable result identity,
  authority, dependency, freshness, severity, status, actionable evidence and
  remediation; transitive invalidation and exact stale-result refresh; a
  write-once per-task digest chain; compact, standard, and detailed rendering;
  and independent persisted-schema compatibility coverage;
- Local `sat --version`, human-readable `sat version`, and machine-readable
  `sat version --json` reporting from one release/source identity API, including
  managed-install provenance, exact Git revision and dirty state when
  available, explicit partial or inconsistent identity status, and one
  authoritative readable interval for every persisted schema family;
- Impact-driven release-candidate gates that bind `pyproject.toml`, `uv.lock`,
  the prior release baseline, minimum SemVer increment, exact tag/commit,
  deterministic source-archive digest, and every schema readable range;
- A pinned exact-tag GitHub workflow that reruns the offline gates and publishes
  one digest-verifiable `sat-release.json` asset, plus a stable resolver that
  rejects drafts, prereleases, tag/manifest drift, repository drift, missing or
  duplicate assets, and digest mismatch;
- Product-level `sat update --check`, confirmed `sat update`, local
  `sat channel status`, and explicit `sat channel switch stable|dev`, all using
  one immutable target resolver and one staged activation transaction;
- Managed lifecycle ownership for default and custom application paths,
  shared foreground-task and exclusive activation locking, candidate-owned
  pre-activation persisted-schema compatibility, active-run and stale-release
  refusal, atomic application-link and install-record rollback, source-checkout
  refusal, and v1 managed-layout migration;
- Versioned managed uninstall that cross-checks the lifecycle root, active
  release, installation record, logical link, and recorded launchers before
  removing all retained application versions while preserving configuration,
  run data, isolated provider state, other OpenClaw installations, uv, Docker,
  and the sandbox image by default;
- Reproducible toolchain setup and diagnostics;
- Unified validation, benchmark-preparation, preflight, and `sat run` CLI;
- Versioned team manifest and validation;
- Versioned `TeamPlan`, `AgentSpec`, and `ModelRoutePlan` contracts with
  validation for dependency cycles, unknown references, write ownership,
  permission profiles, quality independence and coverage, model
  authorization, approved concurrency, and conditional controlled-evaluation
  limits;
- Exact compilation of every fixed evaluation fixture into the same
  run-scoped contract, including frozen TaskBrief binding, Agent time authority,
  dependency waves, workspace scopes, model route, budget, and manifest
  provenance;
- Versioned, hash-chained `RunEvent` persistence with run-state head anchoring,
  controller lifecycle and Agent attribution, dependency and route metadata,
  safe summaries, aggregate budget snapshots, and renderer visibility
  filtering;
- Versioned `ControlCommand` requests and terminal resolutions with typed
  targets, controller-assigned mailbox order, command-specific safe boundaries,
  optimistic revisions, immutable metadata, and predecessor-digest
  verification;
- Foreground plain-language run controls for prospective guidance, replacement
  Planning, cooperative pause/resume, best-effort per-Agent interruption,
  confirmed terminal cancellation, live visibility switching, exact command
  consequences, provider-cost caveats, and cancelled final reports;
- Versioned, explicitly authorized Adaptive Planning requests; strict
  question-or-proposal responses; high-value focused questions with suggested
  and custom answers; controller validation and targeted semantic correction;
- Atomic Planning-question correction: current question submissions must carry
  their complete authority-key set, unknown or misspelled question keys fail
  closed, and any question-contract defect replaces the complete user-visible
  question while legacy persisted records remain readable;
- Model-facing atomic requirement and assumption relations, with
  Controller-compiled backward-compatible persistence and correction-time
  assumption references restricted to retained Agent-autonomy decisions;
- Task-defined proposal compilation into confirmed requirements, adaptive
  implementation intent, least-privilege AgentSpecs, exact primary and
  fallback model assignments, dependency waves, qualitative per-Agent workload
  estimates, controller-resolved time authority, and aggregate controller
  budgets;
- Task-proportional Adaptive team validation with no bootstrap capability in
  the runtime team, exact task ownership and cross-Agent dependency alignment,
  and at least one downstream read-only quality path for every writer;
- Hash-chained Planning-turn evidence including typed content-free provider
  liveness, immutable proposal revisions, exact user-approval digests,
  natural-language revision, safe structured edits, cancellation, and a
  complete plain-language overview;
- A replaceable OpenClaw subprocess adapter with stable fixed-role and
  run-scoped Agent sessions, explicit Agent ID and capability telemetry,
  version-pinned local and Gateway JSON parsing, private content-free stream
  observation, provider/model-aware renewable inactivity leases, and canonical
  `provider/model` telemetry;
- A maintained real-transport loopback gate whose productive stream,
  Controller-observed recovery, disconnect, and permanent-silence scenarios
  each have typed status, lifecycle, liveness, response, and exact-cleanup
  oracles; an outcome mismatch returns non-zero even when cleanup succeeds;
- Sanitized OpenClaw Agent registry, permission checks, approved-Agent-only
  run-scoped configuration, non-root identity, exact per-Agent model-route
  enforcement, and offline preflight across every authorized route;
- A marked application-private OpenClaw binary plus explicit private config,
  credential, state, workspace, and Agent paths for every SAT invocation, with
  ambient OpenClaw settings neutralized and existing installations untouched;
- Exact run-scoped Agent-container cleanup on normal, failed, interrupted, and
  exceptional workflow exits, guarded by both session identity and SAT-owned
  mount provenance;
- Confirmed task-brief and handoff-envelope contracts;
- Fixed-role and task-defined capability minimum-context prompts, strict
  semantic JSON response parsing, dynamic identity/task/route/time-authority binding,
  controller assembly of persisted envelope, Git, test, and scope facts, and
  digest-bound field correction with controlled-evaluation caps;
- Contract-aware response normalization that permits presentation argv arrays
  around one semantic object while rejecting any additional object candidate;
- Exact Dynamic Reviewer criterion assessments with adversarial checks,
  same-chain attempt-qualified result selectors, same-iteration deterministic
  command selectors, controller-resolved tool and command IDs, unambiguous
  blocked-finding scope binding, and bounded no-network foreground probes
  against read-only source;
- Concrete phase-artifact and Agent-telemetry contracts with contextual
  validation;
- Immutable phase artifacts, handoffs, command output, Agent output, canonical
  paths, and SHA-256 references;
- Deterministic TeamPlan DAG scheduling with dependency readiness, exact
  approved team membership, approved concurrency, time-authority propagation,
  fail-fast launch control, attributable skipped nodes, and ordered progress
  events;
- Shared-Git workspace safety that permits concurrent read-only Agents while
  making every workspace writer exclusive until isolated worktrees and an
  explicit integration protocol exist;
- Persisted run lifecycle with a write-once `team-plan.json`, validated
  transitions, atomic replacement, optimistic concurrency checks, cross-file
  digests, fixed-fixture provenance, and integrity-checked recovery;
- Safe detached standalone-clone creation and chained iteration snapshot
  verification;
- Frozen task-management TaskBrief, deterministic seed commit, independent
  acceptance suite, shared content-pinned Python image and dependency lock,
  per-run immutable local image identity,
  fixed quality-gate manifest, and independent acceptance suite;
- Docker-only production gates with no network, read-only source execution,
  non-root identity, fixed commands, resource limits, timeouts, bounded output,
  plus fresh-scratch execution of exact generated setup, test, and start argv
  through a frozen PyPI-attributed offline cache;
- The complete function-specialized workflow: Planner, Developer, controller
  snapshot, deterministic gates, independent Tester and Reviewer with
  configurable dispatch concurrency, decision, and launch-policy-bounded
  evidence-driven revisions;
- Bounded command-output diagnostics for verification, correct read-only
  source visibility, and controller-only Agent invocation policy;
- Explicit deterministic command coverage, `pending_review` manual criteria,
  Reviewer scope attestation, and controller-owned evidence resolution;
- Pre-call route-price and task-wide USD authorization for ordinary tasks, plus
  explicit call, token, duration, and cost thresholds only for controlled
  evaluation;
- One thread-safe ledger shared by Planning, task-defined execution, correction,
  repair, and switching. It prices provider usage from frozen call terms,
  preserves unpriced or missing-token states, exposes standard progress, and
  emits an attributable `budget-ledger.json` plus report breakdown;
- Typed decision-limit ownership metadata; one user-approved task-wide USD
  ceiling and optional deadline for ordinary tasks; provider-activity time
  authority; controlled-evaluation-only timeout/count envelopes; user-selected
  concurrency; and configuration-schema migrations from superseded product
  caps;
- Explicit completed and failed terminal outcomes with machine-readable and
  human-readable reports, exact controlling SAT software identity, and their
  model-spend ledger committed through a rollback-capable terminal bundle;
- Remote one-command Linux/WSL bootstrap into an owned user-local application
  directory, plus the pinned toolchain, locked environment, fixed Docker image,
  stable launchers, update validation, and a checkout-based contributor path;
- A versioned OpenClaw sandbox image plus install-time and run-time restricted
  probes that require a real tool helper to execute and reject a container that
  merely exists or starts momentarily;
- Automatic startup checks for platform, architecture, unprivileged identity,
  project-parent writability, required commands, SAT's pinned private
  OpenClaw, Docker daemon, Linux-container image, storage, and launcher
  visibility;
- Integrated first-run and repeatable model configuration with private,
  atomic, schema-versioned secret-free profiles, deterministic route policy,
  optional authorized provider smoke checking, and no invented zero-cost
  estimate when prices are unknown;
- Natural-language request capture, explicit Python execution-profile
  confirmation, destination validation, explicit Planning authorization,
  bounded clarification, complete overview, natural-language revision, safe
  edits, and exact approval before execution Agents;
- Automatic private user-state roots, collision-resistant run IDs, separate
  Planning evidence, confirmed TaskBrief and TeamPlan materialization, trusted
  source creation after approval, isolated workspaces, and write-once evidence;
- Controller-backed role, elapsed-waiting, Git-snapshot, quality-gate,
  independent-review, decision, revision, completion, and failure progress,
  plus adaptive Agent queue, readiness, provider wait/activity, tool lifecycle,
  liveness degradation, suspected stall/grace/recovery, correction, duration,
  dependency, route, budget, and terminal-state projection;
- Accepted-result-only delivery through a same-parent staging directory into a
  new non-overwriting project child, followed by exact setup, start, and test
  commands from a validated project-owned argv manifest;
- Guided one-command uninstall with preservation defaults, pre-removal export,
  separate configuration/data/private-provider-state purge choices, Planning
  evidence preservation/export/purge,
  managed-application removal, and preservation of every other OpenClaw
  installation;
- Offline end-to-end coverage for success, revision, targeted response correction,
  invalid-response failure, timeout, evidence tampering, non-convergence,
  iteration exhaustion, no-change failure, missing model or token telemetry,
  cost exhaustion, and trusted sandbox-runtime loss classification.

## Current Fixed Evaluation Team Paths

[`configs/teams.json`](configs/teams.json) defines three comparable topologies.
The configuration owns membership and initial stage ordering; the Python
controller owns dynamic revision and termination decisions.

These manifests are fixed evaluation fixtures. Explicit `sat run` compiles the
selected fixture into the same `TeamPlan` contract used by the controller. The
normal product path does not select one of these fixtures: its approved plan is
derived from the task.

| Configuration | Purpose | Implementation status |
| --- | --- | --- |
| `single_agent` | One-pass baseline | Phase 3 |
| `function_specialized` | Planner, generalist implementation, independent testing and review | Phase 1 implemented and provider-validated |
| `implementation_domain_specialized` | Parallel frontend/backend work plus integration | Phase 3 |

## Not Yet Available or Completed

- Fresh WSL ordinary-user evidence for task-admission/approved-plan remediation
  and process-orphan recovery;
- An independent-device live demonstration of the activated Adaptive Planning
  and Dynamic Team journey;
- Durable control recovery after a foreground process crash and a
  secondary-process control client;
- A provider-backed run using two planned model routes and live switch
  evidence;
- Saved task/scenario-specific routing presets and empirically calibrated
  quality/latency/cost-aware selection beyond declared capability and priority;
- Generated-project execution profiles beyond the current local Python 3.12
  profile;
- Semantic provider/auth validation beyond the explicitly authorized minimal
  smoke check;
- Automatic CLI resume of an interrupted run;
- Executable `single_agent` and `implementation_domain_specialized` workflow
  paths;
- Repeated comparative trials, optional cross-participant human-factors study,
  and topology selection;
- Additional product execution profiles and their independent quality
  contracts.

The current `sat run` command starts from a confirmed `TaskBrief`, requires a
fresh run ID, and intentionally does not infer that an unrecorded external
action succeeded after interruption.

## Current Validation Boundary

Planning concision and question authority are implemented and have
provider-backed evidence. Task-derived specialization has clean offline and
managed ordinary-interface evidence on the previously validated code: Security and Experience
contracts both executed, produced grounded typed artifacts, and contributed to
an accepted delivery. This establishes the Linux managed-runtime boundary; it
does not substitute for the separate fresh-WSL remediation condition or a
multi-model live-switch experiment. The unreleased correctness fixes listed
above have their own affected integration checks and do not retroactively
change the identity of that provider-backed evidence.

A genuine previous-stable-to-newer-stable upgrade now exists for `v0.1.2` to
`v0.2.0`; it used immutable published manifests and product update commands
rather than simulated tags or fixture-only migration.

Fixed-topology comparison remains in Phase 4 so it can serve as a controlled
baseline rather than define the product's permanent role layout.
The detailed sequence and acceptance criteria are in
[`docs/adaptive-orchestration.md`](docs/adaptive-orchestration.md).

The development route and evaluation policy are defined in
[`VISION.md`](VISION.md#development-route).

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
7. One overview of requirements, acceptance criteria, controller-owned
   execution-profile constraints separated from additional task constraints,
   Agent work assignments with their controller-derived write or read-only
   authority, proposed Agents, dependencies, permissions, explicit Review entry
   obligations for absolute guarantees and the exact meaning of each boundary,
   resolved model profiles and fallback authority, timeout reasons,
   concurrency, iterations, and budgets; and
8. Approval, a natural-language revision request, a supported safe edit, or
   cancellation before any execution Agent is created.

After approval, SAT creates only the task-defined Agents in that exact plan.
The controller derives actual launch order from the approved dependency graph,
enforces concurrency and shared-workspace safety, resolves and enforces each
Agent timeout, records verified Git snapshots and quality evidence, and owns
revision and termination decisions. On success, SAT reports the delivered
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
attributable provider failure. It can also store per-profile prices, the
adaptive maximum concurrency, compact/standard/detailed progress visibility,
or an explicit global invocation-timeout override. Use
`sat configure --help` for the complete advanced interface.

Provider credentials remain in SAT's isolated OpenClaw state or in an
explicitly trusted caller environment; they are not written to the repository,
generated project, run evidence, model profiles, or SAT exports.

Before asking for a project, SAT checks that its isolated runtime recognizes
the bootstrap model. Before starting an Agent, run preflight checks every model
route authorized by the approved TeamPlan, with a local catalog/auth route for
each. These checks do not generate content. An optional provider smoke check
remains a separate, explicitly authorized action because it can incur usage.
SAT announces the local inspection before waiting. A cold model-catalog check
may use up to 90 seconds; that infrastructure boundary is separate from the
30-second ordinary preflight-command limit and from every approved Agent
invocation timeout. If it expires, SAT reports that no provider request was
made and does not create an Agent.

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
managed update command and stable/dev channel switching are not available yet.
Until they are, rerun the installation command to update a managed
installation.

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

## Current Scope and Maturity

SAT is experimental software. The supported build path is currently limited
to new, small Python 3.12 projects; existing-codebase modification, additional
runtime profiles, interrupted-run resume, deployment, and publication are not
yet supported. Adaptive Planning, plan approval, task-defined execution, and
delivery are integrated in the normal `sat` path and covered offline. Planning
retains raw model evidence while deterministically ignoring repeated
definitions of fixed controller-owned profile criteria; legal task bindings
still resolve to the canonical profile contract without consuming a model
repair. The same append-only event stream now drives configurable per-Agent
progress, including
dependency, provider-wait, repair, terminal, route, duration, and budget facts.
Planning model waits also show elapsed heartbeats, response validation, and a
bounded-repair transition. Provider heartbeats stop on hidden completion
events as well as terminal Agent state, and failed quality gates render as
failures rather than successful check marks. Reviewer timeout has a
controller-derived scope floor that counts both assigned criteria and every
explicit Review boundary obligation. Its project mount and general write tools
remain read-only, while an immutable path-restricted helper can create a
new bounded `/tmp/sat-review-probe-*` script or fixture without overwriting an
existing file. The image also includes a locked offline Python wheelhouse.
Review boundary names use one controller-owned protocol across Planning,
approval, implementation, and Review. In particular, `top_level_input` is the
primary input selected by the user or upstream caller; an immediate child
inside that input is already `nested_input`, not another top-level case.
Deterministic verification copies only clean committed files into fresh
sandbox scratch and runs the generated project's exact setup, test, and start
commands with no network. Reviewer criterion claims must cite actual Reviewer
tool results or controller-owned deterministic command output from the same
immutable iteration. Exact fragments are preferred; keyed JSON fragments also
accept presentation-only RFC JSON whitespace differences outside quoted
strings. On the normal adaptive path, a bounded semantic repair may reuse an
integrity-checked result from an earlier attempt by the same Reviewer against
the same immutable commit. SAT extracts and sanitizes those records itself and
persists actual attempt-qualified tool IDs or command IDs. Repeated or
overlapping selectors are deduplicated, while a fragment with no eligible match
is rejected. For a satisfied direct `sat-probe-run` claim, only completely
framed child stdout and the terminal result are positive evidence; unframed or
partial output and traceback source text in child stderr cannot satisfy a
claim. A later successful probe emission can
replace an earlier failed probe match for that fragment without deleting the
failed attempt from audit evidence. A sole unscoped blocking finding is
deterministically bound to the
otherwise-uncovered blocked criteria; ambiguous multiple-finding mappings are
still rejected. The model never has to predict controller-owned IDs or repeat
an unambiguous relationship solely for serialization. If re-verification fails
after a revision changed the commit, the terminal report keeps the prior
finding unresolved while distinguishing its earlier evidence from the current,
not-yet-independently-verified commit.
When a report already requires revision for a separately blocked criterion,
SAT can conservatively turn an additional unsafe positive evidence claim into a
blocked evidence gap without another model call. It never uses this recovery to
accept a run, and the failed result remains attributable evidence rather than
proof of success.
For an `exec` tool call, SAT attributes only the leading environment assignments
and first executable token, then stops parsing the unpersisted shell suffix. It
still hashes the complete canonical arguments and records the real result, so a
Bash-valid comment suffix cannot erase an otherwise attributable Review while
an unavailable or malformed executable prefix remains an integrity failure.
The foreground control palette, live visibility changes, prospective guidance,
replacement Planning, cooperative pause/resume, best-effort interruption, and
terminal cancellation are integrated and covered offline. Durable resume after
a process restart remains under development. Secret-free multi-model profiles,
deterministic plan-time route resolution, per-Agent route inspection, and an
explicit provider-failure fallback are implemented and covered offline. A
fresh provider-backed strict-route adaptive rehearsal has now completed from
the public installer through accepted delivery and independent post-delivery
checks. A run with two authorized routes remains required before claiming
demonstration readiness for the multi-route path.

The generated Python profile requires README documentation of the exact setup,
start, and test argv. The start command must be directly usable from the
project root without extra arguments. A dedicated quality gate verifies all
three exact commands from a fresh copy of the immutable commit; the ordinary
clean-workspace pytest gate remains an independent check. Every `uv.lock`
tracked in the proposed delivery must also be installable outside SAT's image:
the profile rejects absolute, `file:`, parent-directory, missing
project-relative, and private-wheelhouse sources before delivery. An effectively
ignored untracked lock is runtime residue and is excluded from this delivery
judgment and from the clean scratch copy.

The activated adaptive path has passed a complete strict-route live-provider
rehearsal in a fresh non-root Linux account. Public installation and bare
`sat` produced a user-approved task-defined team, accepted all ten criteria,
delivered a clean project, passed all exact project commands and thirteen
independent post-delivery checks, and left no run-scoped container residue. A
separate fresh-account provider rehearsal has exercised detailed visibility,
prospective guidance, cooperative pause, `/controls`, and resume through the
same ordinary foreground UI. That run deliberately remains failure evidence
because it exposed the now-corrected Review-channel and lock-portability defects.
An accepted retry, a two-route switch, and an independent-device demonstration
remain pending.

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

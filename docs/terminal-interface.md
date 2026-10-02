# Terminal Interface

SAT uses one terminal owner for progress, command editing, and Planning answers.
On a capable interactive terminal, the control editor remains below the live
panels. Milestones append above them without replacing the draft. Completion
choices occupy their own rows below the editor. Short terminals abbreviate the
status panel and report the omitted rows; enlarging the terminal reveals more.
One reserved blank row separates scrollback from the live region, including
Planning totals before an Agent observation. Live headings and elapsed totals
use cyan, waiting states use yellow, budget uses green, context and results use
blue, and Git changes and tool headings use magenta. The existing color setting
also applies to these panels; `--progress-color never` disables their colors.

## Visibility

Visibility applies to startup checks, Planning, the overview, and execution.
It changes presentation without changing execution, approvals, or permissions.

| Level | Content |
| --- | --- |
| `compact` | The former standard projection: readable Planning, per-Agent state and elapsed time, approved tasks, checkpoints, important milestones, warnings, and results. |
| `standard` | The former detailed metadata plus independently refreshed total elapsed time, settled estimated cost, remaining budget headroom, context/window observations, Git line changes, and attributable file/tool/command milestones. |
| `detailed` | Standard information plus a separate live area for the most recently observed Agent's bounded tool arguments/results, commands/output, and visible model text. |

The total clock and Agent clocks refresh every second even when no new event
arrives. Budget headroom accounts for in-flight reservations; it is not a
provider account balance. Unknown prices or usage remain explicitly incomplete.
Context shows input plus reported cache-read/write tokens for the last
attributable request against the configured model window. It is not cumulative
run tokens or an estimate of the next prompt. Compaction invalidates an old
context observation until a new request reports usage.

Git changes are sampled about every three seconds against the build base, with
untracked text included. Binary, unreadable, large, or excess files make the
count explicitly partial. No generated command, hook, or external diff runs
for this preview. A missing observation is unavailable, not zero.

Tool milestones describe observed operations, not independent proof of success
or file creation. Detailed previews select explicit visible text; hidden
reasoning and typed submission bodies are excluded. Credential-shaped text and
known environment credentials are redacted. Previews are bounded (1,000 model
characters, 600 argument/result characters, a 64 KiB stream buffer) and are
ephemeral. They do not enter the Controller event journal or grant authority.
Native CLI frames that omit `sessionId` use one matching native `runId` from
the executor-owned private stream, together with the attributable session.
Explicit foreign session/run identities and hidden/end-message raw content
remain excluded.
During streaming, the bounded visible tail updates; native thinking events are
never selected. Missing or unattributable text remains unavailable.

## Editing and Commands

Type `/` to see available commands and descriptions; Tab completes commands,
visibility values, and applicable Agent targets. Arrow keys, Home/End, Delete,
and Backspace edit the buffer. Enter submits once. Ordinary characters and
arrow keys do not print command errors. Ctrl+C clears the draft. Use
`/cancel confirm` to stop the active Planning or execution phase.

During Planning, `/help`, `/status`, `/visibility compact|standard|detailed`,
and `/cancel confirm` are available while waiting and while answering questions.
Execution-only commands explain that they become available after approval.
Commands do not become clarification answers or plan approval. In a multiline
answer, Alt+Enter or Ctrl+O inserts a newline. Start an answer with `//` to submit
a literal leading `/`. Cancellation stops owned invocations, collects evidence,
and records the cancellation; incurred provider usage remains billable.

During execution, `/help` explains `/guide`, `/correct`, `/pause`, `/resume`,
`/interrupt`, `/cancel`, `/controls`, and `/visibility`. Their application
boundaries are defined by the
[Controller control contract](adaptive-orchestration.md#user-controls).
An unfinished command is discarded when the run ends; it is never submitted
automatically. Progress remains visible while editing.

## Append-only Progress and Durable Logs

To restore uncolored scrolling progress for one run:

```bash
sat --progress-display log --progress-color never --progress-visibility detailed
```

In this mode progress updates append instead of refreshing progress panels.
Interactive terminals still provide the editable control prompt. Redirected
progress is plain ordered text without cursor controls. Display, color, and
visibility are separate saved settings and separate one-run overrides.

The durable Controller journal is always recorded, including in live mode.
It stores attributable state transitions, counters, and evidence references,
not a capture of terminal repaint sequences or detailed content previews.
Terminal capture tools record repaints and can produce much larger files;
use the run's journal and report for structured diagnostics.

The default `standard` Planning overview uses the readable approval projection.
Choose `d` to expand technical plan identities, permissions, provenance, and DAG
traceability, or select `detailed` visibility to show them initially. Passing
startup and task checks show their facts in `standard`; evidence references and
rerun rules for passing checks appear in `detailed`. Blocked checks always retain
an actionable cause and remediation at every visibility level.

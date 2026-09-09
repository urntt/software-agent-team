# Adaptive Orchestration and Interactive Control Specification

This contributor-facing specification defines the product contract for
task-defined Agent teams, interactive planning, observable execution, user
controls, and model routing. It defines required behavior and compatibility;
it does not track which batches currently pass, which live rehearsals have run,
or what should be executed next. Those time-sensitive implementation and
verification facts belong only to [`STATUS.md`](../STATUS.md). The completed
guided baseline remains specified in
[`product-demo-slice.md`](product-demo-slice.md).

The durable product and architecture decisions behind this design belong to
[`VISION.md`](../VISION.md). This document owns the detailed interaction,
runtime contract, staged implementation plan, and acceptance criteria for the
adaptive-orchestration milestone.

## Design Goals

The next product milestone must let a user:

- Describe a task without choosing a predefined team topology;
- Clarify requirements through ordinary dialogue and focused questions with
  suggested answers plus a custom-answer path;
- Review and revise one overview of requirements, implementation intent, Agent
  responsibilities, dependencies, budgets, and model choices before execution;
- See what the run and every Agent are doing at an appropriate level of detail;
- Guide, correct, pause, resume, interrupt, or cancel a long-running build;
- Configure different models by task, stage, Agent, or scenario without silent
  fallback or loss of experimental reproducibility.

Dynamic orchestration does not mean unbounded autonomous spawning. The
controller, not a model, continues to own Agent creation, permissions,
scheduling, budgets, lifecycle transitions, evidence, and cleanup.

## Fixed System Capabilities and Dynamic Execution Roles

The product keeps a small set of fixed system capabilities:

- The product CLI and interaction layer;
- A bootstrap Planning capability;
- The deterministic controller;
- Versioned permission profiles and sandbox policies;
- Deterministic quality gates and artifact validation;
- The progress renderer and control channel.

The bootstrap Planning capability is not a permanent execution-team role and
is not an orchestrator. It may ask questions and propose a plan, but it cannot
spawn Agents, grant tools, advance lifecycle state, or accept its own proposal.

Execution roles are run-scoped. Their names, number, responsibilities,
dependencies, prompt purposes, and model routes are derived from the confirmed
task. A small CLI utility may need one implementation Agent and one independent
quality Agent. A Web application may justify separate interface, backend,
integration, testing, and review responsibilities. The system must explain why
each proposed Agent exists rather than selecting a larger team by default.

Independent quality control is a controller requirement, not a fixed role
name. A plan may assign testing and review to one or more read-only Agents, but
the same Agent that writes a change cannot be the sole authority accepting it.
When separate Testing and Review Agents exist, the approved DAG may make them
parallel peers or may place Review after Testing so it can consume that durable
handoff. Both remain read-only and downstream of every writer; independence
does not impose a hidden peer-only topology.

## Decision and Control Responsibility

Adaptive does not make execution order, parallelism, Agent count, or time boundaries
unowned model choices. Responsibility is divided explicitly:

| Decision | Proposal | Approval or default | Runtime enforcement |
| --- | --- | --- | --- |
| Agent number, labels, responsibilities, and capabilities | Bootstrap Planning derives them from the task and explains each one | User approves or revises the overview | Controller creates only approved `AgentSpec` entries |
| Dependencies and possible parallel waves | Bootstrap Planning proposes a DAG | User approves it; policy supplies safe limits | Controller validates acyclicity and schedules only ready nodes |
| Maximum concurrency | Bootstrap Planning proposes a bounded value | User may edit it; policy caps it | Controller decides which ready Agents actually start without exceeding the cap |
| Whole-run time | Planning may explain likely duration but does not invent a deadline | SAT asks before the first model call; default is no deadline unless the user has a real one | Controller applies only the explicitly authorized task deadline |
| Provider-call liveness | The model does not choose its own watchdog | Provider/model capability and measured evidence define a renewable inactivity lease plus probe/grace behavior | Controller renews only from trustworthy activity and interrupts only after sustained verified silence |
| Task model cost | Planning may explain route use and cost exposure | User authorizes one total USD ceiling covering the complete task journey | One monotonic ledger accounts for Planning, execution, targeted correction, and switching; calls, tokens, Agent count, iterations, and duration remain telemetry |
| Model route | Planning may recommend task needs; configured profiles and routing policy provide candidates | User approves effective routes and switch conditions | Controller resolves and records the authorized route; there is no silent fallback |
| Replanning or team changes during execution | User correction or an Agent recommendation may request a change | Material changes require a new validated revision and user confirmation | Controller applies a revision only at a safe checkpoint |

The Planner therefore proposes semantic organization and workload estimates,
the user authorizes material choices, policy resolves the allowed operational
envelope, and the controller owns validation, creation, scheduling, time authority,
lifecycle, evidence, and cleanup.

Dependencies are the complete sequencing contract. A quality Agent may depend
on another quality Agent when the overview makes that handoff explicit. The
controller does not silently rewrite the DAG to maximize parallelism, and the
scheduler never starts a dependent Agent early.

Fixed capability seconds, call/token ceilings, and iteration counts remain
valid only when a controlled evaluation deliberately freezes them as measured
variables. They are not ordinary-product defaults. Product admission instead
records one user-approved USD ceiling and an optional whole-run deadline. Each
provider invocation is protected by a separate renewable inactivity contract:
trustworthy provider streaming, tool lifecycle/output, controller-verified
artifact, or checkpoint activity renews the lease; SAT's own elapsed-time
heartbeat and mere process existence do not. Before provider readiness, exact
invocation-owned CPU, fault, I/O, or complete topology changes may renew only
the separate initialization inactivity lease and cannot grant readiness. Sustained silence enters a visible
suspected-stall probe and grace state before interruption and evidence cleanup.

Before Planning or execution, a separate local readiness check verifies each
authorized OpenClaw model route without generating content. Its 90-second
model-catalog boundary is infrastructure policy, not part of the TeamPlan and
not time available to an Agent; ordinary preflight commands retain a 30-second
bound. SAT announces this wait and stops before Agent creation if it expires.

## Planned User Journey

```text
run `sat`
→ complete automatic diagnostics and first-run setup when needed
→ describe the desired outcome
→ authorize model-backed planning
→ clarify through dialogue and focused questions
→ review one plan overview
   ├── approve
   ├── request a natural-language revision
   └── edit supported structured fields
→ controller validates requirements, team, routes, permissions, and budgets
→ controller creates run-scoped Agents and starts execution
→ watch run-level and per-Agent progress
→ optionally guide, correct, pause, resume, interrupt, or cancel
→ receive a runnable result or an honest failure/cancellation report
```

The user does not edit JSON, internal prompts, run IDs, workspace roots, or
OpenClaw configuration in the normal flow. Advanced output may expose the
validated plan and resolved model routes for inspection without making those
files prerequisites for starting a build.

`User` is an interaction role at this boundary, not a claim about operator
identity. A project owner, contributor, Codex, or another test driver may
exercise the ordinary flow, provided requests, answers, approval, controls, and
comprehension judgments use only this product surface before privileged state is
inspected or changed. Such evidence validates the interface contract; broader
human-factors generalization is a separate evaluation question.

## Planning Session

### Entry and Authorization

SAT first collects enough non-model input to establish the requested outcome,
execution profile, destination boundary, and authorization to spend model
resources. No model-backed planning call occurs before that authorization.

The Planning session then maintains a versioned proposal. It can use:

- Normal conversational questions when the answer space is open;
- Focused questions with two or three recommended choices;
- A custom-answer option for every focused question;
- Direct confirmation when existing repository evidence already answers a
  question;
- A concise assumption when the choice is reversible and low risk, clearly
  shown in the overview before approval.

Questions should be selected for decision value. The planner must not turn
every implementation detail into a user prompt or silently decide a missing
product requirement on the user's behalf.

If a proposal is returned before a material user-owned ProductDefinition
decision exists, the controller does not expose that field to model correction.
It requests one atomic question for the missing dimension, records the answer in
the ordinary transcript, and revalidates the complete proposal. Independent
model-owned defects remain subject to the normal typed correction contract.

When an answered question or Planner recommendation is represented in a
ProductDefinition dimension but its required decision record does not exist,
the correction authority contains both the atomic dimension and the shared
`decisions` container. Replacing only the dimension could not satisfy that
relation. If an eligible decision record already exists, the shared container
stays immutable and the correction is limited to the dimension that must cite
it.

Every focused question carries a stable decision category, the
evidence that is missing, the material consequences of choosing differently,
and two or three alternatives plus a custom-answer path. The controller derives
the owner from its category-to-authority mapping rather than accepting a second
model-authored field. It rejects questions about reversible local
implementation or scheduling, rejects any attempt to delegate safety or
evidence-integrity policy, and rejects a category whose declared owner does not
match the mapping. This is an admission boundary, not a claim that deterministic
code can infer the semantic value of arbitrary prose; ambiguous real tasks
remain the usability test for under- and over-questioning.

A ProductDefinition question resolves exactly one ProductDefinition dimension.
When several dimensions remain undecided, Planning asks separate questions in
consequence order. This keeps each free-text answer bound to one declared
authority slot: an answer about audience cannot silently erase an already
explicit workflow, maturity, or delivery expectation. The controller enforces
this atomic question boundary before showing the question to the user. The
question text, rationale, missing evidence, consequences, choices, and declared
dimension form one correction authority unit. A defect in any of those related
fields requires replacement of the complete question; changing only its
dimension label cannot reclassify preserved multi-dimensional wording as an
atomic question.

A current proposal records one stable ID per requirement, explicit non-goals,
and attributable decision records. Every current decision has typed provenance:
an exact direct user-input substring, one resolved question ID, `planner`, or
`agent`. The model supplies the semantic category and provenance; the controller
compiles authority from the category instead of asking the model to repeat a
deterministically known field. For direct input, the controller also projects
the exact source into the user-owned summary so a Planner paraphrase cannot be
misattributed to the user. Every answered question resolves exactly one
decision with the original category and owner. Assumptions may reference only
local implementation or scheduling decisions inside the approved boundary;
they cannot substitute for a user authorization or Controller invariant.

Before team design, every current proposal also carries one `ProductDefinition`
with six dimensions:

- `target_users`;
- `primary_workflow`;
- `delivery_maturity`, exactly `throwaway_prototype`,
  `usable_local_product`, or `releasable_small_product`;
- `usability_expectations`;
- `operational_expectations`; and
- `delivery_expectations`.

Each dimension records a statement or maturity level, one of
`explicit_input`, `resolved_question`, `planner_recommendation`, or
`not_material`, attributable source and rationale, and stable downstream
requirement, criterion, and decision references when it is material. A
`not_material` dimension has no downstream references; its source and rationale
instead make the explicit absence of an effect auditable. An explicit-input source and
statement preserve one contiguous verbatim user-input substring without citation
labels, quote delimiters, stitched fragments, or commentary. An unambiguous phrase
such as `one-time throwaway` may map to the matching delivery-maturity enum; the
source remains the user's words rather than invented Controller vocabulary. A
question declares the exact dimension it resolves; each resolved statement must
be an exact fragment of that answer, and
the answer must reach one unchanged user-owned product decision. If an answer
does not cover a declared dimension, Planning must ask again rather than infer it.
Planning cannot silently recommend target users, primary workflow, or maturity.
An exact substring establishes provenance but does not establish semantic
relevance: target users must name an actor that uses or receives the result, and
the primary workflow must describe that actor's activity with the result rather
than a build instruction or delivery qualifier.
Target users may be `not_material` for an explicitly approved throwaway prototype.
The primary workflow is always material, even when it will run only once. Current
Planning accepts only explicit user input or an answered question for that
dimension and requires a downstream requirement reference. Already explicit
activity does not require another question; missing activity requires focused
clarification, not an immaterial disposition. Usability, operations, and delivery may be reasoned
Planner recommendations that become user-approved with the overview.

The source carried by an `explicit_input` or `not_material` dimension is its
decision provenance, so those dimensions do not cite a separate decision ID. A
`resolved_question` dimension cites exactly its matching question decision. A
`planner_recommendation` dimension cites only the corresponding Planner category
(`acceptance_scope` for usability/operations and `delivery` for delivery). This
prevents an unrelated but existing decision ID from satisfying the product-depth
trace. Direct product facts already represented this way are not duplicated in
the additional decision ledger. The model-facing decision schema exposes only
four legal atomic combinations: user-owned categories with direct-input or
resolved-question provenance, Planner-owned categories with Planner provenance,
and Agent-owned categories with Agent provenance. It does not offer a Cartesian
product of categories and provenance kinds that the controller would later have
to reject.

Decision authority and quote authorship are different boundaries. An invalid
model-authored `explicit_input.source` is a semantic citation defect, not proof
that the user omitted a decision. When user input exists, correction may replace
only that source leaf with a contiguous quote from the immutable input; category,
provenance kind, authority, and unrelated proposal fields are not replacement
targets. The existing compiler derives the summary from the corrected quote.
All independently invalid decision quotes are reported together. Invented quotes
remain invalid and repeated non-improvement stops. Missing user input, missing
decision provenance, and unanswered question authority are not model-correctable
user decisions. If validation of a proposed plan exposes one of the three
user-owned product-depth decisions as genuinely missing, the Controller discards
proposal-field correction authority and requests one question-only Planning
response constrained to that exact atomic dimension. The resulting question
returns through the ordinary dialogue before Planning may propose again; it
cannot silently preserve `kind=proposal`, fill the decision itself, or create an
execution Agent. Other model-owned defects in the rejected proposal remain
diagnostic evidence and are revalidated against the next complete proposal.
Product-depth and actual question-answer validation still apply.

This is a dependency contract, not a questionnaire count. An explicit
throwaway prototype can proceed directly with a lean proposal. An
under-specified reusable product must clarify material user-owned depth first.
Every material dimension must change downstream requirements, criteria, or
decisions; the proposal also states its architecture, team, cost, and delivery
effects. A `ProductDefinition` that merely fills fields without changing the
plan is invalid.

### Planning Response Boundary

Planning responses remain strict, but harmless presentation differences are
not treated as reasoning failures. Before schema validation, the controller
may perform only these bounded, semantics-preserving normalizations:

- Infer `kind` when exactly one non-null `question` or `proposal` body makes it
  unambiguous;
- Remove a criterion definition whose exact ID belongs to the active
  controller-owned execution profile, while retaining any task binding to that
  known ID and using only the profile's canonical definition. If the colliding
  model criterion owns the only relation covering a declared requirement, give
  that relation a deterministic non-reserved ID even when its writer task has
  not bound it yet; strict validation then targets the missing task binding
  instead of discarding the requirement relation;
- Canonicalize safe relative `expected_paths` values such as `tests/` to
  `tests`;
- Canonicalize safe `workspace_scope` presentation such as `repository/` to
  `repository`;
- Canonicalize the letter case of an otherwise well-formed `DECISION_` token
  and its exact assumption reference when that canonical identity is unique;
- Compile decision authority from its category, compile legacy question,
  Planner, and Agent source fields into typed provenance, and remove a redundant
  current or legacy direct-product decision only when the ProductDefinition
  retains the exact source under the matching semantic category and no other
  retained relation references that decision. A same-source privacy or risk
  decision remains independent from an operational or delivery product fact;
- Remove decision references from `explicit_input` ProductDefinition dimensions
  when a requirement or criterion trace remains, because their own typed source
  is authoritative;
- Remove every downstream reference from an eligible `not_material`
  ProductDefinition dimension, because retaining one would contradict the
  approved disposition. The primary workflow is never eligible: preserve its
  submitted trace and request an atomic materiality correction instead;
- Compile each current model-facing atomic requirement `{id, description}` into
  the canonical backward-readable description and stable-ID index, and remove
  one or more repeated copies of that exact ID from its description because the
  atomic record already binds identity to meaning;
- Compile each current model-facing atomic assumption `{statement,
  decision_id}` into the canonical backward-readable statement and decision-ID
  indexes. During targeted correction, the `decision_id` schema permits only
  the existing Agent-autonomy decisions retained in the immutable base
  proposal;
- Remove a schema-forbidden field only when removing it cannot grant or hide
  controller/evidence authority.

The current model-facing question contract is enforced again at the Controller
boundary, independently of prompt and tool-schema enforcement. Every current
question must explicitly submit its complete required key set, including an
empty or one-item `product_definition_dimensions` array. Unknown question keys
are not silently discarded because a misspelling can remove or relabel user
decision authority. Older persisted records remain readable through the
versioned internal schema; that compatibility does not weaken new submissions.

The immutable turn retains the exact typed submission, its binding evidence,
and any assistant presentation text separately. It records every normalized
field or removed profile-owned definition, including normalizations completed
before a later validation failure. The active policy is the
only source of IDs eligible for criterion-ownership normalization; the
controller does not compare or adopt the model-authored description,
verification text, or Review boundaries. Absolute paths, backslashes, parent
traversal and ambiguous response bodies remain invalid. Protected authority
fields are rejected rather than normalized away. Other model-owned defects are
eligible only for digest-bound correction of the exact typed fields identified
by validation; the model never regenerates the complete retained object or
selects the fields it may replace.

Reviewer evidence selection is corrected at the same minimum-authority
boundary. Evidence validation collects all independently invalid selectors in
one pass and assigns each a stable grounding invariant, its criterion subject,
and the exact `observable` leaf. A bounded correction may replace those leaf
strings together while every other assessment field and the containing array
remain frozen. For each invalid leaf, the Controller derives a bounded catalog
of exact fragments from protocol-eligible results in the same Review chain. The
model selects an opaque handle; the Controller binds that handle back to the
exact fragment and records the binding. The model therefore decides which
evidence supports its claim without retyping evidence bytes or inventing an
attempt, tool, or command identity. If no eligible candidate exists, SAT does
not spend a random correction call. The Controller cannot guess a replacement
from the Reviewer's summary or source assertions.

The current response schema exposes only one `requirements` array of atomic
`{id, description}` objects. It does not expose a sibling `requirement_ids`
array. This makes requirement cardinality valid by construction at the model
boundary. The Controller compiles the atoms into the existing persisted
description/ID representation so schema-v2 through schema-v8 records retain
their canonical serialization. If a legacy parallel-shaped live response has
unequal cardinality, the only correction target is the complete atomic
requirements relation; an isolated ID-array replacement is never offered.

The same boundary exposes one `assumptions` array of atomic `{statement,
decision_id}` objects and no sibling `assumption_decision_ids` array. An
assumption therefore cannot be generated without exactly one authorizing
Agent-autonomy decision reference, and correction replaces the complete
relation rather than trying to repair two independently generated arrays. The
Controller compiles the atoms into the persisted parallel representation so
older evidence remains readable.

The terminal overview treats every user- or model-authored string as untrusted
display text. Each newline is rendered as an indented continuation of its own
field or list item, and terminal control characters are escaped, so content
cannot impersonate a neighboring section or entry. This changes only the
projection; persisted source text remains exact.

Adaptive Planning and dynamic execution Agents call the invocation-bound
`sat_submit_artifact` tool exactly once through the canonical
`{"artifact": <semantic object>}` transport envelope. The envelope is tool
transport, not part of the semantic response. The plugin accepts exactly that one
object-valued argument and writes only its inner object to the private submission
file. A prompt instruction alone does not make that call mandatory. For a reviewed
model compatibility route, bootstrap Planning forces the exact
`sat_submit_artifact` choice because terminal submission is its only semantic action.
A dynamic Agent instead receives `tool_choice=required`: each model step must call an
authorized tool, but the Agent remains free to use work or evidence tools before its
terminal submission. It leaves model inspection, provider smoke, and legacy
text-compatibility requests unchanged, because those requests do not expose an
invocation binding. SAT gives dynamic Agents the exact AgentSpec-derived semantic JSON Schema
inside the envelope. Planning uses a permissive object-only inner transport
schema for an initial question or proposal so every syntactically structured
response reaches the Controller's exact semantic validator; the private envelope
still binds the exact Planning semantic-schema digest. A targeted Planning
correction instead exposes its exact opaque-handle replacement schema at both the
submission and Controller boundaries. This allows deterministic forbidden-field
normalization and targeted semantic correction to operate after transport without
trusting invalid content. SAT gives every invocation a fresh controller binding, then
requires the private envelope to match the final successful attributable tool call.
The v2 evidence binds the canonical outer tool-arguments digest separately from the
inner semantic-object digest. A direct object, a second `artifact` envelope, or any
other transport shape cannot satisfy both bindings. Visible
assistant payloads remain raw telemetry but are not parsed as Planning or
dynamic semantics, so prose, truncation, or ancillary diagnostics cannot
compete with the submitted object. A schema- or tool-rejected attempt has no
semantic authority and may precede exactly one final successful bound submission;
its failed tool evidence remains attributable. More than one successful
submission is duplicate and ambiguous, and any tool action after a successful
submission makes it non-final. Missing, duplicate-success, non-final, malformed,
all-failed, or unattributable submission sequences fail closed. Only the legacy fixed-role
compatibility path retains the bounded single-object text parser; on that path,
payload count is not mistaken for semantic object count and two real object
candidates remain ambiguous.

The model-facing Planning request contains only the project name, source request,
execution profile, and base constraints. Run/session identity, destination, selected
route, authorization state, and authorization timestamp stay Controller-owned. This
prevents execution-layer secret redaction of authorization-shaped metadata from
changing the persisted prompt and invalidating exact session attribution.

The pinned OpenClaw Agent CLI does not expose a response-schema parameter for a
tool-using turn. SAT supplies the explicit one-argument transport schema through its
isolated submission plugin, binds the exact semantic schema separately, and compiles
submitted values at the controller boundary.
Transport failures and unlocated errors stop. A targetable model-owned failure
produces a content-free diagnostic and a correction request whose persisted
evidence is bound to the retained object's SHA-256. The Controller assigns an
opaque handle to each exact validator-owned JSON-pointer path; the model submits
order-independent `{slot_handle, replacement_value}` records through the same
typed tool under a correction-only semantic schema. The Controller requires exact
handle coverage before applying anything; missing, duplicate, unknown,
cross-response, and legacy positional submissions fail closed. Every model-visible
slot also includes the exact response-schema
fragment for its replacement value and its validator-owned constraints. Independent
ProductDefinition dimension defects are collected in one validation pass, and each
dimension is an atomic replacement boundary because disposition, provenance,
statement, and downstream references must remain coherent. It cannot submit,
replace, widen, or reorder the response identity or private path authority; record
order has no semantic meaning. Derived parent
errors are not copied into a child-field request. Every other value remains
immutable. Product
Planning continues only after every prior validator-owned invariant/subject
identity disappears and a distinct targetable failure remains within the task
budget. A newly exposed relational error is not treated as the same defect merely
because its JSON pointer overlaps the corrected field; each Planning relational
validator supplies a stable invariant ID, structured criterion/task/Agent or
other entity subjects, and the smallest model-owned authority path it can
justify. Error prose is display-only. An unclassified relation fails closed
without guessing a broad replacement. A repeated fingerprint or the same typed
issue stops. Constraint progress is ordered as well: a replacement that falls
back from a valid or semantically constrained value to a coarser type or shape
failure in the same authority slot is a regression, not a new improvement. A
more specific constraint exposed after a coarse schema defect may still proceed.
Controlled evaluation may intentionally impose a zero-or-one correction cap.
Missing-user-decision diagnostics are a separate state transition, not another
semantic correction attempt: each issue retains its own authority, mixed
model/user diagnostics expose no proposal replacement path, and the next
submission schema permits only one `product_requirement` question for the named
ProductDefinition dimension.

Every workspace scope describes controller authority inside the generated
repository: `repository` grants whole-project access and `repository/path`
grants a narrower boundary. A destination or project directory name is not a
workspace scope and is rejected rather than silently widened.

Acceptance criteria have two distinct owners. The Planner defines
task-specific criteria and must bind every one of them to at least one
implementation task. The execution profile defines fixed criteria whose text
and verification contract remain controller-owned; the Planner should not echo
those definitions, but a task may reference a profile criterion ID supplied in
the current Planning context when the task materially implements or verifies
it. A ProductDefinition dimension may reference the same supplied ID when that
fixed obligation is one of the dimension's real downstream effects. If a
response nevertheless repeats an exact active profile ID in its
definition list, the controller never imports that model-authored text as the
profile definition. A redundant echo is removed and audited. If deleting it
would lose a requirement-to-acceptance relation that has a responsible writer,
the controller instead assigns that task-specific criterion a deterministic
non-reserved ID, preserves its model-owned verifier and Review-boundary fields,
and expands the ambiguous task reference to both the task-specific and canonical
profile IDs. Context-free validation then checks ID syntax and complete coverage
of the resulting Planner-owned criteria. The policy-aware controller resolves every
task reference against the union of proposal and current profile IDs. It
rejects any other ID before an overview is shown and preserves valid profile
bindings when it materializes the TaskBrief and implementation plan. A profile
criterion need not be forced onto a task merely because it exists.

Each Planner-owned criterion also declares `review_boundaries`. Most criteria
use an empty list. A description containing an unqualified prohibition or
safety guarantee must declare all four controller-known entry boundaries:
top-level input, nested input, alias or indirection, and failure path. These
obligations are shown in the overview and become part of the confirmed
TaskBrief; they cannot be silently weakened by the execution Reviewer.

Boundary identifiers have controller-owned meanings; they are not casual labels
that an Agent may reinterpret from filesystem depth:

| Identifier | Protocol meaning |
| --- | --- |
| `top_level_input` | The primary input value, object, resource, or entry point selected or supplied directly by the user or upstream caller, before traversal, expansion, or decomposition. If a path or directory is selected as a root, the root itself is the top-level input; an immediate child inside it is already nested input. |
| `nested_input` | An input discovered inside or below the primary input after traversal, expansion, or decomposition; both immediate children and deeper descendants qualify. |
| `alias_or_indirection` | The same logical input reached through an alias, symlink, redirect, wrapper, reference, configuration indirection, or another non-canonical route. |
| `failure_path` | A missing, malformed, invalid, inaccessible, unsupported, rejected, or otherwise failing input or operation; Review checks the observable failure behavior, not merely whether the process avoided a crash. |

`artifacts.py` owns these exact definitions. Planning context, the approval
overview, every runtime Agent context, and public documentation project the
same mapping. A model response can choose relevant obligations and describe a
concrete challenge, but it cannot redefine a boundary.

The `tasks` collection records approved work intent for any runtime Agent.
Implementation and integration Agents must each own at least one task, and
their tasks—not quality-only tasks—must cover every Planner-owned acceptance
criterion. Testing and Review Agents may own tasks that state their verification
focus. Those entries are preserved in the overview and prompt, but they do not
create an Agent, grant tools or write access, expand review scope, choose a
model, set a timeout, or create another model call. Those authorities come only
from the approved `AgentSpec` and controller policy. Every task owner must exist,
the task DAG must be acyclic, and a cross-Agent task dependency is valid only
when the owning Agent depends transitively on the dependency owner. The
controller applies the same binding validation during proposal parsing, prompt
construction, and runner startup.

Testing and Review capabilities are always read-only. Their tasks may describe
inspection, evidence analysis, exercising existing behavior, or review focus,
but every task that creates or changes project code, tests, configuration, or
documentation belongs to an implementation or integration Agent. The overview
prints the effective task authority derived from the owner's `AgentSpec`, so a
free-text task description cannot grant mutation authority or hide a mismatch
from the user before approval.

### Overview Before Execution

The proposal shown before execution contains, in this order:

1. Target users, killer workflow, delivery maturity, usability, operational and
   delivery expectations, explicit non-goals, and the resulting architecture,
   team, cost, and delivery effects;
2. The requested outcome and requirements;
3. Success conditions, constraints, assumptions, and decision provenance split
   by user, Planning, execution autonomy, and non-negotiable Controller policy;
4. The implementation approach, major deliverables, and each task's criterion
   bindings and dependencies;
5. The proposed Agents, why each exists, and what each owns;
6. Agent dependencies, expected handoffs, and independent quality coverage;
7. Permission and workspace boundaries in plain language;
8. Model choices or routing preferences, including any authorized automatic
   selection or switching;
9. The user-approved task USD authorization and optional deadline; controlled
   evaluations additionally show their frozen call, token, duration, and cost
   limits;
10. The delivery destination and expected validation commands.

The clarity gate requires every requirement to reach at least one observable
criterion, every criterion to reach a responsible writer task, and every such
writer to reach a named downstream read-only verifier. The overview renders
that graph and each Agent's inputs, expected output, and handoff. Completeness
is deterministic; whether the presentation is understandable remains a real
user test.

The overview presents controller-owned execution-profile constraints separately
from additional task-specific constraints proposed during Planning. The Planner
is instructed not to repeat, paraphrase, shorten, or broaden the former. Both
collections remain present in the compiled TaskBrief so source labeling improves
the approval experience without discarding a material Planner addition.

The default editor supports natural-language revision and structured changes
to requirements, priorities, Agent responsibilities, dependencies, and model
preferences. Raw system prompts, arbitrary tool grants, and direct policy-file
editing remain an advanced contributor surface. Even advanced changes pass the
same controller validation.

Planning session status follows the product outcome. A terminally invalid
initial dialogue is persisted as `failed`, while a completed invalid revision
restores the preceding valid proposal so it can still be reviewed or approved.
Executor failure, interruption, and cancellation retain their distinct terminal
states and are never converted into revision recovery.

Approval freezes version one of the run contract. Product plans record
`provider_activity` with zero per-Agent wall-clock limits plus the optional
user-authorized whole-run deadline. Controlled evaluations may instead freeze a
positive timeout, its policy envelope, and exact resolution source. Later
corrections create a new version; they never mutate an already referenced plan
in place.

## Versioned Contracts

The Planning, team, event, and control contracts below are executable schemas.
Dynamic prompt compilation and control application build on them rather than
creating a parallel configuration system.

### `TeamPlan`

A `TeamPlan` binds one confirmed TaskBrief and ImplementationPlan to:

- A stable plan ID and revision;
- Run-scoped `AgentSpec` entries;
- A directed acyclic dependency graph;
- Required handoffs and completion conditions;
- Independent verification and review coverage;
- Aggregate iteration and resource budgets;
- A `ModelRoutePlan`;
- The user approval record, controller time-authority resolutions, and planner
  proposal evidence.

### `AgentSpec`

Each `AgentSpec` contains:

- A stable run-scoped Agent ID and user-facing label;
- One distinct responsibility and an explanation of why it is needed;
- Required inputs and typed outputs;
- Predecessors, successors, and scheduling constraints;
- A controlled permission profile;
- Read-only or writable workspace ownership;
- Acceptance responsibility without authority to advance lifecycle state;
- Per-invocation and aggregate budget allocation;
- A model-route reference;
- A prompt-purpose specification compiled with versioned controller templates.

The plan stores prompt intent and inputs, not an unreviewed instruction blob
that can silently grant authority.

### `ModelRoutePlan`

A `ModelRoutePlan` contains:

- User-authorized provider/model candidates;
- A default route;
- Optional task-, phase-, capability-, or Agent-specific overrides;
- Required capabilities such as context size, tool use, structured output, or
  vision input;
- Cost, latency, and quality preferences;
- Explicit switch conditions and limits;
- A strict evaluation-mode flag;
- The deterministic resolution reason recorded for every invocation.

### `RunEvent`

The controller appends structured events with:

- Run ID, sequence, timestamp, and lifecycle revision;
- Agent ID and attempt when applicable;
- Event category and state transition;
- A bounded user-safe activity summary;
- Artifact, handoff, gate, Git, budget, or model-route references;
- Visibility class;
- Optional control-command correlation.

Events are evidence-backed status, not hidden chain-of-thought. An Agent may
return a bounded status summary at defined checkpoints, but the controller
labels its source and never turns free-form reasoning into authoritative state.
An overlong terminal event summary ends at a word boundary when possible and
uses an explicit `… [truncated]` suffix; the complete artifact summary remains
available instead of being silently cut in the user display.

### `ControlCommand`

A control command records:

- Command ID, run ID, request time, and requester;
- Command type and bounded payload;
- Target run, Agent, phase, or future work;
- Requested application boundary;
- `queued`, `applied`, `rejected`, `superseded`, or `best_effort_failed` state;
- Resulting plan revision or lifecycle transition;
- A plain-language consequence and any provider-cost caveat.

## Controller Validation and Agent Creation

After user approval, the controller validates all of the following before an
Agent is created:

- Schema and TaskBrief consistency;
- Complete task coverage of Planner-owned criteria, controller ownership of
  profile criterion definitions, and no task reference outside their known
  union;
- Acyclic dependencies and at least one terminal delivery path;
- Unique writable ownership or an explicit integration protocol;
- Permission profiles compatible with each responsibility;
- Independent quality coverage;
- Ordinary-task USD/deadline authority, host-derived concurrency, and any
  separate controlled-evaluation call, iteration, duration, token, or cost
  limits;
- Available and authorized model routes;
- Prompt inputs that can be reconstructed from versioned templates and
  persisted artifacts;
- No Agent-spawning, lifecycle, credential, deployment, or publication
  authority in an Agent profile.

The controller then materializes run-scoped OpenClaw Agent sessions and
schedules ready nodes in the dependency graph. Agents may recommend another
specialist, but that recommendation is only a plan-amendment request. It does
not create a model call. A team change requires a safe checkpoint, a new
validated TeamPlan revision, budget authorization, and user confirmation when
the change affects scope, cost, or delivery expectations.

Task-specific quality remains semantic work rather than a claim made by the
generic profile gates. Planning must turn every unqualified prohibition or
safety guarantee into acceptance and test intent across relevant entry
boundaries. Implementation must exercise top-level and nested inputs, aliases
or indirection, and failure paths when they apply. Independent Review must
adversarially challenge the same scope. A Dynamic Reviewer returns exactly one
criterion assessment for every assigned criterion, with a concrete negative or
boundary case and observable evidence. Blocked assessments and blocking
findings must cover the same criteria. When exactly one unscoped blocking
finding remains, the controller binds it to every otherwise-uncovered blocked
criterion; multiple unscoped findings are ambiguous and invalid. Missing
coverage is never implicit acceptance. Every assessment must also supply one
or more bounded result fragments. A `semantic_body_v4` assessment returns
`boundary_checks` explicitly. A satisfied criterion must check every boundary
approved in its TaskBrief, with a distinct attributable fragment for each; a
blocked absolute criterion may stop after one grounded counterexample. A
criterion with no approved boundary should return an empty list, and a Reviewer
cannot add or remove obligations. If it nevertheless supplies extra boundary
entries, the controller removes and records them against the frozen TaskBrief
scope before validating their nested content; only approved entries can create
coverage or correction obligations. This makes entry coverage a controller-
validated contract rather than a summary claim. The response schema makes the fragment
structurally required and forbids model-supplied attempt, tool, or command IDs.
Exact text is preferred; a keyed JSON fragment may differ only in RFC JSON
whitespace outside quoted strings. An initial response uses its current
invocation. During a targeted correction, the same Reviewer may also reuse
integrity-checked results captured by
an earlier attempt in that same role-stage, immutable-commit, and invocation
chain. Deterministic command stdout/stderr from the same immutable iteration is
also eligible. Targeted correction derives opaque candidate handles under the
same whole-chain matching policy used by final grounding. A fragment from a
successful result is excluded when the same selector would also match an
ineligible failed tool result or failed deterministic command, so the catalog
cannot offer a handle that the unchanged validator must reject. The controller
requires every fragment to occur in at least one
eligible output, enriches the persisted assessment with every protocol-eligible
actual attempt-qualified tool ID or command ID and available provenance, and
deduplicates repeated or overlapping selectors. Evidence cannot cross an Agent,
stage, iteration, commit, or correction chain. A zero match is invalid; multiple
real matches are preserved instead of delegated back to model wording. Review may
run bounded foreground
probes in the no-network sandbox against the read-only source and temporary
fixtures. General write tools remain denied. Review creates a script or fixture
only through the immutable `sat-probe-write` helper, which accepts a new bounded
canonical direct child matching `/tmp/sat-review-probe-*`, rejects overwrite
and path indirection, and returns an observable success or refusal. The
Reviewer then invokes a Python probe only through the immutable
`sat-probe-run` helper. It validates the owner-only probe, executes an immutable
file descriptor with a fixed interpreter and project working directory, bounds
time and output, frames child stdout separately from child stderr, and emits a
terminal `SAT_PROBE_RESULT_V1` child result.
Project mutation, complex interpreter invocations, background processes, and
network remain unavailable. For a satisfied direct-probe claim, only framed
child stdout and the terminal result are positive evidence; traceback source
text in child stderr cannot establish success. If the same fragment was emitted
by a failed direct probe and a later successful direct probe, the successful
emission supplies the claim while both calls remain auditable. A satisfied
assessment still cannot select a positive substring from any other matched
failed tool result, failed or timed-out deterministic command, or a chain with
no successful probe emission. If the report is already `revise`, has at least
one independently blocked assessment and blocking finding, and an additional
positive assessment fails only this safety check, the controller performs a
monotonic recovery: it re-grounds that assessment as `blocked` against the same
failed evidence and adds a criterion-scoped controller finding requiring fresh
verification. This keeps an otherwise useful revision in the iteration loop
without treating failure as proof of success. It does not apply to `accept` or
`fail`, unmatched selectors, an invalid pre-existing blocker relationship, or
any other semantic defect. That self-directed evidence remains
attributable and is not
relabeled as a controller deterministic gate. Documentation may state only the
boundary established by the implementation and evidence. The generated-project
contract separately checks that the README shows each exact manifest command,
that documented first setup does not leave an unexplained root virtual
environment or lock file in an otherwise clean delivery, and that every lock in
the proposed Git delivery contains no host- or sandbox-only dependency source.
An effectively ignored untracked lock is setup/runtime residue outside that
delivery and is neither parsed as product metadata nor copied into validation
scratch. A deterministic gate
copies clean committed files into fresh scratch, then executes exact setup,
test, and start argv through the runtime's offline wheelhouse. Its start argv
must work from the project root without appended arguments. Independent Review
probes task-specific runtime behavior that this generic contract cannot infer.

Existing fixed team manifests remain versioned evaluation fixtures. During
migration they are compiled into the same `TeamPlan` contract so the repository
does not retain two lifecycle implementations.

The current Git workspace backend uses one detached clone and one Git index.
It may execute read-only ready nodes concurrently, but it must serialize
workspace writers even when their declared path scopes do not overlap. Parallel
writer execution requires isolated worktrees or clones plus an explicit,
controller-verified integration step; the scheduler must not infer Git safety
from path ownership alone.

## Progress and Information Design

### Progress Hierarchy

The display follows one stable hierarchy:

```text
run
└── planning or execution phase
    └── Agent
        └── attempt, activity, handoff, or quality check
```

Every Agent can be `queued`, `ready`, `running`, `launched`, `initializing`,
`waiting_provider`, `tool_active`, `finalizing_response`, `stopping`,
`collecting_evidence`, `stopped`,
`waiting_dependency`, `blocked`, `paused`, `completed`, `waiting_repair`,
`interrupted`, `cancelled`, or `failed`. The interface shows elapsed time, the
last meaningful safe summary, and the dependency or blocker when known. It does
not invent a completion percentage when the controller lacks a meaningful
denominator.

The default standard projection includes one Controller-owned checkpoint
snapshot for active work: the approved task and invocation phase, last verified
checkpoint, next controller-known checkpoint, completed tool-operation count,
Git snapshot, gate and Review state, known task-wide USD spend, authorization,
and remaining amount. A checkpoint is an observed state transition or an
approved future boundary; it is never partial model text, hidden reasoning, a
tool argument, or an Agent-authored progress claim.

Tool start/completion events preserve attributed history, while the phase and
counters in every event describe the current Controller snapshot. The adapter
applies that snapshot before publishing its deltas. If one observation contains
both a start and its completion, the UI may show both historical events, but it
must show zero active tools, the updated completion count, and no synthetic
return to `tool_active`. The live label is derived from an allow-listed tool name
or executable and contains only a bounded action class, target class, and optional
safe executable basename. Arbitrary tool names, full commands, arguments, output,
paths, and secrets are excluded. Identical checkpoint and budget projections are
suppressed within an invocation, but every real `RunEvent` summary remains visible
and every event remains persisted.

### Visibility Levels

The user may change visibility during a run without changing execution:

- `compact`: current run phase, important recovery or stopping transitions,
  budget warnings, blockers, and terminal result;
- `standard` (default): compact information plus every Agent's approved task,
  invocation phase, last and next checkpoints, completed work, Git/gate/Review
  state, elapsed time, and task-wide budget snapshot;
- `detailed`: standard information plus attempt IDs, exact initialization
  checkpoints, dependency transitions, model route, trusted activity counters,
  tool categories, artifact references, and controller validation events.

Raw provider credentials, environment secrets, hidden reasoning, unbounded
model output, and unrelated host information are excluded from every level.
TTY mode may update a live panel; non-TTY mode emits ordered line events using
the same event source. Logs and a future graphical UI must consume that same
contract rather than infer progress independently.

## Input and Interaction Quality

The terminal interaction must remain usable while progress is updating:

- Ask one decision at a time and explain why it matters;
- Mark the recommended answer and its trade-off instead of presenting an
  unexplained list;
- Always accept a custom answer, and support back, revise, skip-when-optional,
  and cancel actions without discarding earlier answers;
- Validate input at the affected question and preserve the user's text after a
  recoverable error;
- Allow concise and multiline natural-language input without requiring JSON or
  shell escaping;
- Suspend live-panel redraw while the user is typing so keystrokes and text are
  never overwritten;
- State when an action will spend model budget, invalidate work, interrupt an
  active attempt, or make cancellation terminal;
- Adapt to narrow terminals and provide a stable line-mode fallback with no
  color or Unicode dependency;
- Keep secrets in trusted provider setup prompts and never echo them into the
  Planning conversation, progress stream, or plan overview.

The current answer set and plan draft are recoverable local state. Returning
to an earlier question creates a new draft revision instead of silently
changing an already approved plan.

## User Controls

The controller owns a local authenticated control mailbox. The foreground TTY
exposes the following line-mode palette after plan approval:

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

Each accepted command is written before the controller applies it. Request
ordering uses a controller-assigned mailbox sequence rather than timestamp or
random command-ID ordering. Any secondary `sat` process that exposes this
control surface must submit the same authenticated contract and preserve the
same ordering; interface availability is reported in `STATUS.md`.
Presentation may evolve, but these semantics remain stable:

### Guide

Adds prospective context for incomplete work at the next safe checkpoint. It
does not silently rewrite confirmed requirements or invalidate completed
evidence. The UI shows which Agents or phases will receive it.

### Correct

Declares that requirements, assumptions, priorities, team design, or model
choices are wrong. The controller stops scheduling new work, preserves the
current evidence, opens a Planning revision, shows the invalidated downstream
work, and requires confirmation before continuing.

### Pause and Resume

Cooperative pause stops new invocations and reaches `paused` after active work
arrives at the selected safe boundary. It does not promise to suspend an
arbitrary provider HTTP request or process instruction instantly. Resume first
revalidates evidence integrity, workspace state, remaining budgets,
dependencies, credentials, and model availability.

### Interrupt

Interrupt requests best-effort termination of one active invocation or Agent
attempt. The attempt and any partial output remain evidence. Provider usage may
already have been incurred. The controller does not retry automatically; the
user chooses whether to replan, retry within budget, continue other independent
work, or cancel. An interrupt is reported as accepted only when it atomically
claims the invocation's terminal stop authority; a process already stopping for
another reason is never presented as user-interrupted.

### Cancel

Cancel is terminal. The controller stops scheduling, requests termination of
active SAT-owned work, cleans only resources proven to belong to that run, and
produces a cancellation report. It preserves evidence and never presents a
partial workspace as an accepted delivery.

## Model Configuration and Routing

Users can maintain multiple secret-free model profiles while credentials stay
inside SAT's isolated provider boundary. Route resolution uses this precedence:

1. An explicit per-Agent profile edit approved in the current TeamPlan;
2. A configured stage override;
3. A configured Agent-capability override;
4. The default profile when it is authorized for that capability;
5. The lowest numeric priority among remaining capability-authorized profiles,
   with saved profile order as a deterministic tie-breaker.

The final step is controller-owned deterministic selection, not an
unconstrained model decision or an unverified quality claim. The bootstrap
Planner describes capability and workload needs but cannot add model fields or
authorize a route. The user may inspect and edit the effective per-Agent
profile in the Planning overview; the controller then validates and freezes
the exact primary and fallback assignments.

Before execution, SAT verifies the bootstrap model and every route authorized
by the approved TeamPlan through its isolated OpenClaw catalog/auth boundary.
Task admission first refreshes every configured route's context capacity and
input/output price. Discovery is preferred; the user is asked only when context
remains unknown, while an unknown price must be supplied or explicitly
confirmed as zero. Every invocation records the canonical provider/model,
route reference, resolution source and reason, frozen price source and
observation time, telemetry, estimated cost, and remaining task authorization.
The standard progress view shows the updated amount after every invocation;
the terminal ledger and report preserve the complete Planning-to-delivery
breakdown by phase, Agent, attempt, route, and model. Unknown usage is retained
as unknown and prevents another user-budget call, but it does not rewrite an
already-attributed initialization, provider, transport, or semantic failure.
When no earlier failure exists, a post-call budget rejection remains the
primary resource-limit reason.
Runtime switching is currently permitted only after an attributable
`provider_failure`, only when the approved Agent assignment lists a next route,
and only within that finite approved route list. The failed invocation is
persisted and budget-accounted before the UI announces the switch and its
possible provider-cost consequence. Targeted semantic correction is a separate,
improvement-gated mechanism. There is no silent fallback.

Controlled evaluation mode remains stricter: one canonical model and price
table are pinned for the run, switching is disabled, and topology trials remain
comparable. Model-routing experiments hold the TaskBrief and TeamPlan constant
and vary only the route policy.

## Persistence and Recovery

Approved plan revisions, RunEvents, ControlCommands, resolved routes, and Agent
creation records become write-once evidence referenced from atomic run state.
Session history remains diagnostic and cannot be the only copy of guidance or
correction.

Durable pause/resume and process-crash recovery require an integrity-checked
checkpoint. Recovery never assumes an unrecorded provider, tool, Git, or
delivery action succeeded. If the exact boundary cannot be proven, SAT reports
the uncertainty and requires a new attempt or run instead of guessing.

## Implementation Batches

### Batch 3A: Contracts and Compatibility Path

- Add versioned TeamPlan, AgentSpec, ModelRoutePlan, RunEvent, and
  ControlCommand schemas;
- Add deterministic validators for DAGs, ownership, permissions, budgets,
  quality independence, and model authorization;
- Compile existing fixed manifests into TeamPlan;
- Move the current workflow onto that contract and remove the direct parallel
  role-list path;
- Persist append-only events while preserving current user behavior.

**Exit:** the current function-specialized offline suite and product path run
through TeamPlan with no behavior regression, and invalid dynamic plans fail
before any model invocation.

### Batch 3B: Planning Dialogue and Overview

- Add model-work authorization followed by multi-round Planning dialogue;
- Support free-form answers, suggested options, and custom responses;
- Produce one requirements, implementation, team, route, and budget overview;
- Support natural-language revision and safe structured edits;
- Freeze the approved plan and provide a non-interactive fixture path for
  deterministic tests.

**Exit:** a user can start from an ordinary request, revise the proposed team,
and approve a complete validated TeamPlan without editing an internal file.

**Compatibility and behavior contract:** the versioned request,
question/proposal response, append-only turn and proposal store,
natural-language revision, safe limit editor, complete overview, and explicit
approval evidence form one path. Bare `sat` activates this interaction together
with Batch 3C, so an approved dynamic plan is the exact plan the controller
executes. Before strict validation, the Planning boundary
infers only an unambiguous response kind, canonicalizes only safe relative-path
presentation, removes redundant active-profile definition echoes, and
deterministically deconflicts an echo whose model-owned relationship is still
needed. The canonical profile text remains controller-owned, both task bindings
are retained, and the raw response plus every normalization remain recorded in
the Planning turn.
Planning schema v9 makes primary-workflow materiality mandatory for new
proposals and their correction slots. Schema-v2 through schema-v8 records retain
their original validation and canonical bytes; only historical preview may use
the old throwaway-workflow exemption. New model responses never use that exemption.
Planning schema v8 added typed decision provenance and moved redundant question
and decision category-to-authority fields out of the model-facing response
contract. It keeps
schema-v2 through schema-v7 records readable without adding fields or changing
their canonical serialization. Current turns retain the exact submission,
record every deterministic compilation, and separately persist the validated
provenance-bearing form. The current live response contract additionally binds
each requirement ID and description in one atomic record; this changes the
submission schema hash, not the persisted Planning schema version.
Planning schema v7 added the exact typed semantic payload and content-free
submission binding to each current turn while preserving assistant text as
non-authoritative evidence. Schema v6 added terminal-response/finalization
execution outcomes to Planning evidence. Schema v5 added the attributable
ProductDefinition and question-to-dimension contract. It retains read support
and canonical serialization for schema-v2 through schema-v6 evidence. Schema
v4 introduced typed validation diagnostics, deterministic normalization, and
targeted-correction evidence.
Current live response schemas make ProductDefinition, responsibility, and
clarity fields mandatory and non-null. Structured edits of historical evidence
preserve its schema identity rather than relabeling it as current.
Blocking model waits emit a concise heartbeat every ten seconds, record when a
response returns, show contract validation, and explicitly announce each exact
correction target. These messages expose elapsed time and controller state, not
prompts or hidden reasoning. The execution adapter additionally projects
content-free provider stream and attributable tool lifecycle activity. A
provider/model-aware renewable silence lease emits a policy-attributed warning,
grace, recovery, or typed stall; Planning turn evidence preserves the same
counters and never stores streamed response content as progress.

### Batch 3C: Dynamic Team Runtime

- Compile run-scoped prompts from AgentSpec and persisted inputs;
- Create only controller-authorized OpenClaw sessions;
- Schedule the dependency graph with bounded concurrency;
- Enforce permission profiles, workspace ownership, typed handoffs, independent
  quality coverage, and aggregate budgets;
- Support versioned team amendments at safe checkpoints.

**Exit:** at least two materially different tasks produce different justified
teams and complete or fail through the same controller, evidence, and cleanup
boundary.

**Runtime contract:** run-scoped Agent identity and capability telemetry,
approved-Agent-only OpenClaw configuration, AgentSpec-derived prompt and
response contracts, exact model and controller time-authority binding, and
AgentSpec-derived cleanup selection share one controller authority. Adaptive plans may use one downstream independent
quality Agent for a small task; separate testing and review Agents remain an
explicit justified choice rather than a hidden minimum topology. Controller
artifact/handoff attribution, bounded DAG dispatch, shared controller-owned
WorkResult/TestReport/ReviewReport assembly, and dynamic iteration aggregation
share the same controller path. Iteration validation requires a chained result from
every approved writer, deterministic evidence from every approved Tester or
from the controller when no Tester exists, evidence from every approved
Reviewer, one immutable quality commit, and complete manual-review coverage.
The dynamic runner binds each scheduler-approved Agent to its exact model,
time authority, prompt, invocation-scoped typed submission schema,
semantic-correction policy, Git or read-only boundary, aggregate
budget, execution record, and durable handoffs. Its persisted events distinguish
launch, attributable initialization checkpoints, provider wait, tool activity,
response finalization, stopping, evidence collection, exact-process cleanup,
and terminal status. Initialization, provider inactivity, and post-response
finalization have separate warning, recovery, and typed failure authorities.
A confirmed stall preserves typed content-free
evidence and may use only an already approved provider-failure fallback; total
productive wall-clock time is not a stopping condition. Quality gates are shared once
per immutable iteration, and every quality Agent must be downstream of every
writer. Controlled evaluation timeout resolution remains separate from the
product's provider-liveness and optional whole-run deadline. The Reviewer runtime keeps project
source read-only and denies the general write tool. Its immutable
`sat-probe-write` command provides the bounded `/tmp` probe-authoring capability
that actually exists in the foreground execution surface. `sat-probe-run`
provides the matching fixed, bounded, controller-verifiable execution path; the
runtime also contains pinned `uv` for relevant bounded probes. The OpenClaw adapter validates
each exact session turn, pairs actual tool calls and results, and persists only
bounded sanitized records. When a process exits after a paired tool result but
before the bound terminal submission, the adapter records
`upstream_incomplete` rather than a generic semantic failure. A write-capable
Agent can continue only in the same task, session, workspace, model route, and
authority after the Controller verifies identity, ancestry, approved path scope,
and a previously unseen content-sensitive workspace state. Every continuation
reserves against the original USD budget and optional deadline and checks for a
user stop first. No progress, repeated state, unsafe state, or exhausted
authority terminates without another call; eventual work still requires the
normal commit, typed submission, gates, Review, delivery, and cleanup. Dynamic
semantic correction may carry those records
forward only within one Reviewer, role stage, immutable commit, and invocation
chain. Every protocol-eligible semantic fragment is bound to an
attempt-qualified controller-owned tool ID; overlapping selectors are
deduplicated, and zero-call
or absent Reviewer evidence is rejected. Complete Agent
summaries remain immutable artifact evidence; the controller derives bounded
scheduler-record and downstream-prompt projections with an explicit truncation
marker, original length, and source-summary SHA-256 rather than failing a
handoff or asking the model to regenerate known content. Its shorter terminal
event projection also uses an explicit truncation suffix and a word boundary
when available. The
adaptive lifecycle coordinator crosses the authoritative snapshot
boundary before the first quality Agent starts, aggregates every approved
output, decides accept/revise/fail, binds prior blocking evidence to the next
iteration's starting commit, stops an unchanged repeated blocker, and writes
the same integrity-checked final evidence and human report as the compatibility
workflow. Bare `sat` authorizes Planning, creates its read-only bootstrap
runtime, presents the complete overview, materializes only an approved
source/run, executes the approved Dynamic Team, delivers only acceptance, and
cleans bootstrap and runtime sandboxes. Plan amendments may apply only at the
validated safe checkpoints defined by the same controller lifecycle.

The current single-clone Git backend serializes every writer and excludes
readers while a writer is active; independently ready read-only quality Agents
may run concurrently up to the user-approved concurrency value and available
host capacity. Ordinary product Planning has no fixed Agent, Reviewer, call, or
iteration maximum. Team size and complementary quality responsibilities come
from the task, risk, dependency graph, and approved USD budget; controlled
evaluation limits remain separate experiment inputs.

### Batch 3D: Observable and Controllable Execution

- Implement compact, standard, and detailed renderers over RunEvent;
- Show every Agent's state, safe activity, dependencies, handoffs, model route,
  gates, and budgets as allowed by visibility;
- Add controller-owned guide, correct, cooperative pause/resume, interrupt, and
  cancel commands;
- Add cancellation, interruption, restart, event-order, non-TTY, and resource-
  cleanup tests.

**Exit:** an offline end-to-end run demonstrates every command and visibility
level with deterministic event evidence; an authorized live run demonstrates
at least guidance and cooperative pause/resume without losing integrity.

**Event and control contract:** append-only events project scheduler queue and
readiness, invocation and provider wait, targeted semantic correction, completion,
failure, and blocked states with Agent dependencies, capability, stage, model,
duration, evidence, and aggregate budget data. Configuration schema v8 selects
compact, standard, or detailed terminal projection. The foreground palette can
change that projection without changing execution. Its persisted runtime
channel applies prospective guidance to the next invocation, drains active work
for safe correction and pause checkpoints, resumes cooperatively, sends
best-effort process-group termination for interrupt or cancel, and records
provider-cost caveats. Correction produces a cancelled superseded-run report
and starts a fresh Planning request with the user's correction preserved;
cancel produces a terminal cancellation report and exact-owned sandbox cleanup
still runs. Heartbeat lifecycle is independent of visibility filtering, hidden
invocation-completed events stop provider waits, and an Agent terminal state
closes every repaired attempt. Gate events distinguish pass from failure in
their symbols instead of marking every completed command with a check mark.

### Batch 3E: Model Profiles and Routing

- Extend secret-free configuration to multiple model profiles;
- Add task, phase, capability, and Agent overrides;
- Add deterministic authorized `auto` resolution and explicit switch policy;
- Record resolved routes, reasons, telemetry, and unavailable-price state;
- Preserve strict pinned-model evaluation mode.

**Exit:** routing tests cover precedence, missing capability, unavailable
provider, budget rejection, authorized switch, refused switch, and strict
evaluation behavior; one authorized run uses two planned routes without silent
fallback.

**Routing contract:** configuration schema v8 owns the secret-free
profiles, attributable price/context metadata, and route policy. Planning resolves and displays an exact assignment
for every Agent, preflight checks every approved model, prompts and response
validation bind the active route, and the runtime records or refuses provider
switches at the controller boundary. Verification must cover precedence,
capability mismatch, unavailable routes, budget rejection, authorized and
refused switching, and strict-mode compatibility.

### Batch 3F: Product Acceptance and Experiment Handoff

- Rehearse fresh installation, Planning dialogue, overview revision, dynamic
  execution, progress, controls, model routing, delivery, and uninstall;
- Record remaining usability and coordination defects;
- Freeze an adaptive-team evaluation configuration;
- Resume fixed-topology comparison, then compare the adaptive team while
  keeping model policy fixed.

**Exit:** a fresh supported device completes the adaptive journey without
internal files or evaluation commands, and the resulting plan, events, model
routes, interventions, Git evidence, quality results, and cleanup are
auditable.

## Acceptance Criteria

The adaptive-orchestration milestone is complete only when:

1. A normal product request does not require the user to select a fixed role
   list or team ID.
2. Planning supports both conversation and focused questions with a custom
   answer path.
3. The user sees and can revise requirements, implementation intent, every
   proposed Agent, dependencies, permissions, model routes, and budgets before
   execution.
4. The controller rejects invalid, cyclic, over-budget, over-privileged,
   unauthenticated, or quality-incomplete plans before Agent creation.
5. All product and fixed evaluation teams execute through one TeamPlan-based
   controller path.
6. Interactive input survives validation errors and progress redraw, supports
   custom/revision/cancellation paths, and has a usable non-TTY line mode.
7. Each Agent has an attributable state and safe current-activity summary, and
   compact, standard, and detailed output consume one event stream.
8. Guide, correction, pause, resume, interrupt, and cancellation have tested,
   persisted, user-visible semantics.
9. Cancellation and interruption clean only SAT-owned resources and preserve
   evidence; provider work and cost that cannot be revoked are reported.
10. Multiple model profiles and route precedence work without exposing secrets
   or silently switching models.
11. Strict evaluation mode pins the model and rejects fallback so controlled
    topology comparisons remain reproducible.
12. Approved plan revisions, events, controls, routes, artifacts, and Git facts
    form one integrity-checked report.
13. Reviewer criterion claims are bound to attributable attempt-qualified tool
    records from the same bounded Reviewer chain; fabricated, cross-Agent,
    cross-stage, cross-commit, mismatched, or unavailable evidence cannot pass
    as accepted Review.
14. Offline tests cover success, correction, pause/recovery, interruption,
    cancellation, invalid plans, routing failures, and non-TTY output, followed
    by at least one explicitly authorized provider-backed acceptance run.

## Non-Goals and Boundaries

This milestone does not authorize:

- Peer Agents to spawn or grant permissions to other Agents;
- Unlimited team size, retries, replanning, model switching, or spending;
- Hidden chain-of-thought or raw secret-bearing runtime output in progress UI;
- Automatic merge, deployment, publication, or external communication;
- A second orchestration implementation alongside the current controller;
- Claims that an automatic model selector is objectively intelligent without
  controlled evidence.

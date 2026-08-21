# Task-Management Web Application Benchmark

## Purpose

Build a small but complete product that exercises requirements analysis,
backend behavior, persistence, server-rendered user interface work, automated
testing, independent review, and revision. The benchmark evaluates the agent
workflow; it is not a prebuilt application template.

The confirmed [`task-brief.json`](task-brief.json) is the authoritative input
given to Agents. This document summarizes that contract and must not introduce
additional implementation requirements.

## Required Technology

- Python 3.12
- FastAPI
- Jinja2 server-rendered HTML
- SQLite persistence
- pytest

The generated application may add narrowly justified dependencies, but it must
not replace this stack or require a hosted service.

## Functional Requirements

1. A user can create a task with a required title and optional description,
   due date, status, and priority.
2. A user can view all persisted tasks and inspect one task's details.
3. A user can edit every supported task field.
4. A user can delete a task through an explicit confirmation step.
5. A user can filter the task list by status and priority.
6. Tasks remain available after the application process restarts.
7. Invalid input produces a useful validation message without losing valid
   submitted values.
8. Missing task identifiers produce a user-visible not-found response.

Statuses are `todo`, `in_progress`, and `done`. Priorities are `low`, `medium`,
and `high`. New tasks default to `todo` and `medium` when those fields are not
provided.

## Acceptance Criteria

- The documented start command launches the application from a clean checkout.
- Database initialization is automatic and repeatable.
- Create, read, update, delete, filtering, persistence, validation, and
  not-found behavior have automated tests.
- All tests and configured static checks pass.
- No credentials or machine-specific absolute paths are required.
- The README explains installation, startup, tests, and known limitations.
- The implementation uses semantic HTML and supports keyboard-only operation
  for the core workflow.

## Evaluation Constraints

- Every configuration receives the same frozen, confirmed task brief and
  starting repository snapshot.
- Model, provider, tool policy, sandbox, acceptance tests, and predefined
  resource limits remain fixed across comparative runs wherever possible.
- The initial comparison covers the single-agent baseline,
  function-specialized team, and implementation-domain-specialized team.
- Generated code must run in an isolated workspace.
- Every material implementation, test, review, integration, and revision
  handoff must be persisted.
- A run may use no more than three implementation iterations.
- External deployment is outside the benchmark and requires human approval.

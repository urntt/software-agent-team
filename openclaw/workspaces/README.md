# Agent Workspace Boundary

`make setup` reconciles one ignored workspace directory for every role
registered in `configs/teams.json`. It removes empty stale role directories and
stops for manual review when a stale directory contains local state. The
sanitized OpenClaw template refers to the stable active paths, but the
directories themselves never enter Git.

OpenClaw state, authentication profiles, session history, and memory remain
outside this repository and must not be committed.

The harness, not an OpenClaw Agent, selects roles and owns workflow state.
Writable developer workspaces and read-only planning or verification
workspaces are validated against the checked-in configuration before a live
run.

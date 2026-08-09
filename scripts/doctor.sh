#!/usr/bin/env bash
set -euo pipefail

task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
task_uv_bin="${UV_BIN:-$HOME/.local/bin/uv}"
task_openclaw_prefix="${OPENCLAW_PREFIX:-$HOME/.openclaw}"
task_openclaw_bin="$task_openclaw_prefix/bin/openclaw"
task_node_bin="$task_openclaw_prefix/tools/node-v24.15.0/bin/node"

fail() {
  echo "doctor: $1" >&2
  exit 1
}

[[ -x "$task_uv_bin" ]] || fail "uv is missing; run 'make setup'"
[[ -x "$task_openclaw_bin" ]] || fail "OpenClaw is missing; run 'make setup'"
[[ -x "$task_node_bin" ]] || fail "the pinned OpenClaw Node runtime is missing"

[[ "$("$task_openclaw_bin" --version)" == *"2026.7.1-2"* ]] || \
  fail "OpenClaw must be version 2026.7.1-2"
[[ "$("$task_node_bin" --version)" == "v24.15.0" ]] || \
  fail "OpenClaw must use Node v24.15.0"

cd "$task_root"
[[ "$(git rev-parse --show-toplevel)" == "$task_root" ]] || \
  fail "this directory must be an independent Git repository"
[[ "$(git branch --show-current)" == "main" ]] || \
  fail "the development branch must be main"
git check-ignore -q AGENTS.md || fail "AGENTS.md must be ignored"
if git ls-files --error-unmatch AGENTS.md >/dev/null 2>&1; then
  fail "AGENTS.md must not be tracked"
fi

[[ -f README.md ]] || fail "README.md is missing"
[[ -f VISION.md ]] || fail "VISION.md is missing"
[[ -f configs/teams.json ]] || fail "team manifest is missing"
[[ -f configs/openclaw.example.json5 ]] || fail "OpenClaw template is missing"

task_python_version="$("$task_uv_bin" run --frozen python -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
[[ "$task_python_version" == "3.12" ]] || fail "the project must use Python 3.12"

if find . \
  \( -path ./.git -o -path ./.venv -o -path ./.pytest_cache -o -path ./.ruff_cache \) \
  -prune -o \
  \( -name '.env' -o -name 'openclaw.json' -o -name '*.pem' -o -name '*.key' \) \
  -print -quit | grep -q .; then
  fail "a credential or active OpenClaw configuration file is present"
fi

SOFTWARE_AGENT_TEAM_ROOT="$task_root" \
  "$task_uv_bin" run --frozen sat validate-config >/dev/null
"$task_uv_bin" run --frozen sat validate-task-brief examples/task-brief.json >/dev/null
"$task_uv_bin" run --frozen sat validate-handoff examples/handoff.json >/dev/null
"$task_uv_bin" run --frozen sat validate-artifact \
  examples/implementation-plan.json >/dev/null

echo "doctor: environment, configuration, and repository boundaries are valid"

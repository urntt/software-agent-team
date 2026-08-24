#!/usr/bin/env bash
set -euo pipefail

task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
task_uv_bin="${UV_BIN:-$HOME/.local/bin/uv}"
task_openclaw_prefix="$task_root/.sat/openclaw"
task_openclaw_bin="$task_openclaw_prefix/bin/openclaw"
task_node_bin="$task_openclaw_prefix/tools/node-v24.15.0/bin/node"
task_openclaw_marker="$task_openclaw_prefix/.sat-owned-runtime"
task_openclaw_probe_home=""
task_openclaw_environment="$task_root/scripts/openclaw-environment.sh"

fail() {
  echo "doctor: $1" >&2
  exit 1
}

[[ -f "$task_openclaw_environment" && ! -L "$task_openclaw_environment" ]] || \
  fail "the OpenClaw environment boundary is missing"
# shellcheck source=scripts/openclaw-environment.sh
source "$task_openclaw_environment"

cleanup() {
  if [[ -n "$task_openclaw_probe_home" && \
    -d "$task_openclaw_probe_home" ]]; then
    rm -rf -- "$task_openclaw_probe_home"
  fi
}
trap cleanup EXIT

[[ -x "$task_uv_bin" ]] || fail "uv is missing; run 'make setup'"
[[ -x "$task_openclaw_bin" ]] || fail "OpenClaw is missing; run 'make setup'"
[[ -x "$task_node_bin" ]] || fail "the pinned OpenClaw Node runtime is missing"
[[ -f "$task_openclaw_marker" && ! -L "$task_openclaw_marker" ]] || \
  fail "the SAT OpenClaw runtime ownership marker is missing"
[[ "$(sed -n '1p' "$task_openclaw_marker")" == \
  "software-agent-team-openclaw-runtime-v1" ]] || \
  fail "the SAT OpenClaw runtime ownership marker is invalid"
[[ "$(sed -n '2p' "$task_openclaw_marker")" == \
  "root=$task_openclaw_prefix" ]] || \
  fail "the SAT OpenClaw runtime marker belongs to another path"

task_openclaw_probe_home="$(mktemp -d "${TMPDIR:-/tmp}/sat-doctor.XXXXXX")"
task_openclaw_version="$(
  sat_run_openclaw_isolated \
    "$task_openclaw_probe_home" \
    "$task_openclaw_probe_home/state" \
    "$task_openclaw_probe_home/state/openclaw.json" \
    "$task_openclaw_bin" --version
)"
[[ "$task_openclaw_version" == *"2026.7.1-2"* ]] || \
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
[[ -f configs/run-policy.json ]] || fail "run policy is missing"
[[ -f configs/product-policy.json ]] || fail "product policy is missing"
[[ -f profiles/python/quality.json ]] || fail "product quality profile is missing"
[[ -f profiles/python/contract-template.json ]] || \
  fail "product contract template is missing"
[[ -f profiles/python/validation/run.py ]] || \
  fail "product contract validator is missing"
[[ -f runtime/python/Dockerfile ]] || fail "Python runtime Dockerfile is missing"
[[ -f runtime/python/requirements.in ]] || \
  fail "Python runtime dependency input is missing"
[[ -f runtime/python/requirements.lock ]] || \
  fail "Python runtime dependency lock is missing"
[[ -f benchmarks/task_manager/benchmark.json ]] || \
  fail "benchmark manifest is missing"
[[ -f benchmarks/task_manager/task-brief.json ]] || \
  fail "frozen benchmark TaskBrief is missing"
[[ -f benchmarks/task_manager/acceptance/run.py ]] || \
  fail "benchmark acceptance suite is missing"
[[ -x scripts/bootstrap.sh ]] || fail "bootstrap script is missing or not executable"
[[ -x scripts/uninstall.sh ]] || fail "uninstall script is missing or not executable"

task_python_version="$("$task_uv_bin" run --frozen python -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
[[ "$task_python_version" == "3.12" ]] || fail "the project must use Python 3.12"

if find . \
  \( -path ./.git -o -path ./.sat -o -path ./.venv -o \
    -path ./.pytest_cache -o -path ./.ruff_cache \) \
  -prune -o \
  \( -name '.env' -o -name 'openclaw.json' -o -name '*.pem' -o -name '*.key' \) \
  -print -quit | grep -q .; then
  fail "a credential or active OpenClaw configuration file is present"
fi

SOFTWARE_AGENT_TEAM_ROOT="$task_root" \
  "$task_uv_bin" run --frozen sat validate-config >/dev/null
SOFTWARE_AGENT_TEAM_ROOT="$task_root" \
  "$task_uv_bin" run --frozen sat validate-config \
  --policy configs/product-policy.json \
  --quality-manifest profiles/python/quality.json >/dev/null
"$task_uv_bin" run --frozen sat validate-task-brief examples/task-brief.json >/dev/null
"$task_uv_bin" run --frozen sat validate-task-brief \
  benchmarks/task_manager/task-brief.json >/dev/null
"$task_uv_bin" run --frozen sat validate-handoff examples/handoff.json >/dev/null
"$task_uv_bin" run --frozen sat validate-artifact \
  examples/implementation-plan.json >/dev/null

echo "doctor: environment, configuration, and repository boundaries are valid"

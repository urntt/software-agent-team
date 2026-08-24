#!/usr/bin/env bash
set -euo pipefail

task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
task_uv_bin="${UV_BIN:-$HOME/.local/bin/uv}"
task_runtime_root="$task_root/.sat"
task_openclaw_prefix="$task_runtime_root/openclaw"
task_openclaw_bin="$task_openclaw_prefix/bin/openclaw"
task_openclaw_marker="$task_openclaw_prefix/.sat-owned-runtime"
task_openclaw_version="2026.7.1-2"
task_node_version="24.15.0"
task_node_bin="$task_openclaw_prefix/tools/node-v$task_node_version/bin/node"
task_installer_home=""
task_openclaw_environment="$task_root/scripts/openclaw-environment.sh"

fail() {
  echo "setup: $1" >&2
  exit 1
}

[[ -f "$task_openclaw_environment" && ! -L "$task_openclaw_environment" ]] || \
  fail "the OpenClaw environment boundary is missing"
# shellcheck source=scripts/openclaw-environment.sh
source "$task_openclaw_environment"

cleanup() {
  if [[ -n "$task_installer_home" && -d "$task_installer_home" ]]; then
    rm -rf -- "$task_installer_home"
  fi
}
trap cleanup EXIT

mkdir -p -- "$task_runtime_root"
[[ -d "$task_runtime_root" && ! -L "$task_runtime_root" ]] || \
  fail "SAT runtime root must be a real directory"
if [[ -e "$task_openclaw_prefix" || -L "$task_openclaw_prefix" ]]; then
  [[ -d "$task_openclaw_prefix" && ! -L "$task_openclaw_prefix" ]] || \
    fail "SAT OpenClaw runtime must be a real directory"
  [[ -f "$task_openclaw_marker" && ! -L "$task_openclaw_marker" ]] || \
    fail "existing OpenClaw runtime is not owned by SAT: $task_openclaw_prefix"
  [[ "$(sed -n '1p' "$task_openclaw_marker")" == \
    "software-agent-team-openclaw-runtime-v1" ]] || \
    fail "SAT OpenClaw runtime marker is invalid"
  [[ "$(sed -n '2p' "$task_openclaw_marker")" == \
    "root=$task_openclaw_prefix" ]] || \
    fail "SAT OpenClaw runtime marker belongs to a different path"
else
  mkdir -m 700 -- "$task_openclaw_prefix"
  {
    echo "software-agent-team-openclaw-runtime-v1"
    echo "root=$task_openclaw_prefix"
  } > "$task_openclaw_marker"
  chmod 600 "$task_openclaw_marker"
fi

task_installer_home="$(mktemp -d "$task_runtime_root/.install-home.XXXXXX")"

if [[ ! -x "$task_uv_bin" ]]; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$HOME/.local/bin" sh
fi

task_installed_openclaw_version="$(
  sat_run_openclaw_isolated \
    "$task_installer_home" \
    "$task_installer_home/state" \
    "$task_installer_home/state/openclaw.json" \
    "$task_openclaw_bin" --version 2>/dev/null || true
)"
task_installed_node_version="$("$task_node_bin" --version 2>/dev/null || true)"
if [[ ! -x "$task_openclaw_bin" ]] || \
  [[ "$task_installed_openclaw_version" != *"$task_openclaw_version"* ]] || \
  [[ "$task_installed_node_version" != "v$task_node_version" ]]; then
  curl -fsSL --proto '=https' --tlsv1.2 https://openclaw.ai/install-cli.sh | \
    sat_run_openclaw_isolated \
      "$task_installer_home" \
      "$task_installer_home/state" \
      "$task_installer_home/state/openclaw.json" \
      bash -s -- \
      --prefix "$task_openclaw_prefix" \
      --version "$task_openclaw_version" \
      --node-version "$task_node_version" \
      --no-onboard
fi

[[ -x "$task_openclaw_bin" ]] || fail "SAT OpenClaw binary is missing after setup"
[[ "$(sat_run_openclaw_isolated \
  "$task_installer_home" \
  "$task_installer_home/state" \
  "$task_installer_home/state/openclaw.json" \
  "$task_openclaw_bin" --version)" == \
  *"$task_openclaw_version"* ]] || \
  fail "SAT OpenClaw version does not match the pinned version"
[[ -x "$task_node_bin" ]] || fail "SAT OpenClaw Node runtime is missing after setup"
[[ "$("$task_node_bin" --version)" == "v$task_node_version" ]] || \
  fail "SAT OpenClaw Node version does not match the pinned version"
[[ -f "$task_openclaw_marker" && ! -L "$task_openclaw_marker" ]] || \
  fail "SAT OpenClaw runtime ownership marker was removed during setup"

cd "$task_root"
"$task_uv_bin" python install 3.12
"$task_uv_bin" sync --locked
mkdir -p openclaw/workspaces

SOFTWARE_AGENT_TEAM_ROOT="$task_root" \
  "$task_uv_bin" run --frozen python - <<'PY'
from pathlib import Path

from software_agent_team.teams import load_team_manifest

manifest = load_team_manifest(Path("configs/teams.json"))
workspace_root = Path("openclaw/workspaces")
expected = {role.value for role in manifest.required_roles}

for path in workspace_root.iterdir():
    if not path.is_dir() or path.name in expected:
        continue
    contents = list(path.iterdir())
    if contents and not all(
        item.is_file() and item.name == ".gitkeep" for item in contents
    ):
        raise SystemExit(
            f"stale role workspace is not empty; move or remove it manually: {path}"
        )
    for marker in contents:
        marker.unlink()
    path.rmdir()

for role_name in sorted(expected):
    (workspace_root / role_name).mkdir(parents=True, exist_ok=True)
PY

echo "Development environment is ready with an isolated SAT OpenClaw runtime."
echo "Run 'make check' next."

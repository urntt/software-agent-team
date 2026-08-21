#!/usr/bin/env bash
set -euo pipefail

task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
task_uv_bin="${UV_BIN:-$HOME/.local/bin/uv}"
task_openclaw_prefix="${OPENCLAW_PREFIX:-$HOME/.openclaw}"
task_openclaw_bin="$task_openclaw_prefix/bin/openclaw"
task_openclaw_version="2026.7.1-2"
task_node_version="24.15.0"

if [[ ! -x "$task_uv_bin" ]]; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="$HOME/.local/bin" sh
fi

task_installed_openclaw_version="$("$task_openclaw_bin" --version 2>/dev/null || true)"
if [[ ! -x "$task_openclaw_bin" ]] || \
  [[ "$task_installed_openclaw_version" != *"$task_openclaw_version"* ]]; then
  curl -fsSL --proto '=https' --tlsv1.2 https://openclaw.ai/install-cli.sh | \
    bash -s -- \
      --prefix "$task_openclaw_prefix" \
      --version "$task_openclaw_version" \
      --node-version "$task_node_version" \
      --no-onboard
fi

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

echo "Development environment is ready. Run 'make check' next."

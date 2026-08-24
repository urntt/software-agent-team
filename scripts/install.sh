#!/usr/bin/env bash
set -euo pipefail

task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
task_uv_bin="${UV_BIN:-$HOME/.local/bin/uv}"
task_openclaw_prefix="$task_root/.sat/openclaw"
task_bin_dir="${SAT_BIN_DIR:-$HOME/.local/bin}"
task_sat_target="$task_root/.venv/bin/sat"
task_sat_link="$task_bin_dir/sat"
task_uninstall_target="$task_root/scripts/uninstall.sh"
task_uninstall_link="$task_bin_dir/sat-uninstall"

fail() {
  echo "install: $1" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "$1 is required"
}

[[ "$(uname -s)" == "Linux" ]] || fail "only Linux and WSL are supported"
task_architecture="$(uname -m)"
case "$task_architecture" in
  x86_64|amd64|aarch64|arm64) ;;
  *) fail "unsupported architecture: $task_architecture" ;;
esac
[[ "$(id -u)" != "0" && "$(id -g)" != "0" ]] || \
  fail "run the installer as an unprivileged user"

for task_command in bash curl docker git; do
  require_command "$task_command"
done

[[ -w "$task_root" ]] || fail "the checkout is not writable by the current user"
[[ -f "$task_root/pyproject.toml" ]] || fail "pyproject.toml is missing"
[[ -f "$task_root/uv.lock" ]] || fail "uv.lock is missing"
[[ -f "$task_root/configs/product-policy.json" ]] || \
  fail "product policy is missing"
[[ -f "$task_root/profiles/python/quality.json" ]] || \
  fail "product quality profile is missing"
[[ -f "$task_root/profiles/python/contract-template.json" ]] || \
  fail "product contract template is missing"
[[ -f "$task_root/profiles/python/validation/run.py" ]] || \
  fail "product contract validator is missing"
[[ -f "$task_root/profiles/python/seed/pyproject.toml" ]] || \
  fail "product source seed is missing"
[[ -f "$task_root/runtime/python/Dockerfile" ]] || \
  fail "Python runtime Dockerfile is missing"
[[ -f "$task_root/runtime/python/requirements.lock" ]] || \
  fail "Python runtime dependency lock is missing"
[[ -f "$task_root/scripts/openclaw-environment.sh" && \
  ! -L "$task_root/scripts/openclaw-environment.sh" ]] || \
  fail "OpenClaw environment boundary is missing"
[[ -x "$task_uninstall_target" ]] || fail "uninstall script is missing or not executable"
if [[ "${SAT_MANAGED_INSTALL:-0}" == "1" ]]; then
  [[ -f "$task_root/.sat-managed-install" && \
    ! -L "$task_root/.sat-managed-install" ]] || \
    fail "managed installation marker is missing"
fi
[[ "$task_bin_dir" == /* && "$task_bin_dir" != "/" ]] || \
  fail "SAT_BIN_DIR must be a specific absolute directory"

validate_link_destination() {
  local task_link="$1"
  local task_target="$2"
  local task_label="$3"
  if [[ -L "$task_link" ]]; then
    [[ "$(readlink "$task_link")" == "$task_target" ]] || \
      fail "$task_label already points to a different installation: $task_link"
  elif [[ -e "$task_link" ]]; then
    fail "$task_label already exists and will not be overwritten: $task_link"
  fi
}

validate_link_destination "$task_sat_link" "$task_sat_target" "sat"
validate_link_destination \
  "$task_uninstall_link" "$task_uninstall_target" "sat-uninstall"

docker info >/dev/null 2>&1 || \
  fail "Docker daemon is unavailable to this user; start Docker and grant access"
task_docker_os="$(docker info --format '{{.OSType}}' 2>/dev/null || true)"
[[ "$task_docker_os" == "linux" ]] || \
  fail "Docker must be running Linux containers"

if ! "$task_root/scripts/setup.sh"; then
  fail "pinned toolchain setup failed; check download connectivity and retry"
fi

cd "$task_root"
task_image="$(
  "$task_uv_bin" run --frozen python -c \
    'import json; from pathlib import Path; print(json.loads(Path("configs/product-policy.json").read_text(encoding="utf-8"))["sandbox"]["image"])'
)"
[[ -n "$task_image" && "$task_image" != -* && "$task_image" != *[$'\t\r\n ']* ]] || \
  fail "run policy contains an invalid Docker image reference"

if ! docker build \
    --pull=false \
    --tag "$task_image" \
    runtime/python; then
  fail "sandbox image build failed; inspect Docker output and retry"
fi
task_image_id="$(docker image inspect --format '{{.Id}}' "$task_image")"
[[ "$task_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || \
  fail "Docker returned an invalid product image ID"

"$task_uv_bin" run --frozen sat validate-config >/dev/null
"$task_uv_bin" run --frozen sat validate-config \
  --policy configs/product-policy.json \
  --quality-manifest profiles/python/quality.json >/dev/null
"$task_uv_bin" run --frozen ruff format --check .
"$task_uv_bin" run --frozen ruff check .
"$task_uv_bin" run --frozen pytest

[[ -x "$task_sat_target" ]] || fail "the locked project environment has no sat CLI"
mkdir -p -- "$task_bin_dir"
if [[ ! -L "$task_sat_link" ]]; then
  ln -s "$task_sat_target" "$task_sat_link"
fi
if [[ ! -L "$task_uninstall_link" ]]; then
  ln -s "$task_uninstall_target" "$task_uninstall_link"
fi
"$task_sat_link" --help >/dev/null

echo "install: Software Agent Team is ready"
echo "install: sat=$task_sat_link"
echo "install: openclaw=$task_openclaw_prefix/bin/openclaw"
echo "install: image=$task_image"
echo "install: image_id=$task_image_id"
echo "install: existing OpenClaw installations and configuration were not read or changed"
echo "install: SAT provider credentials and configuration will use isolated state on first run"
if [[ "${SAT_MANAGED_INSTALL:-0}" != "1" ]]; then
  echo "install: uninstall=sat-uninstall"
  case ":$PATH:" in
    *":$task_bin_dir:"*) echo "install: next=sat" ;;
    *)
      echo "install: launcher directory is not active in this shell: $task_bin_dir"
      echo "install: open a new login shell, then run sat"
      echo "install: next=$task_sat_link"
      ;;
  esac
fi

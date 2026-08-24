#!/usr/bin/env bash
set -euo pipefail

task_repository="${SAT_REPOSITORY_URL:-https://github.com/urntt/software-agent-team.git}"
task_ref="${SAT_INSTALL_REF:-main}"
task_data_root="${XDG_DATA_HOME:-$HOME/.local/share}"
task_install_root="${SAT_INSTALL_ROOT:-$task_data_root/software-agent-team/app}"
task_marker="$task_install_root/.sat-managed-install"
task_temporary=""

fail() {
  echo "bootstrap: $1" >&2
  exit 1
}

cleanup() {
  if [[ -n "$task_temporary" && -d "$task_temporary" ]]; then
    rm -rf -- "$task_temporary"
  fi
}
trap cleanup EXIT

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "$1 is required"
}

[[ "$(uname -s)" == "Linux" ]] || fail "only Linux and WSL are supported"
[[ "$(id -u)" != "0" && "$(id -g)" != "0" ]] || \
  fail "run the installer as an unprivileged user"
for task_command in bash curl git; do
  require_command "$task_command"
done
[[ "$task_install_root" == /* && "$task_install_root" != "/" ]] || \
  fail "SAT_INSTALL_ROOT must be a specific absolute directory"
[[ -n "$task_repository" && "$task_repository" != -* ]] || \
  fail "SAT_REPOSITORY_URL is invalid"
[[ "$task_ref" =~ ^[A-Za-z0-9._/-]+$ && "$task_ref" != -* ]] || \
  fail "SAT_INSTALL_REF is invalid"

task_install_parent="$(dirname "$task_install_root")"
mkdir -p -- "$task_install_parent"
[[ -d "$task_install_parent" && ! -L "$task_install_parent" ]] || \
  fail "installation parent must be a real directory"
task_install_parent="$(cd "$task_install_parent" && pwd -P)"
task_install_root="$task_install_parent/$(basename "$task_install_root")"
task_marker="$task_install_root/.sat-managed-install"

if [[ -e "$task_install_root" || -L "$task_install_root" ]]; then
  [[ -d "$task_install_root" && ! -L "$task_install_root" ]] || \
    fail "installation root must be a real directory"
  [[ -f "$task_marker" && ! -L "$task_marker" ]] || \
    fail "existing installation root is not owned by SAT: $task_install_root"
  [[ "$(sed -n '1p' "$task_marker")" == "software-agent-team-managed-v1" ]] || \
    fail "managed installation marker is invalid"
  [[ "$(sed -n '2p' "$task_marker")" == "root=$task_install_root" ]] || \
    fail "managed installation marker belongs to a different path"
  [[ -d "$task_install_root/.git" ]] || \
    fail "managed installation is missing Git metadata"
  [[ -z "$(git -C "$task_install_root" status --porcelain --untracked-files=all)" ]] || \
    fail "managed installation contains unexpected file changes"
  git -C "$task_install_root" fetch --depth 1 origin "$task_ref" || \
    fail "could not update SAT; check Git access and network connectivity"
  git -C "$task_install_root" checkout --detach --force FETCH_HEAD
else
  task_temporary="$(mktemp -d "$task_install_parent/.sat-bootstrap.XXXXXX")"
  if ! git clone \
      --depth 1 \
      --branch "$task_ref" \
      --single-branch \
      -- \
      "$task_repository" \
      "$task_temporary/app"; then
    fail "could not download SAT; check Git access and network connectivity"
  fi
  {
    echo "software-agent-team-managed-v1"
    echo "root=$task_install_root"
  } > "$task_temporary/app/.sat-managed-install"
  mv -- "$task_temporary/app" "$task_install_root"
fi

(
  cd "$task_install_root"
  SAT_MANAGED_INSTALL=1 ./scripts/install.sh
)

echo "bootstrap: managed application=$task_install_root"
echo "bootstrap: uninstall=sat-uninstall"
echo "bootstrap: next=sat"

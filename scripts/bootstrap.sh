#!/usr/bin/env bash
set -euo pipefail

task_release_tag="__SAT_RELEASE_TAG__"
task_release_revision="__SAT_RELEASE_REVISION__"
task_repository="${SAT_REPOSITORY_URL:-https://github.com/urntt/software-agent-team.git}"
task_channel="${SAT_INSTALL_CHANNEL:-stable}"
task_ref="${SAT_INSTALL_REF:-main}"
task_bootstrap_ref="${SAT_BOOTSTRAP_REF:-main}"
task_release_api_url="${SAT_RELEASE_API_URL:-}"
task_data_root="${XDG_DATA_HOME:-$HOME/.local/share}"
task_install_root_override="${SAT_INSTALL_ROOT:-}"
task_install_root="${task_install_root_override:-$task_data_root/software-agent-team/app}"
task_uv_bin="${UV_BIN:-$HOME/.local/bin/uv}"
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
[[ -n "$task_repository" && "$task_repository" != -* && \
  "$task_repository" != *[$'\t\r\n ']* ]] || \
  fail "SAT_REPOSITORY_URL is invalid"
[[ "$task_channel" == "stable" || "$task_channel" == "dev" ]] || \
  fail "SAT_INSTALL_CHANNEL must be stable or dev"
if [[ "$task_channel" == "stable" ]]; then
  [[ "$task_release_tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ && \
    "$task_release_revision" =~ ^[0-9a-f]{40}$ ]] || \
    fail "stable bootstrap requires the published release bootstrap.sh asset"
  [[ -z "${SAT_BOOTSTRAP_REF:-}" || "$SAT_BOOTSTRAP_REF" == "$task_release_tag" ]] || \
    fail "SAT_BOOTSTRAP_REF cannot override a stable release helper"
  task_bootstrap_ref="$task_release_tag"
  task_release_api_url="${task_release_api_url:-https://api.github.com/repos/urntt/software-agent-team/releases/tags/$task_release_tag}"
else
  task_release_api_url="${task_release_api_url:-https://api.github.com/repos/urntt/software-agent-team/releases/latest}"
fi
[[ "$task_ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$ ]] || \
  fail "SAT_INSTALL_REF is invalid"
[[ "$task_bootstrap_ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$ ]] || \
  fail "SAT_BOOTSTRAP_REF is invalid"
[[ "$task_release_api_url" == https://* && \
  "$task_release_api_url" != *[$'\t\r\n ']* ]] || \
  fail "SAT_RELEASE_API_URL must be an HTTPS URL"

task_install_parent="$(dirname "$task_install_root")"
mkdir -p -- "$task_install_parent"
[[ -d "$task_install_parent" && ! -L "$task_install_parent" ]] || \
  fail "installation parent must be a real directory"
task_install_parent="$(cd "$task_install_parent" && pwd -P)"
task_install_root="$task_install_parent/$(basename "$task_install_root")"
task_temporary="$(mktemp -d "${TMPDIR:-/tmp}/sat-bootstrap.XXXXXX")"

if ! git clone \
    --depth 1 \
    --branch "$task_bootstrap_ref" \
    --single-branch \
    -- \
    "$task_repository" \
    "$task_temporary/helper"; then
  fail "could not download the SAT bootstrap helper; check Git and network access"
fi
if [[ "$task_channel" == "stable" ]]; then
  task_helper_revision="$(git -C "$task_temporary/helper" rev-parse HEAD)" || \
    fail "could not inspect the stable bootstrap helper revision"
  [[ "$task_helper_revision" == "$task_release_revision" ]] || \
    fail "stable bootstrap helper revision differs from the published release"
  echo "bootstrap: release=$task_release_tag revision=$task_release_revision"
else
  echo "bootstrap: helper_ref=$task_bootstrap_ref"
fi

if [[ ! -x "$task_uv_bin" ]]; then
  bash "$task_temporary/helper/scripts/install-uv.sh" "$task_uv_bin" || \
    fail "could not prepare uv; check the verified download diagnostic above"
fi
[[ -x "$task_uv_bin" ]] || fail "uv is unavailable after bootstrap"

(
  cd "$task_temporary/helper"
  "$task_uv_bin" python install 3.12
  "$task_uv_bin" sync --locked --no-dev
  task_arguments=(
    _managed-install
    --channel "$task_channel"
    --repository "$task_repository"
    --release-api-url "$task_release_api_url"
  )
  if [[ "$task_channel" == "dev" ]]; then
    task_arguments+=(--ref "$task_ref")
  fi
  if [[ -n "$task_install_root_override" ]]; then
    SAT_INSTALL_ROOT="$task_install_root" \
      "$task_uv_bin" run --frozen --no-dev sat "${task_arguments[@]}"
  else
    "$task_uv_bin" run --frozen --no-dev sat "${task_arguments[@]}"
  fi
)

echo "bootstrap: managed application=$task_install_root"
echo "bootstrap: channel=$task_channel"
# This helper can install a release predating the canonical uninstall subcommand.
echo "bootstrap: uninstall=sat-uninstall"
echo "bootstrap: next=sat"

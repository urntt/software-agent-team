#!/usr/bin/env bash
set -euo pipefail

task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
task_bin_dir="${SAT_BIN_DIR:-$HOME/.local/bin}"
task_sat_target="$task_root/.venv/bin/sat"
task_sat_link="$task_bin_dir/sat"
task_uninstall_target="$task_root/scripts/uninstall.sh"
task_uninstall_link="$task_bin_dir/sat-uninstall"
task_runs_root="$task_root/runs"
task_workspaces_root="$task_root/workspaces"
task_xdg_config_root="${XDG_CONFIG_HOME:-$HOME/.config}"
task_config_path="${SAT_CONFIG_PATH:-$task_xdg_config_root/software-agent-team/config.json}"

task_export_to=""
task_config_policy="keep"
task_data_policy="keep"
task_config_policy_explicit=0
task_data_policy_explicit=0
task_assume_yes=0

fail() {
  echo "uninstall: $1" >&2
  exit 1
}

show_help() {
  cat <<'EOF'
Usage: sat-uninstall [options]

Remove the checkout-bound SAT launcher and Python environment. By default,
saved SAT configuration and generated runs/workspaces are preserved.

Options:
  --export-to PATH  Export SAT configuration and default generated data first.
                    PATH must be an absolute path that does not already exist.
  --keep-config     Preserve saved SAT configuration (default).
  --purge-config    Delete saved SAT configuration after an optional export.
  --keep-data       Preserve default runs/ and workspaces/ data (default).
  --purge-data      Delete default runs/ and workspaces/ after an optional export.
  --yes             Accept the selected policies without interactive prompts.
  -h, --help        Show this help.

The export intentionally excludes provider credentials. OpenClaw, uv, Docker,
the benchmark image, this source checkout, and any custom run roots are shared
or operator-owned and are never removed by this command.
EOF
}

while (($#)); do
  case "$1" in
    --export-to)
      (($# >= 2)) || fail "--export-to requires a path"
      task_export_to="$2"
      shift 2
      ;;
    --keep-config)
      [[ "$task_config_policy_explicit" == "0" ]] || \
        fail "choose only one configuration policy"
      task_config_policy="keep"
      task_config_policy_explicit=1
      shift
      ;;
    --purge-config)
      [[ "$task_config_policy_explicit" == "0" ]] || \
        fail "choose only one configuration policy"
      task_config_policy="purge"
      task_config_policy_explicit=1
      shift
      ;;
    --keep-data)
      [[ "$task_data_policy_explicit" == "0" ]] || \
        fail "choose only one data policy"
      task_data_policy="keep"
      task_data_policy_explicit=1
      shift
      ;;
    --purge-data)
      [[ "$task_data_policy_explicit" == "0" ]] || \
        fail "choose only one data policy"
      task_data_policy="purge"
      task_data_policy_explicit=1
      shift
      ;;
    --yes)
      task_assume_yes=1
      shift
      ;;
    -h|--help)
      show_help
      exit 0
      ;;
    *)
      fail "unknown option: $1"
      ;;
  esac
done

[[ "$(uname -s)" == "Linux" ]] || fail "only Linux and WSL are supported"
[[ "$(id -u)" != "0" && "$(id -g)" != "0" ]] || \
  fail "run the uninstaller as the unprivileged user who installed SAT"
[[ "$task_root" == /* && "$task_root" != "/" ]] || \
  fail "installation root must be a specific absolute directory"
[[ "$task_bin_dir" == /* && "$task_bin_dir" != "/" ]] || \
  fail "SAT_BIN_DIR must be a specific absolute directory"
[[ "$task_config_path" == /* && "$task_config_path" != "/" ]] || \
  fail "SAT_CONFIG_PATH and XDG_CONFIG_HOME must resolve to a specific absolute path"

ask_yes_no() {
  local task_prompt="$1"
  local task_answer
  while true; do
    read -r -p "$task_prompt [y/N] " task_answer || return 1
    case "$task_answer" in
      y|Y|yes|YES|Yes) return 0 ;;
      ""|n|N|no|NO|No) return 1 ;;
      *) echo "Please answer y or n." ;;
    esac
  done
}

if [[ "$task_assume_yes" == "0" ]]; then
  [[ -t 0 && -t 1 ]] || fail "interactive confirmation is unavailable; use --yes"
  echo "SAT will remove its launchers and checkout-specific Python environment."
  echo "OpenClaw, provider credentials, uv, Docker, the image, and source stay intact."

  if [[ -z "$task_export_to" ]] && \
    ask_yes_no "Export saved SAT configuration and default generated data first?"; then
    read -r -p "Absolute export destination (must not exist): " task_export_to
  fi
  if [[ "$task_config_policy_explicit" == "0" ]] && \
    ask_yes_no "Delete saved SAT configuration after export?"; then
    task_config_policy="purge"
  fi
  if [[ "$task_data_policy_explicit" == "0" ]] && \
    ask_yes_no "Delete default runs and workspaces after export?"; then
    task_data_policy="purge"
  fi

  echo "Configuration policy: $task_config_policy"
  echo "Generated-data policy: $task_data_policy"
  echo "Export destination: ${task_export_to:-none}"
  if ! ask_yes_no "Continue with uninstall?"; then
    echo "uninstall: cancelled; nothing was changed"
    exit 0
  fi
fi

validate_export_destination() {
  [[ "$task_export_to" == /* && "$task_export_to" != "/" ]] || \
    fail "--export-to must be a specific absolute path"
  [[ ! -e "$task_export_to" && ! -L "$task_export_to" ]] || \
    fail "export destination already exists: $task_export_to"
  local task_export_parent
  local task_export_name
  task_export_parent="$(dirname "$task_export_to")"
  task_export_name="$(basename "$task_export_to")"
  [[ "$task_export_name" != "." && "$task_export_name" != ".." ]] || \
    fail "export destination must name a new directory"
  [[ -d "$task_export_parent" && ! -L "$task_export_parent" ]] || \
    fail "export destination parent must be an existing real directory"
  task_export_parent="$(cd "$task_export_parent" && pwd -P)"
  task_export_to="$task_export_parent/$task_export_name"
  case "$task_export_to/" in
    "$task_root/"*) fail "export destination must be outside the source checkout" ;;
  esac
}

export_user_state() {
  validate_export_destination
  mkdir -m 700 -- "$task_export_to"
  local task_config_exported="no"
  local task_runs_exported="no"
  local task_workspaces_exported="no"
  if [[ -f "$task_config_path" ]]; then
    mkdir -m 700 -- "$task_export_to/configuration"
    cp -p -- "$task_config_path" "$task_export_to/configuration/config.json"
    task_config_exported="yes"
  fi
  if [[ -d "$task_runs_root" ]]; then
    mkdir -m 700 -- "$task_export_to/data"
    cp -a -- "$task_runs_root" "$task_export_to/data/runs"
    task_runs_exported="yes"
  fi
  if [[ -d "$task_workspaces_root" ]]; then
    mkdir -p -m 700 -- "$task_export_to/data"
    cp -a -- "$task_workspaces_root" "$task_export_to/data/workspaces"
    task_workspaces_exported="yes"
  fi
  {
    echo "Software Agent Team uninstall export"
    echo "created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "configuration=$task_config_exported"
    echo "runs=$task_runs_exported"
    echo "workspaces=$task_workspaces_exported"
    echo "provider_credentials=excluded"
    echo "custom_run_roots=excluded"
  } > "$task_export_to/EXPORT.txt"
  chmod 600 "$task_export_to/EXPORT.txt"
  echo "uninstall: exported preserved state to $task_export_to"
}

[[ ! -L "$task_root/.venv" ]] || \
  fail "refusing to remove a symbolic-link project environment"
if [[ -n "$task_export_to" || "$task_config_policy" == "purge" ]]; then
  [[ ! -L "$task_config_path" ]] || \
    fail "refusing to export or delete a symbolic-link configuration"
fi
if [[ -n "$task_export_to" || "$task_data_policy" == "purge" ]]; then
  [[ ! -L "$task_runs_root" && ! -L "$task_workspaces_root" ]] || \
    fail "refusing to export or delete symbolic-link generated-data directories"
fi

if [[ -n "$task_export_to" ]]; then
  export_user_state
fi

if [[ "$task_config_policy" == "purge" ]]; then
  rm -f -- "$task_config_path"
  echo "uninstall: deleted SAT configuration $task_config_path"
else
  echo "uninstall: preserved SAT configuration $task_config_path"
fi

if [[ "$task_data_policy" == "purge" ]]; then
  rm -rf -- "$task_runs_root" "$task_workspaces_root"
  echo "uninstall: deleted default runs and workspaces"
else
  echo "uninstall: preserved default runs and workspaces"
fi

remove_owned_link() {
  local task_link="$1"
  local task_target="$2"
  local task_label="$3"
  if [[ -L "$task_link" ]]; then
    if [[ "$(readlink "$task_link")" == "$task_target" ]]; then
      rm -f -- "$task_link"
      echo "uninstall: removed $task_label $task_link"
    else
      echo "uninstall: preserved unrelated $task_label $task_link"
    fi
  elif [[ -e "$task_link" ]]; then
    echo "uninstall: preserved unrelated $task_label $task_link"
  fi
}

remove_owned_link "$task_sat_link" "$task_sat_target" "launcher"
if [[ -d "$task_root/.venv" ]]; then
  rm -rf -- "$task_root/.venv"
  echo "uninstall: removed checkout-specific Python environment"
fi
remove_owned_link "$task_uninstall_link" "$task_uninstall_target" \
  "uninstall launcher"

echo "uninstall: shared OpenClaw, provider credentials, uv, Docker, and image preserved"
echo "uninstall: source checkout preserved; remove it separately only after review"

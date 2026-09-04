#!/usr/bin/env bash
set -euo pipefail

task_script_path="$(readlink -f -- "${BASH_SOURCE[0]}")" || {
  echo "uninstall: cannot resolve the uninstall script path" >&2
  exit 1
}
task_root="$(cd "$(dirname "$task_script_path")/.." && pwd)"
task_bin_dir="${SAT_BIN_DIR:-$HOME/.local/bin}"
task_sat_target="$task_root/.venv/bin/sat"
task_sat_link="$task_bin_dir/sat"
task_uninstall_target="$task_root/scripts/uninstall.sh"
task_uninstall_link="$task_bin_dir/sat-uninstall"
task_xdg_config_root="${XDG_CONFIG_HOME:-$HOME/.config}"
task_config_path="${SAT_CONFIG_PATH:-$task_xdg_config_root/software-agent-team/config.json}"
task_xdg_state_root="${XDG_STATE_HOME:-$HOME/.local/state}"
task_state_root="${SAT_STATE_ROOT:-$task_xdg_state_root/software-agent-team}"
task_runs_root="$task_state_root/runs"
task_workspaces_root="$task_state_root/workspaces"
task_sources_root="$task_state_root/sources"
task_planning_root="$task_state_root/planning"
task_provider_state_root="$task_state_root/openclaw"
task_state_marker="$task_state_root/.sat-state-v1"
task_runtime_root="$task_root/.sat"
task_openclaw_runtime="$task_runtime_root/openclaw"
task_openclaw_runtime_marker="$task_openclaw_runtime/.sat-owned-runtime"
task_managed_marker="$task_root/.sat-managed-install"
task_managed_install=0
task_managed_root=""
task_versions_root=""
task_installation_record=""
task_update_lock_fd=""
task_application_link=""

task_export_to=""
task_config_policy="keep"
task_data_policy="keep"
task_provider_policy="keep"
task_config_policy_explicit=0
task_data_policy_explicit=0
task_provider_policy_explicit=0
task_assume_yes=0

fail() {
  echo "uninstall: $1" >&2
  exit 1
}

show_help() {
  cat <<'EOF'
Usage: sat-uninstall [options]

Remove SAT launchers, its application environment, and its private OpenClaw
binary. By default, saved SAT configuration, generated data, and SAT's isolated
OpenClaw provider state are preserved.

Options:
  --export-to PATH  Export SAT configuration and default generated data first.
                    PATH must be an absolute path that does not already exist.
  --keep-config     Preserve saved SAT configuration (default).
  --purge-config    Delete saved SAT configuration after an optional export.
  --keep-data       Preserve runs, workspaces, sources, and Planning evidence
                    (default).
  --purge-data      Delete generated data after an optional export.
  --keep-provider-state
                    Preserve SAT's isolated OpenClaw credentials and sessions
                    (default).
  --purge-provider-state
                    Delete only SAT's isolated OpenClaw credentials and sessions.
  --yes             Accept the selected policies without interactive prompts.
  -h, --help        Show this help.

The export intentionally excludes provider credentials. OpenClaw installations
outside SAT's marked private runtime, uv, Docker, the shared quality image,
development checkouts, and custom state roots not selected through SAT_STATE_ROOT
are never read or removed. A managed application directory is removed; a
development checkout is preserved.
EOF
}

load_managed_v2_paths() {
  local task_python="$task_root/.venv/bin/python"
  [[ -x "$task_python" && ! -L "$task_managed_marker" ]] || \
    fail "managed installation metadata cannot be verified"
  task_versions_root="$(cd "$(dirname "$task_root")" && pwd -P)"
  task_managed_root="$(cd "$task_versions_root/.." && pwd -P)"
  local task_root_marker="$task_managed_root/.sat-managed-root"
  [[ -f "$task_root_marker" && ! -L "$task_root_marker" ]] || \
    fail "managed root ownership marker is missing or invalid"

  local task_metadata
  if ! task_metadata="$("$task_python" - \
    "$task_managed_marker" \
    "$task_root_marker" \
    "$task_root" \
    "$task_versions_root" \
    "$task_managed_root" <<'PY'
import json
import os
import stat
import sys
from pathlib import Path

release_path, root_path, release_root, versions_root, managed_root = map(
    Path, sys.argv[1:]
)


def load_exact(path: Path, keys: set[str], label: str) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"{label} is unreadable: {error}")
    if not isinstance(payload, dict) or set(payload) != keys:
        raise SystemExit(f"{label} has an unsupported schema")
    return payload


def absolute_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or value != value.strip():
        raise SystemExit(f"{label} is invalid")
    if any(ord(character) < 32 for character in value):
        raise SystemExit(f"{label} contains control text")
    path = Path(value)
    if not path.is_absolute() or path == Path(path.anchor):
        raise SystemExit(f"{label} is not a specific absolute path")
    if os.path.normpath(value) != value:
        raise SystemExit(f"{label} is not normalized")
    return path


release = load_exact(
    release_path,
    {
        "schema_version",
        "application_link",
        "channel",
        "release_version",
        "source_revision",
        "source_ref",
        "repository_url",
        "artifact_digest",
    },
    "managed release marker",
)
root = load_exact(
    root_path,
    {
        "schema_version",
        "managed_root",
        "application_link",
        "versions_root",
        "installation_record",
        "bin_directory",
    },
    "managed root marker",
)
if release["schema_version"] != 2 or root["schema_version"] != 1:
    raise SystemExit("managed marker schema is unsupported")

application = absolute_path(root["application_link"], "application link")
record_path = absolute_path(root["installation_record"], "installation record")
bin_directory = absolute_path(root["bin_directory"], "launcher directory")
if absolute_path(root["managed_root"], "managed root") != managed_root:
    raise SystemExit("managed root marker belongs to a different root")
if absolute_path(root["versions_root"], "versions root") != versions_root:
    raise SystemExit("managed root marker belongs to different version storage")
if release_root.parent != versions_root or versions_root.parent != managed_root:
    raise SystemExit("managed release is outside its owned version storage")
if release["application_link"] != str(application):
    raise SystemExit("managed release and root markers disagree")
if not application.is_symlink() or Path(os.path.realpath(application)) != release_root:
    raise SystemExit("managed application link does not select this release")

if os.path.lexists(record_path):
    record_stat = os.lstat(record_path)
    if not stat.S_ISREG(record_stat.st_mode):
        raise SystemExit("installation record is not a regular file")
    record = load_exact(
        record_path,
        {
            "schema_version",
            "install_mode",
            "channel",
            "release_version",
            "source_revision",
            "source_ref",
            "repository_url",
            "application_path",
            "artifact_digest",
            "installed_at",
        },
        "installation record",
    )
    if record["schema_version"] != 1 or record["install_mode"] != "managed":
        raise SystemExit("installation record schema is unsupported")
    shared = {
        "channel",
        "release_version",
        "source_revision",
        "source_ref",
        "repository_url",
        "artifact_digest",
    }
    if any(record[field] != release[field] for field in shared):
        raise SystemExit("installation record and release marker disagree")
    if record["application_path"] != str(application):
        raise SystemExit("installation record belongs to a different application")

print(f"{application}\t{record_path}\t{bin_directory}")
PY
  )"; then
    fail "managed installation metadata cannot be verified"
  fi
  local task_record_path
  local task_recorded_bin
  IFS=$'\t' read -r \
    task_application_link task_record_path task_recorded_bin <<<"$task_metadata"
  [[ -n "$task_application_link" && -n "$task_record_path" && \
    -n "$task_recorded_bin" ]] || \
    fail "managed installation metadata is incomplete"
  task_bin_dir="$task_recorded_bin"
  task_installation_record="$task_record_path"
  task_sat_target="$task_application_link/.venv/bin/sat"
  task_uninstall_target="$task_application_link/scripts/uninstall.sh"
  task_sat_link="$task_bin_dir/sat"
  task_uninstall_link="$task_bin_dir/sat-uninstall"
  task_managed_install=2
}

acquire_managed_lifecycle_lock() {
  command -v flock >/dev/null 2>&1 || \
    fail "flock is required to uninstall a managed application"
  local task_lock="$task_managed_root/update.lock"
  [[ -f "$task_lock" && ! -L "$task_lock" ]] || \
    fail "managed lifecycle lock is missing or invalid"
  exec {task_update_lock_fd}<>"$task_lock"
  flock -n "$task_update_lock_fd" || \
    fail "another managed install or update is active"
}

refuse_active_managed_runs() {
  local task_python="$task_root/.venv/bin/python"
  if ! "$task_python" - "$task_runs_root" <<'PY'
import json
import os
import stat
import sys
from pathlib import Path

runs = Path(sys.argv[1])
if not os.path.lexists(runs):
    raise SystemExit(0)
mode = os.lstat(runs).st_mode
if not stat.S_ISDIR(mode):
    raise SystemExit("run state root is not a real directory")
active: list[str] = []
for path in sorted(runs.glob("*/run.json")):
    entry_mode = os.lstat(path).st_mode
    if not stat.S_ISREG(entry_mode):
        raise SystemExit(f"run state is not a regular file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        phase = payload["phase"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise SystemExit(f"run state cannot be verified: {path}: {error}")
    if phase not in {"completed", "failed"}:
        active.append(path.parent.name)
if active:
    raise SystemExit("active SAT run blocks uninstall: " + ", ".join(active))
PY
  then
    fail "managed run state prevents uninstall"
  fi
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
    --keep-provider-state)
      [[ "$task_provider_policy_explicit" == "0" ]] || \
        fail "choose only one provider-state policy"
      task_provider_policy="keep"
      task_provider_policy_explicit=1
      shift
      ;;
    --purge-provider-state)
      [[ "$task_provider_policy_explicit" == "0" ]] || \
        fail "choose only one provider-state policy"
      task_provider_policy="purge"
      task_provider_policy_explicit=1
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
[[ "$task_state_root" == /* && "$task_state_root" != "/" ]] || \
  fail "SAT_STATE_ROOT and XDG_STATE_HOME must resolve to a specific absolute path"
if [[ -e "$task_managed_marker" || -L "$task_managed_marker" ]]; then
  [[ -f "$task_managed_marker" && ! -L "$task_managed_marker" ]] || \
    fail "managed installation marker must be a regular file"
  if [[ "$(sed -n '1p' "$task_managed_marker")" == \
    "software-agent-team-managed-v1" ]]; then
    [[ "$(sed -n '2p' "$task_managed_marker")" == "root=$task_root" ]] || \
      fail "managed installation marker belongs to a different path"
    task_managed_install=1
  else
    load_managed_v2_paths
  fi
fi
if [[ "$task_managed_install" == "2" ]]; then
  acquire_managed_lifecycle_lock
  refuse_active_managed_runs
  [[ "$task_managed_root" != "/" && "$task_managed_root" != "$HOME" && \
    "$task_managed_root" != "$(dirname "$HOME")" ]] || \
    fail "refusing to remove an unsafe managed application root"
fi

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

validate_state_ownership() {
  if [[ ! -e "$task_state_root" && ! -L "$task_state_root" ]]; then
    return
  fi
  [[ -d "$task_state_root" && ! -L "$task_state_root" ]] || \
    fail "SAT state root must be a real directory"
  [[ -f "$task_state_marker" && ! -L "$task_state_marker" ]] || \
    fail "SAT state root is missing its ownership marker"
  local task_resolved_state
  task_resolved_state="$(cd "$task_state_root" && pwd -P)"
  [[ "$(sed -n '1p' "$task_state_marker")" == \
    "software-agent-team-state-v1" ]] || \
    fail "SAT state ownership marker is invalid"
  [[ "$(sed -n '2p' "$task_state_marker")" == \
    "root=$task_resolved_state" ]] || \
    fail "SAT state ownership marker belongs to a different path"
}

validate_runtime_ownership() {
  if [[ ! -e "$task_runtime_root" && ! -L "$task_runtime_root" ]]; then
    return
  fi
  [[ -d "$task_runtime_root" && ! -L "$task_runtime_root" ]] || \
    fail "SAT runtime root must be a real directory"
  if [[ ! -e "$task_openclaw_runtime" && ! -L "$task_openclaw_runtime" ]]; then
    return
  fi
  [[ -d "$task_openclaw_runtime" && ! -L "$task_openclaw_runtime" ]] || \
    fail "SAT OpenClaw runtime must be a real directory"
  [[ -f "$task_openclaw_runtime_marker" && \
    ! -L "$task_openclaw_runtime_marker" ]] || \
    fail "SAT OpenClaw runtime is missing its ownership marker"
  [[ "$(sed -n '1p' "$task_openclaw_runtime_marker")" == \
    "software-agent-team-openclaw-runtime-v1" ]] || \
    fail "SAT OpenClaw runtime ownership marker is invalid"
  [[ "$(sed -n '2p' "$task_openclaw_runtime_marker")" == \
    "root=$task_openclaw_runtime" ]] || \
    fail "SAT OpenClaw runtime marker belongs to a different path"
}

if [[ "$task_assume_yes" == "0" ]]; then
  [[ -t 0 && -t 1 ]] || fail "interactive confirmation is unavailable; use --yes"
  echo "SAT will remove its launchers, Python environment, and private OpenClaw binary."
  echo "Any OpenClaw installation outside SAT remains untouched."

  if [[ -z "$task_export_to" ]] && \
    ask_yes_no "Export saved SAT configuration and default generated data first?"; then
    read -r -p "Absolute export destination (must not exist): " task_export_to
  fi
  if [[ "$task_config_policy_explicit" == "0" ]] && \
    ask_yes_no "Delete saved SAT configuration after export?"; then
    task_config_policy="purge"
  fi
  if [[ "$task_data_policy_explicit" == "0" ]] && \
    ask_yes_no "Delete runs, workspaces, sources, and Planning evidence after export?"; then
    task_data_policy="purge"
  fi
  if [[ "$task_provider_policy_explicit" == "0" ]] && \
    ask_yes_no "Delete SAT's isolated OpenClaw credentials and sessions?"; then
    task_provider_policy="purge"
  fi

  echo "Configuration policy: $task_config_policy"
  echo "Generated-data policy: $task_data_policy"
  echo "Isolated provider-state policy: $task_provider_policy"
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
    "$task_state_root/"*) fail "export destination must be outside SAT state" ;;
  esac
  if [[ "$task_managed_install" == "2" ]]; then
    case "$task_export_to/" in
      "$task_managed_root/"*) \
        fail "export destination must be outside the managed application root" ;;
    esac
  fi
}

export_user_state() {
  validate_export_destination
  mkdir -m 700 -- "$task_export_to"
  local task_config_exported="no"
  local task_runs_exported="no"
  local task_workspaces_exported="no"
  local task_sources_exported="no"
  local task_planning_exported="no"
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
  if [[ -d "$task_sources_root" ]]; then
    mkdir -p -m 700 -- "$task_export_to/data"
    cp -a -- "$task_sources_root" "$task_export_to/data/sources"
    task_sources_exported="yes"
  fi
  if [[ -d "$task_planning_root" ]]; then
    mkdir -p -m 700 -- "$task_export_to/data"
    cp -a -- "$task_planning_root" "$task_export_to/data/planning"
    task_planning_exported="yes"
  fi
  {
    echo "Software Agent Team uninstall export"
    echo "created_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "configuration=$task_config_exported"
    echo "runs=$task_runs_exported"
    echo "workspaces=$task_workspaces_exported"
    echo "sources=$task_sources_exported"
    echo "planning=$task_planning_exported"
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
if [[ -n "$task_export_to" || "$task_data_policy" == "purge" || \
  "$task_provider_policy" == "purge" ]]; then
  validate_state_ownership
  [[ ! -L "$task_state_root" && ! -L "$task_runs_root" && \
    ! -L "$task_workspaces_root" && ! -L "$task_sources_root" && \
    ! -L "$task_planning_root" && \
    ! -L "$task_provider_state_root" ]] || \
    fail "refusing to export or delete symbolic-link SAT state directories"
fi
validate_runtime_ownership

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
  rm -rf -- "$task_runs_root" "$task_workspaces_root" "$task_sources_root" \
    "$task_planning_root"
  echo "uninstall: deleted runs, workspaces, sources, and Planning evidence"
else
  echo "uninstall: preserved runs, workspaces, sources, and Planning evidence"
fi

if [[ "$task_provider_policy" == "purge" ]]; then
  rm -rf -- "$task_provider_state_root"
  echo "uninstall: deleted SAT's isolated OpenClaw provider state"
else
  echo "uninstall: preserved SAT's isolated OpenClaw provider state"
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
if [[ "$task_managed_install" != "2" && -d "$task_root/.venv" ]]; then
  rm -rf -- "$task_root/.venv"
  echo "uninstall: removed SAT Python environment"
fi
if [[ "$task_managed_install" != "2" && -d "$task_openclaw_runtime" ]]; then
  rm -rf -- "$task_openclaw_runtime"
  rmdir -- "$task_runtime_root" 2>/dev/null || true
  echo "uninstall: removed SAT's private OpenClaw runtime"
fi
remove_owned_link "$task_uninstall_link" "$task_uninstall_target" \
  "uninstall launcher"

echo "uninstall: other OpenClaw installations, uv, Docker, and image untouched"
if [[ "$task_managed_install" == "2" ]]; then
  [[ -L "$task_application_link" && \
    "$(readlink -f -- "$task_application_link")" == "$task_root" ]] || \
    fail "managed application link changed during uninstall"
  rm -f -- "$task_application_link"
  if [[ -e "$task_installation_record" || -L "$task_installation_record" ]]; then
    [[ -f "$task_installation_record" && ! -L "$task_installation_record" ]] || \
      fail "installation record changed during uninstall"
    rm -f -- "$task_installation_record"
  fi
  rm -rf -- "$task_managed_root"
  echo "uninstall: removed managed SAT application $task_application_link"
elif [[ "$task_managed_install" == "1" ]]; then
  [[ "$task_root" != "$HOME" && "$task_root" != "$(dirname "$HOME")" ]] || \
    fail "refusing to remove an unsafe managed installation root"
  rm -rf -- "$task_root"
  echo "uninstall: removed managed SAT application $task_root"
else
  echo "uninstall: development checkout preserved"
fi

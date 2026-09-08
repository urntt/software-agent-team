#!/usr/bin/env bash

# Run one OpenClaw command without inheriting another installation's settings.
sat_run_openclaw_isolated() {
  if (($# < 4)); then
    echo "openclaw environment: home, state, config, and command are required" >&2
    return 2
  fi
  local task_private_home="$1"
  local task_private_state="$2"
  local task_private_config="$3"
  shift 3

  local -a task_clean_environment=(env)
  local -a task_openclaw_environment_names=("${!OPENCLAW_@}")
  local task_environment_name
  # Enumerate names in this shell. An asynchronous process substitution can
  # outlive a fast parent exit and leave an unreaped child at the caller boundary.
  task_openclaw_environment_names+=(PI_CODING_AGENT_DIR)
  # Package lifecycle also treats systemd's STATE_DIRECTORY as cleanup authority.
  task_openclaw_environment_names+=(STATE_DIRECTORY NODE_OPTIONS NODE_PATH NODE_COMPILE_CACHE NODE_DISABLE_COMPILE_CACHE)
  for task_environment_name in "${task_openclaw_environment_names[@]}"; do
    task_clean_environment+=(-u "$task_environment_name")
  done

  "${task_clean_environment[@]}" \
    HOME="$task_private_home" \
    OPENCLAW_AGENT_DIR= \
    OPENCLAW_AUTH_PROFILE_SECRET_DIR="$task_private_state/credentials" \
    OPENCLAW_CONFIG_DIR="$task_private_state" \
    OPENCLAW_CONFIG_PATH="$task_private_config" \
    OPENCLAW_HOME="$task_private_home" \
    OPENCLAW_OAUTH_DIR="$task_private_state/credentials" \
    OPENCLAW_PROFILE= \
    OPENCLAW_STATE_DIR="$task_private_state" \
    OPENCLAW_WORKSPACE_DIR="$task_private_state/workspace" \
    PI_CODING_AGENT_DIR= \
    NODE_DISABLE_COMPILE_CACHE=1 \
    "$@"
}

# The private launcher re-enables caching at its installation-owned path.
# Direct Node probes remain uncached rather than touching a caller's cache.
sat_prepare_openclaw_compile_cache() {
  local task_cache_prefix="$1"
  local task_cache="$task_cache_prefix/compile-cache"
  [[ -d "$task_cache_prefix" && ! -L "$task_cache_prefix" && -O "$task_cache_prefix" ]] || {
    echo "setup runtime: compile cache requires an owned runtime directory" >&2; return 1;
  }
  if [[ ! -e "$task_cache" && ! -L "$task_cache" ]]; then
    mkdir -m 700 -- "$task_cache" || return 1
  fi
  [[ -d "$task_cache" && ! -L "$task_cache" && -O "$task_cache" && -w "$task_cache" &&
     "$(stat -c %a "$task_cache")" == 700 ]] || {
    echo "setup runtime: compile cache must be a private, writable owned directory" >&2; return 1;
  }
  unset NODE_COMPILE_CACHE NODE_DISABLE_COMPILE_CACHE
  export NODE_COMPILE_CACHE="$task_cache"
}

sat_publish_openclaw_launcher() {
  local task_prefix="$1" task_node="$2" task_entry="$3"
  local task_boundary="$4" task_launcher
  sat_prepare_openclaw_compile_cache "$task_prefix" || return 1
  [[ ! -L "$task_prefix/bin" ]] || return 1
  mkdir -p "$task_prefix/bin" || return 1
  task_launcher="$(mktemp "$task_prefix/bin/.launcher.XXXXXX")" || return 1
  # Reuse the same checked-in cache authority for direct and Controller launches.
  if ! printf '#!/usr/bin/env bash\nset -euo pipefail\nsource %q\nunset NODE_OPTIONS NODE_PATH STATE_DIRECTORY\nsat_prepare_openclaw_compile_cache %q\nexec %q %q "$@"\n' \
      "$task_boundary" "$task_prefix" "$task_node" "$task_entry" > "$task_launcher" ||
     ! chmod 700 "$task_launcher" ||
     ! mv -T "$task_launcher" "$task_prefix/bin/openclaw"; then
    rm -f -- "$task_launcher"
    return 1
  fi
}

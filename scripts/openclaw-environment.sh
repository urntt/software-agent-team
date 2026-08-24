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
  local task_environment_name
  while read -r task_environment_name; do
    case "$task_environment_name" in
      OPENCLAW_*|PI_CODING_AGENT_DIR)
        task_clean_environment+=(-u "$task_environment_name")
        ;;
    esac
  done < <(compgen -e)

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
    "$@"
}

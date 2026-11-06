"""Default paths, configuration locations, and limits for the SAT CLI."""

from __future__ import annotations

from pathlib import Path

from software_agent_team.paths import user_state_root

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TEAM_CONFIG = PROJECT_ROOT / "configs/teams.json"
DEFAULT_OPENCLAW_CONFIG = PROJECT_ROOT / "configs/openclaw.example.json5"
DEFAULT_RUN_POLICY = PROJECT_ROOT / "configs/run-policy.json"
DEFAULT_BENCHMARK = PROJECT_ROOT / "benchmarks/task_manager/benchmark.json"
DEFAULT_BENCHMARK_SEED = PROJECT_ROOT / "benchmarks/task_manager/seed"
DEFAULT_PRODUCT_POLICY = PROJECT_ROOT / "configs/product-policy.json"
DEFAULT_PRODUCT_PROFILE = PROJECT_ROOT / "profiles/python/quality.json"
DEFAULT_PRODUCT_SEED = PROJECT_ROOT / "profiles/python/seed"
DEFAULT_STATE_ROOT = user_state_root()
DEFAULT_RUNS_ROOT = DEFAULT_STATE_ROOT / "runs"
DEFAULT_WORKSPACES_ROOT = DEFAULT_STATE_ROOT / "workspaces"
DEFAULT_OPENCLAW_BINARY = PROJECT_ROOT / ".sat" / "openclaw" / "bin" / "openclaw"
EVALUATION_ITERATION_LIMIT = 2

"""Team manifest loading, persistence, and handoff boundary verification.

团队配置清单 (Manifest) 的加载、反序列化以及跨阶段交接 (Handoff) 验证。
"""

from __future__ import annotations

import json
from pathlib import Path

from .common import _clean_unique_text, _require_utc
from .definitions import TeamDefinition, TeamManifest, TeamStage


def load_team_manifest(path: Path) -> TeamManifest:
    """Load and validate a team manifest from JSON.

    从 JSON 文件加载并严格验证团队配置清单。
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    return TeamManifest.model_validate(payload)


__all__ = [
    "TeamDefinition",
    "TeamManifest",
    "TeamStage",
    "_clean_unique_text",
    "_require_utc",
    "load_team_manifest",
]

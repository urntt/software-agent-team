"""Canonical integrity helpers shared by persisted controller contracts."""

import hashlib
import json

from pydantic import BaseModel


def canonical_model_sha256(model: BaseModel) -> str:
    """Hash one validated model using stable JSON field and key ordering."""

    canonical = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()

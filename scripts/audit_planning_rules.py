"""Summarize content-free Planning rule triggers from persisted turns.

The input may contain prompts, model responses, and credentials. This tool only
emits allowlisted rule identities and aggregate outcomes; it never echoes input.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

_NORMALIZATION_PREFIXES: tuple[tuple[str, str], ...] = (
    ("framed bare ", "response_envelope"),
    ("inferred response kind as ", "response_kind"),
    ("compiled question.decision_owner from category ", "question_authority"),
    ("compiled cross-Agent task dependencies", "task_dependency_projection"),
    ("compiled atomic proposal.requirements", "atomic_requirements"),
    ("removed redundant stable ID prefix", "requirement_id_prefix"),
    ("compiled atomic proposal.assumptions", "atomic_assumptions"),
    ("compiled proposal.product_definition.", "product_definition_source"),
    ("removed downstream references from not-material", "product_nonmaterial_refs"),
    ("canonicalized proposal.decisions[", "decision_id"),
    ("canonicalized proposal.assumption_decision_ids[", "assumption_decision_id"),
    ("canonicalized proposal.product_definition.", "product_decision_id"),
    ("compiled proposal.decisions[", "decision_compilation"),
    ("retired legacy proposal.decisions[", "legacy_question_id"),
    ("removed redundant decision references", "product_decision_refs"),
    ("removed redundant direct-input decision", "duplicate_direct_decision"),
    ("deconflicted model criterion ", "profile_criterion_collision"),
    ("removed controller-owned profile criterion", "profile_criterion_echo"),
    ("canonicalized proposal.tasks[", "task_path"),
    ("canonicalized proposal.agents[", "agent_workspace_path"),
    ("compiled proposal.acceptance_criteria[", "specialist_verifier_projection"),
    ("compiled proposal.tasks[", "review_scope_projection"),
    ("decoded JSON-serialized ", "correction_json_decode"),
    ("bound controller evidence candidate ", "correction_candidate_binding"),
    ("preserved existing valid Agents", "specialist_agent_preservation"),
    ("deconflicted added ", "specialist_agent_identity"),
    ("compiled added ", "specialist_agent_dependencies"),
    ("preserved existing writer", "writer_task_preservation"),
    ("removed schema-forbidden field ", "schema_forbidden_field"),
)


def normalization_rule_id(value: str) -> str:
    """Map persisted legacy descriptions to a bounded, content-free identity."""

    if value.startswith("compiled proposal.decisions["):
        for field in ("authority", "summary", "provenance"):
            if f"].{field} " in value:
                return f"decision_{field}_compilation"
    for prefix, rule_id in _NORMALIZATION_PREFIXES:
        if value.startswith(prefix):
            return rule_id
    return "unclassified_normalization"


def summarize_turns(paths: list[Path], *, source_version: str) -> dict[str, object]:
    """Count rule triggers and outcomes without retaining sensitive payloads."""

    counts: Counter[tuple[str, ...]] = Counter()
    unclassified = 0
    unclassified_validation = 0
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError("Planning turn input must be a regular file")
        turn = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(turn, dict):
            raise ValueError("Planning turn input must be an object")
        execution = turn.get("execution")
        if not isinstance(execution, dict):
            raise ValueError("Planning turn has no execution evidence")
        model = execution.get("model") or "unknown"
        if not isinstance(model, str) or len(model) > 120:
            model = "unknown"
        model_fingerprint = hashlib.sha256(model.encode("utf-8")).hexdigest()[:16]
        diagnostic = turn.get("response_validation")
        failure_class = (
            diagnostic.get("failure_class", "none")
            if isinstance(diagnostic, dict)
            else "none"
        )
        if failure_class not in {
            "none",
            "transport",
            "semantic_schema",
            "semantic_context",
            "evidence_grounding",
            "missing_user_decision",
        }:
            raise ValueError("Planning failure class is invalid")
        outcome = turn.get("semantic_correction_outcome") or "none"
        if outcome not in {
            "none",
            "accepted",
            "improved",
            "no_improvement",
            "invalid_submission",
            "not_evaluated",
        }:
            raise ValueError("Planning correction outcome is invalid")
        turn_result = (
            "accepted"
            if turn.get("parsed_response") is not None
            and turn.get("validation_error") is None
            else "rejected"
            if turn.get("validation_error") is not None
            else "no_response"
        )
        normalizations = turn.get("response_normalizations") or []
        if not isinstance(normalizations, list):
            raise ValueError("Planning normalizations must be a list")
        for value in normalizations:
            if not isinstance(value, str):
                raise ValueError("Planning normalization entry is invalid")
            rule_id = normalization_rule_id(value)
            unclassified += rule_id == "unclassified_normalization"
            counts[
                (
                    source_version,
                    "planning",
                    model_fingerprint,
                    failure_class,
                    "normalization",
                    rule_id,
                    turn_result,
                    outcome,
                )
            ] += 1
        if isinstance(diagnostic, dict):
            issues = diagnostic.get("issues") or []
            if not isinstance(issues, list):
                raise ValueError("Planning diagnostic issues must be a list")
            for issue in issues:
                if not isinstance(issue, dict):
                    raise ValueError("Planning diagnostic issue is invalid")
                rule_id = issue.get("invariant_id") or issue.get("code")
                if not isinstance(rule_id, str):
                    raise ValueError("Planning invariant ID is invalid")
                if re.fullmatch(r"planning_[a-z0-9_]{1,90}", rule_id) is None:
                    rule_id = "unclassified_validation"
                    unclassified_validation += 1
                counts[
                    (
                        source_version,
                        "planning",
                        model_fingerprint,
                        failure_class,
                        "validation",
                        rule_id,
                        turn_result,
                        outcome,
                    )
                ] += 1
    return {
        "schema_version": 1,
        "source_version": source_version,
        "turn_count": len(paths),
        "unclassified_normalization_count": unclassified,
        "unclassified_validation_count": unclassified_validation,
        "rules": [
            {
                "version": version,
                "stage": stage,
                "model_sha256_prefix": model,
                "failure_class": failure_class,
                "event_kind": event_kind,
                "rule_id": rule_id,
                "turn_result": turn_result,
                "correction_outcome": outcome,
                "trigger_count": count,
            }
            for (
                version,
                stage,
                model,
                failure_class,
                event_kind,
                rule_id,
                turn_result,
                outcome,
            ), count in sorted(counts.items())
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-version", required=True)
    parser.add_argument("paths", nargs="+", type=Path)
    arguments = parser.parse_args()
    if not arguments.source_version.isascii() or not all(
        character.isalnum() or character in ".-_"
        for character in arguments.source_version
    ):
        parser.error("source version must be a bounded public identifier")
    report = summarize_turns(arguments.paths, source_version=arguments.source_version)
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

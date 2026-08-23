"""Frozen, task-specific calibration for the governed LLM research planner."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .llm_research_planner import (
    LLMResearchPlannerValidationError,
    build_planner_prompt,
    parse_planner_candidate_text,
)
from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"
CORPUS_PATH = (
    Path(__file__).with_name("calibration_fixtures")
    / "llm-research-planner-v0.1.json"
)
_CASE_FIELDS = {
    "id", "label", "safety_critical", "question", "answer_criteria",
    "selected_lens", "industry_driver_notes", "thesis_notes", "prior_outcomes",
    "human_directives", "remaining_budget", "catalog", "expected_actions",
}
_CATALOG_FIELDS = {"coverage_item_ref", "operation", "description", "cost_units"}
_OUTCOME_FIELDS = {"coverage_item_ref", "outcome_kind", "summary"}
_DIRECTIVE_FIELDS = {
    "control_effect", "target_coverage_item_ref", "verbatim_text",
}


class PlannerCalibrationError(ValueError):
    pass


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise PlannerCalibrationError(f"{name} has an unexpected shape")
    return dict(value)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlannerCalibrationError(f"{name} must be non-empty text")
    return value.strip()


def validate_planner_calibration_corpus(value: Any) -> dict[str, Any]:
    corpus = _closed(
        value,
        {"schema_version", "id", "description", "cases", "content_hash"},
        "planner calibration corpus",
    )
    if corpus["schema_version"] != SCHEMA_VERSION:
        raise PlannerCalibrationError("corpus schema_version is unsupported")
    _text(corpus["id"], "corpus id")
    _text(corpus["description"], "corpus description")
    if not isinstance(corpus["cases"], list) or not corpus["cases"]:
        raise PlannerCalibrationError("corpus cases must be non-empty")
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(corpus["cases"]):
        case = _closed(raw, _CASE_FIELDS, f"case {index}")
        case_id = _text(case["id"], "case id")
        if case_id in seen:
            raise PlannerCalibrationError("case ids must be unique")
        seen.add(case_id)
        _text(case["label"], "case label")
        _text(case["question"], "case question")
        _text(case["answer_criteria"], "case answer_criteria")
        if not isinstance(case["safety_critical"], bool):
            raise PlannerCalibrationError("case safety_critical must be boolean")
        lens = _closed(
            case["selected_lens"],
            {"lens_ref", "label", "objective", "priority_topics"},
            "selected_lens",
        )
        for field in ("lens_ref", "label", "objective"):
            _text(lens[field], f"selected_lens.{field}")
        if not isinstance(lens["priority_topics"], list) or not all(
            isinstance(item, str) and item for item in lens["priority_topics"]
        ):
            raise PlannerCalibrationError("lens priority_topics are invalid")
        for field in ("industry_driver_notes", "thesis_notes"):
            if not isinstance(case[field], list) or not all(
                isinstance(item, str) and item for item in case[field]
            ):
                raise PlannerCalibrationError(f"case {field} is invalid")
        outcomes = []
        for raw_outcome in case["prior_outcomes"]:
            outcome = _closed(raw_outcome, _OUTCOME_FIELDS, "prior outcome")
            if outcome["outcome_kind"] not in {
                "observed", "not_found_in_scope", "source_unavailable"
            }:
                raise PlannerCalibrationError("prior outcome kind is invalid")
            for field in ("coverage_item_ref", "summary"):
                _text(outcome[field], f"prior outcome {field}")
            outcomes.append(outcome)
        directives = []
        for raw_directive in case["human_directives"]:
            directive = _closed(raw_directive, _DIRECTIVE_FIELDS, "human directive")
            if directive["control_effect"] not in {
                "focus_coverage_item", "request_replan", "deprioritize"
            }:
                raise PlannerCalibrationError("human directive effect is invalid")
            _text(directive["verbatim_text"], "directive text")
            target = directive["target_coverage_item_ref"]
            if target is not None:
                _text(target, "directive target")
            directives.append(directive)
        budget = _closed(
            case["remaining_budget"],
            {"rounds_remaining", "cost_units_remaining", "seconds_remaining"},
            "remaining budget",
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in budget.values()
        ):
            raise PlannerCalibrationError("remaining budget is invalid")
        catalog = []
        for raw_item in case["catalog"]:
            item = _closed(raw_item, _CATALOG_FIELDS, "catalog item")
            for field in ("coverage_item_ref", "operation", "description"):
                _text(item[field], f"catalog item {field}")
            if (
                isinstance(item["cost_units"], bool)
                or not isinstance(item["cost_units"], int)
                or item["cost_units"] < 1
            ):
                raise PlannerCalibrationError("catalog item cost is invalid")
            catalog.append(item)
        refs = [item["coverage_item_ref"] for item in catalog]
        if not refs or len(refs) != len(set(refs)):
            raise PlannerCalibrationError("catalog refs must be non-empty and unique")
        if not isinstance(case["expected_actions"], list) or not case["expected_actions"]:
            raise PlannerCalibrationError("expected_actions must be non-empty")
        expected = []
        for action in case["expected_actions"]:
            try:
                candidate = parse_planner_candidate_text(canonical_json({
                    "schema_version": "0.1",
                    "action": action,
                    "rationale": "gold",
                }))
            except LLMResearchPlannerValidationError as exc:
                raise PlannerCalibrationError("expected action is invalid") from exc
            expected.append(candidate["action"])
        case["selected_lens"] = lens
        case["prior_outcomes"] = outcomes
        case["human_directives"] = directives
        case["remaining_budget"] = budget
        case["catalog"] = catalog
        case["expected_actions"] = expected
        cases.append(case)
    body = {
        "schema_version": corpus["schema_version"],
        "id": corpus["id"],
        "description": corpus["description"],
        "cases": cases,
    }
    if corpus["content_hash"] != content_hash(body):
        raise PlannerCalibrationError("corpus content_hash mismatch")
    return {**body, "content_hash": corpus["content_hash"]}


def load_planner_calibration_corpus(path: Path = CORPUS_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlannerCalibrationError(f"cannot load planner corpus: {exc}") from exc
    return validate_planner_calibration_corpus(value)


def calibration_case_context(case: Mapping[str, Any]) -> dict[str, Any]:
    """Materialize a synthetic ContextPack with production prompt semantics."""

    digest = content_hash(case)
    selected_lens = {
        **case["selected_lens"],
        "evidence_standard": {
            "preferred_source_classes": ["company_primary", "regulatory_filing"],
            "minimum_independent_sources": 1,
            "negative_claim_rule": "candidate_only_until_separate_claim_admission",
        },
    }
    outcomes = []
    for index, outcome in enumerate(case["prior_outcomes"], 1):
        quoted = {
            "id": f"research-outcome:calibration:{case['id']}:{index}",
            **outcome,
            "formal_negative_claim_created": False,
        }
        outcomes.append({
            "ref": quoted["id"],
            "hash": content_hash(quoted),
            "quoted_data": quoted,
        })
    directives = []
    for index, directive in enumerate(case["human_directives"], 1):
        quoted = {
            "id": f"research-directive-version:calibration:{case['id']}:{index}",
            **directive,
            "actor_ref": "human:calibration-gold",
        }
        directives.append({
            "ref": quoted["id"],
            "hash": content_hash(quoted),
            "quoted_data": quoted,
        })
    catalog = []
    for index, item in enumerate(case["catalog"], 1):
        template = {
            "id": f"probe-template-version:calibration:{case['id']}:{index}",
            "operation": item["operation"],
            "description": item["description"],
            "permission_scope": "read_only_calibration",
            "cost": {
                "cost_units": item["cost_units"],
                "max_attempts": 1,
                "max_seconds": 30,
            },
        }
        catalog.append({
            "coverage_item_ref": item["coverage_item_ref"],
            "template_version_ref": template["id"],
            "template_version_hash": content_hash(template),
            "parameters": {"scope": item["coverage_item_ref"]},
            "quoted_data": template,
        })
    question = {
        "id": f"research-question-version:calibration:{case['id']}",
        "question": case["question"],
        "answer_criteria": case["answer_criteria"],
    }
    doctrine = {
        "id": f"doctrine-pack-version:calibration:{case['id']}",
        "default_lens_ref": selected_lens["lens_ref"],
        "lenses": [selected_lens],
    }
    driver = {
        "id": f"industry-driver-pack-version:calibration:{case['id']}",
        "notes": case["industry_driver_notes"],
    }
    theses = [
        {
            "ref": f"thesis-version:calibration:{case['id']}:{index}",
            "hash": content_hash({"note": note}),
            "authority_kind": "human_admission",
            "quoted_data": {"statement": note, "authority_kind": "human_admission"},
        }
        for index, note in enumerate(case["thesis_notes"], 1)
    ]
    return {
        "schema_version": "0.1",
        "id": "planner-context-pack-version:" + digest[:32],
        "created_at": "2026-08-23T18:00:00.000000+00:00",
        "loop_version_ref": f"bounded-planner-loop-version:calibration:{case['id']}",
        "loop_version_hash": content_hash({"case": case["id"]}),
        "round_ordinal": len(outcomes) + 1,
        "question_input": {
            "ref": question["id"], "hash": content_hash(question), "quoted_data": question,
        },
        "doctrine_input": {
            "ref": doctrine["id"], "hash": content_hash(doctrine), "quoted_data": doctrine,
        },
        "selected_lens_ref": selected_lens["lens_ref"],
        "selected_lens": selected_lens,
        "override_input": None,
        "driver_pack_input": {
            "ref": driver["id"], "hash": content_hash(driver), "quoted_data": driver,
        },
        "thesis_inputs": theses,
        "outcome_inputs": outcomes,
        "directive_inputs": directives,
        "remaining_budget": dict(case["remaining_budget"]),
        "catalog_inputs": catalog,
        "content_hash": content_hash({"case": case["id"], "digest": digest}),
    }


def build_calibration_prompt(case: Mapping[str, Any]) -> str:
    return build_planner_prompt(calibration_case_context(case))


def score_planner_outputs(
    corpus: Mapping[str, Any],
    outputs: Mapping[str, Any],
    *,
    case_refs: Sequence[str] | None = None,
) -> dict[str, Any]:
    frozen = validate_planner_calibration_corpus(corpus)
    available = [case["id"] for case in frozen["cases"]]
    selected = available if case_refs is None else list(case_refs)
    if not selected or len(selected) != len(set(selected)):
        raise PlannerCalibrationError("case_refs must be a non-empty unique subset")
    unknown = sorted(set(selected) - set(available))
    if unknown:
        raise PlannerCalibrationError(f"unknown case_refs: {unknown}")
    selected_set = set(selected)
    rows = []
    for case in frozen["cases"]:
        if case["id"] not in selected_set:
            continue
        raw = outputs.get(case["id"])
        parse_error = None
        candidate = None
        try:
            if isinstance(raw, Mapping) and "parsed_output" in raw:
                if raw.get("parse_error") is not None:
                    raise LLMResearchPlannerValidationError(str(raw["parse_error"]))
                candidate = parse_planner_candidate_text(
                    canonical_json(raw["parsed_output"])
                )
            elif isinstance(raw, str):
                candidate = parse_planner_candidate_text(raw)
            else:
                raise LLMResearchPlannerValidationError("missing output")
        except LLMResearchPlannerValidationError as exc:
            parse_error = str(exc)
        expected = [canonical_json(item) for item in case["expected_actions"]]
        action_match = (
            candidate is not None
            and canonical_json(candidate["action"]) in expected
        )
        rows.append({
            "case_ref": case["id"],
            "label": case["label"],
            "safety_critical": case["safety_critical"],
            "schema_valid": candidate is not None,
            "action_match": action_match,
            "parse_error": parse_error,
            "actual_action": candidate["action"] if candidate else None,
            "expected_actions": case["expected_actions"],
        })
    total = len(rows)
    schema_valid = sum(item["schema_valid"] for item in rows)
    action_matches = sum(item["action_match"] for item in rows)
    safety = [item for item in rows if item["safety_critical"]]
    safety_passes = sum(item["schema_valid"] and item["action_match"] for item in safety)
    return {
        "schema_version": SCHEMA_VERSION,
        "corpus_ref": frozen["id"],
        "corpus_hash": frozen["content_hash"],
        "total_cases": total,
        "schema_valid_cases": schema_valid,
        "action_match_cases": action_matches,
        "safety_critical_cases": len(safety),
        "safety_critical_passes": safety_passes,
        "hard_gate_pass": schema_valid == total and safety_passes == len(safety),
        "rows": rows,
    }


__all__ = [
    "PlannerCalibrationError",
    "build_calibration_prompt",
    "calibration_case_context",
    "load_planner_calibration_corpus",
    "score_planner_outputs",
    "validate_planner_calibration_corpus",
]

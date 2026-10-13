"""Small, source-bound selections for the planner's full financial-note targets.

Only the prompt uses option references. Before normal strategy validation the
selection expands from the exact full research state; stored/executed plans
retain the original target and all its lineage, never a model-built binding.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping

from .document_research_strategy import DocumentResearchStrategyError, normalize_evidence_target
from .store import content_hash

CONTRACT = "planner-financial-note-option:0.1"


def option_ref(company_ref, document, target):
    return CONTRACT + ":" + content_hash({
        "company_ref": company_ref,
        "document_ref": document.get("document_ref"),
        "document_version_hash": document.get("document_version_hash"),
        "target": normalize_evidence_target(target),
    })


def prompt_state(state):
    """Lossless choice directory; the caller keeps the complete state intact."""
    result = copy.deepcopy(state)
    count = 0
    for company in result.get("companies", ()):
        for document in company.get("readable_documents", ()):
            targets = document.get("evidence_targets")
            if not isinstance(targets, list) or not targets:
                continue
            try:
                normalized = [normalize_evidence_target(t) for t in targets]
            except DocumentResearchStrategyError:
                continue
            options = []
            for target in normalized:
                options.append({"option_ref": option_ref(company.get("company_ref"), document, target),
                    **{key: target[key] for key in (
                        "kind", "form", "applicability_kind", "periods")}})
            document["evidence_target_options"] = options
            del document["evidence_targets"]
            count += len(options)
    return result, count


def expand_selection(value, *, company_ref, state):
    if not isinstance(value, Mapping) or "evidence_target_ref" not in value:
        return value
    if "evidence_target" in value:
        raise DocumentResearchStrategyError("choose a note option or a full target, not both")
    selected = value["evidence_target_ref"]
    if not isinstance(selected, str):
        raise DocumentResearchStrategyError("financial note option reference must be text")
    matches = []
    for company in state.get("companies", ()):
        if company.get("company_ref") != company_ref:
            continue
        for document in company.get("readable_documents", ()):
            if (document.get("document_ref") != value.get("document_ref")
                    or document.get("document_version_hash") != value.get("document_version_hash")):
                continue
            for target in document.get("evidence_targets", ()):
                if option_ref(company_ref, document, target) == selected:
                    matches.append(normalize_evidence_target(target))
    if len(matches) != 1:
        raise DocumentResearchStrategyError("financial note option is absent or ambiguous for this company/document/version")
    result = dict(value)
    del result["evidence_target_ref"]
    result["evidence_target"] = copy.deepcopy(matches[0])
    return result

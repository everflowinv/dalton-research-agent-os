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
FORMAL_SELECTION_CONTRACT = 'planner-financial-note-formal-selection:0.1'


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
        # The complete state keeps the financial-reader projection and its
        # company-level operations feedback. Show a shared proof once, with
        # an explicit link; never replace either in the execution state.
        priority = company.get('model_history_note_priority')
        financial = company.get('financial_model')
        if (isinstance(priority, Mapping) and isinstance(financial, dict)
                and financial.get('model_history_note_priority') == priority):
            del financial['model_history_note_priority']
            financial['model_history_note_priority_ref'] = priority['content_hash']
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


def expand_formal_selection(value, *, company_ref, stored_strategy,
                            prompt_state, state_hash):
    """Rebuild a compact choice from the exact successful planner authorities.

    The caller proves the immutable Work/prompt and saved full plan. The prompt
    directory proves the choice was actually offered; the saved target must
    independently recreate its source-bound option hash. This inventory is not
    a replacement research state and grants no source or execution permission.
    """
    if not isinstance(value, Mapping) or 'evidence_target_ref' not in value:
        return value
    if (not isinstance(prompt_state, Mapping) or prompt_state.get('content_hash')!=state_hash
            or (prompt_state.get('prompt_projection') or {}).get('full_state_hash',state_hash)!=state_hash
            or not isinstance(stored_strategy, Mapping)):
        raise DocumentResearchStrategyError('formal note choice lacks its exact original state')
    target=normalize_evidence_target(stored_strategy.get('evidence_target'))
    document={k:stored_strategy.get(k) for k in ('document_ref','document_version_hash')}
    expected={'option_ref':option_ref(company_ref,document,target),
              **{k:target[k] for k in ('kind','form','applicability_kind','periods')}}
    if (value.get('evidence_target_ref')!=expected['option_ref']
            or any(value.get(k)!=document[k] for k in document)):
        raise DocumentResearchStrategyError('formal note choice differs from the saved source target')
    offered=[]
    for company in prompt_state.get('companies',()):
        if company.get('company_ref')!=company_ref:continue
        for doc in company.get('readable_documents',()):
            if any(doc.get(k)!=document[k] for k in document):continue
            offered.extend(option for option in doc.get('evidence_target_options',()) if option==expected)
    if len(offered)!=1:
        raise DocumentResearchStrategyError('formal note choice is absent or ambiguous in its original prompt')
    inventory={'companies':[{'company_ref':company_ref,'readable_documents':[
        {**document,'evidence_targets':[target]}]}]}
    return expand_selection(value,company_ref=company_ref,state=inventory)

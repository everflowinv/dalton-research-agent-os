"""Question-bound thesis semantics. Legacy versions are read without invention."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

QUESTION_FIELDS = frozenset({"question_ref", "stance", "proposition_direction", "view_status",
                             "falsification_conditions", "passage_refs", "framework_version_ref", "disagreement"})
BASE_FIELDS = frozenset({"statement", "mechanism", "confidence", "implied_expectation",
                         "claim_refs", "catalyst_refs", "falsifier_refs", "change_reason"})


def thesis_payload(wire: Mapping[str, Any]) -> dict[str, Any]:
    fields = BASE_FIELDS | QUESTION_FIELDS if wire.get("schema_version") == "0.3" or "question_ref" in wire else BASE_FIELDS
    return {key: wire[key] for key in fields}


def validate_question_fields(content: Mapping[str, Any]) -> None:
    if not isinstance(content.get("question_ref"), str) or not content["question_ref"].startswith("question:") or not content["question_ref"][9:].strip():
        raise ValueError("a thesis must name its stable question: reference")
    if content.get("stance") not in {"house", "counter"}:
        raise ValueError("thesis stance must be house or counter")
    if content.get("proposition_direction") not in {"supports", "contradicts", "uncertain"}:
        raise ValueError("thesis direction must answer the named proposition")
    if content.get("view_status") not in {"active", "broken"}:
        raise ValueError("thesis view_status must be active or broken")
    passages = content.get("passage_refs")
    if not isinstance(passages, (list, tuple)) or any(not isinstance(ref, str) or not ref.startswith("doc-passage:") for ref in passages):
        raise ValueError("passage_refs must name document passages")
    if not content.get("claim_refs") and not passages:
        raise ValueError("a formal thesis requires claim or document passage evidence")
    conditions = content.get("falsification_conditions")
    if not isinstance(conditions, (list, tuple)) or not conditions:
        raise ValueError("a formal thesis requires quantified falsification conditions and deadlines")
    for item in conditions:
        if not isinstance(item, Mapping) or set(item) != {"metric_ref", "operator", "threshold", "unit", "deadline", "evidence_refs"}:
            raise ValueError("falsification condition has an invalid closed shape")
        if not all(isinstance(item[k], str) and item[k].strip() for k in ("metric_ref", "unit", "deadline")):
            raise ValueError("falsification must name a metric, unit and deadline")
        if item["operator"] not in {"lt", "lte", "gt", "gte", "eq"}:
            raise ValueError("falsification comparator is invalid")
        try:
            threshold = Decimal(str(item["threshold"]))
            date.fromisoformat(item["deadline"])
        except (ValueError, InvalidOperation) as exc:
            raise ValueError("falsification threshold or deadline is invalid") from exc
        if isinstance(item["threshold"], bool) or not threshold.is_finite():
            raise ValueError("falsification threshold must be finite")
        if not isinstance(item["evidence_refs"], list) or not item["evidence_refs"] or not all(isinstance(r,str) and r.strip() for r in item["evidence_refs"]):
            raise ValueError("falsification must cite the basis of the condition")
    framework = content.get("framework_version_ref")
    if framework is not None and (not isinstance(framework,str) or not framework.strip()):
        raise ValueError("framework_version_ref must be a version reference or null")
    disagreement = content.get("disagreement")
    if disagreement is not None:
        required = {"other_thesis_version_ref", "other_thesis_hash", "reason", "evidence_refs", "deadline", "arbiter_ref"}
        if not isinstance(disagreement, Mapping) or set(disagreement) != required:
            raise ValueError("disagreement must identify both views, evidence, deadline and arbiter")
        if not all(isinstance(disagreement[k],str) and disagreement[k].strip() for k in required - {"evidence_refs"}):
            raise ValueError("disagreement fields must be nonempty")
        if (not disagreement["arbiter_ref"].startswith("human:") or
                not isinstance(disagreement["evidence_refs"], (list, tuple)) or
                not disagreement["evidence_refs"] or
                not all(isinstance(r, str) and r.strip() for r in disagreement["evidence_refs"])):
            raise ValueError("disagreement requires evidence and a human arbiter")
        digest = disagreement["other_thesis_hash"]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("disagreement must bind the other thesis content hash")
        date.fromisoformat(disagreement["deadline"])


def require_current_question(core: Any, content: Mapping[str, Any], *, thesis_ref: str,
                             industry_ref: str | None = None, industry_thesis: bool = False) -> None:
    validate_question_fields(content)
    if industry_ref is not None and (industry_thesis or content.get("framework_version_ref") is not None):
        if not content.get("framework_version_ref"):
            raise ValueError("industry thesis must bind the latest industry framework version")
        row = core.execute("SELECT version_id FROM industry_framework_versions WHERE industry_ref=? "
                           "ORDER BY version_number DESC LIMIT 1", (industry_ref,)).fetchone()
        if row is None or row[0] != content["framework_version_ref"]:
            raise ValueError("industry thesis must bind the latest industry framework version")
    matched_disagreement = False
    for row in core.execute("SELECT v.content_json FROM current_pointers p JOIN thesis_versions v ON v.version_id=p.version_id"):
        other = json.loads(row[0])
        if other.get("thesis_ref") == thesis_ref or other.get("question_ref") != content["question_ref"]:
            continue
        if other.get("view_status") == "broken" or content.get("view_status") == "broken":
            continue
        disagreement = content.get("disagreement") or {}
        if (disagreement.get("other_thesis_version_ref") == other.get("id") and
                disagreement.get("other_thesis_hash") == other.get("content_hash")):
            matched_disagreement = True
        if other.get("stance") == content["stance"] == "house":
            raise ValueError("one question may have only one current house thesis")
        if {other.get("proposition_direction"), content["proposition_direction"]} == {"supports", "contradicts"}:
            disagreement = content.get("disagreement") or {}
            if (disagreement.get("other_thesis_version_ref") != other.get("id") or
                    disagreement.get("other_thesis_hash") != other.get("content_hash")):
                raise ValueError("opposite current views require a disagreement bound to the other exact version")
    if content.get("disagreement") and not matched_disagreement:
        raise ValueError("disagreement must identify a current active view of the same question")


def validate_revision_proposal(statement: Any, confidence: Any, *, decision: str) -> None:
    if confidence not in {"low", "medium", "high"}:
        raise ValueError("a thesis revision must explicitly propose ordinal low, medium or high confidence")
    if statement is not None and (not isinstance(statement, str) or not statement.strip()):
        raise ValueError("a proposed thesis statement cannot be empty")
    text = str(statement or "").lower()
    explicit = {"low": ("低置信度", "low confidence"), "medium": ("中等置信度", "medium confidence"),
                "high": ("高置信度", "high confidence")}
    if any(level != confidence and any(word in text for word in words) for level, words in explicit.items()):
        raise ValueError("proposed confidence contradicts the explicit confidence stated in the text")
    if decision == "THESIS_BROKEN" and confidence == "high" and any(x in text for x in ("仍然成立", "thesis remains intact")):
        raise ValueError("a broken thesis cannot be described as intact")


def require_evidence(store: Any, content: Mapping[str, Any]) -> None:
    """Resolve every formal source and falsification basis, never just its prefix."""
    from pathlib import Path
    from .evidence_index import passage_resolver
    resolve = passage_resolver(Path(store.path).parent) if getattr(store, "path", ":memory:") != ":memory:" else lambda ref: None
    refs = list(content.get("claim_refs") or ()) + list(content.get("passage_refs") or ())
    for condition in content.get("falsification_conditions") or ():
        refs.extend(condition["evidence_refs"])
    refs.extend((content.get("disagreement") or {}).get("evidence_refs") or ())
    for ref in set(refs):
        if ref.startswith("doc-passage:"):
            valid = bool(resolve(ref))
        else:
            valid = store.connection.execute(
                "SELECT 1 FROM claim_versions c JOIN evidence_relations e "
                "ON e.claim_version_id=c.claim_version_id WHERE c.claim_version_id=? LIMIT 1", (ref,)).fetchone()
        if not valid:
            raise ValueError("thesis evidence does not resolve to a held claim or passage: " + ref)


def record_disagreement(store: Any, cur: Any, wire: Mapping[str, Any]) -> None:
    """Append the two exact versions in the same transaction as the new view."""
    other = wire.get("disagreement")
    if other:
        store._insert_event(cur, "thesis_disagreement", "thesis", wire["thesis_ref"], {
            "question_ref": wire["question_ref"], "thesis_version_ref": wire["id"],
            "thesis_hash": wire["content_hash"], **dict(other),
        }, content_hash=wire["content_hash"], actor_id=wire["committed_by_ref"],
           idempotency_key="thesis-disagreement:" + wire["id"])


def direction_binding(thesis: Mapping[str, Any], statement: str, confidence: str) -> dict[str, Any]:
    from .store import content_hash
    return {"thesis_version_ref": thesis.get("ref") or thesis.get("id"),
            "thesis_hash": thesis["content_hash"],
            "proposal_hash": content_hash({"statement": statement, "confidence": confidence})}


def validate_directions(value: Any, proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A cheap independent verifier compares prose, not producer-supplied direction labels."""
    if not isinstance(value, list) or len(value) != len(proposals):
        raise ValueError("direction checks must cover every proposed statement")
    results = []
    for binding, result in zip(proposals, value):
        if not isinstance(result, Mapping) or set(result) != {"thesis_version_ref", "relation", "reason"}:
            raise ValueError("direction check requires thesis_version_ref, relation and reason")
        if result["thesis_version_ref"] != binding["thesis_version_ref"] or result["relation"] not in {"same", "opposite", "unclear"}:
            raise ValueError("direction check does not match the proposed thesis")
        if not isinstance(result["reason"], str) or not result["reason"].strip():
            raise ValueError("direction check must explain its comparison")
        results.append({**binding, "relation": result["relation"], "reason": result["reason"]})
    return results

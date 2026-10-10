"""Exact supplemental SEC companyfacts contexts from held, hash-verified raw.

Companyfacts supplies consolidated facts only. It cannot recover class members
or replace any face-statement value; accession, filing day, form and duration
must identify the same disclosure before an absent row can be appended.
"""
from __future__ import annotations
import json
from pathlib import Path
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence
from .store import content_hash

RULE_REF = "rule:statement-companyfacts-context:1"
CONCEPTS = (
    "WeightedAverageNumberOfSharesOutstandingBasic", "WeightedAverageNumberOfDilutedSharesOutstanding",
    "WeightedAverageNumberOfShareOutstandingBasicAndDiluted", "NetIncomeLossAvailableToCommonStockholdersBasic",
    "NetIncomeLossAvailableToCommonStockholdersDiluted",
)


def supplemental_lines(state_dir: str | Path, filing: Mapping[str, Any], lines: Sequence[Mapping[str, Any]],
                       source: Mapping[str, Any] | None) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if source is None:
        return [dict(row) for row in lines], None
    from .acquired_sec_filings import _spool_bytes
    digest = str(source.get("raw_hash") or "")
    raw, problem = _spool_bytes(Path(state_dir), digest)
    if raw is None:
        raise ValueError("supplementary companyfacts source cannot be verified: " + str(problem))
    payload = json.loads(raw)
    if str(payload.get("cik", "")).zfill(10) != str(filing["cik"]).zfill(10):
        raise ValueError("supplementary companyfacts CIK differs from filing")
    facts = payload.get("facts")
    if not isinstance(facts, Mapping) or facts != source.get("facts"):
        raise ValueError("supplementary companyfacts differs from exact held raw")
    result = [dict(row) for row in lines]
    present = {(row["concept"], row.get("period_start"), row["period_end"]) for row in result
               if not row.get("dimension_axis") and not row.get("dimensions")}
    evidence = []
    gaap = facts.get("us-gaap") or {}
    for concept in CONCEPTS:
        candidates = {}
        for unit, rows in ((gaap.get(concept) or {}).get("units") or {}).items():
            expected_unit = "shares" if concept.startswith("WeightedAverage") else "USD"
            if unit != expected_unit:
                continue
            for fact in rows:
                if (fact.get("accn") != filing["accession"] or fact.get("filed") != filing["filed"]
                        or fact.get("form") != filing["form"] or not fact.get("start") or not fact.get("end")):
                    continue
                try:
                    number = Decimal(str(fact.get("val")))
                except (InvalidOperation, ValueError, TypeError):
                    continue
                if not number.is_finite() or fact["end"] > filing["report_date"]:
                    continue
                key = ("us-gaap:" + concept, fact["start"], fact["end"])
                if key in present:
                    continue
                candidates.setdefault(key, []).append((unit, fact))
        for key, matches in sorted(candidates.items()):
            if len({(unit, str(fact["val"])) for unit, fact in matches}) != 1:
                raise ValueError("conflicting companyfacts for exact accession/duration: " + key[0])
            unit, fact = matches[0]
            row = {"statement": "income", "concept": key[0], "label": concept, "level": 1,
                   "parent_concept": None, "is_breakdown": False, "dimension_axis": None, "dimension_member": None,
                   "dimension_count": 0, "dimensions": {}, "period_start": key[1], "period_end": key[2],
                   "value": format(Decimal(str(fact["val"])), "f"), "unit": unit, "balance": None}
            ordinal = len(result)
            result.append(row)
            evidence.append({"ordinal": ordinal, "concept": key[0], "unit": unit, "fact": fact,
                             "line_hash": content_hash(row)})
    if not evidence:
        return result, None
    proof = {"rule_ref": RULE_REF, "raw_ref": "raw-sink:" + digest, "raw_hash": digest,
             "cik": str(filing["cik"]), "evidence": evidence, "evidence_hash": content_hash(evidence)}
    return result, proof


def validate_supplement(record: Mapping[str, Any], filing: Mapping[str, Any]) -> None:
    proof = record.get("supplementary_companyfacts")
    if proof is None:
        return
    if (proof.get("rule_ref") != RULE_REF or proof.get("raw_ref") != "raw-sink:" + str(proof.get("raw_hash"))
            or len(str(proof.get("raw_hash"))) != 64 or proof.get("cik") != str(filing["cik"])
            or proof.get("evidence_hash") != content_hash(proof.get("evidence"))
            or record.get("kind") != "complete_snapshot"):
        raise ValueError("supplementary companyfacts proof binding drifted")
    for evidence in proof["evidence"]:
        line = record["lines"][evidence["ordinal"]]
        fact = evidence["fact"]
        if (evidence["line_hash"] != content_hash(line) or fact.get("accn") != filing["accession"]
                or fact.get("filed") != filing["filed"] or fact.get("form") != filing["form"]
                or line["concept"] != evidence["concept"] or line["unit"] != evidence["unit"]
                or line["period_start"] != fact.get("start") or line["period_end"] != fact.get("end")
                or Decimal(line["value"]) != Decimal(str(fact.get("val"))) or line.get("dimensions") != {}):
            raise ValueError("supplementary companyfacts exact fact/line proof differs")


def rebind_supplement_after_units(
    proof: Mapping[str, Any], before: Sequence[Mapping[str, Any]],
    after: Sequence[Mapping[str, Any]], unit_proof: Mapping[str, Any],
    filing: Mapping[str, Any],
) -> dict[str, Any]:
    """Bind new companyfacts evidence to only exact proven XML unit additions.

    Generation only: existing stored context readers never repair held hashes.
    The caller must replay the XML proof against its original before calling.
    """
    validate_supplement({"kind": "complete_snapshot", "lines": before,
                         "supplementary_companyfacts": proof}, filing)
    from .sec_filing_units import RULE_REF as UNIT_RULE_REF

    evidence = unit_proof.get("evidence")
    if (unit_proof.get("rule_ref") != UNIT_RULE_REF or not isinstance(evidence, list)
            or unit_proof.get("evidence_hash") != content_hash(evidence)
            or len(before) != len(after)):
        raise ValueError("companyfacts/XML composition proof differs")
    bindings = {}
    for item in evidence:
        ordinal = item.get("ordinal")
        if (type(ordinal) is not int or not 0 <= ordinal < len(before)
                or ordinal in bindings
                or item.get("content_hash") != content_hash({k: v for k, v in item.items() if k != "content_hash"})):
            raise ValueError("companyfacts/XML composition ordinal or binding hash differs")
        old = before[ordinal]
        for key in ("concept", "period_start", "period_end", "value", "dimensions"):
            if old.get(key) != item.get(key):
                raise ValueError("companyfacts/XML composition source facts differ")
        if old.get("unit") != item.get("source_unit_ref"):
            raise ValueError("companyfacts/XML composition source unit differs")
        bindings[ordinal] = item
    for ordinal, old in enumerate(before):
        expected = dict(old)
        if ordinal in bindings:
            binding = bindings[ordinal]
            expected.update(unit=binding["unit"], source_unit_ref=binding["source_unit_ref"],
                            unit_definition_hash=binding["content_hash"])
        if expected != after[ordinal]:
            raise ValueError("companyfacts/XML composition changed an unproved line field")
    updated = dict(proof)
    updated["evidence"] = [{**item, "line_hash": content_hash(after[item["ordinal"]])}
                           for item in proof["evidence"]]
    updated["evidence_hash"] = content_hash(updated["evidence"])
    validate_supplement({"kind": "complete_snapshot", "lines": after,
                         "supplementary_companyfacts": updated}, filing)
    return updated

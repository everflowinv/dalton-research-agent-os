"""P13al: what one company looks like, as material for deciding its model.

The statements ledger holds every line of every filing, values and all. That is
not what the modelling judgement needs. What it needs is the *vocabulary*: which
concepts this company reports, what it calls them, how they nest, which are
segment breakdowns. Values move every quarter; the structure is what a model is
built on, and feeding a few hundred numbers into the decision would crowd out
the thing being decided.

So this projects the ledger down to structure, deduplicated across filings and
across periods, and hashes it. The hash is what binds a specification to the
company it was written for: a spec decided against three filings is history
once a fourth arrives with a line nobody had seen before.
"""

from __future__ import annotations

from typing import Any, Mapping

from .store import content_hash

# A 10-Q parses to a few hundred lines but only a couple of hundred distinct
# concepts, and most of those repeat across filings. This is a ceiling that
# keeps one prompt bounded, not an expectation.
MAX_CONCEPTS_PER_STATEMENT = 200
MAX_FILINGS = 8


class CompanyModelStateError(RuntimeError):
    """The company has nothing to model against."""


def _line(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "concept": str(row["concept"]),
        "label": str(row["label"]),
        "level": int(row["level"]),
        "parent_concept": row["parent_concept"],
        "is_breakdown": bool(row["is_breakdown"]),
        "dimension_axis": row["dimension_axis"],
    }


def build_company_model_state(
    missions: Any, company_ref: str, *, ticker: str | None = None,
    industry_classification: str | None = None,
) -> dict[str, Any]:
    """Project one company's filed statements down to the structure of them.

    Raises when the company has no statements: a model specification decided
    with no filings behind it would be the model deciding what the company
    reports, which is exactly backwards.

    ``industry_classification`` is W4's addition: the dossier's answer to the
    Deep Insight Gate's first question, carried here so the specification lane
    can pick the driver template that goes with it. It is *in the hashed body*
    on purpose -- a company reclassified from a compounder to a commodity
    producer needs a new specification, and a hash that ignored the
    reclassification would replay the old one. The key is omitted rather than
    written as ``null`` when nothing is known, so every state built before this
    existed still hashes to what it hashed to then.
    """

    filings = missions.statement_filings(company_ref)
    if not filings:
        raise CompanyModelStateError(
            "this company has no filed statements to model against")
    filings = sorted(filings, key=lambda item: (item["report_date"],
                                                item["accession"]))[-MAX_FILINGS:]

    statements: dict[str, list[dict[str, Any]]] = {}
    seen: dict[str, set[str]] = {}
    # Newest first, so when a line's structure changed between filings the
    # current disclosure is the one that survives deduplication.
    for filing in reversed(filings):
        for row in missions.statement_lines(filing["ingest_id"]):
            statement = str(row["statement"])
            key = f"{row['concept']}|{row['dimension_member'] or ''}"
            known = seen.setdefault(statement, set())
            if key in known:
                continue
            bucket = statements.setdefault(statement, [])
            if len(bucket) >= MAX_CONCEPTS_PER_STATEMENT:
                continue
            known.add(key)
            bucket.append(_line(row))

    concepts = sorted({
        line["concept"] for lines in statements.values() for line in lines
    })
    if not concepts:
        raise CompanyModelStateError("this company's filings carry no statement lines")

    body = {
        "company_ref": company_ref,
        "ticker": ticker,
        "entity_name": filings[-1]["entity_name"],
        "cik": filings[-1]["cik"],
        "filings": [
            {"accession": item["accession"], "form": item["form"],
             "report_date": item["report_date"], "line_count": item["line_count"]}
            for item in filings
        ],
        "statements": {name: statements[name] for name in sorted(statements)},
        "concepts": concepts,
    }
    if industry_classification:
        body["industry_classification"] = str(industry_classification)
    return {**body, "state_hash": content_hash(body)}


__all__ = [
    "MAX_CONCEPTS_PER_STATEMENT",
    "MAX_FILINGS",
    "CompanyModelStateError",
    "build_company_model_state",
]

"""What one company looks like, as source-bound material for deciding its model.

The structural projection remains the model's vocabulary.  A separately
bounded numeric-period projection now supplies the dated filed values needed
to judge this company's arithmetic.  It carries exact units, dimensions and
source identities; it never infers a fiscal year, a missing value, or a note.

The complete state hashes both projections.  A specification is therefore
history when the bounded filed values, their authorities, their configured
bounds, or the company's statement structure changes.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping
import json

from .company_model_series import period_kind
from .store import content_hash

# A 10-Q parses to a few hundred lines but only a couple of hundred distinct
# concepts, and most of those repeat across filings. This is a ceiling that
# keeps one prompt bounded, not an expectation.
MAX_CONCEPTS_PER_STATEMENT = 200
MAX_FILINGS = 8
DEFAULT_NUMERIC_CONTEXT_POLICY = {
    "max_periods_per_series": 8,
    "max_total_cells": 300,
}
DEFAULT_MODEL_SPEC_CALL_BUDGET = {
    "max_input_tokens": 120_000,
    "max_output_tokens": 6_000,
    "max_cost_usd": 2.50,
    "timeout_seconds": 300,
}
DEFAULT_MODEL_SPEC_PROMPT_BYTES = int(
    DEFAULT_MODEL_SPEC_CALL_BUDGET["max_input_tokens"]
)
NUMERIC_CONTEXT_SCHEMA_VERSION = "company-model-numeric-context-0.2"


class CompanyModelStateError(RuntimeError):
    """The company has nothing to model against."""


class CompanyModelPromptBudgetError(CompanyModelStateError):
    """The fixed prompt cannot fit even after all numeric rows are omitted."""

    def __init__(self, *, base_prompt_bytes: int, prompt_byte_limit: int) -> None:
        self.report = {
            "base_prompt_bytes": base_prompt_bytes,
            "prompt_byte_limit": prompt_byte_limit,
            "over_by_bytes": base_prompt_bytes - prompt_byte_limit,
        }
        super().__init__(
            "model specification base prompt exceeds its configured input bound: "
            f"base_prompt_bytes={base_prompt_bytes}, "
            f"prompt_byte_limit={prompt_byte_limit}"
        )


def validate_numeric_context_policy(value: Any = None) -> dict[str, int]:
    """Return the closed owner-configurable bounds for filed numeric context."""

    policy = dict(DEFAULT_NUMERIC_CONTEXT_POLICY) if value is None else (
        dict(value) if isinstance(value, Mapping) else None
    )
    fields = set(DEFAULT_NUMERIC_CONTEXT_POLICY)
    if policy is None or set(policy) != fields:
        raise ValueError("model_spec_numeric_context has an invalid closed shape")
    for field in sorted(fields):
        item = policy[field]
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise ValueError(f"model_spec_numeric_context.{field} must be a positive integer")
    return policy


def model_spec_numeric_context_config(
    model_config: Mapping[str, Any] | None,
) -> dict[str, int]:
    return validate_numeric_context_policy(
        None if model_config is None else model_config.get("model_spec_numeric_context")
    )


def model_spec_prompt_byte_limit(
    model_config: Mapping[str, Any] | None,
) -> int:
    """Resolve the byte limit enforced by the actual ``model_spec`` Work."""

    from .call_budget import resolve_call_budget

    return int(resolve_call_budget(
        {} if model_config is None else model_config,
        "model_spec", defaults=DEFAULT_MODEL_SPEC_CALL_BUDGET,
    )["max_input_tokens"])


def _line(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "concept": str(row["concept"]),
        "label": str(row["label"]),
        "level": int(row["level"]),
        "parent_concept": row["parent_concept"],
        "is_breakdown": bool(row["is_breakdown"]),
        "dimension_axis": row["dimension_axis"],
        "unit": str(row["unit"]),
        "period_kind": "duration" if row["period_start"] else "instant",
    }


def _duration_days(start: Any, end: Any) -> int | None:
    if not isinstance(start, str) or not start:
        return None
    if not isinstance(end, str) or not end:
        return None
    try:
        days = (date.fromisoformat(end[:10]) - date.fromisoformat(start[:10])).days + 1
    except ValueError:
        return None
    return days if days > 0 else None


def _numeric_line_authority(row: Mapping[str, Any]) -> dict[str, Any]:
    """Hash the exact immutable statement-line fields used in the prompt."""

    body = {
        key: row.get(key)
        for key in (
            "line_id", "ingest_id", "statement", "ordinal", "concept", "label",
            "level", "parent_concept", "is_breakdown", "dimension_axis",
            "dimension_member", "dimension_count", "period_start", "period_end",
            "value", "unit", "balance",
        )
    }
    return {**body, "content_hash": content_hash(body)}


def _numeric_context(
    filings: list[Mapping[str, Any]], lines_by_ingest: Mapping[str, list[Mapping[str, Any]]],
    *, structure_series: set[tuple[str, str]], policy: Mapping[str, int],
) -> tuple[dict[str, Any], list[list[dict[str, Any]]]]:
    for filing in filings:
        digest = filing.get("content_hash")
        if (
            not isinstance(digest, str) or len(digest) != 64
            or any(ch not in "0123456789abcdef" for ch in digest)
        ):
            raise CompanyModelStateError(
                "filed statement authority has an invalid content hash"
            )
    filing_authorities = [{
        "ingest_id": str(filing["ingest_id"]),
        "accession": str(filing["accession"]),
        "form": str(filing["form"]),
        "filed": str(filing["filed"]),
        "report_date": str(filing["report_date"]),
        "content_hash": str(filing["content_hash"]),
        "source_record_refs": list(filing.get("source_record_refs") or []),
    } for filing in filings]
    by_ingest = {item["ingest_id"]: item for item in filing_authorities}

    # One series is one statement/concept/dimension/unit/balance definition.
    # Restatements from the newest filing win. Multiple conflicting rows in
    # that same filing and period remain separate, explicitly ambiguous cells.
    candidates: dict[tuple[str, ...], dict[tuple[str, str], list[dict[str, Any]]]] = {}
    newest_filings = sorted(
        filings, key=lambda filing: (str(filing["filed"]), str(filing["accession"])),
        reverse=True,
    )
    newest_rank = {
        str(filing["ingest_id"]): rank
        for rank, filing in enumerate(newest_filings)
    }
    for filing in newest_filings:
        ingest_id = str(filing["ingest_id"])
        authority = by_ingest[ingest_id]
        for raw in lines_by_ingest.get(ingest_id, []):
            if (
                (str(raw.get("statement") or ""), str(raw.get("concept") or ""))
                not in structure_series
                or raw.get("value") is None
            ):
                continue
            if (
                raw.get("ingest_id") != ingest_id
                or not str(raw.get("line_id") or "").startswith(ingest_id + "#")
            ):
                raise CompanyModelStateError(
                    "filed statement line differs from its filing authority"
                )
            line = _numeric_line_authority(raw)
            series_key = tuple(str(raw.get(field) or "") for field in (
                "statement", "concept", "dimension_axis", "dimension_member",
                "unit", "balance",
            ))
            period_key = (
                str(raw.get("period_start") or ""), str(raw.get("period_end") or ""),
            )
            period_rows = candidates.setdefault(series_key, {}).setdefault(period_key, [])
            if period_rows and period_rows[0]["_filing_rank"] < newest_rank[ingest_id]:
                continue
            if period_rows and period_rows[0]["_filing_rank"] > newest_rank[ingest_id]:
                period_rows.clear()
            cell = {
                "statement": str(raw["statement"]),
                "concept": str(raw["concept"]),
                "dimension_axis": raw.get("dimension_axis"),
                "dimension_member": raw.get("dimension_member"),
                "period_start": raw.get("period_start"),
                "period_end": str(raw["period_end"]),
                "period_shape": period_kind(raw.get("period_start"), raw.get("period_end")),
                "duration_days": _duration_days(raw.get("period_start"), raw.get("period_end")),
                "value": str(raw["value"]),
                "unit": str(raw["unit"]),
                "balance": raw.get("balance"),
                "accession": authority["accession"],
                "filing_form": authority["form"],
                "line_id": str(raw["line_id"]),
                "line_content_hash": line["content_hash"],
                "filing_content_hash": authority["content_hash"],
                "_filing_rank": newest_rank[ingest_id],
            }
            # Exact duplicate rows do not add evidence; different values or
            # line authorities in the same filed period remain visible.
            comparable = {key: value for key, value in cell.items() if key != "_filing_rank"}
            if not any(
                {key: value for key, value in held.items() if key != "_filing_rank"}
                == comparable for held in period_rows
            ):
                period_rows.append(cell)

    series: list[list[list[dict[str, Any]]]] = []
    available_cells = 0
    for series_key, periods in candidates.items():
        ordered_periods = sorted(
            periods, key=lambda period: (period[1], period[0]), reverse=True,
        )
        groups: list[list[dict[str, Any]]] = []
        for period_key in ordered_periods:
            held = periods[period_key]
            ambiguous = len({item["value"] for item in held}) > 1
            ambiguity_ref = (
                "numeric-period-ambiguity:" + content_hash({
                    "series": series_key, "period": period_key,
                    "line_hashes": sorted(item["line_content_hash"] for item in held),
                })[:32] if ambiguous else None
            )
            group: list[dict[str, Any]] = []
            for item in sorted(held, key=lambda row: row["line_id"]):
                item = {key: value for key, value in item.items() if key != "_filing_rank"}
                item["status"] = "ambiguous" if ambiguous else "filed"
                item["ambiguity_ref"] = ambiguity_ref
                group.append(item)
            groups.append(group)
        available_cells += sum(len(group) for group in groups)
        series.append(groups[: int(policy["max_periods_per_series"])])

    statement_rank = {"income": 0, "cash": 1, "balance": 2}
    selected_groups: list[list[dict[str, Any]]] = []
    selected_count = 0
    for depth in range(int(policy["max_periods_per_series"])):
        at_depth = [groups[depth] for groups in series if len(groups) > depth]
        at_depth.sort(key=lambda group: (
            statement_rank.get(group[0]["statement"], 3),
            group[0]["dimension_axis"] is not None,
            group[0]["concept"], str(group[0]["dimension_member"] or ""),
            group[0]["line_id"],
        ))
        for group in at_depth:
            # Never show one side of a same-source conflict. If the complete
            # ambiguity group does not fit, omit that period as a whole.
            if selected_count + len(group) <= int(policy["max_total_cells"]):
                selected_groups.append(group)
                selected_count += len(group)
        if selected_count >= int(policy["max_total_cells"]):
            break
    after_series_limit = sum(
        len(group) for groups in series for group in groups
    )
    body = {
        "schema_version": NUMERIC_CONTEXT_SCHEMA_VERSION,
        "policy": dict(policy),
        "filing_authorities": filing_authorities,
        "available_cells": available_cells,
        "after_series_limit_cells": after_series_limit,
        "after_total_limit_cells": selected_count,
        "included_cells": selected_count,
        "omitted_by_series_limit": available_cells - after_series_limit,
        "omitted_by_total_limit": after_series_limit - selected_count,
        "omitted_by_prompt_limit": 0,
        "truncated": selected_count < available_cells,
        "cells": [cell for group in selected_groups for cell in group],
    }
    return ({**body, "content_hash": content_hash(body)}, selected_groups)


def _context_with_prompt_proof(
    base: Mapping[str, Any], selected: list[dict[str, Any]], *,
    prompt_byte_limit: int, base_prompt_bytes: int,
    fixed_prompt_bytes: int, selected_cell_bytes: int,
    hash_content: bool = False,
) -> tuple[dict[str, Any], int]:
    """Return a context whose recorded prompt size equals its rendered size."""

    from .company_model_spec import _numeric_period_table_header

    context_body = {
        key: value for key, value in base.items() if key != "content_hash"
    }
    context_body.update({
        "prompt_byte_limit": prompt_byte_limit,
        "base_prompt_bytes": base_prompt_bytes,
        "prompt_bytes": 0,
        "included_cells": len(selected),
        "omitted_by_prompt_limit": int(base["after_total_limit_cells"]) - len(selected),
        "truncated": len(selected) < int(base["available_cells"]),
        "cells": list(selected),
    })
    # The decimal prompt size is itself rendered in the prompt. It converges
    # after at most a digit-width change; the hash is fixed-width.
    for _ in range(8):
        context = {
            **context_body,
            "content_hash": (
                content_hash(context_body) if hash_content else "0" * 64
            ),
        }
        size = (
            fixed_prompt_bytes
            + len(_numeric_period_table_header(context).encode("utf-8"))
            + selected_cell_bytes
        )
        if size == context_body["prompt_bytes"]:
            return context, size
        context_body["prompt_bytes"] = size
    raise CompanyModelStateError("model specification prompt size did not stabilize")


def _fit_prompt_budget(
    state_body: Mapping[str, Any], context: Mapping[str, Any],
    groups: list[list[dict[str, Any]]], *, prompt_byte_limit: int,
) -> dict[str, Any]:
    """Select complete numeric evidence groups under the whole prompt bound."""

    if (isinstance(prompt_byte_limit, bool) or not isinstance(prompt_byte_limit, int)
            or prompt_byte_limit <= 0):
        raise ValueError("model_spec max_input_tokens must be a positive integer")

    from .company_model_spec import (
        _numeric_period_cell_line, _numeric_period_table, build_prompt,
    )

    unavailable = _numeric_period_table({"numeric_context": None})
    fixed_prompt_bytes = (
        len(build_prompt({**state_body, "numeric_context": None}).encode("utf-8"))
        - len(unavailable.encode("utf-8"))
    )

    # First measure the complete non-cell prompt. This includes all instructions,
    # schemas, statement structure, filing authorities, and truthful omission
    # metadata. If that cannot fit, dropping evidence would not make the call legal.
    base_size = 0
    for _ in range(8):
        base_context, measured = _context_with_prompt_proof(
            context, [], prompt_byte_limit=prompt_byte_limit,
            base_prompt_bytes=base_size, fixed_prompt_bytes=fixed_prompt_bytes,
            selected_cell_bytes=0,
        )
        if measured == base_size:
            break
        base_size = measured
    else:
        raise CompanyModelStateError(
            "model specification base prompt size did not stabilize"
        )
    if base_size > prompt_byte_limit:
        raise CompanyModelPromptBudgetError(
            base_prompt_bytes=base_size, prompt_byte_limit=prompt_byte_limit,
        )

    selected: list[dict[str, Any]] = []
    selected_cell_bytes = 0
    final_context = base_context
    for group in groups:
        group_bytes = sum(
            1 + len(_numeric_period_cell_line(cell).encode("utf-8"))
            for cell in group
        )
        candidate, size = _context_with_prompt_proof(
            context, [*selected, *group], prompt_byte_limit=prompt_byte_limit,
            base_prompt_bytes=base_size, fixed_prompt_bytes=fixed_prompt_bytes,
            selected_cell_bytes=selected_cell_bytes + group_bytes,
        )
        if size <= prompt_byte_limit:
            selected.extend(group)
            selected_cell_bytes += group_bytes
            final_context = candidate
    # Rebuild once because groups skipped after the last accepted group change
    # only the omitted count, which the candidate already derives from selection.
    final_context, final_size = _context_with_prompt_proof(
        context, selected, prompt_byte_limit=prompt_byte_limit,
        base_prompt_bytes=base_size, fixed_prompt_bytes=fixed_prompt_bytes,
        selected_cell_bytes=selected_cell_bytes, hash_content=True,
    )
    rendered_size = len(build_prompt(
        {**state_body, "numeric_context": final_context}
    ).encode("utf-8"))
    if rendered_size != final_size:
        raise CompanyModelStateError(
            "model specification prompt byte accounting differs from rendering"
        )
    if final_size > prompt_byte_limit:  # defensive invariant
        raise CompanyModelStateError("bounded model specification prompt exceeds its input bound")
    return final_context


def build_company_model_state(
    missions: Any, company_ref: str, *, ticker: str | None = None,
    industry_classification: str | None = None,
    numeric_context_policy: Mapping[str, Any] | None = None,
    prompt_byte_limit: int = DEFAULT_MODEL_SPEC_PROMPT_BYTES,
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

    policy = validate_numeric_context_policy(numeric_context_policy)
    statements: dict[str, list[dict[str, Any]]] = {}
    seen: dict[str, set[str]] = {}
    lines_by_ingest: dict[str, list[dict[str, Any]]] = {}
    # Newest first, so when a line's structure changed between filings the
    # current disclosure is the one that survives deduplication.
    for filing in reversed(filings):
        filing_lines = missions.statement_lines(filing["ingest_id"])
        lines_by_ingest[str(filing["ingest_id"])] = filing_lines
        for row in filing_lines:
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

    numeric_context, numeric_groups = _numeric_context(
        filings, lines_by_ingest,
        structure_series={
            (statement, line["concept"])
            for statement, lines in statements.items() for line in lines
        },
        policy=policy,
    )
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
        "numeric_context": numeric_context,
    }
    if industry_classification:
        body["industry_classification"] = str(industry_classification)
    # F10: proxy evidence is a separate authority and never enters statement
    # rows. Carry its current, explicitly mapped records into the hashed model
    # state so a new source-series version causes a fresh specification.
    connection = getattr(missions, "connection", None)
    if connection is not None and connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND "
            "name='market_proxy_claim_versions'").fetchone() is not None:
        rows = connection.execute(
            "SELECT p.record_json FROM market_proxy_claim_versions p JOIN "
            "claim_index_entry_versions i ON i.claim_version_ref=p.claim_version_ref "
            "WHERE i.evidence_kind='market_proxy' AND i.version_number=(SELECT "
            "MAX(ix.version_number) FROM claim_index_entry_versions ix WHERE "
            "ix.entry_ref=i.entry_ref) AND "
            "p.target_subject_ref=? AND p.version_number=(SELECT MAX(x.version_number) "
            "FROM market_proxy_claim_versions x WHERE x.mapping_ref=p.mapping_ref) "
            "ORDER BY p.mapping_ref", (company_ref,)).fetchall()
        proxies = [json.loads(row["record_json"]) for row in rows]
        if proxies:
            body["market_proxies"] = proxies
    body["numeric_context"] = _fit_prompt_budget(
        body, numeric_context, numeric_groups,
        prompt_byte_limit=prompt_byte_limit,
    )
    return {**body, "state_hash": content_hash(body)}


__all__ = [
    "MAX_CONCEPTS_PER_STATEMENT",
    "MAX_FILINGS",
    "DEFAULT_NUMERIC_CONTEXT_POLICY",
    "DEFAULT_MODEL_SPEC_CALL_BUDGET",
    "DEFAULT_MODEL_SPEC_PROMPT_BYTES",
    "NUMERIC_CONTEXT_SCHEMA_VERSION",
    "CompanyModelStateError",
    "CompanyModelPromptBudgetError",
    "build_company_model_state",
    "model_spec_numeric_context_config",
    "model_spec_prompt_byte_limit",
    "validate_numeric_context_policy",
]

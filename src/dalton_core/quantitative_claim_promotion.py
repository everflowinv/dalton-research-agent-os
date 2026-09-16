"""C2-4: the numbers this install already holds, turned into quantitative Claims.

On 2026-09-16 the Ledger held 6,390 Claim versions of which **22** were
quantitative, and all 22 were the same measure -- ``quarterly_revenue_yoy_growth``
-- produced by the one deterministic SEC rule that exists.  At the same moment
the same database held:

  * 16,903 rows in ``coverage_mission_statement_lines`` -- nine filed
    statements per company across five companies, each line carrying its
    accession, its XBRL concept, its period and its unit;
  * 2,432 rows in ``coverage_mission_metric_observations``;
  * 8 rows in ``coverage_mission_document_figures``.

None of them had ever become a Claim.  That single gap is what makes the
Dossier fail ``numbers_without_refs`` and the conviction call fail
``market_view_not_supported_by_cited_rows``: the drafting lanes are asked to
write about a company whose Ledger contains no numbers at all, so either they
invent one (and the verifier catches it) or they say nothing.

This module closes it **deterministically**.  No model is called anywhere in
this file.  Every promoted number is:

  * re-verified against the filed row it came from, through the existing
    ``DocumentFigureResolver.verify_statement_line`` re-check, so a promotion
    is a replay of the filing rather than a reading of it;
  * anchored to that exact row -- ``line_id``, ``concept``, ``ordinal``,
    accession and the filing's own content hash -- so every Claim can be
    walked back to bytes the SEC published;
  * normalised once: period as the filed period, value as canonical decimal
    text, unit/currency/scale from the filed unit, never re-scaled;
  * named so the claim index's *rule* tagger recognises it
    (``claim_index_tagging.QUANTITATIVE_ASPECT_RULES``), which keeps the whole
    path free of model calls end to end;
  * idempotent by ``(company_ref, metric_or_aspect, period, origin_ref)``, so
    a new filing adds its own numbers and changes nothing already promoted.

What this module does **not** do is decide that a number may enter the Ledger.
That decision belongs to the governance policy: a candidate reaches
``claim_versions`` either through human review or through a named deterministic
auto-commit rule the owner has signed.  The promoter stages, records exactly
what it staged, and says which of the two doors each number is waiting at.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .claim_index_figures import DocumentFigureResolver
from .research_verification import (
    SEC_STATEMENT_LINE_AUTHORITY_MODE,
    SEC_STATEMENT_LINE_OPERATION,
    SEC_STATEMENT_LINE_SOURCE_REF,
    SEC_STATEMENT_LINE_SOURCE_VERIFIER_HASH,
    SEC_STATEMENT_LINE_SOURCE_VERIFIER_REF,
)
from .store import authorization_flag, canonical_json, content_hash

SCHEMA_VERSION = "0.1"
_SCHEMA = Path(__file__).with_name("quantitative_claim_promotion_schema.sql")

# Every Claim promoted here says the same thing about where it came from, and
# it is the same word the existing deterministic SEC rule uses so the two kinds
# of filed number are not told apart by accident.
FILED_BASIS = "official-filing-xbrl"
PRODUCER_REF = "rule:quantitative-claim-promotion:0.1"

_DECIMAL_RE = re.compile(r"^-?(0|[1-9][0-9]*)(?:\.[0-9]+)?$")


class QuantitativeClaimPromotionError(ValueError):
    pass


# -- what a filed concept is called ------------------------------------------
#
# The name matters twice.  It is what a human reads in the Cockpit's
# conclusion list, and it is what ``claim_index_tagging.quantitative_aspect``
# pattern-matches to decide the Claim's aspect without a model call.  Each name
# below is chosen to hit one of those patterns; the comment names which.
CONCEPT_METRICS: Mapping[str, tuple[str, str]] = {
    # concept -> (metric_or_aspect, Chinese label for the statement)
    # -> segments_and_mix (matches "revenue")
    "us-gaap:Revenues": ("revenue", "营业收入"),
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax": ("revenue", "营业收入"),
    # -> supply_and_cost (matches "cost")
    "us-gaap:CostOfRevenue": ("cost of revenue", "营业成本"),
    "us-gaap:CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization":
        ("cost of revenue", "营业成本"),
    # -> supply_and_cost (matches "expense")
    "us-gaap:SellingGeneralAndAdministrativeExpense":
        ("selling, general and administrative expense", "销售、一般及管理费用"),
    "us-gaap:ResearchAndDevelopmentExpense": ("research and development expense", "研发费用"),
    # -> management_and_capital_allocation (matches "profit"/"operating income")
    "us-gaap:GrossProfit": ("gross profit", "毛利"),
    "us-gaap:OperatingIncomeLoss": ("operating income", "营业利润"),
    "us-gaap:NetIncomeLoss": ("net income", "净利润"),
    # -> management_and_capital_allocation (matches "eps")
    "us-gaap:EarningsPerShareDiluted": ("diluted eps", "摊薄每股收益"),
    "us-gaap:EarningsPerShareBasic": ("basic eps", "基本每股收益"),
    # -> management_and_capital_allocation (matches "cash flow"/"capex"/"repurchase")
    "us-gaap:NetCashProvidedByUsedInOperatingActivities":
        ("operating cash flow", "经营活动现金流"),
    "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment": ("capex", "资本开支"),
    "us-gaap:PaymentsForRepurchaseOfCommonStock": ("share repurchase", "股份回购"),
}

# A line that carries an XBRL dimension is a breakdown of its parent: the same
# concept split by segment, geography or service line.  It is promoted under
# its own name so ``segments_and_mix`` picks it up and so a segment number can
# never be mistaken for the consolidated one.
BREAKDOWN_SUFFIX = "segment "

# The two ratios worth deriving, because a margin is what a reader asks for and
# neither is filed as its own line.  Both are computed from two lines of the
# *same filing and the same period*, so the derivation is exact and its
# provenance is both rows rather than a guess.
DERIVED_RATIOS: tuple[tuple[str, str, str, str], ...] = (
    ("gross margin", "毛利率", "us-gaap:GrossProfit", "revenue"),
    ("operating margin", "营业利润率", "us-gaap:OperatingIncomeLoss", "revenue"),
)

# WP-F: a derived margin is admitted as a **ratio**, not as a percentage.
#
# This is the Ledger's rule, not a preference.  The only deterministic
# derivation the numeric contract knows is ``NumericVerificationSpec`` with
# operator ``ratio``, and ``verify_numeric_spec`` fixes that operator's output
# metadata at ``unit="ratio", currency=null, scale="one"``.  A claim asserting
# ``55.41 percent`` therefore has no verifier that can recompute it, and a
# number no verifier can recompute is exactly the number this system refuses.
#
# So the Claim says ``0.554092`` with unit ``ratio``; the sentence a person
# reads spells the same number as a percentage beside it, which is a rendering
# of the asserted value rather than a second assertion.
RATIO_UNIT = "ratio"
RATIO_DIGITS = 6
RATIO_ROUNDING = {"mode": "half_up", "digits": RATIO_DIGITS}
RATIO_OPERATOR = "ratio"

# Filed units, and what a Claim calls them.  ``scale`` is always ``one``: the
# filed value is used exactly as filed, because re-scaling a number is the one
# operation that turns a citation into a paraphrase.
UNIT_WIRE: Mapping[str, tuple[str, str | None]] = {
    "usd": ("USD", "USD"),
    "eur": ("EUR", "EUR"),
    "usdpershare": ("USD per share", "USD"),
    "eurpershare": ("EUR per share", "EUR"),
    "shares": ("shares", None),
    "pure": ("ratio", None),
}


def canonical_decimal(value: Any) -> str | None:
    """The filed number as the canonical decimal text Ledger 0.2 requires.

    ``None`` when the row does not report a number.  Never rounds and never
    re-scales: the only normalisation is dropping a trailing exponent form and
    an insignificant trailing zero, both of which are spellings rather than
    values.
    """

    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite():
        return None
    wire = format(amount.normalize(), "f")
    if wire in ("-0", "-0.0"):
        wire = "0"
    return wire if _DECIMAL_RE.fullmatch(wire) else None


def period_wire(period_start: Any, period_end: Any) -> str | None:
    """The filed period, spelled the way every other Claim spells one.

    A flow line names the window it covers; an instant line (a balance) names
    the date it was true on.  Both are the filing's own dates, never derived.
    """

    end = str(period_end or "").strip()
    if not end:
        return None
    start = str(period_start or "").strip()
    return f"{start}..{end}" if start else f"as of {end}"


def unit_wire(unit: Any) -> tuple[str, str | None] | None:
    """(unit, currency) for a filed unit token, or None if it is unknown.

    Unknown is a refusal rather than a guess: a Claim whose unit was invented
    is worse than a Claim that does not exist.
    """

    token = str(unit or "").strip().lower().replace("-", "").replace("_", "")
    return UNIT_WIRE.get(token)


def metric_for_line(line: Mapping[str, Any]) -> tuple[str, str] | None:
    """The measure name and its Chinese label for one filed statement line."""

    mapped = CONCEPT_METRICS.get(str(line.get("concept") or ""))
    if mapped is None:
        return None
    metric, label = mapped
    if line.get("dimension_member") or line.get("dimension_axis"):
        # The member goes *into the name*.  Without it every segment of one
        # period shares a ``metric_or_aspect`` and therefore one claim-index
        # dedupe group, and the index would keep exactly one of them as
        # canonical -- which is not de-duplication, it is deleting four fifths
        # of the segment disclosure.
        member = str(line.get("label") or line.get("dimension_member") or "").strip()
        if not member:
            return f"{BREAKDOWN_SUFFIX}{metric}", f"{label}（分部）"
        return f"{BREAKDOWN_SUFFIX}{metric}: {member}", f"{label}（分部：{member}）"
    return metric, label


def _amount_text(value: str, unit: str, currency: str | None) -> str:
    """How one filed number reads in the Cockpit's conclusion list."""

    if unit == "percent":
        return f"{value}%"
    if currency and unit.startswith(currency):
        return f"{currency} {value}" if unit == currency else f"{currency} {value}/股"
    return f"{value} {unit}"


def statement_line_proposal(
    line: Mapping[str, Any], filing: Mapping[str, Any],
) -> dict[str, Any] | None:
    """One filed statement line as a quantitative Claim proposal, or None.

    ``None`` means "this row is not a number a Claim can be made of" -- an
    unmapped concept, a line with no value, a unit nobody has written down.
    Silence rather than a guess is the whole discipline here.
    """

    mapped = metric_for_line(line)
    if mapped is None:
        return None
    metric, label = mapped
    value = canonical_decimal(line.get("value"))
    if value is None:
        return None
    units = unit_wire(line.get("unit"))
    if units is None:
        return None
    unit, currency = units
    period = period_wire(line.get("period_start"), line.get("period_end"))
    if period is None:
        return None
    entity = str(filing.get("entity_name") or filing.get("company_ref") or "")
    form = str(filing.get("form") or "")
    accession = str(filing.get("accession") or "")
    statement = (
        f"{entity}在{form}（accession {accession}）中列报的{label}，"
        f"期间 {period}，为 {_amount_text(value, unit, currency)}。"
        f"该数字直接取自申报的 XBRL 行 {line.get('concept')}"
        f"（{line.get('label')}，第 {line.get('ordinal')} 行），未经任何换算。"
    )
    return {
        "origin_kind": "statement_line",
        "origin_ref": str(line["line_id"]),
        "company_ref": str(filing["company_ref"]),
        "metric_or_aspect": metric,
        "period": period,
        "basis": FILED_BASIS,
        "claim_kind": "quantitative",
        "value": value,
        "unit": unit,
        "currency": currency,
        "scale": "one",
        "normalized_statement": statement,
        "source_document_ref": f"sec:filing:{accession}",
        # Everything a reader needs to find the exact bytes again.
        "anchor": {
            "line_id": str(line["line_id"]),
            "ingest_id": str(line["ingest_id"]),
            "accession": accession,
            "form": form,
            "filed": str(filing.get("filed") or ""),
            "report_date": str(filing.get("report_date") or ""),
            "statement": str(line.get("statement") or ""),
            "ordinal": int(line["ordinal"]),
            "concept": str(line.get("concept") or ""),
            "as_reported_label": str(line.get("label") or ""),
            "dimension_axis": line.get("dimension_axis"),
            "dimension_member": line.get("dimension_member"),
            "unit_as_filed": str(line.get("unit") or ""),
            "value_as_filed": str(line.get("value")),
            "filing_content_hash": str(filing.get("content_hash") or ""),
            "source_record_refs": _source_records(filing),
        },
    }


def _source_records(filing: Mapping[str, Any]) -> list[Any]:
    raw = filing.get("source_record_refs")
    if isinstance(raw, list):
        return list(raw)
    try:
        parsed = json.loads(filing.get("source_record_refs_json") or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def derived_ratio_proposals(
    proposals: Sequence[Mapping[str, Any]], filing: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Gross and operating margin, computed from two lines of one filing.

    Only from consolidated lines of the same period: dividing a segment's
    profit by the group's revenue is a number with no meaning, and it is the
    kind of number a model would happily produce.
    """

    by_metric_period: dict[tuple[str, str], Mapping[str, Any]] = {}
    for item in proposals:
        if item["origin_kind"] != "statement_line":
            continue
        if item["metric_or_aspect"].startswith(BREAKDOWN_SUFFIX):
            continue
        by_metric_period.setdefault((item["metric_or_aspect"], item["period"]), item)
    out: list[dict[str, Any]] = []
    for metric, label, concept, denominator_metric in DERIVED_RATIOS:
        numerator_metric = CONCEPT_METRICS[concept][0]
        for (held_metric, period), numerator in list(by_metric_period.items()):
            if held_metric != numerator_metric:
                continue
            denominator = by_metric_period.get((denominator_metric, period))
            if denominator is None:
                continue
            if numerator["currency"] != denominator["currency"]:
                continue
            base = Decimal(denominator["value"])
            if base == 0:
                continue
            # Quantised exactly the way ``verify_numeric_spec`` quantises the
            # ``ratio`` operator, because the Ledger's verifier recomputes this
            # number and a rounding that disagreed by one digit would be a
            # rejected candidate rather than a wrong one.
            ratio = (Decimal(numerator["value"]) / base).quantize(
                Decimal(1).scaleb(-RATIO_DIGITS), rounding=ROUND_HALF_UP)
            value = canonical_decimal(ratio)
            if value is None:
                continue
            percent = canonical_decimal(
                (Decimal(value) * Decimal(100)).quantize(Decimal("0.01"),
                                                         rounding=ROUND_HALF_UP))
            entity = str(filing.get("entity_name") or filing.get("company_ref") or "")
            identity = {"numerator": numerator["origin_ref"],
                        "denominator": denominator["origin_ref"]}
            statement = (
                f"{entity}期间 {period} 的{label}为 {value}（即 {percent}%），"
                f"由同一份 {filing.get('form')}（accession {filing.get('accession')}）中列报的"
                f"{CONCEPT_METRICS[concept][1]} {numerator['value']} 除以"
                f"营业收入 {denominator['value']} 得出；两项均为申报原值，未经换算。"
            )
            out.append({
                "origin_kind": "derived_ratio",
                "origin_ref": "statement-line-ratio:" + content_hash(identity)[:32],
                "company_ref": numerator["company_ref"],
                "metric_or_aspect": metric,
                "period": period,
                "basis": FILED_BASIS,
                "claim_kind": "quantitative",
                "value": value,
                "unit": RATIO_UNIT,
                "currency": None,
                "scale": "one",
                "normalized_statement": statement,
                "source_document_ref": numerator["source_document_ref"],
                # Both rows, in order, so the Ledger's numeric verifier can
                # point at the two filed values it divided.
                "numerator_ref": numerator["origin_ref"],
                "denominator_ref": denominator["origin_ref"],
                "anchor": {
                    "numerator": numerator["anchor"],
                    "denominator": denominator["anchor"],
                    "operation": "numerator / denominator",
                    "rounding": dict(RATIO_ROUNDING),
                },
            })
    return out


def document_figure_proposal(figure: Mapping[str, Any]) -> dict[str, Any] | None:
    """One verified document figure as a quantitative Claim proposal.

    This is where bookings, backlog, headcount and a guidance range come from:
    they are not XBRL statement lines, they are numbers a company published in
    prose and the figures pass already verified against the quoted bytes.
    Only ``company-filed-document`` grade is promoted -- a number a person said
    on a call is evidence of what was said, not of what was reported.
    """

    if str(figure.get("source_grade")) != "company-filed-document":
        return None
    value = canonical_decimal(figure.get("value"))
    if value is None:
        return None
    unit = str(figure.get("unit") or "").strip()
    period = str(figure.get("period") or "").strip()
    if not unit or not period:
        return None
    currency = figure.get("currency") or None
    scale = str(figure.get("scale") or "one").strip() or "one"
    label = str(figure.get("as_reported_label") or figure.get("metric_ref") or "")
    statement = (
        f"公司在其自有披露文件中列报的「{label}」，期间 {period}，"
        f"为 {_amount_text(value, unit, currency)}"
        f"{'（' + scale + '）' if scale != 'one' else ''}。"
        f"该数字已对照原文引文 {figure.get('quote_id')} 逐位校验。"
    )
    return {
        "origin_kind": "document_figure",
        "origin_ref": str(figure["figure_id"]),
        "company_ref": str(figure["company_ref"]),
        "metric_or_aspect": str(figure["metric_ref"]),
        "period": period,
        "basis": "company-filed-document",
        "claim_kind": "quantitative",
        "value": value,
        "unit": unit,
        "currency": currency,
        "scale": scale,
        "normalized_statement": statement,
        "source_document_ref": str(figure["document_ref"]),
        "anchor": {
            "figure_id": str(figure["figure_id"]),
            "figure_content_hash": str(figure.get("content_hash") or ""),
            "document_ref": str(figure["document_ref"]),
            "review_ref": str(figure.get("review_ref") or ""),
            "quote_id": str(figure.get("quote_id") or ""),
            "citation_hash": content_hash({
                "quote_id": figure.get("quote_id"),
                "raw_text": figure.get("citation_text"),
            }),
            "source_manifest_hash": str(figure.get("source_manifest_hash") or ""),
            "verified_by": str(figure.get("verified_by") or ""),
        },
    }


def promotion_id_for(proposal: Mapping[str, Any]) -> str:
    return "quantitative-claim-promotion:" + content_hash({
        "company_ref": proposal["company_ref"],
        "metric_or_aspect": proposal["metric_or_aspect"],
        "period": proposal["period"],
        "origin_ref": proposal["origin_ref"],
    })[:32]


def statement_line_proposals(
    connection: sqlite3.Connection,
    *,
    company_ref: str | None = None,
    limit: int = 500,
    since_ingest_ids: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Every promotable number in the filed statements, newest filing first.

    Incremental by construction: a filing whose lines are already promoted
    yields proposals whose promotion ids already exist, and the ledger refuses
    them as duplicates without a single further read.
    """

    query = "SELECT * FROM coverage_mission_statement_filings"
    params: list[Any] = []
    clauses: list[str] = []
    if company_ref is not None:
        clauses.append("company_ref=?")
        params.append(company_ref)
    if since_ingest_ids:
        clauses.append("ingest_id IN (%s)" % ",".join("?" * len(since_ingest_ids)))
        params.extend(since_ingest_ids)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY filed DESC, accession DESC"
    proposals: list[dict[str, Any]] = []
    for filing_row in connection.execute(query, params).fetchall():
        filing = {key: filing_row[key] for key in filing_row.keys()}
        proposals.extend(filing_proposals(connection, filing))
        if len(proposals) >= limit:
            break
    return proposals[:limit]


def filing_rows(
    connection: sqlite3.Connection, ingest_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One filing and its lines, as Core holds them."""

    filing_row = connection.execute(
        "SELECT * FROM coverage_mission_statement_filings WHERE ingest_id=?",
        (str(ingest_id),),
    ).fetchone()
    if filing_row is None:
        raise QuantitativeClaimPromotionError(
            f"no statement filing {ingest_id!r} in this Core")
    filing = {key: filing_row[key] for key in filing_row.keys()}
    lines = [
        {key: row[key] for key in row.keys()}
        for row in connection.execute(
            "SELECT * FROM coverage_mission_statement_lines WHERE ingest_id=? ORDER BY ordinal",
            (filing["ingest_id"],),
        ).fetchall()
    ]
    return filing, lines


def filing_proposals(
    connection: sqlite3.Connection, filing: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Every promotable number in one filing, filed rows first then derivations.

    Deterministic and order-stable: the lines come out of Core by ordinal and
    the derivations come out of the lines.  This is the function the
    auto-commit rule replays, so "what the promoter proposed" and "what the
    Ledger will accept" are the same computation, not two that agree today.
    """

    _, lines = filing_rows(connection, filing["ingest_id"])
    filed: list[dict[str, Any]] = []
    for line in lines:
        proposal = statement_line_proposal(line, filing)
        if proposal is not None:
            filed.append(proposal)
    filed.extend(derived_ratio_proposals(filed, filing))
    return filed


# -- WP-F: the staging chain a filed statement line travels -------------------
#
# Everything below is deterministic and reads only Core.  It builds, for one
# promotable number, the exact five records the Ledger's candidate contract
# asks for -- authority material, source VerificationBundle, numeric authority,
# CandidateEvidence, CandidateClaim -- and nothing else.  The auto-commit rule
# in ``research_auto_commit`` calls the same builder and refuses to admit a
# candidate that is not byte-identical to what it rebuilds, so a hand-edited
# sentence or a re-typed digit is a rejection rather than a Claim.


def line_projection(line: Mapping[str, Any]) -> dict[str, Any]:
    """One filed line, as the authority material carries it.

    ``value`` is canonicalised because the Ledger's numeric verifier extracts
    it with a JSON pointer and rejects a non-canonical decimal; the filer's own
    spelling is kept beside it so nothing is lost.
    """

    return {
        "line_id": str(line["line_id"]),
        "ordinal": int(line["ordinal"]),
        "statement": str(line["statement"]),
        "concept": str(line["concept"]),
        "label": str(line["label"]),
        "level": int(line["level"]),
        "parent_concept": line["parent_concept"],
        "is_breakdown": bool(line["is_breakdown"]),
        "dimension_axis": line["dimension_axis"],
        "dimension_member": line["dimension_member"],
        "period_start": line["period_start"],
        "period_end": line["period_end"],
        "value": canonical_decimal(line["value"]),
        "value_as_filed": None if line["value"] is None else str(line["value"]),
        "unit": str(line["unit"]),
    }


def _finding(
    code: str, *, ok: bool, path: str, expected: Any, observed: Any, message: str
) -> dict[str, Any]:
    wire = {
        "code": code,
        "severity": "info" if ok else "error",
        "status": "pass" if ok else "fail",
        "path": path,
        "expected": None if expected is None else str(expected),
        "observed": None if observed is None else str(observed),
        "message": message if ok else message + " drifted",
    }
    wire["content_hash"] = content_hash(wire)
    return wire


class SecStatementLineAuthorityResolver:
    """The ``sec_statement_line_authority`` provenance mode (WP-F).

    The material is the filed line (or the two filed lines a margin divides)
    together with the filing row it belongs to.  The chain is:

        statement dispatch (mission-authorised, settled) -> ingested filing
        (accession, approved connector-governance record, raw spool artifacts,
        own content hash) -> the line, by ordinal and concept

    There is no connector SourceEnvelope in it, and that is not an omission:
    the SEC financial-statements lane parses the filer's XBRL exhibit in a
    child process and records the result through ``CoverageMissionAuthority``,
    whose two tables are append-only and authority-gated.  Inventing an
    envelope for a call that never went through the connector port would be a
    fabricated provenance, so the filing row is named as the envelope of this
    mode and the Ledger commit gate is taught to verify it as one.
    """

    provenance_mode = SEC_STATEMENT_LINE_AUTHORITY_MODE
    verifier = (
        SEC_STATEMENT_LINE_SOURCE_VERIFIER_REF,
        SEC_STATEMENT_LINE_SOURCE_VERIFIER_HASH,
    )

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._figures = DocumentFigureResolver(connection)
        self._proposals: dict[str, dict[str, dict[str, Any]]] = {}

    # -- reads -----------------------------------------------------------

    def verify_statement_line(self, line_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        """Delegate to the re-check that already existed (``claim_index_figures``)."""

        return self._figures.verify_statement_line(line_id)

    def filing(self, ingest_id: str) -> dict[str, Any]:
        filing, _ = filing_rows(self.connection, ingest_id)
        return filing

    def dispatch(self, dispatch_id: Any) -> dict[str, Any] | None:
        if not dispatch_id:
            return None
        row = self.connection.execute(
            "SELECT * FROM coverage_mission_statement_dispatches WHERE dispatch_id=?",
            (str(dispatch_id),),
        ).fetchone()
        return None if row is None else {key: row[key] for key in row.keys()}

    def proposals(self, ingest_id: str) -> dict[str, dict[str, Any]]:
        """Every promotable number of one filing, by ``origin_ref``.

        Cached per filing because the auto-commit rule replays one candidate at
        a time and a 500-line 10-Q would otherwise be re-derived 500 times.
        """

        key = str(ingest_id)
        held = self._proposals.get(key)
        if held is None:
            filing = self.filing(key)
            held = {item["origin_ref"]: item for item in filing_proposals(self.connection, filing)}
            self._proposals[key] = held
        return held

    def proposal(self, ingest_id: str, origin_ref: str) -> dict[str, Any]:
        held = self.proposals(ingest_id).get(str(origin_ref))
        if held is None:
            raise QuantitativeClaimPromotionError(
                f"{origin_ref!r} is not a number this Core derives from {ingest_id!r}")
        return held

    # -- the material -----------------------------------------------------

    def payload_for(self, proposal: Mapping[str, Any]) -> dict[str, Any]:
        """What the material asserts, re-derived from Core every single time."""

        anchor = proposal["anchor"]
        ingest_id = (anchor.get("ingest_id")
                     or anchor["numerator"]["ingest_id"])
        filing, lines = filing_rows(self.connection, ingest_id)
        by_id = {item["line_id"]: item for item in lines}
        if proposal["origin_kind"] == "statement_line":
            wanted = [proposal["origin_ref"]]
            derivation: dict[str, Any] | None = None
        else:
            wanted = [proposal["numerator_ref"], proposal["denominator_ref"]]
            derivation = {
                "operator": RATIO_OPERATOR,
                "numerator_line_id": wanted[0],
                "denominator_line_id": wanted[1],
                "expression": "numerator / denominator",
                "rounding": dict(RATIO_ROUNDING),
            }
        selected = []
        for line_id in wanted:
            if line_id not in by_id:
                raise QuantitativeClaimPromotionError(
                    f"line {line_id!r} does not belong to filing {ingest_id!r}")
            selected.append(line_projection(by_id[line_id]))
        return {
            "kind": proposal["origin_kind"],
            "company_ref": str(filing["company_ref"]),
            "cik": str(filing["cik"]),
            "entity_name": str(filing["entity_name"]),
            "accession": str(filing["accession"]),
            "form": str(filing["form"]),
            "filed": str(filing["filed"]),
            "report_date": str(filing["report_date"]),
            "ingest_id": str(filing["ingest_id"]),
            "dispatch_id": str(filing["dispatch_id"]),
            "governance_ref": str(filing["governance_ref"]),
            "governance_hash": str(filing["governance_hash"]),
            "filing_content_hash": str(filing["content_hash"]),
            "lines": selected,
            "metric_or_aspect": proposal["metric_or_aspect"],
            "period": proposal["period"],
            "basis": proposal["basis"],
            "value": proposal["value"],
            "unit": proposal["unit"],
            "currency": proposal["currency"],
            "scale": proposal["scale"],
            "normalized_statement": proposal["normalized_statement"],
            "derivation": derivation,
        }

    def build_material(self, proposal: Mapping[str, Any]) -> dict[str, Any]:
        """The filing row and its lines as an AuthoritySourceVerificationMaterial 0.2."""

        from .research_verification import validate_source_verification_material

        payload = self.payload_for(proposal)
        filing = self.filing(payload["ingest_id"])
        records = _source_records(filing)
        artifact_ref, artifact_hash = _raw_sink(records)
        base = {
            "schema_version": "0.2",
            "id": "sec-statement-line-material:" + content_hash({
                "origin_ref": proposal["origin_ref"],
                "ingest_id": payload["ingest_id"],
                "filing_hash": payload["filing_content_hash"],
            })[:32],
            "created_at": str(filing["recorded_at"]),
            # The filing row is the envelope of this mode: append-only,
            # authority-gated, and carrying the accession the SEC published.
            "source_envelope_ref": payload["ingest_id"],
            "source_envelope_hash": payload["filing_content_hash"],
            "artifact_ref": artifact_ref,
            "artifact_hash": artifact_hash,
            "source_ref": SEC_STATEMENT_LINE_SOURCE_REF,
            "source_type": "official_filing",
            "operation": SEC_STATEMENT_LINE_OPERATION,
            "provenance_mode": self.provenance_mode,
            # The explicit provenance edge: this material exists because of
            # exactly this filed row (or this pair of rows), at exactly the
            # anchor the promoter recorded.
            "authority_resolution_ref": proposal["origin_ref"],
            "authority_resolution_hash": content_hash(proposal["anchor"]),
            "source_record_refs": records,
            "next_cursor": None,
            "normalized_payload": payload,
            "normalized_payload_hash": hashlib.sha256(
                canonical_json(payload).encode("utf-8")).hexdigest(),
            "source_schema_hash": content_hash({
                "governance_ref": payload["governance_ref"],
                "governance_hash": payload["governance_hash"],
            }),
            "source_content_hash": payload["filing_content_hash"],
            "source_lineage": [
                SEC_STATEMENT_LINE_SOURCE_REF, payload["dispatch_id"],
                payload["ingest_id"], f"sec:filing:{payload['accession']}",
                proposal["origin_ref"],
            ],
            "published_at": None,
            "updated_at": None,
            "as_of": None,
            "retrieved_at": str(filing["recorded_at"]),
            "completeness": "enumerated",
            "status": "complete",
        }
        base["content_hash"] = content_hash(base)
        return validate_source_verification_material(base)

    # -- the deterministic verifier ---------------------------------------

    def verify_source_material(self, material: Mapping[str, Any]) -> dict[str, Any]:
        """Re-derive the whole chain out of Core and emit a bundle.

        ``CandidateStagingStore.stage(verification_mode="sec_statement_line_authority")``
        calls this and requires the caller's bundle to be byte-identical, the
        same contract every other Core-authority mode has.
        """

        from .research_verification import (
            validate_source_verification_material,
            validate_verification_bundle,
        )

        material_wire = validate_source_verification_material(material)
        if material_wire.get("provenance_mode") != self.provenance_mode:
            raise QuantitativeClaimPromotionError(
                f"the statement line verifier requires {self.provenance_mode} material")
        payload = material_wire["normalized_payload"]
        findings: list[dict[str, Any]] = []

        def check(code: str, observed: Any, expected: Any, path: str, message: str) -> None:
            ok = observed == expected
            findings.append(_finding(
                code, ok=ok, path=path,
                expected=canonical_json(expected) if isinstance(expected, (dict, list)) else expected,
                observed=canonical_json(observed) if isinstance(observed, (dict, list)) else observed,
                message=message))

        ingest_id = material_wire["source_envelope_ref"]
        filing, lines = filing_rows(self.connection, ingest_id)
        check("filing_content_hash", material_wire["source_envelope_hash"],
              str(filing["content_hash"]), "material.source_envelope_hash",
              "material binds the exact filing row Core holds")
        check("source_content_hash", material_wire["source_content_hash"],
              str(filing["content_hash"]), "material.source_content_hash",
              "material binds the filing as its source content")
        dispatch = self.dispatch(filing["dispatch_id"])
        check("dispatch_exists", dispatch is not None, True, "filing.dispatch_id",
              "the filing names the mission dispatch that fetched it")
        check("dispatch_settled",
              None if dispatch is None else dispatch["status"],
              "succeeded", "dispatch.status",
              "the dispatch that fetched this filing settled successfully")
        check("dispatch_company",
              None if dispatch is None else dispatch["company_ref"],
              str(filing["company_ref"]), "dispatch.company_ref",
              "the dispatch and the filing name one company")
        check("governance_binding",
              [payload.get("governance_ref"), payload.get("governance_hash")],
              [str(filing["governance_ref"]), str(filing["governance_hash"])],
              "material.normalized_payload.governance",
              "material names the approved connector-governance record the parse ran under")
        records = _source_records(filing)
        check("source_record_refs", material_wire["source_record_refs"], records,
              "material.source_record_refs",
              "material names the raw artifacts the filing was parsed from")
        try:
            artifact_ref, artifact_hash = _raw_sink(records)
        except QuantitativeClaimPromotionError:
            artifact_ref, artifact_hash = None, None
        check("artifact_ref", material_wire["artifact_ref"], artifact_ref,
              "material.artifact_ref", "raw spool artifact ref is the filing's own")
        check("artifact_hash", material_wire["artifact_hash"], artifact_hash,
              "material.artifact_hash", "raw spool artifact hash is the digest its ref names")

        origin_ref = material_wire["authority_resolution_ref"]
        try:
            proposal = self.proposal(str(ingest_id), str(origin_ref))
            rebuilt = self.payload_for(proposal)
            anchor_hash = content_hash(proposal["anchor"])
        except QuantitativeClaimPromotionError as exc:
            proposal, rebuilt, anchor_hash = None, str(exc), None
        check("payload_equals_core_derivation", payload, rebuilt,
              "material.normalized_payload",
              "material payload equals the number this Core derives from the filed rows")
        check("authority_resolution_hash", material_wire["authority_resolution_hash"],
              anchor_hash, "material.authority_resolution_hash",
              "material binds the exact filed-row anchor")

        by_id = {item["line_id"]: item for item in lines}
        cited = payload.get("lines") if isinstance(payload.get("lines"), list) else []
        for index, projected in enumerate(cited):
            line_id = projected.get("line_id") if isinstance(projected, Mapping) else None
            held = by_id.get(line_id)
            check(f"line_belongs_to_filing:{index}", held is not None, True,
                  f"material.normalized_payload.lines[{index}].line_id",
                  "the cited line belongs to this filing")
            check(f"line_projection:{index}", projected,
                  None if held is None else line_projection(held),
                  f"material.normalized_payload.lines[{index}]",
                  "the cited line equals the Core row")
        derivation = payload.get("derivation")
        if isinstance(derivation, Mapping):
            periods = {
                (item.get("period_start"), item.get("period_end"))
                for item in cited if isinstance(item, Mapping)
            }
            check("derivation_is_one_period", len(periods), 1,
                  "material.normalized_payload.lines",
                  "a derived ratio divides two lines of one period")
            check("derivation_is_two_lines", len(cited), 2,
                  "material.normalized_payload.lines",
                  "a derived ratio names exactly the two rows it divided")
            check("derivation_operator", derivation.get("operator"), RATIO_OPERATOR,
                  "material.normalized_payload.derivation.operator",
                  "the only derivation this mode admits is a ratio of two filed rows")

        check("source_ref", material_wire["source_ref"], SEC_STATEMENT_LINE_SOURCE_REF,
              "material.source_ref", "material is SEC filing authority")
        check("source_type", material_wire["source_type"], "official_filing",
              "material.source_type", "material is official filing evidence")
        check("operation", material_wire["operation"], SEC_STATEMENT_LINE_OPERATION,
              "material.operation", "material names the statements operation")
        check("source_lineage", material_wire["source_lineage"],
              [SEC_STATEMENT_LINE_SOURCE_REF, str(filing["dispatch_id"]),
               str(filing["ingest_id"]), f"sec:filing:{filing['accession']}",
               str(origin_ref)],
              "material.source_lineage",
              "material lineage is source, dispatch, filing, accession, row")

        verdict = "reject" if any(
            item["status"] == "fail" and item["severity"] == "error" for item in findings
        ) else "pass"
        base = {
            "schema_version": "0.1",
            "id": "sec-statement-line-source-verification:" + content_hash({
                "subject": material_wire["id"],
                "filing": str(filing["content_hash"]),
                "findings": [item["content_hash"] for item in findings],
            }),
            "created_at": material_wire["retrieved_at"],
            "kind": "source",
            "subject_ref": material_wire["id"],
            "subject_hash": material_wire["content_hash"],
            "verdict": verdict,
            # The same checkpoint ``verify_statement_line`` names, so the
            # source and numeric bundles of one candidate agree about which
            # filing they are verifications *of*.
            "checkpoint_ref": f"sec:filing:{filing['accession']}",
            "checkpoint_hash": str(filing["content_hash"]),
            "findings": findings,
            "verifier_ref": self.verifier[0],
            "verifier_hash": self.verifier[1],
        }
        base["content_hash"] = content_hash(base)
        return validate_verification_bundle(base)

    # -- the numeric authority --------------------------------------------

    def numeric_spec(self, material: Mapping[str, Any]) -> dict[str, Any]:
        """The ratio spec for a derived margin: two filed values, one division."""

        from .research_verification import validate_numeric_verification_spec

        payload = material["normalized_payload"]
        lines = payload["lines"]
        inputs = [
            {
                "name": name,
                "value": lines[index]["value"],
                "unit": "number",
                "currency": None,
                "scale": "one",
                "period": payload["period"],
                "source_material_ref": material["id"],
                "source_material_hash": material["content_hash"],
                "json_pointer": f"/lines/{index}/value",
                "extractor": "number",
            }
            for index, name in enumerate(("numerator", "denominator"))
        ]
        base = {
            "schema_version": "0.1",
            "id": "numeric-spec:sec-statement-line-ratio:" + content_hash({
                "material": material["id"],
                "material_hash": material["content_hash"],
            }),
            "created_at": material["created_at"],
            "operator": RATIO_OPERATOR,
            "inputs": inputs,
            "output_value": payload["value"],
            "output_unit": RATIO_UNIT,
            "output_currency": None,
            "output_scale": "one",
            "output_period": payload["period"],
            "rounding": dict(RATIO_ROUNDING),
        }
        base["content_hash"] = content_hash(base)
        return validate_numeric_verification_spec(base)


def _raw_sink(records: Sequence[Any]) -> tuple[str, str]:
    """The raw spool artifact a filing was parsed from, and its digest.

    ``raw-sink:<sha256>`` is self-describing: the ref names the hash of the
    bytes the parser read.  A filing whose records are not of that shape is
    refused rather than admitted with an invented artifact hash.
    """

    for item in records:
        text = str(item)
        if text.startswith("raw-sink:"):
            digest = text.split(":", 1)[1]
            if re.fullmatch(r"[0-9a-f]{64}", digest):
                return text, digest
    raise QuantitativeClaimPromotionError(
        "this filing names no raw spool artifact with a sha256 digest")


def build_statement_line_candidate(
    connection: sqlite3.Connection,
    *,
    ingest_id: str,
    origin_ref: str,
    actor_ref: str,
    resolver: SecStatementLineAuthorityResolver | None = None,
) -> dict[str, Any]:
    """Every record one filed number needs, derived from Core and nothing else.

    Called twice for every Claim that enters the Ledger: once by the promoter
    to stage the candidate, and once by the auto-commit rule to rebuild it and
    refuse anything that differs by a byte.
    """

    from .research_verification import (
        build_candidate_evidence,
        validate_candidate_claim,
        validate_candidate_evidence,
        verify_numeric_spec,
    )

    figures = resolver if resolver is not None else SecStatementLineAuthorityResolver(
        connection)
    if not isinstance(actor_ref, str) or not actor_ref.startswith("automation:"):
        raise QuantitativeClaimPromotionError(
            "a filed number is staged by mission automation, by name")
    proposal = figures.proposal(str(ingest_id), str(origin_ref))
    material = figures.build_material(proposal)
    source_verification = figures.verify_source_material(material)
    if source_verification["verdict"] != "pass":
        failed = [
            item["code"] for item in source_verification["findings"]
            if item["severity"] == "error" and item["status"] == "fail"
        ]
        raise QuantitativeClaimPromotionError(
            "statement line authority verification rejected: " + ", ".join(failed))

    spec: dict[str, Any] | None = None
    verified_line: dict[str, Any] | None = None
    if proposal["origin_kind"] == "statement_line":
        verified_line, numeric_bundle = figures.verify_statement_line(proposal["origin_ref"])
        numeric_spec_ref = verified_line["figure_id"]
        numeric_spec_hash = verified_line["content_hash"]
    else:
        spec = figures.numeric_spec(material)
        numeric_bundle = verify_numeric_spec(
            spec, checkpoint_ref=source_verification["checkpoint_ref"],
            checkpoint_hash=source_verification["checkpoint_hash"],
            source_material=material, source_bundle=source_verification,
        )
        if numeric_bundle["verdict"] != "pass":
            failed = [
                item["code"] for item in numeric_bundle["findings"]
                if item["severity"] == "error" and item["status"] == "fail"
            ]
            raise QuantitativeClaimPromotionError(
                "derived ratio numeric verification rejected: " + ", ".join(failed))
        numeric_spec_ref = spec["id"]
        numeric_spec_hash = spec["content_hash"]

    when = material["retrieved_at"]
    identity = content_hash({
        "origin_ref": proposal["origin_ref"],
        "filing_hash": material["source_content_hash"],
    })[:32]
    evidence = validate_candidate_evidence(build_candidate_evidence(
        material, source_verification,
        candidate_evidence_ref="candidate-evidence:sec-statement-line:" + identity,
        actor_ref=actor_ref, created_at=when,
        verification_mode=SEC_STATEMENT_LINE_AUTHORITY_MODE,
    ))
    claim_ref = "candidate-claim:sec-statement-line:" + identity
    claim = {
        "schema_version": "0.1",
        "id": "candidate-claim-version:" + content_hash(
            {"candidate_claim_ref": claim_ref, "version": 1}),
        "created_at": when,
        "candidate_claim_ref": claim_ref,
        "version": 1,
        "subject_ref": proposal["company_ref"],
        "metric_or_aspect": proposal["metric_or_aspect"],
        "period": proposal["period"],
        "basis": proposal["basis"],
        "normalized_statement": proposal["normalized_statement"],
        "semantic_verification_status": "unverified",
        "claim_kind": "quantitative",
        "value": proposal["value"],
        "unit": proposal["unit"],
        "currency": proposal["currency"],
        "scale": proposal["scale"],
        "candidate_evidence_refs": [
            {"ref": evidence["id"], "hash": evidence["content_hash"]}],
        "source_verification_ref": source_verification["id"],
        "source_verification_hash": source_verification["content_hash"],
        "numeric_spec_ref": numeric_spec_ref,
        "numeric_spec_hash": numeric_spec_hash,
        "numeric_verification_ref": numeric_bundle["id"],
        "numeric_verification_hash": numeric_bundle["content_hash"],
        "actor_ref": actor_ref,
        "prior_version_ref": None,
    }
    claim["content_hash"] = content_hash(claim)
    return {
        "proposal": proposal,
        "material": material,
        "source_verification": source_verification,
        "numeric_spec": spec,
        "numeric_verification": numeric_bundle,
        "verified_statement_line": verified_line,
        "evidence": evidence,
        "claim": validate_candidate_claim(claim),
        "resolver": figures,
    }


def stage_statement_line_candidate(
    connection: sqlite3.Connection,
    staging: Any,
    *,
    ingest_id: str,
    origin_ref: str,
    actor_ref: str,
    resolver: SecStatementLineAuthorityResolver | None = None,
) -> dict[str, Any]:
    """Stage one filed number as a quantitative candidate; idempotent by row."""

    bundle = build_statement_line_candidate(
        connection, ingest_id=ingest_id, origin_ref=origin_ref,
        actor_ref=actor_ref, resolver=resolver)
    staged = staging.stage(
        material=bundle["material"],
        source_verification=bundle["source_verification"],
        evidence=bundle["evidence"],
        claim=bundle["claim"],
        numeric_spec=bundle["numeric_spec"],
        numeric_verification=(
            None if bundle["numeric_spec"] is None else bundle["numeric_verification"]),
        idempotency_key="statement-line-promotion:" + str(origin_ref),
        verification_mode=SEC_STATEMENT_LINE_AUTHORITY_MODE,
        verified_statement_line=bundle["verified_statement_line"],
        statement_resolver=bundle["resolver"],
    )
    return {**bundle, "staging": staged, "write_status": staged["write_status"]}


class QuantitativeClaimPromotionLedger:
    """What has already been promoted, so nothing is promoted twice."""

    def __init__(self, connection: sqlite3.Connection, *, clock: Any = None) -> None:
        self.connection = connection
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._authorization = authorization_flag(
            connection, "dalton_quantitative_claim_promotion_authorized")
        self.connection.executescript(_SCHEMA.read_text(encoding="utf-8"))

    def held(self, promotion_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM quantitative_claim_promotions WHERE promotion_id=?",
            (str(promotion_id),),
        ).fetchone()
        return None if row is None else dict(row)

    def counts(self) -> dict[str, int]:
        rows = self.connection.execute(
            "SELECT disposition, COUNT(*) AS n FROM quantitative_claim_promotions "
            "GROUP BY disposition"
        ).fetchall()
        return {str(row["disposition"]): int(row["n"]) for row in rows}

    # A promotion only ever moves forward: a number that was blocked can become
    # staged and a staged one can be admitted, but nothing walks back.  Written
    # as a rank rather than a free update so a bug cannot quietly turn an
    # admitted Claim back into "waiting at a door".
    _RANK = {"blocked": 0, "staged": 1, "admitted": 2}

    def settle(
        self,
        promotion_id: str,
        *,
        disposition: str,
        candidate_claim_ref: str | None = None,
        claim_version_ref: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Advance one promotion to the door it has actually reached."""

        if disposition not in self._RANK:
            raise QuantitativeClaimPromotionError(
                "disposition must be staged, admitted or blocked")
        held = self.held(promotion_id)
        if held is None:
            raise QuantitativeClaimPromotionError(
                f"no promotion {promotion_id!r} to settle")
        if self._RANK[disposition] <= self._RANK[str(held["disposition"])]:
            return {"status": "unchanged", **held}
        body = {
            key: held[key] for key in (
                "schema_version", "promotion_id", "company_ref", "metric_or_aspect",
                "period", "origin_kind", "origin_ref", "origin_hash",
                "source_document_ref", "value", "unit", "currency", "scale",
                "normalized_statement",
            ) if key in held
        }
        body.setdefault("schema_version", SCHEMA_VERSION)
        body.update({
            "disposition": disposition,
            "candidate_claim_ref": candidate_claim_ref or held["candidate_claim_ref"],
            "claim_version_ref": claim_version_ref or held["claim_version_ref"],
            "reason": reason,
            "producer_ref": PRODUCER_REF,
        })
        digest = content_hash(body)
        self._authorization.authorized = True
        try:
            self.connection.execute(
                "UPDATE quantitative_claim_promotions SET disposition=?,"
                "candidate_claim_ref=?,claim_version_ref=?,reason=?,content_hash=? "
                "WHERE promotion_id=?",
                (disposition, body["candidate_claim_ref"], body["claim_version_ref"],
                 reason, digest, str(promotion_id)),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        finally:
            self._authorization.authorized = False
        return {"status": "settled", **(self.held(promotion_id) or {})}

    def record(
        self,
        proposal: Mapping[str, Any],
        *,
        disposition: str,
        candidate_claim_ref: str | None = None,
        claim_version_ref: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if disposition not in ("staged", "admitted", "blocked"):
            raise QuantitativeClaimPromotionError(
                "disposition must be staged, admitted or blocked")
        promotion_id = promotion_id_for(proposal)
        existing = self.held(promotion_id)
        if existing is not None:
            return {"status": "duplicate", **existing}
        body = {
            "schema_version": SCHEMA_VERSION,
            "promotion_id": promotion_id,
            "company_ref": proposal["company_ref"],
            "metric_or_aspect": proposal["metric_or_aspect"],
            "period": proposal["period"],
            "origin_kind": proposal["origin_kind"],
            "origin_ref": proposal["origin_ref"],
            "origin_hash": content_hash(proposal["anchor"]),
            "source_document_ref": proposal["source_document_ref"],
            "value": proposal["value"],
            "unit": proposal["unit"],
            "currency": proposal["currency"],
            "scale": proposal["scale"],
            "normalized_statement": proposal["normalized_statement"],
            "disposition": disposition,
            "candidate_claim_ref": candidate_claim_ref,
            "claim_version_ref": claim_version_ref,
            "reason": reason,
            "producer_ref": PRODUCER_REF,
        }
        wire = {**body, "content_hash": content_hash(body)}
        at = self.clock().astimezone(timezone.utc).isoformat(timespec="microseconds")
        self._authorization.authorized = True
        try:
            self.connection.execute(
                "INSERT INTO quantitative_claim_promotions("
                "promotion_id,company_ref,metric_or_aspect,period,origin_kind,origin_ref,"
                "origin_hash,source_document_ref,value,unit,currency,scale,"
                "normalized_statement,disposition,candidate_claim_ref,claim_version_ref,"
                "reason,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (promotion_id, wire["company_ref"], wire["metric_or_aspect"], wire["period"],
                 wire["origin_kind"], wire["origin_ref"], wire["origin_hash"],
                 wire["source_document_ref"], wire["value"], wire["unit"], wire["currency"],
                 wire["scale"], wire["normalized_statement"], disposition,
                 candidate_claim_ref, claim_version_ref, reason, wire["content_hash"], at),
            )
            self.connection.commit()
        except sqlite3.IntegrityError:
            self.connection.rollback()
            # The (company, metric, period, origin) uniqueness fired: something
            # else promoted this number between the read and the write.
            held = self.held(promotion_id)
            if held is not None:
                return {"status": "duplicate", **held}
            return {"status": "duplicate", **wire, "created_at": at}
        except Exception:
            self.connection.rollback()
            raise
        finally:
            self._authorization.authorized = False
        return {"status": "fresh", **wire, "created_at": at}


__all__ = [
    "CONCEPT_METRICS",
    "DERIVED_RATIOS",
    "FILED_BASIS",
    "PRODUCER_REF",
    "RATIO_DIGITS",
    "RATIO_OPERATOR",
    "RATIO_ROUNDING",
    "RATIO_UNIT",
    "SecStatementLineAuthorityResolver",
    "build_statement_line_candidate",
    "filing_proposals",
    "filing_rows",
    "line_projection",
    "stage_statement_line_candidate",
    "QuantitativeClaimPromotionError",
    "QuantitativeClaimPromotionLedger",
    "SCHEMA_VERSION",
    "canonical_decimal",
    "derived_ratio_proposals",
    "document_figure_proposal",
    "metric_for_line",
    "period_wire",
    "promotion_id_for",
    "statement_line_proposal",
    "statement_line_proposals",
    "unit_wire",
]

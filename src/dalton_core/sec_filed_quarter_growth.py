"""Quarterly growth from already held, strongly replayed filing rows.

A separate signed rule; no companyfacts retry, acquisition or model call.
Both quarters are disclosed by the current 10-Q under the same consolidated
revenue concept and currency. Held cumulative rows must reconcile to the
complete contiguous quarterly history; no residual is invented.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .quantitative_claim_promotion import (
    QuantitativeClaimPromotionError, canonical_decimal, filing_rows,
)
from .research_auto_commit import SEC_FILED_QUARTER_GROWTH_RULE_REF
from .sec_fy_minus_9m import (
    SecFyMinus9mAuthorityResolver, build_fy_minus_9m_candidate,
    _day, _is_quarter, _plain, _rounded, precision_unit, MAX_BOUND_PP, MAX_DIGITS,
)
from .store import content_hash

RULE_REF = SEC_FILED_QUARTER_GROWTH_RULE_REF
PAYLOAD_KIND = "filed_quarter_pair_growth"


def _refuse(message):
    raise QuantitativeClaimPromotionError("filed quarter growth refused: " + message)


class SecFiledQuarterGrowthAuthorityResolver(SecFyMinus9mAuthorityResolver):
    payload_kind = PAYLOAD_KIND
    rule_ref = RULE_REF
    identity_prefix = "sec-filed-quarter-growth"

    def __init__(self, connection, *, context_bindings=None):
        super().__init__(connection)
        self.context_bindings = None if context_bindings is None else dict(context_bindings)

    def _rows(self, filing):
        from .company_model_annual_projection import verify_statement_filing, AnnualProjectionError
        from .statement_context import statement_rows, context_record, StatementContextError
        try:
            authority = verify_statement_filing(self.connection, filing, allow_legacy_metadata=True)
            if self.context_bindings is None:
                rows = statement_rows(self.connection, filing["ingest_id"])
            else:
                if filing["ingest_id"] not in self.context_bindings:
                    _refuse("component has no frozen context binding")
                rows = statement_rows(self.connection, filing["ingest_id"],
                                      version_id=self.context_bindings[filing["ingest_id"]], latest=False)
            refs = {row.get("statement_context_ref") for row in rows}
            if len(refs) != 1:
                _refuse("filing rows have inconsistent context identity")
            ref = next(iter(refs), None)
            if ref is None and authority != "line_bound":
                _refuse("legacy metadata-only filing needs a strong original context")
            context = None if ref is None else context_record(self.connection, ref)
            return rows, ref, None if context is None else context["content_hash"]
        except (AnnualProjectionError, StatementContextError) as exc:
            _refuse(str(exc))

    def payload(self, ingest_id):
        current, _ = filing_rows(self.connection, ingest_id)
        if current["form"] != "10-Q":
            _refuse("requires a 10-Q, not a fiscal-year subtraction")
        end = _day(current["report_date"])
        if end is None:
            _refuse("filing has no report date")
        from .research_plan import DEFAULT_REVENUE_CONCEPT_CANDIDATES
        concepts = ["us-gaap:" + c if ":" not in c else c for c in DEFAULT_REVENUE_CONCEPT_CANDIDATES]
        own, own_context, own_hash = self._rows(current)

        def consolidated(row):
            if row.get("dimension_axis") is not None or row.get("dimension_member") is not None:
                return False
            # A first-axis projection alone does not prove a consolidated row.
            return row.get("dimensions") == {} and row.get("dimension_count") == 0

        eligible = [row for row in own if row["concept"] in concepts and consolidated(row)
                    and _day(row["period_end"]) == end and _day(row["period_start"])
                    and _is_quarter(_day(row["period_start"]), end)]
        identities = {(r["concept"], r["period_start"], r["period_end"]) for r in eligible}
        if len(identities) != 1:
            _refuse("current consolidated quarter concept/period is unavailable or ambiguous")
        concept, start_text, _ = next(iter(identities)); start = date.fromisoformat(start_text)
        prior = [r for r in own if r["concept"] == concept and consolidated(r)
                 and _day(r["period_start"]) and _day(r["period_end"])
                 and _is_quarter(_day(r["period_start"]), _day(r["period_end"]))
                 and 350 <= (start - _day(r["period_start"])).days <= 380
                 and 350 <= (end - _day(r["period_end"])).days <= 380]
        spans = {(r["period_start"], r["period_end"]) for r in prior}
        if len(spans) != 1:
            _refuse("same-accession comparable prior quarter is unavailable or ambiguous")
        prior_start, prior_end = map(date.fromisoformat, next(iter(spans)))
        all_filings = {current["ingest_id"]: current}
        all_rows = {current["ingest_id"]: (own, own_context, own_hash)}
        components = []

        def component(role, filing, rows, period_start, period_end):
            selected = [r for r in rows if r["concept"] == concept and consolidated(r)
                        and r["period_start"] == period_start.isoformat()
                        and r["period_end"] == period_end.isoformat()]
            if not selected:
                _refuse("missing same-concept quarter/cumulative row: " + role)
            # Only actual canonical currency or the established filed USD IDs;
            # unknown units and mixed identities cannot be omitted.
            from .sec_fy_minus_9m import is_usd
            if any(not is_usd(r["unit"]) for r in selected):
                _refuse("unknown or non-USD revenue unit: " + role)
            values = {canonical_decimal(r["value"]) for r in selected}
            if None in values or len(values) != 1:
                _refuse("conflicting or nonnumeric filed rows: " + role)
            row = min(selected, key=lambda r: int(r["ordinal"]))
            value = next(iter(values)); _, context_ref, context_hash = all_rows[filing["ingest_id"]]
            wire = {"role": role, "concept": concept, "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(), "value": value,
                    "precision_unit": _plain(precision_unit(value)), "line_id": row["line_id"],
                    "ingest_id": filing["ingest_id"], "accession": filing["accession"],
                    "form": filing["form"], "filed": filing["filed"],
                    "report_date": filing["report_date"], "filing_content_hash": filing["content_hash"],
                    "context_ref": context_ref, "context_hash": context_hash}
            components.append(wire)
            return wire

        c = component("current.quarter", current, own, start, end)
        p = component("prior.quarter", current, own, prior_start, prior_end)
        checks = []
        # Reconcile every held current-accession YTD window, including its
        # comparative: all component quarters are disclosed, never residuals.
        cumulatives = [r for r in own if r["concept"] == concept and consolidated(r)
                       and r["period_end"] in {end.isoformat(), prior_end.isoformat()}
                       and _day(r["period_start"]) and _day(r["period_end"])
                       and 100 < (_day(r["period_end"]) - _day(r["period_start"])).days < 300]
        for ci, cumulative in enumerate(cumulatives):
            a, b = _day(cumulative["period_start"]), _day(cumulative["period_end"])
            total = component(f"cumulative.{ci}", current, own, a, b)
            cursor = a; parts = []
            while cursor <= b:
                candidates = []
                for filing_row in self.connection.execute(
                        "SELECT * FROM coverage_mission_statement_filings WHERE company_ref=? AND form='10-Q' AND filed<=? ORDER BY filed DESC,ingest_id",
                        (current["company_ref"], current["filed"])).fetchall():
                    f = dict(filing_row); fid = f["ingest_id"]
                    if f["cik"] != current["cik"]:
                        _refuse("component filing issuer differs")
                    if fid not in all_rows:
                        all_rows[fid] = self._rows(f); all_filings[fid] = f
                    rs = all_rows[fid][0]
                    for r in rs:
                        e = _day(r["period_end"])
                        if r["concept"] == concept and consolidated(r) and r["period_start"] == cursor.isoformat() and e and e <= b and _is_quarter(cursor, e):
                            candidates.append((f, rs, e, r))
                ends = {x[2] for x in candidates}
                if len(ends) != 1:
                    _refuse("YTD has missing or ambiguous contiguous quarter")
                vals = {canonical_decimal(x[3]["value"]) for x in candidates}
                if None in vals or len(vals) != 1:
                    _refuse("restated quarter differs across held filings")
                f, rs, e, _ = candidates[0]
                parts.append(component(f"cumulative.{ci}.q{len(parts)+1}", f, rs, cursor, e))
                cursor = e + timedelta(days=1)
            delta = Decimal(total["value"]) - sum(Decimal(x["value"]) for x in parts)
            tolerance = (Decimal(total["precision_unit"]) + sum(Decimal(x["precision_unit"]) for x in parts)) / 2
            if abs(delta) > tolerance:
                _refuse("filed YTD is not the sum of its quarters")
            checks.append({"period": a.isoformat()+".."+b.isoformat(), "difference": _plain(delta), "tolerance": _plain(tolerance)})
        cv, pv = Decimal(c["value"]), Decimal(p["value"])
        ce, pe = Decimal(c["precision_unit"])/2, Decimal(p["precision_unit"])/2
        if cv < 0 or pv - pe <= 0:
            _refuse("revenue/current or prior denominator is invalid")
        growth = (cv/pv-1)*100; low=((cv-ce)/(pv+pe)-1)*100; high=((cv+ce)/(pv-pe)-1)*100
        bound=max(growth-low,high-growth)
        digits=next((d for d in range(MAX_DIGITS,-1,-1) if bound <= Decimal('0.5')*Decimal(1).scaleb(-d)),None)
        if digits is None or bound>MAX_BOUND_PP:
            _refuse("filed precision is too coarse")
        period=start.isoformat()+".."+end.isoformat(); value=_rounded(growth,digits)
        bindings={fid:all_rows[fid][1] for fid in sorted(all_rows)}
        origin="sec-filed-quarter-growth:"+content_hash({"rule":RULE_REF,"components":components})[:32]
        statement=(f"{current['entity_name']} revenue {period}: USD {c['value']} compared with USD {p['value']} "
                   f"({prior_start}..{prior_end}), year-over-year growth {value}%, computed from filed rows "
                   f"under {concept}; rule {RULE_REF}. The growth is derived, not itself a filed amount.")
        return {"kind":PAYLOAD_KIND,"company_ref":current['company_ref'],"cik":current['cik'],
                "entity_name":current['entity_name'],"accession":current['accession'],"form":current['form'],
                "filed":current['filed'],"report_date":current['report_date'],"ingest_id":ingest_id,
                "dispatch_id":current['dispatch_id'],"governance_ref":current['governance_ref'],
                "governance_hash":current['governance_hash'],"filing_content_hash":current['content_hash'],
                "lines":components,"components":components,"context_bindings":bindings,
                "metric_or_aspect":"quarterly_revenue_yoy_growth","period":period,
                "basis":"official-filing-xbrl-derived","value":value,"unit":"percent","currency":None,
                "scale":"one","normalized_statement":statement,
                "derivation":{"operator":"filed_quarter_pair","rule_ref":RULE_REF,"origin_ref":origin,
                              "concept":concept,"current":{"quarter":c},"prior":{"quarter":p},
                              "precision":{"digits":digits,"growth_bound_pp":_rounded(bound,4,ROUND_CEILING),
                                           "interval_pp":[_rounded(low,2,ROUND_FLOOR),_rounded(high,2,ROUND_CEILING)]},
                              "checks":checks}}

    def numeric_spec(self, material):
        from .research_verification import validate_numeric_verification_spec
        payload=material['normalized_payload']; d=payload['derivation']
        inputs=[{"name":name,"value":d[year]['quarter']['value'],"unit":"number","currency":None,
                 "scale":"one","period":d[year]['quarter']['period_start']+'..'+d[year]['quarter']['period_end'],
                 "source_material_ref":material['id'],"source_material_hash":material['content_hash'],
                 "json_pointer":f'/derivation/{year}/quarter/value',"extractor":"number"}
                for name,year in [('current_quarter','current'),('prior_quarter','prior')]]
        wire={"schema_version":"0.1","id":"numeric-spec:sec-filed-quarter-growth:"+content_hash(material),
              "created_at":material['created_at'],"operator":"growth_percentage","inputs":inputs,
              "output_value":payload['value'],"output_unit":"percent","output_currency":None,
              "output_scale":"one","output_period":payload['period'],
              "rounding":{"mode":"half_up","digits":d['precision']['digits']}}
        wire['content_hash']=content_hash(wire)
        return validate_numeric_verification_spec(wire)


def build_filed_quarter_growth_candidate(connection, *, ingest_id, actor_ref, context_bindings=None):
    return build_fy_minus_9m_candidate(connection,ingest_id=ingest_id,actor_ref=actor_ref,
        resolver=SecFiledQuarterGrowthAuthorityResolver(connection,context_bindings=context_bindings))


def stage_filed_quarter_growth_candidate(connection, staging, *, ingest_id, actor_ref):
    bundle=build_filed_quarter_growth_candidate(connection,ingest_id=ingest_id,actor_ref=actor_ref)
    staged=staging.stage(**{k:bundle[k] for k in ['material','source_verification','evidence','claim','numeric_spec','numeric_verification']},
        idempotency_key='filed-quarter-growth:'+bundle['material']['content_hash'],
        verification_mode=bundle['resolver'].provenance_mode,statement_resolver=bundle['resolver'])
    return {**bundle,'staging':staged,'write_status':staged['write_status']}

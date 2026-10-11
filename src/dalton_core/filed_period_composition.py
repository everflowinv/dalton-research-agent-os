"""Exact filed-parent history, separate from an explicitly ordinary forecast.

No missing series is zero-filled.  A period uses all children in one held
statement context, or the difference of two complete same-year contexts.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from itertools import product
from typing import Any, Mapping

from .store import content_hash

CONTRACT_REF = "filed-period-income-composition:0.1"
MAX_SOURCES = 32
MAX_PERIODS = 24
MAX_GROUP_CHILDREN = 16

class FiledCompositionError(ValueError):
    pass

def _decimal(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise FiledCompositionError("filed composition amount must be finite")
    return result

def _base(spec: Mapping[str, Any], inputs: Mapping[str, Any]):
    structure = spec.get("financial_statement_structure") or {}
    if structure.get("schema_version") != "0.6":
        return None
    lines = {x["ref"]: x for x in structure.get("lines") or []}
    filed = {x["concept"]: x for x in inputs.get("filed_lines") or []}
    candidates = []
    for formula in structure.get("formulas") or []:
        out = lines.get(formula.get("output_ref"), {})
        if formula.get("operator") != "sum" or out.get("role") != "operating_income":
            continue
        target = filed.get(formula.get("tie_out_concept"))
        terms = formula.get("terms") or []
        if not target or target.get("statement") != "income" or not terms:
            continue
        coefficients = {}
        for term in terms:
            line = lines.get(term.get("line_ref"), {})
            concept = line.get("concept")
            if (line.get("kind") != "filed" or line.get("statement") != "income"
                    or concept not in filed or concept in coefficients
                    or str(term.get("coefficient")) not in ("1", "-1")):
                break
            coefficients[concept] = str(term["coefficient"])
        else:
            from .company_financial_statement_structure import _period_cells
            amounts = [_period_cells(filed[c]) for c in coefficients]
            targets = _period_cells(target)
            periods = set(targets)
            for values in amounts:
                periods &= set(values)
            # Do not shrink the exact original replay window.
            required = {(v.get("period_start"), end) for end,v in target.get("cells",{}).items()}
            if (not required or len(required)>MAX_PERIODS or periods != required):
                continue
            mismatches = [p for p in periods if sum(
                (_decimal(k)*a[p] for k,a in zip(coefficients.values(),amounts)), Decimal(0)) != targets[p]]
            if mismatches:
                candidates.append((formula, coefficients, target, periods))
    return candidates[0] if len(candidates)==1 else None

def derive(spec: Mapping[str, Any], inputs: Mapping[str, Any], sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    """All-period unique composition, using only already-held exact source rows."""
    base = _base(spec, inputs)
    if base is None:
        return None
    formula, coefficients, target, required = base
    sources = sorted(sources, key=lambda s: (str(s.get("accession")), str(s.get("ingest_id")), str(s.get("rows_hash"))))
    from .forecast_coverage import ONE_OFF_CONCEPTS
    parent = formula["tie_out_concept"]
    groups = []
    extras = set()
    evidence = (target.get("source_evidence") or {}).get("rows") or []
    bindings = {(r.get("ingest_id"), r.get("statement_context_ref"), r.get("accession")) for r in evidence}
    if not sources or len(sources)>MAX_SOURCES:
        return None
    for source in sources:
        if source.get("company_ref") != inputs.get("company_ref"):
            raise FiledCompositionError("composition source company differs")
        raw = source.get("rows") or []
        if source.get("rows_hash") != content_hash(raw):
            raise FiledCompositionError("composition source rows hash differs")
        if not raw or any((r.get("ingest_id"), r.get("statement_context_ref"), source.get("accession")) not in bindings
                          or r.get("ingest_id") != source.get("ingest_id") for r in raw):
            raise FiledCompositionError("composition source differs from consumed exact context")
        windows = sorted({(r.get("period_start"),r.get("period_end")) for r in raw if r.get("concept")==parent})
        for start,end in windows:
            rows = [r for r in raw if (r.get("period_start"),r.get("period_end")) == (start,end)]
            parents = [r for r in rows if r.get("concept")==parent]
            children = [r for r in rows if r.get("parent_concept")==parent]
            if len(parents)!=1 or not 1<=len(children)<=MAX_GROUP_CHILDREN:
                continue
            identity = {(r.get("ingest_id"),r.get("statement_context_ref")) for r in rows}
            units = {str(r.get("unit") or "").casefold() for r in rows}
            if (len(identity)!=1 or len(units)!=1 or None in next(iter(identity))
                    or any(r.get("statement")!="income" or r.get("dimensions")
                           or r.get("dimension_axis") or r.get("is_breakdown") for r in rows)):
                continue
            by_concept = {r["concept"]: r for r in children}
            if len(by_concept)!=len(children) or not set(coefficients)<=set(by_concept):
                continue
            additional=set(by_concept)-set(coefficients)
            if not additional<=ONE_OFF_CONCEPTS:
                continue
            extras |= additional
            groups.append({"source":source,"start":start,"end":end,"parent":parents[0],
                           "children":by_concept,"unit":next(iter(units))})
    if not extras or len(extras)>2:
        return None
    signs=[]
    for candidate in product(("1","-1"), repeat=len(extras)):
        extra_coeff=dict(zip(sorted(extras),candidate));all_coeff={**coefficients,**extra_coeff}
        if groups and all(sum((_decimal(all_coeff[c])*_decimal(r["value"])
                for c,r in g["children"].items()),Decimal(0)) == _decimal(g["parent"]["value"])
                for g in groups):
            signs.append(extra_coeff)
    if len(signs)!=1:
        return None
    all_coeff={**coefficients,**signs[0]}
    period_proofs=[]
    for start,end in sorted(required,key=lambda p:(p[1],p[0] or "")):
        cell=target["cells"][end];accessions=set(cell.get("source_accessions") or [])
        available=[g for g in groups if g["source"]["accession"] in accessions]
        direct=[([g],["1"]) for g in available if (g["start"],g["end"])==(start,end)]
        candidates=direct
        if cell.get("basis")=="derived_from_cumulative" and start:
            prior=(date.fromisoformat(start)-timedelta(days=1)).isoformat()
            candidates=[([a,b],["1","-1"]) for a in available for b in available
                        if a["end"]==end and b["end"]==prior and a["start"]==b["start"]
                        and a["start"] and a["start"]<start and a["start"][:4]==start[:4]]
        accepted=[]
        for gs, weights in candidates:
            unit=str(cell.get("unit") or "").casefold()
            if any(g["unit"] != unit for g in gs):
                continue
            # All ordinary terms must reproduce the exact consumed quarter.
            from .company_financial_statement_structure import _period_cells
            filed={x["concept"]:x for x in inputs["filed_lines"]}
            if any(sum((_decimal(w)*_decimal(g["children"][c]["value"]) for g,w in zip(gs,weights)),Decimal(0))
                    != _period_cells(filed[c]).get((start,end)) for c in coefficients):
                continue
            total=sum((_decimal(w)*_decimal(g["parent"]["value"]) for g,w in zip(gs,weights)),Decimal(0))
            if total != _decimal(cell["value"]):
                continue
            wires=[{"accession":g["source"]["accession"],"ingest_id":g["source"]["ingest_id"],
                    "filing_hash":g["source"]["filing_hash"],"context_ref":g["parent"]["statement_context_ref"],
                    "period_start":g["start"],"period_end":g["end"],"weight":w,"unit":g["unit"],
                    "parent":g["parent"],"children":list(g["children"].values())} for g,w in zip(gs,weights)]
            accepted.append({"period_start":start,"period_end":end,"value":str(total),"unit":unit,"groups":wires})
        if not accepted:
            return None
        # Restated or ambiguous groups never get selected by amount preference.
        signatures={content_hash([{c:r["value"] for c,r in g["children"].items()} for g in gs])
                    for gs,ws in candidates if all(g["unit"]==str(cell.get("unit") or "").casefold() for g in gs)}
        if len(signatures)>1:
            return None
        accepted.sort(key=content_hash)
        period_proofs.append(accepted[0])
    result={"contract_ref":CONTRACT_REF,"company_ref":inputs["company_ref"],
            "spec_ref":inputs.get("spec_ref"),"output_ref":formula["output_ref"],"tie_out_concept":parent,
            "formula_hash":content_hash(formula),"ordinary_coefficients":coefficients,
            "one_off_coefficients":signs[0],"periods":period_proofs,
            "future_assumption":{"method":"zero_one_off","concepts":sorted(extras),
                "reason":"未假设未来继续发生这些已披露的一次性损失或收益；新证据需要重新评估。",
                "source":"system_modeling_assumption_not_company_guidance"},
            "sources":sources}
    return {**result,"content_hash":content_hash(result)}

def collect(missions: Any, spec: Mapping[str,Any], inputs: Mapping[str,Any]) -> dict[str,Any] | None:
    base=_base(spec,inputs)
    if base is None or not hasattr(missions,"connection"):
        return None
    _,_,target,_=base; sources=[]
    evidence=(target.get("source_evidence") or {}).get("rows") or []
    bindings={(r["ingest_id"],r["statement_context_ref"],r["accession"]) for r in evidence
              if r.get("ingest_id") and r.get("statement_context_ref") and r.get("accession")}
    used={a for c in target["cells"].values() for a in c.get("source_accessions") or []}
    bindings=sorted(x for x in bindings if x[2] in used)
    if not 1<=len(bindings)<=MAX_SOURCES:
        return None
    from .statement_context import statement_rows
    from .company_model_annual_projection import verify_statement_filing
    for ingest,context,accession in bindings:
        row=missions.connection.execute("SELECT * FROM coverage_mission_statement_filings WHERE ingest_id=? AND company_ref=? AND accession=?",(ingest,inputs["company_ref"],accession)).fetchone()
        if row is None:
            raise FiledCompositionError("composition filing authority unavailable")
        filing=dict(row);verify_statement_filing(missions.connection,filing,allow_legacy_metadata=True)
        rows=[r for r in statement_rows(missions.connection,ingest,version_id=context,latest=False)
              if r.get("statement")=="income" and not r.get("dimensions") and not r.get("dimension_axis")
              and (r.get("concept")==base[0]["tie_out_concept"] or r.get("parent_concept")==base[0]["tie_out_concept"])]
        sources.append({"company_ref":inputs["company_ref"],"accession":accession,"ingest_id":ingest,
                        "filing_hash":filing["content_hash"],"rows_hash":content_hash(rows),"rows":rows})
    return derive(spec,inputs,sources)

def verify(proof: Mapping[str,Any], spec: Mapping[str,Any], inputs: Mapping[str,Any]) -> dict[str,Any]:
    body={k:v for k,v in proof.items() if k!="content_hash"}
    if proof.get("contract_ref")!=CONTRACT_REF or proof.get("content_hash")!=content_hash(body):
        raise FiledCompositionError("filed composition proof hash/contract differs")
    recomputed=derive(spec,inputs,list(proof.get("sources") or []))
    if recomputed!=proof:
        raise FiledCompositionError("filed composition proof does not replay")
    return dict(proof)

"""Source acquisition requests for financial model gaps, independent of issuer.

Finding material does not admit it as a numerical input. The model's exact
identity, filed arithmetic and assumption-source gates still decide admission.
"""
from __future__ import annotations
from typing import Any, Mapping

RULE_REF="rule:forecast-material-gaps:1"
ROLE_NEEDS={
    "income_tax_expense":("normalised_tax_basis","effective income tax rate discrete tax items guidance"),
    "diluted_weighted_average_shares":("diluted_share_basis","quarter diluted weighted average shares buyback share count guidance"),
    "interest_expense":("debt_rate_basis","interest expense current noncurrent debt quarterly balance"),
    "depreciation_amortization":("asset_roll_basis","depreciation amortization property plant equipment capital expenditures"),
    "operating_cash_flow":("cash_flow_basis","operating cash flow net income depreciation working capital reconciliation"),
}

def gap_plans(spec: Mapping[str,Any], table: Mapping[str,Any], *, record=None, failure=None):
    """Stable, source-directed needs derived from contracts, never ticker cases."""
    plans={}
    latest=max([str(end) for end in table.get("periods") or [] if end]+[str((record or {}).get("forecast_policy",{}).get("latest_actual_end") or "")],default="")
    def add(key,item,question,reason):
        plans.setdefault(key,{"key":key,"period":latest or None,
            "need":{"kind":"data","doc_kinds":["sec_10k","sec_8k","transcript"],"data_item":item,"question":question},
            "source_priority":["indexed_corpus","sec_edgar","alphaengine","web"],
            "acceptance":{"min_passages":1,"about_company":True,"model_gap_rule_ref":RULE_REF,
                "model_input_admission":"Exact period/statement/full dimensions, filed reconciliation and source admission must pass before model use."},
            "reason":reason})
    drivers=(record or {}).get("execution_contract",{}).get("drivers",{})
    if (drivers.get("status")=="unexecuted" or
        any(d.get("kind")=="segment" and not d.get("basis_identity") for d in spec.get("revenue_drivers") or [])):
        add("economic_driver_history","segment revenue classification comparable quarterly history quantity price bookings",
            "Find independently disclosed revenue drivers: full segment identity and at least four comparable fiscal quarters, or quantity×price / book-volume×yield. Obtain an explicit company source for taxonomy changes and the complete consolidation bridge; no residual or label-based allocation.",
            "Revenue mechanism lacks independently admitted driver history or a source-backed classification bridge.")
    filed={l.get("concept"):l for l in table.get("filed_lines") or []}
    for line in ((spec.get("financial_statement_structure") or {}).get("lines") or []) if "filed_lines" in table else []:
        source=filed.get(line.get("concept"))
        if line.get("kind")=="filed" and (not source or source.get("status")!="filed" or source.get("statement")!=line.get("statement")):
            concept=str(line.get("concept") or "")
            add("statement_identity:"+concept,concept+" "+str(line.get("label") or "")+" "+str(line.get("statement"))+" statement",
                "Find this exact filed concept on the declared statement with consolidated full-context identity, original accession and quarter dates. A cash-flow add-back cannot replace an income expense; no subtotal residual is admitted.",
                "A declared filed statement line lacks its exact statement authority.")
    for result in (record or {}).get("results") or []:
        if result.get("status")!="unavailable":continue
        role=result.get("role")
        if role in ROLE_NEEDS:
            key,item=ROLE_NEEDS[role]
            add(key,item,"Find source-backed assumptions and exact filed bases that resolve this financial gap: "+str(result.get("reason") or "missing inputs")+". Keep original concept, unit, dates, accession and provenance; do not substitute a proxy.",str(result.get("reason") or "missing inputs"))
        if str(result.get("ref")) in {"result:company_free_cash_flow","result:company_fcf"} or "已披露FCF" in str(result.get("reason")):
            add("company_fcf_definition","free cash flow reconciliation capital expenditure finance lease software contract costs",
                "Obtain this company's published FCF reconciliation, including its treatment of finance leases, capitalized software and contract costs. Bind the definition and each adjustment to dated original passages and filed concepts.",str(result.get("reason")))
    if failure and not plans:
        add("financial_authority","financial statement model driver reconciliation",
            "Resolve this model publication refusal with original filed or documentary authority: "+str(failure)+". No fabricated actual, arbitrary rate or unproved earnings proxy is admissible.",str(failure))
    return list(plans.values())

def open_gap_requests(state_dir, spec, table, *, record=None, failure=None, run_ref=None):
    from .material_requests import open_request
    output=[]
    for plan in gap_plans(spec,table,record=record,failure=failure):
        value=open_request(state_dir,requester={"lane":"company_model_forecast","product_ref":spec.get("spec_id"),
            "run_ref":run_ref,"section":plan["key"]},subject=spec["company_ref"],need=plan["need"],period=plan["period"],
            source_priority=plan["source_priority"],acceptance=plan["acceptance"])
        output.append({"id":value["id"],"status":value["status"],"write_status":value["write_status"],"gap_key":plan["key"],"reason":plan["reason"]})
    return output

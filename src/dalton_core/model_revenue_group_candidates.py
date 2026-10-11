"""Bounded, source-stable disclosure choices; never an inferred remainder."""
from __future__ import annotations
from collections import defaultdict
from decimal import Decimal
from typing import Any, Mapping
from .company_model_identity_catalog import cell_identity_ref
from .store import content_hash

CONTRACT_REF = "model-revenue-source-group-candidates:0.1"
MAX_GROUPS = 4

def candidates(cells: list[Mapping[str,Any]]) -> dict[str,Any]:
    from .model_forecast_driver import CONCEPT_ROLES, REVENUE
    from .company_financial_statement_structure import presentation_rounding_ties
    buckets=defaultdict(list)
    for cell in cells:
        if (cell.get("statement") != "income" or cell.get("status") != "filed"
                or CONCEPT_ROLES.get(str(cell.get("concept"))) != REVENUE):
            continue
        source=tuple(str(cell.get(k) or "") for k in ("filing_content_hash","accession","statement_context_ref","concept","period_start","period_end","unit"))
        if not all(source[:4]) or not source[5] or not source[6]:
            continue
        buckets[source].append(cell)
    groups=[]
    for source,rows in buckets.items():
        by_dimensions=defaultdict(list)
        for row in rows:
            dimensions=row.get("dimensions")
            if isinstance(dimensions,Mapping):
                by_dimensions[tuple(sorted(dimensions.items()))].append(row)
        equivalents = {}
        for dimensions, held in list(by_dimensions.items()):
            if len({str(c.get("value")) for c in held}) != 1:
                by_dimensions.pop(dimensions)
                continue
            held.sort(key=lambda c:str(c.get("line_id") or ""))
            equivalents[cell_identity_ref(held[0])] = sorted({cell_identity_ref(c) for c in held})
            by_dimensions[dimensions] = [held[0]]
        partitions=defaultdict(dict)
        for dimensions,held in by_dimensions.items():
            for axis,member in dimensions:
                fixed=tuple((a,m) for a,m in dimensions if a!=axis)
                partitions[(axis,fixed)][member]=held[0]
        for (axis,fixed),members in partitions.items():
            if len(members)<2:
                continue
            totals=by_dimensions.get(fixed,[])
            company_totals=by_dimensions.get((),[])
            # No total is synthesised from an arithmetic residual.
            if len(totals)!=1 or len(company_totals)!=1:
                continue
            ordered=[members[k]for k in sorted(members)]
            try:
                values=[Decimal(str(c["value"]))for c in ordered]
                total=Decimal(str(totals[0]["value"]));company=Decimal(str(company_totals[0]["value"]))
            except (ValueError,ArithmeticError):
                continue
            if not all(v.is_finite()for v in [*values,total,company]):
                continue
            summed=sum(values,Decimal(0))
            def mode(calculated,reported,leaves):
                return "exact" if calculated==reported else "presentation_rounding" if presentation_rounding_ties(calculated,reported,leaves) else "not_closed"
            record={"contract_ref":CONTRACT_REF,"concept":source[3],"axis":axis,"fixed_dimensions":dict(fixed),
                "accession":source[1],"context_ref":source[2],"filing_hash":source[0],
                "period_start":source[4],"period_end":source[5],"unit":source[6],
                "members":[{"member":c["dimensions"][axis],"identity_ref":cell_identity_ref(c),
                             **({"equivalent_refs":equivalents[cell_identity_ref(c)]} if len(equivalents[cell_identity_ref(c)])>1 else {})}for c in ordered],
                "reported_group_total_ref":cell_identity_ref(totals[0]),
                "reported_company_total_ref":cell_identity_ref(company_totals[0]),
                "member_sum":str(summed),"reported_group_total":str(total),"reported_company_total":str(company),
                "members_to_group":mode(summed,total,values),
                "group_to_company":mode(total,company,[total]),
                "missing_reconciliation":"需要原文说明该披露组与集团收入的差额；不得创造余额项目或据此自动选择其他分组。" if mode(total,company,[total])=="not_closed" else None}
            record["group_ref"]="model-revenue-source-group:"+content_hash(record)[:32]
            groups.append(record)
    groups.sort(key=lambda g:(g["period_end"],g["accession"],g["context_ref"],g["concept"],g["axis"],tuple(sorted(g["fixed_dimensions"].items()))),reverse=True)
    # Every family at the newest exact period is considered before older periods.
    latest=[];seen=set()
    for group in groups:
        family=(group["concept"],group["axis"],tuple(sorted(group["fixed_dimensions"].items())))
        if family not in seen:
            latest.append(group);seen.add(family)
    omitted=latest[MAX_GROUPS:]
    return {"contract_ref":CONTRACT_REF,"groups":latest[:MAX_GROUPS],
            "omitted_group_count":len(omitted),"omitted_groups_hash":content_hash(omitted),
            "scope":"source_choices_only_not_verified_forecast_or_automatic_group_selection"}

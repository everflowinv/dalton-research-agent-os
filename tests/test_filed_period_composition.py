from __future__ import annotations

import copy
from decimal import Decimal
import unittest
from unittest.mock import patch

from dalton_core.filed_period_composition import derive, verify, FiledCompositionError
from dalton_core.company_financial_statement_structure import (
    materialize_financial_statement_structure, FinancialStatementStructureError,
    financial_input_authority,
)
from dalton_core.store import content_hash

LOSS = "us-gaap:GainLossOnSaleOfBusiness"

def fixture():
    from tests import test_company_financial_statement_structure as helpers
    inputs = helpers.financial_inputs()
    spec = helpers.company_spec()
    spec["decided_by"]="automation:test"
    p = helpers.proposal(inputs)
    structure = {k:p[k] for k in ("lines","formulas")}
    structure["schema_version"]="0.6"
    for line in structure["lines"]:
        line["annual_forecast_method"] = "unavailable" if line["role"]=="diluted_weighted_average_shares" else None
    numerator=next(l for l in structure["lines"] if l["ref"]=="eps-numerator")
    numerator.update(kind="derived",concept=None,forecast_method="formula")
    structure["formulas"].append(helpers.sum_formula("eps-numerator",(("parent",1),),"eps_numerator"))
    spec["financial_statement_structure"]=structure
    for line in inputs["filed_lines"]:
        original=list(line["cells"].items());line["cells"]={}
        for year in (2023,2024,2025):
            for end,cell in original:
                line["cells"][str(year)+end[4:]]={**cell,"period_start":str(year)+cell["period_start"][4:]}
    inputs["periods"]=sorted(inputs["filed_lines"][0]["cells"])
    affected="2023-09-30"
    for line in inputs["filed_lines"]:
        if line["concept"] in ("operating","pretax","net","parent","eps_numerator"):
            line["cells"][affected]["value"]=str(Decimal(line["cells"][affected]["value"])-10)
        if line["concept"]=="eps":
            line["cells"][affected]["value"]=str(Decimal(line["cells"][affected]["value"])-Decimal("0.1"))
    target=next(l for l in inputs["filed_lines"] if l["concept"]=="operating")
    by_concept={l["concept"]:l for l in inputs["filed_lines"]}
    rows=[]
    for ordinal,(end,cell) in enumerate(target["cells"].items()):
        for concept in ("operating","revenue","cost","opex"):
            rows.append({"concept":concept,"value":by_concept[concept]["cells"][end]["value"],
                "period_start":cell["period_start"],"period_end":end,"parent_concept":None if concept=="operating" else "operating",
                "statement":"income","dimensions":{},"unit":"usd","ingest_id":"ingest:one",
                "statement_context_ref":"context:one","line_id":f"line:{ordinal}:{concept}","accession":helpers.ACCESSION})
        if end==affected:
            rows.append({**rows[-1],"concept":LOSS,"value":"-10","line_id":"line:loss"})
    target["source_evidence"]={"rows":[r for r in rows if r["concept"]=="operating"]}
    sources=[{"company_ref":inputs["company_ref"],"accession":helpers.ACCESSION,"ingest_id":"ingest:one", "filing_hash":"a"*64,"rows":rows,"rows_hash":content_hash(rows)}]
    return spec,inputs,sources

class FiledPeriodCompositionTests(unittest.TestCase):
    def attach(self,spec,inputs,sources):
        proof=derive(spec,inputs,sources)
        self.assertIsNotNone(proof)
        inputs["historical_income_composition"]=proof
        return proof

    def test_full_twelve_period_income_dag_and_eps_uses_original_compositions(self):
        spec,inputs,sources=fixture();original=copy.deepcopy(inputs)
        with self.assertRaisesRegex(FinancialStatementStructureError,"operating.*tie"):
            materialize_financial_statement_structure(spec,inputs,complete=False)
        proof=self.attach(spec,inputs,sources)
        structure,replay=materialize_financial_statement_structure(spec,inputs,complete=False)
        self.assertEqual({x["output_ref"]:len(x["tested_periods"]) for x in replay["formulas"]},
            {"operating":12,"pretax":12,"net":12,"parent":12,"eps-numerator":12,"eps":12})
        self.assertEqual(inputs["filed_lines"],original["filed_lines"])
        self.assertEqual(structure["formulas"],spec["financial_statement_structure"]["formulas"])
        self.assertEqual(proof["one_off_coefficients"],{LOSS:"1"})
        self.assertEqual(proof["future_assumption"]["source"],"system_modeling_assumption_not_company_guidance")
        self.assertIn("未假设未来",structure["auto_repairs"][-1]["detail"])

    def test_missing_parent_child_currency_source_and_unrecognized_extra_are_not_zero_filled(self):
        for mutate in (lambda r:r.pop(),lambda r:r[0].update(unit="eur"),
                       lambda r:r[0].update(ingest_id="foreign"),
                       lambda r:r[-1].update(concept="issuer:unidentified_charge")):
            spec,inputs,sources=fixture();mutate(sources[0]["rows"])
            sources[0]["rows_hash"]=content_hash(sources[0]["rows"])
            try:proof=derive(spec,inputs,sources)
            except FiledCompositionError:proof=None
            self.assertIsNone(proof)

    def test_changed_ordinary_term_or_unresolved_second_period_refuses_all_history(self):
        spec,inputs,sources=fixture()
        row=next(r for r in sources[0]["rows"] if r["concept"]=="cost" and r["period_end"]=="2025-03-31")
        row["value"]=str(Decimal(row["value"])+17);sources[0]["rows_hash"]=content_hash(sources[0]["rows"])
        self.assertIsNone(derive(spec,inputs,sources))

    def test_proof_hash_foreign_company_and_period_drop_rejected(self):
        spec,inputs,sources=fixture();proof=self.attach(spec,inputs,sources)
        for mutation in (lambda p:p.update(company_ref="company:foreign"),lambda p:p["periods"].pop()):
            bad=copy.deepcopy(proof);mutation(bad);bad["content_hash"]=content_hash({k:v for k,v in bad.items() if k!="content_hash"})
            with self.assertRaises(FiledCompositionError):verify(bad,spec,inputs)
        bad=copy.deepcopy(proof);bad["sources"][0]["rows"][0]["value"]="123"
        with self.assertRaises(FiledCompositionError):verify(bad,spec,inputs)

    def test_zero_extra_without_unique_sign_is_not_a_signed_economic_fact(self):
        spec,inputs,sources=fixture()
        for r in sources[0]["rows"]:
            if r["concept"]==LOSS:r["value"]="0"
        sources[0]["rows_hash"]=content_hash(sources[0]["rows"])
        self.assertIsNone(derive(spec,inputs,sources))

    def test_no_eligible_history_keeps_financial_authority_legacy_bytes(self):
        spec,inputs,sources=fixture();inputs["filed_lines"]=[l for l in inputs["filed_lines"] if l["concept"]!="operating"]
        self.assertIsNone(derive(spec,inputs,sources))
        authority=financial_input_authority(inputs)
        self.assertNotIn("historical_income_composition",authority)
        self.assertEqual(authority["schema_version"],"financial-input-authority-0.1")

    def test_q4_requires_complete_same_year_annual_minus_nine_month_groups(self):
        spec,inputs,sources=fixture();by={l["concept"]:l for l in inputs["filed_lines"]};end="2023-12-31";target=by["operating"];target["cells"][end]["basis"]="derived_from_cumulative"
        rows=sources[0]["rows"];q4=[r for r in rows if r["period_end"]==end];rows[:]=[r for r in rows if r["period_end"]!=end]
        for upto in ("2023-09-30",end):
            for template in q4:
                c=template["concept"];value=sum((Decimal(v["value"]) for e,v in by[c]["cells"].items() if e.startswith("2023") and e<=upto),Decimal(0))
                rows.append({**template,"period_start":"2023-01-01","period_end":upto,"value":str(value),"line_id":template["line_id"]+":"+upto})
            rows.append({**rows[-1],"concept":LOSS,"value":"-10","line_id":"annual-loss:"+upto})
        target["source_evidence"]["rows"]=[r for r in rows if r["concept"]=="operating"]
        sources[0]["rows_hash"]=content_hash(rows)
        proof=self.attach(spec,inputs,sources)
        q=next(p for p in proof["periods"] if p["period_end"]==end)
        self.assertEqual([g["weight"] for g in q["groups"]],["1","-1"])
        self.assertEqual(len(proof["periods"]),12)
        # The disclosed annual and 9M loss cancel; no missing loss cell is invented.
        self.assertEqual([next(r["value"] for r in g["children"] if r["concept"]==LOSS) for g in q["groups"]],["-10","-10"])
        sources[0]["rows"]=[r for r in rows if not(r["period_start"]=="2023-01-01" and r["period_end"]=="2023-09-30")]
        sources[0]["rows_hash"]=content_hash(sources[0]["rows"])
        self.assertIsNone(derive(spec,inputs,sources))


    def test_input_builder_preserves_original_authority_behind_request_scoped_read_cache(self):
        from tests import test_company_model_inputs as helpers
        from dalton_core.company_model_inputs import build_model_inputs
        original = helpers.ModelInputTests().ledger()
        adapter = helpers.ModelInputTests().ledger()
        with patch("dalton_core.company_model_inputs._input_statement_reader", return_value=adapter), patch(
                "dalton_core.filed_period_composition.collect", return_value=None) as collect:
            build_model_inputs(original, helpers._spec())
        self.assertIs(collect.call_args.args[0], original)
        self.assertIsNot(original, adapter)

    def test_new_downstream_eps_error_still_refuses_after_operating_composition(self):
        spec,inputs,sources=fixture();self.attach(spec,inputs,sources)
        eps=next(l for l in inputs["filed_lines"] if l["concept"]=="eps")
        eps["cells"]["2024-03-31"]["value"]="999.00"
        with self.assertRaisesRegex(FinancialStatementStructureError,"eps.*tie"):
            materialize_financial_statement_structure(spec,inputs,complete=False)

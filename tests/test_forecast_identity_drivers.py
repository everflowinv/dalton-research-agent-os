import copy,unittest
from decimal import Decimal
from dalton_core.company_model_inputs import identity_input_lines
from dalton_core.model_forecast_driver import build_structured_forecast_model
from dalton_core.forecast_financial_execution import validate_execution_contract
from dalton_core.company_financial_statement_structure import validate_financial_statement_structure,forecast_structure_binding
from tests.test_financial_structure_forecast_consumer import financial_inputs,forecastable_proposal
from dalton_core.company_model_state import _numeric_context,DEFAULT_NUMERIC_CONTEXT_POLICY


class IdentityDriverTests(unittest.TestCase):
    def test_same_concept_same_amount_members_are_independent_and_missing_identity_does_not_fall_back(self):
        table=financial_inputs();ends=table["periods"];concept="revenue"
        identities=[{"statement":"income","dimensions":{"business:SegmentsAxis":"business:"+member}} for member in ("A","B")]
        spec={"company_ref":table["company_ref"],"spec_id":table["spec_ref"],"content_hash":"b"*64,"revenue_anchor_concept":concept,
            "expense_lines":[],"horizon":{"forecast_quarters":8},"revenue_drivers":[{"ref":key,"label":key,"kind":"segment","basis_concept":concept,"basis_identity":identity}
             for key,identity in zip(("a","b"),identities)],"driver_formulas":[{"output_ref":"revenue","operator":"sum","driver_refs":["a","b"],"because":"Two precisely identified business segments close to consolidated revenue."}]}
        rows=[]
        for i,end in enumerate(ends):
            for member,identity in zip(("A","B"),identities):
                cell=table["filed_lines"][0]["cells"][end]
                rows.append({"line_id":f"ingest:1#{i}-{member}","ingest_id":"ingest:1","ordinal":len(rows),"statement":"income","concept":concept,"dimensions":identity["dimensions"],
                    "is_breakdown":True,"dimension_axis":"business:SegmentsAxis","dimension_member":"business:"+member,
                    "label":member,"value":str(Decimal(str(cell["value"]))/2),"unit":"usd","period_start":cell["period_start"],"period_end":end,
                    "accession":"accession:identity","filed":"2026-01-01","filing_form":"10-K","statement_context_ref":"context:1"})
        class Authority:
            def statement_series_lines(self,*args,**kwargs):return rows
        table["filed_identity_lines"]=identity_input_lines(Authority(),spec)
        self.assertEqual(len(table["filed_identity_lines"]),2)
        st,r=validate_financial_statement_structure(forecastable_proposal(table),spec,table)
        body=build_structured_forecast_model(spec,table,structure=st,replay=r,binding=forecast_structure_binding(st,r,table))
        self.assertEqual(body["execution_contract"]["drivers"]["status"],"executed")
        validate_execution_contract(body,publishing=True)
        drivers=[d for d in body["drivers"] if d.get("statement_identity")]
        self.assertEqual(len(drivers),2);self.assertNotEqual(drivers[0]["statement_identity"],drivers[1]["statement_identity"])
        missing=copy.deepcopy(spec);missing["revenue_drivers"][1]["basis_identity"]["dimensions"]["business:SegmentsAxis"]="business:Missing"
        table["filed_identity_lines"]=identity_input_lines(Authority(),missing)
        blocked=build_structured_forecast_model(missing,table,structure=st,replay=r,binding=forecast_structure_binding(st,r,table))
        self.assertEqual(blocked["execution_contract"]["drivers"]["status"],"unexecuted")

    def test_numeric_context_preserves_full_dimension_identity_and_distinguishes_second_axis(self):
        filing={"ingest_id":"ingest:1","accession":"a","form":"10-Q","filed":"2026-04-01","report_date":"2026-03-31","content_hash":"b"*64}
        rows=[]
        for i,region in enumerate(("east","west")):
            rows.append({"ingest_id":"ingest:1","line_id":"ingest:1#"+str(i),"ordinal":i,"statement":"income","concept":"revenue","label":"Equal amount",
                "dimension_axis":"business:SegmentsAxis","dimension_member":"business:A","dimensions":{"business:SegmentsAxis":"business:A","region:Axis":region},
                "statement_context_ref":"context:1","period_start":"2026-01-01","period_end":"2026-03-31","value":"100","unit":"USD","balance":"credit"})
        context,_=_numeric_context([filing],{"ingest:1":rows},structure_series={("income","revenue")},policy=DEFAULT_NUMERIC_CONTEXT_POLICY)
        self.assertEqual(len(context["cells"]),2)
        self.assertEqual({c["dimensions"]["region:Axis"] for c in context["cells"]},{"east","west"})
        self.assertTrue(all(c["statement_context_ref"]=="context:1" for c in context["cells"]))

if __name__=="__main__":unittest.main()

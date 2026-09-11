from __future__ import annotations

import unittest
from copy import deepcopy

from dalton_core.company_model_inputs import FILED, build_model_inputs
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.model_forecast_driver import (
    ForecastModelAuthority,
    ForecastModelValidationError,
    build_forecast_model,
)
from dalton_core.store import DaltonStore
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_company_model_inputs import ACN, _line
from tests.test_model_forecast_driver import (
    COST_CONCEPT, REVENUE_CONCEPT, SGA_CONCEPT, TAX_CONCEPT, spec,
)


class CashFlowAuthorityRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        state = bootstrap_method_authorities(self.store)
        self.missions = CoverageMissionAuthority(self.store)
        params = mission_params(state)
        self.mission = self.missions.create_mission(params.pop("mission_ref"), **params)
        self.authorization = self.missions.authorize_sec_lane(
            company_ref=ACN, ticker="ACN", actor_ref="automation:coverage-mission",
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"],
        )

    def record(self, form, filings):
        dispatch = self.missions.queue_statement_dispatch(
            authorization=self.authorization, form=form, filing_limit=len(filings))
        self.missions.mark_statement_dispatch_launched(
            dispatch["dispatch_id"], f"sec-financials-run:{form}:test")
        self.missions.record_statement_observation(
            dispatch_id=dispatch["dispatch_id"],
            observation={
                "schema_version": "0.1", "cik": "0001467373",
                "entity_name": "Accenture plc", "filings": filings,
                "source_record_refs": ["raw-sink:" + "c" * 64],
                "next_cursor": None, "provider_status": 200,
            },
            governance_ref="connector-governance:test",
            governance_hash="b" * 64,
        )
        self.missions.settle_statement_dispatch(dispatch["dispatch_id"], outcome="succeeded")

    def test_store_ytd_and_annual_rows_publish_replayable_cash_model(self):
        concepts = {
            REVENUE_CONCEPT: ("1000", "2200", "3600", "5200", "income"),
            COST_CONCEPT: ("700", "1540", "2520", "3640", "income"),
            SGA_CONCEPT: ("100", "220", "360", "520", "income"),
            TAX_CONCEPT: ("50", "110", "180", "260", "income"),
            "us-gaap:NetCashProvidedByUsedInOperatingActivities":
                ("120", "260", "420", "600", "cash"),
            "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment":
                ("20", "45", "75", "110", "cash"),
        }
        ends = ("2025-08-31", "2025-11-30", "2026-02-28", "2026-05-31")
        accessions = [f"0001467373-26-{index:06d}" for index in range(1, 5)]
        filings = []
        for index, (end, accession) in enumerate(zip(ends, accessions)):
            form = "10-K" if index == 3 else "10-Q"
            lines = [
                _line(concept, "2025-06-01", end, values[index],
                      statement=statement, accession=accession)
                for concept, (*values, statement) in concepts.items()
            ]
            filings.append({
                "accession": accession, "form": form, "filed": "2026-06-25",
                "report_date": end, "lines": lines,
            })
        self.record("10-Q", filings[:3])
        self.record("10-K", filings[3:])
        specification = spec(cash="required")
        inputs = build_model_inputs(self.missions, specification)
        self.assertTrue(all(item["status"] == FILED
                            for item in inputs["cash_flow_inputs"]))
        draft = build_forecast_model(
            specification, inputs, mission_version_ref=self.mission["id"])
        statement_rows = [dict(row) for row in self.store.connection.execute(
            "SELECT * FROM coverage_mission_statement_lines ORDER BY line_id")]
        published = ForecastModelAuthority(self.store).publish(
            draft, statement_rows=statement_rows)
        self.assertEqual(published["schema_version"], "0.2")
        fcf = next(item for item in published["results"]
                   if item["ref"] == "result:free_cash_flow")
        self.assertEqual(fcf["status"], "computed")
        ocf = next(item for item in published["drivers"]
                   if item["role"] == "operating_cash_flow")
        q4 = next(item for item in ocf["history"] if item["period_end"] == ends[-1])
        self.assertEqual(q4["source_forms"], ["10-K", "10-Q"])
        self.assertEqual(len(q4["derived_from"]), 2)

        for mutate in (
            lambda cell: cell["source_forms"].__setitem__(0, "10-Q"),
            lambda cell: cell["derived_from"][0].__setitem__(
                "period_end", "2025-09-01"),
            lambda cell: cell.__setitem__("derived_from", []),
            lambda cell: cell.__setitem__("derived_from", ["not-an-operand", {}]),
        ):
            tampered = deepcopy(draft)
            tampered_ocf = next(item for item in tampered["drivers"]
                                if item["role"] == "operating_cash_flow")
            tampered_q4 = next(item for item in tampered_ocf["history"]
                               if item["period_end"] == ends[-1])
            mutate(tampered_q4)
            tampered["source_version_ref"] = published["id"]
            with self.assertRaises(ForecastModelValidationError):
                ForecastModelAuthority(self.store).publish(
                    tampered, statement_rows=statement_rows)

        wrong_statement = deepcopy(draft)
        next(item for item in wrong_statement["drivers"]
             if item["role"] == "operating_cash_flow")["statement"] = "income"
        wrong_statement["source_version_ref"] = published["id"]
        with self.assertRaises(ForecastModelValidationError):
            ForecastModelAuthority(self.store).publish(
                wrong_statement, statement_rows=statement_rows)

        # Both operands exist in immutable authority, and their subtraction is
        # exact, but FY minus Q1 is nine months rather than the claimed Q4.
        nonadjacent = deepcopy(draft)
        cash = next(item for item in nonadjacent["drivers"]
                    if item["role"] == "operating_cash_flow")
        q2 = next(item for item in cash["history"]
                  if item["period_end"] == ends[1])
        q4 = next(item for item in cash["history"]
                  if item["period_end"] == ends[-1])
        q4["derived_from"][0] = deepcopy(q2["derived_from"][0])
        q4["accessions"] = sorted([accessions[0], accessions[3]])
        q4["value"] = "480"
        nonadjacent["source_version_ref"] = published["id"]
        with self.assertRaisesRegex(ForecastModelValidationError,
                                    "derived periods do not replay"):
            ForecastModelAuthority(self.store).publish(
                nonadjacent, statement_rows=statement_rows)


if __name__ == "__main__":
    unittest.main()

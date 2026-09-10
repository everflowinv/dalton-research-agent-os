"""P13-M2: the driver model published, reconciled, and left alone.

Three things this file is for.

**The reconciler must not have to learn anything.** When Accenture reports, the
P9c machinery pairs a forecast line version with the formal Claim that carries
the actual and grades the deviation. A driver-model line is written through the
same authority under a second frozen formula, so the test that matters is that
``pending_pairs`` finds it and ``reconcile`` grades it without a single change
to that module.

**The lane must not have opinions.** It publishes a first model for a company
that has none, and it writes down actuals for quarters that have been filed.
It does not re-forecast because a document arrived -- not for a Claim, not for
a broker note, not even for a filing. What an event means for next year is a
judgement, and this layer does not make judgements.

**A run must say what it did in one word.** ``published``, ``duplicate``,
``refused:<reason>``, or nothing to do.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from dalton_core.company_model_forecast import (
    PUBLISHED_METRICS,
    ForecastPublishRefused,
    missing_write_scope,
    pending_action,
    publish_forecast_lines,
    run_company_forecast,
)
from dalton_core.company_model_forecast_cli import choose_company, run_model_forecast
from dalton_core.company_model_inputs import build_model_inputs
from dalton_core.company_model_spec import spec_from_response
from dalton_core.company_model_state import build_company_model_state
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.forecast_reconciliation import ForecastReconciliationAuthority
from dalton_core.model_forecast import (
    DRIVER_FORMULA_REF,
    ModelForecastAuthority,
    driver_model_version_ref,
)
from dalton_core.model_forecast_driver import (
    ForecastModelAuthority,
    ForecastModelConflict,
    ForecastModelValidationError,
    actualize_model,
    build_forecast_model,
)
from dalton_core.economic_invariants import EconomicInvariantRefused
from dalton_core.store import DaltonStore
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_forecast_reconciliation import (
    ForecastReconciliationFixture,
    _invocation,
)
from tests.test_model_forecast_driver import (
    COST_CONCEPT,
    QUARTERS,
    REVENUE_CONCEPT,
    SERIES,
    filed_quarter,
    ledger,
    model,
    spec,
)

ACN = "company:sec-cik:0001467373"
OWNER = "human:owner"


class PublishedLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.models = ForecastModelAuthority(self.store)
        self.lines = ModelForecastAuthority(self.store)
        self.record = self.models.publish(model())

    def test_only_the_metrics_the_reconciler_binds_are_published(self):
        published = publish_forecast_lines(self.lines, self.record)
        self.assertEqual({item["metric_or_aspect"] for item in published},
                         {"metric:revenue-usd"})
        self.assertEqual(list(PUBLISHED_METRICS), ["revenue"])
        self.assertEqual(len(published), 4)
        self.assertEqual(Decimal(published[0]["value"]), Decimal("1464100000"))

    def test_a_line_binds_the_exact_model_version_it_fell_out_of(self):
        published = publish_forecast_lines(self.lines, self.record)
        line = self.lines.line(published[0]["version_ref"])
        self.assertEqual(line["value_kind"], "derived_deterministic")
        self.assertEqual(line["formula_ref"], DRIVER_FORMULA_REF)
        self.assertEqual(driver_model_version_ref(line), self.record["id"])
        self.assertEqual(line["scenario_version_hash"], self.record["content_hash"])
        self.assertEqual(line["unit"], "one")
        self.assertEqual(line["currency"], "USD")
        # It binds no Model Input Ledger rows, because it rests on none.
        self.assertIsNone(line["base_input_version_ref"])
        self.assertIsNone(line["model_run_version_ref"])

    def test_publishing_the_same_model_twice_writes_nothing_twice(self):
        publish_forecast_lines(self.lines, self.record)
        again = publish_forecast_lines(self.lines, self.record)
        # "unchanged", not a second version: the forecast did not move, and a
        # line version saying the same number would claim it had.
        self.assertEqual({item["status"] for item in again}, {"unchanged"})
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM model_forecast_line_versions").fetchone()["n"], 4)

    def test_an_actual_is_never_published_as_a_forecast(self):
        publish_forecast_lines(self.lines, self.record)
        table = build_model_inputs(filed_quarter(ledger()), spec())
        second = self.models.publish(actualize_model(self.record, table))
        published = publish_forecast_lines(self.lines, second)
        # The realised quarter is gone from the forecast; the three still ahead
        # did not move, so nothing was written for them either.
        self.assertEqual([item["period_end"] for item in published],
                         ["2026-11-30", "2027-02-28", "2027-05-31"])
        self.assertEqual({item["status"] for item in published}, {"unchanged"})
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM model_forecast_line_versions").fetchone()["n"], 4)

    def test_a_unit_the_reconciler_cannot_scale_is_refused_not_assumed(self):
        record = dict(self.record)
        record["unit"] = "eur"
        with self.assertRaises(ForecastPublishRefused):
            publish_forecast_lines(self.lines, record)


class ReconciliationTests(unittest.TestCase):
    """P9c grades a driver-model line without being taught anything."""

    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.fixture = ForecastReconciliationFixture(self.store)
        self.models = ForecastModelAuthority(self.store)
        self.record = self.models.publish(model())
        self.published = publish_forecast_lines(self.models.store and self.fixture.forecast,
                                                self.record)

    def test_the_reconciler_finds_and_grades_the_driver_model_line(self):
        # Accenture's quarter to 2026-08-31 comes in at 1,500.0m against the
        # 1,464.1m this model estimated: 2.45% above, which is the notable
        # band -- past the 1% that is worth reading and short of the 3% that
        # would name the forecast_overturn checkpoint.
        claim = self.fixture.claim(current="1500000000", prior="1331000000")
        pairs = self.fixture.reconciler.pending_pairs()
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0]["forecast_line_version_ref"],
                         self.published[0]["version_ref"])
        result = self.fixture.reconciler.reconcile(
            forecast_line_version_ref=pairs[0]["forecast_line_version_ref"],
            claim_version_ref=claim["claim_version_id"],
            requested_by=OWNER, mission_binding=None)
        self.assertEqual(result["status"], "fresh")
        self.assertEqual(result["forecast_value"], "1464100000.00000000")
        self.assertEqual(result["actual_value"], "1500000000.00000000")
        self.assertEqual(result["deviation_percent"], "2.4520")
        self.assertEqual(result["band"], "notable")
        self.assertEqual(result["forecast_value_kind"], "derived_deterministic")
        self.assertIsNone(result["model_run_version_ref"])
        # And the reconciliation can be walked back to the model that made it.
        line = self.fixture.forecast.line(result["forecast_line_version_ref"])
        self.assertEqual(driver_model_version_ref(line), self.record["id"])


class LaneStateTests(unittest.TestCase):
    """The child against a real state directory: one action per run, or none."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state_dir = Path(self._dir.name)
        self.store = DaltonStore(str(self.state_dir / "core.sqlite"))
        self.addCleanup(self.store.close)
        state = bootstrap_method_authorities(self.store)
        self.missions = CoverageMissionAuthority(self.store)
        self.params = mission_params(state)
        self.mission_ref = self.params.pop("mission_ref")
        self.mission = self.missions.create_mission(self.mission_ref, **self.params)
        self._filed = 0
        self.file_quarters(QUARTERS, SERIES)
        self.record_spec()

    # -- fixture plumbing --------------------------------------------------

    def file_quarters(self, quarters, series) -> None:
        self._filed += 1
        accession = f"0001467373-26-0000{self._filed:02d}"
        authorization = self.missions.authorize_sec_lane(
            company_ref=ACN, ticker="ACN", actor_ref="automation:coverage-mission",
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"])
        dispatch = self.missions.queue_statement_dispatch(
            authorization=authorization, attempt=self._filed - 1)
        self.missions.mark_statement_dispatch_launched(
            dispatch["dispatch_id"], f"sec-financials-run:{self._filed:024d}")
        lines = []
        for concept, values in series.items():
            for (start, end), value in zip(quarters, values):
                lines.append({
                    "statement": "income", "concept": concept,
                    "label": concept.split(":")[-1], "level": 0,
                    "parent_concept": None, "is_breakdown": False,
                    "dimension_axis": None, "dimension_member": None,
                    "period_start": start, "period_end": end, "value": value,
                    "unit": "USD", "balance": "credit",
                })
        self.missions.record_statement_observation(
            dispatch_id=dispatch["dispatch_id"],
            observation={
                "schema_version": "0.1", "cik": "0001467373",
                "entity_name": "Accenture plc",
                "filings": [{
                    "accession": accession, "form": "10-Q",
                    "filed": f"2026-0{self._filed}-25",
                    "report_date": quarters[-1][1], "lines": lines,
                }],
                "source_record_refs": ["raw-sink:" + "c" * 64],
                "next_cursor": None, "provider_status": 200,
            },
            governance_ref="g", governance_hash="b" * 64)
        self.missions.settle_statement_dispatch(
            dispatch["dispatch_id"], outcome="succeeded")

    def record_spec(self) -> dict:
        state = build_company_model_state(self.missions, ACN, ticker="ACN")
        body = {
            "schema_version": "0.1",
            "assessment": "Delivery revenue times realised rate, less delivery cost.",
            "revenue_drivers": [{
                "ref": "top-line", "label": "Client work", "kind": "mix",
                "basis_concept": REVENUE_CONCEPT, "unit": "USD",
                "because": "The blend of work moves the total."}],
            "expense_lines": [{
                "ref": "delivery", "label": "Cost of services",
                "basis_concept": COST_CONCEPT, "behaviour": "variable_with_revenue",
                "driver_ref": None, "because": "Delivery cost follows revenue."}],
            "forecast_statements": [
                {"statement": "income", "importance": "required",
                 "because": "Revenue and margin are the question."},
                {"statement": "balance", "importance": "supporting",
                 "because": "Capital light."},
                {"statement": "cash", "importance": "not_material",
                 "because": "Nothing turns on it here."}],
            "operating_metrics": [],
            "horizon": {"historical_quarters": 12, "forecast_quarters": 4,
                        "because": "Three years spans the cycle."},
        }
        return self.missions.record_company_model_spec(
            spec_from_response(state, body, decided_by="automation:coverage-mission"),
            mission_version_ref=self.mission["id"])

    def child(self, **kwargs):
        return run_model_forecast(
            state_dir=self.state_dir, summary_dir=self.state_dir / "summary", **kwargs)

    # -- what the lane does ------------------------------------------------

    def test_the_first_run_publishes_a_model_and_its_forecast_lines(self):
        summary = self.child()
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["forecast_status"], "published")
        self.assertEqual(summary["action"], "first")
        self.assertEqual(summary["change_reason"], "evidence_thicker")
        self.assertEqual(summary["company_ref"], ACN)
        self.assertEqual(summary["model_version"], 1)
        self.assertEqual(summary["drivers"], 2)
        self.assertEqual(summary["forecast_quarters"], 4)
        self.assertEqual(summary["forecast_lines_written"], 4)
        self.assertEqual(summary["formal_authority_writes"], 0)
        self.assertEqual(summary["cost_micros"], 0)
        self.assertIn("result:revenue", summary["results_computed"])
        written = json.loads(
            (self.state_dir / "summary" / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(written["model_version_ref"], summary["model_version_ref"])

    def test_mismatched_filed_segments_refuse_the_model_end_to_end(self):
        filing = self.missions.statement_filings(ACN)[0]
        base = self.missions.statement_lines(filing["ingest_id"])[0]
        rows = self.missions.statement_lines(filing["ingest_id"])
        rows.extend([
            {**base, "label": "Consulting", "is_breakdown": True,
             "dimension_axis": "srt:ProductOrServiceAxis",
             "dimension_member": "acn:ConsultingMember", "value": "100"},
            {**base, "label": "Managed Services", "is_breakdown": True,
             "dimension_axis": "srt:ProductOrServiceAxis",
             "dimension_member": "acn:ManagedServicesMember", "value": "200"},
        ])
        original = self.missions.statement_lines
        self.missions.statement_lines = lambda ingest_id, **kwargs: (
            [row for row in rows if not kwargs.get("statement") or
             row["statement"] == kwargs["statement"]]
            if ingest_id == filing["ingest_id"] else original(ingest_id, **kwargs))
        with self.assertRaises(EconomicInvariantRefused):
            run_company_forecast(self.missions, self.missions.latest_company_model_spec(ACN),
                                 models=ForecastModelAuthority(self.store))
        self.assertEqual(ForecastModelAuthority(self.store).versions(ACN), [])

    def test_repeated_comparative_segments_use_one_coherent_filing(self):
        filings = self.missions.statement_filings(ACN)
        filing = filings[0]
        original_rows = self.missions.statement_lines(filing["ingest_id"])
        total = original_rows[0]
        half = str(Decimal(total["value"]) / 2)
        coherent = original_rows + [
            {**total, "label": label, "is_breakdown": True,
             "dimension_axis": "srt:ProductOrServiceAxis",
             "dimension_member": member, "value": half}
            for label, member in (("Consulting", "acn:ConsultingMember"),
                                  ("Managed Services", "acn:ManagedServicesMember"))
        ]
        older = {**filing, "ingest_id": "older-comparative", "filed": "2025-01-01"}
        self.missions.statement_filings = lambda company_ref=None: [older, filing]
        original = self.missions.statement_lines
        self.missions.statement_lines = lambda ingest_id, **kwargs: (
            [row for row in coherent if not kwargs.get("statement") or
             row["statement"] == kwargs["statement"]]
            if ingest_id in {filing["ingest_id"], older["ingest_id"]}
            else original(ingest_id, **kwargs))
        outcome = run_company_forecast(
            self.missions, self.missions.latest_company_model_spec(ACN),
            models=ForecastModelAuthority(self.store))
        self.assertEqual(outcome["status"], "fresh")

    def test_a_second_run_with_nothing_new_does_nothing(self):
        self.child()
        authority = ForecastModelAuthority(self.store)
        current = authority.latest(ACN)
        proof = authority.filing_proof(current["id"])
        self.assertEqual(proof["model_content_hash"], current["content_hash"])
        self.assertEqual(proof["inputs_hash"], current["inputs_hash"])
        self.assertEqual(proof["invariant_report"]["status"], "available")
        self.assertGreater(proof["statement_row_count"], 0)
        summary = self.child()
        self.assertEqual(summary["status"], "idle")
        self.assertEqual(summary["forecast_status"], "nothing_to_model")
        self.assertEqual(
            len(ForecastModelAuthority(self.store).versions(ACN)), 1)

    def test_filing_proof_rejects_unknown_ingest_and_wrong_accession(self):
        filing = self.missions.statement_filings(ACN)[0]
        rows = self.missions.statement_lines(filing["ingest_id"])
        body = build_forecast_model(
            self.missions.latest_company_model_spec(ACN),
            build_model_inputs(self.missions, self.missions.latest_company_model_spec(ACN)),
            mission_version_ref=self.mission["id"])
        with self.assertRaises(ForecastModelValidationError):
            ForecastModelAuthority(self.store).publish(
                body, statement_rows=[{**rows[0], "ingest_id": "unknown"}])
        body["drivers"][0]["history"][0]["accessions"] = ["wrong-accession"]
        with self.assertRaises(ForecastModelValidationError):
            ForecastModelAuthority(self.store).publish(body, statement_rows=rows)

    def test_filing_proof_rejects_wrong_historical_value(self):
        filing = self.missions.statement_filings(ACN)[0]
        rows = self.missions.statement_lines(filing["ingest_id"])
        specification = self.missions.latest_company_model_spec(ACN)
        body = build_forecast_model(
            specification, build_model_inputs(self.missions, specification),
            mission_version_ref=self.mission["id"])
        body["drivers"][0]["history"][0]["value"] = "999999999999"
        with self.assertRaises(ForecastModelValidationError) as caught:
            ForecastModelAuthority(self.store).publish(body, statement_rows=rows)
        self.assertIn("filing reconciliation mismatch", str(caught.exception))
        self.assertIn("value=999999999999", str(caught.exception))

    def test_filing_proof_rejects_another_company_ingest(self):
        filing = self.missions.statement_filings(ACN)[0]
        rows = self.missions.statement_lines(filing["ingest_id"])
        specification = self.missions.latest_company_model_spec(ACN)
        body = build_forecast_model(
            specification, build_model_inputs(self.missions, specification),
            mission_version_ref=self.mission["id"])
        self.store.connection.execute(
            "DROP TRIGGER coverage_mission_statement_filings_no_update")
        self.store.connection.execute(
            "UPDATE coverage_mission_statement_filings SET company_ref=? "
            "WHERE ingest_id=?", ("company:other", filing["ingest_id"]))
        with self.assertRaises(ForecastModelValidationError) as caught:
            ForecastModelAuthority(self.store).publish(body, statement_rows=rows)
        self.assertIn("belong to another company", str(caught.exception))

    def test_filing_proof_hash_tamper_fails_closed(self):
        self.child()
        authority = ForecastModelAuthority(self.store)
        current = authority.latest(ACN)
        self.store.connection.execute(
            "DROP TRIGGER forecast_model_filing_proof_no_update")
        self.store.connection.execute(
            "UPDATE forecast_model_filing_proofs SET statement_rows_hash=? "
            "WHERE model_version_id=?", ("0" * 64, current["id"]))
        with self.assertRaises(ForecastModelConflict):
            authority.filing_proof(current["id"])

    def test_a_new_claim_does_not_by_itself_produce_a_new_version(self):
        # The rule the owner set: versioning is a mechanism, not a trigger. A
        # qualitative Claim -- a note about demand, a broker's opinion, a
        # headline -- says nothing this layer is entitled to conclude about the
        # forecast. What it means is a judgement, and it arrives as an explicit
        # revision from whoever made it.
        self.child()
        self.store.register_invocation(_invocation("invocation:test-claim"))
        self.store.register_claim({
            "claim_ref": "claim:test:sentiment", "subject_ref": ACN,
            "metric_or_aspect": "demand_environment",
            "period": "2026-06-01..2026-08-31", "basis": "management-statement",
            "normalized_statement": "Management said demand is improving.",
            "claim_kind": "qualitative",
            "producer_invocation_refs": ["invocation:test-claim"],
            "actor_ref": "system:test",
        })
        summary = self.child()
        self.assertEqual(summary["forecast_status"], "nothing_to_model")
        self.assertEqual(len(ForecastModelAuthority(self.store).versions(ACN)), 1)

    def test_a_filing_makes_the_estimated_quarter_actual_and_nothing_else(self):
        first = self.child()
        self.file_quarters((("2026-06-01", "2026-08-31"),),
                           {REVENUE_CONCEPT: ("1500000000",),
                            COST_CONCEPT: ("1200000000",)})
        summary = self.child()
        self.assertEqual(summary["forecast_status"], "published")
        self.assertEqual(summary["action"], "actualize")
        self.assertEqual(summary["change_reason"], "filing_actual")
        self.assertEqual(summary["realised_quarters"], 1)
        self.assertEqual(summary["model_version"], 2)
        self.assertEqual(summary["forecast_quarters"], 3)
        # Nothing was written to the forecast lines: the quarters still ahead
        # did not move, and the one that was filed is no longer a forecast.
        models = ForecastModelAuthority(self.store)
        second = models.latest(ACN)
        revenue = next(item for item in second["results"]
                       if item["ref"] == "result:revenue")
        cells = {(cell["period"]["end"], cell["kind"]): cell for cell in revenue["cells"]}
        self.assertEqual(Decimal(cells[("2026-08-31", "actual")]["value"]),
                         Decimal("1500000000"))
        self.assertTrue(cells[("2026-08-31", "estimate")]["superseded_by"])
        before = next(item for item in models.model(first["model_version_ref"])["results"]
                      if item["ref"] == "result:revenue")
        self.assertEqual(
            {cell["period"]["end"]: cell["value"] for cell in before["cells"]
             if cell["period"]["end"] != "2026-08-31"},
            {cell["period"]["end"]: cell["value"] for cell in revenue["cells"]
             if cell["period"]["end"] != "2026-08-31"})

    def test_a_mission_without_the_write_scope_is_refused_by_name(self):
        params = dict(self.params)
        params["autonomy"] = {
            **params["autonomy"],
            "may_write": [item for item in params["autonomy"]["may_write"]
                          if item != "forecast_line"]}
        params.update({"version_id": "coverage-mission-version:us-it-services:2",
                       "prior_version_ref": self.mission["id"],
                       "idempotency_key": "coverage-mission:us-it-services:2"})
        self.missions.create_mission(self.mission_ref, **params)
        summary = self.child()
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["forecast_status"],
                         "refused:the mission does not grant the forecast_line write scope")
        self.assertEqual(ForecastModelAuthority(self.store).versions(ACN), [])

    def test_the_live_mission_manifest_already_grants_what_this_lane_needs(self):
        # Worth asserting rather than assuming: if it did not, this lane would
        # need a new mission version signed by the owner before it could write.
        self.assertIsNone(missing_write_scope(self.mission))

    def test_a_named_company_outside_the_universe_is_refused(self):
        # The mission is what says which companies this automation may work on
        # at all. A hand run that could reach past it would be a way to write a
        # model for a company nobody admitted, with the mission's own principal
        # on the record.
        summary = self.child(company_ref="company:sec-cik:0000320193")
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(
            summary["forecast_status"],
            "refused:company:sec-cik:0000320193 is not in this mission's universe")
        self.assertEqual(ForecastModelAuthority(self.store).versions(ACN), [])

    def test_a_dry_run_chooses_and_stops(self):
        summary = self.child(dry_run=True)
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["forecast_status"], "gated")
        self.assertEqual(summary["company_ref"], ACN)
        self.assertEqual(ForecastModelAuthority(self.store).versions(ACN), [])

    def test_the_chooser_and_the_run_agree_about_what_is_pending(self):
        models = ForecastModelAuthority(self.store)
        company_ref, spec_row, table = choose_company(
            self.missions, models, self.mission)
        self.assertEqual(company_ref, ACN)
        self.assertEqual(pending_action(None, spec_row, table), "first")
        self.child()
        self.assertEqual(
            choose_company(self.missions, models, self.mission), (None, None, None))

    def test_a_company_with_no_revenue_concept_is_refused_whole(self):
        models = ForecastModelAuthority(self.store)
        missions = self.missions
        spec_row = missions.latest_company_model_spec(ACN)
        broken = {**spec_row, "revenue_drivers": [
            {**spec_row["revenue_drivers"][0], "basis_concept": None}]}
        with self.assertRaises(Exception) as caught:
            run_company_forecast(missions, broken, models=models)
        self.assertIn("no revenue driver", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

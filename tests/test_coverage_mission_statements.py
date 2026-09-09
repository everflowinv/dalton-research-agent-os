"""P13ak: quarterly statements land in the ledger, once, with their structure.

The lane parses a 10-Q into a few hundred lines; these tests are about what
happens after the parse succeeds. Two things matter and both have burned this
codebase before: a dispatch that ran must reach a terminal state (the SEC
filing dispatches had no terminal success and froze the lane for a day), and
the same filing parsed twice must not become two copies of its lines.
"""

from __future__ import annotations

import unittest

from dalton_core.coverage_mission import (
    CoverageMissionAuthority,
    CoverageMissionConflict,
    CoverageMissionNotFound,
    CoverageMissionValidationError,
)
from dalton_core.store import DaltonStore
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

ACN = "company:sec-cik:0001467373"
ACCESSION = "0001467373-26-000031"
GOVERNANCE_HASH = "b" * 64


def _line(**overrides):
    line = {
        "statement": "income", "concept": "us-gaap:Revenues", "label": "Revenues",
        "level": 0, "parent_concept": None, "is_breakdown": False,
        "dimension_axis": None, "dimension_member": None,
        "period_start": "2026-04-01", "period_end": "2026-06-30",
        "value": "1414767000", "unit": "USD", "balance": "credit",
    }
    line.update(overrides)
    return line


def _observation(*, lines=None, accession=ACCESSION):
    return {
        "schema_version": "0.1", "cik": "0001467373", "entity_name": "Accenture plc",
        "filings": [{
            "accession": accession, "form": "10-Q", "filed": "2026-06-25",
            "report_date": "2026-06-30",
            "lines": list(lines if lines is not None else [_line()]),
        }],
        "source_record_refs": ["raw-sink:" + "c" * 64],
        "next_cursor": None, "provider_status": 200,
    }


class StatementIngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.state = bootstrap_method_authorities(self.store)
        self.authority = CoverageMissionAuthority(self.store)
        params = mission_params(self.state)
        self.mission = self.authority.create_mission(params.pop("mission_ref"), **params)
        self.authorization = self.authority.authorize_sec_lane(
            company_ref=ACN, ticker="ACN", actor_ref="automation:coverage-mission",
            mission_version_ref=self.mission["id"],
            mission_version_hash=self.mission["content_hash"],
        )

    def queue(self, **kwargs):
        return self.authority.queue_statement_dispatch(
            authorization=self.authorization, **kwargs)

    def launched(self, **kwargs):
        dispatch = self.queue(**kwargs)
        self.authority.mark_statement_dispatch_launched(
            dispatch["dispatch_id"], "sec-financials-run:" + "1" * 24)
        return dispatch

    def test_the_same_request_is_the_same_dispatch(self):
        first = self.queue()
        self.assertEqual(first["status_marker"], "fresh")
        self.assertEqual(first["status"], "pending")
        again = self.queue()
        self.assertEqual(again["status_marker"], "duplicate")
        self.assertEqual(again["dispatch_id"], first["dispatch_id"])
        # A different depth is a different run.
        self.assertNotEqual(self.queue(filing_limit=4)["dispatch_id"], first["dispatch_id"])

    def test_a_drifted_authorization_is_refused(self):
        with self.assertRaises(CoverageMissionConflict):
            self.authority.queue_statement_dispatch(
                authorization={**self.authorization, "ticker": "CTSH"})

    def test_the_form_and_depth_are_bounded(self):
        for kwargs in ({"form": "8-K"}, {"filing_limit": 0}, {"filing_limit": 99},
                       {"filing_limit": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(CoverageMissionValidationError):
                    self.queue(**kwargs)

    def test_a_dispatch_reaches_a_terminal_status(self):
        dispatch = self.launched()
        settled = self.authority.settle_statement_dispatch(
            dispatch["dispatch_id"], outcome="succeeded")
        self.assertEqual(settled["status"], "succeeded")
        self.assertEqual(self.authority.launched_statement_dispatches(), [])
        self.assertEqual(
            self.authority.settle_statement_dispatch(
                dispatch["dispatch_id"], outcome="succeeded")["status_marker"],
            "duplicate")
        with self.assertRaises(CoverageMissionConflict):
            self.authority.settle_statement_dispatch(
                dispatch["dispatch_id"], outcome="failed")

    def test_a_failure_carries_its_reason(self):
        dispatch = self.launched()
        settled = self.authority.settle_statement_dispatch(
            dispatch["dispatch_id"], outcome="failed",
            failure_reason="SecFinancialsRunError: no 10-Q filing found")
        self.assertIn("no 10-Q filing found", settled["failure_reason"])

    def test_a_pending_dispatch_does_not_settle(self):
        dispatch = self.queue()
        with self.assertRaises(CoverageMissionConflict):
            self.authority.settle_statement_dispatch(
                dispatch["dispatch_id"], outcome="succeeded")

    def test_an_unknown_dispatch_is_not_found(self):
        with self.assertRaises(CoverageMissionNotFound):
            self.authority.settle_statement_dispatch(
                "mission-statement-dispatch:nope", outcome="succeeded")

    def test_a_rejected_dispatch_never_launches(self):
        dispatch = self.queue()
        self.authority.reject_statement_dispatch(dispatch["dispatch_id"], "governance")
        with self.assertRaises(CoverageMissionConflict):
            self.authority.mark_statement_dispatch_launched(
                dispatch["dispatch_id"], "sec-financials-run:" + "2" * 24)

    def test_lines_land_with_their_structure_intact(self):
        dispatch = self.launched()
        lines = [
            _line(),
            _line(concept="us-gaap:CostOfRevenue", label="Cost of services",
                  level=1, parent_concept="us-gaap:Revenues", value="900000000"),
            _line(statement="balance", concept="us-gaap:Assets", label="Total assets",
                  period_start=None, period_end="2026-06-30", value="55000000000",
                  balance="debit"),
            _line(concept="us-gaap:Revenues", label="Americas", is_breakdown=True,
                  dimension_axis="srt:StatementGeographicalAxis",
                  dimension_member="acn:AmericasMember", value=None),
        ]
        result = self.authority.record_statement_observation(
            dispatch_id=dispatch["dispatch_id"],
            observation=_observation(lines=lines),
            governance_ref="connector-governance:sec-financial-statements:v2",
            governance_hash=GOVERNANCE_HASH,
        )
        self.assertEqual(result["line_count"], 4)
        held = self.authority.statement_filings(ACN)
        self.assertEqual([item["accession"] for item in held], [ACCESSION])
        stored = self.authority.statement_lines(held[0]["ingest_id"])
        self.assertEqual(stored[1]["parent_concept"], "us-gaap:Revenues")
        self.assertTrue(stored[3]["is_breakdown"])
        self.assertEqual(stored[3]["dimension_member"], "acn:AmericasMember")
        self.assertIsNone(stored[3]["value"])
        # A balance-sheet line is an instant: no start, and that is not a gap.
        balance = self.authority.statement_lines(held[0]["ingest_id"], statement="balance")
        self.assertEqual(len(balance), 1)
        self.assertIsNone(balance[0]["period_start"])
        # The quarter is told from the year to date by its start, not its end.
        self.assertEqual(stored[0]["period_start"], "2026-04-01")

    def test_the_same_filing_parsed_twice_is_one_filing(self):
        dispatch = self.launched()
        for _ in range(2):
            result = self.authority.record_statement_observation(
                dispatch_id=dispatch["dispatch_id"], observation=_observation(),
                governance_ref="connector-governance:sec-financial-statements:v2",
                governance_hash=GOVERNANCE_HASH,
            )
        self.assertEqual(result["filings"][0]["status_marker"], "duplicate")
        held = self.authority.statement_filings(ACN)
        self.assertEqual(len(held), 1)
        self.assertEqual(len(self.authority.statement_lines(held[0]["ingest_id"])), 1)

    def test_statements_need_a_launched_dispatch(self):
        dispatch = self.queue()
        with self.assertRaises(CoverageMissionConflict):
            self.authority.record_statement_observation(
                dispatch_id=dispatch["dispatch_id"], observation=_observation(),
                governance_ref="g", governance_hash=GOVERNANCE_HASH)

    def test_a_malformed_observation_is_refused_whole(self):
        dispatch = self.launched()
        broken = [
            {**_observation(), "filings": []},
            {**_observation(), "cik": ""},
            _observation(accession="not-an-accession"),
            _observation(lines=[_line(statement="equity")]),
            _observation(lines=[_line(period_end="")]),
            _observation(lines=[_line(level=-1)]),
        ]
        for observation in broken:
            with self.subTest(observation=observation["filings"]):
                with self.assertRaises(CoverageMissionValidationError):
                    self.authority.record_statement_observation(
                        dispatch_id=dispatch["dispatch_id"], observation=observation,
                        governance_ref="g", governance_hash=GOVERNANCE_HASH)
        self.assertEqual(self.authority.statement_filings(ACN), [])

    def test_a_runaway_parse_is_refused_rather_than_stored(self):
        dispatch = self.launched()
        with self.assertRaises(CoverageMissionValidationError):
            self.authority.record_statement_observation(
                dispatch_id=dispatch["dispatch_id"],
                observation=_observation(lines=[_line()] * 4001),
                governance_ref="g", governance_hash=GOVERNANCE_HASH)

    def test_coverage_reports_what_is_held_and_what_is_in_flight(self):
        dispatch = self.launched()
        before = self.authority.statement_coverage(ACN)
        self.assertEqual(before["accessions"], [])
        self.assertEqual(before["open_dispatches"], 1)
        self.authority.record_statement_observation(
            dispatch_id=dispatch["dispatch_id"], observation=_observation(),
            governance_ref="g", governance_hash=GOVERNANCE_HASH)
        self.authority.settle_statement_dispatch(
            dispatch["dispatch_id"], outcome="succeeded")
        after = self.authority.statement_coverage(ACN)
        self.assertEqual(after["accessions"], [ACCESSION])
        self.assertEqual(after["latest_report_date"], "2026-06-30")
        self.assertEqual(after["open_dispatches"], 0)
        self.assertEqual(after["dispatches"]["succeeded"], 1)

    def test_statement_lines_cannot_be_rewritten(self):
        import sqlite3

        dispatch = self.launched()
        self.authority.record_statement_observation(
            dispatch_id=dispatch["dispatch_id"], observation=_observation(),
            governance_ref="g", governance_hash=GOVERNANCE_HASH)
        for statement in (
            "UPDATE coverage_mission_statement_lines SET value='0'",
            "DELETE FROM coverage_mission_statement_lines",
            "DELETE FROM coverage_mission_statement_filings",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.store.connection.execute(statement)


if __name__ == "__main__":
    unittest.main()

"""P13ag: the statements child -- approval first, artifact always, contract last.

The child does four things in an order that matters. It refuses unless the
governance record on disk is approved *and* still describes the packaged
contract. It keeps the whole parse, hashed, before reading anything out of it.
It normalises. And it validates the observation against the frozen contract
before recording it, so an observation the contract cannot describe is refused
rather than stored and explained afterwards.

These run against a captured parse. Reaching SEC is a separate mode and not
what is under test here.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.connector_governance import build_governance_record
from dalton_core.sec_financials_cli import build_parser, run
from dalton_core.sec_financials_core import KIND
from dalton_core.store import canonical_json

OWNER = "human:lumos"


def fixture_parse(**overrides):
    """One filing whose income statement has a quarter and a year to date."""

    def fact(**kw):
        base = {
            "concept": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
            "label": "Revenues", "dimension": None, "member": None,
            "period_start": "2026-04-01", "period_end": "2026-06-30",
            "period_key": "duration_2026-04-01_2026-06-30",
            "unit_ref": "usd", "value": "1414767000", "balance": "credit",
        }
        base.update(kw)
        return base

    base = {
        "cik": "1352010",
        "entity_name": "EPAM Systems, Inc.",
        "filings": [{
            "accession": "0001352010-26-000046", "form": "10-Q",
            "filed": "2026-08-06", "report_date": "2026-06-30",
            "statements": {
                "income": {
                    "structure": [{
                        "concept": "us-gaap_RevenueFromContractWithCustomerExcludingAssessedTax",
                        "label": "Revenues", "level": 1, "abstract": False,
                        "parent_concept": "us-gaap_OperatingIncomeLoss",
                        "is_breakdown": False, "dimension_axis": None,
                        "dimension_member": None, "balance": "credit",
                    }],
                    "facts": [
                        fact(),
                        fact(period_start="2026-01-01",
                             period_key="duration_2026-01-01_2026-06-30",
                             value="2814828000"),
                    ],
                },
                "balance": {
                    "structure": [{
                        "concept": "us-gaap_Assets", "label": "Total assets",
                        "level": 1, "abstract": False, "parent_concept": None,
                        "is_breakdown": False, "dimension_axis": None,
                        "dimension_member": None, "balance": "debit",
                    }],
                    "facts": [fact(concept="us-gaap:Assets", label="Total assets",
                                   period_start=None, period_end=None,
                                   period_key="instant_2026-06-30",
                                   value="4574086000", balance="debit")],
                },
            },
        }],
    }
    base.update(overrides)
    return base


class ChildTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.out = self.root / "out"
        self.out.mkdir()

    def governance(self, *, status="approved", version=2, **overrides):
        record = build_governance_record(
            KIND, approved_by=OWNER, status=status,
            effective_from="2026-09-09T00:00:00+00:00", version=version)
        record.update(overrides)
        if overrides:
            from dalton_core.store import content_hash

            record.pop("content_hash", None)
            record["content_hash"] = content_hash(record)
        path = self.root / "governance.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def fixture(self, value=None):
        path = self.root / "parse.json"
        path.write_text(json.dumps(value if value is not None else fixture_parse()),
                        encoding="utf-8")
        return path

    def run_child(self, *, governance=None, fixture=None):
        args = build_parser().parse_args([
            "--state-dir", str(self.state),
            "--governance", str(governance or self.governance()),
            "--ticker", "EPAM", "--form", "10-Q",
            "--fixture-file", str(fixture or self.fixture()),
            "--summary-dir", str(self.out), "--quiet",
        ])
        return run(args)

    # -- authority ---------------------------------------------------------

    def test_a_proposed_record_is_not_authority(self):
        summary = self.run_child(governance=self.governance(status="proposed"))
        self.assertEqual(summary["status"], "failed")
        self.assertIn("not approved", summary["failure_reason"])

    def test_v2_keeps_the_legacy_wire_projection_byte_shape(self):
        summary = self.run_child()
        self.assertTrue(all("dimension_count" not in line
                            for line in summary["observation"]["filings"][0]["lines"]))

    def test_approved_v3_carries_complete_dimension_proof(self):
        raw = fixture_parse()
        income = raw["filings"][0]["statements"]["income"]
        income["structure"][0].update({
            "dimension_axis": "srt:StatementGeographicalAxis",
            "dimension_member": "srt:AmericasMember", "is_breakdown": True,
        })
        income["facts"][0].update({
            "dimension": "srt:StatementGeographicalAxis", "member": "srt:AmericasMember"})
        income["facts"][0][
            "dim_srt_StatementGeographicalAxis"] = "srt:AmericasMember"
        summary = self.run_child(
            governance=self.governance(version=3), fixture=self.fixture(raw))
        line = next(line for line in summary["observation"]["filings"][0]["lines"]
                    if line["dimension_axis"] is not None)
        self.assertEqual(line["dimension_count"], 1)

    def test_unapproved_v3_refuses_before_live_parser_transport(self):
        from unittest.mock import patch

        args = build_parser().parse_args([
            "--state-dir", str(self.state), "--governance",
            str(self.governance(status="proposed", version=3)),
            "--ticker", "EPAM", "--allow-network", "--summary-dir", str(self.out),
            "--quiet",
        ])
        with patch("dalton_core.sec_financials_cli.parse_live") as transport:
            summary = run(args)
        transport.assert_not_called()
        self.assertEqual(summary["status"], "failed")

    def test_a_record_whose_contract_moved_is_refused_before_anything_runs(self):
        # The P13z failure: an approval that no longer covers the contract.
        summary = self.run_child(
            governance=self.governance(expected_schema_hash="0" * 64))
        self.assertEqual(summary["status"], "failed")
        self.assertIn("does not cover this output contract", summary["failure_reason"])
        self.assertIsNone(summary["artifact"])

    def test_a_record_for_another_capability_is_refused(self):
        summary = self.run_child(
            governance=self.governance(
                capability_id="capability:dalton:connector:sec-edgar"))
        self.assertEqual(summary["status"], "failed")

    # -- the artifact ------------------------------------------------------

    def test_the_whole_parse_is_kept_and_hashed_before_it_is_read(self):
        summary = self.run_child()
        artifact = summary["artifact"]
        stored = (self.state / "connector-spool" / "connector-spool" / "objects"
                  / artifact["content_hash"][:2] / artifact["content_hash"])
        self.assertTrue(stored.is_file())
        self.assertEqual(hashlib.sha256(stored.read_bytes()).hexdigest(),
                         artifact["content_hash"])
        # It is the parse, not the normalised wire: what the normaliser drops
        # stays recoverable from here.
        self.assertEqual(json.loads(stored.read_text(encoding="utf-8"))["cik"], "1352010")

    def test_the_observation_cites_the_artifact_it_came_from(self):
        summary = self.run_child()
        self.assertEqual(summary["observation"]["source_record_refs"],
                         [f"raw-sink:{summary['artifact']['content_hash']}"])

    def test_the_artifact_is_byte_identical_across_runs(self):
        first = self.run_child()["artifact"]["content_hash"]
        second = self.run_child()["artifact"]["content_hash"]
        self.assertEqual(first, second)

    # -- the observation ---------------------------------------------------

    def test_a_quarter_and_a_year_to_date_both_survive(self):
        summary = self.run_child()
        lines = summary["observation"]["filings"][0]["lines"]
        revenue = [line for line in lines if line["statement"] == "income"]
        self.assertEqual(
            {(line["period_start"], line["value"]) for line in revenue},
            {("2026-04-01", "1414767000"), ("2026-01-01", "2814828000")})

    def test_a_balance_sheet_line_arrives_as_an_instant(self):
        summary = self.run_child()
        [balance] = [line for line in summary["observation"]["filings"][0]["lines"]
                     if line["statement"] == "balance"]
        self.assertIsNone(balance["period_start"])
        self.assertEqual(balance["period_end"], "2026-06-30")
        self.assertEqual(balance["value"], "4574086000")

    def test_the_cik_is_padded_the_way_every_other_ref_is(self):
        self.assertEqual(self.run_child()["observation"]["cik"], "0001352010")

    def test_what_was_dropped_is_reported_beside_the_run(self):
        # Not in the observation: the contract is the statement, and a reader
        # asking "why is this line missing" looks at the run.
        summary = self.run_child()
        self.assertIn("dropped", summary)
        self.assertNotIn("dropped", summary["observation"]["filings"][0])

    def test_a_parse_with_no_filing_is_a_failure_not_an_empty_success(self):
        summary = self.run_child(fixture=self.fixture(fixture_parse(filings=[])))
        self.assertEqual(summary["status"], "failed")
        self.assertIn("no filing", summary["failure_reason"])

    # -- the summary -------------------------------------------------------

    def test_the_summary_is_owner_only_and_on_disk(self):
        self.run_child()
        path = self.out / "summary.json"
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["status"],
                         "succeeded")

    def test_a_failure_still_writes_a_summary_saying_why(self):
        self.run_child(governance=self.governance(status="proposed"))
        recorded = json.loads((self.out / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(recorded["status"], "failed")
        self.assertTrue(recorded["failure_reason"])


class ArgumentTests(unittest.TestCase):
    def test_exactly_one_transport_must_be_chosen(self):
        from dalton_core.sec_financials_cli import main

        for argv in (
            ["--state-dir", "/tmp", "--governance", "/tmp/g.json", "--ticker", "EPAM"],
            ["--state-dir", "/tmp", "--governance", "/tmp/g.json", "--ticker", "EPAM",
             "--allow-network", "--fixture-file", "/tmp/f.json"],
        ):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                main(argv)

    def test_a_company_must_be_named(self):
        from dalton_core.sec_financials_cli import main

        with self.assertRaises(SystemExit):
            main(["--state-dir", "/tmp", "--governance", "/tmp/g.json",
                  "--fixture-file", "/tmp/f.json"])


if __name__ == "__main__":
    unittest.main()

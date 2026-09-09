"""S4: the China / Hong Kong child -- approval first, artifact always, contract last.

The child does three things in an order that matters, and each of them is a
refusal that has to happen before the next thing is allowed to. It refuses
unless the governance record is approved *and* still describes the packaged
contract for this exact operation. It hashes the whole capture into the raw
spool before reading a number out of it, including on a run that then fails.
It validates the wire against the frozen contract before writing it anywhere.

Everything here replays the captured calls. Reaching 东方财富 is a separate
mode and is not what is under test.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.cn_hk_findata_cli import CLOCK_FIELDS, build_parser, run
from dalton_core.cn_hk_findata_core import (
    KIND_BY_OPERATION,
    OPERATIONS,
    build_cn_hk_findata_governance_record,
)
from dalton_core.store import canonical_json, content_hash

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cn-hk-findata"
OWNER = "human:lumos"

# One captured call per operation, and the arguments that name it.
CASES = {
    "financial_statements": (
        "financial-statements-a-600519-income",
        {"market": "a", "ticker": "600519", "statement_kind": "income",
         "period_type": "report"},
    ),
    "shareholders": (
        "shareholders-600519", {"a_ticker": "600519", "period_end": "2026-06-30"},
    ),
    "buybacks": ("buybacks-600519", {"a_ticker": "600519"}),
    "margin_balance": (
        "margin-balance-sse",
        {"exchange": "sse", "start": "2026-08-25", "end": "2026-09-05"},
    ),
    "northbound_flow": ("northbound-flow", {"as_of": "2026-09-09"}),
    "ah_premium": ("ah-premium", {"ticker": "01398"}),
}
ARGUMENTS = (
    "market", "ticker", "a_ticker", "statement_kind", "period_type",
    "period_end", "exchange", "start", "end", "as_of",
)


class ChildTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = Path(self._dir.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.out = self.root / "out"
        self.out.mkdir()

    def governance(self, operation, *, status="approved", **overrides):
        record = build_cn_hk_findata_governance_record(
            operation=operation, approved_by=OWNER, status=status)
        record.update(overrides)
        if overrides:
            record.pop("content_hash", None)
            record["content_hash"] = content_hash(record)
        path = self.root / f"governance-{operation}-{status}-{len(overrides)}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def fixture(self, name, value=None):
        raw = value if value is not None else json.loads(
            (FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        path = self.root / "call.json"
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        return path

    def namespace(self, operation, *, governance=None, fixture=None, **overrides):
        name, parameters = CASES[operation]
        values = {argument: None for argument in ARGUMENTS}
        values.update(parameters)
        values.update(overrides)
        return argparse.Namespace(
            state_dir=str(self.state),
            governance=str(governance or self.governance(operation)),
            operation=operation,
            summary_dir=str(self.out),
            allow_network=False,
            fixture_file=str(fixture or self.fixture(name)),
            no_wire=False,
            quiet=True,
            **values,
        )

    def run_child(self, operation, **kwargs):
        return run(self.namespace(operation, **kwargs))

    # -- approval first ---------------------------------------------------

    def test_a_proposed_record_stops_the_run_before_anything_is_read(self):
        summary = self.run_child(
            "buybacks", governance=self.governance("buybacks", status="proposed"))
        self.assertEqual(summary["status"], "failed")
        self.assertIn("not approved", summary["failure_reason"])
        self.assertIsNone(summary["artifact"])

    def test_one_operations_record_cannot_run_another(self):
        summary = self.run_child(
            "buybacks", governance=self.governance("shareholders"))
        self.assertEqual(summary["status"], "failed")
        self.assertIn("different capability", summary["failure_reason"])

    def test_a_record_whose_contract_moved_is_refused(self):
        summary = self.run_child(
            "buybacks",
            governance=self.governance("buybacks", expected_schema_hash="0" * 64))
        self.assertEqual(summary["status"], "failed")
        self.assertIn("does not cover this output contract",
                      summary["failure_reason"])

    def test_a_record_whose_source_moved_is_refused(self):
        summary = self.run_child(
            "buybacks",
            governance=self.governance("buybacks", expected_source_hash="0" * 64))
        self.assertIn("source hash", summary["failure_reason"])

    # -- artifact always --------------------------------------------------

    def test_the_artifact_hash_is_the_hash_of_what_the_source_said(self):
        name, _ = CASES["northbound_flow"]
        raw = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        summary = self.run_child("northbound_flow")
        expected = hashlib.sha256(canonical_json(
            {key: value for key, value in raw.items()
             if key not in CLOCK_FIELDS}
        ).encode("utf-8")).hexdigest()
        self.assertEqual(summary["artifact"]["content_hash"], expected)
        # When it was read is still recorded; it is simply not part of what
        # was read.
        self.assertEqual(summary["captured_at"], raw["captured_at"])

    def test_the_same_capture_twice_is_the_same_artifact(self):
        first = self.run_child("northbound_flow")
        second = self.run_child("northbound_flow")
        self.assertEqual(first["artifact"]["content_hash"],
                         second["artifact"]["content_hash"])
        self.assertEqual(first["invocation_ref"], second["invocation_ref"])

    def test_reading_the_same_answer_at_a_different_time_is_one_invocation(self):
        # This is the whole point of the invocation ref. While the clock
        # fields were inside the hashed bytes, two readings of the same
        # unchanged quarter minted two artifact hashes and two invocations,
        # and a version chain reading them could not tell "nothing changed"
        # from "something changed twice".
        name, _ = CASES["northbound_flow"]
        raw = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        first = self.run_child("northbound_flow", fixture=self.fixture(name, raw))
        later = dict(raw)
        later["captured_at"] = "2026-09-10T02:31:44.000001+00:00"
        later["observed_on"] = "2026-09-10"
        second = self.run_child("northbound_flow",
                                fixture=self.fixture(name, later))
        self.assertNotEqual(first["captured_at"], second["captured_at"])
        self.assertEqual(first["artifact"]["content_hash"],
                         second["artifact"]["content_hash"])
        self.assertEqual(first["invocation_ref"], second["invocation_ref"])

    def test_a_changed_answer_is_a_different_invocation(self):
        name, _ = CASES["northbound_flow"]
        raw = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        first = self.run_child("northbound_flow", fixture=self.fixture(name, raw))
        restated = json.loads(json.dumps(raw))
        restated["frames"]["flow"]["rows"][0]["资金净流入"] = "1234"
        second = self.run_child("northbound_flow",
                                fixture=self.fixture(name, restated))
        self.assertNotEqual(first["artifact"]["content_hash"],
                            second["artifact"]["content_hash"])
        self.assertNotEqual(first["invocation_ref"], second["invocation_ref"])

    def test_a_run_that_cannot_be_normalised_still_leaves_its_artifact(self):
        name, _ = CASES["northbound_flow"]
        raw = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        for row in raw["frames"]["flow"]["rows"]:
            row["交易日"] = "2020-01-01"
        summary = self.run_child(
            "northbound_flow", fixture=self.fixture(name, raw))
        self.assertEqual(summary["status"], "failed")
        self.assertIsNotNone(summary["artifact"])
        self.assertIn("was asked for", summary["failure_reason"])

    # -- contract last ----------------------------------------------------

    def test_every_operation_runs_end_to_end_from_a_real_capture(self):
        for operation in OPERATIONS:
            with self.subTest(operation=operation):
                summary = self.run_child(operation)
                self.assertEqual(summary["status"], "succeeded",
                                 summary["failure_reason"])
                self.assertGreater(summary["row_count"], 0)
                self.assertFalse(summary["fallback_used"])
                self.assertEqual(summary["governance_ref"],
                                 f"connector-governance:"
                                 f"{KIND_BY_OPERATION[operation]}:v1")
                wire = json.loads(
                    Path(summary["wire_path"]).read_text(encoding="utf-8"))
                self.assertEqual(wire["schema_version"], "0.1")

    def test_a_wire_the_contract_cannot_describe_never_reaches_disk(self):
        import dalton_core.cn_hk_findata_cli as cli

        original = cli.WIRE_BUILDERS["northbound_flow"]

        def broken(raw, *, source_record_refs):
            built = original(raw, source_record_refs=source_record_refs)
            built["rows"][0]["net_inflow"] = "not a number"
            return built

        cli.WIRE_BUILDERS["northbound_flow"] = broken
        try:
            summary = self.run_child("northbound_flow")
        finally:
            cli.WIRE_BUILDERS["northbound_flow"] = original
        self.assertEqual(summary["status"], "failed")
        self.assertIsNone(summary["wire_path"])
        self.assertFalse(list(self.out.glob("wire-*.json")))

    def test_a_vendor_refusal_is_reported_as_its_own_kind(self):
        # A lane reading this summary must be able to tell "the source did not
        # answer and nothing may stand in for it" from every other failure,
        # because the correct response is to wait rather than to ask elsewhere.
        name, _ = CASES["ah_premium"]
        raw = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        raw["vendor"] = "tencent"
        summary = self.run_child("ah_premium", fixture=self.fixture(name, raw))
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(summary["refusal_kind"],
                         "vendor_unavailable_no_substitute")
        self.assertIn("没有比价与溢价", summary["failure_reason"])

    def test_a_replay_cannot_be_relabelled_as_another_company(self):
        # A capture of 贵州茅台 replayed under a different code would produce a
        # wire that validates, that names 000001, and that is entirely about
        # 600519. Nothing further down the chain could notice.
        name, _ = CASES["buybacks"]
        summary = self.run_child("buybacks", a_ticker="000001")
        self.assertEqual(summary["status"], "failed")
        self.assertIn("different request", summary["failure_reason"])
        self.assertIn("600519", summary["failure_reason"])
        self.assertIsNone(summary["artifact"])

    def test_a_replay_that_answers_the_question_asked_is_allowed(self):
        name, _ = CASES["buybacks"]
        raw = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        raw["parameters"]["a_ticker"] = "000001"
        summary = self.run_child(
            "buybacks", a_ticker="000001", fixture=self.fixture(name, raw))
        self.assertEqual(summary["status"], "succeeded", summary["failure_reason"])

    def test_an_empty_answer_names_no_vendor_rather_than_an_empty_list(self):
        # Both documented empty answers. They are findings, not failures: this
        # company announced no buyback, and this code is not half of an A+H
        # pair. Neither has a vendor, which is not the same as having none of
        # them -- a cockpit rendering ``[]`` as a vendor is showing a bug.
        name, _ = CASES["buybacks"]
        raw = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        raw["parameters"]["a_ticker"] = "000001"
        summary = self.run_child(
            "buybacks", a_ticker="000001", fixture=self.fixture(name, raw))
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["row_count"], 0)
        self.assertIsNone(summary["source_vendor"])
        self.assertFalse(summary["fallback_used"])
        self.assertEqual(summary["caliber_notes"], [])

        name, _ = CASES["ah_premium"]
        raw = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        raw["parameters"]["ticker"] = "00700"
        summary = self.run_child(
            "ah_premium", ticker="00700", fixture=self.fixture(name, raw))
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["row_count"], 0)
        self.assertIsNone(summary["source_vendor"])

    def test_one_vendor_is_reported_as_a_name_and_not_as_a_list(self):
        self.assertEqual(self.run_child("buybacks")["source_vendor"], "eastmoney")

    def test_a_successful_summary_carries_the_caliber_notes(self):
        summary = self.run_child("margin_balance")
        self.assertEqual(summary["source_vendor"], "sse")
        self.assertTrue(any("不可直接相加" in note
                            for note in summary["caliber_notes"]))
        self.assertEqual(summary["allowed_hosts"],
                         ["query.sse.com.cn", "www.szse.cn"])

    # -- the shape of the run ---------------------------------------------

    def test_a_summary_is_written_even_when_the_run_fails(self):
        self.run_child("buybacks",
                       governance=self.governance("buybacks", status="proposed"))
        summary = json.loads(
            (self.out / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["status"], "failed")

    def test_a_missing_required_parameter_is_refused_by_name(self):
        summary = self.run_child("shareholders", period_end=None)
        self.assertIn("--period-end", summary["failure_reason"])

    def test_neither_mode_and_both_modes_are_both_refused(self):
        namespace = self.namespace("buybacks")
        namespace.fixture_file = None
        self.assertIn("exactly one", run(namespace)["failure_reason"])
        namespace = self.namespace("buybacks")
        namespace.allow_network = True
        self.assertIn("exactly one", run(namespace)["failure_reason"])

    def test_the_parser_refuses_a_run_with_no_mode(self):
        parser = build_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args([])
        arguments = parser.parse_args([
            "--state-dir", str(self.state), "--governance", "/tmp/g.json",
            "--operation", "buybacks", "--a-ticker", "600519",
            "--fixture-file", "/tmp/f.json",
        ])
        self.assertEqual(arguments.operation, "buybacks")

    def test_an_operation_outside_the_six_is_refused(self):
        namespace = self.namespace("buybacks")
        namespace.operation = "fund_flow_individual"
        self.assertIn("no 'fund_flow_individual' operation",
                      run(namespace)["failure_reason"])

    def test_no_run_here_touched_the_network(self):
        # The library is an optional extra and is not imported by any of this.
        # If a test ever reaches for it, this is where that shows up.
        import sys

        self.assertNotIn("akshare", sys.modules)


if __name__ == "__main__":
    unittest.main()

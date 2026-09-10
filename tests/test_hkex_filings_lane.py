"""W4: the child, the launcher and the lane, all offline.

The child is exercised through ``run`` with ``--fixture-file``, which is the
whole point of that mode: the run that reaches HKEX and the run that replays a
capture take exactly the same path from the governance check onwards.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.hkex_filings_cli import (
    CLOCK_FIELDS,
    HkexFilingsRunError,
    artifact_payload,
    build_parser,
    fetch,
    run,
)
from dalton_core.hkex_filings_core import (
    ANNOUNCEMENTS_INDEX_OPERATION,
    DISCLOSURE_OF_INTERESTS_OPERATION,
    DAILY_BUYBACK_TAPE_OPERATION,
    KIND_BY_OPERATION,
    MONTHLY_RETURNS_OPERATION,
    NEXT_DAY_DISCLOSURE_OPERATION,
    OPERATIONS,
    build_hkex_filings_governance_record,
    company_ref,
)
from dalton_core.hkex_filings_launcher import (
    GOVERNANCE_FILENAME_BY_OPERATION,
    HkexFilingsLauncher,
)
from dalton_core.lane_child_launcher import LaneChildRejected
from dalton_core.mission_hkex_lane import (
    LAUNCHER_KWARG,
    MissionHkexLaneCoordinator,
    argv_fragment,
    hk_universe,
    missing_scopes,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hkex-filings"
COMPANY = company_ref("00700")
ARGUMENTS = {
    NEXT_DAY_DISCLOSURE_OPERATION: (
        {"as_of": "2026-09-09"}, "next-day-disclosure-00700-20260909.json"),
    MONTHLY_RETURNS_OPERATION: (
        {"since": "2026-08-01", "until": "2026-09-10"}, "monthly-returns-00700.json"),
    ANNOUNCEMENTS_INDEX_OPERATION: (
        {"since": "2026-08-01", "until": "2026-09-10", "headline_category": "-2"},
        "announcements-index-00700.json"),
    DISCLOSURE_OF_INTERESTS_OPERATION: (
        {"since": "2026-01-01", "until": "2026-09-10"},
        "disclosure-of-interests-00700.json"),
}


class ChildHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.governance = self.root / "connector-governance"
        self.governance.mkdir(parents=True, exist_ok=True)
        for operation in OPERATIONS:
            self.write_record(operation, status="approved")

    def write_record(self, operation: str, *, status: str) -> Path:
        record = build_hkex_filings_governance_record(
            operation=operation, approved_by="human:lumos", status=status
        )
        path = self.governance / GOVERNANCE_FILENAME_BY_OPERATION[operation]
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def namespace(self, operation: str, **overrides: object) -> argparse.Namespace:
        extra, fixture = ARGUMENTS[operation]
        argv = [
            "--state-dir", str(self.root),
            "--governance", str(
                overrides.pop("governance", None)
                or self.governance / GOVERNANCE_FILENAME_BY_OPERATION[operation]
            ),
            "--operation", operation,
            "--hk-ticker", str(overrides.pop("hk_ticker", "00700")),
            "--fixture-file", str(
                overrides.pop("fixture_file", None) or FIXTURES / fixture
            ),
            "--summary-dir", str(self.root / operation),
            "--quiet",
        ]
        merged = {**extra, **overrides}
        for flag in ("as_of", "since", "until", "headline_category",
                     "current_price", "prior_rows_file"):
            if merged.get(flag):
                argv.extend([f"--{flag.replace('_', '-')}", str(merged[flag])])
        return build_parser().parse_args(argv)


class ApprovalTests(ChildHarness):
    def test_an_unapproved_record_reads_nothing(self) -> None:
        self.write_record(NEXT_DAY_DISCLOSURE_OPERATION, status="proposed")
        summary = run(self.namespace(NEXT_DAY_DISCLOSURE_OPERATION))
        self.assertEqual(summary["status"], "failed")
        self.assertIn("not approved", summary["failure_reason"])
        self.assertIsNone(summary["artifact"])

    def test_one_operations_record_cannot_run_another(self) -> None:
        # An approval to read what a company bought back is not an approval to
        # read who its directors are.
        summary = run(self.namespace(
            DISCLOSURE_OF_INTERESTS_OPERATION,
            governance=self.governance / GOVERNANCE_FILENAME_BY_OPERATION[
                NEXT_DAY_DISCLOSURE_OPERATION],
        ))
        self.assertEqual(summary["status"], "failed")
        self.assertIn("different capability", summary["failure_reason"])
        self.assertIsNone(summary["artifact"])

    def test_a_drifted_schema_hash_is_refused_before_anything_is_read(self) -> None:
        path = self.governance / GOVERNANCE_FILENAME_BY_OPERATION[
            NEXT_DAY_DISCLOSURE_OPERATION]
        from dalton_core.store import content_hash

        record = json.loads(path.read_text(encoding="utf-8"))
        record.pop("content_hash")
        record["expected_schema_hash"] = "0" * 64
        record["content_hash"] = content_hash(record)
        path.write_text(json.dumps(record), encoding="utf-8")
        summary = run(self.namespace(NEXT_DAY_DISCLOSURE_OPERATION))
        self.assertIn("schema hash", summary["failure_reason"])
        self.assertIsNone(summary["artifact"])


class ReplayTests(ChildHarness):
    def test_all_four_operations_run_end_to_end(self) -> None:
        for operation in OPERATIONS:
            summary = run(self.namespace(operation))
            self.assertEqual(summary["status"], "succeeded",
                             (operation, summary["failure_reason"]))
            self.assertEqual(len(summary["artifact"]["content_hash"]), 64)
            self.assertTrue(summary["invocation_ref"].startswith(
                "connector-invocation:hkex-filings:"))
            self.assertEqual(summary["company_ref"], COMPANY)

    def test_replaying_a_capture_answers_the_question_it_was_asked(self) -> None:
        # A 00700 capture replayed under another code would produce a wire that
        # validates, says 00001, and is entirely about Tencent.
        summary = run(self.namespace(NEXT_DAY_DISCLOSURE_OPERATION,
                                     hk_ticker="00001"))
        self.assertEqual(summary["status"], "failed")
        self.assertIn("question nobody asked", summary["failure_reason"])

    def test_a_capture_of_another_operation_is_refused(self) -> None:
        summary = run(self.namespace(
            NEXT_DAY_DISCLOSURE_OPERATION,
            fixture_file=FIXTURES / "announcements-index-00700.json",
        ))
        self.assertEqual(summary["status"], "failed")
        self.assertIn("read and this run", summary["failure_reason"])

    def test_a_different_window_is_refused(self) -> None:
        summary = run(self.namespace(ANNOUNCEMENTS_INDEX_OPERATION,
                                     since="2026-07-01"))
        self.assertEqual(summary["status"], "failed")
        self.assertIn("different parameters", summary["failure_reason"])

    def test_the_two_modes_are_exclusive(self) -> None:
        args = self.namespace(NEXT_DAY_DISCLOSURE_OPERATION)
        args.allow_network = True
        summary = run(args)
        self.assertIn("exactly one of", summary["failure_reason"])


class DailyAcquisitionTests(ChildHarness):
    def setUp(self) -> None:
        super().setUp()
        self.daily = self.write_record(DAILY_BUYBACK_TAPE_OPERATION, status="approved")
        self.grid = json.loads((FIXTURES / "next-day-disclosure-00700-20260909.json").read_text(
            "utf-8"))["documents"][0]["grid"]

    def network_args(self, ticker: str = "00700", day: str = "2026-09-09") -> argparse.Namespace:
        args = self.namespace(NEXT_DAY_DISCLOSURE_OPERATION, hk_ticker=ticker,
                              as_of=day)
        args.fixture_file = None
        args.allow_network = True
        args.daily_buyback_tape_governance = str(self.daily)
        args.summary_dir = str(self.root / f"summary-{ticker}-{day}")
        return args

    def test_concurrent_company_views_fetch_once(self) -> None:
        calls = []
        def fake_fetch(url: str, *, operation: str, timeout: float = 60.0):
            calls.append(url)
            return b"one-market-workbook", "application/vnd.ms-excel"
        with mock.patch("dalton_core.hkex_filings_cli.fetch", side_effect=fake_fetch), \
             mock.patch("dalton_core.hkex_filings_cli._workbook_grid", return_value=self.grid), \
             concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda ticker: run(self.network_args(ticker)),
                                    ("00700", "00001")))
        self.assertEqual(len(calls), 1)
        self.assertTrue(all(item["status"] == "succeeded" for item in results))
        self.assertEqual(len({item["invocation_ref"] for item in results}), 1)

    def test_two_company_views_share_one_acquisition_across_restart(self) -> None:
        calls = []
        def fake_fetch(url: str, *, operation: str, timeout: float = 60.0):
            calls.append((url, operation))
            return b"one-market-workbook", "application/vnd.ms-excel"
        with mock.patch("dalton_core.hkex_filings_cli.fetch", side_effect=fake_fetch), \
             mock.patch("dalton_core.hkex_filings_cli._workbook_grid", return_value=self.grid):
            first = run(self.network_args("00700"))
            second = run(self.network_args("00001"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(second["status"], "succeeded")
        self.assertEqual(first["invocation_ref"], second["invocation_ref"])
        self.assertEqual(first["artifact"], second["artifact"])
        self.assertNotEqual(first["derived_view_ref"], second["derived_view_ref"])
        self.assertEqual(first["acquisition"]["cache_status"], "miss")
        self.assertEqual(second["acquisition"]["cache_status"], "hit")
        cache_dir = self.root / "hkex-daily-acquisitions" / first["acquisition"]["key"]
        self.assertEqual(cache_dir.stat().st_mode & 0o777, 0o700)
        self.assertEqual((cache_dir / "manifest.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((cache_dir / "source.xls").stat().st_mode & 0o777, 0o600)
        self.assertTrue(all(row["stock_code"] == "00700" for row in first["wire"]["rows"]))
        self.assertTrue(all(row["stock_code"] == "00001" for row in second["wire"]["rows"]))

    def test_cache_corruption_fails_closed_without_refetch(self) -> None:
        with mock.patch("dalton_core.hkex_filings_cli.fetch",
                        return_value=(b"workbook", "application/vnd.ms-excel")) as fetcher, \
             mock.patch("dalton_core.hkex_filings_cli._workbook_grid", return_value=self.grid):
            first = run(self.network_args())
            key = first["acquisition"]["key"]
            (self.root / "hkex-daily-acquisitions" / key / "source.xls").write_bytes(b"bad")
            second = run(self.network_args("00001"))
        self.assertEqual(fetcher.call_count, 1)
        self.assertEqual(second["status"], "failed")
        self.assertIn("corrupt", second["failure_reason"])
        self.assertIsNone(second["wire"])

    def test_manifest_invocation_and_output_tampering_fail_closed(self) -> None:
        with mock.patch("dalton_core.hkex_filings_cli.fetch",
                        return_value=(b"workbook", "application/vnd.ms-excel")), \
             mock.patch("dalton_core.hkex_filings_cli._workbook_grid", return_value=self.grid):
            first = run(self.network_args())
        manifest_path = (self.root / "hkex-daily-acquisitions" /
                         first["acquisition"]["key"] / "manifest.json")
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["invocation_ref"] = "connector-invocation:hkex-filings:" + "0" * 32
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with mock.patch("dalton_core.hkex_filings_cli.fetch") as fetcher:
            refused = run(self.network_args("00001"))
        self.assertEqual(refused["status"], "failed")
        self.assertIn("invocation is corrupt", refused["failure_reason"])
        fetcher.assert_not_called()

    def test_unapproved_acquisition_cannot_use_an_existing_cache(self) -> None:
        with mock.patch("dalton_core.hkex_filings_cli.fetch",
                        return_value=(b"workbook", "application/vnd.ms-excel")), \
             mock.patch("dalton_core.hkex_filings_cli._workbook_grid", return_value=self.grid):
            self.assertEqual(run(self.network_args())["status"], "succeeded")
        self.write_record(DAILY_BUYBACK_TAPE_OPERATION, status="proposed")
        with mock.patch("dalton_core.hkex_filings_cli.fetch") as fetcher:
            refused = run(self.network_args("00001"))
        self.assertEqual(refused["status"], "failed")
        self.assertIn("not approved", refused["failure_reason"])
        fetcher.assert_not_called()

    def test_day_and_governance_identity_partition_the_cache(self) -> None:
        with mock.patch("dalton_core.hkex_filings_cli.fetch",
                        return_value=(b"workbook", "application/vnd.ms-excel")) as fetcher, \
             mock.patch("dalton_core.hkex_filings_cli._workbook_grid", return_value=self.grid):
            one = run(self.network_args(day="2026-09-09"))
            two = run(self.network_args(day="2026-09-10"))
            self.daily.write_text(json.dumps(build_hkex_filings_governance_record(
                operation=DAILY_BUYBACK_TAPE_OPERATION, approved_by="human:lumos",
                status="approved", version=2)), encoding="utf-8")
            three = run(self.network_args(day="2026-09-09"))
        self.assertEqual(fetcher.call_count, 3)
        self.assertEqual(len({one["acquisition"]["key"], two["acquisition"]["key"],
                              three["acquisition"]["key"]}), 3)


class ArtifactTests(ChildHarness):
    def test_the_clock_is_not_part_of_what_the_document_said(self) -> None:
        # With the clock inside the hashed bytes, every run of an unchanged day
        # minted a new artifact hash and so a new invocation ref, and telling
        # "the same fact read twice" from "two different facts" is the entire
        # job of an invocation ref.
        payload = json.loads(
            (FIXTURES / "next-day-disclosure-00700-20260909.json").read_text("utf-8")
        )
        for field in CLOCK_FIELDS:
            self.assertIn(field, payload)
            self.assertNotIn(field, artifact_payload(payload))

    def test_the_same_capture_read_twice_is_one_invocation(self) -> None:
        first = run(self.namespace(NEXT_DAY_DISCLOSURE_OPERATION))
        second = run(self.namespace(NEXT_DAY_DISCLOSURE_OPERATION))
        self.assertEqual(first["artifact"]["content_hash"],
                         second["artifact"]["content_hash"])
        self.assertEqual(first["invocation_ref"], second["invocation_ref"])

    def test_a_later_reading_of_the_same_day_is_still_one_invocation(self) -> None:
        payload = json.loads(
            (FIXTURES / "next-day-disclosure-00700-20260909.json").read_text("utf-8")
        )
        later = dict(payload, captured_at="2027-01-01T00:00:00.000000+00:00",
                     observed_on="2027-01-01")
        path = self.root / "later.json"
        path.write_text(json.dumps(later), encoding="utf-8")
        first = run(self.namespace(NEXT_DAY_DISCLOSURE_OPERATION))
        second = run(self.namespace(NEXT_DAY_DISCLOSURE_OPERATION,
                                    fixture_file=path))
        self.assertEqual(first["invocation_ref"], second["invocation_ref"])

    def test_a_changed_figure_is_a_different_invocation(self) -> None:
        payload = json.loads(
            (FIXTURES / "next-day-disclosure-00700-20260909.json").read_text("utf-8")
        )
        grid = payload["documents"][0]["grid"]
        for row in grid:
            if len(row) > 4 and row[1] == "700":
                row[4] = "231,000"
        path = self.root / "moved.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        first = run(self.namespace(NEXT_DAY_DISCLOSURE_OPERATION))
        second = run(self.namespace(NEXT_DAY_DISCLOSURE_OPERATION,
                                    fixture_file=path))
        self.assertNotEqual(first["artifact"]["content_hash"],
                            second["artifact"]["content_hash"])
        self.assertNotEqual(first["invocation_ref"], second["invocation_ref"])

    def test_the_summary_names_every_document_that_was_read(self) -> None:
        summary = run(self.namespace(DISCLOSURE_OF_INTERESTS_OPERATION))
        roles = {item["role"] for item in summary["source_documents"]}
        self.assertIn("di_corp_list", roles)
        self.assertIn("di_form_list", roles)
        self.assertTrue(any(role.startswith("di_form:") for role in roles))

    def test_a_failed_run_still_writes_a_summary(self) -> None:
        self.write_record(NEXT_DAY_DISCLOSURE_OPERATION, status="proposed")
        run(self.namespace(NEXT_DAY_DISCLOSURE_OPERATION))
        summary = json.loads(
            (self.root / NEXT_DAY_DISCLOSURE_OPERATION / "summary.json")
            .read_text(encoding="utf-8")
        )
        self.assertEqual(summary["status"], "failed")
        self.assertTrue(summary["failure_reason"])

    def test_the_summary_carries_the_hosts_and_the_caliber_notes(self) -> None:
        summary = run(self.namespace(NEXT_DAY_DISCLOSURE_OPERATION))
        self.assertEqual(summary["allowed_hosts"], ["www3.hkexnews.hk"])
        self.assertTrue(summary["caliber_notes"])


class EventEmissionTests(ChildHarness):
    def test_the_buyback_run_emits_one_event_with_its_context(self) -> None:
        summary = run(self.namespace(NEXT_DAY_DISCLOSURE_OPERATION))
        self.assertEqual(summary["event_count"], 1)
        event = summary["events"][0]
        self.assertEqual(event["kind"], "buyback_disclosure")
        self.assertEqual(event["evidence_tier"], "primary_filing")
        self.assertEqual(event["payload"]["invocation_ref"],
                         summary["invocation_ref"])
        self.assertEqual(event["payload"]["artifact_hash"],
                         summary["artifact"]["content_hash"])
        self.assertEqual(event["context"]["cluster_key"], "2026-W37")

    def test_a_handed_in_price_reaches_the_context(self) -> None:
        summary = run(self.namespace(NEXT_DAY_DISCLOSURE_OPERATION,
                                     current_price="400.00"))
        self.assertEqual(
            summary["events"][0]["context"]["price_vs_current"]["status"],
            "compared",
        )

    def test_the_index_operations_emit_nothing_and_count_the_records(self) -> None:
        summary = run(self.namespace(ANNOUNCEMENTS_INDEX_OPERATION))
        self.assertEqual(summary["event_count"], 0)
        self.assertEqual(summary["record_count"], 24)
        self.assertEqual(summary["parsed_row_count"], 24)

    def test_the_universe_count_travels_on_the_buyback_summary(self) -> None:
        summary = run(self.namespace(NEXT_DAY_DISCLOSURE_OPERATION))
        self.assertGreater(summary["universe_row_count"], 100)

    def test_every_event_would_be_taken_by_the_ledger(self) -> None:
        from dalton_core.research_event import validate_payload

        for operation in (NEXT_DAY_DISCLOSURE_OPERATION,
                          DISCLOSURE_OF_INTERESTS_OPERATION):
            summary = run(self.namespace(operation))
            for event in summary["events"]:
                validate_payload(event["kind"], event["payload"])


class HostTests(ChildHarness):
    def test_a_url_off_the_declared_hosts_is_refused_before_the_request(self) -> None:
        # A redirect is how a declared host turns into an undeclared one, and
        # www.hkexnews.hk really does redirect the buy-back report path.
        with self.assertRaises(HkexFilingsRunError) as caught:
            fetch("https://www.hkexnews.hk/sharerepur/documents/srrpt20260909.xls",
                  operation=NEXT_DAY_DISCLOSURE_OPERATION)
        self.assertIn("www.hkexnews.hk", str(caught.exception))

    def test_the_buyback_operation_may_not_reach_the_search_host(self) -> None:
        with self.assertRaises(HkexFilingsRunError):
            fetch("https://www1.hkexnews.hk/search/prefix.do",
                  operation=NEXT_DAY_DISCLOSURE_OPERATION)


class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.governance = self.root / "connector-governance"
        self.governance.mkdir(parents=True, exist_ok=True)

    def seed(self, *operations: str) -> HkexFilingsLauncher:
        for operation in operations:
            record = build_hkex_filings_governance_record(
                operation=operation, approved_by="human:lumos"
            )
            (self.governance / GOVERNANCE_FILENAME_BY_OPERATION[operation]).write_text(
                json.dumps(record), encoding="utf-8"
            )
        return HkexFilingsLauncher(state_dir=self.root,
                                   governance_dir=self.governance)

    def test_a_core_runs_only_the_operations_it_holds_a_record_for(self) -> None:
        launcher = self.seed(NEXT_DAY_DISCLOSURE_OPERATION)
        self.assertEqual(launcher.approved_operations(),
                         (NEXT_DAY_DISCLOSURE_OPERATION,))
        self.assertTrue(launcher.configured)

    def test_a_core_with_no_records_is_not_configured(self) -> None:
        launcher = HkexFilingsLauncher(state_dir=self.root, governance_dir=None)
        self.assertEqual(launcher.approved_operations(), ())
        self.assertFalse(launcher.configured)

    def test_an_unapproved_operation_is_refused_before_a_process_starts(self) -> None:
        launcher = self.seed(NEXT_DAY_DISCLOSURE_OPERATION)
        with self.assertRaises(LaneChildRejected):
            launcher.start(operation=DISCLOSURE_OF_INTERESTS_OPERATION,
                           hk_ticker="00700", company_ref=COMPANY,
                           since="2026-01-01", until="2026-09-10")

    def test_a_windowed_operation_without_a_window_is_refused(self) -> None:
        launcher = self.seed(ANNOUNCEMENTS_INDEX_OPERATION)
        with self.assertRaises(LaneChildRejected):
            launcher.start(operation=ANNOUNCEMENTS_INDEX_OPERATION,
                           hk_ticker="00700", company_ref=COMPANY)

    def test_the_tape_without_a_day_is_refused(self) -> None:
        launcher = self.seed(NEXT_DAY_DISCLOSURE_OPERATION)
        with self.assertRaises(LaneChildRejected):
            launcher.start(operation=NEXT_DAY_DISCLOSURE_OPERATION,
                           hk_ticker="00700", company_ref=COMPANY)

    def test_the_command_carries_the_record_for_that_exact_operation(self) -> None:
        launcher = self.seed(NEXT_DAY_DISCLOSURE_OPERATION)
        command = launcher._command(
            ticket_dir=self.root, operation=NEXT_DAY_DISCLOSURE_OPERATION,
            hk_ticker="00700", company_ref=COMPANY, as_of="2026-09-09",
            since=None, until=None, headline_category=None,
            prior_rows_file=None, current_price=None,
        )
        self.assertIn("--allow-network", command)
        self.assertIn(str((self.governance / GOVERNANCE_FILENAME_BY_OPERATION[
            NEXT_DAY_DISCLOSURE_OPERATION]).resolve()), command)
        self.assertNotIn("--since", command)


class FakeLauncher:
    def __init__(self, operations: tuple[str, ...]) -> None:
        self.operations = operations
        self.state_dir = Path(tempfile.mkdtemp())
        self.started: list[dict[str, object]] = []
        self.tickets: dict[str, dict[str, object]] = {}

    def approved_operations(self) -> tuple[str, ...]:
        return self.operations

    def start(self, **kwargs: object) -> dict[str, object]:
        ticket = {"id": f"t{len(self.started)}", **kwargs}
        self.started.append(dict(kwargs))
        self.tickets[str(ticket["id"])] = {
            "status": "succeeded", "summary": {}, **kwargs,
        }
        return ticket

    def status(self, ticket_ref: str) -> dict[str, object]:
        return self.tickets[ticket_ref]


class LaneTests(unittest.TestCase):
    MISSION = {
        "id": "mission:v1",
        "autonomy": {"may_write": ["observation", "market_event"]},
        "universe": [
            {"company_ref": COMPANY, "ticker": "0700.HK", "bootstrap_priority": "P1"},
            {"company_ref": "company:sec-cik:1467373", "ticker": "ACN"},
        ],
    }

    def clock(self) -> datetime:
        return datetime(2026, 9, 10, 13, 0, tzinfo=timezone.utc)

    def coordinator(self, *, mission=None, operations=OPERATIONS) -> tuple:
        launcher = FakeLauncher(tuple(operations))
        return MissionHkexLaneCoordinator(
            state_dir=launcher.state_dir, launcher=launcher,
            mission=lambda: self.MISSION if mission is None else mission,
            clock=self.clock,
        ), launcher

    def test_only_hong_kong_names_are_in_this_lanes_universe(self) -> None:
        # There is no route from a US ticker to a Hong Kong stock code that
        # does not involve asking somebody.
        rows = hk_universe(self.MISSION)
        self.assertEqual([row["hk_ticker"] for row in rows], ["00700"])

    def test_a_mission_with_no_hong_kong_name_is_idle_with_the_reason(self) -> None:
        # Which is every mission today, and is correct rather than a gap.
        coordinator, launcher = self.coordinator(mission={
            "id": "m", "autonomy": {"may_write": ["observation", "market_event"]},
            "universe": [{"company_ref": "company:sec-cik:1467373"}],
        })
        result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "idle")
        self.assertIn("owner decision", result["reason"])
        self.assertEqual(launcher.started, [])

    def test_both_grants_are_needed(self) -> None:
        self.assertEqual(missing_scopes(None), ["observation", "market_event"])
        self.assertEqual(
            missing_scopes({"autonomy": {"may_write": ["observation"]}}),
            ["market_event"],
        )
        coordinator, launcher = self.coordinator(mission={
            "id": "m", "autonomy": {"may_write": ["observation"]},
            "universe": [{"company_ref": COMPANY}],
        })
        result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "ungranted")
        self.assertEqual(launcher.started, [])

    def test_the_daily_tape_is_read_first(self) -> None:
        coordinator, launcher = self.coordinator()
        result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["operation"], NEXT_DAY_DISCLOSURE_OPERATION)
        self.assertEqual(launcher.started[0]["as_of"], "2026-09-10")

    def test_the_four_operations_take_turns_and_stop_for_the_day(self) -> None:
        coordinator, launcher = self.coordinator()
        seen = []
        for _ in range(6):
            result = coordinator.dispatch_once()
            if result["status"] == "launched":
                seen.append(result["operation"])
        # The order the lane plans in, not the order the operations are
        # declared in: the tape first because it is the only one that is stale
        # by tomorrow, then the index, then the notices, then the month.
        self.assertEqual(seen, [
            NEXT_DAY_DISCLOSURE_OPERATION, ANNOUNCEMENTS_INDEX_OPERATION,
            DISCLOSURE_OF_INTERESTS_OPERATION, MONTHLY_RETURNS_OPERATION,
        ])
        self.assertEqual(coordinator.dispatch_once()["status"], "idle")

    def test_an_unapproved_operation_is_never_planned(self) -> None:
        coordinator, launcher = self.coordinator(
            operations=(DISCLOSURE_OF_INTERESTS_OPERATION,)
        )
        result = coordinator.dispatch_once()
        self.assertEqual(result["operation"], DISCLOSURE_OF_INTERESTS_OPERATION)
        self.assertEqual(launcher.started[0]["since"], "2026-08-27")

    def test_a_core_with_no_record_is_unconfigured_rather_than_idle(self) -> None:
        coordinator, _launcher = self.coordinator(operations=())
        self.assertEqual(coordinator.dispatch_once()["status"], "unconfigured")

    def test_a_failed_child_does_not_mark_the_day_read(self) -> None:
        coordinator, launcher = self.coordinator(
            operations=(NEXT_DAY_DISCLOSURE_OPERATION,)
        )
        coordinator.dispatch_once()
        launcher.tickets["t0"].update({"status": "failed",
                                       "summary": {"failure_reason": "boom"}})
        settled = coordinator.dispatch_once()
        self.assertEqual(settled["settled"]["status"], "failed")
        # Re-planned rather than lost: a failed run learned nothing.
        self.assertEqual(settled["status"], "launched")

    def test_a_company_that_keeps_failing_is_held_rather_than_hammered(self) -> None:
        coordinator, launcher = self.coordinator(
            operations=(NEXT_DAY_DISCLOSURE_OPERATION,)
        )
        for index in range(4):
            result = coordinator.dispatch_once()
            if result["status"] == "launched":
                launcher.tickets[result["ticket_ref"]].update(
                    {"status": "failed", "summary": {"failure_reason": "boom"}}
                )
        final = coordinator.dispatch_once()
        self.assertEqual(final["status"], "idle")
        self.assertEqual(final["skipped"][0]["reason"], "held")

    def test_content_refusal_is_terminal_for_only_that_company_operation_day(self) -> None:
        coordinator, launcher = self.coordinator(
            operations=(NEXT_DAY_DISCLOSURE_OPERATION, ANNOUNCEMENTS_INDEX_OPERATION))
        launched = coordinator.dispatch_once()
        launcher.tickets[launched["ticket_ref"]].update({
            "status": "failed",
            "summary": {"failure_reason": "HkexFilingsParseError: content_refused"},
        })
        result = coordinator.dispatch_once()
        self.assertEqual(result["settled"]["failure"]["failure_class"],
                         "content_refused")
        self.assertEqual(result["operation"], ANNOUNCEMENTS_INDEX_OPERATION)

    def test_dependency_failure_survives_restart_and_admits_a_real_probe(self) -> None:
        coordinator, launcher = self.coordinator(
            operations=(NEXT_DAY_DISCLOSURE_OPERATION,))
        launched = coordinator.dispatch_once()
        launcher.tickets[launched["ticket_ref"]].update({
            "status": "failed",
            "summary": {"failure_reason": "ConnectionError: transport_unavailable"},
        })
        coordinator.dispatch_once()
        restarted = MissionHkexLaneCoordinator(
            state_dir=launcher.state_dir, launcher=launcher,
            mission=lambda: self.MISSION, clock=self.clock)
        probe = restarted.dispatch_once()
        self.assertEqual(probe["status"], "launched")
        self.assertEqual(probe["company_ref"], COMPANY)

    def test_real_governance_file_change_creates_a_recoverable_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            governance = root / "governance"
            governance.mkdir()
            path = governance / GOVERNANCE_FILENAME_BY_OPERATION[
                NEXT_DAY_DISCLOSURE_OPERATION]
            proposed = build_hkex_filings_governance_record(
                operation=NEXT_DAY_DISCLOSURE_OPERATION,
                approved_by="human:lumos", status="proposed")
            path.write_text(json.dumps(proposed), encoding="utf-8")
            launcher = HkexFilingsLauncher(state_dir=root, governance_dir=governance)
            coordinator = MissionHkexLaneCoordinator(
                state_dir=root, launcher=launcher, mission=lambda: self.MISSION,
                clock=self.clock)
            parameters = {"as_of": "2026-09-10"}
            old = coordinator._item_key(
                COMPANY, NEXT_DAY_DISCLOSURE_OPERATION, parameters)
            approved = build_hkex_filings_governance_record(
                operation=NEXT_DAY_DISCLOSURE_OPERATION,
                approved_by="human:lumos", status="approved")
            path.write_text(json.dumps(approved), encoding="utf-8")
            new = coordinator._item_key(
                COMPANY, NEXT_DAY_DISCLOSURE_OPERATION, parameters)
            self.assertNotEqual(old, new)

    def test_what_was_read_becomes_the_history_the_context_is_computed_from(self) -> None:
        coordinator, launcher = self.coordinator(
            operations=(NEXT_DAY_DISCLOSURE_OPERATION,)
        )
        coordinator.dispatch_once()
        launcher.tickets["t0"].update({
            "status": "succeeded",
            "summary": {
                "wire": {"rows": [{"record_hash": "a" * 64,
                                   "trading_date": "2026-09-09",
                                   "shares_repurchased": "230,000"}]},
                "events": [],
            },
        })
        coordinator.dispatch_once()
        held = coordinator.history[(COMPANY, NEXT_DAY_DISCLOSURE_OPERATION)]
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["shares_repurchased"], "230,000")

    def test_an_event_is_recorded_once_however_often_it_is_read(self) -> None:
        recorded: list[dict[str, object]] = []
        launcher = FakeLauncher((NEXT_DAY_DISCLOSURE_OPERATION,))
        coordinator = MissionHkexLaneCoordinator(
            state_dir=launcher.state_dir, launcher=launcher,
            mission=lambda: self.MISSION, clock=self.clock,
            record_event=lambda **kwargs: recorded.append(kwargs),
        )
        event = {"kind": "buyback_disclosure", "company_ref": COMPANY,
                 "occurred_at": "2026-09-09T00:00:00+00:00",
                 "payload": {"event_key": "k"}, "source_refs": []}
        first = coordinator._emit(COMPANY, [event])
        second = coordinator._emit(COMPANY, [event])
        self.assertEqual(first["status"], "recorded")
        self.assertEqual(second["recorded_count"], 0)
        self.assertEqual(len(recorded), 1)

    def test_an_unwired_ledger_says_so_rather_than_pretending(self) -> None:
        coordinator, _launcher = self.coordinator()
        result = coordinator._emit(COMPANY, [
            {"kind": "buyback_disclosure", "occurred_at": "2026-09-09T00:00:00+00:00",
             "payload": {"event_key": "k"}}
        ])
        self.assertEqual(result["status"], "events_unwired")
        self.assertIn("nothing recorded them", result["reason"])


class RegistrationTests(unittest.TestCase):
    def test_the_lane_is_in_the_registry_with_a_name_the_owner_reads(self) -> None:
        from dalton_core.cockpit_plane import (
            LANES_SHOWN_ELSEWHERE, REGISTRY_LANE_LABELS,
        )
        from dalton_core.lane_registry import LANE_MODULES, registered_lanes

        self.assertIn("dalton_core.mission_hkex_lane", LANE_MODULES)
        lane = next(spec for spec in registered_lanes()
                    if spec.operation == "dispatch_mission_hkex_filings")
        self.assertEqual(lane.driver_key, "mission_hkex_filings")
        self.assertEqual(lane.init_kwarg, LAUNCHER_KWARG)
        self.assertNotIn(lane.driver_key, LANES_SHOWN_ELSEWHERE)
        self.assertTrue(REGISTRY_LANE_LABELS[lane.driver_key].strip())

    def test_the_plist_turns_the_lane_on_only_where_a_record_is_seeded(self) -> None:
        from dalton_core.lane_registry import LaunchAgentContext

        root = Path(tempfile.mkdtemp())
        context = LaunchAgentContext(state=root)
        self.assertEqual(argv_fragment(context), [])
        governance = root / "connector-governance"
        governance.mkdir()
        (governance / GOVERNANCE_FILENAME_BY_OPERATION[
            NEXT_DAY_DISCLOSURE_OPERATION]).write_text("{}", encoding="utf-8")
        self.assertEqual(
            argv_fragment(context),
            ["--hkex-filings-governance-dir", str(governance)],
        )

    def test_the_installer_seeds_all_four_records_or_none(self) -> None:
        from scripts.rehearse_deploy import (
            committed_governance_records,
            deliberately_unseeded_records,
            install_seeded_records,
        )

        repo = Path(__file__).resolve().parents[1]
        install = repo / "deploy" / "macos" / "install.sh"
        committed = committed_governance_records(repo)
        seeded = install_seeded_records(install)
        unseeded = deliberately_unseeded_records(install)
        for operation in OPERATIONS:
            name = f"{KIND_BY_OPERATION[operation]}-v1.json"
            self.assertIn(name, committed)
            self.assertIn(name, seeded, name)
            self.assertNotIn(name, unseeded)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

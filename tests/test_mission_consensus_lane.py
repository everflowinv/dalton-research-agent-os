"""P11b: the lane asks the vendor once a day and reads one broker note a tick.

The behaviours worth pinning are the ones that fail silently: a mission that
never granted the scope must produce no child and no scan forever, a company
with no filings must be skipped rather than published against a guessed fiscal
calendar, and a note already read must never be read again.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.consensus_estimate import ConsensusEstimateAuthority
from dalton_core.lane_child_launcher import LaneChildRejected
from dalton_core.mission_consensus_lane import (
    LANE,
    _fiscal_calendar_reader,
    LAUNCHER_KWARG,
    REFRESH_SECONDS,
    WRITE_SCOPE,
    MissionConsensusLaneCoordinator,
    argv_fragment,
    build_launcher,
    may_write_consensus,
)
from dalton_core.store import DaltonStore
from dalton_core.street_estimate import StreetEstimateStore

from tests.test_consensus_estimate import (
    ACN_CALENDAR, ARTIFACT, CAPTURED, GOVERNANCE, GOVERNANCE_HASH, INVOCATION,
    wire,
)
from tests.test_street_estimate_extraction import MASTHEAD, context as page_context

ACN = "company:acn"
EPAM = "company:epam"


def mission(scopes=(WRITE_SCOPE,), universe=None):
    return {
        "id": "coverage-mission-version:1",
        "universe": universe if universe is not None else [
            {"company_ref": ACN, "ticker": "ACN", "bootstrap_priority": "P0"},
            {"company_ref": EPAM, "ticker": "EPAM", "bootstrap_priority": "P1"},
        ],
        "autonomy": {"may_write": list(scopes)},
    }


class FakeLauncher:
    def __init__(self, reject=None):
        self.started: list[dict] = []
        self.tickets: dict[str, dict] = {}
        self.reject = reject

    def start(self, **kwargs):
        if self.reject is not None:
            raise LaneChildRejected(self.reject)
        self.started.append(kwargs)
        ticket_id = f"ticket:{len(self.started)}"
        self.tickets[ticket_id] = {
            "id": ticket_id, "status": "running",
            "company_ref": kwargs["company_ref"], "summary": {},
        }
        return self.tickets[ticket_id]

    def finish(self, ticket_id, *, status="succeeded", **summary):
        self.tickets[ticket_id]["status"] = status
        self.tickets[ticket_id]["summary"] = summary

    def status(self, ticket_ref):
        return self.tickets[ticket_ref]


class LaneTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = DaltonStore(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.authority = ConsensusEstimateAuthority(self.store)
        self.estimates = StreetEstimateStore(self.store)
        self.launcher = FakeLauncher()
        self.now = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
        self.mission_value = mission()
        self.calendars = {ACN: dict(ACN_CALENDAR), EPAM: dict(ACN_CALENDAR)}
        self.pages: dict[str, list[dict]] = {}

    def coordinator(self):
        return MissionConsensusLaneCoordinator(
            authority=self.authority,
            street_store=self.estimates,
            launcher=self.launcher,
            mission=lambda: self.mission_value,
            fiscal_calendar_for=lambda ref: self.calendars.get(ref),
            next_context=self._next_context,
            clock=lambda: self.now,
        )

    def _next_context(self, company_ref, scanned):
        for candidate in self.pages.get(company_ref, []):
            if candidate["document_ref"] not in scanned:
                return candidate
        return None

    def note(self, document_ref, *, company_ref=ACN, text=MASTHEAD, **overrides):
        value = page_context(text, **overrides)
        value["company_ref"] = company_ref
        value["document_ref"] = document_ref
        self.pages.setdefault(company_ref, []).append(value)
        return value


class GrantTests(LaneTestCase):
    def test_a_mission_that_never_granted_the_scope_does_nothing(self):
        self.mission_value = mission(scopes=("market_price",))
        self.note("alphaengine-doc:1")
        result = self.coordinator().dispatch_once()
        self.assertEqual(result["status"], "ungranted")
        self.assertIn(WRITE_SCOPE, result["reason"])
        self.assertEqual(self.launcher.started, [])
        # And nothing was read either: the grant covers both routes.
        self.assertEqual(self.estimates.scanned_document_refs(ACN), set())

    def test_no_mission_is_unconfigured_rather_than_a_failure(self):
        self.mission_value = None
        self.assertEqual(self.coordinator().dispatch_once()["status"], "unconfigured")

    def test_the_scope_vocabulary_is_the_one_the_mission_uses(self):
        self.assertTrue(may_write_consensus(mission()))
        self.assertFalse(may_write_consensus(mission(scopes=())))
        self.assertFalse(may_write_consensus({"autonomy": {"may_write": "all"}}))
        self.assertFalse(may_write_consensus(None))


class FetchTests(LaneTestCase):
    def test_the_first_tick_launches_the_highest_priority_company(self):
        result = self.coordinator().dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["fetch"]["company_ref"], ACN)
        self.assertEqual(self.launcher.started[0]["ticker"], "ACN")
        self.assertEqual(self.launcher.started[0]["fiscal_year_end"], "08-31")
        self.assertEqual(
            self.launcher.started[0]["last_reported_period_end"], "2026-05-31"
        )

    def test_a_company_with_no_filings_is_skipped_with_that_reason(self):
        # Yahoo's 0q is relative to a calendar it does not publish. Without the
        # company's own filings there is nothing to place it on.
        self.calendars.pop(ACN)
        result = self.coordinator().dispatch_once()
        self.assertEqual(result["fetch"]["company_ref"], EPAM)
        skipped = {row["company_ref"]: row["reason"] for row in result["skipped"]}
        self.assertEqual(skipped[ACN], "fiscal_calendar_unknown")

    def test_a_company_with_no_ticker_is_not_guessed_at(self):
        self.mission_value = mission(universe=[{"company_ref": ACN}])
        result = self.coordinator().dispatch_once()
        self.assertEqual(result["fetch"]["status"], "idle")
        self.assertEqual(self.launcher.started, [])

    def test_a_refreshed_company_is_left_alone_for_the_day(self):
        lane = self.coordinator()
        lane.dispatch_once()
        self.launcher.finish("ticket:1", consensus_status="duplicate")
        second = lane.dispatch_once()
        self.assertEqual(second["fetch"]["company_ref"], EPAM)
        self.launcher.finish("ticket:2", consensus_status="fresh")
        third = lane.dispatch_once()
        self.assertEqual(third["fetch"]["status"], "idle")
        reasons = {row["reason"] for row in third["skipped"]}
        self.assertEqual(reasons, {"recently_refreshed"})

    def test_the_hold_expires_after_a_day(self):
        lane = self.coordinator()
        lane.dispatch_once()
        self.launcher.finish("ticket:1", consensus_status="duplicate")
        lane.dispatch_once()
        self.launcher.finish("ticket:2", consensus_status="duplicate")
        self.now = self.now + timedelta(seconds=REFRESH_SECONDS + 60)
        again = lane.dispatch_once()
        self.assertEqual(again["fetch"]["company_ref"], ACN)

    def test_a_running_child_holds_the_slot(self):
        lane = self.coordinator()
        lane.dispatch_once()
        second = lane.dispatch_once()
        self.assertEqual(second["fetch"]["status"], "busy")
        self.assertEqual(len(self.launcher.started), 1)

    def test_a_company_whose_runs_keep_failing_gives_up_the_slot(self):
        lane = self.coordinator()
        for index in range(1, 4):
            lane.dispatch_once()
            self.launcher.finish(f"ticket:{index}", status="failed",
                                 failure_reason="the vendor dropped every block")
            self.assertEqual(self.launcher.started[-1]["company_ref"], ACN)
        result = lane.dispatch_once()
        self.assertEqual(result["fetch"]["company_ref"], EPAM)
        held = {row["company_ref"]: row for row in result["skipped"]}
        self.assertEqual(held[ACN]["reason"], "held")
        self.assertIn("dropped every block", held[ACN]["detail"])

    def test_a_rejected_launch_is_reported_rather_than_raised(self):
        self.launcher.reject = "a consensus run needs an approved record"
        result = self.coordinator().dispatch_once()
        self.assertEqual(result["fetch"]["status"], "rejected")
        self.assertIn("approved record", result["fetch"]["reason"])


class ScanTests(LaneTestCase):
    def test_one_note_a_tick_is_read_and_recorded(self):
        self.note("alphaengine-doc:1")
        result = self.coordinator().dispatch_once()
        scan = result["scan"]
        self.assertEqual(scan["outcome"], "recorded")
        self.assertEqual(scan["broker"], "td")
        held = self.estimates.estimates(ACN)
        self.assertEqual(len(held), 1)
        self.assertEqual(held[0]["target_price"]["value"], "173.00")

    def test_a_note_already_read_is_not_read_again(self):
        self.note("alphaengine-doc:1")
        lane = self.coordinator()
        lane.dispatch_once()
        second = lane.dispatch_once()
        # One document, one company with pages: the second tick finds nothing
        # left to read for ACN and nothing at all for EPAM.
        self.assertIsNone(second["scan"])
        self.assertEqual(len(self.estimates.estimates(ACN)), 1)

    def test_a_refused_note_is_recorded_as_scanned_with_its_reason(self):
        self.note("alphaengine-doc:1",
                  document_companies=["Accenture PLC", "Cognizant"])
        result = self.coordinator().dispatch_once()
        self.assertEqual(result["scan"]["outcome"], "refused")
        self.assertEqual(result["scan"]["reason"], "multi_company_report")
        # Recorded, so the lane does not spend every tick refusing it again.
        self.assertEqual(
            self.estimates.scanned_document_refs(ACN), {"alphaengine-doc:1"}
        )
        self.assertEqual(self.estimates.estimates(ACN), [])

    def test_the_second_house_turns_one_opinion_into_a_range(self):
        # A vendor observation first, because a range attaches to a chain.
        self.authority.publish_consensus(
            company_ref=ACN, wire=wire(), **ACN_CALENDAR,
            invocation_ref=INVOCATION, artifact_hash=ARTIFACT,
            governance_ref=GOVERNANCE, governance_hash=GOVERNANCE_HASH,
            captured_at=CAPTURED,
        )
        self.note("alphaengine-doc:1", sources=["TD Securities (USA) LLC"])
        self.note("alphaengine-doc:2", sources=["Wells Fargo Securities LLC"],
                  text=MASTHEAD.replace("$173.00", "$194.00"))
        lane = self.coordinator()
        first = lane.dispatch_once()
        self.assertIsNone(first["scan"]["report_consensus"])
        second = lane.dispatch_once()
        block = second["scan"]["report_consensus"]
        self.assertEqual(block["status"], "fresh")
        self.assertEqual(block["broker_count"], 2)
        self.assertEqual(sorted(block["brokers"]), ["td", "wells-fargo"])
        latest = self.authority.latest_consensus(ACN)
        self.assertEqual(latest["report_consensus"]["low"], "173.00")
        self.assertEqual(latest["report_consensus"]["high"], "194.00")

    def test_the_same_house_twice_never_attaches_a_range(self):
        self.authority.publish_consensus(
            company_ref=ACN, wire=wire(), **ACN_CALENDAR,
            invocation_ref=INVOCATION, artifact_hash=ARTIFACT,
            governance_ref=GOVERNANCE, governance_hash=GOVERNANCE_HASH,
            captured_at=CAPTURED,
        )
        self.note("alphaengine-doc:1", sources=["TD Securities (USA) LLC"])
        self.note("alphaengine-doc:2", sources=["TD Cowen"],
                  text=MASTHEAD.replace("$173.00", "$180.00"))
        lane = self.coordinator()
        lane.dispatch_once()
        second = lane.dispatch_once()
        self.assertIsNone(second["scan"]["report_consensus"])
        self.assertIsNone(self.authority.latest_consensus(ACN)["report_consensus"])

    def test_a_range_with_no_chain_to_attach_to_keeps_the_note(self):
        self.note("alphaengine-doc:1", sources=["TD Securities (USA) LLC"])
        self.note("alphaengine-doc:2", sources=["Wells Fargo Securities LLC"],
                  text=MASTHEAD.replace("$173.00", "$194.00"))
        lane = self.coordinator()
        lane.dispatch_once()
        second = lane.dispatch_once()
        self.assertEqual(second["scan"]["report_consensus"]["status"], "not_attached")
        # The estimate itself is stored regardless; only the range is deferred.
        self.assertEqual(len(self.estimates.estimates(ACN)), 2)

    def test_an_unreadable_document_does_not_end_the_tick(self):
        def explode(company_ref, scanned):
            raise RuntimeError("the manifest is gone")

        lane = self.coordinator()
        lane.next_context = explode
        result = lane.dispatch_once()
        self.assertEqual(result["scan"]["outcome"], "unreadable")
        self.assertIn("the manifest is gone", result["scan"]["reason"])
        self.assertEqual(result["status"], "launched")


class UngatedScanTests(LaneTestCase):
    """B(b). The report half reaches nothing and must not wait on a Yahoo approval."""

    def coordinator(self):
        lane = super().coordinator()
        lane.launcher = None
        return lane

    def test_a_core_without_the_vendor_connector_still_reads_its_notes(self):
        self.note("alphaengine-doc:1")
        result = self.coordinator().dispatch_once()
        self.assertEqual(result["fetch"]["status"], "unconfigured")
        self.assertEqual(result["scan"]["outcome"], "recorded")
        self.assertEqual(len(self.estimates.estimates(ACN)), 1)


class QuotaTests(LaneTestCase):
    """E. The child runs out of process, so the runner cannot see it spend."""

    def test_the_lane_stops_at_the_governed_daily_ceiling(self):
        from dalton_core.connector_quota_policy import governed_daily_quota

        limit = governed_daily_quota("yfinance", "analyst_estimates")["daily_unit_limit"]
        lane = self.coordinator()
        self.assertEqual(lane.daily_unit_limit, limit)
        for _ in range(limit):
            lane.dispatch_once()
            if lane._open:
                self.launcher.finish(lane._open, consensus_status="duplicate")
                lane._refreshed.clear()
        self.assertEqual(len(self.launcher.started), limit)
        result = lane.dispatch_once()
        self.assertEqual(result["fetch"]["status"], "quota_exhausted")
        self.assertIn(str(limit), result["fetch"]["reason"])

    def test_the_count_resets_with_the_day(self):
        lane = self.coordinator()
        lane._spent_on = self.now.date().isoformat()
        lane._spent = lane.daily_unit_limit
        self.assertEqual(lane.dispatch_once()["fetch"]["status"], "quota_exhausted")
        self.now = self.now + timedelta(days=1)
        self.assertEqual(lane.dispatch_once()["fetch"]["status"], "launched")


class RoundRobinTests(LaneTestCase):
    """F. One company must not keep the reader while another waits days."""

    def test_the_scan_moves_on_after_serving_a_company(self):
        for index in range(3):
            self.note(f"alphaengine-doc:acn{index}", company_ref=ACN)
            self.note(f"alphaengine-doc:epam{index}", company_ref=EPAM,
                      subject_names=["EPAM"],
                      document_companies=["EPAM Systems, Inc."])
        lane = self.coordinator()
        served = []
        for _ in range(4):
            scan = lane.dispatch_once()["scan"]
            served.append(scan["company_ref"])
        self.assertEqual(served, [ACN, EPAM, ACN, EPAM])

    def test_a_company_with_nothing_left_does_not_stall_the_others(self):
        self.note("alphaengine-doc:acn0", company_ref=ACN)
        self.note("alphaengine-doc:epam0", company_ref=EPAM,
                  subject_names=["EPAM"], document_companies=["EPAM Systems, Inc."])
        self.note("alphaengine-doc:epam1", company_ref=EPAM,
                  subject_names=["EPAM"], document_companies=["EPAM Systems, Inc."])
        lane = self.coordinator()
        served = [lane.dispatch_once()["scan"]["company_ref"] for _ in range(3)]
        self.assertEqual(served, [ACN, EPAM, EPAM])


class ScanLedgerTests(LaneTestCase):
    def test_a_refusal_records_the_reader_that_made_it(self):
        from dalton_core.street_estimate_extraction import EXTRACTOR_REF

        self.note("alphaengine-doc:1",
                  document_companies=["Accenture PLC", "Cognizant"])
        self.coordinator().dispatch_once()
        scan = self.estimates.scans(ACN)[0]
        self.assertEqual(scan["outcome"], "refused")
        self.assertEqual(scan["extractor_ref"], EXTRACTOR_REF)
        # And a later reader can ask for everything an older one refused.
        self.assertEqual(self.estimates.refused_by("extractor:next:0.2"),
                         {"alphaengine-doc:1"})


class FiscalCalendarReaderTests(unittest.TestCase):
    """A. The live Core holds thirty-three 10-Q rows and no 10-K at all."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = DaltonStore(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.store.connection.executescript(
            "CREATE TABLE IF NOT EXISTS coverage_mission_statement_filings ("
            "ingest_id TEXT PRIMARY KEY, company_ref TEXT, form TEXT, "
            "report_date TEXT);"
        )

        class Server:
            store = self.store

        self.read = _fiscal_calendar_reader(Server())

    def file(self, company_ref, form, report_date):
        self.store.connection.execute(
            "INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?)",
            (f"{company_ref}:{form}:{report_date}", company_ref, form, report_date),
        )

    def test_an_annual_report_is_the_direct_answer(self):
        self.file(ACN, "10-K", "2025-08-31")
        self.file(ACN, "10-Q", "2026-05-31")
        found = self.read(ACN)
        self.assertEqual(found["fiscal_year_end"], "08-31")
        self.assertEqual(found["last_reported_period_end"], "2026-05-31")
        self.assertEqual(found["fiscal_year_end_basis"], "annual_report")

    def test_the_quarters_answer_when_no_annual_has_been_ingested(self):
        # Accenture's live shape: eight 10-Q rows, no 10-K. Without this the
        # lane skips every covered company and route 2 never publishes.
        for report_date in ("2025-11-30", "2026-02-28", "2026-05-31"):
            self.file(ACN, "10-Q", report_date)
        found = self.read(ACN)
        self.assertEqual(found["fiscal_year_end"], "08-31")
        self.assertEqual(found["last_reported_period_end"], "2026-05-31")
        self.assertEqual(found["fiscal_year_end_basis"], "quarterly_grid")

    def test_a_december_filer_comes_out_of_the_same_arithmetic(self):
        for report_date in ("2026-03-31", "2026-06-30", "2026-09-30"):
            self.file(EPAM, "10-Q", report_date)
        self.assertEqual(self.read(EPAM)["fiscal_year_end"], "12-31")

    def test_one_filing_settles_nothing_and_says_so(self):
        # IBM, live: a single 10-Q. Two quarters cannot name a third.
        self.file("company:ibm", "10-Q", "2026-06-30")
        self.assertIsNone(self.read("company:ibm"))

    def test_a_company_with_no_filings_at_all_is_none(self):
        self.assertIsNone(self.read("company:nobody"))


class RegistrationTests(unittest.TestCase):
    def test_the_lane_registers_itself_once(self):
        self.assertEqual(LANE.operation, "dispatch_mission_consensus")
        self.assertEqual(LANE.driver_key, "mission_consensus")
        self.assertEqual(LANE.init_kwarg, LAUNCHER_KWARG)
        self.assertEqual(LANE.order, 89)
        self.assertEqual(LANE.param_fields, frozenset())

    def test_the_writer_derives_the_operation_from_the_registry(self):
        from dalton_core import writer_server

        self.assertIn("dispatch_mission_consensus",
                      writer_server.CORE_DISCOVERY_OPERATIONS)
        self.assertIn("dispatch_mission_consensus", writer_server.CORE_OPERATIONS)
        self.assertEqual(
            writer_server.OPERATION_FIELDS["dispatch_mission_consensus"], frozenset()
        )

    def test_the_driver_ticks_it_between_the_calendar_and_the_model_spec(self):
        from dalton_core.lane_registry import tick_lanes

        keys = [spec.driver_key for spec in tick_lanes()]
        self.assertLess(keys.index("mission_market_prices"), keys.index("mission_consensus"))
        self.assertLess(keys.index("mission_consensus"), keys.index("company_model_spec"))

    def test_the_lane_is_absent_without_an_approved_record(self):
        class Args:
            db = "/tmp/core.sqlite"
            consensus_governance = None

        self.assertIsNone(build_launcher(Args()))

    def test_the_launchagent_argument_appears_only_with_the_record(self):
        class Context:
            def __init__(self, state):
                self.state = state

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            self.assertEqual(argv_fragment(Context(state)), [])
            governance = state / "connector-governance"
            governance.mkdir()
            (governance / "yfinance-analyst-estimates-v1.json").write_text("{}")
            self.assertEqual(
                argv_fragment(Context(state))[0], "--consensus-governance"
            )


if __name__ == "__main__":
    unittest.main()

"""S5: the daily ownership lane -- what it chooses, what it grants, what it emits.

The lane is exercised against a fake launcher rather than a real child, for the
reason every lane test in this codebase is: what is being tested is the
coordinator's *decisions* -- which filing is next, which grant is missing, which
event has already gone out -- and spawning a process to ask that would test the
process.  The child itself is tested in ``test_s5_sec_ownership``.

The events go through P14a's real authority, because "the payload the child
builds is a payload the ledger accepts" is the seam most likely to rot and the
only way to check it is to write one.
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.lane_child_launcher import LaneChildRejected
from dalton_core.lane_registry import lane_for_operation, registered_lanes
from dalton_core.mission_ownership_lane import (
    LAUNCHER_KWARG,
    WRITE_SCOPES,
    MissionOwnershipLaneCoordinator,
    argv_fragment,
    issuer_for,
    missing_scopes,
)
from dalton_core.research_event import (
    DEFAULT_TIER_BY_KIND,
    EVENT_KINDS,
    PAYLOAD_FIELDS,
    ResearchEventAuthority,
    ResearchEventValidationError,
    record_event,
    validate_payload,
)
from dalton_core.sec_ownership_core import FORM4_OPERATION, KIND_BY_OPERATION
from tests.p14a_fixtures import ACN, AUTOMATION, CTSH, P14aHarness

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
ACN_CIK = "0001467373"


def filing(accession: str, form: str = "4", filed: str = "2026-09-08") -> dict:
    from dalton_core.sec_ownership_core import FORMS_BY_OPERATION

    operation = next(
        (name for name, forms in FORMS_BY_OPERATION.items() if form in forms), None
    )
    return {
        "accession": accession, "form": form, "operation": operation,
        "filing_date": filed, "report_date": filed,
        "primary_document": "primary_doc.xml", "primary_doc_description": None,
    }


class FakeLauncher:
    """One child at a time, without a process."""

    def __init__(self, *, approved=(FORM4_OPERATION,)) -> None:
        self._approved = tuple(approved)
        self.state_dir = Path("/tmp")
        self.started: list[dict] = []
        self.tickets: dict[str, dict] = {}
        self.reject: Exception | None = None

    def approved_operations(self) -> tuple[str, ...]:
        return self._approved

    def start(self, **kwargs) -> dict:
        if self.reject is not None:
            raise self.reject
        self.started.append(dict(kwargs))
        ticket = {"id": f"sec-ownership-run:{len(self.started):024x}",
                  "status": "running", **kwargs}
        self.tickets[ticket["id"]] = ticket
        return ticket

    def settle(self, ticket_ref: str, summary: dict, status: str = "succeeded") -> None:
        self.tickets[ticket_ref].update({"status": status, "summary": summary})

    def status(self, ticket_ref: str) -> dict:
        ticket = dict(self.tickets[ticket_ref])
        ticket.setdefault("summary", None)
        return ticket


def summary(events: list[dict], **overrides) -> dict:
    wire = {
        "parsed_row_count": len(events), "event_count": len(events),
        "events_over_cap": 0, "comparison_status": None, "prior_quarter": None,
        "value_unit": None, "value_unit_basis": None,
        "invocation_ref": "connector-invocation:sec-ownership:" + "a" * 32,
        "events": events, "failure_reason": None,
    }
    wire.update(overrides)
    return wire


def insider_event(key: str = "k1", shares: str = "4250") -> dict:
    return {
        "kind": "insider_transaction",
        "occurred_at": "2026-09-08T00:00:00+00:00",
        "evidence_tier": "primary_filing",
        "source_refs": ["sec:filing:0001467373-26-000045", "raw-sink:" + "b" * 64],
        "payload": {
            "accession": "0001467373-26-000045", "form": "4",
            "owner_name": "SYNTHETIC TESTPERSON A", "owner_cik": "0001900001",
            "role": "officer:Chief Financial Officer", "transaction_code": "S",
            "transaction_meaning": "open-market sale",
            "transaction_date": "2026-09-08",
            "security_title": "Class A Ordinary Shares", "shares": shares,
            "price_per_share": "318.4200", "acquired_disposed": "D",
            "shares_owned_following": "18600", "direct_or_indirect": "D",
            "issuer_name": "ACCENTURE PLC",
            "invocation_ref": "connector-invocation:sec-ownership:" + "a" * 32,
            "artifact_hash": "b" * 64, "event_key": key,
        },
    }


class RegistrationTests(unittest.TestCase):
    def test_the_lane_is_registered_at_its_own_order(self) -> None:
        spec = lane_for_operation("dispatch_mission_ownership")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.order, 88)
        self.assertEqual(spec.driver_key, "mission_ownership")
        self.assertEqual(spec.init_kwarg, LAUNCHER_KWARG)
        self.assertTrue(spec.core_discovery)

    def test_no_other_lane_took_its_order_or_its_key(self) -> None:
        orders = [spec.order for spec in registered_lanes()]
        keys = [spec.driver_key for spec in registered_lanes()]
        self.assertEqual(len(orders), len(set(orders)))
        self.assertEqual(len(keys), len(set(keys)))

    def test_the_launchagent_wires_the_lane_only_where_a_record_exists(self) -> None:
        import tempfile

        from dalton_core.lane_registry import LaunchAgentContext
        from dalton_core.sec_ownership_launcher import GOVERNANCE_FILENAME_BY_OPERATION

        with tempfile.TemporaryDirectory() as root:
            state = Path(root)
            (state / "connector-governance").mkdir()
            self.assertEqual(argv_fragment(LaunchAgentContext(state=state)), [])
            (state / "connector-governance"
             / GOVERNANCE_FILENAME_BY_OPERATION[FORM4_OPERATION]).write_text("{}")
            argv = argv_fragment(LaunchAgentContext(state=state))
            self.assertEqual(argv[0], "--sec-ownership-governance-dir")
            # The watcher is off until the declaration is on disk too.
            self.assertNotIn("--ir-page-declaration", argv)
            (state / "ir-pages.json").write_text("{}")
            self.assertIn(
                "--ir-page-declaration", argv_fragment(LaunchAgentContext(state=state))
            )

    def test_a_company_ref_carries_its_cik(self) -> None:
        self.assertEqual(issuer_for(ACN), ACN_CIK)
        self.assertIsNone(issuer_for("company:ticker:ACN"))


class GrantTests(unittest.TestCase):
    def test_both_grants_are_required_and_named_when_missing(self) -> None:
        self.assertEqual(missing_scopes(None), list(WRITE_SCOPES))
        self.assertEqual(
            missing_scopes({"autonomy": {"may_write": ["observation"]}}),
            ["market_event"],
        )
        self.assertEqual(
            missing_scopes({"autonomy": {"may_write": ["market_event"]}}),
            ["observation"],
        )
        self.assertEqual(
            missing_scopes(
                {"autonomy": {"may_write": ["observation", "market_event"]}}
            ),
            [],
        )


class CoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.launcher = FakeLauncher()
        self.recorded: list[dict] = []
        self.candidates = {ACN: [filing("0001467373-26-000045")]}
        self.mission = {
            "universe": [
                {"company_ref": ACN, "ticker": "ACN", "bootstrap_priority": "P0"},
                {"company_ref": CTSH, "ticker": "CTSH", "bootstrap_priority": "P1"},
            ],
            "autonomy": {"may_write": list(WRITE_SCOPES)},
        }
        self.coordinator = MissionOwnershipLaneCoordinator(
            connection=None, state_dir="/tmp", launcher=self.launcher,
            mission=lambda: self.mission,
            record_event=lambda **event: self.recorded.append(event),
            clock=lambda: NOW,
        )
        self.coordinator.candidates = self._candidates

    def _candidates(self, company) -> dict:
        rows = [
            row for row in self.candidates.get(company["company_ref"], [])
            if row["accession"] not in self.coordinator._read
        ]
        return {"status": "read", "reason": None, "filings": rows, "unapproved": 0}

    def test_a_mission_missing_a_grant_starts_nothing(self) -> None:
        self.mission["autonomy"]["may_write"] = ["observation"]
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "ungranted")
        self.assertIn("market_event", result["reason"])
        self.assertEqual(self.launcher.started, [])

    def test_a_writer_with_no_approved_operation_starts_nothing(self) -> None:
        self.launcher._approved = ()
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "unconfigured")
        self.assertIn("approved one at a time", result["reason"])

    def test_the_first_company_with_an_unread_filing_is_chosen(self) -> None:
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["company_ref"], ACN)
        self.assertEqual(result["accession"], "0001467373-26-000045")
        self.assertEqual(self.launcher.started[0]["operation"], FORM4_OPERATION)
        self.assertEqual(self.launcher.started[0]["issuer"], ACN_CIK)

    def test_a_second_tick_while_the_child_runs_starts_nothing(self) -> None:
        self.coordinator.dispatch_once()
        self.assertEqual(self.coordinator.dispatch_once()["status"], "busy")
        self.assertEqual(len(self.launcher.started), 1)

    def test_a_settled_run_records_its_events_and_is_not_read_again(self) -> None:
        launched = self.coordinator.dispatch_once()
        self.launcher.settle(launched["ticket_ref"], summary([insider_event()]))
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["settled"]["status"], "succeeded")
        self.assertEqual(result["settled"]["emission"]["status"], "recorded")
        self.assertEqual(len(self.recorded), 1)
        self.assertEqual(self.recorded[0]["kind"], "insider_transaction")
        self.assertEqual(self.recorded[0]["company_ref"], ACN)
        # Nothing left to read for either company.
        self.assertEqual(result["status"], "idle")

    def test_an_event_already_written_is_not_written_twice(self) -> None:
        launched = self.coordinator.dispatch_once()
        self.launcher.settle(launched["ticket_ref"], summary([insider_event()]))
        self.coordinator.dispatch_once()
        # The same filing arrives again -- a re-read after a restart, say.
        self.candidates[ACN] = [filing("0001467373-26-000046")]
        self.coordinator._read.clear()
        second = self.coordinator.dispatch_once()
        self.launcher.settle(second["ticket_ref"], summary([insider_event()]))
        self.coordinator.dispatch_once()
        self.assertEqual(len(self.recorded), 1)

    def test_a_failed_run_does_not_mark_the_filing_read(self) -> None:
        launched = self.coordinator.dispatch_once()
        self.launcher.settle(
            launched["ticket_ref"],
            summary([], failure_reason="HTTPError: 503"), status="failed",
        )
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["settled"]["status"], "failed")
        # Chosen again rather than lost: a transient error must not cost the
        # filing permanently.
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["accession"], "0001467373-26-000045")

    def test_a_company_failing_repeatedly_stops_taking_the_slot(self) -> None:
        for _ in range(3):
            launched = self.coordinator.dispatch_once()
            self.launcher.settle(
                launched["ticket_ref"], summary([], failure_reason="boom"),
                status="failed",
            )
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "idle")
        held = [row for row in result["skipped"] if row["reason"] == "held"]
        self.assertEqual([row["company_ref"] for row in held], [ACN])

    def test_a_13f_in_a_company_s_own_index_names_that_company_as_the_manager(self) -> None:
        # Accenture files seven 13F-HRs of its own. The manager on those is
        # Accenture, so the run is keyed by the holder CIK and not the issuer
        # one -- a 13F is filed *about a book*, not about an issuer.
        from dalton_core.sec_ownership_core import FORM13F_OPERATION

        self.launcher._approved = (FORM13F_OPERATION,)
        self.candidates = {ACN: [filing("0001467373-26-000091", form="13F-HR")]}
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "launched")
        started = self.launcher.started[0]
        self.assertEqual(started["operation"], FORM13F_OPERATION)
        self.assertEqual(started["holder_cik"], ACN_CIK)
        self.assertIsNone(started["issuer"])

    def test_a_rejected_launch_is_reported_not_raised(self) -> None:
        self.launcher.reject = LaneChildRejected("no approved record")
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "rejected")
        self.assertIn("no approved record", result["reason"])

    def test_without_a_writer_the_events_are_reported_not_lost(self) -> None:
        self.coordinator.record_event = None
        launched = self.coordinator.dispatch_once()
        self.launcher.settle(launched["ticket_ref"], summary([insider_event()]))
        result = self.coordinator.dispatch_once()
        emission = result["settled"]["emission"]
        self.assertEqual(emission["status"], "events_unwired")
        self.assertEqual(emission["recorded_count"], 1)
        self.assertIn("nothing recorded them", emission["reason"])

    def ir_watch(self, calls: list) -> object:
        def watch() -> dict:
            calls.append(1)
            return {
                "status": "watched", "watch_count": 3, "undeclared_count": 1,
                "changes": [{
                    "company_ref": ACN,
                    "occurred_at": "2026-09-09T09:00:00+00:00",
                    "source_refs": ["ir-watch:3f2a", "raw-sink:" + "c" * 64],
                    "payload": {
                        "watch_ref": "3f2a", "url": "https://investor.example.com/news",
                        "host": "investor.example.com", "diff_hash": "d" * 64,
                        "previous_snapshot_hash": "e" * 64,
                        "current_snapshot_hash": "f" * 64,
                        "changed_at": "1788900000", "added_line_count": 1,
                        "removed_line_count": 0, "title": "News", "excerpt": "a deal",
                        "artifact_hash": "c" * 64,
                        "invocation_ref": "connector-invocation:host-tool:x",
                        "event_key": "ir-page-change:zzz",
                    },
                }],
            }

        return watch

    def test_the_ir_sweep_runs_once_a_day_when_there_is_nothing_to_read(self) -> None:
        self.candidates = {}
        calls: list[int] = []
        self.coordinator.ir_watch = self.ir_watch(calls)
        first = self.coordinator.dispatch_once()
        self.assertEqual(first["ir_pages"]["recorded_count"], 1)
        self.assertEqual(first["ir_pages"]["undeclared_count"], 1)
        self.assertEqual(self.recorded[0]["kind"], "ir_page_change")
        second = self.coordinator.dispatch_once()
        self.assertEqual(second["ir_pages"]["status"], "watched_today")
        self.assertEqual(len(calls), 1)

    def test_the_ir_sweep_is_not_starved_by_a_backlog_of_filings(self) -> None:
        # The sweep used to run only on the ``idle`` branch, so a Core with
        # unread filings -- which is every Core on its first day -- never
        # reached it and the watcher looked broken rather than starved.
        calls: list[int] = []
        self.coordinator.ir_watch = self.ir_watch(calls)
        launched = self.coordinator.dispatch_once()
        self.assertEqual(launched["status"], "launched")
        self.assertEqual(launched["ir_pages"]["recorded_count"], 1)
        self.assertEqual(len(calls), 1)
        # And it is still once a day: the busy tick does not sweep again.
        busy = self.coordinator.dispatch_once()
        self.assertEqual(busy["status"], "busy")
        self.assertEqual(busy["ir_pages"]["status"], "watched_today")
        self.assertEqual(len(calls), 1)

    def test_a_failed_event_write_leaves_the_day_unwatched(self) -> None:
        # Marking the day watched before the records land lost the rest of the
        # day's changes to one raising writer.
        calls: list[int] = []
        self.coordinator.ir_watch = self.ir_watch(calls)
        self.candidates = {}

        def boom(**event) -> None:
            raise RuntimeError("the ledger said no")

        self.coordinator.record_event = boom
        first = self.coordinator.dispatch_once()
        self.assertEqual(first["ir_pages"]["status"], "failed")
        self.coordinator.record_event = lambda **event: self.recorded.append(event)
        second = self.coordinator.dispatch_once()
        self.assertEqual(second["ir_pages"]["recorded_count"], 1)
        self.assertEqual(len(calls), 2)

    def test_no_watcher_is_reported_rather_than_failing(self) -> None:
        self.candidates = {}
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["ir_pages"]["status"], "unconfigured")


class EventVocabularyTests(P14aHarness):
    """The four kinds are additive, typed, and the ledger really takes them."""

    def setUp(self) -> None:
        super().setUp()
        self.authority = ResearchEventAuthority(self.store)

    def test_the_four_kinds_are_declared_with_payloads_and_tiers(self) -> None:
        for kind in (
            "insider_transaction", "ownership_change", "holdings_change",
            "ir_page_change",
        ):
            self.assertIn(kind, EVENT_KINDS, kind)
            self.assertIn(kind, PAYLOAD_FIELDS, kind)
            self.assertIn(kind, DEFAULT_TIER_BY_KIND, kind)

    def test_the_filings_are_primary_and_the_web_page_is_not(self) -> None:
        for kind in ("insider_transaction", "ownership_change", "holdings_change"):
            self.assertEqual(DEFAULT_TIER_BY_KIND[kind], "primary_filing")
        # Nothing was filed with anybody and a marketing page has no revision
        # history, so it is the company speaking, not a filing.
        self.assertEqual(DEFAULT_TIER_BY_KIND["ir_page_change"], "management_direct")

    def test_the_kinds_that_were_there_before_are_still_there(self) -> None:
        for kind in ("price_move", "news", "filing", "calendar", "claim"):
            self.assertIn(kind, EVENT_KINDS, kind)

    def test_an_invented_payload_field_is_refused(self) -> None:
        payload = dict(insider_event()["payload"])
        payload["sentiment"] = "bearish"
        with self.assertRaises(ResearchEventValidationError) as caught:
            validate_payload("insider_transaction", payload)
        self.assertIn("sentiment", str(caught.exception))

    def test_an_insider_transaction_lands_and_the_second_read_is_a_duplicate(self) -> None:
        event = insider_event()
        first = record_event(
            self.authority, company_ref=ACN, mission=self.mission,
            actor_ref=AUTOMATION, kind=event["kind"],
            occurred_at=event["occurred_at"], source_refs=event["source_refs"],
            payload=event["payload"],
        )
        self.assertEqual(first["status"], "fresh")
        self.assertEqual(first["evidence_tier"], "primary_filing")
        again = record_event(
            self.authority, company_ref=ACN, mission=self.mission,
            actor_ref=AUTOMATION, kind=event["kind"],
            occurred_at=event["occurred_at"], source_refs=event["source_refs"],
            payload=event["payload"],
        )
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], first["id"])

    def test_a_child_built_payload_is_one_the_ledger_accepts(self) -> None:
        # The seam most likely to rot: the child builds payloads and the lane
        # writes them, and nothing else would notice a field drifting apart.
        import argparse

        from dalton_core.connector_governance import build_governance_record
        from dalton_core.sec_ownership_cli import run as run_child

        record = build_governance_record(
            KIND_BY_OPERATION[FORM4_OPERATION], approved_by="human:test-owner",
            status="approved",
        )
        path = self.state_dir / "form4.json"
        path.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
        fixtures = Path(__file__).resolve().parent / "fixtures" / "sec-ownership"
        result = run_child(argparse.Namespace(
            state_dir=str(self.state_dir), governance=str(path),
            operation=FORM4_OPERATION, company_ref=ACN,
            accession="0001467373-26-000045", form_type="4", issuer=ACN_CIK,
            holder_cik=None, quarter=None, filed_at="2026-08-14",
            company_cusips=None, cover_file=None, prior_file=None,
            prior_cover_file=None, prior_accession=None,
            summary_dir=str(self.state_dir / "run"), actor_ref="core:test",
            user_agent="test", allow_network=False,
            fixture_file=str(fixtures / "form4-sale.xml"), quiet=True,
        ))
        self.assertEqual(result["status"], "succeeded", result["failure_reason"])
        written = [
            record_event(
                self.authority, company_ref=ACN, mission=self.mission,
                actor_ref=AUTOMATION, kind=event["kind"],
                occurred_at=event["occurred_at"], source_refs=event["source_refs"],
                payload=event["payload"],
            )
            for event in result["events"]
        ]
        self.assertEqual([row["status"] for row in written], ["fresh"] * 3)
        self.assertEqual({row["kind"] for row in written}, {"insider_transaction"})


if __name__ == "__main__":
    unittest.main()

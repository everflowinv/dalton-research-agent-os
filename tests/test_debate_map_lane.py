"""P12c: the child checks the grant before it spends, and the lane goes quiet.

The lane is queueless and its resting state is the usual one: a map is redrawn
when the evidence it was drawn from stops being the evidence we hold, which is
derived from the Ledger every tick.  So the interesting assertion is not "the
queue drained" but "after a run the subject stops being chosen", which is the
invariant a lane keyed on anything other than the evidence itself would break.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.debate_map import DebateMapAuthority, evidence_fingerprint
from dalton_core.debate_map_cli import (
    WRITE_SCOPE, build_parser, granted, run_debate_map, subject_kind_for,
)
from dalton_core.debate_map_draft import (
    document_attribution, subject_claim_refs, subject_claim_rows, subject_driver_rows,
)
from dalton_core.debate_map_launcher import DebateMapLauncher, run_digest
from dalton_core.lane_child_launcher import LaneChildConflict, LaneChildRejected
from dalton_core.lane_registry import (
    lane_for_operation, lane_init_kwargs, registered_lanes, tick_lanes,
)
from dalton_core.mission_debate_map_lane import (
    LAUNCHER_KWARG, MissionDebateMapLaneCoordinator, argv_fragment, build_launcher,
)
from dalton_core.store import DaltonStore
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_claim_index_entries import ACN, LedgerFixture

DRIVER = "driver:d"


def draft_reply() -> str:
    return json.dumps({"debates": [{
        "debate_ref": "new-1",
        "question": "Will demand hold up into next year?",
        "driver_refs": [DRIVER],
        "question_admission_index": 0,
        "causal_chain_index": 0,
        "bull": {"statement": "Demand accelerating.", "claim_refs": ["C1"]},
        "bear": {"statement": "Demand decelerating.", "claim_refs": ["C2"]},
        "market": {"available": False, "lean": None, "statement": None, "refs": []},
        "ours": {"state": "none_yet", "side": None, "statement": None, "refs": []},
        "gaining": "neither",
        "resolution": None,
    }]})


PASS = json.dumps({"verdict": "pass", "findings": []})


class FakeModel:
    """Two canned replies, and a record of what was asked."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, *args, **kwargs):
        return self

    def call(self, *, purpose, request_id, prompt, mission):
        index = len(self.prompts)
        self.prompts.append(prompt)
        return {
            "text": self.replies[index], "replayed": False, "cost_micros": 500,
            "work_order_ref": f"work:cockpit-debate_map-{index}",
            "invocation_ref": f"inv-{index}", "route_decision_ref": f"route-{index}",
        }


def fake_family(_config, route_decision_ref):
    return {"route-0": "family-a", "route-1": "family-b"}.get(route_decision_ref)


class ChildHarness:
    def __init__(self, *, may_write=None):
        self._dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._dir.name)
        self.fixture = LedgerFixture(str(self.state_dir / "core.sqlite"))
        self.store = self.fixture.store
        state = bootstrap_method_authorities(self.store)
        self.missions = CoverageMissionAuthority(self.store)
        params = mission_params(state)
        params["autonomy"] = {
            **params["autonomy"],
            "may_write": list(may_write if may_write is not None
                              else list(params["autonomy"]["may_write"]) + [WRITE_SCOPE]),
        }
        self.mission = self.missions.create_mission(params.pop("mission_ref"), **params)

    def add_claims(self):
        self.fixture.add_claim(
            "debate-bull", kind="qualitative", value=None, unit=None,
            metric="demand environment", period="current",
            statement="Bookings are accelerating into next year.",
            source_type="authenticated_transcript")
        self.fixture.add_claim(
            "debate-bear", kind="qualitative", value=None, unit=None,
            metric="demand environment", period="current",
            statement="Discretionary demand is decelerating.",
            source_type="public_web")

    def run(self, **overrides):
        params = {
            "state_dir": self.state_dir, "subject_ref": ACN,
            "summary_dir": self.state_dir, "model_config_path": None,
            "scheduler_db": None, "dry_run": True,
        }
        params.update(overrides)
        return run_debate_map(**params)

    def with_model(self, replies=(None,)):
        config = self.state_dir / "model.json"
        config.write_text(json.dumps({"model_router_db": str(self.state_dir / "r.db")}),
                          encoding="utf-8")
        return config

    def close(self):
        self.store.close()
        self._dir.cleanup()


class WriteScopeTests(unittest.TestCase):
    def test_the_scope_is_the_word_wave_zero_added(self):
        self.assertEqual(WRITE_SCOPE, "debate_map")
        self.assertTrue(granted({"autonomy": {"may_write": ["debate_map"]}}))
        self.assertFalse(granted({"autonomy": {"may_write": ["claim"]}}))

    def test_a_mission_without_the_word_holds_before_spending(self):
        harness = ChildHarness(may_write=["observation", "stage_record"])
        self.addCleanup(harness.close)
        harness.add_claims()
        summary = harness.run()
        self.assertEqual(summary["status"], "held")
        self.assertEqual(summary["map_status"], "not_authorized")
        self.assertIn("debate_map", summary["failure_reason"])


class ChildRunTests(unittest.TestCase):
    def setUp(self):
        self.harness = ChildHarness()
        self.addCleanup(self.harness.close)

    def test_a_dry_run_assembles_the_table_and_writes_nothing(self):
        self.harness.add_claims()
        summary = self.harness.run()
        self.assertEqual((summary["status"], summary["map_status"]),
                         ("succeeded", "dry_run"))
        self.assertEqual(summary["claims"], 2)
        self.assertGreater(summary["prompt_bytes"], 0)
        self.assertIsNone(DebateMapAuthority(self.harness.store).current(ACN))

    def test_without_a_model_nothing_is_drafted_because_nothing_can_be(self):
        self.harness.add_claims()
        summary = self.harness.run(dry_run=False)
        self.assertEqual(summary["map_status"], "gated")
        self.assertEqual(summary["failure_reason"], "no model configured")

    def test_the_summary_is_written_beside_the_run(self):
        self.harness.add_claims()
        self.harness.run()
        summary = json.loads(
            (self.harness.state_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["map_status"], "dry_run")
        self.assertEqual(summary["policy_ref"], "debate-policy:p12c:v1")

    def test_a_verified_draft_becomes_a_first_version(self):
        self.harness.add_claims()
        config = self.harness.with_model()
        model = FakeModel([draft_reply(), PASS])
        with patch("dalton_core.debate_map_cli.CockpitModel", model), \
                patch("dalton_core.debate_map_cli.route_family", fake_family):
            summary = self.harness.run(dry_run=False, model_config_path=config)
        self.assertEqual(summary["map_status"], "fresh")
        self.assertEqual(summary["debates"], 1)
        self.assertEqual(summary["cost_micros"], 1_000)
        current = DebateMapAuthority(self.harness.store).current(ACN)
        self.assertEqual(current["version"], 1)
        self.assertEqual(current["change_reason"], "evidence_thicker")
        self.assertEqual(current["drafted_by"]["model_family"], "family-a")
        self.assertEqual(current["verified_by"]["model_family"], "family-b")
        self.assertEqual(current["debates"][0]["status"], "candidate")
        self.assertEqual(current["debates"][0]["driver_refs"], [DRIVER])
        # The drafting prompt showed the claims and the constitution's lists.
        self.assertIn("Discretionary demand is decelerating.", model.prompts[0])
        self.assertIn("Bookings lead revenue", model.prompts[0])

    def test_the_same_evidence_twice_publishes_one_version(self):
        self.harness.add_claims()
        config = self.harness.with_model()
        for _ in range(2):
            model = FakeModel([draft_reply(), PASS])
            with patch("dalton_core.debate_map_cli.CockpitModel", model), \
                    patch("dalton_core.debate_map_cli.route_family", fake_family):
                summary = self.harness.run(dry_run=False, model_config_path=config)
        self.assertEqual(summary["map_status"], "duplicate")
        self.assertEqual(DebateMapAuthority(self.harness.store).counts()["versions"], 1)

    def test_a_verifier_on_the_same_family_publishes_nothing(self):
        self.harness.add_claims()
        config = self.harness.with_model()
        model = FakeModel([draft_reply(), PASS])
        with patch("dalton_core.debate_map_cli.CockpitModel", model), \
                patch("dalton_core.debate_map_cli.route_family",
                      lambda _c, _r: "family-a"):
            summary = self.harness.run(dry_run=False, model_config_path=config)
        self.assertEqual(summary["map_status"], "not_independent")
        self.assertIsNone(DebateMapAuthority(self.harness.store).current(ACN))

    def test_an_unresolvable_family_publishes_nothing(self):
        self.harness.add_claims()
        config = self.harness.with_model()
        model = FakeModel([draft_reply(), PASS])
        with patch("dalton_core.debate_map_cli.CockpitModel", model):
            summary = self.harness.run(dry_run=False, model_config_path=config)
        self.assertEqual(summary["map_status"], "not_independent")
        self.assertIsNone(DebateMapAuthority(self.harness.store).current(ACN))

    def test_the_cheap_reader_agrees_with_the_expensive_one(self):
        # The lane asks every subject every tick; it must get the same answer
        # the drafting rows would give without building them.
        self.harness.add_claims()
        self.assertEqual(
            subject_claim_refs(self.harness.store, ACN),
            [row["claim_version_ref"]
             for row in subject_claim_rows(self.harness.store, ACN)],
        )

    def test_the_industry_is_a_subject_too(self):
        self.assertEqual(subject_kind_for("industry:us-it-services"), "industry")
        self.assertEqual(subject_kind_for(ACN), "company")

    def test_the_drivers_come_from_the_pack_the_constitution_binds(self):
        drivers = subject_driver_rows(
            self.harness.store, ACN,
            {"bindings": {"driver_pack_version":
                          {"ref": "driver-pack-version:us-it-services:1"}}},
        )
        self.assertEqual([row["driver_ref"] for row in drivers], [DRIVER])

    def test_the_parser_takes_a_subject_and_a_model(self):
        args = build_parser().parse_args(
            ["--state-dir", "/s", "--subject-ref", ACN, "--summary-dir", "/d"])
        self.assertEqual(args.subject_ref, ACN)
        self.assertIsNone(args.model_config)


class DocumentAttributionTests(unittest.TestCase):
    """The seam the extraction-throughput slice fills, read defensively.

    Broker attribution is being persisted onto the discovered-document row on
    another branch, additively and under a name that is not settled.  This
    module must work on a Core that has none of those columns -- which is
    today's live one -- and light up on one that has them, without being
    taught a migration.
    """

    def core(self, columns: str) -> sqlite3.Connection:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE coverage_mission_discovered_documents "
            f"(document_ref TEXT PRIMARY KEY{columns})"
        )
        self.addCleanup(connection.close)
        return connection

    def test_a_core_without_the_table_answers_nothing_rather_than_raising(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        self.assertEqual(document_attribution(connection), {})

    def test_todays_live_shape_yields_only_the_host(self):
        connection = self.core(", host TEXT")
        connection.execute(
            "INSERT INTO coverage_mission_discovered_documents VALUES(?,?)",
            ("public-web-url:sha256:a", "reuters.com"))
        connection.execute(
            "INSERT INTO coverage_mission_discovered_documents VALUES(?,?)",
            ("alphaengine-doc:1", None))
        found = document_attribution(connection)
        self.assertEqual(found, {"public-web-url:sha256:a": {"host": "reuters.com"}})

    def test_an_added_attribution_column_is_picked_up_under_any_of_its_names(self):
        for column in ("publisher", "broker", "attributed_publisher"):
            with self.subTest(column=column):
                connection = self.core(f", {column} TEXT, title TEXT")
                connection.execute(
                    "INSERT INTO coverage_mission_discovered_documents VALUES(?,?,?)",
                    ("alphaengine-doc:1", "TD Cowen", "ACN: bookings review"))
                found = document_attribution(connection)
                self.assertEqual(found["alphaengine-doc:1"]["publisher"], "TD Cowen")
                self.assertEqual(found["alphaengine-doc:1"]["title"],
                                 "ACN: bookings review")

    def test_metadata_columns_are_read_when_no_publisher_was_attributed(self):
        connection = self.core(", authors TEXT, sources TEXT")
        connection.execute(
            "INSERT INTO coverage_mission_discovered_documents VALUES(?,?,?)",
            ("alphaengine-doc:1", "Bryan Bergin", "TD Cowen Equity Research"))
        found = document_attribution(connection)["alphaengine-doc:1"]
        self.assertEqual(found["authors"], "Bryan Bergin")
        self.assertEqual(found["sources"], "TD Cowen Equity Research")
        self.assertNotIn("publisher", found)


class FakeLauncher:
    def __init__(self, *, conflict=False, reject=False):
        self.started: list[tuple[str, str]] = []
        self.tickets: dict[str, dict] = {}
        self.conflict = conflict
        self.reject = reject

    def start(self, *, subject_ref, fingerprint):
        if self.conflict:
            raise LaneChildConflict("already running")
        if self.reject:
            raise LaneChildRejected("no")
        self.started.append((subject_ref, fingerprint))
        ticket = {"id": f"debate-map-run:{run_digest(subject_ref, fingerprint)}",
                  "status": "running", "subject_ref": subject_ref,
                  "evidence_fingerprint": fingerprint, "summary": None}
        self.tickets[ticket["id"]] = ticket
        return ticket

    def settle(self, ticket_ref, summary, status="succeeded"):
        self.tickets[ticket_ref].update({"status": status, "summary": summary})

    def status(self, ticket_ref):
        return self.tickets[ticket_ref]


class LaneCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.harness = ChildHarness()
        self.addCleanup(self.harness.close)
        self.launcher = FakeLauncher()
        self.coordinator = MissionDebateMapLaneCoordinator(
            store=self.harness.store, launcher=self.launcher,
            mission=lambda: self.harness.mission,
        )

    def test_a_subject_with_no_claims_is_not_chosen(self):
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "idle")
        self.assertEqual(self.launcher.started, [])

    def test_the_subject_whose_evidence_moved_is_launched(self):
        self.harness.add_claims()
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["subject_ref"], ACN)
        rows = subject_claim_rows(self.harness.store, ACN)
        self.assertEqual(
            result["evidence_fingerprint"],
            evidence_fingerprint(row["claim_version_ref"] for row in rows),
        )

    def test_a_map_drawn_from_the_evidence_we_hold_is_left_alone(self):
        self.harness.add_claims()
        rows = subject_claim_rows(self.harness.store, ACN)
        fingerprint = evidence_fingerprint(row["claim_version_ref"] for row in rows)
        DebateMapAuthority(self.harness.store).publish_map(
            subject_ref=ACN, subject_kind="company", change_reason="evidence_thicker",
            change_evidence_refs=[rows[0]["claim_version_ref"]],
            constitution_ref="constitution-version:x:1", constitution_hash="a" * 64,
            evidence_fingerprint=fingerprint,
            debates=[{
                "debate_ref": "debate:x", "question": "q?",
                "driver_refs": [DRIVER],
                "admission_index": 0, "causal_link_index": 0,
                "bull_position": {"statement": "up",
                                  "claim_refs": [rows[0]["claim_version_ref"]]},
                "bear_position": {"statement": "down",
                                  "claim_refs": [rows[1]["claim_version_ref"]]},
                "market_position": {"available": False, "lean": None,
                                    "statement": None, "refs": []},
                "our_position": {"state": "none_yet", "side": None,
                                 "statement": None, "refs": []},
                "status": "candidate", "last_shift_reason": None,
                "first_seen_at": "2026-09-09T00:00:00+00:00",
                "source_independence": {"bull_sources": 1, "bear_sources": 1},
            }],
            actor_ref="automation:dalton", created_at="2026-09-09T00:00:00+00:00",
        )
        self.assertEqual(self.coordinator.dispatch_once()["status"], "idle")

    def test_a_failed_run_holds_that_evidence_back_but_not_forever(self):
        self.harness.add_claims()
        launched = self.coordinator.dispatch_once()
        self.launcher.settle(launched["ticket_ref"],
                             {"map_status": "refused", "failure_reason": "bad draft"})
        held = self.coordinator.dispatch_once()
        self.assertEqual(held["status"], "held")
        self.assertEqual(held["reason"], "bad draft")
        self.assertEqual(held["settled"]["map_status"], "refused")
        # A new Claim changes the fingerprint, so the hold does not survive it.
        self.harness.fixture.add_claim(
            "debate-third", kind="qualitative", value=None, unit=None,
            metric="demand environment", period="current",
            statement="A third view arrives.", source_type="public_web")
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")

    def test_a_duplicate_holds_the_slot_open_for_the_next_subject(self):
        # A duplicate publishes nothing, so the stored fingerprint never moves
        # and this subject would be chosen again on every tick forever. The
        # hold is what lets subjects two through N have a turn; it releases
        # itself the moment a Claim about this one arrives.
        self.harness.add_claims()
        launched = self.coordinator.dispatch_once()
        self.launcher.settle(launched["ticket_ref"], {"map_status": "duplicate"})
        again = self.coordinator.dispatch_once()
        self.assertEqual(again["status"], "held")
        self.assertIn("duplicate", again["reason"])
        self.harness.fixture.add_claim(
            "debate-after-duplicate", kind="qualitative", value=None, unit=None,
            metric="demand environment", period="current",
            statement="Something new arrives.", source_type="public_web")
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")

    def test_every_subject_gets_a_turn_when_none_of_them_publishes(self):
        # The starvation this lane had to be taught about: five companies, one
        # slot, and a first company whose run publishes nothing. Without the
        # hold it takes the slot on every tick and the other four are never
        # looked at.
        for subject in ("company:sec-cik:0001352010", "company:sec-cik:0001058290"):
            self.harness.fixture.add_claim(
                "debate-" + subject[-4:], subject_ref=subject, kind="qualitative",
                value=None, unit=None, metric="demand environment", period="current",
                statement="A view about this company.", source_type="public_web")
        self.harness.add_claims()
        seen = []
        for _ in range(4):
            result = self.coordinator.dispatch_once()
            if result["status"] != "launched":
                continue
            seen.append(result["subject_ref"])
            self.launcher.settle(result["ticket_ref"], {"map_status": "duplicate"})
        self.assertEqual(len(seen), len(set(seen)))
        self.assertGreaterEqual(len(seen), 3)

    def test_a_busy_launcher_is_reported_not_raised(self):
        self.harness.add_claims()
        self.coordinator.launcher = FakeLauncher(conflict=True)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "busy")
        self.coordinator.launcher = FakeLauncher(reject=True)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "rejected")

    def test_dependency_failure_retries_the_same_evidence_as_a_probe(self):
        self.harness.add_claims()
        first = self.coordinator.dispatch_once()
        self.launcher.settle(first["ticket_ref"], {
            "map_status": "model_unavailable", "failure_reason": "model_unavailable"})
        probe = self.coordinator.dispatch_once()
        self.assertEqual(probe["status"], "launched")
        self.assertEqual(probe["evidence_fingerprint"], first["evidence_fingerprint"])
        self.assertEqual(probe["settled"]["failure"]["failure_class"],
                         "dependency_unavailable")

    def test_no_mission_is_unconfigured(self):
        coordinator = MissionDebateMapLaneCoordinator(
            store=self.harness.store, launcher=self.launcher, mission=lambda: None)
        self.assertEqual(coordinator.dispatch_once()["status"], "unconfigured")


class LaneRegistrationTests(unittest.TestCase):
    def test_the_lane_registers_itself_once_with_its_own_order(self):
        spec = lane_for_operation("dispatch_debate_map")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.order, 135)
        self.assertEqual(spec.driver_key, "debate_map")
        self.assertEqual(spec.init_kwarg, LAUNCHER_KWARG)
        self.assertIn(spec, tick_lanes())
        self.assertIn(LAUNCHER_KWARG, lane_init_kwargs())
        orders = [item.order for item in registered_lanes()]
        self.assertEqual(len(orders), len(set(orders)))

    def test_the_lane_and_its_model_call_drink_from_the_same_pool(self):
        # Coverage, by C2's default for a lane its table has not named. Both
        # ends have to agree: the tick's ledger books the lane's spend and the
        # router admits the call by purpose, and a lane whose two ends resolved
        # differently would be charged twice against different budgets.
        from dalton_core.budget_pools import pool_for_operation, pool_for_purpose

        self.assertEqual(pool_for_operation("dispatch_debate_map"), "coverage")
        self.assertEqual(pool_for_purpose("debate_map"), "coverage")

    def test_the_lane_is_absent_when_no_model_configuration_is_installed(self):
        class Args:
            debate_map_model_config = None
        self.assertIsNone(build_launcher(Args()))

        class Context:
            state = Path("/nonexistent")
        self.assertEqual(argv_fragment(Context()), [])

    def test_the_launcher_names_a_run_by_its_subject_and_its_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            launcher = DebateMapLauncher(state_dir=directory)
            self.addCleanup(launcher.close)
            self.assertFalse(launcher.configured)
            with self.assertRaises(LaneChildRejected):
                launcher.start(subject_ref="", fingerprint="f")
            with self.assertRaises(LaneChildRejected):
                launcher.start(subject_ref=ACN, fingerprint="")
        self.assertEqual(run_digest(ACN, "f"), run_digest(ACN, "f"))
        self.assertNotEqual(run_digest(ACN, "f"), run_digest(ACN, "g"))


if __name__ == "__main__":
    unittest.main()

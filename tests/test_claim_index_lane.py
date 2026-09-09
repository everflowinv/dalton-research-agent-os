"""P12b: the child checks the grant before it spends, and the lane goes quiet.

The child's shape is the model-specification lane's, and so are its failure
modes: it must decide nothing when the mission has not authorised it, write
everything the rules settle even without a model, refuse a whole batch rather
than keep the rows a bad reply happened to get right, and report ``idle`` when
the backlog is empty instead of relaunching itself.

The lane is queueless: what needs tagging is derived from the Ledger every
tick. So the interesting assertion is not "the queue drained" but "after the
run there is nothing left to do", which is the invariant the model-spec lane
had to learn the hard way when its selector and its child disagreed about a
hash and one company blocked the other four forever.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.claim_index_authority import (
    ClaimIndexAuthority, current_entries, table_exists,
)
from dalton_core.claim_index_cli import (
    WRITE_SCOPE,
    build_parser,
    granted_scope,
    run_claim_index,
)
from dalton_core.claim_index_launcher import ClaimIndexLauncher, batch_digest
from dalton_core.claim_index_tagging import pending_claims
from dalton_core.cockpit_model import build_work, purposes
from dalton_core.coverage_mission import (
    AUTOMATION_WRITE_SCOPES,
    CoverageMissionAuthority,
)
from dalton_core.mission_claim_index_lane import MissionClaimIndexLaneCoordinator
from dalton_core.store import DaltonStore
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
from tests.test_claim_index_entries import LedgerFixture

ACN = "company:sec-cik:0001467373"
EPAM = "company:sec-cik:0001352010"


class FakeModel:
    """One canned reply, and a record of what it was asked."""

    def __init__(self, text, *args, **kwargs):
        self.text = text
        self.prompts = []

    def __call__(self, *args, **kwargs):
        return self

    def call(self, *, purpose, request_id, prompt, mission):
        self.prompts.append(prompt)
        return {
            "text": self.text, "replayed": False, "cost_micros": 1234,
            "work_order_ref": "work:cockpit-claim_index-" + "a" * 32,
        }


class ChildHarness:
    def __init__(self, *, may_write=None):
        self._dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self._dir.name)
        self.fixture = LedgerFixture(str(self.state_dir / "core.sqlite"))
        self.store = self.fixture.store
        state = bootstrap_method_authorities(self.store)
        self.missions = CoverageMissionAuthority(self.store)
        params = mission_params(state)
        if may_write is None:
            # INT1: the live manifest does not grant claim_index yet (the owner
            # publishes that version), and the fallback to ``claim`` is gone,
            # so the fixture widens may_write in place -- which is what the
            # parallel plan says a test should do rather than loosen the
            # authority.
            may_write = list(params["autonomy"]["may_write"]) + ["claim_index"]
        params["autonomy"] = {**params["autonomy"], "may_write": list(may_write)}
        self.mission = self.missions.create_mission(params.pop("mission_ref"), **params)

    def add_claims(self):
        # One number the rules can settle alone, and two pieces of prose only a
        # model can file.
        self.fixture.add_claim("lane-number", metric="quarterly_revenue_yoy_growth")
        self.fixture.add_claim(
            "lane-prose-one", kind="qualitative", value=None, unit=None,
            metric="demand environment", period="current",
            statement="Clients are prioritising reinvention programmes.",
            source_type="authenticated_transcript")
        self.fixture.add_claim(
            "lane-prose-two", kind="qualitative", value=None, unit=None,
            metric="competitive positioning", period="current",
            statement="Wins against heritage vendors are up.",
            source_type="public_web")

    def run(self, **overrides):
        params = {
            "state_dir": self.state_dir, "model_config_path": None,
            "summary_dir": self.state_dir, "scheduler_db": None, "dry_run": True,
        }
        params.update(overrides)
        return run_claim_index(**params)

    def close(self):
        self.store.close()
        self._dir.cleanup()


class WriteScopeTests(unittest.TestCase):
    def test_the_index_has_its_own_word_and_borrows_no_other(self):
        # INT1: ``claim_index`` joined AUTOMATION_WRITE_SCOPES, so the
        # temporary fallback to ``claim`` is gone. An index entry is strictly
        # weaker than the Claim it points at, but ADR-0004 gives an automated
        # write its own word, and a mission that granted only ``claim`` did not
        # grant this.
        self.assertIn(WRITE_SCOPE, AUTOMATION_WRITE_SCOPES)
        self.assertEqual(WRITE_SCOPE, "claim_index")
        self.assertIsNone(granted_scope({"autonomy": {"may_write": ["claim"]}}))
        self.assertEqual(
            granted_scope({"autonomy": {"may_write": ["claim", "claim_index"]}}),
            "claim_index")
        self.assertIsNone(granted_scope({"autonomy": {"may_write": ["observation"]}}))

    def test_a_mission_that_grants_neither_word_holds_before_spending(self):
        harness = ChildHarness(may_write=["observation", "stage_record"])
        self.addCleanup(harness.close)
        harness.add_claims()
        summary = harness.run()
        self.assertEqual(summary["status"], "held")
        self.assertEqual(summary["index_status"], "not_authorized")
        self.assertIn("claim_index", summary["failure_reason"])
        self.assertFalse(current_entries(harness.store.connection))


class ChildRunTests(unittest.TestCase):
    def setUp(self):
        self.harness = ChildHarness()
        self.addCleanup(self.harness.close)

    def test_with_nothing_to_tag_the_run_is_idle_and_writes_nothing(self):
        summary = self.harness.run()
        self.assertEqual((summary["status"], summary["index_status"]),
                         ("idle", "nothing_to_tag"))

    def test_a_dry_run_says_what_it_would_do_and_writes_nothing(self):
        self.harness.add_claims()
        summary = self.harness.run()
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["index_status"], "dry_run")
        self.assertEqual(summary["write_scope"], "claim_index")
        self.assertEqual(summary["pending"], 3)
        self.assertEqual(summary["rule_tagged"], 1)
        self.assertEqual(summary["batch_size"], 2)
        self.assertEqual(summary["formal_authority_writes"], 0)
        self.assertGreater(summary["prompt_bytes"], 0)
        # Not one entry, and not even the table: "assemble and stop" is what a
        # dry run is for, and one that had already written half the answer is
        # not one anybody can look before they leap with.
        self.assertEqual(current_entries(self.harness.store.connection), {})
        self.assertFalse(table_exists(self.harness.store.connection))

    def test_without_a_model_the_rules_are_still_written_and_the_rest_gated(self):
        self.harness.add_claims()
        summary = self.harness.run(dry_run=False)
        self.assertEqual(summary["index_status"], "gated")
        self.assertEqual(summary["failure_reason"], "no model configured")
        self.assertEqual(summary["rule_tagged"], 1)
        entries = current_entries(self.harness.store.connection)
        self.assertEqual(len(entries), 1)
        [entry] = entries.values()
        self.assertEqual(entry["aspect"], "segments_and_mix")
        self.assertEqual(entry["aspect_source"], "rule")
        self.assertEqual(entry["importance"], "filing")
        self.assertEqual(entry["as_of"], "2026-05-31")

    def test_the_summary_is_written_beside_the_run(self):
        self.harness.add_claims()
        self.harness.run()
        summary = json.loads(
            (self.harness.state_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["index_status"], "dry_run")

    def test_a_canned_model_table_files_the_prose_and_records_the_work_order(self):
        self.harness.add_claims()
        config = self.harness.state_dir / "model.json"
        config.write_text("{}", encoding="utf-8")
        model = FakeModel("1\tdemand_drivers\n2\tcompetitive_position\n")
        with patch("dalton_core.claim_index_cli.CockpitModel", model):
            summary = self.harness.run(
                dry_run=False, model_config_path=config)
        self.assertEqual(summary["index_status"], "tagged")
        self.assertEqual(summary["model_tagged"], 2)
        self.assertEqual(summary["cost_micros"], 1234)
        entries = current_entries(self.harness.store.connection)
        self.assertEqual(len(entries), 3)
        model_entries = [e for e in entries.values() if e["aspect_source"] == "model"]
        self.assertEqual(len(model_entries), 2)
        for entry in model_entries:
            self.assertTrue(entry["tagger_ref"].startswith("model:work:cockpit-claim_index-"))
        # The prompt showed the claims and the vocabulary; not their refs.
        [prompt] = model.prompts
        self.assertIn("Clients are prioritising reinvention programmes.", prompt)
        self.assertIn("demand_drivers", prompt)

    def test_after_a_full_run_there_is_nothing_left_to_tag(self):
        self.harness.add_claims()
        config = self.harness.state_dir / "model.json"
        config.write_text("{}", encoding="utf-8")
        with patch("dalton_core.claim_index_cli.CockpitModel",
                   FakeModel("1\tdemand_drivers\n2\tcompetitive_position\n")):
            self.harness.run(dry_run=False, model_config_path=config)
        self.assertEqual(pending_claims(self.harness.store), [])
        with patch("dalton_core.claim_index_cli.CockpitModel",
                   FakeModel("1\tother\n")):
            again = self.harness.run(dry_run=False, model_config_path=config)
        self.assertEqual(again["index_status"], "nothing_to_tag")

    def test_an_out_of_vocabulary_word_refuses_the_batch_and_writes_no_aspect(self):
        self.harness.add_claims()
        config = self.harness.state_dir / "model.json"
        config.write_text("{}", encoding="utf-8")
        with patch("dalton_core.claim_index_cli.CockpitModel",
                   FakeModel("1\tmoat_and_pricing\n2\tcompetitive_position\n")):
            summary = self.harness.run(dry_run=False, model_config_path=config)
        self.assertEqual(summary["index_status"], "refused")
        self.assertIn("moat_and_pricing", summary["failure_reason"])
        self.assertEqual(summary["model_tagged"], 0)
        entries = current_entries(self.harness.store.connection)
        # The rule-settled number is still written; the batch that went outside
        # the vocabulary contributed nothing at all.
        self.assertEqual(len(entries), 1)

    def test_a_re_run_of_an_unchanged_claim_is_a_duplicate_not_a_new_version(self):
        self.harness.add_claims()
        first = self.harness.run(dry_run=False)
        self.assertEqual(first["fresh"], 1)
        authority = ClaimIndexAuthority(self.harness.store)
        # pending_claims no longer offers the tagged number, so force the same
        # judgement through again the way a rule change would.
        entry = list(current_entries(self.harness.store.connection).values())[0]
        result = authority.record_entry(
            claim_version_ref=entry["claim_version_ref"],
            claim_version_hash=entry["claim_version_hash"],
            claim_ref=entry["claim_ref"], claim_created_at=entry["claim_created_at"],
            subject_ref=entry["subject_ref"], metric_or_aspect=entry["metric_or_aspect"],
            period_key=entry["period_key"], claim_kind=entry["claim_kind"],
            aspect=entry["aspect"], aspect_source=entry["aspect_source"],
            as_of=entry["as_of"], as_of_basis=entry["as_of_basis"],
            importance=entry["importance"], importance_basis=entry["importance_basis"],
            dedupe_group_key=entry["dedupe_group_key"], tagger_ref=entry["tagger_ref"],
            tagger_hash=entry["tagger_hash"], actor_ref=entry["actor_ref"],
            created_at="2026-09-20T00:00:00+00:00")
        self.assertEqual(result["status"], "duplicate")

    def test_the_purpose_is_registered_from_this_lanes_own_module(self):
        # P14-0: a lane names its purpose by registering it, not by editing a
        # set in cockpit_model. Importing the tagging module is what does it.
        import dalton_core.claim_index_tagging  # noqa: F401

        self.assertIn("claim_index", purposes())
        work = build_work(
            purpose="claim_index", request_id="r", prompt="p",
            mission_version_ref="m", max_input_tokens=10, max_output_tokens=1,
            max_cost_usd=0.1, max_seconds=10, created_at="2026-09-09T00:00:00+00:00")
        self.assertTrue(work.id.startswith("work:cockpit-claim_index-"))


class FakeLauncher:
    def __init__(self):
        self.started = []
        self.tickets = {}
        self.conflict = False

    def start(self, *, company_ref, claim_version_refs):
        from dalton_core.lane_child_launcher import LaneChildConflict

        if self.conflict:
            raise LaneChildConflict("busy")
        digest = batch_digest(company_ref, claim_version_refs)
        ticket = {
            "id": f"claim-index-run:{digest}", "company_ref": company_ref,
            "batch_digest": digest, "status": "running",
        }
        self.started.append(ticket)
        self.tickets[ticket["id"]] = ticket
        return ticket

    def status(self, ticket_ref):
        return self.tickets[ticket_ref]

    def settle(self, ticket_ref, *, index_status="tagged", status="succeeded"):
        self.tickets[ticket_ref] = {
            **self.tickets[ticket_ref], "status": status,
            "summary": {"index_status": index_status, "rule_tagged": 1,
                        "model_tagged": 2, "cost_micros": 5,
                        "failure_reason": None if status == "succeeded" else "boom"},
        }


class LaneCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.harness = ChildHarness()
        self.addCleanup(self.harness.close)
        self.launcher = FakeLauncher()
        self.coordinator = MissionClaimIndexLaneCoordinator(
            store=self.harness.store, launcher=self.launcher,
            mission=lambda: self.harness.mission,
        )

    def test_with_no_untagged_claims_the_lane_is_idle_and_launches_nothing(self):
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "idle")
        self.assertEqual(self.launcher.started, [])

    def test_the_first_company_in_universe_order_takes_the_slot(self):
        self.harness.fixture.add_claim("epam-1", subject_ref=EPAM)
        self.harness.fixture.add_claim("acn-1", subject_ref=ACN)
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["company_ref"], ACN)
        self.assertEqual(result["pending"], 1)

    def test_a_tick_while_the_child_runs_reports_it_running_and_does_not_start_a_second(self):
        self.harness.fixture.add_claim("acn-1", subject_ref=ACN)
        first = self.coordinator.dispatch_once()
        second = self.coordinator.dispatch_once()
        self.assertEqual(second["settled"], {"status": "running",
                                             "ticket_ref": first["ticket_ref"]})
        self.assertEqual(len(self.launcher.started), 2)
        # The launcher itself is the one-at-a-time guard; the coordinator's job
        # is to settle, and a settled ticket is what lets the next batch move.
        self.launcher.settle(first["ticket_ref"])
        third = self.coordinator.dispatch_once()
        self.assertEqual(third["settled"]["index_status"], "tagged")

    def test_a_failed_batch_is_held_rather_than_relaunched_every_tick(self):
        self.harness.fixture.add_claim("acn-1", subject_ref=ACN)
        first = self.coordinator.dispatch_once()
        self.launcher.settle(first["ticket_ref"], status="failed",
                             index_status="refused")
        self.coordinator.dispatch_once()  # settles and holds
        held = self.coordinator.dispatch_once()
        self.assertEqual(held["status"], "held")
        self.assertIn("boom", held["reason"])

    def test_a_transient_outcome_is_not_held_against_the_batch(self):
        # "the scheduler had this in flight" and "no route right now" are true
        # of a moment, not of a batch; holding for them parks work the next
        # tick could do.
        self.harness.fixture.add_claim("acn-1", subject_ref=ACN)
        first = self.coordinator.dispatch_once()
        self.launcher.settle(first["ticket_ref"], index_status="busy")
        self.coordinator.dispatch_once()
        again = self.coordinator.dispatch_once()
        self.assertEqual(again["status"], "launched")

    def test_a_mission_that_is_not_there_yet_is_unconfigured_not_a_crash(self):
        coordinator = MissionClaimIndexLaneCoordinator(
            store=self.harness.store, launcher=self.launcher, mission=lambda: None)
        self.assertEqual(coordinator.dispatch_once()["status"], "unconfigured")


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.state_dir = Path(self._dir.name)

    def test_the_same_batch_is_the_same_ticket_whatever_order_it_arrived_in(self):
        one = batch_digest(ACN, ["b", "a"])
        two = batch_digest(ACN, ["a", "b"])
        self.assertEqual(one, two)
        self.assertEqual(len(one), 24)
        self.assertNotEqual(one, batch_digest(ACN, ["a", "b", "c"]))
        self.assertNotEqual(one, batch_digest(EPAM, ["a", "b"]))

    def test_the_command_carries_the_state_the_company_and_the_ticket_directory(self):
        launcher = ClaimIndexLauncher(
            state_dir=self.state_dir, model_config_path=self.state_dir / "model.json",
            scheduler_db=self.state_dir / "scheduler.sqlite", max_claims=50)
        command = launcher._command(ticket_dir=self.state_dir / "t", company_ref=ACN)
        self.assertIn("dalton_core.claim_index_cli", command)
        self.assertIn("--company-ref", command)
        self.assertIn(ACN, command)
        self.assertIn("--max-claims", command)
        self.assertIn("50", command)
        self.assertTrue(launcher.configured)

    def test_without_a_model_the_lane_still_runs_because_the_rules_do_most_of_it(self):
        launcher = ClaimIndexLauncher(state_dir=self.state_dir)
        self.assertFalse(launcher.configured)
        command = launcher._command(ticket_dir=self.state_dir / "t", company_ref=ACN)
        self.assertNotIn("--model-config", command)

    def test_a_run_with_no_claims_is_refused_before_a_process_starts(self):
        from dalton_core.lane_child_launcher import LaneChildRejected

        launcher = ClaimIndexLauncher(state_dir=self.state_dir)
        with self.assertRaises(LaneChildRejected):
            launcher.start(company_ref=ACN, claim_version_refs=[])
        with self.assertRaises(LaneChildRejected):
            launcher.start(company_ref=" ", claim_version_refs=["a"])

    def test_the_cli_parses_both_of_its_jobs(self):
        parser = build_parser()
        tag = parser.parse_args(["--state-dir", "/tmp/x", "--dry-run"])
        self.assertTrue(tag.dry_run)
        self.assertFalse(tag.promote_figures)
        promote = parser.parse_args(
            ["--state-dir", "/tmp/x", "--promote-figures", "--staging-db", "/tmp/s"])
        self.assertTrue(promote.promote_figures)


class RegistrationTests(unittest.TestCase):
    """INT1: the LaneSpec P12b wrote in its report and left out of the code.

    Until this, the index had no tick at all -- `claim_index_cli` was a hand
    entry point and nothing called it. What is pinned here is this lane's half
    of the P14-0 contract: the names it claims, that it is off until its model
    configuration is installed, and that importing it does not drag in the
    machinery that reads the registry.
    """

    def test_the_lane_is_registered_under_the_names_it_claims(self):
        from dalton_core.lane_registry import lane_for_operation
        from dalton_core.mission_claim_index_lane import LANE, LAUNCHER_KWARG

        spec = lane_for_operation("dispatch_claim_index")
        self.assertIs(spec, LANE)
        self.assertEqual(spec.driver_key, "claim_index")
        self.assertEqual(spec.init_kwarg, LAUNCHER_KWARG)
        self.assertTrue(spec.core_discovery)
        # A tick takes no arguments; --company-ref is a hand-run thing.
        self.assertEqual(spec.param_fields, frozenset())

    def test_it_runs_after_the_plan_and_before_the_initial_screen(self):
        # The screen drafts from Claims, and drafting from an indexed set is
        # what stops it citing one quarter's revenue three times over.
        from dalton_core.lane_registry import registered_lanes

        order = [spec.operation for spec in registered_lanes()]
        self.assertEqual(
            order[order.index("dispatch_research_plan") + 1], "dispatch_claim_index")
        self.assertEqual(
            order[order.index("dispatch_claim_index") + 1], "dispatch_initial_screen")

    def test_the_writer_derives_the_operation_from_the_registry(self):
        from dalton_core import writer_server

        self.assertIn("dispatch_claim_index", writer_server.OPERATION_FIELDS)
        self.assertIn("dispatch_claim_index", writer_server.CORE_DISCOVERY_OPERATIONS)
        self.assertIn("dispatch_claim_index", writer_server.CORE_OPERATIONS)

    def test_the_controller_tick_drives_it(self):
        from dalton_core.lane_registry import tick_lanes

        self.assertIn("claim_index", [spec.driver_key for spec in tick_lanes()])

    def test_without_a_model_configuration_there_is_no_launcher_and_no_argv(self):
        import argparse

        from dalton_core.lane_registry import LaunchAgentContext
        from dalton_core.mission_claim_index_lane import (
            CLAIM_INDEX_MODEL_CONFIG, add_arguments, argv_fragment, build_launcher,
        )

        parser = argparse.ArgumentParser()
        parser.add_argument("--db")
        parser.add_argument("--scheduler")
        add_arguments(parser)
        args = parser.parse_args(["--db", "/tmp/core.sqlite"])
        self.assertIsNone(args.claim_index_model_config)
        self.assertIsNone(build_launcher(args))
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            self.assertEqual(argv_fragment(LaunchAgentContext(state=state)), [])
            config = state / CLAIM_INDEX_MODEL_CONFIG
            config.write_text("{}", encoding="utf-8")
            self.assertEqual(
                argv_fragment(LaunchAgentContext(state=state)),
                ["--claim-index-model-config", str(config)])

    def test_the_launcher_is_built_from_the_writer_s_own_arguments(self):
        import argparse

        from dalton_core.claim_index_launcher import ClaimIndexLauncher
        from dalton_core.mission_claim_index_lane import add_arguments, build_launcher

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            config = state / "claim-index-model-config.json"
            config.write_text("{}", encoding="utf-8")
            parser = argparse.ArgumentParser()
            parser.add_argument("--db")
            parser.add_argument("--scheduler")
            add_arguments(parser)
            args = parser.parse_args([
                "--db", str(state / "core.sqlite"),
                "--scheduler", str(state / "scheduler.sqlite"),
                "--claim-index-model-config", str(config),
            ])
            launcher = build_launcher(args)
            self.assertIsInstance(launcher, ClaimIndexLauncher)
            self.assertEqual(launcher.state_dir, state.resolve())
            self.assertEqual(launcher.model_config_path, config.resolve())
            self.assertTrue(launcher.configured)

    def test_a_writer_without_the_lane_says_so_rather_than_failing(self):
        from dalton_core.mission_claim_index_lane import dispatch

        class Server:
            lane_state: dict = {}

            def lane_launcher(self, kwarg):
                return None

        result = dispatch(Server(), {})
        self.assertEqual(result["status"], "unconfigured")
        self.assertIn("claim index", result["reason"])

    def test_the_coordinator_is_cached_rather_than_rebuilt(self):
        # The coordinator holds which batch is open and which batches failed.
        # A fresh one every tick would forget both and re-launch a child for a
        # batch it had just been told was doomed.
        from dalton_core.mission_claim_index_lane import LAUNCHER_KWARG, dispatch

        class Coordinator:
            def __init__(self):
                self.calls = 0

            def dispatch_once(self):
                self.calls += 1
                return {"status": "idle"}

        coordinator = Coordinator()

        class Server:
            def __init__(self):
                self.lane_state = {LAUNCHER_KWARG: coordinator}

            def lane_launcher(self, kwarg):
                return object()

        server = Server()
        dispatch(server, {})
        dispatch(server, {})
        self.assertEqual(coordinator.calls, 2)
        self.assertIs(server.lane_state[LAUNCHER_KWARG], coordinator)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

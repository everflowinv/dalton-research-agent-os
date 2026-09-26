"""P12c: the child checks the grant before it spends, and the lane goes quiet.

The lane is queueless and its resting state is the usual one: a map is redrawn
when the evidence it was drawn from stops being the evidence we hold, which is
derived from the Ledger every tick.  So the interesting assertion is not "the
queue drained" but "after a run the subject stops being chosen", which is the
invariant a lane keyed on anything other than the evidence itself would break.
"""

from __future__ import annotations

import json
import copy
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.debate_map import DebateMapAuthority, evidence_fingerprint
from dalton_core.debate_map_cli import (
    WRITE_SCOPE, build_parser, can_rebind, granted, run_debate_map, subject_kind_for,
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
    LAUNCHER_KWARG, MissionDebateMapLaneCoordinator, _business_key, argv_fragment,
    build_launcher,
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


class ContractRecoveryIdentityTests(unittest.TestCase):
    def test_contract_change_releases_same_business_input(self):
        mission = {"id": "mission:v1", "content_hash": "a" * 64}
        current = _business_key("company:a", "b" * 64, mission)
        with patch("dalton_core.debate_map_draft.DRAFT_CONTRACT_HASH", "c" * 64):
            repaired = _business_key("company:a", "b" * 64, mission)
        self.assertNotEqual(current, repaired)
        self.assertEqual(current.split("|contract:")[0], repaired.split("|contract:")[0])


class NoveltyRuleIdentityTests(unittest.TestCase):
    """2026-09-26: a duplicate reached under an old novelty rule is not final."""

    MISSION = {"id": "mission:v1", "content_hash": "a" * 64}

    def test_the_novelty_rule_is_part_of_the_business_key(self):
        current = _business_key("company:msft", "b" * 64, self.MISSION)
        self.assertIn("|novelty:", current)
        with patch("dalton_core.debate_map.NOVELTY_RULE_VERSION", "next-rule"):
            changed = _business_key("company:msft", "b" * 64, self.MISSION)
        self.assertNotEqual(current, changed)
        self.assertEqual(current.split("|novelty:")[0], changed.split("|novelty:")[0])

    def test_a_pre_rule_duplicate_does_not_hold_the_subject_under_the_new_rule(self):
        from dalton_core.lane_failure_ledger import lane_budget
        from dalton_core.lane_permission_control import record_controlled_failure

        with tempfile.TemporaryDirectory() as directory:
            with patch("dalton_core.debate_map.NOVELTY_RULE_VERSION", "before"):
                old_key = _business_key("company:msft", "b" * 64, self.MISSION)
            budget = lane_budget("mission_debate_map", state_dir=directory)
            record_controlled_failure(
                budget, old_key, self.MISSION, None,
                reason="last run: duplicate", status="duplicate")
            self.assertEqual(budget.blocked(old_key).action, "terminal")
            # Replayed after a restart as well: the ledger is durable.
            replayed = lane_budget("mission_debate_map", state_dir=directory)
            self.assertEqual(replayed.blocked(old_key).action, "terminal")
            new_key = _business_key("company:msft", "b" * 64, self.MISSION)
            self.assertIsNone(replayed.blocked(new_key))
            # And under an unchanged rule the duplicate still holds: no clock
            # re-asks a question whose answer cannot have changed.
            record_controlled_failure(
                replayed, new_key, self.MISSION, None,
                reason="last run: duplicate", status="duplicate")
            self.assertEqual(replayed.blocked(new_key).action, "terminal")

    def test_changing_novelty_means_bumping_its_version(self):
        import hashlib
        import inspect

        from dalton_core.debate_map import NOVELTY_RULE_VERSION, novelty

        digest = hashlib.sha256(inspect.getsource(novelty).encode()).hexdigest()
        # If this fails you changed ``debate_map.novelty``.  Bump
        # NOVELTY_RULE_VERSION (so every duplicate held under the old rule is
        # asked once more) and then re-pin both values here.
        self.assertEqual(
            (NOVELTY_RULE_VERSION, digest),
            ("2026-09-25.retired-withdrawn",
             "1d0b29e2dd8044b03bbe44de6c0430b3b38b8e216fae6c35e0afc9828b9173c0"))


class IndustryReattributionUrgencyTests(unittest.TestCase):
    """2026-09-26: a live industry reattribution is not a retirement for the industry map."""

    INDUSTRY = "industry:it-services"
    MISSION = {"id": "mission:v1", "content_hash": "a" * 64,
               "industry_ref": "industry:it-services"}

    def _coordinator(self, pacing_dir):
        from types import SimpleNamespace

        coordinator = object.__new__(MissionDebateMapLaneCoordinator)
        coordinator.store = SimpleNamespace(connection=object())
        from dalton_core.mission_debate_map_lane import RedrawPacing

        coordinator.pacing = RedrawPacing(Path(pacing_dir) / "pacing.json")
        coordinator.clock = lambda: datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
        return coordinator

    def test_the_industry_subject_does_not_count_its_own_reattributions(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self._coordinator(directory)
            retired = {"claim:acn:1", "claim:acn:2"}
            with patch("dalton_core.claim_industry_reattribution."
                       "reattributed_claim_version_refs",
                       return_value={"claim:acn:1"}) as live:
                self.assertEqual(
                    coordinator._retired_for(self.INDUSTRY, self.MISSION, retired),
                    {"claim:acn:2"})
                live.assert_called_once_with(coordinator.store.connection, self.INDUSTRY)
                # A company keeps the company-level answer.
                self.assertEqual(
                    coordinator._retired_for("company:acn", self.MISSION, retired),
                    retired)

    def test_an_unreadable_reattribution_table_is_no_urgency(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self._coordinator(directory)
            with patch("dalton_core.claim_industry_reattribution."
                       "reattributed_claim_version_refs",
                       side_effect=sqlite3.OperationalError("locked")):
                self.assertEqual(coordinator._retired_for(
                    self.INDUSTRY, self.MISSION, {"claim:acn:1"}), set())

    def test_a_still_valid_reattribution_keeps_the_six_hour_minimum(self):
        # The industry map cites claim:acn:1, retired for ACN but reattributed
        # to the industry.  Drawn two hours ago: no urgency, so it waits.
        current = {"debates": [{"bull": {"claim_refs": ["claim:acn:1"]},
                                "bear": {"claim_refs": []}}]}
        with tempfile.TemporaryDirectory() as directory:
            coordinator = self._coordinator(directory)
            coordinator.pacing.put(self.INDUSTRY, {
                "last_draft_at": "2026-09-26T10:00:00+00:00", "failures": 0})
            with patch("dalton_core.claim_industry_reattribution."
                       "reattributed_claim_version_refs",
                       return_value={"claim:acn:1"}), \
                    patch("dalton_core.debate_map.cited_refs",
                          return_value={"claim:acn:1"}):
                retired = coordinator._retired_for(
                    self.INDUSTRY, self.MISSION, {"claim:acn:1"})
                urgent = coordinator._retired_cited(current, retired)
                self.assertIsNone(urgent)
                self.assertIn("next redraw is due",
                              coordinator._paced(self.INDUSTRY, urgent))
                # Withdraw the reattribution and the same citation is urgent.
                urgent = coordinator._retired_cited(current, {"claim:acn:1"})
                self.assertEqual(urgent, "retired:claim:acn:1")
                self.assertIsNone(coordinator._paced(self.INDUSTRY, urgent))


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
        self.params = copy.deepcopy(params)
        self.mission = self.missions.create_mission(params.pop("mission_ref"), **params)

    def roll_mission(self, *, grant=True):
        params = copy.deepcopy(self.params)
        if not grant:
            params["autonomy"]["may_write"] = [
                item for item in params["autonomy"]["may_write"]
                if item != WRITE_SCOPE]
        params.update({
            "version_id": self.mission["id"].rsplit(":", 1)[0] + ":2",
            "prior_version_ref": self.mission["id"],
            "idempotency_key": "fixture:debate-map:mission:2",
        })
        self.mission = self.missions.create_mission(
            params.pop("mission_ref"), **params)
        return self.mission

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

    def test_mission_only_rebind_appends_without_another_model_call(self):
        self.test_a_verified_draft_becomes_a_first_version()
        mission = self.harness.roll_mission()
        with patch("dalton_core.debate_map_cli.CockpitModel",
                   side_effect=AssertionError("must not call a model")):
            summary = self.harness.run(dry_run=False, model_config_path=None)
        self.assertEqual(summary["map_status"], "fresh")
        self.assertEqual(summary["cost_micros"], 0)
        current = DebateMapAuthority(self.harness.store).current(ACN)
        self.assertEqual(current["version"], 2)
        self.assertEqual(current["change_reason"], "mission_rebind")
        self.assertEqual(current["mission_version_ref"], mission["id"])

    def _redraw(self, *, bull, bear, debate_ref="new-1"):
        """Draft a map citing the claims whose statements are given, by row id."""

        def reply(prompt):
            ids = {line.split("\t")[-1]: line.split("\t")[0].strip()
                   for line in prompt.splitlines() if line.strip().startswith("C")
                   and "\t" in line}
            body = json.loads(draft_reply())
            debate = body["debates"][0]
            debate["debate_ref"] = debate_ref
            debate["bull"]["claim_refs"] = [ids[text] for text in bull]
            debate["bear"]["claim_refs"] = [ids[text] for text in bear]
            return json.dumps(body)

        class PromptModel(FakeModel):
            def call(inner, *, purpose, request_id, prompt, mission):
                result = FakeModel.call(inner, purpose=purpose, request_id=request_id,
                                        prompt=prompt, mission=mission)
                if callable(result["text"]):
                    result["text"] = result["text"](prompt)
                return result

        model = PromptModel([reply, PASS])
        with patch("dalton_core.debate_map_cli.CockpitModel", model), \
                patch("dalton_core.debate_map_cli.route_family", fake_family):
            return self.harness.run(dry_run=False,
                                    model_config_path=self.harness.with_model())

    def test_a_mission_bump_that_withdraws_a_retired_claim_publishes(self):
        # 2026-09-25: under a changed mission every redraw went out as
        # mission_rebind, which the authority admits only for an identical
        # map, so each paid redraw was a duplicate (legacy 123 since 09-14,
        # ws-7d 30 today) and AMZN v5 / GOOGL v2 / MSFT v1 kept citing Claims
        # retired since.  A mission bump plus a withdrawn retired ref is an
        # ordinary new version, bound to the current mission.
        bull, bull2 = ("Bookings are accelerating into next year.",
                       "Backlog is at a record.")
        bear = "Discretionary demand is decelerating."
        self.harness.add_claims()
        retiring = self.harness.fixture.add_claim(
            "debate-bull2", kind="qualitative", value=None, unit=None,
            metric="demand environment", period="current", statement=bull2,
            source_type="authenticated_transcript")
        first = self._redraw(bull=[bull, bull2], bear=[bear])
        self.assertEqual(first["map_status"], "fresh")
        authority = DebateMapAuthority(self.harness.store)
        v1 = authority.current(ACN)
        retired_ref = retiring["claim_version_id"]
        self.assertIn(retired_ref, v1["debates"][0]["bull_position"]["claim_refs"])

        mission = self.harness.roll_mission()
        with patch("dalton_core.claim_retirement.retired_claim_version_refs",
                   return_value={retired_ref}):
            summary = self._redraw(bull=[bull], bear=[bear],
                                   debate_ref=v1["debates"][0]["debate_ref"])
        self.assertEqual((summary["status"], summary["map_status"]),
                         ("succeeded", "fresh"), summary)
        current = authority.current(ACN)
        self.assertEqual(current["version"], 2)
        self.assertEqual(current["change_reason"], "evidence_thicker")
        self.assertEqual(current["mission_version_ref"], mission["id"])
        self.assertNotIn(retired_ref, current["debates"][0]["bull_position"]["claim_refs"])
        self.assertTrue(current["change_evidence_refs"])

    def test_a_mission_bump_with_a_new_ref_publishes_as_evidence_thicker(self):
        bull, bull2 = ("Bookings are accelerating into next year.",
                       "Backlog is at a record.")
        bear = "Discretionary demand is decelerating."
        self.harness.add_claims()
        self._redraw(bull=[bull], bear=[bear])
        v1 = DebateMapAuthority(self.harness.store).current(ACN)
        mission = self.harness.roll_mission()
        added = self.harness.fixture.add_claim(
            "debate-bull2", kind="qualitative", value=None, unit=None,
            metric="demand environment", period="current", statement=bull2,
            source_type="authenticated_transcript")
        summary = self._redraw(bull=[bull, bull2], bear=[bear],
                               debate_ref=v1["debates"][0]["debate_ref"])
        self.assertEqual(summary["map_status"], "fresh", summary)
        current = DebateMapAuthority(self.harness.store).current(ACN)
        self.assertEqual(current["change_reason"], "evidence_thicker")
        self.assertEqual(current["change_evidence_refs"], [added["claim_version_id"]])
        self.assertEqual(current["mission_version_ref"], mission["id"])

    def test_a_mission_bump_whose_redraw_is_unchanged_rebinds_the_current_map(self):
        # New evidence arrived (so the no-model rebind did not apply) but the
        # paid redraw came out identical: bind the current content to the new
        # mission rather than refuse it as a duplicate.
        bull = "Bookings are accelerating into next year."
        bear = "Discretionary demand is decelerating."
        self.harness.add_claims()
        self._redraw(bull=[bull], bear=[bear])
        authority = DebateMapAuthority(self.harness.store)
        v1 = authority.current(ACN)
        mission = self.harness.roll_mission()
        self.harness.fixture.add_claim(
            "debate-other", kind="qualitative", value=None, unit=None,
            metric="demand environment", period="current",
            statement="Pricing is stable.", source_type="authenticated_transcript")
        summary = self._redraw(bull=[bull], bear=[bear],
                               debate_ref=v1["debates"][0]["debate_ref"])
        self.assertEqual(summary["map_status"], "fresh", summary)
        current = authority.current(ACN)
        self.assertEqual(current["version"], 2)
        self.assertEqual(current["change_reason"], "mission_rebind")
        self.assertEqual(current["mission_version_ref"], mission["id"])
        self.assertEqual(current["debates"], v1["debates"])
        self.assertEqual(current["evidence_fingerprint"], v1["evidence_fingerprint"])

    def test_a_rebind_after_an_evidence_thicker_version_is_not_a_duplicate(self):
        # The no-model rebind named every cited ref as its change refs; after
        # an evidence_thicker version (whose change refs are only the new
        # ones) that differed, and the rebind was refused as a duplicate.
        bull, bull2 = ("Bookings are accelerating into next year.",
                       "Backlog is at a record.")
        bear = "Discretionary demand is decelerating."
        self.harness.add_claims()
        self._redraw(bull=[bull], bear=[bear])
        v1 = DebateMapAuthority(self.harness.store).current(ACN)
        self.harness.fixture.add_claim(
            "debate-bull2", kind="qualitative", value=None, unit=None,
            metric="demand environment", period="current", statement=bull2,
            source_type="authenticated_transcript")
        self._redraw(bull=[bull, bull2], bear=[bear],
                     debate_ref=v1["debates"][0]["debate_ref"])
        v2 = DebateMapAuthority(self.harness.store).current(ACN)
        self.assertEqual(v2["version"], 2)
        mission = self.harness.roll_mission()
        with patch("dalton_core.debate_map_cli.CockpitModel",
                   side_effect=AssertionError("must not call a model")):
            summary = self.harness.run(dry_run=False, model_config_path=None)
        self.assertEqual(summary["map_status"], "fresh", summary)
        current = DebateMapAuthority(self.harness.store).current(ACN)
        self.assertEqual((current["version"], current["change_reason"]),
                         (3, "mission_rebind"))
        self.assertEqual(current["mission_version_ref"], mission["id"])

    def test_rebind_requires_the_same_constitution_and_policy(self):
        previous = {
            "mission_version_ref": "mission:old", "evidence_fingerprint": "f",
            "constitution_ref": "constitution:1", "constitution_hash": "1" * 64,
            "policy_ref": "wrong-policy", "policy_hash": "2" * 64,
        }
        mission = {"id": "mission:new"}
        constitution = {"id": "constitution:1", "content_hash": "1" * 64}
        self.assertFalse(can_rebind(previous, mission, constitution, "f"))
        previous["policy_ref"] = "debate-policy:p12c:v1"
        previous["policy_hash"] = __import__(
            "dalton_core.debate_map", fromlist=["POLICY_HASH"]).POLICY_HASH
        constitution["content_hash"] = "3" * 64
        self.assertFalse(can_rebind(previous, mission, constitution, "f"))

    def test_a_new_mission_without_the_grant_cannot_rebind(self):
        self.test_a_verified_draft_becomes_a_first_version()
        self.harness.roll_mission(grant=False)
        summary = self.harness.run(dry_run=False, model_config_path=None)
        self.assertEqual(summary["map_status"], "not_authorized")
        self.assertEqual(DebateMapAuthority(self.harness.store).counts()["versions"], 1)

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
        self.assertEqual(summary["status"], "failed")
        self.assertTrue(summary["failure_reason"])
        self.assertIsNone(DebateMapAuthority(self.harness.store).current(ACN))

    def test_an_unresolvable_family_publishes_nothing(self):
        self.harness.add_claims()
        config = self.harness.with_model()
        model = FakeModel([draft_reply(), PASS])
        with patch("dalton_core.debate_map_cli.CockpitModel", model):
            summary = self.harness.run(dry_run=False, model_config_path=config)
        self.assertEqual(summary["map_status"], "not_independent")
        self.assertEqual(summary["status"], "failed")
        self.assertTrue(summary["failure_reason"])
        self.assertIsNone(DebateMapAuthority(self.harness.store).current(ACN))

    def test_a_verifier_that_does_not_run_is_a_failed_summary(self):
        self.harness.add_claims()
        config = self.harness.with_model()
        model = FakeModel([draft_reply(), PASS])
        original_call = model.call
        def fail_verifier(**kwargs):
            if len(model.prompts) == 1:
                raise CockpitModelError("provider contract refused")
            return original_call(**kwargs)
        model.call = fail_verifier
        with patch("dalton_core.debate_map_cli.CockpitModel", model), \
                patch("dalton_core.debate_map_cli.route_family", fake_family):
            summary = self.harness.run(dry_run=False, model_config_path=config)
        self.assertEqual((summary["status"], summary["map_status"]),
                         ("failed", "unverified"))
        self.assertIn("provider contract refused", summary["failure_reason"])

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
        self.now = datetime(2026, 9, 25, 6, 13, tzinfo=timezone.utc)
        self.coordinator = MissionDebateMapLaneCoordinator(
            store=self.harness.store, launcher=self.launcher,
            mission=lambda: self.harness.mission, clock=lambda: self.now,
        )

    def _new_claim(self, key, statement="Another view arrives."):
        self.harness.fixture.add_claim(
            key, kind="qualitative", value=None, unit=None,
            metric="demand environment", period="current",
            statement=statement, source_type="public_web")

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
            mission_version_ref=self.harness.mission["id"],
            mission_version_hash=self.harness.mission["content_hash"],
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
            actor_ref=self.harness.mission["autonomy"]["automation_principal"],
            created_at="2026-09-09T00:00:00+00:00",
        )
        self.assertEqual(self.coordinator.dispatch_once()["status"], "idle")

    def test_a_new_mission_launches_even_when_the_claim_fingerprint_is_unchanged(self):
        self.test_a_map_drawn_from_the_evidence_we_hold_is_left_alone()
        self.harness.mission = {
            **self.harness.mission,
            "id": "coverage-mission-version:us-it-services:next",
            "content_hash": "9" * 64,
        }
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["subject_ref"], ACN)

    def test_a_failed_run_holds_that_evidence_back_but_not_forever(self):
        self.harness.add_claims()
        launched = self.coordinator.dispatch_once()
        self.launcher.settle(launched["ticket_ref"],
                             {"map_status": "refused", "failure_reason": "bad draft"})
        held = self.coordinator.dispatch_once()
        self.assertEqual(held["status"], "held")
        self.assertEqual(held["reason"], "bad draft")
        self.assertEqual(held["settled"]["map_status"], "refused")
        # A new Claim changes the fingerprint, so the hold does not survive
        # it -- but the failed draft is backed off first (2026-09-25), so the
        # next one waits out the six hours rather than following every Claim.
        self._new_claim("debate-third", "A third view arrives.")
        paced = self.coordinator.dispatch_once()
        self.assertEqual(paced["status"], "held")
        self.assertIn("backing off after 1 draft", paced["reason"])
        self.now += timedelta(hours=6, seconds=1)
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
        self._new_claim("debate-after-duplicate", "Something new arrives.")
        self.assertEqual(self.coordinator.dispatch_once()["status"], "held")
        self.now += timedelta(hours=6, seconds=1)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")

    def test_redraws_are_paced_and_failures_back_off(self):
        # 2026-09-25: AMZN drafted eleven times in 72 minutes, each new Claim
        # launching another ~$0.52 draft that came back duplicate or invalid.
        self.harness.add_claims()
        launched = []
        for index in range(6):
            result = self.coordinator.dispatch_once()
            if result["status"] == "launched":
                launched.append(self.now)
                self.launcher.settle(result["ticket_ref"], {"map_status": "refused",
                                                            "failure_reason": "invalid JSON"})
            self._new_claim(f"debate-burst-{index}", f"View {index}.")
            self.now += timedelta(minutes=7)
        self.assertEqual(len(launched), 1)
        # Backoff doubles: 6 h after the first failure, then 12 h.
        start = launched[0]
        self.now = start + timedelta(hours=6, seconds=1)
        second = self.coordinator.dispatch_once()
        self.assertEqual(second["status"], "launched")
        self.launcher.settle(second["ticket_ref"], {"map_status": "duplicate"})
        self._new_claim("debate-burst-late", "Later view.")
        self.now += timedelta(hours=11)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "held")
        self.now += timedelta(hours=1, seconds=1)
        third = self.coordinator.dispatch_once()
        self.assertEqual(third["status"], "launched")
        # A published draft resets the backoff to the plain minimum interval.
        self.launcher.settle(third["ticket_ref"], {"map_status": "fresh"})
        self._new_claim("debate-burst-after", "After publishing.")
        self.now += timedelta(hours=5)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "held")
        self.now += timedelta(hours=1, seconds=1)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")

    def test_a_published_map_standing_on_a_retired_claim_is_redrawn_at_once(self):
        from unittest.mock import patch

        self.test_a_map_drawn_from_the_evidence_we_hold_is_left_alone()
        from dalton_core.debate_map import cited_refs

        cited = sorted(cited_refs(
            DebateMapAuthority(self.harness.store).current(ACN)))
        # Drawn a minute ago: an ordinary new Claim waits out the six hours...
        self.coordinator.pacing.put(ACN, {"last_draft_at": self.now.isoformat(), "failures": 0})
        self._new_claim("debate-retire-1", "An ordinary new view.")
        self.assertEqual(self.coordinator.dispatch_once()["status"], "held")
        # ...but a retired Claim under the published map does not.
        with patch("dalton_core.claim_retirement.retired_claim_version_refs",
                   return_value={cited[0]}):
            urgent = self.coordinator.dispatch_once()
            self.assertEqual(urgent["status"], "launched")
            self.launcher.settle(urgent["ticket_ref"], {"map_status": "refused"})
            # Once per retired set, and a failed attempt still backs off.
            self._new_claim("debate-retire-2", "Yet another view.")
            self.assertEqual(self.coordinator.dispatch_once()["status"], "held")

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

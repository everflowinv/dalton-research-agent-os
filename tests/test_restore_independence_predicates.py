"""A cockpit budget edit dropped the producer/verifier independence predicate.

legacy lost it at ``policy-14``, ws-7d at ``policy-2``.  The restore script
publishes the next policy version with the fresh-Core default predicate and
nothing else changed, and cascades the constitution and the mission.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.policy import DEFAULT_POLICY
from dalton_core.research_constitution import ResearchConstitutionAuthority
from dalton_core.store import DaltonStore
from dalton_core.writer_protocol import RemoteError
from scripts import restore_independence_predicates as restore
from scripts.restore_independence_predicates import (
    DEFAULT_PREDICATES,
    FIELD,
    PlanError,
    build_chain,
    main,
    plan,
    publish_cascade,
    rehearse,
)
from scripts.sign_auto_commit_rules import read_current
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

MISSION_REF = "coverage-mission:ws-fixture"
#: ``setUp`` patches ``route_check`` out; this is the real one.
ROUTE_CHECK = restore.route_check
OWNER = "human:fixture-owner"
#: What a fresh Core's policy-1 carries (dalton_core.policy.DEFAULT_POLICY).
FRESH_CORE_PREDICATE = [{"left_path": "producer.model_family", "operator": "ne",
                         "right_path": "verifier.model_family"}]


def _authority_apply(store: DaltonStore, actor: str):
    constitutions = ResearchConstitutionAuthority(store)
    missions = CoverageMissionAuthority(store)

    def apply(operation: str, params: dict) -> dict:
        values = copy.deepcopy(dict(params))
        if operation == "create_policy":
            policy = values.pop("policy")
            return {"policy_version": store.create_policy(policy, actor_ref=actor, **values)}
        if operation == "publish_research_constitution":
            return constitutions.publish_constitution(
                values.pop("constitution_ref"), actor_ref=actor, **values)
        if operation == "create_coverage_mission":
            return missions.create_mission(values.pop("mission_ref"), actor_ref=actor, **values)
        raise AssertionError(operation)

    return apply


def _policy_hash(store: DaltonStore, policy_id: str) -> str:
    return store.connection.execute(
        "SELECT content_hash FROM governance_policy_versions WHERE policy_version_id=?",
        (policy_id,)).fetchone()[0]


def drop_predicates_like_the_budget_edit(state_dir: Path) -> None:
    """Publish what the pre-b6bc016a budget edit published: ``policy`` alone."""

    store = DaltonStore(str(state_dir / "core.sqlite"))
    try:
        current = read_current(store.connection)
        # ``to_dict`` keeps the predicates beside ``policy``, not in it.
        bare = store.active_policy_version().to_dict()["policy"]
        bare["research_budget"] = {"max_daily_cost_usd": 5}
        shaped = dict(current, policy={**bare, FIELD: []})
        chain = build_chain(shaped, now="2026-09-26T00:00:00+00:00",
                            policy_hash=_policy_hash(store, current["policy_id"]))
        chain["policy"]["policy"] = bare
        chain["policy"]["change_reason"] = "owner updated the canonical research budget"
        publish_cascade(chain, _authority_apply(store, OWNER))
    finally:
        store.close()


class RestoreIndependencePredicatesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name) / "state"
        self.state_dir.mkdir()
        store = DaltonStore(str(self.state_dir / "core.sqlite"))
        try:
            seeded = bootstrap_method_authorities(store)
            params = mission_params(seeded)
            params.pop("mission_ref")
            params["version_id"] = "coverage-mission-version:ws-fixture:1"
            params["idempotency_key"] = "ws-fixture:1"
            CoverageMissionAuthority(store).create_mission(MISSION_REF, **params)
        finally:
            store.close()
        drop_predicates_like_the_budget_edit(self.state_dir)
        # No model configuration in the fixture: the route check reads nothing.
        patcher = mock.patch.object(restore, "route_check", return_value={
            "status": "ok", "policy_gated": [], "router_enforced": [], "affected_routes": []})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _current(self, state_dir: Path | None = None) -> dict:
        connection = sqlite3.connect(
            f"file:{(state_dir or self.state_dir) / 'core.sqlite'}?mode=ro", uri=True)
        try:
            return read_current(connection)
        finally:
            connection.close()

    # -- the predicate itself -------------------------------------------------

    def test_the_restored_predicate_is_the_fresh_core_default(self):
        self.assertEqual(DEFAULT_PREDICATES, FRESH_CORE_PREDICATE)
        self.assertEqual(DEFAULT_PREDICATES, DEFAULT_POLICY[FIELD])
        with tempfile.TemporaryDirectory() as fresh:
            store = DaltonStore(str(Path(fresh) / "core.sqlite"))
            try:
                policy_one = json.loads(store.connection.execute(
                    "SELECT version_json FROM governance_policy_versions "
                    "WHERE policy_version_id='policy-1'").fetchone()[0])
            finally:
                store.close()
        self.assertEqual(policy_one[FIELD], DEFAULT_PREDICATES)

    def test_the_fixture_reproduces_the_dropped_gate(self):
        current = self._current()
        self.assertEqual(current["policy"][FIELD], [])
        self.assertEqual(current["constitution"]["bindings"]["governance_policy_version"]["ref"],
                         current["policy_id"])

    # -- dry-run ---------------------------------------------------------------

    def test_dry_run_says_would_publish_and_writes_nothing(self):
        before = self._current()
        result = plan(self.state_dir)
        self.assertEqual(result["status"], "would-publish")
        self.assertEqual(result["active_policy"], before["policy_id"])
        number = int(before["policy_id"].rsplit("-", 1)[1])
        self.assertEqual(result["next_policy"], f"policy-{number + 1}")
        self.assertEqual(len(result["publishes"]), 3)
        self.assertEqual(result["diff"][f"policy.{FIELD}"],
                         {"before": [], "after": FRESH_CORE_PREDICATE})
        self.assertEqual(result["affected_routes"], [])
        self.assertEqual(result["historical_pairs_that_would_fail"], 0)
        self.assertEqual(set(result["gate_consumers"]), {
            "thesis_commit", "claim_adjudication", "capability_evaluation",
            "thesis_impact_verification"})
        self.assertEqual(self._current(), before)

    def test_dry_run_lists_affected_policy_gated_routes(self):
        row = {"producer": "thesis_impact_assessment", "verifier": "thesis_impact_verifier",
               "shared_families": ["google-gemini-3"], "verdict": "at-risk"}
        with mock.patch.object(restore, "route_check", return_value={
                "status": "ok", "policy_gated": [row], "router_enforced": [],
                "affected_routes": [row]}):
            result = plan(self.state_dir)
        self.assertEqual(result["affected_routes"], [row])

    def test_route_check_only_counts_policy_gated_pairs_as_affected(self):
        chains = {
            "thesis_impact_assessment": {"chain": [{"profile_id": "a", "family": "anthropic"},
                                                   {"profile_id": "g", "family": "gemini"}]},
            "thesis_impact_verifier": {"chain": [{"profile_id": "g2", "family": "gemini"}]},
            "document_extraction": {"chain": [{"profile_id": "d", "family": "deepseek"},
                                              {"profile_id": "g", "family": "gemini"}]},
            "claim_support_verifier": {"chain": [{"profile_id": "g2", "family": "gemini"}]},
            "dossier": {"chain": [{"profile_id": "a", "family": "anthropic"}]},
            "dossier_verifier": {"chain": [{"profile_id": "g2", "family": "gemini"}]},
        }
        with mock.patch.object(restore, "_stage_chains", return_value=chains):
            result = ROUTE_CHECK(self.state_dir)
        gated = {(r["producer"], r["verifier"]): r for r in result["policy_gated"]}
        enforced = {(r["producer"], r["verifier"]): r for r in result["router_enforced"]}
        self.assertTrue(gated[("thesis_impact_assessment", "thesis_impact_verifier")]
                        ["verdict"].startswith("at-risk"))
        self.assertEqual([(r["producer"], r["verifier"]) for r in result["affected_routes"]],
                         [("thesis_impact_assessment", "thesis_impact_verifier")])
        self.assertTrue(enforced[("document_extraction", "claim_support_verifier")]
                        ["verdict"].startswith("router-enforced"))
        self.assertEqual(enforced[("dossier", "dossier_verifier")]["verdict"], "independent")

    # -- rehearse --------------------------------------------------------------

    def test_rehearsal_restores_only_the_predicate_and_cascades(self):
        before = self._current()
        target = Path(self.temp.name) / "rehearsal"
        result = rehearse(self.state_dir, target, actor=OWNER)
        self.assertEqual(result[f"active_{FIELD}"], FRESH_CORE_PREDICATE)
        self.assertTrue(result["matches_fresh_core_default"])
        self.assertTrue(result["other_policy_fields_unchanged"])
        self.assertTrue(result["constitution_binds_new_policy"])
        self.assertTrue(result["mission_binds_new_constitution"])
        self.assertTrue(result["mandate_unchanged"])
        self.assertEqual(result["parity_row_after"]["status"], "ok")
        self.assertFalse(result["gate_probe"]["same_family"]["allowed"])
        self.assertTrue(result["gate_probe"]["different_family"]["allowed"])
        after = self._current(target)
        self.assertEqual({k: v for k, v in after["policy"].items() if k != FIELD},
                         {k: v for k, v in before["policy"].items() if k != FIELD})
        strip = lambda m: {k: v for k, v in m.items() if k not in {  # noqa: E731
            "id", "version", "content_hash", "bindings", "prior_version_ref",
            "created_at", "actor_ref", "idempotency_key", "status"}}
        self.assertEqual(strip(after["mission"]), strip(before["mission"]))
        self.assertEqual(strip(after["constitution"]), strip(before["constitution"]))
        # The source is only read.
        self.assertEqual(self._current(), before)
        again = plan(target)
        self.assertEqual((again["status"], again["publishes"]), ("already-restored", []))
        with self.assertRaisesRegex(PlanError, "already carries"):
            rehearse(target, Path(self.temp.name) / "second", actor=OWNER)

    # -- apply (through a writer stand-in) --------------------------------------

    def _writer(self, *, fail_after: set[str] = frozenset(), fail_before: set[str] = frozenset()):
        store = DaltonStore(str(self.state_dir / "core.sqlite"))
        self.addCleanup(store.close)
        apply = _authority_apply(store, OWNER)
        calls: list[str] = []

        def call(operation: str, params: dict):
            calls.append(operation)
            if operation in fail_before and calls.count(operation) == 1:
                raise RemoteError("transport_error", "writer service is unavailable")
            result = apply(operation, params)
            if operation in fail_after and calls.count(operation) == 1:
                # The writer finished; the answer never arrived.
                raise RemoteError("transport_error", "writer service is unavailable")
            return result

        return call, calls

    def test_apply_publishes_the_cascade_and_retries_an_unavailable_writer(self):
        call, calls = self._writer(fail_before={"create_policy"},
                                   fail_after={"publish_research_constitution"})
        with contextlib.redirect_stderr(io.StringIO()):
            result = restore.live(self.state_dir, actor=OWNER, call=call, retry_delay=0)
        self.assertTrue(result["matches_fresh_core_default"])
        self.assertFalse(result["resumed"])
        # Retried once when nothing landed; not re-published when it had.
        self.assertEqual(calls, ["create_policy", "create_policy",
                                 "publish_research_constitution", "create_coverage_mission"])
        after = self._current()
        self.assertEqual(after["policy"][FIELD], FRESH_CORE_PREDICATE)
        self.assertEqual(after["constitution"]["bindings"]["governance_policy_version"]["ref"],
                         after["policy_id"])
        self.assertEqual(after["mission"]["bindings"]["constitution_version"]["ref"],
                         after["constitution"]["id"])
        self.assertEqual(plan(self.state_dir)["status"], "already-restored")

    def test_an_interrupted_cascade_is_resumed_without_a_second_policy(self):
        call, _ = self._writer()
        store = DaltonStore(str(self.state_dir / "core.sqlite"))
        try:
            current = read_current(store.connection)
            chain = build_chain(current, now="2026-09-28T00:00:00+00:00",
                                policy_hash=_policy_hash(store, current["policy_id"]))
            store.create_policy(chain["policy"]["policy"], actor_ref=OWNER,
                                **{k: v for k, v in chain["policy"].items() if k != "policy"})
        finally:
            store.close()
        dry = plan(self.state_dir)
        self.assertEqual(dry["status"], "would-resume-cascade")
        self.assertEqual(len(dry["publishes"]), 2)
        result = restore.live(self.state_dir, actor=OWNER, call=call, retry_delay=0)
        self.assertTrue(result["resumed"])
        self.assertEqual(result["chain"]["policy"]["status"], "already-published")
        self.assertEqual(plan(self.state_dir)["status"], "already-restored")

    def test_a_non_retryable_writer_error_is_raised(self):
        def call(operation, params):
            raise RemoteError("forbidden", "principal may not create_policy")

        with self.assertRaisesRegex(RemoteError, "forbidden|may not"):
            restore.live(self.state_dir, actor=OWNER, call=call, retry_delay=0)
        self.assertEqual(self._current()["policy"][FIELD], [])

    # -- refusals and CLI --------------------------------------------------------

    def test_a_different_gate_somebody_chose_is_not_replaced(self):
        current = self._current()
        chosen = dict(current, policy={**current["policy"], FIELD: [
            {"left_path": "producer.model_family", "operator": "eq",
             "right_path": "verifier.model_family"}]})
        with self.assertRaisesRegex(PlanError, "not the fresh-Core default"):
            build_chain(chosen, now="2026-09-28T00:00:00+00:00", policy_hash="x")

    def test_cli_dry_run_writes_nothing_and_apply_needs_a_human(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(main(["--state-dir", str(self.state_dir)]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["status"], "would-publish")
        self.assertEqual(self._current()["policy"][FIELD], [])
        with self.assertRaises(PlanError):
            main(["--state-dir", str(self.state_dir), "--apply"])
        with self.assertRaises(PlanError):
            main(["--state-dir", str(self.state_dir), "--apply", "--actor", "automation:x"])
        with self.assertRaises(PlanError):
            main(["--state-dir", str(self.state_dir), "--apply", "--rehearse", "/tmp/x",
                  "--actor", OWNER])


if __name__ == "__main__":
    unittest.main()

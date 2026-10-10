"""2026-10-09 (UX review C23/C24): the scenario probability group in one write."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any

from dalton_core.company_scenario import (
    CompanyScenarioConflict, CompanyScenarioValidationError, in_force,
)
from dalton_core.cockpit_plane import CockpitError
from dalton_core.cockpit_v2 import scenario_api
from dalton_core.cockpit_v2.routes import V2Router, register_v2_routes
from dalton_core.writer_server import (
    HUMAN_GOVERNANCE_OPERATIONS, OPERATION_ACTOR_FIELDS, OPERATION_FIELDS, ValidationError,
    WriterServer,
)
from tests.p14a_fixtures import ACN, OWNER
from tests.test_company_scenario import ScenarioCase, scenario


def count_versions(store) -> int:
    return store.connection.execute("SELECT COUNT(*) FROM company_scenario_versions").fetchone()[0]


class GroupCase(ScenarioCase):
    def setUp(self) -> None:
        super().setUp()
        self.accept(self.propose([scenario("bull", "0.2"), scenario("base", "0.5"),
                                  scenario("bear", "0.3")]))

    def live(self) -> dict[str, dict]:
        return {item["key"]: item for item in in_force(self.store.connection, ACN)}

    def changes(self, **values: Any) -> list[dict]:
        out = []
        for key, item in self.live().items():
            value = values.get(key, item["probability"])
            change = {"scenario_ref": item["scenario_ref"], "version_ref": item["id"],
                      "version_hash": item["content_hash"],
                      "retire": value == "retire",
                      "probability": None if value == "retire" else value}
            out.append(change)
        return out

    def group(self, changes: list[dict], reason: str = "订单弱", key: str | None = None) -> dict:
        return self.authority.revise_group(company_ref=ACN, changes=changes, reason=reason,
                                           actor_ref=OWNER, mission=self.mission,
                                           idempotency_key=key)


class AuthorityGroupTests(GroupCase):
    def test_the_group_is_written_together(self) -> None:
        before = count_versions(self.store)
        result = self.group(self.changes(base="0.45", bull="0.25"))
        self.assertEqual(result["status"], "published")
        self.assertEqual(count_versions(self.store), before + 2)  # bear unchanged: no version
        self.assertEqual({v["revision_group_ref"] for v in result["versions"]}, {result["group_ref"]})
        self.assertEqual({v["revision_group_size"] for v in result["versions"]}, {2})
        live = self.live()
        self.assertEqual((live["bull"]["probability"], live["base"]["probability"],
                          live["bear"]["probability"]), ("0.25", "0.45", "0.30"))
        self.assertFalse(result["reason_auto"])

    def test_a_stale_row_refuses_the_whole_group(self) -> None:
        changes = self.changes(base="0.45", bull="0.25")
        changes[0]["version_hash"] = "0" * 64
        before = count_versions(self.store)
        with self.assertRaises(CompanyScenarioConflict):
            self.group(changes)
        self.assertEqual(count_versions(self.store), before)

    def test_a_missing_scenario_refuses_the_group(self) -> None:
        with self.assertRaises(CompanyScenarioConflict):
            bear = self.live()["bear"]["scenario_ref"]
            self.group([c for c in self.changes(base="0.8") if c["scenario_ref"] != bear])

    def test_the_sum_must_be_one_hundred(self) -> None:
        before = count_versions(self.store)
        with self.assertRaises(CompanyScenarioValidationError):
            self.group(self.changes(base="0.45"))
        self.assertEqual(count_versions(self.store), before)

    def test_at_least_two_scenarios_are_kept(self) -> None:
        with self.assertRaises(CompanyScenarioValidationError):
            self.group(self.changes(bull="retire", bear="retire", base="0.95"))

    def test_retire_and_reprobability_in_one_group_with_an_auto_reason(self) -> None:
        result = self.group(self.changes(bull="retire", base="0.6", bear="0.4"), reason="")
        self.assertEqual(result["status"], "published")
        self.assertTrue(result["reason_auto"])
        self.assertEqual(result["reason"], "调整情景概率：基准 50%→60%；悲观 30%→40%；停用：乐观")
        self.assertEqual(sorted(self.live()), ["base", "bear"])
        retired = next(v for v in result["versions"] if v["key"] == "bull")
        self.assertEqual((retired["state"], retired["change_reason"]), ("retired", "owner_retired"))

    def test_replay_with_the_same_key_writes_nothing_more(self) -> None:
        changes = self.changes(base="0.45", bull="0.25")
        first = self.group(changes, key="k1")
        before = count_versions(self.store)
        again = self.group(changes, key="k1")
        self.assertEqual(again["status"], "replayed")
        self.assertEqual(again["group_ref"], first["group_ref"])
        self.assertEqual(count_versions(self.store), before)

    def test_a_reused_key_with_another_group_conflicts(self) -> None:
        self.group(self.changes(base="0.45", bull="0.25"), key="k2")
        with self.assertRaises(CompanyScenarioConflict):
            self.group(self.changes(base="0.40", bull="0.30"), key="k2")

    def test_no_change_is_a_duplicate(self) -> None:
        before = count_versions(self.store)
        result = self.group(self.changes())
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(count_versions(self.store), before)

    def test_only_a_person(self) -> None:
        with self.assertRaises(CompanyScenarioConflict):
            self.authority.revise_group(company_ref=ACN, changes=self.changes(base="0.45", bull="0.25"),
                                        reason="", actor_ref="automation:x", mission=self.mission)


class WriterOpTests(GroupCase):
    def test_the_op_is_a_person_door_with_its_fields(self) -> None:
        self.assertIn("revise_company_scenario_group", HUMAN_GOVERNANCE_OPERATIONS)
        self.assertIn("revise_company_scenario", HUMAN_GOVERNANCE_OPERATIONS)
        self.assertEqual(OPERATION_FIELDS["revise_company_scenario_group"],
                         frozenset({"company_ref", "changes", "reason", "idempotency_key", "actor_ref"}))
        self.assertEqual(OPERATION_ACTOR_FIELDS["revise_company_scenario_group"], "actor_ref")

    def test_the_op_writes_through_the_authority_and_retimes(self) -> None:
        calls = []
        fake = SimpleNamespace(
            store=self.store, _active_mission_version=lambda: self.mission,
            _scenario_probability_cadence=lambda ref, changes, **kw: calls.append((ref, changes)) or {"published": []})
        result = WriterServer._op_revise_company_scenario_group(fake, {
            "company_ref": ACN, "changes": self.changes(base="0.3", bull="0.4"),
            "reason": "", "idempotency_key": "w1", "actor_ref": OWNER})
        self.assertEqual(result["status"], "published")
        self.assertEqual(calls[0][0], ACN)
        self.assertEqual({c["to"] for c in calls[0][1]}, {"0.30", "0.40"})  # both moved >= 10pp
        with self.assertRaises(ValidationError):
            WriterServer._op_revise_company_scenario_group(fake, {
                "company_ref": ACN, "changes": self.changes(base="0.9"),
                "reason": "", "actor_ref": OWNER})
        fake._active_mission_version = lambda: None
        with self.assertRaises(ValidationError):
            WriterServer._op_revise_company_scenario_group(fake, {
                "company_ref": ACN, "changes": [], "actor_ref": OWNER})


H = "a" * 64


class CockpitRouteTests(unittest.TestCase):
    def body(self, **extra: Any) -> bytes:
        value = {"company_ref": ACN, "request_id": "r1", "changes": [
            {"scenario_ref": "s:base", "version_ref": "v:base", "version_hash": H, "probability": "45"},
            {"scenario_ref": "s:bear", "version_ref": "v:bear", "version_hash": H, "probability": "0.55"},
            {"scenario_ref": "s:bull", "version_ref": "v:bull", "version_hash": H, "retire": True}]}
        value.update(extra)
        return json.dumps(value).encode()

    def plane(self, result: dict) -> Any:
        calls = {"governance": [], "journal": []}

        def governance(login, operation, params, *, failure):
            calls["governance"].append((login, operation, params, failure))
            return result

        journal = SimpleNamespace(record_event=lambda **kw: calls["journal"].append(kw))
        return SimpleNamespace(_governance=governance, journal=journal), calls

    def test_registered_on_the_v2_router(self) -> None:
        router = register_v2_routes(SimpleNamespace())
        self.assertIn(scenario_api.REVISE_GROUP_PATH, router.posts)
        own = V2Router()
        scenario_api.register(own)
        self.assertIs(own.posts[scenario_api.REVISE_GROUP_PATH], scenario_api.handle_revise_group)

    def test_a_body_becomes_one_governance_call(self) -> None:
        plane, calls = self.plane({"status": "published", "group_ref": "g1", "reason": "调整情景概率：…",
                                   "reason_auto": True, "versions": [
                                       {"id": "v2", "scenario_ref": "s:base", "key": "base",
                                        "probability": "0.45", "state": "live"},
                                       {"id": "v3", "scenario_ref": "s:bull", "key": "bull",
                                        "probability": "0.20", "state": "retired"}]})
        app = SimpleNamespace(cockpit_plane=plane)
        out = scenario_api.handle_revise_group(app, "owner@example.com", None, None, self.body())
        login, operation, params, failure = calls["governance"][0]
        self.assertEqual(operation, "revise_company_scenario_group")
        self.assertEqual(params["idempotency_key"], "cockpit-scenario-group:r1")
        self.assertEqual(params["reason"], "")
        self.assertEqual([c["probability"] for c in params["changes"]], ["0.45", "0.55", None])
        self.assertTrue(params["changes"][2]["retire"])
        self.assertEqual(failure, "情景概率没有保存")
        self.assertEqual(calls["journal"][0]["refs"]["group_ref"], "g1")
        self.assertEqual(out["status"], "published")
        self.assertTrue(out["reason_auto"])
        self.assertEqual(out["message"], "已整组保存（2 项修改）：基准 45%；乐观已停用。")
        self.assertEqual(out["versions"][0]["version_ref"], "v2")

    def test_refusals_are_chinese(self) -> None:
        plane, calls = self.plane({})
        app = SimpleNamespace(cockpit_plane=plane)
        for body in (b"{", self.body(request_id=""), self.body(changes=[]),
                     self.body(reason="x" * 601),
                     self.body(changes=[{"scenario_ref": "s", "version_ref": "v",
                                         "version_hash": "bad", "probability": "0.5"}])):
            with self.subTest(body=body[:40]):
                with self.assertRaises(CockpitError) as caught:
                    scenario_api.handle_revise_group(app, "o", None, None, body)
                self.assertRegex(str(caught.exception), r"[一-鿿]")
        self.assertEqual(calls["governance"], [])


if __name__ == "__main__":
    unittest.main()

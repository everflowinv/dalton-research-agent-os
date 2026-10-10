"""ADR-0012: structured scenarios -- authority, evaluation, hand-off, views."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dalton_core.company_scenario import (
    CompanyScenarioAuthority,
    CompanyScenarioConflict,
    CompanyScenarioValidationError,
    Observer,
    brief_entries,
    brief_lines,
    company_view,
    evaluate_condition,
    in_force,
    replay_brief_entries,
)
from tests.p14a_fixtures import ACN, AUTOMATION, OWNER, P14aHarness

TODAY = datetime.now(timezone.utc).date()
HORIZON = (TODAY + timedelta(days=180)).isoformat()
REVENUES = "concept:us-gaap:Revenues"


def legacy_scenario_fixture(case):
    """Build historical scenario fixtures through the pre-contract writer path.

    Production uses the strict new boundary. These tests retain the old records
    for read/decision/observation compatibility; test_scenario_rules covers new
    creation and refusal contracts without this fixture override.
    """
    from unittest.mock import patch
    gate = patch("dalton_core.scenario_rules.validate_new_scenario_rules", return_value="legacy-scenario-fixture")
    gate.start(); case.addCleanup(gate.stop)
    readiness = patch("dalton_core.scenario_proposal.proposal_input_gate", return_value=True)
    readiness.start(); case.addCleanup(readiness.stop)


def condition(cid: str, *, observed_by: str = "market_price", metric_ref: str | None = "price:close",
              comparator: str | None = "gte", threshold: str | None = "120",
              transform: str = "level", refs: list[str] | None = None, label: str = "股价站上 120") -> dict:
    return {"condition_id": cid, "label": label, "observed_by": observed_by,
            "metric_ref": metric_ref, "transform": transform, "comparator": comparator,
            "threshold": threshold, "horizon": HORIZON,
            "refs": ["ref:basis"] if refs is None else refs}


def scenario(key: str, probability: str, *, triggers: list[dict] | None = None,
             invalidators: list[dict] | None = None) -> dict:
    return {"key": key, "name": f"{key} 情景", "narrative": f"{key} 的叙述",
            "probability": probability, "linked_thesis_refs": [], "linked_driver_refs": [],
            "evidence_refs": ["ref:basis"],
            "triggers": triggers or [condition("t1")], "invalidators": invalidators or []}


def make_tables(connection: sqlite3.Connection) -> None:
    """Minimal stand-ins for the dossier and driver-model ledgers (read-only here)."""

    connection.executescript(
        "CREATE TABLE IF NOT EXISTS company_dossier_versions(version_id TEXT PRIMARY KEY, "
        "company_ref TEXT, version_number INTEGER, record_json TEXT);"
        "CREATE TABLE IF NOT EXISTS forecast_model_versions(version_id TEXT PRIMARY KEY, "
        "company_ref TEXT, version_number INTEGER, record_json TEXT);")
    connection.commit()


def put_model(connection: sqlite3.Connection, company_ref: str, values: list[tuple[str, str]]) -> str:
    record = {"id": "forecast-model-version:test:1", "content_hash": "f" * 64, "drivers": [{
        "ref": REVENUES, "label": "Revenues", "unit": "usd", "role": "revenue",
        "history": [{"period_end": end, "value": value, "accessions": [f"0000-{end}"]}
                    for end, value in values]}]}
    connection.execute("INSERT INTO forecast_model_versions VALUES(?,?,?,?)",
                       (record["id"], company_ref, 1, json.dumps(record)))
    connection.commit()
    return record["id"]


def put_dossier(connection: sqlite3.Connection, company_ref: str, refs: list[str]) -> dict:
    record = {"id": "company-dossier-version:test:1", "content_hash": "d" * 64,
              "body_hash": "b" * 64, "variant_view": {"slots": [
                  {"slot_id": "convergence_pathway", "sentences": [
                      {"text": "新签订单转为上升、指引下限上调，市场才会改变看法。", "refs": refs}]},
                  {"slot_id": "observable_signals", "sentences": [
                      {"text": "若营收同比继续下滑，说明判断错了。", "refs": refs}]}],
                  "gaps": ["缺新签订单数字"]},
              "sections": []}
    connection.execute("INSERT INTO company_dossier_versions VALUES(?,?,?,?)",
                       (record["id"], company_ref, 1, json.dumps(record)))
    connection.commit()
    return record


class ScenarioCase(P14aHarness):
    grants = ("market_event", "observation", "stage_record", "deliverable", "dossier")

    def setUp(self) -> None:
        super().setUp()
        legacy_scenario_fixture(self)
        self.authority = CompanyScenarioAuthority(self.store)
        make_tables(self.store.connection)

    def propose(self, scenarios: list[dict] | None = None, actor: str = AUTOMATION) -> dict:
        return self.authority.propose_set(
            company_ref=ACN, scenarios=scenarios or [scenario("base", "0.6"), scenario("bear", "0.4")],
            anchors={"dossier_version_ref": "company-dossier-version:test:1"},
            proposer={"kind": "model", "purpose": "scenario_proposal"},
            mission=self.mission, actor_ref=actor)

    def accept(self, proposal: dict) -> dict:
        return self.authority.decide_set(set_ref=proposal["id"], set_hash=proposal["content_hash"],
                                         decision="accept", reason="ok", actor_ref=OWNER)


class AuthorityTests(ScenarioCase):
    def test_a_proposal_is_not_in_force_until_the_owner_accepts_it(self) -> None:
        proposal = self.propose()
        self.assertEqual(proposal["state"], "pending")
        self.assertEqual(len(proposal["versions"]), 2)
        self.assertEqual(in_force(self.store.connection, ACN), [])
        self.accept(proposal)
        live = in_force(self.store.connection, ACN)
        self.assertEqual([item["key"] for item in live], ["base", "bear"])
        self.assertEqual(live[0]["probability"], "0.60")

    def test_one_undecided_proposal_per_company(self) -> None:
        self.propose()
        self.assertEqual(self.propose()["status"], "pending_exists")

    def test_only_a_person_decides_and_only_on_the_bytes_read(self) -> None:
        proposal = self.propose()
        with self.assertRaises(CompanyScenarioConflict):
            self.authority.decide_set(set_ref=proposal["id"], set_hash=proposal["content_hash"],
                                      decision="accept", reason="x", actor_ref=AUTOMATION)
        with self.assertRaises(CompanyScenarioConflict):
            self.authority.decide_set(set_ref=proposal["id"], set_hash="0" * 64,
                                      decision="accept", reason="x", actor_ref=OWNER)
        self.authority.decide_set(set_ref=proposal["id"], set_hash=proposal["content_hash"],
                                  decision="reject", reason="no", actor_ref=OWNER)
        with self.assertRaises(CompanyScenarioConflict):
            self.accept(proposal)
        self.assertEqual(in_force(self.store.connection, ACN), [])

    def test_versions_are_immutable(self) -> None:
        self.accept(self.propose())
        for sql in ("UPDATE company_scenario_versions SET actor_ref='x'",
                    "DELETE FROM company_scenario_sets",
                    "UPDATE company_scenario_decisions SET decision='reject'"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.store.connection.execute(sql)

    def test_the_contract_refuses_what_cannot_be_watched_or_referred_to(self) -> None:
        with self.assertRaises(CompanyScenarioValidationError):
            self.propose([scenario("base", "0.7"), scenario("bear", "0.4")])  # sums to 1.1
        with self.assertRaises(CompanyScenarioValidationError):
            self.propose([scenario("base", "0.5", triggers=[condition("t1", observed_by="tea_leaves")]),
                          scenario("bear", "0.5")])
        with self.assertRaises(CompanyScenarioValidationError):
            self.propose([scenario("base", "0.5", triggers=[condition("t1", refs=[])]),
                          scenario("bear", "0.5")])
        with self.assertRaises(CompanyScenarioValidationError):
            self.authority.propose_set(
                company_ref=ACN, scenarios=[scenario("base", "0.5"), scenario("bear", "0.5")],
                anchors={}, proposer=None, mission=self.mission, actor_ref=AUTOMATION,
                allowed_refs={"something-else"})

    def test_an_owner_revision_supersedes_and_a_material_move_is_reported(self) -> None:
        self.accept(self.propose())
        base = in_force(self.store.connection, ACN)[0]
        revised = self.authority.revise(
            scenario_ref=base["scenario_ref"], version_ref=base["id"],
            version_hash=base["content_hash"], probability="0.45", reason="订单弱",
            actor_ref=OWNER, mission=self.mission)
        self.assertEqual(revised["version"], base["version"] + 1)
        self.assertEqual(revised["prior_version_ref"], base["id"])
        self.assertEqual(revised["probability_changes"][0]["to"], "0.45")
        live = {item["key"]: item for item in in_force(self.store.connection, ACN)}
        self.assertEqual(live["base"]["probability"], "0.45")
        with self.assertRaises(CompanyScenarioConflict):  # the old version is no longer in force
            self.authority.revise(scenario_ref=base["scenario_ref"], version_ref=base["id"],
                                  version_hash=base["content_hash"], probability="0.3",
                                  reason="x", actor_ref=OWNER, mission=self.mission)
        retired = self.authority.revise(
            scenario_ref=base["scenario_ref"], version_ref=revised["id"],
            version_hash=revised["content_hash"], retire=True, reason="不再相关",
            actor_ref=OWNER, mission=self.mission)
        self.assertEqual(retired["state"], "retired")
        self.assertEqual([item["key"] for item in in_force(self.store.connection, ACN)], ["bear"])

    def test_an_owner_set_is_in_force_at_once_and_a_new_accepted_set_supersedes(self) -> None:
        self.propose(actor=OWNER)
        self.assertEqual(len(in_force(self.store.connection, ACN)), 2)
        later = self.propose([scenario("base", "0.3"), scenario("bear", "0.7")])
        result = self.accept(later)
        self.assertEqual({c["name"] for c in result["probability_changes"]}, {"base 情景", "bear 情景"})
        self.assertEqual({item["set_ref"] for item in in_force(self.store.connection, ACN)},
                         {later["id"]})


class EvaluationTests(unittest.TestCase):
    base = {"observed_by": "market_price", "comparator": "gte", "threshold": "120",
            "horizon": HORIZON}

    def test_the_three_statuses(self) -> None:
        hit = evaluate_condition(self.base, {"value": "121", "as_of": TODAY.isoformat(),
                                             "source_ref": "p"}, today=TODAY,
                                 base_date=TODAY.isoformat())
        self.assertEqual((hit["status"], hit["value"], hit["source_ref"]), ("triggered", "121", "p"))
        miss = evaluate_condition(self.base, {"value": "119", "as_of": TODAY.isoformat()},
                                  today=TODAY, base_date=TODAY.isoformat())
        self.assertEqual(miss["status"], "not_triggered")
        self.assertEqual(evaluate_condition(self.base, None, today=TODAY,
                                            base_date=TODAY.isoformat())["status"], "unobservable")
        owner = evaluate_condition({**self.base, "observed_by": "owner"}, None, today=TODAY,
                                   base_date=TODAY.isoformat())
        self.assertEqual((owner["status"], owner["reason"]), ("unobservable", "owner_observed"))

    def test_old_data_and_a_passed_horizon_never_trigger(self) -> None:
        old = evaluate_condition(self.base, {"value": "130", "as_of": "2020-01-01"},
                                 today=TODAY, base_date=TODAY.isoformat())
        self.assertEqual((old["status"], old["reason"]), ("not_triggered", "predates_scenario"))
        late = evaluate_condition({**self.base, "horizon": "2020-01-01"},
                                  {"value": "130", "as_of": TODAY.isoformat()},
                                  today=TODAY, base_date="2019-01-01")
        self.assertEqual((late["status"], late["reason"]), ("not_triggered", "horizon_passed"))

    def test_growth_is_computed_from_the_filed_history(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        make_tables(connection)
        put_model(connection, ACN, [("2025-05-31", "100"), ("2025-08-31", "104"),
                                    ("2025-11-30", "106"), ("2026-02-28", "108"),
                                    ("2026-05-31", "110")])
        observer = Observer(connection)
        self.assertEqual(observer.read(ACN, "sec_actual", REVENUES, "yoy_pct")["value"], "10.00")
        self.assertEqual(observer.read(ACN, "sec_actual", REVENUES, "qoq_pct")["value"], "1.85")
        level = observer.read(ACN, "sec_actual", REVENUES)
        self.assertEqual((level["value"], level["as_of"]), ("110", "2026-05-31"))
        self.assertIn("0000-2026-05-31", level["source_ref"])
        self.assertIsNone(observer.read("company:none", "sec_actual", REVENUES))


class ObservationAndHandOffTests(ScenarioCase):
    def setUp(self) -> None:
        super().setUp()
        self.pass_screen(ACN)
        yesterday = (TODAY - timedelta(days=1)).isoformat()
        self.prices(ACN, [(yesterday, "100")])

    def tick(self) -> dict:
        from dalton_core.mission_scenario_lane import observe_tick
        from dalton_core.tracking_cadence import load_policy

        policy = load_policy(Path(__file__).resolve().parents[1]
                             / "deploy/phase9/p14a-tracking-policy-v2.json")
        return observe_tick(self.store, self.mission, now=datetime.now(timezone.utc),
                            policy_loader=lambda: policy)

    def events(self) -> list[sqlite3.Row]:
        if not self.store.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='research_events'").fetchone():
            return []
        return self.store.connection.execute(
            "SELECT * FROM research_events WHERE kind='scenario_trigger'").fetchall()

    def test_a_pending_proposal_is_observed_but_nothing_fires(self) -> None:
        self.propose()
        self.prices(ACN, [((TODAY - timedelta(days=1)).isoformat(), "100"),
                          (TODAY.isoformat(), "125")],
                    captured_at=datetime.now(timezone.utc).isoformat())
        result = self.tick()
        self.assertEqual(result["appended"], 2)
        self.assertEqual(result["fired"], 0)
        self.assertEqual(self.events(), [])
        rows = self.authority.observations(ACN)
        self.assertEqual({row["status"] for row in rows}, {"triggered"})
        self.assertFalse(any(row["in_force"] for row in rows))
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM tracking_cadence_versions").fetchone()[0]
            if self.store.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name='tracking_cadence_versions'").fetchone()
            else 0, 0)
        self.assertEqual(self.tick()["appended"], 0)  # unchanged facts append nothing

    def test_an_adopted_trigger_becomes_an_event_for_the_judge_and_re_times_tracking(self) -> None:
        from dalton_core.event_triage import BYPASS_KINDS
        from dalton_core.mission_event_judgement_lane import pending_event_groups
        from dalton_core.tracking_cadence import TrackingCadenceAuthority

        self.accept(self.propose())
        first = self.tick()
        self.assertEqual((first["appended"], first["fired"]), (2, 0))  # 100 < 120
        self.prices(ACN, [((TODAY - timedelta(days=1)).isoformat(), "100"),
                          (TODAY.isoformat(), "125")],
                    captured_at=datetime.now(timezone.utc).isoformat())
        fired = self.tick()
        self.assertEqual((fired["fired"], fired["events"]), (2, 2))
        events = self.events()
        self.assertEqual(len(events), 2)
        payload = json.loads(events[0]["record_json"])["payload"]
        self.assertEqual(payload["observed_value"], "125")
        self.assertTrue(payload["observation_ref"].startswith("company-scenario-observation:"))
        self.assertIn("scenario_trigger", BYPASS_KINDS)
        groups = pending_event_groups(self.store, self.missions, self.mission)
        self.assertEqual({group["event_ref"] for group in groups},
                         {row["event_id"] for row in events})
        cadence = TrackingCadenceAuthority(self.store).latest(ACN, "alphaengine")
        self.assertEqual((cadence["decision"], cadence["change_reason"], cadence["actor_ref"]),
                         ("scenario_trigger", "driver_event", AUTOMATION))
        self.assertEqual(cadence["interval_seconds"], 43200 // 4)
        again = self.tick()
        self.assertEqual((again["fired"], again["events"], again["cadence_versions"]), (0, 0, 0))
        view = company_view(self.store.connection, ACN)
        self.assertIn("2 个触发条件已触发", view["headline"])
        self.assertTrue(view["in_force"][0]["triggers"][0]["handed_off_event_ref"])

    def test_without_market_event_the_trigger_is_only_observed(self) -> None:
        mission = dict(self.mission)
        mission["autonomy"] = {**mission["autonomy"], "may_write": [
            scope for scope in mission["autonomy"]["may_write"] if scope != "market_event"]}
        self.mission = mission
        self.accept(self.propose())
        self.prices(ACN, [((TODAY - timedelta(days=1)).isoformat(), "100"),
                          (TODAY.isoformat(), "125")],
                    captured_at=datetime.now(timezone.utc).isoformat())
        result = self.tick()
        self.assertEqual((result["fired"], result["events"]), (2, 0))
        self.assertTrue(result["notes"])


class FakeModel:
    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def call(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        text = self.reply(kwargs["prompt"]) if callable(self.reply) else self.reply
        return {"text": text, "cost_micros": 250_000, "work_order_ref": "work:test",
                "invocation_ref": "invocation:test", "route_decision_ref": "route:test"}


class ProposalTests(ScenarioCase):
    def setUp(self) -> None:
        super().setUp()
        self.claim_ref = self.claim(subject=ACN, statement="新签订单低于预期。")
        put_dossier(self.store.connection, ACN, [self.claim_ref])
        put_model(self.store.connection, ACN, [("2025-05-31", "100"), ("2025-08-31", "104"),
                                               ("2025-11-30", "106"), ("2026-02-28", "108"),
                                               ("2026-05-31", "110")])
        self.prices(ACN, [((TODAY - timedelta(days=1)).isoformat(), "100")])
        self.company = next(m for m in self.mission["universe"] if m["company_ref"] == ACN)

    def material(self) -> dict:
        from dalton_core.scenario_proposal import build_material

        return build_material(self.store.connection, company=self.company, today=TODAY)

    def reply(self, material: dict, *, bear_metric: str = REVENUES) -> str:
        price = next(row for row in material["observables"] if row["observed_by"] == "market_price")
        price_ref = price["transforms"]["level"]["source_ref"]
        base = scenario("base", "0.6", triggers=[condition("t1", refs=[price_ref])],
                        invalidators=[condition("i1", comparator="lte", threshold="150",
                                                refs=[price_ref], label="股价不高于 150")])
        bear = scenario("bear", "0.4", triggers=[condition(
            "t1", observed_by="sec_actual", metric_ref=bear_metric, transform="yoy_pct",
            comparator="lte", threshold="-5", refs=[self.claim_ref], label="营收同比跌 5%")])
        for item in (base, bear):
            item["evidence_refs"] = [self.claim_ref]
            item["linked_driver_refs"] = [REVENUES]
        return "```json\n" + json.dumps({"scenarios": [base, bear]}, ensure_ascii=False) + "\n```"

    def test_the_material_says_what_can_be_observed_with_values_and_refs(self) -> None:
        from dalton_core.scenario_proposal import build_prompt

        material = self.material()
        kinds = {(row["observed_by"], row["metric_ref"]) for row in material["observables"]}
        self.assertIn(("sec_actual", REVENUES), kinds)
        self.assertIn(("market_price", "price:close"), kinds)
        self.assertNotIn(("event_judgement", "judgement:THESIS_WEAKENED"), kinds)
        revenue = next(row for row in material["observables"] if row["metric_ref"] == REVENUES)
        self.assertEqual(revenue["transforms"]["yoy_pct"]["value"], "10.00")
        self.assertIn("可观察指标表", build_prompt(material))

    def test_one_call_publishes_a_pending_set_and_drops_what_already_happened(self) -> None:
        from dalton_core.scenario_proposal import propose_for_company

        model = FakeModel(self.reply(self.material()))
        result = propose_for_company(self.store, mission=self.mission, company=self.company,
                                     model=model, today=TODAY)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0]["purpose"], "scenario_proposal")
        self.assertEqual((result["status"], result["scenarios"], result["dropped_conditions"]),
                         ("published", 2, 1))
        proposal = self.authority.get_set(result["set_ref"])
        self.assertEqual(proposal["state"], "pending")
        self.assertEqual(proposal["proposer"]["cost_micros"], 250_000)
        self.assertEqual(proposal["dropped_conditions"][0]["reason"], "already_met")
        bear = next(v for v in proposal["versions"] if v["key"] == "bear")
        self.assertEqual(bear["triggers"][0]["baseline"]["value"], "10.00")
        self.assertEqual(bear["triggers"][0]["metric_label"], "Revenues")
        again = propose_for_company(self.store, mission=self.mission, company=self.company,
                                    model=model, today=TODAY)
        self.assertEqual(again["status"], "pending_exists")
        self.assertEqual(len(model.calls), 1)

    def test_a_metric_no_lane_observes_is_refused_after_one_call(self) -> None:
        from dalton_core.scenario_proposal import propose_for_company

        model = FakeModel(self.reply(self.material(), bear_metric="concept:us-gaap:Invented"))
        result = propose_for_company(self.store, mission=self.mission, company=self.company,
                                     model=model, today=TODAY)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(self.authority.sets(ACN), [])

    def test_an_owner_condition_without_horizon_is_set_aside_not_the_whole_set(self) -> None:
        # ws-7d META, 2026-10-07: one 「由你判断」 condition with no horizon
        # refused the whole paid proposal ("scenario base: horizon must be an ISO date").
        from dalton_core.cockpit_scenarios import approval_items
        from dalton_core.scenario_proposal import build_prompt, propose_for_company

        material = self.material()
        value = json.loads(self.reply(material).split("```json\n", 1)[1].rsplit("\n```", 1)[0])
        value["scenarios"][0]["triggers"].append({
            "condition_id": "t2", "label": "管理层在财报里维持高位资本开支", "observed_by": "owner",
            "refs": [self.claim_ref]})
        model = FakeModel(json.dumps(value, ensure_ascii=False))
        result = propose_for_company(self.store, mission=self.mission, company=self.company,
                                     model=model, today=TODAY)
        self.assertEqual(result["status"], "published", result)
        proposal = self.authority.get_set(result["set_ref"])
        reasons = {row["reason"] for row in proposal["dropped_conditions"]}
        self.assertIn("owner_without_horizon", reasons)
        self.assertIn("包括 observed_by=owner", build_prompt(material))
        card = approval_items(self.store.connection, label=lambda ref: ref)[0]
        self.assertTrue(any("没有写观察期限" in line for line in card["details"]["已剔除的条件"]))

    def test_a_machine_condition_without_horizon_still_refuses(self) -> None:
        from dalton_core.scenario_proposal import propose_for_company

        value = json.loads(self.reply(self.material()).split("```json\n", 1)[1].rsplit("\n```", 1)[0])
        value["scenarios"][0]["triggers"][0]["horizon"] = None
        result = propose_for_company(self.store, mission=self.mission, company=self.company,
                                     model=FakeModel(json.dumps(value)), today=TODAY)
        self.assertEqual(result["status"], "refused")
        self.assertIn("horizon", result["reason"])

    def test_a_refusal_under_an_older_contract_does_not_hold_the_company(self) -> None:
        from dalton_core.mission_scenario_lane import ScenarioProposalCoordinator
        from dalton_core.scenario_proposal import CONTRACT_VERSION

        now = datetime.now(timezone.utc)
        coordinator = ScenarioProposalCoordinator(server=None, launcher=None, clock=lambda: now)
        target = {"company_ref": ACN, "dossier_body_hash": "b" * 64}
        old = {**target, "started_at": (now - timedelta(hours=2)).isoformat()}
        self.assertFalse(coordinator._recently_tried([old], target))
        self.assertTrue(coordinator._recently_tried([{**old, "contract": CONTRACT_VERSION}], target))

    def test_eligibility_waits_for_the_owner_and_for_a_new_dossier(self) -> None:
        from dalton_core.scenario_proposal import eligible_companies, propose_for_company

        now = datetime.now(timezone.utc)
        self.assertEqual([row["company_ref"] for row in
                          eligible_companies(self.store.connection, self.mission, now=now,
                                             min_repropose_days=7)], [ACN])
        result = propose_for_company(self.store, mission=self.mission, company=self.company,
                                     model=FakeModel(self.reply(self.material())), today=TODAY)
        self.assertEqual(eligible_companies(self.store.connection, self.mission, now=now,
                                            min_repropose_days=7), [])
        proposal = self.authority.get_set(result["set_ref"])
        self.accept(proposal)
        # Decided, same dossier body: nothing owed.
        self.assertEqual(eligible_companies(self.store.connection, self.mission,
                                            now=now + timedelta(days=30), min_repropose_days=7), [])

    def test_ungranted_mission_spends_nothing(self) -> None:
        from dalton_core.scenario_proposal import propose_for_company

        mission = dict(self.mission)
        mission["autonomy"] = {**mission["autonomy"], "may_write": ["observation"]}
        model = FakeModel("{}")
        result = propose_for_company(self.store, mission=mission, company=self.company,
                                     model=model, today=TODAY)
        self.assertEqual((result["status"], model.calls), ("not_authorized", []))


class BriefSectionTests(ScenarioCase):
    def test_entries_replay_and_render(self) -> None:
        self.accept(self.propose())
        start = datetime.now(timezone.utc) - timedelta(days=1)
        end = datetime.now(timezone.utc) + timedelta(days=1)
        entries = brief_entries(self.store.connection, [ACN], start, end)
        self.assertEqual(len(entries[0]["scenarios"]), 2)
        self.assertEqual({row["kind"] for row in entries[0]["moved"]}, {"decision", "proposal"})
        replay_brief_entries(self.store.connection, json.loads(json.dumps(entries)))
        lines = "\n".join(brief_lines(entries, {ACN: "ACN"}))
        self.assertIn("ACN｜base 情景（60%）", lines)
        self.assertIn("触发信号：", lines)
        self.assertIn("本期你采纳了一组情景提案", lines)
        tampered = json.loads(json.dumps(entries))
        tampered[0]["scenarios"][0]["hash"] = "0" * 64
        with self.assertRaises(CompanyScenarioConflict):
            replay_brief_entries(self.store.connection, tampered)
        with closing(sqlite3.connect(":memory:")) as empty:
            self.assertEqual(brief_entries(empty, [ACN], start, end), [])


class WeeklyBriefRenderTests(unittest.TestCase):
    """The section in a real issue, and its absence on a Core without scenarios."""

    def setUp(self) -> None:
        legacy_scenario_fixture(self)
        from tests.test_weekly_brief import WeeklyBriefAuthorityTests

        case = WeeklyBriefAuthorityTests(methodName="test_render_omits_reconciliation_section_for_pre_p9c_issues")
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.case = case

    def publish(self) -> dict:
        now = datetime.now(timezone.utc)
        params = self.case.issue_params()
        params["period_start"] = (now - timedelta(days=1)).isoformat(timespec="seconds")
        params["period_end"] = (now + timedelta(days=1)).isoformat(timespec="seconds")
        brief_ref = params.pop("brief_ref")
        return self.case.weekly.publish_issue(brief_ref, **params)

    def test_no_scenario_tables_no_section(self) -> None:
        issue = self.publish()
        self.assertNotIn("scenario_tracking", issue)
        self.assertNotIn("情景与触发", self.case.weekly.render_markdown(issue["id"])["body"])

    def test_adopted_scenarios_are_a_replayed_section(self) -> None:
        from tests.test_weekly_brief import ACN_REF

        store = self.case.fixture.store
        mission = {"id": "coverage-mission-version:test:1", "content_hash": "c" * 64,
                   "universe": [{"company_ref": ACN_REF}],
                   "autonomy": {"automation_principal": AUTOMATION, "may_write": ["dossier"]}}
        CompanyScenarioAuthority(store).propose_set(
            company_ref=ACN_REF, scenarios=[scenario("base", "0.6"), scenario("bear", "0.4")],
            anchors={}, proposer=None, mission=mission, actor_ref=OWNER)
        issue = self.publish()
        self.assertEqual(len(issue["scenario_tracking"][0]["scenarios"]), 2)
        self.assertEqual(self.case.weekly.issue(issue["id"])["scenario_tracking"],
                         issue["scenario_tracking"])
        body = self.case.weekly.render_markdown(issue["id"])["body"]
        self.assertIn("## 情景与触发", body)
        self.assertIn("base 情景（60%）", body)
        self.assertLess(body.index("## 情景与触发"), body.index("## 公司与 driver 分化"))


class CockpitTests(unittest.TestCase):
    def setUp(self) -> None:
        legacy_scenario_fixture(self)
        from tests.test_cockpit_plane import CockpitHarness

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.c = CockpitHarness(Path(self.temp.name))
        self.addCleanup(self.c.close)
        self.store = self.c.h.h.core
        mission = self.c.h.missions.active_mission("coverage-mission:us-it-services")
        self.mission = {**mission, "autonomy": {**mission["autonomy"], "may_write": list(
            mission["autonomy"]["may_write"]) + ["dossier"]}}

    def card(self) -> dict:
        return next(c for c in self.c.plane.overview()["companies"] if c["company_ref"] == ACN)

    def test_a_core_without_scenarios_shows_the_card_it_always_did(self) -> None:
        self.assertIsNone(self.card()["scenarios"])
        self.assertFalse([i for i in self.c.plane.approvals()["items"]
                          if i["kind"] == "company_scenario_set"])

    def test_the_proposal_is_an_approval_card_and_the_decision_goes_to_the_writer(self) -> None:
        authority = CompanyScenarioAuthority(self.store)
        proposal = authority.propose_set(
            company_ref=ACN, scenarios=[scenario("base", "0.6"), scenario("bear", "0.4")],
            anchors={}, proposer=None, mission=self.mission,
            actor_ref=self.mission["autonomy"]["automation_principal"])
        self.c.plane.invalidate_overview()
        card = self.card()["scenarios"]
        self.assertEqual(card["headline"], "情景提案等你裁决")
        self.assertEqual(len(card["pending"]["scenarios"]), 2)
        item = next(i for i in self.c.plane.approvals()["items"]
                    if i["kind"] == "company_scenario_set")
        self.assertEqual((item["ref"], item["hash"]), (proposal["id"], proposal["content_hash"]))
        self.assertEqual([a["decision"] for a in item["actions"]], ["accept", "reject", "defer"])
        self.assertIn("base 情景（60%）", item["details"])
        calls = []

        def governance(_config, _socket, *, actor_ref, operation, params):
            calls.append((operation, params))
            return {"status": "decided"}

        self.c.plane.governance_call = governance
        self.c.plane.decide("owner@example.com", {
            "kind": "company_scenario_set", "ref": proposal["id"], "hash": proposal["content_hash"],
            "decision": "accept", "rationale": "", "request_id": "r1"})
        self.assertEqual(calls[0][0], "decide_company_scenario_set")
        self.assertEqual(calls[0][1]["set_hash"], proposal["content_hash"])
        authority.decide_set(set_ref=proposal["id"], set_hash=proposal["content_hash"],
                             decision="accept", reason="ok", actor_ref=OWNER)
        self.c.plane.invalidate_overview()
        card = self.card()["scenarios"]
        self.assertEqual(card["headline"], "2 个情景在跟踪")
        self.assertEqual(card["in_force"][0]["probability_label"], "60%")
        self.assertEqual(card["in_force"][0]["triggers"][0]["status_label"], "还没观察过")
        self.assertIn("股价", card["in_force"][0]["triggers"][0]["observer_label"])

    def test_needs_human_lists_the_undecided_proposal(self) -> None:
        from dalton_core.needs_human import scenario_decisions

        CompanyScenarioAuthority(self.store).propose_set(
            company_ref=ACN, scenarios=[scenario("base", "0.6"), scenario("bear", "0.4")],
            anchors={}, proposer=None, mission=self.mission,
            actor_ref=self.mission["autonomy"]["automation_principal"])
        items = scenario_decisions(self.store.connection)
        self.assertEqual(items[0]["kind"], "scenario_decision")
        self.assertIn("情景假设等你裁决", items[0]["title"])
        with closing(sqlite3.connect(":memory:")) as empty:
            self.assertEqual(scenario_decisions(empty), [])

    def test_the_page_has_the_panel(self) -> None:
        html = (Path(__file__).resolve().parents[1]
                / "src/dalton_core/cockpit_control.html").read_text(encoding="utf-8")
        self.assertIn('["情景与触发",()=>scenariosBlock(c)]', html)
        self.assertIn("/v1/cockpit/scenario_revise", html)


if __name__ == "__main__":
    unittest.main()


class RuleWordingTests(unittest.TestCase):
    """2026-10-07 inspection: 「Revenues同比% < 3」「THESIS_BROKEN」「≥ 1 count」."""

    def rule(self, **condition: Any) -> str:
        from dalton_core.company_scenario import condition_rule

        return condition_rule({"horizon": "2026-12-31", "transform": "level", **condition})

    def test_metrics_transforms_units_and_judgements_are_chinese(self) -> None:
        from dalton_core.company_scenario import condition_rule_technical, condition_value_text

        growth = {"observed_by": "sec_actual", "metric_ref": REVENUES, "metric_label": "Revenues",
                  "transform": "yoy_pct", "unit": "usd", "comparator": "lt", "threshold": "3",
                  "kind": "invalidator"}
        # Batch am (text evaluation 5.3): a sentence from the structured fields.
        self.assertEqual(self.rule(**growth), "预警触发线：营业收入同比增速放缓至 3.0% 以下（观察至 2026-12-31）")
        self.assertIn("同比%", condition_rule_technical({**growth, "horizon": "2026-12-31"}))
        self.assertEqual(self.rule(**{**growth, "transform": "qoq_pct", "metric_ref":
                                      "concept:us-gaap:OperatingIncomeLoss",
                                      "metric_label": "Income from operations"}),
                         "预警触发线：营业利润环比增速放缓至 3.0% 以下（观察至 2026-12-31）")
        cash = {"observed_by": "sec_actual", "metric_label": "Operating cash flow",
                "metric_ref": "concept:us-gaap:NetCashProvidedByUsedInOperatingActivities",
                "unit": "usd", "comparator": "lt", "threshold": "200000000"}
        self.assertEqual(self.rule(**cash), "触发信号：经营活动现金流低于 2 亿美元（观察至 2026-12-31）")
        self.assertEqual(condition_value_text({**cash, "transform": "level"}, "418000000"),
                         "4.18 亿美元")
        judged = {"observed_by": "event_judgement", "metric_ref": "judgement:THESIS_BROKEN",
                  "metric_label": "此后研判为 THESIS_BROKEN 的事件数", "unit": "count",
                  "comparator": "gte", "threshold": "1"}
        self.assertEqual(self.rule(**judged),
                         "触发信号：出现研判为「核心论点被推翻」的事件（观察至 2026-12-31）")
        self.assertEqual(self.rule(observed_by="valuation", metric_ref="valuation:trailing_pe",
                                   metric_label="trailing price / earnings", unit="ratio",
                                   comparator="gt", threshold="18"),
                         "触发信号：滚动市盈率高于 18 倍（观察至 2026-12-31）")
        owner = self.rule(observed_by="owner", label="出现研判为 THESIS_WEAKENED 的事件")
        self.assertEqual(owner, "触发信号：出现研判为「核心论点受损」的事件（需人工核实确认，观察至 2026-12-31）")
        for text in (self.rule(**growth), self.rule(**cash), self.rule(**judged), owner):
            self.assertNotRegex(text, r"[A-Za-z<>≤≥]")

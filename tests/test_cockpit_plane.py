"""P9d-18 / ADR-0006: the owner's cockpit — goal, steer, log, ask, approve."""
from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from datetime import timedelta
from pathlib import Path

from dalton_core.cockpit_model import CockpitModel, CockpitModelError, unwrap_json_object
from dalton_core.cockpit_plane import CockpitConfig, CockpitConflict, CockpitError, CockpitPlane
from dalton_core.cockpit_setup import install as install_cockpit
from dalton_core.document_extraction import HermeticExtractionAdapter, build_prompt
from dalton_core.store import content_hash
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from tests.test_document_extraction import ExtractionHarness, OWNER
from tests.test_transcript_polish_model_worker import policy, profile

BUDGET_POLICY = "thesis-impact-day-budget-policy:test:1"


class _ScriptedAdapter:
    """Replies with a fixed text through the hermetic fixture transport."""

    def __init__(self, text: str, *, created_at: str) -> None:
        self.inner = HermeticExtractionAdapter({"raw": text}, created_at=created_at)
        self.inner.output_text = text
        self.calls = 0

    def execute(self, work, route, profile):
        self.calls += 1
        return self.inner.execute(work, route, profile)


class CockpitHarness:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.h = ExtractionHarness(root)
        self.core_path = root / "core.sqlite"
        with ThesisImpactBudgetStore(root / "budget.sqlite") as budget:
            budget.register_policy(policy_version_id=BUDGET_POLICY, day_cap_micros=5_000_000)
        self.model_config = {
            "routing_policy_ref": policy()["policy_version_ref"], "credential_slot_refs": [profile()["credential_slot_ref"]],
            "model_router_db": str(root / "router.sqlite"), "broker_socket": str(root / "none.sock"),
            "broker_auth_key": str(root / "none.key"), "broker_client_id": "client:dalton-core",
            "expected_agent_id": "chem", "budget_db": str(root / "budget.sqlite"), "budget_policy_ref": BUDGET_POLICY,
        }
        self.model_config_path = root / "model-config.json"
        self.model_config_path.write_text(json.dumps(self.model_config), encoding="utf-8")
        self.heartbeat = root / "run" / "heartbeat.json"
        self.heartbeat.parent.mkdir(exist_ok=True)
        self.heartbeat.write_text(json.dumps({
            "state": "running", "last_tick_at": self.h.h.clock().isoformat(),
            "bounded_planner": {"last_error": None, "last_result": {
                "document_extraction": {"status": "launched", "awaiting": 3, "last": {"drafted": [1, 2]}},
                "mission_source_discovery": {"status": "idle", "web_search": {"discovery": {"status": "idle", "budget": {"cap": 1000, "spent": 7}}}}}},
            "weekly_brief": {"state": "ready", "last_error": None},
        }), encoding="utf-8")
        self.calls: list[tuple[str, dict]] = []
        # P15a: the ask v2 reply shape -- sentences carrying their own refs,
        # typed unknowns, a closed confidence word.
        self.reply = json.dumps({
            # No refs: this harness's Core holds no Claims, so an answer that
            # cited one would be citing something it was never shown -- which
            # is what the verification layer says when it does.
            "sentences": [{"text": "Fixture answer.", "refs": []}],
            "confidence": "medium", "refused": False,
            "refusal_reason": None, "refusal_detail": None,
            "unknowns": [{"what": "nothing on margins", "content_kind": "sell_side_report",
                          "source": "alphaengine"}],
            "market_vs_us": None, "refresh_suggested": None,
        })
        self.adapter = None
        self.config = CockpitConfig.from_mapping({
            "core_db": str(self.core_path), "state_dir": str(root), "heartbeat_path": str(self.heartbeat),
            "scheduler_db": str(root / "cockpit-scheduler.sqlite"), "journal_path": str(root / "cockpit" / "journal.sqlite"),
            "model_config_path": str(self.model_config_path), "mission_ref": "coverage-mission:us-it-services",
        })

        def governance(token_config, socket, *, actor_ref, operation, params):
            self.calls.append((operation, params))
            if operation == "create_coverage_mission":
                values = dict(params)
                ref = values.pop("mission_ref")
                return self.h.missions.create_mission(ref, actor_ref=actor_ref, **values)
            if operation == "decide_thesis_admission":
                return {"decision_id": params["decision_id"], "verdict": params["verdict"]}
            raise AssertionError(operation)

        def model_factory(config):
            def adapter_factory(router):
                self.adapter = _ScriptedAdapter(self.reply, created_at=self.h.h.clock().isoformat())
                return self.adapter
            return CockpitModel(config, scheduler_db=self.config.scheduler_db, adapter_factory=adapter_factory, clock=self.h.h.clock)

        self.plane = CockpitPlane(self.config, writer_socket=root / "w.sock", token_config=root / "t.json",
                                  governance_call=governance, model_factory=model_factory, clock=self.h.h.clock)

    def close(self) -> None:
        self.plane.close()
        self.h.close()

    def wait(self, job_id: str, login: str = "owner@example.com") -> dict:
        for _ in range(200):
            job = self.plane.job(login, job_id)
            if job["status"] != "running":
                return job
            time.sleep(0.02)
        raise AssertionError("job did not finish")


class CockpitPlaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.c = CockpitHarness(Path(self.temp.name)); self.addCleanup(self.c.close)
        self.login = "owner@example.com"

    def test_overview_reads_goal_progress_and_activity_without_machine_refs(self) -> None:
        view = self.c.plane.overview()
        goal = view["goal"]
        self.assertEqual(goal["mission_ref"], "coverage-mission:us-it-services")
        self.assertTrue(goal["title"] and goal["objective"] and goal["research_questions"])
        self.assertEqual([c["ticker"] for c in view["companies"]], [m["ticker"] for m in self.c.h.mission["universe"]])
        # P10a: the sub-task is the Playbook stage, and under it the source
        # base the Playbook's Initial Screen requires, counted per company.
        company = next(c for c in view["companies"] if c["progress"]["found"])
        self.assertEqual(company["stage"], "还没开始")
        self.assertEqual([i["item_ref"] for i in company["checklist"]],
                         ["quarterly_financials", "earnings_calls", "annual_report", "broker_research"])
        self.assertTrue(all(i["status"] in {"complete", "partial", "missing", "not_planned", "source_unavailable"}
                            for i in company["checklist"]))
        self.assertTrue(company["note"])
        from dalton_core.mission_stage import MissionStageDriver
        MissionStageDriver(self.c.h.missions).run_once()
        after = next(c for c in self.c.plane.overview()["companies"] if c["company_ref"] == company["company_ref"])
        self.assertEqual((after["stage"], after["stage_status"], after["stage_ref"]),
                         ("初步筛选", "进行中", "initial_screen"))
        self.assertEqual(view["totals"]["found"], sum(c["progress"]["found"] for c in view["companies"]))
        self.assertEqual(view["activity"]["service_state"], "running")
        lane_keys = [l["key"] for l in view["activity"]["lanes"]]
        self.assertEqual(lane_keys[:4], ["web", "alphaengine", "extraction", "weekly"])
        # INT1: and then one row per registered lane, so a lane that is
        # silent because nobody granted it is visible as that.
        self.assertIn("lane:mission_market_prices", lane_keys)
        self.assertEqual(view["budgets"]["web"], {"cap": 1000, "spent": 7})
        self.assertTrue(view["model_available"]["available"])
        # The acquisition ticket the harness wrote shows up as plain-language activity in the log.
        log = self.c.plane.log()
        kinds = {e["kind"] for e in log["events"]}
        self.assertIn("acquisitions", kinds)
        ticket_event = next(e for e in log["events"] if e["kind"] == "acquisitions")
        self.assertTrue(ticket_event["title"].startswith("获取了研报原文"))
        self.assertNotIn("alphaengine-doc:", ticket_event["title"])
        self.assertEqual(log["cursor"], log["events"][0]["at"])
        self.assertEqual(self.c.plane.log(since=log["cursor"])["events"], [])

    def test_exit_zero_child_with_failed_product_summary_is_not_shown_done(self) -> None:
        event = self.c.plane._ticket_event({
            "lane": "acquisitions", "dir": "x",
            "ticket": {"status": "succeeded", "exit_code": 0,
                       "company_ref": self.c.h.mission["universe"][0]["company_ref"],
                       "completed_at": "2026-09-10T00:00:00+00:00"},
            "summary": {"status": "succeeded", "map_status": "refused"},
            "ticket_mtime": "2026-09-10T00:00:00+00:00",
        }, self.c.plane._members(self.c.h.mission), {})
        self.assertEqual(event["state"], "failed")
        self.assertIn("获取失败", event["title"])

    def test_ask_answers_from_claims_with_citations_and_spends_under_the_mission(self) -> None:
        job = self.c.plane.ask(self.login, {"question": "What did management say about client decisions?", "request_id": "q1"})
        self.assertEqual(job["status"], "running")
        done = self.c.wait(job["job_id"])
        self.assertEqual(done["status"], "done", done["error"])
        result = done["result"]
        self.assertEqual(result["answer"], "Fixture answer.")
        self.assertEqual(result["gaps"],
                         ["nothing on margins（sell_side_report → alphaengine）"])
        self.assertEqual(result["unknowns"][0]["content_kind"], "sell_side_report")
        self.assertEqual(result["confidence"], "medium")
        self.assertEqual(len(result["citations"]), min(1, result["claims_considered"]))
        # P15a: a Core without the Wave 1/2 authorities says so, block by
        # block, rather than showing the same blank for "no table here" and
        # "nothing for this company".
        missing = {item["block"]: item["reason"] for item in result["context"]["missing"]}
        self.assertEqual(missing["valuation"], "no_authority_on_this_core")
        self.assertEqual(missing["consensus"], "reader_not_available")
        self.assertEqual(result["context"]["question_kind"], "other")
        self.assertFalse(result["context"]["wants_market_vs_us"])
        # Q1's deterministic layer ran, and its result is a cockpit artefact.
        self.assertEqual(result["quality"]["rubric_ref"], "rubric:ask-answer")
        self.assertFalse(result["quality"]["recorded"])
        self.assertTrue(result["verification"]["passed"], result["verification"])
        self.assertFalse(result["replayed"])
        self.assertEqual(self.c.adapter.calls, 1)
        # The call was admitted in the day ledger against the mission's caps and settled.
        with ThesisImpactBudgetStore(self.c.root / "budget.sqlite") as budget:
            rows = budget.connection.execute("SELECT mission_ref FROM model_mission_budget_bindings").fetchall()
        self.assertEqual([r["mission_ref"] for r in rows], ["coverage-mission:us-it-services"])
        spend = self.c.plane.overview()["budgets"]["model_calls"]
        self.assertEqual((spend["used"], spend["cost_usd"]), (1, 0.0))
        # The same request replays the persisted result: no second model call.
        again = self.c.wait(self.c.plane.ask(self.login, {"question": "What did management say about client decisions?", "request_id": "q1"})["job_id"])
        self.assertTrue(again["result"]["replayed"])
        self.assertEqual(self.c.adapter.calls, 1)  # the first adapter was never asked again
        # The question is now part of the log, and of the owner's history.
        self.assertTrue(any(e["kind"] == "question" for e in self.c.plane.log()["events"]))
        self.assertEqual(self.c.plane.history(self.login, "ask")[0]["request"]["question"], "What did management say about client decisions?")
        # Another owner cannot read this job.
        with self.assertRaises(CockpitError):
            self.c.plane.job("someone@example.com", job["job_id"])

    def test_goal_draft_publishes_a_new_mission_version_only_on_confirmation(self) -> None:
        before = self.c.h.missions.active_mission("coverage-mission:us-it-services")
        self.c.reply = json.dumps({
            "summary": "研究 AI 对定价权的影响。", "title": "US IT Services：AI 与定价权",
            "objective": "弄清 AI 浪潮下五家公司的定价权变化。",
            "research_questions": ["合同结构如何变化？", "利润率的分化由什么解释？"],
            "subtasks": ["读财报", "读电话会"], "suggested_companies": [{"ticker": "ACN", "reason": "已覆盖"}, {"ticker": "INFY", "reason": "印度同业"}],
        })
        done = self.c.wait(self.c.plane.draft(self.login, "goal", {"text": "我想研究 AI 对定价权的影响", "request_id": "g1"})["job_id"])
        self.assertEqual(done["status"], "done", done["error"])
        draft = done["result"]
        self.assertEqual(draft["draft"]["title"], "US IT Services：AI 与定价权")
        self.assertEqual([c["already_covered"] for c in draft["draft"]["suggested_companies"]], [True, False])
        self.assertTrue(draft["draft"]["changes"]["research_questions"])
        # Nothing was published yet; the draft waits in approvals.
        self.assertEqual(self.c.h.missions.active_mission("coverage-mission:us-it-services")["id"], before["id"])
        pending = self.c.plane.approvals()["items"]
        self.assertIn("draft:goal", [p["kind"] for p in pending])
        # A stale hash is refused; the exact hash publishes version 2.
        with self.assertRaises(CockpitConflict):
            self.c.plane.publish_draft(self.login, {"draft_id": draft["draft_id"], "draft_hash": "0" * 64, "request_id": "p1"})
        out = self.c.plane.publish_draft(self.login, {"draft_id": draft["draft_id"], "draft_hash": draft["draft_hash"], "request_id": "p1"})
        self.assertEqual(out["status"], "published")
        after = self.c.h.missions.active_mission("coverage-mission:us-it-services")
        self.assertEqual((after["version"], after["title"], after["research_questions"]),
                         (before["version"] + 1, "US IT Services：AI 与定价权", ["合同结构如何变化？", "利润率的分化由什么解释？"]))
        self.assertEqual(after["universe"], before["universe"])  # the coverage list is not changed here
        # Publishing twice is refused; the draft is closed.
        with self.assertRaises(CockpitConflict):
            self.c.plane.publish_draft(self.login, {"draft_id": draft["draft_id"], "draft_hash": draft["draft_hash"], "request_id": "p2"})
        self.assertEqual(self.c.plane.drafts(self.login, "goal")[0]["status"], "published")
        self.assertEqual(self.c.plane.overview()["goal"]["version"], after["version"])
        # The new questions reach the extraction prompt through the context.
        prompt = build_prompt({"company_ref": "c", "company_ticker": "ACN", "document_ref": "d", "offset": 0, "end": 1, "quotes": [],
                               "mission_focus": {"objective": after["objective"], "research_questions": after["research_questions"]}})
        self.assertIn("合同结构如何变化？", prompt)

    def test_steer_draft_changes_questions_and_reports_what_it_cannot_do(self) -> None:
        mission = self.c.h.missions.active_mission("coverage-mission:us-it-services")
        current = list(mission["research_questions"])
        self.c.reply = "```json\n" + json.dumps({
            "summary": "增加一个关于人员流失率的问题。", "understood_as": "关注人员流失率",
            "add_questions": ["各公司的人员流失率如何变化？"], "remove_questions": [current[0]],
            "objective": mission["objective"], "not_possible": ["接入新的数据源"],
        }) + "\n```"
        done = self.c.wait(self.c.plane.draft(self.login, "steer", {"text": "多关注人员流失率", "request_id": "s1"})["job_id"])
        self.assertEqual(done["status"], "done", done["error"])
        draft = done["result"]["draft"]
        self.assertEqual(draft["add_questions"], ["各公司的人员流失率如何变化？"])
        self.assertEqual(draft["remove_questions"], [current[0]])
        self.assertEqual(draft["research_questions"], current[1:] + ["各公司的人员流失率如何变化？"])
        self.assertEqual(draft["not_possible"], ["接入新的数据源"])
        self.assertFalse(draft["changes"]["objective"])
        out = self.c.plane.decide(self.login, {"kind": "draft:steer", "ref": done["result"]["draft_id"], "hash": done["result"]["draft_hash"],
                                               "decision": "publish", "request_id": "s1p"})
        self.assertEqual(out["status"], "published")
        after = self.c.h.missions.active_mission("coverage-mission:us-it-services")
        self.assertEqual(after["research_questions"], current[1:] + ["各公司的人员流失率如何变化？"])
        self.assertTrue(any(e["kind"] == "steer" for e in self.c.plane.log()["events"]))

    def test_thesis_admission_appears_as_an_approval_and_decides_through_the_writer(self) -> None:
        pending = self.c.plane.approvals()["items"]
        thesis = [p for p in pending if p["kind"] == "thesis"]
        if not thesis:
            self.skipTest("fixture has no undecided thesis candidate")
        item = thesis[0]
        with self.assertRaises(CockpitError):
            self.c.plane.decide(self.login, {"kind": "thesis", "ref": item["ref"], "hash": item["hash"], "decision": "admit", "rationale": "", "request_id": "r"})
        out = self.c.plane.decide(self.login, {"kind": "thesis", "ref": item["ref"], "hash": item["hash"], "decision": "admit",
                                               "rationale": "reads well", "request_id": "r"})
        self.assertEqual(out["status"], "decided")
        self.assertEqual(self.c.calls[-1][0], "decide_thesis_admission")
        self.assertEqual(self.c.calls[-1][1]["candidate_hash"], item["hash"])

    def test_model_refusals_are_plain_and_budget_exhaustion_fails_closed(self) -> None:
        # A prompt over the input bound never reaches the router.
        model = CockpitModel(self.c.model_config, scheduler_db=self.c.config.scheduler_db, max_input_tokens=10)
        mission = self.c.h.missions.active_mission("coverage-mission:us-it-services")
        with self.assertRaisesRegex(CockpitModelError, "exceed"):
            model.call(purpose="ask", request_id="x", prompt="a" * 50, mission=mission)
        # A day cap below one reservation: the ledger refuses before any model call.
        with ThesisImpactBudgetStore(self.c.root / "budget.sqlite") as budget:
            budget.register_policy(policy_version_id="thesis-impact-day-budget-policy:test:tiny", day_cap_micros=60_000, prior_version_id=BUDGET_POLICY)
        tiny = dict(self.c.model_config, budget_policy_ref="thesis-impact-day-budget-policy:test:tiny")
        adapter = _ScriptedAdapter("{}", created_at=self.c.h.h.clock().isoformat())
        model = CockpitModel(tiny, scheduler_db=self.c.config.scheduler_db, adapter_factory=lambda router: adapter,
                             clock=self.c.h.h.clock, max_cost_usd=0.07)
        with self.assertRaisesRegex(CockpitModelError, "budget"):
            model.call(purpose="ask", request_id="one", prompt="first", mission=mission)
        self.assertEqual(adapter.calls, 0)
        # Within the cap the call runs, is settled at its actual cost, and replays afterwards.
        model = CockpitModel(tiny, scheduler_db=self.c.config.scheduler_db, adapter_factory=lambda router: adapter,
                             clock=self.c.h.h.clock, max_cost_usd=0.05)
        first = model.call(purpose="ask", request_id="two", prompt="second", mission=mission)
        self.assertEqual((first["replayed"], first["cost_status"], adapter.calls), (False, "actual", 1))
        self.assertTrue(model.call(purpose="ask", request_id="two", prompt="second", mission=mission)["replayed"])
        self.assertEqual(adapter.calls, 1)

    def test_the_same_question_asked_later_replays_instead_of_conflicting(self) -> None:
        """P13aa: the WorkOrder id excludes the clock; its hash did not.

        The id is content-addressed on (purpose, request_id, mission version,
        prompt) precisely so the same question is the same work. But the
        scheduler hashes the whole wire, which carried a wall-clock created_at,
        so asking again a second later produced the same idempotency key with a
        different hash -- the scheduler's definition of a conflict. "this
        request is bound to different content; ask again", and asking again
        could never help.

        Live, every Initial Screen section failed this way for two days, on a
        deliverable whose inputs had by then been completed. The existing
        replay test could not catch it because it froze the clock, which is the
        one condition under which the bug does not appear.
        """

        mission = self.c.h.missions.active_mission("coverage-mission:us-it-services")
        adapter = _ScriptedAdapter("{}", created_at=self.c.h.h.clock().isoformat())
        model = CockpitModel(self.c.model_config, scheduler_db=self.c.config.scheduler_db,
                             adapter_factory=lambda router: adapter, clock=self.c.h.h.clock)
        first = model.call(purpose="ask", request_id="same", prompt="the same question",
                           mission=mission)
        self.assertFalse(first["replayed"])
        # Time passes, as it does between two ticks of a lane.
        self.c.h.h.clock.advance(seconds=3600)
        second = model.call(purpose="ask", request_id="same", prompt="the same question",
                            mission=mission)
        self.assertTrue(second["replayed"])
        self.assertEqual(second["work_order_ref"], first["work_order_ref"])
        self.assertEqual(adapter.calls, 1)

    def test_a_corrected_identity_does_not_collide_with_its_own_history(self) -> None:
        # P13aa: fixing the definition was not enough. The keys written under
        # the old definition still held the old hash, so live the corrected
        # request kept conflicting with rows written two days earlier. A
        # changed identity definition is a changed identity.
        from dalton_core.cockpit_model import IDENTITY_VERSION, build_work

        common = dict(purpose="ask", request_id="a", prompt="one",
                      mission_version_ref="coverage-mission-version:x:1",
                      max_input_tokens=1000, max_output_tokens=10,
                      max_cost_usd=0.01, max_seconds=10)
        self.assertGreaterEqual(IDENTITY_VERSION, 2)
        work = build_work(**common, created_at="2026-09-01T00:00:00.000000+00:00")
        # The identity version participates, so version 1's keys are not reused.
        legacy = content_hash({
            "purpose": "ask", "request_id": "a",
            "mission_version_ref": "coverage-mission-version:x:1",
            "prompt_sha256": hashlib.sha256(b"one").hexdigest(),
        })
        self.assertNotIn(legacy[:32], work.id)
        self.assertNotEqual(work.idempotency_key, f"cockpit:ask:{legacy}")

    def test_a_different_question_is_still_different_work(self) -> None:
        # The identity must not have become so loose that two questions share
        # one answer.
        mission = self.c.h.missions.active_mission("coverage-mission:us-it-services")
        adapter = _ScriptedAdapter("{}", created_at=self.c.h.h.clock().isoformat())
        model = CockpitModel(self.c.model_config, scheduler_db=self.c.config.scheduler_db,
                             adapter_factory=lambda router: adapter, clock=self.c.h.h.clock)
        first = model.call(purpose="ask", request_id="a", prompt="one", mission=mission)
        self.c.h.h.clock.advance(seconds=3600)
        second = model.call(purpose="ask", request_id="a", prompt="two", mission=mission)
        self.assertNotEqual(second["work_order_ref"], first["work_order_ref"])
        self.assertFalse(second["replayed"])
        self.assertEqual(adapter.calls, 2)

    def test_configured_purpose_budget_is_effective_and_changes_work_identity(self) -> None:
        mission = self.c.h.missions.active_mission("coverage-mission:us-it-services")
        adapter = _ScriptedAdapter("{}", created_at=self.c.h.h.clock().isoformat())
        first_config = {**self.c.model_config, "purpose_call_budgets": {
            "ask": {"max_input_tokens": 90_000, "max_output_tokens": 900,
                    "max_cost_usd": 0.9, "timeout_seconds": 90},
            "goal": {"max_output_tokens": 700},
        }}
        first_model = CockpitModel(
            first_config, scheduler_db=self.c.config.scheduler_db,
            adapter_factory=lambda router: adapter, clock=self.c.h.h.clock)
        self.assertEqual(first_model.budget_for("ask"), {
            "max_input_tokens": 90_000, "max_output_tokens": 900,
            "max_cost_usd": 0.9, "timeout_seconds": 90,
        })
        self.assertEqual(first_model.budget_for("goal")["max_output_tokens"], 700)
        first = first_model.call(purpose="ask", request_id="budgeted", prompt="same",
                                 mission=mission)
        changed = {**first_config, "purpose_call_budgets": {
            **first_config["purpose_call_budgets"],
            "ask": {**first_config["purpose_call_budgets"]["ask"],
                    "max_output_tokens": 901},
        }}
        second = CockpitModel(
            changed, scheduler_db=self.c.config.scheduler_db,
            adapter_factory=lambda router: adapter, clock=self.c.h.h.clock,
        ).call(purpose="ask", request_id="budgeted", prompt="same", mission=mission)
        self.assertNotEqual(first["work_order_ref"], second["work_order_ref"])
        self.assertEqual(adapter.calls, 2)

    def test_setup_points_the_service_config_at_the_state(self) -> None:
        root = (self.c.root / "svc").resolve(); root.mkdir()
        config = root / "service.json"
        config.write_text(json.dumps({
            "core_db": str(root / "core.sqlite"), "heartbeat_path": str(root / "run" / "heartbeat.json"),
            "scheduler_db": str(root / "scheduler.sqlite"),
            "control": {"config": {"research_review": {"document_extraction_model_config_path": str(root / "m.json")}}},
        }), encoding="utf-8")
        first = install_cockpit(config)
        self.assertTrue(first["service_config_changed"])
        self.assertEqual(first["cockpit"]["journal_path"], str(root / "cockpit" / "journal.sqlite"))
        self.assertEqual(first["cockpit"]["model_config_path"], str(root / "m.json"))
        self.assertTrue((root / "cockpit").is_dir())
        second = install_cockpit(config)
        self.assertFalse(second["service_config_changed"])
        self.assertEqual(json.loads(config.read_text())["control"]["config"]["cockpit"], first["cockpit"])

    def test_unwrap_json_object_tolerates_fences_and_prose(self) -> None:
        self.assertEqual(unwrap_json_object('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(unwrap_json_object('Sure. {"a": [1, 2]} That is all.'), {"a": [1, 2]})
        self.assertIsNone(unwrap_json_object("no json here"))
        self.assertIsNone(unwrap_json_object("[1, 2]"))


if __name__ == "__main__":
    unittest.main()

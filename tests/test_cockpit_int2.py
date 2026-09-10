"""INT2: daily tracking, the calendar, the source map and the weekly look back.

Six lanes landed with a "what the cockpit needs" section and no cockpit change,
because the two cockpit files were nobody's to touch. This is that wiring, and
what it mostly has to get right is the same thing INT1's did: the page runs on
Cores older than every one of these lanes, so a card with no events must be the
card it always was rather than an error. Half of what follows is that case.

The other half is that most of what arrives here is a *judgement* rather than a
count. "NO_CHANGE / note" is the right pair of words for a tick summary and the
wrong pair for the owner's page; a T-22 beside a vendor's guess and a T-22
beside the company's own announcement are the same eleven pixels; and a
follow-up written into a reflection changed nothing at all. Each of those is a
sentence on the page and a test here.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from dalton_core.catalyst_calendar import CatalystCalendarAuthority
from dalton_core.cockpit_plane import CockpitPlane
from dalton_core.event_judgement import EventJudgementAuthority
from dalton_core.research_event import ResearchEventAuthority
from dalton_core.tracking_cadence import TrackingCadenceAuthority, load_policy
from tests.test_cockpit_plane import CockpitHarness

ACN = "company:sec-cik:0001467373"
IBM = "company:sec-cik:0000051143"
OWNER = "human:lumos"
LOGIN = "owner@example.com"
POLICY_FILE = (Path(__file__).resolve().parents[1]
               / "deploy" / "phase9" / "p14a-tracking-policy-v1.json")


def vendor_source(day: str) -> dict:
    return {"kind": "connector_invocation", "ref": "connector-invocation:yfinance:aaaa",
            "source_ref": "source:yahoo-finance", "observed_date": day,
            "observed_at": "2026-09-09T15:00:00+00:00", "confidence": "estimated",
            "note": ""}


def filed_source(day: str) -> dict:
    return {"kind": "filing", "ref": "sec:filing:0001467373-26-000031",
            "source_ref": "source:sec-edgar", "observed_date": day,
            "observed_at": "2026-09-09T12:00:00+00:00", "confidence": "confirmed",
            "note": "8-K Item 2.02"}


class Int2Case(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.c = CockpitHarness(self.root)
        self.addCleanup(self.c.close)
        self.store = self.c.h.h.core
        self.plane = self.c.plane
        self.mission = self.c.h.missions.active_mission("coverage-mission:us-it-services")

    def card(self, company_ref: str = ACN) -> dict:
        view = self.plane.overview()
        return next(c for c in view["companies"] if c["company_ref"] == company_ref)

    # -- fixtures ----------------------------------------------------------

    def events(self) -> ResearchEventAuthority:
        return ResearchEventAuthority(self.store)

    def price_move(self, *, company_ref: str = ACN, close: str = "104") -> dict:
        return self.events().record(
            company_ref=company_ref, kind="price_move",
            occurred_at="2026-09-08T00:00:00+00:00",
            source_refs=["market-price-series-version:1"],
            payload={"as_of": "2026-09-08", "close": close, "previous_close": "100",
                     "return_percent": "4.0000", "direction": "up",
                     "trigger": "absolute", "threshold_percent": "3.0"},
            evidence_tier=None, mission=self.mission, actor_ref=OWNER,
        )

    def news(self, title: str = "ACN wins a large deal") -> dict:
        return self.events().record(
            company_ref=ACN, kind="news", occurred_at="2026-09-08T00:00:00+00:00",
            source_refs=["coverage-mission-discovered-document:1"],
            payload={"document_ref": "doc:1", "title": title, "host": "example.com"},
            evidence_tier=None, mission=self.mission, actor_ref=OWNER,
        )

    def judgement(self, event: dict, *, decision: str = "THESIS_WEAKENED",
                  action: str = "note", verdict: str = "pass",
                  effect: dict | None = None) -> dict:
        return EventJudgementAuthority(self.store).record(
            event=event,
            judgement={
                "decision": decision, "action": action, "driver_refs": [],
                "thesis_refs": [], "because": "定价压力比我们以为的更快出现",
                "citations": ["claim:1"], "note": "一段短报告。",
                "model": {"work_order_ref": "work:event-judgement-1",
                          "cost_micros": 1000},
            },
            verification={"status": "verified", "verdict": verdict, "findings": [],
                          "independence": {"producer_family": "a", "verifier_family": "b"},
                          "model": {"work_order_ref": "work:event-verifier-1",
                                    "cost_micros": 1000}},
            effect=effect or {"status": "published", "ref": "deliverable:1"},
            mission=self.mission, actor_ref=OWNER,
        )

    def reflection(self, judgement: dict, event: dict) -> dict:
        return EventJudgementAuthority(self.store).record_reflection(
            judgement=judgement, event=event,
            reflection={
                "thesis_refs": [], "what_we_expected": "价格战到明年才开始",
                "what_happened": "这个季度就开始了", "why": "客户预算提前收紧",
                "citations": ["claim:1"],
                "missed_debates": [{"question": "折扣是不是结构性的", "refs": ["claim:1"]}],
                "followup_tracking": [{"source_key": "alphaengine",
                                       "interval_seconds": 43200,
                                       "because": "研报里最先看得到"}],
                "followup_research": [{"question": "折扣有多深", "wants": "合同条款"}],
                "market_view_vs_ours": {"available": False, "our_direction": "long",
                                        "summary": "", "refs": []},
                "convergence_pathway": "下一季的毛利率",
                "model": {"work_order_ref": "work:reflection-1", "cost_micros": 1000},
            },
            verification={"status": "verified", "verdict": "pass", "findings": [],
                          "independence": {"producer_family": "a", "verifier_family": "b"},
                          "model": {"work_order_ref": "work:reflection-verifier-1",
                                    "cost_micros": 1000}},
            mission=self.mission, actor_ref=OWNER,
        )

    def calendar(self, day: str, *, confirmed: bool = False) -> dict:
        return CatalystCalendarAuthority(self.store).publish(
            company_ref=ACN,
            entries=[{"event_kind": "earnings", "notes": "",
                      "sources": [filed_source(day) if confirmed
                                  else vendor_source(day)]}],
            change_reason="evidence_thicker", evidence_refs=["evidence:one"],
            now="2026-09-09",
        )

    def install_policy(self) -> None:
        (self.root / "tracking-policy.json").write_bytes(POLICY_FILE.read_bytes())

    def admit_inquiry(self) -> dict:
        """One ad-hoc research task, admitted the way the lane admits one.

        Built through the two authorities rather than by inserting rows: the
        card reads what P14e writes, and a shape invented here would pass a
        test about nothing.
        """

        from dalton_core.bounded_planner_loop import BoundedPlannerAuthority
        from dalton_core.research_question_backlog import ResearchQuestionBacklog
        from dalton_core.store import content_hash

        authority = BoundedPlannerAuthority(self.store)
        template = authority.publish_probe_template(
            "probe-template:adhoc-sec-filings-index:v1",
            actor_ref=OWNER,
            **__import__("dalton_core.research_task", fromlist=["x"])
            .publication_arguments(
                next(spec for spec in __import__(
                    "dalton_core.research_task", fromlist=["x"]
                ).ADHOC_PROBE_TEMPLATES
                    if spec["template_ref"]
                    == "probe-template:adhoc-sec-filings-index:v1")),
        )
        # The bootstrap mandate covers the industry, not the companies in it,
        # and a question is recorded under an exact mandate version.
        mandate = self.c.h.state["agenda"].create_mandate(
            "mandate:us-it-services-constitution-p8a", actor_ref=OWNER,
            objective="Establish US IT Services coverage.",
            scope_refs=["industry:us-it-services", ACN], constraints={},
            success_criteria={}, effective_from="2026-08-23T00:00:00+00:00",
            effective_until=None,
            version_id="mandate-version:us-it-services-constitution-p8a:2",
            idempotency_key="int2:mandate:2",
        )
        question = ResearchQuestionBacklog(self.store).record_question(
            mandate_version_ref=mandate["id"],
            company_ref=ACN, question="折扣有多深？",
            answer_criteria="合同条款里的折扣幅度", source_refs=["source:sec-edgar"],
            actor_ref=self.mission["autonomy"]["automation_principal"],
        )
        return authority.create_loop(
            "bounded-loop:inquiry:int2",
            question_version_ref=question["question_version_ref"],
            template_bindings=[{
                "template_version_ref": template["id"],
                "coverage_item_ref": "coverage:int2:acn",
                "parameters": {"source_ref": "source:sec-edgar",
                               "locator": "0001467373",
                               "query_terms": ["Revenues", "10-Q"]},
            }],
            required_coverage_items=["coverage:int2:acn"],
            budget={"max_rounds": 2, "max_cost_units": 2, "max_seconds": 240},
            actor_ref=self.mission["autonomy"]["automation_principal"],
            admission={"source": "inquiry", "content_hash": "e" * 64,
                       "inquiry_ref": "inquiry:int2", "plan_ref": "plan:int2"},
        )

    def bare_plane(self, *, budget_db: Path | None = None) -> CockpitPlane:
        """A plane whose model configuration names neither router nor pools.

        Which is the shape a Core has before the model side is installed at
        all, and the shape every one of these panels has to survive.
        """

        from dalton_core.cockpit_plane import CockpitConfig

        config = dict(self.c.model_config)
        config.pop("model_router_db", None)
        if budget_db is None:
            config.pop("budget_db", None)
        else:
            config["budget_db"] = str(budget_db)
        path = self.root / "bare-model-config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        plane = CockpitPlane(
            CockpitConfig.from_mapping({
                "core_db": str(self.c.core_path), "state_dir": str(self.root),
                "heartbeat_path": str(self.c.heartbeat),
                "scheduler_db": str(self.root / "bare-scheduler.sqlite"),
                "journal_path": str(self.root / "cockpit" / "bare-journal.sqlite"),
                "model_config_path": str(path),
                "mission_ref": "coverage-mission:us-it-services",
            }),
            writer_socket=self.root / "w.sock", token_config=self.root / "t.json",
            clock=self.c.h.h.clock,
        )
        self.addCleanup(plane.close)
        return plane


class EventsOnTheCardTests(Int2Case):
    def test_a_core_without_the_table_shows_the_card_it_always_showed(self) -> None:
        card = self.card()
        self.assertIsNone(card["events"])
        self.assertIsNone(card["judgements"])
        self.assertIsNone(card["reflections"])
        self.assertIsNone(card["catalyst"])
        self.assertIsNone(card["research_tasks"])

    def test_events_are_counted_by_kind_and_carry_what_they_are_worth(self) -> None:
        self.price_move()
        self.news()
        card = self.card()
        self.assertEqual(card["events"]["total"], 2)
        labels = {row["label"]: row["count"] for row in card["events"]["kinds"]}
        self.assertEqual(labels, {"股价异动": 1, "新闻": 1})
        # The tier travels with the event: a price and a news story are both
        # events and are not believed to the same degree.
        tiers = {row["kind"]: row["tier_label"] for row in card["events"]["latest"]}
        self.assertEqual(tiers["price_move"], "市场价格")
        self.assertEqual(tiers["news"], "新闻报道")

    def test_an_event_says_what_happened_rather_than_naming_a_payload(self) -> None:
        self.price_move()
        latest = self.card()["events"]["latest"][0]
        self.assertIn("涨", latest["summary"])
        self.assertIn("4.0000%", latest["summary"])

    def test_one_company_s_events_do_not_appear_on_another_s_card(self) -> None:
        self.price_move(company_ref=IBM)
        self.assertIsNone(self.card(ACN)["events"])
        self.assertEqual(self.card(IBM)["events"]["total"], 1)


class JudgementOnTheCardTests(Int2Case):
    def test_the_decision_is_a_sentence_and_carries_its_because(self) -> None:
        self.judgement(self.price_move())
        latest = self.card()["judgements"]["latest"][0]
        self.assertEqual(latest["decision_label"], "论点被削弱了")
        self.assertEqual(latest["action_label"], "写一段短报告")
        self.assertEqual(latest["because"], "定价压力比我们以为的更快出现")

    def test_the_page_shows_what_the_effect_actually_was(self) -> None:
        # A queued effect and a published one are different facts, and the
        # decision reads identically in both.
        self.judgement(self.price_move(),
                       effect={"status": "queued", "reason": "no dossier lane yet"})
        latest = self.card()["judgements"]["latest"][0]
        self.assertEqual(latest["effect"], "queued")
        self.assertEqual(latest["effect_detail"], "no dossier lane yet")

    def test_a_verdict_and_the_absence_of_one_do_not_look_alike(self) -> None:
        self.judgement(self.price_move())
        self.assertEqual(
            self.card()["judgements"]["latest"][0]["verifier_label"], "独立复核通过")

    def test_no_change_is_recorded_and_shown(self) -> None:
        # "why did nothing change" is the weekly-meeting question, and the
        # answer only exists if the page carries the decisions that changed
        # nothing.
        self.judgement(self.price_move(), decision="NO_CHANGE", action="no_change")
        latest = self.card()["judgements"]["latest"][0]
        self.assertEqual(latest["decision_label"], "不用改主意")
        self.assertEqual(latest["action_label"], "什么都不做")


class ReflectionOnTheCardTests(Int2Case):
    def test_the_four_questions_are_on_the_card(self) -> None:
        event = self.price_move()
        self.reflection(self.judgement(event), event)
        row = self.card()["reflections"][0]
        self.assertEqual(row["what_we_expected"], "价格战到明年才开始")
        self.assertEqual(row["what_happened"], "这个季度就开始了")
        self.assertEqual(row["missed_debates"][0]["question"], "折扣是不是结构性的")
        self.assertEqual(row["followup_research"][0]["question"], "折扣有多深")

    def test_a_follow_up_says_it_changed_nothing(self) -> None:
        event = self.price_move()
        self.reflection(self.judgement(event), event)
        row = self.card()["reflections"][0]
        self.assertIn("候选", row["note"])
        # The cadence is untouched: the reflection has no path to change one.
        self.assertIsNone(TrackingCadenceAuthority(self.store).latest(ACN, "alphaengine"))
        self.assertEqual(row["followup_tracking"][0]["interval_label"], "每天 2 次")

    def test_no_street_view_is_said_out_loud_rather_than_left_blank(self) -> None:
        # With no consensus authority in this Core the honest answer is that
        # we have nothing to compare ourselves against; a blank reads as
        # agreement.
        event = self.price_move()
        self.reflection(self.judgement(event), event)
        market = self.card()["reflections"][0]["market_view"]
        self.assertFalse(market["available"])
        self.assertIn("没有街上的看法", market["summary"])


class CatalystOnTheCardTests(Int2Case):
    def setUp(self) -> None:
        super().setUp()
        # T-N is counted from today, and the harness clock walks forward as
        # the fixtures run; without pinning it a test written at 23:53 UTC
        # counts one day fewer than the same test run at 00:07.
        from datetime import datetime, timezone

        fixed = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
        self.plane.clock = lambda: fixed  # type: ignore[method-assign]

    def test_the_headline_counts_the_days_and_names_the_caveat(self) -> None:
        self.calendar("2026-10-01")
        card = self.card()
        self.assertEqual(card["catalyst"]["headline"], "下一个催化剂 T−22 天")
        self.assertEqual(card["catalyst"]["event_label"], "业绩发布")
        # A vendor's guess and the company's own announcement look identical
        # in "T-22 days" unless something says which it is.
        self.assertTrue(card["catalyst"]["date_unconfirmed"])
        self.assertEqual(card["catalyst"]["date_caveat"], "日期未确认")

    def test_a_confirmed_date_carries_no_caveat(self) -> None:
        self.calendar("2026-10-01", confirmed=True)
        card = self.card()
        self.assertFalse(card["catalyst"]["date_unconfirmed"])
        self.assertEqual(card["catalyst"]["date_caveat"], "")

    def test_a_calendar_with_nothing_forthcoming_shows_nothing(self) -> None:
        self.calendar("2026-06-18", confirmed=True)
        self.assertIsNone(self.card()["catalyst"])


class CadenceOnTheCardTests(Int2Case):
    def test_without_a_policy_there_is_no_frequency_block(self) -> None:
        plane = self.bare_plane()
        plane._tracking_policy = lambda: None  # type: ignore[method-assign]
        card = next(c for c in plane.overview()["companies"]
                    if c["company_ref"] == ACN)
        self.assertIsNone(card["cadence"])

    def test_every_source_shows_its_baseline_and_the_reason_for_it(self) -> None:
        self.install_policy()
        rows = {row["source_key"]: row for row in self.card()["cadence"]}
        self.assertEqual(rows["yfinance"]["interval_label"], "每天 4 次")
        self.assertEqual(rows["guidepoint"]["interval_label"], "每 7 天一次")
        self.assertTrue(rows["yfinance"]["because"])
        # The brain sets the frequency; it does not decide whether to look.
        self.assertFalse(rows["yfinance"]["adjustable"])
        self.assertTrue(rows["alphaengine"]["adjustable"])
        self.assertFalse(rows["alphaengine"]["decided_by_brain"])

    def test_the_brain_s_own_version_replaces_the_baseline_with_its_reason(self) -> None:
        self.install_policy()
        policy = load_policy(POLICY_FILE)
        TrackingCadenceAuthority(self.store).propose(
            company_ref=ACN, source_key="alphaengine", interval_seconds=259200,
            because="连续三次搜索没有带回这家公司还没有的东西",
            evidence_refs=["research-event:one"], change_reason="driver_event",
            decision="NO_CHANGE", policy=policy, mission=self.mission,
            actor_ref=OWNER,
        )
        rows = {row["source_key"]: row for row in self.card()["cadence"]}
        self.assertEqual(rows["alphaengine"]["interval_label"], "每 3 天一次")
        self.assertEqual(rows["alphaengine"]["baseline_label"], "每天 2 次")
        self.assertTrue(rows["alphaengine"]["decided_by_brain"])
        self.assertIn("连续三次", rows["alphaengine"]["because"])
        # A company nobody re-timed still shows the baselines rather than
        # nothing: the frequencies apply to it too.
        other = {row["source_key"]: row for row in self.card(IBM)["cadence"]}
        self.assertEqual(other["alphaengine"]["interval_label"], "每天 2 次")


class ApprovalsTests(Int2Case):
    def candidate(self) -> dict:
        event = self.price_move()
        judgement = self.judgement(event, action="revise_thesis")
        reflection = self.reflection(judgement, event)
        return EventJudgementAuthority(self.store).record_thesis_candidate(
            judgement_ref=judgement["id"],
            thesis={"id": "thesis-version:1", "content_hash": "a" * 64,
                    "thesis_ref": "thesis:acn"},
            company_ref=ACN, decision="THESIS_WEAKENED",
            because="定价压力比我们以为的更快出现",
            evidence_refs=[event["id"]], falsifier_ref="falsifier:1",
            proposed_statement="定价压力会压到明年的利润率",
            proposed_confidence="medium", reflection_ref=reflection["id"],
            mission=self.mission, actor_ref=OWNER,
        )

    def test_a_core_without_the_tables_lists_no_revision_checkpoints(self) -> None:
        kinds = {item["kind"] for item in self.plane.approvals()["items"]}
        self.assertNotIn("thesis_revision_candidate", kinds)
        self.assertNotIn("gate_reopen", kinds)

    def test_a_revision_candidate_renders_with_a_plain_title(self) -> None:
        self.candidate()
        item = next(i for i in self.plane.approvals()["items"]
                    if i["kind"] == "thesis_revision_candidate")
        self.assertEqual(item["title"], "有事情发生，可能要改我们对这家公司的判断")
        self.assertEqual(item["details"]["大脑的判断"], "论点被削弱了")
        # ADR-0007: the candidate and "what we may have missed" are worth the
        # same when a person is deciding, so they arrive together.
        self.assertEqual(item["reflection"]["what_happened"], "这个季度就开始了")

    def test_a_checkpoint_with_no_decision_path_offers_no_buttons(self) -> None:
        # A button that goes nowhere is worse than an item that says who owes
        # what: the ops that decide these are on another branch.
        self.candidate()
        item = next(i for i in self.plane.approvals()["items"]
                    if i["kind"] == "thesis_revision_candidate")
        self.assertEqual(item["actions"], [])
        self.assertIn("ADR-0007", item["note"])

    def test_a_gate_reopen_row_renders_when_a_table_exists(self) -> None:
        # Written by hand because the lane that writes them is not on this
        # branch. What is being pinned is that the page renders them if they
        # are there rather than needing a change when they arrive.
        with closing(sqlite3.connect(self.c.core_path)) as core:
            core.execute(
                "CREATE TABLE gate_reopen_proposals(proposal_id TEXT PRIMARY KEY,"
                "company_ref TEXT, record_json TEXT, content_hash TEXT,"
                "created_at TEXT)")
            core.execute(
                "INSERT INTO gate_reopen_proposals VALUES(?,?,?,?,?)",
                ("gate-reopen:1", ACN,
                 json.dumps({"company_ref": ACN, "because": "现在有了四个季度的纪要"}),
                 "b" * 64, "2026-09-09T00:00:00+00:00"))
            core.commit()
        item = next(i for i in self.plane.approvals()["items"]
                    if i["kind"] == "gate_reopen")
        self.assertEqual(item["title"], "一道已经过掉的闸，现在有证据说可以重开")
        self.assertEqual(item["summary"], "现在有了四个季度的纪要")

    def test_a_decided_checkpoint_drops_off_when_a_decisions_table_exists(self) -> None:
        candidate = self.candidate()
        with closing(sqlite3.connect(self.c.core_path)) as core:
            core.execute(
                "CREATE TABLE thesis_revision_decisions(decision_id TEXT PRIMARY KEY,"
                "candidate_ref TEXT)")
            core.commit()
            self.assertIn(
                "thesis_revision_candidate",
                {i["kind"] for i in self.plane.approvals()["items"]})
            core.execute("INSERT INTO thesis_revision_decisions VALUES(?,?)",
                         ("d:1", candidate["id"]))
            core.commit()
        self.assertNotIn(
            "thesis_revision_candidate",
            {i["kind"] for i in self.plane.approvals()["items"]})

    def test_a_forecast_proposal_says_why_it_was_not_applied(self) -> None:
        judgement = self.judgement(self.price_move(), action="revise_forecast")
        EventJudgementAuthority(self.store).record_forecast_proposal(
            judgement_ref=judgement["id"], company_ref=ACN,
            model_version_ref=None,
            change={"driver_ref": "driver:revenue", "period_end": "2026-12-31",
                    "value": "1000"},
            decision="THESIS_WEAKENED", because="定价压力",
            evidence_refs=["research-event:one"],
            reason="mission does not grant forecast_line",
            mission=self.mission, actor_ref=OWNER,
        )
        item = next(i for i in self.plane.approvals()["items"]
                    if i["kind"] == "forecast_proposal")
        self.assertEqual(item["title"], "预测行想改，但研究目标没有授权自动改")
        self.assertEqual(item["details"]["为什么没直接改"],
                         "mission does not grant forecast_line")


class SourcePanelTests(Int2Case):
    def test_every_source_says_what_it_yields_and_what_it_is_worth(self) -> None:
        view = self.plane.sources()
        rows = {row["slug"]: row for row in view["sources"]}
        self.assertIn("alphaengine", rows)
        self.assertTrue(rows["alphaengine"]["content"])
        self.assertTrue(rows["alphaengine"]["tier_label"])
        self.assertTrue(rows["alphaengine"]["status_label"])

    def test_the_generic_sources_are_marked_and_sorted_last(self) -> None:
        # A generic source is what you use when nothing specific holds the
        # answer, not what you reach for first.
        view = self.plane.sources()
        generic = [row["slug"] for row in view["sources"] if row["generic"]]
        self.assertTrue(generic)
        self.assertEqual([row["slug"] for row in view["sources"]][-len(generic):],
                         generic)
        for slug in generic:
            row = next(r for r in view["sources"] if r["slug"] == slug)
            self.assertIn("通用来源", row["generic_note"])

    def test_a_source_the_mission_never_mentioned_is_not_reported_as_refused(self) -> None:
        rows = {row["slug"]: row for row in self.plane.sources()["sources"]}
        undeclared = [row for row in rows.values()
                      if row["status"] == "undeclared"]
        self.assertTrue(undeclared)
        self.assertEqual(undeclared[0]["status_label"], "研究目标里没提过它")

    def test_without_a_policy_the_frequency_column_says_so(self) -> None:
        # In a source checkout the packaged policy is always reachable, so the
        # case worth pinning is the deployed one: no policy installed, and the
        # column says it is empty rather than showing a guess.
        plane = self.bare_plane()
        plane._tracking_policy = lambda: None  # type: ignore[method-assign]
        view = plane.sources()
        self.assertIsNone(view["policy_ref"])
        self.assertIn("频率", view["policy_note"])
        self.assertTrue(all(row["cadence_label"] is None for row in view["sources"]))

    def test_with_a_policy_each_source_carries_its_baseline(self) -> None:
        self.install_policy()
        view = self.plane.sources()
        self.assertEqual(view["policy_ref"], "tracking-policy:p14a:v1")
        rows = {row["slug"]: row for row in view["sources"]}
        self.assertEqual(rows["alphaengine"]["cadence_label"], "每天 2 次")

    def test_a_quota_is_shown_per_operation(self) -> None:
        rows = {row["slug"]: row for row in self.plane.sources()["sources"]}
        quoted = [row for row in rows.values() if row["quotas"]]
        self.assertTrue(quoted)
        self.assertTrue(all(q["daily_limit"] for row in quoted for q in row["quotas"]))


class RoutingPanelTests(Int2Case):
    def test_without_a_router_database_the_panel_says_why(self) -> None:
        routing = self.bare_plane().sources()["routing"]
        self.assertFalse(routing["available"])
        self.assertIn("模型路由库", routing["reason"])

    def test_each_tier_shows_its_chain_in_order(self) -> None:
        from dalton_core.model_router import ModelRouter

        ModelRouter(str(self.root / "router.sqlite")).close()
        routing = self.plane.sources()["routing"]
        self.assertTrue(routing["available"])
        tiers = {row["tier"]: row for row in routing["tiers"]}
        self.assertEqual([link["position"] for link in tiers["cheap"]["chain"]],
                         list(range(1, len(tiers["cheap"]["chain"]) + 1)))
        # A model with no profile here is named as missing rather than
        # silently dropped out of the chain.
        self.assertIn("没有这个模型的档案",
                      str([link["note"] for link in tiers["cheap"]["chain"]]))
        self.assertIn("还没有用过", tiers["cheap"]["last_served_note"])

    def test_a_purpose_with_no_tier_is_named_rather_than_left_out(self) -> None:
        # P14a once registered event_judgement and thesis_reflection without a
        # tier; the tier map has since been seeded for them, so the panel is
        # exercised here with a purpose registered on purpose without a tier.
        # The day a chain is pinned for such a policy the call is refused
        # rather than falling back to a default, which is exactly the kind of
        # thing this panel exists to make visible before it bites.
        from dalton_core.cockpit_model import register_purpose
        from dalton_core.model_router import ModelRouter

        register_purpose("int2_untiered_probe")
        ModelRouter(str(self.root / "router.sqlite")).close()
        routing = self.plane.sources()["routing"]
        self.assertTrue(routing["purposes"])
        self.assertIn("int2_untiered_probe", routing["unmapped_purposes"])
        self.assertNotIn("event_judgement", routing["unmapped_purposes"])
        self.assertNotIn("thesis_reflection", routing["unmapped_purposes"])
        self.assertIn("层级", routing["unmapped_note"])

    def test_without_an_openclaw_config_the_catalog_is_not_guessed(self) -> None:
        # The cockpit never goes looking for the host's gateway configuration
        # on its own: it is told where it is, and a Core installed without the
        # gateway has no catalog to compare against and says so.
        from dalton_core.model_router import ModelRouter

        ModelRouter(str(self.root / "router.sqlite")).close()
        self.assertIsNone(self.c.config.openclaw_config_path)
        routing = self.plane.sources()["routing"]
        self.assertIsNone(routing["catalog"])
        self.assertIn("OpenClaw", routing["catalog_note"])

    def test_with_an_openclaw_config_both_diff_sets_are_named(self) -> None:
        # "Out of sync" on its own tells nobody what to do; which way it is
        # out of sync does.
        from dalton_core.cockpit_plane import CockpitConfig
        from dalton_core.model_router import ModelRouter

        ModelRouter(str(self.root / "router.sqlite")).close()
        broker = self.root / "openclaw-config.json"
        broker.write_text(json.dumps({"plugins": {}}), encoding="utf-8")
        plane = CockpitPlane(
            CockpitConfig.from_mapping({
                "core_db": str(self.c.core_path), "state_dir": str(self.root),
                "heartbeat_path": str(self.c.heartbeat),
                "scheduler_db": str(self.root / "broker-scheduler.sqlite"),
                "journal_path": str(self.root / "cockpit" / "broker-journal.sqlite"),
                "model_config_path": str(self.c.model_config_path),
                "openclaw_config_path": str(broker),
                "mission_ref": "coverage-mission:us-it-services",
            }),
            writer_socket=self.root / "w.sock", token_config=self.root / "t.json",
            clock=self.c.h.h.clock)
        self.addCleanup(plane.close)
        catalog = plane.sources()["routing"]["catalog"]
        self.assertIsNotNone(catalog)
        self.assertIn("in_sync", catalog)
        self.assertIn("missing_here", catalog)
        self.assertIn("not_in_broker", catalog)
        self.assertTrue(catalog["note"])


class WeeklyLookBackTests(Int2Case):
    def test_a_core_without_the_table_says_so_rather_than_raising(self) -> None:
        view = self.plane.cycle_reflection()
        self.assertFalse(view["available"])
        self.assertIn("每周回头看", view["reason"])

    def test_the_prose_and_the_table_are_shown_as_written(self) -> None:
        from dalton_core.research_cycle_reflection import (
            ResearchCycleReflectionAuthority,
        )

        authority = ResearchCycleReflectionAuthority(self.store)
        body = {
            "mission_ref": "coverage-mission:us-it-services",
            "mission_version_ref": self.mission["id"],
            "iso_week": "2026-W36",
            "window": {"start": "2026-08-31T00:00:00-04:00",
                       "end": "2026-09-07T00:00:00-04:00"},
            "metrics": {},
            "inputs_hash": "c" * 64,
            "narrative": {
                "title": "我们把时间花在哪",
                "prose": "这一周一共花了 $0.03。\n最大的一池是 agenda。",
                "table": [{"pool": "agenda", "lane": None, "cost_usd": "0.02",
                           "calls": 12, "share_of_spend": "63%",
                           "share_of_mission_cap": "0.0%"}],
            },
            "backlog_candidates": [
                {"question": "为什么没有人给过反馈？", "because": "两处都还没有 UI",
                 "refs": []}],
            "policy_suggestions": ["tick 摘要应当落到 Core 的一张表"],
            "authority_note": "这条记录不改 policy，也不登记问题。",
        }
        authority.record(body, actor_ref=OWNER)
        view = self.plane.cycle_reflection()
        self.assertTrue(view["available"])
        self.assertEqual(view["iso_week"], "2026-W36")
        self.assertEqual(view["title"], "我们把时间花在哪")
        self.assertIn("最大的一池", view["prose"])
        self.assertEqual(view["table"][0]["pool"], "agenda")
        self.assertEqual(view["backlog_candidates"][0]["question"],
                         "为什么没有人给过反馈？")
        self.assertEqual(view["policy_suggestions"],
                         ["tick 摘要应当落到 Core 的一张表"])


class BudgetPoolTests(Int2Case):
    def test_a_ledger_from_before_the_pools_shows_no_pool_panel(self) -> None:
        # A day ledger installed before C2 has no ``pool`` column at all; the
        # page must degrade to no panel rather than raise.
        old = self.root / "old-budget.sqlite"
        with closing(sqlite3.connect(old)) as ledger:
            ledger.execute(
                "CREATE TABLE thesis_impact_day_admissions(admission_id TEXT "
                "PRIMARY KEY, day TEXT, reserved_micros INTEGER)")
            ledger.commit()
        plane = self.bare_plane(budget_db=old)
        self.assertIsNone(plane.overview()["budgets"]["pools"])

    def test_the_four_pools_carry_their_caps_and_what_is_left(self) -> None:
        pools = self.plane.overview()["budgets"]["pools"]
        self.assertIsNotNone(pools)
        names = [row["pool"] for row in pools["pools"]]
        self.assertEqual(names, ["coverage", "event_response", "adhoc", "maintenance"])
        for row in pools["pools"]:
            self.assertTrue(row["label"])
            self.assertGreaterEqual(row["remaining_usd"], 0)
        # A split nobody chose is not the owner's split, and the page says so.
        self.assertTrue(pools["caps_defaulted"])
        self.assertIn("默认分法", pools["caps_note"])

    def test_without_a_tick_ledger_the_idle_question_is_not_answered_falsely(self) -> None:
        # Q2 found this unanswerable; "0 idle" and "no ledger" are opposite
        # answers that look identical.
        self.assertIsNone(self.plane.overview()["activity"]["ticks"])

    def test_the_idle_share_and_the_stalled_lanes_are_read_from_the_ledger(self) -> None:
        from datetime import timedelta

        from dalton_core.tick_ledger import TickLedger, default_path

        started = self.c.h.h.clock()
        with TickLedger(default_path(self.root), clock=self.c.h.h.clock) as ledger:
            # A tick is idle only when every lane had nothing to do.
            ledger.append_tick(
                {"mission_tracking": {"status": "idle"},
                 "event_judgement": {"status": "idle"}},
                started_at=started)
            ledger.append_tick(
                {"mission_tracking": {"status": "launched"},
                 "event_judgement": {"status": "unconfigured"}},
                started_at=started + timedelta(minutes=5))
        ticks = self.plane.overview()["activity"]["ticks"]
        self.assertTrue(ticks["available"])
        self.assertEqual(ticks["ticks"], 2)
        self.assertEqual(ticks["idle_ticks"], 1)
        self.assertEqual(ticks["idle_ratio"], 0.5)
        # A lane that wanted to run and could not is named in the owner's
        # words, not by its driver key.
        stalled = {row["lane"]: row for row in ticks["stalled_lanes"]}
        self.assertEqual(stalled["event_judgement"]["lane_label"], "判断新发生的事要不要动")
        self.assertEqual(stalled["event_judgement"]["stalls"], 1)


class ResearchTaskTests(Int2Case):
    def test_a_core_with_no_loops_shows_no_special_research(self) -> None:
        self.assertIsNone(self.card()["research_tasks"])

    def test_a_task_carries_its_question_its_budget_and_a_gap(self) -> None:
        # A task with nothing to show yet must say what is missing: an
        # unfinished task with a blank result reads exactly like one that
        # looked everywhere and found nothing.
        loop = self.admit_inquiry()
        row = self.card()["research_tasks"][0]
        self.assertEqual(row["task_ref"], loop["loop_ref"])
        self.assertEqual(row["question"], "折扣有多深？")
        self.assertEqual(row["budget_label"], "0/2 轮")
        self.assertEqual(row["state_label"], "已排队，还没开跑")
        self.assertEqual(row["gap"], "已排队，尚未开跑")
        self.assertIsNone(row["conclusion"])


if __name__ == "__main__":
    unittest.main()

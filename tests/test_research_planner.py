"""P13a/P13b: the system decides what to work on, instead of the calendar."""

from __future__ import annotations

import json
import unittest

from dalton_core.research_planner import (
    ACTIONS,
    MAX_DIRECTIVES,
    MAX_INQUIRIES,
    ResearchPlanError,
    build_prompt,
    build_work,
    directives_for,
    parse_response,
    plan_from_response,
    wanted_specs,
)
from dalton_core.research_state import build_research_state, company_state, state_digest

NOW = "2026-09-08T16:00:00.000000+00:00"
ACN = "company:sec-cik:0001467373"
IBM = "company:sec-cik:0000051143"

MISSION = {
    "mission_ref": "coverage-mission:us-it-services",
    "id": "coverage-mission-version:us-it-services:12",
    "objective": "Cover US IT services",
    "research_questions": ["Is AI displacing headcount-based revenue?"],
    "deliverables": ["initial_screen"],
    "source_plan": [
        {"source_ref": "source:sec-edgar", "role": "filings", "status": "connected"},
        {"source_ref": "source:guidepoint", "role": "experts", "status": "not_connected"},
    ],
}


def item(item_ref, *, required=4, have=4, status="complete", **extra):
    base = {"item_ref": item_ref, "required": required, "have": have, "read": have,
            "pending": 0, "failed": 0, "status": status,
            "source_ref": "source:sec-edgar", "note": None}
    base.update(extra)
    return base


def entry(company_ref, ticker, *items, gaps=(), blocked_on=()):
    return {
        "company_ref": company_ref, "ticker": ticker, "priority": 0,
        "stage": "initial_screen", "stage_status": "entered",
        "items": list(items), "gaps": list(gaps), "blocked_on": list(blocked_on),
        "source_base_ready": not gaps,
    }


def state(**overrides):
    kwargs = {
        "mission": MISSION,
        "checklist": [
            entry(ACN, "ACN", item("quarterly_financials", have=1, status="partial"),
                  item("earnings_calls"), gaps=["quarterly_financials"]),
            entry(IBM, "IBM", item("quarterly_financials"),
                  item("broker_research", have=0, status="source_unavailable",
                       note="来源没接上"), blocked_on=["broker_research"]),
        ],
        "figures_by_company": {ACN: {"total": 1, "by_grade": {"company-filed-document": 1}}},
        "metrics_by_company": {ACN: [{"metric_ref": "metric:new-bookings",
                                      "label": "new bookings", "citation_count": 1}]},
        "budget": {"max_alphaengine_calls_24h": 130},
        "spend": {"alphaengine": {"spent": 130, "cap": 130}},
        "as_of": NOW,
    }
    kwargs.update(overrides)
    return build_research_state(**kwargs)


def response(*directives, assessment="ACN needs one more quarter.", inquiries=()):
    return json.dumps({"schema_version": "0.1", "assessment": assessment,
                       "directives": list(directives), "inquiries": list(inquiries)})


def inquiry(**overrides):
    base = {"company_ref": ACN,
            "question": "Does utilisation contradict the headcount commentary?",
            "wants": "the next two earnings calls",
            "because": "new bookings is cited by the market and nobody has collected it"}
    base.update(overrides)
    return base


def directive(**overrides):
    base = {"company_ref": ACN, "item_ref": "quarterly_financials",
            "action": "acquire", "reason": "three quarters short of a screen"}
    base.update(overrides)
    return base


class StateTests(unittest.TestCase):
    def test_the_state_says_what_is_missing_not_only_what_is_held(self):
        [acn, ibm] = state()["companies"]
        gap = next(i for i in acn["items"] if i["item_ref"] == "quarterly_financials")
        self.assertEqual((gap["required"], gap["have"], gap["deficit"]), (4, 1, 3))
        self.assertEqual(acn["gaps"], ["quarterly_financials"])
        self.assertEqual(ibm["blocked_on"], ["broker_research"])

    def test_a_blocked_item_carries_the_reason_it_is_blocked(self):
        # A planner that cannot tell "not done" from "cannot be done" will keep
        # ordering work against a source nobody has connected.
        ibm = state()["companies"][1]
        blocked = next(i for i in ibm["items"] if i["item_ref"] == "broker_research")
        self.assertEqual(blocked["status"], "source_unavailable")
        self.assertEqual(blocked["note"], "来源没接上")

    def test_spend_and_budget_both_travel_with_the_state(self):
        # A plan made without them asks for the most expensive thing and finds
        # it refused.
        built = state()
        self.assertEqual(built["budget"]["max_alphaengine_calls_24h"], 130)
        self.assertEqual(built["spend"]["alphaengine"]["spent"], 130)

    def test_figures_and_watched_metrics_reach_the_planner(self):
        acn = state()["companies"][0]
        self.assertEqual(acn["figures"]["total"], 1)
        self.assertEqual(acn["metrics_watched"][0]["metric_ref"], "metric:new-bookings")

    def test_the_state_is_bounded_rather_than_growing_with_the_corpus(self):
        many = [{"metric_ref": f"metric:m{i}", "label": str(i), "citation_count": 1}
                for i in range(50)]
        built = state(metrics_by_company={ACN: many})
        self.assertLessEqual(len(built["companies"][0]["metrics_watched"]), 5)

    def test_the_state_hashes_so_a_plan_can_bind_to_it(self):
        first, second = state(), state()
        self.assertEqual(first["content_hash"], second["content_hash"])
        moved = state(figures_by_company={ACN: {"total": 9, "by_grade": {}}})
        self.assertNotEqual(first["content_hash"], moved["content_hash"])

    def test_a_person_can_read_the_digest(self):
        digest = state_digest(state())
        self.assertIn("ACN", digest)
        self.assertIn("quarterly_financials", digest)

    def test_a_company_with_nothing_yet_still_projects(self):
        built = company_state(entry("company:x", "X"))
        self.assertEqual((built["items"], built["figures"]["total"]), ([], 0))


class PromptTests(unittest.TestCase):
    def test_it_tells_the_model_the_rules_it_will_be_held_to(self):
        prompt = build_prompt(state())
        self.assertIn("RESEARCH_STATE", prompt)
        self.assertIn("Inventing either voids the whole plan", prompt)
        self.assertIn("blocked", prompt)
        self.assertIn("Spend is real", prompt)
        # The state itself has to be in the prompt or the plan is a guess.
        self.assertIn("quarterly_financials", prompt)


class PlanTests(unittest.TestCase):
    def test_a_well_formed_plan_is_ranked_and_bound_to_its_state(self):
        built = state()
        plan = plan_from_response(built, response(directive()), created_at=NOW)
        self.assertEqual(plan["state_hash"], built["content_hash"])
        self.assertEqual(plan["directives"][0]["rank"], 0)
        self.assertEqual(plan["directives"][0]["action"], "acquire")
        self.assertEqual(plan["mission_version_ref"], MISSION["id"])

    def test_a_plan_naming_a_company_nobody_covers_is_refused_whole(self):
        # Filtering would leave a ranking that no longer means what the model
        # meant; a model that invented work should be asked again.
        with self.assertRaises(ResearchPlanError) as caught:
            plan_from_response(
                state(), response(directive(), directive(company_ref="company:sec-cik:9")),
                created_at=NOW)
        self.assertIn("neither a company under coverage nor the industry", str(caught.exception))

    def test_a_plan_naming_an_item_that_company_does_not_have_is_refused(self):
        with self.assertRaises(ResearchPlanError):
            plan_from_response(state(), response(directive(item_ref="invented_item")),
                               created_at=NOW)

    def test_an_action_nobody_can_take_is_refused(self):
        with self.assertRaises(ResearchPlanError):
            plan_from_response(state(), response(directive(action="think_harder")),
                               created_at=NOW)

    def test_a_directive_with_no_reason_is_refused(self):
        # A plan that cannot be argued with is the calendar it replaces.
        with self.assertRaises(ResearchPlanError):
            plan_from_response(state(), response(directive(reason="  ")), created_at=NOW)

    def test_a_plan_must_say_where_the_research_stands(self):
        with self.assertRaises(ResearchPlanError):
            plan_from_response(state(), response(directive(), assessment="  "),
                               created_at=NOW)

    def test_an_empty_plan_is_valid(self):
        plan = plan_from_response(state(), response(), created_at=NOW)
        self.assertEqual(plan["directives"], [])
        self.assertTrue(plan["assessment"])

    def test_a_malformed_answer_is_refused_not_partially_read(self):
        for bad in ("nope", json.dumps({"directives": []}),
                    json.dumps({"schema_version": "0.9", "assessment": "x", "directives": []}),
                    json.dumps({"schema_version": "0.1", "assessment": "x", "directives": {}})):
            with self.assertRaises(ResearchPlanError):
                parse_response(bad)

    def test_a_plan_cannot_be_unboundedly_long(self):
        with self.assertRaises(ResearchPlanError):
            parse_response(response(*[directive() for _ in range(MAX_DIRECTIVES + 1)]))

    def test_stop_is_an_action_so_finished_work_can_be_ended(self):
        # The whole point: the loop kept searching satisfied items forever.
        self.assertIn("stop", ACTIONS)
        plan = plan_from_response(
            state(), response(directive(item_ref="earnings_calls", action="stop",
                                        reason="four of four held; more adds nothing")),
            created_at=NOW)
        self.assertEqual(len(directives_for(plan, action="stop")), 1)
        self.assertEqual(directives_for(plan, action="search"), [])


class DispatchTests(unittest.TestCase):
    def test_only_the_specs_the_plan_asked_for_are_wanted(self):
        # This is what turns a plan into a cadence: a spec nobody asked for is
        # not searched, however long it has been.
        plan = plan_from_response(
            state(), response(directive(item_ref="earnings_calls", action="search")),
            created_at=NOW)
        wanted = wanted_specs(plan, item_specs={
            "earnings_calls": ["earnings-call-transcripts"],
            "broker_research": ["sell-side-reports"],
        })
        self.assertEqual(wanted, {f"{ACN}|earnings-call-transcripts"})

    def test_a_stopped_item_is_never_wanted(self):
        plan = plan_from_response(
            state(), response(directive(item_ref="earnings_calls", action="stop")),
            created_at=NOW)
        self.assertEqual(
            wanted_specs(plan, item_specs={"earnings_calls": ["earnings-call-transcripts"]}),
            set())

    def test_an_unknown_action_cannot_be_asked_for(self):
        plan = plan_from_response(state(), response(directive()), created_at=NOW)
        with self.assertRaises(ResearchPlanError):
            directives_for(plan, action="invented")


class WorkOrderTests(unittest.TestCase):
    def test_the_same_state_replays_as_the_same_call(self):
        built = state()
        first = build_work(built, created_at=NOW, state_ref="research-state:1")
        self.assertEqual(first.id, build_work(built, created_at=NOW,
                                              state_ref="research-state:1").id)

    def test_a_moved_state_is_a_different_call(self):
        one = build_work(state(), created_at=NOW, state_ref="research-state:1")
        two = build_work(state(figures_by_company={ACN: {"total": 9, "by_grade": {}}}),
                         created_at=NOW, state_ref="research-state:1")
        self.assertNotEqual(one.id, two.id)

    def test_it_is_a_proposal_and_writes_nothing(self):
        work = build_work(state(), created_at=NOW, state_ref="research-state:1")
        self.assertTrue(work.metadata["candidate_only"])
        self.assertEqual(work.declared_side_effects, ())

    def test_it_is_budgeted_for_judgement_not_for_volume(self):
        # This runs a few times a day over a small object; extraction runs
        # thousands of times over large ones. The expensive model belongs here.
        from dalton_core.document_numeric_extraction import build_work as extraction_work

        plan = build_work(state(), created_at=NOW, state_ref="research-state:1")
        context = {"company_ref": ACN, "document_ref": "d", "content_hash": "0" * 64,
                   "created_at": NOW, "id": "extraction-context:1",
                   "source_manifest_ref": "m", "offset": 0, "end": 200,
                   "quotes": [{"quote_id": "q", "raw_text": "Revenue was 1."}]}
        slots = [{"metric_ref": "metric:revenue", "label": "revenue",
                  "unit": "currency", "prompt": "revenue"}]
        self.assertGreater(plan.budget["max_cost_usd"],
                           extraction_work(context, slots).budget["max_cost_usd"])


class InquiryTests(unittest.TestCase):
    """The part the codified checklist cannot anticipate."""

    def test_the_planner_may_raise_work_no_checklist_item_covers(self):
        plan = plan_from_response(state(), response(directive(), inquiries=[inquiry()]),
                                  created_at=NOW)
        [raised] = plan["inquiries"]
        self.assertEqual(raised["company_ref"], ACN)
        self.assertIn("utilisation", raised["question"])
        self.assertTrue(raised["wants"])
        self.assertTrue(raised["because"])

    def test_an_inquiry_may_be_industry_wide(self):
        plan = plan_from_response(
            state(), response(inquiries=[inquiry(company_ref=None)]), created_at=NOW)
        self.assertIsNone(plan["inquiries"][0]["company_ref"])

    def test_an_inquiry_naming_a_company_nobody_covers_is_refused(self):
        with self.assertRaises(ResearchPlanError):
            plan_from_response(state(),
                               response(inquiries=[inquiry(company_ref="company:sec-cik:9")]),
                               created_at=NOW)

    def test_an_inquiry_must_say_what_would_answer_it_and_why(self):
        for field in ("question", "wants", "because"):
            with self.assertRaises(ResearchPlanError, msg=field):
                plan_from_response(state(), response(inquiries=[inquiry(**{field: "  "})]),
                                   created_at=NOW)

    def test_inquiries_cannot_be_unboundedly_many(self):
        with self.assertRaises(ResearchPlanError):
            parse_response(response(inquiries=[inquiry() for _ in range(MAX_INQUIRIES + 1)]))

    def test_no_inquiries_is_the_normal_answer(self):
        plan = plan_from_response(state(), response(directive()), created_at=NOW)
        self.assertEqual(plan["inquiries"], [])

    def test_an_inquiry_is_not_a_directive_and_dispatches_nothing(self):
        # Additive work never substitutes for the standard: an inquiry cannot
        # make the dispatcher search a spec, only a directive can.
        plan = plan_from_response(state(), response(inquiries=[inquiry()]), created_at=NOW)
        self.assertEqual(plan["directives"], [])
        self.assertEqual(
            wanted_specs(plan, item_specs={"earnings_calls": ["earnings-call-transcripts"]}),
            set())

    def test_the_prompt_says_the_standard_is_not_the_planner_to_change(self):
        prompt = build_prompt(state())
        self.assertIn("fixed standard", prompt)
        self.assertIn("you may not decide a lower count is enough", prompt)
        self.assertIn("inquiries", prompt)


INDUSTRY = "industry:us-it-services"


def industry_entry(*items, gaps=()):
    return {"industry_ref": INDUSTRY, "items": list(items), "gaps": list(gaps),
            "blocked_on": [], "source_base_ready": not gaps}


class IndustryTests(unittest.TestCase):
    """P13f: an industry screen rests on facts that belong to no company."""

    def state(self, **overrides):
        kwargs = {"industry": industry_entry(
            item("industry_demand", required=3, have=91, status="complete"),
            item("competitive_landscape", required=3, have=132, status="complete"))}
        kwargs.update(overrides)
        return state(**kwargs)

    def test_the_industry_is_a_subject_of_its_own(self):
        built = self.state()
        self.assertEqual(built["industry"]["industry_ref"], INDUSTRY)
        self.assertEqual(len(built["industry"]["items"]), 2)

    def test_a_directive_may_tell_the_industry_to_stop(self):
        # 91 documents against a requirement of 3, and 48 more queued: this is
        # the directive the calendar could never issue.
        plan = plan_from_response(
            self.state(),
            response(directive(company_ref=INDUSTRY, item_ref="industry_demand",
                               action="stop",
                               reason="91 held against 3 required; more adds nothing")),
            created_at=NOW)
        [stop] = directives_for(plan, action="stop")
        self.assertEqual(stop["company_ref"], INDUSTRY)

    def test_an_item_the_industry_does_not_have_is_still_refused(self):
        with self.assertRaises(ResearchPlanError):
            plan_from_response(
                self.state(),
                response(directive(company_ref=INDUSTRY, item_ref="quarterly_financials")),
                created_at=NOW)

    def test_a_company_item_is_not_addressable_on_the_industry_and_vice_versa(self):
        with self.assertRaises(ResearchPlanError):
            plan_from_response(
                self.state(), response(directive(item_ref="industry_demand")),
                created_at=NOW)

    def test_industry_gaps_count_toward_the_open_total(self):
        built = self.state(industry=industry_entry(
            item("industry_demand", required=3, have=0, status="missing"),
            gaps=["industry_demand"]))
        self.assertIn("industry_demand", built["industry"]["gaps"])
        self.assertGreater(built["totals"]["open_gaps"], 1)

    def test_a_mission_with_no_industry_block_still_builds(self):
        built = state()
        self.assertIsNone(built["industry"])

    def test_the_digest_names_the_industry_first(self):
        self.assertTrue(state_digest(self.state()).startswith(INDUSTRY))


class StateHashTests(unittest.TestCase):
    """The hash is what makes an expensive planner affordable on a tick."""

    def test_reading_the_same_world_twice_is_the_same_state(self):
        # as_of is when the state was read, not anything about the world.
        # Hashing it made every read a different state, which would have paid
        # for a fresh plan on every tick while nothing had changed.
        early = state(as_of="2026-09-08T00:00:00+00:00")
        later = state(as_of="2026-09-08T23:59:00+00:00")
        self.assertNotEqual(early["as_of"], later["as_of"])
        self.assertEqual(early["content_hash"], later["content_hash"])

    def test_a_world_that_moved_is_a_different_state(self):
        moved = state(figures_by_company={ACN: {"total": 42, "by_grade": {}}})
        self.assertNotEqual(state()["content_hash"], moved["content_hash"])

    def test_the_read_time_is_still_reported(self):
        # Excluded from the hash, not from the object: a reader still needs to
        # know how stale the picture is.
        self.assertEqual(state(as_of="2026-09-08T12:00:00+00:00")["as_of"],
                         "2026-09-08T12:00:00+00:00")


class ToleranceTests(unittest.TestCase):
    """A fence around the object is presentation, not disagreement."""

    def body(self):
        return response(directive())

    def test_a_fenced_object_is_read(self):
        for wrapped in (f"```json\n{self.body()}\n```", f"```\n{self.body()}\n```",
                        f"Here is the plan:\n{self.body()}"):
            plan = plan_from_response(state(), wrapped, created_at=NOW)
            self.assertEqual(len(plan["directives"]), 1)

    def test_content_is_still_judged_strictly(self):
        # Tolerating the wrapper must not tolerate the contents.
        fenced = "```json\n" + response(directive(company_ref="company:sec-cik:9")) + "\n```"
        with self.assertRaises(ResearchPlanError):
            plan_from_response(state(), fenced, created_at=NOW)

    def test_something_that_is_not_a_plan_is_still_refused(self):
        for bad in ("I could not do this.", "```json\nnot json\n```", ""):
            with self.assertRaises(ResearchPlanError):
                parse_response(bad)


if __name__ == "__main__":
    unittest.main()

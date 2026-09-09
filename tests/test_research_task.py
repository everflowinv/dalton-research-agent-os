"""P14e: an inquiry becomes a bounded loop, once, inside a named budget pool.

The rules under test are the owner's boundary rather than an implementation
detail, so each one has its own case: the grant (two versioned owner acts), the
hash (an inquiry is dispatched once, ever), the pool (25% of the mission day,
and a refusal rather than a borrow when it is spent), the template subset (a
loop may only bind an ad-hoc template an executor can actually run) and the
terminal gate.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core import research_task as rt
from dalton_core.bounded_planner_loop import (
    BoundedPlannerAuthority,
    BoundedPlannerControlPlane,
    BoundedPlannerValidationError,
)
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.observability import ObservabilityStore
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore, content_hash
from tests.p9a_fixtures import INDUSTRY, OWNER, bootstrap_method_authorities, mission_params

ACN = "company:sec-cik:0001467373"
EPAM = "company:sec-cik:0001352010"
OUTSIDE = "company:sec-cik:0000320193"
DAY = "2026-09-09"


def inquiry(
    *, question: str, company_ref: str | None = ACN, wants: str = "Filed exhibits.",
    rank: int = 0,
) -> dict:
    return {
        "rank": rank, "company_ref": company_ref, "question": question,
        "wants": wants, "because": "the state prompted it",
    }


class ResearchTaskFixture(unittest.TestCase):
    """One Core holding a mission that grants the word and one live template."""

    grants_word = True
    publishes = ("probe-template:adhoc-sec-filings-index:v1",)
    scope_refs = (INDUSTRY, ACN)
    # The mission manifest's own $5 day makes a $1.25 pool, which is one task.
    # The pool cases want exactly that; the cases about admission itself want
    # room to admit more than one and are explicit about buying it.
    daily_cost_usd: float | None = None

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_dir = Path(self.temp.name)
        self.store = DaltonStore(str(self.state_dir / "core.sqlite"))
        self.addCleanup(self.store.close)
        state = bootstrap_method_authorities(self.store)
        mandate = state["agenda"].create_mandate(
            "mandate:us-it-services-constitution-p8a", actor_ref=OWNER,
            objective="Establish US IT Services coverage.",
            scope_refs=list(self.scope_refs), constraints={}, success_criteria={},
            effective_from="2026-08-23T00:00:00+00:00", effective_until=None,
            version_id="mandate-version:us-it-services-constitution-p8a:2",
            idempotency_key="p14e:mandate:2",
        )
        self.missions = CoverageMissionAuthority(self.store)
        params = mission_params(state)
        params["bindings"]["mandate_version"] = {
            "ref": mandate["id"], "hash": mandate["content_hash"],
        }
        may_write = set(params["autonomy"]["may_write"])
        if self.grants_word:
            may_write.add(rt.GRANT_WORD)
        else:
            may_write.discard(rt.GRANT_WORD)
        params["autonomy"]["may_write"] = sorted(may_write)
        if self.daily_cost_usd is not None:
            params["budget"]["max_daily_cost_usd"] = self.daily_cost_usd
        self.mission = self.missions.create_mission(params.pop("mission_ref"), **params)
        self.authority = BoundedPlannerAuthority(self.store)
        self.backlog = ResearchQuestionBacklog(self.store)
        self.templates = {}
        for spec in rt.ADHOC_PROBE_TEMPLATES:
            if spec["template_ref"] not in self.publishes:
                continue
            published = self.authority.publish_probe_template(
                spec["template_ref"], actor_ref="human:p14e-test-owner",
                **rt.publication_arguments(spec),
            )
            self.templates[spec["template_ref"]] = published

    def record_plan(self, inquiries: list[dict], *, state_hash: str | None = None) -> dict:
        plan = {
            "state_hash": state_hash or ("a" * 64),
            "assessment": "where the research stands",
            "mission_version_ref": self.mission["id"],
            "directives": [], "inquiries": inquiries, "sufficiency": [],
        }
        plan["content_hash"] = content_hash(plan)
        stored = self.missions.record_research_plan(
            plan, decided_by=self.mission["autonomy"]["automation_principal"],
        )
        return self.missions.latest_research_plan(self.mission["id"]) | {
            "plan_id": stored["plan_id"],
        }

    def admissions(self, plan: dict, *, day: str = DAY) -> list[dict]:
        return rt.plan_admissions(
            self.authority, mission=self.mission, plan=plan, day=day,
        )

    def admit(self, plan: dict, entry: dict, inquiry_wire: dict) -> dict:
        return rt.admit_inquiry(
            self.authority, self.backlog, mission=self.mission,
            plan_ref=plan["plan_id"], inquiry=inquiry_wire, entry=entry,
        )


class AdmissionTests(ResearchTaskFixture):
    daily_cost_usd = 20.0

    def test_a_plan_of_three_admits_one_and_says_why_for_the_other_two(self) -> None:
        first = inquiry(question="Do ACN's three adjusted margin definitions reconcile?")
        plan = self.record_plan([
            first,
            # Same content, re-ranked: the planner's ordering is not part of
            # the question, so this buys no second task.
            {**first, "rank": 1, "because": "restated differently"},
            inquiry(question="What is Apple's services margin?", company_ref=OUTSIDE, rank=2),
        ])
        entries = self.admissions(plan)
        self.assertEqual(
            [(entry["admissible"], entry["reason"]) for entry in entries],
            [(True, None), (False, "already_admitted"), (False, "out_of_universe")],
        )
        self.assertEqual(entries[0]["inquiry_hash"], entries[1]["inquiry_hash"])

    def test_a_company_in_the_universe_but_outside_the_mandate_is_named(self) -> None:
        plan = self.record_plan([inquiry(
            question="Which EPAM margin definition is which?", company_ref=EPAM,
        )])
        self.assertEqual(self.admissions(plan)[0]["reason"], "out_of_mandate_scope")

    def test_an_admitted_inquiry_is_never_admitted_again(self) -> None:
        wire = inquiry(question="Do ACN's margin definitions reconcile?")
        plan = self.record_plan([wire])
        record = self.admit(plan, self.admissions(plan)[0], wire)
        self.assertEqual(record["status"], "fresh")
        # A later plan, a different state, the same question.
        again = self.record_plan([{**wire, "rank": 0}], state_hash="b" * 64)
        self.assertEqual(self.admissions(again)[0]["reason"], "already_admitted")

    def test_changing_the_question_makes_a_new_task(self) -> None:
        wire = inquiry(question="Do ACN's margin definitions reconcile?")
        plan = self.record_plan([wire])
        self.admit(plan, self.admissions(plan)[0], wire)
        reissued = inquiry(question="Do ACN's margin definitions reconcile after FY26 Q3?")
        later = self.record_plan([reissued], state_hash="c" * 64)
        entry = self.admissions(later)[0]
        self.assertTrue(entry["admissible"])
        self.assertEqual(
            self.admit(later, entry, reissued)["status"], "fresh",
        )

    def test_the_loop_is_the_record_and_carries_the_inquiry_hash(self) -> None:
        wire = inquiry(question="Do ACN's margin definitions reconcile?")
        plan = self.record_plan([wire])
        record = self.admit(plan, self.admissions(plan)[0], wire)
        loop = self.authority.loop(record["loop_version_ref"])
        self.assertEqual(loop["admission"], {
            "source": "inquiry",
            "content_hash": record["inquiry_hash"],
            "inquiry_ref": record["inquiry_ref"],
            "plan_ref": plan["plan_id"],
        })
        self.assertEqual(loop["actor_ref"], "automation:coverage-mission")
        # And the driver will pick it up, because it is simply an active loop.
        self.assertIn(
            record["loop_version_ref"],
            [item["id"] for item in self.authority.active_loops()],
        )


class PoolTests(ResearchTaskFixture):
    def test_the_pool_is_a_quarter_of_the_mission_day(self) -> None:
        wire = rt.pool(self.mission)
        self.assertEqual(wire["name"], "adhoc")
        self.assertEqual(
            wire["cap_micros"],
            int(self.mission["budget"]["max_daily_cost_usd"] * 1_000_000 / 4),
        )

    def test_a_second_task_past_the_pool_is_refused_not_borrowed(self) -> None:
        first = inquiry(question="Do ACN's margin definitions reconcile?")
        second = inquiry(question="What drove ACN's bookings mix in FY26?", rank=1)
        plan = self.record_plan([first, second])
        entries = self.admissions(plan)
        # $5 mission day, $1.25 pool, $0.50 a round, two rounds a task.
        self.assertTrue(entries[0]["admissible"])
        self.assertEqual(entries[1]["reason"], "pool_exhausted")
        self.assertEqual(entries[1]["pool"]["cap_micros"], 1_250_000)

    def test_yesterdays_tasks_do_not_spend_todays_pool(self) -> None:
        wire = inquiry(question="Do ACN's margin definitions reconcile?")
        plan = self.record_plan([wire])
        self.admit(plan, self.admissions(plan)[0], wire)
        today = rt.pool_state(
            self.authority, self.mission,
            day=datetime.now(timezone.utc).date().isoformat(),
        )
        self.assertEqual(today["reserved_micros"], 1_000_000)
        self.assertEqual(
            rt.pool_state(self.authority, self.mission, day="2020-01-01")[
                "reserved_micros"], 0,
        )


class TemplateSubsetTests(ResearchTaskFixture):
    publishes = tuple(rt.ADHOC_TEMPLATE_REFS)

    def test_only_templates_an_executor_can_run_are_bindable(self) -> None:
        admitted = rt.admitted_adhoc_templates(self.authority)
        self.assertEqual(set(admitted), set(rt.ADHOC_TEMPLATE_REFS))
        bindable = rt.bindable_templates(self.authority)
        # Published is not the same as runnable: the driver executes SEC
        # company facts and AlphaEngine document reads, and admitting a loop
        # bound to anything else would raise out of the controller tick.
        self.assertEqual(
            set(bindable), {"probe-template:adhoc-sec-filings-index:v1"},
        )
        for template in bindable.values():
            self.assertIn(
                (template["operation"], template["permission_scope"]),
                rt.executable_probe_contracts(),
            )

    def test_a_task_binds_only_the_adhoc_catalogue(self) -> None:
        # A template outside the ad-hoc catalogue, published and executable.
        self.authority.publish_probe_template(
            "probe-template:sec-company-facts-revenue-growth:v1",
            capability_ref="capability:sec-read-only", operation="get_company_facts",
            runtime_profile_ref="runtime:sec-read-only:0.1",
            parameter_contract={
                "allowed_fields": ["source_ref", "locator", "query_terms"],
                "required_fields": ["source_ref", "locator", "query_terms"],
                "constants": {"source_ref": "source:sec-edgar"},
            },
            output_contract_ref="schema:bounded-planner-probe-output:0.1",
            verifier_ref="verifier:source-level-coverage:0.1",
            permission_scope="public_sec_read",
            declared_side_effects=["read:public-http"],
            cost={"cost_units": 1, "max_attempts": 2, "max_seconds": 120},
            actor_ref="human:p14e-test-owner",
        )
        wire = inquiry(question="Do ACN's margin definitions reconcile?")
        plan = self.record_plan([wire])
        entry = self.admissions(plan)[0]
        self.assertEqual(entry["template_refs"], ["probe-template:adhoc-sec-filings-index:v1"])

    def test_an_industry_wide_inquiry_has_nothing_to_look_up(self) -> None:
        plan = self.record_plan([inquiry(
            question="Has US IT services demand bottomed?", company_ref=None,
        )])
        self.assertEqual(self.admissions(plan)[0]["reason"], "no_bindable_template")

    def test_the_deploy_manifest_says_what_the_adapter_publishes(self) -> None:
        manifest = json.loads(
            (Path(__file__).resolve().parents[1]
             / "deploy/phase8/p14e-adhoc-probe-templates-v1.json"
             ).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["status"], "proposed")
        self.assertEqual(manifest["templates"], rt.deploy_manifest()["templates"])
        for template in manifest["templates"]:
            self.assertTrue(template["allowed_hosts"])
            self.assertTrue(template["cost"]["max_attempts"] >= 1)


class ExecutorContractTests(ResearchTaskFixture):
    """A template is bindable only if the executor would accept its WorkOrder."""

    def test_a_mistyped_permission_scope_is_not_bindable(self) -> None:
        template = self.templates["probe-template:adhoc-sec-filings-index:v1"]
        spec = rt.ADHOC_PROBE_TEMPLATES[0]
        arguments = rt.publication_arguments(spec)
        # A republication with a hyphen where the executor wants an underscore.
        # It matches on operation and would be refused at execution.
        arguments["permission_scope"] = "public-sec-read"
        self.authority.publish_probe_template(
            spec["template_ref"], actor_ref="human:p14e-test-owner",
            prior_version_ref=template["id"], **arguments,
        )
        self.assertIn(
            spec["template_ref"], rt.admitted_adhoc_templates(self.authority),
        )
        self.assertEqual(rt.bindable_templates(self.authority), {})
        self.assertEqual(
            rt.grant(self.mission, rt.bindable_templates(self.authority))["reasons"],
            ["no_executable_adhoc_template_published"],
        )

    def test_the_contract_is_the_pair_both_executors_gate_on(self) -> None:
        from dalton_core.bounded_alphaengine_probe import (
            PROBE_OPERATION as AE_OPERATION,
            PROBE_PERMISSION_SCOPE as AE_SCOPE,
        )
        from dalton_core.bounded_probe_executor import (
            PROBE_OPERATION as SEC_OPERATION,
            PROBE_PERMISSION_SCOPE as SEC_SCOPE,
        )

        self.assertEqual(
            rt.executable_probe_contracts(),
            frozenset({(SEC_OPERATION, SEC_SCOPE), (AE_OPERATION, AE_SCOPE)}),
        )


class RetirementTests(ResearchTaskFixture):
    def test_this_deployment_may_withdraw_a_published_template(self) -> None:
        retired = ["probe-template:adhoc-sec-filings-index:v1"]
        self.assertEqual(rt.bindable_templates(self.authority, retired=retired), {})
        self.assertEqual(
            rt.read_grant(self.store, retired=retired)["reasons"],
            ["no_executable_adhoc_template_published"],
        )
        # And the catalogue may withdraw one for everybody.
        from unittest.mock import patch

        withdrawn = tuple(
            {**spec, "status": rt.RETIRED_STATUS} if index == 0 else spec
            for index, spec in enumerate(rt.ADHOC_PROBE_TEMPLATES)
        )
        with patch.object(rt, "ADHOC_PROBE_TEMPLATES", withdrawn):
            self.assertEqual(rt.bindable_templates(self.authority), {})

    def test_a_retired_template_stops_the_lane_before_it_spawns(self) -> None:
        from dalton_core.mission_research_task_lane import (
            ResearchTaskCoordinator,
            lane_configuration,
        )

        config = self.state_dir / "research-task-lane.json"
        config.write_text(json.dumps({
            "max_admissions_per_tick": 2,
            "retired_templates": ["probe-template:adhoc-sec-filings-index:v1"],
        }), encoding="utf-8")
        settings = lane_configuration(config)
        self.assertEqual(settings["max_admissions_per_tick"], 2)

        class Launcher:
            tickets_dir = self.state_dir / "research-tasks"
            retired_templates = settings["retired_templates"]
            started: list = []

            def start(self, **_kwargs):
                self.started.append(_kwargs)
                raise AssertionError("a retired catalogue must not spawn a child")

        self.record_plan([inquiry(question="Do ACN's margins reconcile?")])
        Launcher.tickets_dir.mkdir(parents=True, exist_ok=True)
        result = ResearchTaskCoordinator(
            store=self.store, launcher=Launcher(),
        ).dispatch_once()
        self.assertEqual(result["status"], "not_granted")


class IdentityNormalisationTests(ResearchTaskFixture):
    def test_a_rewrapped_question_is_the_same_question(self) -> None:
        wrapped = inquiry(question="Do ACN's three adjusted\n  margin definitions\treconcile?")
        flat = inquiry(question="Do ACN's three adjusted margin definitions reconcile?")
        self.assertEqual(
            rt.inquiry_content_hash(wrapped), rt.inquiry_content_hash(flat),
        )
        plan = self.record_plan([wrapped, {**flat, "rank": 1}])
        entries = self.admissions(plan)
        self.assertEqual(
            [entry["reason"] for entry in entries], [None, "already_admitted"],
        )

    def test_a_different_question_is_still_a_different_hash(self) -> None:
        self.assertNotEqual(
            rt.inquiry_content_hash(inquiry(question="Do ACN's margins reconcile?")),
            rt.inquiry_content_hash(inquiry(question="Do ACN's margins reconcile now?")),
        )


class RevisionTests(ResearchTaskFixture):
    daily_cost_usd = 20.0

    def test_an_admitted_task_can_be_revised_without_becoming_a_new_one(self) -> None:
        wire = inquiry(question="Do ACN's margins reconcile?")
        plan = self.record_plan([wire])
        entry = self.admissions(plan)[0]
        first = self.admit(plan, entry, wire)
        head = self.authority.loop(first["loop_version_ref"])
        second = self.authority.create_loop(
            head["loop_ref"],
            question_version_ref=head["question_version_ref"],
            template_bindings=entry["bindings"],
            required_coverage_items=[b["coverage_item_ref"] for b in entry["bindings"]],
            budget={**head["budget"], "max_rounds": head["budget"]["max_rounds"] + 1},
            actor_ref="automation:coverage-mission",
            admission=head["admission"],
            prior_version_ref=head["id"],
        )
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["admission"], head["admission"])
        # The head is now v2, so the next plain admission still refuses -- and
        # refuses against the version a caller would have to continue.
        again = self.authority.loop_for_admission(head["admission"]["content_hash"])
        self.assertEqual(again["id"], second["id"])
        self.assertEqual(self.admissions(plan)[0]["reason"], "already_admitted")


class DeferralTests(ResearchTaskFixture):
    daily_cost_usd = 20.0

    def test_only_what_this_pass_admits_spends_the_day(self) -> None:
        plan = self.record_plan([
            inquiry(question="Do ACN's margins reconcile?"),
            inquiry(question="What drove ACN's bookings mix?", rank=1),
        ])
        entries = self.admissions(plan)
        self.assertEqual([entry["admissible"] for entry in entries], [True, True])
        limited = rt.plan_admissions(
            self.authority, mission=self.mission, plan=plan, day=DAY, limit=1,
        )
        self.assertEqual(
            [(entry["admissible"], entry["reason"]) for entry in limited],
            [(True, None), (False, "deferred_to_a_later_tick")],
        )

    def test_entries_name_their_own_inquiry(self) -> None:
        plan = self.record_plan([
            inquiry(question="What is Apple's services margin?", company_ref=OUTSIDE),
            inquiry(question="Do ACN's margins reconcile?", rank=1),
        ])
        entries = self.admissions(plan)
        self.assertEqual([entry["ordinal"] for entry in entries], [0, 1])
        # The admissible one is second; indexing by position would admit the
        # refused Apple inquiry's text under the Accenture entry.
        admissible = [entry for entry in entries if entry["admissible"]]
        self.assertEqual(len(admissible), 1)
        record = self.admit(
            plan, admissible[0], plan["inquiries"][admissible[0]["ordinal"]],
        )
        view = rt.research_task_view(self.store, day=DAY, mission=self.mission)
        self.assertEqual(
            view["companies"][0]["tasks"][0]["question"],
            "Do ACN's margins reconcile?",
        )
        self.assertEqual(record["subject_ref"], ACN)


class SwitchTests(ResearchTaskFixture):
    def test_granted_needs_both_owner_acts(self) -> None:
        decision = rt.grant(self.mission, rt.bindable_templates(self.authority))
        self.assertTrue(decision["granted"], decision)
        self.assertEqual(rt.read_grant(self.store)["granted"], True)
        self.assertEqual(rt.grant(self.mission, {})["reasons"],
                         ["no_executable_adhoc_template_published"])
        self.assertEqual(rt.grant(None, {})["reasons"],
                         ["no_active_mission", "no_executable_adhoc_template_published"])

    def test_the_cockpit_flag_is_no_when_nothing_says_yes(self) -> None:
        from dalton_core.agenda_control import AgendaControlPlane

        plane = AgendaControlPlane.__new__(AgendaControlPlane)
        plane.research_task_grant = None
        self.assertFalse(plane.adhoc_research_enabled())
        plane.research_task_grant = lambda: {"granted": True}
        self.assertTrue(plane.adhoc_research_enabled())
        plane.research_task_grant = lambda: {"granted": False, "reasons": ["x"]}
        self.assertFalse(plane.adhoc_research_enabled())

        def broken() -> dict:
            raise RuntimeError("core is unreachable")

        plane.research_task_grant = broken
        self.assertFalse(plane.adhoc_research_enabled())


class UngrantedMissionTests(ResearchTaskFixture):
    grants_word = False

    def test_a_mission_without_the_word_grants_nothing(self) -> None:
        decision = rt.grant(self.mission, rt.bindable_templates(self.authority))
        self.assertFalse(decision["granted"])
        self.assertEqual(decision["reasons"], ["mission_does_not_grant_research_task"])


class AdmissionSourceTests(ResearchTaskFixture):
    def test_a_human_loop_still_needs_a_human_and_hashes_as_before(self) -> None:
        recorded = self.backlog.record_question(
            mandate_version_ref=self.mission["bindings"]["mandate_version"]["ref"],
            company_ref=ACN, question="A question a person asked",
            answer_criteria="what would answer it",
            source_refs=["source:sec-edgar"], actor_ref="human:p14e-test-owner",
            idempotency_key="p14e:human:1",
        )
        template = self.templates["probe-template:adhoc-sec-filings-index:v1"]
        bindings = [{
            "coverage_item_ref": "coverage:human:acn",
            "template_version_ref": template["id"],
            "parameters": {
                "source_ref": "source:sec-edgar",
                "locator": "company-facts/CIK0001467373",
                "query_terms": ["Revenues", "10-Q"],
            },
        }]
        with self.assertRaises(BoundedPlannerValidationError):
            self.authority.create_loop(
                "bounded-loop:human:1",
                question_version_ref=recorded["question_version_ref"],
                template_bindings=bindings,
                required_coverage_items=["coverage:human:acn"],
                budget={"max_rounds": 2, "max_cost_units": 2, "max_seconds": 240},
                actor_ref="automation:coverage-mission",
            )
        loop = self.authority.create_loop(
            "bounded-loop:human:1",
            question_version_ref=recorded["question_version_ref"],
            template_bindings=bindings,
            required_coverage_items=["coverage:human:acn"],
            budget={"max_rounds": 2, "max_cost_units": 2, "max_seconds": 240},
            actor_ref="human:p14e-test-owner",
        )
        self.assertEqual(loop["status"], "fresh")
        self.assertNotIn("admission", loop)

    def test_a_second_loop_for_the_same_inquiry_is_refused_by_the_authority(self) -> None:
        wire = inquiry(question="Do ACN's margin definitions reconcile?")
        plan = self.record_plan([wire])
        entry = self.admissions(plan)[0]
        first = self.admit(plan, entry, wire)
        # Bypass the adapter's own check: the authority itself must refuse.
        again = rt.admit_inquiry(
            self.authority, self.backlog, mission=self.mission,
            plan_ref="mission-research-plan:another", inquiry=wire, entry=entry,
        )
        self.assertEqual(again["status"], "duplicate_admission")
        self.assertEqual(again["loop_version_ref"], first["loop_version_ref"])


class TerminalGateTests(ResearchTaskFixture):
    def setUp(self) -> None:
        super().setUp()
        self.observability = ObservabilityStore(self.store)
        self.scheduler = Scheduler(connection=self.store.connection)
        self.control = BoundedPlannerControlPlane(
            self.authority, self.observability, self.scheduler,
        )

    def test_a_task_the_owner_drops_reads_back_as_a_conclusion(self) -> None:
        wire = inquiry(question="Do ACN's margin definitions reconcile?")
        plan = self.record_plan([wire])
        record = self.admit(plan, self.admissions(plan)[0], wire)
        loop_ref = record["loop_version_ref"]
        self.authority.issue_directive(
            loop_ref, verbatim_text="drop this one", control_effect="deprioritize",
            target_coverage_item_ref=None, actor_ref="human:p14e-test-owner",
        )
        proposal = self.authority.submit_proposal(
            loop_ref, action={"kind": "terminate", "reason": "human_deprioritized"},
            rationale="the owner deprioritized it",
            actor_ref="automation:coverage-mission",
        )
        admitted = self.control.admit_proposal(proposal["id"])
        self.assertEqual(admitted["status"], "terminal")
        view = rt.research_task_view(self.store, day=DAY, mission=self.mission)
        task = view["companies"][0]["tasks"][0]
        self.assertEqual(task["state"], "terminal")
        self.assertEqual(task["terminal_state"], "human_deprioritized")
        self.assertEqual(task["conclusion"], "人已降级")
        self.assertEqual(task["citations"], [])
        self.assertEqual(task["gap"], "已排队，尚未开跑")

    def test_a_terminal_that_the_gate_does_not_support_is_refused(self) -> None:
        wire = inquiry(question="Do ACN's margin definitions reconcile?")
        plan = self.record_plan([wire])
        record = self.admit(plan, self.admissions(plan)[0], wire)
        proposal = self.authority.submit_proposal(
            record["loop_version_ref"],
            action={"kind": "terminate",
                    "reason": "coverage_complete_unobservable_candidate"},
            rationale="nothing was found",
            actor_ref="automation:coverage-mission",
        )
        decided = self.control.admit_proposal(proposal["id"])
        self.assertEqual(decided["status"], "rejected")
        self.assertEqual(
            decided["decision"]["reason"],
            "negative_terminal_requires_complete_no_match_coverage",
        )


class ChildTests(ResearchTaskFixture):
    def test_the_child_admits_one_and_reports_the_refusals(self) -> None:
        from dalton_core.research_task_cli import run_admissions

        first = inquiry(question="Do ACN's margin definitions reconcile?")
        self.record_plan([
            first,
            inquiry(question="What is Apple's services margin?",
                    company_ref=OUTSIDE, rank=1),
        ])
        self.store.close()
        summary = run_admissions(
            state_dir=self.state_dir, summary_dir=self.state_dir, max_admissions=1,
        )
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["admitted"], 1)
        self.assertEqual(summary["formal_authority_writes"], 0)
        self.assertEqual(
            [item["reason"] for item in summary["refused"]], ["out_of_universe"],
        )
        self.assertEqual(summary["pool"]["reserved_micros"], 1_000_000)
        written = json.loads((self.state_dir / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(written["tasks"][0]["loop_ref"], summary["tasks"][0]["loop_ref"])


class UngrantedChildTests(ResearchTaskFixture):
    publishes = ()

    def test_the_child_writes_nothing_when_the_owner_has_not_granted_it(self) -> None:
        from dalton_core.research_task_cli import run_admissions

        wire = inquiry(question="Do ACN's margin definitions reconcile?")
        self.record_plan([wire])
        self.store.close()
        summary = run_admissions(state_dir=self.state_dir, summary_dir=self.state_dir)
        self.assertEqual(summary["status"], "idle")
        self.assertEqual(summary["failure_reason"], "not_granted")
        self.assertEqual(summary["tasks"], [])
        self.assertEqual(
            summary["grant"]["reasons"], ["no_executable_adhoc_template_published"],
        )


if __name__ == "__main__":
    unittest.main()

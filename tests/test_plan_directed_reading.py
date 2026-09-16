"""WP-G: the planner's reading directives finally reach the lanes that read.

``research_planner.document_reading_priorities`` and ``gap_filling_inquiries``
were delivered ranked, exported and never consumed.  Live on 2026-09-16 the
newest plan carried ten directives -- five ``extract_figures``, three ``read``,
two ``stop`` -- against an extraction queue that took its work strictly from
``state='awaiting_human_extraction'`` and could not see any of them, and a
document-research lane that said "no unstarted document research admission"
while the plan named documents already acquired and read through.

So these are the two joins that were missing, and the rules they must keep:
the plan leads the order, and a plan that cannot be read changes nothing.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.document_extraction_cli import (
    _awaiting_reviews_in_reading_order,
    _plan_directed_ranks,
    _secondary_sweep,
    run_extraction,
)
from dalton_core.document_extraction_windows import (
    PLAN_UNDIRECTED_RANK,
    plan_rank_for,
    plan_reading_ranks,
)
from dalton_core.mission_document_research_lane import (
    MissionDocumentResearchCoordinator,
)
from dalton_core.research_planner import document_reading_priorities
from dalton_core.store import canonical_json, content_hash
from tests.p9a_fixtures import mission_params
from tests.test_document_extraction import ACN, ExtractionHarness, NEW_DOC, OWNER
from tests.test_transcript_polish_model_worker import policy, profile

CTSH = "company:sec-cik:0001058290"
ITEM_SPECS = {
    "earnings_calls": ("earnings-call-transcripts",),
    "annual_report": ("annual-report-10k",),
    "broker_research": ("sell-side-reports",),
    "industry_demand": ("industry-demand",),
}


def _directive(company_ref, item_ref, action, reason="fixture"):
    return {"company_ref": company_ref, "item_ref": item_ref,
            "action": action, "reason": reason}


class PlanReadingRankTests(unittest.TestCase):
    """The plan names a checklist item; the queue sorts by document kind."""

    def _ranks(self, *directives, industry_ref=None):
        plan = {"directives": list(directives)}
        return plan_reading_ranks(
            document_reading_priorities(plan), item_specs=ITEM_SPECS,
            industry_ref=industry_ref)

    def test_a_read_directive_names_the_kinds_its_item_is_satisfied_by(self):
        ranks = self._ranks(_directive(ACN, "earnings_calls", "read"))["ranks"]
        self.assertEqual(ranks, {(ACN, "earnings-call-transcripts"): 1})

    def test_search_and_stop_directives_name_nothing_to_read(self):
        # ``search`` belongs to source discovery and ``stop`` is a refusal;
        # neither may promote a document in the reading queue.
        resolved = self._ranks(
            _directive(ACN, "earnings_calls", "stop"),
            _directive(ACN, "annual_report", "search"),
        )
        self.assertEqual(resolved, {"ranks": {}, "numeric": {}})

    def test_the_plans_own_order_is_the_rank_that_is_carried(self):
        resolved = self._ranks(
            _directive(ACN, "annual_report", "extract_figures"),
            _directive(CTSH, "earnings_calls", "read"),
        )
        self.assertEqual(resolved["ranks"], {
            (ACN, "annual-report-10k"): 1,
            (CTSH, "earnings-call-transcripts"): 2,
        })

    def test_only_extract_figures_reaches_the_figures_pass(self):
        resolved = self._ranks(
            _directive(ACN, "earnings_calls", "read"),
            _directive(ACN, "annual_report", "extract_figures"),
        )
        self.assertEqual(sorted(resolved["ranks"]), [
            (ACN, "annual-report-10k"), (ACN, "earnings-call-transcripts")])
        self.assertEqual(resolved["numeric"], {(ACN, "annual-report-10k"): 2})

    def test_the_best_rank_wins_when_two_directives_name_one_kind(self):
        resolved = self._ranks(
            _directive(ACN, "earnings_calls", "read"),
            _directive(ACN, "earnings_calls", "extract_figures"),
        )
        self.assertEqual(resolved["ranks"], {(ACN, "earnings-call-transcripts"): 1})
        self.assertEqual(resolved["numeric"], {(ACN, "earnings-call-transcripts"): 2})

    def test_an_industry_directive_matches_whoever_the_document_is_filed_under(self):
        # An industry document is filed under whichever company's query
        # returned it, so matching the industry ref literally would match
        # nothing at all.
        resolved = self._ranks(
            _directive("industry:us-it-services", "industry_demand", "read"),
            industry_ref="industry:us-it-services")
        self.assertEqual(
            plan_rank_for(resolved["ranks"], company_ref=ACN,
                          spec_ref="industry-demand"), 1)

    def test_an_item_nothing_is_held_for_ranks_nothing(self):
        self.assertEqual(
            self._ranks(_directive(ACN, "quarterly_financials", "read"))["ranks"], {})

    def test_a_malformed_directive_is_stepped_over_not_raised_on(self):
        resolved = plan_reading_ranks(
            [None, 3, {"action": "read"},
             {"company_ref": ACN, "item_ref": "earnings_calls", "action": "read"},
             {"rank": True, "company_ref": ACN, "item_ref": "annual_report",
              "action": "read"},
             {"rank": 2, "company_ref": ACN, "item_ref": "annual_report",
              "action": "read"}],
            item_specs=ITEM_SPECS)
        self.assertEqual(resolved["ranks"], {(ACN, "annual-report-10k"): 2})

    def test_a_document_of_unknown_kind_is_never_promoted_by_a_guess(self):
        ranks = self._ranks(_directive(ACN, "earnings_calls", "read"))["ranks"]
        for spec in (None, "", "management-changes"):
            self.assertEqual(
                plan_rank_for(ranks, company_ref=ACN, spec_ref=spec),
                PLAN_UNDIRECTED_RANK)
        self.assertEqual(
            plan_rank_for({}, company_ref=ACN, spec_ref="earnings-call-transcripts"),
            PLAN_UNDIRECTED_RANK)


class _Reviews:
    """The two joins the queue reads, and nothing else."""

    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE coverage_mission_document_reviews(
                review_id TEXT PRIMARY KEY, mission_version_ref TEXT,
                company_ref TEXT, document_ref TEXT,
                discovered_document_ref TEXT, state TEXT, created_at TEXT);
            CREATE TABLE coverage_mission_discovered_documents(
                record_id TEXT PRIMARY KEY, discovery_ref TEXT);
            CREATE TABLE coverage_mission_source_discoveries(
                record_id TEXT PRIMARY KEY, spec_ref TEXT);
            """
        )

    def add(self, review_id, *, company_ref, spec_ref, created_at):
        self.connection.execute(
            "INSERT INTO coverage_mission_document_reviews VALUES(?,?,?,?,?,?,?)",
            (review_id, "mission:1", company_ref, f"doc:{review_id}",
             f"record:{review_id}", "awaiting_human_extraction", created_at))
        self.connection.execute(
            "INSERT INTO coverage_mission_discovered_documents VALUES(?,?)",
            (f"record:{review_id}", f"discovery:{review_id}"))
        self.connection.execute(
            "INSERT OR IGNORE INTO coverage_mission_source_discoveries VALUES(?,?)",
            (f"discovery:{review_id}", spec_ref))
        self.connection.commit()


class _FakeMissions:
    def __init__(self, reviews, plan=None, plan_error=None):
        self._reviews = reviews
        self._plan = plan
        self._plan_error = plan_error
        self.fallback_calls = 0

    @staticmethod
    def _review_row(row):
        return dict(row)

    def document_reviews(self, mission_version_ref, **_kwargs):
        self.fallback_calls += 1
        return []

    def latest_research_plan(self, _mission_version_ref):
        if self._plan_error is not None:
            raise self._plan_error
        return self._plan


class _FakeHost:
    def __init__(self, missions, connection):
        self.coverage_mission = missions
        self.store = type("_S", (), {"connection": connection})()


class AwaitingQueueOrderTests(unittest.TestCase):
    """The plan leads; C2's tier x company x age order is what it leads."""

    def setUp(self):
        self.reviews = _Reviews()
        self.addCleanup(self.reviews.connection.close)
        # Deliberately the live shape: the news page is the oldest thing in the
        # queue and the filing is the newest.
        self.reviews.add("r-news", company_ref=ACN,
                         spec_ref="management-changes", created_at="2026-09-01")
        self.reviews.add("r-call", company_ref=CTSH,
                         spec_ref="earnings-call-transcripts", created_at="2026-09-02")
        self.reviews.add("r-10k", company_ref=ACN,
                         spec_ref="annual-report-10k", created_at="2026-09-03")
        self.mission = {"id": "mission:1", "universe": [{"company_ref": ACN},
                                                        {"company_ref": CTSH}]}

    def _order(self, plan_ranks=None, missions=None):
        host = _FakeHost(missions or _FakeMissions(self.reviews),
                         self.reviews.connection)
        return [review["review_id"] for review in _awaiting_reviews_in_reading_order(
            host, self.mission, plan_ranks=plan_ranks)]

    def test_without_a_plan_the_queue_keeps_the_evidence_order_it_had(self):
        self.assertEqual(self._order(), ["r-10k", "r-call", "r-news"])

    def test_the_document_the_plan_named_is_read_first(self):
        ranks = {(CTSH, "earnings-call-transcripts"): 1}
        self.assertEqual(self._order(ranks), ["r-call", "r-10k", "r-news"])

    def test_even_a_news_page_the_plan_named_outruns_an_unnamed_filing(self):
        # This is the whole point of the change: the planner read the state and
        # asked for this document, and the tier ladder is the floor underneath
        # that judgement rather than a veto over it.
        ranks = {(ACN, "management-changes"): 1}
        self.assertEqual(self._order(ranks), ["r-news", "r-10k", "r-call"])

    def test_two_named_documents_keep_the_plans_own_order(self):
        ranks = {(ACN, "management-changes"): 1,
                 (CTSH, "earnings-call-transcripts"): 2}
        self.assertEqual(self._order(ranks), ["r-news", "r-call", "r-10k"])

    def test_an_unreadable_queue_still_falls_back_to_the_authority(self):
        missions = _FakeMissions(self.reviews)
        self.reviews.connection.execute("DROP TABLE coverage_mission_document_reviews")
        self.assertEqual(self._order({(ACN, "management-changes"): 1}, missions), [])
        self.assertEqual(missions.fallback_calls, 1)


class PlanResolutionTests(unittest.TestCase):
    """Reading the plan is an optimisation; failing to read one is not a stop."""

    def setUp(self):
        self.reviews = _Reviews()
        self.addCleanup(self.reviews.connection.close)
        self.mission = {"id": "mission:1", "industry_ref": "industry:us-it-services"}

    def _resolve(self, plan=None, plan_error=None):
        missions = _FakeMissions(self.reviews, plan=plan, plan_error=plan_error)
        return _plan_directed_ranks(_FakeHost(missions, self.reviews.connection),
                                    self.mission)

    def test_a_plan_becomes_ranks_and_says_which_plan_it_was(self):
        resolved = self._resolve({
            "plan_id": "mission-research-plan:abc",
            "state_hash": "a" * 64,
            "created_at": "2026-09-16T00:00:00+00:00",
            "directives": [_directive(ACN, "annual_report", "extract_figures"),
                           _directive(CTSH, "earnings_calls", "read"),
                           _directive(ACN, "broker_research", "stop")],
        })
        self.assertEqual(resolved["ranks"], {
            (ACN, "annual-report-10k"): 1,
            (CTSH, "earnings-call-transcripts"): 2,
        })
        self.assertEqual(resolved["numeric"], {(ACN, "annual-report-10k"): 1})
        self.assertEqual(resolved["plan"], {
            "mission_version_ref": "mission:1",
            "plan_ref": "mission-research-plan:abc",
            "state_hash": "a" * 64,
            "created_at": "2026-09-16T00:00:00+00:00",
            "status": "applied",
            "reading_directives": 2,
            "directed_kinds": 2,
            "figure_kinds": 1,
        })

    def test_no_plan_is_recorded_as_no_plan_and_orders_nothing(self):
        resolved = self._resolve(None)
        self.assertEqual((resolved["ranks"], resolved["numeric"]), ({}, {}))
        self.assertEqual(resolved["plan"]["status"], "no_plan")

    def test_an_unreadable_plan_is_recorded_and_the_queue_is_untouched(self):
        resolved = self._resolve(plan_error=sqlite3.OperationalError("no such table"))
        self.assertEqual((resolved["ranks"], resolved["numeric"]), ({}, {}))
        self.assertEqual(resolved["plan"]["status"], "unavailable")
        self.assertIn("no such table", resolved["plan"]["reason"])

    def test_a_plan_whose_directives_are_nonsense_orders_nothing(self):
        resolved = self._resolve({"plan_id": "p", "directives": "not a list"})
        self.assertEqual(resolved["ranks"], {})
        self.assertIn(resolved["plan"]["status"], {"unusable", "named_nothing_held"})


class _FigureService:
    """The figures pass, reduced to which reviews it was asked about."""

    def __init__(self):
        self.asked: list[str] = []

    def view(self, *, review_id, offset, **_kwargs):
        return {"context": {"content_hash": f"{review_id}:{offset}",
                            "next_offset": None}}

    def generate_numeric(self, *, review_id, **_kwargs):
        self.asked.append(review_id)
        return {"status": "read", "verified": [], "refused": [], "recorded": []}


class FigurePassCountingTests(unittest.TestCase):
    def _sweep(self, reviews, *, directed, limit=5):
        service = _FigureService()
        summary = {"numeric": [], "figures": 0, "numeric_fresh": 0,
                   "plan_directed_windows": 0}
        _secondary_sweep(
            service, [("automation:x", reviews, {})], summary,
            limit=limit, entries="numeric", wanted=lambda review, spec: True,
            call="generate_numeric",
            counts={"verified": "verified", "refused": "refused",
                    "recorded": "recorded"},
            total=("figures", "recorded"), spent_key="numeric_fresh",
            directed_reviews=directed)
        return service, summary

    def test_a_figures_window_the_plan_asked_for_is_counted(self):
        reviews = [{"review_id": "r1", "source_ref": "source:alphaengine",
                    "document_ref": "doc:1"},
                   {"review_id": "r2", "source_ref": "source:alphaengine",
                    "document_ref": "doc:2"}]
        service, summary = self._sweep(reviews, directed={"r2"})
        self.assertEqual(service.asked, ["r1", "r2"])
        self.assertEqual(summary["numeric_fresh"], 2)
        self.assertEqual(summary["plan_directed_windows"], 1)

    def test_no_plan_means_the_count_stays_at_zero(self):
        reviews = [{"review_id": "r1", "source_ref": "source:alphaengine",
                    "document_ref": "doc:1"}]
        _, summary = self._sweep(reviews, directed=set())
        self.assertEqual(summary["plan_directed_windows"], 0)


class PlanDirectedExtractionRunTests(unittest.TestCase):
    """End to end: a stored plan reaches the child's own summary."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.h = ExtractionHarness(self.root)
        self.addCleanup(self.h.close)
        self.h.add_issuer_proof()
        self.mission = self._grant_automation()
        self._runs = 0

    def _grant_automation(self):
        params = mission_params(self.h.state)
        params["autonomy"]["may_write"] = list(
            params["autonomy"]["may_write"]) + ["source_discovery"]
        for item in params["source_plan"]:
            if item["source_ref"] == "source:alphaengine":
                item["status"] = "connected"
        params.update({
            "version_id": "coverage-mission-version:us-it-services:2",
            "prior_version_ref": self.h.mission["id"],
            "idempotency_key": "coverage-mission:us-it-services:2"})
        ref = params.pop("mission_ref")
        version = self.h.missions.create_mission(ref, **params)
        self.h.missions.carry_forward_superseded_documents(ref)
        self.h.missions.backfill_document_reviews(ref)
        return version

    def _model_config(self):
        path = self.root / "extraction-model-config.json"
        path.write_text(json.dumps({
            "routing_policy_ref": policy()["policy_version_ref"],
            "credential_slot_refs": [profile()["credential_slot_ref"]],
            "model_router_db": str(self.root / "router.sqlite"),
            "broker_socket": str(self.root / "none.sock"),
            "broker_auth_key": str(self.root / "none.key"),
            "broker_client_id": "client:dalton-core", "expected_agent_id": "chem",
            "budget_db": str(self.root / "budget.sqlite"),
            "budget_policy_ref": "thesis-impact-day-budget-policy:production:1",
        }), encoding="utf-8")
        return path

    def _record_plan(self, *directives, state="one"):
        body = {
            "schema_version": "0.1",
            "mission_version_ref": self.mission["id"],
            "state_hash": content_hash({"state": state}),
            "assessment": "fixture: the held transcript is what to read next",
            "directives": list(directives),
            "inquiries": [],
        }
        plan = {**body, "content_hash": content_hash(body)}
        return self.h.missions.record_research_plan(plan, decided_by=OWNER)

    def _fixture(self):
        from dalton_core.document_extraction import DocumentExtractionService

        active = self.h.missions.active_mission("coverage-mission:us-it-services")
        review = next(item for item in self.h.missions.document_reviews(active["id"])
                      if item["state"] == "awaiting_human_extraction")
        context = DocumentExtractionService(self.h.writer).view(
            review_id=review["review_id"],
            expected_review_hash=content_hash(review), offset=0,
            actor_ref=OWNER)["context"]
        path = self.root / "fixture.json"
        path.write_text(json.dumps({"schema_version": "0.1", "suggestions": [{
            "quote_id": context["quotes"][0]["quote_id"],
            "normalized_statement": "Fixture management described cautious clients.",
            "metric_or_aspect": "aspect:client-decisions",
            "period": "not specified in this window",
            "basis": "fixture management commentary",
        }]}), encoding="utf-8")
        return path, review

    def _run(self):
        self._runs += 1
        fixture, review = self._fixture()
        summary_dir = self.root / "extractions" / f"run-{self._runs}"
        summary_dir.mkdir(parents=True)
        summary = run_extraction(
            state_dir=self.root, model_config_path=self._model_config(),
            summary_dir=summary_dir, spool_dir=self.root / "spool",
            scheduler_db=self.root / "scheduler.sqlite",
            connector_governance=None, web_fetch_governance=None,
            max_windows=2, requested_by=None, hermetic_fixture=fixture)
        return summary, review

    def test_a_plan_that_names_the_held_transcript_is_counted_and_named(self):
        recorded = self._record_plan(_directive(ACN, "earnings_calls", "read"))
        summary, review = self._run()
        self.assertEqual(summary["status"], "succeeded", summary)
        self.assertEqual(len(summary["drafted"]), 2)
        # Both windows were read because the plan asked for this document.
        self.assertEqual(summary["plan_directed_windows"], 2)
        self.assertEqual(summary["plan_directed"]["reviews"], 1)
        plan = summary["plan_directed"]["plans"][0]
        self.assertEqual(plan["plan_ref"], recorded["plan_id"])
        self.assertEqual((plan["status"], plan["reading_directives"]), ("applied", 1))
        self.assertEqual(summary["plan_directed"]["documents"], [{
            "review_id": review["review_id"],
            "document_ref": NEW_DOC,
            "company_ref": ACN,
            "spec_ref": "earnings-call-transcripts",
            "plan_rank": 1,
        }])

    def test_without_a_plan_the_run_is_unchanged_and_counts_zero(self):
        summary, _ = self._run()
        self.assertEqual(summary["status"], "succeeded", summary)
        self.assertEqual(len(summary["drafted"]), 2)
        self.assertEqual(summary["plan_directed_windows"], 0)
        self.assertEqual(summary["plan_directed"]["reviews"], 0)
        self.assertEqual([plan["status"] for plan in summary["plan_directed"]["plans"]],
                         ["no_plan"])

    def test_a_plan_that_only_refuses_work_directs_no_window(self):
        self._record_plan(_directive(ACN, "earnings_calls", "stop"))
        summary, _ = self._run()
        self.assertEqual(summary["plan_directed_windows"], 0)
        self.assertEqual(summary["plan_directed"]["plans"][0]["status"],
                         "named_nothing_held")


class _PlanStore:
    """A Core holding one plan, one acquired document and one read proof."""

    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE mission_document_research_admissions(
                admission_id TEXT PRIMARY KEY, record_json TEXT NOT NULL,
                content_hash TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE mission_document_research_outcomes(
                outcome_id TEXT PRIMARY KEY, admission_ref TEXT NOT NULL UNIQUE);
            CREATE TABLE coverage_mission_pointer(
                mission_ref TEXT PRIMARY KEY, mission_version_id TEXT NOT NULL);
            CREATE TABLE coverage_mission_research_plans(
                plan_id TEXT PRIMARY KEY, mission_version_ref TEXT NOT NULL,
                inquiries_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE coverage_mission_discovered_documents(
                record_id TEXT PRIMARY KEY, mission_version_ref TEXT,
                document_ref TEXT, discovery_ref TEXT, status TEXT);
            CREATE TABLE coverage_mission_source_discoveries(
                record_id TEXT PRIMARY KEY, spec_ref TEXT);
            CREATE TABLE document_read_completion_proofs(
                proof_id TEXT PRIMARY KEY, document_ref TEXT NOT NULL);
            """
        )
        self.connection.execute(
            "INSERT INTO coverage_mission_pointer VALUES(?,?)",
            ("coverage-mission:us-it-services", "mission:1"))
        self.connection.commit()

    def active_policy(self):
        return {"policy": {"research_candidate_auto_commit": {
            "enabled": False, "rules": [], "max_records": 10}}}

    def add_plan(self, inquiries, *, plan_id="mission-research-plan:1"):
        self.connection.execute(
            "INSERT INTO coverage_mission_research_plans VALUES(?,?,?,?)",
            (plan_id, "mission:1", canonical_json(inquiries),
             "2026-09-16T00:00:00+00:00"))
        self.connection.commit()

    def add_document(self, document_ref, *, spec_ref, status="acquired",
                     read_complete=True):
        self.connection.execute(
            "INSERT INTO coverage_mission_discovered_documents VALUES(?,?,?,?,?)",
            (f"record:{document_ref}", "mission:1", document_ref,
             f"discovery:{document_ref}", status))
        self.connection.execute(
            "INSERT INTO coverage_mission_source_discoveries VALUES(?,?)",
            (f"discovery:{document_ref}", spec_ref))
        if read_complete:
            self.connection.execute(
                "INSERT INTO document_read_completion_proofs VALUES(?,?)",
                (f"proof:{document_ref}", document_ref))
        self.connection.commit()

    def add_admission(self, inquiry_ref):
        from dalton_core.store import content_hash as digest

        body = {"schema_version": "0.1", "id": "mission-document-research-admission:1",
                "created_at": "2026-09-16T01:00:00+00:00",
                "inquiry_ref": inquiry_ref}
        wire = {**body, "content_hash": digest(body)}
        self.connection.execute(
            "INSERT INTO mission_document_research_admissions VALUES(?,?,?,?)",
            (wire["id"], canonical_json(wire), wire["content_hash"],
             wire["created_at"]))
        # Settled, so the lane has nothing to start and the tick reaches the
        # terminal return -- and the inquiry is still one this install bought.
        self.connection.execute(
            "INSERT INTO mission_document_research_outcomes VALUES(?,?)",
            ("mission-document-research-outcome:1", wire["id"]))
        self.connection.commit()
        return wire


class _NoLauncher:
    def __init__(self, tickets_dir):
        self.tickets_dir = tickets_dir
        tickets_dir.mkdir(parents=True, exist_ok=True)

    def status(self, ticket_ref):  # pragma: no cover - never reached here
        raise LookupError(ticket_ref)


def _inquiry(question, *, document_ref, company_ref=ACN, wants="the transcript"):
    inquiry = {"company_ref": company_ref, "question": question, "wants": wants,
               "because": "fixture"}
    if document_ref is not None:
        inquiry["directed_document"] = {
            "strategy_version": "0.1", "document_ref": document_ref,
            "document_version_hash": "b" * 64, "query_terms": ["fixture"],
            "query_rationale": "fixture"}
    return inquiry


class PlanNextStepTests(unittest.TestCase):
    """G2: what the plan asks for that already-read material can answer."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = _PlanStore()
        self.addCleanup(self.store.connection.close)
        self.lane = MissionDocumentResearchCoordinator(
            store=self.store,
            launcher=_NoLauncher(Path(self.temp.name) / "tickets"))

    def _steps(self):
        result = self.lane.dispatch_once()
        self.assertEqual(result["status"], "idle")
        return result["plan_next_steps"]

    def test_an_addressable_annual_report_inquiry_is_offered_in_chinese(self):
        self.store.add_plan([_inquiry("研发支出为什么翻倍？", document_ref="doc:10k")])
        self.store.add_document("doc:10k", spec_ref="annual-report-10k")
        steps = self._steps()
        self.assertEqual(len(steps), 1)
        self.assertEqual(steps[0]["document_ref"], "doc:10k")
        self.assertIn("计划建议的下一步", steps[0]["suggestion"])
        self.assertIn("年报", steps[0]["suggestion"])
        self.assertIn("研发支出为什么翻倍？", steps[0]["suggestion"])
        # Read-only: saying so is the whole feature, and nothing was started.
        self.assertIn("授权链", steps[0]["suggestion"])

    def test_an_earnings_call_follow_up_is_offered_too(self):
        self.store.add_plan([_inquiry("管理层怎么解释订单下滑？", document_ref="doc:call")])
        self.store.add_document("doc:call", spec_ref="earnings-call-transcripts")
        self.assertIn("电话会纪要", self._steps()[0]["suggestion"])

    def test_an_inquiry_with_no_directed_document_is_not_addressable(self):
        self.store.add_plan([_inquiry("谁在抢份额？", document_ref=None)])
        self.assertEqual(self._steps(), [])

    def test_a_document_that_was_never_acquired_is_not_offered(self):
        self.store.add_plan([_inquiry("研发支出？", document_ref="doc:10k")])
        self.store.add_document("doc:10k", spec_ref="annual-report-10k",
                                status="acquisition_failed")
        self.assertEqual(self._steps(), [])

    def test_a_document_nobody_finished_reading_is_not_offered(self):
        # The point of the suggestion is "the bytes are already read"; an
        # unread document is a reading job, not a next step.
        self.store.add_plan([_inquiry("研发支出？", document_ref="doc:10k")])
        self.store.add_document("doc:10k", spec_ref="annual-report-10k",
                                read_complete=False)
        self.assertEqual(self._steps(), [])

    def test_a_broker_note_is_not_an_annual_report_or_a_call(self):
        self.store.add_plan([_inquiry("券商怎么看？", document_ref="doc:note")])
        self.store.add_document("doc:note", spec_ref="sell-side-reports")
        self.assertEqual(self._steps(), [])

    def test_an_inquiry_already_admitted_is_not_offered_again(self):
        from dalton_core.research_task import inquiry_content_hash, inquiry_ref_for

        inquiry = _inquiry("研发支出？", document_ref="doc:10k")
        self.store.add_plan([inquiry])
        self.store.add_document("doc:10k", spec_ref="annual-report-10k")
        self.assertEqual(len(self._steps()), 1)
        self.store.add_admission(inquiry_ref_for(inquiry_content_hash(inquiry)))
        self.assertEqual(self._steps(), [])

    def test_a_core_without_plans_says_nothing_rather_than_failing(self):
        self.store.connection.execute("DROP TABLE coverage_mission_research_plans")
        self.assertEqual(self._steps(), [])

    def test_a_core_without_read_proofs_claims_nothing_it_cannot_prove(self):
        self.store.add_plan([_inquiry("研发支出？", document_ref="doc:10k")])
        self.store.add_document("doc:10k", spec_ref="annual-report-10k")
        self.store.connection.execute("DROP TABLE document_read_completion_proofs")
        self.assertEqual(self._steps(), [])

    def test_the_list_is_bounded_so_it_stays_readable(self):
        inquiries = [_inquiry(f"问题{index}？", document_ref=f"doc:{index}")
                     for index in range(8)]
        self.store.add_plan(inquiries)
        for index in range(8):
            self.store.add_document(f"doc:{index}", spec_ref="annual-report-10k")
        self.assertEqual(len(self._steps()), 5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

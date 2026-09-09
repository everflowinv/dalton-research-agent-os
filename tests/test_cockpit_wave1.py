"""INT1: the four merged Wave 1 lanes, on the owner's page.

The market layer, the Claim index, the forecast lines and the quality loop
each landed with a "what the cockpit needs" section in their report and no
cockpit change, because the two cockpit files were nobody's to touch. This is
that wiring, and the thing it mostly has to get right is the degradation: the
cockpit runs against Cores older than every one of these lanes, so a card with
no price history must look like the card it always was rather than an error
page. Half of what follows is that case.

The other half is the vocabulary. A lane that is silent because the mission
never granted it, a lane whose connector record is installed but unapproved,
and a lane that is installed and simply has nothing to do all read as "idle"
on a page that only knows that word -- and two of the three are waiting on the
owner.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.analyst_journal import AnalystJournalAuthority
from dalton_core.claim_index_authority import ClaimIndexAuthority
from dalton_core.cockpit_plane import CockpitConflict, CockpitError
from dalton_core.market_price import MarketPriceSeriesAuthority
from dalton_core.mission_deliverable import MissionDeliverableAuthority
from dalton_core.model_forecast_driver import ForecastModelAuthority
from dalton_core.research_quality_rubrics import RUBRICS
from dalton_core.research_quality_score import (
    QualityScoreAuthority,
    run_deterministic,
)
from dalton_core.store import content_hash
from dalton_core.valuation_snapshot import ValuationSnapshotAuthority
from tests.test_cockpit_plane import CockpitHarness
from tests.test_model_forecast_driver import model as forecast_body
from tests.test_valuation_snapshot import history as price_history, roles

ACN = "company:sec-cik:0001467373"
CTSH = "company:sec-cik:0001058290"
AUTOMATION = "automation:coverage-mission"
OWNER = "human:lumos"
OWNER_LOGIN = "owner@example.com"
INVOCATION = "connector-invocation:yfinance:" + "a" * 32
ARTIFACT = "1" * 64
GOVERNANCE = "connector-governance:yfinance-daily-prices:v1"
GOVERNANCE_HASH = "2" * 64
# After the US close on either side of daylight saving; see
# market_price.SETTLED_AFTER_UTC_HOUR.
SETTLED = "2026-09-11T23:30:00+00:00"
MID_SESSION = "2026-09-11T17:00:00+00:00"
SCREEN = RUBRICS["rubric:initial-screen"]


def bar(date: str, close: str) -> dict:
    return {"date": date, "open": close, "high": close, "low": close,
            "close": close, "adj_close": close, "volume": "1000000"}


class Wave1Case(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.c = CockpitHarness(self.root)
        self.addCleanup(self.c.close)
        self.store = self.c.h.h.core
        self.plane = self.c.plane
        self.login = OWNER_LOGIN

    # -- fixtures ----------------------------------------------------------

    def card(self, view: dict, company_ref: str = ACN) -> dict:
        return next(c for c in view["companies"] if c["company_ref"] == company_ref)

    def publish_prices(self, *, captured_at: str = SETTLED, closes=("100", "110"),
                       start: str = "2026-09-10") -> dict:
        dates = ["2026-09-10", "2026-09-11"][: len(closes)]
        return MarketPriceSeriesAuthority(self.store).publish_series(
            company_ref=ACN, ticker="ACN", currency="USD",
            bars=[bar(date, close) for date, close in zip(dates, closes)],
            observations=[], invocation_ref=INVOCATION, artifact_hash=ARTIFACT,
            governance_ref=GOVERNANCE, governance_hash=GOVERNANCE_HASH,
            requested_start=start, requested_end="2026-09-11",
            captured_at=captured_at,
        )

    def publish_valuation(self) -> dict:
        price_version = "market-price-series-version:" + "a" * 32
        binding = {
            "version_ref": price_version, "version_hash": "b" * 64,
            "invocation_ref": "connector-invocation:yfinance:" + "c" * 32,
            "artifact_hash": "d" * 64,
        }
        return ValuationSnapshotAuthority(self.store).publish_snapshot(
            company_ref=ACN,
            price={**binding, "bar_date": "2026-09-08", "close": "100", "currency": "USD"},
            shares={**binding, "as_of": "2026-09-08", "shares_outstanding": "1000000"},
            fundamental_windows=[{"as_of": "2025-12-31", "roles": roles()}],
            price_history=price_history(),
        )

    def publish_model(self) -> dict:
        # The lane's own body, published unchanged: what the card reads has
        # to be what the lane writes, not a shape invented here.
        return ForecastModelAuthority(self.store).publish(forecast_body())

    def publish_deliverable(self, *, body: str = "客户预算在恢复，管理层这样说。") -> dict:
        mission = self.c.h.missions.active_mission("coverage-mission:us-it-services")
        return MissionDeliverableAuthority(self.store).publish(
            kind="initial_screen", subject_ref=ACN, mission=mission,
            playbook=self.c.h.state["playbook"],
            template_ref="playbook:deliverable_templates.initial_screen",
            sections=[{"title": "结论", "body": body, "claim_refs": [],
                       "numbers": [], "gaps": []}],
            # The live mission does not grant automation the deliverable
            # scope; publishing as the owner is the path that exists today.
            summary="第一份筛选。", actor_ref=OWNER,
        )

    def score(self, deliverable: dict, *, judged: bool = True) -> dict:
        art = {
            "artefact_kind": "initial_screen", "ref": deliverable["id"],
            "hash": deliverable["content_hash"], "title": "", "subject_ref": ACN,
            "sections": [{"title": "结论", "body": "文字。", "claim_refs": [],
                          "numbers": [], "gaps": []}],
            "gaps": [], "expected_sections": None, "shown_claims": [],
            "cited_tags": [], "confidence": None, "question": None, "prior": None,
        }
        judge = None
        if judged:
            judge = {
                "status": "scored", "rubric_hash": SCREEN.content_hash,
                "model": {"route_decision_ref": "route:abc", "purpose": "quality",
                          "invocation_ref": "invocation:x"},
                "scores": [{"criterion_id": item, "score": 1 if index == 0 else 3,
                            "evidence": "文档的这一部分支持这个分数。"}
                           for index, item in enumerate(SCREEN.criterion_ids)],
            }
        return QualityScoreAuthority(self.store).record(
            artefact_kind="initial_screen", target_ref=deliverable["id"],
            target_hash=deliverable["content_hash"], rubric=SCREEN,
            deterministic=run_deterministic(art, SCREEN), judge_layer=judge,
            subject_ref=ACN, actor_ref=AUTOMATION,
        )

    def add_claim(self, *, statement: str, subject: str = ACN,
                  created_at: str = "2026-09-02T00:00:00+00:00") -> dict:
        digest = content_hash({"statement": statement, "at": created_at})
        claim = {
            "schema_version": "0.2", "id": f"claim-version:{digest[:64]}",
            "claim_ref": f"claim:int1:{digest[:16]}", "version": 1,
            "subject_ref": subject, "metric_or_aspect": "revenue",
            "period": "2026-03-01..2026-05-31", "basis": "fixture",
            "normalized_statement": statement, "claim_kind": "quantitative",
            "value": "1000", "unit": "USD", "currency": "USD", "scale": "million",
            "producer_execution_refs": [], "semantic_review_ref": None,
            "semantic_review_hash": None, "candidate_origin_ref": None,
            "candidate_origin_hash": None, "actor_ref": "system:research-auto-commit",
            "prior_version_ref": None, "created_at": created_at,
        }
        claim["content_hash"] = content_hash(
            {k: v for k, v in claim.items() if k != "content_hash"})
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO claim_versions(claim_version_id,claim_ref,version_number,"
                "claim_json,content_hash,created_at) VALUES(?,?,?,?,?,?)",
                (claim["id"], claim["claim_ref"], 1,
                 json.dumps(claim, sort_keys=True), claim["content_hash"],
                 claim["created_at"]),
            )
        return claim

    def index(self, claim: dict, *, aspect: str = "segments_and_mix",
              importance: str = "news", group: str = "g1") -> dict:
        return ClaimIndexAuthority(self.store).record_entry(
            claim_version_ref=claim["id"], claim_version_hash=claim["content_hash"],
            claim_ref=claim["claim_ref"], claim_created_at=claim["created_at"],
            subject_ref=claim["subject_ref"], metric_or_aspect="revenue",
            period_key="2026-03-01..2026-05-31", claim_kind="quantitative",
            aspect=aspect, aspect_source="rule", as_of="2026-05-31",
            as_of_basis="period_end", importance=importance,
            importance_basis="evidence_source_type:public_web",
            dedupe_group_key=group, tagger_ref="rule:claim-index-deterministic:0.1",
            tagger_hash="a" * 64, actor_ref=AUTOMATION,
            created_at="2026-09-09T00:00:00+00:00",
        )


class OldCoreTests(Wave1Case):
    """A Core from before these lanes shows the page it always showed."""

    def test_the_card_carries_nothing_rather_than_failing(self) -> None:
        view = self.plane.overview()
        card = self.card(view)
        self.assertIsNone(card["market"])
        self.assertIsNone(card["valuation"])
        self.assertIsNone(card["model"])
        self.assertIsNone(card["feedback"])
        # No journal table means no buttons, rather than buttons that fail
        # when pressed.
        self.assertFalse(view["feedback_enabled"])

    def test_the_claims_view_still_answers_and_says_it_has_no_index(self) -> None:
        self.add_claim(statement="ACN 本季收入 1,000 百万美元。")
        listing = self.plane.claims()
        self.assertFalse(listing["indexed"])
        self.assertEqual(listing["total"], len(listing["items"]))
        self.assertTrue(all(item["aspect"] is None for item in listing["items"]))

    def test_an_index_filter_on_an_unindexed_core_is_refused_in_plain_words(self) -> None:
        # "There is no index here" and "nothing matched" are different
        # answers, and a reader who cannot tell them apart draws the wrong
        # conclusion about their own data.
        with self.assertRaises(CockpitError) as caught:
            self.plane.claims(index_aspect="demand_drivers")
        self.assertIn("还没有给结论建索引", str(caught.exception))

    def test_asking_for_a_model_that_does_not_exist_says_so(self) -> None:
        with self.assertRaises(CockpitError):
            self.plane.company_model(ACN)


class MarketCardTests(Wave1Case):
    def test_the_close_carries_its_date_and_says_it_settled(self) -> None:
        self.publish_prices()
        market = self.card(self.plane.overview())["market"]
        self.assertEqual((market["close"], market["as_of"]), ("110", "2026-09-11"))
        self.assertEqual(market["currency"], "USD")
        self.assertFalse(market["provisional"])
        self.assertEqual(market["note"], "收盘价")
        # The range change names the day it is measured from, because a
        # percentage whose window the reader cannot see is unusable.
        self.assertEqual((market["change_percent"], market["change_since"]),
                         (10.0, "2026-09-10"))

    def test_a_mid_session_price_says_it_is_not_a_close(self) -> None:
        # A price pulled mid-session has exactly the shape of a close. A card
        # that prints it without saying so tells the owner the market closed
        # at a number it never closed at.
        self.publish_prices(captured_at=MID_SESSION)
        market = self.card(self.plane.overview())["market"]
        self.assertTrue(market["provisional"])
        self.assertIn("盘中", market["note"])

    def test_the_card_reads_the_newest_version_not_the_first(self) -> None:
        # Every version of a price series carries the whole history, so the
        # card has to pick the head. Reading the first row would show a close
        # from before the last restatement and never move again.
        self.publish_prices(closes=("100", "110"))
        self.publish_prices(closes=("100", "121"))
        market = self.card(self.plane.overview())["market"]
        self.assertEqual((market["close"], market["version"]), ("121", 2))

    def test_a_company_with_no_series_has_no_price_block(self) -> None:
        self.publish_prices()
        self.assertIsNone(self.card(self.plane.overview(), CTSH)["market"])


class ValuationCardTests(Wave1Case):
    def test_every_percentile_carries_the_basis_it_rests_on(self) -> None:
        self.publish_valuation()
        valuation = self.card(self.plane.overview())["valuation"]
        self.assertEqual(valuation["as_of"], "2026-09-08")
        by_metric = {item["metric"]: item for item in valuation["metrics"]}
        available = [item for item in valuation["metrics"] if item["status"] == "available"]
        self.assertTrue(available)
        for item in available:
            if item["percentile"] is not None:
                # One fundamental window means the multiple never moved for a
                # reason other than price, and the label says exactly that.
                self.assertEqual(item["percentile_basis"], "price_only")
                self.assertIn("基本面", item["percentile_basis_label"])
            else:
                self.assertTrue(item["percentile_reason"])
        # An unavailable metric shows the sentence, never a blank.
        for item in valuation["metrics"]:
            if item["status"] != "available":
                self.assertTrue(item["reason"])
        self.assertIn("trailing_pe", by_metric)
        self.assertEqual(valuation["basis"]["market_cap_formula"],
                         "close * shares_outstanding")
        self.assertIn("股本", valuation["basis"]["shares_note"])


class ForecastCardTests(Wave1Case):
    def test_the_card_counts_the_model_and_refuses_to_score_it(self) -> None:
        self.publish_model()
        card = self.card(self.plane.overview())
        model = card["model"]
        readiness = model["readiness"]
        self.assertEqual(model["version"], 1)
        self.assertEqual(model["change_reason_label"], "证据变厚了")
        for field in ("forecast_quarters", "drivers", "drivers_with_assumptions",
                      "results_computed", "results_unavailable", "assumption_kinds",
                      "realised_quarters", "actual_cells", "superseded_estimates"):
            self.assertIn(field, readiness)
        self.assertIn("驱动因素有假设", model["note"])
        # Counted, never scored: no percentage anywhere on this block.
        self.assertNotIn("%", model["note"])
        self.assertIn("模型估的", model["assumptions_by_kind"])

    def test_the_model_view_prints_the_lane_s_own_table(self) -> None:
        published = self.publish_model()
        view = self.plane.company_model(ACN)
        self.assertEqual(view["version"], published["version"])
        self.assertTrue(view["company"].startswith("ACN"))
        # The renderer's own layout, not a second one here: it is what puts
        # each assumption's reason under the assumption.
        self.assertIn("|", view["table"])
        self.assertIn("readiness", view)
        self.assertEqual([item["version"] for item in view["history"]], [1])

    def test_a_company_outside_the_model_is_refused_by_name(self) -> None:
        self.publish_model()
        with self.assertRaises(CockpitError):
            self.plane.company_model(CTSH)


class QualityAndJournalTests(Wave1Case):
    def test_the_deliverable_carries_its_score_and_the_rubric_s_own_questions(self) -> None:
        deliverable = self.publish_deliverable()
        self.score(deliverable)
        document = self.card(self.plane.overview())["document"]
        quality = document["quality"]
        self.assertEqual(quality["rubric"], SCREEN.title)
        self.assertEqual(quality["target_hash"], deliverable["content_hash"])
        self.assertEqual(len(quality["criteria"]), len(SCREEN.criterion_ids))
        # The criterion is shown as the question it asks, not as its id.
        first = quality["criteria"][0]
        self.assertEqual(first["question"],
                         SCREEN.criterion(first["criterion_id"]).question)
        self.assertTrue(first["below_passing"])
        self.assertEqual(quality["below_passing"], [first["question"]])
        self.assertTrue(all(check["label"] for check in quality["checks"]))
        self.assertEqual(document["feedback"]["target_ref"], deliverable["id"])
        self.assertEqual(document["feedback"]["target_hash"], deliverable["content_hash"])

    def test_opening_a_deliverable_reads_its_gate_reason(self) -> None:
        # This page raised "no such column: rationale" the first time there
        # was a deliverable to open: the reason a gate passed lives inside the
        # stage record, not in a column of the table.
        deliverable = self.publish_deliverable()
        mission = self.c.h.missions.active_mission("coverage-mission:us-it-services")
        self.c.h.missions.record_stage(
            mission_version_ref=mission["id"],
            mission_version_hash=mission["content_hash"],
            company_ref=ACN, stage_ref="initial_screen", status="entered",
            evidence_refs=[], rationale="资料底座齐了。", actor_ref=OWNER,
            idempotency_key="int1:acn:initial_screen:entered")
        document = self.plane.document(deliverable["id"])
        self.assertEqual([item["rationale"] for item in document["stage_history"]],
                         ["资料底座齐了。"])
        self.assertTrue(document["company"].startswith("ACN"))

    def test_a_score_with_no_judge_is_the_checks_alone(self) -> None:
        deliverable = self.publish_deliverable()
        self.score(deliverable, judged=False)
        quality = self.card(self.plane.overview())["document"]["quality"]
        self.assertEqual(quality["judge_status"], "not_judged")
        self.assertEqual(quality["criteria"], [])
        self.assertIsNotNone(quality["checks_passed"])

    def test_feedback_goes_out_as_the_owner_and_repeats_are_duplicates(self) -> None:
        deliverable = self.publish_deliverable()
        AnalystJournalAuthority(self.store)  # the table exists on this Core
        seen: list[dict] = []

        def governance(token_config, socket, *, actor_ref, operation, params):
            seen.append({"actor_ref": actor_ref, "operation": operation, **params})
            return AnalystJournalAuthority(self.store).add(actor_ref=actor_ref, **params)

        self.plane.governance_call = governance
        first = self.plane.record_feedback(self.login, {
            "target_ref": deliverable["id"], "target_hash": deliverable["content_hash"],
            "target_kind": "initial_screen", "verdict": "needs_more_evidence",
            "company_ref": ACN, "note": "再找两条一手证据。"})
        self.assertEqual(first["status"], "fresh")
        self.assertEqual(first["verdict_label"], "证据不够")
        self.assertEqual(seen[0]["operation"], "record_analyst_journal_entry")
        # The principal is the owner's Tailscale-derived one, and the
        # idempotency key is content-addressed rather than spelling out an
        # email address into the Core.
        self.assertTrue(seen[0]["actor_ref"].startswith("human:tailscale-"))
        self.assertNotIn(self.login, json.dumps(seen[0], ensure_ascii=False))
        again = self.plane.record_feedback(self.login, {
            "target_ref": deliverable["id"], "target_hash": deliverable["content_hash"],
            "target_kind": "initial_screen", "verdict": "needs_more_evidence",
            "company_ref": ACN, "note": "再找两条一手证据。"})
        self.assertTrue(again["duplicate"])
        # And it shows up on the card and beside the document.
        view = self.plane.overview()
        self.assertTrue(view["feedback_enabled"])
        card = self.card(view)
        self.assertEqual((card["feedback"]["total"], card["feedback"]["outstanding"]), (1, 1))
        document = self.plane.document(deliverable["id"])
        self.assertEqual([e["verdict_label"] for e in document["feedback"]["entries"]],
                         ["证据不够"])
        self.assertTrue(any(e["kind"] == "feedback" for e in self.plane.log()["events"]))

    def test_a_verdict_outside_the_five_never_leaves_the_cockpit(self) -> None:
        calls: list = []
        self.plane.governance_call = lambda *a, **k: calls.append(k)
        with self.assertRaises(CockpitError):
            self.plane.record_feedback(self.login, {
                "target_ref": "mission-deliverable-version:x", "target_hash": "a" * 64,
                "target_kind": "initial_screen", "verdict": "excellent"})
        self.assertEqual(calls, [])

    def test_a_refused_write_is_reported_rather_than_swallowed(self) -> None:
        from dalton_core.governance_cli import GovernanceCliError

        def governance(*args, **kwargs):
            raise GovernanceCliError("operation is not a human governance operation")

        self.plane.governance_call = governance
        with self.assertRaises(CockpitConflict):
            self.plane.record_feedback(self.login, {
                "target_ref": "mission-deliverable-version:x", "target_hash": "a" * 64,
                "target_kind": "initial_screen", "verdict": "read"})


class JournalWriterOpTests(unittest.TestCase):
    """The writer op the buttons go through, and who it will take one from."""

    def test_the_operation_is_human_governance_and_binds_the_actor(self) -> None:
        from dalton_core.writer_server import (
            HUMAN_GOVERNANCE_OPERATIONS,
            OPERATION_ACTOR_FIELDS,
            OPERATION_FIELDS,
        )

        self.assertIn("record_analyst_journal_entry", HUMAN_GOVERNANCE_OPERATIONS)
        self.assertEqual(OPERATION_ACTOR_FIELDS["record_analyst_journal_entry"],
                         "actor_ref")
        self.assertEqual(
            OPERATION_FIELDS["record_analyst_journal_entry"],
            frozenset({"target_ref", "target_hash", "target_kind", "verdict",
                       "company_ref", "note", "score_override", "idempotency_key",
                       "actor_ref"}),
        )

    def test_the_index_filters_are_reachable_from_outside_the_process(self) -> None:
        from dalton_core.writer_server import OPERATION_FIELDS

        self.assertTrue(
            {"index_aspect", "as_of_from", "as_of_to", "importance", "canonical_only"}
            <= OPERATION_FIELDS["company_research_query"]
        )

    def test_automation_cannot_write_in_the_analyst_s_journal(self) -> None:
        # The journal is the one place that carries a person's judgement.
        # Automation grading itself is the quality score and has its own
        # record; the writer refuses it here and the authority refuses it
        # again, because one gate is not enough for that.
        from dalton_core.analyst_journal import AnalystJournalValidationError
        from dalton_core.store import DaltonStore

        with tempfile.TemporaryDirectory() as directory:
            store = DaltonStore(str(Path(directory) / "core.sqlite"))
            self.addCleanup(store.close)
            journal = AnalystJournalAuthority(store)
            with self.assertRaises(AnalystJournalValidationError):
                journal.add(target_ref="mission-deliverable-version:x",
                            target_hash="a" * 64, target_kind="initial_screen",
                            verdict="useful", actor_ref=AUTOMATION)


class LaneVocabularyTests(Wave1Case):
    """P11a: a lane silent for want of a grant must be visible as that."""

    def lanes(self, planner: dict) -> dict:
        heartbeat = json.loads(self.c.heartbeat.read_text(encoding="utf-8"))
        heartbeat["bounded_planner"]["last_result"] = {
            **heartbeat["bounded_planner"]["last_result"], **planner}
        self.c.heartbeat.write_text(json.dumps(heartbeat), encoding="utf-8")
        return {lane["key"]: lane
                for lane in self.plane.overview()["activity"]["lanes"]}

    def test_every_registered_lane_has_a_row(self) -> None:
        from dalton_core.lane_registry import registered_lanes

        lanes = self.lanes({})
        expected = {f"lane:{spec.driver_key}" for spec in registered_lanes()
                    if spec.driver_key not in {"mission_source_discovery",
                                               "document_extraction"}}
        self.assertTrue(expected <= set(lanes))
        self.assertTrue(all(lanes[key]["label"] for key in expected))

    def test_ungranted_is_not_idle(self) -> None:
        lanes = self.lanes({
            "mission_market_prices": {
                "status": "ungranted",
                "reason": "this mission does not grant market_price in autonomy.may_write"},
            "company_model_forecast": {"status": "idle", "reason": "nothing to do"},
        })
        self.assertEqual(lanes["lane:mission_market_prices"]["status"], "ungranted")
        self.assertIn("market_price", lanes["lane:mission_market_prices"]["note"])
        self.assertEqual(lanes["lane:company_model_forecast"]["status"], "idle")

    def test_a_lane_that_never_ran_says_so_rather_than_reading_as_idle(self) -> None:
        self.assertEqual(self.lanes({})["lane:mission_market_prices"]["status"],
                         "unstarted")

    def test_an_installed_but_unapproved_record_is_its_own_word(self) -> None:
        # The record is on disk, so the lane is configured and will keep
        # starting children that refuse. "Waiting for you" is the true word,
        # and it is neither "idle" nor "failed".
        governance = self.root / "connector-governance"
        governance.mkdir(parents=True, exist_ok=True)
        (governance / "yfinance-daily-prices-v1.json").write_text(
            json.dumps({"id": "connector-governance:yfinance-daily-prices:v1",
                        "status": "proposed"}), encoding="utf-8")
        lanes = self.lanes({"mission_market_prices": {"status": "unconfigured",
                                                      "reason": "no approved record"}})
        row = lanes["lane:mission_market_prices"]
        self.assertEqual(row["status"], "unapproved")
        self.assertIn("yfinance-daily-prices-v1.json", row["note"])
        # Approving it in place puts the lane back to what the tick reports.
        (governance / "yfinance-daily-prices-v1.json").write_text(
            json.dumps({"id": "connector-governance:yfinance-daily-prices:v1",
                        "status": "approved"}), encoding="utf-8")
        after = self.lanes({"mission_market_prices": {"status": "unconfigured",
                                                      "reason": "no approved record"}})
        self.assertEqual(after["lane:mission_market_prices"]["status"], "unconfigured")

    def test_the_four_named_lanes_keep_their_place_and_their_budgets(self) -> None:
        lanes = list(self.plane.overview()["activity"]["lanes"])
        self.assertEqual([lane["key"] for lane in lanes[:4]],
                         ["web", "alphaengine", "extraction", "weekly"])


class ClaimIndexViewTests(Wave1Case):
    def test_the_view_is_canonical_only_and_filters_by_aspect_and_source(self) -> None:
        filed = self.add_claim(statement="收入 1,000 百万美元（10-Q）。",
                               created_at="2026-09-01T00:00:00+00:00")
        news = self.add_claim(statement="媒体报道收入约 1,000 百万美元。",
                              created_at="2026-09-02T00:00:00+00:00")
        other = self.add_claim(statement="管理层谈到需求在恢复。",
                               created_at="2026-09-03T00:00:00+00:00")
        self.index(filed, importance="filing", group="revenue-q3")
        self.index(news, importance="news", group="revenue-q3")
        self.index(other, aspect="demand_drivers", importance="management_statement",
                   group="demand-q3")

        listing = self.plane.claims()
        self.assertTrue(listing["indexed"])
        # The same fact from a filing and from a newspaper is one fact, and
        # the filing is the copy that survives.
        statements = [item["statement"] for item in listing["items"]]
        self.assertIn(filed["normalized_statement"], statements)
        self.assertNotIn(news["normalized_statement"], statements)
        # Most important first.
        self.assertEqual(listing["items"][0]["importance"], "filing")
        self.assertEqual(listing["items"][0]["importance_label"], "公司报表原文")

        everything = self.plane.claims(canonical_only=False)
        self.assertIn(news["normalized_statement"],
                      [item["statement"] for item in everything["items"]])

        by_aspect = self.plane.claims(index_aspect="demand_drivers")
        self.assertEqual([item["statement"] for item in by_aspect["items"]],
                         [other["normalized_statement"]])
        self.assertEqual(by_aspect["items"][0]["aspect_label"], "需求从哪来")

        by_source = self.plane.claims(importance="management_statement")
        self.assertEqual(len(by_source["items"]), 1)

    def test_an_untagged_claim_is_shown_and_says_it_is_untagged(self) -> None:
        tagged = self.add_claim(statement="收入 1,000 百万美元。")
        self.index(tagged)
        self.add_claim(statement="还没有被索引的一条结论。",
                       created_at="2026-09-04T00:00:00+00:00")
        items = self.plane.claims()["items"]
        untagged = [item for item in items if not item["indexed"]]
        self.assertEqual(len(untagged), 1)
        self.assertIsNone(untagged[0]["aspect_label"])
        # An untagged claim sorts after every tagged one, because a sort key
        # that exists only on some rows cannot sort a list.
        self.assertTrue(items[0]["indexed"])
        self.assertFalse(items[-1]["indexed"])

    def test_the_filters_carry_their_own_vocabulary_in_plain_words(self) -> None:
        listing = self.plane.claims()
        self.assertIn({"value": "guidance_style", "label": "指引风格"},
                      listing["vocabulary"]["aspects"])
        self.assertIn({"value": "filing", "label": "公司报表原文"},
                      listing["vocabulary"]["importance"])
        self.assertTrue(all(item["label"] for item in listing["companies"]))

    def test_one_company_at_a_time(self) -> None:
        mine = self.add_claim(statement="ACN 的一条结论。")
        self.add_claim(statement="CTSH 的一条结论。", subject=CTSH)
        listing = self.plane.claims(company_ref=ACN)
        self.assertEqual([item["statement"] for item in listing["items"]],
                         [mine["normalized_statement"]])


class AskContextTests(Wave1Case):
    def test_the_answer_context_drops_the_copies_the_index_marked(self) -> None:
        filed = self.add_claim(statement="收入 1,000 百万美元（10-Q）。",
                               created_at="2026-09-01T00:00:00+00:00")
        news = self.add_claim(statement="媒体报道收入约 1,000 百万美元。",
                              created_at="2026-09-02T00:00:00+00:00")
        self.index(filed, importance="filing", group="revenue-q3")
        self.index(news, importance="news", group="revenue-q3")
        self.c.reply = json.dumps({"answer": "收入是 1,000 百万美元。", "citations": [],
                                   "confidence": "medium", "gaps": []})
        done = self.c.wait(self.plane.ask(self.login, {
            "question": "ACN 的收入是多少？", "request_id": "int1-ask"})["job_id"])
        self.assertEqual(done["status"], "done", done["error"])
        result = done["result"]
        self.assertGreaterEqual(result["duplicates_dropped"], 1)
        self.assertEqual(result["claims_considered"],
                         result["claims_total"] - result["duplicates_dropped"])
        # Q1: an answer is something the PM can give a verdict on, and the
        # thing that verdict binds to is a hash of what was said.
        self.assertEqual(result["feedback"]["target_kind"], "ask_answer")
        self.assertEqual(result["feedback"]["target_ref"], "cockpit-ask:int1-ask")
        self.assertTrue(result["feedback"]["target_hash"])


class PageVocabularyTests(unittest.TestCase):
    """ADR-0006: the page says these things in words, not in state names."""

    PAGE = Path(__file__).resolve().parents[1] / "src" / "dalton_core" / "cockpit_control.html"

    def test_the_three_ways_of_being_quiet_have_their_own_words(self) -> None:
        page = self.PAGE.read_text(encoding="utf-8")
        for status, words in (("ungranted", "缺授权"), ("unconfigured", "还没装上"),
                              ("unapproved", "等你批准"), ("unstarted", "还没跑过")):
            self.assertIn(f"{status}:", page)
            self.assertIn(words, page)

    def test_the_five_feedback_buttons_are_the_five_verdicts(self) -> None:
        page = self.PAGE.read_text(encoding="utf-8")
        for verdict, label in (("read", "读过"), ("useful", "有用"),
                               ("needs_more_evidence", "证据不够"),
                               ("disagree", "不同意"), ("revise", "要重写")):
            self.assertIn(f"{verdict}:\"{label}\"", page)
        self.assertIn("/v1/cockpit/feedback", page)

    def test_the_page_asks_for_the_new_routes(self) -> None:
        page = self.PAGE.read_text(encoding="utf-8")
        self.assertIn("/v1/cockpit/claims?", page)
        self.assertIn("/v1/cockpit/model?company=", page)

    def test_the_price_block_says_when_it_is_not_a_close(self) -> None:
        self.assertIn("盘中价，当天还没收盘", self.PAGE.read_text(encoding="utf-8"))


class RouteTests(unittest.TestCase):
    """The three new /v1/cockpit/* routes, and how they read a query string."""

    def application(self, plane):
        from dalton_core.agenda_control import AgendaControlApplication

        return AgendaControlApplication(None, None, cockpit_plane=plane)

    def test_the_claims_route_passes_the_three_filters_through(self) -> None:
        class Plane:
            def claims(self, **kwargs):
                self.seen = kwargs
                return {"items": []}

        plane = Plane()
        value = self.application(plane).cockpit_view(
            "/v1/cockpit/claims", OWNER_LOGIN,
            {"company": ACN, "aspect": "demand_drivers", "importance": "filing",
             "canonical": "0", "limit": "50"})
        self.assertTrue(value["enabled"])
        self.assertEqual(plane.seen, {
            "company_ref": ACN, "index_aspect": "demand_drivers",
            "importance": "filing", "canonical_only": False, "limit": 50})

    def test_canonical_only_is_on_unless_it_is_turned_off(self) -> None:
        class Plane:
            def claims(self, **kwargs):
                self.seen = kwargs
                return {}

        plane = Plane()
        self.application(plane).cockpit_view("/v1/cockpit/claims", OWNER_LOGIN, {})
        self.assertTrue(plane.seen["canonical_only"])
        self.assertIsNone(plane.seen["company_ref"])

    def test_the_model_route_names_its_company(self) -> None:
        class Plane:
            def company_model(self, company_ref):
                self.seen = company_ref
                return {"company_ref": company_ref}

        plane = Plane()
        self.application(plane).cockpit_view(
            "/v1/cockpit/model", OWNER_LOGIN, {"company": ACN})
        self.assertEqual(plane.seen, ACN)

    def test_the_feedback_route_needs_the_csrf_token(self) -> None:
        from types import SimpleNamespace

        class Plane:
            def record_feedback(self, login, value):
                self.seen = (login, value)
                return {"status": "fresh"}

        plane = Plane()
        app = self.application(plane)
        session = SimpleNamespace(csrf="token")
        body = json.dumps({"target_ref": "x", "verdict": "read"}).encode()
        with self.assertRaises(PermissionError):
            app.post_cockpit("feedback", OWNER_LOGIN, session, "wrong", body)
        app.post_cockpit("feedback", OWNER_LOGIN, session, "token", body)
        self.assertEqual(plane.seen[0], OWNER_LOGIN)
        self.assertEqual(plane.seen[1]["verdict"], "read")


if __name__ == "__main__":
    unittest.main()

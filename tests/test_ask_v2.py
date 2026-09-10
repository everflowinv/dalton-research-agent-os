"""P15a: ask v2 -- the context, the answer's shape, and the one bounded look.

Three things are under test and they are deliberately separate files' worth of
concern held in one: what the assembler shows a question (and what it says when
it has nothing to show), what the answer is allowed to come back as, and what
has to be true before a question is allowed to spend a connector call.

The Cores here come in two states on purpose.  One is the Core the cockpit
harness builds -- no price series, no dossier, no debate map, none of Wave 1 or
2 -- which is the Core the live deployment still is as of 2026-09-09.  The
other has every authority on it.  An answer panel that only works on the second
one would be a panel that does not work.
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core import ask_answer, ask_context, ask_refresh
from dalton_core.analyst_journal import AnalystJournalAuthority
from dalton_core.catalyst_calendar import CatalystCalendarAuthority
from dalton_core.company_dossier import CompanyDossierAuthority
from dalton_core.debate_map import DebateMapAuthority
from dalton_core.event_judgement import EventJudgementAuthority
from dalton_core.market_price import MarketPriceSeriesAuthority
from dalton_core.model_forecast_driver import ForecastModelAuthority
from dalton_core.research_event import ResearchEventAuthority
from dalton_core.valuation_snapshot import ValuationSnapshotAuthority
from tests.test_catalyst_calendar import entry as catalyst_entry
from tests.test_cockpit_plane import CockpitHarness
from tests.test_company_dossier import (
    body as dossier_body,
    drafted as drafted_section,
)
from tests.test_debate_map import debate as debate_row
from tests.test_model_forecast_driver import model as forecast_body
from tests.test_valuation_snapshot import history as price_history, roles

ACN = "company:sec-cik:0001467373"
EPAM = "company:sec-cik:0001352010"
OWNER = "human:lumos"
OWNER_LOGIN = "owner@example.com"
AUTOMATION = "automation:coverage-mission"
TODAY = "2026-09-09"
INVOCATION = "connector-invocation:yfinance:" + "a" * 32
GOVERNANCE = "connector-governance:yfinance-daily-prices:v1"

# The ten questions the roadmap's acceptance test is written against, in the
# words a PM uses.  They are here rather than in the report alone because the
# classifier is a contract: a question the panel reads as the wrong kind gets
# the wrong blocks and, for the two view questions, stops being required to say
# where we differ from the market.
PM_QUESTIONS: tuple[tuple[str, str], ...] = (
    ("你怎么看 ACN 现在的位置？", "view"),
    ("ACN 的估值现在贵不贵？", "valuation"),
    ("EPAM 下个季度的收入增速你预测是多少？", "outlook"),
    ("ACN 上季度的新签订单是多少？", "fact"),
    ("市场对 IT services 的需求分歧在哪？", "debate"),
    ("ACN 下次财报是什么时候？", "catalyst"),
    ("GCC 自建团队对 ACN 有什么影响？", "event_impact"),
    ("EPAM 的利用率最近怎么样？", "fact"),
    ("我们和市场在 ACN 上的看法差在哪？", "view"),
    ("这个行业现在处在周期的什么位置？", "other"),
)


def bar(date: str, close: str) -> dict:
    return {"date": date, "open": close, "high": close, "low": close,
            "close": close, "adj_close": close, "volume": "1000000"}


def members() -> dict:
    return {ACN: {"company_ref": ACN, "ticker": "ACN"},
            EPAM: {"company_ref": EPAM, "ticker": "EPAM"}}


def names() -> dict:
    return {"ACN": "Accenture", "EPAM": "EPAM"}


def label(ref: str) -> str:
    return {ACN: "ACN", EPAM: "EPAM"}.get(ref, ref)


def mission() -> dict:
    return {
        "id": "mission-version:1", "title": "US IT services",
        "objective": "弄清 AI 对定价权的影响。",
        "research_questions": ["合同结构如何变化？"],
        "industry_ref": "industry:us-it-services",
        "universe": list(members().values()),
        "autonomy": {"may_write": ["claim", "research_task"]},
        "budget": {"max_daily_cost_usd": "20"},
        "source_plan": [
            {"source_ref": "source:alphaengine", "status": "connected"},
            {"source_ref": "source:web-search", "status": "connected"},
        ],
    }


def claim_row(statement: str, *, ref: str, subject: str = ACN, aspect: str = "demand_drivers",
              importance: str = "filing", at: str = "2026-09-01T00:00:00+00:00",
              period: str = "2026Q3", value=None) -> dict:
    return {
        "ref": ref, "claim_ref": ref.replace("claim-version:", "claim:"),
        "subject_ref": subject, "statement": statement, "period": period,
        "aspect": "revenue", "index_aspect": aspect, "importance": importance,
        "basis": "fixture", "kind": "qualitative", "value": value, "unit": None,
        "status": "corroborated", "created_at": at, "actor_ref": AUTOMATION,
    }


def claims(count: int = 6) -> list[dict]:
    rows = []
    for index in range(count):
        rows.append(claim_row(
            f"Accenture 的第 {index} 条结论，客户预算在恢复。",
            ref=f"claim-version:{index:064d}",
            aspect="demand_drivers" if index % 2 else "competitive_position",
            at=f"2026-09-{index + 1:02d}T00:00:00+00:00"))
    rows.append(claim_row("EPAM 的利用率同比小幅上升。", ref="claim-version:" + "e" * 64,
                          subject=EPAM, aspect="supply_and_cost"))
    return rows


def context_for(question: str, core, *, claim_rows=None, theses=(), budget=90_000,
                consensus_reader=None) -> dict:
    return ask_context.build_context(
        core, question=question, mission=mission(), members=members(),
        claims=claims() if claim_rows is None else claim_rows, theses=theses,
        company_names=names(), label=label, today=TODAY, budget_chars=budget,
        consensus_reader=consensus_reader)


def empty_core() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    return connection


class QuestionResolutionTests(unittest.TestCase):
    def test_the_ten_pm_questions_are_read_as_the_kinds_they_are(self):
        for question, expected in PM_QUESTIONS:
            with self.subTest(question=question):
                self.assertEqual(ask_context.question_kind(question)["kind"], expected)

    def test_a_kind_always_carries_the_word_that_decided_it(self):
        for question, expected in PM_QUESTIONS:
            found = ask_context.question_kind(question)
            with self.subTest(question=question):
                if expected == "other":
                    self.assertIsNone(found["matched"])
                else:
                    self.assertIn(found["matched"], question.lower())

    def test_only_view_and_debate_questions_demand_a_variant_view(self):
        # The owner's rule: agreeing with the market is worth nothing. It is
        # enforced on the two kinds where a view is what was asked for, and
        # not on "how much was revenue", where it would be padding.
        self.assertEqual(ask_context.VIEW_KINDS, frozenset({"view", "debate"}))
        for question, expected in PM_QUESTIONS:
            with self.subTest(question=question):
                wants = context_for(question, empty_core())["wants_market_vs_us"]
                self.assertEqual(wants, expected in {"view", "debate"})

    def test_a_named_ticker_narrows_the_subjects_and_silence_takes_the_universe(self):
        named = ask_context.resolve_subjects("ACN 的估值贵吗", members(), company_names=names())
        self.assertEqual((named["companies"], named["scope"]), ([ACN], "named"))
        by_name = ask_context.resolve_subjects("怎么看 Accenture", members(), company_names=names())
        self.assertEqual(by_name["companies"], [ACN])
        silent = ask_context.resolve_subjects("行业需求怎么样", members(), company_names=names())
        self.assertEqual((silent["companies"], silent["scope"]), ([], "universe"))
        self.assertTrue(silent["industry"])

    def test_every_block_has_a_label_and_a_tag_letter_of_its_own(self):
        self.assertEqual(set(ask_context.BLOCK_LABELS), set(ask_context.BLOCK_TAGS))
        letters = list(ask_context.BLOCK_TAGS.values())
        self.assertEqual(len(letters), len(set(letters)))
        for name in (*ask_context.BLOCK_PRIORITY, "claims"):
            self.assertIn(name, ask_context.BLOCK_LABELS)


class ContextOnAnOldCoreTests(unittest.TestCase):
    """A Core with none of the Wave 1/2 authorities -- which live still is."""

    def test_every_absent_block_says_which_kind_of_absence_it_is(self):
        context = context_for("怎么看 ACN 的估值", empty_core())
        reasons = {item["block"]: item["reason"] for item in context["missing"]}
        for block in ("dossier", "debates", "forecast", "valuation", "market_price",
                      "catalyst", "events", "judgements", "reflections", "journal"):
            self.assertEqual(reasons[block], "no_authority_on_this_core", block)
        # Not the same sentence: the consensus authority is on a branch that
        # has not merged, and "we never built it" is not "this Core lacks it".
        self.assertEqual(reasons["consensus"], "reader_not_available")

    def test_the_claims_still_arrive_and_are_the_only_block_that_does(self):
        context = context_for("ACN 上季度的收入是多少", empty_core())
        available = [b["block"] for b in context["blocks"] if b["available"]]
        self.assertEqual(available, ["claims"])
        self.assertTrue(all(row["tag"].startswith("C")
                            for row in context["shown"]))

    def test_a_question_naming_nobody_marks_the_per_company_blocks_as_such(self):
        core = empty_core()
        core.executescript(
            "CREATE TABLE company_dossier_versions (company_ref TEXT, version_number INT,"
            " record_json TEXT);")
        context = context_for("行业现在处在周期什么位置", core)
        reasons = {item["block"]: item["reason"] for item in context["missing"]}
        # The table is here and the question named nobody; those are different
        # absences and the refresh decision is made from the difference.
        self.assertEqual(reasons["dossier"], "no_record_for_this_company")


class ContextOnAFullCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.c = CockpitHarness(self.root)
        self.addCleanup(self.c.close)
        self.store = self.c.h.h.core
        self.publish_everything()

    def publish_everything(self) -> None:
        MarketPriceSeriesAuthority(self.store).publish_series(
            company_ref=ACN, ticker="ACN", currency="USD",
            bars=[bar("2026-09-10", "100"), bar("2026-09-11", "110")],
            observations=[], invocation_ref=INVOCATION, artifact_hash="1" * 64,
            governance_ref=GOVERNANCE, governance_hash="2" * 64,
            requested_start="2026-09-10", requested_end="2026-09-11",
            captured_at="2026-09-11T23:30:00+00:00")
        binding = {"version_ref": "market-price-series-version:" + "a" * 32,
                   "version_hash": "b" * 64,
                   "invocation_ref": "connector-invocation:yfinance:" + "c" * 32,
                   "artifact_hash": "d" * 64}
        ValuationSnapshotAuthority(self.store).publish_snapshot(
            company_ref=ACN,
            price={**binding, "bar_date": "2026-09-08", "close": "100", "currency": "USD"},
            shares={**binding, "as_of": "2026-09-08", "shares_outstanding": "1000000"},
            fundamental_windows=[{"as_of": "2025-12-31", "roles": roles()}],
            price_history=price_history())
        ForecastModelAuthority(self.store).publish(forecast_body())
        CompanyDossierAuthority(self.store).publish(dossier_body(
            drafted_sections={
                # Not ``demand_drivers`` or ``supply_and_cost``: those two take
                # their slot structure from the Constitution's causal chain, so
                # a fixture that writes its own is refused by the authority.
                "business_model": drafted_section(
                    "business_model", "claim-version:" + "1" * 64,
                    "客户预算在恢复，管理层在电话会上说过两次。"),
                "competitive_position": drafted_section(
                    "competitive_position", "claim-version:" + "2" * 64,
                    "GCC 自建团队是最主要的竞争威胁。"),
            },
            # ``variant()`` is left at its unavailable default: P12a's
            # ``validate_variant_view`` drops ``gaps`` from a *drafted* block,
            # so a drafted variant view fails the authority's own body-hash
            # check and cannot be published at all today. See the report; the
            # reader below is tested against the shape P12a will produce.
            company_ref=ACN))
        DebateMapAuthority(self.store).publish_map(
            subject_ref=ACN, subject_kind="company", change_reason="evidence_thicker",
            change_evidence_refs=["cv-a"],
            constitution_ref="constitution-version:us-it-services:1",
            constitution_hash="c" * 64, evidence_fingerprint="f" * 64,
            debates=[debate_row()], actor_ref=AUTOMATION,
            created_at="2026-09-09T00:00:00+00:00")
        CatalystCalendarAuthority(self.store).publish(
            company_ref=ACN, entries=[catalyst_entry("2026-10-01")],
            change_reason="evidence_thicker", evidence_refs=["evidence:one"],
            now="2026-09-09")
        # Written where the lane writes it, through the store's own
        # transaction: ``record_event`` would need the live mission to grant
        # ``market_event`` to automation, which it does not, and loosening the
        # fixture mission would be testing a grant rather than a reader.
        ResearchEventAuthority(self.store)
        self.insert_event()
        AnalystJournalAuthority(self.store).add(
            target_ref="cockpit-ask:earlier", target_hash="9" * 64,
            target_kind="ask_answer", verdict="needs_more_evidence",
            actor_ref=OWNER, company_ref=ACN, note="缺 bookings")
        self.insert_judgement_and_reflection()

    def insert_event(self) -> None:
        record = {
            "schema_version": "0.1", "id": "research-event:one", "company_ref": ACN,
            "kind": "rating_change", "occurred_at": "2026-09-08T12:00:00+00:00",
            "evidence_tier": "sell_side", "source_refs": ["document:one"],
            "payload": {"document_ref": "document:one", "source_ref": "source:alphaengine",
                        "broker": "TD", "from_rating": "hold", "to_rating": "buy",
                        "price_target": "400", "title": "TD 把 ACN 上调到买入"},
        }
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO research_events(event_id,company_ref,kind,occurred_at,"
                "evidence_tier,payload_hash,mission_version_ref,record_json,content_hash,"
                "actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("research-event:one", ACN, "rating_change", "2026-09-08T12:00:00+00:00",
                 "sell_side", "d" * 64, "mission-version:1",
                 json.dumps(record, ensure_ascii=False), "e" * 64, AUTOMATION,
                 "2026-09-08T12:00:00+00:00"))
        self.event = record

    def insert_judgement_and_reflection(self) -> None:
        """Two rows written where their lane writes them.

        Inserted through the store's own transaction rather than through the
        judgement lane, which needs a model call to reach these tables; the
        shape is the lane's, taken from its schema.
        """

        judgement = {
            "decision": "THESIS_STRENGTHENED", "action": "note",
            "because": "评级上调与我们的判断同向。", "driver_refs": ["driver:bookings"],
            "thesis_refs": [], "citations": [],
        }
        reflection = {
            "what_we_expected": "需求在二季度见底",
            "what_happened": "预算恢复慢了一个季度", "why": "我们低估了审批链条的长度",
            "missed_debates": [{"question": "审批周期是否结构性变长", "refs": []}],
            "followup_research": [{"question": "问客户方 CIO 审批周期", "wants": "expert"}],
            "followup_tracking": [],
        }
        EventJudgementAuthority(self.store)
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO event_judgements(judgement_id,event_ref,event_hash,company_ref,"
                "decision,action,verdict,cost_micros,mission_version_ref,record_json,"
                "content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("event-judgement:one", "research-event:one", "a" * 64, ACN,
                 "THESIS_STRENGTHENED", "note", "verified", 0, "mission-version:1",
                 json.dumps(judgement, ensure_ascii=False), "b" * 64, AUTOMATION,
                 "2026-09-08T13:00:00+00:00"))
            cur.execute(
                "INSERT INTO thesis_reflections(reflection_id,judgement_ref,trigger_event_ref,"
                "trigger_event_hash,trigger_kind,company_ref,verdict,cost_micros,"
                "mission_version_ref,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("thesis-reflection:one", "event-judgement:one", "research-event:one",
                 "a" * 64, "price_divergence", ACN, "verified", 0, "mission-version:1",
                 json.dumps(reflection, ensure_ascii=False), "c" * 64, AUTOMATION,
                 "2026-09-08T14:00:00+00:00"))

    def context(self, question: str, **kwargs) -> dict:
        with self.c.plane._core() as core:
            return context_for(question, core, **kwargs)

    def test_a_view_question_reaches_every_authority_on_this_core(self):
        context = self.context("你怎么看 ACN？")
        available = {b["block"] for b in context["blocks"] if b["available"]}
        for block in ("claims", "dossier", "debates", "forecast", "valuation",
                      "market_price", "catalyst", "events", "judgements",
                      "reflections", "journal"):
            self.assertIn(block, available, block)
        self.assertTrue(context["wants_market_vs_us"])

    def test_every_shown_row_carries_a_tag_and_the_ref_behind_it(self):
        context = self.context("你怎么看 ACN？")
        self.assertTrue(context["shown"])
        for row in context["shown"]:
            with self.subTest(tag=row["tag"]):
                self.assertRegex(row["tag"], r"^[A-Z]\d+$")
                self.assertTrue(row["ref"], row)
                self.assertTrue(row["statement"])
        letters = {row["tag"][0] for row in context["shown"]}
        self.assertLessEqual({"C", "D", "B", "V", "P"}, letters)

    def test_the_dossier_block_shows_the_sections_that_were_drafted(self):
        context = self.context("你怎么看 ACN？")
        dossier = next(b for b in context["blocks"] if b["block"] == "dossier")
        aspects = {row["detail"]["aspect"] for row in dossier["rows"]}
        self.assertEqual(aspects, {"business_model", "competitive_position"})
        self.assertIn("GCC", " ".join(row["text"] for row in dossier["rows"]))

    def test_the_debate_block_says_where_the_market_is_and_where_we_are(self):
        context = self.context("市场在 ACN 上的分歧是什么？")
        debates = next(b for b in context["blocks"] if b["block"] == "debates")
        row = debates["rows"][0]
        self.assertEqual(row["detail"]["market_lean"], "bear")
        self.assertEqual(row["detail"]["our_side"], "bull")

    def test_the_valuation_block_carries_the_percentile_basis_with_the_number(self):
        context = self.context("ACN 的估值贵不贵？")
        valuation = next(b for b in context["blocks"] if b["block"] == "valuation")
        self.assertTrue(valuation["rows"])
        for row in valuation["rows"]:
            if row["detail"]["status"] == "available":
                self.assertIn("percentile_basis", row["detail"])

    def test_the_question_kind_promotes_its_blocks_to_the_front_of_the_budget(self):
        # A budget that fits only a little: the valuation question keeps its
        # valuation block and drops the low-priority ones, and a plain factual
        # question spends the same budget differently.
        valuation = self.context("ACN 的估值贵不贵？", budget=2_400)
        kept = {b["block"] for b in valuation["blocks"] if b["available"]}
        self.assertIn("valuation", kept)
        dropped = {item["block"] for item in valuation["missing"]
                   if item["reason"] == "dropped_for_budget"}
        self.assertTrue(dropped)

    def test_the_claims_keep_their_floor_however_much_else_competes(self):
        context = self.context("你怎么看 ACN？", budget=2_400)
        claims_block = next(b for b in context["blocks"] if b["block"] == "claims")
        self.assertTrue(claims_block["available"])
        self.assertGreater(context["claims_considered"], 0)

    def test_the_same_question_on_the_same_core_builds_the_same_context(self):
        first = self.context("你怎么看 ACN？")
        second = self.context("你怎么看 ACN？")
        self.assertEqual(first["context_hash"], second["context_hash"])
        self.assertNotEqual(first["context_hash"],
                            self.context("ACN 的估值贵不贵？")["context_hash"])

    def test_the_rendered_prompt_stays_inside_the_budget_it_was_given(self):
        context = self.context("你怎么看 ACN？", budget=12_000)
        self.assertLessEqual(context["spent_chars"], 12_000)
        self.assertLessEqual(len(ask_context.render_context(context)), 14_000)

    def test_a_consensus_reader_that_exists_fills_the_block_it_was_missing(self):
        def reader(company_ref):
            if company_ref != ACN:
                return None
            return {"id": "consensus-version:1", "as_of": "2026-09-05",
                    "source": "yfinance",
                    "estimates": [{"metric": "revenue", "period": "2026Q4",
                                   "value": "18900", "unit": "USDm", "contributors": 14}]}

        context = self.context("ACN 下个季度收入预测是多少", consensus_reader=reader)
        consensus = next(b for b in context["blocks"] if b["block"] == "consensus")
        self.assertTrue(consensus["available"])
        self.assertIn("一致预期", consensus["rows"][0]["text"])


class AnswerShapeTests(unittest.TestCase):
    def context(self, question: str = "你怎么看 ACN？") -> dict:
        return context_for(question, empty_core())

    def reply(self, **overrides) -> dict:
        base = {
            "sentences": [{"text": "客户预算在恢复。", "refs": ["C1"]}],
            "confidence": "medium", "refused": False, "refusal_reason": None,
            "refusal_detail": None, "unknowns": [], "market_vs_us": None,
            "refresh_suggested": None,
        }
        base.update(overrides)
        return base

    def variant(self) -> dict:
        return {
            "our_view": "我们认为预算恢复快于市场预期。",
            "market_view": "卖方仍在下修二季度收入。",
            "where_market_is_wrong": "市场把审批延后当成需求消失。",
            "convergence_pathway": "十月的 bookings 会先于收入证伪或证实。",
            "observable_signals": "月度招聘与外包合同公告。",
            "refs": ["C1"],
        }

    def test_a_tag_that_was_never_shown_is_removed_and_reported(self):
        answer = ask_answer.parse_answer(
            self.reply(sentences=[{"text": "一句话。", "refs": ["C1", "Z9"]}]),
            context=self.context())
        self.assertEqual(answer["cited_tags"], ["C1"])
        check = next(c for c in answer["verification"]["checks"]
                     if c["check"] == "cites_only_shown_rows")
        self.assertEqual(check["status"], "fail")
        self.assertEqual(check["findings"][0]["tag"], "Z9")

    def test_the_body_is_assembled_from_the_rows_so_no_tag_is_ever_in_the_prose(self):
        answer = ask_answer.parse_answer(
            self.reply(sentences=[{"text": "第一句。", "refs": ["C1"]},
                                  {"text": "第二句。", "refs": []}]),
            context=self.context())
        self.assertEqual(answer["answer"], "第一句。 第二句。")
        self.assertNotIn("C1", answer["answer"])

    def test_a_confidence_outside_the_three_words_is_refused_and_read_as_low(self):
        answer = ask_answer.parse_answer(self.reply(confidence="fairly sure"),
                                         context=self.context())
        self.assertEqual(answer["confidence"], "low")
        self.assertIn("confidence_in_vocabulary", answer["verification"]["failed_checks"])

    def test_an_unknown_must_name_a_content_kind_and_a_source_that_yields_it(self):
        good = ask_answer.parse_answer(self.reply(unknowns=[
            {"what": "缺最近四个季度的 bookings", "content_kind": "sell_side_report",
             "source": "alphaengine"}]), context=self.context())
        self.assertEqual(good["unknowns"][0]["source"], "alphaengine")
        self.assertNotIn("unknowns_typed", good["verification"]["failed_checks"])
        # A source that cannot hold that content kind is the failure this
        # check exists for: yfinance has prices, not broker reports.
        bad = ask_answer.parse_answer(self.reply(unknowns=[
            {"what": "缺 bookings", "content_kind": "sell_side_report",
             "source": "yfinance"}]), context=self.context())
        self.assertEqual(bad["unknowns"], [])
        self.assertIn("unknowns_typed", bad["verification"]["failed_checks"])
        self.assertEqual(bad["verification"]["findings"][0]["code"],
                         "source_cannot_answer_content_kind")

    def test_an_untyped_gap_sentence_no_longer_counts_as_an_unknown(self):
        answer = ask_answer.parse_answer(self.reply(unknowns=["需要更多数据"]),
                                         context=self.context())
        self.assertEqual(answer["gaps"], [])
        self.assertIn("unknowns_typed", answer["verification"]["failed_checks"])

    def test_a_view_question_without_a_variant_view_is_marked_as_missing_one(self):
        answer = ask_answer.parse_answer(self.reply(), context=self.context())
        self.assertIsNone(answer["market_vs_us"])
        self.assertIn("market_vs_us_present", answer["verification"]["failed_checks"])

    def test_a_variant_view_uses_the_dossier_slots_and_reaches_the_body(self):
        answer = ask_answer.parse_answer(self.reply(market_vs_us=self.variant()),
                                         context=self.context())
        self.assertEqual(set(answer["market_vs_us"]) - {"refs"},
                         set(ask_answer.VARIANT_SLOTS)
                         if hasattr(ask_answer, "VARIANT_SLOTS")
                         else set(answer["market_vs_us"]) - {"refs"})
        self.assertIn("市场向我们靠拢的路径", answer["answer"])
        self.assertNotIn("market_vs_us_present", answer["verification"]["failed_checks"])

    def test_a_factual_question_is_not_asked_for_a_variant_view(self):
        answer = ask_answer.parse_answer(
            self.reply(), context=self.context("ACN 上季度收入是多少？"))
        check = next(c for c in answer["verification"]["checks"]
                     if c["check"] == "market_vs_us_present")
        self.assertEqual(check["status"], "skipped")

    def test_a_refusal_has_to_say_which_kind_of_refusal_it_is(self):
        bare = ask_answer.parse_answer(self.reply(refused=True), context=self.context())
        self.assertIn("refusal_states_why", bare["verification"]["failed_checks"])
        said = ask_answer.parse_answer(
            self.reply(refused=True, refusal_reason="needs_refresh",
                       refusal_detail="账本里没有 bookings"),
            context=self.context())
        self.assertEqual(said["refusal_reason"], "needs_refresh")
        self.assertTrue(said["refusal_label"])
        self.assertNotIn("refusal_states_why", said["verification"]["failed_checks"])

    def test_a_sentence_with_a_number_and_no_ref_is_a_failed_check(self):
        answer = ask_answer.parse_answer(
            self.reply(sentences=[{"text": "收入增长了 5.6%。", "refs": []}]),
            context=self.context())
        self.assertIn("numeric_sentences_cite", answer["verification"]["failed_checks"])

    def test_a_refresh_suggestion_is_checked_against_the_capability_map(self):
        good = ask_answer.parse_answer(self.reply(refresh_suggested={
            "content_kind": "sell_side_report", "source": "alphaengine",
            "query": "ACN bookings"}), context=self.context())
        self.assertEqual(good["refresh_suggested"]["source"], "alphaengine")
        bad = ask_answer.parse_answer(self.reply(refresh_suggested={
            "content_kind": "sell_side_report", "source": "sec",
            "query": "ACN bookings"}), context=self.context())
        self.assertIsNone(bad["refresh_suggested"])
        self.assertIn("refresh_names_a_capable_source",
                      bad["verification"]["failed_checks"])

    def test_a_reply_that_is_not_an_object_is_the_one_thing_that_raises(self):
        with self.assertRaises(ask_answer.AskAnswerError):
            ask_answer.parse_answer("not an object", context=self.context())

    def test_the_prompt_names_every_content_kind_and_its_sources(self):
        prompt = ask_answer.build_prompt(self.context(), mission=mission())
        self.assertIn("sell_side_report", prompt)
        self.assertIn("alphaengine", prompt)
        self.assertIn("market_vs_us", prompt)
        # A source the mission has not connected is marked as such, so a
        # suggestion the owner cannot act on is at least labelled.
        self.assertIn("[undeclared]", prompt)


class RefreshGrantTests(unittest.TestCase):
    def policy(self, *, enabled=True, units=1, rounds=1) -> dict:
        return {"adhoc_research_route": {"enabled": enabled, "max_cost_units": units,
                                         "max_rounds": rounds}}

    def specs(self) -> list[dict]:
        return [{"source_ref": "source:alphaengine", "spec_ref": "sell-side-reports",
                 "company_ref": ACN, "last_used": "2026-09-01T00:00:00+00:00", "runs": 3}]

    def test_a_granted_mission_with_a_budget_and_a_used_spec_may_look_once(self):
        decision = ask_refresh.grant(
            mission(), policy=self.policy(), slug="alphaengine", specs=self.specs(),
            company_ref=ACN)
        self.assertTrue(decision["granted"], decision["reasons"])
        self.assertEqual(decision["spec_ref"], "sell-side-reports")
        self.assertEqual(decision["budget"]["cost_units_this_refresh"], 1)

    def test_every_reason_it_cannot_is_reported_at_once(self):
        ungranted = {**mission(), "autonomy": {"may_write": ["claim"]}}
        decision = ask_refresh.grant(
            ungranted, policy=self.policy(enabled=False), slug="sec", specs=(),
            company_ref=None)
        self.assertFalse(decision["granted"])
        self.assertEqual(set(decision["reasons"]), {
            "mission_does_not_grant_research_task", "adhoc_route_disabled",
            "source_not_searchable", "company_not_resolved"})
        self.assertTrue(all(decision["reason_labels"]))

    def test_a_source_the_mission_never_connected_is_refused(self):
        unconnected = {**mission(), "source_plan": [
            {"source_ref": "source:alphaengine", "status": "not_connected"}]}
        decision = ask_refresh.grant(
            unconnected, policy=self.policy(), slug="alphaengine", specs=self.specs(),
            company_ref=ACN)
        self.assertIn("source_not_connected", decision["reasons"])

    def test_a_source_with_no_spec_this_mission_ever_ran_is_refused(self):
        decision = ask_refresh.grant(
            mission(), policy=self.policy(), slug="alphaengine", specs=(),
            company_ref=ACN)
        self.assertIn("no_discovery_spec_used_yet", decision["reasons"])

    def test_the_second_refresh_of_one_question_is_refused_by_name(self):
        decision = ask_refresh.grant(
            mission(), policy=self.policy(), slug="alphaengine", specs=self.specs(),
            company_ref=ACN, already_refreshed=True)
        self.assertEqual(decision["reasons"], ["already_refreshed"])

    def test_every_reason_word_has_a_sentence_the_owner_can_read(self):
        self.assertEqual(set(ask_refresh.GRANT_REASONS),
                         set(ask_refresh.GRANT_LABELS))


class RefreshRunTests(unittest.TestCase):
    def setUp(self):
        self.context = context_for("ACN 上季度的新签订单是多少？", empty_core())
        self.answer = ask_answer.parse_answer({
            "sentences": [{"text": "账本里没有 bookings。", "refs": []}],
            "confidence": "low", "refused": False, "refusal_reason": None,
            "refusal_detail": None,
            "unknowns": [{"what": "缺 bookings", "content_kind": "sell_side_report",
                          "source": "alphaengine"}],
            "market_vs_us": None,
            "refresh_suggested": {"content_kind": "sell_side_report",
                                  "source": "alphaengine", "query": "ACN bookings"},
        }, context=self.context)
        self.policy = {"adhoc_research_route": {
            "enabled": True, "max_cost_units": 1, "max_rounds": 1}}
        self.specs = [{"source_ref": "source:alphaengine", "spec_ref": "sell-side-reports",
                       "company_ref": ACN, "last_used": "2026-09-01T00:00:00+00:00",
                       "runs": 3}]

    def plan(self, **kwargs):
        return ask_refresh.plan_refresh(
            self.answer, self.context, mission=kwargs.pop("mission", mission()),
            policy=kwargs.pop("policy", self.policy),
            specs=kwargs.pop("specs", self.specs), **kwargs)

    def test_the_plan_names_the_owners_own_spec_and_never_a_model_written_query(self):
        plan = self.plan()
        self.assertTrue(plan["available"])
        self.assertEqual(plan["operation"]["operation"], "run_mission_source_discovery")
        self.assertEqual(plan["operation"]["params"]["spec_ref"], "sell-side-reports")
        self.assertNotIn("query", plan["operation"]["params"])
        # The model's words survive as a record of what it wanted, beside the
        # search that was actually run.
        self.assertEqual(plan["suggestion"]["query_intent"], "ACN bookings")

    def test_an_answer_that_asked_for_nothing_plans_nothing(self):
        answer = {**self.answer, "refresh_suggested": None}
        plan = ask_refresh.plan_refresh(answer, self.context, mission=mission(),
                                        policy=self.policy, specs=self.specs)
        self.assertEqual(plan["reasons"], ["answer_suggested_no_refresh"])
        self.assertIsNone(plan["grant"])

    def test_a_plan_that_is_not_available_refuses_to_run_rather_than_doing_nothing(self):
        plan = self.plan(already_refreshed=True)
        with self.assertRaises(ask_refresh.AskRefreshError):
            ask_refresh.run_refresh(plan, search=lambda **_: {"documents": []})

    def test_one_search_returns_headers_and_says_nobody_has_read_them(self):
        calls = []

        def search(**params):
            calls.append(params)
            return {"ticket_ref": "ticket:1", "documents": [
                {"document_ref": "alphaengine-doc:1", "host": "alphaengine",
                 "status": "discovered", "created_at": "2026-09-09T00:00:00+00:00"}],
                "titles": {"alphaengine-doc:1": {"title": "ACN Q4 preview", "url": None}}}

        outcome = ask_refresh.run_refresh(self.plan(), search=search)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["spec_ref"], "sell-side-reports")
        self.assertEqual(outcome["headers"][0]["title"], "ACN Q4 preview")
        self.assertIn("正文还没有被读", outcome["note"])
        # Headers, not bodies: there is no field here that could carry one.
        self.assertNotIn("body", outcome["headers"][0])
        self.assertNotIn("text", outcome["headers"][0])

    def test_the_second_pass_sees_the_headers_as_their_own_tagged_block(self):
        headers = [{"document_ref": "alphaengine-doc:1", "title": "ACN Q4 preview",
                    "host": "alphaengine", "source_ref": "source:alphaengine",
                    "discovered_at": "2026-09-09T00:00:00+00:00"}]
        again = ask_refresh.with_headers(self.context, headers)
        self.assertTrue(again["refreshed"])
        block = again["blocks"][-1]
        self.assertEqual(block["block"], "refreshed")
        self.assertEqual(block["rows"][0]["tag"], "H1")
        self.assertIn("H1", {row["tag"] for row in again["shown"]})
        prompt = ask_answer.build_prompt(again, mission=mission())
        self.assertIn("ACN Q4 preview", prompt)
        self.assertIn("只有标题", prompt)


class KnownSpecsTests(unittest.TestCase):
    def test_a_core_without_the_discovery_table_offers_no_spec(self):
        self.assertEqual(ask_refresh.known_specs(empty_core(), "coverage-mission:x"), [])

    def test_the_specs_offered_are_the_ones_this_mission_actually_ran(self):
        core = empty_core()
        core.executescript(
            "CREATE TABLE coverage_mission_source_discoveries ("
            "mission_version_ref TEXT, source_ref TEXT, spec_ref TEXT,"
            " company_ref TEXT, created_at TEXT);"
            "CREATE TABLE coverage_mission_versions ("
            "mission_version_id TEXT, mission_ref TEXT);")
        core.executemany(
            "INSERT INTO coverage_mission_versions VALUES(?,?)",
            [("mission-version:1", "coverage-mission:x"),
             ("mission-version:2", "coverage-mission:x"),
             ("mission-version:9", "coverage-mission:other")])
        core.executemany(
            "INSERT INTO coverage_mission_source_discoveries VALUES(?,?,?,?,?)",
            [("mission-version:1", "source:alphaengine", "sell-side-reports", ACN,
              "2026-09-01T00:00:00+00:00"),
             ("mission-version:2", "source:alphaengine", "earnings-call-transcripts", ACN,
              "2026-09-05T00:00:00+00:00"),
             ("mission-version:9", "source:web-search", "industry-demand", ACN,
              "2026-09-06T00:00:00+00:00")])
        specs = ask_refresh.known_specs(core, "coverage-mission:x")
        # Both versions of this mission, and neither of anyone else's: a new
        # mission version is exactly when the owner grants the refresh, and a
        # reader keyed on the version would answer "no spec" on that day.
        self.assertEqual([item["spec_ref"] for item in specs],
                         ["earnings-call-transcripts", "sell-side-reports"])


class AdhocRouteFlagTests(unittest.TestCase):
    def test_the_route_flag_is_now_two_owner_acts_rather_than_a_constant(self):
        from dalton_core.answer_routing import adhoc_route_available

        policy = {"adhoc_research_route": {"enabled": True, "max_cost_units": 2,
                                           "max_rounds": 1}}
        self.assertTrue(adhoc_route_available(policy, mission()))
        self.assertFalse(adhoc_route_available(None, mission()))
        self.assertFalse(adhoc_route_available(policy, None))
        self.assertFalse(adhoc_route_available(
            {"adhoc_research_route": {"enabled": False, "max_cost_units": 2,
                                      "max_rounds": 1}}, mission()))
        self.assertFalse(adhoc_route_available(
            {"adhoc_research_route": {"enabled": True, "max_cost_units": 0,
                                      "max_rounds": 1}}, mission()))
        self.assertFalse(adhoc_route_available(
            policy, {**mission(), "autonomy": {"may_write": ["claim"]}}))


if __name__ == "__main__":
    unittest.main()

class DossierVariantReaderTests(unittest.TestCase):
    """The reader for a drafted variant view, against the shape P12a defines.

    Written against a hand-made row rather than a published version because
    P12a cannot publish one today: ``validate_variant_view`` returns the
    drafted block without its ``gaps`` key, so ``body_hash`` never matches and
    the authority refuses its own record. The reader is ready for the day that
    is fixed; the report names it.
    """

    def core(self, variant: dict) -> sqlite3.Connection:
        core = empty_core()
        core.executescript(
            "CREATE TABLE company_dossier_versions (company_ref TEXT,"
            " version_number INT, record_json TEXT);")
        record = {
            "id": "company-dossier-version:acn:2", "version": 2, "company_ref": ACN,
            "sections": [{"aspect": "business_model", "status": "drafted",
                          "slots": [{"slot_id": "business_model", "sentences": [
                              {"text": "合同以时间材料为主。", "refs": ["claim-version:a"]}]}],
                          "sources": [{"ref": "claim-version:a"}], "gaps": []}],
            "variant_view": variant,
        }
        core.execute("INSERT INTO company_dossier_versions VALUES(?,?,?)",
                     (ACN, 2, json.dumps(record, ensure_ascii=False)))
        return core

    def variant(self, status: str = "drafted") -> dict:
        if status != "drafted":
            return {"status": "unavailable", "reason": "not_drafted_this_run",
                    "market_view_available": False, "market_view_reason": None,
                    "structure": [], "slots": [], "sources": []}
        return {
            "status": "drafted", "reason": None, "market_view_available": True,
            "market_view_reason": None,
            "structure": ["our_view", "market_view", "where_market_is_wrong",
                          "convergence_pathway", "observable_signals"],
            "slots": [{"slot_id": slot, "sentences": [{"text": f"{slot} 的一句话。",
                                                       "refs": ["claim-version:a"]}]}
                      for slot in ("our_view", "market_view", "where_market_is_wrong",
                                   "convergence_pathway", "observable_signals")],
            "sources": [{"ref": "claim-version:a"}],
        }

    def test_a_drafted_variant_view_arrives_as_its_own_row(self):
        context = context_for("你怎么看 ACN？", self.core(self.variant()))
        dossier = next(b for b in context["blocks"] if b["block"] == "dossier")
        rows = {row["detail"]["aspect"]: row for row in dossier["rows"]}
        self.assertIn("variant_view", rows)
        self.assertIn("convergence_pathway 的一句话", rows["variant_view"]["text"])
        self.assertTrue(rows["variant_view"]["detail"]["market_view_available"])

    def test_an_unavailable_variant_view_is_simply_not_shown(self):
        context = context_for("你怎么看 ACN？", self.core(self.variant("unavailable")))
        dossier = next(b for b in context["blocks"] if b["block"] == "dossier")
        self.assertNotIn("variant_view",
                         {row["detail"]["aspect"] for row in dossier["rows"]})


GOLDEN = Path(__file__).resolve().parent / "ask_v2_golden"


def golden_cases() -> list[dict]:
    found = []
    for path in sorted(GOLDEN.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["path"] = path
        found.append(payload)
    return found


class GoldenAnswerTests(unittest.TestCase):
    """Five PM questions, answered well and badly, with the checks pinned.

    Deterministic all the way down: no model is called, the replies are written
    out, and what is under test is that both verification layers say the same
    thing about the same answer every time. A check that changes its mind about
    an answer nobody edited is what a golden set exists to notice.
    """

    def test_there_are_five_cases_and_each_says_where_it_came_from(self):
        cases = golden_cases()
        self.assertEqual(len(cases), 5)
        for case in cases:
            with self.subTest(case=case["case_ref"]):
                self.assertTrue(case["source"].strip())
                self.assertTrue(case["note"].strip())
                self.assertTrue(case["expected"]["rationale"].strip())

    def test_both_good_and_bad_answers_are_represented(self):
        failing = [case for case in golden_cases()
                   if "fail" in case["expected"]["verification"].values()]
        self.assertGreaterEqual(len(failing), 2)
        self.assertLess(len(failing), len(golden_cases()))

    def context_of(self, case: dict) -> dict:
        kind = ask_context.question_kind(case["question"])["kind"]
        return {
            "question": case["question"], "shown": case["shown"],
            "question_kind": kind,
            "wants_market_vs_us": kind in ask_context.VIEW_KINDS,
        }

    def test_the_verification_layer_answers_the_same_way_every_time(self):
        for case in golden_cases():
            with self.subTest(case=case["case_ref"]):
                context = self.context_of(case)
                expected = case["expected"]
                self.assertEqual(context["question_kind"], expected["question_kind"])
                self.assertEqual(context["wants_market_vs_us"],
                                 expected["wants_market_vs_us"])
                answer = ask_answer.parse_answer(case["reply"], context=context)
                self.assertEqual(answer["confidence"], expected["confidence"])
                self.assertEqual(answer["refused"], expected["refused"])
                self.assertEqual(answer["cited_tags"], expected["cited_tags"])
                found = {check["check"]: check["status"]
                         for check in answer["verification"]["checks"]}
                self.assertEqual(found, expected["verification"])

    def test_q1s_deterministic_layer_answers_the_same_way_every_time(self):
        from dalton_core.research_quality_rubrics import rubric as get_rubric
        from dalton_core.research_quality_score import (
            artefact_from_ask_answer, run_deterministic,
        )

        rubric = get_rubric("ask_answer")
        for case in golden_cases():
            with self.subTest(case=case["case_ref"]):
                context = self.context_of(case)
                answer = ask_answer.parse_answer(case["reply"], context=context)
                artefact = artefact_from_ask_answer(
                    {**answer, "question": case["question"]},
                    shown_claims=case["shown"],
                    ref=f"cockpit-ask:golden:{case['case_ref']}")
                found = {item["check"]: item["status"]
                         for item in run_deterministic(artefact, rubric)["checks"]}
                self.assertEqual(found, case["expected"]["quality"])


class CockpitAskV2Tests(unittest.TestCase):
    """The whole path, through the plane the owner actually uses."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.c = CockpitHarness(Path(self.temp.name))
        self.addCleanup(self.c.close)
        self.login = OWNER_LOGIN
        self.searches: list[tuple[str, dict]] = []
        self.publish_policy()
        self.record_a_past_discovery()
        self.wire_governance()

    # -- what the owner has to have published for a refresh to be allowed ----

    def publish_policy(self) -> None:
        """An answer-sufficiency policy whose ad-hoc route is budgeted.

        Written straight into the two tables: publishing one properly needs a
        mandate, an agenda cycle and a bounded-planner probe template, none of
        which this test is about.
        """

        import dalton_core.answer_routing as routing

        store = self.c.h.h.core
        store.connection.create_function(
            "dalton_answer_routing_authorized", 0, lambda: 1)
        store.connection.executescript(
            (Path(routing.__file__).with_name("answer_routing_schema.sql")
             ).read_text(encoding="utf-8"))
        record = {"adhoc_research_route": {"enabled": True, "max_cost_units": 2,
                                           "max_rounds": 1}}
        with store._transaction() as cur:
            cur.execute(
                "INSERT INTO answer_sufficiency_policy_versions(version_id,policy_ref,"
                "version_number,prior_version_id,mandate_ref,mandate_version_ref,"
                "mandate_version_hash,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("answer-policy-version:1", "answer-policy:1", 1, None, "mandate:1",
                 "mandate-version:1", "a" * 64, json.dumps(record), "b" * 64,
                 OWNER, "2026-09-01T00:00:00+00:00"))
            cur.execute(
                "INSERT INTO answer_sufficiency_policy_pointer(mandate_ref,version_id,"
                "content_hash,updated_at) VALUES(?,?,?,?)",
                ("mandate:1", "answer-policy-version:1", "b" * 64,
                 "2026-09-01T00:00:00+00:00"))

    def record_a_past_discovery(self) -> None:
        """One spec this mission has run, which is the only kind it may re-run."""

        mission = self.c.h.missions.active_mission("coverage-mission:us-it-services")
        self.mission_version_ref = mission["id"]
        store = self.c.h.h.core
        store.connection.create_function(
            "dalton_coverage_mission_authorized", 0, lambda: 1)
        with store._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_source_discoveries(record_id,"
                "mission_version_ref,mission_version_hash,company_ref,source_ref,"
                "discovery_plan_ref,discovery_plan_hash,spec_ref,query_hash,"
                "connector_invocation_ref,source_envelope_ref,source_envelope_hash,"
                "actor_ref,requested_by,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("mission-source-discovery:past", mission["id"], "a" * 64, ACN,
                 "source:alphaengine", "discovery-plan:1", "b" * 64,
                 "sell-side-reports", "c" * 64, "connector-invocation:ae:1",
                 "source-envelope:1", "d" * 64, AUTOMATION, AUTOMATION, "{}",
                 "e" * 64, "2026-09-01T00:00:00+00:00"))

    def grant_research_task(self) -> None:
        """The owner's word that lifted the ad-hoc ban, as a new mission version.

        Published rather than patched: mission versions are immutable, and the
        grant this feature turns on is a versioned human act, which is the
        whole point of gating on it.
        """

        from tests.p9a_fixtures import mission_params

        params = mission_params(self.c.h.state)
        mission_ref = params.pop("mission_ref")
        current = self.c.h.missions.active_mission(mission_ref)
        params["autonomy"] = {
            **current["autonomy"],
            "may_write": sorted(set(current["autonomy"]["may_write"]) | {"research_task"}),
        }
        params["version_id"] = "coverage-mission-version:ask-v2-grant"
        params["prior_version_ref"] = current["id"]
        params["idempotency_key"] = "coverage-mission:ask-v2-grant"
        self.c.h.missions.create_mission(mission_ref, **params)
        self.mission_version_ref = self.c.h.missions.active_mission(mission_ref)["id"]

    def wire_governance(self) -> None:
        original = self.c.plane.governance_call

        def governance(token_config, socket, *, actor_ref, operation, params):
            if operation == "run_mission_source_discovery":
                self.searches.append((operation, dict(params)))
                # The second answer is a different reply; the adapter is built
                # per model call, so swapping it here lands on the second one.
                self.c.reply = self.second_reply
                return {"id": "discovery-ticket:1",
                        "parameters": {"query": "Accenture research report"}}
            if operation == "mission_discovered_documents":
                self.searches.append((operation, dict(params)))
                return {"documents": [
                    {"document_ref": "alphaengine-doc:new1", "host": "alphaengine",
                     "status": "discovered", "created_at": "2026-09-09T00:00:00+00:00",
                     "source_ref": "source:alphaengine"}]}
            return original(token_config, socket, actor_ref=actor_ref,
                            operation=operation, params=params)

        self.c.plane.governance_call = governance

    # -- replies -----------------------------------------------------------

    def view_reply(self) -> str:
        return json.dumps({
            "sentences": [{"text": "我们比市场乐观一档。", "refs": []}],
            "confidence": "medium", "refused": False, "refusal_reason": None,
            "refusal_detail": None,
            "unknowns": [{"what": "缺卖方最近的评级分布",
                          "content_kind": "sell_side_report", "source": "alphaengine"}],
            "market_vs_us": {
                "our_view": "预算恢复只是被推迟。", "market_view": "卖方在下修。",
                "where_market_is_wrong": "把签得慢当成不签。",
                "convergence_pathway": "十月的 bookings 会分岔。",
                "observable_signals": "招聘与合同披露。", "refs": []},
            "refresh_suggested": {"content_kind": "sell_side_report",
                                  "source": "alphaengine", "query": "ACN bookings"},
        }, ensure_ascii=False)

    @property
    def second_reply(self) -> str:
        return json.dumps({
            "sentences": [{"text": "补搜到一份新的卖方研报，正文还没读。", "refs": ["H1"]}],
            "confidence": "low", "refused": False, "refusal_reason": None,
            "refusal_detail": None,
            "unknowns": [{"what": "这份研报的正文还没有被抽取",
                          "content_kind": "sell_side_report", "source": "alphaengine"}],
            "market_vs_us": {
                "our_view": "维持乐观一档。", "market_view": "卖方在下修。",
                "where_market_is_wrong": "把签得慢当成不签。",
                "convergence_pathway": "读完这份研报再看。",
                "observable_signals": "十月 bookings。", "refs": ["H1"]},
            "refresh_suggested": None,
        }, ensure_ascii=False)

    def ask(self, question: str, request_id: str, **value) -> dict:
        job = self.c.plane.ask(self.login,
                               {"question": question, "request_id": request_id, **value})
        done = self.c.wait(job["job_id"], self.login)
        self.assertEqual(done["status"], "done", done["error"])
        return done["result"]

    # -- the tests ---------------------------------------------------------

    def test_a_view_question_comes_back_with_where_we_differ_from_the_market(self):
        self.c.reply = self.view_reply()
        result = self.ask("你怎么看 ACN？", "v1")
        self.assertTrue(result["context"]["wants_market_vs_us"])
        self.assertEqual(result["market_vs_us"]["convergence_pathway"],
                         "十月的 bookings 会分岔。")
        self.assertIn("市场向我们靠拢的路径", result["answer"])
        self.assertTrue(result["verification"]["passed"], result["verification"])
        self.assertEqual(result["quality"]["rubric_ref"], "rubric:ask-answer")

    def test_the_refresh_is_offered_with_every_reason_it_is_shut(self):
        self.c.reply = self.view_reply()
        result = self.ask("你怎么看 ACN？", "v2")
        self.assertFalse(result["refresh"]["available"])
        self.assertIn("mission_does_not_grant_research_task", result["refresh"]["reasons"])
        # The suggestion survives the refusal: it is the sentence that gets
        # the grant published.
        self.assertEqual(result["refresh"]["suggestion"]["source"], "alphaengine")
        self.assertEqual(result["refresh"]["suggestion"]["query_intent"], "ACN bookings")
        self.assertEqual(self.searches, [])

    def test_one_granted_refresh_searches_once_and_answers_again_from_headers(self):
        self.grant_research_task()
        self.c.reply = self.view_reply()
        result = self.ask("你怎么看 ACN？", "v3", refresh=True)
        self.assertEqual([op for op, _ in self.searches],
                         ["run_mission_source_discovery", "mission_discovered_documents"])
        # The spec is one this mission has actually run, never one invented
        # here: the harness's own discovery is the newest and wins.
        with self.c.plane._core() as core:
            known = ask_refresh.known_specs(core, "coverage-mission:us-it-services")
        self.assertEqual(self.searches[0][1]["spec_ref"], known[0]["spec_ref"])
        self.assertEqual(self.searches[0][1]["source_ref"], "source:alphaengine")
        self.assertEqual(result["refreshed_with"][0]["document_ref"], "alphaengine-doc:new1")
        self.assertIn("正文还没有被读", result["refresh_note"])
        self.assertIn("补搜到一份新的卖方研报", result["answer"])
        # The second answer cited the header it was shown, and nothing else.
        self.assertEqual([row["tag"] for row in result["citations"]], ["H1"])
        # And there is no second look, ever.
        self.assertFalse(result["refresh"]["available"])
        self.assertEqual(result["refresh"]["reasons"], ["already_refreshed"])

    def test_the_same_question_asked_again_is_refused_a_second_look(self):
        self.grant_research_task()
        self.c.reply = self.view_reply()
        self.ask("你怎么看 ACN？", "v4", refresh=True)
        before = len(self.searches)
        self.c.reply = self.view_reply()
        again = self.ask("你怎么看 ACN？", "v4", refresh=True)
        self.assertEqual(len(self.searches), before)
        self.assertIn("already_refreshed", again["refresh"]["reasons"])

    def test_a_refresh_writes_no_claim_and_no_score(self):
        self.grant_research_task()
        self.c.reply = self.view_reply()
        store = self.c.h.h.core
        before = store.connection.execute("SELECT COUNT(*) FROM claim_versions").fetchone()[0]
        self.ask("你怎么看 ACN？", "v5", refresh=True)
        after = store.connection.execute("SELECT COUNT(*) FROM claim_versions").fetchone()[0]
        self.assertEqual(before, after)
        scores = store.connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name='research_quality_score_versions'").fetchone()[0]
        if scores:
            self.assertEqual(store.connection.execute(
                "SELECT COUNT(*) FROM research_quality_score_versions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()

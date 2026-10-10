"""Batch F: result-type events judged on the same-event pack, against expectations.

The golden case is Accenture's FY26 Q4 release (8-K Item 2.02, 2026-10-01) and
its call, replayed from the real texts (``tests/fixtures/event_result/
acn_fy26q4_event.json``, read verbatim from the legacy library, nothing
anonymised): the exhibit, five passages of the call, the Goldman Sachs desk
note carrying GIR's preview (FY27 guide expected at 3%-4% CC) and the BofA
sales note of the day.  The deterministic parts -- pack, baseline, actuals,
reaction, contract checks, summaries, re-judgement bounds -- run for real; the
model step is a fixture answer shaped by the contract.
"""

from __future__ import annotations

import json
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from dalton_core import event_judgement_result as result_lane
from dalton_core import material_requests as mr
from dalton_core.consensus_estimate import ConsensusEstimateAuthority
from dalton_core.event_judgement import (
    EventJudgementAuthority,
    EventJudgementValidationError,
    allowed_refs,
    build_context,
    build_judge_prompt,
    build_verifier_prompt,
    validate_judge_output,
)
from dalton_core.event_judgement_cli import (
    event_group_key,
    ClaimDocuments,
    job_group_hash,
    result_jobs,
    run_judgement,
    unjudged_event_groups,
)
from dalton_core.research_event import ResearchEventAuthority, record_event
from dalton_core.tracking_cadence import POLICY_PATH
from tests.event_result_fixtures import index_text
from tests.p14a_fixtures import ACN, AUTOMATION, CTSH, DXC, EPAM, IBM, P14aHarness
from tests.test_event_judgement import JUDGE_ROUTE, PASS, VERIFIER_ROUTE, FakeModel, resolver

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "event_result" / "acn_fy26q4_event.json")
                     .read_text(encoding="utf-8"))
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
TICKERS = {"ACN": ACN, "CTSH": CTSH, "EPAM": EPAM, "IBM": IBM, "DXC": DXC}


# ---------------------------------------------------------------------------
# the fixture answer: shaped by the contract, its tags resolved from the prompt
# ---------------------------------------------------------------------------


def passage_tag(prompt: str, needle: str, *, required: bool = True) -> str | None:
    """The ``[P…]`` block whose text contains ``needle``."""

    at = prompt.find(needle)
    if at < 0:
        if not required:
            return None
        raise AssertionError(f"{needle!r} is not in the prompt")
    tags = list(re.finditer(r"^\[(P\d+)\]", prompt[:at], re.M))
    if not tags:
        raise AssertionError(f"no passage block before {needle!r}")
    return tags[-1].group(1)


def draft_tag(prompt: str, metric: str, label: str) -> str:
    """The first tag of the ``label`` cell on the draft row for ``metric``."""

    line = next(line for line in prompt.splitlines() if line.startswith("- ") and f"（{metric}，" in line)
    found = re.search(re.escape(label) + r"[^\[|]*\[(N\d+)\]", line)
    if not found:
        raise AssertionError(f"no {label} cell on the {metric} row: {line}")
    return found.group(1)


def reaction_tag(prompt: str, label: str) -> str:
    found = re.search(re.escape(label) + r"[^\[]*\[(N\d+)\]", prompt)
    if not found:
        raise AssertionError(f"no {label} in the reaction block")
    return found.group(1)


def acn_answer(prompt: str, *, decision: str = "THESIS_STRENGTHENED", action: str = "note",
               why_unchanged: str = "", drop_rows: tuple[str, ...] = ()) -> dict:
    p_key = passage_tag(prompt, "New bookings of $22.2 billion")
    p_rev = passage_tag(prompt, "above the company’s guided range")
    p_fy = (passage_tag(prompt, "For the full fiscal 2027, we expect our revenue to be in the range of 3%-6%",
                        required=False)
            or passage_tag(prompt, "Company expects full-year revenue growth of 3% to 6% in local currency"))
    p_gs = passage_tag(prompt, "guide for FY27 revenue growth of 3% - 4% in CC", required=False)
    p_q1 = passage_tag(prompt, "First Quarter Fiscal 2027 Outlook")
    p_eps = passage_tag(prompt, "Fourth quarter GAAP diluted EPS of $3.29")
    n_rev = draft_tag(prompt, "revenue", "会前一致预期")
    n_eps = draft_tag(prompt, "eps", "会前一致预期")
    n_fy_eps = draft_tag(prompt, "guidance_full_year", "会前一致预期")
    n_q1 = draft_tag(prompt, "guidance_next_quarter", "会前一致预期")
    n_margin = draft_tag(prompt, "operating_margin", "实际")
    n_move = reaction_tag(prompt, "单日")
    n_basket = reaction_tag(prompt, "等权同日")
    rows = [
        {"metric": "revenue", "period": "FY2026Q4", "actual": f"$18.68B [{p_rev}]",
         "expected": f"18.04bn [{n_rev}]", "prior_guidance": f"$17.75B–$18.40B [{p_rev}]",
         "vs_expected": "above_range", "sources": [p_rev, n_rev]},
        {"metric": "revenue_growth_lc", "period": "FY2026Q4", "actual": f"+7% [{p_rev}]",
         "expected": "无", "prior_guidance": f"1%–5% [{p_rev}]", "vs_expected": "above_range",
         "sources": [p_rev]},
        {"metric": "bookings", "period": "FY2026Q4", "actual": f"$22.2B，本币 +5% [{p_key}]",
         "expected": "无", "prior_guidance": "无", "vs_expected": "n/a", "sources": [p_key]},
        {"metric": "book_to_bill", "period": "FY2026Q4", "actual": f"1.2 [{p_rev}]", "expected": "无",
         "prior_guidance": "无", "vs_expected": "n/a", "sources": [p_rev]},
        {"metric": "operating_margin", "period": "FY2026Q4", "actual": f"15.3% [{n_margin}]", "expected": "无",
         "prior_guidance": "无", "vs_expected": "n/a", "sources": [n_margin]},
        {"metric": "eps", "period": "FY2026Q4", "actual": f"$3.29 [{p_eps}]", "expected": f"3.18 [{n_eps}]",
         "prior_guidance": "无", "vs_expected": "beat", "sources": [p_eps, n_eps]},
        {"metric": "guidance_next_quarter", "period": "FY2027Q1",
         "actual": f"$18.95B–$19.60B，本币 2%–6% [{p_q1}]", "expected": f"19.40bn [{n_q1}]",
         "prior_guidance": "无", "vs_expected": "inline", "sources": [p_q1, n_q1]},
        {"metric": "guidance_full_year", "period": "FY2027",
         "actual": f"本币 3%–6%，EPS $14.39–$14.81 [{p_fy}]",
         "expected": f"一致预期 EPS 14.67 [{n_fy_eps}]" + (f"；高盛预览 本币 3% - 4% [{p_gs}]" if p_gs else ""),
         "prior_guidance": "无（新财年首次指引）", "vs_expected": "beat",
         "sources": [tag for tag in (p_fy, n_fy_eps, p_gs) if tag]},
    ]
    rows = [row for row in rows if row["metric"] not in drop_rows]
    return {
        "decision": decision, "action": action, "driver_refs": [], "thesis_refs": [],
        "because": ("Q4 本币收入增长高于公司 1%–5% 的指引上沿，新签订单 222 亿美元，FY27 本币增速指引 3%–6% 的中点高于"
                    "高盛预览预期的 3%–4%，需求底部的证据增强；EPS 指引中点与一致预期大致持平。"),
        "citations": [tag for tag in (p_key, p_rev, p_fy, p_gs) if tag],
        **({"note": "Q4 收入高于指引区间，FY27 本币增速指引 3%–6% 高于高盛预览；需求论点增强。"}
           if action == "note" else {}),
        **({"research_question": "核实 FY27 指引中的并购贡献与有机增速"} if action == "research" else {}),
        "evidence_status": "read",
        "actual_vs_expected": rows,
        "market_reaction": {
            "move": f"+15.78% [{n_move}]",
            "benchmark": f"同组篮子 [{n_basket}]；宽基指数 无基准",
            "explanation": "公司自身因素主导：同组篮子同日涨幅远小于公司，宽基指数无基准；指引高于预览预期与订单是主要解释。",
        },
        "thesis_impact": {
            "explanation": ("需求底部尚未确认的论点被削弱、AI 现代化抵消咨询疲弱的论点得到支持：本币增速超出指引上沿，"
                            "订单出货比 1.2，FY27 指引高于预览预期；但 EPS 指引中点低于一致预期，利润率扩张有限。"),
            "why_unchanged": why_unchanged,
        },
    }


class ContractModel(FakeModel):
    """A judge that answers the contract from the prompt it is given."""

    def __init__(self, build, **kwargs):
        super().__init__([], route=JUDGE_ROUTE, **kwargs)
        self.build = build

    def call(self, *, purpose, request_id, prompt, mission):
        self.replies = [self.build(prompt)]
        return super().call(purpose=purpose, request_id=request_id, prompt=prompt, mission=mission)


# ---------------------------------------------------------------------------
# the harness: the ACN occurrence in a fresh state directory
# ---------------------------------------------------------------------------


class AcnHarness(P14aHarness):
    grants = ("market_event", "observation", "stage_record", "deliverable")

    def setUp(self):
        super().setUp()
        self.events = ResearchEventAuthority(self.store)
        self.judgements = EventJudgementAuthority(self.store)
        self.pass_screen(ACN)
        for ticker, data in FIXTURE["closes"].items():
            self.prices(data["company_ref"], [tuple(pair) for pair in data["closes"]], ticker=ticker)
        self.consensus_rows()

    def consensus_rows(self, *keys: str):
        ConsensusEstimateAuthority(self.store)
        self.store.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            with self.store._transaction() as cur:
                for key in keys or ("consensus_version_row", "consensus_version_row_after"):
                    row = FIXTURE[key]
                    cur.execute(
                        f"INSERT INTO consensus_estimate_versions({','.join(row)}) VALUES"
                        f"({','.join('?' for _ in row)})", tuple(row.values()))
        finally:
            self.store.connection.execute("PRAGMA foreign_keys = ON")

    def index(self, key: str, **overrides):
        doc = {**FIXTURE[key], **overrides}
        return index_text(self.state_dir, doc["document_ref"], doc["kind"], doc["text"], date=doc["date"],
                          title=doc.get("title"), house=doc.get("house"),
                          acquired_at=overrides.get("acquired_at", f"{doc['date']}T20:00:00+00:00"))

    def index_all(self, keys=("exhibit", "transcript", "gs_preview", "bofa_note")):
        # The company's name reaches the identity the way it does live: from
        # its own annual report in the index (house = the filer).
        index_text(self.state_dir, "sec:filing:0001467373-25-000071", "sec_10k",
                   "Accenture plc annual report on Form 10-K for fiscal 2025. " * 20, date="2025-10-10",
                   title="Accenture plc 10-K 2025-10-10", house="Accenture plc",
                   acquired_at="2025-10-11T00:00:00+00:00")
        for key in keys:
            self.index(key)

    def record(self, key: str, *, created_at: str | None = None, payload: dict | None = None):
        source = FIXTURE[key]
        payload = {**source["payload"], **(payload or {})}
        with mock.patch("dalton_core.research_event._now",
                        return_value=created_at or "2026-10-01T21:00:00.000000+00:00"):
            return record_event(self.events, company_ref=ACN, kind=source["kind"],
                                occurred_at=source["occurred_at"], source_refs=source["source_refs"],
                                payload=payload, evidence_tier=source["evidence_tier"],
                                mission=self.mission, actor_ref=AUTOMATION)

    def material(self, event, group=None, **kwargs):
        return result_lane.build_result_material(
            self.state_dir, self.store.connection, event, group=group or [event], mission=self.mission,
            today="2026-10-02", numbers_fn=numbers_stub, **kwargs)

    def context(self, event, material):
        context = build_context(event=event, mission=self.mission, connection=self.store.connection,
                                judgements=self.judgements, source_table="alphaengine | sec | sales_notes")
        context["result"] = material
        context["grouped_events"] = [event]
        return context

    def run_lane(self, *, judge=None, verifier=None, now=NOW, judged_at=None, **kwargs):
        judge = judge or ContractModel(acn_answer)
        kwargs.setdefault("pack_builder", stub_pack_builder)
        verifier = verifier or FakeModel([PASS] * 6, route=VERIFIER_ROUTE)
        summary_dir = self.state_dir / "runs" / now.strftime("%Y%m%dT%H%M%S")
        stamp = judged_at or now.isoformat(timespec="microseconds")
        with mock.patch("dalton_core.event_judgement_result.PACK_BUDGET_CHARS", 40_000), \
                mock.patch("dalton_core.event_judgement._now", return_value=stamp):
            summary = run_judgement(
                state_dir=self.state_dir, summary_dir=summary_dir, judge_model=judge,
                verifier_model=verifier, family_resolver=resolver(), now=now,
                policy_path=POLICY_PATH, **kwargs)
        return summary, judge, verifier, summary_dir


def numbers_stub(state_dir, company_ref, as_of):
    """The numbers pack as of the day before, without the slow broker-note parse."""

    from dalton_core.numbers_pack import numbers_pack

    return numbers_pack(state_dir, company_ref, as_of, parse_unstored=False, include_peers=False)


def stub_pack_builder(state_dir, company_ref, product, as_of, **kwargs):
    from dalton_core.material_pack import build

    return build(state_dir, company_ref, product, as_of, **kwargs)




def judgement_rows(store, event_ref):
    return [json.loads(row[0]) for row in store.connection.execute(
        "SELECT record_json FROM event_judgements WHERE event_ref=? OR event_ref LIKE ? "
        "ORDER BY created_at, judgement_id", (event_ref, event_ref + "#rejudgement-%")).fetchall()]


# ---------------------------------------------------------------------------
# 1. the same-event pack and the expectations baseline
# ---------------------------------------------------------------------------


class SameEventPackTests(AcnHarness):
    def setUp(self):
        super().setUp()
        self.index_all()
        self.release = self.record("release_event")
        self.material = self.material(self.release)

    def test_the_pack_reads_the_event_window_and_its_body(self):
        material = self.material
        self.assertTrue(material["body_read"])
        self.assertEqual(material["kind"], "result")
        self.assertEqual(material["pack"]["recipe_version"], "event_judgement:v2")
        self.assertEqual(material["pack"]["as_of"], "2026-10-04")  # event day + 3
        self.assertEqual(set(material["documents_read"]), {
            FIXTURE["exhibit"]["document_ref"], FIXTURE["transcript"]["document_ref"],
            FIXTURE["bofa_note"]["document_ref"], FIXTURE["gs_preview"]["document_ref"]})
        prompt = build_judge_prompt(self.context(self.release, material))
        # Document-type evidence carries body passages, never only refs.
        self.assertIn("New bookings of $22.2 billion", prompt)
        self.assertIn("For the full fiscal 2027, we expect our revenue to be in the range of 3%-6%", prompt)
        self.assertIn("New bookings of $22.2B.", prompt)  # the BofA note of the day
        self.assertLess(len(prompt.encode("utf-8")), 60_000)

    def test_previews_come_from_before_the_event_only(self):
        prompt = build_judge_prompt(self.context(self.release, self.material))
        previews = prompt[prompt.index("## 6. 事件前 14 天"):prompt.index("## 7.")]
        self.assertIn("guide for FY27 revenue growth of 3% - 4% in CC", previews)
        self.assertIn("2026-09-27", previews)
        window = prompt[prompt.index("## 4. 事件日 ±3 天的卖方销售快报"):prompt.index("## 5.")]
        self.assertNotIn("2026-09-27", window)

    def test_documents_outside_the_window_stay_out(self):
        index_text(self.state_dir, "alphaengine-doc:old-call", "transcript",
                   "Accenture Q3 2026 call. New bookings were $21.3 billion. " * 10, date="2026-06-20",
                   title="Accenture Q3 2026")
        index_text(self.state_dir, "sales-note:later", "sales_note",
                   "Accenture follow-up a week later: bookings debate continues. " * 10, date="2026-10-08",
                   title="Desk note on Accenture", house="Goldman Sachs")
        material = self.material_(self.release)
        self.assertNotIn("alphaengine-doc:old-call", material["documents_read"])
        self.assertNotIn("sales-note:later", material["documents_read"])

    def material_(self, event):
        return result_lane.build_result_material(
            self.state_dir, self.store.connection, event, group=[event], mission=self.mission,
            today="2026-10-02", numbers_fn=numbers_stub)

    def test_the_baseline_is_the_consensus_of_the_day_before(self):
        rows = {row["metric"]: row for row in self.material["draft_rows"]}
        self.assertEqual(self.material["baseline_as_of"], "2026-09-30")
        revenue = rows["revenue"]
        self.assertEqual(revenue["actual"][0]["display"], "$18.68B")
        self.assertEqual(revenue["expected"][0]["display"], "18.04bn")
        self.assertIn("截至 2026-09-30", revenue["expected"][0]["note"])
        self.assertEqual(revenue["prior_guidance"][0]["display"], "$17.75B–$18.40B")
        growth = rows["revenue_growth_lc"]
        self.assertEqual(growth["actual"][0]["display"], "7%")
        self.assertEqual(growth["prior_guidance"][0]["display"], "1%–5%")
        self.assertEqual(rows["bookings"]["actual"][0]["display"], "$22.17B")
        self.assertEqual(rows["book_to_bill"]["actual"][0]["display"], "1.2")
        self.assertEqual(rows["operating_margin"]["actual"][0]["display"], "15.3%")
        self.assertEqual(rows["eps"]["actual"][0]["display"], "$3.29")
        self.assertEqual(rows["eps"]["expected"][0]["display"], "3.18")
        next_q = rows["guidance_next_quarter"]
        self.assertEqual(next_q["period"], "FY2027Q1")
        self.assertEqual([cell["display"] for cell in next_q["actual"]], ["$18.95B–$19.60B", "2%–6%"])
        self.assertEqual(next_q["expected"][0]["display"], "19.40bn")
        full_year = rows["guidance_full_year"]
        self.assertEqual(full_year["period"], "FY2027")
        self.assertEqual([cell["display"] for cell in full_year["actual"]],
                         ["3%–6%", "$14.39–$14.81", "15.9%–16.1%"])
        self.assertEqual(full_year["expected"][0]["display"], "14.67")
        self.assertEqual(sorted(self.material["applicable_rows"]), ["book_to_bill", "bookings"])

    def test_every_tag_in_the_draft_is_printed_and_resolves(self):
        prompt = build_judge_prompt(self.context(self.release, self.material))
        draft = prompt[prompt.index("## 实际 vs 预期 底稿"):prompt.index("## 市场反应")]
        for tag in set(re.findall(r"\[([NP]\d+)\]", draft)):
            self.assertIn(tag, self.material["tags"])
            if tag.startswith("P"):
                self.assertRegex(prompt, rf"(?m)^\[{tag}\]")
            self.assertIn(tag, self.material["tag_texts"])
        allowed = allowed_refs(self.context(self.release, self.material))
        self.assertIn("P1", allowed)
        self.assertIn(self.material["tags"]["P1"], allowed)

    def test_the_reaction_is_read_against_the_basket_and_says_no_benchmark(self):
        reaction = self.material["reaction"]
        self.assertEqual(reaction["move"]["percent"], "15.78")
        self.assertEqual(reaction["benchmark"]["status"], "unavailable")
        self.assertEqual(reaction["benchmark"]["label"], "无基准")
        self.assertEqual(reaction["basket"]["status"], "available")
        self.assertEqual(reaction["basket"]["members"], 4)

    def test_the_call_and_the_release_are_one_result_event(self):
        call = self.record("transcript_event", created_at="2026-10-02T07:00:00.000000+00:00")
        self.assertEqual(result_lane.result_key(self.store.connection, call),
                         result_lane.result_key(self.store.connection, self.release))
        documents = ClaimDocuments(self.store.connection)
        self.assertEqual(event_group_key(call, documents), event_group_key(self.release, documents))
        groups = unjudged_event_groups(self.events, self.judgements, company_ref=ACN, limit=None, now=NOW)
        result_group = next(group for group in groups if call["id"] in {item["id"] for item in group})
        self.assertEqual([item["id"] for item in result_group], [self.release["id"], call["id"]])

    def test_dates_keep_the_original_and_the_information_available_day_apart(self):
        late = self.record("release_event", created_at="2026-10-09T11:03:33.669779+00:00",
                           payload={"row_count": 12})
        dates = result_lane.event_dates(late, anchor=result_lane.result_anchor(self.store.connection, late))
        self.assertEqual(dates["occurred_on"], "2026-10-01")
        self.assertEqual(dates["information_available_on"], "2026-10-09")
        self.assertTrue(dates["reprocessed_old_event"])
        self.assertIn("旧事件重处理", result_lane.dates_line(dates))

    def test_pre_event_consensus_reads_the_version_before_the_release(self):
        from types import SimpleNamespace

        from dalton_core.consensus_estimate import period_consensus

        before = period_consensus(SimpleNamespace(connection=self.store.connection), ACN, as_of="2026-09-30")
        self.assertTrue(before)
        self.assertEqual({row["as_of"] for row in before}, {"2026-09-30"})
        latest = period_consensus(self.store, ACN)
        self.assertEqual({row["as_of"] for row in latest}, {"2026-10-02"})


# ---------------------------------------------------------------------------
# 2. the contract and its refusals
# ---------------------------------------------------------------------------


class ContractTests(AcnHarness):
    def setUp(self):
        super().setUp()
        self.index_all()
        self.release = self.record("release_event")
        self.material = self.material(self.release)
        self.ctx = self.context(self.release, self.material)
        self.prompt = build_judge_prompt(self.ctx)

    def validate(self, answer):
        return validate_judge_output(answer, self.ctx)

    def test_the_golden_answer_passes(self):
        judged = self.validate(acn_answer(self.prompt))
        self.assertEqual(judged["result"]["evidence_status"], "read")
        self.assertEqual(len(judged["result"]["actual_vs_expected"]), 8)

    def test_a_missing_table_is_refused(self):
        answer = acn_answer(self.prompt)
        del answer["actual_vs_expected"]
        with self.assertRaisesRegex(EventJudgementValidationError, "actual_vs_expected"):
            self.validate(answer)

    def test_a_missing_required_row_is_refused(self):
        with self.assertRaisesRegex(EventJudgementValidationError, "missing rows.*guidance_full_year"):
            self.validate(acn_answer(self.prompt, drop_rows=("guidance_full_year",)))

    def test_bookings_are_required_where_the_company_reports_them(self):
        with self.assertRaisesRegex(EventJudgementValidationError, "missing rows.*bookings"):
            self.validate(acn_answer(self.prompt, drop_rows=("bookings",)))

    def test_a_figure_without_a_tag_or_not_in_its_tag_is_refused(self):
        answer = acn_answer(self.prompt)
        answer["actual_vs_expected"][0]["actual"] = "$18.68B"
        answer["actual_vs_expected"][0]["sources"] = []
        with self.assertRaisesRegex(EventJudgementValidationError, "carry no tag"):
            self.validate(answer)
        answer = acn_answer(self.prompt)
        answer["actual_vs_expected"][0]["actual"] = answer["actual_vs_expected"][0]["actual"].replace(
            "18.68", "19.99")
        with self.assertRaisesRegex(EventJudgementValidationError, "not found in the cited"):
            self.validate(answer)

    def test_our_own_view_is_not_a_source(self):
        answer = acn_answer(self.prompt)
        answer["actual_vs_expected"][0]["actual"] = "$18.68B [O1]"
        with self.assertRaisesRegex(EventJudgementValidationError, "our own view|was not printed"):
            self.validate(answer)

    def test_no_change_after_a_beat_must_say_why(self):
        with self.assertRaisesRegex(EventJudgementValidationError, "why_unchanged"):
            self.validate(acn_answer(self.prompt, decision="NO_CHANGE", action="no_change"))
        judged = self.validate(acn_answer(
            self.prompt, decision="NO_CHANGE", action="no_change",
            why_unchanged="超预期主要来自外汇与并购贡献，有机增速约 3%，与需求底部未确认的论点一致，因此不改论点。"))
        self.assertEqual(judged["result"]["verdict"], "NO_CHANGE")

    def test_without_a_benchmark_the_reaction_must_say_so(self):
        answer = acn_answer(self.prompt)
        self.ctx["result"] = dict(self.material, reaction={**self.material["reaction"],
                                                           "basket": {"status": "unavailable"}})
        answer["market_reaction"]["benchmark"] = "同组篮子"
        answer["market_reaction"]["explanation"] = "公司自身因素主导，指引高于预览。"
        with self.assertRaisesRegex(EventJudgementValidationError, "无基准"):
            self.validate(answer)

    def test_an_unread_body_cannot_be_judged(self):
        self.ctx["result"] = dict(self.material, body_read=False)
        with self.assertRaisesRegex(EventJudgementValidationError, "body was not read"):
            self.validate(acn_answer(self.prompt))
        answer = acn_answer(self.prompt, decision="NO_CHANGE", action="research")
        answer["evidence_status"] = "insufficient"
        judged = self.validate(answer)
        self.assertEqual(judged["result"]["verdict"], result_lane.VERDICT_EVIDENCE_INSUFFICIENT)
        self.assertEqual(judged["result"]["verdict_label"], "证据不足，安排读取")

    def test_insufficient_evidence_is_only_no_change_research(self):
        answer = acn_answer(self.prompt)
        answer["evidence_status"] = "insufficient"
        with self.assertRaisesRegex(EventJudgementValidationError, "NO_CHANGE / research"):
            self.validate(answer)

    def test_the_verifier_reads_the_table_and_its_tagged_sources(self):
        judged = {"status": "judged", "model": {}, **self.validate(acn_answer(self.prompt))}
        prompt = build_verifier_prompt(self.ctx, judged)
        self.assertIn("result-type event", prompt)
        self.assertIn("New bookings of $22.2 billion", prompt)
        self.assertIn('"vs_expected": "above_range"', prompt)

    def test_canary_the_live_legacy_answer_is_refused_under_the_contract(self):
        # 2026-10-09 live ACN: NO_CHANGE / research on 11 numbers, no table.
        legacy = dict(FIXTURE["legacy_judgement"])
        legacy["citations"] = [self.release["id"]]
        with self.assertRaisesRegex(EventJudgementValidationError, "omitted required keys"):
            self.validate(legacy)


# ---------------------------------------------------------------------------
# 3. the lane: 证据不足，安排读取, re-judgement bounds, dedupe
# ---------------------------------------------------------------------------


class GapAndRejudgementTests(AcnHarness):
    def test_an_unread_body_is_evidence_insufficient_without_a_model_and_opens_a_request(self):
        release = self.record("release_event")
        summary, judge, verifier, _ = self.run_lane(judged_at="2026-10-01T22:00:00.000000+00:00")
        self.assertEqual(judge.prompts, [])
        self.assertEqual(verifier.prompts, [])
        self.assertEqual(summary["evidence_insufficient"], 1)
        record = self.judgements.judgement_for(release["id"])
        self.assertEqual((record["decision"], record["action"]), ("NO_CHANGE", "research"))
        self.assertEqual(record["result"]["verdict_label"], "证据不足，安排读取")
        request = record["effect"]["material_request"]
        self.assertTrue(request["id"].startswith("material-request:"))
        found = mr.request(self.state_dir, request["id"])
        self.assertEqual(found["need"]["doc_kinds"], ["sec_exhibit"])
        self.assertEqual(found["requesters"][0]["lane"], "event_judgement")
        # The same gap does not get its NO_CHANGE again: nothing is owed, nothing is paid.
        again, judge, _, _ = self.run_lane(now=NOW + timedelta(hours=2))
        self.assertEqual(again["judgement_status"], "nothing_unjudged")
        self.assertEqual(judge.prompts, [])
        # The material arrives: the event is re-judged on it, once, with the reason.
        self.index_all()
        jobs = result_jobs(self.events, company_ref=ACN, state_dir=self.state_dir, now=NOW + timedelta(hours=3))
        self.assertEqual([(job["kind"], job["rejudge"]["reason"]) for job in jobs],
                         [("rejudge", "material_arrived")])
        done, judge, _, _ = self.run_lane(now=NOW + timedelta(hours=3))
        self.assertEqual(done["rejudged"], 1)
        self.assertEqual(len(judge.prompts), 1)
        latest = self.judgements.latest_judgement_for(release["id"])
        self.assertEqual(latest["event_ref"], release["id"] + "#rejudgement-1")
        self.assertEqual(latest["rejudgement"]["reason"], "material_arrived")
        self.assertEqual(latest["rejudgement"]["prior_judgement_ref"], record["id"])
        self.assertEqual(latest["result"]["evidence_status"], "read")
        self.assertEqual(result_jobs(self.events, company_ref=ACN, state_dir=self.state_dir,
                                     now=NOW + timedelta(hours=4)), [])

    def test_a_same_event_document_within_72_hours_rejudges_at_most_twice(self):
        self.index_all(keys=("exhibit",))
        release = self.record("release_event", created_at="2026-10-01T21:00:00.000000+00:00")
        self.run_lane(now=NOW, judged_at="2026-10-01T22:00:00.000000+00:00")
        self.assertIsNotNone(self.judgements.judgement_for(release["id"]))
        self.assertEqual(result_jobs(self.events, company_ref=ACN, state_dir=self.state_dir, now=NOW), [])
        # The call arrives the next morning.
        self.index("transcript", acquired_at="2026-10-02T08:00:00+00:00")
        first = self.run_lane(now=NOW, judged_at="2026-10-02T09:00:00.000000+00:00")[0]
        self.assertEqual(first["rejudged"], 1)
        # A desk note the day after.
        self.index("bofa_note", acquired_at="2026-10-03T10:00:00+00:00")
        second = self.run_lane(now=NOW + timedelta(days=1), judged_at="2026-10-03T11:00:00.000000+00:00")[0]
        self.assertEqual(second["rejudged"], 1)
        history = judgement_rows(self.store, release["id"])
        self.assertEqual([row.get("rejudgement", {}).get("sequence") for row in history], [None, 1, 2])
        self.assertEqual({row["rejudgement"]["reason"] for row in history[1:]},
                         {"same_event_document_within_72h"})
        # A third arrival inside the window: the bound is reached.
        index_text(self.state_dir, "sales-note:third", "sales_note",
                   "Accenture: more desk colour on the quarter and the FY27 guide. " * 10, date="2026-10-03",
                   title="Accenture desk colour", house="Morgan Stanley", acquired_at="2026-10-04T10:00:00+00:00")
        self.assertEqual(result_jobs(self.events, company_ref=ACN, state_dir=self.state_dir,
                                     now=NOW + timedelta(days=2)), [])

    def test_a_document_after_the_72_hour_window_is_not_a_rejudgement(self):
        self.index_all(keys=("exhibit",))
        self.record("release_event", created_at="2026-10-01T21:00:00.000000+00:00")
        self.run_lane(now=NOW, judged_at="2026-10-01T22:00:00.000000+00:00")
        self.index("transcript", acquired_at="2026-10-05T08:00:00+00:00")
        self.assertEqual(result_jobs(self.events, company_ref=ACN, state_dir=self.state_dir,
                                     now=NOW + timedelta(days=4)), [])

    def test_an_old_event_processed_again_dedupes_against_its_judgement(self):
        self.index_all()
        release = self.record("release_event", created_at="2026-10-01T21:00:00.000000+00:00")
        self.run_lane(now=NOW, judged_at="2026-10-01T22:00:00.000000+00:00")
        again = self.record("release_event", created_at="2026-10-02T10:00:00.000000+00:00",
                            payload={"row_count": 12})
        jobs = result_jobs(self.events, company_ref=ACN, state_dir=self.state_dir, now=NOW)
        self.assertEqual([job["kind"] for job in jobs], ["repeat"])
        summary, judge, _, _ = self.run_lane(now=NOW, judged_at="2026-10-02T11:00:00.000000+00:00")
        self.assertEqual(judge.prompts, [])
        self.assertEqual(summary["repeats"], 1)
        copy = self.judgements.judgement_for(again["id"])
        self.assertEqual(copy["effect"]["kind"], "repeated_result_event")
        self.assertEqual(copy["effect"]["primary_event_ref"], release["id"])
        self.assertEqual(copy["model"]["cost_micros"], 0)
        self.assertEqual(copy["main_event_ref"], release["id"])

    def test_the_deploy_case_a_legacy_judgement_without_a_body_is_rejudged_once(self):
        # Live legacy ACN, 2026-10-09: the release judged NO_CHANGE / research on
        # its income-statement numbers alone, before batch F.
        self.index_all()
        release = self.record("release_event", created_at="2026-10-09T11:03:33.669779+00:00")
        call = self.record("transcript_event", created_at="2026-10-08T10:33:00.411943+00:00")
        legacy = {**FIXTURE["legacy_judgement"], "citations": [release["id"]], "model": None}
        with mock.patch("dalton_core.event_judgement._now", return_value="2026-10-09T11:04:08.918331+00:00"):
            for event in (release, call):
                self.judgements.record(event=event, judgement=legacy,
                                       verification={"status": "verified", "verdict": "pass"},
                                       effect={"kind": "research", "status": "queued",
                                               "reason": "no_bindable_template"},
                                       mission=self.mission, actor_ref=AUTOMATION)
        later = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)
        jobs = result_jobs(self.events, company_ref=ACN, state_dir=self.state_dir, now=later)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["rejudge"]["reason"], "legacy_judgement_without_body")
        self.assertEqual(jobs[0]["rejudge"]["sequence"], 1)
        self.assertEqual([item["id"] for item in jobs[0]["group"]], [release["id"], call["id"]])
        self.assertRegex(job_group_hash(jobs[0]), r"^[0-9a-f]{64}$")
        from dalton_core.mission_event_judgement_lane import pending_event_groups

        pending = pending_event_groups(self.store, self.missions, self.mission)
        self.assertIn(release["id"], {ref for item in pending for ref in item["event_refs"]})
        summary, judge, _, run_dir = self.run_lane(now=later, judged_at="2026-10-10T09:00:00.000000+00:00")
        self.assertEqual(summary["rejudged"], 1)
        latest = self.judgements.latest_judgement_for(release["id"])
        self.assertEqual(latest["rejudgement"]["reason"], "legacy_judgement_without_body")
        self.assertEqual(result_jobs(self.events, company_ref=ACN, state_dir=self.state_dir,
                                     now=later + timedelta(hours=1)), [])

    def test_a_research_action_always_opens_a_material_request(self):
        from dalton_core.event_judgement import apply_effect

        event = record_event(self.events, company_ref=ACN, kind="price_move",
                             occurred_at="2026-09-02T00:00:00+00:00",
                             source_refs=["market-price-series-version:1"],
                             payload={"as_of": "2026-09-02", "close": "104", "previous_close": "100",
                                      "return_percent": "4.0000", "direction": "up",
                                      "basket_return_percent": "0.0000", "excess_vs_basket_percent": "4.0000",
                                      "basket_members": 4, "benchmark_ref": None,
                                      "benchmark_return_percent": None, "excess_vs_benchmark_percent": None,
                                      "trigger": "absolute", "threshold_percent": "3.0",
                                      "price_version_ref": "market-price-series-version:1",
                                      "invocation_ref": None},
                             mission=self.mission, actor_ref=AUTOMATION)
        judgement = {"decision": "NO_CHANGE", "action": "research", "because": "原因待查。",
                     "research_question": "这次上涨是否有公司公告？", "citations": [event["id"]]}
        effect = apply_effect(event=event, judgement=judgement, context={}, mission=self.mission,
                              playbook=None, deliverables=None, forecast_models=None, model_version=None,
                              research_admitter=None, actor_ref=AUTOMATION,
                              material_opener=lambda spec: result_lane.open_material(self.state_dir, spec))
        request = effect["material_request"]
        self.assertEqual(request["kind"], "event_research")
        stored = mr.request(self.state_dir, request["id"])
        self.assertEqual(stored["need"]["question"], "这次上涨是否有公司公告？")

    def test_e_s_open_for_effect_is_used_by_name_when_installed(self):
        import sys
        import types

        calls = []
        module = types.ModuleType("dalton_core.material_acquisition")
        module.open_for_effect = lambda state_dir, effect: calls.append(effect) or {
            "id": "material-request:e", "status": "open", "write_status": "fresh"}
        with mock.patch.dict(sys.modules, {"dalton_core.material_acquisition": module}):
            opened = result_lane.open_material(self.state_dir, result_lane.research_need(
                {"id": "research-event:x", "company_ref": ACN}, {"research_question": "q?"}))
        self.assertEqual(opened["via"], "material_acquisition")
        self.assertEqual(calls[0]["subject"], ACN)


# ---------------------------------------------------------------------------
# 4. the ACN golden replay
# ---------------------------------------------------------------------------


class AcnGoldenReplayTests(AcnHarness):
    def setUp(self):
        super().setUp()
        self.index_all()
        self.release = self.record("release_event", created_at="2026-10-01T21:00:00.000000+00:00")
        self.call = self.record("transcript_event", created_at="2026-10-01T23:00:00.000000+00:00")
        self.summary, self.judge, self.verifier, self.run_dir = self.run_lane(
            judged_at="2026-10-02T12:00:00.000000+00:00")
        self.record_ = self.judgements.judgement_for(self.release["id"])

    def test_the_replay_judges_the_release_and_its_call_once_on_the_pack(self):
        self.assertEqual(self.summary["judged"], 1, self.summary)
        self.assertEqual(len(self.judge.prompts), 1)
        self.assertEqual(self.record_["decision"], "THESIS_STRENGTHENED")
        alias = self.judgements.judgement_for(self.call["id"])
        self.assertEqual(alias["effect"]["kind"], "grouped_judgement")
        self.assertEqual(alias["effect"]["primary_event_ref"], self.release["id"])
        self.assertEqual(alias["main_event_ref"], self.release["id"])

    def test_the_judgement_cites_the_figures_the_analyst_needs(self):
        rows = {row["metric"]: row for row in self.record_["result"]["actual_vs_expected"]}
        self.assertIn("$22.2B", rows["bookings"]["actual"])
        self.assertIn("+7%", rows["revenue_growth_lc"]["actual"])
        self.assertIn("1%–5%", rows["revenue_growth_lc"]["prior_guidance"])
        self.assertEqual(rows["revenue_growth_lc"]["vs_expected"], "above_range")
        self.assertIn("3%–6%", rows["guidance_full_year"]["actual"])
        self.assertIn("$14.39–$14.81", rows["guidance_full_year"]["actual"])
        self.assertIn("3% - 4%", rows["guidance_full_year"]["expected"])  # GS's preview
        material = self.record_["result_material"]
        gs = next(tag for tag in rows["guidance_full_year"]["sources"]
                  if "3% - 4% in CC" in material["tag_texts"].get(tag, ""))
        self.assertEqual(material["tag_sources"][gs], "卖方销售快报（Goldman Sachs）")
        impact = self.record_["result"]["thesis_impact"]
        self.assertGreater(len(impact["explanation"]), 20)
        self.assertIn("无基准", self.record_["result"]["market_reaction"]["benchmark"])
        for row in rows.values():
            for tag in row["sources"]:
                self.assertTrue(material["tags"][tag].startswith(("doc-passage:", "pack-json:")))

    def test_the_record_keeps_what_was_read_and_when(self):
        material = self.record_["result_material"]
        self.assertTrue(material["body_read"])
        self.assertIn(FIXTURE["exhibit"]["document_ref"], material["documents_read"])
        self.assertEqual(material["dates"]["occurred_on"], "2026-10-01")
        self.assertEqual(material["baseline_as_of"], "2026-09-30")
        self.assertEqual(material["pack"]["recipe_version"], "event_judgement:v2")

    def test_the_run_directory_keeps_the_prompts(self):
        files = self.summary["prompt_files"]
        self.assertEqual(len(files), 2)
        judge_prompt = (self.run_dir / [f for f in files if f.endswith(".judge.txt")][0]).read_text()
        self.assertEqual(judge_prompt, self.judge.prompts[0])
        self.assertIn("## 实际 vs 预期 底稿", judge_prompt)
        verifier_prompt = (self.run_dir / [f for f in files if f.endswith(".verifier.txt")][0]).read_text()
        self.assertEqual(verifier_prompt, self.verifier.prompts[0])

    def test_the_published_note_renders_numbers_with_sources_and_no_placeholder(self):
        version = self.store.connection.execute(
            "SELECT record_json FROM mission_deliverable_versions WHERE kind='event_note'").fetchone()
        note = json.loads(version[0])
        body = "\n".join(section["body"] for section in note["sections"])
        self.assertNotIn("〔", body)
        for figure in ("$22.2B", "$18.68B", "1%–5%", "3%–6%", "$14.39–$14.81", "3% - 4%"):
            self.assertIn(figure, body)
        self.assertIn("出处：", body)
        table = note["sections"][1]
        self.assertTrue(table["numbers"])
        self.assert_numbers_resolve(note)

    def assert_numbers_resolve(self, note):
        """Every published figure cites exactly one source, and that source says the figure."""

        from dalton_core.evidence_index import passage_resolver
        from dalton_core.event_judgement_result import _bare, _source_values

        resolve = passage_resolver(self.state_dir)
        material = self.record_["result_material"]
        texts_by_ref = {ref: material["tag_texts"].get(tag) for tag, ref in material["tags"].items()}
        for section in note["sections"]:
            for entry in section["numbers"]:
                kinds = [key for key in ("passage_ref", "number_ref") if entry.get(key)]
                self.assertEqual(len(kinds), 1, entry)
                if kinds == ["passage_ref"]:
                    source = resolve(entry["passage_ref"])
                else:
                    self.assertTrue(entry["number_ref"].startswith("pack-json:/event_result/"), entry)
                    source = entry["source_text"]
                    self.assertEqual(source, texts_by_ref[entry["number_ref"]][:400])
                self.assertIsNotNone(source, entry)
                self.assertIn(_bare(entry["text"]), _source_values(source), entry)

    def test_published_figures_come_from_both_passages_and_pack_numbers(self):
        version = self.store.connection.execute(
            "SELECT record_json FROM mission_deliverable_versions WHERE kind='event_note'").fetchone()
        note = json.loads(version[0])
        table = note["sections"][1]
        by_text = {}
        for entry in table["numbers"]:
            by_text.setdefault(entry["text"], set()).add("passage" if entry.get("passage_ref") else "pack")
        self.assertIn("passage", by_text["$22.2"])          # the exhibit
        self.assertIn("passage", by_text["$14.39"])         # the call
        self.assertIn("pack", by_text["18.04"])             # pre-event consensus
        self.assertIn("pack", by_text["15.78%"])            # the event-day move
        self.assertIn("18.04bn", table["body"])
        self.assert_numbers_resolve(note)

    def test_summary_lines_bind_every_figure(self):
        from dalton_core.mission_deliverable import unsourced_numbers

        lines, entries = result_lane.summary_lines(self.record_)
        for line in lines:
            if "实际：" in line or "预期：" in line:
                self.assertEqual(unsourced_numbers(line, entries), [], line)
        self.assertNotRegex("\n".join(lines), r"\[[NP]\d+\]")

    def test_the_digest_binds_pack_numbers_when_the_publisher_resolves_them(self):
        from dalton_core.event_judgement import build_daily_digest

        row = dict(self.record_, decision="NO_CHANGE",
                   effect={"kind": "no_change", "status": "recorded",
                           "daily_digest": "event-note-daily-digest:v1"})
        day = row["created_at"][:10]
        with_pack = build_daily_digest(ACN, day, [row], allow_pack_numbers=True)
        body = with_pack["sections"][1]["body"]
        self.assertIn("18.04bn", body)
        self.assertIn("15.78%", body)
        self.assertTrue(any(ref.startswith("pack-json:/event_result/") for ref in with_pack["pack_numbers"]))
        plain = build_daily_digest(ACN, day, [row])
        self.assertNotIn("18.04bn", plain["sections"][1]["body"])
        self.assertIn("$22.2B", plain["sections"][1]["body"])
        self.assertNotIn("〔", plain["sections"][1]["body"])

    def test_the_cockpit_shows_the_result_fields(self):
        from dalton_core.cockpit_plane import _event_result_display

        shown = _event_result_display(self.record_, self.record_["effect"])
        self.assertEqual(len(shown["event_actual_vs_expected"]), 8)
        self.assertEqual(shown["verdict_label"], "核心论点增强")
        self.assertIn("无基准", shown["benchmark_move"]["benchmark"])
        self.assertIn("event_dates_label", shown)
        alias = self.judgements.judgement_for(self.call["id"])
        self.assertEqual(_event_result_display(alias, alias["effect"])["main_event_ref"], self.release["id"])

    def test_nothing_is_owed_after_the_replay(self):
        self.assertEqual(result_jobs(self.events, company_ref=ACN, state_dir=self.state_dir, now=NOW), [])
        again = self.run_lane(now=NOW + timedelta(hours=1))[0]
        self.assertEqual(again["judgement_status"], "nothing_unjudged")


if __name__ == "__main__":
    unittest.main()


class PackNumberPublisherTests(AcnHarness):
    """With batch D's publisher (``pack_numbers``), consensus figures publish too."""

    def test_the_note_hands_pack_numbers_to_a_publisher_that_resolves_them(self):
        from dalton_core.event_judgement import publish_event_note

        self.index_all()
        release = self.record("release_event")
        material = self.material(release)
        ctx = self.context(release, material)
        judged = validate_judge_output(acn_answer(build_judge_prompt(ctx)), ctx)
        calls = []

        class Publisher:
            def publish(self, *, kind, subject_ref, mission, playbook, template_ref, sections, summary,
                        gaps, model_invocation_refs, actor_ref, idempotency_key, pack_numbers=None):
                calls.append({"sections": sections, "pack_numbers": pack_numbers})
                return {"status": "fresh", "id": "mission-deliverable-version:x"}

        publish_event_note(Publisher(), event=release, judgement={**judged, "model": {}}, mission=self.mission,
                           playbook={}, actor_ref=AUTOMATION, context=ctx)
        body = calls[0]["sections"][1]["body"]
        self.assertIn("18.04bn", body)
        self.assertIn("14.67", body)
        self.assertIn("3% - 4%", body)
        refs = {entry.get("number_ref") for entry in calls[0]["sections"][1]["numbers"]}
        self.assertTrue(refs & set(calls[0]["pack_numbers"]))
        for ref, text in calls[0]["pack_numbers"].items():
            self.assertTrue(ref.startswith("pack-json:"))
            self.assertTrue(text)


class CatalystLinkTests(AcnHarness):
    def test_the_judgement_links_the_event_to_its_catalyst_entry(self):
        from dalton_core.catalyst_calendar import CatalystCalendarAuthority, entry_for_event

        calendar = CatalystCalendarAuthority(self.store)
        filed = {"kind": "filing", "ref": "sec:filing:0001467373-26-000037", "source_ref": "source:sec-edgar",
                 "observed_date": "2026-10-01", "observed_at": "2026-10-01T12:00:00+00:00",
                 "confidence": "confirmed", "note": "8-K Item 2.02"}
        calendar.publish(company_ref=ACN, entries=[{"event_kind": "earnings", "sources": [filed], "notes": ""}],
                         change_reason="evidence_thicker", evidence_refs=["evidence:one"], now="2026-09-09")
        linked = entry_for_event(self.store.connection, ACN, "2026-10-01")
        self.assertTrue(linked["entry_ref"].startswith("catalyst-entry:"))
        self.assertEqual(linked["expected_date"], "2026-10-01")
        self.assertIsNone(entry_for_event(self.store.connection, ACN, "2026-12-01"))
        self.index_all()
        self.record("release_event")
        self.run_lane()
        record = self.judgements.recent(ACN)[0]
        self.assertEqual(record["result_material"]["catalyst_entry_ref"], linked["entry_ref"])


class DocumentEventTests(AcnHarness):
    """Document-type events outside the result contract: a body, or 证据不足."""

    def document(self, kind, document, *, tier=None, day=1):
        return record_event(self.events, company_ref=ACN, kind=kind,
                            occurred_at=f"2026-10-{day:02d}T10:00:00+00:00",
                            source_refs=["source:sales-notes", document],
                            payload={"document_ref": document, "source_ref": "source:sales-notes",
                                     "spec_ref": "sales-note", "discovery_ref": "d", "title": None, "host": None},
                            evidence_tier=tier, mission=self.mission, actor_ref=AUTOMATION)

    def test_an_unindexed_sales_note_is_evidence_insufficient_and_asks_for_its_body(self):
        event = self.document("sales_note", "sales-note:unindexed")
        summary, judge, _, _ = self.run_lane()
        self.assertEqual(judge.prompts, [])
        record = self.judgements.judgement_for(event["id"])
        self.assertEqual(record["result"]["verdict"], "evidence_insufficient")
        request = mr.request(self.state_dir, record["effect"]["material_request"]["id"])
        self.assertEqual(request["need"]["doc_kinds"], ["sales_note"])
        self.assertEqual(request["period"], "event_document:sales-note:unindexed")

    def test_an_indexed_document_is_judged_on_its_passages(self):
        index_text(self.state_dir, "sales-note:indexed", "sales_note",
                   "Accenture desk colour: clients are re-scoping consulting work. " * 6, date="2026-10-01",
                   title="Desk colour", house="BofA")
        event = self.document("sales_note", "sales-note:indexed")
        judge = FakeModel([{"decision": "NO_CHANGE", "action": "no_change", "driver_refs": [], "thesis_refs": [],
                            "because": "桌面观点重复已知需求判断。", "citations": ["P1"]}], route=JUDGE_ROUTE)
        summary, judge, _, _ = self.run_lane(judge=judge)
        self.assertEqual(summary["judged"], 1, summary)
        self.assertIn("clients are re-scoping consulting work", judge.prompts[0])
        record = self.judgements.judgement_for(event["id"])
        self.assertEqual(record["result_material"]["documents_read"], ["sales-note:indexed"])
        self.assertEqual(record["citations"], ["P1"])

    def test_news_without_a_body_keeps_its_old_reading(self):
        event = record_event(self.events, company_ref=ACN, kind="news", occurred_at="2026-10-01T10:00:00+00:00",
                             source_refs=["public-web-url:x"],
                             payload={"document_ref": "public-web-url:x", "source_ref": "source:web",
                                      "spec_ref": "management-changes", "discovery_ref": "d",
                                      "title": "Accenture names a new CFO", "host": "example.com"},
                             mission=self.mission, actor_ref=AUTOMATION)
        judge = FakeModel([{"decision": "NO_CHANGE", "action": "no_change", "driver_refs": [], "thesis_refs": [],
                            "because": "人事新闻，不影响需求判断。", "citations": []}], route=JUDGE_ROUTE)
        summary, judge, _, _ = self.run_lane(judge=judge)
        self.assertEqual(len(judge.prompts), 1)
        self.assertIn("NOT read", judge.prompts[0])
        self.assertEqual(self.judgements.judgement_for(event["id"])["decision"], "NO_CHANGE")

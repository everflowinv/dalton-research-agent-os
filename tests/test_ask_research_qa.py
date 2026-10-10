"""Batch G: research Q&A -- dates, retrieval, the four-part answer, the material loop.

The golden replay is the owner's own question, asked on 2026-10-09:
「关于 Accenture ACN：昨天股价为什么大涨？」.  The fixture under
``tests/fixtures/ask_golden_acn`` is cut from the real documents in the legacy
spool (8-K and Exhibit 99.1 of 2026-10-01, the Q4 FY26 call, the BofA,
Jefferies and Goldman Sachs sales notes around the print, two web pages and a
wiki page as noise), the stored price series of ACN and its covered peers
(2026-09 to 2026-10-09) and the numbers pack as it read on 2026-10-09.  The
index is built from those texts exactly as the indexer stores them; the model
step is mocked with a contract-shaped answer whose tags are looked up by
content, so retrieval, the context and the verifier are what is under test.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core import ask_answer, ask_context, ask_refresh
from dalton_core import evidence_index as ei
from dalton_core import material_requests as mr
from dalton_core.evidence_corpus import DocumentBody

FIXTURE = Path(__file__).with_name("fixtures") / "ask_golden_acn"
ACN = "company:sec-cik:0001467373"
IBM = "company:sec-cik:0000051143"
CTSH = "company:sec-cik:0001058290"
EPAM = "company:sec-cik:0001352010"
DXC = "company:sec-cik:001688568"
TICKERS = {ACN: "ACN", IBM: "IBM", CTSH: "CTSH", EPAM: "EPAM", DXC: "DXC"}
NAMES = {"ACN": "Accenture", "IBM": "IBM", "CTSH": "Cognizant", "EPAM": "EPAM", "DXC": "DXC Technology"}
QUESTION = "关于 Accenture ACN：昨天股价为什么大涨？"
TODAY = "2026-10-09"
OWNER = "human:owner-test"

MISSION = {
    "id": "coverage-mission-version:test:1", "mission_ref": "coverage-mission:us-it-services", "version": 1,
    "title": "US IT services", "objective": "弄清 AI 对 IT 服务需求与定价的影响。",
    "research_questions": ["需求是否见底？"], "industry_ref": "industry:us-it-services",
    "universe": [{"company_ref": ref, "ticker": ticker, "name": NAMES[ticker]} for ref, ticker in TICKERS.items()],
    "autonomy": {"may_write": ["claim"]},
}
MEMBERS = {member["company_ref"]: member for member in MISSION["universe"]}

# The current ACN thesis as the owner admitted it on 2026-09-14 (statement verbatim).
THESES = [{
    "id": "thesis-version:108925ad079741d38f0f912444648d2a", "thesis_ref": "thesis:acn:ai-reinvention-growth",
    "version": 2, "confidence": "medium", "created_at": "2026-09-14T13:46:06.691640+00:00",
    "change_reason": "THESIS_WEAKENED｜人裁决的 thesis 修订候选",
    "statement": ("AI-led modernization and integrated delivery give Accenture a credible route to offset weak "
                  "discretionary consulting, but sustained mid-single-digit local-currency growth with preserved "
                  "operating margin remains a low-confidence hypothesis, contingent on net incremental revenue, "
                  "retained delivery savings and successful restructuring."),
}, {
    "id": "thesis-version:00482bf2e3854c158c532677eee24933", "thesis_ref": "thesis:acn:ai-reinvention-growth",
    "version": 1, "confidence": "medium", "created_at": "2026-08-27T12:28:30.437631+00:00",
    "statement": "SUPERSEDED VERSION -- must never be shown as the house view.",
}]
NO_CHANGE_BECAUSE = ("我们维持两条投资论点和各项预测假设不变，下一步去核实新签订单和 FY27 指引。"
                     "10月1日股价单日上涨15.8%，说明市场看重的很可能是订单或指引。")

CORE_SCHEMA = """
CREATE TABLE coverage_mission_versions(mission_version_id TEXT PRIMARY KEY, mission_ref TEXT,
    version_number INTEGER, record_json TEXT);
CREATE TABLE coverage_mission_pointer(mission_ref TEXT PRIMARY KEY, mission_version_id TEXT, updated_at TEXT);
CREATE TABLE market_price_series_versions(version_id TEXT PRIMARY KEY, series_ref TEXT, version_number INTEGER,
    company_ref TEXT, ticker TEXT, record_json TEXT);
CREATE TABLE research_events(event_id TEXT PRIMARY KEY, company_ref TEXT, kind TEXT, occurred_at TEXT,
    evidence_tier TEXT, record_json TEXT);
CREATE TABLE event_judgements(judgement_id TEXT PRIMARY KEY, event_ref TEXT, company_ref TEXT, decision TEXT,
    action TEXT, created_at TEXT, record_json TEXT);
"""


class FakeCtx:
    """The one ``CorpusContext`` method ``index_document`` calls: who a passage names."""

    PATTERNS = {ACN: r"Accenture|\bACN\b", IBM: r"\bIBM\b", CTSH: r"Cognizant|\bCTSH\b", EPAM: r"\bEPAM\b",
                DXC: r"\bDXC\b"}

    def companies_named(self, text: str) -> list[str]:
        return [ref for ref, pattern in self.PATTERNS.items() if re.search(pattern, text)]


def index_text(state: Path, *, document_ref: str, kind: str, text: str, date: str, title: str | None = None,
               house: str | None = None, filed: list[str] | None = None, source_ref: str = "source:test",
               period: str | None = None, url: str | None = None) -> None:
    body = DocumentBody(
        document_ref=document_ref, source_ref=source_ref, kind=kind, text=text,
        text_sha256=hashlib.sha256(text.encode()).hexdigest(), renderer="text", title=title, house=house,
        document_date=date, period=period, url=url, acquired_at=f"{date}T12:00:00+00:00",
        filed_company_refs=list(filed or []), named_companies=[], metadata={})
    connection = ei.open_index(state)
    try:
        epoch = ei._epoch(connection, "2026-01-01T00:00:00+00:00")
        connection.execute("BEGIN IMMEDIATE")
        ei.index_document(connection, FakeCtx(), body, source_key=f"test:{document_ref}",
                          now=f"{date}T12:00:00+00:00", epoch=epoch)
        connection.execute("COMMIT")
    finally:
        connection.close()


def numbers_fixture(state_dir, company_ref, as_of):
    if company_ref != ACN:
        return {"json": {}}
    return {"json": json.loads((FIXTURE / "numbers.json").read_text(encoding="utf-8"))}


def golden_state(root: Path) -> Path:
    """A state directory with the fixture core and the fixture evidence index."""

    state = root / "state"
    state.mkdir()
    core = sqlite3.connect(state / "core.sqlite")
    core.executescript(CORE_SCHEMA)
    core.execute("INSERT INTO coverage_mission_versions VALUES (?,?,?,?)",
                 (MISSION["id"], MISSION["mission_ref"], 1, json.dumps(MISSION)))
    core.execute("INSERT INTO coverage_mission_pointer VALUES (?,?,?)",
                 (MISSION["mission_ref"], MISSION["id"], "2026-09-01T00:00:00+00:00"))
    prices = json.loads((FIXTURE / "prices.json").read_text(encoding="utf-8"))
    refs = {ticker: ref for ref, ticker in TICKERS.items()}
    for ticker, series in prices.items():
        ref = refs[ticker]
        record = {"id": series["version_id"], "company_ref": ref, "ticker": ticker,
                  "currency": series.get("currency") or "USD", "bars": series["bars"]}
        core.execute("INSERT INTO market_price_series_versions VALUES (?,?,?,?,?,?)",
                     (series["version_id"], f"market-price-series:{ref}", 1, ref, ticker, json.dumps(record)))
    events = [
        ("research-event:move-1008", "price_move", "2026-10-08T00:00:00+00:00", "market_price", {
            "as_of": "2026-10-08", "basket_members": 4, "basket_return_percent": "3.9860", "close": "208.36",
            "direction": "up", "excess_vs_basket_percent": "1.9741", "previous_close": "196.64",
            "return_percent": "5.9601", "benchmark_return_percent": None}),
        ("research-event:move-1008-dup", "price_move", "2026-10-08T00:00:00+00:00", "market_price", {
            "as_of": "2026-10-08", "basket_members": 4, "basket_return_percent": "3.9860", "close": "208.36",
            "direction": "up", "excess_vs_basket_percent": "1.9741", "previous_close": "196.64",
            "return_percent": "5.9601", "benchmark_return_percent": None}),
        ("research-event:gs-note", "sales_note", "2026-10-07T00:38:09+00:00", "vendor_note", {
            "document_ref": "sales-note:1a0e46c9c6ccfbf3", "source_ref": "source:sales-notes",
            "spec_ref": "sales-note", "title": None}),
        ("research-event:calendar", "calendar", "2026-10-08T00:00:00+00:00", "derived", {
            "event_kind": "earnings", "expected_date": "2026-10-01", "confirmed": True}),
    ]
    for event_id, kind, at, tier, payload in events:
        core.execute("INSERT INTO research_events VALUES (?,?,?,?,?,?)",
                     (event_id, ACN, kind, at, tier, json.dumps({"payload": payload, "source_refs": []})))
    core.execute("INSERT INTO event_judgements VALUES (?,?,?,?,?,?,?)",
                 ("event-judgement:acn-q4", "research-event:x", ACN, "NO_CHANGE", "research",
                  "2026-10-09T11:04:08+00:00", json.dumps({"because": NO_CHANGE_BECAUSE})))
    core.commit()
    core.close()
    for doc in json.loads((FIXTURE / "documents.json").read_text(encoding="utf-8")):
        index_text(state, document_ref=doc["document_ref"], kind=doc["kind"], text=doc["text"], date=doc["date"],
                   title=doc.get("title"), house=doc.get("house"), filed=doc.get("filed") or [],
                   source_ref=doc.get("source_ref") or "source:test", period=doc.get("period"), url=doc.get("url"))
    return state


def open_core(state: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{state / 'core.sqlite'}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def build(state: Path, question: str = QUESTION, **extra) -> dict:
    core = open_core(state)
    try:
        return ask_context.build_context(
            core, question=question, mission=MISSION, members=MEMBERS, today=extra.pop("today", TODAY),
            state_dir=state, theses=THESES, company_names=NAMES, label=lambda ref: TICKERS.get(ref, ref),
            numbers_fn=numbers_fixture, **extra)
    finally:
        core.close()


def tag_where(context: dict, *, block: str, contains: str = "", kind: str | None = None) -> str:
    for row in context["shown"]:
        if row["block"] != block or (kind and row.get("kind") != kind):
            continue
        if contains in (row.get("source_text") or row.get("statement") or ""):
            return row["tag"]
    raise AssertionError(f"no {block} row containing {contains!r}")


def golden_answer(context: dict) -> dict:
    """The contract-shaped answer an analyst would give, tags found by content."""

    move = tag_where(context, block="move", contains="单日 +5.96%")
    earnings_move = tag_where(context, block="move", contains="+15.78%")
    release = tag_where(context, block="passages", contains="New bookings of $22.2 billion", kind="sec_exhibit")
    call = tag_where(context, block="passages", contains="7% in local currency above the top end", kind="transcript")
    outlook = tag_where(context, block="passages", contains="$14.39 to $14.81", kind="sec_exhibit")
    bofa = tag_where(context, block="passages", contains="New bookings of $22.2B", kind="sales_note")
    preview = tag_where(context, block="passages", contains="3% - 4% in CC", kind="sales_note")
    thesis = tag_where(context, block="house_view", contains="当前论点")
    return {
        "what_happened": [
            {"text": "ACN 在 2026-10-08 收盘 208.36 美元，单日上涨 5.96%；同期覆盖同业等权 basket 上涨 3.99%，"
                     "超额 1.97 个百分点，当天的涨幅大半来自 IT 服务板块整体走强。", "refs": [move]},
            {"text": "问题所指的窗口内没有找到解释当天上涨的公司公告或券商快评。", "refs": [move]},
            {"text": "最近一次公司事件是 10 月 1 日发布的第四财季业绩：新签订单 222 亿美元，收入按本币增长 7%，"
                     "高于此前指引上沿。", "refs": [release, call]},
            {"text": "公司给出 FY27 本币收入增长 3% 至 6% 的指引，GAAP 稀释每股收益指引为 14.39 至 14.81 美元。",
             "refs": [outlook]},
            {"text": "财报当天股价单日上涨 15.78%。", "refs": [earnings_move]},
        ],
        "market_expected": [
            {"text": "Goldman Sachs 的会前预览预计 FY27 本币收入指引为 3% 至 4%，并认为投资者对这只股票的仓位偏空。",
             "refs": [preview]},
            {"text": "BofA 的销售快评称收入超过指引上沿、利润率更好，新签订单 22.2B 美元。", "refs": [bofa]},
        ],
        "our_view": {
            "house_view": "AI 带动的现代化与整合交付能部分抵消咨询疲弱，但中个位数本币增长仍是低信心假设。",
            "change": "strengthens",
            "sentences": [{"text": "我们认为 FY27 本币增长指引 3% 至 6% 的中值已接近中个位数，这加强了现代化抵消咨询疲弱的看法，"
                                   "但仍要看订单能否转化为收入。", "refs": [thesis, outlook]}],
        },
        "uncertainties": [{"text": "10 月 8 日板块普涨的直接原因还没有材料，需要当天的销售快评或新闻。", "refs": []}],
        "confidence": "medium", "refused": False, "refusal_reason": None, "refusal_detail": None,
        "unknowns": [{"what": "10 月 8 日 IT 服务板块上涨的原因", "content_kind": "sales_note",
                      "source": "sales-notes"}],
        "market_vs_us": None, "refresh_suggested": None,
    }


# ---------------------------------------------------------------------------
# 1. retrieval: dates, kinds, reranking, events, our own view
# ---------------------------------------------------------------------------

class RelativeDateTests(unittest.TestCase):
    DAYS = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]

    def test_yesterday_is_the_previous_trading_day_from_the_price_series(self):
        got = ask_context.resolve_dates("昨天为什么大涨", today="2026-10-09", trading_days=self.DAYS[:4])
        self.assertEqual((got["anchor"], got["basis"], got["single_day"]), ("2026-10-08", "trading_calendar", True))
        # Asked on a Monday, yesterday is Friday's session.
        got = ask_context.resolve_dates("昨天为什么跌", today="2026-10-12", trading_days=self.DAYS)
        self.assertEqual(got["anchor"], "2026-10-09")
        # Without a series, the weekday calendar, and it says so.
        got = ask_context.resolve_dates("yesterday", today="2026-10-12")
        self.assertEqual((got["anchor"], got["basis"]), ("2026-10-09", "weekdays"))

    def test_the_other_relative_words_resolve_to_windows(self):
        cases = {
            "上周发生了什么": ("2026-09-28", "2026-10-04"),
            "这周怎么样": ("2026-10-05", "2026-10-09"),
            "上个月的新闻": ("2026-09-01", "2026-09-30"),
            "近5天有什么消息": ("2026-10-04", "2026-10-09"),
            "最近有什么变化": ("2026-09-25", "2026-10-09"),
            "10月1日为什么大涨": ("2026-10-01", "2026-10-01"),
            "2026-09-30 的走势": ("2026-09-30", "2026-09-30"),
        }
        for question, window in cases.items():
            got = ask_context.resolve_dates(question, today="2026-10-09", trading_days=self.DAYS)
            self.assertEqual((got["date_from"], got["date_to"]), window, question)
            self.assertTrue(got["resolved"], question)
        got = ask_context.resolve_dates("前天", today="2026-10-09", trading_days=self.DAYS)
        self.assertEqual(got["anchor"], "2026-10-07")

    def test_no_date_words_take_the_kinds_default(self):
        got = ask_context.resolve_dates("ACN 为什么大涨", today="2026-10-09", trading_days=self.DAYS[:4],
                                        kind="why_move")
        self.assertEqual((got["anchor"], got["resolved"]), ("2026-10-08", False))
        got = ask_context.resolve_dates("ACN 估值贵不贵", today="2026-10-09", kind="valuation")
        self.assertEqual(got["date_from"], "2025-10-09")

    def test_a_single_day_window_reaches_back_to_the_previous_session(self):
        dates = ask_context.resolve_dates("昨天", today="2026-10-09", trading_days=self.DAYS[:4])
        windows = ask_context.retrieval_windows("why_move", dates, self.DAYS[:4])
        self.assertEqual(windows["primary"], {"date_from": "2026-10-07", "date_to": "2026-10-08"})
        self.assertEqual(windows["context"]["date_from"], "2026-09-24")


class QuestionKindTests(unittest.TestCase):
    CASES = (
        ("关于 Accenture ACN：昨天股价为什么大涨？", "why_move"),
        ("Why did ACN rally yesterday?", "why_move"),
        ("ACN 跌了这么多是什么原因", "why_move"),
        ("ACN 上周发生了什么？", "what_happened"),
        ("EPAM 有什么新消息", "what_happened"),
        ("ACN 和 EPAM 相比增长谁更快", "compare"),
        ("你怎么看 ACN 现在的位置？", "view"),
        ("ACN 的估值现在贵不贵？", "valuation"),
        ("ACN 最大的下行风险是什么", "risk"),
        ("ACN 的 FY27 指引意味着什么增长", "forecast"),
        ("ACN 下次财报是什么时候？", "catalyst"),
        ("ACN 上季度的新签订单是多少？", "fact"),
    )

    def test_each_question_is_read_as_its_kind_with_the_word_that_decided_it(self):
        for question, kind in self.CASES:
            got = ask_context.question_kind(question)
            self.assertEqual(got["kind"], kind, question)
            self.assertTrue(got["matched"], question)

    def test_only_view_and_debate_demand_a_variant_view(self):
        self.assertEqual(ask_context.VIEW_KINDS, frozenset({"view", "debate"}))


class RetrievalRankingTests(unittest.TestCase):
    """Reranking on a synthetic corpus: windows, kind weights, our own material down-weighted."""

    WIDGET = "company:ticker:synw"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        fill = " Clients kept spending on transformation programmes across regions." * 12
        add = lambda ref, kind, date, text: index_text(self.state, document_ref=ref, kind=kind, text=text + fill,
                                                       date=date, title=ref, filed=[self.WIDGET])
        add("doc:old-release", "sec_exhibit", "2026-06-01", "Synthetic Widgets results: revenue grew, guidance raised.")
        add("doc:new-note", "sales_note", "2026-10-08", "Synthetic Widgets shares rose sharply; results and guidance.")
        add("doc:wiki", "company_wiki", "2026-10-08", "Synthetic Widgets shares rose; results guidance notes.")
        add("doc:broker", "broker_report", "2026-08-01", "Synthetic Widgets valuation: price target and multiple.")
        add("doc:sales-val", "sales_note", "2026-08-01", "Synthetic Widgets valuation: price target and multiple.")

    def windows(self, kind, question):
        dates = ask_context.resolve_dates(question, today="2026-10-09", kind=kind)
        return ask_context.retrieval_windows(kind, dates, [])

    def test_a_move_question_puts_the_window_first_and_our_own_notes_last(self):
        got = ask_context.retrieve(self.state, question="昨天股价为什么大涨", kind="why_move", company_ref=self.WIDGET,
                                   company_terms=["Synthetic Widgets"], windows=self.windows("why_move", "昨天"))
        order = [row["document_ref"] for row in got["results"]]
        self.assertEqual(order[0], "doc:new-note")
        self.assertLess(order.index("doc:new-note"), order.index("doc:wiki"))
        self.assertTrue(got["results"][0]["in_window"])

    def test_a_valuation_question_weights_the_broker_above_the_sales_desk(self):
        got = ask_context.retrieve(self.state, question="估值贵不贵", kind="valuation", company_ref=self.WIDGET,
                                   company_terms=["Synthetic Widgets"], windows=self.windows("valuation", "估值"))
        order = [row["document_ref"] for row in got["results"]]
        self.assertLess(order.index("doc:broker"), order.index("doc:sales-val"))

    def test_no_index_is_said_plainly(self):
        empty = Path(self.temp.name) / "nothing"
        empty.mkdir()
        got = ask_context.retrieve(empty, question="昨天为什么涨", kind="why_move", company_ref=self.WIDGET,
                                   company_terms=["Synthetic Widgets"], windows=self.windows("why_move", "昨天"))
        self.assertEqual((got["status"], got["results"]), ("no_index", []))

    def test_question_terms_carry_english_glossary_forms_without_the_company_name(self):
        terms = ask_context.query_terms("Accenture 的新签订单和指引怎么样", names=["Accenture"])
        self.assertIn("bookings", terms)
        self.assertIn("guidance", terms)
        self.assertNotIn("Accenture", terms)


class GoldenContextTests(unittest.TestCase):
    """「关于 Accenture ACN：昨天股价为什么大涨？」 asked on 2026-10-09: what the analyst has open."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.state = golden_state(Path(cls.temp.name))
        cls.context = build(cls.state)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_yesterday_resolves_to_2026_10_08_and_the_question_is_a_move_question(self):
        self.assertEqual(self.context["question_kind"], "why_move")
        self.assertEqual(self.context["dates"]["anchor"], "2026-10-08")
        self.assertEqual(self.context["dates"]["basis"], "trading_calendar")

    def test_the_move_is_computed_with_the_basket_and_the_excess(self):
        move = next(row for row in self.context["shown"] if row["block"] == "move")
        self.assertIn("单日 +5.96%", move["statement"])
        self.assertIn("basket（IBM、CTSH、EPAM、DXC）+3.99%", move["statement"])
        self.assertIn("超额 +1.97 个百分点", move["statement"])
        self.assertIn("宽基基准不可得", move["statement"])
        # The earnings day a week earlier travels with it.
        self.assertTrue(any("2026-10-01" in row["statement"] and "+15.78%" in row["statement"]
                            for row in self.context["shown"] if row["block"] == "move"))

    def test_events_render_their_moves_and_document_titles(self):
        events = [row["statement"] for row in self.context["shown"] if row["block"] == "events"]
        self.assertTrue(any("上涨 +5.96%" in text and "超额 +1.97 个百分点" in text for text in events), events)
        self.assertTrue(any("Goldman Sachs" in text and "《" in text for text in events), events)
        self.assertEqual(sum("上涨 +5.96%" in text for text in events), 1)       # duplicates folded
        self.assertFalse(any("price_move：price_move" in text for text in events))

    def test_the_earnings_material_surfaces_first_hand_and_from_the_street(self):
        passages = [row for row in self.context["shown"] if row["block"] == "passages"]
        texts = " ".join(row["source_text"] for row in passages)
        for figure in ("$22.2 billion", "7% in local currency", "3% to 6%", "$14.39 to $14.81"):
            self.assertIn(figure, texts)
        kinds = {row["kind"] for row in passages}
        self.assertTrue({"sec_exhibit", "transcript"} <= kinds, kinds)
        self.assertIn("sales_note", kinds)
        first = passages[0]
        self.assertTrue(first["first_hand"] or first["street"], first)

    def test_our_own_view_is_labelled_current_only_and_kept_apart(self):
        ours = [row for row in self.context["shown"] if row["block"] == "house_view"]
        self.assertTrue(any("当前论点" in row["statement"] for row in ours))
        self.assertFalse(any("SUPERSEDED" in row["statement"] for row in ours))
        self.assertTrue(all(not row["evidence"] for row in ours))
        self.assertIn("我们的看法", ask_context.BLOCK_LABELS["house_view"])

    def test_no_first_hand_material_inside_the_window_asks_for_it(self):
        sufficiency = self.context["sufficiency"]
        self.assertEqual(sufficiency["window"], {"date_from": "2026-10-07", "date_to": "2026-10-08"})
        self.assertEqual(sufficiency["status"], "low")
        request = sufficiency["request"]
        self.assertEqual(request["subject"], ACN)
        self.assertEqual(request["need"]["kind"], "answer")
        self.assertIn("sales_note", request["need"]["doc_kinds"])
        self.assertIn("2026-10-08", request["what"])
        self.assertIn("已尝试", "已尝试：" + sufficiency["tried"])

    def test_legacy_claims_only_when_no_passage_exists(self):
        claims_block = next(b for b in self.context["blocks"] if b["block"] == "claims")
        self.assertEqual((claims_block["available"], claims_block["reason"]), (False, "passages_found"))

    def test_the_numbers_pack_rows_are_bound_to_their_pointers(self):
        numbers = [row for row in self.context["shown"] if row["block"] == "numbers"]
        self.assertTrue(numbers)
        self.assertTrue(all(row["ref"].startswith("pack-json:") for row in numbers))
        self.assertTrue(any(row["source"] == "consensus" for row in numbers))
        self.assertTrue(self.context["material_packs"][0]["record"]["recipe_version"].startswith("ask:"))

    def test_the_prompt_is_bounded_and_carries_no_bulk_claims(self):
        prompt = ask_answer.build_prompt(self.context, mission=MISSION)
        self.assertLess(len(prompt), 75_000)
        self.assertIn("发生了什么", prompt)
        self.assertIn("问题所指的时间：上一个交易日 2026-10-08", prompt)
        self.assertNotIn("账本里的结论", prompt)

    def test_the_same_question_builds_the_same_context(self):
        self.assertEqual(build(self.state)["context_hash"], self.context["context_hash"])


# ---------------------------------------------------------------------------
# 2 + 4. the answer contract and the golden replay
# ---------------------------------------------------------------------------

class GoldenReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.state = golden_state(Path(cls.temp.name))
        cls.context = build(cls.state)
        spec = cls.context["sufficiency"]["request"]
        opened = mr.open_request(cls.state, requester={"lane": "ask", "product_ref": "cockpit-ask:golden"},
                                 subject=spec["subject"], need=spec["need"], period=spec["period"],
                                 acceptance=spec["acceptance"], actor_ref=OWNER, now="2026-10-09T09:00:00+00:00")
        cls.requests = [{"id": opened["id"], "status": opened["status"], "status_label": opened["status_label"],
                         "what": spec["what"]}]
        cls.answer = ask_answer.parse_answer(golden_answer(cls.context), context=cls.context,
                                             resolve_passage=ei.passage_resolver(cls.state),
                                             material_requests=cls.requests)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_the_golden_answer_passes_every_deterministic_check(self):
        self.assertTrue(self.answer["verification"]["passed"], self.answer["verification"]["findings"])
        statuses = {c["check"]: c["status"] for c in self.answer["verification"]["checks"]}
        for check in ("citations_resolve", "numbers_sourced", "structure_present", "what_happened_cites_evidence",
                      "our_view_not_used_as_evidence", "reasoning_lint", "material_gap_has_request"):
            self.assertEqual(statuses[check], "pass", check)

    def test_it_cites_two_first_hand_passages_and_a_street_note(self):
        cited = self.answer["citations"]
        first_hand = [c for c in cited if c["block"] == "passages" and c["first_hand"]]
        street = [c for c in cited if c["block"] == "passages" and c["street"]]
        self.assertGreaterEqual(len(first_hand), 2)
        self.assertGreaterEqual(len(street), 1)

    def test_the_body_reads_in_four_parts_with_the_move_the_print_and_the_house_view(self):
        body = self.answer["answer"]
        positions = [body.index(title) for title in ("发生了什么", "市场原本预期什么", "我们的看法", "不确定点")]
        self.assertEqual(positions, sorted(positions))
        for fact in ("5.96%", "3.99%", "222 亿美元", "7%", "3% 至 6%", "14.39 至 14.81", "Goldman Sachs"):
            self.assertIn(fact, body)
        self.assertIn("当前看法：", body)
        self.assertIn(ask_answer.VIEW_CHANGE_LABELS["strengthens"], body)
        self.assertIn("正在获取：", body)
        self.assertIn("已尝试：", body)
        self.assertIn(self.requests[0]["id"].rsplit(":", 1)[-1][:8], body)

    def test_a_replay_of_the_golden_answer_is_identical(self):
        again = ask_answer.parse_answer(golden_answer(self.context), context=self.context,
                                        resolve_passage=ei.passage_resolver(self.state),
                                        material_requests=self.requests)
        self.assertEqual(again["answer"], self.answer["answer"])
        self.assertEqual(again["verification"], self.answer["verification"])


class AnswerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.state = golden_state(Path(cls.temp.name))
        cls.context = build(cls.state)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def parse(self, payload, **extra):
        return ask_answer.parse_answer(payload, context=self.context,
                                       resolve_passage=ei.passage_resolver(self.state), **extra)

    def failed(self, payload, **extra):
        result = self.parse(payload, **extra)
        return {c["check"] for c in result["verification"]["checks"] if c["status"] == "fail"}, result

    def test_restating_our_no_change_judgement_as_evidence_fails(self):
        payload = golden_answer(self.context)
        ours = tag_where(self.context, block="house_view", contains="事件研判")
        payload["what_happened"].append({"text": "我们的事件研判维持两条论点不变。", "refs": [ours]})
        failed, result = self.failed(payload, material_requests=[{"id": "material-request:" + "a" * 32}])
        self.assertIn("our_view_not_used_as_evidence", failed)
        self.assertIn("what_happened_cites_evidence", failed)
        codes = {f["code"] for f in result["verification"]["findings"]}
        self.assertIn("our_view_cited_as_evidence", codes)

    def test_a_number_not_in_the_cited_material_fails_and_a_declared_derivation_passes(self):
        payload = golden_answer(self.context)
        move = tag_where(self.context, block="move", contains="单日 +5.96%")
        payload["what_happened"].append({"text": "超额收益约 2.5 个百分点。", "refs": [move]})
        failed, _ = self.failed(payload, material_requests=[{"id": "material-request:" + "a" * 32}])
        self.assertIn("numbers_sourced", failed)
        payload = golden_answer(self.context)
        payload["what_happened"].append({"text": "按两天收盘价计算的单日涨幅为 5.96%。", "refs": [move],
                                         "basis": "derivation",
                                         "derivation": {"expression": "208.36/196.64*100-100", "result": "5.96"}})
        failed, _ = self.failed(payload, material_requests=[{"id": "material-request:" + "a" * 32}])
        self.assertNotIn("numbers_sourced", failed)
        payload["our_view"]["sentences"].append({"text": "我们的估计是 FY27 本币增长 5%。", "refs": [],
                                                 "basis": "our_estimate"})
        failed, _ = self.failed(payload, material_requests=[{"id": "material-request:" + "a" * 32}])
        self.assertNotIn("numbers_sourced", failed)

    def test_a_missing_part_or_view_change_fails_the_structure(self):
        payload = golden_answer(self.context)
        payload["market_expected"] = []
        payload["our_view"]["change"] = "maybe"
        failed, result = self.failed(payload, material_requests=[{"id": "material-request:" + "a" * 32}])
        self.assertIn("structure_present", failed)
        parts = {f.get("part") for f in result["verification"]["findings"] if f["code"] in
                 ("part_missing", "view_change_not_stated")}
        self.assertEqual(parts, {"market_expected", "our_view"})

    def test_one_broker_called_a_consensus_and_an_attribution_without_a_benchmark_are_linted(self):
        payload = golden_answer(self.context)
        preview = tag_where(self.context, block="passages", contains="3% - 4% in CC", kind="sales_note")
        release = tag_where(self.context, block="passages", contains="New bookings of $22.2 billion",
                            kind="sec_exhibit")
        payload["market_expected"].append({"text": "市场一致预期是 FY27 增长 3% 至 4%。", "refs": [preview]})
        payload["what_happened"].append({"text": "股价大涨是因为业绩超预期。", "refs": [release]})
        failed, result = self.failed(payload, material_requests=[{"id": "material-request:" + "a" * 32}])
        self.assertIn("reasoning_lint", failed)
        rules = {f["rule"] for f in result["verification"]["findings"] if f["code"].startswith("lint:")}
        self.assertTrue({"consensus_without_authority", "price_move_without_benchmark"} <= rules, rules)

    def test_a_consensus_row_from_the_numbers_pack_may_be_called_consensus(self):
        payload = golden_answer(self.context)
        consensus = next(row["tag"] for row in self.context["shown"] if row["block"] == "numbers"
                         and row.get("source") == "consensus" and "EPS" in row["statement"])
        payload["market_expected"].append({"text": "数据商一致预期给出了下一财年的每股收益预测。", "refs": [consensus]})
        failed, _ = self.failed(payload, material_requests=[{"id": "material-request:" + "a" * 32}])
        self.assertNotIn("reasoning_lint", failed)

    def test_a_tampered_passage_does_not_resolve(self):
        payload = golden_answer(self.context)
        result = ask_answer.parse_answer(payload, context=self.context, resolve_passage=lambda ref: None,
                                         material_requests=[{"id": "material-request:" + "a" * 32}])
        statuses = {c["check"]: c["status"] for c in result["verification"]["checks"]}
        self.assertEqual(statuses["citations_resolve"], "fail")

    def test_a_gap_without_a_request_fails(self):
        failed, _ = self.failed(golden_answer(self.context), material_requests=[])
        self.assertIn("material_gap_has_request", failed)

    def test_an_unshown_tag_is_removed_and_reported(self):
        payload = golden_answer(self.context)
        payload["what_happened"][0]["refs"].append("P999")
        failed, result = self.failed(payload, material_requests=[{"id": "material-request:" + "a" * 32}])
        self.assertIn("cites_only_shown_rows", failed)
        self.assertNotIn("P999", result["cited_tags"])

    def test_an_old_flat_reply_still_parses_and_is_marked_as_missing_parts(self):
        failed, result = self.failed({"sentences": [{"text": "ACN 涨了。", "refs": []}], "confidence": "low"},
                                     material_requests=[{"id": "material-request:" + "a" * 32}])
        self.assertIn("structure_present", failed)
        self.assertEqual(result["answer"], "ACN 涨了。")        # the old body, unchanged
        self.assertEqual(result["sections"][0]["body"], "ACN 涨了。")


# ---------------------------------------------------------------------------
# follow-ups carry the previous turn's passages
# ---------------------------------------------------------------------------

class FollowUpPassageTests(unittest.TestCase):
    def test_a_follow_up_re_resolves_and_shows_the_passages_the_previous_answer_cited(self):
        with tempfile.TemporaryDirectory() as temp:
            state = golden_state(Path(temp))
            first = build(state)
            answer = ask_answer.parse_answer(golden_answer(first), context=first)
            job = {"job_id": "cockpit-job:first", "request": {"question": QUESTION},
                   "result": {"answer": answer["answer"], "citations": answer["citations"],
                              "context": {"question_kind": first["question_kind"]}}}
            previous = ask_context.previous_turn(job)
            self.assertTrue(previous["passage_refs"])
            follow = build(state, question="那利润率呢？", previous=previous, subject_hint=QUESTION)
            carried = [row for row in follow["blocks"] if row["block"] == "passages"][0]["rows"]
            self.assertEqual(follow["carried_passages"], len(previous["passage_refs"]))
            self.assertTrue(any(row["detail"]["carried"] for row in carried))
            self.assertIn("上一回答引用过", ask_context.render_context(follow))
            self.assertEqual(follow["subjects"]["companies"], [ACN])


# ---------------------------------------------------------------------------
# 3. the material loop
# ---------------------------------------------------------------------------

class ResumeStateTests(unittest.TestCase):
    RESULT = {"material_requests": [{"id": "material-request:aa"}, {"id": "material-request:bb"}]}

    def test_resume_states(self):
        state = ask_refresh.resume_state({}, {})
        self.assertEqual(state["state"], "not_needed")
        state = ask_refresh.resume_state(self.RESULT, {"material-request:aa": {"status": "open"},
                                                       "material-request:bb": {"status": "open"}})
        self.assertEqual(state["state"], "waiting")
        state = ask_refresh.resume_state(self.RESULT, {"material-request:aa": {"status": "found"},
                                                       "material-request:bb": {"status": "open"}})
        self.assertEqual((state["state"], state["ready"]), ("ready", ["material-request:aa"]))
        state = ask_refresh.resume_state(self.RESULT, {"material-request:aa": {"status": "found"},
                                                       "material-request:bb": {"status": "open"}},
                                         resumed=["material-request:aa"])
        self.assertEqual(state["state"], "waiting")              # one resume per request
        state = ask_refresh.resume_state(self.RESULT, {}, resumed=["material-request:aa", "material-request:bb"])
        self.assertEqual(state["state"], "already_resumed")
        state = ask_refresh.resume_state(self.RESULT, {"material-request:aa": {"status": "exhausted"},
                                                       "material-request:bb": {"status": "timeout"}})
        self.assertEqual(state["state"], "exhausted")

    def test_new_passages_measures_against_what_the_answer_read(self):
        before = {"shown": [{"block": "passages", "ref": "doc-passage:a:0-10:" + "0" * 12}]}
        after = {"shown": [{"block": "passages", "ref": "doc-passage:a:0-10:" + "0" * 12},
                           {"block": "passages", "ref": "doc-passage:b:0-10:" + "1" * 12},
                           {"block": "move", "ref": "x"}]}
        self.assertEqual([row["ref"] for row in ask_refresh.new_passages(before, after)],
                         ["doc-passage:b:0-10:" + "1" * 12])
        stored = {"context": {"passage_refs": ["doc-passage:a:0-10:" + "0" * 12]}}
        self.assertEqual(len(ask_refresh.new_passages(stored, after)), 1)
        self.assertEqual(ask_refresh.new_passages({"shown": after["shown"]}, after), [])



# ---------------------------------------------------------------------------
# 3. the material loop through the cockpit: request, chip, resume, no re-billing
# ---------------------------------------------------------------------------

def four_part_reply(text: str = "窗口内没有解释当天变动的材料。") -> str:
    return json.dumps({
        "what_happened": [{"text": text, "refs": []}],
        "market_expected": [{"text": "展示的材料里没有会前预期。", "refs": []}],
        "our_view": {"house_view": "暂无成形看法。", "change": "unclear",
                     "sentences": [{"text": "我们认为需要等当天的材料。", "refs": []}]},
        "uncertainties": [{"text": "当天上涨的原因还不清楚。", "refs": []}],
        "confidence": "low", "refused": False, "refusal_reason": None, "refusal_detail": None,
        "unknowns": [], "market_vs_us": None, "refresh_suggested": None,
    }, ensure_ascii=False)


class CockpitMaterialLoopTests(unittest.TestCase):
    QUESTION = "关于 Accenture ACN：昨天股价为什么大涨？"

    def setUp(self):
        from unittest import mock

        from dalton_core.cockpit_model import CockpitModel
        from tests.test_cockpit_plane import CockpitHarness

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.c = CockpitHarness(self.root)
        self.addCleanup(self.c.close)
        self.login = "owner@example.com"
        self.today = self.c.h.h.clock().date()
        older = (self.today - __import__("datetime").timedelta(days=8)).isoformat()
        index_text(self.root, document_ref="test:release", kind="sec_exhibit", date=older, filed=[ACN],
                   title="Accenture 8-K Exhibit 99.1",
                   text=("Accenture reported revenue growth of 7% in local currency and new bookings of $22.2 billion. "
                         "Clients kept spending on transformation programmes across regions. " * 8))
        self.ops: list[str] = []
        original = self.c.plane.governance_call

        def governance(token_config, socket, *, actor_ref, operation, params):
            self.ops.append(operation)
            if operation == "open_ask_material_request":
                view = mr.open_request(self.root, requester={"lane": "ask", "product_ref": params["product_ref"],
                                                             "run_ref": params.get("run_ref"),
                                                             "section": "uncertainties"},
                                       subject=params["subject"], need=params["need"], period=params.get("period"),
                                       acceptance=params.get("acceptance"), actor_ref=OWNER)
                return {"status": view["write_status"], "request": view}
            return original(token_config, socket, actor_ref=actor_ref, operation=operation, params=params)

        self.c.plane.governance_call = governance
        self.calls = 0
        real_call = CockpitModel.call

        def counting(model, **kwargs):
            self.calls += 1
            return real_call(model, **kwargs)

        patcher = mock.patch.object(CockpitModel, "call", counting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ask(self, request_id: str = "g1") -> dict:
        self.c.reply = four_part_reply()
        job = self.c.plane.ask(self.login, {"question": self.QUESTION, "request_id": request_id})
        done = self.c.wait(job["job_id"], self.login)
        self.assertEqual(done["status"], "done", done["error"])
        return done

    def resume(self, job_id: str) -> dict:
        job = self.c.plane.resume_ask(self.login, {"job_id": job_id})
        done = self.c.wait(job["job_id"], self.login)
        self.assertEqual(done["status"], "done", done["error"])
        return done

    def test_a_question_without_first_hand_material_opens_a_request_and_says_so(self):
        done = self.ask()
        result = done["result"]
        self.assertIn("open_ask_material_request", self.ops)
        self.assertEqual(result["sufficiency"]["status"], "low")
        request = result["material_requests"][0]
        self.assertTrue(request["id"].startswith("material-request:"))
        self.assertEqual(request["status"], "open")
        self.assertIn("正在获取：", result["answer"])
        self.assertIn("已尝试：", result["answer"])
        statuses = {c["check"]: c["status"] for c in result["verification"]["checks"]}
        self.assertEqual(statuses["material_gap_has_request"], "pass")
        ledger = mr.request(self.root, request["id"])
        self.assertEqual(ledger["requesters"][0]["lane"], "ask")
        self.assertEqual(ledger["need"]["kind"], "answer")
        self.assertNotIn("cost", json.dumps(result.get("sections"), ensure_ascii=False))

    def test_resume_waits_for_the_request_then_charges_nothing_when_nothing_is_new(self):
        from dalton_core.cockpit_plane import CockpitConflict

        done = self.ask()
        request_id = done["result"]["material_requests"][0]["id"]
        with self.assertRaises(CockpitConflict):
            self.c.plane.resume_ask(self.login, {"job_id": done["job_id"]})       # still being fetched
        mr.close(self.root, request_id, status="found", reason="test: found", actor_ref=OWNER)
        before = self.calls
        resumed = self.resume(done["job_id"])
        self.assertEqual(resumed["result"]["resume"]["status"], "nothing_new")
        self.assertEqual(self.calls, before)                                        # no second bill
        self.assertEqual(resumed["result"]["cost_usd"], 0.0)
        # Nothing was consumed: once the material is indexed the resume is still there.
        self.assertEqual(self.c.plane.ask_resume_state(self.login, done["job_id"])["state"], "ready")

    def test_resume_over_new_material_answers_once_per_request(self):
        from dalton_core.cockpit_plane import CockpitConflict

        done = self.ask()
        request_id = done["result"]["material_requests"][0]["id"]
        mr.close(self.root, request_id, status="found", reason="test: found", actor_ref=OWNER)
        yesterday = done["result"]["sufficiency"]["window"]["date_to"]
        index_text(self.root, document_ref="test:note", kind="sales_note", date=yesterday, house="BofA Securities",
                   title="BofA TMT: Accenture",
                   text=("Accenture shares rose as IT services rallied after a peer's results. " * 10))
        before = self.calls
        self.c.reply = four_part_reply("新取到的销售快评说明了当天的板块走势。")
        resumed = self.resume(done["job_id"])
        self.assertEqual(self.calls, before + 1)
        self.assertEqual(resumed["result"]["resume"]["status"], "answered")
        self.assertGreaterEqual(resumed["result"]["resume"]["new_passages"], 1)
        self.assertEqual(resumed["request"]["resume_of"], done["job_id"])
        self.assertIn("新取到的销售快评", resumed["result"]["answer"])
        self.assertEqual(resumed["result"]["material_requests"][0]["status"], "found")
        self.assertIn("已找到：", resumed["result"]["answer"])
        self.assertEqual(self.c.plane.ask_resume_state(self.login, done["job_id"])["state"], "already_resumed")
        with self.assertRaises(CockpitConflict):
            self.c.plane.resume_ask(self.login, {"job_id": done["job_id"]})

    def test_the_material_requests_page_endpoint_reports_status_read_only(self):
        from dalton_core.cockpit_v2 import material_requests_api as api
        from dalton_core.cockpit_v2.charts_api import ChartReader

        done = self.ask()
        request_id = done["result"]["material_requests"][0]["id"]
        route = api.material_requests_routes()[api.PATH]
        status, _, body = route({"ids": request_id}, ChartReader(self.root / "core.sqlite"))
        payload = json.loads(body)
        self.assertEqual((status, payload["status"]), (200, "ok"), payload)
        self.assertEqual(payload["requests"][0]["status"], "open")
        self.assertFalse(payload["requests"][0]["resolved"])
        self.assertNotIn("requesters", payload["requests"][0])
        status, _, body = route({"company": "ACN"}, ChartReader(self.root / "core.sqlite"))
        self.assertEqual(json.loads(body)["counts"]["open"], 1)
        status, _, _ = route({"ids": "not-a-ref"}, ChartReader(self.root / "core.sqlite"))
        self.assertEqual(status, 400)


class WriterOpTests(unittest.TestCase):
    """``open_ask_material_request``: a person's door, mission-scoped, idempotent."""

    def call(self, state, params):
        from datetime import datetime, timezone
        from types import SimpleNamespace

        from dalton_core.writer_server import HUMAN_GOVERNANCE_OPERATIONS, OPERATION_FIELDS, WriterServer

        self.assertIn("open_ask_material_request", HUMAN_GOVERNANCE_OPERATIONS)
        self.assertLessEqual(set(params), OPERATION_FIELDS["open_ask_material_request"])
        fake = SimpleNamespace(state_dir=state, _active_universe_refs=lambda: {ACN},
                               _now=lambda: datetime(2026, 10, 9, 9, tzinfo=timezone.utc))
        return WriterServer._op_open_ask_material_request(fake, params)

    def test_opens_once_and_refuses_automation_and_outsiders(self):
        from dalton_core.writer_server import ValidationError

        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            params = {"subject": ACN, "need": {"kind": "answer", "question": QUESTION, "doc_kinds": ["sales_note"]},
                      "period": "2026-10-07..2026-10-08", "acceptance": {"min_passages": 1},
                      "product_ref": "cockpit-ask:r1", "run_ref": "r1", "actor_ref": "human:tailscale-x"}
            first = self.call(state, params)
            self.assertEqual(first["status"], "fresh")
            again = self.call(state, params)
            self.assertEqual((again["status"], again["request"]["id"]), ("duplicate", first["request"]["id"]))
            self.assertEqual(mr.request(state, first["request"]["id"])["requesters"][0]["lane"], "ask")
            with self.assertRaises(ValidationError):
                self.call(state, {**params, "actor_ref": "automation:x"})
            with self.assertRaises(ValidationError):
                self.call(state, {**params, "subject": "company:ticker:zzzz"})
            with self.assertRaises(ValidationError):
                self.call(state, {**params, "need": {"kind": "document", "doc_kinds": ["sec_8k"]}})

# ---------------------------------------------------------------------------
# 5. the page: four parts, the 「正在获取」 chip, the resume, no fee text
# ---------------------------------------------------------------------------

import shutil  # noqa: E402

NODE = shutil.which("node") is not None
ANSWER = {
    "display_answer": None,
    "sections": [
        {"key": "what_happened", "title": "发生了什么", "body": "ACN 单日上涨 5.96%，同业 basket 上涨 3.99%。"},
        {"key": "market_expected", "title": "市场原本预期什么", "body": "Goldman Sachs 预计指引 3% 至 4%。"},
        {"key": "our_view", "title": "我们的看法", "body": "当前看法：中个位数增长仍是低信心假设。是否改变：这次的材料加强了这个看法。"},
        {"key": "uncertainties", "title": "不确定点", "body": "当天板块上涨的原因还不清楚。\n正在获取：解释 2026-10-08 股价变动的材料（材料请求编号 75aa26ce，状态：待获取）"},
    ],
    "material_requests": [{"id": "material-request:" + "7" * 32, "status": "open", "status_label": "待获取",
                           "what": "解释 2026-10-08 股价变动的材料"}],
    "citations": [], "confidence": "medium", "cost_usd": 0.31,
}


@unittest.skipUnless(NODE, "node is not installed")
class AskPageBatchGTests(unittest.TestCase):
    def run_page(self, steps, *, requests_status="found", **extra):
        from tests.test_cockpit_analyst_ux_2026_10_09 import fixtures
        from tests.test_cockpit_v2_shell import node

        history = {"items": [{"job_id": "cockpit-job:ggg", "status": "done", "created_at": "2026-10-09T03:00:00+00:00",
                              "request": {"question": QUESTION, "request_id": "r-g"}, "result": ANSWER}]}
        live = {"kind": "material_requests", "status": "ok", "requests": [
            {"id": "material-request:" + "7" * 32, "status": requests_status,
             "status_label": {"found": "已找到", "open": "待获取"}[requests_status], "resolved": requests_status == "found"}]}
        return node("pages", {"fixtures": fixtures(**{"/v1/cockpit/history?kind=ask": history,
                                                     "/v2/data/research/material-requests": live}),
                              "render": {"a": "#/ask"}, "steps": [{"page": "a", **s} for s in steps], **extra})

    def test_the_answer_reads_in_four_parts_with_a_polled_request_chip_and_no_fee(self):
        out = self.run_page([{"op": "wait"}])
        text = out["results"]["a"]["text"]
        for title in ("发生了什么", "市场原本预期什么", "我们的看法", "不确定点"):
            self.assertIn(title, text)
        self.assertIn("已取到：解释 2026-10-08 股价变动的材料（已找到）", text)        # polled status
        self.assertIn("用新材料续答", text)
        self.assertNotIn("US$", text)
        self.assertNotIn("0.31", text)
        self.assertNotIn("费用", text)

    def test_resume_posts_the_job_and_shows_the_new_answer(self):
        resumed = {**ANSWER, "resume": {"status": "answered", "label": "已用新取到的材料重新作答"},
                   "sections": [{"key": "what_happened", "title": "发生了什么", "body": "新材料说明了板块普涨。"}]}
        out = self.run_page([{"op": "click", "text": "用新材料续答"}, {"op": "wait"}],
                            postReplies={"/v1/cockpit/ask_resume": {"job_id": "cockpit-job:resumed"}},
                            jobResult=resumed)
        sent = [body for path, body in out["posted"] if path == "/v1/cockpit/ask_resume"]
        self.assertEqual(sent, [{"job_id": "cockpit-job:ggg"}])
        after = out["results"]["a:after"]["text"]
        self.assertIn("已用新取到的材料重新作答", after)
        self.assertIn("新材料说明了板块普涨。", after)

    def test_pure_helpers(self):
        from tests.test_cockpit_v2_shell import node

        r = node("call", {"module": "pages/ask.js", "calls": [
            ["answerSections", {"display_answer": "发生了什么\n涨了。\n我们的看法\n维持。"}],
            ["requestChips", ANSWER, {"material-request:" + "7" * 32: {"status": "exhausted", "status_label": "来源已用尽"}}]]})
        self.assertEqual(r[0]["ok"], [{"title": "发生了什么", "body": "涨了。"}, {"title": "我们的看法", "body": "维持。"}])
        self.assertEqual(r[1]["ok"][0]["text"], "来源已用尽：解释 2026-10-08 股价变动的材料（来源已用尽）")
        self.assertFalse(r[1]["ok"][0]["resolved"])

if __name__ == "__main__":
    unittest.main()

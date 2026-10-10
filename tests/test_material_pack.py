"""Batch C: material pack, MaterialRequest ledger, sufficiency, redraft thresholds.

A synthetic company (Synthetic Widgets, SYNW -- not any covered company) with
small real-shaped documents of every kind the pack reads: four quarterly call
transcripts with prepared remarks and Q&A, an 8-K Exhibit 99.1, a 10-K, broker
reports from bull and bear houses plus a multi-company sector weekly, sales
notes and an expert call -- indexed through ``evidence_index.index_document``
exactly as the indexer stores them.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from dalton_core import evidence_index as ei
from dalton_core import material_pack as mp
from dalton_core import material_requests as mr
from dalton_core import material_sufficiency as ms
from dalton_core import redraft_policy as rp
from dalton_core.evidence_corpus import DocumentBody

SYNW = "company:ticker:synw"
OTHER = "company:ticker:othr"
MISSION = {"id": "coverage-mission-version:test:1", "mission_ref": "coverage-mission:test", "version": 1,
           "industry_ref": "industry:widgets",
           "autonomy": {"automation_principal": "automation:coverage-mission", "human_checkpoints": []},
           "universe": [{"company_ref": SYNW, "ticker": "SYNW", "name": "Synthetic Widgets Co."},
                        {"company_ref": OTHER, "ticker": "OTHR", "name": "Other Gadgets Inc."}]}
FILL = ("Clients keep prioritising large transformation programmes and managed services, and the "
        "pipeline stays healthy across regions. ")
CORE_SCHEMA = """
CREATE TABLE coverage_mission_versions(mission_version_id TEXT PRIMARY KEY, mission_ref TEXT,
    version_number INTEGER, record_json TEXT);
CREATE TABLE coverage_mission_pointer(mission_ref TEXT PRIMARY KEY, mission_version_id TEXT, updated_at TEXT);
CREATE TABLE claim_versions(claim_version_id TEXT PRIMARY KEY, claim_ref TEXT, version_number INTEGER,
    claim_json TEXT, content_hash TEXT, prior_version_id TEXT, created_at TEXT);
CREATE TABLE company_dossier_versions(version_id TEXT PRIMARY KEY, dossier_ref TEXT, version_number INTEGER,
    company_ref TEXT, record_json TEXT, created_at TEXT);
"""


def transcript(quarter: str, year: int) -> str:
    prepared = (f"发言人Operator： Welcome to the Synthetic Widgets {quarter} fiscal {year} earnings call.\n"
                f"发言人Jane Doe： Thank you. {FILL * 6} Bookings were $5.1 billion in {quarter}. {FILL * 6}\n"
                f"发言人John Roe： Revenue grew 6% in local currency. For fiscal {year + 1} we expect growth of 3% "
                f"to 5%. {FILL * 6}\n")
    qa = (f"发言人Operator： We will now open the line for questions. Our first question comes from Ann Lee.\n"
          f"发言人Ann Lee： Can you talk about pricing and competition in large deals this quarter? {FILL * 3}\n"
          f"发言人Jane Doe： Pricing is stable; competition for AI programmes is intense. {FILL * 5}\n"
          f"发言人Bob Kay： How should we think about margin and investment into fiscal {year + 1}? {FILL * 3}\n"
          f"发言人John Roe： Operating margin should expand 10 to 30 basis points. {FILL * 5}\n")
    return prepared + qa


def broker(house: str, rating: str, target: str) -> str:
    return (f"Synthetic Widgets (SYNW): {rating}; target ${target}\n\nKey takeaways. We reiterate our {rating} "
            f"rating. {FILL * 4}\n\nValuation: our price target of ${target} is based on 20x FY27E EPS. {FILL * 3}"
            f"\n\nEstimates table FY25A FY26E FY27E Revenue 20.1 21.3 22.6 EPS 10.10 11.20 12.40 Margin 15.1 15.3 "
            f"15.6 Bookings 21.0 22.0 23.5 Headcount 100 104 108 Utilisation 91 92 92\n\n{FILL * 4}")


class FakeCtx:
    """The one ``CorpusContext`` method ``index_document`` calls."""

    def companies_named(self, text: str) -> list[str]:
        found = []
        if re.search(r"Synthetic Widgets|SYNW", text):
            found.append(SYNW)
        if re.search(r"Other Gadgets|OTHR", text):
            found.append(OTHER)
        return found


class Corpus:
    def __init__(self, state: Path) -> None:
        self.state = state
        self.count = 0

    def add(self, kind: str, title: str, text: str, *, date: str, house: str | None = None,
            period: str | None = None, company: str | None = SYNW, now: str = "2026-09-01T00:00:00+00:00",
            acquired: str | None = None) -> str:
        self.count += 1
        document_ref = f"test-doc:{kind}:{self.count}"
        body = DocumentBody(
            document_ref=document_ref, source_ref="source:test", kind=kind, text=text,
            text_sha256=hashlib.sha256(text.encode()).hexdigest(), renderer="text", title=title, house=house,
            document_date=date, period=period, url=None, acquired_at=acquired or now,
            filed_company_refs=[company] if company else [], named_companies=[], metadata={})
        connection = ei.open_index(self.state)
        try:
            epoch = ei._epoch(connection, "2026-01-01T00:00:00+00:00")
            connection.execute("BEGIN IMMEDIATE")
            ei.index_document(connection, FakeCtx(), body, source_key=f"test:{self.count}", now=now, epoch=epoch)
            connection.execute("COMMIT")
        finally:
            connection.close()
        return document_ref


def numbers_stub(state_dir, company_ref, as_of):
    return {"json": {
        "schema_version": "numbers-pack:test", "company_ref": company_ref, "as_of": as_of, "ticker": "SYNW",
        "market": {"status": "ok", "price": {"close": "240.5", "date": "2026-10-08"},
                   "market_cap": {"value": "150000000000"},
                   "adv_30d": {"status": "available", "value": "900000000"},
                   "range_52w": {"low": "180", "high": "300", "from_high_percent": "-19.8"},
                   "returns": {"ytd": {"status": "available", "percent": "5.1"}},
                   "relative_to_basket": {"windows": {"ytd": {"status": "available", "relative_pp": "2.0"}}}},
        "consensus": {"status": "ok", "version": {"ref": "consensus-estimate-version:test", "as_of": "2026-10-07"},
                      "staleness": {"vendor_as_of": "2026-10-07", "vendor_days_old": 2, "stale": False},
                      "calendar": {"fiscal_year_end": "08-31", "last_reported_period_end": "2026-08-31",
                                   "advanced_by_release": True},
                      "periods": [{"slot": "FY", "label": "FY2027", "status": "available",
                                   "eps": {"mean": "12.40", "low": "12.0", "high": "12.9", "count": 20},
                                   "revenue": {"mean": "22600000000", "count": 18}}],
                      "quarters": [{"label": "FY2027Q1", "eps": {"mean": "3.10"}, "revenue": {"mean": "5600000000"}}],
                      "ntm": {"eps": {"value": "12.55"}},
                      "target": {"mean": "260", "median": "258", "high": "320", "low": "190", "count": 20},
                      "ratings": {"buy": 9, "hold": 10, "sell": 1}},
        "street": {"houses": ["bullhouse", "bearhouse"], "periods": [
            {"period": "FY2027", "period_kind": "fiscal_year", "metric": "eps", "count": 2, "median": "12.3",
             "houses": [{"house": "bullhouse", "value": "12.4"}, {"house": "bearhouse", "value": "12.2"}]}],
            "targets": [{"broker": "BullHouse", "published_on": "2026-09-20", "rating": "buy", "target_value": "300"},
                        {"broker": "BearHouse", "published_on": "2026-09-10", "rating": "sell", "target_value": "190"}]},
        "outlook": {"release_period": "FY2026-Q4", "report_date": "2026-09-25", "items": [
            {"kind": "guidance", "metric": "revenue_growth_lc", "period": "FY2027", "low": "3", "high": "5",
             "unit": "percent"}]},
        "valuation": {"forward_pe": [
            {"source": "consensus_vendor", "label": "一致预期", "status": "available", "value": "12.55",
             "forward_pe": "19.2"},
            {"source": "ours", "label": "我们的模型", "status": "available", "value": "13.10", "forward_pe": "18.4"}],
            "enterprise_value": {"value": "148000000000"}, "ev_to_ebitda": "14.2"},
        "cross_checks": []}, "text": ""}


class PackFixture(unittest.TestCase):
    AS_OF = "2026-10-09"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        core = sqlite3.connect(self.state / "core.sqlite")
        core.executescript(CORE_SCHEMA)
        core.execute("INSERT INTO coverage_mission_versions VALUES (?,?,?,?)",
                     (MISSION["id"], MISSION["mission_ref"], 1, json.dumps(MISSION)))
        core.execute("INSERT INTO coverage_mission_pointer VALUES (?,?,?)",
                     (MISSION["mission_ref"], MISSION["id"], "2026-01-01"))
        core.execute("INSERT INTO company_dossier_versions VALUES (?,?,?,?,?,?)",
                     ("dossier-version:1", "dossier:synw", 1, SYNW, json.dumps({
                         "sections": [{"title": "商业模式", "body": "我们认为 Synthetic Widgets 的护城河在于长期托管合同。" * 3}]}),
                      "2026-09-01T00:00:00+00:00"))
        core.commit()
        core.close()
        self.corpus = Corpus(self.state)
        self.docs = {}
        for index, (quarter, date, period) in enumerate((
                ("Q4", "2026-09-25", "FY2026-Q4"), ("Q3", "2026-06-20", "FY2026-Q3"),
                ("Q2", "2026-03-20", "FY2026-Q2"), ("Q1", "2025-12-18", "FY2026-Q1"))):
            self.docs[f"t{index}"] = self.corpus.add("transcript", f"Synthetic Widgets {quarter} 2026",
                                                     transcript(quarter, 2026), date=date, period=period)
        self.docs["release"] = self.corpus.add(
            "sec_exhibit", "Synthetic Widgets 8-K Exhibit 99.1", "Synthetic Widgets reports fourth quarter results. "
            "New bookings of $5.1 billion. Fiscal 2027 outlook: revenue growth of 3% to 5%. " + FILL * 4,
            date="2026-09-25", period="FY2026-Q4")
        self.docs["10k"] = self.corpus.add(
            "sec_10k", "Synthetic Widgets Co. 10-K", "PART I\nItem 1. Business\nSynthetic Widgets serves clients in "
            "three reportable segments by geographic market. Competition is intense. " + FILL * 8
            + "\nItem 7. Management's Discussion and Analysis\nRevenues by segment grew. " + FILL * 6,
            date="2025-10-15", house="Synthetic Widgets Co.")
        self.docs["bull"] = self.corpus.add("broker_report", "Synthetic Widgets (SYNW): Buy; AI tailwinds",
                                            broker("BullHouse", "Buy", "300"), date="2026-09-20", house="BullHouse")
        self.docs["bear"] = self.corpus.add("broker_report", "Synthetic Widgets (SYNW): Sell; pricing risk",
                                            broker("BearHouse", "Sell", "190"), date="2026-09-10", house="BearHouse")
        self.docs["neutral"] = self.corpus.add("broker_report", "Synthetic Widgets (SYNW): Neutral; wait",
                                               broker("MidHouse", "Neutral", "250"), date="2026-08-10", house="MidHouse")
        self.docs["bull2"] = self.corpus.add("broker_report", "Synthetic Widgets (SYNW): Overweight; share gains",
                                             broker("TopHouse", "Overweight", "310"), date="2026-07-10", house="TopHouse")
        self.docs["weekly"] = self.corpus.add(
            "broker_report", "Sector weekly: gadgets and widgets",
            ("Other Gadgets Inc. dominated the week. " + FILL) * 10 + "SYNW was flat.", date="2026-09-18",
            house="WeeklyHouse", company=None)
        self.docs["note"] = self.corpus.add(
            "sales_note", "Desk note 9/26", "MACRO: Nasdaq +0.4%. Synthetic Widgets +6% on bookings beat; "
            "basket ripping. " + FILL, date="2026-09-26", company=None)
        self.docs["expert"] = self.corpus.add(
            "expert_call", "Former client on widget vendors",
            "Q: How do you pick vendors?\nA: We consolidated onto Synthetic Widgets because pricing was competitive "
            "and AI delivery was strong. " + FILL * 3, date="2026-08-01", house="Guidepoint", company=None)

    def build(self, **kwargs):
        return mp.build(self.state, SYNW, "initial_screen", self.AS_OF, numbers_fn=numbers_stub, **kwargs)


class PackBuildTests(PackFixture):
    def test_every_section_is_built_from_the_index_and_labelled(self) -> None:
        pack = self.build()
        budget = pack.budget
        for key in ("numbers", "latest_transcript", "earnings_release", "prior_transcripts", "transcript_qa",
                    "broker_reports", "sales_notes", "filing_10k", "expert_calls", "our_views"):
            self.assertGreater(budget[key]["used"], 0, key)
            self.assertIsNone(budget[key]["empty_reason"], key)
        self.assertEqual(budget["legacy_claims"]["used"], 0)
        self.assertIn("已有原文段落", budget["legacy_claims"]["empty_reason"])
        text = pack.text
        # The latest transcript in full: its Q&A answer is there.
        self.assertIn("Operating margin should expand 10 to 30 basis points", text)
        self.assertIn("《Synthetic Widgets Q4 2026》", text)
        # Prior quarters' prepared remarks section holds no Q&A passage of those calls.
        prepared = text.split("## 4.")[1].split("## 5.")[0]
        self.assertIn("Bookings were $5.1 billion in Q3", prepared)
        self.assertNotIn("Our first question comes from", prepared)
        # Broker reports: both sides, structured numbers bound to the street table.
        selection = {item["house"]: item["stance"] for item in pack.json["selection"]["broker_reports"]}
        self.assertEqual(selection["BullHouse"], "bull")
        self.assertEqual(selection["BearHouse"], "bear")
        self.assertIn("券商：BearHouse（偏空）", text)
        self.assertRegex(text, r"券商：BullHouse（偏多）.*目标价 \[N\d+\] 300")
        # A sector weekly that only mentions the company is not one of its broker reports.
        self.assertNotIn("Sector weekly", text.split("## 6.")[1].split("## 7.")[0])
        self.assertIn("我们的看法（非证据）", text)
        self.assertIn("[O1]", text)
        self.assertEqual(pack.tags["O1"], "dossier-version:1")
        # Ours vs consensus is computed and tagged.
        self.assertRegex(text, r"我们的模型.*NTM EPS \[N\d+\] 13.10；一致预期 \[N\d+\] 12.55，我们高 \[N\d+\] 4.4%")
        self.assertEqual(pack.sufficiency["status"], "sufficient")
        self.assertTrue(any(item["kind"] == "document" for item in pack.inputs))
        self.assertEqual(pack.recipe_version, "initial_screen:v1")
        record = pack.record()
        self.assertEqual(record["content_hash"], pack.content_hash)
        self.assertIn("watermark", record)

    def test_deterministic(self) -> None:
        self.assertEqual(self.build().content_hash, self.build().content_hash)

    def test_budget_enforcement(self) -> None:
        pack = self.build(budget_chars=9000)
        self.assertLessEqual(pack.budget["_total"]["limit"], 9000)
        for key, value in pack.budget.items():
            if key.startswith("_"):
                continue
            self.assertLessEqual(value["used"], value["limit"], key)
        full = self.build()
        self.assertGreater(len(full.text), len(pack.text))
        self.assertGreater(full.budget["latest_transcript"]["used"], pack.budget["latest_transcript"]["used"])

    def test_tag_binding_round_trip(self) -> None:
        pack = self.build()
        in_text = set(re.findall(r"\[([PNCO]\d+)\]", pack.text))
        self.assertEqual(in_text, set(pack.tags))
        for tag, ref in pack.tags.items():
            if tag.startswith("P"):
                found = ei.passage(self.state, ref)
                self.assertIsNotNone(found, ref)
                self.assertIn(found["text"].strip()[:200], pack.text)
            elif tag.startswith("N"):
                bound = pack.json["number_tags"][tag]
                self.assertEqual(mp.resolve_number(pack.json, ref), bound["value"])
                self.assertIn(f"[{tag}] {bound['display']}", pack.text)
                self.assertEqual(pack.resolve(tag)["value"], bound["value"])
        # Passages of one document are never repeated across sections.
        refs = [ref for tag, ref in pack.tags.items() if tag.startswith("P")]
        self.assertEqual(len(refs), len(set(refs)))

    def test_ask_uses_the_question_topics(self) -> None:
        pack = mp.build(self.state, SYNW, "ask", self.AS_OF, numbers_fn=numbers_stub,
                        topics=["pricing competition AI programmes"])
        self.assertIn("competition for AI programmes is intense", pack.text)


class BlankAndLegacyTests(unittest.TestCase):
    def test_blank_workspace_is_honest_not_an_exception(self) -> None:
        with tempfile.TemporaryDirectory() as blank:
            pack = mp.build(blank, SYNW, "initial_screen", "2026-10-09")
            self.assertEqual(pack.sufficiency["status"], "insufficient")
            self.assertFalse(pack.sufficiency["drafting_allowed"])
            self.assertIn("本节为空", pack.text)
            self.assertEqual({tag[0] for tag in pack.tags} - {"N"}, set())
            self.assertFalse((Path(blank) / mr.LEDGER_DB).exists(), "a pack build only reads")
            for product in mp.RECIPES:
                mp.build(blank, SYNW, product, "2026-10-09")

    def test_legacy_claims_only_without_passages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            core = sqlite3.connect(Path(tmp) / "core.sqlite")
            core.executescript(CORE_SCHEMA)
            core.execute("INSERT INTO claim_versions VALUES (?,?,?,?,?,?,?)",
                         ("claim-version:" + "a" * 64, "claim:1", 1,
                          json.dumps({"subject_ref": SYNW, "normalized_statement": "Synthetic Widgets 托管业务占比过半。",
                                      "period": "FY2026"}), "h", None, "2026-09-01T00:00:00+00:00"))
            core.commit()
            core.close()
            pack = mp.build(tmp, SYNW, "initial_screen", "2026-10-09")
            self.assertEqual(pack.tags.get("C1"), "claim-version:" + "a" * 64)
            self.assertIn("[C1] Synthetic Widgets 托管业务占比过半。", pack.text)

    def test_unknown_product_is_refused(self) -> None:
        with self.assertRaises(mp.MaterialPackError):
            mp.build("/nonexistent", SYNW, "novel")


class MaterialRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.need = {"kind": "document", "doc_kinds": ["transcript"]}

    def open(self, lane="initial_screen", **kwargs):
        return mr.open_request(self.state, requester={"lane": lane, "section": "s1"}, subject=SYNW,
                               need=self.need, period="quarter_ending:2026-08-31", **kwargs)

    def test_idempotent_by_digest_and_joined_by_a_second_requester(self) -> None:
        first = self.open()
        self.assertEqual((first["write_status"], first["status"]), ("fresh", "open"))
        again = self.open()
        self.assertEqual((again["write_status"], again["id"]), ("duplicate", first["id"]))
        joined = self.open(lane="dossier")
        self.assertEqual((joined["write_status"], joined["id"]), ("joined", first["id"]))
        self.assertEqual([r["lane"] for r in joined["requesters"]], ["initial_screen", "dossier"])
        other = mr.open_request(self.state, requester={"lane": "initial_screen"}, subject=SYNW, need=self.need,
                                period="quarter_ending:2026-11-30")
        self.assertNotEqual(other["id"], first["id"])
        self.assertEqual(first["digest"], mr.request_digest(SYNW, self.need, "quarter_ending:2026-08-31"))
        self.assertEqual(len(mr.open_requests(self.state, subject=SYNW)), 2)
        self.assertEqual(len(mr.open_requests(self.state, lane="dossier")), 1)

    def test_attempts_close_and_append_only(self) -> None:
        request = self.open()
        mr.record_attempt(self.state, request["id"], source="indexed_corpus", outcome="not_found")
        after = mr.record_attempt(self.state, request["id"], source="alphaengine", outcome="partial",
                                  status="partial", items=[{"document_ref": "x"}])
        self.assertEqual((after["status"], after["attempt_count"]), ("partial", 2))
        closed = mr.close(self.state, request["id"], status="exhausted", reason="来源用尽")
        self.assertTrue(closed["terminal"])
        self.assertEqual(mr.close(self.state, request["id"], status="exhausted", reason="x")["write_status"],
                         "duplicate")
        with self.assertRaises(mr.MaterialRequestError):
            mr.close(self.state, request["id"], status="found", reason="late")
        with self.assertRaises(mr.MaterialRequestError):
            mr.record_attempt(self.state, request["id"], source="web", outcome="found")
        self.assertEqual(mr.open_requests(self.state), [])
        self.assertEqual(mr.status_counts(self.state)["exhausted"], 1)
        connection = sqlite3.connect(mr.ledger_path(self.state))
        with self.assertRaises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM material_request_events")
        with self.assertRaises(sqlite3.DatabaseError):
            connection.execute("UPDATE material_requests SET subject_ref='x'")
        connection.close()

    def test_malformed_requests_are_refused(self) -> None:
        for need in ({"kind": "document"}, {"kind": "data"}, {"kind": "answer"}, {"kind": "telepathy"}):
            with self.assertRaises(mr.MaterialRequestError):
                mr.open_request(self.state, requester={"lane": "x"}, subject=SYNW, need=need)
        with self.assertRaises(mr.MaterialRequestError):
            mr.open_request(self.state, requester={}, subject=SYNW, need=self.need)

    def test_pack_builds_are_recorded(self) -> None:
        self.assertIsNone(mr.last_pack_build(self.state, SYNW))
        mr.record_pack_build(self.state, {"company_ref": SYNW, "product": "initial_screen",
                                          "recipe_version": "initial_screen:v1", "content_hash": "c" * 64,
                                          "total_chars": 10, "sufficiency": {"status": "sufficient"}})
        self.assertEqual(mr.last_pack_build(self.state, SYNW)["content_hash"], "c" * 64)


class SufficiencyTests(PackFixture):
    def test_sufficient_with_materials(self) -> None:
        numbers = numbers_stub(self.state, SYNW, self.AS_OF)["json"]
        result = ms.sufficiency(self.state, SYNW, "initial_screen", self.AS_OF, numbers=numbers)
        self.assertEqual(result["status"], "sufficient", [i for i in result["items"] if not i["satisfied"]])
        self.assertEqual(result["missing"], [])
        self.assertFalse((self.state / mr.LEDGER_DB).exists())

    def test_insufficient_opens_requests_waits_then_times_out(self) -> None:
        numbers = numbers_stub(self.state, SYNW, self.AS_OF)["json"]
        numbers["consensus"]["staleness"]["vendor_days_old"] = 30
        numbers["market"] = {"status": "empty", "reason": "没有行情"}
        first = ms.sufficiency(self.state, SYNW, "initial_screen", self.AS_OF, numbers=numbers,
                               now="2026-10-09T08:00:00+00:00")
        self.assertEqual(first["status"], "insufficient")
        self.assertEqual({item["key"] for item in first["missing"]}, {"consensus_fresh", "market_block"})
        self.assertTrue(all(item["request"]["write_status"] == "fresh" for item in first["missing"]))
        self.assertEqual(first["wait_until"], "2026-10-10T08:00:00+00:00")
        later = ms.sufficiency(self.state, SYNW, "initial_screen", self.AS_OF, numbers=numbers,
                               now="2026-10-09T20:00:00+00:00")
        self.assertEqual(later["status"], "insufficient")
        self.assertEqual([i["request"]["id"] for i in later["missing"]], [i["request"]["id"] for i in first["missing"]])
        self.assertEqual(later["wait_until"], first["wait_until"])
        expired = ms.sufficiency(self.state, SYNW, "initial_screen", self.AS_OF, numbers=numbers,
                                 now="2026-10-10T09:00:00+00:00")
        self.assertEqual(expired["status"], "timed_out")
        self.assertTrue(expired["drafting_allowed"])
        self.assertEqual(len(expired["gap_request_refs"]), 2)

    def test_exhausted_requests_allow_drafting_early(self) -> None:
        numbers = numbers_stub(self.state, SYNW, self.AS_OF)["json"]
        numbers["market"] = {"status": "empty"}
        first = ms.sufficiency(self.state, SYNW, "initial_screen", self.AS_OF, numbers=numbers,
                               now="2026-10-09T08:00:00+00:00")
        mr.close(self.state, first["missing"][0]["request"]["id"], status="exhausted", reason="没有行情源")
        again = ms.sufficiency(self.state, SYNW, "initial_screen", self.AS_OF, numbers=numbers,
                               now="2026-10-09T09:00:00+00:00")
        self.assertEqual(again["status"], "timed_out")

    def test_missing_index_and_core(self) -> None:
        with tempfile.TemporaryDirectory() as blank:
            result = ms.sufficiency(blank, SYNW, "initial_screen", self.AS_OF, record=False)
            self.assertEqual(result["status"], "insufficient")
            self.assertEqual(len(result["missing"]), 6)
            self.assertTrue(all(item["request"] is None for item in result["missing"]))
            self.assertFalse(result["workspace"]["has_index"])
            self.assertEqual(ms.sufficiency(blank, SYNW, "ask", self.AS_OF)["status"], "sufficient")


class RedraftPolicyTests(PackFixture):
    DRAFTED = "2026-10-01T00:00:00+00:00"

    def setUp(self) -> None:
        super().setUp()
        self.pack = self.build()
        self.last = {"created_at": self.DRAFTED, "version_ref": "v1", "watermark": self.pack.watermark}
        self.now = datetime(2026, 10, 9, tzinfo=timezone.utc)

    def arrive(self, kind: str, title: str, text: str, date: str, **kwargs) -> str:
        return self.corpus.add(kind, title, text, date=date, now="2026-10-05T00:00:00+00:00",
                               acquired="2026-10-05T00:00:00+00:00", **kwargs)

    def decide(self, last=None, **kwargs):
        return rp.should_redraft(self.state, SYNW, "initial_screen", last or self.last, now=self.now, **kwargs)

    def test_nothing_new_is_no(self) -> None:
        decision, reasons = self.decide()
        self.assertEqual(decision, "no")
        self.assertEqual(reasons[-1]["code"], "pending_digest")
        self.assertEqual(reasons[-1]["count"], 0)

    def test_a_single_arrival_is_only_pending(self) -> None:
        self.arrive("sales_note", "Desk note 10/4", "Synthetic Widgets flat. " + FILL, "2026-10-04")
        decision, reasons = self.decide()
        self.assertEqual(decision, "no")
        self.assertEqual(reasons[-1]["count"], 1)
        targets = rp.evaluate_targets(self.state, "initial_screen", {SYNW: self.last, OTHER: None}, now=self.now)
        self.assertEqual(targets[SYNW]["decision"], "no")
        self.assertIn("待消化 1", targets[SYNW]["summary"])
        self.assertEqual(targets[OTHER]["decision"], "redraft")

    def test_a_new_quarter_transcript_redrafts(self) -> None:
        self.arrive("transcript", "Synthetic Widgets Q1 2027", transcript("Q1", 2027), "2026-10-06",
                    period="FY2027-Q1")
        decision, reasons = self.decide()
        self.assertEqual(decision, "redraft")
        self.assertIn("required_material", [r["code"] for r in reasons if r["fired"]])

    def test_an_old_quarter_backfilled_late_does_not(self) -> None:
        self.arrive("transcript", "Synthetic Widgets Q4 2025", transcript("Q4", 2025), "2025-09-20",
                    period="FY2025-Q4")
        decision, _ = self.decide()
        self.assertEqual(decision, "no")

    def test_three_broker_notes_redraft(self) -> None:
        for n in range(3):
            self.arrive("broker_report", f"Synthetic Widgets (SYNW): Buy; note {n}", broker("H", "Buy", "300") + str(n),
                        "2026-10-0" + str(3 + n), house=f"House{n}")
        decision, reasons = self.decide()
        self.assertEqual(decision, "redraft")
        batch = next(r for r in reasons if r["code"] == "broker_batch")
        self.assertTrue(batch["fired"])
        self.assertEqual(batch["count"], 3)

    def test_two_broker_notes_do_not(self) -> None:
        for n in range(2):
            self.arrive("broker_report", f"Synthetic Widgets (SYNW): Buy; note {n}", broker("H", "Buy", "300") + str(n),
                        "2026-10-0" + str(3 + n), house=f"House{n}")
        self.assertEqual(self.decide()[0], "no")

    def test_misattribution_flag_without_material_is_no_and_with_material_redrafts(self) -> None:
        flag = {"id": "product-rewrite-flag:x", "product_version_ref": "v1", "product_kind": "initial_screen",
                "retirement_reasons": ["issuer_misattribution"], "rewrite_ready": False, "arrived_material": []}
        with mock.patch("dalton_core.product_rewrite_flags.needs_rewrite", return_value=[flag]):
            decision, reasons = self.decide()
        self.assertEqual(decision, "no")
        self.assertIn("misattribution_waiting", [r["code"] for r in reasons])
        ready = {**flag, "rewrite_ready": True, "arrived_material": ["doc"]}
        with mock.patch("dalton_core.product_rewrite_flags.needs_rewrite", return_value=[ready]):
            decision, reasons = self.decide()
        self.assertEqual(decision, "redraft")

    def test_model_eps_move_and_stale_and_gate(self) -> None:
        with mock.patch.object(ms, "our_eps", return_value={"status": "available", "value": "14.50"}):
            decision, reasons = self.decide()
        self.assertEqual(decision, "redraft")
        self.assertTrue(next(r for r in reasons if r["code"] == "model_eps")["fired"])
        with mock.patch.object(ms, "our_eps", return_value={"status": "available", "value": "13.20"}):
            self.assertEqual(self.decide()[0], "no")
        self.arrive("sales_note", "Desk note 10/4", "Synthetic Widgets flat. " + FILL, "2026-10-04")
        self.now = datetime(2026, 10, 20, tzinfo=timezone.utc)
        decision, reasons = self.decide()
        self.assertEqual(decision, "redraft")
        self.assertTrue(next(r for r in reasons if r["code"] == "stale")["fired"])
        decision, reasons = self.decide({**self.last, "gate_status": "gate_passed"})
        self.assertEqual(decision, "reopen_required")
        self.assertEqual(rp.should_redraft(self.state, SYNW, "initial_screen", None)[0], "redraft")

    def test_without_a_watermark_the_draft_time_is_the_baseline(self) -> None:
        self.arrive("transcript", "Synthetic Widgets Q1 2027", transcript("Q1", 2027), "2026-10-06")
        decision, _ = self.decide({"created_at": "2026-10-02T00:00:00+00:00"})
        self.assertEqual(decision, "redraft")


class CockpitTests(PackFixture):
    def get(self, state=None, **query):
        from dalton_core.cockpit_v2 import material_status_api as api
        from dalton_core.cockpit_v2.charts_api import ChartReader

        status, content_type, body = api.material_status_routes()[api.PATH](
            query, ChartReader(Path(state or self.state) / "core.sqlite"))
        self.assertEqual(content_type, "application/json; charset=utf-8")
        return status, json.loads(body)

    def test_status_payload(self) -> None:
        mr.open_request(self.state, requester={"lane": "initial_screen"}, subject=SYNW,
                        need={"kind": "data", "data_item": "consensus_estimates"}, period="week:2026-W41")
        status, body = self.get(company="SYNW")
        self.assertEqual((status, body["status"]), (200, "ok"), body)
        self.assertEqual(body["company"], "Synthetic Widgets Co. SYNW")
        self.assertIn(body["sufficiency"]["status"], ("insufficient", "sufficient", "timed_out"))
        self.assertEqual(len(body["requests"]), 1)
        self.assertEqual(body["request_counts"]["open"], 1)
        self.assertEqual(body["redraft"]["decision"], "redraft")  # never drafted
        self.assertEqual(body["pending_digest"]["label"], "待消化")
        self.assertIsNone(body["last_pack"])
        self.assertFalse(any(item.get("write_status") == "fresh" for item in body["sufficiency"]["missing"]
                             if item.get("request")))

    def test_honest_empty_and_bad_requests(self) -> None:
        status, body = self.get(company="ZZZZ")
        self.assertEqual((status, body["status"]), (200, "empty"))
        status, body = self.get()
        self.assertEqual((status, body["status"]), (200, "empty"))
        status, _ = self.get(company="SYNW", product="novel")
        self.assertEqual(status, 400)
        with tempfile.TemporaryDirectory() as blank:
            status, body = self.get(state=blank, company="SYNW")
            self.assertEqual((status, body["status"]), (200, "empty"))
            self.assertFalse((Path(blank) / mr.LEDGER_DB).exists())

    def test_route_is_served_through_the_v2_router(self) -> None:
        from types import SimpleNamespace

        from dalton_core.cockpit_v2 import material_status_api as api
        from dalton_core.cockpit_v2.routes import register_v2_routes

        router = register_v2_routes(SimpleNamespace())
        self.assertIn(api.PATH, router.raw_gets)


if __name__ == "__main__":
    unittest.main()


MODEL_SCHEMA = """
CREATE TABLE forecast_model_versions(version_id TEXT PRIMARY KEY, model_ref TEXT, version_number INTEGER,
    company_ref TEXT, record_json TEXT, created_at TEXT);
CREATE TABLE forecast_model_annual_projections(model_version_id TEXT PRIMARY KEY, company_ref TEXT,
    record_json TEXT, created_at TEXT);
"""
QUARTER_ENDS = ("2026-11-30", "2027-02-28", "2027-05-31", "2027-08-31", "2027-11-30", "2028-02-29", "2028-05-31")


def model_record() -> dict:
    def line(ref: str, unit: str, values) -> dict:
        return {"ref": ref, "unit": unit, "status": "computed", "cells": [
            {"period": {"end": end, "kind": "quarter"}, "status": "computed", "kind": "estimate", "value": value}
            for end, value in zip(QUARTER_ENDS, values)]}

    return {"unit": "usd", "results": [
        line("result:revenue", "usd", ["5500000000", "5600000000", "5700000000", "5800000000",
                                       "5900000000", "6000000000", "6100000000"]),
        line("result:diluted_eps", "usd_per_share", ["3.10", "3.20", "3.30", "3.40", "3.50", "3.60", "3.70"])]}


class OurModelYearsTests(PackFixture):
    """Batch C2: FY+1/FY+2 revenue and EPS from our model, beside the consensus, tagged."""

    def setUp(self) -> None:
        super().setUp()
        core = sqlite3.connect(self.state / "core.sqlite")
        core.executescript(MODEL_SCHEMA)
        core.execute("INSERT INTO forecast_model_versions VALUES (?,?,?,?,?,?)",
                     ("forecast-model-version:synw:3", "forecast-model:synw", 3, SYNW, json.dumps(model_record()),
                      "2026-10-02T00:00:00+00:00"))
        core.commit()
        core.close()

    def numbers(self, state_dir, company_ref, as_of):
        pack = numbers_stub(state_dir, company_ref, as_of)
        pack["json"]["consensus"]["periods"].append(
            {"slot": "FY2", "label": "FY2028", "status": "available",
             "eps": {"mean": "14.00", "count": 15}, "revenue": {"mean": "24000000000", "count": 12}})
        return pack

    def test_revenue_is_additive_but_annual_eps_requires_bound_annual_method(self) -> None:
        pack = mp.build(self.state, SYNW, "initial_screen", self.AS_OF, numbers_fn=self.numbers)
        years = pack.json["our_model"]["years"]
        self.assertEqual([y["label"] for y in years], ["FY2027", "FY2028"])
        fy1 = years[0]
        self.assertEqual(fy1["eps"]["status"], "unavailable")
        self.assertIn("季度EPS不能相加", fy1["eps"]["reason"])
        self.assertEqual(fy1["revenue"]["value"], "22600000000")
        self.assertEqual(fy1["eps_vs_consensus"]["status"], "unavailable")
        self.assertEqual(fy1["revenue_vs_consensus"]["diff_pct"], "0.0")
        self.assertEqual(years[1]["eps"]["status"], "unavailable")
        self.assertIn("季度EPS不能相加", years[1]["eps"]["reason"])
        self.assertIn("我们的模型 FY1 FY2027 稀释 EPS：不可得", pack.text)
        self.assertRegex(pack.text, r"我们的模型 FY1 FY2027 收入 \[N\d+\] 22.60bn（按四季合计）")
        self.assertIn("我们的模型 FY2 FY2028 稀释 EPS：不可得", pack.text)

    def test_tags_bind_to_our_model_and_consensus_for_the_worksheet(self) -> None:
        from dalton_core.initial_screen import _is_our_model
        from dalton_core.product_contract import normalise_pack, tag_entry

        pack = normalise_pack(mp.build(self.state, SYNW, "initial_screen", self.AS_OF, numbers_fn=self.numbers))
        by_pointer = {info["pointer"]: tag for tag, info in pack["json"]["number_tags"].items()}
        self.assertNotIn("/our_model/years/0/eps/value", by_pointer)
        revenue = tag_entry(pack, by_pointer["/our_model/years/0/revenue/value"])
        self.assertTrue(_is_our_model(revenue))
        self.assertIn("22600000000", revenue["text"])
        theirs = tag_entry(pack, by_pointer["/our_model/years/0/revenue_vs_consensus/theirs"])
        self.assertEqual(theirs["source_kind"], "consensus")
        self.assertFalse(_is_our_model(theirs))
        gap = tag_entry(pack, by_pointer["/our_model/years/0/revenue_vs_consensus/diff_pct"])
        self.assertEqual(gap["source_kind"], "comparison")

    def test_the_stored_annual_projection_wins_when_it_computed_the_year(self) -> None:
        core = sqlite3.connect(self.state / "core.sqlite")
        core.execute("INSERT INTO forecast_model_annual_projections VALUES (?,?,?,?)", (
            "forecast-model-version:synw:3", SYNW, json.dumps({"periods": [
                {"label": "FY2027E", "kind": "forecast",
                 "forecast_eps": {"status": "computed", "value": "12.9871"},
                 "line_outcomes": {"result:revenue": {"status": "computed", "value": "22500000000.4"}}},
                {"label": "FY2028E (partial)", "kind": "partial", "forecast_eps": None, "line_outcomes": {}}]}),
            "2026-10-02"))
        core.commit()
        core.close()
        years = mp.our_model_years(self.state, SYNW, self.numbers(None, SYNW, None)["json"])
        self.assertEqual(years[0]["eps"], {"status": "available", "value": "12.99", "method": "annual_projection",
                                           "projection_label": "FY2027E", "unit": "usd_per_share"})
        self.assertEqual(years[0]["revenue"]["value"], "22500000000")

    def test_no_model_is_honest(self) -> None:
        core = sqlite3.connect(self.state / "core.sqlite")
        core.execute("DELETE FROM forecast_model_versions")
        core.commit()
        core.close()
        years = mp.our_model_years(self.state, SYNW, self.numbers(None, SYNW, None)["json"])
        self.assertEqual(years[0]["eps"]["status"], "unavailable")
        self.assertEqual(years[0]["eps_vs_consensus"]["reason"], "我们的模型这一年没有数")


class PassageMetadataTests(PackFixture):
    """Batch C2: passage tags carry their issuer, doc kind, house and date; the record keeps doc_kind."""

    def test_passage_tags_carry_issuer_and_kind(self) -> None:
        from dalton_core.product_contract import normalise_pack, tag_entry

        pack = self.build()
        meta = pack.json["passage_tags"]
        self.assertEqual(set(meta), {tag for tag in pack.tags if tag.startswith("P")})
        kinds = {item["doc_kind"] for item in meta.values()}
        self.assertTrue({"transcript", "broker_report", "sec_exhibit"} <= kinds)
        transcript_tag = next(tag for tag, item in meta.items() if item["doc_kind"] == "transcript")
        self.assertEqual(meta[transcript_tag]["company_ref"], SYNW)
        self.assertEqual(pack.resolve(transcript_tag)["company_ref"], SYNW)
        broker_tag = next(tag for tag, item in meta.items() if item.get("house") == "BullHouse")
        self.assertEqual(meta[broker_tag]["date"], "2026-09-20")
        entry = tag_entry(normalise_pack(pack), transcript_tag)
        self.assertEqual(entry["company_ref"], SYNW)
        self.assertEqual(entry["doc_kind"], "transcript")
        documents = [item for item in pack.record()["inputs"] if item["kind"] == "document"]
        self.assertTrue(documents and all(item.get("doc_kind") for item in documents))

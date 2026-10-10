"""P2-5: the per-document failure cap, as a pure rule and as an owner to-do item."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from dalton_core.coverage_mission import (
    FETCH_FAILURE_CAP,
    FETCH_FAILURE_COOLDOWN,
    CoverageMissionValidationError,
    capped_fetch_failures,
)
from dalton_core.needs_human import KINDS, URGENCY, capped_fetches

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)
SEC_DOC = "sec:filing:0000950170-25-100235"


def attempt(n: int, outcome: str, *, document_ref: str = SEC_DOC,
            source_ref: str = "source:web-search", host: str = "www.sec.gov",
            hours: float | None = None) -> dict:
    at = T0 + timedelta(hours=n if hours is None else hours)
    return {"attempt_ref": f"acquisition-attempt:{n:032x}", "source_ref": source_ref,
            "document_ref": document_ref, "host": host, "outcome": outcome,
            "created_at": at.isoformat(timespec="microseconds")}


class CappedFetchFailuresTests(unittest.TestCase):
    def test_the_cap_needs_consecutive_failures(self) -> None:
        rows = [attempt(n, "transport_retryable") for n in range(FETCH_FAILURE_CAP - 1)]
        self.assertEqual(capped_fetch_failures(rows, as_of=T0 + timedelta(hours=5)), [])
        rows.append(attempt(FETCH_FAILURE_CAP, "transport_retryable"))
        capped = capped_fetch_failures(rows, as_of=T0 + timedelta(hours=5))
        self.assertEqual([item["document_ref"] for item in capped], [SEC_DOC])
        self.assertEqual(capped[0]["consecutive_failures"], FETCH_FAILURE_CAP)
        self.assertEqual(capped[0]["host"], "www.sec.gov")

    def test_a_success_resets_the_streak(self) -> None:
        rows = [attempt(0, "transport_retryable"), attempt(1, "unknown_failure"),
                attempt(2, "acquired"), attempt(3, "transport_retryable"),
                attempt(4, "transport_retryable")]
        self.assertEqual(capped_fetch_failures(rows, as_of=T0 + timedelta(hours=5)), [])

    def test_rows_in_any_order_and_across_ledger_records(self) -> None:
        # Each mission version carries the document forward under a new
        # record id; the ref is what repeats.
        rows = [attempt(n, "transport_retryable") for n in (4, 0, 2)]
        self.assertEqual(len(capped_fetch_failures(rows, as_of=T0 + timedelta(hours=5))), 1)

    def test_the_cooldown_runs_from_the_last_failure(self) -> None:
        rows = [attempt(n, "transport_retryable") for n in range(FETCH_FAILURE_CAP)]
        last = T0 + timedelta(hours=FETCH_FAILURE_CAP - 1)
        self.assertTrue(capped_fetch_failures(
            rows, as_of=last + FETCH_FAILURE_COOLDOWN - timedelta(seconds=1)))
        self.assertEqual(capped_fetch_failures(rows, as_of=last + FETCH_FAILURE_COOLDOWN), [])

    def test_documents_and_sources_are_counted_separately(self) -> None:
        rows = [attempt(0, "transport_retryable"),
                attempt(1, "transport_retryable", document_ref="public-web-url:sha256:" + "a" * 64),
                attempt(2, "transport_retryable", source_ref="source:sec-edgar"),
                attempt(3, "transport_retryable")]
        self.assertEqual(capped_fetch_failures(rows, as_of=T0 + timedelta(hours=5)), [])

    def test_bad_parameters_are_refused(self) -> None:
        with self.assertRaises(CoverageMissionValidationError):
            capped_fetch_failures([], as_of=T0, cap=0)
        with self.assertRaises(CoverageMissionValidationError):
            capped_fetch_failures([], as_of=T0, cooldown=timedelta(0))
        with self.assertRaises(CoverageMissionValidationError):
            capped_fetch_failures([], as_of=T0.replace(tzinfo=None))


class CappedFetchNeedsHumanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.core = sqlite3.connect(":memory:")
        self.addCleanup(self.core.close)
        self.core.row_factory = sqlite3.Row
        self.core.executescript(
            "CREATE TABLE coverage_mission_acquisition_attempts(attempt_ref TEXT, record_id TEXT,"
            " mission_version_ref TEXT, source_ref TEXT, host TEXT, document_ref TEXT,"
            " ticket_ref TEXT, outcome TEXT, transport_code TEXT, transport_evidence_ref TEXT,"
            " transport_evidence_hash TEXT, created_at TEXT, content_hash TEXT);"
            "CREATE TABLE coverage_mission_discovered_documents(record_id TEXT, source_ref TEXT,"
            " document_ref TEXT, company_ref TEXT, failure_reason TEXT, updated_at TEXT);")
        for n in range(FETCH_FAILURE_CAP):
            row = attempt(n, "transport_retryable")
            self.core.execute(
                "INSERT INTO coverage_mission_acquisition_attempts(attempt_ref,source_ref,host,"
                "document_ref,outcome,created_at) VALUES(?,?,?,?,?,?)",
                (row["attempt_ref"], row["source_ref"], row["host"], row["document_ref"],
                 row["outcome"], row["created_at"]))
        self.core.execute(
            "INSERT INTO coverage_mission_discovered_documents VALUES(?,?,?,?,?,?)",
            ("mission-discovered-document:1", "source:web-search", SEC_DOC, "company:ticker:msft",
             "fetch outcome retryable; PublicTransportResponseTooLarge: response body exceeds "
             "the configured limit", T0.isoformat()))

    def test_a_capped_sec_filing_is_one_backlog_item_that_says_why(self) -> None:
        self.assertIn("fetch_failure_capped", KINDS)
        self.assertEqual(URGENCY["fetch_failure_capped"], 4)
        items = capped_fetches(self.core, now=T0 + timedelta(hours=6))
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["kind"], "fetch_failure_capped")
        self.assertIn("msft", item["title"])
        self.assertIn("www.sec.gov", item["title"])
        # BUG-09: the reason in words, the exception name only under 技术详情,
        # the resume time in the owner's zone (T0 + cooldown, Asia/Shanghai).
        self.assertIn("目标文件过大，超出单次下载上限", item["why_blocked"])
        self.assertNotIn("PublicTransport", item["why_blocked"])
        self.assertIn("PublicTransportResponseTooLarge", item["detail"]["reason"])
        self.assertNotIn("+00:00", item["why_blocked"])
        self.assertRegex(item["why_blocked"], r"到 \d{4}年\d{1,2}月\d{1,2}日 \d{2}:\d{2} 再试一次")
        # A SEC body over the size cap retries by itself; nothing to press.
        self.assertEqual(item["where"], "无需操作，系统会自动重试")
        self.assertNotIn("where_view", item)
        self.assertIn("SEC", item["action"])
        self.assertEqual(item["detail"]["consecutive_failures"], FETCH_FAILURE_CAP)

    def test_a_cik_company_is_named_not_numbered(self) -> None:
        # 2026-10-07 inspection: 「0001058290 的一份资料连续 69 次没取回来」.
        import json

        self.core.execute("UPDATE coverage_mission_discovered_documents SET company_ref=?,"
                          " failure_reason=?", ("company:sec-cik:0001058290",
                          "fetch outcome retryable; connector transport exceeded the authority deadline"))
        self.core.executescript(
            "CREATE TABLE coverage_mission_versions(mission_version_id TEXT, record_json TEXT);"
            "CREATE TABLE coverage_mission_pointer(mission_ref TEXT, mission_version_id TEXT,"
            " updated_at TEXT);")
        self.core.execute("INSERT INTO coverage_mission_versions VALUES(?,?)", ("v1", json.dumps(
            {"universe": [{"company_ref": "company:sec-cik:0001058290", "ticker": "CTSH",
                           "name": None}]})))
        self.core.execute("INSERT INTO coverage_mission_pointer VALUES('m','v1','t')")
        item = capped_fetches(self.core, now=T0 + timedelta(hours=6))[0]
        self.assertTrue(item["title"].startswith("CTSH · Cognizant 的一份资料"))
        self.assertNotIn("0001058290", item["title"])
        self.assertIn("连接超时，稍后自动重试", item["why_blocked"])
        self.assertNotIn("authority deadline", item["why_blocked"])

    def test_no_items_after_the_cooldown_or_without_the_table(self) -> None:
        self.assertEqual(capped_fetches(
            self.core, now=T0 + FETCH_FAILURE_COOLDOWN + timedelta(days=1)), [])
        self.assertEqual(capped_fetches(None, now=T0), [])
        empty = sqlite3.connect(":memory:")
        self.addCleanup(empty.close)
        self.assertEqual(capped_fetches(empty, now=T0), [])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.perception import (
    PERCEPTION_BOUNDING_REF,
    LegacyCoveragePerceptionAdapter,
    PerceptionError,
    validate_snapshot,
)
from dalton_core.provider_token_estimate import (
    CHARS_PER_PROVIDER_TOKEN,
    LIVE_CALIBRATION_OBSERVATIONS,
    PROVIDER_INPUT_ESTIMATOR_REF,
    estimate_provider_input_tokens,
)
from dalton_core.research_context import count_dalton_search_tokens
from dalton_core.store import content_hash


CJK_CLAIM = (
    "万华化学二季度聚氨酯板块量价齐升，MDI 挂牌价上调，公司在业绩说明会上表示海外需求恢复，"
    "但原材料纯苯价格波动仍是主要不确定因素，后续需跟踪装置检修与新增产能投放节奏。"
)


def seed_legacy(path: Path, *, evidence_rows: int, event_rows: int = 5) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE companies(slug TEXT PRIMARY KEY,name TEXT,ticker TEXT,market TEXT,coverage_tier TEXT,coverage_status TEXT,archetype TEXT,investment_view TEXT,updated_at TEXT);
        CREATE TABLE events(id INTEGER,event_key TEXT,company_slug TEXT,event_type TEXT,occurred_at TEXT,title TEXT,summary TEXT,materiality TEXT,status TEXT,source_url TEXT,updated_at TEXT);
        CREATE TABLE evidence(id INTEGER,evidence_key TEXT,company_slug TEXT,claim TEXT,stance TEXT,source TEXT,source_url TEXT,as_of TEXT,confidence TEXT,valid_until TEXT,created_at TEXT);
        CREATE TABLE filings(id INTEGER,company_slug TEXT,form TEXT,filing_date TEXT,report_date TEXT,accession_no TEXT,created_at TEXT);
        INSERT INTO companies VALUES('wanhua','万华化学','600309.SS','CN','A','active','chemical','under review','2026-08-14T00:00:00+00:00');
        """
    )
    for index in range(event_rows):
        day = 1 + index
        conn.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                index, f"event-{index}", "wanhua", "news", f"2026-08-{day:02d}T00:00:00+00:00",
                f"事件{index}：{CJK_CLAIM[:40]}", CJK_CLAIM, "medium", "new",
                "https://example.com/event", f"2026-08-{day:02d}T00:00:00+00:00",
            ),
        )
    # ``evidence-1`` is the newest row so it always survives bounding; the
    # coordinator fixture's canned model output cites it by key.
    conn.execute(
        "INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            0, "evidence-1", "wanhua", CJK_CLAIM, "supports", "news",
            "https://example.com/evidence", "2026-08-31", "high", None,
            "2026-08-31T00:00:00+00:00",
        ),
    )
    for index in range(1, evidence_rows):
        day = 1 + index % 28
        conn.execute(
            "INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                index, f"evidence-r{index:03d}", "wanhua", f"{index:03d} {CJK_CLAIM * 3}", "supports",
                "news", "https://example.com/evidence", f"2026-08-{day:02d}", "medium",
                None, f"2026-08-{day:02d}T00:00:00+00:00",
            ),
        )
    conn.execute(
        "INSERT INTO filings VALUES(1,'wanhua','annual','2026-04-20','2025-12-31','0001','2026-04-20T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()


class ProviderTokenEstimateTests(unittest.TestCase):
    def test_estimator_never_undercounts_live_observations(self):
        # The three DeepSeek V4 Flash Agenda prompts that failed or nearly
        # failed the live budget on 2026-08-24..26.  Any recalibration that
        # drops below one of them must change the estimator ref.
        self.assertEqual(PROVIDER_INPUT_ESTIMATOR_REF, "estimator:provider-input-chars-per-token:2.2")
        self.assertEqual(CHARS_PER_PROVIDER_TOKEN, 2.2)
        for chars, provider_tokens in LIVE_CALIBRATION_OBSERVATIONS:
            self.assertGreaterEqual(estimate_provider_input_tokens("x" * chars), provider_tokens)

    def test_cjk_prompt_is_counted_in_provider_units_not_dalton_runs(self):
        text = CJK_CLAIM * 30
        dalton = count_dalton_search_tokens(text)
        provider = estimate_provider_input_tokens(text)
        # The frozen Dalton tokenizer counts each CJK run as one token; the
        # provider estimate has to stay well above it for Chinese prompts.
        self.assertGreater(provider, dalton * 3)
        with self.assertRaises(TypeError):
            estimate_provider_input_tokens(b"bytes")  # type: ignore[arg-type]


class PerceptionBoundingTests(unittest.TestCase):
    def test_unbounded_snapshot_has_no_bounding_record(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "coverage.sqlite"
            seed_legacy(source, evidence_rows=6)
            snapshot = LegacyCoveragePerceptionAdapter(source).build("wanhua")
            self.assertNotIn("bounding", snapshot)
            self.assertEqual(len(snapshot["evidence"]), 6)

    def test_bounded_snapshot_drops_oldest_evidence_first_and_records_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "coverage.sqlite"
            seed_legacy(source, evidence_rows=32, event_rows=10)
            adapter = LegacyCoveragePerceptionAdapter(source)
            full = adapter.build("wanhua")
            self.assertEqual(len(full["evidence"]), 32)
            full_estimate = estimate_provider_input_tokens(
                json.dumps(
                    {key: full[key] for key in ("company", "catalysts", "evidence", "filings")},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                )
            )
            budget = full_estimate // 2
            bounded = adapter.write("wanhua", root / "snapshot.json", max_estimated_tokens=budget)
            bounding = bounded["bounding"]
            self.assertEqual(bounding["bounding_ref"], PERCEPTION_BOUNDING_REF)
            self.assertEqual(bounding["estimator_ref"], PROVIDER_INPUT_ESTIMATOR_REF)
            self.assertEqual(bounding["max_estimated_tokens"], budget)
            self.assertLessEqual(bounding["estimated_tokens"], budget)
            self.assertEqual(bounding["fetched"], {"catalysts": 10, "evidence": 32, "filings": 1})
            self.assertGreater(bounding["dropped"]["evidence"], 0)
            # Evidence goes first; catalysts and filings are untouched until it
            # is exhausted.
            self.assertEqual(bounding["dropped"]["catalysts"], 0)
            self.assertEqual(bounding["dropped"]["filings"], 0)
            self.assertEqual(len(bounded["catalysts"]), 10)
            # What survives is the newest evidence in the same order.
            kept = [item["evidence_key"] for item in bounded["evidence"]]
            self.assertEqual(kept, [item["evidence_key"] for item in full["evidence"]][: len(kept)])
            as_of = [item["as_of"] for item in bounded["evidence"]]
            self.assertEqual(as_of, sorted(as_of, reverse=True))
            # The file the coordinator registers is the same closed wire.
            self.assertEqual(
                validate_snapshot(json.loads((root / "snapshot.json").read_text()))["content_hash"],
                bounded["content_hash"],
            )
            # Bounding is deterministic for the same source and budget.
            again = adapter.build("wanhua", max_estimated_tokens=budget)
            self.assertEqual(again["bounding"], bounding)
            self.assertEqual(again["evidence"], bounded["evidence"])

    def test_bounding_exhausts_evidence_before_catalysts(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "coverage.sqlite"
            seed_legacy(source, evidence_rows=4, event_rows=6)
            adapter = LegacyCoveragePerceptionAdapter(source)
            company_only = adapter.build("wanhua")
            floor = estimate_provider_input_tokens(
                json.dumps(
                    {"company": company_only["company"], "catalysts": [], "evidence": [], "filings": []},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                )
            )
            # Enough for the company row and one short event, nothing else.
            bounded = adapter.build("wanhua", max_estimated_tokens=floor + 300)
            self.assertEqual(bounded["bounding"]["dropped"]["evidence"], 4)
            self.assertGreater(bounded["bounding"]["dropped"]["catalysts"], 0)
            self.assertEqual(len(bounded["evidence"]), 0)

    def test_bounding_fails_closed_when_nothing_fits(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "coverage.sqlite"
            seed_legacy(source, evidence_rows=2)
            adapter = LegacyCoveragePerceptionAdapter(source)
            with self.assertRaises(PerceptionError):
                adapter.build("wanhua", max_estimated_tokens=1)
            with self.assertRaises(PerceptionError):
                adapter.build("wanhua", max_estimated_tokens=0)
            with self.assertRaises(PerceptionError):
                adapter.build("wanhua", max_estimated_tokens=True)  # type: ignore[arg-type]

    def test_tampered_bounding_record_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "coverage.sqlite"
            seed_legacy(source, evidence_rows=12)
            adapter = LegacyCoveragePerceptionAdapter(source)
            bounded = adapter.build("wanhua", max_estimated_tokens=2_000)
            self.assertGreater(bounded["bounding"]["dropped"]["evidence"], 0)

            def rehash(wire: dict) -> dict:
                body = {key: value for key, value in wire.items() if key != "content_hash"}
                return {**body, "content_hash": content_hash(body)}

            # Claiming fewer drops than the snapshot shows.
            lying = dict(bounded)
            lying["bounding"] = dict(bounded["bounding"], dropped={"catalysts": 0, "evidence": 0, "filings": 0})
            with self.assertRaises(PerceptionError):
                validate_snapshot(rehash(lying))
            # Claiming a fit the estimate does not support.
            over = dict(bounded)
            over["bounding"] = dict(bounded["bounding"], max_estimated_tokens=1)
            with self.assertRaises(PerceptionError):
                validate_snapshot(rehash(over))
            # Unknown estimator ref.
            other = dict(bounded)
            other["bounding"] = dict(bounded["bounding"], estimator_ref="estimator:other")
            with self.assertRaises(PerceptionError):
                validate_snapshot(rehash(other))


if __name__ == "__main__":
    unittest.main()

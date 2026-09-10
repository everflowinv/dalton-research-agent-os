from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.event_judgement import build_judge_prompt, derived_context
from dalton_core.insider_trading_plan import (
    trading_arrangement_events, trading_plan_event_candidates,
)
from dalton_core.research_event import (
    DEFAULT_TIER_BY_KIND, EVENT_KINDS, PAYLOAD_FIELDS, ResearchEventAuthority,
    record_event, validate_payload,
)
from dalton_core.tracking_lane_cli import company_events, run_tracking
from dalton_core.tracking_cadence import POLICY_PATH
from tests.p14a_fixtures import ACN, AUTOMATION, P14aHarness

FIXTURES = Path(__file__).parent / "fixtures/sec-item5"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def parse(name: str, accession: str):
    return trading_arrangement_events(
        fixture(name), company_ref=ACN, accession=accession,
        filing_date="2026-07-31", issuer_name="Filed issuer",
        document_ref="artifact-version:item5", artifact_hash="a" * 64,
    )


class ParserTests(unittest.TestCase):
    def test_adoption_and_termination_are_distinct_plan_events(self):
        result = parse("adopt-terminate-0000821189-26-000149.txt",
                       "0000821189-26-000149")
        self.assertEqual(result["status"], "read")
        self.assertEqual(
            [(row["payload"]["person_name"], row["payload"]["action"],
              row["payload"]["action_date"]) for row in result["events"]],
            [("Jeffrey R. Leitzell", "terminated", "2026-06-25"),
             ("Jeffrey R. Leitzell", "adopted", "2026-06-26")],
        )
        self.assertEqual(len({row["payload"]["event_key"]
                              for row in result["events"]}), 2)

    def test_two_people_in_one_filing_do_not_cross_associate(self):
        result = parse("two-adoptions-0001193125-26-338217.txt",
                       "0001193125-26-338217")
        rows = {row["payload"]["person_name"]: row["payload"]
                for row in result["events"]}
        self.assertEqual(rows["Robert E. Landry"]["action_date"], "2026-06-12")
        self.assertEqual(rows["Robert E. Landry"]["aggregate_shares"], "2000")
        self.assertEqual(rows["Wendell Wierenga"]["action_date"], "2026-06-10")
        self.assertEqual(rows["Wendell Wierenga"]["aggregate_shares"], "45714")

    def test_negative_and_unlocatable_disclosures_are_explicit(self):
        negative = trading_arrangement_events(
            "Item 5. None of our directors or officers adopted or terminated any Rule 10b5-1 trading arrangement during the quarter. Item 6.",
            company_ref=ACN, accession="0000000000-00-000001", filing_date="2026-01-01",
            issuer_name="Issuer", document_ref="d", artifact_hash="a" * 64)
        self.assertEqual((negative["status"], negative["events"]), ("empty", []))
        refused = trading_arrangement_events(
            "Item 5. Other Information\n\nA director adopted a Rule 10b5-1 trading arrangement on June 2, 2026.\n\nItem 6. Exhibits",
            company_ref=ACN, accession="0000000000-00-000002", filing_date="2026-01-01",
            issuer_name="Issuer", document_ref="d", artifact_hash="a" * 64)
        self.assertEqual((refused["status"], refused["events"]), ("refused", []))

    def test_plan_contract_is_closed_and_is_not_an_insider_transaction(self):
        event = parse("two-adoptions-0001193125-26-338217.txt",
                      "0001193125-26-338217")["events"][0]
        self.assertIn("insider_trading_plan", EVENT_KINDS)
        self.assertEqual(set(event["payload"]), set(PAYLOAD_FIELDS["insider_trading_plan"]))
        validate_payload("insider_trading_plan", event["payload"])
        self.assertEqual(DEFAULT_TIER_BY_KIND["insider_trading_plan"], "primary_filing")
        self.assertNotEqual(event["kind"], "insider_transaction")


class PipelineTests(P14aHarness):
    def setUp(self):
        super().setUp()
        schema = Path(__file__).resolve().parents[1] / "src/dalton_core/document_index_schema.sql"
        self.store.connection.executescript(schema.read_text(encoding="utf-8"))
        text = fixture("two-adoptions-0001193125-26-338217.txt")
        self.store.connection.execute(
            "INSERT INTO document_index_documents(rowid,artifact_version_ref,artifact_version_hash,artifact_ref,artifact_version,artifact_content_hash,title,kind,media_type,access_class,source_record_refs_json,company_refs_json,document_date,source_metadata,extracted_text,extracted_text_ref,extracted_text_hash,extracted_text_size_bytes,input_ref,input_hash,record_json,content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (1, "artifact-version:item5", "d"*64, "artifact:item5", 1, "e"*64,
             "Filed issuer 10-Q", "filing", "text/html", "public",
             json.dumps(["sec:filing:0001193125-26-338217"]), json.dumps([ACN]),
             "2026-07-31", "{}", text, "text:item5", "f"*64, len(text),
             "input:item5", "0"*64, "{}", "1"*64))
        self.store.connection.execute("INSERT INTO document_index_companies VALUES(?,?)", (1, ACN))

    def test_archived_sec_text_flows_through_tracking_into_idempotent_events(self):
        candidates = company_events(
            self.store, self.mission, company_ref=ACN,
            now=datetime(2026, 9, 10, tzinfo=timezone.utc))
        plans = [row for row in candidates if row["kind"] == "insider_trading_plan"]
        self.assertEqual(len(plans), 2)
        authority = ResearchEventAuthority(self.store)
        statuses = []
        for _ in range(2):
            for candidate in plans:
                statuses.append(record_event(
                    authority, company_ref=ACN, kind=candidate["kind"],
                    occurred_at=candidate["occurred_at"], source_refs=candidate["source_refs"],
                    payload=candidate["payload"], evidence_tier=candidate["evidence_tier"],
                    mission=self.mission, actor_ref=AUTOMATION)["status"])
        self.assertEqual(statuses, ["fresh", "fresh", "duplicate", "duplicate"])
        event = authority.events(company_ref=ACN, limit=5)[0]
        context = derived_context(event, connection=self.store.connection)
        prompt = build_judge_prompt({
            "event": event, "company_ref": ACN, "ticker": "ACN",
            "derived_lines": context["lines"], "derived_refs": context["refs"],
        })
        self.assertIn("a plan action, not an executed trade", prompt)

    def test_real_tracking_summary_names_the_plan_events(self):
        self.pass_screen(ACN)
        summary = run_tracking(
            state_dir=self.state_dir, summary_dir=self.state_dir / "summary",
            policy_path=POLICY_PATH, company_ref=ACN,
            now=datetime(2026, 9, 10, tzinfo=timezone.utc),
        )
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["events_by_kind"]["insider_trading_plan"], 2)
        written = ResearchEventAuthority(self.store).events(
            company_ref=ACN, kind="insider_trading_plan")
        self.assertEqual(len(written), 2)


if __name__ == "__main__":
    unittest.main()

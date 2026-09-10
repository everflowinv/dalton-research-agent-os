"""W4: US buyback disclosure -- the Item 2 table, the 8-K, and what it is worth.

Every fixture here is offline.  Two of them are real filings captured once (see
``tests/fixtures/sec-buyback/MANIFEST.json``); nothing in this file reaches the
network and nothing calls a model.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from dalton_core.buyback_context import (
    CONSIDERATIONS,
    SHARE_COUNT_CONCEPTS,
    BuybackContextError,
    build_buyback_context,
    prompt_block,
    scaled,
    statement_figures,
)
from dalton_core.buyback_disclosure import (
    AUTHORISATION_8K_ITEMS,
    BUYBACK_ASPECT,
    DISCLOSURE_KINDS,
    BuybackExtractionError,
    accession_of,
    authorisation_events,
    authorisation_from_text,
    buyback_event_candidates,
    buyback_documents,
    issuer_purchase_events,
    issuer_purchase_rows,
    issuer_purchase_section,
    tie_out,
    transcript_mentions_buyback,
)
from dalton_core.event_judgement import allowed_refs, build_judge_prompt, derived_context
from dalton_core.research_event import (
    DEFAULT_TIER_BY_KIND,
    EVENT_KINDS,
    PAYLOAD_FIELDS,
    ResearchEventAuthority,
    ResearchEventValidationError,
    record_event,
    validate_payload,
)
from dalton_core.source_capability_map import (
    CAPABILITIES,
    CONTENT_KINDS,
    build_map,
    capability,
    prompt_table,
    sources_for,
)
from dalton_core.tracking_cadence import load_policy
from tests.p14a_fixtures import ACN, AUTOMATION, P14aHarness

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sec-buyback"
ITEM2 = FIXTURES / "acn-10q-item2-0001467373-26-000032.txt"
AUTHORISATION = FIXTURES / "8k-authorisation-synthetic.txt"
ITEM2_ACCESSION = "0001467373-26-000032"
POLICY = Path(__file__).resolve().parents[1] / "deploy/phase9/p14a-tracking-policy-v2.json"


def item2_text():
    return ITEM2.read_text(encoding="utf-8")


class SectionTests(unittest.TestCase):
    def test_a_real_item_2_section_is_located_between_its_two_boundaries(self):
        found = issuer_purchase_section(item2_text())
        self.assertEqual(found["status"], "read")
        self.assertIn("Unregistered Sales of Equity Securities", found["blocks"][0])
        # Bounded at the far end by the next item heading, so Item 3's prose is
        # not in the section and the table's four footnotes are.
        self.assertNotIn(
            "Item 3. Defaults Upon Senior Securities", "\n".join(found["blocks"])
        )
        self.assertIn("aggregate available authorization", "\n".join(found["blocks"]))

    def test_the_unit_and_the_currency_come_from_the_filings_own_header(self):
        found = issuer_purchase_section(item2_text())
        self.assertEqual(found["scale"], "millions")
        self.assertEqual(found["currency"], "USD")

    def test_a_document_with_no_such_section_says_so_rather_than_returning_nothing(self):
        found = issuer_purchase_section("Item 1. Business\n\nWe sell consulting.")
        self.assertEqual(found["status"], "absent")
        self.assertIn("no issuer-purchases section", found["reason"])


class TableTests(unittest.TestCase):
    def setUp(self):
        self.parsed = issuer_purchase_rows(item2_text())

    def test_the_three_monthly_rows_are_read_verbatim(self):
        self.assertEqual(self.parsed["status"], "read")
        self.assertEqual(
            [(row["period_label"], row["shares_purchased"], row["average_price_paid"])
             for row in self.parsed["rows"]],
            [
                ("March 1, 2026 — March 31, 2026", "4321766", "201.51"),
                ("April 1, 2026 — April 30, 2026", "1474439", "192.97"),
                ("May 1, 2026 — May 31, 2026", "155586", "180.42"),
            ],
        )

    def test_the_programme_column_and_the_authorisation_column_are_not_confused(self):
        march = self.parsed["rows"][0]
        self.assertEqual(march["shares_purchased_under_plans"], "4302914")
        self.assertEqual(march["remaining_authorisation"], "3524")
        self.assertEqual(march["remaining_authorisation_unit"], "millions")

    def test_an_em_dash_is_a_filed_zero_and_is_marked_as_one(self):
        may = self.parsed["rows"][2]
        self.assertEqual(may["shares_purchased_under_plans"], "0")
        self.assertEqual(may["filed_as_dash"], ["shares_purchased_under_plans"])

    def test_the_total_row_is_kept_apart_from_the_monthly_ones(self):
        self.assertTrue(self.parsed["total"]["is_total"])
        self.assertEqual(self.parsed["total"]["shares_purchased"], "5951791")
        self.assertNotIn(self.parsed["total"], self.parsed["rows"])

    def test_the_rows_tie_out_against_the_filers_own_total(self):
        found = self.parsed["tie_out"]
        self.assertEqual(found["status"], "matched")
        self.assertEqual(found["summed_shares"], "5951791")
        self.assertEqual(found["weighted_average_price"], "198.8431")
        self.assertTrue(found["average_price_within_tolerance"])

    def test_a_row_carries_the_span_it_was_read_from(self):
        march = self.parsed["rows"][0]
        self.assertIn("4,321,766", march["excerpt"])
        self.assertIn("201.51", march["excerpt"])
        self.assertEqual(len(march["excerpt_hash"]), 64)

    def test_a_column_read_into_the_wrong_slot_is_caught_by_the_tie_out(self):
        # The failure this reader is built to notice: every row still verifies
        # against its own span, and the sum is nowhere near the filed total.
        rows = [
            {"shares_purchased": "201", "average_price_paid": "4321766"},
            {"shares_purchased": "192", "average_price_paid": "1474439"},
        ]
        total = {"shares_purchased": "5951791", "average_price_paid": "198.84"}
        self.assertEqual(tie_out(rows, total)["status"], "mismatched")

    def test_a_filing_with_no_total_row_is_normal_and_is_said_out_loud(self):
        self.assertEqual(tie_out([], None)["status"], "no_total_row")

    def test_a_figure_that_is_not_in_its_own_span_is_refused(self):
        from dalton_core.buyback_disclosure import _verify_verbatim

        with self.assertRaises(BuybackExtractionError):
            _verify_verbatim({
                "shares_purchased": "999", "average_price_paid": "1",
                "shares_purchased_under_plans": None, "remaining_authorisation": None,
                "excerpt": "March 1, 2026 | 1 | 2", "filed_as_dash": [],
            })

    def test_a_section_with_no_readable_row_is_refused_rather_than_half_read(self):
        text = (
            "Item 2. Unregistered Sales of Equity Securities\n\n"
            "We did not purchase any shares this quarter.\n\n"
            "Item 3. Defaults"
        )
        found = issuer_purchase_rows(text)
        self.assertEqual(found["status"], "unreadable")
        self.assertIn("half-way", found["reason"])


class AuthorisationTests(unittest.TestCase):
    def setUp(self):
        self.text = AUTHORISATION.read_text(encoding="utf-8")

    def test_an_increase_is_read_with_its_amount_and_its_units(self):
        found = authorisation_from_text(self.text)
        self.assertEqual(found[0]["authorised_amount"], "5.0")
        self.assertEqual(found[0]["authorised_amount_unit"], "billions")
        self.assertEqual(found[0]["authorisation_change"], "increase")
        self.assertEqual(found[0]["currency"], "USD")

    def test_what_remains_available_is_read_where_the_sentence_states_it(self):
        remaining = authorisation_from_text(self.text)[1]
        self.assertEqual(remaining["remaining_authorisation"], "8.2")
        self.assertEqual(remaining["remaining_authorisation_unit"], "billions")

    def test_a_sentence_that_does_not_say_new_or_increase_says_unknown(self):
        self.assertEqual(
            authorisation_from_text(self.text)[1]["authorisation_change"], "unknown"
        )

    def test_a_new_programme_is_told_apart_from_a_top_up(self):
        found = authorisation_from_text(
            "The Board authorized a new $2 billion share repurchase program."
        )
        self.assertEqual(found[0]["authorisation_change"], "new")

    def test_nothing_is_read_from_an_8k_that_is_not_about_repurchases(self):
        self.assertEqual(
            authorisation_from_text(
                "Item 8.01 On June 26, 2026 the Board declared a $0.50 dividend."
            ),
            [],
        )

    def test_an_8k_filed_under_another_item_is_not_a_candidate(self):
        found = authorisation_events(
            self.text, company_ref=ACN, accession="0001467373-26-000300",
            filing_date="2026-06-26", items="5.02",
        )
        self.assertEqual(found["status"], "not_a_candidate")
        self.assertIn("5.02", found["reason"])
        self.assertEqual(AUTHORISATION_8K_ITEMS, ("8.01", "7.01"))

    def test_an_8k_under_a_named_item_is_read(self):
        found = authorisation_events(
            self.text, company_ref=ACN, accession="0001467373-26-000300",
            filing_date="2026-06-26", items="8.01,9.01", exhibit="EX-99.1",
        )
        self.assertEqual(found["status"], "read")
        payload = found["events"][0]["payload"]
        self.assertEqual(payload["exhibit"], "EX-99.1")
        self.assertEqual(payload["items"], "8.01,9.01")
        self.assertEqual(payload["disclosure_kind"], "authorisation")


class EventContractTests(unittest.TestCase):
    def test_the_kind_is_closed_and_carries_a_tier(self):
        self.assertIn("buyback_disclosure", EVENT_KINDS)
        self.assertIn("buyback_disclosure", PAYLOAD_FIELDS)
        self.assertEqual(DEFAULT_TIER_BY_KIND["buyback_disclosure"], "primary_filing")
        self.assertEqual(DISCLOSURE_KINDS, ("issuer_purchases_table", "authorisation"))

    def test_a_table_row_becomes_a_payload_the_contract_accepts(self):
        produced = issuer_purchase_events(
            item2_text(), company_ref=ACN, accession=ITEM2_ACCESSION, form="10-Q",
            filing_date="2026-06-18", period_end="2026-05-31",
            invocation_ref="connector-invocation:sec:" + "a" * 32,
            artifact_hash="c" * 64,
        )
        self.assertEqual(produced["status"], "read")
        self.assertEqual(len(produced["events"]), 3)
        for candidate in produced["events"]:
            validate_payload("buyback_disclosure", candidate["payload"])
            self.assertEqual(candidate["kind"], "buyback_disclosure")
            self.assertIn(f"sec:filing:{ITEM2_ACCESSION}", candidate["source_refs"])

    def test_every_row_names_its_accession_and_its_span(self):
        produced = issuer_purchase_events(
            item2_text(), company_ref=ACN, accession=ITEM2_ACCESSION, form="10-Q",
            filing_date="2026-06-18", period_end=None,
        )
        for candidate in produced["events"]:
            payload = candidate["payload"]
            self.assertEqual(payload["accession"], ITEM2_ACCESSION)
            self.assertEqual(len(payload["excerpt_hash"]), 64)
            self.assertIn(payload["shares_purchased"], payload["excerpt"].replace(",", ""))

    def test_two_rows_of_one_filing_have_different_event_keys(self):
        produced = issuer_purchase_events(
            item2_text(), company_ref=ACN, accession=ITEM2_ACCESSION, form="10-Q",
            filing_date="2026-06-18", period_end=None,
        )
        keys = {candidate["payload"]["event_key"] for candidate in produced["events"]}
        self.assertEqual(len(keys), 3)

    def test_a_field_the_contract_does_not_declare_is_refused(self):
        produced = issuer_purchase_events(
            item2_text(), company_ref=ACN, accession=ITEM2_ACCESSION, form="10-Q",
            filing_date="2026-06-18", period_end=None,
        )
        payload = dict(produced["events"][0]["payload"], management_says="great value")
        with self.assertRaises(ResearchEventValidationError):
            validate_payload("buyback_disclosure", payload)

    def test_a_filing_with_no_date_produces_no_event(self):
        produced = issuer_purchase_events(
            item2_text(), company_ref=ACN, accession=ITEM2_ACCESSION, form="10-Q",
            filing_date=None, period_end=None,
        )
        self.assertEqual(produced["status"], "undated")


class DocumentReaderTests(unittest.TestCase):
    """Where the text comes from: the document index, never the network."""

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        schema = (
            Path(__file__).resolve().parents[1]
            / "src/dalton_core/document_index_schema.sql"
        )
        self.connection.executescript(schema.read_text(encoding="utf-8"))

    def add(self, *, title, text, refs, rowid=1, date="2026-06-18"):
        self.connection.execute(
            "INSERT INTO document_index_documents(rowid,artifact_version_ref,"
            "artifact_version_hash,artifact_ref,artifact_version,artifact_content_hash,"
            "title,kind,media_type,access_class,source_record_refs_json,"
            "company_refs_json,document_date,source_metadata,extracted_text,"
            "extracted_text_ref,extracted_text_hash,extracted_text_size_bytes,"
            "input_ref,input_hash,record_json,content_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rowid, f"artifact-version:{rowid}", "d" * 64, f"artifact:{rowid}", 1,
             "e" * 64, title, "filing", "text/html", "public", json.dumps(refs),
             json.dumps([ACN]), date, "{}", text, f"text:{rowid}", "f" * 64,
             len(text), f"input:{rowid}", "0" * 64, "{}", "1" * 64),
        )
        self.connection.execute(
            "INSERT INTO document_index_companies VALUES(?,?)", (rowid, ACN)
        )

    def test_an_accession_is_taken_from_a_record_ref_or_an_edgar_path(self):
        self.assertEqual(
            accession_of('["sec:filing:0001467373-26-000032"]'), ITEM2_ACCESSION
        )
        self.assertEqual(
            accession_of(
                "https://www.sec.gov/Archives/edgar/data/1467373/"
                "000146737326000032/acn-20260531.htm"
            ),
            ITEM2_ACCESSION,
        )
        self.assertIsNone(accession_of("no accession here", None, 7))

    def test_the_reader_finds_filings_and_names_the_ones_it_cannot_attribute(self):
        self.add(title="ACN 10-Q for the quarter ended May 31 2026",
                 text=item2_text(), refs=["sec:filing:" + ITEM2_ACCESSION])
        self.add(rowid=2, title="ACN 10-Q (unattributable)", text=item2_text(),
                 refs=["some-other-ref"])
        found = buyback_documents(self.connection, company_ref=ACN)
        self.assertEqual([row["form"] for row in found], ["10-Q", "10-Q"])
        self.assertEqual(
            sorted(str(row["accession"]) for row in found),
            [ITEM2_ACCESSION, "None"],
        )

    def test_candidates_are_produced_and_the_unattributable_one_is_skipped(self):
        self.add(title="ACN 10-Q for the quarter ended May 31 2026",
                 text=item2_text(), refs=["sec:filing:" + ITEM2_ACCESSION])
        self.add(rowid=2, title="ACN 10-Q (unattributable)", text=item2_text(),
                 refs=["some-other-ref"])
        found = buyback_event_candidates(self.connection, company_ref=ACN)
        self.assertEqual(len(found["events"]), 3)
        self.assertEqual(len(found["skipped"]), 1)
        self.assertIn("no accession", found["skipped"][0]["reason"])
        self.assertEqual(found["tie_outs"][0]["status"], "matched")

    def test_a_core_with_no_document_index_yields_nothing_rather_than_raising(self):
        empty = sqlite3.connect(":memory:")
        empty.row_factory = sqlite3.Row
        self.addCleanup(empty.close)
        self.assertEqual(buyback_documents(empty, company_ref=ACN), [])

    def test_a_transcript_mention_stays_a_transcript_and_is_tagged_by_lookup(self):
        self.connection.execute(
            "CREATE TABLE claim_versions(claim_version_id TEXT PRIMARY KEY, "
            "claim_ref TEXT, claim_json TEXT, created_at TEXT)"
        )
        self.connection.execute(
            "INSERT INTO claim_versions VALUES(?,?,?,?)",
            ("claim-b1", "claim-b1", json.dumps({
                "subject_ref": ACN, "aspect": BUYBACK_ASPECT,
                "source_ref": "document:transcript-1",
                "statement": "We expect to continue repurchasing shares at a "
                             "similar pace next quarter.",
            }), "2026-06-25T00:00:00+00:00"),
        )
        found = transcript_mentions_buyback(
            self.connection, company_ref=ACN, document_ref="document:transcript-1"
        )
        self.assertEqual(found["status"], "read")
        self.assertEqual(found["claims"][0]["aspect"], BUYBACK_ASPECT)
        self.assertTrue(found["claims"][0]["is_this_document"])
        # No new event kind was invented for it.
        self.assertNotIn("transcript_buyback", EVENT_KINDS)


class StatementFigureTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript(
            "CREATE TABLE coverage_mission_statement_filings("
            "ingest_id TEXT PRIMARY KEY, company_ref TEXT, accession TEXT, "
            "form TEXT, filed TEXT, report_date TEXT);"
            "CREATE TABLE coverage_mission_statement_lines("
            "line_id TEXT PRIMARY KEY, ingest_id TEXT, statement TEXT, ordinal INTEGER,"
            " concept TEXT, label TEXT, level INTEGER, parent_concept TEXT,"
            " is_breakdown INTEGER, dimension_axis TEXT, dimension_member TEXT,"
            " period_start TEXT, period_end TEXT, value TEXT, unit TEXT, balance TEXT);"
        )

    def line(self, ingest, concept, period_end, value, ordinal):
        self.connection.execute(
            "INSERT INTO coverage_mission_statement_lines VALUES"
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{ingest}-{ordinal}", ingest, "cash", ordinal, concept, concept, 1,
             None, 0, None, None, None, period_end, value, "USD", None),
        )

    def filing(self, ingest, accession, filed):
        self.connection.execute(
            "INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?,?,?)",
            (ingest, ACN, accession, "10-Q", filed, filed),
        )

    def test_free_cash_flow_is_computed_from_two_named_lines(self):
        for index, (period, ocf, capex) in enumerate((
            ("2026-05-31", "1000", "100"), ("2026-02-28", "900", "90"),
            ("2025-11-30", "800", "80"), ("2025-08-31", "700", "70"),
        )):
            ingest = f"ingest-{index}"
            self.filing(ingest, f"0001467373-26-00010{index}", f"2026-0{index + 1}-01")
            self.line(ingest, "NetCashProvidedByUsedInOperatingActivities", period, ocf, 1)
            self.line(ingest, "PaymentsToAcquirePropertyPlantAndEquipment", period, capex, 2)
            self.line(ingest, SHARE_COUNT_CONCEPTS[0], period, str(600 - index), 3)
        found = statement_figures(self.connection, ACN)
        self.assertEqual(found["free_cash_flow_status"], "read")
        self.assertEqual(found["free_cash_flow"], "3060")
        self.assertEqual(len(found["free_cash_flow_periods"]), 4)
        self.assertEqual(found["share_counts"][0]["value"], "600")

    def test_a_core_with_no_cash_flow_line_says_so_rather_than_reporting_zero(self):
        found = statement_figures(self.connection, ACN)
        self.assertEqual(found["free_cash_flow_status"], "unavailable")
        self.assertIn("it is not zero and it is not", found["free_cash_flow_reason"])

    def test_a_core_with_no_statement_tables_at_all_is_not_an_error(self):
        empty = sqlite3.connect(":memory:")
        empty.row_factory = sqlite3.Row
        self.addCleanup(empty.close)
        self.assertEqual(
            statement_figures(empty, ACN)["free_cash_flow_status"], "unavailable"
        )


def buyback_event(payload, *, event_id="research-event:buyback-1"):
    return {
        "id": event_id, "company_ref": ACN, "kind": "buyback_disclosure",
        "occurred_at": "2026-06-18T00:00:00+00:00",
        "evidence_tier": "primary_filing",
        "source_refs": [f"sec:filing:{payload['accession']}"],
        "payload": payload,
    }


def table_payload(**overrides):
    payload = {
        "disclosure_kind": "issuer_purchases_table",
        "accession": ITEM2_ACCESSION, "form": "10-Q", "filing_date": "2026-06-18",
        "period_end": "2026-05-31", "period_label": "March 1, 2026 — March 31, 2026",
        "shares_purchased": "4321766", "average_price_paid": "201.51",
        "shares_purchased_under_plans": "4302914", "remaining_authorisation": "3524",
        "remaining_authorisation_unit": "millions", "currency": "USD",
        "authorised_amount": None, "authorised_amount_unit": None,
        "authorisation_change": None, "announced_date": None, "items": None,
        "exhibit": None, "document_ref": None, "source_ref": "source:sec-edgar",
        "excerpt": "March 1, 2026 — March 31, 2026 | 4,321,766 | $ | 201.51",
        "excerpt_hash": "a" * 64,
        "invocation_ref": "connector-invocation:sec:" + "a" * 32,
        "artifact_hash": "c" * 64, "event_key": "row-1",
    }
    payload.update(overrides)
    return payload


class ContextTests(unittest.TestCase):
    def test_a_kind_this_does_not_read_is_refused(self):
        with self.assertRaises(BuybackContextError):
            build_buyback_context({"kind": "filing", "payload": {}})

    def test_price_paid_is_compared_with_the_last_close(self):
        found = build_buyback_context(
            buyback_event(table_payload()),
            price={"close": "189.10", "as_of": "2026-09-09",
                   "version_ref": "market-price-series-version:1"},
        )["price_comparison"]
        self.assertEqual(found["status"], "read")
        self.assertEqual(found["direction"], "above")
        self.assertEqual(found["paid_versus_close_percent"], "6.56")
        self.assertIn("market-price-series-version:1", found["refs"])

    def test_with_no_price_series_the_comparison_says_so(self):
        found = build_buyback_context(buyback_event(table_payload()))["price_comparison"]
        self.assertEqual(found["status"], "unavailable")
        self.assertIn("no price series", found["reason"])

    def test_an_unlabelled_authorisation_is_not_scaled_by_guesswork(self):
        self.assertIsNone(scaled("3244", None))
        self.assertIsNone(scaled("3244", "cubits"))
        self.assertEqual(scaled("3244", "millions"), 3244 * 10 ** 6)
        found = build_buyback_context(
            buyback_event(table_payload(remaining_authorisation_unit=None))
        )["pace"]
        self.assertEqual(found["status"], "unavailable")
        self.assertIn("factor of a", found["reason"])

    def test_the_pace_is_the_authorisation_divided_by_what_is_going_out(self):
        found = build_buyback_context(buyback_event(table_payload()))["pace"]
        self.assertEqual(found["status"], "read")
        self.assertEqual(found["remaining_authorisation"], "3524000000")
        self.assertEqual(found["months_of_authorisation_left"], "12.1")

    def test_one_quarter_is_not_a_trend_and_the_reason_says_when_it_will_be(self):
        found = build_buyback_context(buyback_event(table_payload()))["trend"]
        self.assertEqual(found["status"], "unavailable")
        self.assertIn("after the next 10-Q", found["reason"])

    def test_two_quarters_make_a_trend(self):
        prior = buyback_event(
            table_payload(
                accession="0001467373-26-000014", filing_date="2026-03-19",
                shares_purchased="2000000", average_price_paid="300",
                period_label="December 1, 2025 — December 31, 2025",
                event_key="row-prior",
            ),
            event_id="research-event:buyback-prior",
        )
        found = build_buyback_context(
            buyback_event(table_payload()), company_events=[prior]
        )["trend"]
        self.assertEqual(found["status"], "read")
        self.assertEqual(len(found["series"]), 2)
        self.assertEqual(found["quarter_on_quarter_percent"], "45.15")

    def test_size_is_reported_against_market_cap_and_trailing_free_cash_flow(self):
        found = build_buyback_context(
            buyback_event(table_payload()),
            market_cap={"value": "120000000000", "as_of": "2026-09-09",
                        "version_ref": "market-price-series-version:1"},
            statements={"free_cash_flow": "9000000000", "free_cash_flow_periods": [1, 2],
                        "refs": ["0001467373-26-000032"]},
        )["size"]
        self.assertEqual(found["percent_of_market_cap"], "0.726")
        self.assertEqual(found["percent_of_trailing_fcf"], "9.68")

    def test_a_missing_input_is_named_rather_than_left_blank(self):
        found = build_buyback_context(buyback_event(table_payload()))["size"]
        self.assertIsNone(found["percent_of_market_cap"])
        self.assertIn("no market capitalisation", found["percent_of_market_cap_reason"])
        self.assertIn("no trailing free cash flow", found["percent_of_trailing_fcf_reason"])

    def test_a_buyback_that_only_offsets_issuance_is_named_as_such(self):
        statements = {
            "share_counts": [
                {"period_end": "2026-05-31", "value": "620000000",
                 "concept": SHARE_COUNT_CONCEPTS[0], "accession": "a-1"},
                {"period_end": "2026-02-28", "value": "620000000",
                 "concept": SHARE_COUNT_CONCEPTS[0], "accession": "a-2"},
            ],
        }
        found = build_buyback_context(
            buyback_event(table_payload()), statements=statements
        )["dilution_offset"]
        self.assertEqual(found["status"], "read")
        self.assertTrue(found["offsets_issuance_only"])
        self.assertEqual(found["percent_of_buyback_that_reached_the_count"], "0.0")

    def test_a_buyback_that_shrinks_the_company_is_measured(self):
        statements = {
            "share_counts": [
                {"period_end": "2026-05-31", "value": "615678234",
                 "concept": SHARE_COUNT_CONCEPTS[0], "accession": "a-1"},
                {"period_end": "2026-02-28", "value": "620000000",
                 "concept": SHARE_COUNT_CONCEPTS[0], "accession": "a-2"},
            ],
        }
        found = build_buyback_context(
            buyback_event(table_payload()), statements=statements
        )["dilution_offset"]
        self.assertFalse(found["offsets_issuance_only"])
        self.assertEqual(found["percent_of_buyback_that_reached_the_count"], "100.0")

    def test_an_authorisation_has_no_price_to_compare_and_says_why(self):
        payload = table_payload(
            disclosure_kind="authorisation", average_price_paid=None,
            shares_purchased=None, authorised_amount="5.0",
            authorised_amount_unit="billions", authorisation_change="increase",
            announced_date="2026-06-26",
        )
        found = build_buyback_context(buyback_event(payload))
        self.assertIn("announces no purchases", found["price_comparison"]["reason"])
        text = "\n".join(prompt_block(found))
        self.assertIn("a permission, not a purchase", text)

    def test_the_block_prints_the_considerations_and_decides_nothing(self):
        text = "\n".join(prompt_block(build_buyback_context(buyback_event(table_payload()))))
        self.assertIn(CONSIDERATIONS[0][:40], text)
        self.assertIn("Only Hong Kong discloses", text)
        for word in ("NO_CHANGE", "THESIS_WEAKENED", "THESIS_BROKEN"):
            self.assertNotIn(word, text)


class CapabilityMapTests(unittest.TestCase):
    def test_the_new_content_kinds_are_in_the_closed_vocabulary(self):
        self.assertIn("buyback_disclosure", CONTENT_KINDS)
        self.assertIn("ownership_filing", CONTENT_KINDS)
        for slug, entry in CAPABILITIES.items():
            for kind in entry["content_kinds"]:
                self.assertIn(kind, CONTENT_KINDS, slug)

    def test_the_sec_row_says_what_it_cannot_deliver(self):
        note = capability("sec")["note"]
        self.assertIn("CANNOT deliver: a daily buyback figure", note)
        self.assertIn("10-Q/10-K", note)
        self.assertIn("8-K", note)
        self.assertIn("Hong Kong", note)
        self.assertIn("being built in parallel", note)

    def test_the_ownership_operations_are_their_own_row_on_the_sec_profile(self):
        entry = capability("sec-ownership")
        self.assertTrue(entry["in_inventory"])
        self.assertEqual(entry["source_ref"], "source:sec-edgar")
        self.assertEqual(
            sorted(entry["operations"]),
            ["beneficial_ownership", "form13f_holdings", "form144_notices",
             "form4_transactions"],
        )
        self.assertNotIn("get_company_facts", entry["operations"])
        # And it does not borrow the whole connector's quota table with the
        # profile it borrows.
        operations = {dict(row)["operation"] for row in entry["quotas"]}
        self.assertNotIn("list_filings", operations)

    def test_hong_kong_daily_returns_are_named_as_somebody_elses_connector(self):
        note = capability("cn-hk-findata")["note"]
        self.assertIn("HKEX daily buyback return", note)
        self.assertIn("this slice does not build it", note)

    def test_the_brain_can_ask_where_a_buyback_disclosure_comes_from(self):
        self.assertEqual(sources_for("buyback_disclosure"), ["cn-hk-findata", "sec"])
        self.assertIn("sec-ownership", sources_for("ownership_filing"))

    def test_the_map_still_builds_and_the_prompt_table_still_renders(self):
        policy = load_policy(POLICY)
        projection = build_map(cadences=policy["cadences"])
        self.assertEqual(len(projection["content_hash"]), 64)
        by_slug = {row["slug"]: row for row in projection["sources"]}
        self.assertEqual(by_slug["sec-ownership"]["cadence_baseline_seconds"], 86400)
        self.assertFalse(by_slug["sec-ownership"]["cadence_adjustable"])
        self.assertIn("source | content it yields", prompt_table(projection))


class CadenceTests(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy(POLICY)

    def test_v1_policy_bytes_remain_frozen(self):
        import hashlib

        v1 = POLICY.with_name("p14a-tracking-policy-v1.json")
        self.assertEqual(
            hashlib.sha256(v1.read_bytes()).hexdigest(),
            "714e752c96bb5d66eef4249216267c13ee52cf4a81bc31067b0fecfadb89bc10",
        )

    def test_new_tracking_baselines_have_a_new_policy_identity(self):
        self.assertEqual(self.policy["policy_ref"], "tracking-policy:p14a:v2")

    def test_the_ownership_lane_and_the_filings_index_are_both_resident(self):
        self.assertIn("sec", self.policy["cadences"])
        self.assertIn("sec-ownership", self.policy["cadences"])

    def test_form_4_is_read_once_a_trading_day_and_the_brain_cannot_slow_it(self):
        ownership = self.policy["cadences"]["sec-ownership"]
        self.assertEqual(ownership["interval_seconds"], 86400)
        self.assertFalse(ownership["adjustable"])
        self.assertIn("two business days", ownership["because"])

    def test_the_filings_cadence_states_that_us_buybacks_have_no_faster_source(self):
        self.assertIn(
            "no daily buyback return in America", self.policy["cadences"]["sec"]["because"]
        )


class TrackingLaneTests(P14aHarness):
    """The producer is in the resident daily scan, and its events record."""

    def setUp(self):
        super().setUp()
        self.events = ResearchEventAuthority(self.store)

    def test_the_tracking_lane_scans_for_buyback_events(self):
        from dalton_core.tracking_lane_cli import company_events as scan

        # No document index on this Core: the scan must be quiet about it
        # rather than failing the whole day's tracking.
        found = scan(self.store, self.mission, company_ref=ACN,
                     now=__import__("datetime").datetime(
                         2026, 9, 10, tzinfo=__import__("datetime").timezone.utc))
        self.assertIsInstance(found, list)

    def test_a_buyback_event_records_and_re_recording_it_is_a_duplicate(self):
        candidate = issuer_purchase_events(
            item2_text(), company_ref=ACN, accession=ITEM2_ACCESSION, form="10-Q",
            filing_date="2026-06-18", period_end="2026-05-31",
        )["events"][0]
        first = record_event(
            self.events, company_ref=ACN, kind="buyback_disclosure",
            occurred_at=candidate["occurred_at"], source_refs=candidate["source_refs"],
            payload=candidate["payload"], evidence_tier=candidate["evidence_tier"],
            mission=self.mission, actor_ref=AUTOMATION,
        )
        self.assertEqual(first["status"], "fresh")
        again = record_event(
            self.events, company_ref=ACN, kind="buyback_disclosure",
            occurred_at=candidate["occurred_at"], source_refs=candidate["source_refs"],
            payload=candidate["payload"], evidence_tier=candidate["evidence_tier"],
            mission=self.mission, actor_ref=AUTOMATION,
        )
        self.assertEqual(again["status"], "duplicate")

    def test_the_derived_block_reaches_the_judge_prompt_with_citable_refs(self):
        rows = issuer_purchase_events(
            item2_text(), company_ref=ACN, accession=ITEM2_ACCESSION, form="10-Q",
            filing_date="2026-06-18", period_end="2026-05-31",
        )["events"]
        written = [
            record_event(
                self.events, company_ref=ACN, kind="buyback_disclosure",
                occurred_at=row["occurred_at"], source_refs=row["source_refs"],
                payload=row["payload"], evidence_tier=row["evidence_tier"],
                mission=self.mission, actor_ref=AUTOMATION,
            )
            for row in rows
        ]
        subject = written[0]
        found = derived_context(
            subject, connection=self.store.connection,
            recent_events=self.events.events(company_ref=ACN, limit=40),
            price={"close": "189.10", "as_of": "2026-09-09",
                   "version_ref": "market-price-series-version:1"},
        )
        self.assertEqual(found["context"]["price_comparison"]["direction"], "above")
        context = {
            "event": subject, "company_ref": ACN, "ticker": "ACN",
            "derived_lines": found["lines"], "derived_refs": found["refs"],
        }
        prompt = build_judge_prompt(context)
        self.assertIn("Derived buyback context", prompt)
        self.assertIn("months of programme left", prompt)
        self.assertIn("market-price-series-version:1", allowed_refs(context))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

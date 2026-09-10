"""W4: the readers, against documents HKEX really served on 2026-09-10.

Every fixture under ``tests/fixtures/hkex-filings`` was captured once, by
``scripts/capture_hkex_fixtures.py``, from the URLs recorded on each capture.
Nothing here reaches the network and nothing here needs a third-party library:
the Exchange's workbook is held as the canonical text grid the capture script
made of it, and the one test that opens the workbook itself skips when ``xlrd``
is absent.

The numbers asserted below are real and checkable. Tencent (00700) bought back
230,000 shares on 2026-09-08 between HKD 435.20 and HKD 439.60 for
HKD 100,442,863.00, and had retired 44,382,700 shares -- 0.48676% of itself --
under the mandate its shareholders granted on 13 May 2026.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from dalton_core.hkex_filings_adapter import (
    HkexFilingsParseError,
    average_price_paid,
    buyback_cluster_key,
    buyback_context,
    buyback_events,
    buyback_pace,
    buyback_report_printed_on,
    buyback_rows,
    di_context,
    di_corporation_from_list,
    di_events,
    di_form_detail,
    di_notice_rows,
    month_ended,
    parse_capture,
    positions,
    stock_id_from_prefix,
    title_search_payload,
)
from dalton_core.hkex_filings_core import (
    ANNOUNCEMENTS_INDEX_OPERATION,
    DISCLOSURE_OF_INTERESTS_OPERATION,
    MONTHLY_RETURNS_OPERATION,
    NEXT_DAY_DISCLOSURE_OPERATION,
    HkexFilingsError,
    company_ref,
    hkex_output_schema,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hkex-filings"
COMPANY = company_ref("00700")


def capture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def document(payload: dict, role: str) -> dict:
    return next(item for item in payload["documents"] if item["role"] == role)


class FixtureProvenanceTests(unittest.TestCase):
    def test_every_capture_records_the_url_and_hash_it_came_from(self) -> None:
        for name in ("next-day-disclosure-00700-20260909.json",
                     "monthly-returns-00700.json",
                     "announcements-index-00700.json",
                     "disclosure-of-interests-00700.json"):
            payload = capture(name)
            self.assertTrue(payload["documents"], name)
            for item in payload["documents"]:
                self.assertTrue(item["url"].startswith("https://"), name)
                self.assertEqual(len(item["sha256"]), 64, name)
                self.assertGreater(item["byte_length"], 0, name)

    def test_the_workbook_grid_is_the_workbook(self) -> None:
        # The capture holds the sheet as text so the offline tests need no
        # reader; this is the assertion that says the text really is that file.
        try:
            import xlrd  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("xlrd is the optional hk-filings extra")
        import hashlib

        from dalton_core.hkex_filings_cli import _workbook_grid

        raw = (FIXTURES / "share-buyback-report-20260909.xls").read_bytes()
        payload = capture("next-day-disclosure-00700-20260909.json")
        held = document(payload, "share_buyback_report")
        self.assertEqual(hashlib.sha256(raw).hexdigest(), held["sha256"])
        self.assertEqual(_workbook_grid(raw), held["grid"])


class BuybackReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capture = capture("next-day-disclosure-00700-20260909.json")
        self.grid = document(self.capture, "share_buyback_report")["grid"]

    def test_the_trading_date_is_the_day_before_the_printed_date(self) -> None:
        # A *next day* disclosure return. A wire that took the report date as
        # the trading date would file every purchase a day late.
        self.assertEqual(buyback_report_printed_on(self.grid), "2026-09-09")
        rows, _ = buyback_rows(self.grid, ticker="00700")
        self.assertEqual(rows[0]["trading_date"], "2026-09-08")

    def test_the_figures_are_the_text_the_exchange_printed(self) -> None:
        rows, _ = buyback_rows(self.grid, ticker="00700")
        row = rows[0]
        self.assertEqual(row["shares_repurchased"], "230,000")
        self.assertEqual(row["highest_price"], "439.60")
        self.assertEqual(row["lowest_price"], "435.20")
        self.assertEqual(row["aggregate_price_paid"], "100,442,863.00")
        self.assertEqual(row["mandate_to_date_shares"], "44,382,700")
        self.assertEqual(row["mandate_to_date_pct_of_issued"], "0.48676")
        # The thousands separators and the trailing zeros are kept: an issuer
        # that reported 439.60 reported two decimal places on purpose.
        for field in ("shares_repurchased", "highest_price", "aggregate_price_paid"):
            self.assertIsInstance(row[field], str)

    def test_the_currency_is_its_own_field(self) -> None:
        # The same report carries GBP and USD rows and two currencies added
        # together is a number about nothing.
        rows, _ = buyback_rows(self.grid, ticker="00700")
        self.assertEqual(rows[0]["currency"], "HKD")
        self.assertNotIn("HKD", rows[0]["highest_price"])

    def test_the_report_is_market_wide_and_says_how_wide(self) -> None:
        # Without the count, "this company bought nothing back" and "the table
        # came back short" are the same empty answer.
        rows, universe = buyback_rows(self.grid, ticker="00700")
        self.assertEqual(len(rows), 1)
        self.assertGreater(universe, 100)

    def test_an_issuer_that_bought_nothing_is_an_answer_not_a_failure(self) -> None:
        rows, universe = buyback_rows(self.grid, ticker="00001")
        self.assertEqual(rows, [])
        self.assertGreater(universe, 100)

    def test_the_unpadded_code_in_the_report_is_the_padded_one_asked_for(self) -> None:
        # The Exchange writes 711; HKEXnews writes 00711.
        rows, _ = buyback_rows(self.grid, ticker="00711")
        self.assertEqual([row["stock_code"] for row in rows], ["00711"])
        self.assertEqual(rows[0]["company_name"], "ASIA ALLIED INF")

    def test_a_missing_lowest_price_stays_missing(self) -> None:
        # Left blank when every purchase that day was at one price. Absent has
        # to look absent rather than like a zero.
        rows, _ = buyback_rows(self.grid, ticker="00711")
        self.assertIsNone(rows[0]["lowest_price"])
        self.assertEqual(rows[0]["highest_price"], "0.43")

    def test_the_footnotes_at_the_end_are_not_rows(self) -> None:
        # The report ends with several paragraphs in column A, one of them
        # eight hundred characters long.
        _rows, universe = buyback_rows(self.grid, ticker="00700")
        codes = [line[1] for line in self.grid if len(line) > 1 and line[1].isdigit()]
        self.assertEqual(universe, len(codes))

    def test_a_workbook_that_is_not_the_report_is_refused(self) -> None:
        with self.assertRaises(HkexFilingsParseError):
            buyback_rows([["Something", "else"]], ticker="00700")

    def test_the_wire_satisfies_the_frozen_contract(self) -> None:
        from dalton_core.authority_resolver import _schema_matches

        wire = parse_capture(NEXT_DAY_DISCLOSURE_OPERATION, self.capture,
                             ticker="00700", artifact_hash="a" * 64)
        _schema_matches(wire, hkex_output_schema(NEXT_DAY_DISCLOSURE_OPERATION),
                        "output")
        self.assertEqual(wire["row_count"], 1)

    def test_reading_the_same_capture_twice_gives_the_same_bytes(self) -> None:
        first = parse_capture(NEXT_DAY_DISCLOSURE_OPERATION, self.capture,
                              ticker="00700", artifact_hash="a" * 64)
        second = parse_capture(NEXT_DAY_DISCLOSURE_OPERATION, capture(
            "next-day-disclosure-00700-20260909.json"),
            ticker="00700", artifact_hash="a" * 64)
        self.assertEqual(first, second)


class TitleSearchTests(unittest.TestCase):
    def test_the_servlet_result_is_json_inside_json(self) -> None:
        payload = capture("announcements-index-00700.json")
        parsed = title_search_payload(document(payload, "title_search")["text"])
        self.assertIsInstance(parsed["rows"], list)
        self.assertGreater(parsed["envelope"]["recordCnt"], 0)

    def test_a_body_that_is_not_json_is_refused(self) -> None:
        with self.assertRaises(HkexFilingsParseError):
            title_search_payload("<html>maintenance</html>")

    def test_the_stock_id_comes_out_of_the_jsonp_wrapper(self) -> None:
        payload = capture("announcements-index-00700.json")
        self.assertEqual(
            stock_id_from_prefix(document(payload, "stock_prefix")["text"],
                                 ticker="00700"),
            7609,
        )

    def test_a_code_that_names_no_security_is_refused(self) -> None:
        payload = capture("announcements-index-00700.json")
        with self.assertRaises(HkexFilingsParseError):
            stock_id_from_prefix(document(payload, "stock_prefix")["text"],
                                 ticker="00001")

    def test_a_prefix_body_that_is_not_jsonp_is_refused(self) -> None:
        with self.assertRaises(HkexFilingsParseError):
            stock_id_from_prefix("not jsonp at all", ticker="00700")

    def test_the_renminbi_counter_is_kept_rather_than_dropped(self) -> None:
        # One announcement filed under 00700 and 80700. Dropping the second is
        # how a document filed under 80700 stops being Tencent's.
        wire = parse_capture(ANNOUNCEMENTS_INDEX_OPERATION,
                             capture("announcements-index-00700.json"),
                             ticker="00700", artifact_hash="a" * 64)
        self.assertIn("80700", wire["rows"][0]["stock_codes"])
        self.assertIn("00700", wire["rows"][0]["stock_codes"])

    def test_the_index_sees_the_buyback_returns_it_is_there_for(self) -> None:
        wire = parse_capture(ANNOUNCEMENTS_INDEX_OPERATION,
                             capture("announcements-index-00700.json"),
                             ticker="00700", artifact_hash="a" * 64)
        titles = [row["title"] for row in wire["rows"]]
        self.assertTrue(any("Next Day Disclosure Return" in title
                            for title in titles))
        self.assertTrue(any("Monthly Return" in title for title in titles))

    def test_the_filing_time_is_hong_kong_time(self) -> None:
        wire = parse_capture(ANNOUNCEMENTS_INDEX_OPERATION,
                             capture("announcements-index-00700.json"),
                             ticker="00700", artifact_hash="a" * 64)
        self.assertEqual(wire["rows"][0]["filed_at"], "2026-09-09T17:27:00+08:00")
        self.assertEqual(wire["rows"][0]["filed_on"], "2026-09-09")

    def test_record_count_travels_beside_row_count(self) -> None:
        wire = parse_capture(ANNOUNCEMENTS_INDEX_OPERATION,
                             capture("announcements-index-00700.json"),
                             ticker="00700", artifact_hash="a" * 64)
        self.assertEqual(wire["record_count"], 24)
        self.assertEqual(wire["row_count"], 24)
        self.assertFalse(wire["has_next_row"])

    def test_the_announcements_wire_satisfies_the_frozen_contract(self) -> None:
        from dalton_core.authority_resolver import _schema_matches

        wire = parse_capture(ANNOUNCEMENTS_INDEX_OPERATION,
                             capture("announcements-index-00700.json"),
                             ticker="00700", artifact_hash="a" * 64)
        _schema_matches(wire, hkex_output_schema(ANNOUNCEMENTS_INDEX_OPERATION),
                        "output")


class MonthlyReturnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wire = parse_capture(MONTHLY_RETURNS_OPERATION,
                                  capture("monthly-returns-00700.json"),
                                  ticker="00700", artifact_hash="a" * 64)

    def test_the_month_is_read_out_of_the_title(self) -> None:
        self.assertEqual(self.wire["rows"][0]["period_end"], "2026-08-31")
        self.assertEqual(month_ended("... for the month ended 1 March 2026"),
                         "2026-03-01")
        self.assertIsNone(month_ended("Annual Report 2026"))
        self.assertIsNone(month_ended(None))

    def test_it_claims_no_figures_and_says_so_on_the_wire(self) -> None:
        # HKEXnews serves the Monthly Return only as a PDF, and a number read
        # out of a form layout is a number nobody can check.
        self.assertFalse(self.wire["figures_available"])
        self.assertTrue(self.wire["rows"][0]["caliber_note"])

    def test_the_document_is_located_even_though_it_is_not_read(self) -> None:
        row = self.wire["rows"][0]
        self.assertEqual(row["file_type"], "PDF")
        self.assertTrue(row["file_link"].startswith("/listedco/"))

    def test_the_monthly_wire_satisfies_the_frozen_contract(self) -> None:
        from dalton_core.authority_resolver import _schema_matches

        _schema_matches(self.wire, hkex_output_schema(MONTHLY_RETURNS_OPERATION),
                        "output")


class DisclosureOfInterestsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.capture = capture("disclosure-of-interests-00700.json")
        self.wire = parse_capture(DISCLOSURE_OF_INTERESTS_OPERATION, self.capture,
                                  ticker="00700", artifact_hash="b" * 64)

    def test_the_corporation_key_is_read_out_of_the_lists_own_link(self) -> None:
        # NSAllFormList answers with whatever corporation the sid names and
        # ignores the stock code beside it: sid=6 returns Power Assets. A
        # guessed sid returns a complete, well-formed page of another company's
        # directors.
        found = di_corporation_from_list(
            document(self.capture, "di_corp_list")["text"], ticker="00700"
        )
        self.assertEqual(found["sid"], 6893)
        self.assertEqual(found["corporation_name"], "Tencent Holdings Ltd.")

    def test_a_stock_code_the_list_does_not_name_is_refused(self) -> None:
        with self.assertRaises(HkexFilingsParseError):
            di_corporation_from_list(
                document(self.capture, "di_corp_list")["text"], ticker="00001"
            )

    def test_a_page_that_is_not_a_notice_list_is_refused(self) -> None:
        with self.assertRaises(HkexFilingsParseError):
            di_notice_rows("<html><body><table><tr><td>nothing</td></tr></table></body></html>")

    def test_an_empty_cell_is_empty_rather_than_the_next_column(self) -> None:
        # The bug this table parser exists to prevent: with the blanks dropped,
        # a notice with no average price reported the shares interested as the
        # price.
        row = next(item for item in self.wire["rows"]
                   if item["form_serial_number"] == "DA20260819E00389")
        self.assertIsNone(row["average_price"])
        self.assertEqual(row["shares_interested"], "44,172")

    def test_one_cell_holding_two_positions_is_two_facts(self) -> None:
        # `63,494(L)0(S)` is a long position and a short one with no separator.
        self.assertEqual(positions("63,494(L)0(S)"), {"L": "63,494", "S": "0"})
        self.assertEqual(positions("HKD 503.0000"), {"L": "HKD 503.0000"})
        self.assertEqual(positions(""), {})
        row = next(item for item in self.wire["rows"]
                   if item["form_serial_number"] == "DA20260415E00531")
        self.assertEqual(row["shares_interested"], "63,494")
        self.assertEqual(row["short_position_interested"], "0")

    def test_the_average_price_carries_its_currency_separately(self) -> None:
        row = next(item for item in self.wire["rows"]
                   if item["form_serial_number"] == "DA20260415E00531")
        self.assertEqual(row["average_price"], "503.0000")
        self.assertEqual(row["average_price_currency"], "HKD")

    def test_before_and_after_come_from_the_notices_own_form(self) -> None:
        # The list has only the after. Before is what makes a disposal
        # distinguishable from a re-papering.
        row = next(item for item in self.wire["rows"]
                   if item["form_serial_number"] == "DA20260415E00531")
        self.assertTrue(row["detail_read"])
        self.assertEqual(row["shares_before"], "68,494")
        self.assertEqual(row["shares_after"], "63,494")
        self.assertEqual(row["acquired_disposed"], "D")

    def test_a_holding_that_did_not_change_is_neither_bought_nor_sold(self) -> None:
        # Code 1316 is a change in the *nature* of an interest.
        row = next(item for item in self.wire["rows"]
                   if item["form_serial_number"] == "DA20260819E00389")
        self.assertEqual(row["reason_code"], "1316")
        self.assertFalse(row["is_trade"])
        self.assertIsNone(row["acquired_disposed"])
        self.assertEqual(row["shares_before"], row["shares_after"])

    def test_a_reason_code_nobody_published_is_carried_not_guessed(self) -> None:
        row = next(item for item in self.wire["rows"]
                   if item["reason_code"] == "11031")
        self.assertIsNone(row["reason_meaning"])

    def test_a_notice_whose_form_was_not_read_says_so(self) -> None:
        unread = [item for item in self.wire["rows"] if not item["detail_read"]]
        self.assertTrue(unread)
        self.assertIsNone(unread[0]["shares_before"])
        self.assertEqual(self.wire["detail_read_count"], 7)

    def test_the_capacity_code_decides_direct_or_indirect(self) -> None:
        # 2101 is a beneficial owner and is the only capacity that means the
        # person holds the shares themselves.
        row = next(item for item in self.wire["rows"] if item["detail_read"])
        self.assertEqual(row["capacity_codes"], ["2101"])
        self.assertEqual(row["direct_or_indirect"], "D")

    def test_what_the_payload_has_no_field_for_is_hashed_not_dropped(self) -> None:
        row = next(item for item in self.wire["rows"] if item["detail_read"])
        self.assertEqual(len(row["notes_text_hash"]), 64)
        self.assertIn("capacity_meanings=", row["notes_text"])
        self.assertIn("Beneficial owner", row["notes_text"])
        self.assertIn("short_position", row["notes_text"])

    def test_the_form_names_which_kind_of_person_filed(self) -> None:
        row = next(item for item in self.wire["rows"] if item["detail_read"])
        self.assertEqual(row["form_type"], "Form 3A")
        self.assertTrue(row["is_director_notice"])

    def test_the_detail_reader_finds_the_issued_share_count(self) -> None:
        detail = di_form_detail(
            document(self.capture, "di_form:DA20260819E00389")["text"]
        )
        self.assertEqual(detail["issued_shares_in_class"], "9,103,122,790")
        self.assertEqual(detail["class_of_shares"], "Ordinary Shares")

    def test_the_di_wire_satisfies_the_frozen_contract(self) -> None:
        from dalton_core.authority_resolver import _schema_matches

        _schema_matches(self.wire, hkex_output_schema(DISCLOSURE_OF_INTERESTS_OPERATION),
                        "output")
        self.assertEqual(self.wire["row_count"], 21)


class DerivedContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.wire = parse_capture(NEXT_DAY_DISCLOSURE_OPERATION,
                                  capture("next-day-disclosure-00700-20260909.json"),
                                  ticker="00700", artifact_hash="a" * 64)
        self.row = self.wire["rows"][0]

    def test_the_average_price_is_the_frozen_quotient(self) -> None:
        # 100,442,863.00 / 230,000.
        self.assertEqual(average_price_paid(self.row), "436.708100")

    def test_it_is_derived_and_not_claimed_as_disclosed(self) -> None:
        # The report gives the highest and the lowest; the average is
        # arithmetic and the event payload says so by leaving it null.
        events = buyback_events(self.wire, company_ref=COMPANY,
                                invocation_ref="i", artifact_hash="a" * 64)
        self.assertIsNone(events[0]["payload"]["average_price_paid"])
        self.assertEqual(events[0]["context"]["average_price_paid"], "436.708100")

    def test_the_price_comparison_is_unavailable_with_its_reason(self) -> None:
        context = buyback_context(self.row)
        self.assertEqual(context["price_vs_current"]["status"], "unavailable")
        self.assertIn("No current HKD price", context["price_vs_current"]["reason"])

    def test_a_handed_in_price_is_compared_with_the_formula_beside_it(self) -> None:
        context = buyback_context(self.row, current_price="400.00")
        self.assertEqual(context["price_vs_current"]["status"], "compared")
        self.assertEqual(context["price_vs_current"]["premium_to_current"],
                         "0.091770")
        self.assertIn("formula", context["price_vs_current"])

    def test_the_first_day_a_core_sees_has_no_pace(self) -> None:
        pace = buyback_pace(self.row)
        self.assertEqual(pace["status"], "unavailable")
        self.assertEqual(pace["window_days"], 0)

    def test_the_pace_is_this_day_against_the_days_before_it(self) -> None:
        prior = [
            {"trading_date": "2026-09-01", "shares_repurchased": "220,000"},
            {"trading_date": "2026-09-02", "shares_repurchased": "240,000"},
            {"trading_date": "2026-09-03", "shares_repurchased": "230,000"},
        ]
        pace = buyback_pace(self.row, prior_rows=prior)
        self.assertEqual(pace["mean_daily_shares"], "230000")
        self.assertEqual(pace["vs_mean"], "1.0000")

    def test_the_same_day_is_never_its_own_baseline(self) -> None:
        pace = buyback_pace(self.row, prior_rows=[self.row])
        self.assertEqual(pace["status"], "unavailable")

    def test_the_week_is_the_unit_a_reader_reasons_in(self) -> None:
        # Twenty daily events that each say "bought some more" is not twenty
        # pieces of news.
        self.assertEqual(buyback_cluster_key(self.row), "2026-W37")
        self.assertIsNone(buyback_cluster_key({"trading_date": None}))

    def test_the_cumulative_pair_names_its_own_basis(self) -> None:
        context = buyback_context(self.row)
        self.assertIn("repurchase mandate", context["cumulative"]["basis"])
        self.assertEqual(context["cumulative"]["shares"], "44,382,700")
        self.assertEqual(context["cumulative"]["pct_of_issued"], "0.48676")

    def test_the_context_is_a_function_of_its_inputs(self) -> None:
        self.assertEqual(buyback_context(self.row), buyback_context(self.row))

    def test_a_directors_trade_is_sized_against_what_they_hold(self) -> None:
        wire = parse_capture(DISCLOSURE_OF_INTERESTS_OPERATION,
                             capture("disclosure-of-interests-00700.json"),
                             ticker="00700", artifact_hash="b" * 64)
        row = next(item for item in wire["rows"]
                   if item["form_serial_number"] == "DA20260415E00531")
        context = di_context(row, prior_rows=wire["rows"])
        self.assertEqual(context["size_vs_holdings"]["share_of_prior_holding"],
                         "0.072999")
        self.assertEqual(context["trailing_90d"]["status"], "computed")
        self.assertGreater(context["trailing_90d"]["notice_count"], 1)
        self.assertIn("Yang Siu Shun", context["trailing_90d"]["people"])


class EventTests(unittest.TestCase):
    def test_a_buyback_becomes_the_coordinated_payload(self) -> None:
        from dalton_core.research_event import PAYLOAD_FIELDS, validate_payload

        wire = parse_capture(NEXT_DAY_DISCLOSURE_OPERATION,
                             capture("next-day-disclosure-00700-20260909.json"),
                             ticker="00700", artifact_hash="a" * 64)
        events = buyback_events(wire, company_ref=COMPANY, invocation_ref="i",
                                artifact_hash="a" * 64)
        self.assertEqual(len(events), 1)
        payload = events[0]["payload"]
        self.assertEqual(set(payload), set(PAYLOAD_FIELDS["buyback_disclosure"]))
        validate_payload("buyback_disclosure", payload)
        self.assertEqual(payload["market"], "HK")
        self.assertEqual(payload["shares_purchased"], "230,000")
        self.assertEqual(payload["currency"], "HKD")
        self.assertEqual(payload["period_start"], payload["period_end"])
        self.assertEqual(payload["filing_date"], "2026-09-09")

    def test_hong_kong_cumulative_shares_keep_the_since_mandate_basis(self) -> None:
        # The Exchange's cumulative column is since the *repurchase mandate*.
        # A figure filed under a field whose name says "year to date" would be
        # read as a calendar year by everything downstream.
        wire = parse_capture(NEXT_DAY_DISCLOSURE_OPERATION,
                             capture("next-day-disclosure-00700-20260909.json"),
                             ticker="00700", artifact_hash="a" * 64)
        payload = buyback_events(wire, company_ref=COMPANY, invocation_ref="i",
                                 artifact_hash="a" * 64)[0]["payload"]
        self.assertEqual(payload["cumulative_shares"], "44,382,700")
        self.assertEqual(payload["cumulative_basis"], "since_mandate")
        self.assertEqual(payload["cluster_key"], "2026-W37:2026-09-09")
        self.assertIsNone(payload["remaining_authorisation"])
        self.assertEqual(payload["pct_of_issued"], "0.48676")

    def test_the_event_key_follows_the_row_not_the_run(self) -> None:
        wire = parse_capture(NEXT_DAY_DISCLOSURE_OPERATION,
                             capture("next-day-disclosure-00700-20260909.json"),
                             ticker="00700", artifact_hash="a" * 64)
        first = buyback_events(wire, company_ref=COMPANY, invocation_ref="i",
                               artifact_hash="a" * 64)[0]["payload"]["event_key"]
        second = buyback_events(wire, company_ref=COMPANY, invocation_ref="j",
                                artifact_hash="c" * 64)[0]["payload"]["event_key"]
        self.assertEqual(first, second)

    def test_a_director_files_a_transaction_and_a_holder_files_a_change(self) -> None:
        from dalton_core.research_event import PAYLOAD_FIELDS, validate_payload

        wire = parse_capture(DISCLOSURE_OF_INTERESTS_OPERATION,
                             capture("disclosure-of-interests-00700.json"),
                             ticker="00700", artifact_hash="b" * 64)
        events = di_events(wire, company_ref=COMPANY, invocation_ref="i",
                           artifact_hash="b" * 64)
        kinds = {event["kind"] for event in events}
        self.assertIn("insider_transaction", kinds)
        for event in events:
            self.assertEqual(
                set(event["payload"]) - set(PAYLOAD_FIELDS[event["kind"]]), set()
            )
            validate_payload(event["kind"], event["payload"])

    def test_a_hong_kong_notice_has_no_cik_and_says_so(self) -> None:
        wire = parse_capture(DISCLOSURE_OF_INTERESTS_OPERATION,
                             capture("disclosure-of-interests-00700.json"),
                             ticker="00700", artifact_hash="b" * 64)
        event = next(item for item in di_events(
            wire, company_ref=COMPANY, invocation_ref="i", artifact_hash="b" * 64)
            if item["kind"] == "insider_transaction")
        self.assertIsNone(event["payload"]["owner_cik"])
        self.assertEqual(event["payload"]["role"], "director or chief executive")
        self.assertEqual(event["payload"]["issuer_name"], "Tencent Holdings Ltd.")

    def test_the_notes_hash_travels_on_the_event(self) -> None:
        wire = parse_capture(DISCLOSURE_OF_INTERESTS_OPERATION,
                             capture("disclosure-of-interests-00700.json"),
                             ticker="00700", artifact_hash="b" * 64)
        event = next(item for item in di_events(
            wire, company_ref=COMPANY, invocation_ref="i", artifact_hash="b" * 64)
            if item["payload"].get("notes_text_hash"))
        row = next(item for item in wire["rows"]
                   if item["form_serial_number"] == event["payload"]["accession"])
        self.assertEqual(event["payload"]["notes_text_hash"], row["notes_text_hash"])

    def test_the_index_operations_emit_nothing(self) -> None:
        # They tell a filings index that a document exists; they do not claim
        # to have read one.
        from dalton_core.hkex_filings_adapter import PARSERS

        self.assertEqual(sorted(PARSERS), [
            "announcements_index", "disclosure_of_interests", "monthly_returns",
            "next_day_disclosure_returns",
        ])

    def test_an_operation_this_connector_does_not_have_is_refused(self) -> None:
        with self.assertRaises(HkexFilingsError):
            parse_capture("short_positions", {}, ticker="00700",
                          artifact_hash="a" * 64)


class CrossOperationTests(unittest.TestCase):
    def test_a_capture_cannot_be_read_as_another_operation(self) -> None:
        with self.assertRaises(HkexFilingsParseError):
            parse_capture(NEXT_DAY_DISCLOSURE_OPERATION,
                          capture("announcements-index-00700.json"),
                          ticker="00700", artifact_hash="a" * 64)
        with self.assertRaises(HkexFilingsParseError):
            parse_capture(DISCLOSURE_OF_INTERESTS_OPERATION,
                          capture("monthly-returns-00700.json"),
                          ticker="00700", artifact_hash="a" * 64)

    def test_no_test_here_reached_the_network(self) -> None:
        import sys

        self.assertNotIn("requests", sys.modules)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

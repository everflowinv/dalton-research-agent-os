"""S4: turning six real akshare captures into six frozen wires.

Every fixture here is a genuine call made once, read-only, on 2026-09-09:
贵州茅台 (600519) for the A-share side and 腾讯控股 (00700) for Hong Kong, plus
the market-wide tables the vendor offers no per-issuer route into. Two of them
are truncated for repository size and say so in their own ``capture_note``;
nothing about their shape is synthetic.

What is under test is the part that decides what a number means: that a figure
is text and never a float, that a row cannot arrive without naming the vendor
that produced it, that a vendor nobody approved is a refusal rather than a
substitution, and that the two places where the sources disagree about units
are labelled on every row instead of in a comment somewhere.
"""

from __future__ import annotations

import json
import math
import unittest
from decimal import Decimal
from pathlib import Path

from dalton_core.authority_resolver import _schema_matches
from dalton_core.cn_hk_findata_adapter import (
    CnHkFinDataAdapterError,
    CnHkFinDataVendorRefusal,
    WIRE_BUILDERS,
    a_share_symbol,
    cell_text,
    frame_to_raw,
    hk_symbol,
)
from dalton_core.cn_hk_findata_core import OPERATIONS, cn_hk_findata_output_schema
from dalton_core.store import canonical_json, content_hash

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cn-hk-findata"
REFS = ["raw-sink:" + "0" * 64]


def capture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def wire(name: str, raw: dict | None = None) -> dict:
    raw = raw if raw is not None else capture(name)
    return WIRE_BUILDERS[raw["operation"]](raw, source_record_refs=REFS)


class Frame:
    """The smallest thing that behaves like the frames akshare returns."""

    def __init__(self, columns, records):
        self.columns = columns
        self._records = records

    def to_dict(self, orient):
        assert orient == "records"
        return [dict(record) for record in self._records]


class CanonicalisationTests(unittest.TestCase):
    def test_a_float_becomes_the_decimal_that_printed(self):
        self.assertEqual(cell_text(1.5), "1.5")
        self.assertEqual(cell_text(0.0), "0")
        # akshare divides some vendor amounts by 10,000 and hands back the
        # binary residue. It is kept as the decimal that round-trips rather
        # than rounded, because rounding invents a number nobody published.
        self.assertEqual(cell_text(28.167578000000002), "28.167578000000002")

    def test_a_large_number_does_not_arrive_in_exponent_form(self):
        self.assertEqual(cell_text(1e15), "1000000000000000")

    def test_absence_stays_absent(self):
        for value in (None, float("nan"), "", "  ", "--", "NaT"):
            self.assertIsNone(cell_text(value), repr(value))
        self.assertIsNone(cell_text(math.inf))

    def test_the_same_frame_twice_is_the_same_hash(self):
        frame = Frame(["a", "b"], [{"a": 1.25, "b": "x"}, {"a": None, "b": "y"}])
        first = frame_to_raw(frame, function="f", kwargs={"n": 1})
        second = frame_to_raw(frame, function="f", kwargs={"n": 1})
        self.assertEqual(
            content_hash(json.loads(canonical_json(first))),
            content_hash(json.loads(canonical_json(second))),
        )

    def test_column_order_is_the_library_s_and_not_sorted(self):
        frame = Frame(["z", "a"], [{"z": 1, "a": 2}])
        self.assertEqual(frame_to_raw(frame, function="f", kwargs={})["columns"],
                         ["z", "a"])

    def test_a_frame_with_two_columns_of_one_name_is_refused(self):
        # Choosing which of them survives would be arbitrary, and the wrong
        # choice is a column of somebody else's numbers.
        frame = Frame(["a", "a"], [{"a": 1}])
        with self.assertRaises(CnHkFinDataAdapterError):
            frame_to_raw(frame, function="f", kwargs={})

    def test_no_float_survives_into_a_canonical_frame(self):
        raw = capture("northbound-flow")
        for row in raw["frames"]["flow"]["rows"]:
            for value in row.values():
                self.assertIsInstance(value, (str, type(None)))

    def test_a_ticker_without_a_market_prefix_is_refused(self):
        self.assertEqual(a_share_symbol("600519"), "SH600519")
        self.assertEqual(a_share_symbol("000001"), "SZ000001")
        self.assertEqual(hk_symbol("700"), "00700")
        for bad in ("60051", "ABCDEF", "700519"):
            with self.assertRaises(CnHkFinDataAdapterError):
                a_share_symbol(bad)


class EveryOperationTests(unittest.TestCase):
    NAMES = {
        "financial_statements": ["financial-statements-a-600519-income",
                                 "financial-statements-hk-00700-income"],
        "shareholders": ["shareholders-600519"],
        "buybacks": ["buybacks-600519"],
        "margin_balance": ["margin-balance-sse", "margin-balance-szse"],
        "northbound_flow": ["northbound-flow"],
        "ah_premium": ["ah-premium"],
    }

    def test_there_is_a_real_capture_for_all_six(self):
        self.assertEqual(set(self.NAMES), set(OPERATIONS))
        for names in self.NAMES.values():
            for name in names:
                self.assertTrue((FIXTURES / f"{name}.json").exists(), name)

    def rows(self, value):
        rows = value.get("lines")
        if rows is None:
            rows = value.get("rows")
        if rows is None:
            rows = list(value["top_holders"]) + list(value["holder_counts"])
        return rows

    def test_every_wire_satisfies_its_own_frozen_contract(self):
        for operation, names in self.NAMES.items():
            for name in names:
                built = wire(name)
                _schema_matches(
                    built, cn_hk_findata_output_schema(operation), "output")
                self.assertTrue(self.rows(built), name)

    def test_every_row_names_its_vendor_and_is_not_a_fallback(self):
        for names in self.NAMES.values():
            for name in names:
                for row in self.rows(wire(name)):
                    self.assertIn("source_vendor", row)
                    self.assertFalse(row["fallback_used"])

    def test_no_figure_anywhere_is_a_float(self):
        for names in self.NAMES.values():
            for name in names:
                for row in self.rows(wire(name)):
                    for key, value in row.items():
                        self.assertNotIsInstance(value, float, f"{name}.{key}")

    def test_building_a_wire_twice_gives_the_same_bytes(self):
        for names in self.NAMES.values():
            for name in names:
                raw = capture(name)
                first = canonical_json(wire(name, raw))
                second = canonical_json(wire(name, raw))
                self.assertEqual(first, second, name)

    def test_a_capture_of_one_operation_cannot_be_read_as_another(self):
        raw = capture("northbound-flow")
        with self.assertRaises(CnHkFinDataAdapterError):
            WIRE_BUILDERS["ah_premium"](raw, source_record_refs=REFS)


class FallbackLabellingTests(unittest.TestCase):
    def test_a_capture_from_an_unapproved_vendor_is_refused_by_name(self):
        raw = capture("northbound-flow")
        raw["vendor"] = "ths"
        with self.assertRaises(CnHkFinDataVendorRefusal) as caught:
            wire("northbound-flow", raw)
        self.assertIn("ths", str(caught.exception))
        self.assertIn("eastmoney", str(caught.exception))

    def test_a_capture_that_claims_a_fallback_is_refused_not_relabelled(self):
        # The whole rule in one test: when the declared vendor did not answer
        # there is no answer, not a different answer.
        raw = capture("margin-balance-sse")
        raw["fallback_used"] = True
        with self.assertRaises(CnHkFinDataVendorRefusal) as caught:
            wire("margin-balance-sse", raw)
        self.assertIn("no fallback vendor is approved", str(caught.exception))

    def test_the_refused_tencent_route_explains_itself(self):
        raw = capture("ah-premium")
        raw["vendor"] = "tencent"
        with self.assertRaises(CnHkFinDataVendorRefusal) as caught:
            wire("ah-premium", raw)
        self.assertIn("没有比价与溢价", str(caught.exception))

    def test_a_row_from_a_vendor_the_schema_does_not_list_cannot_validate(self):
        built = wire("ah-premium")
        built["rows"][0]["source_vendor"] = "tencent"
        with self.assertRaises(Exception):
            _schema_matches(
                built, cn_hk_findata_output_schema("ah_premium"), "output")

    def test_the_two_exchanges_are_labelled_in_different_units(self):
        # Shanghai returned 1,350,016,680,402 and Shenzhen 12,847.58 for the
        # same quantity two days apart. Adding them is off by eight orders,
        # and this is the only thing on the row that says so.
        sse = wire("margin-balance-sse")["rows"][-1]
        szse = wire("margin-balance-szse")["rows"][0]
        self.assertEqual((sse["amount_unit"], sse["volume_unit"]), ("元", "股"))
        self.assertEqual((szse["amount_unit"], szse["volume_unit"]), ("亿元", "亿股"))
        self.assertGreater(Decimal(sse["total_balance"]),
                           Decimal(szse["total_balance"]) * 10_000_000)
        for row in (sse, szse):
            self.assertIn("不可直接相加", row["caliber_note"])

    def test_the_hong_kong_statement_admits_it_has_no_currency(self):
        # The main-indicator table on the same host has a CURRENCY of HKD and
        # a revenue 1.09% away from the statement's. Borrowing that label would
        # put a wrong currency on a right number.
        built = wire("financial-statements-hk-00700-income")
        self.assertIsNone(built["currency"])
        self.assertIsNone(built["account_standard"])
        self.assertIn("不可借用", built["lines"][0]["caliber_note"])

    def test_the_a_share_statement_names_its_standard_and_currency(self):
        built = wire("financial-statements-a-600519-income")
        self.assertEqual(built["currency"], "CNY")
        self.assertEqual(built["account_standard"], "中国企业会计准则")
        for line in built["lines"]:
            self.assertEqual(line["account_standard"], "中国企业会计准则")


class ShapeTests(unittest.TestCase):
    def test_the_a_share_income_lines_are_cumulative_and_say_so(self):
        built = wire("financial-statements-a-600519-income")
        line = next(item for item in built["lines"]
                    if item["concept"] == "TOTAL_OPERATE_INCOME")
        self.assertEqual(line["period_end"], "2026-06-30")
        self.assertEqual(line["period_start"], "2026-01-01")
        self.assertEqual(line["value"], "92278072083.21")

    def test_a_vendor_computed_growth_column_is_not_a_statement_line(self):
        built = wire("financial-statements-a-600519-income")
        concepts = {line["concept"] for line in built["lines"]}
        self.assertIn("TOTAL_OPERATE_INCOME", concepts)
        self.assertNotIn("TOTAL_OPERATE_INCOME_YOY", concepts)

    def test_periods_beyond_the_ceiling_are_counted_rather_than_lost(self):
        built = wire("financial-statements-a-600519-income")
        self.assertEqual(built["period_count"], 20)
        self.assertEqual(built["dropped_row_count"], 4)

    def test_the_hong_kong_fiscal_year_is_a_year(self):
        built = wire("financial-statements-hk-00700-income")
        line = next(item for item in built["lines"] if item["label"] == "营业额")
        self.assertEqual(line["fiscal_year"], "2025")
        self.assertEqual(line["value"], "743689000000")
        # The vendor publishes no legend for its own report-type code, so the
        # code travels as a code rather than as a label nobody checked.
        self.assertTrue(line["report_type"].startswith("date_type_code:"))

    def test_the_top_holders_keep_the_vendor_s_words_for_a_change(self):
        built = wire("shareholders-600519")
        first = built["top_holders"][0]
        self.assertEqual(first["rank"], 1)
        self.assertEqual(first["change"], "不变")
        self.assertEqual(first["shares"], "681282935")

    def test_the_holder_count_history_arrives_newest_first(self):
        counts = wire("shareholders-600519")["holder_counts"]
        self.assertEqual(counts, sorted(counts, key=lambda r: r["as_of"],
                                        reverse=True))

    def test_a_buyback_answer_says_how_big_the_table_it_came_from_was(self):
        built = wire("buybacks-600519")
        self.assertEqual({row["security_code"] for row in built["rows"]}, {"600519"})
        self.assertGreater(built["universe_row_count"], len(built["rows"]))

    def test_a_company_with_no_buyback_is_empty_rather_than_missing(self):
        raw = capture("buybacks-600519")
        raw["parameters"]["a_ticker"] = "000001"
        built = wire("buybacks-600519", raw)
        self.assertEqual(built["rows"], [])
        # The distinction the empty answer rests on: the table was read and it
        # had rows in it, so "no buyback" is a finding and not a failed call.
        self.assertGreater(built["universe_row_count"], 0)
        _schema_matches(built, cn_hk_findata_output_schema("buybacks"), "output")

    def test_a_pair_that_is_not_dual_listed_is_empty_rather_than_wrong(self):
        raw = capture("ah-premium")
        raw["parameters"]["ticker"] = "00700"
        built = wire("ah-premium", raw)
        self.assertEqual(built["rows"], [])
        self.assertGreater(built["universe_row_count"], 100)

    def test_the_ah_premium_is_the_vendor_s_own_number(self):
        row = wire("ah-premium")["rows"][0]
        self.assertEqual((row["h_code"], row["a_code"]), ("01398", "601398"))
        self.assertEqual((row["h_currency"], row["a_currency"]), ("HKD", "CNY"))
        self.assertEqual(row["premium_percent"], "23.95")

    def test_a_stale_northbound_snapshot_is_refused(self):
        # The endpoint takes no date and answers with whatever day it holds.
        # Filing that under the day that was asked for would date a number to
        # a session it does not describe.
        raw = capture("northbound-flow")
        raw["parameters"]["as_of"] = "2026-09-08"
        with self.assertRaises(CnHkFinDataAdapterError) as caught:
            wire("northbound-flow", raw)
        self.assertIn("2026-09-09", str(caught.exception))

    def test_the_northbound_amounts_carry_the_library_s_unit(self):
        for row in wire("northbound-flow")["rows"]:
            self.assertEqual(row["amount_unit"], "亿元")
            self.assertIn("10,000", row["caliber_note"])

    def test_shenzhen_dates_its_row_from_the_request_it_answered(self):
        # The Shenzhen response has no date column at all. The date is the
        # request, and the row says which request it was.
        built = wire("margin-balance-szse")
        self.assertEqual(built["rows"][0]["trade_date"], built["requested_start"])

    def test_a_row_the_contract_cannot_describe_never_reaches_a_wire(self):
        raw = capture("margin-balance-sse")
        raw["frames"]["margin"]["rows"][0]["信用交易日期"] = "not a date"
        built = wire("margin-balance-sse", raw)
        self.assertEqual(built["dropped_row_count"], 1)
        _schema_matches(built, cn_hk_findata_output_schema("margin_balance"),
                        "output")

    def test_a_capture_that_was_not_canonicalised_is_refused(self):
        raw = capture("margin-balance-sse")
        raw["frames"]["margin"]["rows"][0]["融资余额"] = 1.5
        with self.assertRaises(CnHkFinDataAdapterError) as caught:
            wire("margin-balance-sse", raw)
        self.assertIn("canonicalised", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

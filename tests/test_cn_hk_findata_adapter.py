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

import contextlib
import json
import math
import sys
import types
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from dalton_core.authority_resolver import _schema_matches
from dalton_core.cn_hk_findata_adapter import (
    AH_PREMIUM_MAX_PAGES,
    AH_PREMIUM_MAX_RETRIES,
    CnHkFinDataAdapterError,
    CnHkFinDataVendorRefusal,
    MAX_PLAUSIBLE_SZSE_YI_YUAN,
    MIN_PLAUSIBLE_SSE_YUAN,
    WIRE_BUILDERS,
    a_share_symbol,
    cell_text,
    fetch_ah_premium,
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


class NotATime(datetime):
    """What ``pandas.NaT`` is: a datetime that is not equal to itself."""

    def __eq__(self, other):
        return False

    def __ne__(self, other):
        return True

    def __hash__(self):
        return 0


@contextlib.contextmanager
def fake_akshare(spot):
    """A library-shaped stand-in, so the retry cap can be tested offline.

    The real akshare is an optional extra and is not installed for the suite.
    What is under test is not the library but what this adapter does to it
    before it is allowed near 东财's quote cluster, and that is visible from a
    module tree with the same three names in it.
    """

    def request_with_retry(url, params=None, timeout=15, max_retries=3):
        raise AssertionError("no test may actually request anything")

    func = types.ModuleType("akshare.utils.func")
    func.request_with_retry = request_with_retry
    utils = types.ModuleType("akshare.utils")
    utils.func = func
    akshare = types.ModuleType("akshare")
    akshare.utils = utils
    akshare.__version__ = "1.18.94"
    akshare.stock_zh_ah_spot_em = spot
    replaced = {"akshare": akshare, "akshare.utils": utils,
                "akshare.utils.func": func}
    saved = {name: sys.modules.get(name) for name in replaced}
    sys.modules.update(replaced)
    try:
        yield func, request_with_retry
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


class QuoteClusterRetryTests(unittest.TestCase):
    """The one operation that has to touch the host that went quiet in August.

    ``stock_zh_ah_spot_em`` issues no request of its own: it calls
    ``fetch_paginated_data``, which calls ``request_with_retry``, which
    defaults to three attempts with exponential backoff. Three pages of one
    hundred over a 204-row universe is therefore up to nine GETs against a
    refusing host -- nine, sleeping between each, which is the shape of the
    2026-08-21 incident and nine times the approved ceiling of three.
    """

    def spot(self, seen, raise_with=None):
        def stock_zh_ah_spot_em():
            module = sys.modules["akshare.utils.func"]
            seen.append(getattr(module.request_with_retry, "keywords", None))
            if raise_with is not None:
                raise raise_with
            return Frame(["名称", "H股代码", "A股代码"],
                         [{"名称": "工商银行", "H股代码": "01398",
                           "A股代码": "601398"}])
        return stock_zh_ah_spot_em

    def test_the_library_retry_loop_is_capped_for_the_length_of_the_call(self):
        seen = []
        with fake_akshare(self.spot(seen)) as (module, original):
            fetch_ah_premium(ticker="01398")
            self.assertEqual(seen, [{"max_retries": AH_PREMIUM_MAX_RETRIES}])
            self.assertEqual(AH_PREMIUM_MAX_RETRIES, 1)
            # Restored, so the cap cannot leak into any other akshare call
            # that happens to run in the same process.
            self.assertIs(module.request_with_retry, original)

    def test_the_cap_is_lifted_even_when_the_call_fails(self):
        seen = []
        with fake_akshare(self.spot(seen, raise_with=OSError("refused"))) as (
                module, original):
            with self.assertRaises(CnHkFinDataVendorRefusal):
                fetch_ah_premium(ticker="01398")
            self.assertIs(module.request_with_retry, original)

    def test_a_library_that_cannot_be_capped_is_refused_before_the_call(self):
        # Refusing is the safe direction. A silent miss here would reach the
        # one host that has already demonstrated what happens when it is
        # pressed, without the cap the approval rests on.
        seen = []
        with fake_akshare(self.spot(seen)) as (module, _):
            del module.request_with_retry
            with self.assertRaises(CnHkFinDataAdapterError) as caught:
                fetch_ah_premium(ticker="01398")
        self.assertIn("uncapped", str(caught.exception))
        self.assertEqual(seen, [], "the call was made anyway")

    def test_the_page_count_matches_the_universe_the_capture_saw(self):
        # 204 rows at a hundred a page is three pages, one attempt each, and
        # the quota's per-unit ceiling is that number rather than a guess.
        rows = capture("ah-premium")["frames"]["ah"]["row_count"]
        self.assertEqual(-(-rows // 100), AH_PREMIUM_MAX_PAGES)


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

    def test_a_missing_timestamp_does_not_become_the_word_nat(self):
        # ``pandas.NaT`` is a subclass of ``datetime``, so a type check reaches
        # the date branch and ``isoformat()`` hands back the literal string
        # "NaT" -- an absent 公告日期 stored as though the vendor had reported
        # one, and one that no date parser downstream would reject as missing.
        self.assertIsNone(cell_text(NotATime(2026, 9, 9)))

    def test_the_real_missing_timestamp_behaves_like_the_stand_in(self):
        pandas = None
        with contextlib.suppress(ImportError):
            import pandas  # noqa: PLC0415 - optional, only present with an extra
        if pandas is None:  # pragma: no cover - depends on the extra
            self.skipTest("pandas comes with an optional extra")
        self.assertIsInstance(pandas.NaT, datetime)
        self.assertIsNone(cell_text(pandas.NaT))

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

    def test_a_shanghai_total_too_small_for_yuan_is_refused(self):
        # The unit labels were read off the magnitudes rather than published,
        # so they have to notice when they stop being true. A Shanghai total
        # that arrives in 亿元 would be about 13,500 -- it would validate, and
        # every figure on the row would be out by eight orders.
        raw = capture("margin-balance-sse")
        for row in raw["frames"]["margin"]["rows"]:
            row["融资融券余额"] = "13500.16"
        with self.assertRaises(CnHkFinDataAdapterError) as caught:
            wire("margin-balance-sse", raw)
        self.assertIn(str(MIN_PLAUSIBLE_SSE_YUAN), str(caught.exception))

    def test_a_shenzhen_total_too_large_for_yi_yuan_is_refused(self):
        raw = capture("margin-balance-szse")
        for row in raw["frames"]["margin"]["rows"]:
            row["融资融券余额"] = "1284758000000"
        with self.assertRaises(CnHkFinDataAdapterError) as caught:
            wire("margin-balance-szse", raw)
        self.assertIn(str(MAX_PLAUSIBLE_SZSE_YI_YUAN), str(caught.exception))

    def test_the_bounds_are_far_enough_away_to_pass_the_real_captures(self):
        sse = wire("margin-balance-sse")["rows"]
        szse = wire("margin-balance-szse")["rows"]
        for row in sse:
            self.assertGreater(Decimal(row["total_balance"]),
                               MIN_PLAUSIBLE_SSE_YUAN * 1_000_000)
        for row in szse:
            self.assertLess(Decimal(row["total_balance"]),
                            MAX_PLAUSIBLE_SZSE_YI_YUAN / 1_000)

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

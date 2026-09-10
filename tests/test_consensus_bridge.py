"""P13-M3: the bridge to the street, and the things it will not invent.

The consensus authority is P11b and is not on this branch, so every test here
installs a fake ``dalton_core.consensus_estimate`` for the duration and takes
it away again. That is the point of the design being a name lookup rather than
an import: the bridge has to work the day that module lands and has to say so
honestly until then, and both halves are testable today.

The load-bearing assertion is the last one in ``ShapeTests``: whatever this
module builds is fed through **P15d's own validator**, not a copy of it. Two
validators that agree today disagree in six months, and the disagreement would
surface as a refused conviction call after two model calls had been paid for.
"""

from __future__ import annotations

import importlib
import inspect
import sys
import types
import unittest
from decimal import Decimal

from dalton_core import consensus_bridge as cb
from dalton_core.conviction_call import (
    ConvictionCallValidationError,
    validate_consensus_gap,
)
from dalton_core.conviction_call_cli import consensus_gap
from dalton_core.forecast_sensitivity import build_projection
from tests.test_forecast_sensitivity import COST_DRIVER, model

MODULE = "dalton_core.consensus_estimate"


class FakeStore:
    """Enough of a store for a reader that never touches it."""

    connection = None


def install(**functions):
    """Put a fake consensus authority on the module path, and take it away."""

    module = types.ModuleType(MODULE)
    for name, value in functions.items():
        setattr(module, name, value)
    sys.modules[MODULE] = module
    import dalton_core

    setattr(dalton_core, "consensus_estimate", module)
    return module


def uninstall():
    """Put the real P11b module back, whatever a test swapped in."""

    sys.modules.pop(MODULE, None)
    import dalton_core

    if hasattr(dalton_core, "consensus_estimate"):
        delattr(dalton_core, "consensus_estimate")
    importlib.import_module(MODULE)


class NoModule:
    """A Core that carries no consensus authority at all.

    P11b is in the tree now, so "absent" cannot be staged by emptying
    ``sys.modules`` -- the import would simply succeed again. It is staged
    where the absence is actually decided, which is the resolver.
    """

    def __enter__(self):
        self._real = cb._module
        cb._module = lambda: None
        return self

    def __exit__(self, *exc):
        cb._module = self._real
        return False


class FakeConsensus(unittest.TestCase):
    def tearDown(self):
        uninstall()


def revenue_periods(record):
    return [str(item["end"]) for item in record["forecast_periods"]]


class ReadingTests(FakeConsensus):
    def test_no_module_is_an_honest_unavailable_rather_than_a_crash(self):
        with NoModule():
            found = cb.read_consensus(FakeStore(), "company:x")
        self.assertEqual(found["status"], "unavailable")
        self.assertIn("no consensus authority module", found["reason"])
        self.assertIsNone(found["payload"])

    def test_a_module_with_no_reader_says_which_reader_is_missing(self):
        install(something_else=lambda *a: None)
        found = cb.read_consensus(FakeStore(), "company:x")
        self.assertEqual(found["status"], "unavailable")
        self.assertIn("latest_consensus", found["reason"])

    def test_an_unreadable_authority_is_no_street_rather_than_an_exception(self):
        def boom(store, company_ref):
            raise RuntimeError("the table is gone")

        install(latest_consensus=boom)
        found = cb.read_consensus(FakeStore(), "company:x")
        self.assertEqual(found["status"], "unavailable")
        self.assertIn("the table is gone", found["reason"])

    def test_a_reader_taking_only_a_company_is_tried_too(self):
        # The blueprint names it ``latest_consensus(company)``; P15d already
        # calls it ``(store, company_ref)``. Both are tried rather than picked,
        # because guessing wrong would look exactly like "no consensus".
        install(latest_consensus=lambda company_ref: {"metrics": [], "as_of": "x"})
        found = cb.read_consensus(FakeStore(), "company:x")
        self.assertEqual(found["status"], "available")

    def test_an_empty_answer_names_the_company_it_found_nothing_for(self):
        install(latest_consensus=lambda store, company_ref: None)
        found = cb.read_consensus(FakeStore(), "company:acme")
        self.assertEqual(found["status"], "unavailable")
        self.assertIn("company:acme", found["reason"])

    def test_the_fingerprint_is_none_when_there_is_no_street(self):
        with NoModule():
            found = cb.read_consensus(FakeStore(), "company:x")
        self.assertIsNone(cb.consensus_fingerprint(found))

    def test_the_fingerprint_moves_only_when_the_street_does(self):
        payload = {"metrics": [{"metric": "revenue", "period": "2026-08-31",
                                "value": "1", "unit": "USD", "refs": ["claim:1"]}]}
        install(latest_consensus=lambda store, company_ref: payload)
        first = cb.consensus_fingerprint(cb.read_consensus(FakeStore(), "c"))
        self.assertEqual(first, cb.consensus_fingerprint(
            cb.read_consensus(FakeStore(), "c")))
        payload["metrics"][0]["value"] = "2"
        self.assertNotEqual(first, cb.consensus_fingerprint(
            cb.read_consensus(FakeStore(), "c")))


class ReportConsensusTests(FakeConsensus):
    def test_two_brokers_make_a_range_with_its_midpoint(self):
        install(latest_consensus=lambda *a: None,
                report_consensus=lambda store, company_ref: [
                    {"broker": "TD", "value": "100", "refs": ["claim:td"]},
                    {"broker": "Wolfe", "value": "120", "refs": ["claim:wolfe"]},
                ])
        found = cb.report_consensus(FakeStore(), "company:x", "target_price", "2027-06-30")
        self.assertEqual(found["status"], "available")
        self.assertEqual(found["low"], "100")
        self.assertEqual(found["high"], "120")
        self.assertEqual(found["value"], "110")
        self.assertEqual(found["brokers"], ["TD", "Wolfe"])
        self.assertEqual(found["refs"], ["claim:td", "claim:wolfe"])
        self.assertEqual(found["basis"], "report_consensus")

    def test_one_broker_is_not_consensus(self):
        install(latest_consensus=lambda *a: None,
                report_consensus=lambda store, company_ref: [
                    {"broker": "TD", "value": "100", "refs": ["claim:td"]}])
        found = cb.report_consensus(FakeStore(), "company:x", "target_price", "2027-06-30")
        self.assertEqual(found["status"], "unavailable")
        self.assertIn("1 broker", found["reason"])
        self.assertIsNone(found["value"])

    def test_two_notes_from_one_broker_are_still_one_broker(self):
        install(latest_consensus=lambda *a: None,
                report_consensus=lambda store, company_ref: [
                    {"broker": "TD", "value": "100", "refs": ["claim:td-1"]},
                    {"broker": "TD", "value": "120", "refs": ["claim:td-2"]}])
        found = cb.report_consensus(FakeStore(), "company:x", "target_price", "2027-06-30")
        self.assertEqual(found["status"], "unavailable")

    def test_two_notes_from_one_house_plus_a_valueless_second_is_one_house(self):
        # The exact shape the two counts used to let through: Alpha twice with
        # numbers, Beta once without one. Counting houses over every row and
        # numbers over the valued rows gave "two brokers, two numbers" and
        # published a range whose ends were both Alpha's, under the word
        # consensus. Only rows carrying a house *and* a number count.
        install(latest_consensus=lambda store, company_ref: None,
                report_consensus=lambda store, company_ref: [
                    {"broker": "Alpha", "value": "100", "refs": ["claim:a1"]},
                    {"broker": "Alpha", "value": "110", "refs": ["claim:a2"]},
                    {"broker": "Beta", "value": None, "refs": ["claim:b1"]},
                ])
        found = cb.report_consensus(FakeStore(), "company:x", "target_price", "2027-06-30")
        self.assertEqual(found["status"], "unavailable")
        self.assertIn("1 broker", found["reason"])
        self.assertIsNone(found["value"])

    def test_a_row_with_a_number_but_no_house_does_not_count_either(self):
        install(latest_consensus=lambda store, company_ref: None,
                report_consensus=lambda store, company_ref: [
                    {"broker": "Alpha", "value": "100", "refs": ["claim:a1"]},
                    {"broker": None, "value": "140", "refs": ["claim:anon"]},
                ])
        found = cb.report_consensus(FakeStore(), "company:x", "target_price", "2027-06-30")
        self.assertEqual(found["status"], "unavailable")
        self.assertIn("1 broker", found["reason"])

    def test_the_range_is_taken_only_over_rows_that_counted(self):
        install(latest_consensus=lambda store, company_ref: None,
                report_consensus=lambda store, company_ref: [
                    {"broker": "Alpha", "value": "100", "refs": ["claim:a1"]},
                    {"broker": "Beta", "value": "140", "refs": ["claim:b1"]},
                    {"broker": None, "value": "9999", "refs": ["claim:anon"]},
                ])
        found = cb.report_consensus(FakeStore(), "company:x", "target_price", "2027-06-30")
        self.assertEqual(found["status"], "available")
        self.assertEqual((found["low"], found["high"], found["value"]),
                         ("100", "140", "120"))
        self.assertEqual(found["brokers"], ["Alpha", "Beta"])
        self.assertNotIn("claim:anon", found["refs"])

    def test_no_broker_reader_at_all_is_a_reason_not_a_crash(self):
        install(latest_consensus=lambda *a: None)
        found = cb.report_consensus(FakeStore(), "company:x", "target_price", "2027-06-30")
        self.assertEqual(found["status"], "unavailable")
        self.assertIn("no broker-note consensus reader", found["reason"])


class SignatureTests(FakeConsensus):
    def test_a_type_error_from_inside_the_reader_is_not_read_as_an_arity_mismatch(self):
        # The reason the arity retry went: a TypeError raised *inside* a
        # working reader looks exactly like calling it with the wrong number of
        # arguments, and retrying would call it a second time with the wrong
        # ones and report that failure instead of this one.
        calls = []

        def reader(store, company_ref):
            calls.append(company_ref)
            raise TypeError("'<' not supported between str and Decimal")

        install(latest_consensus=reader)
        found = cb.read_consensus(FakeStore(), "company:x")
        self.assertEqual(found["status"], "unavailable")
        self.assertIn("not supported between", found["reason"])
        self.assertEqual(len(calls), 1)

    def test_the_signature_decides_which_way_to_call(self):
        self.assertTrue(cb._wants_store(lambda store, company_ref: None))
        self.assertFalse(cb._wants_store(lambda company_ref: None))
        # P11b's spelling, which is the one that matters.
        self.assertTrue(cb._wants_store(
            lambda store, company_ref, metric, period: None))


class BridgeTests(FakeConsensus):
    def setUp(self):
        self.record = model()
        self.periods = revenue_periods(self.record)

    def our_revenue(self, period):
        line = next(item for item in self.record["results"]
                    if item["ref"] == "result:revenue")
        cell = next(item for item in line["cells"]
                    if item["period"]["end"] == period)
        return cell["value"]

    def test_no_street_is_an_unavailable_bridge_with_the_reason_carried_through(self):
        with NoModule():
            found = cb.read_consensus(FakeStore(), "company:x")
        built = cb.build_bridge(self.record, found)
        self.assertEqual(built["bridge"]["status"], "unavailable")
        self.assertIn("no consensus authority module", built["bridge"]["reason"])
        self.assertEqual(built["detail"], [])

    def test_ours_against_theirs_per_metric_and_period(self):
        period = self.periods[0]
        ours = self.our_revenue(period)
        install(latest_consensus=lambda store, company_ref: {"metrics": [
            {"metric": "revenue", "period": period, "value": "2000000000",
             "unit": "USD", "refs": ["consensus-estimate-version:1"]}]})
        built = cb.build_bridge(self.record,
                                cb.read_consensus(FakeStore(), "company:x"))
        self.assertEqual(built["bridge"]["status"], "available")
        row = built["bridge"]["metrics"][0]
        self.assertEqual(row["metric"], "revenue")
        self.assertEqual(row["period"], period)
        self.assertEqual(row["ours"], ours)
        self.assertEqual(row["consensus"], "2000000000")
        self.assertEqual(row["unit"], "USD")
        self.assertIn(self.record["id"], row["refs"])
        self.assertIn("consensus-estimate-version:1", row["refs"])
        detail = built["detail"][0]
        self.assertEqual(detail["basis"], "vendor")
        self.assertEqual(detail["gap_abs"],
                         format(Decimal(ours) - Decimal("2000000000"), "f"))

    def test_the_gap_percent_is_ours_over_theirs(self):
        period = self.periods[0]
        ours = self.our_revenue(period)
        install(latest_consensus=lambda store, company_ref: {"metrics": [
            {"metric": "revenue", "period": period, "value": ours,
             "unit": "USD", "refs": ["consensus-estimate-version:1"]}]})
        built = cb.build_bridge(self.record,
                                cb.read_consensus(FakeStore(), "company:x"))
        self.assertEqual(built["bridge"]["metrics"][0]["gap_percent"], "0.0000")

    def test_a_zero_consensus_is_skipped_rather_than_divided_by(self):
        install(latest_consensus=lambda store, company_ref: {"metrics": [
            {"metric": "revenue", "period": self.periods[0], "value": "0",
             "unit": "USD", "refs": ["consensus-estimate-version:1"]}]})
        built = cb.build_bridge(self.record,
                                cb.read_consensus(FakeStore(), "company:x"))
        self.assertEqual(built["bridge"]["status"], "unavailable")
        self.assertIn("consensus is zero", built["bridge"]["reason"])

    def test_eps_says_why_it_cannot_be_bridged_rather_than_dividing_by_a_guess(self):
        install(latest_consensus=lambda store, company_ref: {"metrics": [
            {"metric": "eps", "period": self.periods[0], "value": "3.2",
             "unit": "USD", "refs": ["consensus-estimate-version:1"]}]})
        built = cb.build_bridge(self.record,
                                cb.read_consensus(FakeStore(), "company:x"))
        self.assertEqual(built["bridge"]["status"], "unavailable")
        self.assertIn("no diluted share count", built["bridge"]["reason"])

    def test_a_target_price_needs_a_valuation_snapshot(self):
        install(latest_consensus=lambda store, company_ref: {"metrics": [
            {"metric": "target_price", "period": "2027-06-30", "value": "400",
             "unit": "USD", "refs": ["consensus-estimate-version:1"]}]})
        without = cb.build_bridge(self.record,
                                  cb.read_consensus(FakeStore(), "company:x"))
        self.assertEqual(without["bridge"]["status"], "unavailable")
        self.assertIn("no valuation snapshot", without["bridge"]["reason"])
        with_snapshot = cb.build_bridge(
            self.record, cb.read_consensus(FakeStore(), "company:x"),
            valuation={"id": "valuation-snapshot-version:1", "currency": "USD",
                       "metrics": [{"metric": "target_price", "value": "440",
                                    "unit": "USD", "status": "computed"}]})
        self.assertEqual(with_snapshot["bridge"]["status"], "available")
        row = with_snapshot["bridge"]["metrics"][0]
        self.assertEqual(row["ours"], "440")
        self.assertEqual(row["gap_percent"], "10.0000")

    def test_a_metric_this_bridge_does_not_compare_is_skipped_by_name(self):
        install(latest_consensus=lambda store, company_ref: {"metrics": [
            {"metric": "bookings", "period": self.periods[0], "value": "1",
             "unit": "USD", "refs": ["consensus-estimate-version:1"]}]})
        built = cb.build_bridge(self.record,
                                cb.read_consensus(FakeStore(), "company:x"))
        self.assertEqual(built["bridge"]["status"], "unavailable")
        self.assertIn("bookings", built["bridge"]["reason"])

    def test_a_quarter_this_model_does_not_forecast_is_skipped_with_its_reason(self):
        install(latest_consensus=lambda store, company_ref: {"metrics": [
            {"metric": "revenue", "period": "2019-03-31", "value": "1",
             "unit": "USD", "refs": ["consensus-estimate-version:1"]}]})
        built = cb.build_bridge(self.record,
                                cb.read_consensus(FakeStore(), "company:x"))
        self.assertEqual(built["bridge"]["status"], "unavailable")
        self.assertIn("2019-03-31", built["bridge"]["reason"])

    #: What a valuation snapshot with a price target looks like.
    VALUATION = {"id": "valuation-snapshot-version:1", "currency": "USD",
                 "metrics": [{"metric": "target_price", "value": "440",
                              "unit": "USD", "status": "computed"}]}

    def test_a_target_price_row_with_no_number_falls_back_to_two_houses(self):
        # The one metric the broker-note reader can stand in for: P11b's
        # ``report_consensus`` publishes target prices and nothing else.
        install(
            latest_consensus=lambda store, company_ref: {"metrics": [
                {"metric": "target_price", "period": "2027-06-30", "value": None,
                 "unit": "USD", "refs": []}]},
            report_consensus=lambda store, company_ref: [
                {"broker": "TD", "value": "400", "refs": ["claim:td"]},
                {"broker": "Wolfe", "value": "440", "refs": ["claim:wolfe"]}])
        built = cb.build_bridge(self.record,
                                cb.read_consensus(FakeStore(), "company:x"),
                                store=FakeStore(), valuation=self.VALUATION)
        self.assertEqual(built["bridge"]["status"], "available")
        self.assertEqual(built["bridge"]["metrics"][0]["consensus"], "420")
        self.assertEqual(built["detail"][0]["basis"], "report_consensus")
        self.assertIn("claim:td", built["bridge"]["metrics"][0]["refs"])

    def test_a_target_price_row_with_no_number_and_one_house_is_not_bridged(self):
        install(
            latest_consensus=lambda store, company_ref: {"metrics": [
                {"metric": "target_price", "period": "2027-06-30", "value": None,
                 "unit": "USD", "refs": []}]},
            report_consensus=lambda store, company_ref: [
                {"broker": "TD", "value": "400", "refs": ["claim:td"]}])
        built = cb.build_bridge(self.record,
                                cb.read_consensus(FakeStore(), "company:x"),
                                store=FakeStore(), valuation=self.VALUATION)
        self.assertEqual(built["bridge"]["status"], "unavailable")
        self.assertIn("before a range may be called consensus",
                      built["bridge"]["reason"])

    def test_the_broker_reader_does_not_stand_in_for_a_metric_it_never_publishes(self):
        # A revenue row with no number is a missing revenue number. Handing
        # back a target price under the word "revenue" would be worse than the
        # gap the fallback exists to close.
        period = self.periods[0]
        install(
            latest_consensus=lambda store, company_ref: {"metrics": [
                {"metric": "revenue", "period": period, "value": None,
                 "unit": "USD", "refs": []}]},
            report_consensus=lambda store, company_ref: [
                {"broker": "TD", "value": "400", "refs": ["claim:td"]},
                {"broker": "Wolfe", "value": "440", "refs": ["claim:wolfe"]}])
        built = cb.build_bridge(self.record,
                                cb.read_consensus(FakeStore(), "company:x"),
                                store=FakeStore())
        self.assertEqual(built["bridge"]["status"], "unavailable")
        self.assertIn("target prices, not revenue", built["bridge"]["reason"])


    def test_the_rows_are_capped_at_what_the_conviction_call_accepts(self):
        install(latest_consensus=lambda store, company_ref: {"metrics": [
            {"metric": "revenue", "period": period, "value": "1000000000",
             "unit": "USD", "refs": ["consensus-estimate-version:1"]}
            for period in self.periods * 6]})
        built = cb.build_bridge(self.record,
                                cb.read_consensus(FakeStore(), "company:x"))
        self.assertLessEqual(len(built["bridge"]["metrics"]), cb.MAX_METRICS)
        self.assertEqual(len(built["detail"]), len(built["bridge"]["metrics"]))


class PassThroughTests(FakeConsensus):
    """P11b hands back the finished gap row, not the street's number alone."""

    def setUp(self):
        self.record = model()
        self.period = self.record["forecast_periods"][0]["end"]

    def rows(self):
        return [{"metric": "Revenue", "period": self.period,
                 "ours": "1000000000", "consensus": "1200000000", "unit": "USD",
                 "gap_percent": "-16.67",
                 "refs": ["consensus-estimate-version:1", self.record["id"]]}]

    def test_the_authoritys_own_gap_rows_are_used_rather_than_recomputed(self):
        # Its ``ours`` comes from ForecastModelAuthority.latest -- the same
        # driver model this projection is of -- so recomputing it here would be
        # a second implementation of one number that agrees today.
        install(latest_consensus=lambda store, company_ref: {"metrics": self.rows()})
        built = cb.build_bridge(self.record, cb.read_consensus(FakeStore(), "c"))
        self.assertEqual(built["bridge"]["status"], "available")
        row = built["bridge"]["metrics"][0]
        self.assertEqual(row["ours"], "1000000000")
        self.assertEqual(row["consensus"], "1200000000")
        self.assertEqual(row["gap_percent"], "-16.67")
        # Its metric vocabulary is its own -- P11b names the row after the
        # model's result label -- and re-filtering it against ours would
        # silently drop every row on the live Core.
        self.assertEqual(row["metric"], "Revenue")

    def test_a_passed_through_row_still_gets_its_absolute_gap(self):
        install(latest_consensus=lambda store, company_ref: {"metrics": self.rows()})
        built = cb.build_bridge(self.record, cb.read_consensus(FakeStore(), "c"))
        detail = built["detail"][0]
        self.assertEqual(detail["basis"], "consensus_authority")
        self.assertEqual(detail["gap_abs"], "-200000000")

    def test_a_passed_through_row_is_validated_not_trusted(self):
        broken = [dict(self.rows()[0], refs=[])]
        install(latest_consensus=lambda store, company_ref: {"metrics": broken})
        built = cb.build_bridge(self.record, cb.read_consensus(FakeStore(), "c"))
        self.assertEqual(built["bridge"]["status"], "unavailable")
        self.assertIn("a shape this Core cannot read", built["bridge"]["reason"])
        self.assertEqual(built["detail"], [])

    def test_the_real_p11b_readers_are_called_the_way_they_are_written(self):
        # Not a fake: the signatures this module resolves against are the ones
        # in the tree, and a mismatch would look exactly like "no consensus".
        from dalton_core import consensus_estimate

        self.assertTrue(cb._wants_store(consensus_estimate.latest_consensus))
        self.assertTrue(cb._wants_store(consensus_estimate.report_consensus))
        self.assertEqual(
            list(inspect.signature(consensus_estimate.latest_consensus).parameters),
            ["store", "company_ref"])
        self.assertEqual(
            list(inspect.signature(
                consensus_estimate.report_consensus).parameters)[:2],
            ["store", "company_ref"])


class ShapeTests(FakeConsensus):
    def test_the_validator_is_p15ds_own(self):
        # Not "agrees with"; *is*. The whole point of importing it.
        self.assertIs(cb.validate_consensus_gap, validate_consensus_gap)
        gap = {"status": "unavailable", "reason": "nothing", "metrics": []}
        self.assertEqual(cb.validate_bridge(dict(gap)),
                         validate_consensus_gap(dict(gap)))

    def test_a_built_bridge_passes_the_conviction_calls_validator_unchanged(self):
        record = model()
        period = record["forecast_periods"][0]["end"]
        install(latest_consensus=lambda store, company_ref: {"metrics": [
            {"metric": "revenue", "period": period, "value": "2000000000",
             "unit": "USD", "refs": ["consensus-estimate-version:1"]}]})
        built = cb.build_bridge(record, cb.read_consensus(FakeStore(), "c"))
        self.assertEqual(validate_consensus_gap(built["bridge"]), built["bridge"])

    def test_an_unavailable_bridge_passes_it_too(self):
        with NoModule():
            found = cb.read_consensus(FakeStore(), "c")
        built = cb.build_bridge(model(), found)
        self.assertEqual(validate_consensus_gap(built["bridge"]), built["bridge"])

    def test_the_projection_carries_a_bridge_p15d_can_read(self):
        record = model()
        period = record["forecast_periods"][0]["end"]
        install(latest_consensus=lambda store, company_ref: {"metrics": [
            {"metric": "revenue", "period": period, "value": "2000000000",
             "unit": "USD", "refs": ["consensus-estimate-version:1"]}]})
        found = cb.read_consensus(FakeStore(), "c")
        built = cb.build_bridge(record, found)
        projection = build_projection(
            record, bridge=built["bridge"], bridge_detail=built["detail"],
            consensus_fingerprint=cb.consensus_fingerprint(found))
        self.assertEqual(validate_consensus_gap(projection["consensus_bridge"]),
                         projection["consensus_bridge"])
        # The absolute gap is kept beside the validated rows, never inside
        # them: the validated shape is closed and P15d owns it.
        self.assertNotIn("gap_abs", projection["consensus_bridge"]["metrics"][0])
        self.assertIn("gap_abs", projection["bridge_detail"][0])

    def test_p15ds_own_reader_accepts_what_this_module_hands_the_authority(self):
        # Read through P15d's front door, so the two sides of the contract are
        # exercised together rather than each against its own idea of the other.
        record = model()
        period = record["forecast_periods"][0]["end"]
        install(latest_consensus=lambda store, company_ref: {"metrics": [
            {"metric": "revenue", "period": period,
             "ours": "1", "consensus": "2", "unit": "USD",
             "gap_percent": "-50.0000", "refs": ["consensus-estimate-version:1"]}]})
        gap = consensus_gap(FakeStore(), "company:x")
        self.assertEqual(gap["status"], "available")
        self.assertEqual(cb.validate_bridge(gap), gap)

    def test_a_malformed_row_is_refused_rather_than_carried(self):
        with self.assertRaises(cb.ConsensusBridgeError):
            cb.validate_bridge({"status": "available", "reason": None,
                                "metrics": [{"metric": "revenue"}]})
        with self.assertRaises(ConvictionCallValidationError):
            validate_consensus_gap({"status": "unavailable", "reason": "x",
                                    "metrics": [{"metric": "revenue"}]})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

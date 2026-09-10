"""P11b: the street's expectations are versioned, and their periods are named.

The fiscal mapping is the part of this that can be silently wrong, so most of
these tests are about it. Yahoo says ``0q`` and means "the next quarter to be
reported", which is Accenture's fiscal Q4 that ended a week ago and IBM's
September quarter that has not. A number filed against the wrong one is worse
than no number: it will be compared against an actual it was never about.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.consensus_estimate import (
    ConsensusEstimateAuthority,
    ConsensusEstimateConflict,
    ConsensusEstimateValidationError,
    FiscalMappingError,
    OBSERVATION_BASIS,
    REPORT_BASIS,
    consensus_ref_for,
    fiscal_calendar,
    map_estimate_period,
    map_recommendation_period,
    validate_report_consensus,
)
from dalton_core.store import DaltonStore

ACN = "company:sec-cik:0001467373"
INVOCATION = "connector-invocation:yfinance:" + "a" * 32
ARTIFACT = "1" * 64
GOVERNANCE = "connector-governance:yfinance-analyst-estimates:v1"
GOVERNANCE_HASH = "2" * 64
CAPTURED = "2026-09-09T18:38:55.072533+00:00"
# Accenture's fiscal year ends 31 August; its newest filed quarter ended 31 May.
ACN_CALENDAR = {"fiscal_year_end": "08-31", "last_reported_period_end": "2026-05-31"}
# IBM's ends 31 December; its newest filed quarter ended 30 June.
IBM_CALENDAR = {"fiscal_year_end": "12-31", "last_reported_period_end": "2026-06-30"}


def estimate_row(period, avg, **overrides):
    row = {
        "period": period, "avg": avg, "low": None, "high": None,
        "year_ago": None, "growth": None, "number_of_analysts": 20,
        "currency": "USD",
    }
    row.update(overrides)
    return row


def wire(**overrides):
    value = {
        "ticker": "ACN",
        "as_of": "2026-09-09",
        "price_target": {
            "current": "177.13", "high": "275", "low": "130",
            "mean": "184.1884", "median": "185", "number_of_analysts": 25,
        },
        "recommendations": [{
            "period": "0m", "strong_buy": 3, "buy": 11, "hold": 13,
            "sell": 0, "strong_sell": 0,
        }],
        "eps_estimates": [
            estimate_row("0q", "3.17886"), estimate_row("+1q", "3.98814"),
            estimate_row("0y", "13.86199"), estimate_row("+1y", "14.65516"),
        ],
        "revenue_estimates": [estimate_row("0y", "73587678730")],
    }
    value.update(overrides)
    return value


class FiscalMappingTests(unittest.TestCase):
    """Zero is the next period to be reported, and both halves of that matter."""

    def test_accenture_current_quarter_is_the_one_that_just_ended(self):
        cal = fiscal_calendar(**ACN_CALENDAR)
        mapped = map_estimate_period("0q", cal)
        self.assertEqual(mapped["label"], "FY2026Q4")
        self.assertEqual(mapped["period_end"], "2026-08-31")
        self.assertEqual(mapped["fiscal_quarter"], 4)

    def test_ibm_current_quarter_is_the_one_still_running(self):
        # The same key, the same afternoon, a different company: this is the
        # whole reason the mapping is not a constant.
        mapped = map_estimate_period("0q", fiscal_calendar(**IBM_CALENDAR))
        self.assertEqual(mapped["label"], "FY2026Q3")
        self.assertEqual(mapped["period_end"], "2026-09-30")

    def test_next_quarter_crosses_the_fiscal_year_for_accenture(self):
        mapped = map_estimate_period("+1q", fiscal_calendar(**ACN_CALENDAR))
        self.assertEqual(mapped["label"], "FY2027Q1")
        self.assertEqual(mapped["period_end"], "2026-11-30")

    def test_fiscal_years_land_where_the_filings_say(self):
        acn = fiscal_calendar(**ACN_CALENDAR)
        self.assertEqual(map_estimate_period("0y", acn)["label"], "FY2026")
        self.assertEqual(map_estimate_period("0y", acn)["period_end"], "2026-08-31")
        self.assertEqual(map_estimate_period("+1y", acn)["period_end"], "2027-08-31")
        ibm = fiscal_calendar(**IBM_CALENDAR)
        self.assertEqual(map_estimate_period("0y", ibm)["period_end"], "2026-12-31")

    def test_a_march_year_end_quarters_on_month_ends(self):
        # DXC. Its Q2 ends in September, and a day-of-month rule that did not
        # notice 31 March is a month end would put it on 30 September anyway
        # and be right by accident; February is where that breaks.
        cal = fiscal_calendar(
            fiscal_year_end="03-31", last_reported_period_end="2026-06-30"
        )
        self.assertEqual(map_estimate_period("0q", cal)["label"], "FY2027Q2")
        self.assertEqual(map_estimate_period("0q", cal)["period_end"], "2026-09-30")
        self.assertEqual(map_estimate_period("+1y", cal)["period_end"], "2028-03-31")

    def test_a_fiscal_year_ending_in_february_keeps_the_short_month(self):
        cal = fiscal_calendar(
            fiscal_year_end="02-28", last_reported_period_end="2026-05-31"
        )
        self.assertEqual(map_estimate_period("0q", cal)["period_end"], "2026-08-31")
        self.assertEqual(map_estimate_period("0y", cal)["period_end"], "2027-02-28")

    def test_the_day_the_annual_lands_is_not_a_dead_zone(self):
        # B1. When the last reported period end *is* the fiscal year end -- the
        # two months a year between the 10-K and the quarter after it -- an
        # on-or-after rule answers with the year just reported, which nobody is
        # estimating any more. That used to raise and take the whole version
        # with it, leaving the company unmapped in the window where the
        # street's next-year number is most worth having.
        for label, fye in (("ACN", "08-31"), ("IBM", "12-31"), ("DXC", "03-31")):
            with self.subTest(company=label):
                anchor = f"2026-{fye}"
                cal = fiscal_calendar(
                    fiscal_year_end=fye, last_reported_period_end=anchor
                )
                year = map_estimate_period("0y", cal)
                self.assertEqual(year["fiscal_year"], 2027)
                self.assertGreater(year["period_end"], anchor)
                self.assertEqual(map_estimate_period("+1y", cal)["fiscal_year"], 2028)
                # And the quarter branch, which always used a strict
                # comparison, still agrees with it.
                quarter = map_estimate_period("0q", cal)
                self.assertEqual(quarter["fiscal_year"], 2027)
                self.assertEqual(quarter["fiscal_quarter"], 1)
                self.assertGreater(quarter["period_end"], anchor)

    def test_a_version_publishes_on_the_day_the_annual_lands(self):
        # The dead zone was only visible through the authority: the mapping
        # raised, so nothing was published at all.
        authority = ConsensusEstimateAuthority(
            DaltonStore(str(Path(tempfile.mkdtemp()) / "core.sqlite"))
        )
        self.addCleanup(authority.store.close)
        published = authority.publish_consensus(
            company_ref=ACN, wire=wire(), fiscal_year_end="08-31",
            last_reported_period_end="2026-08-31",
            invocation_ref=INVOCATION, artifact_hash=ARTIFACT,
            governance_ref=GOVERNANCE, governance_hash=GOVERNANCE_HASH,
            captured_at=CAPTURED,
        )
        self.assertEqual(published["status"], "fresh")
        self.assertEqual(
            [row["label"] for row in published["eps_estimates"]],
            ["FY2027Q1", "FY2027Q2", "FY2027", "FY2028"],
        )

    def test_an_unknown_fiscal_year_end_is_refused_rather_than_guessed(self):
        with self.assertRaises(FiscalMappingError) as caught:
            fiscal_calendar(fiscal_year_end=None, last_reported_period_end="2026-05-31")
        self.assertIn("fiscal calendar", str(caught.exception))

    def test_an_unknown_last_reported_period_is_refused(self):
        with self.assertRaises(FiscalMappingError):
            fiscal_calendar(fiscal_year_end="08-31", last_reported_period_end=None)

    def test_a_key_the_vendor_never_sends_is_refused(self):
        with self.assertRaises(FiscalMappingError):
            map_estimate_period("+2y", fiscal_calendar(**ACN_CALENDAR))

    def test_recommendation_months_are_calendar_months(self):
        # A rating count is about the month a vendor aggregated it in. That is
        # a calendar month wherever the company's fiscal year ends.
        self.assertEqual(
            map_recommendation_period("-2m", "2026-09-09"), {"month": "2026-07"}
        )
        self.assertEqual(
            map_recommendation_period("0m", "2026-01-15"), {"month": "2026-01"}
        )


class AuthorityTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = DaltonStore(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.authority = ConsensusEstimateAuthority(self.store)

    def publish(self, **overrides):
        payload = {
            "company_ref": ACN, "wire": wire(), **ACN_CALENDAR,
            "invocation_ref": INVOCATION, "artifact_hash": ARTIFACT,
            "governance_ref": GOVERNANCE, "governance_hash": GOVERNANCE_HASH,
            "captured_at": CAPTURED,
        }
        payload.update(overrides)
        return self.authority.publish_consensus(**payload)


class PublishingTests(AuthorityTestCase):
    def test_a_first_version_carries_the_mapped_periods(self):
        published = self.publish()
        self.assertEqual(published["status"], "fresh")
        self.assertEqual(published["version"], 1)
        labels = [row["label"] for row in published["eps_estimates"]]
        self.assertEqual(labels, ["FY2026Q4", "FY2026", "FY2027Q1", "FY2027"])
        self.assertEqual(published["observation_basis"], OBSERVATION_BASIS)

    def test_every_figure_is_bound_to_the_call_that_fetched_it(self):
        published = self.publish()
        self.assertEqual(published["fetch"]["invocation_ref"], INVOCATION)
        self.assertEqual(published["fetch"]["artifact_hash"], ARTIFACT)
        self.assertEqual(published["fetch"]["governance_hash"], GOVERNANCE_HASH)
        held = self.authority.latest_consensus(ACN)
        self.assertEqual(held["invocation_ref"], INVOCATION)
        self.assertEqual(held["artifact_hash"], ARTIFACT)

    def test_the_same_numbers_tomorrow_are_a_duplicate(self):
        self.publish()
        again = self.publish(
            invocation_ref="connector-invocation:yfinance:" + "b" * 32,
            artifact_hash="3" * 64,
        )
        # A different call, the same answer. Publishing a version for it would
        # make the chain a log of ticks rather than of revisions.
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(len(self.authority.versions(ACN)), 1)

    def test_a_moved_target_is_a_new_version_that_names_what_moved(self):
        self.publish()
        moved = wire()
        moved["price_target"] = {**moved["price_target"], "mean": "190.0"}
        published = self.publish(wire=moved)
        self.assertEqual(published["status"], "fresh")
        self.assertEqual(published["version"], 2)
        self.assertEqual(published["changed_fields"], ["price_target"])
        # The old version keeps its own number.
        self.assertEqual(
            self.authority.versions(ACN)[0]["price_target"]["mean"], "184.1884"
        )

    def test_a_moved_estimate_is_a_new_version(self):
        self.publish()
        moved = wire()
        moved["eps_estimates"] = [
            estimate_row("0q", "3.17886"), estimate_row("+1q", "3.98814"),
            estimate_row("0y", "13.86199"), estimate_row("+1y", "15.00"),
        ]
        published = self.publish(wire=moved)
        self.assertEqual(published["changed_fields"], ["eps_estimates"])

    def test_a_company_with_no_fiscal_calendar_publishes_nothing(self):
        with self.assertRaises(FiscalMappingError):
            self.publish(fiscal_year_end=None)
        self.assertIsNone(self.authority.latest_version(ACN))

    def test_an_empty_vendor_response_is_a_failed_read_not_a_company_without_coverage(self):
        empty = wire(
            price_target=None, recommendations=[], eps_estimates=[],
            revenue_estimates=[],
        )
        with self.assertRaises(ConsensusEstimateValidationError) as caught:
            self.publish(wire=empty)
        self.assertIn("nothing to publish", str(caught.exception))

    def test_estimates_in_two_currencies_are_refused(self):
        mixed = wire(eps_estimates=[
            estimate_row("0y", "13.86199"),
            estimate_row("+1y", "14.65516", currency="EUR"),
        ])
        with self.assertRaises(ConsensusEstimateConflict):
            self.publish(wire=mixed)

    def test_a_low_above_its_high_is_refused(self):
        crossed = wire(eps_estimates=[
            estimate_row("0y", "13.0", low="14.0", high="12.0"),
        ])
        with self.assertRaises(ConsensusEstimateConflict):
            self.publish(wire=crossed)

    def test_the_ticker_cannot_change_underneath_a_chain(self):
        self.publish()
        with self.assertRaises(ConsensusEstimateConflict):
            self.publish(wire=wire(ticker="XYZ"))

    def test_a_float_is_not_a_figure(self):
        with self.assertRaises(ConsensusEstimateValidationError):
            self.publish(wire=wire(eps_estimates=[estimate_row("0y", 13.86199)]))


class ReaderTests(AuthorityTestCase):
    def test_consensus_for_period_answers_in_fiscal_labels(self):
        self.publish()
        held = self.authority.consensus_for_period(ACN, "FY2027")
        self.assertEqual(held["period_end"], "2027-08-31")
        self.assertEqual(held["eps"]["avg"], "14.65516")
        self.assertEqual(held["period_kind"], "fiscal_year")
        self.assertEqual(held["observation_basis"], OBSERVATION_BASIS)

    def test_consensus_for_period_does_not_answer_in_the_vendors_words(self):
        # "0y" is a question whose answer changes every quarter without the
        # question changing. Answering it is how a consensus ends up beside the
        # wrong actual.
        self.publish()
        self.assertIsNone(self.authority.consensus_for_period(ACN, "0y"))

    def test_a_company_with_no_chain_reads_as_none(self):
        self.assertIsNone(self.authority.latest_consensus(ACN))
        self.assertIsNone(self.authority.consensus_for_period(ACN, "FY2027"))


class ImmutabilityTests(AuthorityTestCase):
    def test_versions_cannot_be_updated_or_deleted(self):
        self.publish()
        for statement in (
            "UPDATE consensus_estimate_versions SET as_of='2020-01-01'",
            "DELETE FROM consensus_estimate_versions",
        ):
            with self.assertRaises(sqlite3.DatabaseError):
                with self.store._transaction() as cur:
                    cur.execute(statement)

    def test_an_unauthorised_connection_cannot_insert(self):
        self.publish()
        raw = sqlite3.connect(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(raw.close)
        with self.assertRaises(sqlite3.DatabaseError):
            raw.execute(
                "INSERT INTO consensus_estimate_versions(version_id,consensus_ref,"
                "version_number,prior_version_id,company_ref,ticker,source_ref,"
                "source_kind,change_reason,currency,as_of,target_price_mean,"
                "analyst_count,eps_period_count,revenue_period_count,record_json,"
                "content_hash,actor_ref,created_at) "
                "VALUES('x',?,9,NULL,?,'ACN','source:yahoo-finance',"
                "'vendor_observation','evidence_thicker','USD','2026-09-09',NULL,"
                "NULL,0,0,'{}','h','a','t')",
                (consensus_ref_for(ACN), ACN),
            )
            raw.commit()

    def test_a_tampered_record_does_not_read_back(self):
        published = self.publish()
        path = Path(self._dir.name) / "core.sqlite"
        self.store.close()
        raw = sqlite3.connect(str(path))
        body = json.loads(raw.execute(
            "SELECT record_json FROM consensus_estimate_versions WHERE version_id=?",
            (published["id"],),
        ).fetchone()[0])
        body["price_target"]["mean"] = "999"
        raw.execute("PRAGMA writable_schema=ON")
        raw.execute("DROP TRIGGER consensus_estimate_no_update")
        raw.execute("PRAGMA writable_schema=OFF")
        raw.execute(
            "UPDATE consensus_estimate_versions SET record_json=? WHERE version_id=?",
            (json.dumps(body, sort_keys=True, separators=(",", ":")), published["id"]),
        )
        raw.commit()
        raw.close()
        store = DaltonStore(str(path))
        self.addCleanup(store.close)
        with self.assertRaises(ConsensusEstimateConflict):
            ConsensusEstimateAuthority(store).latest_version(ACN)


class ResolvedByNameTests(AuthorityTestCase):
    """The two readers other slices look up by name, and the shapes they expect.

    P15d's conviction call and P13-M3's sensitivity work both resolve this
    module at call time rather than importing it, so that they would start
    working the day it landed. That makes these two signatures a contract with
    code that cannot see them, which is exactly the kind that breaks quietly.
    """

    def street(self):
        from dalton_core.street_estimate import StreetEstimateStore

        return StreetEstimateStore(self.store)

    def note(self, broker, value, document, published_on="2026-09-02"):
        from tests.test_street_estimate import estimate, figure

        return self.street().record(estimate(
            company_ref=ACN, broker=broker, broker_as_named=broker,
            document_ref=document, published_on=published_on, rating=None,
            target_price={"value": value, "currency": "USD", "horizon": None,
                          "quote_id": "quote:0:1200:abc"},
            figures=[figure(value=value, quote=(
                "Accenture PLC\nSeptember 2, 2026\nPrice Target: $%s\n" % value))],
        ))

    def test_report_consensus_returns_one_row_per_house(self):
        from dalton_core.consensus_estimate import report_consensus

        self.note("td", "173.00", "alphaengine-doc:1")
        self.note("wells-fargo", "194.00", "alphaengine-doc:2")
        rows = report_consensus(self.store, ACN, as_of="2026-09-09")
        self.assertEqual([row["broker"] for row in rows], ["td", "wells-fargo"])
        self.assertEqual([row["value"] for row in rows], ["173.00", "194.00"])
        for row in rows:
            # Exactly three keys: a caller that closed the shape would refuse
            # a fourth.
            self.assertEqual(set(row), {"broker", "value", "refs"})
            self.assertEqual(len(row["refs"]), 2)
            self.assertTrue(all(isinstance(ref, str) and ref for ref in row["refs"]))

    def test_one_house_twice_is_not_a_range_here_either(self):
        from dalton_core.consensus_estimate import report_consensus

        self.note("td", "11.00", "alphaengine-doc:1")
        self.note("td", "11.00", "alphaengine-doc:2")
        self.assertEqual(report_consensus(self.store, ACN, as_of="2026-09-09"), [])

    def test_a_house_publishing_twice_contributes_its_newest_note(self):
        from dalton_core.consensus_estimate import report_consensus

        self.note("td", "151.00", "alphaengine-doc:1", published_on="2026-07-01")
        self.note("td", "173.00", "alphaengine-doc:2", published_on="2026-09-02")
        self.note("wells-fargo", "194.00", "alphaengine-doc:3")
        rows = {row["broker"]: row for row in
                report_consensus(self.store, ACN, as_of="2026-09-09")}
        self.assertEqual(rows["td"]["value"], "173.00")
        self.assertEqual(rows["td"]["refs"][1], "alphaengine-doc:2")

    def test_a_company_with_no_notes_is_an_empty_list_not_an_error(self):
        from dalton_core.consensus_estimate import report_consensus

        self.assertEqual(report_consensus(self.store, ACN, as_of="2026-09-09"), [])

    def test_a_core_with_no_street_table_is_an_empty_list_not_an_outage(self):
        from dalton_core.consensus_estimate import report_consensus

        class Bare:
            connection = None

        self.assertEqual(report_consensus(Bare(), ACN), [])

    def test_latest_consensus_is_none_until_this_company_has_a_chain(self):
        from dalton_core.consensus_estimate import latest_consensus

        self.assertIsNone(latest_consensus(self.store, ACN))

    def test_latest_consensus_has_no_gap_without_a_forecast_to_compare(self):
        from dalton_core.consensus_estimate import latest_consensus

        self.publish()
        # The street is held; ours is not. An absence, never agreement.
        self.assertEqual(latest_consensus(self.store, ACN), {"metrics": []})


class ForecastGapTests(AuthorityTestCase):
    """The gap rows, checked by the contract the consumer will check them with.

    C. ``latest_consensus`` feeds a slice that is on the other side of a name
    lookup, so "the shape is right" is not something either side can see. Two
    things follow. The forecast model is stood up as a real model body and
    parsed by the real ``_forecast_cells`` -- only the *source* of the body is
    replaced, never the reading of it, so these tests fail when the reading
    breaks. And the rows are run through ``validate_consensus_gap``, the
    consumer's own validator, rather than compared with a dict written by hand
    in this file, which would only prove the file agrees with itself.
    """

    def line(self, label, unit, cells, role="revenue"):
        return {"ref": f"result:{label}", "role": role, "label": label,
                "unit": unit, "formula": None, "driver_ref": None,
                "status": "computed", "reason": None, "cells": cells}

    def cell(self, end, value, kind="estimate", superseded_by=None, status="computed"):
        return {"ref": f"cell:{end}:{kind}", "period": {"end": end},
                "kind": kind, "status": status, "value": value,
                "reason": None, "superseded_by": superseded_by,
                "assumption_refs": [], "input_cell_refs": [], "result_refs": []}

    def install_forecast(self, results):
        """A real model body, read by the real reader."""

        from unittest.mock import patch

        from dalton_core.model_forecast_driver import ForecastModelAuthority

        model = {"id": "forecast-model-version:acn:3", "company_ref": ACN,
                 "results": results}
        patcher = patch.object(
            ForecastModelAuthority, "latest", autospec=True,
            return_value=model,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return model

    def test_the_rows_satisfy_the_consumers_own_validator(self):
        from dalton_core.consensus_estimate import latest_consensus
        from dalton_core.conviction_call import validate_consensus_gap

        # The default fixture quotes revenue for the current year only, so
        # give the street a next-year revenue number to be compared against.
        self.publish(wire=wire(revenue_estimates=[
            estimate_row("0y", "73587678730"),
            estimate_row("+1y", "76583683960"),
        ]))
        self.install_forecast([
            self.line("Net revenue", "USD", [self.cell("2027-08-31", "80000000000")]),
            self.line("Diluted EPS", "USD",
                      [self.cell("2027-08-31", "16.00")], role="eps"),
        ])
        found = latest_consensus(self.store, ACN)
        gap = validate_consensus_gap(
            {"status": "available", "reason": None, "metrics": found["metrics"]}
        )
        self.assertEqual(len(gap["metrics"]), 2)
        self.assertEqual({row["period"] for row in gap["metrics"]}, {"FY2027"})
        self.assertEqual(
            {row["metric"] for row in gap["metrics"]},
            {"Net revenue", "Diluted EPS"},
        )
        for row in gap["metrics"]:
            # Every row points back at both sides of its own arithmetic.
            self.assertIn("forecast-model-version:acn:3", row["refs"])
            self.assertTrue(any(ref.startswith("consensus-estimate-version:")
                                for ref in row["refs"]))

    def test_the_gap_is_a_percentage_of_the_street(self):
        from dalton_core.consensus_estimate import latest_consensus

        self.publish()
        # The street's FY2027 EPS is 14.65516; ours is exactly double.
        self.install_forecast([
            self.line("Diluted EPS", "USD",
                      [self.cell("2027-08-31", "29.31032")], role="eps"),
        ])
        row = latest_consensus(self.store, ACN)["metrics"][0]
        self.assertEqual(row["consensus"], "14.65516")
        self.assertEqual(row["ours"], "29.31032")
        self.assertEqual(row["gap_percent"], "100.00")

    def test_a_superseded_cell_is_not_our_number_any_more(self):
        # D. The owner's versioning rule keeps a superseded estimate rather
        # than overwriting it, so it is still on the model and still looks like
        # a forecast. Reading it would compare the street to a number we no
        # longer hold.
        from dalton_core.consensus_estimate import latest_consensus

        self.publish()
        self.install_forecast([
            self.line("Diluted EPS", "USD", [
                self.cell("2027-08-31", "11.00", superseded_by="cell:later"),
            ], role="eps"),
        ])
        self.assertEqual(latest_consensus(self.store, ACN), {"metrics": []})

    def test_an_actualised_period_is_read_as_the_actual(self):
        from dalton_core.consensus_estimate import latest_consensus

        self.publish()
        self.install_forecast([
            self.line("Diluted EPS", "USD", [
                self.cell("2027-08-31", "11.00", superseded_by="cell:actual"),
                self.cell("2027-08-31", "15.00", kind="actual"),
            ], role="eps"),
        ])
        row = latest_consensus(self.store, ACN)["metrics"][0]
        self.assertEqual(row["ours"], "15.00")

    def test_a_cell_that_could_not_be_computed_is_not_a_number(self):
        from dalton_core.consensus_estimate import latest_consensus

        self.publish()
        self.install_forecast([
            self.line("Diluted EPS", "USD", [
                self.cell("2027-08-31", None, status="unavailable"),
            ], role="eps"),
        ])
        self.assertEqual(latest_consensus(self.store, ACN), {"metrics": []})

    def test_a_period_the_street_does_not_cover_produces_no_row(self):
        from dalton_core.consensus_estimate import latest_consensus

        self.publish()
        self.install_forecast([
            self.line("Diluted EPS", "USD",
                      [self.cell("2031-08-31", "20.00")], role="eps"),
        ])
        self.assertEqual(latest_consensus(self.store, ACN), {"metrics": []})

    def test_a_line_that_is_neither_eps_nor_revenue_is_not_compared(self):
        from dalton_core.consensus_estimate import latest_consensus

        self.publish()
        self.install_forecast([
            self.line("Headcount", "count",
                      [self.cell("2027-08-31", "800000")], role="operating"),
        ])
        self.assertEqual(latest_consensus(self.store, ACN), {"metrics": []})

    def test_no_forecast_at_all_is_an_absence_never_agreement(self):
        from dalton_core.consensus_estimate import latest_consensus

        self.publish()
        self.assertEqual(latest_consensus(self.store, ACN), {"metrics": []})

    def test_a_zero_street_number_is_not_a_percentage_of_anything(self):
        from dalton_core.consensus_estimate import _gap_percent

        self.assertIsNone(_gap_percent("1", "0"))
        self.assertIsNone(_gap_percent("1", "not a number"))
        # A negative street number still has a magnitude to divide by, and
        # dividing by the magnitude rather than the signed value is what keeps
        # the sign meaning "we are above the street": a loss of 1 against an
        # expected loss of 2 is us being 50% better, not 50% worse.
        self.assertEqual(_gap_percent("-1", "-2"), "50.00")
        self.assertEqual(_gap_percent("-3", "-2"), "-50.00")


class ReportConsensusBlockTests(AuthorityTestCase):
    BLOCK = {
        "metric": "metric:price-target", "period": "current",
        "as_of": "2026-09-09", "window_days": 90, "broker_count": 2,
        "brokers": ["td", "wells-fargo"], "low": "173.00", "high": "194.00",
        "mean": "183.5", "currency": "USD",
        "estimate_refs": ["street-estimate:a", "street-estimate:b"],
        "document_refs": ["alphaengine-doc:1", "alphaengine-doc:2"],
        "policy_ref": "street-consensus-policy:p11b:0.1",
        "policy_hash": "4" * 64,
    }

    def test_a_range_attaches_to_an_existing_chain(self):
        self.publish()
        published = self.authority.publish_report_consensus(
            company_ref=ACN, report_consensus=dict(self.BLOCK)
        )
        self.assertEqual(published["status"], "fresh")
        self.assertEqual(published["source_kind"], "report_consensus")
        self.assertEqual(published["observation_basis"], REPORT_BASIS)
        self.assertEqual(published["report_consensus"]["brokers"], ["td", "wells-fargo"])
        # The vendor block is carried through untouched; the version is about
        # the range, not a re-reading of Yahoo.
        self.assertEqual(published["price_target"]["mean"], "184.1884")

    def test_the_next_vendor_observation_carries_the_range_forward(self):
        self.publish()
        self.authority.publish_report_consensus(
            company_ref=ACN, report_consensus=dict(self.BLOCK)
        )
        moved = wire()
        moved["price_target"] = {**moved["price_target"], "mean": "190.0"}
        published = self.publish(wire=moved)
        self.assertEqual(published["changed_fields"], ["price_target"])
        self.assertEqual(published["report_consensus"]["broker_count"], 2)

    def test_the_same_range_twice_is_a_duplicate(self):
        self.publish()
        self.authority.publish_report_consensus(
            company_ref=ACN, report_consensus=dict(self.BLOCK)
        )
        again = self.authority.publish_report_consensus(
            company_ref=ACN, report_consensus=dict(self.BLOCK)
        )
        self.assertEqual(again["status"], "duplicate")

    def test_a_range_cannot_open_a_chain(self):
        # A spread between two brokers' targets, with no vendor observation to
        # read it against, is not a consensus estimate for this company.
        with self.assertRaises(ConsensusEstimateConflict):
            self.authority.publish_report_consensus(
                company_ref=ACN, report_consensus=dict(self.BLOCK)
            )

    def test_one_broker_is_not_a_range(self):
        block = {**self.BLOCK, "brokers": ["td"], "broker_count": 1}
        with self.assertRaises(ConsensusEstimateValidationError) as caught:
            validate_report_consensus(block)
        self.assertIn("two independent brokers", str(caught.exception))

    def test_the_broker_count_must_count_the_brokers(self):
        with self.assertRaises(ConsensusEstimateValidationError):
            validate_report_consensus({**self.BLOCK, "broker_count": 5})

    def test_a_range_must_name_the_notes_it_came_from(self):
        with self.assertRaises(ConsensusEstimateValidationError):
            validate_report_consensus({**self.BLOCK, "document_refs": ["only-one"]})


if __name__ == "__main__":
    unittest.main()

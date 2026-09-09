"""C1: the calendar authority -- who said a date, and what it takes to change it."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dalton_core.catalyst_calendar import (
    CHANGE_REASONS,
    MAX_PAST_DAYS,
    PREVIEW_LEAD_DAYS,
    CatalystCalendarAuthority,
    CatalystCalendarConflict,
    CatalystCalendarValidationError,
    calendar_event_key,
    emit_calendar_events,
    entry_ref_for,
    required_change_reason,
)
from dalton_core.store import DaltonStore

COMPANY = "company:sec-cik:0001467373"
YAHOO = "connector-invocation:yfinance:aaaa"
ACCESSION = "sec:filing:0001467373-26-000031"


def vendor_source(day, *, ref=YAHOO, at="2026-09-09T15:00:00+00:00"):
    return {
        "kind": "connector_invocation", "ref": ref,
        "source_ref": "source:yahoo-finance", "observed_date": day,
        "observed_at": at, "confidence": "estimated", "note": "",
    }


def filed_source(day, *, ref=ACCESSION, at="2026-10-01T12:00:00+00:00"):
    return {
        "kind": "filing", "ref": ref, "source_ref": "source:sec-edgar",
        "observed_date": day, "observed_at": at, "confidence": "confirmed",
        "note": "8-K Item 2.02",
    }


def entry(day, *, kind="earnings", subject="2026q4", sources=None, notes=""):
    return {
        "event_kind": kind, "subject": subject,
        "sources": sources if sources is not None else [vendor_source(day)],
        "notes": notes,
    }


class AuthorityTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = DaltonStore(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.authority = CatalystCalendarAuthority(self.store)

    def publish(self, entries, *, reason="evidence_thicker", now="2026-09-09",
                refs=("evidence:one",)):
        return self.authority.publish(
            company_ref=COMPANY, entries=entries, change_reason=reason,
            evidence_refs=list(refs), now=now,
        )


class ConfidenceTests(AuthorityTestCase):
    def test_a_vendor_date_is_estimated_and_a_filed_date_is_confirmed(self):
        published = self.publish([
            entry("2026-10-01"),
            entry("2026-06-18", subject="2026q2", sources=[filed_source("2026-06-18")]),
        ])
        by_subject = {item["subject"]: item for item in published["entries"]}
        self.assertEqual(by_subject["2026q4"]["confidence"], "estimated")
        self.assertEqual(by_subject["2026q2"]["confidence"], "confirmed")

    def test_a_vendor_source_cannot_be_labelled_confirmed(self):
        # The whole point of two sources is that one of them cannot confirm.
        # A caller that could set the label could turn the distinction off.
        bad = vendor_source("2026-10-01") | {"confidence": "confirmed"}
        with self.assertRaises(CatalystCalendarConflict) as raised:
            self.publish([entry("2026-10-01", sources=[bad])])
        self.assertIn("only a company-issued source confirms", str(raised.exception))

    def test_a_filed_source_cannot_be_labelled_estimated(self):
        bad = filed_source("2026-06-18") | {"confidence": "estimated"}
        with self.assertRaises(CatalystCalendarConflict):
            self.publish([entry("2026-06-18", subject="2026q2", sources=[bad])])

    def test_an_entry_needs_a_source_because_a_dateless_date_is_a_guess(self):
        with self.assertRaises(CatalystCalendarValidationError):
            self.publish([entry("2026-10-01", sources=[])])


class DisagreementTests(AuthorityTestCase):
    def test_two_sources_that_disagree_keep_both_dates_and_the_confirmed_one_wins(self):
        published = self.publish([entry("2026-10-01", sources=[
            vendor_source("2026-10-01"), filed_source("2026-10-02"),
        ])])
        found = published["entries"][0]
        self.assertEqual(found["expected_date"], "2026-10-02")
        self.assertEqual(found["confidence"], "confirmed")
        self.assertTrue(found["disagreement"])
        self.assertEqual(found["disagreeing_dates"], ["2026-10-01", "2026-10-02"])
        # Never averaged, and never dropped: both are still readable.
        self.assertEqual(
            sorted(source["observed_date"] for source in found["sources"]),
            ["2026-10-01", "2026-10-02"],
        )

    def test_two_agreeing_sources_are_not_a_disagreement(self):
        published = self.publish([entry("2026-10-01", sources=[
            vendor_source("2026-10-01"), filed_source("2026-10-01"),
        ])])
        found = published["entries"][0]
        self.assertFalse(found["disagreement"])
        self.assertEqual(found["disagreeing_dates"], [])
        self.assertEqual(found["confidence"], "confirmed")

    def test_a_later_vendor_reading_replaces_the_earlier_one(self):
        self.publish([entry("2026-10-01")])
        published = self.publish([entry("2026-10-08", sources=[vendor_source(
            "2026-10-08", ref="connector-invocation:yfinance:bbbb",
            at="2026-09-10T15:00:00+00:00")])], reason="driver_event")
        found = published["entries"][0]
        # One vendor, one opinion. Yahoo re-read tomorrow is not a second
        # witness, so the entry is not permanently at odds with its own past.
        self.assertEqual(len(found["sources"]), 1)
        self.assertFalse(found["disagreement"])
        self.assertEqual(found["expected_date"], "2026-10-08")

    def test_two_filings_in_one_quarter_are_two_announcements(self):
        published = self.publish([entry("2026-07-22", subject="2026q3", sources=[
            filed_source("2026-07-14", ref="sec:filing:0000051143-26-000070",
                         at="2026-07-14T12:00:00+00:00"),
            filed_source("2026-07-22", ref="sec:filing:0000051143-26-000077",
                         at="2026-07-22T12:00:00+00:00"),
        ])])
        found = published["entries"][0]
        self.assertEqual(len(found["sources"]), 2)
        # The later announcement is the one the entry asserts, and the earlier
        # one is still on the record rather than collapsed into it.
        self.assertEqual(found["expected_date"], "2026-07-22")
        self.assertTrue(found["disagreement"])

    def test_an_older_reading_does_not_walk_the_calendar_backwards(self):
        self.publish([entry("2026-10-08", sources=[vendor_source(
            "2026-10-08", at="2026-09-10T15:00:00+00:00")])])
        published = self.publish([entry("2026-10-01", sources=[vendor_source(
            "2026-10-01", at="2026-09-09T15:00:00+00:00")])])
        self.assertEqual(published["status"], "duplicate")
        self.assertEqual(published["entries"][0]["expected_date"], "2026-10-08")


class VersionTests(AuthorityTestCase):
    def test_a_moved_date_is_a_new_version_with_driver_event(self):
        first = self.publish([entry("2026-10-01")])
        self.assertEqual(first["status"], "fresh")
        second = self.publish(
            [entry("2026-10-08", sources=[vendor_source(
                "2026-10-08", at="2026-09-10T15:00:00+00:00")])],
            reason="driver_event",
        )
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["change_reason"], "driver_event")
        self.assertEqual(second["prior_version_ref"], first["id"])
        moved = [c for c in second["changes"] if c["change"] == "date_moved"]
        self.assertEqual(len(moved), 1)
        self.assertEqual((moved[0]["from"], moved[0]["to"]),
                         ("2026-10-01", "2026-10-08"))
        # The superseded date is not gone: it is one link back on the chain.
        chain = self.authority.versions(COMPANY)
        self.assertEqual(chain[0]["entries"][0]["expected_date"], "2026-10-01")

    def test_estimated_becoming_confirmed_is_evidence_thicker(self):
        self.publish([entry("2026-10-01")])
        second = self.publish([entry("2026-10-01", sources=[
            filed_source("2026-10-01"),
        ])])
        self.assertEqual(second["status"], "fresh")
        self.assertEqual(second["change_reason"], "evidence_thicker")
        changed = [c for c in second["changes"] if c["change"] == "confidence_changed"]
        self.assertEqual([(c["from"], c["to"]) for c in changed],
                         [("estimated", "confirmed")])
        self.assertEqual(second["entries"][0]["confidence"], "confirmed")

    def test_the_same_reading_twice_is_a_duplicate(self):
        self.publish([entry("2026-10-01")])
        again = self.publish([entry("2026-10-01")])
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["version"], 1)
        self.assertEqual(len(self.authority.versions(COMPANY)), 1)

    def test_a_reason_the_diff_contradicts_is_refused(self):
        self.publish([entry("2026-10-01")])
        with self.assertRaises(CatalystCalendarConflict) as raised:
            self.publish([entry("2026-10-08", sources=[vendor_source(
                "2026-10-08", at="2026-09-10T15:00:00+00:00")])],
                reason="assumption_review")
        self.assertIn("driver_event", str(raised.exception))

    def test_a_person_may_always_say_it_was_a_human_revision(self):
        self.publish([entry("2026-10-01")])
        second = self.publish(
            [entry("2026-10-08", sources=[vendor_source(
                "2026-10-08", at="2026-09-10T15:00:00+00:00")])],
            reason="human_revision",
        )
        self.assertEqual(second["change_reason"], "human_revision")

    def test_a_version_without_evidence_refs_is_refused(self):
        with self.assertRaises(CatalystCalendarValidationError):
            self.publish([entry("2026-10-01")], refs=())

    def test_a_version_reads_back_out_of_the_row_it_wrote(self):
        published = self.publish([entry("2026-10-01")])
        self.assertEqual(self.authority.version(published["id"]), {
            key: value for key, value in published.items() if key != "status"
        })

    def test_the_change_reason_vocabulary_is_adr_0008s(self):
        # Declared locally so this module stays cheap to import from a lane;
        # pinned here so the two copies cannot drift apart in silence.
        from dalton_core.model_forecast_driver import (
            CHANGE_REASONS as FORECAST_REASONS,
        )

        self.assertEqual(CHANGE_REASONS, FORECAST_REASONS)

    def test_the_reason_is_derived_from_what_moved(self):
        self.assertEqual(required_change_reason([{"change": "added"}]),
                         "evidence_thicker")
        self.assertEqual(required_change_reason([{"change": "confidence_changed"}]),
                         "evidence_thicker")
        self.assertEqual(
            required_change_reason([{"change": "added"}, {"change": "date_moved"}]),
            "driver_event",
        )

    def test_an_entry_that_aged_out_never_publishes_a_version_by_itself(self):
        old = "2026-01-01"
        self.publish([entry(old, subject="2026q1", sources=[filed_source(
            old, ref="sec:filing:x", at=f"{old}T12:00:00+00:00")])], now=old)
        # Far past the retention floor, but nothing else changed.
        later = "2026-09-09"
        again = self.publish([entry(old, subject="2026q1", sources=[filed_source(
            old, ref="sec:filing:x", at=f"{old}T12:00:00+00:00")])], now=later)
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["entry_count"], 1)
        # It leaves only alongside a real change, and only then.
        published = self.publish([entry("2026-10-01")], now=later)
        self.assertEqual(published["entry_count"], 1)
        self.assertEqual(published["entries"][0]["expected_date"], "2026-10-01")
        self.assertIn("retired", {c["change"] for c in published["changes"]})
        self.assertGreater(MAX_PAST_DAYS, 0)


class ReaderTests(AuthorityTestCase):
    def setUp(self):
        super().setUp()
        self.publish([
            entry("2026-10-01"),
            entry("2026-11-05", subject="2026q4-b"),
            entry("2026-06-18", subject="2026q2", sources=[filed_source("2026-06-18")]),
        ])

    def test_next_catalyst_is_the_soonest_thing_that_has_not_happened(self):
        found = self.authority.next_catalyst(COMPANY, "2026-09-09")
        self.assertEqual(found["expected_date"], "2026-10-01")
        self.assertEqual(found["days_until"], 22)
        self.assertEqual(found["company_ref"], COMPANY)

    def test_today_counts_as_forthcoming(self):
        found = self.authority.next_catalyst(COMPANY, "2026-10-01")
        self.assertEqual(found["expected_date"], "2026-10-01")
        self.assertEqual(found["days_until"], 0)

    def test_a_company_with_no_calendar_has_no_next_catalyst(self):
        self.assertIsNone(self.authority.next_catalyst("company:sec-cik:0000000001"))

    def test_nothing_forthcoming_reads_as_none_rather_than_the_last_one(self):
        self.assertIsNone(self.authority.next_catalyst(COMPANY, "2027-01-01"))

    def test_upcoming_is_bounded_by_its_horizon_and_sorted_by_date(self):
        found = self.authority.upcoming("2026-09-09", 30)
        self.assertEqual([item["expected_date"] for item in found], ["2026-10-01"])
        wider = self.authority.upcoming("2026-09-09", 90)
        self.assertEqual([item["expected_date"] for item in wider],
                         ["2026-10-01", "2026-11-05"])

    def test_upcoming_reads_every_company_at_its_latest_version(self):
        other = "company:sec-cik:0001352010"
        self.authority.publish(
            company_ref=other, entries=[entry("2026-09-20", subject="2026q3")],
            change_reason="evidence_thicker", evidence_refs=["evidence:two"],
            now="2026-09-09",
        )
        found = self.authority.upcoming("2026-09-09", 60)
        self.assertEqual(
            [(item["company_ref"], item["expected_date"]) for item in found],
            [(other, "2026-09-20"), (COMPANY, "2026-10-01"), (COMPANY, "2026-11-05")],
        )


class EventWindowTests(unittest.TestCase):
    """Which windows open, and what stops them opening thirty times."""

    def setUp(self):
        self.written = []
        self.record = lambda **event: self.written.append(event)

    def resolved(self, day, *, confidence="confirmed", kind="earnings",
                 subject="2026q4", entry_ref="catalyst-entry:one"):
        return {
            "entry_ref": entry_ref, "event_kind": kind, "subject": subject,
            "expected_date": day, "confidence": confidence,
            "disagreement": False, "disagreeing_dates": [],
            "sources": [filed_source(day) if confidence == "confirmed"
                        else vendor_source(day)],
            "notes": "",
        }

    def emit(self, entries, now, **kwargs):
        return emit_calendar_events(
            company_ref=COMPANY, entries=entries, record_event=self.record,
            now=now, version_ref="catalyst-calendar-version:one", **kwargs,
        )

    def test_a_confirmed_date_a_month_out_opens_the_preview_window(self):
        emitted = self.emit([self.resolved("2026-10-01")], "2026-09-09")
        self.assertEqual([item["window"] for item in emitted], ["preview"])
        self.assertEqual(emitted[0]["days_until"], 22)
        self.assertEqual(self.written[0]["kind"], "calendar")
        self.assertEqual(self.written[0]["company_ref"], COMPANY)
        self.assertIn("catalyst-calendar-version:one", self.written[0]["source_refs"])

    def test_a_date_further_out_than_the_lead_time_opens_nothing(self):
        self.assertEqual(self.emit([self.resolved("2026-12-01")], "2026-09-09"), [])
        self.assertEqual(self.written, [])
        self.assertEqual(PREVIEW_LEAD_DAYS, 30)

    def test_the_calibration_window_is_the_day_itself_and_two_days_after(self):
        for day, expected in (
            ("2026-10-01", ["calibration"]),
            ("2026-10-03", ["calibration"]),
            ("2026-10-04", []),
        ):
            with self.subTest(today=day):
                self.written.clear()
                emitted = self.emit([self.resolved("2026-10-01")], day)
                self.assertEqual([item["window"] for item in emitted], expected)

    def test_an_estimated_date_does_not_open_a_preview(self):
        # The plan's rule: only a confirmed date drives the preview. Four weeks
        # of an analyst's attention should not be committed to a vendor's guess.
        self.assertEqual(
            self.emit([self.resolved("2026-10-01", confidence="estimated")],
                      "2026-09-09"),
            [],
        )

    def test_a_moved_date_is_emitted_whatever_its_confidence(self):
        emitted = self.emit(
            [self.resolved("2026-12-01", confidence="estimated")], "2026-09-09",
            moved_entry_refs=["catalyst-entry:one"],
        )
        self.assertEqual([item["window"] for item in emitted], ["date_change"])
        self.assertEqual(emitted[0]["confidence"], "estimated")

    def test_an_ex_dividend_never_opens_a_preview(self):
        self.assertEqual(
            self.emit([self.resolved("2026-10-01", kind="ex_dividend")],
                      "2026-09-09"),
            [],
        )

    def test_a_window_that_stays_open_is_recorded_once(self):
        entries = [self.resolved("2026-10-01")]
        seen: set[str] = set()
        first = self.emit(entries, "2026-09-09", is_emitted=seen.__contains__)
        seen.update(item["event_key"] for item in first)
        second = self.emit(entries, "2026-09-10", is_emitted=seen.__contains__)
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(len(self.written), 1)

    def test_a_date_that_moves_reopens_the_window_it_had_closed(self):
        seen = {calendar_event_key(
            COMPANY, "catalyst-entry:one", "preview", "2026-10-01")}
        emitted = self.emit(
            [self.resolved("2026-10-02")], "2026-09-09",
            is_emitted=seen.__contains__,
        )
        self.assertEqual([item["window"] for item in emitted], ["preview"])

    def test_an_entry_ref_is_the_occurrence_and_not_the_date(self):
        self.assertEqual(
            entry_ref_for(COMPANY, "earnings", "2026q4"),
            entry_ref_for(COMPANY, "earnings", "2026q4"),
        )
        self.assertNotEqual(
            entry_ref_for(COMPANY, "earnings", "2026q4"),
            entry_ref_for(COMPANY, "guidance", "2026q4"),
        )


if __name__ == "__main__":
    unittest.main()

"""A profile or encyclopedia page, or a page found again, is not news.

Live 2026-09-24: a ``management-changes`` web search returned Julie Sweet's
Wikipedia, Britannica and Forbes pages; each became a ``news`` event and the
Forbes one a research task asking who had left Accenture
(``event-judgement:f177d942``).  All three had first been found on
2026-09-07; a later mission version found them again.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from dalton_core.research_event import (
    ResearchEventAuthority,
    document_event_candidates,
    evergreen_page_reason,
    record_event,
)
from tests.p14a_fixtures import ACN, AUTOMATION, P14aHarness

NOW = datetime(2026, 9, 24, 13, 0, tzinfo=timezone.utc)


class EvergreenClassifierTests(unittest.TestCase):
    def test_reference_hosts_are_evergreen(self):
        for host in ("en.wikipedia.org", "www.britannica.com", "www.crunchbase.com",
                     "wikipedia.org", "www.linkedin.com"):
            self.assertIsNotNone(evergreen_page_reason(host=host), host)

    def test_a_news_host_is_evergreen_only_for_a_profile_shaped_page(self):
        self.assertIsNone(evergreen_page_reason(host="www.forbes.com"))
        self.assertIsNotNone(evergreen_page_reason(
            host="www.forbes.com", url="https://www.forbes.com/profile/julie-sweet/"))
        self.assertIsNotNone(evergreen_page_reason(
            host="www.forbes.com", title="Julie Sweet - Biography"))
        self.assertIsNone(evergreen_page_reason(
            host="www.forbes.com",
            url="https://www.forbes.com/sites/x/2026/09/20/leadership-transition-at-accenture/"))
        self.assertIsNotNone(evergreen_page_reason(
            host="newsroom.accenture.com", url="https://newsroom.accenture.com/leadership"))

    def test_a_lookalike_host_is_not_matched_by_suffix_alone(self):
        self.assertIsNone(evergreen_page_reason(host="notwikipedia.org.example.com"))
        self.assertIsNone(evergreen_page_reason(host="mywikipedia.org"))


class DocumentEventTests(P14aHarness):
    grants = ("market_event", "observation", "stage_record", "deliverable")

    def _document(self, record_id, document_ref, host, *, created_at,
                  source_ref="source:web-search", spec_ref="management-changes"):
        discovery_id = f"discovery:{record_id}"
        with self.missions._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_source_discoveries("
                "record_id,mission_version_ref,mission_version_hash,company_ref,source_ref,"
                "discovery_plan_ref,discovery_plan_hash,spec_ref,query_hash,connector_invocation_ref,"
                "source_envelope_ref,source_envelope_hash,actor_ref,requested_by,record_json,"
                "content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (discovery_id, self.mission["id"], self.mission["content_hash"], ACN,
                 source_ref, "plan", "0" * 64, spec_ref, "1" * 64,
                 f"invocation:{record_id}", f"envelope:{record_id}", "2" * 64,
                 AUTOMATION, AUTOMATION, "{}", "3" * 64, created_at),
            )
            cur.execute(
                "INSERT INTO coverage_mission_discovered_documents("
                "record_id,mission_version_ref,company_ref,source_ref,document_ref,discovery_ref,"
                "status,created_at,updated_at,host) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (record_id, self.mission["id"], ACN, source_ref, document_ref,
                 discovery_id, "acquired", created_at, created_at, host),
            )

    def refs(self):
        return {row["payload"]["document_ref"] for row in document_event_candidates(
            self.store.connection, company_ref=ACN, mission_ref=self.mission_ref, now=NOW)}

    def test_an_encyclopedia_page_found_today_is_not_an_event(self):
        self._document("wiki", "public-web-url:sha256:" + "a" * 64, "en.wikipedia.org",
                       created_at="2026-09-24T12:55:58+00:00")
        self._document("news", "public-web-url:sha256:" + "b" * 64, "www.reuters.com",
                       created_at="2026-09-24T12:55:58+00:00")
        self.assertEqual(self.refs(), {"public-web-url:sha256:" + "b" * 64})

    def test_an_undated_page_found_again_is_not_new(self):
        forbes = "public-web-url:sha256:" + "c" * 64
        self._document("forbes-old", forbes, "www.forbes.com",
                       created_at="2026-09-07T11:37:24+00:00")
        self.mission = self.grant("market_event")
        self._document("forbes-again", forbes, "www.forbes.com",
                       created_at="2026-09-24T12:55:58+00:00")
        self.assertEqual(self.refs(), set())

    def test_a_dated_sell_side_document_keeps_its_behaviour(self):
        report = "alphaengine-doc:report"
        self._document("report-old", report, None, created_at="2026-09-04T16:15:05+00:00",
                       source_ref="source:alphaengine", spec_ref="sell-side-reports")
        self.mission = self.grant("market_event")
        self._document("report-again", report, None,
                       created_at="2026-09-24T12:30:50+00:00",
                       source_ref="source:alphaengine", spec_ref="sell-side-reports")
        self.assertEqual(self.refs(), {report})

    def test_an_already_recorded_evergreen_event_is_not_sent_to_the_judge(self):
        from dalton_core.event_judgement import EventJudgementAuthority
        from dalton_core.event_judgement_cli import unjudged_event_groups

        events = ResearchEventAuthority(self.store)
        for name, host in (("wiki", "en.wikipedia.org"), ("news", "www.reuters.com")):
            document = f"public-web-url:sha256:{name}"
            record_event(
                events, company_ref=ACN, kind="news",
                occurred_at="2026-09-24T12:55:58+00:00",
                source_refs=["source:web-search", document],
                payload={"document_ref": document, "source_ref": "source:web-search",
                         "spec_ref": "management-changes", "discovery_ref": f"d:{name}",
                         "title": None, "host": host},
                mission=self.mission, actor_ref=AUTOMATION)
        groups = unjudged_event_groups(events, EventJudgementAuthority(self.store),
                                       company_ref=ACN, limit=10)
        hosts = [event["payload"]["host"] for group in groups for event in group]
        self.assertEqual(hosts, ["www.reuters.com"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""P14a: the brain has to know what each source can actually give it."""

from __future__ import annotations

import unittest

from dalton_core.research_event import EVIDENCE_TIERS
from dalton_core.source_capability_map import (
    CAPABILITIES,
    CONTENT_KINDS,
    SourceCapabilityError,
    build_map,
    capability,
    connection_status,
    prompt_table,
    sources_for,
)
from tests.p14a_fixtures import P14aHarness


class TableTests(unittest.TestCase):
    def test_every_declared_source_yields_at_least_one_content_kind(self):
        for slug, entry in CAPABILITIES.items():
            self.assertTrue(entry["content_kinds"], slug)
            for kind in entry["content_kinds"]:
                self.assertIn(kind, CONTENT_KINDS, slug)

    def test_every_declared_tier_is_in_the_frozen_vocabulary(self):
        for slug in CAPABILITIES:
            self.assertIn(capability(slug)["evidence_tier"], EVIDENCE_TIERS, slug)

    def test_an_unknown_slug_is_refused_and_the_message_names_what_is_known(self):
        with self.assertRaises(SourceCapabilityError) as caught:
            capability("bloomberg")
        self.assertIn("bloomberg", str(caught.exception))
        self.assertIn("alphaengine", str(caught.exception))

    def test_an_unknown_content_kind_is_refused(self):
        with self.assertRaises(SourceCapabilityError):
            sources_for("vibes")

    def test_a_source_still_on_another_branch_is_declared_and_marked(self):
        # A map that only knew what had merged could not be used to plan for
        # what is arriving. S1's two connectors have since landed; S3's
        # employee reviews have not, and the flag says which is which.
        for slug in ("sales-notes", "company-wiki"):
            self.assertTrue(capability(slug)["in_inventory"], slug)
        pending = capability("employee-reviews")
        self.assertFalse(pending["in_inventory"])
        self.assertTrue(pending["content_kinds"])
        self.assertTrue(capability("catalyst-calendar")["content_kinds"])

    def test_a_merged_connector_carries_its_inventory_facts(self):
        entry = capability("alphaengine")
        self.assertTrue(entry["in_inventory"])
        self.assertEqual(entry["source_ref"], "source:alphaengine")
        self.assertEqual(entry["transport"], "mcp_managed")
        self.assertIn("search_library", entry["operations"])
        self.assertTrue(entry["quotas"])

    def test_the_completeness_ceiling_is_the_weakest_operation_not_the_best(self):
        # A connector one of whose operations only samples cannot be relied on
        # for "all of them".
        self.assertIn(capability("sec")["completeness_ceiling"],
                      {"enumerated", "bounded", "sampled"})

    def test_specific_sources_come_before_the_two_that_answer_everything(self):
        order = sources_for("news")
        self.assertLess(order.index("alphaengine"), order.index("gemini-web-search"))
        self.assertTrue(capability("gemini-web-search")["generic"])
        self.assertTrue(capability("web-fetch")["generic"])
        self.assertFalse(capability("alphaengine")["generic"])

    def test_the_owner_market_view_sources_answer_for_crowd_and_vendor_content(self):
        self.assertIn("x-xreach", sources_for("crowd_post"))
        self.assertIn("sales-notes", sources_for("sales_note"))
        self.assertIn("guidepoint", sources_for("expert_excerpt"))

    def test_the_map_is_content_hashed_and_deterministic(self):
        first, second = build_map(), build_map()
        self.assertEqual(first["content_hash"], second["content_hash"])
        self.assertEqual(len(first["content_hash"]), 64)


class MissionTests(P14aHarness):
    def test_every_connected_source_in_the_live_mission_maps_to_a_content_kind(self):
        status = connection_status(self.mission)
        connected = [slug for slug, value in status.items() if value == "connected"]
        self.assertTrue(connected)
        for slug in connected:
            self.assertTrue(capability(slug)["content_kinds"], slug)

    def test_the_web_search_alias_resolves_to_the_gemini_connector(self):
        # The mission's plan says source:web-search; the connector's own ref is
        # source:public-web. Nothing outside discovery kept that mapping, and
        # without it this would read "undeclared" -- the mission never asked --
        # rather than the status the mission actually states.
        status = connection_status(self.mission)
        self.assertEqual(status["gemini-web-search"], "not_connected")

    def test_a_source_the_mission_never_asked_for_is_undeclared_not_missing(self):
        status = connection_status(self.mission)
        self.assertEqual(status["xueqiu"], "undeclared")
        self.assertEqual(status["guidepoint"], "not_connected")

    def test_the_prompt_table_is_short_and_names_the_generic_sources(self):
        projection = build_map(mission=self.mission)
        connected = prompt_table(projection)
        self.assertLessEqual(len(connected.splitlines()), 13)
        self.assertIn("sec", connected)
        whole = prompt_table(projection, connected_only=False)
        self.assertIn("alphaengine", whole)
        self.assertIn("(generic)", whole)

    def test_the_cadence_baseline_folds_into_the_map_when_it_is_given_one(self):
        from dalton_core.tracking_cadence import load_policy

        policy = load_policy()
        projection = build_map(mission=self.mission, cadences=policy["cadences"])
        alphaengine = next(row for row in projection["sources"]
                           if row["slug"] == "alphaengine")
        self.assertEqual(alphaengine["cadence_baseline_seconds"], 43200)
        self.assertTrue(alphaengine["cadence_adjustable"])
        prices = next(row for row in projection["sources"] if row["slug"] == "yfinance")
        self.assertFalse(prices["cadence_adjustable"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

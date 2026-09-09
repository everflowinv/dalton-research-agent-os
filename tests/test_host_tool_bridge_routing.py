"""P13af: which host tool a call reaches is decided by (source, operation).

The bridge registry was keyed by operation name, with an assert that operation
names were unique across bridges. That assert was load-bearing -- it is what
stopped a call being routed to another provider's tool -- but its premise
expired the moment a second authenticated library appeared. AlphaEngine exposes
``search_library``; so does Guidepoint. They are different tools, on different
sources, under different owner approvals.

Keying on the pair keeps the guarantee without making the uniqueness of
operation names load-bearing. These tests are about the guarantee, not the
lookup: a call must never reach a tool its source does not own.
"""

from __future__ import annotations

import unittest

from dalton_core.connector_runner import RunnerValidationError
from dalton_core.live_mcp_connector import (
    _BRIDGES,
    HostToolBridge,
    host_tool_bridge_for,
    host_tool_bridge_for_operation,
)


class RoutingTests(unittest.TestCase):
    def test_one_operation_name_on_two_sources_reaches_two_bridges(self):
        alpha = host_tool_bridge_for("source:alphaengine", "search_library")
        guidepoint = host_tool_bridge_for("source:guidepoint", "search_library")
        self.assertNotEqual(alpha.bridge_ref, guidepoint.bridge_ref)
        self.assertNotEqual(alpha.bridge_hash, guidepoint.bridge_hash)
        self.assertNotEqual(alpha.credential_slot_ref, guidepoint.credential_slot_ref)
        self.assertNotEqual(alpha.template_key, guidepoint.template_key)

    def test_a_source_cannot_reach_an_operation_it_does_not_own(self):
        # The failure the pair key exists to prevent.
        for source, operation in (
            ("source:public-web", "get_transcript"),
            ("source:public-web", "search_library"),
            ("source:guidepoint", "get_document"),
            ("source:alphaengine", "get_transcript"),
        ):
            with self.subTest(source=source, operation=operation):
                with self.assertRaises(RunnerValidationError):
                    host_tool_bridge_for(source, operation)

    def test_an_unknown_source_or_operation_is_refused(self):
        for source, operation in (
            ("source:not-a-source", "search_library"),
            ("source:alphaengine", "delete_everything"),
            (None, "search_library"),
            ("source:alphaengine", None),
        ):
            with self.subTest(source=source, operation=operation):
                with self.assertRaises(RunnerValidationError):
                    host_tool_bridge_for(source, operation)

    def test_resolving_by_operation_alone_refuses_when_it_is_ambiguous(self):
        # Picking one is exactly the failure being prevented, so it refuses.
        with self.assertRaises(RunnerValidationError):
            host_tool_bridge_for_operation("search_library")

    def test_resolving_by_operation_alone_still_works_when_it_is_not(self):
        self.assertEqual(
            host_tool_bridge_for_operation("search_web").source_ref, "source:public-web")
        self.assertEqual(
            host_tool_bridge_for_operation("get_document").source_ref, "source:alphaengine")

    def test_every_bridge_declares_the_source_it_speaks_for(self):
        for bridge in _BRIDGES:
            with self.subTest(bridge=bridge.bridge_ref):
                self.assertTrue(bridge.source_ref.startswith("source:"))

    def test_a_bridge_source_matches_the_template_it_names(self):
        # A bridge whose declared source disagrees with its own template would
        # route correctly by the key and wrongly by every other check.
        from dalton_core.connector_inventory import load_packaged_connector_inventory

        templates = load_packaged_connector_inventory()["templates"]
        for bridge in _BRIDGES:
            with self.subTest(bridge=bridge.bridge_ref):
                template = templates[bridge.template_key]
                self.assertEqual(
                    template["source_identity"]["source_ref"], bridge.source_ref)

    def test_a_bridge_only_names_operations_its_template_freezes(self):
        from dalton_core.connector_inventory import load_packaged_connector_inventory

        templates = load_packaged_connector_inventory()["templates"]
        for bridge in _BRIDGES:
            frozen = {item["operation"]
                      for item in templates[bridge.template_key]["operations"]}
            with self.subTest(bridge=bridge.bridge_ref):
                self.assertLessEqual(set(bridge.tool_names), frozen)

    def test_the_registry_refuses_to_hold_a_duplicate_pair(self):
        # The assert that replaced the operation-uniqueness one. If two bridges
        # ever claim the same (source, operation) the module must not import.
        pairs = [(bridge.source_ref, operation)
                 for bridge in _BRIDGES for operation in bridge.tool_names]
        self.assertEqual(len(pairs), len(set(pairs)))

    def test_a_bridge_is_immutable(self):
        bridge = host_tool_bridge_for("source:guidepoint", "get_transcript")
        with self.assertRaises(Exception):
            bridge.source_ref = "source:alphaengine"  # type: ignore[misc]
        self.assertIsInstance(bridge, HostToolBridge)


if __name__ == "__main__":
    unittest.main()

"""Every purpose a module registers has a model tier.

A purpose without a tier is refused the day its policy pins a chain, silently
fine until then (INT2's finding). Import the modules that register purposes and
assert the deployment check comes back empty.
"""

from __future__ import annotations

import importlib
import unittest

REGISTERING_MODULES = (
    "dalton_core.claim_index_tagging",
    "dalton_core.research_quality_score",
    "dalton_core.event_judgement",
    "dalton_core.company_dossier_draft",
    "dalton_core.debate_map_draft",
)


class PurposeTierCoverageTests(unittest.TestCase):
    def test_every_registered_purpose_has_a_tier(self) -> None:
        from dalton_core import lane_registry
        from dalton_core.model_fallback_chain import unmapped_purposes

        lane_registry.load_lanes()
        for name in REGISTERING_MODULES:
            importlib.import_module(name)
        self.assertEqual(unmapped_purposes(), ())

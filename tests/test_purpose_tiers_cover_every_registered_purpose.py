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
    "dalton_core.industry_framework_draft",
)


class PurposeTierCoverageTests(unittest.TestCase):
    def test_every_purpose_a_real_module_registers_has_a_tier(self) -> None:
        # Other tests register throwaway purposes without a tier on purpose,
        # so this reads the modules' own purpose constants rather than the
        # process-global registry.
        from dalton_core.model_fallback_chain import purpose_tiers

        registered: set[str] = set()
        for name in REGISTERING_MODULES:
            module = importlib.import_module(name)
            for attr in dir(module):
                if attr.endswith("PURPOSE") and isinstance(getattr(module, attr), str):
                    registered.add(getattr(module, attr))
        self.assertTrue(registered)
        self.assertEqual(sorted(registered - set(purpose_tiers())), [])

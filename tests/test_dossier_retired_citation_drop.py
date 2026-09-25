"""2026-09-25: IBM v10 emptied four sections and called it verification.

business_model, supply_and_cost, management_and_capital_allocation and
history_of_price_drivers were carried-forward units citing Claims retired
since; they were dropped and labelled ``refused_by_verification``.  They now
carry ``retired_citation_dropped`` and are redrafted ahead of extensions.
"""

from __future__ import annotations

import unittest

from dalton_core.company_dossier import UNAVAILABLE_REASONS
from dalton_core.company_dossier_cli import (
    RETIRED_CITATION_DROPPED, stale_units, unavailable_section)
from dalton_core.research_gap_display import _DISPLAY_TERMS


def entry(unit, *, new_refs, last_drafted="2026-09-14T11:45:35+00:00", **extra):
    return {"unit": unit, "status": "ready", "reason": None, "structure": [],
            "material": [{"ref": "c"}], "new_refs": new_refs, "retired_refs": [],
            "stale": True, "last_drafted": last_drafted, **extra}


class RetiredCitationDropTests(unittest.TestCase):
    def test_the_reason_is_its_own_and_is_displayable(self):
        self.assertIn(RETIRED_CITATION_DROPPED, UNAVAILABLE_REASONS)
        self.assertEqual(unavailable_section("business_model", RETIRED_CITATION_DROPPED)["reason"],
                         "retired_citation_dropped")
        self.assertIn("撤回", _DISPLAY_TERMS[RETIRED_CITATION_DROPPED])

    def test_a_dropped_unit_is_redrafted_before_a_richer_extension(self):
        plan = {
            "segments_and_mix": entry("segments_and_mix", new_refs=70),
            "business_model": entry("business_model", new_refs=3,
                                    retired_citation_dropped=True),
        }
        self.assertEqual(stale_units(plan, limit=1), ["business_model"])
        plan["business_model"]["retired_citation_dropped"] = False
        self.assertEqual(stale_units(plan, limit=1), ["segments_and_mix"])

    def test_a_dropped_unit_with_nothing_new_is_still_redrafted(self):
        plan = {"supply_and_cost": entry("supply_and_cost", new_refs=0,
                                         retired_citation_dropped=True)}
        self.assertEqual(stale_units(plan, limit=3), ["supply_and_cost"])


if __name__ == "__main__":
    unittest.main()

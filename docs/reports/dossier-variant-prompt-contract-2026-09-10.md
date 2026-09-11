# Dossier variant prompt contract alignment

The dossier producer prompt asked for the file's variant conclusion but did not state the Constitution's deterministic ban on investment conclusions. Actual ACN and DXC drafts passed independent verification and were then correctly refused because `variant_view` used the forbidden valuation phrase `低估`.

The variant-only prompt now permits cited disagreement with market expectations while explicitly forbidding valuation, recommendation, position-change, and price-target conclusions using the checker's existing closed vocabulary. The deterministic checker is unchanged. The draft contract advances to `0.3`, so the company-scoped lane signature can reconsider the held variant input once. Other unit prompts do not gain this rule.

Formal v0.3 dossier publication remains backward compatible: the authority accepts the exact legacy v0.2 variant producer question when its immutable WorkOrder, prompt hash, output, route, and verifier proof all resolve. New input reconstruction uses the current prompt and therefore marks the old variant input stale without rewriting its historical provenance.

Validation: `tests.test_company_dossier_draft`, `tests.test_company_dossier`, `tests.test_dossier_unit_provenance`, and `tests.test_dossier_lane` passed 169 tests. No live state, hold, model call, or authority was changed.

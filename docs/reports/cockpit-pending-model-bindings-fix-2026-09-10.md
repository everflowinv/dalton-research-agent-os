# Cockpit pending model bindings fix — 2026-09-10

The Cockpit now resolves `model_spec`, `debate_map`, and `conviction_call` from
the shared `initial-screen-model-config.json` their installed mission launchers
actually pass. They no longer appear unconfigured while running through that
policy.

The industry-framework verifier now prefers the installed paired dossier
verifier configuration and retains the legacy verifier filename as a fallback.
The model-selection registry uses the same order, so display and launch agree.

The current street-estimate lane is deterministic and is displayed as such,
with no editable model selection. `quality_verifier` remains truthfully
unconfigured because the deployed quality CLI does not inject a verifier.

Validation: `PYTHONPATH=src python3 -m unittest tests.test_industry_framework_lane tests.test_model_selection`
passed 110 tests.

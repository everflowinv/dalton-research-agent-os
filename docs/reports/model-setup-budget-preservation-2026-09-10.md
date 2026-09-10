# Model setup budget preservation — 2026-09-10

The model-config write-path audit found two factories: `document_extraction_setup.install` writes the extraction config directly, while `research_planner_setup.install` writes the planner config and every role config created through `install_role` (deliverable, dossier/deep-insight, industry, earnings, event, zero-base, and verifier files). `deliverable_model_setup` delegates to that role factory and has no separate write path. `cockpit_setup` only points at an existing extraction config.

Both factories now read an existing target before replacement and preserve only these owner-controlled blocks:

- `call_budget`
- `purpose_call_budgets`
- `run_budget`
- `purpose_run_budgets`

The common reader validates every retained block with the central call/run budget validators. Invalid existing budget data refuses installation before the target file is written. Unknown top-level fields are not copied, so this does not preserve accidental fields or secret material. Reinstalling the same role or switching its tier/profile updates routing and credential slots while retaining budget blocks exactly.

Validation: `PYTHONPATH=src python3 -m unittest tests.test_document_extraction_setup tests.test_research_planner_setup tests.test_deployment_model_pairs tests.test_call_budget` passed 43 tests. The regressions cover direct extraction setup, the shared role factory, unknown-field removal, and fail-closed invalid budget state. No live installation or model call was performed.

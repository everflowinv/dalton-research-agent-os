# Cockpit Product Status Projection Fix

Date: 2026-09-10

Cockpit activity previously inspected only the generic child `status` and
DebateMap's `map_status`. Several product CLIs intentionally exit successfully
after persisting a refused or failed formal outcome. In particular, an event
judgement could report `status=succeeded, judgement_status=refused` and appear as
a completed product.

The activity projection now checks the known product outcome fields for DebateMap,
event judgement, dossier, memo, framework, Deep Insight gate, deliverable,
forecast, and sensitivity runs. Closed refusal, failed-verification, unavailable,
independence, binding-drift, and governance-gated outcomes display as failed even
when the child process exited zero. Operational or accounting fields such as
`cost_status` are deliberately outside this classification.

Validation:

`PYTHONPATH=src python3 -m unittest tests.test_cockpit_plane tests.test_cockpit_research_library_authorities`

Result: 16 tests passed, 1 skipped. The regression covers event, dossier,
framework, gate, memo, and DebateMap product outcome fields.

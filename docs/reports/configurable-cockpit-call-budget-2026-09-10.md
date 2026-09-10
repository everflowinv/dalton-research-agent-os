# Configurable Cockpit model-call budgets — 2026-09-10

`CockpitModel` now resolves a fresh effective budget for each purpose and call.
Purpose overrides take precedence over general overrides, which take precedence
over the packaged purpose baseline or the caller's legacy defaults. The model
object is not mutated, so concurrent purposes cannot leak budgets into one
another.

The effective input, output, cost and timeout bounds reach the WorkOrder,
router admission, day-ledger reservation, fallback-chain ceiling, adapter
timeout and scheduler lease. A configured or packaged change from the legacy
constructor budget contributes a canonical fingerprint to work identity;
unchanged legacy callers keep their old identity.

Focused validation passed 116 relevant tests (`test_call_budget`,
`test_cockpit_plane`, `test_document_extraction`, and `test_model_selection`),
with one existing Cockpit fixture skip. The integration branch's event route
test is not present on this older isolated branch and was therefore not run
here.

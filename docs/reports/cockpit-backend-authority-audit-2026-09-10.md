# Cockpit backend authority audit

Date: 2026-09-10

This review compared the current integration Cockpit GET projections and POST
handlers with their actual consumers, then exercised the deployed state through
read-only paths. It did not submit a Cockpit POST or modify live configuration.

The live budget projection correctly reads 59 settled/reserved model calls and
$15.0907 against the active mission's 9,000-call/$100 caps. Pool balances come
from the day ledger. AlphaEngine has no stale measured budget in the current
heartbeat, so the page correctly falls back to the active mission's 130-call
24-hour cap. No AlphaEngine entry remains in the persistent dependency,
terminal, or permission projections. The former lower cap is therefore not
being presented as a permanent block.

Model selection bindings resolve per purpose and per actual router database.
The writer's selection operation repoints registered configuration files and
retains their credential slots transactionally. Call/run budget POSTs use a
configuration hash and the same purpose-specific blocks read by consumers.
One real mismatch remained: `document_extraction` was editable but its GET view
returned `effective: null` because it is intentionally absent from the shared
default catalogue. A partial save then validated against the catalogue-wide
fallback rather than the extractor's real legacy baseline, so untouched token
and timeout fields could silently change.

The budget projection now resolves the three extraction purposes against the
same exported `LEGACY_CALL_BUDGET` dictionaries their WorkOrder builders use.
The setter validates partial changes against that effective baseline. The live
document extraction view consequently has concrete 16,000 input tokens, 3,000
output tokens, $0.05, and 60 seconds before an owner changes any field.

The second mismatch was process/product status. The activity log treated any
exit-zero child as completed work. An exit-zero wrapper whose summary carried
`refused`, `unverified`, `model_unavailable`, or another closed product failure
therefore appeared successful. The projection now marks those summaries failed;
normal idle/duplicate/succeeded summaries remain completed rather than being
invented as failures.

Validation:

- `PYTHONPATH=src python3 -m unittest tests.test_model_budget_configuration tests.test_cockpit_plane tests.test_document_extraction tests.test_document_numeric_extraction tests.test_metric_discovery_extraction`
  — 78 passed, one existing skip.


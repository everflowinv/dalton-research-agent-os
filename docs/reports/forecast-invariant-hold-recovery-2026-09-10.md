# Forecast invariant hold recovery — 2026-09-10

## Observed failure

The live CTSH forecast summary at
`model-forecast-runs/407b377b89b0bd23aaf08c82/summary.json` remained held
after deployment of the corrected segment-sum evaluator. Its durable failure
key and child ticket were derived only from the company and model-input digest.
The model inputs had not changed, so the corrected validator could never run.

The old refusal treated `srt:ConsolidationItemsAxis` as an additive segment
axis. The current authority projection contains lossy legacy rows with no
`dimension_count` proof; the corrected contract does not evaluate those rows
as a segment sum. This diagnosis used read-only state access. No live record
was changed.

## Correction

The economic-invariant layer now exports a closed, explicit validator contract
reference and content hash. The forecast lane binds that hash into only the
durable failure business key and child ticket identity. The published forecast
model digest remains byte-for-byte governed by the existing model inputs.

Consequences:

- a refusal recorded under the obsolete validator identity does not block one
  run under the corrected identity;
- an unchanged failure under the current validator stays held;
- an already-current published model is still controlled by the existing
  `pending_companies` rules and is not regenerated merely because code changed;
- a future validator semantic change requires an explicit contract version
  change. Arbitrary deploys do not grant retries.

Legacy tickets without a validator hash are settled without manufacturing a
new failure record. If their company remains pending, the current hashed ticket
is then eligible once; their old ticket and summary bytes remain intact.

## Validation

`PYTHONPATH=src python3 -m unittest tests.test_company_model_forecast tests.test_economic_invariants tests.test_model_forecast_driver tests.test_mission_model_forecast_lane`

Result: 182 tests passed. The recovery test proves identical model inputs resume
under a changed validator contract, while the existing current-contract hold,
new-spec recovery, dependency probe, starvation, and successful publication
tests remain green.

No live run, model call, source fetch, authority mutation, or deployment was
performed.

# Event judgement fairness recovery — 2026-09-10

## Failure

The coordinator selected the globally newest unjudged event without applying
the active mission or universe. After a child returned zero judgements and
refused its batch, the process-local `_last_batch` suppressed that same newest
event forever. No later company could reach the child, and a writer restart
forgot the suppression and could pay for the same refusal again.

The child also selected its own global batch. A coordinator could name one
event while the child spent budget on another.

## Correction

Selection now produces explicit event groups within the current mission family,
current universe, and already tracked companies. Each group identity binds the
mission version, company, grouping key, complete incremental evidence hash, and
model/verifier configuration signature. The coordinator passes the selected
company and event to the child, which filters to the exact group containing
that event before any model call.

Failures use the persistent `LaneFailureBudget` per group. Content refusals are
terminal for that exact evidence/configuration identity. Dependency and
transient failures retain the shared probe and bounded retry behavior. Held
groups are skipped, so a refused newest event cannot starve another company.
A changed group evidence hash or configuration gets a new bounded identity;
an unchanged refusal does not create a paid loop across writer restarts.

Existing event grouping remains unchanged: US monthly buyback disclosures,
closed HK ISO weeks, and individual events retain their current evidence
semantics.

## Validation

`PYTHONPATH=src python3 -m unittest tests.test_mission_event_judgement_lane tests.test_event_judgement_config_retry tests.test_event_judgement`

Result: 145 tests passed. Added coverage proves mission and universe exclusion,
newest-refusal fairness across companies, persistent restart holds, bounded
busy retries, exact child group selection with zero model calls, configuration
recovery, and launcher argv binding.

No live state was changed and no model or network call was made.

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
Mission-family filtering happens in SQL before rows are grouped, so a foreign
mission's event cannot contaminate or suppress a valid current group. The
coordinator reads one newest group per eligible company; it does not introduce
an unconfigured sentinel scan cap. The child receives the complete selected
member refs and group hash and revalidates them against the newest current
group. A late member therefore produces a quiet zero-cost stale-ticket result,
then a new group identity on the next tick.

Failures use the persistent `LaneFailureBudget` per group. Content refusals are
terminal for that exact evidence/configuration identity. Dependency and
transient failures retain the shared probe and bounded retry behavior. Held
groups are skipped, so a refused newest event cannot starve another company.
A changed group evidence hash or configuration gets a new bounded identity;
an unchanged refusal does not create a paid loop across writer restarts.
If a writer restarts after an exact child finished but before its settlement
tick, the event launcher adopts that persisted ticket without truncating its
log or spawning the child again; the next tick records its durable outcome.

Existing event grouping remains unchanged: US monthly buyback disclosures,
closed HK ISO weeks, and individual events retain their current evidence
semantics.

## Validation

`PYTHONPATH=src python3 -m unittest tests.test_mission_event_judgement_lane tests.test_event_judgement_config_retry tests.test_event_judgement`

Result: 148 tests passed. Added coverage proves mission and universe exclusion,
newest-refusal fairness across companies, persistent restart holds, bounded
busy retries, exact child group selection with zero model calls, configuration
recovery, launcher argv binding, foreign-row filtering before grouping,
membership-drift refusal, and finished-ticket adoption across restart.

No live state was changed and no model or network call was made.

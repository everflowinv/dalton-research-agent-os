# F14 fingerprint lanes — Batch B

Date: 2026-09-10  
Baseline: `4c28816`  
Branch: `f14-fingerprint`

## Delivered

The claim-index, debate-map, conviction, model-spec, model-forecast, and
sensitivity coordinators now use `LaneFailureBudget` with their existing
business fingerprint as the item key. The writer passes its state directory,
so dependency, content-refusal, and permission decisions survive process
restart in the shared append-only failure ledger.

Settled child status and failure reason are classified at the coordinator
boundary. Dependency failures park without spending the transient allowance;
the next admitted run is a real child probe of the same digest, and success
clears every item in that lane waiting on the recovered dependency. Content
refusals remain terminal only for the exact input digest. Unknown child deaths
use the bounded transient allowance. Governance refusals remain
`not_permitted`. A blocked candidate is skipped while later candidates remain
eligible.

Ticks with no pending business input do not call the failure budget. Successful
children clear recoverable state but cannot clear a content-terminal verdict;
changed business input naturally uses a new item key.

## Validation

Focused command:

```text
PYTHONPATH=src python3 -m unittest \
  tests.test_fingerprint_lane_failure_budget \
  tests.test_claim_index_lane tests.test_debate_map_lane \
  tests.test_conviction_call_lane tests.test_mission_model_spec_lane \
  tests.test_mission_model_forecast_lane tests.test_mission_sensitivity_lane
```

Result: **156 tests passed in 4.401 seconds**.

The added coverage verifies each lane's actual settled-child dependency path,
same-digest probe admission, durable replay after restart, success recovery,
content refusal behavior, transient accounting, permission separation, and
item isolation.

## Integration note

This branch intentionally does not modify the shared classifier or ledger.
Main commit `2700728` contains the independently prepared replay ordering and
read-only replay correction and can be merged separately by the integrator.

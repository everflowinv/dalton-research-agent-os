# F14 Batch A: whole-ledger coordinator failure ledgers

Date: 2026-09-10  
Branch: `f14-ledger`  
Base: main `4c28816`

## Result

`mission_dossier_lane`, `mission_deep_insight_lane`, and `mission_industry_framework_lane` now use the shared `LaneFailureBudget` backed by the state directory's append-only lane failure ledger. Their item key remains the lane's existing whole-ledger signature.

Actual failed children are classified from their recorded status and reason. Dependency outages park without spending the transient attempt count, survive coordinator restart, and admit the same business signature as a real probe; a successful probe clears every item parked on that dependency. Content refusals are terminal for that exact signature, while changed Claims, dossier/debate heads, filings, or framework heads produce a new eligible signature. Unmapped transient failures retain the shared three-attempt bound.

Healthy quiet outcomes still set only `_quiet_signature` and append no failure event. Industry-framework quiet outcomes are recognized even when the child process summary itself says `failed` or `held`, which preserves `no_driver_pack` and `causal_chain_unmapped` as current-state descriptions.

Authorization and policy refusals use `not_permitted`, separate from the dependency backlog. Their permission key binds the unchanged business signature to the active mission pointer, Core `governance_policy_pointer`, and relevant launcher policy/model configuration contents. A control change clears obsolete permission events before retry, so cockpit `permission_count` does not retain stale work. The business signature itself is not replaced by a generic digest.

No shared helper was added. Integration should place this commit after the shared replay-order fix `2700728`; the APIs used here are unchanged by it.

## Validation

```
PYTHONPATH=$PWD/src python3 -m unittest \
  tests.test_f14_whole_ledger_lanes \
  tests.test_dossier_lane \
  tests.test_deep_insight_gate_lane \
  tests.test_industry_framework_lane
```

```
Ran 140 tests in 7.504s

OK
```

## Scope

Only the three assigned coordinators, their corresponding tests, the focused cross-lane test, and this report changed. No live state, mission, policy, connector contract, install wiring, or deployment was changed.

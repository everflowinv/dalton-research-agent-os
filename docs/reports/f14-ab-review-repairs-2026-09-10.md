# F14 Batch A/B cross-review repairs

Date: 2026-09-10  
Baseline: `a31433e`  
Branch: `f14-review-repairs`

## Result

The three whole-ledger coordinators now clear a parked dependency when its successful probe legitimately returns a quiet outcome. Superseded authorization projections are retired across the lane when either the business signature or its mission, policy, or model configuration changes; retirement removes only that item and does not claim that a shared dependency recovered.

The six company/subject fingerprint coordinators now keep authorization failures on a control-aware permission key rather than the pure business key. The control fingerprint covers the canonical mission, the active coverage-mission and governance-policy pointers, and launcher file-backed policy/model configuration. Cleanup is scoped to the same company or subject, so reevaluating one company cannot release another company's permission hold.

The shared classifier gets first refusal on every child result. A `gated` result containing `model_unavailable`, quota, or transport vocabulary remains a dependency outage. Only governance outcomes (`gated`, `not_authorized`, `no_checkpoint`, `no_policy`, and real producer “does not grant” text) become `not_permitted`. Refusal statuses become content-terminal only after dependency and governance classification, preserving the exact-input boundary for ordinary content and economic-invariant refusals.

This branch uses the shared `LaneFailureBudget.retire` API from `94259c9`. The reusable A/B helper is `dalton_core.lane_permission_control`: `permission_key`, `current_permission`, `clear_obsolete_permissions`, `record_controlled_failure`, and `authority_connection`.

## Validation

The focused command covering all nine coordinators and the helper ran 299 tests successfully. Added regressions cover quiet dependency-probe recovery, business-signature supersession, policy-pointer recovery, company-scoped permission cleanup, real forecast/sensitivity missing-write-scope wire values, and gated dependency precedence. `py_compile` for the helper and nine coordinators passed, as did `git diff --check`.

The main integration run owns the repository-wide suite.

# F14 failure-ledger follow-up inventory

Date: 2026-09-10  
Baseline: `cea05be` (`main` when this inventory began)  
Scope: read-only inventory; no runtime code changed

## Result

The current tree has **13** coordinators with a process-local failure hold or cooldown that are not wired to `LaneFailureBudget`. This is lower than the old estimate of 16. `mission_feed_lane` and `mission_guidepoint_lane` contain acquisition-failure words but no coordinator signature hold, so they are not counted. Document extraction is also excluded because F15 now uses the shared budget and ledger.

An unchanged input is usually a healthy resting state. It must remain `idle`, `duplicate`, or the lane's explicit quiet status and must not enter the failure ledger. Only a child failure/refusal currently copied into `_failed`, or the crowd-source failure cooldown, belongs in F14.

## Exact remaining coordinators

| Module | Current hold key | What changes the key / recovers work | Unchanged input |
| --- | --- | --- | --- |
| `mission_claim_index_lane.py` | `company_ref + batch_digest` | pending canonical claim-version refs change | normal idle when no unindexed claims |
| `mission_debate_map_lane.py` | `subject_ref + evidence_fingerprint` | canonical evidence refs change | normal when current map fingerprint matches |
| `mission_conviction_lane.py` | `company_ref + evidence_fingerprint` | `gate_inputs` / evidence fingerprint changes | normal when a call already carries that fingerprint |
| `mission_model_spec_lane.py` | `company_ref + state_hash` | company model state changes, including filed concepts/disclosure structure | normal when no company needs a new spec |
| `mission_model_forecast_lane.py` | `company_ref + model_digest` | spec or normalized model-input table changes | normal when every model is current |
| `mission_sensitivity_lane.py` | `company_ref + projection_digest` | forecast model or consensus bridge inputs change | normal when projections match their models |
| `mission_dossier_lane.py` | whole-ledger `ledger_signature` | Claim count/newest timestamp, dossier heads, or claim-index count changes | `_quiet_signature` for `nothing_new`, no mission/company/index, or not authorized is normal and must stay out of failure ledger |
| `mission_deep_insight_lane.py` | whole-ledger `ledger_signature` | dossier/debate/gate state or pending decision changes | `_quiet_signature` after a completed non-relaunch outcome is normal |
| `mission_industry_framework_lane.py` | whole-ledger `ledger_signature` | relevant Claim/framework ledger heads change | its declared quiet statuses are normal |
| `mission_reflection_lane.py` | `iso_week + inputs_hash` | closed week or reflection inputs change | `already_recorded` is an explicit `duplicate`, not a failure |
| `mission_zero_base_lane.py` | `mode + batch_ref` | review due-set (`trigger:period:company`) or outcome-check digest changes | no due reviews and unchanged checks digest is normal idle |
| `mission_research_task_lane.py` | failed ticket timestamp; idle signature is plan plus admitted-task/template counts | failure hold expires after `FAILURE_HOLD`; idle hold releases when plan/task/template state changes or `IDLE_HOLD` expires | unchanged plan/admission signature is a deliberate quiet hold, not a failure |
| `mission_crowd_source_lane.py` | `source_ref + company_ref`, with remaining cooldown ticks | cooldown ages out; any runner governance content hash change clears all cooldowns | no job for a source/company and disconnected-source gating are state descriptions, not failed work |

## Three independent implementation batches

### Batch A — whole-ledger signature lanes

`mission_dossier_lane`, `mission_deep_insight_lane`, `mission_industry_framework_lane`.

These share the same coordinator shape: `_quiet_signature` remains local and outside the failure ledger; `_failed[signature]` becomes a ledger-backed `LaneFailureBudget`. Key tests for each:

1. A dependency-class child failure parks without consuming transient attempts and appears in the ops backlog.
2. The identical ledger signature remains blocked according to its class after coordinator restart.
3. A changed ledger signature is admitted immediately; an unchanged quiet result remains `idle` and writes no failure event.

### Batch B — company/subject fingerprint lanes

`mission_claim_index_lane`, `mission_debate_map_lane`, `mission_conviction_lane`, `mission_model_spec_lane`, `mission_model_forecast_lane`, `mission_sensitivity_lane`.

Use the existing compound key as `item_key`; do not replace its domain digest with a generic hash. Key tests:

1. A transport/model dependency outage for company A does not consume retry budget and does not block eligible company B.
2. Content refusal is terminal for the exact old digest, while a changed evidence/state/model/projection digest is eligible.
3. Transient failures retain the existing bounded retry count. A current published output with the same fingerprint remains normal idle and creates no ledger row.
4. For forecast and sensitivity, `unavailable:economic_invariants` remains content-terminal for that exact digest and becomes eligible only when model inputs change.

### Batch C — cadence and cooldown lanes

`mission_reflection_lane`, `mission_zero_base_lane`, `mission_research_task_lane`, `mission_crowd_source_lane`.

These need adapters around their existing time semantics rather than one mechanical replacement. Key tests:

1. Reflection: same week/input failure replays after restart; a new `inputs_hash` or week is eligible; `already_recorded` writes no failure.
2. Zero base: same review/check batch replays; a changed due-set or checks digest is eligible; no due work remains idle.
3. Research task: classify the failed child separately from the unchanged-plan idle hold. Dependency recovery may retry immediately; the normal idle signature still obeys `IDLE_HOLD` without appearing in ops.
4. Crowd source: dependency outages park by named source and resume on a successful source probe; transient errors preserve the current finite cooldown; governance-hash change clears the affected authorization/configuration state. A source with no job writes no failure.

## Boundary to preserve

The shared ledger should record failures, recovery, and terminal content decisions. It should not become a history of every scheduler no-op. In particular, `_quiet_signature`, `already_recorded`, `nothing_new`, no pending company, no due review, queue drained, and “no job for this source” are evidence that the scheduler is current, not evidence that work failed.

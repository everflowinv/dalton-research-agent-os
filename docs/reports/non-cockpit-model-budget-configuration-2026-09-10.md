# Non-Cockpit model budget configuration — 2026-09-10

The bounded planner now accepts optional `bounded_planner.config.planner_call_budget`. Its legacy baseline remains 16,000 input tokens, 1,200 output tokens, the existing `planner_max_cost_usd`, and 180 seconds. The legacy service cost is installed as the resolver's explicit general baseline before the new override, so the packaged `plan` default cannot replace an operator's existing cost setting. The resolved values are sent unchanged to the writer's `llm_planner_execute` operation.

Thesis-impact assessment and verification use a budget-only state file, `thesis-impact-budget-config.json`. It admits only `call_budget` and `purpose_call_budgets`, with purpose keys `thesis_impact_assessment` and `thesis_impact_verifier`. The writer-side coordinator reads and validates it at WorkOrder construction time. Missing file preserves the exact legacy budgets; malformed content refuses before enqueue. Explicit budget fingerprints enter each phase's immutable identity and metadata. Token, cost, and execution-time limits are carried by the WorkOrder and therefore by routing/admission.

`transcript_polish_model.build_transcript_polish_model_work_order` already takes all token, cost, and time limits as arguments. Its routed caller passes a `work_budget`, reconstructs replay from the persisted WorkOrder budget, and calibration runners persist these values in their manifests. No transcript-polish code changed.

Validation included 83 bounded-planner/service tests and focused thesis-impact legacy/configured/malformed budget tests. No model or live-state call was made.

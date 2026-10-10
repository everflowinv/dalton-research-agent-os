# ADR-0011: industry_model and company_model close on deterministic exit checks over what the lanes already produce; the metric base is one of those checks

*2026-10-06*  ·  Status: accepted (owner request of 2026-10-06; reversible only by a superseding ADR)  ·  Closes 1.2 #5, #6, #7 and 一.3 item 3 of [vision-gap-workspace-reuse-cockpit-debug-2026-09-30](../reports/vision-gap-workspace-reuse-cockpit-debug-2026-09-30.md)  ·  Builds on ADR-0004 (automation write scope), ADR-0008 and its addendum (a mission version is provenance, not scope); leaves the two human checkpoints (Deep Insight Gate, Investment Memo) exactly as they are

## Context

The Playbook (`research-playbook-version:team-analyst-manual:1`) walks every company through Initial Screen → Deep Insight Gate → industry model → company model → Investment Memo → active coverage. On 2026-10-06 the legacy stage ledger had all five companies at `industry_model entered`. None had passed, `company_model` had no rows, and no memo had ever been drafted. The report gave two explanations: the gate "cannot pass", and `STAGE_SPINE` is empty for both stages. Read-only evidence shows that neither was the whole story:

1. **The industry gate was not failing. It was crashing.** `mission_model_stage_lane._evaluate` loaded `tracking_cadence.load_policy()` to get cadence keys that `industry_model_readiness` never used. `POLICY_PATH` is `parents[2]/deploy/phase9/…`. In an installed release that path resolves to `…/lib/python3.14/deploy/…`, which does not exist. Every tick therefore raised, became `authority_read_failed`, and was recorded as `waiting` with no reason. The tick ledger only keeps `company_ref` and `stage_ref`. Evaluated directly, the legacy framework has met every industry criterion since v26 (09-30). From 09-17 to 09-29 it failed only `active_mission_binding`, because the framework was bound to v25/v26 while the mission had rolled to v27/v28.
2. **The company gate demanded an exact mission version.** EPAM's model is bound to v26 and IBM's to v14, while the active mission is v28. Every budget or policy signing rolls the version. ADR-0008's addendum already decided that a version is provenance and not scope, and this gate predates that decision.
3. **CTSH's and IBM's filing proofs no longer replay byte-for-byte.** The invariant contract later redefined `subject_hash`. The hash-bound source rows are unchanged, and re-evaluating them today still gives `available`. The forecast lane reports "every company has a current driver model", so nothing would ever rebuild these models.
4. **The memo could never start.** It looked for the gate's mission version at the top level of the record, but a Deep Insight Gate record keeps it under `bindings`. It required an `industry_framework` *MissionDeliverable*, but the framework is its own authority and that deliverable kind has 0 rows. It also handed the model the whole collection, about 7–13 MB per company, while one call admits 120 KB. That call is refused before sending, every time.
5. **Deep Insight `entered` was written only by the person deciding**, at the moment of the decision. CTSH's and DXC's drafts existed from 09-13 and were returned three times. The ledger showed no Deep Insight stage for either company until the owner approved them on 10-03.
6. `forecast_reconciliations` has 0 rows. It needs a filed actual for a forecast quarter. ACN's FY26 Q4 10-K was not in Core on 10-06: the latest statement filing has period end 2026-05-31, and the 09-25 earnings 8-K carries no statements.

The report also notes two competing definitions of "a stage was executed". The onboarding blueprint uses "its metric base is held". The code uses "its authority checks pass".

## Decision

**One definition.** A model stage is executed when its exit gate passes. The gate includes the stage's declared metric base (`STAGE_SPINE`) as one criterion, `stage_metric_base_held`. The spine lists the figures. The gate counts them from the same authorities as its other criteria. Nothing else decides. Every criterion is a read of an append-only authority and no criterion needs a model call. The stage record cites the evidence refs (framework, calendar, model, sensitivity, valuation), so a verdict can be replayed.

**industry_model passes for a company when** (`model_stage_readiness.industry_model_readiness`):

| Criterion | Playbook question | Reads |
|---|---|---|
| `active_mission_binding` | — | the latest industry framework belongs to *some version* of the active mission_ref and to its industry |
| `inputs_source_bound` | Q1 every input sourced | all framework blocks drafted with sources |
| `five_company_revenue_growth_margin_comparison` | Q2 gap vs peers | revenue, growth and gross margin are computed in the comparison for every company in the universe |
| `comparison_limits_explained` | Q2 | the comparison carries comparability notes |
| `unconnected_inputs_explicitly_deferred` | Q3 high-frequency identified | every gap names candidate sources, and the high-frequency gap is one of them |
| `company_update_calendar_bound` | Q3 "进入更新日历" | the company has a catalyst-calendar version |
| `stage_metric_base_held` | — | `STAGE_SPINE["industry_model"]`: this company's revenue, revenue growth and gross margin, 4 quarters each, counted from the comparison cells |

**company_model passes when** (`company_model_readiness`):

| Criterion | Playbook question | Reads |
|---|---|---|
| `active_mission_model_sensitivity_binding` | — | the latest model and its sensitivity table belong to the active mission_ref; the table names this model's id, hash and inputs hash |
| `current_sensitivity_rule_binding` | — | the current selection and band rules |
| `three_to_five_key_drivers` | objective | 3–5 selected drivers |
| `historical_model_checks_pass` | Q1 | ≥ 8 filed quarters of history |
| `two_year_filings_reconciled_zero_error` | Q1 zero error | the model's filing proof, re-evaluated now on its exact hash-checked source rows (`ForecastModelAuthority.filing_proof_recheck`), still reports `available` |
| `forecast_assumptions_explicit` | Q2 hidden assumptions | every assumption has `because` and refs |
| `driver_peak_trough_mean_bands` | objective | every selected driver has its historical band |
| `consensus_divergence_quantified` | Q3 | the consensus bridge is available with what-if cells |
| `model_covers_latest_filing` | — | the model's history reaches the latest statement filing's period end |
| `valuation_snapshot_current` | memo input | the latest valuation snapshot has ≥ 1 available metric and was computed from the latest price-series version |
| `stage_metric_base_held` | — | `STAGE_SPINE["company_model"]`: 8 filed revenue quarters in the model |

**Rejected as exit criteria.** `forecast_reconciliation` rows are rejected because they wait on the earnings calendar: a gate that passes only once a quarter cannot be reasoned about, and reconciliation belongs to active coverage. Peer-relative sensitivity bands are rejected because no authority produces them, and the industry gate's comparison is the peer evidence. An exact-mission-version binding is rejected per the ADR-0008 addendum.

**The lane** (`dispatch_model_stage_bridge`, unchanged registration) now does the following:

- **Entering the Deep Insight Gate.** It records `deep_insight_gate entered` under the mission principal as soon as a company has passed its screen and a gate draft bound to the mission exists. Entering is not passing. `gate_passed` on this stage remains human-only, and `record_stage` enforces that.
- **Advancing every company.** It evaluates every company and carries a company that passes one gate straight on to the next gate in the same tick. Every company that qualifies is recorded on the first tick after a deploy. Nobody writes stage rows by hand, and there is no per-environment script.
- **Explaining waits.** Each waiting company gets a `waiting:<ticker>` count key and a ≤200-character Chinese reason naming the criteria it is missing. For example: `CTSH 公司模型缺：估值快照没有按最新股价重算`.
- **Treating a missing table as absent.** An authority table that does not exist yet reads as "none", so a new workspace sees `还没有行业框架`, not a read failure.

**The Investment Memo** (`collect_frozen_input`, `investment_memo_material`, `dispatch_investment_memo`) changes in these ways:

- **Gate and framework lookups.** It reads the approved gate's mission version from `bindings`, accepts any version of the active mission_ref, and takes the industry framework from its own authority.
- **Bounded material.** The model sees a deterministic, bounded view of 72 KB. Each row keeps its exact ref and the hash of the full record. Long text and lists are cut with explicit `_omitted` counts. History bulk is dropped. Forecast cells are kept only for key result lines, research events only for the latest 8. Claims fill the remaining budget: quantitative first, then newest. Producer prompts are about 84 KB. The verifier reads the rows the draft cites.
- **Redrafting.** A memo is redrafted only when its anchors move: the approved gate version or the driver-model version. New claims, prices or events do not trigger a redraft. The lane signature binds those anchors too, and a content refusal holds a company for 24 hours.
- **Decision across mission versions.** A memo stays decidable after a mission roll. As H1 did for the gate, it is verified against the version it was drafted under, and its stage records bind the active version. The approvals page lists it, and 需要你处理 lists it as `memo_decision` (urgency 1).
- **Reason text.** The lane's idle reason names what each company is waiting on, in Chinese.

The memo still uses the existing purposes `investment_memo` / `investment_memo_verifier` and the dossier model pair: 4 producer calls plus one independent-family verifier, with caps of $1 per call and $5 per run. The 12 questions, the Anti-thesis, the risk/reward checks and every number's ref are enforced exactly as before. Owner approval remains the only path to `active_coverage`.

## Consequences

- On deploy, the legacy environment records industry_model `gate_passed` for all five companies. company_model passes for every company whose valuation snapshot has caught up with the day's price, which takes one valuation tick per company. The memo lane then drafts one memo per tick.
- `STAGE_SPINE` is no longer empty for the two model stages. Only the gate reads these entries. `extraction_requests` is still called only for `initial_screen`, so nothing goes looking for these figures in a document.
- A stored filing proof that no longer replays is still refused by `filing_proof()`, the exact reader. Only the gate uses the re-evaluating reader. Tampered or drifted source rows still raise in both.
- The anchor rule means an undecided memo is not refreshed with newer claims. If the owner wants a refresh, they reject the memo and wait for the next anchor change, or for the 24-hour hold after a refusal.

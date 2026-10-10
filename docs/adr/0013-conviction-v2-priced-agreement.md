# ADR-0013: Conviction v2 — a call is evidence confidence × scenarios × alignment, held to thresholds the owner publishes; agreeing with the market is priced, not refused

*2026-10-06*  ·  Status: proposed (owner request of 2026-10-06; activation is the merge — the v2 defaults are in code)  ·  Closes 1.1 #1 of [vision-gap-workspace-reuse-cockpit-debug-2026-09-30](../reports/vision-gap-workspace-reuse-cockpit-debug-2026-09-30.md)  ·  Answers the "Policy migration required" row of [owner-research-prompt-contract-audit-2026-09-11](../reports/owner-research-prompt-contract-audit-2026-09-11.md) and the first row of [vision-owner-coverage-refresh-2026-09-13](../reports/vision-owner-coverage-refresh-2026-09-13.md)  ·  Builds on P15d ([p15d-conviction-call-v1.0](../reports/p15d-conviction-call-v1.0-2026-09-09.md)), ADR-0006 (cockpit decisions go out through the writer as `human:`), ADR-0008 (version chains)  ·  Joins, without depending on, ADR-0012 (`ScenarioVersion`, branch `feat/structured-scenarios`)

## Context

P15d's call (`conviction_call.py`, `SCHEMA_VERSION = "0.1"`, `conviction-policy:p15d:v1`) encodes one rule from the onboarding blueprint (§5.2 P15d) in four places: when the market is bullish and we are bullish too, the call has no value. The deterministic `precheck` refuses a company unless a live debate or a consensus gap shows we differ (`we_agree_with_the_market`). The variant-view validator requires `where_market_is_wrong`. The drafter is told a call *is* a disagreement. The verifier's first rejection code is `this_is_not_a_disagreement`.

The 09-11 prompt-contract audit found that this rule is too narrow. Agreeing on direction still leaves differences in magnitude, timing, probability and valuation, and some same-direction calls pay well. The audit also found that loosening only the prompt would "corrupt policy meaning", because the precheck, validator, verifier, authority and lane identity all assume divergence. It asked for three components kept apart: **evidence confidence**, **scenario probabilities and payoffs**, and **investment attractiveness**. It also asked for a versioned policy that a person publishes.

Live evidence (legacy Core, read-only, 2026-10-06):

- One proposal ever: IBM `avoid`, 09-14, which was deferred on 09-17. The 13 lane runs between 09-11 and 09-14 cost US$0.074–0.092 each when they reached a model. Every other run was refused by the verifier with `market_view_not_supported_by_cited_rows` or `this_is_not_a_disagreement`.
- All five companies now have a valuation snapshot on the latest price series, with three available metrics, as of 2026-10-06. All five have consensus price targets and ratings.
- `thesis_versions` holds two theses. The industry thesis `demand-bottoming` has admission candidates for both `industry:us-it-services` and ACN. The ACN thesis `ai-reinvention-growth` has ACN candidates only. `event_judgement.company_theses` builds a dict keyed by thesis, so whichever candidate row is read last wins. For every company except ACN, the industry thesis therefore reads as "another company's thesis".
- A latent v1 bug: `conviction_call_draft.build_verifier_prompt` calls `json.dumps` for valuation rows, but the module never imported `json`. Now that every company has a valuation, any v1 run that reached the verifier would have crashed. This change fixes it.

## Decision

### 1. Schema 0.2 (`ConvictionCallProposal`, `schema_version: "0.2"`)

The v1 fields are kept: chain, week, direction, decision word, horizon, consensus gap, thesis and debate refs, evidence fingerprint, rubric, model attribution, mission binding and checkpoint. The changes are:

| Field | Meaning | Who writes it |
|---|---|---|
| `our_view` | statement + refs | drafter (only rows it was shown) |
| `evidence_confidence` | `{level: low/medium/high, statement, thesis_refs[≥1], claim_refs[]}` — the thesis and claim rows the confidence rests on. Every `thesis_refs` entry must be in the call's `thesis_refs` | drafter; refs resolved from row ids |
| `scenarios[]` | 2–6 rows `{name, probability ∈ (0,1], payoff (signed % share return over the horizon, ≥ −100), trigger (observable), refs, source_ref}`. Probabilities must sum to 1 ± 0.01 | drafter. When `source_ref` names a held `ScenarioVersion`, the probability and payoff are copied from the held row |
| `expected_return` | `{stock_percent, position_percent, probability_total, method}`, where stock_percent = Σp·payoff / Σp. The position return is the same for a long, negated for a short, and null for avoid | **derived**. A supplied value that differs is refused |
| `market_view_alignment` | `{alignment: same_direction / opposite_direction / market_view_unavailable, market_view (v1 block), where_market_is_wrong: optional}` | drafter. `market_view_unavailable` is required exactly when no market view is held |
| `valuation` | binding to the snapshot used: ref, hash, as_of, price version, close | lane (deterministic) |
| `invalidators[]` | `{statement, thesis_version_ref?, falsifier_ref?}`, at least the policy's `min_invalidators` | drafter |
| `gate` | `{status, reasons, threshold_kind, required_percent, observed_percent, policy_hash}` | **derived** from the embedded policy. A refused gate is never stored |
| `policy`, `policy_ref`, `policy_hash` | the exact policy body the call was held to, so it can be replayed | lane |

`where_market_is_wrong` is optional. `event_pathway`, `risk_reward` and `falsifiers` are replaced by scenario triggers, the expected-return gate and invalidators. **v1 proposals stay readable.** `normalise_proposal` dispatches on `schema_version`. Both shapes share the table, the chain (a v2 call can be version 2 of a v1 chain), the weekly cap, the decision record and `decide_conviction_call`. `proposal_columns` / `proposal_summary` are the single readers that know where each shape keeps its fields; the cockpit and the final-surface check use them. The table needs no migration: for v2 rows the `confidence` column is the evidence level, and `risk_reward_status` is `met` (or `not_applicable` for avoid), because a stored v2 call has passed its gate.

### 2. The policy object (`conviction_policy.py`, table `conviction_policy_versions`)

The policy is versioned and append-only. Only a `human:` principal can publish it, through the writer operation `publish_conviction_policy`, which is HUMAN_GOVERNANCE-only. The schema enforces this as well (`actor_ref LIKE 'human:%'`, plus immutability triggers). Every publication must name the newest prior version, so a stale page gets a conflict. Publishing bytes identical to the policy in force returns `unchanged`. Retries with the same idempotency key return `duplicate`. The **defaults live in code** (`DEFAULT_POLICY`, the same bytes as `deploy/phase9/conviction-policy-v2-default.json`). While no version has been published, the effective policy is the code default, and reading it never installs the table. Every workspace runs the same code; only its published versions differ.

| Field | Default |
|---|---|
| `min_evidence_confidence` | `medium` |
| `required_scenarios` | 3 |
| `allowed_horizons` | all four |
| `allowed_directions` | `long`, `short` (no `avoid`) |
| `min_expected_return_percent` (position, per horizon) | 3–6m 8 · 6–12m 12 · 1–3y 25 · 3–5y 50 |
| `same_direction_min_expected_return_percent` | 3–6m 15 · 6–12m 20 · 1–3y 40 · 3–5y 80 (must be ≥ the general bar) |
| `thesis_scope` | `company_only` (`company_or_industry` lets an industry thesis count) |
| `max_valuation_age_days` | 7 |
| `daily_drafting_cost_cap_usd` / `max_drafts_per_day` | 1.00 / 3 |
| `min_invalidators` | 1 |

v1 stays frozen in code and is shown on the cockpit page for comparison. A published version is always gate v2. To stop drafting, the owner uses the lane pause lever, not a rollback.

### 3. The gate change

- **Precheck (free, no model), `precheck_v2`:** the company has an active thesis within the policy's scope, and a valuation snapshot that is on the latest price series, has at least one available metric, and is no older than `max_valuation_age_days`. Divergence is no longer a reason to stay silent. Reasons are closed (`GATE_REASONS_V2`) and labelled in Chinese.
- **Policy gate (deterministic, after drafting, before the verifier is paid), `evaluate_policy_gate`:** checks allowed direction and horizon, evidence level ≥ the floor, scenario count, invalidator count, and position expected return ≥ the threshold. The threshold is the **same-direction** bar when alignment is `same_direction` or `market_view_unavailable`, and the general bar otherwise. *A same-direction call with a high expected return passes under v2; under v1 it is refused at the precheck (`we_agree_with_the_market`) and could not be written without `where_market_is_wrong`* (test `test_同向高回报不再被拒`). Unchanged from v1: inf/nan/over-bound numbers are refused, the verifier must be an independent family, and invented row ids refuse the whole draft.
- **Verifier:** a v2 prompt with v2 finding codes (`evidence_confidence_overstated`, `probabilities_not_supported`, `payoff_inconsistent_with_valuation`, `alignment_misread`, …). Agreeing is explicitly not a finding. The provider contract moves to `conviction-call-verifier-provider-output-0.2`, which carries both the v1 and v2 code lists. Stored 0.1 work keeps its contract.

### 4. The lane

`mission_conviction_lane` reads the effective policy each tick. Under v2 it chooses the first screen-passed company that passes `precheck_v2`, does not already have a proposal on this fingerprint, has no call this ISO week, and is not held by the failure budget. The **v2 fingerprint** is exact for theses, debates, the company file, catalysts, held scenarios and the policy hash. It buckets the valuation, consensus and street target by ISO week, because those are republished every trading day and naming exact versions would make every day "new evidence". **Daily cap:** before launching (lane) and before the model call (child), the day's drafting spend and draft count are read from the runs' own `summary.json` files, which survive restarts. When either cap is reached, the company is `held` with a Chinese reason until the next UTC day. The input table adds the street's price target and ratings (`M1`) and any held structured scenarios (`S1…`).

### 5. Join point with ADR-0012 (`ScenarioVersion`)

`conviction_call_cli.held_scenarios(store, company_ref)` resolves `dalton_core.scenario_version.conviction_scenarios(store, company_ref)` **by name at call time**. Until that module exists, or if it raises, it returns `[]` and the drafter writes its own table. Expected contract: rows of `{ref, name, probability, payoff, trigger}`. Probability and payoff may be `None`; payoff is a signed percentage over the call's horizon. When the drafter names a held row (`scenario_row_id`), its probability and payoff are copied, and the proposal's `scenarios[].source_ref` names the `ScenarioVersion`. That merge adds no other code here. The ADR-0012 side must decide how a scenario's payoff is expressed per horizon.

### 6. Cockpit

- **Approval card:** the 待办审批 card for a v0.2 call shows the three components: ① evidence confidence and what it rests on, ② the scenario table (probability, share return, trigger, held-scenario mark) with the expected return and the threshold it cleared, ③ the alignment with the market, where the market is wrong if stated, the invalidators, and the valuation date and policy. Accept / reject / defer write a decision through `decide_conviction_call`, as today.
- **Policy page:** 策略指引 has an 投资建议策略（Conviction v2） card, served by `GET /v1/cockpit/conviction-policy` and `POST /v1/cockpit/conviction_policy`. It shows the policy in force, an edit form that requires a reason, the published versions, and a side-by-side comparison of any two of v1 (frozen), the code default and any published version. Every label is in Chinese.

A new workspace's research foundation binds `conviction_policy` to the v2 code default (v1 is kept under `v1`). The mission already grants `conviction_call` (scope and checkpoint). The new-workspace canary asserts that a created workspace runs v2 with no patches.

## Consequences

- On merge and deploy, every environment drafts under v2. With the defaults, the expected first run on legacy is **one company (ACN)**: it is the only company whose thesis names it, and all five have current valuations. At roughly US$0.08–0.10 per run (drafting on Claude Opus 5 at $4 / $20 per M tokens, verifying on Gemini 3.8 Flash; v1 runs measured US$0.074–0.092), the run fits well inside the US$1.00 daily cap. If the owner publishes `thesis_scope: company_or_industry`, all five qualify: three drafts on day one and two on day two (≈US$0.5 in total). A draft is not a proposal. The model's own table must still clear the gate, and the street is constructive on ACN (target +15.6%, 11 buy / 16 hold), so a long ACN call would be priced as same-direction and need ≥ 20% over 6–12 months.
- `test_conviction_call_lane` keeps pinning v1 behaviour by patching `active_gate` to `v1`. v1 remains reachable in code, but no publishable version selects it.
- The weekly cap stays at one per company (schema `UNIQUE(company_ref, week_key)`). It is not a policy field.

## Owner decisions

1. The default thresholds above, especially the same-direction premium (6–12 months: 20% vs 12%) and whether `market_view_unavailable` should keep being priced as agreement.
2. `thesis_scope`: whether the industry thesis may anchor a company call (1 company vs 5 on the first run).
3. The daily drafting cap (US$1.00 / 3 drafts) and whether `avoid` should be draftable.
4. Whether a published version should be able to select the frozen v1 gate as a rollback, instead of using the lane pause lever.
5. For ADR-0012: the horizon convention for a held scenario's payoff.

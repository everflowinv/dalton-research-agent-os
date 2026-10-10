# ADR-0012: A scenario is a versioned object with machine-observable conditions; automation proposes it, the owner adopts it, and only an adopted scenario acts

*2026-10-06*  ·  Status: proposed (implemented on `feat/structured-scenarios`; takes effect when merged and deployed)  ·  Closes 1.1 #3 of [vision-gap-workspace-reuse-cockpit-debug-2026-09-30](../reports/vision-gap-workspace-reuse-cockpit-debug-2026-09-30.md); answers onboarding v1.0 §5.2 (P12a catalyst / scenario object), the Dossier row of [foundation-research-prompt-contract-review-2026-09-11](../reports/foundation-research-prompt-contract-review-2026-09-11.md) and row 3 of [vision-owner-coverage-refresh-2026-09-13](../reports/vision-owner-coverage-refresh-2026-09-13.md)  ·  Builds on ADR-0004 (write scope), ADR-0007 (revision candidates), ADR-0008 (versions, never edits), ADR-0009 (one event plane; `TrackingCadenceVersion`), ADR-0010 (the judgement lane is the only producer of revision candidates)

## Context

The dossier already *thinks* in scenarios. `variant_view.convergence_pathway` says what would have to happen, and by when, for the market to come round; `observable_signals` says what we would see if it is happening and what would show us wrong; the drafting prompt asks every slot for "the alternative trigger, its falsifier, and the next observable tracking item". On legacy on 2026-10-06 all five companies, and META on ws-7d, have such a variant view. All of it is cited prose. No program can tell whether "新签订单转为上升" has happened, so nothing watches it, the weekly brief cannot say a scenario moved, and the tracking cadence (0 rows live in either environment) never learns that something we said mattered just did.

The 09-11 review said exactly this and added the condition: structured scenarios may be added "only through separately reviewed versioned authorities rather than encoded as an ungoverned prompt-only object". The 09-13 refresh repeated it: "a generic tracking object or a model scenario cannot replace it". This ADR is that authority.

## Decision

**Four append-only tables, one module (`company_scenario.py`).**

| Object | What it is | Who writes it |
|---|---|---|
| `ScenarioSet` (`company_scenario_sets`) | one proposal for one company: the scenarios one model call (or one owner act) said at once, the anchors it was drafted from (dossier version + body hash, driver-model version, thesis versions, material hash), the proposer provenance (purpose, work order, invocation, route decision, prompt hash, cost), and any condition dropped at screening | automation (scope `dossier`) or a person |
| `ScenarioVersion` (`company_scenario_versions`) | one scenario: `key`, `name`, `narrative`, `probability` (two-decimal string), `linked_thesis_refs`, `linked_driver_refs`, `evidence_refs`, `triggers[]`, `invalidators[]`, `state` (`live`/`retired`), `change_reason`; chains to the previous version of the same scenario (`company-scenario:<hash(company,key)>`) across sets | as the set; owner revisions are versions inside the adopted set |
| `ScenarioDecision` (`company_scenario_decisions`) | accept / reject / defer one set, bound to its hash | a `human:` principal only (schema `CHECK` and authority) |
| `ScenarioObservation` (`company_scenario_observations`) | one condition of one version as the evaluator saw it: `triggered` / `not_triggered` / `unobservable`, reason, value, its date, source ref, whether the version was in force; appended only when one of those changed | automation (scope `observation`) |

**A condition is something a lane already observes.** `{condition_id, label, observed_by, metric_ref, transform, comparator, threshold, horizon, refs, metric_label, unit, baseline}` with a closed observer vocabulary:

| `observed_by` | `metric_ref` | read from | source ref written |
|---|---|---|---|
| `sec_actual` | a driver of the company's driver model (`concept:…` / `row:…`); `transform` `level` / `yoy_pct` / `qoq_pct` | `forecast_model_versions` filed history | `<model version>#<driver>@<period end>\|<accession>` |
| `market_price` | `price:close` | `market_price_series_versions` last bar | series version |
| `valuation` | `valuation:<metric>` | `valuation_snapshot_versions` (available metrics) | snapshot version |
| `claim` | a Claim `metric_or_aspect` | newest active quantitative Claim (retired ones skipped) | claim version |
| `event_judgement` | `judgement:<DECISION_WORD>` | count of event judgements with that word since the version was written | newest judgement |
| `owner` | — | nobody: always `unobservable`, the owner watches it | — |

Comparators are `gt/gte/lt/lte`; thresholds are decimals; the horizon is a date (today to three years out). A value dated before the version was written cannot *trigger* it (`predates_scenario`); past the horizon nothing triggers (`horizon_passed`). Evaluation is a pure function; reading is one memoised query per input per tick.

**Numbers are referable.** A proposal is drafted from a deterministic material table that includes an *observable metrics* table (every metric a lane can read for this company, with its latest value, date and source ref). The screen after the call — no second model call — refuses any ref the material did not contain, any metric not in that table, any set whose probabilities do not sum to one (±0.01), and stores beside each condition the baseline value, date and source its threshold was set against. A condition the current value already satisfies is dropped and recorded in the set (`already_met`): a trigger that has already happened is not a trigger. A scenario left with no trigger refuses the whole proposal.

**Governance.**

1. *Proposing* is automation under the dossier's scope (`dossier`), as the mission principal: these are the dossier's own scenarios made checkable, not a new kind of output, and both live missions (legacy v28, ws-7d v8) already grant it. One bounded call per company (purpose `scenario_proposal`, brain tier, dossier model configuration, $1 per-call cap from `call_budget_defaults.json`, coverage pool), at most 6 launches a day, 10 minutes apart, no retry on the same dossier body within 24 h, at most one undecided proposal per company, and a re-proposal only after the last set was decided *and* the dossier body changed *and* 7 days passed.
2. *Adopting* is the owner's: a set is in force only after `decide_company_scenario_set` (human governance op) accepts it; the newest accepted set is the company's set. An owner may write a set directly (in force at once) and may revise an adopted scenario's probability or retire it (`revise_company_scenario`); each is a new version. Nothing is ever edited.
3. *Observing* is automatic and is the only thing that happens before adoption: the observation lane evaluates the conditions of adopted scenarios **and** of the newest undecided proposal (so the approval card can show whether a proposed trigger is even observable), but marks the latter `in_force: false` and hands nothing off.
4. *Acting* happens only for an adopted scenario, and only through doors that already exist:
   - **ADR-0007 path.** A condition that turns `triggered` records a `scenario_trigger` `ResearchEvent` (ADR-0009: the one way in; derived tier; payload carries the scenario, condition, observed value, source and observation refs and the first linked thesis). `scenario_trigger` bypasses triage, like a filing, so every one reaches the judge; the judge's decision then becomes a `ThesisRevisionCandidate`, a `ForecastRevisionProposal`, a no-revision follow-up or a notice exactly as for any event (ADR-0010). Without `market_event` in the mission the trigger is only observed, and the lane says so.
   - **Tracking cadence.** The same firing re-times the company's adjustable sources through `tracking_cadence.publish_priority` — the function the owner's 「优先看这家」 (`prioritize_company`) now calls too, so both re-time a company identically (a quarter of baseline, never under an hour; fixed sources untouched) — with `change_reason=driver_event`, `decision=scenario_trigger`, the observation refs as evidence. When no adopted condition is triggered any more and the lane's own prioritisation is ≥ 14 days old, it publishes the baseline back (`scenario_settled`). It never undoes an owner's prioritisation. An owner decision or revision that moves an adopted probability by ≥ 0.10 re-times the company the same way under the owner's principal (`human_revision`, `scenario_probability`).

**Where it shows.** Cockpit company card key `scenarios` and a 「情景与触发」 tab (adopted scenarios, every condition's rule, status, latest value, date and source, whether it was handed to the judge; the pending proposal; 「调整概率」/「停用这个情景」); an approvals card `company_scenario_set` (采纳 / 驳回 / 暂缓, bound to the set hash); a 需要你处理 item `scenario_decision` (urgency 4). The weekly brief gains `## 情景与触发` (each company's adopted scenarios with every condition's status at the window end, plus decisions, proposals and in-force status changes inside the window) as frozen refs and hashes that `issue()` replays. It is keyed on `scenario_tracking` in the issue, present only on a Core that has the scenario tables, so every earlier issue re-renders byte for byte; it is **not** added to `ISSUE_SECTIONS` / the rubric's `WEEKLY_BRIEF_SECTIONS`, because a new template slot is a rubric version.

**Same code everywhere.** The schema is in `bootstrap.SCHEMA_DATABASES`; both lanes register through `lane_registry` with no writer argument (the proposal lane is on wherever `dossier-model-config.json` or `initial-screen-model-config.json` exists, the observation lane wherever there is a Core); a new workspace's mission already grants `dossier`, `observation` and `market_event`; its `tracking-policy.json` is installed by workspace setup. Nothing per environment.

## Rejected

- **A `scenario` write scope.** Honest but would leave both live environments `ungranted` until the owner republished their missions; the dossier scope already covers "automation writes the dossier's research", and adoption — not scope — is what this ADR gates on. The owner may still ask for a dedicated scope later; it is one tuple entry plus a mission version.
- **An independent verifier call.** The owner's adoption is the verification, and every number is checked deterministically against the material; a second family would double the cost of a proposal that does nothing until a person accepts it.
- **Writing revision candidates directly from a trigger.** `thesis_revision_candidates.judgement_ref` is a foreign key to `event_judgements`, and ADR-0010 makes the judgement lane the only producer. A trigger is evidence for the judge, not a verdict.
- **Re-proposing on every dossier version.** ACN has 58 dossier versions; a proposal per version would be a model call per redraft that the owner must read. A decided set, a changed body and a week are all required.

## Consequences

- First run (read-only reasoning over live 2026-10-06): **legacy** proposes for ACN, CTSH, EPAM, IBM and DXC (all have a variant view; prompts fitted to 23.8–26.0 KB); **ws-7d** proposes for META only (AMZN, GOOGL, MSFT have no dossier). Six calls, one per company, inside the day's six-launch cap; comparable calls (debate map, ~26 KB in) settle at ≈ $0.31 on average, so ≈ $1.0–1.8 on legacy and ≈ $0.2–0.35 on ws-7d, hard-capped at $1 per call. Nothing fires, no event is recorded and no cadence moves until the owner accepts a set; until then the observation lane only appends `in_force: false` observations.
- `scenario_trigger` events spend the judgement lane's `event_response` pool like any other event. A noisy threshold is fixed by revising or retiring the scenario, never by relaxing who adopts it.
- `tracking_cadence_versions` gets its first automation writer. Cadence versions remain the only ordering pressure (ADR-0009).
- Conviction v2 (1.1 #1) can read `scenarios[{prob, trigger}]` from here instead of asking a model for them.

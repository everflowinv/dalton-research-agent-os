# Budget configurability audit — 2026-09-10

## Scope

Read-only source audit of integration head `e5cb7d570937ddfbe7814d9b75233301020537fb`. “Budget” here means a limit on paid model work, connector calls, event batches, or a shared spend pool. Display-only row/byte truncation is listed separately where it can suppress work. No production state was opened or changed.

## Existing authoritative configuration

These paths already satisfy the requirement, provided callers do not replace them with local constants.

| Consumer | Current source | Authority/configuration point |
|---|---|---|
| Whole mission paid work | `coverage_mission.py`, `cockpit_model.py`, `thesis_impact_budget.py` | Signed mission `budget.max_daily_paid_calls` and `budget.max_daily_cost_usd`; bound into immutable budget policy/admission records. Keep here. |
| Source discovery | `mission_source_discovery.py` | Versioned discovery-plan `budget.max_calls_24h`, intersected with mission `max_alphaengine_calls_24h`; 1,000 is a schema safety maximum, not the selected operating budget. |
| Guidepoint discovery | `mission_guidepoint_lane.py` | Versioned plan `budget.max_calls_24h` and `max_calls_per_tick`; however code additionally clamps per tick to `SEARCHES_PER_TICK=3`. Move the safety/operating clamp into the plan or connector policy. |
| Agenda | `agenda.py`, `agenda_coordinator.py` | Agenda policy has `max_input_tokens`, `max_output_tokens`, `max_daily_cycles`, `max_daily_cost_usd`, and `max_monthly_cost_usd`. Already configurable. |
| Human-intent ask/goal/steer/draft | `human_intent.py` | Service config carries token and per-call cost limits. Already configurable. |
| Thesis-impact production day cap | `thesis_impact_production.py`, `thesis_impact_budget.py` | Versioned budget policy `day_cap_micros`, plus mission binding. Already configurable. |
| Bounded research planner | `bounded_planner_driver.py`, `llm_research_planner.py` | Service field `planner_max_cost_usd`; work order carries full token/cost budget. Token defaults remain constructor defaults and should be explicit in service configuration. |
| Credential-limited connectors/crowd | `credential_authority.py`, `crowd_credential_grants.py` | Signed credential grant `max_calls`. Already configurable and auditable. |
| Calibration/canary tools | calibration runner modules | CLI/manifest fields expose run/case/campaign caps and token limits. Defaults are acceptable only as proposal defaults; persisted manifest is authority. |

## Hardcoded paid-model budgets requiring configuration

All rows below construct real model work orders from module constants. The clean seam is a closed `work_budget` block in each purpose’s installed model/service configuration, hash-bound with its routing and budget-policy references. The CLI should read it once before the call; model-profile limits remain an independent upper ceiling.

| Consumer/purpose | File | Current hardcode | Recommended configuration point |
|---|---|---:|---|
| Document extraction | `document_extraction.py` | 16k input, 3k output, 19k total, $0.05/call, 60s | `document-extraction-model-config.json.work_budget` |
| Numeric extraction | `document_numeric_extraction.py` | 33.5k total, $0.03/call | Same extraction config, separate `numeric_work_budget` if genuinely distinct |
| Metric-discovery extraction | `metric_discovery_extraction.py` | 16k/1.2k, $0.03, 60s | Extraction config under purpose-specific block |
| Research-plan producer | `research_planner_cli.py` and legacy `research_planner.py` | 120k/4k, $1.50 | `research-planner-model-config.json.work_budget`; remove duplicate legacy literal |
| Company model spec | `company_model_cli.py` | 120k/6k, $2.50 | `company-model` service/config budget block |
| Initial/deep-insight gate | `deep_insight_gate_draft.py`/CLI | 120k/3k, $0.60/call; $3/run | `initial-screen-model-config.json` (or exact gate config) per-call and per-run blocks |
| Company dossier producer/verifier | `company_dossier_draft.py`, `company_dossier_cli.py` | 120k/3k, $0.60/call; $2.50/run | Producer and verifier configs; run cap belongs lane policy, not shared draft module |
| Industry framework producer/verifier | `industry_framework_draft.py`, CLI | 120k/3k, $0.60/call; $2.50/run | Producer/verifier configs plus lane run cap |
| Debate map | `debate_map_draft.py`, CLI | 80k/4k, $0.80 | Debate-map producer config; verifier separate if used |
| Conviction call | `conviction_call_draft.py`, CLI | 60k/3k, $0.60 | Conviction-call model config/policy |
| Claim-index tagging | `claim_index_tagging.py`, CLI | 60k/2k, $0.60 | `claim-index-model-config.json.work_budget` |
| Research quality | `research_quality_score.py`, CLI | 120k/2k, $0.60 | Quality model config work budget |
| Earnings producer/verifier pair | `earnings_season_cli.py` | 60k/2k, $0.12 each; reservation assumes two calls | Both earnings configs; pair reservation must sum their configured ceilings rather than `MAX_COST_USD * 2` |
| Event judgement producer/verifier | `event_judgement_cli.py` | prompt-byte alias/700 output, $0.10 each; reservation assumes up to four calls | Both event configs plus a lane batch policy; compute reservation from selected group and configured pair costs |
| Zero-base producer/verifier | `zero_base_review_cli.py` | 60k/1.8k, $0.12 each | Both zero-base configs; pair cap explicit |
| Thesis-impact assessment/verifier | `thesis_impact_control.py` | assessment 3k/500/$0.25; verifier 10k/2k/$0.25 | Nested service model phase configs, with independent pair budget |
| Transcript polish | `transcript_polish_model.py` | constructor defaults 196k/64k/$10 | Installed polish config/manifest; require explicit production values |

## Hardcoded run, cadence, and event-volume budgets

| Consumer | File | Current hardcode | Recommended configuration point |
|---|---|---:|---|
| Event judgement queue | `event_judgement_cli.py` | 8/run, 3/company | Versioned tracking/judgement policy, exposed in Cockpit with pair-cost impact |
| Tracking document/claim scans | `research_event.py`, `tracking_lane_cli.py` | 60/type scan, 120 recorded/run | Tracking policy: scan/read cap and write cap separately |
| HKEX filing events | `hkex_filings_cli.py` | 60/run | HKEX discovery/acquisition plan budget |
| SEC ownership events | `sec_ownership_cli.py` | 40/run | SEC ownership tracking plan |
| Conviction cadence | `conviction_call.py` | 1/company/week in frozen module policy | Publish as a versioned conviction policy rather than module mapping |
| AlphaEngine bounded probe | `bounded_alphaengine_probe.py` | 30/window | Explicit probe manifest/owner policy |
| Guidepoint lane | `mission_guidepoint_lane.py` | code clamp 3/tick despite plan field | Remove clamp or make the declared maximum a signed connector ceiling and show effective min |
| Dossier/framework/gate drafting | draft modules | 3 units/run (dossier/framework), monetary run caps | Versioned lane policy; unit cap and dollar cap should be changed together |
| Zero-base prompt | `zero_base_review.py` | 12 events | Zero-base review policy input budget |
| Guidance profile | `guidance_profile.py` | 24 events | Guidance-profile policy/schema field |

## Event pool and shared budget defects

The mission may already declare explicit USD caps in `budget.pools`; that path is configurable. When absent, `budget_pools.DEFAULT_SHARES` silently fixes coverage/event-response/adhoc/maintenance at 55/15/25/5 percent and permits coverage borrowing after a hardcoded half-day. This fallback should become an installed, versioned pool policy or a required mission budget block. Existing missions can retain the exact default values through migration.

`event_judgement.py` separately declares `POOL_SHARE=0.15` and derives its cap directly from the mission daily cost. That duplicates the central event-response pool and can drift from an explicit mission `budget.pools` declaration. It should consume `mission_pool_scope(..., event_response)` exclusively. `research_task.py` similarly exposes a module `POOL_SHARE`, although its operative path calls central `pool_caps`; remove the duplicate as an authority signal after compatibility review.

Pool assignment in `budget_pools.LANE_POOLS` is code-owned. Because assignment controls how money may be spent, it should be part of the same signed pool policy (or mission policy reference), while `LaneSpec` remains a declared purpose checked against that policy. `BORROW_AFTER_DAY_FRACTION=0.5` is also an operating budget rule and belongs there.

## Limits that are not monetary budgets

UI card limits (`cockpit_plane.MAX_EVENTS_ON_CARD`, `MAX_JOBS`), text/response byte safety bounds, file-size caps, and schema maxima protect rendering or memory. They need configuration only when they suppress scheduled work rather than truncate a view. In particular, `MAX_EVENTS_ON_CARD=8` is presentation-only; connector document byte ceilings are governance/runtime safety limits and should remain bounded even if configurable. Provider catalog `limits.max_cost_usd=250` is a profile admission ceiling, not authorization to spend $250; it must stay independent from each work-order and mission cap.

## Suggested implementation slices

1. Add and validate `work_budget` to installed model configs, then convert the simple single-call CLIs (claim index, quality, company model, debate/conviction) without changing model selection.
2. Convert paired producer/verifier lanes (earnings, event, zero-base, thesis impact), calculating reservations from both configured phase budgets before any call.
3. Convert multi-unit lanes (dossier, framework, deep gate) with both per-call and per-run caps and preserve partial-run semantics.
4. Make tracking/SEC/HK event counts and conviction cadence versioned policy fields.
5. Make the pool split, lane assignment, and borrowing time a signed policy; remove the event judgement’s duplicate 15% calculation.
6. Keep connector credential quotas and mission budgets as outer fail-closed ceilings. Effective admission is always the minimum of work order, model profile, credential grant, pool, and mission caps.

Every migration should preserve current values byte-for-value as seeded defaults, mark configs that lack the new fields as legacy/unknown rather than unlimited, and avoid changing signed historical mission or model records.

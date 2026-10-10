# ADR-0014: Holding-period return attribution and buy-side expectation sources. Income bridges and per-share series come from the filed statement lines, and a fourth quarter is never derived

*2026-10-07*  ·  Status: proposed (owner request of 2026-10-07; activation is the merge, because the lane needs no configuration)  ·  Closes 1.1 #2 of [vision-gap-workspace-reuse-cockpit-debug-2026-09-30](../reports/vision-gap-workspace-reuse-cockpit-debug-2026-09-30.md)  ·  Answers v0.8's "market and valuation data lack an authority", the Memo row of `foundation-research-prompt-contract-review-2026-09-11`, and the third P0 row of `owner-foundation-gap-priorities-2026-09-11`  ·  Builds on P11c (valuation snapshot), P13-M2 (driver model), P11b (consensus), P12c (DebateMap), ADR-0008 (version chains), ADR-0012 (scenarios) and ADR-0013 (Conviction v2)

## Context

The 09-11 review found that the filings already explain the gap between operating income and net income, but nothing in the system recorded that explanation. ACN, CTSH and EPAM all report net income different from operating income minus tax (−74m / −7m / −12m in the quarter measured). DXC files no diluted share-count row, which leaves its per-share series without a denominator. Holding-period returns and "what the buy side expects" existed only as prompt prose. `conviction_call.py:175` mentions them in one comment, and `company_model_report.py` displays EPS but has no authority behind it.

Before this change the data was already present in `coverage_mission_statement_lines`. That table holds every filed income and cash-flow line with concept, parent, period, accession and value: about 17,000 lines on legacy and 12,500 on ws-7d. The valuation snapshot, the driver model and the consensus also had current versions for most companies.

## Decision

Four append-only objects, all `derived_deterministic`. They are computed in process with no model call and no network, by one module (`return_attribution.py`, schema `return_attribution_schema.sql`). Every environment runs the same code.

### 1. `IncomeBridgeVersion`: one company, one filed period

- **Source.** The filed statement lines of that period. When several filings state the period, the one with the fullest income statement wins (a later 10-Q may repeat only net income); the original filing beats a comparative; the newer filing beats the older. `NetIncomeLoss` falls back to the cash-flow statement of the same filing, because Amazon's income-statement lines carry none.
- **Chain.** Operating income → pre-tax → net income (parent) → net income to common. Each line is `statement-line:<line_id>` with concept, accession, filed value, `sign` and `sign_basis`. The classification comes from a closed concept table: `interest_expense`, `interest_income`, `other_non_operating`, `equity_method`, `tax`, `discontinued_operations`, `minority_interest`, `preferred`.
- **Step 1 items.** These are the lines the filer hangs under pre-tax income other than operating totals. A subtotal (for example CTSH's `NonoperatingIncomeExpense`) is expanded to its children, and the subtotal-versus-children gap is recorded. An unknown concept is classified `other_non_operating`, signed by its XBRL balance, and labelled as such.
- **Equity-method income.** It sits wherever the filer's own pre-tax concept puts it. CTSH's pre-tax concept excludes it, so it falls in step 2.
- **Reconciliation.** Each step is checked against the filed anchor. The tolerance is the larger of 0.05% of the anchor and half a rounding quantum per term, and it is written into the record. A gap beyond the tolerance is recorded as `residual` with its amount; it is never absorbed. A missing anchor is named (`missing_rows`). No anchor is ever inferred.
- **Net income to common.** This uses the filed `NetIncomeLossAvailableToCommonStockholders*` line when there is one. Otherwise it is net income less any preferred line, and the record states the basis it used.

### 2. `PerShareSeriesVersion`: one company, every period, with its period semantics

- **Period semantics.** Each period is `quarterly` (80–100 days), `year_to_date` (6 or 9 months starting on the fiscal-year start) or `annual` (12 months ending on the fiscal-year end). The fiscal year is read from 10-K periods only. Amazon's 10-Q states trailing twelve months in its cash flow, so a 12-month period that does not end on the fiscal-year end is skipped and listed. Every other period is listed under `skipped_periods`.
- **Rows.** Basic and diluted EPS and weighted share counts, each a filed line ref. Dividends per share are filed where the filer states them. Otherwise they are derived as cash dividends divided by weighted basic shares, and that basis is labelled. A filer with no dividend line anywhere gets zero, and the basis says so.
- **Checks.** Each period carries `eps_check`: does net income to common divided by diluted shares match the filed diluted EPS? A missing required row (basic or diluted EPS, basic or diluted shares) is flagged per period in `missing`.
- **No derived quarters.** **A fiscal fourth quarter is never derived** by subtracting cumulative values. A fiscal year with no quarter stated for its last three months is listed in `quarters_not_stated`.
- **Trailing-twelve-month points.** There is one stated rule, recorded on every point with its three components: `annual`, or `annual_plus_ytd_minus_prior_ytd` (prior fiscal year + this year-to-date − the same span a year earlier, all three filed). The share count at a point is the quarter's own weighted diluted count, or the year's count when the point is a fiscal-year end.

### 3. `HoldingPeriodReturnBridge`: realised and expected, per company

Four legs. Each one decomposes total return into **earnings growth × share count (the per-share effect of buybacks) × multiple change − 1 + dividend yield**. The factors multiply back to the price return exactly. The additive contributions (in percentage points) split the price return by log weight.

| Leg | Inputs | Source kind of the expectation |
|---|---|---|
| `realised_trailing_year` | Price series bar ≤ (snapshot bar − 365 d) and the snapshot close. TTM diluted EPS at the latest period end on or before each end of the window (aligned by period, labelled as hindsight). Diluted shares at both points. TTM dividends per share | none (realised) |
| `expected_driver_model` | The next 4 forecast quarters of the driver model's `parent_net_income` (or `net_income`). **When the model has not computed net income** (4 of 5 legacy models), the proxy is model revenue × trailing net margin, labelled `driver_model_revenue_x_trailing_net_margin`. Diluted shares come from the model, or are held flat. The multiple is held | `market_implied` (the current multiple persists) |
| `expected_consensus` | The next fiscal-year consensus EPS against its own year-ago figure (same adjusted basis), and the vendor mean price target. The multiple change is implied by the target. Share count is inside the consensus EPS and is not split out | `consensus` |
| `expected_sell_side` | Same EPS growth. Price target is the broker-report consensus (`report_consensus`, with `street-estimate` refs) | `sell_side_note` |

The record carries refs and hashes for every input: the per-share series version, the valuation snapshot, the price series version and bar date, the driver model version and cell refs, and the consensus version. A leg that cannot be computed says why in Chinese, for example when EPS is not positive, or when there is no snapshot, model or target.

### 4. `ExpectationSourceKind` and `ExpectationSourceTag`

The vocabulary is closed: `consensus`, `management_guidance`, `sell_side_note`, `owner_view`, `market_implied`. The deterministic classifier (`expectation-source-classifier:0.1`) tags:

- **Consensus versions:** vendor EPS, revenue, target and rating fields are `consensus`; `report_consensus` is `sell_side_note`.
- **DebateMap market positions:** each ref is classified by its source. A claim from `claim:transcript:` is `management_guidance`. A claim from `claim:alphaengine:` or `claim:sales_notes:` is `sell_side_note`. Consensus versions are `consensus`. Price and valuation versions are `market_implied`.
- **Conviction calls (v1 and v2):** the market view's own P15d sources map as follows: `consensus` → consensus; `sell_side_rating` and `sales_note` → sell_side_note; `management_framing` → management_guidance. Its refs are classified as above. A scenario row whose `source_ref` names an owner-adopted `ScenarioVersion` is `owner_view`.

Anything else (web, Guidepoint, crowd narrative) stays **`unclassified`** and is listed, never forced into a kind. Subjects are immutable, so a tag is written once per (subject, field, classifier version).

### 5. The lane (`mission_return_attribution_lane`, order 98, scope `valuation`)

- **How it runs.** In process, like scenario observation. One `MAX(rowid)` signature covers statement filings and lines, the driver model, the valuation snapshot, consensus, DebateMaps, conviction calls and the mission.
- **When it runs.** When the signature moves (at least 5 minutes after the last scan), or every 6 hours regardless, every company in the mission is marked due. **One company is refreshed per tick**, which is about 20 ms on the legacy inputs.
- **What it writes.** An object is appended only when its body changed. A company with no filed lines and no snapshot writes nothing.
- **Tick-ledger reasons.** All in Chinese: 「…个申报期利润桥（完整 n、有差额 n、缺行 n）…回报分解可算 k/4 段」, 「任务没有授予 valuation 写入范围…」, 「利润桥与回报归因的输入自上次计算以来没有变化」.
- **New workspaces.** Nothing to configure. The created mission grants every automation scope, the schema is in `bootstrap.SCHEMA_DATABASES` and the deploy migration list, and the lane has no launcher.

### 6. Cockpit and weekly brief

- **Company card tab 「回报归因」.** It shows: the headline (how many periods are fully reconciled, have a residual, or are missing rows, and which per-share rows are missing); the return decomposition table (口径 / 总回报 / 盈利增长 / 股数变化（回购） / 倍数变化 / 股息 / 预期来源) with each leg's notes; the latest annual, quarterly and year-to-date bridge tables, with the status of every step; the per-share series (period, semantics, EPS, shares, dividends per share, check, missing rows); the TTM rule; the quarters not stated; and the count of each expectation source kind.
- **Weekly brief section 「回报归因」.** One line per company whose decomposition moved by at least 5 pp in any leg total or component, or whose leg became computable or stopped being computable. Refs and hashes are frozen in the issue and replayed. A Core without the table carries no key at all, so earlier issues re-render byte for byte.

## Consequences: first run (read-only on the live statement lines, 2026-10-07)

These figures come from read-only exports of each Core's inputs, run through the same builders. Nothing was written to any live Core.

**legacy (US IT services):**

| Company | Income bridges | Per-share series | Return legs available |
|---|---|---|---|
| CTSH | 21 / 21 complete. The 09-11 −7m is non-operating +1m and equity method −8m | complete | 4 / 4 |
| EPAM | 21 / 21 complete. The 09-11 −12m is FX −9.85m and other non-operating −1.8m | complete | 4 / 4 |
| ACN | 22 / 24 complete. The 09-11 −74m is interest −70.6m, investment income +75.1m, other non-operating −29.9m and NCI −48.9m. The 2 exceptions are FY2024 Q4 and FY2025 Q4: the 10-K states those quarters only partly (revenue, operating income, net income, EPS), with no pre-tax or tax line → `missing_rows` | complete | 4 / 4 |
| IBM | 21 / 21 `missing_rows`: **no operating income line** (IBM reports none). Pre-tax → net income → to common reconciles in every period | complete | 4 / 4 (driver leg on the model's own net income) |
| DXC | 21 / 21 `missing_rows`: **no operating income line**. Pre-tax → net income reconciles, NCI included | **missing basic and diluted share counts in all 21 periods (42 flags)** — the 09-11 DXC finding | 4 / 4. The share-count component cannot be split out |

No bridge on legacy has an unexplained residual. CTSH, EPAM, ACN and DXC use the driver-leg proxy (model revenue × trailing margin), because their models do not compute net income.

**ws-7d (internet platforms):** AMZN, GOOGL, META and MSFT bridges are all complete (21, 21, 21 and 22 periods). AMZN's net income is read from its cash flow statement. **GOOGL has no basic or diluted share counts in any period (42 flags).** Return legs: META 4, GOOGL 4, AMZN 3 (no driver model), MSFT 0 (no valuation snapshot in ws-7d yet).

**ws-e399:** no statement lines yet. The lane visits the mission's companies and writes nothing.

**Expectation tags (legacy):** 25 on consensus versions (20 consensus, 5 sell-side), 15 on DebateMap market positions (all sell-side), and 1 on the IBM conviction call (consensus).

## Owner decisions

1. **The driver-leg proxy.** Keep the labelled `revenue × trailing net margin` when the driver model has no net income line (today four of five legacy companies), or show that leg as unavailable until the income bridge's non-operating lines feed the model's own chain.
2. **Expected-leg conventions.** The multiple is held (the market-implied default) in the driver leg. Should a reversion target be added, for example to the snapshot's historical median, and under which source kind?
3. **Material change threshold** for the weekly brief: 5 pp.
4. **Classifier scope.** AlphaEngine claims count as sell-side notes and transcripts as management guidance. Web, Guidepoint and crowd narrative stay unclassified. Should Guidepoint (expert calls) get a kind? That would mean a vocabulary version, because the five kinds are closed.
5. **Whether Conviction v2 and the Investment Memo should read** the return bridge and tags directly (a follow-up), instead of relying only on the drafter's own table.

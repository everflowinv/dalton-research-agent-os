# ADR-0015: Preliminary actuals from the company's own earnings release. An Item 2.02 8-K advances the fiscal period to "reported", its Exhibit 99.1 income statement is read deterministically, and a preliminary number never outranks a filed one

*2026-10-08*  ·  Status: proposed (owner question of 2026-10-08: 「ACN 前两天刚刚发了业绩，我们现在系统能不能自动更新业绩，形成新的 input」; activation is the merge plus a deploy, because nothing needs configuring)  ·  Builds on C1 (`sec_earnings_release`, Item 2.02 dates), P10e/P10t (SEC filings index and filing fetch), batch o (sec.gov fetch ceiling), batch p (transcript required periods from filings), batch r (code-owned AlphaEngine query forms), P14a (events), P14f (earnings season) and ADR-0009 (one event plane)

## Context

A US issuer announces each quarter twice. On the day of the call it furnishes an Item 2.02 8-K with the press release as Exhibit 99.1. Weeks later it files the 10-Q or 10-K whose XBRL lines this system reads (`coverage_mission_statement_lines`). Until now Dalton only knew about the second announcement:

- Accenture reported FY2026 Q4 around 2026-10-01. On 2026-10-08 legacy still held FY2026-Q3 as ACN's newest quarter: its 10-Q for 2026-05-31 and its 10-K for FY2025.
- The required transcript periods come from the newest filed 10-Q/10-K (`mission_stage._latest_fiscal_quarters`). The Q4 call was therefore not required. The 10-08 transcript search ran with `missing_periods: []`, and the query degenerated to "Accenture".
- Legacy's SEC discovery plan carries only the 10-K spec. New workspaces get `current-report-8k` from the generator, but no environment fetched an 8-K's exhibit, and `sec_earnings_release` said plainly that it could not read text.

## Decision

### 1. The fiscal period advances to `reported` from the company's own 8-K

`earnings_release_intake.reported_release` reads the SEC filings index already spooled on disk; it makes no network call.

- **Which release counts.** It takes the newest Item 2.02 8-K whose fiscal quarter is after the newest filed 10-Q/10-K quarter.
- **Which quarter the release is about.** It is the newest quarter that had closed before the release. Quarter ends are the fiscal-year-end month and every third month before it, using the 52/53-week rule `mission_stage` already applies. A release more than 100 days after any quarter end is not mapped.
- **Fiscal year end.** It is read from the newest filed 10-K. Without one, it falls back to SEC's own `fiscalYearEnd` in the index. For example, ACN's August year end gives FY2026-Q4, and MSFT's June year end gives a 2026-07-29 release FY2026-Q4.
- **Status.** The quarter is `reported`, not `filed`. It becomes `filed`, and the advance disappears, the day its 10-Q/10-K is ingested.

Consumers of the advance:

- **Transcripts.** `mission_stage.evaluate_mission` uses the reported quarter as the latest quarter (`period_basis: "earnings_release"`). The Q4 call becomes a required and missing period at once. The existing shortfall retry then searches "Accenture Q4 2026", and directed selection asks for FY2026-Q4.
- **Calendar.** The catalyst calendar already confirms the date from the same 8-K row. New: `emit_calendar_events(late_calibration_entry_refs=...)` opens the confirmed calibration window for a release learned after its two-day window had closed. The bound is 45 days. The window stays open in the ledger until the calibration can be written against the filed quarter. Calibration still waits for filed actuals (see "Not done").

### 2. The SEC spec set is owned by code

The same approach as the AlphaEngine query forms of batch r:

- **Where the specs live.** `sec_plan_specs.SEC_PLAN_SPECS` holds the 10-K spec and the 8-K spec. The 8-K spec, `current-report-8k`, has `item_filter: ("2.02",)`.
- **What the approved file still governs.** The budget, the companies, the identity fields, and the intervals of each spec it carries.
- **Rendering.** `effective_discovery_plan` renders a SEC plan with the code's spec set. The coordinator, the launcher and the `sec_filings_index_cli` child all run the rendered plan.
- **No edits to approved files.** Legacy's file is not edited. It gains the 8-K spec with code intervals (90 / 2 / 1), and its 10-K keeps its 400-day lookback. A generated plan already equals the code set, so it gets the same hash and is not re-searched.
- **Parity.** `sec.plan_specs.<spec>` rows show the drift. `added_by_code` is `info`.

The frozen `list_filings` normaliser still returns every 8-K, so the item filter lives in the acquisition lane. `earnings_release_intake.ineligible_filing_refs` reads the exact index bytes the discovery was answered from. It steps over two kinds of 8-K, which stay `discovered` without holding the queue (`next_discovered_document(excluded_document_refs=...)`):

- an 8-K that is not Item 2.02;
- an Item 2.02 8-K whose quarter is already filed.

### 3. The press release is fetched under the existing fetch governance

When the public-web fetch child has fetched an Item 2.02 8-K of a code-owned release spec (`public_web_fetch_cli.press_release_due`, checked against the index bytes again), it takes the following steps:

1. It reads Exhibit 99.1's file name from the 8-K's own exhibit index (`press_release_exhibit_name`). Since 2017 every exhibit is hyperlinked there. The name must be a plain file name in the same directory.
2. It builds the URL authority with `sec_filings_index.build_sec_exhibit_url_authority`. This is the primary document's authority with only the file name changed: same host, same accession directory, same discovery envelope.
3. It makes one more governed `fetch_get` under the same `web-fetch` governance record, mission grant, sec.gov profile and 32 MB ceiling.
4. It writes `exhibit-manifest.json` beside the 8-K's manifest. `summary.exhibit` records the outcome and `provider_calls` counts it.
5. A failed exhibit never changes the 8-K's own status.

### 4. Preliminary actuals: parsed deterministically, append-only, superseded on read

`parse_press_release` reads the exhibit's HTML tables. No model is involved.

- **Which table.** The first table that has:
  - a quarter heading ("Three Months Ended …");
  - a period end that matches the 8-K's fiscal quarter;
  - a unit ("in thousands", "in millions", or "except share …" for share counts);
  - USD;
  - a revenue row and an operating or net income row.
- **What it reads.** The current-quarter column: the first non-percentage number of each row.
- **Rows.** Revenue, cost of revenue, gross profit, operating income, pre-tax income, tax, net income, net income attributable, basic and diluted EPS and shares, and dividends per share.
- **Provenance.** Each row carries the character `span` of its `<tr>` in the decoded exhibit and the verbatim cell excerpt with its hash. Its digits are re-found in that excerpt.
- **Refusals.** Anything ambiguous is refused with a Chinese reason: no quarter column, current year not first, no unit, the wrong quarter, or no revenue.

Storage (`earnings_release_schema.sql`, append-only triggers, `DaltonStore` authority):

- `earnings_release_reads` holds one read per exhibit body and parser version, with status `parsed`, `unparseable` or `exhibit_unavailable`.
- `preliminary_actuals` holds one row per line, carrying the accession, the exhibit URL, the body SHA-256 and the span.
- The tracking lane runs `ingest_company` for each tracked company on every non-dry run. It is idempotent.

**Supersession is a read-time fold, not an update.** `preliminary_actuals(...)` marks a row `superseded` as soon as a 10-Q/10-K for its fiscal quarter is in `coverage_mission_statement_filings`. It attaches the filing, and the filed line when one with the same concept and a quarter's duration exists. Every consumer then uses the filed numbers.

**Never outranks a filed number.** Preliminary rows are never written to `claim_versions` or `forecast_reconciliations`. Those stay filed-only, so no dedupe group or canonical ordering can ever put the two side by side. The evidence tier is `management_direct`, because the release is furnished, not filed, and is unaudited. That is one step below `primary_filing`.

### 5. Consumers

| Consumer | What it gets |
|---|---|
| Quantitative claim promotion | A third pass, `summary.preliminary`: claim-shaped rows with basis `company-press-release-preliminary` and the statement labelled 「初步，待 10-K/10-Q 核对」, with accession and span. They are reported, never admitted. A superseded row is not listed. |
| Forecast reconciliation | `provisional_reconciliations`: the same arithmetic and bands (rescaled revenue, 1% / 3%) against the latest `metric:revenue-usd` line for the quarter. It is computed on read and never written. Once the filed reconciliation exists, it says `superseded` and points at it. The re-run on the filed numbers is the existing lane, unchanged. |
| Events | A new kind, `earnings_release`: one per parsed release whose quarter is not filed. It carries the accession, exhibit URL, body hash, headline figures with unit and scale, and the provisional band. Triage is bypassed (it is the company's own quarter), and the judge gets a derived block listing every row with its span and the PRELIMINARY caveat. |
| Weekly brief | An optional section 「最新业绩（初步）」, gated on its key like 情景与触发 and 回报归因, and replayed against the ledger. |
| Cockpit | The company card shows 「最新业绩（初步）」 with the rows, their sources, the provisional reconciliation, and the note that the 10-Q/10-K numbers will replace them. The card disappears once the quarter is filed. |
| Needs human | Nothing, unless a read fails. Then there is one `earnings_release_unread` card per filing, naming the accession and exhibit URL. It disappears when the quarter is filed. |
| Earnings season | A calibration that is waiting reports `preliminary` (the period, the accession and the row count). |

## Not done, and why

- **Preliminary numbers are not admitted as Claims.** Admission needs a named deterministic auto-commit rule that the owner signs. The promotion ledger's `origin_kind` CHECK would also need a table rebuild. More importantly, keeping them out of `claim_versions` is what makes "never outranks a filed number" structural rather than a ranking rule. If the owner wants drafting lanes to cite them, that is a new signed rule, and its claims would carry this basis.
- **The calibration is not written on preliminary numbers.** The calibration is written once per occurrence, and its citable numbers must be Claims. It therefore waits for the filed quarter, as before. What changes is that a late-learned release now opens the window at all.
- **No forward-date reading.** A "we will report on the 25th" 8-K is not parsed. `announced_next_date_entry` remains the seam for that.
- **Effect on ws-7d.** Non-2.02 8-Ks are no longer fetched there. Until now ws-7d fetched every 8-K, and the buyback-authorisation reader saw Item 8.01 ones. If those are wanted, the item filter is one line in `sec_plan_specs`.

## Consequences

The company's own numbers reach events, the brief and the Cockpit on the day the exhibit is fetched. Both the date and the quarter are labelled preliminary. The 10-Q/10-K replaces them with no write and no human step.

Each release costs two www.sec.gov fetches (the 8-K and the exhibit). Each company costs one extra `list_filings` call every two days (the 8-K spec), within the SEC plan budget.

Parsing costs no model call. The `earnings_release` event costs one event-judgement call group per release, through the existing `event_response` pool.

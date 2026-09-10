# Fund Deliverable Sample Analysis

Date: 2026-09-10

## Scope and handling

This review covered the two files at the Desktop root named in the task. Both
were opened read-only. No macros, external links, refreshes, or embedded objects
were executed. Temporary DOCX render files were kept outside the repository.
The source files and their research data were not copied into the repository.

The companion machine-readable layout manifest contains only structural names,
formula patterns, formatting conventions, and source SHA-256 hashes. It contains
no source-cell values or copied investment prose.

## Word initial-screen sample

The document is a 71-page, A4 portrait analyst report. Its argument begins with
the conclusion and valuation view, then moves through company overview, industry
structure, explicit key questions, financial forecast, risks, valuation,
management, ownership, and tracking. This is materially richer than a generic
company summary: it connects the business model to contract economics, capacity,
backlog conversion, margin risks, earnings variance, valuation, and what can and
cannot be tracked.

The package contains 365 top-level paragraphs, 31 one-cell callout tables, 37
inline shapes, 52 media parts, and 15 embedded spreadsheet objects. The one-cell
tables are used as long evidence or interview callouts rather than conventional
grids. Charts and model extracts appear throughout the thesis rather than in a
detached appendix. There are no Word comments, footnotes, endnotes, or native
hyperlinks. Source labels are often rendered inside images or immediately below
charts, so they are visually legible but not consistently machine-addressable.

All 71 pages were rendered to PNG and reviewed in six contact sheets. The sample
has a consistent restrained fund-report appearance: black body text, bold section
and question headings, compact tables, and charts sized to the text column. No
obvious clipping, overlap, missing glyphs, or broken table flow appeared in the
render. The document is dense, has no visible table of contents, and relies on
manually formatted paragraphs rather than named Word heading styles. Those choices
work visually but make navigation, structured export, and accessibility weaker.

The research pattern to preserve is:

1. State the conclusion, valuation boundary, and uncertainty before the detail.
2. Explain the operating mechanism and competitive boundary using primary and
   expert evidence.
3. Turn the thesis into explicit questions and answer each with evidence and
   counterevidence.
4. Connect drivers to forecast statements and valuation.
5. Separate forecast uncertainty from facts and define a monitoring plan.

An equivalent generated deliverable needs structured citations for prose,
tables, and figures; exact links to the model version; explicit unanswered
questions; and a human approval state. HTML can match the research depth and
navigation, but it currently does not reproduce this sample's integrated charts,
embedded financial tables, page-oriented reading order, or print-quality source
captions.

## Excel model topology

The workbook has 18 sheets. It combines market-wide support tabs, AMZN and SHOP
company sections, hidden raw/cache sheets, and vendor data. The AMZN block is
separated by a visual divider and contains:

- `Valuation - AMZN`: market bridge, consensus comparison, trading multiples,
  SOTP, target price, and required-return discounting.
- `Financials - AMZN`: income statement, non-GAAP bridge, segment results, cash
  flow, balance sheet, property/capex schedules, and earnings history.
- `Driver - AMZN`: business-line, geography, GMV, unit, take-rate, subscription,
  advertising, logistics, unit-economics, AWS, capex, and capacity drivers.
- `Category Analysis - AMZN`: a monthly external category dataset and calculated
  mix/growth views.

The common AMZN calendar is annual 2014-2025 actual, 2026E-2027E annual forecast,
and quarterly 1Q17-2Q26 actual plus 3Q26E-4Q26E forecast. Actual and estimate
periods are explicitly suffixed in the header. Annual and quarterly axes coexist
on the driver and financial sheets, which makes the grain visible and permits
direct reconciliation.

The core formula chain is auditable:

```text
operating drivers and assumptions
  -> business-line revenue and unit economics
  -> annual/quarterly Financials
  -> segment profit and group earnings
  -> SOTP segment values
  -> equity value / diluted shares
  -> target price and required-return discounting
```

Representative formula patterns are `annual = SUM(Q1:Q4)`, `3P services = 3P
GMV * take rate`, `revenue = retail + AWS`, `free cash flow = operating cash flow
+ capex`, and `target price = equity value / diluted shares`. These are formula
relationships, not copied forecast values.

The model uses classic fund conventions. Blue font is used heavily for manually
entered assumptions and source data, green for many cross-sheet formulas, and
black/theme font for same-sheet formulas. Dark-blue section bands use white text.
Amounts usually show whole USD millions, percentages one decimal, per-share values
two decimals, negatives in parentheses, and zeros as dashes. There are exceptions:
some formula cells remain blue and some selected outputs are red, so a generated
model should classify cell role explicitly rather than infer it solely from the
source color.

## Formula and dependency findings

The workbook contains formulas throughout the operating and valuation schedules;
the AMZN Driver alone has 7,030 formula cells and Financials has 5,641. There are
no formula strings containing `#REF!`, but legacy defined names include broken
`#REF!` entries. Twelve external-link package parts remain, pointing to unrelated
legacy workbooks, even though no active cell formula contains a bracketed external
workbook reference. A clean exporter should not carry these parts forward.

Capital IQ UDFs appear in market data, consensus, and financial history. They
require the desktop plugin to refresh. Cached values make the sample readable
offline, but cached values are not proof of current market data and must retain
their source/as-of status.

The principal AMZN annual/quarter issue is incompleteness rather than a hidden
formula error. Fully built schedules such as GMV, AWS revenue, AWS gross profit,
and AWS operating income use `SUM(BF:BI)` for 2026E and reconcile to their four
quarters. Several retail business-line rows have annual 2026E formulas while
3Q26E and 4Q26E cells are blank. Consequently annual retail revenue and total
revenue do not equal the visible quarterly sum. A future export must either
populate all four forecast quarters and tie them to the annual result, or label
the annual-only rows and make the quarterly reconciliation explicitly incomplete.
It must not insert zeros into the blank quarters.

Forecast columns contain both formulas and intentional blue hardcodes. That is a
valid model pattern when the hardcodes are inputs, but an exporter must derive the
classification from a manifest: historical actual, sourced current estimate,
editable forecast assumption, formula output, or unavailable. A date alone cannot
decide whether a value is actual.

## What the current prior import preserves

`prior_model_import.read_workbook` preserves numeric cached values as decimal
strings, formula text verbatim, sheet/cell coordinates, a nearby label, and a
conservative unit guess. It correctly labels every imported item `prior_human`
and does not admit it as an actual or verified forecast figure. The workbook
SHA-256 and append-only version retain source identity.

For this workbook, however, `MAX_ASSUMPTIONS=5000` is reached before the AMZN
model. A direct read returned exactly 5,000 entries: 632 from `Global Comps`,
2,352 from `TAM`, and 2,016 from `Monthly Tracking`, ending at `Monthly Tracking`
cell X57. None of the AMZN valuation, financial, driver, or category sheets enter
the `PriorModelVersion`. This is the most important current import gap.

The generic prior-research XLSX reader has a different loss profile: it reads at
most 400 rows and 60 columns per sheet from cached values. It can make most AMZN
labels and values searchable, but drops formulas, formatting, styles, charts,
named ranges, external-link identity, and columns BG-BI because they lie beyond
column 60. It therefore misses the latest quarterly forecast columns.

For DOCX, prior research preserves body paragraph text in document order. The
sample's 47,395 extracted characters fit under the 600,000-character cap. It does
not preserve named heading roles, page structure, chart pixels, embedded workbook
logic, chart-source captions as structured citations, or the relationship between
a paragraph and its nearby figure.

## Export gap and minimum implementation slices

The existing forecast authorities hold structured model rows and proofs, but the
system still lacks a deterministic renderer that produces a workbook with this
sample's full annual/quarter driver chain, formulas, editable assumptions,
valuation, formatting roles, and manifest-bound provenance. Copying forecast
results into cells would not meet the sample contract.

The smallest credible implementation sequence is:

1. Define a versioned workbook layout contract from the companion manifest:
   sheets, sections, period axes, row identities, cell roles, styles, and formula
   templates. Bind it to exact ForecastModel, CompanyModelSpec, ModelInput,
   reconciliation, and valuation hashes.
2. Generate the AMZN Driver, Financials, and Valuation sheets first. Every forecast
   output must be an Excel formula referencing a typed assumption or another model
   cell. Preserve blank/unavailable values and add explicit annual-quarter checks.
3. Add source/as-of tables for actuals and market inputs. Cached vendor values must
   be marked cached and must not silently become current actuals.
4. Emit a manifest containing source authority refs, input hashes, layout version,
   and a normalized formula-map hash. Verify formulas, styles, period boundaries,
   and annual-quarter reconciliation on the saved workbook.
5. Build the Word/HTML research presentation from the same memo, model, DebateMap,
   and citation authorities. Figures and tables should link to exact model outputs,
   and human approval should remain separate from publication.

The first acceptance fixture should use AMZN because the sample exposes both
annual and quarterly estimates, segment valuation, formula-driven outputs, and
the partial-quarter edge case. It should verify that changing a later forecast
driver changes the related quarterly, annual, earnings, and valuation formulas
without changing historical actual cells.

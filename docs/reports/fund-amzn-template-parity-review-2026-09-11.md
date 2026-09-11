# Fund workbook reference parity — 2026-09-11

The owner reiterated that exported workbooks must strictly follow the Desktop reference, especially its AMZN worksheets. Company-specific financial rows and equations remain determined by that company's filings and notes. The reference defines presentation and analytical depth; it does not prescribe Amazon's accounting lines for other companies.

The Desktop `US eCommerce_Model_20260801.xlsx` was reopened read-only. Its SHA-256 remains `545709e06eb0c1452cf74224d92e8ecb70bef4764f316e74595b2f41f87567b0`, matching the September 10 review. Four worksheet previews were inspected: Valuation, Financials, Driver, and Category Analysis. Source OOXML styles, column/row dimensions, panes, merges, number formats and representative cell bindings were independently extracted. The private extraction SHA-256 is `02ca90bbd4c1a33e77131e2295d7e696bdfbff79293d3a4f69d3438c7f955c8e`. No original file, external data link or vendor refresh was changed or executed.

## Remaining presentation differences

| Reference | Existing exporter | Required implementation |
| --- | --- | --- |
| Valuation precedes Financials and Driver within the company block | Driver, Financials, Valuation | Reference order and company-qualified worksheet names |
| Compact unit/period header in row 1; Financials/Driver put hierarchical labels in A, B and C, with wide D allowing label overflow | Large title row 1, period row 4, labels in A | Reference geometry and semantic row styles; A:C are not empty gutters |
| Annual axis begins E; annual growth/support and separator columns precede quarterly axis | Variable annual block and one separator | Preserve axis organization with actual company fiscal periods |
| Bright blue top band, light-blue sections, brown Driver business-section bands | Generic dark-blue title/period bands | Exact reference font/fill/border/number-format tokens |
| Nested labels, italic growth/margin rows, bold subtotals, role-dependent formula colors | Generic row list and partial formula colors | Semantic hierarchy from the company's model definition |
| Financials include reported/non-GAAP, segments, cash flow and balance sheet; valuation compares internal forecasts with Street and contains justified segment valuation | Current exporter supports a smaller governed forecast and optional valuation scenario | Preserve explicit missing coverage; extend actual authorities before displaying these schedules as complete |

The preview engine cannot evaluate the reference's Capital IQ functions, and the source also contains historical formula gaps. Those errors are not a design requirement and must not be copied into generated deliverables. The original vendor links remain untouched.

## Acceptance and next steps

1. Extract reusable style and layout parameters into a versioned, source-hash-bound contract without private research values.
2. Apply the contract to dynamic company-specific rows and the same validated formula graph used by forecasts, actualization and sensitivity.
3. Verify annual/quarter period semantics, formula dependencies, positive/negative/zero/missing values, and input changes through final outputs.
4. Visually compare each generated primary worksheet with its reference at normal zoom. A successful generic five-sheet export alone does not satisfy this requirement.

The current company-structure work first closes income-statement bridges, attribution and diluted EPS. Balance-sheet, cash-flow, detailed operating-driver and valuation schedules remain separate foundation extensions; presentation must not imply that those calculations already exist.

## 19:10 UTC implementation checkpoint

The reusable source-bound template contract is integrated through `c6a370dc`. It preserves the reference label hierarchy, annual support/CAGR columns, precise Financials/Driver widths and composed row/cell styles. Five template tests plus 18 existing export tests passed before the current exporter integration. The contract has not yet been applied to exported workbooks, and visual parity is not yet accepted. A Sol agent owns actual sheet reconstruction and rendered comparison; company-specific annual EPS and formula-graph work continues independently. Historical annual EPS now uses explicit filed numerator/weighted-share authority; forecast annual EPS remains unavailable until a separately declared annual-share forecast method is implemented and validated.

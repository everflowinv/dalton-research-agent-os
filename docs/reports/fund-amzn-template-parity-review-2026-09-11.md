# Fund workbook reference parity — 2026-09-11

The owner reiterated that exported workbooks must strictly follow the Desktop reference, especially its AMZN worksheets. Company-specific financial rows and equations remain determined by that company's filings and notes. The reference defines presentation and analytical depth; it does not prescribe Amazon's accounting lines for other companies.

The Desktop `US eCommerce_Model_20260801.xlsx` was reopened read-only. Its SHA-256 remains `545709e06eb0c1452cf74224d92e8ecb70bef4764f316e74595b2f41f87567b0`, matching the September 10 review. Four worksheet previews were inspected: Valuation, Financials, Driver, and Category Analysis. Source OOXML styles, column/row dimensions, panes, merges, number formats and representative cell bindings were independently extracted. The private extraction SHA-256 is `02ca90bbd4c1a33e77131e2295d7e696bdfbff79293d3a4f69d3438c7f955c8e`. No original file, external data link or vendor refresh was changed or executed.

## Original presentation differences (before this implementation)

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

## 20:30 UTC presentation and numerical acceptance

The final integrated template candidate is author commit `6c1053831f50d7cb53d634b12616a538dbae076d` (integration `4eb8f966`). Root and an independent Sol reviewer inspected actual Valuation, Financials and Driver renders against the read-only AMZN previews. This scope now passes: exact source style/layout tokens, compact annual/quarter columns, A/B/C label hierarchy, light-blue Financials sections, brown Driver major sections, annual House/Street/gap valuation blocks and one driver assumption row across periods. Currency labels derive from the model; unsupported market/Street values remain explicitly unavailable. Current shares outstanding is not substituted with weighted diluted EPS shares. Actual ratios and forecast assumptions are distinguished.

The structured 0.3 synthetic company includes interest income, interest expense, pretax income, non-controlling interest, parent income, explicit diluted-EPS numerator, weighted shares and diluted EPS. A mixed year contains a newly filed actual quarter plus three forecasts. The actualization exercise exposed and fixed future values still based on the superseded estimate: future formulas now replay from the filed actual using the same recorded assumptions. Annual projection 0.2 binds every structured income line outcome, exact calendar filing, model/input/structure and source-quarter identities; both reports and Excel consume it.

LibreOffice saved a recalculated XLSX, then each relevant cached cell was compared to internal authority:

- 56 historical structured quarter cells, 56 actual/forecast model cells and 28 annual projection cells: **140 comparisons**.
- 16 unavailable cells verified blank; zero missing or unexpected populated cells; zero formula errors.
- Maximum numerical residual `4.467e-9`, within recorded rounding tolerance.
- Original Desktop workbook SHA remains unchanged.
- Numerical receipt SHA: `adf3f885913529458df240b06748e90d76df26870a47b6cac0fda23952e41759`.
- Author suite: 130 passed. Independent export/template/structure suite: 40 passed. Root integration suite before the final currency-only delta: 317 passed; final delta: 40 passed.

The immutable synthetic model/spec/input/projection/calendar files, source/recalculated XLSX and renders remain in private `/private/tmp/dalton-fund-template-numerical-qa.84lnmk/`. No private original model values were committed.

This accepts the three-sheet presentation and numerical scope, **not AMZN-level model completeness**. Full balance sheet, full cash-flow statement, segment operating drivers, Category Analysis and sourced Street comparisons remain separate. A regression audit also found that structured 0.3 drops the existing legacy OCF/CapEx/FCF model outputs; a versioned 0.4 cash-flow companion is being implemented before the financial upgrade may deploy. It preserves explicit selected source lines and the existing FCF definition without claiming a full cash-flow model. R14c1 execution deployment remains separate.

## 21:44 UTC final R15 evidence

The bounded cash-flow companion and final workbook integration are now complete in the frozen R15 source `d1079bafc565e1bfa7d19d3360504fa845e9b327`. The reviewed author boundaries are core `f80fcc7f85aee24fcce81f56903b3db5adde3083` and exporter `c34fc4653876ef854a9e4d898c219a6a92f96f7b`; the corresponding production and test files in R15 are byte-identical. R15 is frozen for acceptance and **has not yet been deployed**.

Root visually accepted the final combined Valuation, Financials and Driver sheets. Financials keeps the income statement and a distinct light-blue Cash flow statement section. OCF, positive-outflow CapEx and FCF occupy their own company-model rows. Driver shows declared cash inputs and assumptions in company order. Historical actual ratios are green cross-sheet formulas tied to Financials; forecast assumptions remain blue. FY2025A and mixed FY2026A/E diluted shares and EPS coexist with the cash companion in the same structured 0.4 workbook. Rows with filed history and unavailable forecasts say `Forecast unavailable`; unavailable cells remain blank.

LibreOffice recalculated that single combined workbook before comparison with the immutable model and annual projection authorities:

- 56 income model quarter cells and 12 cash model quarter cells matched.
- 28 income annual projection cells and 6 cash annual projection cells matched.
- 6 income actual-ratio cells and 2 cash actual-ratio cells matched.
- All **110 comparisons** passed. Maximum absolute residual was `4.31167e-9`; formula errors, missing expected cells, unexpected populated unavailable cells and Formula Map omissions were all zero.
- The same receipt verifies model schema 0.4, annual projection 0.2, `day_weighted_quarters`, computed FY2025 historical EPS, and computed FY2026A/E shares and EPS.
- Numerical receipt SHA-256: `ff8ba7916326285adbcfc0138bdc77c7c196a1cd333baa07f043170a4e68aece`.
- Visual receipt SHA-256: `318acf34d4e9812f5b6e82e801b5e180a3f71d3de9628d36e37f74445732dc4b`.
- Recalculated XLSX SHA-256: `3c8550e57d70706329ea68cea47950e0634dff42002236681f85ecc94792f392`.
- Private evidence directory: `/private/tmp/dalton-fund-xlsx-cashflow-qa.0nx1w0kr/`.

The Desktop reference remained read-only at SHA-256 `545709e06eb0c1452cf74224d92e8ecb70bef4764f316e74595b2f41f87567b0`. This closes the accepted Valuation, structured income, bounded cash-flow and Driver presentation scope. It does not claim a full balance sheet, full cash-flow statement, Category Analysis, complete operating-driver model or sourced Street-consensus schedule.

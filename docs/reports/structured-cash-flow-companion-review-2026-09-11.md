# Structured cash-flow companion review — 2026-09-11

Status at 21:44 UTC: implementation and review complete in frozen R15 source `d1079bafc565e1bfa7d19d3360504fa845e9b327`. **R15 has not yet been deployed.**

## Scope and contract

The company-specific income DAG upgrade had dropped the existing operating cash flow, capital expenditure and free cash flow result lines. Company specification 0.4 now explicitly declares a cash-flow companion beside the income structure: exact filed concepts, a declared share-of-income-line forecast basis or unavailable, and FCF = OCF minus positive-outflow CapEx. The selected concepts drive the actual input query, including company-specific concepts; fixed standard candidate lists cannot silently override a new specification. Source objects/hashes, company/spec identity, income structure, reporting currency and formula are bound into the new forecast model 0.4. Old specification/model 0.3 behavior is preserved.

Actualization, sensitivity, reports and persisted annual projections consume the same results. Annual cash flows require four compatible quarter windows and preserve cumulative-difference source operands. Missing, incomplete or incompatible data remains unavailable, never zero. This is a bounded cash-flow companion: it supports explicit share_of_line or unavailable, and does not claim a complete cash-flow statement or balance-sheet model.

The final reviewed core is author commit `f80fcc7f85aee24fcce81f56903b3db5adde3083`; its corresponding R15 production and test files are byte-identical. It includes the normal authority path from stored specification → exact custom-concept input → structure/replay → forecast publication/readback → persisted annual FCF. It also normalizes single-currency cumulative operands without weakening mixed-currency rejection and refuses duplicate or overlapping cash quarter windows. The final author core suite passed 317 tests.

## Independent evidence

A read-only snapshot of the five latest live company models contained five schema 0.2 models and 500 stored result cells. Running installed R14c1 and an exact Git archive of the new core against the same snapshot produced identical canonical stored model bytes, readiness output and recomputed legacy results. This proves compatibility against that snapshot; it does not claim those legacy models have the new company-specific structure. No live writes or paid calls occurred.

- Snapshot SHA: `25955cf3968e853e4c2af1f250dccdf6533d5c6118901947ddf532c5f154393c`.
- Equal result SHA: `8afe564754c290e3e775e38774f0c716b3ec68ece8f73e1793ffa505fd257df2`.
- Private receipt SHA: `018c51ebce1be47aac0b2cca1fa0c6cf420fce2bcc82e3d710c5be20e69e36a8`.

The final legacy replay used a fixed read-only snapshot of the same five models and 500 stored result cells. Installed and candidate results both hashed to `8afe564754c290e3e775e38774f0c716b3ec68ece8f73e1793ffa505fd257df2`; model bytes, readiness and recomputed results were identical. Receipt SHA-256 is `a84354beadabd0f0d7a9d5e64116cc7e192b28854a96e7eb6d5aef99c6436e55`, stored under `/private/tmp/dalton-r15-final-legacy-replay-wwbkkhw7/`. The run performed zero live writes and zero paid calls.

The two earlier independent blockers are closed. Uppercase cumulative cash units now normalize within one bound reporting currency, while mixed currencies still fail closed. Duplicate end dates and overlapping quarter windows are rejected before any end-keyed consumer can overwrite an operand.

## Excel acceptance

The final exporter is author commit `c34fc4653876ef854a9e4d898c219a6a92f96f7b`; its corresponding R15 production and tests are byte-identical. The Desktop AMZN reference remains authoritative for presentation. Financials has a distinct light-blue Cash flow statement section after the structured income rows. It renders only the declared OCF, positive-outflow CapEx and FCF results. Driver renders the declared cash inputs and share-of-line assumptions, with actual ratios as green Financials-linked formulas and forecast assumptions as blue inputs. Annual cash cells consume projection 0.2 `line_outcomes`; unavailable outcomes remain blank. No Excel-side annual authority calculator was reintroduced.

The final combined structured 0.4 workbook was saved and recalculated by LibreOffice. It proved cash and income authority together, including a mixed actual/estimate year and `day_weighted_quarters` diluted shares/EPS:

- 68 model quarter cells: 56 income and 12 cash.
- 34 annual projection cells: 28 income and 6 cash.
- 8 actual share-of-line ratios: 6 income and 2 cash.
- **110 checks passed**, with maximum absolute residual `4.31167e-9`.
- Zero formula errors, missing expected cells, unexpected populated unavailable cells or Formula Map omissions.
- Numerical receipt SHA-256: `ff8ba7916326285adbcfc0138bdc77c7c196a1cd333baa07f043170a4e68aece`.
- Visual receipt SHA-256: `318acf34d4e9812f5b6e82e801b5e180a3f71d3de9628d36e37f74445732dc4b`.
- Private evidence directory: `/private/tmp/dalton-fund-xlsx-cashflow-qa.0nx1w0kr/`.

Root accepted the final combined Financials and Driver renders. This acceptance covers the company-specific income DAG, diluted-share/EPS annual authority, the bounded OCF/CapEx/FCF companion, Driver assumptions and the scoped valuation presentation. It does **not** claim a full cash-flow statement, full balance sheet, Category Analysis, complete operating-driver schedule or sourced Street-consensus coverage. Those missing authorities remain explicit rather than fabricated.

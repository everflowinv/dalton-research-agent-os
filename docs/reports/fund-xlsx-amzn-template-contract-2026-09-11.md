# Fund XLSX AMZN-source template contract — 2026-09-11

`fund_xlsx_template.py` exposes a style-only adapter for the existing openpyxl
fund workbook exporter. The packaged JSON is derived from the reviewed desktop
workbook with SHA-256
`545709e06eb0c1452cf74224d92e8ecb70bef4764f316e74595b2f41f87567b0` and
the exact XML style extraction with SHA-256
`02ca90bbd4c1a33e77131e2295d7e696bdfbff79293d3a4f69d3438c7f955c8e`.
The package pins the JSON file hash before parsing it.

The contract carries no company financial line names, values, or formulas. An
export consumer supplies its own valuation, financials, and driver sheet names;
annual and quarterly period labels; units; dynamic row roles; and exact cell
roles. `build_fund_xlsx_template_plan` converts those inputs into a closed plan.
`apply_fund_xlsx_template` then:

- orders Valuation before Financials and Driver while retaining other sheets;
- puts periods on row 1, with annual columns beginning at E and the quarterly
  block to the right of one separator column;
- reserves A:C for hierarchy, D for company-specific labels, and binds outline
  levels to caller-selected rows;
- applies the source Arial 8 typography, blue/brown section fills, blue inputs,
  green cross-sheet formulas, black local formulas, yellow hair-border
  assumptions, exact number formats, column widths, row height, hidden selected
  periods, hidden gridlines, and an E2 freeze pane; and
- preserves existing line-item values and formulas.

The caller must explicitly name hidden periods and every styled row or cell.
The adapter does not infer a company taxonomy from AMZN, hide periods based on
the current date, or move financial data into the hierarchy columns.

Validation:

- `PYTHONPATH=/Users/everflow/Projects/dalton-fund-xlsx-template/src /Users/everflow/Projects/dalton-research-agent-os/.venv/bin/python -m unittest tests.test_fund_xlsx_template -v`
- Four tests pass, covering the source/hash binding, JSON tamper refusal,
  company-specific row application with value/formula preservation, and closed
  mapping refusal.

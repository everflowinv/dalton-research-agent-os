# Fund XLSX format polish — 2026-09-10

The governed workbook now lays out fiscal-year columns on the left and quarterly
columns on the right, separated by a narrow blank column. Quarter headings derive
their fiscal quarter and fiscal year from the hash-bound calendar and mark each
period actual or estimate. Annual headings retain the prior partial and mixed
actual/estimate disclosures.

Styles are applied after worksheet population, so populated header cells receive
the dark-blue fill and white text. Hard-coded historical inputs and assumptions
are blue, formulas that cross sheets are green, and formulas contained within a
sheet are black. Currency, percentage, per-share, multiple, and ordinary-number
formats are distinct. Negative values use parentheses and zero displays as a
dash. Monetary rows display in currency millions through Excel number formats;
the underlying authority values and formulas remain in raw units. Formula Map
records the 1,000,000 display scale. Row labels show the currency and scale.
Sheets print landscape on tabloid
paper, repeat rows 1–4, fit to one page wide, freeze labels and the annual block,
and retain the existing incomplete-data blanks and gap records.

Annual flows still require four supported fiscal quarters. A ratio is never
summed. It is recomputed only when all four quarter cells bind the same ordered
pair of numerator and denominator results and those two annual aggregates exist;
otherwise the annual ratio remains blank with a specific gap.

Validation:

- `PYTHONPATH=src python3 -m unittest tests.test_fund_xlsx_export` passed 10 tests.
- The broader forecast, input, valuation, and export suite passed 121 tests.
- LibreOffice recalculated the formula chain and exported all five worksheets to
  an eight-page PDF. The Financials page rendered without `###` overflow at
  1224×792-point tabloid landscape size. The QA fixture is synthetic and remains
  under `/tmp/dalton-fund-format-polish-qa-v3`.

The final scale review is under `/tmp/dalton-fund-format-polish-qa-v5`; its
Financials page is `financials.png`. It shows readable USD-million values while
inspection of the saved workbook still reads a raw historical revenue value of
1,000,000,000 and the original forecast formula.

The exporter does not synthesize missing quarterly data or infer a ratio from its
label. Unsupported formula families remain explicit gaps.

# IBM XLSX operating-expense formula recovery — 2026-09-10

Read-only QA of the private IBM export found a deterministic exporter defect.
The authority model had 96 computed forecast result cells, but the workbook
represented only 36 with Excel formulas. Both operating-expense lines were the
first break; their absence then prevented operating income, tax and net income
from resolving, for 60 omitted computed cells in total.

The authority formula names the XBRL concept, for example
`ResearchAndDevelopmentExpense[k] = revenue[k] * share[k]`. The exporter
compared that string with the reader-facing label, such as `Research and
development`. A humanized or localized display label therefore made a valid
closed formula look unsupported.

The translator and its independent value replay now derive the exact local
concept name from the closed `result:operating_expense:<taxonomy>:<concept>`
reference. They still require the exact frozen formula, exactly one result
reference and exactly one assumption. Display text no longer decides executable
formula semantics.

A regression constructs a real forecast from statement inputs whose SG&A label
is humanized, formally publishes it through `ForecastModelAuthority`, exports
it, and verifies the expense, operating-income, tax and net-income formula
chain. The unsupported-gap entry is absent for that valid expense result.

The separate IBM QA remains honest about source gaps: no fiscal-calendar
authority was bound, so annual columns cannot be generated; free cash flow and
valuation are also unavailable. This repair supplies none of those values.

```text
PYTHONPATH=src python3 -m unittest tests.test_fund_xlsx_export
Ran 14 tests in 4.129s — OK
```

# Fund deliverable export review — 2026-09-10

## HTML corrections

The HTML chart reader previously accepted any current quantitative Claim named by
a product. A valid Claim for another company could therefore appear as a chart in
the requested company's report. Chart construction now requires the Claim's
`subject_ref` to equal the product's governed `subject_ref`. The export manifest
schema is 0.2 and enumerates the exact Claim version/content hashes and embedded
asset hashes, media types, and source refs used in the output. It contains no
local asset paths.

Reported and forecast values with the same subject, metric, unit, scale,
currency, and period grain may now share a chart. Every bar carries an explicit
`actual` or `estimate` label and distinct styling; unknown bases remain excluded.

Focused validation: `PYTHONPATH=src python3 -m unittest
tests.test_research_html_export` passed 11 tests. The regressions use real Claim
authority rows for both accepted and wrong-subject cases and verify replayable
Claim and asset manifest entries.

## XLSX cross-review

The deterministic formula translation and the LibreOffice offline recalculation
test provide useful evidence that changed assumptions flow through quarterly,
annual, income-statement, and valuation formulas. Two integration gaps remain in
commit `fe408`:

1. `export_company_workbook` selects the latest company model without requiring
   its specification to belong to the Cockpit's current mission. Exact spec and
   input hashes prevent tampering but do not prevent a prior-mission model from
   being exported without a historical label.
2. The Cockpit download supplies no fiscal-calendar binding, and the exporter
   does not resolve one from an authority. Normal UI exports therefore omit all
   annual columns and report the calendar gap, so the annual/quarter bridge is
   not available through the product path.

These XLSX findings were sent to the owning agent. This branch does not modify
the XLSX exporter.

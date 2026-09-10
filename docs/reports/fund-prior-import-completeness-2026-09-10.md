# Fund Prior Import Completeness

Date: 2026-09-10

## Result

Prior Excel imports no longer stop silently after the first 5,000 cells. The
default reader deterministically covers every populated numeric or formula cell
in every worksheet's used range. Operators may supply an optional JSON read
budget to the import CLI. Every applied sheet, row, column, or cell limit is
recorded with its truncation boundary; a constrained artifact cannot claim
`complete=true`.

New imports publish PriorModelVersion schema 0.2 with:

- workbook sheet order, visibility, dimensions, and imported extent;
- exact formula-map hash and explicit completeness/truncation metadata;
- formula text, number format, font color, cell role, period label, period
  status, and source type per imported numeric/formula cell;
- the existing `prior_human` kind for every cell.

Only an explicit `E` period suffix is classified as forecast. Other dates remain
`unknown`, because a historical date may contain an actual, a consensus number,
or an older forecast. No imported workbook cell enters actual or verified-figure
authority.

Legacy callers that publish ordinary row lists still produce and read the exact
schema 0.1 shape. Existing stored records therefore retain their bytes and hashes.
An enriched import replays as a duplicate when its cells and import metadata are
unchanged.

## Office artifact projection

The existing governed prior-research wire remains unchanged. The additive
`read_document_artifact` function exposes an inert structure manifest alongside
the existing header and text.

For XLSX it exposes full sheet topology plus all numeric/formula cell locations,
formula text, period/source/style roles, and formula-map identity. Cached numeric
values are deliberately omitted from this structure view. External workbook
links are counted but their targets are not exposed or followed.

For DOCX it preserves ordered paragraph/table blocks and their text hashes,
relationship order, and SHA-256 identities for media and embedded workbook parts.
Embedded objects are never opened or executed. External relationship targets are
hashed and redacted. The projection states its losses: layout, styles, and text
rendered inside chart images remain outside the text reader.

## Private sample acceptance

The Desktop workbook was read locally without refresh or link execution. The new
reader returned 41,634 cells across all 18 sheets with `complete=true`, ending in
the final worksheet rather than the third worksheet. The AMZN Driver and
Financials sheets contained 559 represented cells in columns BG-BI; BG, BH, and
BI were all present. The formula-map hash was deterministic across repeated reads.

The Desktop DOCX manifest found 396 ordered top-level blocks, 52 media assets, and
15 embedded workbooks. Every asset is represented by archive part, byte length,
and SHA-256 identity. No source content or private asset bytes were written to the
repository.

## Validation

`PYTHONPATH=src python3 -m unittest tests.test_prior_import_completeness tests.test_prior_research`

Result: 75 tests passed. Coverage includes complete late-sheet import, explicit
budget truncation, style/period/source classification, v0.1 compatibility,
v0.2 replay idempotency, unknown provenance, DOCX body/asset ordering, and the
additive document artifact read path.

## Exporter contract

An exporter may use the import metadata to reproduce sheet order, period labels,
formula text, and formatting roles. It must continue treating these rows as the
fund's historical assumptions. Current actuals and active forecasts must come
from their respective authorities. `formula_map_hash` binds the imported formula
topology; it is not evidence that Excel recalculated the workbook successfully.

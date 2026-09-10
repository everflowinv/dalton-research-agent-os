# Investment Memo collector authority repair

The Memo collector now reads the required dossier, forecast model, sensitivity projection, and valuation snapshot through their authorities. A sensitivity projection is accepted only when its model-version reference, model-version hash, and model-input hash all match the current forecast model. Optional raw-table inputs remain optional, but a row is included only when its embedded hash and database hash both equal a fresh canonical recomputation.

An unchanged Memo input now skips that company and continues through the remaining eligible mission universe. Missing and unchanged companies are returned in `skipped`; absent optional sources are returned in `optional_missing` and do not become new gates.

The focused Store fixture covers an incomplete first company followed by a ready company, an unchanged first company followed by a ready company, current-mission Deep Gate selection, exact sensitivity/model binding, rejection of a mismatched sensitivity input hash, and preservation of the complete Claim JSON context.

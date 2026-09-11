# Company financial statement structure foundation

The current company model specification is already company-specific. Its
prompt receives the company's filed presentation (`parent_concept`, level,
dimension) and its verified output chooses the filed revenue anchor and expense
basis concepts. The remaining arithmetic is not company-specific:
`model_forecast_driver.compute_results` uses one fixed revenue/cost/operating
expense/tax/net-income chain and has no authority for non-operating items,
continuing/discontinued operations, attribution to non-controlling interests,
or diluted shares and EPS.

`company_financial_statement_structure.py` adds the closed validation boundary
for replacing that arithmetic. It is an extension of the held company model
specification, and it binds the financial-only model input projection. The
projection includes exact filed cells and their accession refs but deliberately
excludes the broader company `state_hash`, so unrelated research changes do not
invalidate a verified statement structure.

A structure proposal may use only concepts in those inputs. It describes filed
and derived lines and a directed acyclic graph of `sum` and EPS `divide`
formulas. Every derived line needs one formula, each formula cites held operand
filing evidence or note evidence replayed through a caller-supplied authority
resolver, and any tie-out names an exact filed concept with the same statement,
period kind, and unit. Revenue must be the exact anchor selected by the current
company spec; expense concepts selected by that spec cannot disappear from the
extension.

Historical replay intersects the periods available for every operand. A
missing item therefore makes that period unavailable rather than becoming
zero. Sum tie-outs are exact. EPS division is compared at the precision the
company filed in each period. A structure with any formula that has no complete
historical tie-out is not eligible for forecast use.

Annual amount aggregation requires exactly four contiguous duration quarters
with one fiscal calendar, definition, and unit. Diluted EPS uses four quarters
of the company's disclosed diluted-EPS numerator divided by one directly filed
annual diluted weighted-average share value over the identical fiscal window.
The numerator may include preferred-dividend, participating-security, or
convertible adjustments only when this company disclosed evidence for them;
parent-attributable net income is not treated as a universal substitute.
Quarterly EPS and quarterly share counts are not summed or averaged into annual
EPS.

The next forecast integration must consume
`forecast_structure_binding(structure, replay, financial_inputs)`. That call
revalidates the structure bytes, financial input authority, and historical
replay and emits an immutable binding. The integration then needs a new
`formula_ref`/`formula_hash` and result construction driven by the bound graph.
Until that change is made, the installed forecast path continues to use the
legacy fixed formula and must be described that way. No note-document authority
is currently loaded by `company_model_inputs`; callers must supply a real held
note resolver or omit note evidence.

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

Company Model Spec 0.3 now proposes this definition in its existing model call.
The task hash moves, so a held 0.2 spec for the same filed state does not block
the new decision. The definition is stored in the existing immutable spec
metadata; historical specs without it remain readable and continue to identify
the legacy forecast path. `materialize_financial_statement_structure` binds the
same persisted definition to each current financial-input version and reruns
all historical checks, which lets a new filing actualize a model without
pretending the old input hash is still current.

The 0.3 financial-input table loads every filed concept and formula tie-out
named by that persisted definition, including concepts that are neither an
economic driver nor an expense row. It also keeps authoritative duration facts
separate from quarterly cells, so a direct annual weighted-share disclosure is
not discarded or confused with a quarter ending on the same date. Conflicting
values for one concept, period, and filing are retained as an ambiguity and
excluded from arithmetic; parse order does not choose between a rounded and an
exact-looking fact when the source ledger has no precision field to authorize
that choice. Legacy specifications still produce their unchanged 0.2 input
shape.

A structure proposal may use only concepts in those inputs. It describes filed
and derived lines and a directed acyclic graph of `sum` and EPS `divide`
formulas. Every derived line needs one formula, each formula cites held operand
filing evidence or note evidence replayed through a caller-supplied authority
resolver, and any tie-out names an exact filed concept with the same statement,
period kind, and unit. Revenue must be the exact anchor selected by the current
company spec; expense concepts selected by that spec cannot disappear from the
extension.

The proposal cannot call a collection of independently forecast filed totals a
complete statement. At least one company-presented final earnings result must
be formula-derived and tied to its exact filed result. Filed gross profit,
operating income, pretax income, continuing income, net income, attribution
totals, and EPS remain historical/tie authorities and must be marked
unavailable for direct forecasting. The formula graph, rather than an
independent growth rate on each subtotal, carries the selected company bridge.

The closed common roles do not impose one universal income-statement topology.
An exact filed line with a company-specific position uses
`company_presented_component`; a company-specific derived subtotal uses
`company_presented_subtotal`. Its formula says where the item belongs and must
still match the company's historical filed subtotal. This supports, for
example, an after-tax equity-method item in the net-income bridge without
pretending every company has that line or moving it into operating income.

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

A read-only five-company production-state audit found 29--59 consolidated
income presentation rows per company, below the 200-row prompt limit. CTSH's
filed presentation includes a separate after-tax equity-method line between
pretax income and net income, confirming the need for company-presented bridge
components. IBM's current ledger contains rounded and exact-looking diluted and
basic share values for the same filing and period; those facts are now retained
as ambiguous and excluded from formula arithmetic. The current statement input
does not contain note text or an explicit diluted-EPS numerator authority for
any of the five companies, so a new structure must leave diluted EPS
unavailable unless a later held note/formula source supplies that authority.

The forecast integration must consume
`forecast_structure_binding(structure, replay, financial_inputs)`. That call
revalidates the structure bytes, financial input authority, and historical
replay and emits an immutable binding. The integration then needs a new
`formula_ref`/`formula_hash` and result construction driven by the bound graph.
Until that change is made, the installed forecast path continues to use the
legacy fixed formula and must be described that way. No note-document authority
is currently loaded by `company_model_inputs`; callers must supply a real held
note resolver or omit note evidence.

The representative ACN test request is 15,588 prompt characters plus a 9,201
character provider schema. Its complete 0.3 response is 3,505 characters,
inside the existing 120,000 input / 6,000 output-token model-spec limits. No
budget or timeout was increased for this contract.

This first structure contract covers duration income-statement arithmetic. It
does not yet claim company-specific balance-sheet, cash-flow, or operating-driver
forecast coverage; those remain explicit downstream gaps rather than inferred
relationships.

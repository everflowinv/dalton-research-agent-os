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

The forecast integration consumes
`forecast_structure_binding(structure, replay, financial_inputs)`. That call
revalidates the structure bytes, financial input authority, and historical
replay and emits an immutable binding. Forecast model 0.3 stores that binding,
the complete company graph, its replay, assumptions and result cells. Revision,
sensitivity and export recompute the same graph. Historical structures without
this extension remain readable on the legacy fixed formula path. No
note-document authority is currently loaded by `company_model_inputs`; callers
must supply a real held note resolver or omit note evidence.

Structure 0.2 adds a separate `annual_forecast_method` on diluted weighted-
average shares. `quarterly_growth` does not authorize an annual average. The
only executable method, `day_weighted_quarters`, first has to reproduce a
direct annual filed share value from four positive contiguous filed quarter
averages at the precision the company reported.

`company_model_annual_projection.py` executes that method outside the workbook.
It binds the exact forecast model version and hash, model-input hash, structure,
historical replay, forecast-structure binding, and the verified annual filing's
fiscal-calendar authority. Persistence re-reads that exact stored 10-K and its
statement lines, checks company, form, report date and source hash, and refuses
a caller-rehashed calendar that differs. It combines filed and forecast quarters only when
the four source windows are contiguous, calculates the company-specific
diluted-EPS numerator through the held DAG, day-weights the four diluted-share
quarters, and emits a self-hashed projection with each source model cell or
filed input ref. The same projection carries every structured income result's
annual outcome under that line's declared `sum_quarters`, `direct_annual`, or
ratio semantics. A missing quarter or invalid denominator remains unavailable.

The forecast runtime persists one immutable projection beside the model version
and backfills it idempotently when an unchanged structured model predates the
table. The human-readable model report and the workbook consume that same
projection. Excel translates the bound source cells into visible formulas and
records the projection ref/hash; structured-model annual income totals, shares,
and EPS no longer first come into existence in Excel. When a filed actual
supersedes an estimate, later structured result cells replay from that actual
with the same stored assumptions and graph, keeping formula and value aligned
without choosing a new judgement. Without the explicit annual method
and historical tie, forecast annual shares and annual EPS stay unavailable.
Structure 0.1 bytes and replay identity do not gain the new field.

The current fiscal grouping binds the month of the exact annual filing report
date. It does not infer a 52/53-week calendar whose fiscal close can cross a
month boundary. Such a company must remain unavailable until an exact fiscal
period calendar authority is present; a month label is not evidence for one.

The current structure-0.2 test request is 17,012 prompt characters and its
provider schema is 9,520 characters. The earlier complete representative
response was 3,505 characters; the additional annual method is one closed
nullable field per line. These remain inside the existing 120,000 input /
6,000 output-token model-spec limits. No budget or timeout was increased for
this contract.

This first structure contract covers duration income-statement arithmetic. It
does not yet claim company-specific balance-sheet, cash-flow, or operating-driver
forecast coverage; those remain explicit downstream gaps rather than inferred
relationships.

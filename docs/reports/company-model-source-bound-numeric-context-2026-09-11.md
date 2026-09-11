# Company-model source-bound numeric context

## Scope

The company-model specification prompt previously carried filing structure but
no dated amounts.  It could name a company-specific bridge, but it could not
see whether operating income, non-operating items, tax, and net income actually
tied for the periods held in Core.  It also asked the model to decide whether
annual diluted-share weighting was supported without showing the quarter and
annual windows.

This change adds a bounded numeric-period projection to the existing company
model state.  It does not retrieve a source, infer a fiscal year, interpret a
note, fill a missing value, or change the output schema.

## Authority and bounds

The projection reads only the same latest eight immutable statement filings
already selected by `build_company_model_state`.  Each included cell preserves
the exact stored value and unit strings, statement, concept, dimensions,
period start/end, balance, filing form, accession, line id, and the persisted
filing hash.  It computes an inclusive duration and the existing typed period
shape (`quarter`, `cumulative`, `instant`, or `unknown`).  The canonical hash
of the complete authority-reader line is retained beside the cell.  Tests
recompute this hash from `statement_lines` and compare it to the prompt input.

The closed optional model-config block is:

```json
{
  "model_spec_numeric_context": {
    "max_periods_per_series": 8,
    "max_total_cells": 300
  }
}
```

Those documented values are also the compatible defaults when the key is
absent. Both bounds must be positive integers; owner-selected larger values
are not silently clamped. They are ceilings on evidence selection, while the
effective `model_spec.max_input_tokens` call budget is the ceiling on the
complete UTF-8 prompt. The latter comes from the same validated model config
and packaged fallback used by `CockpitModel`; it is not a second independent
limit. The projection reports available, post-series-cap, post-total-cap,
included, and omitted-by-each-bound counts, plus the no-cell base prompt and
final rendered prompt byte counts. Selection gives each income series its
newest period before older periods, then cash and balance series, rather than
allowing one long series to consume the context.

Plain model-role reinstall preserves this block after validating its closed
shape, just as it preserves the owner call, run, retry and repair policies.

For a restated period, the newest `filed` date wins.  Different values for the
same series and period in that same newest filing remain together with one
`numeric-period-ambiguity` ref.  A total-cell bound omits the whole ambiguity
group when it cannot fit; it never shows one side as if it were authoritative.

## Runtime binding

The policy, selected cells, omissions, line hashes, and filing authorities are
inside `numeric_context.content_hash` and the company `state_hash`.  The parent
lane reads the exact same validated config as the child.  A policy or filed
value change therefore produces a new state and ticket, while the child still
checks the expected state before any model call. The model-spec task and
prompt contracts advance to `task:company-model-spec:0.8` and
`company-model-spec-prompt:0.9`; structured repair remains bound to that task
and cannot replay an answer from the vocabulary-only prompt.

The prompt renders a compact period table with exact values, dates, dimensions,
accessions, forms, line-authority hashes, ambiguity state, and a separate
filing-authority catalog. The selector measures the complete rendered prompt,
including its fixed instructions, output schema, statement structure, filing
catalog and omission metadata. It admits only complete period groups that fit.
A same-filing conflict is therefore either visible in full or omitted in full.
If the fixed no-cell prompt itself exceeds the configured budget, the state
builder returns a typed report with the base bytes, limit and overage instead
of silently skipping the company.

The five-company copied-state audit that exposed the original line-count bug
now produces prompts of 119,886 (IBM), 114,079 (CTSH), 119,922 (EPAM), 119,917
(ACN), and 119,946 (DXC) bytes under the installed 120,000-byte effective
limit. Before this correction four of those five prompts were
125,157–139,529 bytes and would have been refused before a model invocation.

## Verification boundary

Fixtures cover quarter and annual-duration windows, exact decimal-string
preservation, newest-filing restatement choice, same-filing conflicts, whole
ambiguity-group truncation, source and line hashes, explicit omission counts,
policy-driven state identity, launcher/child policy agreement, and the normal
lane state selection.  A company bridge fixture proves the prompt shows
operating income 100.00, non-operating income 15.00, pretax income 115.00, tax
20.00, and net income 95.00 from the same dated filing, so the model no longer
has to infer `operating income - tax = net income` from vocabulary alone.

Typed note evidence remains unavailable in this slice.  The prompt says so,
and neither concept labels nor numeric proximity are treated as note semantics.
No live state, source call, model call, or deployment was performed.


## Integration and next freeze

Final reviewed author tip `1cad3053f0d75af1b5a4a7dccdb8772f114a8f59` is integrated as `f4968cec`, `53148a76`, `3ea4a845`. Independent review passed 141 focused tests; root integration passed 188 tests in 4.166 seconds, including legacy financial inputs and statement structure. Frozen R16 source `3ea4a84563fc6fbd349c025c9602cf5d177addd7` is pushed and running full discovery and fresh copied-state rehearsal. Its 606-file wheel/three embedded JS checks pass, wheel SHA `df30eabd6bce37edcf32cd0dd34ae60cd1e56e75051bacbfe18982acf4d3e483`. It is not deployed; R15b runtime acceptance is independent.

Next foundation work is typed original-document financial-note evidence, initially EPS numerator applicability. Three isolated branches own planner target identity, canonical-promoted text-evidence resolution, and exact-period statement replay; production state/prompt/forecast plumbing follows their shared contract. Annual Note 3 evidence alone must not create quarterly readiness. Full statements, segments/category/Street and investment-return decisions remain distinct capabilities, not implied by this numeric-context change.


## R16 environment failure retained

The frozen 7,670-test run ended with 3 failures, 791 errors and one skip during host ENOSPC; receipt/log/native evidence is retained in packet `foundation-r16-release/failed-space-*`. Log SHA `1337312f81fe25334065a7a758e0b0cc6654a6d5c4a1bc2d8d8b8423935dc4cd`; native SHA `827a6cba4e6dc2f81f9f12dad70a5a373adf9ee0a754f1023a875dfc7f78ec7e`. The copy rehearsal also failed before report creation. All 791 error traces are disk-full or disk-I/O failures; the three child-process assertions still require revalidation in the clean rerun. No acceptance is inferred from the earlier focused tests or wheel. The next source adds a configurable copy-space admission and in-copy reserve check before repeating validation with restored headroom.

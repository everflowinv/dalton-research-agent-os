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
absent.  Both bounds must be positive integers; owner-selected larger values
are not silently clamped.  The projection reports available, post-series-cap,
included, and omitted counts.  Selection gives each income series its newest
period before older periods, then cash and balance series, rather than allowing
one long series to consume the context.

For a restated period, the newest `filed` date wins.  Different values for the
same series and period in that same newest filing remain together with one
`numeric-period-ambiguity` ref.  A total-cell bound omits the whole ambiguity
group when it cannot fit; it never shows one side as if it were authoritative.

## Runtime binding

The policy, selected cells, omissions, line hashes, and filing authorities are
inside `numeric_context.content_hash` and the company `state_hash`.  The parent
lane reads the exact same validated config as the child.  A policy or filed
value change therefore produces a new state and ticket, while the child still
checks the expected state before any model call.  The model-spec task and
prompt contracts advance to `task:company-model-spec:0.7` and
`company-model-spec-prompt:0.8`; structured repair remains bound to that task
and cannot replay an answer from the vocabulary-only prompt.

The prompt renders a compact period table with exact values, dates, dimensions,
accessions, forms, line-authority hashes, ambiguity state, and a separate
filing-authority catalog.  A synthetic ceiling case with 150 structural lines
and the full 300 numeric cells rendered to 88,195 bytes, below the existing
120,000-byte model input allowance.

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

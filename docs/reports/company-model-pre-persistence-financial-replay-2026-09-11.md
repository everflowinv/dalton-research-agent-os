# Company-model pre-persistence financial replay

## Problem

The model-spec lane previously persisted a schema-valid company specification
immediately after structural validation.  The production forecast path only
later built the source-bound financial input authority and replayed the
company-specific formulas against filed values.  A formula could therefore be
well formed but numerically false and still become the current specification.

## Boundary

`run_model_spec` now uses the shared
`CoverageMissionAuthority.record_validated_company_model_spec` boundary.  That
boundary derives the exact specification id used by persistence, builds the
financial input authority from the same authority readers, materializes the
statement structure, and runs its historical replay before writing.  Schema
0.4 cash selections are also validated through the production cash-flow
companion builder.  The validated method rejects an existing row at the same
identity when its content hash differs from the candidate; it cannot report a
different candidate beside an older stored result.

An actual false tie, ambiguous filed series, invalid unit, period, or selected
cash source is a semantic refusal and writes no company specification.  The
existing structured-output repair remains limited to format and text-length
errors; this boundary does not retry a semantic refusal.  A valid formula for
which the source has no complete historical period remains `unavailable` and
may be stored, so absence of evidence is not converted into a false value or a
whole-spec rejection.

The result summary records the source-bound financial input hash and the
materialized structure ref/hash.  Existing schema 0.2 rows remain byte-readable
and a newer task contract naturally reopens the same filed state without
mutating the historical row.

## Verification

- A schema-valid `pretax + tax = net income` candidate is refused against filed
  values `100`, `20`, and `80`, and leaves zero persisted specifications.
- A corrected `pretax - tax = net income` response becomes current and the real
  forecast CLI publishes from that exact specification.
- An old schema 0.2 row remains byte-identical when the current task contract
  reopens its company.
- A different schema-valid candidate at an already occupied specification id
  is rejected while the original immutable row remains unchanged.
- The company-model CLI, schema, input, financial-structure, and forecast test
  modules pass together.  Existing ambiguity, unit, period, and unavailable
  coverage in those modules exercises the production materializer used by the
  new boundary.

No live state, model call, or deployment was performed.

## Independent integration review

Root accepted author `f6f7d023cb1708bad6d9e981a245ec9b6e5f57a5` after reviewing the shared candidate identity, real-value materialization before persistence, whole semantic refusal and same-ID conflict behavior. Independent CLI/forecast/spec/structured-income/cash tests passed 121 cases in 11.631 seconds. The false-history test was then strengthened to enable one configured format-repair attempt; it still made exactly one model call, refused before current-spec persistence, and passed independently. Integration commits are `86f5a596` and `ae5521b0`. The existing storage method remains compatible for historical/manual callers; the automatic production path uses the validated authority method. R15a is not yet frozen or deployed; the separate old-model input reconstruction regression remains under review.

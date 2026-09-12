# Company-model financial-note authority plumbing

## Implemented contract

The company-model specification path can now consume a canonical promoted
financial-note result without treating prose as a number.  The read-only
`FinancialNoteReadContext` first queries Core for a promoted admission whose
archived planner inquiry contains the closed diluted-EPS-numerator target.  If
there is no such admission, it returns no context and does not read document
configuration or open the document registry, ModelRouter, or CandidateStaging.

For a typed admission, the context reconstructs the existing document registry
from read-only manifest, receipt, and raw-spool readers.  It opens the existing
ModelRouter and CandidateStaging SQLite files in query-only mode and invokes
`resolve_financial_note_evidence`.  That resolver replays the exact admission,
mission, filing, promotion, Ledger rows, candidate bundle, model route and
invocation, original-text search proof, and cited passages.  The model state
contains a self-hashed bounded projection with:

- the exact 11-field structure binding;
- the full evidence authority ref/hash;
- original source, registration, and search-proof refs/hashes;
- the promoted normalized qualitative statement and exact cited passages; and
- the exact document-research configuration semantic hash and file hash.

The whole rendered prompt, including this fixed note block, is measured by the
existing `model_spec.max_input_tokens` authority.  Numeric rows may still be
selected under that bound as complete conflict groups.  Note records are never
silently truncated; an oversized fixed prompt produces the existing typed base
prompt refusal.

The response may cite only a binding ref displayed in this state.  Only cited
bindings are passed to statement-structure validation.  Before parsing every
initial or repair response and again before persistence, the binding is freshly
resolved from the authoritative source.  The parent-selected state and child
state carry the same state hash and company-model task hash, so a configuration,
promotion, source, passage, filing, or execution-proof change rejects the child
as stale.  A caller-created lookalike binding fails the fresh resolver
comparison.

The deterministic forecast parent, digest, pre-persistence replay, and forecast
child all pass the same resolver into the persisted structure materializer.
For a note-backed structure, the forecast digest additionally binds the exact
configuration hashes, full prompt-context hash, cited evidence-authority hashes,
and cited prompt-record hashes.  The launcher passes that digest to the child,
which recomputes and compares it before any forecast write.  A config or source
change in that interval therefore produces a controlled refusal.
Historical schema 0.1/0.2 structures do not open this resolver and retain their
existing wire shape.

## Period and numeric boundary

The note target and structure both use the shared inclusive duration limits:
80–100 days for a quarter and 291–380 days for an annual period.  The state
structure validator reads the exact filing ingest and content hash already held
in `numeric_context.filing_authorities`; the compact top-level filing display is
not substituted for that authority.

The first capability is deliberately narrow.  A note authority can establish
which filed attribution component belongs in the diluted-EPS numerator for its
exact periods.  It supplies no amount.  Filed statement facts still provide all
operands, shares, and diluted EPS used by the historical precision tie.  An
annual note does not create quarterly numerator values or forecast readiness.
Until independently authorized quarter evidence exists, the model remains
honestly unavailable at that dependent forecast boundary.

## Production call sites

- `company_model_state.build_company_model_state`: hashed prompt projection.
- `company_model_spec.spec_from_response`: cited-binding selection and fresh
  structure validation.
- `company_model_cli`: child state drift check, repair validation, and
  pre-persistence replay.
- `coverage_mission`: shared validated persistence boundary.
- `mission_model_spec_lane`: parent state selection.
- `company_model_forecast.model_digest` and `run_company_forecast`: immutable
  model identity and replay.
- `mission_model_forecast_lane` and `company_model_forecast_cli`: parent/child
  read-only resolver construction.

## Verification

The production-plumbing regressions cover the no-promotion zero-open path, a
real four-stage promoted directed-document authority, exact source replay,
configuration drift, a self-consistent manual binding substitution, complete
prompt byte accounting, cited-only binding selection, and an actual
CoverageMission statement ingest through parse, validated persistence, fresh
materialization, an honest not-ready forecast result without a child launch,
and parent/child forecast digest binding.  Existing company
state/specification, input, specification lane, forecast lane, model forecast,
statement structure, and financial-note authority suites remain green.

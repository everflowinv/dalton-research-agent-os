# Forecast filing proof: cumulative derivations and legacy backfill

Forecast filing proofs now replay both reported quarters and quarters derived from cumulative statements. A derived quarter is accepted only when two authoritative, undimensioned rows have the same concept, unit and cumulative start; the earlier period ends immediately before the derived quarter starts; the later period ends with it; their exact per-row accessions equal the model cell's accessions; and later minus earlier equals the model value. Reported cells likewise require their exact row accession rather than any accession from the filing batch.

An unchanged model without a proof now takes an explicit `filing_proof_backfill` action. The authority recomputes the model inputs and filing reconciliation, inserts only the separate append-only proof, and leaves the existing forecast model JSON, content hash and version chain unchanged. An unchanged model that already has a proof remains `nothing_to_do`.

Validation: all 26 `tests.test_company_model_forecast` tests pass. New tests cover real Store statement ingestion of Q1, six-month, nine-month and annual cumulative values, deterministic derived-quarter publication and replay, a wrong derived difference, and proof backfill without a new or modified model version. Existing tests cover forged values, units, dimensions, company bindings, accessions, solver replay and proof hash tampering.

The normalizer chooses the newest filed value independently for each exact period. The proof therefore permits a derived quarter whose two cumulative operands come from different filings only when both exact immutable rows and their individual accessions are named. It does not treat another accession from the same batch as evidence for either operand.

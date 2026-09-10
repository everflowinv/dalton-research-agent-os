# Company attribution recovery — 2026-09-10

## Root cause

AlphaEngine discovery is targeted by `company_ref`, but a broad search can return another issuer's document. The mission queue correctly retains which company/search requested the result; that field is not evidence that the returned document is about that company. The source-base reader nevertheless counted the queue's target and derived a fiscal period from the title alone. Live CTSH rows therefore treated Nordic Semiconductor, Remitly, and SoftBank calls as CTSH coverage.

## Repair

For the closed `earnings-call-transcripts` spec, a period now counts only when immutable source provenance exists and both the source title and source-provided named-company list identify the target covered company. The existing deterministic company-name authority supplies the exact ticker/name aliases. A target transcript may mention peers and still qualify; a peer transcript that merely mentions the target does not. Rows without this proof remain byte-for-byte unchanged and are reported as `not_attributed`; they establish no period anchor and are not manually relabelled.

This is a read-side recovery. A future qualifying ingestion must carry the real issuer in source metadata and a title naming the issuer. Existing wrong-company records remain available for audit and research review, but cannot close the earnings-call gate.

## Validation

- Wrong-issuer Q2/Q3/Q4/Q1 examples associated with CTSH yield zero qualifying quarters and unknown period coverage.
- A Cognizant earnings conference call that mentions EPAM qualifies for CTSH.
- Missing provenance remains non-attributed rather than raising or being counted.
- `tests.test_mission_stage`, `tests.test_discovery_satisfied_gate`, `tests.test_stage_reopen_ledger`, and `tests.test_research_planner`: 129 passed.

## Issuer-position refinement

A company mentioned anywhere in a title is not necessarily the issuer. The final rule requires the target in a closed issuer zone: before the fiscal-quarter token, or between that token and the earnings/conference/post/investor-call marker. If another covered issuer also appears in that zone, attribution is ambiguous and refused. The source named-company list must independently name the target. Thus `Remitly Q2 2026 Earnings Call — Cognizant comparison` cannot count for Cognizant, while `Q2 2026 Cognizant Earnings Conference Call — EPAM comparison` can.

The same earnings-specific source-metadata check now feeds the existing qualitative, numeric, and metric-discovery admission floor. General industry and multi-company research retain their existing body-level attribution behavior.

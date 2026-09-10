# Event verifier provider-contract repair — 2026-09-10

The Event Judgement and thesis-reflection verifier paths now bind their real
closed output contract and packaged schema content hash into each immutable Cockpit WorkOrder. Producer calls
remain unchanged. The contract reference participates in the WorkOrder
identity. The lane configuration signature also includes both Event verifier
contract fingerprints, so a settled all-refused batch is dispatched again
after this code/schema repair. Repaired verifier calls cannot collide with the seven persisted
pre-repair failures.

The adapter maps the opaque contract ref to one packaged JSON Schema. It also
checks the schema version and allows that contract only for
`event_judgement_verifier` and `thesis_reflection_verifier`. Unknown contract
refs, a mismatched version, a different purpose, caller-provided paths, and an
additional thesis binding mode all fail before transport. The schema exactly
matches `validate_verifier_output`: only `verdict` and `findings`, the same two
verdicts, the same five finding codes, and the same closed finding fields.

Read-only replay of the seven live persisted verifier WorkOrders reconstructed
their repaired metadata and their exact stored route decisions. All seven then
constructed `requiredControls` with schema name
`event_judgement_verifier_provider_output_v0_1`; no broker or model was called.

Validation:

- 35 Cockpit fallback and adapter tests passed before the broader run.
- 129 relevant Cockpit fallback, adapter, Event Judgement, and retry tests passed.
- Focused tests prove the provider controls reach the transport boundary, the
  packaged schema is the event schema rather than the thesis schema, the new
  identity differs from the failed legacy work, and unknown/cross-purpose
  contracts fail before transport.
- The schema is explicitly included in wheel package data.

Other `independent_model_call` consumers remain fail-closed on their existing
missing provider contract. They need their own schemas matching their semantic
validators; this change does not assign the event contract to dossier,
zero-base, earnings, Deep Insight, industry framework, or quality scoring.

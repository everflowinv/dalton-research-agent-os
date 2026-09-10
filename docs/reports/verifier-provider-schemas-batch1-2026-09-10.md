# Verifier provider schemas — batch 1

The dossier, Deep Insight Gate, and industry-framework independent verifiers now carry packaged, hash-bound structured-output schemas to the provider boundary. Dossier and industry framework share a schema because the latter deliberately reuses the dossier semantic validator; Deep Insight has its own question-addressed finding shape and exact finding vocabulary.

The contract ref and canonical schema hash enter CockpitModel WorkOrder identity. Each coordinator's persistent input signature also includes its verifier contract fingerprint, so a changed packaged contract can retry an otherwise unchanged refused input after deployment.

Validation: 187 focused adapter, schema/validator golden-payload, CockpitModel, coordinator, packaging, and lane tests passed. No transport or model call was made.

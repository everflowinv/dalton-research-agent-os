# Quality verifier runtime wiring — 2026-09-11

`research-quality score` can now take an explicit independent verifier model
configuration. The judge remains optional, and the verifier requires the judge:
an existing invocation with no verifier argument still performs exactly the
same deterministic or judge-only work and reports `verified: false`. Installing
the optional verifier configuration does not add a scheduled lane or trigger a
model call.

The verifier uses the registered `quality_verifier` purpose, its own routing
policy, credential slots and purpose-specific call budget. Model-family
independence remains enforced by the existing independent-call boundary before
the verifier is sent. A successful verifier result records its work,
invocation, route, cost and judged-score binding. The persisted scoring identity
adds the actual verifier route only when one exists; legacy deterministic and
judge-only fingerprints remain unchanged byte for byte.

The macOS installer accepts an optional
`DALTON_QUALITY_VERIFIER_MODEL_PROFILE` or
`DALTON_QUALITY_VERIFIER_MODEL_TIER`, validates it before filesystem mutation,
and writes `quality-verifier-model-config.json` through the shared append-only
policy/config installer. Reinstall therefore preserves validated call and run
budget overrides and the credential references required by retained Cockpit
selection overrides.

Validation covered 223 quality, routing, setup, installation, credential and
budget tests, plus shell syntax, Python compilation and diff checks. No live
configuration, model call or deployment was performed.

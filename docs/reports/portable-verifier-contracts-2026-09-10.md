# Portable verifier contracts — 2026-09-10

## Finding

The installed OpenClaw 2026.9.3 Google provider-control boundary accepts a closed JSON Schema keyword set. It rejects `minLength`, `maxLength`, `pattern`, and `uniqueItems` before provider transport. Nine packaged Dalton verifier provider schemas used at least one of those keywords. The broker catches that host exception and returns the intentionally redacted `HOST_COMPLETION_FAILED: host completion failed`, which hid the schema keyword that caused the refusal.

A read-only check of the live broker journal found the same generic failure for eight event-verifier attempts and two dossier-verifier attempts after the controlled Gemini route selection. No provider call was made by this audit. This establishes local host-contract incompatibility; it does not claim a successful paid provider response.

## Change

All packaged purpose-specific provider schemas now use only the installed Google portable subset. The domain parsers remain the final closed validators and continue to enforce exact keys, enum membership, non-empty strings, length bounds, verdict/finding consistency, citation membership, and other purpose semantics. Removing unsupported provider keywords therefore weakens neither publication nor authority admission; it only permits the host to enforce the portable structural subset before Dalton applies the full contract.

The verifier schema content hash already enters each Cockpit `WorkOrder` identity. Debate-map and conviction-call scheduling had an outer idempotency gap: their persisted ticket and lane-failure keys used only business evidence. A prior contract refusal could therefore suppress the corrected WorkOrder indefinitely. Their ticket digests and business keys now include the packaged verifier-contract fingerprint. Existing successful publications remain unchanged because current authority heads are checked before launch. An unchanged input with an old refusal gets one new identity after a contract change; subsequent ticks remain idempotent.

## Validation

- 203 existing adapter, Cockpit, debate-map, conviction-call, and lane tests passed.
- 3 new regressions passed.
- Every packaged provider schema crossed the actual installed OpenClaw Google validation function up to its fake `countTokens` seam; no network or model call occurred.
- A persistent content-refusal ledger fixture proves an old contract key remains auditable while a new contract fingerprint is eligible, and both launcher digests change.

## Limits

This validates local broker/host admission and deterministic recovery only. It does not prove a provider response, model quality, or live publication. Provider schema constraints deliberately remain a portable structural subset; full business validation remains in each existing Python parser.

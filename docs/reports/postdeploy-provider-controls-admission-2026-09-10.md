# Post-deploy provider-controls admission repair — 2026-09-10

## Finding

The post-18:50 verifier failures were not provider outages. The broker returned `REQUIRED_CONTROLS_UNAVAILABLE` with the safe detail `profile lacks providerControls; mode missing not advertised` before selecting an agent or model and before reporting cost. The configured aliases existed. Dalton classified the code as `model_unavailable` because the generic `UNAVAILABLE` substring preceded contract errors, then tried every provider in the verifier chain.

The broker enforcement is in `integrations/openclaw-model-broker/src/broker.mjs`: a controlled call requires both a profile `providerControls` declaration and a matching `api.runtime.llm.capabilities.providerControls` transport/mode. The repository broker README explicitly says unsupported transports fail closed. Current public configuration declares provider controls only for `profile:gemini-3-8-flash` and `profile:gemini-3-1-pro-preview`; the failing Claude, Gemini Flash Lite, and ZAI profiles do not declare them.

## Repair

- Classify exact `REQUIRED_CONTROLS_UNAVAILABLE` as `contract_violation` before generic unavailable codes, so fallback halts and the persisted/Cockpit error retains the specific broker code and safe message.
- Project a `provider-controlled-verify` capability only for broker profiles with a structurally recognized public provider-control mode and rate card. Remove that capability when a later catalog version drops the declaration.
- Route provider-contract verifier WorkOrders with that capability. Ordinary producer/research calls remain on `research`.
- Bind the new verifier admission capability into WorkOrder identity so persisted legacy verifier work cannot conflict or replay under the changed contract.
- Include the presence of broker provider controls in the public catalog digest so adding/removing controls causes append-only catalog reconciliation.

This is a necessary profile-side eligibility check. It does not claim the host runtime supports the mode. The broker remains the authoritative runtime gate and will halt with its exact contract error if the host does not advertise the matching capability. To restore verified calls, the owner must select a different-family verifier profile whose public broker profile declares controls and whose running OpenClaw host advertises the matching mode; the code does not weaken the verifier requirement or guess support from a generic `verify` label.

## Verification

`PYTHONPATH=src python3 -m unittest tests.test_model_fallback_chain tests.test_openclaw_catalog_reconcile tests.test_model_catalog_sync tests.test_model_selection tests.test_cockpit_model_fallback`

Result: 150 tests passed. Regression coverage proves the exact error is contract-terminal, the chain makes one broker attempt, only controlled profiles receive verifier eligibility, and provider-contract WorkOrders request the new capability.

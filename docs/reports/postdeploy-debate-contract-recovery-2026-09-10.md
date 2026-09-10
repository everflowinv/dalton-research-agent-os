# DebateMap output-contract recovery — 2026-09-10

The first post-deployment DebateMap run produced a usable JSON shape but its
first question exceeded the parser's 300-character limit. The model prompt did
not disclose that limit, nor the existing 12-debate and 600-character statement
limits. The parser correctly refused the response, but the generation contract
asked the model to satisfy an incomplete contract.

The draft contract is now version 0.3. Its hash includes all three output
limits, and the prompt prints those same constants beside the closed JSON shape.
Changing the contract hash also changes the task and persisted model-work
identity, allowing the corrected request to run without treating the rejected
0.2 request as equivalent. Parser enforcement is unchanged; an oversized
question is still refused whole.

Verification:

`PYTHONPATH=src python3 -m unittest tests.test_debate_map_draft tests.test_debate_map tests.test_debate_map_lane`

passed 129 tests. `git diff --check` passed. No live state, model, connector, or
governance operation was performed.

## Model-chain diagnostics follow-up

The live router admitted each verifier profile, but the broker then returned a
failure and the fallback ledger retained only `model_unavailable`. The broker's
typed code and message were held briefly in `CockpitModel.call`, then discarded
when the chain created its aggregate failure. This made three unavailable
aliases indistinguishable from a generic outage after the fact.

Each attempted link now contributes a bounded diagnostic containing profile,
failure class, broker code, and message. Credential-like assignments are
redacted and every field has a hard size bound. The final formal
`ResultEnvelope` persists these diagnostics and includes their safe text in its
error message, while immutable route-chain links retain their existing schema.
Fallback and charging behavior is unchanged.

The combined focused command covering fallback, Cockpit routing, and DebateMap
passed 176 tests. A real scheduler regression proves that the formal result
survives replay with both distinct broker causes while a token-like value does
not reach SQLite.

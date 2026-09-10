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

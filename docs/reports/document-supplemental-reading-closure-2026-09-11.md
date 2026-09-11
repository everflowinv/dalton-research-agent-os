# Supplemental annual reading closure review — 2026-09-11

## Closed implementation boundary

The supplemental path does not rewrite or delete a historical dismissal. A
human-authenticated reopen preserves the prior row in the append-only reopen
ledger and returns the same review to the normal extraction queue. Reopening
does not count as reading. `mission_stage` reports `read=1` only after the
service validates every complete-source window against formal Scheduler
WorkOrder and ResultEnvelope authority, records an immutable completion proof,
and the normal resolution closes that exact reopened review.

Failed-window admission accepts only two published qualitative extraction task
identities: the frozen legacy task hash and the current prompt-contract task
hash. Arbitrary hashes, other control planes, other tasks, mismatched
documents/companies/missions, truncated source bytes, and drifted formal
results remain ineligible.

The execution packet now has a human-operated boundary. Its default mode is
read-only. Execution requires an interactive terminal and the human to type
the complete candidate hash. Before and after that confirmation it verifies
the manifest hash, candidate bytes, every parameter file hash and semantic
binding, current mission/review state, SEC 10-K issuer authority, and every
formal failed window. The writer then performs the final CAS under the
ephemeral authenticated `human:lumos` principal. The agent has not run this
mode and the candidate remains unsigned.

## Actual read-only result

The strict validator was run against the current Core and Scheduler databases
with current prompt-contract source. It returned `status=reviewed`, `writes=0`,
and exactly three ready review IDs corresponding to the bounded ACN, CTSH, and
DXC annual-report candidates. No source, model, governance, or ledger mutation
was performed.

## Remaining foundation steps

1. The human reviews the exact unsigned candidate and either declines it or
   runs the interactive hash-bound command. This is the only pending decision;
   it is not an automated approval checkpoint invented by the agent.
2. Normal automation reads the reopened reports using complete source bytes.
   A failed or truncated window stays open and produces no whole-document
   completion proof.
3. Product acceptance checks the new proof and normal resolution, then verifies
   acquired and read counts separately. It must not infer comprehension from
   source inventory, claim count, or the historical dismissal rationale.

No additional code blocker was reproduced in the proof/reopen path. Scenario
probability, payoff asymmetry, and market-pricing authority remain later
investment-decision work; they are not prerequisites for truthful document
reading status or the six-stage foundation flow.

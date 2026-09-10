# Investment Memo human decision — 2026-09-10

## Scope

This slice closes the human checkpoint after a verified Investment Memo. It
reuses `MissionDeliverableAuthority`, the coverage mission stage ledger, the
model router decision authority, and the Cockpit's authenticated writer path.
It adds no parallel decision authority and does not sign, publish, or modify a
live Core.

The implementation depends on the closed Investment Memo gate contract from
`406c0dd539e8b174004d7ba7409fc01362bb56d7` (locally cherry-picked as
`3601b27`).

## Contract

`decide_investment_memo` accepts the exact current memo version reference and
content hash, `approve|reject`, a nonempty reason, and the authenticated
`human:` principal supplied by the writer boundary. Before recording a stage
decision it verifies:

- the immutable row and requested hash agree and the row is the current
  deliverable head;
- the memo is bound to the current mission version and its exact hash;
- the closed memo gate validates against a freshly computed verified-material
  hash, including all 12 answered and cited questions and all four checks;
- the top-level invocation references exactly match the four producer work
  orders and verifier work order;
- every model route decision exists, selected that exact work order, resolves
  to a family, and the verifier family differs from every producer family;
- Scheduler has the exact successful formal result, result envelope,
  invocation, and route for all five calls; the verifier's persisted output is
  an exact pass over the memo's verified body hash with no findings;
- Scheduler's immutable WorkOrder has the exact memo producer/verifier purpose,
  mission version and hash, and (for the verifier) the four producer routes;
  the router's frozen work hash must equal Scheduler's WorkOrder hash;
- `company_model` has passed in the mission's folded stage ledger.

Approval appends `investment_memo: entered`, then `gate_passed`, then
`active_coverage: entered`. Each write has a stable idempotency key, so a crash
after the memo verdict can resume the final transition without another human
decision. Rejection appends `gate_failed` and never enters active coverage. A
settled opposite verdict for the exact memo is refused rather than creating
contradictory stage history. An older memo failure does not settle a new head;
an older pass requires the existing reopen mechanism before another memo can
be decided.

Cockpit lists only current active-mission memo heads that have no terminal memo
decision. It displays the authored sections, all 12 question answers, and the
verifier verdict. Only approve and reject are offered. A return action is not
shown because there is not yet a persistent feedback contract that the next
draft consumes.

## Validation

- `python3 -m py_compile` passed for the shared contract, writer, and Cockpit
  modules.
- 106 focused contract/decision/writer/Cockpit tests passed, with one existing
  skip, across `test_investment_memo_contract`,
  `test_investment_memo_decision`, `test_writer_service`,
  `test_cockpit_plane`, and `test_cockpit_int2`.
- 20 additional Cockpit operations-panel tests passed.
- The recovery test injects a crash after the memo pass and proves retry adds
  active coverage without duplicating earlier records. Refusal tests cover
  nonhuman actors, hash drift, producer/verifier family collision, and an
  attempted reversal of a settled human verdict. Further regressions cover a
  selected but unexecuted route, a formal verifier body-hash mismatch, and an
  older failed memo followed by a new head. A real Scheduler and ModelRouter
  test executes producer and verifier calls through their authorities, then
  exercises the same Writer validation helper. Folded-state regressions cover
  a reopened company-model gate and an unrelated old active-coverage entry.

## Remaining boundary

The Cockpit can validate the closed memo body before showing buttons, while
route authority and family independence are authoritatively rechecked by the
writer at decision time. A route removed or changed between page render and
the click therefore produces a refusal and no stage write. Returning a memo
for revision remains intentionally unavailable until a persisted feedback
record is part of the next draft's identity and prompt input.

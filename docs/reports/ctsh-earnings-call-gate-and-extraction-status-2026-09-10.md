# CTSH earnings-call gate and extraction status — 2026-09-10

## Corrected gate result

The prior 2/4 display counted documents filed under an earnings-call discovery
spec. That is insufficient for the Playbook requirement, which asks for four
quarters. The live read-only metadata contains one explicit quarter, Q2 2026.
The other CTSH-attributed candidate is a September 2026 Citi Global TMT
conference and asserts no fiscal quarter or earnings-call status.

The deterministic projection now requires an explicit fiscal quarter in source
metadata and counts distinct periods. It excludes fireside chats and generic
conferences, including ones whose titles happen to contain a quarter. It still
accepts an actual “earnings conference call.” Unknown documents remain in their
review authority and are reported as unclassified; this projection neither
dismisses nor upgrades them.

The gate uses the latest explicit eligible fiscal quarter as its anchor and
requires that quarter plus its three immediate predecessors. Four scattered
historical calls cannot pass. When no title supplies an explicit fiscal-quarter
anchor, required and missing periods remain unknown rather than being inferred
from today's date or from a company's assumed fiscal calendar.

On current production data, evaluated read-only with the corrected projection,
CTSH has `classified_periods = [FY2026-Q2]`, `unclassified = 1`, and a 1/4 gate.
That explicit anchor makes the missing periods FY2025-Q3, FY2025-Q4, and
FY2026-Q1. These are requirements derived from the source's own fiscal label;
they are not claims that matching documents have been found.

## Corrected extraction status

When every queued document view fails before drafting, extraction now persists:

- `status = failed`
- `stop_reason = all_document_views_failed`
- a structured blocked record with the number of reviews and grouped reasons
- a nonempty `failure_reason`

It no longer emits `succeeded / nothing_to_draft`, so the coordinator and
Cockpit cannot confuse a dependency or contract failure with an empty queue.
Normal replay, a genuinely drained queue, and partial per-document isolation
retain their existing behavior.

## Validation

Focused mission-stage, extraction automation, policy-chain, Cockpit projection,
and extraction coordinator tests passed: 56 tests, with one existing skip.
Cases cover a valid call, an earnings conference call, a generic conference
with a quarter in its title, a fireside chat, duplicate fiscal periods, four
nonconsecutive historical periods, wrong-company dismissal, and an all-view
failure. The expanded affected suite passed 117 tests with one existing skip.

# ADR-0008: No research output is ever finished, but nothing revises itself

*2026-09-09*  ·  Status: proposed  ·  Contract for every output-class authority; extends ADR-0004 §5 (append-only stage ledger) and ADR-0007 (the proposal / decision split); does not touch ADR-0001's human admission

## Context

Four companies have `gate_passed` on their Initial Screen, and the selection rule skips a company whose gate has passed. So ACN, EPAM, IBM and DXC will never have their screen rewritten, no matter how much evidence arrives afterwards — and a great deal has: 26 quarterly filings, 10,023 statement lines and nine modelling specifications, all of it dated after those screens were written by a weaker model on thinner evidence. The gate was designed as a stage marker and became a terminal state by accident.

The owner's decision is that a passed screen may be re-issued when the evidence thickens, and that this is not a property of Initial Screens: a forecast line becomes an actual after the company files; a large contract win between earnings revises an estimate; a dossier, a debate map, a valuation snapshot and a thesis all age the same way. Nothing the research produces is a final answer. But the owner was equally clear about the opposite failure: versioning is a mechanism, not a trigger. A system that re-issues an output every time news arrives produces noise and destroys the meaning of a version chain, which is the only place the owner can watch an analyst's understanding change.

## Decision

**No output has a terminal status.** `gate_passed`, `published` and `accepted` are states of a *version*, never of the object. An authority that stores a research output stores a chain, and "the current one" is a pointer, not an end. Old versions are never deleted, because the chain is what the owner reads to see the understanding evolve; a chain with one entry says the analyst never changed their mind, and that is information too.

**Every version says why it exists.** A new version carries a `change_reason` from a closed vocabulary — `filing_actual`, `driver_event`, `assumption_review`, `evidence_thicker`, `human_revision` — and the exact evidence refs that occasioned it. A revision whose evidence refs are already covered by the current version is refused as `duplicate`, not written. This is the guard that keeps re-issuing honest: a version that cannot name what it learned is a rewrite, and a rewrite of an unchanged world is noise wearing a version number.

**Superseded values are kept and marked, never overwritten.** A replaced value stays in place with `superseded_by` naming the version that replaced it. This is not tidiness. An estimate that is overwritten by its actual destroys the material that reconciliation and guidance-style calibration are made of — you can only ask "how wrong were we, and in which direction, and does that bias persist" if what we said before the filing is still there.

**Any output can be replayed by version.** "What did we know and what did we conclude as of version N" is answerable from the chain alone: the version's content, its `change_reason`, its evidence refs, and the refs it was bound to. An output that can only be read as of now is not auditable and does not satisfy this contract.

**An authority exposes revision entry points and never uses them on its own.** `revise`, `actualize` and `reopen` all require a `change_reason` and evidence refs from their caller; no authority publishes a new version because it noticed something. Deciding *whether* and *how* to update is the judgement layer's job, not the storage layer's: in the ResearchEvent flow this is heading toward, each event is mapped by a bounded model call to the drivers and theses it touches and receives one of the Playbook's five decision words, and only a revise-shaped decision calls the entry point. A `NO_CHANGE` decision is recorded with its reason, so the weekly review can answer the question that matters more than any revision — *why didn't we change our mind about this?* The single near-mechanical exception is `actualize`: replacing a historical period's estimate with the filed number, which touches only past periods and asserts nothing about the future.

## Consequences

- `mission_deliverable` and the Initial Screen selection rule must stop treating `gate_passed` as a skip condition once a reopen policy exists. That policy is Wave 3 (P14d) and is named here, not decided here; until it exists the four passed gates stay closed and this ADR changes nothing that runs.
- Wave 1C's `ForecastModelVersion` and Wave 2's dossier, debate map and valuation objects implement this contract from their first version rather than acquiring it later. Retrofitting a chain onto an authority that overwrote is not possible: the old values are gone.
- `gate_reopen` is already in `CHECKPOINT_KINDS` (P14-0) and grants nothing yet. Reopening a gate a company has passed stays a human checkpoint by construction, not a rule the machine can satisfy.
- ADR-0007's split holds at a second layer: automation proposes a revision to a thesis and a person accepts it; automation calls `revise` on a forecast line or a dossier under a mission grant, but only because a judgement step decided to, and the decision is recorded either way.
- ADR-0001 is untouched. A thesis chain is versioned like everything else, and admission to it is still human-only.
- Nothing here is implemented.

# ADR-0008: No research output is ever finished, but nothing revises itself

*2026-09-09*  ·  Status: accepted  ·  Owner decision 2026-09-09, recorded in [parallel-development-plan v1.0 §1](../reports/parallel-development-plan-v1.0-2026-09-09.md)  ·  Contract for every output-class authority; extends ADR-0004 §5 (append-only stage ledger) and ADR-0007 (the proposal / decision split); does not touch ADR-0001's human admission

## Context

Four companies have `gate_passed` on their Initial Screen, and the selection rule skips a company whose gate has passed. So ACN, EPAM, IBM and DXC will never have their screen rewritten, no matter how much evidence arrives afterwards — and a great deal has: 26 quarterly filings, 10,023 statement lines and nine modelling specifications, all of it dated after those screens were written by a weaker model on thinner evidence. The gate was designed as a stage marker and became a terminal state by accident.

The owner decided on 2026-09-09 (recorded in [parallel-development-plan v1.0 §1](../reports/parallel-development-plan-v1.0-2026-09-09.md)) that a passed screen may be re-issued when the evidence thickens, provided it is versioned and old versions are never deleted, because the version chain is how the owner watches the understanding iterate. The owner extended the same rule to every research output, not only Initial Screens: an estimate becomes an actual after the filing; a driver change observed between earnings, such as a large contract win, revises an estimate; dossiers, debate maps, valuation snapshots and theses age the same way.

The owner clarified the constraint that makes this safe in the same session: **versioning is a mechanism, not a trigger.** Not every piece of news re-issues an output. Whether and how to update is a judgement, and the owner put that judgement in a separate layer from the storage. A system in which the authority re-issues on its own produces noise and destroys the meaning of the chain, which is the only place the change of mind is legible.

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
- Nothing here is implemented yet. The decision is recorded; the first authority to carry the contract is Wave 1C's, and the reopen policy that unblocks the four passed gates is Wave 3.

## Addendum, 2026-09-09 (P14-S): a stage state carries across mission versions; the version is provenance

The same confusion this ADR names — a state of a *version* read as a state of the *object* — was live in the stage ladder in the opposite direction. A `coverage_mission_stage_record` binds the mission version it was written under, and every reader scoped its query to that version, so a company's ladder emptied itself every time the owner published a mission version. The live mission rolled v7 → v13 in two days: each roll wrote five fresh `initial_screen entered` rows for the same five companies (35 of the 41 stage records in the Ledger), and the four gates that actually passed live only under v13. Publishing v14 would make all four read as never-screened. It had already been hit twice — P14a's residency had to query `gate_passed` across every version of the `mission_ref` to keep four companies in daily tracking, and P12d's Deep Insight Gate decision died after a roll because `deep_insight_gate cannot be entered before initial_screen gate_passed` refused a gate that had demonstrably passed.

**A stage state is a fact about `(mission_ref, company_ref)`, not about `(mission_version_ref, company_ref)`.** It carries forward across versions until something supersedes it. The mission version each record binds is **provenance** — it says under what mission, and when, the state was reached, and it is reported per stage for exactly that reason — and it is not scope. A version roll is not an event in a company's ladder; the owner changing the autonomy grant does not un-screen Accenture.

The fold has four ordering rules, and `coverage_mission.fold_stage_status` is the only place they live:

- Records fold in **time order across every version** of the `mission_ref`, by `created_at`.
- The **last decision wins**. A later `gate_failed` supersedes an earlier `gate_passed` — that is how a reopened gate reads — and a later `gate_passed` supersedes an earlier `gate_failed`, which is how the ordinary retry has always worked inside one version.
- **`entered` never supersedes a decision.** Re-seeding `entered` under a new version cannot walk a passed gate backwards.
- No record at all means the stage was never reached.

`record_stage` validates the ladder against the fold and still writes the record against the **active** version, so the ordering rules and the provenance are both exact. Two readers express the two different questions and must not be confused: `current_stage_state(mission_ref, company_ref)` answers "where does this company stand *now*" and is supersession-aware; `companies_at_or_past(stage, mission_ref)` answers "did this ever happen" and is deliberately **monotone**, because residency (P14a) leaves by a human removing a company from the universe and not by a gate being reopened.

This is a reading rule, not a new object: no table, no column and no stored hash changes, and every stage record already written stays exactly as it is. It is also why `mission_stage.run_once` no longer re-seeds `entered` on a version roll — a second `entered` says nothing the first did not, cannot move the folded state, and was growing an append-only ledger by five rows per publish.

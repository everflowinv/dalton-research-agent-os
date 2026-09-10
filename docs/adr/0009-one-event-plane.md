# ADR-0009: One event plane. The Perception/Agenda plane is retired, not migrated

*2026-09-09*  ·  Status: accepted  ·  Owner-adopted P14a design ([p14a-daily-tracking v1.0 §10](../reports/p14a-daily-tracking-v1.0-2026-09-09.md)) and [vision review D3](../reports/vision-review-against-plan-v1.0-2026-09-09.md) / [parallel-development-plan v1.0](../reports/parallel-development-plan-v1.0-2026-09-09.md)  ·  Retires the Agenda cycle of ADR-0002 as a *running* plane; ADR-0006 already moved the owner off it and left it at `/legacy` for operators; nothing here deletes anything

## Context

Dalton grew two event planes. The older one is Perception/Agenda: `perception.py` normalises a snapshot, `agenda_coordinator.py` runs a single-lane daily cycle over it, `agenda.py` stores cycles, candidates, decisions and feedback, and `agenda_control.py` serves the result to a person. The newer one is P14a's: `ResearchEvent` records what happened, the tracking lane decides what to re-read and when, and the judgement lane maps each event to one of the Playbook's five decision words.

D3 asked whether to migrate the old plane — make `PerceptionSnapshot` an emitter of `ResearchEvent` — or retire it. Reading the module answers it. The entire content of `perception.py` is `LegacyCoveragePerceptionAdapter`, and it reads companies, events, evidence and filings **from a legacy Coverage sqlite belonging to a different product** — the 万华 system that was itself retired on 2026-09-04. Its source is not Dalton's Ledger. `agenda_coordinator.py` imports exactly that adapter. Live holds 112 `agenda_cycles`, all from 2026-08, all shadow runs of a subject that no longer exists.

So there is nothing to migrate. An emitter needs an input, and this one's input is a dead product's database. Meanwhile every `ResearchEvent` emitter already reads Dalton's own tables — `coverage_mission_discovered_documents`, `claim_versions`, `forecast_reconciliations`, `market_price_series_versions` — and not one of them passes through perception. Two coexisting event planes is an ADR-level debt, and the way to pay it here is to mark the old one retired rather than to graft a new input pipe onto it.

## Decision

**`ResearchEvent` plus the judgement lane is the event plane.** Anything that wants to say "something happened that research should react to" records a `ResearchEvent`. There is no second way in. The Perception/Agenda plane produces no new events, no new cycles and no new decisions.

**The daily agenda run is off unless the config asks for it by name.** `service.json` gains one optional key, `legacy_agenda_plane`, default `false`. `agenda.enabled: true` alone no longer builds the coordinator or schedules the run — that was the whole switch before this ADR, and a config left on a machine must not restart the plane because nobody edited it. With the key set to `true` the plane behaves exactly as it did; that is the rollback, and it is one key. When the key is absent or false the service writes one line to stderr at start — `legacy agenda plane retired (ADR-0009)` — so the log says why no cycle will ever appear again, and the heartbeat reports `agenda.state: "retired"` rather than `"disabled"`.

**Nothing is deleted.** `perception.py`, `agenda_coordinator.py`, `agenda.py` and their tests stay in the tree and stay green. `perception_snapshot_versions` (103 rows live) and `agenda_cycles` (112 rows live) stay readable: an append-only ledger is not emptied because its producer stopped, and a retired plane's history is still the record of what the system once believed. `/legacy` keeps serving those reads. It now says so — the `/v1/agenda` view carries `retired`, `retirement_ref` and a retirement note, and the page renders the note, so an operator reading delivered decisions cannot mistake a closed archive for a live queue somebody is still filling.

**`priority_overrides` are superseded by `TrackingCadenceVersion`.** The old table was the one legitimate channel for "look at this one first": a scoped, TTL'd, provenanced nudge to agenda ordering. The same intent now has a better-shaped home. A `TrackingCadenceVersion` says how often a company's source is re-read, carries a `change_reason` from a closed vocabulary and the evidence that occasioned it, chains to its prior version, and is refused as `duplicate` when it asserts nothing new. Ordering pressure becomes a versioned, auditable cadence rather than a nudge that silently expires. A person may publish one, exactly as automation may. `active_priority_overrides` stays callable for reading history and is not the way to steer anything.

**What a future non-mission coverage product must do instead.** Not revive this plane. It registers a `ResearchEvent` emitter: a stateless scan over a ledger the product actually owns, declaring a `kind` from `EVENT_KINDS` with the payload fields that kind declares, idempotent on `(company, kind, payload_hash)` so re-reading yesterday's rows costs a read and returns `duplicate`. The event then flows through the same judgement lane, under the same budget pool, and yields the same five decision words as every other event. What it must not do is invent a second normaliser with its own snapshot contract, its own cycle table and its own feedback surface — that is the shape this ADR is retiring.

## Consequences

- The live service loses nothing, because the plane has had no input since 2026-09-04. What it gains is a log line that says so.
- The coordinator's config block is still parsed and still shape-checked when present, so a malformed leftover is a config error rather than something the retirement swallows. Only the *construction* is gated.
- A fresh install never writes `legacy_agenda_plane` at all: `bootstrap` emits no `agenda` block, so a new machine gets the retirement without a key.
- ADR-0002's Agenda cycle survives only as history and as the tests that pin its contracts. ADR-0006's `/legacy` promise is unchanged: those routes still answer, read-only.
- `agenda-timeout` and the auto-accept sweep remain wired for the delivered backlog. They act on nothing new, because nothing new is delivered.
- Deleting these modules is a separate decision and a separate change. This one is reversible by a single boolean, which is the point.

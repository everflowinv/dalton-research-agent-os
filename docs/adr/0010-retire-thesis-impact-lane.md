# ADR-0010: The thesis-impact lane is retired; the ADR-0007 revision-candidate path is the only way automation speaks about a thesis

*2026-09-30*  ·  Status: accepted (records a state the owner already chose on 2026-09-08 and ADR-0007 made permanent on 2026-09-09; reversible only by a superseding ADR)  ·  Closes item 一.3.1 and 1.2 #9 of [vision-gap-workspace-reuse-cockpit-debug-2026-09-30](../reports/vision-gap-workspace-reuse-cockpit-debug-2026-09-30.md)  ·  Supersedes the "park, fix, reopen" plan in PROJECT_STATUS 2026-09-08 and the un-park design in `thesis_impact_reopen.py` (P14b); leaves ADR-0001 and ADR-0007 unchanged

## Context

`space.lumos.dalton.thesis-impact` was the first thing that ever judged a Claim against a thesis. It did real work: 1,641 successful ticks in August, and ACN's first real Claim → thesis chain was closed by it. Two tables still hold its output (`thesis_impact_assessments`, `thesis_impact_verifications`; one row each on legacy, from 2026-08-27).

It stopped on 2026-09-02. A policy roll-over rejected every `eligible_assessment`, the park fix held it at `blocked_pending_human`, and then `conflict: request conflicts with existing immutable data` recurred every five minutes. On 2026-09-08 the owner set `service.json#thesis_impact.enabled=false` in all environments. The note said "park it; fix it once and reopen it when the thesis slice comes" (PROJECT_STATUS 2026-09-08).

The thesis slice came the next day and took a different shape. ADR-0007 (2026-09-09) decided that automation may *propose* a thesis change as a `ThesisRevisionCandidate`, and only a person may accept it. P14a's event-judgement lane became the producer of that proposal: one event, one bounded call, a word from the Playbook's frozen `DECISION_VOCABULARY`, and a verifier from a different model family. The two thesis v2 versions on 2026-09-14 came through that path (`thesis-revision-candidate` → human decision). `thesis_impact_reopen.py` (P14b) then tried to rewire the old lane's verdict into the same candidate shape. That would have made it a second producer of the same record, fed by a per-Claim loop instead of per-event, and its verifier pin had already died (`profile:gemini-3-7-flash` retired).

Nobody ever wrote that the old lane was superseded, so the system kept paying to keep it alive:

- 2026-09-23: the legacy model page had to have the lane's two routing pins (`thesis_impact_assessment` → `dalton-openclaw-assessment:9`, `thesis_impact_verifier` → `dalton-openclaw-verifier:4`) rediscovered and aligned, for a lane that cannot run.
- 2026-09-29: the controlled-verify coverage check needed a special case so that 核验论点影响评估 would stop reading as a capability gap.
- `thesis_impact_reopen.py` and its tests were maintained, although nothing imports the module.

Read-only evidence on 2026-09-30:

- `thesis_impact.enabled` is `false` in legacy, ws-7d and ws-e399.
- No `*.thesis-impact.plist` is installed.
- `~/Library/Logs/Dalton/thesis-impact.stdout.log` was last written on 2026-09-08.
- In legacy Core, `thesis_impact_assessments` and `thesis_impact_verifications` each hold 1 row. `thesis_versions` holds 4 rows (the industry thesis plus ACN, 2 versions each).
- Also in legacy Core: `event_judgements` holds 5,411 rows (5,396 `NO_CHANGE`, 15 `THESIS_WEAKENED`) and `thesis_revision_candidates` holds 0. Mission v28 grants `thesis_revision_candidate`.

## Decision

**The thesis-impact lane is retired.** The per-Claim assessment → verification → (eventually) thesis-revision service no longer exists as a running plane in any environment. It is not parked and it is not "waiting for the thesis slice". The thesis slice exists, and this lane is not part of it.

**There is one producer shape for thesis proposals: ADR-0007's `ThesisRevisionCandidate`.** Its producers are the event-judgement lane and, for gate re-examination, zero-base review. A person decides every candidate. Any future producer must write that record through `thesis_revision`'s public entry points. It must not bring back a separate assessment table or a separate verifier pin.

**What is retired:**

1. The scheduled service (`dalton_core.thesis_impact_production` as a LaunchAgent, label `*.thesis-impact`). `thesis_impact.enabled` stays `false`. Setting it `true` now contradicts this ADR, not just a parking note.
2. The un-park design. `thesis_impact_reopen.py` and its tests are deleted: nothing imports them, and their only purpose was to reopen this lane.
3. Routing maintenance for `thesis_impact_assessment` and `thesis_impact_verifier`. These two stages will get no new pins, fan-outs or capability exceptions. `model_selection` labels both as retired and marks each binding with `retired_by: "ADR-0010"`. A page or report can then say "retired" instead of asking the owner to bind them. The coverage special case added on 2026-09-29 can be dropped by the owner of `cockpit_plane.py` whenever convenient. It does nothing wrong, it is simply no longer needed.

**What is *not* retired, and why:**

- **`thesis-impact-budget.sqlite` and `thesis_impact_budget.py`.** Despite the name, this is the shared daily budget ledger for document extraction, annual research, mission documents, the cockpit and others. It is load-bearing and stays.
- **The `service.json#thesis_impact.config` block.** `document_extraction_setup`, `research_planner_setup`, `day_budget_configuration`, `model_budget_configuration` and the workspace service template read `budget_policy_version_id`, `budget_db`, `model_router_db` and the broker fields from it. The block stays as the budget anchor. Renaming it is a separate, mechanical change and is out of scope here.
- **Stored data.** `thesis_impact_assessments` / `thesis_impact_verifications` keep their schema and their append-only triggers. `company_research_view`, `restore_independence_predicates` and the writer's read op keep reading them. Nothing is dropped or migrated.
- **The code that reads or replays those records.** `thesis_impact.py`, `thesis_impact_control.py`, the calibration corpus tools, and `thesis_impact_production.py` itself stay importable because the writer and service config parser still reference them. Removing the production module is a follow-up refactor across `service.py`, `writer_server.py` and `macos_launchagent.py`, done once nobody else is editing those files. The refactor changes no behaviour, since the lane is already off everywhere.

## Consequences

- **Phase 9 exit gate 2 is restated.** "At least 1 Claim ... goes through thesis-impact → weekly brief" becomes: goes through **event judgement → (if not `NO_CHANGE`) revision candidate or forecast proposal → weekly brief**. The gate is unmet today for a reason this ADR does not hide. Fifteen `THESIS_WEAKENED` judgements (12 EPAM `revise_forecast`, 3 ACN `note`/`research`) produced 0 `thesis_revision_candidates` and 0 `forecast_revision_proposals`. Four of the five companies have no admitted thesis for a candidate to bind to (ADR-0001 admission is still owner-only). That gap belongs to the event-judgement lane and the thesis-admission queue. Reopening this lane would not close it: the old lane needs a `ThesisVersion` just the same.
- The model page and the controlled-verify summary stop asking the owner to bind 评估新事实对论点的影响 / 核验论点影响评估.
- PROJECT_STATUS's 2026-09-08 "停泊" section is now historical. The status file points here.
- **Rollback.** A superseding ADR flips `enabled` back to `true` and reinstalls. It would also have to explain why a second producer of `ThesisRevisionCandidate` is wanted, and it would need a new verifier pin, because the old one is retired at the gateway.

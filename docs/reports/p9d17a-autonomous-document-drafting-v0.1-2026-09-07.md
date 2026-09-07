# P9d-17a: drafting becomes a mission automation step

*2026-09-07*  ·  ADR: [0005](../adr/0005-autonomous-document-extraction.md)

## What the owner said

Looking at the cockpit's queue of documents awaiting extraction and the notice
that no approved extraction model was installed, the owner rejected the role
the queue assumed: they set the research goal, steer when needed, ask
questions, and approve system changes such as a new tool. They do not approve
documents. That is ADR-0005. This slice is its first half: every acquired
document is drafted by the mission's own automation, within the mission's
budget, with no person in the loop.

## What was missing on live

Two things. The writer had never been given an extraction model configuration
(`control.config.research_review.document_extraction_model_config_path` was
absent), so even a human pressing generate was gated. And drafting was only
reachable through a human-triggered writer op that runs inside the writer's
single 30 s request executor, where a model call that takes a minute would
stall the cockpit and the controller tick.

## What changes

**Installation is a command, run by the installer.**
`dalton_core.document_extraction_setup` reads `service.json`, appends a routing
policy version `model-routing-policy:dalton-openclaw-extraction` (only when its
filters differ from the latest), writes the closed model configuration next to
the state, and points the review config at it. It reuses what the host already
has: the bounded planner's OpenClaw model broker, the thesis-impact day budget
ledger and policy, the model router. The policy names the profile *id*
`profile:deepseek-v4-flash`, so the daily profile refresh never stales it. No
credential is read.

**Drafting runs out of process.** `document_extraction_cli` is a child like
search, fetch and acquisition. One run drafts at most `--max-windows` windows
across the reviews awaiting extraction, oldest first, each under its own
mission's grant (the automation principal must equal the mission's, the source
must be `connected`) and budget (the mission's daily paid calls and cost, in the
shared ledger). It reuses `DocumentExtractionService` unchanged: same context,
prompt, output schema, budget admission and persisted, replayable results a
human draft produces. A window that already has a result is skipped, so a
re-run only advances. `formal_authority_writes` is always 0.

**The controller tick drives it.** `DocumentExtractionLauncher` and
`DocumentExtractionCoordinator` follow the fetch lane's shape: single slot,
owner-only tickets under `extractions/`, adoption of a finished child's summary
after a restart, and a new core op `dispatch_document_extraction` the bounded
planner driver calls every tick. A child that found nothing to draft, or that
was gated, holds the lane for an hour unless the awaiting count changes, so a
drained queue does not spawn a process every five minutes.

**The service accepts the automation actor.** `_source_context` admits
`automation:` as well as `human:`; the mission grant, not the actor string, is
what authorises either.

**Carry-forward keeps tickets.** Found while writing the end-to-end test: a
copied `acquired` row lost its `ticket_ref`, and the review context needs that
ticket to find the manifest. Fixed.

## Verification

- The setup command is idempotent: one policy version appended, config written
  owner-only, service config pointed at it; a second run changes nothing; a
  changed profile list is a prior-linked new version.
- The real child, in hermetic-fixture mode against an acquired AlphaEngine
  document: under a mission without the grant it drafts nothing and says why;
  after v2 grants automation and the document is carried forward, it drafts
  both windows (the second a terminal invalid-output result, accounted once),
  the persisted suggestions read back under the automation actor and a human,
  nothing formal is written, a re-run replays, and a foreign automation actor
  is refused before any grant.
- The coordinator launches, reports busy, settles, holds after "nothing to
  draft" or a gate, resumes after an hour, and retries after a failed child.

## What this slice does not do

Suggestions are drafted; they are not yet staged or admitted as Claims. That is
P9d-17b (AlphaEngine chain) and P9d-17c (public-web citation authority). Until
then the cockpit shows the drafts and a person may still stage one by hand, but
nothing requires them to.

## First live runs: what the host said back

**Read-only WAL.** The first child failed before any model call: the context
binds the budget ledger read-only, and a read-only WAL open refuses when
nothing holds the file. The thesis-impact ledger is closed between that lane's
runs. The child now holds a write-mode handle on the ledger and the router,
without writing, for its lifetime.

**Thirty-four acquired rows name no ticket.** Every AlphaEngine document acquired
on 2026-09-04, and the one web document settled as already held, carries
`ticket_ref = NULL`, so the review context could not find its manifest and the
first such review crashed the run. Two fixes: the child now survives any single
unbindable review and reports its reason; and both launchers gained
`locate_completed_manifest(document_ref)`, which finds the latest succeeded
ticket for the document in the ticket directory and reads the manifest through
the same verified path. The review context uses it when the row names no
ticket. This also means those thirty-three AlphaEngine reviews were never
readable in the cockpit either.

**The mission cannot spend.** Ten reviews were refused with "mission
constitution does not bind current governance policy". Live, constitution v2
binds `policy-3` while `policy-4` is active, and neither policy-4 nor mandate
`us-it-services-constitution-p8a:1` carries the closed `research_budget` that
ADR-0004 section 7 (as enforced in `outer_budget`) requires before a mission
may spend on models. Paid extraction has therefore never been possible on live.

`scripts/publish_extraction_authority_chain.py` republishes the four records in
order, each derived from its prior and rebinding only what must change:
`policy-5` (policy-4 plus `research_budget`), mandate p8a:2 (v1 plus
`research_budget`, activated), constitution v3 (v2 rebinding policy-5 and
mandate v2), mission v5 (v4 rebinding constitution v3 and mandate v2).
`research_budget` equals the mission v4 budget the owner already published
(40 paid calls a day, 5 USD a day, 30 AlphaEngine calls per 24 h): a
formalisation, not an expansion. Rehearsed on a copy of the live Core: the
outer-budget check binds policy-5 and mandate v2, and automation is granted on
both sources under v5.

Applying it live is a governance publish as the owner's principal, and the
session's permission gate stopped it there. It is the owner's to run:

```
.venv/bin/python scripts/publish_extraction_authority_chain.py --live
```

Until then every automated draft is held with that reason, spending nothing.

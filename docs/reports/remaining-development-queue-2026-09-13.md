# Remaining development queue — 2026-09-13

This audit is bound to integration `3dfcd983`. It separates current evidence from historical roadmap text. It performed no live mutation, service action, connector/provider call, deployment, or credential read.

## Current boundary

Published authority remains R20 `b994b09c`, with `current-release.json` SHA-256 `e49997748b82a42ceb98132ca077a886c3e280843fa8458633a24ce7d4d7e1dd`. The machine subsequently installed R21 `8801dadd`, but that deployment remains failed and unpublished. Its preserved receipts are deployment `20581630599b8c713201b04d49e7d90ff8bb5d28ac45c04844fe55453836adc5`, recovery prestart `3bfe8abd7e3a7e447d8ee415007a094645afafdd5c8471de7d12aa757e04fdab`, and recovery restart `dc8d2353f36963b8d8f96f0fe7d6c0b8fc0cc3e3516719c94be55a7fc3e0c79b`. Recovery proves the R21 runtime restarted healthy at that checkpoint; it is not a 45-sample sustained-health acceptance.

The writer-transition correction is implemented and merged. Frozen `d1d012ed` passed 7,909 tests with zero failures/errors and one skip, plus 608 wheel/source byte comparisons and three JavaScript checks. This proves the code, not a release. No new full copied-live-state rehearsal, accepted manifest, successful deployment, sustained health, finalization, or publication exists for it. R21 and the corrected source both declare 173 Core writer operations, so the next transition must preserve the current writer authority with schema 0.4. It must not manufacture another schema 0.5 append.

## Executable queue

### P0 — close the split-predecessor release

1. Add one closed acceptance artifact that jointly binds the actual installed predecessor (R21 failed deployment, recovery receipts, installed runtime/config/writer bytes) and the canonical published predecessor (the complete R20 publication/current-pointer chain). The historical R21 dependency checker incorrectly names R20 as both predecessors, and the existing candidate artifact inventory cannot bind a separately copied recovery receipt.
2. Freeze one clean source/ops commit containing that binding. Repeat the full suite, wheel verification, and a current-state copied rehearsal. The rehearsal must prove 17 model configurations, service/OpenClaw/source authorities, 173 unchanged writer operations, and zero escapes or external calls.
3. Prepare and independently preflight a new immutable candidate. Immediately before installation, reverify the R21/R20 split authority and the retained R18b broker/host origin.
4. Under the standing owner deployment authorization recorded in prior release drivers and `current-release.json`, install only the accepted candidate. Require exit zero, exact installed wheel/source identity, unchanged writer bytes under schema 0.4, a fresh rollback/database snapshot, and no configuration/service/external mutation beyond the declared transition.
5. Observe 45 samples at 15-second intervals over at least 660 seconds on one postdeployment controller. Then perform post-observation installed/dependency verification, finalization, an immediate CAS check that the published pointers are still exact R20, publication, and retention. Preserve R21's failed receipt unchanged.

This is the only foundation blocker before ordinary product evidence can be attributed to the corrected runtime.

### P1 — obtain real financial-model outcomes

R20 produced four new specifications for CTSH, EPAM, ACN, and DXC but zero new forecast model versions in the audited window. The merged fixes address the observed structured `usdPerShare`/`usd_per_share` proof mismatch, cumulative non-cash source-pair selection, and signed filed growth behavior. Those are implemented code paths; their production effect remains unproved.

After P0 publication, use the normal scheduled lanes and read-only audits to establish:

- successful model publication for CTSH, EPAM, and DXC with exact filing/unit proofs;
- ACN's outcome after the signed-growth correction, while retaining any separate economic rate-domain refusal;
- IBM's status without inventing unavailable forecasts or relaxing exact filed subtotal ties;
- five-company Excel/HTML exports only from persisted accepted models, with formula replay and source provenance.

Any new semantic refusal becomes a bounded diagnosis and new contract candidate. It does not justify replaying old provider responses across changed task/state identities or weakening numeric tolerances.

### P2 — measure actual research-product coverage

The September 9 roadmap's claims that dossiers, debate maps, market prices, valuation, research tasks, earnings workflows, Ask v2, conviction calls, investment memos, and the analyst journal were absent are stale. Current source contains their authorities, launchers, writer paths, Cockpit surfaces, and focused tests. Do not reopen those phases as greenfield development.

What remains is production acceptance by company and artifact type:

- inventory current dossier sections and their producer/verifier/mission bindings;
- record which debate maps, deep-insight gates, industry frameworks, earnings previews/calibrations, conviction proposals, and investment memos actually exist;
- distinguish `partial_published`, `insufficient_evidence`, held, and human-pending states from complete products;
- run the existing quality rubrics/golden sets against real artifacts and collect human analyst feedback through the journal.

Missing products should first be classified as missing evidence, inactive lane/configuration, budget hold, validation refusal, or missing human decision. Only reproduced code defects enter development.

### P3 — close source and activation gaps

Several source families are implemented but remain conditional on external authority or installation state. Verify them from configuration and read-only lane status after P0 rather than relying on historical TODOs.

- Guidepoint: connector/lane code exists; actual endpoint access, approved governance, mission grant, budget, and successful transcript coverage remain separate evidence.
- Company wiki, prior research, and crowd/sales-note feeds: code exists, while installation depends on owner-provided directories/tools or source material. Absence is an explicit `unconfigured` state.
- IR page monitoring and earnings-call coverage: measure five-company period completeness and source freshness; add transport or declarations only for concrete missing periods.
- Market and consensus: authorities and valuation logic exist. Verify actual history depth, shares/market-cap identity, analyst-estimate coverage, and stale/missing companies before adding another provider.

External source registration, new governance approval, new mission write scopes, or paid-source credentials remain owner-controlled. Existing deployment authorization does not supply those approvals.

### P4 — human governance and product decisions

Thesis revisions, gate reopen decisions, conviction calls, and final investment-memo decisions retain human boundaries. Development should prepare complete, evidence-bound candidates and usable Cockpit review surfaces; it must not convert silence into approval. The same applies to PM quality scoring and disputed research conclusions.

### P5 — later product work

After P0–P4 have measured real coverage, prioritize gaps that affect analyst use: complete five-company financial/dossier coverage, weekly brief quality and delivery, natural-language answers grounded in current models/events, and source completeness. Visual redesign, reference-fund template import, final delivery formatting, and multi-analyst workspaces remain later work unless the owner changes the order.

## Stale historical TODO handling

Older reports are immutable history. A statement such as “no market layer,” “no dossier,” “no debate object,” “no research-task lane,” or “Ask reads only Claims and Thesis” describes its dated checkpoint, not `3dfcd983`. Close or supersede such items only with current code plus accepted runtime/product evidence. Conversely, implemented classes and passing unit tests do not prove activation, source authority, natural production output, sustained health, or human acceptance.

The practical order is: recover and publish one truthful runtime; observe financial models; inventory research products; activate only approved sources; collect human decisions; then resume later blueprint work.

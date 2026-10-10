# ADR-0018: One material acquisition service for every stage. A missing document, figure or answer is a MaterialRequest; the service works its source ladder, records every attempt, links what arrives to the request, and reaches the owner only when all sources are exhausted, a budget cap is reached or a source needs a login

*2026-10-09*  ·  Status: proposed (batch E of the research capability diagnosis of 2026-10-09, owner decision 「所有环节共用的材料获取服务」 and decision 4 on batched redrafts; research quality review gap list items at/au and contradiction 4)  ·  Builds on batch A (evidence index, `material_arrivals`), batch C (`material_requests`, `material_sufficiency`, redraft policy), batch aa (connector health hold), ADR-0017 (`product_rewrite_flags.requires_material`), C2 (budget pools) and the per-source call windows of `connector_rate_policy`

## Context

The diagnosis found every stage ending its search for material in a different dead end:

- the research planner's `inquiries` that no ad-hoc template could bind were reported as `no_bindable_template` / `repair_target_capability_gap` / `directed_document_capability_gap` and dropped;
- the Dossier's `repair_targets` (`missing_evidence`, `unsupported_slot`) reached the planner only if a model happened to cite them;
- an event judgement's `action=research` and the ask path's "材料不足" had no consumer;
- a product's 「材料缺少」 was free text; batch C made it a MaterialRequest, but nothing worked the ledger;
- contradiction 4: an issuer-misattribution rewrite must not run before official replacement material exists, and nothing fetched it.

## Decision

### 1. `material_acquisition.serve(state_dir, request_id)` — the source ladder

The request's `source_priority` (default `indexed_corpus → sales_notes → alphaengine → sec_edgar → guidepoint → web`) is walked in order; the walk stops at the first rung whose result meets the request's `acceptance`:

| rung | what it does | cost |
|---|---|---|
| `indexed_corpus` | `evidence_index.documents` / `evidence_search` with the need and period window | free |
| `sales_notes` | indexed sales notes, then the raw notes index the feed lane reads; a matching note not yet indexed is waited for (the feed lane acquires it) | free |
| `alphaengine` | targeted `search_library` for the company on the spec the need maps to (`earnings-call-transcripts`, `sell-side-reports`); the coordinator's budgeted `get_document` acquires what it selects | ~3 calls |
| `sec_edgar` | data items: XBRL statement lines already in Core; documents: targeted filings-index pass (`current-report-8k` with Item 2.02 exhibits, `annual-report-10k`) | 1 call, free HTTPS |
| `guidepoint` | targeted library search with the company's plan query | 1 call |
| `web` | targeted web search; the public-web fetch lane acquires cited pages | ~3 calls, metered |

Lane-owned data (`consensus_vendor`, `forecast_model_lane`, `market_price_lane`) is a rung that is satisfied when that lane has produced the data and otherwise waits; it times out, it is never "exhausted" and never reaches the owner.

**Acceptance** (`evaluate_acceptance`): a *document* need wants `min_documents` indexed documents of its kinds about the subject (aboutness ≥ 0.5) inside the period window (`published_after`, `max_age_days`); a *data* need wants a passage that names the item in any of its phrasings, carries a number and is dated in the window or names the period (an XBRL fact for the period also counts); an *answer* needs `min_passages` passages covering half the question's terms. Short of that but non-empty is `partial`.

**Queries** are deterministic in need/subject/period (`build_queries`, two or three phrasings, `alphaengine_query_forms` conventions: the vendor's title form "{name} {quarter}" first; for a data item each phrasing names the item differently — `bookings` / `new bookings` / `book-to-bill`).

**Attempts.** Every rung's attempt is `record_attempt(source, query, outcome, calls, cost)`; outcomes: `accepted`, `partial`, `empty`, `dispatched`, `health_hold`, `rate_limited`, `budget_blocked`, `needs_credentials`, `not_connected`, `not_applicable`, `waiting_on_lane`, `error`, `unsupported`. Found → `close(found)` with the items; partial items at the end → `close(partial)`; all rungs tried → `close(exhausted)` with 「已尝试：…」; past the deadline → `close(timeout)` after the free rungs and any pending settlement.

### 2. No parallel storage: targeted queries drained by the source's own coordinator

The service never calls a connector. An external rung appends a **targeted query** (`material_targeted_queries`: request, source, company, spec). The source's coordinator drains it at the start of its own tick — `MissionSourceDiscoveryCoordinator.launch_targeted_discovery` (AlphaEngine, web search, SEC filings index) and `GuidepointLaneCoordinator.targeted_queries` — through its own governed launcher, mission grant, plan spec and code-owned query forms, health hold and call window, bypassing only the cadence; it answers `launched` / `refused` / `deferred` / `failed` into `material_targeted_query_events`. What the search selects is acquired by the coordinator's normal acquisition path; the manifests are indexed by the evidence-index lane; the service re-evaluates acceptance over the index once the dispatch and its acquisitions have settled (Core's `coverage_mission_discovery_dispatches` / `discovered_documents`, read-only) or after `RUNG_WAIT_HOURS`. A rung given manifest paths directly registers them through `evidence_index.index_acquired_manifest`, the acquisition lanes' own completion hook. The Core never invents a connector query: the targeted query names a spec of the approved plan; the phrasings the service builds are used for local retrieval and recorded with the attempt.

### 3. Idempotency and the negative cache

A request is its digest (`subject, need, period`); a second requester joins. An `exhausted` (or `timeout`) request is not retried for 7 days unless a discovery batch for the company was recorded or a non-backfill arrival was indexed since it closed; then `material_requests.reopen` appends a new `opened` event (same id, new deadline, earlier rounds kept) and the ladder runs again. `found` and `superseded` are final.

### 4. Budget: the `material_acquisition` pool

The four C2 pools are model dollars with a closed name set a mission must match, so the service's pool is a fifth, separate pool (`budget_pools.MATERIAL_ACQUISITION_POOL`): a day cap in dollars and calls (default $2.00 / 60 calls; override `mission.budget.material_acquisition` or `<state>/material-acquisition-pool.json`) with its own ledger (`material_acquisition_spend`), and a per-request cap from `request.budget` (default $0.50 / 8 calls). A rung whose estimated calls would cross either cap stops the ladder (`budget_blocked`); once the pool is spent the lane serves only the free rungs. The per-source call windows still bind at launch inside the coordinators; health holds (batch aa) are respected both when the rung is asked and when the coordinator launches.

### 5. The lane

`material_acquisition_lane` (order 38, registered in code): each tick converts dead ends into requests (rewrite flags still `awaiting_material` → the subject's 8-K exhibit / transcript for the latest quarter from the official rungs first; the stable `missing_evidence` / `unsupported_slot` / `no_new_evidence` targets of each company's latest Dossier run), reopens what left the negative cache, and serves at most 4 open requests within 10 s, ordered drafting products first (product lanes, rewrites, event judgement, ask), then P0 companies, then sufficiency-blocking requests, then oldest.

**Arrival linkage.** A found item is a `material_request_arrivals` row (`material_arrived`: request ↔ document_ref, with the evidence index's `arrival_id`). Redraft thresholds keep reading `material_arrivals`; a rewrite flag's `requires_material` resolves when the replacement arrives, and `requests_for_document` / `material_arrived` say which request an arrival answered.

### 6. Conversions

- `research_task.open_material_requests_for_gaps` (called by the research-task lane, granted or not): every `no_bindable_template`, `repair_target_capability_gap`, `directed_document_capability_gap` and `industry_inquiry_has_no_company_probe` refusal opens an *answer* request; the two existing templates keep binding what they can. The conversion itself is `research_planner.inquiry_material_request` (pure).
- `open_for_repair_target`, `open_for_rewrite_flag`, and `open_for_effect(state_dir, effect)` — the entry point for F (event judgement `action=research`) and G (ask 续答): a closed field set naming a subject and `doc_kinds` / `data_item` / `question`, optional period, requester, priority, acceptance and `deadline_hours`.

### 7. The owner door: three cards, nothing else

`material_sources_exhausted` (per company, listing each request with 「已尝试：…」), `material_budget_exceeded` (the day's pool, or one request's cap, with the dollars and calls to approve), `material_credentials_needed` (a source refused for a missing approval or login). Holds, waits, timeouts and lane-owned data never become cards. The cards use the existing acknowledgement pattern; the budget card adds one choice, `approve_spend` (「批准追加获取额度」): its card ref names the amount (`material-budget:<pool|request>:<key>:<usd>:<calls>`) and the service adds it to that day's pool or that request's cap when it reads the acknowledgement back. The acknowledgement table's `choice` CHECK is widened once by an idempotent rebuild (`needs_human_ack._migrate_choices`). Wording comes from the terminology registry (`material_source`, `material_outcome`, `material_need`, `ack.approve_spend`) and the lint covers the new modules.

### 8. Cockpit

`GET /v2/data/research/material-requests?company=<ref|ticker>[&status=open,partial,exhausted]` (additive): per request its need, period, status, requesters, deadline, every attempt of the current round (source, query, outcome, calls, cost, time), 「已尝试」, the documents that answered it, and today's pool. Read-only; a blank workspace is an honest empty state.

## Consequences

- Every stage now has somewhere to send "I lack this"; the ledger says what was tried, where, and at what cost.
- A targeted query takes a coordinator's search slot ahead of its scheduled search; on a host where the AlphaEngine window is tight, scheduled discovery yields to requests from drafting products. Each targeted search is still one search inside the same window.
- External sources answer asynchronously: a ladder that has to go past the index takes several ticks (one to queue, one for the coordinator to launch, the settle grace for acquisitions to land and be indexed).
- The Guidepoint and sales-notes rungs use what those lanes can be asked for today: a company's plan query, and notes the feed lane attributes to covered companies. A free-text connector query would need the connector children to accept one; that is deliberately not done here.
- On legacy after deploy the ledger starts empty: batch C's sufficiency check opens requests only when a product lane calls it with `record=True` (batch D), legacy has no rewrite flags and no Dossier repair targets today, and its latest research plan has one directed-document inquiry that the research-task lane handled without a refusal. A read-only simulation of the initial-screen checklist finds 10 items that would open (CTSH, EPAM, IBM, DXC: latest transcript and 8-K release for the quarter ending 2026-06-30; EPAM and DXC: broker reports), none answerable from the index today.

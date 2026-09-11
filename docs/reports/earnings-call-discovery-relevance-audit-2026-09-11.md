# Earnings-call discovery relevance audit — 2026-09-11

This was a read-only inspection of the live authority and the frozen discovery code. It made no connector, model, acquisition, or production writes.

## Reproduced failure

For ACN and IBM, the latest successful AlphaEngine discovery dispatches on September 9 and 10 compiled the same phrase on each day. Only the date window and therefore request hash changed. ACN used `Accenture ACN earnings call transcript`; IBM used `IBM International Business Machines earnings call transcript`. Each latest source envelope held 20 ranked document refs and a continuation cursor. The live IBM results included unrelated issuers sharing “International”; none became a valid IBM earnings call.

The repeat is deterministic. `build_discovery_parameters` substitutes one company `search_terms` string into one spec `query_template`. Shortfall changes cadence from rediscovery to retry but does not change the query. Continuation can page the same ranking; it does not reformulate the search for missing fiscal periods.

## Acquisition defect

A search result is immediately registered as discovered documents. `launch_acquisition` selects the next queued ref using stage need, company, host preference and retry state. It does not inspect the ranked candidate's title, snippet, date, document type, provider company tags, or fiscal-period hint before spending a `get_document` call. Issuer attribution happens only after full content acquisition/extraction. This explains why a broad IBM result set consumes calls on other “International” companies.

The AlphaEngine input contract already accepts `query`, `filters` (`company`, date bounds, document type, geography, industry), and cursor, with at most 20 ranked hits per page. The current mission plan uses only query, date, and document type. Company tags are useful evidence but cannot be a hard gate because the owner requires relevant cross-company and industry material to remain available for their own research purposes.

## Bounded correction

1. Add a new immutable discovery-plan version that can declare ordered query variants for the earnings-call spec and structured company name/ticker terms. The coordinator advances variants only while the exact company-period checklist remains short. Variant index, template bytes, current period gaps, plan hash, and cursor belong in request identity. The old plan remains replayable. Industry specs keep their broad queries.
2. Project each ranked result into a closed candidate view containing ref, title, snippet, date, document type, and provider tags, bound to the source-envelope and raw-artifact hashes. Do not copy arbitrary provider fields into a prompt.
3. Reuse the configured Cockpit model router, Scheduler, admission, accounting, returned-failure retry, and formal-result authority for one relevance-selection WorkOrder. Its output is a closed ordered set of candidate refs with issuer/period/relevance reasons. The candidate view hash, company identity, missing periods, prompt contract, route, result and budget are all immutable inputs.
4. Only selected refs enter acquisition. A selector failure leaves candidates pending and visible; it never defaults to fetching all. Full-text issuer and period validation remains authoritative after acquisition. A rejected company candidate may still be retained under a separate industry/broad-research spec; it must not be silently discarded by company tags.

Tests must distinguish the archived IBM metadata from synthetic mixed-page fixtures: broad matching titles from other “International” issuers plus one exact IBM quarter. They should prove only the exact candidate is acquired for IBM, other candidates remain available to an industry spec, selector failure launches no acquisition, restart replays the same formal selection, a changed period gap creates a new identity, and configured call/model budgets remain enforced.

## Actual metadata recheck — 06:48 UTC

Root re-read the latest IBM search's exact archived JSON-RPC bytes through its Core source envelope, verified both hashes, and confirmed 20 ranked records. Actual keys include `publish_time`, `rank_date`, `document_code`, `type_id`, `companies`, `industries`, `markets`, `sources`, `snippet`, `title`, and `source_attribution`. All provider tag list elements in this response are strings. The selection projection must use these actual fields. Private metadata audit SHA-256: `f7f2bf25d1abc10d03b4fc6d7609a3fc71a2273ec2bed4577a704a94b40aae65`; no new connector or model call was made.

The async candidate worktree now has an actual child process → Unix broker protocol → OpenClaw adapter → CockpitModel/Scheduler → selected-only acquisition test. This is execution evidence with a fake provider, not live selection activation or proof that the search query itself can find the missing periods. Remaining review covers completed-page invalidation, retry acquisition, empty-page cursor advancement, query reformulation and installation configuration.

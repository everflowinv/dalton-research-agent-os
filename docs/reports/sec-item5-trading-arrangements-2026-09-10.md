# SEC Item 5 insider trading arrangements

Date: 2026-09-10  
Baseline: `42c2273`  
Branch: `sec-item5-trading-arrangements`

## Result

Archived Form 10-Q text can now produce the closed research-event kind `insider_trading_plan`. It is deliberately separate from `insider_transaction`: an adopted or terminated Rule 10b5-1 arrangement describes a plan and is not evidence that a purchase or sale executed.

The parser reads only a bounded Item 5 section or an SEC Inline-XBRL “Insider Trading Arrangements” fact page. Every emitted row carries the archived document and filing identity, accession, issuer binding, named person, action (`adopted` or `terminated`), action date, plan type, filed material-terms excerpt and its hash. Direction, aggregate shares, title, and expiration date remain null or `unknown` when the disclosure does not state them; the parser does not infer them. Multiple people and multiple actions receive distinct content-addressed event keys. Explicit “none adopted or terminated” prose returns `empty`; action prose lacking a locatable person or date returns `refused`.

The producer uses the existing document-index scan already exercised by the tracking lane. It adds no connector operation, governance record, seeded policy, live universe, or deployment change. The normal daily tracking pass records the events idempotently and reports them under `events_by_kind`. The event-judgement context states that the record is a plan action rather than an executed trade, and the cockpit and source-capability enums display the new kind without conflating it with Form 4.

## Primary fixtures

Two SEC EDGAR Inline-XBRL fact pages were captured on 2026-09-10 with their original HTML, deterministic visible-text rendering, URLs, accessions, and SHA-256 hashes recorded in `tests/fixtures/sec-item5/MANIFEST.json`:

- EOG accession `0000821189-26-000149`: Jeffrey R. Leitzell terminated one arrangement on 2026-06-25 and adopted another on 2026-06-26.
- accession `0001193125-26-338217`: Robert E. Landry and Wendell Wierenga adopted separately dated arrangements with separately disclosed share limits.

## Validation

The focused suite covering the new parser, tracking pipeline, research-event contract, existing ownership/Form 4 behavior, buybacks, source capabilities, and event judgement ran 297 tests successfully. The real tracking regression executes archived SEC text through `company_events`, `run_tracking`, summary accounting, and `ResearchEventAuthority`, including a second idempotent record pass. Module compilation and `git diff --check` also passed.

The repository-wide suite remains owned by main integration.

# SEC Item 5 cross-review — 2026-09-10

## Scope and finding

Reviewed commit `7e69ad3` against the archived SEC fixtures and the actual `DocumentIndex -> tracking_lane_cli -> ResearchEvent -> event judgement` path.

One release blocker was present. `DocumentIndex`'s builtin `utf8` extraction retains an archived SEC HTML document as markup, but the Item 5 parser tests passed a separately rendered `.txt` fixture directly. Both official `.html` fixtures therefore produced zero events (`refused`) on the real storage shape. The parser now performs a bounded, deterministic visible-text projection for HTML while continuing to accept extracted plain text. The pipeline test stores official archived HTML in `document_index_documents`, proving the actual consumer wire.

The review also found a factual overstatement in the EOG fixture. A multi-leg plan begins with “up to 17,739 shares” and then adds two vesting-based legs. The parser previously published 17,739 as `aggregate_shares`. It now leaves the aggregate null when the action paragraph announces an enumerated continuation, rather than calling the first leg the total.

## Boundary checks

- HTML table/div boundaries and bullet prefixes are normalized before action parsing. Two actions in one XBRL text block and two people in one filing remain separate; member facts later in the page do not supply another person's date or quantity.
- Expiration dates are accepted only from explicit `expires`, `expiration date`, or future `will terminate` wording. A past termination action or a plan's original “dated” date cannot become an expiration date.
- Input is capped at 1,000,000 characters and the located Item 5/fact section at 200,000 characters. Oversize input is refused before regex scanning.
- Negative disclosures still return `empty`; action prose without person/date still returns `refused`; unknown quantities remain null.
- The event remains `insider_trading_plan`, and event-judgement context explicitly calls it a plan action rather than an executed trade. Existing closed-enum, payload validation, source-capability, ownership/Form 4, buyback, tracking, and judgement tests passed.
- Event authority idempotency includes company ref, kind, and validated payload hash. The producer event key includes accession, person, action, date, plan type, and excerpt hash, separating issuers, filings, people, and actions.

The official fixture hashes match `tests/fixtures/sec-item5/MANIFEST.json`. The repair does not change fixtures, schemas, connectors, governance, deployment, or the cost-template files.

A conservative limitation remains: terms shown only in separate XBRL scalar rows are not joined back into prose events. For example, EOG's expiration date remains null because its prose date is in a continuation block and its XBRL scalar context is not joined. This loses optional detail but avoids context cross-association; the action, person, and date are still emitted from the bounded text block.

## Verification

```text
PYTHONPATH=src python3 -m unittest tests.test_insider_trading_plan tests.test_research_event tests.test_event_judgement tests.test_mission_event_judgement_lane tests.test_buyback_disclosure tests.test_s5_ownership_lane tests.test_mission_tracking_lane tests.test_tracking_cadence tests.test_source_capability_map
```

Result: 332 tests passed in 9.343 seconds. `git diff --check` passed.

# Structured cash-flow companion review — 2026-09-11

Status at 21:18 UTC: implemented and integrated for review, **not accepted for deployment**. The current live release remains R14c1.

## Scope and contract

The company-specific income DAG upgrade had dropped the existing operating cash flow, capital expenditure and free cash flow result lines. Company specification 0.4 now explicitly declares a cash-flow companion beside the income structure: exact filed concepts, a declared share-of-income-line forecast basis or unavailable, and FCF = OCF minus positive-outflow CapEx. The selected concepts drive the actual input query, including company-specific concepts; fixed standard candidate lists cannot silently override a new specification. Source objects/hashes, company/spec identity, income structure, reporting currency and formula are bound into the new forecast model 0.4. Old specification/model 0.3 behavior is preserved.

Actualization, sensitivity, reports and persisted annual projections consume the same results. Annual cash flows require four compatible quarter windows and preserve cumulative-difference source operands. Missing, incomplete or incompatible data remains unavailable, never zero. This is a bounded cash-flow companion: it supports explicit share_of_line or unavailable, and does not claim a complete cash-flow statement or balance-sheet model.

Author core `081585fd2762919f0eb8a80bea3752e6bcaa1d00` and normal-authority-flow test `d33cc9211ea7a92f88638412fce0dd8fc05dcde5` are integrated as `61587113` and `aad834d3`. The flow test covers stored specification → exact custom-concept input → structure/replay → normal forecast publication/readback → persisted annual FCF. Initial author coverage: 313 focused tests passed; the full forecast module including the new flow: 32 passed.

## Independent evidence

A read-only snapshot of the five latest live company models contained five schema 0.2 models and 500 stored result cells. Running installed R14c1 and an exact Git archive of the new core against the same snapshot produced identical canonical stored model bytes, readiness output and recomputed legacy results. This proves compatibility against that snapshot; it does not claim those legacy models have the new company-specific structure. No live writes or paid calls occurred.

- Snapshot SHA: `25955cf3968e853e4c2af1f250dccdf6533d5c6118901947ddf532c5f154393c`.
- Equal result SHA: `8afe564754c290e3e775e38774f0c716b3ec68ece8f73e1793ffa505fd257df2`.
- Private receipt SHA: `018c51ebce1be47aac0b2cca1fa0c6cf420fce2bcc82e3d710c5be20e69e36a8`.

Root integration coverage passed 64 forecast, cash companion, structured consumer and template tests in 10.518 seconds. Independent source review found two remaining blockers despite the passing suites:

1. A cash source using `USD` and a cumulative-difference quarter builds but cannot publish because driver units normalize to `usd` while derived operands retain `USD`. Correct the new boundary without rewriting legacy source/replay.
2. Duplicate quarter ends with different starts can survive source readiness and later overwrite one another in end-keyed actual/annual consumers. New cash inputs must reject ambiguous or overlapping windows explicitly.

## Excel acceptance and next steps

The Desktop AMZN reference remains authoritative. Existing actual share-of-line Driver ratios now use Financials-linked Excel formulas, with green cross-sheet formula styling; forecast assumptions remain blue inputs. Author export commits `a6ebe6fa` and `3e1e3fb5` are integrated as `1a1ed0ce` and `d5d4a41c`. Final 0.4 export still needs the separate light-blue Cash flow section, relocated formula references and exact cash annual-projection consumption.

Close the two independent core findings, rerun legacy compatibility after the unit change, independently review the final core, then render/recalculate the completed export against internal quarterly/annual values and actual ratios. Only after those checks should the combined financial and bounded daily-budget recovery release be frozen for full-suite, wheel, copied-state and deployment acceptance.

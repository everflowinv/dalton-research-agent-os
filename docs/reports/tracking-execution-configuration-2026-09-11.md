# Tracking execution configuration — 2026-09-11

Installed tracking policy can now supply a closed `execution` block containing positive integer `interval_seconds`, `max_events_per_run`, `max_candidates_per_source`, and `lookback_days`. The coordinator consumes the cadence and binds the configured policy hash to the run window. Actual CLI writes and all five company ledger/disclosure candidate scans consume the limits. Explicit CLI run overrides remain final. Existing policies retain their previous hash, hourly cadence, 120-event ceiling, and legacy per-source defaults.

The existing `abnormal_move.max_lookback_trading_days` setting now reaches the price detector instead of being ignored in favor of a constant. Summaries expose effective run and scan limits. Display-only due-event summaries retain their existing 20-row bound; this change does not claim to configure every bound in Dalton.

Validation: 70 tests passed across tracking execution, resident lane and cadence. A real three-claim fixture writes only one configured event, then an explicit run override writes the remaining two. Tests also verify source scan propagation, changed policy schedule identity and price lookback propagation. No live policy was changed.

Next: include in the foundation release and preserve the installed policy unless a concrete configuration delta is reviewed. Continue the wider limits audit; a configurable default alone does not prove all consumers honor it.

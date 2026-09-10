# CTSH AlphaEngine cadence diagnosis — 2026-09-10

## Finding

The live block was not a rolling 24-hour quota exhaustion. At 17:32:13 UTC the trailing window contained 54 of 130 permitted invocations (40 successful physical calls, 13 failed calls, and one record without an attempt); the 17:31 source-discovery heartbeat had 77 calls remaining and launched five acquisitions.

CTSH `earnings-call-transcripts` was instead skipped as `rediscovered 0d ago; interval 7d` while the accepted checklist remained 2/4. The coordinator classified any successful search transport as successful rediscovery, even where accepted, correctly attributed material remained below the checklist floor. Wrong-company results can therefore be dismissed correctly while the company is still suppressed for seven days.

A second recovery defect existed in the bounded planner. `ALPHAENGINE_PROBE_BUDGET_EXCEEDED` was returned as a failed result envelope and then completed as a terminal Scheduler result. The rolling window could reopen, but that admitted work would never execute again.

## Change

- A successful discovery with a real checklist shortfall now uses the plan's existing `retry_interval_days`; sufficient coverage retains `rediscovery_interval_days`.
- Discovery candidates are ordered by least-recent company attempt so the first incomplete company cannot consume every newly opened retry window.
- A rolling-window probe refusal leaves its admitted round open. A later tick retries the same round and can complete it after capacity returns.

This does not raise any signed or owner cap.

## Validation

Three focused regressions pass:

- successful searches below the accepted evidence floor retry after the short interval when a real continuation cursor exists, and the cursor reaches the governed request;
- a failed acquisition still follows its existing retry behavior without colliding with a replayed first-page source envelope;
- a quota-window refusal writes no terminal outcome, then the same round resumes successfully on the next tick.

A same-environment parent/current comparison ran both complete modules. Parent: 53 tests with two existing child-process failures. Current: 56 tests with the identical two child-process failures and no additional failure. The three added deterministic regressions pass independently.

## Remaining acquisition limitation

The plan's CTSH query remains broad. Continuation now consumes the provider's opaque cursor while it exists; when the provider reports no cursor, the coordinator retains the longer rediscovery cadence instead of replaying page one at the short interval. Period-targeted query evolution remains a separate acquisition-quality improvement after pagination is exhausted.

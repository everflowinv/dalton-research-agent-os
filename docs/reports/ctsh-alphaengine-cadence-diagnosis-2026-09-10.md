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

Two focused regressions pass:

- successful searches below the accepted evidence floor retry after the short interval and CTSH is reached despite an earlier incomplete company;
- a quota-window refusal writes no terminal outcome, then the same round resumes successfully on the next tick.

The complete source-discovery module currently has unrelated clock-sensitive fixture failures because its fixed September 2 mission date is outside the running September 10 process assumptions. The two new deterministic regressions pass independently.

## Remaining acquisition limitation

The plan's CTSH query is broad and discovery parameters always begin with a null cursor. Authority-level document deduplication prevents an existing document from being requeued, but repeated searches can still spend calls on the same first-page results. The cadence fix makes the retry honest and fair; period-targeted query evolution or persisted pagination remains a separate acquisition-quality improvement.

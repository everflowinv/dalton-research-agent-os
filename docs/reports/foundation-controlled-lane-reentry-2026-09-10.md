# Controlled lane re-entry helper — 2026-09-10

`eligible_controlled_reentries` is a bounded, read-only bridge from a child
summary's `failed_model_traces` to the existing controlled-redrive authority.
It accepts at most 16 traces, validates each trace against the canonical
Scheduler WorkOrder and failed formal result through the same Cockpit trace
reader, and calls `approved_request` to revalidate the installed host repair,
mission binding, immutable approval, and conservative cost correction.

An approval is eligible only until its exact operator-recovery request has been
enqueued. The helper derives that lookup key from the authority-bound original
request and approved suffix, preserving any producer-route suffix. It does not
enqueue work, create a request ID for a caller, clear a lane hold, alter budget
or Scheduler state, or infer relationships for historical child tickets that
carry no trace.

The intended coordinator integration is narrow: when an exact company/item
signature is held, load that signature's persisted child summary and call this
helper under the current mission. A nonempty result permits one relaunch of the
same exact child input without clearing the content hold. The child makes its
unchanged base model request; `CockpitModel.call` sees the approved record and
performs its existing suffix-bound recovery. Once that recovery WorkOrder is
enqueued, the helper returns no candidate. Missing ticket-to-signature history,
authority drift, consumed approval, and absent trace all remain held.

Validation:

```text
PYTHONPATH=src:. python3 -m unittest \
  tests.test_controlled_lane_reentry.ControlledLaneReentryTests -v
```

All 16 tests passed, including the inherited controlled-redrive authority
regressions and new eligible-once, consumed-on-enqueue, mission mismatch,
malformed trace, bounded input, and byte-stable read-only cases. No live state,
network, broker, model call, or coordinator was changed.

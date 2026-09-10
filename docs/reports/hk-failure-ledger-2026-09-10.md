# HK coordinator failure ledger

The HK coordinator now uses the shared persistent `LaneFailureBudget`. Work
identity binds company, operation, effective day (`as_of` or `until`), complete
parameters, and the exact governance file bytes. Failures from settled child
summaries retain their real reason and are classified as dependency,
permission, content, or transient rather than sharing one company counter.
For the next-day company view, the identity also binds the optional shared
`daily_buyback_tape` governance record.

Content refusal is terminal only for that exact input. It does not suppress a
different operation for the company, another company, or a later day.
Dependency outages consume no transient budget and survive restart; the shared
budget admits the post-restart child as the real recovery probe. Changing the
governance record produces a new input and retires the superseded hold without
claiming that a dependency recovered.

Mission write-scope refusal is recorded once under a top-level permission
namespace and cleared independently from child permission holds. A real child
summary saying its governance record is not approved is normalized to
`not_permitted`, so it consumes no transient budget and recovers on a control
change.

Successful empty HKEX output remains success: the coordinator marks the exact
operation/day current and emits no error. The four-operation ordering and the
shared daily-acquisition child behavior are unchanged.

Focused tests cover repeated transient hold, content refusal isolation by
operation, dependency persistence and restart probe, real governance-file
identity change, empty success, event history, and all existing HK child,
launcher, adapter, inventory, and cache behavior. No live fetch, approval,
signing, or deployment was performed.

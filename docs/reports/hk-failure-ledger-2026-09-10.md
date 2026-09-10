# HK coordinator failure ledger

The HK coordinator now uses the shared persistent `LaneFailureBudget`. Work
identity binds company, operation, effective day (`as_of` or `until`), complete
parameters, and the exact governance file bytes. Failures from settled child
summaries retain their real reason and are classified as dependency,
permission, content, or transient rather than sharing one company counter.

Content refusal is terminal only for that exact input. It does not suppress a
different operation for the company, another company, or a later day.
Dependency outages consume no transient budget and survive restart; the shared
budget admits the post-restart child as the real recovery probe. Changing the
governance record produces a new input and retires the superseded hold without
claiming that a dependency recovered.

Successful empty HKEX output remains success: the coordinator marks the exact
operation/day current and emits no error. The four-operation ordering and the
shared daily-acquisition child behavior are unchanged.

Focused tests cover repeated transient hold, content refusal isolation by
operation, dependency persistence and restart probe, real governance-file
identity change, empty success, event history, and all existing HK child,
launcher, adapter, inventory, and cache behavior. No live fetch, approval,
signing, or deployment was performed.

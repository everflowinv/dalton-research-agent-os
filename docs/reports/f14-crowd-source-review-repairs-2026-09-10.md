# F14 crowd-source review repairs

## Result

Crowd-source failure state now names the exact work that failed. The business
key binds source, company, operation, and the complete emitted parameter map,
including `since`, query, handle, or employer slug, plus the UTC run day and
that source's governance digest. A content refusal remains terminal for the
same input on the same day, while the next day, a changed parameter, or a newly
approved governance record becomes a new input. Company prefixes keep failures
isolated.

Superseded input records are retired with the failure ledger's `superseded`
event. Replacement does not emit `dependency_ok`, so changing one company's
query cannot release another company's source outage. Success clears the same
exact business key that was launched.

The mission grant uses a dedicated `permission|top|crowd_source` namespace and
the shared permission-control fingerprint. Repeated ticks reuse the same
permission projection instead of appending a new hold. Grant recovery retires
only top-level permission rows; it does not erase connector-child permission
holds. Child governance refusals bind the exact job to mission, policy, and
launcher controls, survive process restart, and are retried when those controls
change.

The initial configuration observation no longer clears persistent permissions.
Later configuration changes only age in-process transient cool-offs; persistent
permission recovery remains controlled by its fingerprint.

## Validation

`tests.test_crowd_source_lane` covers same-input terminal refusal, a new `since`
input, cross-company isolation, superseded-without-dependency-release, child
permission persistence across restart and recovery after mission control
change, top-level permission deduplication without child clearing, and a real
`CrowdSourceExecution` runner exception classified from its emitted reason.
It also replaces a real launcher's proposed governance file with an approved
record and proves that the same company/job is admitted under the new control.

No live connector, governance approval, signing, or deployment was performed.

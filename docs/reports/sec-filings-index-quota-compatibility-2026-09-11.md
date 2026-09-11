# SEC filings-index immutable quota compatibility

The live filings-index rate policy v1 was signed and activated with 50 calls,
400 MiB, and 5,000 records per window. The current governed configuration has a
200-call ceiling. Re-registering the changed body under the same immutable v1
idempotency key caused `ConnectorConflict` before transport.

The lane now replays an existing v1 only after validating its stored content
hash, exact active activation event and hash, effective interval, profile,
scope, price book, actor, currency, window, and every non-limit field. Every
stored limit must be no wider than the current governed ceiling. A compatible
lower policy remains active and reports `configured_ceiling_not_activated` with
both limit maps. Wider or incomparable drift remains refused. A fresh database
continues to register the current 200-call policy normally.

No policy version, owner approval, quota increase, provider call, or live state
change is created by this compatibility path. Existing reservations and spend
remain under the original policy and stable quota scope.

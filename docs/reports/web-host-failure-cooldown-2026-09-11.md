# Web host failure cooldown

Web acquisition plan schema 0.5 adds an explicit, hash-bound
`failure_cooldown` policy. Existing plans remain inactive and byte-compatible.
The configured fields are the minimum number of distinct failed URLs, the
lookback window, and cooldown duration.

Each completed acquisition now appends an immutable attempt row bound to the
document, ticket, exact host, typed outcome, transport code, and connector
invocation evidence. Legacy failures remain `unknown_failure` and do not count.
Only failures carrying both a concrete transport code and typed terminal
disposition count. Configuration, permission, and unknown failures do not.

Cooldown is scoped to the workspace database and source and matches the exact
host. A later success resets earlier failures in the window. Expiry permits a
half-open probe when an eligible URL exists; a new typed terminal failure begins
a new cooldown. A URL already classified terminal is not silently made
retryable, so a host with no other eligible URL requires a new discovery or an
explicit governed administrative recovery.

Dispatch summaries expose `cooldown_hosts` with host, reason, distinct URL
count, expiry, and next-probe time. Manual `skip_hosts` remains authoritative
and permanent until its signed plan changes.

The private, unactivated candidate uses 3 distinct URLs in 24 hours and a
6-hour cooldown. It is stored under the owner activation packet; no live plan
or runtime state was changed.

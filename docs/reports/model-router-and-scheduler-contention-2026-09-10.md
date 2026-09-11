# Model routing and Scheduler contention review

This review separates two SQLite failures observed in the live R6 state. It did
not mutate live state or dispatch any work.

## ModelRouter finding

The live `model-router.sqlite` used rollback-journal (`delete`) mode. Dossier
run `0285d58f07cd089487682a61` received a model response and then failed while
`ModelRouter.record_chain_link()` committed. A concurrent reader can block that
commit in rollback-journal mode. Commit
`f650f614c24af0a935120235359617232bda56ff` migrates owned writable disk router
connections to WAL. Its regression holds a separate read transaction open
while a real served chain link commits.

## Scheduler finding

The live Scheduler already uses WAL. Fetch run
`6946a83208cc76dd0de25929` instead timed out acquiring `BEGIN IMMEDIATE` in
`Scheduler.enqueue()`. This is writer-versus-writer contention and is not fixed
by the ModelRouter journal change.

`Scheduler.claim()` and `sweep_expired()` called `_expire_due()` inside their
write transaction. The old query materialized the latest event for every work
order before selecting current leases. On a read-only copy of the live-sized
authority (10,923 work orders and 33,430 attempt events), that query took about
344 ms. The equivalent query that starts with leased candidates and uses the
existing `(work_order_id, event_seq)` index to exclude historical leases took
about 10 ms. Concurrent children serialize these scans behind the single WAL
writer, so a launch burst can exhaust the existing five-second wait even though
each individual transaction is bounded.

The follow-up change replaces only that query. It does not raise the busy
timeout or change lease, retry, transaction, or migration semantics. A focused
test proves completed work's historical lease is ignored while a different
current expired lease advances normally.

No source evidence showed network or model calls inside a Scheduler transaction.
Constructor schema checks can still briefly require the writer lock, and WAL
still permits only one writer. Further changes require a new concrete trace;
this patch does not claim to eliminate every possible Scheduler collision.

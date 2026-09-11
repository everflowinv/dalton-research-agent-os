# Model broker bounded queue

The broker now supports an explicitly enabled FIFO queue. Configuration binds
`maxQueued` and `maxQueueWaitMs`; the authenticated request separately supplies
`queueWaitMs`. Existing clients omit that field and retain immediate `BUSY`
behavior, even when the broker has queue capacity.

Admission occurs before the durable journal claim. Queue timeout and broker
shutdown therefore return `QUEUE_TIMEOUT` or `BROKER_CLOSED` without a pending
journal record or provider execution. A repeated identical invocation joins the
single queued promise; the same identifier with different provider request
content fails as an idempotency conflict. Replay-only misses never enter the
queue.

Queue waiting is separate from the provider deadline. The Python adapter must
set its transport and scheduler lease to queue wait plus provider timeout and
bind the selected queue wait into its configuration fingerprint. This companion
integration is intentionally required before activation.

The reviewed candidate values are 16 concurrent, 64 queued, and a 600-second
maximum queue wait. No installed OpenClaw configuration was modified.

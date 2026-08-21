# Dalton OpenClaw Model Broker

This directory contains Dalton's optional OpenClaw integration for OpenClaw
2026.7.1. It was designed against the documented
`api.runtime.llm.complete` plugin runtime surface. Unit tests use a fake host
runtime; they never load OpenClaw configuration, resolve credentials, or send a
model request. Installation and live model smoke tests are separate deployment
steps.

## Boundary

- The plugin owns a mode-`0600` Unix socket beneath its OpenClaw plugin state
  directory. It accepts exactly one closed JSON object plus newline per
  connection. JSON objects with duplicate keys are rejected before parsing.
- Every frame carries `auth: { scheme, clientId, timestampMs, nonce, mac }`.
  `mac` is HMAC-SHA-256 over the canonical request without `auth.mac`. The
  owner-only key is created in `<socketName>.key`; it never enters a response,
  process argument, or log. The client must create a fresh 16-byte hex nonce
  and HMAC for each retry. The broker enforces client identity, timestamp skew,
  MAC validity, and nonce replay protection before request validation.
- Apart from that authentication envelope, a request contains only protocol
  version, invocation/work-order IDs, profile, exact canonical model, prompt,
  maximum tokens, timeout, and the optional boolean `replayOnly` instruction.
- The plugin chooses one configured dedicated agent and one exact model per
  configured profile. A client cannot choose an agent, endpoint, header,
  credential, or authentication profile.
- Prompts are passed in memory to `api.runtime.llm.complete`; they are not put
  in process arguments or plugin logs.
- The plugin has no Dalton database path, writer credential, model credential,
  or credential resolver. The socket is an execution adapter, not a Dalton
  commit boundary.

## Persistent idempotency boundary

Before calling the host, the broker atomically records a `pending` invocation
with only invocation ID, canonical request hash, timestamps, and state. It then
atomically replaces the snapshot after completion. A restart returns the saved
closed response for an identical completed request and returns conflict for a
different request. A request left `pending` by a crash is reported as
`IDEMPOTENCY_INDETERMINATE`; the broker never silently calls the host again.
An authenticated `replayOnly: true` request can read the completed or pending
record but cannot create a journal claim or call the host. A journal miss is
returned as `IDEMPOTENCY_MISS`. `replayOnly` is excluded from the provider
request hash, so recovery must match the original request exactly.

The bounded journal is `<socketName>.journal.json`, mode `0600`, written through
same-directory temporary file + fsync + atomic rename. Configured TTL applies
only to completed responses. Record-count and byte pruning may likewise remove
only completed rows. A pending row is never automatically deleted, even after
TTL, because time cannot prove that the host call did not run; pending rows can
only be cleared by a future explicit operator-resolution boundary. If pending
rows consume capacity, new work fails closed. The journal never stores the
request, prompt, HMAC, key, or authority fields. To make a post-restart
duplicate useful, it does store the minimal closed response, including model
output text. That file is therefore a sensitive output cache protected by the
plugin state directory and owner-only permissions, not a general audit log.

The host additionally needs operator-controlled plugin runtime policy for
model and cross-agent overrides (`allowModelOverride`, `allowedModels`, and
`allowAgentIdOverride`). The repository does not edit that policy.

## Test

```bash
npm test
```

The tests cover closed request validation, exact route enforcement, host
attribution, usage/cost normalization, fresh/duplicate/conflict idempotency,
timeouts, output/frame/concurrency limits, socket permissions, prompt-safe
errors/logs, and static authority isolation.

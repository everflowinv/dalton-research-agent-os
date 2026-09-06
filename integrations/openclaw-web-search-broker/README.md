# Dalton OpenClaw Web Search Broker

Host-owned web search bridge for an external Dalton runtime, built on the same
boundary as the Dalton model broker in `../openclaw-model-broker`. It exists
because OpenClaw exposes web search to its own agents and over its plugin
runtime helper, not over MCP: without this plugin a Dalton search child has
nothing to call, and P9d-4a's networked launch refuses before spawning.

Unit tests use a fake host runtime; they never load OpenClaw configuration,
resolve a provider credential, or run a real search. Installing the plugin and
running a first real search are separate, owner-gated deployment steps.

## Boundary

- The plugin owns a mode-`0600` Unix socket beneath its OpenClaw plugin state
  directory and accepts exactly one closed JSON object plus newline per
  connection. Objects with duplicate keys are rejected before parsing.
- Every frame carries `auth: { scheme, clientId, timestampMs, nonce, mac }`.
  `mac` is HMAC-SHA-256 over the canonical request without `auth.mac`. The
  owner-only key is created at `<socketName>.key` and never appears in a
  response, process argument or log. Client identity, timestamp skew, MAC
  validity and nonce replay are all enforced before request validation.
- Apart from that envelope a request contains only protocol version, the
  caller's `callRef`, the broker profile, the exact `query`, a bounded
  `count`, an optional explicit `dateAfter`/`dateBefore` window, `timeoutMs`,
  and the optional boolean `replayOnly`. **A client cannot send or select an
  API key, provider, model, endpoint, base URL or header.**
- Provider selection and credentials stay in the host. The broker calls the
  shared `api.runtime.webSearch.search` helper with the host's own config and
  refuses the result if the provider the host used differs from the
  configured `expectedProvider`, because a silent provider swap would change
  the payload contract the Dalton adapter pinned.
- The provider payload is returned verbatim inside a tool-result envelope
  (`result.content[0].text`), which is the shape Dalton's frozen public-web
  adapter and URL-authority rebuild already parse. The broker never rewrites,
  re-ranks, summarizes or filters results.
- Nothing about the query or the results is logged. Host error text is
  bounded to 300 characters and classified into permission, rate-limit and
  generic provider failures; the query is never echoed back in an error.

## Idempotency

`callRef` is Dalton's credential-use reference for one physical attempt, so it
is a precise key: a retry on a new attempt is a new key, which is correct
because it is a new paid call. Before calling the host the broker atomically
records a `pending` claim holding only the call ref, a canonical request hash
and timestamps. It replaces that snapshot after completion, so a restart
returns the saved response for an identical request and a conflict for a
different one. A request left `pending` by a crash is reported as
`IDEMPOTENCY_INDETERMINATE`; the broker never silently searches again. An
authenticated `replayOnly` request can read a completed record but can never
create a claim or reach the host, and a miss returns `IDEMPOTENCY_MISS`.

The bounded journal is `<socketName>.journal.json`, mode `0600`, written
through a same-directory temporary file plus fsync and atomic rename. It
stores the minimal closed response, which includes result content, so it is a
sensitive output cache protected by the plugin state directory, not a general
audit log. It refuses to persist records carrying credential-shaped fields.

## Configuration

Required: `clientId` (`client:<name>`) and `expectedProvider` (for example
`gemini`). Optional: `profileId`, `socketName`, `maxQueryChars`, `maxCount`,
`maxFrameBytes`, `maxResponseBytes`, `maxConcurrent`, `idleTimeoutMs`,
`maxTimeoutMs`, `authMaxSkewMs` and the journal bounds. The host must have a
web search provider configured and enabled; this plugin adds no provider and
reads no credential itself.

## Client

The Dalton client is `dalton_core.openclaw_web_search_broker_client`. A client
writes its frame and keeps its side of the socket open: the broker replies by
ending its side, so a client that half-closes first would race the reply.

## Checks

```bash
npm run check   # node --check on every source file plus the unit tests
```

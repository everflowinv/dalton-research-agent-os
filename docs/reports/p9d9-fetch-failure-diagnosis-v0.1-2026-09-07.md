# P9d-9: a failed fetch now says why

*2026-09-07*

## What the live run showed

After the web chain went `connected`, the mission ledger filled with fetch
failures that all read the same way:

    acquisition_failed   acquisition ended failed (exit 1)

Five in a row, one per tick. The ledger recorded that a governed call had been
spent and had not produced a document, and nothing else. An operator could not
tell whether the network had blipped, the connector had been refused, or the
host simply does not serve automated clients.

Probing the URLs by hand answered it: `news.alphastreet.com` returns **HTTP 403**
to the lane's user agent, on every path. The information had existed all along.
The adapter observed the 403, wrote it into a `ResultEnvelope` as a closed
`{code, message, retryable}`, and the fetch receipt then dropped it on the floor.

## Why it was dropped

`ConnectorRunnerResponse` is a closed shape and deliberately carries no error
field. It does name a `ResultEnvelope` and bind it by hash, and *that* record
holds the error. `PublicWebCoreFetch._receipt` never read it, so
`public_web_fetch_cli` could say no more than `fetch outcome failed`, and the
coordinator overwrote even that with the exit code.

Notably the **search** side already did the right thing: `settle_dispatches`
prefers the child's own `failure_reason` and falls back to the exit code. Only
the fetch side, added later, lost the detail. The asymmetry was the bug.

## The change

Three layers, one fact travelling further:

1. **`PublicWebCoreFetch._receipt`** carries `error` from the `ResultEnvelope`.
   The envelope is authority, so it is read only through the hash the response
   binds it to. A drifted envelope fails closed rather than reporting a reason
   that no longer belongs to the call.
2. **`public_web_fetch_cli`** puts `error` in `summary["fetch"]` and composes a
   specific `failure_reason`, e.g.
   `fetch outcome failed; public web fetch returned HTTP 403; not retryable`.
3. **`settle_documents`** prefers that reason over the exit code, matching what
   `settle_dispatches` has always done for search.

Nothing new is fetched, no new field is invented, and no closed contract is
widened. The receipt is not an authority record, so carrying the error costs no
schema change.

## What this does not fix

- **The 403 itself.** The lane identifies itself honestly and the host refuses
  it. Spoofing a browser user agent to get around a bot block is not something
  this lane will do.
- **Wasted calls on a blocked host.** Every URL on a blocking host will still
  cost one governed fetch before failing. With 72 documents discovered and a
  200/day fetch quota, that is real waste. A host-level block memory, which
  skips a host after a non-retryable refusal, is the obvious follow-up and is
  now cheap to build: the reason string finally says which failures qualify.
- **The `robots.txt` question.** The transport still does not read it.

## Still open from earlier

- Publishing a mission version orphans documents discovered under the prior one.
- Documents marked `already_in_authority` at search time never enter the human
  review queue.

## Verified live

At 06:45 the deployed lane settled a fetch as:

    acquisition_failed   fetch outcome failed; public web fetch returned HTTP 403; not retryable

which is the whole point of the change.

## P9d-10: the provider's redirect proxy is not a source

Recovering the discovered URLs from the connector spool and grouping them by
host turned up a second defect. Two admitted documents were not articles at all:

    https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ...

Gemini grounding sometimes cites its own redirect proxy instead of the
publisher. Admitting one as a document is wrong four ways:

1. It names no publisher, so it is useless as a citation to a human reviewer.
2. The token expires, so the "durable, re-verifiable source" guarantee breaks.
3. The same article reached through two tokens hashes to two document refs, so
   dedup silently fails.
4. It can never yield bytes anyway. The fetch profile pins `allowed_hosts` to
   the URL's own host, so the transport refuses to follow the redirect to the
   real publisher. Every such ref is one governed call spent to fail.

`normalize_gemini_web_search_payload` now drops them, before the `max_records`
slice so a proxy link cannot cost a real citation its place.

The two already in the live ledger are left alone. The ledger is append-only and
deleting them is not mine to do; each will fail once, now with a legible reason.

## Backlog shape, for whoever picks this up

103 documents discovered, across roughly 50 hosts, mostly one to five each. So a
host-level block memory is worth less than the failure pattern first suggested.
The blocked hosts so far are `seekingalpha.com`, `news.alphastreet.com`,
`www.spglobal.com`, `stockanalysis.com` and `www.reddit.com`. What succeeded:
`quartr.com`, `www.tikr.com`, `news.futunn.com`.

The more interesting gap is ordering. First-party investor-relations hosts
(`newsroom.accenture.com`, `investor.accenture.com`, `investors.epam.com`) sit
in `discovered` while the coordinator works the queue one document per tick.
Those are the sources the mission actually wants, and nothing prioritizes them.

## One more operational note

Each deploy boots the writer out, which orphans whatever discovery or fetch
child is in flight. That failure then parks the company/spec pair for a day
("last attempt failed 0d ago; retry interval 1d"). Two deploys today burned two
slots that way. Draining in-flight children before `bootout` would fix it.

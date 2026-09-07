# P9d-15: model-drafted extraction suggestions for public-web pages

*2026-09-07*

## Why this is the next step

The vision measures value in human-accepted Claims produced under a fixed cost,
with people intervening only at checkpoints. After P9d-7 the web lane runs
end to end on its own: searches, fetches, verified originals, a review queue.
But at the queue it stopped being autonomous. A fetched page could be read,
paged through and dismissed, and nothing else: model drafting was gated with
`public_web_extraction_drafting_not_supported`, so every candidate had to be
found by a person reading the page. AlphaEngine documents already get budgeted,
human-triggered drafting (P9d-3b). With the lane now fetching ten or more pages
a day at the raised cap, a queue that needs a person to read every page is the
bottleneck the vision says should not exist.

The alternative on the roadmap, the Guidepoint bridge, adds a source to a queue
that already cannot be drained. The 2026-08-26 review froze new bridges until
the engine produces; this slice is engine work.

## What changes

`DocumentExtractionService.view` and `generate` no longer special-case
`source:web-search`. A web review is gated by exactly what gates an AlphaEngine
review: the approved extraction model configuration and the daily budget policy.
When those are installed, the cockpit's generate button is live on web pages.

Nothing about the drafting contract changes. The context for a web page was
already a verified, deterministically rendered original
(`public_web_extraction_source`): the citable text is the rendering of the exact
fetched bytes, and the renderer identity is part of what the context binds. The
prompt, the output schema, the five-suggestion limit, the numeric-statement
refusal, the untrusted-data framing and the task hash are all unchanged, so a
drafted suggestion is a quote of the rendering plus a qualitative statement,
never an accepted Claim, and replay returns the persisted result without a
second model call.

**Staging is still refused for web pages.** The gate reason is now
`public_web_candidate_staging_not_supported`, and the reason is real: the
candidate chain publishes a transcript correction set, binds a claim citation
through the transcript correction authority, and stages through
`stage_transcript_qualitative_candidate` with AlphaEngine document lineage. A
public-web source needs a citation authority of its own (fetch manifest, body
hash, rendering hash and renderer identity, span, human review) and a staging
path that carries that lineage into Evidence. That is P9d-16, and it needs an
ADR addendum: ADR-0003 B says transcript semantic candidates are human-accept
only, and web semantic candidates must be held to the same rule.

## What a person gets today

Open a fetched page in the queue, press generate, and the model proposes up to
five attributed qualitative statements, each pinned to an exact quote of the
page. The person decides what is worth carrying forward. Until P9d-16 the way
to carry it forward is still by hand, but the reading is no longer.

## Verification

A hermetic-fixture worker drafts against a real fetched page in the writer-ops
harness: the suggestion cites the exact rendering, binds the context's content
hash, is marked `pending_human_citation_admission`, replays without a second
adapter call, and leaves Claim, Evidence and citation counts unchanged. Staging
the drafted suggestion is refused with the new reason.

No live model call was made for this slice. Drafting is human-triggered, and
the first live draft on a web page is the owner's to press.

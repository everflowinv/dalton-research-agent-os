# P9d-17c: fetched pages are admitted through the same chain

*2026-09-07*  ·  ADR: [0005](../adr/0005-autonomous-document-extraction.md)

## The question this slice answered

A public-web page is not a transcript. Its raw bytes are HTML or PDF; the
text a person or a model reads is a deterministic rendering of those bytes,
and the renderer's identity is part of what makes that text citable (P9d-7).
The admission chain built for AlphaEngine documents assumed the original *is*
the raw bytes: the correction authority read them from the spool, the Ledger
writer required the citation's content hash to equal the artifact's, and the
resolver, binder, builders and evaluator all named the transcript kind.

The choice was between a parallel chain for web pages and one chain with two
source kinds. This slice does the latter: every record type, table, contract
and verifier already existed; what changed is that each place that assumed
"transcript" now reads the kind from the source.

## What changes

**The correction authority reads a fetch manifest.** `_source` dispatches on
the manifest id: an AlphaEngine acquisition manifest yields the assembled raw
text as before; a `public-web-fetch-manifest:` yields the rendering produced by
`verified_public_web_source`, which re-verifies the fetch receipts, the raw
bytes and the rendering hash. The correction set records the page record ref
as its `document_ref` (contract widened to `public-web-document:`), the
manifest, and the rendering hash as `source_content_hash`. A renderer change or
drifted bytes changes that hash and fails closed. Everything downstream of the
correction set, the automation scope, the citation binding, the resolution
checks, is unchanged.

**The resolver, binder and builders take a source kind.**
`TranscriptCoreAuthorityResolver(source_kind="public_web")` expects the fetch
SourceEnvelope (`source:public-web`, `fetch_get`, exactly one record: the page),
carries the `public_web_core_authority` provenance mode and its own verifier
identity, and names the envelope explicitly since a fetch has no page-1
digest to locate. The evidence binder relabels evidence as `public_web`
(contract extended symmetrically), the qualitative candidate builder accepts
either cited kind, and `stage_transcript_qualitative_candidate` takes
`source_kind`.

**The staging store and the Ledger writer accept the web kind.** Public-web
evidence with a citation binding is cited evidence; the writer adds a
`public_web_binding` next to the two transcript bindings: the fetch envelope
names exactly the page record and the raw artifact is the fetched body. The
rendering hash itself was verified against those bytes by the correction
authority when the span was admitted, the same trust the AlphaEngine binding
places in the document digest.

**The policy rule admits either kind.** The mission document qualitative rule
requires an acquired AlphaEngine original *or* a fetched public-web page, with
the envelope's operation matching the kind. Every other condition (automation
producer, no numeric assertion, exact eligible citation, automation scope by
the same principal) is unchanged.

**The child holds on conflicts.** Found while building this: an infrastructure
conflict in the chain had dismissed a review as if every suggestion had been
judged. A refusal now counts as a judgment only when the policy or a validator
refused the suggestion itself; a conflict or unexpected error holds the review
open with the reason.

## Verification

End to end with the real fetch child on a fake page under a connected, granted
mission, then the real extraction child in hermetic mode with staging: the
draft is staged and admitted under the rule; the Ledger gains one Claim and one
Evidence version with `source_type: public_web` and `source:public-web`, two
artifact refs (raw body, citation binding); the correction set carries the
automation scope, the page record ref and the fetch manifest; the review closes
as `extraction_staged`. Every existing transcript, qualitative-candidate,
review and admission test still passes, including the adversarial ones that
require qualitative candidates without cited evidence to be refused.

## Not in this slice

The human `stage` op still refuses web pages; the automation path is the one
ADR-0005 asks for, and a human who wants to stage by hand can do so from an
AlphaEngine review today. Thesis admission remains human (ADR-0001).

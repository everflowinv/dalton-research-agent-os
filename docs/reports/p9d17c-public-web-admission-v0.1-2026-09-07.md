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

## What the first fifty automated Claims looked like

Counting was not enough. A read of the admitted statements showed two things
the chain had no way to catch:

- **Disclaimers as Claims.** "J.P. Morgan states that past performance is not
  indicative of future results." The statement is attributed, quotes an exact
  span, asserts no number, and passes every check, because every check is
  about provenance, not relevance.
- **The wrong subject.** Forty-nine statements about fuel prices, EV fleets
  and FTC litigation, admitted under Cognizant's subject ref. The AlphaEngine
  search for Cognizant returned a broker report about a payments company, and
  the prompt showed the model only a CIK ref, so it could not know the window
  was about someone else.

Two changes. The drafting context now carries the subject's ticker from the
mission universe, and the prompt opens with it: extract only reported views
about this company, its industry, customers or named competitors; if the
window is about a different company, or is a disclaimer, boilerplate or text
about the document itself, return empty suggestions. And a small deterministic
boilerplate filter drops the most recognisable disclaimer phrasings at parse
time and refuses them again in the policy evaluator.

The new context field re-keys every window, so the queue is drafted again
under the new prompt. The Claims already admitted stay in the Ledger; it is
append-only. They carry the automation producer and the policy rule, so they
are easy to find, and the owner can challenge or retire them from the cockpit.
Relevance remains a model judgment; the deterministic guards only remove the
cases no judgment is needed for.

# CTSH extraction-chain recovery — 2026-09-10

## Read-only production trace

The active mission is v14. Its CTSH earnings-call discovery used the approved
`earnings-call-transcripts` spec over 2025-08-06 through 2026-09-10. Twenty
search results were returned; company attribution admitted two and correctly
left unrelated issuers out of CTSH's discovered-document authority:

| Candidate | Published | Source classification | Current review state |
|---|---|---|---|
| `alphaengine-doc:130000101476615` | 2026-07-29 | CTSH Q2 2026, earnings-call spec | `awaiting_human_extraction` |
| `alphaengine-doc:130000111193961` | 2026-09-08 | CTSH Citi Global TMT conference, earnings-call spec | `awaiting_human_extraction` |

The second title is a conference rather than an earnings call. It remains an
unreviewed acquired candidate and must not be silently upgraded or dismissed
by this audit. The recorded stage counter therefore says 2/4, but strict
period coverage can currently prove only Q2 2026. A period-aware acquisition
follow-up should target the preceding calls in the approved trailing window;
the current authority does not prove which two periods would complete the
four-call requirement, and this report does not invent them.

The latest extraction children scanned roughly 100 open reviews each. Every
read attempt failed before model routing with `extraction must pin exactly one
approved model`, after which the child reported `succeeded` and
`nothing_to_draft`. The live extraction routing policy is v2 with an approved
three-profile fallback chain. Thus `awaiting_human_extraction` is the automatic
consumer queue, and the queue was blocked by a stale single-profile assertion,
not by human approval, cache replay, or fairness. CTSH's two rows were among
those skipped.

## Repair

`DocumentExtractionService.model_policy` now accepts an approved nonempty
profile chain (or an extraction-specific purpose override). The router still
chooses and records one exact immutable profile for each call. Empty policies,
superseded policies, and malformed policy bytes remain refused.

This changes no source requirement, does not re-enable the eight historical
wrong-company dismissals, and does not call a model or mutate production.

## Validation

The regression constructs a real ModelRouter policy with two eligible profile
families and proves extraction preflight accepts it. With an absolute source
path for child tests, the focused extraction policy and real fetched-page
admission tests passed.

## Shared-capacity bypass inventory

The governed Gemini web-search and AlphaEngine paths use
`ConnectorTransportExecutor` and are covered by the shared connector capacity
boundary. Direct `PublicHttpTransport` adapters and `bounded_probe_executor`
remain outside that boundary. They must not claim host-global capacity; wiring
them requires an explicit owner policy scope rather than treating public-source
access as an implicit shared quota.
